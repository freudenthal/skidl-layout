"""Phase 11: the pad OUTLINE, and what actually limits a net's spacing.

Two changes to :mod:`skidl_layout.fabspec`'s judge, both forced by measurement:

* **Pad corners are modelled.** Phase 8 and Phase 10 measured to a pad's
  bounding *rectangle*. Almost every modern KiCad footprint uses ``roundrect``
  pads, whose copper is absent from the corner, so a track passing a corner read
  closer than it really was -- a phantom "SHORT". Measured on ``lt3724_buck``: a
  0.1524 mm ``VCC`` track past the ``SGND`` pad ``CC.2`` (0805,
  ``roundrect_rratio 0.25``) read **0.0634 mm** against the sharp corner and
  **0.1670 mm** against the real outline, while KRT's own ``check_drc`` -- which
  models the roundrect -- reported no violation at the 0.1524 mm fab floor. The
  judge was wrong, not the router.

* **The limiting pair is named.** A bare "SHORT" is not actionable. Phase 10
  reported ``lt3758_flyback``'s 72 V ``VIN`` stuck at 0.2000 mm and attributed
  it to a hole in the clearance map; the limiting pair is in fact ``U1.9`` and
  ``U1.10``, **adjacent pins of an MSOP-10 at 0.5 mm pitch**. No router, no
  placer and no clearance map can widen a package's pin pitch.
"""

from __future__ import annotations

import pytest

from skidl_layout.fabspec import (
    DEFAULT_ROUNDRECT_RRATIO,
    _pad_as_capsule,
    _pad_corner_radius,
    _point_in_pad,
    measure_voltage_spacing,
)


# --- boards ----------------------------------------------------------------

def _pad_board(shape: str, gap_to_corner: float, rratio: float = 0.25) -> str:
    """A 1 x 1 mm pad of ``shape`` at (10, 10) plus a zero-width diagonal HV
    track passing ``gap_to_corner`` mm from the pad's bottom-right *bounding*
    corner (10.5, 10.5), measured along the corner diagonal."""
    rr = "\n\t\t\t(roundrect_rratio %s)" % rratio if shape == "roundrect" else ""
    d = gap_to_corner / (2 ** 0.5)
    cx, cy = 10.5 + d, 10.5 + d
    return (
        '(kicad_pcb\n'
        '\t(net 0 "")\n'
        '\t(net 1 "HV")\n'
        '\t(net 2 "GND")\n'
        '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.05))\n'
        '\t(footprint "P"\n'
        '\t\t(at 10 10)\n'
        '\t\t(layer "F.Cu")\n'
        '\t\t(property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))\n'
        '\t\t(pad "1" smd %s\n'
        '\t\t\t(at 0 0)\n'
        '\t\t\t(size 1 1)\n'
        '\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")%s\n'
        '\t\t\t(net 2 "GND")))\n'
        '\t(segment (start %.6f %.6f) (end %.6f %.6f)\n'
        '\t\t(width 0.0) (layer "F.Cu") (net 1))\n'
        ')\n'
    ) % (shape, rr, cx - 2, cy + 2, cx + 2, cy - 2)


def _two_pad_board(pitch: float, same_footprint: bool) -> str:
    """Two 0.3 x 1 mm rect pads ``pitch`` mm apart, in one footprint or two."""
    pad_hv = '(pad "1" smd rect (at 0 0) (size 0.3 1)\n' \
             '\t\t\t(layers "F.Cu" "F.Mask") (net 1 "HV"))'
    if same_footprint:
        body = (
            '\t(footprint "U"\n'
            '\t\t(at 10 10)\n'
            '\t\t(layer "F.Cu")\n'
            '\t\t(property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t%s\n'
            '\t\t(pad "2" smd rect (at %s 0) (size 0.3 1)\n'
            '\t\t\t(layers "F.Cu" "F.Mask") (net 2 "GND")))'
        ) % (pad_hv, pitch)
    else:
        body = (
            '\t(footprint "R"\n'
            '\t\t(at 10 10)\n'
            '\t\t(layer "F.Cu")\n'
            '\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t%s\n'
            '\t(footprint "R"\n'
            '\t\t(at %s 10)\n'
            '\t\t(layer "F.Cu")\n'
            '\t\t(property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 0.3 1)\n'
            '\t\t\t(layers "F.Cu" "F.Mask") (net 2 "GND")))'
        ) % (pad_hv, 10 + pitch)
    return (
        '(kicad_pcb\n'
        '\t(net 0 "")\n'
        '\t(net 1 "HV")\n'
        '\t(net 2 "GND")\n'
        '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.05))\n'
        '%s\n'
        ')\n'
    ) % body


def _two_track_board(gap_mm: float) -> str:
    y2 = 10.0 + 0.2 + gap_mm
    return (
        '(kicad_pcb\n'
        '\t(net 0 "")\n'
        '\t(net 1 "HV")\n'
        '\t(net 2 "GND")\n'
        '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.05))\n'
        '\t(segment (start 5 10.000000) (end 25 10.000000) (width 0.2)\n'
        '\t\t(layer "F.Cu") (net 1))\n'
        '\t(segment (start 5 %.6f) (end 25 %.6f) (width 0.2)\n'
        '\t\t(layer "F.Cu") (net 2))\n'
        ')\n'
    ) % (y2, y2)


# --- the pad outline -------------------------------------------------------

def test_a_rect_pad_is_measured_exactly_as_before(tmp_path):
    """The fix must be inert on a true rectangle. That is what keeps every
    Phase-8 / Phase-10 number taken on rect pads reproducible."""
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_pad_board("rect", 0.30))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["measured_mm"] == pytest.approx(0.30, abs=1e-3)


