from __future__ import annotations

import math
from dataclasses import dataclass, field

from simp_sexp import Sexp

from .writer import PlacedPart, load_footprint


@dataclass(frozen=True)
class PadGeometry:
    number: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    shape: str = "rect"
    rot_deg: float = 0.0
    layers: tuple[str, ...] = ()
    net_name: str | None = None
    pad_type: str = "smd"

    @property
    def local_bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x_mm - self.width_mm / 2,
            self.y_mm - self.height_mm / 2,
            self.x_mm + self.width_mm / 2,
            self.y_mm + self.height_mm / 2,
        )

    @property
    def is_through_board(self) -> bool:
        return self.pad_type in {"thru_hole", "np_thru_hole"} or "*.Cu" in self.layers

    def transformed_bounds(self, placed: PlacedPart) -> tuple[float, float, float, float]:
        x_min, y_min, x_max, y_max = self.local_bounds
        corners = [
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ]
        points = [
            transform_point(placed.x_mm, placed.y_mm, placed.rot_deg, x, y)
            for x, y in corners
        ]
        return _bounds_union((x, y, x, y) for x, y in points)


@dataclass(frozen=True)
class FootprintGeometry:
    footprint: str
    pads: list[PadGeometry] = field(default_factory=list)
    body_bounds: tuple[float, float, float, float] | None = None
    courtyard_bounds: tuple[float, float, float, float] | None = None

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        if self.courtyard_bounds is not None:
            return self.courtyard_bounds
        if self.body_bounds is not None:
            return self.body_bounds
        if self.pads:
            return _bounds_union(pad.local_bounds for pad in self.pads)
        return (-1.0, -1.0, 1.0, 1.0)

    @property
    def physical_bounds(self) -> tuple[float, float, float, float]:
        bounds = []
        if self.body_bounds is not None:
            bounds.append(self.body_bounds)
        bounds.extend(pad.local_bounds for pad in self.pads)
        if bounds:
            return _bounds_union(bounds)
        return self.bounds

    @property
    def width_mm(self) -> float:
        x_min, _, x_max, _ = self.bounds
        return x_max - x_min

    @property
    def height_mm(self) -> float:
        _, y_min, _, y_max = self.bounds
        return y_max - y_min

    def transformed_bounds(self, placed: PlacedPart) -> tuple[float, float, float, float]:
        return self._transform_bounds(self.bounds, placed)

    def transformed_physical_bounds(
        self,
        placed: PlacedPart,
    ) -> tuple[float, float, float, float]:
        return self._transform_bounds(self.physical_bounds, placed)

    @staticmethod
    def _transform_bounds(
        bounds: tuple[float, float, float, float],
        placed: PlacedPart,
    ) -> tuple[float, float, float, float]:
        x_min, y_min, x_max, y_max = bounds
        corners = [
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ]
        points = [
            transform_point(placed.x_mm, placed.y_mm, placed.rot_deg, x, y)
            for x, y in corners
        ]
        return _bounds_union((x, y, x, y) for x, y in points)

    def pad_world_centers(self, placed: PlacedPart) -> dict[str, tuple[float, float]]:
        return {
            pad.number: transform_point(
                placed.x_mm, placed.y_mm, placed.rot_deg, pad.x_mm, pad.y_mm
            )
            for pad in self.pads
        }

    def pad_side_counts(self) -> dict[str, int]:
        """Summarize which side of the footprint pads mostly face."""
        x_min, y_min, x_max, y_max = self.bounds
        x_mid = (x_min + x_max) / 2
        y_mid = (y_min + y_max) / 2
        counts = {"left": 0, "right": 0, "top": 0, "bottom": 0}
        for pad in self.pads:
            dx = pad.x_mm - x_mid
            dy = pad.y_mm - y_mid
            if abs(dx) >= abs(dy):
                counts["right" if dx >= 0 else "left"] += 1
            else:
                counts["bottom" if dy >= 0 else "top"] += 1
        return counts


def transform_point(
    origin_x: float,
    origin_y: float,
    rot_deg: float,
    local_x: float,
    local_y: float,
) -> tuple[float, float]:
    radians = math.radians(rot_deg)
    # Match KiCad PCB footprint rotation. Positive angles rotate local +Y
    # toward board +X, which is the transform pcbnew applies when rendering
    # `(footprint ... (at x y angle))` records.
    return (
        origin_x + local_x * math.cos(radians) + local_y * math.sin(radians),
        origin_y - local_x * math.sin(radians) + local_y * math.cos(radians),
    )


def _find_child(node, key: str):
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def _as_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _layer_name(node) -> str:
    layer = _find_child(node, "layer")
    if layer is not None and len(layer) > 1:
        return str(layer[1]).strip('"')
    return ""


