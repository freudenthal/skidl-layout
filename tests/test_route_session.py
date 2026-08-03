# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.route_session` -- the live, rollback-exact map.

⛔ **The corpus-scale claims are gates, not tests.** Whether a pad pair routes
pin to pin on six real boards, whether the census returns exactly after 500
random operations, and whether the session agrees with the real router are
``C1``/``C2``/``C4`` in ``canaries/drive_route_session.py``, where a router
belongs. What lives here is everything that can be pinned **without** routing a
board: the refusals (an instrument that can observe nothing must fail), the
arithmetic, and -- most importantly -- the three invariant mis-statements the
gates caught, each of which now has a test so it cannot come back.
"""

from __future__ import annotations

import pytest

from skidl_layout.route_session import (
    KRT_PINNED_SHA,
    PairResult,
    RollbackError,
    RouteSession,
    SessionError,
    Snapshot,
    StampToken,
    _CellsOnlyRecorder,
    _census_drift,
    _FullRecorder,
)


def _has_krt():
    from skidl_layout.krt import find_krt

    return find_krt(None) is not None


needs_krt = pytest.mark.skipif(not _has_krt(), reason="no KiCadRoutingTools")


# --------------------------------------------------------------------------- #
# The refusals -- rule 3: an instrument that can observe nothing must FAIL
# --------------------------------------------------------------------------- #
def test_from_board_refuses_an_empty_layer_list():
    """⛔⛔ Open issue 10: ``board_info.copper_layers`` parses ``[]`` on every
    board this stack writes, and in-process there is no ``DEFAULT_LAYERS``
    fallback -- a zero-layer map makes every route return "Cannot determine
    endpoints", which reads exactly like a board with nothing to route."""
    with pytest.raises(SessionError) as excinfo:
        RouteSession.from_board("nonexistent.kicad_pcb", layers=[],
                                clearance_mm=0.25, track_width_mm=0.3,
                                via_size_mm=0.6, via_drill_mm=0.3)
    assert "layer" in str(excinfo.value).lower()
    # ⭐ It must refuse BEFORE touching the file: the layer list is a caller
    # error and reporting it as a missing board would send a reader to the
    # wrong place.
    assert "open-issue-10" in str(excinfo.value) or "issue 10" in str(excinfo.value)


class _FakePad:
    def __init__(self, layers, net_name="N", x=1.0, y=2.0):
        self.layers = list(layers)
        self.net_name = net_name
        self.global_x, self.global_y = x, y
        self.size_x = self.size_y = 0.5
        self.net_id = 1


class _StubSession(RouteSession):
    """A session with the KRT seams stubbed, for the pure-logic assertions."""

    @classmethod
    def build(cls, layers=("F.Cu", "B.Cu")):
        def _expand(pad_layers, routing_layers):
            out = []
            for name in pad_layers:
                if name == "*.Cu":
                    out.extend(routing_layers)
                elif name.endswith(".Cu"):
                    out.append(name)
            return sorted(set(out), key=lambda n: (routing_layers.index(n)
                                                   if n in routing_layers
                                                   else len(routing_layers), n))

        class _Coord:
            grid_step = 0.1

            @staticmethod
            def to_grid(x, y):
                return int(round(x / 0.1)), int(round(y / 0.1))

        class _Config:
            grid_step = 0.1

            def __init__(self, layers):
                self.layers = list(layers)

        krt = {"net_queries": type("NQ", (), {"expand_pad_layers":
                                              staticmethod(_expand)})}
        return cls(pcb=None, config=_Config(layers), coord=_Coord(),
                   layer_map={name: i for i, name in enumerate(layers)},
                   obstacles=None, krt=krt, layers=tuple(layers))


def test_a_paste_only_pad_is_not_a_terminal_and_is_not_an_error():
    """⛔⛔ **Two different zeroes.** MEASURED on three of the six eval boards:
    a controller's thermal pad brings paste-only sub-apertures that carry a net
    name and no copper. Those are not routing terminals and never were -- KRT's
    own ``expand_pad_layers`` drops non-copper layers by design."""
    session = _StubSession.build()
    assert session.terminals(_FakePad(["F.Paste"])) == []
    assert session.has_copper(_FakePad(["F.Paste"])) is False
    assert len(session.terminals(_FakePad(["F.Cu", "F.Mask"]))) == 1
    assert len(session.terminals(_FakePad(["*.Cu"]))) == 2


