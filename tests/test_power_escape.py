# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.power_escape` -- power-layout Phase 13.

Five halves:

* **The geometry** -- courtyard rectangles at 0/90/45 degrees, edge-to-edge
  gaps, and the annulus, whose one non-negotiable property is that it does
  **not** cover the controller it surrounds (a keepout that blocks the
  controller's own pads is the exact opposite of the intent).
* **The lane and its source** -- derived from a :class:`FabSpec` rather than
  typed in, and carrying which of the three sources produced it.
* **The constraint generator** -- opt-in, silent when off, position-free when
  on, and correctly converting an *edge* gap into the centre-to-centre distance
  :class:`~skidl_layout.constraints.FarConstraint` actually means.
* **The enforcement pass**, which is the mechanism that ships -- it must relieve
  what it can, refuse what it cannot, and never buy room by manufacturing an
  overlap or pushing a part off the board.
* **The keepout writer** -- round-tripped through KRT's own parser, never
  eyeballed. Skipped when no KiCadRoutingTools checkout is importable.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

from skidl_layout.constraints import BoardOutline, FarConstraint
from skidl_layout.fabspec import OSHPARK_2L, FabSpec
from skidl_layout.geometry import FootprintGeometry
from skidl_layout.power_escape import (
    ESCAPE_LANE_MM,
    ESCAPE_ROOM_FIELD,
    annulus_polygon,
    apply_escape_room,
    declared_escape_refs,
    escape_far_constraints,
    lane_from_fab,
    mark_escape_room,
    measure_escape_room,
    measure_escape_rooms,
    part_rect,
    rect_gap,
    resolve_escape_targets,
    resolve_lane_mm,
    write_keepout_polygons,
)
from skidl_layout.power_roles import classify_power_roles
from skidl_layout.writer import PlacedPart

from test_power_constraints import (
    BOOST_FOOTPRINTS,
    COURTYARDS,
    FP_GEOMETRIES,
    MSOP,
    R08,
)
from test_power_roles import _boost


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _placed(ref, x, y, footprint, rot=0.0):
    return PlacedPart(ref=ref, x_mm=x, y_mm=y, rot_deg=rot, footprint=footprint)


class _Result:
    """The duck type ``measure_escape_room`` reads: placed parts and a plan."""

    def __init__(self, placed, power_stage_plan=None):
        self.placed_parts = list(placed)
        self.power_stage_plan = power_stage_plan
        self.power_plan = None


def _radius(footprint):
    width, height = COURTYARDS[footprint]
    return math.hypot(width, height) / 2.0


class _Part:
    """A skidl-ish part: a ref, a footprint and a ``fields`` dict."""

    def __init__(self, ref, footprint):
        self.ref = ref
        self.footprint = footprint
        self.fields: dict = {}


class _Circuit:
    def __init__(self, *parts):
        self.parts = list(parts)


# --------------------------------------------------------------------------- #
# the DECLARATION -- the fix for the guess that produced retraction R-1
# --------------------------------------------------------------------------- #

def test_mark_writes_the_declaration_and_returns_the_refs():
    u1, u2 = _Part("U1", MSOP), _Part("U2", MSOP)
    assert mark_escape_room(u1, u2) == ["U1", "U2"]
    assert u1.fields[ESCAPE_ROOM_FIELD] is True
    assert u2.fields[ESCAPE_ROOM_FIELD] is True


def test_mark_carries_an_explicit_lane_and_can_be_cleared():
    u1 = _Part("U1", MSOP)
    mark_escape_room(u1, lane_mm=1.25)
    assert u1.fields[ESCAPE_ROOM_FIELD] == pytest.approx(1.25)
    assert declared_escape_refs(_Circuit(u1)) == {"U1": pytest.approx(1.25)}
    mark_escape_room(u1, clear=True)
    assert declared_escape_refs(_Circuit(u1)) == {}


def test_declared_refs_are_undeclared_by_default():
    """⛔ Silence is the contract: an unmarked board declares nothing."""
    assert declared_escape_refs(_Circuit(_Part("U1", MSOP), _Part("R1", R08))) == {}


def test_declared_refs_read_the_alternate_field_holders():
    """A part loaded from a library carries ``_extra_fields``, not ``fields``."""
    part = _Part("U9", MSOP)
    del part.fields
    part._extra_fields = {ESCAPE_ROOM_FIELD: True}
    assert declared_escape_refs(_Circuit(part)) == {"U9": None}


def test_a_declaration_beats_the_classifier_and_the_pad_count():
    """⭐ The precedence that fixes R-1: the producer outranks every guess."""
    circuit = _Circuit(_Part("U1", MSOP), _Part("M1", MSOP), _Part("R1", R08))
    mark_escape_room(circuit.parts[0])
    plan = classify_power_roles(_boost())
    targets, source = resolve_escape_targets(
        placed_refs={"U1", "M1", "R1"}, circuit=circuit,
        power_stage_plan=plan, fallback="M1")
    assert targets == [("U1", None)]
    assert source == "declared"


def test_the_pad_count_guess_is_the_last_resort_and_says_so():
    targets, source = resolve_escape_targets(
        placed_refs={"U1", "M1"}, circuit=_Circuit(_Part("U1", MSOP)),
        power_stage_plan=None, fallback="M1")
    assert targets == [("M1", None)]
    assert source == "pad_count"


def test_an_explicit_ref_beats_even_a_declaration():
    circuit = _Circuit(_Part("U1", MSOP), _Part("U2", MSOP))
    mark_escape_room(circuit.parts[0])
    targets, source = resolve_escape_targets(
        placed_refs={"U1", "U2"}, circuit=circuit, controller_ref="U2")
    assert targets == [("U2", None)] and source == "explicit"


def test_targets_never_include_a_part_that_is_not_placed():
    circuit = _Circuit(_Part("U1", MSOP), _Part("U7", MSOP))
    mark_escape_room(*circuit.parts)
    targets, _source = resolve_escape_targets(placed_refs={"U1"}, circuit=circuit)
    assert targets == [("U1", None)]


def test_the_declaration_survives_a_snapshot():
    """⚠ A worker rebuilds context from a snapshot; an untracked attr is lost."""
    from skidl_layout.snapshot import SnapshotPart

    snap = SnapshotPart(ref="U1", name="", value="", foot="", footprint=MSOP,
                        description="", hierarchy="", pin_len=8,
                        fields={ESCAPE_ROOM_FIELD: 1.5})
    assert declared_escape_refs(_Circuit(snap)) == {"U1": pytest.approx(1.5)}


# --------------------------------------------------------------------------- #
# the geometry
# --------------------------------------------------------------------------- #

def test_part_rect_is_the_courtyard_centred_on_the_placement():
    geometry = FP_GEOMETRIES[MSOP]
    rect = part_rect(_placed("U1", 10.0, 20.0, MSOP), geometry)
    width, height = COURTYARDS[MSOP]
    assert rect == pytest.approx(
        (10.0 - width / 2, 20.0 - height / 2, 10.0 + width / 2, 20.0 + height / 2)
    )


@pytest.mark.parametrize("rot", [90.0, 270.0, -90.0])
def test_part_rect_transposes_a_quarter_turn(rot):
    """⚠ The swap is the whole reason this helper exists rather than ``bounds``."""
    geometry = FP_GEOMETRIES[MSOP]
    width, height = COURTYARDS[MSOP]
    rect = part_rect(_placed("U1", 0.0, 0.0, MSOP, rot=rot), geometry)
    assert (rect[2] - rect[0]) == pytest.approx(height)
    assert (rect[3] - rect[1]) == pytest.approx(width)


@pytest.mark.parametrize("rot", [0.0, 45.0, 180.0])
def test_part_rect_does_not_transpose_off_the_quarter_turn(rot):
    geometry = FP_GEOMETRIES[MSOP]
    width, height = COURTYARDS[MSOP]
    rect = part_rect(_placed("U1", 0.0, 0.0, MSOP, rot=rot), geometry)
    assert (rect[2] - rect[0]) == pytest.approx(width)
    assert (rect[3] - rect[1]) == pytest.approx(height)


def test_rect_gap_axis_diagonal_and_touching():
    a = (0.0, 0.0, 2.0, 2.0)
    assert rect_gap(a, (3.0, 0.0, 4.0, 2.0)) == pytest.approx(1.0)      # +x only
    assert rect_gap(a, (0.0, 5.0, 2.0, 6.0)) == pytest.approx(3.0)      # +y only
    assert rect_gap(a, (5.0, 6.0, 6.0, 7.0)) == pytest.approx(5.0)      # diagonal
    assert rect_gap(a, (2.0, 2.0, 3.0, 3.0)) == pytest.approx(0.0)      # touching
    assert rect_gap(a, (1.0, 1.0, 3.0, 3.0)) == pytest.approx(0.0)      # overlapping


def test_annulus_is_four_boxes_that_do_not_cover_the_courtyard():
    """⛔ The one property a keepout annulus must have."""
    courtyard = (10.0, 20.0, 13.0, 25.0)
    ring = annulus_polygon(courtyard, 1.0)
    assert len(ring) == 4
    for polygon in ring:
        assert len(polygon) == 4
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        box = (min(xs), min(ys), max(xs), max(ys))
        # No ring box may intrude on the courtyard itself -- if one did, the
        # keepout would block the controller's own pads.
        assert not (box[0] < courtyard[2] and box[2] > courtyard[0]
                    and box[1] < courtyard[3] and box[3] > courtyard[1])
        # ... and every box must lie inside the outer boundary.
        assert box[0] >= courtyard[0] - 1.0 - 1e-9
        assert box[2] <= courtyard[2] + 1.0 + 1e-9


def test_annulus_covers_every_side():
    courtyard = (0.0, 0.0, 2.0, 2.0)
    ring = annulus_polygon(courtyard, 0.5)
    xs = [p[0] for polygon in ring for p in polygon]
    ys = [p[1] for polygon in ring for p in polygon]
    assert min(xs) == pytest.approx(-0.5) and max(xs) == pytest.approx(2.5)
    assert min(ys) == pytest.approx(-0.5) and max(ys) == pytest.approx(2.5)


def test_annulus_of_a_zero_lane_is_nothing():
    assert annulus_polygon((0.0, 0.0, 1.0, 1.0), 0.0) == []


# --------------------------------------------------------------------------- #
# the lane and where it came from
# --------------------------------------------------------------------------- #

def test_lane_from_oshpark_is_the_documented_constant():
    """The constant is the *default*; the spec is the source of truth."""
    assert lane_from_fab(OSHPARK_2L) == pytest.approx(ESCAPE_LANE_MM)
    assert lane_from_fab(OSHPARK_2L) == pytest.approx(0.6 + 2 * 0.1524)


def test_lane_follows_a_different_spec():
    coarse = FabSpec(name="coarse", via_size_mm=0.8, via_drill_mm=0.4,
                     min_drill_mm=0.3, min_clearance_mm=0.2, clearance_mm=0.25,
                     min_track_mm=0.2, track_width_mm=0.3)
    assert lane_from_fab(coarse) == pytest.approx(0.8 + 2 * 0.2)


def test_lane_carries_its_source():
    assert resolve_lane_mm(None, None) == (ESCAPE_LANE_MM,
                                           "default:ESCAPE_LANE_MM")
    assert resolve_lane_mm(None, OSHPARK_2L) == (pytest.approx(ESCAPE_LANE_MM),
                                                 "fab:oshpark-2l")
    assert resolve_lane_mm(1.25, OSHPARK_2L) == (1.25, "explicit")
    # ⚠ ``True`` means "derive it", never "1.0 mm".
    assert resolve_lane_mm(True, OSHPARK_2L)[1] == "fab:oshpark-2l"


# --------------------------------------------------------------------------- #
# the measurement
# --------------------------------------------------------------------------- #

def test_measure_finds_the_controller_from_the_stage_plan():
    circuit = _boost()
    plan = classify_power_roles(circuit)
    placed = [_placed("U1", 0.0, 0.0, MSOP), _placed("R1", 6.0, 0.0, R08)]
    room = measure_escape_room(_Result(placed, plan), fp_geometries=FP_GEOMETRIES)
    assert room.controller_ref == "U1"
    assert room.controller_source == "power_stage_plan"


def test_measure_falls_back_to_the_pad_count():
    """No plan in hand -> WS-8's own rule, unchanged."""
    big = FootprintGeometry(
        footprint="big", courtyard_bounds=(-3.0, -3.0, 3.0, 3.0),
        pads=[object()] * 8)
    small = FootprintGeometry(
        footprint="small", courtyard_bounds=(-1.0, -1.0, 1.0, 1.0),
        pads=[object()] * 2)
    geometries = {"big": big, "small": small}
    placed = [_placed("R1", 0.0, 0.0, "small"), _placed("U9", 20.0, 0.0, "big")]
    room = measure_escape_room(_Result(placed), fp_geometries=geometries)
    assert room.controller_ref == "U9"
    assert room.controller_source == "pad_count"


def test_measure_reports_the_gap_the_tight_set_and_the_lane():
    placed = [
        _placed("U1", 0.0, 0.0, MSOP),
        _placed("R1", 5.0, 0.0, R08),      # close
        _placed("R2", 40.0, 0.0, R08),     # far
    ]
    room = measure_escape_room(_Result(placed), fp_geometries=FP_GEOMETRIES,
                               controller_ref="U1", fab_spec=OSHPARK_2L)
    expected = 5.0 - COURTYARDS[MSOP][0] / 2 - COURTYARDS[R08][0] / 2
    assert room.nearest_gap_mm == pytest.approx(expected)
    assert room.nearest_ref == "R1"
    assert room.tight_refs == ["R1"]
    assert room.lane_mm == pytest.approx(ESCAPE_LANE_MM)
    assert room.lane_source == "fab:oshpark-2l"
    assert room.to_dict()["nearest_ref"] == "R1"


def test_measure_returns_none_without_a_placement():
    assert measure_escape_room(_Result([]), fp_geometries=FP_GEOMETRIES) is None


# --------------------------------------------------------------------------- #
# the constraint generator (the measured-negative path, still gated)
# --------------------------------------------------------------------------- #

def test_escape_constraints_are_silent_without_a_plan():
    assert escape_far_constraints(None, BOOST_FOOTPRINTS,
                                  fp_geometries=FP_GEOMETRIES) == []


def test_escape_constraints_name_every_part_once_toward_the_controller():
    plan = classify_power_roles(_boost())
    generated = escape_far_constraints(plan, BOOST_FOOTPRINTS,
                                       fp_geometries=FP_GEOMETRIES)
    assert generated
    assert all(isinstance(c, FarConstraint) for c in generated)
    assert {c.target_ref for c in generated} == {"U1"}
    refs = [c.ref for c in generated]
    assert len(refs) == len(set(refs))
    assert "U1" not in refs


def test_escape_constraint_distance_is_the_edge_gap_converted():
    """⚠⚠ The unit trap: ``distance_mm`` is centre-to-centre, not an edge gap."""
    plan = classify_power_roles(_boost())
    generated = escape_far_constraints(plan, BOOST_FOOTPRINTS,
                                       fp_geometries=FP_GEOMETRIES, lane_mm=0.9048)
    by_ref = {c.ref: c for c in generated}
    assert by_ref["R1"].distance_mm == pytest.approx(
        0.9048 + _radius(MSOP) + _radius(R08), abs=1e-6)


def test_escape_constraints_are_deterministic_and_position_free():
    plan = classify_power_roles(_boost())
    first = escape_far_constraints(plan, BOOST_FOOTPRINTS,
                                   fp_geometries=FP_GEOMETRIES)
    second = escape_far_constraints(plan, BOOST_FOOTPRINTS,
                                    fp_geometries=FP_GEOMETRIES)
    assert [(c.ref, c.target_ref, c.distance_mm) for c in first] == \
           [(c.ref, c.target_ref, c.distance_mm) for c in second]


def test_generate_power_constraints_escape_room_is_opt_in():
    from skidl_layout.power_constraints import generate_power_constraints

    plan = classify_power_roles(_boost())
    off = generate_power_constraints(plan, footprint_by_ref=BOOST_FOOTPRINTS,
                                     fp_geometries=FP_GEOMETRIES)
    on = generate_power_constraints(plan, footprint_by_ref=BOOST_FOOTPRINTS,
                                    fp_geometries=FP_GEOMETRIES, escape_room=True)
    assert len(on.far) > len(off.far)
    assert [(c.ref, c.target_ref, c.distance_mm) for c in on.far[:len(off.far)]] == \
           [(c.ref, c.target_ref, c.distance_mm) for c in off.far]
    added = on.far[len(off.far):]
    assert added and all(c.target_ref == "U1" for c in added)
    for constraint in added:
        assert on.reasons[f"far {constraint.ref}->{constraint.target_ref}"]


def test_position_aware_escape_constraints_name_only_the_tight_refs():
    """The diagnostic form: it has a placement, so it can be selective."""
    from skidl_layout.power_escape import escape_constraints

    placed = [
        _placed("U1", 0.0, 0.0, MSOP),
        _placed("R1", 5.0, 0.0, R08),      # inside the lane
        _placed("R2", 40.0, 0.0, R08),     # nowhere near
    ]
    generated = escape_constraints(_Result(placed), fp_geometries=FP_GEOMETRIES,
                                   controller_ref="U1")
    assert [(c.ref, c.target_ref) for c in generated] == [("R1", "U1")]
    assert generated[0].distance_mm == pytest.approx(
        ESCAPE_LANE_MM + _radius(MSOP) + _radius(R08), abs=1e-6)


# --------------------------------------------------------------------------- #
# the enforcement pass -- the mechanism that ships
# --------------------------------------------------------------------------- #

def test_apply_escape_room_relieves_a_tight_neighbour():
    placed = [_placed("U1", 0.0, 0.0, MSOP), _placed("R1", 5.0, 0.0, R08)]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        controller_ref="U1")
    assert info["tight_before"] == ["R1"]
    assert info["moved"]["R1"]["axis"] == "+x"
    assert info["tight_after"] == []
    assert info["gap_after_mm"] >= ESCAPE_LANE_MM
    room = measure_escape_room(_Result(out), fp_geometries=FP_GEOMETRIES,
                               controller_ref="U1")
    assert room.nearest_gap_mm >= ESCAPE_LANE_MM


