# -*- coding: utf-8 -*-
"""Tests for :mod:`skidl_layout.power_roles` -- the power-electronics vocabulary.

Two halves:

* **Fakes** exercise the device-typing ladder one rule at a time, including the
  deny-list and the pin-name normalisation.
* **Real netlists** (stock KiCad-10 symbols only) exercise the net roles and the
  commutation-loop walk on a boost and on a flyback, plus a negative control.

Every real-netlist assertion is made **twice**: once on the circuit and once on
its ref- and net-scrambled twin (``tests/scramble.py``). That second run is the
anti-cheat gate -- it is what makes "topologically, not by name" a fact rather
than a claim.
"""

from __future__ import annotations

import os

import pytest

from scramble import scramble_circuit
from skidl_layout.power_roles import (
    CONTROLLER_PIN_NAMES,
    PowerStagePlan,
    classify_devices,
    classify_power_roles,
    pin_name_tokens,
)

_SYMBOL_ENV_VARS = (
    "KICAD_SYMBOL_DIR",
    "KICAD9_SYMBOL_DIR",
    "KICAD8_SYMBOL_DIR",
    "KICAD7_SYMBOL_DIR",
    "KICAD6_SYMBOL_DIR",
)
requires_symbols = pytest.mark.skipif(
    not any(os.environ.get(var) for var in _SYMBOL_ENV_VARS),
    reason="KiCad symbol libraries not installed",
)


# --------------------------------------------------------------------------- #
# Fakes -- the same shape tests/test_layout_power.py uses
# --------------------------------------------------------------------------- #

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
        self.num = str(len(part.pins) + 1)
        self.func = None
        if net is not None:
            net._pins.append(self)


class _Part:
    """A part whose pins are given as ``(pin_name, net)`` pairs."""

    def __init__(self, ref, name="", value="", pins=()):
        self.ref = ref
        self.name = name
        self.value = value
        self.footprint = ""
        self.pins = []
        for pin_name, net in pins:
            self.pins.append(_Pin(self, net, pin_name))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self._nets = nets

    def get_nets(self):
        return self._nets


def _kind(ref, name, pin_names, value=""):
    n = _Net("N")
    part = _Part(ref, name=name, value=value,
                 pins=[(pn, n) for pn in pin_names])
    return classify_devices(_Circuit([part], [n]))[ref].kind


# --------------------------------------------------------------------------- #
# Pin-name normalisation
# --------------------------------------------------------------------------- #

def test_pin_tokens_strip_overline_and_split_alternatives():
    assert pin_name_tokens("~{SHDN}/UVLO") == frozenset({"SHDN", "UVLO"})
    assert pin_name_tokens("V_{SS}") == frozenset({"VSS"})
    assert pin_name_tokens("gate") == frozenset({"GATE"})
    assert pin_name_tokens("") == frozenset()
    assert pin_name_tokens(None) == frozenset()
    assert pin_name_tokens("GPIO26/ADC0") == frozenset({"GPIO26", "ADC0"})


# --------------------------------------------------------------------------- #
# WS-1: device typing
# --------------------------------------------------------------------------- #

def test_switch_from_fet_and_bjt_pin_names():
    # Si7336ADP's real pin list: three source pins collapse to one terminal.
    assert _kind("M1", "Si7336ADP", ["S", "S", "S", "G", "D"]) == "switch"
    assert _kind("Q1", "IRF740", ["G", "D", "S"]) == "switch"
    assert _kind("Q2", "Q_NPN_BCE", ["B", "C", "E"]) == "switch"


def test_rectifier_needs_exactly_anode_and_cathode():
    assert _kind("D1", "D_Schottky", ["K", "A"]) == "rectifier"
    # A laser diode's KiCad pins are C/A, not K/A -- correctly not a rectifier.
    assert _kind("D3", "SPL_PL90", ["C", "A"]) == "unknown"


def test_magnetics_from_symbol_and_from_winding_pin_names():
    assert _kind("L1", "L", ["1", "2"]) == "magnetics"
    assert _kind("L2", "L_Small", ["1", "2"]) == "magnetics"
    assert _kind("T1", "Transformer_1P_1S", ["AA", "AB", "SA", "SB"]) == "magnetics"


