from __future__ import annotations

import pytest

from skidl_layout.candidates import PlacementCandidate
from skidl_layout.constraints import (
    BoardOutline,
    EdgeAnchor,
    FixedPosition,
    KeepOut,
    LayoutConstraints,
    NearConstraint,
)
from skidl_layout.geometry import FootprintGeometry, PadGeometry
from skidl_layout.refinement import (
    _best_pin_gravity_trial,
    _is_better,
    refine_candidate_placement,
    refine_placement,
)
from skidl_layout.scoring import LayoutScore, score_placement
from skidl_layout.writer import PlacedPart


class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, net):
        self.part = part
        self.net = net
        net._pins.append(self)


class _Part:
    def __init__(self, ref, footprint, name="", value="", nets=None, pins=2):
        self.ref = ref
        self.footprint = footprint
        self.name = name
        self.value = value
        self.pins = []
        for net in nets or []:
            self.pins.append(_Pin(self, net))
        while len(self.pins) < pins:
            self.pins.append(_Pin(self, _Net(f"{ref}_N{len(self.pins)}")))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self._nets = nets

    def get_nets(self):
        return self._nets


BBOXES = {
    "Package_QFP:MCU": (8.0, 8.0),
    "Connector:USB": (10.0, 5.0),
    "Mechanical:MountingHole_3.2mm_M3": (3.2, 3.2),
    "Package:Long": (16.0, 4.0),
    "Resistor_SMD:R_0603": (1.6, 0.8),
    "Capacitor_SMD:C_0603": (1.6, 0.8),
    "Capacitor_SMD:C_0805": (2.0, 1.25),
    "Package_TO_SOT_SMD:SOT-23-5": (3.0, 3.0),
    "Module:DaisySeed": (8.0, 8.0),
    "Connector_Audio:Jack": (8.0, 8.0),
    "Package_TO_SOT_SMD:SOT-23": (3.0, 3.0),
}


def _connected_circuit():
    sig = _Net("SIG")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[sig], pins=8)
    u2 = _Part("U2", "Package_QFP:MCU", nets=[sig], pins=8)
    return _Circuit([u1, u2], [sig])


def _signature(placed_parts):
    return [
        (
            part.ref,
            round(part.x_mm, 4),
            round(part.y_mm, 4),
            round(part.rot_deg, 4),
        )
        for part in placed_parts
    ]


def _mcu_cap_geometries():
    return {
        "Package_QFP:MCU": FootprintGeometry(
            footprint="Package_QFP:MCU",
            courtyard_bounds=(-4.0, -4.0, 4.0, 4.0),
            pads=[
                PadGeometry("1", 4.0, -1.0, 0.5, 0.5),
                PadGeometry("2", 4.0, 1.0, 0.5, 0.5),
            ],
        ),
        "Capacitor_SMD:C_0603": FootprintGeometry(
            footprint="Capacitor_SMD:C_0603",
            courtyard_bounds=(-0.8, -0.4, 0.8, 0.4),
            pads=[
                PadGeometry("1", -0.45, 0.0, 0.4, 0.5),
                PadGeometry("2", 0.45, 0.0, 0.4, 0.5),
            ],
        ),
    }


def _regulator_cap_geometries():
    geometries = _mcu_cap_geometries()
    geometries["Package_TO_SOT_SMD:SOT-23-5"] = FootprintGeometry(
        footprint="Package_TO_SOT_SMD:SOT-23-5",
        courtyard_bounds=(-1.5, -1.5, 1.5, 1.5),
        pads=[
            PadGeometry("1", -1.5, -0.9, 0.4, 0.6),
            PadGeometry("2", 0.0, 1.2, 0.4, 0.6),
            PadGeometry("3", 1.5, -0.9, 0.4, 0.6),
        ],
    )
    geometries["Capacitor_SMD:C_0805"] = FootprintGeometry(
        footprint="Capacitor_SMD:C_0805",
        courtyard_bounds=(-1.0, -0.625, 1.0, 0.625),
        pads=[
            PadGeometry("1", -0.5, 0.0, 0.4, 0.6),
            PadGeometry("2", 0.5, 0.0, 0.4, 0.6),
        ],
    )
    return geometries


def _distance(point_a, point_b):
    return ((point_a[0] - point_b[0]) ** 2 + (point_a[1] - point_b[1]) ** 2) ** 0.5


