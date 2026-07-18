from __future__ import annotations

import math
from typing import Optional

from .constraints import (
    AnchorZone,
    BoardOutline,
    EdgeAnchor,
    FORM_FACTORS,
    FixedPosition,
    KeepOut,
    LayoutConstraints,
)
from .hierarchy import PlacementGroup
from .roles import DECAP_VALUE_RE, GND_NET_RE, POWER_NET_RE, is_nc_net, is_ui_grid_part
from .spatial import SpatialGrid
from .writer import PlacedPart

_DEFAULT_BBOX = (2.0, 2.0)

_PASSIVE_BBOX = (1.7, 0.9)


def _is_mock_placeholder(value) -> bool:
    return value.__class__.__module__.startswith("unittest.mock")


def _footprint_attr(part, name: str) -> str:
    fp = getattr(part, name, None)
    if fp is None or _is_mock_placeholder(fp):
        return ""
    return str(fp or "")


def _footprint_name(part) -> str:
    return _footprint_attr(part, "footprint") or _footprint_attr(part, "foot")


def _pin_net_names(part) -> list[str]:
    names = []
    for pin in part.pins:
        net = getattr(pin, 'net', None)
        if net is not None and not is_nc_net(net):
            name = getattr(net, 'name', None)
            if name:
                names.append(name)
    return names


def _is_decoupling_cap(part) -> bool:
    if len(part) != 2:
        return False
    val = (getattr(part, 'value', '') or '').strip()
    if not DECAP_VALUE_RE.match(val):
        return False
    nets = _pin_net_names(part)
    return any(POWER_NET_RE.match(n) for n in nets) and any(
        GND_NET_RE.match(n) for n in nets
    )


def _bbox(part, fp_bboxes: dict) -> tuple[float, float]:
    fp = _footprint_name(part)
    if fp in fp_bboxes:
        return fp_bboxes[fp]
    if len(part) == 2:
        return _PASSIVE_BBOX
    return _DEFAULT_BBOX


def _overlaps(x1, y1, w1, h1, x2, y2, w2, h2, clearance=0.5) -> bool:
    return (abs(x1 - x2) < (w1 + w2) / 2 + clearance and
            abs(y1 - y2) < (h1 + h2) / 2 + clearance)


def _overlaps_any(x, y, w, h, occupied, clearance=0.5) -> bool:
    if isinstance(occupied, SpatialGrid):
        return occupied.check_any_overlap(x, y, w, h, clearance)
    for ox, oy, ow, oh in occupied:
        if _overlaps(x, y, w, h, ox, oy, ow, oh, clearance):
            return True
    return False


def _occupied_from_keepouts(keepouts: list[KeepOut] | None) -> list[tuple]:
    occupied = []
    for keepout in keepouts or []:
        occupied.append(
            (
                (keepout.x_min + keepout.x_max) / 2,
                (keepout.y_min + keepout.y_max) / 2,
                keepout.x_max - keepout.x_min,
                keepout.y_max - keepout.y_min,
            )
        )
    return occupied


def _fits_bounds(x, y, w, h, bounds) -> bool:
    if bounds is None:
        return True
    half_w, half_h = w / 2, h / 2
    return (
        x - half_w >= bounds.x_min
        and y - half_h >= bounds.y_min
        and x + half_w <= bounds.x_max
        and y + half_h <= bounds.y_max
    )


def _find_clear_position(
    target_x: float,
    target_y: float,
    width: float,
    height: float,
    occupied,
    bounds=None,
    step: float = 1.0,
    max_radius: float = 120.0,
) -> tuple[float, float]:
    if _fits_bounds(target_x, target_y, width, height, bounds) and not _overlaps_any(
        target_x, target_y, width, height, occupied
    ):
        return target_x, target_y
    steps = max(1, int(max_radius / step))
    for i in range(1, steps):
        radius = step * i
        angle_count = max(4, int(radius * 2 * math.pi))
        for j in range(angle_count):
            angle = j * (2 * math.pi / angle_count)
            x = target_x + radius * math.cos(angle)
            y = target_y + radius * math.sin(angle)
            if _fits_bounds(x, y, width, height, bounds) and not _overlaps_any(
                x, y, width, height, occupied
            ):
                return x, y
    # Bounds may be the limiting factor — retry ignoring bounds.
    if bounds is not None:
        for i in range(1, max(1, int(max_radius / step))):
            radius = step * i
            angle_count = max(4, int(radius * 2 * math.pi))
            for j in range(angle_count):
                angle = j * (2 * math.pi / angle_count)
                x = target_x + radius * math.cos(angle)
                y = target_y + radius * math.sin(angle)
                if not _overlaps_any(x, y, width, height, occupied):
                    return x, y
    return target_x, target_y


