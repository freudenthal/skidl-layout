"""Is a pad reachable at all, or is the router being blamed for geometry?

⛔⛔⛔ **This module exists because of standing finding 21.** ``route_pair``
answers *"no path"* for two structurally different reasons and the answer looks
identical: KRT's endpoint rescues look a pad up in ``pcb_data.pads_by_net`` by
its **file** coordinates, so a fine-pitch pad that needs the rescue and does not
get it answers *"no path"* after ~2 iterations -- **indistinguishable from a real
one**. S3 measured exactly that: routed 3.8 mm / 2 vias / 3 270 iterations with
the anchor at its file position, ``"no path"`` / 5 001 iterations with it parked
30 mm away, on the identical stamped map with the identical question.

⭐ **A router failing is evidence about the ROUTER. This is evidence about the
board.** Two verdicts that license different actions:

    PASSABLE -> a track of this width fits. If the router said no, that is a
                ROUTER finding (grid, ordering, rip-up, a missing rescue) and
                moving the part is the wrong lever.
    CAGED    -> no route exists at any grid. This one is geometry, and the
                placement is the only thing that can fix it.

No physics, no heuristics, three classical steps:

  1. **Rasterise foreign copper, one grid per clearance class.** Clearance is
     PAIRWISE, so one grid at one clearance is already the wrong model on any
     board carrying a second class.
  2. **Euclidean distance transform per class**, then
     ``slack(p) = 2 * min_k(dist_k(p) - clearance_k)`` -- the widest track that
     legally fits at ``p``. Linear time, exact Euclidean metric.
  3. **Widest path is a Kruskal problem and Kruskal solves it EXACTLY.** Add
     cells in descending slack under union-find; the slack at which the seed and
     the rest of the net first join **is** the bottleneck. No search order, no
     grid alignment, no heuristic -- which is why it answers a question a
     gridded A\\* cannot.

⭐ **PROVENANCE, and it is not ours.** The algorithm, the two verdicts and the
three design notes below (``_open_room``'s sentinel, ``_own_slack``'s ordering,
the per-class grids) are a port of ``placement/reachability.py`` from
**KiCadRoutingTools**, branch ``placement`` (Rob Boerman, 2026-08-03), read at
``fe6db00``. ⛔ It is **reimplemented here rather than imported**: that module
lives on an unmerged branch, and the standing escalation order says *solve it in
``skidl-layout``* (rung 1) before reaching into KRT. **KRT divergence stays
ZERO.** Their own validation, quoted rather than claimed: a pad the router failed
on at every grid measured PASSABLE by **+5.5 um** against a hand-derived 6.4 um
throat, and a genuinely enclosed pad measured CAGED.

⛔ **What this is NOT:** it is not a judge and nothing it returns may become one
(overview 7.4). It is a *disqualifier's disqualifier* -- it tells you whether a
route failure is worth acting on. It does not rank placements, it does not score,
and it says nothing about whether a board is *good*.

⚠ Needs scipy, checked once and loudly: a silently-degraded reachability answer
is worse than none, because the answer is used to say a board is unroutable.

⭐ Leaf module with **one named consumer**: ``construct.py``'s blame column
(S5B), which calls :func:`explain_route_failure` **on failures only** and
**never steers on the verdict** -- overview §5.8, *a stage that both adds an
instrument and changes the loop it measures can attribute neither*.
``tests/test_reachability.py``'s ``REACHABILITY_CONSUMERS`` pins the list, and a
second guard pins that every permitted consumer is **itself** a leaf.
⚠ This line used to read *"nothing imports it yet"*; the *"yet"* was load-bearing
and the full suite is what said so.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

#: ⛔ The raster step. 10 um resolves a 0.5 mm-pitch lattice to 50 cells a
#: pitch, which is what makes a micron-scale throat measurement meaningful.
#: ⚠ Cost is quadratic in the view, which is why the view is LOCAL by default.
DEFAULT_STEP_MM = 0.01

#: ⛔ The default half-width of the view around the seed. The answer is LOCAL --
#: a throat is a property of the copper within a few millimetres -- and
#: rasterising a whole board at 10 um to find one costs memory for nothing.
DEFAULT_MARGIN_MM = 4.0

#: ⛔⛔ **THREE verdicts, and the third is a CORRECTION to the source.** KRT's
#: version has two, and derives ``caged`` from ``bottleneck is None`` -- but
#: ``bottleneck`` is also ``None`` when the view simply did not contain the rest
#: of the net, so a view too small to answer the question **reports CAGED**.
#: Measured here on `lt3758_boost_hv` at ``margin_mm=1.0``: verdict CAGED,
#: note *"no other island of this net is inside the view"*. That is standing
#: finding 1 wearing a geometry hat -- *an instrument that observed nothing is
#: indistinguishable from one that found everything* -- and it is the more
#: dangerous direction, because CAGED is the verdict that blames the placement.
#: ⭐ ``UNDETERMINED`` is therefore its own answer, and :attr:`Reachability.caged`
#: is **False** on it, so no consumer can read a not-asked as a geometry finding.
VERDICTS: tuple[str, ...] = ("PASSABLE", "CAGED", "UNDETERMINED")


class ReachabilityError(RuntimeError):
    """⛔ Raised rather than returning a worse answer (standing finding 1)."""


class ScipyRequired(ReachabilityError):
    """scipy is missing. ⛔ Do NOT substitute a coarser distance metric."""


def _require_scipy():
    try:
        from scipy import ndimage
    except ImportError as exc:                              # pragma: no cover
        raise ScipyRequired(
            "reachability needs scipy (distance_transform_edt, label) and this "
            "interpreter has none. Install it, or run from one that has it -- "
            "⛔ do NOT substitute a coarser distance metric: this answer is "
            "used to say a board is unroutable, and an approximate one is "
            "worse than none.") from exc
    return ndimage


def _open_room(view) -> float:
    """Clearance-adjusted room for a cell with no foreign copper within reach.

    ⛔ Derived from the view, never a constant, and that is not cosmetic: a
    fixed sentinel that happens to exceed the own-copper value makes free space
    sort **above** own copper in the Kruskal order, so the whole open board
    unions together before the seed activates and the bottleneck comes back as
    the sentinel itself -- *a number that looks like a measurement and is not*.
    """
    return max(view[2] - view[0], view[3] - view[1])


def _own_slack(view) -> float:
    """Own-net copper is a GIVEN, not a constraint.

    The pad and its existing tracks are already there and are traversable
    whatever the local slack says -- a pad can score negative slack at its own
    centre under a rule forbidding a *new* track there, which says nothing about
    the copper already present. Strictly above every free-space value, so own
    copper is processed first and can never itself be the reported bottleneck.
    """
    return 2.0 * _open_room(view) + 1.0


# --------------------------------------------------------------------------- #
# rasterisation
# --------------------------------------------------------------------------- #

def _capsule(occ, x0, y0, x1, y1, r, view, step) -> None:
    """Stamp a thick segment (a via is a zero-length one)."""
    vx0, vy0 = view[0], view[1]
    h, w = occ.shape
    gx0 = max(0, int((min(x0, x1) - r - vx0) / step))
    gx1 = min(w, int((max(x0, x1) + r - vx0) / step) + 2)
    gy0 = max(0, int((min(y0, y1) - r - vy0) / step))
    gy1 = min(h, int((max(y0, y1) + r - vy0) / step) + 2)
    if gx0 >= gx1 or gy0 >= gy1:
        return
    ys, xs = np.mgrid[gy0:gy1, gx0:gx1]
    px = vx0 + (xs + 0.5) * step
    py = vy0 + (ys + 0.5) * step
    dx, dy = x1 - x0, y1 - y0
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        dist = np.hypot(px - x0, py - y0)
    else:
        t = np.clip(((px - x0) * dx + (py - y0) * dy) / length2, 0.0, 1.0)
        dist = np.hypot(px - (x0 + t * dx), py - (y0 + t * dy))
    occ[gy0:gy1, gx0:gx1] |= (dist <= r)


def _rect(occ, cx, cy, sx, sy, view, step, rot_deg=0.0) -> None:
    """Stamp a pad rectangle, honouring a residual non-orthogonal tilt.

    ⚠ ``size_x``/``size_y`` are **board space with the rotation baked in**
    (standing finding 21's third clause), so ``rot_deg`` here is the *residual*
    tilt only -- non-zero on a pad at a non-orthogonal angle, 0 otherwise.
    """
    vx0, vy0 = view[0], view[1]
    h, w = occ.shape
    reach = math.hypot(sx, sy) / 2.0
    gx0 = max(0, int((cx - reach - vx0) / step))
    gx1 = min(w, int((cx + reach - vx0) / step) + 2)
    gy0 = max(0, int((cy - reach - vy0) / step))
    gy1 = min(h, int((cy + reach - vy0) / step) + 2)
    if gx0 >= gx1 or gy0 >= gy1:
        return
    ys, xs = np.mgrid[gy0:gy1, gx0:gx1]
    px = vx0 + (xs + 0.5) * step - cx
    py = vy0 + (ys + 0.5) * step - cy
    if abs(rot_deg) > 1e-9:
        th = math.radians(-rot_deg)
        px, py = (px * math.cos(th) - py * math.sin(th),
                  px * math.sin(th) + py * math.cos(th))
    occ[gy0:gy1, gx0:gx1] |= ((np.abs(px) <= sx / 2) & (np.abs(py) <= sy / 2))


def _pad_on_layer(pad, layer: str) -> bool:
    if getattr(pad, "drill", 0) and pad.drill > 0:
        return True                          # ⛔ a barrel blocks every layer
    layers = getattr(pad, "layers", None) or []
    return layer in layers or "*.Cu" in layers


def slack_field(pcb, target_net_id: int, layer: str, view, base_clearance: float,
                net_clearances: Optional[Dict[int, float]] = None,
                step: float = DEFAULT_STEP_MM):
    """``(slack, own, room)`` for one copper layer.

    ``net_clearances`` is ``{net_id: clearance_mm}`` -- ⛔ **one grid per
    clearance class**, because clearance is pairwise and a single grid at a
    single number is the wrong model the moment a board carries a second class.
    Nets absent from the map use ``base_clearance``.
    """
    ndimage = _require_scipy()
    w = int(math.ceil((view[2] - view[0]) / step))
    h = int(math.ceil((view[3] - view[1]) / step))
    if w <= 0 or h <= 0:
        raise ReachabilityError(
            f"empty view {view!r}: a field with no cells cannot observe "
            f"anything, and an instrument that observes nothing must raise")
    per_class: Dict[float, np.ndarray] = {}
    own = np.zeros((h, w), dtype=bool)
    clearances = dict(net_clearances or {})

    def grid_for(net_id):
        c = round(float(clearances.get(net_id, base_clearance)), 4)
        return per_class.setdefault(c, np.zeros((h, w), dtype=bool))

    for seg in pcb.segments:
        if seg.layer != layer:
            continue
        grid = own if seg.net_id == target_net_id else grid_for(seg.net_id)
        _capsule(grid, seg.start_x, seg.start_y, seg.end_x, seg.end_y,
                 seg.width / 2.0, view, step)
    for via in pcb.vias:                      # ⛔ a via blocks every layer
        grid = own if via.net_id == target_net_id else grid_for(via.net_id)
        _capsule(grid, via.x, via.y, via.x, via.y, via.size / 2.0, view, step)
    for footprint in pcb.footprints.values():
        for pad in footprint.pads:
            if getattr(pad, "pad_type", "") == "np_thru_hole":
                # ⛔ NPTH carries NO copper even when `layers` lists `*.Cu`
                # (standing finding 15(c)): only its hole matters, and that is
                # a drill constraint rather than a clearance one.
                continue
            if not _pad_on_layer(pad, layer):
                continue
            grid = own if pad.net_id == target_net_id else grid_for(pad.net_id)
            _rect(grid, pad.global_x, pad.global_y, pad.size_x, pad.size_y,
                  view, step, getattr(pad, "rect_rotation", 0.0) or 0.0)

    # room(p) = min_k(dist_k(p) - clearance_k), the clearance-adjusted room at
    # p. A track of width t fits iff room >= t/2, so track slack is 2*room.
    room = np.full((h, w), np.inf)
    for clearance, occ in per_class.items():
        if not occ.any():
            continue
        room = np.minimum(
            room, ndimage.distance_transform_edt(~occ) * step - clearance)
    room[np.isinf(room)] = _open_room(view)
    room = np.minimum(room, _open_room(view))
    for occ in per_class.values():
        room[occ] = -1.0
    slack = 2.0 * room
    slack[own] = _own_slack(view)
    return slack, own, room


# --------------------------------------------------------------------------- #
# widest path
# --------------------------------------------------------------------------- #

def widest_path(slacks, targets, via_ok, seed_xy, seed_layer, view,
                step: float) -> Optional[float]:
    """Kruskal widest path over a multi-layer grid; mm, or ``None``.

    Nodes are ``(cell, layer)``. In-layer edges join adjacent **active** cells;
    a via edge joins two layers at one cell and is permitted only where a via of
    the given diameter is legal on **both** -- a legality that does not depend
    on the threshold, so it composes with Kruskal unchanged.

    Returns the largest ``t`` such that a path exists using only cells of slack
    ``>= t``. ⭐ Exact for the raster. ``None`` means no positive-slack path
    exists at any width.
    """
    layer_count = len(slacks)
    h, w = slacks[0].shape
    n = h * w
    sx = int((seed_xy[0] - view[0]) / step)
    sy = int((seed_xy[1] - view[1]) / step)
    if not (0 <= sx < w and 0 <= sy < h):
        raise ReachabilityError(f"seed {seed_xy!r} is outside the view {view!r}")
    seed = seed_layer * n + sy * w + sx

    flat = np.concatenate([s.reshape(-1) for s in slacks])
    tgt_flat = np.concatenate([t.reshape(-1) for t in targets])
    via_flat = via_ok.reshape(-1)

    order = np.argsort(flat, kind="stable")[::-1]
    target_node = layer_count * n
    parent = np.arange(layer_count * n + 1, dtype=np.int64)
    active = np.zeros(layer_count * n, dtype=bool)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for idx in order:
        idx = int(idx)
        value = float(flat[idx])
        if value < 0.0:                       # ⛔ never route through copper
            break
        active[idx] = True
        lay, cell = divmod(idx, n)
        if tgt_flat[idx]:
            union(idx, target_node)
        y, x = divmod(cell, w)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w:
                j = lay * n + ny * w + nx
                if active[j]:
                    union(idx, j)
        if via_flat[cell]:
            for other in range(layer_count):
                if other != lay and active[other * n + cell]:
                    union(idx, other * n + cell)
        if active[seed] and find(seed) == find(target_node):
            return float(value)
    return None


@dataclass(frozen=True)
class Reachability:
    """What the free space says about one pad reaching the rest of its net."""

    net: str
    net_id: int
    seed: Tuple[float, float]
    layers: Tuple[str, ...]
    step_mm: float
    track_mm: float
    via_mm: float
    view: Tuple[float, float, float, float]
    #: ``None`` = no positive-slack path exists at any width.
    bottleneck_mm: Optional[float]
    target_cells: int
    via_legal_fraction: float
    grid: Tuple[int, int]
    open_room_mm: float
    #: ⛔ Every number carries its source (the house rule since S1).
    track_source: str = ""
    clearance_source: str = ""
    note: str = ""

    @property
    def wide_open(self) -> bool:
        """The path never came near foreign copper inside the view.

        ⚠ Reported as a flag rather than as a number, because the number would
        be *the view's own size* -- a property of what was asked, not of the
        board.
        """
        return (self.bottleneck_mm is not None
                and self.bottleneck_mm >= 2.0 * self.open_room_mm - 1e-9)

    @property
    def determined(self) -> bool:
        """Did the view actually contain the question?

        ⛔ ``False`` when no other island of the net lies inside the view, so
        there was nothing to reach and ``bottleneck_mm`` is ``None`` for a
        reason that has **nothing to do with the board**.
        """
        return self.target_cells > 0

    @property
    def caged(self) -> bool:
        """⛔ **False when :attr:`determined` is False.** See :data:`VERDICTS`:
        a question that was never asked must never answer *"the geometry is
        impossible"*."""
        if not self.determined:
            return False
        return (self.bottleneck_mm is None
                or self.bottleneck_mm < self.track_mm)

    @property
    def verdict(self) -> str:
        if not self.determined:
            return "UNDETERMINED"
        return "CAGED" if self.caged else "PASSABLE"

    @property
    def margin_um(self) -> Optional[float]:
        """How far from the verdict boundary, in microns. Signed."""
        if self.bottleneck_mm is None:
            return None
        return 1000.0 * (self.bottleneck_mm - self.track_mm)

    def to_dict(self) -> dict:
        return {"net": self.net, "net_id": self.net_id,
                "seed": list(self.seed), "layers": list(self.layers),
                "step_mm": self.step_mm, "track_mm": self.track_mm,
                "via_mm": self.via_mm,
                "bottleneck_mm": (None if self.bottleneck_mm is None
                                  else round(self.bottleneck_mm, 5)),
                "wide_open": self.wide_open, "verdict": self.verdict,
                "determined": self.determined,
                "margin_um": (None if self.margin_um is None
                              else round(self.margin_um, 2)),
                "target_cells": self.target_cells,
                "via_legal_fraction": round(self.via_legal_fraction, 4),
                "grid": list(self.grid),
                "view": [round(v, 3) for v in self.view],
                "track_source": self.track_source,
                "clearance_source": self.clearance_source,
                "note": self.note}

    def format_text(self) -> str:
        lines = [f"net        {self.net} from ({self.seed[0]}, {self.seed[1]}) "
                 f"on {self.layers[0]}",
                 f"layers     {list(self.layers)}   via {self.via_mm} mm legal "
                 f"at {100.0 * self.via_legal_fraction:.1f}% of cells",
                 f"grid       {self.grid[0]}x{self.grid[1]} @ {self.step_mm} mm",
                 f"track      {self.track_mm} mm ({self.track_source})",
                 f"clearance  ({self.clearance_source})"]
        if self.note:
            lines.append(f"note       {self.note}")
        if not self.determined:
            lines.append("BOTTLENECK not measured: the view held no other "
                         "island of this net")
            lines.append("VERDICT    UNDETERMINED -- widen margin_mm. ⛔ This "
                         "is NOT a CAGED finding; nothing was asked of the "
                         "board.")
        elif self.bottleneck_mm is None:
            lines.append("BOTTLENECK none: no positive-slack path exists at "
                         "any grid")
            lines.append("VERDICT    CAGED for any track width")
        elif self.wide_open:
            lines.append(f"BOTTLENECK >= {2.0 * self.open_room_mm:.2f} mm -- the "
                         f"path never approached foreign copper inside the "
                         f"view, so this is bounded by the VIEW, not by the "
                         f"board. There is no throat here to measure.")
            lines.append(f"VERDICT    PASSABLE at track {self.track_mm} mm "
                         f"(nothing is in the way)")
        else:
            lines.append(f"BOTTLENECK {self.bottleneck_mm:.4f} mm (the widest "
                         f"track that reaches the net)")
            lines.append(f"VERDICT    {self.verdict} at track "
                         f"{self.track_mm} mm (margin {self.margin_um:+.1f} um)")
        return "\n".join(lines)


def track_and_clearance(fab) -> tuple:
    """``(track_mm, clearance_mm, track_source, clearance_source)``.

    ⛔ **Every number carries its source.** The clearance is the FabSpec's
    ``min_clearance_mm`` -- the fab floor a route step actually uses -- and not
    the design ``clearance_mm``: grading a throat at the looser number
    manufactures a CAGED verdict on copper that routes.
    """
    track = float(fab.track_width_mm)
    clearance = float(fab.min_clearance_mm)
    return (track, clearance,
            f"FabSpec({fab.name}).track_width_mm",
            f"FabSpec({fab.name}).min_clearance_mm")


def pad_reachability(pcb, seed_xy, net_name: Optional[str] = None,
                     net_id: Optional[int] = None, *, fab=None,
                     layers: Optional[Sequence[str]] = None, view=None,
                     track_mm: Optional[float] = None,
                     via_mm: Optional[float] = None,
                     base_clearance: Optional[float] = None,
                     net_clearances: Optional[Dict[int, float]] = None,
                     step: float = DEFAULT_STEP_MM,
                     margin_mm: float = DEFAULT_MARGIN_MM) -> Reachability:
    """Can a track of ``track_mm`` get from ``seed_xy`` to the rest of its net?

    Pass ``fab`` (a ``FabSpec``) and the widths derive themselves with their
    sources recorded; pass the numbers explicitly to override. ⛔ Passing
    neither raises rather than guessing.

    ``view`` defaults to a ``margin_mm`` box around the seed. ⚠ A view that
    excludes the rest of the net is **reported in ``note``**, never silently
    answered -- that is the observes-nothing failure wearing a geometry hat.
    """
    ndimage = _require_scipy()
    # ⛔ Track which numbers the CALLER supplied before any defaulting, so the
    # recorded source is a fact rather than an inference. (Comparing the
    # resolved value against the default would call an explicit 0.3 "FabSpec".)
    t_src = "caller" if track_mm is not None else ""
    c_src = "caller" if base_clearance is not None else ""
    v_src = "caller" if via_mm is not None else ""
    if fab is not None:
        d_track, d_clear, fab_t_src, fab_c_src = track_and_clearance(fab)
        if track_mm is None:
            track_mm, t_src = d_track, fab_t_src
        if base_clearance is None:
            base_clearance, c_src = d_clear, fab_c_src
        if via_mm is None:
            via_mm, v_src = (float(fab.via_size_mm),
                             f"FabSpec({fab.name}).via_size_mm")
    if track_mm is None or base_clearance is None or via_mm is None:
        raise ReachabilityError(
            "pad_reachability needs a FabSpec (fab=) or explicit track_mm / "
            "base_clearance / via_mm. ⛔ It will not guess: a throat graded at "
            "a guessed clearance manufactures or hides a CAGED verdict.")
    track_mm, base_clearance, via_mm = (float(track_mm), float(base_clearance),
                                        float(via_mm))
    del v_src                     # the via's source is implied by the same fab

    if net_id is None:
        net_id = next((i for i, n in pcb.nets.items() if n.name == net_name),
                      None)
    if net_id is None:
        raise ReachabilityError(f"no net named {net_name!r} on this board")
    name = pcb.nets[net_id].name if net_id in pcb.nets else str(net_id)

    if layers is None:
        layers = tuple(pcb.board_info.copper_layers or ())
        if not layers:
            # ⛔ Open issue 10: KRT parses every board this stack writes as ZERO
            # copper layers, and an in-process consumer then gets a zero-layer
            # map and "Cannot determine endpoints" on every route -- which reads
            # exactly like "this board has nothing to route".
            raise ReachabilityError(
                "this board reports NO copper layers. That is open issue 10 "
                "(kicad_parser.extract_layers' regex), not an empty board -- "
                "pass layers= explicitly and assert it non-empty.")
    layers = tuple(layers)
    if view is None:
        view = (seed_xy[0] - margin_mm, seed_xy[1] - margin_mm,
                seed_xy[0] + margin_mm, seed_xy[1] + margin_mm)
    view = tuple(float(v) for v in view)

    slacks, owns, rooms = [], [], []
    for layer in layers:
        s, o, r = slack_field(pcb, net_id, layer, view, base_clearance,
                              net_clearances, step)
        slacks.append(s)
        owns.append(o)
        rooms.append(r)
    via_ok = np.ones_like(rooms[0], dtype=bool)
    for room in rooms:
        via_ok &= (room >= via_mm / 2.0)

    # The seed pad IS own-net copper, so "reach the net" would be trivially true
    # unless the pad's island is separated from the rest of the net. Label per
    # layer, then stitch the labels through the net's own vias.
    labels, counts = [], []
    for own in owns:
        lab, k = ndimage.label(own)
        labels.append(lab)
        counts.append(k)
    offsets, running = [0], 0
    for k in counts:
        running += k
        offsets.append(running)
    parent = list(range(running + 1))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for via in pcb.vias:
        if via.net_id != net_id:
            continue
        cx = int((via.x - view[0]) / step)
        cy = int((via.y - view[1]) / step)
        if not (0 <= cx < labels[0].shape[1] and 0 <= cy < labels[0].shape[0]):
            continue
        ids = [offsets[i] + int(labels[i][cy, cx]) for i in range(len(layers))
               if labels[i][cy, cx]]
        for k in range(1, len(ids)):
            union(ids[0], ids[k])

    sx = int((seed_xy[0] - view[0]) / step)
    sy = int((seed_xy[1] - view[1]) / step)
    if not (0 <= sx < labels[0].shape[1] and 0 <= sy < labels[0].shape[0]):
        raise ReachabilityError(f"seed {seed_xy!r} is outside the view {view!r}")
    seed_label = int(labels[0][sy, sx])
    if seed_label == 0:
        raise ReachabilityError(
            f"({seed_xy[0]}, {seed_xy[1]}) is not on {name}'s copper on "
            f"{layers[0]} -- pass the pad CENTRE, and check the layer")
    seed_root = find(offsets[0] + seed_label)

    targets = []
    for i, own in enumerate(owns):
        t = np.zeros_like(own)
        for lab in range(1, counts[i] + 1):
            if find(offsets[i] + lab) != seed_root:
                t |= (labels[i] == lab)
        targets.append(t)
    target_cells = sum(int(t.sum()) for t in targets)

    note = ""
    if target_cells == 0:
        note = ("no other island of this net is inside the view -- this pad is "
                "already joined to everything nearby. Widen margin_mm, or this "
                "is not a reachability question")
    bottleneck = (None if target_cells == 0
                  else widest_path(slacks, targets, via_ok, seed_xy, 0, view,
                                   step))
    return Reachability(
        net=name, net_id=net_id, seed=(seed_xy[0], seed_xy[1]), layers=layers,
        step_mm=step, track_mm=track_mm, via_mm=via_mm, view=view,
        bottleneck_mm=bottleneck, target_cells=target_cells,
        via_legal_fraction=float(via_ok.mean()),
        grid=(slacks[0].shape[1], slacks[0].shape[0]),
        open_room_mm=_open_room(view), track_source=t_src,
        clearance_source=c_src, note=note)


def explain_route_failure(pcb, seed_xy, *, fab, net_name=None, net_id=None,
                          **kwargs) -> dict:
    """⭐ The question standing finding 21 says we could not answer.

    Given a pad the router refused, return ``{"verdict", "blame", ...}`` where
    ``blame`` is **"router"** when the geometry is PASSABLE (the failure is the
    router's -- a grid, an ordering, a missing endpoint rescue) and
    **"geometry"** when it is CAGED (the placement is the only lever).

    ⛔ It answers *"is this failure worth acting on"*, not *"is this placement
    good"*. Nothing here may become a judge.
    """
    result = pad_reachability(pcb, seed_xy, net_name=net_name, net_id=net_id,
                              fab=fab, **kwargs)
    blob = result.to_dict()
    # ⛔ THREE answers, never two. "unknown" is not "router": a view that never
    # contained the rest of the net has said nothing about either.
    blob["blame"] = ("unknown" if not result.determined
                     else "geometry" if result.caged else "router")
    return blob