def test_two_terminal_passives_come_from_symbol_identity():
    assert _kind("R1", "R", ["", ""]) == "resistor"
    assert _kind("R2", "R_Small", ["", ""]) == "resistor"
    assert _kind("RS", "R_Shunt", ["", "", "", ""]) == "resistor"
    assert _kind("C1", "C", ["", ""]) == "capacitor"
    assert _kind("C2", "C_Polarized", ["", ""]) == "capacitor"


def test_reference_prefix_is_never_consulted():
    """A part called ``R9`` that is not a resistor symbol stays unknown."""
    assert _kind("R9", "SomeUnknownThing", ["1", "2"]) == "unknown"
    # ... and a resistor symbol is a resistor whatever it is called.
    assert _kind("FOO7", "R", ["", ""]) == "resistor"


def test_deny_list_keeps_leds_crystals_and_connectors_out():
    # Device:LED's pins are literally K/A -- without the deny-list it would be a
    # rectifier, and every indicator LED would look like part of a power stage.
    assert _kind("D5", "LED", ["K", "A"]) == "unknown"
    assert _kind("Y1", "Crystal", ["1", "2"]) == "unknown"
    assert _kind("J1", "Screw_Terminal_01x02", ["Pin_1", "Pin_2"]) == "unknown"
    assert _kind("SW1", "SW_Push", ["1", "2"]) == "unknown"


def test_controller_needs_five_pins_and_two_switcher_pin_names():
    lt3757 = ["VC", "FBX", "SS", "RT", "SYNC", "SENSE", "GATE", "INTVCC",
              "~{SHDN}/UVLO", "VIN", "GND"]
    assert _kind("U1", "LT3757AEMSE", lt3757) == "controller"
    # A 5-pin LDO with an enable is NOT a controller.
    assert _kind("U2", "AP2112K-3.3", ["VIN", "GND", "EN", "NC", "VOUT"]) == "unknown"
    # Nor is a SPI flash: its ~{CS} and VCC must not read as switcher pins.
    assert _kind("U3", "W25Q32JVSS",
                 ["~{CS}", "DO/IO_{1}", "~{WP}/IO_{2}", "GND", "DI/IO_{0}",
                  "CLK", "~{HOLD}/~{RESET}/IO_{3}", "VCC"]) == "unknown"
    # One switcher pin is not enough.
    assert _kind("U4", "TLC555xD",
                 ["GND", "VCC", "TRIG", "OUT", "~{RST}", "CONT", "THRES",
                  "DISCH"]) == "unknown"


def test_controller_pin_set_excludes_the_generic_names():
    """The names that would put an MCU one coincidence from being a controller."""
    for generic in ("VCC", "VDD", "EN", "VIN", "VOUT", "CS", "GND", "OUT"):
        assert generic not in CONTROLLER_PIN_NAMES


def test_empty_and_none_circuits_are_silent():
    assert classify_power_roles(None).stages == []
    assert classify_power_roles(_Circuit([], [])).stages == []
    assert PowerStagePlan().summary() == ""


# --------------------------------------------------------------------------- #
# Real netlists
# --------------------------------------------------------------------------- #

R08 = "Resistor_SMD:R_0805_2012Metric"
C08 = "Capacitor_SMD:C_0805_2012Metric"


