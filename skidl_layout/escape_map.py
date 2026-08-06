# -*- coding: utf-8 -*-
"""The per-pad escape map -- which side can each pad of a bare footprint escape?

Construction-arc stage **S1**. The artifact this module produces answers, for a
**bare footprint with no board and no netlist**, the question the construction
loop asks first: *"pad 7 of this IC connects to a neighbour -- which side of the
part does that wire leave from, and which side is favoured?"*

⛔⛔ **This is a LEAF module.** Nothing in the engine, the scorer or the refiner
imports it, and nothing consumes the map yet -- S3 (the first construction loop)
is the consumer. Same discipline as :mod:`~skidl_layout.route_session`: it is
never added to a package re-export the engine reaches, and
``test_escape_map.py::test_escape_map_is_a_leaf_no_module_imports_it`` says so.

⚠ **That guard reads IMPORT STATEMENTS, not the module name as a substring**,
and so does its sibling's -- the sibling's used to be a bare substring scan, and
this very docstring failed it on 2026-08-03 for saying the name out loud. Both
were made import-aware; ⛔ do not "simplify" either back to ``name in text``.

⭐⭐⭐ **The trick that makes this a wrapper rather than a new derivation: the
singleton wrap.** The per-pad primitive already exists --
:func:`~skidl_layout.cells_compile.escape_corridor_clear` is per **pad**, per
side, per layer, and :func:`~skidl_layout.cells_compile.derive_access`
aggregates it per **net**. So a one-member cell built by
:func:`~skidl_layout.cells.synthesise_cell` with **one local net per copper
pad** turns the existing per-net derivation into a per-pad one, unmodified.
Neither of those two functions is edited by this module; both are only called.

Two behaviours fall out of the wrap with no rule authored at all, and the tests
**assert** them rather than encoding them:

* a chip passive's **opposite** side is ``BLOCKED``, because the corridor
  crosses the other pad, which under the wrap is a foreign net;
* a QFP pad reads clear only on **its own** side, because every other corridor
  crosses a neighbouring pad.

Two things do **not** fall out, and this module owns both:

1. ⛔⛔ **``FAVORED`` as ``derive_access`` computes it is the WRONG answer for a
   per-pad map, and it is wrong for a structural reason rather than a corner
   case.** ``derive_access`` ranks a net's escapes on ``(cost, side, layer)``
   where ``cost`` is the pad-edge-to-box-edge distance. The cell box is the
   tight union of the members' **physical** envelopes, so on a single-part cell
   every pad that touches the box edge is at distance **0.0 on every side it
   touches** -- and a chip passive's pad touches three of them. The rank is then
   decided entirely by the alphabetical ``side`` tie-break, which promotes
   ``N`` for pad 1 and ``E`` for pad 2 of **every** chip passive at **every**
   size. That is not "the side the pad is nearest"; it is an artifact of sorting
   ``E < N < S < W``.
   ⭐ **The fix is general, not a special case:** ``FAVORED`` is decided by
   :func:`pad_occupancy_side` -- *the side of the part the pad sits on*, which
   is the human's rule for all four footprint classes ("favoured = its own
   side"; "pads favour the side they sit on"; "a QFP pad escapes only on the
   side it occupies"). It enters the ranking as a **tie-break behind the
   distance**, so a strictly nearer side still wins and ``derive_access``'s own
   ordering is preserved wherever it is not degenerate.
2. **The row channel** (:class:`RowChannel`) -- a dual-row part's pads can route
   *between* the rows and leave N or S. A straight corridor cannot represent a
   jogged path, so ``derive_access`` under-reports it. That is the safe
   direction, and it is the one authored rule S1 adds.

⛔ **Say which box every rule means** (standing finding 13). Every corridor and
escape verdict here runs in the **physical** (``body ∪ pads``) frame, which is
what :func:`~skidl_layout.cells._member_bounds` builds and what the validator
overlaps on. The **courtyard** box is *recorded* on the artifact for S3's
standoff and is never an input to any verdict here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .cells import (
    Access,
    Side,
    _q,
    _rotate_side,
    resolve_footprint_name,
    synthesise_cell,
)
from .cells_compile import derive_access, escape_corridor_clear

__all__ = [
    "EscapeMap",
    "PadEscape",
    "RowChannel",
    "copper_pads",
    "escape_map_for",
    "escape_map_to_dict",
    "escape_maps_for",
    "escapable_sides_ranked",
    "favored_side",
    "pad_occupancy_side",
    "rotate_escape",
    "terminal_pads",
]

_SIDES: tuple[str, ...] = ("N", "E", "S", "W")

#: The two axes a pair of opposite pad rows can run along. ⭐ Named for the
#: direction the **channel** runs, not for the sides the pads sit on: pads on
#: E and W leave a channel that runs N-S.
_ROW_AXIS = {frozenset({"E", "W"}): "NS", frozenset({"N", "S"}): "EW"}

#: How far a coordinate may miss an edge and still count as touching it -- the
#: same quantum :mod:`~skidl_layout.cells` stores geometry at, and the same
#: tolerance ``escape_corridor_clear`` uses for a pad flush with the box edge.
#: ⛔ Never a second, larger tolerance stacked on top of that one (trap 4).
_TOL = 1e-6


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PadEscape:
    """One copper pad, on one side, on one layer -- in the footprint's own frame.

    ``access`` carries exactly :class:`~skidl_layout.cells.CellPort`'s
    semantics: ``BLOCKED`` is an **assertion** (the rule probed and it failed),
    never "not asked". The map is **total** -- every ``(copper pad, side,
    layer)`` triple is present -- so "not asked" cannot arise here at all.
    """

    pad: str
    side: Side
    layer: int
    access: Access
    #: Pad edge to box edge for a ``corridor`` entry; pad **centre** to box edge
    #: for a ``row_channel`` one (the jog makes an edge distance meaningless).
    distance_mm: float
    #: ``"corridor"`` (from ``escape_corridor_clear``) or ``"row_channel"``
    #: (this module's one authored rule). ⭐ Every verdict says which rule made
    #: it, so a disagreement with the probe is attributable.
    source: str = "corridor"

    @property
    def key(self) -> tuple:
        """The total sort key -- over content, never arrival order."""
        return (_pad_key(self.pad), self.layer, self.side)


@dataclass(frozen=True)
class RowChannel:
    """The clear lane between a dual-row part's two rows, if there is one.

    ``lo``/``hi`` are the raw inner edges of the two rows on the **transverse**
    axis (x for an ``NS`` channel), in the footprint's local frame;
    ``clear_mm`` is what is left of ``hi - lo`` after ``clearance_mm`` is owed
    to each row. ⚠ ``clear_mm`` is already net of clearance on both sides -- a
    consumer that subtracts it again is double-counting, the same convention
    :class:`~skidl_layout.cells.CellTransit` follows.
    """

    axis: str                      # "NS" (pads on E/W) or "EW" (pads on N/S)
    lo: float
    hi: float
    clear_mm: float
    lane_mm: float
    open: bool

    def to_dict(self) -> dict:
        return {"axis": self.axis, "lo": self.lo, "hi": self.hi,
                "clear_mm": self.clear_mm, "lane_mm": self.lane_mm,
                "open": self.open}


@dataclass(frozen=True)
class EscapeMap:
    """Every ``(copper pad, side, layer)`` verdict for one bare footprint."""

    #: ⛔ ALWAYS ``"Library:Name"``, never bare (standing finding 6). A bare name
    #: handed to the geometry loader resolves nothing and silently boxes the part
    #: in a 2 x 2 mm fallback that is self-consistent, so no gate downstream
    #: catches it. :func:`escape_map_for` resolves before it derives, and raises
    #: when it cannot.
    footprint: str
    pad_count: int                       # copper pads only
    entries: tuple[PadEscape, ...]       # TOTAL over (copper pad, side, layer)
    channel: RowChannel | None
    #: ⭐ The box every verdict above was computed in: ``body ∪ pads``.
    physical_bounds: tuple[float, float, float, float]
    #: ⭐ Recorded for S3's standoff -- **never** an input to a verdict here.
    #: ``None`` when the footprint declares no courtyard; nothing to overlap is
    #: not the same as an overlap of zero.
    courtyard_bounds: tuple[float, float, float, float] | None
    lane_mm: float
    clearance_mm: float
    layers: tuple[int, ...] = (0,)
    #: The class the row detection assigned, from measured pad geometry only:
    #: ``"single_pad"`` / ``"two_pin"`` / ``"dual_row"`` / ``"four_sided"`` /
    #: ``"other"``. ⛔ Never from the footprint name or a pin-count threshold.
    detection: str = "other"
    meta: dict = None                    # provenance and derivation notes

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})

    # -- accessors -------------------------------------------------------- #
    @property
    def pads(self) -> tuple[str, ...]:
        return tuple(sorted({entry.pad for entry in self.entries}, key=_pad_key))

    def entries_for(self, pad: str) -> tuple[PadEscape, ...]:
        return tuple(e for e in self.entries if e.pad == str(pad))

    def access(self, pad: str, side: str, layer: int = 0) -> Access:
        for entry in self.entries:
            if (entry.pad == str(pad) and entry.side == side
                    and entry.layer == int(layer)):
                return entry.access
        raise KeyError(
            f"{self.footprint}: no entry for pad {pad!r} side {side!r} "
            f"layer {layer!r} -- the map is total, so this is a lookup bug"
        )

    def escapable_sides(self, pad: str) -> tuple[str, ...]:
        """Sides ``pad`` is not ``BLOCKED`` on, on any layer. Sorted."""
        return tuple(sorted({e.side for e in self.entries_for(pad)
                             if e.access != "BLOCKED"}))

    @property
    def pads_without_escape(self) -> tuple[str, ...]:
        """Pads that are ``BLOCKED`` on every side of every layer.

        ⚠ **Not a defect on its own, and this is measured rather than
        assumed:** an exposed pad is enclosed by its own ring on all four sides
        and has no *lateral* escape at all -- it leaves through a via, which is
        out of scope at ``layers=(0,)``. Measured on the corpus: exactly the
        three exposed pads (``MSOP-10-1EP`` pad 11, ``TSSOP-16-1EP`` pad 17,
        ``QFN-56-1EP`` pad 57). ⛔ What *is* a defect -- and what
        :func:`escape_map_for` raises on -- is a map in which **no** pad
        escapes anywhere; see the observes-nothing rule.
        """
        return tuple(pad for pad in self.pads if not self.escapable_sides(pad))

    def counts(self) -> dict:
        out = {"FAVORED": 0, "ACCESSIBLE": 0, "BLOCKED": 0,
               "corridor": 0, "row_channel": 0}
        for entry in self.entries:
            out[entry.access] += 1
            out[entry.source] += 1
        return out


# --------------------------------------------------------------------------- #
# Geometry helpers -- every one of them a MEASUREMENT of the pad set
# --------------------------------------------------------------------------- #
def _is_copper(pad) -> bool:
    """Does this pad carry copper?

    ⚠ **Paste-only apertures are real and they are on the corpus.**
    ``QFN-56-1EP`` declares four ``F.Paste``-only thermal sub-apertures, and on
    a placed board they can even carry a net name. They are not routable
    terminals, so they get no map entry -- but they are **counted and reported**,
    never silently dropped, and they still obstruct as copper would, which is
    the safe direction.
    """
    return any(layer == "*.Cu" or layer.endswith(".Cu") for layer in pad.layers)


def copper_pads(geometry) -> list:
    """The footprint's copper pads, in sorted-by-number order (rule 4)."""
    return sorted((pad for pad in geometry.pads if _is_copper(pad)),
                  key=lambda pad: _pad_key(str(pad.number)))


