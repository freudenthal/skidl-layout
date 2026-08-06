# -*- coding: utf-8 -*-
"""The first construction loop -- one anchor, one side (construction arc S3).

⛔⛔ :mod:`skidl_layout.construct` is a **leaf**. Nothing in the engine imports
it and nothing consumes a ``SideResult`` yet, so no test here may reach the
scorer, the refiner or a placement digest.

⚠ The circuits are built from the same small synthetic ``_Part`` / ``_Net`` /
``_Circuit`` harness :mod:`test_cells_partition` uses, for the same reason:
``conftest.py``'s autouse ``mock_active_circuit`` fixture makes a **second**
``@circuit`` build in one test a subcircuit of the first and it dies with
*"Reference collision"*. The footprint **names** are real, so the pad geometry
the ordering rule reads is the library's own.

⚠ The pads are a local dataclass rather than ``kicad_parser.Pad``: the tests
must run without a KiCad-routing-tools checkout, and every field
:func:`~skidl_layout.construct.moved_pads` touches is declared here with the
same name and the same meaning. ⛔ The **real** proof that the moved-pad seam is
exact is gate ``CL0``'s board-rewrite control, not this file -- a fake pad can
only show that the arithmetic is what it says it is.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import pytest

from skidl_layout.construct import (
    ANCHOR_PAD_NOT_ESCAPABLE,
    EDGE_GAP_TERMS,
    LADDER_RUNGS,
    MAX_PUSH_STEPS,
    MAX_SLIDE_STEPS,
    NEIGHBOUR_CLASSES,
    ROTATIONS,
    SIDES,
    ConstructError,
    Neighbour,
    Placement,
    SideResult,
    construct_side,
    courtyard_gap,
    edge_gap_mm,
    moved_pads,
    side_neighbours,
    side_result_to_dict,
    standoff_base_mm,
)
from skidl_layout.construct import (
    _AXES,
    _bind,
    _classify,
    _classify_plan_literal,
    _is_monotone,
    _ladder,
    _pair_gap,
    _pad_key,
    _side_extent,
    _unrotate,
)

_FP_DIRS = None


def _fp_dirs():
    global _FP_DIRS
    if _FP_DIRS is None:
        from skidl_layout.metrics import discover_footprint_dir

        found = discover_footprint_dir()
        _FP_DIRS = [found] if found else []
    return _FP_DIRS


needs_footprints = pytest.mark.skipif(
    not _fp_dirs(), reason="no KiCad footprint library on this host")

SOIC8 = "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm"
R0805 = "Resistor_SMD:R_0805_2012Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
C0402 = "Capacitor_SMD:C_0402_1005Metric"


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #
@dataclass
class _Pad:
    """Every field :func:`moved_pads` reads, with KRT's own names."""

    pad_number: str = "1"
    net_name: str = ""
    net_id: int = 0
    component_ref: str = ""
    global_x: float = 0.0
    global_y: float = 0.0
    local_x: float = 0.0
    local_y: float = 0.0
    size_x: float = 1.0
    size_y: float = 0.5
    shape: str = "rect"
    layers: list = field(default_factory=lambda: ["F.Cu"])
    rotation: float = 0.0
    rect_rotation: float = 0.0
    local_clearance: float = 0.0
    polygons: object = None
    hole_x: object = None
    hole_y: object = None
    pad_type: str = "smd"
    drill: float = 0.0


class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, num, net, name=""):
        self.part = part
        self.num = str(num)
        self.name = name
        self.net = net
        net._pins.append(self)


class _Part:
    def __init__(self, ref, name="", value="", footprint="", pins=(),
                 description=""):
        self.ref = ref
        self.name = name
        self.value = value
        self.footprint = footprint
        self.description = description
        self.pins = []
        for entry in pins:
            self.pins.append(_Pin(self, entry[0], entry[1],
                                  entry[2] if len(entry) > 2 else ""))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = list(parts)
        self._nets = list(nets)

    def get_nets(self):
        return self._nets


CONTROLLER_PINS = ("FB", "COMP", "SW", "VCC", "GND", "BOOT", "SENSE", "RT")


def _tiny_circuit():
    """An 8-pin controller, a decoupling cap on VCC, and a compensation cap.

    ⭐ ``CDEC`` reaches the anchor on **both** of its nets (``VCC`` and
    ``GND``), which is exactly the shape that makes the plan's literal class
    order collapse ``decoupling`` into ``internal``.
    """
    names = ("FB", "COMP", "SW", "VCC", "GND", "BOOT", "SENSE", "RT")
    nets = {name: _Net(name) for name in names}
    anchor = _Part("U1", name="LT9999", footprint=SOIC8,
                   pins=[(str(i + 1), nets[names[i]], CONTROLLER_PINS[i])
                         for i in range(8)])
    cdec = _Part("CDEC", name="C", value="100nF", footprint=C0402,
                 pins=[("1", nets["VCC"]), ("2", nets["GND"])])
    ccomp = _Part("CC", name="C", value="1nF", footprint=C0805,
                  pins=[("1", nets["COMP"]), ("2", nets["GND"])])
    rt = _Part("RT", name="R", value="10k", footprint=R0805,
               pins=[("1", nets["RT"]), ("2", nets["GND"])])
    return _Circuit([anchor, cdec, ccomp, rt], list(nets.values()))


def _anchor_pads(mapping, ref="U1"):
    return [_Pad(pad_number=number, net_name=net, component_ref=ref)
            for number, net in sorted(mapping.items(), key=lambda kv: kv[0])]


# --------------------------------------------------------------------------- #
# The declared constants -- standing finding 20's guard, applied to ourselves
# --------------------------------------------------------------------------- #
def test_every_declared_table_is_non_empty_and_has_no_duplicates():
    """⛔ A declared constant that matches nothing is the observes-nothing
    defect wearing a filter's clothes. Start by proving the tables exist."""
    for table in (SIDES, ROTATIONS, NEIGHBOUR_CLASSES, LADDER_RUNGS,
                  EDGE_GAP_TERMS):
        assert table, f"{table!r} is empty"
        assert len(set(table)) == len(table), f"{table!r} repeats an entry"
    assert MAX_SLIDE_STEPS > 0 and MAX_PUSH_STEPS > 0


def test_the_axis_table_covers_every_side_exactly_once():
    assert sorted(_AXES) == sorted(SIDES)
    #: ⭐ KiCad's y grows downward and the escape map agrees, so N is -y.
    assert _AXES["N"][1] < 0 and _AXES["S"][1] > 0
    assert _AXES["W"][1] < 0 and _AXES["E"][1] > 0
    assert _AXES["W"][0] == _AXES["E"][0] == 0
    assert _AXES["N"][0] == _AXES["S"][0] == 1


def test_the_ladder_never_pushes_before_it_slides_or_rotates():
    """⛔ Overview 7.2: **never slide perpendicular first** -- that consumes the
    fanout allowance, which is the one thing the standoff exists to protect."""
    rungs = list(_ladder(90))
    assert rungs[0] == (LADDER_RUNGS[0], 90, 0, 0)
    first_push = next(i for i, r in enumerate(rungs) if r[3])
    first_rotation = next(i for i, r in enumerate(rungs) if r[1] != 90)
    last_slide = max(i for i, r in enumerate(rungs) if r[2])
    assert last_slide < first_rotation < first_push
    assert len(rungs) == 1 + MAX_SLIDE_STEPS + (len(ROTATIONS) - 1) \
        + MAX_PUSH_STEPS
    assert all(r[1] in ROTATIONS for r in rungs)


def test_the_ladder_is_bounded():
    assert len(list(_ladder(0))) < 32


def test_unrotate_inverts_the_side_rotation_on_every_side_and_angle():
    from skidl_layout.escape_map import rotate_escape

    for side in SIDES:
        for deg in ROTATIONS:
            assert rotate_escape(_unrotate(side, deg), deg) == side


# --------------------------------------------------------------------------- #
# moved_pads -- the seam gate CL0 proves against a board-rewrite control
# --------------------------------------------------------------------------- #
def test_moved_pads_translates_from_the_local_frame():
    pads = [_Pad(local_x=1.0, local_y=2.0, global_x=99.0, global_y=99.0)]
    moved = moved_pads(pads, x_mm=10.0, y_mm=20.0, rot_deg=0)
    assert (moved[0].global_x, moved[0].global_y) == (11.0, 22.0)
    #: ⛔ the footprint-frame position is what the transform STARTS from and
    #: must never be rewritten -- that is what makes this absolute.
    assert (moved[0].local_x, moved[0].local_y) == (1.0, 2.0)


def test_moved_pads_swaps_the_baked_in_size_at_ninety_degrees():
    """⛔⛔ ``kicad_parser._resolve_pad_rect`` bakes a ~90 deg pad rotation into
    ``size_x``/``size_y``. A rect pad stamped without the swap blocks the wrong
    rectangle and **nothing says so**."""
    pads = [_Pad(size_x=1.5, size_y=0.35)]
    for deg in (90, 270):
        moved = moved_pads(pads, x_mm=0.0, y_mm=0.0, rot_deg=deg)
        assert (moved[0].size_x, moved[0].size_y) == (0.35, 1.5), deg
    for deg in (0, 180):
        moved = moved_pads(pads, x_mm=0.0, y_mm=0.0, rot_deg=deg)
        assert (moved[0].size_x, moved[0].size_y) == (1.5, 0.35), deg


def test_moved_pads_takes_the_rotation_delta_not_the_new_angle():
    """⚠ ``Pad.rotation`` is the pad's ABSOLUTE board angle -- the footprint's
    rotation is already folded in, per the dataclass's own comment."""
    pads = [_Pad(rotation=45.0)]
    moved = moved_pads(pads, x_mm=0.0, y_mm=0.0, rot_deg=90, base_rot_deg=0.0)
    assert moved[0].rotation == pytest.approx(135.0)
    same = moved_pads(pads, x_mm=0.0, y_mm=0.0, rot_deg=90, base_rot_deg=90.0)
    assert same[0].rotation == pytest.approx(45.0)


def test_moved_pads_with_a_base_rotation_is_the_identity_at_the_same_angle():
    pads = [_Pad(local_x=1.0, local_y=0.0, global_x=1.0, global_y=0.0,
                 size_x=1.5, size_y=0.35, rotation=90.0)]
    moved = moved_pads(pads, x_mm=0.0, y_mm=0.0, rot_deg=90,
                       base_rot_deg=90.0)
    assert moved[0].size_x == 1.5 and moved[0].size_y == 0.35
    assert moved[0].rotation == pytest.approx(90.0)


def test_moved_pads_transforms_a_custom_pad_polygon_with_its_pad():
    """⛔ ``polygons`` are in GLOBAL coordinates -- a custom-shaped pad whose
    outline is not transformed stamps its copper at the old place."""
    pads = [_Pad(local_x=0.0, local_y=0.0, global_x=5.0, global_y=5.0,
                 polygons=[[(5.5, 5.0), (5.0, 5.5)]])]
    moved = moved_pads(pads, x_mm=10.0, y_mm=0.0, rot_deg=90)
    (ax, ay), (bx, by) = moved[0].polygons[0]
    # the pad centre moved to (10, 0); its +x offset turns onto -y at +90
    assert (round(ax, 6), round(ay, 6)) == (10.0, -0.5)
    assert (round(bx, 6), round(by, 6)) == (10.5, 0.0)


def test_moved_pads_moves_an_offset_drill_hole_with_the_copper():
    pads = [_Pad(local_x=0.0, local_y=0.0, global_x=5.0, global_y=5.0,
                 hole_x=5.25, hole_y=5.0)]
    moved = moved_pads(pads, x_mm=1.0, y_mm=1.0, rot_deg=180)
    assert round(moved[0].hole_x, 6) == 0.75
    assert round(moved[0].hole_y, 6) == 1.0


def test_moved_pads_leaves_a_pad_with_no_polygons_or_hole_alone():
    pads = [_Pad()]
    moved = moved_pads(pads, x_mm=3.0, y_mm=4.0, rot_deg=270)
    assert moved[0].polygons is None
    assert moved[0].hole_x is None and moved[0].hole_y is None


def test_moved_pads_folds_a_residual_rect_rotation_into_the_open_interval():
    pads = [_Pad(rect_rotation=60.0)]
    moved = moved_pads(pads, x_mm=0.0, y_mm=0.0, rot_deg=90)
    assert -90.0 < moved[0].rect_rotation <= 90.0
    assert moved[0].rect_rotation == pytest.approx(-30.0)


def test_moved_pads_agrees_with_the_geometry_transform():
    """⭐ One rotation convention in this arc, not two."""
    from skidl_layout.geometry import transform_point

    pads = [_Pad(local_x=1.3, local_y=-0.7)]
    for deg in ROTATIONS:
        moved = moved_pads(pads, x_mm=7.0, y_mm=11.0, rot_deg=deg)
        want = transform_point(7.0, 11.0, deg, 1.3, -0.7)
        assert (round(moved[0].global_x, 9),
                round(moved[0].global_y, 9)) == (round(want[0], 9),
                                                 round(want[1], 9))


# --------------------------------------------------------------------------- #
# The standoff -- from the FabSpec, never a constant
# --------------------------------------------------------------------------- #
def test_the_standoff_base_is_derived_from_the_fab_spec():
    from skidl_layout.fabspec import resolve_fab_spec
    from skidl_layout.power_escape import lane_from_fab

    fab = resolve_fab_spec("oshpark-2l")
    assert standoff_base_mm(fab) == pytest.approx(2.0048)
    assert standoff_base_mm(fab) == pytest.approx(
        2 * (fab.track_width_mm + fab.clearance_mm) + lane_from_fab(fab))


def test_the_standoff_base_moves_with_the_fab_spec_and_is_not_a_table():
    from skidl_layout.fabspec import resolve_fab_spec

    from dataclasses import replace

    #: ⚠ Every shipped preset carries the SAME 0.3/0.25/0.6 triple, so a
    #: preset-against-preset comparison would assert nothing. A perturbed spec
    #: is what actually shows the standoff is a derivation and not a table.
    two = resolve_fab_spec("oshpark-2l")
    finer = replace(two, track_width_mm=0.2, clearance_mm=0.2,
                    via_size_mm=0.7)
    assert standoff_base_mm(finer) != standoff_base_mm(two)
    assert edge_gap_mm(finer) == pytest.approx(0.6)


def test_the_edge_gap_is_one_passing_trace_with_clearance_on_both_sides():
    from skidl_layout.fabspec import resolve_fab_spec

    fab = resolve_fab_spec("oshpark-2l")
    assert edge_gap_mm(fab) == pytest.approx(
        fab.track_width_mm + 2 * fab.clearance_mm)
    assert edge_gap_mm(fab) == pytest.approx(0.8)