def _boost():
    """The LT3757 datasheet Figure-11 boost, in the canary's own net topology.

    Deliberately keeps the canary's reference designators (``COUT3``, ``M1``,
    ``RS``, ...) so the assertions below read as the same ground truth Phase 0
    hardcoded -- and deliberately *scrambles* them again in the twin so no rule
    may depend on them.
    """
    from skidl import Circuit, Net, Part

    ckt = Circuit(name="boost")
    with ckt:
        vin, vout, gnd = Net("VIN"), Net("VOUT"), Net("GND")
        sw, sense, fbx, gate = Net("SW"), Net("SW_SENSE"), Net("FBX"), Net("GATE")
        vc, rt_net, ss, intvcc = Net("VC"), Net("RT"), Net("SS"), Net("INTVCC")

        u1 = Part("Regulator_Switching", "LT3757AEMSE", ref="U1", value="LT3757")
        u1["VIN"] += vin
        u1["GND"] += gnd
        u1["SYNC"] += gnd
        u1["~{SHDN}/UVLO"] += vin
        u1["GATE"] += gate
        u1["SENSE"] += sense
        u1["FBX"] += fbx
        u1["VC"] += vc
        u1["RT"] += rt_net
        u1["SS"] += ss
        u1["INTVCC"] += intvcc

        cin = Part("Device", "C", ref="CIN", value="10uF", footprint=C08)
        cin[1] += vin
        cin[2] += gnd
        l1 = Part("Device", "L", ref="L1", value="10uH")
        l1[1] += vin
        l1[2] += sw
        m1 = Part("Transistor_FET", "Si7336ADP", ref="M1")
        m1["D"] += sw
        m1["G"] += gate
        m1["S"] += sense
        rs = Part("Device", "R", ref="RS", value="0.01", footprint=R08)
        rs[1] += sense
        rs[2] += gnd
        d1 = Part("Device", "D_Schottky", ref="D1", value="B360")
        d1["A"] += sw
        d1["K"] += vout

        # Two electrolytics and one ceramic, exactly as the datasheet board has.
        for ref, value in (("COUT1", "47uF"), ("COUT2", "47uF")):
            c = Part("Device", "C_Polarized", ref=ref, value=value)
            c[1] += vout
            c[2] += gnd
        cout3 = Part("Device", "C", ref="COUT3", value="10uF", footprint=C08)
        cout3[1] += vout
        cout3[2] += gnd

        r1 = Part("Device", "R", ref="R1", value="226k", footprint=R08)
        r1[1] += vout
        r1[2] += fbx
        r2 = Part("Device", "R", ref="R2", value="16.2k", footprint=R08)
        r2[1] += fbx
        r2[2] += gnd
        rc = Part("Device", "R", ref="RC", value="22k", footprint=R08)
        rc[1] += vc
        rc[2] += gnd
        rt = Part("Device", "R", ref="RT", value="41.2k", footprint=R08)
        rt[1] += rt_net
        rt[2] += gnd
        for ref, net in (("CC2", ss), ("CVCC", intvcc)):
            c = Part("Device", "C", ref=ref, value="0.1uF", footprint=C08)
            c[1] += net
            c[2] += gnd
    return ckt


def _flyback():
    """A transformer-coupled stage: no rectifier on the switch node at all."""
    from skidl import Circuit, Net, Part

    ckt = Circuit(name="flyback")
    with ckt:
        vin, hv, gnd = Net("VIN_12V"), Net("HV_RAIL"), Net("GND")
        drain, sense, fbx = Net("SW_DRAIN"), Net("SW_SENSE"), Net("FBX")
        gate, anode = Net("GATE"), Net("HV_ANODE")
        vc, rt_net, ss, intvcc = Net("VC"), Net("RT"), Net("SS"), Net("INTVCC")

        u1 = Part("Regulator_Switching", "LT3757AEMSE", ref="U1", value="LT3757")
        u1["VIN"] += vin
        u1["GND"] += gnd
        u1["SYNC"] += gnd
        u1["~{SHDN}/UVLO"] += vin
        u1["GATE"] += gate
        u1["SENSE"] += sense
        u1["FBX"] += fbx
        u1["VC"] += vc
        u1["RT"] += rt_net
        u1["SS"] += ss
        u1["INTVCC"] += intvcc

        q1 = Part("Transistor_FET", "IRF740", ref="Q1")
        q1["G"] += gate
        q1["D"] += drain
        q1["S"] += sense
        t1 = Part("Device", "Transformer_1P_1S", ref="T1", value="1:12")
        t1["AA"] += vin
        t1["AB"] += drain
        t1["SA"] += anode
        t1["SB"] += gnd
        d1 = Part("Device", "D", ref="D1", value="UF4007")
        d1["A"] += anode
        d1["K"] += hv
        for ref, value in (("C1", "100uF"), ("C2", "10uF")):
            c = Part("Device", "C", ref=ref, value=value, footprint=C08)
            c[1] += vin
            c[2] += gnd
        c3 = Part("Device", "C", ref="C3", value="4.7uF", footprint=C08)
        c3[1] += hv
        c3[2] += gnd
        r1 = Part("Device", "R", ref="R1", value="0.1", footprint=R08)
        r1[1] += sense
        r1[2] += gnd
        r2 = Part("Device", "R", ref="R2", value="1M", footprint=R08)
        r2[1] += hv
        r2[2] += fbx
        r3 = Part("Device", "R", ref="R3", value="9.09k", footprint=R08)
        r3[1] += fbx
        r3[2] += gnd
        for ref, net in (("C4", vc), ("C5", ss), ("C6", intvcc)):
            c = Part("Device", "C", ref=ref, value="10nF", footprint=C08)
            c[1] += net
            c[2] += gnd
        rt = Part("Device", "R", ref="R5", value="42.2k", footprint=R08)
        rt[1] += rt_net
        rt[2] += gnd
    return ckt


