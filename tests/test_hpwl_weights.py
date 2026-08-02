"""The plane-net WEIGHT: GND 2.0 / POWER 1.6 (shipped) -> a named dose table.

⛔⛔ **What this exists to protect, and it is a MEASURED premise, not a wish.**
On nine frozen control placements (2026-08-01) ``hpwl_nets="plane_free"`` makes
:func:`~skidl_layout.scoring._weighted_hpwl` a **byte-identical duplicate** of
:func:`~skidl_layout.scoring._total_hpwl` on 9 of 9 boards -- because the only
weights ``_net_weight`` ever returns above 1.0 on this corpus are the two plane
ones (a power-converter board carries no USB, clock or crystal net at all). So
``_net_weight`` **IS** the plane predicate wearing different clothes, and
"keep the planes" / "drop the planes" are the mildest and the most extreme dose
of **one** lever.

⭐⭐ **``hpwl_nets`` sets the axis to zero; ``hpwl_weights`` is the dose on it.**
And the dose can say something removal cannot: ``ratnest.is_plane_net`` names
``VIN``/``VOUT``/``VCC``, which ``power._strategy`` **trunks** rather than pours,
so a poured ground and a trunked supply want different coefficients.
``"trace_aware"`` (0.5 / 1.6) is exactly that, and no net-set choice can express
it.

Six things are pinned here, in the order they can fail:

1. ⭐⭐ **The default is a TRUE no-op.** ``"legacy"`` must reproduce every
   historical number and stay the default in every place the default is
   declared -- two defaults would make a hand-scored placement silently
   incomparable with a planned one.
2. ⭐⭐⭐ **The weighted term scales EXACTLY with the dose**, and the raw term
   does not move at all. ``total_hpwl`` being a dose invariant is what makes a
   decomposition of the two terms readable.
3. ⛔ **Only the two plane coefficients move.** The 1.5 signal-token weight and
   the 1.0 floor are the same under every dose; the table would otherwise be a
   sweep wearing a lever's clothes.
4. ⛔⛔ **The extreme dose is NOT reachable from this table.** ``0.0/0.0``
   *is* ``hpwl_nets="plane_free"``, which is graded and parked; a dose table
   that could reach it would silently re-run a finished experiment.
5. ⛔ **Two seams are deliberately NOT threaded, and both are facts rather than
   judgement calls** -- ``score_placement_quick`` has no weighted term at all,
   and ``_rank_and_limit_trials`` / ``validator._compute_hpwl`` rank on
   *unweighted* HPWL. Pinned so a later reader does not "fix" them.
6. ⛔⛔ **The worker boundary**, slot 10 -- the ``crossing_objective`` defect's
   fifth home. Parallelism engages at >= 30 parts, so an unthreaded knob
   corrupts exactly the two biggest boards and leaves the small ones perfect.
"""

from __future__ import annotations

import inspect

import pytest

from skidl_layout.constraints import BoardOutline
from skidl_layout.engine import _resolve_hpwl_weights
from skidl_layout.scoring import (
    DEFAULT_HPWL_WEIGHTS,
    HPWL_NETS_PLANE_FREE,
    HPWL_OBJECTIVE_PADS,
    HPWL_WEIGHT_DOSES,
    HPWL_WEIGHTS,
    _net_pad_extents,
    _net_weight,
    _placement_pad_points,
    _total_hpwl,
    _weight_pair,
    _weighted_hpwl,
    score_placement,
)
from skidl_layout.writer import PlacedPart

from tests.test_hpwl_objective import _wide_pad_geometries
from tests.test_layout_scoring import BBOXES, _Circuit, _Net, _Part


class _PickleStub:
    """A picklable stand-in for the candidate/snapshot in a worker payload."""

    parts: list = []

    def get_nets(self):
        return []


def _gnd_only_board():
    """Two parts sharing exactly one net, and it is GROUND.

    ⭐ One net means the weighted term is ``hpwl x weight`` with nothing else in
    the sum, so "scales exactly with the dose" is an equality rather than a
    trend. The centroids are 40 mm apart, so the raw term is 40.0 under every
    dose.
    """
    gnd = _Net("GND")
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[gnd]),
        _Part("U2", name="MCU", footprint="Package_QFP:MCU", nets=[gnd]),
    ]
    circuit = _Circuit(parts, [gnd])
    placed = [
        PlacedPart("U1", 20.0, 40.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 40.0, 0.0, "Package_QFP:MCU"),
    ]
    return circuit, placed