def test_the_side_extent_measures_from_the_part_origin_towards_the_side():
    box = (-3.0, -2.0, 4.0, 5.0)
    assert _side_extent(box, 0.0, 0.0, "W") == 3.0
    assert _side_extent(box, 0.0, 0.0, "E") == 4.0
    assert _side_extent(box, 0.0, 0.0, "N") == 2.0
    assert _side_extent(box, 0.0, 0.0, "S") == 5.0


# --------------------------------------------------------------------------- #
# The courtyard rule -- the same rule as cells_families, not a second one
# --------------------------------------------------------------------------- #
def test_the_pair_gap_clears_when_EITHER_axis_separates():
    a = (0.0, 0.0, 1.0, 1.0)
    beside = (2.0, 0.0, 3.0, 1.0)
    below = (0.0, 2.0, 1.0, 3.0)
    overlapping = (0.5, 0.5, 1.5, 1.5)
    assert _pair_gap(a, beside) == pytest.approx(1.0)
    assert _pair_gap(a, below) == pytest.approx(1.0)
    assert _pair_gap(a, overlapping) < 0


def test_courtyard_gap_is_infinite_for_fewer_than_two_boxes():
    assert courtyard_gap({}) == float("inf")
    assert courtyard_gap({"A": (0.0, 0.0, 1.0, 1.0)}) == float("inf")


@needs_footprints
def test_courtyard_gap_agrees_with_cells_families_member_courtyard_gap():
    """⭐ The plan named ``cells_families.member_courtyard_gap`` for this check
    and that function takes a **LayoutCell**, not placements. This asserts the
    two are the same *rule* over the same geometry, so :func:`courtyard_gap` is
    not a second rule quietly disagreeing with the shipped one."""
    from skidl_layout.cells import synthesise_cell
    from skidl_layout.cells_families import member_courtyard_gap
    from skidl_layout.construct import _courtyard_box
    from skidl_layout.geometry import load_footprint_geometries

    dirs = _fp_dirs()
    members = [("R1", R0805, 0.0, 0.0, 0), ("R2", R0805, 3.0, 0.0, 0),
               ("C1", C0402, 0.0, 3.0, 90)]
    cell = synthesise_cell(name="@t", members=members,
                           nets={"N1": [("R1", "1"), ("R2", "1")]},
                           fp_lib_dirs=dirs)
    theirs = member_courtyard_gap(cell, dirs)
    geometries = load_footprint_geometries({R0805, C0402}, dirs)
    boxes = {member.local_ref: _courtyard_box(
        geometries[member.footprint], member.local_ref, member.dx, member.dy,
        member.rotation) for member in cell.part_members}
    assert courtyard_gap(boxes) == pytest.approx(theirs, abs=1e-6)


@needs_footprints
def test_the_courtyard_box_is_larger_than_the_physical_box():
    """⛔ Standing finding 13: they are DIFFERENT boxes, and confusing them
    shipped 7 of 7 cells with overlapping courtyards while every gate passed."""
    from skidl_layout.construct import _courtyard_box, _physical_box
    from skidl_layout.geometry import load_footprint_geometries

    geometry = load_footprint_geometries({R0805}, _fp_dirs())[R0805]
    court = _courtyard_box(geometry, "R1", 0.0, 0.0, 0)
    phys = _physical_box(geometry, "R1", 0.0, 0.0, 0)
    assert court[0] < phys[0] and court[2] > phys[2]
    assert round(phys[0] - court[0], 3) >= 0.1


# --------------------------------------------------------------------------- #
# P1 -- the classes, and the order the plan got wrong
# --------------------------------------------------------------------------- #
class _Binding:
    def __init__(self, ref, anchor_pad, net, role):
        self.ref, self.anchor_pad = ref, anchor_pad
        self.net, self.role = net, role


def test_a_pin_binding_beats_every_net_touches_the_anchor():
    """⛔⛔ **The plan's own class order makes ``decoupling`` unreachable.** A
    decoupling capacitor's supply pad AND its ground pad are both on nets the
    anchor touches, so the plan's ``internal`` test swallows it on every corpus
    board. The more specific classifier -- the partition's own netlist-derived
    ``PinBinding`` -- wins."""
    anchor_nets = {"VCC", "GND"}
    nets_by_ref = {"CDEC": ["GND", "VCC"]}
    bindings = {"CDEC": _Binding("CDEC", "4", "VCC", "decap")}
    assert _classify("CDEC", anchor_nets, nets_by_ref, bindings)[0] == \
        "decoupling"
    #: ⚠ and this is what the plan's stated order WOULD have said
    assert _classify_plan_literal("CDEC", anchor_nets, nets_by_ref,
                                  bindings) == "internal"


def test_a_part_wholly_inside_the_anchor_with_no_binding_is_internal():
    anchor_nets = {"COMP", "GND"}
    nets_by_ref = {"CC": ["COMP", "GND"]}
    assert _classify("CC", anchor_nets, nets_by_ref, {})[0] == "internal"


def test_a_part_with_a_net_the_anchor_does_not_touch_is_one_pad():
    anchor_nets = {"GATE"}
    nets_by_ref = {"RG": ["GATE", "DRIVE"]}
    assert _classify("RG", anchor_nets, nets_by_ref, {})[0] == "one_pad"


def test_every_declared_neighbour_class_is_reachable_by_some_input():
    """⛔ A class that could never match anything is a defect, not a report."""
    seen = {
        _classify("A", {"V"}, {"A": ["V"]},
                  {"A": _Binding("A", "1", "V", "bulk")})[0],
        _classify("B", {"V", "G"}, {"B": ["V", "G"]}, {})[0],
        _classify("C", {"V"}, {"C": ["V", "X"]}, {})[0],
    }
    assert seen == {"decoupling", "internal", "one_pad"}
    #: ``template`` comes from a *cell*, not a part -- see
    #: :func:`side_neighbours`, which inventories family groups.
    assert "template" in NEIGHBOUR_CLASSES


def test_bind_takes_the_net_and_the_pad_from_the_SAME_decision():
    """⛔⛔ A first cut took the pad from the binding and the net from the
    plane-free preference, and paired ``U1`` pad 1 (``VIN``) with a ``PGND``
    pad -- two different nets. The router cannot tell you the question was
    wrong; it can only tell you the answer is no."""
    by_net = {"VIN": ["1"], "PGND": ["12"]}
    net_of_pad = {"1": "VIN", "12": "PGND"}
    bindings = {"CIN1": _Binding("CIN1", "1", "VIN", "bulk")}
    net, pad, warning = _bind("CIN1", ["PGND", "VIN"], by_net, net_of_pad,
                              bindings)
    assert (net, pad) == ("VIN", "1")
    assert warning == ""


def test_bind_warns_when_the_binding_names_a_pad_that_carries_another_net():
    by_net = {"VIN": ["1"]}
    net_of_pad = {"1": "SW"}
    bindings = {"CIN1": _Binding("CIN1", "1", "VIN", "bulk")}
    _net, _pad, warning = _bind("CIN1", ["VIN"], by_net, net_of_pad, bindings)
    assert "carries" in warning


def test_bind_prefers_a_plane_free_net_when_there_is_no_binding():
    """⛔ Standing finding 5: a part whose only shared net is ``GND`` is not
    *"beside that pad"* in any useful sense."""
    by_net = {"GND": ["5"], "COMP": ["2"]}
    net_of_pad = {"5": "GND", "2": "COMP"}
    net, pad, _w = _bind("CC", ["COMP", "GND"], by_net, net_of_pad, {})
    assert (net, pad) == ("COMP", "2")


def test_bind_falls_back_to_a_plane_net_rather_than_giving_up():
    by_net = {"GND": ["5"]}
    net_of_pad = {"5": "GND"}
    assert _bind("CX", ["GND"], by_net, net_of_pad, {})[0] == "GND"


def test_bind_returns_none_when_no_pad_of_the_anchor_carries_a_shared_net():
    assert _bind("CX", ["NOWHERE"], {"GND": ["5"]}, {"5": "GND"}, {}) is None


def test_the_pad_key_sorts_two_before_ten():
    assert sorted(["10", "2", "1"], key=_pad_key) == ["1", "2", "10"]
    assert sorted(["A1", "2"], key=_pad_key) == ["2", "A1"]


# --------------------------------------------------------------------------- #
# P2 -- the side list, and its order
# --------------------------------------------------------------------------- #
@needs_footprints
def _partition_and_map():
    from skidl_layout.cells_partition import partition_circuit
    from skidl_layout.escape_map import escape_map_for
    from skidl_layout.fabspec import resolve_fab_spec
    from skidl_layout.power_escape import lane_from_fab

    dirs = _fp_dirs()
    fab = resolve_fab_spec("oshpark-2l")
    partition = partition_circuit(_tiny_circuit(), fp_lib_dirs=dirs,
                                  board="tiny")
    emap = escape_map_for(SOIC8, dirs, lane_mm=lane_from_fab(fab),
                          clearance_mm=fab.clearance_mm)
    return partition, emap, dirs, fab


@needs_footprints
def test_side_neighbours_orders_by_the_measured_pad_coordinate():
    """⭐ **P2's ordering rule, and it is the important one.** The list is a
    projection of the anchor's pin order onto the edge. ⛔ Never the pad
    NUMBER -- that is true of an MSOP and false of a QFN."""
    from skidl_layout.geometry import load_footprint_geometries

    partition, emap, dirs, _fab = _partition_and_map()
    geometry = load_footprint_geometries({SOIC8}, dirs)[SOIC8]
    pads = _anchor_pads({"1": "FB", "2": "COMP", "3": "SW", "4": "VCC",
                         "5": "GND", "6": "BOOT", "7": "SENSE", "8": "RT"})
    found = side_neighbours(partition, "U1", emap, side="W",
                            anchor_pads=pads, anchor_geometry=geometry)
    assert found, "an empty side list here would make every later test vacuous"
    positions = {str(pad.number): pad.y_mm for pad in geometry.pads}
    keys = [positions[n.anchor_pad] for n in found]
    assert keys == sorted(keys), f"{[(n.ref, n.anchor_pad) for n in found]}"
    #: the order key is total over content
    assert all(isinstance(n.order_key, tuple) and len(n.order_key) == 3
               for n in found)


@needs_footprints
def test_side_neighbours_is_stable_under_a_permuted_group_listing():
    partition, emap, dirs, _fab = _partition_and_map()
    from skidl_layout.geometry import load_footprint_geometries

    geometry = load_footprint_geometries({SOIC8}, dirs)[SOIC8]
    mapping = {"1": "FB", "2": "COMP", "3": "SW", "4": "VCC", "5": "GND",
               "6": "BOOT", "7": "SENSE", "8": "RT"}
    forward = side_neighbours(partition, "U1", emap, side="W",
                              anchor_pads=_anchor_pads(mapping),
                              anchor_geometry=geometry)
    backward = side_neighbours(partition, "U1", emap, side="W",
                               anchor_pads=list(reversed(
                                   _anchor_pads(mapping))),
                               anchor_geometry=geometry)
    assert [n.to_dict() for n in forward] == [n.to_dict() for n in backward]


@needs_footprints
def test_side_neighbours_puts_each_neighbour_on_exactly_one_side():
    """⚠ Overview open question 3 -- the draft assumes one connecting pad per
    neighbour per side. Asserted rather than assumed."""
    partition, emap, dirs, _fab = _partition_and_map()
    from skidl_layout.geometry import load_footprint_geometries

    geometry = load_footprint_geometries({SOIC8}, dirs)[SOIC8]
    pads = _anchor_pads({"1": "FB", "2": "COMP", "3": "SW", "4": "VCC",
                         "5": "GND", "6": "BOOT", "7": "SENSE", "8": "RT"})
    seen: dict = {}
    for side in SIDES:
        for neighbour in side_neighbours(partition, "U1", emap, side=side,
                                         anchor_pads=pads,
                                         anchor_geometry=geometry):
            seen.setdefault(neighbour.ref, []).append(side)
    assert seen, "no neighbour on any side -- rule 3"
    duplicated = {ref: sides for ref, sides in seen.items() if len(sides) > 1}
    assert not duplicated, duplicated


@needs_footprints
def test_side_neighbours_refuses_an_anchor_that_is_in_no_group():
    partition, emap, dirs, _fab = _partition_and_map()
    with pytest.raises(ConstructError):
        side_neighbours(partition, "NOSUCH", emap, side="W",
                        anchor_pads=_anchor_pads({"1": "VCC"}))


@needs_footprints
def test_side_neighbours_refuses_a_side_that_is_not_a_side():
    partition, emap, _dirs, _fab = _partition_and_map()
    with pytest.raises(ConstructError):
        side_neighbours(partition, "U1", emap, side="NE",
                        anchor_pads=_anchor_pads({"1": "VCC"}))


@needs_footprints
def test_side_neighbours_raises_when_no_pad_carries_a_net():
    """⛔ An inventory built on a pad set with no nets observes nothing."""
    partition, emap, _dirs, _fab = _partition_and_map()
    with pytest.raises(ConstructError):
        side_neighbours(partition, "U1", emap, side="W",
                        anchor_pads=[_Pad(pad_number="1", net_name="")])


# --------------------------------------------------------------------------- #
# The result object
# --------------------------------------------------------------------------- #
def _result(**kwargs):
    base = dict(board="b", anchor="U1", side="W", neighbours=(),
                placements=(), failures=(), skipped=(),
                standoff_base_mm=2.0048, meta={})
    base.update(kwargs)
    return SideResult(**base)


def _placement(ref, x, y, routed=True):
    return Placement(ref=ref, x_mm=x, y_mm=y, rot_deg=0, standoff_mm=3.0,
                     slide_steps=0, routed=routed, route_length_mm=1.0,
                     route_iterations=1)


def test_routed_fraction_is_zero_rather_than_a_division_by_zero():
    assert _result().routed_fraction == 0.0


def test_routed_fraction_counts_the_routed_placements():
    result = _result(placements=(_placement("A", 0, 0),
                                 _placement("B", 0, 1, routed=False)))
    assert result.routed_fraction == 0.5


def test_legal_reads_BOTH_boxes():
    """⛔ Overview 7.1. The courtyard box is the one nothing checked until
    2026-08-03, and KiCad DRC is not the backstop."""
    assert _result(meta={}).legal
    assert not _result(meta={"physical_overlaps": [["A", "B"]]}).legal
    assert not _result(meta={"courtyard_overlaps": [["A", "B"]]}).legal


