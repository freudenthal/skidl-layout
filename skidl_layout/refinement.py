from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

from .candidates import PlacementCandidate
from .constraints import LayoutConstraints
from .geometry import FootprintGeometry, PadGeometry, transform_point
from .placer import _find_clear_position, _overlaps_any
from .roles import GND_NET_RE, POWER_NET_RE, classify_parts, is_nc_net, is_ui_grid_part
from .scoring import LayoutScore, _net_ref_lists, score_placement
from .validator import _same_physical_side, _through_board_pads_collide, validate
from .writer import PlacedPart


@dataclass
class RefinementResult:
    placed_parts: list[PlacedPart]
    start_score: float
    final_score: float
    accepted_moves: int = 0
    accepted_rotations: int = 0
    accepted_swaps: int = 0
    ref_reasons: dict[str, list[str]] = field(default_factory=dict)
    # Raw (unclamped) penalties: on saturated boards start/final_score both
    # clamp to 0.0, so the penalty is what actually shows local progress.
    start_penalty: float = 0.0
    final_penalty: float = 0.0

    @property
    def accepted_count(self) -> int:
        return self.accepted_moves + self.accepted_rotations + self.accepted_swaps


@dataclass(frozen=True)
class _TargetPadCandidate:
    role_sort: float
    origin_distance: float
    pad_xy: tuple[float, float]
    reason: str
    ref: str
    center_xy: tuple[float, float]


@dataclass(frozen=True)
class _PassiveGravityTarget:
    xy: tuple[float, float]
    reason: str
    parent_ref: str | None = None
    parent_center: tuple[float, float] | None = None
    side: str | None = None


@dataclass(frozen=True)
class _PassiveOwnerCandidate:
    ref: str
    role_name: str
    shared_signal_nets: tuple[str, ...]
    shared_supply_nets: tuple[str, ...]
    target_nets: tuple[str, ...]
    target_points: tuple[tuple[float, float], ...]
    center_xy: tuple[float, float]
    distance: float
    affinity: int


_PRIMARY_OWNER_ROLES = {"ic", "regulator", "module_socket"}
_FALLBACK_OWNER_ROLES = {"connector", "panel_jack", "control"}


def _clone_placed(placed_parts: list[PlacedPart]) -> list[PlacedPart]:
    return [replace(part) for part in placed_parts]


def _score(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
    clearance_mm: float,
    board_layers: int,
    ctx=None,
) -> LayoutScore:
    return score_placement(
        placed_parts,
        circuit,
        fp_bboxes,
        outline=constraints.outline if constraints is not None else None,
        keepouts=constraints.keepouts if constraints is not None else None,
        cutouts=constraints.cutouts if constraints is not None else None,
        fp_geometries=fp_geometries,
        clearance_mm=clearance_mm,
        board_layers=board_layers,
        ctx=ctx,
    )


def _hard_count(score: LayoutScore) -> int:
    return (
        score.overlap_count
        + score.outline_violation_count
        + score.keepout_violation_count
        + score.missing_count
    )


def _is_better(current: LayoutScore, trial: LayoutScore) -> bool:
    current_hard = _hard_count(current)
    trial_hard = _hard_count(trial)
    if trial_hard < current_hard:
        return True
    if trial_hard > current_hard:
        return False
    # Compare on the raw (unclamped) penalty rather than the 0-clamped score:
    # on dense boards the soft penalties saturate past 100, so every legal
    # placement clamps to score 0.0 and the local search would go blind to
    # HPWL/congestion/warning improvements. Lower penalty is strictly better.
    return trial.penalty < current.penalty - 1e-6


def _replace_ref(
    placed_parts: list[PlacedPart],
    ref: str,
    replacement: PlacedPart,
) -> list[PlacedPart]:
    return [replacement if part.ref == ref else part for part in placed_parts]


def _replace_refs(
    placed_parts: list[PlacedPart],
    replacements: dict[str, PlacedPart],
) -> list[PlacedPart]:
    return [replacements.get(part.ref, part) for part in placed_parts]


