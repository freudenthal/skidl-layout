from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from skidl_layout.constraints import (
    AnchorZone,
    AlignConstraint,
    BoardOutline,
    DistributeConstraint,
    EdgeAnchor,
    FaceEdgeConstraint,
    FarConstraint,
    FixedPosition,
    KeepOut,
    LayoutConstraints,
    NearConstraint,
)
from skidl_layout.hierarchy import PlacementGroup
from skidl_layout.geometry import FootprintGeometry
from skidl_layout.writer import PlacedPart
from skidl_layout.placer import place_parts, _overlaps, derive_outline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_net(name: str):
    net = MagicMock()
    net.name = name
    return net


def _make_mock_part(ref, value="10k", footprint="Resistor_SMD:R_0805_2012Metric", num_pins=2, pin_nets=None):
    part = MagicMock()
    part.ref = ref
    part.value = value
    part.footprint = footprint
    pins = []
    for i in range(num_pins):
        pin = MagicMock()
        net_name = pin_nets[i] if pin_nets and i < len(pin_nets) else f"NET_{ref}_{i}"
        pin.net = _mock_net(net_name)
        pins.append(pin)
    part.pins = pins
    part.__len__ = lambda self: num_pins
    part.__iter__ = lambda self: iter(pins)
    return part


def _simple_constraints(**kwargs):
    return LayoutConstraints(
        fixed=kwargs.get('fixed', []),
        zones=kwargs.get('zones', []),
        edge_anchors=kwargs.get('edge_anchors', []),
        keepouts=kwargs.get('keepouts', []),
        outline=kwargs.get('outline', None),
    )


_FP_BBOXES = {
    "Package_DIP:DIP-28": (7.62, 35.56),
    "Package_QFP:QFP-48": (10.0, 10.0),
    "Connector_USB:USB_C": (10.0, 5.0),
    "Connector_PinHeader:PinHeader_1x06_P2.54mm": (2.54, 15.24),
    "Resistor_SMD:R_0805_2012Metric": (2.0, 1.25),
    "Capacitor_SMD:C_0805_2012Metric": (2.0, 1.25),
}


# ---------------------------------------------------------------------------
# Fixed positions
# ---------------------------------------------------------------------------

def test_fixed_positions_honored():
    ic = _make_mock_part("U1", "ATmega328", "Package_DIP:DIP-28", num_pins=28)
    # Give the cap real VCC/GND nets so it is classified as a decoupling cap,
    # which places it directly beside the IC (not stacked below it).
    cap = _make_mock_part(
        "C1", "100nF", "Capacitor_SMD:C_0805_2012Metric",
        num_pins=2, pin_nets=["VCC", "GND"],
    )

    group = PlacementGroup(
        name="main",
        parts=[ic, cap],
        adjacency={"U1": {"C1": 2}, "C1": {"U1": 2}},
    )

    constraints = _simple_constraints(
        fixed=[FixedPosition("U1", 50.0, 60.0, 0.0)],
        outline=BoardOutline(100.0, 80.0),
    )

    result = place_parts({"main": group}, constraints, _FP_BBOXES)

    u1 = next(p for p in result if p.ref == "U1")
    assert u1.x_mm == 50.0
    assert u1.y_mm == 60.0

    c1 = next(p for p in result if p.ref == "C1")
    dist = ((c1.x_mm - 50) ** 2 + (c1.y_mm - 60) ** 2) ** 0.5
    assert dist < 15.0


def test_fixed_rotation_preserved():
    r = _make_mock_part("R1", "10k", "Resistor_SMD:R_0805_2012Metric", num_pins=2)
    group = PlacementGroup(name="g", parts=[r], adjacency={})
    constraints = _simple_constraints(fixed=[FixedPosition("R1", 20.0, 30.0, 90.0)])

    result = place_parts({"g": group}, constraints, _FP_BBOXES)
    r1 = next(p for p in result if p.ref == "R1")
    assert r1.rot_deg == 90.0


# ---------------------------------------------------------------------------
# All parts placed
# ---------------------------------------------------------------------------

