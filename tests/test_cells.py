# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.cells` -- the layout-cell artifact.

The geometry, rotation, serialisation and transit-sweep halves need no
KiCadRoutingTools and no board on disk; the harvesting half is an integration
test that skips when either is unavailable.
"""

from __future__ import annotations

import os

import pytest

from skidl_layout.cells import (
    CellCopper,
    CellMember,
    CellPad,
    CellPort,
    CellSegment,
    CellTransit,
    CellVia,
    LayoutCell,
    TransitLane,
    _complement,
    _rotate_side,
    cell_digest,
    cell_geometry,
    cell_pad_numbers,
    deserialise_cell,
    harvest_cell,
    hpwl_winner_labels,
    member_placed_parts,
    net_escape_points,
    resolve_footprint_name,
    resolve_hpwl_points,
    rotate_cell,
    serialise_cell,
    sweep_transit,
)
from skidl_layout.writer import PlacedPart

ROTATIONS = (0, 90, 180, 270)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _divider_cell() -> LayoutCell:
    """Two 0805s side by side: VOUT - R1 - VFB - R2 - GND, 6.85 x 1.40 mm.

    Modelled on the arrangement ``harvest_cell`` actually lifts off
    ``lt3844_buck_manual.kicad_pcb``, so the unit tests and the integration test
    are talking about the same object.
    """
    pads = (
        CellPad("R1", "1", "VOUT", 6.3375, 0.7, 1.025, 1.4),
        CellPad("R1", "2", "VFB", 4.5125, 0.7, 1.025, 1.4),
        CellPad("R2", "1", "VFB", 2.3375, 0.7, 1.025, 1.4),
        CellPad("R2", "2", "GND", 0.5125, 0.7, 1.025, 1.4),
    )
    return LayoutCell(
        name="fb_divider",
        width=6.85,
        height=1.40,
        members=(
            CellMember("R1", "part", "Resistor_SMD:R_0805_2012Metric", 5.425, 0.7, 180),
            CellMember("R2", "part", "Resistor_SMD:R_0805_2012Metric", 1.425, 0.7, 180),
        ),
        pads=pads,
        nets={"VOUT": ("R1.1",), "VFB": ("R1.2", "R2.1"), "GND": ("R2.2",)},
        internal_nets=frozenset({"VFB"}),
        ports=(
            CellPort("VOUT", "E", 0, "FAVORED", 6.85, 0.7, 0.5),
            CellPort("GND", "W", 0, "FAVORED", 0.0, 0.7, 0.5),
        ),
        layers_defined=(0,),
        fab="oshpark-2l",
        stackup=2,
    ).normalised()


# --------------------------------------------------------------------------- #
# quantisation and normalisation -- the determinism floor
# --------------------------------------------------------------------------- #
def test_normalise_quantises_to_a_nanometre():
    cell = LayoutCell(name="c", width=1.00000004, height=2.0).normalised()
    assert cell.width == 1.0


def test_normalise_kills_negative_zero():
    cell = LayoutCell(name="c", width=-1e-9, height=2.0).normalised()
    assert serialise_cell(cell).count("-0.0") == 0


def test_rotation_must_be_a_right_angle():
    with pytest.raises(ValueError):
        rotate_cell(_divider_cell(), 45)


# --------------------------------------------------------------------------- #
# rotation -- derived from the transform, not from a hardcoded cycle
# --------------------------------------------------------------------------- #
def test_side_cycle_is_kicad_y_down_not_the_plan_prose():
    """⚠ The plan's prose says N->E->S->W; that is a ``y``-up frame.

    In KiCad's ``y``-down frame the same rotation gives N->W->S->E. This test
    pins the *derived* answer so a future edit that hardcodes the prose cycle
    fails loudly instead of shipping mirrored cells.
    """
    assert [_rotate_side(s, 90) for s in ("N", "W", "S", "E")] == \
        ["W", "S", "E", "N"]


def test_side_cycle_is_a_group_of_order_four():
    for side in ("N", "E", "S", "W"):
        turned = side
        for _ in range(4):
            turned = _rotate_side(turned, 90)
        assert turned == side


def test_180_is_90_twice():
    cell = _divider_cell()
    assert serialise_cell(rotate_cell(cell, 180)) == \
        serialise_cell(rotate_cell(rotate_cell(cell, 90), 90))


def test_four_90s_return_the_cell_byte_identically():
    cell = _divider_cell()
    turned = cell
    for _ in range(4):
        turned = rotate_cell(turned, 90)
    assert serialise_cell(turned) == serialise_cell(cell)
    assert turned.digest == cell.digest


@pytest.mark.parametrize("deg", ROTATIONS)
def test_rotation_by_deg_and_its_inverse_round_trip(deg):
    cell = _divider_cell()
    back = rotate_cell(rotate_cell(cell, deg), (360 - deg) % 360)
    assert serialise_cell(back) == serialise_cell(cell)


def test_90_swaps_the_box():
    cell = _divider_cell()
    turned = rotate_cell(cell, 90)
    assert (turned.width, turned.height) == (cell.height, cell.width)


def test_180_keeps_the_box():
    cell = _divider_cell()
    turned = rotate_cell(cell, 180)
    assert (turned.width, turned.height) == (cell.width, cell.height)


@pytest.mark.parametrize("deg", ROTATIONS)
def test_members_stay_inside_the_box_under_rotation(deg):
    turned = rotate_cell(_divider_cell(), deg)
    for member in turned.members:
        assert -1e-9 <= member.dx <= turned.width + 1e-9
        assert -1e-9 <= member.dy <= turned.height + 1e-9


@pytest.mark.parametrize("deg", ROTATIONS)
def test_pads_stay_inside_the_box_under_rotation(deg):
    turned = rotate_cell(_divider_cell(), deg)
    for pad in turned.pads:
        x0, y0, x1, y1 = pad.bounds
        assert x0 >= -1e-6 and y0 >= -1e-6
        assert x1 <= turned.width + 1e-6 and y1 <= turned.height + 1e-6


@pytest.mark.parametrize("deg", ROTATIONS)
def test_member_rotations_accumulate(deg):
    turned = rotate_cell(_divider_cell(), deg)
    for member, source in zip(turned.members, _divider_cell().members):
        assert member.rotation == (source.rotation + deg) % 360


def test_rotation_field_accumulates_modulo_360():
    cell = _divider_cell()
    assert rotate_cell(rotate_cell(cell, 270), 180).rotation == 90


def test_ports_move_with_the_box():
    cell = _divider_cell()
    turned = rotate_cell(cell, 90)
    port = turned.port("VOUT", _rotate_side("E", 90), 0)
    assert port is not None
    assert port.access == "FAVORED"


def test_copper_rotates_and_round_trips():
    cell = _divider_cell()
    with_copper = LayoutCell(
        **{**cell.__dict__,
           "copper": CellCopper(
               segments=(CellSegment(1.0, 0.5, 3.0, 0.5, 0, 0.25, "VFB"),),
               vias=(CellVia(2.0, 0.7, 0.6, 0.3, "VFB"),))},
    ).normalised()
    turned = with_copper
    for _ in range(4):
        turned = rotate_cell(turned, 90)
    assert serialise_cell(turned) == serialise_cell(with_copper)


def test_hpwl_points_rotate_with_the_cell():
    cell = _divider_cell()
    with_points = LayoutCell(
        **{**cell.__dict__, "hpwl_points": {"VOUT": (6.3375, 0.7)}}).normalised()
    turned = rotate_cell(with_points, 90)
    assert turned.hpwl_points["VOUT"] != with_points.hpwl_points["VOUT"]
    for _ in range(3):
        turned = rotate_cell(turned, 90)
    assert serialise_cell(turned) == serialise_cell(with_points)


# --------------------------------------------------------------------------- #
# serialisation and the digest
# --------------------------------------------------------------------------- #
def test_serialise_load_round_trip_is_byte_identical():
    cell = _divider_cell()
    assert serialise_cell(deserialise_cell(serialise_cell(cell))) == \
        serialise_cell(cell)


def test_digest_ignores_meta():
    cell = _divider_cell()
    tagged = LayoutCell(**{**cell.__dict__, "meta": {"note": "anything"}})
    assert cell_digest(tagged) == cell_digest(cell)


def test_digest_moves_when_geometry_moves():
    cell = _divider_cell()
    nudged = LayoutCell(**{**cell.__dict__, "width": cell.width + 0.1})
    assert cell_digest(nudged) != cell_digest(cell)


def test_digest_is_member_order_independent():
    cell = _divider_cell()
    reversed_members = LayoutCell(
        **{**cell.__dict__, "members": tuple(reversed(cell.members))})
    assert cell_digest(reversed_members) == cell_digest(cell)


def test_digest_is_16_hex_chars():
    digest = _divider_cell().digest
    assert len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)


# --------------------------------------------------------------------------- #
# the two maps' opposite defaults -- the trap section 3.2 names
# --------------------------------------------------------------------------- #
def test_an_unspecified_port_is_unusable():
    cell = _divider_cell()
    assert cell.port("VOUT", "N", 0) is None
    assert "N" not in cell.escapable_sides("VOUT")


def test_a_blocked_port_is_not_an_escapable_side():
    cell = _divider_cell()
    blocked = LayoutCell(**{
        **cell.__dict__,
        "ports": cell.ports + (CellPort("VOUT", "N", 0, "BLOCKED"),)}).normalised()
    assert "N" not in blocked.escapable_sides("VOUT")


def test_an_unspecified_layer_is_fully_passable():
    cell = _divider_cell()
    assert cell.transit_for(3, "EW") is None
    assert cell.passable_width(3, "EW") == (cell.height, cell.height)
    assert cell.passable_width(3, "NS") == (cell.width, cell.width)


def test_a_specified_layer_uses_its_lanes():
    cell = _divider_cell()
    with_transit = LayoutCell(**{
        **cell.__dict__,
        "transit": (CellTransit(0, "EW",
                                lanes=(TransitLane(0, "EW", 0.0, 0.1),)),)}).normalised()
    assert with_transit.passable_width(0, "EW") == (0.1, 0.1)


# --------------------------------------------------------------------------- #
# the transit sweep
# --------------------------------------------------------------------------- #
def test_complement_of_nothing_is_the_whole_box():
    assert _complement(0.0, 10.0, [], 0.1) == [(0.0, 10.0)]


def test_complement_drops_intervals_below_the_track_floor():
    lanes = _complement(0.0, 10.0, [(0.0, 4.95), (5.0, 10.0)], 0.1)
    assert lanes == []


def test_complement_merges_overlapping_obstructions():
    assert _complement(0.0, 10.0, [(1.0, 5.0), (3.0, 7.0)], 0.1) == \
        [(0.0, 1.0), (7.0, 10.0)]


def test_two_terminal_passives_are_open_NORTH_SOUTH_not_east_west():
    """⛔ **MEASURED, and it inverts the plan's section 3.2 prediction.**

    The plan predicts a two-terminal passive is *"transparent east-west under
    its body and opaque north-south through its pad rows"*. On the real KiCad
    footprint convention it is the other way round: a chip passive at rotation 0
    puts its two pads at the **ends of the x axis**, each spanning the footprint's
    **full y extent**. So an east-west traverse must cross both pads (blocked),
    while a north-south traverse slips through the gap **between** them (open).

    ⭐ This is exactly the class of error ``power_escape.part_rect``'s docstring
    records, and it is why the sweep is derived from pad rectangles rather than
    from a per-part-kind rule of thumb.
    """
    cell = _divider_cell()
    transit = sweep_transit(cell, layers=(0,), clearance_mm=0.1,
                            min_track_mm=0.15)
    by_axis = {t.axis: t for t in transit}
    assert by_axis["EW"].total_width == 0.0
    assert by_axis["NS"].total_width > 0.0
    # Three inter-pad gaps across a two-resistor divider.
    assert len(by_axis["NS"].lanes) == 3


def test_a_blank_layer_gets_no_entry_and_therefore_full_passage():
    cell = _divider_cell()
    transit = sweep_transit(cell, layers=(0, 1), clearance_mm=0.1,
                            min_track_mm=0.15)
    inner = [t for t in transit if t.layer == 1]
    # ⭐ Layer 1 carries no surface pads, so its lanes are the whole box.
    assert all(t.total_width == pytest.approx(
        cell.height if t.axis == "EW" else cell.width) for t in inner)


def test_a_through_board_pad_obstructs_every_layer():
    """⛔⛔ The blank-layer exception -- the one way this map breaks a board.

    Surface pads leave an inner layer fully passable. Make the same pads
    through-board and the inner layer must read **exactly like the top one**.
    """
    cell = _divider_cell()
    surface = {(t.layer, t.axis): t.total_width
               for t in sweep_transit(cell, layers=(0, 1), clearance_mm=0.1,
                                      min_track_mm=0.15)}
    assert surface[(1, "EW")] == pytest.approx(cell.height)   # blank -> open

    thru = LayoutCell(**{
        **cell.__dict__,
        "pads": tuple(CellPad(p.local_ref, p.pad, p.local_net, p.x, p.y,
                              p.w, p.h, through_board=True)
                      for p in cell.pads)}).normalised()
    drilled = {(t.layer, t.axis): t.total_width
               for t in sweep_transit(thru, layers=(0, 1), clearance_mm=0.1,
                                      min_track_mm=0.15)}
    assert drilled[(1, "EW")] == drilled[(0, "EW")] == 0.0
    assert drilled[(1, "NS")] == drilled[(0, "NS")]


def test_a_via_obstructs_every_layer_including_undefined_ones():
    cell = _divider_cell()
    with_via = LayoutCell(**{
        **cell.__dict__,
        "copper": CellCopper(vias=(CellVia(3.425, 0.7, 6.0, 0.3, "VFB"),))},
    ).normalised()
    transit = sweep_transit(with_via, layers=(0, 1, 2, 3), clearance_mm=0.1,
                            min_track_mm=0.15)
    # The via's 6 mm body spans the box's whole height, so east-west is shut on
    # layers 2 and 3 -- which the cell defines nothing on.
    assert {t.layer for t in transit if t.axis == "EW"} == {0, 1, 2, 3}
    assert all(t.total_width == 0.0 for t in transit if t.axis == "EW")


def test_total_width_and_max_trace_are_not_interchangeable():
    """⭐ The whole reason both scalars exist."""
    cell = _divider_cell()
    transit = sweep_transit(cell, layers=(0,), clearance_mm=0.05,
                            min_track_mm=0.1)
    ew = next(t for t in transit if t.axis == "EW")
    # Not asserted equal: on a real cell they differ whenever there is more
    # than one lane, and the sweep must keep them apart.
    assert ew.max_trace <= ew.total_width


def test_transit_records_the_clearance_it_was_computed_at():
    transit = sweep_transit(_divider_cell(), layers=(0,), clearance_mm=0.1524,
                            min_track_mm=0.15)
    assert all(entry.clearance == 0.1524 for entry in transit)
    assert all(entry.source == "geometric" for entry in transit)


def test_transit_lanes_survive_a_rotation_round_trip():
    cell = _divider_cell()
    swept = LayoutCell(**{
        **cell.__dict__,
        "transit": sweep_transit(cell, layers=(0,), clearance_mm=0.05,
                                 min_track_mm=0.1)}).normalised()
    turned = swept
    for _ in range(4):
        turned = rotate_cell(turned, 90)
    assert serialise_cell(turned) == serialise_cell(swept)


def test_rotating_swaps_the_transit_axes():
    cell = _divider_cell()
    swept = LayoutCell(**{
        **cell.__dict__,
        "transit": sweep_transit(cell, layers=(0,), clearance_mm=0.05,
                                 min_track_mm=0.1)}).normalised()
    turned = rotate_cell(swept, 90)
    assert turned.transit_for(0, "NS").total_width == \
        swept.transit_for(0, "EW").total_width


# --------------------------------------------------------------------------- #
# section 3.6 -- the HPWL representative point
# --------------------------------------------------------------------------- #
def test_hpwl_point_picks_the_pad_nearest_an_escapable_edge():
    cell = _divider_cell()
    points = resolve_hpwl_points(cell)
    # VOUT escapes east; its only pad is the eastmost one.
    assert points["VOUT"] == (6.3375, 0.7)
    # GND escapes west; its only pad is the westmost one.
    assert points["GND"] == (0.5125, 0.7)


def test_hpwl_point_is_not_produced_for_an_internal_net():
    assert "VFB" not in resolve_hpwl_points(_divider_cell())


def test_hpwl_point_ignores_edges_the_net_cannot_escape_from():
    """⭐ Or a net is represented by a pad against a wall it can never cross."""
    cell = _divider_cell()
    # Give VOUT a WEST port instead of an EAST one; the winner must move to the
    # west pad even though VOUT's only pad is in the east.
    moved = LayoutCell(**{
        **cell.__dict__,
        "nets": {**dict(cell.nets), "VOUT": ("R1.1", "R2.2")},
        "pads": cell.pads[:1] + (
            CellPad("R2", "2", "VOUT", 0.5125, 0.7, 1.025, 1.4),),
        "internal_nets": frozenset({"VFB"}),
        "ports": (CellPort("VOUT", "W", 0, "FAVORED", 0.0, 0.7),)}).normalised()
    assert resolve_hpwl_points(moved)["VOUT"] == (0.5125, 0.7)


@pytest.mark.parametrize("deg", ROTATIONS)
def test_the_winning_candidate_is_rotation_invariant(deg):
    """⭐⭐ The property the whole precomputation rests on (gate ``T2h``)."""
    cell = _divider_cell()
    assert hpwl_winner_labels(rotate_cell(cell, deg)) == hpwl_winner_labels(cell)


def test_the_tie_break_is_total():
    """⛔ A symmetric two-pad passive ties on distance AND on tangency."""
    cell = LayoutCell(
        name="sym", width=4.0, height=2.0,
        members=(CellMember("C1", "part", "Capacitor_SMD:C_0603_1608Metric",
                            2.0, 1.0, 0),),
        pads=(CellPad("C1", "1", "N1", 1.0, 1.0, 0.5, 0.5),
              CellPad("C1", "2", "N1", 3.0, 1.0, 0.5, 0.5)),
        nets={"N1": ("C1.1", "C1.2")},
        internal_nets=frozenset(),
        ports=(CellPort("N1", "N", 0, "ACCESSIBLE", 2.0, 0.0),),
    ).normalised()
    # Both pads are 1.0 from the north edge and 1.0 from the centre; only the
    # (local_ref, pad) key can decide, and it must decide the same way twice.
    assert hpwl_winner_labels(cell) == {"N1": "C1.1"}
    assert resolve_hpwl_points(cell) == resolve_hpwl_points(cell)


# --------------------------------------------------------------------------- #
# the footprint masquerade
# --------------------------------------------------------------------------- #
def test_pad_numbers_are_one_per_escaping_net_in_sorted_order():
    cell = _divider_cell()
    assert cell_pad_numbers(cell) == {"GND": "1", "VOUT": "2"}


def test_synthetic_geometry_box_is_the_cell_box():
    cell = _divider_cell()
    geometry = cell_geometry(cell, "@cell:fb_divider#1")
    assert geometry.width_mm == pytest.approx(cell.width)
    assert geometry.height_mm == pytest.approx(cell.height)


def test_synthetic_pads_do_not_inflate_the_physical_bounds():
    """⚠ Zero-size pads: they are net positions, not copper. The box is the box."""
    cell = _divider_cell()
    geometry = cell_geometry(cell, "@cell:fb_divider#1")
    x0, y0, x1, y1 = geometry.physical_bounds
    assert (x1 - x0) == pytest.approx(cell.width)
    assert (y1 - y0) == pytest.approx(cell.height)


def test_synthetic_pads_carry_the_escaping_nets_only():
    geometry = cell_geometry(_divider_cell(), "@cell:x#1")
    assert sorted(p.net_name for p in geometry.pads) == ["GND", "VOUT"]


def test_pad_world_centers_track_the_cells_rotation():
    """⭐⭐ This is what buys MST crossings over real port positions."""
    cell = _divider_cell()
    key = "@cell:fb_divider#1"
    geometry = cell_geometry(cell, key)
    at0 = geometry.pad_world_centers(PlacedPart("X1", 50.0, 50.0, 0.0, key))
    at180 = geometry.pad_world_centers(PlacedPart("X1", 50.0, 50.0, 180.0, key))
    assert at0 != at180


def test_member_expansion_is_centre_referenced_and_ref_sorted():
    cell = _divider_cell()
    parts = member_placed_parts(cell, 50.0, 50.0, 0)
    assert [p.ref for p in parts] == ["R1", "R2"]
    xs = [p.x_mm for p in parts]
    assert min(xs) > 50.0 - cell.width and max(xs) < 50.0 + cell.width


def test_member_expansion_moves_with_the_cell_rotation():
    cell = _divider_cell()
    a = member_placed_parts(cell, 50.0, 50.0, 0)
    b = member_placed_parts(cell, 50.0, 50.0, 90)
    assert [(p.x_mm, p.y_mm) for p in a] != [(p.x_mm, p.y_mm) for p in b]
    assert {p.rot_deg for p in b} == {270.0}


def test_member_expansion_honours_a_ref_map():
    parts = member_placed_parts(_divider_cell(), 50.0, 50.0, 0,
                                ref_map={"R1": "RFB1", "R2": "RFB2"})
    assert [p.ref for p in parts] == ["RFB1", "RFB2"]


def test_escape_points_fall_back_to_the_pad_centroid_when_uncompiled():
    """An uncompiled (harvested) cell has no ports, so it has no escape rule."""
    cell = _divider_cell()
    raw = LayoutCell(**{**cell.__dict__, "ports": (), "hpwl_points": {}})
    points = net_escape_points(raw.normalised())
    assert set(points) == set(raw.escaping_nets)
    assert points["VOUT"] == (6.3375, 0.7)


# --------------------------------------------------------------------------- #
# footprint-name resolution -- the measured board-file gap
# --------------------------------------------------------------------------- #
def test_a_prefixed_name_is_returned_unchanged():
    assert resolve_footprint_name("Lib:Part", []) == "Lib:Part"


def test_an_unresolvable_bare_name_comes_back_bare():
    assert resolve_footprint_name("Nope_1234", []) == "Nope_1234"


def test_a_bare_name_resolves_against_a_pretty_dir(tmp_path):
    pretty = tmp_path / "Resistor_SMD.pretty"
    pretty.mkdir()
    (pretty / "R_0805_2012Metric.kicad_mod").write_text("(footprint)")
    assert resolve_footprint_name("R_0805_2012Metric", [str(tmp_path)]) == \
        "Resistor_SMD:R_0805_2012Metric"


# --------------------------------------------------------------------------- #
# harvesting -- integration, skipped without KRT and the hand boards
# --------------------------------------------------------------------------- #
_HAND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skidl-eda", "canaries", "lt3844_buck", "lt3844_buck_manual.kicad_pcb")


def _fp_dirs():
    from skidl_layout.engine import FP_LIB_DIRS_AUTO, resolve_fp_lib_dirs

    return resolve_fp_lib_dirs(FP_LIB_DIRS_AUTO)


@pytest.mark.skipif(not os.path.isfile(_HAND), reason="hand board not present")
def test_harvest_lifts_the_arrangement_off_a_hand_board():
    cell = harvest_cell(_HAND, ["RFB1", "RFB2"], "fb_divider",
                        fp_lib_dirs=_fp_dirs())
    assert cell.member_refs == ("RFB1", "RFB2")
    assert cell.meta["unresolved_footprints"] == []
    assert cell.width > 5.0 and cell.height > 1.0
    # ⛔ VFB has a third pin on U1, so it ESCAPES despite both divider pins
    # being members.
    assert "VFB" in cell.escaping_nets
    assert cell.internal_nets == frozenset()


@pytest.mark.skipif(not os.path.isfile(_HAND), reason="hand board not present")
def test_harvest_round_trips_through_four_rotations_byte_identically():
    cell = harvest_cell(_HAND, ["RFB1", "RFB2"], "fb_divider",
                        fp_lib_dirs=_fp_dirs())
    turned = cell
    for _ in range(4):
        turned = rotate_cell(turned, 90)
    assert serialise_cell(turned) == serialise_cell(cell)


@pytest.mark.skipif(not os.path.isfile(_HAND), reason="hand board not present")
def test_harvest_is_stable_across_two_calls():
    a = harvest_cell(_HAND, ["RFB1", "RFB2"], "fb", fp_lib_dirs=_fp_dirs())
    b = harvest_cell(_HAND, ["RFB2", "RFB1"], "fb", fp_lib_dirs=_fp_dirs())
    assert a.digest == b.digest      # ⭐ ref order must not change the digest


@pytest.mark.skipif(not os.path.isfile(_HAND), reason="hand board not present")
def test_harvest_rejects_a_ref_the_board_does_not_have():
    with pytest.raises(ValueError):
        harvest_cell(_HAND, ["RFB1", "NOPE"], "fb", fp_lib_dirs=_fp_dirs())