def test_refinement_moves_unlocked_part_when_score_improves():
    circuit = _connected_circuit()
    constraints = LayoutConstraints(
        outline=BoardOutline(100.0, 50.0),
        fixed=[FixedPosition("U1", 10.0, 10.0)],
    )
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 80.0, 10.0, 0.0, "Package_QFP:MCU"),
    ]
    candidate = PlacementCandidate(
        name="test",
        placed_parts=placed,
        constraints=constraints,
    )
    before = score_placement(
        candidate.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
    )

    result = refine_candidate_placement(candidate, circuit, BBOXES)
    after = score_placement(
        candidate.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
    )
    by_ref = {part.ref: part for part in candidate.placed_parts}

    assert result.accepted_moves >= 1
    assert after.score > before.score
    assert by_ref["U1"].x_mm == pytest.approx(10.0)
    assert by_ref["U1"].y_mm == pytest.approx(10.0)
    assert by_ref["U2"].x_mm < 80.0
    assert "local refinement accepted" in "; ".join(candidate.reasons)
    assert "connected-net centroid" in "; ".join(candidate.ref_reasons["U2"])


def test_refinement_preserves_back_side_when_moving_part():
    circuit = _connected_circuit()
    constraints = LayoutConstraints(
        outline=BoardOutline(100.0, 50.0),
        fixed=[FixedPosition("U1", 10.0, 10.0)],
    )
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU", side="front"),
        PlacedPart("U2", 80.0, 10.0, 0.0, "Package_QFP:MCU", side="back"),
    ]

    result = refine_placement(placed, circuit, BBOXES, constraints=constraints)
    by_ref = {part.ref: part for part in result.placed_parts}

    assert result.accepted_moves >= 1
    assert by_ref["U2"].x_mm < 80.0
    assert by_ref["U2"].side == "back"


def test_refinement_preserves_edge_anchor_positions():
    vbus = _Net("VBUS")
    j1 = _Part("J1", "Connector:USB", name="USB connector", nets=[vbus], pins=4)
    u1 = _Part("U1", "Package_QFP:MCU", nets=[vbus], pins=8)
    circuit = _Circuit([j1, u1], [vbus])
    constraints = LayoutConstraints(
        outline=BoardOutline(100.0, 60.0),
        edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=50.0, rot_deg=180.0)],
    )
    placed = [
        PlacedPart("J1", 50.0, 57.5, 180.0, "Connector:USB"),
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
    ]

    result = refine_placement(placed, circuit, BBOXES, constraints=constraints)
    by_ref = {part.ref: part for part in result.placed_parts}

    assert by_ref["J1"].x_mm == pytest.approx(50.0)
    assert by_ref["J1"].y_mm == pytest.approx(57.5)
    assert by_ref["J1"].rot_deg == pytest.approx(180.0)


def test_refinement_uses_pad_aware_pin_gravity_for_signal_passive():
    sig = _Net("GPIO1")
    vcc = _Net("3V3")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[sig, vcc], pins=8)
    r1 = _Part("R1", "Resistor_SMD:R_0603", nets=[sig, vcc], pins=2)
    circuit = _Circuit([u1, r1], [sig, vcc])
    constraints = LayoutConstraints(
        outline=BoardOutline(80.0, 40.0),
        fixed=[FixedPosition("U1", 20.0, 20.0)],
    )
    geometries = {
        "Package_QFP:MCU": FootprintGeometry(
            footprint="Package_QFP:MCU",
            courtyard_bounds=(-4.0, -4.0, 4.0, 4.0),
            pads=[
                PadGeometry("1", 4.0, -1.0, 0.5, 0.5),
                PadGeometry("2", 4.0, 1.0, 0.5, 0.5),
            ],
        ),
        "Resistor_SMD:R_0603": FootprintGeometry(
            footprint="Resistor_SMD:R_0603",
            courtyard_bounds=(-0.8, -0.4, 0.8, 0.4),
            pads=[
                PadGeometry("1", -0.45, 0.0, 0.4, 0.5),
                PadGeometry("2", 0.45, 0.0, 0.4, 0.5),
            ],
        ),
    }
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("R1", 70.0, 30.0, 0.0, "Resistor_SMD:R_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
    )
    by_ref = {part.ref: part for part in result.placed_parts}

    assert result.accepted_moves >= 1
    assert by_ref["U1"].x_mm == pytest.approx(20.0)
    assert by_ref["R1"].x_mm < 70.0
    assert abs(by_ref["R1"].x_mm - 24.0) < 12.0
    assert "passive pin gravity" in "; ".join(result.ref_reasons["R1"])


