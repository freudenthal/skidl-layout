from __future__ import annotations

import pytest
from simp_sexp import Sexp

from skidl_layout.constraints import BoardCutout, BoardOutline, KeepOut
from skidl_layout.geometry import FootprintGeometry, PadGeometry, footprint_geometry_from_sexp
from skidl_layout.writer import PlacedPart
from skidl_layout.validator import validate, ValidationResult, run_kicad_drc


BBOXES_0805 = {"Resistor_SMD:R_0805": (2.0, 1.25)}


def _test_geometry(
    footprint: str,
    pad_x: float,
    *,
    pad_type: str = "smd",
    layers: tuple[str, ...] = ("F.Cu",),
) -> FootprintGeometry:
    return FootprintGeometry(
        footprint,
        pads=[
            PadGeometry(
                "1",
                pad_x,
                0.0,
                1.0,
                1.0,
                pad_type=pad_type,
                layers=layers,
            )
        ],
        courtyard_bounds=(-5.0, -5.0, 5.0, 5.0),
    )


def _panel_switch_geometry(footprint: str = "Demo:PanelSwitch") -> FootprintGeometry:
    return footprint_geometry_from_sexp(
        footprint,
        Sexp(
            f"""
(footprint "{footprint}"
  (pad "1" thru_hole circle (at 0 0) (size 1 1) (layers "*.Cu" "*.Mask"))
  (fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd"))
  (fp_poly
    (pts (xy -4 -14) (xy 4 -14) (xy 4 14) (xy -4 14))
    (stroke (width 0.1) (type solid))
    (fill none)
    (layer "F.SilkS"))
)
"""
        ),
    )


