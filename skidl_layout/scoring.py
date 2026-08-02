from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .congestion import build_congestion_map
from .decaps import measure_decap_pad_distances
from .geometry import FootprintGeometry
from .grid import points_form_clean_grid
from .power import plan_power_routes
from .roles import (
    GND_NET_RE,
    POWER_NET_RE,
    PartRole,
    _alpha_tokens,  # re-exported for backward compat (defined in roles.py)
    _part_tokens,  # re-exported for backward compat (defined in roles.py)
    classify_parts,
    is_nc_net,
    is_bulk_cap,
    is_ui_grid_part,
    part_pin_nets_by_number,
    pin_net_names,
)
from .ratnest import PadPoint, count_crossings, is_plane_net, net_airwires
from .validator import validate
from .writer import PlacedPart

# --------------------------------------------------------------------------- #
# The crossing objective -- metric-validation step 1. OFF by default.
# --------------------------------------------------------------------------- #
#: The shipped behaviour, and the default everywhere.
CROSSING_OBJECTIVE_LEGACY = "legacy"
#: Plane nets excluded from the crossing count, and the term rescaled so it is
#: not clipped. ⛔ The two halves are inseparable -- see ``_crossing_term``.
#: ⚠ GRADED AND PARKED: it regressed 2 of 6 boards, because un-saturating the
#: term gave the STAR PROXY authority where the proxy disagrees with the truth.
#: Kept only so the negative is reproducible; ``mst`` is its replacement.
CROSSING_OBJECTIVE_SIGNAL = "signal"
#: ⭐⭐ Plane-free MST over PAD positions -- the metric every judge in this repo
#: already measures (``ratnest.analyse_board``), now computed at placement time.
CROSSING_OBJECTIVE_MST = "mst"
CROSSING_OBJECTIVES = (CROSSING_OBJECTIVE_LEGACY, CROSSING_OBJECTIVE_SIGNAL,
                       CROSSING_OBJECTIVE_MST)

#: ⭐⭐⭐ PROMOTED 2026-07-30, from ``legacy`` to ``mst``. Graded on the six-board
#: eval set: signal crossings better on 6/6 (corpus 144 -> 89 against the human's
#: 20) and **vias better on 6/6** (115 -> 70 against 30), router effort better on
#: 5/6. What earned it is gate ``E0`` -- the objective and the judge are the same
#: number, verified against the written board rather than asserted.
#:
#: ⛔ **This moved every placement digest in the repo, deliberately.** The old
#: baselines are not lost: ``crossing_objective="legacy"`` still reproduces them
#: byte for byte, pinned by ``tests/test_crossing_objective.py`` and recorded
#: beside the new ones in ``lt3757_boost/phase0_out/phase0_metrics.json``.
#: ⭐ Defined HERE rather than in ``engine`` so a direct ``score_placement`` call
#: grades with the same objective the placer optimised -- two defaults would make
#: a hand-scored placement silently incomparable with a planned one.
DEFAULT_CROSSING_OBJECTIVE = CROSSING_OBJECTIVE_MST

#: ⛔⛔ MEASURED 2026-07-30 on the six-board eval set: ``2.0`` saturates the
#: ``20.0`` ceiling at **10 crossings**, and the star metric reads 101-499 on
#: every board. The term is therefore a CONSTANT on 12 of 12 board-arms and
#: contributes **no gradient at all** -- the placer's whole continuous quality
#: signal is HPWL. Kept exactly as-is so ``legacy`` stays byte-identical.
_CROSSING_GAIN_LEGACY, _CROSSING_CAP_LEGACY = 2.0, 20.0

#: Signal mode. The gain is DERIVED, not chosen: over all 132 board-instances in
#: the eval set (6 boards x {hand, auto, 20 random}) plane-free star crossings
#: run 0-242 with a 90th percentile of 138, so ``20.0 / 138 ~= 0.145`` puts the
#: ceiling just above that percentile. ⭐ The consequence that matters: **no auto
#: or hand placement in the corpus reaches the cap** (worst is 88), so the term
#: is live over the entire range a search actually traverses, while the cap still
#: stops a pathological scatter from deciding a board on this term alone.
_CROSSING_GAIN_SIGNAL, _CROSSING_CAP_SIGNAL = 0.15, 20.0


#: MST mode. Derived the same way and from the same 132 board-instances, but
#: against the MST-over-pads distribution rather than the star one, because the
#: two are on completely different scales: plane-free MST signal crossings run
#: **0-136 with a 90th percentile of 85** (the star metric's were 0-242, p90
#: 138), so ``20.0 / 85 ~= 0.235`` puts the ceiling at the same percentile.
#: ⭐ The worst AUTO or HAND placement in the corpus is 42 -> a term of 9.9,
#: less than half the cap, so the term is live across the whole range a search
#: traverses and only a bad random scatter clips.
_CROSSING_GAIN_MST, _CROSSING_CAP_MST = 0.235, 20.0


# --------------------------------------------------------------------------- #
# The HPWL objective -- metric-validation step 2. OFF by default.
# --------------------------------------------------------------------------- #
#: The shipped behaviour: each net's bounding box is taken over the **centres**
#: of the parts it touches.
HPWL_OBJECTIVE_CENTROID = "centroid"
#: ⭐⭐ The same bounding box taken over **pad positions** -- the points
#: ``_placement_pad_points`` already produces for the MST crossing objective and
#: ``ratnest.read_pad_points`` reads back off a written board.
HPWL_OBJECTIVE_PADS = "pads"
HPWL_OBJECTIVES = (HPWL_OBJECTIVE_CENTROID, HPWL_OBJECTIVE_PADS)

#: ⛔ **Default unchanged, and on this path every HPWL number is byte-identical
#: to before the parameter existed.** Defined HERE rather than in ``engine`` for
#: the same reason ``DEFAULT_CROSSING_OBJECTIVE`` is: two defaults would make a
#: directly-scored placement silently incomparable with a planned one.
DEFAULT_HPWL_OBJECTIVE = HPWL_OBJECTIVE_CENTROID


# --------------------------------------------------------------------------- #
# The HPWL net SET -- metric-validation step 3. OFF by default.
# --------------------------------------------------------------------------- #
#: The shipped behaviour: **every** net contributes to both HPWL terms.
HPWL_NETS_ALL = "all"
#: ⭐⭐ Nets this stack **pours** rather than routes leave the term, using the
#: same :func:`ratnest.is_plane_net` predicate ``_mst_crossings`` already
#: consumes. ⛔⛔ **The two terms had different net vocabularies and nobody had
#: named it:** the MST promotion made the crossing term plane-free over pads and
#: left HPWL all-nets -- and HPWL is the term with all the gradient. Measured on
#: the six frozen control placements 2026-08-01: plane nets carry **37-68 % of
#: the *weighted* HPWL** (``_net_weight`` gives GND 2.0, POWER 1.6, and a plane
#: net touches nearly every part), so on 3 of 6 boards the placer's only
#: continuous quality signal is majority-plane -- compaction of copper that gets
#: **poured, not routed**.
HPWL_NETS_PLANE_FREE = "plane_free"
HPWL_NET_SETS = (HPWL_NETS_ALL, HPWL_NETS_PLANE_FREE)

#: ⛔ Same defaulting discipline as the two objectives above, and for the same
#: reason: a second default would make a directly-scored placement silently
#: incomparable with a planned one.
DEFAULT_HPWL_NETS = HPWL_NETS_ALL


# --------------------------------------------------------------------------- #
# The plane-net WEIGHT -- metric-validation step 4. OFF by default.
# --------------------------------------------------------------------------- #
#: ⭐⭐ **The same axis as ``hpwl_nets``, at a smaller dose**, and that is the
#: whole point. Measured 2026-08-01 on nine frozen control placements:
#: ``hpwl_nets="plane_free"`` makes :func:`_weighted_hpwl` a **byte-identical
#: duplicate** of :func:`_total_hpwl` on 9 of 9 boards, because the only weights
#: :func:`_net_weight` ever returns above 1.0 on this corpus are the two plane
#: ones (a power-converter board carries no USB, clock or crystal net). So
#: ``_net_weight`` **IS** the plane predicate wearing different clothes, "keep"
#: and "drop" are the mildest and most extreme dose of ONE lever, and the
#: interesting doses are the ones in between.
#:
#: ⭐⭐ **And the two numbers can move independently, which removal cannot
#: express.** ``power._strategy`` gives ground ``pour``/``plane`` and gives a
#: supply rail ``trunk``/``wide_trunk``, so ``VIN``/``VOUT``/``VCC`` are named
#: plane nets that get **routed as tracks**; on ``lt3844_buck``
#: ``ratnest.is_plane_net`` covers 55.9 % of pins where the poured set covers
#: 32.4 %. ``trace_aware`` is that finding as a dose: the *poured* ground stops
#: being optimised, the *trunked* supply keeps its weight.
#:
#: ⛔ **Only the two plane coefficients move.** The 1.5 signal-token weight
#: matches nothing on this corpus, so changing it would be risk with no
#: measurable effect.
HPWL_WEIGHTS: dict[str, tuple[float, float]] = {
    # dose            (GND, POWER)
    "legacy": (2.0, 1.6),        # today, and the control
    "light": (1.0, 1.0),         # a plane net counts, but no more than a signal
    "quarter": (0.5, 0.5),       # planes present but not dominant
    "trace_aware": (0.5, 1.6),   # poured ground down, trunked supply unchanged
}
HPWL_WEIGHT_DOSES = tuple(HPWL_WEIGHTS)

