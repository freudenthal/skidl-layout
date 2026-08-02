# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.cells_compile` and :mod:`skidl_layout.cells_place`.

The compiler half is pure geometry. The placement half exercises the footprint
masquerade against a real ``plan_layout`` call, which needs footprint geometry on
disk and is skipped when KiCad's libraries are not installed.
"""

from __future__ import annotations

import os

import pytest

from skidl_layout.cells import (
    CellMember,
    CellPad,
    CellPort,
    LayoutCell,
    rotate_cell,
)
from skidl_layout.cells_compile import (
    CellAcceptance,
    VIA_COST_MM,
    compile_cell,
    derive_access,
    escape_corridor_clear,
    grade_cell,
    naive_area_mm2,
)
from skidl_layout.cells_place import (
    CELL_PREFIX,
    CellInstance,
    cell_fp_geometries,
    expand_cell_placements,
    substitute_cells,
)
from skidl_layout.writer import PlacedPart


def _divider_cell(name="fb_divider") -> LayoutCell:
    return LayoutCell(
        name=name,
        width=6.85,
        height=1.40,
        members=(
            CellMember("R1", "part", "Resistor_SMD:R_0805_2012Metric",
                       5.425, 0.7, 180, w=2.05, h=1.4),
            CellMember("R2", "part", "Resistor_SMD:R_0805_2012Metric",
                       1.425, 0.7, 180, w=2.05, h=1.4),
        ),
        pads=(
            CellPad("R1", "1", "VOUT", 6.3375, 0.7, 1.025, 1.4),
            CellPad("R1", "2", "VFB", 4.5125, 0.7, 1.025, 1.4),
            CellPad("R2", "1", "VFB", 2.3375, 0.7, 1.025, 1.4),
            CellPad("R2", "2", "GND", 0.5125, 0.7, 1.025, 1.4),
        ),
        nets={"VOUT": ("R1.1",), "VFB": ("R1.2", "R2.1"), "GND": ("R2.2",)},
        internal_nets=frozenset({"VFB"}),
        stackup=2,
    ).normalised()


# --------------------------------------------------------------------------- #
# the geometric access probe
# --------------------------------------------------------------------------- #
def test_a_pad_on_the_edge_escapes_at_zero_cost():
    cell = _divider_cell()
    pad = next(p for p in cell.pads if p.local_net == "VOUT")
    clear, distance = escape_corridor_clear(
        cell, pad, "E", 0, lane_mm=0.9048, clearance_mm=0.25,
        same_net_labels=frozenset({pad.label}))
    assert clear and distance == pytest.approx(0.0, abs=1e-6)


def test_a_pad_behind_another_net_is_blocked():
    """VOUT's pad sits east of every other pad, so going WEST crosses them."""
    cell = _divider_cell()
    pad = next(p for p in cell.pads if p.local_net == "VOUT")
    clear, _distance = escape_corridor_clear(
        cell, pad, "W", 0, lane_mm=0.9048, clearance_mm=0.25,
        same_net_labels=frozenset({pad.label}))
    assert not clear


def test_same_net_copper_does_not_block_its_own_escape():
    """Two pads of ONE net in line: the inner one still escapes past the outer.

    ⚠ Deliberately not the divider fixture -- there the westward corridor also
    crosses the GND pad, so a pass would have proved nothing about same-net
    handling. This isolates the one variable.
    """
    cell = LayoutCell(
        name="pair", width=6.0, height=2.0,
        members=(CellMember("C1", "part", "X:Y", 3.0, 1.0, 0, w=6.0, h=2.0),),
        pads=(CellPad("C1", "1", "N1", 1.0, 1.0, 1.0, 1.4),
              CellPad("C1", "2", "N1", 4.0, 1.0, 1.0, 1.4)),
        nets={"N1": ("C1.1", "C1.2")}, internal_nets=frozenset(),
    ).normalised()
    labels = frozenset(p.label for p in cell.pads)
    east = max(cell.pads, key=lambda p: p.x)
    clear, _d = escape_corridor_clear(cell, east, "W", 0, lane_mm=0.9048,
                                      clearance_mm=0.25, same_net_labels=labels)
    assert clear


def test_every_triple_gets_an_entry_so_absent_means_never_asked():
    cell = _divider_cell()
    ports = derive_access(cell, layers=(0, 1), lane_mm=0.9048,
                          clearance_mm=0.25)
    triples = {(p.local_net, p.side, p.layer) for p in ports}
    assert len(triples) == len(cell.escaping_nets) * 4 * 2


def test_a_surface_pad_cannot_reach_an_inner_layer_without_a_via():
    cell = _divider_cell()
    ports = derive_access(cell, layers=(0, 1), lane_mm=0.9048,
                          clearance_mm=0.25)
    inner = [p for p in ports if p.layer == 1]
    assert inner and all(p.access == "BLOCKED" for p in inner)


