from __future__ import annotations

import pytest

from skidl_layout.candidates import generate_placement_candidates
from skidl_layout.constraints import (
    AlignConstraint,
    BoardOutline,
    DistributeConstraint,
    EdgeAnchor,
    FaceEdgeConstraint,
    LayoutConstraints,
    NearConstraint,
)
from skidl_layout.hierarchy import PlacementGroup
from skidl_layout.intent import (
    ChannelSlot,
    MatingIntent,
    PlacementIntent,
    PlacementIntentPlan,
    RepeatedChannelIntent,
)
from skidl_layout.power import PowerChain, PowerTopology


class _Part:
    def __init__(self, ref, footprint, pins=4):
        self.ref = ref
        self.footprint = footprint
        self.value = ""
        self.name = ""
        self.pins = [object() for _ in range(pins)]

    def __len__(self):
        return len(self.pins)


def test_generate_placement_candidates_is_deterministic_and_named():
    connector = _Part("J1", "Connector:USB", pins=16)
    group = PlacementGroup(name="", parts=[connector], adjacency={})
    constraints = LayoutConstraints(outline=BoardOutline(50.0, 30.0))
    intent = PlacementIntentPlan(
        edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=25.0)]
    )

    candidates = generate_placement_candidates(
        {None: group},
        constraints,
        {"Connector:USB": (10.0, 5.0)},
        intent_plan=intent,
    )

    assert [candidate.name for candidate in candidates[:5]] == [
        "baseline",
        "connector_edge_first",
        "power_first",
        "power_topology_first",
        "cluster_first",
    ]
    baseline_j1 = candidates[0].placed_parts[0]
    edge_j1 = candidates[1].placed_parts[0]
    assert baseline_j1.y_mm != pytest.approx(edge_j1.y_mm)
    # courtyard bottom edge sits 0.5mm inside the board edge (default inset)
    assert edge_j1.y_mm + 2.5 == pytest.approx(29.5)


def test_module_socket_candidate_biases_module_to_internal_zone():
    module = _Part("J1", "Module:Electrosmith_Daisy_Seed", pins=40)
    aux = _Part("R1", "Resistor:R_0603", pins=2)
    group = PlacementGroup(name="", parts=[module, aux], adjacency={})
    constraints = LayoutConstraints(outline=BoardOutline(100.0, 60.0))
    intent = PlacementIntentPlan()

    intent.intents["J1"] = [
        PlacementIntent("J1", "module_socket", 86, ["plug-in module/socket"])
    ]

    candidates = generate_placement_candidates(
        {None: group},
        constraints,
        {
            "Module:Electrosmith_Daisy_Seed": (18.0, 53.0),
            "Resistor:R_0603": (1.6, 0.8),
        },
        intent_plan=intent,
    )
    candidate = next(
        candidate for candidate in candidates if candidate.name == "module_socket_central"
    )

    module_zone = next(zone for zone in candidate.constraints.zones if zone.refs == ["J1"])
    assert module_zone.x_min == pytest.approx(25.0)
    assert module_zone.x_max == pytest.approx(75.0)
    assert module_zone.y_min == pytest.approx(2.4)
    assert module_zone.y_max == pytest.approx(57.6)
    assert "placement zone" in "; ".join(candidate.ref_reasons["J1"])


def test_repeated_channel_candidate_distributes_channel_refs():
    sensor_0 = _Part("U2", "Sensor:S", pins=3)
    sensor_1 = _Part("U3", "Sensor:S", pins=3)
    group = PlacementGroup(name="", parts=[sensor_0, sensor_1], adjacency={})
    constraints = LayoutConstraints(outline=BoardOutline(100.0, 50.0))
    intent = PlacementIntentPlan(
        repeated_channels=[
            RepeatedChannelIntent(
                name="channel",
                refs=["U2", "U3"],
                channel_numbers=[0, 1],
                refs_by_channel={0: ["U2"], 1: ["U3"]},
                pattern="test",
                slots=[
                    ChannelSlot(channel_number=0, slot_index=0, refs=["U2"]),
                    ChannelSlot(channel_number=1, slot_index=1, refs=["U3"]),
                ],
            )
        ]
    )

    candidates = generate_placement_candidates(
        {None: group},
        constraints,
        {"Sensor:S": (4.0, 4.0)},
        intent_plan=intent,
    )
    array_candidate = next(
        candidate for candidate in candidates if candidate.name == "repeated_channel_array"
    )
    placed = {part.ref: part for part in array_candidate.placed_parts}

    assert placed["U2"].x_mm == pytest.approx(12.0)
    assert placed["U3"].x_mm == pytest.approx(88.0)
    assert placed["U2"].y_mm == pytest.approx(12.5)
    assert placed["U3"].y_mm == pytest.approx(12.5)
    assert "placement zone" in "; ".join(array_candidate.ref_reasons["U2"])
    assert "channel slot: CH0" in "; ".join(array_candidate.ref_reasons["U2"])
    assert any(zone.refs == ["U2"] for zone in array_candidate.constraints.zones)


