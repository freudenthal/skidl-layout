from __future__ import annotations

from skidl_layout.power import (
    PowerRouteIntent,
    identify_power_nets,
    infer_power_topology,
    plan_power_routes,
)
from skidl_layout.writer import PlacedPart


class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, net, func=None):
        self.part = part
        self.net = net
        self.func = func
        net._pins.append(self)


class _Part:
    def __init__(
        self,
        ref,
        value="",
        name="",
        footprint="",
        nets=None,
        pins=2,
        pin_funcs=None,
    ):
        self.ref = ref
        self.value = value
        self.name = name
        self.footprint = footprint
        self.pins = []
        for idx, net in enumerate(nets or []):
            func = pin_funcs[idx] if pin_funcs and idx < len(pin_funcs) else None
            self.pins.append(_Pin(self, net, func=func))
        while len(self.pins) < pins:
            idx = len(self.pins)
            func = pin_funcs[idx] if pin_funcs and idx < len(pin_funcs) else None
            self.pins.append(_Pin(self, _Net(f"{ref}_N{idx}"), func=func))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self._nets = nets

    def get_nets(self):
        return self._nets


def _power_circuit():
    vbus = _Net("VBUS")
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig = _Net("SIG")
    j1 = _Part(
        "J1",
        name="USB connector",
        footprint="Connector:USB",
        nets=[vbus, gnd],
        pins=2,
    )
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd, sig], pins=3)
    u2 = _Part(
        "U2",
        name="LDO regulator",
        footprint="Package_TO_SOT:SOT23",
        nets=[vbus, gnd, vcc],
        pins=3,
    )
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    return _Circuit([j1, u1, u2, c1], [vbus, vcc, gnd, sig])


def test_identify_power_nets_prioritizes_ground_and_high_current():
    nets = identify_power_nets(_power_circuit())
    names = [net.name for net in nets]
    assert names[:2] == ["GND", "VBUS"]
    assert next(net for net in nets if net.name == "VBUS").suggested_width_mm == 0.8


def test_four_layer_power_plan_uses_internal_layers():
    nets = identify_power_nets(_power_circuit(), board_layers=4)
    gnd = next(net for net in nets if net.name == "GND")
    vcc = next(net for net in nets if net.name == "VCC")
    assert gnd.suggested_layer == "In1.Cu"
    assert vcc.suggested_layer == "In2.Cu"


def test_two_layer_power_plan_uses_outer_copper():
    nets = identify_power_nets(_power_circuit(), board_layers=2)
    gnd = next(net for net in nets if net.name == "GND")
    vcc = next(net for net in nets if net.name == "VCC")
    assert gnd.suggested_layer == "F.Cu"
    assert vcc.suggested_layer == "F.Cu"


def test_infer_power_topology_builds_source_regulator_storage_load_chain():
    topology = infer_power_topology(_power_circuit())

    assert len(topology.chains) == 1
    chain = topology.chains[0]
    assert chain.source_ref == "J1"
    assert chain.source_net == "VBUS"
    assert chain.converter_refs == ["U2"]
    assert chain.storage_refs == ["C1"]
    assert chain.load_refs == ["U1"]
    assert chain.output_nets == ["VCC"]
    assert chain.ordered_refs == ["J1", "U2", "C1", "U1"]


def test_infer_power_topology_accepts_onboard_usb_dev_module_as_source():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    row = _Net("ROW0")
    pico = _Part(
        "A1",
        name="Raspberry Pi Pico module",
        footprint="Module:RaspberryPi_Pico_Common_THT",
        nets=[vbus, gnd, row],
        pins=3,
    )
    circuit = _Circuit([pico], [vbus, gnd, row])

    topology = infer_power_topology(circuit)

    assert topology.warnings == []
    assert len(topology.chains) == 1
    assert topology.chains[0].source_ref == "A1"
    assert topology.chains[0].source_net == "VBUS"


def test_infer_power_topology_uses_power_output_pin_for_ic_source_net():
    vbus = _Net("VBUS")
    v3v3 = _Net("+3V3")
    gnd = _Net("GND")
    dp = _Net("USB_D+")
    usb = _Part(
        "J_USB",
        name="USB-C receptacle",
        footprint="Connector_USB:USB_C_Receptacle_GCT_USB4105",
        nets=[vbus, gnd, dp],
        pins=3,
    )
    mcu = _Part(
        "U1",
        name="USB MIDI bridge MCU",
        footprint="Package_SO:SOIC-16",
        nets=[vbus, gnd, dp, v3v3],
        pins=16,
        pin_funcs=["PWRIN", "PWRIN", "BIDIR", "PWROUT"],
    )
    opto = _Part("U2", name="6N138 optocoupler", nets=[v3v3, gnd], pins=8)
    circuit = _Circuit([usb, mcu, opto], [vbus, v3v3, gnd, dp])

    topology = infer_power_topology(circuit)

    source_pairs = {(chain.source_ref, chain.source_net) for chain in topology.chains}
    assert ("J_USB", "VBUS") in source_pairs
    assert ("U1", "+3V3") in source_pairs
    assert ("U1", "VBUS") not in source_pairs
    u1_chain = next(
        chain
        for chain in topology.chains
        if chain.source_ref == "U1" and chain.source_net == "+3V3"
    )
    assert "power-output pin" in u1_chain.reasons[0]


