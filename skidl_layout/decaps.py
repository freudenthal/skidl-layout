from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .candidates import PlacementCandidate
from .constraints import LayoutConstraints
from .geometry import FootprintGeometry, PadGeometry, transform_point
from .placer import (
    _bbox,
    _clamp_to_bounds,
    _find_clear_position,
    _occupied_from_keepouts,
)
from .roles import DECAP_VALUE_RE, GND_NET_RE, POWER_NET_RE, classify_parts
from .writer import PlacedPart


@dataclass(frozen=True)
class DecapPlacementIntent:
    ref: str
    parent_ref: str
    supply_net: str
    ground_net: str
    target_power_pin: str | None
    target_ground_pin: str | None
    target_power_xy: tuple[float, float] | None
    target_ground_xy: tuple[float, float] | None
    average_pad_distance_mm: float | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class DecapRefinementResult:
    placed_parts: list[PlacedPart]
    intents: list[DecapPlacementIntent] = field(default_factory=list)
    ref_reasons: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class DecapPadDistance:
    ref: str
    parent_ref: str
    average_pad_distance_mm: float


def _pin_number(pin, index: int) -> str:
    for attr in ("num", "number", "pin_number", "name"):
        value = getattr(pin, attr, None)
        if value not in (None, ""):
            return str(value).strip('"')
    return str(index + 1)


def _pin_net_name(pin) -> str | None:
    net = getattr(pin, "net", None)
    name = getattr(net, "name", None)
    return str(name) if name else None


def _part_pin_nets_by_number(part) -> dict[str, str]:
    pin_nets: dict[str, str] = {}
    for index, pin in enumerate(getattr(part, "pins", []) or []):
        net_name = _pin_net_name(pin)
        if net_name:
            pin_nets[_pin_number(pin, index)] = net_name
    return pin_nets


def _pad_net_name(
    pad: PadGeometry,
    pin_nets: dict[str, str],
) -> str | None:
    return pad.net_name or pin_nets.get(pad.number)


def _pads_for_net(
    part,
    geometry: FootprintGeometry,
    net_name: str,
) -> list[PadGeometry]:
    pin_nets = _part_pin_nets_by_number(part)
    pads = [
        pad
        for pad in geometry.pads
        if _pad_net_name(pad, pin_nets) == net_name
    ]
    return sorted(pads, key=lambda pad: _pad_sort_key(pad.number))


def _pad_sort_key(number: str) -> tuple[int, str]:
    try:
        return int(number), number
    except ValueError:
        return 10_000, number


def _supply_ground_for_decap(part) -> tuple[str, str] | None:
    try:
        pin_count = len(part)
    except Exception:
        pin_count = len(getattr(part, "pins", []) or [])
    if pin_count != 2:
        return None
    if not DECAP_VALUE_RE.match(str(getattr(part, "value", "") or "").strip()):
        return None

    nets = list(_part_pin_nets_by_number(part).values())
    supplies = [net for net in nets if POWER_NET_RE.match(net)]
    grounds = [net for net in nets if GND_NET_RE.match(net)]
    if not supplies or not grounds:
        return None
    return supplies[0], grounds[0]


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _pad_world_xy(
    pad: PadGeometry,
    placed: PlacedPart,
) -> tuple[float, float]:
    return transform_point(
        placed.x_mm,
        placed.y_mm,
        placed.rot_deg,
        pad.x_mm,
        pad.y_mm,
    )


def _nearest_pad(
    pads: list[PadGeometry],
    target: PadGeometry,
) -> PadGeometry | None:
    if not pads:
        return None
    return min(
        pads,
        key=lambda pad: _distance(
            (pad.x_mm, pad.y_mm),
            (target.x_mm, target.y_mm),
        ),
    )


def _role_priority(role: str) -> int:
    return {
        "regulator": 50,
        "ic": 40,
        "module_socket": 40,
        "connector": 15,
        "unknown": 0,
    }.get(role, 5)


def _alpha_tokens(text: str) -> set[str]:
    generic_tokens = {
        "CAP",
        "CAPACITOR",
        "DEVICE",
        "FOOTPRINT",
        "IC",
        "LGA",
        "MCU",
        "METRIC",
        "MODULE",
        "PACKAGE",
        "PKG",
        "RESISTOR",
        "SMD",
        "SENSOR",
    }
    tokens: set[str] = set()
    for match in re.finditer(r"[A-Za-z]{3,}", str(text or "")):
        token = match.group(0).upper()
        if token not in generic_tokens:
            tokens.add(token)
        if token[0] in {"C", "R", "L", "D", "U", "J", "Q"} and len(token) >= 4:
            stripped = token[1:]
            if stripped not in generic_tokens:
                tokens.add(stripped)
    return tokens


