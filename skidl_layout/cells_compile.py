# -*- coding: utf-8 -*-
"""The cell compiler -- turn a harvested arrangement into a compiled cell.

Two halves, deliberately separable:

* **The geometric half** (:func:`compile_cell`) needs no router, no board on
  disk and no KiCadRoutingTools. It derives the access map from pad geometry,
  sweeps the transit map, resolves the HPWL representative points and runs the
  acceptance checks that do not require copper. It is **deterministic and
  offline**, which is what lets a cell library be content-addressed.
* **The routed half** (:func:`probe_cell_access`, :func:`probe_transit`) writes
  a throwaway board and asks KRT. It *confirms* the geometric half rather than
  replacing it, and gate ``T2t`` is the confirmation that matters: ⛔ **the
  geometric sweep may UNDER-report; if a routed probe ever finds it
  OVER-reporting that is a defect, not a tolerance.**

⚠ **Why the geometric half exists at all**, given the plan asks for a routed
probe: the plan's decisive measurement (WS-T4, bail-out 4) grades a
**placement-only** arm, and stamping is conditional on its outcome. A cell needs
an access map before it can have HPWL points, but it does not need *routed*
copper to be placed. Building the offline half first is what makes the bail-out
reachable without first building a stamper the bail-out might condemn.

⚠ **Both probes escape in a STRAIGHT LINE only.** A net that could get out by
dog-legging around a neighbour reads ``BLOCKED``. That is the same safe
direction the transit sweep takes -- it denies an escape that exists, it never
invents one -- and it is recorded here so the number is not read as exact.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from .cells import (
    QUANTUM_DP,
    CellPort,
    LayoutCell,
    _q,
    resolve_hpwl_points,
    sweep_transit,
)

__all__ = [
    "CellAcceptance",
    "ShrinkResult",
    "compile_cell",
    "escape_corridor_clear",
    "harvest_copper",
    "probe_cell_access",
    "shrink_cell",
    "write_cell_board",
]

_SIDES = ("N", "E", "S", "W")

#: What a layer change costs a port, in millimetres of equivalent probe length.
#: ⭐ Not a physical length -- a rank-ordering weight, so that a top-layer escape
#: always outranks an inner-layer one of the same geometric length. The number
#: is the arc's own ``ESCAPE_LANE_MM`` scaled by 10, i.e. "a via is worth about
#: ten lane widths of detour", which is a stated convention and not a
#: measurement. ⚠ Change it only with a graded arm.
VIA_COST_MM = 9.048


@dataclass(frozen=True)
class CellAcceptance:
    """The compiler's verdict on one cell. ⚠ Report-only by default (gate T2)."""

    name: str
    digest: str
    every_net_has_a_port: bool
    unreachable_nets: tuple[str, ...]
    transit_complete: bool
    hpwl_points_complete: bool
    rotation_invariant: bool
    box_area_mm2: float
    naive_area_mm2: float
    box_not_larger_than_naive: bool
    unresolved_footprints: tuple[str, ...]
    #: ⛔⛔ The smallest AABB separation between any two members, on whichever
    #: axis separates them (the validator's own rule -- two parts clear if
    #: EITHER axis does). Negative means their **physical envelopes overlap**.
    min_member_gap_mm: float = 0.0
    members_legal: bool = True

    @property
    def area_ratio(self) -> float:
        return (0.0 if not self.naive_area_mm2
                else round(self.box_area_mm2 / self.naive_area_mm2, 4))

    @property
    def ok(self) -> bool:
        """⛔⛔ ``box_not_larger_than_naive`` is **deliberately not in here.**

        MEASURED on the first three cells harvested off ``lt3844_buck_manual``:
        every one is 15-18 % **larger** than the naive edge-to-edge row
        (9.59 vs 8.33, 10.17 vs 8.99, 9.85 vs 8.69 mm^2). That is not a defect
        and it is not noise -- **the human deliberately bought escape room with
        area**, putting 4.0 mm between two 0805s a packer would have put 2.85 mm
        apart. The plan's criterion belongs to WS-T2's *shrink ladder*, i.e. to
        **generated** cells whose box is being minimised; applied to a
        **harvested** cell it grades the human's judgement as a failure. It is
        still computed and still reported (see :attr:`area_ratio`), because "how
        much area does a hand arrangement spend on routability" is one of the
        more interesting numbers this plan produces.

        ⛔⛔ ``members_legal`` IS in here, and it was missing until 2026-07-31.
        MEASURED, and it let 22 of 24 generated cells through: every criterion
        above is about *reachability*, and not one of them asks whether the
        members physically fit. That was safe while every cell was **harvested**
        off a real board -- legality held by construction, and the whole
        harvested library sits at a comfortable 0.837 mm minimum gap. It stopped
        being safe the moment :func:`~skidl_layout.cells.synthesise_cell` let a
        cell be *authored*, and nothing noticed, because a cell is only checked
        against ``validator.validate`` when it is **placed** and no arm places a
        generated cell. ⭐ A cell whose parts overlap can still get every net
        out, still sweep a transit map and still beat the naive row -- it just
        cannot be built.
        """
        return (self.every_net_has_a_port and self.transit_complete
                and self.hpwl_points_complete and self.rotation_invariant
                and self.members_legal
                and not self.unresolved_footprints)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "digest": self.digest, "ok": self.ok,
            "every_net_has_a_port": self.every_net_has_a_port,
            "unreachable_nets": list(self.unreachable_nets),
            "transit_complete": self.transit_complete,
            "hpwl_points_complete": self.hpwl_points_complete,
            "rotation_invariant": self.rotation_invariant,
            "box_area_mm2": self.box_area_mm2,
            "naive_area_mm2": self.naive_area_mm2,
            "area_ratio": self.area_ratio,
            "box_not_larger_than_naive": self.box_not_larger_than_naive,
            "unresolved_footprints": list(self.unresolved_footprints),
            "min_member_gap_mm": self.min_member_gap_mm,
            "members_legal": self.members_legal,
        }