def test_a_through_board_pad_can_reach_an_inner_layer_and_pays_the_via_cost():
    cell = _divider_cell()
    thru = LayoutCell(**{
        **cell.__dict__,
        "pads": tuple(
            CellPad(p.local_ref, p.pad, p.local_net, p.x, p.y, p.w, p.h,
                    through_board=True) for p in cell.pads)}).normalised()
    ports = derive_access(thru, layers=(0, 1), lane_mm=0.9048,
                          clearance_mm=0.25)
    inner_open = [p for p in ports if p.layer == 1 and p.access != "BLOCKED"]
    assert inner_open
    assert all(p.cost >= VIA_COST_MM for p in inner_open)


def test_favored_is_a_subset_of_accessible_and_never_empty():
    cell = _divider_cell()
    ports = derive_access(cell, layers=(0,), lane_mm=0.9048, clearance_mm=0.25)
    for net in cell.escaping_nets:
        mine = [p for p in ports if p.local_net == net]
        if any(p.access != "BLOCKED" for p in mine):
            assert any(p.access == "FAVORED" for p in mine)


def test_derive_access_is_deterministic():
    cell = _divider_cell()
    kwargs = {"layers": (0, 1), "lane_mm": 0.9048, "clearance_mm": 0.25}
    assert derive_access(cell, **kwargs) == derive_access(cell, **kwargs)


# --------------------------------------------------------------------------- #
# compile + acceptance
# --------------------------------------------------------------------------- #
def test_compile_fills_both_maps_and_the_hpwl_points():
    cell, acceptance = compile_cell(_divider_cell(), stackup=2,
                                    clearance_mm=0.25, lane_mm=0.9048,
                                    min_track_mm=0.1524)
    assert cell.ports and cell.transit
    assert set(cell.hpwl_points) == set(cell.escaping_nets)
    assert isinstance(acceptance, CellAcceptance)
    assert acceptance.every_net_has_a_port
    assert acceptance.rotation_invariant


def test_compile_is_reproducible_digest_and_all():
    kwargs = {"stackup": 2, "clearance_mm": 0.25, "lane_mm": 0.9048,
              "min_track_mm": 0.1524}
    a, _ = compile_cell(_divider_cell(), **kwargs)
    b, _ = compile_cell(_divider_cell(), **kwargs)
    assert a.digest == b.digest


def test_compiled_cell_still_round_trips_through_four_rotations():
    from skidl_layout.cells import serialise_cell

    cell, _ = compile_cell(_divider_cell(), stackup=2, clearance_mm=0.25,
                           lane_mm=0.9048, min_track_mm=0.1524)
    turned = cell
    for _ in range(4):
        turned = rotate_cell(turned, 90)
    assert serialise_cell(turned) == serialise_cell(cell)


def test_naive_area_uses_member_envelopes_not_pads():
    """⛔ The units error that reported every harvested cell as oversized."""
    cell = _divider_cell()
    # 2.05 + 2.05 wide, 1.4 tall, no clearance.
    assert naive_area_mm2(cell, 0.0) == pytest.approx(4.1 * 1.4)
    assert naive_area_mm2(cell, 0.25) == pytest.approx(4.35 * 1.4)


def test_area_over_naive_is_reported_but_does_not_fail_acceptance():
    """⛔⛔ A hand arrangement buys escape room WITH area; that is not a defect."""
    cell, _ = compile_cell(_divider_cell(), stackup=2, clearance_mm=0.25,
                           lane_mm=0.9048, min_track_mm=0.1524)
    acceptance = grade_cell(cell, layers=(0, 1), clearance_mm=0.25)
    assert not acceptance.box_not_larger_than_naive   # 9.59 vs 6.09 mm^2
    assert acceptance.area_ratio > 1.0
    assert acceptance.ok                              # and it still accepts


def test_a_net_with_no_escape_is_named_not_swallowed():
    """A pad boxed in on all four sides must be REPORTED, not silently dropped."""
    cell = LayoutCell(
        name="boxed", width=6.0, height=6.0,
        members=(CellMember("U1", "part", "X:Y", 3.0, 3.0, 0, w=6.0, h=6.0),),
        pads=(
            CellPad("U1", "1", "TRAPPED", 3.0, 3.0, 0.5, 0.5),
            CellPad("U1", "2", "WALL", 3.0, 0.5, 5.5, 0.5),
            CellPad("U1", "3", "WALL", 3.0, 5.5, 5.5, 0.5),
            CellPad("U1", "4", "WALL", 0.5, 3.0, 0.5, 5.5),
            CellPad("U1", "5", "WALL", 5.5, 3.0, 0.5, 5.5),
        ),
        nets={"TRAPPED": ("U1.1",), "WALL": ("U1.2", "U1.3", "U1.4", "U1.5")},
        internal_nets=frozenset(),
    ).normalised()
    _compiled, acceptance = compile_cell(cell, stackup=2, clearance_mm=0.25,
                                         lane_mm=0.9048, min_track_mm=0.1524)
    assert "TRAPPED" in acceptance.unreachable_nets
    assert not acceptance.every_net_has_a_port
    assert not acceptance.ok


