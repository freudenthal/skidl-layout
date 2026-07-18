from __future__ import annotations

import pytest
from simp_sexp import Sexp

from skidl_layout.constraints import (
    AlignConstraint,
    BoardCutout,
    BoardOutline,
    DistributeConstraint,
    EdgeAnchor,
    FixedPosition,
    KeepOut,
    LayoutConstraints,
    NearConstraint,
)
from skidl_layout.engine import (
    LayoutResult,
    _apply_edge_intent_score,
    _compact_auto_outline_seed,
    _edge_parallel,
    _connector_mating_face_for_ref,
    _effective_keepouts,
    _footprint_names,
    _face_world_points,
    _legalize_edge_anchor_neighbors,
    _legalize_small_parts_from_outline,
    _local_bounds_for_face,
    _placed_bounds,
    _snap_edge_anchors_to_outline,
    _snap_mounting_holes_to_outline_corners,
    plan_layout,
)
from skidl_layout.geometry import (
    FootprintGeometry,
    PadGeometry,
    footprint_geometry_from_sexp,
)
from skidl_layout.grid import points_form_clean_grid
from skidl_layout.intent import MatingIntent, PlacementIntent, PlacementIntentPlan
from skidl_layout.placer import derive_outline, derive_outline_from_circuit
from skidl_layout.routability import RoutabilityFeedback
from skidl_layout.scoring import LayoutScore
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
    def __init__(self, ref, value="", footprint="", name="", nets=None, pins=2):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.name = name
        self.node = None
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
    "Package_QFP:MCU": (12.0, 12.0),
    "Capacitor:C_0805": (2.0, 1.25),
    "Connector:USB": (10.0, 5.0),
    "Connector:USB_C_Receptacle": (10.0, 5.0),
    "MountingHole:M2": (4.4, 4.4),
    "Connector:PinHeader_1x04": (2.54, 10.16),
    "Connector:PinHeader_1x06": (2.54, 15.24),
    "Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal": (7.8, 6.56),
    "Connector_Audio:Thonkiconn_PJ398SM": (8.0, 8.0),
    "Connector_Audio:Jack_3.5mm_PJ320D_Horizontal": (14.0, 10.0),
    "Connector_Audio:Jack_3.5mm_CUI_SJ1-3523N_Horizontal": (13.0, 15.0),
    "Switch:3PDT_Footswitch": (6.0, 6.0),
    (
        "TerminalBlock_Phoenix:"
        "TerminalBlock_Phoenix_MKDS-3-2-5.08_1x02_P5.08mm_Horizontal"
    ): (10.16, 8.0),
}

USB4105_FP = (
    "Connector_USB:"
    "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
)


def _distance(point_a, point_b):
    return (
        (point_a[0] - point_b[0]) ** 2
        + (point_a[1] - point_b[1]) ** 2
    ) ** 0.5


def _circuit():
    vbus = _Net("VBUS")
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=2)
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    j1 = _Part("J1", name="USB connector", footprint="Connector:USB", nets=[vbus, gnd])
    return _Circuit([u1, c1, j1], [vbus, vcc, gnd])


def test_footprint_names_accepts_foot_alias():
    part = _Part("R1", name="resistor")
    part.foot = "Device:R"
    circuit = _Circuit([part], [])

    assert _footprint_names(circuit) == {"Device:R"}


def test_plan_layout_derives_outline_scores_and_power_plan():
    result = plan_layout(
        _circuit(),
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(fixed=[FixedPosition("U1", 30.0, 30.0)]),
        board_layers=4,
    )

    assert isinstance(result, LayoutResult)
    assert result.outline is not None
    assert result.validation.placed_parts == 3
    assert result.validation.missing_refs == []
    assert result.score.power_net_count == 3
    assert result.power_plan.net("GND") is not None
    assert any(
        intent.strategy == "plane" for intent in result.power_plan.route_intents
    )


def test_cutouts_feed_effective_keepout_avoidance():
    constraints = LayoutConstraints(
        cutouts=[
            BoardCutout(
                x_min=10.0,
                y_min=12.0,
                x_max=20.0,
                y_max=22.0,
                name="sensor_window",
            )
        ]
    )

    keepouts = _effective_keepouts(
        constraints,
        placed_parts=[],
        intent_plan=None,
        fp_bboxes={},
        fp_geometries={},
    )

    assert len(keepouts) == 1
    assert keepouts[0].x_min == pytest.approx(10.0)
    assert keepouts[0].x_max == pytest.approx(20.0)


def test_plan_layout_auto_outline_stays_near_placed_envelope():
    result = plan_layout(
        _circuit(),
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(fixed=[FixedPosition("U1", 30.0, 30.0)]),
    )

    assert result.outline is not None
    envelope = derive_outline(result.placed_parts, BBOXES)
    outline_area = result.outline.width_mm * result.outline.height_mm
    envelope_area = envelope.width_mm * envelope.height_mm

    assert outline_area <= envelope_area * 1.35 + 0.001


def test_plan_layout_auto_outline_uses_rotated_placed_geometry():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sig = _Net("SDA")
    u1 = _Part(
        "U1",
        name="sensor IC",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd, sig],
        pins=3,
    )
    j1 = _Part(
        "J1",
        name="pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig],
        pins=6,
    )
    circuit = _Circuit([u1, j1], [vcc, gnd, sig])

    result = plan_layout(circuit, fp_bboxes=BBOXES)

    bounds = [
        _placed_bounds(part, BBOXES, result.fp_geometries)
        for part in result.placed_parts
    ]
    actual_height = max(bound[3] for bound in bounds) - min(
        bound[1] for bound in bounds
    )

    assert result.outline is not None
    assert result.validation.outline_violations == []
    assert result.outline.height_mm <= actual_height + 3.75


def test_plan_layout_routability_feedback_does_not_inflate_auto_outline():
    base = plan_layout(_circuit(), fp_bboxes=BBOXES)
    with_feedback = plan_layout(
        _circuit(),
        fp_bboxes=BBOXES,
        routability=RoutabilityFeedback(
            unrouted_count=2,
            total_nets=4,
            unrouted_nets=["SDA", "SCL"],
            source="unit-test",
        ),
    )

    assert base.outline is not None
    assert with_feedback.outline is not None
    assert with_feedback.outline.width_mm == pytest.approx(base.outline.width_mm)
    assert with_feedback.outline.height_mm == pytest.approx(base.outline.height_mm)
    assert with_feedback.report is not None
    assert any(
        risk.startswith("unrouted net:")
        for risk in with_feedback.report.top_risks()
    )


def test_plan_layout_reads_existing_board_outline(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        """
(kicad_pcb
  (gr_rect
    (start 10 20)
    (end 110 90)
    (layer "Edge.Cuts")
    (stroke (width 0.1))
  )
)
"""
    )

    result = plan_layout(
        _circuit(),
        fp_bboxes=BBOXES,
        existing_pcb_path=str(pcb),
    )

    assert result.outline is not None
    assert result.outline.x_min == 10.0
    assert result.outline.y_min == 20.0
    assert result.outline.x_max == 110.0
    assert result.outline.y_max == 90.0


def test_plan_layout_prefers_explicit_outline_over_existing_board(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        """
(kicad_pcb
  (gr_rect (start 10 20) (end 110 90) (layer "Edge.Cuts"))
)
"""
    )

    explicit = BoardOutline(50.0, 40.0)
    result = plan_layout(
        _circuit(),
        fp_bboxes=BBOXES,
        outline=explicit,
        existing_pcb_path=str(pcb),
    )

    assert result.outline is explicit
    assert result.outline.width_mm == 50.0
    assert result.outline.height_mm == 40.0


def test_plan_layout_returns_candidates_report_and_preserves_edge_anchors():
    outline = BoardOutline(100.0, 60.0)
    result = plan_layout(
        _circuit(),
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=50.0, rot_deg=180.0)],
        ),
    )

    names = [candidate.name for candidate in result.candidates]
    j1 = next(placed for placed in result.placed_parts if placed.ref == "J1")
    _, h = BBOXES[j1.footprint]

    assert names[:5] == [
        "baseline",
        "connector_edge_first",
        "power_first",
        "power_topology_first",
        "cluster_first",
    ]
    assert result.report.selected in names
    assert result.intent_plan is not None
    assert result.report.part_reasons["J1"]
    assert result.report.power_corridors
    assert j1.x_mm == 50.0
    # courtyard bottom edge sits 0.5mm inside the board edge (default inset)
    assert j1.y_mm + h / 2 == pytest.approx(outline.y_max - 0.5)
    assert j1.rot_deg == 180.0


def test_plan_layout_selects_best_finalized_candidate(monkeypatch):
    from types import SimpleNamespace

    from skidl_layout.candidates import PlacementCandidate
    from skidl_layout.scoring import LayoutScore

    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    circuit = _Circuit([u1], [vcc, gnd])
    outline = BoardOutline(120.0, 40.0)

    def fake_candidates(*args, **kwargs):
        return [
            PlacementCandidate(
                name="preliminary_winner",
                placed_parts=[
                    PlacedPart("U1", 80.0, 20.0, 0.0, "Package_QFP:MCU"),
                ],
                constraints=LayoutConstraints(outline=outline),
            ),
            PlacementCandidate(
                name="stable_candidate",
                placed_parts=[
                    PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU"),
                ],
                constraints=LayoutConstraints(outline=outline),
            ),
        ]

    def fake_score(placed_parts, *args, **kwargs):
        x_mm = {placed.ref: placed.x_mm for placed in placed_parts}["U1"]
        if x_mm > 60.0:
            return LayoutScore(score=90.0)
        if x_mm < 5.0:
            return LayoutScore(score=0.0, outline_violation_count=1)
        return LayoutScore(score=24.0)

    def fake_refine(placed_parts, *args, **kwargs):
        u1_x = {placed.ref: placed.x_mm for placed in placed_parts}["U1"]
        if u1_x <= 60.0:
            return SimpleNamespace(
                accepted_count=0,
                placed_parts=placed_parts,
                start_score=24.0,
                final_score=24.0,
                ref_reasons={},
            )
        return SimpleNamespace(
            accepted_count=1,
            placed_parts=[
                PlacedPart("U1", 0.0, 20.0, 0.0, "Package_QFP:MCU"),
            ],
            start_score=90.0,
            final_score=0.0,
            ref_reasons={"U1": ["forced degradation for regression coverage"]},
        )

    monkeypatch.setattr(
        "skidl_layout.engine.generate_placement_candidates",
        fake_candidates,
    )
    monkeypatch.setattr(
        "skidl_layout.engine.refine_candidate_orientations",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "skidl_layout.engine.refine_candidate_decaps",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "skidl_layout.engine.refine_candidate_placement",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("skidl_layout.engine.refine_placement", fake_refine)
    monkeypatch.setattr("skidl_layout.engine.score_placement", fake_score)

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )
    candidate_scores = {
        candidate.name: candidate.score for candidate in result.candidates
    }

    assert result.report.selected == "stable_candidate"
    assert result.score.score == pytest.approx(24.0)
    assert candidate_scores["preliminary_winner"] == pytest.approx(0.0)
    assert candidate_scores["stable_candidate"] == pytest.approx(24.0)


