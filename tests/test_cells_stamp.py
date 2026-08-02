# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.cells_stamp` -- copper stamping and the fences.

The transform half and the fence arithmetic need nothing at all. The splice half
needs KRT's s-expression constructors (escalation rung 2) and skips without a
checkout; the *routing* half is gate ``U2`` in ``canaries/drive_templates.py``,
where a router belongs.
"""

from __future__ import annotations

import pytest

from skidl_layout.cells import (
    CellCopper,
    CellMember,
    CellPad,
    CellSegment,
    CellTransit,
    CellVia,
    LayoutCell,
    TransitLane,
)
from skidl_layout.cells_stamp import (
    cell_fence_polygons,
    instance_copper,
    layer_name,
    stamped_copper_key,
)


def _has_krt():
    from skidl_layout.krt import find_krt

    return find_krt(None) is not None


needs_krt = pytest.mark.skipif(not _has_krt(), reason="no KiCadRoutingTools")


def _coppered_cell() -> LayoutCell:
    """A 4 x 2 mm box with one internal track, one boundary track and a via."""
    return LayoutCell(
        name="snub",
        width=4.0,
        height=2.0,
        members=(CellMember("R1", "part", "L:F", 1.0, 1.0, 0, "top", 2.0, 2.0),
                 CellMember("C1", "part", "L:F", 3.0, 1.0, 0, "top", 2.0, 2.0)),
        pads=(CellPad("R1", "1", "A", 0.5, 1.0, 0.5, 1.0),
              CellPad("R1", "2", "SNUB", 1.5, 1.0, 0.5, 1.0),
              CellPad("C1", "1", "SNUB", 2.5, 1.0, 0.5, 1.0),
              CellPad("C1", "2", "B", 3.5, 1.0, 0.5, 1.0)),
        nets={"A": ("R1.1",), "SNUB": ("R1.2", "C1.1"), "B": ("C1.2",)},
        internal_nets=frozenset({"SNUB"}),
        copper=CellCopper(
            segments=(CellSegment(1.5, 1.0, 2.5, 1.0, 0, 0.3, "SNUB"),
                      CellSegment(0.5, 1.0, 0.0, 1.0, 0, 0.3, "A")),
            vias=(CellVia(2.0, 1.0, 0.6, 0.3, "SNUB"),)),
        transit=(CellTransit(layer=0, axis="EW",
                             lanes=(TransitLane(0, "EW", 0.0, 0.4),
                                    TransitLane(0, "EW", 1.6, 2.0)),
                             clearance=0.25),),
        layers_defined=(0,),
        stackup=2,
    ).normalised()


# --------------------------------------------------------------------------- #
# layer names
# --------------------------------------------------------------------------- #
def test_layer_names_come_from_the_writers_own_table():
    assert layer_name(0) == "F.Cu"
    assert layer_name(1) == "B.Cu"
    assert layer_name(1, 4) == "In1.Cu"
    assert layer_name(3, 4) == "B.Cu"


def test_a_layer_outside_the_stackup_raises():
    with pytest.raises(ValueError):
        layer_name(3, 2)


# --------------------------------------------------------------------------- #
# the transform
# --------------------------------------------------------------------------- #
def test_copper_lands_relative_to_the_box_centre():
    tracks, vias = instance_copper(_coppered_cell(), 100.0, 50.0, 0)
    # the box is 4 x 2 centred at (100, 50), so its origin corner is (98, 49)
    snub = [t for t in tracks if t.net == "SNUB"][0]
    assert (snub.x1, snub.y1) == pytest.approx((99.5, 50.0))
    assert (snub.x2, snub.y2) == pytest.approx((100.5, 50.0))
    assert (vias[0].x, vias[0].y) == pytest.approx((100.0, 50.0))


def test_rotation_moves_copper_with_the_cell():
    straight, _v = instance_copper(_coppered_cell(), 100.0, 50.0, 0)
    turned, _v2 = instance_copper(_coppered_cell(), 100.0, 50.0, 90)
    assert {t.key() for t in straight} != {t.key() for t in turned}
    # the 1 mm SNUB track stays 1 mm long whichever way the cell faces
    def _len(tracks):
        t = [x for x in tracks if x.net == "SNUB"][0]
        return round(((t.x2 - t.x1) ** 2 + (t.y2 - t.y1) ** 2) ** 0.5, 6)
    assert _len(straight) == _len(turned) == 1.0


def test_only_internal_net_copper_is_locked():
    """⛔ The section 10.2 hazard: a locked boundary stub is never rippable."""
    tracks, vias = instance_copper(_coppered_cell(), 10.0, 10.0, 0)
    locked = {t.net for t in tracks if t.locked}
    assert locked == {"SNUB"}
    assert all(v.locked for v in vias)


def test_locking_can_be_turned_off_so_the_negative_is_reproducible():
    tracks, _v = instance_copper(_coppered_cell(), 10.0, 10.0, 0,
                                 lock_internal=False)
    assert not any(t.locked for t in tracks)


def test_the_net_map_renames_copper_onto_circuit_nets():
    tracks, _v = instance_copper(_coppered_cell(), 10.0, 10.0, 0,
                                 net_map={"A": "VIN"})
    assert "VIN" in {t.net for t in tracks}
    assert "A" not in {t.net for t in tracks}


def test_a_cell_with_no_copper_stamps_nothing():
    cell = LayoutCell(name="bare", width=1.0, height=1.0).normalised()
    assert instance_copper(cell, 0.0, 0.0, 0) == ([], [])


def test_the_emitted_order_is_total_so_a_stamp_is_reproducible():
    a = instance_copper(_coppered_cell(), 10.0, 10.0, 180)
    b = instance_copper(_coppered_cell(), 10.0, 10.0, 180)
    assert [t.key() for t in a[0]] == [t.key() for t in b[0]]
    assert [t.key() for t in a[0]] == sorted(t.key() for t in a[0])


# --------------------------------------------------------------------------- #
# the fences
# --------------------------------------------------------------------------- #
def test_the_blanket_fence_is_the_whole_box():
    poly = cell_fence_polygons(_coppered_cell(), 100.0, 50.0, 0,
                               mode="blanket")[0]
    xs = sorted({p[0] for p in poly})
    ys = sorted({p[1] for p in poly})
    assert xs == [98.0, 102.0]
    assert ys == [49.0, 51.0]


def test_the_lane_fence_is_the_box_MINUS_the_lanes():
    """⭐ The transit map's first consumer."""
    polys = cell_fence_polygons(_coppered_cell(), 100.0, 50.0, 0, mode="lanes",
                                layer=0, axis="EW")
    # lanes cover y in [0, 0.4] and [1.6, 2.0]; the blocked strip is [0.4, 1.6]
    assert len(polys) == 1
    ys = sorted({p[1] for p in polys[0]})
    assert ys == pytest.approx([49.4, 50.6])


