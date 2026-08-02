"""The HPWL net SET: all nets (shipped) -> plane-free (opt-in).

⛔⛔ **What this exists to protect, and it is an INCONSISTENCY, not a wish.**
The MST promotion made the *crossing* term plane-free over pads
(``_mst_crossings`` filters on ``ratnest.is_plane_net``) and left both *HPWL*
terms all-nets. One objective, two net vocabularies -- and HPWL is the term with
all the gradient. Measured on the six frozen control placements 2026-08-01:
plane nets carry **37-68 % of the weighted HPWL** (``_net_weight`` gives GND
2.0), so on 3 of 6 boards the placer's only continuous quality signal is
majority-plane -- compaction of copper that gets **poured, not routed**.

⭐ **``hpwl_objective`` is the POINTS; ``hpwl_nets`` is the SET.** They are
orthogonal, they compose, and this file pins that they do -- the pads arm was
graded and parked with the planes still in, and the whole reason it was kept
one kwarg away is so the combination can be measured.

Five things are pinned here, in the order they can fail:

1. ⭐⭐ **The default is a TRUE no-op.** ``"all"`` must reproduce every
   historical number, and it must stay the default in every place the default is
   declared -- two defaults would make a hand-scored placement silently
   incomparable with a planned one.
2. ⭐⭐⭐ **``"plane_free"`` drops exactly the poured nets**, by the same
   predicate the crossing term already consumes, in **both** point modes and in
   **both** terms.
3. ⛔ **Nothing else about membership moves.** A net still needs two distinct
   placed refs; a net whose pins land on one part still contributes nothing.
4. ⛔⛔ **The pre-filter is filtered too.** ``_rank_and_limit_trials`` keeps the
   best 3 of up to 9 position trials and ranked them on an all-nets HPWL. The
   pads plan deliberately left that seam alone on a measured argument -- every
   trial it sees is a pure translation, so centroid and pad rankings agree. ⛔
   **Dropping a net from the SET is not a translation**, so an unfiltered
   pre-filter could discard exactly the trial a plane-free objective wanted, and
   the objective would never see it.
5. ⛔⛔ **The worker boundary**, slot 9 -- the ``crossing_objective`` defect's
   fourth home. Parallelism engages at >= 30 parts, so an unthreaded knob
   corrupts exactly the two biggest boards and leaves the small ones perfect.
"""

from __future__ import annotations

import inspect

import pytest

from skidl_layout.constraints import BoardOutline
from skidl_layout.engine import _resolve_hpwl_nets
from skidl_layout.scoring import (
    DEFAULT_HPWL_NETS,
    HPWL_NET_SETS,
    HPWL_NETS_ALL,
    HPWL_NETS_PLANE_FREE,
    HPWL_OBJECTIVE_CENTROID,
    HPWL_OBJECTIVE_PADS,
    _hpwl_by_net,
    _net_pad_extents,
    _placement_pad_points,
    _total_hpwl,
    _weighted_hpwl,
    score_placement,
)
from skidl_layout.writer import PlacedPart

from tests.test_hpwl_objective import _wide_pad_geometries
from tests.test_layout_scoring import BBOXES, _Circuit, _Net, _Part


def _mixed_board():
    """Two parts sharing one signal net **and** one ground net.

    ⭐ The two nets are deliberately the same length, so "the plane-free total is
    exactly half" is a statement about the filter and not about geometry. GND is
    weighted 2.0 and SIG 1.0, which is what makes the *weighted* reading the
    sharper one: planes are 2/3 of the weighted term here against 1/2 of the raw
    one, the same asymmetry the corpus measurement found.
    """
    sig, gnd = _Net("SIG"), _Net("GND")
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[sig, gnd]),
        _Part("U2", name="MCU", footprint="Package_QFP:MCU", nets=[sig, gnd]),
    ]
    circuit = _Circuit(parts, [sig, gnd])
    placed = [
        PlacedPart("U1", 20.0, 40.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 40.0, 0.0, "Package_QFP:MCU"),
    ]
    return circuit, placed