def test_plan_layout_honors_explicit_part_edge_rotation():
    gnd = _Net("GND")
    sig = _Net("SIG")
    jack = _Part(
        "J1",
        name="right edge audio jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[sig, gnd],
        pins=3,
    )
    jack.edge_preference = "right"
    jack.edge_rot_deg = 270
    circuit = _Circuit([jack], [sig, gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=BoardOutline(60.0, 30.0)),
    )

    placed = {part.ref: part for part in result.placed_parts}
    assert placed["J1"].rot_deg == pytest.approx(270.0)
    assert result.intent_plan is not None
    anchor = next(
        anchor for anchor in result.intent_plan.edge_anchors if anchor.ref == "J1"
    )
    assert anchor.edge == "right"
    assert anchor.rot_deg == pytest.approx(270.0)


def test_plan_layout_repairs_edge_header_rotation_that_breaks_pin_access():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    j1 = _Part(
        "J1",
        name="output pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd],
        pins=6,
    )
    j1.edge_preference = "right"
    j1.edge_offset_mm = 20.0
    j1.edge_rot_deg = 90.0
    circuit = _Circuit([j1], [vcc, gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=BoardOutline(50.0, 40.0)),
    )

    placed = {part.ref: part for part in result.placed_parts}
    j1 = placed["J1"]
    width, height = BBOXES[j1.footprint]
    bounds = _placed_bounds(j1, BBOXES, result.fp_geometries)

    assert j1.rot_deg in {0.0, 180.0}
    assert height > width
    assert bounds[2] == pytest.approx(result.outline.x_max - 0.5)
    assert not any(
        "J1: connector row is not parallel" in warning
        for warning in result.score.warnings
    )


def test_explicit_edge_anchor_uses_inferred_rotation_when_unspecified():
    gnd = _Net("GND")
    sig = _Net("SIG")
    jack = _Part(
        "J1",
        name="left edge stereo audio jack",
        footprint="Connector_Audio:Jack_3.5mm_CUI_SJ1-3523N_Horizontal",
        nets=[sig, gnd],
        pins=3,
    )
    jack.edge_preference = "left"
    circuit = _Circuit([jack], [sig, gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=BoardOutline(60.0, 30.0),
            edge_anchors=[EdgeAnchor("J1", "left", offset_mm=15.0)],
        ),
    )

    placed = {part.ref: part for part in result.placed_parts}
    assert placed["J1"].rot_deg == pytest.approx(270.0)


def test_plan_layout_keeps_inferred_pin_header_on_auto_outline_edge():
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sig = _Net("SDA")
    u1 = _Part(
        "U1",
        name="sensor IC",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd, sig],
        pins=3,
    )
    j1 = _Part(
        "J1",
        name="pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig],
        pins=6,
    )
    circuit = _Circuit([u1, j1], [vcc, gnd, sig])

    result = plan_layout(circuit, fp_bboxes=BBOXES)

    placed = {part.ref: part for part in result.placed_parts}
    j1 = placed["J1"]
    width, height = BBOXES[j1.footprint]
    if j1.rot_deg % 180 == 90:
        width, height = height, width
    bounds = (
        j1.x_mm - width / 2,
        j1.y_mm - height / 2,
        j1.x_mm + width / 2,
        j1.y_mm + height / 2,
    )

    assert result.outline is not None
    assert j1.x_mm == pytest.approx(
        (result.outline.x_min + result.outline.x_max) / 2
    )
    assert bounds[3] == pytest.approx(result.outline.y_max - 0.5)
    assert width > height
    assert not any(
        warning.startswith("J1: violates") or "J1: connector row" in warning
        for warning in result.score.warnings
    )


def test_right_angle_pin_header_is_centered_on_edge_and_faces_outward():
    outline = BoardOutline(42.0, 24.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sig = _Net("SDA")
    j1 = _Part(
        "J1",
        name="right-angle pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig],
        pins=6,
    )
    circuit = _Circuit([j1], [vcc, gnd, sig])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    placed = {part.ref: part for part in result.placed_parts}
    anchor = next(
        anchor for anchor in result.intent_plan.edge_anchors if anchor.ref == "J1"
    )
    bounds = _placed_bounds(placed["J1"], BBOXES, result.fp_geometries)

    assert anchor.edge == "bottom"
    assert anchor.offset_mm == pytest.approx((outline.x_min + outline.x_max) / 2)
    assert anchor.rot_deg == pytest.approx(270.0)
    assert placed["J1"].rot_deg == pytest.approx(270.0)
    assert placed["J1"].x_mm == pytest.approx((outline.x_min + outline.x_max) / 2)
    assert bounds[3] == pytest.approx(outline.y_max - anchor.inset_mm)
    assert (bounds[2] - bounds[0]) > (bounds[3] - bounds[1])


def test_pin_header_parallel_repair_prefers_outward_mating_face():
    outline = BoardOutline(42.0, 24.0)
    intent_plan = PlacementIntentPlan(
        edge_anchors=[
            EdgeAnchor("J1", "bottom", offset_mm=21.0, inset_mm=0.0, rot_deg=270.0),
        ],
        mating_intents=[
            MatingIntent(
                "J1",
                "header",
                edge_preference="bottom",
                mating_side="pin_access",
            ),
        ],
    )

    snapped, moved = _snap_edge_anchors_to_outline(
        [PlacedPart("J1", 12.0, 12.0, 90.0, "Connector:PinHeader_1x06")],
        outline,
        intent_plan,
        LayoutConstraints(
            outline=outline,
            edge_anchors=[
                EdgeAnchor(
                    "J1",
                    "bottom",
                    offset_mm=21.0,
                    inset_mm=0.0,
                    rot_deg=90.0,
                ),
            ],
        ),
        BBOXES,
        None,
    )

    placed = {part.ref: part for part in snapped}
    bounds = _placed_bounds(placed["J1"], BBOXES)
    assert moved == ["J1"]
    assert placed["J1"].rot_deg == pytest.approx(270.0)
    assert bounds[3] == pytest.approx(outline.y_max)
    assert (bounds[2] - bounds[0]) > (bounds[3] - bounds[1])


def test_terminal_block_is_parallel_to_edge_and_faces_outward():
    outline = BoardOutline(48.0, 30.0)
    vin = _Net("VIN")
    gnd = _Net("GND")
    j1 = _Part(
        "J1",
        name="2-pin Phoenix terminal block",
        footprint=(
            "TerminalBlock_Phoenix:"
            "TerminalBlock_Phoenix_MKDS-3-2-5.08_1x02_P5.08mm_Horizontal"
        ),
        nets=[vin, gnd],
        pins=2,
    )
    circuit = _Circuit([j1], [vin, gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    placed = {part.ref: part for part in result.placed_parts}
    anchor = next(
        anchor for anchor in result.intent_plan.edge_anchors if anchor.ref == "J1"
    )
    mating = next(
        mating for mating in result.intent_plan.mating_intents if mating.ref == "J1"
    )
    bounds = _placed_bounds(placed["J1"], BBOXES, result.fp_geometries)

    assert mating.kind == "terminal_block"
    assert anchor.edge == "bottom"
    assert anchor.offset_mm == pytest.approx((outline.x_min + outline.x_max) / 2)
    assert anchor.rot_deg == pytest.approx(0.0)
    assert placed["J1"].rot_deg == pytest.approx(0.0)
    assert bounds[3] == pytest.approx(outline.y_max - anchor.inset_mm)
    assert (bounds[2] - bounds[0]) > (bounds[3] - bounds[1])
    assert not any("J1: violates" in warning for warning in result.score.warnings)


def test_plan_layout_clamps_geometry_backed_edge_header_inside_outline(monkeypatch):
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sig = _Net("SDA")
    j1 = _Part(
        "J1",
        name="pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig],
        pins=6,
    )
    circuit = _Circuit([j1], [vcc, gnd, sig])
    outline = BoardOutline(20.0, 20.0)
    geometries = {
        "Connector:PinHeader_1x06": FootprintGeometry(
            footprint="Connector:PinHeader_1x06",
            courtyard_bounds=(0.0, 0.0, 2.54, 15.24),
        ),
    }
    monkeypatch.setattr(
        "skidl_layout.engine._resolve_geometries",
        lambda circuit, fp_lib_dirs: geometries,
    )

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=19.5)],
        ),
    )

    placed = {part.ref: part for part in result.placed_parts}
    bounds = _placed_bounds(placed["J1"], BBOXES, geometries)

    assert bounds[0] >= outline.x_min - 1e-6
    assert bounds[2] <= outline.x_max + 1e-6
    assert bounds[3] == pytest.approx(outline.y_max - 0.5)
    assert placed["J1"].rot_deg in {90.0, 270.0}


