# -*- coding: utf-8 -*-
"""A live routing session: a board you can add a part to, route against, roll back.

⛔⛔⛔ **THIS IS A PROXY, AND EVERY NUMBER IT RETURNS IS A PROXY NUMBER.**
:meth:`RouteSession.route_pair` asks one question -- *"can this pad reach that
pad, how long, how hard"* -- against a **static** obstacle map on a **partial**
board. It is optimistic in two directions that cannot be argued away:

* parts that are not yet stamped cannot block, so a pair that routes here may
  not route on the finished board;
* it is a **pair**, not the net's real topology, and the real router rips up,
  reroutes, and routes every net together.

⛔ **Nothing this module returns may be promoted to a judge.** The frozen judge
stays :func:`skidl_layout.ratnest.analyse_board`, and a final grade stays a full
route through :mod:`skidl_layout.krt`. What the session is *for* is the inner
loop of a constructive placer -- a question cheap enough to ask thousands of
times -- and how well it agrees with the real router is a measured quantity, not
an assumption.

**Why it is buildable at all.** KRT's Rust obstacle map is **refcounted**
(``rust_router/src/obstacle_map.rs:512-633``: ``blocked_cells[layer]`` is a
key->count map and the bitmap flips only on the 0->1 and 1->0 transitions), so
balanced add/remove is *exact* and two parts whose clearance halos overlap can be
stamped and removed independently. That is the invariant the whole loop rests on
and this module **asserts** it on every removal rather than assuming it.

Four defects are designed around rather than discovered again -- see the module
constants and the raising checks below:

1. ⛔⛔⛔ ``board_info.copper_layers`` parses **empty** on every board this stack
   writes (KRT open issue 10). In-process there is no ``DEFAULT_LAYERS``
   fallback, so a zero-layer map routes nothing while looking like a clean
   "nothing to route". **``layers`` is required and asserted non-empty.**
2. ⛔⛔ KRT's own ``_RecordingObstacles`` records **track cells only** and leaks
   via refcounts monotonically. :class:`_FullRecorder` records both.
3. ⛔⛔ ``static_base=True`` stamps into a permanent bitmap with **no refcount
   and no removal**, so a session built that way could never un-stamp anything.
   ``route.py`` uses it; we must not.
4. ⛔ The **first** in-process route call in a fresh process costs ~68 ms and can
   return a different answer than the ~1.5 ms steady state. :meth:`from_board`
   performs and discards one warm-up route and records that it did. ⚠ Do **not**
   write a test asserting the first call is slower -- in a process that has
   already routed anything the warm-up costs 2-4 ms and returns the identical
   iteration count.

⛔ **This module is a LEAF.** Nothing in the placement engine imports it, and it
must not be added to any ``__init__`` re-export the engine reaches -- an
import-time KRT dependency inside the engine would break the "KRT absent" path.
"""

from __future__ import annotations

import inspect
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = ["RouteSession", "PairResult", "StampToken", "Snapshot",
           "RollbackError", "SessionError"]

#: The KRT commit this module's private-API dependency was pinned against.
#: ⚠ Named in the error message on purpose, so a future re-sync fails loudly
#: rather than subtly. This is the **only** private-API dependency this repo has
#: taken and it is recorded in the KRT standing-rules section.
KRT_PINNED_SHA = "cae82e3"

#: ⛔ The private names. ``_add_pad_obstacle`` we CALL; ``_RecordingObstacles``
#: we deliberately do **not** use (defect 2) and only check for, so that its
#: disappearance is a signal that the recording seam moved.
_PRIVATE_KRT_NAMES = ("_add_pad_obstacle", "_RecordingObstacles")


class SessionError(RuntimeError):
    """Anything the session refuses to guess about."""


class RollbackError(SessionError):
    """⛔ The census did not return to its recorded value.

    This is the one failure this module must never paper over. A session that
    cannot roll back exactly is not a session, and a "rebuild the map instead"
    fallback would silently cost ~0.076 s per trial while hiding the defect.
    """


@dataclass(frozen=True)
class PairResult:
    """One pad-pair question and its answer. ⛔ A proxy number -- see the module
    docstring."""

    routed: bool
    length_mm: float | None
    iterations: int
    vias: int
    path_cells: int
    elapsed_s: float
    failure: str | None = None


