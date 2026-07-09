from __future__ import annotations

from skidl_layout.congestion import build_congestion_map
from skidl_layout.constraints import BoardOutline, KeepOut
from skidl_layout.power import plan_power_routes
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
    def __init__(self, ref, name="", footprint="Pkg:X", nets=None, pins=2):
        self.ref = ref
        self.name = name
        self.value = ""
        self.footprint = footprint
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


def _crossed_circuit():
    a = _Net("A")
    b = _Net("B")
    p1 = _Part("U1", nets=[a, b], pins=8)
    p2 = _Part("U2", nets=[a, b], pins=8)
    p3 = _Part("U3", nets=[a, b], pins=8)
    return _Circuit([p1, p2, p3], [a, b])


def test_dense_cluster_has_higher_peak_congestion_than_spread_layout():
    circuit = _crossed_circuit()
    outline = BoardOutline(80.0, 50.0)
    dense = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:X"),
        PlacedPart("U2", 24.0, 20.0, 0.0, "Pkg:X"),
        PlacedPart("U3", 22.0, 24.0, 0.0, "Pkg:X"),
    ]
    spread = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Pkg:X"),
        PlacedPart("U2", 40.0, 25.0, 0.0, "Pkg:X"),
        PlacedPart("U3", 70.0, 40.0, 0.0, "Pkg:X"),
    ]

    dense_map = build_congestion_map(dense, circuit, outline=outline)
    spread_map = build_congestion_map(spread, circuit, outline=outline)

    assert dense_map.peak_demand > spread_map.peak_demand
    assert dense_map.top_regions()[0].reasons


def test_keepout_and_power_corridor_add_congestion_pressure():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    source = _Part("J1", name="USB connector", nets=[vbus, gnd], pins=4)
    load = _Part("U1", name="load", nets=[vbus, gnd], pins=8)
    circuit = _Circuit([source, load], [vbus, gnd])
    placed = [
        PlacedPart("J1", 10.0, 10.0, 0.0, "Pkg:X"),
        PlacedPart("U1", 70.0, 30.0, 0.0, "Pkg:X"),
    ]
    outline = BoardOutline(90.0, 50.0)
    base = build_congestion_map(placed, circuit, outline=outline)
    with_keepout = build_congestion_map(
        placed,
        circuit,
        outline=outline,
        keepouts=[KeepOut(35.0, 15.0, 55.0, 35.0)],
        power_plan=plan_power_routes(circuit, placed),
    )

    assert with_keepout.peak_demand > base.peak_demand
    assert any("keepout" in reason for reason in with_keepout.top_regions()[0].reasons)


def test_four_layer_board_reduces_average_routing_congestion():
    circuit = _crossed_circuit()
    outline = BoardOutline(100.0, 60.0)
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Pkg:X"),
        PlacedPart("U2", 50.0, 30.0, 0.0, "Pkg:X"),
        PlacedPart("U3", 90.0, 50.0, 0.0, "Pkg:X"),
    ]

    two_layer = build_congestion_map(
        placed,
        circuit,
        outline=outline,
        board_layers=2,
    )
    four_layer = build_congestion_map(
        placed,
        circuit,
        outline=outline,
        board_layers=4,
    )

    assert four_layer.average_demand < two_layer.average_demand