def test_footprint_geometry_uses_pad_bounds_without_fab_body():
    outline = BoardOutline(24.0, 30.0)
    footprint = Sexp(
        """
(footprint "Connector:PinHeader_1x02_P2.54mm_Horizontal"
  (pad "1" thru_hole rect (at 0 0) (size 0.2 0.4) (layers "*.Cu" "*.Mask"))
  (pad "2" thru_hole rect (at 2.54 0) (size 0.2 0.4) (layers "*.Cu" "*.Mask"))
  (fp_rect (start -0.8 -1.0) (end 3.34 1.0) (layer "F.CrtYd"))
)
"""
    )
    footprint_name = "Connector:PinHeader_1x02_P2.54mm_Horizontal"
    geometry = footprint_geometry_from_sexp(footprint_name, footprint)

    assert geometry.body_bounds == pytest.approx((-0.1, -0.2, 2.64, 0.2))

    intent_plan = PlacementIntentPlan(
        edge_anchors=[
            EdgeAnchor(
                "J1",
                "bottom",
                offset_mm=12.0,
                inset_mm=0.0,
                rot_deg=270.0,
            ),
        ],
        mating_intents=[
            MatingIntent(
                "J1",
                "header",
                edge_preference="bottom",
                mating_side="pin_access",
            ),
        ],
    )

    snapped, moved = _snap_edge_anchors_to_outline(
        [PlacedPart("J1", 5.0, 5.0, 0.0, footprint_name)],
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        {footprint_name: (3.94, 2.0)},
        {footprint_name: geometry},
    )

    placed = {part.ref: part for part in snapped}
    face = _connector_mating_face_for_ref("J1", footprint_name, intent_plan)

    assert moved == ["J1"]
    assert face is not None
    assert face.local_exit == "+x"

    face_points = _face_world_points(
        placed["J1"],
        face,
        _local_bounds_for_face(3.94, 2.0, geometry),
        use_face_offset=True,
    )
    assert sum(y for _, y in face_points) / len(face_points) == pytest.approx(
        outline.y_max
    )


def test_edge_anchor_snap_places_noncenter_origin_usb_mating_face_on_bottom_edge():
    outline = BoardOutline(40.0, 20.0)
    geometries = {
        USB4105_FP: FootprintGeometry(
            footprint=USB4105_FP,
            body_bounds=(-4.0, -2.0, 6.0, 4.0),
            courtyard_bounds=(-5.0, -3.0, 7.0, 5.0),
        ),
    }
    intent_plan = PlacementIntentPlan(
        edge_anchors=[EdgeAnchor("J_USB", "bottom", offset_mm=20.0, inset_mm=0.0, rot_deg=0.0)],
        mating_intents=[
            MatingIntent("J_USB", "usb", edge_preference="bottom"),
        ],
    )

    snapped, moved = _snap_edge_anchors_to_outline(
        [PlacedPart("J_USB", 8.0, 8.0, 0.0, USB4105_FP)],
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        {USB4105_FP: (12.0, 8.0)},
        geometries,
    )

    placed = {part.ref: part for part in snapped}
    assert moved == ["J_USB"]
    assert placed["J_USB"].x_mm + 1.0 == pytest.approx(20.0)
    assert placed["J_USB"].y_mm + 3.1 == pytest.approx(outline.y_max)


def test_edge_intent_score_uses_mating_face_not_courtyard_edge():
    outline = BoardOutline(40.0, 20.0)
    geometries = {
        USB4105_FP: FootprintGeometry(
            footprint=USB4105_FP,
            body_bounds=(-4.0, -2.0, 6.0, 4.0),
            courtyard_bounds=(-5.0, -3.0, 7.0, 5.0),
        ),
    }
    intent_plan = PlacementIntentPlan(
        edge_anchors=[EdgeAnchor("J_USB", "bottom", offset_mm=20.0, inset_mm=0.0, rot_deg=0.0)],
        mating_intents=[
            MatingIntent("J_USB", "usb", edge_preference="bottom"),
        ],
    )
    constraints = LayoutConstraints(
        outline=outline,
        edge_anchors=[
            EdgeAnchor("J_USB", "bottom", offset_mm=20.0, inset_mm=0.0, rot_deg=0.0),
        ],
    )
    placed = [PlacedPart("J_USB", 20.0, outline.y_max - 3.1, 0.0, USB4105_FP)]

    result = _apply_edge_intent_score(
        LayoutScore(score=80.0),
        placed,
        {USB4105_FP: (12.0, 8.0)},
        outline,
        intent_plan,
        constraints=constraints,
        fp_geometries=geometries,
    )

    assert result.warnings == []


def test_fixed_floorplan_position_suppresses_inferred_connector_edge_warning():
    outline = BoardOutline(40.0, 20.0)
    intent_plan = PlacementIntentPlan(
        edge_anchors=[EdgeAnchor("J_USB", "bottom", offset_mm=20.0, inset_mm=0.0, rot_deg=0.0)],
        mating_intents=[
            MatingIntent("J_USB", "usb", edge_preference="bottom"),
        ],
    )
    constraints = LayoutConstraints(
        outline=outline,
        fixed=[FixedPosition("J_USB", 20.0, 12.0, 0.0)],
    )
    score = LayoutScore(
        score=40.0,
        warning_count=1,
        warnings=["J_USB: connector is 7.0mm from nearest board edge"],
    )

    result = _apply_edge_intent_score(
        score,
        [PlacedPart("J_USB", 20.0, 12.0, 0.0, USB4105_FP)],
        {USB4105_FP: (12.0, 8.0)},
        outline,
        intent_plan,
        constraints=constraints,
        fp_geometries={},
    )

    assert result.warnings == []
    assert result.score == pytest.approx(45.0)


def test_plan_layout_stamps_eurorack_front_back_sides_on_placements():
    plus12 = _Net("+12V")
    minus12 = _Net("-12V")
    gnd = _Net("GND")
    sig = _Net("OUT")
    power = _Part(
        "J10",
        name="Eurorack power header",
        footprint="Connector:PinHeader_1x06",
        nets=[plus12, minus12, gnd],
        pins=6,
    )
    jack = _Part(
        "J1",
        name="3.5mm mono output jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[sig, gnd],
        pins=3,
    )
    circuit = _Circuit([power, jack], [plus12, minus12, gnd, sig])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=BoardOutline(40.0, 120.0)),
        assembly_policy="double_sided",
    )

    placed = {part.ref: part for part in result.placed_parts}
    layout = result.to_dict()
    sides = {part["ref"]: part["side"] for part in layout["placed_parts"]}

    assert placed["J1"].side == "front"
    assert placed["J10"].side == "back"
    assert sides["J1"] == "front"
    assert sides["J10"] == "back"
    assert any("Thonkiconn/PJ398" in warning for warning in result.report.warnings)
    assert any("Thonkiconn/PJ398" in risk for risk in result.report.top_risks())
    assert all(anchor.ref != "J10" for anchor in result.intent_plan.edge_anchors)
    j10_bounds = _placed_bounds(placed["J10"], BBOXES, result.fp_geometries)
    assert j10_bounds[0] >= result.outline.x_min
    assert j10_bounds[1] >= result.outline.y_min
    assert j10_bounds[2] <= result.outline.x_max
    assert j10_bounds[3] <= result.outline.y_max
    assert layout["intent_plan"]["assembly_sides"]["J10"] == "back"


def test_plan_layout_places_two_generic_headers_on_opposing_edges():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig1 = _Net("SIG1")
    sig2 = _Net("SIG2")
    j1 = _Part(
        "J1",
        name="pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig1, sig2],
        pins=6,
    )
    j2 = _Part(
        "J2",
        name="pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig1, sig2],
        pins=6,
    )
    r1 = _Part("R1", value="10K", footprint="Capacitor:C_0805", nets=[sig1, sig2])
    holes = [
        _Part(f"H{idx}", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
        for idx in range(1, 5)
    ]
    circuit = _Circuit([j1, j2, r1, *holes], [vcc, gnd, sig1, sig2])

    result = plan_layout(circuit, fp_bboxes=BBOXES)

    anchors = {
        anchor.ref: anchor.edge
        for anchor in result.intent_plan.edge_anchors
    }
    placed = {part.ref: part for part in result.placed_parts}
    width, height = BBOXES["Connector:PinHeader_1x06"]

    assert anchors["J1"] == "left"
    assert anchors["J2"] == "right"
    assert placed["J1"].x_mm - width / 2 == pytest.approx(result.outline.x_min + 0.5)
    assert placed["J2"].x_mm + width / 2 == pytest.approx(result.outline.x_max - 0.5)
    assert placed["J1"].rot_deg == 0.0
    assert placed["J2"].rot_deg == 0.0
    assert height > width
    assert result.validation.overlaps == []

    x_mid = (result.outline.x_min + result.outline.x_max) / 2
    y_mid = (result.outline.y_min + result.outline.y_max) / 2
    hole_quadrants = {
        (placed[ref].x_mm < x_mid, placed[ref].y_mm < y_mid)
        for ref in ("H1", "H2", "H3", "H4")
    }
    assert hole_quadrants == {
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    }
    for ref in ("H1", "H2", "H3", "H4"):
        x_edge_distance = min(
            placed[ref].x_mm - result.outline.x_min,
            result.outline.x_max - placed[ref].x_mm,
        )
        y_edge_distance = min(
            placed[ref].y_mm - result.outline.y_min,
            result.outline.y_max - placed[ref].y_mm,
        )
        assert x_edge_distance <= result.outline.width_mm * 0.25
        assert y_edge_distance <= result.outline.height_mm * 0.25


def test_plan_layout_places_usb_c_inline_pair_on_opposing_edges():
    vbus = _Net("VBUS")
    gnd = _Net("GND")
    dp = _Net("USB_D+")
    dm = _Net("USB_D-")
    j1 = _Part(
        "J1",
        name="USB-C IN receptacle",
        footprint="Connector:USB_C_Receptacle",
        nets=[vbus, gnd, dp, dm],
        pins=16,
    )
    j2 = _Part(
        "J2",
        name="USB-C OUT receptacle",
        footprint="Connector:USB_C_Receptacle",
        nets=[vbus, gnd, dp, dm],
        pins=16,
    )
    circuit = _Circuit([j1, j2], [vbus, gnd, dp, dm])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=BoardOutline(60.0, 24.0)),
    )

    anchors = {anchor.ref: anchor for anchor in result.intent_plan.edge_anchors}
    placed = {part.ref: part for part in result.placed_parts}

    assert anchors["J1"].edge == "left"
    assert anchors["J2"].edge == "right"
    assert anchors["J1"].rot_deg == pytest.approx(270.0)
    assert anchors["J2"].rot_deg == pytest.approx(90.0)
    assert placed["J1"].x_mm < placed["J2"].x_mm
    assert result.validation.overlaps == []


