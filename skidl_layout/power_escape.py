# -*- coding: utf-8 -*-
"""The escape room around a fine-pitch controller (power-layout Phase 13).

Phase 12 identified the binding defect on this arc's four failing boards, and it
is **escape**, not congestion. Every net that fails to route is a controller
housekeeping net (``RT_N``, ``SS``, ``INTVCC``, ``UVLO``, ``VCC``, ``VFB``,
``TG``, ``SHDN_N``); **no power net has ever failed**. KRT says why in its own
log, 269 times across the Phase-12 runs: the pads are *"boxed in by static
obstacles (neighboring pads + clearance), not by congestion"*.

The cause is geometric and it has a number. A fine-pitch surface-mount
controller must leave the top layer through a via, and a via plus its clearance
on both sides needs :data:`ESCAPE_LANE_MM` of clear lane. **Every controller in
the corpus has less room than that** -- 0.578 mm on the one board that routes
fully (which is also the one with a through-hole DIP-8 controller, whose pins
reach both layers without an escape via) down to 0.000 mm on
``lt8710_inverting``.

This module owns the geometry of that observation. It is pure computation --
no routing, no board writes, no placement -- and it serves two mechanisms:

* **hold the neighbours off** -- :func:`escape_far_constraints` turns the lane
  into ``Far`` constraints the placer already honours, so the room exists in the
  first place. This is Phase 13's primary lever;
* **keep tracks off the via sites** -- :func:`annulus_polygon` plus
  :func:`write_keepout_polygons` draw the ring KRT's ``--keepout`` reads out of
  a board's own file text, so nothing camps on room that does exist.

⚠⚠ **KRT's keepout is NOT net-scoped.** ``obstacle_map.add_user_keepout_obstacles``
blocks every copper layer for **every** net being routed, so an annulus drawn
round a controller blocks the controller's own escape too. Phase 13 measures that
honestly rather than pretending otherwise; the working form needs the escapes
routed *first* and then the keepout applied to a second pass, which is Phase 14's
work. The geometry is built here so Phase 14 does not re-derive it.

The measurement half started life as ``skidl-eda/canaries/ws8_escape_room.py``
(Phase 12, WS-8) and was moved here so there is exactly one implementation; that
canary now imports from this module and its published numbers are this module's
regression baseline.
"""

from __future__ import annotations

import math
import os
import re
import uuid
from dataclasses import dataclass, field

from .constraints import FarConstraint

__all__ = [
    "ESCAPE_LANE_MM",
    "ESCAPE_ROOM_FIELD",
    "EscapeRoom",
    "annulus_polygon",
    "apply_escape_room",
    "declared_escape_refs",
    "escape_constraints",
    "escape_far_constraints",
    "lane_from_fab",
    "mark_escape_room",
    "measure_escape_room",
    "measure_escape_rooms",
    "part_rect",
    "rect_gap",
    "resolve_escape_targets",
    "resolve_lane_mm",
    "write_keepout_polygons",
]


#: Via diameter + clearance on both sides -- the room one escape via and its
#: track needs, on the ``oshpark-2l`` rules this arc's corpus is built to.
#: 0.6 mm via + 2 x 0.1524 mm clearance = 0.9048 mm. Source: Phase 12, WS-8.
#: :func:`lane_from_fab` derives the same number from a :class:`FabSpec`, which
#: is the form a caller should prefer -- this constant is the documented default
#: for a call with no spec in hand, never a substitute for one.
ESCAPE_LANE_MM = 0.9048

#: KRT's own default keepout layer (``routing_defaults.py:136``), and the layer
#: :func:`write_keepout_polygons` writes to unless told otherwise.
DEFAULT_KEEPOUT_LAYER = "User.2"

#: KiCad-10 canonical layer ids for the nine user layers. The skidl-layout board
#: writer declares none of them (``writer._LAYERS`` stops at ``B.Fab``), so a
#: board that is to carry a keepout polygon needs the declaration added -- see
#: :func:`write_keepout_polygons`. Read off KiCad's own demo boards, whose
#: ``(layers ...)`` block is otherwise byte-identical to the writer's.
_USER_LAYER_IDS = {f"User.{n}": 37 + 2 * n for n in range(1, 10)}

#: Where the declaration is inserted -- immediately after the last layer the
#: writer emits, which is where KiCad itself puts the user layers.
#: ⚠ NOT anchored to end-of-line: the s-expression writer closes the ``(layers``
#: block on the same line as its last entry (``(33 "B.Fab" user))``), so an
#: ``$``-anchored pattern never matches a real board. Cost one smoke run.
_LAST_WRITER_LAYER_RE = re.compile(r'^([ \t]*)\(33 "B\.Fab" user\)', re.MULTILINE)

_NAMESPACE_UUID = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


# --------------------------------------------------------------------------- #
# Geometry -- ported verbatim from ws8_escape_room.py so the WS-8 table stands
# --------------------------------------------------------------------------- #

def part_rect(part, geometry) -> tuple[float, float, float, float]:
    """Axis-aligned courtyard rectangle for a placed part, in mm.

    ⚠ The 90-degree swap is load-bearing: ``geometry.bounds`` is the footprint's
    own unrotated courtyard, and a part placed at 90 or 270 degrees occupies the
    transposed box. Getting this wrong makes every gap on a rotated part read as
    the wrong axis's clearance.
    """
    x0, y0, x1, y1 = geometry.bounds
    width, height = (x1 - x0), (y1 - y0)
    rot = abs(float(getattr(part, "rot_deg", 0.0) or 0.0)) % 180.0
    if 45.0 < rot < 135.0:
        width, height = height, width
    cx, cy = float(part.x_mm), float(part.y_mm)
    return (cx - width / 2.0, cy - height / 2.0,
            cx + width / 2.0, cy + height / 2.0)


def rect_gap(a, b) -> float:
    """Edge-to-edge gap between two axis-aligned rectangles. 0 if they touch.

    Diagonal separation is the Euclidean corner distance; an overlap on either
    axis reduces to the other axis's gap, which is what a via lane actually
    needs.
    """
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy) if (dx and dy) else max(dx, dy)