def _three_class_board():
    """One ground net, one supply net and one signal net, all the same length.

    ⭐ Equal lengths are the point: every difference between two doses is then
    attributable to a coefficient and not to geometry, which is what lets
    ``trace_aware`` be distinguished from ``quarter`` by arithmetic.
    """
    gnd, vin, sig = _Net("GND"), _Net("VIN"), _Net("SW")
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU",
              nets=[gnd, vin, sig]),
        _Part("U2", name="MCU", footprint="Package_QFP:MCU",
              nets=[gnd, vin, sig]),
    ]
    circuit = _Circuit(parts, [gnd, vin, sig])
    placed = [
        PlacedPart("U1", 20.0, 40.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 40.0, 0.0, "Package_QFP:MCU"),
    ]
    return circuit, placed


# --------------------------------------------------------------------------- #
# 1. The default is "legacy", everywhere, and it is a true no-op
# --------------------------------------------------------------------------- #
def test_default_is_legacy_everywhere_it_is_declared():
    from skidl_layout.refinement import refine_candidate_placement, refine_placement

    assert DEFAULT_HPWL_WEIGHTS == "legacy"
    for fn in (score_placement, refine_placement, refine_candidate_placement):
        assert (inspect.signature(fn).parameters["hpwl_weights"].default
                == "legacy")


def test_legacy_reproduces_the_shipped_coefficients():
    """⛔ The one row that must never move: it is every historical number."""
    assert HPWL_WEIGHTS["legacy"] == (2.0, 1.6)
    assert _net_weight("GND") == 2.0
    assert _net_weight("VIN") == 1.6
    assert _net_weight("USB_DP") == 1.5
    assert _net_weight("SW") == 1.0


def test_implicit_default_equals_an_explicit_legacy():
    """⛔ A knob whose OFF value differs from its absence is not an opt-in, it
    is a second default."""
    circuit, placed = _three_class_board()
    outline = BoardOutline(100.0, 100.0)

    implicit = score_placement(placed, circuit, BBOXES, outline=outline)
    explicit = score_placement(placed, circuit, BBOXES, outline=outline,
                               hpwl_weights="legacy")
    assert implicit.total_hpwl_mm == explicit.total_hpwl_mm
    assert implicit.weighted_hpwl_mm == explicit.weighted_hpwl_mm
    assert implicit.penalty == explicit.penalty


# --------------------------------------------------------------------------- #
# 2. ⭐⭐⭐ The weighted term scales with the dose; the raw term does not move
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dose", sorted(HPWL_WEIGHTS))
def test_a_gnd_only_boards_weighted_term_scales_exactly_with_the_dose(dose):
    circuit, placed = _gnd_only_board()
    gnd_weight, _power = HPWL_WEIGHTS[dose]

    assert _total_hpwl(placed, circuit) == pytest.approx(40.0)
    assert _weighted_hpwl(placed, circuit,
                          hpwl_weights=dose) == pytest.approx(40.0 * gnd_weight)


@pytest.mark.parametrize("dose", sorted(HPWL_WEIGHTS))
def test_the_raw_term_is_a_dose_INVARIANT(dose):
    """⭐⭐ The whole decomposition rests on this. ``hpwl_weights`` reaches the
    *weighted* term only, so any movement in ``total_hpwl_mm`` across doses
    would mean the knob had leaked into the wrong sum."""
    circuit, placed = _three_class_board()
    outline = BoardOutline(100.0, 100.0)

    legacy = score_placement(placed, circuit, BBOXES, outline=outline)
    dosed = score_placement(placed, circuit, BBOXES, outline=outline,
                            hpwl_weights=dose)
    assert dosed.total_hpwl_mm == pytest.approx(legacy.total_hpwl_mm)


def test_trace_aware_moves_ground_and_leaves_the_supply_alone():
    """⭐⭐⭐ The dose §1.1 argues for, and the one no net-set can express:
    ``is_plane_net`` names both ``GND`` and ``VIN``, but the stack POURS the
    first and TRUNKS the second."""
    circuit, placed = _three_class_board()
    # Three nets, each 40 mm: GND, VIN, SW.
    assert _total_hpwl(placed, circuit) == pytest.approx(120.0)

    legacy = _weighted_hpwl(placed, circuit)                 # 2.0 + 1.6 + 1.0
    trace_aware = _weighted_hpwl(placed, circuit, hpwl_weights="trace_aware")
    quarter = _weighted_hpwl(placed, circuit, hpwl_weights="quarter")

    assert legacy == pytest.approx(40.0 * (2.0 + 1.6 + 1.0))
    assert trace_aware == pytest.approx(40.0 * (0.5 + 1.6 + 1.0))
    assert quarter == pytest.approx(40.0 * (0.5 + 0.5 + 1.0))
    # ⛔ And the two are genuinely different levers, not one under two names.
    assert trace_aware != pytest.approx(quarter)