def test_the_serialised_form_is_json_and_order_stable():
    import json

    result = _result(
        neighbours=(Neighbour(ref="R1", anchor_pad="1", net="N", side="W",
                              klass="one_pad", priority=1,
                              order_key=(0.0, (0, 1, "1"), "R1")),),
        placements=(_placement("R1", 1.0, 2.0),),
        skipped=({"ref": "R2", "why": ANCHOR_PAD_NOT_ESCAPABLE},))
    blob = side_result_to_dict(result)
    assert json.dumps(blob, sort_keys=True, default=str) == \
        json.dumps(side_result_to_dict(result), sort_keys=True, default=str)
    assert blob["skipped"][0]["why"] == ANCHOR_PAD_NOT_ESCAPABLE


def test_monotone_reads_the_axis_the_side_runs_along():
    rising = [_placement("A", 0.0, 1.0), _placement("B", 0.0, 2.0)]
    falling = [_placement("A", 0.0, 2.0), _placement("B", 0.0, 1.0)]
    assert _is_monotone(rising, "W") and not _is_monotone(falling, "W")
    #: on N/S the edge runs along x, so the same y values say nothing
    assert _is_monotone(rising, "N")


def test_construct_side_refuses_an_anchor_it_has_no_geometry_for():
    with pytest.raises(ConstructError):
        construct_side(object(), "U1", side="W", session=None, geometries={},
                       fab=None, escape_map=None)


# --------------------------------------------------------------------------- #
# ⛔ The leaf guard -- import-aware from the start (standing finding 16)
# --------------------------------------------------------------------------- #
#: ⚠ **This guard reads IMPORT STATEMENTS, never the module name as a
#: substring.** A sibling's used to do the latter and a *docstring* that said
#: the name out loud failed it, costing a full ~9-minute suite run. The
#: property being guarded is "nothing imports it"; prose about a module is
#: documentation and this arc wants more of it, not less. ⛔ Do not "simplify"
#: this back to ``name in text``.
_CONSTRUCT_IMPORT_RE = re.compile(
    r"^[ \t]*(?:"
    r"from[ \t]+[.\w]*\bconstruct\b[ \t]+import"
    #: ⚠ ``[^#\n]`` and not ``[^\n]``: a trailing comment on an unrelated
    #: import is prose, and prose is allowed.
    r"|from[ \t]+[.\w]+[ \t]+import[ \t]+[^#\n]*\bconstruct\b"
    r"|import[ \t]+[^#\n]*\bconstruct\b"
    r")"
    r"|import_module\([^)]*\bconstruct\b",
    re.MULTILINE,
)


def test_construct_is_a_leaf_no_module_imports_it():
    """⛔ Nothing in ``skidl_layout`` may import this module."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    importers = []
    for path in sorted(root.glob("*.py")):
        if path.name == "construct.py":
            continue
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if _CONSTRUCT_IMPORT_RE.search(line):
                importers.append(f"{path.name}:{number}: {line.strip()}")
    assert importers == [], f"construct is imported by {importers}"


def test_the_leaf_guard_matches_imports_and_not_prose():
    """⭐ The guard's own guard -- a pattern that is too narrow stops guarding
    *silently*, which is worse than the false positive that started this."""
    imports = [
        "from .construct import construct_side",
        "from construct import construct_side",
        "from skidl_layout.construct import SideResult",
        "    from .construct import SideResult",              # function-local
        "from . import construct",
        "from skidl_layout import construct",
        "import construct",
        "import skidl_layout.construct",
        "import skidl_layout.construct as CS",
        '    mod = importlib.import_module("skidl_layout.construct")',
    ]
    prose = [
        "# same leaf discipline as construct",
        '    """Consumed by :mod:`~skidl_layout.construct`."""',
        "    # ⛔ do not import construct here",
        "CONSTRUCT_NOTE = 'construct stays a leaf'",
        "#: construct is the sibling this mirrors",
        "from typing import Literal  # construct mirrors this vocabulary",
        "import os  # construct does its own path handling",
    ]
    missed = [line for line in imports
              if not _CONSTRUCT_IMPORT_RE.search(line)]
    tripped = [line for line in prose if _CONSTRUCT_IMPORT_RE.search(line)]
    assert not missed, f"the guard would MISS these real imports: {missed}"
    assert not tripped, f"the guard falsely trips on this prose: {tripped}"


def test_the_package_does_not_re_export_the_construction_api():
    import skidl_layout

    for name in ("construct_side", "side_neighbours", "SideResult",
                 "Neighbour", "Placement", "moved_pads", "construct_cell",
                 "CellResult", "TightenStep", "side_order",
                 "ring2_subanchor", "template_port_member",
                 "cell_result_to_dict"):
        assert not hasattr(skidl_layout, name), \
            f"skidl_layout re-exports {name} -- the leaf rule is broken"


def test_the_module_does_not_import_the_engine_or_the_scorer():
    """⛔ S3 is a SEARCH change and touches no default. It may call geometry,
    the validator and the session; it may not reach the scorer or the engine."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    for banned in ("scoring", "refinement", "engine", "candidates",
                   "placer"):
        pattern = re.compile(rf"^[ \t]*(?:from[ \t]+[.\w]*\b{banned}\b|"
                             rf"import[ \t]+[^#\n]*\b{banned}\b)",
                             re.MULTILINE)
        assert not pattern.search(text), f"construct.py imports {banned}"


def test_math_is_the_only_heavy_dependency():
    """⚠ A leaf that grew a dependency is a leaf that is about to stop being
    one. ``math`` and ``dataclasses`` are the whole top-level import list."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    top = [line for line in text.splitlines()
           if re.match(r"^(import|from) ", line)]
    assert top == ["from __future__ import annotations", "import math",
                   "from dataclasses import dataclass, replace"], top


assert math  # the module under test uses it; keep the import honest


# =========================================================================== #
# S4 -- P3, the flattened template, ring 2, and the tighten pass
# =========================================================================== #
from skidl_layout.construct import (                             # noqa: E402
    MAX_RINGS,
    TIGHTEN_FLOORS,
    TIGHTEN_STOP_REASONS,
    CellResult,
    TightenStep,
    cell_result_to_dict,
    construct_cell,
    ring2_subanchor,
    side_order,
    template_port_member,
)
from skidl_layout.construct import (                             # noqa: E402
    _allowance_of,
    _connecting_pad_offsets,
    _distribute_stacked,
    _position_for,
)


def test_the_S4_declared_tables_are_non_empty_and_have_no_duplicates():
    """⛔ Standing finding 20, applied to S4's own constants before the run."""
    for table in (TIGHTEN_STOP_REASONS, TIGHTEN_FLOORS):
        assert table and len(set(table)) == len(table), table
    assert MAX_RINGS == 2, "ring 2 is bounded; a third ring is S5's problem"


def test_the_ring_bound_is_what_the_module_actually_implements():
    """⛔ ``MAX_RINGS`` is a **declared bound**, and standing finding 20 says a
    declared constant must be checked against what the code does rather than
    against what the comment says. The bound is structural -- there is one
    ring-2 pass and no third -- so the guard is that no third ring ever appears
    without the constant moving with it."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    assert MAX_RINGS == 2
    assert "ring3" not in text and "ring_3" not in text
    #: the ring-2 pass exists exactly once and its leftovers are logged
    assert text.count("ring2_candidates = sorted(") == 1
    assert "ring2_failures" in text
    for field in ("ring2", "ring2_failures"):
        assert field in CellResult.__dataclass_fields__


def test_every_declared_tighten_stop_reason_is_produced_by_the_code():
    """⛔ A declared reason nothing emits is the observes-nothing defect wearing
    a filter's clothes. The set the source assigns must equal the declared set.

    ⚠ ``cap`` is a **backstop** the ``allowance`` floor provably pre-empts (see
    :func:`test_the_tighten_cap_cannot_bind_under_the_allowance_floor`); it is
    still *assigned* in the source, and the driver reports it as unreachable
    rather than dropping it."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    body = text.split("def _tighten_cell")[1].split("\ndef ")[0]
    assigned = {value for value in re.findall(r'"(\w+)"', body)
                if value in set(TIGHTEN_STOP_REASONS)}
    assert assigned == set(TIGHTEN_STOP_REASONS), assigned


def test_the_tighten_cap_cannot_bind_under_the_allowance_floor():
    """⛔⛔ **The plan's own cap is unreachable, and this proves it rather than
    reporting it.** The floor spends the allowance in
    ``floor(allowance / step)`` steps; the cap is
    ``ceil((floor_offset + allowance) / step)`` with ``floor_offset >= 0``, so
    the cap is never the smaller of the two. ⭐ Keeping the constant and proving
    it is a backstop is the honest form of standing finding 20 -- silently
    dropping it, or letting it sit there unexplained, are the two failures."""
    step = 0.25
    for allowance in (2.0048, 4.0096, 0.5, 1.0):
        for floor_offset in (0.0, 0.3, 1.5, 6.2):
            standoff = floor_offset + allowance
            cap = math.ceil(standoff / step)
            floor_steps = math.floor(allowance / step)
            assert floor_steps <= cap, (allowance, floor_offset)


# --------------------------------------------------------------------------- #
# The flattened template -- section 2.4's measured defect, fixed by construction
# --------------------------------------------------------------------------- #
_SNUBBER = {"CC1": ["GND", "ITH_C"], "RC": ["ITH", "ITH_C"]}


def test_the_port_member_is_the_one_with_a_pad_on_the_bound_net():
    """⛔⛔ **The measured defect this fixes.** ``side_neighbours`` used the
    alphabetically first member as a template's representative, and on
    ``ltc1871_sepic``'s ``rc_snubber:CC1-RC`` -- bound to ``ITH`` -- that member
    is ``CC1``, **which has no ITH pad at all**. Harmless in S3 only because
    templates were never routed."""
    ref, why = template_port_member(("CC1", "RC"), "ITH", {"ITH", "GND"},
                                    _SNUBBER)
    assert ref == "RC", why
    assert "ITH" in why


def test_the_port_member_regression_for_the_second_measured_instance():
    """``lt3844_buck``'s ``rc_snubber:CC-RC``, bound to ``VC``: the shipped
    representative is ``CC`` and ``CC`` has no ``VC`` pad."""
    nets = {"CC": ["SGND", "VC_C"], "RC": ["VC", "VC_C"]}
    assert template_port_member(("CC", "RC"), "VC", {"VC", "SGND"},
                                nets)[0] == "RC"


def test_the_port_member_breaks_a_tie_on_plane_free_shared_nets():
    """``lt3844_buck``'s ``divider:RFB1-RFB2``: both touch ``VFB``, and
    ``RFB1`` shares ``VOUT`` with the anchor as well while ``RFB2``'s extra net
    is ``SGND`` -- a plane net, which standing finding 5 says does not count."""
    nets = {"RFB1": ["VFB", "VOUT"], "RFB2": ["SGND", "VFB"]}
    assert template_port_member(("RFB1", "RFB2"), "VFB",
                                {"VFB", "VOUT", "SGND"}, nets)[0] == "RFB1"


def test_the_port_member_breaks_a_total_tie_on_the_smallest_ref():
    nets = {"RA": ["FB"], "RB": ["FB"]}
    assert template_port_member(("RB", "RA"), "FB", {"FB"}, nets)[0] == "RA"


def test_the_port_member_raises_when_no_member_carries_the_net():
    """⛔ Rule 3: it may not fall back to a guess. A template whose members do
    not touch the net it was bound to is a contradiction between two shipped
    tables, and bail-out 6 is the right answer."""
    with pytest.raises(ConstructError):
        template_port_member(("CC1", "RC"), "NOWHERE", {"NOWHERE"}, _SNUBBER)


# --------------------------------------------------------------------------- #
# The stacked-binding distribution
# --------------------------------------------------------------------------- #
class _FakeMap:
    """The smallest thing :func:`_distribute_stacked` reads: a favoured side
    per pad and a footprint name."""

    def __init__(self, favoured, footprint="Lib:FP"):
        self._favoured = dict(favoured)
        self.footprint = footprint
        self.entries = ()

    def escapable_sides(self, pad):
        return (self._favoured[str(pad)],) if str(pad) in self._favoured else ()


def _fake_favored(monkeypatch, mapping):
    import skidl_layout.escape_map as EM

    monkeypatch.setattr(EM, "favored_side",
                        lambda emap, pad, layer=0: mapping[str(pad)])


def _n(ref, pad, net, key):
    return Neighbour(ref=ref, anchor_pad=pad, net=net, side="W",
                     klass="decoupling", priority=302,
                     order_key=(key, _pad_key(pad), ref))


def test_distribution_spreads_a_stack_over_the_rails_own_pads(monkeypatch):
    """⭐ ``lt8710_sepic``'s ``COUT1..5``: five parts bind to pad 6 while the
    ``VOUT`` rail carries pads **6 and 8**, both on the same side. ⚠ The
    multiplicities come from the PARSED pads, never from the binding's prose."""
    _fake_favored(monkeypatch, {"6": "W", "8": "W"})
    members = [_n(f"COUT{i}", "6", "VOUT", 0.0) for i in range(1, 6)]
    out, moves = _distribute_stacked(
        members, {"VOUT": ["6", "8"]}, _FakeMap({"6": "W", "8": "W"}), 0, "W",
        {"6": (0.0, 1.0), "8": (0.0, 3.0)}, "Lib:FP")
    assert [(n.ref, n.anchor_pad) for n in out] == [
        ("COUT1", "6"), ("COUT3", "6"), ("COUT5", "6"),
        ("COUT2", "8"), ("COUT4", "8")]
    assert [m["ref"] for m in moves] == ["COUT2", "COUT4"]


def test_distribution_does_nothing_when_the_rail_has_one_pad_on_that_side(
        monkeypatch):
    """⭐ ``lt8710_sepic``'s ``CIN1..7``: seven parts, and ``VIN`` has exactly
    one pad. *Twelve parts against two pads is a hint, not a fanout* -- and one
    pad is not even a hint. It stays a cursor stack, and what that costs is
    LENGTH, not legality."""
    _fake_favored(monkeypatch, {"13": "E"})
    members = [_n(f"CIN{i}", "13", "VIN", 0.0) for i in range(1, 8)]
    out, moves = _distribute_stacked(
        members, {"VIN": ["13"]}, _FakeMap({"13": "E"}), 0, "E",
        {"13": (0.0, 1.0)}, "Lib:FP")
    assert moves == [] and list(out) == members


def test_distribution_does_nothing_for_a_single_neighbour(monkeypatch):
    _fake_favored(monkeypatch, {"6": "W", "8": "W"})
    members = [_n("COUT1", "6", "VOUT", 0.0)]
    out, moves = _distribute_stacked(
        members, {"VOUT": ["6", "8"]}, _FakeMap({"6": "W", "8": "W"}), 0, "W",
        {"6": (0.0, 1.0), "8": (0.0, 3.0)}, "Lib:FP")
    assert moves == [] and list(out) == members


