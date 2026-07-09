from __future__ import annotations

import math
import os
import pytest
from simp_sexp import Sexp

from skidl_layout.writer import (
    PlacedPart,
    footprint_bbox,
    load_footprint,
    load_footprint_bboxes,
    write_kicad_pcb,
)
from skidl_layout.constraints import BoardCutout, BoardOutline

_FP_DIRS: list[str] = []
_KICAD_FP = os.environ.get("KICAD9_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
_HAVE_KICAD_FP = os.path.isdir(_KICAD_FP)

requires_kicad_fp = pytest.mark.skipif(
    not _HAVE_KICAD_FP, reason="KiCad footprint libraries not installed"
)


# ---------------------------------------------------------------------------
# Minimal mock objects
# ---------------------------------------------------------------------------

class _MockNet:
    def __init__(self, name):
        self.name = name


class _MockCircuit:
    def __init__(self, nets=None):
        self._nets = nets or []
        self.parts = []

    def get_nets(self):
        return self._nets


class _MockPart:
    def __init__(self, ref, name="", value="", footprint="", lib=""):
        self.ref = ref
        self.hiername = ref
        self.name = name
        self.value = value
        self.footprint = footprint
        self.lib = lib


# ---------------------------------------------------------------------------
# load_footprint
# ---------------------------------------------------------------------------

@requires_kicad_fp
def test_load_footprint_returns_sexp():
    fp = load_footprint("Resistor_SMD:R_0805_2012Metric", _FP_DIRS)
    assert isinstance(fp, Sexp)
    assert fp[0] == "footprint"


@requires_kicad_fp
def test_load_footprint_has_pads():
    fp = load_footprint("Resistor_SMD:R_0805_2012Metric", _FP_DIRS)
    pads = list(fp.search("pad"))
    assert len(pads) >= 2


def test_load_footprint_not_found_raises():
    with pytest.raises(FileNotFoundError):
        load_footprint("NonExistent:NoSuchPart", ["/tmp/no_such_dir"])


# ---------------------------------------------------------------------------
# footprint_bbox
# ---------------------------------------------------------------------------

@requires_kicad_fp
def test_footprint_bbox_positive_dimensions():
    fp = load_footprint("Resistor_SMD:R_0805_2012Metric", _FP_DIRS)
    w, h = footprint_bbox(fp)
    assert w > 0
    assert h > 0


def test_footprint_bbox_empty_returns_zero():
    fp = Sexp("(footprint NoName)")
    w, h = footprint_bbox(fp)
    assert w == 0.0
    assert h == 0.0


def test_footprint_bbox_single_pad():
    src = '(footprint "X" (pad "1" smd (at 1.0 2.0) (size 1.0 2.0)))'
    fp = Sexp(src)
    w, h = footprint_bbox(fp)
    assert w == pytest.approx(1.0)
    assert h == pytest.approx(2.0)


def test_footprint_bbox_two_pads():
    src = (
        '(footprint "X"'
        '  (pad "1" smd (at -1.0 0) (size 0.5 0.5))'
        '  (pad "2" smd (at  1.0 0) (size 0.5 0.5))'
        ")"
    )
    fp = Sexp(src)
    w, h = footprint_bbox(fp)
    # x spans from -1.25 to 1.25 → 2.5
    assert w == pytest.approx(2.5)
    assert h == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# load_footprint_bboxes
# ---------------------------------------------------------------------------

@requires_kicad_fp
def test_load_footprint_bboxes_known():
    names = {"Resistor_SMD:R_0805_2012Metric", "Capacitor_SMD:C_0201_0603Metric"}
    bboxes = load_footprint_bboxes(names, _FP_DIRS)
    for name in names:
        assert name in bboxes
        w, h = bboxes[name]
        assert w > 0 and h > 0


def test_load_footprint_bboxes_missing_skipped():
    names = {"NonExistent:Foo"}
    bboxes = load_footprint_bboxes(names, ["/tmp/no_such_dir"])
    assert "NonExistent:Foo" not in bboxes


# ---------------------------------------------------------------------------
# write_kicad_pcb
# ---------------------------------------------------------------------------

def _make_minimal_fp_lib(tmp_path: "Path") -> str:
    """Create a minimal .kicad_mod file in a temp lib dir, return lib root."""
    lib_dir = tmp_path / "TestLib.pretty"
    lib_dir.mkdir()
    mod = lib_dir / "R_Test.kicad_mod"
    mod.write_text(
        '(footprint "R_Test"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -2) (layer "F.SilkS"))\n'
        '  (property "Value" "R_Test" (at 0 2) (layer "F.Fab"))\n'
        '  (pad "1" smd (at -0.5 0) (size 0.6 1.0) (layers "F.Cu"))\n'
        '  (pad "2" smd (at  0.5 0) (size 0.6 1.0) (layers "F.Cu"))\n'
        ")\n"
    )
    return str(tmp_path)


def test_write_minimal_pcb_creates_file(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit(nets=[_MockNet("VCC"), _MockNet("GND")])

    parts = [
        PlacedPart(ref="R1", x_mm=10.0, y_mm=20.0, rot_deg=0.0, footprint="TestLib:R_Test"),
        PlacedPart(ref="R2", x_mm=30.0, y_mm=20.0, rot_deg=90.0, footprint="TestLib:R_Test"),
    ]

    out = str(tmp_path / "out" / "board.kicad_pcb")
    write_kicad_pcb(parts, circuit, [lib_root], out)

    assert os.path.isfile(out)


def test_write_minimal_pcb_valid_sexp(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit(nets=[_MockNet("VCC")])

    parts = [PlacedPart(ref="R1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:R_Test")]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(parts, circuit, [lib_root], out)

    with open(out) as f:
        content = f.read()

    board = Sexp(content)
    assert board[0] == "kicad_pcb"


def test_write_strips_library_only_pad_properties(tmp_path):
    lib_dir = tmp_path / "TestLib.pretty"
    lib_dir.mkdir()
    (lib_dir / "ThermalPad.kicad_mod").write_text(
        '(footprint "ThermalPad"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -2) (layer "F.SilkS"))\n'
        '  (property "Value" "ThermalPad" (at 0 2) (layer "F.Fab"))\n'
        '  (pad "1" thru_hole circle (at 0 0) (size 0.6 0.6) (drill 0.2)\n'
        '    (property "pad_prop_heatsink")\n'
        '    (layers "*.Cu" "F.Mask"))\n'
        ")\n"
    )
    circuit = _MockCircuit()
    parts = [PlacedPart(ref="U1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:ThermalPad")]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [str(tmp_path)], out)

    content = open(out).read()
    assert "pad_prop_heatsink" not in content
    assert Sexp(content)[0] == "kicad_pcb"


def test_write_demotes_footprint_edge_cuts_to_user_drawing_layer(tmp_path):
    lib_dir = tmp_path / "TestLib.pretty"
    lib_dir.mkdir()
    (lib_dir / "EdgeMarkedJack.kicad_mod").write_text(
        '(footprint "EdgeMarkedJack"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -2) (layer "F.SilkS"))\n'
        '  (property "Value" "EdgeMarkedJack" (at 0 2) (layer "F.Fab"))\n'
        '  (fp_line (start -2 1) (end 2 1) (stroke (width 0.12) (type solid)) (layer "Edge.Cuts"))\n'
        '  (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu" "*.Mask"))\n'
        ")\n"
    )
    circuit = _MockCircuit()
    parts = [
        PlacedPart(
            ref="J1",
            x_mm=5.0,
            y_mm=5.0,
            rot_deg=0.0,
            footprint="TestLib:EdgeMarkedJack",
        )
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [str(tmp_path)], out)

    board = Sexp(open(out).read())
    footprint = list(board.search("footprint"))[0]
    line = list(footprint.search("fp_line"))[0]
    layer = next(child for child in line if isinstance(child, list) and child[0] == "layer")
    assert str(layer[1]).strip('"') == "Dwgs.User"


def test_write_minimal_pcb_footprint_count(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()

    parts = [
        PlacedPart(ref="R1", x_mm=10.0, y_mm=10.0, rot_deg=0.0, footprint="TestLib:R_Test"),
        PlacedPart(ref="R2", x_mm=20.0, y_mm=10.0, rot_deg=0.0, footprint="TestLib:R_Test"),
    ]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(parts, circuit, [lib_root], out)

    with open(out) as f:
        board = Sexp(f.read())

    footprints = list(board.search("footprint"))
    assert len(footprints) == 2


def test_write_back_side_part_flips_footprint_layers(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()

    parts = [
        PlacedPart(
            ref="R1",
            x_mm=10.0,
            y_mm=10.0,
            rot_deg=0.0,
            footprint="TestLib:R_Test",
            side="back",
        ),
    ]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(parts, circuit, [lib_root], out)

    board = Sexp(open(out).read())
    footprint = list(board.search("footprint"))[0]
    top_layer = next(child for child in footprint if isinstance(child, list) and child[0] == "layer")
    reference = next(
        prop
        for prop in board.search("property")
        if len(prop) > 2 and str(prop[1]).strip('"') == "Reference"
    )
    value = next(
        prop
        for prop in board.search("property")
        if len(prop) > 2 and str(prop[1]).strip('"') == "Value"
    )
    ref_layer = next(child for child in reference if isinstance(child, list) and child[0] == "layer")
    value_layer = next(child for child in value if isinstance(child, list) and child[0] == "layer")
    pad_layers = [
        next(child for child in pad if isinstance(child, list) and child[0] == "layers")
        for pad in board.search("pad")
    ]

    assert str(top_layer[1]).strip('"') == "B.Cu"
    assert str(ref_layer[1]).strip('"') == "B.SilkS"
    assert str(value_layer[1]).strip('"') == "B.Fab"
    assert all(str(layer[1]).strip('"') == "B.Cu" for layer in pad_layers)


def test_write_rotated_footprint_makes_pad_rotations_explicit(tmp_path):
    lib_dir = tmp_path / "TestLib.pretty"
    lib_dir.mkdir()
    (lib_dir / "LongPads.kicad_mod").write_text(
        '(footprint "LongPads"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -2) (layer "F.SilkS"))\n'
        '  (pad "1" smd roundrect (at -1 0) (size 2.0 0.5) (layers "F.Cu" "F.Mask"))\n'
        '  (pad "2" smd roundrect (at 1 0 15) (size 2.0 0.5) (layers "F.Cu" "F.Mask"))\n'
        ")\n"
    )
    circuit = _MockCircuit()
    parts = [
        PlacedPart(
            ref="U1",
            x_mm=10.0,
            y_mm=10.0,
            rot_deg=90.0,
            footprint="TestLib:LongPads",
        )
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [str(tmp_path)], out)

    pads = {
        str(pad[1]).strip('"'): pad
        for pad in Sexp(open(out).read()).search("pad")
    }
    pad1_at = next(child for child in pads["1"] if isinstance(child, list) and child[0] == "at")
    pad2_at = next(child for child in pads["2"] if isinstance(child, list) and child[0] == "at")
    assert float(pad1_at[3]) == pytest.approx(90.0)
    assert float(pad2_at[3]) == pytest.approx(105.0)


def test_write_minimal_pcb_net_declarations(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    nets = [_MockNet("VCC"), _MockNet("GND"), _MockNet("SIG")]
    circuit = _MockCircuit(nets=nets)

    parts = [PlacedPart(ref="R1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:R_Test")]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(parts, circuit, [lib_root], out)

    with open(out) as f:
        board = Sexp(f.read())

    net_nodes = [child for child in board if isinstance(child, list) and child[0] == "net"]
    net_names = [str(n[2]) for n in net_nodes if len(n) > 2]
    assert "VCC" in net_names
    assert "GND" in net_names
    assert "SIG" in net_names


def test_write_minimal_pcb_with_outline(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()
    parts = [PlacedPart(ref="R1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:R_Test")]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(parts, circuit, [lib_root], out, outline=BoardOutline(100.0, 80.0))

    with open(out) as f:
        board = Sexp(f.read())

    rects = list(board.search("gr_rect"))
    assert len(rects) == 1
    end = next(c for c in rects[0] if isinstance(c, list) and c[0] == "end")
    assert float(end[1]) == pytest.approx(100.0)
    assert float(end[2]) == pytest.approx(80.0)


def test_write_hides_mounting_hole_silkscreen_reference(tmp_path):
    lib_dir = tmp_path / "MountingHole.pretty"
    lib_dir.mkdir()
    (lib_dir / "M2.kicad_mod").write_text(
        '(footprint "M2"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -3.15 0) (layer "F.SilkS"))\n'
        '  (pad "" thru_hole circle (at 0 0) (size 2.2 2.2) (drill 2.2) (layers "*.Cu" "*.Mask"))\n'
        ")\n"
    )
    circuit = _MockCircuit()
    parts = [
        PlacedPart(
            ref="H1",
            x_mm=3.0,
            y_mm=3.0,
            rot_deg=0.0,
            footprint="MountingHole:M2",
        )
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [str(tmp_path)], out, outline=BoardOutline(20, 20))

    board = Sexp(open(out).read())
    ref = next(
        prop
        for prop in board.search("property")
        if len(prop) > 2 and str(prop[1]).strip('"') == "Reference"
    )
    assert ["hide", "yes"] in ref


def test_write_hides_small_smd_passive_silkscreen_reference(tmp_path):
    lib_dir = tmp_path / "Resistor_SMD.pretty"
    lib_dir.mkdir()
    (lib_dir / "R_0603_1608Metric.kicad_mod").write_text(
        '(footprint "R_0603_1608Metric"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -1.43 0) (layer "F.SilkS"))\n'
        '  (property "Value" "R_0603_1608Metric" (at 0 1.43 0) (layer "F.Fab"))\n'
        '  (pad "1" smd rect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Mask"))\n'
        '  (pad "2" smd rect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Mask"))\n'
        ")\n"
    )
    circuit = _MockCircuit()
    parts = [
        PlacedPart(
            ref="R1",
            x_mm=5.0,
            y_mm=5.0,
            rot_deg=0.0,
            footprint="Resistor_SMD:R_0603_1608Metric",
        )
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [str(tmp_path)], out, outline=BoardOutline(20, 20))

    board = Sexp(open(out).read())
    ref = next(
        prop
        for prop in board.search("property")
        if len(prop) > 2 and str(prop[1]).strip('"') == "Reference"
    )
    assert ["hide", "yes"] in ref


def test_write_nudges_silkscreen_reference_inside_outline(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()
    parts = [
        PlacedPart(
            ref="R1",
            x_mm=5.0,
            y_mm=0.4,
            rot_deg=0.0,
            footprint="TestLib:R_Test",
        )
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [lib_root], out, outline=BoardOutline(10, 10))

    board = Sexp(open(out).read())
    ref = next(
        prop
        for prop in board.search("property")
        if len(prop) > 2 and str(prop[1]).strip('"') == "Reference"
    )
    at = next(child for child in ref if isinstance(child, list) and child[0] == "at")
    assert float(at[1]) == pytest.approx(0.0)
    assert float(at[2]) == pytest.approx(1.6)


def test_write_rounded_rect_outline_as_edge_lines(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()
    parts = [
        PlacedPart(ref="R1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:R_Test")
    ]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(
        parts,
        circuit,
        [lib_root],
        out,
        outline=BoardOutline(100.0, 80.0, corner_radius_mm=3.0),
    )

    with open(out) as f:
        board = Sexp(f.read())

    assert list(board.search("gr_rect")) == []
    assert len(list(board.search("gr_line"))) > 8


def test_write_rounded_rect_outline_skips_degenerate_closing_segment(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()
    parts = [
        PlacedPart(ref="R1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:R_Test")
    ]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(
        parts,
        circuit,
        [lib_root],
        out,
        outline=BoardOutline(
            vertices=[
                (-2.0, -0.3),
                (102.46668346943426, -0.3),
                (102.46668346943426, 78.85001260207571),
                (-2.0, 78.85001260207571),
            ],
            corner_radius_mm=2.7,
        ),
    )

    board = Sexp(open(out).read())
    for line in board.search("gr_line"):
        start = next(child for child in line if isinstance(child, list) and child[0] == "start")
        end = next(child for child in line if isinstance(child, list) and child[0] == "end")
        length = math.hypot(float(end[1]) - float(start[1]), float(end[2]) - float(start[2]))
        assert length > 1e-6


def test_write_missing_footprint_skipped(tmp_path):
    circuit = _MockCircuit()
    parts = [
        PlacedPart(ref="U1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="NoLib:NoFP"),
    ]
    out = str(tmp_path / "board.kicad_pcb")
    write_kicad_pcb(parts, circuit, [], out, strict_missing_footprints=False)

    with open(out) as f:
        board = Sexp(f.read())

    footprints = list(board.search("footprint"))
    assert len(footprints) == 0


def test_write_missing_footprint_raises_by_default(tmp_path):
    circuit = _MockCircuit()
    parts = [
        PlacedPart(ref="U1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="NoLib:NoFP"),
    ]
    out = str(tmp_path / "board.kicad_pcb")

    with pytest.raises(FileNotFoundError, match="INCOMPLETE PCB"):
        write_kicad_pcb(parts, circuit, [], out)

    assert not os.path.exists(out)


def test_write_skips_schematic_only_power_flags(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()
    circuit.parts = [
        _MockPart("R1", name="R", footprint="TestLib:R_Test"),
        _MockPart("PF1", name="PWR_FLAG", footprint="", lib="power"),
    ]
    parts = [
        PlacedPart(ref="R1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:R_Test"),
        PlacedPart(ref="PF1", x_mm=8.0, y_mm=5.0, rot_deg=0.0, footprint=""),
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [lib_root], out)

    board = Sexp(open(out).read())
    footprints = list(board.search("footprint"))
    assert len(footprints) == 1
    assert '"R1"' in open(out).read()
    assert "PF1" not in open(out).read()


def test_write_empty_footprint_still_fails_for_physical_parts(tmp_path):
    circuit = _MockCircuit()
    circuit.parts = [_MockPart("U1", name="MCU", footprint="")]
    parts = [
        PlacedPart(ref="U1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint=""),
    ]
    out = str(tmp_path / "board.kicad_pcb")

    with pytest.raises(FileNotFoundError, match="INCOMPLETE PCB"):
        write_kicad_pcb(parts, circuit, [], out)


def test_write_polygon_outline_as_edge_lines(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()
    parts = [
        PlacedPart(
            ref="R1",
            x_mm=5.0,
            y_mm=5.0,
            rot_deg=0.0,
            footprint="TestLib:R_Test",
        )
    ]
    outline = BoardOutline(vertices=[(0, 0), (30, 0), (25, 20), (0, 20)])
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [lib_root], out, outline=outline)

    with open(out) as f:
        board = Sexp(f.read())

    assert list(board.search("gr_rect")) == []
    assert len(list(board.search("gr_line"))) == 4


def test_write_internal_cutouts_as_edge_cuts(tmp_path):
    lib_root = _make_minimal_fp_lib(tmp_path)
    circuit = _MockCircuit()
    parts = [
        PlacedPart(ref="R1", x_mm=5.0, y_mm=5.0, rot_deg=0.0, footprint="TestLib:R_Test")
    ]
    cutouts = [
        BoardCutout(10.0, 10.0, 16.0, 18.0, name="rect_window"),
        BoardCutout(22.0, 12.0, 28.0, 18.0, shape="circle", radius_mm=3.0),
        BoardCutout(
            32.0,
            10.0,
            40.0,
            18.0,
            shape="polygon",
            vertices=[(32.0, 10.0), (40.0, 12.0), (38.0, 18.0), (32.0, 16.0)],
        ),
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(
        parts,
        circuit,
        [lib_root],
        out,
        outline=BoardOutline(50.0, 30.0),
        cutouts=cutouts,
    )

    board = Sexp(open(out).read())
    edge_rects = [
        rect
        for rect in board.search("gr_rect")
        if any(
            isinstance(child, list)
            and child[0] == "layer"
            and str(child[1]).strip('"') == "Edge.Cuts"
            for child in rect
        )
    ]
    edge_circles = list(board.search("gr_circle"))
    edge_lines = list(board.search("gr_line"))

    assert len(edge_rects) == 2  # outer rectangular outline plus rect cutout.
    assert len(edge_circles) == 1
    assert len(edge_lines) == 4


def test_write_filters_unsupported_internal_footprint_layers(tmp_path):
    lib_dir = tmp_path / "TestLib.pretty"
    lib_dir.mkdir()
    mod = lib_dir / "Module_With_Zone.kicad_mod"
    mod.write_text(
        '(footprint "Module_With_Zone"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -2) (layer F.SilkS))\n'
        '  (property "Value" "Module_With_Zone" (at 0 2) (layer F.Fab))\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "*.Cu" "F.Mask"))\n'
        '  (zone\n'
        '    (net 0)\n'
        '    (net_name "")\n'
        '    (layers F.Cu B.Cu In1.Cu In2.Cu)\n'
        '    (uuid "11111111-1111-1111-1111-111111111111")\n'
        '    (hatch edge 0.5)\n'
        '    (connect_pads (clearance 0.2))\n'
        '    (polygon (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)))\n'
        '  )\n'
        '  (zone\n'
        '    (net 0)\n'
        '    (net_name "")\n'
        '    (layers F.Cu B.Cu)\n'
        '    (uuid "11111111-1111-1111-1111-111111111111")\n'
        '    (hatch edge 0.5)\n'
        '    (connect_pads (clearance 0.2))\n'
        '    (polygon (pts (xy -2 -2) (xy 2 -2) (xy 2 2) (xy -2 2)))\n'
        '  )\n'
        ')\n'
    )
    circuit = _MockCircuit()
    parts = [
        PlacedPart(
            ref="U1",
            x_mm=5.0,
            y_mm=5.0,
            rot_deg=0.0,
            footprint="TestLib:Module_With_Zone",
        )
    ]
    out = str(tmp_path / "board.kicad_pcb")

    write_kicad_pcb(parts, circuit, [str(tmp_path)], out)

    content = open(out).read()
    assert "In1.Cu" not in content
    assert "In2.Cu" not in content
    board = Sexp(content)
    zone = list(board.search("zone"))[0]
    layers = next(child for child in zone if isinstance(child, list) and child[0] == "layers")
    assert layers == ["layers", "F.Cu", "B.Cu"]
    uuids = [
        str(node[1]).strip('"')
        for node in board.search("uuid")
        if len(node) > 1
    ]
    assert len(uuids) == len(set(uuids))
    assert "11111111-1111-1111-1111-111111111111" not in uuids