def test_plan_layout_grids_passives_between_opposing_headers():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig1 = _Net("SIG1")
    sig2 = _Net("SIG2")
    sig3 = _Net("SIG3")
    sig4 = _Net("SIG4")
    j1 = _Part(
        "J1",
        name="pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig1, sig2, sig3, sig4],
        pins=6,
    )
    j2 = _Part(
        "J2",
        name="pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sig1, sig2, sig3, sig4],
        pins=6,
    )
    passives = [
        _Part(f"R{idx}", value="10K", footprint="Capacitor:C_0805", nets=[sig1, sig2])
        for idx in range(1, 5)
    ] + [
        _Part(f"C{idx}", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
        for idx in range(1, 3)
    ]
    circuit = _Circuit([j1, j2, *passives], [vcc, gnd, sig1, sig2, sig3, sig4])

    result = plan_layout(circuit, fp_bboxes=BBOXES)

    placed = {part.ref: part for part in result.placed_parts}
    passive_refs = {part.ref for part in passives}
    passive_xs = sorted(round(placed[ref].x_mm, 1) for ref in passive_refs)
    passive_ys = sorted(round(placed[ref].y_mm, 1) for ref in passive_refs)
    unique_xs = sorted(set(passive_xs))
    unique_ys = sorted(set(passive_ys))

    assert result.validation.overlaps == []
    assert len(unique_xs) >= 2
    assert len(unique_ys) >= 3
    assert min(unique_xs) > placed["J1"].x_mm
    assert max(unique_xs) < placed["J2"].x_mm
    assert abs(placed["C1"].y_mm - placed["C2"].y_mm) <= 3.0
    for left_ref, right_ref in (("R1", "R2"), ("R3", "R4")):
        assert abs(placed[left_ref].x_mm - placed[right_ref].x_mm) <= 3.5
        assert abs(placed[left_ref].y_mm - placed[right_ref].y_mm) <= 3.0
    assert any(
        "passives arranged on an even grid" in reason
        for reason in result.report.reasons
    )


def test_plan_layout_infers_mounting_holes_to_corners():
    outline = BoardOutline(60.0, 40.0)
    circuit = _circuit()
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    h2 = _Part("H2", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    circuit.parts.extend([h1, h2])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    placed = {part.ref: part for part in result.placed_parts}
    assert placed["H1"].x_mm == pytest.approx(2.7)
    assert placed["H1"].y_mm == pytest.approx(2.7)
    assert placed["H2"].x_mm == pytest.approx(57.3)
    assert placed["H2"].y_mm == pytest.approx(2.7)
    assert "locked by fixed-position constraint" in result.report.part_reasons["H1"]


def test_rounded_outline_places_mounting_holes_at_corner_radius_centers():
    outline = BoardOutline(60.0, 40.0, corner_radius_mm=2.7)
    circuit = _circuit()
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    h2 = _Part("H2", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    h3 = _Part("H3", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    h4 = _Part("H4", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    circuit.parts.extend([h1, h2, h3, h4])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    placed = {part.ref: part for part in result.placed_parts}
    corners = {
        (round(placed[ref].x_mm, 3), round(placed[ref].y_mm, 3))
        for ref in ("H1", "H2", "H3", "H4")
    }

    assert corners == {
        (2.7, 2.7),
        (57.3, 2.7),
        (57.3, 37.3),
        (2.7, 37.3),
    }


def test_snap_four_mounting_holes_reconciles_stale_floorplan_to_final_outline():
    outline = BoardOutline(66.3, 32.3, corner_radius_mm=2.7)
    intent_plan = PlacementIntentPlan()
    for ref in ("H1", "H2", "H3", "H4"):
        intent_plan.intents[ref] = [
            PlacementIntent(ref, "mounting_hole", 90, ["test mounting hole"])
        ]
    placed_parts = [
        PlacedPart("H1", 2.7, 2.7, 0.0, "MountingHole:M2"),
        PlacedPart("H2", 47.3, 2.7, 0.0, "MountingHole:M2"),
        PlacedPart("H3", 2.7, 29.3, 0.0, "MountingHole:M2"),
        PlacedPart("H4", 47.3, 29.3, 0.0, "MountingHole:M2"),
    ]

    snapped, moved = _snap_mounting_holes_to_outline_corners(
        placed_parts,
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
    )

    placed = {part.ref: part for part in snapped}
    assert set(moved) == {"H2", "H3", "H4"}
    assert placed["H1"].x_mm == pytest.approx(2.7)
    assert placed["H1"].y_mm == pytest.approx(2.7)
    assert placed["H2"].x_mm == pytest.approx(63.6)
    assert placed["H2"].y_mm == pytest.approx(2.7)
    assert placed["H3"].x_mm == pytest.approx(2.7)
    assert placed["H3"].y_mm == pytest.approx(29.6)
    assert placed["H4"].x_mm == pytest.approx(63.6)
    assert placed["H4"].y_mm == pytest.approx(29.6)


def test_snap_two_mounting_holes_reconciles_stale_floorplan_to_final_outline():
    outline = BoardOutline(60.0, 40.0)
    intent_plan = PlacementIntentPlan()
    for ref in ("H1", "H2"):
        intent_plan.intents[ref] = [
            PlacementIntent(ref, "mounting_hole", 90, ["test mounting hole"])
        ]
    placed_parts = [
        PlacedPart("H1", 3.2, 3.2, 0.0, "MountingHole:M2"),
        PlacedPart("H2", 56.8, 3.2, 0.0, "MountingHole:M2"),
    ]

    snapped, moved = _snap_mounting_holes_to_outline_corners(
        placed_parts,
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
    )

    placed = {part.ref: part for part in snapped}
    assert set(moved) == {"H1", "H2"}
    assert placed["H1"].x_mm == pytest.approx(2.7)
    assert placed["H1"].y_mm == pytest.approx(2.7)
    assert placed["H2"].x_mm == pytest.approx(57.3)
    assert placed["H2"].y_mm == pytest.approx(2.7)


def test_snap_mounting_holes_preserves_two_hole_panel_pattern():
    outline = BoardOutline(28.5, 103.2, corner_radius_mm=2.7)
    intent_plan = PlacementIntentPlan()
    for ref in ("H1", "H2"):
        intent_plan.intents[ref] = [
            PlacementIntent(ref, "mounting_hole", 90, ["test mounting hole"])
        ]
    placed_parts = [
        PlacedPart("H1", 17.5, 8.0, 0.0, "MountingHole:M3"),
        PlacedPart("H2", 17.5, 102.0, 0.0, "MountingHole:M3"),
    ]

    snapped, moved = _snap_mounting_holes_to_outline_corners(
        placed_parts,
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
    )

    assert moved == []
    assert snapped == placed_parts


def test_plan_layout_centers_single_qwiic_between_two_mounting_holes():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sda = _Net("SDA")
    scl = _Net("SCL")
    u1 = _Part(
        "U1",
        name="MCP9808 temperature sensor",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd, sda, scl],
        pins=8,
    )
    j100 = _Part(
        "J100",
        name="Qwiic STEMMA QT JST SH connector",
        footprint="Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
        nets=[gnd, vcc, sda, scl],
        pins=4,
    )
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    h2 = _Part("H2", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    circuit = _Circuit([u1, j100, h1, h2], [vcc, gnd, sda, scl])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    placed = {part.ref: part for part in result.placed_parts}
    anchor = next(anchor for anchor in result.intent_plan.edge_anchors if anchor.ref == "J100")

    assert anchor.edge == "top"
    assert anchor.offset_mm == pytest.approx(outline.width_mm / 2)
    assert anchor.rot_deg == 180.0
    assert placed["J100"].x_mm == pytest.approx(outline.width_mm / 2)
    assert placed["J100"].y_mm - BBOXES[j100.footprint][1] / 2 == pytest.approx(outline.y_min)
    assert placed["H1"].y_mm == pytest.approx(placed["H2"].y_mm)
    assert placed["H1"].x_mm < placed["J100"].x_mm < placed["H2"].x_mm
    assert "connector_between_mounting_holes" in {
        intent.kind for intent in result.intent_plan.intents_for("J100")
    }


def test_plan_layout_splits_qwiic_and_header_on_two_hole_breakout():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sda = _Net("SDA")
    scl = _Net("SCL")
    alert = _Net("ALERT")
    u1 = _Part(
        "U1",
        name="MCP9808 temperature sensor",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd, sda, scl, alert],
        pins=8,
    )
    j100 = _Part(
        "J100",
        name="Qwiic STEMMA QT JST SH connector",
        footprint="Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
        nets=[gnd, vcc, sda, scl],
        pins=4,
    )
    j1 = _Part(
        "J1",
        name="0.1 inch pin header",
        footprint="Connector:PinHeader_1x06",
        nets=[vcc, gnd, sda, scl, alert],
        pins=6,
    )
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    h2 = _Part("H2", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    circuit = _Circuit([u1, j100, j1, h1, h2], [vcc, gnd, sda, scl, alert])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    anchors = {anchor.ref: anchor for anchor in result.intent_plan.edge_anchors}
    placed = {part.ref: part for part in result.placed_parts}

    assert anchors["J1"].edge == "top"
    assert anchors["J1"].offset_mm == pytest.approx(outline.width_mm / 2)
    assert anchors["J100"].edge == "bottom"
    assert anchors["J100"].offset_mm == pytest.approx(outline.width_mm / 2)
    assert result.validation.overlaps == []
    assert placed["H1"].x_mm < placed["J1"].x_mm < placed["H2"].x_mm
    assert placed["J100"].x_mm == pytest.approx(outline.width_mm / 2)
    assert "connector_between_mounting_holes" in {
        intent.kind for intent in result.intent_plan.intents_for("J1")
    }
    assert "connector_opposite_mounting_hole_header" in {
        intent.kind for intent in result.intent_plan.intents_for("J100")
    }


def test_panel_control_physical_body_crossing_outline_is_score_invalid(monkeypatch):
    gnd = _Net("GND")
    sig = _Net("BYPASS")
    sw1 = _Part(
        "SW1",
        name="guitar pedal footswitch",
        footprint="Switch:3PDT_Footswitch",
        nets=[sig, gnd],
        pins=6,
    )
    circuit = _Circuit([sw1], [sig, gnd])
    outline = BoardOutline(20.0, 20.0)
    geometries = {
        "Switch:3PDT_Footswitch": FootprintGeometry(
            footprint="Switch:3PDT_Footswitch",
            courtyard_bounds=(-3.0, -3.0, 3.0, 3.0),
            body_bounds=(-8.0, -8.0, 8.0, 8.0),
        ),
    }
    monkeypatch.setattr(
        "skidl_layout.engine._resolve_geometries",
        lambda circuit, fp_lib_dirs: geometries,
    )

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            fixed=[FixedPosition("SW1", 5.0, 10.0)],
        ),
    )

    assert result.score.outline_violation_count >= 1
    assert not result.ok
    assert any(
        "SW1: panel/mechanical body crosses board outline" == warning
        for warning in result.score.warnings
    )


def test_legalize_small_parts_nudges_passives_clear_of_outline():
    outline = BoardOutline(30.0, 20.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    r1 = _Part("R1", value="10K", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    circuit = _Circuit([r1, u1], [vcc, gnd])
    placed_parts = [
        PlacedPart("R1", x_mm=0.7, y_mm=10.0, rot_deg=0.0, footprint="Capacitor:C_0805"),
        PlacedPart("U1", x_mm=16.0, y_mm=10.0, rot_deg=0.0, footprint="Package_QFP:MCU"),
    ]

    legalized, moved = _legalize_small_parts_from_outline(
        placed_parts,
        circuit,
        outline,
        None,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
        clearance_mm=0.5,
    )

    placed = {part.ref: part for part in legalized}
    bounds = _placed_bounds(placed["R1"], BBOXES)

    assert moved == ["R1"]
    assert bounds[0] >= outline.x_min + 1.5
    assert placed["U1"].x_mm == pytest.approx(16.0)


def test_legalize_small_parts_nudges_passives_clear_of_mounting_holes():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    r1 = _Part("R1", value="10K", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    circuit = _Circuit([r1, h1, u1], [vcc, gnd])
    intent_plan = PlacementIntentPlan()
    intent_plan.intents["H1"] = [
        PlacementIntent("H1", "mounting_hole", 90, ["test mounting hole"])
    ]
    placed_parts = [
        PlacedPart("H1", x_mm=4.0, y_mm=4.0, rot_deg=0.0, footprint="MountingHole:M2"),
        PlacedPart("R1", x_mm=7.2, y_mm=4.0, rot_deg=0.0, footprint="Capacitor:C_0805"),
        PlacedPart("U1", x_mm=22.0, y_mm=14.0, rot_deg=0.0, footprint="Package_QFP:MCU"),
    ]

    legalized, moved = _legalize_small_parts_from_outline(
        placed_parts,
        circuit,
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
        clearance_mm=0.5,
    )

    placed = {part.ref: part for part in legalized}
    hole_bounds = _placed_bounds(placed["H1"], BBOXES)
    passive_bounds = _placed_bounds(placed["R1"], BBOXES)
    halo = (
        hole_bounds[0] - 2.0,
        hole_bounds[1] - 2.0,
        hole_bounds[2] + 2.0,
        hole_bounds[3] + 2.0,
    )

    assert moved == ["R1"]
    assert not (
        passive_bounds[0] < halo[2]
        and passive_bounds[2] > halo[0]
        and passive_bounds[1] < halo[3]
        and passive_bounds[3] > halo[1]
    )
    assert placed["H1"].x_mm == pytest.approx(4.0)


def test_plan_layout_nudges_fixed_passive_clear_of_mounting_hole_clearance():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    r1 = _Part("R1", value="10K", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    circuit = _Circuit([r1, h1, u1], [vcc, gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            fixed=[
                FixedPosition("H1", 4.0, 4.0),
                FixedPosition("R1", 7.2, 4.0),
                FixedPosition("U1", 22.0, 14.0),
            ],
        ),
    )

    placed = {part.ref: part for part in result.placed_parts}
    hole_bounds = _placed_bounds(placed["H1"], BBOXES, result.fp_geometries)
    passive_bounds = _placed_bounds(placed["R1"], BBOXES, result.fp_geometries)
    halo = (
        hole_bounds[0] - 2.0,
        hole_bounds[1] - 2.0,
        hole_bounds[2] + 2.0,
        hole_bounds[3] + 2.0,
    )

    assert "R1" not in result.validation.keepout_violations
    assert "H1" not in result.validation.keepout_violations
    assert result.score.keepout_violation_count == 0
    assert not (
        passive_bounds[0] < halo[2]
        and passive_bounds[2] > halo[0]
        and passive_bounds[1] < halo[3]
        and passive_bounds[3] > halo[1]
    )


def test_plan_layout_reports_fixed_large_part_inside_mounting_hole_clearance():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    circuit = _Circuit([h1, u1], [vcc, gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            fixed=[
                FixedPosition("H1", 4.0, 4.0),
                FixedPosition("U1", 7.2, 4.0),
            ],
        ),
    )

    assert "U1" in result.validation.keepout_violations
    assert "H1" not in result.validation.keepout_violations
    assert result.score.keepout_violation_count >= 1


def test_outline_edge_keepout_band_allows_mounting_holes_only():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    r1 = _Part("R1", value="10K", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    circuit = _Circuit([r1, h1, u1], [vcc, gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            fixed=[
                FixedPosition("H1", 4.0, 3.0),
                FixedPosition("R1", 10.0, 3.0),
                FixedPosition("U1", 24.0, 16.0),
            ],
            keepouts=[
                KeepOut(outline.x_min, outline.y_min, outline.x_max, outline.y_min + 6.0),
            ],
        ),
    )

    assert "H1" not in result.validation.keepout_violations
    assert "R1" in result.validation.keepout_violations


def test_plan_layout_applies_corner_radius_hint_to_existing_outline():
    outline = BoardOutline(40.0, 28.0)
    circuit = _circuit()

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
        corner_radius_mm=2.7,
    )

    assert result.outline is not None
    assert result.outline.corner_radius_mm == pytest.approx(2.7)


def test_legalize_small_parts_nudges_connectors_clear_of_mounting_holes():
    outline = BoardOutline(40.0, 28.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sda = _Net("SDA")
    scl = _Net("SCL")
    j100 = _Part(
        "J100",
        name="Qwiic STEMMA QT JST SH connector",
        footprint="Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
        nets=[gnd, vcc, sda, scl],
        pins=4,
    )
    h1 = _Part("H1", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=8)
    circuit = _Circuit([j100, h1, u1], [vcc, gnd, sda, scl])
    intent_plan = PlacementIntentPlan()
    intent_plan.intents["H1"] = [
        PlacementIntent("H1", "mounting_hole", 90, ["test mounting hole"])
    ]
    placed_parts = [
        PlacedPart("H1", x_mm=4.0, y_mm=4.0, rot_deg=0.0, footprint="MountingHole:M2"),
        PlacedPart(
            "J100",
            x_mm=8.5,
            y_mm=4.0,
            rot_deg=0.0,
            footprint="Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal",
        ),
        PlacedPart("U1", x_mm=24.0, y_mm=14.0, rot_deg=0.0, footprint="Package_QFP:MCU"),
    ]

    legalized, moved = _legalize_small_parts_from_outline(
        placed_parts,
        circuit,
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
        clearance_mm=0.5,
    )

    placed = {part.ref: part for part in legalized}
    hole_bounds = _placed_bounds(placed["H1"], BBOXES)
    connector_bounds = _placed_bounds(placed["J100"], BBOXES)
    halo = (
        hole_bounds[0] - 2.0,
        hole_bounds[1] - 2.0,
        hole_bounds[2] + 2.0,
        hole_bounds[3] + 2.0,
    )

    assert moved == ["J100"]
    assert not (
        connector_bounds[0] < halo[2]
        and connector_bounds[2] > halo[0]
        and connector_bounds[1] < halo[3]
        and connector_bounds[3] > halo[1]
    )
    assert placed["H1"].x_mm == pytest.approx(4.0)


def test_edge_neighbor_legalizer_allows_opposite_side_tht_body_overlap():
    outline = BoardOutline(40.0, 30.0)
    constraints = LayoutConstraints(
        outline=outline,
        edge_anchors=[EdgeAnchor("J1", "left", offset_mm=15.0)],
    )
    intent_plan = PlacementIntentPlan(
        edge_anchors=[EdgeAnchor("J1", "left", offset_mm=15.0)],
    )
    placed_parts = [
        PlacedPart("J1", 10.0, 15.0, 0.0, "Demo:FrontTHT", side="front"),
        PlacedPart("U1", 10.0, 15.0, 0.0, "Demo:BackTHT", side="back"),
    ]
    bboxes = {
        "Demo:FrontTHT": (10.0, 10.0),
        "Demo:BackTHT": (10.0, 10.0),
    }
    geometries = {
        "Demo:FrontTHT": FootprintGeometry(
            "Demo:FrontTHT",
            pads=[
                PadGeometry(
                    "1",
                    -3.0,
                    0.0,
                    1.0,
                    1.0,
                    pad_type="thru_hole",
                    layers=("*.Cu", "*.Mask"),
                )
            ],
            courtyard_bounds=(-5.0, -5.0, 5.0, 5.0),
        ),
        "Demo:BackTHT": FootprintGeometry(
            "Demo:BackTHT",
            pads=[
                PadGeometry(
                    "1",
                    3.0,
                    0.0,
                    1.0,
                    1.0,
                    pad_type="thru_hole",
                    layers=("*.Cu", "*.Mask"),
                )
            ],
            courtyard_bounds=(-5.0, -5.0, 5.0, 5.0),
        ),
    }

    legalized, moved = _legalize_edge_anchor_neighbors(
        placed_parts,
        outline,
        intent_plan,
        constraints,
        bboxes,
        geometries,
        clearance_mm=0.5,
    )

    placed = {part.ref: part for part in legalized}
    assert moved == []
    assert placed["U1"].x_mm == pytest.approx(10.0)
    assert placed["U1"].side == "back"


def test_edge_anchor_snap_avoids_mounting_hole_halos():
    outline = BoardOutline(40.0, 28.0)
    intent_plan = PlacementIntentPlan(
        edge_anchors=[
            EdgeAnchor("J1", "right", offset_mm=24.0, rot_deg=90.0),
        ]
    )
    intent_plan.intents["H1"] = [
        PlacementIntent("H1", "mounting_hole", 90, ["test mounting hole"])
    ]
    placed_parts = [
        PlacedPart("H1", x_mm=36.0, y_mm=24.0, rot_deg=0.0, footprint="MountingHole:M2"),
        PlacedPart(
            "J1",
            x_mm=36.0,
            y_mm=24.0,
            rot_deg=90.0,
            footprint="Connector:PinHeader_1x06",
        ),
    ]

    snapped, moved = _snap_edge_anchors_to_outline(
        placed_parts,
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
    )

    placed = {part.ref: part for part in snapped}
    hole_bounds = _placed_bounds(placed["H1"], BBOXES)
    header_bounds = _placed_bounds(placed["J1"], BBOXES)
    halo = (
        hole_bounds[0] - 2.0,
        hole_bounds[1] - 2.0,
        hole_bounds[2] + 2.0,
        hole_bounds[3] + 2.0,
    )

    assert moved == ["J1"]
    assert header_bounds[2] == pytest.approx(outline.x_max - 0.5)
    assert not (
        header_bounds[0] < halo[2]
        and header_bounds[2] > halo[0]
        and header_bounds[1] < halo[3]
        and header_bounds[3] > halo[1]
    )


def test_edge_anchor_snap_clamps_candidate_inside_outline(monkeypatch):
    outline = BoardOutline(32.0, 24.0)
    intent_plan = PlacementIntentPlan(
        edge_anchors=[
            EdgeAnchor("J1", "bottom", offset_mm=5.0, rot_deg=0.0),
        ],
    )

    def off_board_candidate(*_args, **_kwargs):
        return (-6.0, 20.0, 0.0, -6.0, 20.0, 15.0, 2.0)

    monkeypatch.setattr(
        "skidl_layout.engine._edge_anchor_position_avoiding_keepouts",
        off_board_candidate,
    )

    snapped, moved = _snap_edge_anchors_to_outline(
        [PlacedPart("J1", 12.0, 12.0, 0.0, "Connector:PinHeader_1x06")],
        outline,
        intent_plan,
        LayoutConstraints(
            outline=outline,
            edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=5.0, rot_deg=0.0)],
        ),
        BBOXES,
        None,
    )

    placed = {part.ref: part for part in snapped}
    bounds = _placed_bounds(placed["J1"], BBOXES, None)

    assert "J1" in moved
    assert bounds[0] >= outline.x_min - 1e-6
    assert bounds[2] <= outline.x_max + 1e-6


def test_edge_anchor_snap_separates_same_edge_connectors_after_keepout_avoidance():
    outline = BoardOutline(56.0, 26.0)
    intent_plan = PlacementIntentPlan(
        edge_anchors=[
            EdgeAnchor("J1", "bottom", offset_mm=6.6, rot_deg=0.0),
            EdgeAnchor("J2", "bottom", offset_mm=48.4, rot_deg=0.0),
        ]
    )
    intent_plan.intents["H1"] = [
        PlacementIntent("H1", "mounting_hole", 90, ["left mounting hole"])
    ]
    intent_plan.intents["H2"] = [
        PlacementIntent("H2", "mounting_hole", 90, ["right mounting hole"])
    ]
    placed_parts = [
        PlacedPart("H1", x_mm=4.0, y_mm=22.0, rot_deg=0.0, footprint="MountingHole:M2"),
        PlacedPart("H2", x_mm=52.0, y_mm=22.0, rot_deg=0.0, footprint="MountingHole:M2"),
        PlacedPart("J1", x_mm=6.6, y_mm=20.0, rot_deg=0.0, footprint="Connector:USB"),
        PlacedPart("J2", x_mm=48.4, y_mm=20.0, rot_deg=0.0, footprint="Connector:USB"),
    ]

    snapped, moved = _snap_edge_anchors_to_outline(
        placed_parts,
        outline,
        intent_plan,
        LayoutConstraints(outline=outline),
        BBOXES,
        None,
    )

    placed = {part.ref: part for part in snapped}
    j1_bounds = _placed_bounds(placed["J1"], BBOXES)
    j2_bounds = _placed_bounds(placed["J2"], BBOXES)

    assert {"J1", "J2"}.issubset(set(moved))
    assert j1_bounds[3] == pytest.approx(outline.y_max - 0.5)
    assert j2_bounds[3] == pytest.approx(outline.y_max - 0.5)
    assert j1_bounds[2] + 0.75 <= j2_bounds[0]
    assert placed["J1"].x_mm < placed["J2"].x_mm


def test_edge_anchor_snap_preserves_explicit_left_right_offsets():
    outline = BoardOutline(90.0, 70.0)
    intent_plan = PlacementIntentPlan(
        edge_anchors=[
            EdgeAnchor("BAT1", "left", offset_mm=52.0),
            EdgeAnchor("J1", "right", offset_mm=18.0),
        ]
    )
    constraints = LayoutConstraints(
        outline=outline,
        edge_anchors=[
            EdgeAnchor("BAT1", "left", offset_mm=52.0),
            EdgeAnchor("J1", "right", offset_mm=18.0),
        ],
    )
    placed_parts = [
        PlacedPart("BAT1", 10.0, 35.0, 0.0, "Connector:USB"),
        PlacedPart("J1", 80.0, 35.0, 0.0, "Connector:USB"),
    ]

    snapped, moved = _snap_edge_anchors_to_outline(
        placed_parts,
        outline,
        intent_plan,
        constraints,
        BBOXES,
        None,
    )
    placed = {part.ref: part for part in snapped}

    assert set(moved) == {"BAT1", "J1"}
    assert placed["BAT1"].y_mm == pytest.approx(52.0)
    assert placed["J1"].y_mm == pytest.approx(18.0)


def test_plan_layout_does_not_edge_anchor_oled_daughterboard_header():
    outline = BoardOutline(60.0, 40.0)
    vcc = _Net("3V3")
    gnd = _Net("GND")
    sda = _Net("OLED_SDA")
    scl = _Net("OLED_SCL")
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd, sda, scl], pins=4)
    j1 = _Part(
        "J1",
        name="OLED daughterboard header",
        footprint="Connector:PinHeader_1x04",
        nets=[vcc, gnd, sda, scl],
        pins=4,
    )
    circuit = _Circuit([u1, j1], [vcc, gnd, sda, scl])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    assert all(anchor.ref != "J1" for anchor in result.intent_plan.edge_anchors)
    assert any(
        intent.kind == "internal_connector"
        for intent in result.intent_plan.intents_for("J1")
    )


def test_plan_layout_aligns_panel_jacks_without_edge_anchoring():
    outline = BoardOutline(80.0, 40.0)
    gnd = _Net("GND")
    sig1 = _Net("IN_1")
    sig2 = _Net("IN_2")
    sig3 = _Net("OUT_1")
    j1 = _Part(
        "J1",
        name="Thonkiconn PJ398SM input jack",
        footprint="Connector_Audio:Thonkiconn_PJ398SM",
        nets=[sig1, gnd],
    )
    j2 = _Part(
        "J2",
        name="Thonkiconn PJ398SM input jack",
        footprint="Connector_Audio:Thonkiconn_PJ398SM",
        nets=[sig2, gnd],
    )
    j3 = _Part(
        "J3",
        name="Thonkiconn PJ398SM output jack",
        footprint="Connector_Audio:Thonkiconn_PJ398SM",
        nets=[sig3, gnd],
    )
    circuit = _Circuit([j1, j2, j3], [gnd, sig1, sig2, sig3])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    assert all(anchor.ref not in {"J1", "J2", "J3"} for anchor in result.intent_plan.edge_anchors)
    placed = {part.ref: part for part in result.placed_parts}
    ys = [placed[ref].y_mm for ref in ("J1", "J2", "J3")]
    xs = [placed[ref].x_mm for ref in ("J1", "J2", "J3")]
    assert max(ys) - min(ys) <= 1.0
    assert max(xs) - min(xs) >= 30.0


def test_plan_layout_keeps_tall_panel_jacks_on_inferred_vertical_grid():
    outline = BoardOutline(24.0, 128.5)
    gnd = _Net("GND")
    signals = [_Net(f"JACK_{idx}") for idx in range(1, 5)]
    jacks = [
        _Part(
            f"J{idx}",
            name="Thonkiconn PJ398SM panel jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[signal, gnd],
        )
        for idx, signal in enumerate(signals, start=1)
    ]
    circuit = _Circuit(jacks, [gnd, *signals])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    assert all(
        anchor.ref not in {jack.ref for jack in jacks}
        for anchor in result.intent_plan.edge_anchors
    )
    assert any(
        constraint.axis == "y"
        and set(constraint.refs) == {jack.ref for jack in jacks}
        for constraint in result.intent_plan.distribute_constraints
    )
    placed = {part.ref: part for part in result.placed_parts}
    xs = [placed[jack.ref].x_mm for jack in jacks]
    ys = [placed[jack.ref].y_mm for jack in jacks]
    assert max(xs) - min(xs) <= 1.0
    assert max(ys) - min(ys) >= outline.height_mm * 0.60
    assert result.validation.overlaps == []


def test_plan_layout_grids_repeated_sensors_without_ejecting_local_caps():
    outline = BoardOutline(100.0, 50.0)
    vcc = _Net("VCC")
    gnd = _Net("GND")
    ch0 = _Net("CH0_SIG")
    ch1 = _Net("CH1_SIG")
    u2 = _Part(
        "U2",
        name="MCP9808 temperature sensor",
        footprint="Package_QFP:MCU",
        nets=[ch0, vcc, gnd],
        pins=8,
    )
    c2 = _Part("C2", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    u3 = _Part(
        "U3",
        name="MCP9808 temperature sensor",
        footprint="Package_QFP:MCU",
        nets=[ch1, vcc, gnd],
        pins=8,
    )
    c3 = _Part("C3", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    circuit = _Circuit([u2, c2, u3, c3], [vcc, gnd, ch0, ch1])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )
    placed = {part.ref: part for part in result.placed_parts}
    sensor_points = [
        (placed["U2"].x_mm, placed["U2"].y_mm),
        (placed["U3"].x_mm, placed["U3"].y_mm),
    ]
    c2_point = (placed["C2"].x_mm, placed["C2"].y_mm)
    c3_point = (placed["C3"].x_mm, placed["C3"].y_mm)

    assert points_form_clean_grid(sensor_points, tolerance_mm=1.0)
    assert placed["U3"].x_mm - placed["U2"].x_mm >= 60.0
    assert _distance(c2_point, sensor_points[0]) <= 8.0
    assert _distance(c3_point, sensor_points[1]) <= 8.0
    assert _distance(c2_point, sensor_points[0]) < _distance(
        c2_point,
        sensor_points[1],
    )
    assert _distance(c3_point, sensor_points[1]) < _distance(
        c3_point,
        sensor_points[0],
    )


def test_fixed_generous_outline_spreads_ui_and_mechanics():
    outline = BoardOutline(120.0, 80.0)
    vcc = _Net("VCC")
    gnd = _Net("GND")
    signals = [_Net(f"SIG{idx}") for idx in range(1, 4)]
    u1 = _Part(
        "U1",
        name="MCU",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd, *signals],
        pins=8,
    )
    switches = [
        _Part(
            f"SW{idx}",
            name="panel pushbutton switch",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[signal, gnd],
            pins=2,
        )
        for idx, signal in enumerate(signals, start=1)
    ]
    holes = [
        _Part(f"H{idx}", name="MountingHole", footprint="MountingHole:M2", nets=[], pins=0)
        for idx in range(1, 5)
    ]
    circuit = _Circuit([u1, *switches, *holes], [vcc, gnd, *signals])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    placed = {part.ref: part for part in result.placed_parts}
    switch_xs = [placed[ref].x_mm for ref in ("SW1", "SW2", "SW3")]
    switch_ys = [placed[ref].y_mm for ref in ("SW1", "SW2", "SW3")]
    hole_xs = [placed[ref].x_mm for ref in ("H1", "H2", "H3", "H4")]
    hole_ys = [placed[ref].y_mm for ref in ("H1", "H2", "H3", "H4")]

    assert result.outline is outline
    assert result.validation.overlaps == []
    assert max(switch_xs) - min(switch_xs) >= outline.width_mm * 0.60
    assert max(switch_ys) - min(switch_ys) <= 1.0
    assert max(hole_xs) - min(hole_xs) >= outline.width_mm * 0.90
    assert max(hole_ys) - min(hole_ys) >= outline.height_mm * 0.85
    assert not any("board outline is" in warning for warning in result.score.warnings)


def test_fixed_generous_outline_uses_two_dimensional_visible_grid():
    outline = BoardOutline(100.0, 70.0)
    gnd = _Net("GND")
    signals = [_Net(f"SIG{idx}") for idx in range(1, 5)]
    u1 = _Part(
        "U1",
        name="MCU",
        footprint="Package_QFP:MCU",
        nets=[gnd, *signals],
        pins=8,
    )
    leds = [
        _Part(
            f"LED{idx}",
            name="indicator LED",
            footprint="Capacitor:C_0805",
            nets=[signal, gnd],
            pins=2,
        )
        for idx, signal in enumerate(signals, start=1)
    ]
    circuit = _Circuit([u1, *leds], [gnd, *signals])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    placed = {part.ref: part for part in result.placed_parts}
    led_xs = [placed[ref].x_mm for ref in ("LED1", "LED2", "LED3", "LED4")]
    led_ys = [placed[ref].y_mm for ref in ("LED1", "LED2", "LED3", "LED4")]

    assert result.validation.overlaps == []
    assert max(led_xs) - min(led_xs) >= outline.width_mm * 0.45
    assert max(led_ys) - min(led_ys) >= outline.height_mm * 0.25
    assert not any("board outline is" in warning for warning in result.score.warnings)


def test_panel_grid_constraints_resist_proximity_optimization():
    outline = BoardOutline(40.0, 120.0)
    gnd = _Net("GND")
    sig1 = _Net("IN_1")
    sig2 = _Net("IN_2")
    sig3 = _Net("OUT_1")
    u1 = _Part(
        "U1",
        name="op amp",
        footprint="Package_QFP:MCU",
        nets=[gnd, sig1, sig2, sig3],
        pins=4,
    )
    jacks = [
        _Part(
            f"J{idx}",
            name="Thonkiconn PJ398SM panel jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[net, gnd],
        )
        for idx, net in enumerate((sig1, sig2, sig3), start=1)
    ]
    circuit = _Circuit([u1, *jacks], [gnd, sig1, sig2, sig3])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            align=[AlignConstraint(refs=["J1", "J2", "J3"], axis="x", value_mm=20.0)],
            distribute=[
                DistributeConstraint(
                    refs=["J1", "J2", "J3"],
                    axis="y",
                    start_mm=24.0,
                    end_mm=96.0,
                ),
            ],
            near=[
                NearConstraint("J1", "U1", distance_mm=2.0),
                NearConstraint("J2", "U1", distance_mm=2.0),
                NearConstraint("J3", "U1", distance_mm=2.0),
            ],
        ),
    )

    placed = {part.ref: part for part in result.placed_parts}
    assert placed["J1"].x_mm == pytest.approx(20.0)
    assert placed["J2"].x_mm == pytest.approx(20.0)
    assert placed["J3"].x_mm == pytest.approx(20.0)
    assert placed["J1"].y_mm == pytest.approx(24.0)
    assert placed["J2"].y_mm == pytest.approx(60.0)
    assert placed["J3"].y_mm == pytest.approx(96.0)


def test_plan_layout_keeps_horizontal_audio_jack_row_on_edge():
    outline = BoardOutline(75.0, 100.0)
    vcc = _Net("VCC")
    gnd = _Net("GND")
    signal_nets = [_Net(f"OUT{idx}") for idx in range(1, 7)]
    u1 = _Part(
        "U1",
        name="MCU",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd, *signal_nets],
        pins=8,
    )
    jacks = [
        _Part(
            f"J{idx}",
            name="horizontal 3.5mm trigger output jack",
            footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
            nets=[signal_nets[idx - 1], gnd],
            pins=2,
        )
        for idx in range(1, 7)
    ]
    passives = [
        _Part(
            f"R{idx}",
            value="220",
            footprint="Capacitor:C_0805",
            nets=[signal_nets[idx - 1]],
        )
        for idx in range(1, 7)
    ]
    circuit = _Circuit([u1, *jacks, *passives], [vcc, gnd, *signal_nets])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline),
    )

    anchors = {anchor.ref: anchor.edge for anchor in result.intent_plan.edge_anchors}
    placed = {part.ref: part for part in result.placed_parts}
    jack_width, jack_height = BBOXES["Connector_Audio:Jack_3.5mm_PJ320D_Horizontal"]

    for ref in {f"J{idx}" for idx in range(1, 7)}:
        assert anchors[ref] == "right"
        jack = placed[ref]
        assert jack.rot_deg == 180.0
        assert jack.x_mm + jack_width / 2 == pytest.approx(outline.x_max)
        assert not any(warning.startswith(f"{ref}: violates right-edge") for warning in result.score.warnings)