def test_the_dose_composes_with_both_other_hpwl_knobs():
    """⭐ Three orthogonal knobs: the POINTS, the SET and the DOSE.

    ⚠ Under ``plane_free`` the dose must become a **no-op** -- the nets it
    weights are exactly the nets that set removes. That is not a coincidence to
    tolerate, it is the measured premise (§1.1) stated as an assertion.
    """
    circuit, placed = _three_class_board()
    geometries = _wide_pad_geometries()
    extents = _net_pad_extents(
        _placement_pad_points(placed, circuit, geometries))

    for objective, kwargs in ((HPWL_OBJECTIVE_PADS, {"pad_extents": extents}),
                              ("centroid", {})):
        dosed = {
            dose: _weighted_hpwl(placed, circuit, hpwl_objective=objective,
                                 hpwl_weights=dose, **kwargs)
            for dose in HPWL_WEIGHTS
        }
        assert len(set(round(v, 9) for v in dosed.values())) == len(HPWL_WEIGHTS)

        plane_free = {
            dose: _weighted_hpwl(placed, circuit, hpwl_objective=objective,
                                 hpwl_nets=HPWL_NETS_PLANE_FREE,
                                 hpwl_weights=dose, **kwargs)
            for dose in HPWL_WEIGHTS
        }
        assert len(set(round(v, 9) for v in plane_free.values())) == 1


def test_plane_free_makes_the_weighted_term_a_duplicate_of_the_raw_one():
    """⛔⛔ The corpus finding this whole plan rests on, in miniature: with the
    planes gone, ``_net_weight`` returns 1.0 for everything left, so the two
    differently-shaped objective terms become one number scored twice."""
    circuit, placed = _three_class_board()
    raw = _total_hpwl(placed, circuit, hpwl_nets=HPWL_NETS_PLANE_FREE)
    weighted = _weighted_hpwl(placed, circuit, hpwl_nets=HPWL_NETS_PLANE_FREE)
    assert raw == pytest.approx(weighted)


# --------------------------------------------------------------------------- #
# 3-4. ⛔ Only the plane coefficients move, and the extreme dose is unreachable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dose", sorted(HPWL_WEIGHTS))
def test_only_the_two_plane_coefficients_move(dose):
    assert _net_weight("USB_DP", dose) == 1.5
    assert _net_weight("SCK", dose) == 1.0
    assert _net_weight("GND", dose) == HPWL_WEIGHTS[dose][0]
    assert _net_weight("VIN", dose) == HPWL_WEIGHTS[dose][1]


def test_no_dose_reaches_zero():
    """⛔⛔ ``0.0/0.0`` is ``hpwl_nets="plane_free"``, which is already graded
    and PARKED. A dose table that could reach it would silently re-run a
    finished experiment under a new name."""
    assert all(gnd > 0.0 and power > 0.0
               for gnd, power in HPWL_WEIGHTS.values())


def test_no_dose_raises_a_plane_weight_above_legacy():
    """⛔ The lever is *re-weight downwards*; an upward dose is a different
    experiment with a different guard."""
    for gnd, power in HPWL_WEIGHTS.values():
        assert gnd <= 2.0 and power <= 1.6


def test_weight_pair_accepts_a_name_or_a_resolved_pair():
    assert _weight_pair("quarter") == (0.5, 0.5)
    assert _weight_pair((0.5, 1.6)) == (0.5, 1.6)
    assert _net_weight("GND", (0.25, 0.25)) == 0.25
    with pytest.raises(ValueError):
        _weight_pair("half")


# --------------------------------------------------------------------------- #
# 5. ⛔ The two seams that are deliberately NOT threaded
# --------------------------------------------------------------------------- #
def test_the_quick_scorer_has_no_weighted_term_to_dose():
    """⛔ Not an omission -- ``score_placement_quick``'s only HPWL contribution
    is ``min(total_hpwl / 50.0, 30.0)``, and the raw term is a dose invariant.
    Threading the knob there would be dead code."""
    from skidl_layout.scoring import score_placement_quick

    assert "hpwl_weights" not in inspect.signature(
        score_placement_quick).parameters
    source = inspect.getsource(score_placement_quick)
    assert "_weighted_hpwl" not in source