def test_all_parts_placed():
    ic = _make_mock_part("U1", "ATMEGA", "Package_DIP:DIP-28", num_pins=28)
    caps = [_make_mock_part(f"C{i}", "100nF", "Capacitor_SMD:C_0805_2012Metric", num_pins=2) for i in range(5)]
    resistors = [_make_mock_part(f"R{i}", "10k", "Resistor_SMD:R_0805_2012Metric", num_pins=2) for i in range(3)]

    all_p = [ic] + caps + resistors
    adj = {p.ref: {"U1": 1} for p in caps + resistors}
    adj["U1"] = {p.ref: 1 for p in caps + resistors}

    group = PlacementGroup(name="main", parts=all_p, adjacency=adj)
    constraints = _simple_constraints(fixed=[FixedPosition("U1", 50.0, 40.0, 0.0)])

    result = place_parts({"main": group}, constraints, _FP_BBOXES)
    result_refs = {p.ref for p in result}
    expected_refs = {p.ref for p in all_p}
    assert result_refs == expected_refs


def test_empty_groups():
    result = place_parts({}, _simple_constraints(), {})
    assert result == []


def test_multiple_groups_all_placed():
    g1 = PlacementGroup(
        name="g1",
        parts=[_make_mock_part("R1"), _make_mock_part("R2")],
        adjacency={},
    )
    g2 = PlacementGroup(
        name="g2",
        parts=[_make_mock_part("R3"), _make_mock_part("R4")],
        adjacency={},
    )
    result = place_parts({"g1": g1, "g2": g2}, _simple_constraints(), _FP_BBOXES)
    assert {p.ref for p in result} == {"R1", "R2", "R3", "R4"}


# ---------------------------------------------------------------------------
# Decoupling cap placement
# ---------------------------------------------------------------------------

def test_decoupling_cap_near_ic():
    ic = _make_mock_part("U1", "MCU", "Package_DIP:DIP-28", num_pins=28)
    cap = _make_mock_part(
        "C1", "100nF", "Capacitor_SMD:C_0805_2012Metric",
        num_pins=2, pin_nets=["VCC", "GND"],
    )

    group = PlacementGroup(
        name="main",
        parts=[ic, cap],
        adjacency={"U1": {"C1": 2}, "C1": {"U1": 2}},
    )
    constraints = _simple_constraints(
        fixed=[FixedPosition("U1", 50.0, 50.0, 0.0)],
        outline=BoardOutline(120.0, 100.0),
    )

    result = place_parts({"main": group}, constraints, _FP_BBOXES)
    c1 = next(p for p in result if p.ref == "C1")
    dist = ((c1.x_mm - 50) ** 2 + (c1.y_mm - 50) ** 2) ** 0.5
    assert dist < 15.0


def test_decoupling_cap_not_decap_by_value():
    """A 10k resistor should NOT be treated as a decoupling cap."""
    r = _make_mock_part("R1", "10k", "Resistor_SMD:R_0805_2012Metric",
                        num_pins=2, pin_nets=["VCC", "GND"])
    group = PlacementGroup(name="g", parts=[r], adjacency={})
    result = place_parts({"g": group}, _simple_constraints(), _FP_BBOXES)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# No overlaps
# ---------------------------------------------------------------------------

def test_no_overlaps_multiple_parts():
    """After placement, no two parts should overlap."""
    ic = _make_mock_part("U1", "MCU", "Package_DIP:DIP-28", num_pins=28)
    parts = [_make_mock_part(f"R{i}", "10k", "Resistor_SMD:R_0805_2012Metric") for i in range(6)]
    adj = {"U1": {p.ref: 1 for p in parts}}
    for p in parts:
        adj[p.ref] = {"U1": 1}

    group = PlacementGroup(name="main", parts=[ic] + parts, adjacency=adj)
    constraints = _simple_constraints(
        fixed=[FixedPosition("U1", 50.0, 50.0, 0.0)],
        outline=BoardOutline(200.0, 200.0),
    )

    result = place_parts({"main": group}, constraints, _FP_BBOXES)

    for i, p1 in enumerate(result):
        w1, h1 = _FP_BBOXES.get(p1.footprint, (2.0, 2.0))
        for p2 in result[i + 1:]:
            w2, h2 = _FP_BBOXES.get(p2.footprint, (2.0, 2.0))
            assert not _overlaps(p1.x_mm, p1.y_mm, w1, h1,
                                 p2.x_mm, p2.y_mm, w2, h2), (
                f"{p1.ref} overlaps {p2.ref}")


# ---------------------------------------------------------------------------
# Parts within outline
# ---------------------------------------------------------------------------