def test_soft_constraints_do_not_move_edge_anchored_connectors():
    outline = BoardOutline(75.0, 100.0)
    gnd = _Net("GND")
    j1 = _Part(
        "J1",
        name="horizontal 3.5mm trigger output jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[gnd],
        pins=2,
    )
    j2 = _Part(
        "J2",
        name="horizontal 3.5mm trigger output jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[gnd],
        pins=2,
    )
    circuit = _Circuit([j1, j2], [gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            edge_anchors=[
                EdgeAnchor("J1", "right", offset_mm=25.0),
                EdgeAnchor("J2", "right", offset_mm=75.0),
            ],
            distribute=[
                DistributeConstraint(
                    refs=["J1", "J2"],
                    axis="x",
                    start_mm=10.0,
                    end_mm=20.0,
                ),
            ],
        ),
    )

    placed = {part.ref: part for part in result.placed_parts}

    assert _placed_bounds(placed["J1"], BBOXES, result.fp_geometries)[2] == pytest.approx(
        outline.x_max - 0.5
    )
    assert _placed_bounds(placed["J2"], BBOXES, result.fp_geometries)[2] == pytest.approx(
        outline.x_max - 0.5
    )


def test_edge_parallel_check_is_limited_to_pin_access_headers():
    outline = BoardOutline(40.0, 30.0)
    gnd = _Net("GND")
    audio_jack = _Part(
        "J1",
        name="horizontal 3.5mm audio jack",
        footprint="Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
        nets=[gnd],
        pins=2,
    )
    header = _Part(
        "J2",
        name="pin header connector",
        footprint="Connector:PinHeader_1x06",
        nets=[gnd],
        pins=6,
    )
    circuit = _Circuit([audio_jack, header], [gnd])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(
            outline=outline,
            edge_anchors=[
                EdgeAnchor("J1", "bottom", offset_mm=10.0, rot_deg=90.0),
                EdgeAnchor("J2", "top", offset_mm=30.0, rot_deg=0.0),
            ],
        ),
    )

    assert not any(
        warning.startswith("J1: connector row is not parallel")
        for warning in result.score.warnings
    )
    assert not any(
        warning.startswith("J2: connector row is not parallel")
        for warning in result.score.warnings
    )
    placed = {part.ref: part for part in result.placed_parts}
    assert _edge_parallel(
        "top",
        _placed_bounds(placed["J2"], BBOXES, result.fp_geometries),
    )


