from __future__ import annotations

from skidl_layout.constraints import BoardOutline


def test_rectangle_shorthand():
    outline = BoardOutline(100.0, 80.0)
    assert outline.width_mm == 100.0
    assert outline.height_mm == 80.0
    assert outline.vertices == [
        (0.0, 0.0),
        (100.0, 0.0),
        (100.0, 80.0),
        (0.0, 80.0),
    ]


def test_rectangle_keyword_args():
    outline = BoardOutline(width_mm=100.0, height_mm=80.0)
    assert outline.width_mm == 100.0
    assert outline.height_mm == 80.0


def test_rectangle_corner_radius_metadata():
    outline = BoardOutline(100.0, 80.0, corner_radius_mm=2.5)
    assert outline.corner_radius_mm == 2.5
    assert outline.width_mm == 100.0
    assert outline.height_mm == 80.0


def test_polygon_vertices():
    verts = [(0, 0), (100, 0), (100, 50), (50, 80), (0, 50)]
    outline = BoardOutline(vertices=verts)
    assert outline.width_mm == 100.0
    assert outline.height_mm == 80.0
    assert len(outline.vertices) == 5


def test_empty_outline():
    outline = BoardOutline()
    assert outline.width_mm == 0.0
    assert outline.height_mm == 0.0
    assert outline.vertices == []


def test_vertices_override_dimensions():
    verts = [(10, 20), (210, 20), (210, 170), (10, 170)]
    outline = BoardOutline(50, 50, vertices=verts)
    assert outline.x_min == 10.0
    assert outline.y_min == 20.0
    assert outline.x_max == 210.0
    assert outline.y_max == 170.0
    assert outline.width_mm == 200.0
    assert outline.height_mm == 150.0