def test_distribution_never_reaches_a_pad_on_another_side(monkeypatch):
    """⛔ Never across sides in v1 -- spreading a bank over two sides is a
    design decision S4 has no mandate for."""
    _fake_favored(monkeypatch, {"6": "W", "8": "E"})
    members = [_n(f"COUT{i}", "6", "VOUT", 0.0) for i in range(1, 4)]
    _out, moves = _distribute_stacked(
        members, {"VOUT": ["6", "8"]}, _FakeMap({"6": "W", "8": "E"}), 0, "W",
        {"6": (0.0, 1.0), "8": (0.0, 3.0)}, "Lib:FP")
    assert moves == []


def test_distribution_leaves_the_result_sorted_by_the_order_key(monkeypatch):
    _fake_favored(monkeypatch, {"6": "W", "8": "W"})
    members = [_n(f"COUT{i}", "6", "VOUT", 0.0) for i in range(1, 5)]
    out, _moves = _distribute_stacked(
        members, {"VOUT": ["6", "8"]}, _FakeMap({"6": "W", "8": "W"}), 0, "W",
        {"6": (0.0, 1.0), "8": (0.0, 3.0)}, "Lib:FP")
    keys = [n.order_key for n in out]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------- #
# P3 -- side sequencing
# --------------------------------------------------------------------------- #
@needs_footprints
def test_side_order_is_descending_by_summed_priority():
    from skidl_layout.geometry import load_footprint_geometries

    partition, emap, dirs, _fab = _partition_and_map()
    geometry = load_footprint_geometries({SOIC8}, dirs)[SOIC8]
    pads = _anchor_pads({"1": "FB", "2": "COMP", "3": "SW", "4": "VCC",
                         "5": "GND", "6": "BOOT", "7": "SENSE", "8": "RT"})
    order = side_order(partition, "U1", emap, anchor_pads=pads,
                       anchor_geometry=geometry)
    assert sorted(order) == sorted(SIDES)
    sums = []
    for side in order:
        found = side_neighbours(partition, "U1", emap, side=side,
                                anchor_pads=pads, anchor_geometry=geometry,
                                flatten_templates=True,
                                distribute_stacked=True)
        sums.append(sum(n.priority for n in found))
    assert sums == sorted(sums, reverse=True), list(zip(order, sums))


@needs_footprints
def test_side_order_ties_on_the_side_letter_and_never_on_arrival():
    """⛔ Standing finding 8: a total key over content. The two empty sides of a
    dual-row anchor tie at zero, and the tie-break must be the letter."""
    from skidl_layout.geometry import load_footprint_geometries

    partition, emap, dirs, _fab = _partition_and_map()
    geometry = load_footprint_geometries({SOIC8}, dirs)[SOIC8]
    mapping = {"1": "FB", "2": "COMP", "3": "SW", "4": "VCC", "5": "GND",
               "6": "BOOT", "7": "SENSE", "8": "RT"}
    forward = side_order(partition, "U1", emap,
                         anchor_pads=_anchor_pads(mapping),
                         anchor_geometry=geometry)
    backward = side_order(partition, "U1", emap,
                          anchor_pads=list(reversed(_anchor_pads(mapping))),
                          anchor_geometry=geometry)
    assert forward == backward


# --------------------------------------------------------------------------- #
# Ring 2
# --------------------------------------------------------------------------- #
def test_ring2_subanchor_prefers_the_most_plane_free_shared_nets():
    #: ⚠ ``VOUT`` is a PLANE net to ``ratnest.is_plane_net`` (the stack trunks
    #: ``VIN``/``VOUT``/``VCC``), so ``MID_A``/``MID_B`` stand in for the real
    #: plane-free nets here -- using ``VOUT`` would have tested nothing.
    nets = {"D1": ["SEPIC_MID", "MID_A"], "L1": ["SEPIC_MID", "SW"],
            "U1": ["SW", "GND"], "R9": ["MID_A", "GND"]}
    assert ring2_subanchor("D1", ["L1", "U1", "R9"], nets)[0] in ("L1", "R9")
    nets["L1"].append("MID_A")
    assert ring2_subanchor("D1", ["L1", "U1", "R9"], nets) == (
        "L1", ["MID_A", "SEPIC_MID"])


def test_ring2_subanchor_ties_on_the_ref():
    nets = {"RFB2": ["FB", "GND"], "RFB1": ["FB", "VOUT"], "U1": ["FB", "GND"]}
    assert ring2_subanchor("RFB2", ["RFB1", "U1"], nets) == ("RFB1", ["FB"])


def test_ring2_subanchor_ignores_plane_nets():
    """⛔ Standing finding 5 for the sixth time: a part whose only link to a
    candidate is ``GND`` is not *beside* it in any useful sense."""
    nets = {"CX": ["GND", "SOMEWHERE"], "U1": ["GND"]}
    assert ring2_subanchor("CX", ["U1"], nets) is None


def test_ring2_subanchor_returns_none_rather_than_guessing():
    assert ring2_subanchor("CX", [], {"CX": ["A"]}) is None
    assert ring2_subanchor("CX", ["CX"], {"CX": ["A"]}) is None


# --------------------------------------------------------------------------- #
# The connecting-pad alignment
# --------------------------------------------------------------------------- #
class _FakeFootprint:
    def __init__(self, pads, rotation=0.0):
        self.pads, self.rotation = pads, rotation


def test_the_connecting_pad_offset_is_measured_per_rotation():
    """⭐ S3 aligned the part ORIGIN; S4 aligns the pad that is actually being
    reached, which is what the eyes-on pass asked for."""
    pads = [_Pad(pad_number="1", net_name="VCC", local_x=-1.0, local_y=0.0),
            _Pad(pad_number="2", net_name="GND", local_x=1.0, local_y=0.0)]
    neighbour = _n("C1", "4", "VCC", 0.0)
    offsets = _connecting_pad_offsets(_FakeFootprint(pads), neighbour, "W")
    #: side W runs along y, so at 0 deg the pad sits on the axis
    assert offsets[0] == 0.0
    #: at 90 deg the same pad has swung onto the y axis
    assert abs(abs(offsets[90]) - 1.0) < 1e-6


def test_the_connecting_pad_is_the_smallest_pad_key_on_the_net():
    """⛔ A total key over content -- never the file's pad order."""
    pads = [_Pad(pad_number="10", net_name="VCC", local_x=0.0, local_y=2.0),
            _Pad(pad_number="2", net_name="VCC", local_x=0.0, local_y=-2.0)]
    offsets = _connecting_pad_offsets(_FakeFootprint(pads),
                                      _n("C1", "4", "VCC", 0.0), "W")
    assert offsets[0] == -2.0


def test_the_alignment_offset_shifts_the_position_by_exactly_that_much():
    class _G:
        footprint = "Lib:FP"

        def _transform_bounds(self, bounds, placed):
            return (-0.5, -0.5, 0.5, 0.5)

        bounds = (-0.5, -0.5, 0.5, 0.5)

    plain = _position_for(_G(), _n("C1", "4", "V", 0.0), "W", 0, 1.0, 2.0,
                          10.0, 10.0, 7.5, None, 0.4, 0, 10.0)
    aligned = _position_for(_G(), _n("C1", "4", "V", 0.0), "W", 0, 1.0, 2.0,
                            10.0, 10.0, 7.5, None, 0.4, 0, 10.0,
                            align_offset=0.75)
    assert round(plain[1] - aligned[1], 9) == 0.75
    assert plain[0] == aligned[0], "alignment must not move the standoff axis"


def test_the_allowance_is_read_off_the_placements_own_terms():
    placement = Placement(
        ref="C1", x_mm=0.0, y_mm=0.0, rot_deg=0, standoff_mm=5.0,
        slide_steps=0, routed=True, route_length_mm=1.0, route_iterations=1,
        standoff_terms=(("anchor_courtyard_extent_mm", 1.0, "m"),
                        ("fanout_allowance_mm", 4.0096, "pushed once"),
                        ("neighbour_courtyard_facing_mm", 0.5, "m")))
    assert _allowance_of(placement) == 4.0096
    assert _allowance_of(_placement("C2", 0.0, 0.0)) == 0.0


# --------------------------------------------------------------------------- #
# The S4 result objects
# --------------------------------------------------------------------------- #
def _tighten_step(ref, recovered=2.0, stopped="floor"):
    return TightenStep(ref=ref, moved_from_mm=(1.0, 2.0),
                       moved_to_mm=(1.0, 4.0), recovered_mm=recovered,
                       steps_accepted=8, steps_tried=9, stopped_by=stopped,
                       side="W", subanchor="U1", step_mm=0.25)


def _cell(**kwargs):
    base = dict(board="b", anchor="U1", sides=(), ring2=(), ring2_failures=(),
                tighten=(), skipped=(), meta={})
    base.update(kwargs)
    return CellResult(**base)


def test_the_cell_placements_are_the_sides_then_ring_two():
    side = _result(side="W", placements=(_placement("A", 0.0, 1.0),))
    cell = _cell(sides=(side,), ring2=(_placement("B", 0.0, 2.0),))
    assert [p.ref for p in cell.placements] == ["A", "B"]


def test_the_cell_routed_fraction_is_zero_rather_than_a_division_by_zero():
    assert _cell().routed_fraction == 0.0


def test_the_cell_routed_fraction_counts_ring_two_as_well():
    side = _result(placements=(_placement("A", 0.0, 1.0),))
    cell = _cell(sides=(side,),
                 ring2=(_placement("B", 0.0, 2.0, routed=False),))
    assert cell.routed_fraction == 0.5


def test_the_cell_legality_reads_BOTH_boxes_over_the_whole_cell():
    assert _cell().legal
    assert not _cell(meta={"physical_overlaps": [["A", "B"]]}).legal
    assert not _cell(meta={"courtyard_overlaps": [["A", "B"]]}).legal


def test_the_cell_serialised_form_is_json_and_order_stable():
    import json

    cell = _cell(sides=(_result(placements=(_placement("A", 1.0, 2.0),)),),
                 ring2=(_placement("B", 3.0, 4.0),),
                 tighten=(_tighten_step("A"),),
                 skipped=({"ref": "Z", "why": ANCHOR_PAD_NOT_ESCAPABLE},))
    first = json.dumps(cell_result_to_dict(cell), sort_keys=True, default=str)
    assert first == json.dumps(cell_result_to_dict(cell), sort_keys=True,
                               default=str)
    assert cell_result_to_dict(cell)["tighten"][0]["stopped_by"] == "floor"


def test_a_tighten_step_records_the_zero_movers_too():
    """⛔ A tighten that reports only its successes is the observes-nothing
    defect. A neighbour that could not move at all still gets a row, with the
    reason it could not."""
    step = _tighten_step("Z", recovered=0.0, stopped="collision")
    blob = step.to_dict()
    assert blob["recovered_mm"] == 0.0 and blob["stopped_by"] == "collision"
    assert blob["moved_from_mm"] == [1.0, 2.0]


def test_construct_cell_refuses_an_anchor_it_has_no_geometry_for():
    with pytest.raises(ConstructError):
        construct_cell(object(), "U1", session=None, geometries={}, fab=None,
                       escape_map=None)


def test_construct_cell_refuses_a_tighten_floor_that_is_not_declared():
    assert "allowance" in TIGHTEN_FLOORS and "courtyard" in TIGHTEN_FLOORS
    assert "nonsense" not in TIGHTEN_FLOORS


@needs_footprints
def test_flattening_changes_the_representative_and_the_control_arm_does_not():
    """⛔ Gate ``FC0``'s control in miniature: with the flag OFF the side list
    is exactly S3's."""
    from skidl_layout.geometry import load_footprint_geometries

    partition, emap, dirs, _fab = _partition_and_map()
    geometry = load_footprint_geometries({SOIC8}, dirs)[SOIC8]
    pads = _anchor_pads({"1": "FB", "2": "COMP", "3": "SW", "4": "VCC",
                         "5": "GND", "6": "BOOT", "7": "SENSE", "8": "RT"})
    kwargs = dict(anchor_pads=pads, anchor_geometry=geometry)
    for side in SIDES:
        off = side_neighbours(partition, "U1", emap, side=side, **kwargs)
        on = side_neighbours(partition, "U1", emap, side=side,
                             flatten_templates=True, distribute_stacked=True,
                             **kwargs)
        templates_off = {n.ref for n in off if n.klass == "template"}
        templates_on = {n.ref for n in on if n.klass == "template"}
        #: this synthetic circuit has no family cell, so both are empty and the
        #: lists must be identical -- which is the control arm's whole claim
        assert templates_off == templates_on == set()
        assert [n.to_dict() for n in off] == [n.to_dict() for n in on]


# =========================================================================== #
# S5 -- the four arrangement policies and the crossing census
# =========================================================================== #
from skidl_layout.construct import (                             # noqa: E402
    CROSSING_KINDS,
    OVERFLOW_ADMISSION,
    OVERFLOW_ORDER,
    OVERFLOW_REASONS,
    OverflowMove,
    busy_anchor_extra_mm,
    pair_crossings,
    side_span,
)
from skidl_layout.construct import (                             # noqa: E402
    _anchor_direction,
    _illegal_pairs,
    _pad_local_xy,
    _reprojected_key,
    _ring2_side_excluding,
    _same_side_crossings,
)


def test_the_S5_declared_tables_are_non_empty_and_have_no_duplicates():
    """⛔ Standing finding 20, applied to S5's own constants before the run --
    and it has now fired on four consecutive plans, so this is not ceremony."""
    for table in (OVERFLOW_REASONS, CROSSING_KINDS, OVERFLOW_ADMISSION,
                  OVERFLOW_ORDER):
        assert table and len(set(table)) == len(table), table


def test_every_declared_overflow_reason_is_assigned_somewhere_in_the_source():
    """⛔ A declared reason nothing emits is the observes-nothing defect wearing
    a filter's clothes. Read against what the CODE emits, never against what the
    plan says it emits."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    for reason in OVERFLOW_REASONS:
        assert 'reason="%s"' % reason in text, reason


def test_both_readings_of_every_S5_choice_are_reachable_in_the_source():
    """⭐ ``TIGHTEN_FLOORS`` and ``_classify_plan_literal`` set the house rule:
    the refuted reading SHIPS so the control stays re-measurable. S5 has two
    more of them and neither may be a dead branch."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    assert 'admission == "footprint_side"' in text
    assert 'policy == "edge_depth"' in text
    assert 'policy != "perimeter"' in text


# --------------------------------------------------------------------------- #
# pair_crossings -- the instrument S4's report needed and did not have
# --------------------------------------------------------------------------- #
def _line(x1, y1, x2, y2, ref="A", side="W", ring=1):
    return ((x1, y1), (x2, y2), ref, side, ring)


def test_the_crossing_census_of_nothing_is_zero_on_every_declared_kind():
    census = pair_crossings([])
    assert census["lines"] == 0
    for kind in CROSSING_KINDS:
        assert census[kind] == 0