def annulus_polygon(courtyard, lane_mm: float) -> list[list[tuple[float, float]]]:
    """The ring around ``courtyard``, ``lane_mm`` wide, as four rectangles.

    ⛔ **Deliberately NOT one keyhole outline.** KRT's
    ``kicad_parser._parse_gr_polys_on_layer`` reads every ``gr_poly``
    independently and does not model a polygon with a hole; a self-intersecting
    outline would be read as a solid region covering the controller's own pads,
    which is the exact opposite of the intent. Four side rectangles say the same
    thing with nothing to misread.

    ⚠ **The controller's own courtyard is excluded by construction** -- no
    returned rectangle overlaps it -- so a keepout built from these never blocks
    the controller's pads. :func:`~tests.test_power_escape` asserts it.

    Returns four closed rectangles (left, right, bottom, top) in the courtyard's
    own coordinate frame, or ``[]`` for a non-positive lane.
    """
    lane = float(lane_mm or 0.0)
    if lane <= 0.0:
        return []
    x0, y0, x1, y1 = (float(v) for v in courtyard)
    ox0, oy0, ox1, oy1 = x0 - lane, y0 - lane, x1 + lane, y1 + lane
    return [
        # left / right run the full outer height; top / bottom fill the gap
        # between them, so the four boxes tile the ring without overlapping.
        [(ox0, oy0), (x0, oy0), (x0, oy1), (ox0, oy1)],
        [(x1, oy0), (ox1, oy0), (ox1, oy1), (x1, oy1)],
        [(x0, oy0), (x1, oy0), (x1, y0), (x0, y0)],
        [(x0, y1), (x1, y1), (x1, oy1), (x0, oy1)],
    ]


# --------------------------------------------------------------------------- #
# The lane, and where it came from
# --------------------------------------------------------------------------- #

def lane_from_fab(fab_spec) -> float | None:
    """One escape via's lane width, in mm, from a :class:`FabSpec`.

    ``via_size + 2 * min_clearance`` -- the via's own copper plus the clearance
    it owes a neighbour on each side. On ``oshpark-2l`` that is
    ``0.6 + 2 * 0.1524 = 0.9048``, which is :data:`ESCAPE_LANE_MM` exactly.

    ⛔ ``min_clearance_mm`` and not ``clearance_mm``: the question is whether a
    via *can* fit, so the fab's published floor is the right bound. Grading the
    lane against the routed-at clearance would ask for room nobody needs.
    """
    if fab_spec is None:
        return None
    via = getattr(fab_spec, "via_size_mm", None)
    clearance = getattr(fab_spec, "min_clearance_mm", None)
    if via is None or clearance is None:
        return None
    return float(via) + 2.0 * float(clearance)


def resolve_lane_mm(lane_mm=None, fab_spec=None) -> tuple[float, str]:
    """``(lane_mm, source)`` -- the arc's rule that every number carries its source.

    Precedence: an explicit numeric ``lane_mm`` (including one arriving as a
    ``float`` through an ``escape_room=`` knob) beats a spec-derived one, which
    beats :data:`ESCAPE_LANE_MM`. ``True`` means "derive it", not "1.0".
    """
    if lane_mm is not None and not isinstance(lane_mm, bool):
        return float(lane_mm), "explicit"
    derived = lane_from_fab(fab_spec)
    if derived is not None:
        return derived, f"fab:{getattr(fab_spec, 'name', '?')}"
    return ESCAPE_LANE_MM, "default:ESCAPE_LANE_MM"


# --------------------------------------------------------------------------- #
# The DECLARATION -- the producer says which parts need room
# --------------------------------------------------------------------------- #
#
# ⛔⛔ Why this exists, and it is a correction rather than a feature.
#
# Before Phase 13 there was no way to say "this part needs escape room", so
# three different routines each *guessed*, and they did not agree:
#
#   * ``power_roles.classify_power_roles`` -> every part typed ``controller``
#     that also anchors a switching stage. Silent on a board with no converter.
#   * ``_controller_by_pad_count`` below -> the placed part with the most pads.
#     This is what WS-8 used, and on ``uc3844_flyback`` it picked ``M1`` (a
#     9-pad DPAK) over the 8-pad DIP-8 controller -- so the published escape
#     table measured the MOSFET. That is retraction R-1 of the Phase-13 report.
#   * ``drive_phase12._static_profile`` -> the part with the most *pins*, a
#     third answer again.
#
# A guess is the right behaviour for a board nobody has annotated, and the wrong
# behaviour for a board somebody has. **A declaration beats every guess**, it
# names as many parts as the board has, and it travels with the netlist rather
# than with the driver that happens to be running.

#: The ``Part.fields`` key a declaration is stored under. ``fields`` is skidl's
#: own per-part dict and it survives ``Part.copy()``, which a plain attribute
#: does not -- so a declared part stays declared through a ``5 * U1``-style
#: replication.
ESCAPE_ROOM_FIELD = "escape_room"


def mark_escape_room(*parts, lane_mm=None, clear: bool = False) -> list[str]:
    """Declare that each part needs a clear escape lane around it.

    The producer-side half of the escape lever, called from the board source
    where the part is built::

        from skidl_eda import mark_escape_room

        U1 = Part("Regulator_Switching", "LT3757", footprint=...)
        U2 = Part("MCU_ST_STM32F1", "STM32F103C8Tx", footprint=...)
        mark_escape_room(U1, U2)                  # both, at the fab's own lane
        mark_escape_room(U3, lane_mm=1.2)         # a wider lane for one part

    **Any number of parts per board.** Each declared part gets its own annulus,
    its own neighbour set and its own all-or-nothing verdict, so one crowded
    controller does not veto the room around another.

    Args:
        *parts: skidl ``Part`` objects (or anything with a ``fields`` dict).
        lane_mm: the clear lane this part needs, in mm. ``None`` (the normal
            case) means **derive it from the fab spec** at layout time --
            ``via_size + 2 x min_clearance`` -- so the board does not hardcode a
            number that belongs to the fabricator.
        clear: remove the declaration instead of adding it.

    Returns:
        The refs marked, in the order given. A part with no usable field store
        is skipped rather than raising -- and is absent from the return, which
        is how a caller detects it.
    """
    marked: list[str] = []
    value = True if lane_mm is None else float(lane_mm)
    for part in parts:
        store = getattr(part, "fields", None)
        if store is None:
            try:
                part.fields = store = {}
            except Exception:                      # noqa: BLE001
                continue
        try:
            if clear:
                store.pop(ESCAPE_ROOM_FIELD, None)
            else:
                store[ESCAPE_ROOM_FIELD] = value
        except Exception:                          # noqa: BLE001
            continue
        ref = getattr(part, "ref", None)
        marked.append(str(ref) if ref is not None else "")
    return marked


def _declared_on(part):
    """This part's declaration, or ``None``. ``(True | float)``.

    Reads the same three holders skidl itself accepts for extra part data --
    ``fields``, ``_extra_fields`` and a plain attribute -- because a part loaded
    from a library, a part built in source and a snapshot part do not all carry
    the same one.
    """
    for holder in ("fields", "_extra_fields"):
        store = getattr(part, holder, None)
        if isinstance(store, dict) and ESCAPE_ROOM_FIELD in store:
            return store[ESCAPE_ROOM_FIELD]
    return getattr(part, ESCAPE_ROOM_FIELD, None)


