# -*- coding: utf-8 -*-
"""The geometry-only renderer -- construction-arc stage **S5C**, gate ``AC6``.

⭐⭐⭐ **Roadmap next-steps item 10, and it is a LEAF-GUARD test as much as a
renderer test.** The scene/SVG/PNG emitter was duplicated across two canary
drivers (194 + 185 near-identical lines); the promotion is only safe if the new
module imports **nothing** from this package -- ``construct.py`` is guarded by
an import-aware test and by ``ESCAPE_MAP_CONSUMERS``, whose companion test
requires every permitted consumer to be a leaf too.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from skidl_layout.scene_render import (CELL_STYLE, DEFAULT_STYLE, SIDE_STYLE,
                                       SceneError, resolve_style,
                                       scene_to_png, scene_to_svg)

BOXES = [("U1", "anchor", (20.0, 20.0, 30.0, 30.0), (21.0, 21.0, 29.0, 29.0)),
         ("R1", "neighbour", (33.1, 24.0, 35.2, 26.7),
          (33.3, 24.2, 35.0, 26.5)),
         ("C9", "ring2", (36.0, 31.5, 38.4, 33.9), (36.2, 31.7, 38.2, 33.7))]
PADS = [("U1", "1", 21.5, 22.5, 0.6, 1.2), ("R1", "2", 34.9, 25.35, 0.9, 1.1)]
LINES = [(21.5, 22.5, 34.9, 25.35, "VIN 5.0")]


def _scene(**over) -> dict:
    scene = {"board": "b", "anchor": "U1", "title": "b U1 tightened",
             "boxes": list(BOXES), "pads": list(PADS), "lines": list(LINES),
             "extent": (18.0, 18.0, 40.4, 35.9), "caption": "cap <tion> & co",
             "legend": "dashed = courtyard"}
    scene.update(over)
    return scene


# --------------------------------------------------------------------------- #
# ⛔ The leaf property -- the reason this module exists in this shape
# --------------------------------------------------------------------------- #
def test_scene_render_imports_nothing_from_this_package():
    """⛔⛔ **The strongest form of the leaf rule: it imports nothing of ours.**

    A renderer inside ``skidl_layout`` that imported ``construct`` would break
    ``test_construct.py``'s guard *and* ``ESCAPE_MAP_CONSUMERS``' companion
    (which requires every permitted consumer of the escape map to be a leaf
    itself). One that takes plain boxes, pads, lines, segments and vias cannot.
    """
    path = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "scene_render.py")
    text = path.read_text(encoding="utf-8")
    offenders = [line.strip() for line in text.splitlines()
                 if re.match(r"^[ \t]*(from[ \t]+\.|from[ \t]+skidl_layout|"
                             r"import[ \t]+skidl_layout)", line)]
    assert offenders == [], f"scene_render imports {offenders}"


def test_scene_render_knows_nothing_about_the_construction_loop():
    """⛔ It must never learn what a ``CellResult`` or a ``Unit`` is.

    ⚠ **Asserted over the parsed CODE, not over the file's text**, and that is
    the same correction its two sibling guards already carry: a docstring that
    says *"this module does not know what a ``CellResult`` is"* is documentation,
    and this arc wants more of it, not less. A substring scan would forbid the
    sentence that states the property.
    """
    import ast

    path = (pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
            / "scene_render.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree)
              if isinstance(node, ast.Attribute)}
    names |= {node.name for node in ast.walk(tree)
              if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    for symbol in ("CellResult", "BoardResult", "Neighbour", "Placement",
                   "SideResult", "UnitPort", "Unit"):
        assert symbol not in names, f"scene_render uses {symbol} in code"


def test_the_engine_never_re_exports_the_renderer():
    import skidl_layout

    assert not hasattr(skidl_layout, "scene_to_svg")
    assert not hasattr(skidl_layout, "scene_to_png")


# --------------------------------------------------------------------------- #
# ⛔ Standing finding 1 -- an instrument that draws nothing must RAISE
# --------------------------------------------------------------------------- #
def test_a_scene_with_no_box_raises():
    with pytest.raises(SceneError):
        scene_to_svg(_scene(boxes=[]))


def test_a_scene_with_a_degenerate_extent_raises():
    with pytest.raises(SceneError):
        scene_to_svg(_scene(extent=(0.0, 0.0, 0.0, 10.0)))
    with pytest.raises(SceneError):
        scene_to_svg(_scene(extent=(0.0, 0.0, 10.0)))


def test_an_undeclared_style_key_raises_rather_than_being_ignored():
    """⛔ A silently ignored style key is a picture that does not match the
    caller's intent, and nothing downstream would say so."""
    with pytest.raises(SceneError):
        scene_to_svg(_scene(style={"scale": 9.0, "wobble": 3}))


