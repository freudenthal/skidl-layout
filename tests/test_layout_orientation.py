from __future__ import annotations

import pytest

from skidl_layout.constraints import FaceEdgeConstraint, LayoutConstraints
from skidl_layout.geometry import FootprintGeometry, PadGeometry
from skidl_layout.orientation import (
    infer_connector_mating_face,
    infer_edge_mating_rotation,
    refine_orientations,
)
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
    def __init__(self, ref, footprint, pins):
        self.ref = ref
        self.footprint = footprint
        self.pins = []
        for num, net in pins:
            self.pins.append(_Pin(self, num, net))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self._nets = nets

    def get_nets(self):
        return self._nets


def _connector_part(ref: str, name: str, footprint: str) -> _Part:
    sig = _Net(f"{ref}_SIG")
    part = _Part(ref, footprint, [(1, sig)])
    part.name = name
    return part


@pytest.mark.parametrize(
    ("part", "mating_kind", "expected_kind", "local_exit", "rotations"),
    [
        (
            _connector_part(
                "J1",
                "3.5mm stereo headphone jack",
                "Connector_Audio:Jack_3.5mm_PJ320D_Horizontal",
            ),
            "audio_jack",
            "audio_jack",
            "-x",
            {"left": 0.0, "bottom": 90.0, "right": 180.0, "top": 270.0},
        ),
        (
            _connector_part(
                "J2",
                "right-angle pin header",
                "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Horizontal",
            ),
            "header",
            "header",
            "+x",
            {"right": 0.0, "top": 90.0, "left": 180.0, "bottom": 270.0},
        ),
        (
            _connector_part(
                "J3",
                "USB-C receptacle",
                "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",
            ),
            "usb",
            "usb",
            "+y",
            {"bottom": 0.0, "right": 90.0, "top": 180.0, "left": 270.0},
        ),
        (
            _connector_part(
                "J4",
                "2-pin terminal block",
                "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-3-2-5.08_1x02_P5.08mm_Horizontal",
            ),
            "generic_connector",
            "terminal_block",
            "+y",
            {"bottom": 0.0, "right": 90.0, "top": 180.0, "left": 270.0},
        ),
        (
            _connector_part(
                "J5",
                "horizontal barrel power jack",
                "Connector_BarrelJack:BarrelJack_Horizontal",
            ),
            "barrel",
            "barrel",
            "+y",
            {"bottom": 0.0, "right": 90.0, "top": 180.0, "left": 270.0},
        ),
    ],
)
def test_infers_connector_mating_face_and_outward_edge_rotation(
    part,
    mating_kind,
    expected_kind,
    local_exit,
    rotations,
):
    face = infer_connector_mating_face(part, mating_kind=mating_kind)

    assert face is not None
    assert face.kind == expected_kind
    assert face.local_exit == local_exit
    assert face.edge_inset_mm == 0.0
    for edge, rotation in rotations.items():
        assert infer_edge_mating_rotation(
            part,
            edge,
            mating_kind=mating_kind,
        ) == rotation


def test_explicit_connector_mating_face_metadata_overrides_footprint_guess():
    header = _connector_part(
        "J5",
        "right-angle pin header",
        "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Horizontal",
    )
    header.mating_face_local_direction = "-y"

    face = infer_connector_mating_face(header, mating_kind="header")

    assert face.local_exit == "-y"
    assert infer_edge_mating_rotation(header, "top", mating_kind="header") == 0.0


def test_usb_mating_face_can_be_inferred_from_footprint_text_without_part():
    face = infer_connector_mating_face(
        None,
        text=(
            "Connector_USB:"
            "USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
        ),
        mating_kind="usb",
    )

    assert face is not None
    assert face.local_exit == "+y"
    assert face.local_face_offset_mm == pytest.approx(3.1)


def test_vertical_panel_audio_jack_has_no_edge_mating_face():
    jack = _connector_part(
        "J1",
        "Thonkiconn PJ398SM vertical 3.5mm audio jack",
        "Connector_Audio:Jack_3.5mm_PJ398SM_Vertical",
    )

    assert infer_connector_mating_face(jack, mating_kind="panel_jack") is None
    assert infer_edge_mating_rotation(jack, "right", mating_kind="panel_jack") is None


def test_refine_orientations_rotates_pad_toward_connected_neighbor():
    sig = _Net("SIG")
    u1 = _Part("U1", "Pkg:Directional", [(1, sig)])
    j1 = _Part("J1", "Pkg:Other", [(1, sig)])
    circuit = _Circuit([u1, j1], [sig])
    placed = [
        PlacedPart("U1", 0.0, 0.0, 0.0, "Pkg:Directional"),
        PlacedPart("J1", 10.0, 0.0, 0.0, "Pkg:Other"),
    ]
    geometries = {
        "Pkg:Directional": FootprintGeometry(
            footprint="Pkg:Directional",
            pads=[PadGeometry("1", 0.0, -1.0, 0.5, 0.5)],
        )
    }

    result = refine_orientations(placed, circuit, geometries)
    refined = {part.ref: part for part in result.placed_parts}

    assert refined["U1"].rot_deg == 270.0
    assert "pad/net pressure" in result.ref_reasons["U1"][0]


def test_refine_orientations_skips_face_edge_constrained_part():
    sig = _Net("SIG")
    u1 = _Part("U1", "Pkg:Directional", [(1, sig)])
    j1 = _Part("J1", "Pkg:Other", [(1, sig)])
    circuit = _Circuit([u1, j1], [sig])
    placed = [
        PlacedPart("U1", 0.0, 0.0, 0.0, "Pkg:Directional"),
        PlacedPart("J1", 10.0, 0.0, 0.0, "Pkg:Other"),
    ]
    geometries = {
        "Pkg:Directional": FootprintGeometry(
            footprint="Pkg:Directional",
            pads=[PadGeometry("1", 0.0, -1.0, 0.5, 0.5)],
        )
    }
    constraints = LayoutConstraints(
        face_edges=[FaceEdgeConstraint("U1", "right")]
    )

    result = refine_orientations(placed, circuit, geometries, constraints=constraints)
    refined = {part.ref: part for part in result.placed_parts}

    assert refined["U1"].rot_deg == 0.0
    assert result.ref_reasons == {}