#: ⛔ Fifth application of the same defaulting discipline, and the fifth
#: identical reason: two defaults would make a directly-scored placement
#: silently incomparable with a planned one.
DEFAULT_HPWL_WEIGHTS = "legacy"


#: The shipped behaviour: a COUNT of overlapping pairs at 25.0 points each.
OVERLAP_OBJECTIVE_COUNT = "count"
#: ⭐ Depth-graded: **equal to the binary term at full penetration** and
#: continuous below it, so the barrier is never weakened -- only the *way in* to
#: it acquires a gradient.
OVERLAP_OBJECTIVE_AREA = "area"
OVERLAP_OBJECTIVES = (OVERLAP_OBJECTIVE_COUNT, OVERLAP_OBJECTIVE_AREA)

#: ⛔ Sixth application of the same defaulting discipline, declared **in
#: ``scoring``** rather than ``engine`` for the same reason
#: :data:`DEFAULT_HPWL_NETS` is: two defaults would make a directly-scored
#: placement silently incomparable with a planned one.
DEFAULT_OVERLAP_OBJECTIVE = OVERLAP_OBJECTIVE_COUNT

#: The points one fully-interpenetrating pair costs, under **both** objectives.
#: ⛔ Pulled out of the two ``penalty`` lines so the equality at ``unit == 1``
#: is a shared constant rather than two literals that could drift apart.
OVERLAP_PAIR_PENALTY = 25.0


def _overlap_term(
    validation,
    clearance_mm: float,
    overlap_objective: str = DEFAULT_OVERLAP_OBJECTIVE,
) -> float:
    """The overlap contribution to ``penalty``. Default = the shipped count.

    ``"count"`` is ``len(validation.overlaps) * 25.0`` -- byte-identical to the
    line it replaces, and the arithmetic is deliberately the *same expression*
    rather than a re-derivation.

    ``"area"`` grades each overlapping pair by how far it has penetrated::

        depth(a, b) = max(0.0, clearance_mm - _pair_gap(a, b))   # 0 at threshold
        unit(a, b)  = min(depth(a, b) / clearance_mm, 1.0)       # 0..1
        term        = 25.0 * SUM over overlapping pairs of unit(a, b)

    ⭐⭐⭐ **Why this shape and not a bigger one.** ``unit`` saturates at 1.0, so
    a fully-interpenetrating pair costs **exactly** what it costs today. The
    change is strictly a *refinement below* the existing cost: it can never make
    an illegal placement cheaper than the count already made it, only
    distinguish a 0.01 mm graze from a 3 mm interpenetration on the way out.

    ⛔⛔ **A pair with no AABB gap is charged the FULL 1.0.**
    :func:`~skidl_layout.validator.overlap_gaps` returns ``None`` for
    through-board pad collisions (which live on *opposite* sides of the board,
    where a signed AABB separation is meaningless) and for a hand-built
    ``ValidationResult`` with no index. Charging those less would make a real
    pad collision cheaper than it is today, which is the one thing this term is
    forbidden to do.

    ⛔⛔ **This does not touch ``ok``.** Both :attr:`ValidationResult.ok` and
    :attr:`LayoutScore.ok` stay boolean and stay keyed off ``overlaps`` being
    non-empty. A depth grader must never make an illegal board *legal*; it may
    only change which illegal board the search prefers on the way out of
    illegality.

    ⚠ **And it is very likely NOT the whole story, which is measured rather than
    guessed.** ``penalty`` is the LAST thing the search consults --
    ``refinement._is_better`` is lexicographic on ``_hard_count`` and reaches
    the penalty comparison on only 48.9-61.3 % of its calls (WS-Z1, six power
    boards), of which 26.0-48.1 % have an illegal side. That fraction is this
    term's entire reachable surface.
    """
    pairs = validation.overlaps
    if overlap_objective != OVERLAP_OBJECTIVE_AREA:
        return len(pairs) * OVERLAP_PAIR_PENALTY
    if not pairs:
        return 0.0

    from .validator import overlap_gaps

    if clearance_mm <= 0.0:
        return len(pairs) * OVERLAP_PAIR_PENALTY
    total = 0.0
    for _ref_a, _ref_b, gap in overlap_gaps(validation):
        if gap is None:
            total += 1.0
            continue
        total += min(max(0.0, clearance_mm - gap) / clearance_mm, 1.0)
    return total * OVERLAP_PAIR_PENALTY


def _weight_pair(hpwl_weights) -> tuple[float, float]:
    """``(gnd, power)`` from a dose NAME or an already-resolved pair.

    ⭐ Accepting both is what lets :func:`_weighted_hpwl` resolve once and hand
    the pair down its inner loop without re-hashing a dict per net, while every
    caller that has only the name (and every existing one-argument call to
    :func:`_net_weight`) keeps working unchanged.
    """
    if isinstance(hpwl_weights, str):
        pair = HPWL_WEIGHTS.get(hpwl_weights)
        if pair is None:
            raise ValueError(
                f"unknown hpwl_weights {hpwl_weights!r}; "
                f"expected one of {', '.join(HPWL_WEIGHT_DOSES)}"
            )
        return pair
    gnd, power = hpwl_weights
    return (float(gnd), float(power))


def _net_pad_extents(pad_points) -> dict[str, list]:
    """``{net: [min_x, max_x, min_y, max_y, {refs}]}`` over pad points.

    ⭐ One pass, shared by both HPWL terms. ``refs`` is carried because the
    centroid terms require a net to touch **two distinct placed parts** before
    it contributes, and dropping that condition would smuggle a second change
    (a wider net set) into a plan that is allowed exactly one.
    """
    extents: dict[str, list] = {}
    for point in pad_points:
        entry = extents.get(point.net)
        if entry is None:
            extents[point.net] = [point.x, point.x, point.y, point.y,
                                  {point.ref}]
            continue
        if point.x < entry[0]:
            entry[0] = point.x
        if point.x > entry[1]:
            entry[1] = point.x
        if point.y < entry[2]:
            entry[2] = point.y
        if point.y > entry[3]:
            entry[3] = point.y
        entry[4].add(point.ref)
    return extents


def _hpwl_by_net(
    placed_parts: list[PlacedPart],
    circuit,
    ctx=None,
    *,
    hpwl_objective: str = DEFAULT_HPWL_OBJECTIVE,
    hpwl_nets: str = DEFAULT_HPWL_NETS,
    pad_extents: dict[str, list] | None = None,
) -> list[tuple[str, float]]:
    """``(net, half-perimeter)`` for every net that contributes, per mode.

    ⭐ **The single source of truth for both HPWL terms.** ``_total_hpwl`` and
    ``_weighted_hpwl`` differ only in the per-net weight, and having them walk
    the connectivity twice was already wasteful; in ``"pads"`` mode it would
    also mean building the pad-extent map twice per score call.

    ⛔ **``hpwl_objective`` changes the POINTS; ``hpwl_nets`` changes the SET.**
    They are orthogonal and compose, which is why they are two parameters and
    not one enum: the pads arm was graded and parked with the plane nets still
    in, and its own measured mechanism was that plane-exactness grows the plane
    box. Under ``"plane_free"`` the *membership* rule is otherwise unchanged --
    ``_net_ref_lists``' "at least two distinct placed refs" still applies, and a
    net whose pins all land on one part still contributes nothing.
    """
    if circuit is None:
        return []

    nets = _net_ref_lists(circuit, ctx)
    plane_free = hpwl_nets == HPWL_NETS_PLANE_FREE
    if hpwl_objective == HPWL_OBJECTIVE_PADS:
        extents = pad_extents or {}
        result = []
        for name, _refs in nets:
            if plane_free and is_plane_net(name):
                continue
            entry = extents.get(name)
            if entry is None or len(entry[4]) < 2:
                continue
            result.append((name, (entry[1] - entry[0]) + (entry[3] - entry[2])))
        return result

    pos_by_ref = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed_parts}
    result = []
    for name, refs in nets:
        if plane_free and is_plane_net(name):
            continue
        xs, ys = [], []
        for ref in refs:
            pos = pos_by_ref.get(ref)
            if pos is not None:
                xs.append(pos[0])
                ys.append(pos[1])
        if len(xs) >= 2:
            result.append((name, (max(xs) - min(xs)) + (max(ys) - min(ys))))
    return result


