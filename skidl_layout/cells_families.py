# -*- coding: utf-8 -*-
"""Generated cell families (cell-toolchain plan, WS-U5 / the plan's WS-T7).

⭐⭐ **Why generation, when harvesting already works.** Every cell this project
owns today was lifted off a **hand** board, so the executed plan's win is
consistent with two mechanisms it could not separate: *rigidity helps*, or *the
human's arrangement helps*. A generated family is the arm that has no human in
it at all -- its arrangement comes from an enumeration, not from a person.
⚠ That does not settle the question either (WS-U0's AUTO-harvest control is the
direct answer); what it does is make the toolchain able to **produce** a cell
for a topology no hand board happens to contain.

⛔⛔ **The first cut of this module enumerated GEOMETRY and ignored WIRING, and
that is why its cells were unroutable.** It laid every family out as a straight
line -- ``row`` or ``column``, one uniform rotation for every member, members in
declaration order -- and then picked the smallest box. Two consequences, both
measured 2026-07-31:

* **A three-part family came out as three parts in a line.** ``divider_cap`` is
  R1, R2 and a feed-forward C1 across the upper leg, and a line puts C1 at one
  end with its ``MID`` pad **9.10 mm** from the ``MID`` junction it belongs to.
  ⭐ Three parts in a row is the arrangement a *chain* wants, and a three-part
  passive group is almost never a chain: the common case is **one net that every
  member touches with exactly one pad** (:func:`junction_net`), which is a
  junction, not a series string.
* **The box was chosen by area alone, so every gap collapsed to the design
  clearance and no channel survived.** 0 of 24 cells were open on both axes and
  the widest lane anywhere was 0.400 mm against a 0.300 mm default track.

⭐⭐ **So the enumeration is now over SHAPES, and the shapes come from the
netlist:**

=========  ==================================================================
``long``   the chain, end to end along one axis, **each member turned so its
           shared pad faces its neighbour**. Long and thin; the interconnect is
           as short as a chain can be. Crossable **NS** (through the gaps
           between members) and never EW.
``short``  every member side by side, parallel, **all bus pads on the same
           side**. Short in the interconnect direction and wide across it;
           crossable **EW** (between the rows) and never NS.
``stack``  ⭐ three or more members with a junction net: a spine of two end to
           end, and the remainder in a second row **aligned to one end of the
           spine, deliberately not centred**. The vacated column above the far
           spine member leaves the spine's own gap clear top to bottom, so this
           is the only shape open on **both** axes.
=========  ==================================================================

⛔ **A shape is generated at several gaps and the winner is the smallest box
that still meets its shape's transit contract** (:data:`SHAPE_CONTRACT`), not
the smallest box. That is a deliberate reversal of the first cut's rule: a lane
costs area, and a cell nobody can route across is worth less than a slightly
bigger one that a stranger can cross. ⚠ When no gap in the ladder meets the
contract the smallest box wins and :attr:`FamilyRun.contract_met` is ``False`` --
reported, never silently substituted.

⛔ **The bound is logged, always.** A silent top-N reads as "covered
everything"; :class:`FamilyRun` carries ``enumerated`` and ``dropped`` so a
report can say what the enumeration did not reach.

⛔ **No routed probe here.** The routed probe (WS-U1) is one KRT invocation per
side per cell; running it inside the sweep would put a router in the middle of a
geometry problem. Pass ``probe=`` to shrink the winners only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import permutations
from typing import Sequence

from .cells import CellCache, LayoutCell, synthesise_cell

__all__ = [
    "FAMILIES",
    "SHAPES",
    "SHAPE_CONTRACT",
    "FamilyRun",
    "FamilySpec",
    "SIZES",
    "Arrangement",
    "arrangements",
    "COURTYARD_MARGIN_MM",
    "arrangement_signature",
    "cell_pad_hpwl",
    "chain_order",
    "classify_three_part_topology",
    "courtyard_overhangs",
    "junction_gap_mm",
    "member_courtyard_gap",
    "member_relation",
    "missing_courtyards",
    "enumerate_chain_arrangements",
    "enumerate_junction_arrangements",
    "generate_all",
    "generate_family",
    "junction_net",
    "member_envelopes",
    "pad_offsets",
]

#: The chip sizes a family is parameterised over. ⚠ Metric names, because that
#: is what the stock KiCad libraries key on -- ``R_0402_1005Metric``.
SIZES = ("0402", "0603", "0805")

#: The arrangement shapes, in a fixed order.
SHAPES = ("long", "short", "stack")

#: ``shape -> the layer-0 axes a winning arrangement must be crossable on``.
#: ⭐ Derived, not chosen: a row of parts blocks the axis it runs *along* and
#: opens the one it runs *across*, so each shape can only ever offer what its
#: own geometry leaves behind. ``stack`` asks for both because that is the whole
#: reason it exists.
SHAPE_CONTRACT: dict[str, tuple[str, ...]] = {
    "long": ("NS",),
    "short": ("EW",),
    "stack": ("EW", "NS"),
}

_METRIC = {"0402": "1005", "0603": "1608", "0805": "2012"}
_LIBRARY = {"R": "Resistor_SMD", "C": "Capacitor_SMD", "L": "Inductor_SMD"}

#: The gap ladder, as an addition to the design clearance. ⭐ It reaches past
#: ``2 x clearance + track_width`` on purpose: at ``oshpark-2l`` (0.25 mm
#: clearance, 0.3 mm routed track) a channel that takes a **real** trace does
#: not exist until the members are **0.8 mm** apart, so a ladder that stopped
#: at +0.5 could not produce a crossable cell of any shape -- and the first
#: cut's, which stopped there, did not. ⚠ The lane a gap buys is
#: ``gap - 2 x clearance``: +0.0 and +0.25 buy nothing at all and are kept only
#: so the contract's failure is visible rather than assumed away.
GAP_LADDER = (0.0, 0.25, 0.5, 0.75, 1.0)


def footprint_for(kind: str, size: str) -> str:
    """``("R", "0805")`` -> ``"Resistor_SMD:R_0805_2012Metric"``."""
    if kind not in _LIBRARY:
        raise ValueError(f"unknown part kind {kind!r}")
    if size not in _METRIC:
        raise ValueError(f"unknown chip size {size!r}")
    return f"{_LIBRARY[kind]}:{kind}_{size}_{_METRIC[size]}Metric"


@dataclass(frozen=True)
class FamilySpec:
    """A topology, as parts and connectivity -- no geometry.

    ``parts`` is ``[(local_ref, kind), ...]``; ``nets`` is
    ``{net: [(local_ref, pad), ...]}``; ``internal`` names the nets that never
    leave the box.
    """

    name: str
    parts: tuple[tuple[str, str], ...]
    nets: dict
    internal: tuple[str, ...] = ()
    note: str = ""

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(ref for ref, _kind in self.parts)

    @property
    def pad_nets(self) -> dict[str, dict[str, str]]:
        """``{local_ref: {pad number: net}}``."""
        out: dict[str, dict[str, str]] = {ref: {} for ref in self.refs}
        for net, pins in self.nets.items():
            for ref, pad in pins:
                out[str(ref)][str(pad)] = str(net)
        return out

    def nets_of(self, ref: str) -> frozenset:
        return frozenset(self.pad_nets[ref].values())

    def pad_on(self, ref: str, net: str) -> str | None:
        """The (single) pad of ``ref`` that sits on ``net``."""
        for pad, name in sorted(self.pad_nets[ref].items()):
            if name == net:
                return pad
        return None


#: ⭐ Four starter topologies, chosen because each is a *local* structure whose
#: arrangement is the whole point -- and because between them they cover the two
#: cases the machinery must handle: cells with an internal net (the snubber's
#: mid-node touches nothing else) and cells without one.
FAMILIES: tuple[FamilySpec, ...] = (
    FamilySpec(
        name="divider",
        parts=(("R1", "R"), ("R2", "R")),
        nets={"IN": [("R1", "1")], "MID": [("R1", "2"), ("R2", "1")],
              "OUT": [("R2", "2")]},
        note="feedback divider; MID goes to the FB pin, so it escapes",
    ),
    FamilySpec(
        name="divider_cap",
        parts=(("R1", "R"), ("R2", "R"), ("C1", "C")),
        nets={"IN": [("R1", "1"), ("C1", "1")],
              "MID": [("R1", "2"), ("R2", "1"), ("C1", "2")],
              "OUT": [("R2", "2")]},
        note="divider with a feed-forward cap across the upper leg; MID is the "
             "junction every member touches with exactly one pad, which is what "
             "makes this the family the `stack` shape exists for",
    ),
    FamilySpec(
        name="lc_filter",
        parts=(("L1", "L"), ("C1", "C")),
        nets={"IN": [("L1", "1")], "MID": [("L1", "2"), ("C1", "1")],
              "OUT": [("C1", "2")]},
        note="series L, shunt C; MID is the filtered rail and escapes",
    ),
    FamilySpec(
        name="rc_snubber",
        parts=(("R1", "R"), ("C1", "C")),
        nets={"A": [("R1", "1")], "SNUB": [("R1", "2"), ("C1", "1")],
              "B": [("C1", "2")]},
        internal=("SNUB",),
        note="series RC across a switch node; SNUB touches nothing else, so it "
             "is the corpus's only INTERNAL generated net",
    ),
)


# --------------------------------------------------------------------------- #
# Reading the topology
# --------------------------------------------------------------------------- #
def junction_net(spec: FamilySpec) -> str | None:
    """The net every member touches with **exactly one** pad, if there is one.

    ⭐⭐ **This is the shape of a real three-part passive group and the first
    cut had no concept of it.** ``divider_cap``'s ``MID`` carries ``R1.2``,
    ``R2.1`` and ``C1.2`` -- one pad each, all three members -- so the group is a
    junction with three legs, not a series chain, and laying it out as a line
    puts two of the three legs a whole cell-length from the node.

    ⛔ A net that touches a member **twice** is not a junction (that member is
    shorted across it), and a net that misses a member is not one either.
    Ties go to the lexicographically smallest name, so this is a pure function.
    """
    refs = set(spec.refs)
    candidates = []
    for net, pins in spec.nets.items():
        touched = [str(ref) for ref, _pad in pins]
        if set(touched) == refs and len(touched) == len(refs):
            candidates.append(str(net))
    return sorted(candidates)[0] if candidates else None


def _share(spec: FamilySpec, a: str, b: str) -> str | None:
    """The net ``a`` and ``b`` are wired together on, smallest name first."""
    common = sorted(spec.nets_of(a) & spec.nets_of(b))
    return common[0] if common else None


def chain_order(spec: FamilySpec) -> tuple[str, ...] | None:
    """The members ordered so that **each is wired to the next**, or ``None``.

    A Hamiltonian path over the "shares a net" graph. ⛔ Several usually exist,
    so the tie-break is total and stated: fewest **split nets** first (a net
    whose pads straddle a non-adjacent pair costs the length it straddles), then
    the lexicographically smallest ``(refs)`` tuple. ⚠ Enumerating permutations
    is only defensible because a family is 2-4 parts; a bigger family needs a
    real path search and should say so rather than quietly time out.
    """
    refs = spec.refs
    if len(refs) > 6:                                          # pragma: no cover
        raise ValueError(f"{spec.name}: chain_order is bounded at 6 members")
    best: list[tuple] = []
    for order in permutations(sorted(refs)):
        if any(_share(spec, order[i], order[i + 1]) is None
               for i in range(len(order) - 1)):
            continue
        index = {ref: i for i, ref in enumerate(order)}
        span = 0
        for _net, pins in sorted(spec.nets.items()):
            positions = [index[str(ref)] for ref, _pad in pins]
            span += max(positions) - min(positions)
        best.append((span, order))
    if not best:
        return None
    best.sort()
    return tuple(best[0][1])


def pad_offsets(footprint: str, fp_lib_dirs) -> dict[str, tuple[float, float]]:
    """``{pad number: (x, y)}`` in the footprint's **own unrotated** frame.

    ⭐ Orientation is derived from this and never assumed. "Pad 1 is on the
    left" is true of every stock chip footprint and is exactly the kind of
    convention that is right until the one footprint where it is not -- and a
    member turned the wrong way puts its shared pad on the far side of the part
    from the neighbour it is wired to, which is the defect this whole rewrite
    exists to remove.
    """
    from .geometry import load_footprint_geometries

    geometry = load_footprint_geometries({footprint}, list(fp_lib_dirs or [])
                                         ).get(footprint)
    if geometry is None:                                       # pragma: no cover
        return {}
    return {str(pad.number): (pad.x_mm, pad.y_mm) for pad in geometry.pads}


def _facing_rotation(offsets: dict, pad: str | None, want: str) -> int:
    """0 or 180 so that ``pad`` ends up on the ``want`` side of the part.

    ``want`` is ``"E"`` (+x) or ``"W"`` (-x). ⚠ 180 degrees leaves a chip
    passive's axis-aligned envelope unchanged, so choosing it never resizes the
    member and the cursor arithmetic below stays valid at every rotation.
    """
    if not pad or pad not in offsets:
        return 0
    centre = (sum(x for x, _y in offsets.values()) / len(offsets)
              if offsets else 0.0)
    on_east = offsets[pad][0] > centre
    return 0 if on_east == (want == "E") else 180


def member_envelopes(spec: FamilySpec, size: str, rotation: int,
                     fp_lib_dirs) -> dict[str, tuple[float, float]]:
    """``{local_ref: (w, h)}`` -- each part's **real** physical envelope, rotated.

    ⛔⛔ **This function exists because a body-size table is the wrong number and
    using one produced 22 unbuildable cells.** MEASURED 2026-07-31: the first cut
    of this generator seeded its step from a hardcoded
    ``{"0402": 1.0, "0603": 1.6, "0805": 2.0}``, i.e. the chip's **body** length.
    A KiCad ``R_0805_2012Metric`` footprint's body-plus-pads envelope is
    **2.85 mm**, not 2.0, so a step of ``2.0 + 0.25`` overlapped two adjacent
    0805s by 0.60 mm — and every family at every size came out with its members
    intersecting. ⭐ The envelope is exactly what ``harvest_cell`` measures for a
    member and what ``validator`` overlaps on, so taking it from the footprint
    library is not defensive, it is using the same definition as everything else.
    """
    from .geometry import load_footprint_geometries
    from .writer import PlacedPart

    footprints = {ref: footprint_for(kind, size) for ref, kind in spec.parts}
    geometries = load_footprint_geometries(set(footprints.values()),
                                           list(fp_lib_dirs or []))
    out: dict[str, tuple[float, float]] = {}
    for ref, footprint in footprints.items():
        geometry = geometries.get(footprint)
        if geometry is None:                                   # pragma: no cover
            out[ref] = (2.0, 2.0)
            continue
        placed = PlacedPart(ref=ref, x_mm=0.0, y_mm=0.0,
                            rot_deg=float(rotation), footprint=footprint,
                            side="front")
        x0, y0, x1, y1 = geometry.transformed_physical_bounds(placed)
        out[ref] = (x1 - x0, y1 - y0)
    return out


# --------------------------------------------------------------------------- #
# The shape enumeration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Arrangement:
    """One authored layout of a family's parts, before compilation."""

    label: str
    members: tuple[tuple, ...]
    shape: str = ""


