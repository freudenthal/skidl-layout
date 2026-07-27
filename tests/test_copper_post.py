"""Unit tests for the exposed-pad thermal via array (power-layout Phase 5, WS-3).

No KiCad and no KRT: the array is planned and spliced against synthetic board
text, so what is under test is the geometry, the refusal, and the determinism.
"""

from __future__ import annotations

import dataclasses
import math
import re

import pytest

from skidl_layout import OSHPARK_2L
from skidl_layout.copper_post import (
    find_exposed_pad,
    plan_thermal_vias,
    splice_vias,
)

VIA_IN_PAD_SPEC = dataclasses.replace(
    OSHPARK_2L, via_in_pad=True, name="test-via-in-pad")


def _board(fp_at="15.5 14.0", pad_angle="", ep="1.68 1.88", vias=True):
    """A board with one controller footprint whose pad 11 is the exposed pad."""
    via_block = """
\t(via
\t\t(at 3.000000 3.000000)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net 1)
\t\t(uuid "aaaaaaaa-1111-2222-3333-444444444444")
\t)""" if vias else ""
    return f"""(kicad_pcb
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "SW")
\t(footprint "MSOP-10-1EP"
\t\t(at {fp_at})
\t\t(property "Reference" "U1")
\t\t(pad "1" smd roundrect (at -1.0 -0.5{pad_angle}) (size 0.4 0.25)
\t\t\t(layers "F.Cu" "F.Mask" "F.Paste") (net 2 "SW"))
\t\t(pad "11" smd rect (at 0 0{pad_angle}) (size {ep})
\t\t\t(layers "F.Cu" "F.Mask") (net 1 "GND"))
\t\t(pad "11" smd rect (at 0 0{pad_angle}) (size 3.0 3.0)
\t\t\t(layers "F.Paste") (net 1 "GND"))
\t){via_block}
)
"""


# --- finding the pad -------------------------------------------------------

def test_find_exposed_pad_picks_the_largest_ground_pad(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    pad = find_exposed_pad(str(path), "U1", "GND")
    assert pad["number"] == "11"
    assert pad["w"] == pytest.approx(1.68)
    assert pad["h"] == pytest.approx(1.88)
    assert (pad["x"], pad["y"]) == (pytest.approx(15.5), pytest.approx(14.0))


def test_find_exposed_pad_ignores_paste_only_apertures(tmp_path):
    # The 3x3 F.Paste aperture is bigger than the copper pad; picking it would
    # place vias in a stencil opening with no copper under them.
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    pad = find_exposed_pad(str(path), "U1", "GND")
    assert pad["w"] == pytest.approx(1.68)
    assert "F.Cu" in pad["layers"]


def test_find_exposed_pad_returns_none_when_the_net_is_absent(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    assert find_exposed_pad(str(path), "U1", "EARTH") is None
    assert find_exposed_pad(str(path), "U9", "GND") is None


# --- the refusal (the shipped-spec path) -----------------------------------

def test_refuses_on_a_spec_that_forbids_via_in_pad(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    plan = plan_thermal_vias(str(path), "U1", "GND", OSHPARK_2L)
    assert plan.refused is True
    assert plan.count == 0
    assert "via_in_pad" in plan.reason
    assert OSHPARK_2L.name in plan.reason


def test_refuses_without_a_spec_at_all(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    plan = plan_thermal_vias(str(path), "U1", "GND", None)
    assert plan.refused is True and plan.count == 0


def test_refusal_never_raises_on_a_missing_pad(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    plan = plan_thermal_vias(str(path), "U1", "NOPE", VIA_IN_PAD_SPEC)
    assert plan.count == 0 and plan.refused is False and plan.reason


# --- the array -------------------------------------------------------------

def test_array_on_the_measured_lt3757_pad_is_two_by_two(tmp_path):
    # 1.68 x 1.88 mm pad, 0.6 mm vias, 0.25 mm clearance -> 0.85 mm pitch.
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    plan = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC)
    assert plan.shape == (2, 2)
    assert plan.count == 4
    assert plan.pitch_mm == pytest.approx(0.85)
    assert plan.net == "GND"


def test_every_via_lands_fully_inside_the_pad(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    plan = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC)
    half = VIA_IN_PAD_SPEC.via_size_mm / 2.0
    for x, y in plan.positions:
        assert abs(x - plan.pad["x"]) + half <= plan.pad["w"] / 2.0 + 1e-9
        assert abs(y - plan.pad["y"]) + half <= plan.pad["h"] / 2.0 + 1e-9
    assert plan.edge_margin_mm > 0


def test_the_array_is_centred_on_the_pad(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    plan = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC)
    cx = sum(p[0] for p in plan.positions) / plan.count
    cy = sum(p[1] for p in plan.positions) / plan.count
    assert cx == pytest.approx(plan.pad["x"])
    assert cy == pytest.approx(plan.pad["y"])


def test_a_pad_edge_margin_shrinks_the_array(tmp_path):
    # Demanding the annular ring as pad-edge margin too costs a column on this
    # pad -- the measured trade-off, pinned so it cannot change silently.
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board())
    plan = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC,
                             edge_margin_mm=0.127)
    assert plan.shape == (1, 2)
    assert plan.count == 2


def test_a_tiny_pad_takes_a_single_via(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board(ep="0.8 0.8"))
    plan = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC)
    assert plan.count == 1


def test_a_pad_smaller_than_a_via_places_nothing(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board(ep="0.4 0.4"))
    plan = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC)
    assert plan.count == 0
    assert "does not fit" in plan.reason


