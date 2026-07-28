"""Move plane-stitching vias out of SMD pads, and tie the pad back with a stub.

Power-layout Phase 8, WS-B. **Every poured board this stack has produced is
via-in-pad** -- Phase 5's ``fab_check`` rule measured 11 violations on the
Phase-4 boost, 12 on the Phase-6 board and 37 on avalanche, and verified rather
than assumed that all of them are KRT ``route_planes.py`` plane vias sitting
dead-centre in an SMD ground pad. That is exactly what a plane router does (drop
each ground pad to the plane through a via *at* the pad) and it is nonetheless
via-in-pad, which the shipped ``oshpark-2l`` spec declares ``False``.

This is a **post-process** (rung 3 of the escalation ladder), the same rung and
the same machinery as Phase 5's :mod:`skidl_layout.copper_post`: read the board
KRT already wrote, compute geometry, splice, then let the caller re-grade with
KRT's own ``check_drc`` / ``check_connected``.

**The thing that makes this different from a thermal via array.** A thermal via
is *additive* -- dropping one costs nothing. These are plane **stitching** vias:
the via at the pad IS the pad's only path to the plane on the other layer, so
moving it without replacing that path orphans the pad. Every relocation
therefore emits **two** objects:

    1. the via, on a deterministic ring just outside the pad rectangle, and
    2. a **stub track** on the pad's own copper layer from the pad centre to the
       new via position -- the pad-to-plane link the move would otherwise break.

Nothing here drops a via. Phase 5's "drop rather than ship a violation" rule is
deliberately *not* inherited (Phase-8 plan section 6, bail-out 2): an unresolved
via-in-pad is a manufacturability note, and a ground pad floating off the plane
is a broken board. A via with no legal position **stays where it is** and is
counted.

Pure geometry + text splicing: no KiCad, no KRT, no I/O beyond the board file.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field

__all__ = [
    "ViaMove",
    "ViaRelocationPlan",
    "find_vias_in_pads",
    "plan_via_relocations",
    "apply_via_relocations",
]

#: Namespace for the deterministic ids of the stub tracks this module splices in.
#: Distinct from :data:`skidl_layout.copper_post._UUID_NAMESPACE` so a stub and a
#: thermal via at the same coordinates cannot collide.
_UUID_NAMESPACE = uuid.UUID("2b7c1e94-3d5a-5f60-8e17-4a2c9b6d0f31")

#: Candidate positions per ring, 360/8 = 45 degrees apart. Ordered by angle
#: ascending from +x -- deterministic by construction, never nearest-first with
#: ties, because a tie-break by distance is exactly where run-to-run drift gets
#: in (the pour boundary already moves +-4 mm^2 between identical runs).
RING_CANDIDATES = 8

#: How many times the ring may grow before a via is given up on. The plan's
#: "grow the radius one step and retry, at most twice" -- so three radii total.
MAX_RADIUS_STEPS = 3


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass
class ViaMove:
    """One via-in-pad and what this module decided to do about it."""

    #: Index into the board's ``(via ...)`` blocks, in file order. Stable for a
    #: given board text, which is what makes the splice addressable.
    via_index: int
    x: float
    y: float
    net_id: int
    net_name: str
    #: ``"REF.PADNUM"`` of the pad the via sits in.
    pad: str
    pad_layer: str = "F.Cu"
    #: ``"relocated"``, ``"unresolved"`` or ``"foreign_net"``.
    status: str = "unresolved"
    new_x: float | None = None
    new_y: float | None = None
    #: Which ring the accepted candidate came from (0 = the tightest).
    radius_step: int | None = None
    radius_mm: float | None = None
    angle_deg: float | None = None
    #: Width of the pad-to-via stub track, mm.
    stub_width_mm: float | None = None
    reason: str = ""

    @property
    def moved(self) -> bool:
        return self.status == "relocated"

    def to_dict(self) -> dict:
        return {
            "via_index": self.via_index,
            "from": [round(self.x, 4), round(self.y, 4)],
            "to": ([round(self.new_x, 4), round(self.new_y, 4)]
                   if self.new_x is not None else None),
            "net": self.net_name,
            "pad": self.pad,
            "pad_layer": self.pad_layer,
            "status": self.status,
            "radius_step": self.radius_step,
            "radius_mm": round(self.radius_mm, 4) if self.radius_mm else None,
            "angle_deg": self.angle_deg,
            "stub_width_mm": self.stub_width_mm,
            "reason": self.reason,
        }


@dataclass
class ViaRelocationPlan:
    """What :func:`plan_via_relocations` decided across the whole board."""

    moves: list = field(default_factory=list)
    #: Vias inside a pad of a **different** net -- left alone on purpose.
    foreign: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def in_pad_count(self) -> int:
        return len(self.moves) + len(self.foreign)

    @property
    def relocatable(self) -> list:
        return [m for m in self.moves if m.moved]

    @property
    def unresolved(self) -> list:
        return [m for m in self.moves if not m.moved]

    def to_dict(self) -> dict:
        return {
            "in_pad_count": self.in_pad_count,
            "relocatable": len(self.relocatable),
            "unresolved": len(self.unresolved),
            "foreign_net": len(self.foreign),
            "moves": [m.to_dict() for m in self.moves],
            "foreign": [m.to_dict() for m in self.foreign],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# board reading
# ---------------------------------------------------------------------------


def _board_geometry(pcb_path: str) -> dict:
    """Everything the clearance predicate needs, read once.

    Returns ``{pads, vias, segments, outline, net_ids}``. Pads carry their net
    name (via :func:`skidl_layout.copper_post._pad_nets`) because the whole rule
    turns on same-net vs foreign-net.
    """
    from simp_sexp import Sexp

    from .copper_post import _pad_nets
    from .fabspec import _edge_bbox, _smd_copper_pads, _net_id_to_name
    from .reader import _find_child, _find_children

    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    board = Sexp(text)

    net_map = _net_id_to_name(board)
    name_to_id = {name: nid for nid, name in net_map.items()}
    pad_nets = _pad_nets(board)

    pads = []
    for pad in _smd_copper_pads(board):
        name = pad_nets.get((pad["ref"], pad["number"]))
        pads.append({**pad, "net_name": name,
                     "net_id": name_to_id.get(name) if name else None})

    vias = []
    for index, via in enumerate(board.search("via")):
        at = _find_child(via, "at")
        size = _find_child(via, "size")
        net = _find_child(via, "net")
        if at is None or len(at) < 3:
            continue
        vias.append({
            "index": index,
            "x": float(at[1]), "y": float(at[2]),
            "size": float(size[1]) if size is not None and len(size) > 1 else 0.6,
            "net_id": int(net[1]) if net is not None and len(net) > 1 else 0,
        })

    segments = []
    for seg in board.search("segment"):
        start = _find_child(seg, "start")
        end = _find_child(seg, "end")
        width = _find_child(seg, "width")
        net = _find_child(seg, "net")
        layer = _find_child(seg, "layer")
        if start is None or end is None or len(start) < 3 or len(end) < 3:
            continue
        segments.append({
            "x1": float(start[1]), "y1": float(start[2]),
            "x2": float(end[1]), "y2": float(end[2]),
            "width": float(width[1]) if width is not None and len(width) > 1 else 0.2,
            "net_id": int(net[1]) if net is not None and len(net) > 1 else 0,
            "layer": str(layer[1]).strip('"') if layer is not None and len(layer) > 1
                     else "F.Cu",
        })

    return {
        "text": text,
        "pads": pads,
        "vias": vias,
        "segments": segments,
        "outline": _edge_bbox(board, _find_children),
        "net_map": net_map,
    }


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def _point_to_segment_mm(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 < 1e-18:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _segment_to_segment_mm(a, b) -> float:
    """Minimum distance between two segments ``(x1,y1,x2,y2)``."""
    if _segments_intersect(a, b):
        return 0.0
    return min(
        _point_to_segment_mm(a[0], a[1], *b),
        _point_to_segment_mm(a[2], a[3], *b),
        _point_to_segment_mm(b[0], b[1], *a),
        _point_to_segment_mm(b[2], b[3], *a),
    )


def _segments_intersect(a, b) -> bool:
    def cross(ox, oy, ax, ay, bx, by):
        return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)

    d1 = cross(b[0], b[1], b[2], b[3], a[0], a[1])
    d2 = cross(b[0], b[1], b[2], b[3], a[2], a[3])
    d3 = cross(a[0], a[1], a[2], a[3], b[0], b[1])
    d4 = cross(a[0], a[1], a[2], a[3], b[2], b[3])
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _pad_corners(pad) -> list:
    """The pad rectangle's four corners in board space, rotation applied."""
    radians = math.radians(pad["angle"])
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    hw, hh = pad["w"] / 2.0, pad["h"] / 2.0
    corners = []
    for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        # Forward of fabspec._point_in_pad's inverse rotation.
        corners.append((pad["x"] + lx * cos_a + ly * sin_a,
                        pad["y"] - lx * sin_a + ly * cos_a))
    return corners