# --------------------------------------------------------------------------- #
# The geometric access probe
# --------------------------------------------------------------------------- #
def _obstruction_rects(cell: LayoutCell, layer: int, clearance: float,
                       skip_pad_labels: frozenset[str]):
    """Every obstruction on ``layer``, inflated by ``clearance``.

    ⛔ Through-board pads and every via count on **every** layer -- the same
    blank-layer exception the transit sweep obeys.
    """
    rects = []
    for pad in cell.pads:
        if pad.label in skip_pad_labels:
            continue
        if not (pad.through_board or layer == 0):
            continue
        x0, y0, x1, y1 = pad.bounds
        rects.append((x0 - clearance, y0 - clearance,
                      x1 + clearance, y1 + clearance))
    if cell.copper is not None:
        for segment in cell.copper.segments:
            if segment.layer != layer:
                continue
            half = segment.width / 2.0 + clearance
            rects.append((min(segment.x1, segment.x2) - half,
                          min(segment.y1, segment.y2) - half,
                          max(segment.x1, segment.x2) + half,
                          max(segment.y1, segment.y2) + half))
        for via in cell.copper.vias:
            half = via.size / 2.0 + clearance
            rects.append((via.x - half, via.y - half,
                          via.x + half, via.y + half))
    return rects


def escape_corridor_clear(cell: LayoutCell, pad, side: str, layer: int,
                          *, lane_mm: float, clearance_mm: float,
                          same_net_labels: frozenset[str]) -> tuple[bool, float]:
    """Can ``pad`` reach ``side`` on ``layer`` down a straight ``lane_mm`` lane?

    Returns ``(clear, distance_mm)``. The corridor is the band of width
    ``lane_mm`` centred on the pad, running from the pad's own edge to the box
    edge. Obstructions are every other net's copper on that layer (plus every
    through-board pad and via), inflated by ``clearance_mm``.

    ⭐ Same-net copper does not obstruct -- a net escaping past its own other pad
    is connected, not shorted, which is the distinction ``obstacle_map``'s
    ``add_net_stubs_as_obstacles`` makes on KRT's side.
    """
    x0, y0, x1, y1 = pad.bounds
    half = lane_mm / 2.0
    if side in ("N", "S"):
        band_lo, band_hi = pad.x - half, pad.x + half
        if side == "N":
            run_lo, run_hi = 0.0, y0
            distance = y0
        else:
            run_lo, run_hi = y1, cell.height
            distance = cell.height - y1
        corridor = (band_lo, run_lo, band_hi, run_hi)
    else:
        band_lo, band_hi = pad.y - half, pad.y + half
        if side == "W":
            run_lo, run_hi = 0.0, x0
            distance = x0
        else:
            run_lo, run_hi = x1, cell.width
            distance = cell.width - x1
        corridor = (run_lo, band_lo, run_hi, band_hi)

    # ⛔ Tolerance, not ``< 0.0``: a pad flush with the box edge computes its
    # distance as ``width - (x + w/2)``, which in binary floating point lands a
    # few ulps either side of zero. Without the tolerance a pad ON the edge --
    # the most favourable escape there is, and the common case, since the box is
    # the tight union of the members' envelopes -- reads BLOCKED half the time,
    # depending on the parity of the last bit.
    tolerance = 10.0 ** -QUANTUM_DP
    if distance < -tolerance:
        return False, 0.0
    if corridor[2] <= corridor[0] + tolerance or corridor[3] <= corridor[1]:
        # The pad already touches this edge: a zero-length escape is clear.
        return True, max(0.0, _q(distance))

    for rect in _obstruction_rects(cell, layer, clearance_mm, same_net_labels):
        if (rect[0] < corridor[2] and rect[2] > corridor[0]
                and rect[1] < corridor[3] and rect[3] > corridor[1]):
            return False, _q(distance)
    return True, max(0.0, _q(distance))