def _find_clear_edge_position(
    anchor: EdgeAnchor,
    target_x: float,
    target_y: float,
    width: float,
    height: float,
    occupied,
    bounds=None,
    step: float = 1.0,
    max_radius: float = 120.0,
) -> tuple[float, float]:
    """Find a clear position for an edge anchor without moving it off-edge."""
    edge = anchor.edge.lower()
    if edge not in {"top", "bottom", "left", "right"}:
        return _find_clear_position(
            target_x, target_y, width, height, occupied,
            bounds=bounds, step=step, max_radius=max_radius,
        )

    if _fits_bounds(target_x, target_y, width, height, bounds) and not _overlaps_any(
        target_x, target_y, width, height, occupied
    ):
        return target_x, target_y

    half_w, half_h = width / 2, height / 2
    if edge in {"top", "bottom"}:
        low = bounds.x_min + half_w if bounds is not None else target_x - max_radius
        high = bounds.x_max - half_w if bounds is not None else target_x + max_radius
        fixed = target_y
        axis_target = max(low, min(high, target_x))
    else:
        low = bounds.y_min + half_h if bounds is not None else target_y - max_radius
        high = bounds.y_max - half_h if bounds is not None else target_y + max_radius
        fixed = target_x
        axis_target = max(low, min(high, target_y))

    steps = max(1, int(max_radius / step))
    candidates = [axis_target]
    for i in range(1, steps + 1):
        delta = i * step
        candidates.extend((axis_target - delta, axis_target + delta))

    for value in candidates:
        if value < low or value > high:
            continue
        if edge in {"top", "bottom"}:
            x, y = value, fixed
        else:
            x, y = fixed, value
        if _fits_bounds(x, y, width, height, bounds) and not _overlaps_any(
            x, y, width, height, occupied
        ):
            return x, y

    # Staying on the requested edge is more important than silently moving a
    # mating connector inward.  Let validation report any residual overlap.
    return target_x, target_y


def _find_near_parent(
    parent_x: float,
    parent_y: float,
    pw: float,
    ph: float,
    width: float,
    height: float,
    n: int,
    occupied,
    bounds=None,
) -> tuple[float, float]:
    """Try right/below/left/above offsets from parent, return closest clear position."""
    # Use rotation-safe (square) dimensions so orientation refinement can't
    # create overlaps by rotating a part after placement.
    side = max(width, height)
    gap = 1.0
    offsets = [
        (pw / 2 + side / 2 + gap + n * (side + 0.5), 0),
        (0, ph / 2 + side / 2 + gap + n * (side + 0.5)),
        (-(pw / 2 + side / 2 + gap + n * (side + 0.5)), 0),
        (0, -(ph / 2 + side / 2 + gap + n * (side + 0.5))),
    ]

    best, best_dist = None, float("inf")
    for dx, dy in offsets:
        tx = parent_x + dx
        ty = parent_y + dy
        if bounds is not None:
            tx, ty = _clamp_to_bounds(tx, ty, side, side, bounds)
        x, y = _find_clear_position(
            tx, ty, side, side, occupied, bounds=bounds,
            step=0.5, max_radius=25.0,
        )
        if bounds is not None:
            x, y = _clamp_to_bounds(x, y, side, side, bounds)
        dist = math.hypot(x - parent_x, y - parent_y)
        if dist < best_dist:
            best = (x, y)
            best_dist = dist

    # If best position still overlaps, widen search from parent center.
    if best is not None and _overlaps_any(best[0], best[1], side, side, occupied):
        x, y = _find_clear_position(
            parent_x, parent_y, side, side, occupied,
            bounds=bounds, step=1.0, max_radius=80.0,
        )
        if not _overlaps_any(x, y, side, side, occupied):
            best = (x, y)

    return best


def _clamp_to_bounds(x, y, w, h, bounds) -> tuple[float, float]:
    if bounds is None:
        return x, y
    half_w, half_h = w / 2, h / 2
    x = max(bounds.x_min + half_w, min(bounds.x_max - half_w, x))
    y = max(bounds.y_min + half_h, min(bounds.y_max - half_h, y))
    return x, y


def _clamp_to_outline(x, y, w, h, outline) -> tuple[float, float]:
    return _clamp_to_bounds(x, y, w, h, outline)


def _group_matches(zone_name: str, group_name: str) -> bool:
    if not zone_name:
        return False
    if zone_name == "*":
        return True
    group_name = group_name or ""
    return (
        group_name == zone_name
        or group_name.endswith(zone_name)
        or zone_name in group_name
    )


def _zone_for_part(part, group: PlacementGroup, constraints: LayoutConstraints):
    for zone in constraints.zones or []:
        if getattr(part, "ref", None) in (zone.refs or []):
            return zone
    for zone in constraints.zones or []:
        if _group_matches(zone.group_name, group.name):
            return zone
    return None


def _bounds_for_part(part, group: PlacementGroup, constraints: LayoutConstraints):
    return _zone_for_part(part, group, constraints) or constraints.outline


def _bounds_center(bounds) -> tuple[float, float]:
    return (bounds.x_min + bounds.x_max) / 2, (bounds.y_min + bounds.y_max) / 2