def _long(spec: FamilySpec, size: str, order, extents, offsets, gap: float):
    """The chain, end to end along +x, each member turned toward its neighbour."""
    members, cursor = [], 0.0
    for index, ref in enumerate(order):
        back = _share(spec, order[index - 1], ref) if index else None
        forward = (_share(spec, ref, order[index + 1])
                   if index + 1 < len(order) else None)
        # ⛔ The BACK constraint wins when a member is wired to both neighbours
        # through the same net (its two links want the same pad on two sides,
        # which no two-pad part can do). Honouring one of them beats splitting
        # the difference and satisfying neither.
        if back is not None:
            rotation = _facing_rotation(offsets[ref], spec.pad_on(ref, back), "W")
        else:
            rotation = _facing_rotation(offsets[ref],
                                        spec.pad_on(ref, forward), "E")
        span = extents[ref][0]
        if index:
            cursor += (extents[order[index - 1]][0] + span) / 2.0 + gap
        members.append((ref, footprint_for(dict(spec.parts)[ref], size),
                        round(cursor, 6), 0.0, rotation))
    return members


def _short(spec: FamilySpec, size: str, order, extents, offsets, gap: float,
           bus: str | None):
    """Every member parallel, stacked along +y, all bus pads on the west side."""
    members, cursor = [], 0.0
    for index, ref in enumerate(order):
        pad = spec.pad_on(ref, bus) if bus else None
        rotation = _facing_rotation(offsets[ref], pad, "W")
        span = extents[ref][1]
        if index:
            cursor += (extents[order[index - 1]][1] + span) / 2.0 + gap
        members.append((ref, footprint_for(dict(spec.parts)[ref], size),
                        0.0, round(cursor, 6), rotation))
    return members


