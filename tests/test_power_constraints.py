# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.power_constraints` -- power-layout Phase 3.

Four halves:

* **The generator** on the boost and flyback plans (the ``test_power_roles``
  fixtures, reused exactly as ``test_power_metrics`` reuses them) -- what it
  emits, what it deliberately does not, and that every distance is derived from
  footprint geometry rather than typed in.
* **Satisfiability (gate C2)** -- the datasheet floorplan satisfies every
  generated constraint. A generated constraint the known-good layout violates is
  a bug in generation, never a reason to touch the floorplan.
* **Scramble invariance (gate C3)** -- the generated set on a ref- and
  net-scrambled twin is identical modulo the renaming, distances to 1e-9. Same
  anti-cheat gate ``power_roles`` and ``power_metrics`` hold to.
* **The flag** -- kwarg > env > OFF, and the Phase-3 coupling that makes the
  generated candidate reachable.
"""

from __future__ import annotations

import math

import pytest

from scramble import scramble_circuit
from skidl_layout.constraints import BoardOutline, LayoutConstraints
from skidl_layout.geometry import FootprintGeometry
from skidl_layout.power_constraints import (
    PowerConstraintSet,
    generate_power_constraints,
)
from skidl_layout.power_roles import classify_power_roles

from test_power_roles import _boost, _flyback, _non_power_board


# --------------------------------------------------------------------------- #
# Geometry fixtures
#
# The ``test_power_roles`` fixtures assign a footprint to the passives only, so
# the tests state their own. The sizes are the **real** KiCad courtyards of the
# footprints ``canaries/lt3757_boost`` uses, read once from the installed
# libraries and frozen here -- so the C2 assertion below is the same arithmetic
# the canary driver does, without this suite depending on KiCad being present.
# --------------------------------------------------------------------------- #

R08 = "Resistor_SMD:R_0805_2012Metric"
R20 = "Resistor_SMD:R_2010_5025Metric"
C08 = "Capacitor_SMD:C_0805_2012Metric"
C12 = "Capacitor_SMD:C_1206_3216Metric"
CP63 = "Capacitor_SMD:CP_Elec_6.3x5.4"
IND = "Inductor_SMD:L_7.3x7.3_H4.5"
SO8 = "Package_SO:PowerPAK_SO-8_Single"
SMC = "Diode_SMD:D_SMC"
MSOP = "Package_SO:MSOP-10-1EP_3x3mm_P0.5mm_EP1.68x1.88mm"

#: footprint -> (width_mm, height_mm) courtyard, KiCad 10 as installed
COURTYARDS = {
    R08: (3.410, 1.950),
    R20: (6.410, 3.210),
    C08: (3.450, 2.010),
    C12: (4.650, 2.350),
    CP63: (9.650, 7.150),
    IND: (8.450, 7.850),
    SO8: (7.150, 5.550),
    SMC: (9.850, 6.750),
    MSOP: (6.310, 3.550),
}


def _geometry(width, height):
    return FootprintGeometry(
        footprint="x", courtyard_bounds=(-width / 2, -height / 2,
                                         width / 2, height / 2)
    )


FP_GEOMETRIES = {name: _geometry(*size) for name, size in COURTYARDS.items()}


#: The boost fixture's footprint assignment, matching the canary's part for
#: part -- the 2010 sense resistor and the 1206 HF ceramic in particular, since
#: those two set the hot loop's own distance bounds.
BOOST_FOOTPRINTS = {
    "U1": MSOP, "CIN": C12, "L1": IND, "M1": SO8, "RS": R20, "D1": SMC,
    "COUT1": CP63, "COUT2": CP63, "COUT3": C12, "R1": R08, "R2": R08,
    "RC": R08, "RT": R08, "CC2": C08, "CVCC": C08,
}

FLYBACK_FOOTPRINTS = {
    "U1": MSOP, "Q1": SO8, "T1": IND, "D1": SMC, "C1": C08, "C2": C08,
    "C3": C08, "R1": R08, "R2": R08, "R3": R08, "C4": C08, "C5": C08,
    "C6": C08, "R5": R08,
}


def _radius(footprint):
    width, height = COURTYARDS[footprint]
    return math.hypot(width, height) / 2.0


def _generate(circuit, footprints, **kwargs):
    plan = classify_power_roles(circuit)
    return generate_power_constraints(
        plan, footprint_by_ref=footprints, fp_geometries=FP_GEOMETRIES, **kwargs
    ), plan


def _near_pairs(generated):
    return {(c.ref, c.target_ref) for c in generated.near}


def _far_pairs(generated):
    return {(c.ref, c.target_ref) for c in generated.far}


# --------------------------------------------------------------------------- #
# Silence
# --------------------------------------------------------------------------- #

def test_no_plan_and_no_stages_generate_nothing():
    from skidl_layout.power_roles import PowerStagePlan

    assert not generate_power_constraints(None)
    assert not generate_power_constraints(PowerStagePlan())
    assert generate_power_constraints(None).summary() == ""


def test_a_board_with_no_converter_generates_nothing():
    generated, plan = _generate(_non_power_board(), {})
    assert plan.stages == []
    assert not generated
    assert generated.summary() == ""


def test_without_footprint_geometry_nothing_is_guessed():
    """A flat-millimetre fallback is exactly what this module must not do."""
    plan = classify_power_roles(_boost())
    generated = generate_power_constraints(plan)
    assert generated.near == []
    assert generated.warnings
    assert any("no footprint geometry" in w for w in generated.warnings)


# --------------------------------------------------------------------------- #
# The hot loop
# --------------------------------------------------------------------------- #

def test_the_commutation_loop_becomes_near_constraints_hop_by_hop():
    generated, plan = _generate(_boost(), BOOST_FOOTPRINTS)
    assert plan.stages[0].loops[0].member_refs == ["COUT3", "D1", "M1", "RS"]
    # Three hops for four members -- the polygon is NOT closed (see the
    # docstring of _generate_near_loop): the loop returns through the ground
    # plane, and the closing edge is the one constraint variant B violates.
    assert ("RS", "COUT3") not in _near_pairs(generated)
    assert ("COUT3", "RS") not in _near_pairs(generated)
    assert {("D1", "COUT3"), ("D1", "M1"), ("RS", "M1")} <= _near_pairs(generated)


def test_the_more_movable_member_is_the_one_asked_to_move():
    """The placer moves ``ref`` and never ``target_ref``; refinement's gravity
    only follows a near constraint for a passive ``ref``. So a constraint naming
    the FET as ``ref`` would be a no-op waiting to happen."""
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS)
    by_ref = {c.ref for c in generated.near}
    # M1 (the switch) and U1 (the controller) are only ever targets.
    assert "M1" not in by_ref
    assert "U1" not in by_ref
    assert ("RS", "M1") in _near_pairs(generated)
    assert ("R1", "U1") in _near_pairs(generated)


def test_every_near_distance_is_the_two_courtyard_half_diagonals_plus_slack():
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS, clearance_mm=0.5)
    for constraint in generated.near:
        expected = (
            _radius(BOOST_FOOTPRINTS[constraint.ref])
            + _radius(BOOST_FOOTPRINTS[constraint.target_ref])
            + 1.0
        )
        assert constraint.distance_mm == pytest.approx(expected, abs=1e-6)


def test_clearance_widens_every_near_distance_by_twice_itself():
    tight, _ = _generate(_boost(), BOOST_FOOTPRINTS, clearance_mm=0.0)
    loose, _ = _generate(_boost(), BOOST_FOOTPRINTS, clearance_mm=1.0)
    for a, b in zip(tight.near, loose.near):
        assert b.distance_mm == pytest.approx(a.distance_mm + 2.0, abs=1e-6)


def test_a_part_with_no_geometry_skips_its_constraint_with_a_warning():
    footprints = dict(BOOST_FOOTPRINTS)
    footprints.pop("D1")
    generated, _ = _generate(_boost(), footprints)
    assert ("D1", "COUT3") not in _near_pairs(generated)
    assert ("D1", "M1") not in _near_pairs(generated)
    # The rest of the loop is unaffected.
    assert ("RS", "M1") in _near_pairs(generated)
    assert any("D1" in w for w in generated.warnings)


# --------------------------------------------------------------------------- #
# The feedback divider and the small-signal stand-off
# --------------------------------------------------------------------------- #

def test_the_divider_is_pulled_to_the_controller_top_resistor_first():
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS)
    pairs = [(c.ref, c.target_ref) for c in generated.near]
    assert ("R1", "U1") in pairs
    assert ("R2", "R1") in pairs
    # Order matters: the placer walks constraints.near once, so the top
    # resistor must settle against the controller before the bottom follows it.
    assert pairs.index(("R1", "U1")) < pairs.index(("R2", "R1"))


def test_small_signal_parts_are_pushed_from_the_switch_and_the_magnetics():
    generated, plan = _generate(_boost(), BOOST_FOOTPRINTS)
    pairs = _far_pairs(generated)
    assert set(plan.stages[0].small_signal_refs) == {"RC", "CC2", "RT", "R1", "R2"}
    for ref in plan.stages[0].small_signal_refs:
        assert (ref, "M1") in pairs
        assert (ref, "L1") in pairs
    # ... and nothing is pushed away from the controller or a capacitor.
    assert all(target in {"M1", "L1"} for _ref, target in pairs)


def test_the_far_distance_grows_with_the_aggressor_courtyard():
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS)
    by_pair = {(c.ref, c.target_ref): c.distance_mm for c in generated.far}
    assert by_pair[("RT", "M1")] == pytest.approx(10.0 + _radius(SO8), abs=1e-6)
    assert by_pair[("RT", "L1")] == pytest.approx(10.0 + _radius(IND), abs=1e-6)


def test_excluding_the_divider_from_the_far_push_is_available_but_off():
    """MEASURED both ways in the Phase-3 bake-off; the exclusion lost."""
    default, _ = _generate(_boost(), BOOST_FOOTPRINTS)
    excluded, _ = _generate(_boost(), BOOST_FOOTPRINTS,
                            far_excludes_divider=True)
    assert ("R1", "M1") in _far_pairs(default)
    assert ("R1", "M1") not in _far_pairs(excluded)
    assert ("RT", "M1") in _far_pairs(excluded)


# --------------------------------------------------------------------------- #
# What is deliberately NOT generated
# --------------------------------------------------------------------------- #

def test_no_rail_capacitor_constraint_is_generated():
    """The phase's third measured negative -- see the block comment in the
    module. Variant B places COUT2 25 mm from the rectifier on purpose."""
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS)
    pairs = _near_pairs(generated) | _far_pairs(generated)
    for bulk in ("COUT1", "COUT2", "CIN"):
        assert not any(bulk in pair for pair in pairs)


def test_the_zone_is_available_but_ships_disabled():
    outline = BoardOutline(42.0, 33.0)
    default, _ = _generate(_boost(), BOOST_FOOTPRINTS, outline=outline)
    assert default.zones == []

    zoned, _ = _generate(_boost(), BOOST_FOOTPRINTS, outline=outline, zones=True)
    assert len(zoned.zones) == 1
    zone = zoned.zones[0]
    assert zone.group_name == "power_stage_U1"
    assert {"M1", "L1", "D1", "RS", "COUT3"} <= set(zone.refs)
    assert zone.x_max - zone.x_min <= outline.width_mm + 1e-9
    assert zone.y_max - zone.y_min <= outline.height_mm + 1e-9


def test_no_zone_without_an_outline():
    zoned, _ = _generate(_boost(), BOOST_FOOTPRINTS, outline=None, zones=True)
    assert zoned.zones == []


# --------------------------------------------------------------------------- #
# The flyback -- a second topology, so nothing is boost-shaped by accident
# --------------------------------------------------------------------------- #

def test_the_flyback_loop_closes_through_the_transformer():
    generated, plan = _generate(_flyback(), FLYBACK_FOOTPRINTS)
    members = plan.stages[0].loops[0].member_refs
    assert members[1:] == ["T1", "Q1", "R1"]
    pairs = _near_pairs(generated)
    assert (members[0], "T1") in pairs
    assert ("T1", "Q1") in pairs
    assert ("R1", "Q1") in pairs
    # The transformer is the aggressor here as much as the FET is.
    assert ("R5", "T1") in _far_pairs(generated)
    assert ("R5", "Q1") in _far_pairs(generated)


# --------------------------------------------------------------------------- #
# Gate C2 -- the datasheet floorplan satisfies everything generated
# --------------------------------------------------------------------------- #

#: Variant B's hand floorplan, kept in sync with
#: ``skidl-eda/canaries/lt3757_boost/floorplan.py::FLOORPLAN``. Only the refs
#: the generator can touch are needed.
VARIANT_B = {
    "U1": (15.5, 14.0), "CC1": (5.0, 6.5), "CC2": (5.0, 10.5), "RC": (8.0, 7.5),
    "R1": (11.0, 13.0), "R2": (8.7, 13.0), "RT": (10.5, 17.0),
    "CIN": (17.5, 4.0), "L1": (29.5, 11.0), "CVCC": (20.0, 11.0),
    "RS": (19.5, 19.0), "M1": (27.0, 19.0), "D1": (31.5, 27.0),
    "COUT3": (24.0, 26.5), "COUT1": (17.0, 27.0), "COUT2": (6.5, 27.0),
}


def _distance(a, b):
    return math.hypot(VARIANT_B[a][0] - VARIANT_B[b][0],
                      VARIANT_B[a][1] - VARIANT_B[b][1])


def test_variant_b_satisfies_every_generated_constraint():
    """Gate C2, as a test rather than only as a driver line.

    This is what caught the two generation bugs the phase shipped without: the
    closing loop edge (B has 8.75 mm where the bound demanded 7.19) and the
    per-rail capacitor constraints (B has 25.0 mm where the bound demanded
    12.98). Both were removed; neither was special-cased.
    """
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS,
                             outline=BoardOutline(42.0, 33.0))
    assert generated.near, "nothing generated -- the gate would be vacuous"
    for constraint in generated.near:
        assert _distance(constraint.ref, constraint.target_ref) <= \
            constraint.distance_mm + 1e-9, f"near {constraint} fights variant B"
    for constraint in generated.far:
        assert _distance(constraint.ref, constraint.target_ref) >= \
            constraint.distance_mm - 1e-9, f"far {constraint} fights variant B"


# --------------------------------------------------------------------------- #
# Gate C3 -- scramble invariance
# --------------------------------------------------------------------------- #

def _structure(generated, ref_map=None):
    ref_map = ref_map or {}
    r = lambda x: ref_map.get(x, x)          # noqa: E731
    return (
        [(r(c.ref), r(c.target_ref), round(c.distance_mm, 9))
         for c in generated.near],
        [(r(c.ref), r(c.target_ref), round(c.distance_mm, 9))
         for c in generated.far],
    )


def test_the_generated_set_survives_ref_and_net_scrambling():
    circuit = _boost()
    twin, ref_map, _net_map = scramble_circuit(circuit)
    twin_footprints = {
        ref_map[ref]: footprint for ref, footprint in BOOST_FOOTPRINTS.items()
    }

    original, _ = _generate(circuit, BOOST_FOOTPRINTS)
    scrambled, _ = _generate(twin, twin_footprints)
    assert _structure(original, ref_map) == _structure(scrambled)


# --------------------------------------------------------------------------- #
# The result shape
# --------------------------------------------------------------------------- #

def test_every_generated_constraint_carries_its_provenance():
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS)
    for constraint in generated.near:
        assert generated.reasons[f"near {constraint.ref}->{constraint.target_ref}"]
    for constraint in generated.far:
        assert generated.reasons[f"far {constraint.ref}->{constraint.target_ref}"]


def test_to_dict_and_summary_round_trip_the_counts():
    generated, _ = _generate(_boost(), BOOST_FOOTPRINTS)
    data = generated.to_dict()
    assert len(data["near"]) == len(generated.near)
    assert len(data["far"]) == len(generated.far)
    assert "Generated power constraints" in generated.summary()
    assert set(generated.refs()) >= {"D1", "M1", "RS", "R1", "R2", "U1"}


def test_an_empty_set_is_falsey():
    assert not PowerConstraintSet()
    assert PowerConstraintSet(near=[object()])


# --------------------------------------------------------------------------- #
# Wiring: the candidate strategy and the flag
# --------------------------------------------------------------------------- #

def test_the_flag_resolves_kwarg_then_env_then_off(monkeypatch):
    from skidl_layout.engine import _resolve_power_constraints

    monkeypatch.delenv("SKIDL_LAYOUT_POWER_CONSTRAINTS", raising=False)
    assert _resolve_power_constraints(None) is False
    assert _resolve_power_constraints(True) is True

    monkeypatch.setenv("SKIDL_LAYOUT_POWER_CONSTRAINTS", "1")
    assert _resolve_power_constraints(None) is True
    assert _resolve_power_constraints(False) is False

    for falsey in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("SKIDL_LAYOUT_POWER_CONSTRAINTS", falsey)
        assert _resolve_power_constraints(None) is False


def test_power_constraints_implies_power_score_unless_asked_otherwise(monkeypatch):
    """MEASURED: without the power term in the selection key the generated
    candidate is built, refined, and never chosen -- 39.93 -> 39.93 on the
    boost. An explicit ``power_score=False`` still wins."""
    from skidl_layout.engine import _resolve_power_score

    monkeypatch.delenv("SKIDL_LAYOUT_POWER_SCORE", raising=False)
    assert _resolve_power_score(None) is False
    assert _resolve_power_score(None, implied_by=True) is True
    assert _resolve_power_score(False, implied_by=True) is False

    monkeypatch.setenv("SKIDL_LAYOUT_POWER_SCORE", "0")
    assert _resolve_power_score(None, implied_by=True) is False


def test_the_strategy_is_appended_at_the_tail_and_only_with_a_plan():
    from skidl_layout.candidates import generate_placement_candidates
    from skidl_layout.engine import extract_groups

    circuit = _boost()
    groups = extract_groups(circuit)
    bboxes = dict(COURTYARDS)
    constraints = LayoutConstraints(outline=BoardOutline(42.0, 33.0))

    without = [
        c.name
        for c in generate_placement_candidates(
            groups, constraints, bboxes, circuit=circuit
        )
    ]
    assert "power_stage_first" not in without

    plan = classify_power_roles(circuit)
    with_plan = [
        c.name
        for c in generate_placement_candidates(
            groups, constraints, bboxes, power_stage_plan=plan, circuit=circuit
        )
    ]
    # Emission order is a byte-identity hazard: the new strategy goes at the
    # very tail and everything before it is untouched.
    assert with_plan[-1] == "power_stage_first"
    assert with_plan[:-1] == without


def test_a_no_stage_board_emits_the_historical_set_even_with_a_plan():
    from skidl_layout.candidates import generate_placement_candidates
    from skidl_layout.engine import extract_groups

    circuit = _non_power_board()
    groups = extract_groups(circuit)
    plan = classify_power_roles(circuit)
    assert plan.stages == []
    without = generate_placement_candidates(groups, LayoutConstraints(), {})
    with_plan = generate_placement_candidates(
        groups, LayoutConstraints(), {}, power_stage_plan=plan, circuit=circuit
    )
    assert [c.name for c in without] == [c.name for c in with_plan]


def test_generated_constraints_never_overwrite_a_user_declared_pair():
    from skidl_layout.candidates import _with_power_stage_plan
    from skidl_layout.constraints import NearConstraint

    circuit = _boost()
    plan = classify_power_roles(circuit)
    user = LayoutConstraints(
        near=[NearConstraint("RS", "M1", 99.0)],
        outline=BoardOutline(42.0, 33.0),
    )
    built = _with_power_stage_plan(
        user, None, plan, {}, FP_GEOMETRIES, BOOST_FOOTPRINTS
    )
    rs_to_m1 = [c for c in built.near if (c.ref, c.target_ref) == ("RS", "M1")]
    assert len(rs_to_m1) == 1
    assert rs_to_m1[0].distance_mm == 99.0
    # ... and the user's own list is untouched.
    assert len(user.near) == 1