def derive_access(cell: LayoutCell, *, layers, lane_mm: float,
                  clearance_mm: float) -> tuple[CellPort, ...]:
    """The access map, from geometry alone.

    Every ``(escaping net, side, layer)`` triple gets an entry, so **absent
    means "never asked"** stays meaningful and a 2-layer cell dropped on a
    4-layer board can be told apart from a cell that said no.

    ⭐ ``FAVORED`` is the cheapest quartile of a net's non-``BLOCKED`` ports,
    ranked on ``distance + VIA_COST_MM * (layer != 0)``.

    ⛔ **A surface pad cannot reach an inner or bottom layer without a via, and
    v1 places no vias**, so a net with no through-board pad is ``BLOCKED`` on
    every layer but the top. That is not a modelling shortcut -- it is the same
    reason the hand boards route with **zero vias**, which is the advantage this
    whole plan is trying to bottle.
    """
    ports: list[CellPort] = []
    for net in cell.escaping_nets:
        same_net = frozenset(pad.label for pad in cell.pads
                             if pad.local_net == net)
        scored: list[tuple[float, CellPort]] = []
        for layer in sorted({int(v) for v in layers}):
            for side in _SIDES:
                best: tuple[float, float, float] | None = None
                for pad in cell.pads:
                    if pad.local_net != net:
                        continue
                    if layer != 0 and not pad.through_board:
                        continue
                    clear, distance = escape_corridor_clear(
                        cell, pad, side, layer, lane_mm=lane_mm,
                        clearance_mm=clearance_mm, same_net_labels=same_net)
                    if not clear:
                        continue
                    exit_x, exit_y = _edge_point(cell, pad, side)
                    if best is None or distance < best[0]:
                        best = (distance, exit_x, exit_y)
                if best is None:
                    ports.append(CellPort(local_net=net, side=side, layer=layer,
                                          access="BLOCKED"))
                    continue
                cost = best[0] + (VIA_COST_MM if layer != 0 else 0.0)
                port = CellPort(local_net=net, side=side, layer=layer,
                                access="ACCESSIBLE", x=best[1], y=best[2],
                                cost=cost)
                scored.append((cost, port))
                ports.append(port)
        if scored:
            # ⛔ Ranked on (cost, side, layer) so a tie cannot depend on list
            # order -- the same total-tie-break discipline section 3.6 demands.
            ordered = sorted(scored, key=lambda item: (item[0], item[1].side,
                                                       item[1].layer))
            favoured = max(1, len(ordered) // 4)
            promote = {(p.side, p.layer) for _cost, p in ordered[:favoured]}
            ports = [replace(p, access="FAVORED")
                     if (p.local_net == net and (p.side, p.layer) in promote
                         and p.access == "ACCESSIBLE") else p
                     for p in ports]
    return tuple(ports)


def _edge_point(cell: LayoutCell, pad, side: str) -> tuple[float, float]:
    """Where a straight escape from ``pad`` crosses ``side``."""
    if side == "N":
        return _q(pad.x), 0.0
    if side == "S":
        return _q(pad.x), _q(cell.height)
    if side == "W":
        return 0.0, _q(pad.y)
    return _q(cell.width), _q(pad.y)


# --------------------------------------------------------------------------- #
# compile
# --------------------------------------------------------------------------- #
def compile_cell(
    cell: LayoutCell,
    *,
    fab_spec=None,
    stackup: int | None = None,
    clearance_mm: float | None = None,
    lane_mm: float | None = None,
    min_track_mm: float | None = None,
    body_obstructs: bool = False,
) -> tuple[LayoutCell, CellAcceptance]:
    """Fill a harvested cell's two maps and its HPWL points, and grade it.

    ⛔ **The fab spec is not optional in spirit even though it is in signature.**
    A cell compiled at the stock 0.2 mm Default class is not a cell that fits our
    fab -- that is open defect 6 reappearing *inside* the compiler, which is why
    the plan makes controlling the ``.kicad_pro`` rule 5. With no spec the
    conservative arc defaults are used and ``fab`` is recorded as ``""`` so the
    provenance is visible rather than assumed.

    Returns ``(compiled_cell, acceptance)``. The acceptance verdict is
    **report-only by default** -- a gate that fails on a known-open finding
    leaves a driver permanently red and useless as a regression harness.
    """
    from .power_escape import ESCAPE_LANE_MM, lane_from_fab

    n_layers = int(stackup if stackup is not None
                   else getattr(fab_spec, "copper_layers", cell.stackup) or 2)
    # ⚠ The DESIGN clearance (``clearance_mm``, 0.25 on ``oshpark-2l``), not
    # the published floor: both sweeps ask "will a real trace fit here", and a
    # real trace is routed at the design value. ``lane_mm`` keeps
    # ``lane_from_fab``'s min-clearance basis unchanged, because that one asks
    # the different question "can a via physically fit".
    clearance = float(clearance_mm if clearance_mm is not None
                      else getattr(fab_spec, "clearance_mm", 0.25) or 0.25)
    lane = float(lane_mm if lane_mm is not None
                 else (lane_from_fab(fab_spec) or ESCAPE_LANE_MM))
    min_track = float(min_track_mm if min_track_mm is not None
                      else getattr(fab_spec, "min_track_mm", 0.1524) or 0.1524)
    layers = tuple(range(n_layers))

    ports = derive_access(cell, layers=layers, lane_mm=lane,
                          clearance_mm=clearance)
    transit = sweep_transit(cell, layers=layers, clearance_mm=clearance,
                            min_track_mm=min_track,
                            body_obstructs=bool(body_obstructs))
    staged = replace(cell, ports=ports, transit=transit,
                     layers_defined=(0,) if cell.copper is None
                     else tuple(sorted({s.layer for s in cell.copper.segments}
                                       | {0})),
                     stackup=n_layers,
                     fab=str(getattr(fab_spec, "name", "") or "")).normalised()
    hpwl_points = resolve_hpwl_points(staged)
    compiled = replace(staged, hpwl_points=hpwl_points,
                       meta={**dict(cell.meta),
                             "compiled": {"clearance_mm": clearance,
                                          "lane_mm": lane,
                                          "min_track_mm": min_track,
                                          "body_obstructs": bool(body_obstructs),
                                          "layers": list(layers)}}).normalised()

    acceptance = grade_cell(compiled, layers=layers,
                            clearance_mm=clearance)
    return compiled, acceptance


def naive_area_mm2(cell: LayoutCell, clearance_mm: float = 0.0) -> float:
    """The area the same members would occupy in a row at ``clearance_mm``.

    ⭐ The plan's acceptance criterion is *"box area <= the naive arrangement"*.
    The naive arrangement is each member's own **physical envelope** -- the same
    ``transformed_physical_bounds`` the box was built from -- placed edge to edge
    along ``x`` with ``clearance_mm`` between neighbours. That is the weakest
    legal arrangement of the same parts, and the one a cell must at least match.

    ⛔ **Measured trap, and it produced a false negative before it was fixed:**
    computing this from *pad* rectangles while the box comes from *body ∪ pads*
    compares two different envelopes and reports every cell as oversized. The
    member extents are stored on :class:`~skidl_layout.cells.CellMember` for
    exactly this reason.
    """
    members = cell.part_members
    if not members:
        return cell.area_mm2
    widths = [m.w for m in members]
    heights = [m.h for m in members]
    total_w = sum(widths) + clearance_mm * max(0, len(widths) - 1)
    return _q(total_w * max(heights))


def grade_cell(cell: LayoutCell, *, layers,
               clearance_mm: float = 0.0) -> CellAcceptance:
    """The acceptance gate for a compiled cell (plan section WS-T2)."""
    from .cells import hpwl_winner_labels, rotate_cell

    unreachable = tuple(net for net in cell.escaping_nets
                        if not cell.escapable_sides(net))
    wanted = {(int(layer), axis) for layer in layers for axis in ("EW", "NS")}
    have = {(entry.layer, entry.axis) for entry in cell.transit}
    hpwl_complete = all(net in cell.hpwl_points
                        for net in cell.escaping_nets
                        if net not in unreachable)
    base = hpwl_winner_labels(cell)
    invariant = all(hpwl_winner_labels(rotate_cell(cell, deg)) == base
                    for deg in (90, 180, 270))
    naive = naive_area_mm2(cell, clearance_mm)
    gap = min_member_gap(cell)
    return CellAcceptance(
        name=cell.name,
        digest=cell.digest,
        every_net_has_a_port=not unreachable,
        unreachable_nets=unreachable,
        transit_complete=wanted <= have,
        hpwl_points_complete=hpwl_complete,
        rotation_invariant=invariant,
        box_area_mm2=cell.area_mm2,
        naive_area_mm2=naive,
        box_not_larger_than_naive=cell.area_mm2 <= naive + 1e-6,
        unresolved_footprints=tuple(
            dict(cell.meta).get("unresolved_footprints") or ()),
        min_member_gap_mm=gap,
        # ⚠ The floor is the DESIGN clearance, the same number the transit sweep
        # and the escape probe use, so "legal" here means the same thing it
        # means on a board rather than merely "not literally intersecting".
        members_legal=gap >= float(clearance_mm) - 1e-6,
    )


def min_member_gap(cell: LayoutCell) -> float:
    """Smallest AABB separation between any two part members, in mm.

    ⭐ **The validator's own rule, not a stricter one:** two parts clear each
    other if **either** axis separates them, so the per-pair gap is
    ``max(gap_x, gap_y)`` and the cell's is the minimum over pairs. Negative
    means two physical envelopes overlap and the cell cannot be built.
    ``inf`` for a cell with fewer than two members -- nothing to collide with.
    """
    members = cell.part_members
    if len(members) < 2:
        return float("inf")
    worst = float("inf")
    ordered = sorted(members, key=lambda m: m.local_ref)
    for index, a in enumerate(ordered):
        for b in ordered[index + 1:]:
            gap_x = abs(a.dx - b.dx) - (a.w + b.w) / 2.0
            gap_y = abs(a.dy - b.dy) - (a.h + b.h) / 2.0
            worst = min(worst, max(gap_x, gap_y))
    return _q(worst)


# --------------------------------------------------------------------------- #
# The box-shrink ladder (cell-toolchain plan, WS-U3)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ShrinkResult:
    """What the ladder did, as data rather than as a log line."""

    cell: LayoutCell
    original_box: tuple[float, float]
    final_box: tuple[float, float]
    original_area: float
    final_area: float
    original_area_ratio: float
    final_area_ratio: float
    rungs_tried: int
    rungs_accepted: int
    stopped_by: tuple[str, str]        # (x reason, y reason)
    probe_backoff: int = 0             # rungs given back to keep a routed escape
    probe_exhausted: bool = False      # the backoff bound was hit

    @property
    def shrank(self) -> bool:
        return self.final_area < self.original_area - 1e-9

    @property
    def area_kept(self) -> float:
        """Fraction of the original box area the ladder could **not** remove."""
        return (0.0 if not self.original_area
                else round(self.final_area / self.original_area, 4))

    def to_dict(self) -> dict:
        return {
            "name": self.cell.name, "digest": self.cell.digest,
            "original_box": list(self.original_box),
            "final_box": list(self.final_box),
            "original_area": self.original_area, "final_area": self.final_area,
            "original_area_ratio": self.original_area_ratio,
            "final_area_ratio": self.final_area_ratio,
            "area_kept": self.area_kept,
            "rungs_tried": self.rungs_tried,
            "rungs_accepted": self.rungs_accepted,
            "stopped_by": list(self.stopped_by),
            "probe_backoff": self.probe_backoff,
            "probe_exhausted": self.probe_exhausted,
            "shrank": self.shrank,
        }


def _compact_axis(cell: LayoutCell, axis: str, compaction: float,
                  min_gap: float) -> LayoutCell | None:
    """Pull every member ``compaction`` mm closer together along ``axis``.

    Members move **toward the box centre in proportion to their distance from
    it**, which keeps a symmetric arrangement symmetric and is a pure function
    of the input (no search, no ordering). Pads travel with their member.
    Returns ``None`` when two envelopes would end up closer than ``min_gap`` —
    that is the ladder's collision rung, and it is checked on the *physical*
    envelopes because that is the envelope ``validator`` overlaps on.
    """
    members = cell.part_members
    if len(members) < 2:
        return None
    span = cell.width if axis == "x" else cell.height
    if span <= compaction + 1e-9:
        return None
    centre = span / 2.0
    factor = (span - compaction) / span

    def _pos(member):
        return member.dx if axis == "x" else member.dy

    def _extent(member):
        return member.w if axis == "x" else member.h

    moved: dict[str, float] = {}
    for member in members:
        moved[member.local_ref] = centre + (_pos(member) - centre) * factor

    # ⛔ Collision on the moved envelopes, pairwise on this axis only: two parts
    # that already miss each other on the *other* axis may legally overlap here
    # (a two-row arrangement is the common case), so a 1-D test alone would
    # refuse shrinks that are perfectly legal.
    by_ref = {m.local_ref: m for m in members}
    refs = sorted(by_ref)
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            ma, mb = by_ref[a], by_ref[b]
            gap_this = (abs(moved[a] - moved[b])
                        - (_extent(ma) + _extent(mb)) / 2.0)
            other_a = ma.dy if axis == "x" else ma.dx
            other_b = mb.dy if axis == "x" else mb.dx
            other_ext = ((ma.h + mb.h) if axis == "x" else (ma.w + mb.w)) / 2.0
            gap_other = abs(other_a - other_b) - other_ext
            if gap_other >= min_gap - 1e-9:
                continue                       # they clear on the other axis
            if gap_this < min_gap - 1e-9:
                return None

    shift = {ref: moved[ref] - _pos(by_ref[ref]) for ref in refs}
    lo = min(moved[ref] - _extent(by_ref[ref]) / 2.0 for ref in refs)
    hi = max(moved[ref] + _extent(by_ref[ref]) / 2.0 for ref in refs)

    new_members = []
    for member in cell.members:
        delta = shift.get(member.local_ref, 0.0)
        if axis == "x":
            new_members.append(replace(member, dx=_q(member.dx + delta - lo)))
        else:
            new_members.append(replace(member, dy=_q(member.dy + delta - lo)))
    new_pads = []
    for pad in cell.pads:
        delta = shift.get(pad.local_ref, 0.0)
        if axis == "x":
            new_pads.append(replace(pad, x=_q(pad.x + delta - lo)))
        else:
            new_pads.append(replace(pad, y=_q(pad.y + delta - lo)))

    # ⚠ Copper is NOT transformed here: a segment between two members that moved
    # is no longer connected to either. ``shrink_cell`` refuses a cell carrying
    # internal copper outright rather than emit a broken stub.
    kwargs = {"width": _q(hi - lo)} if axis == "x" else {"height": _q(hi - lo)}
    return replace(cell, members=tuple(new_members), pads=tuple(new_pads),
                   ports=(), transit=(), hpwl_points={}, **kwargs).normalised()


def shrink_cell(
    cell: LayoutCell,
    *,
    fab_spec=None,
    step_mm: float = 0.1,
    min_gap_mm: float | None = None,
    max_rungs: int = 200,
    probe=None,
    max_probe_backoff: int = 40,
    **compile_kwargs,
) -> ShrinkResult:
    """The deterministic 0.1 mm ladder: **width first, then height**.

    Each rung compacts the arrangement by one more ``step_mm``, re-compiles from
    scratch and keeps the result only if :attr:`CellAcceptance.ok` still holds.
    The ladder stops at the first rung that fails; it does **not** keep climbing
    past a failure, because a box that stops accepting and then accepts again is
    a sign the acceptance test is non-monotone and should be read, not searched
    around.

    ⛔⛔ **``probe`` is applied as a BACKOFF, not inside the rung loop, and the
    reason is measured.** ``probe`` is WS-U1's routed access probe — four KRT
    invocations per call. Running it per rung would cost ~1000 routes across this
    corpus (over an hour) to protect a boundary the geometric test is *nearly*
    right about: **21 of 24 harvested cells shrink to the geometric floor with
    every routed escape intact**. So phase 1 climbs geometrically while recording
    every accepted cell, and phase 2 walks that history **backwards**, probing,
    and returns the tightest box that still routes. ⭐ On the three cells that do
    lose an escape (`cc2_rc` twice, `cf_rc` — all compensation networks losing
    `VC`/`ITH` eastward or westward) the backoff pays for itself; on the other
    21 it costs exactly one probe.

    ⚠ ``max_probe_backoff`` bounds phase 2. Hitting it sets
    :attr:`ShrinkResult.probe_exhausted` and returns the **original** cell —
    never a box whose escapes were never confirmed.

    ⭐⭐ **This is the one place ``box area <= naive`` is the RIGHT criterion.**
    The executed plan measured it wrong for *harvested* cells — every hand cell
    is 1.04-1.60x the naive row because the human bought escape room with area,
    and grading that as a failure grades the human's judgement. Here the box is
    being **minimised**, so the ratio is the ladder's own score and
    :attr:`ShrinkResult.final_area_ratio` reports where it landed.

    ⛔ **A cell carrying internal copper is returned unchanged.** Moving a member
    would leave its segments behind, and silently emitting a track that no
    longer lands on a pad is worse than not shrinking.

    ``min_gap_mm`` defaults to the compile clearance — two members closer than
    the design clearance is a board the validator would reject.
    """
    original = cell.normalised()
    compiled, acceptance = compile_cell(original, fab_spec=fab_spec,
                                        **compile_kwargs)
    original_area = compiled.area_mm2
    original_ratio = acceptance.area_ratio
    base = ShrinkResult(
        cell=compiled, original_box=(compiled.width, compiled.height),
        final_box=(compiled.width, compiled.height),
        original_area=original_area, final_area=original_area,
        original_area_ratio=original_ratio, final_area_ratio=original_ratio,
        rungs_tried=0, rungs_accepted=0, stopped_by=("skipped", "skipped"))
    if compiled.copper is not None:
        return replace(base, stopped_by=("internal-copper", "internal-copper"))
    if len(compiled.part_members) < 2:
        return replace(base, stopped_by=("single-member", "single-member"))

    clearance = float(dict(compiled.meta).get("compiled", {}).get(
        "clearance_mm", 0.25) or 0.25)
    min_gap = float(min_gap_mm if min_gap_mm is not None else clearance)
    step = float(step_mm)

    best, best_acceptance = compiled, acceptance
    tried = accepted = 0
    reasons: list[str] = []
    history: list[tuple[LayoutCell, CellAcceptance]] = [(compiled, acceptance)]
    for axis in ("x", "y"):
        # ⛔ ``max_rungs`` bounds the TOTAL attempts on this axis, not the run
        # since the last acceptance -- the ladder re-bases on each winner, so a
        # per-run bound would never be reached and the loop would not terminate.
        reason = "max-rungs"
        for _rung in range(max_rungs):
            tried += 1
            candidate = _compact_axis(best, axis, step, min_gap)
            if candidate is None:
                reason = "collision"
                break
            # ⛔ MEASURED, and without it the ladder reports nonsense: a cell
            # whose members share a coordinate on this axis (every in-line
            # divider does) has nothing to pull together there, so the
            # proportional compaction is a no-op and the loop "accepts" 200
            # rungs that changed nothing. Progress is the box actually getting
            # smaller, not the request being made.
            span_before = best.width if axis == "x" else best.height
            span_after = candidate.width if axis == "x" else candidate.height
            if span_after >= span_before - 1e-6:
                reason = "no-progress"
                break
            try:
                built, verdict = compile_cell(candidate, fab_spec=fab_spec,
                                              **compile_kwargs)
            except Exception:                                  # noqa: BLE001
                reason = "compile-error"
                break
            if not verdict.ok:
                reason = "acceptance"
                break
            accepted += 1
            best, best_acceptance = built, verdict
            history.append((built, verdict))
        reasons.append(reason)

    # -- phase 2: give rungs back until the ROUTER agrees ------------------- #
    backoff, exhausted = 0, False
    if probe is not None and len(history) > 1:
        index = len(history) - 1
        while index >= 0:
            if backoff > max_probe_backoff:
                exhausted = True
                best, best_acceptance = history[0]
                break
            if probe(history[index][0]):
                best, best_acceptance = history[index]
                break
            index -= 1
            backoff += 1
        else:                                                  # pragma: no cover
            best, best_acceptance = history[0]

    return ShrinkResult(
        cell=best, original_box=(compiled.width, compiled.height),
        final_box=(best.width, best.height),
        original_area=original_area, final_area=best.area_mm2,
        original_area_ratio=original_ratio,
        final_area_ratio=best_acceptance.area_ratio,
        rungs_tried=tried, rungs_accepted=accepted,
        stopped_by=(reasons[0], reasons[1]),
        probe_backoff=backoff, probe_exhausted=exhausted)


# --------------------------------------------------------------------------- #
# The routed half -- confirmation, not replacement
# --------------------------------------------------------------------------- #
class _PseudoPin:
    __slots__ = ("num", "name", "net", "part", "func")

    def __init__(self, num, net, part):
        self.num = str(num)
        self.name = str(num)
        self.net = net
        self.part = part
        self.func = None


class _PseudoNet:
    __slots__ = ("name", "_pins", "is_ncnet")

    def __init__(self, name):
        self.name = str(name)
        self._pins: list = []
        self.is_ncnet = False

    def get_pins(self):
        return self._pins


class _PseudoPart:
    """The attribute surface ``writer.write_kicad_pcb`` actually reads.

    ⭐ Same trick ``snapshot.py`` uses for the parallel workers: the writer is
    duck-typed all the way down, so a cell's throwaway board does not need a
    live skidl ``Circuit`` (which a harvested cell has no way to reconstruct --
    it came off a board, not out of a netlist).
    """

    __slots__ = ("ref", "footprint", "value", "name", "pins", "hiername",
                 "hiertuple", "lib", "fields")

    def __init__(self, ref, footprint, value=""):
        self.ref = str(ref)
        self.footprint = str(footprint)
        self.value = str(value or ref)
        self.name = str(ref)
        self.pins: list[_PseudoPin] = []
        self.hiername = f"cell/{ref}"
        self.hiertuple = ("cell", str(ref))
        self.lib = ""
        self.fields = {}

    def get_pins(self, num=None, silent=False):
        """⛔⛔ **The quote-stripping is not defensive, it is the bug fix.**

        MEASURED 2026-07-31, and it made the whole routed probe a no-op: the
        writer resolves a pad's net by calling ``part.get_pins(str(pad[1]))``
        (``writer.py:705``), and by that point ``_prepare_footprint_for_board``
        has **quoted** the pad number, so the string handed in is ``'"1"'`` and
        not ``'1'``. A live skidl ``Part`` matches it anyway; this duck-typed
        stand-in did not, so **every pad on every probe board was written with
        no net**, KRT read "Only 0 pad(s)" for each one, declared the board
        already routed, and the probe reported nothing.

        ⭐ That is exactly the class of failure gate ``T2t`` exists to catch and
        why the executed plan leaving the probe **built but never run** was the
        risk it was: a probe that silently measures nothing looks identical to a
        probe that found everything reachable. Fixed here rather than in
        ``writer.py``, which 1195 byte-identity tests gate.
        """
        if num is None:
            return list(self.pins)
        wanted = str(num)
        if len(wanted) >= 2 and wanted[0] == '"' and wanted[-1] == '"':
            wanted = wanted[1:-1]
        return [pin for pin in self.pins if pin.num == wanted]

    def __len__(self):
        return len(self.pins)

    def __iter__(self):
        return iter(self.pins)


class _PseudoCircuit:
    __slots__ = ("parts", "_nets")

    def __init__(self, parts, nets):
        self.parts = list(parts)
        self._nets = list(nets)

    def get_nets(self):
        return list(self._nets)


def _pseudo_circuit(cell: LayoutCell, extra_ports=()):
    """A duck-typed circuit for the cell's members plus optional probe ports."""
    nets: dict[str, _PseudoNet] = {}
    parts: dict[str, _PseudoPart] = {}
    for member in cell.part_members:
        parts[member.local_ref] = _PseudoPart(member.local_ref, member.footprint)
    for pad in cell.pads:
        if not pad.local_net or pad.local_ref not in parts:
            continue
        net = nets.setdefault(pad.local_net, _PseudoNet(pad.local_net))
        pin = _PseudoPin(pad.pad, net, parts[pad.local_ref])
        parts[pad.local_ref].pins.append(pin)
        net._pins.append(pin)
    for ref, footprint, net_name in extra_ports:
        part = _PseudoPart(ref, footprint)
        net = nets.setdefault(net_name, _PseudoNet(net_name))
        pin = _PseudoPin("1", net, part)
        part.pins.append(pin)
        net._pins.append(pin)
        parts[ref] = part
    ordered_nets = [nets[name] for name in sorted(nets)]
    ordered_parts = [parts[ref] for ref in sorted(parts)]
    return _PseudoCircuit(ordered_parts, ordered_nets)


#: A one-pad SMD footprint from the stock KiCad library, used as the probe port.
#: ⭐ A real library footprint rather than a synthesised one: the writer loads
#: footprints from disk, so an invented name would have to be written to disk
#: first, and a stock part keeps the throwaway board loadable by KiCad.
PROBE_FOOTPRINT = "TestPoint:TestPoint_Pad_D1.0mm"


def write_cell_board(cell: LayoutCell, path: str, *, fp_lib_dirs,
                     fab_spec=None, margin_mm: float = 0.0,
                     probe_ports=()) -> str:
    """Write the cell alone on its own throwaway board.

    ⛔ **A sibling ``.kicad_pro`` is written from the fab spec** when one is
    given (plan rule 5). Without it KRT's front end seeds a minimal project whose
    ``Default`` net class clearance is 0.2 mm and routes the cell at a floor the
    cell's own fab never asked for -- open defect 6, inside the compiler.
    """
    from .constraints import BoardOutline
    from .writer import PlacedPart, write_kicad_pcb

    ports = []
    placed: list[PlacedPart] = []
    ox, oy = margin_mm, margin_mm
    for member in cell.part_members:
        placed.append(PlacedPart(ref=member.local_ref, x_mm=_q(ox + member.dx),
                                 y_mm=_q(oy + member.dy),
                                 rot_deg=float(member.rotation),
                                 footprint=member.footprint, side="front"))
    for index, (net, x, y) in enumerate(probe_ports, start=1):
        ref = f"TP{index}"
        ports.append((ref, PROBE_FOOTPRINT, net))
        placed.append(PlacedPart(ref=ref, x_mm=_q(ox + x), y_mm=_q(oy + y),
                                 rot_deg=0.0, footprint=PROBE_FOOTPRINT,
                                 side="front"))

    circuit = _pseudo_circuit(cell, extra_ports=ports)
    width = cell.width + 2 * margin_mm
    height = cell.height + 2 * margin_mm
    outline = BoardOutline(vertices=[(0.0, 0.0), (width, 0.0),
                                     (width, height), (0.0, height)])
    write_kicad_pcb(placed, circuit, list(fp_lib_dirs or []), path,
                    outline=outline, fab_spec=fab_spec,
                    strict_missing_footprints=False)
    if fab_spec is not None:
        _write_project(path, fab_spec)
    return path


def _write_project(pcb_path: str, fab_spec) -> str:
    """A ``.kicad_pro`` beside the board, carrying the fab's own clearance.

    ⭐ Shape copied from ``skidl_eda``'s own seeding rather than invented: the
    only fields that matter here are the ``Default`` net class's clearance,
    track width, via size and drill, because ``route.py`` resolves the board's
    nominal clearance through the **Default class** and not through
    ``rules.min_clearance``.
    """
    import json

    clearance = float(getattr(fab_spec, "clearance_mm", 0.25) or 0.25)
    track = float(getattr(fab_spec, "track_width_mm", 0.3) or 0.3)
    via = float(getattr(fab_spec, "via_size_mm", 0.6) or 0.6)
    drill = float(getattr(fab_spec, "via_drill_mm", 0.3) or 0.3)
    project = {
        "board": {"design_settings": {
            "rules": {"min_clearance": clearance, "min_track_width": track,
                      "min_through_hole_diameter": drill,
                      "min_via_diameter": via},
        }},
        "net_settings": {"classes": [{
            "name": "Default", "clearance": clearance, "track_width": track,
            "via_diameter": via, "via_drill": drill,
            "microvia_diameter": 0.3, "microvia_drill": 0.1,
            "diff_pair_gap": 0.25, "diff_pair_width": 0.2,
            "diff_pair_via_gap": 0.25, "line_style": 0, "pcb_color": "rgba(0, 0, 0, 0.000)",
            "schematic_color": "rgba(0, 0, 0, 0.000)", "wire_width": 6,
            "bus_width": 12,
        }]},
        "meta": {"filename": os.path.basename(pcb_path)[:-len(".kicad_pcb")]
                 + ".kicad_pro", "version": 3},
    }
    pro_path = pcb_path[:-len(".kicad_pcb")] + ".kicad_pro"
    with open(pro_path, "w", encoding="utf-8") as handle:
        json.dump(project, handle, indent=2, sort_keys=True)
    return pro_path


def probe_cell_access(cell: LayoutCell, workdir: str, *, fp_lib_dirs,
                      fab_spec=None, side_margin_mm: float = 1.5,
                      krt_dir: str | None = None,
                      timeout_s: int = 300) -> dict:
    """Route one probe board per side and report which nets got out.

    ⭐ **This is what turns "routed and then checked for acceptance" into a
    mechanical test rather than an author's assertion.** One route per *side*,
    with every escaping net probed at once.

    ⚠⚠ **The known confound, stated rather than hidden:** probing every net on
    one side simultaneously creates contention that a single-net probe would
    not, so a net can read ``BLOCKED`` because a sibling took the channel rather
    than because the geometry denies it. That biases the routed probe toward
    **under**-reporting, which is the same safe direction as the geometric
    sweep -- and it is why this function *confirms* the geometric map rather
    than replacing it. A per-net probe is 4x more routes for a distinction only
    a contended cell can show.
    """
    from . import krt as krt_mod

    os.makedirs(workdir, exist_ok=True)
    results: dict[str, dict] = {}
    for side in _SIDES:
        probes = []
        for net in cell.escaping_nets:
            point = _side_probe_point(cell, net, side, side_margin_mm)
            if point is not None:
                probes.append((net, point[0], point[1]))
        if not probes:
            results[side] = {"skipped": "no probe points"}
            continue
        side_dir = os.path.join(workdir, side)
        os.makedirs(side_dir, exist_ok=True)
        board = os.path.join(side_dir, f"{cell.name}_{side}.kicad_pcb")
        write_cell_board(cell, board, fp_lib_dirs=fp_lib_dirs,
                         fab_spec=fab_spec, margin_mm=side_margin_mm,
                         probe_ports=probes)
        out = os.path.join(side_dir, f"{cell.name}_{side}_routed.kicad_pcb")
        log = os.path.join(side_dir, "route_log.txt")
        # ⛔⛔ **The internal nets are routed too, and leaving them out made the
        # whole probe useless as a copper harvest.** MEASURED 2026-07-31: with
        # only the escaping nets in scope, a cell's internal net is never routed,
        # so ``harvest_copper`` finds **zero** internal-net copper -- and internal
        # copper is the only copper a cell may LOCK (a boundary stub must stay
        # rippable, plan section 10.2). The consequence was a stamping gate that
        # passed on 6 of 6 boards while asserting something about 0 tracks.
        # ⚠ Internal nets get no probe pad: both their ends are already inside
        # the box, which is what makes them internal.
        routed_nets = ([net for net, _x, _y in probes]
                       + sorted(cell.internal_nets))
        try:
            feedback = krt_mod.route_and_check(
                board, side_dir, krt_dir=krt_dir,
                nets=routed_nets, out_path=out,
                route_log_path=log, timeout_s=timeout_s)
        except Exception as exc:                            # noqa: BLE001
            results[side] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        unrouted = set(feedback.unrouted_nets or ())
        results[side] = {
            "board": board, "routed_board": out,
            "total": len(probes),
            # ⚠ ``routed`` is the ESCAPING nets only -- it answers "did this net
            # get OUT", which is the access map's question. The internal nets
            # are in scope so their copper exists, not so they are graded here.
            "routed": [net for net, _x, _y in probes if net not in unrouted],
            "unrouted": sorted(unrouted & {net for net, _x, _y in probes}),
            "internal_routed": sorted(set(cell.internal_nets) - unrouted),
            "internal_unrouted": sorted(set(cell.internal_nets) & unrouted),
            "drc": feedback.drc_violation_count,
        }
    return results


def harvest_copper(cell: LayoutCell, pcb_path: str, *, margin_mm: float = 0.0,
                   nets=None, clip_to_box: bool = True,
                   krt_dir: str | None = None):
    """Lift a routed probe board's copper back into a :class:`CellCopper`.

    ⭐ **This is why the routed probe pays for itself twice.** WS-U1 runs it to
    confirm the geometric access map; the same routed board is the only source
    of real copper a *harvested* cell can have, because the hand boards carry
    zero segments and zero vias. So the probe run doubles as the copper harvest
    and WS-U2 gets its input for free.

    ``margin_mm`` is the one :func:`write_cell_board` used, so board coordinates
    map back to the cell's local frame by subtracting it -- one number, stated in
    both places, rather than a re-derivation that could drift.

    ⛔ ``clip_to_box`` drops copper that leaves the box. A stamped track outside
    the cell is copper the cell does not own: it would be an obstacle wherever
    the placer happens to drop the cell, on a board that never asked for it.
    ⚠ The clip is on **both endpoints**, so a boundary stub that runs from a pad
    to the probe pad outside is dropped entirely rather than truncated -- a
    half-track ending in mid-air is worse than no track.
    """
    from .cells import CellCopper, CellSegment, CellVia, _q
    from .ratnest import _kicad_parser

    kp = _kicad_parser(krt_dir)
    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    parsed_nets, name_to_id = kp.extract_nets(text, kp.detect_kicad_version(text))
    names = {ident: net.name for ident, net in parsed_nets.items()}
    layer_index = {name: index for index, name in enumerate(
        _copper_layer_names(cell.stackup))}
    wanted = None if nets is None else {str(n) for n in nets}

    def _inside(x, y):
        return (not clip_to_box
                or (-1e-6 <= x <= cell.width + 1e-6
                    and -1e-6 <= y <= cell.height + 1e-6))

    segments, vias = [], []
    for segment in kp.extract_segments(text, name_to_id):
        if getattr(segment, "graphic", False):
            continue
        net = names.get(segment.net_id, "")
        if wanted is not None and net not in wanted:
            continue
        if segment.layer not in layer_index:
            continue
        x1, y1 = segment.start_x - margin_mm, segment.start_y - margin_mm
        x2, y2 = segment.end_x - margin_mm, segment.end_y - margin_mm
        if not (_inside(x1, y1) and _inside(x2, y2)):
            continue
        segments.append(CellSegment(x1=_q(x1), y1=_q(y1), x2=_q(x2), y2=_q(y2),
                                    layer=layer_index[segment.layer],
                                    width=_q(segment.width), local_net=net))
    for via in kp.extract_vias(text, name_to_id):
        net = names.get(via.net_id, "")
        if wanted is not None and net not in wanted:
            continue
        x, y = via.x - margin_mm, via.y - margin_mm
        if not _inside(x, y):
            continue
        vias.append(CellVia(x=_q(x), y=_q(y), size=_q(via.size),
                            drill=_q(via.drill), local_net=net))
    return CellCopper(segments=tuple(segments), vias=tuple(vias)).normalised()


def _copper_layer_names(copper_layers: int):
    from .writer import _copper_layer_names as names

    return names(int(copper_layers))


def _side_probe_point(cell: LayoutCell, net: str, side: str,
                      margin_mm: float) -> tuple[float, float] | None:
    """Where the probe pad for ``net`` sits just outside ``side``'s edge.

    Spread along the edge by the net's index so two probes on one side do not
    land on top of each other -- deterministic, because the net order is sorted.
    """
    nets = cell.escaping_nets
    if net not in nets:
        return None
    index = nets.index(net)
    step = (cell.width if side in ("N", "S") else cell.height) / (len(nets) + 1)
    offset = step * (index + 1)
    half = margin_mm / 2.0
    if side == "N":
        return offset, -half
    if side == "S":
        return offset, cell.height + half
    if side == "W":
        return -half, offset
    return cell.width + half, offset