def _stack(spec: FamilySpec, size: str, extents, offsets, gap: float,
           row_gap: float, junction: str):
    """A two-member spine plus a second row aligned to the spine's west end.

    ⭐⭐ **"Aligned, deliberately not centred" is the whole trick and it is
    geometric, not aesthetic.** The spine's own inter-member gap is the only
    column a stranger can cross the box on top to bottom; a second-row member
    centred over the spine sits **exactly on that column** and closes it. Pushed
    west, over the anchor, it leaves the column clear -- and the vacated area
    over the far spine member is what makes the row gap a full-width EW lane.
    That is two passthroughs from one offset, and it is why this is the only
    shape :data:`SHAPE_CONTRACT` asks for both axes from.
    """
    others = {ref: sorted(spec.nets_of(ref) - {junction}) for ref in spec.refs}
    twins = [(others[a], a) for a in sorted(spec.refs)]
    lone = None
    for i, (nets_a, a) in enumerate(twins):
        for nets_b, b in twins[i + 1:]:
            if nets_a and nets_a == nets_b:
                # ⛔ Two members with the SAME other-net are electrically in
                # parallel; lifting either out of the spine gives the same
                # topology, so the choice is arbitrary and must therefore be
                # total: the later ref by sort order goes up.
                lone, anchor = b, a
                break
        if lone:
            break
    if lone is None:
        ordered = sorted(spec.refs)
        anchor, lone = ordered[0], ordered[-1]
    far = next(r for r in sorted(spec.refs) if r not in (anchor, lone))

    rot_anchor = _facing_rotation(offsets[anchor],
                                  spec.pad_on(anchor, junction), "E")
    rot_far = _facing_rotation(offsets[far], spec.pad_on(far, junction), "W")
    rot_lone = _facing_rotation(offsets[lone],
                                spec.pad_on(lone, junction), "E")

    w_anchor, h_anchor = extents[anchor]
    w_far, _h_far = extents[far]
    w_lone, h_lone = extents[lone]
    x_anchor = 0.0
    x_far = (w_anchor + w_far) / 2.0 + gap
    # West-aligned: the lone member's own west edge on the anchor's west edge,
    # so its east edge never reaches past the anchor's into the spine gap.
    x_lone = x_anchor - w_anchor / 2.0 + w_lone / 2.0
    y_lone = -((h_anchor + h_lone) / 2.0 + row_gap)
    kinds = dict(spec.parts)
    return [
        (anchor, footprint_for(kinds[anchor], size), round(x_anchor, 6), 0.0,
         rot_anchor),
        (far, footprint_for(kinds[far], size), round(x_far, 6), 0.0, rot_far),
        (lone, footprint_for(kinds[lone], size), round(x_lone, 6),
         round(y_lone, 6), rot_lone),
    ]


def arrangements(spec: FamilySpec, size: str, *, gap_mm: float,
                 fp_lib_dirs, shape: str = "long",
                 max_arrangements: int = 24,
                 ) -> tuple[list[Arrangement], int]:
    """``(kept, dropped)`` arrangements for one family at one size and shape.

    The enumeration is the gap ladder (and, for ``stack``, the gap ladder
    crossed with a row-gap ladder) in a fixed order, so it is a pure function of
    its arguments. ⚠ Rotation of the **cell** is not enumerated: a cell is
    already rotatable in {0, 90, 180, 270} at placement time, so generating a
    row and a column of the same parts produces two cells with the same area,
    the same maps transposed, and one arbitrary winner. The first cut enumerated
    both and spent half its budget on it.

    ⛔ ``shape`` that a family cannot take returns ``([], 0)`` -- ``stack`` needs
    three or more members and a junction net, and a family without a Hamiltonian
    path has no ``long``. Reported by :class:`FamilyRun`, never faked.
    """
    if shape not in SHAPES:
        raise ValueError(f"unknown shape {shape!r}")
    kinds = dict(spec.parts)
    extents = member_envelopes(spec, size, 0, fp_lib_dirs)
    offsets = {ref: pad_offsets(footprint_for(kinds[ref], size), fp_lib_dirs)
               for ref in spec.refs}
    order = chain_order(spec)
    junction = junction_net(spec)

    out: list[Arrangement] = []
    if shape == "long" and order:
        for extra in GAP_LADDER:
            gap = gap_mm + extra
            out.append(Arrangement(
                label=f"long-gap{gap:.2f}", shape=shape,
                members=tuple(_long(spec, size, order, extents, offsets, gap))))
    elif shape == "short":
        bus = junction or _bus_net(spec)
        stacked = order or tuple(sorted(spec.refs))
        for extra in GAP_LADDER:
            gap = gap_mm + extra
            out.append(Arrangement(
                label=f"short-gap{gap:.2f}", shape=shape,
                members=tuple(_short(spec, size, stacked, extents, offsets,
                                     gap, bus))))
    elif shape == "stack" and junction and len(spec.refs) >= 3:
        for extra in GAP_LADDER:
            for row_extra in GAP_LADDER:
                gap = gap_mm + extra
                row_gap = gap_mm + row_extra
                out.append(Arrangement(
                    label=f"stack-gap{gap:.2f}-row{row_gap:.2f}", shape=shape,
                    members=tuple(_stack(spec, size, extents, offsets, gap,
                                         row_gap, junction))))

    dropped = max(0, len(out) - int(max_arrangements))
    return out[:int(max_arrangements)], dropped