def _xy(node, key: str) -> tuple[float, float] | None:
    child = _find_child(node, key)
    if child is not None and len(child) >= 3:
        return _as_float(child[1]), _as_float(child[2])
    return None


def _bounds_union(bounds_iter) -> tuple[float, float, float, float]:
    x_min = y_min = float("inf")
    x_max = y_max = float("-inf")
    for bx_min, by_min, bx_max, by_max in bounds_iter:
        x_min = min(x_min, bx_min)
        y_min = min(y_min, by_min)
        x_max = max(x_max, bx_max)
        y_max = max(y_max, by_max)
    if x_min == float("inf"):
        return (-1.0, -1.0, 1.0, 1.0)
    return x_min, y_min, x_max, y_max


def _expand_bounds(
    bounds: tuple[float, float, float, float],
    amount: float,
) -> tuple[float, float, float, float]:
    x_min, y_min, x_max, y_max = bounds
    return x_min - amount, y_min - amount, x_max + amount, y_max + amount


def _stroke_half_width(node) -> float:
    stroke = _find_child(node, "stroke")
    width = _find_child(stroke, "width") if stroke is not None else None
    if width is not None and len(width) > 1:
        return max(0.0, _as_float(width[1]) / 2)
    width = _find_child(node, "width")
    if width is not None and len(width) > 1:
        return max(0.0, _as_float(width[1]) / 2)
    return 0.0


def _bounds_from_points(
    points: list[tuple[float, float]],
    stroke_half_width: float = 0.0,
) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    bounds = _bounds_union((x, y, x, y) for x, y in points)
    if stroke_half_width > 0.0:
        return _expand_bounds(bounds, stroke_half_width)
    return bounds


def _pts_xy_points(node) -> list[tuple[float, float]]:
    pts = _find_child(node, "pts")
    if pts is None:
        return []
    points = []
    for child in pts:
        if isinstance(child, list) and len(child) >= 3 and child[0] == "xy":
            points.append((_as_float(child[1]), _as_float(child[2])))
    return points


def _normalize_angle(angle: float) -> float:
    return angle % (math.tau)


def _angle_between_ccw(start: float, angle: float, end: float) -> bool:
    start = _normalize_angle(start)
    angle = _normalize_angle(angle)
    end = _normalize_angle(end)
    return (angle - start) % math.tau <= (end - start) % math.tau + 1e-12


def _circle_center_from_three_points(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float] | None:
    x1, y1 = start
    x2, y2 = mid
    x3, y3 = end
    determinant = 2 * (
        x1 * (y2 - y3)
        + x2 * (y3 - y1)
        + x3 * (y1 - y2)
    )
    if abs(determinant) < 1e-12:
        return None
    x1_sq_y1_sq = x1 * x1 + y1 * y1
    x2_sq_y2_sq = x2 * x2 + y2 * y2
    x3_sq_y3_sq = x3 * x3 + y3 * y3
    center_x = (
        x1_sq_y1_sq * (y2 - y3)
        + x2_sq_y2_sq * (y3 - y1)
        + x3_sq_y3_sq * (y1 - y2)
    ) / determinant
    center_y = (
        x1_sq_y1_sq * (x3 - x2)
        + x2_sq_y2_sq * (x1 - x3)
        + x3_sq_y3_sq * (x2 - x1)
    ) / determinant
    return center_x, center_y


def _arc_bounds(node) -> tuple[float, float, float, float] | None:
    start = _xy(node, "start")
    mid = _xy(node, "mid")
    end = _xy(node, "end")
    if start is None or mid is None or end is None:
        return None

    center = _circle_center_from_three_points(start, mid, end)
    if center is None:
        return _bounds_from_points([start, mid, end], _stroke_half_width(node))

    cx, cy = center
    radius = math.hypot(start[0] - cx, start[1] - cy)
    if radius <= 0.0:
        return _bounds_from_points([start, mid, end], _stroke_half_width(node))

    start_angle = math.atan2(start[1] - cy, start[0] - cx)
    mid_angle = math.atan2(mid[1] - cy, mid[0] - cx)
    end_angle = math.atan2(end[1] - cy, end[0] - cx)
    ccw = _angle_between_ccw(start_angle, mid_angle, end_angle)

    points = [start, mid, end]
    for angle in (0.0, math.pi / 2, math.pi, math.pi * 3 / 2):
        on_arc = (
            _angle_between_ccw(start_angle, angle, end_angle)
            if ccw
            else _angle_between_ccw(end_angle, angle, start_angle)
        )
        if on_arc:
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return _bounds_from_points(points, _stroke_half_width(node))