def test_parts_within_outline():
    outline = BoardOutline(100.0, 80.0)
    parts = [_make_mock_part(f"R{i}") for i in range(8)]
    group = PlacementGroup(name="g", parts=parts, adjacency={})
    constraints = _simple_constraints(outline=outline)

    result = place_parts({"g": group}, constraints, _FP_BBOXES)
    for pp in result:
        w, h = _FP_BBOXES.get(pp.footprint, (2.0, 2.0))
        assert pp.x_mm - w / 2 >= 0, f"{pp.ref} off left edge"
        assert pp.y_mm - h / 2 >= 0, f"{pp.ref} off top edge"
        assert pp.x_mm + w / 2 <= outline.width_mm, f"{pp.ref} off right edge"
        assert pp.y_mm + h / 2 <= outline.height_mm, f"{pp.ref} off bottom edge"


def test_parts_within_offset_outline_bounds():
    outline = BoardOutline(
        vertices=[(10.0, 20.0), (110.0, 20.0), (110.0, 100.0), (10.0, 100.0)]
    )
    parts = [_make_mock_part(f"R{i}") for i in range(4)]
    group = PlacementGroup(name="g", parts=parts, adjacency={})
    constraints = _simple_constraints(outline=outline)

    result = place_parts({"g": group}, constraints, _FP_BBOXES)
    for pp in result:
        w, h = _FP_BBOXES.get(pp.footprint, (2.0, 2.0))
        assert pp.x_mm - w / 2 >= outline.x_min
        assert pp.y_mm - h / 2 >= outline.y_min
        assert pp.x_mm + w / 2 <= outline.x_max
        assert pp.y_mm + h / 2 <= outline.y_max


def test_group_anchor_zone_keeps_parts_in_service_area():
    outline = BoardOutline(110.0, 180.0)
    zone = AnchorZone("service", 0.0, 130.0, 110.0, 180.0)
    ic = _make_mock_part("U1", "MCU", "Package_QFP:QFP-48", num_pins=48)
    cap = _make_mock_part(
        "C1", "100nF", "Capacitor_SMD:C_0805_2012Metric",
        num_pins=2, pin_nets=["VCC", "GND"],
    )
    group = PlacementGroup(
        name="service",
        parts=[ic, cap],
        adjacency={"U1": {"C1": 2}, "C1": {"U1": 2}},
    )

    result = place_parts(
        {"service": group},
        _simple_constraints(outline=outline, zones=[zone]),
        _FP_BBOXES,
    )

    for pp in result:
        w, h = _FP_BBOXES[pp.footprint]
        assert pp.x_mm - w / 2 >= zone.x_min
        assert pp.x_mm + w / 2 <= zone.x_max
        assert pp.y_mm - h / 2 >= zone.y_min
        assert pp.y_mm + h / 2 <= zone.y_max


def test_ref_anchor_zone_overrides_group_zone():
    outline = BoardOutline(110.0, 180.0)
    top_zone = AnchorZone("sensors", 0.0, 0.0, 110.0, 70.0)
    ref_zone = AnchorZone("", 0.0, 130.0, 110.0, 180.0, refs=["U1"])
    ic = _make_mock_part("U1", "MCU", "Package_QFP:QFP-48", num_pins=48)
    group = PlacementGroup(name="sensors", parts=[ic], adjacency={})

    result = place_parts(
        {"sensors": group},
        _simple_constraints(outline=outline, zones=[top_zone, ref_zone]),
        _FP_BBOXES,
    )

    u1 = next(p for p in result if p.ref == "U1")
    assert ref_zone.y_min <= u1.y_mm <= ref_zone.y_max


def test_edge_anchor_places_connector_on_bottom_edge():
    outline = BoardOutline(110.0, 180.0)
    usb = _make_mock_part(
        "J1", "USB", "Connector_USB:USB_C", num_pins=16,
    )
    group = PlacementGroup(name="service", parts=[usb], adjacency={})

    result = place_parts(
        {"service": group},
        _simple_constraints(
            outline=outline,
            edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=55.0, rot_deg=180.0)],
        ),
        _FP_BBOXES,
    )

    j1 = next(p for p in result if p.ref == "J1")
    _, h = _FP_BBOXES[j1.footprint]
    assert j1.x_mm == pytest.approx(55.0)
    # courtyard bottom edge sits 0.5mm inside the board edge (default inset)
    assert j1.y_mm + h / 2 == pytest.approx(outline.y_max - 0.5)
    assert j1.rot_deg == 180.0


