from __future__ import annotations

import math
from dataclasses import dataclass, field

from .candidates import PlacementCandidate
from .connector_metadata import (
    ConnectorMatingFace,
    infer_connector_mating_face,
    infer_edge_mating_rotation,
    rotation_for_local_exit,
)
from .constraints import LayoutConstraints
from .geometry import FootprintGeometry, PadGeometry, transform_point
from .roles import GND_NET_RE, POWER_NET_RE
from .writer import PlacedPart


__all__ = [
    "ConnectorMatingFace",
    "OrientationResult",
    "infer_connector_mating_face",
    "infer_edge_mating_rotation",
    "refine_candidate_orientations",
    "refine_orientations",
    "rotation_for_local_exit",
]


@dataclass
class OrientationResult:
    placed_parts: list[PlacedPart]
    ref_reasons: dict[str, list[str]] = field(default_factory=dict)


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


def _net_refs(circuit) -> dict[str, list[str]]:
    refs_by_net: dict[str, list[str]] = {}
    if circuit is None:
        return refs_by_net
    for net in circuit.get_nets():
        name = str(getattr(net, "name", "") or "")
        if not name:
            continue
        refs = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref and ref not in refs:
                refs.append(ref)
        refs_by_net[name] = refs
    return refs_by_net


def _net_weight(net_name: str) -> float:
    if GND_NET_RE.match(net_name):
        return 2.4
    if POWER_NET_RE.match(net_name):
        return 2.0
    upper = net_name.upper()
    if any(
        token in upper
        for token in ("D+", "D-", "USB", "CLK", "XTAL", "SCL", "SDA")
    ):
        return 1.6
    return 1.0


def _pad_net_name(
    pad: PadGeometry,
    pin_nets: dict[str, str],
) -> str | None:
    return pad.net_name or pin_nets.get(pad.number)


def _rotation_score(
    placed: PlacedPart,
    geometry: FootprintGeometry,
    part,
    rotation: float,
    placed_by_ref: dict[str, PlacedPart],
    refs_by_net: dict[str, list[str]],
) -> float | None:
    pin_nets = _part_pin_nets_by_number(part)
    score = 0.0
    scored_pads = 0

    for pad in geometry.pads:
        net_name = _pad_net_name(pad, pin_nets)
        if not net_name:
            continue
        neighbor_refs = [
            ref
            for ref in refs_by_net.get(net_name, [])
            if ref != placed.ref and ref in placed_by_ref
        ]
        if not neighbor_refs:
            continue
        pad_x, pad_y = transform_point(
            placed.x_mm,
            placed.y_mm,
            rotation,
            pad.x_mm,
            pad.y_mm,
        )
        target_x = sum(placed_by_ref[ref].x_mm for ref in neighbor_refs) / len(
            neighbor_refs
        )
        target_y = sum(placed_by_ref[ref].y_mm for ref in neighbor_refs) / len(
            neighbor_refs
        )
        score += math.hypot(pad_x - target_x, pad_y - target_y) * _net_weight(net_name)
        scored_pads += 1

    if scored_pads == 0:
        return None
    return score


def _locked_rotation_refs(constraints: LayoutConstraints | None) -> set[str]:
    if constraints is None:
        return set()
    locked = {fixed.ref for fixed in constraints.fixed or []}
    locked.update(anchor.ref for anchor in constraints.edge_anchors or [])
    locked.update(face.ref for face in constraints.face_edges or [])
    return locked


def refine_orientations(
    placed_parts: list[PlacedPart],
    circuit,
    fp_geometries: dict[str, FootprintGeometry],
    constraints: LayoutConstraints | None = None,
) -> OrientationResult:
    """Rotate unlocked parts to reduce pad-to-neighbor net pressure."""
    if circuit is None or not fp_geometries:
        return OrientationResult(list(placed_parts), {})

    part_by_ref = {part.ref: part for part in circuit.parts}
    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    refs_by_net = _net_refs(circuit)
    locked_refs = _locked_rotation_refs(constraints)
    ref_reasons: dict[str, list[str]] = {}
    refined: list[PlacedPart] = []

    for placed in placed_parts:
        geometry = fp_geometries.get(placed.footprint)
        part = part_by_ref.get(placed.ref)
        if geometry is None or part is None or placed.ref in locked_refs:
            refined.append(placed)
            continue

        candidates = []
        for rotation in (0.0, 90.0, 180.0, 270.0):
            score = _rotation_score(
                placed,
                geometry,
                part,
                rotation,
                placed_by_ref,
                refs_by_net,
            )
            if score is not None:
                candidates.append((score, abs(rotation - placed.rot_deg), rotation))

        if not candidates:
            refined.append(placed)
            continue

        _, _, best_rotation = min(candidates)
        if best_rotation != placed.rot_deg:
            refined_part = PlacedPart(
                ref=placed.ref,
                x_mm=placed.x_mm,
                y_mm=placed.y_mm,
                rot_deg=best_rotation,
                footprint=placed.footprint,
            )
            refined.append(refined_part)
            ref_reasons.setdefault(placed.ref, []).append(
                f"rotated to {best_rotation:.0f} deg by pad/net pressure"
            )
            placed_by_ref[placed.ref] = refined_part
        else:
            refined.append(placed)

    return OrientationResult(refined, ref_reasons)


def refine_candidate_orientations(
    candidate: PlacementCandidate,
    circuit,
    fp_geometries: dict[str, FootprintGeometry],
) -> None:
    result = refine_orientations(
        candidate.placed_parts,
        circuit,
        fp_geometries,
        constraints=candidate.constraints,
    )
    if not result.ref_reasons:
        return
    candidate.placed_parts = result.placed_parts
    candidate.reasons.append("unlocked rotations refined using pad/net pressure")
    for ref, reasons in result.ref_reasons.items():
        candidate.ref_reasons.setdefault(ref, []).extend(reasons)