def test_apply_escape_room_is_a_no_op_when_there_is_already_room():
    placed = [_placed("U1", 0.0, 0.0, MSOP), _placed("R1", 40.0, 0.0, R08)]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        controller_ref="U1")
    # Same objects, so a caller can test ``info["moved"]`` instead of diffing.
    assert all(a is b for a, b in zip(out, placed))
    assert info["moved"] == {}
    assert info["gap_after_mm"] == info["gap_before_mm"]


def test_apply_escape_room_reverts_when_one_neighbour_stays_blocked():
    """⭐⭐ All-or-nothing: a breached annulus is not an escape route.

    ``R1`` can move; ``R2`` is pinned against the outline edge and cannot. The
    controller therefore still has no via site, so ``R1``'s move bought nothing
    and is given back rather than paid for.
    """
    # A letterbox outline: nothing can move vertically. ``R1`` has open board to
    # its left and clears; ``R2`` would need to leave the right edge, and its
    # only other direction is now occupied by the moved ``R1``.
    outline = BoardOutline(vertices=[(-6.0, 2.5), (13.0, 2.5),
                                     (13.0, 7.2), (-6.0, 7.2)])
    placed = [
        _placed("U1", 6.5, 5.0, MSOP),
        _placed("R1", 2.0, 5.0, R08),
        _placed("R2", 10.5, 5.0, R08),
    ]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        outline=outline, controller_ref="U1")
    assert info["blocked"] == ["R2"]
    assert info["reverted"] is True
    assert info["moved"] == {}
    assert "R1" in info["reverted_moves"]
    assert all(a is b for a, b in zip(out, placed))


