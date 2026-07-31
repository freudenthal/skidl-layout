"""The opt-in signal-net crossing objective (metric-validation step 1).

⛔⛔ **What this exists to protect.** Measured 2026-07-30 on the six-board eval
set: the shipped crossing term ``min(crossings * 2.0, 20.0)`` saturates at **10**
crossings while the star metric reads 101-499 on every board, so the term is a
CONSTANT on 12 of 12 board-arms and contributes no gradient at all. The
``"signal"`` mode drops poured nets from the count *and* rescales the term; the
tests below pin both halves, and — first — that the default path did not move.
"""

from __future__ import annotations

import pytest

from skidl_layout.constraints import BoardOutline
from skidl_layout.engine import _resolve_crossing_objective
from skidl_layout.scoring import (
    CROSSING_OBJECTIVE_LEGACY,
    CROSSING_OBJECTIVE_SIGNAL,
    _CROSSING_CAP_SIGNAL,
    _crossing_term,
    _estimate_crossings,
    score_placement,
)
from skidl_layout.writer import PlacedPart

from tests.test_layout_scoring import BBOXES, _Circuit, _Net, _Part


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
# 1. The default did not move
# --------------------------------------------------------------------------- #
def test_default_is_legacy_and_scores_identically():
    circuit, placed = _crossing_board()
    outline = BoardOutline(100.0, 100.0)

    implicit = score_placement(placed, circuit, BBOXES, outline=outline)
    explicit = score_placement(
        placed, circuit, BBOXES, outline=outline,
        crossing_objective=CROSSING_OBJECTIVE_LEGACY,
    )
    assert implicit.penalty == explicit.penalty
    assert implicit.score == explicit.score


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

    legacy = score_placement(placed, circuit, BBOXES, outline=outline)
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
def test_resolver_precedence_and_typo_handling(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_CROSSING_OBJECTIVE", raising=False)
    assert _resolve_crossing_objective(None) == CROSSING_OBJECTIVE_LEGACY
    assert _resolve_crossing_objective("signal") == CROSSING_OBJECTIVE_SIGNAL
    assert _resolve_crossing_objective("  SIGNAL ") == CROSSING_OBJECTIVE_SIGNAL

    monkeypatch.setenv("SKIDL_LAYOUT_CROSSING_OBJECTIVE", "signal")
    assert _resolve_crossing_objective(None) == CROSSING_OBJECTIVE_SIGNAL
    # ⭐ An explicit kwarg still wins, so an A/B can pin both arms.
    assert _resolve_crossing_objective("legacy") == CROSSING_OBJECTIVE_LEGACY

    # ⛔ A typo must not silently select the shipped objective -- that would make
    # an A/B read as "the lever does nothing".
    with pytest.raises(ValueError):
        _resolve_crossing_objective("signl")


def test_refiner_accepts_the_objective_and_defaults_to_legacy():
    """⭐ The refiner is where the gradient is consumed; a change made only at
    candidate-selection time would never steer a move."""
    import inspect

    from skidl_layout.refinement import refine_candidate_placement, refine_placement

    for fn in (refine_placement, refine_candidate_placement):
        param = inspect.signature(fn).parameters["crossing_objective"]
        assert param.default == CROSSING_OBJECTIVE_LEGACY