def _bus_net(spec: FamilySpec) -> str | None:
    """The net the most members touch -- ``short``'s spine when there is no
    junction. Ties go to the smallest name, so it is a pure function."""
    ranked = sorted(((-len({str(r) for r, _p in pins}), str(net))
                     for net, pins in spec.nets.items()))
    return ranked[0][1] if ranked else None


# --------------------------------------------------------------------------- #
# THE ARRANGEMENT OBJECTIVE (cell-arrangement-objective plan, WS-A2)
#
# ⛔⛔ Everything below this line is a PARALLEL entry point. ``_meets``,
# :func:`arrangements`, :func:`generate_family` and :func:`generate_all` are
# untouched, no default moves, and the 54 ``family_cache`` digests recompile
# byte-identical -- gate ``A2`` asserts exactly that. Winner SELECTION lives in
# the driver (the ``propose_pair_cells`` precedent: policy in the driver,
# mechanism in the library).
# --------------------------------------------------------------------------- #

#: The three arrangements the human's rule permits for a **junction** -- three
#: 2-pin devices sharing one net. ⛔ A LINE is not among them, and that is the
#: whole content of the rule (``canaries/ideal_arrangements.py`` is the answer
#: key; this is the enumeration that has to rediscover it).
JUNCTION_SHAPES = ("bus3", "ell_a", "ell_b")

#: How much room a junction arrangement leaves between two members whose
#: **facing pads carry the same net**, on top of whichever floor binds.
#: ⭐ Deliberately small, and it is the opposite of what :data:`GAP_LADDER`
#: does. The shape generator widens the gap to open a *channel a stranger can
#: cross*; the human's rule closes it, because two facing same-net pads already
#: block that space and the connection between them is then a stub that costs
#: no channel at all. ⚠ Measured consequence, and it is the finding that made
#: this enumeration necessary: every shipped winner in ``family_cache`` is at
#: ``gap1.00`` (24 ``long``, 24 ``short``, 6 ``stack``) -- the contract test
#: drove the whole library to the widest rung of the ladder.
#:
#: ⛔⛔⛔ **THIS IS NO LONGER THE ONLY FLOOR, AND THE FIRST CUT SHIPPED SEVEN
#: UNBUILDABLE CELLS BECAUSE IT WAS.** Reported by the human 2026-08-03 from
#: the rendered cells: **7 of 7** ``family_cache_junction`` winners overlapped
#: their neighbours' **COURTYARDS** by 0.100-0.310 mm. The acceptance criterion
#: (``min_member_gap`` -> ``CellAcceptance.members_legal``) measures the
#: **body-plus-pads** envelope (``transformed_physical_bounds``); a KiCad
#: courtyard is a *separate, larger* outline, and nothing in this stack was
#: comparing it. See :func:`junction_gap_mm`.
JUNCTION_GAP_EXTRA_MM = 0.05

#: Extra clearance demanded between two courtyards, on top of touching.
#: ⚠ 0.0 means "may touch, may not overlap", which is what KiCad's own
#: courtyard rule tests. Kept nameable so a fab that wants daylight can ask.
COURTYARD_MARGIN_MM = 0.0

#: The pairwise relations :func:`arrangement_signature` classifies.
MEMBER_RELATIONS = ("INLINE", "OFFSET", "SIDE", "T")


# --------------------------------------------------------------------------- #
# The objective's own HPWL term
# --------------------------------------------------------------------------- #
def cell_pad_hpwl(cell) -> float:
    """Half-perimeter wire length over the cell's **own pad positions**, in mm.

    ⛔⛔ **This is NOT the parked board-level ``hpwl_objective="pads"`` knob and
    it does not read ``scoring.py``.** That knob changes how a *board* placer
    scores a *placement*; this is a number about one cell's internal geometry,
    used at GENERATION time to choose between arrangements of the same parts.
    The two never meet, and the board knob stays parked and default OFF.

    ⭐⭐ **Why pads and not centroids, measured on the 0805 junction cell
    2026-08-02.** A centroid HPWL cannot tell two shapes apart when the parts
    sit at the same centres, and on the five authored arrangements it ranks
    ``short`` (9.25 by pads) *above* ``stack`` (6.96) -- the wrong way round.
    Over pads the five come out ``ell_b`` 4.86 < ``ell_a`` 5.11 < ``stack``
    6.96 < ``short`` 9.25 < ``long`` 9.73, which is exactly the human's
    ordering. ⚠ Single-pad nets contribute nothing by construction: a net with
    one pad in the box owns no in-cell wiring.
    """
    by_net: dict[str, list] = {}
    for pad in cell.pads:
        if pad.local_net:
            by_net.setdefault(str(pad.local_net), []).append((pad.x, pad.y))
    total = 0.0
    for _net, points in sorted(by_net.items()):
        if len(points) < 2:
            continue
        xs = [x for x, _y in points]
        ys = [y for _x, y in points]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return round(total, 6)


# --------------------------------------------------------------------------- #
# Reading a three-part topology
# --------------------------------------------------------------------------- #
def classify_three_part_topology(nets) -> str:
    """``"junction"`` | ``"chain"`` | ``"other"`` from connectivity alone.

    ⭐⭐ **The one distinction the whole plan turns on.** A *junction* is one
    net every member touches with exactly one pad -- a divider's ``MID`` -- and
    a line is the one arrangement it must never take, because the two end
    members' junction pads are then separated by the middle member's foreign
    pad and the hop has to go around the outside. A *chain* is three parts in
    series, and there the line is the **right** answer. ⛔ Same three
    footprints, different netlist, different winner: an objective that returns
    the same geometry for both has not read the topology.

    ``nets`` is ``{net: [(local_ref, pad), ...]}``. Junction is tested first --
    a topology that is both is a junction, since the junction net is the
    stronger statement.
    """
    pins = [(str(ref), str(pad)) for pin_list in nets.values()
            for ref, pad in pin_list]
    refs = sorted({ref for ref, _pad in pins})
    if len(refs) != 3:
        return "other"
    for _net, pin_list in sorted(nets.items()):
        touched = [str(ref) for ref, _pad in pin_list]
        if sorted(set(touched)) == refs and len(touched) == 3:
            return "junction"
    links = []
    for _net, pin_list in sorted(nets.items()):
        members = {str(ref) for ref, _pad in pin_list}
        if len(pin_list) == 2 and len(members) == 2:
            links.append(tuple(sorted(members)))
    if len(links) == 2 and len(set(links)) == 2:
        degree: dict[str, int] = {}
        for a, b in links:
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
        if len(degree) == 3 and sorted(degree.values()) == [1, 1, 2]:
            return "chain"
    return "other"


# --------------------------------------------------------------------------- #
# A rotation- and reflection-invariant description of a three-part arrangement
# --------------------------------------------------------------------------- #
def _overlap_fraction(c1: float, e1: float, c2: float, e2: float) -> float:
    """How much two centred 1-D extents overlap, as a fraction of the smaller."""
    lo = max(c1 - e1 / 2.0, c2 - e2 / 2.0)
    hi = min(c1 + e1 / 2.0, c2 + e2 / 2.0)
    smaller = min(e1, e2)
    if smaller <= 0:                                           # pragma: no cover
        return 0.0
    return max(0.0, min(1.0, (hi - lo) / smaller))