def declared_escape_refs(circuit) -> dict:
    """``{ref: lane_mm | None}`` for every part the producer marked.

    ``None`` as a value means "this part is declared, derive its lane from the
    fab spec" -- distinct from the ref being absent, which means undeclared.
    Order follows the circuit's own part order, so the result is deterministic
    without comparing reference designators.
    """
    out: dict = {}
    for part in getattr(circuit, "parts", None) or []:
        ref = getattr(part, "ref", None)
        if ref is None:
            continue
        declared = _declared_on(part)
        if declared is None or declared is False:
            continue
        out[str(ref)] = None if declared is True else float(declared)
    return out


# --------------------------------------------------------------------------- #
# Finding the controller -- declaration first, then topology, never by name
# --------------------------------------------------------------------------- #

def _stage_controller_refs(power_stage_plan) -> list[str]:
    """Every ``controller_ref`` a stage plan names, in the plan's own order."""
    out: list[str] = []
    for stage in list(getattr(power_stage_plan, "stages", None) or []):
        ref = getattr(stage, "controller_ref", None)
        if ref and ref not in out:
            out.append(str(ref))
    return out


def _controller_by_pad_count(placed, geom_of):
    """WS-8's fallback: the placed part whose footprint carries the most pads.

    ⛔ Not by reference string and not by footprint name. But also **not
    reliable** -- see the block comment above :func:`mark_escape_room`. "Most
    pads" is a proxy for "most connections to make", and a power MOSFET in a
    DPAK with thermal sub-pads beats a DIP-8 controller on it. This is the
    last resort for an unannotated board, and callers that can tell the
    difference should say so with :func:`mark_escape_room`.
    """
    def _pads(part):
        geometry = geom_of(part)
        return len(getattr(geometry, "pads", []) or []) if geometry else 0

    return max(placed, key=_pads, default=None)


def resolve_escape_targets(*, placed_refs, circuit=None, controller_ref=None,
                           power_stage_plan=None, fallback=None) -> tuple:
    """``([(ref, lane_mm | None)], source)`` -- who gets room, and who said so.

    **The precedence, and it is the whole point of the declaration:**

    1. an explicit ``controller_ref`` (a str or an iterable) -- the caller wins;
    2. **the producer's own marks** (:func:`mark_escape_room`) -- as many parts
       as the board declared;
    3. the classifier's ``controller_ref`` per switching stage;
    4. ``fallback`` -- the pad-count guess, and *only* one part.

    ⚠ Steps 3 and 4 are guesses and are labelled as such in ``source``, so a
    report can say whether a number describes the part somebody meant or the
    part something inferred. Refs not actually placed are dropped at every step.
    """
    placed = set(placed_refs or ())

    def _keep(pairs):
        return [(ref, lane) for ref, lane in pairs if ref in placed]

    if controller_ref is not None:
        refs = ([str(controller_ref)] if isinstance(controller_ref, str)
                else [str(r) for r in controller_ref])
        kept = _keep((ref, None) for ref in refs)
        if kept:
            return kept, "explicit"

    declared = _keep(declared_escape_refs(circuit).items()) if circuit is not None else []
    if declared:
        return declared, "declared"

    staged = _keep((ref, None) for ref in _stage_controller_refs(power_stage_plan))
    if staged:
        return staged, "power_stage_plan"

    if fallback is not None and str(fallback) in placed:
        return [(str(fallback), None)], "pad_count"
    return [], "none"


# --------------------------------------------------------------------------- #
# Result shape
# --------------------------------------------------------------------------- #

@dataclass
class EscapeRoom:
    """How much clear room one controller has to escape into.

    ``lane_source`` records where ``lane_mm`` came from (``"explicit"``,
    ``"fab:oshpark-2l"``, ``"default:ESCAPE_LANE_MM"``) so a report can say which
    number it graded against rather than implying a universal one.
    """

    controller_ref: str
    courtyard: tuple[float, float, float, float]
    lane_mm: float
    neighbor_gaps: dict = field(default_factory=dict)
    tight_refs: list = field(default_factory=list)
    annulus: list = field(default_factory=list)
    lane_source: str = "default:ESCAPE_LANE_MM"
    controller_pads: int = 0
    controller_source: str = "pad_count"

    @property
    def nearest_gap_mm(self) -> float | None:
        return min(self.neighbor_gaps.values(), default=None)

    @property
    def nearest_ref(self) -> str | None:
        if not self.neighbor_gaps:
            return None
        return min(self.neighbor_gaps.items(), key=lambda kv: (kv[1], kv[0]))[0]

    @property
    def n_tight(self) -> int:
        return len(self.tight_refs)

    def to_dict(self) -> dict:
        nearest = self.nearest_gap_mm
        return {
            "controller": self.controller_ref,
            "controller_pads": self.controller_pads,
            "controller_source": self.controller_source,
            "courtyard_mm": [round(v, 3) for v in self.courtyard],
            "lane_mm": round(self.lane_mm, 4),
            "lane_source": self.lane_source,
            "nearest_gap_mm": None if nearest is None else round(nearest, 3),
            "nearest_ref": self.nearest_ref,
            "neighbors_inside_one_via_lane": list(self.tight_refs),
            "n_tight": self.n_tight,
            "five_closest": [
                (ref, round(gap, 3))
                for ref, gap in sorted(self.neighbor_gaps.items(),
                                       key=lambda kv: (kv[1], kv[0]))[:5]
            ],
        }


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #

def measure_escape_rooms(result, fp_lib_dirs=None, *, lane_mm=None,
                         controller_ref=None, fab_spec=None,
                         fp_geometries=None, circuit=None) -> list:
    """Measure the room around **every** part that needs it, on a placement.

    Args:
        result: a :class:`~skidl_layout.engine.LayoutResult` (duck-typed:
            ``placed_parts``, optionally ``power_stage_plan`` and ``circuit``).
        fp_lib_dirs: footprint roots, for loading courtyard geometry. Ignored
            when ``fp_geometries`` is supplied.
        lane_mm: an explicit lane in mm, applied to every target. ``None`` ->
            each target's **declared** lane if it has one, else derived from
            ``fab_spec``, else :data:`ESCAPE_LANE_MM`.
        controller_ref: name the target(s) explicitly -- a ref or an iterable of
            refs. ``None`` (the normal case) resolves them through
            :func:`resolve_escape_targets`.
        fab_spec: a :class:`~skidl_layout.fabspec.FabSpec` to derive the lane
            from.
        fp_geometries: pre-loaded ``{footprint: FootprintGeometry}``, so a caller
            measuring several arms of one board pays the library read once.
        circuit: the netlist, so producer :func:`mark_escape_room` declarations
            are read. ``result.circuit`` is used when this is omitted.

    Returns:
        One :class:`EscapeRoom` per resolved target, in resolution order.
        Empty when there is no placement or nothing resolves -- never a guess
        dressed up as a measurement.
    """
    placed = list(getattr(result, "placed_parts", None) or [])
    if not placed:
        return []

    if fp_geometries is None:
        from .geometry import load_footprint_geometries

        fp_geometries = load_footprint_geometries(
            {str(p.footprint) for p in placed if getattr(p, "footprint", None)},
            fp_lib_dirs or [])

    def _geom(part):
        return fp_geometries.get(str(getattr(part, "footprint", "") or ""))

    by_ref = {str(p.ref): p for p in placed}
    if circuit is None:
        circuit = getattr(result, "circuit", None)
    fallback = _controller_by_pad_count(placed, _geom)
    targets, source = resolve_escape_targets(
        placed_refs=by_ref,
        circuit=circuit,
        controller_ref=controller_ref,
        power_stage_plan=getattr(result, "power_stage_plan", None)
                         or getattr(result, "power_plan", None),
        fallback=None if fallback is None else fallback.ref,
    )

    rooms: list = []
    for ref, declared_lane in targets:
        controller = by_ref.get(ref)
        if controller is None or _geom(controller) is None:
            continue
        # ⚠ Precedence within one target: an explicit call argument beats the
        # part's own declaration, which beats the fab spec. So a sweep can
        # override every board at once without editing any board's source.
        lane, lane_source = resolve_lane_mm(
            lane_mm if lane_mm is not None else declared_lane, fab_spec)
        if lane_mm is None and declared_lane is not None:
            lane_source = "declared"
        courtyard = part_rect(controller, _geom(controller))

        gaps: dict = {}
        for part in placed:
            if str(part.ref) == ref or _geom(part) is None:
                continue
            gaps[str(part.ref)] = rect_gap(courtyard,
                                           part_rect(part, _geom(part)))
        tight = [r for r, gap in sorted(gaps.items(), key=lambda kv: (kv[1], kv[0]))
                 if gap < lane]
        rooms.append(EscapeRoom(
            controller_ref=ref,
            courtyard=courtyard,
            lane_mm=lane,
            neighbor_gaps=gaps,
            tight_refs=tight,
            annulus=annulus_polygon(courtyard, lane),
            lane_source=lane_source,
            controller_pads=len(getattr(_geom(controller), "pads", []) or []),
            controller_source=source,
        ))
    return rooms


def measure_escape_room(result, fp_lib_dirs=None, *, lane_mm=None,
                        controller_ref=None, fab_spec=None,
                        fp_geometries=None, circuit=None) -> "EscapeRoom | None":
    """The first (or only) escape room on a placement, or ``None``.

    The singular form :mod:`skidl-eda`'s ``ws8_escape_room`` canary and the
    Phase-13 gates read. **A board may declare several parts** -- use
    :func:`measure_escape_rooms` for all of them; this returns the first in
    resolution order so a single-controller board reads exactly as before.
    """
    rooms = measure_escape_rooms(result, fp_lib_dirs, lane_mm=lane_mm,
                                 controller_ref=controller_ref,
                                 fab_spec=fab_spec, fp_geometries=fp_geometries,
                                 circuit=circuit)
    return rooms[0] if rooms else None


# --------------------------------------------------------------------------- #
# The lever -- position-FREE constraints
# --------------------------------------------------------------------------- #

def _half_diagonal(size) -> float | None:
    if size is None:
        return None
    return math.hypot(size[0], size[1]) / 2.0


def escape_far_constraints(power_stage_plan, footprint_by_ref, *,
                           fp_geometries=None, fp_bboxes=None,
                           lane_mm=None, fab_spec=None, circuit=None) -> list:
    """One :class:`FarConstraint` per part, holding it off the controller.

    ⛔⛔ **No positions in, on purpose.** ``power_constraints`` is a pure function
    of the plan and the footprint geometry precisely so the Phase-2 objective
    that judges its output is not scoring a placement against constraints derived
    from that same placement. "Every neighbour *currently* inside the lane" is
    therefore not something this function can ask -- and it does not need to.
    The placer's ``far`` pass is already conditional::

        if current >= constraint.distance_mm:  continue

    so emitting the constraint for **every** part and letting the pass skip the
    ones already far enough is the same rule, expressed without a coordinate.

    ⚠⚠ **The unit trap.** ``FarConstraint.distance_mm`` is centre-to-centre
    Euclidean distance between part *origins* (``placer.py`` ~line 755), applied
    as an unconditional radial push. The escape requirement is an *edge* gap.
    The conversion used here is::

        distance_mm = lane_mm + r(controller) + r(neighbour)

    with ``r`` the courtyard **half-diagonal** -- the same corner-on reach every
    other generated distance in this arc is sized from. It over-delivers on the
    axes and is exactly right on the diagonal, which is the conservative
    direction: the constraint asks for at least a lane and sometimes more.

    ⛔ **That conversion is a claim, and the arc's rule is that a claim is
    measured, not asserted.** ``drive_phase13`` gate E3 re-measures the resulting
    edge gap with :func:`measure_escape_room` rather than trusting this formula.

    Returns constraints in ``footprint_by_ref``'s own insertion order (which is
    the circuit's part order), so the emitted list is deterministic without ever
    comparing reference designators. A part whose courtyard cannot be loaded is
    skipped -- a flat-millimetre guess is not a substitute.
    """
    # Declaration first, classifier second -- the same precedence the measuring
    # and enforcing halves use, so all three agree on who the ICs are.
    declared = declared_escape_refs(circuit) if circuit is not None else {}
    controllers = list(declared) or _stage_controller_refs(power_stage_plan)
    if not controllers:
        return []
    lane, _source = resolve_lane_mm(lane_mm, fab_spec)
    if lane <= 0.0:
        return []
    per_target = {ref: declared.get(ref) for ref in controllers}

    fp_geometries = dict(fp_geometries or {})
    fp_bboxes = dict(fp_bboxes or {})
    footprint_by_ref = dict(footprint_by_ref or {})

    def _radius(ref):
        footprint = footprint_by_ref.get(ref)
        if not footprint:
            return None
        geometry = fp_geometries.get(footprint)
        if geometry is not None:
            x0, y0, x1, y1 = geometry.bounds
            return _half_diagonal((max(0.0, x1 - x0), max(0.0, y1 - y0)))
        bbox = fp_bboxes.get(footprint)
        if bbox is not None:
            return _half_diagonal((max(0.0, float(bbox[0])),
                                   max(0.0, float(bbox[1]))))
        return None

    out: list = []
    claimed: set = set()
    for controller in controllers:
        r_controller = _radius(controller)
        if r_controller is None:
            continue
        # Each declared IC may ask for its own lane; an explicit ``lane_mm``
        # overrides every one of them.
        own = per_target.get(controller)
        target_lane = lane if (lane_mm is not None or own is None) else float(own)
        for ref in footprint_by_ref:
            if ref == controller or ref in claimed or ref in controllers:
                continue
            r_ref = _radius(ref)
            if r_ref is None:
                continue
            claimed.add(ref)
            out.append(FarConstraint(
                ref=ref, target_ref=controller,
                distance_mm=round(target_lane + r_controller + r_ref, 6)))
    return out