def test_plan_layout_reports_power_topology_chain():
    vbus = _Net("VBUS")
    vcc = _Net("VCC")
    gnd = _Net("GND")
    sig = _Net("SIG")
    j1 = _Part("J1", name="USB connector", footprint="Connector:USB", nets=[vbus, gnd])
    u2 = _Part(
        "U2",
        name="LDO regulator",
        footprint="Package_TO_SOT:SOT23",
        nets=[vbus, gnd, vcc],
        pins=3,
    )
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd, sig], pins=3)
    circuit = _Circuit([j1, u2, c1, u1], [vbus, vcc, gnd, sig])

    result = plan_layout(
        circuit,
        fp_bboxes={
            **BBOXES,
            "Package_TO_SOT:SOT23": (3.0, 3.0),
        },
        constraints=LayoutConstraints(outline=BoardOutline(100.0, 60.0)),
    )

    assert any(candidate.name == "power_topology_first" for candidate in result.candidates)
    assert any("VBUS: J1 -> U2 -> C1 -> U1" in chain for chain in result.report.power_topology)
    assert any(
        "power chain: VBUS from J1" in reason
        for reason in result.report.part_reasons["U2"]
    )


def test_plan_layout_refines_decaps_to_actual_parent_pads(monkeypatch):
    vdd = _Net("VDD")
    gnd = _Net("GND")
    sig = _Net("SIG")
    u1 = _Part(
        "U1",
        name="MCU",
        footprint="Package_QFP:MCU",
        nets=[vdd, gnd, sig],
        pins=3,
    )
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vdd, gnd])
    circuit = _Circuit([u1, c1], [vdd, gnd, sig])
    geometries = {
        "Package_QFP:MCU": FootprintGeometry(
            footprint="Package_QFP:MCU",
            pads=[
                PadGeometry("1", -4.0, -1.5, 0.6, 0.6),
                PadGeometry("2", -4.0, 1.5, 0.6, 0.6),
                PadGeometry("3", 4.0, 0.0, 0.6, 0.6),
            ],
            body_bounds=(-5.0, -5.0, 5.0, 5.0),
        ),
        "Capacitor:C_0805": FootprintGeometry(
            footprint="Capacitor:C_0805",
            pads=[
                PadGeometry("1", -0.6, 0.0, 0.4, 0.4),
                PadGeometry("2", 0.6, 0.0, 0.4, 0.4),
            ],
            body_bounds=(-1.0, -0.6, 1.0, 0.6),
        ),
    }
    monkeypatch.setattr(
        "skidl_layout.engine._resolve_geometries",
        lambda circuit, fp_lib_dirs: geometries,
    )

    result = plan_layout(
        circuit,
        fp_bboxes={
            "Package_QFP:MCU": (10.0, 10.0),
            "Capacitor:C_0805": (2.0, 1.2),
        },
        constraints=LayoutConstraints(
            fixed=[FixedPosition("U1", 20.0, 20.0)],
            outline=BoardOutline(60.0, 40.0),
        ),
    )
    placed = {part.ref: part for part in result.placed_parts}

    assert placed["C1"].x_mm < placed["U1"].x_mm
    assert placed["C1"].rot_deg % 180 == 90.0
    assert any(
        "actual U1 VDD/GND pads" in reason
        for reason in result.report.part_reasons["C1"]
    )