def _pad_key(number: str) -> tuple:
    """The total order pads are iterated in -- ``"2"`` before ``"10"``.

    ⛔ A total key over content, never arrival order (rule 4). Plain string
    order would put pad 10 between 1 and 2, which is harmless for a map keyed by
    number and confusing in every table the map is printed into.
    """
    text = str(number)
    return (0, len(text), text) if text.isdigit() else (1, len(text), text)


def terminal_pads(geometry) -> list:
    """The copper pads that are **routable terminals** -- what the map covers.

    Three exclusions, all measured from the pad itself and every one of them
    **counted and reported** rather than silently dropped (trap 3):

    1. ⚠ **No copper layer.** ``QFN-56-1EP`` declares four ``F.Paste``-only
       thermal sub-apertures, and on three of six eval boards paste-only pads
       even carry net names. Not terminals.
    2. ⛔ **Non-plated through holes.** ``USB_C_Receptacle_XKB_U262-16XN-4BVC11``
       carries two ``np_thru_hole`` pads whose layers include ``*.Cu`` -- they
       are mounting holes, they conduct nothing, and treating them as terminals
       is what made the first sweep raise on that footprint.
    3. ⛔ **An empty pad number.** KiCad's own convention for a pad with no
       electrical terminal (the same two USB-C holes). A map entry keyed on
       ``""`` is not addressable by any consumer.

    ⭐ **All three still OBSTRUCT.** They stay in the cell as foreign-net copper,
    so a corridor that crosses a mounting hole is still ``BLOCKED`` -- excluding
    them from the *answer* must never soften the *question*.
    """
    out = []
    for pad in copper_pads(geometry):
        if str(pad.number).strip() == "":
            continue
        if str(getattr(pad, "pad_type", "")) == "np_thru_hole":
            continue
        out.append(pad)
    return out