def _point_to_pad_mm(px, py, pad) -> float:
    """Distance from a point to a pad rectangle; 0.0 when inside."""
    from .fabspec import _point_in_pad

    if _point_in_pad(px, py, pad):
        return 0.0
    corners = _pad_corners(pad)
    return min(
        _point_to_segment_mm(px, py, *corners[i], *corners[(i + 1) % 4])
        for i in range(4)
    )


def _segment_to_pad_mm(seg, pad) -> float:
    from .fabspec import _point_in_pad

    if _point_in_pad(seg[0], seg[1], pad) or _point_in_pad(seg[2], seg[3], pad):
        return 0.0
    corners = _pad_corners(pad)
    return min(
        _segment_to_segment_mm(seg, (*corners[i], *corners[(i + 1) % 4]))
        for i in range(4)
    )


def _pad_half_diagonal(pad) -> float:
    return math.hypot(pad["w"], pad["h"]) / 2.0


def _pad_copper_layer(pad) -> str:
    for name in pad.get("layers") or []:
        if name.endswith(".Cu"):
            return name
    return "F.Cu"


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def find_vias_in_pads(pcb_path: str, spec) -> list:
    """``[(via, pad)]`` for every via centre inside an SMD copper pad.

    Deliberately the **same** predicate ``fab_check``'s ``via_in_pad`` rule uses
    (:func:`skidl_layout.fabspec._point_in_pad`, first pad wins), so the count
    this module reports and the count the gate reports cannot drift apart.
    """
    from .fabspec import _point_in_pad

    geom = _board_geometry(pcb_path)
    hits = []
    for via in geom["vias"]:
        for pad in geom["pads"]:
            if _point_in_pad(via["x"], via["y"], pad):
                hits.append((via, pad))
                break
    return hits


