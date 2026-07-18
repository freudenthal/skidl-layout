from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

from .geometry import FootprintGeometry
from .roles import is_nc_net
from .writer import PlacedPart


_MACOS_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


@dataclass
class ValidationResult:
    overlaps: list[tuple[str, str]] = field(default_factory=list)
    outline_violations: list[str] = field(default_factory=list)
    keepout_violations: list[str] = field(default_factory=list)
    cutout_violations: list[str] = field(default_factory=list)
    worst_hpwl_nets: list[tuple[str, float]] = field(default_factory=list)
    worst_hpwl_refs: dict[str, list[str]] = field(default_factory=dict)
    missing_refs: list[str] = field(default_factory=list)
    extra_refs: list[str] = field(default_factory=list)
    total_parts: int = 0
    placed_parts: int = 0

    @property
    def ok(self) -> bool:
        return (
            not self.overlaps
            and not self.missing_refs
            and not self.outline_violations
            and not self.keepout_violations
            and not self.cutout_violations
        )

    def summary(self) -> str:
        lines = []
        lines.append(f"Parts: {self.placed_parts}/{self.total_parts} placed")
        if self.missing_refs:
            lines.append(f"MISSING: {', '.join(self.missing_refs[:20])}")
        if self.overlaps:
            lines.append(f"OVERLAPS ({len(self.overlaps)}):")
            for a, b in self.overlaps[:20]:
                lines.append(f"  {a} ↔ {b}")
        else:
            lines.append("No overlaps")
        if self.outline_violations:
            lines.append(f"OUTSIDE OUTLINE ({len(self.outline_violations)}):")
            for ref in self.outline_violations[:20]:
                lines.append(f"  {ref}")
        if self.keepout_violations:
            lines.append(f"INSIDE KEEPOUT ({len(self.keepout_violations)}):")
            for ref in self.keepout_violations[:20]:
                lines.append(f"  {ref}")
        if self.cutout_violations:
            lines.append(f"INTERSECTS CUTOUT ({len(self.cutout_violations)}):")
            for ref in self.cutout_violations[:20]:
                lines.append(f"  {ref}")
        if self.worst_hpwl_nets:
            lines.append("Worst HPWL nets:")
            for name, hpwl in self.worst_hpwl_nets[:10]:
                lines.append(f"  {name}: {hpwl:.1f}mm")
        return "\n".join(lines)


def _fallback_bounds(
    pp: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
) -> tuple[float, float, float, float]:
    w, h = fp_bboxes.get(pp.footprint, (2.0, 2.0))
    if pp.rot_deg % 180 == 90:
        w, h = h, w
    return pp.x_mm - w / 2, pp.y_mm - h / 2, pp.x_mm + w / 2, pp.y_mm + h / 2


def _placed_bounds(
    pp: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    *,
    physical: bool = False,
) -> tuple[float, float, float, float]:
    geometry = (fp_geometries or {}).get(pp.footprint)
    if geometry is not None:
        if physical:
            return geometry.transformed_physical_bounds(pp)
        return geometry.transformed_bounds(pp)
    return _fallback_bounds(pp, fp_bboxes)


def _rects_overlap(a, b, clearance_mm: float = 0.0) -> bool:
    ax_min, ay_min, ax_max, ay_max = a
    bx_min, by_min, bx_max, by_max = b
    return (
        ax_min < bx_max + clearance_mm
        and ax_max > bx_min - clearance_mm
        and ay_min < by_max + clearance_mm
        and ay_max > by_min - clearance_mm
    )


def _assembly_side(pp: PlacedPart) -> str:
    side = str(getattr(pp, "side", "front") or "front").lower()
    if side not in {"front", "back", "mechanical"}:
        return "front"
    return side


def _same_physical_side(a: PlacedPart, b: PlacedPart) -> bool:
    a_side = _assembly_side(a)
    b_side = _assembly_side(b)
    if {a_side, b_side} == {"front", "back"}:
        return False
    return True


