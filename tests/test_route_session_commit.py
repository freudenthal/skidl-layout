# -*- coding: utf-8 -*-
"""S7 C1/C2/C3 -- committed copper, its token, and its exact rollback.

⛔⛔ Part 1 §7.5 deferred this for one stated reason: *"a committed route
changes what 'rollback' means, and exact rollback is the invariant the entire
session rests on."* The deferral is lifted **by turning that reason into the
design**, so what is tested here is the discipline, not the feature:

* a commit is a **token** carrying its own pre-census;
* a LIFO uncommit is held to the **exact** pre-census;
* an out-of-order uncommit is held to the only universally true claim -- a
  removal can only decrement, so **no census field may grow**;
* :meth:`RouteSession.restore` takes the **copper off first**, because it went
  on last.

⛔ The corpus-scale claims (200+ randomized sequences on a real board, and the
construction loop's ON/OFF arms) are gates ``RT4``/``RT5`` in
``canaries/drive_route_tiers.py``, where a router belongs.
"""

from __future__ import annotations

import pytest

from skidl_layout.route_session import (
    CommitToken,
    PairResult,
    RollbackError,
    RouteSession,
    SessionError,
    Snapshot,
)


class _Segment:
    def __init__(self, layer="F.Cu", start=(0.0, 0.0), end=(1.0, 0.0),
                 width=0.3, net_id=1):
        self.layer, self.start, self.end = layer, start, end
        self.width, self.net_id = width, net_id


class _Obstacles:
    """A refcounted stand-in whose census is derived from what it holds.

    ⛔ It **saturates at zero** exactly like KRT's Rust map, because that
    saturation is the reason ``lift_net`` has a precondition and the reason a
    rollback test on a non-saturating fake would prove nothing.
    """

    def __init__(self):
        self.cells: dict = {}

    def _add(self, key):
        self.cells[key] = self.cells.get(key, 0) + 1

    def _remove(self, key):
        if key not in self.cells:
            return
        if self.cells[key] <= 1:
            del self.cells[key]
        else:
            self.cells[key] -= 1

    def dynamic_refcount_stats(self):
        counts = list(self.cells.values())
        return (len(counts), sum(1 for c in counts if c >= 2),
                max(counts) if counts else 0)

    def get_static_stats(self):
        return (0, 0)


class _ObstacleMapModule:
    """The three KRT module functions :meth:`commit_pair` reaches for."""

    @staticmethod
    def add_segments_list_as_obstacles(obstacles, segments, config):
        for index, segment in enumerate(segments):
            obstacles._add(("seg", segment.layer, segment.start, segment.end))

    @staticmethod
    def remove_segments_list_from_obstacles(obstacles, segments, config):
        for segment in segments:
            obstacles._remove(("seg", segment.layer, segment.start,
                               segment.end))

    @staticmethod
    def add_vias_list_as_obstacles(obstacles, vias, config):
        for via in vias:
            obstacles._add(("via", tuple(via)))

    @staticmethod
    def remove_vias_list_from_obstacles(obstacles, vias, config):
        for via in vias:
            obstacles._remove(("via", tuple(via)))


class _Coord:
    grid_step = 0.1

    @staticmethod
    def to_grid(x, y):
        return int(round(x / 0.1)), int(round(y / 0.1))


class _Config:
    grid_step = 0.1
    layers = ["F.Cu", "B.Cu"]


def _session():
    return RouteSession(
        pcb=None, config=_Config(), coord=_Coord(),
        layer_map={"F.Cu": 0, "B.Cu": 1}, obstacles=_Obstacles(),
        krt={"obstacle_map": _ObstacleMapModule()},
        layers=("F.Cu", "B.Cu"))


def _routed(segments=(), vias=(), net="SW"):
    return PairResult(routed=True, length_mm=1.0, iterations=7, vias=len(vias),
                      path_cells=4, elapsed_s=0.001,
                      segments=tuple(segments), new_vias=tuple(vias),
                      path=((0, 0, 0), (1, 0, 0)), net=net)


# --------------------------------------------------------------------------- #
# C1 -- the copper is on the result only when it was asked for
# --------------------------------------------------------------------------- #
def test_a_pair_result_carries_no_copper_by_default():
    """⛔ Every consumer written before S7 must be byte-unchanged."""
    result = PairResult(routed=True, length_mm=1.0, iterations=5, vias=0,
                        path_cells=3, elapsed_s=0.001)
    assert result.segments is None
    assert result.new_vias is None
    assert result.path is None
    assert result.has_copper is False


def test_a_result_with_copper_says_so():
    assert _routed([_Segment()]).has_copper is True
    assert _routed([], [(1, 2)]).has_copper is True


# --------------------------------------------------------------------------- #
# C2 -- the token, and the refusals
# --------------------------------------------------------------------------- #
def test_commit_pair_refuses_an_unrouted_pair():
    session = _session()
    failed = PairResult(routed=False, length_mm=None, iterations=3, vias=0,
                        path_cells=0, elapsed_s=0.001, failure="no path")
    with pytest.raises(SessionError, match="UNROUTED"):
        session.commit_pair(failed)


