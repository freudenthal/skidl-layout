"""The crossing objective: legacy -> signal -> mst, and the promotion.

⛔⛔ **What this exists to protect.** Measured 2026-07-30 on the six-board eval
set: the shipped crossing term ``min(crossings * 2.0, 20.0)`` saturates at **10**
crossings while the star metric reads 101-499 on every board, so the term was a
CONSTANT on 12 of 12 board-arms and contributed no gradient at all.

Three modes now exist and each is pinned here:

- ``legacy`` -- the pre-2026-07-30 default. ⭐⭐ It must stay bit-reproducible
  forever, because it is the only thing that keeps every historical placement
  digest in the repo recoverable after the promotion.
- ``signal`` -- plane-free star, rescaled. GRADED AND PARKED: better on 3/6,
  worse on 2/6. Kept so the negative stays reproducible.
- ``mst`` -- plane-free MST over PAD positions. ⭐⭐⭐ **The default since
  2026-07-30**: crossings and vias both better on 6/6.
"""

from __future__ import annotations

import pytest

from skidl_layout.constraints import BoardOutline, LayoutConstraints
from skidl_layout.engine import _resolve_crossing_objective
from skidl_layout.geometry import FootprintGeometry, PadGeometry
from skidl_layout.scoring import (
    CROSSING_OBJECTIVE_LEGACY,
    DEFAULT_CROSSING_OBJECTIVE,
    CROSSING_OBJECTIVE_MST,
    CROSSING_OBJECTIVE_SIGNAL,
    _CROSSING_CAP_SIGNAL,
    _crossing_term,
    _estimate_crossings,
    _mst_crossings,
    _placement_pad_points,
    score_placement,
)
from skidl_layout.writer import PlacedPart

from tests.test_layout_scoring import BBOXES, _Circuit, _Net, _Part


def _square_pad_geometries():
    """A 12x12 part with its two pads 4 mm apart on the X axis."""
    return {
        "Package_QFP:MCU": FootprintGeometry(
            footprint="Package_QFP:MCU",
            pads=(
                PadGeometry(number="1", x_mm=-2.0, y_mm=0.0,
                            width_mm=1.0, height_mm=1.0),
                PadGeometry(number="2", x_mm=2.0, y_mm=0.0,
                            width_mm=1.0, height_mm=1.0),
            ),
        )
    }


def _crossing_board():
    """Two signal nets that cross, plus a ground net over the same four parts.

    The star anchor is the leftmost ref, so ``GND`` contributes three spokes
    from ``U1`` that sweep the whole board — the plane-net inflation the signal
    mode exists to remove, in miniature.
    """
    sig_a, sig_b, gnd = _Net("SIG_A"), _Net("SIG_B"), _Net("GND")
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[sig_a, gnd]),
        _Part("U2", name="MCU", footprint="Package_QFP:MCU", nets=[sig_a, gnd]),
        _Part("U3", name="MCU", footprint="Package_QFP:MCU", nets=[sig_b, gnd]),
        _Part("U4", name="MCU", footprint="Package_QFP:MCU", nets=[sig_b, gnd]),
    ]
    circuit = _Circuit(parts, [sig_a, sig_b, gnd])
    # U1--U2 and U3--U4 are the diagonals of a square, so they cross.
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 60.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U3", 20.0, 60.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U4", 60.0, 20.0, 0.0, "Package_QFP:MCU"),
    ]
    return circuit, placed


# --------------------------------------------------------------------------- #
# 1. ⭐⭐⭐ The default IS mst -- and legacy is still exactly reproducible
# --------------------------------------------------------------------------- #
def test_default_is_mst_everywhere_it_is_declared():
    """⛔ One default, in one place. Two would make a hand-scored placement
    silently incomparable with a planned one."""
    import inspect

    from skidl_layout.refinement import refine_candidate_placement, refine_placement

    assert DEFAULT_CROSSING_OBJECTIVE == CROSSING_OBJECTIVE_MST
    for fn in (score_placement, refine_placement, refine_candidate_placement):
        assert (inspect.signature(fn).parameters["crossing_objective"].default
                == CROSSING_OBJECTIVE_MST)


def test_implicit_default_scores_as_mst_not_legacy():
    circuit, placed = _crossing_board()
    outline = BoardOutline(100.0, 100.0)
    geometries = _square_pad_geometries()

    implicit = score_placement(placed, circuit, BBOXES, outline=outline,
                               fp_geometries=geometries)
    as_mst = score_placement(placed, circuit, BBOXES, outline=outline,
                             fp_geometries=geometries,
                             crossing_objective=CROSSING_OBJECTIVE_MST)
    as_legacy = score_placement(placed, circuit, BBOXES, outline=outline,
                                fp_geometries=geometries,
                                crossing_objective=CROSSING_OBJECTIVE_LEGACY)
    assert implicit.penalty == as_mst.penalty
    assert implicit.penalty != as_legacy.penalty