def test_an_undefined_layer_gets_NO_lane_fence():
    """⚠ Blank layer = fully passable. The opposite of a port's absence."""
    assert cell_fence_polygons(_coppered_cell(), 0.0, 0.0, 0, mode="lanes",
                               layer=1, axis="EW") == []


def test_a_fully_blocked_axis_fences_the_whole_box():
    cell = LayoutCell(name="solid", width=2.0, height=2.0,
                      transit=(CellTransit(layer=0, axis="EW", lanes=()),),
                      ).normalised()
    polys = cell_fence_polygons(cell, 0.0, 0.0, 0, mode="lanes", layer=0,
                                axis="EW")
    assert len(polys) == 1
    assert sorted({p[1] for p in polys[0]}) == pytest.approx([-1.0, 1.0])


def test_an_unknown_fence_mode_raises():
    with pytest.raises(ValueError):
        cell_fence_polygons(_coppered_cell(), 0.0, 0.0, 0, mode="halo")


def test_the_fence_rotates_with_the_cell():
    straight = cell_fence_polygons(_coppered_cell(), 0.0, 0.0, 0,
                                   mode="blanket")[0]
    turned = cell_fence_polygons(_coppered_cell(), 0.0, 0.0, 90,
                                 mode="blanket")[0]
    assert sorted({p[0] for p in straight}) == pytest.approx([-2.0, 2.0])
    assert sorted({p[0] for p in turned}) == pytest.approx([-1.0, 1.0])