# --------------------------------------------------------------------------- #
# 1. The default is "all", everywhere, and it is a true no-op
# --------------------------------------------------------------------------- #
def test_default_is_all_everywhere_it_is_declared():
    from skidl_layout.refinement import refine_candidate_placement, refine_placement

    assert DEFAULT_HPWL_NETS == HPWL_NETS_ALL
    for fn in (score_placement, refine_placement, refine_candidate_placement):
        assert (inspect.signature(fn).parameters["hpwl_nets"].default
                == HPWL_NETS_ALL)


def test_implicit_default_equals_an_explicit_all():
    """⛔ A knob whose OFF value differs from its absence is not an opt-in, it
    is a second default."""
    circuit, placed = _mixed_board()
    outline = BoardOutline(100.0, 100.0)

    implicit = score_placement(placed, circuit, BBOXES, outline=outline)
    explicit = score_placement(placed, circuit, BBOXES, outline=outline,
                               hpwl_nets=HPWL_NETS_ALL)
    assert implicit.total_hpwl_mm == explicit.total_hpwl_mm
    assert implicit.weighted_hpwl_mm == explicit.weighted_hpwl_mm
    assert implicit.penalty == explicit.penalty


# --------------------------------------------------------------------------- #
# 2. ⭐⭐⭐ plane-free drops exactly the poured nets -- both terms, both modes
# --------------------------------------------------------------------------- #
def test_plane_free_drops_the_ground_net_from_both_terms():
    circuit, placed = _mixed_board()
    outline = BoardOutline(100.0, 100.0)

    all_nets = score_placement(placed, circuit, BBOXES, outline=outline)
    plane_free = score_placement(placed, circuit, BBOXES, outline=outline,
                                 hpwl_nets=HPWL_NETS_PLANE_FREE)

    # SIG and GND each span 40 mm between the two centroids.
    assert all_nets.total_hpwl_mm == pytest.approx(80.0)
    assert plane_free.total_hpwl_mm == pytest.approx(40.0)
    # ⭐ GND is weighted 2.0, so it is 2/3 of the weighted term against 1/2 of
    # the raw one -- the corpus's 37-68 % asymmetry in miniature.
    assert all_nets.weighted_hpwl_mm == pytest.approx(120.0)
    assert plane_free.weighted_hpwl_mm == pytest.approx(40.0)


def test_plane_free_uses_the_same_predicate_the_crossing_term_uses():
    """⛔ One definition of 'plane net' for the whole objective, or the two
    terms drift apart again -- which is the defect this plan exists to fix."""
    from skidl_layout.ratnest import is_plane_net

    circuit, placed = _mixed_board()
    kept = {name for name, _ in _hpwl_by_net(
        placed, circuit, hpwl_nets=HPWL_NETS_PLANE_FREE)}
    dropped = {name for name, _ in _hpwl_by_net(placed, circuit)} - kept
    assert dropped and all(is_plane_net(n) for n in dropped)
    assert not any(is_plane_net(n) for n in kept)