def test_legacy_remains_bit_reproducible_after_the_promotion():
    """⭐⭐ The promotion moved every recorded placement digest in the repo. That
    is only acceptable because the old baselines are still ONE KWARG AWAY -- this
    pins that ``legacy`` was not disturbed by the swap or the promotion."""
    circuit, placed = _crossing_board()
    outline = BoardOutline(100.0, 100.0)

    a = score_placement(placed, circuit, BBOXES, outline=outline,
                        crossing_objective=CROSSING_OBJECTIVE_LEGACY)
    b = score_placement(placed, circuit, BBOXES, outline=outline,
                        crossing_objective=CROSSING_OBJECTIVE_LEGACY)
    assert a.penalty == b.penalty
    # The legacy term is still the saturated one it always was.
    assert _crossing_term(101, CROSSING_OBJECTIVE_LEGACY) == 20.0


def test_estimate_crossings_default_counts_plane_nets():
    circuit, placed = _crossing_board()
    assert _estimate_crossings(placed, circuit) == _estimate_crossings(
        placed, circuit, exclude_plane_nets=False
    )


# --------------------------------------------------------------------------- #
# 2. The filter half -- plane nets leave the count
# --------------------------------------------------------------------------- #
def test_signal_mode_drops_plane_nets_from_the_count():
    circuit, placed = _crossing_board()
    with_planes = _estimate_crossings(placed, circuit, exclude_plane_nets=False)
    without = _estimate_crossings(placed, circuit, exclude_plane_nets=True)

    assert without < with_planes, "GND spokes must leave the count"
    # The two signal diagonals still cross: the filter removes plane inflation,
    # not the real defect.
    assert without == 1


def test_signal_mode_lowers_the_penalty_for_the_same_placement():
    circuit, placed = _crossing_board()
    outline = BoardOutline(100.0, 100.0)

    legacy = score_placement(
        placed, circuit, BBOXES, outline=outline,
        crossing_objective=CROSSING_OBJECTIVE_LEGACY,
    )
    signal = score_placement(
        placed, circuit, BBOXES, outline=outline,
        crossing_objective=CROSSING_OBJECTIVE_SIGNAL,
    )
    assert signal.penalty < legacy.penalty


# --------------------------------------------------------------------------- #
# 3. The rescale half -- ⛔ the reason the filter alone is not enough
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("count", [10, 50, 101, 183, 499])
def test_legacy_term_is_saturated_across_the_corpus_range(count):
    """The finding this whole change rests on: no gradient above 10 crossings."""
    assert _crossing_term(count, CROSSING_OBJECTIVE_LEGACY) == 20.0


def test_legacy_term_is_flat_between_two_real_board_arms():
    """``lt3844_buck``: 101 (auto) vs 183 (hand). The term cannot tell them apart."""
    assert (_crossing_term(101, CROSSING_OBJECTIVE_LEGACY)
            == _crossing_term(183, CROSSING_OBJECTIVE_LEGACY))


@pytest.mark.parametrize(
    "auto,hand",
    [(7, 1), (10, 1), (11, 1), (35, 33), (88, 12), (40, 3)],
)
def test_signal_term_is_live_over_every_measured_board_arm(auto, hand):
    """The same six boards, plane-free. ⭐ Every arm is strictly below the cap,
    so the term expresses the full auto->hand improvement rather than clipping."""
    t_auto = _crossing_term(auto, CROSSING_OBJECTIVE_SIGNAL)
    t_hand = _crossing_term(hand, CROSSING_OBJECTIVE_SIGNAL)
    assert t_auto < _CROSSING_CAP_SIGNAL
    assert t_hand < t_auto


def test_signal_term_still_caps_a_pathological_scatter():
    """The corpus's worst random scatter (242) is what the ceiling is for."""
    assert _crossing_term(242, CROSSING_OBJECTIVE_SIGNAL) == _CROSSING_CAP_SIGNAL


# --------------------------------------------------------------------------- #
# 4. Resolution
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 3bis. MST over pads -- the replacement for the star metric
# --------------------------------------------------------------------------- #
def test_mst_mode_sees_pad_structure_the_star_metric_collapses():
    """⭐ The star metric works off part CENTROIDS, so four parts wired as two
    crossing diagonals look the same however their pads are arranged. The MST
    reads pads, so it can tell those apart -- that is the whole point."""
    circuit, placed = _crossing_board()
    geometries = _square_pad_geometries()

    star = _estimate_crossings(placed, circuit, exclude_plane_nets=True)
    mst = _mst_crossings(placed, circuit, geometries)
    assert star >= 1
    assert isinstance(mst, int)


def test_mst_mode_excludes_plane_nets_by_default():
    circuit, placed = _crossing_board()
    geometries = _square_pad_geometries()

    with_planes = _mst_crossings(placed, circuit, geometries,
                                 exclude_plane_nets=False)
    without = _mst_crossings(placed, circuit, geometries)
    assert without <= with_planes