def _graphic_bounds(
    fp: Sexp,
    layer_suffix: str,
) -> tuple[float, float, float, float] | None:
    bounds = []
    for line in fp.search("fp_line"):
        if not _layer_name(line).endswith(layer_suffix):
            continue
        start = _xy(line, "start")
        end = _xy(line, "end")
        if start and end:
            xs = [start[0], end[0]]
            ys = [start[1], end[1]]
            bounds.append(
                _expand_bounds(
                    (min(xs), min(ys), max(xs), max(ys)),
                    _stroke_half_width(line),
                )
            )
    for rect in fp.search("fp_rect"):
        if not _layer_name(rect).endswith(layer_suffix):
            continue
        start = _xy(rect, "start")
        end = _xy(rect, "end")
        if start and end:
            xs = [start[0], end[0]]
            ys = [start[1], end[1]]
            bounds.append(
                _expand_bounds(
                    (min(xs), min(ys), max(xs), max(ys)),
                    _stroke_half_width(rect),
                )
            )
    for circle in fp.search("fp_circle"):
        if not _layer_name(circle).endswith(layer_suffix):
            continue
        center = _xy(circle, "center")
        end = _xy(circle, "end")
        if center and end:
            radius = math.hypot(end[0] - center[0], end[1] - center[1])
            bounds.append(
                _expand_bounds(
                    (
                        center[0] - radius,
                        center[1] - radius,
                        center[0] + radius,
                        center[1] + radius,
                    ),
                    _stroke_half_width(circle),
                )
            )
    for arc in fp.search("fp_arc"):
        if not _layer_name(arc).endswith(layer_suffix):
            continue
        arc_bounds = _arc_bounds(arc)
        if arc_bounds is not None:
            bounds.append(arc_bounds)
    for poly in fp.search("fp_poly"):
        if not _layer_name(poly).endswith(layer_suffix):
            continue
        poly_bounds = _bounds_from_points(_pts_xy_points(poly), _stroke_half_width(poly))
        if poly_bounds is not None:
            bounds.append(poly_bounds)
    if not bounds:
        return None
    return _bounds_union(bounds)


def footprint_geometry_from_sexp(footprint: str, fp: Sexp) -> FootprintGeometry:
    pads: list[PadGeometry] = []
    for pad in fp.search("pad"):
        if len(pad) < 4:
            continue
        at = _find_child(pad, "at")
        size = _find_child(pad, "size")
        if at is None or size is None or len(size) < 3:
            continue
        layers = _find_child(pad, "layers")
        net = _find_child(pad, "net")
        pads.append(
            PadGeometry(
                number=str(pad[1]).strip('"'),
                pad_type=str(pad[2]).strip('"'),
                x_mm=_as_float(at[1]) if len(at) > 1 else 0.0,
                y_mm=_as_float(at[2]) if len(at) > 2 else 0.0,
                rot_deg=_as_float(at[3]) if len(at) > 3 else 0.0,
                width_mm=_as_float(size[1], 1.0),
                height_mm=_as_float(size[2], 1.0),
                shape=str(pad[3]).strip('"'),
                layers=tuple(str(layer).strip('"') for layer in (layers or [])[1:]),
                net_name=(
                    str(net[2]).strip('"')
                    if net is not None and len(net) > 2
                    else None
                ),
            )
        )

    courtyard = _graphic_bounds(fp, ".CrtYd")
    body = _graphic_bounds(fp, ".Fab") or _graphic_bounds(fp, ".SilkS")
    if body is None and pads:
        # When the footprint omits Fab/Silk body graphics, fall back to the pad
        # envelope instead of the courtyard halo.  Edge connector placement uses
        # this shape as the local body proxy, so the mating face stays anchored
        # to the actual footprint instead of drifting to the courtyard extents.
        body = _bounds_union(pad.local_bounds for pad in pads)
    return FootprintGeometry(
        footprint=footprint,
        pads=pads,
        body_bounds=body,
        courtyard_bounds=courtyard,
    )


def load_footprint_geometry(
    fp_name: str,
    fp_lib_dirs: list[str],
) -> FootprintGeometry:
    return footprint_geometry_from_sexp(fp_name, load_footprint(fp_name, fp_lib_dirs))


def load_footprint_geometries(
    fp_names: set[str],
    fp_lib_dirs: list[str],
) -> dict[str, FootprintGeometry]:
    geometries: dict[str, FootprintGeometry] = {}
    for fp_name in fp_names:
        try:
            geometries[fp_name] = load_footprint_geometry(fp_name, fp_lib_dirs)
        except FileNotFoundError:
            pass
    return geometries


def geometry_bboxes(
    geometries: dict[str, FootprintGeometry],
) -> dict[str, tuple[float, float]]:
    return {
        fp_name: (geometry.width_mm, geometry.height_mm)
        for fp_name, geometry in geometries.items()
    }