def test_two_crossing_lines_on_one_side_are_a_same_side_crossing():
    census = pair_crossings([_line(0, 0, 10, 10, "A", "W"),
                             _line(0, 10, 10, 0, "B", "W")])
    assert census["same_side"] == 1 and census["cross_side"] == 0


def test_two_crossing_lines_on_different_sides_are_a_cross_side_crossing():
    census = pair_crossings([_line(0, 0, 10, 10, "A", "W"),
                             _line(0, 10, 10, 0, "B", "N")])
    assert census["cross_side"] == 1 and census["same_side"] == 0


def test_a_ring_two_line_takes_precedence_over_the_side_labels():
    census = pair_crossings([_line(0, 0, 10, 10, "A", "W"),
                             _line(0, 10, 10, 0, "B", "W", ring=2)])
    assert census["ring2"] == 1 and census["same_side"] == 0


def test_lines_meeting_at_a_shared_endpoint_are_NOT_crossings():
    """⛔ 44 of S4's 65 raw intersections are the stacked binding fanning onto
    ONE anchor pad. Counting those would report a defect that is not there."""
    census = pair_crossings([_line(0, 0, 5, 5, "A", "W"),
                             _line(0, 10, 5, 5, "B", "W")])
    assert census["shared_endpoint"] == 1
    assert census["same_side"] == 0 and census["cross_side"] == 0


def test_a_shared_endpoint_beats_the_ring_two_label_too():
    """⚠ The precedence is stated rather than accidental: *not a crossing* is a
    stronger statement than *which lines it involved*."""
    census = pair_crossings([_line(0, 0, 5, 5, "A", "W"),
                             _line(0, 10, 5, 5, "B", "W", ring=2)])
    assert census["shared_endpoint"] == 1 and census["ring2"] == 0


def test_parallel_lines_never_cross():
    census = pair_crossings([_line(0, 0, 10, 0, "A", "W"),
                             _line(0, 5, 10, 5, "B", "W")])
    assert sum(census[kind] for kind in CROSSING_KINDS) == 0


def test_a_collinear_overlap_is_still_a_crossing():
    """⚠ Rare on real pad centres and cheap to be right about -- and *rare* is
    exactly how the five ``SW1`` crossings stayed invisible for a day."""
    census = pair_crossings([_line(0, 0, 10, 0, "A", "W"),
                             _line(4, 0, 14, 0, "B", "W")])
    assert census["same_side"] == 1


def test_the_crossing_census_is_order_stable_and_reports_its_line_count():
    lines = [_line(0, 0, 10, 10, "B", "W"), _line(0, 10, 10, 0, "A", "W")]
    first = pair_crossings(lines)
    second = pair_crossings(list(reversed(lines)))
    assert first["detail"] == second["detail"]
    assert first["lines"] == 2
    assert first["kinds_declared"] == list(CROSSING_KINDS)


def test_the_same_side_helper_counts_only_the_same_side_kind():
    class _State:
        entries = {}

        def pads(self, ref):
            return []

    assert _same_side_crossings(_State(), []) == 0


# --------------------------------------------------------------------------- #
# side_span -- section 2.3's table, in code
# --------------------------------------------------------------------------- #
class _FakeGeometry:
    """The two boxes, stated separately -- standing finding 13 in a fixture."""

    def __init__(self, half=0.5, footprint="fake:FP"):
        self.half = half
        self.footprint = footprint
        self.bounds = None

    def transformed_bounds(self, placed):
        return (placed.x_mm - self.half, placed.y_mm - self.half,
                placed.x_mm + self.half, placed.y_mm + self.half)

    def transformed_physical_bounds(self, placed):
        return (placed.x_mm - self.half / 2.0, placed.y_mm - self.half / 2.0,
                placed.x_mm + self.half / 2.0, placed.y_mm + self.half / 2.0)

    def _transform_bounds(self, bounds, placed):
        return self.transformed_bounds(placed)


def test_side_span_raises_when_it_can_observe_nothing():
    """⛔ Rule 3, six instances in seven runs."""
    with pytest.raises(ConstructError):
        side_span([], "W", {})


def test_side_span_measures_the_column_along_the_edge_on_the_courtyard():
    geometries = {"A": _FakeGeometry(0.5), "B": _FakeGeometry(0.5)}
    placements = [_placement("A", 10.0, 20.0), _placement("B", 10.0, 24.0)]
    row = side_span(placements, "W", geometries)
    assert row["span_mm"] == pytest.approx(5.0)      # 19.5 .. 24.5
    assert row["mid"] == pytest.approx(22.0)
    assert row["centre"] is None and row["offset_mm"] is None


def test_side_span_offsets_and_ratios_need_the_anchor_and_say_so():
    geometries = {"A": _FakeGeometry(0.5)}
    placements = [_placement("A", 10.0, 30.0)]
    row = side_span(placements, "W", geometries,
                    anchor_box=(24.0, 20.0, 26.0, 24.0))
    assert row["centre"] == pytest.approx(22.0)
    assert row["offset_mm"] == pytest.approx(8.0)
    assert row["anchor_edge_mm"] == pytest.approx(4.0)
    assert row["ratio"] == pytest.approx(0.25)


def test_side_span_reads_the_ALONG_axis_of_the_side_it_is_given():
    """⛔ N/S measure along x, E/W along y -- one table, stated once."""
    geometries = {"A": _FakeGeometry(0.5), "B": _FakeGeometry(0.5)}
    placements = [_placement("A", 10.0, 20.0), _placement("B", 14.0, 20.0)]
    assert side_span(placements, "N", geometries)["span_mm"] == \
        pytest.approx(5.0)
    assert side_span(placements, "W", geometries)["span_mm"] == \
        pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# busy_anchor_extra_mm -- P-B's term, and every number carries its source
# --------------------------------------------------------------------------- #
class _FakeFab:
    name = "fake"
    clearance_mm = 0.25
    min_clearance_mm = 0.1524
    track_width_mm = 0.3
    via_size_mm = 0.6
    via_drill_mm = 0.3


def _neighbours(count, ref="N"):
    return [Neighbour(ref="%s%d" % (ref, i), anchor_pad="1", net="X",
                      side="W", klass="one_pad", priority=1,
                      order_key=(0.0, (0, 1, "1"), "%s%d" % (ref, i)))
            for i in range(count)]


def test_a_side_that_is_not_over_subscribed_gets_no_keep_out_at_all():
    members = _neighbours(1)
    extra, terms = busy_anchor_extra_mm(
        members, fab=_FakeFab(), anchor_geometry=_FakeGeometry(5.0), side="W",
        geometries={m.ref: _FakeGeometry(0.5) for m in members})
    assert extra == 0.0
    assert dict((t[0], t[1]) for t in terms)["lanes"] == 0


def test_the_keep_out_is_one_via_lane_per_whole_multiple_of_oversubscription():
    members = _neighbours(6)
    geometries = {m.ref: _FakeGeometry(1.0) for m in members}
    extra, terms = busy_anchor_extra_mm(
        members, fab=_FakeFab(), anchor_geometry=_FakeGeometry(2.0), side="W",
        geometries=geometries)
    values = dict((t[0], t[1]) for t in terms)
    # 6 x 2.0 of extent + 5 x 0.8 of edge gap = 16.0 over an anchor edge of 4.0
    assert values["side_load_mm"] == pytest.approx(16.0)
    assert values["anchor_edge_mm"] == pytest.approx(4.0)
    assert values["side_load_ratio"] == pytest.approx(4.0)
    assert values["lanes"] == 3
    assert extra == pytest.approx(3 * values["lane_mm"])


def test_every_keep_out_term_carries_its_source_and_none_is_a_table():
    members = _neighbours(4)
    _extra, terms = busy_anchor_extra_mm(
        members, fab=_FakeFab(), anchor_geometry=_FakeGeometry(1.0), side="W",
        geometries={m.ref: _FakeGeometry(0.5) for m in members})
    assert [t[0] for t in terms] == ["side_load_mm", "anchor_edge_mm",
                                     "side_load_ratio", "lanes", "lane_mm",
                                     "busy_anchor_extra_mm"]
    assert all(isinstance(t[2], str) and t[2] for t in terms)
    assert "lane_from_fab" in dict((t[0], t[2]) for t in terms)["lane_mm"]


def test_the_keep_out_moves_with_the_fab_spec_rather_than_with_a_table():
    """⛔ The size-table trap has fired four times; this is a fifth site."""
    class _Wider(_FakeFab):
        via_size_mm = 1.2

    members = _neighbours(6)
    geometries = {m.ref: _FakeGeometry(1.0) for m in members}
    kwargs = dict(anchor_geometry=_FakeGeometry(2.0), side="W",
                  geometries=geometries)
    narrow, _t = busy_anchor_extra_mm(members, fab=_FakeFab(), **kwargs)
    wide, _t = busy_anchor_extra_mm(members, fab=_Wider(), **kwargs)
    assert wide > narrow


def test_the_keep_out_refuses_an_anchor_with_no_extent_to_divide_by():
    with pytest.raises(ConstructError):
        busy_anchor_extra_mm(_neighbours(2), fab=_FakeFab(),
                             anchor_geometry=_FakeGeometry(0.0), side="W",
                             geometries={})


# --------------------------------------------------------------------------- #
# The standoff term -- P-B pushes against P7 and must not be handed back
# --------------------------------------------------------------------------- #
def _position(**kwargs):
    geometry = _FakeGeometry(0.5)
    return _position_for(geometry, _neighbours(1)[0], "W", 0, 2.0, 2.0048,
                         25.0, 25.0, 25.0, None, 0.8, 0, 25.0, **kwargs)


def test_the_OFF_arm_emits_no_busy_term_at_all():
    """⛔ Gate ``AR0``'s control is a PLAIN DIFF against S4's recorded artifact,
    so a term that is always present would make *byte-identical* a claim about a
    stripper rather than about behaviour."""
    _x, _y, _s, _c, terms = _position()
    assert [t[0] for t in terms] == ["anchor_courtyard_extent_mm",
                                     "fanout_allowance_mm",
                                     "neighbour_courtyard_facing_mm",
                                     "standoff_mm"]


def test_the_busy_term_is_a_FIFTH_term_and_not_a_bigger_allowance():
    """⛔⛔ P-B pushes directly against P7, which spends the WHOLE allowance on
    68 of 69 neighbours. The tighten's floor is ``standoff - allowance``, so the
    keep-out survives only while it lives OUTSIDE that band."""
    _x, _y, plain, _c, plain_terms = _position()
    _x, _y, busy, _c, busy_terms = _position(busy_extra=1.5)
    values = dict((t[0], t[1]) for t in busy_terms)
    assert values["busy_anchor_extra_mm"] == 1.5
    assert values["fanout_allowance_mm"] == \
        dict((t[0], t[1]) for t in plain_terms)["fanout_allowance_mm"]
    assert busy == pytest.approx(plain + 1.5)


def test_a_zero_busy_extra_still_declares_itself():
    """⚠ ON with nothing to add is not the same as OFF, and the artifact says
    which -- a policy that is silent when it does nothing is unreadable."""
    _x, _y, _s, _c, terms = _position(busy_extra=0.0)
    assert "busy_anchor_extra_mm" in [t[0] for t in terms]


# --------------------------------------------------------------------------- #
# P-D -- ring 2's memory of the anchor
# --------------------------------------------------------------------------- #
def test_the_anchor_direction_is_the_shipped_pad_occupancy_rule():
    """⛔ The arc has ONE *which side is that on* rule and this is not a second
    one."""
    from skidl_layout.escape_map import pad_occupancy_side

    box = (0.0, 0.0, 10.0, 10.0)
    for point in ((20.0, 5.0), (-20.0, 5.0), (5.0, 20.0), (5.0, -20.0)):
        assert _anchor_direction(box, point[0], point[1]) == \
            pad_occupancy_side(point[0], point[1], box)


class _FakeEscape:
    def __init__(self, entries):
        self._entries = entries

    def entries_for(self, pad):
        return tuple(e for e in self._entries if e.pad == str(pad))


@dataclass
class _Entry:
    pad: str
    side: str
    layer: int
    access: str
    distance_mm: float


def test_ring_two_skips_the_side_the_anchor_lies_on_and_takes_the_next_best():
    emap = _FakeEscape([_Entry("1", "E", 0, "FAVORED", 0.0),
                        _Entry("1", "N", 0, "ACCESSIBLE", 0.4),
                        _Entry("1", "S", 0, "ACCESSIBLE", 0.9),
                        _Entry("1", "W", 0, "BLOCKED", 0.0)])
    side, access, why = _ring2_side_excluding(emap, "1", 0, "E")
    assert side == "N" and access == "ACCESSIBLE" and "excluded" in why


def test_ring_two_prefers_a_favoured_side_over_a_nearer_accessible_one():
    emap = _FakeEscape([_Entry("1", "E", 0, "ACCESSIBLE", 0.1),
                        _Entry("1", "N", 0, "FAVORED", 5.0),
                        _Entry("1", "S", 0, "BLOCKED", 0.0)])
    assert _ring2_side_excluding(emap, "1", 0, "W")[0] == "N"


def test_ring_two_keeps_the_existing_answer_when_every_side_points_at_A():
    emap = _FakeEscape([_Entry("1", "E", 0, "FAVORED", 0.0),
                        _Entry("1", "N", 0, "BLOCKED", 0.0),
                        _Entry("1", "S", 0, "BLOCKED", 0.0),
                        _Entry("1", "W", 0, "BLOCKED", 0.0)])
    side, _access, why = _ring2_side_excluding(emap, "1", 0, "E")
    assert side is None and "kept" in why


def test_ring_two_reads_the_side_through_the_sub_anchors_own_rotation():
    from skidl_layout.escape_map import rotate_escape

    emap = _FakeEscape([_Entry("1", "E", 0, "FAVORED", 0.0),
                        _Entry("1", "N", 0, "ACCESSIBLE", 0.4)])
    forbidden = rotate_escape("E", 90)
    assert _ring2_side_excluding(emap, "1", 90, forbidden)[0] == \
        rotate_escape("N", 90)


# --------------------------------------------------------------------------- #
# P-C -- the re-projection, and the two readings of it
# --------------------------------------------------------------------------- #
def _pad_positions():
    #: a dual-row anchor: pads 1..3 west, 4..6 east
    return {"1": (-2.0, -1.0), "2": (-2.0, 0.0), "3": (-2.0, 1.0),
            "4": (2.0, -1.0), "5": (2.0, 0.0), "6": (2.0, 1.0)}