def test_copper_on_an_absent_layer_DOES_raise():
    """⛔ The other zero: a pad that declares copper none of which is in the
    map is the open-issue-10 signature and must be loud."""
    session = _StubSession.build(layers=("F.Cu",))
    with pytest.raises(SessionError) as excinfo:
        session.terminals(_FakePad(["In1.Cu"]))
    assert "open-issue-10" in str(excinfo.value)


def test_route_pair_refuses_a_pad_with_no_copper():
    """⛔ Rule 3 at the seam where it belongs: answering "unroutable" for a
    paste aperture would be indistinguishable from a real unroutable pair."""
    session = _StubSession.build()
    with pytest.raises(SessionError) as excinfo:
        session.route_pair(_FakePad(["F.Paste"]), _FakePad(["F.Cu"]),
                           lift_own_net=False)
    assert "NO COPPER" in str(excinfo.value)


def test_add_part_refuses_zero_pads():
    session = _StubSession.build()
    with pytest.raises(SessionError):
        session.add_part("R1", [])


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
def test_path_length_is_manhattan_over_the_grid_and_ignores_layer_changes():
    """⚠ A layer change contributes zero length -- it is a via, counted
    separately. And the result is quantised, which is why the gate's
    "not shorter than the straight line" check carries a sqrt(2)*grid_step
    tolerance rather than an exact >=."""
    session = _StubSession.build()
    assert session.path_length_mm([]) is None
    assert session.path_length_mm([(0, 0, 0)]) is None
    assert session.path_length_mm([(0, 0, 0), (3, 0, 0)]) == pytest.approx(0.3)
    assert session.path_length_mm([(0, 0, 0), (3, 4, 0)]) == pytest.approx(0.7)
    # a via: same cell, different layer -> no length
    assert session.path_length_mm([(0, 0, 0), (0, 0, 1)]) == pytest.approx(0.0)


def test_census_drift_names_the_field_that_moved():
    """A drifting number is only actionable when you know WHICH one drifted."""
    assert _census_drift((1, 2, 3), (1, 2, 3)) == "lengths differ"
    assert "cells_ge2: 2 -> 9" in _census_drift((1, 2, 3), (1, 9, 3))
    assert _census_drift((1, 2), (5, 2)).startswith("cells: 1 -> 5")


def test_pair_result_is_frozen():
    result = PairResult(routed=True, length_mm=1.0, iterations=5, vias=0,
                        path_cells=3, elapsed_s=0.001)
    with pytest.raises(Exception):
        result.routed = False


# --------------------------------------------------------------------------- #
# The recorders -- defect 2
# --------------------------------------------------------------------------- #
class _FakeMap:
    """Counts what actually reached the map."""

    def __init__(self):
        self.cells, self.vias = [], []

    def add_blocked_cells_batch(self, cells):
        self.cells.append(list(map(list, cells)))

    def add_blocked_vias_batch(self, vias):
        self.vias.append(list(map(list, vias)))

    def anything_else(self):
        return "forwarded"


def test_full_recorder_records_cells_AND_vias():
    """⛔⛔ KRT's own ``_RecordingObstacles`` records track cells only, so a
    rollback built on it leaks via refcounts monotonically (~971 via-cell and
    ~39 via refcounts from a single 2-pad stamp, measured)."""
    real = _FakeMap()
    recorder = _FullRecorder(real)
    recorder.add_blocked_cells_batch([[1, 2, 0]])
    recorder.add_blocked_vias_batch([[1, 2]])
    cells, vias = recorder.merged()
    assert len(cells) == 1 and len(vias) == 1
    assert real.cells and real.vias           # and it forwarded both
    assert recorder.anything_else() == "forwarded"


def test_the_via_unaware_control_is_deliberately_blind():
    """⛔ The regression control, not a tool: gate ``C2`` asserts a session
    built on it LEAKS, so a revert to KRT's track-only proxy is caught by a
    test rather than by a corridor that quietly closed."""
    real = _FakeMap()
    recorder = _CellsOnlyRecorder(real)
    recorder.add_blocked_cells_batch([[1, 2, 0]])
    recorder.add_blocked_vias_batch([[1, 2]])
    cells, vias = recorder.merged()
    assert len(cells) == 1
    assert len(vias) == 0                     # ⛔ the leak, on purpose
    assert real.vias                          # it still reached the map