def test_commit_pair_refuses_a_result_that_was_never_asked_for_copper():
    """⛔ Inventing the copper is forbidden, so the refusal has to be here."""
    session = _session()
    bare = PairResult(routed=True, length_mm=1.0, iterations=3, vias=0,
                      path_cells=3, elapsed_s=0.001)
    with pytest.raises(SessionError, match="keep_copper"):
        session.commit_pair(bare)


def test_commit_pair_refuses_a_routed_pair_with_zero_copper():
    """⛔ Rule 3: a commit that blocks nothing is indistinguishable from one
    that blocked everything."""
    session = _session()
    with pytest.raises(SessionError, match="ZERO"):
        session.commit_pair(_routed([], []))


def test_commit_pair_refuses_a_non_pair_result():
    with pytest.raises(SessionError):
        _session().commit_pair({"routed": True})


def test_a_commit_whose_census_delta_is_ZERO_is_ACCEPTED_and_still_rolls_back():
    """⛔⛔ **The check that used to be here was WRONG and a real board found
    it.** ``commit_pair`` once refused a commit whose census did not move; it
    fired on ``ltc1871_sepic``/``VOUT`` with one segment. The census is a
    five-number **summary** and every pad is already stamped by the base map,
    so a short stub between two stamped pads can raise real refcounts while
    leaving all five numbers pinned -- *a no-op census delta is a legitimate
    outcome of refcounting*, and an invariant that forbids it asserts something
    about the instrument. ⭐ What proves a commit is real is the ROUND TRIP."""
    session = _session()

    class _Pinned(_ObstacleMapModule):
        """Adds to a cell that is already at refcount 2: nothing in the
        five-number summary moves, and the refcount genuinely rises."""

        @staticmethod
        def add_segments_list_as_obstacles(obstacles, segments, config):
            for segment in segments:
                obstacles._add(("pinned",))

        @staticmethod
        def remove_segments_list_from_obstacles(obstacles, segments, config):
            for segment in segments:
                obstacles._remove(("pinned",))

    session.obstacles._add(("pinned",))
    session.obstacles._add(("pinned",))
    session.obstacles._add(("other",))
    session.obstacles._add(("other",))
    session.obstacles._add(("other",))
    session.krt["obstacle_map"] = _Pinned()
    before = session.census
    token = session.commit_pair(_routed([_Segment()]))
    assert token.post_census == before, "the delta really is nil here"
    session.uncommit(token)
    assert session.census == before


def test_the_commit_token_records_what_it_stamped():
    session = _session()
    token = session.commit_pair(_routed([_Segment(), _Segment(end=(2.0, 0.0))],
                                        [(3, 4)]), net="SW")
    assert isinstance(token, CommitToken)
    assert (token.segment_count, token.via_count) == (2, 1)
    assert token.net == "SW"
    assert token.pre_census != token.post_census
    assert session.commits == (token,)


def test_a_lifo_uncommit_restores_the_census_EXACTLY():
    session = _session()
    before = session.census
    token = session.commit_pair(_routed([_Segment()], [(3, 4)]))
    assert session.census != before
    session.uncommit(token)
    assert session.census == before
    assert session.commits == ()


def test_a_broken_lifo_undo_RAISES_rather_than_being_papered_over():
    """⛔⛔ Plan bail-out C in one test: the invariant is never downgraded."""
    session = _session()
    token = session.commit_pair(_routed([_Segment()]))
    # ⚠ Residue: something blocks a cell behind the session's back -- the exact
    # shape of standing finding 22's undiagnosed drift, staged deliberately.
    session.obstacles._add(("seg", "F.Cu", (99.0, 0.0), (99.0, 1.0)))
    with pytest.raises(RollbackError, match="LIFO"):
        session.uncommit(token)


def test_an_out_of_order_uncommit_is_held_to_the_weaker_TRUE_claim():
    """⛔ ``pre_census`` is only the right claim for a true LIFO undo -- the
    same distinction :meth:`remove_part` records having got wrong twice."""
    session = _session()
    first = session.commit_pair(_routed([_Segment()]), net="A")
    second = session.commit_pair(_routed([_Segment(end=(5.0, 0.0))]), net="B")
    # out of order: the map must not GROW, but it will not equal first's
    # pre_census either, because B is still live.
    session.uncommit(first)
    assert session.commits == (second,)
    session.uncommit(second)


def test_uncommitting_a_token_this_session_does_not_hold_raises():
    session = _session()
    other = CommitToken(net="X", segments=(), vias=(), pre_census=())
    with pytest.raises(RollbackError):
        session.uncommit(other)