def test_apply_escape_room_partial_keeps_what_it_could_move():
    """The measured negative, kept reachable so it can be re-derived."""
    outline = BoardOutline(vertices=[(-6.0, 2.5), (13.0, 2.5),
                                     (13.0, 7.2), (-6.0, 7.2)])
    placed = [
        _placed("U1", 6.5, 5.0, MSOP),
        _placed("R1", 2.0, 5.0, R08),
        _placed("R2", 10.5, 5.0, R08),
    ]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        outline=outline, controller_ref="U1", partial=True)
    assert info["blocked"] == ["R2"]
    assert info["reverted"] is False
    assert "R1" in info["moved"]
    # ⚠ And it bought nothing: the annulus is still breached by R2.
    assert info["tight_after"] == ["R2"]
    assert out is not placed


def test_apply_escape_room_does_not_revert_when_everything_clears():
    placed = [_placed("U1", 0.0, 0.0, MSOP), _placed("R1", 5.0, 0.0, R08)]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        controller_ref="U1")
    assert info["blocked"] == []
    assert info["reverted"] is False
    assert info["moved"]


def test_two_declared_ics_each_get_their_own_room():
    """⭐ More than one chip per board, which is the point of the declaration."""
    circuit = _Circuit(_Part("U1", MSOP), _Part("U2", MSOP),
                       _Part("R1", R08), _Part("R2", R08))
    mark_escape_room(circuit.parts[0], circuit.parts[1])
    placed = [
        _placed("U1", 0.0, 0.0, MSOP),
        _placed("R1", 5.0, 0.0, R08),        # crowds U1
        _placed("U2", 0.0, 30.0, MSOP),
        _placed("R2", 5.0, 30.0, R08),       # crowds U2
    ]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        circuit=circuit)
    assert info["controllers"] == ["U1", "U2"]
    assert info["controller_source"] == "declared"
    assert set(info["moved"]) == {"R1", "R2"}
    assert info["per_controller"]["U1"]["moved"].keys() == {"R1"}
    assert info["per_controller"]["U2"]["moved"].keys() == {"R2"}
    for room in measure_escape_rooms(_Result(out), fp_geometries=FP_GEOMETRIES,
                                     circuit=circuit):
        assert room.nearest_gap_mm >= ESCAPE_LANE_MM