def _part_tokens(part) -> set[str]:
    fields = (
        getattr(part, "ref", ""),
        getattr(part, "name", ""),
        getattr(part, "value", ""),
        getattr(part, "footprint", ""),
    )
    tokens: set[str] = set()
    for field in fields:
        tokens.update(_alpha_tokens(str(field)))
    return tokens


def _token_affinity(cap_part, parent_part) -> int:
    cap_tokens = _part_tokens(cap_part)
    parent_tokens = _part_tokens(parent_part)
    if not cap_tokens or not parent_tokens:
        return 0
    best = 0
    for cap_token in cap_tokens:
        for parent_token in parent_tokens:
            if cap_token == parent_token:
                best = max(best, 3)
            elif cap_token in parent_token or parent_token in cap_token:
                best = max(best, 2)
    return best


def _average_candidate_pad_distance(
    cap_placed: PlacedPart,
    parent_placed: PlacedPart,
    power_pads: list[PadGeometry],
    ground_pads: list[PadGeometry],
) -> float:
    pad_points = [
        _pad_world_xy(pad, parent_placed)
        for pad in [*power_pads, *ground_pads]
    ]
    if not pad_points:
        return _distance(
            (cap_placed.x_mm, cap_placed.y_mm),
            (parent_placed.x_mm, parent_placed.y_mm),
        )
    return min(
        _distance((cap_placed.x_mm, cap_placed.y_mm), pad_xy)
        for pad_xy in pad_points
    )


def _select_parent(
    cap_ref: str,
    supply_net: str,
    ground_net: str,
    circuit,
    placed_by_ref: dict[str, PlacedPart],
    fp_geometries: dict[str, FootprintGeometry],
    roles=None,
) -> tuple[object, list[PadGeometry], list[PadGeometry]] | None:
    if roles is None:
        roles = classify_parts(circuit)
    part_by_ref = {getattr(part, "ref", None): part for part in circuit.parts}
    cap_part = part_by_ref.get(cap_ref)
    cap_placed = placed_by_ref.get(cap_ref)
    candidates = []
    for part in circuit.parts:
        ref = getattr(part, "ref", None)
        if not ref or ref == cap_ref or ref not in placed_by_ref:
            continue
        role = roles.get(ref)
        role_name = role.role if role is not None else "unknown"
        if role_name not in {"ic", "regulator", "module_socket"}:
            continue
        geometry = fp_geometries.get(placed_by_ref[ref].footprint)
        if geometry is None:
            continue
        power_pads = _pads_for_net(part, geometry, supply_net)
        if not power_pads:
            continue
        ground_pads = _pads_for_net(part, geometry, ground_net)
        score = (
            _role_priority(role_name)
            + len(power_pads) * 4
            + len(ground_pads) * 2
        )
        parent_placed = placed_by_ref[ref]
        distance = (
            _average_candidate_pad_distance(
                cap_placed,
                parent_placed,
                power_pads,
                ground_pads,
            )
            if cap_placed is not None
            else 0.0
        )
        affinity = _token_affinity(cap_part, part) if cap_part is not None else 0
        candidates.append(
            (-affinity, distance, -score, str(ref), part, power_pads, ground_pads)
        )

    if not candidates:
        return None

    _, _, _, _, parent, power_pads, ground_pads = min(candidates)
    return parent, power_pads, ground_pads


def _select_target_pads(
    power_pads: list[PadGeometry],
    ground_pads: list[PadGeometry],
    usage_index: int,
) -> tuple[PadGeometry | None, PadGeometry | None]:
    if not power_pads:
        return None, None
    power_pad = power_pads[usage_index % len(power_pads)]
    return power_pad, _nearest_pad(ground_pads, power_pad)


