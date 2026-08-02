# -*- coding: utf-8 -*-
"""Layout cells -- pre-placed, rotatable, nestable rigid bodies.

A **cell** (the plan calls it a "layout template" in prose; ``LayoutCell`` in
code, because ``TEMPLATE`` is already skidl's part destination and
``panel_template`` is already a skidl-layout intent) is a few parts at frozen
relative offsets inside a rectangle, optionally carrying internal copper, and
carrying **two maps**:

* the **access map** (:class:`CellPort`) -- the cell's *own* nets getting *out*:
  per net, per side, per layer, ``FAVORED`` / ``ACCESSIBLE`` / ``BLOCKED``;
* the **transit map** (:class:`CellTransit`) -- *foreign* nets passing
  *through*: per layer, per axis, the lanes a stranger's trace can cross on,
  plus a **total passable width** and a **max single trace width**.

⚠⚠ **Their defaults are opposite, and that is the trap this docstring exists to
nail down.** An **unspecified port is unusable** -- the cell makes no claim, so
a planner must not route there. An **unspecified layer is fully passable** -- a
layer the cell puts nothing on genuinely does not obstruct, so its transit is
the whole box on both axes (:meth:`LayoutCell.passable_width` applies that
default rather than leaving it to be remembered).

⛔⛔ **The blank-layer default has one exception that would silently produce
unroutable boards:** through-board pads and vias obstruct **every** layer,
including layers the cell never defined. :func:`sweep_transit` projects them
onto the whole stackup rather than onto ``layers_defined``.

Coordinates
-----------
A cell's local frame has its origin at the box's ``(min_x, min_y)`` corner and
spans ``[0, width] x [0, height]``. **The frame is KiCad's**: ``+y`` runs *down*
the board, and rotation follows :func:`~skidl_layout.geometry.transform_point`'s
negated-angle convention.

⚠⚠ **Consequence, and it contradicts the plan's prose.** The plan states 90
degrees maps ``N -> E -> S -> W -> N``. That holds in a ``y``-up frame; in
KiCad's ``y``-down frame the same rotation maps ``N -> W -> S -> E -> N``.
⛔ **Nothing here hardcodes either cycle.** A side is carried by its outward
normal and the normal goes through the same linear map as every other vector
(:func:`_rotate_side`), so the cycle is *derived* from the transform and cannot
drift away from where the geometry actually goes. This is the failure mode
``power_escape.part_rect``'s docstring records, avoided by construction.

Self-containment
----------------
⭐ A cell stores its members' **pad rectangles** (:class:`CellPad`) in local
coordinates. That is derived data -- it could be recomputed from the footprint
library every time -- and storing it is deliberate: a compiled cell in a
content-addressed cache must be placeable, sweepable and scoreable **without the
footprint library being present or unchanged**. A footprint that is silently
revised upstream would otherwise change a cell's geometry without changing its
digest.

Determinism
-----------
⛔ Every coordinate is quantised to :data:`QUANTUM_DP` decimal places (1 nm,
KiCad's own resolution) on construction and again after every transform, so
rotating a cell four times returns it **byte-identical** rather than merely
close: ``w - (w - x)`` is not ``x`` in binary floating point, and a library
keyed by a content hash cannot tolerate that.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Literal, Mapping, Sequence

from .geometry import FootprintGeometry, load_footprint_geometries
from .writer import PlacedPart

__all__ = [
    "QUANTUM_DP",
    "Access",
    "Axis",
    "CellCache",
    "CellCopper",
    "CellMember",
    "CellPad",
    "CellPort",
    "CellSegment",
    "CellTransit",
    "CellVia",
    "LayoutCell",
    "NestedCell",
    "Side",
    "TransitLane",
    "cell_digest",
    "cell_geometry",
    "cell_pad_geometries",
    "compose_cells",
    "deserialise_cell",
    "harvest_cell",
    "inherit_maps_naively",
    "member_placed_parts",
    "net_escape_points",
    "resolve_hpwl_points",
    "rotate_cell",
    "serialise_cell",
    "sweep_transit",
    "synthesise_cell",
]

Side = Literal["N", "E", "S", "W"]
Access = Literal["FAVORED", "ACCESSIBLE", "BLOCKED"]
Axis = Literal["EW", "NS"]

#: Decimal places every stored coordinate is rounded to. 1 nm -- the resolution
#: KiCad itself stores board geometry at, and the same ``%.6f`` the canonical
#: placement digest uses (``canaries/sweep_clearance.py:68-77``).
QUANTUM_DP = 6

#: The four sides' outward normals in the cell's own (KiCad, ``y``-down) frame.
#: ⭐ Rotation is applied to *these*, never to the letters.
_SIDE_NORMALS: dict[str, tuple[int, int]] = {
    "N": (0, -1),
    "S": (0, 1),
    "W": (-1, 0),
    "E": (1, 0),
}
_NORMAL_SIDES = {normal: side for side, normal in _SIDE_NORMALS.items()}

_VALID_ROTATIONS = (0, 90, 180, 270)

_ACCESS_RANK = {"FAVORED": 0, "ACCESSIBLE": 1, "BLOCKED": 2}


def _q(value) -> float:
    """Quantise to the storage grid, and normalise ``-0.0`` to ``0.0``.

    ⛔ The ``+ 0.0`` is not decoration: ``round(-1e-9, 6)`` is ``-0.0``, which
    serialises as ``-0.0`` and would give two identical cells two digests.
    """
    return round(float(value), QUANTUM_DP) + 0.0


def _qi(value) -> int:
    return int(round(float(value)))


def _norm_rot(deg) -> int:
    """A rotation in ``{0, 90, 180, 270}``, or raise."""
    rot = _qi(deg) % 360
    if rot not in _VALID_ROTATIONS:
        raise ValueError(
            f"cell rotations are restricted to {_VALID_ROTATIONS}, got {deg!r}"
        )
    return rot


# --------------------------------------------------------------------------- #
# The artifact
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CellMember:
    """One part (or nested cell) at a frozen offset inside the box."""

    local_ref: str
    kind: Literal["part", "cell"] = "part"
    footprint: str = ""            # or a cell digest when ``kind == "cell"``
    dx: float = 0.0                # offset from the cell origin (box min corner)
    dy: float = 0.0
    rotation: int = 0
    side: Literal["top"] = "top"   # ⛔ v1: top only, no mirroring
    #: The member's own physical (body ∪ pads) envelope, **already rotated**.
    #: ⭐ Stored rather than recomputed so "how much room would these parts take
    #: WITHOUT the cell" is answerable from the cell alone -- the acceptance
    #: criterion "box area <= the naive arrangement" needs the same envelope the
    #: box was built from, and comparing a body-inclusive box against a
    #: pad-only naive area is not a measurement, it is a units error.
    w: float = 0.0
    h: float = 0.0

    def normalised(self) -> "CellMember":
        return replace(self, dx=_q(self.dx), dy=_q(self.dy),
                       rotation=_norm_rot(self.rotation),
                       w=_q(self.w), h=_q(self.h))


@dataclass(frozen=True)
class CellPad:
    """One member pad's axis-aligned rectangle, in the cell's local frame.

    ⚠ ``w``/``h`` are the **already-rotated** AABB extents, so a member placed
    at 90 degrees reports the transposed box. Keeping the rect axis-aligned is
    what makes the transit sweep a 1-D interval problem.
    """

    local_ref: str
    pad: str
    local_net: str
    x: float
    y: float
    w: float
    h: float
    through_board: bool = False

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x - self.w / 2.0, self.y - self.h / 2.0,
                self.x + self.w / 2.0, self.y + self.h / 2.0)

    @property
    def label(self) -> str:
        return f"{self.local_ref}.{self.pad}"

    def normalised(self) -> "CellPad":
        return replace(self, x=_q(self.x), y=_q(self.y), w=_q(self.w),
                       h=_q(self.h))


@dataclass(frozen=True)
class CellPort:
    """One escaping net, on one side, on one layer.

    ``access`` is a *claim*: ``BLOCKED`` means the compiler probed and the probe
    failed. An **absent** ``(net, side, layer)`` triple is undefined -- the cell
    says nothing, and a planner must treat it as unusable.
    """

    local_net: str
    side: Side
    layer: int
    access: Access
    x: float = 0.0                 # exit point on the box edge, local coords
    y: float = 0.0
    cost: float = 0.0              # probe length + via penalty; ranks FAVORED

    def normalised(self) -> "CellPort":
        return replace(self, x=_q(self.x), y=_q(self.y), cost=_q(self.cost),
                       layer=int(self.layer))


@dataclass(frozen=True)
class TransitLane:
    """One clear channel across the box, on one layer, along one axis."""

    layer: int
    axis: Axis
    lo: float                      # the lane's span on the TRANSVERSE axis
    hi: float
    clear_width: float = 0.0       # hi - lo, already net of clearance both sides

    def normalised(self) -> "TransitLane":
        lo, hi = _q(self.lo), _q(self.hi)
        return replace(self, layer=int(self.layer), lo=lo, hi=hi,
                       clear_width=_q(hi - lo))


@dataclass(frozen=True)
class CellTransit:
    """The per-(layer, axis) summary a planner scores on.

    ⭐ ``total_width`` and ``max_trace`` are **not** interchangeable and that is
    the whole reason both exist: a fine-pitch part with many small inter-pad
    gaps has a large ``total_width`` and a ``max_trace`` too small for a power
    trace, while one wide channel under a coil has a modest ``total_width`` and
    a ``max_trace`` that will take anything.

    ⚠ Both are **already net of clearance on both sides** -- the obstructions
    were inflated by ``clearance`` before the complement was taken, so a
    consumer that subtracts clearance again is double-counting. ``clearance``
    travels with them because the numbers are meaningless without it, and
    ``source`` follows ``power_escape.resolve_lane_mm``'s convention that every
    number carries where it came from.
    """

    layer: int
    axis: Axis
    lanes: tuple[TransitLane, ...] = ()
    total_width: float = 0.0
    max_trace: float = 0.0
    clearance: float = 0.0
    source: str = "geometric"      # "geometric" | "probed" | "declared"

    def normalised(self) -> "CellTransit":
        lanes = tuple(lane.normalised() for lane in self.lanes)
        return replace(
            self,
            layer=int(self.layer),
            lanes=lanes,
            total_width=_q(sum(lane.clear_width for lane in lanes)),
            max_trace=_q(max((lane.clear_width for lane in lanes), default=0.0)),
            clearance=_q(self.clearance),
        )


@dataclass(frozen=True)
class CellSegment:
    """One track segment of the cell's internal copper, in local coordinates."""

    x1: float
    y1: float
    x2: float
    y2: float
    layer: int
    width: float
    local_net: str = ""

    def normalised(self) -> "CellSegment":
        return replace(self, x1=_q(self.x1), y1=_q(self.y1), x2=_q(self.x2),
                       y2=_q(self.y2), layer=int(self.layer), width=_q(self.width))