def test_uncommit_all_round_trips_and_reports_exactness():
    """⛔ The claim is a ROUND TRIP -- off, back on, off again -- because the
    obvious claim (*"the census equals the first commit's pre_census"*) is
    about the STAMPS on the map, not about the copper, and it reported
    ``exact=False`` on 4 of 4 real boards while nothing was wrong."""
    session = _session()
    base = session.census
    for index in range(6):
        session.commit_pair(_routed([_Segment(end=(float(index) + 1.0, 0.0))]),
                            net=f"N{index}")
    blob = session.uncommit_all()
    assert blob["uncommitted"] == 6
    assert blob["exact"] is True
    assert blob["replayed"] == blob["with_copper"]
    assert blob["final"] == blob["without_copper"]
    assert session.census == base
    assert session.commits == ()
    # ⛔ and on an empty stack it is a no-op that still reports a denominator
    assert session.uncommit_all()["uncommitted"] == 0


def test_uncommit_all_is_exact_even_with_UNRELATED_stamps_live():
    """⭐ The whole point of the round trip: other things on the map may not
    make the copper's rollback look broken."""
    session = _session()
    session.obstacles._add(("someone", "else"))
    session.commit_pair(_routed([_Segment()]), net="SW")
    session.obstacles._add(("stamped", "later"))
    blob = session.uncommit_all()
    assert blob["exact"] is True, blob
    assert ("someone", "else") in session.obstacles.cells
    assert ("stamped", "later") in session.obstacles.cells


def test_uncommit_all_reports_INEXACT_when_the_replay_disagrees():
    """⛔ bail-out C's own detector, exercised."""
    session = _session()
    session.commit_pair(_routed([_Segment()]), net="SW")

    calls = {"n": 0}
    real_add = _ObstacleMapModule.add_segments_list_as_obstacles

    class _Flaky(_ObstacleMapModule):
        @staticmethod
        def add_segments_list_as_obstacles(obstacles, segments, config):
            calls["n"] += 1
            if calls["n"] == 1:          # the replay drops one segment
                return None
            real_add(obstacles, segments, config)

    session.krt["obstacle_map"] = _Flaky()
    blob = session.uncommit_all()
    assert blob["exact"] is False
    assert "re-stamping" in blob["drift"]


def test_commit_and_uncommit_round_trip_over_many_seeded_sequences():
    """⛔ The property the whole loop rests on, exercised rather than argued.

    ⚠ Seeded, never :func:`random.random` -- an unreproducible rollback failure
    is worth much less than a reproducible one.
    """
    import random

    for seed in range(64):
        rng = random.Random(seed)
        session = _session()
        base = session.census
        tokens = []
        for step in range(rng.randint(2, 10)):
            segments = [_Segment(layer=rng.choice(["F.Cu", "B.Cu"]),
                                 start=(float(rng.randint(0, 9)), 0.0),
                                 end=(float(rng.randint(10, 19)), 0.0))
                        for _ in range(rng.randint(1, 3))]
            vias = [(rng.randint(0, 9), rng.randint(0, 9))
                    for _ in range(rng.randint(0, 2))]
            tokens.append(session.commit_pair(_routed(segments, vias),
                                              net=f"N{step}"))
        order = list(tokens)
        rng.shuffle(order)
        for token in order:
            session.uncommit(token)
        assert session.census == base, f"seed {seed} did not roll back"
        assert session.commits == ()


# --------------------------------------------------------------------------- #
# C2 -- restore() has to take the copper off FIRST
# --------------------------------------------------------------------------- #
def test_restore_removes_the_commits_before_the_stamps():
    session = _session()
    base = session.snapshot()
    assert base.commits == ()
    session.commit_pair(_routed([_Segment()]), net="SW")
    assert len(session.snapshot().commits) == 1
    session.restore(base)
    assert session.commits == ()
    assert session.census == base.census


def test_a_snapshot_carries_its_commits():
    session = _session()
    token = session.commit_pair(_routed([_Segment()]), net="SW")
    snap = session.snapshot()
    assert snap.commits == (token,)
    assert isinstance(snap, Snapshot)
    session.uncommit(token)


# --------------------------------------------------------------------------- #
# C3 -- the tap targets
# --------------------------------------------------------------------------- #
def test_committed_terminals_returns_terminal_shaped_rows_for_ONE_net():
    session = _session()
    session.commit_pair(_routed([_Segment(layer="F.Cu", start=(1.0, 2.0),
                                          end=(3.0, 2.0))]), net="SW")
    session.commit_pair(_routed([_Segment(layer="B.Cu", start=(9.0, 9.0),
                                          end=(9.0, 8.0))]), net="OTHER")
    rows = session.committed_terminals("SW")
    assert rows, "a tap over nothing would be standing finding 1"
    assert all(len(row) == 5 for row in rows)
    assert {row[2] for row in rows} == {0}, "F.Cu only -- OTHER is on B.Cu"
    assert (10, 20, 0, 1.0, 2.0) in rows
    assert session.committed_terminals("NOT_A_NET") == []


def test_committed_terminals_deduplicates_shared_endpoints():
    session = _session()
    session.commit_pair(_routed([_Segment(start=(1.0, 1.0), end=(2.0, 1.0)),
                                 _Segment(start=(2.0, 1.0), end=(3.0, 1.0))]),
                        net="SW")
    rows = session.committed_terminals("SW")
    assert len(rows) == 3, rows