def _part_dimensions(
    placed: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[float, float]:
    geometry = (fp_geometries or {}).get(placed.footprint)
    if geometry is not None:
        x_min, y_min, x_max, y_max = geometry.transformed_bounds(
            PlacedPart(placed.ref, 0.0, 0.0, placed.rot_deg, placed.footprint)
        )
        return x_max - x_min, y_max - y_min
    return fp_bboxes.get(placed.footprint, (2.0, 2.0))


def _bounds_for_ref(ref: str, constraints: LayoutConstraints | None):
    if constraints is None:
        return None
    for zone in constraints.zones or []:
        if ref in (zone.refs or []):
            return zone
    return constraints.outline


def _bounds_key(bounds) -> tuple[float, float, float, float] | None:
    if bounds is None:
        return None
    return (bounds.x_min, bounds.y_min, bounds.x_max, bounds.y_max)


def _clamp_to_bounds(
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    bounds,
) -> tuple[float, float]:
    if bounds is None:
        return x_mm, y_mm
    half_w = width_mm / 2
    half_h = height_mm / 2
    return (
        max(bounds.x_min + half_w, min(bounds.x_max - half_w, x_mm)),
        max(bounds.y_min + half_h, min(bounds.y_max - half_h, y_mm)),
    )


def _locked_position_refs(constraints: LayoutConstraints | None, circuit=None) -> set[str]:
    if constraints is None:
        return set()
    locked = {fixed.ref for fixed in constraints.fixed or []}
    locked.update(anchor.ref for anchor in constraints.edge_anchors or [])
    part_by_ref = {
        getattr(part, "ref", ""): part
        for part in (getattr(circuit, "parts", []) or [])
    }
    for constraint in constraints.align or []:
        locked.update(
            ref
            for ref in (constraint.refs or [])
            if ref in part_by_ref and is_ui_grid_part(part_by_ref[ref])
        )
    for constraint in constraints.distribute or []:
        locked.update(
            ref
            for ref in (constraint.refs or [])
            if ref in part_by_ref and is_ui_grid_part(part_by_ref[ref])
        )
    return locked


def _locked_rotation_refs(constraints: LayoutConstraints | None) -> set[str]:
    if constraints is None:
        return set()
    locked = _locked_position_refs(constraints)
    locked.update(face.ref for face in constraints.face_edges or [])
    return locked


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


def _part_net_names(part) -> set[str]:
    return set(_part_pin_nets_by_number(part).values())


def _is_supply_or_ground_net(net_name: str) -> bool:
    return bool(POWER_NET_RE.match(net_name) or GND_NET_RE.match(net_name))


def _role_name_for_ref(ref: str, roles: dict) -> str:
    role = roles.get(ref)
    return role.role if role is not None else "unknown"


def _role_weight(role_name: str) -> float:
    return {
        "regulator": 6.0,
        "module_socket": 5.5,
        "ic": 5.0,
        "connector": 2.2,
        "panel_jack": 2.0,
        "control": 1.8,
    }.get(role_name, 1.0)


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
        "SENSOR",
        "SMD",
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
    tokens: set[str] = set()
    for field_name in ("ref", "name", "value", "footprint"):
        tokens.update(_alpha_tokens(str(getattr(part, field_name, "") or "")))
    return tokens


def _token_affinity(passive_part, owner_part) -> int:
    passive_tokens = _part_tokens(passive_part)
    owner_tokens = _part_tokens(owner_part)
    if not passive_tokens or not owner_tokens:
        return 0
    best = 0
    for passive_token in passive_tokens:
        for owner_token in owner_tokens:
            if passive_token == owner_token:
                best = max(best, 3)
            elif passive_token in owner_token or owner_token in passive_token:
                best = max(best, 2)
    return best


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
    return [
        pad
        for pad in geometry.pads
        if _pad_net_name(pad, pin_nets) == net_name
    ]


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


def _ref_center_from_geometry(
    placed: PlacedPart,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[float, float]:
    geometry = (fp_geometries or {}).get(placed.footprint)
    if geometry is None:
        return placed.x_mm, placed.y_mm
    x_min, y_min, x_max, y_max = geometry.transformed_bounds(placed)
    return (x_min + x_max) / 2, (y_min + y_max) / 2


def _target_pad_candidates_for_net(
    net_name: str,
    skip_ref: str,
    origin_xy: tuple[float, float],
    circuit,
    placed_by_ref: dict[str, PlacedPart],
    fp_geometries: dict[str, FootprintGeometry],
    roles: dict,
) -> list[_TargetPadCandidate]:
    candidates: list[_TargetPadCandidate] = []
    for part in getattr(circuit, "parts", []) or []:
        ref = getattr(part, "ref", None)
        if not ref or ref == skip_ref or ref not in placed_by_ref:
            continue
        role = roles.get(ref)
        role_name = role.role if role is not None else "unknown"
        if role_name in {"decoupling_cap", "signal_passive"}:
            continue
        placed = placed_by_ref[ref]
        geometry = fp_geometries.get(placed.footprint)
        if geometry is None:
            continue
        pads = _pads_for_net(part, geometry, net_name)
        if not pads:
            continue
        role_weight = {
            "ic": 5.0,
            "regulator": 5.0,
            "module_socket": 4.0,
            "connector": 2.2,
            "control": 2.0,
            "panel_jack": 2.0,
        }.get(role_name, 1.0)
        center = _ref_center_from_geometry(placed, fp_geometries)
        for pad in pads:
            pad_xy = _pad_world_xy(pad, placed)
            origin_distance = math.hypot(
                pad_xy[0] - origin_xy[0],
                pad_xy[1] - origin_xy[1],
            )
            distance_from_center = math.hypot(
                pad_xy[0] - center[0],
                pad_xy[1] - center[1],
            )
            candidates.append(
                _TargetPadCandidate(
                    role_sort=-role_weight,
                    origin_distance=origin_distance,
                    pad_xy=pad_xy,
                    reason=(
                        f"{ref}.{pad.number} on {net_name} "
                        f"({role_name}, pad offset {distance_from_center:.1f}mm)"
                    ),
                    ref=ref,
                    center_xy=center,
                )
            )
    return sorted(
        candidates,
        key=lambda item: (item.role_sort, item.origin_distance, item.reason),
    )


def _weighted_pad_points_for_owner(
    owner_part,
    owner_placed: PlacedPart,
    owner_geometry: FootprintGeometry | None,
    target_nets: list[str],
    signal_nets: set[str],
) -> tuple[tuple[float, float], ...]:
    if owner_geometry is None:
        return ()
    points: list[tuple[float, float]] = []
    for net_name in target_nets:
        pads = _pads_for_net(owner_part, owner_geometry, net_name)
        repeat = 4 if net_name in signal_nets else 1
        for pad in pads:
            points.extend([_pad_world_xy(pad, owner_placed)] * repeat)
    return tuple(points)


def _passive_owner_candidates(
    ref: str,
    placed_by_ref: dict[str, PlacedPart],
    circuit,
    roles: dict,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> list[_PassiveOwnerCandidate]:
    part_by_ref = {getattr(part, "ref", None): part for part in circuit.parts}
    part = part_by_ref.get(ref)
    placed = placed_by_ref.get(ref)
    if part is None or placed is None:
        return []

    role_name = _role_name_for_ref(ref, roles)
    passive_nets = _part_net_names(part)
    signal_nets = {
        net_name
        for net_name in passive_nets
        if not _is_supply_or_ground_net(net_name)
    }
    is_decap = role_name == "decoupling_cap"
    candidates: list[_PassiveOwnerCandidate] = []
    for other_ref, other_part in part_by_ref.items():
        if other_ref == ref or other_ref not in placed_by_ref:
            continue
        other_role_name = _role_name_for_ref(other_ref, roles)
        if other_role_name not in _PRIMARY_OWNER_ROLES | _FALLBACK_OWNER_ROLES:
            continue

        other_nets = _part_net_names(other_part)
        shared = sorted(passive_nets & other_nets)
        if not shared:
            continue
        shared_signal = tuple(
            net_name
            for net_name in shared
            if not _is_supply_or_ground_net(net_name)
        )
        shared_supply = tuple(
            net_name for net_name in shared if _is_supply_or_ground_net(net_name)
        )
        if is_decap:
            if other_role_name not in _PRIMARY_OWNER_ROLES:
                continue
            if not any(POWER_NET_RE.match(net_name) for net_name in shared_supply):
                continue
            if not any(GND_NET_RE.match(net_name) for net_name in shared_supply):
                continue
            target_nets = list(shared_supply)
        else:
            if signal_nets and not shared_signal:
                continue
            target_nets = list(shared_signal or shared)

        owner_placed = placed_by_ref[other_ref]
        owner_geometry = (fp_geometries or {}).get(owner_placed.footprint)
        target_points = _weighted_pad_points_for_owner(
            other_part,
            owner_placed,
            owner_geometry,
            target_nets,
            signal_nets,
        )
        center_xy = _ref_center_from_geometry(owner_placed, fp_geometries)
        candidates.append(
            _PassiveOwnerCandidate(
                ref=other_ref,
                role_name=other_role_name,
                shared_signal_nets=shared_signal,
                shared_supply_nets=shared_supply,
                target_nets=tuple(target_nets),
                target_points=target_points,
                center_xy=center_xy,
                distance=math.hypot(
                    placed.x_mm - owner_placed.x_mm,
                    placed.y_mm - owner_placed.y_mm,
                ),
                affinity=_token_affinity(part, other_part),
            )
        )

    primary = [
        candidate
        for candidate in candidates
        if candidate.role_name in _PRIMARY_OWNER_ROLES
    ]
    return primary or candidates


def _select_passive_owner_candidate(
    ref: str,
    placed_by_ref: dict[str, PlacedPart],
    circuit,
    roles: dict,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> _PassiveOwnerCandidate | None:
    candidates = _passive_owner_candidates(
        ref,
        placed_by_ref,
        circuit,
        roles,
        fp_geometries,
    )
    if not candidates:
        return None
    role_name = _role_name_for_ref(ref, roles)
    if role_name == "decoupling_cap":
        return min(
            candidates,
            key=lambda candidate: (
                -candidate.affinity,
                candidate.distance,
                -_role_weight(candidate.role_name),
                candidate.ref,
            ),
        )
    return min(
        candidates,
        key=lambda candidate: (
            -candidate.affinity,
            -len(candidate.shared_signal_nets),
            -_role_weight(candidate.role_name),
            candidate.distance,
            candidate.ref,
        ),
    )


def _passive_owner_gravity_target(
    ref: str,
    placed_by_ref: dict[str, PlacedPart],
    circuit,
    roles: dict,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> _PassiveGravityTarget | None:
    owner = _select_passive_owner_candidate(
        ref,
        placed_by_ref,
        circuit,
        roles,
        fp_geometries,
    )
    if owner is None:
        return None
    target_points = owner.target_points or (owner.center_xy,)
    target_xy = (
        sum(point[0] for point in target_points) / len(target_points),
        sum(point[1] for point in target_points) / len(target_points),
    )
    signal_text = ", ".join(owner.shared_signal_nets)
    supply_text = ", ".join(owner.shared_supply_nets)
    if signal_text:
        net_text = signal_text
    else:
        net_text = supply_text
    affinity_text = (
        f", name affinity {owner.affinity}"
        if owner.affinity
        else ""
    )
    reason = (
        f"{owner.ref} {owner.role_name} owner on {net_text} "
        f"net(s){affinity_text}"
    )
    if owner.target_points:
        reason += " using owner pad geometry"
    else:
        reason += "; no owner pad geometry available"
    return _PassiveGravityTarget(
        xy=target_xy,
        reason=reason,
        parent_ref=owner.ref,
        parent_center=owner.center_xy,
        side=_target_side(owner.center_xy, target_xy),
    )


def _passive_center_gravity_target(
    ref: str,
    placed_by_ref: dict[str, PlacedPart],
    circuit,
    roles: dict,
) -> _PassiveGravityTarget | None:
    """Fallback passive gravity when footprint pad geometry is unavailable."""

    part_by_ref = {getattr(part, "ref", None): part for part in circuit.parts}
    part = part_by_ref.get(ref)
    placed = placed_by_ref.get(ref)
    if part is None or placed is None:
        return None

    nets = set(_part_pin_nets_by_number(part).values())
    if not nets:
        return None

    parent_roles = {"ic", "regulator", "module_socket"}
    candidates: list[tuple[float, float, str, tuple[float, float], list[str]]] = []
    for other_ref, other_part in part_by_ref.items():
        if other_ref == ref or other_ref not in placed_by_ref:
            continue
        role = roles.get(other_ref)
        role_name = role.role if role is not None else "unknown"
        if role_name not in parent_roles:
            continue
        other_nets = set(_part_pin_nets_by_number(other_part).values())
        shared = sorted(nets & other_nets)
        if not shared:
            continue
        signal_shared = [
            name
            for name in shared
            if not POWER_NET_RE.match(name) and not GND_NET_RE.match(name)
        ]
        if signal_shared:
            net_score = 4.0 + len(signal_shared)
        else:
            net_score = 0.6 * len(shared)
        if net_score <= 0.0:
            continue
        parent = placed_by_ref[other_ref]
        parent_center = (parent.x_mm, parent.y_mm)
        distance = math.hypot(placed.x_mm - parent.x_mm, placed.y_mm - parent.y_mm)
        role_weight = {"ic": 5.0, "regulator": 5.0, "module_socket": 4.5}[role_name]
        candidates.append(
            (
                -(role_weight + net_score),
                distance,
                other_ref,
                parent_center,
                signal_shared or shared,
            )
        )

    if not candidates:
        return None
    _sort, _distance, parent_ref, parent_center, shared = min(candidates)
    target_xy = parent_center
    return _PassiveGravityTarget(
        xy=target_xy,
        reason=(
            f"{parent_ref} center on shared "
            f"{', '.join(shared[:3])} net(s); no pad geometry available"
        ),
        parent_ref=parent_ref,
        parent_center=parent_center,
        side=_target_side(parent_center, target_xy),
    )


def _target_side(
    parent_center: tuple[float, float],
    target_xy: tuple[float, float],
) -> str:
    dx = target_xy[0] - parent_center[0]
    dy = target_xy[1] - parent_center[1]
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "bottom" if dy >= 0 else "top"


def _slot_tangent(
    parent_center: tuple[float, float],
    target_xy: tuple[float, float],
) -> tuple[float, float]:
    dx = target_xy[0] - parent_center[0]
    dy = target_xy[1] - parent_center[1]
    distance = math.hypot(dx, dy)
    if distance <= 1e-6:
        return 0.0, 1.0
    return -dy / distance, dx / distance


def _passive_pin_gravity_target(
    ref: str,
    placed_by_ref: dict[str, PlacedPart],
    circuit,
    fp_geometries: dict[str, FootprintGeometry] | None,
    roles: dict,
    constraints: LayoutConstraints | None = None,
) -> _PassiveGravityTarget | None:
    if circuit is None:
        return None
    part_by_ref = {getattr(part, "ref", None): part for part in circuit.parts}
    part = part_by_ref.get(ref)
    placed = placed_by_ref.get(ref)
    if part is None or placed is None:
        return None
    role = roles.get(ref)
    role_name = role.role if role is not None else "unknown"
    if role_name not in {
        "decoupling_cap",
        "signal_passive",
        "diode",
        "inductor",
        "crystal",
    }:
        return None

    near_target: _PassiveGravityTarget | None = None
    for constraint in (constraints.near if constraints is not None else []) or []:
        if constraint.ref != ref or constraint.target_ref not in placed_by_ref:
            continue
        target = placed_by_ref[constraint.target_ref]
        near_target = _PassiveGravityTarget(
            xy=(target.x_mm, target.y_mm),
            reason=(
                f"near constraint to {constraint.target_ref} "
                f"within {constraint.distance_mm:.1f}mm"
            ),
            parent_ref=constraint.target_ref,
            parent_center=(target.x_mm, target.y_mm),
        )
        if (
            not fp_geometries
            or _role_name_for_ref(constraint.target_ref, roles)
            in _PRIMARY_OWNER_ROLES
        ):
            return near_target
        break

    owner_target = _passive_owner_gravity_target(
        ref,
        placed_by_ref,
        circuit,
        roles,
        fp_geometries,
    )
    if owner_target is not None:
        return owner_target
    if near_target is not None:
        return near_target

    if not fp_geometries:
        return _passive_center_gravity_target(ref, placed_by_ref, circuit, roles)
    geometry = fp_geometries.get(placed.footprint)
    if geometry is None or not geometry.pads:
        return _passive_center_gravity_target(ref, placed_by_ref, circuit, roles)

    target_points: list[tuple[float, float]] = []
    reasons: list[str] = []
    parent_votes: dict[str, float] = {}
    parent_centers: dict[str, tuple[float, float]] = {}
    for net_name in sorted(set(_part_pin_nets_by_number(part).values())):
        if GND_NET_RE.match(net_name) or POWER_NET_RE.match(net_name):
            # Rails are useful context but terrible global attractors. Let the
            # signal-side pad decide where the passive belongs unless this is
            # the only information available.
            net_weight = 0.35
        else:
            net_weight = 1.0
        candidates = _target_pad_candidates_for_net(
            net_name,
            ref,
            (placed.x_mm, placed.y_mm),
            circuit,
            placed_by_ref,
            fp_geometries,
            roles,
        )
        if not candidates:
            continue
        candidate = candidates[0]
        repeat = max(1, round(net_weight * 4))
        target_points.extend([candidate.pad_xy] * repeat)
        reasons.append(candidate.reason)
        parent_votes[candidate.ref] = parent_votes.get(candidate.ref, 0.0) + repeat
        parent_centers[candidate.ref] = candidate.center_xy

    if not target_points:
        return _passive_center_gravity_target(ref, placed_by_ref, circuit, roles)
    target_xy = (
        sum(point[0] for point in target_points) / len(target_points),
        sum(point[1] for point in target_points) / len(target_points),
    )
    parent_ref = None
    parent_center = None
    side = None
    if parent_votes:
        parent_ref = min(
            parent_votes,
            key=lambda item: (-parent_votes[item], item),
        )
        parent_center = parent_centers.get(parent_ref)
        if parent_center is not None:
            side = _target_side(parent_center, target_xy)
    return _PassiveGravityTarget(
        xy=target_xy,
        reason="; ".join(reasons[:3]),
        parent_ref=parent_ref,
        parent_center=parent_center,
        side=side,
    )


def _composed_passive_pin_gravity_targets(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    roles: dict,
    constraints: LayoutConstraints | None,
    clearance_mm: float,
) -> dict[str, _PassiveGravityTarget]:
    placed_by_ref = {part.ref: part for part in placed_parts}
    targets: dict[str, _PassiveGravityTarget] = {}
    grouped: dict[tuple[str, str], list[tuple[str, _PassiveGravityTarget]]] = {}
    for part in sorted(placed_parts, key=lambda item: item.ref):
        target = _passive_pin_gravity_target(
            part.ref,
            placed_by_ref,
            circuit,
            fp_geometries,
            roles,
            constraints,
        )
        if target is None:
            continue
        targets[part.ref] = target
        if target.parent_ref is not None and target.side is not None:
            grouped.setdefault((target.parent_ref, target.side), []).append(
                (part.ref, target)
            )

    for (parent_ref, side), entries in grouped.items():
        if len(entries) < 2:
            continue
        parent_center = entries[0][1].parent_center
        if parent_center is None:
            continue
        tangent = _slot_tangent(parent_center, entries[0][1].xy)
        axis_index = 0 if abs(tangent[0]) >= abs(tangent[1]) else 1
        entries.sort(key=lambda item: (item[1].xy[axis_index], item[0]))
        spacing = max(1.8, clearance_mm + 0.6)
        for ref, _target in entries:
            placed = placed_by_ref.get(ref)
            if placed is None:
                continue
            width_mm, height_mm = _part_dimensions(placed, fp_bboxes, fp_geometries)
            spacing = max(
                spacing,
                min(max(width_mm, height_mm) + clearance_mm + 0.8, 5.0),
            )
        mid = (len(entries) - 1) / 2
        for index, (ref, target) in enumerate(entries):
            offset = (index - mid) * spacing
            slot_xy = (
                target.xy[0] + tangent[0] * offset,
                target.xy[1] + tangent[1] * offset,
            )
            targets[ref] = _PassiveGravityTarget(
                xy=slot_xy,
                reason=(
                    f"{target.reason}; composed passive group slot "
                    f"{index + 1}/{len(entries)} around {parent_ref} {side} side"
                ),
                parent_ref=target.parent_ref,
                parent_center=target.parent_center,
                side=target.side,
            )
    return targets


def _net_weight(name: str) -> float:
    if GND_NET_RE.match(name):
        return 2.0
    if POWER_NET_RE.match(name):
        return 1.7
    upper = name.upper()
    if any(token in upper for token in ("USB", "D+", "D-", "CLK", "XTAL")):
        return 1.5
    return 1.0


def _ref_neighbors(circuit, placed_by_ref: dict[str, PlacedPart]):
    neighbors: dict[str, list[tuple[str, float]]] = {}
    degrees: dict[str, int] = {}
    if circuit is None:
        return neighbors, degrees

    for net in circuit.get_nets():
        if is_nc_net(net):
            continue
        refs: list[str] = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref in placed_by_ref and ref not in refs:
                refs.append(ref)
        if len(refs) < 2:
            continue
        weight = _net_weight(str(getattr(net, "name", "") or ""))
        for ref in refs:
            others = [other for other in refs if other != ref]
            degrees[ref] = degrees.get(ref, 0) + len(others)
            for other in others:
                neighbors.setdefault(ref, []).append((other, weight))
    return neighbors, degrees


def _neighbor_centroid(
    ref: str,
    neighbors: dict[str, list[tuple[str, float]]],
    placed_by_ref: dict[str, PlacedPart],
) -> tuple[float, float] | None:
    weighted = neighbors.get(ref, [])
    if not weighted:
        return None
    total = 0.0
    x_sum = 0.0
    y_sum = 0.0
    for other_ref, weight in weighted:
        other = placed_by_ref.get(other_ref)
        if other is None:
            continue
        total += weight
        x_sum += other.x_mm * weight
        y_sum += other.y_mm * weight
    if total <= 0:
        return None
    return x_sum / total, y_sum / total


def _move_trials(
    placed: PlacedPart,
    centroid: tuple[float, float],
    width_mm: float,
    height_mm: float,
    bounds,
) -> list[PlacedPart]:
    dx = centroid[0] - placed.x_mm
    dy = centroid[1] - placed.y_mm
    distance = math.hypot(dx, dy)
    if distance <= 1e-6:
        return []

    unit_x = dx / distance
    unit_y = dy / distance
    sign_x = 1.0 if dx > 0 else -1.0
    sign_y = 1.0 if dy > 0 else -1.0
    trials: list[PlacedPart] = []
    seen: set[tuple[float, float]] = set()

    directions = [(unit_x, unit_y)]
    if abs(dx) > 1e-6:
        directions.append((sign_x, 0.0))
    if abs(dy) > 1e-6:
        directions.append((0.0, sign_y))

    for step_mm in (6.0, 3.0, 1.0):
        for dir_x, dir_y in directions:
            step = min(step_mm, distance)
            x_mm = placed.x_mm + dir_x * step
            y_mm = placed.y_mm + dir_y * step
            x_mm, y_mm = _clamp_to_bounds(
                x_mm,
                y_mm,
                width_mm,
                height_mm,
                bounds,
            )
            key = (round(x_mm, 4), round(y_mm, 4))
            if key in seen or key == (round(placed.x_mm, 4), round(placed.y_mm, 4)):
                continue
            seen.add(key)
            trials.append(replace(placed, x_mm=x_mm, y_mm=y_mm))
    return trials


def _targeted_clear_move_trials(
    placed_parts: list[PlacedPart],
    placed: PlacedPart,
    target: tuple[float, float],
    width_mm: float,
    height_mm: float,
    bounds,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    constraints: LayoutConstraints | None,
) -> list[PlacedPart]:
    """Return direct clear-position trials around a pin-gravity target."""
    occupied = _occupied_without_ref(
        placed_parts,
        placed.ref,
        fp_bboxes,
        fp_geometries,
        constraints,
    )
    target_x, target_y = _clamp_to_bounds(
        target[0],
        target[1],
        width_mm,
        height_mm,
        bounds,
    )
    x_mm, y_mm = _find_clear_position(
        target_x,
        target_y,
        width_mm,
        height_mm,
        occupied,
        bounds=bounds,
        step=0.5,
        max_radius=18.0,
    )
    x_mm, y_mm = _clamp_to_bounds(x_mm, y_mm, width_mm, height_mm, bounds)

    trials: list[PlacedPart] = []
    seen = {(round(placed.x_mm, 4), round(placed.y_mm, 4))}
    for x, y in (
        (x_mm, y_mm),
        ((placed.x_mm + x_mm) / 2, (placed.y_mm + y_mm) / 2),
        ((target_x + x_mm) / 2, (target_y + y_mm) / 2),
    ):
        key = (round(x, 4), round(y, 4))
        if key in seen:
            continue
        seen.add(key)
        trials.append(replace(placed, x_mm=x, y_mm=y))
    directions = [
        (-1.0, 0.0),
        (1.0, 0.0),
        (0.0, -1.0),
        (0.0, 1.0),
    ]
    if math.hypot(placed.x_mm - target_x, placed.y_mm - target_y) > 18.0:
        radii = (2.0, 4.0, 6.0, 9.0, 12.0, 16.0)
    else:
        radii = (1.5, 3.0, 5.0, 8.0, 12.0)
    for radius in radii:
        for dir_x, dir_y in directions:
            x = target_x + dir_x * radius
            y = target_y + dir_y * radius
            x, y = _clamp_to_bounds(x, y, width_mm, height_mm, bounds)
            key = (round(x, 4), round(y, 4))
            if key in seen:
                continue
            seen.add(key)
            trials.append(replace(placed, x_mm=x, y_mm=y))
    return trials


def _rotation_trials(placed: PlacedPart) -> list[PlacedPart]:
    trials = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        if abs(rotation - placed.rot_deg) <= 1e-6:
            continue
        trials.append(replace(placed, rot_deg=rotation))
    return trials


# Two-tier trials: when a single ref generates more than this many candidate
# positions, cheaply rank them (this-ref overlap count, then touching-net HPWL)
# and full-score only the best RANK_LIMIT. Acceptance is still decided by
# _is_better on the full score — only *which* trials get full-scored changes.
RANK_LIMIT = 3


def _rank_trial(
    trial: PlacedPart,
    ref: str,
    other_boxes: list[tuple[float, float, float, float]],
    touching_nets: list[tuple[str, list[str]]],
    pos_base: dict[str, tuple[float, float]],
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[int, float]:
    """Cheap O(neighbors) proxy for a trial's quality: (this-ref overlap count,
    HPWL over the nets that touch this ref). Lower is better."""
    w, h = _part_dimensions(trial, fp_bboxes, fp_geometries)
    tx0 = trial.x_mm - w / 2
    ty0 = trial.y_mm - h / 2
    tx1 = trial.x_mm + w / 2
    ty1 = trial.y_mm + h / 2
    overlaps = 0
    for ox0, oy0, ox1, oy1 in other_boxes:
        if tx0 < ox1 and ox0 < tx1 and ty0 < oy1 and oy0 < ty1:
            overlaps += 1

    hpwl = 0.0
    tx, ty = trial.x_mm, trial.y_mm
    for _name, refs in touching_nets:
        xs, ys = [], []
        for r in refs:
            if r == ref:
                xs.append(tx)
                ys.append(ty)
            else:
                pos = pos_base.get(r)
                if pos is not None:
                    xs.append(pos[0])
                    ys.append(pos[1])
        if len(xs) >= 2:
            hpwl += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return overlaps, hpwl


def _rank_and_limit_trials(
    placed_parts: list[PlacedPart],
    ref: str,
    trials: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    ctx,
) -> list[PlacedPart]:
    """Keep the RANK_LIMIT cheapest-ranked trials, stable by original index."""
    pos_base = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed_parts}
    other_boxes: list[tuple[float, float, float, float]] = []
    for pp in placed_parts:
        if pp.ref == ref:
            continue
        w, h = _part_dimensions(pp, fp_bboxes, fp_geometries)
        other_boxes.append(
            (pp.x_mm - w / 2, pp.y_mm - h / 2, pp.x_mm + w / 2, pp.y_mm + h / 2)
        )
    touching_nets = [
        (name, refs)
        for name, refs in _net_ref_lists(circuit, ctx)
        if ref in refs
    ]
    ranked = sorted(
        enumerate(trials),
        key=lambda item: (
            _rank_trial(
                item[1],
                ref,
                other_boxes,
                touching_nets,
                pos_base,
                fp_bboxes,
                fp_geometries,
            ),
            item[0],
        ),
    )
    return [trial for _idx, trial in ranked[:RANK_LIMIT]]


def _best_single_ref_trial(
    placed_parts: list[PlacedPart],
    current_score: LayoutScore,
    ref: str,
    trials: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
    clearance_mm: float,
    board_layers: int,
    ctx=None,
) -> tuple[list[PlacedPart], LayoutScore, PlacedPart] | None:
    if len(trials) > RANK_LIMIT:
        trials = _rank_and_limit_trials(
            placed_parts,
            ref,
            trials,
            circuit,
            fp_bboxes,
            fp_geometries,
            ctx,
        )
    best_parts = None
    best_score = current_score
    best_trial = None
    for trial in trials:
        trial_parts = _replace_ref(placed_parts, ref, trial)
        trial_score = _score(
            trial_parts,
            circuit,
            fp_bboxes,
            constraints,
            fp_geometries,
            clearance_mm,
            board_layers,
            ctx,
        )
        if _is_better(best_score, trial_score):
            best_parts = trial_parts
            best_score = trial_score
            best_trial = trial
    if best_parts is None or best_trial is None:
        return None
    return best_parts, best_score, best_trial


def _hard_violation_key(score: LayoutScore) -> tuple[int, int, int, int]:
    return (
        score.overlap_count,
        score.outline_violation_count,
        score.keepout_violation_count,
        score.missing_count,
    )


def _best_pin_gravity_trial(
    placed_parts: list[PlacedPart],
    current_score: LayoutScore,
    ref: str,
    placed: PlacedPart,
    target_xy: tuple[float, float],
    trials: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
    clearance_mm: float,
    board_layers: int,
    ctx=None,
) -> tuple[list[PlacedPart], LayoutScore, PlacedPart] | None:
    current_distance = math.hypot(
        placed.x_mm - target_xy[0],
        placed.y_mm - target_xy[1],
    )
    current_hard = _hard_violation_key(current_score)
    best: tuple[tuple[float, float], list[PlacedPart], LayoutScore, PlacedPart] | None = None
    ranked_trials = sorted(
        trials,
        key=lambda trial: (
            math.hypot(trial.x_mm - target_xy[0], trial.y_mm - target_xy[1]),
            trial.x_mm,
            trial.y_mm,
            trial.rot_deg,
        ),
    )
    scored_candidates = 0
    checked_candidates = 0
    for trial in ranked_trials:
        target_distance = math.hypot(
            trial.x_mm - target_xy[0],
            trial.y_mm - target_xy[1],
        )
        if target_distance >= current_distance - 0.25:
            continue
        if best is not None and checked_candidates >= 24:
            break
        checked_candidates += 1
        trial_parts = _replace_ref(placed_parts, ref, trial)
        if not _ref_is_clear_of_hard_violations(
            ref,
            trial_parts,
            circuit,
            fp_bboxes,
            constraints,
            fp_geometries,
            clearance_mm,
        ):
            continue
        if not _ref_is_clear_of_drc_courtyards(
            ref,
            trial_parts,
            fp_bboxes,
            constraints,
            fp_geometries,
        ):
            continue
        trial_score = _score(
            trial_parts,
            circuit,
            fp_bboxes,
            constraints,
            fp_geometries,
            clearance_mm,
            board_layers,
            ctx,
        )
        scored_candidates += 1
        if _hard_violation_key(trial_score) > current_hard:
            continue
        key = (target_distance, trial_score.penalty)
        if best is None or key < best[0]:
            best = (key, trial_parts, trial_score, trial)
        if best is not None and scored_candidates >= 3:
            break
    if best is None:
        return None
    _, best_parts, best_score, best_trial = best
    return best_parts, best_score, best_trial


def _ref_is_clear_of_hard_violations(
    ref: str,
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
    clearance_mm: float,
) -> bool:
    validation = validate(
        placed_parts,
        circuit,
        fp_bboxes,
        clearance_mm=clearance_mm,
        outline=constraints.outline if constraints is not None else None,
        keepouts=constraints.keepouts if constraints is not None else None,
        fp_geometries=fp_geometries,
    )
    if any(ref in pair for pair in validation.overlaps):
        return False
    if ref in validation.outline_violations:
        return False
    if ref in validation.keepout_violations:
        return False
    if ref in validation.missing_refs:
        return False
    return True


def _ref_is_clear_of_drc_courtyards(
    ref: str,
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> bool:
    placed = next((part for part in placed_parts if part.ref == ref), None)
    if placed is None:
        return True
    width_mm, height_mm = _part_dimensions(placed, fp_bboxes, fp_geometries)
    occupied = _occupied_without_ref(
        placed_parts,
        ref,
        fp_bboxes,
        fp_geometries,
        constraints,
    )
    return not _overlaps_any(
        placed.x_mm,
        placed.y_mm,
        width_mm,
        height_mm,
        occupied,
        clearance=0.0,
    )


def _occupied_without_ref(
    placed_parts: list[PlacedPart],
    ref: str,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    constraints: LayoutConstraints | None,
) -> list[tuple[float, float, float, float]]:
    occupied: list[tuple[float, float, float, float]] = []
    if constraints is not None:
        for keepout in constraints.keepouts or []:
            if ref in (getattr(keepout, "allowed_refs", []) or []):
                continue
            occupied.append(
                (
                    (keepout.x_min + keepout.x_max) / 2,
                    (keepout.y_min + keepout.y_max) / 2,
                    keepout.x_max - keepout.x_min,
                    keepout.y_max - keepout.y_min,
                )
            )
    subject = next((part for part in placed_parts if part.ref == ref), None)
    for part in placed_parts:
        if part.ref == ref:
            continue
        if subject is not None and not _same_physical_side(subject, part):
            subject_geometry = (fp_geometries or {}).get(subject.footprint)
            part_geometry = (fp_geometries or {}).get(part.footprint)
            if (
                subject_geometry is not None
                and part_geometry is not None
                and not _through_board_pads_collide(
                    subject,
                    subject_geometry,
                    part,
                    part_geometry,
                    0.0,
                )
            ):
                continue
        width_mm, height_mm = _part_dimensions(part, fp_bboxes, fp_geometries)
        occupied.append((part.x_mm, part.y_mm, width_mm, height_mm))
    return occupied


def _clearance_search_radius(bounds) -> float:
    if bounds is None:
        return 160.0
    return max(80.0, bounds.x_max - bounds.x_min, bounds.y_max - bounds.y_min)


def _legalize_one_overlap(
    placed_parts: list[PlacedPart],
    current_score: LayoutScore,
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
    clearance_mm: float,
    board_layers: int,
    position_locked: set[str],
    degrees: dict[str, int],
    ctx=None,
) -> tuple[list[PlacedPart], LayoutScore, str, str] | None:
    validation = validate(
        placed_parts,
        circuit,
        fp_bboxes,
        clearance_mm=clearance_mm,
        outline=constraints.outline if constraints is not None else None,
        keepouts=constraints.keepouts if constraints is not None else None,
        cutouts=constraints.cutouts if constraints is not None else None,
        fp_geometries=fp_geometries,
    )
    if not validation.overlaps:
        return None

    placed_by_ref = {part.ref: part for part in placed_parts}
    for ref_a, ref_b in validation.overlaps:
        candidates = [
            ref
            for ref in (ref_a, ref_b)
            if ref in placed_by_ref and ref not in position_locked
        ]
        candidates.sort(key=lambda ref: (degrees.get(ref, 0), ref))
        for ref in candidates:
            placed = placed_by_ref[ref]
            width_mm, height_mm = _part_dimensions(
                placed,
                fp_bboxes,
                fp_geometries,
            )
            bounds = _bounds_for_ref(ref, constraints)
            x_mm, y_mm = _find_clear_position(
                placed.x_mm,
                placed.y_mm,
                width_mm,
                height_mm,
                _occupied_without_ref(
                    placed_parts,
                    ref,
                    fp_bboxes,
                    fp_geometries,
                    constraints,
                ),
                bounds=bounds,
                step=1.0,
                max_radius=_clearance_search_radius(bounds),
            )
            x_mm, y_mm = _clamp_to_bounds(
                x_mm,
                y_mm,
                width_mm,
                height_mm,
                bounds,
            )
            if (
                abs(x_mm - placed.x_mm) <= 1e-6
                and abs(y_mm - placed.y_mm) <= 1e-6
            ):
                continue
            trial = replace(placed, x_mm=x_mm, y_mm=y_mm)
            trial_parts = _replace_ref(placed_parts, ref, trial)
            trial_score = _score(
                trial_parts,
                circuit,
                fp_bboxes,
                constraints,
                fp_geometries,
                clearance_mm,
                board_layers,
                ctx,
            )
            if _is_better(current_score, trial_score):
                other = ref_b if ref == ref_a else ref_a
                reason = (
                    f"legalized overlap with {other} by moving to "
                    f"({x_mm:.1f},{y_mm:.1f})"
                )
                return trial_parts, trial_score, ref, reason
    return None


def _same_swap_class(
    a: PlacedPart,
    b: PlacedPart,
    constraints: LayoutConstraints | None,
) -> bool:
    if a.footprint != b.footprint:
        return False
    return _bounds_key(_bounds_for_ref(a.ref, constraints)) == _bounds_key(
        _bounds_for_ref(b.ref, constraints)
    )


def refine_placement(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    constraints: LayoutConstraints | None = None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    clearance_mm: float = 0.5,
    board_layers: int = 2,
    max_passes: int = 2,
    max_movable_refs: int = 32,
    max_pair_swaps: int = 16,
    max_legalization_moves: int = 64,
    preanchored_refs: set[str] | None = None,
    ctx=None,
    progress=None,
) -> RefinementResult:
    """Apply deterministic score-gated local placement adjustments.

    ``progress`` (optional ``callable(str)``) is invoked at per-ref, swap-loop
    and legalization-loop boundaries for observability only. It must never
    influence a decision: with ``progress=None`` (the default) the refinement
    is byte-identical.
    """
    current_parts = _clone_placed(placed_parts)
    current_score = _score(
        current_parts,
        circuit,
        fp_bboxes,
        constraints,
        fp_geometries,
        clearance_mm,
        board_layers,
        ctx,
    )
    start_score = current_score.score
    start_penalty = current_score.penalty
    position_locked = _locked_position_refs(constraints, circuit)
    rotation_locked = _locked_rotation_refs(constraints)
    preanchored_refs = set(preanchored_refs or ())
    rotation_locked.update(preanchored_refs)
    roles = (
        ctx.roles
        if ctx is not None
        else (classify_parts(circuit) if circuit is not None else {})
    )
    accepted_moves = 0
    accepted_rotations = 0
    accepted_swaps = 0
    ref_reasons: dict[str, list[str]] = {}
    pin_gravity_anchored_refs: set[str] = set(preanchored_refs)

    for _pass_idx in range(max_passes):
        changed = False
        placed_by_ref = {part.ref: part for part in current_parts}
        neighbors, degrees = _ref_neighbors(circuit, placed_by_ref)
        pin_targets = _composed_passive_pin_gravity_targets(
            current_parts,
            circuit,
            fp_bboxes,
            fp_geometries,
            roles,
            constraints,
            clearance_mm,
        )
        movable_refs = [
            part.ref for part in current_parts if part.ref not in position_locked
        ]
        movable_refs.sort(key=lambda ref: (-degrees.get(ref, 0), ref))
        movable_refs = movable_refs[:max_movable_refs]

        for _ref_idx, ref in enumerate(movable_refs):
            if progress is not None:
                progress(
                    f"pass {_pass_idx}: ref {_ref_idx + 1}/{len(movable_refs)} {ref}"
                )
            placed_by_ref = {part.ref: part for part in current_parts}
            placed = placed_by_ref[ref]
            width_mm, height_mm = _part_dimensions(
                placed,
                fp_bboxes,
                fp_geometries,
            )
            pin_target = None if ref in preanchored_refs else pin_targets.get(ref)
            if pin_target is not None:
                pin_gravity_anchored_refs.add(ref)
                target_xy = pin_target.xy
                target_reason = pin_target.reason
                bounds = _bounds_for_ref(ref, constraints)
                move_trials = _targeted_clear_move_trials(
                    current_parts,
                    placed,
                    target_xy,
                    width_mm,
                    height_mm,
                    bounds,
                    fp_bboxes,
                    fp_geometries,
                    constraints,
                )
                move_trials.extend(
                    _move_trials(
                        placed,
                        target_xy,
                        width_mm,
                        height_mm,
                        bounds,
                    )
                )
                best = _best_pin_gravity_trial(
                    current_parts,
                    current_score,
                    ref,
                    placed,
                    target_xy,
                    move_trials,
                    circuit,
                    fp_bboxes,
                    constraints,
                    fp_geometries,
                    clearance_mm,
                    board_layers,
                    ctx,
                )
                if best is not None:
                    current_parts, current_score, trial = best
                    accepted_moves += 1
                    changed = True
                    ref_reasons.setdefault(ref, []).append(
                        (
                            "locally moved by passive pin gravity toward "
                            f"({target_xy[0]:.1f},{target_xy[1]:.1f}); "
                            f"{target_reason}"
                        )
                    )
                    placed = trial

            centroid = (
                None
                if ref in pin_gravity_anchored_refs
                else _neighbor_centroid(ref, neighbors, placed_by_ref)
            )
            if centroid is not None:
                move_trials = _move_trials(
                    placed,
                    centroid,
                    width_mm,
                    height_mm,
                    _bounds_for_ref(ref, constraints),
                )
                best = _best_single_ref_trial(
                    current_parts,
                    current_score,
                    ref,
                    move_trials,
                    circuit,
                    fp_bboxes,
                    constraints,
                    fp_geometries,
                    clearance_mm,
                    board_layers,
                    ctx,
                )
                if best is not None:
                    current_parts, current_score, trial = best
                    accepted_moves += 1
                    changed = True
                    ref_reasons.setdefault(ref, []).append(
                        (
                            "locally moved toward connected-net centroid "
                            f"({trial.x_mm:.1f},{trial.y_mm:.1f})"
                        )
                    )
                    placed = trial

            if (
                ref in rotation_locked
                or not fp_geometries
                or placed.footprint not in fp_geometries
            ):
                continue
            best = _best_single_ref_trial(
                current_parts,
                current_score,
                ref,
                _rotation_trials(placed),
                circuit,
                fp_bboxes,
                constraints,
                fp_geometries,
                clearance_mm,
                board_layers,
                ctx,
            )
            if best is not None:
                current_parts, current_score, trial = best
                accepted_rotations += 1
                changed = True
                ref_reasons.setdefault(ref, []).append(
                    f"locally rotated to {trial.rot_deg:.0f} deg after scoring"
                )

        if progress is not None:
            progress(f"pass {_pass_idx}: swap loop ({len(movable_refs)} refs)")
        placed_by_ref = {part.ref: part for part in current_parts}
        swap_attempts = 0
        for idx, ref_a in enumerate(movable_refs):
            if swap_attempts >= max_pair_swaps:
                break
            a = placed_by_ref.get(ref_a)
            if a is None:
                continue
            for ref_b in movable_refs[idx + 1:]:
                if swap_attempts >= max_pair_swaps:
                    break
                b = placed_by_ref.get(ref_b)
                if b is None or not _same_swap_class(a, b, constraints):
                    continue
                if a.ref in pin_gravity_anchored_refs or b.ref in pin_gravity_anchored_refs:
                    continue
                swap_attempts += 1
                trial_a = replace(a, x_mm=b.x_mm, y_mm=b.y_mm)
                trial_b = replace(b, x_mm=a.x_mm, y_mm=a.y_mm)
                trial_parts = _replace_refs(
                    current_parts,
                    {a.ref: trial_a, b.ref: trial_b},
                )
                trial_score = _score(
                    trial_parts,
                    circuit,
                    fp_bboxes,
                    constraints,
                    fp_geometries,
                    clearance_mm,
                    board_layers,
                    ctx,
                )
                if not _is_better(current_score, trial_score):
                    continue
                current_parts = trial_parts
                current_score = trial_score
                placed_by_ref = {part.ref: part for part in current_parts}
                accepted_swaps += 1
                changed = True
                ref_reasons.setdefault(a.ref, []).append(
                    f"locally swapped position with {b.ref}"
                )
                ref_reasons.setdefault(b.ref, []).append(
                    f"locally swapped position with {a.ref}"
                )
                break

        if progress is not None:
            progress(f"pass {_pass_idx}: legalization loop")
        legalizations = 0
        while legalizations < max_legalization_moves:
            placed_by_ref = {part.ref: part for part in current_parts}
            neighbors, degrees = _ref_neighbors(circuit, placed_by_ref)
            legalized = _legalize_one_overlap(
                current_parts,
                current_score,
                circuit,
                fp_bboxes,
                constraints,
                fp_geometries,
                clearance_mm,
                board_layers,
                position_locked,
                degrees,
                ctx,
            )
            if legalized is None:
                break
            current_parts, current_score, moved_ref, reason = legalized
            accepted_moves += 1
            legalizations += 1
            changed = True
            ref_reasons.setdefault(moved_ref, []).append(reason)

        if not changed:
            break

    return RefinementResult(
        placed_parts=current_parts,
        start_score=start_score,
        final_score=current_score.score,
        accepted_moves=accepted_moves,
        accepted_rotations=accepted_rotations,
        accepted_swaps=accepted_swaps,
        ref_reasons=ref_reasons,
        start_penalty=start_penalty,
        final_penalty=current_score.penalty,
    )


def refine_candidate_placement(
    candidate: PlacementCandidate,
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    clearance_mm: float = 0.5,
    board_layers: int = 2,
    ctx=None,
    progress=None,
) -> RefinementResult:
    result = refine_placement(
        candidate.placed_parts,
        circuit,
        fp_bboxes,
        constraints=candidate.constraints,
        fp_geometries=fp_geometries,
        clearance_mm=clearance_mm,
        board_layers=board_layers,
        preanchored_refs=set(candidate.pin_gravity_anchored_refs),
        ctx=ctx,
        progress=progress,
    )
    if result.accepted_count == 0:
        return result

    candidate.placed_parts = result.placed_parts
    candidate.reasons.append(
        (
            f"local refinement accepted {result.accepted_count} "
            f"score-gated adjustment(s): "
            f"{result.start_score:.1f} -> {result.final_score:.1f}"
        )
    )
    for ref, reasons in result.ref_reasons.items():
        candidate.ref_reasons.setdefault(ref, []).extend(reasons)
    return result