def test_each_declared_ic_may_ask_for_its_own_lane():
    circuit = _Circuit(_Part("U1", MSOP), _Part("U2", MSOP))
    mark_escape_room(circuit.parts[0])
    mark_escape_room(circuit.parts[1], lane_mm=3.0)
    placed = [_placed("U1", 0.0, 0.0, MSOP), _placed("U2", 0.0, 30.0, MSOP)]
    rooms = {r.controller_ref: r for r in
             measure_escape_rooms(_Result(placed), fp_geometries=FP_GEOMETRIES,
                                  circuit=circuit, fab_spec=OSHPARK_2L)}
    assert rooms["U1"].lane_mm == pytest.approx(ESCAPE_LANE_MM)
    assert rooms["U1"].lane_source == "fab:oshpark-2l"
    assert rooms["U2"].lane_mm == pytest.approx(3.0)
    assert rooms["U2"].lane_source == "declared"


def test_one_crowded_ic_does_not_veto_room_for_another():
    """⛔ All-or-nothing is scoped PER CONTROLLER, never per board."""
    # A letterbox outline, so nothing moves vertically. ``U1``'s annulus stays
    # breached (``R2`` has the right edge behind it and the moved ``R1`` beside
    # it); ``U2`` sits in open board far to the left and clears cleanly.
    outline = BoardOutline(vertices=[(-40.0, 2.5), (13.0, 2.5),
                                     (13.0, 7.2), (-40.0, 7.2)])
    circuit = _Circuit(_Part("U1", MSOP), _Part("U2", MSOP),
                       _Part("R1", R08), _Part("R2", R08), _Part("R3", R08))
    mark_escape_room(circuit.parts[0], circuit.parts[1])
    placed = [
        _placed("U1", 6.5, 5.0, MSOP),
        _placed("R1", 2.0, 5.0, R08),
        _placed("R2", 10.5, 5.0, R08),       # nowhere legal to go
        _placed("U2", -30.0, 5.0, MSOP),
        _placed("R3", -26.0, 5.0, R08),      # open board to its right
    ]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        outline=outline, circuit=circuit)
    assert info["per_controller"]["U1"]["reverted"] is True
    assert info["per_controller"]["U1"]["blocked"] == ["R2"]
    assert "R1" not in info["moved"]          # U1's move was given back
    # ⭐ U2's room survives U1's revert -- the verdict is per controller.
    assert info["per_controller"]["U2"]["reverted"] is False
    assert "R3" in info["moved"]
    assert out is not placed