def test_the_unweighted_seams_stay_unweighted():
    """⛔ ``_rank_and_limit_trials`` and ``validator._compute_hpwl`` both rank
    on *unweighted* half-perimeters. ``hpwl_nets`` reaches both (dropping a net
    removes a term from the sum); a dose has nothing there to change."""
    from skidl_layout.refinement import _rank_and_limit_trials
    from skidl_layout.validator import _compute_hpwl

    for fn in (_rank_and_limit_trials, _compute_hpwl):
        params = inspect.signature(fn).parameters
        assert "hpwl_nets" in params
        assert "hpwl_weights" not in params


def test_the_move_generators_own_net_weight_is_untouched():
    """⭐⭐ There are FOUR ``_net_weight`` functions and this plan moves exactly
    one. ``refinement``'s feeds ``_ref_neighbors`` -- the weighted neighbour
    centroid the move trials AIM at -- so the search still aims at
    plane-weighted targets and is then scored plane-light. That inconsistency is
    measured as a report-only arm and is deliberately not shipped."""
    from skidl_layout import congestion, orientation, refinement

    assert refinement._net_weight("GND") == 2.0
    assert refinement._net_weight("VIN") == 1.7
    assert congestion._net_weight("GND") == 1.8
    assert orientation._net_weight("GND") == 2.4
    for module in (refinement, congestion, orientation):
        assert "hpwl_weights" not in inspect.signature(
            module._net_weight).parameters


# --------------------------------------------------------------------------- #
# 6. The resolver, and the worker boundary
# --------------------------------------------------------------------------- #
def test_resolver_precedence_and_strictness(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_HPWL_WEIGHTS", raising=False)
    assert _resolve_hpwl_weights(None) == "legacy"
    assert _resolve_hpwl_weights("TRACE_AWARE") == "trace_aware"

    monkeypatch.setenv("SKIDL_LAYOUT_HPWL_WEIGHTS", "quarter")
    assert _resolve_hpwl_weights(None) == "quarter"
    assert _resolve_hpwl_weights("legacy") == "legacy"

    monkeypatch.setenv("SKIDL_LAYOUT_HPWL_WEIGHTS", "")
    assert _resolve_hpwl_weights(None) == "legacy"


def test_unknown_dose_raises_rather_than_falling_back():
    """⛔ A typo that silently selected the shipped dose would make an A/B read
    as 'no effect', which is the one failure mode a graded lever cannot
    survive."""
    with pytest.raises(ValueError):
        _resolve_hpwl_weights("traceaware")
    assert set(HPWL_WEIGHT_DOSES) == {"legacy", "light", "quarter",
                                      "trace_aware"}


def test_worker_payload_unpacks_slot_10_and_tolerates_short_payloads():
    from skidl_layout.parallel import refine_candidate_worker

    source = inspect.getsource(refine_candidate_worker)
    assert "hpwl_weights=hpwl_weights" in source
    assert "fields[10]" in source
    assert "len(fields) > 10" in source


def test_the_short_payload_path_actually_defaults(monkeypatch):
    """⭐ Asserted by EXECUTION, not by reading the source: a payload pickled
    before slot 10 existed must still load and keep ``legacy``.

    ⚠ The stubs are module-level because the payload is genuinely pickled --
    a locally-defined class would fail at ``pickle.dumps`` and the test would
    never reach the thing it is about.
    """
    import pickle

    from skidl_layout import parallel
    from skidl_layout.context import LayoutContext

    captured = {}

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return args[0]

    monkeypatch.setattr("skidl_layout.engine._refine_candidate_trio", _spy)
    monkeypatch.setattr(LayoutContext, "from_circuit",
                        staticmethod(lambda _snapshot: None))

    # Nine fields: the pre-slot-10 payload shape.
    payload = pickle.dumps((_PickleStub(), _PickleStub(), {}, {}, 0.5, 2,
                            "mst", False, "centroid"))
    parallel.refine_candidate_worker(payload)
    assert captured["hpwl_weights"] == "legacy"
    assert captured["hpwl_nets"] == "all"


def test_plan_layout_exposes_the_knob():
    import skidl_layout as SL

    params = inspect.signature(SL.plan_layout).parameters
    assert "hpwl_weights" in params
    assert params["hpwl_weights"].default is None  # None -> env -> "legacy"


def test_plan_params_carries_it_across_the_pickled_boundary():
    """⛔ ``_FinalizeParams`` is what ``plan_candidate_worker`` unpickles. A
    field missing here is invisible until a >= 30-part board disagrees with a
    < 30-part one -- which is exactly how the ``crossing_objective`` promotion
    corrupted the two biggest boards while 1100 tests passed."""
    import dataclasses

    from skidl_layout.engine import _FinalizeParams

    fields = {f.name: f for f in dataclasses.fields(_FinalizeParams)}
    assert "hpwl_weights" in fields
    assert fields["hpwl_weights"].default == "legacy"