def test_refinement_uses_ic_gravity_when_pad_geometry_is_unavailable():
    sig = _Net("SIG")
    gnd = _Net("GND")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[sig, gnd], pins=8)
    j1 = _Part("J1", "Connector:USB", nets=[sig, gnd], pins=2)
    r1 = _Part("R1", "Resistor_SMD:R_0603", nets=[sig, gnd])
    circuit = _Circuit([u1, j1, r1], [sig, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("J1", 90.0, 20.0, 0.0, "Connector:USB"),
        PlacedPart("R1", 82.0, 20.0, 0.0, "Resistor_SMD:R_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=LayoutConstraints(
            outline=BoardOutline(100.0, 40.0),
            fixed=[FixedPosition("U1", 20.0, 20.0)],
            edge_anchors=[EdgeAnchor("J1", "right", offset_mm=20.0)],
        ),
        fp_geometries=None,
        max_passes=1,
    )

    final = {part.ref: part for part in result.placed_parts}

    assert final["U1"].x_mm == pytest.approx(20.0)
    assert final["J1"].x_mm == pytest.approx(90.0)
    assert final["R1"].x_mm < 40.0
    assert "passive pin gravity" in "; ".join(result.ref_reasons["R1"])


def test_refinement_keeps_pin_gravity_passive_from_centroid_drift():
    sig = _Net("GPIO1")
    vcc = _Net("3V3")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[sig, vcc], pins=8)
    j1 = _Part("J1", "Connector:USB", nets=[sig], pins=4)
    r1 = _Part("R1", "Resistor_SMD:R_0603", nets=[sig, vcc], pins=2)
    circuit = _Circuit([u1, j1, r1], [sig, vcc])
    constraints = LayoutConstraints(
        outline=BoardOutline(100.0, 50.0),
        fixed=[FixedPosition("U1", 20.0, 20.0), FixedPosition("J1", 90.0, 20.0)],
    )
    geometries = {
        "Package_QFP:MCU": FootprintGeometry(
            footprint="Package_QFP:MCU",
            courtyard_bounds=(-4.0, -4.0, 4.0, 4.0),
            pads=[
                PadGeometry("1", 4.0, -1.0, 0.5, 0.5),
                PadGeometry("2", 4.0, 1.0, 0.5, 0.5),
            ],
        ),
        "Resistor_SMD:R_0603": FootprintGeometry(
            footprint="Resistor_SMD:R_0603",
            courtyard_bounds=(-0.8, -0.4, 0.8, 0.4),
            pads=[
                PadGeometry("1", -0.45, 0.0, 0.4, 0.5),
                PadGeometry("2", 0.45, 0.0, 0.4, 0.5),
            ],
        ),
        "Connector:USB": FootprintGeometry(
            footprint="Connector:USB",
            courtyard_bounds=(-5.0, -2.5, 5.0, 2.5),
            pads=[PadGeometry("1", -2.0, 0.0, 0.8, 0.8)],
        ),
    }
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("J1", 90.0, 20.0, 0.0, "Connector:USB"),
        PlacedPart("R1", 70.0, 30.0, 0.0, "Resistor_SMD:R_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
    )
    by_ref = {part.ref: part for part in result.placed_parts}
    reasons = "; ".join(result.ref_reasons["R1"])

    assert by_ref["R1"].x_mm < 40.0
    assert "passive pin gravity" in reasons
    assert "connected-net centroid" not in reasons


def test_refinement_moves_decap_toward_nearby_common_rail_ic_group():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    u2 = _Part("U2", "Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    c1 = _Part("C1", "Capacitor_SMD:C_0603", value="100nF", nets=[vcc, gnd])
    circuit = _Circuit([u1, u2, c1], [vcc, gnd])
    constraints = LayoutConstraints(
        outline=BoardOutline(110.0, 45.0),
        fixed=[FixedPosition("U1", 20.0, 20.0), FixedPosition("U2", 80.0, 20.0)],
    )
    geometries = _mcu_cap_geometries()
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 80.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 68.0, 20.0, 0.0, "Capacitor_SMD:C_0603"),
    ]
    u1_supply_target = (24.0, 20.0)
    u2_supply_target = (84.0, 20.0)

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
    )
    by_ref = {part.ref: part for part in result.placed_parts}
    cap_xy = (by_ref["C1"].x_mm, by_ref["C1"].y_mm)
    reasons = "; ".join(result.ref_reasons["C1"])

    assert result.accepted_moves >= 1
    assert _distance(cap_xy, u2_supply_target) < _distance(
        (68.0, 20.0),
        u2_supply_target,
    )
    assert _distance(cap_xy, u2_supply_target) < _distance(
        cap_xy,
        u1_supply_target,
    )
    assert "passive pin gravity" in reasons


