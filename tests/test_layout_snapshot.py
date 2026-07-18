from __future__ import annotations

import pickle

import pytest

from skidl_layout.context import LayoutContext
from skidl_layout.power import plan_power_routes
from skidl_layout.refinement import refine_placement
from skidl_layout.roles import is_nc_net
from skidl_layout.scoring import score_placement
from skidl_layout.snapshot import SnapshotNet, snapshot_circuit
from skidl_layout.writer import PlacedPart


# --- fakes mirroring the live skidl attribute surface ------------------------


class _Func:
    """Stand-in for a live pin.func (enum-like: has .name and a str())."""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"Pin.types.{self.name}"


class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, net, func=None, num=None, name=None):
        self.part = part
        self.net = net
        self.func = func
        self.num = num
        self.name = name
        if net is not None:
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
            self.pins.append(_Pin(self, net, func=func, num=str(idx + 1)))
        while len(self.pins) < pins:
            idx = len(self.pins)
            func = pin_funcs[idx] if pin_funcs and idx < len(pin_funcs) else None
            self.pins.append(
                _Pin(self, _Net(f"{ref}_N{idx}"), func=func, num=str(idx + 1))
            )

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
    j1 = _Part("J1", name="USB connector", footprint="Connector:USB", nets=[vbus, gnd])
    # U2 is an LDO whose VCC pin is a power output — exercises SnapshotPinFunc.
    u2 = _Part(
        "U2",
        name="LDO regulator",
        footprint="Package_TO_SOT:SOT23",
        nets=[vbus, gnd, vcc],
        pins=3,
        pin_funcs=[None, None, _Func("PWROUT")],
    )
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd, sig], pins=3)
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    return _Circuit([j1, u2, u1, c1], [vbus, vcc, gnd, sig])


BBOXES = {
    "Connector:USB": (10.0, 6.0),
    "Package_TO_SOT:SOT23": (3.0, 3.0),
    "Package_QFP:MCU": (12.0, 12.0),
    "Capacitor:C_0805": (2.0, 1.25),
}


def _placed():
    return [
        PlacedPart("J1", 0.0, 0.0, 0.0, "Connector:USB"),
        PlacedPart("U2", 20.0, 0.0, 0.0, "Package_TO_SOT:SOT23"),
        PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 0.0, 20.0, 0.0, "Capacitor:C_0805"),
    ]


def _plan_signature(plan):
    return (
        list(plan.warnings),
        [n.name for n in plan.nets],
        plan.summary(),
        len(plan.corridors),
        [
            (i.net_name, i.strategy, i.refs, i.ordered_refs, i.span_mm)
            for i in plan.route_intents
        ],
    )


def _context_signature(ctx: LayoutContext):
    return (
        {ref: (r.role, r.confidence, tuple(r.reasons)) for ref, r in ctx.roles.items()},
        ctx.pin_nets,
        ctx.net_refs,
        sorted(ctx.power_nets),
        sorted(ctx.ground_nets),
        ctx.net_ref_lists,
        ctx.pin_counts,
        {ref: sorted(toks) for ref, toks in ctx.part_tokens.items()},
    )


# --- tests -------------------------------------------------------------------


def test_snapshot_scoring_identical():
    circuit = _power_circuit()
    snap = snapshot_circuit(circuit)
    placed = _placed()

    live = score_placement(placed, circuit, BBOXES).to_dict()
    shot = score_placement(placed, snap, BBOXES).to_dict()
    assert live == shot

    # ...and with each side's own precomputed context.
    live_ctx = score_placement(
        placed, circuit, BBOXES, ctx=LayoutContext.from_circuit(circuit)
    ).to_dict()
    shot_ctx = score_placement(
        placed, snap, BBOXES, ctx=LayoutContext.from_circuit(snap)
    ).to_dict()
    assert live_ctx == shot_ctx == live


def test_snapshot_context_identical():
    circuit = _power_circuit()
    snap = snapshot_circuit(circuit)
    assert _context_signature(
        LayoutContext.from_circuit(circuit)
    ) == _context_signature(LayoutContext.from_circuit(snap))


def test_snapshot_power_plan_identical():
    circuit = _power_circuit()
    snap = snapshot_circuit(circuit)
    placed = _placed()
    assert _plan_signature(plan_power_routes(circuit, placed)) == _plan_signature(
        plan_power_routes(snap, placed)
    )


def test_snapshot_refinement_identical():
    circuit = _power_circuit()
    snap = snapshot_circuit(circuit)

    live = refine_placement(_placed(), circuit, BBOXES)
    shot = refine_placement(_placed(), snap, BBOXES)

    def sig(result):
        return [
            (p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side)
            for p in result.placed_parts
        ]

    assert sig(live) == sig(shot)
    assert live.final_penalty == shot.final_penalty


def test_snapshot_pickle_roundtrip():
    circuit = _power_circuit()
    snap = snapshot_circuit(circuit)
    blob = pickle.dumps(snap)
    restored = pickle.loads(blob)

    placed = _placed()
    live = score_placement(placed, circuit, BBOXES).to_dict()
    restored_score = score_placement(placed, restored, BBOXES).to_dict()
    assert restored_score == live

    # net interning survives the round-trip: a pin's net is the same object as
    # the one in the circuit's net list.
    net_by_id = {id(n): n for n in restored.get_nets()}
    for part in restored.parts:
        for pin in part.pins:
            if pin.net is not None and not pin.net.is_ncnet:
                assert net_by_id.get(id(pin.net)) is pin.net or pin.net.name


def test_snapshot_func_power_output_detected():
    """The LDO's PWROUT pin must be seen as a power output through the snapshot
    (exercises SnapshotPinFunc.name and __str__)."""
    from skidl_layout.power import _power_output_nets

    circuit = _power_circuit()
    snap = snapshot_circuit(circuit)
    u2_live = next(p for p in circuit.parts if p.ref == "U2")
    u2_snap = next(p for p in snap.parts if p.ref == "U2")
    assert _power_output_nets(u2_live) == _power_output_nets(u2_snap) == ["VCC"]


def test_is_nc_net_marker_and_isinstance():
    # marker path (snapshot nets)
    assert is_nc_net(SnapshotNet("NC", is_ncnet=True)) is True
    assert is_nc_net(SnapshotNet("VCC", is_ncnet=False)) is False
    assert is_nc_net(_Net("VCC")) is False

    # live-skidl isinstance path, if importable
    NCNet = pytest.importorskip("skidl.net").NCNet
    from skidl import Circuit

    ckt = Circuit()
    nc = NCNet(circuit=ckt)
    assert is_nc_net(nc) is True


def test_snapshot_marks_live_ncnet():
    """A live NCNet on a pin is marked is_ncnet=True in the snapshot."""
    skidl_net = pytest.importorskip("skidl.net")
    NCNet = skidl_net.NCNet
    from skidl import Circuit

    ckt = Circuit()
    nc = NCNet(name="NC_1", circuit=ckt)

    class _P:
        def __init__(self, part, net):
            self.part = part
            self.net = net
            self.func = None
            self.num = "1"
            self.name = "1"

    class _Pt:
        def __init__(self):
            self.ref = "U9"
            self.name = ""
            self.value = ""
            self.footprint = ""
            self.pins = [_P(self, nc)]

        def __len__(self):
            return 1

    class _Ck:
        parts = [_Pt()]

        def get_nets(self):
            return [nc]

    snap = snapshot_circuit(_Ck())
    assert snap.parts[0].pins[0].net.is_ncnet is True