def _pad_collision_pairs(
    placed: list[PlacedPart],
    clearance_mm: float,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[tuple[str, str]]:
    if not fp_geometries:
        return []

    collisions: list[tuple[str, str]] = []
    for i, a in enumerate(placed):
        a_geometry = fp_geometries.get(a.footprint)
        if a_geometry is None or not a_geometry.pads:
            continue
        for b in placed[i + 1:]:
            if _same_physical_side(a, b):
                continue
            b_geometry = fp_geometries.get(b.footprint)
            if b_geometry is None or not b_geometry.pads:
                continue
            if _through_board_pads_collide(
                a, a_geometry, b, b_geometry, clearance_mm
            ):
                collisions.append((a.ref, b.ref))
    return collisions


def _through_board_pads_collide(
    a: PlacedPart,
    a_geometry: FootprintGeometry,
    b: PlacedPart,
    b_geometry: FootprintGeometry,
    clearance_mm: float,
) -> bool:
    for a_pad in a_geometry.pads:
        a_bounds = None
        for b_pad in b_geometry.pads:
            if not (a_pad.is_through_board or b_pad.is_through_board):
                continue
            if a_bounds is None:
                a_bounds = a_pad.transformed_bounds(a)
            if _rects_overlap(
                a_bounds, b_pad.transformed_bounds(b), clearance_mm
            ):
                return True
    return False


def _dedupe_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a, b in pairs:
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((a, b))
    return deduped


def _check_overlaps(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    clearance_mm: float,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[tuple[str, str]]:
    if len(placed) >= 20:
        from .spatial import SpatialGrid

        grid = SpatialGrid(cell_size_mm=10.0)
        bounds_by_ref: dict[str, tuple[float, float, float, float]] = {}
        for pp in placed:
            b = _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True)
            bounds_by_ref[pp.ref] = b
            cx = (b[0] + b[2]) / 2
            cy = (b[1] + b[3]) / 2
            w = b[2] - b[0]
            h = b[3] - b[1]
            grid.insert(pp.ref, cx, cy, w, h)
        placed_by_ref = {pp.ref: pp for pp in placed}
        body_overlaps = [
            (a_ref, b_ref)
            for a_ref, b_ref in grid.all_overlapping_pairs(clearance=clearance_mm)
            if _same_physical_side(placed_by_ref[a_ref], placed_by_ref[b_ref])
        ]
        return _dedupe_pairs(
            body_overlaps + _pad_collision_pairs(placed, clearance_mm, fp_geometries)
        )

    overlaps: list[tuple[str, str]] = []
    for i, a in enumerate(placed):
        a_bounds = _placed_bounds(a, fp_bboxes, fp_geometries, physical=True)
        for b in placed[i + 1:]:
            if not _same_physical_side(a, b):
                continue
            b_bounds = _placed_bounds(b, fp_bboxes, fp_geometries, physical=True)
            if _rects_overlap(a_bounds, b_bounds, clearance_mm):
                overlaps.append((a.ref, b.ref))
    return _dedupe_pairs(
        overlaps + _pad_collision_pairs(placed, clearance_mm, fp_geometries)
    )


def _point_in_polygon(x: float, y: float, vertices: list[tuple[float, float]]) -> bool:
    inside = False
    count = len(vertices)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = vertices[i]
        xj, yj = vertices[j]
        intersects = ((yi > y) != (yj > y)) and (
            x <= (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _outline_contains_bounds(bounds, outline) -> bool:
    x_min, y_min, x_max, y_max = bounds
    if (
        x_min < outline.x_min
        or y_min < outline.y_min
        or x_max > outline.x_max
        or y_max > outline.y_max
    ):
        return False
    vertices = getattr(outline, "vertices", []) or []
    if len(vertices) <= 4:
        return True
    shapely_result = _shapely_outline_contains_bounds(bounds, vertices)
    if shapely_result is not None:
        return shapely_result
    corners = [
        (x_min, y_min),
        (x_max, y_min),
        (x_max, y_max),
        (x_min, y_max),
    ]
    return all(_point_in_polygon(x, y, vertices) for x, y in corners)


def _shapely_outline_contains_bounds(
    bounds,
    vertices: list[tuple[float, float]],
) -> bool | None:
    try:
        from shapely.geometry import Polygon, box
    except Exception:
        return None
    try:
        polygon = Polygon(vertices)
        if polygon.is_empty or not polygon.is_valid:
            return None
        return bool(polygon.covers(box(*bounds)))
    except Exception:
        return None


def _check_outline_violations(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[str]:
    if outline is None:
        return []

    violations = []
    for pp in placed:
        if not _outline_contains_bounds(
            _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True), outline
        ):
            violations.append(pp.ref)
    return violations


def _check_keepout_violations(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    keepouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[str]:
    if not keepouts:
        return []
    violations = []
    keepout_bounds = [
        (
            keepout.x_min,
            keepout.y_min,
            keepout.x_max,
            keepout.y_max,
            set(getattr(keepout, "allowed_refs", []) or []),
        )
        for keepout in keepouts
    ]
    for pp in placed:
        bounds = _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True)
        if any(
            pp.ref not in allowed_refs
            and _rects_overlap(bounds, (x_min, y_min, x_max, y_max))
            for x_min, y_min, x_max, y_max, allowed_refs in keepout_bounds
        ):
            violations.append(pp.ref)
    return violations


def _check_cutout_violations(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[str]:
    if not cutouts:
        return []
    violations = []
    cutout_bounds = [
        getattr(cutout, "bounds", None)
        or (
            getattr(cutout, "x_min"),
            getattr(cutout, "y_min"),
            getattr(cutout, "x_max"),
            getattr(cutout, "y_max"),
        )
        for cutout in cutouts
    ]
    for pp in placed:
        bounds = _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True)
        if any(_rects_overlap(bounds, cutout) for cutout in cutout_bounds):
            violations.append(pp.ref)
    return violations


def _compute_hpwl(
    placed: list[PlacedPart],
    circuit,
) -> list[tuple[str, float, list[str]]]:
    pos_by_ref = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed}
    net_hpwl: list[tuple[str, float]] = []

    for net in circuit.get_nets():
        if is_nc_net(net):
            continue
        xs, ys = [], []
        refs = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref and ref in pos_by_ref:
                x, y = pos_by_ref[ref]
                xs.append(x)
                ys.append(y)
                if ref not in refs:
                    refs.append(ref)
        if len(xs) < 2:
            continue
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        net_hpwl.append((net.name, hpwl, refs))

    net_hpwl.sort(key=lambda t: t[1], reverse=True)
    return net_hpwl[:10]


def validate(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    clearance_mm: float = 0.5,
    outline=None,
    keepouts=None,
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> ValidationResult:
    result = ValidationResult(placed_parts=len(placed_parts))

    result.overlaps = _check_overlaps(
        placed_parts, fp_bboxes, clearance_mm, fp_geometries
    )
    result.outline_violations = _check_outline_violations(
        placed_parts, fp_bboxes, outline, fp_geometries
    )
    result.keepout_violations = _check_keepout_violations(
        placed_parts, fp_bboxes, keepouts, fp_geometries
    )
    result.cutout_violations = _check_cutout_violations(
        placed_parts, fp_bboxes, cutouts, fp_geometries
    )

    if circuit is not None:
        result.total_parts = len(circuit.parts)
        circuit_refs = {getattr(p, "ref", None) for p in circuit.parts}
        placed_refs = {pp.ref for pp in placed_parts}
        result.missing_refs = sorted(circuit_refs - placed_refs - {None})
        result.extra_refs = sorted(placed_refs - circuit_refs)
        worst_hpwl = _compute_hpwl(placed_parts, circuit)
        result.worst_hpwl_nets = [(name, hpwl) for name, hpwl, _ in worst_hpwl]
        result.worst_hpwl_refs = {name: refs for name, _, refs in worst_hpwl}

    return result


def find_kicad_cli() -> str | None:
    return shutil.which("kicad-cli") or (
        _MACOS_KICAD_CLI if os.path.isfile(_MACOS_KICAD_CLI) else None
    )


def run_kicad_drc(pcb_path: str) -> tuple[bool, str]:
    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return True, "kicad-cli not available"

    try:
        result = subprocess.run(
            [kicad_cli, "pcb", "drc", "--output", pcb_path + ".drc.json", pcb_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        report = result.stdout + result.stderr
        passed = result.returncode == 0
        return passed, report
    except FileNotFoundError:
        return True, "kicad-cli not available"
    except subprocess.TimeoutExpired:
        return False, "DRC timed out after 60s"