def test_refinement_named_decap_uses_token_matched_rail_owner():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", "Package_QFP:MCU", name="ESP32 module", nets=[vcc, gnd], pins=8)
    u4 = _Part(
        "U4",
        "Package_QFP:MCU",
        name="BME280 environmental sensor",
        nets=[gnd, vcc],
        pins=8,
    )
    cbme1 = _Part("CBME1", "Capacitor_SMD:C_0603", value="100nF", nets=[vcc, gnd])
    circuit = _Circuit([u1, u4, cbme1], [vcc, gnd])
    constraints = LayoutConstraints(
        outline=BoardOutline(95.0, 40.0),
        fixed=[FixedPosition("U1", 20.0, 20.0), FixedPosition("U4", 70.0, 20.0)],
    )
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U4", 70.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("CBME1", 22.0, 20.0, 0.0, "Capacitor_SMD:C_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=_mcu_cap_geometries(),
        max_passes=1,
    )
    by_ref = {part.ref: part for part in result.placed_parts}
    cap_xy = (by_ref["CBME1"].x_mm, by_ref["CBME1"].y_mm)

    assert result.accepted_moves >= 1
    assert by_ref["U1"].x_mm == pytest.approx(20.0)
    assert by_ref["U4"].x_mm == pytest.approx(70.0)
    assert _distance(cap_xy, (74.0, 20.0)) < _distance((22.0, 20.0), (74.0, 20.0))
    assert _distance(cap_xy, (70.0, 20.0)) < _distance(cap_xy, (20.0, 20.0))
    assert "U4 ic owner" in "; ".join(result.ref_reasons["CBME1"])
    assert "name affinity" in "; ".join(result.ref_reasons["CBME1"])