def escape_constraints(result, fp_lib_dirs=None, *, lane_mm=None,
                       controller_ref=None, fab_spec=None,
                       fp_geometries=None) -> list:
    """The position-AWARE convenience form: constrain only the tight neighbours.

    Measures the placement first (:func:`measure_escape_room`) and emits a
    ``Far`` only for the refs actually inside the lane. **This is a diagnostic
    and a caller-facing convenience, not what the engine uses** -- the engine
    path goes through :func:`escape_far_constraints`, which is position-free and
    therefore safe to hand to a candidate generator that is about to be scored.
    Reaching for this one inside placement would close the loop Phase 3's
    "no positions in" rule exists to keep open.
    """
    room = measure_escape_room(result, fp_lib_dirs, lane_mm=lane_mm,
                               controller_ref=controller_ref, fab_spec=fab_spec,
                               fp_geometries=fp_geometries)
    if room is None:
        return []
    placed = {str(p.ref): p for p in getattr(result, "placed_parts", None) or []}
    if fp_geometries is None:
        from .geometry import load_footprint_geometries

        fp_geometries = load_footprint_geometries(
            {str(p.footprint) for p in placed.values()
             if getattr(p, "footprint", None)}, fp_lib_dirs or [])

    controller = placed.get(room.controller_ref)
    geometry = fp_geometries.get(str(getattr(controller, "footprint", "") or ""))
    r_controller = _half_diagonal(
        None if geometry is None
        else (geometry.bounds[2] - geometry.bounds[0],
              geometry.bounds[3] - geometry.bounds[1]))
    if r_controller is None:
        return []

    out: list = []
    for ref in room.tight_refs:
        part = placed.get(ref)
        geometry = fp_geometries.get(str(getattr(part, "footprint", "") or ""))
        if geometry is None:
            continue
        r_ref = _half_diagonal((geometry.bounds[2] - geometry.bounds[0],
                                geometry.bounds[3] - geometry.bounds[1]))
        out.append(FarConstraint(
            ref=ref, target_ref=room.controller_ref,
            distance_mm=round(room.lane_mm + r_controller + r_ref, 6)))
    return out


# --------------------------------------------------------------------------- #
# The enforcement pass -- what a FarConstraint alone cannot do
# --------------------------------------------------------------------------- #
#
# ⭐⭐ MEASURED, and it is the central finding of Phase 13.
#
# ``LayoutConstraints.far`` is read in exactly two places in this engine:
# ``placer.py``'s post-placement push (which runs inside ``place_parts``, i.e. on
# the candidate SEED) and ``engine._constraint_floorplan_refs`` (which only
# decides what legalization may move). **Nothing downstream of the seed enforces
# it.** So a ``FarConstraint`` is a *hint to the seed*, not a rule about the
# placement -- refinement is free to close the gap again, and on a crowded board
# it does.
#
# Measured on ``lt3757_sepic`` with the escape lane wired through
# ``generate_power_constraints`` alone: the baseline candidate's SEED escape gap
# rose 1.163 -> 2.055 mm (the constraint works, exactly as designed), and the
# FINAL selected placement's gap fell 0.540 -> 0.050 mm. The lever moved its own
# judge BACKWARDS. That is not a tuning problem; it is the seam being in the
# wrong place.
#
# This pass is the seam that works: it runs on the already-selected placement,
# so nothing can undo it, and it is written so it **cannot make the placement
# worse** -- a move is accepted only when the moved part still sits inside the
# outline and still clears every other part by the validator's own rule
# (``validator._placed_bounds`` + ``_rects_overlap``, the same functions
# ``validate_placement`` grades with). A part with nowhere legal to go is
# recorded as blocked rather than shoved, so "this board has no room" is a
# reported number instead of a manufactured overlap.
# --------------------------------------------------------------------------- #

#: The order the four axis pushes are tried in when their magnitudes tie --
#: named so the tie-break is documented rather than an accident of tuple order.
_PUSH_AXES = ("+x", "-x", "+y", "-y")


def _push_options(rect, courtyard, lane: float):
    """``[(magnitude, axis, dx, dy)]`` -- the four ways to clear the lane.

    Each entry moves ``rect`` just far enough that its edge stands ``lane`` clear
    of ``courtyard`` on that side. The smallest magnitude is the natural
    direction: a part sitting to the right of the controller gets a small ``+x``
    and a large ``-x``, so sorting by magnitude picks the side it is already on
    without anything having to reason about quadrants.
    """
    # ⚠ A nanometre of slop, and it is load-bearing for the GATE rather than for
    # the board: pushing to exactly ``lane`` lands on ``gap == lane`` only in
    # exact arithmetic, and in floating point it lands a few ULPs short, so
    # ``gap < lane`` stays true and the part reads as still-tight. Measured:
    # ``lt3757_sepic``'s CIN3 came out at 0.9047999999 against a 0.9048 lane.
    lane = float(lane) + 1e-6
    x0, y0, x1, y1 = rect
    cx0, cy0, cx1, cy1 = courtyard
    moves = {
        "+x": (cx1 + lane - x0, 0.0),
        "-x": (cx0 - lane - x1, 0.0),
        "+y": (0.0, cy1 + lane - y0),
        "-y": (0.0, cy0 - lane - y1),
    }
    out = [(math.hypot(dx, dy), _PUSH_AXES.index(axis), axis, dx, dy)
           for axis, (dx, dy) in moves.items()]
    out.sort()
    return [(mag, axis, dx, dy) for mag, _order, axis, dx, dy in out]