def _clamp_axis(value: float, low: float, high: float) -> float:
    if low > high:
        return (low + high) / 2
    return max(low, min(high, value))


def _edge_anchor_position(
    anchor: EdgeAnchor,
    width: float,
    height: float,
    outline: BoardOutline,
) -> tuple[float, float, float]:
    if outline is None:
        return 10.0, 10.0, anchor.rot_deg or 0.0
    edge = anchor.edge.lower()
    x_mid, y_mid = _bounds_center(outline)

    # Auto-rotate so the part's long axis runs coplanar to the edge.
    # A tall part (h > w) on a horizontal edge needs 90° rotation, and
    # a wide part (w > h) on a vertical edge likewise.
    if anchor.rot_deg is not None:
        rot = anchor.rot_deg
        ew, eh = (height, width) if rot % 180 == 90 else (width, height)
    else:
        horizontal_edge = edge in {"top", "bottom"}
        tall = height > width * 1.3
        wide = width > height * 1.3
        if (horizontal_edge and tall) or (not horizontal_edge and wide):
            rot = 90.0
            ew, eh = height, width
        else:
            rot = 0.0
            ew, eh = width, height

    if edge in {"top", "bottom"}:
        x = anchor.offset_mm if anchor.offset_mm is not None else x_mid
        x = _clamp_axis(x, outline.x_min + ew / 2, outline.x_max - ew / 2)
        y = (
            outline.y_min + eh / 2 + anchor.inset_mm
            if edge == "top"
            else outline.y_max - eh / 2 - anchor.inset_mm
        )
    elif edge in {"left", "right"}:
        x = (
            outline.x_min + ew / 2 + anchor.inset_mm
            if edge == "left"
            else outline.x_max - ew / 2 - anchor.inset_mm
        )
        y = anchor.offset_mm if anchor.offset_mm is not None else y_mid
        y = _clamp_axis(y, outline.y_min + eh / 2, outline.y_max - eh / 2)
    else:
        raise ValueError(f"Unknown edge anchor '{anchor.edge}' for {anchor.ref}")
    return x, y, rot


def _rotated_local_bounds(geometry, ref: str, footprint: str, rot_deg: float):
    if geometry is None:
        return None
    return geometry.transformed_bounds(
        PlacedPart(ref=ref, x_mm=0.0, y_mm=0.0, rot_deg=rot_deg, footprint=footprint)
    )


def _edge_anchor_origin_position(
    anchor: EdgeAnchor,
    width: float,
    height: float,
    outline: BoardOutline,
    *,
    geometry=None,
    ref: str = "",
    footprint: str = "",
) -> tuple[float, float, float, float, float, float, float]:
    """Return origin, rotation, occupied-center and dimensions for an edge anchor.

    KiCad footprint coordinates are module origins, not bounding-box centers.
    For many THT connectors the origin is at pad 1 or another mechanical
    reference, so placing the origin at the edge-safe bbox center still leaves
    the true courtyard outside the board.  When geometry is available, place
    the transformed footprint bounds against the edge and use their center for
    collision checks.
    """
    target_x, target_y, rot = _edge_anchor_position(anchor, width, height, outline)
    ew, eh = (height, width) if rot % 180 == 90 else (width, height)
    if outline is None or geometry is None:
        return target_x, target_y, rot, target_x, target_y, ew, eh

    bounds = _rotated_local_bounds(geometry, ref, footprint, rot)
    if bounds is None:
        return target_x, target_y, rot, target_x, target_y, ew, eh
    bx_min, by_min, bx_max, by_max = bounds
    center_dx = (bx_min + bx_max) / 2
    center_dy = (by_min + by_max) / 2
    ew = bx_max - bx_min
    eh = by_max - by_min
    edge = anchor.edge.lower()
    x_mid, y_mid = _bounds_center(outline)

    if edge in {"top", "bottom"}:
        center_x = anchor.offset_mm if anchor.offset_mm is not None else x_mid
        center_x = _clamp_axis(
            center_x,
            outline.x_min + ew / 2,
            outline.x_max - ew / 2,
        )
        origin_x = center_x - center_dx
        if edge == "top":
            origin_y = outline.y_min + anchor.inset_mm - by_min
        else:
            origin_y = outline.y_max - anchor.inset_mm - by_max
        center_y = origin_y + center_dy
    elif edge in {"left", "right"}:
        center_y = anchor.offset_mm if anchor.offset_mm is not None else y_mid
        center_y = _clamp_axis(
            center_y,
            outline.y_min + eh / 2,
            outline.y_max - eh / 2,
        )
        origin_y = center_y - center_dy
        if edge == "left":
            origin_x = outline.x_min + anchor.inset_mm - bx_min
        else:
            origin_x = outline.x_max - anchor.inset_mm - bx_max
        center_x = origin_x + center_dx
    else:
        raise ValueError(f"Unknown edge anchor '{anchor.edge}' for {anchor.ref}")

    return origin_x, origin_y, rot, center_x, center_y, ew, eh