def test_the_two_knobs_are_orthogonal_and_compose():
    """⭐⭐ Four readings, four values. ``hpwl_objective`` moves the points and
    ``hpwl_nets`` moves the set; if any pair collapsed, one knob would be
    silently absorbing the other and the 2x2 grade would be uninterpretable.

    ⚠ **U2 is TURNED, and it has to be.** With both parts flat and carrying the
    two nets on the same pad numbers, the pad offsets cancel exactly and the
    pads arm reports the centroid number -- the fixture would pass the
    orthogonality claim while exercising nothing. (It did, on the first draft;
    the same trap ``_two_part_board``'s docstring records.)
    """
    circuit, flat = _mixed_board()
    placed = [flat[0], PlacedPart("U2", 60.0, 40.0, 90.0, "Package_QFP:MCU")]
    outline = BoardOutline(100.0, 100.0)
    geometries = _wide_pad_geometries()

    readings = {
        (objective, nets): score_placement(
            placed, circuit, BBOXES, outline=outline,
            fp_geometries=geometries,
            hpwl_objective=objective, hpwl_nets=nets).total_hpwl_mm
        for objective in (HPWL_OBJECTIVE_CENTROID, HPWL_OBJECTIVE_PADS)
        for nets in (HPWL_NETS_ALL, HPWL_NETS_PLANE_FREE)
    }
    assert len(set(readings.values())) == 4, readings
    # And the plane-free half is a strict subset sum of the all-nets half in
    # BOTH point modes -- the filter removes nets, it does not re-measure them.
    for objective in (HPWL_OBJECTIVE_CENTROID, HPWL_OBJECTIVE_PADS):
        assert (readings[(objective, HPWL_NETS_PLANE_FREE)]
                < readings[(objective, HPWL_NETS_ALL)])


def test_plane_free_in_pads_mode_drops_the_same_net():
    circuit, placed = _mixed_board()
    geometries = _wide_pad_geometries()
    extents = _net_pad_extents(
        _placement_pad_points(placed, circuit, geometries))

    all_nets = dict(_hpwl_by_net(placed, circuit,
                                hpwl_objective=HPWL_OBJECTIVE_PADS,
                                pad_extents=extents))
    plane_free = dict(_hpwl_by_net(placed, circuit,
                                   hpwl_objective=HPWL_OBJECTIVE_PADS,
                                   hpwl_nets=HPWL_NETS_PLANE_FREE,
                                   pad_extents=extents))
    assert set(all_nets) - set(plane_free) == {"GND"}
    # ⛔ The nets that SURVIVE must be measured identically -- the filter is a
    # filter, not a re-scale.
    assert all(plane_free[n] == pytest.approx(all_nets[n]) for n in plane_free)


def test_a_board_that_is_all_planes_scores_zero_hpwl_plane_free():
    """⚠ The failure mode §1.2 predicts, in its purest form: with the planes
    gone there is no compaction pressure left in the HPWL terms at all. The
    plan's bail-out 3 exists for exactly this, and the guard is the TOTAL-length
    column against the random control."""
    gnd, vcc = _Net("GND"), _Net("VCC")
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[gnd, vcc]),
        _Part("U2", name="MCU", footprint="Package_QFP:MCU", nets=[gnd, vcc]),
    ]
    circuit = _Circuit(parts, [gnd, vcc])
    placed = [PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
              PlacedPart("U2", 90.0, 90.0, 0.0, "Package_QFP:MCU")]

    assert _total_hpwl(placed, circuit) > 0.0
    assert _total_hpwl(placed, circuit,
                       hpwl_nets=HPWL_NETS_PLANE_FREE) == 0.0
    assert _weighted_hpwl(placed, circuit,
                          hpwl_nets=HPWL_NETS_PLANE_FREE) == 0.0


