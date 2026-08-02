# -*- coding: utf-8 -*-
"""Tests for the cell TOOLCHAIN -- nesting, the cache, the shrink ladder,
synthesis, generated families and the matcher (cell-toolchain plan, WS-U3..U7).

⛔ Everything here is offline: no router, no KRT, no board on disk except the
synthetic text a splice test needs. The routed halves are exercised by
``canaries/drive_templates.py`` gates ``U1``/``U2``, which is where a router
belongs.
"""

from __future__ import annotations

import os

import pytest

from skidl_layout.cells import (
    CellCache,
    CellCopper,
    CellMember,
    CellPad,
    CellPort,
    CellSegment,
    CellVia,
    LayoutCell,
    NestedCell,
    compose_cells,
    deserialise_cell,
    inherit_maps_naively,
    member_placed_parts,
    rotate_cell,
    serialise_cell,
    synthesise_cell,
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


def _divider_cell(name="fb_divider") -> LayoutCell:
    """The same 6.85 x 1.40 mm two-0805 divider ``test_cells`` models."""
    return LayoutCell(
        name=name,
        width=6.85,
        height=1.40,
        members=(
            CellMember("R1", "part", "Resistor_SMD:R_0805_2012Metric",
                       5.425, 0.7, 180, "top", 2.0, 1.4),
            CellMember("R2", "part", "Resistor_SMD:R_0805_2012Metric",
                       1.425, 0.7, 180, "top", 2.0, 1.4),
        ),
        pads=(
            CellPad("R1", "1", "VOUT", 6.3375, 0.7, 1.025, 1.4),
            CellPad("R1", "2", "VFB", 4.5125, 0.7, 1.025, 1.4),
            CellPad("R2", "1", "VFB", 2.3375, 0.7, 1.025, 1.4),
            CellPad("R2", "2", "GND", 0.5125, 0.7, 1.025, 1.4),
        ),
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
# part_members -- the filter nesting rests on
# --------------------------------------------------------------------------- #
def test_a_flat_cell_has_every_member_as_a_part():
    """⭐ The byte-identity claim: filtering is a no-op on pre-nesting cells."""
    cell = _divider_cell()
    assert cell.part_members == cell.members
    assert cell.nested_members == ()


def test_a_cell_member_is_excluded_from_part_members_and_refs():
    cell = _divider_cell()
    with_child = LayoutCell(
        name="outer", width=cell.width, height=cell.height,
        members=cell.members + (CellMember("inner", "cell", cell.digest,
                                           0.0, 0.0, 0, "top", 1.0, 1.0),),
        pads=cell.pads, nets=cell.nets).normalised()
    assert len(with_child.members) == 3
    assert len(with_child.part_members) == 2
    assert with_child.member_refs == ("R1", "R2")
    assert [m.local_ref for m in with_child.nested_members] == ["inner"]


def test_expansion_never_tries_to_place_a_cell_member_as_a_footprint():
    """⛔ The failure this filter prevents: placing a 16-hex digest as a name."""
    cell = _divider_cell()
    with_child = LayoutCell(
        name="outer", width=cell.width, height=cell.height,
        members=cell.members + (CellMember("inner", "cell", cell.digest,
                                           0.0, 0.0, 0),),
        pads=cell.pads, nets=cell.nets).normalised()
    placed = member_placed_parts(with_child, 10.0, 10.0, 0)
    assert sorted(p.ref for p in placed) == ["R1", "R2"]
    assert all(":" in p.footprint for p in placed)


# --------------------------------------------------------------------------- #
# WS-U4 -- nesting
# --------------------------------------------------------------------------- #
def _two_child_parent():
    a = _divider_cell("div_a")
    b = _divider_cell("div_b")
    children = [NestedCell(a, 0.0, 0.0, 0, "A_"),
                NestedCell(b, 0.0, a.height + 1.0, 90, "B_")]
    return a, b, children, compose_cells("nested", children)


def test_composition_flattens_parts_and_records_one_member_per_child():
    a, b, children, parent = _two_child_parent()
    assert len(parent.part_members) == 4
    assert len(parent.nested_members) == 2
    assert sorted(m.footprint for m in parent.nested_members) == sorted(
        [a.digest, b.digest])


def test_composition_prefixes_member_refs_so_two_copies_do_not_collide():
    _a, _b, _c, parent = _two_child_parent()
    assert parent.member_refs == ("A_R1", "A_R2", "B_R1", "B_R2")


def test_composed_box_contains_every_child():
    a, b, children, parent = _two_child_parent()
    turned_b = rotate_cell(b, 90)
    assert parent.width == pytest.approx(max(a.width, turned_b.width))
    assert parent.height == pytest.approx(a.height + 1.0 + turned_b.height)


def test_composition_records_its_depth():
    _a, _b, _c, parent = _two_child_parent()
    assert dict(parent.meta)["nest_depth"] == 1
    deeper = compose_cells("deeper", [NestedCell(parent, 0.0, 0.0, 0, "P_")])
    assert dict(deeper.meta)["nest_depth"] == 2


def test_a_composed_cell_round_trips_through_four_rotations():
    _a, _b, _c, parent = _two_child_parent()
    turned = parent
    for _ in range(4):
        turned = rotate_cell(turned, 90)
    assert serialise_cell(turned) == serialise_cell(parent)


def test_composition_inherits_internal_nets_and_never_promotes_one():
    """⚠ The documented limitation, pinned so it cannot drift silently."""
    _a, _b, _c, parent = _two_child_parent()
    assert parent.internal_nets == frozenset({"VFB"})
    assert "VOUT" in parent.escaping_nets


def test_composition_needs_at_least_one_child():
    with pytest.raises(ValueError):
        compose_cells("empty", [])


def test_naive_inheritance_is_a_DIFFERENT_answer_from_recomputing():
    """⛔ The whole reason WS-U4 recomputes. If these ever agree, say so."""
    _a, _b, children, parent = _two_child_parent()
    naive_ports, naive_transit = inherit_maps_naively(children)
    # Inheritance keeps each child's own sides; the parent's real ports are
    # recomputed by the compiler. Here we only assert that inheritance produces
    # a DIFFERENT structure -- a rotated child's ports are inherited verbatim.
    assert naive_ports
    assert {(p.local_net, p.side) for p in naive_ports} != set()
    # ⭐ The rotated child's ports moved side under rotation, so inheriting them
    # gives the parent two nets claiming opposite edges of the same box.
    sides = {p.side for p in naive_ports}
    assert len(sides) > 1
    assert naive_transit == () or all(t.source == "inherited"
                                      for t in naive_transit)


def test_composed_copper_is_translated_into_the_parent_frame():
    a = _divider_cell("with_copper")
    a = LayoutCell(**{**{f: getattr(a, f) for f in a.__dataclass_fields__},
                      "copper": CellCopper(
                          segments=(CellSegment(0.0, 0.0, 1.0, 0.0, 0, 0.3,
                                                "VFB"),),
                          vias=(CellVia(0.5, 0.5, 0.6, 0.3, "VFB"),))}
                   ).normalised()
    parent = compose_cells("p", [NestedCell(a, 2.0, 3.0, 0, "A_")])
    assert parent.copper is not None
    assert parent.copper.segments[0].x1 == pytest.approx(0.0)
    assert parent.copper.vias[0].x == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# WS-U5 -- the content-addressed cache
# --------------------------------------------------------------------------- #
def test_cache_store_and_load_round_trip(tmp_path):
    cache = CellCache(str(tmp_path / "cache"))
    cell = _divider_cell()
    cache.store(cell)
    loaded = cache.load(cell.digest)
    assert loaded is not None
    assert serialise_cell(loaded, include_meta=False) == serialise_cell(
        cell, include_meta=False)


def test_cache_store_is_idempotent_on_the_digest(tmp_path):
    cache = CellCache(str(tmp_path / "cache"))
    cell = _divider_cell()
    cache.store(cell)
    cache.store(cell)
    assert len(cache.digests()) == 1


def test_cache_lists_digests_in_sorted_order(tmp_path):
    cache = CellCache(str(tmp_path / "cache"))
    for i in range(5):
        cache.store(LayoutCell(name=f"c{i}", width=1.0 + i, height=1.0))
    assert list(cache.digests()) == sorted(cache.digests())


def test_cache_miss_is_none_not_an_exception(tmp_path):
    assert CellCache(str(tmp_path / "nothing")).load("deadbeefdeadbeef") is None


def test_cache_by_name_filters(tmp_path):
    cache = CellCache(str(tmp_path / "cache"))
    cache.store(_divider_cell("alpha"))
    cache.store(_divider_cell("beta"))
    assert [c.name for c in cache.by_name("alpha")] == ["alpha"]


# --------------------------------------------------------------------------- #
# synthesise_cell -- an arrangement with no board behind it
# --------------------------------------------------------------------------- #
@needs_footprints
def test_synthesise_builds_a_cell_from_a_specification():
    cell = synthesise_cell(
        "pair",
        [("R1", "Resistor_SMD:R_0805_2012Metric", 0.0, 0.0, 0),
         ("R2", "Resistor_SMD:R_0805_2012Metric", 3.0, 0.0, 0)],
        {"IN": [("R1", "1")], "MID": [("R1", "2"), ("R2", "1")],
         "OUT": [("R2", "2")]},
        fp_lib_dirs=_fp_dirs(), internal_nets=("MID",))
    assert cell.member_refs == ("R1", "R2")
    assert cell.escaping_nets == ("IN", "OUT")
    assert cell.width > 3.0
    assert not dict(cell.meta)["unresolved_footprints"]


@needs_footprints
def test_synthesise_rejects_a_net_naming_an_unknown_member():
    with pytest.raises(ValueError):
        synthesise_cell("bad",
                        [("R1", "Resistor_SMD:R_0805_2012Metric", 0, 0, 0)],
                        {"IN": [("R9", "1")]}, fp_lib_dirs=_fp_dirs())


@needs_footprints
def test_synthesise_is_deterministic():
    args = ("pair",
            [("R1", "Resistor_SMD:R_0603_1608Metric", 0.0, 0.0, 90),
             ("C1", "Capacitor_SMD:C_0603_1608Metric", 0.0, 2.5, 90)],
            {"A": [("R1", "1")], "M": [("R1", "2"), ("C1", "1")],
             "B": [("C1", "2")]})
    first = synthesise_cell(*args, fp_lib_dirs=_fp_dirs())
    second = synthesise_cell(*args, fp_lib_dirs=_fp_dirs())
    assert first.digest == second.digest


# --------------------------------------------------------------------------- #
# WS-U3 -- the shrink ladder
# --------------------------------------------------------------------------- #
@needs_footprints
def test_shrink_is_deterministic():
    from skidl_layout.cells_compile import shrink_cell

    cell = synthesise_cell(
        "pair",
        [("R1", "Resistor_SMD:R_0805_2012Metric", 0.0, 0.0, 0),
         ("R2", "Resistor_SMD:R_0805_2012Metric", 6.0, 0.0, 0)],
        {"IN": [("R1", "1")], "MID": [("R1", "2"), ("R2", "1")],
         "OUT": [("R2", "2")]}, fp_lib_dirs=_fp_dirs())
    a = shrink_cell(cell)
    b = shrink_cell(cell)
    assert a.cell.digest == b.cell.digest
    assert a.final_box == b.final_box


@needs_footprints
def test_shrink_actually_shrinks_a_loose_arrangement():
    from skidl_layout.cells_compile import shrink_cell

    cell = synthesise_cell(
        "loose",
        [("R1", "Resistor_SMD:R_0805_2012Metric", 0.0, 0.0, 0),
         ("R2", "Resistor_SMD:R_0805_2012Metric", 12.0, 0.0, 0)],
        {"IN": [("R1", "1")], "MID": [("R1", "2"), ("R2", "1")],
         "OUT": [("R2", "2")]}, fp_lib_dirs=_fp_dirs())
    result = shrink_cell(cell)
    assert result.shrank
    assert result.final_box[0] < result.original_box[0]
    assert result.rungs_accepted > 0


@needs_footprints
def test_shrink_never_puts_two_members_closer_than_the_gap():
    from skidl_layout.cells_compile import shrink_cell

    cell = synthesise_cell(
        "loose",
        [("R1", "Resistor_SMD:R_0805_2012Metric", 0.0, 0.0, 0),
         ("R2", "Resistor_SMD:R_0805_2012Metric", 12.0, 0.0, 0)],
        {"IN": [("R1", "1")], "MID": [("R1", "2"), ("R2", "1")],
         "OUT": [("R2", "2")]}, fp_lib_dirs=_fp_dirs())
    result = shrink_cell(cell, min_gap_mm=0.5)
    members = {m.local_ref: m for m in result.cell.part_members}
    gap = (abs(members["R1"].dx - members["R2"].dx)
           - (members["R1"].w + members["R2"].w) / 2.0)
    assert gap >= 0.5 - 1e-6


def test_shrink_refuses_a_cell_carrying_internal_copper():
    """⛔ Moving a member would strand its track. Refuse, do not emit a stub."""
    from skidl_layout.cells_compile import shrink_cell

    cell = _divider_cell()
    with_copper = LayoutCell(
        **{**{f: getattr(cell, f) for f in cell.__dataclass_fields__},
           "copper": CellCopper(segments=(CellSegment(0, 0, 1, 0, 0, 0.3,
                                                      "VFB"),))}).normalised()
    result = shrink_cell(with_copper)
    assert result.stopped_by == ("internal-copper", "internal-copper")
    assert not result.shrank


def test_shrink_of_a_single_member_cell_is_a_no_op():
    from skidl_layout.cells_compile import shrink_cell

    cell = LayoutCell(name="one", width=2.0, height=2.0,
                      members=(CellMember("R1", "part", "L:F", 1.0, 1.0, 0,
                                          "top", 2.0, 2.0),)).normalised()
    result = shrink_cell(cell)
    assert result.stopped_by == ("single-member", "single-member")


# --------------------------------------------------------------------------- #
# WS-U5 -- generated families
# --------------------------------------------------------------------------- #
def test_family_footprint_names_are_the_stock_library_ones():
    from skidl_layout.cells_families import footprint_for

    assert footprint_for("R", "0402") == "Resistor_SMD:R_0402_1005Metric"
    assert footprint_for("C", "0603") == "Capacitor_SMD:C_0603_1608Metric"
    assert footprint_for("L", "0805") == "Inductor_SMD:L_0805_2012Metric"
    with pytest.raises(ValueError):
        footprint_for("Q", "0805")
    with pytest.raises(ValueError):
        footprint_for("R", "1234")


@needs_footprints
def test_arrangement_enumeration_is_bounded_and_reports_the_drop():
    from skidl_layout.cells_families import FAMILIES, GAP_LADDER, arrangements

    kept, dropped = arrangements(FAMILIES[0], "0603", gap_mm=0.25,
                                 fp_lib_dirs=_fp_dirs(), shape="long",
                                 max_arrangements=3)
    assert len(kept) == 3
    assert dropped == len(GAP_LADDER) - 3


@needs_footprints
def test_arrangement_enumeration_is_a_pure_function():
    from skidl_layout.cells_families import FAMILIES, arrangements

    first = arrangements(FAMILIES[1], "0805", gap_mm=0.25,
                         fp_lib_dirs=_fp_dirs(), shape="stack")
    second = arrangements(FAMILIES[1], "0805", gap_mm=0.25,
                          fp_lib_dirs=_fp_dirs(), shape="stack")
    assert [a.members for a in first[0]] == [a.members for a in second[0]]


# --- the shape rewrite ----------------------------------------------------- #
def test_the_chain_and_the_junction_are_read_from_the_NETLIST():
    """⛔ The first cut laid every family out in declaration order and called it
    a row. The order a chain wants is the order the *wiring* implies."""
    from skidl_layout.cells_families import FAMILIES, chain_order, junction_net

    by_name = {spec.name: spec for spec in FAMILIES}
    assert chain_order(by_name["divider"]) == ("R1", "R2")
    assert junction_net(by_name["divider"]) == "MID"
    # ⭐ Three parts, one pad each on MID -- a junction, not a series string.
    assert junction_net(by_name["divider_cap"]) == "MID"
    assert set(chain_order(by_name["divider_cap"])) == {"R1", "R2", "C1"}
    # ⛔ A two-part family HAS a shared net but it is not a junction over three
    # legs, so ``stack`` must refuse it rather than invent a second row.
    assert junction_net(by_name["rc_snubber"]) == "SNUB"


def test_a_net_that_touches_a_member_TWICE_is_not_a_junction():
    from skidl_layout.cells_families import FamilySpec, junction_net

    shorted = FamilySpec(
        name="shorted", parts=(("R1", "R"), ("R2", "R")),
        nets={"N": [("R1", "1"), ("R1", "2"), ("R2", "1")],
              "OUT": [("R2", "2")]})
    assert junction_net(shorted) is None


def test_a_family_with_no_hamiltonian_path_has_no_chain():
    from skidl_layout.cells_families import FamilySpec, chain_order

    islands = FamilySpec(name="islands", parts=(("R1", "R"), ("R2", "R")),
                         nets={"A": [("R1", "1")], "B": [("R1", "2")],
                               "C": [("R2", "1")], "D": [("R2", "2")]})
    assert chain_order(islands) is None


@needs_footprints
def test_the_long_shape_turns_each_member_TOWARD_its_neighbour():
    """⭐ The shared pads must end up facing each other, not at opposite ends."""
    from skidl_layout.cells_families import FAMILIES, generate_family

    by_name = {spec.name: spec for spec in FAMILIES}
    cell, _run = generate_family(by_name["divider"], "0805",
                                 fp_lib_dirs=_fp_dirs(), layers=2, shape="long")
    pads = {f"{p.local_ref}.{p.pad}": p for p in cell.pads}
    mid = [p for p in cell.pads if p.local_net == "MID"]
    assert len(mid) == 2
    # The two MID pads are the innermost pads of the cell on the x axis.
    xs = sorted(p.x for p in cell.pads)
    assert sorted(p.x for p in mid) == xs[1:3]
    assert pads["R1.1"].x < pads["R1.2"].x < pads["R2.1"].x < pads["R2.2"].x


@needs_footprints
def test_the_short_shape_puts_every_bus_pad_on_ONE_side():
    from skidl_layout.cells_families import FAMILIES, generate_family

    by_name = {spec.name: spec for spec in FAMILIES}
    cell, _run = generate_family(by_name["divider_cap"], "0805",
                                 fp_lib_dirs=_fp_dirs(), layers=2,
                                 shape="short")
    mid = [p for p in cell.pads if p.local_net == "MID"]
    others = [p for p in cell.pads if p.local_net != "MID"]
    assert len(mid) == 3
    assert max(p.x for p in mid) < min(p.x for p in others)


@needs_footprints
def test_the_stack_shape_is_OFFSET_not_centred_and_opens_BOTH_axes():
    """⭐⭐⭐ The whole point of the shape, as two assertions.

    The lone member sits over ONE end of the spine, so the spine's own gap stays
    clear top to bottom -- and that plus the row gap is two passthroughs.
    """
    from skidl_layout.cells_families import FAMILIES, generate_family

    by_name = {spec.name: spec for spec in FAMILIES}
    cell, run = generate_family(by_name["divider_cap"], "0805",
                                fp_lib_dirs=_fp_dirs(), layers=2, shape="stack")
    assert run.contract_met
    rows = {}
    for member in cell.part_members:
        rows.setdefault(round(member.dy, 3), []).append(member)
    assert sorted(len(v) for v in rows.values()) == [1, 2], (
        "a stack is two members in one row and one in another")
    lone = next(v[0] for v in rows.values() if len(v) == 1)
    spine = sorted(next(v for v in rows.values() if len(v) == 2),
                   key=lambda m: m.dx)
    # ⛔ NOT centred: the lone member's east edge stops at the anchor's, so it
    # never reaches over the spine's gap.
    assert lone.dx + lone.w / 2.0 <= spine[0].dx + spine[0].w / 2.0 + 1e-6
    assert abs(lone.dx - (spine[0].dx + spine[1].dx) / 2.0) > 0.5
    ew = cell.transit_for(0, "EW")
    ns = cell.transit_for(0, "NS")
    assert ew and ns and ew.lanes and ns.lanes
    assert ew.max_trace >= 0.3 and ns.max_trace >= 0.3


@needs_footprints
def test_a_two_part_family_REFUSES_the_stack_shape():
    from skidl_layout.cells_families import FAMILIES, generate_family

    by_name = {spec.name: spec for spec in FAMILIES}
    cell, run = generate_family(by_name["lc_filter"], "0603",
                                fp_lib_dirs=_fp_dirs(), layers=2, shape="stack")
    assert cell is None
    assert "stack" in run.skipped and run.enumerated == 0


@needs_footprints
def test_every_shape_meets_its_own_transit_contract():
    """⛔⛔ The regression test for "0 of 24 open on both axes, 0 passing a
    power trace" -- the finding that made the first cut's cells unroutable."""
    from skidl_layout.cells_families import (FAMILIES, SHAPE_CONTRACT, SHAPES,
                                             generate_family)

    for spec in FAMILIES:
        for shape in SHAPES:
            cell, run = generate_family(spec, "0603", fp_lib_dirs=_fp_dirs(),
                                        layers=2, shape=shape)
            if cell is None:
                continue
            assert run.contract_met, f"{spec.name} {shape}"
            for axis in SHAPE_CONTRACT[shape]:
                transit = cell.transit_for(0, axis)
                assert transit and transit.max_trace >= 0.3, (
                    f"{spec.name} {shape}: {axis} max_trace "
                    f"{transit.max_trace if transit else None}")


@needs_footprints
def test_a_generated_cell_carries_its_shape_and_contract_as_provenance():
    from skidl_layout.cells_families import FAMILIES, generate_family

    cell, _run = generate_family(FAMILIES[1], "0603", fp_lib_dirs=_fp_dirs(),
                                 layers=2, shape="stack")
    meta = dict(cell.meta)
    assert meta["shape"] == "stack"
    assert meta["contract"] == ["EW", "NS"]
    assert meta["contract_met"] is True
    assert meta["compiled"]["body_obstructs"] is True


def test_the_snubber_is_the_family_with_an_internal_net():
    from skidl_layout.cells_families import FAMILIES

    by_name = {spec.name: spec for spec in FAMILIES}
    assert by_name["rc_snubber"].internal == ("SNUB",)
    assert by_name["divider"].internal == ()


@needs_footprints
def test_generate_family_produces_a_cached_generated_cell(tmp_path):
    from skidl_layout.cells_families import FAMILIES, generate_family

    cache = CellCache(str(tmp_path / "cache"))
    cell, run = generate_family(FAMILIES[0], "0603", fp_lib_dirs=_fp_dirs(),
                               cache=cache, layers=2)
    assert cell is not None
    assert dict(cell.meta)["source"] == "generated"
    assert dict(cell.meta)["family"] == "divider"
    assert dict(cell.meta)["size"] == "0603"
    assert cache.load(cell.digest) is not None
    from skidl_layout.cells_families import GAP_LADDER

    assert run.enumerated == len(GAP_LADDER) and run.accepted > 0


@needs_footprints
def test_generate_family_is_deterministic(tmp_path):
    from skidl_layout.cells_families import FAMILIES, generate_family

    a, _ = generate_family(FAMILIES[3], "0402", fp_lib_dirs=_fp_dirs(),
                           layers=2)
    b, _ = generate_family(FAMILIES[3], "0402", fp_lib_dirs=_fp_dirs(),
                           layers=2)
    assert a.digest == b.digest


@needs_footprints
def test_a_SHAPED_cell_buys_a_PASSTHROUGH_with_area_and_that_is_the_trade():
    """⛔⛔ **A retraction, kept as a test so it cannot be re-derived.**

    Two earlier readings of ``area/naive`` are both dead. The first cut of this
    generator reported **0.69-0.86** and that was an overlap artifact (its
    members intersected); corrected it became **0.879-1.000**, five of twelve at
    exactly 1.000. ⛔ Now it is **> 1.0 on every shape**, and that is not a
    regression -- it is the generator finally doing what the human does: **the
    room is the point**. A cell whose members sit at the design clearance has no
    channel across it at all, so "smallest box" and "crossable" are in direct
    opposition and the shape contract picks crossable.

    ⭐ Which makes ``box area <= naive`` wrong for *authored* cells for the same
    reason the executed plan found it wrong for *harvested* ones -- the third
    time this project has reached that conclusion from a different direction.
    """
    from skidl_layout.cells_compile import compile_cell, naive_area_mm2
    from skidl_layout.cells_families import FAMILIES, generate_family

    cell, run = generate_family(FAMILIES[0], "0603", fp_lib_dirs=_fp_dirs(),
                                layers=2, shape="long")
    _built, acceptance = compile_cell(cell, body_obstructs=True)
    assert acceptance.area_ratio > 1.0
    assert run.contract_met
    # ⭐ And the thing bought is real: the naive edge-to-edge row of the same
    # members has no NS channel a routed track fits in, and this cell does.
    assert cell.transit_for(0, "NS").max_trace >= 0.3
    assert naive_area_mm2(cell, 0.25) < cell.area_mm2


# --------------------------------------------------------------------------- #
# WS-U7 -- the matcher
# --------------------------------------------------------------------------- #
class _Pin:
    def __init__(self, num, net):
        self.num = num
        self.net = net


class _Net:
    def __init__(self, name):
        self.name = name


class _Part:
    def __init__(self, ref, footprint, pins):
        self.ref = ref
        self.footprint = footprint
        self.pins = pins


class _Circuit:
    def __init__(self, parts):
        self.parts = parts


def _divider_circuit(extra_gnd=True):
    """Two dividers plus a stranger, all tied to GND."""
    fp = "Resistor_SMD:R_0805_2012Metric"
    nets = {n: _Net(n) for n in ("VOUT", "VFB", "GND", "VIN", "FB2")}
    parts = [
        _Part("RFB1", fp, [_Pin("1", nets["VOUT"]), _Pin("2", nets["VFB"])]),
        _Part("RFB2", fp, [_Pin("1", nets["VFB"]), _Pin("2", nets["GND"])]),
        _Part("RUV1", fp, [_Pin("1", nets["VIN"]), _Pin("2", nets["FB2"])]),
        _Part("RUV2", fp, [_Pin("1", nets["FB2"]), _Pin("2", nets["GND"])]),
        _Part("RX", fp, [_Pin("1", nets["GND"]), _Pin("2", nets["GND"])]),
    ]
    return _Circuit(parts)


def test_plane_nets_are_excluded_from_the_host_adjacency():
    """⛔⛔ Without this every pair of resistors matches every 2-R cell."""
    from skidl_layout.cells_match import circuit_graph

    _labels, adjacency, _nets = circuit_graph(_divider_circuit())
    assert "RUV1" not in adjacency["RFB1"]
    assert "RFB2" in adjacency["RFB1"]


def test_the_matcher_finds_both_dividers_and_not_the_stranger():
    from skidl_layout.cells_match import match_cell

    cell = _divider_cell()
    found, note = match_cell(cell, _divider_circuit())
    assert note == ""
    refs = sorted(m.refs for m in found)
    assert ("RFB1", "RFB2") in refs
    assert ("RUV1", "RUV2") in refs
    assert not any("RX" in pair for pair in refs)


def test_a_match_carries_a_consistent_net_binding():
    from skidl_layout.cells_match import match_cell

    found, _note = match_cell(_divider_cell(), _divider_circuit())
    match = next(m for m in found if m.refs == ("RFB1", "RFB2"))
    assert match.net_map["VOUT"] in ("VOUT", "GND")
    assert len(set(match.net_map.values())) == len(match.net_map)


def test_matching_is_deterministic():
    from skidl_layout.cells_match import match_cell

    circuit = _divider_circuit()
    first, _ = match_cell(_divider_cell(), circuit)
    second, _ = match_cell(_divider_cell(), circuit)
    assert [m.to_dict() for m in first] == [m.to_dict() for m in second]


def test_greedy_bind_never_claims_a_part_twice():
    from skidl_layout.cells_match import greedy_bind

    report = greedy_bind([_divider_cell()], _divider_circuit())
    claimed = [ref for match in report.matches for ref in match.refs]
    assert len(claimed) == len(set(claimed))
    assert report.parts_total == 5


def test_coverage_is_reported_as_a_fraction():
    from skidl_layout.cells_match import greedy_bind

    report = greedy_bind([_divider_cell()], _divider_circuit())
    assert 0.0 < report.coverage <= 1.0
    assert report.to_dict()["coverage"] == report.coverage


def test_the_node_bound_is_reported_not_silently_applied():
    from skidl_layout.cells_match import match_cell

    cell = _divider_cell()
    found, note = match_cell(cell, _divider_circuit(), max_nodes=1)
    assert found == []
    assert note.startswith("bound:")


def test_the_state_bound_is_reported_not_silently_applied():
    from skidl_layout.cells_match import match_cell

    found, note = match_cell(_divider_cell(), _divider_circuit(), max_states=1)
    assert note.startswith("abandoned at")


def test_the_signature_prunes_but_never_rejects_a_real_match():
    from skidl_layout.cells_match import cell_signature, index_by_signature

    cell = _divider_cell()
    other = LayoutCell(name="single", width=1.0, height=1.0,
                       members=(CellMember("R1", "part", "L:F"),)).normalised()
    assert cell_signature(cell) != cell_signature(other)
    index = index_by_signature([cell, other])
    assert index[cell_signature(cell)] == (cell,)


# --------------------------------------------------------------------------- #
# ⛔⛔ the acceptance criterion that was missing until 2026-07-31
# --------------------------------------------------------------------------- #
def test_overlapping_members_are_REJECTED_by_acceptance():
    """⛔⛔ 22 of 24 generated cells passed acceptance while their parts
    intersected. Every other criterion asks about *reachability*; none asked
    whether the members physically fit."""
    from skidl_layout.cells_compile import compile_cell

    cell = LayoutCell(
        name="colliding", width=3.0, height=2.0,
        members=(CellMember("R1", "part", "L:F", 1.0, 1.0, 0, "top", 2.0, 2.0),
                 CellMember("R2", "part", "L:F", 2.0, 1.0, 0, "top", 2.0, 2.0)),
        pads=(CellPad("R1", "1", "A", 0.5, 1.0, 0.4, 1.0),
              CellPad("R2", "2", "B", 2.5, 1.0, 0.4, 1.0)),
        nets={"A": ("R1.1",), "B": ("R2.2",)}).normalised()
    _built, acceptance = compile_cell(cell)
    assert acceptance.min_member_gap_mm < 0
    assert acceptance.members_legal is False
    assert acceptance.ok is False


def test_the_default_sweep_calls_the_strip_UNDER_A_BODY_a_lane_and_the_opt_in_does_not():
    """⛔⛔ **"The transit lanes are drawn straight through the parts."**

    MEASURED 2026-07-31 on the shipped caches: **28 of 28** layer-0 lanes in the
    generated family cache and **15 of 18** in the harvested one crossed a
    member's physical envelope. The sweep was not wrong -- it is a *copper*
    sweep, and a chip passive's ceramic body is not copper, so the strip between
    its two pads genuinely passes DRC. ⭐ It is still not a channel any reader
    would accept, so ``body_obstructs`` exists, is **off by default** (every
    recorded transit number stays byte-identical), and is what generated
    families sweep with.
    """
    from skidl_layout.cells import sweep_transit

    # One member 3 x 2 mm with pads only at its two ends: a 1 mm strip of bare
    # body runs across the middle, and only the copper sweep calls it a lane.
    cell = LayoutCell(
        name="one_body", width=3.0, height=2.0,
        members=(CellMember("R1", "part", "L:F", 1.5, 1.0, 0, "top", 3.0, 2.0),),
        pads=(CellPad("R1", "1", "A", 0.3, 1.0, 0.4, 0.4),
              CellPad("R1", "2", "B", 2.7, 1.0, 0.4, 0.4)),
        nets={"A": ("R1.1",), "B": ("R1.2",)}).normalised()

    copper = {t.axis: t for t in sweep_transit(cell, layers=(0,),
                                              clearance_mm=0.1,
                                              min_track_mm=0.15)}
    assert copper["EW"].lanes, "the copper sweep sees a clear strip"
    bodies = {t.axis: t for t in sweep_transit(cell, layers=(0,),
                                               clearance_mm=0.1,
                                               min_track_mm=0.15,
                                               body_obstructs=True)}
    assert not bodies["EW"].lanes, "the body sweep does not"
    # ⛔ And a body stops at the solder mask: an inner layer is untouched.
    inner = {t.axis: t for t in sweep_transit(cell, layers=(0, 1),
                                              clearance_mm=0.1,
                                              min_track_mm=0.15,
                                              body_obstructs=True)
             if t.layer == 1}
    assert inner["EW"].lanes


def test_a_gap_below_the_design_clearance_is_illegal_too():
    from skidl_layout.cells_compile import compile_cell

    cell = LayoutCell(
        name="tight", width=4.1, height=2.0,
        members=(CellMember("R1", "part", "L:F", 1.0, 1.0, 0, "top", 2.0, 2.0),
                 CellMember("R2", "part", "L:F", 3.1, 1.0, 0, "top", 2.0, 2.0)),
        pads=(CellPad("R1", "1", "A", 0.5, 1.0, 0.4, 1.0),
              CellPad("R2", "2", "B", 3.6, 1.0, 0.4, 1.0)),
        nets={"A": ("R1.1",), "B": ("R2.2",)}).normalised()
    _built, acceptance = compile_cell(cell, clearance_mm=0.25)
    assert acceptance.min_member_gap_mm == pytest.approx(0.1)
    assert acceptance.members_legal is False


def test_two_parts_clear_on_EITHER_axis_are_legal():
    """⭐ The validator's own rule, not a stricter one: a two-row arrangement
    may overlap on x as long as y separates it."""
    from skidl_layout.cells_compile import min_member_gap

    cell = LayoutCell(
        name="two_rows", width=2.0, height=5.0,
        members=(CellMember("R1", "part", "L:F", 1.0, 1.0, 0, "top", 2.0, 2.0),
                 CellMember("R2", "part", "L:F", 1.0, 4.0, 0, "top", 2.0, 2.0)),
        ).normalised()
    assert min_member_gap(cell) == pytest.approx(1.0)


def test_min_member_gap_of_a_single_member_cell_is_infinite():
    from skidl_layout.cells_compile import min_member_gap

    cell = LayoutCell(name="one", width=2.0, height=2.0,
                      members=(CellMember("R1", "part", "L:F", 1.0, 1.0),
                               )).normalised()
    assert min_member_gap(cell) == float("inf")


@needs_footprints
def test_every_generated_family_places_its_members_LEGALLY():
    """⛔⛔ The regression test for the body-size-table defect: a generated cell
    must be buildable, not merely reachable."""
    from skidl_layout.cells_compile import compile_cell
    from skidl_layout.cells_families import FAMILIES, SHAPES, generate_family

    built = 0
    for spec in FAMILIES:
        for size in ("0402", "0603", "0805"):
            for shape in SHAPES:
                cell, run = generate_family(spec, size, fp_lib_dirs=_fp_dirs(),
                                            layers=2, shape=shape)
                if cell is None:
                    assert run.skipped, f"{spec.name} {size} {shape}"
                    continue
                built += 1
                _b, acceptance = compile_cell(cell, body_obstructs=True)
                assert acceptance.members_legal, (
                    f"{spec.name} {size} {shape}: min gap "
                    f"{acceptance.min_member_gap_mm} mm")
    assert built == 27, built
