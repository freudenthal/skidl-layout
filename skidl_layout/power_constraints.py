# -*- coding: utf-8 -*-
"""Power-stage plan -> placement constraints (power-layout Phase 3).

The fourth corner of the power quartet:

* :mod:`~skidl_layout.power` recognises power *nets* by name regex.
* :mod:`~skidl_layout.power_roles` turns a **netlist** into a
  :class:`~skidl_layout.power_roles.PowerStagePlan` -- it names the switch, the
  commutation loop, the sense resistor, the feedback divider.
* :mod:`~skidl_layout.power_metrics` turns **(placement, plan)** into numbers --
  loop area, feedback-node length, small-signal separation.
* **this module** turns **(plan, footprint geometry)** into the
  ``Near`` / ``Far`` / ``AnchorZone`` objects ``plan_layout`` already honours, so
  a power board can get a datasheet-shaped floorplan without a human writing one.

**No positions in.** :func:`generate_power_constraints` is a pure function of the
plan, the footprint geometry, and the board outline. It never reads a placed
coordinate -- if it did, the Phase-2 objective that judges its output would be
scoring a placement against constraints derived from that same placement, and
the whole loop would be circular. The board *outline* is admitted because it is
a mechanical given, not a placement.

**Names are never signals.** Everything below reads the plan's structure and the
parts' footprint geometry. Reference designators appear only as dictionary keys
and as the ``ref`` fields of the emitted constraints, so the generated set comes
out identical modulo renaming on a ref- and net-scrambled twin (gate C3).

**Distances are geometry, never flat millimetres.** Phase 2 measured that a
loop containing a 44 mm transformer cannot be held to a 2010 sense resistor's
budget. Every generated ``distance_mm`` is therefore ``r(a) + r(b) + slack``,
where ``r`` is the part's courtyard *half-diagonal* -- its corner-on reach -- so
the constraint is satisfiable by construction and still as tight as the parts
admit. A generated constraint the datasheet floorplan itself violates is a bug
in this module (gate C2).

    plan = classify_power_roles(circuit)
    generated = generate_power_constraints(plan, footprint_by_ref=..., ...)
    constraints.near.extend(generated.near)

**What is deliberately NOT generated**, and why. Every entry was *measured* --
one in Phase 2, the rest here, against the datasheet floorplan:

* Nothing from switch-node *span* -- it orders the Phase-0 corpus exactly
  inverted against the routed copper it is a proxy for (Phase 2).
* No rail-capacitor constraint of any kind -- not bulk-to-loop (Phase 2's
  disabled metric) and not the per-rail replacement this phase's plan proposed
  and this phase then **measured wrong**; see the block comment above
  :func:`_generate_far_small_signal`.
* Not the commutation loop's **closing** edge -- the loop returns through a
  ground plane, not through a component hop, and the datasheet floorplan
  violates that one edge; see :func:`_generate_near_loop`.
* No per-stage :class:`~skidl_layout.constraints.AnchorZone` by default -- a
  zone is a hard clamp, not a hint, and the one this module can compute made the
  candidate lose; ``zones=True`` still builds it. See
  :func:`_generate_zone`.
* No :class:`~skidl_layout.constraints.FixedPosition`, ``EdgeAnchor`` or
  ``AlignConstraint`` -- each encodes a choice (which edge? which order?) that
  the plan simply does not contain.
* Nothing at all on a board whose plan has zero stages. Silence is the contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .constraints import AnchorZone, FarConstraint, NearConstraint

__all__ = [
    "PowerConstraintSet",
    "generate_power_constraints",
]


# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: How movable each device kind is, lowest first. The placer's near pass moves
#: ``constraint.ref`` and never ``constraint.target_ref``, and refinement's
#: gravity only follows a near constraint when ``ref``'s
#: :mod:`~skidl_layout.roles` role is a passive one -- a 3-pin FET classifies as
#: ``"ic"`` and gets no gravity at all. So the *more movable* member of every
#: pair has to be the one named ``ref``, or the constraint is a no-op waiting to
#: happen.
_MOVABILITY: dict[str, int] = {
    "capacitor": 0,
    "input_cap": 0,
    "output_cap": 0,
    "resistor": 0,
    "sense_resistor": 0,
    "fb_divider_top": 0,
    "fb_divider_bottom": 0,
    "rectifier": 0,
    "unknown": 0,
    "magnetics": 1,
    "switch": 2,
    "controller": 3,
}
_DEFAULT_MOVABILITY = 0

#: Base separation for the small-signal ``FarConstraint``, before the switch's
#: own courtyard reach is added. Phase 2 measured the datasheet floorplan's
#: small-signal floor at 16.6 mm centroid-to-centroid on the boost and the
#: default placer's at 14 mm; 10 mm + r(switch) lands in that band for a mid-size
#: switch without over-constraining a small board.
_SMALL_SIGNAL_BASE_MM = 10.0

#: A per-stage zone is sized to this multiple of its members' summed courtyard
#: area -- room to place, not room to sprawl -- and never more than this
#: fraction of the board.
_ZONE_AREA_FACTOR = 3.0
_ZONE_MAX_OUTLINE_FRACTION = 0.55


# --------------------------------------------------------------------------- #
# Result shape
# --------------------------------------------------------------------------- #

@dataclass
class PowerConstraintSet:
    """What :func:`generate_power_constraints` produced.

    ``reasons`` maps a constraint key (``"near RS->M1"``, ``"zone
    power_stage_U1"``) to the provenance lines that justify it, so the report can
    say *why* every generated constraint exists rather than just listing
    coordinates. ``warnings`` records what could **not** be generated -- almost
    always a part whose footprint geometry was unavailable, which is a skip and
    never a flat-millimetre guess.
    """

    near: list = field(default_factory=list)
    far: list = field(default_factory=list)
    zones: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.near or self.far or self.zones)

    def refs(self) -> list:
        """Every ref this set mentions, in generation order."""
        seen: list = []
        for constraint in list(self.near) + list(self.far):
            for ref in (constraint.ref, constraint.target_ref):
                if ref not in seen:
                    seen.append(ref)
        for zone in self.zones:
            for ref in zone.refs or []:
                if ref not in seen:
                    seen.append(ref)
        return seen

    def to_dict(self) -> dict:
        return {
            "near": [
                {"ref": c.ref, "target_ref": c.target_ref,
                 "distance_mm": round(c.distance_mm, 6)}
                for c in self.near
            ],
            "far": [
                {"ref": c.ref, "target_ref": c.target_ref,
                 "distance_mm": round(c.distance_mm, 6)}
                for c in self.far
            ],
            "zones": [
                {"group_name": z.group_name, "x_min": round(z.x_min, 6),
                 "y_min": round(z.y_min, 6), "x_max": round(z.x_max, 6),
                 "y_max": round(z.y_max, 6), "refs": list(z.refs or [])}
                for z in self.zones
            ],
            "reasons": {k: list(v) for k, v in self.reasons.items()},
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        """Human-readable summary, or ``""`` when nothing was generated.

        Silence on a board with no converter is the contract -- the same one
        :meth:`~skidl_layout.power_roles.PowerStagePlan.summary` holds to.
        """
        if not self and not self.warnings:
            return ""
        lines = ["Generated power constraints:"]
        for constraint in self.near:
            lines.append(
                f"  near {constraint.ref} -> {constraint.target_ref} "
                f"within {constraint.distance_mm:.2f}mm"
            )
        for constraint in self.far:
            lines.append(
                f"  far  {constraint.ref} from {constraint.target_ref} "
                f"beyond {constraint.distance_mm:.2f}mm"
            )
        for zone in self.zones:
            lines.append(
                f"  zone {zone.group_name} "
                f"[{zone.x_min:.1f},{zone.y_min:.1f} .. "
                f"{zone.x_max:.1f},{zone.y_max:.1f}] "
                f"for {', '.join(zone.refs or []) or 'nobody'}"
            )
        if self.warnings:
            lines.append("  Warnings:")
            for warning in self.warnings[:20]:
                lines.append(f"    {warning}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Footprint geometry -- the only thing besides the plan that is read
# --------------------------------------------------------------------------- #

class _Sizes:
    """Per-ref courtyard reach and area, from footprint geometry or bboxes.

    The half-diagonal computation is deliberately *replicated* from
    ``power_metrics._Placement.courtyard_radius`` rather than imported: that one
    is a private of a module built around a *placement*, and importing it here
    would tie a position-free generator to a position-consuming one.
    """

    def __init__(self, footprint_by_ref=None, fp_geometries=None, fp_bboxes=None):
        self.footprint_by_ref = dict(footprint_by_ref or {})
        self.fp_geometries = dict(fp_geometries or {})
        self.fp_bboxes = dict(fp_bboxes or {})

    def _size(self, ref):
        """``(width_mm, height_mm)`` of ``ref``'s courtyard, or ``None``."""
        footprint = self.footprint_by_ref.get(ref)
        if not footprint:
            return None
        geometry = self.fp_geometries.get(footprint)
        if geometry is not None:
            x_min, y_min, x_max, y_max = geometry.bounds
            return max(0.0, x_max - x_min), max(0.0, y_max - y_min)
        bbox = self.fp_bboxes.get(footprint)
        if bbox is not None:
            width, height = bbox
            return max(0.0, float(width)), max(0.0, float(height))
        return None

    def radius(self, ref):
        """Half the courtyard diagonal -- the part's own corner-on reach."""
        size = self._size(ref)
        return None if size is None else math.hypot(size[0], size[1]) / 2.0

    def area(self, ref):
        size = self._size(ref)
        return None if size is None else size[0] * size[1]

    def extent(self, ref):
        return self._size(ref)


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

def _movability(kinds: dict, ref: str) -> int:
    return _MOVABILITY.get(kinds.get(ref, "unknown"), _DEFAULT_MOVABILITY)


def _orient(kinds: dict, first: str, second: str) -> tuple[str, str]:
    """``(ref, target_ref)`` with the more movable part as ``ref``.

    Ties keep the caller's order -- ``second`` moves toward ``first`` -- which is
    the loop's own traversal order and therefore deterministic without ever
    comparing reference designators.
    """
    if _movability(kinds, first) < _movability(kinds, second):
        return first, second
    return second, first


class _Emitter:
    """Accumulates one :class:`PowerConstraintSet`, de-duplicating as it goes."""

    def __init__(self, result: PowerConstraintSet, sizes: _Sizes, slack: float):
        self.result = result
        self.sizes = sizes
        self.slack = slack
        self._near_pairs: set = set()
        self._far_pairs: set = set()
        #: Refs already spoken for by an earlier stage. A multi-stage board gets
        #: per-stage sets; where they overlap, the first stage wins.
        self.claimed: set = set()

    # -- helpers ------------------------------------------------------------
    def _radii(self, a: str, b: str, what: str):
        r_a, r_b = self.sizes.radius(a), self.sizes.radius(b)
        missing = [ref for ref, r in ((a, r_a), (b, r_b)) if r is None]
        if missing:
            self.result.warnings.append(
                f"{what} {a}<->{b} skipped: no footprint geometry for "
                f"{', '.join(missing)} (a flat-millimetre guess is not a "
                f"substitute)"
            )
            return None
        return r_a, r_b

    def near(self, kinds: dict, first: str, second: str, why: str) -> bool:
        if not first or not second or first == second:
            return False
        if first in self.claimed or second in self.claimed:
            self.result.warnings.append(
                f"near {first}<->{second} skipped: already constrained by an "
                f"earlier power stage"
            )
            return False
        radii = self._radii(first, second, "near")
        if radii is None:
            return False
        ref, target = _orient(kinds, first, second)
        if (ref, target) in self._near_pairs or (target, ref) in self._near_pairs:
            return False
        self._near_pairs.add((ref, target))
        distance = round(radii[0] + radii[1] + self.slack, 6)
        self.result.near.append(
            NearConstraint(ref=ref, target_ref=target, distance_mm=distance)
        )
        self.result.reasons.setdefault(f"near {ref}->{target}", []).append(why)
        return True

    def far(self, first: str, second: str, distance_mm: float, why: str) -> bool:
        if not first or not second or first == second:
            return False
        if first in self.claimed or second in self.claimed:
            return False
        if (first, second) in self._far_pairs:
            return False
        self._far_pairs.add((first, second))
        self.result.far.append(
            FarConstraint(
                ref=first, target_ref=second, distance_mm=round(distance_mm, 6)
            )
        )
        self.result.reasons.setdefault(f"far {first}->{second}", []).append(why)
        return True


def _stage_kinds(stage) -> dict:
    return {device.ref: device.kind for device in stage.devices}


def _ordered(stage, wanted) -> list:
    """``wanted``, in the stage's own device order -- never alphabetical.

    ``wanted`` must be an ordered iterable, not a set: a set of strings iterates
    in hash order, which ``PYTHONHASHSEED`` varies **between processes** -- and
    this list is baked into an :class:`~skidl_layout.constraints.AnchorZone`
    that a parallel-placement worker unpickles. Refs the stage's device list
    does not carry keep the caller's order.
    """
    wanted = list(dict.fromkeys(wanted))
    out = [device.ref for device in stage.devices if device.ref in wanted]
    for ref in wanted:
        if ref not in out:
            out.append(ref)
    return out


def _generate_near_loop(emitter, stage, kinds) -> None:
    """Section 3.1 -- the hot loop, hop by hop along the conduction chain.

    **The polygon is deliberately NOT closed**, and that is a change from the
    plan, forced by gate C2. The loop's member order is the *conduction* order
    (capacitor -> rectifier -> switch -> sense resistor) and it closes **through
    ground**, which is a plane, not a component-to-component hop. Constraining
    the last member to abut the first asks the sense resistor to sit next to the
    output capacitor: the datasheet floorplan puts them 8.75 mm apart where the
    half-diagonal bound would demand 7.19, so the closing edge is the one
    generated constraint the known-good layout violates. Dropping it also
    removes the ``A -> B -> C -> A`` cycle the one-shot placer post-pass cannot
    converge on.
    """
    for loop in stage.loops:
        members = list(loop.member_refs)
        for first, second in zip(members, members[1:]):
            emitter.near(
                kinds, first, second,
                f"commutation-loop hop {first}-{second}: the current that "
                f"stops when the switch opens flows through both",
            )
    switches = stage.refs_of_kind("switch")
    if stage.sense_resistor_ref and switches:
        emitter.near(
            kinds, stage.sense_resistor_ref, switches[0],
            "the sense resistor carries the switch current and must sit at "
            "the switch's return terminal",
        )


def _generate_near_divider(emitter, stage, kinds) -> None:
    """Section 3.2 -- the high-impedance feedback node."""
    if not stage.feedback_divider:
        return
    top, bottom = stage.feedback_divider
    if stage.controller_ref:
        # Emitted first on purpose: the placer walks ``constraints.near`` once,
        # in order, so the top resistor settles against the controller before
        # the bottom one is pulled to the top resistor.
        emitter.near(
            kinds, top, stage.controller_ref,
            "the divider tap IS the feedback node -- the highest-impedance "
            "net on the board -- so it must be short",
        )
    emitter.near(
        kinds, top, bottom,
        "the divider's two resistors share the feedback node",
    )


# --------------------------------------------------------------------------- #
# MEASURED NEGATIVE (Phase 3): no rail-capacitor near constraint.
#
# The plan's section 2.6(3) proposed a per-rail replacement for the bulk-cap
# metric Phase 2 disabled -- input caps pulled to the magnetics, output caps to
# the rectifier -- on the reasoning that "per-rail" is the notion Phase 2 found
# missing. Generated and measured against the datasheet floorplan, it is the
# WRONG rule and it is wrong in the same direction the Phase-2 metric was:
#
#     CIN  -> L1     variant B 13.89 mm   bound  9.37 mm   VIOLATED
#     D1   -> COUT1  variant B 14.50 mm   bound 12.98 mm   VIOLATED
#     D1   -> COUT2  variant B 25.00 mm   bound 12.98 mm   VIOLATED
#
# Variant B places its bulk electrolytics far from the rectifier ON PURPOSE --
# the loop is ordered by ESL, HF ceramic nearest, and Phase 1 already separates
# that ceramic out as the loop member. On a boost the input cap is not in a
# high-di/dt loop at all (the inductor current is continuous), so pulling it to
# the magnetics buys nothing and costs board area the small-signal column needs.
# Where a rail capacitor genuinely IS in the hot loop -- the input cap of a buck
# -- ``power_roles`` already puts it in ``member_refs`` and section 3.1 above
# constrains it. Nothing to add here; the correct rule is silence.
# --------------------------------------------------------------------------- #


def _generate_far_small_signal(
    emitter, stage, sizes, far_excludes_divider: bool
) -> None:
    """Section 3.3 -- small-signal parts away from the switching node."""
    aggressors = stage.refs_of_kind("switch") + stage.refs_of_kind("magnetics")
    if not aggressors:
        return
    divider = set(stage.feedback_divider or ())
    for ref in stage.small_signal_refs:
        if far_excludes_divider and ref in divider:
            # The divider is being pulled *toward* the controller by section
            # 3.2; a far-push from the switch on the same ref is exactly the
            # placer tug-of-war the enforcement model cannot resolve.
            continue
        for aggressor in aggressors:
            radius = sizes.radius(aggressor)
            if radius is None:
                emitter.result.warnings.append(
                    f"far {ref}<->{aggressor} skipped: no footprint geometry "
                    f"for {aggressor}"
                )
                continue
            emitter.far(
                ref, aggressor, _SMALL_SIGNAL_BASE_MM + radius,
                "high-impedance node: keep it out of the switching node's "
                "dV/dt field",
            )


def _generate_zone(emitter, stage, sizes, outline) -> None:
    """Section 3.4 -- one power zone per stage, only when an outline exists.

    Sized from the members' own courtyards rather than a fraction of the board,
    so a 20-part converter on a big panel does not get the whole panel. Anchored
    at the outline's bottom-left corner, which is the corner
    ``candidates._with_power_zone`` and ``_with_power_topology`` already use --
    picking any *other* corner would need a placement to justify it, and this
    module has none.

    **This is the least-evidenced part of the design** and it ships behind the
    bake-off decision recorded in the Phase-3 report, not on principle.
    """
    if outline is None or not stage.controller_ref:
        return
    wanted: list = []
    for loop in stage.loops:
        wanted.extend(loop.member_refs)
        wanted.extend(loop.bulk_refs)
    for kind in ("switch", "magnetics", "rectifier", "input_cap", "output_cap"):
        wanted.extend(stage.refs_of_kind(kind))
    refs = _ordered(stage, [r for r in wanted if r not in emitter.claimed])
    if len(refs) < 2:
        return

    areas = [sizes.area(ref) for ref in refs]
    known = [a for a in areas if a is not None]
    if not known:
        emitter.result.warnings.append(
            f"zone for {stage.controller_ref} skipped: no footprint geometry "
            f"for any member"
        )
        return

    outline_area = outline.width_mm * outline.height_mm
    if outline_area <= 0.0:
        return
    target = min(
        _ZONE_AREA_FACTOR * sum(known),
        _ZONE_MAX_OUTLINE_FRACTION * outline_area,
    )
    scale = min(1.0, math.sqrt(target / outline_area))
    width = outline.width_mm * scale
    height = outline.height_mm * scale

    # Never smaller than twice the largest member in either axis: a zone the
    # parts cannot fit inside is a clamp that manufactures overlaps.
    extents = [sizes.extent(ref) for ref in refs]
    extents = [e for e in extents if e is not None]
    if extents:
        width = max(width, 2.0 * max(e[0] for e in extents))
        height = max(height, 2.0 * max(e[1] for e in extents))
    width = min(width, outline.width_mm)
    height = min(height, outline.height_mm)

    emitter.result.zones.append(
        AnchorZone(
            group_name=f"power_stage_{stage.controller_ref}",
            x_min=round(outline.x_min, 6),
            y_min=round(outline.y_max - height, 6),
            x_max=round(outline.x_min + width, 6),
            y_max=round(outline.y_max, 6),
            refs=refs,
        )
    )
    emitter.result.reasons.setdefault(
        f"zone power_stage_{stage.controller_ref}", []
    ).append(
        f"the {stage.topology} stage's power path kept together in "
        f"{width:.1f}x{height:.1f}mm ({_ZONE_AREA_FACTOR:.0f}x its members' "
        f"summed courtyard)"
    )


def generate_power_constraints(
    power_stage_plan,
    footprint_by_ref: dict | None = None,
    fp_bboxes: dict | None = None,
    fp_geometries: dict | None = None,
    outline=None,
    clearance_mm: float = 0.5,
    zones: bool = False,
    far_excludes_divider: bool = False,
) -> PowerConstraintSet:
    """Turn a :class:`~skidl_layout.power_roles.PowerStagePlan` into constraints.

    Args:
        power_stage_plan: what
            :func:`~skidl_layout.power_roles.classify_power_roles` found.
            ``None`` or a plan with no stages -> an empty set, silently.
        footprint_by_ref: ``ref -> footprint name``. Required for any distance
            to be generated at all: ``fp_bboxes`` and ``fp_geometries`` are
            keyed by *footprint*, and a constraint sized without geometry would
            be the flat millimetre this module exists to avoid.
        fp_bboxes: ``footprint -> (width_mm, height_mm)``, the fallback when a
            :class:`~skidl_layout.geometry.FootprintGeometry` is unavailable.
        fp_geometries: ``footprint -> FootprintGeometry``, the preferred source
            (its ``bounds`` are the courtyard).
        outline: the board, for the per-stage zone. ``None`` -> no zones, same
            guard ``candidates._with_power_zone`` uses.
        clearance_mm: half the slack added to every near distance, so a pair is
            asked to abut with a real gap rather than exactly touch.
        zones: emit the per-stage :class:`~skidl_layout.constraints.AnchorZone`.
            **Ships DISABLED**, measured: a zone is a hard clamp
            (``placer._bounds_for_part`` returns it *instead of* the outline),
            and on the boost it pinned the power path into the bottom-left
            corner hard enough that the candidate lost outright -- penalty
            unchanged at 44.62 on the shared outline where the zone-free set
            reaches 4.68. Kept as a knob because the corner choice, not the
            idea, is what is unevidenced.
        far_excludes_divider: leave the feedback divider out of the
            small-signal far-push, on the theory that a ref pulled toward the
            controller and pushed from the switch is a tug-of-war. **Ships
            DISABLED**, measured: excluding the divider is neutral on the
            shared outline (4.68 either way) and worse on the derived one
            (12.52 vs 11.25), so the tug-of-war is real but costs less than
            leaving the divider unprotected.

    Returns:
        A :class:`PowerConstraintSet`. Never raises on a missing role or a
        missing footprint -- the affected constraint is skipped and the reason
        recorded in ``warnings``.
    """
    result = PowerConstraintSet()
    stages = list(getattr(power_stage_plan, "stages", None) or [])
    if not stages:
        return result

    sizes = _Sizes(footprint_by_ref, fp_geometries, fp_bboxes)
    emitter = _Emitter(result, sizes, 2.0 * float(clearance_mm or 0.0))

    for stage in stages:
        kinds = _stage_kinds(stage)
        before_near = len(result.near)
        before_far = len(result.far)
        before_zones = len(result.zones)

        _generate_near_loop(emitter, stage, kinds)
        _generate_near_divider(emitter, stage, kinds)
        _generate_far_small_signal(emitter, stage, sizes, far_excludes_divider)
        if zones:
            _generate_zone(emitter, stage, sizes, outline)

        # Everything this stage spoke for is off-limits to the next one.
        for constraint in result.near[before_near:] + result.far[before_far:]:
            emitter.claimed.add(constraint.ref)
            emitter.claimed.add(constraint.target_ref)
        for zone in result.zones[before_zones:]:
            emitter.claimed.update(zone.refs or [])

    return result