def test_refinement_series_passive_prefers_module_pin_over_panel_endpoint():
    cv_tip = _Net("CV_TIP")
    cv_in = _Net("CV_IN")
    module = _Part(
        "A1",
        "Module:DaisySeed",
        name="Electrosmith Daisy Seed Rev4",
        nets=[cv_in],
        pins=40,
    )
    jack = _Part(
        "J_CV",
        "Connector_Audio:Jack",
        name="Thonkiconn PJ398SM panel jack",
        nets=[cv_tip],
        pins=2,
    )
    resistor = _Part("R_CV", "Resistor_SMD:R_0603", nets=[cv_tip, cv_in])
    circuit = _Circuit([module, jack, resistor], [cv_tip, cv_in])
    constraints = LayoutConstraints(
        outline=BoardOutline(90.0, 40.0),
        fixed=[FixedPosition("A1", 68.0, 20.0)],
        edge_anchors=[EdgeAnchor("J_CV", "left", offset_mm=20.0)],
    )
    geometries = {
        "Module:DaisySeed": FootprintGeometry(
            footprint="Module:DaisySeed",
            courtyard_bounds=(-4.0, -4.0, 4.0, 4.0),
            pads=[PadGeometry("1", -4.0, 0.0, 0.6, 0.6)],
        ),
        "Connector_Audio:Jack": FootprintGeometry(
            footprint="Connector_Audio:Jack",
            courtyard_bounds=(-4.0, -4.0, 4.0, 4.0),
            pads=[PadGeometry("1", 4.0, 0.0, 0.8, 0.8)],
        ),
        "Resistor_SMD:R_0603": FootprintGeometry(
            footprint="Resistor_SMD:R_0603",
            courtyard_bounds=(-0.8, -0.4, 0.8, 0.4),
            pads=[
                PadGeometry("1", -0.45, 0.0, 0.4, 0.5),
                PadGeometry("2", 0.45, 0.0, 0.4, 0.5),
            ],
        ),
    }
    placed = [
        PlacedPart("A1", 68.0, 20.0, 0.0, "Module:DaisySeed"),
        PlacedPart("J_CV", 4.0, 20.0, 0.0, "Connector_Audio:Jack"),
        PlacedPart("R_CV", 12.0, 20.0, 0.0, "Resistor_SMD:R_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
        max_passes=1,
    )
    by_ref = {part.ref: part for part in result.placed_parts}

    assert result.accepted_moves >= 1
    assert by_ref["A1"].x_mm == pytest.approx(68.0)
    assert by_ref["J_CV"].x_mm == pytest.approx(4.0)
    assert by_ref["R_CV"].x_mm > 50.0
    assert "A1 module_socket owner" in "; ".join(result.ref_reasons["R_CV"])


def test_refinement_series_passive_prefers_transistor_pin_over_panel_endpoint():
    fm_cv = _Net("FM_CV")
    expo_base = _Net("EXPO_BASE")
    q1 = _Part(
        "Q1",
        "Package_TO_SOT_SMD:SOT-23",
        name="MMBT3904 expo transistor",
        nets=[expo_base],
        pins=3,
    )
    jack = _Part(
        "J_FM",
        "Connector_Audio:Jack",
        name="Thonkiconn PJ398SM panel jack",
        nets=[fm_cv],
        pins=2,
    )
    resistor = _Part("R_FM", "Resistor_SMD:R_0603", nets=[fm_cv, expo_base])
    circuit = _Circuit([q1, jack, resistor], [fm_cv, expo_base])
    constraints = LayoutConstraints(
        outline=BoardOutline(80.0, 40.0),
        fixed=[FixedPosition("Q1", 62.0, 20.0)],
        edge_anchors=[EdgeAnchor("J_FM", "left", offset_mm=20.0)],
    )
    geometries = {
        "Package_TO_SOT_SMD:SOT-23": FootprintGeometry(
            footprint="Package_TO_SOT_SMD:SOT-23",
            courtyard_bounds=(-1.5, -1.5, 1.5, 1.5),
            pads=[PadGeometry("1", -1.5, 0.0, 0.4, 0.6)],
        ),
        "Connector_Audio:Jack": FootprintGeometry(
            footprint="Connector_Audio:Jack",
            courtyard_bounds=(-4.0, -4.0, 4.0, 4.0),
            pads=[PadGeometry("1", 4.0, 0.0, 0.8, 0.8)],
        ),
        "Resistor_SMD:R_0603": FootprintGeometry(
            footprint="Resistor_SMD:R_0603",
            courtyard_bounds=(-0.8, -0.4, 0.8, 0.4),
            pads=[
                PadGeometry("1", -0.45, 0.0, 0.4, 0.5),
                PadGeometry("2", 0.45, 0.0, 0.4, 0.5),
            ],
        ),
    }
    placed = [
        PlacedPart("Q1", 62.0, 20.0, 0.0, "Package_TO_SOT_SMD:SOT-23"),
        PlacedPart("J_FM", 4.0, 20.0, 0.0, "Connector_Audio:Jack"),
        PlacedPart("R_FM", 12.0, 20.0, 0.0, "Resistor_SMD:R_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
        max_passes=1,
    )
    by_ref = {part.ref: part for part in result.placed_parts}

    assert result.accepted_moves >= 1
    assert by_ref["Q1"].x_mm == pytest.approx(62.0)
    assert by_ref["J_FM"].x_mm == pytest.approx(4.0)
    assert by_ref["R_FM"].x_mm > 48.0
    assert "Q1 ic owner" in "; ".join(result.ref_reasons["R_FM"])


def test_refinement_preanchored_decap_skips_generic_pin_gravity():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    u2 = _Part("U2", "Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    c1 = _Part("C1", "Capacitor_SMD:C_0603", value="100nF", nets=[vcc, gnd])
    circuit = _Circuit([u1, u2, c1], [vcc, gnd])
    constraints = LayoutConstraints(
        outline=BoardOutline(110.0, 45.0),
        fixed=[FixedPosition("U1", 20.0, 20.0), FixedPosition("U2", 80.0, 20.0)],
    )
    geometries = _mcu_cap_geometries()
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 80.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 68.0, 20.0, 0.0, "Capacitor_SMD:C_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
        preanchored_refs={"C1"},
    )
    by_ref = {part.ref: part for part in result.placed_parts}

    assert by_ref["C1"].x_mm == pytest.approx(68.0)
    assert by_ref["C1"].y_mm == pytest.approx(20.0)
    assert "C1" not in result.ref_reasons


def test_refinement_uses_near_constraint_for_passive_without_pad_geometry():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    u2 = _Part("U2", "Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    c1 = _Part("C1", "Capacitor_SMD:C_0603", value="100nF", nets=[vcc, gnd])
    circuit = _Circuit([u1, u2, c1], [vcc, gnd])
    constraints = LayoutConstraints(
        outline=BoardOutline(110.0, 45.0),
        fixed=[FixedPosition("U1", 20.0, 20.0), FixedPosition("U2", 80.0, 20.0)],
        near=[NearConstraint(ref="C1", target_ref="U2", distance_mm=5.0)],
    )
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 80.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 50.0, 20.0, 0.0, "Capacitor_SMD:C_0603"),
    ]

    result = refine_placement(placed, circuit, BBOXES, constraints=constraints)
    by_ref = {part.ref: part for part in result.placed_parts}
    cap_xy = (by_ref["C1"].x_mm, by_ref["C1"].y_mm)
    reasons = "; ".join(result.ref_reasons["C1"])

    assert result.accepted_moves >= 1
    assert _distance(cap_xy, (80.0, 20.0)) < _distance((50.0, 20.0), (80.0, 20.0))
    assert "near constraint to U2" in reasons


def test_refinement_composes_named_regulator_caps_around_parent_pins():
    vin = _Net("VIN")
    vout = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part(
        "U1",
        "Package_TO_SOT_SMD:SOT-23-5",
        name="AP2112 regulator",
        nets=[vin, gnd, vout],
        pins=5,
    )
    cin = _Part("CIN", "Capacitor_SMD:C_0603", value="100nF", nets=[vin, gnd])
    cbulk = _Part("CBULK", "Capacitor_SMD:C_0805", value="10uF", nets=[vin, gnd])
    cout = _Part("COUT", "Capacitor_SMD:C_0603", value="100nF", nets=[vout, gnd])
    h1 = _Part(
        "H1",
        "Mechanical:MountingHole_3.2mm_M3",
        name="mounting hole",
        pins=0,
    )
    circuit = _Circuit([u1, cin, cbulk, cout, h1], [vin, vout, gnd])
    constraints = LayoutConstraints(
        outline=BoardOutline(80.0, 50.0),
        fixed=[
            FixedPosition("U1", 40.0, 25.0),
            FixedPosition("H1", 35.0, 25.0),
        ],
    )
    placed = [
        PlacedPart("U1", 40.0, 25.0, 0.0, "Package_TO_SOT_SMD:SOT-23-5"),
        PlacedPart("H1", 35.0, 25.0, 0.0, "Mechanical:MountingHole_3.2mm_M3"),
        PlacedPart("CIN", 72.0, 10.0, 0.0, "Capacitor_SMD:C_0603"),
        PlacedPart("CBULK", 70.0, 40.0, 0.0, "Capacitor_SMD:C_0805"),
        PlacedPart("COUT", 10.0, 35.0, 0.0, "Capacitor_SMD:C_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=_regulator_cap_geometries(),
        max_passes=4,
    )
    by_ref = {part.ref: part for part in result.placed_parts}
    score = score_placement(
        result.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
        fp_geometries=_regulator_cap_geometries(),
    )
    input_cap_spacing = _distance(
        (by_ref["CIN"].x_mm, by_ref["CIN"].y_mm),
        (by_ref["CBULK"].x_mm, by_ref["CBULK"].y_mm),
    )

    assert result.accepted_moves >= 3
    assert by_ref["H1"].x_mm == pytest.approx(35.0)
    assert by_ref["H1"].y_mm == pytest.approx(25.0)
    assert score.overlap_count == 0
    assert (
        _distance((by_ref["CIN"].x_mm, by_ref["CIN"].y_mm), (40.0, 25.0))
        < 4.0
    )
    assert (
        _distance((by_ref["CBULK"].x_mm, by_ref["CBULK"].y_mm), (40.0, 25.0))
        < 4.0
    )
    assert by_ref["CIN"].x_mm <= by_ref["U1"].x_mm + 0.5
    assert by_ref["CBULK"].x_mm <= by_ref["U1"].x_mm + 0.5
    assert by_ref["COUT"].x_mm > by_ref["U1"].x_mm
    assert input_cap_spacing > 2.0
    assert "composed passive group slot" in "; ".join(result.ref_reasons["CIN"])
    assert "passive pin gravity" in "; ".join(result.ref_reasons["COUT"])


def test_refinement_passive_pin_gravity_avoids_mounting_hole_and_edge_keepouts():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    c1 = _Part("C1", "Capacitor_SMD:C_0603", value="100nF", nets=[vcc, gnd])
    h1 = _Part(
        "H1",
        "Mechanical:MountingHole_3.2mm_M3",
        name="mounting hole",
        pins=0,
    )
    circuit = _Circuit([u1, c1, h1], [vcc, gnd])
    constraints = LayoutConstraints(
        outline=BoardOutline(100.0, 40.0),
        fixed=[
            FixedPosition("U1", 85.0, 20.0),
            FixedPosition("H1", 75.0, 20.0),
        ],
        keepouts=[
            KeepOut(87.0, 0.0, 100.0, 40.0, allowed_refs=["U1"]),
            KeepOut(73.5, 18.5, 76.5, 21.5, allowed_refs=["H1"]),
        ],
    )
    geometries = _mcu_cap_geometries()
    placed = [
        PlacedPart("U1", 85.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("H1", 75.0, 20.0, 0.0, "Mechanical:MountingHole_3.2mm_M3"),
        PlacedPart("C1", 20.0, 20.0, 0.0, "Capacitor_SMD:C_0603"),
    ]
    supply_target = (89.0, 20.0)

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
    )
    by_ref = {part.ref: part for part in result.placed_parts}
    cap_xy = (by_ref["C1"].x_mm, by_ref["C1"].y_mm)
    score = score_placement(
        result.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
        keepouts=constraints.keepouts,
        fp_geometries=geometries,
    )

    assert result.accepted_moves >= 1
    assert _distance(cap_xy, supply_target) < _distance(
        (20.0, 20.0),
        supply_target,
    )
    assert by_ref["H1"].x_mm == pytest.approx(75.0)
    assert by_ref["H1"].y_mm == pytest.approx(20.0)
    assert score.overlap_count == 0
    assert score.keepout_violation_count == 0
    assert "passive pin gravity" in "; ".join(result.ref_reasons["C1"])


def test_refinement_better_gate_prioritizes_hard_violations():
    assert _is_better(
        LayoutScore(score=60.0, overlap_count=2),
        LayoutScore(score=55.0, overlap_count=1),
    )
    assert not _is_better(
        LayoutScore(score=60.0, overlap_count=1),
        LayoutScore(score=90.0, overlap_count=2),
    )
    assert _is_better(
        LayoutScore(score=60.0, overlap_count=1),
        LayoutScore(score=65.0, overlap_count=1),
    )


def test_refinement_legalizes_overlap_without_net_centroid():
    u1 = _Part("U1", "Package_QFP:MCU", pins=8)
    u2 = _Part("U2", "Package_QFP:MCU", pins=8)
    circuit = _Circuit([u1, u2], [])
    constraints = LayoutConstraints(
        outline=BoardOutline(60.0, 40.0),
        fixed=[FixedPosition("U1", 20.0, 20.0)],
    )
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
    ]

    result = refine_placement(placed, circuit, BBOXES, constraints=constraints)
    by_ref = {part.ref: part for part in result.placed_parts}
    score = score_placement(
        result.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
    )

    assert result.accepted_moves >= 1
    assert score.overlap_count == 0
    assert by_ref["U1"].x_mm == pytest.approx(20.0)
    assert by_ref["U1"].y_mm == pytest.approx(20.0)
    assert by_ref["U2"].x_mm != pytest.approx(20.0) or by_ref["U2"].y_mm != pytest.approx(20.0)
    assert "legalized overlap" in "; ".join(result.ref_reasons["U2"])


def test_refinement_legalizes_movable_decap_from_fixed_floorplan_cap():
    vbat = _Net("VBAT")
    gnd = _Net("GND")
    u1 = _Part("U1", "Package_QFP:MCU", nets=[vbat, gnd], pins=8)
    cin = _Part("CIN", "Capacitor_SMD:C_0805", value="10uF", nets=[vbat, gnd])
    c1 = _Part("C1", "Capacitor_SMD:C_0603", value="100nF", nets=[vbat, gnd])
    circuit = _Circuit([u1, cin, c1], [vbat, gnd])
    constraints = LayoutConstraints(
        outline=BoardOutline(40.0, 25.0),
        fixed=[
            FixedPosition("U1", 24.0, 12.0),
            FixedPosition("CIN", 14.0, 10.0),
        ],
    )
    placed = [
        PlacedPart("U1", 24.0, 12.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("CIN", 14.0, 10.0, 0.0, "Capacitor_SMD:C_0805"),
        PlacedPart("C1", 14.0, 10.0, 0.0, "Capacitor_SMD:C_0603"),
    ]

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        max_movable_refs=0,
    )
    by_ref = {part.ref: part for part in result.placed_parts}
    score = score_placement(
        result.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
    )

    assert result.accepted_moves >= 1
    assert score.overlap_count == 0
    assert by_ref["U1"].x_mm == pytest.approx(24.0)
    assert by_ref["U1"].y_mm == pytest.approx(12.0)
    assert by_ref["CIN"].x_mm == pytest.approx(14.0)
    assert by_ref["CIN"].y_mm == pytest.approx(10.0)
    assert (
        by_ref["C1"].x_mm != pytest.approx(14.0)
        or by_ref["C1"].y_mm != pytest.approx(10.0)
    )
    assert "legalized overlap with CIN" in "; ".join(result.ref_reasons["C1"])


def test_refinement_legalizes_multiple_independent_overlaps():
    parts = [_Part(ref, "Package_QFP:MCU", pins=8) for ref in ("U1", "U2", "U3", "U4")]
    circuit = _Circuit(parts, [])
    constraints = LayoutConstraints(
        outline=BoardOutline(80.0, 50.0),
        fixed=[FixedPosition("U1", 20.0, 20.0)],
    )
    placed = [
        PlacedPart(part.ref, 20.0, 20.0, 0.0, "Package_QFP:MCU")
        for part in parts
    ]

    result = refine_placement(placed, circuit, BBOXES, constraints=constraints)
    score = score_placement(
        result.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
    )

    assert result.accepted_moves >= 3
    assert score.overlap_count == 0


def test_pin_gravity_rejects_module_courtyard_overlap():
    sig = _Net("EN")
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", "RF_Module:LargeModule", name="ESP32 module", nets=[sig, vcc, gnd], pins=8)
    r1 = _Part("REN", "Resistor_SMD:R_0402", value="100K", nets=[sig, vcc])
    circuit = _Circuit([u1, r1], [sig, vcc, gnd])
    fp_geometries = {
        "RF_Module:LargeModule": FootprintGeometry(
            footprint="RF_Module:LargeModule",
            body_bounds=(-3.0, -3.0, 3.0, 3.0),
            courtyard_bounds=(-10.0, -5.0, 10.0, 5.0),
            pads=[
                PadGeometry("1", -4.0, 0.0, 0.5, 0.5),
                PadGeometry("2", 4.0, 0.0, 0.5, 0.5),
            ],
        ),
        "Resistor_SMD:R_0402": FootprintGeometry(
            footprint="Resistor_SMD:R_0402",
            body_bounds=(-0.35, -0.2, 0.35, 0.2),
            courtyard_bounds=(-0.55, -0.3, 0.55, 0.3),
            pads=[
                PadGeometry("1", -0.25, 0.0, 0.25, 0.25),
                PadGeometry("2", 0.25, 0.0, 0.25, 0.25),
            ],
        ),
    }
    fp_bboxes = {
        name: (
            geom.bounds[2] - geom.bounds[0],
            geom.bounds[3] - geom.bounds[1],
        )
        for name, geom in fp_geometries.items()
    }
    constraints = LayoutConstraints(outline=BoardOutline(40.0, 30.0))
    placed = [
        PlacedPart("U1", 20.0, 15.0, 0.0, "RF_Module:LargeModule"),
        PlacedPart("REN", 5.0, 15.0, 0.0, "Resistor_SMD:R_0402"),
    ]
    current_score = score_placement(
        placed,
        circuit,
        fp_bboxes,
        outline=constraints.outline,
        fp_geometries=fp_geometries,
    )

    best = _best_pin_gravity_trial(
        placed,
        current_score,
        "REN",
        placed[1],
        (16.0, 15.0),
        [
            PlacedPart("REN", 16.0, 15.0, 0.0, "Resistor_SMD:R_0402"),
            PlacedPart("REN", 8.5, 15.0, 0.0, "Resistor_SMD:R_0402"),
        ],
        circuit,
        fp_bboxes,
        constraints,
        fp_geometries,
        clearance_mm=0.5,
        board_layers=2,
    )

    assert best is not None
    _parts, _score, trial = best
    assert trial.x_mm == pytest.approx(8.5)


def test_refinement_legalizes_more_than_sixteen_overlaps_by_default():
    pair_count = 18
    parts = []
    placed = []
    fixed = []
    for idx in range(pair_count):
        x = 15.0 + (idx % 6) * 25.0
        y = 15.0 + (idx // 6) * 25.0
        fixed_ref = f"U{idx}"
        movable_ref = f"C{idx}"
        parts.extend([
            _Part(fixed_ref, "Package_QFP:MCU", pins=8),
            _Part(movable_ref, "Package_QFP:MCU", pins=8),
        ])
        placed.extend([
            PlacedPart(fixed_ref, x, y, 0.0, "Package_QFP:MCU"),
            PlacedPart(movable_ref, x, y, 0.0, "Package_QFP:MCU"),
        ])
        fixed.append(FixedPosition(fixed_ref, x, y))

    circuit = _Circuit(parts, [])
    constraints = LayoutConstraints(
        outline=BoardOutline(180.0, 100.0),
        fixed=fixed,
    )

    result = refine_placement(placed, circuit, BBOXES, constraints=constraints)
    score = score_placement(
        result.placed_parts,
        circuit,
        BBOXES,
        outline=constraints.outline,
    )

    assert result.accepted_moves >= pair_count
    assert score.overlap_count == 0


def test_refinement_can_rotate_geometry_into_outline():
    circuit = _Circuit([_Part("U1", "Package:Long", pins=8)], [])
    constraints = LayoutConstraints(outline=BoardOutline(20.0, 20.0))
    placed = [PlacedPart("U1", 18.0, 10.0, 0.0, "Package:Long")]
    geometries = {
        "Package:Long": FootprintGeometry(
            footprint="Package:Long",
            body_bounds=(-8.0, -2.0, 8.0, 2.0),
        )
    }

    result = refine_placement(
        placed,
        circuit,
        BBOXES,
        constraints=constraints,
        fp_geometries=geometries,
    )

    assert result.accepted_rotations == 1
    assert result.placed_parts[0].rot_deg == pytest.approx(90.0)
    assert result.final_score > result.start_score


def test_refinement_is_deterministic():
    circuit = _connected_circuit()
    constraints = LayoutConstraints(outline=BoardOutline(100.0, 50.0))
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 80.0, 10.0, 0.0, "Package_QFP:MCU"),
    ]

    first = refine_placement(placed, circuit, BBOXES, constraints=constraints)
    second = refine_placement(placed, circuit, BBOXES, constraints=constraints)

    assert _signature(first.placed_parts) == _signature(second.placed_parts)
    assert first.final_score == pytest.approx(second.final_score)