def apply_escape_room(placed_parts, *, lane_mm, fp_geometries, fp_bboxes=None,
                      outline=None, clearance_mm: float = 0.5,
                      controller_ref=None, power_stage_plan=None,
                      partial: bool = False, circuit=None) -> tuple:
    """Push each declared IC's tightest neighbours out to one clear via lane.

    **Any number of ICs per board.** Targets are resolved by
    :func:`resolve_escape_targets` — an explicit ``controller_ref`` first, then
    the producer's own :func:`mark_escape_room` declarations (read off
    ``circuit``), then the classifier's controllers, then the pad-count guess.
    Each target carries its own lane, its own neighbour set and its own
    all-or-nothing verdict, reported under ``info["per_controller"]``.

    Runs on a **finished** placement -- after candidate selection, where nothing
    downstream can close the gap again. See the block comment above for why that
    is the only seam that holds.

    ⛔ **Cannot make the placement worse, by construction.** A move is accepted
    only when the moved part's physical bounds stay inside ``outline`` and clear
    every other part by ``clearance_mm``, judged with the very functions
    :func:`~skidl_layout.validator.validate_placement` uses. On a board with no
    room the pass moves nothing and says so; it never buys escape room by
    manufacturing an overlap.

    ⚠ The pass reports its own before/after gap, so its caller never has to
    trust that the push arithmetic was right -- ``info["gap_before_mm"]`` and
    ``info["gap_after_mm"]`` are re-measured from the placements themselves.

    ⭐⭐ **All-or-nothing, and this is the measured heart of the phase.** If even
    one neighbour has nowhere legal to go, the controller still cannot put a via
    down, so every move made on that board bought **nothing** -- and moving parts
    is not free. Measured across the corpus, the correlation is exact:

    ======================  =========  ===========================================
    board                   blocked    outcome with the moves kept
    ======================  =========  ===========================================
    ``lt3724_buck``         none       lane cleared, routing **10/16 -> 12/16**
    ``lt3757_sepic``        none       lane cleared, no change and no cost
    ``uc3844_flyback``      ``RS``     lane still 0.000 mm, no gain
    ``lt3758_flyback``      ``RS``     lane still 0.457 mm, no gain
    ``lt8710_inverting``    ``MN``     lane still 0.000 mm, and it **cost** a
                                       broken ``GND``, a lost zone (4 -> 3) and
                                       ``IMON`` necked 0.300 -> 0.1524 mm
    ======================  =========  ===========================================

    So the default **reverts the whole pass** unless every tight neighbour
    cleared. ``partial=True`` keeps whatever moved and is how the table above is
    re-derived (``drive_phase13`` arm Bp); it is not the recommended setting.
    ⛔ A partially-relieved annulus is not an escape route.

    Returns:
        ``(placed_parts, info)``. ``placed_parts`` is a new list (the input is
        never mutated) and is the *same objects* when nothing moved, so a caller
        can cheaply test ``if info["moved"]``.
    """
    from dataclasses import replace as _replace

    from .validator import (
        _outline_contains_bounds,
        _placed_bounds,
        _rects_overlap,
        _same_physical_side,
    )

    placed = list(placed_parts or [])
    lane_default = float(lane_mm or 0.0)
    info = {"lane_mm": round(lane_default, 6), "controller": None, "moved": {},
            "blocked": [], "tight_before": [], "tight_after": [],
            "gap_before_mm": None, "gap_after_mm": None,
            "partial": bool(partial), "reverted": False, "reverted_moves": {},
            "controllers": [], "controller_source": "none",
            "per_controller": {}}
    if not placed or lane_default <= 0.0:
        return placed, info

    fp_geometries = dict(fp_geometries or {})
    fp_bboxes = dict(fp_bboxes or {})

    def _geom(part):
        return fp_geometries.get(str(getattr(part, "footprint", "") or ""))

    by_ref = {str(p.ref): p for p in placed}
    fallback = _controller_by_pad_count(placed, _geom)
    targets, source = resolve_escape_targets(
        placed_refs=by_ref,
        circuit=circuit,
        controller_ref=controller_ref,
        power_stage_plan=power_stage_plan,
        fallback=None if fallback is None else fallback.ref,
    )
    targets = [(ref, lane) for ref, lane in targets
               if by_ref.get(ref) is not None and _geom(by_ref[ref]) is not None]
    if not targets:
        return placed, info
    info["controller"] = targets[0][0]
    info["controllers"] = [ref for ref, _ in targets]
    info["controller_source"] = source

    # ⚠ ONE working placement threaded through every target, so a later
    # controller sees the moves an earlier one made. Otherwise two annuli
    # sharing a neighbour would each plan a move for it and the second would
    # silently overwrite the first.
    working = {str(p.ref): p for p in placed}
    phys = {ref: _placed_bounds(p, fp_bboxes, fp_geometries, physical=True)
            for ref, p in working.items()}

    def _gaps(state, courtyard, skip):
        out = []
        for ref, part in state.items():
            if ref == skip or _geom(part) is None:
                continue
            out.append((ref, rect_gap(courtyard, part_rect(part, _geom(part)))))
        return sorted(out, key=lambda t: (t[1], t[0]))

    for ref, declared_lane in targets:
        # Each declared part gets its OWN lane -- a 0.5 mm-pitch controller and
        # a coarse power module do not need the same room, and the board said so.
        lane = float(declared_lane) if declared_lane is not None else lane_default
        controller = working[ref]
        courtyard = part_rect(controller, _geom(controller))
        all_before = _gaps(working, courtyard, ref)
        tight = [(r, gap) for r, gap in all_before if gap < lane]
        entry = {
            "lane_mm": round(lane, 6),
            "tight_before": [r for r, _ in tight],
            # ⚠ The NEAREST gap, not the tightest *violating* one: with every
            # violation relieved the tight list is empty, and ``None`` there
            # would read as "not measured" rather than "nothing left in the lane".
            "gap_before_mm": round(all_before[0][1], 4) if all_before else None,
            "moved": {}, "blocked": [], "reverted": False, "reverted_moves": {},
            "tight_after": [], "gap_after_mm": None,
        }
        info["per_controller"][ref] = entry
        if not tight:
            entry["gap_after_mm"] = entry["gap_before_mm"]
            continue

        undo = {}
        for neighbour, _gap in tight:
            part = working[neighbour]
            geometry = _geom(part)
            if geometry is None:
                entry["blocked"].append(neighbour)
                continue
            chosen = None
            for magnitude, axis, dx, dy in _push_options(
                    part_rect(part, geometry), courtyard, lane):
                if magnitude <= 1e-9:
                    continue
                moved = _replace(part, x_mm=part.x_mm + dx, y_mm=part.y_mm + dy)
                bounds = _placed_bounds(moved, fp_bboxes, fp_geometries,
                                        physical=True)
                if outline is not None and not _outline_contains_bounds(
                        bounds, outline):
                    continue
                # ⛔ Every other part, INCLUDING the other declared controllers:
                # buying one IC its room by parking a passive on another IC is
                # not a win, and the validator would call it an overlap anyway.
                clash = any(
                    _rects_overlap(bounds, phys[other], clearance_mm)
                    for other, other_part in working.items()
                    if other != neighbour and _same_physical_side(moved,
                                                                  other_part)
                )
                if clash:
                    continue
                chosen = (axis, dx, dy, moved, bounds)
                break
            if chosen is None:
                entry["blocked"].append(neighbour)
                continue
            axis, dx, dy, moved, bounds = chosen
            undo[neighbour] = (working[neighbour], phys[neighbour])
            working[neighbour] = moved
            phys[neighbour] = bounds
            entry["moved"][neighbour] = {"axis": axis, "dx_mm": round(dx, 4),
                                         "dy_mm": round(dy, 4)}

        # ⭐⭐ All-or-nothing, PER CONTROLLER. A neighbour with nowhere legal to
        # go leaves this annulus breached, so this controller still cannot put a
        # via down and every move made for it is pure cost -- measured as a
        # broken GND, a lost pour zone and a necked track on ``lt8710_inverting``.
        # ⛔ Scoped to the one controller on purpose: a crowded IC must not veto
        # the room another IC on the same board could have had.
        if entry["blocked"] and not partial:
            entry["reverted"] = True
            entry["reverted_moves"] = dict(entry["moved"])
            entry["moved"] = {}
            for neighbour, (old_part, old_bounds) in undo.items():
                working[neighbour] = old_part
                phys[neighbour] = old_bounds

        after = _gaps(working, courtyard, ref)
        entry["tight_after"] = [r for r, gap in after if gap < lane]
        entry["gap_after_mm"] = round(after[0][1], 4) if after else None

    # Roll the per-controller detail up. For a single target these are exactly
    # that target's numbers, so a one-IC board reads as it always did.
    entries = list(info["per_controller"].values())
    for entry in entries:
        info["moved"].update(entry["moved"])
        info["blocked"].extend(entry["blocked"])
        info["reverted_moves"].update(entry["reverted_moves"])
    info["reverted"] = any(e["reverted"] for e in entries)
    info["tight_before"] = sorted({r for e in entries for r in e["tight_before"]})
    info["tight_after"] = sorted({r for e in entries for r in e["tight_after"]})
    befores = [e["gap_before_mm"] for e in entries
               if e["gap_before_mm"] is not None]
    afters = [e["gap_after_mm"] for e in entries if e["gap_after_mm"] is not None]
    # ⚠ The WORST escape on the board, not an average: a board is escapable only
    # if EVERY declared part is.
    info["gap_before_mm"] = min(befores) if befores else None
    info["gap_after_mm"] = min(afters) if afters else None

    if not info["moved"]:
        return placed, info
    return [working[str(p.ref)] for p in placed], info