def _placement_pad_points(placed_parts, circuit, fp_geometries) -> list[PadPoint]:
    """Every net-carrying pad of a *planned* placement, in board coordinates.

    ⭐⭐ The point of this function is that the objective and the judge stop
    measuring different things. ``ratnest.read_pad_points`` does exactly this
    for a written ``.kicad_pcb``; this does it for a placement that has not been
    written yet, from the same two sources the writer uses --
    ``roles.part_pin_nets_by_number`` for pad->net and
    ``FootprintGeometry.pad_world_centers`` for pad->position.

    ⛔ **Ordering is load-bearing.** ``ratnest.mst_edges`` breaks ties on
    ``(distance, index)``, so the caller must supply a stable point order or the
    tree -- and therefore the crossing count -- becomes input-order dependent.
    This reproduces ``analyse_board``'s order exactly: refs sorted, each ref's
    pads sorted by ``(ref, pad)`` as STRINGS (so ``"10"`` sorts before ``"2"``,
    matching ``read_pad_points``).

    ⚠ A part with no footprint geometry falls back to its centroid for every
    pad. That is the pre-MST behaviour for that part and it is silent: geometry
    is missing only when the footprint could not be loaded, which
    ``validate``/``score_placement`` already report on their own terms.
    """
    parts_by_ref = {
        str(getattr(part, "ref", "") or ""): part
        for part in (getattr(circuit, "parts", []) or [])
    }
    pads_by_ref: dict[str, list[PadPoint]] = {}
    for placed in placed_parts:
        part = parts_by_ref.get(placed.ref)
        if part is None:
            continue
        pin_nets = part_pin_nets_by_number(part)
        if not pin_nets:
            continue
        geometry = (fp_geometries or {}).get(placed.footprint)
        centers = geometry.pad_world_centers(placed) if geometry is not None else {}
        points = []
        for pad_number, net_name in pin_nets.items():
            x_mm, y_mm = centers.get(pad_number, (placed.x_mm, placed.y_mm))
            points.append(PadPoint(ref=placed.ref, pad=pad_number,
                                   net=net_name, x=x_mm, y=y_mm))
        if points:
            pads_by_ref[placed.ref] = sorted(points, key=lambda p: (p.ref, p.pad))

    ordered: list[PadPoint] = []
    for ref in sorted(pads_by_ref):
        ordered.extend(pads_by_ref[ref])
    return ordered


def _mst_crossings(placed_parts, circuit, fp_geometries,
                   *, exclude_plane_nets: bool = True,
                   pad_points: list[PadPoint] | None = None) -> int:
    """Plane-free MST-over-pads crossings for a planned placement.

    ⭐ Byte-for-byte the number ``RatNest.signal_crossings`` reports for the same
    placement once written: same pad points, same per-net MST, same
    ``count_crossings`` predicate (skip same-net, skip shared endpoint).
    ⭐⭐ That equivalence is the entire justification for the swap, so it is
    **gated on real boards** rather than on fixtures -- gate ``E0`` of
    ``skidl-eda/canaries/grade_crossing_objective.py`` places each board and
    asserts this function against ``analyse_board`` on the ``.kicad_pcb`` it
    wrote. The unit tests here cover ordering, plane filtering and the fallback.
    """
    if circuit is None:
        return 0
    if pad_points is None:
        pad_points = _placement_pad_points(placed_parts, circuit, fp_geometries)
    by_net: dict[str, list[PadPoint]] = {}
    for point in pad_points:
        if exclude_plane_nets and is_plane_net(point.net):
            continue
        by_net.setdefault(point.net, []).append(point)

    wires = []
    for net in sorted(by_net):
        wires.extend(net_airwires(by_net[net], net))
    return count_crossings(wires)


def _crossing_term(crossing_count: int, crossing_objective: str) -> float:
    """The crossing penalty contribution, per objective mode.

    ⛔⛔ **Excluding plane nets and rescaling the term are ONE change, not two.**
    Filtering alone buys nothing while the term is clipped -- both arms still
    land on the 20.0 ceiling and the score cannot tell them apart. Rescaling
    alone amplifies a metric that MISRANKS: on ``lt3844_buck`` the all-nets star
    count prefers the engine's board (+81.2 %) where MST-over-pads prefers the
    human's (-61.4 %), because the human's compact board stacks the star's long
    plane-net spokes on top of each other (``SGND`` 88, ``PGND`` 67, ``VIN`` 64,
    ``VOUT`` 55 crossings against ``SW`` 13 for the first signal net). Plane-free,
    the same metric ranks the human's board better on **6 of 6**.
    """
    if crossing_objective == CROSSING_OBJECTIVE_MST:
        return min(crossing_count * _CROSSING_GAIN_MST, _CROSSING_CAP_MST)
    if crossing_objective == CROSSING_OBJECTIVE_SIGNAL:
        return min(crossing_count * _CROSSING_GAIN_SIGNAL, _CROSSING_CAP_SIGNAL)
    return min(crossing_count * _CROSSING_GAIN_LEGACY, _CROSSING_CAP_LEGACY)


@dataclass
class LayoutScore:
    score: float
    penalty: float = 0.0
    total_hpwl_mm: float = 0.0
    overlap_count: int = 0
    outline_violation_count: int = 0
    keepout_violation_count: int = 0
    cutout_violation_count: int = 0
    missing_count: int = 0
    warning_count: int = 0
    weighted_hpwl_mm: float = 0.0
    crossing_count: int = 0
    congestion_score: float = 0.0
    power_corridor_count: int = 0
    role_counts: dict[str, int] = field(default_factory=dict)
    power_net_count: int = 0
    congestion_regions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    footprint_envelope_bbox_mm: dict[str, float] = field(default_factory=dict)
    footprint_envelope_area_ratio: float = 0.0
    compact_outline_mm: dict[str, float] = field(default_factory=dict)
    compact_outline_area_ratio: float = 0.0
    empty_margin_ratios: dict[str, float] = field(default_factory=dict)
    max_empty_margin_ratio: float = 0.0
    front_panel_trace_count: int = 0
    front_panel_trace_mm: float = 0.0

    @property
    def ok(self) -> bool:
        return (
            self.overlap_count == 0
            and self.outline_violation_count == 0
            and self.keepout_violation_count == 0
            and self.cutout_violation_count == 0
            and self.missing_count == 0
        )

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "penalty": self.penalty,
            "total_hpwl_mm": self.total_hpwl_mm,
            "overlap_count": self.overlap_count,
            "outline_violation_count": self.outline_violation_count,
            "keepout_violation_count": self.keepout_violation_count,
            "cutout_violation_count": self.cutout_violation_count,
            "missing_count": self.missing_count,
            "warning_count": self.warning_count,
            "weighted_hpwl_mm": self.weighted_hpwl_mm,
            "crossing_count": self.crossing_count,
            "congestion_score": self.congestion_score,
            "power_corridor_count": self.power_corridor_count,
            "role_counts": dict(self.role_counts),
            "power_net_count": self.power_net_count,
            "congestion_regions": list(self.congestion_regions),
            "warnings": list(self.warnings),
            "footprint_envelope_bbox_mm": dict(self.footprint_envelope_bbox_mm),
            "footprint_envelope_area_ratio": self.footprint_envelope_area_ratio,
            "compact_outline_mm": dict(self.compact_outline_mm),
            "compact_outline_area_ratio": self.compact_outline_area_ratio,
            "empty_margin_ratios": dict(self.empty_margin_ratios),
            "max_empty_margin_ratio": self.max_empty_margin_ratio,
            "front_panel_trace_count": self.front_panel_trace_count,
            "front_panel_trace_mm": self.front_panel_trace_mm,
            "ok": self.ok,
        }

    def summary(self) -> str:
        lines = [f"Layout score: {self.score:.1f}/100"]
        lines.append(f"Total HPWL: {self.total_hpwl_mm:.1f}mm")
        if self.overlap_count:
            lines.append(f"Overlaps: {self.overlap_count}")
        if self.outline_violation_count:
            lines.append(f"Outside outline: {self.outline_violation_count}")
        if self.keepout_violation_count:
            lines.append(f"Inside keepout: {self.keepout_violation_count}")
        if self.cutout_violation_count:
            lines.append(f"Intersects cutout: {self.cutout_violation_count}")
        if self.missing_count:
            lines.append(f"Missing placements: {self.missing_count}")
        if self.crossing_count:
            lines.append(f"Estimated crossings: {self.crossing_count}")
        if self.congestion_score:
            lines.append(f"Pin escape congestion: {self.congestion_score:.1f}")
        if self.congestion_regions:
            lines.append("Top congested regions:")
            for region in self.congestion_regions[:5]:
                lines.append(f"  {region}")
        if self.power_corridor_count:
            lines.append(f"Power corridors: {self.power_corridor_count}")
        if self.front_panel_trace_count:
            lines.append(
                f"Visible front-panel trace spans: {self.front_panel_trace_count} "
                f"({self.front_panel_trace_mm:.1f}mm)"
            )
        if self.compact_outline_mm and self.compact_outline_area_ratio:
            lines.append(
                "Compact outline estimate: "
                f"{self.compact_outline_mm.get('width', 0.0):.1f}mm x "
                f"{self.compact_outline_mm.get('height', 0.0):.1f}mm "
                f"({self.compact_outline_area_ratio * 100:.0f}% of outline)"
            )
        if self.warnings:
            lines.append("Warnings:")
            for warning in self.warnings[:20]:
                lines.append(f"  {warning}")
        return "\n".join(lines)