def test_max_shape_caps_the_grid(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board(ep="4.0 4.0"))
    full = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC)
    capped = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC,
                               max_shape=(2, 1))
    assert full.count > capped.count
    assert capped.shape == (2, 1)


def test_a_rotated_footprint_rotates_the_array(tmp_path):
    # A 1.0 x 3.0 pad admits one column and three rows, so the array is a line
    # -- which makes the rotation visible. A square array would rotate onto
    # itself and prove nothing.
    upright = tmp_path / "up.kicad_pcb"
    upright.write_text(_board(ep="1.0 3.0"))
    turned = tmp_path / "turned.kicad_pcb"
    turned.write_text(_board(fp_at="15.5 14.0 90", pad_angle=" 90", ep="1.0 3.0"))

    a = plan_thermal_vias(str(upright), "U1", "GND", VIA_IN_PAD_SPEC)
    b = plan_thermal_vias(str(turned), "U1", "GND", VIA_IN_PAD_SPEC)
    assert a.shape == b.shape == (1, 3)

    # Upright: the line runs along board Y. Turned 90 degrees: along board X.
    assert len({round(p[0], 4) for p in a.positions}) == 1
    assert len({round(p[1], 4) for p in a.positions}) == 3
    assert len({round(p[0], 4) for p in b.positions}) == 3
    assert len({round(p[1], 4) for p in b.positions}) == 1
    xs = sorted(round(p[0], 4) for p in b.positions)
    assert math.isclose(xs[1] - xs[0], b.pitch_mm, abs_tol=1e-6)


def test_pitch_override_is_honoured(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_board(ep="4.0 4.0"))
    wide = plan_thermal_vias(str(path), "U1", "GND", VIA_IN_PAD_SPEC,
                             pitch_mm=1.5)
    assert wide.pitch_mm == pytest.approx(1.5)
    assert wide.count < plan_thermal_vias(
        str(path), "U1", "GND", VIA_IN_PAD_SPEC).count


# --- splicing --------------------------------------------------------------

def test_splice_writes_the_vias_with_the_ground_net_id(tmp_path):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board())
    dst = tmp_path / "out.kicad_pcb"
    plan = plan_thermal_vias(str(src), "U1", "GND", VIA_IN_PAD_SPEC)
    assert splice_vias(str(src), str(dst), plan) == 4
    text = dst.read_text()
    assert len(re.findall(r"\(via\b", text)) == 5   # one template + four ours
    for x, y in plan.positions:
        assert f"(at {x:.6f} {y:.6f})" in text
    # Ours carry the GND net id (1), mirrored from the template's shape.
    assert text.count('(layers "F.Cu" "B.Cu")') == 5


def test_splice_uuids_are_deterministic(tmp_path):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board())
    plan = plan_thermal_vias(str(src), "U1", "GND", VIA_IN_PAD_SPEC)
    first, second = tmp_path / "a.kicad_pcb", tmp_path / "c.kicad_pcb"
    splice_vias(str(src), str(first), plan)
    splice_vias(str(src), str(second), plan)
    assert first.read_text() == second.read_text()
    # ...and they are not the template's uuid.
    ids = set(re.findall(r'\(uuid "([^"]+)"\)', second.read_text()))
    assert len(ids) == 5


def test_splice_with_nothing_to_place_copies_the_board(tmp_path):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board())
    dst = tmp_path / "out.kicad_pcb"
    plan = plan_thermal_vias(str(src), "U1", "GND", OSHPARK_2L)  # refused
    assert splice_vias(str(src), str(dst), plan) == 0
    assert dst.read_text() == src.read_text()


def test_splice_refuses_to_guess_a_via_shape(tmp_path):
    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board(vias=False))
    plan = plan_thermal_vias(str(src), "U1", "GND", VIA_IN_PAD_SPEC)
    with pytest.raises(ValueError, match="no \\(via"):
        splice_vias(str(src), str(tmp_path / "out.kicad_pcb"), plan)


def test_spliced_vias_are_seen_as_via_in_pad_by_fab_check(tmp_path):
    # The two halves of Phase 5 meeting: WS-3 can produce the board WS-4 must
    # refuse to bless.
    from skidl_layout import fab_check

    src = tmp_path / "b.kicad_pcb"
    src.write_text(_board())
    dst = tmp_path / "out.kicad_pcb"
    plan = plan_thermal_vias(str(src), "U1", "GND", VIA_IN_PAD_SPEC)
    splice_vias(str(src), str(dst), plan)
    flagged = fab_check(str(dst), OSHPARK_2L, run_drc=False)
    hits = [v for v in flagged.violations if v.rule == "via_in_pad"]
    assert len(hits) == 4
    allowed = fab_check(str(dst), VIA_IN_PAD_SPEC, run_drc=False)
    assert not [v for v in allowed.violations if v.rule == "via_in_pad"]
