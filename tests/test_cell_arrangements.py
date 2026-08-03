# -*- coding: utf-8 -*-
"""Tests for the ARRANGEMENT OBJECTIVE's library half (cell-arrangement plan, WS-A2).

⛔ Everything here is offline: no router, no KRT, no board on disk. The routed
half -- the hard gates, the sweep, the random control and the gauge -- lives in
``canaries/drive_cell_arrangements.py``, which is where a router belongs.

⛔⛔ These functions are a **parallel entry point**. Nothing here may move an
existing digest, and ``test_existing_shape_enumeration_unmoved`` is the guard
that says so in this file rather than only in a driver.
"""

from __future__ import annotations

import pytest

from skidl_layout.cells import (
    CellMember,
    CellPad,
    LayoutCell,
    rotate_cell,
    synthesise_cell,
)
from skidl_layout.cells_families import (
    FAMILIES,
    JUNCTION_GAP_EXTRA_MM,
    FamilySpec,
    arrangement_signature,
    arrangements,
    cell_pad_hpwl,
    chain_order,
    classify_three_part_topology,
    courtyard_overhangs,
    enumerate_chain_arrangements,
    enumerate_junction_arrangements,
    footprint_for,
    junction_gap_mm,
    junction_net,
    member_courtyard_gap,
    member_relation,
    missing_courtyards,
    pad_offsets,
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

#: The junction the whole plan is calibrated on -- a divider with a
#: feed-forward cap, where ``MID`` is touched by all three members once each.
JUNCTION_NETS = {
    "IN": [("C1", "1"), ("R1", "1")],
    "MID": [("C1", "2"), ("R1", "2"), ("R2", "1")],
    "OUT": [("R2", "2")],
}

#: ⭐ The control: the SAME three parts in series. The correct arrangement
#: flips to the line, and an objective that returns the same geometry for both
#: has not read the topology.
CHAIN_NETS = {
    "IN": [("C1", "1")],
    "N1": [("C1", "2"), ("R1", "1")],
    "N2": [("R1", "2"), ("R2", "1")],
    "OUT": [("R2", "2")],
}

_PARTS = (("C1", "C"), ("R1", "R"), ("R2", "R"))


def _junction_spec() -> FamilySpec:
    return FamilySpec(name="divider_cap_j", parts=_PARTS, nets=JUNCTION_NETS)


def _chain_spec() -> FamilySpec:
    return FamilySpec(name="chain_rc", parts=_PARTS, nets=CHAIN_NETS)


def _pad(ref, number, net, x, y, w=1.0, h=1.0) -> CellPad:
    return CellPad(local_ref=ref, pad=number, local_net=net, x=x, y=y, w=w, h=h)


def _member(ref, dx, dy, w, h, rotation=0) -> CellMember:
    return CellMember(local_ref=ref, kind="part", footprint="X:Y", dx=dx, dy=dy,
                      rotation=rotation, w=w, h=h)


# --------------------------------------------------------------------------- #
# cell_pad_hpwl
# --------------------------------------------------------------------------- #
def test_cell_pad_hpwl_is_the_hand_computed_half_perimeter():
    """Two nets, both hand-computable: 3 + 0 for ``A``, 0 + 4 for ``B``."""
    cell = LayoutCell(name="t", width=10.0, height=10.0, pads=(
        _pad("R1", "1", "A", 1.0, 1.0), _pad("R2", "1", "A", 4.0, 1.0),
        _pad("R1", "2", "B", 8.0, 1.0), _pad("R2", "2", "B", 8.0, 5.0)))
    assert cell_pad_hpwl(cell) == pytest.approx(3.0 + 4.0)


def test_cell_pad_hpwl_uses_the_bounding_box_over_three_pads():
    """A junction's three pads contribute one bounding box, not three edges."""
    cell = LayoutCell(name="t", width=10.0, height=10.0, pads=(
        _pad("R1", "1", "MID", 0.0, 0.0), _pad("R2", "1", "MID", 2.0, 0.0),
        _pad("C1", "2", "MID", 2.0, 3.0)))
    assert cell_pad_hpwl(cell) == pytest.approx(5.0)


def test_cell_pad_hpwl_ignores_single_pad_and_netless_pads():
    """⛔ A one-pad net owns no in-cell wiring; a net-less pad owns none either."""
    cell = LayoutCell(name="t", width=10.0, height=10.0, pads=(
        _pad("R1", "1", "OUT", 0.0, 0.0),
        _pad("R2", "1", "", 9.0, 9.0),
        _pad("R1", "2", "A", 0.0, 0.0), _pad("R2", "2", "A", 1.5, 0.0)))
    assert cell_pad_hpwl(cell) == pytest.approx(1.5)


@needs_footprints
def test_cell_pad_hpwl_reproduces_the_recorded_line_arrangement():
    """⭐ The calibration anchor: the 0805 ``long`` arrangement of the plan's
    section 1.3, whose recorded pad HPWL is 9.73 mm (9.725 unrounded)."""
    cell = synthesise_cell(
        "long",
        [("C1", "Capacitor_SMD:C_0805_2012Metric", 0.0, 0.0, 0),
         ("R1", "Resistor_SMD:R_0805_2012Metric", 3.85, 0.0, 0),
         ("R2", "Resistor_SMD:R_0805_2012Metric", 7.70, 0.0, 0)],
        JUNCTION_NETS, fp_lib_dirs=_fp_dirs())
    assert cell_pad_hpwl(cell) == pytest.approx(9.725, abs=1e-6)


# --------------------------------------------------------------------------- #
# classify_three_part_topology
# --------------------------------------------------------------------------- #
def test_classify_reads_the_junction():
    assert classify_three_part_topology(JUNCTION_NETS) == "junction"


def test_classify_reads_the_chain():
    assert classify_three_part_topology(CHAIN_NETS) == "chain"


def test_classify_rejects_a_two_part_topology():
    assert classify_three_part_topology(
        {"IN": [("R1", "1")], "MID": [("R1", "2"), ("R2", "1")],
         "OUT": [("R2", "2")]}) == "other"


def test_classify_rejects_a_net_that_touches_a_member_twice():
    """⛔ A member shorted across the net is not a junction leg."""
    nets = {"MID": [("C1", "1"), ("C1", "2"), ("R1", "1")],
            "OUT": [("R1", "2"), ("R2", "1")], "X": [("R2", "2")]}
    assert classify_three_part_topology(nets) == "other"


def test_classify_rejects_a_ring():
    """Three 2-pad links that close on themselves are not a path."""
    nets = {"A": [("C1", "1"), ("R1", "1")], "B": [("R1", "2"), ("R2", "1")],
            "C": [("R2", "2"), ("C1", "2")]}
    assert classify_three_part_topology(nets) == "other"


# --------------------------------------------------------------------------- #
# member_relation / arrangement_signature
# --------------------------------------------------------------------------- #
def test_member_relation_is_symmetric_and_names_the_four_shapes():
    long_a = _member("A", 0.0, 0.0, 2.9, 1.45)
    long_b = _member("B", 3.2, 0.0, 2.9, 1.45)       # end to end
    side_b = _member("B", 0.0, 1.75, 2.9, 1.45)      # long sides shared
    tee_b = _member("B", 2.45, 0.0, 1.45, 2.9)       # turned across
    away_b = _member("B", 3.85, 2.45, 2.9, 1.45)     # diagonal
    assert member_relation(long_a, long_b) == "INLINE"
    assert member_relation(long_b, long_a) == "INLINE"
    assert member_relation(long_a, side_b) == "SIDE"
    assert member_relation(long_a, tee_b) == "T"
    assert member_relation(long_a, away_b) == "OFFSET"


@needs_footprints
def test_arrangement_signature_separates_the_four_named_shapes():
    """⭐ ``bus3``/``ell_a``/``ell_b`` and the forbidden line are all distinct,
    and the shipped shapes fold onto them: ``short`` -> ``bus3``'s signature,
    ``stack`` -> ``ell_b``'s, ``long`` -> the LINE's."""
    spec = _junction_spec()
    kept, _dropped = enumerate_junction_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    seen: dict[str, set] = {}
    for arr in kept:
        cell = synthesise_cell(arr.label, arr.members, spec.nets,
                               fp_lib_dirs=_fp_dirs())
        seen.setdefault(arr.shape, set()).add(arrangement_signature(cell))
    assert seen["bus3"] == {("SIDE", "SIDE", "SIDE")}
    assert seen["ell_a"] == {("SIDE", "T", "T")}
    assert seen["ell_b"] == {("INLINE", "OFFSET", "SIDE")}
    assert seen["long"] == {("INLINE", "INLINE", "INLINE")}
    assert seen["short"] == seen["bus3"]
    assert seen["stack"] == seen["ell_b"]


@needs_footprints
def test_arrangement_signature_is_rotation_invariant():
    """⛔ A signature that moved with the frame could not gauge anything --
    a cell is rotatable in {0, 90, 180, 270} at placement time."""
    spec = _junction_spec()
    kept, _dropped = enumerate_junction_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    for arr in kept:
        cell = synthesise_cell(arr.label, arr.members, spec.nets,
                               fp_lib_dirs=_fp_dirs())
        base = arrangement_signature(cell)
        for deg in (90, 180, 270):
            assert arrangement_signature(rotate_cell(cell, deg)) == base, arr.label


# --------------------------------------------------------------------------- #
# the enumerations
# --------------------------------------------------------------------------- #
@needs_footprints
def test_junction_enumeration_is_deterministic():
    spec = _junction_spec()
    first, drop_a = enumerate_junction_arrangements(
        spec, "0603", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    second, drop_b = enumerate_junction_arrangements(
        spec, "0603", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    assert drop_a == drop_b
    assert [(a.label, a.shape, a.members) for a in first] == \
           [(b.label, b.shape, b.members) for b in second]


@needs_footprints
def test_junction_enumeration_counts_and_reports_what_it_dropped():
    """15 authored + 5 ``long`` + 5 ``short`` + 5 of ``stack``'s 25, and the
    20 off-diagonal ``stack`` rows are COUNTED, never silently discarded."""
    kept, dropped = enumerate_junction_arrangements(
        _junction_spec(), "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    assert len(kept) == 30
    assert dropped == 20
    by_shape = {}
    for arr in kept:
        by_shape[arr.shape] = by_shape.get(arr.shape, 0) + 1
    assert by_shape == {"bus3": 3, "ell_a": 6, "ell_b": 6,
                        "long": 5, "short": 5, "stack": 5}


@needs_footprints
def test_junction_enumeration_puts_the_authored_shapes_first():
    """⛔ So a lowered cap removes FOILS, never the answers."""
    kept, _dropped = enumerate_junction_arrangements(
        _junction_spec(), "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs(),
        max_arrangements=15)
    assert {a.shape for a in kept} == {"bus3", "ell_a", "ell_b"}


@needs_footprints
def test_junction_enumeration_is_empty_for_a_two_part_family():
    """⛔ Data, not an error: ``divider`` has two members and no junction."""
    spec = next(f for f in FAMILIES if f.name == "divider")
    assert enumerate_junction_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs()) == ([], 0)


@needs_footprints
@pytest.mark.parametrize("size", ["0402", "0603", "0805"])
def test_bus3_rotation_comes_from_the_netlist_at_every_size(size):
    """⭐⭐ Trap 7: which pad carries the junction net is READ, never cycled.
    Every ``bus3`` member's ``MID`` pad must end up on the east side of the
    part, at every footprint size."""
    spec = _junction_spec()
    kept, _dropped = enumerate_junction_arrangements(
        spec, size, gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    bus = [a for a in kept if a.shape == "bus3"]
    assert bus
    for arr in bus:
        cell = synthesise_cell(arr.label, arr.members, spec.nets,
                               fp_lib_dirs=_fp_dirs())
        for member in cell.part_members:
            pads = [p for p in cell.pads if p.local_ref == member.local_ref]
            mid = next(p for p in pads if p.local_net == "MID")
            other = next(p for p in pads if p.local_net != "MID")
            assert mid.x > other.x, f"{arr.label}/{member.local_ref}"


@needs_footprints
def test_junction_gap_tracks_the_clearance_when_the_clearance_binds():
    """⛔ Not a hardcoded number. At a clearance high enough to dominate the
    courtyard floor, the authored gap follows the clearance."""
    kept, _dropped = enumerate_junction_arrangements(
        _junction_spec(), "0805", gap_mm=1.00, fp_lib_dirs=_fp_dirs())
    want = f"gap{1.00 + JUNCTION_GAP_EXTRA_MM:.2f}"
    assert all(want in a.label for a in kept
               if a.shape in ("bus3", "ell_a", "ell_b"))


# --------------------------------------------------------------------------- #
# ⛔⛔⛔ THE COURTYARD — the box `members_legal` never looked at
# --------------------------------------------------------------------------- #
@needs_footprints
@pytest.mark.parametrize("size,want", [("0402", 0.350), ("0603", 0.560),
                                       ("0805", 0.560)])
def test_junction_gap_is_raised_by_the_courtyard_floor(size, want):
    """⛔⛔ The regression guard for the defect that shipped seven unbuildable
    cells: at the design clearance the COURTYARD floor binds, not the
    clearance, and the authored gap must follow it."""
    gap = junction_gap_mm(_junction_spec(), size, clearance_mm=0.25,
                          fp_lib_dirs=_fp_dirs())
    assert gap == pytest.approx(want, abs=1e-6)
    assert gap > 0.25 + JUNCTION_GAP_EXTRA_MM


@needs_footprints
@pytest.mark.parametrize("size", ["0402", "0603", "0805"])
def test_courtyard_overhang_is_measured_not_tabled(size):
    """Every member reports a real, positive overhang read off the library."""
    overhangs = courtyard_overhangs(_junction_spec(), size, _fp_dirs())
    assert set(overhangs) == {"C1", "R1", "R2"}
    for ref, (ox, oy) in overhangs.items():
        assert ox > 0.0 and oy > 0.0, ref
    assert not missing_courtyards(_junction_spec(), size, _fp_dirs())


@needs_footprints
@pytest.mark.parametrize("size", ["0402", "0603", "0805"])
def test_every_authored_arrangement_clears_its_courtyards(size):
    """⛔⛔⛔ THE REGRESSION TEST FOR THE REPORTED DEFECT. 7 of 7 delivered
    cells overlapped courtyards by 0.100-0.310 mm while passing
    ``members_legal`` at exactly the design clearance, because that criterion
    measures **body u pads** and a courtyard is a different, larger box."""
    spec = _junction_spec()
    kept, _dropped = enumerate_junction_arrangements(
        spec, size, gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    authored = [a for a in kept if a.shape in ("bus3", "ell_a", "ell_b")]
    assert authored
    for arr in authored:
        cell = synthesise_cell(arr.label, arr.members, spec.nets,
                               fp_lib_dirs=_fp_dirs())
        assert member_courtyard_gap(cell, _fp_dirs()) >= -1e-9, arr.label


@needs_footprints
def test_every_authored_chain_arrangement_clears_its_courtyards():
    spec = _chain_spec()
    kept, _dropped = enumerate_chain_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    for arr in (a for a in kept if a.shape in ("line", "bus3", "ell_a", "ell_b")):
        cell = synthesise_cell(arr.label, arr.members, spec.nets,
                               fp_lib_dirs=_fp_dirs())
        assert member_courtyard_gap(cell, _fp_dirs()) >= -1e-9, arr.label


@needs_footprints
def test_courtyard_gap_is_smaller_than_the_physical_gap():
    """⭐ The whole point, stated as a property: the courtyard box is strictly
    larger, so its gap is strictly smaller. A test that confused the two would
    pass on the defect."""
    from skidl_layout.cells_compile import min_member_gap

    spec = _junction_spec()
    kept, _dropped = enumerate_junction_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    for arr in kept:
        cell = synthesise_cell(arr.label, arr.members, spec.nets,
                               fp_lib_dirs=_fp_dirs())
        assert member_courtyard_gap(cell, _fp_dirs()) < min_member_gap(cell)


def test_member_courtyard_gap_is_infinite_below_two_members():
    cell = LayoutCell(name="t", width=1.0, height=1.0,
                      members=(_member("R1", 0.0, 0.0, 1.0, 1.0),))
    assert member_courtyard_gap(cell, []) == float("inf")


@needs_footprints
def test_chain_enumeration_carries_the_line_and_the_non_line_foils():
    """⭐ The line is authored explicitly, and ``stack`` is absent by
    construction -- it needs a junction net and a chain has none."""
    kept, _dropped = enumerate_chain_arrangements(
        _chain_spec(), "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    shapes = {a.shape for a in kept}
    assert "line" in shapes
    assert {"bus3", "ell_a", "ell_b"} <= shapes
    assert "stack" not in shapes


@needs_footprints
def test_chain_line_follows_the_derived_chain_order():
    """The authored line places the members in ``chain_order``'s order along +x."""
    spec = _chain_spec()
    kept, _dropped = enumerate_chain_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    line = next(a for a in kept if a.shape == "line")
    order = [ref for ref, _fp, _x, _y, _rot in
             sorted(line.members, key=lambda m: m[2])]
    assert tuple(order) == chain_order(spec)


@needs_footprints
def test_chain_line_puts_every_shared_pad_facing_its_neighbour():
    """⭐ What "a line is right for a CHAIN" means geometrically: each hop is
    between adjacent facing pads, so no connection travels past a part."""
    spec = _chain_spec()
    kept, _dropped = enumerate_chain_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    line = next(a for a in kept if a.shape == "line")
    cell = synthesise_cell("line", line.members, spec.nets,
                           fp_lib_dirs=_fp_dirs())
    for net in ("N1", "N2"):
        pads = [p for p in cell.pads if p.local_net == net]
        assert len(pads) == 2
        # the two ends of the hop must be nearer to each other than either is
        # to its own part's other pad
        span = abs(pads[0].x - pads[1].x)
        for pad in pads:
            mate = next(p for p in cell.pads
                        if p.local_ref == pad.local_ref and p.pad != pad.pad)
            assert span < abs(pad.x - mate.x)


@needs_footprints
def test_enumerated_arrangements_synthesise_to_stable_digests():
    """⛔ Two enumerations must not merely agree on coordinates -- the cells
    they synthesise must be byte-identical, since a digest anchors a cache."""
    spec = _junction_spec()
    kept, _dropped = enumerate_junction_arrangements(
        spec, "0805", gap_mm=0.25, fp_lib_dirs=_fp_dirs())
    digests = [synthesise_cell(a.label, a.members, spec.nets,
                               fp_lib_dirs=_fp_dirs()).digest for a in kept]
    again = [synthesise_cell(a.label, a.members, spec.nets,
                             fp_lib_dirs=_fp_dirs()).digest for a in kept]
    assert digests == again
    assert len(set(digests)) == len(digests), "two candidates are the same cell"


# --------------------------------------------------------------------------- #
# ⛔ the guard: the parallel entry point moved nothing
# --------------------------------------------------------------------------- #
@needs_footprints
def test_existing_shape_enumeration_unmoved():
    """⛔⛔ ``arrangements`` is what the 54 ``family_cache`` digests were built
    from. The new enumeration is a PARALLEL entry point; if this drifts, every
    recorded family number drifts with it."""
    spec = next(f for f in FAMILIES if f.name == "divider_cap")
    assert junction_net(spec) == "MID"
    kept, dropped = arrangements(spec, "0805", gap_mm=0.25,
                                 fp_lib_dirs=_fp_dirs(), shape="stack",
                                 max_arrangements=32)
    assert (len(kept), dropped) == (25, 0)
    assert kept[0].label == "stack-gap0.25-row0.25"
    assert kept[-1].label == "stack-gap1.25-row1.25"
    offsets = pad_offsets(footprint_for("R", "0805"), _fp_dirs())
    assert sorted(offsets) == ["1", "2"]
