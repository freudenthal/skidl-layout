from __future__ import annotations

import pytest

from skidl_layout.constraints import BoardOutline
from skidl_layout.intent import classify_floorplan_intent_gap, infer_placement_intents


class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, net, name=""):
        self.part = part
        self.net = net
        self.name = name
        net._pins.append(self)


class _Part:
    def __init__(self, ref, value="", footprint="", name="", nets=None, pins=2,
                 pin_names=None):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.name = name
        self.node = None
        self.pins = []
        for i, net in enumerate(nets or []):
            pname = pin_names[i] if pin_names and i < len(pin_names) else ""
            self.pins.append(_Pin(self, net, name=pname))
        while len(self.pins) < pins:
            idx = len(self.pins)
            pname = pin_names[idx] if pin_names and idx < len(pin_names) else ""
            self.pins.append(_Pin(self, _Net(f"{ref}_N{idx}"), name=pname))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self.nets = nets

    def get_nets(self):
        return self.nets


def _kinds(plan, ref):
    return {intent.kind for intent in plan.intents_for(ref)}


def test_classifies_large_module_connector_board_as_floorplan_gap():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    sig = _Net("SIG")
    module = _Part(
        "U1",
        name="ESP32-S3 WROOM logger module",
        footprint="RF_Module:ESP32-S3-WROOM-1",
        nets=[vbus, gnd, sig],
        pins=44,
    )
    usb = _Part(
        "J1",
        name="USB-C connector",
        footprint="Connector_USB:USB_C_Receptacle",
        nets=[vbus, gnd],
        pins=16,
    )
    sd = _Part(
        "J2",
        name="microSD card socket",
        footprint="Connector_Card:microSD_Hirose_DM3AT",
        nets=[vbus, gnd, sig],
        pins=12,
    )
    debug = _Part(
        "J3",
        name="SWD debug header",
        footprint="Connector:PinHeader_1x04",
        nets=[gnd, sig],
        pins=4,
    )
    circuit = _Circuit([module, usb, sd, debug], [vbus, gnd, sig])

    diagnosis = classify_floorplan_intent_gap(circuit)

    assert diagnosis["needs_floorplan"] is True
    assert diagnosis["large_module_refs"] == ["U1"]
    assert {"J1", "J2", "J3"}.issubset(set(diagnosis["connector_refs"]))
    assert diagnosis["confidence"] >= 0.8


def test_floorplan_gap_classifier_respects_explicit_edge_intent():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    sig = _Net("SIG")
    module = _Part(
        "U1",
        name="ESP32-S3 WROOM logger module",
        footprint="RF_Module:ESP32-S3-WROOM-1",
        nets=[vbus, gnd, sig],
        pins=44,
    )
    usb = _Part("J1", name="USB-C connector", nets=[vbus, gnd], pins=16)
    sd = _Part("J2", name="microSD socket", nets=[vbus, gnd, sig], pins=12)
    debug = _Part("J3", name="SWD debug header", nets=[gnd, sig], pins=4)
    usb.edge_preference = "bottom"
    circuit = _Circuit([module, usb, sd, debug], [vbus, gnd, sig])

    diagnosis = classify_floorplan_intent_gap(circuit)

    assert diagnosis["needs_floorplan"] is False


def test_floorplan_gap_classifier_treats_outline_only_as_weak_intent():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    sig = _Net("SIG")
    module = _Part(
        "U1",
        name="ESP32-S3 WROOM logger module",
        footprint="RF_Module:ESP32-S3-WROOM-1",
        nets=[vbus, gnd, sig],
        pins=44,
    )
    usb = _Part("J1", name="USB-C connector", nets=[vbus, gnd], pins=16)
    sd = _Part("J2", name="microSD socket", nets=[vbus, gnd, sig], pins=12)
    debug = _Part("J3", name="SWD debug header", nets=[gnd, sig], pins=4)
    circuit = _Circuit([module, usb, sd, debug], [vbus, gnd, sig])

    diagnosis = classify_floorplan_intent_gap(
        circuit,
        floorplan_meta={"outline": "explicit"},
    )

    assert diagnosis["needs_floorplan"] is True