# --------------------------------------------------------------------------- #
# the splice
# --------------------------------------------------------------------------- #
_BOARD = """(kicad_pcb
	(version 20241229)
	(generator "test")
	(net 0 "")
	(net 1 "A")
	(net 2 "SNUB")
	(net 3 "B")
)
"""


@needs_krt
def test_splice_writes_every_track_and_via(tmp_path):
    from skidl_layout.cells_stamp import stamp_cell_copper

    src = tmp_path / "in.kicad_pcb"
    src.write_text(_BOARD, encoding="utf-8")
    out = tmp_path / "out.kicad_pcb"
    result = stamp_cell_copper(str(src), str(out),
                              [(_coppered_cell(), 10.0, 10.0, 0, {})])
    assert result.tracks == 2 and result.vias == 1
    assert result.locked_tracks == 1 and result.locked_vias == 1
    assert result.net_form == "id"
    assert result.unknown_nets == ()
    text = out.read_text(encoding="utf-8")
    assert text.count("(segment") == 2
    assert text.count("(via") == 1
    assert text.count("(locked yes)") == 2
    assert text.rstrip().endswith(")")


@needs_krt
def test_the_splice_is_byte_identical_across_runs(tmp_path):
    """⛔ KRT's constructors stamp a fresh uuid4; we rewrite it as a uuid5."""
    from skidl_layout.cells_stamp import stamp_cell_copper

    src = tmp_path / "in.kicad_pcb"
    src.write_text(_BOARD, encoding="utf-8")
    first, second = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    for path in (first, second):
        stamp_cell_copper(str(src), str(path),
                          [(_coppered_cell(), 10.0, 10.0, 90, {})])
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


@needs_krt
def test_a_net_the_board_does_not_declare_is_REPORTED_not_dropped(tmp_path):
    from skidl_layout.cells_stamp import stamp_cell_copper

    src = tmp_path / "in.kicad_pcb"
    src.write_text(_BOARD, encoding="utf-8")
    out = tmp_path / "out.kicad_pcb"
    result = stamp_cell_copper(str(src), str(out),
                              [(_coppered_cell(), 10.0, 10.0, 0,
                                {"A": "NOT_ON_THE_BOARD"})])
    assert result.unknown_nets == ("NOT_ON_THE_BOARD",)
    assert result.tracks == 2          # still written, at net 0, and flagged


@needs_krt
def test_a_board_that_does_not_end_in_a_paren_is_refused(tmp_path):
    from skidl_layout.cells_stamp import stamp_cell_copper

    src = tmp_path / "in.kicad_pcb"
    src.write_text("(kicad_pcb\n", encoding="utf-8")
    with pytest.raises(ValueError):
        stamp_cell_copper(str(src), str(tmp_path / "out.kicad_pcb"),
                          [(_coppered_cell(), 0.0, 0.0, 0, {})])


@needs_krt
def test_what_we_wrote_reads_back_as_the_same_geometry(tmp_path):
    """⭐ One normalisation for both sides, so the gate cannot pass by accident."""
    from skidl_layout.cells_stamp import board_copper, stamp_cell_copper

    src = tmp_path / "in.kicad_pcb"
    src.write_text(_BOARD, encoding="utf-8")
    out = tmp_path / "out.kicad_pcb"
    stamp_cell_copper(str(src), str(out),
                      [(_coppered_cell(), 12.5, 7.25, 270, {})])
    tracks, vias = instance_copper(_coppered_cell(), 12.5, 7.25, 270)
    ok, missing_t, missing_v = board_copper(str(out)).contains_all(
        stamped_copper_key(tracks, vias))
    assert ok, (missing_t, missing_v)


def test_the_key_is_endpoint_order_insensitive():
    from skidl_layout.cells_stamp import _Track

    a = _Track(0.0, 0.0, 1.0, 1.0, 0.3, "F.Cu", "N")
    b = _Track(1.0, 1.0, 0.0, 0.0, 0.3, "F.Cu", "N")
    assert stamped_copper_key([a], []) == stamped_copper_key([b], [])