def pad_occupancy_side(pad_x: float, pad_y: float,
                       bounds: tuple[float, float, float, float]) -> Side:
    """*Which side of the part does this pad sit on?* -- measured, per pad.

    ⭐ This is :meth:`~skidl_layout.geometry.FootprintGeometry.pad_side_counts`'
    own arithmetic applied to **one** pad instead of summed over all of them,
    with one deliberate change: the centre is taken from the box the caller
    passes, and every caller here passes the **physical** box.
    ``pad_side_counts`` measures against ``bounds``, which *prefers the
    courtyard* -- a different and larger box (standing finding 13). For a
    symmetric footprint the two centres coincide; for an asymmetric one they do
    not, and a rule that silently used the courtyard centre would be the same
    class of mistake that shipped seven overlapping cells.

    ⛔ **Derived from the measured pad position, never from a package-name
    table, a pin-count threshold or a hardcoded envelope** -- the size-table
    trap has fired four times and this is the fifth place it could.

    ⚠ Ties resolve the way ``pad_side_counts`` resolves them (``abs(dx) >=
    abs(dy)`` favours the horizontal axis, ``dx >= 0`` favours ``E``), so a pad
    dead in the centre -- an exposed pad -- reads ``"E"``. That is arbitrary but
    it is **total and stated**, which is what the determinism rule asks for, and
    an exposed pad's escape verdict never depends on it (it is ``BLOCKED``
    everywhere by its own ring).
    """
    x_min, y_min, x_max, y_max = bounds
    dx = pad_x - (x_min + x_max) / 2.0
    dy = pad_y - (y_min + y_max) / 2.0
    if abs(dx) >= abs(dy):
        return "E" if dx >= 0 else "W"
    return "S" if dy >= 0 else "N"