@dataclass(frozen=True)
class StampToken:
    """What :meth:`RouteSession.add_part` stamped, and what it must undo.

    Carries the **pre-stamp census** so the removal is checked rather than
    trusted.
    """

    part_key: str
    cells: Any                       # np.ndarray (n, 3) int32
    vias: Any                        # np.ndarray (m, 2) int32
    pre_census: tuple
    #: The census immediately after this stamp -- recorded for diagnostics.
    #: ⛔⛔ **NOT a state identity, and using it as one is a measured mistake.**
    #: The census is a five-number summary, so two genuinely different maps can
    #: share it: gate ``C2``'s soak produced a state whose census equalled
    #: ``CIN2``'s ``post_census`` after many intervening operations, the LIFO
    #: test believed it, and the exact-``pre_census`` assertion then failed on a
    #: map that was behaving correctly (the soak still landed on the base census
    #: exactly). ⭐ **LIFO is a STRUCTURAL fact, so it is tracked structurally**
    #: -- see ``ops_at_stamp``.
    post_census: tuple = ()
    #: The session's operation counter at stamp time. A removal is a true LIFO
    #: undo iff this token is last in the token list **and** no add or remove
    #: has happened since -- both exact, neither inferable from a summary.
    ops_at_stamp: int = -1
    pads: int = 0
    #: ⭐ ``((net_name, cells, vias), ...)`` -- the SAME stamp, recorded per pad
    #: as well as merged. This is what makes :meth:`RouteSession.lift_net`
    #: possible from one stamping path: "remove this net's pads" is a selection
    #: over records we already made, never a second way of computing them.
    per_pad: tuple = ()

    @property
    def cell_count(self) -> int:
        return int(len(self.cells))

    @property
    def via_count(self) -> int:
        return int(len(self.vias))


@dataclass(frozen=True)
class Snapshot:
    """The session's state as a value: the census plus the live token list."""

    census: tuple
    tokens: tuple = ()
    routes: int = 0


class _FullRecorder:
    """Records **both** blocked cells and blocked vias, and forwards the rest.

    ⛔⛔ **Why this exists rather than KRT's ``_RecordingObstacles``**
    (``obstacle_map.py:2347``): KRT's proxy records ``add_blocked_cells_batch``
    and ``add_blocked_cell`` only. Stamping one 2-pad part through it leaks
    ~971 via-cell and ~39 via refcounts that nothing ever removes, so a rollback
    built on it drifts **monotonically** -- and the drift is invisible until the
    map has silently closed a corridor. Fifteen lines here, and no KRT edit.
    """

    def __init__(self, real):
        self._real = real
        self.cells: list = []
        self.vias: list = []

    def add_blocked_cells_batch(self, cells):
        import numpy as np

        self.cells.append(np.array(cells, copy=True))
        self._real.add_blocked_cells_batch(cells)

    def add_blocked_cell(self, gx, gy, layer):
        import numpy as np

        self.cells.append(np.array([[gx, gy, layer]], dtype=np.int32))
        self._real.add_blocked_cell(gx, gy, layer)

    def add_blocked_vias_batch(self, vias):
        import numpy as np

        self.vias.append(np.array(vias, copy=True))
        self._real.add_blocked_vias_batch(vias)

    def add_blocked_via(self, gx, gy):
        import numpy as np

        self.vias.append(np.array([[gx, gy]], dtype=np.int32))
        self._real.add_blocked_via(gx, gy)

    def merged(self):
        import numpy as np

        cells = (np.vstack(self.cells).astype(np.int32) if self.cells
                 else np.empty((0, 3), dtype=np.int32))
        vias = (np.vstack(self.vias).astype(np.int32) if self.vias
                else np.empty((0, 2), dtype=np.int32))
        return cells, vias

    def __getattr__(self, name):
        return getattr(self._real, name)


class _CellsOnlyRecorder(_FullRecorder):
    """⛔ **Deliberately via-unaware -- the regression control, not a tool.**

    Gate ``C2`` asserts that a session built on this one **leaks**, so a future
    revert to KRT's track-only proxy is caught by a test rather than by a
    corridor that quietly closed.
    """

    def add_blocked_vias_batch(self, vias):
        self._real.add_blocked_vias_batch(vias)

    def add_blocked_via(self, gx, gy):
        self._real.add_blocked_via(gx, gy)