def test_the_edge_depth_reprojection_puts_the_pad_nearest_the_new_edge_first():
    """⛔ Bail-out 3 found this: ordering the arrivals on the N edge by the pad's
    x is DEGENERATE, because a whole row shares it."""
    keys = [_reprojected_key(_pad_positions(), pad, "N", "U1",
                             _FakeGeometry(3.0), 0, "fake:FP", pad)
            for pad in ("4", "5", "6")]
    assert [key[0] for key in keys] == [2.0, 2.0, 2.0]      # degenerate
    assert [k[3] for k in sorted(keys)] == ["4", "5", "6"]  # y -1, 0, +1


def test_the_perimeter_reprojection_orders_start_side_then_end_side():
    keys = {pad: _reprojected_key(_pad_positions(), pad, "N", "U1",
                                  _FakeGeometry(3.0), 0, "fake:FP", pad,
                                  policy="perimeter")
            for pad in _pad_positions()}
    order = [pad for pad, _k in sorted(keys.items(), key=lambda kv: kv[1])]
    #: the W row walked toward the N edge, then the E row walked away from it
    assert order == ["3", "2", "1", "4", "5", "6"]


def test_the_reprojection_refuses_an_undeclared_ordering_policy():
    with pytest.raises(ConstructError):
        _reprojected_key(_pad_positions(), "1", "N", "U1", _FakeGeometry(3.0),
                         0, "fake:FP", "1", policy="nonsense")


def test_the_reprojection_reads_MEASURED_pad_positions_and_raises_without_them():
    with pytest.raises(ConstructError):
        _pad_local_xy({}, "9", 0, "fake:FP")


# --------------------------------------------------------------------------- #
# The value types and the guards
# --------------------------------------------------------------------------- #
def test_a_neighbour_omits_its_origin_side_until_it_has_actually_moved():
    """⛔ Trap 16: ``Neighbour.side`` stops meaning *the escape side of my anchor
    pad* only when P-C is on, and the OFF arm's serialisation has to stay S4's
    exactly (gate ``AR0`` is a plain diff)."""
    from dataclasses import replace as _replace

    plain = _neighbours(1)[0]
    assert "origin_side" not in plain.to_dict()
    moved = _replace(plain, side="N", origin_side="W")
    assert moved.to_dict()["origin_side"] == "W"


def test_an_overflow_move_records_the_candidates_it_did_NOT_move():
    """⛔ A policy that reports only its successes is standing finding 1 wearing
    a bookkeeping hat."""
    move = OverflowMove(ref="C1", from_side="W", to_side=None,
                        reason="no_free_side", anchor_pad="1", net="VIN")
    blob = move.to_dict()
    assert blob["to_side"] is None and blob["reason"] in OVERFLOW_REASONS
    assert blob["pair_mm_before"] is None and blob["pair_mm_after"] is None


def test_the_illegality_check_reads_BOTH_boxes():
    class _State:
        boxes_phys = {"A": (0.0, 0.0, 1.0, 1.0), "B": (5.0, 0.0, 6.0, 1.0)}
        boxes_court = {"A": (-1.0, -1.0, 2.0, 2.0),
                       "B": (1.5, -1.0, 7.0, 2.0)}

    #: the physical boxes are 4 mm apart and the COURTYARDS overlap
    assert _illegal_pairs(_State(), 0.25) == [["A", "B", "courtyard"]]


def test_construct_cell_refuses_an_undeclared_admission_or_ordering():
    for kwargs in ({"overflow_admission": "nonsense"},
                   {"overflow_order": "nonsense"}):
        with pytest.raises(ConstructError):
            construct_cell(object(), "U1", session=None, geometries={},
                           fab=None, escape_map=None, **kwargs)


