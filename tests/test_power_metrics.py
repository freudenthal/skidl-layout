# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.power_metrics` -- power-layout Phase 2, Stage A.

Three halves, in increasing order of how much machinery they need:

* **Polygon primitives** on synthetic point sets, including the bow tie that is
  the whole reason ``effective_area_mm2`` exists.
* **The metrics** over a hand-placed LT3757 boost, both pad-accurate (footprint
  geometry present) and centroid-only (absent).
* **Scramble invariance** -- every number must come out numerically identical on
  a ref- and net-scrambled twin. The metrics take names as *keys*, never as
  *signals*; this is the same anti-cheat gate ``power_roles`` holds to, and it
  is cheap now and expensive later.
"""

from __future__ import annotations

import math

import pytest

from scramble import scramble_circuit
from skidl_layout.layout_quality import layout_quality
from skidl_layout.power_metrics import (
    LoopGeometry,
    PowerMetrics,
    _convex_hull_area,
    _self_intersects,
    _shoelace,
    measure_power_layout,
)
from skidl_layout.power_roles import classify_power_roles
from skidl_layout.writer import PlacedPart

from test_power_roles import _boost, _non_power_board, requires_symbols


# --------------------------------------------------------------------------- #
# WS-1: the polygon primitives
# --------------------------------------------------------------------------- #

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
#: The same four corners visited in an order that crosses -- two lobes that
#: circulate oppositely, so the signed shoelace partly cancels.
BOW_TIE = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (10.0, 10.0)]
COLLINEAR = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (15.0, 0.0)]


def test_shoelace_measures_a_known_square():
    assert abs(_shoelace(SQUARE)) == pytest.approx(100.0)


def test_a_bow_tie_is_detected_and_a_simple_polygon_is_not():
    assert _self_intersects(BOW_TIE) is True
    assert _self_intersects(SQUARE) is False
    assert _self_intersects(COLLINEAR) is False


def test_the_bow_tie_cancels_and_the_hull_does_not():
    """The measured failure mode, in miniature.

    The bow tie's shoelace reads 0 over the same four corners whose square reads
    100 -- a naive loop-area term would call it a perfect layout.
    """
    assert abs(_shoelace(BOW_TIE)) == pytest.approx(0.0, abs=1e-9)
    assert _convex_hull_area(BOW_TIE) == pytest.approx(100.0)


def test_convex_hull_of_a_degenerate_loop_is_zero_not_an_error():
    assert _convex_hull_area(COLLINEAR) == pytest.approx(0.0)
    assert _convex_hull_area([(0.0, 0.0), (1.0, 1.0)]) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Loop geometry over synthetic placements
# --------------------------------------------------------------------------- #

class _Stage:
    """The slice of ``PowerStage`` the metrics read, without building a netlist."""

    def __init__(self, loops, **kwargs):
        self.controller_ref = kwargs.get("controller_ref")
        self.topology = kwargs.get("topology", "boost")
        self.loops = loops
        self.switch_node_nets = kwargs.get("switch_node_nets", [])
        self.input_rail = kwargs.get("input_rail")
        self.output_rail = kwargs.get("output_rail")
        self.ground_net = kwargs.get("ground_net")
        self.feedback_net = kwargs.get("feedback_net")
        self.sense_net = kwargs.get("sense_net")
        self.feedback_divider = kwargs.get("feedback_divider")
        self.sense_resistor_ref = kwargs.get("sense_resistor_ref")
        self.small_signal_refs = kwargs.get("small_signal_refs", [])
        self._kinds = kwargs.get("kinds", {})

    def refs_of_kind(self, kind):
        return [ref for ref, k in self._kinds.items() if k == kind]


class _Loop:
    def __init__(self, member_refs, bulk_refs=()):
        self.member_refs = list(member_refs)
        self.bulk_refs = list(bulk_refs)


class _Plan:
    def __init__(self, stages):
        self.stages = list(stages)


def _placed(mapping):
    return [
        PlacedPart(ref=ref, x_mm=x, y_mm=y, rot_deg=0.0, footprint="")
        for ref, (x, y) in mapping.items()
    ]


def _measure_loop(mapping, refs, bulk=()):
    plan = _Plan([_Stage([_Loop(refs, bulk)])])
    metrics = measure_power_layout(_placed(mapping), plan)
    return metrics.stages[0].loops[0]


def test_a_square_loop_measures_its_own_area_and_perimeter():
    loop = _measure_loop(
        dict(zip("ABCD", SQUARE)), ["A", "B", "C", "D"]
    )
    assert loop.area_mm2 == pytest.approx(100.0)
    assert loop.self_intersecting is False
    assert loop.effective_area_mm2 == pytest.approx(100.0)
    assert loop.perimeter_mm == pytest.approx(40.0)
    assert loop.longest_edge_mm == pytest.approx(10.0)
    assert set(loop.edges_mm) == {"A-B", "B-C", "C-D", "D-A"}


def test_a_bow_tie_loop_reports_the_hull_as_its_effective_area():
    """The anti-bow-tie contract, which gate G2 checks on a real board."""
    loop = _measure_loop(dict(zip("ABCD", BOW_TIE)), ["A", "B", "C", "D"])
    assert loop.self_intersecting is True
    assert loop.area_mm2 == pytest.approx(0.0, abs=1e-9)
    assert loop.convex_hull_area_mm2 == pytest.approx(100.0)
    # NOT the shoelace: a bow tie can never report as the tightest loop found.
    assert loop.effective_area_mm2 == pytest.approx(100.0)


def test_a_collinear_loop_is_measured_not_crashed_on():
    loop = _measure_loop(dict(zip("ABCD", COLLINEAR)), ["A", "B", "C", "D"])
    assert loop.area_mm2 == pytest.approx(0.0, abs=1e-9)
    assert loop.self_intersecting is False
    assert loop.perimeter_mm == pytest.approx(30.0)


def test_a_three_member_loop_is_a_triangle():
    loop = _measure_loop(
        {"A": (0.0, 0.0), "B": (6.0, 0.0), "C": (0.0, 8.0)}, ["A", "B", "C"]
    )
    assert loop.area_mm2 == pytest.approx(24.0)
    assert loop.perimeter_mm == pytest.approx(24.0)
    assert loop.longest_edge_mm == pytest.approx(10.0)


def test_fewer_than_three_members_is_none_never_zero():
    """``0.0`` would read as a perfect loop. Absence must read as absence."""
    loop = _measure_loop({"A": (0.0, 0.0), "B": (6.0, 0.0)}, ["A", "B"])
    assert loop.area_mm2 is None
    assert loop.effective_area_mm2 is None
    assert loop.perimeter_mm is None
    assert loop.longest_edge_mm is None


def test_an_unplaced_member_is_none_and_warned_about():
    plan = _Plan([_Stage([_Loop(["A", "B", "C", "MISSING"])])])
    metrics = measure_power_layout(
        _placed({"A": (0.0, 0.0), "B": (6.0, 0.0), "C": (0.0, 8.0)}), plan
    )
    assert metrics.stages[0].loops[0].area_mm2 is None
    assert any("MISSING" in w for w in metrics.warnings)


def test_no_stage_means_no_metrics_and_an_empty_summary():
    assert measure_power_layout([], None).stages == []
    empty = measure_power_layout([], _Plan([]))
    assert empty.stages == []
    assert empty.summary() == ""
    assert empty.to_dict() == {"stages": [], "warnings": []}


def test_looseness_is_none_without_footprint_geometry():
    """No geometry, no normalizer -- and therefore no ``HOT_LOOP_AREA``.

    Staying silent is the intended behavior: a raw-mm2 fallback would be exactly
    the un-normalized threshold ``HIGH_CONGESTION`` was calibrated away from.
    """
    loop = _measure_loop(dict(zip("ABCD", SQUARE)), ["A", "B", "C", "D"])
    assert loop.min_perimeter_mm is None
    assert loop.looseness_ratio is None
    assert loop.perimeter_ratio is None


# --------------------------------------------------------------------------- #
# WS-2: the distance metrics, on a real netlist
# --------------------------------------------------------------------------- #

#: A Figure-11-shaped floorplan: the commutation loop's own coordinates are the
#: canary's, the rest is arranged the way the datasheet asks -- small signal on
#: the controller's side, switch node opposite. Kept small and explicit so the
#: expected distances below are checkable by hand rather than by re-running the
#: engine. It is NOT variant B's placement; the driver measures that one.
_FLOORPLAN = {
    "U1": (13.0, 13.0),
    "COUT3": (24.0, 26.5),
    "D1": (31.5, 27.0),
    "M1": (27.0, 19.0),
    "RS": (19.5, 19.0),
    "COUT1": (24.0, 31.0),
    "COUT2": (31.0, 31.0),
    "CIN": (6.0, 26.0),
    "L1": (25.0, 9.0),
    "R1": (11.0, 15.0),
    "R2": (11.0, 17.3),
    "RC": (6.0, 9.0),
    "RT": (6.0, 12.0),
    "CC2": (9.0, 6.0),
    "CVCC": (17.0, 6.0),
}


def _boost_metrics(placements=None, fp_geometries=None):
    circuit = _boost()
    plan = classify_power_roles(circuit)
    placed = _placed(placements or _FLOORPLAN)
    return measure_power_layout(
        placed, plan, circuit=circuit, fp_geometries=fp_geometries
    ), circuit, plan


def test_centroid_fallback_measures_everything_it_can_and_says_it_is_not_pad_accurate():
    metrics, _, _ = _boost_metrics()
    stage = metrics.stages[0]
    assert stage.pad_accurate is False

    loop = stage.loops[0]
    assert loop.member_refs == ["COUT3", "D1", "M1", "RS"]
    # The Phase-0 hand floorplan's own loop, to three decimals.
    assert loop.area_mm2 == pytest.approx(57.0)
    assert loop.self_intersecting is False
    assert loop.perimeter_mm == pytest.approx(32.942, abs=1e-3)

    # Divider spacing is centroid-to-centroid either way.
    assert stage.fb_node["fb_top_to_fb_bottom_mm"] == pytest.approx(2.3)
    # No pads, so the FB "pad" distance degrades to the controller centroid.
    assert stage.fb_node["pad_accurate"] is False
    assert stage.fb_node["fb_top_to_fb_pad_mm"] == pytest.approx(
        math.hypot(13.0 - 11.0, 13.0 - 15.0)
    )
    assert stage.sense_return["sense_r_to_switch_mm"] == pytest.approx(7.5)
    assert stage.sense_return["sense_r_to_controller_gnd_pad_mm"] is None


def test_switch_node_span_reads_the_parts_off_the_netlist_not_a_list():
    metrics, _, _ = _boost_metrics()
    switch = metrics.stages[0].switch_node
    assert switch["net"] == "SW"
    assert set(switch["refs"]) == {"L1", "M1", "D1"}
    # L1 (25, 9), M1 (27, 19), D1 (31.5, 27) -> a 6.5 x 18 mm bounding box.
    assert switch["span_mm"] == pytest.approx(
        math.hypot(31.5 - 25.0, 27.0 - 9.0), abs=1e-3
    )


def test_small_signal_separation_is_a_floor_and_names_its_worst_pair():
    metrics, _, _ = _boost_metrics()
    ss = metrics.stages[0].small_signal
    assert ss["min_mm"] is not None
    a, b = ss["worst_pair"].split("-")
    assert ss["pairs_mm"][ss["worst_pair"]] == pytest.approx(ss["min_mm"])
    assert ss["min_mm"] == pytest.approx(min(ss["pairs_mm"].values()))
    assert b in {"L1", "M1", "D1"}


def test_kelvin_tap_finds_the_nearest_part_on_the_output_rail():
    metrics, _, _ = _boost_metrics()
    kelvin = metrics.stages[0].kelvin
    # R1 at (11, 15); COUT3 at (24, 26.5) is the nearest VOUT part to it
    # (17.4mm, against COUT1's 20.6mm) -- and it is an output capacitor, which
    # is what the datasheet asks for.
    assert kelvin["fb_top"] == "R1"
    assert kelvin["neighbour_ref"] == "COUT3"
    assert kelvin["taps_output_cap"] is True


def test_kelvin_tap_flags_a_rectifier_neighbour():
    """Move the divider next to the diode: the same code must now object."""
    placements = dict(_FLOORPLAN, R1=(31.5, 28.0))
    metrics, _, _ = _boost_metrics(placements)
    kelvin = metrics.stages[0].kelvin
    assert kelvin["neighbour_ref"] == "D1"
    assert kelvin["taps_output_cap"] is False
    assert kelvin["nearest_output_cap_ref"] in {"COUT1", "COUT2", "COUT3"}


def test_bulk_cap_distance_is_measured_over_loop_plus_bulk():
    """Phase-1 limitation L-2: the bulk members count as loop anchors.

    ``COUT1``/``COUT2`` are the loop capacitor's tie-break losers, so scoring
    against ``member_refs`` alone would charge the placement for a choice it
    never made. They are anchors, so they never appear as offenders.
    """
    metrics, _, _ = _boost_metrics()
    bulk = metrics.stages[0].bulk_caps
    assert "COUT1" not in bulk["distances_mm"]
    assert "COUT2" not in bulk["distances_mm"]
    assert bulk["worst_ref"] == "CIN"


# --------------------------------------------------------------------------- #
# WS-3: scramble invariance (plan gate G3)
# --------------------------------------------------------------------------- #

def _numbers(metrics):
    """Every float the measurement produced, with names stripped out."""
    out = []
    for stage in metrics.stages:
        for loop in stage.loops:
            out.append((loop.area_mm2, loop.effective_area_mm2,
                        loop.convex_hull_area_mm2, loop.perimeter_mm,
                        loop.longest_edge_mm, loop.self_intersecting,
                        sorted(round(v, 9) for v in loop.edges_mm.values())))
        out.append((
            stage.fb_node.get("fb_top_to_fb_pad_mm"),
            stage.fb_node.get("fb_top_to_fb_bottom_mm"),
            stage.sense_return.get("sense_r_to_switch_mm"),
            stage.sense_return.get("sense_r_to_controller_gnd_pad_mm"),
            stage.small_signal.get("min_mm"),
            stage.switch_node.get("span_mm"),
            stage.kelvin.get("neighbour_mm"),
            stage.kelvin.get("taps_output_cap"),
            stage.bulk_caps.get("max_mm"),
        ))
    return out


def test_every_metric_is_identical_on_a_ref_and_net_scrambled_twin():
    circuit = _boost()
    twin, ref_map, _net_map = scramble_circuit(circuit)

    placed = _placed(_FLOORPLAN)
    twin_placed = _placed({ref_map[ref]: xy for ref, xy in _FLOORPLAN.items()})

    original = measure_power_layout(
        placed, classify_power_roles(circuit), circuit=circuit
    )
    scrambled = measure_power_layout(
        twin_placed, classify_power_roles(twin), circuit=twin
    )
    assert _numbers(original) == _numbers(scrambled)


# --------------------------------------------------------------------------- #
# WS-5: the layout_quality codes
# --------------------------------------------------------------------------- #

class _FakeResult:
    def __init__(self, power_metrics):
        self.power_metrics = power_metrics
        self.validation = None
        self.score = None
        self.placed_parts = []
        self.outline = None


def test_the_hand_floorplan_trips_no_power_advisory():
    """The calibration contract: silent on anything as good as variant B."""
    metrics, _, _ = _boost_metrics()
    codes = {i.code for i in layout_quality(_FakeResult(metrics)).issues}
    assert codes == set()


def test_a_scattered_placement_trips_the_loop_and_kelvin_codes():
    scattered = dict(
        _FLOORPLAN,
        COUT3=(19.5, 11.0), D1=(15.5, 26.8), M1=(16.9, 20.0), RS=(30.4, 25.8),
        R1=(14.5, 26.0),
    )
    metrics, _, _ = _boost_metrics(scattered)
    issues = layout_quality(_FakeResult(metrics)).issues
    codes = {i.code for i in issues}
    assert "HOT_LOOP_SELF_INTERSECTING" in codes
    assert "KELVIN_TAP_TARGET" in codes
    # Advisory only: a mediocre hot loop never blocks a board.
    assert all(not i.blocking for i in issues)
    assert layout_quality(_FakeResult(metrics)).ok is True


def test_every_power_threshold_can_be_disabled():
    scattered = dict(_FLOORPLAN, RS=(45.0, 45.0), R1=(31.9, 27.4))
    metrics, _, _ = _boost_metrics(scattered)
    off = layout_quality(
        _FakeResult(metrics),
        hot_loop_looseness_threshold=None,
        fb_node_threshold_mm=None,
        sense_return_threshold_mm=None,
    )
    assert {i.code for i in off.issues} <= {
        "HOT_LOOP_SELF_INTERSECTING", "KELVIN_TAP_TARGET"
    }


def test_a_board_with_no_power_metrics_is_untouched():
    assert layout_quality(_FakeResult(None)).issues == []
    assert layout_quality(_FakeResult(PowerMetrics())).issues == []


# --------------------------------------------------------------------------- #
# WS-4: the LayoutResult wiring
# --------------------------------------------------------------------------- #

@requires_symbols
def test_plan_layout_attaches_the_metrics_without_moving_anything():
    from skidl_layout import plan_layout

    def digest(res):
        return [
            (p.ref, round(p.x_mm, 6), round(p.y_mm, 6), round(p.rot_deg, 3), p.side)
            for p in sorted(res.placed_parts, key=lambda p: p.ref)
        ]

    result = plan_layout(_boost(), fp_lib_dirs=[])
    assert result.power_metrics is not None
    assert result.power_metrics.stages[0].loops[0].member_refs == [
        "COUT3", "D1", "M1", "RS"
    ]
    assert "power_metrics" in result.to_dict()
    assert "Power layout metrics" in result.summary()
    assert digest(plan_layout(_boost(), fp_lib_dirs=[])) == digest(result)


@requires_symbols
def test_plan_layout_stays_silent_on_a_non_power_board():
    from skidl_layout import plan_layout

    result = plan_layout(_non_power_board(), fp_lib_dirs=[])
    assert result.power_metrics is not None
    assert result.power_metrics.stages == []
    assert "power_metrics" not in result.to_dict()
    assert "Power layout metrics" not in result.summary()


# --------------------------------------------------------------------------- #
# WS-8: Stage B -- the opt-in scorer term
# --------------------------------------------------------------------------- #

def test_the_flag_resolves_kwarg_then_env_then_off(monkeypatch):
    from skidl_layout.engine import _resolve_power_score

    monkeypatch.delenv("SKIDL_LAYOUT_POWER_SCORE", raising=False)
    assert _resolve_power_score(None) is False
    assert _resolve_power_score(True) is True

    monkeypatch.setenv("SKIDL_LAYOUT_POWER_SCORE", "1")
    assert _resolve_power_score(None) is True
    # An explicit kwarg always beats the env var.
    assert _resolve_power_score(False) is False

    for falsey in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("SKIDL_LAYOUT_POWER_SCORE", falsey)
        assert _resolve_power_score(None) is False


def test_the_hand_floorplan_earns_no_power_penalty():
    from skidl_layout.engine import _power_loop_penalty

    metrics, _, _ = _boost_metrics()
    penalty, findings = _power_loop_penalty(metrics)
    assert penalty == 0.0
    assert findings == []


def test_a_bad_placement_earns_a_bounded_power_penalty():
    from skidl_layout.engine import _POWER_TOTAL_CAP, _power_loop_penalty

    scattered = dict(
        _FLOORPLAN,
        COUT3=(60.0, 60.0), D1=(0.0, 60.0), M1=(60.0, 0.0), RS=(0.0, 0.0),
        R1=(14.5, 26.0), CC2=(26.0, 19.5),
    )
    metrics, _, _ = _boost_metrics(scattered)
    penalty, findings = _power_loop_penalty(metrics)
    assert penalty > 0.0
    assert penalty <= _POWER_TOTAL_CAP
    assert findings


def test_the_term_moves_penalty_not_only_score():
    """The single most important line in Stage B.

    Candidate selection keys on ``-finalized.score.penalty``; the two adjusters
    that ship before this one move only ``score.score`` and therefore cannot
    influence which candidate wins. A term that copied them would pass every
    other test in this file and change zero placements.
    """
    from skidl_layout.engine import _apply_power_loop_score
    from skidl_layout.scoring import LayoutScore

    circuit = _boost()
    scattered = dict(
        _FLOORPLAN, COUT3=(60.0, 60.0), D1=(0.0, 60.0), M1=(60.0, 0.0),
        RS=(0.0, 0.0), R1=(14.5, 26.0),
    )

    class _Ctx:
        power_stage_plan = None

        def power_stage_plan_for(self, ckt):
            return classify_power_roles(ckt)

    before = LayoutScore(score=100.0, penalty=12.0)
    after = _apply_power_loop_score(
        before, _placed(scattered), circuit, _Ctx(), None, True
    )
    assert after.penalty > before.penalty
    assert after.score < before.score
    assert after.warnings


def test_the_term_is_a_true_no_op_when_off():
    from skidl_layout.engine import _apply_power_loop_score
    from skidl_layout.scoring import LayoutScore

    class _ExplodingCtx:
        def power_stage_plan_for(self, ckt):  # pragma: no cover - must not run
            raise AssertionError("the term classified with the flag OFF")

    score = LayoutScore(score=100.0, penalty=12.0)
    assert _apply_power_loop_score(
        score, _placed(_FLOORPLAN), _boost(), _ExplodingCtx(), None, False
    ) is score


def test_the_context_memoizes_the_stage_plan():
    from skidl_layout.context import LayoutContext

    ctx = LayoutContext()
    circuit = _boost()
    first = ctx.power_stage_plan_for(circuit)
    assert first.stages
    assert ctx.power_stage_plan_for(circuit) is first
