# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.power_zones` -- power-layout Phase 15.

Five halves, matching the five things the phase claims:

* **The hull** -- stable under input reordering (a polygon that moves between
  identical runs is a measurement of nothing), correct on the margin, and
  enclosing disjoint clusters rather than merging them into a slab.
* **The region model** -- sections from an explicit dict and from the
  classifier, with every dropped member named and every containment failure
  reported.
* **Overlap and priority** -- overlapping regions must come out at **distinct**
  fill priorities, because equal priorities let KiCad tie-break on the ``uuid4``
  we mint fresh each run.
* **The writer** -- s-expressions from KRT's own ``kicad_writer``, round-tripped
  through KRT's own **parser**, never eyeballed. Skipped when no KRT checkout is
  importable.
* **The KiCad-10 net form** -- ⭐ detected from the board's ``(version …)``, never
  assumed. The plan said "must be True because the stack is on the KiCad-10
  backend"; that is the *schematic* backend, and ``writer.py`` still stamps
  ``(version 20241229)`` on the board. A test pins the detection to KRT's own
  threshold so the two cannot drift.
"""

from __future__ import annotations

import os
import sys

import pytest

from skidl_layout.fabspec import OSHPARK_2L
from skidl_layout.power_zones import (
    DEFAULT_MARGIN_MM,
    ZonePlan,
    ZoneRegion,
    board_uses_name_nets,
    convex_hull,
    net_ids_from_board,
    plan_zone_regions,
    region_polygon,
    splice_zones,
    zone_sexprs,
)
from skidl_layout.power_zones import (
    _kicad_writer,
    _point_in_polygon,
    _polygon_area,
    _polygons_overlap,
    _rect_inside,
)
from skidl_layout.writer import PlacedPart

from test_power_constraints import BOOST_FOOTPRINTS, C08, FP_GEOMETRIES, MSOP, R08
from test_power_roles import _boost


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _placed(ref, x, y, footprint, rot=0.0):
    return PlacedPart(ref=ref, x_mm=x, y_mm=y, rot_deg=rot, footprint=footprint)


class _Result:
    """The duck type the planner reads: placed parts and (maybe) a stage plan."""

    def __init__(self, placed, power_stage_plan=None, outline=None):
        self.placed_parts = list(placed)
        self.power_stage_plan = power_stage_plan
        self.power_plan = None
        self.outline = outline


#: A minimal board text with the two things the splice path reads: a net table
#: and a balanced outer paren. Deliberately NOT a real board -- the real-board
#: round trip is the KRT-parser test below.
MINIMAL_BOARD = (
    '(kicad_pcb\n'
    '  (version 20241229)\n'
    '  (generator "skidl")\n'
    '  (net 0 "")\n'
    '  (net 1 "GND")\n'
    '  (net 2 "VIN")\n'
    ')\n'
)

KICAD10_BOARD = MINIMAL_BOARD.replace("20241229", "20260623")


def _krt_writer_or_skip():
    writer = _kicad_writer()
    if writer is None:
        pytest.skip("no importable KiCadRoutingTools checkout")
    return writer


# --------------------------------------------------------------------------- #
# the hull
# --------------------------------------------------------------------------- #

def test_hull_of_a_square_is_its_four_corners_without_interior_points():
    hull = convex_hull([(0, 0), (2, 0), (2, 2), (0, 2), (1, 1), (0.5, 1.5)])
    assert hull == [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]


def test_hull_drops_collinear_points():
    """⛔ A hull that keeps every point on an edge is not a stable outline: two
    placements that differ only in a part's position along an edge would emit
    different polygons for the same region."""
    hull = convex_hull([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)])
    assert hull == [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]


def test_hull_is_identical_under_input_reordering():
    """⭐ The determinism property the whole phase rests on."""
    points = [(3.5, 1.0), (0.0, 0.0), (2.0, 4.0), (1.0, 1.0), (4.0, 3.0)]
    first = convex_hull(points)
    assert convex_hull(list(reversed(points))) == first
    assert convex_hull(sorted(points, key=lambda p: -p[1])) == first


def test_region_polygon_is_identical_under_ref_reordering():
    placed = [_placed("U1", 10, 10, MSOP), _placed("R1", 20, 10, R08),
              _placed("C1", 15, 18, C08)]
    a = region_polygon(["U1", "R1", "C1"], placed, FP_GEOMETRIES)
    b = region_polygon(["C1", "U1", "R1"], placed, FP_GEOMETRIES)
    assert a == b and len(a) >= 4


def test_region_polygon_applies_the_margin_on_every_side():
    placed = [_placed("R1", 10.0, 10.0, R08)]
    width, height = 3.410, 1.950
    poly = region_polygon(["R1"], placed, FP_GEOMETRIES, margin_mm=1.0)
    xs = [x for x, _ in poly]
    ys = [y for _, y in poly]
    assert min(xs) == pytest.approx(10.0 - width / 2 - 1.0)
    assert max(xs) == pytest.approx(10.0 + width / 2 + 1.0)
    assert min(ys) == pytest.approx(10.0 - height / 2 - 1.0)
    assert max(ys) == pytest.approx(10.0 + height / 2 + 1.0)


def test_region_polygon_encloses_disjoint_clusters():
    """A hull spans two clusters -- that is the documented weakness AND the
    documented behaviour. It is recorded here so nobody 'fixes' it silently."""
    placed = [_placed("R1", 5, 5, R08), _placed("R2", 60, 55, R08)]
    poly = region_polygon(["R1", "R2"], placed, FP_GEOMETRIES)
    assert _point_in_polygon(5.0, 5.0, poly)
    assert _point_in_polygon(60.0, 55.0, poly)
    single = _polygon_area(region_polygon(["R1"], placed, FP_GEOMETRIES))
    # The bridging slab is what a hull costs on interleaved sections. Recorded,
    # not defended -- the plan's bail-out 2 is an explicit ``polygon=`` override.
    assert _polygon_area(poly) > 10 * single


def test_region_polygon_skips_unplaced_and_geometry_less_members():
    placed = [_placed("R1", 10, 10, R08)]
    assert region_polygon(["R1", "NOPE"], placed, FP_GEOMETRIES) == \
        region_polygon(["R1"], placed, FP_GEOMETRIES)
    assert region_polygon(["NOPE"], placed, FP_GEOMETRIES) == []


def test_region_polygon_refuses_a_concave_style():
    """⛔ Out of scope by the plan's §5, and it says so rather than silently
    falling back to a hull the caller did not ask for."""
    with pytest.raises(ValueError, match="rect_hull"):
        region_polygon(["R1"], [_placed("R1", 1, 1, R08)], FP_GEOMETRIES,
                       style="alpha")


# --------------------------------------------------------------------------- #
# containment and overlap
# --------------------------------------------------------------------------- #

def test_every_member_courtyard_lies_inside_its_own_region():
    placed = [_placed("U1", 10, 10, MSOP), _placed("R1", 22, 12, R08),
              _placed("C1", 16, 20, C08)]
    poly = region_polygon(["U1", "R1", "C1"], placed, FP_GEOMETRIES)
    from skidl_layout.power_escape import part_rect
    for part in placed:
        assert _rect_inside(part_rect(part, FP_GEOMETRIES[part.footprint]), poly)


def test_overlap_detection_catches_nesting_and_edge_crossing():
    big = [(0, 0), (10, 0), (10, 10), (0, 10)]
    nested = [(2, 2), (4, 2), (4, 4), (2, 4)]
    crossing = [(8, 5), (14, 5), (14, 7), (8, 7)]
    apart = [(20, 20), (24, 20), (24, 24), (20, 24)]
    assert _polygons_overlap(big, nested)
    assert _polygons_overlap(big, crossing)
    assert not _polygons_overlap(big, apart)


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #

def _boost_placement():
    """The boost fixture, laid out in two clearly separated clusters."""
    spots = {
        "U1": (10, 10), "CIN": (22, 8), "L1": (24, 20), "M1": (16, 20),
        "RS": (10, 26), "D1": (30, 14), "COUT1": (38, 20), "COUT2": (38, 30),
        "COUT3": (30, 28), "R1": (46, 10), "R2": (46, 16), "RC": (46, 22),
        "RT": (46, 28), "CC2": (52, 12), "CVCC": (52, 20),
    }
    return [_placed(ref, x, y, BOOST_FOOTPRINTS[ref])
            for ref, (x, y) in spots.items()]


def test_explicit_sections_produce_one_region_each():
    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed), fp_geometries=FP_GEOMETRIES,
        sections={"power": ["U1", "M1", "L1", "D1", "CIN"],
                  "analog": ["R1", "R2", "RC", "RT", "CC2", "CVCC"]},
        net="GND")
    assert [r.name for r in plan.regions] == ["power", "analog"]
    assert all(r.net == "GND" for r in plan.regions)
    assert all(r.source == "explicit" for r in plan.regions)
    assert all(len(r.polygon) >= 4 for r in plan.regions)
    assert plan.covered_nets == ["GND"]


def test_a_section_may_carry_its_own_net_layer_and_relief():
    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed), fp_geometries=FP_GEOMETRIES,
        sections={"power": {"refs": ["U1", "M1"], "net": "PGND",
                            "layer": "F.Cu", "direct_connect": False,
                            "margin_mm": 2.0}},
        net="GND")
    region = plan.regions[0]
    assert (region.net, region.layer, region.direct_connect) == \
        ("PGND", "F.Cu", False)
    assert region.margin_mm == pytest.approx(2.0)


def test_an_explicit_polygon_override_is_used_verbatim():
    placed = _boost_placement()
    outline = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    plan = plan_zone_regions(
        _Result(placed), fp_geometries=FP_GEOMETRIES,
        sections={"all": {"refs": ["U1"], "polygon": outline}}, net="GND")
    assert plan.regions[0].polygon == outline


def test_a_member_outside_an_overridden_polygon_is_REPORTED():
    """⚠ The one way containment can fail. It must be a warning, never silence."""
    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed), fp_geometries=FP_GEOMETRIES,
        sections={"tiny": {"refs": ["U1", "COUT2"],
                           "polygon": [(0, 0), (4, 0), (4, 4), (0, 4)]}},
        net="GND")
    assert any("lie OUTSIDE" in w for w in plan.warnings)


def test_a_dropped_member_is_named_not_swallowed():
    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed), fp_geometries=FP_GEOMETRIES,
        sections={"power": ["U1", "GHOST"]}, net="GND")
    assert plan.regions[0].missing_refs == ["GHOST"]
    assert any("GHOST" in w for w in plan.warnings)


def test_derived_regions_come_from_the_classifier_and_cover_every_part():
    """⭐ The coverage property: a derived plan must not leave a placed part
    outside every region, or a ground pad that had copper before loses it."""
    from skidl_layout.power_roles import classify_power_roles

    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed, classify_power_roles(_boost())),
        fp_geometries=FP_GEOMETRIES)
    assert plan.regions, plan.warnings
    assert any(r.source.startswith("derived:") for r in plan.regions)
    claimed = {ref for region in plan.regions for ref in region.refs}
    assert claimed == {p.ref for p in placed}


def test_an_empty_placement_plans_nothing_and_says_so():
    plan = plan_zone_regions(_Result([]), fp_geometries=FP_GEOMETRIES)
    assert plan.regions == [] and plan.carves == []
    assert any("no placed parts" in w for w in plan.warnings)


def test_no_classifier_and_no_sections_declines_rather_than_guessing():
    plan = plan_zone_regions(_Result(_boost_placement()),
                             fp_geometries=FP_GEOMETRIES)
    assert plan.regions == []
    assert any("no power stage was classified" in w for w in plan.warnings)


# --------------------------------------------------------------------------- #
# priority -- the non-deterministic-fill guard
# --------------------------------------------------------------------------- #

def test_overlapping_regions_on_different_nets_get_DISTINCT_priorities():
    """⚠⚠ Equal priorities let KiCad tie-break on the fresh uuid4 we mint each
    run; that graded a real board '1 net unconnected' on 4 of 6 rolls."""
    _krt_writer_or_skip()
    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed), fp_geometries=FP_GEOMETRIES,
        sections={"outer": {"refs": [p.ref for p in placed], "net": "GND"},
                  "inner": {"refs": ["U1", "M1"], "net": "VIN"}})
    assert plan.overlaps
    priorities = [r.priority for r in plan.regions]
    assert len(set(priorities)) == len(priorities)
    # Smaller area wins: the nested VIN region must outrank the board-wide GND.
    by_name = {r.name: r for r in plan.regions}
    assert by_name["inner"].priority > by_name["outer"].priority
    assert any("overlap" in w for w in plan.warnings)


def test_non_overlapping_regions_stay_at_priority_zero():
    """A board that never had the ambiguity must be unaffected."""
    _krt_writer_or_skip()
    placed = [_placed("R1", 5, 5, R08), _placed("R2", 60, 55, R08)]
    plan = plan_zone_regions(
        _Result(placed), fp_geometries=FP_GEOMETRIES,
        sections={"a": {"refs": ["R1"], "net": "GND"},
                  "b": {"refs": ["R2"], "net": "VIN"}})
    assert [r.priority for r in plan.regions] == [0, 0]
    assert plan.overlaps == []


# --------------------------------------------------------------------------- #
# the escape carve -- POUR-exclusion, not the refuted track keepout
# --------------------------------------------------------------------------- #

def test_the_carve_excludes_POUR_and_leaves_tracks_vias_and_pads_allowed():
    """⛔⛔ The load-bearing distinction of the whole phase. ``route.py
    --keepout`` blocks tracks and lost 32, 22 and 0-with-8-DRC nets across three
    measured arms. A rule area blocks fill only, which is the asymmetry an
    escape via needs."""
    writer = _krt_writer_or_skip()
    sexpr = writer.generate_keepout_zone_sexpr(
        layers=["B.Cu"], polygon_points=[(0, 0), (1, 0), (1, 1), (0, 1)],
        name="escape-U1-0")
    assert "(copperpour not_allowed)" in sexpr
    assert "(tracks allowed)" in sexpr
    assert "(vias allowed)" in sexpr
    assert "(pads allowed)" in sexpr


def test_escape_carve_writes_one_rule_area_per_annulus_rectangle():
    from skidl_layout.power_roles import classify_power_roles

    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed, classify_power_roles(_boost())),
        fp_geometries=FP_GEOMETRIES, escape_carve=True, fab_spec=OSHPARK_2L)
    assert len(plan.carves) == 4          # the annulus is four tiling rects
    assert all(layer == "B.Cu" for layer, _poly, _name in plan.carves)
    assert all(name.startswith("escape-U1-") for _l, _p, name in plan.carves)
    assert any("block POUR only" in w for w in plan.warnings)


def test_the_carve_never_covers_the_controllers_own_courtyard():
    """⛔ A carve on the controller's own pads is the exact opposite of the
    intent. The annulus excludes it by construction; this is the assertion."""
    from skidl_layout.power_escape import part_rect
    from skidl_layout.power_roles import classify_power_roles

    placed = _boost_placement()
    plan = plan_zone_regions(
        _Result(placed, classify_power_roles(_boost())),
        fp_geometries=FP_GEOMETRIES, escape_carve=True, fab_spec=OSHPARK_2L)
    controller = next(p for p in placed if p.ref == "U1")
    x0, y0, x1, y1 = part_rect(controller, FP_GEOMETRIES[controller.footprint])
    centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    for _layer, polygon, _name in plan.carves:
        assert not _point_in_polygon(centre[0], centre[1], polygon)


def test_no_carve_when_nothing_is_placed():
    plan = plan_zone_regions(_Result([]), fp_geometries=FP_GEOMETRIES,
                             escape_carve=True)
    assert plan.carves == []


# --------------------------------------------------------------------------- #
# the KiCad-9 / KiCad-10 net form -- detected, never assumed
# --------------------------------------------------------------------------- #

def test_board_version_decides_the_net_form():
    assert board_uses_name_nets(KICAD10_BOARD) is True
    assert board_uses_name_nets(MINIMAL_BOARD) is False
    assert board_uses_name_nets("") is False


def test_the_detection_threshold_matches_KRTs_own():
    """⛔ Two copies of a constant is a drift waiting to happen; this is the
    assertion that stops it."""
    from skidl_layout.krt import find_krt
    from skidl_layout import power_zones as PZ

    resolved = find_krt()
    if resolved is None:
        pytest.skip("no importable KiCadRoutingTools checkout")
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    import kicad_parser

    assert PZ.KICAD_10_MIN_VERSION == kicad_parser.KICAD_10_MIN_VERSION


def test_zone_sexprs_emit_the_form_the_board_asked_for():
    _krt_writer_or_skip()
    plan = ZonePlan(regions=[ZoneRegion(
        name="power", refs=["U1"], net="GND", layer="B.Cu",
        polygon=[(0, 0), (10, 0), (10, 10), (0, 10)])])
    nine = zone_sexprs(plan, {"GND": 1}, kicad10=False)[0]
    ten = zone_sexprs(plan, {"GND": 1}, kicad10=True)[0]
    assert "(net 1)" in nine and '(net_name "GND")' in nine
    assert '(net "GND")' in ten and "net_name" not in ten


def test_a_region_on_a_net_the_board_does_not_declare_emits_no_zone():
    _krt_writer_or_skip()
    plan = ZonePlan(regions=[ZoneRegion(
        name="power", refs=["U1"], net="NOT_A_NET", layer="B.Cu",
        polygon=[(0, 0), (10, 0), (10, 10), (0, 10)])])
    assert zone_sexprs(plan, net_ids_from_board(MINIMAL_BOARD)) == []
    assert any("is not on the board" in w for w in plan.warnings)


# --------------------------------------------------------------------------- #
# the splice
# --------------------------------------------------------------------------- #

def test_net_ids_are_read_off_a_pre10_net_table():
    assert net_ids_from_board(MINIMAL_BOARD) == {"": 0, "GND": 1, "VIN": 2}


def test_net_names_are_recovered_from_a_KICAD_10_board_with_NO_net_table():
    """⛔⛔ KiCad 10 removes the top-level net table outright. Measured on a
    board round-tripped through KiCad 10.0.4: the whole ``(net <id> "<name>")``
    block is gone and only per-item ``(net "NAME")`` references survive. Reading
    only the table returns ``{}``, and every region then silently emits no
    zone -- which is the failure this recovery path exists to prevent."""
    k10 = (
        '(kicad_pcb\n\t(version 20260623)\n\t(generator "pcbnew")\n'
        '\t(footprint "X"\n'
        '\t\t(pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5)\n'
        '\t\t\t(layers "*.Cu")\n\t\t\t(net "GND"))\n'
        '\t\t(pad "2" thru_hole circle (at 2 0) (size 1 1) (drill 0.5)\n'
        '\t\t\t(layers "*.Cu")\n\t\t\t(net "VIN"))\n'
        '\t)\n)\n'
    )
    assert net_ids_from_board(k10) == {"": 0, "GND": 1, "VIN": 2}


def test_splice_keeps_the_board_balanced_and_adds_every_block(tmp_path):
    src = tmp_path / "in.kicad_pcb"
    dst = tmp_path / "out.kicad_pcb"
    src.write_text(MINIMAL_BOARD, encoding="utf-8")
    blocks = ["\t(zone\n\t\t(net 1)\n\t)", "\t(zone\n\t\t(net 2)\n\t)"]
    assert splice_zones(str(src), str(dst), blocks) == 2
    text = dst.read_text(encoding="utf-8")
    assert text.count("(zone") == 2
    assert text.count("(") == text.count(")")
    assert text.rstrip().endswith(")")


def test_splice_of_nothing_still_produces_the_output_board(tmp_path):
    src = tmp_path / "in.kicad_pcb"
    dst = tmp_path / "out.kicad_pcb"
    src.write_text(MINIMAL_BOARD, encoding="utf-8")
    assert splice_zones(str(src), str(dst), []) == 0
    assert dst.read_text(encoding="utf-8") == MINIMAL_BOARD


def test_splice_carries_the_sibling_project_file(tmp_path):
    """⚠ The project file holds the DRC floor the chain routed to; stranding it
    manufactures phantom clearance violations at the next grading step."""
    src = tmp_path / "in.kicad_pcb"
    dst = tmp_path / "out.kicad_pcb"
    src.write_text(MINIMAL_BOARD, encoding="utf-8")
    (tmp_path / "in.kicad_pro").write_text('{"board": {}}', encoding="utf-8")
    splice_zones(str(src), str(dst), ["\t(zone\n\t\t(net 1)\n\t)"])
    assert (tmp_path / "out.kicad_pro").is_file()


def test_spliced_zones_round_trip_through_KRTs_own_parser(tmp_path):
    """⛔ Verified by parsing, never by eye -- the rule the keepout writer set."""
    from skidl_layout.krt import find_krt

    resolved = find_krt()
    if resolved is None:
        pytest.skip("no importable KiCadRoutingTools checkout")
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    from kicad_parser import parse_kicad_pcb

    plan = ZonePlan(
        regions=[ZoneRegion(name="power", refs=["U1"], net="GND", layer="B.Cu",
                            polygon=[(0, 0), (20, 0), (20, 20), (0, 20)]),
                 ZoneRegion(name="rail", refs=["U1"], net="VIN", layer="F.Cu",
                            polygon=[(2, 2), (8, 2), (8, 8), (2, 8)],
                            direct_connect=False)],
        carves=[("B.Cu", [(4, 4), (6, 4), (6, 6), (4, 6)], "escape-U1-0")])

    board = _board_with_outline()
    src = tmp_path / "in.kicad_pcb"
    dst = tmp_path / "out.kicad_pcb"
    src.write_text(board, encoding="utf-8")
    sexprs = zone_sexprs(plan, net_ids_from_board(board),
                         kicad10=board_uses_name_nets(board))
    assert splice_zones(str(src), str(dst), sexprs) == 3

    pcb = parse_kicad_pcb(str(dst))
    zones = list(getattr(pcb, "zones", None) or [])
    # The two pours must be readable and carry their nets; the rule area is a
    # keepout and KRT's parser may or may not surface it as a Zone -- what must
    # never happen is the file failing to parse.
    poured = {z.net_name for z in zones if getattr(z, "net_name", None)}
    assert {"GND", "VIN"} <= poured


def _board_with_outline() -> str:
    """A board with a net table, an ``Edge.Cuts`` rectangle and two real pads.

    ⚠ **The pads are not decoration.** KiCad drops every net no pad references
    when it re-saves, so a board with a bare net table comes back from pcbnew
    with an EMPTY net table and every region's net "not declared on the board".
    That cost the first version of the KiCad-10 test, and it is the same trap a
    caller would hit splicing zones into a stripped board. The pads are
    through-hole so they reach both copper layers, which is also what lets a
    ``B.Cu`` pour survive KiCad's island removal.
    """
    return (
        '(kicad_pcb\n'
        '  (version 20241229)\n'
        '  (generator "skidl")\n'
        '  (generator_version "9.0")\n'
        '  (general (thickness 1.6))\n'
        '  (paper "A4")\n'
        '  (layers\n'
        '    (0 "F.Cu" signal)\n'
        '    (2 "B.Cu" signal)\n'
        '    (44 "Edge.Cuts" user)\n'
        '  )\n'
        '  (net 0 "")\n'
        '  (net 1 "GND")\n'
        '  (net 2 "VIN")\n'
        '  (gr_rect (start 0 0) (end 30 30)\n'
        '    (stroke (width 0.05) (type solid)) (fill no)\n'
        '    (layer "Edge.Cuts") (uuid "11111111-1111-1111-1111-111111111111"))\n'
        '  (footprint "TESTPOINTS"\n'
        '    (layer "F.Cu")\n'
        '    (uuid "22222222-2222-2222-2222-222222222222")\n'
        '    (at 15 15)\n'
        '    (pad "1" thru_hole circle (at -3 0) (size 1.6 1.6) (drill 0.8)\n'
        '      (layers "*.Cu" "*.Mask") (net 1 "GND")\n'
        '      (uuid "33333333-3333-3333-3333-333333333333"))\n'
        '    (pad "2" thru_hole circle (at 3 0) (size 1.6 1.6) (drill 0.8)\n'
        '      (layers "*.Cu" "*.Mask") (net 2 "VIN")\n'
        '      (uuid "44444444-4444-4444-4444-444444444444"))\n'
        '  )\n'
        ')\n'
    )


# --------------------------------------------------------------------------- #
# ⭐ KiCad 10 itself -- the only proof that matters for a zone
# --------------------------------------------------------------------------- #

def test_a_spliced_KICAD_10_board_loads_and_fills_in_KiCad(tmp_path):
    """⭐⭐ The board is re-saved by **KiCad 10's own pcbnew** (so it carries a
    real KiCad-10 ``(version …)``), zones are spliced into *that*, and KiCad is
    asked to fill them. A zone KiCad refuses to load is worse than no zone, and
    a format claim graded by our own parser is not a claim about KiCad.

    Skipped when no ``pcbnew``-bearing python is installed.
    """
    from skidl_layout.copper_fill import fill_board, find_kicad_python

    if find_kicad_python() is None:
        pytest.skip("no python that imports pcbnew was found")

    src = tmp_path / "board.kicad_pcb"
    src.write_text(_board_with_outline(), encoding="utf-8")
    # Round-trip through KiCad itself: fill_board(out_path=...) writes the board
    # KiCad saved, which is by definition the current KiCad file format.
    if fill_board(str(src), out_path=str(tmp_path / "k10.kicad_pcb")) is None:
        pytest.skip("KiCad refill unavailable on this machine")

    k10 = (tmp_path / "k10.kicad_pcb").read_text(encoding="utf-8",
                                                 errors="replace")
    assert board_uses_name_nets(k10), (
        "KiCad 10 re-saved a board that still reads as pre-10; the version "
        "detection is wrong")

    plan = ZonePlan(regions=[ZoneRegion(
        name="power", refs=[], net="GND", layer="B.Cu",
        polygon=[(2, 2), (28, 2), (28, 28), (2, 28)])])
    sexprs = zone_sexprs(plan, net_ids_from_board(k10),
                         kicad10=board_uses_name_nets(k10))
    assert '(net "GND")' in sexprs[0]
    spliced = tmp_path / "k10_zoned.kicad_pcb"
    assert splice_zones(str(tmp_path / "k10.kicad_pcb"), str(spliced), sexprs) == 1

    filled = fill_board(str(spliced))
    assert filled is not None, "KiCad refused to load or fill the spliced board"
    # It loaded, it filled, and the copper landed on the net we asked for.
    assert filled.area_by_net().get("GND", 0.0) > 100.0
    assert filled.island_count.get(("GND", "B.Cu")) == 1