def test_the_S5_meta_blocks_are_emitted_only_when_a_policy_is_on():
    """⛔ The OFF arm must serialise exactly what S4 serialised. A new key that
    is always present would make gate ``AR0``'s *byte-identical* a claim about a
    stripper rather than about behaviour."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    guard = text.index("if any((centre_side_lists, busy_anchor_keepout,")
    for key in ('meta["crossings"]', 'meta["side_spans"]', 'meta["overflow"]',
                'meta["centring"]', 'meta["busy_keepout"]',
                'meta["ring2_side_choice"]'):
        assert text.index(key) > guard, key


def test_centring_is_a_translation_and_the_source_says_so():
    """⛔ Trap 17: re-running ``_position_for`` with a shifted ``desired``
    re-enters the cursor logic and can REORDER. P2's order is the property the
    procedure exists to keep."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    start = text.index("def _centre_sides(")
    body = text[start:text.index("def construct_cell(")]
    #: prose ABOUT the cursor is documentation and this arc wants more of it;
    #: what may not appear is a CALL (standing finding 16's shape)
    assert "_position_for(" not in body
    assert 'entry["x_mm"] = base_x + (to_delta if along == 0 else 0.0)' in body


# =========================================================================== #
# S5B -- L2, the board. ⛔ The unit model and the L2 loop.
#
# ⚠ These are UNIT tests over the parts of S5B that need no session: the
# declared sets, the box arithmetic, the corridor rule, the port rank, the
# anchor rule and the stranger geometry. ⛔ The **real** proof that the L2 loop
# places and routes is gates ``BD3``/``BD4``/``BD5`` in
# ``canaries/drive_construct_board.py``, not this file -- a synthetic pad can
# only show that the arithmetic is what it says it is.
# =========================================================================== #
from skidl_layout.construct import (                              # noqa: E402
    CONNECTOR_POLICIES,
    L2_ANCHOR_RULES,
    L2_PORT_SIDES,
    L2_SIDE_ORDERS,
    PORT_SIDE_SOURCES,
    UNIT_SOURCE,
    BoardResult,
    StrangerCrossing,
    Unit,
    UnitPort,
    _clip_segment,
    _corridor_leaves_unit,
    _member_world_pads,
    _unit_pad_number,
    board_result_to_dict,
    l2_anchor,
    port_rank,
    stranger_crossings,
    unit_box,
)


class _Footprint:
    """The two attributes :func:`moved_pads` and the unit model read."""

    def __init__(self, pads, rotation=0.0):
        self.pads = list(pads)
        self.rotation = rotation


def _port(number, net="N1", side="E", access="FAVORED", cost=0.0,
          source="member_escape", member="R1", pad="1"):
    return UnitPort(net=net, side=side, x_mm=0.0, y_mm=0.0, member=member,
                    pad=pad, access=access, cost=cost, source=source,
                    number=number)


def _unit(name, refs, ports=(), kind="ic", source="constructed"):
    return Unit(name=name, kind=kind, refs=tuple(refs), anchor=refs[0],
                offsets={ref: (0.0, 0.0, 0) for ref in refs},
                physical_box=(0.0, 0.0, 1.0, 1.0),
                courtyard_box=(0.0, 0.0, 1.0, 1.0), ports=tuple(ports),
                source=source)


class _Geom:
    """The two boxes and nothing else -- what :func:`unit_box` reads."""

    def __init__(self, footprint, phys, court):
        self.footprint = footprint
        self._phys = phys
        self._court = court
        self.pads = []

    def transformed_bounds(self, placed):
        return tuple(v + (placed.x_mm if i % 2 == 0 else placed.y_mm)
                     for i, v in enumerate(self._court))

    def transformed_physical_bounds(self, placed):
        return tuple(v + (placed.x_mm if i % 2 == 0 else placed.y_mm)
                     for i, v in enumerate(self._phys))


def test_the_S5B_declared_sets_are_stated_once_and_are_not_empty():
    """⛔ Standing finding 20's eleventh opportunity: a declared constant that
    matches nothing is a defect, and a declared set that does not exist cannot
    be checked against what the producer emits."""
    for declared in (UNIT_SOURCE, L2_ANCHOR_RULES, L2_PORT_SIDES,
                     L2_SIDE_ORDERS, CONNECTOR_POLICIES, PORT_SIDE_SOURCES):
        assert declared and len(set(declared)) == len(declared)
    assert UNIT_SOURCE[0] == "constructed"
    assert L2_ANCHOR_RULES[0] == "largest_cell"
    assert L2_PORT_SIDES[0] == "member_escape"
    assert CONNECTOR_POLICIES[0] == "loop_placed"


def test_a_composite_pad_number_is_addressable_and_orders_totally():
    """⛔ Two members of one unit routinely share a pad number, so the composite
    number is what makes the pair target and the failure log unambiguous."""
    assert _unit_pad_number("C1", 2) == "C1.2"
    assert _unit_pad_number("U1", "A1") == "U1.A1"
    numbers = {_unit_pad_number(ref, n) for ref in ("C1", "C2")
               for n in (1, 2)}
    assert len(numbers) == 4


def test_member_pads_are_rewritten_into_the_UNITS_frame():
    """⛔⛔ ``moved_pads`` transforms from ``local_*``. A unit whose pads still
    carried their FOOTPRINT-frame locals would stack every member on the unit
    origin -- silently, and with a perfectly legal-looking box."""
    footprint = _Footprint([_Pad(pad_number="1", local_x=-0.8, local_y=0.0,
                                global_x=-0.8, global_y=0.0, net_name="A")])
    moved = _member_world_pads("C7", footprint, (4.0, 3.0, 0))
    assert len(moved) == 1
    pad = moved[0]
    assert str(pad.pad_number) == "C7.1"
    assert (round(pad.local_x, 6), round(pad.local_y, 6)) == \
        (round(pad.global_x, 6), round(pad.global_y, 6))
    assert round(pad.global_x, 6) == 3.2 and round(pad.global_y, 6) == 3.0


def test_unit_box_unions_BOTH_boxes_and_they_are_different():
    """⛔ Standing finding 13: escape and rotation are ``body u pads``
    questions; standoff and collision are courtyard ones."""
    geometries = {
        "A": _Geom("fpA", (-1.0, -0.5, 1.0, 0.5), (-1.3, -0.8, 1.3, 0.8)),
        "B": _Geom("fpB", (-1.0, -0.5, 1.0, 0.5), (-1.3, -0.8, 1.3, 0.8))}
    physical, courtyard = unit_box({"A": (0.0, 0.0, 0), "B": (5.0, 0.0, 0)},
                                  geometries)
    assert physical == (-1.0, -0.5, 6.0, 0.5)
    assert courtyard == (-1.3, -0.8, 6.3, 0.8)
    assert physical != courtyard


def test_unit_box_raises_rather_than_boxing_nothing():
    """⛔ Rule 8, and standing finding 6: an unresolved footprint silently
    becomes a 2 x 2 mm fallback that is self-consistent."""
    with pytest.raises(ConstructError):
        unit_box({}, {})
    with pytest.raises(ConstructError):
        unit_box({"A": (0.0, 0.0, 0)}, {})


def test_a_one_member_unit_escapes_on_every_side():
    """⚠ 37 of 53 non-anchor units on the corpus are exactly this, so the
    interior re-decision must be unable to fire on them."""
    pad = _Pad(pad_number="1", global_x=0.0, global_y=0.0, size_x=1.0,
               size_y=0.5)
    for side in SIDES:
        assert _corridor_leaves_unit((-1.0, -0.5, 1.0, 0.5), pad, side, [],
                                     (-1.0, -0.5, 1.0, 0.5))


def test_a_member_boxed_in_by_ANOTHER_member_does_not_escape_that_way():
    """⛔⛔ This is the rule that replaced a bounding-box touch test, which put
    9 of 9 units on ONE side. **A bounding box is not an occupancy map.**"""
    pad = _Pad(pad_number="1", global_x=0.0, global_y=0.0, size_x=1.0,
               size_y=0.5)
    member = (-1.0, -0.5, 1.0, 0.5)
    unit = (-1.0, -0.5, 10.0, 0.5)
    blocker = (3.0, -0.5, 4.0, 0.5)
    assert not _corridor_leaves_unit(member, pad, "E", [blocker], unit)
    assert _corridor_leaves_unit(member, pad, "W", [blocker], unit)
    #: ⭐ A blocker off the pad's own along-edge span does not obstruct it.
    assert _corridor_leaves_unit(member, pad, "E", [(3.0, 4.0, 4.0, 5.0)],
                                 unit)


def test_port_rank_prefers_a_perimeter_port_over_one_redecided_inside():
    """⛔⛔ Standing finding 15(a) one level up: *"the lowest-numbered port"*
    picks by SPELLING, and a unit's ports on one net can belong to members
    anywhere in the cell."""
    perimeter = _port("Z9.1", access="ACCESSIBLE", cost=1.0,
                      source="member_escape")
    interior = _port("A1.1", access="FAVORED", cost=0.0,
                     source="member_escape_interior")
    assert sorted([interior, perimeter], key=port_rank)[0] is perimeter
    #: within one source, S1's own ordering: FAVORED, then the corridor cost,
    #: then the pad key
    a = _port("B1.1", access="FAVORED", cost=0.5)
    b = _port("B1.2", access="ACCESSIBLE", cost=0.0)
    assert sorted([b, a], key=port_rank)[0] is a
    c = _port("C1.1", access="FAVORED", cost=0.1)
    assert sorted([a, c], key=port_rank)[0] is c


def test_l2_anchor_is_about_MEMBER_COUNT_and_not_about_kind():
    """⚠ ``lt3758_iso_flyback``'s ``ic:U2`` is an ``ic`` group with ONE member,
    so a rule keyed on kind alone would offer a bare opto-isolator as an
    anchor."""
    big = _unit("ic:U1", ["U1", "C1", "C2"], [_port("U1.1")])
    #: ⚠ ``most_ports`` counts **distinct nets leaving the unit**, not pads.
    small = _unit("ic:U2", ["U2"], [_port("U2.1", net="A"),
                                    _port("U2.2", net="B"),
                                    _port("U2.3", net="C")])
    connector = _unit("connector:J1", ["J1"], [_port("J1.1")],
                      kind="connector", source="footprint")
    units = [big, small, connector]
    assert l2_anchor(units) == "ic:U1"
    assert l2_anchor(units, rule="most_ports") == "ic:U2"
    assert l2_anchor(units, rule="connector") == "connector:J1"


def test_l2_anchor_ties_break_on_the_unit_name_and_it_is_TESTED_not_lucky():
    """⭐ No tie occurs on the corpus (the largest unit is unique on 5 of 5), so
    the tie-break is exercised here rather than by luck."""
    a = _unit("ic:B", ["B", "B2"], [_port("B.1")])
    b = _unit("ic:A", ["A", "A2"], [_port("A.1")])
    assert l2_anchor([a, b]) == "ic:A"


def test_l2_anchor_raises_rather_than_answering_about_nothing():
    """⛔ Rule 8, six instances in seven runs."""
    with pytest.raises(ConstructError):
        l2_anchor([])
    with pytest.raises(ConstructError):
        l2_anchor([_unit("ic:U1", ["U1"], [_port("U1.1")])], rule="connector")
    with pytest.raises(ConstructError):
        l2_anchor([_unit("ic:U1", ["U1"], [_port("U1.1")])], rule="biggest")


def test_a_unit_port_sorts_by_the_composite_key_that_route_pair_uses():
    """⭐ The routed pair and the aligned pair must be the SAME pad -- see
    ``unit_ports``' note. The key is ``_pad_key`` of the composite number."""
    ports = [_port("C10.2"), _port("C1.1"), _port("C1.10")]
    numbers = [p.number for p in sorted(ports, key=lambda p: p.key)]
    assert numbers == ["C1.1", "C1.10", "C10.2"]


def test_clip_segment_is_a_real_clip_and_misses_are_None():
    rect = (0.0, 0.0, 10.0, 10.0)
    assert _clip_segment((((-5.0, 5.0), (15.0, 5.0))), rect) is not None
    t0, t1 = _clip_segment(((-5.0, 5.0), (15.0, 5.0)), rect)
    assert round(t0, 6) == 0.25 and round(t1, 6) == 0.75
    assert _clip_segment(((-5.0, 20.0), (15.0, 20.0)), rect) is None
    #: a segment wholly inside is entirely inside
    assert _clip_segment(((1.0, 1.0), (2.0, 2.0)), rect) == (0.0, 1.0)


def test_a_stranger_is_only_counted_for_a_line_that_is_not_the_channels_own():
    """⛔⛔ **The PAIR LINE, not the copper.** A ``PairResult`` carries no path
    and inventing one is forbidden, so ``overlap_mm`` is how far a straight
    pad-to-pad segment reaches past the outer edge of the allowance band."""

    class _State:
        entries = {}

    lines = [((5.0, 0.0), (5.0, 12.0), "OTHER", "N", 1),
             ((6.0, 0.0), (6.0, 12.0), "MINE", "N", 1)]
    channel = ("HOST", "N", (0.0, 10.0, 10.0, 12.0), 2.0, ("MINE",))
    out = stranger_crossings(_State(), [], channels=[channel],
                             ring2_refs=set(), routed_ok={"MINE"},
                             phase="t", lines=lines)
    assert [s.intruder for s in out] == ["OTHER"]
    assert out[0].channel_of == "HOST" and out[0].side == "N"
    assert out[0].still_routed is True
    #: the line runs from the far edge of the band right through it, so it
    #: reaches the whole allowance
    assert round(out[0].overlap_mm, 4) == 2.0


def test_a_board_result_serialises_every_bucket_it_accounts_for():
    """⛔ The accounting is TOTAL: a unit that simply vanishes is the
    observes-nothing defect wearing a bookkeeping hat."""
    result = BoardResult(board="b", anchor="ic:U1", sides=(), ring2=(),
                         ring2_failures=(), tighten=(), skipped=(),
                         strangers=(StrangerCrossing(
                             channel_of="ic:U1", side="N", intruder="x",
                             net="n", overlap_mm=0.5, still_routed=True),),
                         units=(_unit("ic:U1", ["U1"], [_port("U1.1")]),),
                         meta={"physical_overlaps": [],
                               "courtyard_overlaps": []})
    blob = board_result_to_dict(result)
    for key in ("board", "anchor", "routed_fraction", "legal", "sides",
                "ring2", "ring2_failures", "tighten", "skipped", "strangers",
                "units", "meta"):
        assert key in blob
    assert result.legal is True
    assert result.routed_fraction == 0.0
    assert blob["strangers"][0]["overlap_mm"] == 0.5
    assert blob["units"][0]["name"] == "ic:U1"


def test_construct_is_still_a_leaf_after_S5B():
    """⛔⛔ ``construct.py`` is a LEAF and S5B added no importer. The renderer
    stayed in the driver for exactly this reason (roadmap item 10)."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    importers = []
    for path in sorted(root.glob("*.py")):
        if path.name == "construct.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from\s+\.construct\s+import|import\s+"
                     r"skidl_layout\.construct)", text, re.M):
            importers.append(path.name)
    assert importers == []


def test_the_blame_view_is_built_from_the_STATE_and_takes_the_extra_pads():
    """⛔⛔ Stamping a rolled-back unit into the SESSION is not enough, and the
    difference is silent: the reachability view is built from ``_CellState``."""
    import pathlib

    text = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "construct.py").read_text(encoding="utf-8")
    start = text.index("class _ConstructedBoardView")
    body = text[start:text.index("def _blame(")]
    assert "extra" in body
    assert "state.entries" in body
    #: ⚠ no committed copper: this arc commits none (overview 7.5)
    assert "self.segments = []" in body and "self.vias = []" in body


# =========================================================================== #
# S5C -- THE ARC. ⛔ P-E the escape demand, P-F the ring, P-G L0, P-H the shrink
# =========================================================================== #
from skidl_layout.construct import (                              # noqa: E402
    ARC_EDGE_GAP,
    CORNER_OWNER,
    QUADRANTS,
    RING_STOP_REASONS,
    SIDE_ASSIGNMENT,
    SIDE_DEMAND_REASONS,
    TIGHTEN_MODE,
    arc_gap_mm,
    corner_owners,
    l0_anchor,
    ring_radius,
    ring_slots,
)
from skidl_layout.construct import (                              # noqa: E402
    _QUADRANT_ENDS,
    _edge_extent,
    _facing_extent,
    _position_for,
)


def test_every_s5c_policy_vocabulary_is_declared_and_two_valued():
    """⛔ Standing finding 20: a policy with one reading is not a policy, and a
    declared clause that matches nothing must be visible."""
    assert SIDE_ASSIGNMENT == ("favored", "escapable")
    assert ARC_EDGE_GAP == ("edge_gap", "fanout")
    assert CORNER_OWNER == ("none", "heavier", "split")
    assert TIGHTEN_MODE == ("part", "ring")
    assert set(QUADRANTS) == {"NE", "SE", "SW", "NW"}
    assert len(set(RING_STOP_REASONS)) == len(RING_STOP_REASONS)
    assert len(set(SIDE_DEMAND_REASONS)) == len(SIDE_DEMAND_REASONS)


def test_the_quadrant_table_is_a_derivation_of_the_axes_and_not_a_table():
    """⭐ ``_AXES`` says which axis a side runs along and which end is "lo"; the
    quadrant is named for its two sides. ⛔ A table that drifted from that
    derivation would put a part in the wrong corner **silently**."""
    for side in SIDES:
        _axis, _sign, along = _AXES[side]
        lo_side = next(s for s in SIDES
                       if _AXES[s][0] == along and _AXES[s][1] < 0)
        hi_side = next(s for s in SIDES
                       if _AXES[s][0] == along and _AXES[s][1] > 0)
        want = tuple(("".join(sorted((side, other),
                                     key=lambda t: "NSEW".index(t))), other)
                     for other in (lo_side, hi_side))
        assert _QUADRANT_ENDS[side] == want


def test_the_arc_gap_is_the_fabspec_and_never_a_constant():
    fab = _FakeFab()
    assert arc_gap_mm(fab, "edge_gap") == pytest.approx(edge_gap_mm(fab))
    assert arc_gap_mm(fab, "fanout") == pytest.approx(standoff_base_mm(fab))
    #: ⚠ The two are DIFFERENT numbers, and the difference is plan section 5.2.
    assert arc_gap_mm(fab, "fanout") > arc_gap_mm(fab, "edge_gap")
    with pytest.raises(ConstructError):
        arc_gap_mm(fab, "somewhere_in_between")


# --------------------------------------------------------------------------- #
# P-F -- corner ownership
# --------------------------------------------------------------------------- #
def test_no_quadrant_has_an_owner_under_the_none_policy():
    owners = corner_owners({"N": 1.0, "E": 90.0, "S": 1.0, "W": 40.0}, "none")
    assert set(owners) == set(QUADRANTS)
    assert set(owners.values()) == {None}


def test_the_heavier_adjacent_side_owns_the_quadrant():
    owners = corner_owners({"N": 1.0, "E": 90.0, "S": 1.0, "W": 40.0},
                           "heavier")
    assert owners == {"NE": "E", "SE": "E", "NW": "W", "SW": "W"}


def test_a_corner_tie_breaks_on_the_declared_side_order_not_the_letter():
    """⛔ A total key over content, and the tie-break is **stated**: the index in
    ``SIDES`` (N, E, S, W), never the alphabet."""
    owners = corner_owners({"N": 5.0, "E": 5.0, "S": 5.0, "W": 5.0}, "heavier")
    assert owners["NE"] == "N"      # N is index 0, E is index 1
    assert owners["SE"] == "E"      # E is index 1, S is index 2
    assert owners["SW"] == "S"
    assert owners["NW"] == "N"


def test_an_undeclared_corner_policy_raises():
    with pytest.raises(ConstructError):
        corner_owners({}, "whoever_asks_first")


# --------------------------------------------------------------------------- #
# P-F -- ring_radius
# --------------------------------------------------------------------------- #
def _ring_geoms(members, half=1.0):
    return {m.ref: _FakeGeometry(half) for m in members}


def test_the_ring_radius_is_the_plan_arithmetic_and_every_term_names_itself():
    """⭐ ``capacity(side, R) = face + 2R`` and ``R_fit = (packed - face) / 2``
    -- plan section 2.4, in code, on the ``none`` arm where the reserve is 0."""
    fab = _FakeFab()
    members = _neighbours(4)
    info = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                       geometries=_ring_geoms(members, 1.0),
                       anchor_geometry=_FakeGeometry(2.0), fab=fab,
                       edge_gap="edge_gap", corner_owner="none")
    row = info["sides"]["W"]
    gap = edge_gap_mm(fab)
    assert row["face_mm"] == pytest.approx(4.0)          # 2 x half
    assert row["packed_mm"] == pytest.approx(4 * 2.0 + 3 * gap)
    assert row["R_fit_mm"] == pytest.approx(
        max(standoff_base_mm(fab), (row["packed_mm"] - 4.0) / 2.0))
    assert row["capacity_mm"] == pytest.approx(4.0 + 2 * row["R_spawn_mm"])
    names = [term[0] for term in row["terms"]]
    assert names[:6] == ["face_mm", "arc_edge_gap_mm", "packed_mm",
                         "reserve_mm", "corner_share", "fanout_allowance_mm"]
    assert all(len(term) == 3 and term[2] for term in row["terms"])


def test_a_side_that_fits_inside_its_own_face_gets_the_allowance_and_no_more():
    """⛔ The floor is the fanout allowance, never zero: the ring may shrink the
    radius but it may not delete the term P4 exists to create."""
    fab = _FakeFab()
    members = _neighbours(1)
    info = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                       geometries=_ring_geoms(members, 0.25),
                       anchor_geometry=_FakeGeometry(10.0), fab=fab)
    assert info["sides"]["W"]["R_fit_mm"] == pytest.approx(
        standoff_base_mm(fab))


def test_owning_one_quadrant_instead_of_two_DOUBLES_the_radius_a_side_needs():
    """⭐ The arithmetic corner ownership actually buys, stated as a number."""
    fab = _FakeFab()
    members = _neighbours(6)
    geoms = _ring_geoms(members, 1.0)
    both = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                       geometries=geoms, anchor_geometry=_FakeGeometry(2.0),
                       fab=fab, corner_owner="none")["sides"]["W"]
    #: W loses NW to a heavier N by giving N a bigger packed load.
    heavier = ring_radius({"W": members, "N": _neighbours(9, ref="M"),
                           "E": (), "S": ()},
                          geometries=dict(geoms,
                                          **_ring_geoms(_neighbours(9, ref="M"),
                                                        1.0)),
                          anchor_geometry=_FakeGeometry(2.0), fab=fab,
                          corner_owner="heavier")
    assert heavier["owners"]["NW"] == "N"
    assert heavier["sides"]["W"]["owns"] == ["SW"]
    assert heavier["sides"]["W"]["R_fit_mm"] > both["R_fit_mm"] * 1.9



def test_the_split_policy_partitions_every_quadrant_and_starves_nobody():
    """⛔⛔⛔ **The third reading, and CONTACT is why it exists.** Plan section
    5.4's literal rule (``heavier``) makes the region single-claimant by
    **side**, and once P-E balances the ring there are four loaded runs and only
    four quadrants -- so the two heaviest take all of them and a side that needs
    more than its own face owns **nothing**. ``split`` makes the region
    single-claimant by **area** instead: half a quadrant each, no tie-break,
    nobody starved."""
    from skidl_layout.construct import corner_share

    owners = corner_owners({"N": 4.0, "E": 3.0, "S": 2.0, "W": 1.0}, "split")
    assert set(owners.values()) == {"both"}
    #: ⭐ Each adjacent side gets exactly half of ``R - reserve`` ...
    assert corner_share("split", "NE", "E", owners, 10.0, 0.25) == \
        pytest.approx((10.0 - 0.25) / 2.0)
    assert corner_share("split", "NE", "N", owners, 10.0, 0.25) == \
        pytest.approx((10.0 - 0.25) / 2.0)
    #: ... and the two halves together are the whole quadrant, so two runs can
    #: still never overlap inside it.
    assert (corner_share("split", "NE", "E", owners, 10.0, 0.25)
            + corner_share("split", "NE", "N", owners, 10.0, 0.25)) == \
        pytest.approx(10.0 - 0.25)


def test_no_side_is_ever_starved_under_split_however_the_load_falls():
    fab = _FakeFab()
    loads = {"N": _neighbours(9, ref="A"), "E": _neighbours(8, ref="B"),
             "S": _neighbours(7, ref="C"), "W": _neighbours(6, ref="D")}
    geoms = {}
    for members in loads.values():
        geoms.update(_ring_geoms(members, 1.0))
    starved = ring_radius(loads, geometries=geoms,
                          anchor_geometry=_FakeGeometry(1.0), fab=fab,
                          corner_owner="heavier")
    fair = ring_radius(loads, geometries=geoms,
                       anchor_geometry=_FakeGeometry(1.0), fab=fab,
                       corner_owner="split")
    #: ⛔ MEASURED: N (the heaviest) takes BOTH of its quadrants, E and S take
    #: one each, and the lightest side is left with **none** -- so the side that
    #: starves is decided by the load ordering rather than by its own demand.
    assert starved["sides"]["N"]["owns"] == ["NE", "NW"]
    assert starved["sides"]["W"]["owns"] == []
    assert sorted(starved["infeasible"]) == ["W"]
    #: ... and ``split`` leaves none of them impossible.
    assert fair["infeasible"] == []
    assert all(row["total_share"] == pytest.approx(1.0)
               for row in fair["sides"].values())


def test_the_split_radius_is_TWICE_the_shared_one_and_that_is_its_price():
    """⚠ Half a quadrant each means each side needs twice the radius to hold
    the same run -- ⛔ and whether that beats the corner spill it buys is
    **open question 22, and S6's**."""
    fab = _FakeFab()
    members = _neighbours(6)
    geoms = _ring_geoms(members, 1.0)
    whole = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                        geometries=geoms, anchor_geometry=_FakeGeometry(2.0),
                        fab=fab, corner_owner="none")["sides"]["W"]
    half = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                       geometries=geoms, anchor_geometry=_FakeGeometry(2.0),
                       fab=fab, corner_owner="split")["sides"]["W"]
    assert half["total_share"] == pytest.approx(1.0)
    assert whole["total_share"] == pytest.approx(2.0)
    assert half["R_fit_mm"] == pytest.approx(
        2.0 * (whole["R_fit_mm"] - whole["reserve_mm"])
        + half["reserve_mm"], rel=1e-6)


