from __future__ import annotations

import pytest

from skidl_layout.reader import (
    read_board_outline,
    read_footprint_bboxes,
    read_placed_positions,
)

MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (footprint "Resistor_SMD:R_0805_2012Metric"
    (at 50.5 30.2 90)
    (property "Reference" "R1" (at 0 0) (effects (font (size 1 1))))
    (pad "1" smd rect (at -0.9 0 90) (size 1 1.2) (layers "F.Cu"))
    (pad "2" smd rect (at 0.9 0 90) (size 1 1.2) (layers "F.Cu"))
  )
  (footprint "Package_DIP:DIP-16_W7.62mm"
    (at 0 0)
    (property "Reference" "U1" (at 0 0) (effects (font (size 1 1))))
    (pad "1" thru_hole rect (at -3.81 -8.89) (size 1.6 1.6) (layers "*.Cu"))
    (pad "16" thru_hole oval (at 3.81 -8.89) (size 1.6 1.6) (layers "*.Cu"))
  )
)"""

EMPTY_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
)"""


def _write_pcb(tmp_path, content: str, name: str = "test.kicad_pcb"):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_read_placed_skips_origin(tmp_path):
    path = _write_pcb(tmp_path, MINIMAL_PCB)
    placed = read_placed_positions(path)
    refs = {fp.ref for fp in placed}
    assert "R1" in refs
    assert "U1" not in refs


def test_read_placed_extracts_position(tmp_path):
    path = _write_pcb(tmp_path, MINIMAL_PCB)
    placed = read_placed_positions(path)
    r1 = next(fp for fp in placed if fp.ref == "R1")
    assert abs(r1.x_mm - 50.5) < 1e-6
    assert abs(r1.y_mm - 30.2) < 1e-6


def test_read_placed_extracts_angle(tmp_path):
    path = _write_pcb(tmp_path, MINIMAL_PCB)
    placed = read_placed_positions(path)
    r1 = next(fp for fp in placed if fp.ref == "R1")
    assert abs(r1.rot_deg - 90.0) < 1e-6


def test_read_placed_default_angle_zero(tmp_path):
    pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (footprint "Device:C"
    (at 10 20)
    (property "Reference" "C1" (at 0 0) (effects (font (size 1 1))))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu"))
  )
)"""
    path = _write_pcb(tmp_path, pcb)
    placed = read_placed_positions(path)
    assert len(placed) == 1
    assert abs(placed[0].rot_deg) < 1e-6


def test_read_footprint_bboxes(tmp_path):
    path = _write_pcb(tmp_path, MINIMAL_PCB)
    bboxes = read_footprint_bboxes(path)
    assert "Resistor_SMD:R_0805_2012Metric" in bboxes
    w, h = bboxes["Resistor_SMD:R_0805_2012Metric"]
    # pads at x=-0.9 and x=0.9, each 1mm wide → extents [-1.4, 1.4] → width 2.8
    assert abs(w - 2.8) < 1e-6
    # both pads at y=0, height 1.2 → extents [-0.6, 0.6] → height 1.2
    assert abs(h - 1.2) < 1e-6


def test_read_footprint_bboxes_dedup(tmp_path):
    pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (footprint "Device:R"
    (at 10 10)
    (property "Reference" "R1" (at 0 0) (effects (font (size 1 1))))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))
  )
  (footprint "Device:R"
    (at 20 20)
    (property "Reference" "R2" (at 0 0) (effects (font (size 1 1))))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))
  )
)"""
    path = _write_pcb(tmp_path, pcb)
    bboxes = read_footprint_bboxes(path)
    assert list(bboxes.keys()).count("Device:R") == 1


def test_empty_pcb(tmp_path):
    path = _write_pcb(tmp_path, EMPTY_PCB)
    assert read_placed_positions(path) == []
    assert read_footprint_bboxes(path) == {}
    assert read_board_outline(path) is None


def test_read_rect_board_outline(tmp_path):
    pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (gr_rect (start 10 20) (end 110 80) (layer "Edge.Cuts") (stroke (width 0.1)))
)"""
    path = _write_pcb(tmp_path, pcb)
    outline = read_board_outline(path)
    assert outline is not None
    assert outline.x_min == 10.0
    assert outline.y_min == 20.0
    assert outline.width_mm == 100.0
    assert outline.height_mm == 60.0


def test_read_polygon_board_outline(tmp_path):
    pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 30 0) (end 25 20) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 25 20) (end 0 20) (layer "Edge.Cuts") (stroke (width 0.1)))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (stroke (width 0.1)))
)"""
    path = _write_pcb(tmp_path, pcb)
    outline = read_board_outline(path)
    assert outline is not None
    assert outline.vertices == [(0.0, 0.0), (30.0, 0.0), (25.0, 20.0), (0.0, 20.0)]