def test_no_overlaps_when_separated():
    parts = [
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 30.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(parts, None, BBOXES_0805)
    assert result.overlaps == []


def test_overlap_detected():
    parts = [
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 10.5, 10.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(parts, None, BBOXES_0805)
    assert len(result.overlaps) > 0
    pair = result.overlaps[0]
    assert set(pair) == {"R1", "R2"}


def test_visual_body_overlap_counts_when_pads_do_not_overlap():
    parts = [
        PlacedPart("SW1", 20.0, 20.0, 0.0, "Demo:PanelSwitch", side="mechanical"),
        PlacedPart("U1", 28.0, 20.0, 0.0, "Demo:PanelSwitch", side="front"),
    ]
    geometries = {"Demo:PanelSwitch": _panel_switch_geometry()}

    result = validate(parts, None, {}, clearance_mm=0.0, fp_geometries=geometries)

    assert result.overlaps == [("SW1", "U1")]


def test_courtyard_overlap_without_body_or_pad_collision_is_not_hard_overlap():
    parts = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Demo:Module"),
        PlacedPart("C1", 8.0, 20.0, 0.0, "Demo:Cap"),
    ]
    geometries = {
        "Demo:Module": FootprintGeometry(
            "Demo:Module",
            pads=[PadGeometry("1", -8.0, 0.0, 1.0, 1.0)],
            body_bounds=(-6.0, -5.0, 6.0, 5.0),
            courtyard_bounds=(-20.0, -8.0, 20.0, 8.0),
        ),
        "Demo:Cap": FootprintGeometry(
            "Demo:Cap",
            pads=[
                PadGeometry("1", -0.6, 0.0, 0.4, 0.4),
                PadGeometry("2", 0.6, 0.0, 0.4, 0.4),
            ],
            body_bounds=(-1.0, -0.6, 1.0, 0.6),
        ),
    }

    result = validate(parts, None, {}, clearance_mm=0.5, fp_geometries=geometries)

    assert result.overlaps == []


def test_front_and_back_parts_may_share_xy_without_overlap():
    parts = [
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805", side="front"),
        PlacedPart("R2", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805", side="back"),
    ]

    result = validate(parts, None, BBOXES_0805)

    assert result.overlaps == []


def test_back_side_parts_still_conflict_with_other_back_side_parts():
    parts = [
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805", side="back"),
        PlacedPart("R2", 10.5, 10.0, 0.0, "Resistor_SMD:R_0805", side="back"),
    ]

    result = validate(parts, None, BBOXES_0805)

    assert result.overlaps == [("R1", "R2")]


def test_front_and_back_through_hole_bodies_may_overlap_when_pads_clear():
    parts = [
        PlacedPart("J1", 10.0, 10.0, 0.0, "Demo:THTLeft", side="front"),
        PlacedPart("J2", 10.0, 10.0, 0.0, "Demo:THTRight", side="back"),
    ]
    geometries = {
        "Demo:THTLeft": _test_geometry(
            "Demo:THTLeft", -3.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
        "Demo:THTRight": _test_geometry(
            "Demo:THTRight", 3.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
    }

    result = validate(parts, None, {}, clearance_mm=0.5, fp_geometries=geometries)

    assert result.overlaps == []


def test_front_and_back_through_hole_pad_collision_detected():
    parts = [
        PlacedPart("J1", 10.0, 10.0, 0.0, "Demo:THTA", side="front"),
        PlacedPart("J2", 10.0, 10.0, 0.0, "Demo:THTB", side="back"),
    ]
    geometries = {
        "Demo:THTA": _test_geometry(
            "Demo:THTA", 0.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
        "Demo:THTB": _test_geometry(
            "Demo:THTB", 0.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
    }

    result = validate(parts, None, {}, clearance_mm=0.0, fp_geometries=geometries)

    assert result.overlaps == [("J1", "J2")]


def test_back_smd_pad_cannot_share_front_through_hole_pad():
    parts = [
        PlacedPart("J1", 10.0, 10.0, 0.0, "Demo:THT", side="front"),
        PlacedPart("R1", 10.0, 10.0, 0.0, "Demo:SMD", side="back"),
    ]
    geometries = {
        "Demo:THT": _test_geometry(
            "Demo:THT", 0.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
        "Demo:SMD": _test_geometry("Demo:SMD", 0.0, layers=("B.Cu",)),
    }

    result = validate(parts, None, {}, clearance_mm=0.0, fp_geometries=geometries)

    assert result.overlaps == [("J1", "R1")]


def test_cross_side_pad_collision_detected_with_spatial_grid_path():
    parts = [
        PlacedPart("J1", 10.0, 10.0, 0.0, "Demo:THT", side="front"),
        PlacedPart("R1", 10.0, 10.0, 0.0, "Demo:SMD", side="back"),
    ]
    parts.extend(
        PlacedPart(f"F{i}", 100.0 + i * 10.0, 100.0, 0.0, "Demo:SMD", side="front")
        for i in range(18)
    )
    geometries = {
        "Demo:THT": _test_geometry(
            "Demo:THT", 0.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
        "Demo:SMD": _test_geometry("Demo:SMD", 0.0, layers=("B.Cu",)),
    }

    result = validate(parts, None, {}, clearance_mm=0.0, fp_geometries=geometries)

    assert ("J1", "R1") in result.overlaps


def test_mechanical_parts_still_overlap_front_or_back_parts():
    parts = [
        PlacedPart("H1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805", side="mechanical"),
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805", side="back"),
    ]

    result = validate(parts, None, BBOXES_0805)

    assert result.overlaps == [("H1", "R1")]


def test_clearance_boundary():
    # Parts just outside clearance — width 2.0 each, so gap needed = 2.0 + 0.5 = 2.5
    # At x-distance of 2.6 they should be clear
    parts = [
        PlacedPart("R1", 0.0, 0.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 2.6, 0.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(parts, None, BBOXES_0805)
    assert result.overlaps == []

    # At x-distance of 2.4 they should overlap
    parts[1] = PlacedPart("R2", 2.4, 0.0, 0.0, "Resistor_SMD:R_0805")
    result = validate(parts, None, BBOXES_0805)
    assert len(result.overlaps) > 0


def test_ok_property_no_overlaps():
    result = ValidationResult(placed_parts=5, total_parts=5)
    assert result.ok is True


def test_ok_property_with_overlaps():
    result = ValidationResult(placed_parts=5, total_parts=5)
    result.overlaps = [("R1", "R2")]
    assert result.ok is False


def test_ok_property_with_missing():
    result = ValidationResult(placed_parts=4, total_parts=5, missing_refs=["C1"])
    assert result.ok is False


def test_ok_property_with_outline_violations():
    result = ValidationResult(placed_parts=5, total_parts=5, outline_violations=["R1"])
    assert result.ok is False


def test_summary_parts_count():
    result = ValidationResult(placed_parts=10, total_parts=12, missing_refs=["C1", "C2"])
    s = result.summary()
    assert "10/12" in s
    assert "MISSING" in s
    assert "C1" in s


def test_summary_overlaps_loud():
    result = ValidationResult(placed_parts=2, total_parts=2)
    result.overlaps = [("R1", "R2")]
    s = result.summary()
    assert "OVERLAPS" in s
    assert "R1" in s
    assert "R2" in s


def test_summary_no_overlaps_message():
    result = ValidationResult(placed_parts=2, total_parts=2)
    s = result.summary()
    assert "No overlaps" in s


def test_outline_violation_detected():
    outline = BoardOutline(50.0, 50.0)
    parts = [
        PlacedPart("R1", 25.0, 25.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 51.0, 25.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(parts, None, BBOXES_0805, outline=outline)
    assert "R2" in result.outline_violations
    assert "R1" not in result.outline_violations
    assert result.ok is False


def test_outline_violation_negative():
    outline = BoardOutline(50.0, 50.0)
    parts = [PlacedPart("R1", -5.0, 25.0, 0.0, "Resistor_SMD:R_0805")]
    result = validate(parts, None, BBOXES_0805, outline=outline)
    assert "R1" in result.outline_violations


def test_offset_outline_bounds_used_for_validation():
    outline = BoardOutline(
        vertices=[(10.0, 20.0), (60.0, 20.0), (60.0, 70.0), (10.0, 70.0)]
    )
    parts = [
        PlacedPart("R1", 20.0, 30.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 5.0, 30.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(parts, None, BBOXES_0805, outline=outline)
    assert result.outline_violations == ["R2"]


def test_rotated_bbox_used_for_outline_validation():
    outline = BoardOutline(20.0, 10.0)
    parts = [PlacedPart("J1", 10.0, 8.25, 90.0, "Connector:PinHeader_1x06")]

    result = validate(
        parts,
        None,
        {"Connector:PinHeader_1x06": (2.5, 15.0)},
        outline=outline,
    )

    assert result.outline_violations == []


def test_rotated_large_physical_body_extending_past_outline_is_violation():
    outline = BoardOutline(100.0, 30.0)
    parts = [
        PlacedPart(
            "SW1",
            90.0,
            15.0,
            90.0,
            "Demo:PanelSwitch",
            side="mechanical",
        )
    ]
    geometries = {"Demo:PanelSwitch": _panel_switch_geometry()}

    result = validate(parts, None, {}, outline=outline, fp_geometries=geometries)

    assert result.outline_violations == ["SW1"]


def test_no_outline_no_violations():
    parts = [PlacedPart("R1", 999.0, 999.0, 0.0, "Resistor_SMD:R_0805")]
    result = validate(parts, None, BBOXES_0805)
    assert result.outline_violations == []


def test_outline_violation_in_summary():
    result = ValidationResult(placed_parts=2, total_parts=2, outline_violations=["R2"])
    s = result.summary()
    assert "OUTSIDE OUTLINE" in s
    assert "R2" in s


def test_keepout_violation_detected_and_fails_validation():
    parts = [
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 30.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(
        parts,
        None,
        BBOXES_0805,
        keepouts=[KeepOut(8.0, 8.0, 12.0, 12.0)],
    )

    assert result.keepout_violations == ["R1"]
    assert result.ok is False
    assert "INSIDE KEEPOUT" in result.summary()


def test_keepout_allows_named_source_ref_only():
    parts = [
        PlacedPart("H1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(
        parts,
        None,
        BBOXES_0805,
        keepouts=[KeepOut(8.0, 8.0, 12.0, 12.0, allowed_refs=["H1"])],
    )

    assert result.keepout_violations == ["R1"]


def test_cutout_violation_is_distinct_from_keepout():
    parts = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("U2", 30.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
    ]

    result = validate(
        parts,
        None,
        BBOXES_0805,
        cutouts=[
            BoardCutout(
                x_min=8.0,
                y_min=8.0,
                x_max=12.0,
                y_max=12.0,
                name="sensor_window",
            )
        ],
    )

    assert result.cutout_violations == ["U1"]
    assert result.keepout_violations == []
    assert result.ok is False
    assert "INTERSECTS CUTOUT" in result.summary()


def test_polygon_outline_containment_checks_corners():
    outline = BoardOutline(
        vertices=[
            (0.0, 0.0),
            (50.0, 0.0),
            (50.0, 50.0),
            (25.0, 35.0),
            (0.0, 50.0),
        ]
    )
    parts = [
        PlacedPart("R1", 10.0, 10.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 25.0, 45.0, 0.0, "Resistor_SMD:R_0805"),
    ]

    result = validate(parts, None, BBOXES_0805, outline=outline)

    assert result.outline_violations == ["R2"]


def test_validate_with_none_circuit():
    parts = [PlacedPart("R1", 10.0, 10.0, 0.0, "R_0805")]
    result = validate(parts, None, {})
    assert result.placed_parts == 1
    assert result.total_parts == 0
    assert result.missing_refs == []
    assert result.extra_refs == []
    assert result.worst_hpwl_nets == []


def test_unknown_footprint_uses_default_bbox():
    # Parts with unknown footprint fall back to 2.0×2.0 default
    parts = [
        PlacedPart("U1", 0.0, 0.0, 0.0, "Unknown:Part"),
        PlacedPart("U2", 0.5, 0.0, 0.0, "Unknown:Part"),
    ]
    result = validate(parts, None, {})
    # With default 2.0×2.0 and clearance 0.5, these must overlap
    assert len(result.overlaps) > 0


def test_run_kicad_drc_missing_binary():
    # kicad-cli almost certainly not installed in CI
    passed, report = run_kicad_drc("/nonexistent/board.kicad_pcb")
    # Either it's not installed (passed=True, "not available") or returncode != 0
    assert isinstance(passed, bool)
    assert isinstance(report, str)


def test_multiple_overlapping_pairs():
    # Three parts all at the same spot — should produce 3 pairs
    parts = [
        PlacedPart("R1", 0.0, 0.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R2", 0.0, 0.0, 0.0, "Resistor_SMD:R_0805"),
        PlacedPart("R3", 0.0, 0.0, 0.0, "Resistor_SMD:R_0805"),
    ]
    result = validate(parts, None, BBOXES_0805)
    assert len(result.overlaps) == 3