def _non_power_board():
    """An MCU-ish board: an LDO, a reset BJT, decoupling -- and no magnetics."""
    from skidl import Circuit, Net, Part

    ckt = Circuit(name="mcu")
    with ckt:
        vbus, v33, gnd, rst = Net("VBUS"), Net("3V3"), Net("GND"), Net("RESET")
        u1 = Part("Regulator_Linear", "AP2112K-3.3", ref="U1")
        u1["VIN"] += vbus
        u1["GND"] += gnd
        u1["EN"] += vbus
        u1["VOUT"] += v33
        u2 = Part("MCU_Microchip_ATtiny", "ATtiny85-20S", ref="U2")
        u2["VCC"] += v33
        u2["GND"] += gnd
        u2["~{RESET}/PB5"] += rst
        q1 = Part("Transistor_BJT", "Q_NPN_BCE", ref="Q1")
        q1["B"] += rst
        q1["C"] += v33
        q1["E"] += gnd
        for ref, net in (("C1", vbus), ("C2", v33), ("C3", v33)):
            c = Part("Device", "C", ref=ref, value="100nF", footprint=C08)
            c[1] += net
            c[2] += gnd
        r1 = Part("Device", "R", ref="R1", value="10k", footprint=R08)
        r1[1] += v33
        r1[2] += rst
        d1 = Part("Device", "LED", ref="D1", value="green")
        d1["A"] += v33
        d1["K"] += rst
    return ckt


# --- structural fingerprint, for the scrambled-twin comparison --------------

def _fingerprint(plan, ref_map=None, net_map=None):
    """Everything the classifier claims, with names mapped through the scramble.

    ``reasons`` are prose and are excluded on purpose; the structure is what has
    to survive renaming.
    """
    ref_map = ref_map or {}
    net_map = net_map or {}
    r = lambda x: ref_map.get(x, x)          # noqa: E731
    n = lambda x: net_map.get(x, x)          # noqa: E731
    return [
        (
            r(stage.controller_ref),
            stage.topology,
            [n(x) for x in stage.switch_node_nets],
            n(stage.input_rail),
            n(stage.output_rail),
            n(stage.ground_net),
            sorted((r(d.ref), d.kind) for d in stage.devices),
            [
                (
                    [r(x) for x in loop.member_refs],
                    [n(x) for x in loop.net_names],
                    n(loop.returns_through),
                    [r(x) for x in loop.bulk_refs],
                )
                for loop in stage.loops
            ],
            tuple(r(x) for x in stage.feedback_divider)
            if stage.feedback_divider else None,
            r(stage.sense_resistor_ref),
        )
        for stage in plan.stages
    ]


def _assert_scramble_invariant(circuit):
    """The anti-cheat gate: classify the circuit and its anonymised twin."""
    plan = classify_power_roles(circuit)
    twin, ref_map, net_map = scramble_circuit(circuit)
    twin_plan = classify_power_roles(twin)
    assert _fingerprint(plan, ref_map, net_map) == _fingerprint(twin_plan)
    return plan


