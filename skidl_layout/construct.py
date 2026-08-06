# -*- coding: utf-8 -*-
"""The construction loop -- one anchor, one side (S3) and the whole cell (S4).

Construction-arc stage **S3** implemented exactly P1-P5 of the overview's
construction loop for a **single side of a single IC cell**: take an anchor out
of a :class:`~skidl_layout.cells_partition.Partition`, collect the parts that
connect to it, assign each one to a side by the escape side of the anchor pad it
connects to (:mod:`~skidl_layout.escape_map`), order them by the geometric order
of those pads along that edge, choose a rotation and a standoff derived from the
``FabSpec``, put them down, and **ask the router whether the connecting pair
routes**.

⭐⭐⭐ **Stage S4 adds the rest of the cell** and is the second half of this
module: :func:`side_order` (P3 -- which side is built first), a **flattened**
template (the member with a pad on the bound net takes the side slot, its
partners go to a **second ring**), a **stacked-binding distribution** policy,
**connecting-pad alignment**, ring 2 itself (:func:`construct_cell`), and P7's
**tighten pass**. ⛔ Every one of those sits behind a keyword flag whose OFF arm
reproduces S3's recorded artifact byte for byte -- that control is gate ``FC0``
and it is the reason the flags exist.

⛔⛔ **Rigid template units are deliberately NOT built.** The record's own
refutation is that *rigidity is not the mechanism* -- cells harvested off the
AUTO board scored **worse** than no cells, and what helped was bottled human
judgement. A template is therefore **flattened**, never placed as a unit.

⛔⛔ **This is a LEAF module.** Nothing in the engine, the scorer or the refiner
imports it, and nothing consumes a :class:`SideResult` or a :class:`CellResult`
yet -- S5 (the board) is the consumer. Same discipline as
:mod:`~skidl_layout.route_session`, :mod:`~skidl_layout.escape_map` and
:mod:`~skidl_layout.cells_partition`, and
``test_construct.py::test_construct_is_a_leaf_no_module_imports_it`` says so.
⚠ **That guard reads IMPORT STATEMENTS, never the module name as a substring**
(standing finding 16) -- prose about a module is documentation and this arc
wants more of it, not less.

⛔ **What this module is NOT.** It does not place a board (S5), does not compare
anything against anything and **may not say the word "better"** (S6 alone may),
and does not touch ``scoring.py`` / ``refinement.py`` / ``engine.py`` or any
default. Every module it names below is **called**, never edited.

**The four things that are genuinely new here** -- everything else is plumbing
between three shipped modules:

1. **The inventory and its priority** (P1) -- four neighbour classes, ranked by
   how many nets a part shares with the anchor, tied by a total key over
   content.
2. **The side list and its order** (P2) -- ⭐ the important rule. Within a side,
   neighbours are ordered by **the measured coordinate of the anchor pad they
   connect to along that edge**, so the fanout is non-crossing *by construction*
   rather than by a crossing penalty. ⛔ Never by pad *number*: pad numbers
   ascend along the row on an MSOP and do not on a QFN, and assuming they do is
   the size-table trap in a sixth costume.
3. **The rotation enumeration and the standoff** (P4) -- a finite, ordered,
   deterministic sweep over ``(0, 90, 180, 270)``, and a standoff every term of
   which comes from the ``FabSpec`` or from a **measured courtyard**, never from
   a constant.
4. **The failure log**, which the overview calls the most valuable output.

⛔ **Say which box every rule means** (standing finding 13):

* **escape and rotation reasoning** run on ``physical_bounds`` (``body ∪ pads``)
  -- that is the frame :mod:`~skidl_layout.escape_map` derives in and the box
  the validator overlaps on;
* **standoff and collision** run on the **courtyard**
  (``FootprintGeometry.bounds``, which *prefers* ``courtyard_bounds``). The
  measured overhang is 0.175 mm/side at 0402 and 0.275-0.280 mm at 0603/0805,
  so a standoff computed off ``physical_bounds`` is wrong by exactly that much
  *and every test still passes at 0805*.

⚠ **The seam the plan named for the courtyard check does not fit, and this is
recorded rather than worked around.** ``cells_families.member_courtyard_gap``
takes a :class:`~skidl_layout.cells.LayoutCell`, not a list of placed parts, so
it cannot be handed S3's placements. :func:`courtyard_gap` below is that
function's **rule**, verbatim -- two parts clear each other if *either* axis
separates them, so the per-pair gap is ``max(gap_x, gap_y)`` and the set's is
the minimum over pairs -- applied to placed parts, and a unit test asserts the
two agree on the same geometry. ⛔ It is not a second rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

__all__ = [
    "ANCHOR_PAD_NOT_ESCAPABLE",
    "ARC_EDGE_GAP",
    "BRIDGE_TIER_RULES",
    "CONNECTOR_POLICIES",
    "CORNER_OWNER",
    "CROSSING_KINDS",
    "CellResult",
    "ConstructError",
    "EDGE_GAP_TERMS",
    "FAMILY_UNITS",
    "L2_ANCHOR_RULES",
    "L2_PORT_SIDES",
    "L2_SIDE_ORDERS",
    "LADDER_RUNGS",
    "MAX_PUSH_STEPS",
    "MAX_RINGS",
    "MAX_SLIDE_STEPS",
    "NEIGHBOUR_CLASSES",
    "NetSegment",
    "QUADRANTS",
    "RING_STOP_REASONS",
    "Neighbour",
    "OVERFLOW_ADMISSION",
    "OVERFLOW_ORDER",
    "OVERFLOW_REASONS",
    "OverflowMove",
    "Placement",
    "PORT_SIDE_SOURCES",
    "ROTATIONS",
    "SEGMENT_KINDS",
    "SIDES",
    "SIDE_ASSIGNMENT",
    "SIDE_DEMAND_REASONS",
    "SideResult",
    "StrangerCrossing",
    "TIGHTEN_FLOORS",
    "TIGHTEN_MODE",
    "TIGHTEN_STOP_REASONS",
    "TightenStep",
    "UNIT_SOURCE",
    "Unit",
    "UnitPort",
    "BoardResult",
    "board_result_to_dict",
    "arc_gap_mm",
    "board_units",
    "busy_anchor_extra_mm",
    "cell_result_to_dict",
    "construct_board",
    "construct_cell",
    "construct_side",
    "construct_template",
    "corner_owners",
    "corner_share",
    "courtyard_gap",
    "edge_gap_mm",
    "l0_anchor",
    "l2_anchor",
    "l2_side_lists",
    "moved_pads",
    "pair_crossings",
    "port_rank",
    "reset_router_residue",
    "ring2_subanchor",
    "ring_radius",
    "ring_slots",
    "segment_census",
    "segment_tiers",
    "segments_by_tier",
    "shrink_ring",
    "side_demand",
    "side_neighbours",
    "side_order",
    "side_result_to_dict",
    "side_span",
    "standoff_base_mm",
    "stranger_crossings",
    "template_port_member",
    "unit_box",
    "unit_from_cell",
    "unit_from_part",
    "unit_ports",
]

#: ⛔ The four sides, in the canonical order the cell layer uses.
SIDES: tuple[str, ...] = ("N", "E", "S", "W")

#: ⛔ **A finite, ordered, deterministic enumeration** -- the overview's [CALL]
#: on *"rotate until no crossings"* is explicit that a loop with an implicit
#: exit is not acceptable. Every rotation decision in this module walks this
#: tuple in this order and tie-breaks on the smallest.
ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)

#: The four neighbour classes of overview P1, **highest priority first**.
#: ⛔ Declared-constant guard (standing finding 20): a class that matches
#: nothing is *reported loudly* by the driver, and a class that could never
#: match anything at all is a defect. ``template`` is reachable because
#: :func:`side_neighbours` inventories **cells** as well as parts -- P1 says
#: *"collect every part **and cell** that connects to A"*.
NEIGHBOUR_CLASSES: tuple[str, ...] = ("internal", "decoupling", "one_pad",
                                      "template")

#: The class's contribution to the priority. ⭐ The *within-class* term is the
#: number of nets the neighbour shares with the anchor -- overview P1's
#: *"more connections to A ⇒ higher priority"*.
_CLASS_RANK = {name: (len(NEIGHBOUR_CLASSES) - i) * 100
               for i, name in enumerate(NEIGHBOUR_CLASSES)}

#: ⛔ The along-the-edge gap between two consecutive neighbours, in FabSpec
#: terms and **never** a constant: one passing trace with its clearance on both
#: sides. Recorded as ``(term, why)`` pairs because every number in this stack
#: carries its source.
EDGE_GAP_TERMS = (("track_width_mm", "1 x track width -- one passing trace"),
                  ("clearance_mm", "2 x clearance -- one on each side"))

#: ⛔ The slide ladder is **bounded** and its bounds are named constants
#: (overview 7.2 step 4 is *"fail loudly"*, which needs a rung to run out of).
MAX_SLIDE_STEPS = 6
MAX_PUSH_STEPS = 2

#: ⛔ Overview 7.2, in strict order. ⛔ **Never slide perpendicular first** --
#: that consumes the fanout allowance, which is the one thing the standoff
#: exists to protect. Declared as data so the driver can assert every rung was
#: reachable and report which ones were used.
LADDER_RUNGS = ("slide along the side axis, away from A's centre",
                "try the remaining rotations",
                "push out by one more standoff unit",
                "fail loudly")

#: ⛔ **S4.** Why one neighbour's tighten stopped where it did. Declared as data
#: so the driver can report a reason that never occurs (standing finding 20).
#:
#: * ``floor`` -- the next step would put this neighbour's courtyard into its own
#:   **(sub-)anchor's**. ⭐ That is exactly the point at which the fanout
#:   allowance has been fully consumed, so it is the *designed* stopping place
#:   and the number that answers overview open question 5.
#: * ``collision`` -- the next step would collide with some **other** placed box.
#: * ``unroutable`` -- the step was legal and then a pair of the cell stopped
#:   routing, so it was reverted.
#: * ``cap`` -- the per-neighbour step budget ran out first.
TIGHTEN_STOP_REASONS: tuple[str, ...] = ("collision", "unroutable", "floor",
                                         "cap")

#: ⛔ **One ring only** (S4 section 5.3). A member still unplaced after ring 2 is
#: a logged failure; a third ring is S5's problem if it ever exists. Bounded and
#: attributable beats deep and unreadable.
MAX_RINGS = 2

#: The reason string a skip carries when the anchor pad has no escapable side.
#: ⚠ On the corpus this is the **exposed pad** and that is the correct answer
#: (an EP is enclosed by its own ring), so it is a skip and not a failure --
#: but it is counted and named, never silently dropped.
ANCHOR_PAD_NOT_ESCAPABLE = "anchor pad has no escapable side"

#: ⛔ **S5.** Why one overflow candidate did or did not leave its side.
#: Declared as data so the driver can report a clause that matches nothing
#: (standing finding 20, which has now fired on **four** consecutive plans).
#:
#: * ``side_oversubscribed`` -- the side's load ratio is > 1.0 but the candidate
#:   was not among the offered tail. ⚠ Recorded for completeness: a policy that
#:   reports only the parts it moved is the observes-nothing defect.
#: * ``no_free_side`` -- every other side is at least as loaded as this one.
#: * ``no_escape_on_free_side`` -- the destination is not escapable under
#:   :data:`OVERFLOW_ADMISSION`'s selected rule.
#: * ``unroutable`` -- the candidate was placed on the destination side and the
#:   ladder ran out (a collision or the router's "no path"); it went back.
#: * ``kept`` -- the move survived. ⛔ Every candidate gets exactly one.
OVERFLOW_REASONS: tuple[str, ...] = ("side_oversubscribed", "no_free_side",
                                     "no_escape_on_free_side", "unroutable",
                                     "kept")

#: ⛔⛔ **The two readings of section 5.3 step 3, and the difference is a DEFECT
#: in the plan that only contact could show** -- the same shape as
#: :data:`TIGHTEN_FLOORS` and :func:`_classify_plan_literal`, and for the same
#: reason: **both ship, one is used, and the control stays re-measurable.**
#:
#: The plan says *"the neighbour's own anchor pad must have that side in
#: ``EscapeMap.escapable_sides``"*. That is ``"pad_side"`` below, and it is
#: **all but empty on this corpus**: a four-sided IC's pad escapes only on the
#: side it occupies (so an ``LQFP-48`` pad on W can never admit E/N/S), and a
#: dual-row IC **with an exposed pad** is BLOCKED on N/S (so an ``MSOP-10-1EP``
#: pad can never admit N/S either). ⛔ **MEASURED 2026-08-05 as a one-variable
#: control: 1 candidate offered and 1 move kept across four subjects, against
#: this module's 23 offered and 19 kept** -- and the plan's own gate ``AR3``
#: expectation (*"stm32_bluepill moves at least one part"*) cannot be satisfied
#: under it, because that four-sided subject is exactly the one it admits
#: nothing on.
#:
#: ``"footprint_side"`` is the implemented default and it is the plan's own
#: section 2.4 question -- *"can the anchor escape there?"*, asked of the
#: **footprint** (does **any** terminal pad of the anchor escape on that side)
#: rather than of the one bound pad. ⭐ It is the weaker admission on purpose:
#: the escape map's job here is to rule out a side that is walled in, and
#: **step 4's router question is the disqualifier** (overview 7.3). The bound
#: pad still has to escape *somewhere*, which it does by construction -- it
#: already carries a placed neighbour.
OVERFLOW_ADMISSION: tuple[str, ...] = ("footprint_side", "pad_side")

#: ⛔ **S5.** The four outcomes of the pair-line census. ⛔ ``shared_endpoint``
#: is **not a crossing** -- 44 of S4's 65 raw intersections are the stacked
#: binding fanning onto one anchor pad, and counting those would report a defect
#: that is not there.
CROSSING_KINDS: tuple[str, ...] = ("same_side", "cross_side", "ring2",
                                   "shared_endpoint")

_TOL = 1e-9


class ConstructError(RuntimeError):
    """⛔ Anything this module refuses to guess about.

    Raised rather than returning a falsy result (standing finding 1, six
    instances in seven runs): an anchor that is in no group, a side list of
    length zero, an escape map that does not describe the anchor's footprint.
    """


# --------------------------------------------------------------------------- #
# Value types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Neighbour:
    """One part (or cell) the loop will place against the anchor."""

    ref: str
    anchor_pad: str           # the anchor pad it connects to
    net: str                  # the net they share
    side: str                 # "N"|"E"|"S"|"W" -- from the EscapeMap
    klass: str                # "internal"|"decoupling"|"one_pad"|"template"
    priority: int             # P1: more connections to A => higher
    #: ⛔ Total, over content -- the P2 sort key. ``(edge coordinate of the
    #: anchor pad, pad key, ref)``: the projection of A's pin order onto the
    #: edge first, then a tie-break that can never depend on arrival order.
    order_key: tuple
    #: How many pads of the anchor carry ``net``. ⚠ The **stacked binding** is
    #: measured, not assumed: on ``lt8710_sepic`` seven ``CIN*`` bind to one
    #: pad. *Twelve parts against two pads is a hint, not a fanout.*
    anchor_pads_on_net: int = 1
    members: tuple[str, ...] = ()      # a template cell's members
    reason: str = ""
    #: ⛔⛔ **S5 trap 16.** The moment ``overflow_to_free_side`` is on,
    #: :attr:`side` stops meaning *"the escape side of my anchor pad"* and starts
    #: meaning *"the side I was actually placed against"*. The escape side is
    #: kept **here**, so every existing reader of :attr:`side` keeps its meaning
    #: only where this field is empty -- and empty is the OFF arm, which is why
    #: :meth:`to_dict` omits it then (the S4 control is a plain diff).
    origin_side: str = ""

    def to_dict(self) -> dict:
        out = {"ref": self.ref, "anchor_pad": self.anchor_pad,
               "net": self.net, "side": self.side, "klass": self.klass,
               "priority": self.priority, "order_key": list(self.order_key),
               "anchor_pads_on_net": self.anchor_pads_on_net,
               "members": list(self.members), "reason": self.reason}
        if self.origin_side:
            out["origin_side"] = self.origin_side
        return out


@dataclass(frozen=True)
class Placement:
    """Where one neighbour was put, and what it cost to put it there."""

    ref: str
    x_mm: float
    y_mm: float
    rot_deg: int
    standoff_mm: float
    slide_steps: int          # how far down overview 7.2's ladder it went
    routed: bool
    route_length_mm: float | None
    route_iterations: int | None
    reasons: tuple[str, ...] = ()
    #: ``((term, value, source), ...)`` for the standoff. ⛔ Every number
    #: carries its source; a standoff quoted without its four terms is a
    #: constant wearing a formula's clothes.
    standoff_terms: tuple = ()
    #: ⚠ Wall clock. Excluded from the determinism digest by the caller -- it
    #: measures this machine, not this placement.
    route_elapsed_s: float | None = None
    route_failure: str | None = None
    anchor_pad: str = ""
    net: str = ""

    def to_dict(self) -> dict:
        return {"ref": self.ref, "x_mm": self.x_mm, "y_mm": self.y_mm,
                "rot_deg": self.rot_deg, "standoff_mm": self.standoff_mm,
                "slide_steps": self.slide_steps, "routed": self.routed,
                "route_length_mm": self.route_length_mm,
                "route_iterations": self.route_iterations,
                "route_elapsed_s": self.route_elapsed_s,
                "route_failure": self.route_failure,
                "anchor_pad": self.anchor_pad, "net": self.net,
                "standoff_terms": [list(t) for t in self.standoff_terms],
                "reasons": list(self.reasons)}


@dataclass(frozen=True)
class SideResult:
    """⭐ The deliverable. A FAILURE here is a first-class result (overview 8).

    *"On ``lt3844_buck``, side W of ``U1``, neighbour ``R4``, the pair route
    failed after the slide ladder ran out"* is the sentence this object exists
    to be able to print.
    """

    board: str
    anchor: str
    side: str
    neighbours: tuple[Neighbour, ...]
    placements: tuple[Placement, ...]
    failures: tuple[dict, ...]        # ref, the step that ran out, the router
    skipped: tuple[dict, ...]         # ref, why (no escape, no geometry, ...)
    standoff_base_mm: float
    meta: dict = None

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})

    @property
    def routed_fraction(self) -> float:
        if not self.placements:
            return 0.0
        return round(sum(1 for p in self.placements if p.routed)
                     / len(self.placements), 4)

    @property
    def legal(self) -> bool:
        """⛔ BOTH boxes (overview 7.1).

        ``physical_bounds`` is the legality the stack already checks; the
        **courtyard** box is the one that had never been checked until
        2026-08-03, when it shipped 7 of 7 overlapping cells while every gate
        passed. ⛔ KiCad DRC is not the backstop -- a deliberate ~1 mm
        courtyard collision on a board this stack wrote produced 15 violations
        and **not one of a courtyard type**.
        """
        return (not self.meta.get("physical_overlaps")
                and not self.meta.get("courtyard_overlaps"))


@dataclass(frozen=True)
class TightenStep:
    """⭐ One neighbour's tighten outcome -- **overview open question 5's data**.

    *"How much of the standoff does the tighten pass actually recover, and does
    recovering it cost routability?"* has been open since the procedure was
    written and S3 could not touch it: every neighbour landed at **rung 0**, so
    the fanout allowance met no opposing pressure at all and
    *"2.0048 mm is right"* stayed **untested rather than confirmed**.

    ⛔⛔ ``recovered_mm`` is a **measurement of slack, never a grade.** S4 may
    not say the word "better" -- S6 alone may, three-armed, on vias and router
    effort.
    """

    ref: str
    moved_from_mm: tuple            # (x, y) before
    moved_to_mm: tuple              # (x, y) after
    recovered_mm: float             # how much standoff the pass clawed back
    steps_accepted: int
    steps_tried: int
    stopped_by: str                 # one of TIGHTEN_STOP_REASONS
    #: ⚠ Three fields beyond the plan's stated shape, added for the same reason
    #: S3 added ``anchor_x_mm``: the plan's report asks for before/after route
    #: lengths *"side by side"* and there was nowhere to put them.
    side: str = ""
    subanchor: str = ""
    route_length_before_mm: float | None = None
    route_length_after_mm: float | None = None
    step_mm: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {"ref": self.ref, "moved_from_mm": list(self.moved_from_mm),
                "moved_to_mm": list(self.moved_to_mm),
                "recovered_mm": self.recovered_mm,
                "steps_accepted": self.steps_accepted,
                "steps_tried": self.steps_tried,
                "stopped_by": self.stopped_by, "side": self.side,
                "subanchor": self.subanchor, "step_mm": self.step_mm,
                "route_length_before_mm": self.route_length_before_mm,
                "route_length_after_mm": self.route_length_after_mm,
                "reason": self.reason}


@dataclass(frozen=True)
class OverflowMove:
    """⭐ **S5.** One neighbour offered a different side, and what happened.

    ⛔ **One of these exists for every candidate considered, moved or not.** A
    policy that reports only its successes is standing finding 1 wearing a
    bookkeeping hat, and this arc has met that defect six times in seven runs.

    ``pair_mm_before`` is the length the connecting pair routed at on the
    **origin** side and ``pair_mm_after`` the length on the **destination** --
    ⛔ both are *length measurements*, never grades (plan rule 11, which is
    stricter in S5 than in S4 precisely because a shorter route reads like
    quality and is not).
    """

    ref: str
    from_side: str
    to_side: str | None
    reason: str                  # one of OVERFLOW_REASONS
    anchor_pad: str
    net: str
    pair_mm_before: float | None = None
    pair_mm_after: float | None = None
    probe_iterations: int | None = None
    #: Which of :data:`OVERFLOW_ADMISSION` decided, and what the map said about
    #: the destination -- ⭐ so the *"first placement in the arc on a
    #: non-FAVORED side"* can be checked against the router's own answer.
    admission: str = ""
    destination_access: str = ""
    from_side_ratio: float | None = None
    to_side_ratio: float | None = None
    why: str = ""

    def to_dict(self) -> dict:
        return {"ref": self.ref, "from_side": self.from_side,
                "to_side": self.to_side, "reason": self.reason,
                "anchor_pad": self.anchor_pad, "net": self.net,
                "pair_mm_before": self.pair_mm_before,
                "pair_mm_after": self.pair_mm_after,
                "probe_iterations": self.probe_iterations,
                "admission": self.admission,
                "destination_access": self.destination_access,
                "from_side_ratio": self.from_side_ratio,
                "to_side_ratio": self.to_side_ratio, "why": self.why}


@dataclass(frozen=True)
class CellResult:
    """⭐ The S4 deliverable. Failures and skips are **first class**.

    Overview section 8: a constructive placer's failures are *localised and
    readable* in a way a search's are not, and the staged plans must treat that
    log as a deliverable rather than as noise to be suppressed.

    ⛔ The accounting is **TOTAL**: every member of the anchor's group and every
    member of every template cell that took a side slot ends in exactly one of
    *placed on a side*, *placed in ring 2*, *failed* or *skipped with a named
    reason*. A part that simply vanishes is the observes-nothing defect wearing
    a bookkeeping hat (standing finding 1, six instances in seven runs).
    """

    board: str
    anchor: str
    sides: tuple                      # SideResult, in P3 order
    ring2: tuple                      # Placement
    ring2_failures: tuple
    tighten: tuple                    # TightenStep
    skipped: tuple
    meta: dict = None

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})

    @property
    def placements(self) -> tuple:
        out: list = []
        for side in self.sides:
            out.extend(side.placements)
        out.extend(self.ring2)
        return tuple(out)

    @property
    def routed_fraction(self) -> float:
        placements = self.placements
        if not placements:
            return 0.0
        return round(sum(1 for p in placements if p.routed)
                     / len(placements), 4)

    @property
    def legal(self) -> bool:
        """⛔ BOTH boxes, over the WHOLE cell -- sides *and* ring 2.

        Standing finding 13: ``min_member_gap`` compares
        ``transformed_physical_bounds`` while ``FootprintGeometry.bounds``
        *prefers* ``courtyard_bounds``, and nothing compared the second until
        2026-08-03 -- when it turned out 7 of 7 shipped cells overlapped
        courtyards while every gate passed.
        """
        return (not self.meta.get("physical_overlaps")
                and not self.meta.get("courtyard_overlaps"))


def side_result_to_dict(result: SideResult) -> dict:
    """A JSON-serialisable, order-stable view. ⭐ Two runs must give one blob.

    ⚠ **Wall-clock fields are on the artifact and are excluded from the
    determinism digest by the caller** -- a route timing is a measurement of
    this machine, not of the placement. Gate ``CL4`` strips
    ``route_elapsed_s`` and ``meta["timing"]`` before it diffs, and says so.
    """
    return {
        "board": result.board,
        "anchor": result.anchor,
        "side": result.side,
        "standoff_base_mm": result.standoff_base_mm,
        "routed_fraction": result.routed_fraction,
        "legal": result.legal,
        "neighbours": [n.to_dict() for n in result.neighbours],
        "placements": [p.to_dict() for p in result.placements],
        "failures": list(result.failures),
        "skipped": list(result.skipped),
        "meta": result.meta,
    }


# --------------------------------------------------------------------------- #
# The moved-pad seam -- the plan's one real technical risk
# --------------------------------------------------------------------------- #
def moved_pads(pads, *, x_mm: float, y_mm: float, rot_deg: float,
               origin_x: float = 0.0, origin_y: float = 0.0,
               base_rot_deg: float = 0.0) -> list:
    """``pads`` as they would be if their footprint sat at ``(x, y, rot)``.

    ⭐⭐⭐ **This is what makes a constructive loop affordable.**
    :meth:`~skidl_layout.route_session.RouteSession.from_board` builds its map
    from a board **on disk**, so every pad ``add_part``/``route_pair`` sees is a
    ``kicad_parser.Pad`` at the coordinates the *file* gives it. A constructive
    loop needs to stamp a part where it just *chose* to, and rewriting the board
    per trial costs a full re-parse and rebuild (measured in gate ``CL0``)
    against the **2.4-6.2 ms** a pair question costs -- which would price the
    loop out. ``kicad_parser.Pad`` is a plain dataclass, so a moved pad is a
    :func:`dataclasses.replace`.

    ⛔⛔ **Four attributes a naive ``replace`` gets wrong, and each of them is
    silent:**

    1. ``size_x``/``size_y`` are in **board space with the rotation already
       baked in** (``kicad_parser._resolve_pad_rect`` swaps them at ~90 deg),
       so a +/-90 deg change must swap them. A rect pad stamped without the
       swap blocks the wrong rectangle and nothing says so.
    2. ``rotation`` is the pad's **absolute board angle** -- the footprint's
       rotation is already folded in, per the dataclass's own comment -- so it
       takes the *delta*, never the new angle.
    3. ``polygons`` (the real copper outline of a custom comb/finger pad) are
       in **global** coordinates and must be transformed too, or a
       custom-shaped pad stamps its copper at the old place.
    4. ``hole_x``/``hole_y`` (a pad whose drill is offset from its copper
       centre) are global as well.

    ⛔ ``local_x``/``local_y`` are the footprint-frame position the transform
    starts from and are deliberately **unchanged**: they are what makes this an
    *absolute* placement rather than a chain of deltas.

    ``base_rot_deg`` is the rotation already baked into the pads handed in --
    the footprint's rotation in the file the session parsed. It is **required**
    to be honest: with it, ``rot_deg`` is the part's new absolute angle; without
    it a part that was not at 0 deg in the file would be rotated twice. ⚠ It is
    an addition to the plan's stated signature, made for exactly that reason
    and recorded in the run report.

    ⭐ Proved against a **board-rewrite control** by gate ``CL0``: stamping via
    this function and stamping the same footprint parsed off a board that
    really has it there give **identical censuses**.
    """
    from .geometry import transform_point

    delta = (float(rot_deg) - float(base_rot_deg)) % 360.0
    swap = abs((delta % 180.0) - 90.0) < 1e-6
    out = []
    for pad in pads:
        gx, gy = transform_point(float(x_mm), float(y_mm), float(rot_deg),
                                 float(pad.local_x) + float(origin_x),
                                 float(pad.local_y) + float(origin_y))
        kwargs = {"global_x": gx, "global_y": gy,
                  "rotation": (float(getattr(pad, "rotation", 0.0) or 0.0)
                               + delta) % 360.0}
        if swap:
            kwargs["size_x"] = pad.size_y
            kwargs["size_y"] = pad.size_x
        residual = float(getattr(pad, "rect_rotation", 0.0) or 0.0)
        if residual:
            folded = (residual + delta) % 180.0
            kwargs["rect_rotation"] = (folded if folded <= 90.0
                                       else folded - 180.0)
        polygons = getattr(pad, "polygons", None)
        if polygons:
            kwargs["polygons"] = [
                [_rotate_about(px, py, pad.global_x, pad.global_y, delta,
                               gx, gy) for px, py in polygon]
                for polygon in polygons]
        hole_x = getattr(pad, "hole_x", None)
        hole_y = getattr(pad, "hole_y", None)
        if hole_x is not None and hole_y is not None:
            kwargs["hole_x"], kwargs["hole_y"] = _rotate_about(
                float(hole_x), float(hole_y), pad.global_x, pad.global_y,
                delta, gx, gy)
        out.append(replace(pad, **kwargs))
    return out


def _rotate_about(px: float, py: float, cx: float, cy: float, deg: float,
                  new_cx: float, new_cy: float) -> tuple[float, float]:
    """``(px, py)`` rigidly attached to a pad centre that moved and turned.

    ⭐ The same sense as :func:`~skidl_layout.geometry.transform_point` -- the
    arc has exactly one rotation convention and this is not a second one.
    """
    radians = math.radians(deg)
    dx, dy = px - cx, py - cy
    return (new_cx + dx * math.cos(radians) + dy * math.sin(radians),
            new_cy - dx * math.sin(radians) + dy * math.cos(radians))


# --------------------------------------------------------------------------- #
# Geometry helpers -- every one of them says which box it means
# --------------------------------------------------------------------------- #
def _placed(ref: str, x_mm: float, y_mm: float, rot_deg: float,
            footprint: str):
    from .writer import PlacedPart

    return PlacedPart(ref=str(ref), x_mm=float(x_mm), y_mm=float(y_mm),
                      rot_deg=float(rot_deg), footprint=str(footprint),
                      side="front")


def _courtyard_box(geometry, ref, x_mm, y_mm, rot_deg) -> tuple:
    """⛔ The **courtyard** box, in world mm. ``transformed_bounds`` *prefers*
    ``courtyard_bounds`` -- that one-word difference IS standing finding 13."""
    return geometry.transformed_bounds(
        _placed(ref, x_mm, y_mm, rot_deg, geometry.footprint))


def _physical_box(geometry, ref, x_mm, y_mm, rot_deg) -> tuple:
    """⛔ The ``body ∪ pads`` box, in world mm -- what the validator overlaps."""
    return geometry.transformed_physical_bounds(
        _placed(ref, x_mm, y_mm, rot_deg, geometry.footprint))


def _pair_gap(a: tuple, b: tuple) -> float:
    """⭐ ``cells_compile.min_member_gap``'s rule, unchanged: two boxes clear
    each other if **either** axis separates them, so the gap is
    ``max(gap_x, gap_y)``. Negative means they overlap."""
    gap_x = max(b[0] - a[2], a[0] - b[2])
    gap_y = max(b[1] - a[3], a[1] - b[3])
    return max(gap_x, gap_y)


def courtyard_gap(boxes: dict) -> float:
    """Smallest **courtyard** separation over ``{ref: box}``, in mm.

    ⭐ Deliberately :func:`~skidl_layout.cells_families.member_courtyard_gap`'s
    rule applied to placed parts rather than to a
    :class:`~skidl_layout.cells.LayoutCell`, because that function's signature
    takes a cell and S3 has placements. ``test_construct.py`` asserts the two
    agree on the same geometry, so this is the same rule and not a second one.

    ``inf`` for fewer than two boxes -- nothing to collide with.
    """
    refs = sorted(boxes)
    if len(refs) < 2:
        return float("inf")
    worst = float("inf")
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            worst = min(worst, _pair_gap(boxes[a], boxes[b]))
    return round(worst, 6)


def standoff_base_mm(fab) -> float:
    """``2 x (track + clearance) + via lane`` -- overview P4's fanout allowance.

    ⛔ **Every term from the FabSpec, never a constant.** It is room for *other
    people's* wires (two passing traces) plus one via with its keep-outs to
    escape between the neighbour and the anchor, and it is the design's real
    content. On ``oshpark-2l`` it is **2.0048 mm**.
    """
    from .power_escape import lane_from_fab

    return round(2.0 * (float(fab.track_width_mm) + float(fab.clearance_mm))
                 + float(lane_from_fab(fab)), 6)


def edge_gap_mm(fab) -> float:
    """The along-the-edge spacing between two consecutive neighbours.

    ⛔ From the FabSpec, per :data:`EDGE_GAP_TERMS`. It is *not* the standoff:
    the standoff protects the fanout channel between the anchor and its
    neighbours, this protects one passing trace **between** two neighbours.
    """
    return round(float(fab.track_width_mm) + 2.0 * float(fab.clearance_mm), 6)


#: ``side -> (perpendicular axis index, outward sign, along axis index)``.
#: ⭐ KiCad's y grows **downward**, and
#: :func:`~skidl_layout.escape_map.pad_occupancy_side` uses the same convention
#: (``dy >= 0`` reads ``S``), so N is -y and S is +y. One table, stated once.
_AXES = {"W": (0, -1.0, 1), "E": (0, +1.0, 1),
         "N": (1, -1.0, 0), "S": (1, +1.0, 0)}


def _side_extent(box: tuple, x_mm: float, y_mm: float, side: str) -> float:
    """How far ``box`` reaches from ``(x, y)`` towards ``side``."""
    axis, sign, _along = _AXES[side]
    centre = x_mm if axis == 0 else y_mm
    lo, hi = (box[0], box[2]) if axis == 0 else (box[1], box[3])
    return round((hi - centre) if sign > 0 else (centre - lo), 6)


def _pad_key(number: str) -> tuple:
    """``"2"`` before ``"10"`` -- a total key over content, never arrival."""
    text = str(number)
    return (0, len(text), text) if text.isdigit() else (1, len(text), text)


# --------------------------------------------------------------------------- #
# P1 and P2 -- the inventory, the side, the order
# --------------------------------------------------------------------------- #
def _anchor_pads_by_net(pads) -> dict:
    """``{net: [pad number, ...]}`` for one part's parsed pads, sorted.

    ⭐ Taking the net off the **pad** rather than re-deriving it from the
    circuit is deliberate: it is the same netlist, and it is the one the router
    will use, so the two instruments cannot disagree about which pad carries
    which net.
    """
    out: dict = {}
    for pad in pads:
        net = str(getattr(pad, "net_name", "") or "")
        if not net:
            continue
        out.setdefault(net, []).append(str(pad.pad_number))
    return {net: sorted(set(numbers), key=_pad_key)
            for net, numbers in sorted(out.items())}


def _pad_local_positions(geometry) -> dict:
    """``{pad number: (x, y)}`` in the footprint's own frame, MEASURED.

    ⛔ This is what P2's ordering rule reads. It is the library's pad
    geometry, never the pad number and never a package-name table.
    """
    out: dict = {}
    for pad in geometry.pads:
        number = str(pad.number)
        if number.strip():
            out.setdefault(number, (pad.x_mm, pad.y_mm))
    return out


def _pad_local_xy(positions: dict, pad: str, anchor_rot_deg: int,
                  footprint: str) -> tuple:
    """The anchor pad's ``(x, y)`` relative to the anchor's origin, at the
    anchor's rotation. ⛔ MEASURED from the library geometry, never from the pad
    number."""
    from .geometry import transform_point

    local = positions.get(str(pad))
    if local is None:
        raise ConstructError(
            f"{footprint}: no measured position for pad {pad!r} -- the escape "
            f"map and the footprint disagree about the pad set (bail-out 6)")
    return transform_point(0.0, 0.0, float(anchor_rot_deg), local[0], local[1])


def _pad_edge_coordinate(positions: dict, pad: str, side: str,
                         anchor_rot_deg: int, footprint: str) -> float:
    """The anchor pad's coordinate **along** ``side``'s edge, relative to the
    anchor's origin, at the anchor's rotation."""
    x, y = _pad_local_xy(positions, pad, anchor_rot_deg, footprint)
    _axis, _sign, along = _AXES[side]
    return x if along == 0 else y


#: ⛔⛔ **The two readings of P2 for a part that has CHANGED EDGE, and both
#: ship** -- the same shape as :data:`TIGHTEN_FLOORS`,
#: :func:`_classify_plan_literal` and :data:`OVERFLOW_ADMISSION`, and for the
#: same reason: *the refuted reading stays measurable.*
#:
#: * ``edge_depth`` (the default) -- after the along-edge coordinate, **nearest
#:   the destination edge first**. Measured: **0 same-side crossings on 4 of 4**
#:   subjects with P-C alone.
#: * ``perimeter`` -- walk A's outline from the destination edge's start corner:
#:   the start side's pads toward the edge, then the destination side's own, then
#:   the end side's away from it. ⭐ It is the more *principled* generalisation
#:   of *"the order of the pads along that side of A"* and it is **measured
#:   worse** -- 1 / 2 / 0 / 5 same-side crossings on the four subjects.
#: ⚠ **Neither is a proof.** Whether an off-edge fan is nested or crossed depends
#: on where the arrivals END UP relative to their pad column, and that is decided
#: by the cursor -- see :func:`_centre_sides`, which is why P-A carries a
#: crossing guard rather than trusting this key.
OVERFLOW_ORDER: tuple[str, ...] = ("edge_depth", "perimeter")


def _reprojected_key(positions: dict, pad: str, side: str, anchor: str,
                     anchor_geometry, anchor_rot_deg: int, footprint: str,
                     ref: str, policy: str = "edge_depth") -> tuple:
    """⭐⭐⭐ **P2's ordering rule, generalised to a part that CHANGES EDGE --
    and bail-out 3 is what found it, twice.**

    P2 orders a side list by *"the projection of A's pin order onto that
    edge"*. That is a **projection onto one axis**, and it is degenerate the
    moment a neighbour leaves its side: every pad of a dual-row IC's east row
    has the *same* x, so ordering the arrivals on the N edge by x tie-breaks on
    the pad number and produces an order with nothing to do with the geometry.

    ⛔ **Measured twice, not argued.**
    (i) On ``lt3844_buck`` the projection put ``COUT1``/``COUT2`` (pad 10,
    y 26.62) west of ``RS`` (pad 11, y 25.98) on the N edge, so ``RS``'s line --
    furthest east, ending furthest **north** -- cut across both: **0 same-side
    crossings became 2.**
    (ii) A one-sign fix (*order by depth, nearest the destination edge first*)
    repaired that and **created** a crossing among ``ltc1871_sepic``'s
    west-row arrivals, because those land **west** of their pad column while
    ``lt3844_buck``'s land **east** of theirs. A single signed depth cannot be
    right for both.

    ⭐ **The generalisation that is right for both is A's own PERIMETER.** P2's
    rule is *"the order of the pads along that side of A"*; for a part that has
    changed side it becomes *the order of the pads around A, walking the
    perimeter from the destination edge's start corner*. Concretely, for a
    destination edge traversed in the ``+along`` direction: the pads of the side
    at the **start** of that edge, walked **toward** it; then the destination
    side's own pads in ``along`` order; then the pads of the side at the **end**
    of that edge, walked **away** from it; then anything left.

    ⛔ Every term is measured: the pad's own side comes from the shipped
    :func:`~skidl_layout.escape_map.pad_occupancy_side` against the anchor's
    **physical** box (rule 8), and *"start"* and *"end"* are read off
    :data:`_AXES` rather than tabulated.
    """
    from .escape_map import pad_occupancy_side

    axis, sign, along = _AXES[side]
    x, y = _pad_local_xy(positions, pad, anchor_rot_deg, footprint)
    u = round(x if along == 0 else y, 6)
    depth = round(sign * (x if axis == 0 else y), 6)
    if policy == "edge_depth":
        # ⭐ The along-edge coordinate first -- that IS P2, unchanged -- then the
        # pad NEAREST the destination edge first. ``-depth`` ascending is
        # "nearest first", because ``depth`` grows toward the destination side.
        return (u, round(-depth, 6), _pad_key(pad), str(ref))
    if policy != "perimeter":
        raise ConstructError(
            f"overflow_order={policy!r} is not one of {OVERFLOW_ORDER}")
    own = pad_occupancy_side(
        x, y, _physical_box(anchor_geometry, anchor, 0.0, 0.0, anchor_rot_deg))
    start = next(s for s in SIDES
                 if _AXES[s][0] == along and _AXES[s][1] < 0)
    end = next(s for s in SIDES if _AXES[s][0] == along and _AXES[s][1] > 0)
    if own == start:
        group, second = 0, depth
    elif own == side:
        group, second = 1, u
    elif own == end:
        group, second = 2, -depth
    else:
        group, second = 3, u
    return (group, second, _pad_key(pad), str(ref))


def _shared_nets(refs, anchor_nets, nets_by_ref) -> list:
    shared: set = set()
    for ref in refs:
        shared |= set(nets_by_ref.get(ref, ())) & anchor_nets
    return sorted(shared)


def _classify(ref, anchor_nets, nets_by_ref, bindings) -> tuple:
    """Overview P1's classes for a **part**.

    ⛔⛔ **The plan's own tests are ordered so that ``decoupling`` can almost
    never match, and that is standing finding 20 in a seventh costume.** Its
    table tests ``internal`` first -- *"every pad of the neighbour is on a net
    the anchor also touches"* -- and a decoupling capacitor satisfies that on
    every corpus board, because its supply pad AND its ground pad are both on
    nets the anchor touches. Measured 2026-08-03: with the plan's order,
    ``decoupling`` matched **zero** parts on all three subjects while
    ``internal`` swallowed every ``CIN*``/``COUT*``/``CVCC``.

    ⭐ **The fix is the one S2 reached from the other side: the more specific
    classifier wins.** A :class:`~skidl_layout.cells_partition.PinBinding` with
    role ``decap``/``bulk`` is a *derived netlist fact* about what the part is
    for; *"all my nets touch the anchor"* is a weaker structural coincidence.
    So the binding is tested first, and the deviation is recorded rather than
    slipped in -- the driver reports both orders and their counts.
    """
    own = list(nets_by_ref.get(ref, ()))
    shared = sorted(set(own) & anchor_nets)
    binding = bindings.get(ref)
    if binding is not None and binding.role in ("decap", "bulk"):
        return ("decoupling", shared,
                f"a PinBinding with role {binding.role!r} -- the partition's "
                f"own netlist-derived answer, which is more specific than "
                f"'every net touches the anchor'")
    if own and all(net in anchor_nets for net in own):
        return ("internal", shared,
                "every net of the part is a net the anchor touches")
    return ("one_pad", shared, "shares a net with the anchor")


def _classify_plan_literal(ref, anchor_nets, nets_by_ref, bindings) -> str:
    """⚠ The plan's stated order, kept **only** so the driver can measure what
    it would have produced. ⛔ Never used to place anything."""
    own = list(nets_by_ref.get(ref, ()))
    if own and all(net in anchor_nets for net in own):
        return "internal"
    binding = bindings.get(ref)
    if binding is not None and binding.role in ("decap", "bulk"):
        return "decoupling"
    return "one_pad"


def _pick_net(shared, by_net) -> str | None:
    """Which shared net binds the neighbour to a pad of the anchor.

    ⛔ **Plane-free first** (standing finding 5, six independent arrivals): a
    part whose only shared net is ``GND`` is not *"beside that pad"* in any
    useful sense. Ties break on the net name -- a total key over content.
    ⚠ When the only shared net **is** a plane net the anchor's pad on it is
    still used, because that is what the netlist says; the partition's own
    ``PinBinding`` overrides the choice wherever one exists.
    """
    from .ratnest import is_plane_net

    present = [net for net in shared if net in by_net]
    if not present:
        return None
    free = [net for net in present if not is_plane_net(net)]
    return sorted(free or present)[0]


def _bind(ref, shared, by_net, net_of_pad, bindings):
    """``(net, anchor pad)`` for one neighbour, or ``None``.

    ⛔⛔ **The net and the pad must come from the SAME decision, and getting
    that wrong is silent.** A first cut took the pad from the
    :class:`~skidl_layout.cells_partition.PinBinding` and the net from
    :func:`_pick_net`'s plane-free preference -- on ``lt3844_buck`` that paired
    ``U1`` pad **1** (``VIN``) with ``CIN1``'s ``PGND`` pad, asked the router
    for a path between two **different nets**, lifted the wrong net, and
    reported *"no path"* three times. The router cannot tell you that the
    question was wrong; it can only tell you the answer is no.
    ⭐ So the binding, when there is one, supplies **both**, and the pad is
    checked against the anchor's own pad-to-net table before it is used.
    """
    binding = bindings.get(ref)
    if binding is not None:
        pad = str(binding.anchor_pad)
        net = str(binding.net)
        if net_of_pad.get(pad) != net:
            return (net, pad, f"⚠ the binding names pad {pad} for {net} while "
                              f"that pad carries {net_of_pad.get(pad)!r}")
        return (net, pad, "")
    net = _pick_net(shared, by_net)
    if net is None:
        return None
    return (net, by_net[net][0], "")


def template_port_member(refs, net: str, anchor_nets, nets_by_ref) -> tuple:
    """⭐⭐ **S4.** Which member of a template cell actually carries ``net``.

    ⛔⛔ **This exists because the shipped answer is wrong twice on this corpus
    and would have been wrong three times on S4's own subject list.**
    :func:`side_neighbours` used ``other.refs[0]`` -- *the alphabetically first
    member* -- as a template's representative, and on
    ``ltc1871_sepic``'s ``rc_snubber:CC1-RC`` (bound to ``ITH``),
    ``lt3844_buck``'s ``rc_snubber:CC-RC`` (bound to ``VC``) and
    ``lt8710_sepic``'s ``rc_snubber:CC-RC`` (bound to ``VC``) that member has
    **no pad on the bound net at all** -- ``CC``/``CC1`` sit on the snubber's
    internal node and on ground. It was harmless in S3 **only because templates
    were never routed**: the moment one is, the loop asks the router for a path
    between two different nets, and *the router cannot tell you the question was
    wrong; it can only tell you the answer is no.*

    The rule, stated once and asserted by the driver against what the code
    emits: **a pad on the bound net**, ties by the most **plane-free** nets
    shared with the anchor, then the smallest ref -- a total key over content.
    """
    from .ratnest import is_plane_net

    on_net = [ref for ref in sorted(refs)
              if net in set(nets_by_ref.get(ref, ()))]
    if not on_net:
        raise ConstructError(
            f"no member of {sorted(refs)} has a pad on {net!r}, which is the "
            f"net the template was bound to -- the shared-net set and the "
            f"member net sets disagree (bail-out 6)")

    def _key(ref):
        shared = set(nets_by_ref.get(ref, ())) & set(anchor_nets)
        free = [n for n in shared if not is_plane_net(n)]
        return (-len(free), ref)

    best = sorted(on_net, key=_key)[0]
    return (best, f"the port member: it has a pad on {net}, "
                  f"{len(set(nets_by_ref.get(best, ())) & set(anchor_nets))} "
                  f"net(s) shared with the anchor; candidates {on_net}")


def _distribute_stacked(neighbours, by_net, escape_map, anchor_rot_deg, side,
                        positions, footprint) -> tuple:
    """⭐ **S4 policy: spread a stacked binding across the rail's own pads.**

    ⛔⛔ The bindings **stack**, and it is the opposite of the problem P2
    anticipated: :class:`~skidl_layout.cells_partition.PinBinding` names one
    anchor pad and ``decaps._pads_for_net`` sorts by pad number, so on
    ``lt8710_sepic`` all **seven** ``CIN*`` bind to pad 13 and all **five**
    ``COUT*`` to pad 6. S3 priced the consequence: the along-the-edge cursor
    stacks them into a column and the connecting pairs get **longer and
    longer** (3.9 -> 8.0 -> 12.2 mm on ``lt3844_buck``). ⭐ What survived is the
    guarantee -- the pair lines do not cross -- so **what a stack costs is
    LENGTH, not legality.**

    The policy fires only when it can: at least two neighbours on one anchor
    pad **and** at least two pads of the anchor carrying that net whose
    favoured side is **this** side. ⛔ Never across sides -- spreading a bank
    over two sides is a design decision S4 has no mandate for.
    ⚠ The multiplicities are read from the **parsed pads**, never from the
    binding's prose.
    """
    from .escape_map import favored_side, rotate_escape

    buckets: dict = {}
    for neighbour in neighbours:
        buckets.setdefault((neighbour.net, neighbour.anchor_pad),
                           []).append(neighbour)
    replacements: dict = {}
    moves: list = []
    for (net, pad), members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        here = []
        for candidate in by_net.get(net, ()):
            try:
                local = favored_side(escape_map, candidate)
            except ValueError:
                continue
            if rotate_escape(local, int(anchor_rot_deg)) == side:
                here.append(str(candidate))
        here = sorted(set(here), key=lambda p: (round(_pad_edge_coordinate(
            positions, p, side, anchor_rot_deg, footprint), 6), _pad_key(p)))
        if len(here) < 2:
            continue
        for index, neighbour in enumerate(sorted(members,
                                                 key=lambda n: n.order_key)):
            target = here[index % len(here)]
            if target == neighbour.anchor_pad:
                continue
            replacements[neighbour.ref] = replace(
                neighbour, anchor_pad=target,
                order_key=(round(_pad_edge_coordinate(
                    positions, target, side, anchor_rot_deg, footprint), 6),
                    _pad_key(target), str(neighbour.ref)),
                reason=f"{neighbour.reason}; distributed from pad {pad} to pad "
                       f"{target} -- {len(members)} neighbours bind to pad "
                       f"{pad} while the anchor carries {net} on {here} on "
                       f"this side (parsed pads, not the binding's prose)")
            moves.append({"ref": neighbour.ref, "net": net, "from_pad": pad,
                          "to_pad": target, "pads_on_side": list(here),
                          "group_size": len(members)})
    if not replacements:
        return tuple(neighbours), []
    out = [replacements.get(n.ref, n) for n in neighbours]
    return (tuple(sorted(out, key=lambda n: n.order_key)),
            sorted(moves, key=lambda m: m["ref"]))


def side_neighbours(partition, anchor: str, escape_map, *, side: str,
                    anchor_pads=None, anchor_geometry=None,
                    anchor_rot_deg: int = 0, nets_by_ref=None,
                    flatten_templates: bool = False,
                    distribute_stacked: bool = False,
                    ) -> tuple[Neighbour, ...]:
    """Overview P1 + P2 for one side. **Pure netlist + footprint geometry.**

    ``anchor_pads`` are the anchor's pads as the session parsed them (pad
    numbers and net names); ``anchor_geometry`` is its
    :class:`~skidl_layout.geometry.FootprintGeometry`, which is where the
    ordering rule's **measured** pad coordinates come from.

    ⛔ Returns every neighbour that lands on ``side``, ordered by
    :attr:`Neighbour.order_key`. A neighbour whose anchor pad has no escapable
    side, or which shares no net with the anchor at all, is **not** returned --
    :func:`construct_side` records every one of them in ``skipped`` with its
    reason. This function is the *pure* half and is tested on its own.

    ⛔ ``flatten_templates`` and ``distribute_stacked`` are **S4 policies and
    both default OFF**, because the OFF arm is the control gate ``FC0`` diffs
    against S3's recorded artifact. ON, a template's slot is taken by its
    :func:`template_port_member` instead of by its alphabetically first member,
    and a stacked binding is spread by :func:`_distribute_stacked`.
    """
    from .escape_map import favored_side, rotate_escape

    group = partition.group_of(anchor)
    if group is None:
        raise ConstructError(
            f"{anchor!r} is in no group of this partition -- an inventory over "
            f"nothing is indistinguishable from one that found everything")
    if side not in SIDES:
        raise ConstructError(f"side {side!r} is not one of {SIDES}")

    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    anchor_nets = set(nets_by_ref.get(anchor, ()))
    pads = list(anchor_pads or ())
    by_net = _anchor_pads_by_net(pads)
    if not by_net:
        raise ConstructError(
            f"{anchor!r}: not one of its {len(pads)} parsed pads carries a net "
            f"name -- an inventory built on that would observe nothing (rule 3)")
    numbers = {str(pad.pad_number) for pad in pads}
    net_of_pad = {str(pad.pad_number): str(getattr(pad, "net_name", "") or "")
                  for pad in pads}
    positions = _pad_local_positions(anchor_geometry) if anchor_geometry \
        else {}
    bindings = {b.ref: b for b in group.bindings}

    candidates: list = []
    for ref in sorted(group.refs):
        if ref == anchor:
            continue
        klass, shared, why = _classify(ref, anchor_nets, nets_by_ref, bindings)
        candidates.append((ref, klass, shared, (), why))
    # ⭐ P1 says *"every part **and cell** that connects to A"*. A family group
    # is a cell, so it is inventoried here -- which is also what keeps the
    # ``template`` class from being a constant that matches nothing
    # (standing finding 20).
    for other in sorted(partition.groups, key=lambda g: g.name):
        if other.kind != "family" or anchor in other.refs:
            continue
        shared = _shared_nets(other.refs, anchor_nets, nets_by_ref)
        if not shared:
            continue
        representative, why = other.refs[0], (f"the cell {other.name} has a "
                                              f"port on the anchor")
        if flatten_templates:
            bound_net = _pick_net(shared, by_net)
            if bound_net is None:
                continue
            representative, port_why = template_port_member(
                other.refs, bound_net, anchor_nets, nets_by_ref)
            why = (f"the cell {other.name} has a port on the anchor; "
                   f"FLATTENED -- {port_why} (S3 would have used "
                   f"{other.refs[0]})")
        candidates.append((representative, "template", shared,
                           tuple(other.refs), why))

    out: list[Neighbour] = []
    for ref, klass, shared, members, why in candidates:
        if not shared:
            continue
        bound = _bind(ref, shared, by_net, net_of_pad, bindings)
        if bound is None:
            continue
        net, pad, warning = bound
        if pad not in numbers:
            continue
        try:
            local = favored_side(escape_map, pad)
        except ValueError:
            continue
        world = rotate_escape(local, int(anchor_rot_deg))
        if world != side:
            continue
        out.append(Neighbour(
            ref=ref, anchor_pad=pad, net=net, side=side, klass=klass,
            priority=_CLASS_RANK[klass] + len(shared),
            order_key=(round(_pad_edge_coordinate(
                positions, pad, side, anchor_rot_deg,
                escape_map.footprint), 6), _pad_key(pad), str(ref)),
            anchor_pads_on_net=len(by_net.get(net, ())),
            members=members,
            reason=f"{why}; shares {shared} with {anchor}; pad {pad} carries "
                   f"{net} and favours {local} (world {world} at rotation "
                   f"{anchor_rot_deg}){'; ' + warning if warning else ''}"))
    ordered = tuple(sorted(out, key=lambda n: n.order_key))
    if distribute_stacked and ordered:
        ordered, _moves = _distribute_stacked(
            ordered, by_net, escape_map, anchor_rot_deg, side, positions,
            escape_map.footprint)
    return ordered


# --------------------------------------------------------------------------- #
# P4 and P5 -- rotation, standoff, place, then ask the router
# --------------------------------------------------------------------------- #
def construct_side(partition, anchor: str, *, side: str, session, geometries,
                   fab, escape_map, board: str = "",
                   anchor_x_mm: float = 0.0, anchor_y_mm: float = 0.0,
                   anchor_rot_deg: int = 0, nets_by_ref=None,
                   route: bool = True) -> SideResult:
    """Overview P1-P5 for one side of one anchor. ⭐ The whole of S3.

    ``session`` is a live :class:`~skidl_layout.route_session.RouteSession`
    whose base map holds the board edge and **nothing this loop will place**;
    ``geometries`` is ``{ref: FootprintGeometry}``; ``escape_map`` is the
    anchor's :class:`~skidl_layout.escape_map.EscapeMap`.

    ⚠ ``anchor_x_mm`` / ``anchor_y_mm`` / ``anchor_rot_deg`` are an addition to
    the plan's stated signature: the anchor has to be *somewhere* before a
    neighbour can be placed relative to it, and the stated signature named no
    way to say where. Recorded in the run report rather than slipped in.

    ⛔ **Routable is a DISQUALIFIER, not a score** (overview 7.3). A position
    that fails to route is rejected and goes down the slide ladder; it is never
    penalised and kept. ⛔⛔ **And nothing the session returns becomes a
    judge** (overview 7.4): the frozen judge stays ``ratnest.analyse_board``
    plus a full route, and S3 does not grade.

    ⭐ The session is left **exactly** as it was found: a snapshot is taken on
    entry and restored on exit, so the exactness invariant is exercised on
    every call rather than asserted about.
    """
    if anchor not in geometries:
        raise ConstructError(f"no footprint geometry for the anchor {anchor!r}")
    anchor_geom = geometries[anchor]
    anchor_fp = session.pcb.footprints.get(anchor)
    if anchor_fp is None:
        raise ConstructError(
            f"{anchor!r} is not on the session's board {session.pcb_path!r}")
    if escape_map.footprint != anchor_geom.footprint:
        raise ConstructError(
            f"the escape map describes {escape_map.footprint!r} but the "
            f"anchor's geometry is {anchor_geom.footprint!r} (bail-out 6)")
    anchor_pads = list(anchor_fp.pads)

    base = standoff_base_mm(fab)
    gap = edge_gap_mm(fab)
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    neighbours = side_neighbours(partition, anchor, escape_map, side=side,
                                 anchor_pads=anchor_pads,
                                 anchor_geometry=anchor_geom,
                                 anchor_rot_deg=anchor_rot_deg,
                                 nets_by_ref=nets_by_ref)
    skipped = _skips(partition, anchor, escape_map, anchor_pads, nets_by_ref,
                     neighbours, anchor_rot_deg)
    if not neighbours:
        raise ConstructError(
            f"{board}/{anchor}/{side}: the side list is EMPTY. An instrument "
            f"that can observe nothing must raise, never return a falsy "
            f"result (rule 3). Skips recorded: "
            f"{[s['ref'] for s in skipped][:8]}")

    snapshot = session.snapshot()
    anchor_court = _courtyard_box(anchor_geom, anchor, anchor_x_mm,
                                  anchor_y_mm, anchor_rot_deg)
    anchor_extent = _side_extent(anchor_court, anchor_x_mm, anchor_y_mm, side)
    anchor_moved = moved_pads(anchor_pads, x_mm=anchor_x_mm, y_mm=anchor_y_mm,
                              rot_deg=anchor_rot_deg,
                              base_rot_deg=anchor_fp.rotation)
    by_number = {str(pad.pad_number): pad for pad in anchor_moved}
    session.add_part(f"@ANCHOR:{anchor}", anchor_moved)

    boxes_phys = {anchor: _physical_box(anchor_geom, anchor, anchor_x_mm,
                                        anchor_y_mm, anchor_rot_deg)}
    boxes_court = {anchor: anchor_court}
    placed_parts = [_placed(anchor, anchor_x_mm, anchor_y_mm, anchor_rot_deg,
                            anchor_geom.footprint)]

    placements: list[Placement] = []
    failures: list[dict] = []
    disagreements: list[dict] = []
    rungs_used: dict = {}
    cursor = None
    timings: list[float] = []

    for neighbour in neighbours:
        if neighbour.klass == "template":
            skipped.append({
                "ref": neighbour.ref, "members": list(neighbour.members),
                "anchor_pad": neighbour.anchor_pad, "net": neighbour.net,
                "side": neighbour.side,
                "why": "a template cell is placed as a UNIT and cell placement "
                       "is S4/S5 -- inventoried and side-assigned here, not "
                       "placed"})
            continue
        geometry = geometries.get(neighbour.ref)
        if geometry is None:
            skipped.append({"ref": neighbour.ref, "side": neighbour.side,
                            "why": "no footprint geometry (standing finding 6)"})
            continue
        footprint = session.pcb.footprints.get(neighbour.ref)
        if footprint is None:
            skipped.append({"ref": neighbour.ref, "side": neighbour.side,
                            "why": "not on the session's board"})
            continue
        outcome = _place_one(
            neighbour, geometry, footprint, session=session,
            anchor_pad=by_number.get(neighbour.anchor_pad), side=side,
            anchor_x_mm=anchor_x_mm, anchor_y_mm=anchor_y_mm,
            anchor_extent=anchor_extent, base=base, gap=gap, cursor=cursor,
            boxes_phys=boxes_phys, boxes_court=boxes_court,
            placed_parts=placed_parts, clearance_mm=float(fab.clearance_mm),
            route=route)
        for name in outcome["rungs"]:
            rungs_used[name] = rungs_used.get(name, 0) + 1
        if outcome["placement"] is None:
            failures.append(outcome["failure"])
            continue
        placement = outcome["placement"]
        placements.append(placement)
        cursor = outcome["cursor"]
        if placement.route_elapsed_s is not None:
            timings.append(placement.route_elapsed_s)
        access = escape_map.access(neighbour.anchor_pad,
                                   _unrotate(side, anchor_rot_deg))
        if (access == "BLOCKED") != (not placement.routed):
            disagreements.append({
                "ref": neighbour.ref, "anchor_pad": neighbour.anchor_pad,
                "side": side, "map_says": access,
                "router_says": "routed" if placement.routed else "unrouted",
                "direction": ("the map was OPTIMISTIC" if not placement.routed
                              else "the map UNDER-REPORTED")})

    census_before_rollback = session.census
    session.restore(snapshot)

    from .validator import validate

    used = {ref: geometries[ref] for ref in boxes_phys if ref in geometries}
    result = validate(placed_parts, None,
                      {g.footprint: (g.width_mm, g.height_mm)
                       for g in used.values()},
                      clearance_mm=float(fab.clearance_mm),
                      fp_geometries={g.footprint: g for g in used.values()})
    refs = sorted(boxes_court)
    court_overlaps = sorted(
        [a, b] for i, a in enumerate(refs) for b in refs[i + 1:]
        if _pair_gap(boxes_court[a], boxes_court[b]) < -_TOL)
    gap_min = courtyard_gap(boxes_court)

    meta = {
        "anchor_footprint": anchor_geom.footprint,
        "anchor_x_mm": round(float(anchor_x_mm), 6),
        "anchor_y_mm": round(float(anchor_y_mm), 6),
        "anchor_rot_deg": int(anchor_rot_deg),
        "anchor_courtyard_extent_mm": anchor_extent,
        "anchor_courtyard_declared": anchor_geom.courtyard_bounds is not None,
        "edge_gap_mm": gap,
        "edge_gap_terms": [list(t) for t in EDGE_GAP_TERMS],
        "fab": str(getattr(fab, "name", "")),
        "clearance_mm": float(fab.clearance_mm),
        "track_width_mm": float(fab.track_width_mm),
        "via_size_mm": float(fab.via_size_mm),
        "physical_overlaps": [list(pair) for pair in result.overlaps],
        "courtyard_overlaps": court_overlaps,
        "courtyard_min_gap_mm": None if gap_min == float("inf") else gap_min,
        "courtyard_missing": sorted(
            ref for ref, g in used.items() if g.courtyard_bounds is None),
        "map_vs_router": disagreements,
        "classes_declared": list(NEIGHBOUR_CLASSES),
        "classes_seen": sorted({n.klass for n in neighbours}),
        "class_census": _census(n.klass for n in neighbours),
        #: ⚠ What the plan's stated order WOULD have produced. Reported, never
        #: used -- see :func:`_classify`. ``decoupling`` is empty here on every
        #: corpus board, which is the whole finding.
        "class_census_plan_literal": _census(
            _classify_plan_literal(
                n.ref, set(nets_by_ref.get(anchor, ())), nets_by_ref,
                {b.ref: b for b in partition.group_of(anchor).bindings})
            for n in neighbours if n.klass != "template"),
        "classes_placed": sorted({n.klass for n in neighbours
                                  if n.ref in {p.ref for p in placements}}),
        "ladder_rungs_declared": list(LADDER_RUNGS),
        "ladder_rungs_used": dict(sorted(rungs_used.items())),
        "stacked_anchor_pads": sorted(
            {n.anchor_pad for n in neighbours
             if sum(1 for m in neighbours
                    if m.anchor_pad == n.anchor_pad) > 1}),
        "monotone": _is_monotone(placements, side),
        "route_calls": len(timings),
        "rollback_exact": bool(session.census == snapshot.census),
        "census_at_end_of_construction": list(census_before_rollback),
        "census_after_rollback": list(session.census),
        "timing": {"route_total_s": round(sum(timings), 6),
                   "route_max_s": round(max(timings), 6) if timings else None,
                   "route_mean_s": (round(sum(timings) / len(timings), 6)
                                    if timings else None)},
    }
    return SideResult(board=board, anchor=anchor, side=side,
                      neighbours=neighbours, placements=tuple(placements),
                      failures=tuple(failures),
                      skipped=tuple(sorted(skipped,
                                           key=lambda s: str(s["ref"]))),
                      standoff_base_mm=base, meta=meta)


def _census(values) -> dict:
    out: dict = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items()))


def _unrotate(side: str, deg: int) -> str:
    """The rotation-0 side that shows up as ``side`` after turning ``deg``."""
    from .escape_map import rotate_escape

    for candidate in SIDES:
        if rotate_escape(candidate, int(deg)) == side:
            return candidate
    raise ConstructError(f"no rotation-0 side maps to {side!r} at {deg} deg")


def _skips(partition, anchor, escape_map, anchor_pads, nets_by_ref,
           neighbours, anchor_rot_deg) -> list:
    """⛔ Every member the side list did NOT take, and why. Never silent."""
    from .escape_map import favored_side, rotate_escape

    group = partition.group_of(anchor)
    taken = {n.ref for n in neighbours}
    by_net = _anchor_pads_by_net(anchor_pads)
    net_of_pad = {str(pad.pad_number): str(getattr(pad, "net_name", "") or "")
                  for pad in anchor_pads}
    anchor_nets = set(nets_by_ref.get(anchor, ()))
    bindings = {b.ref: b for b in group.bindings}
    out: list = []
    for ref in sorted(group.refs):
        if ref == anchor or ref in taken:
            continue
        shared = sorted(set(nets_by_ref.get(ref, ())) & anchor_nets)
        if not shared:
            out.append({"ref": ref,
                        "why": "shares NO net with the anchor -- it is in the "
                               "group because the power-stage classifier put "
                               "it there, not because it touches a pad"})
            continue
        bound = _bind(ref, shared, by_net, net_of_pad, bindings)
        if bound is None:
            out.append({"ref": ref,
                        "why": f"no pad of {anchor} carries any of {shared}"})
            continue
        net, pad, _warning = bound
        try:
            local = favored_side(escape_map, pad)
        except ValueError:
            out.append({"ref": ref, "anchor_pad": pad, "net": net,
                        "why": ANCHOR_PAD_NOT_ESCAPABLE})
            continue
        out.append({"ref": ref, "anchor_pad": pad, "net": net,
                    "side": rotate_escape(local, int(anchor_rot_deg)),
                    "why": "belongs to another side of this anchor"})
    return out


def _ladder(chosen_rot: int):
    """⛔ Overview 7.2's ladder, materialised as an ordered, bounded sequence of
    ``(rung name, rotation, slide steps, push steps)``.

    ⛔ **Never slide perpendicular first**: every slide of the chosen rotation
    is tried before any other rotation, and every rotation before any push.
    """
    yield (LADDER_RUNGS[0], chosen_rot, 0, 0)
    for step in range(1, MAX_SLIDE_STEPS + 1):
        yield (LADDER_RUNGS[0], chosen_rot, step, 0)
    for rot in ROTATIONS:
        if rot != chosen_rot:
            yield (LADDER_RUNGS[1], rot, 0, 0)
    for push in range(1, MAX_PUSH_STEPS + 1):
        yield (LADDER_RUNGS[2], chosen_rot, 0, push)


def _connecting_pad_offsets(footprint, neighbour, side) -> dict:
    """``{rotation: the connecting pad's offset along the edge}``, MEASURED.

    ⭐ **S4's ``align_connecting_pad`` policy, and it exists because of an
    eyes-on pass rather than a number.** S3 aligned the neighbour's *origin* to
    the anchor pad's edge coordinate, so a part whose connecting pad is not at
    its origin lands visibly skewed against the pad it is there to reach
    (``RT`` sat below ``U1`` pad 4 on ``ltc1871_sepic``). ⛔ The connecting pad
    is the one with the **smallest pad key** among the neighbour's pads on the
    bound net -- a total key over content, and on this corpus every one of them
    is a set of size one.
    """
    _axis, _sign, along = _AXES[side]
    out: dict = {}
    for rot in ROTATIONS:
        mine = [pad for pad in moved_pads(
            list(footprint.pads), x_mm=0.0, y_mm=0.0, rot_deg=rot,
            base_rot_deg=footprint.rotation)
            if str(getattr(pad, "net_name", "") or "") == neighbour.net]
        if not mine:
            continue
        pad = sorted(mine, key=lambda p: _pad_key(p.pad_number))[0]
        out[rot] = round(pad.global_x if along == 0 else pad.global_y, 6)
    return out


def _place_one(neighbour, geometry, footprint, *, session, anchor_pad, side,
               anchor_x_mm, anchor_y_mm, anchor_extent, base, gap, cursor,
               boxes_phys, boxes_court, placed_parts, clearance_mm,
               route, align_connecting_pad: bool = False,
               busy_extra: float | None = None,
               slot: float | None = None, allowance: float | None = None,
               forced_rot: int | None = None,
               commit_copper: bool = False) -> dict:
    """P4 + P5 + the slide ladder, for one neighbour.

    ⭐ ``slot``/``allowance``/``forced_rot`` are **P-F's three additions and all
    three default to ``None``**, so the linear arm walks exactly the code S3/S4/
    S5/S5B walked. ``slot`` replaces the unbounded cursor with the ring's own
    along-edge coordinate; ``allowance`` keeps ``fanout_allowance_mm`` meaning
    the allowance once ``base`` has become the radius; ``forced_rot`` hands back
    the rotation the ring **sized itself against**, because a rotation chosen
    after the radius would make the radius wrong (the two-pass scheme is stated
    in :func:`construct_cell`).
    """
    _axis, _sign, along = _AXES[side]
    if anchor_pad is None:
        return {"placement": None, "cursor": cursor, "rungs": (),
                "token": None, "failure": {
                    "ref": neighbour.ref, "step": "inventory",
                    "why": f"pad {neighbour.anchor_pad} is not on the anchor's "
                           f"board footprint", "router": None}}
    target = (anchor_pad.global_x, anchor_pad.global_y)
    desired = target[along]
    centre_along = anchor_x_mm if along == 0 else anchor_y_mm
    offsets = (_connecting_pad_offsets(footprint, neighbour, side)
               if align_connecting_pad else {})

    chosen = (int(forced_rot) if forced_rot is not None else
              _choose_rotation(neighbour, geometry, footprint, side,
                               anchor_extent, base, anchor_x_mm, anchor_y_mm,
                               desired, target, centre_along, gap,
                               align_offsets=offsets, busy_extra=busy_extra))
    attempts: list[dict] = []
    rungs: list[str] = []
    for index, (name, rot, slide, push) in enumerate(_ladder(chosen)):
        x_mm, y_mm, standoff, next_cursor, terms = _position_for(
            geometry, neighbour, side, rot, anchor_extent,
            base * (1 + push), anchor_x_mm, anchor_y_mm, desired, cursor,
            gap, slide, centre_along, align_offset=offsets.get(rot, 0.0),
            busy_extra=busy_extra,
            # ⛔ The slot is the ring's own answer for **this** rotation. A
            # ladder rung that changes rotation changes the courtyard extent, so
            # the slot stops being the sized one -- the run may then not fit, and
            # `ring_slots`' named failure is the honest outcome rather than a
            # silent overlap. The two-box legality test below is the backstop.
            slot=slot, allowance=allowance)
        phys = _physical_box(geometry, neighbour.ref, x_mm, y_mm, rot)
        court = _courtyard_box(geometry, neighbour.ref, x_mm, y_mm, rot)
        clash_phys = sorted(ref for ref, box in boxes_phys.items()
                            if _pair_gap(box, phys) < clearance_mm - _TOL)
        clash_court = sorted(ref for ref, box in boxes_court.items()
                             if _pair_gap(box, court) < -_TOL)
        if clash_phys or clash_court:
            # ⛔ Legality is tested BEFORE the route, on BOTH boxes (7.1), and a
            # candidate that fails it is EXCLUDED rather than scored (7.3).
            attempts.append({"rung": name, "index": index, "rot": rot,
                             "slide": slide, "push": push,
                             "x_mm": round(x_mm, 4), "y_mm": round(y_mm, 4),
                             "why": f"collides -- physical {clash_phys}, "
                                    f"courtyard {clash_court}"})
            rungs.append(name)
            continue
        pads = moved_pads(list(footprint.pads), x_mm=x_mm, y_mm=y_mm,
                          rot_deg=rot, base_rot_deg=footprint.rotation)
        token = session.add_part(f"@N:{neighbour.ref}", pads)
        # ⭐⭐⭐ **S7 C3/C4, and it is the whole point of committing copper.**
        # With ``commit_copper`` ON the question is aimed at the union of the
        # anchor pad's terminals and **every cell of this net's already-
        # committed copper**, so the A* terminates at the nearest of the two --
        # requirement 2's *"from the closest wire, else the closest pad"* in one
        # call rather than two questions and a comparison.
        # ⚠ Committed copper is an obstacle for OTHER nets; for its own net it
        # has to be a TARGET, which is why ``route_pair``'s ``lift_own_net``
        # (already the default) runs first.
        taps = (session.committed_terminals(neighbour.net)
                if commit_copper else None)
        answer = _route_pair(session, pads, neighbour, anchor_pad,
                             keep_copper=commit_copper, extra_targets=taps) \
            if route else None
        if answer is not None and not answer.routed:
            session.remove_part(token)
            attempts.append({"rung": name, "index": index, "rot": rot,
                             "slide": slide, "push": push,
                             "x_mm": round(x_mm, 4), "y_mm": round(y_mm, 4),
                             "why": f"the connecting pair did not route: "
                                    f"{answer.failure}",
                             "iterations": answer.iterations})
            rungs.append(name)
            continue
        boxes_phys[neighbour.ref] = phys
        boxes_court[neighbour.ref] = court
        placed_parts.append(_placed(neighbour.ref, x_mm, y_mm, rot,
                                    geometry.footprint))
        rungs.append(name)
        # ⛔ S7 C4: the ACCEPTED pair's copper becomes an obstacle for the next
        # neighbour's question. ⭐ The ledger is the SESSION's own commit stack;
        # a second one here would be standing finding 29's defect.
        if commit_copper and answer is not None and answer.routed \
                and answer.has_copper:
            session.commit_pair(answer, net=neighbour.net)
        return {"placement": Placement(
            ref=neighbour.ref, x_mm=round(x_mm, 6), y_mm=round(y_mm, 6),
            rot_deg=int(rot), standoff_mm=round(standoff, 6),
            slide_steps=index,
            routed=bool(answer.routed) if answer is not None else False,
            route_length_mm=answer.length_mm if answer is not None else None,
            route_iterations=answer.iterations if answer is not None else None,
            route_elapsed_s=answer.elapsed_s if answer is not None else None,
            route_failure=answer.failure if answer is not None else None,
            anchor_pad=neighbour.anchor_pad, net=neighbour.net,
            standoff_terms=terms,
            reasons=(f"rotation {rot}, chosen from the {ROTATIONS} enumeration",
                     f"ladder rung {index}: {name} "
                     f"(slide {slide}, push {push})",
                     f"the connecting pad is nearest the anchor at this "
                     f"rotation")
            + ((f"aligned by the CONNECTING PAD (offset "
                f"{offsets.get(rot, 0.0)} mm along the edge), not by the "
                f"part origin",) if align_connecting_pad else ())
            + ((f"a BUSY-ANCHOR keep-out of {round(float(busy_extra), 6)} mm "
                f"is in the standoff and OUTSIDE what the tighten may "
                f"recover",) if busy_extra is not None else ())),
            "cursor": next_cursor, "rungs": rungs, "failure": None,
            "token": token, "pads": pads, "target": anchor_pad}
    return {"placement": None, "cursor": cursor, "rungs": rungs,
            "token": None, "failure": {
        "ref": neighbour.ref, "anchor_pad": neighbour.anchor_pad,
        "net": neighbour.net, "side": side, "klass": neighbour.klass,
        "step": LADDER_RUNGS[3],
        "why": f"the slide ladder ran out after {len(attempts)} rung(s) "
               f"({MAX_SLIDE_STEPS} slides, {len(ROTATIONS) - 1} rotations, "
               f"{MAX_PUSH_STEPS} pushes)",
        "rungs": len(attempts), "attempts": attempts[-8:],
        "router": attempts[-1].get("why") if attempts else None}}


#: ⛔ The residue one pair question leaves behind that the refcount census
#: cannot see. Cleared through KRT's own **public** methods -- escalation rung 2
#: ("call KRT's existing public functions"), never a KRT edit.
_ROUTER_RESIDUE = ("clear_free_vias", "clear_allowed_cells",
                   "clear_endpoint_exempt")


def reset_router_residue(session) -> dict:
    """⛔⛔⛔ **A pair question MUTATES the map in a way the census cannot see,
    and the next identical question then gets a different answer.**

    MEASURED 2026-08-03 on ``ltc1871_sepic``: the *same* pad pair, on the *same*
    obstacle map, reports **3 270** router iterations the first time and
    **5 041** every time after -- while
    :attr:`~skidl_layout.route_session.RouteSession.census` is byte-identical
    and ``probe_pair``'s "the map was not mutated" assertion passes. The cause,
    **read from the code path rather than guessed** (rule 8): KRT's #189
    via-in-pad unblock calls ``_register_unblock_via`` when an endpoint pad is
    boxed in, which does ``add_free_via`` + ``add_source_target_cell`` +
    ``add_allowed_cell`` -- and **nothing removes them**, because the function
    that does (``restore_obstacles_inplace``,
    ``routing_context.py:459``) belongs to the batch flow a session
    deliberately does not use. ``GridObstacleMap.get_stats()`` shows it plainly:
    ``free_vias 0 -> 1``, ``source_target 2 -> 5``.

    ⭐ Isolated to a single lever: clearing **free vias alone** restores 3 270
    exactly, with the routed verdict, the length and the via count unchanged.

    ⛔ This is not only a determinism fix. A free via registered by a *previous*
    question is a rescue for a *different* pad that was never placed on any
    board, and leaving it lets the next question route through copper nobody
    committed -- the optimistic direction, which is the one this arc keeps
    paying for. **Clearing it makes each trial question independent, which is
    what a trial question is.**

    ⚠ ``source_target_cells`` is deliberately **not** cleared: the base map
    already owns two of them, and clearing something this loop did not create
    would be a different change.
    """
    obstacles = getattr(session, "obstacles", None)
    cleared = []
    for name in _ROUTER_RESIDUE:
        method = getattr(obstacles, name, None)
        if callable(method):
            method()
            cleared.append(name)
    return {"cleared": cleared,
            "stats": list(obstacles.get_stats())
            if hasattr(obstacles, "get_stats") else []}


def _route_pair(session, pads, neighbour, anchor_pad, *,
                keep_copper: bool = False, extra_targets=None):
    """Ask the router about the connecting pair.

    ⛔⛔ ``lift_net`` is the NORMAL case here, not the exception: the pair's net
    is one the loop just stamped, so a pad would wall in its own escape.
    ``route_pair``'s default ``lift_own_net=True`` does it, and this honours
    that primitive's precondition -- **a part is stamped at most once**, which
    holds because every ``part_key`` here is a distinct ref.

    ⛔ Both ends are the **moved** pads, never the file's copies.
    ``probe_pair`` asserts the refcount census is unchanged afterwards; ⛔ that
    assertion does **not** cover the router's own rescue registrations, which is
    what :func:`reset_router_residue` is for.
    """
    mine = [pad for pad in pads
            if str(getattr(pad, "net_name", "") or "") == neighbour.net
            and session.has_copper(pad)]
    if not mine or not session.has_copper(anchor_pad):
        return None
    reset_router_residue(session)
    # ⛔ S7 C1/C3, both default OFF. ``probe_pair`` still asserts the census is
    # unchanged afterwards, so **asking for the copper does not make the
    # question a commit** -- committing is a separate, tokened act.
    return session.probe_pair(mine[0], anchor_pad, keep_copper=keep_copper,
                              extra_targets=extra_targets)


def _choose_rotation(neighbour, geometry, footprint, side, anchor_extent, base,
                     anchor_x_mm, anchor_y_mm, desired, target, centre_along,
                     gap, align_offsets=None, busy_extra=None) -> int:
    """⛔ A finite, ordered, deterministic enumeration with a stated tie-break.

    Each rotation is scored by the distance from the neighbour's **connecting**
    pad to the anchor's connecting pad -- that is what *"put the connecting pad
    toward A and the remaining pads away"* means, measured rather than
    asserted. Ties break on the **smallest** rotation.
    """
    best = None
    align_offsets = align_offsets or {}
    for rot in ROTATIONS:
        x_mm, y_mm, _s, _c, _t = _position_for(
            geometry, neighbour, side, rot, anchor_extent, base, anchor_x_mm,
            anchor_y_mm, desired, None, gap, 0, centre_along,
            align_offset=align_offsets.get(rot, 0.0), busy_extra=busy_extra)
        pads = moved_pads(list(footprint.pads), x_mm=x_mm, y_mm=y_mm,
                          rot_deg=rot, base_rot_deg=footprint.rotation)
        mine = [pad for pad in pads
                if str(getattr(pad, "net_name", "") or "") == neighbour.net]
        if not mine:
            continue
        distance = min(math.dist((pad.global_x, pad.global_y), target)
                       for pad in mine)
        key = (round(distance, 6), rot)
        if best is None or key < best:
            best = key
    return ROTATIONS[0] if best is None else best[1]


def _position_for(geometry, neighbour, side, rot, anchor_extent, base,
                  anchor_x_mm, anchor_y_mm, desired, cursor, gap, slide,
                  centre_along, align_offset: float = 0.0,
                  busy_extra: float | None = None,
                  slot: float | None = None,
                  allowance: float | None = None):
    """Where a neighbour sits at rotation ``rot``, and what the standoff cost.

    ⛔ The standoff's terms, each with its source:
    ``A's COURTYARD extent on that side`` + ``2 x (track + clearance) + via
    lane`` + ``the neighbour's own COURTYARD half-extent facing A``. The last
    is the term standing finding 13 is about -- a standoff computed off
    ``physical_bounds`` is short by the overhang, *and every test still passes
    at 0805*.
    """
    axis, sign, along = _AXES[side]
    local = geometry._transform_bounds(
        geometry.bounds, _placed(neighbour.ref, 0.0, 0.0, rot,
                                 geometry.footprint))
    facing = (-local[axis]) if sign > 0 else local[axis + 2]
    # ⛔⛔ **P-B's term is OUTSIDE the recoverable band by construction.** S4's
    # tighten spends the *entire* ``fanout_allowance_mm`` on 68 of 69
    # neighbours, and a keep-out that exists *because the anchor is busy* must
    # not be handed straight back -- so it is a **fifth term**, never a bigger
    # allowance. ``_tighten_cell``'s floor is ``standoff - allowance``, so the
    # busy extra survives the pass arithmetically rather than by a promise.
    standoff = anchor_extent + base + facing + float(busy_extra or 0.0)
    perpendicular = ((anchor_x_mm if axis == 0 else anchor_y_mm)
                     + sign * standoff)

    lo, hi = (local[1], local[3]) if along == 1 else (local[0], local[2])
    # ⭐ ``align_offset`` is S4's ``align_connecting_pad`` policy and is 0.0 on
    # the S3 path: with it, the neighbour's CONNECTING PAD lands on the anchor
    # pad's edge coordinate rather than the neighbour's ORIGIN.
    value = desired - float(align_offset)
    if slot is not None:
        # ⭐⭐ **P-F.** The ring hands the part a **slot** -- the along-edge
        # coordinate its courtyard's low edge must sit at -- and the unbounded
        # cursor is not consulted at all. ⛔ That is the whole of open question
        # 14's open half: a run laid into `capacity(side, R)` cannot advance off
        # the end of the edge it belongs to, because there is no "advance".
        value = float(slot) - lo
    elif cursor is not None:
        value = max(value, cursor - lo)
    if slide:
        # ⛔ *"away from A's centre"* -- the direction is the one the neighbour
        # already sits in, so the slide preserves P2's ordering, which is the
        # property the whole procedure exists to keep.
        value += (1.0 if desired >= centre_along else -1.0) * slide * gap
    next_cursor = value + hi + gap

    x_mm = perpendicular if axis == 0 else value
    y_mm = value if axis == 0 else perpendicular
    terms = (("anchor_courtyard_extent_mm", round(anchor_extent, 6),
              "A's courtyard extent on that side, MEASURED per anchor"),)
    if slot is None:
        terms += (("fanout_allowance_mm", round(base, 6),
                   "2 x (track + clearance) + via lane, from the FabSpec"),)
    else:
        # ⛔⛔ **Under P-F ``base`` is the RING RADIUS, not the allowance**, and
        # both numbers have to survive: ``_allowance_of`` reads
        # ``fanout_allowance_mm`` to build the tighten's floor, so a ring arm
        # that renamed it would silently hand the per-part pass a floor of
        # ``standoff - R``. Emitted only on the ring arm, so the linear arm's
        # ``standoff_terms`` stay byte-identical to S4's artifact.
        terms += (("ring_radius_mm", round(base, 6),
                   "P-F: capacity(side, R) = face + 2R, R derived from the "
                   "side's own demand -- this is `base`, per side"),
                  ("fanout_allowance_mm",
                   round(float(base if allowance is None else allowance), 6),
                   "2 x (track + clearance) + via lane, from the FabSpec -- "
                   "the ring's FLOOR, never its value"))
    terms += (("neighbour_courtyard_facing_mm", round(facing, 6),
               "the neighbour's own courtyard half-extent facing A, MEASURED"),)
    if busy_extra is not None:
        # ⛔ Emitted ONLY when P-B is on, so the OFF arm's ``standoff_terms``
        # is byte-identical to S4's recorded artifact (gate ``AR0``).
        terms += (("busy_anchor_extra_mm", round(float(busy_extra), 6),
                   "lane x (ceil(side_load_ratio) - 1) -- P-B, OUTSIDE the "
                   "band the tighten may recover"),)
    terms += (("standoff_mm", round(standoff, 6), "their sum"),)
    return x_mm, y_mm, standoff, next_cursor, terms


def _is_monotone(placements, side: str) -> bool:
    """⭐ The non-crossing-by-construction claim, asserted rather than trusted:
    no two placed neighbours are out of P2's order along the edge."""
    _axis, _sign, along = _AXES[side]
    values = [(p.x_mm if along == 0 else p.y_mm) for p in placements]
    return all(a <= b + 1e-6 for a, b in zip(values, values[1:]))


# =========================================================================== #
# S5 -- the three instruments. ⛔ NONE of these is a policy: they are what the
# four policies are MEASURED with, and two of them run on the control arm too.
# =========================================================================== #
def _orient(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _on_segment(ax, ay, bx, by, px, py) -> bool:
    return (min(ax, bx) - _TOL <= px <= max(ax, bx) + _TOL
            and min(ay, by) - _TOL <= py <= max(ay, by) + _TOL)


def _segments_cross(a, b) -> bool:
    """Do the two segments share a point? ⚠ Endpoints count -- the caller has
    already split off the shared-endpoint case, which is the one that matters."""
    (ax, ay), (bx, by) = a[0], a[1]
    (cx, cy), (dx, dy) = b[0], b[1]
    d1 = _orient(ax, ay, bx, by, cx, cy)
    d2 = _orient(ax, ay, bx, by, dx, dy)
    d3 = _orient(cx, cy, dx, dy, ax, ay)
    d4 = _orient(cx, cy, dx, dy, bx, by)
    if ((d1 > _TOL and d2 < -_TOL) or (d1 < -_TOL and d2 > _TOL)) and \
            ((d3 > _TOL and d4 < -_TOL) or (d3 < -_TOL and d4 > _TOL)):
        return True
    # ⚠ Collinear / touching. Rare on real pad centres and cheap to be right
    # about, and *"rare"* is exactly how the five SW1 crossings stayed invisible.
    if abs(d1) <= _TOL and _on_segment(ax, ay, bx, by, cx, cy):
        return True
    if abs(d2) <= _TOL and _on_segment(ax, ay, bx, by, dx, dy):
        return True
    if abs(d3) <= _TOL and _on_segment(cx, cy, dx, dy, ax, ay):
        return True
    if abs(d4) <= _TOL and _on_segment(cx, cy, dx, dy, bx, by):
        return True
    return False


def _shares_endpoint(a, b) -> bool:
    for p in (a[0], a[1]):
        for q in (b[0], b[1]):
            if abs(p[0] - q[0]) <= 1e-6 and abs(p[1] - q[1]) <= 1e-6:
                return True
    return False


def pair_crossings(lines) -> dict:
    """⭐⭐⭐ **S5's new instrument, and it exists because a claim was read off
    a picture.**

    S4's run report said *"no pair line crosses another on any side of any
    subject"*. It was read off the renders and it is **FALSE** -- measured
    afterwards, **5** of 69 pair lines genuinely cross a same-side neighbour's
    (all ``stm32_bluepill``/``SW1``) and **16** involve a ring-2 line. ⭐ What the
    code asserts, and what held on 4 of 4, is :func:`_is_monotone`; **monotone
    ordering is a non-crossing guarantee only when the parts share a
    perpendicular offset, and the standoff is derived PER PART.**

    S5 changes exactly that property, so the census is a **gate** rather than a
    report (plan bail-out 3), and it is counted **in code, before and after**.

    ``lines`` is an iterable of ``((x1, y1), (x2, y2), ref, side, ring)``.
    ⚠ **The line is the PAIR, not the copper** -- a ``PairResult`` carries no
    path and inventing one is forbidden.

    ⛔ Lines meeting at a **shared endpoint** are not crossings: 44 of S4's 65
    raw intersections are the stacked binding fanning onto one anchor pad.
    """
    lines = [tuple(line) for line in lines]
    counts = {kind: 0 for kind in CROSSING_KINDS}
    detail: list = []
    for i, a in enumerate(lines):
        for b in lines[i + 1:]:
            if _shares_endpoint(a, b):
                kind = "shared_endpoint"
            elif not _segments_cross(a, b):
                continue
            elif int(a[4]) == 2 or int(b[4]) == 2:
                kind = "ring2"
            elif a[3] == b[3]:
                kind = "same_side"
            else:
                kind = "cross_side"
            counts[kind] += 1
            # ⛔ A total key over content, never arrival order (finding 8): the
            # PAIR is what was measured, so the row names its two lines in ref
            # order rather than in the order they were handed in.
            first, second = sorted(((str(a[2]), str(a[3]), int(a[4])),
                                    (str(b[2]), str(b[3]), int(b[4]))))
            detail.append({"a": first[0], "b": second[0], "kind": kind,
                           "a_side": first[1], "b_side": second[1],
                           "a_ring": first[2], "b_ring": second[2]})
    out = dict(counts)
    out["lines"] = len(lines)
    out["kinds_declared"] = list(CROSSING_KINDS)
    out["detail"] = sorted(detail, key=lambda d: (d["kind"], d["a"], d["b"]))
    return out


def side_span(placements, side: str, geometries, *, anchor_box=None) -> dict:
    """⭐ **P-A's target, in code** -- plan section 2.3's ten rows.

    The column's **courtyard** span along ``side``'s edge, its midpoint, and the
    offset of that midpoint from the anchor's own centre along the same edge.
    ⛔ The courtyard, not ``physical_bounds`` (standing finding 13, rule 8): the
    overhang is 0.175 mm/side at 0402 and 0.275-0.280 mm at 0603/0805, so a span
    measured on the wrong box is short by exactly that and *every test still
    passes at 0805*.

    ⚠ ``anchor_box`` is an addition to the plan's stated signature and it is
    required for three of the five keys: the plan's ``(placements, side,
    geometries)`` names nothing that knows where the anchor is or how wide it is,
    and ``centre`` / ``offset_mm`` / ``ratio`` are all *about the anchor*.
    Without it those three are ``None`` rather than guessed.
    """
    _axis, _sign, along = _AXES[side]
    boxes = [_courtyard_box(geometries[p.ref], p.ref, p.x_mm, p.y_mm,
                            p.rot_deg)
             for p in placements if p.ref in geometries]
    if not boxes:
        raise ConstructError(
            f"side_span({side!r}) was handed no placement with a geometry -- an "
            f"instrument that can observe nothing must raise (rule 3)")
    lo = min(box[along] for box in boxes)
    hi = max(box[along + 2] for box in boxes)
    span = round(hi - lo, 6)
    mid = round((lo + hi) / 2.0, 6)
    out = {"side": side, "n": len(boxes), "span_mm": span, "mid": mid,
           "lo": round(lo, 6), "hi": round(hi, 6),
           "centre": None, "offset_mm": None, "ratio": None,
           "anchor_edge_mm": None}
    if anchor_box is not None:
        centre = (anchor_box[along] + anchor_box[along + 2]) / 2.0
        edge = anchor_box[along + 2] - anchor_box[along]
        out["centre"] = round(centre, 6)
        out["offset_mm"] = round(mid - centre, 6)
        out["anchor_edge_mm"] = round(edge, 6)
        out["ratio"] = round(span / edge, 4) if edge > _TOL else None
    return out


def busy_anchor_extra_mm(neighbours, *, fab, anchor_geometry, side,
                         geometries=None, anchor_rot_deg: int = 0,
                         rotations=None) -> tuple:
    """⭐ **P-B.** How much further out a BUSY anchor holds its neighbours.

    The standoff is ``A's courtyard extent + the fanout allowance + the
    neighbour's own facing extent`` and it has **no term for load**. The human,
    looking at S4's renders: *"a high-neighbour-count anchor needs a larger
    keep-out than a small one."*

    ::

        side_load_ratio = (sum of the side's neighbours' along-edge courtyard
                           extents + (n - 1) * edge_gap) / A's along-edge extent
        busy_extra      = lane_mm * max(0, ceil(side_load_ratio) - 1)

    ⛔ **Every term from the FabSpec or a measured courtyard, never a table** --
    the size-table trap has fired four times and this is a fifth site. The term
    is **one via lane per whole multiple of over-subscription**, because what a
    crowded side actually lacks is escape lanes.

    ⚠ ``geometries`` / ``anchor_rot_deg`` / ``rotations`` are additions to the
    plan's stated signature, and the reason is measurable rather than stylistic:
    a :class:`Neighbour` carries **no geometry at all**, so the stated
    ``(neighbours, *, fab, anchor_geometry, side)`` cannot compute a single one
    of the extents its own formula sums. Recorded here rather than slipped in.
    ⚠ ``rotations`` defaults to **0** for every neighbour and the term's
    provenance says so: on an E/W side the loop places at 0/180, whose along-edge
    (y) extent is identical to rotation 0's, so the default is exact there and an
    over-estimate on N/S. The **measured** span is reported beside it by
    :func:`side_span`, so the two can be compared rather than trusted.

    ⛔ Returns ``(extra_mm, terms)`` -- every number carries its source.
    """
    from .power_escape import lane_from_fab

    _axis, _sign, along = _AXES[side]
    anchor_box = _courtyard_box(anchor_geometry, "@A", 0.0, 0.0,
                                int(anchor_rot_deg))
    anchor_edge = anchor_box[along + 2] - anchor_box[along]
    if anchor_edge <= _TOL:
        raise ConstructError(
            f"the anchor's courtyard has zero extent along {side!r} -- a load "
            f"ratio over it would be a division by nothing (rule 3)")
    lane = lane_from_fab(fab)
    if lane is None:
        raise ConstructError(
            "the FabSpec declares no via lane (it needs via_size_mm and "
            "min_clearance_mm), and P-B's term IS a via lane -- a keep-out "
            "computed without it would be a constant wearing a formula's "
            "clothes (rule 3)")
    lane = round(float(lane), 6)
    gap = edge_gap_mm(fab)
    extents: list = []
    for neighbour in neighbours:
        geometry = (geometries or {}).get(neighbour.ref)
        if geometry is None:
            continue
        box = _courtyard_box(geometry, neighbour.ref, 0.0, 0.0,
                             int((rotations or {}).get(neighbour.ref, 0)))
        extents.append((neighbour.ref,
                        round(box[along + 2] - box[along], 6)))
    count = len(extents)
    load = sum(value for _ref, value in extents) + max(0, count - 1) * gap
    ratio = load / anchor_edge
    lanes = max(0, int(math.ceil(ratio - _TOL)) - 1)
    extra = round(lane * lanes, 6)
    terms = (("side_load_mm", round(load, 6),
              f"{count} neighbour courtyard extent(s) along {side} + "
              f"{max(0, count - 1)} x edge gap, MEASURED at rotation 0"),
             ("anchor_edge_mm", round(anchor_edge, 6),
              "A's own courtyard extent along that edge, MEASURED"),
             ("side_load_ratio", round(ratio, 6), "their quotient"),
             ("lanes", lanes, "ceil(ratio) - 1 -- whole multiples of "
                              "over-subscription"),
             ("lane_mm", lane, "power_escape.lane_from_fab(FabSpec)"),
             ("busy_anchor_extra_mm", extra, "lanes x lane"))
    return extra, terms


def _anchor_direction(sub_box: tuple, anchor_x_mm: float,
                      anchor_y_mm: float) -> str:
    """⭐ **P-D.** Which side of a sub-anchor the ANCHOR lies on.

    ⛔ :func:`~skidl_layout.escape_map.pad_occupancy_side`'s own arithmetic,
    called rather than re-derived -- the arc has one *"which side is that on"*
    rule and this is not a second one. The box handed in is the sub-anchor's
    **physical** box, which is the frame that function documents.
    """
    from .escape_map import pad_occupancy_side

    return pad_occupancy_side(float(anchor_x_mm), float(anchor_y_mm), sub_box)


# =========================================================================== #
# S4 -- P3, the flattened template, ring 2, and P7's tighten pass
# =========================================================================== #
def side_order(partition, anchor: str, escape_map, *, anchor_pads,
               anchor_geometry, anchor_rot_deg: int = 0, nets_by_ref=None,
               flatten_templates: bool = True,
               distribute_stacked: bool = True) -> tuple[str, ...]:
    """⭐ **Overview P3.** Which side of the anchor is built first.

    *"Sum the priorities in each side list; process side lists in descending
    order of that sum."* ⛔ Ties break on the side letter -- a total key over
    content, never arrival order (standing finding 8).

    ⭐ The overview's other P3 [CALL] -- *"is a side finished before the next
    begins?"* -- is answered **yes** in :func:`construct_cell`: it keeps the
    *"avoid what you just placed"* reasoning local and makes a failure
    attributable to one side rather than to an interleaving.
    """
    sums = {}
    for side in SIDES:
        found = side_neighbours(partition, anchor, escape_map, side=side,
                                anchor_pads=anchor_pads,
                                anchor_geometry=anchor_geometry,
                                anchor_rot_deg=anchor_rot_deg,
                                nets_by_ref=nets_by_ref,
                                flatten_templates=flatten_templates,
                                distribute_stacked=distribute_stacked)
        sums[side] = sum(n.priority for n in found)
    return tuple(sorted(SIDES, key=lambda s: (-sums[s], s)))


def ring2_subanchor(ref: str, placed, nets_by_ref, *,
                    plane_fallback: bool = False, group_of=None) -> tuple | None:
    """⭐ **Ring 2's [CALL], answered by a total key.**

    The sub-anchor is *the already-placed part sharing the most **plane-free**
    nets* with ``ref``; ties by ``(-shared, ref)``. ⛔ Plane-free because a part
    whose only link to a candidate is ``GND`` is not *"beside"* it in any useful
    sense -- standing finding 5, now six independent arrivals.

    Returns ``(sub-anchor ref, shared plane-free nets)`` or ``None`` when
    nothing placed shares a plane-free net, which is a **named skip** and never
    a guess.

    ⭐⭐⭐ **S9 STAGE C -- ``plane_fallback``, and it is the placement half of the
    plane defect.** The paragraph above is right about *"beside"* and wrong about
    what to do next: **42 of 148 corpus parts have no plane-free net at all**
    (the bulk and bypass capacitors, the power connectors, the ground-return
    resistor), so on today's default they are skipped by name, land wherever the
    board sizing leaves room, and carry no copper. ⛔ The exclusion was correct
    **for a pipeline that pours**; the constructive path does not pour.

    Under ``plane_fallback=True``, a part with no plane-free link falls back to
    a plane-net sub-anchor by the total key :func:`_plane_subanchor` states --
    most shared plane nets, **its own partition group preferred over a
    stranger's**, then the smallest ref -- and the third element of the returned
    tuple is ``"plane_fallback"`` rather than ``"plane_free"``. ⛔⛔ **A guess
    that is not labelled is the defect this whole arc keeps finding**, so the
    label is not optional and every consumer records it.

    ⛔ Default **OFF**: the two-tuple return is preserved exactly when nothing
    falls back, so the OFF arm is byte-identical to S8's recorded constructions.
    """
    from .ratnest import is_plane_net

    mine = set(nets_by_ref.get(ref, ()))
    best = None
    for candidate in sorted(placed):
        if candidate == ref:
            continue
        shared = sorted(net for net in (mine & set(nets_by_ref.get(candidate,
                                                                  ())))
                        if not is_plane_net(net))
        if not shared:
            continue
        key = (-len(shared), candidate)
        if best is None or key < best[0]:
            best = (key, candidate, shared)
    if best is not None:
        return (best[1], best[2], "plane_free") if plane_fallback else (
            best[1], best[2])
    if not plane_fallback:
        return None
    row = _plane_subanchor(ref, placed, nets_by_ref, group_of=group_of)
    if row is None:
        return None
    return (row["ref"], list(row["shared_plane_nets"]), "plane_fallback")


def _allowance_of(placement) -> float:
    """The fanout allowance **this** placement actually paid, from its own
    terms. ⛔ Read off ``standoff_terms`` rather than recomputed, because a
    pushed placement paid ``base * (1 + push)`` and a second derivation of the
    same number is how the two drift apart."""
    for name, value, _source in placement.standoff_terms:
        if name == "fanout_allowance_mm":
            return float(value)
    return 0.0


class _CellState:
    """Where every placed part currently is, and what stamps it.

    ⭐ One structure, because the tighten pass moves parts **after** they were
    placed and everything downstream -- the boxes, the pair questions, the
    render, the final validation -- has to read the *current* position rather
    than the one the placement was born with. Two structures is how the picture
    and the data drift apart (standing finding 1).
    """

    __slots__ = ("entries", "boxes_phys", "boxes_court")

    def __init__(self):
        self.entries: dict = {}
        self.boxes_phys: dict = {}
        self.boxes_court: dict = {}

    def put(self, ref, *, x_mm, y_mm, rot_deg, geometry, footprint, token,
            side="", subanchor="", standoff_mm=0.0, allowance_mm=0.0,
            ring_extra_mm=0.0):
        self.entries[ref] = {"x_mm": float(x_mm), "y_mm": float(y_mm),
                             "rot_deg": int(rot_deg), "geometry": geometry,
                             "footprint": footprint, "token": token,
                             "side": side, "subanchor": subanchor,
                             "standoff_mm": float(standoff_mm),
                             "allowance_mm": float(allowance_mm),
                             #: ⭐ P-B's fifth standoff term, kept so
                             #: :func:`shrink_ring` can rebuild the standoff
                             #: from ``anchor_extent + R + facing + extra``
                             #: rather than re-deriving a number the placement
                             #: already paid (a second derivation is how the
                             #: two drift apart).
                             "ring_extra_mm": float(ring_extra_mm)}
        self.refresh(ref)

    def refresh(self, ref):
        entry = self.entries[ref]
        self.boxes_phys[ref] = _physical_box(
            entry["geometry"], ref, entry["x_mm"], entry["y_mm"],
            entry["rot_deg"])
        self.boxes_court[ref] = _courtyard_box(
            entry["geometry"], ref, entry["x_mm"], entry["y_mm"],
            entry["rot_deg"])

    def pads(self, ref) -> list:
        entry = self.entries[ref]
        return moved_pads(list(entry["footprint"].pads), x_mm=entry["x_mm"],
                          y_mm=entry["y_mm"], rot_deg=entry["rot_deg"],
                          base_rot_deg=entry["footprint"].rotation)

    def placed_parts(self) -> list:
        return [_placed(ref, e["x_mm"], e["y_mm"], e["rot_deg"],
                        e["geometry"].footprint)
                for ref, e in sorted(self.entries.items())]


def _pair_target(state, entry) -> object | None:
    for pad in state.pads(entry["target_ref"]):
        if str(pad.pad_number) == entry["target_pad"]:
            return pad
    return None


def _reroute_all(session, pairs, state) -> tuple:
    """Re-ask every routed pair of the whole cell. ⛔ Deliberately *every* one.

    ⚠ A moved part can invade a **different** pair's channel, so pruning this to
    *"the affected pairs"* is an optimisation that needs a correctness argument
    S4 does not have. The residue is cleared before each question inside
    :func:`_route_pair` (standing finding 22).
    """
    lengths: dict = {}
    for entry in pairs:
        target = _pair_target(state, entry)
        if target is None:
            return False, entry, lengths
        answer = _route_pair(session, state.pads(entry["ref"]),
                             entry["neighbour"], target)
        if answer is None or not answer.routed:
            return False, entry, lengths
        lengths[entry["ref"]] = answer
    return True, None, lengths


#: ⛔⛔ **The two readings of P7's floor, and the difference is a DEFECT in the
#: plan that only contact could show** (measured 2026-08-04).
#:
#: The plan says *"floor: courtyard legality only -- the fanout allowance MAY be
#: consumed"*, which reads as *"step until a box stops you"*. That is
#: ``"courtyard"`` below, and it is **wrong whenever a neighbour is displaced
#: along the edge**: with nothing opposite it, the neighbour marches the full
#: ``ceil(standoff / step)`` budget and walks **past the anchor's own centre
#: line**. Measured on ``ltc1871_sepic``: ``CIN`` accepted 40 of 40 steps and
#: ended at x = 24.955 against an anchor at x = 25.0, having crossed it, while
#: the courtyard check saw nothing because it sits 10 mm down the edge.
#:
#: ``"allowance"`` is the implemented policy and it is the plan's own sentence
#: taken literally in the other direction: what the tighten pays back is *the
#: standoff's generosity*, which is the **fanout allowance** and nothing else.
#: The floor is therefore the perpendicular offset at which the allowance is
#: exhausted -- ``standoff - allowance``, i.e. exactly where the two courtyards
#: would touch if they were opposite each other. ⭐ It also makes the answer to
#: overview open question 5 a number that means something: *how much of the
#: allowance survives contact.*
TIGHTEN_FLOORS: tuple[str, ...] = ("allowance", "courtyard")


def _tighten_cell(session, *, order, by_side, ring2_order, state, pairs,
                  step_mm, clearance_mm, route,
                  floor_policy: str = "allowance") -> list:
    """⭐⭐⭐ **Overview P7 -- and the first grading of the standoff.**

    Deterministic, bounded and **transactional**: one neighbour at a time, in
    P3 side order and P2 order within a side then ring 2 in placement order,
    stepping **toward its (sub-)anchor along the perpendicular axis only**.

    ⛔ **Never along the edge.** P2's order and the monotone property are what
    the whole procedure exists to keep, and a step along the edge would trade
    the one guarantee the construction has for a millimetre.

    ⛔ A step that breaks either box's legality, or that stops **any** pair of
    the cell routing, is **reverted** -- so a tighten failure can never corrupt
    the construction that preceded it, which is why the overview made it a
    separate pass.

    ⚠ The floor is **courtyard legality against the (sub-)anchor**, so the
    fanout allowance *may* be fully consumed: measuring how much of it survives
    contact **is** overview open question 5.
    """
    steps: list[TightenStep] = []
    sequence = [(ref, "side") for side in order
                for ref in [n.ref for n in by_side[side]]
                if ref in state.entries]
    sequence += [(ref, "ring2") for ref in ring2_order if ref in state.entries]
    seen: set = set()
    ordered = []
    for ref, kind in sequence:
        if ref not in seen:
            seen.add(ref)
            ordered.append((ref, kind))

    before_lengths = {entry["ref"]: entry.get("length_mm") for entry in pairs}
    for ref, _kind in ordered:
        entry = state.entries[ref]
        side = entry["side"]
        subanchor = entry["subanchor"]
        if not side or subanchor not in state.entries:
            continue
        axis, sign, _along = _AXES[side]
        cap = int(math.ceil(max(0.0, entry["standoff_mm"]) / step_mm))
        sub = state.entries[subanchor]
        base_perp = sub["x_mm"] if axis == 0 else sub["y_mm"]
        floor_offset = (entry["standoff_mm"] - entry["allowance_mm"]
                        if floor_policy == "allowance" else float("-inf"))
        start = (entry["x_mm"], entry["y_mm"])
        accepted, tried, stopped, why = 0, 0, "cap", ""
        while accepted < cap:
            tried += 1
            x_mm = entry["x_mm"] - (sign * step_mm if axis == 0 else 0.0)
            y_mm = entry["y_mm"] - (sign * step_mm if axis == 1 else 0.0)
            offset = sign * ((x_mm if axis == 0 else y_mm) - base_perp)
            if offset < floor_offset - _TOL:
                stopped = "floor"
                why = (f"the fanout allowance of {entry['allowance_mm']} mm is "
                       f"spent: the perpendicular offset would fall to "
                       f"{round(offset, 4)} mm against a floor of "
                       f"{round(floor_offset, 4)} mm "
                       f"(the two courtyards' contact distance)")
                break
            phys = _physical_box(entry["geometry"], ref, x_mm, y_mm,
                                 entry["rot_deg"])
            court = _courtyard_box(entry["geometry"], ref, x_mm, y_mm,
                                   entry["rot_deg"])
            clash = sorted(
                {other for other, box in state.boxes_phys.items()
                 if other != ref and _pair_gap(box, phys) < clearance_mm - _TOL}
                | {other for other, box in state.boxes_court.items()
                   if other != ref and _pair_gap(box, court) < -_TOL})
            if clash:
                stopped = "collision"
                why = (f"the next {step_mm} mm step would put {ref} into "
                       f"{clash}")
                break
            previous = dict(entry)
            token = entry["token"]
            if token is not None:
                session.remove_part(token)
            entry["x_mm"], entry["y_mm"] = x_mm, y_mm
            state.refresh(ref)
            entry["token"] = (session.add_part(f"@N:{ref}", state.pads(ref))
                              if token is not None else None)
            ok = True
            if route:
                ok, _failed, _lengths = _reroute_all(session, pairs, state)
            if not ok:
                if entry["token"] is not None:
                    session.remove_part(entry["token"])
                entry["x_mm"], entry["y_mm"] = previous["x_mm"], \
                    previous["y_mm"]
                state.refresh(ref)
                entry["token"] = (session.add_part(f"@N:{ref}",
                                                   state.pads(ref))
                                  if token is not None else None)
                stopped = "unroutable"
                why = (f"{_failed['ref'] if _failed else 'a'} pair of the cell "
                       f"stopped routing after the step; reverted to "
                       f"{previous['x_mm']}, {previous['y_mm']}")
                break
            accepted += 1
        else:
            # ⚠ Under the ``allowance`` floor this is a BACKSTOP that provably
            # cannot bind: the floor spends the allowance in
            # ``floor(allowance / step)`` steps and the cap is
            # ``ceil((floor_offset + allowance) / step)``, which is never
            # smaller. It is kept, declared and **reported as unreachable**
            # rather than quietly dropped -- and ``test_construct.py`` proves
            # the inequality rather than asserting it.
            why = (f"the per-neighbour budget of {cap} step(s) "
                   f"(ceil(standoff {round(entry['standoff_mm'], 4)} / step "
                   f"{step_mm})) ran out")
        steps.append(TightenStep(
            ref=ref, moved_from_mm=(round(start[0], 6), round(start[1], 6)),
            moved_to_mm=(round(entry["x_mm"], 6), round(entry["y_mm"], 6)),
            recovered_mm=round(accepted * step_mm, 6),
            steps_accepted=accepted, steps_tried=tried, stopped_by=stopped,
            side=side, subanchor=subanchor, step_mm=step_mm,
            route_length_before_mm=before_lengths.get(ref), reason=why))
    return steps


def _plan_overflow(by_side, ratios, *, escape_map, anchor_rot_deg, positions,
                   admission: str) -> tuple:
    """⭐⭐ **P-C's decision, in strict order, each step deterministic, bounded
    and recorded** -- plan section 5.3 steps 1-3. Step 4 (*ask the router*) can
    only happen once a position exists, so it lives in :func:`construct_cell`.

    ⛔ **This is the first rule in the whole procedure that lets a neighbour
    leave the side its anchor pad chose**, so P2's non-crossing property is at
    risk by construction and :func:`pair_crossings` is a **gate** rather than a
    report (plan bail-out 3).

    Returns ``(moves, offers)`` -- an :class:`OverflowMove` for **every**
    neighbour of **every** over-subscribed side, offered or not (rule 3: a
    policy that reports only its successes is the observes-nothing defect), and
    ``{ref: destination side}`` for the ones actually offered.
    """
    from .escape_map import rotate_escape

    #: Which sides the **footprint** escapes on at all, in world terms -- plan
    #: section 2.4's own question, and :data:`OVERFLOW_ADMISSION`'s default.
    footprint_sides = {rotate_escape(local, int(anchor_rot_deg))
                       for pad in escape_map.pads
                       for local in escape_map.escapable_sides(pad)}
    moves: list = []
    offers: dict = {}
    for side in SIDES:
        ratio = float(ratios.get(side, 0.0))
        if ratio <= 1.0 + _TOL:
            continue
        members = list(by_side[side])
        if not members:
            continue
        # ⛔ *"the ones furthest along the edge from the anchor's centre -- the
        # tail of the P2 order, taken from the end, at most ceil(n/3)"*.
        wanted = int(math.ceil(len(members) / 3.0))
        offered = {n.ref for n in members[len(members) - wanted:]}
        # The destination: the LEAST loaded other side, ties by side letter.
        elsewhere = sorted((s for s in SIDES if s != side),
                           key=lambda s: (round(float(ratios.get(s, 0.0)), 6),
                                          s))
        destination = elsewhere[0] if elsewhere else None
        for member in members:
            common = {"ref": member.ref, "from_side": side, "to_side": None,
                      "anchor_pad": member.anchor_pad, "net": member.net,
                      "admission": admission,
                      "from_side_ratio": round(ratio, 6),
                      "to_side_ratio": (None if destination is None else
                                        round(float(ratios.get(destination,
                                                               0.0)), 6))}
            if member.ref not in offered:
                moves.append(OverflowMove(
                    reason="side_oversubscribed", **common,
                    why=f"{side} is over-subscribed (load ratio "
                        f"{round(ratio, 4)}) but {member.ref} is not in the "
                        f"offered tail of at most ceil({len(members)}/3) = "
                        f"{wanted}"))
                continue
            if destination is None or \
                    float(ratios.get(destination, 0.0)) >= ratio - _TOL:
                moves.append(OverflowMove(
                    reason="no_free_side", **common,
                    why=f"no other side is less loaded than {side}"))
                continue
            local = _unrotate(destination, int(anchor_rot_deg))
            access = escape_map.access(member.anchor_pad, local)
            admitted = (destination in footprint_sides
                        if admission == "footprint_side" else
                        local in escape_map.escapable_sides(member.anchor_pad))
            common["to_side_ratio"] = round(
                float(ratios.get(destination, 0.0)), 6)
            if not admitted:
                moves.append(OverflowMove(
                    reason="no_escape_on_free_side", **common,
                    destination_access=access,
                    why=(f"under {admission!r} the destination {destination} is "
                         f"not escapable: the map calls pad "
                         f"{member.anchor_pad} {access} there and the "
                         f"footprint's escapable sides are "
                         f"{sorted(footprint_sides)}")))
                continue
            offers[member.ref] = destination
            moves.append(OverflowMove(
                reason="", **dict(common, to_side=destination),
                destination_access=access,
                why=f"offered {destination} (load ratio "
                    f"{round(float(ratios.get(destination, 0.0)), 4)} against "
                    f"{side}'s {round(ratio, 4)}); the router decides"))
    return moves, offers


def _ring2_side_excluding(emap, pad, rot_deg: int, forbidden: str) -> tuple:
    """⭐ **P-D.** The best escapable side of ``pad`` that is **not** the side
    the anchor lies on.

    ⛔ *"then take the best remaining escapable side by the existing rule"* --
    and the existing rule is :func:`~skidl_layout.escape_map.favored_side`'s,
    which is ``FAVORED`` before ``ACCESSIBLE``, then the corridor distance, then
    the side letter. ⚠ This is therefore the **second** place in the arc that
    can reach an ``ACCESSIBLE`` side (P-C is the first), so S1's measured
    optimism tail -- 43 of 127 ``BLOCKED`` entries the router routed anyway,
    **zero** over-reports -- can bite here too.

    Returns ``(side or None, access, why)``. ``None`` means every escapable side
    points at the anchor, and the caller **keeps the existing answer** and
    records it rather than failing.
    """
    from .escape_map import rotate_escape

    rank = {"FAVORED": 0, "ACCESSIBLE": 1}
    entries = [e for e in emap.entries_for(pad)
               if e.layer == 0 and e.access != "BLOCKED"]
    for entry in sorted(entries, key=lambda e: (rank.get(e.access, 2),
                                                round(float(e.distance_mm), 6),
                                                e.side)):
        world = rotate_escape(entry.side, int(rot_deg))
        if world != forbidden:
            return (world, entry.access,
                    f"the anchor lies {forbidden} of the sub-anchor, so that "
                    f"side is excluded; {world} is the best remaining "
                    f"({entry.access}, corridor {round(float(entry.distance_mm), 4)} mm)")
    return (None, "",
            f"every escapable side of pad {pad} points at the anchor "
            f"({forbidden}); the existing answer is kept and recorded")


def _pair_lines(state, pairs, ring2_refs) -> list:
    """``((x1, y1), (x2, y2), ref, side, ring)`` for every routed pair.

    ⛔ **The endpoints come from the CURRENT state**, never from the placement a
    neighbour was born with (trap 18): the tighten and P-A both move parts, and
    S4's five real crossings are precisely about position.
    ⚠ **The line is the PAIR, not the copper.**
    """
    lines: list = []
    for entry in pairs:
        ref = entry["ref"]
        if ref not in state.entries:
            continue
        target = _pair_target(state, entry)
        neighbour = entry["neighbour"]
        mine = [pad for pad in state.pads(ref)
                if str(getattr(pad, "net_name", "") or "") == neighbour.net]
        if target is None or not mine:
            continue
        lines.append(((round(mine[0].global_x, 6), round(mine[0].global_y, 6)),
                      (round(target.global_x, 6), round(target.global_y, 6)),
                      ref, neighbour.side, 2 if ref in ring2_refs else 1))
    return sorted(lines, key=lambda line: str(line[2]))


def _illegal_pairs(state, clearance_mm: float) -> list:
    """Both boxes, over everything currently placed (overview 7.1, finding 13)."""
    refs = sorted(state.boxes_phys)
    out = []
    for index, a in enumerate(refs):
        for b in refs[index + 1:]:
            if _pair_gap(state.boxes_phys[a], state.boxes_phys[b]) \
                    < clearance_mm - _TOL:
                out.append([a, b, "physical"])
            elif _pair_gap(state.boxes_court[a], state.boxes_court[b]) < -_TOL:
                out.append([a, b, "courtyard"])
    return out


def _same_side_crossings(state, pairs) -> int:
    """The whole cell's same-side pair-line count, from the CURRENT state."""
    return pair_crossings(_pair_lines(state, pairs, set()))["same_side"]


def _centre_sides(session, *, state, order, laid, pairs, anchor_box,
                  clearance_mm: float, route: bool) -> list:
    """⭐⭐⭐ **P-A.** Balance each side's column about the anchor.

    Measured on S4's recorded artifact: **every** mid-offset on a crowded side is
    POSITIVE (+3.62 to +27.83 mm) and every one-part side is within +/-1.25 mm.
    The cursor does not merely spread the column -- **it spreads it in one
    direction only** -- and the defect is monotone in the neighbour count. That
    is P-A's whole content.

    ⛔ **A rigid translation, not a re-seed** (trap 17). Re-running
    :func:`_position_for` with a shifted ``desired`` re-enters the cursor logic
    and can **reorder**; P2's order is the property the whole procedure exists to
    keep. This takes the laid-out positions and adds **one constant** to the
    along-axis coordinate of every placement on the side.

    ⚠ **The cursor's box-advance is CORRECT and is not changed.** A part cannot
    advance by its connecting pad alone -- the boxes would overlap. Open question
    14 has two halves and only the *direction* half is wrong; *"advance by the
    connecting pad"* is **not** an answer to the other half.

    ⛔ **Transactional**, exactly like the tighten and for the same reason: a
    translation that breaks either box's legality, or that stops **any** pair of
    the cell routing, is reverted whole, so P-A can never corrupt the
    construction that preceded it.
    """
    out: list = []
    for side in order:
        refs = [member.ref for member in laid[side]
                if member.ref in state.entries]
        _axis, _sign, along = _AXES[side]
        centre = round((anchor_box[along] + anchor_box[along + 2]) / 2.0, 6)
        if not refs:
            out.append({"side": side, "n": 0, "applied": False,
                        "delta_mm": 0.0, "offset_before_mm": None,
                        "offset_after_mm": None, "centre_mm": centre,
                        "why": "no placement on this side"})
            continue
        lo = min(state.boxes_court[ref][along] for ref in refs)
        hi = max(state.boxes_court[ref][along + 2] for ref in refs)
        mid = (lo + hi) / 2.0
        delta = round(centre - mid, 6)
        row = {"side": side, "n": len(refs), "refs": list(refs),
               "span_mm": round(hi - lo, 6), "centre_mm": centre,
               "offset_before_mm": round(mid - centre, 6),
               "delta_mm": delta}
        if abs(delta) <= _TOL:
            row.update({"applied": True, "offset_after_mm": 0.0,
                        "why": "already centred"})
            out.append(row)
            continue
        previous = {ref: (state.entries[ref]["x_mm"],
                          state.entries[ref]["y_mm"],
                          state.entries[ref]["token"]) for ref in refs}

        def _move(to_delta):
            for ref in refs:
                entry = state.entries[ref]
                if entry["token"] is not None:
                    session.remove_part(entry["token"])
                    entry["token"] = None
            for ref in refs:
                entry = state.entries[ref]
                base_x, base_y, token = previous[ref]
                entry["x_mm"] = base_x + (to_delta if along == 0 else 0.0)
                entry["y_mm"] = base_y + (0.0 if along == 0 else to_delta)
                state.refresh(ref)
                if token is not None:
                    entry["token"] = session.add_part(f"@N:{ref}",
                                                      state.pads(ref))

        crossings_before = _same_side_crossings(state, pairs)
        _move(delta)
        clash = _illegal_pairs(state, clearance_mm)
        # ⛔⛔ **P-A WILL NOT BUY CENTRING WITH A CROSSING** (bail-out 3).
        # ⭐ This guard exists because it FIRED: a rigid translation cannot
        # reorder a side, but it can move a side's column from one flank of a
        # pad row to the other, and an off-edge fan that was nested on one flank
        # is crossed on the other. Measured on ``ltc1871_sepic``'s combined arm,
        # where P-C had moved five parts to N and P-A then translated them
        # across their own pad columns: 0 same-side crossings became 1.
        crossings_after = _same_side_crossings(state, pairs)
        routed_ok, failed = True, None
        lengths: dict = {}
        if not clash and crossings_after <= crossings_before and route and pairs:
            routed_ok, failed, lengths = _reroute_all(session, pairs, state)
        row["same_side_before"] = crossings_before
        row["same_side_after"] = crossings_after
        if clash or crossings_after > crossings_before or not routed_ok:
            _move(0.0)
            row.update({
                "applied": False, "offset_after_mm": row["offset_before_mm"],
                "why": (f"reverted whole -- the translation collides: "
                        f"{clash[:3]}" if clash else
                        f"reverted whole -- it would raise same-side pair "
                        f"crossings {crossings_before} -> {crossings_after}, "
                        f"and P-A does not buy centring with a crossing "
                        f"(bail-out 3)"
                        if crossings_after > crossings_before else
                        f"reverted whole -- {failed['ref'] if failed else '?'}"
                        f"'s pair stopped routing after the translation")})
            out.append(row)
            continue
        for entry in pairs:
            if entry["ref"] in lengths:
                entry["length_mm"] = lengths[entry["ref"]].length_mm
        lo2 = min(state.boxes_court[ref][along] for ref in refs)
        hi2 = max(state.boxes_court[ref][along + 2] for ref in refs)
        row.update({"applied": True,
                    "offset_after_mm": round((lo2 + hi2) / 2.0 - centre, 6),
                    "route_lengths_after_mm": {
                        ref: lengths[ref].length_mm for ref in sorted(lengths)
                        if ref in refs},
                    "why": f"translated rigidly by {delta} mm along the "
                           f"{'x' if along == 0 else 'y'} axis; P2's order is "
                           f"untouched and every pair still routes"})
        out.append(row)
    return out


def construct_cell(partition, anchor: str, *, session, geometries, fab,
                   escape_map, escape_maps=None, board: str = "",
                   anchor_x_mm: float = 0.0, anchor_y_mm: float = 0.0,
                   anchor_rot_deg: int = 0, nets_by_ref=None,
                   flatten_templates: bool = True,
                   distribute_stacked: bool = True,
                   align_connecting_pad: bool = True,
                   tighten: bool = True, tighten_floor: str = "allowance",
                   route: bool = True,
                   centre_side_lists: bool = False,
                   busy_anchor_keepout: bool = False,
                   overflow_to_free_side: bool = False,
                   ring2_avoids_anchor_side: bool = False,
                   overflow_admission: str = "footprint_side",
                   overflow_order: str = "edge_depth",
                   side_assignment: str = "favored",
                   ring: bool = False,
                   arc_edge_gap: str = "edge_gap",
                   corner_owner: str = "none",
                   spawn_factor: float = 1.0,
                   tighten_mode: str = "part",
                   shrink_step_mm: float | None = None,
                   r_max_factor: float = 4.0,
                   template_units: bool = False,
                   template_session=None,
                   plane_fallback: bool = False,
                   commit_copper: bool = False) -> CellResult:
    """⭐⭐⭐ **The whole L1 cell** -- all four sides, ring 2, and the tighten.

    ⭐⭐⭐ **S5 adds four ARRANGEMENT policies, each behind a flag whose OFF arm
    reproduces S4's recorded artifact byte for byte** (gate ``AR0``, and that
    control is the reason the flags exist). Every one of them changes *where a
    neighbour goes*, never how it is placed once chosen -- ``_place_one``'s
    ladder, ``_choose_rotation`` and ``_position_for``'s standoff formula are
    untouched apart from P-B's fifth term.

    * ``centre_side_lists`` (**P-A**) -- the along-the-edge cursor advances
      **only outward**, so a crowded side is a one-directional queue: measured on
      S4's artifact, every mid-offset on a crowded side is **positive**
      (+3.62 to +27.83 mm) and every one-part side is within +/-1.25 mm. ON, each
      side's placements are **translated rigidly** so the column's midpoint sits
      on the anchor's courtyard centre along that edge. ⛔ A translation, not a
      re-seed (trap 17): re-entering the cursor logic with a shifted ``desired``
      can **reorder**, and P2's order is the property the procedure exists to
      keep. ⛔ Transactional: a translation that breaks either box's legality or
      stops any pair routing is **reverted whole**.
    * ``busy_anchor_keepout`` (**P-B**) -- the standoff has no term for **load**.
      ON, :func:`busy_anchor_extra_mm` adds one via lane per whole multiple of
      over-subscription, as a **fifth standoff term** so the tighten's
      ``standoff - allowance`` floor cannot hand it back.
    * ``overflow_to_free_side`` (**P-C**) -- ⛔ **the first rule in the whole
      procedure that lets a neighbour leave the side its anchor pad chose.** The
      tail of an over-subscribed side is offered the least-loaded side; the
      router answers (overview 7.3: a **disqualifier**, never a score); a refusal
      puts the part back exactly where it was. See :data:`OVERFLOW_ADMISSION`
      for the two readings of the admission test and why the plan's literal one
      is measured rather than used.
    * ``ring2_avoids_anchor_side`` (**P-D**) -- ring 2 picks its side from the
      sub-anchor's map with **no memory of where the anchor is**, and *both* of
      S4's two failures are that one defect. ON, the side the anchor lies on is
      excluded and the best remaining escapable side is taken.


    ``escape_maps`` supplies the sub-anchors' escape maps: a
    ``{resolved footprint: EscapeMap}`` mapping, or a callable taking a resolved
    footprint name. ⭐ S1's derivation works on **any** footprint, so a chip
    passive gets a map too -- three sides open, the opposite one blocked -- and
    that is what makes ring 2 the *same* loop one level down rather than a
    second one.

    ⛔⛔⛔ **The anchor stays where the parsed board has it** (standing finding
    21). ``add_part`` is position-independent and ``route_pair`` is **not**:
    KRT's endpoint rescues look the pad up in ``pcb_data.pads_by_net`` by its
    **file** coordinates, and a fine-pitch pad *needs* the rescue. Neighbours
    move freely; the anchor does not. The caller is responsible for writing the
    board with the anchor at ``(anchor_x_mm, anchor_y_mm)`` and a gate asserts
    it.
    """
    if anchor not in geometries:
        raise ConstructError(f"no footprint geometry for the anchor {anchor!r}")
    anchor_geom = geometries[anchor]
    anchor_fp = session.pcb.footprints.get(anchor)
    if anchor_fp is None:
        raise ConstructError(
            f"{anchor!r} is not on the session's board {session.pcb_path!r}")
    if escape_map.footprint != anchor_geom.footprint:
        raise ConstructError(
            f"the escape map describes {escape_map.footprint!r} but the "
            f"anchor's geometry is {anchor_geom.footprint!r} (bail-out 6)")
    group = partition.group_of(anchor)
    if group is None:
        raise ConstructError(
            f"{anchor!r} is in no group of this partition -- a cell built over "
            f"nothing is indistinguishable from one that found everything")
    if overflow_admission not in OVERFLOW_ADMISSION:
        raise ConstructError(
            f"overflow_admission={overflow_admission!r} is not one of "
            f"{OVERFLOW_ADMISSION}")
    if overflow_order not in OVERFLOW_ORDER:
        raise ConstructError(
            f"overflow_order={overflow_order!r} is not one of "
            f"{OVERFLOW_ORDER}")
    if side_assignment not in SIDE_ASSIGNMENT:
        raise ConstructError(
            f"side_assignment={side_assignment!r} is not one of "
            f"{SIDE_ASSIGNMENT}")
    if arc_edge_gap not in ARC_EDGE_GAP:
        raise ConstructError(
            f"arc_edge_gap={arc_edge_gap!r} is not one of {ARC_EDGE_GAP}")
    if corner_owner not in CORNER_OWNER:
        raise ConstructError(
            f"corner_owner={corner_owner!r} is not one of {CORNER_OWNER}")
    if tighten_mode not in TIGHTEN_MODE:
        raise ConstructError(
            f"tighten_mode={tighten_mode!r} is not one of {TIGHTEN_MODE}")

    from .escape_map import favored_side, rotate_escape
    from .validator import validate

    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    #: ⛔ S9 Stage C: the fallback key prefers a part's **own** partition group
    #: over a stranger's, so it needs the group map. Built once, from the
    #: partition, and never from arrival order (finding 17).
    group_of_ref = {ref: g.name for g in partition.groups for ref in g.refs}
    anchor_pads = list(anchor_fp.pads)
    base = standoff_base_mm(fab)
    gap = edge_gap_mm(fab)
    clearance = float(fab.clearance_mm)
    # ⛔ A COPY. P-G inserts a composite unit's geometry under the group's name
    # so ``side_span``, ``validate`` and the failure log can all see it; mutating
    # the caller's dict would leak an L0 artifact into the next arm.
    geometries = dict(geometries)

    maps_cache: dict = {}

    def _map_for(footprint_name: str):
        if footprint_name == anchor_geom.footprint:
            return escape_map
        if footprint_name in maps_cache:
            return maps_cache[footprint_name]
        found = None
        if isinstance(escape_maps, dict):
            found = escape_maps.get(footprint_name)
        elif callable(escape_maps):
            found = escape_maps(footprint_name)
        if found is None:
            raise ConstructError(
                f"no EscapeMap for {footprint_name!r}. Ring 2 needs the "
                f"sub-anchor's own map and this loop will not guess one "
                f"(rule 3) -- pass escape_maps={{footprint: EscapeMap}}.")
        maps_cache[footprint_name] = found
        return found

    # -- P1/P2/P3 ---------------------------------------------------------- #
    by_side: dict = {}
    by_side_control: dict = {}
    for side in SIDES:
        by_side[side] = side_neighbours(
            partition, anchor, escape_map, side=side, anchor_pads=anchor_pads,
            anchor_geometry=anchor_geom, anchor_rot_deg=anchor_rot_deg,
            nets_by_ref=nets_by_ref, flatten_templates=flatten_templates,
            distribute_stacked=distribute_stacked)
        by_side_control[side] = side_neighbours(
            partition, anchor, escape_map, side=side, anchor_pads=anchor_pads,
            anchor_geometry=anchor_geom, anchor_rot_deg=anchor_rot_deg,
            nets_by_ref=nets_by_ref, flatten_templates=flatten_templates,
            distribute_stacked=False)
    # -- P-E: the escape demand, BEFORE P3 orders the sides ----------------- #
    # ⛔ The redistribution happens before the priority sums are taken, because
    # under ``escapable`` a side's load IS what P3 is ordering by. The OFF arm
    # calls the same function and it returns ``side_neighbours``' answer
    # unchanged, which is gate ``AC1``'s control.
    demand = side_demand(
        partition, anchor, escape_map, anchor_pads=anchor_pads,
        anchor_geometry=anchor_geom, anchor_rot_deg=anchor_rot_deg,
        nets_by_ref=nets_by_ref, geometries=geometries, fab=fab,
        flatten_templates=flatten_templates,
        distribute_stacked=distribute_stacked, assignment=side_assignment,
        edge_gap=arc_edge_gap, overflow_order=overflow_order)
    if side_assignment != "favored":
        by_side = dict(demand["by_side"])

    # -- P-G: L0, BEFORE the ring is sized and before P3 sums the load ------ #
    # ⛔ **The order matters and it is stated:** a unit's along-edge courtyard
    # extent is not its representative member's, so a ring sized against the
    # flattened member would be sized against the wrong object. The units are
    # therefore built first and the template neighbour is **replaced** by one
    # whose ``ref`` is the group's name -- the ``fp_geometries`` masquerade a
    # third time (S5B used it a second).
    template_cells: dict = {}
    unit_members: dict = {}
    l0_rows: list = []
    l0_failures: list = []
    if template_units:
        if template_session is None:
            raise ConstructError(
                "template_units=True needs template_session=(group name) -> "
                "RouteSession, and a FRESH session per L0 cell -- a shared one "
                "measures the session's history (standing finding 22, five "
                "instances, the last three of which move the PLACEMENT)")
        by_refs = {tuple(sorted(g.refs)): g for g in partition.groups}
        rebuilt_l0: dict = {side: [] for side in SIDES}
        for side in SIDES:
            for neighbour in by_side[side]:
                group_l0 = (by_refs.get(tuple(sorted(neighbour.members)))
                            if neighbour.klass == "template"
                            and neighbour.members else None)
                if group_l0 is None:
                    rebuilt_l0[side].append(neighbour)
                    continue
                own = template_session(group_l0.name)
                try:
                    l0 = construct_template(
                        partition, group_l0.name, session=own,
                        geometries=geometries, fab=fab,
                        escape_maps=escape_maps, board=board,
                        nets_by_ref=nets_by_ref, route=route, tighten=tighten)
                except ConstructError as error:
                    # ⛔ A NAMED failure, and the neighbour stays flattened.
                    # ⚠ *"20 of 20 families become units **or named
                    # failures**"* is gate ``AC3``'s wording, and a crash would
                    # make the whole arm unattributable rather than one family.
                    l0_rows.append({
                        "unit": group_l0.name, "side": side,
                        "kind": group_l0.kind, "family": group_l0.family,
                        "topology": group_l0.topology,
                        "members": sorted(group_l0.refs), "anchor": None,
                        "anchor_why": "", "placed": [],
                        "unplaced": sorted(group_l0.refs), "legal": None,
                        "routed_fraction": None, "ports": [],
                        "port_sides": [], "failed": True,
                        "why": f"the L0 loop refused this family: {error}"})
                    rebuilt_l0[side].append(neighbour)
                    continue
                inside = set(group_l0.refs)
                outside = {net for ref, nets in nets_by_ref.items()
                           if ref not in inside for net in nets}
                l0_fps = {ref: session.pcb.footprints[ref]
                          for ref in sorted(group_l0.refs)
                          if ref in session.pcb.footprints}
                unit = unit_from_cell(
                    l0, geometries, escape_maps, name=group_l0.name,
                    kind=group_l0.kind, footprints=l0_fps,
                    outside_nets=outside,
                    anchor_x_mm=l0.meta["l0"]["anchor_x_mm"],
                    anchor_y_mm=l0.meta["l0"]["anchor_y_mm"],
                    anchor_rot_deg=l0.meta["l0"]["anchor_rot_deg"])
                geometries[group_l0.name] = _unit_geometry(
                    group_l0.name, unit.offsets, geometries,
                    (unit.physical_box, unit.courtyard_box))
                composite: list = []
                for ref, offset in sorted(unit.offsets.items()):
                    composite.extend(_member_world_pads(ref, l0_fps[ref],
                                                        offset))
                unit_fp = _UnitFootprint(pads=tuple(sorted(
                    composite, key=lambda p: _pad_key(p.pad_number))))
                template_cells[group_l0.name] = (l0, unit, unit_fp)
                unit_members[group_l0.name] = sorted(group_l0.refs)
                rebuilt_l0[side].append(replace(
                    neighbour, ref=group_l0.name,
                    members=tuple(sorted(group_l0.refs)),
                    reason=f"{neighbour.reason}; L0 UNIT -- the family was "
                           f"constructed on its own anchor "
                           f"{l0.meta['l0']['anchor']} and enters L1 as one "
                           f"box with {len(unit.ports)} port(s)"))
                l0_rows.append({
                    "unit": group_l0.name, "side": side,
                    "kind": group_l0.kind, "family": group_l0.family,
                    "topology": group_l0.topology,
                    "members": sorted(group_l0.refs),
                    "anchor": l0.meta["l0"]["anchor"],
                    "anchor_why": l0.meta["l0"]["anchor_why"],
                    "placed": sorted(set(unit.refs)),
                    "unplaced": sorted(set(group_l0.refs) - set(unit.refs)),
                    "legal": bool(l0.legal),
                    "routed_fraction": l0.routed_fraction,
                    "courtyard_overlaps": l0.meta["courtyard_overlaps"],
                    "physical_overlaps": l0.meta["physical_overlaps"],
                    "ports": [p.to_dict() for p in unit.ports],
                    "port_sides": sorted({p.side for p in unit.ports}),
                    "physical_box": [round(v, 6) for v in unit.physical_box],
                    "courtyard_box": [round(v, 6) for v in unit.courtyard_box]})
                for ref in sorted(set(group_l0.refs) - set(unit.refs)):
                    # ⚠ Plan section 5.6: a member the L0 loop failed to place
                    # stays in the L0 failure log and is **NOT** silently
                    # promoted back to a ring-2 neighbour.
                    l0_failures.append({
                        "ref": ref, "ring": 0, "side": side,
                        "unit": group_l0.name, "step": "L0",
                        "why": f"the L0 loop on {group_l0.name} did not place "
                               f"it; it stays in the L0 failure log rather "
                               f"than being promoted back to ring 2"})
        by_side = {side: tuple(rebuilt_l0[side]) for side in SIDES}

    sums = {side: sum(n.priority for n in by_side[side]) for side in SIDES}
    order = tuple(sorted(SIDES, key=lambda s: (-sums[s], s)))
    every = tuple(n for side in SIDES for n in by_side[side])
    if not every:
        raise ConstructError(
            f"{board}/{anchor}: every one of the four side lists is EMPTY. An "
            f"instrument that can observe nothing must raise, never return a "
            f"falsy result (rule 3).")

    # -- S5: the load ratios, P-B's term, and P-C's offers ------------------ #
    # ⛔ P3's order is taken from the escape-side lists **before** any overflow
    # move: the valve changes where a part goes, not which side is built first.
    busy_terms: dict = {}
    busy_extra: dict = {}
    ratios: dict = {}
    for side in SIDES:
        extra, terms = busy_anchor_extra_mm(
            by_side[side], fab=fab, anchor_geometry=anchor_geom, side=side,
            geometries=geometries, anchor_rot_deg=anchor_rot_deg)
        busy_extra[side] = extra
        busy_terms[side] = [list(t) for t in terms]
        ratios[side] = {name: value for name, value, _why in terms}[
            "side_load_ratio"]

    positions = _pad_local_positions(anchor_geom)
    overflow: list = []
    offers: dict = {}
    if overflow_to_free_side:
        overflow, offers = _plan_overflow(
            by_side, ratios, escape_map=escape_map,
            anchor_rot_deg=anchor_rot_deg, positions=positions,
            admission=overflow_admission)

    #: ⭐ The distribution table, computed as a **difference against its own
    #: control arm** rather than asserted: which neighbour moved to which pad.
    distribution = []
    for side in SIDES:
        control = {n.ref: n.anchor_pad for n in by_side_control[side]}
        for neighbour in by_side[side]:
            if control.get(neighbour.ref, neighbour.anchor_pad) \
                    != neighbour.anchor_pad:
                distribution.append(
                    {"ref": neighbour.ref, "side": side, "net": neighbour.net,
                     "from_pad": control[neighbour.ref],
                     "to_pad": neighbour.anchor_pad})

    skipped = _skips(partition, anchor, escape_map, anchor_pads, nets_by_ref,
                     every, anchor_rot_deg)

    # -- the shared world -------------------------------------------------- #
    snapshot = session.snapshot()
    state = _CellState()
    anchor_moved = moved_pads(anchor_pads, x_mm=anchor_x_mm, y_mm=anchor_y_mm,
                              rot_deg=anchor_rot_deg,
                              base_rot_deg=anchor_fp.rotation)
    by_number = {str(pad.pad_number): pad for pad in anchor_moved}
    anchor_token = session.add_part(f"@ANCHOR:{anchor}", anchor_moved)
    state.put(anchor, x_mm=anchor_x_mm, y_mm=anchor_y_mm,
              rot_deg=anchor_rot_deg, geometry=anchor_geom,
              footprint=anchor_fp, token=anchor_token)

    placements: dict = {}
    #: ⚠ P-G's L0 failures are seeded here rather than appended later, so the
    #: accounting is total from the first line: a member the L0 loop lost is a
    #: **failure of this cell**, not an unaccounted part (rule 3).
    failures: list = list(l0_failures)
    disagreements: list = []
    rungs_used: dict = {}
    pairs: list = []
    timings: list = []
    side_meta: dict = {}

    def _record(neighbour, outcome, target_pad):
        placement = outcome["placement"]
        placements[neighbour.ref] = placement
        if placement.route_elapsed_s is not None:
            timings.append(placement.route_elapsed_s)
        if placement.routed:
            pairs.append({"ref": neighbour.ref, "neighbour": neighbour,
                          "target_ref": target_pad[0],
                          "target_pad": str(target_pad[1]),
                          "length_mm": placement.route_length_mm})

    # -- P3 + P6: one side at a time, finished before the next begins ------- #
    anchor_court = state.boxes_court[anchor]
    extents = {side: _side_extent(anchor_court, anchor_x_mm, anchor_y_mm, side)
               for side in SIDES}
    #: ⛔⛔ **Trap 16.** One cursor **per side**, because P-C can put a part on a
    #: side whose own turn has not come. S4 had one cursor and a side loop, and
    #: the moment a neighbour may leave its side that is no longer the same
    #: thing. ``laid`` is the per-side list **as placed**, which is what monotone
    #: and the tighten's sequence must read once ``Neighbour.side`` stops meaning
    #: *"the escape side of my anchor pad"*.
    cursors: dict = {side: None for side in SIDES}
    cursor_before: dict = {}
    laid: dict = {side: [] for side in SIDES}
    move_index = {move.ref: index for index, move in enumerate(overflow)}

    # -- P-F: the ring, sized from demand BEFORE anything is placed --------- #
    # ⛔⛔ **A two-pass scheme, stated rather than discovered.** ``R`` is a
    # function of the neighbours' along-edge courtyard extents, which depend on
    # their **rotations**; ``_choose_rotation`` scores a rotation by the distance
    # from the connecting pad to the anchor's, which depends on the standoff and
    # therefore on ``R``. That is circular, so it is broken in one stated place:
    # the rotations are chosen at the **shipped** fanout allowance, the ring is
    # sized against those rotations, and ``_place_one`` is then handed the same
    # rotation back through ``forced_rot``. ⛔ Finite, ordered, deterministic --
    # the same property the rotation enumeration itself has.
    def _footprint_of(ref):
        """The parsed footprint, or a P-G unit's composite one.

        ⛔ One lookup, used everywhere a footprint is fetched inside the loop:
        a second one is how the picture and the data drift apart, and a unit
        that fell through to ``session.pcb.footprints`` would return ``None``
        and be silently skipped (standing finding 1).
        """
        cell = template_cells.get(ref)
        return cell[2] if cell is not None else session.pcb.footprints.get(ref)

    def _pre_rotation(member, target_side):
        geometry = geometries.get(member.ref)
        footprint = _footprint_of(member.ref)
        pad = by_number.get(member.anchor_pad)
        if geometry is None or footprint is None or pad is None:
            return None
        _axis, _sign, along = _AXES[target_side]
        aim = (pad.global_x, pad.global_y)
        offs = (_connecting_pad_offsets(footprint, member, target_side)
                if align_connecting_pad else {})
        return _choose_rotation(
            member, geometry, footprint, target_side, extents[target_side],
            base, anchor_x_mm, anchor_y_mm, aim[along], aim,
            anchor_x_mm if along == 0 else anchor_y_mm, gap,
            align_offsets=offs,
            busy_extra=(busy_extra[target_side] if busy_anchor_keepout
                        else None))

    ring_rot: dict = {}
    ring_info = None
    ring_plan: dict = {}
    place_gap = gap
    if ring:
        for side in SIDES:
            for member in by_side[side]:
                rot = _pre_rotation(member, side)
                if rot is not None:
                    ring_rot[member.ref] = int(rot)
        ring_info = ring_radius(
            by_side, geometries=geometries, anchor_geometry=anchor_geom,
            fab=fab, anchor_rot_deg=anchor_rot_deg, anchor_x_mm=anchor_x_mm,
            anchor_y_mm=anchor_y_mm, edge_gap=arc_edge_gap,
            corner_owner=corner_owner, rotations=ring_rot,
            spawn_factor=spawn_factor, r_max_factor=r_max_factor)
        place_gap = float(ring_info["edge_gap_mm"])
        # ⛔ ONE box, taken from the sizing pass, so the placement and the shrink
        # cannot disagree about where the anchor is (see `ring_radius`).
        ring_box = tuple(ring_info["anchor_box"])
        for side in SIDES:
            row = ring_info["sides"][side]
            ring_plan[side] = ring_slots(
                by_side[side], side=side, radius_mm=row["R_spawn_mm"],
                anchor_box=ring_box, geometries=geometries,
                gap_mm=place_gap, owns=row["owns"], shares=row["shares"],
                reserve_mm=ring_info["reserve_mm"], rotations=ring_rot,
                r_max_mm=row["R_max_mm"])

    def _attempt(member, target_side, geometry, footprint):
        row = ring_info["sides"][target_side] if ring_info else None
        slot = (ring_plan.get(target_side, {}).get("slots", {}).get(member.ref)
                if ring else None)
        return _place_one(
            member, geometry, footprint, session=session,
            anchor_pad=by_number.get(member.anchor_pad), side=target_side,
            anchor_x_mm=anchor_x_mm, anchor_y_mm=anchor_y_mm,
            anchor_extent=extents[target_side],
            base=(float(row["R_spawn_mm"]) if row is not None else base),
            gap=place_gap,
            cursor=cursors[target_side], boxes_phys=state.boxes_phys,
            boxes_court=state.boxes_court, placed_parts=[],
            clearance_mm=clearance, route=route,
            align_connecting_pad=align_connecting_pad,
            busy_extra=(busy_extra[target_side] if busy_anchor_keepout
                        else None),
            slot=slot, allowance=(base if ring else None),
            commit_copper=commit_copper,
            # ⚠ A candidate P-C moved to a side the ring never sized has **no
            # slot**, and it falls back to the cursor rather than being handed
            # someone else's. Recorded in ``meta["ring"]["unslotted"]``, never
            # silent.
            forced_rot=(ring_rot.get(member.ref)
                        if ring and slot is not None else None))

    def _commit(member, outcome, target_side, geometry, footprint):
        placement = outcome["placement"]
        state.put(member.ref, x_mm=placement.x_mm, y_mm=placement.y_mm,
                  rot_deg=placement.rot_deg, geometry=geometry,
                  footprint=footprint, token=outcome["token"],
                  side=target_side, subanchor=anchor,
                  standoff_mm=placement.standoff_mm,
                  allowance_mm=_allowance_of(placement),
                  ring_extra_mm=(busy_extra[target_side]
                                 if busy_anchor_keepout else 0.0))
        _record(member, outcome, (anchor, member.anchor_pad))
        cursor_before[member.ref] = cursors[target_side]
        cursors[target_side] = outcome["cursor"]
        laid[target_side].append(member)
        access = escape_map.access(member.anchor_pad,
                                   _unrotate(target_side, anchor_rot_deg))
        if (access == "BLOCKED") != (not placement.routed):
            disagreements.append({
                "ref": member.ref, "ring": 1,
                "anchor_pad": member.anchor_pad, "side": target_side,
                "map_says": access,
                "router_says": "routed" if placement.routed else "unrouted",
                "direction": ("the map was OPTIMISTIC" if not placement.routed
                              else "the map UNDER-REPORTED")})
        return access

    for side in order:
        for neighbour in by_side[side]:
            if (neighbour.klass == "template" and not flatten_templates
                    and not template_units):
                skipped.append({
                    "ref": neighbour.ref, "members": list(neighbour.members),
                    "anchor_pad": neighbour.anchor_pad, "net": neighbour.net,
                    "side": side,
                    "why": "a template cell is placed as a UNIT and cell "
                           "placement is S4/S5 -- inventoried and "
                           "side-assigned here, not placed"})
                continue
            geometry = geometries.get(neighbour.ref)
            if geometry is None:
                skipped.append({"ref": neighbour.ref, "side": side,
                                "why": "no footprint geometry (standing "
                                       "finding 6)"})
                continue
            footprint = _footprint_of(neighbour.ref)
            if footprint is None:
                skipped.append({"ref": neighbour.ref, "side": side,
                                "why": "not on the session's board"})
                continue
            outcome = _attempt(neighbour, side, geometry, footprint)
            for name in outcome["rungs"]:
                rungs_used[name] = rungs_used.get(name, 0) + 1
            if outcome["placement"] is None:
                failures.append(dict(outcome["failure"], ring=1, side=side))
                continue
            _commit(neighbour, outcome, side, geometry, footprint)

    # -- P-C: the overflow valve, AFTER every side is laid out -------------- #
    # ⛔⛔ **The schedule is the whole content of this block and a first cut got
    # it wrong**, which is worth recording because the gate caught it: offering
    # a candidate its new side *at its ORIGIN side's turn* puts the arrivals on
    # the destination edge in **P3-of-origin order** rather than in the
    # destination's own **P2** order -- measured on ``ltc1871_sepic``, that
    # turned 0 same-side crossings into 6 and 0 cross-side into 10, because the
    # parts bound to the west row landed east of the parts bound to the east
    # row. ⭐ P2's ordering rule is *the projection of A's pin order onto that
    # edge*, and a part that changes edge has to be re-projected onto the new
    # one. So: every side is built first (which also makes ``pair_mm_before`` a
    # **measurement** rather than a counterfactual), then each candidate is
    # **withdrawn** and re-offered, destinations in P3 order and candidates in
    # the destination's own P2 order.
    def _withdraw(member):
        """⛔ Take one committed neighbour back off the board, exactly.

        Returns everything needed to put it back byte-identically -- P-C's
        revert is a **rollback**, never a second placement, so *"no move
        survived"* has to be indistinguishable from the control arm.
        """
        ref = member.ref
        saved = dict(state.entries[ref])
        if saved["token"] is not None:
            session.remove_part(saved["token"])
        state.entries.pop(ref, None)
        state.boxes_phys.pop(ref, None)
        state.boxes_court.pop(ref, None)
        home_side = saved["side"]
        laid[home_side] = [m for m in laid[home_side] if m.ref != ref]
        cursor_after = cursors[home_side]
        cursors[home_side] = cursor_before.get(ref, cursors[home_side])
        pair = next((p for p in pairs if p["ref"] == ref), None)
        pairs[:] = [p for p in pairs if p["ref"] != ref]
        seen = [d for d in disagreements
                if d["ref"] == ref and d.get("ring") == 1]
        for entry in seen:
            disagreements.remove(entry)
        return {"entry": saved, "side": home_side, "pair": pair,
                "placement": placements.pop(ref), "cursor": cursor_after,
                "disagreements": seen}

    def _reinstate(member, saved):
        ref = member.ref
        state.entries[ref] = dict(saved["entry"], token=None)
        state.refresh(ref)
        state.entries[ref]["token"] = session.add_part(f"@N:{ref}",
                                                       state.pads(ref))
        placements[ref] = saved["placement"]
        if saved["pair"] is not None:
            pairs.append(saved["pair"])
        disagreements.extend(saved["disagreements"])
        laid[saved["side"]].append(member)
        cursors[saved["side"]] = saved["cursor"]

    if offers:
        by_ref = {n.ref: n for side in SIDES for n in by_side[side]}
        pending = []
        for ref, destination in sorted(offers.items()):
            member = by_ref[ref]
            pending.append((
                order.index(destination),
                _reprojected_key(positions, member.anchor_pad, destination,
                                 anchor, anchor_geom, anchor_rot_deg,
                                 escape_map.footprint, ref,
                                 policy=overflow_order),
                ref, destination, member))
        for _rank, key, ref, destination, member in sorted(
                pending, key=lambda row: (row[0], row[1])):
            index = move_index[ref]
            saved = _withdraw(member) if ref in state.entries else None
            before = (saved["placement"].route_length_mm if saved else None)
            moved = replace(
                member, side=destination, origin_side=member.side,
                order_key=key,
                reason=f"{member.reason}; OVERFLOW -- offered {destination} "
                       f"because {member.side} is over-subscribed, and "
                       f"re-projected onto the {destination} edge")
            away = _attempt(moved, destination, geometries[ref],
                            _footprint_of(ref))
            for name in away["rungs"]:
                rungs_used[name] = rungs_used.get(name, 0) + 1
            if away["placement"] is not None:
                access = _commit(moved, away, destination, geometries[ref],
                                 _footprint_of(ref))
                overflow[index] = replace(
                    overflow[index], reason="kept", to_side=destination,
                    pair_mm_before=before,
                    pair_mm_after=away["placement"].route_length_mm,
                    probe_iterations=away["placement"].route_iterations,
                    destination_access=access,
                    why=f"the router routed the pair at the {destination} "
                        f"position; the map calls that side {access} for pad "
                        f"{member.anchor_pad}")
                continue
            # ⛔ Refused. Back where it was, exactly (overview 7.3: routable is
            # a DISQUALIFIER, never a score).
            if saved is not None:
                _reinstate(member, saved)
            overflow[index] = replace(
                overflow[index], reason="unroutable", to_side=destination,
                pair_mm_before=before,
                why=f"the ladder ran out on {destination}: "
                    f"{(away['failure'] or {}).get('why', '')} -- reverted to "
                    f"{member.side}")

    # ⛔ Trap 16 again: ``by_side`` is the P2 list and the placement order is a
    # different thing the moment P-C moves anything. Rebuilt **only** when a
    # move actually survived, so the OFF arm's serialisation is S4's exactly.
    if any(move.reason == "kept" for move in overflow):
        rebuilt: dict = {}
        for side in SIDES:
            here = {member.ref for member in laid[side]}
            gone = {move.ref for move in overflow
                    if move.reason == "kept" and move.from_side == side}
            rebuilt[side] = tuple(laid[side]) + tuple(
                n for n in by_side[side] if n.ref not in here | gone)
        by_side = rebuilt
    for side in SIDES:
        side_meta[side] = {
            "anchor_courtyard_extent_mm": extents[side],
            "placed": [member.ref for member in laid[side]],
            "priority_sum": sums[side]}

    # -- ⛔⛔ S7 C4: THE COMMITTED COPPER COMES OFF BEFORE ANYTHING MOVES ----- #
    # Every pass after this one (P-A centring, the tighten, the ring shrink,
    # the whole-cell re-route) MOVES a part, and copper committed at a position
    # a part no longer occupies is a wall nobody built. ⭐ So the commits are
    # popped in reverse and the census is ASSERTED back -- which is exactly the
    # invariant gate ``RT4`` proves in the small, run here in the large.
    # ⛔ ``exact=False`` here is plan BAIL-OUT C: record the trace, do not work
    # around it.
    commit_log = {"enabled": bool(commit_copper), "uncommitted": 0,
                  "exact": True}
    if commit_copper:
        commit_log = dict(session.uncommit_all(), enabled=True)

    # -- P-A: centre each side's column on the anchor ----------------------- #
    centring: list = []
    if centre_side_lists:
        centring = _centre_sides(
            session, state=state, order=order, laid=laid, pairs=pairs,
            anchor_box=anchor_court, clearance_mm=clearance, route=route)

    # -- ring 2 ------------------------------------------------------------- #
    universe = {ref for ref in group.refs if ref != anchor}
    for neighbour in every:
        universe |= {ref for ref in neighbour.members}
    universe -= {anchor}
    accounted = set(placements) | {f["ref"] for f in failures}
    # ⛔⛔ **P-G's accounting rule, and it is what keeps ring 2 honest.** A member
    # placed *inside* a placed L0 unit is accounted **by the unit**; it must not
    # come back round as a ring-2 candidate, or the same part would be placed
    # twice. ⚠ And a member the L0 loop **failed** is already in ``failures``
    # (seeded above), so it is not promoted back either -- plan section 5.6.
    for name, members in sorted(unit_members.items()):
        if name in accounted:
            accounted |= set(members)
    ring2_candidates = sorted(universe - accounted)

    ring2: dict = {}
    ring2_failures: list = []
    ring2_order: list = []
    ring2_cursor: dict = {}
    ring2_side_choices: list = []
    plane_fallbacks: list = []
    for ref in ring2_candidates:
        chosen = ring2_subanchor(ref, sorted(state.entries), nets_by_ref,
                                 plane_fallback=plane_fallback,
                                 group_of=group_of_ref)
        if chosen is None:
            ring2_failures.append({
                "ref": ref, "ring": 2,
                "why": ("no placed part shares a plane net with it either, so "
                        "there is no sub-anchor -- skipped rather than guessed"
                        if plane_fallback else
                        "no placed part shares a plane-free net with it, so "
                        "there is no sub-anchor -- skipped rather than "
                        "guessed")})
            continue
        sub, shared = chosen[0], chosen[1]
        via = chosen[2] if len(chosen) > 2 else "plane_free"
        if via == "plane_fallback":
            plane_fallbacks.append({"ref": ref, "subanchor": sub,
                                    "shared_plane_nets": list(shared),
                                    "via": via})
        geometry = geometries.get(ref)
        footprint = session.pcb.footprints.get(ref)
        if geometry is None or footprint is None:
            ring2_failures.append({
                "ref": ref, "ring": 2, "subanchor": sub,
                "why": "no footprint geometry or not on the session's board "
                       "(standing finding 6)"})
            continue
        sub_entry = state.entries[sub]
        sub_pads = state.pads(sub)
        sub_by_net = _anchor_pads_by_net(sub_pads)
        net = _pick_net(shared, sub_by_net)
        if net is None:
            ring2_failures.append({
                "ref": ref, "ring": 2, "subanchor": sub,
                "why": f"no pad of {sub} carries any of {shared}"})
            continue
        pad = sub_by_net[net][0]
        sub_map = _map_for(sub_entry["geometry"].footprint)
        try:
            local = favored_side(sub_map, pad)
        except ValueError:
            ring2_failures.append({
                "ref": ref, "ring": 2, "subanchor": sub, "anchor_pad": pad,
                "net": net, "why": ANCHOR_PAD_NOT_ESCAPABLE})
            continue
        side = rotate_escape(local, int(sub_entry["rot_deg"]))
        # -- P-D: ring 2 may not offer the side the ANCHOR is on ------------ #
        if ring2_avoids_anchor_side:
            # ⚠ ``sub is anchor`` is a DIFFERENT case and the plan says so in
            # advance: there is no *"side the anchor is on"* when the sub-anchor
            # **is** the anchor, so P-D has nothing to exclude and
            # ``lt3844_buck``'s ``L1`` is expected to fail again.
            forbidden = (None if sub == anchor else
                         _anchor_direction(state.boxes_phys[sub], anchor_x_mm,
                                           anchor_y_mm))
            picked, access_, why_ = (
                (None, "", "the sub-anchor IS the anchor -- P-D has nothing "
                           "to exclude") if forbidden is None else
                _ring2_side_excluding(sub_map, pad,
                                      int(sub_entry["rot_deg"]), forbidden))
            ring2_side_choices.append({
                "ref": ref, "subanchor": sub, "anchor_pad": pad, "net": net,
                "anchor_lies": forbidden, "side_without_policy": side,
                "side": picked if picked is not None else side,
                "pointed_at_the_anchor": side == forbidden,
                "changed": bool(picked is not None and picked != side),
                "access": access_, "why": why_})
            if picked is not None:
                side = picked
        klass, klass_shared, why = _classify(
            ref, set(nets_by_ref.get(sub, ())), nets_by_ref, {})
        positions = _pad_local_positions(sub_entry["geometry"])
        member = Neighbour(
            ref=ref, anchor_pad=str(pad), net=net, side=side, klass=klass,
            priority=_CLASS_RANK[klass] + len(klass_shared),
            order_key=(round(_pad_edge_coordinate(
                positions, pad, side, sub_entry["rot_deg"],
                sub_map.footprint), 6), _pad_key(pad), str(ref)),
            anchor_pads_on_net=len(sub_by_net.get(net, ())),
            reason=f"RING 2: sub-anchored on {sub} via {shared}; {why}; pad "
                   f"{pad} of {sub} carries {net} and favours {local} (world "
                   f"{side} at the sub-anchor's rotation "
                   f"{sub_entry['rot_deg']})")
        sub_court = state.boxes_court[sub]
        extent = _side_extent(sub_court, sub_entry["x_mm"], sub_entry["y_mm"],
                              side)
        target = next((p for p in sub_pads if str(p.pad_number) == str(pad)),
                      None)
        outcome = _place_one(
            member, geometry, footprint, session=session, anchor_pad=target,
            side=side, anchor_x_mm=sub_entry["x_mm"],
            anchor_y_mm=sub_entry["y_mm"], anchor_extent=extent, base=base,
            gap=gap, cursor=ring2_cursor.get((sub, side)),
            boxes_phys=state.boxes_phys, boxes_court=state.boxes_court,
            placed_parts=[], clearance_mm=clearance, route=route,
            align_connecting_pad=align_connecting_pad,
            commit_copper=commit_copper)
        for name in outcome["rungs"]:
            rungs_used[name] = rungs_used.get(name, 0) + 1
        if outcome["placement"] is None:
            ring2_failures.append(dict(outcome["failure"], ring=2,
                                       subanchor=sub, side=side))
            continue
        placement = outcome["placement"]
        state.put(ref, x_mm=placement.x_mm, y_mm=placement.y_mm,
                  rot_deg=placement.rot_deg, geometry=geometry,
                  footprint=footprint, token=outcome["token"], side=side,
                  subanchor=sub, standoff_mm=placement.standoff_mm,
                  allowance_mm=_allowance_of(placement))
        ring2[ref] = placement
        ring2_order.append(ref)
        ring2_cursor[(sub, side)] = outcome["cursor"]
        _record(member, outcome, (sub, pad))
        access = sub_map.access(str(pad), _unrotate(side,
                                                    sub_entry["rot_deg"]))
        if (access == "BLOCKED") != (not placement.routed):
            disagreements.append({
                "ref": ref, "ring": 2, "subanchor": sub, "anchor_pad": str(pad),
                "side": side, "map_says": access,
                "router_says": "routed" if placement.routed else "unrouted",
                "direction": ("the map was OPTIMISTIC" if not placement.routed
                              else "the map UNDER-REPORTED")})

    # -- P7 ----------------------------------------------------------------- #
    tighten_steps: list = []
    shrink_rows: list = []
    final_reroute_ok = None
    final_reroute_failed = None
    if tighten:
        if tighten_floor not in TIGHTEN_FLOORS:
            raise ConstructError(
                f"tighten_floor={tighten_floor!r} is not one of "
                f"{TIGHTEN_FLOORS}")
        if tighten_mode == "ring":
            # ⛔ **P-H.** Shrink the RADIUS, not the parts. It needs a ring to
            # shrink, so a ring-less arm asking for it is a caller error rather
            # than a silent fall-back to the per-part pass.
            if ring_info is None:
                raise ConstructError(
                    "tighten_mode='ring' needs ring=True -- there is no radius "
                    "to shrink on the linear arm, and silently running the "
                    "per-part pass instead would make the arm unattributable")
            shrink_rows = shrink_ring(
                session, state=state, order=order, laid=laid,
                radius=ring_info, pairs=pairs, anchor=anchor,
                anchor_x_mm=anchor_x_mm, anchor_y_mm=anchor_y_mm,
                anchor_extents=extents, geometries=geometries,
                clearance_mm=clearance,
                step_mm=(float(shrink_step_mm) if shrink_step_mm
                         else clearance), route=route)
        else:
            tighten_steps = _tighten_cell(
                session, order=order, by_side=by_side,
                ring2_order=ring2_order, state=state, pairs=pairs,
                step_mm=clearance, clearance_mm=clearance, route=route,
                floor_policy=tighten_floor)
        if route and pairs:
            # ⛔ Bail-out 8's evidence: the pass is transactional by
            # specification, so EVERY pair must still route once it is over.
            # ⚠ **And when it does not, the artifact must say enough to tell
            # WHOSE fault it is**: a pass that accepted no step at all left the
            # geometry exactly as the construction built it, so a re-route that
            # fails there is the *construction's* uncommitted view being
            # optimistic (overview 7.5), not the tighten breaking something.
            final_reroute_ok, failed_entry, lengths = _reroute_all(
                session, pairs, state)
            final_reroute_failed = (None if failed_entry is None
                                    else str(failed_entry["ref"]))
            tighten_steps = [
                replace(step, route_length_after_mm=(
                    lengths[step.ref].length_mm if step.ref in lengths
                    else None))
                for step in tighten_steps]

    # ⛔ Rule 3's accounting is *exactly one* bucket per part: a ref ring 2
    # placed is no longer a skip, whatever the side pass called it.
    skipped = [entry for entry in skipped if entry["ref"] not in ring2]

    # -- the final positions, the boxes, the verdict ------------------------ #
    for ref, placement in list(placements.items()):
        entry = state.entries[ref]
        placements[ref] = replace(placement, x_mm=round(entry["x_mm"], 6),
                                  y_mm=round(entry["y_mm"], 6))
    ring2 = {ref: placements[ref] for ref in ring2_order}

    census_before_rollback = session.census
    session.restore(snapshot)

    parts = state.placed_parts()
    used = {ref: state.entries[ref]["geometry"] for ref in state.entries}
    verdict = validate(parts, None,
                       {g.footprint: (g.width_mm, g.height_mm)
                        for g in used.values()},
                       clearance_mm=clearance,
                       fp_geometries={g.footprint: g for g in used.values()})
    refs = sorted(state.boxes_court)
    court_overlaps = sorted(
        [a, b] for i, a in enumerate(refs) for b in refs[i + 1:]
        if _pair_gap(state.boxes_court[a], state.boxes_court[b]) < -_TOL)
    gap_min = courtyard_gap(state.boxes_court)

    sides: list = []
    for side in order:
        chosen = [placements[n.ref] for n in by_side[side]
                  if n.ref in placements]
        sides.append(SideResult(
            board=board, anchor=anchor, side=side,
            neighbours=by_side[side], placements=tuple(chosen),
            failures=tuple(f for f in failures if f.get("side") == side),
            skipped=tuple(s for s in skipped if s.get("side") == side),
            standoff_base_mm=base,
            meta=dict(side_meta[side],
                      monotone=_is_monotone(chosen, side),
                      routed=sum(1 for p in chosen if p.routed),
                      class_census=_census(n.klass for n in by_side[side]))))

    accounted_final = (set(placements) | set(ring2)
                       | {f["ref"] for f in failures}
                       | {f["ref"] for f in ring2_failures}
                       | {s["ref"] for s in skipped})
    for name, members in sorted(unit_members.items()):
        if name in accounted_final:
            accounted_final |= set(members)
    meta = {
        "anchor_footprint": anchor_geom.footprint,
        "anchor_x_mm": round(float(anchor_x_mm), 6),
        "anchor_y_mm": round(float(anchor_y_mm), 6),
        "anchor_rot_deg": int(anchor_rot_deg),
        "side_order": list(order),
        "side_priority_sums": dict(sorted(sums.items())),
        "side_counts": {s: len(by_side[s]) for s in SIDES},
        "flags": {"flatten_templates": bool(flatten_templates),
                  "distribute_stacked": bool(distribute_stacked),
                  "align_connecting_pad": bool(align_connecting_pad),
                  "tighten": bool(tighten), "tighten_floor": tighten_floor,
                  "route": bool(route)},
        "flattened": [{"ref": n.ref, "members": list(n.members),
                       "net": n.net, "anchor_pad": n.anchor_pad,
                       "side": n.side}
                      for n in every if n.klass == "template"],
        "distribution": distribution,
        "ring2_candidates": ring2_candidates,
        "ring2_table": [{"ref": ref, "subanchor": state.entries[ref]
                         ["subanchor"], "side": state.entries[ref]["side"],
                         "standoff_mm": state.entries[ref]["standoff_mm"]}
                        for ref in ring2_order],
        "edge_gap_mm": gap,
        "edge_gap_terms": [list(t) for t in EDGE_GAP_TERMS],
        "fab": str(getattr(fab, "name", "")),
        "clearance_mm": clearance,
        "track_width_mm": float(fab.track_width_mm),
        "via_size_mm": float(fab.via_size_mm),
        "physical_overlaps": [list(pair) for pair in verdict.overlaps],
        "courtyard_overlaps": court_overlaps,
        "courtyard_min_gap_mm": None if gap_min == float("inf") else gap_min,
        "courtyard_missing": sorted(
            ref for ref, g in used.items() if g.courtyard_bounds is None),
        "map_vs_router": disagreements,
        "classes_declared": list(NEIGHBOUR_CLASSES),
        "classes_seen": sorted({n.klass for n in every}),
        "class_census": _census(n.klass for n in every),
        "ladder_rungs_declared": list(LADDER_RUNGS),
        "ladder_rungs_used": dict(sorted(rungs_used.items())),
        "tighten_floors_declared": list(TIGHTEN_FLOORS),
        "tighten_stop_reasons_declared": list(TIGHTEN_STOP_REASONS),
        "tighten_stop_reasons_seen": sorted({s.stopped_by
                                             for s in tighten_steps}),
        "tighten_recovered_mm": round(sum(s.recovered_mm
                                          for s in tighten_steps), 6),
        "tighten_step_mm": clearance,
        "tighten_final_reroute_ok": final_reroute_ok,
        "tighten_final_reroute_failed": final_reroute_failed,
        #: ⭐ *Did the pass move anything at all?* When it did not, the geometry
        #: at the end is the construction's own, and a failed whole-cell
        #: re-route there is attributable to the construction rather than to the
        #: tighten. ⛔ The distinction is the difference between bail-out 8 and
        #: overview 7.5's *"record every place the uncommitted view was
        #: optimistic"* list.
        "tighten_moved_nothing": bool(
            tighten_steps and all(step.recovered_mm == 0.0
                                  for step in tighten_steps)),
        "monotone": {side: _is_monotone(
            [placements[n.ref] for n in by_side[side] if n.ref in placements],
            side) for side in SIDES},
        "stacked_anchor_pads": sorted(
            {n.anchor_pad for side in SIDES for n in by_side[side]
             if sum(1 for m in by_side[side]
                    if m.anchor_pad == n.anchor_pad) > 1}),
        "accounting": {
            "universe": sorted(universe),
            "placed_on_a_side": sorted(set(placements) - set(ring2)),
            "placed_in_ring_2": sorted(ring2),
            "failed": sorted({f["ref"] for f in failures}
                             | {f["ref"] for f in ring2_failures}),
            "skipped": sorted({s["ref"] for s in skipped}),
            "unaccounted": sorted(universe - accounted_final),
            "total": not (universe - accounted_final)},
        "route_calls": len(timings),
        "rollback_exact": bool(session.census == snapshot.census),
        "census_at_end_of_construction": list(census_before_rollback),
        "census_after_rollback": list(session.census),
        "timing": {"route_total_s": round(sum(timings), 6),
                   "route_max_s": round(max(timings), 6) if timings else None,
                   "route_mean_s": (round(sum(timings) / len(timings), 6)
                                    if timings else None)},
    }
    # ⛔⛔ **EMITTED ONLY WHEN THE POLICY IS ON**, for the reason S5 states two
    # blocks below: the OFF arm's control is a PLAIN DIFF against a recorded
    # artifact, so a key that is always present would make *"byte-identical"* a
    # claim about a stripper rather than about behaviour.
    if commit_copper:
        meta["commit_copper"] = commit_log
    # -- S5: the new blocks, emitted ONLY when a policy is on --------------- #
    # ⛔ Conditional on purpose. Gate ``AR0``'s control is a **plain diff**
    # against S4's recorded ``cell_log.json``, so the OFF arm must serialise
    # exactly what S4 serialised -- a new key that is always present would make
    # *"byte-identical"* a claim about a stripper rather than about behaviour.
    # ⭐ Every one of these is computable from the public surface
    # (:func:`pair_crossings`, :func:`side_span`, :func:`busy_anchor_extra_mm`),
    # so the driver measures the control arm with the same instruments.
    if any((centre_side_lists, busy_anchor_keepout, overflow_to_free_side,
            ring2_avoids_anchor_side)):
        undeclared = sorted({m.reason for m in overflow}
                            - set(OVERFLOW_REASONS))
        if undeclared:
            raise ConstructError(
                f"overflow move(s) carry the undeclared reason(s) "
                f"{undeclared} -- every candidate must end in exactly one of "
                f"{OVERFLOW_REASONS} (rule 3)")
        meta["flags"].update({
            "centre_side_lists": bool(centre_side_lists),
            "busy_anchor_keepout": bool(busy_anchor_keepout),
            "overflow_to_free_side": bool(overflow_to_free_side),
            "ring2_avoids_anchor_side": bool(ring2_avoids_anchor_side),
            "overflow_admission": overflow_admission,
            "overflow_order": overflow_order})
        meta["crossings"] = pair_crossings(
            _pair_lines(state, pairs, set(ring2)))
        meta["side_spans"] = {
            side: side_span([placements[n.ref] for n in laid[side]
                             if n.ref in placements], side, geometries,
                            anchor_box=state.boxes_court[anchor])
            for side in SIDES if laid[side]}
        meta["busy_keepout"] = {"applied": bool(busy_anchor_keepout),
                                "extra_mm": dict(sorted(busy_extra.items())),
                                "side_load_ratio": {
                                    s: round(float(v), 6)
                                    for s, v in sorted(ratios.items())},
                                "terms": {s: busy_terms[s]
                                          for s in sorted(busy_terms)}}
        meta["centring"] = centring
        meta["overflow"] = [move.to_dict() for move in overflow]
        meta["overflow_reasons_declared"] = list(OVERFLOW_REASONS)
        meta["overflow_reasons_seen"] = sorted({m.reason for m in overflow})
        meta["overflow_admission_declared"] = list(OVERFLOW_ADMISSION)
        meta["overflow_order_declared"] = list(OVERFLOW_ORDER)
        meta["ring2_side_choice"] = ring2_side_choices

    # -- S5C: the arc's blocks, emitted ONLY when one of ITS flags is on ----- #
    # ⛔ Conditional for the same reason S5's block is: gate ``AC4``'s control
    # arm (``favored x linear x flattened``) is a **plain diff** against S4/S5's
    # recorded artifact, so a key that were always present would make
    # *"byte-identical"* a claim about a stripper rather than about behaviour.
    # -- S9 Stage C: the labelled plane fallback, emitted ONLY when it is on -- #
    # ⛔ Conditional for the reason every block above is: gate ``PN3``'s OFF
    # control is a plain diff against S8's recorded artifact.
    if plane_fallback:
        meta["flags"]["plane_fallback"] = True
        meta["plane_fallback"] = {
            "enabled": True,
            "placed_against_a_plane_net": [
                row for row in plane_fallbacks if row["ref"] in ring2],
            "offered": plane_fallbacks,
            "still_skipped": sorted(f["ref"] for f in ring2_failures),
            "key": "(-shared_plane_nets, not same_partition_group, ref)"}
    if any((side_assignment != "favored", ring, tighten_mode != "part",
            template_units)):
        meta["flags"].update({
            "side_assignment": side_assignment, "ring": bool(ring),
            "arc_edge_gap": arc_edge_gap, "corner_owner": corner_owner,
            "spawn_factor": float(spawn_factor),
            "tighten_mode": tighten_mode,
            "template_units": bool(template_units)})
        meta["l0"] = {
            "enabled": bool(template_units),
            "units": l0_rows,
            "members_inside_units": {name: unit_members[name]
                                     for name in sorted(unit_members)},
            #: ⛔ **A unit is ONE box at L1 and n PARTS on the board**, and every
            #: consumer that has to draw it, write it or route it needs the
            #: member offsets. A renderer that saw only the box would draw a
            #: picture with parts missing -- standing finding 1's fifth instance
            #: was exactly a viewer that drew nothing.
            "offsets": {name: {member: [round(float(v), 6) for v in offset]
                               for member, offset
                               in sorted(cell[1].offsets.items())}
                        for name, cell in sorted(template_cells.items())},
            "unplaced_by_l0": sorted(row["ref"] for row in l0_failures)}
        meta["side_assignment_declared"] = list(SIDE_ASSIGNMENT)
        meta["arc_edge_gap_declared"] = list(ARC_EDGE_GAP)
        meta["corner_owner_declared"] = list(CORNER_OWNER)
        meta["tighten_mode_declared"] = list(TIGHTEN_MODE)
        meta["side_demand"] = {
            "assignment": demand["assignment"],
            "reasons_declared": list(SIDE_DEMAND_REASONS),
            "reasons_seen": sorted({row["reason"] for row in demand["rows"]}),
            "moved": [row for row in demand["rows"] if row["moved"]],
            "rows": demand["rows"], "loads": demand["loads"],
            "packed_mm": demand["packed"],
            "edge_gap_mm": demand["edge_gap_mm"]}
        if ring_info is not None:
            meta["ring"] = {
                "radius": {side: ring_info["sides"][side] for side in SIDES},
                "owners": ring_info["owners"],
                "corner_owner": ring_info["corner_owner"],
                "edge_gap": ring_info["edge_gap"],
                "edge_gap_mm": ring_info["edge_gap_mm"],
                "reserve_mm": ring_info["reserve_mm"],
                "allowance_mm": ring_info["allowance_mm"],
                "spawn_factor": ring_info["spawn_factor"],
                "max_R_fit_mm": ring_info["max_R_fit_mm"],
                "infeasible": ring_info["infeasible"],
                "quadrants_declared": list(QUADRANTS),
                "rotations": dict(sorted(ring_rot.items())),
                "slots": {side: ring_plan[side] for side in SIDES
                          if side in ring_plan},
                #: ⛔ Every neighbour the ring never sized a slot for, named.
                #: A part that silently fell back to the cursor would make the
                #: arm unattributable (standing finding 1).
                "unslotted": sorted(
                    n.ref for side in SIDES for n in by_side[side]
                    if n.ref not in ring_plan.get(side, {}).get("slots", {}))}
        meta["ring_shrink"] = {
            "mode": tighten_mode,
            "stop_reasons_declared": list(RING_STOP_REASONS),
            "stop_reasons_seen": sorted({row["stopped_by"]
                                         for row in shrink_rows}),
            "recovered_mm": round(sum(row["recovered_mm"]
                                      for row in shrink_rows), 6),
            "step_mm": (float(shrink_step_mm) if shrink_step_mm
                        else clearance),
            "rows": shrink_rows}
        # ⭐ The same two instruments the S5 block uses, so an arc arm and a
        # policy arm are measured with one ruler.
        meta.setdefault("crossings",
                        pair_crossings(_pair_lines(state, pairs, set(ring2))))
        meta.setdefault("side_spans", {
            side: side_span([placements[n.ref] for n in laid[side]
                             if n.ref in placements], side, geometries,
                            anchor_box=state.boxes_court[anchor])
            for side in SIDES if laid[side]})

    return CellResult(board=board, anchor=anchor, sides=tuple(sides),
                      ring2=tuple(ring2[ref] for ref in ring2_order),
                      ring2_failures=tuple(sorted(
                          ring2_failures, key=lambda f: str(f["ref"]))),
                      tighten=tuple(tighten_steps),
                      skipped=tuple(sorted(skipped,
                                           key=lambda s: str(s["ref"]))),
                      meta=meta)


def cell_result_to_dict(result: CellResult) -> dict:
    """A JSON-serialisable, order-stable view of a whole cell.

    ⚠ **Wall clock is on the artifact and is excluded from the determinism
    digest by the caller** -- and by **one** stripper shared with the S3
    control arm, because two strippers is how *"identical"* stops meaning
    identical.
    """
    return {
        "board": result.board,
        "anchor": result.anchor,
        "routed_fraction": result.routed_fraction,
        "legal": result.legal,
        "sides": [side_result_to_dict(side) for side in result.sides],
        "ring2": [p.to_dict() for p in result.ring2],
        "ring2_failures": list(result.ring2_failures),
        "tighten": [step.to_dict() for step in result.tighten],
        "skipped": list(result.skipped),
        "meta": result.meta,
    }


# =========================================================================== #
# S5C -- THE ARC. ⛔ Four flagged policies, every one default OFF.
#
# ⭐⭐⭐ **The observation this section implements, from the human on 2026-08-05
# after re-rendering all 21 S4/S5/S5B pictures byte-identically:** *the cell must
# be built as an **arc around the central part**, not as a line down one edge* --
# and **tightening as built is the wrong motion**.
#
# ⛔ None of this is a new geometry engine and none of it is an objective. P-E
# consumes an answer :mod:`~skidl_layout.escape_map` has always given and nobody
# read; P-F turns ``base`` in :func:`_position_for` -- today the constant
# 2.0048 mm -- into a **per-side function of that side's own demand**; P-G runs
# the *same* loop one level down so a template finally has ports; P-H shrinks the
# **radius** instead of the parts, which is the one motion with an arithmetic
# floor no later pass can move.
# =========================================================================== #

#: ⛔ **P-E.** The two readings of *"which side does this neighbour go on?"*.
#:
#: * ``favored`` (the default, and S3/S4/S5/S5B's behaviour) --
#:   :func:`~skidl_layout.escape_map.favored_side` of the anchor pad, **one**
#:   side per pad. ⛔⛔ **Measured: this is why N and S are EMPTY side lists on
#:   3 of 4 subjects** (E/W splits of 7/4, 7/7 and 12/10) -- the map's
#:   ``ACCESSIBLE`` answers are never consulted, so there is nothing to
#:   distribute around an arc.
#: * ``escapable`` -- :func:`~skidl_layout.escape_map.escapable_sides_ranked`,
#:   so a neighbour whose favoured side is over-subscribed may be offered
#:   another side **the map already says it can leave from**. ⛔ ``BLOCKED`` is
#:   never offered: S1's soundness (127 + 373 routed probes, **zero
#:   over-reports**) is the most valuable property this arc owns.
SIDE_ASSIGNMENT: tuple[str, ...] = ("favored", "escapable")

#: ⛔ **P-F.** The two readings of *"how much room between two neighbours along
#: the edge?"*, and the difference is plan section 5.2's [CALL].
#:
#: * ``edge_gap`` -- the shipped :func:`edge_gap_mm`, ``track + 2 x clearance``:
#:   room for **one passing trace**.
#: * ``fanout`` -- the human's arc specification says the spacing between
#:   neighbours is *"a via, two tracks and the part courtyards"*, which is
#:   :func:`standoff_base_mm` -- the same number the **perpendicular** allowance
#:   uses. ⛔ Neither is "correct" until S6 grades them; both ship and the
#:   difference is 3.6-6.6 mm of R on the loaded sides.
ARC_EDGE_GAP: tuple[str, ...] = ("edge_gap", "fanout")

#: ⛔ **P-F.** Who may put a part in a corner quadrant.
#:
#: * ``none`` -- every side may spill into both of its quadrants, which is
#:   exactly the ``capacity = face + 2R`` arithmetic plan section 2.4 sized R
#:   with. ⚠ Two **adjacent** loaded sides can then claim the same quadrant.
#: * ``heavier`` -- each quadrant has **exactly one** owner: the adjacent side
#:   with the larger ``packed``, ties by the index in :data:`SIDES`. A side that
#:   owns a quadrant reserves one clearance at that end of its run, so two owned
#:   runs cannot touch at the corner point either.
#:
#: * ``split`` -- each quadrant is **partitioned**: both adjacent sides get
#:   ``(R - reserve) / 2`` of it. Single-claimant by **area** rather than by
#:   side, so two runs still cannot overlap and **nobody starves**.
#:
#: ⛔ **Correcting a plausible-sounding argument before someone re-derives it:**
#: it is *not* true that two adjacent sides' parts can never overlap because
#: "each stays on its own side of the ring line". An E-side run and an N-side run
#: overlap **precisely inside the NE quadrant**, and both can reach it once
#: either spills past the corner point.
#:
#: ⛔⛔⛔ **AND CONTACT REFUTED THE FIRST TWO READINGS IN OPPOSITE DIRECTIONS,
#: WHICH IS WHY THE THIRD EXISTS** (measured 2026-08-05, gate ``AC2``, and
#: **only** once P-E has something to distribute):
#:
#: * under ``none`` two **adjacent** runs claim one quadrant on **2 of 4**
#:   subjects -- exactly the overlap the paragraph above warns about;
#: * under ``heavier`` a balanced ring has **four** loaded runs and **four**
#:   quadrants, so the two heaviest sides take all four and a side that needs
#:   more than its own face owns **nothing** -- ``R_fit`` collapses to the
#:   allowance and the run is a named failure on **2 of 4** subjects.
#:
#: ⭐ ``split`` is the derivation that is right in both directions, and it needs
#: no tie-break at all. ⛔ All three ship; ``none`` remains the arithmetic plan
#: section 2.4 sized ``R`` with, and both refuted readings stay **measurable**.
CORNER_OWNER: tuple[str, ...] = ("none", "heavier", "split")

#: ⛔ **P-H.** What the shrink moves.
#:
#: * ``part`` -- :func:`_tighten_cell`, the shipped per-part pass. ⛔ Its floor is
#:   ``standoff - allowance`` (S4), so it recovers **exactly the fanout
#:   allowance** and stops at courtyard contact -- *it deletes the one term P4
#:   exists to create* -- and at ring 2 it **chains** (standing finding 28).
#: * ``ring`` -- :func:`shrink_ring`: step the per-side **radius** down,
#:   transactionally, re-flowing that side's slots each step, floored at
#:   ``max(fanout_allowance, R_fit(side))``. ⭐ A floor that is arithmetic has no
#:   sub-anchor to inherit from, so finding 28's mechanism is removed rather than
#:   repaired.
TIGHTEN_MODE: tuple[str, ...] = ("part", "ring")

#: The four corner regions of the offset rectangle. ⛔ Declared so a quadrant
#: that is never claimed is *reported* rather than invisible (finding 20).
QUADRANTS: tuple[str, ...] = ("NE", "SE", "SW", "NW")

#: ⛔ Which quadrant sits at each end of a side's edge, and which side is the
#: other claimant. ⭐ It is a **derivation** rather than a table: ``_AXES`` says
#: which axis a side runs along, the "lo" end is the smaller coordinate, and the
#: quadrant is named for its two sides. ``test_construct.py`` re-derives it from
#: ``_AXES`` and asserts the two agree, so the table cannot rot.
_QUADRANT_ENDS: dict = {"E": (("NE", "N"), ("SE", "S")),
                        "W": (("NW", "N"), ("SW", "S")),
                        "N": (("NW", "W"), ("NE", "E")),
                        "S": (("SW", "W"), ("SE", "E"))}

#: ⛔ Why one side's radius shrink stopped where it did. Declared as data so a
#: reason that never occurs is reported (standing finding 20).
#:
#: * ``floor`` -- ``max(fanout_allowance, R_fit(side))`` was reached. ⭐ The
#:   designed stopping place, and the only one that is pure arithmetic.
#: * ``fit`` -- the next radius no longer gives the side's own run enough
#:   capacity (``face + 2R`` shrinks with R).
#: * ``collision`` -- the step put a box into another box.
#: * ``unroutable`` -- the step was legal and then a pair of the cell stopped
#:   routing, so it was reverted.
#: * ``cap`` -- the bounded step budget ran out first.
RING_STOP_REASONS: tuple[str, ...] = ("floor", "fit", "collision",
                                      "unroutable", "cap")

#: ⛔ Why one neighbour ended on the side it did, under :data:`SIDE_ASSIGNMENT`.
#: **Every** neighbour gets exactly one, on **both** arms: a policy that reports
#: only the parts it moved is the observes-nothing defect wearing a bookkeeping
#: hat (standing finding 1, six instances in seven runs).
SIDE_DEMAND_REASONS: tuple[str, ...] = ("favoured (single-valued assignment)",
                                        "only escapable side",
                                        "favoured, not crowded",
                                        "balanced onto the least loaded "
                                        "escapable side")


def arc_gap_mm(fab, policy: str = "edge_gap") -> float:
    """The along-the-edge gap between two ring neighbours. ⛔ Every term from the
    ``FabSpec``, never a constant (plan section 5.2)."""
    if policy not in ARC_EDGE_GAP:
        raise ConstructError(f"edge_gap={policy!r} is not one of {ARC_EDGE_GAP}")
    return edge_gap_mm(fab) if policy == "edge_gap" else standoff_base_mm(fab)


def _edge_extent(geometry, ref: str, side: str, rot_deg: int) -> float:
    """One part's **courtyard** extent along ``side``'s edge, at ``rot_deg``.

    ⛔ The courtyard and never ``physical_bounds`` (standing finding 13): the
    measured overhang is 0.175 mm/side at 0402 and 0.275-0.280 at 0603/0805, so
    a packing computed on the wrong box is short by exactly that *and every test
    still passes at 0805*.
    """
    _axis, _sign, along = _AXES[side]
    box = _courtyard_box(geometry, ref, 0.0, 0.0, int(rot_deg))
    return round(box[along + 2] - box[along], 6)


def _facing_extent(geometry, ref: str, side: str, rot_deg: int) -> float:
    """The part's own courtyard half-extent **facing** the anchor -- the third
    term of :func:`_position_for`'s standoff, read back for the shrink."""
    axis, sign, _along = _AXES[side]
    local = geometry._transform_bounds(
        geometry.bounds, _placed(ref, 0.0, 0.0, int(rot_deg),
                                 geometry.footprint))
    return round((-local[axis]) if sign > 0 else local[axis + 2], 6)


# --------------------------------------------------------------------------- #
# P-E -- the escape demand
# --------------------------------------------------------------------------- #
def side_demand(partition, anchor: str, escape_map, *, anchor_pads,
                anchor_geometry, anchor_rot_deg: int = 0, nets_by_ref=None,
                geometries=None, fab=None, flatten_templates: bool = False,
                distribute_stacked: bool = False,
                assignment: str = "favored",
                edge_gap: str = "edge_gap",
                overflow_order: str = "edge_depth") -> dict:
    """⭐⭐⭐ **P-E.** The four side lists, with the map's *other* answers used.

    Returns ``{"by_side": {side: (Neighbour, ...)}, "rows": [...],
    "loads": {side: n}, "packed": {side: mm}, "assignment": str}``.

    ⛔ ``assignment="favored"`` **must reproduce** :func:`side_neighbours` on all
    four sides exactly -- that is gate ``AC1``'s control and the reason the flag
    exists. Under ``"escapable"`` the decision is plan section 5.1's, in strict
    order and with a reason recorded for **every** neighbour::

        for each neighbour, in P1 priority order then order_key:
            sides = escapable_sides_ranked(emap, anchor_pad)   # FAVORED first
            if len(sides) == 1:        -> that side
            if favoured side load <= 1 -> favoured
            else: argmin over sides of (packed(side) after adding me,
                                        access rank, corridor cost, side letter)

    ⛔ **A total key over content, never arrival order** (standing finding 8) --
    the argmin's four terms are all content, and the iteration order is the P1
    priority sort.
    ⛔ **``BLOCKED`` is never offered.** S5 already spent the optimism budget
    honestly (19 non-favoured placements, 19 routed, still zero over-reports);
    that licenses using ``ACCESSIBLE``, and nothing more.
    ⚠ **This is the second rule in the procedure that lets a neighbour leave the
    side its anchor pad favours** (P-C was the first), so a moved neighbour's
    ``order_key`` is **re-projected onto the new edge** with
    :func:`_reprojected_key` and its escape side is kept in
    :attr:`Neighbour.origin_side`. Open question 16 says no lexicographic key
    repairs an off-edge fan, so the guard stays downstream in
    :func:`pair_crossings`, measured per arm.
    ⚠ The balancing extents are measured at **rotation 0** -- the rotation is
    chosen later, by :func:`_choose_rotation`, and a demand table that waited for
    it would be circular. On an E/W side the loop places at 0/180, whose
    along-edge extent is identical to rotation 0's, so the approximation is exact
    there and an over-estimate on N/S; :func:`ring_radius` re-measures at the
    **chosen** rotations and the two are reported side by side.
    """
    from .escape_map import escapable_sides_ranked, rotate_escape

    if assignment not in SIDE_ASSIGNMENT:
        raise ConstructError(
            f"assignment={assignment!r} is not one of {SIDE_ASSIGNMENT}")
    by_side = {side: side_neighbours(
        partition, anchor, escape_map, side=side, anchor_pads=anchor_pads,
        anchor_geometry=anchor_geometry, anchor_rot_deg=anchor_rot_deg,
        nets_by_ref=nets_by_ref, flatten_templates=flatten_templates,
        distribute_stacked=distribute_stacked) for side in SIDES}
    every = [n for side in SIDES for n in by_side[side]]
    if not every:
        raise ConstructError(
            f"{anchor!r}: every one of the four side lists is EMPTY -- a demand "
            f"table over nothing is indistinguishable from one that found "
            f"everything (standing finding 1)")
    gap = arc_gap_mm(fab, edge_gap) if fab is not None else 0.0
    positions = _pad_local_positions(anchor_geometry)

    def _ext(neighbour, side):
        geometry = (geometries or {}).get(neighbour.ref)
        if geometry is None:
            raise ConstructError(
                f"side_demand cannot balance {neighbour.ref!r} without its "
                f"footprint geometry -- a packing computed over a 2 x 2 mm "
                f"fallback is self-consistent and wrong (standing finding 6)")
        return _edge_extent(geometry, neighbour.ref, side, 0)

    rows: list = []
    if assignment == "favored":
        for side in SIDES:
            for neighbour in by_side[side]:
                rows.append({"ref": neighbour.ref, "from_side": side,
                             "to_side": side, "moved": False,
                             "reason": SIDE_DEMAND_REASONS[0],
                             "access": "FAVORED", "cost": None,
                             "escapable": [side],
                             "why": f"favored_side({neighbour.anchor_pad}) is "
                                    f"{side} and nothing else is consulted"})
        loads = {side: len(by_side[side]) for side in SIDES}
        packed = {side: (round(sum(_ext(n, side) for n in by_side[side])
                               + max(0, len(by_side[side]) - 1) * gap, 6)
                         if by_side[side] and geometries else 0.0)
                  for side in SIDES}
        return {"by_side": by_side, "rows": rows, "loads": loads,
                "packed": packed, "assignment": assignment,
                "edge_gap_mm": gap}

    loads = {side: 0 for side in SIDES}
    packed = {side: 0.0 for side in SIDES}
    chosen: dict = {}
    ordered = sorted(every, key=lambda n: (-n.priority, n.order_key, n.ref))
    #: ⛔ The favoured side is what the neighbour already carries: it came out of
    #: ``side_neighbours``, which is ``favored_side`` rotated into the world.
    favoured_load = {side: len(by_side[side]) for side in SIDES}
    for neighbour in ordered:
        pad = neighbour.anchor_pad
        ranked = escapable_sides_ranked(escape_map, pad)
        world = [(rotate_escape(local, int(anchor_rot_deg)),
                  0 if index == 0 else 1, round(float(cost), 6), local)
                 for index, (local, cost) in enumerate(ranked)]
        favoured = neighbour.side
        common = {"ref": neighbour.ref, "from_side": favoured,
                  "escapable": [row[0] for row in world]}
        if len(world) == 1:
            pick, reason = world[0], SIDE_DEMAND_REASONS[1]
            why = (f"pad {pad} escapes on {pick[0]} and on no other side, so "
                   f"there is nothing to balance")
        elif favoured_load[favoured] <= 1:
            pick = next((row for row in world if row[0] == favoured),
                        world[0])
            reason = SIDE_DEMAND_REASONS[2]
            why = (f"{favoured} carries {favoured_load[favoured]} favoured "
                   f"neighbour(s); a side that is not crowded keeps its own")
        else:
            best = None
            for side, rank, cost, local in world:
                after = packed[side] + _ext(neighbour, side) + (
                    gap if loads[side] else 0.0)
                key = (round(after, 6), rank, cost, side)
                if best is None or key < best[0]:
                    best = (key, (side, rank, cost, local))
            pick, reason = best[1], SIDE_DEMAND_REASONS[3]
            why = (f"{favoured} is crowded ({favoured_load[favoured]} favoured "
                   f"neighbours); {pick[0]} minimises (packed-after "
                   f"{round(best[0][0], 4)} mm, access rank {pick[1]}, corridor "
                   f"{pick[2]} mm, side letter)")
        side = pick[0]
        chosen[neighbour.ref] = side
        packed[side] += _ext(neighbour, side) + (gap if loads[side] else 0.0)
        loads[side] += 1
        rows.append(dict(common, to_side=side, moved=bool(side != favoured),
                         reason=reason,
                         access=("FAVORED" if pick[1] == 0 else "ACCESSIBLE"),
                         cost=pick[2], why=why))

    rebuilt: dict = {side: [] for side in SIDES}
    for neighbour in every:
        side = chosen[neighbour.ref]
        if side == neighbour.side:
            rebuilt[side].append(neighbour)
            continue
        rebuilt[side].append(replace(
            neighbour, side=side, origin_side=neighbour.side,
            order_key=_reprojected_key(
                positions, neighbour.anchor_pad, side, anchor, anchor_geometry,
                int(anchor_rot_deg), escape_map.footprint, neighbour.ref,
                policy=overflow_order),
            reason=f"{neighbour.reason}; ESCAPE DEMAND -- moved from "
                   f"{neighbour.side} to {side} and re-projected onto the "
                   f"{side} edge"))
    out = {side: tuple(sorted(rebuilt[side], key=lambda n: n.order_key))
           for side in SIDES}
    return {"by_side": out,
            "rows": sorted(rows, key=lambda r: (r["to_side"], r["ref"])),
            "loads": loads, "packed": {s: round(packed[s], 6) for s in SIDES},
            "assignment": assignment, "edge_gap_mm": gap}


# --------------------------------------------------------------------------- #
# P-F -- the offset-rectangle ring
# --------------------------------------------------------------------------- #
def corner_owners(packed: dict, policy: str = "none") -> dict:
    """``{quadrant: owning side, "both" or None}``. ⛔ A total key over content.

    * ``"none"`` -- ``None`` everywhere: **both** adjacent sides may claim every
      quadrant. That is the ``face + 2R`` arithmetic plan section 2.4 sized R
      with, and its risk is now **measured** rather than assumed (two adjacent
      runs claim one quadrant on 2 of 4 subjects once P-E balances the ring).
    * ``"heavier"`` -- the adjacent side with the larger ``packed``, ties broken
      by the index in :data:`SIDES`. ⛔ Measured to **starve** the loser once
      all four sides are loaded: four runs, four quadrants, and the two heaviest
      take all of them.
    * ``"split"`` -- ``"both"``: the quadrant is **partitioned**, half to each
      adjacent side. Single-claimant by area, and nobody starves.
    """
    if policy not in CORNER_OWNER:
        raise ConstructError(f"corner_owner={policy!r} is not one of "
                             f"{CORNER_OWNER}")
    if policy == "none":
        return {quadrant: None for quadrant in QUADRANTS}
    if policy == "split":
        return {quadrant: "both" for quadrant in QUADRANTS}
    adjacent: dict = {quadrant: [] for quadrant in QUADRANTS}
    for side in SIDES:
        for quadrant, _other in _QUADRANT_ENDS[side]:
            adjacent[quadrant].append(side)
    return {quadrant: min(sorted(sides),
                          key=lambda s: (-round(float(packed.get(s, 0.0)), 6),
                                         SIDES.index(s)))
            for quadrant, sides in adjacent.items()}


def corner_share(policy: str, quadrant: str, side: str, owners: dict,
                 radius_mm: float, reserve_mm: float) -> float:
    """How much of ``quadrant`` ``side`` may use along the edge, at ``R``.

    ⛔ **One arithmetic, three policies, and it is the ONLY place the three
    differ.** A second derivation of a side's capacity is how the sizing pass
    and the slot pass would drift apart -- which this run already paid for once.
    """
    free = max(0.0, float(radius_mm) - float(reserve_mm))
    if policy == "none":
        return free
    if policy == "split":
        return free / 2.0
    return free if owners.get(quadrant) == side else 0.0


def ring_radius(by_side, *, geometries, anchor_geometry, fab,
                anchor_rot_deg: int = 0, anchor_x_mm: float = 0.0,
                anchor_y_mm: float = 0.0, edge_gap: str = "edge_gap",
                corner_owner: str = "none", rotations=None,
                spawn_factor: float = 1.0, r_max_factor: float = 4.0) -> dict:
    """⭐⭐⭐ **P-F's arithmetic. The ring is NOT a new geometry engine.**

    It is ``base`` in :func:`_position_for` -- today the constant 2.0048 mm --
    becoming a **per-side function of the side's own demand**::

        face(side)        = the anchor's COURTYARD extent along that side's edge
        packed(side)      = sum(neighbour courtyard extents along that edge)
                            + (n - 1) * arc_edge_gap
        capacity(side, R) = face + sum over OWNED quadrants of (R - reserve)
        R_fit(side)       = the smallest R with capacity >= packed, floored at
                            the fanout allowance

    ⛔ ``capacity = face + 2R`` exactly when both quadrants are available and the
    reserve is zero, which is :data:`CORNER_OWNER`'s ``"none"`` -- so plan
    section 2.4's measured table (``max R_fit`` 21.55 / 13.79 / 12.14 /
    26.91 mm at the shipped gap) is this function's output on that arm, and gate
    ``AC0`` reproduces it in code.

    ⚠ **A whole-perimeter figure is NOT the sizing rule and must not be used.**
    ``sum(parts) / perimeter`` gives 9.66 / 8.73 / 4.82 / 17.77 mm -- 2-4x
    smaller -- because it silently assumes parts redistribute freely around the
    ring, which the escape-side assignment forbids. **Sizing is per side.**

    ``rotations`` is ``{ref: rot_deg}``; a ref that is absent is measured at
    rotation 0. ⛔ Every number carries its source.
    """
    gap = arc_gap_mm(fab, edge_gap)
    reserve = (round(float(fab.clearance_mm), 6) if corner_owner != "none"
               else 0.0)
    allowance = standoff_base_mm(fab)
    #: ⛔⛔ **ONE anchor box, in the frame the slots are laid in, and it is
    #: recorded on the return value so no consumer derives a second one.** The
    #: first cut computed it at the origin here and at the anchor's world
    #: position in the placement loop; :func:`shrink_ring` then re-flowed every
    #: slot **25 mm** off (the anchor's own y) and reverted its first step as
    #: ``unroutable`` on 2 of 2 subjects -- a defect that reads exactly like the
    #: policy failing. *Two derivations of one number is how they drift apart*
    #: (standing findings 1 and 8, the same shape as the two strippers).
    anchor_box = _courtyard_box(anchor_geometry, "@A", float(anchor_x_mm),
                                float(anchor_y_mm), int(anchor_rot_deg))
    rotations = dict(rotations or {})

    raw: dict = {}
    for side in SIDES:
        _axis, _sign, along = _AXES[side]
        face = round(anchor_box[along + 2] - anchor_box[along], 6)
        extents = []
        for neighbour in by_side.get(side, ()):
            geometry = (geometries or {}).get(neighbour.ref)
            if geometry is None:
                raise ConstructError(
                    f"ring_radius cannot size {side!r} without geometry for "
                    f"{neighbour.ref!r} (standing finding 6)")
            extents.append((neighbour.ref,
                            _edge_extent(geometry, neighbour.ref, side,
                                         rotations.get(neighbour.ref, 0))))
        count = len(extents)
        packed = round(sum(value for _ref, value in extents)
                       + max(0, count - 1) * gap, 6)
        raw[side] = {"face_mm": face, "n": count, "packed_mm": packed,
                     "extents": [[ref, value] for ref, value in extents]}

    owners = corner_owners({s: raw[s]["packed_mm"] for s in SIDES},
                           corner_owner)
    sides: dict = {}
    for side in SIDES:
        row = dict(raw[side])
        shares = {quadrant: corner_share(corner_owner, quadrant, side, owners,
                                         1.0, 0.0)
                  for quadrant, _other in _QUADRANT_ENDS[side]}
        owned = [quadrant for quadrant in sorted(shares) if shares[quadrant]]
        total_share = round(sum(shares.values()), 6)
        need = row["packed_mm"] - row["face_mm"]
        if need <= _TOL:
            r_fit, feasible = allowance, True
            why = "the side's own face already holds its run"
        elif total_share <= _TOL:
            r_fit, feasible = allowance, False
            why = (f"{side} owns no corner quadrant, so capacity is its face "
                   f"({row['face_mm']} mm) alone and the run needs "
                   f"{round(row['packed_mm'], 4)} mm")
        else:
            r_fit = max(allowance,
                        round(need / total_share + reserve, 6))
            feasible = True
            why = (f"share {total_share} of the corner quadrants {owned} "
                   f"under {corner_owner!r}; R >= (packed - face) / share "
                   f"+ reserve")
        r_spawn = max(allowance, round(r_fit * float(spawn_factor), 6))
        capacity = round(row["face_mm"]
                         + sum(corner_share(corner_owner, quadrant, side,
                                            owners, r_spawn, reserve)
                               for quadrant, _o in _QUADRANT_ENDS[side]), 6)
        row.update({
            "side": side, "owns": owned,
            "shares": {q: round(v, 6) for q, v in sorted(shares.items())},
            "total_share": total_share, "reserve_mm": reserve,
            "R_fit_mm": round(r_fit, 6), "R_spawn_mm": r_spawn,
            "R_max_mm": round(max(allowance, r_fit * float(r_max_factor)), 6),
            "capacity_mm": capacity, "feasible": bool(feasible),
            "utilisation": (round(row["packed_mm"] / capacity, 6)
                            if capacity > _TOL else None),
            "why": why,
            "terms": [["face_mm", row["face_mm"],
                       "A's COURTYARD extent along that edge, MEASURED"],
                      ["arc_edge_gap_mm", gap,
                       f"arc_gap_mm(FabSpec, {edge_gap!r})"],
                      ["packed_mm", row["packed_mm"],
                       f"{row['n']} neighbour courtyard extent(s) + "
                       f"{max(0, row['n'] - 1)} x the gap"],
                      ["reserve_mm", reserve,
                       "one clearance at each SHARED run end, 0 under "
                       "corner_owner='none'"],
                      ["corner_share", total_share,
                       f"the fraction of its two quadrants {side} may use "
                       f"under corner_owner={corner_owner!r}"],
                      ["fanout_allowance_mm", allowance,
                       "standoff_base_mm(FabSpec) -- the floor"],
                      ["R_fit_mm", round(r_fit, 6), why],
                      ["spawn_factor", float(spawn_factor),
                       "declared, so 'recovered N mm' is attributable"],
                      ["R_spawn_mm", r_spawn,
                       "R_fit x spawn_factor, floored at the allowance"]]})
        sides[side] = row
    return {"sides": sides, "owners": owners, "corner_owner": corner_owner,
            "edge_gap": edge_gap, "edge_gap_mm": gap,
            "reserve_mm": reserve, "allowance_mm": allowance,
            "spawn_factor": float(spawn_factor),
            "anchor_box": [round(v, 6) for v in anchor_box],
            "max_R_fit_mm": round(max(sides[s]["R_fit_mm"] for s in SIDES), 6),
            "quadrants_declared": list(QUADRANTS),
            "infeasible": sorted(s for s in SIDES if not sides[s]["feasible"])}


def ring_slots(neighbours, *, side: str, radius_mm: float, anchor_box,
               geometries, gap_mm: float, owns=(), shares=None,
               reserve_mm: float = 0.0, rotations=None, r_max_mm=None) -> dict:
    """⭐⭐⭐ **P-F's placement, and the end of the unbounded cursor.**

    Returns, under ``"slots"``, ``{ref: the along-edge coordinate of the part's
    COURTYARD low edge}`` -- **centred on the anchor's centre line** and
    **bounded** by ``capacity(side, R)``. ⛔ A run that will not fit is a
    **named failure**, never a silent advance, which is precisely what open
    question 14's open half is about.

    ⛔ The order is P2's, unchanged: the neighbours are laid consecutively in the
    order they arrive, so *monotone along the edge* holds **by construction**
    rather than by a repair pass, and P-A's centring becomes a property of the
    arithmetic rather than a transaction that might revert.
    """
    _axis, _sign, along = _AXES[side]
    rotations = dict(rotations or {})
    members = list(neighbours)
    (lo_quadrant, _a), (hi_quadrant, _b) = _QUADRANT_ENDS[side]
    #: ⛔ ONE arithmetic for the three corner policies, and it lives in
    #: :func:`corner_share`. ``shares`` is what :func:`ring_radius` sized this
    #: side against; ``owns`` is the older all-or-nothing form and is kept so a
    #: caller with a plain list still gets the ``heavier``/``none`` behaviour.
    share = dict(shares) if shares else {q: 1.0 for q in owns}
    free = max(0.0, radius_mm - reserve_mm)
    lo_spill = round(float(share.get(lo_quadrant, 0.0)) * free, 6)
    hi_spill = round(float(share.get(hi_quadrant, 0.0)) * free, 6)
    lo_bound = round(anchor_box[along] - lo_spill, 6)
    hi_bound = round(anchor_box[along + 2] + hi_spill, 6)
    capacity = round(hi_bound - lo_bound, 6)
    centre = round((anchor_box[along] + anchor_box[along + 2]) / 2.0, 6)
    extents = []
    for neighbour in members:
        geometry = (geometries or {}).get(neighbour.ref)
        if geometry is None:
            raise ConstructError(
                f"ring_slots cannot place {neighbour.ref!r} without its "
                f"footprint geometry (standing finding 6)")
        extents.append(_edge_extent(geometry, neighbour.ref, side,
                                    rotations.get(neighbour.ref, 0)))
    total = round(sum(extents) + max(0, len(extents) - 1) * gap_mm, 6)
    out = {"side": side, "n": len(members), "radius_mm": round(radius_mm, 6),
           "lo": lo_bound, "hi": hi_bound, "capacity_mm": capacity,
           "centre_mm": centre, "total_mm": total, "gap_mm": round(gap_mm, 6),
           "owns": sorted(owns), "reserve_mm": round(reserve_mm, 6),
           "shares": {q: round(float(v), 6) for q, v in sorted(share.items())},
           "slots": {}, "quadrant_use": {lo_quadrant: [], hi_quadrant: []},
           "fits": True, "clamped": False, "why": ""}
    if not members:
        out["why"] = "no neighbour on this side"
        return out
    if total > capacity + _TOL:
        out["fits"] = False
        out["why"] = (
            f"the run needs {total} mm and capacity(side={side}, "
            f"R={round(radius_mm, 4)}) is {capacity} mm (face "
            f"{round(anchor_box[along + 2] - anchor_box[along], 4)} mm + owned "
            f"quadrants {sorted(owns)}); a slot that will not fit is a FAILURE "
            f"with a named reason, never a silent advance"
            + (f". R_max is {r_max_mm} mm" if r_max_mm is not None else ""))
        return out
    wanted = round(centre - total / 2.0, 6)
    # ⛔ Clamped inside the bound, low end first. The interval is wide enough
    # (the fit test above), so clamping can never push the run out of the far
    # end -- and a clamp that silently did would be the cursor's defect again.
    # ⛔⛔ **And when it DOES clamp, the run is no longer centred on the anchor,
    # which is plan section 8's assertion 3 meeting an asymmetric interval.**
    # That happens exactly when the two quadrant shares differ (``heavier``), it
    # is *recorded* rather than asserted away, and the run is still bounded --
    # which is the property open question 14's open half is actually about.
    start = round(min(max(wanted, lo_bound), hi_bound - total), 6)
    out["clamped"] = bool(abs(start - wanted) > _TOL)
    out["clamped_by_mm"] = round(start - wanted, 6)
    cursor = start
    for neighbour, extent in zip(members, extents):
        out["slots"][neighbour.ref] = round(cursor, 6)
        if cursor < anchor_box[along] - _TOL:
            out["quadrant_use"][lo_quadrant].append(neighbour.ref)
        if cursor + extent > anchor_box[along + 2] + _TOL:
            out["quadrant_use"][hi_quadrant].append(neighbour.ref)
        cursor = round(cursor + extent + gap_mm, 6)
    out["why"] = (f"{len(members)} slot(s) laid in P2 order over {total} mm, "
                  f"centred on {centre} mm inside [{lo_bound}, {hi_bound}]")
    return out


# --------------------------------------------------------------------------- #
# P-G -- L0, the template unit. ⭐ Section 3's never-built level.
# --------------------------------------------------------------------------- #
class _SingleGroupPartition:
    """⛔ A one-group **view** of a :class:`Partition` -- exactly the surface
    :func:`construct_cell` reads, and nothing else.

    ⭐ It exists so that L0 runs **the same loop**: ``construct_cell`` inventories
    *other* family groups as ``template`` neighbours (P1 says *"every part **and
    cell** that connects to A"*), which is right at L1 and wrong at L0 -- a
    two-part divider should not pull the snubber in. Restricting the view is one
    class; a second loop would be a second thing to keep true.

    ⚠ **Not a subclass and not a copy of the dataclass**: the three members read
    are ``group_of``, ``groups`` and ``meta``, and naming exactly those is what
    makes the restriction auditable.
    """

    __slots__ = ("groups", "meta", "_group")

    def __init__(self, group, meta):
        self._group = group
        self.groups = (group,)
        self.meta = dict(meta or {})

    def group_of(self, ref):
        return self._group if str(ref) in set(self._group.refs) else None


def l0_anchor(group, *, footprints, geometries, nets_by_ref) -> tuple:
    """⭐ **The L0 anchor: the member with the most pads on the family's own
    internal nets**, ties by courtyard area (larger first), then by ref.

    ⛔ *"The family's shared net"* is derived rather than declared: a net is
    **internal** when two or more members of the group carry it. On a junction
    that is the junction net; on a chain it is the link. ⚠ 19 of the corpus's 20
    family groups have exactly **two** members, so this rule is a tie-break far
    more often than it is a choice -- which is why the tie-break is stated and is
    exercised by a unit test rather than by luck.

    ⛔ A total key over content, never arrival order (standing finding 8).
    Returns ``(ref, why)``.
    """
    refs = sorted(group.refs)
    if not refs:
        raise ConstructError(
            f"the group {group.name!r} has no member -- an L0 anchor over "
            f"nothing is indistinguishable from one that found everything")
    carried: dict = {}
    for ref in refs:
        for net in set(nets_by_ref.get(ref, ())):
            carried.setdefault(net, set()).add(ref)
    internal = {net for net, holders in carried.items() if len(holders) >= 2}
    scored = []
    for ref in refs:
        pads = _anchor_pads_by_net(list(footprints[ref].pads))
        count = sum(len(pads[net]) for net in pads if net in internal)
        geometry = geometries.get(ref)
        box = (_courtyard_box(geometry, ref, 0.0, 0.0, 0)
               if geometry is not None else (0.0, 0.0, 0.0, 0.0))
        area = round((box[2] - box[0]) * (box[3] - box[1]), 6)
        scored.append((-count, -area, ref))
    best = min(scored)
    return (best[2],
            f"{best[2]} carries {-best[0]} pad(s) on the group's internal "
            f"net(s) {sorted(internal)} and a courtyard of {-best[1]} mm^2; "
            f"ties break on area then ref (candidates "
            f"{[(r, -c, -a) for c, a, r in sorted(scored)]})")


def construct_template(partition, group_name: str, *, session, geometries,
                       fab, escape_maps, board: str = "", nets_by_ref=None,
                       anchor_x_mm=None, anchor_y_mm=None,
                       anchor_rot_deg: int = 0, **cell_kwargs) -> CellResult:
    """⭐⭐⭐ **P-G. The SAME loop at L0** -- overview section 3's third level,
    which S1-S5B left unbuilt.

    A template is **flattened** today (P6): its anchor-facing member takes the
    side slot and its partner follows in ring 2, so a template has **no ports and
    no arrangement**. This runs :func:`construct_cell` on the family group
    itself, with :func:`l0_anchor` as the anchor, so
    :func:`unit_from_cell` + :func:`unit_ports` (S5B, **unchanged**) can turn the
    result into a placeable :class:`Unit` with real ports -- and *"are the
    accessible sides of that arrangement still reachable?"* becomes a question
    the stack can answer.

    ⛔⛔ **The arrangement comes from the LOOP, not from a cache**, and that is a
    measurement rather than a preference: ``family_cache_junction`` holds **7
    three-part junction winners** while the corpus offers **one** three-member
    family, so consuming it as the source would bind on at most 1 of 20
    instances -- standing finding 20's twelfth instance, pre-empted. The cache
    and ``ideal_arrangements.py`` are the **answer key**: agreement is reported,
    never consumed.

    ⛔ **The anchor stays where the parsed board has it** (standing finding 21),
    so ``anchor_x_mm``/``anchor_y_mm`` default to the session's own footprint
    position rather than to an invented origin.
    """
    group = next((g for g in partition.groups if g.name == group_name), None)
    if group is None:
        raise ConstructError(
            f"no group named {group_name!r} in this partition -- an L0 cell "
            f"built over nothing is indistinguishable from one that found "
            f"everything")
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    footprints = {ref: session.pcb.footprints.get(ref) for ref in group.refs}
    missing = sorted(ref for ref, fp in footprints.items()
                     if fp is None or ref not in geometries)
    if missing:
        raise ConstructError(
            f"{group_name}: {missing} have no geometry or are not on the "
            f"session's board (standing finding 6) -- named, never dropped")
    anchor, why = l0_anchor(group, footprints=footprints,
                            geometries=geometries, nets_by_ref=nets_by_ref)
    parsed = footprints[anchor]
    at_x = float(parsed.x) if anchor_x_mm is None else float(anchor_x_mm)
    at_y = float(parsed.y) if anchor_y_mm is None else float(anchor_y_mm)
    view = _SingleGroupPartition(group, dict(partition.meta))
    result = construct_cell(
        view, anchor, session=session, geometries=geometries, fab=fab,
        escape_map=_map_of(escape_maps, geometries[anchor].footprint),
        escape_maps=escape_maps, board=board or str(partition.meta.get("board",
                                                                       "")),
        anchor_x_mm=at_x, anchor_y_mm=at_y, anchor_rot_deg=int(anchor_rot_deg),
        nets_by_ref=nets_by_ref, **cell_kwargs)
    result.meta["l0"] = {"group": group_name, "kind": group.kind,
                         "family": group.family, "topology": group.topology,
                         "members": list(sorted(group.refs)),
                         "anchor": anchor, "anchor_why": why,
                         "anchor_x_mm": round(at_x, 6),
                         "anchor_y_mm": round(at_y, 6),
                         "anchor_rot_deg": int(anchor_rot_deg)}
    return result


# --------------------------------------------------------------------------- #
# P-H -- the ring shrink
# --------------------------------------------------------------------------- #
def _dependents(state, ref: str) -> list:
    """Every entry sub-anchored on ``ref``, transitively. ⛔ Sorted.

    ⭐ A ring-2 member's position is *relative to its sub-anchor* by
    construction, so a shrink that moved the sub-anchor and left the child
    behind would break the one relationship ring 2 exists to express -- and it
    would show up as a collision, which reads like the ring's fault.
    """
    out, frontier = [], [ref]
    while frontier:
        parent = frontier.pop()
        for other in sorted(state.entries):
            entry = state.entries[other]
            if other != parent and entry.get("subanchor") == parent \
                    and other not in out and other != ref:
                out.append(other)
                frontier.append(other)
    return sorted(out)


def shrink_ring(session, *, state, order, laid, radius, pairs, anchor,
                anchor_x_mm: float, anchor_y_mm: float, anchor_extents,
                geometries, clearance_mm: float, step_mm: float,
                route: bool = True, max_steps: int = 200) -> list:
    """⭐⭐⭐ **P-H. Shrink the RADIUS, not the parts.**

    ⛔ **Not the parts.** Per-part inward stepping is exactly what
    :func:`_tighten_cell` does, and the renders are what it produces: its floor
    is ``standoff - allowance`` (``construct.py``'s tighten), so it spends the
    **whole** fanout allowance by construction -- *it deletes the one term P4
    exists to create* -- and at ring 2 it **chains** (standing finding 28:
    4.000 mm and 5.500 mm recovered against a 2.0048 mm allowance).

    ⭐ Shrinking **R** keeps the side's face gap uniform, keeps P2's order, and
    has an arithmetic floor no later pass can move:
    ``max(fanout_allowance, R_fit(side))``. ⛔ The floors are read **before** the
    pass begins and never re-derived from a reference the pass has moved
    (standing finding 28, and standing finding 23 is why the floor is not a
    legality check).

    ⛔ **Transactional, one step at a time**: a step that breaks either box's
    legality, or that stops **any** pair of the cell routing, is reverted whole
    and the side stops with a named reason from :data:`RING_STOP_REASONS`.

    Returns one row per side, in P3 order.
    """
    allowance = float(radius["allowance_mm"])
    gap = float(radius["edge_gap_mm"])
    anchor_box = tuple(radius["anchor_box"])
    #: ⛔ Read BEFORE the pass, so no floor can inherit a motion this pass made.
    floors = {side: max(allowance, float(radius["sides"][side]["R_fit_mm"]))
              for side in SIDES}
    rows: list = []
    for side in order:
        members = [m for m in laid.get(side, ()) if m.ref in state.entries]
        row = {"side": side, "n": len(members),
               "R_before_mm": round(float(radius["sides"][side]["R_spawn_mm"]),
                                    6),
               "R_after_mm": round(float(radius["sides"][side]["R_spawn_mm"]),
                                   6),
               "floor_mm": round(floors[side], 6),
               "R_fit_mm": round(float(radius["sides"][side]["R_fit_mm"]), 6),
               "steps_accepted": 0, "steps_tried": 0, "recovered_mm": 0.0,
               "stopped_by": "floor", "why": "", "moved": []}
        if not members:
            row["why"] = "no placement on this side"
            row["stopped_by"] = "floor"
            rows.append(row)
            continue
        axis, sign, along = _AXES[side]
        anchor_perp = anchor_x_mm if axis == 0 else anchor_y_mm
        current = float(radius["sides"][side]["R_spawn_mm"])
        owns = radius["sides"][side]["owns"]
        shares = radius["sides"][side].get("shares")
        reserve = float(radius["reserve_mm"])
        rotations = {m.ref: state.entries[m.ref]["rot_deg"] for m in members}
        accepted, tried, stopped, why = 0, 0, "cap", ""
        while tried < max_steps:
            if current <= floors[side] + _TOL:
                stopped = "floor"
                why = (f"R reached the arithmetic floor "
                       f"max(allowance {round(allowance, 4)}, R_fit "
                       f"{round(float(radius['sides'][side]['R_fit_mm']), 4)}) "
                       f"= {round(floors[side], 4)} mm")
                break
            tried += 1
            nxt = max(floors[side], round(current - step_mm, 6))
            plan = ring_slots(members, side=side, radius_mm=nxt,
                              anchor_box=anchor_box, geometries=geometries,
                              gap_mm=gap, owns=owns, shares=shares,
                              reserve_mm=reserve, rotations=rotations)
            if not plan["fits"]:
                stopped = "fit"
                why = plan["why"]
                break
            moving: dict = {}
            for member in members:
                entry = state.entries[member.ref]
                geometry = entry["geometry"]
                rot = entry["rot_deg"]
                facing = _facing_extent(geometry, member.ref, side, rot)
                extra = float(entry.get("ring_extra_mm", 0.0) or 0.0)
                standoff = (float(anchor_extents[side]) + nxt + facing + extra)
                perp = anchor_perp + sign * standoff
                local = _courtyard_box(geometry, member.ref, 0.0, 0.0, rot)
                value = round(plan["slots"][member.ref] - local[along], 6)
                target = ((perp, value) if axis == 0 else (value, perp))
                delta = (round(target[0] - entry["x_mm"], 6),
                         round(target[1] - entry["y_mm"], 6))
                moving[member.ref] = (round(target[0], 6), round(target[1], 6),
                                      standoff)
                for child in _dependents(state, member.ref):
                    kid = state.entries[child]
                    moving[child] = (round(kid["x_mm"] + delta[0], 6),
                                     round(kid["y_mm"] + delta[1], 6),
                                     kid["standoff_mm"])
            previous = {ref: (state.entries[ref]["x_mm"],
                              state.entries[ref]["y_mm"],
                              state.entries[ref]["standoff_mm"],
                              state.entries[ref]["token"]) for ref in moving}

            def _apply(positions):
                for ref in sorted(positions):
                    entry = state.entries[ref]
                    if entry["token"] is not None:
                        session.remove_part(entry["token"])
                        entry["token"] = None
                for ref in sorted(positions):
                    entry = state.entries[ref]
                    x_mm, y_mm, standoff = positions[ref]
                    entry["x_mm"], entry["y_mm"] = x_mm, y_mm
                    entry["standoff_mm"] = standoff
                    state.refresh(ref)
                    if previous[ref][3] is not None:
                        entry["token"] = session.add_part(f"@N:{ref}",
                                                          state.pads(ref))

            _apply(moving)
            clash = _illegal_pairs(state, clearance_mm)
            routed_ok, failed = True, None
            if not clash and route and pairs:
                routed_ok, failed, _lengths = _reroute_all(session, pairs,
                                                           state)
            if clash or not routed_ok:
                _apply({ref: (previous[ref][0], previous[ref][1],
                              previous[ref][2]) for ref in moving})
                stopped = "collision" if clash else "unroutable"
                why = (f"the step to R={nxt} mm collides: {clash[:3]}"
                       if clash else
                       f"the step to R={nxt} mm left "
                       f"{failed['ref'] if failed else 'a'} pair unroutable; "
                       f"reverted whole")
                break
            row["moved"] = sorted(moving)
            accepted += 1
            current = nxt
        else:
            stopped = "cap"
            why = f"the bounded budget of {max_steps} step(s) ran out"
        row.update({"R_after_mm": round(current, 6), "steps_accepted": accepted,
                    "steps_tried": tried,
                    "recovered_mm": round(row["R_before_mm"] - current, 6),
                    "stopped_by": stopped,
                    "why": why or f"stopped by {stopped}"})
        if stopped not in RING_STOP_REASONS:
            raise ConstructError(
                f"the ring shrink stopped {side!r} for the undeclared reason "
                f"{stopped!r} -- every side must end in exactly one of "
                f"{RING_STOP_REASONS} (rule 3)")
        if current < allowance - _TOL:
            raise ConstructError(
                f"{side}: R ended at {current} mm, below the fanout allowance "
                f"{allowance} mm -- that is standing finding 23 recommitted "
                f"(bail-out 8)")
        rows.append(row)
    return rows


# =========================================================================== #
# S5B -- L2, THE BOARD. ⛔ The unit model and the L2 loop.
#
# ⭐⭐⭐ **The [CALL] overview section 3 leaves open is "is L2 really the same
# loop?", and the answer this section gives is "yes, and it is the SAME
# FUNCTION".** Everything below builds a :class:`Unit` that presents exactly the
# surface :func:`_place_one`, :class:`_CellState`, :func:`_tighten_cell`,
# :func:`_centre_sides`, :func:`_reroute_all`, :func:`pair_crossings` and
# :func:`side_span` already consume -- a
# :class:`~skidl_layout.geometry.FootprintGeometry` and a pad list -- so **not
# one line of the L1 ladder, standoff formula, tighten or centring pass is
# re-written for L2.** ⛔ That is a claim the gates check rather than a
# statement: gate ``BD3`` asserts the placement came out of ``_place_one``.
#
# ⭐ It is the ``fp_geometries`` masquerade the record already priced at "one
# engine line", used a second time and for the same reason: a composite object
# that answers the geometry questions is cheaper and safer than a parallel loop
# that answers them again.
# =========================================================================== #

#: ⛔ Where a unit's geometry came from. **Declared, and the driver asserts this
#: set plus the named "handled elsewhere" set PARTITIONS what
#: :func:`board_units` emits** (standing finding 20, eleventh opportunity).
#:
#: * ``constructed`` -- :func:`construct_cell` ran on the group's own anchor.
#: * ``footprint`` -- a 1-member group: its own box and S1's escape map.
#: * ``flattened`` -- a group with no anchor (**every** ``family`` group on this
#:   corpus has ``anchor = None``): its members enter L2 **individually**,
#:   keeping the group name for priority and ordering. ⛔⛔ Not a rigid cell --
#:   *rigidity is a refuted mechanism* (overview section 2, measured: cells
#:   harvested off the AUTO board score **worse** than no cells) and S4 reached
#:   the same answer from the other side.
UNIT_SOURCE: tuple[str, ...] = ("constructed", "footprint", "flattened")

#: ⛔ The three readings of *"what is the L2 anchor?"* (overview open question 4).
#: ⭐ ``largest_cell`` is the implemented default and the other two are
#: **measured every run and reported, never asserted** -- the procedure that
#: would have caught three of standing finding 20's last four instances.
L2_ANCHOR_RULES: tuple[str, ...] = ("largest_cell", "most_ports", "connector")

#: ⛔ The two readings of *"which side of a unit does a net leave from?"*.
#:
#: * ``member_escape`` (default) -- the side the owning **member's** pad escapes
#:   toward (:func:`~skidl_layout.escape_map.favored_side`), rotated into unit
#:   coordinates, **re-decided against the unit box when the member sits in the
#:   box's interior**. A member that is not on the unit's own perimeter cannot
#:   hand its escape side to the unit: the escape leaves the *member* on that
#:   side and then meets the rest of the unit.
#: * ``box_occupancy`` -- :func:`~skidl_layout.escape_map.pad_occupancy_side` of
#:   the pad against the **unit's** physical box, ignoring the member's own
#:   footprint entirely. The cheaper reading; ships measured.
L2_PORT_SIDES: tuple[str, ...] = ("member_escape", "box_occupancy")

#: Which of the two rules actually decided one port's side. ⛔ Recorded per port
#: so *"how often does the interior re-decision fire"* is a number rather than a
#: belief.
PORT_SIDE_SOURCES: tuple[str, ...] = ("member_escape", "member_escape_interior",
                                      "box_occupancy", "no_escape")

#: ⛔ The two readings of P2's ordering rule at L2. ``port_edge`` is P2 itself --
#: the projection of the anchor **unit's** port order onto the edge.
#: ``unit_name`` is the deliberately content-blind control, kept so that
#: *"the geometric order buys something"* is measured rather than assumed.
L2_SIDE_ORDERS: tuple[str, ...] = ("port_edge", "unit_name")

#: ⛔⛔ **The one place this arc departs from overview section 10**, and the
#: departure is recorded loudly rather than slipped in. Section 10 says
#: connectors *"arrive as fixed positions the loop must respect"*; **in this
#: corpus there is no source for such a position** -- the boards are synthesised
#: from netlists, with no authored outline and no mechanical constraint.
#:
#: * ``loop_placed`` (default) -- a connector is an ordinary unit.
#: * ``hand_fixed`` -- the connector's position is read off the **hand** board.
#:   ⛔ **BUILT, MEASURED AND EXPLICITLY REFUSED AS THE DEFAULT**: those
#:   positions are part of the answer key S6 will grade against, and importing
#:   them would contaminate the comparison this whole arc exists to make. The
#:   only thing reported is **how far the loop's connectors land from the
#:   human's** -- ⛔ a distance, never a score.
CONNECTOR_POLICIES: tuple[str, ...] = ("loop_placed", "hand_fixed")

#: ⛔ Why a unit was not placed on a side list. Declared as data so a reason
#: that never occurs is reported (standing finding 20).
L2_SKIP_REASONS: tuple[str, ...] = ("ring2", "no_shared_net", "no_port_on_net",
                                    "no_geometry", "anchor")


@dataclass(frozen=True)
class UnitPort:
    """One net leaving one unit, through one member's pad.

    ⭐ **The same vocabulary as** :class:`~skidl_layout.cells.CellPort` **and
    S1's** :class:`~skidl_layout.escape_map.PadEscape` -- ``side`` is one of
    :data:`SIDES` and ``access`` one of ``FAVORED``/``ACCESSIBLE``/``BLOCKED``,
    so the L1 loop and the L2 loop consume one concept (overview section 4.2).

    ``x_mm``/``y_mm`` are **unit-local**: the pad's position relative to the
    unit's origin at rotation 0. ⛔ Never world coordinates -- a unit is placed
    many times and a port that carried a world position would be stale the
    moment it moved.
    """

    net: str
    side: str
    x_mm: float
    y_mm: float
    member: str               # the member whose pad this is
    pad: str                  # that member's own pad number
    access: str               # S1's vocabulary
    cost: float               # the corridor distance the access was decided on
    #: ⛔ Which of :data:`PORT_SIDE_SOURCES` decided :attr:`side`.
    source: str = "member_escape"
    #: ``"{member}.{pad}"`` -- the composite number the unit's stamped pad
    #: carries. ⭐ **The representative-pair key** (see :func:`unit_ports`).
    number: str = ""

    @property
    def key(self) -> tuple:
        """⛔ A total key over content. ⭐ **Deliberately** ``_pad_key`` **of the
        composite number**, because that is what :func:`_route_pair` and
        :func:`_connecting_pad_offsets` already sort by -- see the note in
        :func:`unit_ports`."""
        return (_pad_key(self.number), self.net, self.side)

    def to_dict(self) -> dict:
        return {"net": self.net, "side": self.side, "x_mm": self.x_mm,
                "y_mm": self.y_mm, "member": self.member, "pad": self.pad,
                "access": self.access, "cost": self.cost,
                "source": self.source, "number": self.number}


@dataclass(frozen=True)
class Unit:
    """⭐ **What L2 places.** One or more parts, one box, a set of ports.

    ⛔ ``physical_box`` and ``courtyard_box`` are **different and both are
    carried** (standing finding 13): escape and rotation reasoning runs on
    ``body ∪ pads``; standoff, collision and spans run on the courtyard.

    ⛔⛔ **This is NOT** :class:`~skidl_layout.cells.LayoutCell`. That type
    answers a different question about a *cached* cell, its caches carry a
    ``gap1.00`` transit contract this arc does not share, and 20 of 54
    ``family_cache_routed`` cells carry a **severed** net. Nothing here reaches
    for ``cells_families`` or ``family_cache``.
    """

    name: str                 # the CellGroup name -- the total content key
    kind: str                 # CellGroup.kind
    refs: tuple               # every member, sorted
    anchor: str | None
    #: ``{ref: (dx_mm, dy_mm, rot_deg)}`` relative to the unit origin, at unit
    #: rotation 0.
    offsets: dict
    physical_box: tuple       # unit-local, body ∪ pads
    courtyard_box: tuple      # unit-local, the LARGER box
    ports: tuple              # UnitPort, sorted by UnitPort.key
    source: str               # one of UNIT_SOURCE
    group: str = ""           # the CellGroup this came from (a flattened
                              # member keeps its group's name for priority)
    reason: str = ""
    meta: dict = None

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})

    @property
    def nets(self) -> tuple:
        return tuple(sorted({port.net for port in self.ports}))

    def ports_on(self, net: str) -> tuple:
        return tuple(port for port in self.ports if port.net == net)

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "refs": list(self.refs),
                "anchor": self.anchor, "source": self.source,
                "group": self.group,
                "offsets": {ref: list(value)
                            for ref, value in sorted(self.offsets.items())},
                "physical_box": [round(v, 6) for v in self.physical_box],
                "courtyard_box": [round(v, 6) for v in self.courtyard_box],
                "ports": [port.to_dict() for port in self.ports],
                "nets": list(self.nets), "reason": self.reason,
                "meta": self.meta}


@dataclass(frozen=True)
class StrangerCrossing:
    """⭐⭐⭐ **The stage's headline: one unit's wire in another's channel.**

    Overview open question 5 is answered *"the tighten recovers the whole
    allowance and costs routability nothing"* **for a cell under its own
    pressure only** -- S4's measurement was taken where no wire but the cell's
    own pairs existed. This is the first instrument in the arc that can see a
    **stranger**.

    ⚠⚠ **AND THE ONE THING IT IS NOT, STATED IN THE TYPE RATHER THAN IN A
    FOOTNOTE: this is the PAIR LINE, not the copper.**
    :class:`~skidl_layout.route_session.PairResult` carries no path and
    inventing one is forbidden, so ``overlap_mm`` is how far a straight
    pad-to-pad segment reaches into the allowance band. ⛔ A real router jogs, so
    this is a **proxy that can over-report and under-report**, and every number
    it produces must be quoted as *"the pair line enters the channel"*, never as
    *"copper is in the channel"*.
    """

    channel_of: str           # the unit whose fanout allowance this is
    side: str                 # which side of it
    intruder: str             # the unit whose routed pair enters the channel
    net: str
    overlap_mm: float         # how deep into the allowance the line reaches
    still_routed: bool        # did the channel's OWN pairs survive
    allowance_mm: float = 0.0
    band: tuple = ()          # the channel rectangle, world mm
    phase: str = ""           # "before" | "after" the tighten

    def to_dict(self) -> dict:
        return {"channel_of": self.channel_of, "side": self.side,
                "intruder": self.intruder, "net": self.net,
                "overlap_mm": self.overlap_mm,
                "still_routed": self.still_routed,
                "allowance_mm": self.allowance_mm,
                "band": [round(v, 6) for v in self.band], "phase": self.phase}


@dataclass(frozen=True)
class BoardResult:
    """⭐ The S5B deliverable. ⛔ Mirrors :class:`CellResult` deliberately, so the
    driver's scene/measure/digest helpers take it with a ~30-line adapter rather
    than a second renderer (roadmap next-steps item 10: the renderer is already
    duplicated twice; **do not make a third copy**).

    ⛔ The accounting is **TOTAL**: every unit ends in exactly one of *placed on
    a side*, *placed in ring 2*, *failed*, or *skipped with a named reason*.
    """

    board: str
    anchor: str
    sides: tuple                      # SideResult over units, in P3 order
    ring2: tuple                      # Placement
    ring2_failures: tuple
    tighten: tuple                    # TightenStep
    skipped: tuple
    strangers: tuple = ()             # StrangerCrossing
    units: tuple = ()                 # Unit
    meta: dict = None

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})

    @property
    def placements(self) -> tuple:
        out: list = []
        for side in self.sides:
            out.extend(side.placements)
        out.extend(self.ring2)
        return tuple(out)

    @property
    def routed_fraction(self) -> float:
        placements = self.placements
        if not placements:
            return 0.0
        return round(sum(1 for p in placements if p.routed)
                     / len(placements), 4)

    @property
    def legal(self) -> bool:
        """⛔ BOTH boxes, over the WHOLE BOARD -- not per side and not per unit."""
        return (not self.meta.get("physical_overlaps")
                and not self.meta.get("courtyard_overlaps"))


def board_result_to_dict(result: BoardResult) -> dict:
    """A JSON-serialisable, order-stable view of a whole board.

    ⚠ Wall clock is on the artifact and is excluded from the determinism digest
    **by the caller**, through the **same stripper** S3/S4/S5 use. Two strippers
    is how *"identical"* stops meaning identical.
    """
    return {
        "board": result.board,
        "anchor": result.anchor,
        "routed_fraction": result.routed_fraction,
        "legal": result.legal,
        "sides": [side_result_to_dict(side) for side in result.sides],
        "ring2": [p.to_dict() for p in result.ring2],
        "ring2_failures": list(result.ring2_failures),
        "tighten": [step.to_dict() for step in result.tighten],
        "skipped": list(result.skipped),
        "strangers": [s.to_dict() for s in result.strangers],
        "units": [u.to_dict() for u in result.units],
        "meta": result.meta,
    }


# --------------------------------------------------------------------------- #
# The unit's geometry -- ⭐ a real FootprintGeometry, not a masquerade class
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _UnitFootprint:
    """The parsed-footprint surface :func:`_place_one` actually consumes.

    ⛔ Exactly two attributes, because that is exactly what
    :func:`moved_pads` and :func:`_connecting_pad_offsets` read: ``pads`` (whose
    ``local_x``/``local_y`` are **unit-local**, so a later ``moved_pads`` at the
    unit's own placement is a single absolute transform rather than a chain of
    deltas) and ``rotation`` (**0.0**, because the member rotations are already
    baked into those pads).
    """

    pads: tuple
    rotation: float = 0.0


def _unit_pad_number(ref: str, number) -> str:
    """``"C1.2"`` -- one member's pad, addressable inside a composite unit.

    ⛔ Two members of one unit routinely share a pad number, so the composite
    number is what makes :func:`_pair_target`'s lookup and the failure log
    unambiguous. ⚠ It is deliberately **not** ``(member, pad)`` as a tuple:
    ``_pad_key`` is the arc's one pad ordering rule and it takes a string.
    """
    return f"{ref}.{number}"


def _member_world_pads(ref, footprint, offset):
    """One member's parsed pads, moved to their **unit-local** position.

    ⛔ ``local_x``/``local_y`` are rewritten to the unit-local position on
    purpose. :func:`moved_pads` transforms from ``local_*``, so a unit whose pads
    still carried their *footprint*-frame locals would place every member on top
    of the unit origin -- silently, and with a perfectly legal-looking box.
    """
    dx, dy, rot = offset
    moved = moved_pads(list(footprint.pads), x_mm=float(dx), y_mm=float(dy),
                       rot_deg=float(rot), base_rot_deg=footprint.rotation)
    return [replace(pad, local_x=pad.global_x, local_y=pad.global_y,
                    pad_number=_unit_pad_number(ref, pad.pad_number))
            for pad in moved]


def unit_box(offsets: dict, geometries: dict) -> tuple:
    """``(physical, courtyard)`` for a unit, in unit-local mm.

    ⛔ **Both boxes, and they are different** (standing finding 13): the
    courtyard overhang is 0.175 mm/side at 0402 and 0.275-0.280 mm at 0603/0805,
    so a standoff computed off the physical box is short by exactly that *and
    every test still passes at 0805*.
    """
    phys, court = [], []
    for ref, (dx, dy, rot) in sorted(offsets.items()):
        geometry = geometries.get(ref)
        if geometry is None:
            raise ConstructError(
                f"no footprint geometry for {ref!r} -- a unit box built over a "
                f"2 x 2 mm fallback is self-consistent and wrong (standing "
                f"finding 6)")
        phys.append(_physical_box(geometry, ref, dx, dy, rot))
        court.append(_courtyard_box(geometry, ref, dx, dy, rot))
    if not phys:
        raise ConstructError(
            "unit_box was handed no member -- an instrument that can observe "
            "nothing must raise (rule 8)")
    def _union(boxes):
        return (round(min(b[0] for b in boxes), 6),
                round(min(b[1] for b in boxes), 6),
                round(max(b[2] for b in boxes), 6),
                round(max(b[3] for b in boxes), 6))
    return _union(phys), _union(court)


def _unit_geometry(name: str, offsets: dict, geometries: dict, boxes: tuple):
    """⭐ A real :class:`~skidl_layout.geometry.FootprintGeometry` for the unit.

    ⛔ ``courtyard_bounds`` is the union of the members' **courtyard** boxes and
    ``body_bounds`` the union of their **physical** ones, so
    ``FootprintGeometry.bounds`` (which *prefers* the courtyard) and
    ``physical_bounds`` (body ∪ pads) both come out right without a single
    override. ⭐ Rotating a union AABB by a multiple of 90 deg is **exact**, so
    ``_transform_bounds`` is not an approximation here.
    """
    from .geometry import FootprintGeometry, PadGeometry, transform_point

    pads = []
    for ref, (dx, dy, rot) in sorted(offsets.items()):
        for pad in geometries[ref].pads:
            x, y = transform_point(float(dx), float(dy), float(rot),
                                   pad.x_mm, pad.y_mm)
            swap = abs((float(rot) % 180.0) - 90.0) < 1e-6
            pads.append(replace(
                pad, number=_unit_pad_number(ref, pad.number),
                x_mm=round(x, 6), y_mm=round(y, 6),
                width_mm=pad.height_mm if swap else pad.width_mm,
                height_mm=pad.width_mm if swap else pad.height_mm))
    assert PadGeometry  # the type these are, kept honest
    physical, courtyard = boxes
    return FootprintGeometry(footprint=name, pads=pads,
                             body_bounds=physical, courtyard_bounds=courtyard)


def unit_ports(offsets: dict, geometries: dict, escape_maps, *,
               footprints: dict, outside_nets, box: tuple,
               port_side: str = "member_escape") -> tuple:
    """Every net that **leaves** the unit, as a :class:`UnitPort` per pad.

    ``outside_nets`` is the set of nets some part **outside** this unit carries;
    a net all of whose pads are inside the unit is internal and is not a port.
    ⛔ A unit with no port at all **raises** (rule 8): a cell nothing connects to
    is either a partition defect or an instrument that observed nothing, and
    both are findings.

    ⭐⭐ **The representative-pair key, stated once.** Plan section 5.6 asks for
    *"the lowest ``(net, member, pad)`` port"*. The implemented key is
    ``_pad_key`` **of the composite number** ``"{member}.{pad}"``, and the reason
    is a correctness one rather than a preference: :func:`_route_pair` picks the
    first pad on the net in the stamped pad list and
    :func:`_connecting_pad_offsets` picks ``sorted(..., key=_pad_key)[0]``.
    Ordering the unit's pads by ``_pad_key`` of the composite number makes
    **all three the same pad**; a separate ``(net, member, pad)`` key would make
    the *routed* pair and the *aligned* pair different pads on the same net, and
    nothing downstream would say so. ⚠ The two orders are compared per unit and
    the disagreement count is reported (never asserted).
    """
    from .escape_map import pad_occupancy_side, rotate_escape

    if port_side not in L2_PORT_SIDES:
        raise ConstructError(
            f"port_side={port_side!r} is not one of {L2_PORT_SIDES}")
    outside = set(outside_nets)
    boxes = {ref: _courtyard_box(geometries[ref], ref, dx, dy, rot)
             for ref, (dx, dy, rot) in offsets.items()}
    ports: list = []
    for ref, (dx, dy, rot) in sorted(offsets.items()):
        geometry = geometries[ref]
        emap = (escape_maps(geometry.footprint) if callable(escape_maps)
                else (escape_maps or {}).get(geometry.footprint))
        member_box = _physical_box(geometry, ref, dx, dy, rot)
        others = [b for other, b in sorted(boxes.items()) if other != ref]
        for pad in _member_world_pads(ref, footprints[ref], (dx, dy, rot)):
            net = str(getattr(pad, "net_name", "") or "")
            if not net or net not in outside:
                continue
            own = str(pad.pad_number).split(".", 1)[1]
            access, cost, source, side = "", 0.0, port_side, None
            if port_side == "member_escape" and emap is not None:
                # ⛔⛔ **The member's escape side is a claim about the MEMBER,
                # and at L2 the corridor has to leave the WHOLE UNIT.** So each
                # escapable side is walked in S1's own rank order and the first
                # one whose corridor reaches the unit's edge without meeting
                # another member wins. ⚠ The first cut tested *"does the member's
                # box touch the unit's bounding box"* instead, and on a real cell
                # that is satisfied by about four members out of fifteen -- so
                # every net's port came off the same two parts and 9 of 9 units
                # landed on ONE side. A bounding box is not an occupancy map.
                ranked = sorted(
                    (e for e in emap.entries_for(own)
                     if e.layer == 0 and e.access != "BLOCKED"),
                    key=lambda e: (_ACCESS_RANK.get(e.access, 2),
                                   round(float(e.distance_mm), 6), e.side))
                if not ranked:
                    source = "no_escape"
                for entry in ranked:
                    world = rotate_escape(entry.side, int(rot))
                    if not _corridor_leaves_unit(member_box, pad, world,
                                                 others, box):
                        continue
                    side, access = world, entry.access
                    cost = round(float(entry.distance_mm), 6)
                    source = "member_escape"
                    break
                else:
                    if ranked:
                        # Every escapable side of this pad meets another member
                        # of its own unit. The pad is interior TO THE UNIT, and
                        # the honest answer is the unit's own occupancy.
                        source = "member_escape_interior"
            if side is None:
                side = pad_occupancy_side(pad.global_x, pad.global_y, box)
                if source == port_side:
                    source = "box_occupancy"
                if emap is not None:
                    local = _unrotate(side, int(rot))
                    access = access or emap.access(own, local)
                    cost = next((round(float(e.distance_mm), 6)
                                 for e in emap.entries_for(own)
                                 if e.side == local and e.layer == 0), 0.0)
            ports.append(UnitPort(
                net=net, side=side, x_mm=round(pad.global_x, 6),
                y_mm=round(pad.global_y, 6), member=ref, pad=own,
                access=access or "", cost=cost, source=source,
                number=str(pad.pad_number)))
    if not ports:
        raise ConstructError(
            f"the unit over {sorted(offsets)} has ZERO ports -- every one of "
            f"its nets is internal. An instrument that can observe nothing must "
            f"raise, never return a falsy result (rule 8)")
    return tuple(sorted(ports, key=lambda port: port.key))


#: The access ordering S1's :func:`~skidl_layout.escape_map.favored_side` uses.
_ACCESS_RANK = {"FAVORED": 0, "ACCESSIBLE": 1}


def port_rank(port: UnitPort) -> tuple:
    """⛔⛔ **Which port of a unit speaks for a net, and it is NOT the first one
    alphabetically -- standing finding 15(a), one level up.**

    S1 measured what happens when a rank collapses onto a tie-break: on a tight
    box every flush pad sits at corridor distance 0.0 on every side it touches,
    so ``FAVORED`` was decided by ``E < N < S < W`` for pad 1 of every chip
    passive at every size. The L2 shape of that mistake is *"take the
    lowest-numbered port"*: a unit's ports on one net can belong to members
    anywhere in the cell, and the lowest composite pad number picks one by
    **spelling**.

    The rank, every term derivable and every one recorded on the port:

    1. ⭐ **A port whose member is on the unit's own perimeter outranks one that
       was re-decided from the inside.** ``member_escape`` is a claim about a
       corridor S1 actually probed; ``member_escape_interior`` is
       ``pad_occupancy_side`` over a composite box and is a weaker statement.
    2. ``FAVORED`` before ``ACCESSIBLE`` before anything else -- S1's ordering.
    3. The corridor cost.
    4. ``_pad_key`` of the composite number, a total key over content.

    ⚠ It ranks the **anchor's** port only. A neighbour unit's own connecting pad
    stays whatever :func:`_route_pair` picks -- the lowest composite key -- so
    the routed pair and the aligned pair remain the same pad (see
    :func:`unit_ports`).
    """
    return (0 if port.source == "member_escape" else 1,
            _ACCESS_RANK.get(port.access, 2), round(float(port.cost), 6),
            _pad_key(port.number))


def _corridor_leaves_unit(member_box: tuple, pad, side: str, others,
                          unit_box_: tuple) -> bool:
    """Does this pad's escape corridor reach the **unit's** edge unobstructed?

    ⭐ **S1's** ``escape_corridor_clear`` **rule, one level up and applied to the
    same question**: a straight corridor the width of the pad, from the member's
    own box face outward to the unit's box face, is clear when no *other member*
    of the unit lies in it. ⛔ Measured from the placed boxes -- never from a
    member count, a package name or a bounding-box touch test.

    ⚠ A 1-member unit has no ``others``, so it is clear on every side and the
    interior re-decision cannot fire on the 37-of-53 single-part tail.
    ⚠ It is the **courtyard** boxes of the other members that obstruct
    (standing finding 13: standoff and collision are courtyard questions), and
    the corridor is a **proxy** for the router exactly as S1's was -- sound in
    the under-reporting direction, never in the other.
    """
    axis, sign, along = _AXES[side]
    half = (float(pad.size_x) if along == 1 else float(pad.size_y)) / 2.0
    centre = pad.global_x if along == 0 else pad.global_y
    lo, hi = centre - half, centre + half
    start = member_box[axis + 2] if sign > 0 else member_box[axis]
    end = unit_box_[axis + 2] if sign > 0 else unit_box_[axis]
    near, far = sorted((start, end))
    if far - near <= _TOL:
        return True
    corridor = ((near, lo, far, hi) if axis == 0 else (lo, near, hi, far))
    return not any(_pair_gap(corridor, other) < -_TOL for other in others)


def unit_from_part(ref: str, geometry, emap, *, name: str, kind: str,
                   footprint, outside_nets, port_side: str = "member_escape",
                   source: str = "footprint", group: str = "",
                   reason: str = "", meta: dict = None) -> Unit:
    """A 1-member unit: its own box, and S1's escape map for its ports.

    ⭐ **37 of 53 non-anchor units on this corpus are exactly this** (plan
    section 2.3), which is why L2 is *one big cell plus a tail of near-atoms*
    rather than the cell-against-cell problem overview section 3 assumed.
    """
    offsets = {ref: (0.0, 0.0, 0)}
    boxes = unit_box(offsets, {ref: geometry})
    ports = unit_ports(offsets, {ref: geometry},
                       {geometry.footprint: emap} if emap is not None else {},
                       footprints={ref: footprint}, outside_nets=outside_nets,
                       box=boxes[0], port_side=port_side)
    return Unit(name=name, kind=kind, refs=(ref,), anchor=ref, offsets=offsets,
                physical_box=boxes[0], courtyard_box=boxes[1], ports=ports,
                source=source, group=group or name,
                reason=reason or f"a 1-member group: {ref}'s own box and S1's "
                                 f"escape map for {geometry.footprint}",
                meta=dict(meta) if meta else None)


def unit_from_cell(result: CellResult, geometries: dict, escape_maps, *,
                   name: str, kind: str, footprints: dict, outside_nets,
                   anchor_x_mm: float, anchor_y_mm: float,
                   anchor_rot_deg: int = 0,
                   port_side: str = "member_escape") -> Unit:
    """⭐ A :class:`CellResult` becomes a placeable unit.

    ⛔⛔ **The unit's origin is its ANCHOR's position, not the box centre.** At
    L2 the anchor unit is put down at its origin and therefore leaves the
    anchor IC exactly where the parsed board has it -- standing finding 21, which
    ``route_pair`` needs and ``add_part`` does not. A centroid origin would move
    the one part that must not move.

    ⚠ A member the cell **failed** to place carries no position and is
    deliberately **not** in the unit: it stays in the cell's own failure log,
    where it is attributable, rather than being invented a place here.

    ⭐⭐⭐ **S8: an L0 unit inside the cell is EXPANDED to its parts.** When the
    cell ran with ``template_units=True`` a placement's ``ref`` is a *group
    name*, not a part -- there is no ``geometries[name]`` a board writer could
    use and no ``footprints[name]`` at all. ``result.meta["l0"]["offsets"]``
    carries each L0 unit's members relative to that unit's origin, so they are
    composed here exactly as ``drive_construct_arc._member_positions`` composes
    them one level down. ⛔ Additive: today's cells carry no ``meta["l0"]``
    offsets, so every recorded S5B/S7 unit is byte-identical.
    """
    from .geometry import transform_point

    offsets = {result.anchor: (0.0, 0.0, int(anchor_rot_deg))}
    inner = (result.meta.get("l0") or {}).get("offsets") or {}
    for placement in result.placements:
        own = inner.get(placement.ref)
        if not own:
            offsets[placement.ref] = (round(placement.x_mm - anchor_x_mm, 6),
                                      round(placement.y_mm - anchor_y_mm, 6),
                                      int(placement.rot_deg))
            continue
        for member, (dx, dy, rot) in sorted(own.items()):
            x, y = transform_point(placement.x_mm, placement.y_mm,
                                   float(placement.rot_deg), float(dx),
                                   float(dy))
            offsets[member] = (round(x - anchor_x_mm, 6),
                               round(y - anchor_y_mm, 6),
                               int((int(rot) + int(placement.rot_deg)) % 360))
    boxes = unit_box(offsets, geometries)
    ports = unit_ports(offsets, geometries, escape_maps, footprints=footprints,
                       outside_nets=outside_nets, box=boxes[0],
                       port_side=port_side)
    return Unit(
        name=name, kind=kind, refs=tuple(sorted(offsets)),
        anchor=result.anchor, offsets=offsets, physical_box=boxes[0],
        courtyard_box=boxes[1], ports=ports, source="constructed",
        group=name,
        reason=f"construct_cell on {result.anchor}: "
               f"{len(result.placements)} member(s) placed, "
               f"{len(result.meta['accounting']['failed'])} failed, origin at "
               f"the anchor's own position (standing finding 21)",
        meta={"cell_legal": result.legal,
              "cell_routed_fraction": result.routed_fraction,
              "cell_failed": list(result.meta["accounting"]["failed"]),
              "cell_side_order": list(result.meta["side_order"])})


# --------------------------------------------------------------------------- #
# The unit inventory
# --------------------------------------------------------------------------- #
#: ⛔⛔⛔ **S8 STAGE C -- the arm the fourth eyes-on unbound.** ``flattened`` is
#: S5B/S7's recorded behaviour and stays the **default**; ``template`` sends a
#: multi-member ``family`` group through :func:`construct_template` so it enters
#: L2 as **one rigid L0 unit** instead of *n* loose members.
#:
#: ⛔ The flatten branch's own comment cited *"rigidity is a refuted mechanism"*.
#: That refutation (cell-toolchain, 2026-07-31: harvested cells 117 corpus vias
#: against plain AUTO's 115) was measured on a **partial implementation** -- cells
#: harvested off the AUTO board, shapes chosen by a hash, **no router in the
#: loop, no escape map, no constructive placer**. It stays true about what it
#: measured, and per the human's standing method rule -- *"a decision to abandon
#: a path based on partially completed work will never yield a good result"* --
#: it may **no longer** be cited to keep ``family`` groups flattened on the
#: constructive path. ``flattened`` is now the **control arm**, not the answer.
FAMILY_UNITS: tuple[str, ...] = ("flattened", "template")


def board_units(partition, *, geometries, escape_maps, footprints,
                session=None, fab=None, unit_source: str = "constructed",
                port_side: str = "member_escape", nets_by_ref=None,
                cell_session=None, family_units: str = "flattened",
                template_session=None, template_kwargs: dict = None,
                orphan_units: bool = False,
                **cell_kwargs) -> tuple:
    """Every :class:`Unit` on one board, in ``CellGroup.name`` order.

    ``cell_session`` is a callable ``(group) -> RouteSession`` -- ⛔⛔ **a FRESH
    session per constructed cell** (standing finding 22, four instances, the
    last two of which move the *placement*). A shared session measures the
    session's history, not the board. ``template_session`` is the same callable
    for an L0 template and is subject to the same rule.

    ⛔ **The source resolution order, stated once and asserted by the driver
    against what this emits** (standing finding 20's eleventh opportunity):

    1. a 1-member group -> ``footprint``;
    2. an ``ic`` group with an anchor and >= 2 members -> ``constructed``;
    3. a multi-member group under ``family_units="template"`` ->
       ``constructed`` from :func:`construct_template`, or **flattened with a
       named reason** if the L0 loop refuses it;
    4. anything else -- **which under the default is every ``family`` group,
       because every one of them has ``anchor = None``** -- -> ``flattened``,
       one unit per member, each keeping the group name for priority.

    ⛔ ``family_units="flattened"`` is the default and must stay byte-identical
    to S5B/S7's recorded artifacts (S8 gate ``PT4``).
    ⛔ :func:`construct_board` needs **no** signature change for this: it
    consumes ``units=``, so **the inventory is the seam**.
    """
    if unit_source not in UNIT_SOURCE:
        raise ConstructError(
            f"unit_source={unit_source!r} is not one of {UNIT_SOURCE}")
    if family_units not in FAMILY_UNITS:
        raise ConstructError(
            f"family_units={family_units!r} is not one of {FAMILY_UNITS}")
    if family_units == "template" and (template_session is None
                                       or fab is None):
        raise ConstructError(
            "family_units='template' needs template_session=(group) -> "
            "RouteSession and fab= -- and a FRESH session per L0 template "
            "(standing finding 22, five instances, the last three of which "
            "move the PLACEMENT)")
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    # ⛔ The L0 loop's knobs are SEPARABLE from the L1 cell's, and deliberately:
    # ``anchor_x_mm``/``anchor_y_mm`` name the *anchor IC's* construction origin
    # and mean nothing to a two-resistor divider, whose anchor must stay where
    # the parsed board has it (standing finding 21). Everything else falls
    # through unless the caller states otherwise.
    l0_kwargs = (dict(template_kwargs) if template_kwargs is not None
                 else {key: value for key, value in cell_kwargs.items()
                       if key not in ("anchor_x_mm", "anchor_y_mm",
                                      "anchor_rot_deg")})
    # ⛔⛔⛔ **THE PRE-PASS, AND IT EXISTS BECAUSE THE INVENTORY WAS NOT A
    # PARTITION.** Measured on the recorded S5B/S7 arm, 2026-08-05: every
    # ``family`` member with a port on the anchor is claimed by **TWO** units --
    # its own flattened unit *and* the ``ic`` cell, which placed it as a side
    # neighbour -- and ``construct_board`` places both. The part ends up wherever
    # the alphabetically-later unit put it (always the ``ic`` cell) and the other
    # unit's box holds nothing. ⛔ So *"the hierarchy is destroyed at placement"*
    # is one level deeper than the S8 plan's section 1 fact 3 said: the L1 cell
    # had already swallowed the family, with ``template_units=False``, so its two
    # members were placed as independent side neighbours (``CB``/``L1``,
    # 25.91 mm apart). ⭐ The ``template`` arm therefore has TWO halves: the L1
    # cell builds its families as rigid L0 units **inside itself** (S5C's P-G,
    # 20/20 at L1, never promoted), and a family already inside one is **not
    # emitted again** at L2. ⛔ The default arm runs none of this.
    absorbed: dict = {}
    cells: dict = {}
    if family_units == "template" and unit_source == "constructed":
        for group in sorted(partition.groups, key=lambda g: g.name):
            if group.kind != "ic" or not group.anchor or group.size < 2:
                continue
            if any(ref not in geometries or ref not in footprints
                   for ref in group.refs):
                continue
            if cell_session is None or fab is None:
                raise ConstructError(
                    "a constructed unit needs cell_session= and fab= -- and a "
                    "FRESH session per cell (standing finding 22)")
            by_name = {other.name: other for other in partition.groups}
            result = construct_cell(
                partition, group.anchor, session=cell_session(group),
                geometries=geometries,
                fab=fab, escape_map=_map_of(
                    escape_maps, geometries[group.anchor].footprint),
                escape_maps=escape_maps,
                board=str(partition.meta.get("board", "")),
                nets_by_ref=nets_by_ref, template_units=True,
                template_session=lambda name: template_session(by_name[name]),
                **cell_kwargs)
            cells[group.name] = result
            # ⛔ A family is absorbed only if its L0 unit was actually PLACED.
            # An L0 unit the ladder refused is not on the board, and dropping
            # its members from the L2 inventory on the strength of a unit that
            # was merely *built* would make them vanish with nobody naming it
            # (standing finding 1).
            landed = {placement.ref for placement in result.placements}
            for unit_name, members in ((result.meta.get("l0") or {})
                                       .get("members_inside_units") or {}
                                       ).items():
                if (unit_name in by_name and len(members) > 1
                        and unit_name in landed):
                    absorbed[unit_name] = sorted(members)
    out: list = []
    for group in sorted(partition.groups, key=lambda g: g.name):
        refs = sorted(group.refs)
        inside = set(refs)
        outside = {net for ref, nets in nets_by_ref.items()
                   if ref not in inside for net in nets}
        missing = [ref for ref in refs if ref not in geometries
                   or ref not in footprints]
        if missing:
            # ⛔ Named, never dropped: a unit that vanishes is standing finding 1
            # wearing a bookkeeping hat.
            for ref in missing:
                out.append(("__skip__", group.name, ref,
                            "no footprint geometry or not on the session's "
                            "board (standing finding 6)"))
            refs = [ref for ref in refs if ref not in missing]
            if not refs:
                continue
        if len(refs) == 1:
            ref = refs[0]
            out.append(unit_from_part(
                ref, geometries[ref], _map_of(escape_maps,
                                              geometries[ref].footprint),
                name=group.name, kind=group.kind, footprint=footprints[ref],
                outside_nets=outside, port_side=port_side,
                source="footprint", group=group.name))
            continue
        if (group.kind == "ic" and group.anchor
                and unit_source == "constructed"):
            if cell_session is None or fab is None:
                raise ConstructError(
                    "a constructed unit needs cell_session= and fab= -- and a "
                    "FRESH session per cell (standing finding 22)")
            # ⛔ The pre-pass already ran this cell under the template arm; a
            # second construction would be a second session's history
            # (standing finding 22) and a second derivation (finding 29).
            result = cells.get(group.name)
            if result is None:
                result = construct_cell(
                    partition, group.anchor, session=cell_session(group),
                    geometries=geometries, fab=fab,
                    escape_map=_map_of(escape_maps,
                                       geometries[group.anchor].footprint),
                    escape_maps=escape_maps,
                    board=str(partition.meta.get("board", "")),
                    nets_by_ref=nets_by_ref, **cell_kwargs)
            out.append(unit_from_cell(
                result, geometries, escape_maps, name=group.name,
                kind=group.kind, footprints=footprints, outside_nets=outside,
                anchor_x_mm=cell_kwargs.get("anchor_x_mm", 0.0),
                anchor_y_mm=cell_kwargs.get("anchor_y_mm", 0.0),
                anchor_rot_deg=cell_kwargs.get("anchor_rot_deg", 0),
                port_side=port_side))
            # ⛔⛔⛔ **A MEMBER THE CELL DID NOT PLACE VANISHED HERE, AND
            # NOTHING NAMED IT.** ``unit_from_cell`` builds the unit out of the
            # cell's *placements*, so a member the L1 ladder refused is simply
            # absent from ``unit.refs`` -- and L2's accounting universe is the
            # set of UNIT NAMES, so ``unaccounted`` stays empty and the part is
            # lost in the gap between the two levels. ⭐ The family path below
            # already computes exactly this set (``unplaced``) and names it;
            # the ic path did not. Measured on ``lt3758_iso_flyback``: ``RLED``
            # is a member of ``ic:U1`` that shares **no net at all** with any
            # other member of it (its nets are ``LED_A``, carried only by
            # ``U2``, and ``VOUT``, carried only by the secondary side), so the
            # cell skips it by name at ring 2 and L2 never hears about it.
            #
            # ⛔ Under ``orphan_units`` each dropped member is re-emitted as its
            # own unit, so L2 places it against whatever it *does* connect to.
            # ⚠ This treats the symptom at the PLACEMENT layer; that the
            # partition put ``RLED`` in ``ic:U1`` at all is a separate question
            # for :mod:`skidl_layout.cells_partition`.
            dropped = [ref for ref in refs if ref not in set(out[-1].refs)]
            if dropped and orphan_units:
                out[-1].meta["dropped_by_cell"] = list(dropped)
                for ref in dropped:
                    shared = sorted(
                        {net for net in nets_by_ref.get(ref, ())}
                        & {net for other in out[-1].refs
                           for net in nets_by_ref.get(other, ())})
                    out.append(unit_from_part(
                        ref, geometries[ref],
                        _map_of(escape_maps, geometries[ref].footprint),
                        name=f"orphan:{ref}", kind="singleton",
                        footprint=footprints[ref], outside_nets=outside,
                        port_side=port_side, source="footprint",
                        group=group.name,
                        reason=f"ORPHAN -- {ref} is a member of {group.name} "
                               f"that the L1 cell did not place; it shares "
                               f"{shared or 'NO net'} with the members the "
                               f"cell did place, so it is re-emitted as its "
                               f"own unit rather than lost between the levels",
                        meta={"orphan_of": group.name,
                              "shared_with_cell": shared}))
            if absorbed:
                mine = {name: refs for name, refs in absorbed.items()
                        if set(refs) <= set(out[-1].refs)}
                out[-1].meta["absorbed_families"] = mine
                # ⛔ The L0 rows travel with the unit that owns them, so a
                # consumer can measure ports, member gaps and legality of a
                # template that lives INSIDE a cell without re-deriving any of
                # it (finding 29).
                out[-1].meta["l0_rows"] = [
                    row for row in ((result.meta.get("l0") or {}).get("units")
                                    or ())
                    if row.get("unit") in mine]
            continue
        # ⭐⭐⭐ S8 STAGE C -- the L0 template path. ``construct_template`` picks
        # the group's own anchor (:func:`l0_anchor`) and runs the SAME loop one
        # level down; ``unit_from_cell`` turns the result into a placeable box
        # with real ports. Both exist and passed 20/20 at S5C; only the wiring
        # into the L2 inventory is new.
        fell_back = ""
        if group.name in absorbed and set(absorbed[group.name]) >= set(refs):
            # ⛔⛔ **NOT a skip and NOT a drop: this group is already ON the
            # board, as a rigid L0 unit INSIDE its L1 cell.** Emitting it again
            # is what made the recorded inventory claim four to seven parts
            # twice. The claim is recorded on the owning cell's
            # ``meta["absorbed_families"]``, so nothing here is silent.
            continue
        if family_units == "template" and len(refs) >= 2:
            try:
                l0 = construct_template(
                    partition, group.name, session=template_session(group),
                    geometries=geometries, fab=fab, escape_maps=escape_maps,
                    board=str(partition.meta.get("board", "")),
                    nets_by_ref=nets_by_ref, **l0_kwargs)
            except ConstructError as error:
                # ⛔ NAMED, never silent, and the group falls back to flattened
                # so one refused family cannot cost the whole arm.
                fell_back = (f"the L0 loop refused this family and it FELL "
                             f"BACK to flattened: {error}")
            else:
                unplaced = sorted(set(refs)
                                  - {p.ref for p in l0.placements}
                                  - {l0.anchor})
                if unplaced:
                    fell_back = (f"construct_template left {unplaced} unplaced, "
                                 f"so the unit would be a SUBSET of the group "
                                 f"and its members would vanish from L2 -- FELL "
                                 f"BACK to flattened")
                else:
                    out.append(unit_from_cell(
                        l0, geometries, escape_maps, name=group.name,
                        kind=group.kind,
                        footprints={ref: footprints[ref] for ref in refs},
                        outside_nets=outside,
                        anchor_x_mm=l0.meta["l0"]["anchor_x_mm"],
                        anchor_y_mm=l0.meta["l0"]["anchor_y_mm"],
                        anchor_rot_deg=l0.meta["l0"]["anchor_rot_deg"],
                        port_side=port_side))
                    out[-1].meta.update({
                        "l0_template": True,
                        "l0_anchor": l0.meta["l0"]["anchor"],
                        "l0_anchor_why": l0.meta["l0"]["anchor_why"],
                        "l0_legal": bool(l0.legal),
                        "l0_routed_fraction": l0.routed_fraction,
                        "l0_courtyard_overlaps":
                            l0.meta.get("courtyard_overlaps"),
                        "l0_physical_overlaps":
                            l0.meta.get("physical_overlaps")})
                    continue
        # ⛔⛔ FLATTENED -- the CONTROL ARM. Under the default this is every
        # `family` group (all have `anchor = None`, so `construct_cell` cannot be
        # called on one); under ``family_units="template"`` it is only the groups
        # the L0 loop refused, and each one says so on its face.
        for ref in refs:
            out.append(unit_from_part(
                ref, geometries[ref], _map_of(escape_maps,
                                              geometries[ref].footprint),
                name=f"{group.name}|{ref}", kind=group.kind,
                footprint=footprints[ref], outside_nets=outside,
                port_side=port_side, source="flattened", group=group.name,
                # ⚠⚠ **The wording below is LEFT VERBATIM ON PURPOSE.** ``reason``
                # is inside ``Unit.to_dict()`` and therefore inside the L2
                # determinism digest, so re-wording it would move the default
                # arm's bytes and break gate ``PT4`` -- the regression lock the
                # plan makes bail-out 5. The citation it makes is corrected in
                # the comment above and in :data:`FAMILY_UNITS`, which are not
                # in the bytes. ⛔ It is only ever emitted under
                # ``family_units="flattened"``: a fallback carries its own
                # reason.
                reason=fell_back or
                       f"FLATTENED out of {group.name} ({len(refs)} members): "
                       f"rigidity is a refuted mechanism, so the members enter "
                       f"L2 individually and keep the group name for priority",
                meta={"family_units_fallback": fell_back} if fell_back
                else None))
    units = tuple(row for row in out if isinstance(row, Unit))
    skipped = tuple({"unit": row[1], "ref": row[2], "why": row[3]}
                    for row in out if isinstance(row, tuple))
    if not units:
        raise ConstructError(
            "board_units produced ZERO units -- an inventory over nothing is "
            "indistinguishable from one that found everything (rule 8)")
    return units, skipped


def _map_of(escape_maps, footprint):
    if callable(escape_maps):
        return escape_maps(footprint)
    return (escape_maps or {}).get(footprint)


# --------------------------------------------------------------------------- #
# S7 B1 -- the containment order, and a net's TIER inside it
# --------------------------------------------------------------------------- #
#: ⛔ The three levels, named once. ``l0_template`` is an **end-node** cell
#: template (the partition is flat, so every group is a leaf); ``l1_cell`` is a
#: template that CONTAINS templates -- an ``ic`` group plus the family cells with
#: a port on its anchor, which is exactly the set :func:`side_neighbours`
#: inventories; ``board`` is everything left.
TIER_NAMES = ("l0_template", "l1_cell", "board")

#: ⛔ The declared order arms. ``mps_within_tier`` is not here: it is not an
#: order we compute, it is KRT recomputing one, so the driver spells it as a
#: pass-level flag rather than as a permutation.
TIER_ORDERS = ("canonical", "reversed", "demand", "narrow")

#: ⛔⛔ **S7 STAGE A2 -- the route-set rule, DECLARED AS AN ARM.**
#: ``placed_pair`` is S5C's recorded behaviour (a net enters the route set when
#: **two or more of the PLACED parts** carry it); ``all_placed`` is the honest
#: rule (a net enters only when **every** part carrying it is placed). The two
#: differ **exactly** where parking differs, which is why the second ships as an
#: arm beside the first rather than as a quiet edit -- and why they must
#: **converge** once nothing is parked.
ROUTE_SET_RULES = ("placed_pair", "all_placed")

#: ⛔ The exclusion reasons, named once. An excluded net always carries one, and
#: a reason that matches nothing is reported as ``0`` rather than omitted.
EXCLUSION_REASONS = ("plane", "autoname", "single_part_net",
                     "single_placed_part", "unplaced_part")

#: ⛔⛔ **S9 STAGE B -- what the route set does with a plane net, DECLARED AS AN
#: ARM.**
#:
#: * ``exclude`` -- today's recorded behaviour, and the reason S9 exists: a net
#:   :func:`~skidl_layout.ratnest.is_plane_net` matches is dropped with the
#:   reason ``"plane"`` and never asked for. ⛔ The exclusion was correct **for a
#:   pipeline that pours**; the constructive path never calls
#:   :func:`~skidl_layout.krt.pour_planes`, so the parts it excludes get neither
#:   tracks nor copper nor a reserved corridor (S9 plan section 1 fact 5).
#: * ``route`` -- a plane net enters the set like any other net and is then
#:   subject to **exactly** the same remaining filters (autoname, single part,
#:   single placed part, the rule). ⭐ It is one branch removed, not a second
#:   code path: the accounting stays total either way.
#:
#: ⛔ **Neither is "right" until S6 grades it.** ``exclude`` stays the default so
#: every recorded S7/S8 arm reproduces byte for byte.
PLANE_POLICIES = ("exclude", "route")


def plane_report(partition, placed_refs=None, *, rule: str = "all_placed",
                 nets_by_ref=None) -> dict:
    """⭐⭐⭐ **S9 STAGE A -- the denominator that did not exist.**

    ⛔⛔⛔ **The defect this instrument makes countable.**
    :func:`~skidl_layout.ratnest.is_plane_net` is a **name regex** consulted in
    five places, and it means two different things: a *preference* in
    :func:`_pick_net`, :func:`template_port_member` and :func:`l2_side_lists`,
    and a **hard exclusion** in :func:`ring2_subanchor` and :func:`route_set`.
    The two hard ones are exactly the two that decide **whether a part is placed
    at all** and **whether it gets any copper** -- so a part carrying no
    plane-free net is skipped by name at ring 2 and every net it touches is
    dropped from the route set. It is not badly scored; it is **unscored**,
    which is standing finding 1 wearing a placement's hat.

    Returns, for one board:

    * ``plane_nets`` -- each plane net with its part count and its parts;
    * ``parts`` -- per part, its nets split into ``plane`` / ``plane_free``;
    * ``plane_only`` / ``has_plane_free`` -- ⛔ a **partition** of the parts,
      asserted here rather than hoped for;
    * ``nearest`` -- for each plane-only part, the placed part it shares the
      most plane nets with, by the **same total key** Stage C's
      ``plane_fallback`` uses, so the report and the placement cannot disagree
      about who the sub-anchor would be;
    * ``census`` -- ``parts``, ``plane_only``, ``requested_nets``,
      ``plane_excluded_nets``, ``parts_with_zero_requested_net``.

    ⚠⚠ **"Nearest" here is CONNECTIVITY, not geometry, and the word is the
    plan's.** This function is pure over a :class:`Partition` and has no
    positions; a geometric nearest would need a placement and would make the
    denominator a property of the arm being measured. The anchor-distance table
    the plan also asks for is measured by the driver, off a written board.

    ⛔ An empty partition **RAISES** (standing finding 1). ⛔ The report is
    deterministic: every list is sorted and nothing is keyed on arrival order.
    """
    from .ratnest import is_plane_net

    if rule not in ROUTE_SET_RULES:
        raise ConstructError(
            f"route-set rule {rule!r} is not one of {ROUTE_SET_RULES}")
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    if not nets_by_ref:
        raise ConstructError(
            "plane_report was handed a netlist with NO parts -- a census over "
            "nothing is indistinguishable from one that found nothing (rule 8)")
    group_of = {}
    for group in partition.groups:
        for ref in group.refs:
            group_of[ref] = group.name

    parts_on: dict = {}
    for ref, nets in nets_by_ref.items():
        for net in set(nets):
            parts_on.setdefault(str(net), set()).add(str(ref))

    parts: dict = {}
    plane_only, has_free = [], []
    for ref in sorted(nets_by_ref):
        mine = sorted({str(net) for net in nets_by_ref[ref]})
        plane = [net for net in mine if is_plane_net(net)]
        free = [net for net in mine if not is_plane_net(net)]
        parts[str(ref)] = {"ref": str(ref), "group": group_of.get(ref, ""),
                           "nets": mine, "plane": plane, "plane_free": free,
                           "plane_only": not free}
        (plane_only if not free else has_free).append(str(ref))
    # ⛔ Totality, ASSERTED. The two lists are built from one branch, so a
    # disagreement here would mean the branch itself was edited into two.
    if sorted(plane_only + has_free) != sorted(str(r) for r in nets_by_ref):
        raise ConstructError(
            f"plane_report is not total: {len(plane_only)} plane-only + "
            f"{len(has_free)} with a plane-free net against "
            f"{len(nets_by_ref)} part(s)")

    placed = (sorted(str(r) for r in placed_refs) if placed_refs is not None
              else sorted(str(r) for r in nets_by_ref))
    nearest: dict = {}
    for ref in plane_only:
        nearest[ref] = _plane_subanchor(ref, placed, nets_by_ref,
                                        group_of=group_of)

    requested, excluded = route_set(partition, placed, rule=rule,
                                    nets_by_ref=nets_by_ref)
    plane_excluded = sorted(row["net"] for row in excluded
                            if row["reason"] == "plane")
    wanted = set(requested)
    zero = sorted(ref for ref in sorted(nets_by_ref)
                  if not (wanted & {str(net) for net in nets_by_ref[ref]}))
    # ⚠⚠ **REPORTED, NOT ASSERTED, and the plan asked for the opposite.** S9's
    # section 4 states *"``parts_with_zero_requested_net`` is a SUBSET of
    # ``plane_only``"* as an in-function assertion. It is not a theorem: a part
    # whose only plane-free net is an autoname (``N$...``), a single-part net or
    # a net with an unplaced member has a plane-free net **and** no requested
    # net. Raising on that would be this module deciding a measurement in
    # advance, so the exception set is a **column** and gate ``PN1`` reads it.
    outside = sorted(set(zero) - set(plane_only))
    return {
        "board": str(getattr(partition, "board", "")),
        "rule": rule,
        "plane_nets": [{"net": net, "parts": sorted(parts_on[net]),
                        "part_count": len(parts_on[net])}
                       for net in sorted(parts_on) if is_plane_net(net)],
        "parts": parts,
        "plane_only": plane_only,
        "has_plane_free": has_free,
        "nearest": nearest,
        "placed": placed,
        "requested_nets": list(requested),
        "plane_excluded_nets": plane_excluded,
        "excluded_census": exclusion_census(excluded),
        "parts_with_zero_requested_net": zero,
        "zero_requested_but_not_plane_only": outside,
        "census": {
            "parts": len(nets_by_ref),
            "nets": len(parts_on),
            "plane_nets": sum(1 for net in parts_on if is_plane_net(net)),
            "plane_only": len(plane_only),
            "has_plane_free": len(has_free),
            "plane_only_share": (round(len(plane_only) / len(nets_by_ref), 6)
                                 if nets_by_ref else None),
            "requested_nets": len(requested),
            "plane_excluded_nets": len(plane_excluded),
            "parts_with_zero_requested_net": len(zero),
            "zero_requested_but_not_plane_only": len(outside),
            "plane_only_with_no_subanchor": sum(
                1 for ref in plane_only if nearest[ref] is None)},
    }


def _plane_subanchor(ref, placed, nets_by_ref, *, group_of=None) -> dict | None:
    """⛔ **ONE key, two consumers.** The sub-anchor a plane-only part would get
    under ``plane_fallback``, by the total key S9 section 6 states::

        (-shared_plane_nets, not same_partition_group, ref)

    -- most shared plane nets first, **its own group's members preferred over
    strangers**, then the smallest ref. :func:`plane_report` reports it and
    :func:`ring2_subanchor` acts on it; two derivations of one choice is how the
    report and the placement drift apart (standing finding 29).
    """
    from .ratnest import is_plane_net

    group_of = group_of or {}
    mine = {str(net) for net in nets_by_ref.get(ref, ())}
    home = group_of.get(ref, "")
    best = None
    for candidate in sorted(str(c) for c in placed):
        if candidate == str(ref):
            continue
        shared = sorted(net for net in (mine & {str(n) for n in
                                                nets_by_ref.get(candidate, ())})
                        if is_plane_net(net))
        if not shared:
            continue
        same_group = bool(home) and group_of.get(candidate, "") == home
        key = (-len(shared), not same_group, candidate)
        if best is None or key < best[0]:
            best = (key, {"ref": candidate, "shared_plane_nets": shared,
                          "same_partition_group": same_group,
                          "via": "plane_fallback",
                          "why": f"shares {len(shared)} plane net(s) "
                                 f"{shared} with {ref}"
                                 + (f"; both are in {home}" if same_group
                                    else "; a stranger's group")})
    return None if best is None else best[1]


def route_set(partition, placed_refs, *, rule: str = "placed_pair",
              nets_by_ref=None, planes: str = "exclude") -> tuple:
    """⭐⭐⭐ **S7 STAGE A1 -- the denominator, and it is the whole point.**

    Returns ``(requested, excluded)``: the sorted nets a route call will
    actually **ask for**, and a row per net that did not enter the set, each
    carrying a **reason** out of :data:`EXCLUSION_REASONS` and its own part
    census.

    ⛔⛔ **This function exists because a caption said "unrouted 7 net(s)" about
    nets it had never asked for.** ``check_connected`` runs over the whole
    board -- plane nets by policy, plus nets orphaned by parking -- so the
    number was a count of a denominator the instrument did not measure
    (standing finding 1, wearing a caption's hat). The fix is not a smaller
    number: it is **three** numbers that add up.

    ⛔ The accounting is **TOTAL** and checked here: every net the netlist
    carries is either requested or excluded with a named reason, never both and
    never neither. ⛔ An empty requested set **RAISES** -- an instrument that
    asks for nothing and then reports no failures is indistinguishable from one
    that routed everything.

    ``planes`` is S9 Stage B's declared arm -- see :data:`PLANE_POLICIES`. ⛔ It
    defaults to ``"exclude"``, which is what every recorded S7/S8 arm measured;
    ⛔⛔ **and it moves the DENOMINATOR**, so two arms that differ in it cannot
    be compared net-for-net (S9 section 5's two-column rule).
    """
    from .ratnest import is_plane_net

    if rule not in ROUTE_SET_RULES:
        raise ConstructError(
            f"route-set rule {rule!r} is not one of {ROUTE_SET_RULES}")
    if planes not in PLANE_POLICIES:
        raise ConstructError(
            f"planes={planes!r} is not one of {PLANE_POLICIES}")
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    placed = set(placed_refs)
    parts_on: dict = {}
    placed_on: dict = {}
    for ref, nets in nets_by_ref.items():
        for net in set(nets):
            parts_on.setdefault(str(net), set()).add(ref)
            if ref in placed:
                placed_on.setdefault(str(net), set()).add(ref)
    requested, excluded = [], []

    def _drop(net, reason):
        excluded.append({"net": net, "reason": reason,
                         "parts": sorted(parts_on[net]),
                         "placed_parts": sorted(placed_on.get(net, ()))})

    for net in sorted(parts_on):
        total, on_board = parts_on[net], placed_on.get(net, set())
        # ⛔ S9 Stage B: ONE branch, declared. Under ``planes="route"`` a plane
        # net falls through to **exactly** the same remaining filters rather
        # than into a second code path -- the accounting below stays total
        # either way, and the OFF value is today's behaviour.
        if planes == "exclude" and is_plane_net(net):
            _drop(net, "plane")
        elif str(net).startswith("N$"):
            _drop(net, "autoname")
        elif len(total) < 2:
            _drop(net, "single_part_net")
        elif len(on_board) < 2:
            _drop(net, "single_placed_part")
        elif rule == "all_placed" and len(on_board) < len(total):
            _drop(net, "unplaced_part")
        else:
            requested.append(net)
    if len(requested) + len(excluded) != len(parts_on):
        raise ConstructError(
            f"the route-set accounting is not total -- {len(requested)} "
            f"requested + {len(excluded)} excluded against {len(parts_on)} "
            f"net(s) on the netlist")
    if not requested:
        raise ConstructError(
            f"the route set under rule {rule!r} is EMPTY over {len(placed)} "
            f"placed part(s) and {len(parts_on)} net(s) -- an instrument that "
            f"asks for nothing cannot report a failure (rule 8)")
    return requested, excluded


def exclusion_census(excluded) -> dict:
    """``reason -> count``. ⛔ Every declared reason present, ``0`` included."""
    out = {reason: 0 for reason in EXCLUSION_REASONS}
    for row in excluded:
        out[row["reason"]] = out.get(row["reason"], 0) + 1
    return out


@dataclass(frozen=True)
class NetTier:
    """One net's place in the containment order. ⛔ A net gets exactly one."""

    net: str
    tier: int                 # 0 | 1 | 2, an index into TIER_NAMES
    cell: str                 # the containing cell's name; "" at board tier
    parts: tuple              # the parts the net touches, sorted
    reason: str

    @property
    def tier_name(self) -> str:
        return TIER_NAMES[self.tier]

    def to_dict(self) -> dict:
        return {"net": self.net, "tier": self.tier, "tier_name": self.tier_name,
                "cell": self.cell, "parts": list(self.parts),
                "reason": self.reason}


def containment_cells(partition, *, nets_by_ref=None) -> tuple:
    """``(l0, l1)``, each ``{cell name: frozenset(refs)}``.

    ⛔⛔ **The L1 membership rule is not invented here -- it is
    :func:`side_neighbours`' own inventory, restated as a set.** That function
    builds its candidate list out of (a) the anchor group's own refs and (b)
    every ``family`` group sharing a net with the anchor ("the cell X has a port
    on the anchor"). A *second* derivation of which parts are in an L1 cell is
    exactly standing finding 29's defect, so this one names its source and any
    change to `side_neighbours`' candidate list must change this too.

    ⚠ **L0 excludes ``ic`` groups deliberately.** An ``ic`` group is the thing
    that CONTAINS templates; treating it as a leaf as well would make the tier
    order ambiguous for every net internal to the anchor cell's own members.
    """
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    l0: dict = {}
    for group in sorted(partition.groups, key=lambda g: g.name):
        if group.kind == "ic" or group.size < 2:
            continue
        l0[group.name] = frozenset(group.refs)
    l1: dict = {}
    for group in sorted(partition.groups, key=lambda g: g.name):
        if group.kind != "ic" or not group.anchor:
            continue
        anchor_nets = set(nets_by_ref.get(group.anchor, ()))
        members = set(group.refs)
        for other in partition.groups:
            if other.kind != "family" or group.anchor in other.refs:
                continue
            if _shared_nets(other.refs, anchor_nets, nets_by_ref):
                members |= set(other.refs)
        l1[group.name] = frozenset(members)
    return l0, l1


def net_tiers(partition, nets, *, nets_by_ref=None) -> tuple:
    """⭐⭐⭐ **S7 requirement 1.** Every net in ``nets``, assigned one tier.

    A net's tier is the **smallest cell in the containment order whose members
    cover every part the net touches**, ties broken by cell name; anything no
    cell covers is ``board``.

    ⛔ ``nets`` is the caller's **route set** -- this function does not decide
    which nets are worth routing (planes, autonames and orphans are the route
    set's business, S7 stage A), only where each one sits. ⛔ The result is a
    **partition** of ``nets``: exactly one row per net, checked here rather than
    hoped for by the caller.

    ⛔ An empty ``nets`` **raises** -- a tiering over nothing is
    indistinguishable from one that tiered everything (standing finding 1).
    """
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    wanted = sorted({str(net) for net in nets})
    if not wanted:
        raise ConstructError(
            "net_tiers was handed an EMPTY net set -- a tiering over nothing "
            "is indistinguishable from one that tiered everything (rule 8)")
    parts_on: dict = {}
    for ref, refs_nets in nets_by_ref.items():
        for net in set(refs_nets):
            parts_on.setdefault(str(net), set()).add(ref)
    l0, l1 = containment_cells(partition, nets_by_ref=nets_by_ref)
    rows: list = []
    for net in wanted:
        parts = frozenset(parts_on.get(net, ()))
        if not parts:
            raise ConstructError(
                f"net {net!r} is carried by NO part of this partition -- a net "
                f"the netlist does not know cannot be tiered (rule 8)")
        hit = None
        for tier, cells in ((0, l0), (1, l1)):
            covering = sorted((len(refs), name) for name, refs in cells.items()
                              if parts <= refs)
            if covering:
                hit = (tier, covering[0][1], covering[0][0])
                break
        if hit is None:
            rows.append(NetTier(
                net=net, tier=2, cell="", parts=tuple(sorted(parts)),
                reason=f"no L0 template and no L1 cell covers "
                       f"{sorted(parts)} -- board tier"))
            continue
        tier, cell, size = hit
        rows.append(NetTier(
            net=net, tier=tier, cell=cell, parts=tuple(sorted(parts)),
            reason=f"{TIER_NAMES[tier]} {cell} ({size} member(s)) covers "
                   f"{sorted(parts)}"))
    seen = [row.net for row in rows]
    if sorted(seen) != wanted or len(set(seen)) != len(seen):
        raise ConstructError(
            f"net_tiers is not a partition of its input: {len(seen)} row(s) "
            f"({len(set(seen))} distinct) against {len(wanted)} net(s)")
    return tuple(rows)


def tier_census(rows) -> dict:
    """``tier name -> count``. ⛔ Every declared tier present, ``0`` included."""
    out = {name: 0 for name in TIER_NAMES}
    for row in rows:
        out[row.tier_name] += 1
    return out


def nets_by_tier(rows, *, order: str = "canonical") -> dict:
    """``tier index -> [net, ...]`` in the arm's declared order.

    ⛔ **This is S7 requirement 4 -- "the route order at each cell-template
    level is a knob"** -- and it is deliberately a *pure permutation of names*:
    it never moves a part (part 1 §7.6 stays deferred).

    - ``canonical``  sorted; the control.
    - ``reversed``   sorted, reversed.
    - ``demand``     descending part count, then name -- the "widest net first"
                     reading.
    - ``narrow``     ascending part count, then name -- its opposite, declared so
                     ``demand`` is a measured choice rather than the only one
                     tried.
    """
    if order not in TIER_ORDERS:
        raise ConstructError(f"order={order!r} is not one of {TIER_ORDERS}")
    out: dict = {index: [] for index in range(len(TIER_NAMES))}
    for row in rows:
        out[row.tier].append(row)
    keys = {
        "canonical": lambda r: (r.net,),
        "reversed": lambda r: (r.net,),
        "demand": lambda r: (-len(r.parts), r.net),
        "narrow": lambda r: (len(r.parts), r.net),
    }
    for index, group in out.items():
        group.sort(key=keys[order])
        if order == "reversed":
            group.reverse()
        out[index] = [row.net for row in group]
    return out


# --------------------------------------------------------------------------- #
# S8 STAGE A -- SEGMENT tiers: a net decomposed into intra-cell pieces
# --------------------------------------------------------------------------- #
#: ⛔ The two kinds, named once. An ``internal`` segment lives wholly inside one
#: cell; a ``bridge`` is everything a cell does not cover, plus **one tap per
#: already-claimed segment** so the pieces are joinable.
SEGMENT_KINDS = ("internal", "bridge")

#: ⛔⛔ **A DECLARED ARM, NOT A CHOICE.** The S8 plan section 4 words the bridge's
#: tier as *"the smallest cell covering the REMAINDER, else board"*, and
#: ``remainder`` is that sentence executed. But a bridge is handed to a router as
#: ``parts + taps``, and on ``lt3844_buck``'s ``SW`` the remainder is **empty**
#: while the two taps (``CB``, ``D1``) both sit inside ``ic:U1`` -- so the literal
#: rule tiers that join at ``board`` and the terminals rule at ``l1_cell``. ⛔ The
#: two agree on the plan's own motivating case (``VFB`` -> ``l1_cell ic:U1``
#: either way), which is why neither can be picked by reading the plan. The
#: plan's wording is the **default**; the other ships beside it and gate ``PT1``
#: reports every net they disagree on (standing finding 20, fourteenth instance).
BRIDGE_TIER_RULES = ("remainder", "terminals")


@dataclass(frozen=True)
class NetSegment:
    """One piece of one net. ⛔ A net's segments **partition** its parts.

    ⚠⚠ **``taps`` is a seventh field the S8 plan's ``NetSegment(net, tier, cell,
    parts, kind, reason)`` did not name, and it is not a decoration.** The plan
    states the motivating bridge as ``{U1} + tap`` *and* states the invariant
    *"the union of a net's segments' parts equals the net's parts, disjointly"*.
    Those two cannot both be true if the tap lives in ``parts`` -- ``RFB1`` is
    already the divider segment's. So a tap is carried **beside** ``parts``: it
    is a terminal the bridge must REACH, owned by another segment, and it is
    excluded from the totality check by construction rather than by an
    exception.
    """

    net: str
    tier: int                 # 0 | 1 | 2, an index into TIER_NAMES
    cell: str                 # the containing cell's name; "" at board tier
    parts: tuple              # the parts THIS segment claims, sorted
    kind: str                 # one of SEGMENT_KINDS
    reason: str
    taps: tuple = ()          # parts owned by a sibling segment, sorted

    @property
    def tier_name(self) -> str:
        return TIER_NAMES[self.tier]

    @property
    def terminals(self) -> tuple:
        """``parts + taps``, sorted -- what a router would be handed."""
        return tuple(sorted(set(self.parts) | set(self.taps)))

    def to_dict(self) -> dict:
        return {"net": self.net, "tier": self.tier, "tier_name": self.tier_name,
                "cell": self.cell, "parts": list(self.parts), "kind": self.kind,
                "taps": list(self.taps), "terminals": list(self.terminals),
                "reason": self.reason}


def segment_tiers(partition, nets, *, nets_by_ref=None,
                  bridge_tier_rule: str = "remainder") -> tuple:
    """⭐⭐⭐ **S8 STAGE A -- the instrument requirements 1 and 2 need.**

    ⛔⛔ **Why whole-net tiering cannot serve them.** :func:`net_tiers` assigns
    *"the smallest cell whose members cover EVERY part the net touches"*, and a
    template's port net touches the anchor **by definition** -- so ``VFB``
    (``RFB1``, ``RFB2``, ``U1``) can never be tiered with its own divider. The
    human's requirement *"route the parts within the root cell templates first"*
    is therefore not an ordering change over whole nets; it needs tiering on
    **pad subsets**, which is this function.

    The rule, stated once: for each net, walk the containment order L0 -> L1
    (:func:`containment_cells`), smallest cell first, ties by name. Each cell
    holding **>= 2** of the net's still-unclaimed parts claims them as one
    ``internal`` segment at that cell's tier. Whatever remains -- plus **one tap
    per already-claimed segment** -- is a single ``bridge`` segment at the tier
    of the smallest cell covering the remainder, else ``board``.

    ⛔ The accounting is **TOTAL and DISJOINT**, checked here rather than hoped
    for: every net yields >= 1 segment and the union of a net's segments'
    ``parts`` is exactly the net's parts, with no part claimed twice.
    ⛔ An empty ``nets`` **raises** (standing finding 1).
    ⛔ :func:`net_tiers` is **unchanged** -- S7's recorded arms stay reproducible.

    ⚠ A net wholly inside one cell yields **one** ``internal`` segment and no
    bridge; a net no cell covers yields **one** ``bridge`` and is exactly
    today's whole-net behaviour. The instrument only becomes interesting where
    the two disagree.

    ``bridge_tier_rule`` is a declared arm, not a preference -- see
    :data:`BRIDGE_TIER_RULES`. The default is the plan's own wording.
    """
    if bridge_tier_rule not in BRIDGE_TIER_RULES:
        raise ConstructError(
            f"bridge_tier_rule={bridge_tier_rule!r} is not one of "
            f"{BRIDGE_TIER_RULES}")
    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    wanted = sorted({str(net) for net in nets})
    if not wanted:
        raise ConstructError(
            "segment_tiers was handed an EMPTY net set -- a decomposition over "
            "nothing is indistinguishable from one that decomposed everything "
            "(rule 8)")
    parts_on: dict = {}
    for ref, refs_nets in nets_by_ref.items():
        for net in set(refs_nets):
            parts_on.setdefault(str(net), set()).add(ref)
    l0, l1 = containment_cells(partition, nets_by_ref=nets_by_ref)
    # ⛔ Smallest cell first, ties by name -- the SAME tie-break net_tiers uses,
    # and it is what makes an L1 cell that swallowed a family lose to the family
    # itself. L1 cells can overlap (two ic groups can pull in one family), so a
    # declared order is a correctness requirement, not a tidiness one.
    ladder = [(tier, name, refs)
              for tier, cells in ((0, l0), (1, l1))
              for _size, name, refs in sorted(
                  (len(refs), name, refs) for name, refs in cells.items())]
    rows: list = []
    for net in wanted:
        parts = frozenset(parts_on.get(net, ()))
        if not parts:
            raise ConstructError(
                f"net {net!r} is carried by NO part of this partition -- a net "
                f"the netlist does not know cannot be segmented (rule 8)")
        unclaimed = set(parts)
        mine: list = []
        for tier, name, refs in ladder:
            held = unclaimed & set(refs)
            if len(held) < 2:
                continue
            mine.append(NetSegment(
                net=net, tier=tier, cell=name, parts=tuple(sorted(held)),
                kind="internal",
                reason=f"{TIER_NAMES[tier]} {name} ({len(refs)} member(s)) "
                       f"holds {len(held)} of {net}'s parts: {sorted(held)}"))
            unclaimed -= held
        # ⛔ One tap per claimed segment -- the lowest-named part of it, so the
        # choice is content-addressed and never arrival-ordered (finding 8).
        taps = tuple(sorted(segment.parts[0] for segment in mine))
        if unclaimed or len(mine) > 1:
            # ⛔ ``remainder`` is the plan's sentence; ``terminals`` also asks the
            # taps to fit. The two differ only where the remainder is empty or
            # the taps leave the covering cell -- and PT1 counts those.
            need = (set(unclaimed) if bridge_tier_rule == "remainder"
                    else set(unclaimed) | set(taps))
            covering = sorted(
                (len(refs), tier, name)
                for tier, name, refs in ladder if need <= set(refs))
            if need and covering:
                size, tier, name = covering[0]
                why = (f"{TIER_NAMES[tier]} {name} ({size} member(s)) covers "
                       f"the {bridge_tier_rule} {sorted(need)}")
            else:
                tier, name = 2, ""
                why = (f"no cell covers the {bridge_tier_rule} {sorted(need)}"
                       if need else
                       f"{len(mine)} claimed segment(s) with nothing left over "
                       f"-- the bridge is the join between them")
            mine.append(NetSegment(
                net=net, tier=tier, cell=name, parts=tuple(sorted(unclaimed)),
                kind="bridge", taps=taps,
                reason=f"{why}; taps {list(taps)} into "
                       f"{len(mine)} claimed segment(s)"))
        # ⚠ There is no third branch and that is a *proof*, not an omission: if
        # nothing was claimed then ``unclaimed`` is the whole (non-empty) net and
        # the first branch already fired. The only way here is exactly one
        # internal segment that claimed every part -- ``VC_C`` on
        # ``lt3844_buck``, and the plan's "one internal segment, no bridge".
        claimed: list = [ref for segment in mine for ref in segment.parts]
        if sorted(claimed) != sorted(parts) or len(set(claimed)) != len(claimed):
            raise ConstructError(
                f"segment_tiers is not a partition of {net!r}'s parts: "
                f"{sorted(claimed)} claimed against {sorted(parts)} carried")
        rows.extend(mine)
    seen = {row.net for row in rows}
    if seen != set(wanted):
        raise ConstructError(
            f"segment_tiers dropped a net: {len(seen)} distinct against "
            f"{len(wanted)} asked for")
    return tuple(rows)


def segment_census(rows) -> dict:
    """The honest shape of a decomposition. ⛔ Every declared key present.

    ⚠ ``nets_with_an_internal_segment`` is the number the whole stage turns on
    and it is **expected to be small on the power boards** (S7's control 6
    measured tier-0 thinness on 4 of 4). It is reported, never hidden.
    """
    out = {"segments": 0, "internal": 0, "bridge": 0,
           "nets": len({row.net for row in rows}),
           "nets_with_an_internal_segment": 0,
           "internal_by_tier": {name: 0 for name in TIER_NAMES},
           "bridge_by_tier": {name: 0 for name in TIER_NAMES}}
    with_internal = set()
    for row in rows:
        out["segments"] += 1
        out[row.kind] += 1
        out[f"{row.kind}_by_tier"][row.tier_name] += 1
        if row.kind == "internal":
            with_internal.add(row.net)
    out["nets_with_an_internal_segment"] = len(with_internal)
    return out


def segments_by_tier(rows, *, order: str = "canonical") -> dict:
    """``tier index -> [NetSegment, ...]`` in the arm's declared order.

    ⛔ The same four orders :func:`nets_by_tier` declares, over segments rather
    than nets, so an S8 arm and an S7 arm can be read against each other. The
    ``demand``/``narrow`` keys count a segment's **terminals** (parts + taps),
    which is what a router is actually handed.
    """
    if order not in TIER_ORDERS:
        raise ConstructError(f"order={order!r} is not one of {TIER_ORDERS}")
    out: dict = {index: [] for index in range(len(TIER_NAMES))}
    for row in rows:
        out[row.tier].append(row)
    keys = {
        "canonical": lambda r: (r.cell, r.net, r.kind),
        "reversed": lambda r: (r.cell, r.net, r.kind),
        "demand": lambda r: (-len(r.terminals), r.cell, r.net, r.kind),
        "narrow": lambda r: (len(r.terminals), r.cell, r.net, r.kind),
    }
    for index, group in out.items():
        group.sort(key=keys[order])
        if order == "reversed":
            group.reverse()
        out[index] = list(group)
    return out


def l2_anchor(units, *, rule: str = "largest_cell") -> str:
    """⭐ **Overview open question 4, answered.** Which unit is the L2 anchor.

    ⛔ ``largest_cell`` -- **the unit with the most members**, ties broken by
    ``Unit.name``. ⚠ *Not* ``kind == "ic"``: ``lt3758_iso_flyback``'s ``ic:U2``
    is an ``ic`` group with **one** member, so a rule keyed on kind alone would
    offer a bare opto-isolator as an anchor candidate. The rule is about
    **member count**.

    ⭐ Measured support (plan section 2.3): on 5 of 5 subjects the largest unit
    is an ``ic`` group holding **34-57 %** of the board and it is **unique** on
    all five, so the tie-break is exercised by a unit test rather than by luck.

    ⛔ Both alternatives ship and are measured every run, **reported not
    asserted**: ``most_ports`` (the unit with the most distinct nets leaving it)
    and ``connector`` (the lowest-named connector -- the *"place the board around
    its I/O"* reading).
    """
    if rule not in L2_ANCHOR_RULES:
        raise ConstructError(f"rule={rule!r} is not one of {L2_ANCHOR_RULES}")
    units = list(units)
    if not units:
        raise ConstructError(
            "l2_anchor was handed no unit -- an instrument that can observe "
            "nothing must raise (rule 8)")
    if rule == "largest_cell":
        return sorted(units, key=lambda u: (-len(u.refs), u.name))[0].name
    if rule == "most_ports":
        return sorted(units, key=lambda u: (-len(u.nets), u.name))[0].name
    connectors = [u for u in units if u.kind == "connector"]
    if not connectors:
        raise ConstructError(
            "the 'connector' anchor rule found NO connector unit on this "
            "board -- a rule that matches nothing must say so (rule 8)")
    return sorted(connectors, key=lambda u: u.name)[0].name


def _l2_class(unit: Unit, anchor_nets: set, bindings: dict) -> tuple:
    """P1's classes, one level up. ⛔ The more specific classifier wins.

    ⚠ **``decoupling`` is expected to be RARE at L2 and that is a finding, not a
    bug**: a decap of the anchor IC is a *member* of the anchor cell, so it is
    never a separate unit. The class is declared, computed and its census
    reported, exactly so that *"a declared class that matches nothing"* is a
    number rather than a surprise (standing finding 20).
    """
    own = set(unit.nets)
    shared = sorted(own & anchor_nets)
    if unit.source == "flattened":
        return ("template", shared,
                f"a member of the {unit.group} template, flattened")
    roles = {bindings[ref].role for ref in unit.refs if ref in bindings}
    if roles & {"decap", "bulk"}:
        return ("decoupling", shared,
                f"a PinBinding with role(s) {sorted(roles & {'decap', 'bulk'})}")
    if own and own <= anchor_nets:
        return ("internal", shared,
                "every net leaving the unit is a net the anchor unit touches")
    return ("one_pad", shared, "shares a net with the anchor unit")


def l2_side_lists(units, anchor: str, *, port_side: str = "member_escape",
                  order: str = "port_edge", bindings=None) -> tuple:
    """⭐ **P2 one level up.** Which side of the anchor **unit** each unit lands
    on, and in what order.

    The join is the same two shipped tables S3 used, one level up: a unit's port
    names the **net**, and the anchor unit's port on that net names the **side**.
    Ordering is the projection of the anchor unit's port order onto that edge.

    ⛔⛔ **Standing finding 24 is LIVE here and is not re-derived: no
    lexicographic key makes an off-edge fan non-crossing.** L2's units carry
    **several ports on one side**, so this is strictly worse than L1. The order
    key is *stated*; the crossing count is *measured after placement*; and the
    guard lives downstream, in P-A's transaction -- exactly where S5 put it.

    Returns ``(by_side, skipped, chosen)``.
    """
    from .ratnest import is_plane_net

    if order not in L2_SIDE_ORDERS:
        raise ConstructError(f"order={order!r} is not one of {L2_SIDE_ORDERS}")
    if port_side not in L2_PORT_SIDES:
        raise ConstructError(
            f"port_side={port_side!r} is not one of {L2_PORT_SIDES}")
    by_name = {u.name: u for u in units}
    if anchor not in by_name:
        raise ConstructError(f"the anchor unit {anchor!r} is not in the unit "
                             f"list")
    host = by_name[anchor]
    anchor_nets = set(host.nets)
    bindings = bindings or {}
    by_side: dict = {side: [] for side in SIDES}
    skipped: list = []
    chosen: list = []
    for unit in sorted(units, key=lambda u: u.name):
        if unit.name == anchor:
            continue
        shared = sorted(set(unit.nets) & anchor_nets)
        if not shared:
            skipped.append({"ref": unit.name, "why": "ring2",
                            "detail": "shares no net with the anchor unit -- "
                                      "it goes to ring 2 against a sub-anchor "
                                      "unit"})
            continue
        free = [net for net in shared if not is_plane_net(net)]
        net = sorted(free or shared)[0]
        host_port = sorted(host.ports_on(net), key=port_rank)[0]
        mine = sorted(unit.ports_on(net), key=lambda p: p.key)[0]
        side = host_port.side
        _axis, _sign, along = _AXES[side]
        key = ((round(host_port.x_mm if along == 0 else host_port.y_mm, 6),
                _pad_key(host_port.number), unit.name)
               if order == "port_edge" else (0.0, (0, 0, ""), unit.name))
        klass, klass_shared, why = _l2_class(unit, anchor_nets, bindings)
        by_side[side].append(Neighbour(
            ref=unit.name, anchor_pad=host_port.number, net=net, side=side,
            klass=klass, priority=_CLASS_RANK[klass] + len(klass_shared),
            order_key=key, anchor_pads_on_net=len(host.ports_on(net)),
            members=tuple(unit.refs),
            reason=f"{why}; shares {shared} with {anchor}; the anchor unit's "
                   f"port {host_port.number} carries {net} and leaves on "
                   f"{side} (by {host_port.source}); this unit answers on "
                   f"{mine.number} ({mine.side}, {mine.access or 'n/a'})"))
        chosen.append({"unit": unit.name, "net": net, "shared": shared,
                       "plane_free": free, "side": side,
                       "anchor_port": host_port.number,
                       "anchor_port_source": host_port.source,
                       "own_port": mine.number, "own_side": mine.side,
                       "own_access": mine.access, "klass": klass})
    for side in SIDES:
        by_side[side] = tuple(sorted(by_side[side],
                                     key=lambda n: n.order_key))
    if not any(by_side.values()):
        raise ConstructError(
            f"every one of the four side lists of {anchor!r} is EMPTY -- an "
            f"instrument that can observe nothing must raise (rule 8)")
    return by_side, tuple(skipped), tuple(chosen)


def l2_side_demand(units, anchor: str, by_side, *, geometries, fab,
                   assignment: str = "favored", edge_gap: str = "edge_gap",
                   order: str = "port_edge") -> dict:
    """⭐⭐ **S9 STAGE D2 -- P-E at L2, and it is a DESIGN DECISION rather than a
    promotion.**

    ⛔⛔ At L1, P-E (:func:`side_demand`) re-assigns a neighbour to a side the
    **anchor's escape map** already calls ``ACCESSIBLE``. At L2 there is no such
    map to consult: the side comes from ``host_port.side`` in
    :func:`l2_side_lists`, and a unit typically has **several ports on one net**.
    So the L2 analogue is *"choose a different PORT of the anchor unit on the
    same net"*, keyed on side load -- a rule that did not exist, which is why S9
    scopes it apart from D1 and lets bail-out 5 defer it without blocking the
    ring.

    ⛔ The safety property P-E owns at L1 survives by construction here: the
    candidate sides are exactly the sides the anchor unit **already has a port
    for on the bound net**, so nothing is offered a side the netlist does not
    reach. Ties break by :func:`port_rank`, then by the side letter.

    ⛔ Under ``"favored"`` the returned ``by_side`` is the input, **unchanged and
    the same objects**, and every neighbour still gets a row -- a policy that
    reports only what it moved is the observes-nothing defect (standing
    finding 1).

    Returns ``{"by_side", "rows", "loads", "packed", "assignment",
    "edge_gap_mm"}`` -- the same shape :func:`side_demand` returns, so one
    reader serves both levels.
    """
    if assignment not in SIDE_ASSIGNMENT:
        raise ConstructError(
            f"assignment={assignment!r} is not one of {SIDE_ASSIGNMENT}")
    by_name = {unit.name: unit for unit in units}
    if anchor not in by_name:
        raise ConstructError(f"the anchor unit {anchor!r} is not in the unit "
                             f"list")
    host = by_name[anchor]
    gap = arc_gap_mm(fab, edge_gap) if fab is not None else 0.0
    every = [n for side in SIDES for n in by_side[side]]
    if not every:
        raise ConstructError(
            f"{anchor!r}: every one of the four L2 side lists is EMPTY -- a "
            f"demand table over nothing is indistinguishable from one that "
            f"found everything (standing finding 1)")

    def _ext(neighbour, side):
        geometry = (geometries or {}).get(neighbour.ref)
        if geometry is None:
            raise ConstructError(
                f"l2_side_demand cannot balance {neighbour.ref!r} without its "
                f"unit geometry (standing finding 6)")
        return _edge_extent(geometry, neighbour.ref, side, 0)

    rows: list = []
    if assignment == "favored":
        for side in SIDES:
            for neighbour in by_side[side]:
                rows.append({
                    "ref": neighbour.ref, "from_side": side, "to_side": side,
                    "moved": False, "reason": SIDE_DEMAND_REASONS[0],
                    "access": "FAVORED", "cost": None, "escapable": [side],
                    "anchor_port_before": neighbour.anchor_pad,
                    "anchor_port_after": neighbour.anchor_pad,
                    "why": f"the anchor unit's port {neighbour.anchor_pad} "
                           f"carries {neighbour.net} and leaves on {side}; no "
                           f"other port is consulted"})
        loads = {side: len(by_side[side]) for side in SIDES}
        packed = {side: (round(sum(_ext(n, side) for n in by_side[side])
                               + max(0, len(by_side[side]) - 1) * gap, 6)
                         if by_side[side] and geometries else 0.0)
                  for side in SIDES}
        return {"by_side": {side: tuple(by_side[side]) for side in SIDES},
                "rows": rows, "loads": loads, "packed": packed,
                "assignment": assignment, "edge_gap_mm": gap}

    loads = {side: 0 for side in SIDES}
    packed = {side: 0.0 for side in SIDES}
    favoured_load = {side: len(by_side[side]) for side in SIDES}
    chosen: dict = {}
    for neighbour in sorted(every, key=lambda n: (-n.priority, n.order_key,
                                                  n.ref)):
        ports = sorted(host.ports_on(neighbour.net), key=port_rank)
        #: ⛔ One candidate per SIDE -- the best-ranked port on it. Two ports on
        #: one side are not two choices, and counting them as such would let a
        #: side with many ports win a tie it did not earn.
        best_port: dict = {}
        for rank, port in enumerate(ports):
            best_port.setdefault(port.side, (rank, port))
        favoured = neighbour.side
        if favoured not in best_port:
            # ⚠ The bound port's side is always among the candidates by
            # construction; if it ever is not, the neighbour keeps what it has
            # and the row says so rather than the loop guessing.
            rows.append({"ref": neighbour.ref, "from_side": favoured,
                         "to_side": favoured, "moved": False,
                         "reason": SIDE_DEMAND_REASONS[0], "access": "FAVORED",
                         "cost": None, "escapable": sorted(best_port),
                         "anchor_port_before": neighbour.anchor_pad,
                         "anchor_port_after": neighbour.anchor_pad,
                         "why": "the bound port's own side is not among the "
                                "anchor unit's ports on this net -- kept"})
            chosen[neighbour.ref] = (favoured, None)
            packed[favoured] += _ext(neighbour, favoured) + (
                gap if loads[favoured] else 0.0)
            loads[favoured] += 1
            continue
        if len(best_port) == 1:
            side, reason = favoured, SIDE_DEMAND_REASONS[1]
            why = (f"the anchor unit's port(s) on {neighbour.net} all leave on "
                   f"{side}, so there is nothing to balance")
        elif favoured_load[favoured] <= 1:
            side, reason = favoured, SIDE_DEMAND_REASONS[2]
            why = (f"{favoured} carries {favoured_load[favoured]} bound "
                   f"neighbour(s); a side that is not crowded keeps its own")
        else:
            best = None
            for candidate in sorted(best_port):
                rank, _port = best_port[candidate]
                after = packed[candidate] + _ext(neighbour, candidate) + (
                    gap if loads[candidate] else 0.0)
                key = (round(after, 6), rank, candidate)
                if best is None or key < best[0]:
                    best = (key, candidate)
            side, reason = best[1], SIDE_DEMAND_REASONS[3]
            why = (f"{favoured} is crowded ({favoured_load[favoured]} bound "
                   f"neighbours); {side} minimises (packed-after "
                   f"{round(best[0][0], 4)} mm, port rank {best_port[side][0]}, "
                   f"side letter)")
        port = best_port[side][1]
        chosen[neighbour.ref] = (side, port)
        packed[side] += _ext(neighbour, side) + (gap if loads[side] else 0.0)
        loads[side] += 1
        rows.append({
            "ref": neighbour.ref, "from_side": favoured, "to_side": side,
            "moved": bool(side != favoured), "reason": reason,
            "access": ("FAVORED" if best_port[side][0] == 0 else "ACCESSIBLE"),
            "cost": None, "escapable": sorted(best_port),
            "anchor_port_before": neighbour.anchor_pad,
            "anchor_port_after": port.number,
            "packed_before_mm": {s: round(packed[s], 6) for s in SIDES},
            "why": why})

    rebuilt: dict = {side: [] for side in SIDES}
    for neighbour in every:
        side, port = chosen[neighbour.ref]
        if side == neighbour.side or port is None:
            rebuilt[side].append(neighbour)
            continue
        _axis, _sign, along = _AXES[side]
        key = ((round(port.x_mm if along == 0 else port.y_mm, 6),
                _pad_key(port.number), neighbour.ref)
               if order == "port_edge" else (0.0, (0, 0, ""), neighbour.ref))
        rebuilt[side].append(replace(
            neighbour, side=side, origin_side=neighbour.side,
            anchor_pad=port.number, order_key=key,
            reason=f"{neighbour.reason}; SIDE DEMAND (L2) -- re-bound to the "
                   f"anchor unit's port {port.number} on {neighbour.net}, "
                   f"moving from {neighbour.side} to {side} and re-projecting "
                   f"onto the {side} edge"))
    return {"by_side": {side: tuple(sorted(rebuilt[side],
                                           key=lambda n: n.order_key))
                        for side in SIDES},
            "rows": sorted(rows, key=lambda r: (r["to_side"], r["ref"])),
            "loads": loads, "packed": {s: round(packed[s], 6) for s in SIDES},
            "assignment": assignment, "edge_gap_mm": gap}


# --------------------------------------------------------------------------- #
# ⭐⭐⭐ The stranger's wire
# --------------------------------------------------------------------------- #
def _clip_segment(seg, rect):
    """Liang-Barsky. ``(t0, t1)`` of the segment inside ``rect``, or ``None``."""
    (x0, y0), (x1, y1) = seg[0], seg[1]
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - rect[0]), (dx, rect[2] - x0),
                 (-dy, y0 - rect[1]), (dy, rect[3] - y0)):
        if abs(p) < _TOL:
            if q < -_TOL:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
    return None if t0 > t1 + _TOL else (t0, t1)


def stranger_crossings(state, pairs, *, channels, ring2_refs,
                       routed_ok=None, phase: str = "", lines=None) -> tuple:
    """⭐⭐⭐ **Open question 5's last half, measured for the first time.**

    A ``channel`` is ``(unit, side, band_rect, allowance_mm, occupants)``: the
    rectangle between one unit's **courtyard** edge on one side and the
    neighbours it is holding at arm's length there -- i.e. the fanout allowance
    the standoff exists to protect. A **stranger** is a pair line belonging to a
    unit that is neither the channel's owner nor one of its occupants, and which
    enters that rectangle.

    ``overlap_mm`` is *how far past the outer boundary of the band the line
    reaches*, so 0.0 means it grazed the far edge and ``allowance_mm`` means it
    reached the owner's own courtyard.

    ⛔⛔ **It is the PAIR LINE, not the copper** (see :class:`StrangerCrossing`).
    A real router jogs; this proxy can over-report and under-report, and every
    number must be quoted as *"the pair line enters the channel"*.

    ⚠ **"No stranger ever entered any channel" is a RESULT**, not a failure: it
    would mean the fanout allowance is still untested even at L2 and overview
    open question 5's last half stays open. ⛔ It must not be reported as *"the
    allowance works"*.
    """
    # ⚠ ``lines`` is an override for a caller that has already computed the pair
    # lines with the same instrument. ⛔ It is **not** a second derivation: the
    # default is ``_pair_lines`` and a caller that passes anything else is
    # asserting it built the lines the same way.
    lines = (list(lines) if lines is not None
             else _pair_lines(state, pairs, set(ring2_refs)))
    routed_ok = set(routed_ok if routed_ok is not None
                    else (line[2] for line in lines))
    out: list = []
    for owner, side, rect, allowance, occupants in channels:
        axis, sign, _along = _AXES[side]
        edge = rect[axis + 2] if sign > 0 else rect[axis]
        survived = all(ref in routed_ok for ref in occupants)
        for line in lines:
            ref = line[2]
            if ref == owner or ref in set(occupants):
                continue
            clipped = _clip_segment(line, rect)
            if clipped is None:
                continue
            t0, t1 = clipped
            depths = []
            for t in (t0, t1):
                x = line[0][0] + t * (line[1][0] - line[0][0])
                y = line[0][1] + t * (line[1][1] - line[0][1])
                depths.append(abs((x if axis == 0 else y) - edge))
            out.append(StrangerCrossing(
                channel_of=str(owner), side=side, intruder=str(ref),
                net="", overlap_mm=round(max(0.0, allowance - min(depths)), 6),
                still_routed=bool(survived),
                allowance_mm=round(float(allowance), 6),
                band=tuple(round(v, 6) for v in rect), phase=phase))
    return tuple(sorted(out, key=lambda s: (s.channel_of, s.side, s.intruder)))


def _channels_for(state, anchor_name, laid, ring2_side, *,
                  allowance_mm: float) -> list:
    """``(owner, side, band, allowance, occupants)`` for every populated side.

    ⛔ The band spans the **owner's own courtyard extent** along that edge and
    reaches ``allowance_mm`` outward from its courtyard face -- that is exactly
    what the standoff's ``fanout_allowance_mm`` term buys, and measuring it on
    any other span would price a different thing.
    """
    channels: list = []
    holders: dict = {}
    for side, members in sorted(laid.items()):
        refs = [m.ref if hasattr(m, "ref") else str(m) for m in members]
        if refs:
            holders.setdefault(anchor_name, {})[side] = refs
    for (owner, side), refs in sorted(ring2_side.items()):
        holders.setdefault(owner, {})[side] = list(refs)
    for owner, sides in sorted(holders.items()):
        if owner not in state.boxes_court:
            continue
        box = state.boxes_court[owner]
        for side, refs in sorted(sides.items()):
            axis, sign, along = _AXES[side]
            face = box[axis + 2] if sign > 0 else box[axis]
            lo, hi = sorted((face, face + sign * allowance_mm))
            rect = ((lo, box[1], hi, box[3]) if axis == 0
                    else (box[0], lo, box[2], hi))
            channels.append((owner, side, rect, allowance_mm, tuple(refs)))
    return channels


# --------------------------------------------------------------------------- #
# The blame column -- ⛔ RECORDED, never steered on
# --------------------------------------------------------------------------- #
class _ConstructedBoardView:
    """The board **as the loop built it**, for :mod:`~skidl_layout.reachability`.

    ⛔⛔ **The parsed board holds PARK positions, and blaming geometry against
    parked copper would measure the park grid.** ``reachability.slack_field``
    rasterises ``pcb.footprints[*].pads``, so a verdict taken against
    ``session.pcb`` would be about a board nobody built. This view carries the
    same nets and board info and the **constructed** pads.

    ⚠ Segments and vias are empty on purpose: this arc commits **no routed
    copper** (overview 7.5), so a view that invented some would be optimistic in
    the one direction the record keeps paying for.
    """

    __slots__ = ("nets", "board_info", "footprints", "segments", "vias")

    def __init__(self, pcb, state, extra=None):
        self.nets = pcb.nets
        self.board_info = pcb.board_info
        self.segments = []
        self.vias = []
        self.footprints = {
            ref: _UnitFootprint(pads=tuple(state.pads(ref)))
            for ref in sorted(state.entries)}
        # ⛔ ``extra`` is the ROLLED-BACK unit's pads. Stamping them into the
        # session is not enough and the difference is silent: this view is built
        # from ``state``, and a unit that failed the ladder is not in it.
        for name, pads in sorted((extra or {}).items()):
            self.footprints[name] = _UnitFootprint(pads=tuple(pads))


def _blame(session, state, *, xy, net, fab, layers, margin_mm=2.0,
           other_xy=None, extra=None) -> dict:
    """``explain_route_failure`` at one position, or a recorded reason it could
    not be asked. ⛔ **A column, never a decision** -- the ladder's behaviour is
    unchanged (plan section 5.8): *a stage that both adds an instrument and
    changes the loop it measures can attribute neither.*

    ⚠ 6.7 s at ``margin_mm=4.0`` and 0.94 s at 2.0 (measured), so it is called
    **on failures only** and never per candidate position.

    ⭐⭐ ``other_xy`` is the **other end of the pair**, and passing it is what
    turns the verdict from ``UNDETERMINED`` into an answer. Measured on
    ``stm32_bluepill``'s one L2 failure: at a 2 mm box round the seed the
    instrument correctly reported *"no other island of this net is inside the
    view"* -- the question was never asked, which is the observes-nothing
    failure wearing a geometry hat. A view that contains **both** endpoints is
    the view the question is actually about.
    """
    try:
        from .reachability import explain_route_failure

        view = None
        if other_xy is not None:
            view = (min(xy[0], other_xy[0]) - margin_mm,
                    min(xy[1], other_xy[1]) - margin_mm,
                    max(xy[0], other_xy[0]) + margin_mm,
                    max(xy[1], other_xy[1]) + margin_mm)
        return explain_route_failure(
            _ConstructedBoardView(session.pcb, state, extra),
            (float(xy[0]), float(xy[1])),
            fab=fab, net_name=str(net), layers=tuple(layers),
            margin_mm=float(margin_mm), view=view)
    except Exception as exc:                                   # noqa: BLE001
        # ⛔ Rule 8's shape: a blame that could not be computed says so, and is
        # never silently recorded as "router".
        return {"verdict": "UNDETERMINED", "blame": "unknown",
                "note": f"{type(exc).__name__}: {exc}"}


def _blame_failure(session, state, footprint, failure, anchor_pad, *, net, fab,
                   layers, margin_mm: float = 2.0) -> dict:
    """⭐⭐ **The blame column, asked as a question that can be answered.**

    ⛔⛔ **A unit that failed the ladder has been ROLLED BACK, so its copper is
    not on the board -- and ``pad_reachability`` asks "can this pad reach the
    REST OF ITS NET".** Measured on ``stm32_bluepill``'s one L2 failure: with
    only the anchor's own pad in the view the instrument correctly answered
    ``UNDETERMINED`` -- *"no other island of this net is inside the view"* --
    which is the honest answer to a question nobody asked. So the failed unit is
    **re-stamped at the last position the ladder tried**, the view is taken over
    **both** endpoints, and the stamp is removed again.

    ⛔ It is still a **column, never a decision**: the ladder has already run and
    its behaviour is unchanged (plan section 5.8).
    """
    attempts = list(failure.get("attempts") or ())
    if anchor_pad is None or not attempts:
        return {"verdict": "UNDETERMINED", "blame": "unknown",
                "note": "no anchor pad or no ladder attempt to blame"}
    last = attempts[-1]
    pads = moved_pads(list(footprint.pads), x_mm=float(last["x_mm"]),
                      y_mm=float(last["y_mm"]), rot_deg=int(last["rot"]),
                      base_rot_deg=0.0)
    mine = [pad for pad in pads
            if str(getattr(pad, "net_name", "") or "") == str(net)]
    if not mine:
        return {"verdict": "UNDETERMINED", "blame": "unknown",
                "note": f"the unit carries no pad on {net!r} to seed from"}
    token = session.add_part(f"@BLAME:{failure.get('ref')}", pads)
    try:
        out = _blame(session, state, xy=(anchor_pad.global_x,
                                         anchor_pad.global_y),
                     net=net, fab=fab, layers=layers, margin_mm=margin_mm,
                     other_xy=(mine[0].global_x, mine[0].global_y),
                     extra={f"@BLAME:{failure.get('ref')}": pads})
    finally:
        session.remove_part(token)
    out["asked_at"] = {"anchor_pad": [round(anchor_pad.global_x, 4),
                                      round(anchor_pad.global_y, 4)],
                       "unit_pad": [round(mine[0].global_x, 4),
                                    round(mine[0].global_y, 4)],
                       "ladder_rung": last.get("rung"),
                       "note": "the failed unit was re-stamped at the last "
                               "position the ladder tried, so the question has "
                               "two endpoints; the stamp was removed again"}
    return out


def _parts_accounting(partition, units, placements, ring2, failures,
                      ring2_failures, skipped, host) -> dict:
    """⭐⭐⭐ **S10 §2.** L2's accounting, totalled over **parts** not unit names.

    ⛔⛔ **The blindness this exists to end, stated as the mechanism rather than
    as the instance.** :func:`construct_board`'s ``accounting`` universe is
    ``{unit.name for unit in units}``, and :func:`unit_from_cell` builds a unit
    out of its cell's **placements** -- so a member the L1 ladder refused is not
    in ``unit.refs``, is not in the universe, and cannot be in ``unaccounted``.
    The board loses a part and every counter reads ``total: true``. MEASURED on
    ``lt3758_iso_flyback``: ``RLED`` is a member of ``ic:U1`` sharing **no net**
    with any other member, the cell skips it by name at ring 2 (correctly), and
    L2 never hears of it.

    ⛔ ``partition`` is the universe on purpose. Deriving it from the units
    would reproduce the same blindness one level out: a part no unit mentions
    would define itself out of existence.

    The four ways a part may be accounted for, and nothing else counts:

    1. it is a member of a unit that **landed** (on a side, in ring 2, or as the
       anchor unit itself -- the anchor is placed by definition and is not in
       the loop's universe);
    2. it is a member of a unit the loop **failed** to place, which is a named
       failure;
    3. it is a member of a unit the loop **skipped**, likewise named;
    4. it arrived in ``unit_skips`` -- typically no footprint geometry, which is
       standing finding 6 and is legitimately not placeable.

    ⚠ A non-empty ``unaccounted_parts`` is a **finding, not automatically a
    failure**; what would be a defect is the number not existing.
    """
    universe = {str(ref) for group in partition.groups for ref in group.refs}
    by_name = {unit.name: unit for unit in units}
    landed_names = set(placements) | set(ring2) | {host.name}
    failed_names = ({f["ref"] for f in failures}
                    | {f["ref"] for f in ring2_failures})
    skipped_names = {s["ref"] for s in skipped}

    def _refs(names):
        return {str(ref) for name in names
                for ref in getattr(by_name.get(name), "refs", ())}

    landed = _refs(landed_names) & universe
    failed = _refs(failed_names) & universe
    skipped_refs = _refs(skipped_names) & universe
    # ⛔ ``unit_skips`` rows name a REF directly (the ``__skip__`` tuples
    # ``board_units`` emits), so they are read as parts and not as unit names.
    named_by_skip = {str(s["ref"]) for s in skipped} & universe
    accounted = landed | failed | skipped_refs | named_by_skip
    # ⭐ The part the cell dropped is visible here whether or not
    # ``orphan_units`` re-emitted it, which is the point: with the flag ON it
    # lands and shows up in ``in_a_landed_unit``; with it OFF it shows up in
    # ``unaccounted_parts``. Neither reading depends on the flag being set.
    claimed_twice = sorted(
        ref for ref in universe
        if sum(1 for unit in units if ref in set(unit.refs)) > 1)
    return {
        "universe_size": len(universe),
        "in_a_landed_unit": len(landed),
        "in_a_failed_unit": sorted(failed - landed),
        "in_a_skipped_unit": sorted((skipped_refs | named_by_skip) - landed),
        "unaccounted_parts": sorted(universe - accounted),
        "total": not (universe - accounted),
        # ⛔ The OTHER way the two levels disagree, and S8 found it the hard
        # way: a part claimed by two units is placed twice and ends up wherever
        # the alphabetically-later one put it.
        "claimed_by_more_than_one_unit": claimed_twice,
        "note": "the universe is the PARTITION's parts; deriving it from the "
                "units would reproduce the blindness one level out",
    }


# --------------------------------------------------------------------------- #
# ⭐⭐⭐ construct_board -- the L2 loop
# --------------------------------------------------------------------------- #
def construct_board(partition, *, units, session, geometries, footprints, fab,
                    escape_maps=None, board: str = "",
                    anchor_x_mm: float = 0.0, anchor_y_mm: float = 0.0,
                    anchor_rule: str = "largest_cell",
                    unit_source: str = "constructed",
                    port_side: str = "member_escape",
                    connector_policy: str = "loop_placed",
                    side_order_policy: str = "port_edge",
                    hand_positions=None,
                    centre_side_lists: bool = True,
                    ring2_avoids_anchor_side: bool = True,
                    tighten: bool = True, tighten_floor: str = "allowance",
                    side_assignment: str = "favored",
                    ring: bool = False,
                    arc_edge_gap: str = "edge_gap",
                    corner_owner: str = "none",
                    spawn_factor: float = 1.0,
                    tighten_mode: str = "part",
                    shrink_step_mm: float | None = None,
                    r_max_factor: float = 4.0,
                    plane_fallback: bool = False,
                    route: bool = True, nets_by_ref=None,
                    blame: bool = True, blame_layers=(),
                    unit_skips=(), commit_copper: bool = False) -> BoardResult:
    """⭐⭐⭐ **L2 -- the board. Overview section 3's third level, P1-P8 one level
    up.**

    ⛔⛔⛔ **The same functions, not the same shape.** ``_place_one``,
    ``_choose_rotation``, ``_position_for``, ``_CellState``, ``_tighten_cell``,
    ``_centre_sides``, ``_reroute_all``, ``pair_crossings`` and ``side_span`` are
    called **unchanged**; the only new thing between L1 and L2 is the
    :class:`Unit` that presents a composite of parts as one geometry and one pad
    list. That is the answer to overview section 3's *"is L2 really the same
    loop?"* [CALL], and gate ``BD3`` checks it rather than trusting it.

    ⛔ **The anchor unit does not move** (standing finding 21, rule 2): it is
    stamped at ``(anchor_x_mm, anchor_y_mm)``, which is where the parsed board
    has its anchor IC, and every gate asserts its members are untouched.

    ⛔ **The fanout allowance is UNCHANGED at 2.0048 mm** (plan section 5.5).
    Changing it and measuring whether it survives in the same run would make the
    answer unattributable, and *"does 2.0048 mm survive a stranger's wire"* is
    this stage's headline question.

    ⛔ ``overflow_to_free_side`` (P-C) and ``busy_anchor_keepout`` (P-B) are
    **not offered at L2**: P-C's price is cross-side crossings 0 -> 11/14/0/21
    and that is precisely the category L2 has less room for (open question 17),
    and P-B's top range (12.667 mm) is *"too much"* by S5's own report with its
    cap still an open decision.

    ⭐⭐⭐ **S9 STAGE D -- THE RING, PROMOTED.** Until 2026-08-05 this function
    carried P-A (``centre_side_lists``), P-D (``ring2_avoids_anchor_side``) and,
    since S8, template units -- and **none** of S5C's arc: every S7 and S8 render
    is the straight-column construction, so no sentence about *"the ring at L2"*
    could have meant anything. ``ring`` / ``arc_edge_gap`` / ``corner_owner`` /
    ``spawn_factor`` / ``r_max_factor`` / ``tighten_mode`` / ``shrink_step_mm``
    (P-F + P-H) and ``side_assignment`` (P-E, scoped apart because at L2 the side
    comes from ``host_port.side`` rather than from an escape map) are now here,
    **every one defaulting to today's L2 behaviour**.

    ⛔ It is a **promotion, not a re-implementation**: :func:`ring_radius`,
    :func:`ring_slots` and :func:`shrink_ring` are called unchanged, keyed on
    ``neighbour.ref`` -- which at L2 **is** the unit name -- with ``unit_geoms``
    where L1 passes ``geometries``. The one genuinely new piece is an L2
    ``_pre_rotation`` mirroring L1's, and it is mandatory rather than tidy: a
    rotation chosen *after* the radius makes the radius wrong.

    ⭐ ``plane_fallback`` (S9 Stage C) offers a plane-only unit a **labelled**
    plane-net sub-anchor at ring 2 instead of a named skip. Default OFF.
    """
    from .validator import validate

    for name, value, declared in (("anchor_rule", anchor_rule,
                                   L2_ANCHOR_RULES),
                                  ("unit_source", unit_source, UNIT_SOURCE),
                                  ("port_side", port_side, L2_PORT_SIDES),
                                  ("connector_policy", connector_policy,
                                   CONNECTOR_POLICIES),
                                  ("side_order_policy", side_order_policy,
                                   L2_SIDE_ORDERS),
                                  ("tighten_floor", tighten_floor,
                                   TIGHTEN_FLOORS),
                                  ("side_assignment", side_assignment,
                                   SIDE_ASSIGNMENT),
                                  ("arc_edge_gap", arc_edge_gap, ARC_EDGE_GAP),
                                  ("corner_owner", corner_owner, CORNER_OWNER),
                                  ("tighten_mode", tighten_mode,
                                   TIGHTEN_MODE)):
        if value not in declared:
            raise ConstructError(f"{name}={value!r} is not one of {declared}")
    # ⛔ Checked HERE rather than in the P7 block, so an inconsistent arm fails
    # before a single part is placed: a run that builds a whole board and then
    # refuses to tighten it has already spent the measurement.
    if tighten_mode == "ring" and not ring:
        raise ConstructError(
            "tighten_mode='ring' needs ring=True -- there is no radius to "
            "shrink on the linear L2 construction, and silently running the "
            "per-part pass instead would make the arm unattributable")

    nets_by_ref = nets_by_ref or partition.meta["nets_by_ref"]
    units = tuple(units)
    by_name = {unit.name: unit for unit in units}
    #: ⛔ S9 Stage C's key prefers a unit's own partition group; at L2 that is
    #: :attr:`Unit.group`, read once and never from arrival order.
    group_of_unit = {unit.name: str(unit.group or "") for unit in units}
    anchor = l2_anchor(units, rule=anchor_rule)
    host = by_name[anchor]
    bindings = {b.ref: b for group in partition.groups for b in group.bindings}
    base = standoff_base_mm(fab)
    gap = edge_gap_mm(fab)
    clearance = float(fab.clearance_mm)

    # -- the unit geometries and the unit footprints ------------------------ #
    unit_geoms: dict = {}
    unit_fps: dict = {}
    for unit in units:
        unit_geoms[unit.name] = _unit_geometry(
            unit.name, unit.offsets, geometries,
            (unit.physical_box, unit.courtyard_box))
        pads = []
        for ref, offset in sorted(unit.offsets.items()):
            pads.extend(_member_world_pads(ref, footprints[ref], offset))
        unit_fps[unit.name] = _UnitFootprint(
            pads=tuple(sorted(pads, key=lambda p: _pad_key(p.pad_number))))

    # -- P1/P2/P3 ----------------------------------------------------------- #
    by_side, ring2_candidates, chosen = l2_side_lists(
        units, anchor, port_side=port_side, order=side_order_policy,
        bindings=bindings)
    control_side_lists = l2_side_lists(
        units, anchor, port_side=port_side, order="unit_name",
        bindings=bindings)[0] if side_order_policy == "port_edge" else None
    # -- D2: P-E at L2, BEFORE P3 sums the load ----------------------------- #
    # ⛔ Before, for the reason L1 states: under ``escapable`` a side's load IS
    # what P3 orders by. The OFF arm calls the same function and gets its own
    # input back, which is gate ``PN5``'s control.
    demand = l2_side_demand(units, anchor, by_side, geometries=unit_geoms,
                            fab=fab, assignment=side_assignment,
                            edge_gap=arc_edge_gap, order=side_order_policy)
    if side_assignment != "favored":
        by_side = {side: list(demand["by_side"][side]) for side in SIDES}
    sums = {side: sum(n.priority for n in by_side[side]) for side in SIDES}
    order = tuple(sorted(SIDES, key=lambda s: (-sums[s], s)))
    every = tuple(n for side in SIDES for n in by_side[side])

    # -- the shared world --------------------------------------------------- #
    snapshot = session.snapshot()
    state = _CellState()
    anchor_pads_world = moved_pads(list(unit_fps[anchor].pads),
                                   x_mm=anchor_x_mm, y_mm=anchor_y_mm,
                                   rot_deg=0, base_rot_deg=0.0)
    token = session.add_part(f"@ANCHOR:{anchor}", anchor_pads_world)
    state.put(anchor, x_mm=anchor_x_mm, y_mm=anchor_y_mm, rot_deg=0,
              geometry=unit_geoms[anchor], footprint=unit_fps[anchor],
              token=token)
    anchor_members_before = {
        ref: (round(anchor_x_mm + unit.offsets[ref][0], 6),
              round(anchor_y_mm + unit.offsets[ref][1], 6),
              int(unit.offsets[ref][2]))
        for unit in (host,) for ref in sorted(host.offsets)}
    by_number = {str(pad.pad_number): pad for pad in anchor_pads_world}

    placements: dict = {}
    failures: list = []
    disagreements: list = []
    rungs_used: dict = {}
    pairs: list = []
    timings: list = []
    fixed: list = []

    anchor_court = state.boxes_court[anchor]
    extents = {side: _side_extent(anchor_court, anchor_x_mm, anchor_y_mm, side)
               for side in SIDES}
    cursors: dict = {side: None for side in SIDES}
    laid: dict = {side: [] for side in SIDES}

    # -- ⭐⭐⭐ D1: P-F, the ring, sized BEFORE anything is placed ------------- #
    # ⛔⛔ **The same two-pass scheme L1 states, and it is mandatory rather than
    # tidy.** ``R`` is a function of the units' along-edge courtyard extents,
    # which depend on their **rotations**; ``_choose_rotation`` scores a rotation
    # by the distance from the connecting pad to the anchor's, which depends on
    # the standoff and therefore on ``R``. The circle is broken in one stated
    # place: rotations are chosen at the **shipped** fanout allowance, the ring
    # is sized against those rotations, and ``_place_one`` is handed the same
    # rotation back through ``forced_rot``.
    def _pre_rotation(member, target_side):
        geometry = unit_geoms.get(member.ref)
        footprint = unit_fps.get(member.ref)
        pad = by_number.get(member.anchor_pad)
        if geometry is None or footprint is None or pad is None:
            return None
        _axis, _sign, along = _AXES[target_side]
        aim = (pad.global_x, pad.global_y)
        # ⛔ L2 always aligns the connecting pad (the existing ``_place_one``
        # call passes ``align_connecting_pad=True``), so the offsets are read
        # unconditionally -- a rotation chosen without them would be scored
        # against a placement rule the loop does not use.
        offs = _connecting_pad_offsets(footprint, member, target_side)
        return _choose_rotation(
            member, geometry, footprint, target_side, extents[target_side],
            base, anchor_x_mm, anchor_y_mm, aim[along], aim,
            anchor_x_mm if along == 0 else anchor_y_mm, gap,
            align_offsets=offs, busy_extra=None)

    ring_rot: dict = {}
    ring_info = None
    ring_plan: dict = {}
    place_gap = gap
    if ring:
        for side in SIDES:
            for member in by_side[side]:
                rot = _pre_rotation(member, side)
                if rot is not None:
                    ring_rot[member.ref] = int(rot)
        ring_info = ring_radius(
            by_side, geometries=unit_geoms,
            anchor_geometry=unit_geoms[anchor], fab=fab, anchor_rot_deg=0,
            anchor_x_mm=anchor_x_mm, anchor_y_mm=anchor_y_mm,
            edge_gap=arc_edge_gap, corner_owner=corner_owner,
            rotations=ring_rot, spawn_factor=spawn_factor,
            r_max_factor=r_max_factor)
        place_gap = float(ring_info["edge_gap_mm"])
        # ⛔ ONE box, taken from the sizing pass, so the placement and the shrink
        # cannot disagree about where the anchor is (standing findings 8 and 29,
        # and the 25 mm re-flow bug ``ring_radius``' docstring records).
        ring_box = tuple(ring_info["anchor_box"])
        for side in SIDES:
            row = ring_info["sides"][side]
            ring_plan[side] = ring_slots(
                by_side[side], side=side, radius_mm=row["R_spawn_mm"],
                anchor_box=ring_box, geometries=unit_geoms,
                gap_mm=place_gap, owns=row["owns"], shares=row["shares"],
                reserve_mm=ring_info["reserve_mm"], rotations=ring_rot,
                r_max_mm=row["R_max_mm"])

    def _record(neighbour, outcome, target):
        placement = outcome["placement"]
        placements[neighbour.ref] = placement
        if placement.route_elapsed_s is not None:
            timings.append(placement.route_elapsed_s)
        if placement.routed:
            pairs.append({"ref": neighbour.ref, "neighbour": neighbour,
                          "target_ref": target[0], "target_pad": str(target[1]),
                          "length_mm": placement.route_length_mm})

    skipped = [dict(row) for row in unit_skips]
    for row in ring2_candidates:
        skipped.append(dict(row))

    # -- P3 + P6: one side at a time, finished before the next begins -------- #
    for side in order:
        for neighbour in by_side[side]:
            unit = by_name[neighbour.ref]
            if connector_policy == "hand_fixed" and unit.kind == "connector":
                # ⛔⛔ The refused default, BUILT so the departure from overview
                # section 10 is measurable rather than argued. The position comes
                # from the HAND board and is therefore part of S6's answer key.
                position = (hand_positions or {}).get(unit.anchor)
                if position is None:
                    skipped.append({"ref": neighbour.ref, "side": side,
                                    "why": "connector_policy='hand_fixed' and "
                                           "the hand board names no position "
                                           "for it"})
                    continue
                outcome = _place_fixed(neighbour, unit_geoms[neighbour.ref],
                                       unit_fps[neighbour.ref],
                                       session=session,
                                       anchor_pad=by_number.get(
                                           neighbour.anchor_pad),
                                       position=position,
                                       boxes_phys=state.boxes_phys,
                                       boxes_court=state.boxes_court,
                                       clearance_mm=clearance, route=route)
                fixed.append({"ref": neighbour.ref, "member": unit.anchor,
                              "hand_xy": [round(position[0], 6),
                                          round(position[1], 6)],
                              "placed": outcome["placement"] is not None})
            else:
                row_r = ring_info["sides"][side] if ring_info else None
                slot = (ring_plan.get(side, {}).get("slots", {})
                        .get(neighbour.ref) if ring else None)
                outcome = _place_one(
                    neighbour, unit_geoms[neighbour.ref],
                    unit_fps[neighbour.ref], session=session,
                    anchor_pad=by_number.get(neighbour.anchor_pad), side=side,
                    anchor_x_mm=anchor_x_mm, anchor_y_mm=anchor_y_mm,
                    anchor_extent=extents[side],
                    base=(float(row_r["R_spawn_mm"]) if row_r is not None
                          else base),
                    gap=place_gap,
                    cursor=cursors[side], boxes_phys=state.boxes_phys,
                    boxes_court=state.boxes_court, placed_parts=[],
                    clearance_mm=clearance, route=route,
                    align_connecting_pad=True,
                    slot=slot, allowance=(base if ring else None),
                    # ⚠ A unit the ring never sized a slot for falls back to the
                    # cursor rather than being handed someone else's; it is
                    # named in ``meta["ring"]["unslotted"]``, never silent.
                    forced_rot=(ring_rot.get(neighbour.ref)
                                if ring and slot is not None else None),
                    commit_copper=commit_copper)
            for rung in outcome["rungs"]:
                rungs_used[rung] = rungs_used.get(rung, 0) + 1
            if outcome["placement"] is None:
                row = dict(outcome["failure"], ring=1, side=side,
                           members=list(unit.refs))
                if blame and route:
                    row["blame"] = _blame_failure(
                        session, state, unit_fps[neighbour.ref],
                        outcome["failure"], by_number.get(
                            neighbour.anchor_pad),
                        net=neighbour.net, fab=fab, layers=blame_layers)
                failures.append(row)
                continue
            placement = outcome["placement"]
            state.put(neighbour.ref, x_mm=placement.x_mm, y_mm=placement.y_mm,
                      rot_deg=placement.rot_deg,
                      geometry=unit_geoms[neighbour.ref],
                      footprint=unit_fps[neighbour.ref],
                      token=outcome["token"], side=side, subanchor=anchor,
                      standoff_mm=placement.standoff_mm,
                      allowance_mm=_allowance_of(placement))
            _record(neighbour, outcome, (anchor, neighbour.anchor_pad))
            cursors[side] = outcome["cursor"]
            laid[side].append(neighbour)
            own = next((p for p in unit.ports_on(neighbour.net)), None)
            if own is not None:
                access = own.access
                if bool(access == "BLOCKED") != (not placement.routed):
                    disagreements.append({
                        "ref": neighbour.ref, "ring": 1, "member": own.member,
                        "anchor_pad": own.pad, "side": side,
                        "map_says": access or "n/a",
                        "router_says": ("routed" if placement.routed
                                        else "unrouted"),
                        "direction": ("the map was OPTIMISTIC"
                                      if not placement.routed
                                      else "the map UNDER-REPORTED")})
    for side in SIDES:
        by_side[side] = tuple(by_side[side])

    # -- ⛔⛔ S7 C4: THE COMMITTED COPPER COMES OFF BEFORE ANYTHING MOVES ----- #
    # Every pass after this one (P-A centring, the tighten, the ring shrink,
    # the whole-cell re-route) MOVES a part, and copper committed at a position
    # a part no longer occupies is a wall nobody built. ⭐ So the commits are
    # popped in reverse and the census is ASSERTED back -- which is exactly the
    # invariant gate ``RT4`` proves in the small, run here in the large.
    # ⛔ ``exact=False`` here is plan BAIL-OUT C: record the trace, do not work
    # around it.
    commit_log = {"enabled": bool(commit_copper), "uncommitted": 0,
                  "exact": True}
    if commit_copper:
        commit_log = dict(session.uncommit_all(), enabled=True)

    # -- P-A ---------------------------------------------------------------- #
    centring: list = []
    if centre_side_lists:
        centring = _centre_sides(session, state=state, order=order, laid=laid,
                                 pairs=pairs, anchor_box=anchor_court,
                                 clearance_mm=clearance, route=route)

    # -- ring 2 ------------------------------------------------------------- #
    nets_by_unit = {unit.name: list(unit.nets) for unit in units}
    ring2: dict = {}
    ring2_failures: list = []
    ring2_order: list = []
    ring2_cursor: dict = {}
    ring2_side_choices: list = []
    ring2_side: dict = {}
    plane_fallbacks: list = []
    for row in ring2_candidates:
        ref = row["ref"]
        unit = by_name[ref]
        chosen_sub = ring2_subanchor(ref, sorted(state.entries), nets_by_unit,
                                     plane_fallback=plane_fallback,
                                     group_of=group_of_unit)
        if chosen_sub is None:
            ring2_failures.append({
                "ref": ref, "ring": 2,
                "why": ("no placed unit shares a plane net with it either, so "
                        "there is no sub-anchor -- skipped rather than guessed"
                        if plane_fallback else
                        "no placed unit shares a plane-free net with it, so "
                        "there is no sub-anchor -- skipped rather than "
                        "guessed")})
            continue
        sub, shared = chosen_sub[0], chosen_sub[1]
        via = chosen_sub[2] if len(chosen_sub) > 2 else "plane_free"
        if via == "plane_fallback":
            plane_fallbacks.append({"ref": ref, "subanchor": sub,
                                    "shared_plane_nets": list(shared),
                                    "via": via})
        sub_unit = by_name[sub]
        sub_entry = state.entries[sub]
        # ⛔ S9 Stage C: the first shared net **both** units carry a port on. A
        # unit's plane pins are ports whenever some outside part carries the
        # net, which is true of every plane net on this corpus -- but *"is true
        # on this corpus"* is not a guarantee, so a shared net with no port on
        # either side is a NAMED skip rather than an IndexError.
        net = next((candidate for candidate in sorted(shared)
                    if sub_unit.ports_on(candidate)
                    and unit.ports_on(candidate)), None)
        if net is None:
            ring2_failures.append({
                "ref": ref, "ring": 2, "subanchor": sub, "via": via,
                "why": f"the sub-anchor {sub} and {ref} share {sorted(shared)} "
                       f"but no net of it has a port on both units -- skipped "
                       f"rather than guessed"})
            continue
        sub_port = sorted(sub_unit.ports_on(net), key=port_rank)[0]
        mine = sorted(unit.ports_on(net), key=lambda p: p.key)[0]
        side = _rotate_side(sub_port.side, int(sub_entry["rot_deg"]))
        if ring2_avoids_anchor_side and sub != anchor:
            forbidden = _anchor_direction(state.boxes_phys[sub], anchor_x_mm,
                                          anchor_y_mm)
            picked = _l2_side_excluding(sub_unit, net, int(sub_entry
                                                           ["rot_deg"]),
                                        forbidden)
            ring2_side_choices.append({
                "ref": ref, "subanchor": sub, "net": net,
                "anchor_lies": forbidden, "side_without_policy": side,
                "side": picked or side,
                "pointed_at_the_anchor": side == forbidden,
                "changed": bool(picked is not None and picked != side)})
            if picked is not None:
                side = picked
        elif ring2_avoids_anchor_side:
            ring2_side_choices.append({
                "ref": ref, "subanchor": sub, "net": net, "anchor_lies": None,
                "side_without_policy": side, "side": side,
                "pointed_at_the_anchor": False, "changed": False,
                "why": "the sub-anchor IS the anchor unit -- there is nothing "
                       "to exclude (open question 15's known leftover)"})
        klass, klass_shared, why = _l2_class(unit, set(sub_unit.nets), bindings)
        _axis, _sign, along = _AXES[side]
        member = Neighbour(
            ref=ref, anchor_pad=sub_port.number, net=net, side=side,
            klass=klass, priority=_CLASS_RANK[klass] + len(klass_shared),
            order_key=(round(sub_port.x_mm if along == 0 else sub_port.y_mm, 6),
                       _pad_key(sub_port.number), str(ref)),
            anchor_pads_on_net=len(sub_unit.ports_on(net)),
            members=tuple(unit.refs),
            reason=f"RING 2: sub-anchored on {sub} via {shared}; {why}; the "
                   f"sub-anchor's port {sub_port.number} carries {net} and "
                   f"leaves on {sub_port.side} (world {side} at rotation "
                   f"{sub_entry['rot_deg']}); this unit answers on "
                   f"{mine.number}")
        sub_pads = state.pads(sub)
        target = next((p for p in sub_pads
                       if str(p.pad_number) == sub_port.number), None)
        extent = _side_extent(state.boxes_court[sub], sub_entry["x_mm"],
                              sub_entry["y_mm"], side)
        outcome = _place_one(
            member, unit_geoms[ref], unit_fps[ref], session=session,
            anchor_pad=target, side=side, anchor_x_mm=sub_entry["x_mm"],
            anchor_y_mm=sub_entry["y_mm"], anchor_extent=extent, base=base,
            gap=gap, cursor=ring2_cursor.get((sub, side)),
            boxes_phys=state.boxes_phys, boxes_court=state.boxes_court,
            placed_parts=[], clearance_mm=clearance, route=route,
            align_connecting_pad=True,
            commit_copper=commit_copper)
        for rung in outcome["rungs"]:
            rungs_used[rung] = rungs_used.get(rung, 0) + 1
        if outcome["placement"] is None:
            row2 = dict(outcome["failure"], ring=2, subanchor=sub, side=side,
                        members=list(unit.refs))
            if blame and route:
                row2["blame"] = _blame_failure(
                    session, state, unit_fps[ref], outcome["failure"], target,
                    net=net, fab=fab, layers=blame_layers)
            ring2_failures.append(row2)
            continue
        placement = outcome["placement"]
        state.put(ref, x_mm=placement.x_mm, y_mm=placement.y_mm,
                  rot_deg=placement.rot_deg, geometry=unit_geoms[ref],
                  footprint=unit_fps[ref], token=outcome["token"], side=side,
                  subanchor=sub, standoff_mm=placement.standoff_mm,
                  allowance_mm=_allowance_of(placement))
        ring2[ref] = placement
        ring2_order.append(ref)
        ring2_cursor[(sub, side)] = outcome["cursor"]
        ring2_side.setdefault((sub, side), []).append(ref)
        _record(member, outcome, (sub, sub_port.number))

    skipped = [row for row in skipped if row["ref"] not in ring2]

    # -- ⭐⭐⭐ the strangers, BEFORE the tighten ----------------------------- #
    allowance = base
    channels = _channels_for(state, anchor, laid, ring2_side,
                             allowance_mm=allowance)
    strangers_before = stranger_crossings(
        state, pairs, channels=channels, ring2_refs=set(ring2),
        phase="before_tighten")

    # -- P7 ------------------------------------------------------------------ #
    tighten_steps: list = []
    shrink_rows: list = []
    final_reroute_ok = None
    final_reroute_failed = None
    if tighten:
        if tighten_mode == "ring":
            # ⛔ **P-H at L2.** Shrink the RADIUS, not the parts. It needs a ring
            # to shrink, so a ring-less arm asking for it is a caller error
            # rather than a silent fall-back to the per-part pass -- exactly the
            # sentence L1 states.
            if ring_info is None:
                raise ConstructError(
                    "tighten_mode='ring' needs ring=True -- there is no radius "
                    "to shrink on the linear L2 construction, and silently "
                    "running the per-part pass instead would make the arm "
                    "unattributable")
            shrink_rows = shrink_ring(
                session, state=state, order=order, laid=laid,
                radius=ring_info, pairs=pairs, anchor=anchor,
                anchor_x_mm=anchor_x_mm, anchor_y_mm=anchor_y_mm,
                anchor_extents=extents, geometries=unit_geoms,
                clearance_mm=clearance,
                step_mm=(float(shrink_step_mm) if shrink_step_mm
                         else clearance), route=route)
        else:
            tighten_steps = _tighten_cell(
                session, order=order, by_side=by_side,
                ring2_order=ring2_order, state=state, pairs=pairs,
                step_mm=clearance, clearance_mm=clearance, route=route,
                floor_policy=tighten_floor)
        if route and pairs:
            final_reroute_ok, failed_entry, lengths = _reroute_all(
                session, pairs, state)
            final_reroute_failed = (None if failed_entry is None
                                    else str(failed_entry["ref"]))
            tighten_steps = [
                replace(step, route_length_after_mm=(
                    lengths[step.ref].length_mm if step.ref in lengths
                    else None))
                for step in tighten_steps]

    channels_after = _channels_for(state, anchor, laid, ring2_side,
                                   allowance_mm=allowance)
    strangers_after = stranger_crossings(
        state, pairs, channels=channels_after, ring2_refs=set(ring2),
        routed_ok=({entry["ref"] for entry in pairs}
                   if final_reroute_ok in (None, True)
                   else {entry["ref"] for entry in pairs}
                   - {final_reroute_failed}),
        phase="after_tighten")

    for ref, placement in list(placements.items()):
        entry = state.entries[ref]
        placements[ref] = replace(placement, x_mm=round(entry["x_mm"], 6),
                                  y_mm=round(entry["y_mm"], 6))
    ring2 = {ref: placements[ref] for ref in ring2_order}

    # -- the anchor unit must be exactly where it was (rule 2) --------------- #
    anchor_entry = state.entries[anchor]
    anchor_members_after = {
        ref: (round(anchor_entry["x_mm"] + host.offsets[ref][0], 6),
              round(anchor_entry["y_mm"] + host.offsets[ref][1], 6),
              int(host.offsets[ref][2])) for ref in sorted(host.offsets)}
    anchor_moved = sorted(ref for ref in anchor_members_before
                          if anchor_members_before[ref]
                          != anchor_members_after.get(ref))

    census_before_rollback = session.census
    session.restore(snapshot)

    parts = state.placed_parts()
    used = {ref: state.entries[ref]["geometry"] for ref in state.entries}
    verdict = validate(parts, None,
                       {g.footprint: (g.width_mm, g.height_mm)
                        for g in used.values()},
                       clearance_mm=clearance,
                       fp_geometries={g.footprint: g for g in used.values()})
    refs = sorted(state.boxes_court)
    court_overlaps = sorted(
        [a, b] for i, a in enumerate(refs) for b in refs[i + 1:]
        if _pair_gap(state.boxes_court[a], state.boxes_court[b]) < -_TOL)
    gap_min = courtyard_gap(state.boxes_court)

    sides: list = []
    for side in order:
        chosen_p = [placements[n.ref] for n in by_side[side]
                    if n.ref in placements]
        sides.append(SideResult(
            board=board, anchor=anchor, side=side,
            neighbours=by_side[side], placements=tuple(chosen_p),
            failures=tuple(f for f in failures if f.get("side") == side),
            skipped=tuple(s for s in skipped if s.get("side") == side),
            standoff_base_mm=base,
            meta={"anchor_courtyard_extent_mm": extents[side],
                  "placed": [n.ref for n in laid[side]],
                  "priority_sum": sums[side],
                  "monotone": _is_monotone(chosen_p, side),
                  "routed": sum(1 for p in chosen_p if p.routed),
                  "class_census": _census(n.klass for n in by_side[side])}))

    universe = {unit.name for unit in units if unit.name != anchor}
    accounted = (set(placements) | set(ring2) | {f["ref"] for f in failures}
                 | {f["ref"] for f in ring2_failures}
                 | {s["ref"] for s in skipped})
    port_key_disagreement = _port_key_control(units)
    meta = {
        "anchor_unit": anchor,
        "anchor_ref": host.anchor,
        "anchor_x_mm": round(float(anchor_x_mm), 6),
        "anchor_y_mm": round(float(anchor_y_mm), 6),
        "anchor_members_moved": anchor_moved,
        "anchor_member_count": len(host.refs),
        "unit_count": len(units),
        "unit_sources_declared": list(UNIT_SOURCE),
        "unit_sources_seen": sorted({u.source for u in units}),
        "unit_source_census": _census(u.source for u in units),
        "unit_kind_census": _census(u.kind for u in units),
        "port_side_sources_declared": list(PORT_SIDE_SOURCES),
        "port_side_source_census": _census(
            port.source for unit in units for port in unit.ports),
        "port_key_control": port_key_disagreement,
        "side_order": list(order),
        "side_priority_sums": dict(sorted(sums.items())),
        "side_counts": {s: len(by_side[s]) for s in SIDES},
        "side_lists": {s: [n.ref for n in by_side[s]] for s in SIDES},
        "side_lists_control": (None if control_side_lists is None else
                               {s: [n.ref for n in control_side_lists[s]]
                                for s in SIDES}),
        "binding_table": list(chosen),
        "flags": {"anchor_rule": anchor_rule, "unit_source": unit_source,
                  "port_side": port_side,
                  "connector_policy": connector_policy,
                  "side_order_policy": side_order_policy,
                  "centre_side_lists": bool(centre_side_lists),
                  "ring2_avoids_anchor_side": bool(ring2_avoids_anchor_side),
                  "tighten": bool(tighten), "tighten_floor": tighten_floor,
                  "route": bool(route), "blame": bool(blame)},
        "anchor_rules_declared": list(L2_ANCHOR_RULES),
        "anchor_rule_picks": {rule: l2_anchor(units, rule=rule)
                              for rule in L2_ANCHOR_RULES
                              if rule != "connector"
                              or any(u.kind == "connector" for u in units)},
        "connector_policies_declared": list(CONNECTOR_POLICIES),
        "connector_fixed": fixed,
        "classes_declared": list(NEIGHBOUR_CLASSES),
        "classes_seen": sorted({n.klass for n in every}),
        "class_census": _census(n.klass for n in every),
        "l2_skip_reasons_declared": list(L2_SKIP_REASONS),
        "l2_skip_reasons_seen": sorted({s.get("why", "") for s in skipped}),
        "ring2_candidates": [row["ref"] for row in ring2_candidates],
        "ring2_table": [{"ref": ref, "subanchor": state.entries[ref]
                         ["subanchor"], "side": state.entries[ref]["side"],
                         "standoff_mm": state.entries[ref]["standoff_mm"]}
                        for ref in ring2_order],
        "ring2_side_choice": ring2_side_choices,
        "edge_gap_mm": gap,
        "edge_gap_terms": [list(t) for t in EDGE_GAP_TERMS],
        "fanout_allowance_mm": base,
        "fab": str(getattr(fab, "name", "")),
        "clearance_mm": clearance,
        "track_width_mm": float(fab.track_width_mm),
        "via_size_mm": float(fab.via_size_mm),
        "physical_overlaps": [list(pair) for pair in verdict.overlaps],
        "courtyard_overlaps": court_overlaps,
        "courtyard_min_gap_mm": None if gap_min == float("inf") else gap_min,
        "map_vs_router": disagreements,
        "ladder_rungs_declared": list(LADDER_RUNGS),
        "ladder_rungs_used": dict(sorted(rungs_used.items())),
        "centring": centring,
        "crossings": pair_crossings(_pair_lines(state, pairs, set(ring2))),
        "side_spans": {side: side_span(
            [placements[n.ref] for n in laid[side] if n.ref in placements],
            side, unit_geoms, anchor_box=anchor_court)
            for side in SIDES if laid[side]},
        "tighten_floors_declared": list(TIGHTEN_FLOORS),
        "tighten_stop_reasons_declared": list(TIGHTEN_STOP_REASONS),
        "tighten_stop_reasons_seen": sorted({s.stopped_by
                                             for s in tighten_steps}),
        "tighten_recovered_mm": round(sum(s.recovered_mm
                                          for s in tighten_steps), 6),
        "tighten_step_mm": clearance,
        "tighten_final_reroute_ok": final_reroute_ok,
        "tighten_final_reroute_failed": final_reroute_failed,
        "tighten_moved_nothing": bool(
            tighten_steps and all(step.recovered_mm == 0.0
                                  for step in tighten_steps)),
        "monotone": {side: _is_monotone(
            [placements[n.ref] for n in laid[side] if n.ref in placements],
            side) for side in SIDES},
        "strangers": {
            "channels": [{"unit": c[0], "side": c[1],
                          "band": [round(v, 6) for v in c[2]],
                          "allowance_mm": round(c[3], 6),
                          "occupants": list(c[4])} for c in channels_after],
            "before_tighten": len(strangers_before),
            "after_tighten": len(strangers_after),
            "deepest_mm_before": (round(max(s.overlap_mm for s
                                            in strangers_before), 6)
                                  if strangers_before else None),
            "deepest_mm_after": (round(max(s.overlap_mm for s
                                           in strangers_after), 6)
                                 if strangers_after else None),
            "own_pair_survived": (all(s.still_routed for s in strangers_after)
                                  if strangers_after else None),
            "note": "⛔ the PAIR LINE, not the copper -- a PairResult carries "
                    "no path and inventing one is forbidden"},
        "accounting": {
            "universe": sorted(universe),
            "placed_on_a_side": sorted(set(placements) - set(ring2)),
            "placed_in_ring_2": sorted(ring2),
            "failed": sorted({f["ref"] for f in failures}
                             | {f["ref"] for f in ring2_failures}),
            "skipped": sorted({s["ref"] for s in skipped}),
            "unaccounted": sorted(universe - accounted),
            "total": not (universe - accounted)},
        # ⭐⭐⭐ **S10 §2 -- A SECOND ACCOUNTING, OVER PARTS, BECAUSE THE ONE
        # ABOVE IS BLIND BY CONSTRUCTION.** Its universe is the set of UNIT
        # NAMES, so a member an L1 cell refused is absent from ``unit.refs``,
        # absent from the universe, and ``unaccounted`` stays ``[]`` while the
        # part is lost in the gap between the two levels with nobody naming it
        # -- standing finding 1, measured live on ``lt3758_iso_flyback``/
        # ``RLED``. ``orphan_units`` routes *around* that by re-emitting the
        # member as its own unit; this block is the part that makes the
        # blindness itself impossible, and it is ON unconditionally because an
        # accounting you have to opt into is not an accounting.
        #
        # ⛔ The universe is the PARTITION's parts -- every part the board has,
        # not every part some unit happens to mention -- so a part that no unit
        # claims is named here even when the unit inventory is internally
        # consistent. ⚠ ``unaccounted_parts`` non-empty is not automatically a
        # failure: a part with no footprint geometry is legitimately absent and
        # arrives here through ``unit_skips``. It is *named*, which is the whole
        # requirement.
        "parts_accounting": _parts_accounting(
            partition, units, placements, ring2, failures, ring2_failures,
            skipped, host),
        "route_calls": len(timings),
        "rollback_exact": bool(session.census == snapshot.census),
        "census_at_end_of_construction": list(census_before_rollback),
        "census_after_rollback": list(session.census),
        "timing": {"route_total_s": round(sum(timings), 6),
                   "route_max_s": round(max(timings), 6) if timings else None,
                   "route_mean_s": (round(sum(timings) / len(timings), 6)
                                    if timings else None)},
    }
    # ⛔⛔ **EMITTED ONLY WHEN THE POLICY IS ON**, for the reason S5 states two
    # blocks below: the OFF arm's control is a PLAIN DIFF against a recorded
    # artifact, so a key that is always present would make *"byte-identical"* a
    # claim about a stripper rather than about behaviour.
    if commit_copper:
        meta["commit_copper"] = commit_log
    # -- S9 Stage C: the labelled plane fallback, ONLY when it is on --------- #
    if plane_fallback:
        meta["flags"]["plane_fallback"] = True
        meta["plane_fallback"] = {
            "enabled": True,
            "placed_against_a_plane_net": [row for row in plane_fallbacks
                                           if row["ref"] in ring2],
            "offered": plane_fallbacks,
            "still_skipped": sorted({f["ref"] for f in ring2_failures}),
            "key": "(-shared_plane_nets, not same_partition_group, ref)"}
    # -- ⭐⭐⭐ S9 Stage D: the arc's blocks, ONLY when one of ITS flags is on -- #
    # ⛔ Conditional for the reason every block above is: gate ``PN4``'s control
    # arm is a **plain diff** against S8's recorded L2 artifact, so a key that
    # were always present would make *"byte-identical"* a claim about a stripper
    # rather than about behaviour.
    if any((side_assignment != "favored", ring, tighten_mode != "part")):
        meta["flags"].update({
            "side_assignment": side_assignment, "ring": bool(ring),
            "arc_edge_gap": arc_edge_gap, "corner_owner": corner_owner,
            "spawn_factor": float(spawn_factor),
            "r_max_factor": float(r_max_factor),
            "tighten_mode": tighten_mode})
        meta["side_assignment_declared"] = list(SIDE_ASSIGNMENT)
        meta["arc_edge_gap_declared"] = list(ARC_EDGE_GAP)
        meta["corner_owner_declared"] = list(CORNER_OWNER)
        meta["tighten_mode_declared"] = list(TIGHTEN_MODE)
        meta["side_demand"] = {
            "assignment": demand["assignment"],
            "reasons_declared": list(SIDE_DEMAND_REASONS),
            "reasons_seen": sorted({row["reason"] for row in demand["rows"]}),
            "moved": [row for row in demand["rows"] if row["moved"]],
            "rows": demand["rows"], "loads": demand["loads"],
            "packed_mm": demand["packed"],
            "edge_gap_mm": demand["edge_gap_mm"]}
        if ring_info is not None:
            meta["ring"] = {
                "radius": {side: ring_info["sides"][side] for side in SIDES},
                "owners": ring_info["owners"],
                "corner_owner": ring_info["corner_owner"],
                "edge_gap": ring_info["edge_gap"],
                "edge_gap_mm": ring_info["edge_gap_mm"],
                "reserve_mm": ring_info["reserve_mm"],
                "allowance_mm": ring_info["allowance_mm"],
                "spawn_factor": ring_info["spawn_factor"],
                "max_R_fit_mm": ring_info["max_R_fit_mm"],
                "infeasible": ring_info["infeasible"],
                "quadrants_declared": list(QUADRANTS),
                "rotations": dict(sorted(ring_rot.items())),
                "slots": {side: ring_plan[side] for side in SIDES
                          if side in ring_plan},
                #: ⛔ Every unit the ring never sized a slot for, named.
                "unslotted": sorted(
                    n.ref for side in SIDES for n in by_side[side]
                    if n.ref not in ring_plan.get(side, {}).get("slots", {}))}
        meta["ring_shrink"] = {
            "mode": tighten_mode,
            "stop_reasons_declared": list(RING_STOP_REASONS),
            "stop_reasons_seen": sorted({row["stopped_by"]
                                         for row in shrink_rows}),
            "recovered_mm": round(sum(row["recovered_mm"]
                                      for row in shrink_rows), 6),
            "step_mm": (float(shrink_step_mm) if shrink_step_mm
                        else clearance),
            "rows": shrink_rows}
    return BoardResult(
        board=board, anchor=anchor, sides=tuple(sides),
        ring2=tuple(ring2[ref] for ref in ring2_order),
        ring2_failures=tuple(sorted(ring2_failures,
                                    key=lambda f: str(f["ref"]))),
        tighten=tuple(tighten_steps),
        skipped=tuple(sorted(skipped, key=lambda s: str(s["ref"]))),
        strangers=tuple(strangers_before) + tuple(strangers_after),
        units=units, meta=meta)


def _rotate_side(side: str, deg: int) -> str:
    from .escape_map import rotate_escape

    return rotate_escape(side, int(deg))


def _l2_side_excluding(unit: Unit, net: str, rot_deg: int,
                       forbidden: str) -> str | None:
    """⭐ **P-D at L2.** The best port side of ``unit`` on ``net`` that is not the
    side the anchor lies on. ⛔ ``FAVORED`` before ``ACCESSIBLE``, then the
    corridor cost, then the side letter -- ``favored_side``'s own ordering, one
    level up. ``None`` keeps the existing answer and records it."""
    rank = {"FAVORED": 0, "ACCESSIBLE": 1}
    for port in sorted(unit.ports_on(net),
                       key=lambda p: (rank.get(p.access, 2), p.cost, p.side,
                                      _pad_key(p.number))):
        world = _rotate_side(port.side, rot_deg)
        if world != forbidden:
            return world
    return None


def _port_key_control(units) -> dict:
    """⚠ **The plan's stated representative key against the implemented one.**

    Plan section 5.6 asks for the lowest ``(net, member, pad)`` port; the code
    uses ``_pad_key`` of the composite number, because that is the pad
    ``_route_pair`` and ``_connecting_pad_offsets`` already agree on. ⛔ The
    difference is **measured every run rather than argued** (standing finding
    20's procedure), and it is a count, not a verdict.
    """
    differs, total = [], 0
    for unit in units:
        for net in unit.nets:
            here = unit.ports_on(net)
            total += 1
            mine = sorted(here, key=lambda p: p.key)[0]
            theirs = sorted(here, key=lambda p: (p.net, p.member,
                                                 _pad_key(p.pad)))[0]
            if mine.number != theirs.number:
                differs.append({"unit": unit.name, "net": net,
                                "implemented": mine.number,
                                "plan_literal": theirs.number})
    return {"nets_examined": total, "disagreements": len(differs),
            "detail": differs[:20]}


def _place_fixed(neighbour, geometry, footprint, *, session, anchor_pad,
                 position, boxes_phys, boxes_court, clearance_mm,
                 route) -> dict:
    """⛔ The ``hand_fixed`` connector policy: put the unit **where the human
    put it**, with no ladder at all, and record what happened.

    ⚠ It exists to be *measured and refused*, not used: those positions are part
    of the answer key S6 will grade against.
    """
    x_mm, y_mm, rot = float(position[0]), float(position[1]), int(position[2])
    phys = _physical_box(geometry, neighbour.ref, x_mm, y_mm, rot)
    court = _courtyard_box(geometry, neighbour.ref, x_mm, y_mm, rot)
    clash = sorted(ref for ref, box in boxes_phys.items()
                   if _pair_gap(box, phys) < clearance_mm - _TOL)
    clash += sorted(ref for ref, box in boxes_court.items()
                    if _pair_gap(box, court) < -_TOL)
    if clash:
        return {"placement": None, "cursor": None, "rungs": (), "token": None,
                "failure": {"ref": neighbour.ref, "step": "hand_fixed",
                            "why": f"the hand position collides with {clash}",
                            "router": None}}
    pads = moved_pads(list(footprint.pads), x_mm=x_mm, y_mm=y_mm, rot_deg=rot,
                      base_rot_deg=footprint.rotation)
    token = session.add_part(f"@N:{neighbour.ref}", pads)
    answer = (_route_pair(session, pads, neighbour, anchor_pad)
              if route and anchor_pad is not None else None)
    boxes_phys[neighbour.ref] = phys
    boxes_court[neighbour.ref] = court
    return {"placement": Placement(
        ref=neighbour.ref, x_mm=round(x_mm, 6), y_mm=round(y_mm, 6),
        rot_deg=rot, standoff_mm=0.0, slide_steps=0,
        routed=bool(answer.routed) if answer is not None else False,
        route_length_mm=answer.length_mm if answer is not None else None,
        route_iterations=answer.iterations if answer is not None else None,
        route_elapsed_s=answer.elapsed_s if answer is not None else None,
        route_failure=answer.failure if answer is not None else None,
        anchor_pad=neighbour.anchor_pad, net=neighbour.net,
        standoff_terms=(("hand_fixed_mm", 0.0,
                         "the HAND board's own position -- part of S6's answer "
                         "key, imported here only to be measured"),),
        reasons=("connector_policy='hand_fixed': placed at the human's "
                 "position, no ladder",)),
        "cursor": None, "rungs": ("hand_fixed",), "failure": None,
        "token": token, "pads": pads, "target": anchor_pad}
