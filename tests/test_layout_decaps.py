from __future__ import annotations

from skidl_layout.decaps import (
    infer_decap_placement_intents,
    measure_decap_pad_distances,
    refine_decaps,
    refine_candidate_decaps,
)
from skidl_layout.candidates import PlacementCandidate
from skidl_layout.constraints import BoardOutline, EdgeAnchor, FixedPosition, LayoutConstraints
from skidl_layout.geometry import FootprintGeometry, PadGeometry
from skidl_layout.validator import validate
from skidl_layout.writer import PlacedPart


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
        self.net = net
        net._pins.append(self)


class _Part:
    def __init__(self, ref, value="", footprint="", pins=None, name=""):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.name = name
        self.pins = []
        for num, net in pins or []:
            self.pins.append(_Pin(self, num, net))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self._nets = nets

    def get_nets(self):
        return self._nets


def _basic_geometries():
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


def test_infers_decap_parent_actual_power_and_ground_pads():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    sig = _Net("SIG")
    parent = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd), ("3", sig)],
        name="MCU",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd, sig])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 20.0, 30.0, 0.0, "Pkg:Cap"),
    ]

    intents = infer_decap_placement_intents(circuit, placed, _basic_geometries())

    assert len(intents) == 1
    assert intents[0].parent_ref == "U1"
    assert intents[0].target_power_pin == "1"
    assert intents[0].target_ground_pin == "2"
    assert intents[0].target_power_xy == (16.0, 18.5)
    assert intents[0].target_ground_xy == (16.0, 21.5)


def test_refine_decaps_moves_cap_to_actual_pad_side_and_rotates_it():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    sig = _Net("SIG")
    parent = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd), ("3", sig)],
        name="MCU",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd, sig])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 20.0, 30.0, 0.0, "Pkg:Cap"),
    ]

    result = refine_decaps(
        placed,
        circuit,
        _basic_geometries(),
        {"Pkg:MCU": (10.0, 10.0), "Pkg:Cap": (2.0, 1.2)},
    )
    refined = {part.ref: part for part in result.placed_parts}

    assert refined["C1"].x_mm < 16.0
    assert 17.0 < refined["C1"].y_mm < 23.0
    assert refined["C1"].rot_deg == 270.0
    assert "actual U1 VDD/GND pads" in result.ref_reasons["C1"][0]


def test_refine_candidate_decaps_marks_ref_as_pin_gravity_anchored():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd)],
        name="MCU",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd])
    candidate = PlacementCandidate(
        name="test",
        placed_parts=[
            PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
            PlacedPart("C1", 20.0, 30.0, 0.0, "Pkg:Cap"),
        ],
        constraints=LayoutConstraints(outline=BoardOutline(50.0, 40.0)),
    )

    refine_candidate_decaps(
        candidate,
        circuit,
        _basic_geometries(),
        {"Pkg:MCU": (10.0, 10.0), "Pkg:Cap": (2.0, 1.2)},
    )

    assert "C1" in candidate.pin_gravity_anchored_refs


def test_refine_decaps_treats_fixed_cap_as_soft_seed():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd)],
        name="MCU",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 6.0, 6.0, 0.0, "Pkg:Cap"),
    ]
    constraints = LayoutConstraints(
        outline=BoardOutline(50.0, 40.0),
        fixed=[
            FixedPosition("U1", 20.0, 20.0),
            FixedPosition("C1", 6.0, 6.0),
        ],
    )

    result = refine_decaps(
        placed,
        circuit,
        _basic_geometries(),
        {"Pkg:MCU": (10.0, 10.0), "Pkg:Cap": (2.0, 1.2)},
        constraints=constraints,
    )
    refined = {part.ref: part for part in result.placed_parts}
    distances = measure_decap_pad_distances(result.placed_parts, circuit, _basic_geometries())

    assert refined["U1"].x_mm == 20.0
    assert refined["C1"].x_mm != 6.0 or refined["C1"].y_mm != 6.0
    assert distances["C1"].average_pad_distance_mm < 6.0
    assert "actual U1 VDD/GND pads" in result.ref_reasons["C1"][0]