def test_plan_layout_auto_outline_tightens_compact_passive_cluster():
    vcc = _Net("VCC")
    gnd = _Net("GND")
    signals = [_Net(f"SIG{idx}") for idx in range(1, 9)]
    u1 = _Part(
        "U1",
        name="MCU",
        footprint="Package_QFP:MCU",
        nets=[vcc, gnd, *signals],
        pins=10,
    )
    passives = [
        _Part(
            f"R{idx}",
            value="10K",
            footprint="Capacitor:C_0805",
            nets=[signal, gnd],
            pins=2,
        )
        for idx, signal in enumerate(signals, start=1)
    ]
    circuit = _Circuit([u1, *passives], [vcc, gnd, *signals])

    result = plan_layout(
        circuit,
        fp_bboxes=BBOXES,
        constraints=LayoutConstraints(fixed=[FixedPosition("U1", 30.0, 30.0)]),
    )

    assert result.outline is not None
    envelope = derive_outline(result.placed_parts, BBOXES)
    outline_area = result.outline.width_mm * result.outline.height_mm
    envelope_area = envelope.width_mm * envelope.height_mm
    assert outline_area <= envelope_area * 1.15 + 0.001


def test_compact_auto_outline_seed_shrinks_visible_mechanical_heavy_circuit():
    gnd = _Net("GND")
    sig1 = _Net("SIG1")
    sig2 = _Net("SIG2")
    sig3 = _Net("SIG3")
    u1 = _Part(
        "U1",
        name="MCU",
        footprint="Package_QFP:MCU",
        nets=[gnd, sig1, sig2, sig3],
        pins=8,
    )
    visible = [
        _Part(
            "SW1",
            name="panel pushbutton switch",
            footprint="Switch:3PDT_Footswitch",
            nets=[sig1, gnd],
            pins=2,
        ),
        _Part(
            "LED1",
            name="indicator LED",
            footprint="Capacitor:C_0805",
            nets=[sig2, gnd],
            pins=2,
        ),
        _Part(
            "J1",
            name="panel jack",
            footprint="Connector_Audio:Thonkiconn_PJ398SM",
            nets=[sig3, gnd],
            pins=2,
        ),
    ]
    circuit = _Circuit([u1, *visible], [gnd, sig1, sig2, sig3])

    seed = derive_outline_from_circuit(circuit, BBOXES)
    compact = _compact_auto_outline_seed(circuit, seed)

    assert compact.width_mm < seed.width_mm
    assert compact.height_mm < seed.height_mm