#: ⛔ Why a pad has no lateral escape. **The two license different actions**,
#: which is the whole reason the distinction exists:
#:
#: * ``"interior"`` -- the pad is strictly inside the pad lattice on **both**
#:   axes, so its own neighbours enclose it. **No lateral escape exists at any
#:   track width**; it needs a via fanout, and no amount of placement helps.
#: * ``"lane"`` -- the pad is on the lattice perimeter, so a lateral corridor
#:   *does* exist; it is merely narrower than ``lane_mm``. ⭐ **A narrower
#:   TRACK may still get out**, and S1 measured exactly that: 43 of 127 sampled
#:   ``BLOCKED`` entries were routed by the real router anyway.
BLOCKED_REASONS: tuple[str, ...] = ("interior", "lane")


def interior_pads(geometry) -> tuple[str, ...]:
    """Pads strictly inside the part's own pad lattice, on **both** axes.

    ⭐ **Adopted from KiCadRoutingTools' ``placement/escape.py``** (branch
    ``placement``, Rob Boerman, read at ``fe6db00``), whose ledger counts an
    interior pad toward **no** face: *"a pad boxed in by its neighbours does not
    escape sideways at all -- it needs a via. Rolling it into a face's demand
    would blame the face for a fanout problem."* ⛔ Reimplemented, not imported:
    that module is on an unmerged branch and KRT divergence stays ZERO.

    ⚠ **Measured on this corpus before adopting it, and the result bounds the
    claim**: of the five footprints standing finding 14 says get nothing from
    the map, interior pads explain **one** --
    ``Raytac_MDBT50Q`` (46 of 61 interior). ``LGA-12``, ``LGA-14`` and
    ``USB_Micro-B_Molex`` have **zero** interior pads: every one of their pads
    is on the lattice perimeter. ⛔ **So this does not answer overview open
    question 8. It SPLITS it**, and the two halves have different answers.

    ⛔ Measured from the pad lattice, never from the footprint name or a
    pin-count threshold -- the size-table trap's sixth opportunity.
    ⚠ Uses ``terminal_pads``, so paste-only, non-plated and unnumbered
    apertures cannot make a real pad look enclosed.
    """
    pads = terminal_pads(geometry)
    xs = sorted({round(pad.x_mm, 3) for pad in pads})
    ys = sorted({round(pad.y_mm, 3) for pad in pads})
    if len(xs) < 3 or len(ys) < 3:
        return ()
    x_lo, x_hi, y_lo, y_hi = xs[0], xs[-1], ys[0], ys[-1]
    return tuple(sorted(
        (str(pad.number) for pad in pads
         if x_lo < round(pad.x_mm, 3) < x_hi
         and y_lo < round(pad.y_mm, 3) < y_hi),
        key=_pad_key))


def blocked_reasons(emap: "EscapeMap", geometry) -> dict:
    """``{pad: reason}`` for every pad with no lateral escape anywhere.

    ⭐ **This is what turns ``pads_without_escape`` from a count into an
    action.** Today every such pad reads the same, and two structurally
    different situations are hiding in it (see :data:`BLOCKED_REASONS`).

    ⛔ **A free function that stores nothing.** It deliberately does not run
    inside :func:`escape_map_for` and does not touch
    :attr:`EscapeMap.meta`, so **no recorded S1 artifact moves** -- adding a
    field to a shipped artifact to carry a new derivation is how a
    re-measurement turns into a diff nobody asked for.

    ⚠ ``geometry`` must be the **same footprint** the map describes; a mismatch
    raises rather than answering about the wrong part.
    """
    if geometry.footprint != emap.footprint:
        raise ValueError(
            f"the map describes {emap.footprint!r} but the geometry is "
            f"{geometry.footprint!r} -- a reason derived from the wrong pad "
            f"lattice is worse than no reason")
    interior = set(interior_pads(geometry))
    return {pad: ("interior" if pad in interior else "lane")
            for pad in emap.pads_without_escape}


def _detection_class(pads, clusters) -> str:
    """The footprint's row class, from the measured pad set alone."""
    if len(pads) == 1:
        return "single_pad"
    if len(pads) == 2:
        return "two_pin"
    occupied = frozenset(clusters)
    if occupied in _ROW_AXIS:
        return "dual_row"
    if occupied == frozenset(_SIDES):
        return "four_sided"
    return "other"


