"""Benchmark / smoke-test corpus for the PCB layout engine.

Each test builds a realistic mock circuit and runs plan_layout(),
asserting high-level invariants (not exact coordinates).
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from skidl_layout.constraints import (
    BoardOutline,
    EdgeAnchor,
    FixedPosition,
    KeepOut,
    LayoutConstraints,
)
from skidl_layout.engine import LayoutResult, plan_layout

from layout_case_helpers import (
    COMMON_BBOXES,
    _Circuit,
    _Net,
    _Part,
    make_connector,
    make_decap,
    make_ic,
    make_passive,
    make_power_nets,
)


def _placed_by_ref(result: LayoutResult) -> dict[str, object]:
    return {p.ref: p for p in result.placed_parts}


def _distance(a, b) -> float:
    return math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm)


def _inside_outline(part, bbox_w, bbox_h, outline: BoardOutline) -> bool:
    hw, hh = bbox_w / 2, bbox_h / 2
    return (
        part.x_mm - hw >= outline.x_min - 0.1
        and part.x_mm + hw <= outline.x_max + 0.1
        and part.y_mm - hh >= outline.y_min - 0.1
        and part.y_mm + hh <= outline.y_max + 0.1
    )


def _assert_explainable_report(result: LayoutResult) -> None:
    report = result.report
    assert report is not None
    assert report.top_risks(limit=5)
    if report.risky_nets:
        net_name, _ = report.risky_nets[0]
        net_report = report.net(net_name)
        assert net_report.risks or net_report.next_actions
        assert (
            net_report.refs
            or net_report.power_corridors
            or net_report.congestion_regions
        )


# ====================================================================
# Case 1: USB-powered MCU board
# ====================================================================

def _usb_mcu_board():
    """STM32-like MCU with USB, regulator, crystal, decaps, debug header."""
    vbus = _Net("VBUS")
    vcc = _Net("VCC")
    gnd = _Net("GND")
    dp = _Net("USB_DP")
    dm = _Net("USB_DM")
    swd_clk = _Net("SWCLK")
    swd_io = _Net("SWDIO")
    xtal_in = _Net("XTAL_IN")
    xtal_out = _Net("XTAL_OUT")
    sig1 = _Net("SIG1")
    sig2 = _Net("SIG2")

    parts = [
        make_ic("U1", "STM32F103", "Package_QFP:LQFP-48_7x7mm_P0.5mm",
                signal_nets=[dp, dm, swd_clk, swd_io, xtal_in, xtal_out, sig1, sig2],
                power_nets=[vcc, gnd], pins=48),
        make_ic("U2", "LDO regulator", "Package_TO_SOT:SOT-23-5",
                signal_nets=[vbus], power_nets=[vcc, gnd], pins=5),
        make_connector("J1", "USB connector", "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
                       nets=[vbus, gnd, dp, dm], pins=16),
        make_connector("J2", "SWD debug header", "Connector_PinHeader:PinHeader_2x05_P2.54mm",
                       nets=[vcc, gnd, swd_clk, swd_io], pins=10, description="SWD header"),
        _Part("Y1", name="crystal", value="8MHz",
              footprint="Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm",
              nets=[xtal_in, xtal_out], pins=2),
        make_decap("C1", vcc, gnd),
        make_decap("C2", vcc, gnd),
        make_decap("C3", vcc, gnd),
        make_passive("C4", "10uF", "Capacitor_SMD:C_0805_2012Metric", nets=[vbus, gnd]),
        make_passive("C5", "1uF", "Capacitor_SMD:C_0603_1608Metric", nets=[vcc, gnd]),
        make_passive("R1", "1.5K", "Resistor_SMD:R_0603_1608Metric", nets=[dp, vcc]),
        make_passive("R2", "22R", "Resistor_SMD:R_0603_1608Metric", nets=[dp, sig1]),
        make_passive("R3", "22R", "Resistor_SMD:R_0603_1608Metric", nets=[dm, sig2]),
        make_passive("C6", "22pF", "Capacitor_SMD:C_0402_1005Metric", nets=[xtal_in, gnd]),
        make_passive("C7", "22pF", "Capacitor_SMD:C_0402_1005Metric", nets=[xtal_out, gnd]),
    ]
    nets = [vbus, vcc, gnd, dp, dm, swd_clk, swd_io, xtal_in, xtal_out, sig1, sig2]
    return _Circuit(parts, nets)


class TestUSBMCUBoard:
    OUTLINE = BoardOutline(50.0, 35.0)

    def _run(self, **kwargs):
        return plan_layout(
            _usb_mcu_board(),
            fp_bboxes=COMMON_BBOXES,
            constraints=LayoutConstraints(outline=self.OUTLINE),
            **kwargs,
        )

    def test_all_parts_placed(self):
        result = self._run()
        assert result.validation.missing_refs == []
        assert result.validation.placed_parts == 15

    def test_no_overlaps(self):
        result = self._run()
        assert result.validation.overlaps == []

    def test_no_outline_violations(self):
        result = self._run()
        assert result.validation.outline_violations == []

    def test_usb_connector_has_edge_intent(self):
        result = self._run()
        assert result.intent_plan is not None
        edge_refs = [a.ref for a in result.intent_plan.edge_anchors]
        assert "J1" in edge_refs

    def test_decaps_near_mcu(self):
        result = self._run()
        placed = _placed_by_ref(result)
        u1 = placed["U1"]
        for cap_ref in ("C1", "C2", "C3"):
            cap = placed[cap_ref]
            assert _distance(u1, cap) < 25.0, (
                f"{cap_ref} is {_distance(u1, cap):.1f}mm from U1 (expected <25mm)"
            )

    def test_crystal_intent_inferred(self):
        result = self._run()
        assert result.intent_plan is not None
        crystal_refs = result.intent_plan.refs_with_kind("crystal_network")
        assert "Y1" in crystal_refs

    def test_debug_header_intent_inferred(self):
        result = self._run()
        assert result.intent_plan is not None
        debug_refs = result.intent_plan.refs_with_kind("test_debug")
        assert "J2" in debug_refs

    def test_report_mentions_power_nets(self):
        result = self._run()
        summary = result.summary()
        assert "GND" in summary or "power" in summary.lower() or "Power" in summary

    def test_candidates_generated(self):
        result = self._run()
        assert result.candidates is not None
        assert len(result.candidates) >= 3
        names = [c.name for c in result.candidates]
        assert "baseline" in names

    def test_report_has_actionable_risks(self):
        _assert_explainable_report(self._run())

    def test_4layer_vs_2layer_scoring(self):
        r2 = self._run(board_layers=2)
        r4 = self._run(board_layers=4)
        assert r2.power_plan is not None
        assert r4.power_plan is not None
        planes_2 = sum(1 for i in r2.power_plan.route_intents if i.strategy == "plane")
        planes_4 = sum(1 for i in r4.power_plan.route_intents if i.strategy == "plane")
        assert planes_4 >= planes_2


# ====================================================================
# Case 2: Repeated sensor/channel array with mux
# ====================================================================

def _sensor_array_board():
    """4-channel sensor array with shared mux and power backbone."""
    vcc, gnd = make_power_nets()
    sda = _Net("SDA")
    scl = _Net("SCL")

    parts = []
    channel_nets = []

    mux = make_ic("U1", "TCA9548A multiplexer", "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
                  signal_nets=[sda, scl], power_nets=[vcc, gnd], pins=16)
    parts.append(mux)
    parts.append(make_decap("C1", vcc, gnd))

    for ch in range(4):
        ch_sda = _Net(f"CH{ch}_SDA")
        ch_scl = _Net(f"CH{ch}_SCL")
        ch_alert = _Net(f"CH{ch}_ALERT")
        channel_nets.extend([ch_sda, ch_scl, ch_alert])

        sensor = make_ic(
            f"U{ch + 2}", f"Sensor CH{ch}",
            "Package_DFN:DFN-8_3x2mm_P0.5mm",
            signal_nets=[ch_sda, ch_scl, ch_alert],
            power_nets=[vcc, gnd], pins=8,
        )
        parts.append(sensor)
        parts.append(make_decap(f"C{ch + 2}", vcc, gnd))
        parts.append(make_passive(
            f"R{ch * 2 + 1}", "4.7K", "Resistor_SMD:R_0402_1005Metric",
            nets=[ch_sda, vcc],
        ))
        parts.append(make_passive(
            f"R{ch * 2 + 2}", "4.7K", "Resistor_SMD:R_0402_1005Metric",
            nets=[ch_scl, vcc],
        ))

    nets = [vcc, gnd, sda, scl] + channel_nets
    return _Circuit(parts, nets)


class TestSensorArrayBoard:
    OUTLINE = BoardOutline(80.0, 40.0)

    def _run(self, **kwargs):
        return plan_layout(
            _sensor_array_board(),
            fp_bboxes=COMMON_BBOXES,
            constraints=LayoutConstraints(outline=self.OUTLINE),
            **kwargs,
        )

    def test_all_parts_placed(self):
        result = self._run()
        assert result.validation.missing_refs == []
        assert result.validation.placed_parts == 18

    def test_no_outline_violations(self):
        result = self._run()
        assert result.validation.outline_violations == []

    def test_channel_detection(self):
        result = self._run()
        assert result.intent_plan is not None
        if result.intent_plan.repeated_channels:
            ch = result.intent_plan.repeated_channels[0]
            assert len(ch.channel_numbers) >= 2

    def test_mux_intent_inferred(self):
        result = self._run()
        assert result.intent_plan is not None
        mux_refs = result.intent_plan.refs_with_kind("mux_bank_controller")
        assert "U1" in mux_refs

    def test_sensor_decaps_near_their_sensors(self):
        result = self._run()
        placed = _placed_by_ref(result)
        for ch in range(4):
            sensor_ref = f"U{ch + 2}"
            cap_ref = f"C{ch + 2}"
            if sensor_ref in placed and cap_ref in placed:
                d = _distance(placed[sensor_ref], placed[cap_ref])
                assert d < 20.0, (
                    f"{cap_ref} is {d:.1f}mm from {sensor_ref} (expected <20mm)"
                )

    def test_summary_mentions_channels_or_power(self):
        result = self._run()
        summary = result.summary()
        has_channel = "channel" in summary.lower()
        has_power = "power" in summary.lower() or "GND" in summary
        assert has_channel or has_power

    def test_report_has_actionable_risks(self):
        _assert_explainable_report(self._run())


# ====================================================================
# Case 3: Power-heavy board with battery input
# ====================================================================

def _power_board():
    """Battery/JST input, charger IC, LDO, bulk caps, load."""
    vbat = _Net("VBAT")
    vcc = _Net("VCC")
    gnd = _Net("GND")
    chrg = _Net("CHRG_STAT")
    sw_node = _Net("SW_NODE")
    fb = _Net("FB")
    sig = _Net("SIG_OUT")

    parts = [
        make_connector("J1", "JST battery connector",
                       "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm",
                       nets=[vbat, gnd], pins=2, description="JST battery input"),
        make_ic("U1", "Battery charger MCP73831", "Package_TO_SOT:SOT-23-5",
                signal_nets=[chrg, vbat], power_nets=[vcc, gnd], pins=5),
        make_ic("U2", "LDO regulator TLV1117", "Package_SO:MSOP-8_3x3mm_P0.65mm",
                signal_nets=[sw_node, fb], power_nets=[vbat, gnd], pins=8),
        make_ic("U3", "Load MCU", "Package_QFP:TQFP-32_7x7mm_P0.8mm",
                signal_nets=[sig, chrg], power_nets=[vcc, gnd], pins=32),
        # Bulk input caps
        _Part("C1", value="100uF", footprint="Capacitor_SMD:C_Elec_6.3x7.7mm",
              nets=[vbat, gnd], pins=2),
        _Part("C2", value="47uF", footprint="Capacitor_SMD:C_Elec_6.3x7.7mm",
              nets=[vcc, gnd], pins=2),
        # Decoupling
        make_decap("C3", vcc, gnd),
        make_decap("C4", vcc, gnd),
        make_decap("C5", vbat, gnd),
        # Output cap
        _Part("C6", value="10uF", footprint="Capacitor_SMD:C_1206_3216Metric",
              nets=[vcc, gnd], pins=2),
        # Feedback divider
        make_passive("R1", "100K", "Resistor_SMD:R_0603_1608Metric", nets=[vcc, fb]),
        make_passive("R2", "33K", "Resistor_SMD:R_0603_1608Metric", nets=[fb, gnd]),
        # Charge status LED
        _Part("D1", name="charge LED", value="red",
              footprint="LED_SMD:LED_0603_1608Metric", nets=[chrg, gnd], pins=2),
        make_passive("R3", "1K", "Resistor_SMD:R_0603_1608Metric", nets=[chrg, sig]),
        # Inductor for buck
        _Part("L1", value="4.7uH", footprint="Inductor_SMD:L_0805_2012Metric",
              name="power inductor", nets=[sw_node, vcc], pins=2),
    ]
    nets = [vbat, vcc, gnd, chrg, sw_node, fb, sig]
    return _Circuit(parts, nets)


class TestPowerBoard:
    OUTLINE = BoardOutline(45.0, 30.0)

    def _run(self, **kwargs):
        return plan_layout(
            _power_board(),
            fp_bboxes=COMMON_BBOXES,
            constraints=LayoutConstraints(outline=self.OUTLINE),
            **kwargs,
        )

    def test_all_parts_placed(self):
        result = self._run()
        assert result.validation.missing_refs == []
        assert result.validation.placed_parts == 15

    def test_no_outline_violations(self):
        result = self._run()
        assert result.validation.outline_violations == []

    def test_jst_connector_has_power_input_intent(self):
        result = self._run()
        assert result.intent_plan is not None
        power_input_refs = result.intent_plan.refs_with_kind("power_input")
        assert "J1" in power_input_refs

    def test_jst_has_mating_intent(self):
        result = self._run()
        mating = {m.ref: m for m in result.intent_plan.mating_intents}
        assert "J1" in mating
        assert mating["J1"].kind == "jst"

    def test_regulator_has_power_cluster_intent(self):
        result = self._run()
        power_cluster_refs = result.intent_plan.refs_with_kind("power_cluster")
        assert any(ref in power_cluster_refs for ref in ("U2", "L1", "D1"))

    def test_power_plan_has_ground_strategy(self):
        result = self._run()
        gnd_plan = result.power_plan.net("GND")
        assert gnd_plan is not None

    def test_2layer_vs_4layer_power_strategy(self):
        r2 = self._run(board_layers=2)
        r4 = self._run(board_layers=4)
        strats_2 = {i.net_name: i.strategy for i in r2.power_plan.route_intents}
        strats_4 = {i.net_name: i.strategy for i in r4.power_plan.route_intents}
        if "GND" in strats_4:
            assert strats_4["GND"] == "plane"

    def test_score_nonzero(self):
        result = self._run()
        assert result.score.score > 0

    def test_report_has_actionable_risks(self):
        _assert_explainable_report(self._run())


# ====================================================================
# Case 4: Board UI — button, LED, display, potentiometer face-edge
# ====================================================================

def _ui_board():
    """Control panel board with buttons, LEDs, display, and potentiometer."""
    vcc, gnd = make_power_nets()
    btn1_sig = _Net("BTN1")
    btn2_sig = _Net("BTN2")
    led1_sig = _Net("LED1_CTRL")
    led2_sig = _Net("LED2_CTRL")
    pot_sig = _Net("POT_OUT")
    sda = _Net("SDA")
    scl = _Net("SCL")

    parts = [
        make_ic("U1", "ATtiny84", "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
                signal_nets=[btn1_sig, btn2_sig, led1_sig, led2_sig, pot_sig, sda, scl],
                power_nets=[vcc, gnd], pins=16),
        make_decap("C1", vcc, gnd),
        # Buttons
        _Part("SW1", name="user button", footprint="Button_Switch_SMD:SW_SPST_TL3342",
              nets=[btn1_sig, gnd], pins=2),
        _Part("SW2", name="reset button", footprint="Button_Switch_SMD:SW_SPST_TL3342",
              nets=[btn2_sig, gnd], pins=2),
        # LEDs
        _Part("D1", name="status LED", footprint="LED_SMD:LED_0805_2012Metric",
              nets=[led1_sig, gnd], pins=2),
        _Part("D2", name="power LED", footprint="LED_SMD:LED_0603_1608Metric",
              nets=[led2_sig, gnd], pins=2),
        make_passive("R1", "330R", "Resistor_SMD:R_0603_1608Metric", nets=[led1_sig, vcc]),
        make_passive("R2", "330R", "Resistor_SMD:R_0603_1608Metric", nets=[led2_sig, vcc]),
        # Display
        _Part("DS1", name="OLED display", footprint="Display_OLED:SSD1306_0.96in",
              nets=[sda, scl, vcc, gnd], pins=4, description="display"),
        # Potentiometer
        _Part("RV1", name="volume pot", footprint="Potentiometer_SMD:POT_Bourns_3362P",
              nets=[vcc, gnd, pot_sig], pins=3, description="potentiometer"),
        # Pullups
        make_passive("R3", "10K", "Resistor_SMD:R_0402_1005Metric", nets=[btn1_sig, vcc]),
        make_passive("R4", "10K", "Resistor_SMD:R_0402_1005Metric", nets=[btn2_sig, vcc]),
    ]
    nets = [vcc, gnd, btn1_sig, btn2_sig, led1_sig, led2_sig, pot_sig, sda, scl]
    return _Circuit(parts, nets)


class TestUIBoard:
    OUTLINE = BoardOutline(60.0, 45.0)

    def _run(self, **kwargs):
        return plan_layout(
            _ui_board(),
            fp_bboxes=COMMON_BBOXES,
            constraints=LayoutConstraints(outline=self.OUTLINE),
            **kwargs,
        )

    def test_all_parts_placed(self):
        result = self._run()
        assert result.validation.missing_refs == []
        assert result.validation.placed_parts == 12

    def test_no_outline_violations(self):
        result = self._run()
        assert result.validation.outline_violations == []

    def test_buttons_have_ui_intent(self):
        result = self._run()
        ui_refs = result.intent_plan.refs_with_kind("board_ui")
        assert "SW1" in ui_refs
        assert "SW2" in ui_refs

    def test_buttons_have_mating_intent(self):
        result = self._run()
        mating = {m.ref: m for m in result.intent_plan.mating_intents}
        assert "SW1" in mating
        assert mating["SW1"].kind == "button"
        assert mating["SW1"].mating_side == "user_control"

    def test_leds_have_mating_intent(self):
        result = self._run()
        mating = {m.ref: m for m in result.intent_plan.mating_intents}
        assert "D1" in mating
        assert mating["D1"].kind == "led"
        assert mating["D1"].mating_side == "visible_face"

    def test_display_has_mating_intent(self):
        result = self._run()
        mating = {m.ref: m for m in result.intent_plan.mating_intents}
        assert "DS1" in mating
        assert mating["DS1"].kind == "display"

    def test_pot_has_mating_intent(self):
        result = self._run()
        mating = {m.ref: m for m in result.intent_plan.mating_intents}
        assert "RV1" in mating
        assert mating["RV1"].mating_side == "user_control"

    def test_face_edge_constraints_generated(self):
        result = self._run()
        face_refs = {f.ref for f in result.intent_plan.face_edges}
        assert len(face_refs) >= 3

    def test_summary_mentions_ui_or_mating(self):
        result = self._run()
        summary = result.summary()
        has_mating = "mating" in summary.lower()
        has_face = "face" in summary.lower()
        has_ui = "board_ui" in summary.lower()
        assert has_mating or has_face or has_ui

    def test_report_has_actionable_risks(self):
        _assert_explainable_report(self._run())


# ====================================================================
# Case 5: RF module with antenna keepout
# ====================================================================

def _rf_board():
    """ESP32 board with antenna, keepout zone, and peripheral I/O."""
    vcc, gnd = make_power_nets()
    ant = _Net("ANT")
    gpio1 = _Net("GPIO1")
    gpio2 = _Net("GPIO2")
    uart_tx = _Net("UART_TX")
    uart_rx = _Net("UART_RX")

    parts = [
        make_ic("U1", "ESP32-WROOM WiFi module", "RF_Module:ESP32-WROOM-32E",
                signal_nets=[ant, gpio1, gpio2, uart_tx, uart_rx],
                power_nets=[vcc, gnd], pins=38),
        _Part("ANT1", name="2.4GHz antenna", footprint="Antenna:ANT_2.4G_Chip",
              nets=[ant], pins=2, description="antenna"),
        make_decap("C1", vcc, gnd),
        make_decap("C2", vcc, gnd),
        _Part("C3", value="10uF", footprint="Capacitor_SMD:C_0805_2012Metric",
              nets=[vcc, gnd], pins=2),
        make_connector("J1", "UART header", "Connector_PinHeader:PinHeader_1x04_P2.54mm",
                       nets=[vcc, gnd, uart_tx, uart_rx], pins=4,
                       description="serial header"),
        make_connector("J2", "GPIO header", "Connector_PinHeader:PinHeader_1x06_P2.54mm",
                       nets=[vcc, gnd, gpio1, gpio2], pins=6,
                       description="header"),
        make_passive("R1", "10K", "Resistor_SMD:R_0603_1608Metric", nets=[gpio1, vcc]),
        make_passive("R2", "10K", "Resistor_SMD:R_0603_1608Metric", nets=[gpio2, vcc]),
        _Part("D1", name="power LED", footprint="LED_SMD:LED_0603_1608Metric",
              nets=[gpio1, gnd], pins=2),
        make_passive("R3", "330R", "Resistor_SMD:R_0603_1608Metric", nets=[gpio1, gnd]),
    ]
    nets = [vcc, gnd, ant, gpio1, gpio2, uart_tx, uart_rx]
    return _Circuit(parts, nets)


class TestRFBoard:
    OUTLINE = BoardOutline(55.0, 35.0)
    ANTENNA_KEEPOUT = KeepOut(x_min=40.0, y_min=0.0, x_max=55.0, y_max=15.0)

    def _run(self, **kwargs):
        constraints = kwargs.pop("constraints", None)
        if constraints is None:
            constraints = LayoutConstraints(
                outline=self.OUTLINE,
                keepouts=[self.ANTENNA_KEEPOUT],
            )
        return plan_layout(
            _rf_board(),
            fp_bboxes=COMMON_BBOXES,
            constraints=constraints,
            **kwargs,
        )

    def test_all_parts_placed(self):
        result = self._run()
        assert result.validation.missing_refs == []
        assert result.validation.placed_parts == 11

    def test_no_outline_violations(self):
        result = self._run()
        assert result.validation.outline_violations == []

    def test_rf_intent_inferred(self):
        result = self._run()
        rf_refs = result.intent_plan.refs_with_kind("rf_module")
        assert "U1" in rf_refs or "ANT1" in rf_refs

    def test_no_keepout_violations(self):
        result = self._run()
        assert result.score.keepout_violation_count == 0

    def test_connectors_have_edge_intent(self):
        result = self._run()
        edge_refs = [a.ref for a in result.intent_plan.edge_anchors]
        assert "J1" in edge_refs or "J2" in edge_refs

    def test_decaps_near_esp32(self):
        result = self._run()
        placed = _placed_by_ref(result)
        u1 = placed["U1"]
        for cap_ref in ("C1", "C2"):
            cap = placed[cap_ref]
            assert _distance(u1, cap) < 25.0, (
                f"{cap_ref} is {_distance(u1, cap):.1f}mm from U1 (expected <25mm)"
            )

    def test_score_nonzero(self):
        result = self._run()
        assert result.score.score > 0

    def test_summary_mentions_rf_or_antenna(self):
        result = self._run()
        summary = result.summary()
        has_rf = "rf" in summary.lower()
        has_antenna = "antenna" in summary.lower()
        has_module = "rf_module" in summary.lower()
        assert has_rf or has_antenna or has_module

    def test_report_has_actionable_risks(self):
        _assert_explainable_report(self._run())