def test_multi_ic_rollup_reports_the_worst_escape_on_the_board():
    circuit = _Circuit(_Part("U1", MSOP), _Part("U2", MSOP), _Part("R1", R08))
    mark_escape_room(circuit.parts[0], circuit.parts[1])
    placed = [
        _placed("U1", 0.0, 0.0, MSOP),
        _placed("U2", 0.0, 40.0, MSOP),
        _placed("R1", 5.0, 40.0, R08),      # only U2 is crowded
    ]
    _out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        circuit=circuit)
    worst = min(e["gap_before_mm"] for e in info["per_controller"].values())
    assert info["gap_before_mm"] == pytest.approx(worst)


def test_escape_far_constraints_follow_the_declaration():
    """Even with no switching stage at all -- an MCU has no converter."""
    circuit = _Circuit(_Part("U9", MSOP), _Part("R1", R08))
    mark_escape_room(circuit.parts[0])
    generated = escape_far_constraints(
        None, {"U9": MSOP, "R1": R08}, fp_geometries=FP_GEOMETRIES,
        circuit=circuit)
    assert [(c.ref, c.target_ref) for c in generated] == [("R1", "U9")]


def test_apply_escape_room_refuses_to_push_a_part_off_the_outline():
    """⛔ A blocked part is REPORTED, never shoved."""
    outline = BoardOutline(vertices=[(0.0, 0.0), (12.0, 0.0),
                                     (12.0, 8.0), (0.0, 8.0)])
    placed = [_placed("U1", 5.0, 4.0, MSOP), _placed("R1", 9.0, 4.0, R08)]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        outline=outline, controller_ref="U1")
    assert info["tight_before"] == ["R1"]
    assert info["blocked"] == ["R1"]
    assert info["moved"] == {}
    assert all(a is b for a, b in zip(out, placed))