def _distance(a: PlacedPart, b: PlacedPart) -> float:
    return math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm)


def _net_ref_lists(circuit, ctx=None) -> list[tuple[str, list[str]]]:
    """(net_name, deduped ref list) for nets touching >=2 distinct refs.

    Returns the ctx-cached topology when available, else the identical live
    traversal (shared shape with congestion._net_refs). Positions are NOT
    filtered here — callers filter refs against their own placed set per call.
    """
    if ctx is not None and ctx.net_ref_lists:
        return ctx.net_ref_lists
    if circuit is None:
        return []

    result: list[tuple[str, list[str]]] = []
    for net in circuit.get_nets():
        if is_nc_net(net):
            continue
        name = str(getattr(net, "name", "") or "")
        refs: list[str] = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref is not None and ref not in refs:
                refs.append(ref)
        if len(refs) >= 2:
            result.append((name, refs))
    return result


def _total_hpwl(
    placed_parts: list[PlacedPart],
    circuit,
    ctx=None,
    *,
    hpwl_objective: str = DEFAULT_HPWL_OBJECTIVE,
    hpwl_nets: str = DEFAULT_HPWL_NETS,
    pad_extents: dict[str, list] | None = None,
) -> float:
    return sum(hpwl for _name, hpwl in _hpwl_by_net(
        placed_parts, circuit, ctx,
        hpwl_objective=hpwl_objective, hpwl_nets=hpwl_nets,
        pad_extents=pad_extents))


def _net_weight(name: str, hpwl_weights=DEFAULT_HPWL_WEIGHTS) -> float:
    """The per-net multiplier :func:`_weighted_hpwl` applies.

    ⛔ **There are FOUR ``_net_weight`` functions in this package** --
    ``refinement`` (GND 2.0 / POWER 1.7, feeding the move generator's neighbour
    centroid), ``congestion`` (1.8 / 1.5) and ``orientation`` (2.4 / 2.0) each
    carry their own, with four different sets of numbers and no shared
    definition. **This one, and only this one, is the objective's.** The others
    are separate consumers with separate jobs and are deliberately untouched;
    see :data:`HPWL_WEIGHTS`.
    """
    gnd_weight, power_weight = _weight_pair(hpwl_weights)
    if GND_NET_RE.match(name):
        return gnd_weight
    if POWER_NET_RE.match(name):
        return power_weight
    if any(token in name.upper() for token in ("USB", "D+", "D-", "CLK", "XTAL")):
        return 1.5
    return 1.0


_PRIMARY_OWNER_ROLES = {"ic", "regulator", "module_socket"}


def _is_supply_or_ground_net(net_name: str) -> bool:
    return bool(POWER_NET_RE.match(net_name) or GND_NET_RE.match(net_name))


def _role_weight(role_name: str) -> float:
    return {
        "regulator": 6.0,
        "module_socket": 5.5,
        "ic": 5.0,
    }.get(role_name, 1.0)


def _token_affinity(passive_part, owner_part, *, token_cache=None) -> int:
    passive_ref = getattr(passive_part, "ref", None)
    owner_ref = getattr(owner_part, "ref", None)
    if token_cache is not None and passive_ref in token_cache:
        passive_tokens = token_cache[passive_ref]
    else:
        passive_tokens = _part_tokens(passive_part)
    if token_cache is not None and owner_ref in token_cache:
        owner_tokens = token_cache[owner_ref]
    else:
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


def _select_primary_owner_ref(
    ref: str,
    part_by_ref: dict,
    nets_by_ref: dict[str, set[str]],
    roles: dict[str, PartRole],
    placed_by_ref: dict[str, PlacedPart],
    *,
    require_signal: bool,
    require_power_and_ground: bool = False,
    token_cache: dict[str, set[str]] | None = None,
) -> str | None:
    part = part_by_ref.get(ref)
    placed = placed_by_ref.get(ref)
    if part is None or placed is None:
        return None
    passive_nets = nets_by_ref.get(ref, set())
    signal_nets = {
        net_name
        for net_name in passive_nets
        if not _is_supply_or_ground_net(net_name)
    }
    candidates = []
    for other_ref, other_role in roles.items():
        if other_ref == ref or other_ref not in placed_by_ref:
            continue
        role_name = other_role.role if other_role is not None else "unknown"
        if role_name not in _PRIMARY_OWNER_ROLES:
            continue
        shared = passive_nets & nets_by_ref.get(other_ref, set())
        if not shared:
            continue
        shared_signal = {
            net_name
            for net_name in shared
            if not _is_supply_or_ground_net(net_name)
        }
        if require_signal and signal_nets and not shared_signal:
            continue
        if require_power_and_ground:
            if not any(POWER_NET_RE.match(net_name) for net_name in shared):
                continue
            if not any(GND_NET_RE.match(net_name) for net_name in shared):
                continue
        other = placed_by_ref[other_ref]
        distance = math.hypot(placed.x_mm - other.x_mm, placed.y_mm - other.y_mm)
        if require_power_and_ground:
            candidates.append(
                (
                    -_token_affinity(
                        part, part_by_ref.get(other_ref), token_cache=token_cache
                    ),
                    distance,
                    -_role_weight(role_name),
                    other_ref,
                )
            )
        else:
            candidates.append(
                (
                    -_token_affinity(
                        part, part_by_ref.get(other_ref), token_cache=token_cache
                    ),
                    -len(shared_signal),
                    -_role_weight(role_name),
                    distance,
                    other_ref,
                )
            )
    if not candidates:
        return None
    return min(candidates)[-1]


def _weighted_hpwl(
    placed_parts: list[PlacedPart],
    circuit,
    ctx=None,
    *,
    hpwl_objective: str = DEFAULT_HPWL_OBJECTIVE,
    hpwl_nets: str = DEFAULT_HPWL_NETS,
    hpwl_weights=DEFAULT_HPWL_WEIGHTS,
    pad_extents: dict[str, list] | None = None,
) -> float:
    # ⭐ Resolved ONCE, not per net: the dose is a constant for the whole call
    # and this loop runs on every score of every trial.
    weights = _weight_pair(hpwl_weights)
    return sum(hpwl * _net_weight(name, weights) for name, hpwl in _hpwl_by_net(
        placed_parts, circuit, ctx,
        hpwl_objective=hpwl_objective, hpwl_nets=hpwl_nets,
        pad_extents=pad_extents))


def _segment_intersects(a1, a2, b1, b2) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = orient(a1, a2, b1)
    o2 = orient(a1, a2, b2)
    o3 = orient(b1, b2, a1)
    o4 = orient(b1, b2, a2)
    return o1 * o2 < 0 and o3 * o4 < 0


def _estimate_crossings(
    placed_parts: list[PlacedPart],
    circuit,
    ctx=None,
    *,
    exclude_plane_nets: bool = False,
) -> int:
    """Star-topology crossing estimate over part centroids.

    ``exclude_plane_nets`` drops nets that get POURED rather than routed as
    tracks, using :func:`ratnest.is_plane_net` so the objective and every judge
    share one definition of "plane net". ⚠ Roughly half our pins are plane pins,
    and KRT records its own ``--ignore-nets`` as a *correctness* requirement for
    an honest airwire objective rather than a refinement.
    """
    if circuit is None:
        return 0

    pos_by_ref = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed_parts}
    segments = []
    for _name, all_refs in _net_ref_lists(circuit, ctx):
        if exclude_plane_nets and is_plane_net(_name):
            continue
        refs = [ref for ref in all_refs if ref in pos_by_ref]
        if len(refs) < 2:
            continue
        anchor = min(refs, key=lambda ref: (pos_by_ref[ref][0], pos_by_ref[ref][1], ref))
        for ref in refs:
            if ref != anchor:
                segments.append((anchor, ref, pos_by_ref[anchor], pos_by_ref[ref]))

    return _count_segment_crossings(segments)


