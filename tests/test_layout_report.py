from __future__ import annotations

from skidl_layout.constraints import BoardOutline, FixedPosition, LayoutConstraints
from skidl_layout.engine import plan_layout
from skidl_layout.report import NetExplanation, PartExplanation, PlacementReport


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
        self.nets = nets

    def get_nets(self):
        return self.nets


BBOXES = {
    "Connector:USB": (10.0, 5.0),
    "Package_QFP:MCU": (12.0, 12.0),
    "Capacitor:C_0805": (2.0, 1.25),
}


def test_report_part_net_and_top_risk_helpers():
    report = PlacementReport(
        selected="baseline",
        hard_violations=["overlap: U1 <-> U2"],
        risky_nets=[("SIG", 42.0)],
        congestion_regions=["(0.0,0.0)-(10.0,10.0): 7.0 [net SIG; pin escape U1]"],
        power_corridors=["VBUS: 0.80mm on F.Cu across 2 refs"],
        part_reasons={"U1": ["placed by baseline strategy"]},
        warnings=["connector J1 is 20.0mm from nearest edge"],
    )

    part = report.part("U1")
    sig = report.net("SIG")
    vbus = report.net("VBUS")
    risks = report.top_risks()

    assert isinstance(part, PartExplanation)
    assert "baseline" in part.summary()
    assert part.violations == ["overlap: U1 <-> U2"]
    assert isinstance(sig, NetExplanation)
    assert sig.hpwl_mm == 42.0
    assert sig.congestion_regions
    assert sig.next_actions
    assert "power corridor" in vbus.summary()
    assert risks[0].startswith("hard violation:")
    assert any("net SIG" in risk for risk in risks)


def test_plan_layout_populates_structured_net_explanations():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    sig = _Net("SIG")
    j1 = _Part("J1", "Connector:USB", name="USB connector", nets=[vbus, gnd], pins=4)
    u1 = _Part("U1", "Package_QFP:MCU", name="MCU", nets=[vbus, gnd, sig], pins=8)
    c1 = _Part("C1", "Capacitor:C_0805", value="100nF", nets=[vbus, gnd])
    circuit = _Circuit([j1, u1, c1], [vbus, gnd, sig])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=BoardOutline(100.0, 50.0),
            fixed=[
                FixedPosition("J1", 10.0, 25.0),
                FixedPosition("U1", 80.0, 25.0),
            ],
        ),
    )
    report = result.report

    assert report is not None
    assert report.part("J1").reasons
    vbus_report = report.net("VBUS")
    assert vbus_report.power_corridors or vbus_report.hpwl_mm is not None
    assert {"J1", "U1"}.issubset(set(vbus_report.refs))
    assert vbus_report.next_actions
    assert report.top_risks(limit=3)