# --------------------------------------------------------------------------- #
# WS-3 / WS-4 on the boost -- this is plan gate G1
# --------------------------------------------------------------------------- #

@requires_symbols
def test_boost_stage_reproduces_the_phase0_ground_truth():
    stage = classify_power_roles(_boost()).stages[0]

    assert stage.controller_ref == "U1"
    assert stage.ground_net == "GND"
    assert stage.switch_node_nets == ["SW"]
    assert stage.input_rail == "VIN"
    assert stage.output_rail == "VOUT"
    assert stage.sense_resistor_ref == "RS"
    assert stage.feedback_divider == ("R1", "R2")
    assert stage.topology == "boost"


@requires_symbols
def test_boost_commutation_loop_is_the_phase0_hardcoded_constant():
    """``measure_power_layout.py``'s ``HOT_LOOP = ["COUT3","D1","M1","RS"]``.

    Derived here, not hardcoded. The ceramic beats the two electrolytics on
    symbol identity alone (``C`` vs ``C_Polarized``) -- no value needed.
    """
    loop = classify_power_roles(_boost()).stages[0].loops[0]
    assert loop.member_refs == ["COUT3", "D1", "M1", "RS"]
    assert loop.returns_through == "GND"
    assert loop.bulk_refs == ["COUT1", "COUT2"]
    assert loop.net_names == ["VOUT", "SW", "SW_SENSE"]


@requires_symbols
def test_boost_switch_node_holds_the_inductor_fet_and_diode():
    """Phase 0's ``SWITCH_NODE_PARTS = ["L1", "M1", "D1"]``."""
    plan = classify_power_roles(_boost())
    stage = plan.stages[0]
    devices = classify_devices(_boost())
    switch_node = stage.switch_node_nets[0]
    from skidl_layout.power_roles import _View
    view = _View(_boost())
    refs = set(view.refs_on(switch_node))
    assert refs == {"L1", "M1", "D1"}
    assert devices["L1"].kind == "magnetics"
    assert devices["M1"].kind == "switch"
    assert devices["D1"].kind == "rectifier"


@requires_symbols
def test_boost_derived_device_roles():
    stage = classify_power_roles(_boost()).stages[0]
    assert stage.device("RS").kind == "sense_resistor"
    assert stage.device("R1").kind == "fb_divider_top"
    assert stage.device("R2").kind == "fb_divider_bottom"
    assert stage.device("CIN").kind == "input_cap"
    assert sorted(stage.refs_of_kind("output_cap")) == ["COUT1", "COUT2", "COUT3"]
    # Small-signal parts are not stage members at all.
    assert stage.device("RC") is None
    assert stage.device("CVCC") is None


@requires_symbols
def test_boost_survives_ref_and_net_scrambling():
    """Plan gate G2 -- the anti-cheat gate."""
    plan = _assert_scramble_invariant(_boost())
    assert len(plan.stages) == 1


# --------------------------------------------------------------------------- #
# WS-4 on the flyback -- plan gate G4
# --------------------------------------------------------------------------- #

@requires_symbols
def test_flyback_stage_is_not_boost_shaped():
    stage = classify_power_roles(_flyback()).stages[0]
    assert stage.topology == "flyback"
    assert stage.device("T1").kind == "magnetics"
    assert stage.device("Q1").kind == "switch"
    assert stage.sense_resistor_ref == "R1"
    assert stage.feedback_divider == ("R2", "R3")
    assert stage.input_rail == "VIN_12V"
    assert stage.output_rail == "HV_RAIL"


@requires_symbols
def test_flyback_loop_closes_through_the_primary_winding():
    """No rectifier sits on the switch node, so the loop goes input-side."""
    loop = classify_power_roles(_flyback()).stages[0].loops[0]
    assert loop.member_refs == ["C2", "T1", "Q1", "R1"]
    assert loop.bulk_refs == ["C1"]
    assert loop.returns_through == "GND"