@dataclass(frozen=True)
class CellVia:
    """One via of the cell's internal copper, in local coordinates.

    ⛔ A via obstructs **every** copper layer, not only the two it nominally
    connects -- :func:`sweep_transit` projects it onto the whole stackup.
    """

    x: float
    y: float
    size: float
    drill: float
    local_net: str = ""

    def normalised(self) -> "CellVia":
        return replace(self, x=_q(self.x), y=_q(self.y), size=_q(self.size),
                       drill=_q(self.drill))


@dataclass(frozen=True)
class CellCopper:
    segments: tuple[CellSegment, ...] = ()
    vias: tuple[CellVia, ...] = ()

    def normalised(self) -> "CellCopper":
        return CellCopper(
            segments=tuple(sorted(
                (s.normalised() for s in self.segments),
                key=lambda s: (s.layer, s.local_net, s.x1, s.y1, s.x2, s.y2))),
            vias=tuple(sorted(
                (v.normalised() for v in self.vias),
                key=lambda v: (v.local_net, v.x, v.y))),
        )


@dataclass(frozen=True)
class LayoutCell:
    """The compiled, content-addressed artifact.

    ⭐ :func:`harvest_cell` returns one of these with ``ports`` / ``transit`` /
    ``copper`` empty -- an *arrangement*. The compiler fills them in. One class
    rather than two so there is exactly one rotation transform, one serialiser
    and one digest to be wrong about.
    """

    name: str
    width: float = 0.0
    height: float = 0.0
    members: tuple[CellMember, ...] = ()
    pads: tuple[CellPad, ...] = ()
    #: local net -> ("R1.1", "C1.2", ...) -- the member pins it lands on
    nets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: fully contained; these never escape and must never be exposed as pins
    internal_nets: frozenset[str] = field(default_factory=frozenset)
    ports: tuple[CellPort, ...] = ()
    transit: tuple[CellTransit, ...] = ()
    #: local net -> (x, y): the HPWL representative point (plan section 3.6)
    hpwl_points: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    copper: CellCopper | None = None
    #: ⚠ sparse -- a cell need not define every layer of the board it lands on
    layers_defined: tuple[int, ...] = ()
    fab: str = ""
    stackup: int = 2
    rotation: int = 0              # how far this instance is turned from source
    #: free-form, never hashed: provenance, acceptance verdicts, probe counts
    meta: Mapping[str, object] = field(default_factory=dict)

    # -- derived ---------------------------------------------------------- #
    @property
    def escaping_nets(self) -> tuple[str, ...]:
        return tuple(sorted(n for n in self.nets if n not in self.internal_nets))

    @property
    def part_members(self) -> tuple["CellMember", ...]:
        """Members that are real parts. ⛔ **Use this, not ``members``, anywhere
        the answer is "which footprints does this cell place".**

        A nested cell (WS-U4) carries one extra ``kind == "cell"`` member per
        child, recording the child's digest, offset and rotation. That entry is
        **provenance, not geometry**: the child's own parts are flattened into
        ``members`` alongside it, so anything that iterates ``members`` to place
        footprints, to count area or to list refs would count every nested part
        twice and then try to place a footprint whose name is a 16-hex digest.
        ⚠ Until nesting existed every member was a part, so filtering here is
        byte-identical on every cell harvested before it.
        """
        return tuple(m for m in self.members if m.kind == "part")

    @property
    def nested_members(self) -> tuple["CellMember", ...]:
        """The ``kind == "cell"`` provenance entries, if this cell is composed."""
        return tuple(m for m in self.members if m.kind == "cell")

    @property
    def member_refs(self) -> tuple[str, ...]:
        return tuple(sorted(m.local_ref for m in self.part_members))

    @property
    def compiled(self) -> bool:
        return bool(self.ports)

    @property
    def digest(self) -> str:
        return cell_digest(self)

    @property
    def area_mm2(self) -> float:
        return _q(self.width * self.height)

    def normalised(self) -> "LayoutCell":
        """Quantise, sort and canonicalise. **Every constructor path ends here.**"""
        return LayoutCell(
            name=str(self.name),
            width=_q(self.width),
            height=_q(self.height),
            members=tuple(sorted((m.normalised() for m in self.members),
                                 key=lambda m: m.local_ref)),
            pads=tuple(sorted((p.normalised() for p in self.pads),
                              key=lambda p: (p.local_ref, p.pad))),
            nets={str(net): tuple(sorted(pins))
                  for net, pins in sorted(dict(self.nets).items())},
            internal_nets=frozenset(str(n) for n in self.internal_nets),
            ports=tuple(sorted((p.normalised() for p in self.ports),
                               key=lambda p: (p.local_net, p.layer, p.side))),
            transit=tuple(sorted((t.normalised() for t in self.transit),
                                 key=lambda t: (t.layer, t.axis))),
            hpwl_points={str(net): (_q(x), _q(y))
                         for net, (x, y) in sorted(dict(self.hpwl_points).items())},
            copper=self.copper.normalised() if self.copper is not None else None,
            layers_defined=tuple(sorted({int(v) for v in self.layers_defined})),
            fab=str(self.fab),
            stackup=int(self.stackup),
            rotation=_norm_rot(self.rotation),
            meta=dict(self.meta),
        )

    def port(self, local_net: str, side: str, layer: int) -> CellPort | None:
        for candidate in self.ports:
            if (candidate.local_net == local_net and candidate.side == side
                    and candidate.layer == int(layer)):
                return candidate
        return None

    def escapable_sides(self, local_net: str) -> tuple[str, ...]:
        """Sides ``local_net`` has a non-``BLOCKED`` port on, on any layer.

        ⛔ Undefined is unusable: a side with no port entry is **not** returned.
        """
        return tuple(sorted({p.side for p in self.ports
                             if p.local_net == local_net
                             and p.access != "BLOCKED"}))

    def transit_for(self, layer: int, axis: str) -> CellTransit | None:
        """The transit entry for ``(layer, axis)``.

        ⚠ ``None`` means **undefined**, which for transit means *fully
        passable* -- the opposite of what ``None`` means for a port. Prefer
        :meth:`passable_width`, which applies the default instead of leaving it
        to be remembered.
        """
        for entry in self.transit:
            if entry.layer == int(layer) and entry.axis == axis:
                return entry
        return None

    def passable_width(self, layer: int, axis: str) -> tuple[float, float]:
        """``(total_width, max_trace)`` with the blank-layer default applied."""
        entry = self.transit_for(layer, axis)
        if entry is not None:
            return entry.total_width, entry.max_trace
        span = self.height if axis == "EW" else self.width
        return _q(span), _q(span)