def _row_channel(pads, clusters, *, obstructions, lane_mm: float,
                 clearance_mm: float) -> RowChannel | None:
    """The lane between two opposite pad rows, or ``None`` if there is no pair.

    ⭐ **An exposed pad closes the channel with no special case at all**, and
    that is asserted rather than asserted-about: the EP sits at the box centre,
    so :func:`pad_occupancy_side` puts it in one of the two row clusters, and
    the row's *inner* edge is then the EP's own edge. Measured:
    ``MSOP-10-1EP`` 0.085 mm and ``TSSOP-16-1EP`` 0.100 mm of clear lane against
    a 0.9048 mm requirement, versus ``SOIC-8``'s 5.050 mm. No ``EP`` substring
    is matched anywhere.
    """
    axis = _ROW_AXIS.get(frozenset(clusters))
    if axis is None:
        return None
    by_number = {str(pad.number): pad for pad in pads}
    if axis == "NS":                       # pads on E and W; channel runs N-S
        lo = max(by_number[n].local_bounds[2] for n in clusters["W"])
        hi = min(by_number[n].local_bounds[0] for n in clusters["E"])
    else:                                  # pads on N and S; channel runs E-W
        lo = max(by_number[n].local_bounds[3] for n in clusters["N"])
        hi = min(by_number[n].local_bounds[1] for n in clusters["S"])
    clear = (hi - lo) - 2.0 * clearance_mm
    is_open = clear >= lane_mm - _TOL

    if is_open:
        # ⛔ The second clause, and it is NOT redundant with the first:
        # ``lo``/``hi`` are extremes over the two ROW clusters, so a row pad
        # cannot narrow them further -- but a pad that is in neither row (a
        # mounting hole, an aperture the terminal filter dropped) sitting inside
        # the band would still block it, and it must, because it is still
        # copper. Tested against the NET band so the clearance is not counted
        # twice against the row edges that defined it.
        band_lo, band_hi = lo + clearance_mm, hi - clearance_mm
        for pad in obstructions:
            px0, py0, px1, py1 = pad.local_bounds
            plo, phi = ((px0, px1) if axis == "NS" else (py0, py1))
            if plo < band_hi - _TOL and phi > band_lo + _TOL:
                is_open = False
                break
    return RowChannel(axis=axis, lo=_q(lo), hi=_q(hi), clear_mm=_q(clear),
                      lane_mm=_q(lane_mm), open=bool(is_open))


def rotate_escape(entry_side: Side, deg: int) -> Side:
    """A side of the rotation-0 map, as seen after the part is turned ``deg``.

    ⭐ Delegates to :func:`skidl_layout.cells._rotate_side` -- the arc has
    exactly one side-rotation transform and this is not a second one. The map
    itself is derived at rotation 0 **only**; a consumer rotates the answer.
    """
    return _rotate_side(entry_side, deg)


def favored_side(emap: EscapeMap, pad: str, layer: int = 0) -> Side:
    """The one favoured side of ``pad``. ⛔ Raises when the pad has none.

    An exposed pad genuinely has none -- see
    :attr:`EscapeMap.pads_without_escape` -- so the caller is told rather than
    handed a plausible default.
    """
    hits = sorted(e.side for e in emap.entries_for(pad)
                  if e.layer == int(layer) and e.access == "FAVORED")
    if not hits:
        raise ValueError(
            f"{emap.footprint}: pad {pad!r} has no FAVORED side on layer "
            f"{layer} (escapable sides: {emap.escapable_sides(pad) or 'none'})"
        )
    return hits[0]


#: The rank ``ACCESSIBLE`` sits behind ``FAVORED`` at. ⛔ ``BLOCKED`` is absent
#: on purpose -- it is an **assertion**, and :func:`escapable_sides_ranked` is a
#: list of what a consumer may reach for, never a list of everything asked.
_RANK: dict = {"FAVORED": 0, "ACCESSIBLE": 1}


def escapable_sides_ranked(emap: EscapeMap, pad: str, layer: int = 0,
                           ) -> tuple[tuple[str, float], ...]:
    """Every side ``pad`` can leave from, best first, each with its cost.

    ⭐⭐ **S5C's one addition to this module, and it adds no derivation.** The map
    already carries every ``(pad, side, layer)`` verdict; :func:`favored_side`
    reads exactly **one** of them, which is why the construction loop's side
    assignment is single-valued and why *"N and S are EMPTY on 3 of 4
    subjects"*. This returns the whole ranked list, so a consumer that wants to
    balance a ring has something to balance with.

    The order is ``FAVORED`` first, then ``ACCESSIBLE``, then the corridor
    distance, then the side letter -- ⛔ **a total key over content, never
    arrival order** (standing finding 8), and it is
    :func:`~skidl_layout.construct._ring2_side_excluding`'s existing rank
    verbatim rather than a second one.

    ⛔ **``BLOCKED`` is never offered.** The map's ``BLOCKED`` is an assertion
    the corridor pass made, and S1's soundness -- 127 routed probes, **zero
    over-reports** -- is the most valuable property this arc owns. Reaching for
    a blocked side would be the first thing that could cost it.

    ⛔ **Raises when the pad has none**, exactly as :func:`favored_side` does: an
    exposed pad genuinely has no lateral escape, and the caller is told rather
    than handed a plausible default (the observes-nothing rule).

    ⚠ Sides are in the footprint's **own** frame (the map is derived at rotation
    0 only). A consumer rotates the answer with :func:`rotate_escape`; it does
    not re-derive.
    """
    entries = [entry for entry in emap.entries_for(pad)
               if entry.layer == int(layer) and entry.access != "BLOCKED"]
    if not entries:
        raise ValueError(
            f"{emap.footprint}: pad {pad!r} escapes on NO side of layer "
            f"{layer} -- a ranked list over nothing is indistinguishable from "
            f"one that found everything (standing finding 1)"
        )
    ordered = sorted(entries,
                     key=lambda e: (_RANK.get(e.access, 2),
                                    round(float(e.distance_mm), 6), e.side))
    return tuple((entry.side, round(float(entry.distance_mm), 6))
                 for entry in ordered)