def test_edge_anchor_uses_footprint_origin_aware_geometry():
    outline = BoardOutline(50.0, 30.0)
    footprint = "Connector_Audio:Jack_OffCenter"
    jack = _make_mock_part("J1", "JACK", footprint, num_pins=3)
    group = PlacementGroup(name="service", parts=[jack], adjacency={})
    geometry = FootprintGeometry(
        footprint=footprint,
        courtyard_bounds=(-1.0, -2.0, 8.0, 4.0),
    )

    result = place_parts(
        {"service": group},
        _simple_constraints(
            outline=outline,
            edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=25.0)],
        ),
        {footprint: (9.0, 6.0)},
        fp_geometries={footprint: geometry},
    )

    j1 = next(p for p in result if p.ref == "J1")
    bounds = geometry.transformed_bounds(j1)
    assert bounds[3] == pytest.approx(outline.y_max - 0.5)
    assert bounds[0] >= outline.x_min
    assert bounds[2] <= outline.x_max


def test_edge_anchor_collision_resolution_stays_on_edge():
    outline = BoardOutline(50.0, 30.0)
    blocker = _make_mock_part(
        "U1", "MCU", "Package_QFP:QFP-48", num_pins=48,
    )
    header = _make_mock_part(
        "J1",
        "header",
        "Connector_PinHeader:PinHeader_1x06_P2.54mm",
        num_pins=6,
    )
    group = PlacementGroup(name="main", parts=[blocker, header], adjacency={})
    header_w, header_h = _FP_BBOXES[header.footprint]
    rotated_h = header_w
    edge_y = outline.y_max - rotated_h / 2 - 0.5

    result = place_parts(
        {"main": group},
        _simple_constraints(
            outline=outline,
            fixed=[FixedPosition("U1", 25.0, edge_y, 0.0)],
            edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=25.0)],
        ),
        _FP_BBOXES,
    )

    j1 = next(p for p in result if p.ref == "J1")
    assert j1.rot_deg == 90.0
    assert j1.y_mm == pytest.approx(edge_y)
    assert j1.x_mm != pytest.approx(25.0)


def test_keepout_is_avoided_during_placement():
    outline = BoardOutline(50.0, 40.0)
    r1 = _make_mock_part("R1")
    r2 = _make_mock_part("R2")
    group = PlacementGroup(name="g", parts=[r1, r2], adjacency={})

    result = place_parts(
        {"g": group},
        _simple_constraints(
            outline=outline,
            keepouts=[KeepOut(20.0, 15.0, 30.0, 25.0)],
        ),
        _FP_BBOXES,
    )

    for placed in result:
        assert not (20.0 <= placed.x_mm <= 30.0 and 15.0 <= placed.y_mm <= 25.0)


def test_align_and_distribute_constraints_move_parts_after_initial_placement():
    parts = [_make_mock_part(f"R{i}") for i in range(1, 4)]
    group = PlacementGroup(name="g", parts=parts, adjacency={})
    constraints = LayoutConstraints(
        outline=BoardOutline(60.0, 40.0),
        align=[AlignConstraint(["R1", "R2", "R3"], "y", 20.0)],
        distribute=[DistributeConstraint(["R1", "R2", "R3"], "x", 10.0, 30.0)],
    )

    result = {placed.ref: placed for placed in place_parts({"g": group}, constraints, _FP_BBOXES)}

    assert [result[ref].x_mm for ref in ["R1", "R2", "R3"]] == pytest.approx(
        [10.0, 20.0, 30.0]
    )
    assert [result[ref].y_mm for ref in ["R1", "R2", "R3"]] == pytest.approx(
        [20.0, 20.0, 20.0]
    )


def test_near_far_and_face_edge_constraints_are_applied():
    u1 = _make_mock_part("U1", "MCU", "Package_QFP:QFP-48", num_pins=48)
    r1 = _make_mock_part("R1")
    r2 = _make_mock_part("R2")
    group = PlacementGroup(
        name="g",
        parts=[u1, r1, r2],
        adjacency={"U1": {"R1": 1, "R2": 1}, "R1": {"U1": 1}, "R2": {"U1": 1}},
    )
    constraints = LayoutConstraints(
        outline=BoardOutline(80.0, 60.0),
        fixed=[FixedPosition("U1", 20.0, 30.0)],
        near=[NearConstraint("R1", "U1", 8.0)],
        far=[FarConstraint("R2", "U1", 24.0)],
        face_edges=[FaceEdgeConstraint("U1", "right", rot_deg=90.0)],
    )

    result = {placed.ref: placed for placed in place_parts({"g": group}, constraints, _FP_BBOXES)}

    assert result["U1"].rot_deg == 90.0
    assert ((result["R1"].x_mm - 20.0) ** 2 + (result["R1"].y_mm - 30.0) ** 2) ** 0.5 < 12.0
    assert ((result["R2"].x_mm - 20.0) ** 2 + (result["R2"].y_mm - 30.0) ** 2) ** 0.5 >= 24.0


