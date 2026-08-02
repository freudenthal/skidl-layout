# -*- coding: utf-8 -*-
"""Stamping a cell's copper onto a board, and fencing the box (WS-U2 / WS-T5).

⭐⭐ **What stamping is for.** A compiled cell already carries an *arrangement*
the placer drops in whole. Its **copper** -- the tracks and vias the human (or
the family generator) drew inside the box -- is carried on
:class:`~skidl_layout.cells.CellCopper` and, until this module, went nowhere. A
board that gets the arrangement but not the copper re-derives the loop the cell
existed to freeze.

The mechanism is three verified facts about KiCadRoutingTools, all recorded in
the plan's section 2 and re-checked here:

1. **Pre-existing copper is a hard obstacle by default** (``obstacle_map.py``),
   so the router routes *around* a stamped track for free -- nothing has to
   teach it that the cell owns that channel.
2. **Copper belonging to an in-scope net is re-added as a connected stub**
   (``obstacle_map.py``), so the router *extends* a boundary stub rather than
   ripping it.
3. ``route.py --keep-input-copper`` stops the cleanup sweep eating the stubs.

⛔⛔ **The lock hazard, and it is asymmetric.** ``protected_nets.py`` honours
``(locked yes)`` **absolutely, with no override**: a locked track can never be
ripped. That is exactly what an *internal* net wants (nothing outside the box
has any business rerouting it) and exactly what a *boundary* net must not have
(the router has to be free to rip and re-lay the stub while it finds a path).
:func:`stamp_cell_copper` therefore locks **internal-net copper only**, and
``lock=`` exists so the negative can be reproduced rather than argued.

⛔ **Emission is a SPLICE, never a re-serialise.** ``writer.write_kicad_pcb``
cannot emit tracks, vias or zones at all, and round-tripping a board through a
re-serialiser changes KiCad-10 spelling that is load-bearing. The blocks are
built by **KRT's own constructors** (escalation rung 2 --
``generate_segment_sexpr`` / ``generate_via_sexpr``) and pasted in before the
board's closing paren (rung 3), which is the same convention
``copper_post.splice_vias`` and ``power_escape.write_keepout_polygons`` follow.

⚠ **Why not ``kicad_writer.add_tracks_and_vias_to_pcb``**, which the plan's
section 2 also names: it does considerably more than splice. It seeds a sibling
``.kicad_pro``, moves copper text to silkscreen, moves copper graphics to
silkscreen and strips zero-length edge cuts — all reasonable for a *router*
front end and all uninvited here, where the board being stamped is an arm in a
controlled comparison and every byte of difference has to be attributable. The
two constructors are used; the file surgery is ours, and it is 20 lines.

⛔ **Determinism.** KRT's constructors stamp a fresh ``uuid4`` into every block.
This module rewrites each one with a ``uuid5`` derived from the block's own
geometry, because a board whose bytes change on every run cannot be gated -- the
same fix ``copper_post.splice_vias`` already carries.

The two fences
--------------
A cell's box is a claim on **area**, and a foreign trace cutting through it
undoes the arrangement. Two ways to say so, and **both ship** rather than one
winning in advance (the executed plan made "lane beats blanket" a bail-out;
here it is a ledger row, because the transit map's consumer has to exist before
its value can be measured on more than one corpus):

* ``"blanket"`` -- one rectangle over the whole box. Simple, and it throws away
  everything :func:`~skidl_layout.cells.sweep_transit` measured.
* ``"lanes"`` -- **box minus lanes**: the box's obstructed strips only, so a
  stranger may still cross on the channels the sweep proved clear. This is the
  first consumer the transit map has ever had.

⚠ Both are emitted as ``User.2`` ``gr_poly`` keep-outs, which is the **only**
mechanism reachable from our subprocess adapter -- KRT reads them out of the raw
file text (``kicad_parser.parse_keepout_zones``), and ``bga_exclusion_zones`` is
not reachable from argv. ⛔⛔ And the keep-out's own known cost is on record
(power-layout Phases 13/14, refuted three times): ``add_user_keepout_obstacles``
blocks *all* copper layers for *every* routed net, so a fence that protects a
cell also forbids the cell's **own** escape from using that area. That is why
the lane fence exists at all, and why neither fence is on by default.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

from .cells import LayoutCell, TransitLane, _q, rotate_cell

__all__ = [
    "BoardCopper",
    "StampResult",
    "board_copper",
    "cell_fence_polygons",
    "instance_copper",
    "layer_name",
    "stamp_cell_copper",
    "stamped_copper_key",
    "write_cell_fences",
]

#: ⭐ Same namespace shape ``copper_post`` uses: a fixed uuid5 namespace so a
#: re-run reproduces every id byte for byte.
_UUID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

_UUID_RE = re.compile(r'(\(uuid\s+)"[^"]*"\s*\)')


def layer_name(index: int, copper_layers: int = 2) -> str:
    """Copper layer *index* -> KiCad name, by ``writer``'s own table.

    ⛔ Not a local convention: :func:`~skidl_layout.writer._copper_layer_names`
    is what the board file was written from, so anything else would stamp copper
    onto a layer the board does not declare.
    """
    from .writer import _copper_layer_names

    names = _copper_layer_names(int(copper_layers))
    index = int(index)
    if not 0 <= index < len(names):
        raise ValueError(f"layer index {index} is outside a "
                         f"{copper_layers}-layer stackup {names}")
    return names[index]


# --------------------------------------------------------------------------- #
# Cell copper -> board coordinates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Track:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str
    locked: bool = False

    def key(self) -> tuple:
        return (self.net, self.layer, self.x1, self.y1, self.x2, self.y2,
                self.width)


@dataclass(frozen=True)
class _Via:
    x: float
    y: float
    size: float
    drill: float
    net: str
    locked: bool = False

    def key(self) -> tuple:
        return (self.net, self.x, self.y, self.size, self.drill)


@dataclass(frozen=True)
class BoardCopper:
    """Every ``(segment)`` and ``(via)`` on a board, keyed so uuids cannot leak.

    ⛔ **The comparison gate U2 makes is on GEOMETRY, not on file bytes.** Every
    KiCad copper block carries a uuid, and the router re-emits the board, so a
    byte diff of the file would fail on identity churn that means nothing. The
    keys here are ``(net, layer, coords, width)`` and ``(net, x, y, size,
    drill)``, quantised to the storage grid.
    """

    tracks: frozenset
    vias: frozenset

    def contains_all(self, other: "BoardCopper") -> tuple[bool, list, list]:
        """``(ok, missing tracks, missing vias)`` of ``other`` inside ``self``."""
        missing_t = sorted(other.tracks - self.tracks)
        missing_v = sorted(other.vias - self.vias)
        return (not missing_t and not missing_v, missing_t, missing_v)


def instance_copper(cell: LayoutCell, origin_x: float, origin_y: float,
                    rot_deg: int = 0, *, copper_layers: int = 2,
                    net_map=None, lock_internal: bool = True,
                    ) -> tuple[list[_Track], list[_Via]]:
    """One placed cell's copper, in board coordinates.

    ``(origin_x, origin_y)`` is the **box centre** -- the same convention
    :func:`~skidl_layout.cells.member_placed_parts` uses, so a stamped track and
    the member pad it lands on are derived from one origin and cannot drift.
    """
    turned = rotate_cell(cell, rot_deg)
    if turned.copper is None:
        return [], []
    half_w, half_h = turned.width / 2.0, turned.height / 2.0
    ox, oy = origin_x - half_w, origin_y - half_h
    mapping = dict(net_map or {})
    internal = set(turned.internal_nets)

    tracks = []
    for segment in turned.copper.segments:
        net = str(mapping.get(segment.local_net, segment.local_net))
        tracks.append(_Track(
            x1=_q(ox + segment.x1), y1=_q(oy + segment.y1),
            x2=_q(ox + segment.x2), y2=_q(oy + segment.y2),
            width=_q(segment.width),
            layer=layer_name(segment.layer, copper_layers), net=net,
            locked=bool(lock_internal and segment.local_net in internal)))
    vias = []
    for via in turned.copper.vias:
        net = str(mapping.get(via.local_net, via.local_net))
        vias.append(_Via(x=_q(ox + via.x), y=_q(oy + via.y),
                         size=_q(via.size), drill=_q(via.drill), net=net,
                         locked=bool(lock_internal
                                     and via.local_net in internal)))
    return (sorted(tracks, key=lambda t: t.key()),
            sorted(vias, key=lambda v: v.key()))


# --------------------------------------------------------------------------- #
# The splice
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StampResult:
    board: str
    tracks: int
    vias: int
    locked_tracks: int
    locked_vias: int
    net_form: str                       # "name" (KiCad 10) | "id"
    unknown_nets: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"board": self.board, "tracks": self.tracks, "vias": self.vias,
                "locked_tracks": self.locked_tracks,
                "locked_vias": self.locked_vias, "net_form": self.net_form,
                "unknown_nets": list(self.unknown_nets)}


def _net_table(text: str) -> dict[str, int]:
    """``{net name: id}`` from the board's own ``(net <id> "<name>")`` table.

    ⚠ **KiCad 10 removes this table entirely** (open issue 9), in which case the
    board addresses nets by name and the ``id`` form is never used. Returning an
    empty map is the correct answer there, not a failure.
    """
    return {name: int(ident) for ident, name in
            re.findall(r'\(net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\s*\)', text)}


def _deterministic(block: str, seed: str) -> str:
    ident = uuid.uuid5(_UUID_NAMESPACE, seed)
    return _UUID_RE.sub(lambda m: f'{m.group(1)}"{ident}")', block, count=1)


def _locked(block: str) -> str:
    """Add ``(locked yes)`` to a freshly built copper block, **immediately before
    the ``(net ...)`` line**.

    ⛔⛔ **The position is load-bearing and this was measured, not assumed.**
    ``kicad_parser.extract_segments`` / ``extract_vias`` match a whole block with
    one strict-ordering regex that tolerates ``(locked yes)`` in exactly two
    slots -- between ``width`` and ``layer``, or between ``layer`` and ``net``
    (and, for a via, either side of ``free``). Put the token anywhere else -- for
    instance straight after ``(segment``, which is where it reads most naturally
    -- and **the block matches nothing at all**: KRT never sees the copper,
    never makes it an obstacle, and routes straight through it. That is KRT's
    own issue #150, and its regexes carry the comment saying so. Before the
    ``(net ...)`` line is the one slot legal for both.
    """
    return re.sub(r"(\n\s*)(\(net\b)", r"\1(locked yes)\1\2", block, count=1)


def stamp_cell_copper(pcb_path: str, out_path: str, placements, *,
                      copper_layers: int = 2, lock_internal: bool = True,
                      ) -> StampResult:
    """Write ``pcb_path`` to ``out_path`` with every placement's copper added.

    ``placements`` is a sequence of ``(cell, x_mm, y_mm, rot_deg, net_map)``
    tuples -- deliberately **not** ``CellInstance`` + ``PlacedPart``, so the
    stamper can be exercised without the placement stack.

    ⛔ **``lock_internal`` locks the cell's INTERNAL nets only.** Locking a
    boundary net's stub makes that net never-rippable with no override
    (``protected_nets.py``), which is the plan's section 10.2 hazard: the router
    would be handed a stub it cannot move and a net it must still connect.
    """
    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    from .krt import find_krt

    krt = find_krt(None)
    if krt is None:                                            # pragma: no cover
        raise RuntimeError("KiCadRoutingTools not found; stamping uses its "
                           "own s-expression constructors (escalation rung 2)")
    import sys

    if krt not in sys.path:
        sys.path.insert(0, krt)
    from kicad_writer import generate_segment_sexpr, generate_via_sexpr

    net_ids = _net_table(text)
    use_names = not net_ids
    tracks: list[_Track] = []
    vias: list[_Via] = []
    for cell, x_mm, y_mm, rot_deg, net_map in placements:
        t, v = instance_copper(cell, float(x_mm), float(y_mm),
                               int(rot_deg) % 360, copper_layers=copper_layers,
                               net_map=net_map, lock_internal=lock_internal)
        tracks.extend(t)
        vias.extend(v)
    tracks.sort(key=lambda t: t.key())
    vias.sort(key=lambda v: v.key())

    unknown = sorted({item.net for item in (*tracks, *vias)
                      if not use_names and item.net not in net_ids})

    blocks: list[str] = []
    for track in tracks:
        block = generate_segment_sexpr(
            (track.x1, track.y1), (track.x2, track.y2), track.width,
            track.layer, net_ids.get(track.net, 0),
            net_name=track.net if use_names else None)
        block = _deterministic(block, f"cell-seg|{track.key()}")
        blocks.append(_locked(block) if track.locked else block)
    for via in vias:
        block = generate_via_sexpr(
            via.x, via.y, via.size, via.drill,
            [layer_name(0, copper_layers), layer_name(copper_layers - 1,
                                                      copper_layers)],
            net_ids.get(via.net, 0),
            net_name=via.net if use_names else None)
        block = _deterministic(block, f"cell-via|{via.key()}")
        blocks.append(_locked(block) if via.locked else block)

    end = text.rstrip()
    if not end.endswith(")"):
        raise ValueError(f"{pcb_path} does not end in a closing paren")
    trailing = text[len(end):]
    body = end[:-1].rstrip("\n")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(body + "\n" + "\n".join(blocks) + "\n)" + trailing)

    return StampResult(board=out_path, tracks=len(tracks), vias=len(vias),
                       locked_tracks=sum(1 for t in tracks if t.locked),
                       locked_vias=sum(1 for v in vias if v.locked),
                       net_form="name" if use_names else "id",
                       unknown_nets=tuple(unknown))


def board_copper(pcb_path: str, *, krt_dir: str | None = None) -> BoardCopper:
    """Read a board's copper through KRT's parser, keyed uuid-insensitively."""
    from .ratnest import _kicad_parser

    kp = _kicad_parser(krt_dir)
    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    nets, name_to_id = kp.extract_nets(text, kp.detect_kicad_version(text))
    names = {ident: net.name for ident, net in nets.items()}

    tracks = set()
    for segment in kp.extract_segments(text, name_to_id):
        if getattr(segment, "graphic", False):
            continue
        ends = sorted(((_q(segment.start_x), _q(segment.start_y)),
                       (_q(segment.end_x), _q(segment.end_y))))
        tracks.add((names.get(segment.net_id, ""), segment.layer,
                    ends[0][0], ends[0][1], ends[1][0], ends[1][1],
                    _q(segment.width)))
    vias = set()
    for via in kp.extract_vias(text, name_to_id):
        vias.add((names.get(via.net_id, ""), _q(via.x), _q(via.y),
                  _q(via.size), _q(via.drill)))
    return BoardCopper(tracks=frozenset(tracks), vias=frozenset(vias))


def stamped_copper_key(tracks, vias) -> BoardCopper:
    """The same keying as :func:`board_copper`, over what we are about to stamp.

    ⭐ One function pair, so "what we wrote" and "what came back" cannot be
    compared through two different normalisations -- which is the way this class
    of gate usually passes for the wrong reason.
    """
    keyed = set()
    for track in tracks:
        (ax, ay), (bx, by) = sorted(((track.x1, track.y1), (track.x2, track.y2)))
        keyed.add((track.net, track.layer, ax, ay, bx, by, track.width))
    return BoardCopper(
        tracks=frozenset(keyed),
        vias=frozenset((v.net, v.x, v.y, v.size, v.drill) for v in vias))


# --------------------------------------------------------------------------- #
# The two fences
# --------------------------------------------------------------------------- #
def _rect(x0: float, y0: float, x1: float, y1: float):
    return [(_q(x0), _q(y0)), (_q(x1), _q(y0)), (_q(x1), _q(y1)),
            (_q(x0), _q(y1))]


def cell_fence_polygons(cell: LayoutCell, origin_x: float, origin_y: float,
                        rot_deg: int = 0, *, mode: str = "lanes",
                        layer: int = 0, axis: str = "EW",
                        min_strip_mm: float = 0.0) -> list[list[tuple]]:
    """The keep-out polygons for one placed cell's box.

    ``mode="blanket"`` returns the whole box as one rectangle.

    ``mode="lanes"`` returns **box minus lanes** on ``(layer, axis)``: the
    complement of :attr:`~skidl_layout.cells.CellTransit.lanes` along the
    transverse axis, as full-width strips. ⭐ This is the transit map's first
    consumer -- until it existed, ``total_width`` and ``max_trace`` were
    measured and reported and nothing could act on them.

    ⚠ An **undefined** ``(layer, axis)`` means *fully passable* (the blank-layer
    default), so ``"lanes"`` on a layer the cell says nothing about returns
    ``[]`` -- no fence at all. That is the correct reading of the default and
    the opposite of what a port's absence means.
    """
    turned = rotate_cell(cell, rot_deg)
    half_w, half_h = turned.width / 2.0, turned.height / 2.0
    ox, oy = origin_x - half_w, origin_y - half_h
    if mode == "blanket":
        return [_rect(ox, oy, ox + turned.width, oy + turned.height)]
    if mode != "lanes":
        raise ValueError(f"fence mode must be 'blanket' or 'lanes', got {mode!r}")

    entry = turned.transit_for(layer, axis)
    if entry is None:
        return []
    span = turned.height if axis == "EW" else turned.width
    lanes: list[TransitLane] = sorted(entry.lanes, key=lambda lane: lane.lo)
    blocked: list[tuple[float, float]] = []
    cursor = 0.0
    for lane in lanes:
        if lane.lo > cursor:
            blocked.append((cursor, lane.lo))
        cursor = max(cursor, lane.hi)
    if cursor < span:
        blocked.append((cursor, span))

    out = []
    for lo, hi in blocked:
        if hi - lo < min_strip_mm:
            continue
        if axis == "EW":                     # transverse axis is y
            out.append(_rect(ox, oy + lo, ox + turned.width, oy + hi))
        else:                                # transverse axis is x
            out.append(_rect(ox + lo, oy, ox + hi, oy + turned.height))
    return out


def write_cell_fences(pcb_path: str, polygons, *, layer: str | None = None) -> int:
    """Append the fence polygons to a board, in place. Returns the count.

    ⭐ Delegates to :func:`~skidl_layout.power_escape.write_keepout_polygons`,
    whose emitted s-expression is round-tripped through **KRT's own parser** in
    the test suite rather than checked by eye -- there is no second spelling of
    a ``gr_poly`` keep-out in this codebase and there must not be.
    """
    from .power_escape import DEFAULT_KEEPOUT_LAYER, write_keepout_polygons

    return write_keepout_polygons(pcb_path, polygons,
                                  layer=layer or DEFAULT_KEEPOUT_LAYER)
