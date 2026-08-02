"""The overlap penalty, graded by DEPTH instead of counted.

⛔⛔⛔ **What this file exists to protect, and it is a MEASURED premise rather
than a wish.** The overlap term is **0 at every placement this engine ships** --
zero overlaps on 12 of 12 board-arms -- but non-zero on **60-90 % of scored
trials on 6 of 6 power boards**. So for most of the search's life the only
signal separating one candidate from another is ``len(overlaps) * 25.0``, a step
function that cannot tell a 0.01 mm graze from a 3 mm interpenetration.
``overlap_objective="area"`` makes that signal continuous.

⛔⛔ **And the thing a reader must understand before touching it: ``penalty`` is
the LAST thing the search consults.** Four count-based gates sit above it, and
WS-Z1 measured all four on the corpus before a line of this was written:

1. ``refinement._is_better`` is **lexicographic** on ``_hard_count`` and only
   compares ``penalty`` on a tie. It reaches the penalty comparison on
   **48.9-61.3 %** of its calls, of which **26.0-48.1 %** have an illegal side.
   ⭐ **That last figure is this objective's entire reachable surface** -- large
   enough to be a lever, and nowhere near 100 %.
2. ``refinement._hard_violation_key``'s rejection at ``refinement.py:1426``
   fired **0 times out of 253-656 comparisons on 6 of 6 boards.** Inert on this
   corpus -- and the instrument saw the comparisons, so that is a real negative
   and not a silent zero.
3. ``engine.py:918``'s ``(not ok, penalty, name)`` pre-filter and
   ``engine.py``'s final ``(ok, -penalty, name)`` selection both put every legal
   candidate above every illegal one. **Never once was the whole field illegal**
   (0 of 6 boards), so depth could not have moved a winner there either.
4. ``LayoutScore.ok`` stays boolean, and this objective must never change it.

Seven things are pinned here, in the order they can fail:

1. ⭐⭐ **The default is a TRUE no-op.** ``"count"`` must reproduce the shipped
   arithmetic exactly and stay the default in every place the default is
   declared -- two defaults would make a directly-scored placement silently
   incomparable with a planned one.
2. ⭐⭐⭐ **The two objectives are EQUAL at full penetration.** ``unit``
   saturates at 1.0, so a fully-interpenetrating pair costs exactly 25.0 under
   both. The change is strictly a refinement *below* the existing cost and can
   never make an illegal board cheaper than the count already made it.
3. ⭐⭐ **The term is monotone in depth**, and ~0 for a pair grazing the
   clearance -- that gradient is the entire point.
4. ⛔⛔ **A pair with NO AABB gap is charged the full 1.0.** ``overlaps`` unions
   the same-side body test with ``_pad_collision_pairs``, a *through-board pad
   vs pad* test across **opposite** sides where a signed AABB separation is
   meaningless. Charging those less would make a real pad collision cheaper
   than it is today.
5. ⛔⛔ **``ok`` is unchanged, on both ``ValidationResult`` and ``LayoutScore``,
   and an illegal board is never ranked above a legal one.**
6. ⛔ **The seams deliberately NOT threaded** -- ``_rank_and_limit_trials``
   (computes no penalty) and the ordering keys ``_hard_count`` /
   ``_hard_violation_key`` (that is the larger B2 change, not built).
7. ⛔⛔ **The worker boundary, slot 11** -- the ``crossing_objective`` defect's
   sixth home, and the first knob to reach **both** scorers. Parallelism engages
   at >= 30 parts, so an unthreaded knob corrupts exactly the two biggest boards
   and leaves the small ones perfect.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from skidl_layout.constraints import BoardOutline
from skidl_layout.engine import _FinalizeParams, _resolve_overlap_objective
from skidl_layout.scoring import (
    DEFAULT_OVERLAP_OBJECTIVE,
    OVERLAP_OBJECTIVE_AREA,
    OVERLAP_OBJECTIVE_COUNT,
    OVERLAP_OBJECTIVES,
    OVERLAP_PAIR_PENALTY,
    LayoutScore,
    _overlap_term,
    score_placement,
    score_placement_quick,
)
from skidl_layout.validator import ValidationResult, overlap_gaps, validate
from skidl_layout.writer import PlacedPart

from tests.test_layout_scoring import BBOXES, _Circuit, _Net, _Part


CLEARANCE = 0.5
#: ``BBOXES["Package_QFP:MCU"]`` is 12 x 12 mm, so two parts whose centres are
#: ``12.0 + gap`` apart have exactly ``gap`` between their AABBs.
MCU = "Package_QFP:MCU"
MCU_SIZE = 12.0


def _pair_at(gap_mm):
    """Two 12 x 12 mm parts on one net, separated by exactly ``gap_mm``.

    ⭐ A negative ``gap_mm`` interpenetrates. One net and one pair means the
    overlap term is the *only* thing that moves between two calls, so every
    assertion below is an equality rather than a trend.
    """
    net = _Net("SW")
    parts = [_Part("U1", name="MCU", footprint=MCU, nets=[net]),
             _Part("U2", name="MCU", footprint=MCU, nets=[net])]
    circuit = _Circuit(parts, [net])
    placed = [PlacedPart("U1", 30.0, 40.0, 0.0, MCU),
              PlacedPart("U2", 30.0 + MCU_SIZE + gap_mm, 40.0, 0.0, MCU)]
    return circuit, placed


def _validation_at(gap_mm):
    circuit, placed = _pair_at(gap_mm)
    return validate(placed, circuit, BBOXES, clearance_mm=CLEARANCE)


def _score(gap_mm, objective, quick=False):
    circuit, placed = _pair_at(gap_mm)
    fn = score_placement_quick if quick else score_placement
    return fn(placed, circuit, BBOXES, outline=BoardOutline(100.0, 100.0),
              clearance_mm=CLEARANCE, overlap_objective=objective)


# --------------------------------------------------------------------------- #
# 1. The default is "count", everywhere, and it is a true no-op
# --------------------------------------------------------------------------- #
def test_default_is_count_everywhere_it_is_declared():
    from skidl_layout.refinement import (
        refine_candidate_placement, refine_placement,
    )

    assert DEFAULT_OVERLAP_OBJECTIVE == OVERLAP_OBJECTIVE_COUNT == "count"
    assert OVERLAP_OBJECTIVES == ("count", "area")
    for fn in (score_placement, score_placement_quick, refine_placement,
               refine_candidate_placement):
        assert (inspect.signature(fn).parameters["overlap_objective"].default
                == "count")


def test_count_reproduces_the_shipped_arithmetic_exactly():
    """⛔ The one expression that must never move: it is every historical
    penalty this repo has recorded."""
    for n, gap in ((0, 2.0), (1, -1.0)):
        validation = _validation_at(gap)
        assert len(validation.overlaps) == n
        assert _overlap_term(validation, CLEARANCE) == n * 25.0
        assert (_overlap_term(validation, CLEARANCE, "count")
                == len(validation.overlaps) * 25.0)


@pytest.mark.parametrize("quick", [False, True])
@pytest.mark.parametrize("gap", [2.0, 0.25, -0.1, -3.0])
def test_implicit_default_equals_an_explicit_count(gap, quick):
    """⛔ A knob whose OFF value differs from its absence is not an opt-in, it
    is a second default."""
    circuit, placed = _pair_at(gap)
    fn = score_placement_quick if quick else score_placement
    outline = BoardOutline(100.0, 100.0)
    implicit = fn(placed, circuit, BBOXES, outline=outline,
                  clearance_mm=CLEARANCE)
    explicit = fn(placed, circuit, BBOXES, outline=outline,
                  clearance_mm=CLEARANCE, overlap_objective="count")
    assert implicit.penalty == explicit.penalty
    assert implicit.overlap_count == explicit.overlap_count
    assert implicit.score == explicit.score


# --------------------------------------------------------------------------- #
# 2. ⭐⭐⭐ The equality at full penetration -- the barrier is not weakened
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("gap", [-0.5, -1.0, -5.0])
def test_full_penetration_costs_exactly_the_same_under_both(gap):
    """``unit`` saturates at 1.0 at ``gap <= 0`` (depth >= clearance), so the
    depth grader is a refinement BELOW the existing cost and never a discount
    on it."""
    validation = _validation_at(gap)
    assert len(validation.overlaps) == 1
    assert _overlap_term(validation, CLEARANCE, "count") == 25.0
    assert _overlap_term(validation, CLEARANCE, "area") == pytest.approx(25.0)


def test_a_legal_board_costs_zero_under_both():
    validation = _validation_at(2.0)
    assert not validation.overlaps
    assert _overlap_term(validation, CLEARANCE, "count") == 0.0
    assert _overlap_term(validation, CLEARANCE, "area") == 0.0


# --------------------------------------------------------------------------- #
# 3. ⭐⭐ The gradient: monotone in depth, ~0 at the threshold
# --------------------------------------------------------------------------- #
def test_a_pair_grazing_the_clearance_costs_almost_nothing_under_area():
    """⭐ The whole point. Under ``"count"`` a 0.499 mm gap and a 3 mm
    interpenetration are the SAME number; under ``"area"`` they are 0.05 and
    25.0."""
    grazing = _validation_at(0.499)
    assert len(grazing.overlaps) == 1                     # illegal either way
    assert _overlap_term(grazing, CLEARANCE, "count") == 25.0
    assert _overlap_term(grazing, CLEARANCE, "area") == pytest.approx(0.05)


def test_the_term_is_monotone_in_depth():
    gaps = [0.49, 0.4, 0.25, 0.1, 0.0, -0.25, -0.5, -2.0]
    terms = [_overlap_term(_validation_at(g), CLEARANCE, "area") for g in gaps]
    assert terms == sorted(terms)
    assert terms[0] < terms[-1]
    assert terms[-1] == pytest.approx(25.0)


def test_the_formula_is_the_documented_one():
    """depth = max(0, clearance - gap); unit = min(depth/clearance, 1)."""
    for gap in (0.4, 0.25, 0.0, -0.2):
        unit = min(max(0.0, CLEARANCE - gap) / CLEARANCE, 1.0)
        assert (_overlap_term(_validation_at(gap), CLEARANCE, "area")
                == pytest.approx(OVERLAP_PAIR_PENALTY * unit))


def test_two_shallow_pairs_can_cost_less_than_one_deep_one():
    """⭐⭐ The ordering inversion the count objective cannot express, and the
    only reason a depth grader can change a search trajectory at all."""
    two_shallow = 2 * _overlap_term(_validation_at(0.45), CLEARANCE, "area")
    one_deep = _overlap_term(_validation_at(-1.0), CLEARANCE, "area")
    assert two_shallow < one_deep
    # ⛔ ... and under the shipped count the same comparison goes the other way.
    assert (2 * _overlap_term(_validation_at(0.45), CLEARANCE, "count")
            > _overlap_term(_validation_at(-1.0), CLEARANCE, "count"))


# --------------------------------------------------------------------------- #
# 4. ⛔⛔ A pair with no AABB gap is charged the FULL cost
# --------------------------------------------------------------------------- #
def test_a_pair_with_no_measurable_gap_is_charged_the_full_binary_cost():
    """⛔ ``overlap_gaps`` returns ``None`` for a through-board pad collision
    (opposite sides of the board) and for an index-less result. Charging it less
    would make a real pad collision cheaper than it is today."""
    hand_built = ValidationResult(overlaps=[("U1", "U2"), ("U3", "U4")])
    assert hand_built._gap_index is None
    assert overlap_gaps(hand_built) == [("U1", "U2", None), ("U3", "U4", None)]
    assert _overlap_term(hand_built, CLEARANCE, "area") == pytest.approx(50.0)
    assert _overlap_term(hand_built, CLEARANCE, "count") == 50.0


def test_overlap_gaps_reads_the_index_validate_already_built():
    """⭐ The accessor must cost one lookup per OVERLAPPING pair, never a walk
    over all pairs -- and it must not touch the lazy ``gap_pairs`` cache, whose
    eager form measured +18 % on a whole search."""
    validation = _validation_at(0.25)
    rows = overlap_gaps(validation)
    assert len(rows) == 1
    assert rows[0][0] == "U1" and rows[0][1] == "U2"
    assert rows[0][2] == pytest.approx(0.25)
    assert validation._gap_pairs_cache is None


def test_a_zero_clearance_falls_back_to_the_count():
    """⛔ ``unit`` divides by ``clearance_mm``. At zero there is no scale to
    measure depth against, so the honest answer is the binary one."""
    validation = _validation_at(-1.0)
    assert _overlap_term(validation, 0.0, "area") == 25.0


# --------------------------------------------------------------------------- #
# 5. ⛔⛔ `ok` is untouched, and illegal never outranks legal
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("objective", OVERLAP_OBJECTIVES)
@pytest.mark.parametrize("gap", [2.0, 0.499, -0.1, -3.0])
def test_ok_is_unchanged_by_the_objective(gap, objective):
    circuit, placed = _pair_at(gap)
    validation = validate(placed, circuit, BBOXES, clearance_mm=CLEARANCE)
    score = _score(gap, objective)
    assert validation.ok == (gap >= CLEARANCE)
    assert score.ok == validation.ok
    assert score.overlap_count == len(validation.overlaps)


def test_an_illegal_board_is_never_cheaper_than_a_legal_one_on_ok():
    """⛔⛔ The invariant that makes the whole thing safe: a depth grader may
    change *which illegal board* the search prefers, never whether a board is
    legal. ⚠ On ``penalty`` alone a barely-illegal board CAN now undercut a
    legal one -- which is exactly why ``ok`` is a separate boolean and why every
    ordering key in the engine reads it first."""
    legal = _score(2.0, "area")
    barely_illegal = _score(0.499, "area")
    assert legal.ok and not barely_illegal.ok
    assert barely_illegal.overlap_count == 1


def test_layout_score_ok_still_reads_the_count_not_the_term():
    source = inspect.getsource(LayoutScore.ok.fget)
    assert "self.overlap_count == 0" in source
    assert "overlap_objective" not in source


# --------------------------------------------------------------------------- #
# 6. ⛔ The seams deliberately NOT threaded
# --------------------------------------------------------------------------- #
def test_the_ordering_keys_are_still_counts():
    """⛔ This is lever B1 (the penalty) and NOT B2 (the ordering key). B2 is
    the larger change and was not built -- pinned so a later reader does not
    assume it was, and so building it is a deliberate act."""
    from skidl_layout.refinement import _hard_count, _hard_violation_key

    for fn in (_hard_count, _hard_violation_key):
        assert "overlap_objective" not in inspect.signature(fn).parameters
        assert "overlap_count" in inspect.getsource(fn)


def test_the_trial_pre_filter_stays_unthreaded():
    """⛔ ``_rank_and_limit_trials`` computes no penalty and calls no scorer, so
    there is no overlap term there to grade. ⚠ Its own box-intersection test is
    still BINARY -- a named limitation of this lever, not an oversight."""
    from skidl_layout.refinement import _rank_and_limit_trials

    assert ("overlap_objective"
            not in inspect.signature(_rank_and_limit_trials).parameters)


# --------------------------------------------------------------------------- #
# 7. The resolver, and the worker boundary
# --------------------------------------------------------------------------- #
def test_resolver_precedence_and_strictness(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_OVERLAP_OBJECTIVE", raising=False)
    assert _resolve_overlap_objective(None) == "count"
    assert _resolve_overlap_objective("AREA") == "area"

    monkeypatch.setenv("SKIDL_LAYOUT_OVERLAP_OBJECTIVE", "area")
    assert _resolve_overlap_objective(None) == "area"
    assert _resolve_overlap_objective("count") == "count"

    monkeypatch.setenv("SKIDL_LAYOUT_OVERLAP_OBJECTIVE", "")
    assert _resolve_overlap_objective(None) == "count"


def test_unknown_objective_raises_rather_than_falling_back():
    """⛔ A typo that silently selected the shipped objective would make an A/B
    read as 'no effect', which is the one failure mode a graded lever cannot
    survive. Sixth resolver, sixth identical reason."""
    for typo in ("depth", "areas", "overlap_area"):
        with pytest.raises(ValueError):
            _resolve_overlap_objective(typo)


def test_worker_payload_unpacks_slot_11_and_tolerates_short_payloads():
    from skidl_layout.parallel import refine_candidate_worker

    source = inspect.getsource(refine_candidate_worker)
    assert "overlap_objective=overlap_objective" in source
    assert "fields[11]" in source
    assert "len(fields) > 11" in source


def test_the_short_payload_path_actually_defaults(monkeypatch):
    """⭐ Asserted by EXECUTION, not by reading the source: a payload pickled
    before slot 11 existed must still load and keep ``count``."""
    import pickle

    from skidl_layout import parallel
    from skidl_layout.context import LayoutContext

    from tests.test_hpwl_weights import _PickleStub

    captured = {}

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return args[0]

    monkeypatch.setattr("skidl_layout.engine._refine_candidate_trio", _spy)
    monkeypatch.setattr(LayoutContext, "from_circuit",
                        staticmethod(lambda _snapshot: None))

    # Eleven fields: the pre-slot-11 payload shape.
    payload = pickle.dumps((_PickleStub(), _PickleStub(), {}, {}, 0.5, 2,
                            "mst", False, "centroid", "all", "legacy"))
    parallel.refine_candidate_worker(payload)
    assert captured["overlap_objective"] == "count"
    assert captured["hpwl_weights"] == "legacy"


def test_plan_layout_exposes_the_knob():
    import skidl_layout as SL

    params = inspect.signature(SL.plan_layout).parameters
    assert "overlap_objective" in params
    assert params["overlap_objective"].default is None  # None -> env -> "count"


def test_plan_params_carries_it_across_the_pickled_boundary():
    """⛔ ``_FinalizeParams`` is what ``plan_candidate_worker`` unpickles. A
    field missing here is invisible until a >= 30-part board disagrees with a
    < 30-part one -- which is exactly how the ``crossing_objective`` promotion
    corrupted the two biggest boards while 1100 tests passed."""
    fields = {f.name: f for f in dataclasses.fields(_FinalizeParams)}
    assert "overlap_objective" in fields
    assert fields["overlap_objective"].default == "count"


def test_the_knob_is_not_on_the_snapshot_fields():
    """⛔ ``_SNAPSHOT_FIELDS`` is for *circuit* fields (``layout_cell``,
    ``escape_room``). An objective is a plan parameter and travels on
    ``_FinalizeParams``; putting it on both would be two sources of truth."""
    from skidl_layout.snapshot import _SNAPSHOT_FIELDS

    assert "overlap_objective" not in _SNAPSHOT_FIELDS


def test_both_scorers_honour_the_knob():
    """⭐⭐ The asymmetry with ``hpwl_weights`` is deliberate: the quick scorer
    carries its OWN ``len(overlaps) * 25.0``, so leaving it binary would rank
    pre-filter candidates on a different overlap scale from the full scorer."""
    for quick in (False, True):
        count = _score(0.499, "count", quick=quick)
        area = _score(0.499, "area", quick=quick)
        assert count.penalty - area.penalty == pytest.approx(24.95)
        assert count.overlap_count == area.overlap_count == 1