# --------------------------------------------------------------------------- #
# Rotation -- derived from the transform, never from a hardcoded cycle
# --------------------------------------------------------------------------- #
def _rotate_vector(dx: float, dy: float, deg: int) -> tuple[float, float]:
    """The linear half of :func:`~skidl_layout.geometry.transform_point`."""
    radians = math.radians(deg)
    cos_r, sin_r = math.cos(radians), math.sin(radians)
    return (dx * cos_r + dy * sin_r, -dx * sin_r + dy * cos_r)


def _rotate_side(side: str, deg: int) -> str:
    """Where a side's outward normal points after the rotation.

    ⭐ This is why the module carries no ``N->E->S->W`` table: the answer is a
    property of ``geometry.transform_point``, so it is *computed* from it.
    """
    normal = _SIDE_NORMALS[side]
    dx, dy = _rotate_vector(float(normal[0]), float(normal[1]), deg)
    key = (_qi(dx), _qi(dy))
    if key not in _NORMAL_SIDES:                       # pragma: no cover
        raise ValueError(f"rotation {deg} took side {side} off-axis: {key}")
    return _NORMAL_SIDES[key]


def _rotate_point(x: float, y: float, deg: int,
                  width: float, height: float) -> tuple[float, float]:
    """A local point through ``deg``, re-based onto the rotated box's origin.

    The linear map alone sends ``[0, w] x [0, h]`` off the origin; the
    translation puts it back so the rotated cell still spans
    ``[0, w'] x [0, h']``.
    """
    rx, ry = _rotate_vector(x, y, deg)
    deg = deg % 360
    if deg == 90:
        ry += width
    elif deg == 180:
        rx += width
        ry += height
    elif deg == 270:
        rx += height
    return rx, ry


def _rotate_span(lo: float, hi: float, axis: str, deg: int,
                 width: float, height: float) -> tuple[float, float]:
    """A lane's transverse span through the rotation.

    An ``EW`` lane's span lives on ``y``, an ``NS`` lane's on ``x``. Rotating
    the transverse segment's two endpoints and keeping the surviving coordinate
    avoids a second convention to get wrong.
    """
    if axis == "EW":
        p1 = _rotate_point(0.0, lo, deg, width, height)
        p2 = _rotate_point(0.0, hi, deg, width, height)
    else:
        p1 = _rotate_point(lo, 0.0, deg, width, height)
        p2 = _rotate_point(hi, 0.0, deg, width, height)
    new_axis = "NS" if axis == "EW" else "EW"
    index = 0 if new_axis == "NS" else 1
    values = sorted((p1[index], p2[index]))
    return values[0], values[1]


def rotate_cell(cell: LayoutCell, deg: int) -> LayoutCell:
    """``cell`` turned by ``deg`` in ``{0, 90, 180, 270}``.

    Members, pads, ports, lanes, copper and HPWL points all move; layers do not
    (no flip in v1). ⭐ Applying ``rotate_cell(c, 90)`` four times returns a cell
    **byte-identical** to ``c`` -- gate ``T1``.
    """
    deg = _norm_rot(deg)
    cell = cell.normalised()
    if deg == 0:
        return cell

    width, height = cell.width, cell.height
    swapped = deg in (90, 270)
    new_width, new_height = (height, width) if swapped else (width, height)

    members = []
    for member in cell.members:
        mx, my = _rotate_point(member.dx, member.dy, deg, width, height)
        mw, mh = (member.h, member.w) if swapped else (member.w, member.h)
        members.append(replace(member, dx=mx, dy=my, w=mw, h=mh,
                               rotation=(member.rotation + deg) % 360))

    pads = []
    for pad in cell.pads:
        px, py = _rotate_point(pad.x, pad.y, deg, width, height)
        pw, ph = (pad.h, pad.w) if swapped else (pad.w, pad.h)
        pads.append(replace(pad, x=px, y=py, w=pw, h=ph))

    ports = []
    for port in cell.ports:
        px, py = _rotate_point(port.x, port.y, deg, width, height)
        ports.append(replace(port, x=px, y=py, side=_rotate_side(port.side, deg)))

    transit = []
    for entry in cell.transit:
        new_axis = ("NS" if entry.axis == "EW" else "EW") if swapped else entry.axis
        lanes = []
        for lane in entry.lanes:
            if swapped:
                lo, hi = _rotate_span(lane.lo, lane.hi, entry.axis, deg,
                                      width, height)
            else:
                # 180 degrees keeps the axis and mirrors the transverse span.
                span = height if entry.axis == "EW" else width
                lo, hi = ((span - lane.hi, span - lane.lo) if deg == 180
                          else (lane.lo, lane.hi))
            lanes.append(replace(lane, axis=new_axis, lo=lo, hi=hi,
                                 clear_width=hi - lo))
        transit.append(replace(entry, axis=new_axis, lanes=tuple(lanes)))

    copper = None
    if cell.copper is not None:
        segments = []
        for segment in cell.copper.segments:
            x1, y1 = _rotate_point(segment.x1, segment.y1, deg, width, height)
            x2, y2 = _rotate_point(segment.x2, segment.y2, deg, width, height)
            segments.append(replace(segment, x1=x1, y1=y1, x2=x2, y2=y2))
        vias = []
        for via in cell.copper.vias:
            vx, vy = _rotate_point(via.x, via.y, deg, width, height)
            vias.append(replace(via, x=vx, y=vy))
        copper = CellCopper(segments=tuple(segments), vias=tuple(vias))

    hpwl_points = {net: _rotate_point(x, y, deg, width, height)
                   for net, (x, y) in cell.hpwl_points.items()}

    return replace(
        cell,
        width=new_width,
        height=new_height,
        members=tuple(members),
        pads=tuple(pads),
        ports=tuple(ports),
        transit=tuple(transit),
        hpwl_points=hpwl_points,
        copper=copper,
        rotation=(cell.rotation + deg) % 360,
    ).normalised()


# --------------------------------------------------------------------------- #
# Serialisation -- canonical, so the digest is a content hash and not a run id
# --------------------------------------------------------------------------- #
def _cell_payload(cell: LayoutCell, *, include_meta: bool) -> dict:
    cell = cell.normalised()
    payload = {
        "name": cell.name,
        "width": cell.width,
        "height": cell.height,
        "members": [
            {"local_ref": m.local_ref, "kind": m.kind, "footprint": m.footprint,
             "dx": m.dx, "dy": m.dy, "rotation": m.rotation, "side": m.side,
             "w": m.w, "h": m.h}
            for m in cell.members
        ],
        "pads": [
            {"local_ref": p.local_ref, "pad": p.pad, "local_net": p.local_net,
             "x": p.x, "y": p.y, "w": p.w, "h": p.h,
             "through_board": bool(p.through_board)}
            for p in cell.pads
        ],
        "nets": {net: list(pins) for net, pins in cell.nets.items()},
        "internal_nets": sorted(cell.internal_nets),
        "ports": [
            {"local_net": p.local_net, "side": p.side, "layer": p.layer,
             "access": p.access, "x": p.x, "y": p.y, "cost": p.cost}
            for p in cell.ports
        ],
        "transit": [
            {"layer": t.layer, "axis": t.axis, "total_width": t.total_width,
             "max_trace": t.max_trace, "clearance": t.clearance,
             "source": t.source,
             "lanes": [{"layer": lane.layer, "axis": lane.axis, "lo": lane.lo,
                        "hi": lane.hi, "clear_width": lane.clear_width}
                       for lane in t.lanes]}
            for t in cell.transit
        ],
        "hpwl_points": {net: [x, y] for net, (x, y) in cell.hpwl_points.items()},
        "copper": None if cell.copper is None else {
            "segments": [
                {"x1": s.x1, "y1": s.y1, "x2": s.x2, "y2": s.y2,
                 "layer": s.layer, "width": s.width, "local_net": s.local_net}
                for s in cell.copper.segments
            ],
            "vias": [
                {"x": v.x, "y": v.y, "size": v.size, "drill": v.drill,
                 "local_net": v.local_net}
                for v in cell.copper.vias
            ],
        },
        "layers_defined": list(cell.layers_defined),
        "fab": cell.fab,
        "stackup": cell.stackup,
        "rotation": cell.rotation,
    }
    if include_meta:
        payload["meta"] = json.loads(
            json.dumps(dict(cell.meta), default=str, sort_keys=True))
    return payload