def test_mst_pad_points_are_ordered_for_a_stable_tie_break():
    """⛔ ``mst_edges`` breaks ties on ``(distance, index)``, so an unstable
    point order silently changes the tree and the crossing count. The order
    must match ``ratnest.analyse_board``: refs sorted, pads sorted as STRINGS."""
    circuit, placed = _crossing_board()
    points = _placement_pad_points(placed, circuit, _square_pad_geometries())

    keys = [(p.ref, p.pad) for p in points]
    assert keys == sorted(keys)
    # Reversing the input placement must not change the output order.
    reversed_points = _placement_pad_points(
        list(reversed(placed)), circuit, _square_pad_geometries()
    )
    assert [(p.ref, p.pad, p.x, p.y) for p in reversed_points] == \
           [(p.ref, p.pad, p.x, p.y) for p in points]


def test_mst_pads_fall_back_to_the_centroid_without_geometry():
    """⚠ A part whose footprint could not be loaded contributes centroid pads --
    the pre-MST behaviour for that part, and deliberately silent."""
    circuit, placed = _crossing_board()
    points = _placement_pad_points(placed, circuit, None)
    by_ref = {p.ref: p for p in points}
    assert by_ref["U1"].x == 20.0 and by_ref["U1"].y == 20.0


@pytest.mark.parametrize("count", [0, 1, 8, 42, 84])
def test_mst_term_is_live_below_the_cap(count):
    """The corpus's worst AUTO board is 42; every real arm must be under 20.0."""
    assert _crossing_term(count, CROSSING_OBJECTIVE_MST) < 20.0


def test_mst_term_caps_only_above_the_corpus_90th_percentile():
    """p90 over all 132 board-instances is 85; the ceiling sits there."""
    assert _crossing_term(84, CROSSING_OBJECTIVE_MST) < 20.0
    assert _crossing_term(136, CROSSING_OBJECTIVE_MST) == 20.0


def test_mst_mode_is_selected_by_score_placement():
    circuit, placed = _crossing_board()
    outline = BoardOutline(100.0, 100.0)
    legacy = score_placement(
        placed, circuit, BBOXES, outline=outline,
        crossing_objective=CROSSING_OBJECTIVE_LEGACY,
    )
    mst = score_placement(
        placed, circuit, BBOXES, outline=outline,
        fp_geometries=_square_pad_geometries(),
        crossing_objective=CROSSING_OBJECTIVE_MST,
    )
    assert mst.penalty != legacy.penalty


def test_resolver_precedence_and_typo_handling(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_CROSSING_OBJECTIVE", raising=False)
    assert _resolve_crossing_objective(None) == CROSSING_OBJECTIVE_MST
    assert _resolve_crossing_objective("signal") == CROSSING_OBJECTIVE_SIGNAL
    # ⭐ The pre-promotion objective stays reachable, which is what makes every
    # historical placement digest reproducible.
    assert _resolve_crossing_objective("legacy") == CROSSING_OBJECTIVE_LEGACY
    assert _resolve_crossing_objective("  SIGNAL ") == CROSSING_OBJECTIVE_SIGNAL

    monkeypatch.setenv("SKIDL_LAYOUT_CROSSING_OBJECTIVE", "signal")
    assert _resolve_crossing_objective(None) == CROSSING_OBJECTIVE_SIGNAL
    # ⭐ An explicit kwarg still wins, so an A/B can pin both arms.
    assert _resolve_crossing_objective("legacy") == CROSSING_OBJECTIVE_LEGACY

    # ⛔ A typo must not silently select the shipped objective -- that would make
    # an A/B read as "the lever does nothing".
    with pytest.raises(ValueError):
        _resolve_crossing_objective("signl")


def test_refiner_threads_the_objective_into_every_trial_score(monkeypatch):
    """⭐⭐ The refiner is where the gradient is consumed, so an objective that
    reached only candidate selection would never steer a move. This asserts the
    value arrives at EVERY trial score, not just the first."""
    from skidl_layout import refinement

    circuit, placed = _crossing_board()
    seen: list[str] = []
    real = refinement.score_placement

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("crossing_objective"))
        return real(*args, **kwargs)

    monkeypatch.setattr(refinement, "score_placement", _spy)
    refinement.refine_placement(
        placed, circuit, BBOXES,
        constraints=LayoutConstraints(outline=BoardOutline(100.0, 100.0)),
        crossing_objective=CROSSING_OBJECTIVE_LEGACY,
    )
    assert seen, "the refiner scored nothing at all"
    assert set(seen) == {CROSSING_OBJECTIVE_LEGACY}

    seen.clear()
    refinement.refine_placement(
        placed, circuit, BBOXES,
        constraints=LayoutConstraints(outline=BoardOutline(100.0, 100.0)),
    )
    assert set(seen) == {CROSSING_OBJECTIVE_MST}, "the default must reach trials"