# --------------------------------------------------------------------------- #
# The derivation
# --------------------------------------------------------------------------- #
def escape_map_for(footprint: str, fp_lib_dirs, *, lane_mm: float,
                   clearance_mm: float, layers=(0,),
                   lane_source: str = "", clearance_source: str = "",
                   allow_no_escape: bool = False,
                   ) -> EscapeMap:
    """Derive the per-pad escape map for one bare footprint. No board, no router.

    ``footprint`` may arrive bare (``"R_0805_2012Metric"``) -- every
    ``.kicad_pcb`` this stack writes carries the bare form -- and is resolved
    against ``fp_lib_dirs`` before anything is measured.

    ⛔ **Raises rather than returning an empty or falsy map** (the
    observes-nothing rule, five instances in six runs): an unresolvable
    footprint, a footprint with no terminal pads, a derivation that produced no
    entries, or a map in which not one pad escapes anywhere.

    ⚠ ``allow_no_escape`` exists for the **census** and for nothing else, and
    the default is the raise. MEASURED on the corpus: ``LGA-12_2x2mm_P0.5mm``
    and ``LGA-14_3x2.5mm_P0.5mm`` really are ``BLOCKED`` on every pad and every
    side, and the reason is structural rather than a bug -- **the via lane
    (0.9048 mm) is wider than the pad pitch (0.5 mm)**, so on a fine-pitch part
    whose pads are *inset* from the box edge every straight corridor overlaps a
    neighbour inflated by clearance. ``LQFP-48`` is the same pitch and escapes
    only because its pads sit **flush** with the box edge, which
    ``escape_corridor_clear`` short-circuits before it tests any obstruction. So
    a sweep that must cover 100 % of a corpus opts in and **reports the list**;
    a single consumer that asked about one part is told loudly instead.
    """
    from .geometry import load_footprint_geometries

    dirs = [str(d) for d in (fp_lib_dirs or [])]
    resolved = resolve_footprint_name(str(footprint), dirs)
    if ":" not in resolved:
        raise ValueError(
            f"{footprint!r} does not resolve to a Library:Name in {dirs!r} -- "
            f"a bare name handed to the geometry loader resolves NOTHING and "
            f"falls back to a silent 2 x 2 mm box (standing finding 6)"
        )
    geometries = load_footprint_geometries({resolved}, dirs)
    geometry = geometries.get(resolved)
    if geometry is None:
        raise ValueError(f"{resolved!r}: no footprint geometry could be loaded "
                         f"from {dirs!r}")

    pads = terminal_pads(geometry)
    if not pads:
        raise ValueError(
            f"{resolved!r}: zero routable terminal pads "
            f"({len(geometry.pads)} aperture(s) declared, "
            f"{len(copper_pads(geometry))} on copper) -- an escape map over "
            f"nothing is indistinguishable from one that found everything"
        )
    non_copper = [pad for pad in geometry.pads if not _is_copper(pad)]
    kept = {id(pad) for pad in pads}
    excluded = [pad for pad in copper_pads(geometry) if id(pad) not in kept]
    layer_list = tuple(sorted({int(v) for v in layers}))
    if not layer_list:
        raise ValueError(f"{resolved!r}: no layers requested")

    # -- the singleton wrap: one local net per TERMINAL pad ----------------- #
    nets: dict[str, list[tuple[str, str]]] = {}
    for pad in pads:
        nets.setdefault(str(pad.number), []).append(("P1", str(pad.number)))
    #: ⚠ A duplicate pad NUMBER is real -- several apertures, one terminal, on
    #: some connectors and exposed pads. The wrap collapses them onto one net,
    #: which is correct (they are one electrical terminal) and is recorded.
    duplicates = sorted((n for n, pins in nets.items() if len(pins) > 1),
                        key=_pad_key)

    cell = synthesise_cell(
        name=f"@escape:{resolved}",
        members=[("P1", resolved, 0.0, 0.0, 0)],
        nets=nets,
        fp_lib_dirs=dirs,
    )
    unresolved = list(dict(cell.meta).get("unresolved_footprints") or ())
    if unresolved:
        raise ValueError(f"{resolved!r}: synthesise_cell could not resolve "
                         f"{unresolved!r}")

    ports = derive_access(cell, layers=layer_list, lane_mm=lane_mm,
                          clearance_mm=clearance_mm)
    if not ports:
        raise ValueError(f"{resolved!r}: derive_access returned no ports")

    # -- distances, and the cross-check that the two instruments agree ------ #
    #: ⚠ A pad NUMBER can own several apertures -- a switch's two contacts, a
    #: USB shield, a ``PowerPAK`` source paddle, a ``SOT-223`` tab. They are one
    #: electrical terminal, so the wrap gives them one net, and the terminal
    #: escapes if **any** of its apertures does. That is exactly what
    #: ``derive_access`` computes (best over the net's pads, same-net copper not
    #: obstructing), and mirroring it here rather than picking one aperture is
    #: what keeps the two instruments from disagreeing on 7 corpus footprints.
    apertures: dict[str, list] = {}
    for cell_pad in cell.pads:
        if cell_pad.local_net:
            apertures.setdefault(cell_pad.local_net, []).append(cell_pad)
    distance: dict[tuple[str, str, int], float] = {}
    clear_flag: dict[tuple[str, str, int], bool] = {}
    for net, members in apertures.items():
        same = frozenset(pad.label for pad in members)
        for layer in layer_list:
            for side in _SIDES:
                best_clear, best_distance, any_distance = False, None, None
                for cell_pad in members:
                    if layer != 0 and not cell_pad.through_board:
                        continue
                    clear, dist = escape_corridor_clear(
                        cell, cell_pad, side, layer, lane_mm=lane_mm,
                        clearance_mm=clearance_mm, same_net_labels=same)
                    if any_distance is None or dist < any_distance:
                        any_distance = dist
                    if clear and (best_distance is None or dist < best_distance):
                        best_clear, best_distance = True, dist
                distance[(net, side, layer)] = _q(
                    best_distance if best_distance is not None
                    else (any_distance or 0.0))
                clear_flag[(net, side, layer)] = bool(best_clear)

    entries: dict[tuple[str, str, int], PadEscape] = {}
    for port in ports:
        key = (port.local_net, port.side, int(port.layer))
        if key not in clear_flag:
            raise ValueError(f"{resolved!r}: derive_access produced {key!r}, "
                             f"which the corridor pass never asked about")
        agreed = clear_flag[key] == (port.access != "BLOCKED")
        if not agreed:
            # ⛔ Two instruments over the same primitive must not disagree. If
            # they ever do, the wrap has stopped being per-pad and every number
            # downstream is suspect.
            raise ValueError(
                f"{resolved!r}: pad {port.local_net} side {port.side} layer "
                f"{port.layer} -- derive_access says {port.access} while "
                f"escape_corridor_clear says clear={clear_flag[key]}"
            )
        entries[key] = PadEscape(
            pad=port.local_net, side=port.side, layer=int(port.layer),
            # ⛔ Everything from ``derive_access`` is ACCESSIBLE or BLOCKED
            # here; FAVORED is re-decided below against the occupancy rule.
            access=("BLOCKED" if port.access == "BLOCKED" else "ACCESSIBLE"),
            distance_mm=distance[key], source="corridor")

    expected = len(nets) * len(_SIDES) * len(layer_list)
    if len(entries) != expected:
        raise ValueError(f"{resolved!r}: the map is not total -- "
                         f"{len(entries)} entries, expected {expected}")

    # -- rows and the channel ---------------------------------------------- #
    physical = tuple(_q(v) for v in geometry.physical_bounds)
    clusters: dict[str, list[str]] = {}
    occupancy: dict[str, str] = {}
    for pad in pads:
        side = pad_occupancy_side(pad.x_mm, pad.y_mm, geometry.physical_bounds)
        occupancy[str(pad.number)] = side
        clusters.setdefault(side, []).append(str(pad.number))
    for side in clusters:
        clusters[side].sort(key=_pad_key)
    detection = _detection_class(pads, clusters)
    channel = _row_channel(pads, clusters,
                           obstructions=copper_pads(geometry),
                           lane_mm=lane_mm, clearance_mm=clearance_mm)

    # -- FAVORED, by the occupancy rule ------------------------------------ #
    #: ⛔ Distance FIRST -- ``derive_access``'s own ordering is preserved
    #: wherever it is not degenerate; occupancy only breaks the tie it leaves.
    #: Then ``side`` and ``layer``, so the key is total over content.
    for pad_number in sorted(nets, key=_pad_key):
        candidates = [entries[key] for key in entries
                      if key[0] == pad_number
                      and entries[key].access != "BLOCKED"]
        if not candidates:
            continue
        own = occupancy.get(pad_number)
        best = min(candidates,
                   key=lambda e: (e.distance_mm, 0 if e.side == own else 1,
                                  e.side, e.layer))
        key = (best.pad, best.side, best.layer)
        entries[key] = replace(entries[key], access="FAVORED")

    # -- the row-channel upgrade (the one authored rule) ------------------- #
    if channel is not None and channel.open:
        x_min, y_min, x_max, y_max = geometry.physical_bounds
        for pad in pads:
            number = str(pad.number)
            if channel.axis == "NS":
                near, far = ("N", "S") if pad.y_mm <= (y_min + y_max) / 2.0 \
                    else ("S", "N")
                dist = (pad.y_mm - y_min) if near == "N" else (y_max - pad.y_mm)
            else:
                near, far = ("W", "E") if pad.x_mm <= (x_min + x_max) / 2.0 \
                    else ("E", "W")
                dist = (pad.x_mm - x_min) if near == "W" else (x_max - pad.x_mm)
            del far
            for layer in layer_list:
                key = (number, near, layer)
                current = entries.get(key)
                # ⛔ Never a downgrade, and ⛔ never FAVORED: the overview says
                # the between-the-rows escape is ACCESSIBLE and not favoured,
                # because it is the detour and not the natural exit.
                if current is None or current.access != "BLOCKED":
                    continue
                entries[key] = PadEscape(pad=number, side=near, layer=layer,
                                         access="ACCESSIBLE",
                                         distance_mm=_q(dist),
                                         source="row_channel")

    ordered = tuple(sorted(entries.values(), key=lambda e: e.key))
    no_escape_anywhere = not any(e.access != "BLOCKED" for e in ordered)
    if no_escape_anywhere and not allow_no_escape:
        raise ValueError(
            f"{resolved!r}: every one of {len(ordered)} entries reads BLOCKED "
            f"-- a map on which nothing escapes anywhere observes nothing. "
            f"If this is the fine-pitch inset-pad case (lane {lane_mm} mm vs "
            f"the pad pitch), the CENSUS may pass allow_no_escape=True and "
            f"report it; a consumer may not."
        )

    emap = EscapeMap(
        footprint=resolved,
        pad_count=len(nets),
        entries=ordered,
        channel=channel,
        physical_bounds=physical,
        courtyard_bounds=(tuple(_q(v) for v in geometry.courtyard_bounds)
                          if geometry.courtyard_bounds is not None else None),
        lane_mm=_q(lane_mm),
        clearance_mm=_q(clearance_mm),
        layers=layer_list,
        detection=detection,
        meta={
            "requested_name": str(footprint),
            "fp_lib_dirs": dirs,
            "lane_source": lane_source or "caller",
            "clearance_source": clearance_source or "caller",
            "pad_apertures": len(geometry.pads),
            "copper_apertures": len(pads),
            "non_copper_pads": [
                {"number": str(p.number), "layers": list(p.layers)}
                for p in sorted(non_copper, key=lambda p: str(p.number))],
            "excluded_copper_pads": [
                {"number": str(p.number), "pad_type": str(p.pad_type),
                 "layers": list(p.layers),
                 "why": ("unnumbered" if str(p.number).strip() == ""
                         else "non-plated through hole")}
                for p in excluded],
            "no_escape_anywhere": bool(no_escape_anywhere),
            "duplicate_pad_numbers": duplicates,
            "through_board_pads": sorted((str(p.number) for p in pads
                                          if p.is_through_board),
                                         key=_pad_key),
            "occupancy": {k: occupancy[k]
                          for k in sorted(occupancy, key=_pad_key)},
            "clusters": {k: clusters[k] for k in sorted(clusters)},
            "favored_rule": "occupancy-tie-break-behind-distance",
            "box_used_for_verdicts": "physical_bounds (body ∪ pads)",
        },
    )
    without = emap.pads_without_escape
    if without:
        emap.meta["pads_without_escape"] = list(without)
    return emap