def plan_via_relocations(
    pcb_path: str,
    spec,
    ring_candidates: int = RING_CANDIDATES,
    max_radius_steps: int = MAX_RADIUS_STEPS,
) -> ViaRelocationPlan:
    """Decide where each via-in-pad should go. Writes nothing.

    For each flagged via, candidates sit on a ring **centred on the pad** at
    ``pad_half_diagonal + via_diameter/2 + clearance`` -- the smallest radius
    that keeps the whole via outside the pad rectangle in every direction --
    stepping by ``360/ring_candidates`` degrees, ordered by **angle ascending
    from +x**. The first candidate that passes the clearance predicate wins; if
    none does, the ring grows by ``via_diameter/2 + clearance`` and the sweep
    repeats, at most ``max_radius_steps`` radii in total.

    A candidate must, with its stub track:

    - keep the via's copper inside the board outline by ``board_edge_keepout_mm``;
    - stay ``clearance_mm`` clear of every pad, via and track of a **different**
      net;
    - not land inside **any** pad -- including one of its own net, which would
      simply be via-in-pad again somewhere else.

    Same-net copper is not an obstacle: a ground via touching ground copper is a
    connection, not a violation. That asymmetry is the whole reason the stub
    track is legal at all.
    """
    from .fabspec import _point_in_pad

    plan = ViaRelocationPlan()
    if spec is None:
        plan.notes.append("no FabSpec resolved: via-in-pad relocation needs one "
                          "for its clearance and via geometry")
        return plan

    geom = _board_geometry(pcb_path)
    clearance = float(spec.clearance_mm)
    via_d = float(spec.via_size_mm)
    keepout = float(spec.board_edge_keepout_mm)
    outline = geom["outline"]

    for via in geom["vias"]:
        pad = None
        for candidate_pad in geom["pads"]:
            if _point_in_pad(via["x"], via["y"], candidate_pad):
                pad = candidate_pad
                break
        if pad is None:
            continue

        net_name = geom["net_map"].get(via["net_id"], "")
        move = ViaMove(
            via_index=via["index"], x=via["x"], y=via["y"],
            net_id=via["net_id"], net_name=net_name,
            pad=f"{pad['ref']}.{pad['number']}",
            pad_layer=_pad_copper_layer(pad),
        )

        # Step 1 of the algorithm: only ever act when via net == pad net. A via
        # of a DIFFERENT net inside a pad is a short, i.e. somebody else's bug --
        # moving it would paper over a genuine DRC problem.
        if pad.get("net_id") != via["net_id"]:
            move.status = "foreign_net"
            move.reason = (
                f"via is on {net_name or '?'} but pad {move.pad} is on "
                f"{pad.get('net_name') or 'no net'}: a foreign-net via inside a "
                "pad is a short, not a via-in-pad relocation -- left alone and "
                "reported")
            plan.foreign.append(move)
            continue

        stub_width = max(float(spec.min_track_mm),
                         min(float(spec.track_width_mm), pad["w"], pad["h"]))
        base_radius = _pad_half_diagonal(pad) + via_d / 2.0 + clearance
        radius_step_mm = via_d / 2.0 + clearance

        chosen = None
        for step in range(max(1, int(max_radius_steps))):
            radius = base_radius + step * radius_step_mm
            for k in range(int(ring_candidates)):
                angle = 360.0 * k / float(ring_candidates)
                radians = math.radians(angle)
                cx = pad["x"] + radius * math.cos(radians)
                cy = pad["y"] + radius * math.sin(radians)
                if _candidate_ok(cx, cy, pad, via, geom, spec, clearance,
                                 via_d, keepout, outline, stub_width):
                    chosen = (cx, cy, step, radius, angle)
                    break
            if chosen:
                break

        if chosen is None:
            move.reason = (
                f"no legal position on {max_radius_steps} ring(s) of "
                f"{ring_candidates} candidates around pad {move.pad} "
                f"(from {base_radius:.3f}mm out): the via STAYS where it is -- "
                "these are plane stitching vias, and an unresolved via-in-pad "
                "beats a pad orphaned from its plane")
            plan.moves.append(move)
            continue

        move.new_x, move.new_y, move.radius_step, move.radius_mm, move.angle_deg = (
            chosen[0], chosen[1], chosen[2], round(chosen[3], 4), chosen[4])
        move.status = "relocated"
        move.stub_width_mm = round(stub_width, 4)
        move.reason = (f"moved {math.hypot(chosen[0] - via['x'], chosen[1] - via['y']):.3f}mm "
                       f"to {chosen[4]:.0f}deg on ring {chosen[2]}, with a "
                       f"{stub_width:.3f}mm stub back to the pad")
        plan.moves.append(move)

    return plan