def _is_primary_part(part) -> bool:
    return len(part) != 2


def _face_edge_rotation(ref: str, constraints: LayoutConstraints, default: float) -> float:
    for face_edge in constraints.face_edges or []:
        if face_edge.ref == ref and face_edge.rot_deg is not None:
            return face_edge.rot_deg
    return default


def _most_adjacent_placed(
    ref: str,
    adjacency: dict,
    placed_map: dict,
    *,
    candidate_refs: set[str] | None = None,
    usage_counts: dict[str, int] | None = None,
) -> Optional[str]:
    """Return the ref of the already-placed part sharing the most nets with `ref`."""
    neighbors = adjacency.get(ref, {})
    best_ref, best_count, best_usage = None, 0, float("inf")
    for other_ref, count in neighbors.items():
        if other_ref not in placed_map:
            continue
        if candidate_refs is not None and other_ref not in candidate_refs:
            continue
        usage = (usage_counts or {}).get(other_ref, 0)
        if count > best_count or (count == best_count and usage < best_usage):
            best_count = count
            best_ref = other_ref
            best_usage = usage
    return best_ref


def _multi_primary_centroid_target(
    ref: str,
    adjacency: dict,
    placed_map: dict[str, PlacedPart],
    candidate_refs: set[str],
    bounds,
) -> tuple[float, float] | None:
    """Return a useful centroid for passives spanning multiple primary refs."""

    neighbors: list[tuple[str, int]] = []
    for other_ref, count in (adjacency.get(ref, {}) or {}).items():
        if other_ref not in placed_map or other_ref not in candidate_refs:
            continue
        neighbors.append((other_ref, max(1, int(count or 1))))
    if len(neighbors) < 2:
        return None

    xs = [placed_map[other_ref].x_mm for other_ref, _ in neighbors]
    ys = [placed_map[other_ref].y_mm for other_ref, _ in neighbors]
    x_span = max(xs) - min(xs)
    y_span = max(ys) - min(ys)
    if bounds is not None:
        bounds_w = float(getattr(bounds, "x_max", 0.0) - getattr(bounds, "x_min", 0.0))
        bounds_h = float(getattr(bounds, "y_max", 0.0) - getattr(bounds, "y_min", 0.0))
        min_span = max(10.0, min(bounds_w, bounds_h) * 0.35)
    else:
        min_span = 12.0
    if max(x_span, y_span) < min_span:
        return None

    total = sum(weight for _, weight in neighbors)
    x = sum(placed_map[other_ref].x_mm * weight for other_ref, weight in neighbors) / total
    y = sum(placed_map[other_ref].y_mm * weight for other_ref, weight in neighbors) / total
    return x, y


def _largest_ic_ref(group: PlacementGroup) -> Optional[str]:
    """Return ref of the part with the most pins (tie: first encountered)."""
    best_ref, best_pins = None, -1
    for part in group.parts:
        n = len(part)
        if n > best_pins:
            best_pins = n
            best_ref = part.ref
    return best_ref