# --------------------------------------------------------------------------- #
# The keepout writer -- escalation rung 3 (post-process the emitted board)
# --------------------------------------------------------------------------- #

def _ensure_user_layer(text: str, layer: str) -> str:
    """Declare ``layer`` in the board's ``(layers ...)`` block if it is absent.

    ⚠ The skidl-layout writer emits no user layers at all
    (``writer._LAYERS`` stops at ``B.Fab``), and KRT reads a keepout out of the
    **file text** -- so a ``gr_poly`` on an undeclared layer is a polygon KiCad
    will not load. Adding the declaration HERE, on a copy, rather than in the
    writer keeps every default-path board byte-identical, which is the arc's
    structural rule.
    """
    if re.search(r'\(\d+\s+"' + re.escape(layer) + r'"\s+user', text):
        return text
    layer_id = _USER_LAYER_IDS.get(layer)
    if layer_id is None:
        raise ValueError(
            f"{layer!r} is not one of KiCad's User.1..User.9 layers and this "
            f"writer will not invent an id for it")
    match = _LAST_WRITER_LAYER_RE.search(text)
    if match is None:
        raise ValueError(
            "could not find the end of the board's (layers ...) block; refusing "
            "to guess where a layer declaration goes")
    indent = match.group(1)
    return (text[:match.end()]
            + f'\n{indent}({layer_id} "{layer}" user)'
            + text[match.end():])


def _gr_poly_sexpr(points, layer: str, seed: str, indent: str) -> str:
    pts = "".join(f"\n{indent}\t\t\t(xy {x:.6f} {y:.6f})" for x, y in points)
    return (
        f"\n{indent}(gr_poly"
        f"\n{indent}\t(pts{pts}\n{indent}\t\t)"
        f"\n{indent}\t(stroke\n{indent}\t\t(width 0.05)\n{indent}\t\t(type solid)\n{indent}\t)"
        f"\n{indent}\t(fill no)"
        f'\n{indent}\t(layer "{layer}")'
        f'\n{indent}\t(uuid "{uuid.uuid5(_NAMESPACE_UUID, seed)}")'
        f"\n{indent})"
    )


def write_keepout_polygons(pcb_path: str, polygons, *,
                           layer: str = DEFAULT_KEEPOUT_LAYER) -> int:
    """Append one ``gr_poly`` per polygon to an existing ``.kicad_pcb``.

    This is escalation rung 3 -- post-processing the emitted board -- and it is
    the **only** way to get a keepout in front of KRT, because
    ``kicad_parser.parse_keepout_zones`` reads the polygons out of the raw file
    text rather than taking them from argv.

    ⚠ **Writes in place.** Call it on a copy, and carry the sibling
    ``.kicad_pro`` with the copy -- the project file holds the DRC floor the
    chain routed to, and stranding it manufactures phantom clearance violations.

    ⚠ The emitted s-expression is shaped to match
    ``kicad_parser._parse_gr_polys_on_layer`` exactly:
    ``(gr_poly (pts (xy X Y) ...) ... (layer "User.2"))`` with nothing between
    the ``pts`` block and the ``layer`` that opens a ``(gr_`` or ``(layers``.
    ⛔ Verified by round-tripping through KRT's own parser in
    ``tests/test_power_escape.py`` -- never by eye.

    Returns:
        How many polygons were written. A polygon with fewer than three
        vertices is skipped (KRT ignores it anyway -- it bounds no area).
    """
    polygons = [list(p) for p in (polygons or [])]
    usable = [p for p in polygons if len(p) >= 3]
    if not usable:
        return 0
    with open(pcb_path, "r", encoding="utf-8") as handle:
        text = handle.read()

    text = _ensure_user_layer(text, layer)

    end = text.rstrip()
    if not end.endswith(")"):
        raise ValueError(f"{pcb_path} does not end in a closing paren")
    trailing = text[len(end):]
    body = end[:-1].rstrip("\n")

    indent = "\t"
    chunks = [
        _gr_poly_sexpr(points, layer, f"{os.path.basename(pcb_path)}:keepout:{i}",
                       indent)
        for i, points in enumerate(usable)
    ]
    with open(pcb_path, "w", encoding="utf-8") as handle:
        handle.write(body + "".join(chunks) + "\n)" + trailing)
    return len(usable)


# --------------------------------------------------------------------------- #
# The fanout pre-pass (power-layout Phase 14, WS-14.3)
# --------------------------------------------------------------------------- #

