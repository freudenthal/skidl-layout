from __future__ import annotations

from skidl_layout.context import LayoutContext
from skidl_layout.snapshot import snapshot_circuit
from skidl_layout.validator import _pad_collision_pairs, validate
from skidl_layout.writer import PlacedPart

from tests.test_layout_parallel import BBOXES, _Circuit, _Net, _Part, _circuit
from tests.test_layout_validator import _test_geometry


# --- WS33: ctx-cached connectivity for validate hpwl -------------------------


def _placed():
    return [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 40.0, 25.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 30.0, 5.0, 0.0, "Connector:USB"),
    ]


def _edge_circuit():
    """Circuit exercising the hazard-#4 edge semantics:
    - a net touching a SINGLE placed ref via TWO pins (hpwl == 0.0 entry),
    - a net with an UNPLACED ref (only one placed pin -> filtered),
    - normal multi-ref nets.
    """
    vbus = _Net("VBUS")
    vcc = _Net("3V3")
    gnd = _Net("GND")
    twopin = _Net("TWO_PIN_SAME_REF")
    dangling = _Net("DANGLING")

    # U1 touches twopin via TWO of its pins (single ref, two pins -> hpwl 0.0).
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU",
               nets=[vcc, gnd, twopin, twopin], pins=4)
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    j1 = _Part("J1", name="USB connector", footprint="Connector:USB",
               nets=[vbus, gnd])
    # X9 is NOT placed; dangling also touches C1 (one placed pin -> < 2 -> drop)
    x9 = _Part("X9", name="unplaced", footprint="Capacitor:C_0805",
               nets=[dangling])
    c1.pins.append(_pin_on(c1, dangling))

    return _Circuit([u1, c1, j1, x9],
                    [vbus, vcc, gnd, twopin, dangling])


def _pin_on(part, net):
    from tests.test_layout_parallel import _Pin

    return _Pin(part, net)


def test_validate_ctx_matches_live():
    circuit = _edge_circuit()
    placed = _placed()
    ctx = LayoutContext.from_circuit(circuit)

    live = validate(placed, circuit, BBOXES)
    cached = validate(placed, circuit, BBOXES, ctx=ctx)

    assert cached.overlaps == live.overlaps
    assert cached.worst_hpwl_nets == live.worst_hpwl_nets
    assert cached.worst_hpwl_refs == live.worst_hpwl_refs
    # The two-pin-single-ref net must appear as an hpwl==0.0 entry (hazard #4).
    assert ("TWO_PIN_SAME_REF", 0.0) in live.worst_hpwl_nets
    assert ("TWO_PIN_SAME_REF", 0.0) in cached.worst_hpwl_nets


def test_validate_bare_ctx_falls_back():
    circuit = _edge_circuit()
    placed = _placed()

    live = validate(placed, circuit, BBOXES)
    # Bare LayoutContext() has hpwl_net_pins is None -> live-walk fallback.
    bare = validate(placed, circuit, BBOXES, ctx=LayoutContext())

    assert bare.worst_hpwl_nets == live.worst_hpwl_nets
    assert bare.worst_hpwl_refs == live.worst_hpwl_refs


def test_hpwl_net_pins_snapshot_equals_live():
    circuit = _circuit()
    snap_ctx = LayoutContext.from_circuit(snapshot_circuit(circuit))
    live_ctx = LayoutContext.from_circuit(circuit)

    assert snap_ctx.hpwl_net_pins == live_ctx.hpwl_net_pins


# --- WS34: through-board prefilter in _pad_collision_pairs -------------------


def test_pad_prefilter_all_smd_no_collisions():
    parts = [
        PlacedPart("R1", 10.0, 10.0, 0.0, "Demo:SMD_F", side="front"),
        PlacedPart("R2", 10.0, 10.0, 0.0, "Demo:SMD_B", side="back"),
    ]
    geometries = {
        "Demo:SMD_F": _test_geometry("Demo:SMD_F", 0.0, layers=("F.Cu",)),
        "Demo:SMD_B": _test_geometry("Demo:SMD_B", 0.0, layers=("B.Cu",)),
    }

    assert _pad_collision_pairs(parts, 0.0, geometries) == []


def test_pad_prefilter_matches_through_hole_reference():
    parts = [
        PlacedPart("J1", 10.0, 10.0, 0.0, "Demo:THT", side="front"),
        PlacedPart("R1", 10.0, 10.0, 0.0, "Demo:SMD", side="back"),
    ]
    geometries = {
        "Demo:THT": _test_geometry(
            "Demo:THT", 0.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
        "Demo:SMD": _test_geometry("Demo:SMD", 0.0, layers=("B.Cu",)),
    }

    assert _pad_collision_pairs(parts, 0.0, geometries) == [("J1", "R1")]


def test_pad_prefilter_same_side_skipped():
    parts = [
        PlacedPart("J1", 10.0, 10.0, 0.0, "Demo:THTA", side="front"),
        PlacedPart("J2", 10.0, 10.0, 0.0, "Demo:THTB", side="front"),
    ]
    geometries = {
        "Demo:THTA": _test_geometry(
            "Demo:THTA", 0.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
        "Demo:THTB": _test_geometry(
            "Demo:THTB", 0.0, pad_type="thru_hole", layers=("*.Cu", "*.Mask")
        ),
    }

    assert _pad_collision_pairs(parts, 0.0, geometries) == []