def test_repeated_channel_grid_anchors_sensors_before_local_passives():
    sensor_0 = _Part("U2", "Sensor:S", pins=3)
    cap_0 = _Part("C2", "Capacitor:C_0603", pins=2)
    sensor_1 = _Part("U3", "Sensor:S", pins=3)
    cap_1 = _Part("C3", "Capacitor:C_0603", pins=2)
    group = PlacementGroup(
        name="",
        parts=[sensor_0, cap_0, sensor_1, cap_1],
        adjacency={},
    )
    constraints = LayoutConstraints(outline=BoardOutline(100.0, 50.0))
    intent = PlacementIntentPlan(
        repeated_channels=[
            RepeatedChannelIntent(
                name="channel",
                refs=["U2", "C2", "U3", "C3"],
                channel_numbers=[0, 1],
                refs_by_channel={0: ["U2", "C2"], 1: ["U3", "C3"]},
                pattern="test",
                slots=[
                    ChannelSlot(
                        channel_number=0,
                        slot_index=0,
                        refs=["U2", "C2"],
                        sensor_refs=["U2"],
                        passive_refs=["C2"],
                    ),
                    ChannelSlot(
                        channel_number=1,
                        slot_index=1,
                        refs=["U3", "C3"],
                        sensor_refs=["U3"],
                        passive_refs=["C3"],
                    ),
                ],
            )
        ],
        near_constraints=[
            NearConstraint(ref="C2", target_ref="U2", distance_mm=5.0),
            NearConstraint(ref="C3", target_ref="U3", distance_mm=5.0),
        ],
    )

    candidates = generate_placement_candidates(
        {None: group},
        constraints,
        {
            "Sensor:S": (4.0, 4.0),
            "Capacitor:C_0603": (1.6, 0.8),
        },
        intent_plan=intent,
    )
    array_candidate = next(
        candidate for candidate in candidates if candidate.name == "repeated_channel_array"
    )
    placed = {part.ref: part for part in array_candidate.placed_parts}
    distribute_refs = {
        tuple(constraint.refs)
        for constraint in array_candidate.constraints.distribute
        if constraint.axis == "x"
    }

    assert ("U2", "U3") in distribute_refs
    assert all("C" not in ref for refs in distribute_refs for ref in refs)
    assert placed["U2"].x_mm == pytest.approx(12.0)
    assert placed["U3"].x_mm == pytest.approx(88.0)
    assert abs(placed["C2"].x_mm - placed["U2"].x_mm) <= 5.0
    assert abs(placed["C3"].x_mm - placed["U3"].x_mm) <= 5.0


def test_inferred_grid_constraints_do_not_override_explicit_floorplan_refs():
    leds = [_Part(f"D{idx}", "LED_SMD:LED_0805", pins=2) for idx in range(1, 4)]
    group = PlacementGroup(name="", parts=leds, adjacency={})
    explicit_align = AlignConstraint(
        refs=["D1", "D2", "D3"],
        axis="y",
        value_mm=8.0,
    )
    constraints = LayoutConstraints(
        outline=BoardOutline(60.0, 30.0),
        align=[explicit_align],
    )
    intent = PlacementIntentPlan(
        align_constraints=[
            AlignConstraint(refs=["D1", "D2", "D3"], axis="y", value_mm=20.0)
        ],
        distribute_constraints=[
            DistributeConstraint(
                refs=["D1", "D2", "D3"],
                axis="x",
                start_mm=10.0,
                end_mm=50.0,
            )
        ],
    )

    candidates = generate_placement_candidates(
        {None: group},
        constraints,
        {"LED_SMD:LED_0805": (2.0, 1.25)},
        intent_plan=intent,
    )
    edge_candidate = next(
        candidate for candidate in candidates if candidate.name == "connector_edge_first"
    )

    assert edge_candidate.constraints.align == [explicit_align]
    assert edge_candidate.constraints.distribute == []


def test_power_topology_candidate_adds_chain_constraints_and_reasons():
    source = _Part("J1", "Connector:USB", pins=4)
    regulator = _Part("U2", "Package_TO_SOT:SOT23", pins=3)
    cap = _Part("C1", "Capacitor:C_0805", pins=2)
    load = _Part("U1", "Package_QFP:MCU", pins=8)
    group = PlacementGroup(
        name="",
        parts=[source, regulator, cap, load],
        adjacency={},
    )
    topology = PowerTopology(
        chains=[
            PowerChain(
                source_ref="J1",
                source_net="VBUS",
                converter_refs=["U2"],
                storage_refs=["C1"],
                load_refs=["U1"],
                output_nets=["VCC"],
            )
        ]
    )

    candidates = generate_placement_candidates(
        {None: group},
        LayoutConstraints(outline=BoardOutline(80.0, 50.0)),
        {
            "Connector:USB": (10.0, 5.0),
            "Package_TO_SOT:SOT23": (3.0, 3.0),
            "Capacitor:C_0805": (2.0, 1.25),
            "Package_QFP:MCU": (12.0, 12.0),
        },
        power_topology=topology,
    )
    candidate = next(
        candidate for candidate in candidates if candidate.name == "power_topology_first"
    )

    assert len(candidate.constraints.near) == 3
    assert candidate.constraints.near[0].target_ref == "J1"
    assert candidate.constraints.near[0].ref == "U2"
    assert "power chain: VBUS from J1" in "; ".join(candidate.ref_reasons["U2"])


def test_candidates_preserve_mating_face_edges_and_reasons():
    button = _Part("SW1", "Button:SW", pins=2)
    group = PlacementGroup(name="", parts=[button], adjacency={})
    constraints = LayoutConstraints(outline=BoardOutline(40.0, 30.0))
    intent = PlacementIntentPlan(
        face_edges=[FaceEdgeConstraint("SW1", "right")],
        mating_intents=[
            MatingIntent(
                ref="SW1",
                kind="button",
                edge_preference="right",
                mating_side="user_control",
                confidence=0.8,
            )
        ],
    )

    candidates = generate_placement_candidates(
        {None: group},
        constraints,
        {"Button:SW": (6.0, 4.0)},
        intent_plan=intent,
    )
    edge_candidate = next(
        candidate for candidate in candidates if candidate.name == "connector_edge_first"
    )

    assert any(face.ref == "SW1" for face in edge_candidate.constraints.face_edges)
    assert "mating intent: button facing right" in "; ".join(
        edge_candidate.ref_reasons["SW1"]
    )