def courtyard_overhangs(spec: FamilySpec, size: str,
                        fp_lib_dirs) -> dict[str, tuple[float, float]]:
    """``{local_ref: (over_x, over_y)}`` -- how far each member's **courtyard**
    sticks out past its body-plus-pads envelope, MEASURED from the library.

    ⛔⛔ **The number this codebase was missing, and its absence shipped seven
    unbuildable cells.** ``FootprintGeometry.physical_bounds`` is body ∪ pads;
    ``.bounds`` prefers ``courtyard_bounds`` when the footprint declares one.
    ``min_member_gap`` -- and therefore ``CellAcceptance.members_legal``, and
    therefore every legality gate this stack has -- compares the **first**.
    So an arrangement can sit at exactly the design clearance, pass every gate,
    and still overlap its neighbour's courtyard.

    Measured on the stock KiCad libraries: **0.175 mm** per side at 0402 and
    **0.275-0.280 mm** at 0603/0805, on R, C and L alike. ⛔ **Do not table
    those numbers** -- that is trap 13, and this function is the reason it does
    not need to be re-learned a fourth time.

    ⚠ A footprint with **no** declared courtyard contributes ``(0.0, 0.0)``:
    nothing to overlap is not the same as an overlap of zero, and the caller
    that needs to know reads :func:`missing_courtyards`.
    """
    from .geometry import load_footprint_geometries

    footprints = {ref: footprint_for(kind, size) for ref, kind in spec.parts}
    geometries = load_footprint_geometries(set(footprints.values()),
                                           list(fp_lib_dirs or []))
    out: dict[str, tuple[float, float]] = {}
    for ref, footprint in footprints.items():
        geometry = geometries.get(footprint)
        if geometry is None or geometry.courtyard_bounds is None:
            out[ref] = (0.0, 0.0)
            continue
        px0, py0, px1, py1 = geometry.physical_bounds
        cx0, cy0, cx1, cy1 = geometry.courtyard_bounds
        out[ref] = (round(max(px0 - cx0, cx1 - px1), 6),
                    round(max(py0 - cy0, cy1 - py1), 6))
    return out


def missing_courtyards(spec: FamilySpec, size: str, fp_lib_dirs) -> list[str]:
    """The footprints that declare **no** courtyard at all.

    ⛔ Rule 3 at the seam where it belongs: a courtyard gate run over a
    footprint with no courtyard would pass by observing nothing, which is
    indistinguishable from passing because the geometry is clear.
    """
    from .geometry import load_footprint_geometries

    footprints = sorted({footprint_for(kind, size) for _ref, kind in spec.parts})
    geometries = load_footprint_geometries(set(footprints),
                                           list(fp_lib_dirs or []))
    return [name for name in footprints
            if geometries.get(name) is None
            or geometries[name].courtyard_bounds is None]


def junction_gap_mm(spec: FamilySpec, size: str, *, clearance_mm: float,
                    fp_lib_dirs, margin_mm: float = COURTYARD_MARGIN_MM
                    ) -> float:
    """The gap an authored arrangement is built at -- **derived, never tabled**.

    Two floors, and the second is the one the first cut did not have:

    1. the design **clearance** plus :data:`JUNCTION_GAP_EXTRA_MM`;
    2. ⛔⛔ **the two members' courtyard overhangs plus** ``margin_mm`` -- so
       the courtyards touch at worst and never overlap.

    Measured consequence at ``oshpark-2l`` (clearance 0.25): floor 1 gives
    0.30 at every size, while floor 2 demands **0.350** at 0402 and **0.555-
    0.560** at 0603/0805. Floor 2 binds everywhere, which is exactly why the
    seven cells shipped on 2026-08-02 overlapped.

    ⚠ The worst pair over the family is used, not a per-pair value, so one
    arrangement has one gap and the shape stays uniform.
    """
    overhangs = courtyard_overhangs(spec, size, fp_lib_dirs)
    refs = sorted(spec.refs)
    worst = 0.0
    for index, a in enumerate(refs):
        for b in refs[index + 1:]:
            worst = max(worst,
                        overhangs[a][0] + overhangs[b][0],
                        overhangs[a][1] + overhangs[b][1])
    return round(max(float(clearance_mm) + JUNCTION_GAP_EXTRA_MM,
                     worst + float(margin_mm)), 6)


def member_courtyard_gap(cell, fp_lib_dirs) -> float:
    """Smallest **courtyard** separation between any two part members, in mm.

    ⭐ Deliberately the same rule as
    :func:`~skidl_layout.cells_compile.min_member_gap` -- two parts clear each
    other if **either** axis separates them, so the per-pair gap is
    ``max(gap_x, gap_y)`` and the cell's is the minimum over pairs. The only
    difference is the box: the courtyard, not body ∪ pads. Negative means two
    courtyards overlap and KiCad's own courtyard rule would object.

    ⛔⛔ **And KiCad's DRC will NOT tell you.** Verified 2026-08-03 on a board
    with two 0805 courtyards deliberately driven ~1 mm into each other:
    ``kicad-cli pcb drc --severity-all`` returned **15 violations** --
    ``shorting_items``, ``silk_over_copper``, ``silk_overlap``, ``clearance``,
    ``track_dangling`` -- and **not one courtyard type**. So on the boards this
    stack writes the courtyard test does not run, DRC cannot be the guard, and
    this function is the guard.

    ``inf`` for a cell with fewer than two members -- nothing to collide with.
    """
    from .geometry import load_footprint_geometries
    from .writer import PlacedPart

    members = list(cell.part_members)
    if len(members) < 2:
        return float("inf")
    geometries = load_footprint_geometries({m.footprint for m in members},
                                           list(fp_lib_dirs or []))
    boxes = {}
    for member in members:
        geometry = geometries.get(member.footprint)
        if geometry is None:
            continue
        placed = PlacedPart(ref=member.local_ref, x_mm=member.dx,
                            y_mm=member.dy, rot_deg=float(member.rotation),
                            footprint=member.footprint, side="front")
        # ⭐ ``transformed_bounds`` prefers the courtyard; ``transformed_
        # physical_bounds`` is body u pads. That one-word difference IS the bug
        # this function exists to catch.
        boxes[member.local_ref] = geometry.transformed_bounds(placed)
    if len(boxes) < 2:
        return float("inf")
    refs = sorted(boxes)
    worst = float("inf")
    for index, a in enumerate(refs):
        ax0, ay0, ax1, ay1 = boxes[a]
        for b in refs[index + 1:]:
            bx0, by0, bx1, by1 = boxes[b]
            worst = min(worst, max(max(bx0 - ax1, ax0 - bx1),
                                   max(by0 - ay1, ay0 - by1)))
    return round(worst, 6)