# --------------------------------------------------------------------------- #
# The two shipped styles
# --------------------------------------------------------------------------- #
def test_the_two_styles_differ_only_in_declared_keys():
    assert set(SIDE_STYLE) == set(DEFAULT_STYLE) == set(CELL_STYLE)
    assert CELL_STYLE == DEFAULT_STYLE
    differing = sorted(k for k in DEFAULT_STYLE
                       if SIDE_STYLE[k] != DEFAULT_STYLE[k])
    assert differing == ["line_dy", "line_font", "line_width", "pad_dy",
                         "pad_font", "png_line_dx", "png_line_dy",
                         "png_line_width", "png_pad_numbers", "png_ref_dy",
                         "png_scale_factor", "ref_font", "scale"]


def test_resolve_style_never_mutates_the_declared_defaults():
    before = dict(DEFAULT_STYLE)
    resolved = resolve_style(_scene(style={"scale": 2.0,
                                           "fills": {"anchor": "#000"}}))
    assert resolved["scale"] == 2.0
    assert resolved["fills"]["anchor"] == "#000"
    # ⭐ The other two kinds survive the partial override.
    assert resolved["fills"]["ring2"] == DEFAULT_STYLE["fills"]["ring2"]
    assert DEFAULT_STYLE == before


# --------------------------------------------------------------------------- #
# The scene's own vocabulary
# --------------------------------------------------------------------------- #
def test_every_element_class_is_emitted_and_counted():
    svg = scene_to_svg(_scene(
        segments=[(21.0, 22.0, 34.0, 25.0, 0.3, "F.Cu")],
        vias=[(30.0, 28.0, 0.6)], labels=[(25.0, 19.0, "R = 21.55 mm")]))
    assert svg.count('class="part"') == len(BOXES)
    assert svg.count('class="courtyard"') == len(BOXES)
    assert svg.count('class="pair"') == len(LINES)
    assert svg.count('class="segment"') == 1
    assert svg.count('class="via"') == 1
    assert svg.count('class="note"') == 1


def test_copper_is_drawn_BENEATH_the_part_boxes():
    """⭐ So the pair line and the part outlines stay legible on top."""
    svg = scene_to_svg(_scene(segments=[(21.0, 22.0, 34.0, 25.0, 0.3, "F.Cu")],
                              vias=[(30.0, 28.0, 0.6)]))
    assert svg.index('class="segment"') < svg.index('class="part"')
    assert svg.index('class="via"') < svg.index('class="part"')
    assert svg.index('class="part"') < svg.index('class="pair"')


def test_a_scene_with_no_copper_emits_no_copper_element():
    """⛔ **The OFF arm's byte-identity depends on exactly this.** A recorded
    S3/S4/S5/S5B render carries no ``segment``/``via``/``note`` element, so a
    renderer that emitted an empty one would move 21 recorded pictures."""
    svg = scene_to_svg(_scene())
    assert "class=\"segment\"" not in svg
    assert "class=\"via\"" not in svg
    assert "class=\"note\"" not in svg


def test_the_caption_and_the_title_are_escaped():
    svg = scene_to_svg(_scene())
    assert "cap &lt;tion&gt; &amp; co" in svg
    assert "<title>b U1 tightened</title>" in svg