# --------------------------------------------------------------------------- #
# 3. ⛔ Exactly one thing changes -- membership is otherwise untouched
# --------------------------------------------------------------------------- #
def test_a_signal_net_on_one_part_still_contributes_nothing():
    solo = _Net("SOLO")
    part = _Part("U1", name="MCU", footprint="Package_QFP:MCU",
                 nets=[solo, solo])
    circuit = _Circuit([part], [solo])
    placed = [PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU")]

    assert _total_hpwl(placed, circuit,
                       hpwl_nets=HPWL_NETS_PLANE_FREE) == 0.0


def test_the_report_and_the_objective_say_one_number():
    """⭐ The ``E0`` lesson, third application: a 'worst nets' report whose top
    rows are planes the objective is no longer optimising would actively
    mislead about what the placer was trying to do."""
    from skidl_layout.validator import _compute_hpwl

    circuit, placed = _mixed_board()
    all_nets = {n for n, _h, _r in _compute_hpwl(placed, circuit)}
    plane_free = {n for n, _h, _r in _compute_hpwl(
        placed, circuit, hpwl_nets=HPWL_NETS_PLANE_FREE)}
    assert "GND" in all_nets and "GND" not in plane_free


# --------------------------------------------------------------------------- #
# 4. ⛔⛔ The pre-filter is filtered too
# --------------------------------------------------------------------------- #
def test_the_trial_pre_filter_drops_plane_nets_under_plane_free():
    """⛔⛔ ``_rank_and_limit_trials`` runs BEFORE the objective sees anything.
    A ref whose only long net is GND would rank its nine trial positions
    entirely on plane geometry, and the three survivors would be chosen by a
    measure the objective has been told to ignore."""
    from skidl_layout.refinement import _rank_and_limit_trials

    circuit, placed = _mixed_board()
    trials = [PlacedPart("U2", 60.0 + dx, 40.0, 0.0, "Package_QFP:MCU")
              for dx in (0.0, 5.0, -5.0, 10.0, -10.0)]

    for nets, expect_gnd in ((HPWL_NETS_ALL, True),
                             (HPWL_NETS_PLANE_FREE, False)):
        seen = {}

        def _spy(trial, ref, other_boxes, touching_nets, pos_base,
                 fp_bboxes, fp_geometries, _seen=seen):
            _seen["nets"] = {name for name, _refs in touching_nets}
            return (0, 0.0)

        import skidl_layout.refinement as R
        original = R._rank_trial
        R._rank_trial = _spy
        try:
            _rank_and_limit_trials(placed, "U2", trials, circuit, BBOXES,
                                   None, None, hpwl_nets=nets)
        finally:
            R._rank_trial = original
        assert ("GND" in seen["nets"]) is expect_gnd, nets


# --------------------------------------------------------------------------- #
# 5. The resolver, and the worker boundary
# --------------------------------------------------------------------------- #
def test_resolver_precedence_and_strictness(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_HPWL_NETS", raising=False)
    assert _resolve_hpwl_nets(None) == HPWL_NETS_ALL
    assert _resolve_hpwl_nets("PLANE_FREE") == HPWL_NETS_PLANE_FREE

    monkeypatch.setenv("SKIDL_LAYOUT_HPWL_NETS", "plane_free")
    assert _resolve_hpwl_nets(None) == HPWL_NETS_PLANE_FREE
    assert _resolve_hpwl_nets("all") == HPWL_NETS_ALL

    monkeypatch.setenv("SKIDL_LAYOUT_HPWL_NETS", "")
    assert _resolve_hpwl_nets(None) == HPWL_NETS_ALL


def test_unknown_net_set_raises_rather_than_falling_back():
    with pytest.raises(ValueError):
        _resolve_hpwl_nets("planefree")
    assert set(HPWL_NET_SETS) == {HPWL_NETS_ALL, HPWL_NETS_PLANE_FREE}


def test_worker_payload_unpacks_hpwl_nets_and_tolerates_short_payloads():
    from skidl_layout.parallel import refine_candidate_worker

    source = inspect.getsource(refine_candidate_worker)
    assert "hpwl_nets=hpwl_nets" in source
    assert "fields[9]" in source


def test_plan_layout_exposes_the_knob():
    import skidl_layout as SL

    params = inspect.signature(SL.plan_layout).parameters
    assert "hpwl_nets" in params
    assert params["hpwl_nets"].default is None  # None -> env -> "all"


def test_plan_params_carries_it_across_the_pickled_boundary():
    """⛔ ``_FinalizeParams`` is what ``plan_candidate_worker`` unpickles. A
    field missing here is invisible until a >= 30-part board disagrees with a
    < 30-part one."""
    import dataclasses

    from skidl_layout.engine import _FinalizeParams

    fields = {f.name: f for f in dataclasses.fields(_FinalizeParams)}
    assert "hpwl_nets" in fields
    assert fields["hpwl_nets"].default == HPWL_NETS_ALL