def infer_decap_placement_intents(
    circuit,
    placed_parts: list[PlacedPart],
    fp_geometries: dict[str, FootprintGeometry],
    roles=None,
) -> list[DecapPlacementIntent]:
    """Infer decap-to-parent pad targets from SKiDL pins and footprint geometry."""
    if circuit is None or not fp_geometries:
        return []

    if roles is None:
        roles = classify_parts(circuit)
    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    usage_counts: dict[tuple[str, str], int] = {}
    intents: list[DecapPlacementIntent] = []

    for part in circuit.parts:
        ref = getattr(part, "ref", None)
        if ref not in placed_by_ref:
            continue
        supply_ground = _supply_ground_for_decap(part)
        if supply_ground is None:
            continue
        supply_net, ground_net = supply_ground
        parent = _select_parent(
            ref,
            supply_net,
            ground_net,
            circuit,
            placed_by_ref,
            fp_geometries,
            roles,
        )
        if parent is None:
            continue

        parent_part, power_pads, ground_pads = parent
        parent_ref = getattr(parent_part, "ref")
        usage_key = (parent_ref, supply_net)
        usage_index = usage_counts.get(usage_key, 0)
        usage_counts[usage_key] = usage_index + 1

        power_pad, ground_pad = _select_target_pads(
            power_pads,
            ground_pads,
            usage_index,
        )
        parent_placed = placed_by_ref[parent_ref]
        power_xy = _pad_world_xy(power_pad, parent_placed) if power_pad else None
        ground_xy = _pad_world_xy(ground_pad, parent_placed) if ground_pad else None

        intents.append(
            DecapPlacementIntent(
                ref=ref,
                parent_ref=parent_ref,
                supply_net=supply_net,
                ground_net=ground_net,
                target_power_pin=power_pad.number if power_pad else None,
                target_ground_pin=ground_pad.number if ground_pad else None,
                target_power_xy=power_xy,
                target_ground_xy=ground_xy,
                reasons=[
                    (
                        f"{ref} shares {supply_net}/{ground_net} with "
                        f"{parent_ref} actual footprint pads"
                    )
                ],
            )
        )

    return intents


def _target_points(intent: DecapPlacementIntent) -> list[tuple[float, float]]:
    return [
        xy
        for xy in (intent.target_power_xy, intent.target_ground_xy)
        if xy is not None
    ]