def fanout_controller(
    pcb_path: str,
    out_path: str,
    controller_ref: str,
    *,
    krt_dir: str | None = None,
    nets=None,
    escape_method: str = "underpad",
    track_width: float | None = None,
    clearance: float | None = None,
    via_size: float | None = None,
    via_drill: float | None = None,
    board_edge_clearance: float | None = None,
    grid_step: float | None = None,
    layer: str = "F.Cu",
    timeout_s: int = 900,
) -> dict:
    """Author ``controller_ref``'s escape copper **before** the router runs.

    ⭐ **The mechanism, and why it is not rip-up.** KRT says of the failing pads
    that they are *"boxed in by static obstacles (neighboring pads + clearance),
    not by congestion"*, and Phase 12 measured a rip-up sweep gaining **+0 nets
    across 15 routes**. A fanout pre-pass does something rip-up cannot: it
    commits the stub or the via-drop *first*, so the router then routes from a
    pad that has already left the package.

    ``escape_method="underpad"`` is the default here rather than KRT's own
    ``"stub"``: it drops a staggered through-via just past each pad instead of
    fanning laterally into the neighbours, and lateral room is exactly what
    these boards do not have.

    ⛔ **Shells out to ``qfn_fanout.py`` rather than importing the package**,
    for the same reason :mod:`skidl_layout.krt` shells out for everything else:
    it keeps the KRT dependency at the process boundary, it is what KRT's
    ``redo_commands.sh`` manifests record, and it does not couple this module to
    KRT's internal import layout across re-syncs. (The Phase-14 *probe*
    imports the package directly, but it writes no board.)

    ⛔ ``--allow-via-in-pad`` is **never passed.** The shipped ``oshpark-2l``
    FabSpec declares ``via_in_pad=False`` and :func:`skidl_layout.fab_check`
    grades that rule, so a board that needed it would fail its own fab spec --
    the same wall the Phase-5 thermal-via array hit.

    ⚠ ``qfn_fanout`` clamps ``track_width`` **up** to the fab floor for the
    layer count, so a thinner request silently gets the floor. The returned
    record reports what was asked for; the board carries what was emitted.

    Args:
        nets: net patterns to fan out. **Scope this to the housekeeping nets.**
            Stubs on a poured net are wasted copper and can fence the pour into
            islands -- the measured Phase-4 reason ``route_promoted`` defaults
            ``True``. ``None`` fans out every net of the component.

    Returns:
        ``{"ran", "controller", "escape_method", "out_path", "argv",
        "vias_placed", "vias_dropped", "tracks", "failed_nets", "reason"}``.
        ``ran=False`` with a ``reason`` when KRT could not analyse the part or
        the CLI failed -- ⛔ never an exception, because a fanout that declines
        is an outcome the caller must be able to route past.
    """
    from . import krt as _krt

    resolved = _krt.find_krt(krt_dir)
    if resolved is None:
        return {"ran": False, "controller": controller_ref,
                "reason": "KiCadRoutingTools not found"}

    args = ["qfn_fanout.py", os.path.abspath(pcb_path),
            "--output", os.path.abspath(out_path),
            "--component", str(controller_ref),
            "--escape-method", str(escape_method),
            "--layer", str(layer)]
    if nets:
        args.append("--nets")
        args.extend(str(n) for n in nets)
    for flag, value in (("--width", track_width), ("--clearance", clearance),
                        ("--via-size", via_size), ("--via-drill", via_drill),
                        ("--board-edge-clearance", board_edge_clearance),
                        ("--grid-step", grid_step)):
        if value is not None:
            args += [flag, f"{value:g}"]

    record = {"ran": False, "controller": str(controller_ref),
              "escape_method": str(escape_method), "out_path": out_path,
              "argv": list(args)}
    try:
        proc = _krt._run_krt(args, resolved, timeout_s)
    except Exception as exc:                            # noqa: BLE001
        record["reason"] = f"{type(exc).__name__}: {exc}"
        return record

    text = (proc.stdout or "") + (proc.stderr or "")
    record.update(_parse_fanout_output(text))
    if not os.path.isfile(out_path):
        record["reason"] = (
            f"qfn_fanout.py wrote no output (exit {proc.returncode}): "
            + "\n".join(text.splitlines()[-5:]))
        return record
    # ⚠ The project file carries the DRC floor the chain routed to. A fanned
    # board that loses it grades correct sub-floor copper as phantom clearance
    # violations at the very next step.
    _copy_sibling_project(pcb_path, out_path)
    record["ran"] = True
    return record


#: ``Underpad via-drop: 10 vias placed, 0 dropped (pitch 0.50, ...)``
_FANOUT_VIA_RE = re.compile(
    r"Underpad via-drop:\s*(\d+)\s*vias placed,\s*(\d+)\s*dropped")
#: ``Generated 18 track segments (10 stubs x 2 segments)``
_FANOUT_TRACK_RE = re.compile(r"Generated\s+(\d+)\s+track segments")
#: ``dropped (no clear via offset): ['SS', 'FLAG']``
_FANOUT_FAILED_RE = re.compile(r"dropped \(no clear via offset\):\s*\[(.*?)\]")


def _parse_fanout_output(text: str) -> dict:
    """The counts KRT prints, parsed at the source.

    ⛔ Parsed HERE rather than left in a log for a later gate to grep: the
    workdir is disposable and a row that carries its own evidence can be
    re-graded without re-running the pre-pass. Same reason Phase 13 parsed the
    keepout line at route time.
    """
    out: dict = {"vias_placed": None, "vias_dropped": None, "tracks": None,
                 "failed_nets": []}
    via = _FANOUT_VIA_RE.search(text or "")
    if via:
        out["vias_placed"] = int(via.group(1))
        out["vias_dropped"] = int(via.group(2))
    track = _FANOUT_TRACK_RE.search(text or "")
    if track:
        out["tracks"] = int(track.group(1))
    failed = _FANOUT_FAILED_RE.search(text or "")
    if failed:
        out["failed_nets"] = sorted(
            n.strip().strip("'\"") for n in failed.group(1).split(",")
            if n.strip())
    if "doesn't appear to be a QFN/QFP" in (text or ""):
        out["analysed"] = False
    return out


def _copy_sibling_project(src_pcb: str, dst_pcb: str) -> None:
    """Carry a board's sibling ``.kicad_pro`` across a copy (see power_copper)."""
    import shutil

    sibling = os.path.splitext(src_pcb)[0] + ".kicad_pro"
    if os.path.isfile(sibling) and os.path.abspath(src_pcb) != os.path.abspath(dst_pcb):
        try:
            shutil.copyfile(sibling, os.path.splitext(dst_pcb)[0] + ".kicad_pro")
        except OSError:  # pragma: no cover - best effort
            pass