def test_plan_power_routes_warns_for_missing_regulator_decap():
    circuit = _power_circuit()
    placed = [
        PlacedPart("J1", 0.0, 0.0, 0.0, "Connector:USB"),
        PlacedPart("U1", 20.0, 0.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 0.0, 0.0, "Package_TO_SOT:SOT23"),
        PlacedPart("C1", 0.0, 40.0, 0.0, "Capacitor:C_0805"),
    ]

    plan = plan_power_routes(circuit, placed)

    assert plan.net("GND") is not None
    assert any(
        "regulator has no local rail decoupling cap within 8mm" in w
        for w in plan.warnings
    )
    assert plan.topology.chains[0].ordered_refs == ["J1", "U2", "C1", "U1"]
    assert "Power topology:" in plan.summary()


def test_plan_power_routes_accepts_nearby_matching_regulator_decap():
    circuit = _power_circuit()
    placed = [
        PlacedPart("J1", 0.0, 0.0, 0.0, "Connector:USB"),
        PlacedPart("U1", 20.0, 0.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 0.0, 0.0, "Package_TO_SOT:SOT23"),
        PlacedPart("C1", 67.5, 0.0, 0.0, "Capacitor:C_0805"),
    ]

    plan = plan_power_routes(circuit, placed)

    assert not any(
        "regulator has no local rail decoupling cap" in w for w in plan.warnings
    )


def test_plan_power_routes_ignores_nearby_unrelated_decap_for_regulator():
    circuit = _power_circuit()
    other = _Net("VREF")
    gnd = next(net for net in circuit.get_nets() if net.name == "GND")
    c2 = _Part("C2", value="100nF", footprint="Capacitor:C_0805", nets=[other, gnd])
    circuit.parts.append(c2)
    circuit._nets.append(other)
    placed = [
        PlacedPart("J1", 0.0, 0.0, 0.0, "Connector:USB"),
        PlacedPart("U1", 20.0, 0.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 0.0, 0.0, "Package_TO_SOT:SOT23"),
        PlacedPart("C1", 0.0, 40.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("C2", 61.0, 0.0, 0.0, "Capacitor:C_0805"),
    ]

    plan = plan_power_routes(circuit, placed)

    assert any(
        "regulator has no local rail decoupling cap within 8mm" in w
        for w in plan.warnings
    )


def test_four_layer_power_plan_adds_plane_and_internal_rail_intents():
    circuit = _power_circuit()
    placed = [
        PlacedPart("J1", 0.0, 0.0, 0.0, "Connector:USB"),
        PlacedPart("U1", 20.0, 0.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 0.0, 0.0, "Package_TO_SOT:SOT23"),
        PlacedPart("C1", 22.0, 0.0, 0.0, "Capacitor:C_0805"),
    ]

    plan = plan_power_routes(circuit, placed, board_layers=4)
    gnd = next(intent for intent in plan.route_intents if intent.net_name == "GND")
    vbus = next(intent for intent in plan.route_intents if intent.net_name == "VBUS")

    assert isinstance(gnd, PowerRouteIntent)
    assert gnd.strategy == "plane"
    assert gnd.layer == "In1.Cu"
    assert vbus.strategy == "internal_rail"
    assert vbus.layer == "In2.Cu"


def test_two_layer_high_current_power_plan_adds_wide_trunk_intent():
    circuit = _power_circuit()
    placed = [
        PlacedPart("J1", 0.0, 0.0, 0.0, "Connector:USB"),
        PlacedPart("U2", 60.0, 0.0, 0.0, "Package_TO_SOT:SOT23"),
    ]

    plan = plan_power_routes(circuit, placed, board_layers=2)
    vbus = next(intent for intent in plan.route_intents if intent.net_name == "VBUS")

    assert vbus.strategy == "wide_trunk"
    assert vbus.width_mm == 0.8
    assert vbus.ordered_refs == ["J1", "U2"]
    assert vbus.span_mm == 60.0