def _count_segment_crossings_loop(segments) -> int:
    crossings = 0
    for idx, (a_ref, b_ref, a1, a2) in enumerate(segments):
        for c_ref, d_ref, b1, b2 in segments[idx + 1:]:
            if {a_ref, b_ref}.intersection({c_ref, d_ref}):
                continue
            if _segment_intersects(a1, a2, b1, b2):
                crossings += 1
    return crossings


# Vectorize only when the O(S^2) loop actually costs something.
_VECTORIZED_CROSSINGS_MIN = 40


def _count_segment_crossings_numpy(segments):
    """Vectorized exact equivalent of _count_segment_crossings_loop.

    Returns the crossing count, or None if numpy is unavailable so the caller
    falls back to the loop. Mirrors _segment_intersects exactly: a pair counts
    iff o1*o2 < 0 and o3*o4 < 0 (strict — collinear/touching never counts). The
    orientation products are computed in the same operand order as the scalar
    predicate, so the float64 result is bit-identical.
    """
    try:
        import numpy as np
    except Exception:
        return None

    n = len(segments)
    # Endpoints and integer ref ids per segment.
    a1x = np.empty(n); a1y = np.empty(n)
    a2x = np.empty(n); a2y = np.empty(n)
    ref_ids: dict[str, int] = {}
    sa = np.empty(n, dtype=np.int64)
    sb = np.empty(n, dtype=np.int64)
    for i, (a_ref, b_ref, p1, p2) in enumerate(segments):
        a1x[i], a1y[i] = p1
        a2x[i], a2y[i] = p2
        sa[i] = ref_ids.setdefault(a_ref, len(ref_ids))
        sb[i] = ref_ids.setdefault(b_ref, len(ref_ids))

    dx = a2x - a1x            # (S,) segment direction x
    dy = a2y - a1y            # (S,) segment direction y

    # [i,j] deltas. dA1{x,y}[i,j] = A1[j] - A1[i]; b{x,y}[i,j] = A2[i] - A1[j].
    dA1x = a1x[None, :] - a1x[:, None]
    dA1y = a1y[None, :] - a1y[:, None]
    dA2x1 = a2x[None, :] - a1x[:, None]
    dA2y1 = a2y[None, :] - a1y[:, None]
    bx = a2x[:, None] - a1x[None, :]
    by = a2y[:, None] - a1y[None, :]

    o1 = dx[:, None] * dA1y - dy[:, None] * dA1x   # orient(a1_i,a2_i, a1_j)
    o2 = dx[:, None] * dA2y1 - dy[:, None] * dA2x1  # orient(a1_i,a2_i, a2_j)
    o3 = -dx[None, :] * dA1y + dy[None, :] * dA1x   # orient(a1_j,a2_j, a1_i)
    o4 = dx[None, :] * by - dy[None, :] * bx        # orient(a1_j,a2_j, a2_i)

    cross = (o1 * o2 < 0) & (o3 * o4 < 0)
    shared = (
        (sa[:, None] == sa[None, :])
        | (sa[:, None] == sb[None, :])
        | (sb[:, None] == sa[None, :])
        | (sb[:, None] == sb[None, :])
    )
    hits = cross & ~shared
    return int(np.triu(hits, k=1).sum())


def _count_segment_crossings(segments) -> int:
    """Exact star-topology crossing count. Pairs sharing a ref are skipped."""
    if len(segments) >= _VECTORIZED_CROSSINGS_MIN:
        vectorized = _count_segment_crossings_numpy(segments)
        if vectorized is not None:
            return vectorized
    return _count_segment_crossings_loop(segments)


def _pin_escape_congestion(placed_parts: list[PlacedPart], circuit) -> float:
    if circuit is None:
        return 0.0
    placed = {pp.ref: pp for pp in placed_parts}
    part_by_ref = {part.ref: part for part in circuit.parts if part.ref in placed}
    congestion = 0.0
    refs = sorted(part_by_ref)
    for i, ref in enumerate(refs):
        a = placed[ref]
        try:
            a_pins = len(part_by_ref[ref])
        except Exception:
            a_pins = 2
        for other_ref in refs[i + 1:]:
            b = placed[other_ref]
            dist = max(_distance(a, b), 0.1)
            if dist > 12.0:
                continue
            try:
                b_pins = len(part_by_ref[other_ref])
            except Exception:
                b_pins = 2
            congestion += (a_pins + b_pins) / dist
    return congestion


def _edge_distance(pp: PlacedPart, fp_bboxes, outline) -> float:
    w, h = fp_bboxes.get(pp.footprint, (2.0, 2.0))
    if pp.rot_deg % 180 == 90:
        w, h = h, w
    return min(
        abs(pp.x_mm - w / 2 - outline.x_min),
        abs(outline.x_max - (pp.x_mm + w / 2)),
        abs(pp.y_mm - h / 2 - outline.y_min),
        abs(outline.y_max - (pp.y_mm + h / 2)),
    )


def _placement_envelope(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    margin_mm: float = 3.0,
) -> tuple[float, float, float] | None:
    bounds = _placement_bounds(placed_parts, fp_bboxes)
    if bounds is None:
        return None

    x_min, y_min, x_max, y_max = bounds
    width = max(0.0, x_max - x_min + 2 * margin_mm)
    height = max(0.0, y_max - y_min + 2 * margin_mm)
    return width, height, width * height