def test_a_roundrect_pad_leaves_more_gap_than_its_bounding_corner(tmp_path):
    """Same geometry, same 0.30 mm to the sharp corner -- but the copper is not
    there. r = 0.25 x min(w, h) = 0.25, the arc centre sits 0.25*sqrt(2) inside
    the corner along the diagonal, so the true gap is 0.30 + 0.25*sqrt(2) - 0.25.
    """
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_pad_board("roundrect", 0.30))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["measured_mm"] == pytest.approx(
        0.30 + 0.25 * (2 ** 0.5) - 0.25, abs=1e-3)
    assert row["measured_mm"] > 0.30


def test_an_oval_pad_is_a_stadium_not_a_rectangle(tmp_path):
    """oval/circle are the limit case: r = min(w, h)/2. On a square pad that is
    a circle inscribed in the 1 x 1 bounding box."""
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_pad_board("oval", 0.30))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["measured_mm"] == pytest.approx(
        0.30 + 0.5 * (2 ** 0.5) - 0.5, abs=1e-3)


def test_a_custom_pad_stays_a_conservative_rectangle():
    """Its primitives are not parsed, so a radius would be invented geometry.
    Conservative is the right default when the shape is genuinely unknown."""
    assert _pad_corner_radius(
        {"shape": "custom", "w": 1.0, "h": 1.0, "rratio": None}) == 0.0


def test_a_roundrect_with_no_rratio_uses_kicads_own_default():
    r = _pad_corner_radius({"shape": "roundrect", "w": 2.0, "h": 1.0,
                            "rratio": None})
    assert r == pytest.approx(DEFAULT_ROUNDRECT_RRATIO * 1.0)


def test_the_radius_is_taken_off_the_short_side():
    """KiCad's rratio multiplies min(w, h): a long thin pad's corner radius is
    bounded by its width, never by its length."""
    assert _pad_corner_radius({"shape": "roundrect", "w": 4.0, "h": 0.4,
                               "rratio": 0.25}) == pytest.approx(0.1)


def test_the_capsule_insets_the_rectangle_by_the_radius():
    inset, r = _pad_as_capsule({"shape": "roundrect", "w": 1.0, "h": 1.45,
                                "rratio": 0.25, "x": 0.0, "y": 0.0,
                                "angle": 0.0})
    assert r == pytest.approx(0.25)
    assert (inset["w"], inset["h"]) == pytest.approx((0.5, 0.95))


def test_a_rect_pad_capsule_is_the_pad_itself():
    pad = {"shape": "rect", "w": 1.0, "h": 1.0, "rratio": None,
           "x": 0.0, "y": 0.0, "angle": 0.0}
    inset, r = _pad_as_capsule(pad)
    assert r == 0.0 and inset is pad


def test_via_in_pad_containment_still_uses_the_bounding_rectangle():
    """⛔ The asymmetry is deliberate and must not be 'tidied up'. For a
    CONTAINMENT test -- ``via_relocate`` asking "is this via in the pad" -- an
    over-large pad can only over-report, which is the safe direction and is the
    model Phase 8's measured via-in-pad counts were taken with. For a SPACING
    test the same conservatism is exactly backwards. Two questions, two models.
    """
    pad = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0, "angle": 0.0,
           "shape": "roundrect", "rratio": 0.25}
    # Dead in the rounded-off corner: outside the copper, inside the rectangle.
    assert _point_in_pad(0.49, 0.49, pad) is True


# --- what limits a net, and who could move it ------------------------------

def test_a_track_to_track_gap_is_reported_as_routable(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_track_board(0.5))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["limiting_pair"] == "track<->track"
    assert row["placement_bound"] is False
    assert row["same_footprint"] is False


def test_two_pins_of_one_package_are_named_as_unreachable(tmp_path):
    """The finding this field exists for. ``lt3758_flyback``'s 72 V ``VIN`` sits
    at 0.2000 mm to ``UVLO`` and no clearance setting moves it, because the pair
    is ``U1.9``/``U1.10`` -- adjacent pins of an MSOP-10 at 0.5 mm pitch. The
    pitch is smaller than the 0.6 mm column B2 asks for, so **no layout of that
    package can comply**. That is a schematic decision (package, pin assignment
    or coating), not a layout one, and the judge must say so."""
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_pad_board(0.5, same_footprint=True))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["measured_mm"] == pytest.approx(0.2, abs=1e-3)
    assert row["meets_requirement"] is False
    assert row["limiting_pair"] == "pad<->pad"
    assert row["placement_bound"] is True
    assert row["same_footprint"] is True
    assert "U1.1" in row["limiting_objects"]
    assert "U1.2" in row["limiting_objects"]


def test_two_pads_of_different_parts_are_placement_bound_but_not_frozen(tmp_path):
    """Routing cannot fix a pad-to-pad gap, but placement can pull two parts
    apart -- a materially different verdict from the same-package case, which
    is why the two are separate fields."""
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_pad_board(0.5, same_footprint=False))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["placement_bound"] is True
    assert row["same_footprint"] is False


def test_a_conformal_coat_is_what_actually_closes_the_package_case(tmp_path):
    """Column B4 asks 0.13 mm at 72 V, which a 0.5 mm-pitch package does give.
    Measured across all four HV boards with the lever on: **B2 leaves 7 nets
    short, B4 leaves none**. The column is a manufacturing decision this stack
    already exposes (``spacing_column=``), and it is the honest answer to the
    package case -- unlike widening a clearance the router cannot apply."""
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_pad_board(0.5, same_footprint=True))

    assert measure_voltage_spacing(
        str(path), {"HV": 72.0}, column="B2")[0]["meets_requirement"] is False
    assert measure_voltage_spacing(
        str(path), {"HV": 72.0}, column="B4")[0]["meets_requirement"] is True