def test_power_decaps_distribute_across_tied_fixed_parents():
    u1 = _make_mock_part("U1", "Sensor", "Package_QFP:QFP-48", num_pins=8)
    u2 = _make_mock_part("U2", "Sensor", "Package_QFP:QFP-48", num_pins=8)
    c1 = _make_mock_part(
        "C1", "100nF", "Capacitor_SMD:C_0805_2012Metric",
        num_pins=2, pin_nets=["VCC", "GND"],
    )
    c2 = _make_mock_part(
        "C2", "100nF", "Capacitor_SMD:C_0805_2012Metric",
        num_pins=2, pin_nets=["VCC", "GND"],
    )
    adjacency = {
        "U1": {"C1": 2, "C2": 2},
        "U2": {"C1": 2, "C2": 2},
        "C1": {"U1": 2, "U2": 2},
        "C2": {"U1": 2, "U2": 2},
    }
    group = PlacementGroup(name="sensors", parts=[u1, u2, c1, c2], adjacency=adjacency)

    result = place_parts(
        {"sensors": group},
        _simple_constraints(
            outline=BoardOutline(100.0, 60.0),
            fixed=[
                FixedPosition("U1", 20.0, 30.0),
                FixedPosition("U2", 80.0, 30.0),
            ],
        ),
        _FP_BBOXES,
    )
    placed = {p.ref: p for p in result}

    def nearest_parent(ref):
        cap = placed[ref]
        return min(
            ["U1", "U2"],
            key=lambda parent: (cap.x_mm - placed[parent].x_mm) ** 2
            + (cap.y_mm - placed[parent].y_mm) ** 2,
        )

    assert {nearest_parent("C1"), nearest_parent("C2")} == {"U1", "U2"}


def test_signal_passive_between_opposing_headers_starts_near_centroid():
    left = _make_mock_part(
        "J_LEFT",
        "Conn_01x06",
        "Connector_PinHeader:PinHeader_1x06_P2.54mm",
        num_pins=6,
    )
    right = _make_mock_part(
        "J_RIGHT",
        "Conn_01x06",
        "Connector_PinHeader:PinHeader_1x06_P2.54mm",
        num_pins=6,
    )
    r1 = _make_mock_part(
        "R1",
        "10k",
        "Resistor_SMD:R_0805_2012Metric",
        num_pins=2,
        pin_nets=["IO1", "BIAS"],
    )
    group = PlacementGroup(
        name="bus",
        parts=[left, right, r1],
        adjacency={
            "J_LEFT": {"R1": 1},
            "J_RIGHT": {"R1": 1},
            "R1": {"J_LEFT": 1, "J_RIGHT": 1},
        },
    )
    constraints = _simple_constraints(
        outline=BoardOutline(80.0, 30.0),
        edge_anchors=[
            EdgeAnchor("J_LEFT", "left", offset_mm=15.0),
            EdgeAnchor("J_RIGHT", "right", offset_mm=15.0),
        ],
    )

    placed = {
        part.ref: part
        for part in place_parts({"bus": group}, constraints, _FP_BBOXES)
    }

    assert placed["J_LEFT"].x_mm < 10.0
    assert placed["J_RIGHT"].x_mm > 70.0
    assert placed["R1"].x_mm == pytest.approx(40.0, abs=4.0)
    assert placed["R1"].y_mm == pytest.approx(15.0, abs=4.0)


def test_derive_outline_encloses_parts():
    parts = [
        PlacedPart("R1", 10.0, 20.0, 0.0, "Resistor_SMD:R_0805_2012Metric"),
        PlacedPart("R2", 50.0, 60.0, 0.0, "Resistor_SMD:R_0805_2012Metric"),
    ]
    outline = derive_outline(parts, _FP_BBOXES, margin_mm=5.0)
    assert isinstance(outline, BoardOutline)
    assert outline.width_mm >= 52.0
    assert outline.height_mm >= 51.0