def _import_krt(krt_dir=None):
    """Import KRT's routing modules in-process, the way ``ratnest`` already does.

    ⛔ Pins the two private names with a signature check that names the SHA
    (plan trap 6): a future re-sync then fails with a message a reader can act
    on, instead of an ``AttributeError`` three frames deep.
    """
    import sys

    from .krt import KrtNotFoundError, find_krt

    resolved = find_krt(krt_dir)
    if resolved is None:
        raise KrtNotFoundError(
            "KiCadRoutingTools not found (set SKIDL_LAYOUT_KRT_DIR or place a "
            "built checkout at the workspace sibling KiCadRoutingTools/); "
            "route_session needs it to build and mutate an obstacle map")
    if resolved not in sys.path:
        sys.path.insert(0, resolved)

    import kicad_parser                                     # noqa: PLC0415
    import net_queries                                      # noqa: PLC0415
    import obstacle_map                                     # noqa: PLC0415
    import routing_utils                                    # noqa: PLC0415
    import single_ended_routing                             # noqa: PLC0415
    from routing_config import GridCoord, GridRouteConfig   # noqa: PLC0415

    for name in _PRIVATE_KRT_NAMES:
        if not hasattr(obstacle_map, name):
            raise SessionError(
                f"⛔ KRT's obstacle_map has no {name!r}. route_session.py takes "
                f"a deliberate private-API dependency on it, pinned against KRT "
                f"{KRT_PINNED_SHA}; the recording/stamping seam has moved and "
                f"this module must be re-read against the new one rather than "
                f"patched around. (KRT divergence must stay ZERO: fix it here, "
                f"not there.)")
    params = inspect.signature(obstacle_map._add_pad_obstacle).parameters
    for needed in ("obstacles", "pad", "coord", "layer_map", "config"):
        if needed not in params:
            raise SessionError(
                f"⛔ KRT's _add_pad_obstacle lost its {needed!r} parameter. "
                f"Pinned against KRT {KRT_PINNED_SHA}; re-read the signature "
                f"before using this module.")
    return {
        "root": resolved, "kicad_parser": kicad_parser,
        "obstacle_map": obstacle_map, "net_queries": net_queries,
        "routing_utils": routing_utils,
        "single_ended_routing": single_ended_routing,
        "GridRouteConfig": GridRouteConfig, "GridCoord": GridCoord,
    }


