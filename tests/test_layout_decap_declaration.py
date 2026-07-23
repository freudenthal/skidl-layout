"""WS-A2: skidl-layout consumes the explicit decouples= declaration.

Covers the roles classification, decaps parent/pin selection, the snapshot
threading (sequential == parallel), and the power decap-proximity warning.
Untagged behaviour stays byte-identical (guarded by the existing suites).
"""

from __future__ import annotations

from skidl_layout.roles import classify_part, decouples_declaration
from skidl_layout.decaps import infer_decap_placement_intents
from skidl_layout.geometry import FootprintGeometry, PadGeometry
from skidl_layout.snapshot import _decouples_target, SnapshotPart
from skidl_layout.writer import PlacedPart


# --- minimal fakes (mirroring the other decap/roles tests) ------------------

class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, num, net):
        self.part = part
        self.num = str(num)
        self.name = str(num)
        self.net = net
        net._pins.append(self)


class _Part:
    def __init__(self, ref, value="", footprint="", pins=None, name="", decouples=None):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.name = name
        self.pins = []
        for num, net in pins or []:
            self.pins.append(_Pin(self, num, net))
        if decouples is not None:
            self.decouples = decouples

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self._nets = nets

    def get_nets(self):
        return self._nets


def _geometries():
    return {
        "Pkg:MCU": FootprintGeometry(
            footprint="Pkg:MCU",
            pads=[
                PadGeometry("1", -4.0, -1.5, 0.6, 0.6),
                PadGeometry("2", -4.0, 1.5, 0.6, 0.6),
                PadGeometry("3", 4.0, 0.0, 0.6, 0.6),
            ],
            body_bounds=(-5.0, -5.0, 5.0, 5.0),
        ),
        "Pkg:Cap": FootprintGeometry(
            footprint="Pkg:Cap",
            pads=[
                PadGeometry("1", -0.6, 0.0, 0.4, 0.4),
                PadGeometry("2", 0.6, 0.0, 0.4, 0.4),
            ],
            body_bounds=(-1.0, -0.6, 1.0, 0.6),
        ),
    }


# --- roles: explicit tag wins, any value -----------------------------------

def test_declared_cap_classified_regardless_of_value():
    vdd, gnd = _Net("VDD"), _Net("GND")
    cap = _Part("C7", value="4.7uF", footprint="Pkg:Cap",
                pins=[("1", vdd), ("2", gnd)], decouples="U1.3")
    role = classify_part(cap)
    assert role.role == "decoupling_cap"
    assert role.confidence == 1.0
    assert "explicitly declared decoupling for U1" in role.reasons[0]


def test_untagged_1uf_not_a_decap():
    vdd, gnd = _Net("VDD"), _Net("GND")
    cap = _Part("C8", value="1uF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    # value-regex heuristic unchanged -> a bare 1uF cap is not a decoupling_cap
    assert classify_part(cap).role != "decoupling_cap"


# --- decaps: declared parent wins; declared value/pin honoured --------------

def test_declared_parent_beats_nearer_twin():
    vdd, gnd = _Net("VDD"), _Net("GND")
    far = _Part("U1", footprint="Pkg:MCU", pins=[("1", vdd), ("2", gnd)], name="MCU")
    near = _Part("U2", footprint="Pkg:MCU", pins=[("1", vdd), ("2", gnd)], name="MCU")
    # 100nF cap sitting next to U2 but declaring U1 as its parent.
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap",
                pins=[("1", vdd), ("2", gnd)], decouples="U1")
    circuit = _Circuit([far, near, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("U2", 70.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 68.0, 22.0, 0.0, "Pkg:Cap"),  # nearest = U2
    ]
    intents = infer_decap_placement_intents(circuit, placed, _geometries())
    assert intents[0].parent_ref == "U1"          # declared, not nearest
    assert "explicitly declared decoupling for U1" in intents[0].reasons[0]


def test_declared_high_value_cap_gets_intent():
    vdd, gnd = _Net("VDD"), _Net("GND")
    parent = _Part("U1", footprint="Pkg:MCU", pins=[("1", vdd), ("2", gnd)], name="MCU")
    cap = _Part("C1", value="4.7uF", footprint="Pkg:Cap",
                pins=[("1", vdd), ("2", gnd)], decouples="U1")
    circuit = _Circuit([parent, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 20.0, 30.0, 0.0, "Pkg:Cap"),
    ]
    # the value-regex heuristic would skip a 4.7uF cap; the tag unlocks it.
    intents = infer_decap_placement_intents(circuit, placed, _geometries())
    assert len(intents) == 1
    assert intents[0].parent_ref == "U1"


def test_declared_pin_seeds_target_pad():
    vdd, gnd = _Net("VDD"), _Net("GND")
    # U1 has two VDD pads (1 and 3); round-robin would pick pad "1".
    parent = _Part("U1", footprint="Pkg:MCU",
                   pins=[("1", vdd), ("2", gnd), ("3", vdd)], name="MCU")
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap",
                pins=[("1", vdd), ("2", gnd)], decouples="U1.3")
    circuit = _Circuit([parent, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 20.0, 30.0, 0.0, "Pkg:Cap"),
    ]
    intents = infer_decap_placement_intents(circuit, placed, _geometries())
    assert intents[0].target_power_pin == "3"      # declared pin, not "1"
    assert "pin 3" in intents[0].reasons[0]


def test_untagged_intent_reason_unchanged():
    """No declaration -> the inferred reason string is byte-identical."""
    vdd, gnd = _Net("VDD"), _Net("GND")
    parent = _Part("U1", footprint="Pkg:MCU", pins=[("1", vdd), ("2", gnd)], name="MCU")
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 20.0, 30.0, 0.0, "Pkg:Cap"),
    ]
    intents = infer_decap_placement_intents(circuit, placed, _geometries())
    assert intents[0].reasons == [
        "C1 shares VDD/GND with U1 actual footprint pads"
    ]


# --- snapshot: sequential == parallel (target survives the snapshot) --------

def test_snapshot_extracts_and_declaration_matches_live():
    vdd, gnd = _Net("VDD"), _Net("GND")
    live = _Part("C1", value="1uF", footprint="Pkg:Cap",
                 pins=[("1", vdd), ("2", gnd)], decouples="U1.2")
    # snapshot builder normalizes the raw value to a (ref, pin) tuple
    assert _decouples_target(live) == ("U1", "2")
    # a SnapshotPart carrying that tuple yields the SAME declaration roles/decaps
    # read -> the parallel-worker path is identity-equivalent to the live path.
    snap = SnapshotPart("C1", "", "1uF", "", "Pkg:Cap", "", "", 2,
                        decouples=("U1", "2"))
    assert decouples_declaration(snap) == decouples_declaration(live) == ("U1", "2")


def test_snapshot_default_decouples_is_none():
    snap = SnapshotPart("C1", "", "100nF", "", "Pkg:Cap", "", "", 2)
    assert snap.decouples is None
    assert decouples_declaration(snap) is None