def test_a_line_may_carry_a_separate_png_label():
    """⚠ S3's SVG says ``5.0 mm / 48 it`` and its PNG says ``5.0mm/48it``; the
    scene carries both rather than the emitter knowing which driver it serves."""
    svg = scene_to_svg(_scene(lines=[(1.0, 2.0, 3.0, 4.0, "svg text",
                                      "png text")]))
    assert "svg text" in svg and "png text" not in svg


def test_the_png_emitter_reads_the_same_scene(tmp_path):
    pytest.importorskip("PIL")
    path = tmp_path / "out.png"
    assert scene_to_png(_scene(segments=[(21.0, 22.0, 34.0, 25.0, 0.3,
                                          "F.Cu")]), str(path)) == str(path)
    assert path.exists() and path.stat().st_size > 0


def test_the_png_emitter_also_refuses_an_empty_scene(tmp_path):
    with pytest.raises(SceneError):
        scene_to_png(_scene(boxes=[]), str(tmp_path / "out.png"))


# --------------------------------------------------------------------------- #
# S7 -- the board style. ⛔ A STYLE, never a second renderer.
# --------------------------------------------------------------------------- #
def test_the_default_box_fill_opacity_is_the_value_the_emitter_always_wrote():
    """⛔ Byte-identity of every recorded S3/S4/S5/S5B render depends on this
    exact literal."""
    from skidl_layout.scene_render import CELL_STYLE, DEFAULT_STYLE

    assert DEFAULT_STYLE["box_fill_opacity"] == 0.6
    assert DEFAULT_STYLE["png_box_fill"] is True
    assert CELL_STYLE["box_fill_opacity"] == 0.6


def test_the_board_style_differs_from_the_cell_style_in_TWO_keys_only():
    """⭐ The promotion's property: the difference between two pictures is
    DATA. A third key here would be a fork wearing a dict's clothes."""
    from skidl_layout.scene_render import BOARD_STYLE, CELL_STYLE

    differ = {key for key in set(BOARD_STYLE) | set(CELL_STYLE)
              if BOARD_STYLE.get(key) != CELL_STYLE.get(key)}
    assert differ == {"box_fill_opacity", "png_box_fill"}, differ
    assert BOARD_STYLE["box_fill_opacity"] < CELL_STYLE["box_fill_opacity"]


def test_the_cell_style_still_writes_the_literal_0_point_6():
    """⛔ Not "some number": the recorded SVGs contain this substring."""
    from skidl_layout.scene_render import CELL_STYLE, scene_to_svg

    scene = {"boxes": [("R1", "anchor", (0.0, 0.0, 2.0, 1.0),
                        (0.2, 0.2, 1.8, 0.8))],
             "pads": [], "lines": [], "segments": [], "vias": [],
             "labels": [], "extent": (0.0, 0.0, 2.0, 1.0),
             "style": CELL_STYLE, "title": "t", "caption": "c", "legend": "l"}
    assert 'fill-opacity="0.6"' in scene_to_svg(scene)


def test_the_board_style_leaves_the_copper_visible_under_a_big_box():
    """⛔⛔ The defect this key exists for: at L2 the anchor unit's box covers
    most of the board and hid 268 of 268 routed segments."""
    from skidl_layout.scene_render import BOARD_STYLE, scene_to_svg

    scene = {"boxes": [("U1", "anchor", (0.0, 0.0, 40.0, 40.0),
                        (0.5, 0.5, 39.5, 39.5))],
             "pads": [], "lines": [],
             "segments": [(1.0, 1.0, 30.0, 30.0, 0.3, "F.Cu")],
             "vias": [], "labels": [], "extent": (0.0, 0.0, 40.0, 40.0),
             "style": BOARD_STYLE, "title": "t", "caption": "c", "legend": "l"}
    svg = scene_to_svg(scene)
    assert 'fill-opacity="0.1"' in svg
    assert svg.count('class="segment"') == 1
