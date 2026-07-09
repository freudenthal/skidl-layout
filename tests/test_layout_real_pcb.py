from __future__ import annotations

import pytest
from simp_sexp import Sexp

from skidl import Circuit, Net, Part, Pin, SKIDL
from skidl_layout import BoardOutline, LayoutConstraints, plan_layout, write_kicad_pcb
from skidl_layout.validator import find_kicad_cli, run_kicad_drc


def _write_footprint(lib_dir, name: str, pads: list[tuple[str, float, float]]):
    pad_text = "\n".join(
        (
            f'  (pad "{num}" smd rect (at {x} {y}) '
            f'(size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))'
        )
        for num, x, y in pads
    )
    (lib_dir / f"{name}.kicad_mod").write_text(
        f'(footprint "{name}"\n'
        '  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -2) (layer "F.SilkS"))\n'
        f'  (property "Value" "{name}" (at 0 2) (layer "F.Fab"))\n'
        f"{pad_text}\n"
        ")\n"
    )


def _make_smoke_fp_lib(tmp_path):
    lib_dir = tmp_path / "Smoke.pretty"
    lib_dir.mkdir()
    _write_footprint(lib_dir, "USB_2PIN", [("1", -1.27, 0), ("2", 1.27, 0)])
    _write_footprint(
        lib_dir,
        "LDO_3PIN",
        [("1", -1.0, 0.8), ("2", 0.0, -0.8), ("3", 1.0, 0.8)],
    )
    _write_footprint(
        lib_dir,
        "MCU_4PIN",
        [("1", -1.0, -1.0), ("2", 1.0, -1.0), ("3", -1.0, 1.0), ("4", 1.0, 1.0)],
    )
    _write_footprint(lib_dir, "C_0805", [("1", -0.6, 0), ("2", 0.6, 0)])
    _write_footprint(lib_dir, "R_0805", [("1", -0.6, 0), ("2", 0.6, 0)])
    return str(tmp_path)


def _custom_part(ref, name, footprint, pins, value=""):
    return Part(
        tool=SKIDL,
        name=name,
        ref=ref,
        value=value,
        footprint=footprint,
        pins=[Pin(num=str(num)) for num in pins],
    )


def _make_smoke_circuit():
    with Circuit(name="layout_smoke") as circuit:
        vbus = Net("VBUS")
        vcc = Net("3V3")
        gnd = Net("GND")
        led = Net("LED")

        j1 = _custom_part("J1", "USB input connector", "Smoke:USB_2PIN", [1, 2])
        u2 = _custom_part("U2", "LDO regulator", "Smoke:LDO_3PIN", [1, 2, 3])
        u1 = _custom_part("U1", "MCU", "Smoke:MCU_4PIN", [1, 2, 3, 4])
        c1 = _custom_part("C1", "bypass capacitor", "Smoke:C_0805", [1, 2], "100nF")
        r1 = _custom_part("R1", "LED resistor", "Smoke:R_0805", [1, 2], "1k")

        j1[1] += vbus
        j1[2] += gnd
        u2[1] += vbus
        u2[2] += gnd
        u2[3] += vcc
        u1[1] += vcc
        u1[2] += gnd
        u1[3] += led
        u1[4] += gnd
        c1[1] += vcc
        c1[2] += gnd
        r1[1] += led
        r1[2] += vcc

    return circuit


def _child(node, key: str):
    return next(child for child in node if isinstance(child, list) and child[0] == key)


def _footprint_ref(footprint) -> str:
    prop = next(
        child
        for child in footprint
        if isinstance(child, list) and child[:2] == ["property", "Reference"]
    )
    return str(prop[2])


def _pad_nets_by_ref(board) -> dict[str, dict[str, str]]:
    result = {}
    for footprint in board.search("footprint"):
        ref = _footprint_ref(footprint)
        pads = {}
        for pad in footprint.search("pad"):
            net = _child(pad, "net")
            pads[str(pad[1])] = str(net[2])
        result[ref] = pads
    return result


def test_plan_and_write_real_skidl_circuit_to_kicad_pcb(tmp_path):
    lib_root = _make_smoke_fp_lib(tmp_path)
    circuit = _make_smoke_circuit()
    outline = BoardOutline(60.0, 40.0)

    result = plan_layout(
        circuit,
        fp_lib_dirs=[lib_root],
        constraints=LayoutConstraints(outline=outline),
        board_layers=4,
    )

    assert result.ok
    assert result.outline is outline
    assert result.validation.placed_parts == len(circuit.parts)
    assert result.fp_bboxes["Smoke:MCU_4PIN"] == pytest.approx((2.8, 2.8))
    assert result.power_plan.net("GND").suggested_layer == "In1.Cu"
    assert any(
        intent.net_name == "VBUS" and intent.strategy == "internal_rail"
        for intent in result.power_plan.route_intents
    )

    board_path = tmp_path / "layout_smoke.kicad_pcb"
    write_kicad_pcb(
        result.placed_parts,
        circuit,
        [lib_root],
        str(board_path),
        outline=result.outline,
    )

    board = Sexp(board_path.read_text())
    footprints = list(board.search("footprint"))
    net_names = [
        str(node[2])
        for node in board
        if isinstance(node, list) and len(node) > 2 and node[0] == "net"
    ]
    pad_nets = _pad_nets_by_ref(board)

    assert board[0] == "kicad_pcb"
    assert board_path.is_file()
    assert len(footprints) == len(circuit.parts)
    assert len(list(board.search("gr_rect"))) == 1
    assert {"VBUS", "3V3", "GND", "LED"}.issubset(set(net_names))
    assert pad_nets["J1"] == {"1": "VBUS", "2": "GND"}
    assert pad_nets["U2"] == {"1": "VBUS", "2": "GND", "3": "3V3"}
    assert pad_nets["C1"] == {"1": "3V3", "2": "GND"}


def test_real_pcb_smoke_drc_if_kicad_cli_exists(tmp_path):
    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed")

    lib_root = _make_smoke_fp_lib(tmp_path)
    circuit = _make_smoke_circuit()
    result = plan_layout(
        circuit,
        fp_lib_dirs=[lib_root],
        constraints=LayoutConstraints(outline=BoardOutline(60.0, 40.0)),
    )
    board_path = tmp_path / "layout_smoke.kicad_pcb"
    write_kicad_pcb(
        result.placed_parts,
        circuit,
        [lib_root],
        str(board_path),
        outline=result.outline,
    )

    passed, report = run_kicad_drc(str(board_path))

    assert passed, report
    assert "Failed to load board" not in report