def test_derive_outline_preserves_offset_bounds():
    parts = [
        PlacedPart("R1", -10.0, 20.0, 0.0, "Resistor_SMD:R_0805_2012Metric"),
        PlacedPart("R2", 40.0, 70.0, 0.0, "Resistor_SMD:R_0805_2012Metric"),
    ]
    outline = derive_outline(parts, _FP_BBOXES, margin_mm=5.0)
    assert outline.x_min == pytest.approx(-16.0)
    assert outline.y_min == pytest.approx(14.375)
    assert outline.x_max == pytest.approx(46.0)
    assert outline.y_max == pytest.approx(75.625)


def test_derive_outline_empty_fallback():
    outline = derive_outline([], _FP_BBOXES)
    assert outline.width_mm == 50.0
    assert outline.height_mm == 50.0


def test_derive_outline_single_part():
    parts = [PlacedPart("R1", 25.0, 25.0, 0.0, "Resistor_SMD:R_0805_2012Metric")]
    outline = derive_outline(parts, _FP_BBOXES, margin_mm=3.0)
    assert outline.width_mm == pytest.approx(8.0, abs=0.1)
    assert outline.height_mm == pytest.approx(7.25, abs=0.1)


def test_derive_outline_uses_rotated_footprint_bounds():
    parts = [
        PlacedPart(
            "J1",
            10.0,
            10.0,
            90.0,
            "Connector_PinHeader:PinHeader_1x06_P2.54mm",
        )
    ]

    outline = derive_outline(parts, _FP_BBOXES, margin_mm=1.0)

    assert outline.width_mm == pytest.approx(17.24)
    assert outline.height_mm == pytest.approx(4.54)


def test_derive_outline_caps_density_minimum_growth():
    parts = [
        PlacedPart("R1", 10.0, 20.0, 0.0, "Resistor_SMD:R_0805_2012Metric"),
        PlacedPart("R2", 18.0, 20.0, 0.0, "Resistor_SMD:R_0805_2012Metric"),
    ]
    base = derive_outline(parts, _FP_BBOXES, margin_mm=3.0)
    capped = derive_outline(
        parts,
        _FP_BBOXES,
        margin_mm=3.0,
        min_area_mm2=10_000.0,
        max_min_area_growth=1.35,
    )

    assert capped.width_mm * capped.height_mm == pytest.approx(
        base.width_mm * base.height_mm * 1.35
    )
    assert capped.width_mm < base.width_mm * 2
    assert capped.height_mm < base.height_mm * 2


# ---------------------------------------------------------------------------
# PlacedPart output type
# ---------------------------------------------------------------------------

def test_returns_placed_part_instances():
    r = _make_mock_part("R1")
    group = PlacementGroup(name="g", parts=[r], adjacency={})
    result = place_parts({"g": group}, _simple_constraints(), _FP_BBOXES)
    assert len(result) == 1
    assert isinstance(result[0], PlacedPart)


def test_footprint_set_on_output():
    r = _make_mock_part("R1", footprint="Resistor_SMD:R_0805_2012Metric")
    group = PlacementGroup(name="g", parts=[r], adjacency={})
    result = place_parts({"g": group}, _simple_constraints(), _FP_BBOXES)
    assert result[0].footprint == "Resistor_SMD:R_0805_2012Metric"


def test_foot_attribute_supported_on_output():
    r = MagicMock()
    r.ref = "R1"
    r.value = "10k"
    r.foot = "Resistor_SMD:R_0805_2012Metric"
    r.pins = []
    r.__len__ = lambda self: 2
    group = PlacementGroup(name="g", parts=[r], adjacency={})

    result = place_parts({"g": group}, _simple_constraints(), _FP_BBOXES)

    assert result[0].footprint == "Resistor_SMD:R_0805_2012Metric"


# ---------------------------------------------------------------------------
# Unknown footprint fallback
# ---------------------------------------------------------------------------

def test_unknown_footprint_uses_default_bbox():
    r = _make_mock_part("R1", footprint="Unknown:Part")
    group = PlacementGroup(name="g", parts=[r], adjacency={})
    result = place_parts({"g": group}, _simple_constraints(), {})
    assert len(result) == 1
    assert isinstance(result[0], PlacedPart)