def _average_xy(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _placement_target(
    intent: DecapPlacementIntent,
    parent: PlacedPart,
    cap_width: float,
    cap_height: float,
    parent_geometry: FootprintGeometry | None = None,
) -> tuple[float, float] | None:
    target = _average_xy(_target_points(intent))
    if target is None:
        return None
    parent_x, parent_y = parent.x_mm, parent.y_mm
    if parent_geometry is not None:
        x_min, y_min, x_max, y_max = parent_geometry.transformed_bounds(parent)
        parent_x = (x_min + x_max) / 2
        parent_y = (y_min + y_max) / 2
    dx = target[0] - parent_x
    dy = target[1] - parent_y
    magnitude = math.hypot(dx, dy)
    if magnitude < 1e-6:
        dx, dy, magnitude = 1.0, 0.0, 1.0
    clearance = max(cap_width, cap_height) / 2 + 0.8
    return (
        target[0] + dx / magnitude * clearance,
        target[1] + dy / magnitude * clearance,
    )


def _cap_pad_for_net(
    cap_part,
    cap_geometry: FootprintGeometry,
    net_name: str,
) -> PadGeometry | None:
    pads = _pads_for_net(cap_part, cap_geometry, net_name)
    return pads[0] if pads else None


def _best_cap_rotation(
    placed: PlacedPart,
    cap_part,
    cap_geometry: FootprintGeometry,
    intent: DecapPlacementIntent,
) -> tuple[float, float | None]:
    power_pad = _cap_pad_for_net(cap_part, cap_geometry, intent.supply_net)
    ground_pad = _cap_pad_for_net(cap_part, cap_geometry, intent.ground_net)
    targets = [
        (power_pad, intent.target_power_xy),
        (ground_pad, intent.target_ground_xy),
    ]
    targets = [
        (pad, xy)
        for pad, xy in targets
        if pad is not None and xy is not None
    ]
    if not targets:
        return placed.rot_deg, None

    candidates = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        distances = [
            _distance(
                transform_point(
                    placed.x_mm,
                    placed.y_mm,
                    rotation,
                    pad.x_mm,
                    pad.y_mm,
                ),
                xy,
            )
            for pad, xy in targets
        ]
        candidates.append(
            (
                sum(distances) / len(distances),
                abs(rotation - placed.rot_deg),
                rotation,
            )
        )
    distance_mm, _, rotation = min(candidates)
    return rotation, distance_mm


def measure_decap_pad_distances(
    placed_parts: list[PlacedPart],
    circuit,
    fp_geometries: dict[str, FootprintGeometry],
) -> dict[str, DecapPadDistance]:
    """Measure decap pad distance to the parent IC/regulator supply pads."""
    if circuit is None or not fp_geometries:
        return {}

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    part_by_ref = {getattr(part, "ref", None): part for part in circuit.parts}
    distances: dict[str, DecapPadDistance] = {}

    for intent in infer_decap_placement_intents(circuit, placed_parts, fp_geometries):
        cap_part = part_by_ref.get(intent.ref)
        cap_placed = placed_by_ref.get(intent.ref)
        cap_geometry = fp_geometries.get(cap_placed.footprint) if cap_placed else None
        if cap_part is None or cap_placed is None or cap_geometry is None:
            continue

        targets = [
            (_cap_pad_for_net(cap_part, cap_geometry, intent.supply_net), intent.target_power_xy),
            (_cap_pad_for_net(cap_part, cap_geometry, intent.ground_net), intent.target_ground_xy),
        ]
        measured = [
            _distance(
                transform_point(
                    cap_placed.x_mm,
                    cap_placed.y_mm,
                    cap_placed.rot_deg,
                    pad.x_mm,
                    pad.y_mm,
                ),
                target_xy,
            )
            for pad, target_xy in targets
            if pad is not None and target_xy is not None
        ]
        if not measured:
            continue
        distances[intent.ref] = DecapPadDistance(
            ref=intent.ref,
            parent_ref=intent.parent_ref,
            average_pad_distance_mm=sum(measured) / len(measured),
        )

    return distances


def _locked_refs(constraints: LayoutConstraints | None) -> set[str]:
    if constraints is None:
        return set()
    # This decap-only refinement pass only moves capacitors already classified
    # as local decoupling caps. Floorplan fixed positions for those caps are
    # useful seed coordinates, but electrical proximity to the parent IC/reg
    # is stronger product intent. Edge anchors remain hard mechanical locks.
    return {anchor.ref for anchor in constraints.edge_anchors or []}


def _transformed_bounds(
    bounds: tuple[float, float, float, float],
    placed: PlacedPart,
) -> tuple[float, float, float, float]:
    x_min, y_min, x_max, y_max = bounds
    points = [
        transform_point(placed.x_mm, placed.y_mm, placed.rot_deg, x, y)
        for x, y in (
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        )
    ]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _occupied_without(
    placed_by_ref: dict[str, PlacedPart],
    skip_ref: str,
    circuit,
    fp_bboxes: dict,
    keepouts,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    parent_ref: str | None = None,
) -> list[tuple[float, float, float, float]]:
    part_by_ref = {getattr(part, "ref", None): part for part in circuit.parts}
    occupied = _occupied_from_keepouts(keepouts)
    for ref, placed in placed_by_ref.items():
        if ref == skip_ref:
            continue
        geometry = (fp_geometries or {}).get(placed.footprint)
        if geometry is not None:
            if ref == parent_ref and geometry.body_bounds is not None:
                # Parent courtyards for modules often include keepout or antenna
                # envelopes that are much larger than the physical package edge.
                # A local decap should be able to sit just outside the body near
                # castellated/module power pads. Final exact-geometry nudging
                # below still prevents physical pad/package clearance failures.
                x_min, y_min, x_max, y_max = _transformed_bounds(
                    geometry.body_bounds,
                    placed,
                )
            else:
                x_min, y_min, x_max, y_max = geometry.transformed_bounds(placed)
            occupied.append(
                (
                    (x_min + x_max) / 2,
                    (y_min + y_max) / 2,
                    x_max - x_min,
                    y_max - y_min,
                )
            )
            continue
        part = part_by_ref.get(ref)
        if part is not None:
            width, height = _bbox(part, fp_bboxes)
        else:
            width, height = fp_bboxes.get(placed.footprint, (2.0, 2.0))
        occupied.append((placed.x_mm, placed.y_mm, width, height))
    return occupied


def _rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    clearance_mm: float = 0.0,
) -> bool:
    ax_min, ay_min, ax_max, ay_max = a
    bx_min, by_min, bx_max, by_max = b
    return (
        ax_min < bx_max + clearance_mm
        and ax_max > bx_min - clearance_mm
        and ay_min < by_max + clearance_mm
        and ay_max > by_min - clearance_mm
    )