def test_refine_decaps_final_position_clears_parent_pads():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "U1",
        footprint="Pkg:MSOP",
        pins=[("4", gnd), ("8", vdd)],
        name="MCP9808",
    )
    cap = _Part(
        "C1",
        value="100nF",
        footprint="Pkg:C0603",
        pins=[("1", vdd), ("2", gnd)],
    )
    circuit = _Circuit([parent, cap], [vdd, gnd])
    geometries = {
        "Pkg:MSOP": FootprintGeometry(
            footprint="Pkg:MSOP",
            pads=[
                PadGeometry("4", -2.1125, 0.975, 1.625, 0.4),
                PadGeometry("8", 2.1125, -0.975, 1.625, 0.4),
            ],
            body_bounds=(-1.5, -1.5, 1.5, 1.5),
            courtyard_bounds=(-3.18, -1.75, 3.18, 1.75),
        ),
        "Pkg:C0603": FootprintGeometry(
            footprint="Pkg:C0603",
            pads=[
                PadGeometry("1", -0.775, 0.0, 0.9, 0.95),
                PadGeometry("2", 0.775, 0.0, 0.9, 0.95),
            ],
            body_bounds=(-0.8, -0.4, 0.8, 0.4),
            courtyard_bounds=(-1.48, -0.73, 1.48, 0.73),
        ),
    }
    fp_bboxes = {"Pkg:MSOP": (6.36, 3.5), "Pkg:C0603": (2.96, 1.46)}
    placed = [
        PlacedPart("U1", 15.0, 9.2, 0.0, "Pkg:MSOP"),
        PlacedPart("C1", 20.0, 9.2, 90.0, "Pkg:C0603"),
    ]
    constraints = LayoutConstraints(
        outline=BoardOutline(29.3, 23.6),
        fixed=[
            FixedPosition("U1", 15.0, 9.2),
            FixedPosition("C1", 20.0, 9.2),
        ],
    )

    result = refine_decaps(
        placed,
        circuit,
        geometries,
        fp_bboxes,
        constraints=constraints,
    )
    validation = validate(
        result.placed_parts,
        circuit,
        fp_bboxes,
        clearance_mm=0.5,
        outline=constraints.outline,
        fp_geometries=geometries,
    )
    refined = {part.ref: part for part in result.placed_parts}
    distances = measure_decap_pad_distances(result.placed_parts, circuit, geometries)

    assert validation.overlaps == []
    assert refined["C1"].x_mm > 18.9
    assert distances["C1"].average_pad_distance_mm < 4.5


def test_refine_decaps_uses_parent_body_not_large_module_courtyard():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "U1",
        footprint="Pkg:Module",
        pins=[("1", vdd), ("2", gnd)],
        name="ESP32_MODULE",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd])
    geometries = {
        "Pkg:Module": FootprintGeometry(
            footprint="Pkg:Module",
            pads=[
                PadGeometry("1", -8.75, -0.6, 1.0, 0.8),
                PadGeometry("2", -8.75, 0.6, 1.0, 0.8),
            ],
            body_bounds=(-9.0, -6.0, 9.0, 6.0),
            courtyard_bounds=(-24.0, -10.0, 24.0, 10.0),
        ),
        **_basic_geometries(),
    }
    placed = [
        PlacedPart("U1", 38.0, 30.0, 0.0, "Pkg:Module"),
        PlacedPart("C1", 11.0, 25.2, 270.0, "Pkg:Cap"),
    ]

    result = refine_decaps(
        placed,
        circuit,
        geometries,
        {"Pkg:Module": (48.0, 20.0), "Pkg:Cap": (2.0, 1.2)},
    )
    refined = {part.ref: part for part in result.placed_parts}
    distances = measure_decap_pad_distances(result.placed_parts, circuit, geometries)

    assert refined["C1"].x_mm > 25.0
    assert distances["C1"].average_pad_distance_mm < 3.0


def test_refine_decaps_keeps_edge_anchored_cap_locked():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd)],
        name="MCU",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 6.0, 6.0, 0.0, "Pkg:Cap"),
    ]
    constraints = LayoutConstraints(
        outline=BoardOutline(50.0, 40.0),
        edge_anchors=[EdgeAnchor("C1", "left")],
    )

    result = refine_decaps(
        placed,
        circuit,
        _basic_geometries(),
        {"Pkg:MCU": (10.0, 10.0), "Pkg:Cap": (2.0, 1.2)},
        constraints=constraints,
    )
    refined = {part.ref: part for part in result.placed_parts}

    assert refined["C1"].x_mm == 6.0
    assert refined["C1"].y_mm == 6.0
    assert "C1" not in result.ref_reasons


def test_measures_decap_distance_to_actual_parent_pads_not_origin():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    sig = _Net("SIG")
    parent = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd), ("3", sig)],
        name="MCU",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd, sig])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 16.0, 20.0, 270.0, "Pkg:Cap"),
    ]

    distances = measure_decap_pad_distances(placed, circuit, _basic_geometries())

    assert distances["C1"].parent_ref == "U1"
    assert distances["C1"].average_pad_distance_mm < 1.5