def member_relation(a, b, *, overlap_min: float = 0.6) -> str:
    """How two members sit against each other, independent of the frame.

    ``SIDE``    separated along **both** members' short axis, well overlapped --
                long sides shared (the pair in ``bus3`` / ``ell_*``).
    ``INLINE``  separated along **both** members' long axis, well overlapped --
                end to end, which is what a chain hop wants and what a junction
                must not be built from.
    ``T``       separated along one member's long axis and the other's short --
                the ``ell_a`` tee.
    ``OFFSET``  separated, but the projections barely overlap -- diagonal.

    ⛔ Deliberately **not** a distance. Two arrangements that differ only by a
    gap are the same *shape*, and that is the point: the enumeration's real
    contribution over the shipped generator turns out to be the gap and the
    facing rotations, not new topologies, and a signature that folded the gap in
    would hide that.
    """
    gap_x = abs(a.dx - b.dx) - (a.w + b.w) / 2.0
    gap_y = abs(a.dy - b.dy) - (a.h + b.h) / 2.0
    if gap_x >= gap_y:
        along = (a.w >= a.h, b.w >= b.h)
        overlap = _overlap_fraction(a.dy, a.h, b.dy, b.h)
    else:
        along = (a.h >= a.w, b.h >= b.w)
        overlap = _overlap_fraction(a.dx, a.w, b.dx, b.w)
    if overlap < overlap_min:
        return "OFFSET"
    if all(along):
        return "INLINE"
    if not any(along):
        return "SIDE"
    return "T"


def arrangement_signature(cell, *, overlap_min: float = 0.6) -> tuple:
    """The sorted multiset of :func:`member_relation` over every member pair.

    ⭐⭐ **This is what "the winner landed on one of the shapes the human
    named" means machine-checkably**, and it has to be structural rather than
    coordinate-wise for two independent reasons: the answer key measures its
    envelopes from the footprint library while the recorded calibration table
    was authored against a hardcoded 0805 envelope (the two differ by
    ~0.02-0.05 mm and **that difference is intended**), and two arrangements
    that differ only by their gap are the same shape.

    Measured on the 0805 junction, and the four are distinct:
    ``bus3`` ``(SIDE, SIDE, SIDE)``, ``ell_a`` ``(SIDE, T, T)``,
    ``ell_b`` ``(INLINE, OFFSET, SIDE)``, the forbidden ``line``
    ``(INLINE, INLINE, INLINE)``.
    ⭐ The shipped shapes fold onto them exactly -- ``short`` -> ``bus3``'s,
    ``stack`` -> ``ell_b``'s, and ``long`` -> the **line**'s -- which is the
    structural statement that ``long`` *is* the forbidden arrangement.
    """
    members = sorted(cell.part_members, key=lambda m: m.local_ref)
    return tuple(sorted(
        member_relation(a, b, overlap_min=overlap_min)
        for index, a in enumerate(members) for b in members[index + 1:]))


# --------------------------------------------------------------------------- #
# The junction / chain enumerations
# --------------------------------------------------------------------------- #
def _along_y(spec: FamilySpec, size: str, order, extents, offsets, gap: float,
             bus: str | None, want: str):
    """Members side by side along +y, each turned so its ``bus`` pad faces
    ``want``. ⚠ 0/180 only, so the axis-aligned envelope never changes and the
    cursor arithmetic stays valid (:func:`_facing_rotation`'s own reason)."""
    kinds = dict(spec.parts)
    members, cursor = [], 0.0
    for index, ref in enumerate(order):
        pad = spec.pad_on(ref, bus) if bus else None
        rotation = _facing_rotation(offsets[ref], pad, want)
        if index:
            cursor += (extents[order[index - 1]][1] + extents[ref][1]) / 2.0 + gap
        members.append((ref, footprint_for(kinds[ref], size), 0.0,
                        round(cursor, 6), rotation))
    return members


def _ell_b(spec: FamilySpec, size: str, pair, odd: str, extents, offsets,
           gap: float, bus: str | None):
    """The pair long-sides-together; the odd member **in line** with the pair's
    second, pin-side to pin-side across the gap."""
    kinds = dict(spec.parts)
    members = _along_y(spec, size, pair, extents, offsets, gap, bus, "E")
    _anchor_ref, _fp, _ax, anchor_y, _rot = members[-1]
    # ⛔⛔ The MAX width over the pair, not the anchor's. The odd member sits on
    # the anchor's row but must also clear the pair's OTHER member diagonally,
    # and using the anchor's width under-spaces it whenever the other member is
    # wider. MEASURED at 0402, where ``C_0402`` is 1.52 wide against ``R_0402``
    # 1.56: ``ell_b-oddR1-R2C1`` came out with its courtyards **0.01 mm** into
    # each other -- the same defect as the shipped cells, surviving one round of
    # fixing because the gap was right and the reference box was not.
    x = (max(extents[r][0] for r in pair) + extents[odd][0]) / 2.0 + gap
    pad = spec.pad_on(odd, bus) if bus else None
    members.append((odd, footprint_for(kinds[odd], size), round(x, 6),
                    anchor_y, _facing_rotation(offsets[odd], pad, "W")))
    return members


def _ell_a(spec: FamilySpec, size: str, pair, odd: str, extents, turned,
           offsets, gap: float, bus: str | None, turn: int):
    """The tee: the pair long-sides-together, the odd member turned across them.

    ⚠ The odd member's rotation is **enumerated** (90 and 270) rather than
    derived: turned across the pair it presents both its pads to the pair's
    midline, so which one carries the junction net is a genuine choice and not
    something the netlist decides. ⛔ That is the honest treatment -- deriving it
    from a rule invented here would be the hardcoded-cycle trap in a new place.
    """
    kinds = dict(spec.parts)
    members = _along_y(spec, size, pair, extents, offsets, gap, bus, "E")
    span = [y for _r, _f, _x, y, _rot in members]
    x = max(extents[r][0] for r in pair) / 2.0 + gap + turned[odd][0] / 2.0
    members.append((odd, footprint_for(kinds[odd], size), round(x, 6),
                    round((min(span) + max(span)) / 2.0, 6), int(turn)))
    return members


def _is_stack_diagonal(label: str) -> bool:
    """``stack-gapA-rowB`` with ``A == B``. ⭐ The shipped generator's own
    winner is on this diagonal on 6 of 6 (``stack-gap1.00-row1.00``), so the
    sample keeps the incumbent it has to beat."""
    if not label.startswith("stack-gap") or "-row" not in label:
        return False
    head, row = label.split("-row", 1)
    return head[len("stack-gap"):] == row


def _junction_candidates(spec: FamilySpec, size: str, *, gap: float,
                         fp_lib_dirs, bus: str | None):
    """The 15 authored non-line arrangements: 3 x ``bus3``, 6 x ``ell_a``,
    6 x ``ell_b``.

    ⛔ ``bus3`` enumerates only **which member is in the middle** (3, not 6):
    reversing the row is a 180 degree rotation of the whole cell, and a cell is
    already rotatable at placement time, so the other three are the same cell.
    ``ell_b`` does enumerate both pair orders, because there the odd member
    lines up with the pair's *second* member and swapping them is a reflection,
    not a rotation.
    """
    kinds = dict(spec.parts)
    extents = member_envelopes(spec, size, 0, fp_lib_dirs)
    turned = member_envelopes(spec, size, 90, fp_lib_dirs)
    offsets = {ref: pad_offsets(footprint_for(kinds[ref], size), fp_lib_dirs)
               for ref in spec.refs}
    refs = tuple(sorted(spec.refs))
    out: list[Arrangement] = []
    for middle in refs:
        ends = [r for r in refs if r != middle]
        order = (ends[0], middle, ends[1])
        out.append(Arrangement(
            label=f"bus3-mid{middle}-gap{gap:.2f}", shape="bus3",
            members=tuple(_along_y(spec, size, order, extents, offsets, gap,
                                   bus, "E"))))
    for odd in refs:
        rest = [r for r in refs if r != odd]
        for pair in (tuple(rest), tuple(reversed(rest))):
            out.append(Arrangement(
                label=f"ell_b-odd{odd}-{pair[0]}{pair[1]}-gap{gap:.2f}",
                shape="ell_b",
                members=tuple(_ell_b(spec, size, pair, odd, extents, offsets,
                                     gap, bus))))
        for turn in (90, 270):
            out.append(Arrangement(
                label=f"ell_a-odd{odd}-r{turn}-gap{gap:.2f}", shape="ell_a",
                members=tuple(_ell_a(spec, size, tuple(rest), odd, extents,
                                     turned, offsets, gap, bus, turn))))
    return out