def _candidate_ok(cx, cy, pad, via, geom, spec, clearance, via_d, keepout,
                  outline, stub_width) -> bool:
    """Is a via at ``(cx, cy)`` plus its stub back to the pad legal?"""
    from .fabspec import _point_in_pad

    via_r = via_d / 2.0
    net_id = via["net_id"]
    # The stub is graded from the via's ORIGINAL position -- that is where
    # _stub_block actually draws it from, and it is the point already known to
    # sit in the pad's copper, so the pad end of the track needs no clearance
    # argument of its own.
    stub = (via["x"], via["y"], cx, cy)
    stub_r = stub_width / 2.0
    pad_layer = _pad_copper_layer(pad)

    if outline is not None:
        x0, y0, x1, y1 = outline
        if not (x0 + via_r + keepout <= cx <= x1 - via_r - keepout
                and y0 + via_r + keepout <= cy <= y1 - via_r - keepout):
            return False

    for other in geom["pads"]:
        # Never land inside ANY pad -- a same-net pad would just be via-in-pad
        # again, one designator over.
        if _point_in_pad(cx, cy, other):
            return False
        if other.get("net_id") == net_id:
            continue
        if _point_to_pad_mm(cx, cy, other) < via_r + clearance - 1e-6:
            return False
        if _segment_to_pad_mm(stub, other) < stub_r + clearance - 1e-6:
            return False

    for other in geom["vias"]:
        if other["index"] == via["index"] or other["net_id"] == net_id:
            continue
        gap = math.hypot(cx - other["x"], cy - other["y"])
        if gap < via_r + other["size"] / 2.0 + clearance - 1e-6:
            return False
        if _point_to_segment_mm(other["x"], other["y"], *stub) < (
                other["size"] / 2.0 + stub_r + clearance - 1e-6):
            return False

    for seg in geom["segments"]:
        if seg["net_id"] == net_id:
            continue
        line = (seg["x1"], seg["y1"], seg["x2"], seg["y2"])
        half = seg["width"] / 2.0
        if _point_to_segment_mm(cx, cy, *line) < via_r + half + clearance - 1e-6:
            return False
        # The stub lives on ONE layer, so only same-layer tracks constrain it.
        if seg["layer"] == pad_layer and _segment_to_segment_mm(stub, line) < (
                stub_r + half + clearance - 1e-6):
            return False

    return True