def _placement_bounds(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if len(placed_parts) < 2:
        return None

    x_min = float("inf")
    y_min = float("inf")
    x_max = float("-inf")
    y_max = float("-inf")
    for pp in placed_parts:
        w, h = fp_bboxes.get(pp.footprint, (2.0, 2.0))
        if pp.rot_deg % 180 == 90:
            w, h = h, w
        x_min = min(x_min, pp.x_mm - w / 2)
        y_min = min(y_min, pp.y_mm - h / 2)
        x_max = max(x_max, pp.x_mm + w / 2)
        y_max = max(y_max, pp.y_mm + h / 2)
    if not all(math.isfinite(value) for value in (x_min, y_min, x_max, y_max)):
        return None
    return x_min, y_min, x_max, y_max


def _outline_utilization_metrics(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
    *,
    compact_margin_mm: float = 3.0,
) -> dict:
    if outline is None:
        return {}
    bounds = _placement_bounds(placed_parts, fp_bboxes)
    if bounds is None:
        return {}

    x_min, y_min, x_max, y_max = bounds
    body_w = max(0.0, x_max - x_min)
    body_h = max(0.0, y_max - y_min)
    body_area = body_w * body_h
    compact_w = body_w + 2 * compact_margin_mm
    compact_h = body_h + 2 * compact_margin_mm
    compact_area = compact_w * compact_h
    outline_area = max(0.0, outline.width_mm) * max(0.0, outline.height_mm)
    if outline_area <= 0.0:
        return {}

    margin_ratios = {
        "left": max(0.0, x_min - outline.x_min) / max(outline.width_mm, 0.001),
        "right": max(0.0, outline.x_max - x_max) / max(outline.width_mm, 0.001),
        "top": max(0.0, y_min - outline.y_min) / max(outline.height_mm, 0.001),
        "bottom": max(0.0, outline.y_max - y_max) / max(outline.height_mm, 0.001),
    }
    return {
        "footprint_envelope_bbox_mm": {
            "width": body_w,
            "height": body_h,
            "area": body_area,
        },
        "footprint_envelope_area_ratio": min(body_area / outline_area, 1.0),
        "compact_outline_mm": {
            "width": compact_w,
            "height": compact_h,
            "area": compact_area,
        },
        "compact_outline_area_ratio": min(compact_area / outline_area, 1.0),
        "empty_margin_ratios": margin_ratios,
        "max_empty_margin_ratio": max(margin_ratios.values()),
    }


def _outline_oversize_warning(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
) -> str | None:
    if outline is None:
        return None
    metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    compact = metrics.get("compact_outline_mm", {})
    envelope_area = float(compact.get("area", 0.0) or 0.0)
    if not metrics or envelope_area <= 0.0:
        return None
    envelope_w = float(compact.get("width", 0.0) or 0.0)
    envelope_h = float(compact.get("height", 0.0) or 0.0)
    outline_area = max(0.0, outline.width_mm) * max(0.0, outline.height_mm)
    if outline_area <= 0.0:
        return None

    area_ratio = outline_area / envelope_area
    width_slack = outline.width_mm - envelope_w
    height_slack = outline.height_mm - envelope_h
    if area_ratio < 2.5 or width_slack < 10.0 or height_slack < 8.0:
        return None

    return (
        f"board outline is {area_ratio:.1f}x larger than compact footprint "
        f"envelope (estimated compact outline {envelope_w:.1f}x{envelope_h:.1f}mm); "
        "shrink auto-sized boards or redistribute parts if this outline is mechanically fixed"
    )


def _outline_oversize_penalty(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
) -> float:
    """Return a score penalty for sparse placements on generous outlines."""
    if outline is None:
        return 0.0
    if len(placed_parts) < 4:
        return 0.0
    metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    compact = metrics.get("compact_outline_mm", {})
    envelope_area = float(compact.get("area", 0.0) or 0.0)
    if not metrics or envelope_area <= 0.0:
        return 0.0
    envelope_w = float(compact.get("width", 0.0) or 0.0)
    envelope_h = float(compact.get("height", 0.0) or 0.0)
    outline_area = max(0.0, outline.width_mm) * max(0.0, outline.height_mm)
    if outline_area <= 0.0:
        return 0.0

    area_ratio = outline_area / envelope_area
    width_slack = max(0.0, outline.width_mm - envelope_w)
    height_slack = max(0.0, outline.height_mm - envelope_h)
    if area_ratio < 2.0 or width_slack < 8.0 or height_slack < 6.0:
        return 0.0

    ratio_penalty = (area_ratio - 2.0) * 4.0
    slack_penalty = max(0.0, width_slack - 8.0) / 6.0
    slack_penalty += max(0.0, height_slack - 6.0) / 6.0
    return min(ratio_penalty + slack_penalty, 28.0)


def _role_counts(roles: dict[str, PartRole]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for role in roles.values():
        counts[role.role] = counts.get(role.role, 0) + 1
    return counts


def _front_panel_trace_metrics(
    placed_parts: list[PlacedPart],
    circuit,
    roles: dict[str, PartRole],
    *,
    long_span_mm: float = 28.0,
) -> dict:
    if circuit is None:
        return {"count": 0, "span_mm": 0.0, "warnings": []}

    placed_by_ref = {pp.ref: pp for pp in placed_parts}
    has_panel_context = any(
        role.role == "panel_jack" and ref in placed_by_ref
        for ref, role in roles.items()
    )
    has_back_side_service = any(
        str(getattr(pp, "side", "front") or "front").lower() == "back"
        for pp in placed_parts
    )
    if not (has_panel_context or has_back_side_service):
        return {"count": 0, "span_mm": 0.0, "warnings": []}

    front_panel_refs = {
        ref
        for ref, role in roles.items()
        if role.role in {"panel_jack", "control"}
        and ref in placed_by_ref
        and str(getattr(placed_by_ref[ref], "side", "front") or "front").lower()
        == "front"
    }
    if not front_panel_refs:
        return {"count": 0, "span_mm": 0.0, "warnings": []}

    count = 0
    total_span = 0.0
    warnings: list[str] = []
    for net in circuit.get_nets():
        if is_nc_net(net):
            continue
        refs: list[str] = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref in placed_by_ref and ref not in refs:
                refs.append(ref)
        panel_refs = [ref for ref in refs if ref in front_panel_refs]
        if not panel_refs:
            continue
        front_non_panel_refs = [
            ref
            for ref in refs
            if ref not in front_panel_refs
            and str(getattr(placed_by_ref[ref], "side", "front") or "front").lower()
            == "front"
        ]
        if not front_non_panel_refs:
            continue

        span = max(
            _distance(placed_by_ref[panel_ref], placed_by_ref[other_ref])
            for panel_ref in panel_refs
            for other_ref in front_non_panel_refs
        )
        if span < long_span_mm:
            continue
        count += 1
        total_span += span
        warnings.append(
            f"{getattr(net, 'name', 'net')}: front-panel trace span is "
            f"{span:.1f}mm; move service electronics to the back or route away "
            "from the control face"
        )

    return {"count": count, "span_mm": total_span, "warnings": warnings}


def _role_warnings(
    placed_parts: list[PlacedPart],
    circuit,
    roles: dict[str, PartRole],
    fp_bboxes: dict[str, tuple[float, float]],
    outline=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    ctx=None,
) -> list[str]:
    placed_by_ref = {pp.ref: pp for pp in placed_parts}
    warnings: list[str] = []

    if outline is not None:
        oversize_warning = _outline_oversize_warning(
            placed_parts, fp_bboxes, outline
        )
        if oversize_warning:
            warnings.append(oversize_warning)
        for ref, role in roles.items():
            if role.role != "connector" or ref not in placed_by_ref:
                continue
            distance = _edge_distance(placed_by_ref[ref], fp_bboxes, outline)
            if distance > 5.0:
                warnings.append(
                    f"{ref}: connector is {distance:.1f}mm from nearest board edge"
                )

    if circuit is None:
        return warnings

    part_by_ref = {part.ref: part for part in circuit.parts}
    # nets_by_ref/token_cache are circuit-invariant; when a LayoutContext is
    # supplied, reuse its precomputed caches (byte-identical to the live walk,
    # since ctx.pin_nets[ref] IS pin_net_names(part) captured at build time).
    # A live fallback covers any ref absent from the cache (e.g. ref=None) so
    # the resulting dict is exactly the same as the ctx=None comprehension.
    if ctx is not None:
        nets_by_ref = {
            ref: (
                set(ctx.pin_nets[ref])
                if ref in ctx.pin_nets
                else set(pin_net_names(part))
            )
            for ref, part in part_by_ref.items()
        }
        token_cache = ctx.part_tokens
    else:
        nets_by_ref = {
            ref: set(pin_net_names(part)) for ref, part in part_by_ref.items()
        }
        token_cache = None
    decap_pad_distances = measure_decap_pad_distances(
        placed_parts,
        circuit,
        fp_geometries or {},
        roles,
    )

    if outline is not None:
        panel_like_count = sum(
            1 for role in roles.values() if role.role in {"panel_jack", "control"}
        )
        primary_refs = [
            ref
            for ref, role in roles.items()
            if role.role in {"ic", "regulator"} and ref in placed_by_ref
        ]
        if (
            len(primary_refs) == 1
            and panel_like_count < 2
            and len(part_by_ref) <= 16
        ):
            primary = placed_by_ref[primary_refs[0]]
            center_x = outline.x_min + outline.width_mm / 2.0
            center_y = outline.y_min + outline.height_mm / 2.0
            distance = math.hypot(primary.x_mm - center_x, primary.y_mm - center_y)
            limit = max(5.0, min(outline.width_mm, outline.height_mm) * 0.18)
            if distance > limit:
                warnings.append(
                    f"{primary.ref}: primary IC/regulator is {distance:.1f}mm from board center"
                )

    parent_roles = {"ic", "regulator", "module_socket"}
    for ref, role in roles.items():
        if role.role != "decoupling_cap" or ref not in placed_by_ref:
            continue
        cap_nets = nets_by_ref.get(ref, set())
        candidates = [
            other_ref
            for other_ref, other_role in roles.items()
            if other_ref in placed_by_ref
            and other_role.role in parent_roles
            and cap_nets.intersection(nets_by_ref.get(other_ref, set()))
        ]
        if not candidates:
            warnings.append(f"{ref}: no placed IC/regulator shares its supply nets")
            continue
        pad_distance = decap_pad_distances.get(ref)
        if pad_distance is not None:
            if pad_distance.average_pad_distance_mm > 6.0:
                warnings.append(
                    f"{ref}: decoupling cap pads average "
                    f"{pad_distance.average_pad_distance_mm:.1f}mm from "
                    f"{pad_distance.parent_ref} supply pads"
                )
            continue
        owner_ref = _select_primary_owner_ref(
            ref,
            part_by_ref,
            nets_by_ref,
            roles,
            placed_by_ref,
            require_signal=False,
            require_power_and_ground=True,
            token_cache=token_cache,
        )
        nearest_ref = owner_ref or min(
            candidates,
            key=lambda other_ref: _distance(
                placed_by_ref[ref], placed_by_ref[other_ref]
            ),
        )
        distance = _distance(placed_by_ref[ref], placed_by_ref[nearest_ref])
        if distance > 5.0:
            warnings.append(
                f"{ref}: decoupling cap is {distance:.1f}mm from {nearest_ref}"
            )

    # Bulk (reservoir) caps want to sit near the regulator/IC they reservoir,
    # but on a looser budget than a decoupling cap -- 15 mm vs 5 mm. Same
    # mechanism as the decap warning above (nearest supply-sharing parent),
    # keyed on the value-based bulk classifier rather than the decoupling role.
    for ref, role in roles.items():
        if ref not in placed_by_ref or role.role == "decoupling_cap":
            continue
        part = part_by_ref.get(ref)
        if part is None or not is_bulk_cap(part):
            continue
        cap_nets = nets_by_ref.get(ref, set())
        candidates = [
            other_ref
            for other_ref, other_role in roles.items()
            if other_ref in placed_by_ref
            and other_role.role in parent_roles
            and cap_nets.intersection(nets_by_ref.get(other_ref, set()))
        ]
        if not candidates:
            continue
        nearest_ref = min(
            candidates,
            key=lambda other_ref: _distance(
                placed_by_ref[ref], placed_by_ref[other_ref]
            ),
        )
        distance = _distance(placed_by_ref[ref], placed_by_ref[nearest_ref])
        if distance > 15.0:
            warnings.append(
                f"{ref}: bulk cap is {distance:.1f}mm from {nearest_ref}"
            )

    for ref, role in roles.items():
        if role.role != "signal_passive" or ref not in placed_by_ref:
            continue
        passive_nets = nets_by_ref.get(ref, set())
        signal_nets = {
            name
            for name in passive_nets
            if not POWER_NET_RE.match(name) and not GND_NET_RE.match(name)
        }
        if not signal_nets:
            continue
        candidates = [
            other_ref
            for other_ref, other_role in roles.items()
            if other_ref in placed_by_ref
            and other_role.role in parent_roles
            and signal_nets.intersection(nets_by_ref.get(other_ref, set()))
        ]
        if not candidates:
            continue
        owner_ref = _select_primary_owner_ref(
            ref,
            part_by_ref,
            nets_by_ref,
            roles,
            placed_by_ref,
            require_signal=True,
            token_cache=token_cache,
        )
        nearest_ref = owner_ref or min(
            candidates,
            key=lambda other_ref: _distance(
                placed_by_ref[ref], placed_by_ref[other_ref]
            ),
        )
        distance = _distance(placed_by_ref[ref], placed_by_ref[nearest_ref])
        if distance > 12.0:
            warnings.append(
                f"{ref}: signal passive is {distance:.1f}mm from {nearest_ref}"
            )

    for ref, role in roles.items():
        if role.role != "crystal" or ref not in placed_by_ref:
            continue
        ic_refs = [
            other_ref
            for other_ref, other_role in roles.items()
            if other_ref in placed_by_ref and other_role.role == "ic"
        ]
        if not ic_refs:
            continue
        nearest_ref = min(
            ic_refs,
            key=lambda other_ref: _distance(
                placed_by_ref[ref], placed_by_ref[other_ref]
            ),
        )
        distance = _distance(placed_by_ref[ref], placed_by_ref[nearest_ref])
        if distance > 10.0:
            warnings.append(
                f"{ref}: crystal is {distance:.1f}mm from nearest IC {nearest_ref}"
            )

    grid_refs = [
        ref
        for ref, role in roles.items()
        if ref in placed_by_ref
        and (
            role.role in {"panel_jack", "control"}
            or is_ui_grid_part(part_by_ref.get(ref))
        )
    ]
    if len(grid_refs) >= 2:
        xs = [placed_by_ref[ref].x_mm for ref in grid_refs]
        ys = [placed_by_ref[ref].y_mm for ref in grid_refs]
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)
        clean_grid = points_form_clean_grid(
            [
                (placed_by_ref[ref].x_mm, placed_by_ref[ref].y_mm)
                for ref in grid_refs
            ]
        )
        tall_panel = (
            outline is not None
            and outline.height_mm >= outline.width_mm * 1.6
            and outline.height_mm >= 60.0
        )
        if tall_panel:
            expected_y_span = min(55.0, outline.height_mm * 0.45)
            if not clean_grid and x_span > max(12.0, outline.width_mm * 0.45):
                warnings.append(
                    "visible/mechanical subjects are not aligned into clean columns"
                )
            if y_span < expected_y_span:
                warnings.append(
                    "visible/mechanical subjects are bunched instead of distributed vertically"
                )
        else:
            expected_x_span = min(20.0, outline.width_mm * 0.35) if outline else 12.0
            if len(grid_refs) <= 4 and y_span > 2.0 and not clean_grid:
                warnings.append(
                    "visible/mechanical subjects are not aligned into a clean row"
                )
            if x_span < expected_x_span:
                warnings.append(
                    "visible/mechanical subjects are bunched instead of distributed"
                )

    return warnings


def _warning_penalty(warnings: list[str]) -> float:
    penalty = min(len(warnings) * 5.0, 25.0)
    if any("bunched instead of distributed" in warning for warning in warnings):
        penalty += 18.0
    return penalty


def score_placement_quick(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    outline=None,
    keepouts=None,
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    clearance_mm: float = 0.5,
    ctx=None,
    hpwl_nets: str = DEFAULT_HPWL_NETS,
    overlap_objective: str = DEFAULT_OVERLAP_OBJECTIVE,
) -> LayoutScore:
    """Cheap scorer for candidates with known violations.

    Runs only validate + HPWL + penalty. Skips congestion, crossings, and
    power corridor analysis.

    ⛔⛔ **``hpwl_nets`` reaches here and ``crossing_objective`` does not, and
    that asymmetry is deliberate.** This function skips crossings entirely, so
    the crossing objective is genuinely irrelevant to it; HPWL is the only
    quality term it has. A net-set change moves that term by **24-52 %**, so an
    unthreaded quick scorer would rank an illegal candidate against a legal one
    on two different scales -- the same class of defect as the parallel-worker
    one, in the candidate PRE-FILTER rather than in the worker.
    ⚠ ``hpwl_objective`` is still not threaded here (it was not when that plan
    shipped either). It changes the same term's *points* rather than its scale,
    and converting it is that plan's call, not this one's.

    ⛔ **``hpwl_weights`` does not reach here either, and that is a fact about
    this function rather than a decision.** It has **no weighted term at all**
    -- its only HPWL contribution is ``min(total_hpwl / 50.0, 30.0)``, and
    ``total_hpwl`` is a dose invariant by construction. Threading the knob here
    would be dead code, so it is named here instead of added.

    ⭐⭐ **``overlap_objective`` DOES reach here, and the asymmetry with
    ``hpwl_weights`` is deliberate rather than inconsistent.** This function
    carries its own ``len(validation.overlaps) * 25.0``, so leaving it binary
    would rank pre-filter candidates on a different overlap scale from the full
    scorer -- the same defect shape ``hpwl_nets`` was threaded here to avoid.
    ⚠ See :func:`score_placement`'s note: this is the pre-filter at
    ``engine.py:918``, whose ``(not ok, penalty, name)`` sort puts every legal
    candidate above every illegal one regardless of penalty, so the knob's
    effect here is confined to ordering *within* the illegal group.
    """
    validation = validate(
        placed_parts,
        circuit,
        fp_bboxes,
        clearance_mm=clearance_mm,
        outline=outline,
        keepouts=keepouts,
        cutouts=cutouts,
        fp_geometries=fp_geometries,
        ctx=ctx,
    )
    roles = ctx.roles if ctx is not None else (classify_parts(circuit) if circuit is not None else {})
    warnings = _role_warnings(
        placed_parts,
        circuit,
        roles,
        fp_bboxes,
        outline,
        fp_geometries=fp_geometries,
        ctx=ctx,
    )
    front_panel_trace = _front_panel_trace_metrics(placed_parts, circuit, roles)
    warnings.extend(front_panel_trace["warnings"])
    outline_metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    total_hpwl = _total_hpwl(placed_parts, circuit, ctx, hpwl_nets=hpwl_nets)

    penalty = 0.0
    penalty += _overlap_term(validation, clearance_mm, overlap_objective)
    penalty += len(validation.outline_violations) * 20.0
    penalty += len(validation.keepout_violations) * 25.0
    penalty += len(validation.cutout_violations) * 30.0
    penalty += len(validation.missing_refs) * 10.0
    penalty += min(total_hpwl / 50.0, 30.0)
    penalty += min(float(front_panel_trace["span_mm"]) / 12.0, 12.0)
    penalty += _warning_penalty(warnings)
    penalty += _outline_oversize_penalty(placed_parts, fp_bboxes, outline)

    return LayoutScore(
        score=max(0.0, 100.0 - penalty),
        penalty=penalty,
        total_hpwl_mm=total_hpwl,
        overlap_count=len(validation.overlaps),
        outline_violation_count=len(validation.outline_violations),
        keepout_violation_count=len(validation.keepout_violations),
        cutout_violation_count=len(validation.cutout_violations),
        missing_count=len(validation.missing_refs),
        warning_count=len(warnings),
        role_counts=_role_counts(roles),
        warnings=warnings,
        footprint_envelope_bbox_mm=dict(outline_metrics.get("footprint_envelope_bbox_mm", {})),
        footprint_envelope_area_ratio=float(outline_metrics.get("footprint_envelope_area_ratio", 0.0) or 0.0),
        compact_outline_mm=dict(outline_metrics.get("compact_outline_mm", {})),
        compact_outline_area_ratio=float(outline_metrics.get("compact_outline_area_ratio", 0.0) or 0.0),
        empty_margin_ratios=dict(outline_metrics.get("empty_margin_ratios", {})),
        max_empty_margin_ratio=float(outline_metrics.get("max_empty_margin_ratio", 0.0) or 0.0),
        front_panel_trace_count=int(front_panel_trace["count"]),
        front_panel_trace_mm=float(front_panel_trace["span_mm"]),
    )


def score_placement(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    outline=None,
    keepouts=None,
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    clearance_mm: float = 0.5,
    board_layers: int = 2,
    ctx=None,
    crossing_objective: str = DEFAULT_CROSSING_OBJECTIVE,
    hpwl_objective: str = DEFAULT_HPWL_OBJECTIVE,
    hpwl_nets: str = DEFAULT_HPWL_NETS,
    hpwl_weights=DEFAULT_HPWL_WEIGHTS,
    overlap_objective: str = DEFAULT_OVERLAP_OBJECTIVE,
) -> LayoutScore:
    """Full placement score.

    ``crossing_objective`` selects how the crossing term is computed and scaled;
    see :func:`_crossing_term`. **Default ``"legacy"``, and on that path this
    function is byte-identical to before the parameter existed.**

    ``hpwl_objective`` selects whether the two HPWL terms measure over part
    centroids (``"centroid"``, the default and the shipped behaviour) or over
    pad positions (``"pads"``).

    ``hpwl_nets`` selects which nets the two HPWL terms measure over: ``"all"``
    (the default and the shipped behaviour) or ``"plane_free"``, which drops the
    nets this stack pours. ⭐ It is the *set*, where ``hpwl_objective`` is the
    *points*; the two compose.

    ``hpwl_weights`` selects the plane-net coefficients :func:`_net_weight`
    applies inside the **weighted** term -- ``"legacy"`` (the default and the
    shipped behaviour, GND 2.0 / POWER 1.6) or a lighter dose from
    :data:`HPWL_WEIGHTS`. ⭐ It is the *dose* on the same axis ``hpwl_nets``
    sets to zero; see :data:`HPWL_WEIGHTS` for why that is one lever and not
    two. ⚠ It reaches the **weighted** term only -- ``total_hpwl`` is an
    invariant of the dose by construction, which is what makes the two terms
    separable in a decomposition.

    ``overlap_objective`` selects how the overlap penalty is computed:
    ``"count"`` (the default and the shipped behaviour,
    ``len(overlaps) * 25.0``) or ``"area"``, which grades each overlapping pair
    by its penetration depth against ``clearance_mm``. See
    :func:`_overlap_term`.

    ⛔⛔⛔ **What ``"area"`` can and cannot do, measured before it was built
    (WS-Z1, six power boards).** The overlap term is **0 at every placement this
    engine ships** -- zero overlaps on 12 of 12 board-arms -- but non-zero on
    **60-90 % of scored trials**, so the only mechanism available to it is the
    search *trajectory*. And ``penalty`` is the LAST thing the search consults:
    ``refinement._is_better`` is lexicographic on ``_hard_count`` and reaches
    the penalty comparison on **48.9-61.3 %** of its calls, of which
    **26.0-48.1 %** have an illegal side. That last figure is this knob's entire
    reachable surface -- large enough to matter, and **not** 100 %.
    ⭐ Grade it on whether the search reaches a different FIXED POINT (the
    placement digest), never on the term's own value at the answer: that value
    is 0 on both arms by construction.
    """
    # ⭐ Computed ONCE and fed to every consumer -- the HPWL terms, the MST
    # crossing term and the validator's worst-net report. Under the ``mst``
    # default the marginal geometry cost of pad-HPWL is therefore ~zero, and
    # the objective and the report cannot disagree about where a pad is.
    pad_points = None
    if circuit is not None and (hpwl_objective == HPWL_OBJECTIVE_PADS
                                or crossing_objective == CROSSING_OBJECTIVE_MST):
        pad_points = _placement_pad_points(placed_parts, circuit, fp_geometries)
    pad_extents = (_net_pad_extents(pad_points)
                   if (pad_points is not None
                       and hpwl_objective == HPWL_OBJECTIVE_PADS) else None)

    validation = validate(
        placed_parts,
        circuit,
        fp_bboxes,
        clearance_mm=clearance_mm,
        outline=outline,
        keepouts=keepouts,
        cutouts=cutouts,
        fp_geometries=fp_geometries,
        ctx=ctx,
        hpwl_objective=hpwl_objective,
        hpwl_nets=hpwl_nets,
        pad_points=pad_points,
    )
    roles = ctx.roles if ctx is not None else (classify_parts(circuit) if circuit is not None else {})
    warnings = _role_warnings(
        placed_parts,
        circuit,
        roles,
        fp_bboxes,
        outline,
        fp_geometries=fp_geometries,
        ctx=ctx,
    )
    front_panel_trace = _front_panel_trace_metrics(placed_parts, circuit, roles)
    warnings.extend(front_panel_trace["warnings"])
    power_plan = None
    if circuit is not None:
        power_plan = plan_power_routes(
            circuit, placed_parts, board_layers=board_layers, ctx=ctx
        )
        warnings.extend(power_plan.warnings)
    outline_metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    total_hpwl = _total_hpwl(placed_parts, circuit, ctx,
                             hpwl_objective=hpwl_objective,
                             hpwl_nets=hpwl_nets,
                             pad_extents=pad_extents)
    weighted_hpwl = _weighted_hpwl(placed_parts, circuit, ctx,
                                   hpwl_objective=hpwl_objective,
                                   hpwl_nets=hpwl_nets,
                                   hpwl_weights=hpwl_weights,
                                   pad_extents=pad_extents)
    if crossing_objective == CROSSING_OBJECTIVE_MST:
        crossing_count = _mst_crossings(placed_parts, circuit, fp_geometries,
                                        pad_points=pad_points)
    else:
        crossing_count = _estimate_crossings(
            placed_parts,
            circuit,
            ctx,
            exclude_plane_nets=(crossing_objective == CROSSING_OBJECTIVE_SIGNAL),
        )
    pin_escape_score = _pin_escape_congestion(placed_parts, circuit)
    congestion_map = build_congestion_map(
        placed_parts,
        circuit,
        outline=outline,
        keepouts=keepouts,
        power_plan=power_plan,
        board_layers=board_layers,
        ctx=ctx,
    )
    congestion_score = (
        pin_escape_score
        + congestion_map.peak_demand
        + congestion_map.average_demand * 0.5
    )
    congestion_regions = [
        region.label for region in congestion_map.top_regions(limit=5)
    ]

    penalty = 0.0
    penalty += _overlap_term(validation, clearance_mm, overlap_objective)
    penalty += len(validation.outline_violations) * 20.0
    penalty += len(validation.keepout_violations) * 25.0
    penalty += len(validation.cutout_violations) * 30.0
    penalty += len(validation.missing_refs) * 10.0
    penalty += min(total_hpwl / 50.0, 30.0)
    penalty += min(weighted_hpwl / 120.0, 20.0)
    penalty += _crossing_term(crossing_count, crossing_objective)
    penalty += min(congestion_score / 8.0, 15.0)
    penalty += min(float(front_panel_trace["span_mm"]) / 12.0, 12.0)
    penalty += _warning_penalty(warnings)
    penalty += _outline_oversize_penalty(placed_parts, fp_bboxes, outline)
    if power_plan is not None:
        for intent in power_plan.route_intents:
            if intent.width_mm >= 0.8 and intent.span_mm > 50.0:
                layer_relief = 0.45 if board_layers >= 4 else 1.0
                penalty += min((intent.span_mm - 50.0) / 10.0, 10.0) * layer_relief

    return LayoutScore(
        score=max(0.0, 100.0 - penalty),
        penalty=penalty,
        total_hpwl_mm=total_hpwl,
        overlap_count=len(validation.overlaps),
        outline_violation_count=len(validation.outline_violations),
        keepout_violation_count=len(validation.keepout_violations),
        cutout_violation_count=len(validation.cutout_violations),
        missing_count=len(validation.missing_refs),
        warning_count=len(warnings),
        weighted_hpwl_mm=weighted_hpwl,
        crossing_count=crossing_count,
        congestion_score=congestion_score,
        role_counts=_role_counts(roles),
        power_net_count=len(power_plan.nets) if power_plan is not None else 0,
        congestion_regions=congestion_regions,
        power_corridor_count=(
            len(power_plan.corridors) if power_plan is not None else 0
        ),
        warnings=warnings,
        footprint_envelope_bbox_mm=dict(outline_metrics.get("footprint_envelope_bbox_mm", {})),
        footprint_envelope_area_ratio=float(outline_metrics.get("footprint_envelope_area_ratio", 0.0) or 0.0),
        compact_outline_mm=dict(outline_metrics.get("compact_outline_mm", {})),
        compact_outline_area_ratio=float(outline_metrics.get("compact_outline_area_ratio", 0.0) or 0.0),
        empty_margin_ratios=dict(outline_metrics.get("empty_margin_ratios", {})),
        max_empty_margin_ratio=float(outline_metrics.get("max_empty_margin_ratio", 0.0) or 0.0),
        front_panel_trace_count=int(front_panel_trace["count"]),
        front_panel_trace_mm=float(front_panel_trace["span_mm"]),
    )