# --------------------------------------------------------------------------- #
# The three invariant mis-statements the gates caught
# --------------------------------------------------------------------------- #
class _CountingMap:
    """A pure-Python stand-in for KRT's refcounted map, including the one
    asymmetry that matters: **removal saturates at zero, addition does not.**"""

    def __init__(self):
        self.counts: dict = {}
        self.via_counts: dict = {}

    def add_blocked_cells_batch(self, cells):
        for row in cells:
            key = tuple(int(v) for v in row)
            self.counts[key] = self.counts.get(key, 0) + 1

    def remove_blocked_cells_batch(self, cells):
        for row in cells:
            key = tuple(int(v) for v in row)
            if key in self.counts:
                if self.counts[key] > 1:
                    self.counts[key] -= 1
                else:
                    del self.counts[key]

    def add_blocked_vias_batch(self, vias):
        for row in vias:
            key = tuple(int(v) for v in row)
            self.via_counts[key] = self.via_counts.get(key, 0) + 1

    def remove_blocked_vias_batch(self, vias):
        for row in vias:
            key = tuple(int(v) for v in row)
            if key in self.via_counts:
                if self.via_counts[key] > 1:
                    self.via_counts[key] -= 1
                else:
                    del self.via_counts[key]

    def dynamic_refcount_stats(self):
        values = list(self.counts.values())
        return (len(values), sum(1 for v in values if v >= 2),
                max(values) if values else 0,
                len(self.via_counts),
                sum(1 for v in self.via_counts.values() if v >= 2))

    def get_static_stats(self):
        return (0, 0)


class _CountingSession(_StubSession):
    @classmethod
    def build(cls, layers=("F.Cu", "B.Cu")):
        session = super().build(layers)
        session.obstacles = _CountingMap()

        def _stamp(obstacles, pad, coord, layer_map, config):
            gx, gy = coord.to_grid(pad.global_x, pad.global_y)
            obstacles.add_blocked_cells_batch([[gx, gy, 0], [gx + 1, gy, 0]])
            obstacles.add_blocked_vias_batch([[gx, gy]])

        session.krt = dict(session.krt)
        session.krt["obstacle_map"] = type(
            "OM", (), {"_add_pad_obstacle": staticmethod(_stamp)})
        return session


def test_lifo_is_structural_not_a_census_comparison():
    """⛔⛔ **The census is a five-number SUMMARY, not a state identity**, and
    two genuinely different maps can share it. Gate ``C2``'s soak produced a
    state whose census equalled an old token's ``post_census``; a LIFO test
    built on that comparison believed it and then failed the exact-``pre_census``
    assertion on a map that was behaving correctly.

    Here two different stampings share a census and the session must still know
    the second removal is not a LIFO undo.
    """
    session = _CountingSession.build()
    a = session.add_part("A", [_FakePad(["F.Cu"], x=1.0, y=1.0)])
    b = session.add_part("B", [_FakePad(["F.Cu"], x=5.0, y=5.0)])
    # remove the OLDER token first: B is now last in the list, but the world
    # has moved on, so removing B is not a LIFO undo of B's stamp.
    session.remove_part(a)
    assert session._tokens == [b]
    session.remove_part(b)                    # must NOT assert b.pre_census
    assert session.census == (0, 0, 0, 0, 0, 0, 0)


def test_an_out_of_order_removal_may_legitimately_leave_the_census_UNCHANGED():
    """⭐ A no-op census delta is a legitimate outcome of refcounting, and an
    invariant that forbids it asserts something about the instrument rather
    than the map. Stamp the same pad three times: removing one copy moves its
    cells 3 -> 2, so they stay distinct, stay >= 2, and the summary does not
    move -- while real cells were released."""
    session = _CountingSession.build()
    pad = _FakePad(["F.Cu"], x=1.0, y=1.0)
    for _ in range(3):
        session.add_part("A", [pad])
    # ⚠ A second triple elsewhere, so ``max_refcount`` is pinned by something
    # other than the cells under test -- which is exactly the situation on a
    # real board, where the base map has already stamped every pad.
    elsewhere = _FakePad(["F.Cu"], x=9.0, y=9.0)
    for _ in range(3):
        session.add_part("Y", [elsewhere])
    other = session.add_part("Z", [_FakePad(["F.Cu"], x=15.0, y=15.0)])
    before = session.census
    session.remove_part(session._tokens[0])   # out of order, and a no-op census
    assert session.census == before
    session.remove_part(other)


def test_lift_net_refuses_when_a_part_is_stamped_twice():
    """⛔⛔ KRT's cell removal **saturates at zero** while its add does not, so
    a lift under repeated stamping INFLATES refcounts instead of restoring
    them. The guard is on the precondition, not on the symptom -- the symptom
    is invisible in the census whenever ``max_refcount`` is pinned elsewhere."""
    session = _CountingSession.build()
    pad = _FakePad(["F.Cu"], net_name="BOOST", x=1.0, y=1.0)
    session.add_part("CB", [pad])
    session.add_part("CB", [pad])
    with pytest.raises(SessionError) as excinfo:
        with session.lift_net("BOOST"):
            pass                              # pragma: no cover
    assert "at most once" in str(excinfo.value)