def serialise_cell(cell: LayoutCell, *, include_meta: bool = True) -> str:
    """Canonical JSON text. ⛔ ``sort_keys`` + fixed separators, or no digest."""
    return json.dumps(_cell_payload(cell, include_meta=include_meta),
                      sort_keys=True, separators=(",", ":"))


def deserialise_cell(text: str) -> LayoutCell:
    """Inverse of :func:`serialise_cell`, up to ``meta``'s JSON round-trip."""
    data = json.loads(text)
    copper = None
    if data.get("copper") is not None:
        copper = CellCopper(
            segments=tuple(CellSegment(**s) for s in data["copper"]["segments"]),
            vias=tuple(CellVia(**v) for v in data["copper"]["vias"]),
        )
    return LayoutCell(
        name=data["name"],
        width=data["width"],
        height=data["height"],
        members=tuple(CellMember(**m) for m in data["members"]),
        pads=tuple(CellPad(**p) for p in data.get("pads", [])),
        nets={net: tuple(pins) for net, pins in data["nets"].items()},
        internal_nets=frozenset(data["internal_nets"]),
        ports=tuple(CellPort(**p) for p in data["ports"]),
        transit=tuple(
            CellTransit(
                layer=t["layer"], axis=t["axis"],
                lanes=tuple(TransitLane(**lane) for lane in t["lanes"]),
                total_width=t["total_width"], max_trace=t["max_trace"],
                clearance=t["clearance"], source=t["source"])
            for t in data["transit"]
        ),
        hpwl_points={net: (xy[0], xy[1])
                     for net, xy in data["hpwl_points"].items()},
        copper=copper,
        layers_defined=tuple(data["layers_defined"]),
        fab=data.get("fab", ""),
        stackup=data.get("stackup", 2),
        rotation=data.get("rotation", 0),
        meta=data.get("meta", {}),
    ).normalised()


