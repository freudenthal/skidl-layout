from __future__ import annotations

from simp_sexp import Sexp

from skidl_layout.constraints import BoardOutline
from skidl_layout.geometry import footprint_geometry_from_sexp
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
    def __init__(self, ref, value="", footprint="", name="", nets=None, pins=2):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.name = name
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
    "Connector:Header": (10.0, 4.0),
    "Connector_Audio:Thonkiconn_PJ398SM": (8.0, 8.0),
    "Package_QFP:MCU": (12.0, 12.0),
    "Capacitor:C_0805": (2.0, 1.25),
    "Potentiometer_THT:Potentiometer_Alpha": (9.5, 9.5),
    "Module:DaisySeed": (8.0, 8.0),
    "Resistor_SMD:R_0603": (1.6, 0.8),
}


def test_score_connector_warns_when_far_from_edge():
    connector = _Part("J1", name="Header", footprint="Connector:Header")
    circuit = _Circuit([connector], [])
    placed = [PlacedPart("J1", 50.0, 50.0, 0.0, "Connector:Header")]

    score = score_placement(placed, circuit, BBOXES, outline=BoardOutline(100.0, 100.0))

    assert isinstance(score, LayoutScore)
    assert score.warning_count == 1
    assert "connector" in score.warnings[0]
    assert score.score < 100.0


def test_score_warns_when_outline_is_much_larger_than_placed_envelope():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    ic = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd])
    cap = _Part("C1", value="1uF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    circuit = _Circuit([ic, cap], [vcc, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 28.0, 20.0, 0.0, "Capacitor:C_0805"),
    ]

    score = score_placement(
        placed,
        circuit,
        BBOXES,
        outline=BoardOutline(80.0, 50.0),
    )

    assert any("outline is" in warning for warning in score.warnings)
    assert score.score < 100.0


def test_score_exports_compact_outline_and_margin_metrics():
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU"),
        _Part("R1", value="10K", footprint="Capacitor:C_0805"),
        _Part("R2", value="10K", footprint="Capacitor:C_0805"),
        _Part("C1", value="100nF", footprint="Capacitor:C_0805"),
        _Part("J1", name="header", footprint="Connector:Header"),
    ]
    circuit = _Circuit(parts, [])
    placed = [
        PlacedPart("U1", 22.0, 18.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("R1", 31.0, 18.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("R2", 31.0, 21.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("C1", 24.0, 25.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 33.0, 25.0, 0.0, "Connector:Header"),
    ]

    score = score_placement(
        placed,
        circuit,
        BBOXES,
        outline=BoardOutline(100.0, 70.0),
    )
    data = score.to_dict()

    assert score.compact_outline_mm["width"] < 40.0
    assert score.compact_outline_area_ratio < 0.25
    assert score.max_empty_margin_ratio > 0.4
    assert data["footprint_envelope_bbox_mm"]["width"] > 0.0
    assert data["empty_margin_ratios"]["right"] > 0.5


def test_score_prefers_better_use_of_generous_outline():
    ic = _Part("U1", name="MCU", footprint="Package_QFP:MCU")
    cap = _Part("C1", value="1uF", footprint="Capacitor:C_0805")
    circuit = _Circuit([ic, cap], [])
    outline = BoardOutline(90.0, 60.0)
    compact = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 28.0, 20.0, 0.0, "Capacitor:C_0805"),
    ]
    spread = [
        PlacedPart("U1", 40.0, 30.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 62.0, 30.0, 0.0, "Capacitor:C_0805"),
    ]

    compact_score = score_placement(compact, circuit, BBOXES, outline=outline)
    spread_score = score_placement(spread, circuit, BBOXES, outline=outline)

    assert compact_score.score < spread_score.score


def test_score_warns_when_primary_ic_is_far_from_center_on_simple_board():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    ic = _Part("U1", name="sensor IC", footprint="Package_QFP:MCU", nets=[vcc, gnd])
    circuit = _Circuit([ic], [vcc, gnd])
    placed = [PlacedPart("U1", 12.0, 12.0, 0.0, "Package_QFP:MCU")]

    score = score_placement(
        placed,
        circuit,
        BBOXES,
        outline=BoardOutline(60.0, 40.0),
    )

    assert any("board center" in warning for warning in score.warnings)


def test_score_decoupling_cap_warns_when_far_from_parent():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig = _Net("SIG")
    ic = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd, sig], pins=3)
    cap = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    circuit = _Circuit([ic, cap], [vcc, gnd, sig])
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 30.0, 10.0, 0.0, "Capacitor:C_0805"),
    ]

    score = score_placement(placed, circuit, BBOXES)

    assert any("decoupling cap" in warning for warning in score.warnings)
    assert score.role_counts["decoupling_cap"] == 1
    assert score.role_counts["ic"] == 1
    assert score.power_net_count == 2