def test_infers_edge_connector_power_and_debug_intent():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    usb = _Part("J1", name="USB connector", footprint="Connector:USB_C", nets=[vbus, gnd], pins=16)
    debug = _Part("J2", name="SWD debug header", footprint="Connector:TagConnect", nets=[gnd], pins=6)
    circuit = _Circuit([usb, debug], [vbus, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    assert {"edge_connector", "power_input"}.issubset(_kinds(plan, "J1"))
    assert {"edge_connector", "test_debug"}.issubset(_kinds(plan, "J2"))
    usb_anchor = next(anchor for anchor in plan.edge_anchors if anchor.ref == "J1")
    debug_anchor = next(anchor for anchor in plan.edge_anchors if anchor.ref == "J2")
    assert usb_anchor.edge == "bottom"
    assert usb_anchor.offset_mm == 40.0
    assert debug_anchor.edge == "right"
    usb_mating = next(mating for mating in plan.mating_intents if mating.ref == "J1")
    debug_mating = next(mating for mating in plan.mating_intents if mating.ref == "J2")
    assert usb_mating.kind == "usb"
    assert usb_mating.edge_preference == "bottom"
    assert usb_mating.mating_side == "outside_board"
    assert 180.0 in usb_mating.allowed_rotations
    assert debug_mating.kind == "header"
    assert usb_anchor.rot_deg == 0.0
    assert usb_anchor.inset_mm == 0.0
    assert any(
        face.ref == "J1" and face.edge == "bottom" and face.rot_deg == 0.0
        for face in plan.face_edges
    )
    assert "mechanical_mating" in _kinds(plan, "J1")


def test_usb_capable_ic_does_not_get_connector_mating_intent():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    dp = _Net("USB_D+")
    dm = _Net("USB_D-")
    mcu = _Part(
        "U1",
        name="USB MIDI bridge MCU",
        footprint="Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
        nets=[vbus, gnd, dp, dm],
        pins=16,
    )
    circuit = _Circuit([mcu], [vbus, gnd, dp, dm])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    assert "edge_connector" not in _kinds(plan, "U1")
    assert "mechanical_mating" not in _kinds(plan, "U1")
    assert "power_input" not in _kinds(plan, "U1")
    assert [mating for mating in plan.mating_intents if mating.ref == "U1"] == []


def test_infers_outward_rotation_for_qwiic_edge_connector():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sda = _Net("SDA")
    scl = _Net("SCL")
    qwiic = _Part(
        "J100",
        name="Qwiic STEMMA QT JST SH connector",
        footprint="Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
        nets=[gnd, vcc, sda, scl],
        pins=4,
    )
    qwiic.edge_preference = "right"
    circuit = _Circuit([qwiic], [vcc, gnd, sda, scl])

    plan = infer_placement_intents(circuit, outline=BoardOutline(40.0, 30.0))

    anchor = next(anchor for anchor in plan.edge_anchors if anchor.ref == "J100")
    assert anchor.edge == "right"
    assert anchor.rot_deg == 90.0
    assert anchor.inset_mm == 0.0


def test_spreads_multiple_connectors_on_same_edge():
    sig = _Net("SIG")
    gnd = _Net("GND")
    headers = [
        _Part(
            f"J{i}",
            name="GPIO pin header",
            footprint="Connector:PinHeader_1x04",
            nets=[sig, gnd],
            pins=4,
        )
        for i in range(1, 4)
    ]
    circuit = _Circuit(headers, [sig, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 60.0))

    anchors = sorted(
        [anchor for anchor in plan.edge_anchors if anchor.ref in {"J1", "J2", "J3"}],
        key=lambda anchor: anchor.ref,
    )
    assert {anchor.edge for anchor in anchors} == {"right"}
    offsets = [anchor.offset_mm for anchor in anchors]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == 3
    assert all(0.0 < offset < 60.0 for offset in offsets)


def test_explicit_edge_preference_overrides_generic_connector_inference():
    sig = _Net("SIG")
    gnd = _Net("GND")
    power = _Part(
        "J1",
        name="9V barrel power input",
        footprint="Connector:BarrelJack",
        nets=[sig, gnd],
        pins=3,
    )
    output = _Part(
        "J2",
        name="audio output header",
        footprint="Connector:PinHeader_1x02",
        nets=[sig, gnd],
        pins=2,
    )
    power.edge_preference = "top"
    power.edge_offset_mm = 20.0
    power.edge_rot_deg = 180
    output.edge_preference = "right"
    circuit = _Circuit([power, output], [sig, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    anchors = {anchor.ref: anchor for anchor in plan.edge_anchors}
    assert anchors["J1"].edge == "top"
    assert anchors["J1"].offset_mm == 20.0
    assert anchors["J1"].rot_deg == 180.0
    assert anchors["J2"].edge == "right"
    assert "explicit_edge_anchor" in _kinds(plan, "J1")
    assert "explicit_edge_anchor" in _kinds(plan, "J2")
    assert any(
        face.ref == "J1" and face.edge == "top" and face.rot_deg == 180.0
        for face in plan.face_edges
    )


def test_explicit_edges_prevent_opposing_header_pair_rewrite():
    sig = _Net("SIG")
    gnd = _Net("GND")
    input_header = _Part(
        "J1",
        name="pedal input header",
        footprint="Connector:PinHeader_1x02",
        nets=[sig, gnd],
        pins=2,
    )
    output_header = _Part(
        "J2",
        name="pedal output header",
        footprint="Connector:PinHeader_1x02",
        nets=[sig, gnd],
        pins=2,
    )
    input_header.edge_preference = "top"
    output_header.edge_preference = "bottom"
    circuit = _Circuit([input_header, output_header], [sig, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    anchors = {anchor.ref: anchor for anchor in plan.edge_anchors}
    assert anchors["J1"].edge == "top"
    assert anchors["J2"].edge == "bottom"


def test_usb_c_inline_input_output_pair_gets_opposing_edges():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    dp = _Net("USB_D+")
    dm = _Net("USB_D-")
    upstream = _Part(
        "J1",
        name="USB-C IN receptacle",
        footprint="Connector_USB:USB_C_Receptacle",
        nets=[vbus, gnd, dp, dm],
        pins=16,
    )
    downstream = _Part(
        "J2",
        name="USB-C OUT receptacle",
        footprint="Connector_USB:USB_C_Receptacle",
        nets=[vbus, gnd, dp, dm],
        pins=16,
    )
    circuit = _Circuit([upstream, downstream], [vbus, gnd, dp, dm])

    plan = infer_placement_intents(circuit, outline=BoardOutline(70.0, 28.0))

    anchors = {anchor.ref: anchor for anchor in plan.edge_anchors}
    assert anchors["J1"].edge == "left"
    assert anchors["J2"].edge == "right"
    assert anchors["J1"].offset_mm == pytest.approx(14.0)
    assert anchors["J2"].offset_mm == pytest.approx(14.0)
    assert anchors["J1"].rot_deg == pytest.approx(270.0)
    assert anchors["J2"].rot_deg == pytest.approx(90.0)
    assert "opposing_inline_connector_pair" in _kinds(plan, "J1")
    assert "opposing_inline_connector_pair" in _kinds(plan, "J2")


def test_breakout_header_centered_between_mounting_holes_keeps_offset_when_edge_has_other_anchor():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sda = _Net("SDA")
    scl = _Net("SCL")
    header = _Part(
        "J1",
        name="MCP9808 breakout pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sda, scl],
        pins=6,
    )
    fixture = _Part(
        "J2",
        name="factory test connector",
        footprint="Connector:PinHeader_1x02",
        nets=[gnd],
        pins=2,
    )
    fixture.edge_preference = "top"
    fixture.edge_offset_mm = 35.0
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    h2 = _Part("H2", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    circuit = _Circuit([header, fixture, h1, h2], [vcc, gnd, sda, scl])

    plan = infer_placement_intents(circuit, outline=outline)

    anchors = {anchor.ref: anchor for anchor in plan.edge_anchors}
    assert anchors["J1"].edge == "top"
    assert anchors["J1"].offset_mm == pytest.approx(outline.width_mm / 2)
    assert anchors["J2"].offset_mm == pytest.approx(35.0)
    assert "connector_between_mounting_holes" in _kinds(plan, "J1")


def test_infers_board_ui_mating_intent():
    gnd = _Net("GND")
    button = _Part(
        "SW1",
        name="user button",
        footprint="Button_Switch_SMD:SW_SPST",
        nets=[gnd],
    )
    led = _Part("D1", name="status LED", footprint="LED_SMD:LED_0805", nets=[gnd])
    circuit = _Circuit([button, led], [gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    mating = {intent.ref: intent for intent in plan.mating_intents}
    assert mating["SW1"].kind == "button"
    assert mating["SW1"].mating_side == "user_control"
    assert mating["D1"].kind == "led"
    assert mating["D1"].mating_side == "visible_face"
    assert any(face.ref == "SW1" and face.edge == "right" for face in plan.face_edges)
    assert any(face.ref == "D1" and face.edge == "right" for face in plan.face_edges)


def test_horizontal_35mm_audio_jack_is_edge_connector():
    sig = _Net("AUDIO_OUT")
    gnd = _Net("GND")
    jack = _Part(
        "J1",
        name="3.5mm stereo headphone jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[sig, gnd],
        pins=3,
    )
    circuit = _Circuit([jack], [sig, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    assert "edge_connector" in _kinds(plan, "J1")
    assert "panel_jack" not in _kinds(plan, "J1")
    anchor = next(anchor for anchor in plan.edge_anchors if anchor.ref == "J1")
    assert anchor.edge == "right"
    mating = next(mating for mating in plan.mating_intents if mating.ref == "J1")
    assert mating.kind == "audio_jack"
    assert mating.edge_preference == "right"
    assert mating.mating_side == "outside_board"
    assert any(face.ref == "J1" and face.edge == "right" for face in plan.face_edges)


def test_vertical_pj398_jack_is_panel_subject_not_edge_connector():
    sig = _Net("AUDIO_IN")
    gnd = _Net("GND")
    jack = _Part(
        "J1",
        name="Thonkiconn PJ398SM vertical 3.5mm audio jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ398SM_Vertical",
        nets=[sig, gnd],
        pins=3,
    )
    circuit = _Circuit([jack], [sig, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    assert "panel_jack" in _kinds(plan, "J1")
    assert "front_panel_subject" in _kinds(plan, "J1")
    assert "edge_connector" not in _kinds(plan, "J1")
    assert all(anchor.ref != "J1" for anchor in plan.edge_anchors)
    mating = next(mating for mating in plan.mating_intents if mating.ref == "J1")
    assert mating.kind == "panel_jack"
    assert mating.edge_preference is None
    assert mating.mating_side == "front_panel"


def test_guitar_pedal_panel_jack_and_footswitch_are_panel_subjects():
    sig = _Net("AUDIO_IN")
    gnd = _Net("GND")
    jack = _Part(
        "J1",
        name="guitar pedal 1/4 inch panel input jack",
        footprint="Connector_Audio:Jack_6.35mm_Neutrik_NMJ4HCD2",
        nets=[sig, gnd],
        pins=3,
    )
    footswitch = _Part(
        "SW1",
        name="guitar pedal latching footswitch",
        footprint="Button_Switch_THT:SW_3PDT_Stomp",
        nets=[sig, gnd],
        pins=6,
    )
    circuit = _Circuit([jack, footswitch], [sig, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(60.0, 110.0))

    assert "panel_jack" in _kinds(plan, "J1")
    assert "front_panel_subject" in _kinds(plan, "J1")
    assert "edge_connector" not in _kinds(plan, "J1")
    assert "panel_control" in _kinds(plan, "SW1")
    assert "front_panel_subject" in _kinds(plan, "SW1")
    assert all(anchor.ref != "J1" for anchor in plan.edge_anchors)
    jack_mating = next(mating for mating in plan.mating_intents if mating.ref == "J1")
    switch_mating = next(mating for mating in plan.mating_intents if mating.ref == "SW1")
    assert jack_mating.kind == "panel_jack"
    assert jack_mating.mating_side == "front_panel"
    assert switch_mating.kind == "button"
    assert switch_mating.mating_side == "user_control"


def test_cui_sj1_horizontal_audio_jack_uses_local_y_socket_exit():
    sig = _Net("AUDIO_OUT")
    gnd = _Net("GND")
    jack = _Part(
        "J1",
        name="3.5mm stereo headphone jack",
        footprint="Connector_Audio:Jack_3.5mm_CUI_SJ1-3523N_Horizontal",
        nets=[sig, gnd],
        pins=3,
    )
    circuit = _Circuit([jack], [sig, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    anchor = next(anchor for anchor in plan.edge_anchors if anchor.ref == "J1")
    assert anchor.edge == "right"
    assert anchor.rot_deg == 90.0

    jack.edge_preference = "left"
    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))
    anchor = next(anchor for anchor in plan.edge_anchors if anchor.ref == "J1")
    assert anchor.edge == "left"
    assert anchor.rot_deg == 270.0


def test_eurorack_power_treats_audio_jacks_as_panel_subjects_with_single_sided_default():
    plus12 = _Net("+12V")
    minus12 = _Net("-12V")
    gnd = _Net("GND")
    sig = _Net("VCO_OUT")
    power = _Part(
        "J10",
        name="Eurorack shrouded IDC power header",
        footprint="Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical",
        nets=[plus12, minus12, gnd],
        pins=10,
    )
    jack = _Part(
        "J1",
        name="3.5mm mono output jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[sig, gnd],
        pins=3,
    )
    circuit = _Circuit([power, jack], [plus12, minus12, gnd, sig])

    plan = infer_placement_intents(circuit, outline=BoardOutline(40.0, 120.0))

    assert "panel_jack" in _kinds(plan, "J1")
    assert "front_panel_subject" in _kinds(plan, "J1")
    assert "edge_connector" not in _kinds(plan, "J1")
    assert all(anchor.ref != "J1" for anchor in plan.edge_anchors)
    jack_mating = next(mating for mating in plan.mating_intents if mating.ref == "J1")
    power_mating = next(mating for mating in plan.mating_intents if mating.ref == "J10")
    assert jack_mating.kind == "panel_jack"
    assert jack_mating.mating_side == "front_panel"
    assert power_mating.kind == "eurorack_power"
    assert power_mating.edge_preference == "bottom"
    assert all(anchor.ref != "J10" for anchor in plan.edge_anchors)
    assert any("Thonkiconn/PJ398" in warning for warning in plan.warnings)
    assert plan.assembly_policy == "single_sided"
    assert plan.assembly_sides["J1"] == "front"
    assert plan.assembly_sides["J10"] == "front"
    assert "front_assembly" in _kinds(plan, "J1")
    assert "front_assembly" in _kinds(plan, "J10")
    assert "back_assembly" not in _kinds(plan, "J10")
    assert "assembly sides: front: 2" in plan.summary()
    assert any("single_sided policy avoids" in warning for warning in plan.warnings)


def test_eurorack_double_sided_policy_keeps_panel_front_and_electronics_back():
    plus12 = _Net("+12V")
    minus12 = _Net("-12V")
    gnd = _Net("GND")
    sig = _Net("VCO_OUT")
    cv = _Net("CV_IN")
    power = _Part(
        "J10",
        name="Eurorack shrouded IDC power header",
        footprint="Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical",
        nets=[plus12, minus12, gnd],
        pins=10,
    )
    jack = _Part(
        "J1",
        name="3.5mm mono output jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[sig, gnd],
        pins=3,
    )
    pot = _Part(
        "RV1",
        name="front panel potentiometer control",
        footprint="Potentiometer_THT:Potentiometer_Alpha",
        nets=[plus12, cv, gnd],
        pins=3,
    )
    ic = _Part(
        "U1",
        name="VCO core IC",
        footprint="Package_SO:SOIC-8",
        nets=[plus12, minus12, gnd, sig],
        pins=8,
    )
    decap = _Part(
        "C1",
        value="100nF",
        footprint="Capacitor_SMD:C_0603",
        nets=[plus12, gnd],
    )
    resistor = _Part(
        "R1",
        value="100K",
        footprint="Resistor_SMD:R_0603",
        nets=[cv, sig],
    )
    circuit = _Circuit(
        [power, jack, pot, ic, decap, resistor],
        [plus12, minus12, gnd, sig, cv],
    )

    plan = infer_placement_intents(
        circuit,
        outline=BoardOutline(40.0, 120.0),
        assembly_policy="double_sided",
    )

    assert plan.assembly_policy == "double_sided"
    assert plan.assembly_sides["J1"] == "front"
    assert plan.assembly_sides["RV1"] == "front"
    assert plan.assembly_sides["J10"] == "back"
    assert plan.assembly_sides["U1"] == "back"
    assert plan.assembly_sides["C1"] == "back"
    assert plan.assembly_sides["R1"] == "back"
    assert "front_assembly" in _kinds(plan, "J1")
    assert "front_assembly" in _kinds(plan, "RV1")
    assert "back_assembly" in _kinds(plan, "J10")
    assert "back_assembly" in _kinds(plan, "U1")
    assert "back_assembly" in _kinds(plan, "C1")
    assert "back_assembly" in _kinds(plan, "R1")
    assert "assembly sides: back: 4, front: 2" in plan.summary()
    assert not any("single_sided policy avoids" in warning for warning in plan.warnings)


def test_eurorack_lfo_panel_parts_are_grid_subjects_not_edge_anchors():
    plus12 = _Net("+12V")
    minus12 = _Net("-12V")
    gnd = _Net("GND")
    vco_out = _Net("VCO_OUT")
    lfo_out = _Net("LFO_OUT")
    rate_cv = _Net("RATE_CV")
    depth_cv = _Net("DEPTH_CV")
    led_drive = _Net("LED_DRIVE")
    power = _Part(
        "J10",
        name="Eurorack shrouded IDC power header",
        footprint="Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical",
        nets=[plus12, minus12, gnd],
        pins=10,
    )
    jacks = [
        _Part(
            "J1",
            name="3.5mm mono VCO output jack",
            footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
            nets=[vco_out, gnd],
            pins=3,
        ),
        _Part(
            "J2",
            name="3.5mm mono LFO output jack",
            footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
            nets=[lfo_out, gnd],
            pins=3,
        ),
    ]
    controls = [
        _Part(
            "RV1",
            name="front panel rate potentiometer",
            footprint="Potentiometer_THT:Potentiometer_Alpha_RD901F",
            nets=[plus12, rate_cv, gnd],
            pins=3,
        ),
        _Part(
            "RV2",
            name="front panel depth potentiometer",
            footprint="Potentiometer_THT:Potentiometer_Alpha_RD901F",
            nets=[plus12, depth_cv, gnd],
            pins=3,
        ),
    ]
    led = _Part(
        "D1",
        name="front panel LFO LED indicator",
        footprint="LED_THT:LED_D3.0mm",
        nets=[led_drive, gnd],
        pins=2,
    )
    ic = _Part(
        "U1",
        name="TL074 analog LFO core",
        footprint="Package_SO:SOIC-14",
        nets=[plus12, minus12, gnd, vco_out, lfo_out, rate_cv, depth_cv, led_drive],
        pins=14,
    )
    decap = _Part(
        "C1",
        value="100nF",
        footprint="Capacitor_SMD:C_0603",
        nets=[plus12, gnd],
    )
    circuit = _Circuit(
        [power, *jacks, *controls, led, ic, decap],
        [plus12, minus12, gnd, vco_out, lfo_out, rate_cv, depth_cv, led_drive],
    )

    plan = infer_placement_intents(
        circuit,
        outline=BoardOutline(40.0, 128.5),
        assembly_policy="double_sided",
    )

    edge_refs = {anchor.ref for anchor in plan.edge_anchors}
    face_refs = {face.ref for face in plan.face_edges}
    panel_refs = {"J1", "J2", "RV1", "RV2", "D1"}

    assert panel_refs.isdisjoint(edge_refs)
    assert panel_refs.isdisjoint(face_refs)
    for ref in panel_refs:
        assert "front_panel_subject" in _kinds(plan, ref) or ref == "D1"
        assert "panel_grid_subject" in _kinds(plan, ref)
        assert "array_subject" in _kinds(plan, ref)

    for ref in {"J1", "J2"}:
        mating = next(mating for mating in plan.mating_intents if mating.ref == ref)
        assert mating.kind == "panel_jack"
        assert mating.edge_preference is None
        assert mating.mating_side == "front_panel"
        assert "panel_jack" in _kinds(plan, ref)
        assert "edge_connector" not in _kinds(plan, ref)

    assert all(anchor.ref != "J10" for anchor in plan.edge_anchors)
    assert "bottom_back_mechanical_context" in _kinds(plan, "J10")
    assert "rear_mechanical_context" in _kinds(plan, "J10")
    assert plan.assembly_sides["J10"] == "back"
    assert plan.assembly_sides["U1"] == "back"
    assert plan.assembly_sides["C1"] == "back"
    assert any("non-panel footprint" in warning for warning in plan.warnings)

    align_by_refs = {frozenset(c.refs): c for c in plan.align_constraints}
    distribute_by_refs = {frozenset(c.refs): c for c in plan.distribute_constraints}
    jack_refs = frozenset({"J1", "J2"})
    control_refs = frozenset({"RV1", "RV2"})
    assert align_by_refs[jack_refs].axis == "x"
    assert align_by_refs[control_refs].axis == "x"
    assert distribute_by_refs[jack_refs].axis == "y"
    assert distribute_by_refs[control_refs].axis == "y"


def test_single_sided_policy_allows_only_explicit_back_side_override():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    u1 = _Part(
        "U1",
        name="MCU",
        footprint="Package_QFP:TQFP-32",
        nets=[vcc, gnd],
        pins=32,
    )
    u1.assembly_side = "back"
    circuit = _Circuit([u1], [vcc, gnd])

    single = infer_placement_intents(circuit, outline=BoardOutline(40.0, 30.0))
    double = infer_placement_intents(
        circuit,
        outline=BoardOutline(40.0, 30.0),
        assembly_policy="double_sided",
    )

    assert single.assembly_policy == "single_sided"
    assert single.assembly_sides["U1"] == "back"
    assert "back_assembly" in _kinds(single, "U1")
    assert "front_assembly" not in _kinds(single, "U1")
    assert any("overrides single_sided policy" in warning for warning in single.warnings)
    assert double.assembly_sides["U1"] == "back"
    assert "back_assembly" in _kinds(double, "U1")


def test_eurorack_defaults_respect_explicit_double_sided_part_side():
    plus12 = _Net("+12V")
    gnd = _Net("GND")
    sig = _Net("VCO_OUT")
    power = _Part(
        "J10",
        name="Eurorack power header",
        footprint="Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical",
        nets=[plus12, gnd],
        pins=10,
    )
    jack = _Part(
        "J1",
        name="3.5mm mono output jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ398SM_Vertical",
        nets=[sig, gnd],
        pins=3,
    )
    jack.assembly_side = "back"
    circuit = _Circuit([power, jack], [plus12, gnd, sig])

    plan = infer_placement_intents(
        circuit,
        outline=BoardOutline(40.0, 120.0),
        assembly_policy="double_sided",
    )

    assert plan.assembly_sides["J1"] == "back"
    assert "back_assembly" in _kinds(plan, "J1")
    assert plan.assembly_sides["J10"] == "back"


def test_repeated_visible_parts_get_kind_grouped_array_constraints():
    gnd = _Net("GND")
    signal = _Net("SIG")
    leds = [
        _Part(
            f"D{idx}",
            name="status LED",
            footprint="LED_SMD:LED_0805",
            nets=[signal, gnd],
        )
        for idx in range(1, 4)
    ]
    switches = [
        _Part(
            f"SW{idx}",
            name="user switch",
            footprint="Button_Switch_SMD:SW_SPST",
            nets=[signal, gnd],
        )
        for idx in range(1, 4)
    ]
    circuit = _Circuit([*leds, *switches], [signal, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    led_refs = frozenset({"D1", "D2", "D3"})
    switch_refs = frozenset({"SW1", "SW2", "SW3"})
    align_by_refs = {frozenset(c.refs): c for c in plan.align_constraints}
    distribute_by_refs = {frozenset(c.refs): c for c in plan.distribute_constraints}

    assert led_refs in align_by_refs
    assert switch_refs in align_by_refs
    assert led_refs in distribute_by_refs
    assert switch_refs in distribute_by_refs
    assert align_by_refs[led_refs].value_mm != align_by_refs[switch_refs].value_mm
    for ref in set(led_refs) | set(switch_refs):
        assert "array_subject" in _kinds(plan, ref)


def test_repeated_panel_pots_get_single_row_constraints():
    gnd = _Net("GND")
    signal = _Net("CV")
    pots = [
        _Part(
            f"RV{idx}",
            name="panel potentiometer",
            footprint="Potentiometer_THT:Potentiometer_Alpha",
            nets=[signal, gnd],
            pins=3,
        )
        for idx in range(1, 5)
    ]
    circuit = _Circuit(pots, [signal, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(100.0, 42.0))

    refs = ["RV1", "RV2", "RV3", "RV4"]
    assert any(
        constraint.refs == refs and constraint.axis == "y"
        for constraint in plan.align_constraints
    )
    assert any(
        constraint.refs == refs
        and constraint.axis == "x"
        and round(constraint.start_mm, 6) == 14.0
        and constraint.end_mm == 86.0
        for constraint in plan.distribute_constraints
    )
    assert all("array_subject" in _kinds(plan, ref) for ref in refs)


def test_repeated_key_switches_get_matrix_constraints():
    gnd = _Net("GND")
    row = _Net("ROW")
    keys = [
        _Part(
            f"K{idx}",
            name="Cherry MX key switch",
            footprint="Button_Switch_Keyboard:SW_Cherry_MX",
            nets=[row, gnd],
        )
        for idx in range(1, 7)
    ]
    circuit = _Circuit(keys, [row, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(90.0, 60.0))

    row_aligns = [
        constraint
        for constraint in plan.align_constraints
        if constraint.axis == "y" and len(constraint.refs) == 3
    ]
    column_aligns = [
        constraint
        for constraint in plan.align_constraints
        if constraint.axis == "x" and len(constraint.refs) == 2
    ]
    assert {tuple(constraint.refs) for constraint in row_aligns} >= {
        ("K1", "K2", "K3"),
        ("K4", "K5", "K6"),
    }
    assert {tuple(constraint.refs) for constraint in column_aligns} >= {
        ("K1", "K4"),
        ("K2", "K5"),
        ("K3", "K6"),
    }
    key_refs = [f"K{i}" for i in range(1, 7)]
    assert all("array_subject" in _kinds(plan, ref) for ref in key_refs)


def test_repeated_sensor_parts_get_matrix_constraints():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sda = _Net("SDA")
    sensors = [
        _Part(
            f"U{idx}",
            name="MCP9808 temperature sensor",
            footprint="Sensor:MCP9808_Breakout",
            nets=[vcc, gnd, sda],
            pins=8,
        )
        for idx in range(1, 10)
    ]
    circuit = _Circuit(sensors, [vcc, gnd, sda])

    plan = infer_placement_intents(circuit, outline=BoardOutline(90.0, 60.0))

    row_aligns = [
        constraint
        for constraint in plan.align_constraints
        if constraint.axis == "y" and len(constraint.refs) == 3
    ]
    column_aligns = [
        constraint
        for constraint in plan.align_constraints
        if constraint.axis == "x" and len(constraint.refs) == 3
    ]
    assert {tuple(constraint.refs) for constraint in row_aligns} >= {
        ("U1", "U2", "U3"),
        ("U4", "U5", "U6"),
        ("U7", "U8", "U9"),
    }
    assert {tuple(constraint.refs) for constraint in column_aligns} >= {
        ("U1", "U4", "U7"),
        ("U2", "U5", "U8"),
        ("U3", "U6", "U9"),
    }
    sensor_refs = [f"U{i}" for i in range(1, 10)]
    assert all("sensor_grid_subject" in _kinds(plan, ref) for ref in sensor_refs)
    assert all("array_subject" in _kinds(plan, ref) for ref in sensor_refs)


def test_tall_panel_visible_parts_get_column_constraints():
    gnd = _Net("GND")
    signal = _Net("SIG")
    controls = [
        _Part(
            f"RV{idx}",
            name="panel potentiometer",
            footprint="Potentiometer_THT:Potentiometer_Alpha",
            nets=[signal, gnd],
        )
        for idx in range(1, 3)
    ]
    jacks = [
        _Part(
            f"J{idx}",
            name="Thonkiconn PJ398SM 3.5mm audio jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[signal, gnd],
        )
        for idx in range(1, 4)
    ]
    circuit = _Circuit([*controls, *jacks], [signal, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(36.0, 118.0))

    control_refs = frozenset({"RV1", "RV2"})
    jack_refs = frozenset({"J1", "J2", "J3"})
    align_by_refs = {frozenset(c.refs): c for c in plan.align_constraints}
    distribute_by_refs = {frozenset(c.refs): c for c in plan.distribute_constraints}

    assert align_by_refs[control_refs].axis == "x"
    assert align_by_refs[jack_refs].axis == "x"
    assert align_by_refs[control_refs].value_mm != align_by_refs[jack_refs].value_mm
    assert distribute_by_refs[control_refs].axis == "y"
    assert distribute_by_refs[jack_refs].axis == "y"
    assert distribute_by_refs[jack_refs].end_mm - distribute_by_refs[jack_refs].start_mm > 60.0


def test_compact_four_panel_jacks_use_source_mined_2x2_grid():
    gnd = _Net("GND")
    signal = _Net("SIG")
    jacks = [
        _Part(
            f"J{idx}",
            name="Thonkiconn PJ398SM 3.5mm audio jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[signal, gnd],
        )
        for idx in range(1, 5)
    ]
    circuit = _Circuit(jacks, [signal, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(30.0, 39.6))

    assert "selected corpus-derived compact 2x2 panel jack template" in plan.warnings
    assert all("panel_template" in _kinds(plan, f"J{idx}") for idx in range(1, 5))
    row_aligns = [
        constraint
        for constraint in plan.align_constraints
        if constraint.axis == "y" and len(constraint.refs) == 2
    ]
    col_aligns = [
        constraint
        for constraint in plan.align_constraints
        if constraint.axis == "x" and len(constraint.refs) == 2
    ]
    assert {tuple(constraint.refs) for constraint in row_aligns} >= {
        ("J1", "J2"),
        ("J3", "J4"),
    }
    assert {tuple(constraint.refs) for constraint in col_aligns} >= {
        ("J1", "J3"),
        ("J2", "J4"),
    }
    assert any(
        constraint.axis == "x"
        and constraint.refs == ["J1", "J2"]
        and constraint.start_mm == 7.5
        and constraint.end_mm == 22.5
        for constraint in plan.distribute_constraints
    )
    assert any(
        constraint.axis == "y"
        and constraint.refs == ["J1", "J3"]
        and round(constraint.start_mm, 3) == 13.464
        and round(constraint.end_mm, 3) == 26.136
        for constraint in plan.distribute_constraints
    )


def test_long_panel_jacks_use_source_mined_two_row_grid():
    gnd = _Net("GND")
    signal = _Net("SIG")
    jacks = [
        _Part(
            f"J{idx}",
            name="Thonkiconn PJ398SM 3.5mm audio jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[signal, gnd],
        )
        for idx in range(1, 9)
    ]
    circuit = _Circuit(jacks, [signal, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(180.0, 39.6))

    assert "selected corpus-derived long two-row panel jack template" in plan.warnings
    top_row = ("J1", "J2", "J3", "J4")
    bottom_row = ("J5", "J6", "J7", "J8")
    align_by_refs = {tuple(constraint.refs): constraint for constraint in plan.align_constraints}
    distribute_by_refs = {
        tuple(constraint.refs): constraint
        for constraint in plan.distribute_constraints
        if constraint.axis == "x"
    }
    assert top_row in align_by_refs
    assert bottom_row in align_by_refs
    assert align_by_refs[top_row].axis == "y"
    assert align_by_refs[bottom_row].axis == "y"
    assert align_by_refs[top_row].value_mm < align_by_refs[bottom_row].value_mm
    assert distribute_by_refs[top_row].start_mm == 9.9
    assert distribute_by_refs[top_row].end_mm == 170.1
    assert distribute_by_refs[bottom_row].start_mm == 9.9
    assert distribute_by_refs[bottom_row].end_mm == 170.1


def test_simple_ic_board_adds_near_constraints_for_passives():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig = _Net("SIG")
    u1 = _Part("U1", name="sensor IC", footprint="Package_SO:SOIC-8", nets=[vcc, gnd, sig], pins=8)
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0603", nets=[vcc, gnd])
    r1 = _Part("R1", value="10k", footprint="Resistor:R_0603", nets=[sig, gnd])
    circuit = _Circuit([u1, c1, r1], [vcc, gnd, sig])

    plan = infer_placement_intents(circuit, outline=BoardOutline(40.0, 25.0))

    near_pairs = {(constraint.ref, constraint.target_ref) for constraint in plan.near_constraints}
    assert ("C1", "U1") in near_pairs
    assert ("R1", "U1") in near_pairs


def test_daisy_seed_is_internal_module_socket_not_edge_header():
    vin = _Net("VIN")
    gnd = _Net("GND")
    sig = _Net("AUDIO_OUT_L")
    daisy = _Part(
        "J1",
        value="Daisy Seed",
        name="Conn_02x20_Counter_Clockwise",
        footprint="Module:Electrosmith_Daisy_Seed",
        nets=[vin, gnd, sig],
        pins=40,
    )
    r1 = _Part("R1", value="100R", footprint="Resistor:R_0603", nets=[sig, gnd])
    circuit = _Circuit([daisy, r1], [vin, gnd, sig])

    plan = infer_placement_intents(circuit, outline=BoardOutline(90.0, 60.0))

    mating = next(intent for intent in plan.mating_intents if intent.ref == "J1")
    assert mating.kind == "module_socket"
    assert mating.mating_side == "plug_in_module"
    assert mating.edge_preference is None
    assert "module_socket" in _kinds(plan, "J1")
    assert "internal_connector" in _kinds(plan, "J1")
    assert "edge_connector" not in _kinds(plan, "J1")
    assert not any(anchor.ref == "J1" for anchor in plan.edge_anchors)
    assert not any(face.ref == "J1" for face in plan.face_edges)
    assert ("R1", "J1") in {
        (constraint.ref, constraint.target_ref)
        for constraint in plan.near_constraints
    }


def test_generic_module_pin_socket_is_not_edge_anchored():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    io1 = _Net("IO1")
    io2 = _Net("IO2")
    module = _Part(
        "U_MODULE",
        value="Generic controller module socket",
        name="Conn_02x05_Odd_Even module socket",
        footprint="Connector_PinSocket_2.54mm:PinSocket_2x05_P2.54mm_Vertical",
        nets=[vcc, gnd, io1, io2],
        pins=10,
    )
    left = _Part(
        "J_LEFT",
        name="Left expansion header",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
        nets=[vcc, gnd, io1, io2],
        pins=4,
    )
    circuit = _Circuit([module, left], [vcc, gnd, io1, io2])

    plan = infer_placement_intents(circuit, outline=BoardOutline(70.0, 35.0))

    mating = next(intent for intent in plan.mating_intents if intent.ref == "U_MODULE")
    assert mating.kind == "module_socket"
    assert mating.edge_preference is None
    assert "module_socket" in _kinds(plan, "U_MODULE")
    assert "internal_connector" in _kinds(plan, "U_MODULE")
    assert "edge_connector" not in _kinds(plan, "U_MODULE")
    assert "rf_module" not in _kinds(plan, "U_MODULE")
    assert not any(anchor.ref == "U_MODULE" for anchor in plan.edge_anchors)
    assert not any(face.ref == "U_MODULE" for face in plan.face_edges)
    assert any(anchor.ref == "J_LEFT" for anchor in plan.edge_anchors)


def test_infers_mux_and_repeated_channel_intent():
    ch0 = _Net("CH0_SIG")
    ch1 = _Net("CH1_SIG")
    ch2 = _Net("CH2_SIG")
    ch3 = _Net("CH3_SIG")
    mux = _Part(
        "U1",
        name="analog mux",
        footprint="Package_QFN:MUX",
        nets=[ch0, ch1, ch2, ch3],
        pins=4,
    )
    sensors = [
        _Part("U2", name="sensor", footprint="Sensor:S", nets=[ch0], pins=3),
        _Part("U3", name="sensor", footprint="Sensor:S", nets=[ch1], pins=3),
    ]
    circuit = _Circuit([mux, *sensors], [ch0, ch1, ch2, ch3])

    plan = infer_placement_intents(circuit)

    assert "mux_bank_controller" in _kinds(plan, "U1")
    assert len(plan.repeated_channels) == 1
    assert plan.repeated_channels[0].channel_numbers == [0, 1, 2, 3]
    assert plan.repeated_channels[0].refs_by_channel[0] == ["U1", "U2"]
    assert plan.repeated_channels[0].refs_by_channel[1] == ["U1", "U3"]
    assert {"U1", "U2", "U3"}.issubset(set(plan.repeated_channels[0].refs))
    assert plan.repeated_channels[0].shared_refs == ["U1"]
    assert plan.repeated_channels[0].controller_refs == ["U1"]
    slots = {slot.channel_number: slot for slot in plan.repeated_channels[0].slots}
    assert slots[0].sensor_refs == ["U2"]
    assert slots[1].sensor_refs == ["U3"]
    assert slots[2].refs == []


def test_repeated_channel_intent_assigns_numbered_shared_rail_decaps_to_slots():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    ch0 = _Net("CH0_SIG")
    ch1 = _Net("CH1_SIG")
    mux = _Part(
        "U1",
        name="analog mux",
        footprint="Package_QFN:MUX",
        nets=[ch0, ch1, vcc, gnd],
        pins=4,
    )
    sensor0 = _Part(
        "U2",
        name="sensor CH0",
        footprint="Sensor:S",
        nets=[ch0, vcc, gnd],
        pins=3,
    )
    sensor1 = _Part(
        "U3",
        name="sensor CH1",
        footprint="Sensor:S",
        nets=[ch1, vcc, gnd],
        pins=3,
    )
    shared_decap = _Part("C1", value="100nF", footprint="Cap:C", nets=[vcc, gnd])
    decap0 = _Part("C2", value="100nF", footprint="Cap:C", nets=[vcc, gnd])
    decap1 = _Part("C3", value="100nF", footprint="Cap:C", nets=[vcc, gnd])
    circuit = _Circuit(
        [mux, sensor0, shared_decap, decap0, sensor1, decap1],
        [ch0, ch1, vcc, gnd],
    )

    plan = infer_placement_intents(circuit)

    assert len(plan.repeated_channels) == 1
    slots = {slot.channel_number: slot for slot in plan.repeated_channels[0].slots}
    assert "C2" in slots[0].refs
    assert "C3" in slots[1].refs
    assert "C1" not in slots[0].refs
    assert "C1" not in slots[1].refs
    near_pairs = {
        (constraint.ref, constraint.target_ref, constraint.distance_mm)
        for constraint in plan.near_constraints
    }
    assert ("C2", "U2", 5.0) in near_pairs
    assert ("C3", "U3", 5.0) in near_pairs
    assert not any(ref == "C1" for ref, _, _ in near_pairs)


# ---------------------------------------------------------------------------
# Task 2: Display + controls co-location
# ---------------------------------------------------------------------------


class TestDisplayControlsColocation:
    """Tests for display FPC detection, nav switch detection, and co-location."""

    def test_display_fpc_gets_top_edge(self):
        """FPC connector with display nets should get top edge, not bottom."""
        eink_cs = _Net("EINK_CS")
        eink_dc = _Net("EINK_DC")
        eink_busy = _Net("EINK_BUSY")
        eink_mosi = _Net("EINK_MOSI")
        gnd = _Net("GND")
        fpc = _Part(
            "J3",
            name="FPC connector",
            footprint="Connector:FPC_24",
            nets=[eink_cs, eink_dc, eink_busy, eink_mosi, gnd],
            pins=24,
        )
        circuit = _Circuit([fpc], [eink_cs, eink_dc, eink_busy, eink_mosi, gnd])

        plan = infer_placement_intents(circuit, outline=BoardOutline(60.0, 40.0))

        mating = next(m for m in plan.mating_intents if m.ref == "J3")
        assert mating.kind == "ffc"
        assert mating.edge_preference == "top", (
            f"Display FPC should be top edge, got {mating.edge_preference}"
        )
        assert "display" in " ".join(mating.reasons).lower()

    def test_display_and_navswitch_same_edge(self):
        """Display FPC and nav switch should be co-located on same edge."""
        eink_dc = _Net("EINK_DC")
        eink_busy = _Net("EINK_BUSY")
        gnd = _Net("GND")
        fpc = _Part(
            "J3",
            name="FPC connector",
            footprint="Connector:FPC_24",
            nets=[eink_dc, eink_busy, gnd],
            pins=24,
        )
        nav = _Part(
            "SW2",
            name="5-way nav switch",
            footprint="Button:JS1300",
            nets=[gnd],
            pins=6,
        )
        circuit = _Circuit([fpc, nav], [eink_dc, eink_busy, gnd])

        plan = infer_placement_intents(circuit, outline=BoardOutline(60.0, 40.0))

        fpc_mating = next(m for m in plan.mating_intents if m.ref == "J3")
        nav_mating = next(m for m in plan.mating_intents if m.ref == "SW2")
        assert fpc_mating.edge_preference == nav_mating.edge_preference, (
            f"Display ({fpc_mating.edge_preference}) and nav switch "
            f"({nav_mating.edge_preference}) should share the same edge"
        )
        # Both should be on the display's preferred edge (top).
        assert fpc_mating.edge_preference == "top"

    def test_controls_follow_display_edge(self):
        """When display is top-edge, buttons/encoders should also be top-edge
        and an AlignConstraint should be emitted."""
        disp_dc = _Net("DISP_DC")
        gnd = _Net("GND")
        fpc = _Part(
            "J3",
            name="FPC connector",
            footprint="Connector:FPC_24",
            nets=[disp_dc, gnd],
            pins=24,
        )
        btn = _Part(
            "SW1",
            name="tact switch",
            footprint="Button_Switch_SMD:SW_SPST",
            nets=[gnd],
        )
        enc = _Part(
            "SW3",
            name="rotary encoder",
            footprint="Encoder:EC11",
            nets=[gnd],
            pins=5,
        )
        circuit = _Circuit([fpc, btn, enc], [disp_dc, gnd])

        plan = infer_placement_intents(circuit, outline=BoardOutline(60.0, 40.0))

        # All controls should follow the display edge.
        fpc_mating = next(m for m in plan.mating_intents if m.ref == "J3")
        btn_mating = next(m for m in plan.mating_intents if m.ref == "SW1")
        enc_mating = next(m for m in plan.mating_intents if m.ref == "SW3")
        assert fpc_mating.edge_preference == "top"
        assert btn_mating.edge_preference == "top"
        assert enc_mating.edge_preference == "top"

        # AlignConstraint should be emitted with all refs.
        assert len(plan.align_constraints) >= 1
        align = plan.align_constraints[0]
        assert "J3" in align.refs
        assert "SW1" in align.refs
        assert "SW3" in align.refs
        # Top/bottom edge => y-axis alignment.
        assert align.axis == "y"

    def test_nav_switch_detected_as_control(self):
        """JS1300-style nav switch should match as nav_control kind."""
        gnd = _Net("GND")
        nav = _Part(
            "SW2",
            name="5-way joystick nav switch",
            footprint="Button:JS1300",
            nets=[gnd],
            pins=6,
        )
        circuit = _Circuit([nav], [gnd])

        plan = infer_placement_intents(circuit)

        mating = next(m for m in plan.mating_intents if m.ref == "SW2")
        assert mating.kind == "nav_control"
        assert mating.mating_side == "user_control"
        assert mating.edge_preference is not None


# ---------------------------------------------------------------------------
# Task 1: RF path clustering and antenna edge anchoring
# ---------------------------------------------------------------------------


def test_coaxial_gets_edge_anchor():
    """Conn_Coaxial should be inferred as edge-anchored on the top edge."""
    ant_net = _Net("ANT")
    gnd = _Net("GND")
    coax = _Part(
        "J1",
        name="Conn_Coaxial",
        footprint="Connector_Coaxial:SMA_Amphenol",
        nets=[ant_net, gnd],
        pins=2,
        pin_names=["Signal", "GND"],
    )
    rf_ic = _Part(
        "U1",
        name="Si4684 RF receiver",
        footprint="Package_QFN:QFN-20",
        nets=[ant_net, gnd],
        pins=20,
        pin_names=["ANT_IN", "GND"],
    )
    circuit = _Circuit([coax, rf_ic], [ant_net, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(60.0, 40.0))

    # EdgeAnchor emitted for the coaxial ref
    coax_anchors = [a for a in plan.edge_anchors if a.ref == "J1"]
    assert len(coax_anchors) >= 1, "Expected EdgeAnchor for coaxial connector"
    assert coax_anchors[0].edge == "top"

    # Mating intent emitted
    coax_mating = [m for m in plan.mating_intents if m.ref == "J1" and m.kind == "coaxial"]
    assert len(coax_mating) >= 1, "Expected coaxial mating intent"
    assert coax_mating[0].edge_preference == "top"
    assert coax_mating[0].mating_side == "outside_board"

    # FaceEdge constraint
    assert any(fe.ref == "J1" and fe.edge == "top" for fe in plan.face_edges)


def test_rf_module_with_antenna_metadata_is_not_a_coax_edge_connector():
    """RF modules may mention antennas, but they should not be edge-anchored."""
    ant_net = _Net("ANT")
    gnd = _Net("GND")
    module = _Part(
        "U3",
        name="ESP32-S3 RF module with integrated PCB antenna",
        footprint="RF_Module:ESP32-S2-MINI-1",
        nets=[ant_net, gnd],
        pins=20,
        pin_names=["ANT", "GND"],
    )
    circuit = _Circuit([module], [ant_net, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(65.0, 40.0))

    assert "rf_module" in _kinds(plan, "U3")
    assert "edge_connector" not in _kinds(plan, "U3")
    assert [anchor for anchor in plan.edge_anchors if anchor.ref == "U3"] == []
    assert [mating for mating in plan.mating_intents if mating.ref == "U3"] == []


def test_explicit_sma_edge_preference_survives_rf_inference():
    """Explicit human/agent edge floorplan wins over default coax top-edge policy."""
    ant_net = _Net("ANT")
    gnd = _Net("GND")
    coax = _Part(
        "J3",
        name="SMA antenna connector",
        footprint="Connector_Coaxial:SMA_Amphenol",
        nets=[ant_net, gnd],
        pins=2,
        pin_names=["Signal", "GND"],
    )
    coax.edge_preference = "right"
    rf_ic = _Part(
        "U5",
        name="Si4684 RF receiver",
        footprint="Package_DFN_QFN:QFN-40",
        nets=[ant_net, gnd],
        pins=40,
        pin_names=["ANT_IN", "GND"],
    )
    circuit = _Circuit([coax, rf_ic], [ant_net, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(65.0, 40.0))

    anchors = [anchor for anchor in plan.edge_anchors if anchor.ref == "J3"]
    assert len(anchors) == 1
    assert anchors[0].edge == "right"
    assert any(
        mating.ref == "J3"
        and mating.kind == "coaxial"
        and mating.edge_preference == "right"
        for mating in plan.mating_intents
    )


def test_rf_ic_near_antenna():
    """RF IC should get NearConstraint to antenna connector (~8mm)."""
    ant_net = _Net("ANT")
    gnd = _Net("GND")
    coax = _Part(
        "J1",
        name="Conn_Coaxial SMA",
        footprint="Connector_Coaxial:SMA",
        nets=[ant_net, gnd],
        pins=2,
        pin_names=["Signal", "GND"],
    )
    rf_ic = _Part(
        "U1",
        name="Si4684",
        footprint="Package_QFN:QFN-20",
        nets=[ant_net, gnd],
        pins=20,
        pin_names=["ANT_IN", "GND"],
    )
    circuit = _Circuit([coax, rf_ic], [ant_net, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(60.0, 40.0))

    # NearConstraint: RF IC near antenna
    near = [
        nc for nc in plan.near_constraints
        if nc.ref == "U1" and nc.target_ref == "J1"
    ]
    assert len(near) == 1, f"Expected NearConstraint(U1->J1), got {plan.near_constraints}"
    assert near[0].distance_mm == 8.0

    # RF IC should have rf_module intent
    assert "rf_module" in _kinds(plan, "U1")


def test_crystal_near_rf_ic():
    """Crystal on RF IC's XTAL pins should get NearConstraint (~4mm)."""
    ant_net = _Net("ANT")
    xtal_net = _Net("XTAL_OUT")
    gnd = _Net("GND")
    coax = _Part(
        "J1",
        name="Conn_Coaxial",
        footprint="Connector_Coaxial:SMA",
        nets=[ant_net, gnd],
        pins=2,
        pin_names=["Signal", "GND"],
    )
    rf_ic = _Part(
        "U1",
        name="Si4684",
        footprint="Package_QFN:QFN-20",
        nets=[ant_net, xtal_net, gnd],
        pins=20,
        pin_names=["ANT_IN", "XTALO", "GND"],
    )
    crystal = _Part(
        "Y1",
        name="Crystal 32.768kHz",
        footprint="Crystal:Crystal_SMD",
        nets=[xtal_net, gnd],
        pins=2,
        pin_names=["1", "2"],
    )
    circuit = _Circuit([coax, rf_ic, crystal], [ant_net, xtal_net, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(60.0, 40.0))

    # NearConstraint: crystal near RF IC
    near_xtal = [
        nc for nc in plan.near_constraints
        if nc.ref == "Y1" and nc.target_ref == "U1"
    ]
    assert len(near_xtal) == 1, (
        f"Expected NearConstraint(Y1->U1), got {plan.near_constraints}"
    )
    assert near_xtal[0].distance_mm == 4.0

    # Crystal should have crystal_network intent
    assert "crystal_network" in _kinds(plan, "Y1")


def test_crystal_near_clock_pins_without_rf_antenna():
    """Clock crystals should cluster with any IC XTAL/OSC pins, not only RF paths."""
    xtal_in = _Net("XTAL_IN")
    xtal_out = _Net("XTAL_OUT")
    gnd = _Net("GND")
    ic = _Part(
        "U5",
        name="Si4684 digital radio",
        footprint="Package_DFN_QFN:QFN-40",
        nets=[xtal_in, xtal_out, gnd],
        pins=40,
        pin_names=["XTALI", "XTALO", "GND"],
    )
    crystal = _Part(
        "Y1",
        name="Crystal 32.768kHz",
        footprint="Crystal:Crystal_SMD",
        nets=[xtal_in, xtal_out],
        pins=2,
        pin_names=["1", "2"],
    )
    circuit = _Circuit([ic, crystal], [xtal_in, xtal_out, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(65.0, 40.0))

    near_xtal = [
        nc for nc in plan.near_constraints
        if nc.ref == "Y1" and nc.target_ref == "U5"
    ]
    assert len(near_xtal) == 1
    assert near_xtal[0].distance_mm == 4.0
    assert "crystal_network" in _kinds(plan, "Y1")


def test_audio_ic_far_from_rf():
    """DAC/codec should get FarConstraint from RF IC (~15mm)."""
    ant_net = _Net("ANT")
    i2s_net = _Net("I2S_DATA")
    gnd = _Net("GND")
    coax = _Part(
        "J1",
        name="Conn_Coaxial",
        footprint="Connector_Coaxial:SMA",
        nets=[ant_net, gnd],
        pins=2,
        pin_names=["Signal", "GND"],
    )
    rf_ic = _Part(
        "U1",
        name="Si4684",
        footprint="Package_QFN:QFN-20",
        nets=[ant_net, gnd],
        pins=20,
        pin_names=["ANT_IN", "GND"],
    )
    dac = _Part(
        "U2",
        name="PCM5102 audio DAC",
        footprint="Package_SO:TSSOP-20",
        nets=[i2s_net, gnd],
        pins=20,
        pin_names=["DOUT", "GND"],
    )
    circuit = _Circuit([coax, rf_ic, dac], [ant_net, i2s_net, gnd])

    plan = infer_placement_intents(circuit, outline=BoardOutline(80.0, 50.0))

    # FarConstraint: audio DAC far from RF IC
    far = [
        fc for fc in plan.far_constraints
        if fc.ref == "U2" and fc.target_ref == "U1"
    ]
    assert len(far) == 1, f"Expected FarConstraint(U2->U1), got {plan.far_constraints}"
    assert far[0].distance_mm == 15.0

    # Audio IC should have analog_separation intent
    assert "analog_separation" in _kinds(plan, "U2")