def test_lift_net_round_trips_exactly_and_actually_unblocks():
    session = _CountingSession.build()
    session.add_part("R1", [_FakePad(["F.Cu"], net_name="MID", x=1.0, y=1.0)])
    session.add_part("R2", [_FakePad(["F.Cu"], net_name="GND", x=4.0, y=1.0)])
    before = session.census
    with session.lift_net("MID") as lifted:
        assert lifted is True
        assert session.census != before       # it really came out
    assert session.census == before           # and went back exactly


def test_lift_net_on_an_unstamped_net_is_a_no_op():
    session = _CountingSession.build()
    session.add_part("R1", [_FakePad(["F.Cu"], net_name="MID")])
    with session.lift_net("NOT_A_NET") as lifted:
        assert lifted is False


def test_remove_part_raises_when_a_removal_makes_the_map_more_blocked():
    """The only universally true claim about an out-of-order removal: a removal
    can only decrement, so no census field may grow."""
    session = _CountingSession.build()
    a = session.add_part("A", [_FakePad(["F.Cu"], x=1.0, y=1.0)])
    session.add_part("B", [_FakePad(["F.Cu"], x=5.0, y=5.0)])
    session.obstacles.remove_blocked_cells_batch = (
        lambda cells: session.obstacles.add_blocked_cells_batch(cells))
    with pytest.raises(RollbackError) as excinfo:
        session.remove_part(a)
    assert "MORE blocked" in str(excinfo.value)


def test_snapshot_and_restore_pop_in_reverse():
    session = _CountingSession.build()
    session.add_part("A", [_FakePad(["F.Cu"], x=1.0, y=1.0)])
    mark = session.snapshot()
    session.add_part("B", [_FakePad(["F.Cu"], x=5.0, y=5.0)])
    session.add_part("C", [_FakePad(["F.Cu"], x=7.0, y=7.0)])
    session.restore(mark)
    assert [t.part_key for t in session._tokens] == ["A"]
    assert session.census == mark.census


def test_a_token_records_its_stamp_per_pad_as_well_as_merged():
    """⭐ One stamping path, two views. ``lift_net`` is a selection over records
    we already made -- never a second way of computing which cells a pad owns,
    which is how "what we removed" and "what we put back" would drift."""
    session = _CountingSession.build()
    token = session.add_part("R1", [_FakePad(["F.Cu"], net_name="A", x=1.0),
                                    _FakePad(["F.Cu"], net_name="B", x=5.0)])
    assert [net for net, _c, _v in token.per_pad] == ["A", "B"]
    assert token.cell_count == sum(len(c) for _n, c, _v in token.per_pad)
    assert token.via_count == sum(len(v) for _n, _c, v in token.per_pad)


# --------------------------------------------------------------------------- #
# The private-API pin -- trap 6
# --------------------------------------------------------------------------- #
@needs_krt
def test_the_private_krt_names_are_pinned_and_named_with_the_sha():
    """⚠ The only private-API dependency this repo has taken. It is pinned by
    an import-time check so a future re-sync fails loudly instead of subtly."""
    from skidl_layout.route_session import _import_krt

    krt = _import_krt(None)
    assert hasattr(krt["obstacle_map"], "_add_pad_obstacle")
    assert hasattr(krt["obstacle_map"], "_RecordingObstacles")
    assert KRT_PINNED_SHA


@needs_krt
def test_import_krt_reports_a_moved_seam_with_an_actionable_message(monkeypatch):
    from skidl_layout import route_session as RS

    krt = RS._import_krt(None)
    monkeypatch.delattr(krt["obstacle_map"], "_add_pad_obstacle")
    with pytest.raises(SessionError) as excinfo:
        RS._import_krt(None)
    message = str(excinfo.value)
    assert KRT_PINNED_SHA in message
    assert "divergence must stay ZERO" in message


def test_route_session_is_a_leaf_module():
    """⛔ Nothing in the placement engine may import it, or an import-time KRT
    dependency reaches the engine and the "KRT absent" path breaks."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    importers = [path.name for path in root.glob("*.py")
                 if path.name != "route_session.py"
                 and "route_session" in path.read_text(encoding="utf-8")]
    assert importers == [], f"route_session is imported by {importers}"