def test_score_named_decap_warns_from_token_matched_owner_without_geometry():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    mcu = _Part("U1", name="ESP32-S3 module", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    sensor = _Part(
        "U4",
        name="BME280 environmental sensor",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd],
        pins=8,
    )
    cap = _Part("CBME1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    circuit = _Circuit([mcu, sensor, cap], [vcc, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U4", 70.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("CBME1", 22.0, 20.0, 0.0, "Capacitor:C_0805"),
    ]

    score = score_placement(placed, circuit, BBOXES)

    assert any("CBME1: decoupling cap" in warning and "U4" in warning for warning in score.warnings)


def test_score_warns_when_signal_passive_is_far_from_ic_parent():
    sig = _Net("SIG")
    gnd = _Net("GND")
    ic = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[sig, gnd], pins=8)
    connector = _Part(
        "J1",
        name="edge connector",
        footprint="Connector:Header",
        nets=[sig, gnd],
    )
    resistor = _Part("R1", value="10K", footprint="Capacitor:C_0805", nets=[sig, gnd])
    circuit = _Circuit([ic, connector, resistor], [sig, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("J1", 90.0, 20.0, 0.0, "Connector:Header"),
        PlacedPart("R1", 82.0, 20.0, 0.0, "Capacitor:C_0805"),
    ]

    score = score_placement(placed, circuit, BBOXES, outline=BoardOutline(100.0, 40.0))

    assert any("signal passive" in warning for warning in score.warnings)
    assert score.score < 100.0


def test_score_signal_passive_warns_from_module_owner_not_panel_endpoint():
    cv_tip = _Net("CV_TIP")
    cv_in = _Net("CV_IN")
    module = _Part(
        "A1",
        name="Electrosmith Daisy Seed Rev4",
        footprint="Module:DaisySeed",
        nets=[cv_in],
        pins=40,
    )
    jack = _Part(
        "J_CV",
        name="Thonkiconn PJ398SM panel jack",
        footprint="Connector_Audio:Thonkiconn_PJ398SM",
        nets=[cv_tip],
    )
    resistor = _Part("R_CV", value="100K", footprint="Resistor_SMD:R_0603", nets=[cv_tip, cv_in])
    circuit = _Circuit([module, jack, resistor], [cv_tip, cv_in])
    placed = [
        PlacedPart("A1", 68.0, 20.0, 0.0, "Module:DaisySeed"),
        PlacedPart("J_CV", 4.0, 20.0, 0.0, "Connector_Audio:Thonkiconn_PJ398SM"),
        PlacedPart("R_CV", 12.0, 20.0, 0.0, "Resistor_SMD:R_0603"),
    ]

    score = score_placement(placed, circuit, BBOXES, outline=BoardOutline(90.0, 40.0))

    assert any("R_CV: signal passive" in warning and "A1" in warning for warning in score.warnings)


def test_score_decoupling_cap_ignores_panel_controls_as_parents():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig = _Net("SIG")
    switch = _Part(
        "SW1",
        name="RotaryEncoder_Switch",
        footprint="Connector:Header",
        nets=[vcc, gnd, sig],
        pins=5,
    )
    cap = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    circuit = _Circuit([switch, cap], [vcc, gnd, sig])
    placed = [
        PlacedPart("SW1", 10.0, 10.0, 0.0, "Connector:Header"),
        PlacedPart("C1", 30.0, 10.0, 0.0, "Capacitor:C_0805"),
    ]

    score = score_placement(placed, circuit, BBOXES)

    assert score.role_counts["control"] == 1
    assert not any("decoupling cap" in warning for warning in score.warnings)


def test_score_accepts_clean_panel_grid_without_alignment_warning():
    gnd = _Net("GND")
    sig = _Net("SIG")
    parts = [
        _Part(
            "RV1",
            name="panel potentiometer",
            footprint="Potentiometer_THT:Potentiometer_Alpha",
            nets=[sig, gnd],
            pins=3,
        ),
        _Part(
            "RV2",
            name="panel potentiometer",
            footprint="Potentiometer_THT:Potentiometer_Alpha",
            nets=[sig, gnd],
            pins=3,
        ),
        _Part(
            "J1",
            name="Thonkiconn PJ398SM panel jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[sig, gnd],
        ),
        _Part(
            "J2",
            name="Thonkiconn PJ398SM panel jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[sig, gnd],
        ),
    ]
    circuit = _Circuit(parts, [sig, gnd])
    placed = [
        PlacedPart("RV1", 20.0, 15.0, 0.0, "Potentiometer_THT:Potentiometer_Alpha"),
        PlacedPart("RV2", 60.0, 15.0, 0.0, "Potentiometer_THT:Potentiometer_Alpha"),
        PlacedPart("J1", 20.0, 32.0, 0.0, "Connector_Audio:Thonkiconn_PJ398SM"),
        PlacedPart("J2", 60.0, 32.0, 0.0, "Connector_Audio:Thonkiconn_PJ398SM"),
    ]

    score = score_placement(
        placed,
        circuit,
        BBOXES,
        outline=BoardOutline(80.0, 50.0),
    )

    assert not any("not aligned" in warning for warning in score.warnings)
    assert not any("bunched" in warning for warning in score.warnings)


def test_score_penalizes_long_visible_front_panel_trace():
    sig = _Net("SIG")
    gnd = _Net("GND")
    jack = _Part(
        "J1",
        name="Thonkiconn PJ398SM panel jack",
        footprint="Connector_Audio:Thonkiconn_PJ398SM",
        nets=[sig, gnd],
    )
    ic = _Part("U1", name="audio processor", footprint="Package_QFP:MCU", nets=[sig, gnd])
    circuit = _Circuit([jack, ic], [sig, gnd])
    placed = [
        PlacedPart("J1", 8.0, 14.0, 0.0, "Connector_Audio:Thonkiconn_PJ398SM", side="front"),
        PlacedPart("U1", 72.0, 14.0, 0.0, "Package_QFP:MCU", side="front"),
    ]

    score = score_placement(
        placed,
        circuit,
        BBOXES,
        outline=BoardOutline(80.0, 30.0),
    )

    assert score.front_panel_trace_count == 2
    assert score.front_panel_trace_mm > 120.0
    assert any("front-panel trace span" in warning for warning in score.warnings)
    assert score.to_dict()["front_panel_trace_count"] == 2


def test_score_ignores_back_side_service_electronics_for_front_panel_trace():
    sig = _Net("SIG")
    gnd = _Net("GND")
    jack = _Part(
        "J1",
        name="Thonkiconn PJ398SM panel jack",
        footprint="Connector_Audio:Thonkiconn_PJ398SM",
        nets=[sig, gnd],
    )
    ic = _Part("U1", name="audio processor", footprint="Package_QFP:MCU", nets=[sig, gnd])
    circuit = _Circuit([jack, ic], [sig, gnd])
    placed = [
        PlacedPart("J1", 8.0, 14.0, 0.0, "Connector_Audio:Thonkiconn_PJ398SM", side="front"),
        PlacedPart("U1", 72.0, 14.0, 0.0, "Package_QFP:MCU", side="back"),
    ]

    score = score_placement(
        placed,
        circuit,
        BBOXES,
        outline=BoardOutline(80.0, 30.0),
    )

    assert score.front_panel_trace_count == 0
    assert not any("front-panel trace span" in warning for warning in score.warnings)


def test_score_does_not_treat_generic_single_sided_buttons_as_front_panel():
    en = _Net("EN")
    gnd = _Net("GND")
    switch = _Part(
        "SW1",
        name="tactile reset switch",
        footprint="Button_Switch_SMD:SW_Push",
        nets=[en, gnd],
    )
    ic = _Part("U1", name="ESP32 module", footprint="RF_Module:ESP32", nets=[en, gnd])
    circuit = _Circuit([switch, ic], [en, gnd])
    placed = [
        PlacedPart("SW1", 8.0, 14.0, 0.0, "Button_Switch_SMD:SW_Push", side="front"),
        PlacedPart("U1", 72.0, 14.0, 0.0, "RF_Module:ESP32", side="front"),
    ]

    score = score_placement(
        placed,
        circuit,
        BBOXES,
        outline=BoardOutline(80.0, 30.0),
    )

    assert score.front_panel_trace_count == 0
    assert not any("front-panel trace span" in warning for warning in score.warnings)


def test_score_counts_hard_validation_failures():
    circuit = _Circuit([], [])
    placed = [
        PlacedPart("R1", 0.0, 0.0, 0.0, "Unknown:Part"),
        PlacedPart("R2", 0.5, 0.0, 0.0, "Unknown:Part"),
    ]

    score = score_placement(placed, circuit, {}, outline=BoardOutline(1.0, 1.0))

    assert score.overlap_count == 1
    assert score.outline_violation_count == 2
    assert score.ok is False


def test_score_counts_physical_body_outline_violation():
    footprint = "Demo:PanelSwitch"
    geometry = footprint_geometry_from_sexp(
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
    circuit = _Circuit([], [])
    placed = [PlacedPart("SW1", 90.0, 15.0, 90.0, footprint)]

    score = score_placement(
        placed,
        circuit,
        {},
        outline=BoardOutline(100.0, 30.0),
        fp_geometries={footprint: geometry},
    )

    assert score.outline_violation_count == 1
    assert score.ok is False


def test_score_includes_power_plan_warnings():
    vbus = _Net("VBUS")
    vcc = _Net("VCC")
    gnd = _Net("GND")
    regulator = _Part(
        "U2",
        name="LDO regulator",
        footprint="Package_TO_SOT:SOT23",
        nets=[vbus, gnd, vcc],
        pins=3,
    )
    circuit = _Circuit([regulator], [vbus, gnd, vcc])
    placed = [PlacedPart("U2", 50.0, 50.0, 0.0, "Package_QFP:MCU")]

    score = score_placement(placed, circuit, BBOXES, board_layers=4)

    assert score.power_net_count == 3
    assert any(
        "regulator" in warning and "decoupling cap" in warning
        for warning in score.warnings
    )


def test_four_layer_board_scores_long_high_current_path_better_than_two_layer():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    j1 = _Part("J1", name="USB connector", footprint="Connector:Header", nets=[vbus, gnd])
    u1 = _Part("U1", name="load", footprint="Package_QFP:MCU", nets=[vbus, gnd], pins=4)
    circuit = _Circuit([j1, u1], [vbus, gnd])
    placed = [
        PlacedPart("J1", 0.0, 0.0, 0.0, "Connector:Header"),
        PlacedPart("U1", 100.0, 0.0, 0.0, "Package_QFP:MCU"),
    ]

    score_2_layer = score_placement(placed, circuit, BBOXES, board_layers=2)
    score_4_layer = score_placement(placed, circuit, BBOXES, board_layers=4)

    assert score_4_layer.score > score_2_layer.score
    assert score_4_layer.power_corridor_count >= 1


def _fixture_circuit_and_placement():
    """A multi-net circuit exercising HPWL, crossings, congestion and the
    dedup edge cases (a single-part 2-pin net, a >=3-ref net)."""
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig_a = _Net("SIG_A")
    sig_b = _Net("SIG_B")
    self_net = _Net("SELF")  # one part, two pins -> single distinct ref
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU",
               nets=[vcc, gnd, sig_a, sig_b], pins=6)
    c1 = _Part("C1", value="1uF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    c2 = _Part("C2", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    r1 = _Part("R1", value="10k", footprint="Resistor_SMD:R_0603", nets=[sig_a])
    r2 = _Part("R2", value="1k", footprint="Resistor_SMD:R_0603", nets=[sig_b])
    j1 = _Part("J1", name="Header", footprint="Connector:Header", nets=[sig_a, sig_b])
    # self_net: both pins on r1 -> exercises the single-distinct-ref dedup path
    r1.pins.append(_Pin(r1, self_net))
    r1.pins.append(_Pin(r1, self_net))
    circuit = _Circuit([u1, c1, c2, r1, r2, j1],
                       [vcc, gnd, sig_a, sig_b, self_net])
    placed = [
        PlacedPart("U1", 40.0, 40.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 45.0, 30.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("C2", 30.0, 45.0, 90.0, "Capacitor:C_0805"),
        PlacedPart("R1", 60.0, 20.0, 0.0, "Resistor_SMD:R_0603"),
        PlacedPart("R2", 20.0, 60.0, 0.0, "Resistor_SMD:R_0603"),
        PlacedPart("J1", 5.0, 5.0, 0.0, "Connector:Header"),
    ]
    return circuit, placed


def test_ctx_cached_scoring_is_numerically_identical():
    from skidl_layout.context import LayoutContext

    circuit, placed = _fixture_circuit_and_placement()
    outline = BoardOutline(80.0, 80.0)
    ctx = LayoutContext.from_circuit(circuit)

    no_ctx = score_placement(placed, circuit, BBOXES, outline=outline)
    with_ctx = score_placement(placed, circuit, BBOXES, outline=outline, ctx=ctx)

    for attr in (
        "penalty",
        "score",
        "total_hpwl_mm",
        "weighted_hpwl_mm",
        "crossing_count",
        "congestion_score",
        "warning_count",
    ):
        assert getattr(no_ctx, attr) == getattr(with_ctx, attr), attr
    assert no_ctx.to_dict() == with_ctx.to_dict()


def test_ctx_net_ref_lists_excludes_nc_and_single_ref_nets():
    from skidl_layout.context import LayoutContext

    circuit, _ = _fixture_circuit_and_placement()
    ctx = LayoutContext.from_circuit(circuit)
    names = {name for name, _ in ctx.net_ref_lists}
    # SELF is a single-part two-pin net -> excluded; multi-ref nets kept.
    assert "SELF" not in names
    assert {"VCC", "GND", "SIG_A", "SIG_B"} <= names
    for _name, refs in ctx.net_ref_lists:
        assert len(refs) == len(set(refs)) >= 2


def test_ctx_cached_quick_score_is_numerically_identical():
    from skidl_layout.context import LayoutContext
    from skidl_layout.scoring import score_placement_quick

    circuit, placed = _fixture_circuit_and_placement()
    outline = BoardOutline(80.0, 80.0)
    ctx = LayoutContext.from_circuit(circuit)

    no_ctx = score_placement_quick(placed, circuit, BBOXES, outline=outline)
    with_ctx = score_placement_quick(placed, circuit, BBOXES, outline=outline, ctx=ctx)
    assert no_ctx.to_dict() == with_ctx.to_dict()