def escape_map_to_dict(emap: EscapeMap) -> dict:
    """A JSON-serialisable, order-stable view. ⭐ Two runs must give one blob."""
    return {
        "footprint": emap.footprint,
        "pad_count": emap.pad_count,
        "detection": emap.detection,
        "physical_bounds": list(emap.physical_bounds),
        "courtyard_bounds": (list(emap.courtyard_bounds)
                             if emap.courtyard_bounds is not None else None),
        "lane_mm": emap.lane_mm,
        "clearance_mm": emap.clearance_mm,
        "layers": list(emap.layers),
        "channel": emap.channel.to_dict() if emap.channel is not None else None,
        "counts": emap.counts(),
        "entries": [{"pad": e.pad, "side": e.side, "layer": e.layer,
                     "access": e.access, "distance_mm": e.distance_mm,
                     "source": e.source} for e in emap.entries],
        "meta": emap.meta,
    }


def escape_maps_for(footprints, fp_lib_dirs, *, lane_mm: float,
                    clearance_mm: float, layers=(0,), **kwargs,
                    ) -> dict[str, EscapeMap]:
    """:func:`escape_map_for` over many names, keyed by **resolved** name.

    ⛔ Iterated in sorted order over the resolved names so the sweep's artifact
    is byte-stable (rule 4). Raises on the first failure -- a sweep that
    silently skips a footprint is the observes-nothing defect in a wider hat.
    """
    dirs = [str(d) for d in (fp_lib_dirs or [])]
    resolved = sorted({resolve_footprint_name(str(f), dirs)
                       for f in footprints if str(f)})
    out: dict[str, EscapeMap] = {}
    for name in resolved:
        out[name] = escape_map_for(name, dirs, lane_mm=lane_mm,
                                   clearance_mm=clearance_mm, layers=layers,
                                   **kwargs)
    return out