# ---------------------------------------------------------------------------
# splicing
# ---------------------------------------------------------------------------

_AT_RE = re.compile(r"(\(at\s+)[-\d.]+\s+[-\d.]+(\s*\))")
_VIA_HEAD_RE = re.compile(r"\n([\t ]*)\(via\b")


def apply_via_relocations(
    pcb_path: str,
    out_path: str,
    plan: ViaRelocationPlan,
    only: set | None = None,
) -> int:
    """Write ``pcb_path`` to ``out_path`` with the accepted moves applied.

    ``only`` limits the applied set to those ``via_index`` values (used by the
    caller's per-via fallback ladder); ``None`` applies every relocatable move.
    Returns the number of vias actually moved.

    Each move rewrites the via's own ``(at ...)`` **in place** -- so via count,
    net, size and drill are untouched -- and appends one ``(segment ...)`` stub
    on the pad's copper layer. The stub's ``uuid`` is a deterministic ``uuid5``
    of (net, endpoints), so re-running produces the same file.
    """
    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    accepted = [m for m in plan.relocatable
                if only is None or m.via_index in only]
    if not accepted:
        if out_path != pcb_path:
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return 0

    spans = _via_spans(text)
    by_index = {m.via_index: m for m in accepted}

    # Rewrite from the END backwards so earlier spans keep their offsets.
    pieces = text
    for index in sorted(by_index, reverse=True):
        if index >= len(spans):
            continue
        start, end = spans[index]
        move = by_index[index]
        block = pieces[start:end]
        new_block, count = _AT_RE.subn(
            lambda m: f"{m.group(1)}{move.new_x:.6f} {move.new_y:.6f}{m.group(2)}",
            block, count=1)
        if not count:
            continue
        pieces = pieces[:start] + new_block + pieces[end:]

    stubs = "".join(_stub_block(m, _segment_indent(text)) for m in accepted)
    insert_at = _last_via_end(text_spans=_via_spans(pieces))
    merged = pieces[:insert_at] + stubs + pieces[insert_at:]

    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(merged)
    return len(accepted)


def _via_spans(text: str) -> list:
    """``[(start, end)]`` of every ``(via ...)`` block, in file order."""
    from .copper_post import _balanced_end

    spans = []
    for match in _VIA_HEAD_RE.finditer(text):
        body_start = match.end() - len("(via")
        spans.append((body_start, _balanced_end(text, body_start)))
    return spans


def _last_via_end(text_spans: list) -> int:
    if not text_spans:
        raise ValueError("board has no (via ...) block; nothing was relocated")
    return text_spans[-1][1]


def _segment_indent(text: str) -> str:
    match = re.search(r"\n([\t ]*)\(segment\b", text)
    return match.group(1) if match else "\t"


def _stub_block(move: ViaMove, indent: str) -> str:
    """The pad-to-via stub track -- the pad's replacement path to the plane."""
    ident = uuid.uuid5(
        _UUID_NAMESPACE,
        f"via-relocate-stub|{move.net_name}|{move.x:.6f}|{move.y:.6f}|"
        f"{move.new_x:.6f}|{move.new_y:.6f}")
    return (
        f"\n{indent}(segment"
        f"\n{indent}\t(start {move.x:.6f} {move.y:.6f})"
        f"\n{indent}\t(end {move.new_x:.6f} {move.new_y:.6f})"
        f"\n{indent}\t(width {move.stub_width_mm:.6f})"
        f"\n{indent}\t(layer \"{move.pad_layer}\")"
        f"\n{indent}\t(net {move.net_id})"
        f"\n{indent}\t(uuid \"{ident}\")"
        f"\n{indent})"
    )