def _legacy_candidates(spec: FamilySpec, size: str, *, gap_mm: float,
                       fp_lib_dirs, shapes=SHAPES):
    """``(candidates, dropped)`` -- the shipped generator's own arrangements,
    kept in the enumeration **deliberately**.

    ⛔ The objective has to beat the incumbent, not be protected from it, and
    ``long`` is the incumbent that a junction must never take. ⚠ ``stack``'s
    5x5 cross-ladder is sampled on its diagonal (5 of 25); the 20 dropped rows
    are counted and reported, never silently discarded.
    """
    out, dropped = [], 0
    for shape in shapes:
        kept, over = arrangements(spec, size, gap_mm=gap_mm,
                                  fp_lib_dirs=fp_lib_dirs, shape=shape,
                                  max_arrangements=64)
        dropped += over
        if shape == "stack":
            diagonal = [a for a in kept if _is_stack_diagonal(a.label)]
            dropped += len(kept) - len(diagonal)
            kept = diagonal
        out.extend(kept)
    return out, dropped


def enumerate_junction_arrangements(
    spec: FamilySpec, size: str, *, gap_mm: float, fp_lib_dirs,
    junction_gap: float | None = None, max_arrangements: int = 32,
) -> tuple[list[Arrangement], int]:
    """``(candidates, dropped)`` for a three-member **junction** family.

    15 authored non-line arrangements at one tight, netlist-derived gap, plus
    the shipped generator's ``long`` / ``short`` / ``stack`` ladders as foils --
    30 in all, so the ``max_arrangements`` cap does not bind at the default.
    ⛔ The authored shapes come **first**, so if a caller does lower the cap it
    removes foils rather than the answers.

    ⛔ Returns ``([], 0)`` for a family that is not a three-member junction --
    that is data, not an error, and :func:`classify_three_part_topology` is how
    a caller asks in advance.
    """
    junction = junction_net(spec)
    if junction is None or len(spec.refs) != 3:
        return [], 0
    # ⛔⛔ DERIVED, not `gap_mm + 0.05`. The constant shipped seven cells whose
    # courtyards overlapped -- see `junction_gap_mm`.
    gap = float(junction_gap if junction_gap is not None
                else junction_gap_mm(spec, size, clearance_mm=gap_mm,
                                     fp_lib_dirs=fp_lib_dirs))
    out = _junction_candidates(spec, size, gap=gap, fp_lib_dirs=fp_lib_dirs,
                               bus=junction)
    legacy, dropped = _legacy_candidates(spec, size, gap_mm=gap_mm,
                                         fp_lib_dirs=fp_lib_dirs)
    out.extend(legacy)
    dropped += max(0, len(out) - int(max_arrangements))
    return out[:int(max_arrangements)], dropped


def enumerate_chain_arrangements(
    spec: FamilySpec, size: str, *, gap_mm: float, fp_lib_dirs,
    chain_gap: float | None = None, max_arrangements: int = 32,
) -> tuple[list[Arrangement], int]:
    """``(candidates, dropped)`` for a three-member **chain** family.

    ⭐⭐ **The control the whole plan exists to earn.** Same three footprints as
    :func:`enumerate_junction_arrangements`, different netlist -- and here the
    line is the *right* answer, so it is authored explicitly at the tight gap
    (``line-gapG``) alongside the shipped ``long`` ladder, with the same
    non-line shapes as foils. An objective that prefers a non-line for the
    junction and the line here has read the topology; one that returns the same
    geometry for both has not.

    ⚠ ``stack`` is absent by construction -- it needs a junction net, and a
    chain has none. That is :func:`arrangements`' own answer, not a filter here.
    """
    order = chain_order(spec)
    if order is None or len(spec.refs) != 3:
        return [], 0
    gap = float(chain_gap if chain_gap is not None
                else junction_gap_mm(spec, size, clearance_mm=gap_mm,
                                     fp_lib_dirs=fp_lib_dirs))
    kinds = dict(spec.parts)
    extents = member_envelopes(spec, size, 0, fp_lib_dirs)
    offsets = {ref: pad_offsets(footprint_for(kinds[ref], size), fp_lib_dirs)
               for ref in spec.refs}
    out = [Arrangement(
        label=f"line-gap{gap:.2f}", shape="line",
        members=tuple(_long(spec, size, order, extents, offsets, gap)))]
    out.extend(_junction_candidates(spec, size, gap=gap,
                                    fp_lib_dirs=fp_lib_dirs,
                                    bus=junction_net(spec) or _bus_net(spec)))
    legacy, dropped = _legacy_candidates(spec, size, gap_mm=gap_mm,
                                         fp_lib_dirs=fp_lib_dirs)
    out.extend(legacy)
    dropped += max(0, len(out) - int(max_arrangements))
    return out[:int(max_arrangements)], dropped


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
@dataclass
class FamilyRun:
    """The record of one (family, size, shape, layers, fab) generation."""

    family: str
    size: str
    layers: int
    fab: str
    shape: str = ""
    min_lane_mm: float = 0.0
    enumerated: int = 0
    dropped: int = 0
    compiled: int = 0
    accepted: int = 0
    contract: tuple = ()
    contract_met: bool = False
    contract_candidates: int = 0
    winner_digest: str = ""
    winner_label: str = ""
    winner_box: tuple = (0.0, 0.0)
    winner_area: float = 0.0
    winner_area_ratio: float = 0.0
    winner_lanes: dict = field(default_factory=dict)
    shrank_from: tuple = (0.0, 0.0)
    skipped: str = ""
    rejected: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "family": self.family, "size": self.size, "shape": self.shape,
            "layers": self.layers, "min_lane_mm": self.min_lane_mm,
            "fab": self.fab, "enumerated": self.enumerated,
            "dropped": self.dropped, "compiled": self.compiled,
            "accepted": self.accepted,
            "contract": list(self.contract),
            "contract_met": self.contract_met,
            "contract_candidates": self.contract_candidates,
            "winner_digest": self.winner_digest,
            "winner_label": self.winner_label,
            "winner_box": list(self.winner_box),
            "winner_area": self.winner_area,
            "winner_area_ratio": self.winner_area_ratio,
            "winner_lanes": dict(self.winner_lanes),
            "shrank_from": list(self.shrank_from),
            "skipped": self.skipped,
            "rejected": list(self.rejected),
        }


def lane_summary(cell: LayoutCell, layer: int = 0) -> dict:
    """``{axis: (lanes, total_width, max_trace)}`` on one layer."""
    out = {}
    for axis in ("EW", "NS"):
        transit = cell.transit_for(layer, axis)
        out[axis] = ((len(transit.lanes), transit.total_width,
                      transit.max_trace) if transit else (0, 0.0, 0.0))
    return out