def _placement_signature(parts):
    return [
        (p.ref, round(p.x_mm, 4), round(p.y_mm, 4), round(p.rot_deg, 4))
        for p in parts
    ]


def test_plan_layout_dedups_identical_candidates():
    """WS1: candidates whose seed placement + constraints match an
    already-refined candidate are reused instead of re-refined, and the reused
    candidate's placement is byte-identical to its canonical."""
    result = plan_layout(_circuit(), fp_bboxes=BBOXES)

    by_name = {c.name: c for c in result.candidates}
    reused = [
        c
        for c in result.candidates
        if any("refinement reused" in reason for reason in c.reasons)
    ]
    # A plain circuit (no power chains / module sockets / repeated channels)
    # collapses several strategies onto the same constraints, so dedup must fire.
    assert reused, "expected at least one deduped candidate"

    for candidate in reused:
        canon_name = next(
            reason.split("'")[1]
            for reason in candidate.reasons
            if "refinement reused" in reason
        )
        canonical = by_name[canon_name]
        assert _placement_signature(candidate.placed_parts) == (
            _placement_signature(canonical.placed_parts)
        )

    # Dedup must not corrupt the selected placement.
    assert result.validation.ok
    assert len(result.placed_parts) == len(_circuit().parts)


def test_plan_layout_candidate_names_filters_strategies():
    """WS5: candidate_names restricts which strategies are planned."""
    result = plan_layout(
        _circuit(), fp_bboxes=BBOXES, candidate_names=["baseline"]
    )
    names = {c.name for c in result.candidates}
    assert names == {"baseline"}
    assert result.validation.ok


def test_plan_layout_candidate_names_rejects_unknown():
    with pytest.raises(ValueError, match="unknown candidate name"):
        plan_layout(
            _circuit(), fp_bboxes=BBOXES, candidate_names=["does_not_exist"]
        )


def test_plan_layout_candidate_names_env_default(monkeypatch):
    monkeypatch.setenv("SKIDL_LAYOUT_CANDIDATES", "baseline, connector_edge_first")
    result = plan_layout(_circuit(), fp_bboxes=BBOXES)
    assert {c.name for c in result.candidates} == {
        "baseline",
        "connector_edge_first",
    }
    # explicit kwarg overrides the env var
    result2 = plan_layout(
        _circuit(), fp_bboxes=BBOXES, candidate_names=["baseline"]
    )
    assert {c.name for c in result2.candidates} == {"baseline"}


def test_plan_layout_progress_callback_emits_stages():
    """WS6: progress callback fires at stage boundaries; default None is silent
    and unchanged."""
    messages = []
    result = plan_layout(
        _circuit(), fp_bboxes=BBOXES, candidate_names=["baseline"],
        progress=messages.append,
    )
    joined = "\n".join(messages)
    assert any("candidate strategy" in m for m in messages)
    assert any("refining baseline" in m for m in messages)
    assert any(m.startswith("selected 'baseline'") for m in messages)
    # silent default must produce an identical placement
    silent = plan_layout(_circuit(), fp_bboxes=BBOXES, candidate_names=["baseline"])
    sig = lambda r: [(p.ref, round(p.x_mm, 4), round(p.y_mm, 4)) for p in r.placed_parts]
    assert sig(result) == sig(silent)