def test_apply_escape_room_never_creates_an_overlap():
    """The push may not buy room by parking one part on top of another."""
    from skidl_layout.validator import _placed_bounds, _rects_overlap

    placed = [
        _placed("U1", 0.0, 0.0, MSOP),
        _placed("R1", 5.0, 0.0, R08),
        _placed("R2", 8.0, 0.0, R08),
        _placed("R3", 0.0, 4.0, R08),
    ]
    out, info = apply_escape_room(
        placed, lane_mm=ESCAPE_LANE_MM, fp_geometries=FP_GEOMETRIES,
        clearance_mm=0.5, controller_ref="U1")
    bounds = [(p.ref, _placed_bounds(p, {}, FP_GEOMETRIES, physical=True))
              for p in out]
    for i, (ref_a, box_a) in enumerate(bounds):
        for ref_b, box_b in bounds[i + 1:]:
            assert not _rects_overlap(box_a, box_b, 0.5), f"{ref_a}/{ref_b}"
    # Something had to give, and the pass says which.
    assert set(info["moved"]) | set(info["blocked"]) == set(info["tight_before"])


def test_apply_escape_room_zero_lane_does_nothing():
    placed = [_placed("U1", 0.0, 0.0, MSOP), _placed("R1", 3.5, 0.0, R08)]
    out, info = apply_escape_room(placed, lane_mm=0.0,
                                  fp_geometries=FP_GEOMETRIES,
                                  controller_ref="U1")
    assert all(a is b for a, b in zip(out, placed))
    assert info["moved"] == {}