@requires_symbols
def test_flyback_survives_ref_and_net_scrambling():
    _assert_scramble_invariant(_flyback())


# --------------------------------------------------------------------------- #
# Plan gate G3 -- no false positives
# --------------------------------------------------------------------------- #

@requires_symbols
def test_non_power_board_yields_no_stage_and_no_noise():
    plan = _assert_scramble_invariant(_non_power_board())
    assert plan.stages == []
    # No magnetics anywhere -> nothing worth warning about either.
    assert plan.warnings == []
    assert plan.summary() == ""


@requires_symbols
def test_reset_transistor_is_typed_but_never_becomes_a_stage():
    """A BJT on an MCU board is a switch *device* -- it is not a power stage."""
    devices = classify_devices(_non_power_board())
    assert devices["Q1"].kind == "switch"
    assert devices["D1"].kind == "unknown"      # LED, via the deny-list
    assert classify_power_roles(_non_power_board()).stages == []


@requires_symbols
def test_integrated_switcher_is_declined_rather_than_guessed():
    """A controller with no external gate drive is reported, not invented."""
    from skidl import Circuit, Net, Part

    ckt = Circuit(name="nogate")
    with ckt:
        vin, vout, gnd, fbx = Net("VIN"), Net("VOUT"), Net("GND"), Net("FBX")
        u1 = Part("Regulator_Switching", "LT3757AEMSE", ref="U1")
        u1["VIN"] += vin
        u1["GND"] += gnd
        u1["FBX"] += fbx
        u1["SENSE"] += gnd
        u1["VC"] += vout
        u1["RT"] += gnd
        l1 = Part("Device", "L", ref="L1", value="10uH")
        l1[1] += vin
        l1[2] += vout
        c1 = Part("Device", "C", ref="C1", value="10uF")
        c1[1] += vout
        c1[2] += gnd
    plan = classify_power_roles(ckt)
    assert plan.stages == []
    assert any("integrated-switch" in w for w in plan.warnings)


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #

@requires_symbols
def test_to_dict_round_trips_the_loop():
    payload = classify_power_roles(_boost()).to_dict()
    assert payload["warnings"] == []
    loop = payload["stages"][0]["loops"][0]
    assert loop["member_refs"] == ["COUT3", "D1", "M1", "RS"]
    assert payload["stages"][0]["feedback_divider"] == ["R1", "R2"]


@requires_symbols
def test_summary_names_the_loop():
    text = classify_power_roles(_boost()).summary()
    assert "COUT3 -> D1 -> M1 -> RS" in text
    assert "boost stage on U1" in text


# --------------------------------------------------------------------------- #
# WS-5 -- the LayoutResult wiring
# --------------------------------------------------------------------------- #

@requires_symbols
def test_plan_layout_attaches_the_stage_plan_without_moving_anything():
    """Plan gate G5 in miniature: the field appears, the placement does not move."""
    from skidl_layout import plan_layout

    def digest(res):
        return [
            (p.ref, round(p.x_mm, 6), round(p.y_mm, 6), round(p.rot_deg, 3), p.side)
            for p in sorted(res.placed_parts, key=lambda p: p.ref)
        ]

    result = plan_layout(_boost(), fp_lib_dirs=[])
    assert result.power_stage_plan is not None
    assert result.power_stage_plan.stages[0].loops[0].member_refs == [
        "COUT3", "D1", "M1", "RS"
    ]
    assert "power_stage_plan" in result.to_dict()
    assert "commutation loop" in result.summary()
    # Same circuit, same placement -- the classifier runs after selection and
    # takes no positions as input, so it cannot perturb it.
    assert digest(plan_layout(_boost(), fp_lib_dirs=[])) == digest(result)


@requires_symbols
def test_plan_layout_stays_silent_on_a_non_power_board():
    from skidl_layout import plan_layout

    result = plan_layout(_non_power_board(), fp_lib_dirs=[])
    assert result.power_stage_plan is not None
    assert result.power_stage_plan.stages == []
    assert "power_stage_plan" not in result.to_dict()
    assert "Power stage plan" not in result.summary()