def test_refine_decaps_avoids_parent_with_off_center_origin():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "U1",
        footprint="Pkg:OffCenterIC",
        pins=[("1", vdd), ("2", gnd)],
        name="MCU",
    )
    cap = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, cap], [vdd, gnd])
    geometries = {
        "Pkg:OffCenterIC": FootprintGeometry(
            footprint="Pkg:OffCenterIC",
            pads=[
                PadGeometry("1", 0.5, 5.0, 0.6, 0.6),
                PadGeometry("2", 0.5, 6.0, 0.6, 0.6),
            ],
            body_bounds=(0.0, 0.0, 10.0, 10.0),
        ),
        **_basic_geometries(),
    }
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:OffCenterIC"),
        PlacedPart("C1", 20.0, 32.0, 0.0, "Pkg:Cap"),
    ]

    result = refine_decaps(
        placed,
        circuit,
        geometries,
        {"Pkg:OffCenterIC": (10.0, 10.0), "Pkg:Cap": (2.0, 1.2)},
    )
    refined = {part.ref: part for part in result.placed_parts}
    parent_bounds = geometries["Pkg:OffCenterIC"].transformed_bounds(refined["U1"])
    cap_bounds = geometries["Pkg:Cap"].transformed_bounds(refined["C1"])

    assert cap_bounds[2] <= parent_bounds[0] or cap_bounds[0] >= parent_bounds[2]


def test_multiple_decaps_distribute_across_parent_power_pads():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd), ("3", vdd)],
        name="MCU",
    )
    c1 = _Part("C1", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    c2 = _Part("C2", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([parent, c1, c2], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 20.0, 30.0, 0.0, "Pkg:Cap"),
        PlacedPart("C2", 20.0, 34.0, 0.0, "Pkg:Cap"),
    ]

    intents = infer_decap_placement_intents(circuit, placed, _basic_geometries())
    by_ref = {intent.ref: intent for intent in intents}

    assert by_ref["C1"].target_power_pin == "1"
    assert by_ref["C2"].target_power_pin == "3"


def test_named_sensor_decap_prefers_matching_parent_over_larger_shared_rail_ic():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    big_mcu = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd), ("3", vdd)],
        name="ESP32-S3 module",
    )
    sensor = _Part(
        "U4",
        footprint="Pkg:MCU",
        pins=[("1", gnd), ("2", vdd), ("3", vdd)],
        name="BME280 environmental sensor",
    )
    cap = _Part(
        "CBME1",
        value="100nF",
        footprint="Pkg:Cap",
        pins=[("1", vdd), ("2", gnd)],
    )
    circuit = _Circuit([big_mcu, sensor, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("U4", 70.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("CBME1", 22.0, 22.0, 0.0, "Pkg:Cap"),
    ]

    intents = infer_decap_placement_intents(circuit, placed, _basic_geometries())

    assert intents[0].parent_ref == "U4"


def test_generic_decap_prefers_nearest_same_rail_parent():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    left = _Part(
        "U1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd)],
        name="MCU",
    )
    right = _Part(
        "U2",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd)],
        name="Sensor",
    )
    cap = _Part("C9", value="100nF", footprint="Pkg:Cap", pins=[("1", vdd), ("2", gnd)])
    circuit = _Circuit([left, right, cap], [vdd, gnd])
    placed = [
        PlacedPart("U1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("U2", 70.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C9", 68.0, 22.0, 0.0, "Pkg:Cap"),
    ]

    intents = infer_decap_placement_intents(circuit, placed, _basic_geometries())

    assert intents[0].parent_ref == "U2"


def test_decap_does_not_use_bulk_cap_as_parent():
    vdd = _Net("VDD")
    gnd = _Net("GND")
    parent = _Part(
        "A1",
        footprint="Pkg:MCU",
        pins=[("1", vdd), ("2", gnd), ("3", vdd)],
        name="Raspberry Pi Pico module",
    )
    decap = _Part(
        "C1",
        value="100nF",
        footprint="Pkg:Cap",
        pins=[("1", vdd), ("2", gnd)],
    )
    bulk = _Part(
        "C2",
        value="10uF",
        footprint="Pkg:Cap",
        pins=[("1", vdd), ("2", gnd)],
    )
    circuit = _Circuit([parent, decap, bulk], [vdd, gnd])
    placed = [
        PlacedPart("A1", 20.0, 20.0, 0.0, "Pkg:MCU"),
        PlacedPart("C1", 60.0, 20.0, 0.0, "Pkg:Cap"),
        PlacedPart("C2", 61.0, 20.0, 0.0, "Pkg:Cap"),
    ]

    intents = infer_decap_placement_intents(circuit, placed, _basic_geometries())

    assert len(intents) == 1
    assert intents[0].parent_ref == "A1"