# --------------------------------------------------------------------------- #
# the keepout writer -- round-tripped through KRT's own parser
# --------------------------------------------------------------------------- #

_MINIMAL_PCB = '''(kicad_pcb
\t(version 20241229)
\t(generator "skidl")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)
\t(net 0 "")
)
'''


def _krt_parser():
    root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "KiCadRoutingTools"))
    if not os.path.isdir(root):
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import kicad_parser
    except Exception:                          # noqa: BLE001
        return None
    return kicad_parser


def test_write_keepout_polygons_declares_the_user_layer(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_MINIMAL_PCB, encoding="utf-8")
    written = write_keepout_polygons(
        str(board), annulus_polygon((10.0, 10.0, 13.0, 15.0), 0.9048))
    assert written == 4
    text = board.read_text(encoding="utf-8")
    assert '(41 "User.2" user)' in text
    assert text.rstrip().endswith(")")
    assert text.count("(gr_poly") == 4


def test_write_keepout_polygons_skips_degenerate_and_empty(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_MINIMAL_PCB, encoding="utf-8")
    assert write_keepout_polygons(str(board), []) == 0
    assert write_keepout_polygons(str(board), [[(0.0, 0.0), (1.0, 1.0)]]) == 0
    assert board.read_text(encoding="utf-8") == _MINIMAL_PCB