# --------------------------------------------------------------------------- #
# the masquerade
# --------------------------------------------------------------------------- #
def _instance(cell=None, index=1):
    cell = cell or compile_cell(_divider_cell(), stackup=2, clearance_mm=0.25,
                                lane_mm=0.9048, min_track_mm=0.1524)[0]
    return CellInstance(cell=cell, ref_map={"R1": "RFB1", "R2": "RFB2"},
                        index=index)


def test_the_synthetic_key_is_marked_and_unambiguous():
    inst = _instance()
    assert inst.footprint_key.startswith(CELL_PREFIX)
    assert ":" in inst.footprint_key and "#" in inst.footprint_key


def test_fp_geometries_are_keyed_by_the_synthetic_key():
    inst = _instance()
    geometries = cell_fp_geometries([inst])
    assert set(geometries) == {inst.footprint_key}
    assert geometries[inst.footprint_key].width_mm == pytest.approx(6.85)


def test_expansion_restores_the_members_and_their_frozen_offsets():
    inst = _instance()
    placed = [PlacedPart(inst.ref, 50.0, 40.0, 0.0, inst.footprint_key)]
    expanded = expand_cell_placements(placed, [inst])
    assert [p.ref for p in expanded] == ["RFB1", "RFB2"]
    gap = abs(expanded[0].x_mm - expanded[1].x_mm)
    assert gap == pytest.approx(4.0)          # the harvested spacing, frozen


def test_expansion_is_rigid_under_rotation():
    inst = _instance()
    at0 = expand_cell_placements(
        [PlacedPart(inst.ref, 50.0, 40.0, 0.0, inst.footprint_key)], [inst])
    at90 = expand_cell_placements(
        [PlacedPart(inst.ref, 50.0, 40.0, 90.0, inst.footprint_key)], [inst])

    def _span(parts):
        return (abs(parts[0].x_mm - parts[1].x_mm),
                abs(parts[0].y_mm - parts[1].y_mm))

    # The members move, but the distance between them is invariant: a cell is a
    # rigid body, not a soft constraint.
    assert _span(at0) != _span(at90)
    assert sum(_span(at0)) == pytest.approx(sum(_span(at90)))


def test_expansion_leaves_ordinary_parts_untouched():
    inst = _instance()
    ordinary = PlacedPart("C9", 10.0, 10.0, 90.0, "Lib:Cap")
    expanded = expand_cell_placements(
        [ordinary, PlacedPart(inst.ref, 50.0, 40.0, 0.0, inst.footprint_key)],
        [inst])
    assert ordinary in expanded


# --------------------------------------------------------------------------- #
# the substituted circuit view
# --------------------------------------------------------------------------- #
class _Pin:
    def __init__(self, num, part):
        self.num, self.part, self.net, self.name, self.func = num, part, None, num, None


class _Part:
    def __init__(self, ref, footprint, pins):
        self.ref, self.footprint, self.value = ref, footprint, ref
        self.name, self.description, self.hierarchy = ref, "", ""
        self.foot = footprint
        self.pins = [_Pin(n, self) for n in pins]

    def __len__(self):
        return len(self.pins)


class _Net:
    def __init__(self, name):
        self.name, self._pins = name, []

    def get_pins(self):
        return self._pins


class _Circuit:
    def __init__(self, parts, nets):
        self.parts, self._nets = parts, nets

    @property
    def nets(self):
        # ``hierarchy.extract_groups`` reads this, not ``get_nets()``.
        return self._nets

    def get_nets(self):
        return self._nets


def _tiny_circuit():
    r1 = _Part("RFB1", "Resistor_SMD:R_0805_2012Metric", ["1", "2"])
    r2 = _Part("RFB2", "Resistor_SMD:R_0805_2012Metric", ["1", "2"])
    u1 = _Part("U1", "Package_SO:SOIC-8", ["1", "2", "3"])
    nets = {n: _Net(n) for n in ("VOUT", "VFB", "GND")}

    def wire(pin, net):
        pin.net = nets[net]
        nets[net]._pins.append(pin)

    wire(r1.pins[0], "VOUT")
    wire(r1.pins[1], "VFB")
    wire(r2.pins[0], "VFB")
    wire(r2.pins[1], "GND")
    wire(u1.pins[0], "VOUT")
    wire(u1.pins[1], "VFB")
    wire(u1.pins[2], "GND")
    return _Circuit([r1, r2, u1], list(nets.values()))