def test_an_asymmetric_interval_CLAMPS_the_run_and_says_so():
    """⛔⛔ **Plan section 8's assertion 3 met an asymmetric interval, and the
    honest answer is to record the clamp rather than assert it away.** A side
    that owns one quadrant and not the other cannot be centred on the anchor --
    but it is still **bounded**, which is what open question 14's open half is
    actually about."""
    members = _neighbours(4)
    plan = ring_slots(members, side="W", radius_mm=12.0,
                      anchor_box=(20.0, 24.0, 30.0, 26.0),
                      geometries=_ring_geoms(members, 1.0), gap_mm=0.8,
                      shares={"NW": 1.0, "SW": 0.0}, reserve_mm=0.25)
    assert plan["fits"] is True
    assert plan["clamped"] is True and plan["clamped_by_mm"] != 0.0
    #: ⭐ and every slot is still inside the interval, which is the property.
    starts = [plan["slots"][m.ref] for m in members]
    assert min(starts) >= plan["lo"] - 1e-6
    assert max(starts) + 2.0 <= plan["hi"] + 1e-6


def test_a_symmetric_interval_never_clamps():
    members = _neighbours(4)
    plan = ring_slots(members, side="W", radius_mm=12.0,
                      anchor_box=(20.0, 24.0, 30.0, 26.0),
                      geometries=_ring_geoms(members, 1.0), gap_mm=0.8,
                      shares={"NW": 0.5, "SW": 0.5}, reserve_mm=0.25)
    assert plan["fits"] is True and plan["clamped"] is False

def test_a_side_that_owns_no_quadrant_and_overflows_its_face_is_INFEASIBLE():
    """⛔ Named, never silently sized: an impossible side is a finding."""
    fab = _FakeFab()
    big = _neighbours(9, ref="M")
    small = _neighbours(6)
    geoms = dict(_ring_geoms(big, 1.0), **_ring_geoms(small, 1.0))
    info = ring_radius({"N": big, "S": big, "E": small, "W": ()},
                       geometries=geoms, anchor_geometry=_FakeGeometry(0.5),
                       fab=fab, corner_owner="heavier")
    assert info["sides"]["E"]["owns"] == []
    assert info["sides"]["E"]["feasible"] is False
    assert "E" in info["infeasible"]
    assert "owns no corner quadrant" in info["sides"]["E"]["why"]


def test_the_spawn_factor_is_declared_so_recovery_is_attributable():
    fab = _FakeFab()
    members = _neighbours(4)
    geoms = _ring_geoms(members, 1.0)
    tight = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                        geometries=geoms, anchor_geometry=_FakeGeometry(2.0),
                        fab=fab, spawn_factor=1.0)["sides"]["W"]
    loose = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                        geometries=geoms, anchor_geometry=_FakeGeometry(2.0),
                        fab=fab, spawn_factor=1.5)["sides"]["W"]
    assert loose["R_spawn_mm"] == pytest.approx(tight["R_fit_mm"] * 1.5)
    assert loose["R_fit_mm"] == tight["R_fit_mm"]


def test_the_ring_is_sized_in_the_anchors_OWN_frame_when_it_is_told_where_it_is():
    """⛔⛔ **The defect this run found in itself.** The first cut computed the
    anchor box at the origin here and at the anchor's world position in the
    placement loop, so the shrink re-flowed every slot **25 mm** off and
    reverted its first step as ``unroutable`` on 2 of 2 subjects -- which reads
    exactly like the policy failing. *Two derivations of one number.*"""
    fab = _FakeFab()
    members = _neighbours(2)
    info = ring_radius({"W": members, "N": (), "E": (), "S": ()},
                       geometries=_ring_geoms(members, 0.5),
                       anchor_geometry=_FakeGeometry(2.0), fab=fab,
                       anchor_x_mm=25.0, anchor_y_mm=25.0)
    assert info["anchor_box"] == [23.0, 23.0, 27.0, 27.0]
    #: ⭐ and the FACE is unchanged by the move -- an extent is not a position.
    assert info["sides"]["W"]["face_mm"] == pytest.approx(4.0)


# --------------------------------------------------------------------------- #
# P-F -- ring_slots
# --------------------------------------------------------------------------- #
def test_the_slots_are_centred_on_the_anchor_by_CONSTRUCTION():
    """⭐ P-A's property, now arithmetic rather than a transaction that might
    revert."""
    members = _neighbours(3)
    plan = ring_slots(members, side="W", radius_mm=10.0,
                      anchor_box=(20.0, 20.0, 30.0, 30.0),
                      geometries=_ring_geoms(members, 1.0), gap_mm=0.8,
                      owns=("NW", "SW"))
    assert plan["fits"] is True
    starts = [plan["slots"][m.ref] for m in members]
    assert starts == sorted(starts)                 # ⛔ monotone in P2 order
    mid = (starts[0] + starts[-1] + 2.0) / 2.0      # last box is 2.0 wide
    assert mid == pytest.approx(plan["centre_mm"])
    assert plan["centre_mm"] == pytest.approx(25.0)


def test_a_run_that_will_not_fit_is_a_NAMED_FAILURE_and_never_an_advance():
    """⛔⛔ Open question 14's open half, in one assertion: there is no
    ``advance`` left to be unbounded."""
    members = _neighbours(8)
    plan = ring_slots(members, side="W", radius_mm=0.5,
                      anchor_box=(20.0, 20.0, 21.0, 21.0),
                      geometries=_ring_geoms(members, 1.0), gap_mm=0.8,
                      owns=("NW", "SW"), reserve_mm=0.25, r_max_mm=9.0)
    assert plan["fits"] is False
    assert plan["slots"] == {}
    assert "FAILURE" in plan["why"] and "R_max is 9.0" in plan["why"]


def test_a_side_that_owns_no_quadrant_is_bounded_by_its_face_alone():
    members = _neighbours(1)
    plan = ring_slots(members, side="W", radius_mm=50.0,
                      anchor_box=(20.0, 20.0, 30.0, 30.0),
                      geometries=_ring_geoms(members, 1.0), gap_mm=0.8,
                      owns=())
    assert plan["lo"] == 20.0 and plan["hi"] == 30.0
    assert plan["capacity_mm"] == pytest.approx(10.0)


def test_the_quadrant_use_census_names_every_part_that_spills_past_the_corner():
    members = _neighbours(6)
    plan = ring_slots(members, side="W", radius_mm=12.0,
                      anchor_box=(20.0, 24.0, 30.0, 26.0),
                      geometries=_ring_geoms(members, 1.0), gap_mm=0.8,
                      owns=("NW", "SW"))
    assert plan["fits"] is True
    spilled = plan["quadrant_use"]["NW"] + plan["quadrant_use"]["SW"]
    assert spilled, "a 16.8 mm run against a 2 mm face MUST use the corners"
    assert set(spilled) <= {m.ref for m in members}


def test_ring_slots_raises_rather_than_placing_a_part_it_cannot_measure():
    members = _neighbours(2)
    with pytest.raises(ConstructError):
        ring_slots(members, side="W", radius_mm=10.0,
                   anchor_box=(0.0, 0.0, 10.0, 10.0), geometries={},
                   gap_mm=0.8, owns=("NW", "SW"))


def test_an_empty_side_is_a_row_with_a_reason_and_not_a_raise():
    plan = ring_slots([], side="N", radius_mm=2.0,
                      anchor_box=(0.0, 0.0, 4.0, 4.0), geometries={},
                      gap_mm=0.8)
    assert plan["fits"] is True and plan["slots"] == {}
    assert plan["why"] == "no neighbour on this side"


# --------------------------------------------------------------------------- #
# P-F -- the slot reaches _position_for, and the linear arm does NOT change
# --------------------------------------------------------------------------- #
def _slot_position(**over):
    kwargs = dict(geometry=_FakeGeometry(1.0),
                  neighbour=_neighbours(1)[0], side="W", rot=0,
                  anchor_extent=2.0, base=2.0048, anchor_x_mm=25.0,
                  anchor_y_mm=25.0, desired=25.0, cursor=None, gap=0.8,
                  slide=0, centre_along=25.0)
    kwargs.update(over)
    return _position_for(**kwargs)


def test_the_slot_replaces_the_cursor_and_puts_the_courtyard_LOW_EDGE_on_it():
    _x, y, _s, _c, _t = _slot_position(cursor=999.0, slot=40.0)
    #: the fake courtyard is +/-1.0 about the origin, so the origin sits at 41.0
    assert y == pytest.approx(41.0)


def test_without_a_slot_the_cursor_still_binds_exactly_as_S3_S4_S5_had_it():
    _x, y, _s, _c, terms = _slot_position(cursor=40.0)
    assert y == pytest.approx(41.0)
    #: ⛔ and the OFF arm's terms are S4's three, in S4's order.
    assert [t[0] for t in terms] == ["anchor_courtyard_extent_mm",
                                     "fanout_allowance_mm",
                                     "neighbour_courtyard_facing_mm",
                                     "standoff_mm"]


def test_the_ring_arm_keeps_BOTH_the_radius_and_the_allowance_in_its_terms():
    """⛔⛔ ``_allowance_of`` reads ``fanout_allowance_mm`` to build the
    per-part tighten's floor, so a ring arm that renamed it would silently hand
    that pass a floor of ``standoff - R``."""
    _x, _y, standoff, _c, terms = _slot_position(base=17.55, slot=40.0,
                                                 allowance=2.0048)
    names = [t[0] for t in terms]
    assert names == ["anchor_courtyard_extent_mm", "ring_radius_mm",
                     "fanout_allowance_mm", "neighbour_courtyard_facing_mm",
                     "standoff_mm"]
    values = {t[0]: t[1] for t in terms}
    assert values["ring_radius_mm"] == pytest.approx(17.55)
    assert values["fanout_allowance_mm"] == pytest.approx(2.0048)
    assert standoff == pytest.approx(2.0 + 17.55 + 1.0)


def test_the_facing_extent_is_the_third_standoff_term_read_back():
    geometry = _FakeGeometry(1.5)
    _x, _y, standoff, _c, terms = _slot_position(geometry=geometry,
                                                 anchor_extent=3.0, base=5.0)
    facing = _facing_extent(geometry, "N0", "W", 0)
    assert facing == pytest.approx(1.5)
    assert standoff == pytest.approx(3.0 + 5.0 + facing)
    assert dict((t[0], t[1]) for t in terms)[
        "neighbour_courtyard_facing_mm"] == pytest.approx(facing)


def test_the_edge_extent_reads_the_ALONG_axis_and_the_COURTYARD():
    geometry = _FakeGeometry(1.25)
    assert _edge_extent(geometry, "A", "W", 0) == pytest.approx(2.5)
    assert _edge_extent(geometry, "A", "N", 0) == pytest.approx(2.5)


# --------------------------------------------------------------------------- #
# P-G -- the L0 anchor
# --------------------------------------------------------------------------- #
class _L0Group:
    def __init__(self, refs, name="family:divider:R1-R2"):
        self.refs = tuple(refs)
        self.name = name
        self.kind = "family"
        self.family = "divider"
        self.topology = "junction"
        self.bindings = ()


class _L0Footprint:
    def __init__(self, pads):
        self.pads = tuple(pads)


def _l0_pad(number, net):
    return _Pad(pad_number=str(number), net_name=net, local_x=0.0, local_y=0.0,
                size_x=1.0, size_y=1.0)


def test_the_l0_anchor_is_the_member_with_the_most_pads_on_an_INTERNAL_net():
    group = _L0Group(["R1", "R2"])
    footprints = {"R1": _L0Footprint([_l0_pad(1, "MID"), _l0_pad(2, "MID")]),
                  "R2": _L0Footprint([_l0_pad(1, "MID"), _l0_pad(2, "GND")])}
    geometries = {"R1": _FakeGeometry(0.5), "R2": _FakeGeometry(0.5)}
    nets = {"R1": ["MID"], "R2": ["MID", "GND"]}
    ref, why = l0_anchor(group, footprints=footprints, geometries=geometries,
                         nets_by_ref=nets)
    assert ref == "R1"
    assert "internal" in why and "MID" in why


def test_the_l0_anchor_ties_break_on_the_larger_COURTYARD_then_the_ref():
    group = _L0Group(["R1", "R2"])
    footprints = {"R1": _L0Footprint([_l0_pad(1, "MID")]),
                  "R2": _L0Footprint([_l0_pad(1, "MID")])}
    geometries = {"R1": _FakeGeometry(0.5), "R2": _FakeGeometry(2.0)}
    nets = {"R1": ["MID"], "R2": ["MID"]}
    assert l0_anchor(group, footprints=footprints, geometries=geometries,
                     nets_by_ref=nets)[0] == "R2"
    #: ⛔ and with the areas equal it is the REF, a total key over content.
    same = {"R1": _FakeGeometry(1.0), "R2": _FakeGeometry(1.0)}
    assert l0_anchor(group, footprints=footprints, geometries=same,
                     nets_by_ref=nets)[0] == "R1"


def test_an_l0_group_with_no_member_raises_rather_than_guessing():
    with pytest.raises(ConstructError):
        l0_anchor(_L0Group([]), footprints={}, geometries={}, nets_by_ref={})


def test_a_net_only_one_member_carries_is_NOT_internal():
    """⛔ *"The family's shared net"* is derived, never declared: a net one
    member holds alone is the family's PORT, not its junction."""
    group = _L0Group(["R1", "R2"])
    footprints = {"R1": _L0Footprint([_l0_pad(1, "OUT"), _l0_pad(2, "OUT")]),
                  "R2": _L0Footprint([_l0_pad(1, "MID")])}
    geometries = {"R1": _FakeGeometry(0.5), "R2": _FakeGeometry(0.5)}
    nets = {"R1": ["OUT"], "R2": ["MID"]}
    ref, why = l0_anchor(group, footprints=footprints, geometries=geometries,
                         nets_by_ref=nets)
    #: neither member scores, so the tie-break decides -- and it is stated.
    assert ref in {"R1", "R2"} and "internal net(s) []" in why