def _distance_xy(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _apply_soft_constraints(
    placed_map: dict[str, PlacedPart],
    all_parts: list[tuple],
    constraints: LayoutConstraints,
    fp_bboxes: dict[str, tuple[float, float]],
) -> None:
    """Apply alignment/proximity constraints after first-pass placement."""
    if not placed_map:
        return

    part_by_ref = {part.ref: part for part, _ in all_parts}
    group_by_ref = {part.ref: group for part, group in all_parts}
    fixed_refs = {fp.ref for fp in (constraints.fixed or [])}
    fixed_refs.update(anchor.ref for anchor in (constraints.edge_anchors or []))
    axis_locks: dict[str, dict[str, float]] = {}

    def _lock_axis(ref: str, axis: str, value: float) -> None:
        part = part_by_ref.get(ref)
        if part is None or not is_ui_grid_part(part):
            return
        axis_locks.setdefault(ref, {})[axis] = value

    def _occupied_without(ref: str) -> list[tuple]:
        occupied = _occupied_from_keepouts(constraints.keepouts)
        for other_ref, pp in placed_map.items():
            if other_ref == ref:
                continue
            part = part_by_ref.get(other_ref)
            if part is None:
                w, h = fp_bboxes.get(pp.footprint, _DEFAULT_BBOX)
            else:
                w, h = _bbox(part, fp_bboxes)
            occupied.append((pp.x_mm, pp.y_mm, w, h))
        return occupied

    def _apply_axis_locks(ref: str, x: float, y: float) -> tuple[float, float]:
        locks = axis_locks.get(ref, {})
        if "x" in locks:
            x = locks["x"]
        if "y" in locks:
            y = locks["y"]
        return x, y

    def _find_clear_position_with_locks(
        ref: str,
        target_x: float,
        target_y: float,
        width: float,
        height: float,
        occupied,
        bounds=None,
        step: float = 1.0,
        max_radius: float = 120.0,
    ) -> tuple[float, float]:
        locks = axis_locks.get(ref, {})
        target_x, target_y = _apply_axis_locks(ref, target_x, target_y)

        if not locks:
            return _find_clear_position(
                target_x,
                target_y,
                width,
                height,
                occupied,
                bounds=bounds,
                step=step,
                max_radius=max_radius,
            )

        if _fits_bounds(target_x, target_y, width, height, bounds) and not _overlaps_any(
            target_x, target_y, width, height, occupied
        ):
            return target_x, target_y

        # Grid alignment is a mechanical/UI promise: keep locked axes fixed and
        # search only along the free axis. If both axes are locked, validation
        # should report the real conflict instead of silently degrading the grid.
        if "x" in locks and "y" in locks:
            return target_x, target_y

        half_w, half_h = width / 2, height / 2
        if "x" in locks:
            low = bounds.y_min + half_h if bounds is not None else target_y - max_radius
            high = bounds.y_max - half_h if bounds is not None else target_y + max_radius
            axis_target = max(low, min(high, target_y))
        else:
            low = bounds.x_min + half_w if bounds is not None else target_x - max_radius
            high = bounds.x_max - half_w if bounds is not None else target_x + max_radius
            axis_target = max(low, min(high, target_x))

        steps = max(1, int(max_radius / step))
        candidates = [axis_target]
        for i in range(1, steps + 1):
            delta = i * step
            candidates.extend((axis_target - delta, axis_target + delta))

        for value in candidates:
            if value < low or value > high:
                continue
            if "x" in locks:
                x, y = target_x, value
            else:
                x, y = value, target_y
            if _fits_bounds(x, y, width, height, bounds) and not _overlaps_any(
                x, y, width, height, occupied
            ):
                return x, y
        return target_x, target_y

    def _move(ref: str, target_x: float, target_y: float) -> None:
        if ref in fixed_refs or ref not in placed_map or ref not in part_by_ref:
            return
        part = part_by_ref[ref]
        group = group_by_ref[ref]
        w, h = _bbox(part, fp_bboxes)
        bounds = _bounds_for_part(part, group, constraints)
        target_x, target_y = _apply_axis_locks(ref, target_x, target_y)
        target_x, target_y = _clamp_to_bounds(target_x, target_y, w, h, bounds)
        target_x, target_y = _apply_axis_locks(ref, target_x, target_y)
        x, y = _find_clear_position_with_locks(
            ref,
            target_x,
            target_y,
            w,
            h,
            _occupied_without(ref),
            bounds=bounds,
        )
        x, y = _clamp_to_bounds(x, y, w, h, bounds)
        x, y = _apply_axis_locks(ref, x, y)
        pp = placed_map[ref]
        placed_map[ref] = PlacedPart(
            ref=pp.ref,
            x_mm=x,
            y_mm=y,
            rot_deg=pp.rot_deg,
            footprint=pp.footprint,
        )

    def _apply_align_constraints() -> None:
        for constraint in constraints.align or []:
            refs = [ref for ref in constraint.refs if ref in placed_map]
            if not refs:
                continue
            axis = constraint.axis.lower()
            if axis not in {"x", "y"}:
                continue
            value = constraint.value_mm
            if value is None:
                first = placed_map[refs[0]]
                value = first.x_mm if axis == "x" else first.y_mm
            for ref in refs:
                _lock_axis(ref, axis, value)
                pp = placed_map[ref]
                _move(
                    ref,
                    value if axis == "x" else pp.x_mm,
                    value if axis == "y" else pp.y_mm,
                )

    _apply_align_constraints()

    for constraint in constraints.distribute or []:
        refs = [ref for ref in constraint.refs if ref in placed_map]
        if len(refs) < 2:
            continue
        axis = constraint.axis.lower()
        if axis not in {"x", "y"}:
            continue
        current = [
            placed_map[ref].x_mm if axis == "x" else placed_map[ref].y_mm
            for ref in refs
        ]
        start = constraint.start_mm if constraint.start_mm is not None else current[0]
        end = constraint.end_mm if constraint.end_mm is not None else current[-1]
        step = (end - start) / (len(refs) - 1)
        for idx, ref in enumerate(refs):
            pp = placed_map[ref]
            value = start + step * idx
            _lock_axis(ref, axis, value)
            _move(
                ref,
                value if axis == "x" else pp.x_mm,
                value if axis == "y" else pp.y_mm,
            )

    _apply_align_constraints()

    def _part_area(ref: str) -> float:
        part = part_by_ref.get(ref)
        if part is None:
            pp = placed_map[ref]
            w, h = fp_bboxes.get(pp.footprint, _DEFAULT_BBOX)
        else:
            w, h = _bbox(part, fp_bboxes)
        return w * h

    def _resolve_overlaps() -> None:
        movable_refs = sorted(
            (ref for ref in placed_map if ref not in fixed_refs),
            key=_part_area,
        )
        for _ in range(2):
            moved = False
            for ref in movable_refs:
                if ref not in placed_map or ref not in part_by_ref:
                    continue
                pp = placed_map[ref]
                part = part_by_ref[ref]
                w, h = _bbox(part, fp_bboxes)
                if not _overlaps_any(pp.x_mm, pp.y_mm, w, h, _occupied_without(ref)):
                    continue
                before = (pp.x_mm, pp.y_mm)
                _move(ref, pp.x_mm, pp.y_mm)
                after = placed_map[ref]
                moved = moved or (before != (after.x_mm, after.y_mm))
            if not moved:
                break

    _resolve_overlaps()

    for constraint in constraints.near or []:
        if constraint.ref not in placed_map or constraint.target_ref not in placed_map:
            continue
        pp = placed_map[constraint.ref]
        target = placed_map[constraint.target_ref]
        current = _distance_xy((pp.x_mm, pp.y_mm), (target.x_mm, target.y_mm))
        if current <= constraint.distance_mm:
            continue
        angle = math.atan2(pp.y_mm - target.y_mm, pp.x_mm - target.x_mm)
        if current == 0:
            angle = 0.0
        _move(
            constraint.ref,
            target.x_mm + math.cos(angle) * constraint.distance_mm,
            target.y_mm + math.sin(angle) * constraint.distance_mm,
        )

    _resolve_overlaps()

    for constraint in constraints.far or []:
        if constraint.ref not in placed_map or constraint.target_ref not in placed_map:
            continue
        pp = placed_map[constraint.ref]
        target = placed_map[constraint.target_ref]
        current = _distance_xy((pp.x_mm, pp.y_mm), (target.x_mm, target.y_mm))
        if current >= constraint.distance_mm:
            continue
        angle = math.atan2(pp.y_mm - target.y_mm, pp.x_mm - target.x_mm)
        if current == 0:
            angle = 0.0
        _move(
            constraint.ref,
            target.x_mm + math.cos(angle) * constraint.distance_mm,
            target.y_mm + math.sin(angle) * constraint.distance_mm,
        )


def place_parts(
    groups: dict,
    constraints: LayoutConstraints,
    fp_bboxes: dict[str, tuple[float, float]],
    circuit=None,
    fp_geometries: dict[str, object] | None = None,
) -> list[PlacedPart]:
    """Place all parts, honoring fixed positions and filling in the rest.

    When *circuit* is provided, connector edge anchors and face-edge
    constraints are inferred automatically.  User-supplied constraints
    take priority — inferred anchors never override explicit ones.
    """
    if circuit is not None:
        from .candidates import _merge_inferred_edge_anchors
        from .intent import infer_placement_intents

        intent_plan = infer_placement_intents(circuit, outline=constraints.outline)
        constraints = _merge_inferred_edge_anchors(constraints, intent_plan)

    fixed_map = {fp.ref: fp for fp in (constraints.fixed or [])}
    edge_map = {ea.ref: ea for ea in (constraints.edge_anchors or [])}

    # placed_map: ref → PlacedPart
    placed_map: dict[str, PlacedPart] = {}
    # Grid-based overlap index — also maintains a flat list for legacy callers.
    _grid = SpatialGrid(cell_size_mm=10.0)
    occupied: list[tuple] = []
    for ko_entry in _occupied_from_keepouts(constraints.keepouts):
        occupied.append(ko_entry)
        _grid.insert(f"__ko_{len(occupied)}", ko_entry[0], ko_entry[1], ko_entry[2], ko_entry[3])

    def _commit(
        pp: PlacedPart,
        w: float,
        h: float,
        center_x: float | None = None,
        center_y: float | None = None,
    ):
        placed_map[pp.ref] = pp
        cx = pp.x_mm if center_x is None else center_x
        cy = pp.y_mm if center_y is None else center_y
        occupied.append((cx, cy, w, h))
        _grid.insert(pp.ref, cx, cy, w, h)

    all_parts = []
    for group in groups.values():
        for part in group.parts:
            all_parts.append((part, group))

    primary_refs = {
        part.ref for part, _ in all_parts if _is_primary_part(part)
    }

    # Layer 1: fixed positions
    for part, group in all_parts:
        if part.ref in fixed_map:
            fp_constraint = fixed_map[part.ref]
            w, h = _bbox(part, fp_bboxes)
            pp = PlacedPart(
                ref=part.ref,
                x_mm=fp_constraint.x_mm,
                y_mm=fp_constraint.y_mm,
                rot_deg=_face_edge_rotation(
                    part.ref, constraints, fp_constraint.rot_deg
                ),
                footprint=_footprint_name(part),
            )
            _commit(pp, w, h)

    # Layer 2: explicit edge anchors, e.g. USB jacks that must meet the outline.
    for part, group in all_parts:
        if part.ref in placed_map or part.ref not in edge_map:
            continue
        w, h = _bbox(part, fp_bboxes)
        footprint = _footprint_name(part)
        geometry = (fp_geometries or {}).get(footprint)
        origin_x, origin_y, rot, center_x, center_y, ew, eh = (
            _edge_anchor_origin_position(
                edge_map[part.ref],
                w,
                h,
                constraints.outline,
                geometry=geometry,
                ref=part.ref,
                footprint=footprint,
            )
        )
        bounds = constraints.outline
        x_center, y_center = _find_clear_edge_position(
            edge_map[part.ref],
            center_x,
            center_y,
            ew,
            eh,
            _grid,
            bounds=bounds,
        )
        origin_x += x_center - center_x
        origin_y += y_center - center_y
        _commit(
            PlacedPart(
                ref=part.ref,
                x_mm=origin_x,
                y_mm=origin_y,
                rot_deg=rot,
                footprint=footprint,
            ),
            ew,
            eh,
            center_x=x_center,
            center_y=y_center,
        )

    # Layer 3: primary parts before passives. This gives capacitors and
    # resistors meaningful parent candidates instead of only the fixed refs.
    for group in groups.values():
        anchor_ref = _largest_ic_ref(group)
        for part in group.parts:
            if part.ref in placed_map or not _is_primary_part(part):
                continue
            w, h = _bbox(part, fp_bboxes)
            bounds = _bounds_for_part(part, group, constraints)
            if anchor_ref and anchor_ref in placed_map:
                anchor = placed_map[anchor_ref]
                aw, ah = _bbox_for_ref(anchor_ref, all_parts, fp_bboxes)
                target_x = anchor.x_mm - aw / 2 - w / 2 - 2.0
                target_y = anchor.y_mm
            elif bounds is not None:
                target_x, target_y = _bounds_center(bounds)
            else:
                target_x, target_y = _spillover_position(placed_map, constraints)
            target_x, target_y = _clamp_to_bounds(target_x, target_y, w, h, bounds)
            x, y = _find_clear_position(
                target_x, target_y, w, h, _grid, bounds=bounds
            )
            x, y = _clamp_to_bounds(x, y, w, h, bounds)
            _commit(
                PlacedPart(
                    ref=part.ref,
                    x_mm=x,
                    y_mm=y,
                    rot_deg=_face_edge_rotation(part.ref, constraints, 0.0),
                    footprint=_footprint_name(part),
                ),
                w,
                h,
            )

    # Layer 4: decoupling caps
    decap_parent_counts: dict[str, int] = {}
    for part, group in all_parts:
        if part.ref in placed_map:
            continue
        if not _is_decoupling_cap(part):
            continue
        w, h = _bbox(part, fp_bboxes)
        bounds = _bounds_for_part(part, group, constraints)
        parent_ref = _most_adjacent_placed(
            part.ref,
            group.adjacency,
            placed_map,
            candidate_refs=primary_refs,
            usage_counts=decap_parent_counts,
        )
        if parent_ref:
            decap_parent_counts[parent_ref] = (
                decap_parent_counts.get(parent_ref, 0) + 1
            )
            parent = placed_map[parent_ref]
            pw, ph = _bbox_for_ref(parent_ref, all_parts, fp_bboxes)
            n = decap_parent_counts[parent_ref] - 1
            x, y = _find_near_parent(
                parent.x_mm, parent.y_mm, pw, ph, w, h, n, _grid, bounds,
            )
            rot = parent.rot_deg
        else:
            target_x, target_y = (
                _bounds_center(bounds)
                if bounds is not None
                else _spillover_position(placed_map, constraints)
            )
            rot = 0.0
            target_x, target_y = _clamp_to_bounds(target_x, target_y, w, h, bounds)
            x, y = _find_clear_position(
                target_x, target_y, w, h, _grid, bounds=bounds
            )
            x, y = _clamp_to_bounds(x, y, w, h, bounds)
        _commit(PlacedPart(ref=part.ref, x_mm=x, y_mm=y, rot_deg=rot,
                           footprint=_footprint_name(part)), w, h)

    # Layer 5: signal passives (2-pin, not decoupling caps)
    # Track how many passives have been stacked per parent ref
    stack_count: dict[str, int] = {}
    for part, group in all_parts:
        if part.ref in placed_map:
            continue
        if len(part) != 2:
            continue
        w, h = _bbox(part, fp_bboxes)
        bounds = _bounds_for_part(part, group, constraints)
        bus_target = _multi_primary_centroid_target(
            part.ref,
            group.adjacency,
            placed_map,
            primary_refs,
            bounds,
        )
        parent_ref = None if bus_target is not None else _most_adjacent_placed(
            part.ref,
            group.adjacency,
            placed_map,
            candidate_refs=primary_refs,
            usage_counts=stack_count,
        )
        if bus_target is not None:
            target_x, target_y = bus_target
            rot = 0.0
            target_x, target_y = _clamp_to_bounds(target_x, target_y, w, h, bounds)
            x, y = _find_clear_position(
                target_x,
                target_y,
                w,
                h,
                _grid,
                bounds=bounds,
                step=0.5,
                max_radius=30.0,
            )
            x, y = _clamp_to_bounds(x, y, w, h, bounds)
        elif parent_ref:
            parent = placed_map[parent_ref]
            pw, ph = _bbox_for_ref(parent_ref, all_parts, fp_bboxes)
            n = stack_count.get(parent_ref, 0)
            stack_count[parent_ref] = n + 1
            x, y = _find_near_parent(
                parent.x_mm, parent.y_mm, pw, ph, w, h, n, _grid, bounds,
            )
            rot = parent.rot_deg
        else:
            target_x, target_y = (
                _bounds_center(bounds)
                if bounds is not None
                else _spillover_position(placed_map, constraints)
            )
            rot = 0.0
            target_x, target_y = _clamp_to_bounds(target_x, target_y, w, h, bounds)
            x, y = _find_clear_position(
                target_x, target_y, w, h, _grid, bounds=bounds
            )
            x, y = _clamp_to_bounds(x, y, w, h, bounds)
        _commit(PlacedPart(ref=part.ref, x_mm=x, y_mm=y, rot_deg=rot,
                           footprint=_footprint_name(part)), w, h)

    _apply_soft_constraints(placed_map, all_parts, constraints, fp_bboxes)

    return list(placed_map.values())


def _bbox_for_ref(ref: str, all_parts, fp_bboxes: dict) -> tuple[float, float]:
    for part, _ in all_parts:
        if part.ref == ref:
            return _bbox(part, fp_bboxes)
    return _DEFAULT_BBOX


def derive_outline(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    margin_mm: float = 3.0,
    form_factor: str | None = None,
    min_area_mm2: float = 0.0,
    max_min_area_growth: float | None = None,
) -> BoardOutline:
    """Return a rectangular outline enclosing placed parts plus margin.

    If *form_factor* matches a key in ``FORM_FACTORS``, the standard
    board dimensions are returned instead of auto-sizing.

    If *min_area_mm2* is positive and the auto-derived area is smaller,
    the outline is expanded proportionally to meet the minimum. If
    *max_min_area_growth* is positive, the minimum is capped to that
    multiple of the placed-part envelope so density estimates cannot
    balloon compact placements.
    """
    if form_factor and form_factor in FORM_FACTORS:
        return FORM_FACTORS[form_factor]

    if not placed_parts:
        return BoardOutline(50.0, 50.0)

    x_min = float("inf")
    y_min = float("inf")
    x_max = float("-inf")
    y_max = float("-inf")
    for pp in placed_parts:
        w, h = fp_bboxes.get(pp.footprint, _DEFAULT_BBOX)
        if pp.rot_deg % 180 == 90:
            w, h = h, w
        x_min = min(x_min, pp.x_mm - w / 2)
        y_min = min(y_min, pp.y_mm - h / 2)
        x_max = max(x_max, pp.x_mm + w / 2)
        y_max = max(y_max, pp.y_mm + h / 2)

    x_min -= margin_mm
    y_min -= margin_mm
    x_max += margin_mm
    y_max += margin_mm

    width = x_max - x_min
    height = y_max - y_min
    if min_area_mm2 > 0 and width * height < min_area_mm2:
        if max_min_area_growth is not None and max_min_area_growth > 0:
            min_area_mm2 = min(min_area_mm2, width * height * max_min_area_growth)
        scale = math.sqrt(min_area_mm2 / (width * height))
        cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
        width *= scale
        height *= scale
        x_min = cx - width / 2
        x_max = cx + width / 2
        y_min = cy - height / 2
        y_max = cy + height / 2

    return BoardOutline(
        vertices=[
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ]
    )


def derive_outline_from_circuit(
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    packing_factor: float = 0.35,
    margin_mm: float = 3.0,
) -> BoardOutline:
    """Estimate board outline from circuit part areas before placement.

    Uses component footprint areas divided by *packing_factor* to estimate
    the minimum board area needed.  Returns a 4:3 aspect ratio rectangle.
    """
    total_area = 0.0
    for part in circuit.parts:
        fp = str(getattr(part, "footprint", ""))
        w, h = fp_bboxes.get(fp, _DEFAULT_BBOX)
        total_area += w * h

    if total_area <= 0:
        return BoardOutline(50.0, 50.0)

    board_area = total_area / packing_factor
    height = math.sqrt(board_area / (4.0 / 3.0))
    width = height * (4.0 / 3.0)
    width += 2 * margin_mm
    height += 2 * margin_mm
    return BoardOutline(width, height)


def _spillover_position(placed_map: dict, constraints) -> tuple[float, float]:
    """Find a position in the spillover area below all placed parts."""
    if not placed_map:
        return 10.0, 10.0
    max_y = max(pp.y_mm for pp in placed_map.values())
    avg_x = sum(pp.x_mm for pp in placed_map.values()) / len(placed_map)
    if constraints.outline:
        return avg_x, max_y + 10.0
    return avg_x, max_y + 10.0