def _fallback_part_bounds(
    placed: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
) -> tuple[float, float, float, float]:
    width, height = fp_bboxes.get(placed.footprint, (2.0, 2.0))
    if placed.rot_deg % 180 == 90:
        width, height = height, width
    return (
        placed.x_mm - width / 2,
        placed.y_mm - height / 2,
        placed.x_mm + width / 2,
        placed.y_mm + height / 2,
    )


def _occupied_bounds_without(
    placed_by_ref: dict[str, PlacedPart],
    skip_ref: str,
    fp_bboxes: dict[str, tuple[float, float]],
    keepouts,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[tuple[float, float, float, float]]:
    occupied: list[tuple[float, float, float, float]] = []
    for keepout in keepouts or []:
        occupied.append((keepout.x_min, keepout.y_min, keepout.x_max, keepout.y_max))
    for ref, placed in placed_by_ref.items():
        if ref == skip_ref:
            continue
        geometry = (fp_geometries or {}).get(placed.footprint)
        if geometry is not None:
            occupied.append(geometry.transformed_physical_bounds(placed))
        else:
            occupied.append(_fallback_part_bounds(placed, fp_bboxes))
    return occupied


def _bounds_fit_outline(
    bounds: tuple[float, float, float, float],
    outline,
) -> bool:
    if outline is None:
        return True
    x_min, y_min, x_max, y_max = bounds
    return (
        x_min >= outline.x_min
        and y_min >= outline.y_min
        and x_max <= outline.x_max
        and y_max <= outline.y_max
    )


def _candidate_bounds(
    placed: PlacedPart,
    geometry: FootprintGeometry | None,
    fp_bboxes: dict[str, tuple[float, float]],
) -> tuple[float, float, float, float]:
    if geometry is not None:
        return geometry.transformed_physical_bounds(placed)
    return _fallback_part_bounds(placed, fp_bboxes)


def _is_clear_bounds(
    bounds: tuple[float, float, float, float],
    occupied_bounds: list[tuple[float, float, float, float]],
    outline,
    clearance_mm: float,
) -> bool:
    return _bounds_fit_outline(bounds, outline) and not any(
        _rects_overlap(bounds, occupied, clearance_mm)
        for occupied in occupied_bounds
    )


def _find_clear_geometry_position(
    placed: PlacedPart,
    geometry: FootprintGeometry | None,
    fp_bboxes: dict[str, tuple[float, float]],
    occupied_bounds: list[tuple[float, float, float, float]],
    outline,
    *,
    clearance_mm: float = 0.5,
    step: float = 0.25,
    max_radius: float = 18.0,
) -> tuple[float, float]:
    def bounds_at(x: float, y: float) -> tuple[float, float, float, float]:
        return _candidate_bounds(
            PlacedPart(
                placed.ref,
                x,
                y,
                placed.rot_deg,
                placed.footprint,
                placed.side,
            ),
            geometry,
            fp_bboxes,
        )

    if _is_clear_bounds(
        bounds_at(placed.x_mm, placed.y_mm),
        occupied_bounds,
        outline,
        clearance_mm,
    ):
        return placed.x_mm, placed.y_mm

    steps = max(1, int(max_radius / step))
    for i in range(1, steps + 1):
        radius = step * i
        angle_count = max(8, int(radius * 2 * math.pi))
        for j in range(angle_count):
            angle = j * (2 * math.pi / angle_count)
            x = placed.x_mm + radius * math.cos(angle)
            y = placed.y_mm + radius * math.sin(angle)
            if _is_clear_bounds(
                bounds_at(x, y),
                occupied_bounds,
                outline,
                clearance_mm,
            ):
                return x, y
    return placed.x_mm, placed.y_mm


def refine_decaps(
    placed_parts: list[PlacedPart],
    circuit,
    fp_geometries: dict[str, FootprintGeometry],
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None = None,
    roles=None,
) -> DecapRefinementResult:
    """Move decaps near actual parent power/GND pads when geometry is available."""
    if circuit is None or not fp_geometries:
        return DecapRefinementResult(list(placed_parts))

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    part_by_ref = {getattr(part, "ref", None): part for part in circuit.parts}
    intents = infer_decap_placement_intents(
        circuit, placed_parts, fp_geometries, roles
    )
    locked = _locked_refs(constraints)
    reasons: dict[str, list[str]] = {}

    for intent in intents:
        if intent.ref in locked:
            continue
        cap_part = part_by_ref.get(intent.ref)
        parent_placed = placed_by_ref.get(intent.parent_ref)
        cap_placed = placed_by_ref.get(intent.ref)
        if cap_part is None or parent_placed is None or cap_placed is None:
            continue

        cap_geometry = fp_geometries.get(cap_placed.footprint)
        if cap_geometry is None:
            continue

        width, height = _bbox(cap_part, fp_bboxes)
        parent_geometry = fp_geometries.get(parent_placed.footprint)
        target = _placement_target(
            intent,
            parent_placed,
            width,
            height,
            parent_geometry,
        )
        if target is None:
            continue

        bounds = constraints.outline if constraints is not None else None
        side = max(width, height)
        target_x, target_y = _clamp_to_bounds(target[0], target[1], side, side, bounds)
        occupied = _occupied_without(
            placed_by_ref,
            intent.ref,
            circuit,
            fp_bboxes,
            constraints.keepouts if constraints is not None else None,
            fp_geometries,
            parent_ref=intent.parent_ref,
        )
        x, y = _find_clear_position(
            target_x,
            target_y,
            side,
            side,
            occupied,
            bounds=bounds,
            step=0.5,
            max_radius=18.0,
        )
        x, y = _clamp_to_bounds(x, y, side, side, bounds)

        provisional = PlacedPart(
            ref=cap_placed.ref,
            x_mm=x,
            y_mm=y,
            rot_deg=cap_placed.rot_deg,
            footprint=cap_placed.footprint,
        )
        rotation, distance_mm = _best_cap_rotation(
            provisional,
            cap_part,
            cap_geometry,
            intent,
        )
        refined = PlacedPart(
            ref=cap_placed.ref,
            x_mm=x,
            y_mm=y,
            rot_deg=rotation,
            footprint=cap_placed.footprint,
        )
        occupied_bounds = _occupied_bounds_without(
            placed_by_ref,
            intent.ref,
            fp_bboxes,
            constraints.keepouts if constraints is not None else None,
            fp_geometries,
        )
        exact_x, exact_y = _find_clear_geometry_position(
            refined,
            cap_geometry,
            fp_bboxes,
            occupied_bounds,
            bounds,
        )
        if (exact_x, exact_y) != (refined.x_mm, refined.y_mm):
            refined = PlacedPart(
                ref=refined.ref,
                x_mm=exact_x,
                y_mm=exact_y,
                rot_deg=refined.rot_deg,
                footprint=refined.footprint,
                side=refined.side,
            )
            rotation, distance_mm = _best_cap_rotation(
                refined,
                cap_part,
                cap_geometry,
                intent,
            )
            refined = PlacedPart(
                ref=refined.ref,
                x_mm=refined.x_mm,
                y_mm=refined.y_mm,
                rot_deg=rotation,
                footprint=refined.footprint,
                side=refined.side,
            )
            exact_x, exact_y = _find_clear_geometry_position(
                refined,
                cap_geometry,
                fp_bboxes,
                occupied_bounds,
                bounds,
            )
            refined = PlacedPart(
                ref=refined.ref,
                x_mm=exact_x,
                y_mm=exact_y,
                rot_deg=refined.rot_deg,
                footprint=refined.footprint,
                side=refined.side,
            )
        placed_by_ref[intent.ref] = refined
        distance_text = (
            f", avg pad distance {distance_mm:.1f}mm"
            if distance_mm is not None
            else ""
        )
        reasons.setdefault(intent.ref, []).append(
            (
                f"placed near actual {intent.parent_ref} "
                f"{intent.supply_net}/{intent.ground_net} pads{distance_text}"
            )
        )

    return DecapRefinementResult(
        placed_parts=[placed_by_ref[placed.ref] for placed in placed_parts],
        intents=intents,
        ref_reasons=reasons,
    )


def refine_candidate_decaps(
    candidate: PlacementCandidate,
    circuit,
    fp_geometries: dict[str, FootprintGeometry],
    fp_bboxes: dict[str, tuple[float, float]],
    ctx=None,
) -> None:
    result = refine_decaps(
        candidate.placed_parts,
        circuit,
        fp_geometries,
        fp_bboxes,
        constraints=candidate.constraints,
        roles=ctx.roles if ctx is not None else None,
    )
    if not result.ref_reasons:
        return
    candidate.placed_parts = result.placed_parts
    candidate.reasons.append("decaps refined near actual parent power pins")
    candidate.pin_gravity_anchored_refs.update(result.ref_reasons)
    for ref, reasons in result.ref_reasons.items():
        candidate.ref_reasons.setdefault(ref, []).extend(reasons)