def _meets(cell: LayoutCell, shape: str, min_lane_mm: float) -> bool:
    """Does the cell offer a lane a **real trace** fits in, on every axis its
    shape promises?

    ⛔ The test is on ``max_trace``, not on the lane count and not on
    ``total_width``. MEASURED on the first cut: ``divider_cap`` 0805 advertised
    **1.000 mm total across 3 lanes whose widest was 0.400 mm**, so a cell can
    be nominally a millimetre open and still not pass one 0.5 mm power trace.
    That is why both scalars exist and why only one of them is a criterion.
    """
    lanes = lane_summary(cell, 0)
    return all(lanes.get(axis, (0, 0.0, 0.0))[2] >= min_lane_mm - 1e-9
               for axis in SHAPE_CONTRACT[shape])


def generate_family(
    spec: FamilySpec,
    size: str,
    *,
    fp_lib_dirs: Sequence[str],
    fab_spec=None,
    layers: int = 2,
    shape: str = "long",
    cache: CellCache | None = None,
    probe=None,
    max_arrangements: int = 32,
    shrink: bool = False,
    min_lane_mm: float | None = None,
) -> tuple[LayoutCell | None, FamilyRun]:
    """Enumerate, compile and cache one family at one size in one shape.

    Returns ``(winner, run)``. ``winner`` is ``None`` when the family cannot take
    the shape or no arrangement accepted -- reported rather than raised, because
    "this topology has no junction, so it has no ``stack``" is data.

    ⛔⛔ **``shrink`` defaults OFF here, and the reversal is deliberate.** The
    0.1 mm ladder minimises box area against an acceptance test that has **no
    transit criterion**, so run over a shaped arrangement it walks straight back
    down the gap ladder and closes the very channels the shape was chosen to
    open. ⭐ Shrinking is the right tool for a *harvested* cell, whose gaps
    nobody chose; it is the wrong tool for an *authored* one, whose gaps are the
    design. ⚠ The option is kept so the negative stays reproducible.

    ⭐ Cells are compiled with ``body_obstructs=True``: a generated cell's whole
    claim is its passthroughs, and a "lane" that runs under a chip body between
    its own two pads is a lane a router may legally take and no reader would
    accept as one.
    """
    from .cells_compile import compile_cell, shrink_cell

    clearance = float(getattr(fab_spec, "clearance_mm", 0.25) or 0.25)
    # ⭐ The ROUTED track width, not the fab's published minimum. A lane that
    # only takes a 0.1524 mm hairline is not a lane on a board every track of
    # which is emitted at 0.3 mm -- the same "design value, not published floor"
    # distinction ``compile_cell`` makes about clearance.
    lane_floor = float(min_lane_mm if min_lane_mm is not None
                       else getattr(fab_spec, "track_width_mm", 0.3) or 0.3)
    run = FamilyRun(family=spec.name, size=size, layers=int(layers),
                    shape=str(shape), contract=SHAPE_CONTRACT[shape],
                    min_lane_mm=lane_floor,
                    fab=str(getattr(fab_spec, "name", "") or ""))
    candidates, dropped = arrangements(spec, size, gap_mm=clearance,
                                       fp_lib_dirs=fp_lib_dirs, shape=shape,
                                       max_arrangements=max_arrangements)
    run.enumerated = len(candidates) + dropped
    run.dropped = dropped
    if not candidates:
        run.skipped = f"{spec.name} cannot take shape {shape!r}"
        return None, run

    compile_kwargs = {"stackup": layers, "body_obstructs": True}
    scored: list[tuple] = []
    for arrangement in candidates:
        name = f"{spec.name}_{size}_{layers}L_{shape}"
        try:
            raw = synthesise_cell(
                name, arrangement.members, spec.nets,
                fp_lib_dirs=fp_lib_dirs, internal_nets=spec.internal,
                fab=str(getattr(fab_spec, "name", "") or ""), stackup=layers)
        except Exception as exc:                               # noqa: BLE001
            run.rejected.append(f"{arrangement.label}: {type(exc).__name__}")
            continue
        run.compiled += 1
        if shrink:
            result = shrink_cell(raw, fab_spec=fab_spec, probe=probe,
                                 **compile_kwargs)
            cell = result.cell
            ratio = result.final_area_ratio
            before = result.original_box
        else:
            cell, acceptance = compile_cell(raw, fab_spec=fab_spec,
                                            **compile_kwargs)
            ratio = acceptance.area_ratio
            before = (cell.width, cell.height)
        _built, verdict = compile_cell(cell, fab_spec=fab_spec, **compile_kwargs)
        if not verdict.ok:
            run.rejected.append(
                f"{arrangement.label}: "
                f"{sorted(k for k, v in verdict.to_dict().items() if v is False)}")
            continue
        run.accepted += 1
        met = _meets(cell, shape, lane_floor)
        run.contract_candidates += int(met)
        # ⛔ Contract FIRST, area second, digest third. The first cut sorted on
        # area alone and every winner was the tightest -- hence opaque -- member
        # of its ladder. ⛔ Total tie-break on the digest: two arrangements can
        # land on the same area, and an ordering-dependent winner would make the
        # cache host-dependent, the failure mode this project has hit twice.
        scored.append((not met, cell.area_mm2, cell.digest,
                       cell, ratio, before, arrangement.label))

    if not scored:
        return None, run
    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    unmet, area, _digest, winner, ratio, before, label = scored[0]
    run.contract_met = not unmet
    winner = replace(winner.normalised(), meta={
        **dict(winner.meta), "source": "generated", "family": spec.name,
        "size": size, "layers": int(layers), "shape": str(shape),
        "arrangement": label, "contract": list(SHAPE_CONTRACT[shape]),
        "contract_met": not unmet, "min_lane_mm": lane_floor,
        "fab": str(getattr(fab_spec, "name", "") or ""),
        "enumerated": run.enumerated, "dropped": run.dropped}).normalised()
    run.winner_digest = winner.digest
    run.winner_label = label
    run.winner_box = (winner.width, winner.height)
    run.winner_area = area
    run.winner_area_ratio = ratio
    run.winner_lanes = {axis: list(values)
                        for axis, values in lane_summary(winner, 0).items()}
    run.shrank_from = tuple(before)
    if cache is not None:
        cache.store(winner)
    return winner, run


def generate_all(
    *,
    fp_lib_dirs: Sequence[str],
    fab_spec=None,
    sizes: Sequence[str] = SIZES,
    layer_counts: Sequence[int] = (2, 4),
    families: Sequence[FamilySpec] = FAMILIES,
    shapes: Sequence[str] = SHAPES,
    cache: CellCache | None = None,
    probe=None,
    max_arrangements: int = 32,
    shrink: bool = False,
    min_lane_mm: float | None = None,
) -> tuple[list[LayoutCell], list[FamilyRun]]:
    """The whole sweep, in a fixed order. ⭐ ``(cells, runs)``, both sorted.

    ⚠ A ``(family, shape)`` pair the family cannot take contributes a
    :class:`FamilyRun` with ``skipped`` set and **no cell** -- the run count is
    therefore larger than the cell count and that difference is the report.
    """
    cells, runs = [], []
    for spec in families:
        for size in sizes:
            for layers in sorted({int(v) for v in layer_counts}):
                for shape in shapes:
                    cell, run = generate_family(
                        spec, size, fp_lib_dirs=fp_lib_dirs, fab_spec=fab_spec,
                        layers=layers, shape=shape, cache=cache, probe=probe,
                        max_arrangements=max_arrangements, shrink=shrink,
                        min_lane_mm=min_lane_mm)
                    runs.append(run)
                    if cell is not None:
                        cells.append(cell)
    return cells, runs