def test_substitution_absorbs_the_members_into_one_pseudo_part():
    inst = _instance()
    view = substitute_cells(_tiny_circuit(), [inst])
    refs = {p.ref for p in view.parts}
    assert refs == {"U1", inst.ref}


def test_the_pseudo_part_exposes_only_escaping_nets():
    inst = _instance()
    view = substitute_cells(_tiny_circuit(), [inst])
    pseudo = next(p for p in view.parts if p.ref == inst.ref)
    exposed = {pin.net.name for pin in pseudo.pins}
    # ⛔ VFB is internal to the cell here only if U1 does not touch it -- it
    # does, so all three escape. What must never appear is a net with no pin
    # outside the cell.
    assert exposed <= set(inst.cell.escaping_nets)


def test_absorbed_member_pins_leave_their_nets():
    """⛔ Or every ref-list traversal carries a ref with no position."""
    inst = _instance()
    view = substitute_cells(_tiny_circuit(), [inst])
    for net in view.get_nets():
        for pin in net.get_pins():
            assert pin.part.ref not in ("RFB1", "RFB2")


def test_a_part_claimed_by_two_cells_raises():
    a = _instance(index=1)
    b = _instance(index=2)
    with pytest.raises(ValueError):
        substitute_cells(_tiny_circuit(), [a, b])


def test_the_view_exposes_nets_for_extract_groups():
    """⛔ MEASURED: ``plan_layout`` -> ``hierarchy.extract_groups`` reads
    ``circuit.nets``, which a bare ``SnapshotCircuit`` does not have."""
    view = substitute_cells(_tiny_circuit(), [_instance()])
    assert list(view.nets) == list(view.get_nets())


def test_substitution_is_idempotent_over_the_snapshot():
    view = substitute_cells(_tiny_circuit(), [_instance()])
    again = substitute_cells(view, [])
    assert {p.ref for p in again.parts} == {p.ref for p in view.parts}


# --------------------------------------------------------------------------- #
# end to end -- needs footprint geometry on disk
# --------------------------------------------------------------------------- #
def _fp_dirs():
    from skidl_layout.engine import FP_LIB_DIRS_AUTO, resolve_fp_lib_dirs

    return resolve_fp_lib_dirs(FP_LIB_DIRS_AUTO)


@pytest.mark.skipif(not _fp_dirs(), reason="no KiCad footprint libraries")
def test_plan_layout_places_a_cell_as_one_rigid_body():
    import skidl_layout as SL
    from skidl_layout.constraints import BoardOutline

    inst = _instance()
    view = substitute_cells(_tiny_circuit(), [inst])
    outline = BoardOutline(vertices=[(0, 0), (40, 0), (40, 30), (0, 30)])
    result = SL.plan_layout(view, fp_lib_dirs=_fp_dirs(), outline=outline,
                            extra_fp_geometries=cell_fp_geometries([inst]),
                            parallel_workers=1)
    assert result.validation.ok
    expanded = expand_cell_placements(result.placed_parts, [inst])
    assert {p.ref for p in expanded} == {"RFB1", "RFB2", "U1"}
    members = sorted((p for p in expanded if p.ref.startswith("RFB")),
                     key=lambda p: p.ref)
    separation = ((members[0].x_mm - members[1].x_mm) ** 2
                  + (members[0].y_mm - members[1].y_mm) ** 2) ** 0.5
    assert separation == pytest.approx(4.0, abs=1e-3)


@pytest.mark.skipif(not _fp_dirs(), reason="no KiCad footprint libraries")
def test_cells_off_is_a_true_no_op_on_extra_fp_geometries():
    """⛔ The byte-identity contract, at the one line the engine gained."""
    import skidl_layout as SL
    from skidl_layout.constraints import BoardOutline

    outline = BoardOutline(vertices=[(0, 0), (40, 0), (40, 30), (0, 30)])
    kwargs = {"fp_lib_dirs": _fp_dirs(), "outline": outline,
              "parallel_workers": 1}
    a = SL.plan_layout(_tiny_circuit(), **kwargs)
    b = SL.plan_layout(_tiny_circuit(), extra_fp_geometries=None, **kwargs)
    c = SL.plan_layout(_tiny_circuit(), extra_fp_geometries={}, **kwargs)

    def _key(result):
        return [(p.ref, round(p.x_mm, 6), round(p.y_mm, 6), round(p.rot_deg, 3))
                for p in sorted(result.placed_parts, key=lambda q: q.ref)]

    assert _key(a) == _key(b) == _key(c)