def cell_digest(cell: LayoutCell) -> str:
    """Content hash over everything that changes the cell's *geometry*.

    ⛔ ``meta`` is excluded on purpose -- provenance, timings and acceptance
    notes must not change a cell's identity, or the content-addressed cache
    misses on every recompile. ⭐ 16 hex chars, matching the canonical placement
    digest (``canaries/sweep_clearance.py:68-77``) so the two read alike.
    """
    blob = serialise_cell(cell, include_meta=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Harvesting -- lift an arrangement off a placed board
# --------------------------------------------------------------------------- #
def _member_bounds(placed: PlacedPart,
                   geometry: FootprintGeometry | None,
                   ) -> tuple[float, float, float, float]:
    """The box a member occupies, by the validator's own rule.

    ⭐ ``transformed_physical_bounds`` and not ``transformed_bounds``: the
    validator's overlap test uses the physical (body + pads) envelope
    (``validator.py:79``), and a cell box that disagreed with it would validate
    differently as a cell than the same parts do loose.
    """
    if geometry is None:
        return (placed.x_mm - 1.0, placed.y_mm - 1.0,
                placed.x_mm + 1.0, placed.y_mm + 1.0)
    return geometry.transformed_physical_bounds(placed)


def _local_pads(placed: PlacedPart, geometry: FootprintGeometry | None,
                origin_x: float, origin_y: float,
                nets_by_pad: Mapping[str, str]) -> list[CellPad]:
    """A member's pads as local-frame AABBs."""
    if geometry is None:
        return []
    out: list[CellPad] = []
    for pad in geometry.pads:
        bx0, by0, bx1, by1 = pad.transformed_bounds(placed)
        out.append(CellPad(
            local_ref=placed.ref,
            pad=str(pad.number),
            local_net=str(nets_by_pad.get(str(pad.number), "") or ""),
            x=(bx0 + bx1) / 2.0 - origin_x,
            y=(by0 + by1) / 2.0 - origin_y,
            w=bx1 - bx0,
            h=by1 - by0,
            through_board=bool(pad.is_through_board),
        ))
    return out


def resolve_footprint_name(bare: str, fp_lib_dirs: Sequence[str]) -> str:
    """``"R_0805_2012Metric"`` -> ``"Resistor_SMD:R_0805_2012Metric"``.

    ⛔⛔ **Measured, and it is not a corner case: every ``.kicad_pcb`` this stack
    writes -- and every board KiCad writes back -- carries the footprint's BARE
    name**, while ``PlacedPart.footprint`` and ``fp_geometries`` are keyed by the
    ``"Library:Name"`` form. So a harvester that reads a board and hands the name
    straight to ``load_footprint_geometries`` resolves **nothing** and silently
    falls back to a 2 x 2 mm box for every member -- a cell whose box is
    fabricated rather than measured.

    Resolution order: a name that already carries a prefix is returned unchanged;
    otherwise the ``*.pretty`` directories under ``fp_lib_dirs`` are searched in
    **sorted** order and the first match wins (⚠ sorted, because two libraries
    can define the same leaf name and an arbitrary filesystem order would make a
    cell digest host-dependent). Unresolvable names come back unchanged, so the
    caller sees the bare name rather than an exception.
    """
    if ":" in bare or not bare:
        return bare
    import os

    for base in sorted(str(d) for d in (fp_lib_dirs or [])):
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            if not entry.endswith(".pretty"):
                continue
            if os.path.isfile(os.path.join(base, entry, f"{bare}.kicad_mod")):
                return f"{entry[:-len('.pretty')]}:{bare}"
    return bare


def harvest_cell(
    pcb_path: str,
    refs: Sequence[str],
    name: str,
    *,
    fp_lib_dirs: Sequence[str] | None = None,
    footprint_map: Mapping[str, str] | None = None,
    margin_mm: float = 0.0,
    krt_dir: str | None = None,
    fab: str = "",
    stackup: int = 2,
) -> LayoutCell:
    """Lift ``refs``' arrangement off a placed board into an uncompiled cell.

    ⭐ **The first cells come from the six hand boards** -- the only known-good
    local arrangements this project owns, and they are on disk today.
    ⚠ **They are placement-only**: the hand boards carry 0 segments and 0 vias,
    so this returns *arrangement only*. Copper is the compiler's job.

    ``margin_mm`` pads the tight box on all four sides. Default ``0.0`` -- the
    box is exactly the union of the members' physical envelopes, which is the
    box the validator would have used for the same parts loose.

    ⛔ A net is **internal** only when every pin it carries *on the whole board*
    belongs to a member. A net with pins outside the set escapes, and exposing
    an internal net as a port would inject a phantom airwire into every score.

    ``footprint_map`` (``{ref: "Library:Name"}``) supplies the library prefix the
    board file does not carry -- see :func:`resolve_footprint_name`. Pass it from
    the circuit when one is in hand; otherwise the library search is used.
    """
    from .ratnest import _kicad_parser

    kp = _kicad_parser(krt_dir)
    pcb = kp.parse_kicad_pcb(pcb_path)

    wanted = list(dict.fromkeys(str(r) for r in refs))
    missing = [ref for ref in wanted if ref not in pcb.footprints]
    if missing:
        raise ValueError(f"{pcb_path} has no footprint(s) {missing}")

    dirs = list(fp_lib_dirs or [])
    placed: dict[str, PlacedPart] = {}
    footprints: dict[str, str] = {}
    pad_nets: dict[str, dict[str, str]] = {}
    for ref in wanted:
        fp = pcb.footprints[ref]
        bare = str(getattr(fp, "footprint_name", "") or "")
        footprint = str((footprint_map or {}).get(ref) or
                        resolve_footprint_name(bare, dirs))
        footprints[ref] = footprint
        placed[ref] = PlacedPart(ref=ref, x_mm=float(fp.x), y_mm=float(fp.y),
                                 rot_deg=float(fp.rotation), footprint=footprint,
                                 side="front")
        pad_nets[ref] = {str(pad.pad_number): str(getattr(pad, "net_name", "") or "")
                         for pad in fp.pads}

    geometries = load_footprint_geometries(
        {f for f in footprints.values() if f}, dirs)
    unresolved = sorted(ref for ref in wanted
                        if footprints[ref] not in geometries)

    x_min = y_min = float("inf")
    x_max = y_max = float("-inf")
    for ref in wanted:
        bx0, by0, bx1, by1 = _member_bounds(placed[ref],
                                            geometries.get(footprints[ref]))
        x_min, y_min = min(x_min, bx0), min(y_min, by0)
        x_max, y_max = max(x_max, bx1), max(y_max, by1)
    x_min -= margin_mm
    y_min -= margin_mm
    x_max += margin_mm
    y_max += margin_mm

    members = []
    for ref in wanted:
        bx0, by0, bx1, by1 = _member_bounds(placed[ref],
                                            geometries.get(footprints[ref]))
        members.append(CellMember(
            local_ref=ref, kind="part", footprint=footprints[ref],
            dx=placed[ref].x_mm - x_min, dy=placed[ref].y_mm - y_min,
            rotation=_norm_rot(placed[ref].rot_deg),
            w=bx1 - bx0, h=by1 - by0))
    members = tuple(members)
    pads: list[CellPad] = []
    for ref in wanted:
        pads.extend(_local_pads(placed[ref], geometries.get(footprints[ref]),
                                x_min, y_min, pad_nets[ref]))

    # Net topology: local nets from the members' pads; escape decided against
    # the WHOLE board.
    member_set = set(wanted)
    net_pins: dict[str, list[str]] = {}
    net_refs_board: dict[str, set[str]] = {}
    for ref, fp in pcb.footprints.items():
        for pad in fp.pads:
            net_name = str(getattr(pad, "net_name", "") or "")
            if not net_name:
                continue
            net_refs_board.setdefault(net_name, set()).add(ref)
            if ref in member_set:
                net_pins.setdefault(net_name, []).append(f"{ref}.{pad.pad_number}")

    nets = {net: tuple(sorted(pins)) for net, pins in net_pins.items()}
    internal = frozenset(
        net for net in nets if net_refs_board.get(net, set()) <= member_set)

    return LayoutCell(
        name=str(name),
        width=x_max - x_min,
        height=y_max - y_min,
        members=members,
        pads=tuple(pads),
        nets=nets,
        internal_nets=internal,
        layers_defined=(),
        fab=fab,
        stackup=int(stackup),
        # ⚠ ``unresolved`` is reported rather than raised: a member whose
        # footprint could not be loaded got a 2 x 2 mm fallback box, so the cell
        # exists but its geometry is a guess. A gate must be able to SEE that
        # rather than infer it from a suspiciously round box.
        meta={"harvested_from": pcb_path, "refs": list(wanted),
              "origin_mm": [_q(x_min), _q(y_min)],
              "unresolved_footprints": unresolved},
    ).normalised()


def synthesise_cell(
    name: str,
    members: Sequence[tuple],
    nets: Mapping[str, Sequence[tuple]],
    *,
    fp_lib_dirs: Sequence[str] | None = None,
    internal_nets: Sequence[str] = (),
    margin_mm: float = 0.0,
    fab: str = "",
    stackup: int = 2,
) -> LayoutCell:
    """Build an uncompiled cell from an **authored** arrangement, with no board.

    :func:`harvest_cell` lifts an arrangement off a placed ``.kicad_pcb``; this
    builds one from a specification. It is what WS-U5's generated families need
    -- a family has no board to be harvested from, and writing one just to read
    it back would put a router in the middle of a pure geometry problem.

    ``members`` is ``[(local_ref, "Library:Name", x, y, rotation), ...]`` in any
    frame (the box is re-based to the origin corner, exactly as harvesting
    does). ``nets`` is ``{net name: [(local_ref, pad number), ...]}``.

    ⛔ ``internal_nets`` is **declared**, not inferred, and that is the real
    difference from harvesting. Harvesting can prove containment by looking at
    the whole board; a synthesised cell has no board, so the author states which
    nets never leave -- and an author who gets it wrong exposes a phantom port
    (safe: an extra airwire) rather than hiding a real one (unsafe: a net the
    placer thinks is contained and the router cannot reach).
    """
    dirs = list(fp_lib_dirs or [])
    placed: dict[str, PlacedPart] = {}
    footprints: dict[str, str] = {}
    for local_ref, footprint, x, y, rotation in members:
        ref = str(local_ref)
        footprints[ref] = str(footprint)
        placed[ref] = PlacedPart(ref=ref, x_mm=float(x), y_mm=float(y),
                                 rot_deg=float(_norm_rot(rotation)),
                                 footprint=str(footprint), side="front")
    order = [str(m[0]) for m in members]

    geometries = load_footprint_geometries(
        {f for f in footprints.values() if f}, dirs)
    unresolved = sorted(ref for ref in order if footprints[ref] not in geometries)

    pad_nets: dict[str, dict[str, str]] = {ref: {} for ref in order}
    net_pins: dict[str, list[str]] = {}
    for net_name, pins in nets.items():
        for local_ref, pad in pins:
            ref, number = str(local_ref), str(pad)
            if ref not in pad_nets:
                raise ValueError(f"net {net_name!r} names unknown member {ref!r}")
            pad_nets[ref][number] = str(net_name)
            net_pins.setdefault(str(net_name), []).append(f"{ref}.{number}")

    x_min = y_min = float("inf")
    x_max = y_max = float("-inf")
    for ref in order:
        bx0, by0, bx1, by1 = _member_bounds(placed[ref],
                                            geometries.get(footprints[ref]))
        x_min, y_min = min(x_min, bx0), min(y_min, by0)
        x_max, y_max = max(x_max, bx1), max(y_max, by1)
    x_min -= margin_mm
    y_min -= margin_mm
    x_max += margin_mm
    y_max += margin_mm

    cell_members, pads = [], []
    for ref in order:
        bx0, by0, bx1, by1 = _member_bounds(placed[ref],
                                            geometries.get(footprints[ref]))
        cell_members.append(CellMember(
            local_ref=ref, kind="part", footprint=footprints[ref],
            dx=placed[ref].x_mm - x_min, dy=placed[ref].y_mm - y_min,
            rotation=_norm_rot(placed[ref].rot_deg),
            w=bx1 - bx0, h=by1 - by0))
        pads.extend(_local_pads(placed[ref], geometries.get(footprints[ref]),
                               x_min, y_min, pad_nets[ref]))

    return LayoutCell(
        name=str(name),
        width=x_max - x_min,
        height=y_max - y_min,
        members=tuple(cell_members),
        pads=tuple(pads),
        nets={net: tuple(sorted(pins)) for net, pins in net_pins.items()},
        internal_nets=frozenset(str(n) for n in internal_nets),
        fab=fab,
        stackup=int(stackup),
        meta={"source": "synthesised", "refs": list(order),
              "unresolved_footprints": unresolved},
    ).normalised()


# --------------------------------------------------------------------------- #
# Nesting -- compose cells into a cell (cell-toolchain plan, WS-U4)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NestedCell:
    """One child cell placed inside a parent, before composition.

    ``dx``/``dy`` position the child's **origin corner** in the parent's raw
    frame (the composer re-bases so the parent still spans ``[0, W] x [0, H]``),
    and ``rotation`` turns the child first. ``prefix`` disambiguates member refs
    when the same child appears twice; ``""`` keeps the child's own names.
    """

    cell: LayoutCell
    dx: float = 0.0
    dy: float = 0.0
    rotation: int = 0
    prefix: str = ""

    def placed(self) -> LayoutCell:
        return rotate_cell(self.cell, self.rotation)

    def ref(self, local_ref: str) -> str:
        return f"{self.prefix}{local_ref}" if self.prefix else str(local_ref)


def _nested_children(children: Sequence[NestedCell]) -> list[tuple[NestedCell, LayoutCell]]:
    return [(child, child.placed()) for child in children]


def compose_cells(name: str, children: Sequence[NestedCell], *,
                  margin_mm: float = 0.0, fab: str = "",
                  stackup: int | None = None) -> LayoutCell:
    """Flatten ``children`` into one uncompiled parent cell.

    ⛔⛔ **The parent's two maps are NOT set here, deliberately.** Composition
    produces *geometry only*; :func:`~skidl_layout.cells_compile.compile_cell`
    re-derives ports and transit from the composed pads. Inheriting the
    children's maps would be wrong in both directions and the plan says so:
    **an outer member can pinch an inner lane** (the child's lane is no longer
    clear once a sibling sits beside it), and **an inner blocked axis blocks the
    outer box** (a child that nothing can cross makes the parent's whole span
    opaque there). :func:`inherit_maps_naively` builds the wrong answer on
    purpose, so gate ``U4`` can show the difference rather than assert it.

    ⭐ Each child leaves **one ``kind == "cell"`` member** behind carrying its
    digest, offset and rotation — the provenance a ledger row needs — while its
    parts are flattened into the same ``members`` tuple so every existing
    consumer (placement expansion, the transit sweep, the naive-area grade)
    works unchanged. :attr:`LayoutCell.part_members` is what keeps the two
    apart.

    ⚠ **Internal nets are inherited but never promoted.** A net internal to a
    child is internal to the parent (nothing outside the child touched it, so
    nothing outside the parent does either). A net that *escaped* a child may
    well be fully contained once its siblings are in the box — but the children
    alone cannot prove that, so it stays an escaping port. Re-harvest if the
    promotion matters.
    """
    placed = _nested_children(children)
    if not placed:
        raise ValueError("compose_cells needs at least one child")

    x_min = min(child.dx for child, _turned in placed) - margin_mm
    y_min = min(child.dy for child, _turned in placed) - margin_mm
    x_max = max(child.dx + turned.width for child, turned in placed) + margin_mm
    y_max = max(child.dy + turned.height for child, turned in placed) + margin_mm

    members: list[CellMember] = []
    pads: list[CellPad] = []
    segments: list[CellSegment] = []
    vias: list[CellVia] = []
    nets: dict[str, list[str]] = {}
    internal: set[str] = set()
    for child, turned in placed:
        ox, oy = child.dx - x_min, child.dy - y_min
        # ⛔ The **source** digest, not the rotated one. A rotated cell has a
        # different content hash, so recording it would make the provenance
        # entry un-findable in the cache -- the cache stores the artifact as
        # authored and the rotation is carried in ``rotation`` beside it. The
        # extents ``w``/``h`` are the rotated ones, because those are geometry.
        members.append(CellMember(
            local_ref=child.ref(turned.name), kind="cell",
            footprint=child.cell.digest, dx=ox, dy=oy,
            rotation=int(child.rotation) % 360,
            w=turned.width, h=turned.height))
        for member in turned.part_members:
            members.append(replace(member, local_ref=child.ref(member.local_ref),
                                   dx=ox + member.dx, dy=oy + member.dy))
        for pad in turned.pads:
            pads.append(replace(pad, local_ref=child.ref(pad.local_ref),
                                x=ox + pad.x, y=oy + pad.y))
            if pad.local_net:
                nets.setdefault(pad.local_net, []).append(
                    f"{child.ref(pad.local_ref)}.{pad.pad}")
        internal |= set(turned.internal_nets)
        if turned.copper is not None:
            for segment in turned.copper.segments:
                segments.append(replace(segment, x1=ox + segment.x1,
                                        y1=oy + segment.y1, x2=ox + segment.x2,
                                        y2=oy + segment.y2))
            for via in turned.copper.vias:
                vias.append(replace(via, x=ox + via.x, y=oy + via.y))

    copper = (CellCopper(segments=tuple(segments), vias=tuple(vias))
              if (segments or vias) else None)
    depth = 1 + max(int(dict(turned.meta).get("nest_depth", 0) or 0)
                    for _child, turned in placed)
    return LayoutCell(
        name=str(name),
        width=x_max - x_min,
        height=y_max - y_min,
        members=tuple(members),
        pads=tuple(pads),
        nets={net: tuple(sorted(pins)) for net, pins in nets.items()},
        internal_nets=frozenset(internal),
        copper=copper,
        fab=str(fab or next((t.fab for _c, t in placed if t.fab), "")),
        stackup=int(stackup if stackup is not None
                    else max(t.stackup for _c, t in placed)),
        meta={"composed_from": [
                  {"name": child.cell.name, "digest": child.cell.digest,
                   "rotated_digest": turned.digest,
                   "dx": _q(child.dx - x_min), "dy": _q(child.dy - y_min),
                   "rotation": int(child.rotation) % 360,
                   "prefix": child.prefix}
                  for child, turned in placed],
              "nest_depth": depth,
              "unresolved_footprints": sorted({
                  ref for _child, turned in placed
                  for ref in (dict(turned.meta).get("unresolved_footprints") or ())
              })},
    ).normalised()


def inherit_maps_naively(children: Sequence[NestedCell], *, margin_mm: float = 0.0
                         ) -> tuple[tuple[CellPort, ...], tuple[CellTransit, ...]]:
    """The maps a parent would get by **inheriting** its children's, translated.

    ⛔ **This is the wrong answer, computed on purpose.** It exists so gate
    ``U4`` can measure how far the recomputed maps diverge from inheritance
    instead of taking the design's word for it — and so that, if they ever
    agreed, that could be *said* rather than quietly assumed away.

    Ports keep their child's side and access verbatim (an inner child's "N" is
    inherited as the parent's "N", which is exactly the error). Transit lanes
    are translated onto the parent's transverse axis and unioned per
    ``(layer, axis)``.
    """
    placed = _nested_children(children)
    if not placed:
        return (), ()
    x_min = min(child.dx for child, _t in placed) - margin_mm
    y_min = min(child.dy for child, _t in placed) - margin_mm

    ports: list[CellPort] = []
    lanes: dict[tuple[int, str], list[TransitLane]] = {}
    clearance: dict[tuple[int, str], float] = {}
    for child, turned in placed:
        ox, oy = child.dx - x_min, child.dy - y_min
        for port in turned.ports:
            ports.append(replace(port, x=ox + port.x, y=oy + port.y))
        for entry in turned.transit:
            key = (entry.layer, entry.axis)
            shift = oy if entry.axis == "EW" else ox
            for lane in entry.lanes:
                lanes.setdefault(key, []).append(
                    replace(lane, lo=lane.lo + shift, hi=lane.hi + shift))
            clearance[key] = entry.clearance
    transit = tuple(
        CellTransit(layer=layer, axis=axis,
                    lanes=tuple(sorted(lanes[(layer, axis)],
                                       key=lambda lane: (lane.lo, lane.hi))),
                    clearance=clearance.get((layer, axis), 0.0),
                    source="inherited").normalised()
        for layer, axis in sorted(lanes))
    return (tuple(sorted((p.normalised() for p in ports),
                         key=lambda p: (p.local_net, p.layer, p.side))),
            transit)


# --------------------------------------------------------------------------- #
# The content-addressed cache (cell-toolchain plan, WS-U5)
# --------------------------------------------------------------------------- #
class CellCache:
    """A directory of ``<digest>.json`` files. ⭐ Deliberately dumb.

    The cache's whole contract is that a cell's **digest is its identity**, so a
    store is idempotent and a load is a pure function of the digest. There is no
    index file, no manifest and no lock: the filesystem *is* the index
    (:meth:`digests` lists it in **sorted** order, because an arbitrary
    directory order is the ``id()``-ordering class of non-determinism this
    project has been bitten by twice).

    ⚠ ``meta`` **is** written, and is **not** part of the digest — so two cells
    with different provenance and identical geometry are one file, and the
    second store wins on ``meta``. That is intentional: provenance must never
    fork the cache.
    """

    def __init__(self, root: str):
        self.root = str(root)

    def path_for(self, digest: str) -> str:
        import os

        return os.path.join(self.root, f"{digest}.json")

    def store(self, cell: LayoutCell) -> str:
        import os

        cell = cell.normalised()
        os.makedirs(self.root, exist_ok=True)
        path = self.path_for(cell.digest)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(serialise_cell(cell, include_meta=True))
        return path

    def load(self, digest: str) -> LayoutCell | None:
        import os

        path = self.path_for(str(digest))
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return deserialise_cell(handle.read())

    def digests(self) -> tuple[str, ...]:
        import os

        if not os.path.isdir(self.root):
            return ()
        return tuple(sorted(entry[:-len(".json")]
                            for entry in sorted(os.listdir(self.root))
                            if entry.endswith(".json")))

    def cells(self) -> tuple[LayoutCell, ...]:
        loaded = [self.load(digest) for digest in self.digests()]
        return tuple(cell for cell in loaded if cell is not None)

    def by_name(self, name: str) -> tuple[LayoutCell, ...]:
        return tuple(cell for cell in self.cells() if cell.name == name)


# --------------------------------------------------------------------------- #
# The transit map -- a 1-D interval sweep (plan section 3.2)
# --------------------------------------------------------------------------- #
def _complement(box_lo: float, box_hi: float,
                blocked: Sequence[tuple[float, float]],
                min_width: float) -> list[tuple[float, float]]:
    """The clear intervals of ``[box_lo, box_hi]`` left by ``blocked``."""
    merged: list[list[float]] = []
    for lo, hi in sorted((min(a, b), max(a, b)) for a, b in blocked):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    lanes: list[tuple[float, float]] = []
    cursor = box_lo
    for lo, hi in merged:
        if lo > cursor:
            lanes.append((cursor, min(lo, box_hi)))
        cursor = max(cursor, hi)
        if cursor >= box_hi:
            break
    if cursor < box_hi:
        lanes.append((cursor, box_hi))
    return [(lo, hi) for lo, hi in lanes
            if hi - lo >= min_width - 10.0 ** -QUANTUM_DP and hi > lo]


def sweep_transit(
    cell: LayoutCell,
    *,
    layers: Sequence[int],
    clearance_mm: float,
    min_track_mm: float,
    layer_of_pad=None,
    body_obstructs: bool = False,
) -> tuple[CellTransit, ...]:
    """The transit map: what a *foreign* net can cross the box on.

    For axis ``EW`` on layer ``L``: project every obstruction onto the
    transverse (N-S) axis, inflated by ``clearance_mm``; take the complement
    inside the box; drop intervals narrower than ``min_track_mm``; the survivors
    are the lanes.

    ⚠ **This is a projection, so it under-reports, and under-reporting is the
    safe direction.** A channel clear for 90 % of the crossing but pinched once
    reads as blocked, and a staircase route that dodges an obstacle is not
    counted at all. Both errors deny a crossing that exists; neither invents
    one. ⛔ **If a routed probe ever finds the projection *over*-reporting, that
    is a defect, not a tolerance** (gate ``T2t``).

    ⛔⛔ Through-board pads and every via are projected onto **all** of
    ``layers``, not merely onto ``cell.layers_defined``. This is the blank-layer
    exception and it is the one way this map could produce an unroutable board.

    ``layer_of_pad`` optionally maps a :class:`CellPad` to the copper layer
    index it sits on; the default puts every surface pad on layer 0, which is
    correct for the top-only v1 restriction.

    ⚠⚠ **``body_obstructs`` answers a question the copper sweep cannot, and it
    is OFF by default so that every recorded transit number stays byte-identical.**
    The default sweep is *copper* truth: a chip passive's ceramic body is not
    copper, so the strip **between its two pads, under the body** comes out as a
    lane -- and it is a lane, in the sense that a track routed there passes DRC.
    ⛔ It is **not** a lane in the sense a human reading a cell picture means, and
    that mismatch is what "the transit lanes are drawn straight through the
    parts" is: MEASURED 2026-07-31, **28 of 28** layer-0 lanes in the generated
    family cache and **15 of 18** in the harvested one cross a member's physical
    envelope. With this flag every member's own ``(w, h)`` envelope joins the
    obstruction list (⛔ **not** inflated by ``clearance`` -- a body is a
    mechanical obstacle, not a conductor, so the copper rule does not apply to
    it), and what survives is a channel a router *and* a reader would both call
    one. Generated families sweep with it on (:mod:`skidl_layout.cells_families`);
    harvested cells keep the copper default so the executed run's tables stand.
    """
    clearance = float(clearance_mm)
    min_track = float(min_track_mm)
    surface_layer = layer_of_pad or (lambda pad: 0)
    bodies: list[tuple[float, float, float, float]] = []
    if body_obstructs:
        for member in cell.part_members:
            bodies.append((member.dx - member.w / 2.0,
                           member.dy - member.h / 2.0,
                           member.dx + member.w / 2.0,
                           member.dy + member.h / 2.0))

    out: list[CellTransit] = []
    for layer in sorted({int(v) for v in layers}):
        # ⛔ A body obstructs the layer its part SITS on and no other. v1 is
        # top-only (``CellMember.side``), so that is layer 0 -- an inner layer
        # under a 0402 is as free as it ever was, and pretending otherwise would
        # make a four-layer cell read as opaque for a mechanical reason that
        # stops at the solder mask.
        obstructions: list[tuple[float, float, float, float]] = (
            list(bodies) if layer == 0 else [])
        for pad in cell.pads:
            if pad.through_board or surface_layer(pad) == layer:
                x0, y0, x1, y1 = pad.bounds
                obstructions.append((x0 - clearance, y0 - clearance,
                                     x1 + clearance, y1 + clearance))
        if cell.copper is not None:
            for segment in cell.copper.segments:
                if segment.layer != layer:
                    continue
                half = segment.width / 2.0 + clearance
                obstructions.append((min(segment.x1, segment.x2) - half,
                                     min(segment.y1, segment.y2) - half,
                                     max(segment.x1, segment.x2) + half,
                                     max(segment.y1, segment.y2) + half))
            for via in cell.copper.vias:            # every layer, always
                half = via.size / 2.0 + clearance
                obstructions.append((via.x - half, via.y - half,
                                     via.x + half, via.y + half))

        for axis in ("EW", "NS"):
            # EW traffic runs along x, so it is blocked by extent in y.
            if axis == "EW":
                spans = [(o[1], o[3]) for o in obstructions]
                box_hi = cell.height
            else:
                spans = [(o[0], o[2]) for o in obstructions]
                box_hi = cell.width
            lanes = tuple(
                TransitLane(layer=layer, axis=axis, lo=lo, hi=hi,
                            clear_width=hi - lo)
                for lo, hi in _complement(0.0, box_hi, spans, min_track))
            out.append(CellTransit(layer=layer, axis=axis, lanes=lanes,
                                   clearance=clearance,
                                   source="geometric").normalised())
    return tuple(out)


# --------------------------------------------------------------------------- #
# The HPWL representative point (plan section 3.6)
# --------------------------------------------------------------------------- #
def _edge_distance(x: float, y: float, side: str,
                   width: float, height: float) -> float:
    """Perpendicular distance from a local point to one box edge."""
    if side == "N":
        return y
    if side == "S":
        return height - y
    if side == "W":
        return x
    return width - x


def _tangential(x: float, y: float, side: str) -> float:
    """The coordinate that runs *along* ``side``."""
    return x if side in ("N", "S") else y


def resolve_hpwl_points(cell: LayoutCell) -> dict[str, tuple[float, float]]:
    """One local point per escaping net -- the pad nearest an escapable edge.

    **The problem this solves.** ``scoring._total_hpwl`` / ``_weighted_hpwl``
    and ``validator._compute_hpwl`` use part **centroids** by default, so a cell
    that presents one centroid reports the *same* position for every net it
    touches. ⛔⛔ The consequence is worse than lost structure: **HPWL becomes
    blind to cell rotation entirely**, and rotation is one of only two degrees
    of freedom a cell has.

    ⭐⭐ **2026-08-01: the consumer exists.** ``hpwl_objective="pads"`` makes all
    three take their per-net box over :func:`scoring._placement_pad_points`,
    which for a cell reads :func:`cell_pad_geometries` -- one synthetic pad per
    escaping net at the point this function chose. ⛔ Still opt-in; the default
    remains ``"centroid"``.

    **The rule**, in order:

    1. Candidates are the centre of every pad on the net, plus the midpoint of
       every internal copper segment on it.
    2. Distance is the perpendicular distance to the nearest edge the net has a
       non-``BLOCKED`` port on. ⭐ Edges the net cannot escape from **do not
       count**, or a net would be represented by a pad against a wall it can
       never cross.
    3. Pick the minimum; ties go to the candidate whose **tangential**
       coordinate is closest to the box centre.
    4. ⛔ **Then a total tie-break, and it is mandatory:** sorted
       ``(local_ref, pad)`` as strings. A symmetric two-pad passive centred on
       its box ties on *both* prior keys, and an incomplete tie-break is the
       ``id()``-ordering class of non-determinism this project has been bitten
       by twice.

    ⚠ **Pad centre, not the port exit point.** HPWL then measures to a point
    *inside* the box and slightly understates the trace a real net needs to get
    out -- but the understatement is a **constant per (cell, net)**, so it
    shifts the term without distorting ranking between two placements of the
    same cell, and it degrades to exactly today's behaviour for a one-part cell.

    ⚠ **Static, not dynamic.** A net escapable on two sides is represented by
    whichever pad is nearer either, even when the rest of the net lies past the
    other side. Choosing by the net's external centroid would be more accurate
    and would make the cell's contribution a function of where every other part
    currently is -- not precomputable, re-evaluated 656 times per candidate, and
    an ordering dependency in the one property this project cannot trade.
    """
    points: dict[str, tuple[float, float]] = {}
    for net in cell.escaping_nets:
        sides = cell.escapable_sides(net)
        if not sides:
            continue
        candidates: list[tuple[float, float, str, float, float]] = []
        for pad in cell.pads:
            if pad.local_net != net:
                continue
            best_side = min(
                sides, key=lambda s: (_edge_distance(pad.x, pad.y, s,
                                                     cell.width, cell.height), s))
            distance = _edge_distance(pad.x, pad.y, best_side,
                                      cell.width, cell.height)
            centre = (cell.width / 2.0 if best_side in ("N", "S")
                      else cell.height / 2.0)
            tangential = abs(_tangential(pad.x, pad.y, best_side) - centre)
            candidates.append((_q(distance), _q(tangential), pad.label,
                               pad.x, pad.y))
        if cell.copper is not None:
            for segment in cell.copper.segments:
                if segment.local_net != net:
                    continue
                mx = (segment.x1 + segment.x2) / 2.0
                my = (segment.y1 + segment.y2) / 2.0
                best_side = min(
                    sides, key=lambda s: (_edge_distance(mx, my, s, cell.width,
                                                         cell.height), s))
                distance = _edge_distance(mx, my, best_side, cell.width,
                                          cell.height)
                centre = (cell.width / 2.0 if best_side in ("N", "S")
                          else cell.height / 2.0)
                tangential = abs(_tangential(mx, my, best_side) - centre)
                candidates.append((_q(distance), _q(tangential),
                                   f"@seg.{segment.layer}.{_q(mx)}.{_q(my)}",
                                   mx, my))
        if not candidates:
            continue
        winner = min(candidates)
        points[net] = (_q(winner[3]), _q(winner[4]))
    return points


def hpwl_winner_labels(cell: LayoutCell) -> dict[str, str]:
    """Which candidate won each net's HPWL point -- the rotation-invariance probe.

    ⭐⭐ The property the design rests on is that the winning candidate is
    **rotation-invariant**: ports rotate with the cell and pads rotate with the
    cell, so the perpendicular distance from a pad to its escapable edge does
    not change under rotation, and neither does distance-to-centre on the
    tangential axis (the box centre *is* the rotation origin). Gate ``T2h``
    asserts this at all four rotations. ⛔ If it does not hold, the invariance
    argument is wrong and the point must be resolved per rotation instead --
    find that out in a unit test, not on a board.
    """
    labels: dict[str, str] = {}
    for net in cell.escaping_nets:
        sides = cell.escapable_sides(net)
        if not sides:
            continue
        candidates = []
        for pad in cell.pads:
            if pad.local_net != net:
                continue
            best_side = min(
                sides, key=lambda s: (_edge_distance(pad.x, pad.y, s,
                                                     cell.width, cell.height), s))
            distance = _edge_distance(pad.x, pad.y, best_side, cell.width,
                                      cell.height)
            centre = (cell.width / 2.0 if best_side in ("N", "S")
                      else cell.height / 2.0)
            tangential = abs(_tangential(pad.x, pad.y, best_side) - centre)
            candidates.append((_q(distance), _q(tangential), pad.label))
        if candidates:
            labels[net] = min(candidates)[2]
    return labels


# --------------------------------------------------------------------------- #
# Placement-time helpers -- the footprint masquerade (plan section 4.1)
# --------------------------------------------------------------------------- #
def net_escape_points(cell: LayoutCell) -> dict[str, tuple[float, float]]:
    """Local ``(x, y)`` per escaping net, for **both** HPWL and pad geometry.

    Prefers the compiled :attr:`LayoutCell.hpwl_points`; falls back to
    :func:`resolve_hpwl_points`; falls back again to the centroid of the net's
    member pads, which is what an *uncompiled* (harvested) cell can offer. ⛔ It
    never falls back to the box centre for a net that has pads -- that is the
    degenerate case the whole of section 3.6 exists to avoid.
    """
    points = dict(cell.hpwl_points or {})
    if not points and cell.ports:
        points = resolve_hpwl_points(cell)
    for net in cell.escaping_nets:
        if net in points:
            continue
        pads = [pad for pad in cell.pads if pad.local_net == net]
        if pads:
            points[net] = (_q(sum(p.x for p in pads) / len(pads)),
                           _q(sum(p.y for p in pads) / len(pads)))
        else:
            points[net] = (_q(cell.width / 2.0), _q(cell.height / 2.0))
    return points


def cell_pad_numbers(cell: LayoutCell) -> dict[str, str]:
    """``{escaping net: pad number}`` -- the join key of the masquerade.

    ⛔ ``scoring._placement_pad_points`` reads pad->net from the **circuit**
    (``roles.part_pin_nets_by_number``) and pad->position from the **geometry**,
    and joins them on the pad number. So the pseudo-``Part`` and the synthetic
    ``FootprintGeometry`` must agree on these strings exactly. One function
    produces them for both.
    """
    return {net: str(index)
            for index, net in enumerate(cell.escaping_nets, start=1)}


def cell_pad_geometries(cell: LayoutCell):
    """Synthetic pads: one per escaping net, at its escape point.

    Positions are **centre-referenced** (the engine's ``PlacedPart`` is a
    centre), so the local frame's origin-corner coordinates are shifted by half
    the box.
    """
    from .geometry import PadGeometry

    numbers = cell_pad_numbers(cell)
    points = net_escape_points(cell)
    half_w, half_h = cell.width / 2.0, cell.height / 2.0
    pads = []
    for net in cell.escaping_nets:
        x, y = points[net]
        pads.append(PadGeometry(
            number=numbers[net],
            x_mm=_q(x - half_w),
            y_mm=_q(y - half_h),
            width_mm=0.0,
            height_mm=0.0,
            shape="rect",
            layers=("F.Cu",),
            net_name=net,
            pad_type="smd",
        ))
    return pads


def cell_geometry(cell: LayoutCell, key: str) -> FootprintGeometry:
    """A synthetic :class:`FootprintGeometry` so a cell can masquerade as one.

    ⭐⭐ This is the plan's key seam: ``fp_geometries`` is a plain string-keyed
    dict threaded through ``plan_layout -> candidates -> placer -> refinement ->
    validator -> scorer``, so a synthetic entry buys rigid-body placement, AABB
    validation, outline containment, orientation trials and MST crossings **with
    no change to the refinement hot loop at all**.

    ⚠ The pads carry **zero size** on purpose: they are net *positions*, not
    copper. ``physical_bounds`` unions ``body_bounds`` with the pad rects, so a
    zero-size pad inside the box cannot inflate the cell's footprint -- the box
    is the box.
    """
    half_w, half_h = cell.width / 2.0, cell.height / 2.0
    box = (_q(-half_w), _q(-half_h), _q(half_w), _q(half_h))
    return FootprintGeometry(
        footprint=key,
        pads=cell_pad_geometries(cell),
        body_bounds=box,
        courtyard_bounds=box,
    )


def member_placed_parts(cell: LayoutCell, origin_x: float, origin_y: float,
                        rot_deg: int = 0, *,
                        ref_map: Mapping[str, str] | None = None,
                        ) -> list[PlacedPart]:
    """Expand a placed cell into its members' :class:`PlacedPart`s.

    ``(origin_x, origin_y)`` is the **box centre** in board coordinates, so a
    placed cell behaves like any other part: the engine's ``PlacedPart`` carries
    a centre, not a corner.

    ⛔ Expansion happens at emission and **before any digest is taken** -- the
    canonical digest feeds on ``ref, x, y, rot, side`` and nothing else, so a
    digest over pseudo-parts is not comparable to the recorded goldens.
    """
    turned = rotate_cell(cell, rot_deg)
    half_w, half_h = turned.width / 2.0, turned.height / 2.0
    out: list[PlacedPart] = []
    for member in turned.part_members:
        ref = (ref_map or {}).get(member.local_ref, member.local_ref)
        out.append(PlacedPart(
            ref=ref,
            x_mm=_q(origin_x - half_w + member.dx),
            y_mm=_q(origin_y - half_h + member.dy),
            rot_deg=float(member.rotation),
            footprint=member.footprint,
            side="front",
        ))
    return sorted(out, key=lambda p: p.ref)