def test_write_keepout_polygons_refuses_a_layer_it_cannot_number(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_MINIMAL_PCB, encoding="utf-8")
    with pytest.raises(ValueError):
        write_keepout_polygons(str(board), [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]],
                               layer="Nonsense.7")


def test_written_keepouts_round_trip_through_krt(tmp_path):
    """⛔ The gate that matters: KRT's OWN parser reads back what we wrote."""
    kicad_parser = _krt_parser()
    if kicad_parser is None:
        pytest.skip("no importable KiCadRoutingTools checkout")

    board = tmp_path / "b.kicad_pcb"
    board.write_text(_MINIMAL_PCB, encoding="utf-8")
    courtyard = (10.0, 10.0, 13.0, 15.0)
    ring = annulus_polygon(courtyard, 0.9048)
    assert write_keepout_polygons(str(board), ring) == len(ring)

    zones = kicad_parser.parse_keepout_zones(
        board.read_text(encoding="utf-8"), "User.2")
    assert len(zones) == len(ring)
    got = sorted(tuple(sorted(round(v, 6) for v in
                              (p[0] for p in zone.points))) for zone in zones)
    want = sorted(tuple(sorted(round(v, 6) for v in
                               (p[0] for p in polygon))) for polygon in ring)
    assert got == want
    assert all(zone.is_closed for zone in zones)
    # And nothing lands on a layer nobody asked for.
    assert kicad_parser.parse_keepout_zones(
        board.read_text(encoding="utf-8"), "User.3") == []


# --------------------------------------------------------------------------- #
# Spacing plan C -- the fanout's JSON_SUMMARY, parsed at the source
# --------------------------------------------------------------------------- #
def test_fanout_output_parse_reads_min_clearance_used():
    """⭐ The one number that makes plan C's judge free.

    ⚠ Recorded, and load-bearing: nothing inside a ``qfn_fanout`` process calls
    ``clearance_ledger.record`` (the recorders are ``plane_pad_tap`` and
    ``kicad_oracle``, in other processes), so the ledger is empty and
    ``min_clearance_used`` equals the ``--clearance`` we passed. Measured at
    0.25 mm on all five power boards.
    """
    from skidl_layout.power_escape import _parse_fanout_output

    text = (
        "Parsing board.kicad_pcb...\n"
        "Underpad via-drop: 5 vias placed, 0 dropped (pitch 0.50, ...)\n"
        "Generated 10 track segments (5 stubs x 2 segments)\n"
        'JSON_SUMMARY: {"clearance": 0.25, "min_clearance_used": 0.25, '
        '"drc_grazes": 0, "total": 0}\n'
    )

    out = _parse_fanout_output(text)

    assert out["min_clearance_used"] == 0.25
    assert out["summary"]["clearance"] == 0.25
    assert out["vias_placed"] == 5 and out["vias_dropped"] == 0


def test_fanout_output_parse_survives_a_missing_or_broken_summary():
    """⛔ A pre-pass that declines is an outcome the caller routes past, so a
    log with no summary -- or a truncated one -- must parse to ``None``, never
    raise."""
    from skidl_layout.power_escape import _parse_fanout_output

    assert _parse_fanout_output("")["min_clearance_used"] is None
    assert _parse_fanout_output(
        "Underpad via-drop: 1 vias placed, 6 dropped"
    )["min_clearance_used"] is None
    assert _parse_fanout_output(
        'JSON_SUMMARY: {"min_clearance_used": 0.2'      # truncated JSON
    )["min_clearance_used"] is None
    assert _parse_fanout_output(
        'JSON_SUMMARY: {"min_clearance_used": null}'
    )["min_clearance_used"] is None


def test_fanout_output_parse_takes_the_last_summary_line():
    """A chained run prints one summary per controller; the parser keeps the
    last, which is the board the next step reads."""
    from skidl_layout.power_escape import _parse_fanout_output

    out = _parse_fanout_output(
        'JSON_SUMMARY: {"min_clearance_used": 0.25}\n'
        'JSON_SUMMARY: {"min_clearance_used": 0.6}\n')

    assert out["min_clearance_used"] == 0.6