@dataclass
class RouteSession:
    """A parsed board plus a live, refcounted obstacle map.

    Build it with :meth:`from_board`; the constructor is not the public seam.
    """

    pcb: Any
    config: Any
    coord: Any
    layer_map: dict
    obstacles: Any
    krt: dict
    pcb_path: str = ""
    layers: tuple = ()
    parsed_layers: tuple = ()
    unstamped_nets: tuple = ()
    liftable_nets: tuple = ()
    warmup: dict = field(default_factory=dict)
    _tokens: list = field(default_factory=list)
    _ops: int = 0
    _routes: int = 0

    # -- construction ------------------------------------------------------- #
    @classmethod
    def from_board(cls, pcb_path: str, *, layers: Sequence[str],
                   clearance_mm: float, track_width_mm: float,
                   via_size_mm: float, via_drill_mm: float,
                   grid_step_mm: float = 0.1,
                   unstamped_nets: Sequence[str] | None = None,
                   liftable_nets: Sequence[str] | None = None,
                   krt_dir: str | None = None) -> "RouteSession":
        """Parse ``pcb_path`` and build a **removable** obstacle map over it.

        ⛔⛔ ``layers`` is REQUIRED and asserted non-empty. ``route.py`` gets a
        ``DEFAULT_LAYERS`` fallback that in-process callers do not, and
        ``board_info.copper_layers`` parses ``[]`` on every board this stack
        writes (KRT open issue 10) -- so a session that trusted the board would
        route nothing and say so in the voice of a board with nothing to route.
        The parsed list is recorded next to the used one for exactly that
        reason.

        ⭐ ``unstamped_nets`` names the nets whose **pads are left out of the
        base map**, which is how a constructive walk gets an empty board to add
        parts to: ``build_base_obstacle_map`` skips ``nets_to_route``'s pads by
        design (``obstacle_map.py``'s pad loop), so naming every signal net
        yields a map holding the board edge and the plane copper and nothing
        else. ⚠ A part's plane-net pads stay stamped, so re-adding that part
        merely **increments** their refcount -- which is precisely the case the
        refcounted map exists to make exact, and :meth:`remove_part` asserts it
        rather than trusting it.

        ⛔⛔⛔ ``liftable_nets`` is the answer to a defect gate ``C1`` caught on
        its first run, and it is the whole reason the exactness invariant is
        worth having. A net's **own** pads are obstacles in a base map built the
        obvious way, so asking "does pad A reach pad B" walls the route in
        behind its own endpoints: on ``lt3758_iso_flyback`` the net ``LED_A``
        -- whose first pad is **through-hole** (``*.Cu``), and therefore blocks
        every layer *and* the via layer -- failed after **3 iterations**, and
        the same pair routes in **4.8 mm / 43 iterations** the moment its own
        net is lifted. ⛔ **KRT's real router never meets this** because it
        always passes ``nets_to_route``, which excludes the routed net's pads
        from the base map. A session that did not model that would report an
        unroutable board and be measuring itself.

        A liftable net is therefore kept OUT of the base map and stamped back
        in through :meth:`add_part`'s own machinery, so it owns a token and
        :meth:`lift_net` can take it away for the duration of one question and
        put it back **exactly**. ⭐ That is the refcounted map paying for
        itself: the correct semantics cost ~1 ms instead of a 0.076 s rebuild.
        """
        layers = [str(name) for name in (layers or ())]
        if not layers:
            raise SessionError(
                "⛔ RouteSession needs an EXPLICIT, non-empty copper layer "
                "list. board_info.copper_layers parses [] on this stack's "
                "boards (KRT open issue 10) and a zero-layer map makes every "
                "route return 'Cannot determine endpoints' -- which is "
                "indistinguishable from a board with nothing to route.")

        krt = _import_krt(krt_dir)
        pcb = krt["kicad_parser"].parse_kicad_pcb(pcb_path)
        parsed = tuple(getattr(pcb.board_info, "copper_layers", ()) or ())

        config = krt["GridRouteConfig"](
            layers=list(layers), track_width=float(track_width_mm),
            clearance=float(clearance_mm), via_size=float(via_size_mm),
            via_drill=float(via_drill_mm), grid_step=float(grid_step_mm))
        if not config.layers:
            raise SessionError("⛔ GridRouteConfig dropped the layer list")
        coord = krt["GridCoord"](config.grid_step)
        layer_map = krt["routing_utils"].build_layer_map(config.layers)
        if not layer_map:
            raise SessionError("⛔ an empty layer map -- see open issue 10")

        # ⛔⛔ ``static_base=False`` is LOAD-BEARING and is NOT what route.py
        # does (``route.py:1295`` passes ``static_base=not NO_STATIC_BASE``):
        # the static path stamps into a permanent bitmap with no refcount and no
        # removal, so a session built that way can never un-stamp anything.
        wanted = {str(n) for n in (unstamped_nets or ())}
        liftable = {str(n) for n in (liftable_nets or ())} - wanted
        exclude = sorted(nid for nid, net in pcb.nets.items()
                         if str(net.name) in (wanted | liftable))
        if (wanted or liftable) and not exclude:
            raise SessionError(
                f"⛔ unstamped_nets/liftable_nets named "
                f"{sorted(wanted | liftable)[:4]}... but NONE of them exist "
                f"on {pcb_path}. A base map that silently stamped everything "
                f"would look exactly like a correct empty one (rule 3).")
        obstacles = _quiet(krt["obstacle_map"].build_base_obstacle_map,
                           pcb, config, exclude, static_base=False)

        session = cls(pcb=pcb, config=config, coord=coord, layer_map=layer_map,
                      obstacles=obstacles, krt=krt, pcb_path=str(pcb_path),
                      layers=tuple(layers), parsed_layers=parsed,
                      unstamped_nets=tuple(sorted(wanted)),
                      liftable_nets=tuple(sorted(liftable)))
        # ⛔ Stamp the liftable nets back in through add_part, so each owns a
        # token and lift_net() can remove exactly what it put there. Doing this
        # any other way (a second stamping path, a remembered cell array) is how
        # "what we wrote" and "what we remove" drift apart.
        for net in sorted(liftable):
            nid = next((i for i, n in pcb.nets.items()
                        if str(n.name) == net), None)
            pads = list(pcb.pads_by_net.get(nid, ())) if nid is not None else []
            if pads:
                session.add_part(f"@NET:{net}", pads)
        session.warmup = session._warm_up()
        return session

    # -- the primitives ----------------------------------------------------- #
    @property
    def census(self) -> tuple:
        """``(distinct cells, cells>=2, max refcount, distinct vias, vias>=2,
        static cells, static vias)`` -- the whole map state as one comparable
        value."""
        return (tuple(self.obstacles.dynamic_refcount_stats())
                + tuple(self.obstacles.get_static_stats()))

    def terminals(self, pad) -> list:
        """``[(gx, gy, layer_idx, x_mm, y_mm), ...]`` for one pad.

        ⛔⛔ **Two different zeroes, and conflating them is what rule 3 is
        actually about.** MEASURED 2026-08-02 on three of the six eval boards:
        a controller's thermal pad brings **paste-only sub-apertures**
        (``layers == ['F.Paste']``) that carry a net name and **no copper at
        all**. Those are not routing terminals and never were -- KRT's own
        ``expand_pad_layers`` drops non-copper layers by design -- so returning
        an empty list for them is the correct answer, not a silent failure.

        What *is* a failure is a pad that **declares copper** none of which
        lands in the layer map: that is the open-issue-10 signature (or a wrong
        layer list), and it raises. ⭐ The loudness the rule demands has moved
        to :meth:`route_pair`, which refuses to answer a question about a pad
        with no copper rather than returning a cheerful "unroutable".
        """
        gx, gy = self.coord.to_grid(pad.global_x, pad.global_y)
        expand = self.krt["net_queries"].expand_pad_layers
        copper = list(expand(pad.layers, self.config.layers))
        out = [(gx, gy, self.layer_map[name], pad.global_x, pad.global_y)
               for name in copper if name in self.layer_map]
        if copper and not out:
            raise SessionError(
                f"⛔ pad at ({pad.global_x}, {pad.global_y}) declares copper "
                f"layers {copper} and NONE of them is in the session's layer "
                f"map {list(self.config.layers)}. That is the open-issue-10 "
                f"signature -- a zero-layer map routes nothing while looking "
                f"like a board with nothing to route (rule 3).")
        return out

    def has_copper(self, pad) -> bool:
        """Whether ``pad`` is a routing terminal at all (see :meth:`terminals`)."""
        return bool(self.terminals(pad))

    def add_part(self, part_key: str, pads, *, recorder_cls=None) -> StampToken:
        """Stamp every pad of one part into the live map and return its token.

        ⚠ Through-hole pads block every layer and NPTH pads carry no copper;
        ``_add_pad_obstacle`` already handles both, and a pad's
        ``local_clearance`` is a hard floor honored inside it. **Do not
        pre-inflate geometry here** or it double-counts.

        ⛔ ``recorder_cls`` exists for exactly one caller: the regression control
        that asserts a **via-unaware** recorder leaks, so a future revert to
        KRT's track-only proxy is caught by a test. Never pass it in real use.
        """
        pads = list(pads)
        if not pads:
            raise SessionError(f"⛔ add_part({part_key!r}) got zero pads")
        pre = self.census
        cls = recorder_cls or _FullRecorder
        add = self.krt["obstacle_map"]._add_pad_obstacle
        per_pad, all_cells, all_vias = [], [], []
        for pad in pads:
            recorder = cls(self.obstacles)
            add(recorder, pad, self.coord, self.layer_map, self.config)
            cells, vias = recorder.merged()
            per_pad.append((str(getattr(pad, "net_name", "") or ""),
                            cells, vias))
            all_cells.append(cells)
            all_vias.append(vias)
        import numpy as np

        cells = (np.vstack(all_cells).astype(np.int32) if all_cells
                 else np.empty((0, 3), dtype=np.int32))
        vias = (np.vstack(all_vias).astype(np.int32) if all_vias
                else np.empty((0, 2), dtype=np.int32))
        if not len(cells):
            raise SessionError(
                f"⛔ stamping {part_key!r} ({len(pads)} pad(s)) produced ZERO "
                f"blocked cells. A stamp that blocks nothing is an instrument "
                f"defect, not a small part (rule 3).")
        self._ops += 1
        token = StampToken(part_key=str(part_key), cells=cells, vias=vias,
                           pre_census=pre, post_census=self.census,
                           ops_at_stamp=self._ops,
                           pads=len(pads), per_pad=tuple(per_pad))
        self._tokens.append(token)
        return token

    def remove_part(self, token: StampToken) -> None:
        """Un-stamp exactly what ``token`` stamped, and **assert** it.

        ⛔ This assertion is the reason the whole loop can be trusted. It is
        never downgraded to a warning and never repaired by rebuilding the map.

        ⚠⚠ **BUT IT IS TWO ASSERTIONS, NOT ONE, AND CONFLATING THEM IS A REAL
        MISTAKE -- gate ``C2`` caught it on its first run.** ``pre_census`` is
        the map state before *this* stamp, so demanding it back is only the
        right claim when the map is **still in the state this stamp left it
        in** -- a true LIFO undo. ⚠⚠ That test took two wrong forms before it
        took the right one, and both failures are worth keeping: "is this token
        last in the token list" is not enough (the shuffled round removes
        earlier tokens first, so the last survivor's world has moved on), and
        "does the live census equal this token's ``post_census``" is worse --
        the census is a five-number **summary**, two different maps can share
        it, and the soak found a state that collided with ``CIN2``'s. ⭐ LIFO is
        a structural fact and is now tracked structurally, with an operation
        counter. Remove a token from the middle of a live
        stack -- which is exactly what the shuffled round and any real
        constructive loop do -- and the correct end state still contains every
        *other* live stamp, so demanding ``pre_census`` there asserts something
        false and fails on a map that is behaving perfectly. (Measured: removing
        ``CB`` mid-stack reported ``cells_ge2 7832 -> 37868`` and nothing was
        wrong.)

        ⛔⛔ **And the out-of-order claim cannot be "the map must shrink"
        either** -- that was the same mistake one rung weaker, and the soak
        caught it at step 3. The census is a **five-number summary** (distinct
        cells, cells at refcount >= 2, max refcount, distinct vias, vias >= 2);
        every pad is already stamped once by the base map, so a soak that adds
        the same part twice takes its cells to refcount 3, and removing one copy
        moves them 3 -> 2 -- **still distinct, still >= 2, census literally
        unchanged** while 1 330 cells were genuinely released. ⭐ *A no-op
        census delta is a legitimate outcome of refcounting, and any invariant
        that forbids it is asserting something about the instrument rather than
        the map.*

        So: LIFO removals are held to the exact ``pre_census``; out-of-order
        removals are held to the only universally true claim -- a removal can
        only decrement, so **no field may grow**. The property that actually
        proves refcounting
        (remove **all** tokens in **any** order and land exactly on the starting
        census) belongs to the caller, and gate ``C2`` asserts it in reverse and
        in a seeded shuffle.
        """
        before = self.census
        lifo = (bool(self._tokens) and self._tokens[-1] is token
                and self._ops == token.ops_at_stamp)
        self._ops += 1
        if len(token.cells):
            self.obstacles.remove_blocked_cells_batch(token.cells)
        if len(token.vias):
            self.obstacles.remove_blocked_vias_batch(token.vias)
        now = self.census
        if lifo and now != token.pre_census:
            raise RollbackError(
                f"⛔⛔ un-stamping {token.part_key!r} (LIFO) did not restore "
                f"the map: {_census_drift(token.pre_census, now)} "
                f"(before={token.pre_census} after={now}). Do NOT paper this "
                f"over with a rebuild -- read the code path (plan bail-out 2).")
        if not lifo and any(a > b for b, a in zip(before, now)):
            raise RollbackError(
                f"⛔⛔ un-stamping {token.part_key!r} out of order made the "
                f"map MORE blocked: {_census_drift(before, now)}. A removal "
                f"can only decrement refcounts, so this is real drift.")
        try:
            self._tokens.remove(token)
        except ValueError:                                     # pragma: no cover
            pass

    def snapshot(self) -> Snapshot:
        return Snapshot(census=self.census, tokens=tuple(self._tokens),
                        routes=self._routes)

    def restore(self, snapshot: Snapshot) -> None:
        """Pop tokens back to ``snapshot`` in **reverse** order and assert."""
        keep = list(snapshot.tokens)
        while len(self._tokens) > len(keep):
            self.remove_part(self._tokens[-1])
        if self._tokens != keep:
            raise RollbackError(
                f"⛔⛔ restore() left a different token list: "
                f"{[t.part_key for t in self._tokens]} vs "
                f"{[t.part_key for t in keep]}")
        now = self.census
        if now != snapshot.census:
            raise RollbackError(
                f"⛔⛔ restore() did not return the census: "
                f"{_census_drift(snapshot.census, now)}")

    @contextmanager
    def lift_net(self, net_name: str):
        """Take **every live pad of one net** out of the map for the duration.

        ⭐ One mechanism, over the per-pad records :meth:`add_part` already
        made, so it reaches a net stamped by ``liftable_nets`` at construction
        and a net stamped part-by-part during a constructive walk alike. There
        is no second way of computing which cells belong to a pad, which is how
        "what we removed" and "what we put back" would drift.

        ⛔ The re-stamp is asserted against the census recorded before the lift,
        not hoped for: a net silently left un-stamped would make every *later*
        question on the board optimistic, and nothing downstream would notice.

        ⛔⛔ **PRECONDITION, and it is not cosmetic: a part may be stamped at
        most ONCE while a lift is taken.** KRT's ``remove_blocked_cells_batch``
        **saturates at zero** (``obstacle_map.rs``: at count 1 it deletes the
        entry, and a further removal is a silent no-op) while
        ``add_blocked_cells_batch`` increments without bound -- so a
        remove-then-add round trip is exact only while every removed row still
        has a positive count, and **over-removal followed by an equal re-add
        RAISES the true refcount** (measured directly). ⚠ The 5-number census
        does not always show it: the same experiment reads EXACT when
        ``max_refcount`` happens to be pinned elsewhere, and only surfaced in
        gate ``C2``'s soak at 9 live stamps of one part, as
        ``max_refcount 8 -> 9``. So the guard below is on the **precondition**,
        not on the symptom -- an assertion that can be invisible is not a
        guard.
        """
        net = str(net_name)
        seen: dict = {}
        for token in self._tokens:
            seen[token.part_key] = seen.get(token.part_key, 0) + 1
        doubled = sorted(k for k, n in seen.items() if n > 1)
        records = [(token, cells, vias)
                   for token in self._tokens
                   for pad_net, cells, vias in token.per_pad
                   if pad_net == net and (len(cells) or len(vias))]
        if doubled and records:
            raise SessionError(
                f"⛔⛔ lift_net({net!r}) needs every part stamped at most once, "
                f"and {doubled[:4]} {'is' if len(doubled) == 1 else 'are'} "
                f"stamped more than once. KRT's cell removal saturates at zero "
                f"while its add does not, so a lift under repeated stamping "
                f"INFLATES refcounts instead of restoring them. Un-stamp the "
                f"duplicates, or ask with lift_own_net=False.")
        if not records:
            yield False
            return
        before = self.census
        for _token, cells, vias in records:
            if len(cells):
                self.obstacles.remove_blocked_cells_batch(cells)
            if len(vias):
                self.obstacles.remove_blocked_vias_batch(vias)
        try:
            yield True
        finally:
            for _token, cells, vias in records:
                if len(cells):
                    self.obstacles.add_blocked_cells_batch(cells)
                if len(vias):
                    self.obstacles.add_blocked_vias_batch(vias)
            now = self.census
            if now != before:
                raise RollbackError(
                    f"⛔⛔ re-stamping the lifted net {net!r} did not restore "
                    f"the map: {_census_drift(before, now)}")

    def route_pair(self, a, b, *, net_id: int | None = None,
                   lift_own_net: bool = True,
                   max_iterations: int | None = None) -> PairResult:
        """Route from pad ``a`` to pad ``b`` against the current map.

        ⛔ **Both overrides are ALWAYS passed.** KRT's derivation path
        (``connectivity.get_net_endpoints``) is a case machine over board state
        that answers *"Cannot determine endpoints: 0 segments, 2 pads"* in
        states a constructive board is routinely in -- it is not an API a
        constructive loop wants.

        ⭐ ``lift_own_net`` reproduces what KRT's real router does by passing
        ``nets_to_route``: the pair's **own** net is taken out of the obstacle
        map for the duration of the question, so a pad cannot wall in its own
        escape. It is a no-op unless the net was declared ``liftable_nets`` at
        construction -- see :meth:`from_board` for the measurement that made it
        necessary.

        ⚠ ``length_mm`` is a **routed** length over the grid, so it is quantised
        and can fall **below** the straight line by up to ``sqrt(2) *
        grid_step`` -- both endpoints snap to cell centres. Measured: a pair
        3.0004 mm apart routes as 3.0000 mm. Do not compare it to HPWL as if
        they were the same quantity.
        """
        own_net = str(getattr(a, "net_name", "") or getattr(b, "net_name", "")
                      or "")
        if lift_own_net and own_net:
            with self.lift_net(own_net) as lifted:
                if lifted:
                    return self.route_pair(a, b, net_id=net_id,
                                           lift_own_net=False,
                                           max_iterations=max_iterations)
        sources, targets = self.terminals(a), self.terminals(b)
        # ⛔ Rule 3, at the seam where it belongs: a pad with no copper (a
        # paste-only aperture) cannot be an endpoint, and answering
        # "unroutable" would be a measurement of the instrument, not the board.
        if not sources or not targets:
            raise SessionError(
                f"⛔ route_pair was asked about a pad with NO COPPER: "
                f"a.layers={list(a.layers)} -> {len(sources)} terminal(s), "
                f"b.layers={list(b.layers)} -> {len(targets)} terminal(s). "
                f"Filter with has_copper() before asking -- a cheerful "
                f"'unroutable' here would be indistinguishable from a real one.")
        if net_id is None:
            net_id = int(getattr(a, "net_id", 0) or getattr(b, "net_id", 0) or 0)
        route = self.krt["single_ended_routing"].route_net_with_obstacles
        previous = getattr(self.config, "max_iterations", None)
        if max_iterations is not None:
            self.config.max_iterations = int(max_iterations)
        started = time.time()
        failure = None
        try:
            result = _quiet(route, self.pcb, net_id, self.config,
                            self.obstacles, sources_override=sources,
                            targets_override=targets)
        except Exception as exc:                               # noqa: BLE001
            result, failure = None, f"{type(exc).__name__}: {exc}"
        finally:
            if max_iterations is not None and previous is not None:
                self.config.max_iterations = previous
        elapsed = time.time() - started
        self._routes += 1

        if not result or result.get("failed"):
            return PairResult(routed=False, length_mm=None,
                              iterations=int((result or {}).get("iterations") or 0),
                              vias=0, path_cells=0,
                              elapsed_s=round(elapsed, 6),
                              failure=failure or "no path")
        path = list(result.get("path") or ())
        return PairResult(
            routed=True, length_mm=self.path_length_mm(path),
            iterations=int(result.get("iterations") or 0),
            vias=len(result.get("new_vias") or ()),
            path_cells=len(path), elapsed_s=round(elapsed, 6), failure=None)

    def probe_pair(self, a, b, **kwargs) -> PairResult:
        """:meth:`route_pair` with the map guaranteed untouched afterwards.

        ⭐ Routing does **not** mutate the obstacle map (measured), so this is a
        census assertion rather than an undo -- which is exactly why it is cheap
        enough to be the default question a placer asks.
        """
        before = self.census
        result = self.route_pair(a, b, **kwargs)
        after = self.census
        if after != before:
            raise RollbackError(
                f"⛔⛔ a route MUTATED the obstacle map: "
                f"{_census_drift(before, after)}. Everything downstream assumed "
                f"it does not; re-read route_net_with_obstacles before using "
                f"any number from this session.")
        return result

    def path_length_mm(self, path) -> float | None:
        """Manhattan step length of a grid path, in mm.

        ⚠ A layer change contributes zero length (it is a via, counted
        separately). The probe read **7.0 mm** for a pair 4.95 mm apart -- i.e.
        this is a **routed** length and that is the entire point.
        """
        cells = list(path or ())
        if len(cells) < 2:
            return None
        total = 0
        for (x0, y0, *_a), (x1, y1, *_b) in zip(cells, cells[1:]):
            total += abs(int(x1) - int(x0)) + abs(int(y1) - int(y0))
        return round(total * float(self.config.grid_step), 6)

    # -- internals ---------------------------------------------------------- #
    def pad_pairs_by_net(self, *, min_pads: int = 2) -> dict:
        """``{net_name: [pad, ...]}`` for nets with at least ``min_pads``
        **copper-bearing** pads.

        ⚠ The copper filter is not cosmetic: a controller's thermal pad
        contributes paste-only sub-apertures that carry a net name, so an
        unfiltered count makes a one-pad net look like a two-pad one and the
        pair question becomes unanswerable half a second later.
        """
        names = {nid: str(net.name) for nid, net in self.pcb.nets.items()}
        out = {}
        for nid, pads in self.pcb.pads_by_net.items():
            if nid not in names:
                continue
            copper = [pad for pad in pads if self.terminals(pad)]
            if len(copper) >= min_pads:
                out[names[nid]] = copper
        return out

    def _warm_up(self) -> dict:
        """Route and discard one pair so the first caller-visible answer is
        steady state (defect 4). Records that it happened, and what it cost."""
        by_net = self.pad_pairs_by_net()
        if not by_net:
            raise SessionError(
                f"⛔ {self.pcb_path}: no net has two pads, so the session "
                f"cannot warm up and could never route anything. That is a "
                f"finding about the board or the layer list, not an empty "
                f"session (rule 3).")
        name = sorted(by_net)[0]
        pads = by_net[name]
        started = time.time()
        result = self.route_pair(pads[0], pads[1])
        self._routes = 0
        return {"net": name, "discarded": True,
                "elapsed_s": round(time.time() - started, 6),
                "routed": result.routed, "iterations": result.iterations}


def _census_drift(before: tuple, after: tuple) -> str:
    """Name the field that moved -- a drifting number is only actionable when
    you know *which* one drifted."""
    fields = ("cells", "cells_ge2", "max_refcount", "vias", "vias_ge2",
              "static_cells", "static_vias")
    parts = [f"{fields[i] if i < len(fields) else i}: {b} -> {a}"
             for i, (b, a) in enumerate(zip(before, after)) if b != a]
    return ", ".join(parts) or "lengths differ"


def _quiet(fn, *args, **kwargs):
    """Run with stdout swallowed -- KRT narrates, and the narration is both
    noise and (via flushing) part of what we would be timing."""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        return fn(*args, **kwargs)
