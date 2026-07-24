"""Tests for the FabSpec design-rule model + fab_check gate (WS-F1, WS-F4)."""

from __future__ import annotations

import math

import pytest
from simp_sexp import Sexp

from skidl_layout.fabspec import (
    FabSpec,
    OSHPARK_2L,
    fab_check,
    resolve_fab_spec,
)


# --------------------------------------------------------------------------
# WS-F1: the dataclass + preset + resolver
# --------------------------------------------------------------------------

def test_oshpark_preset_mil_values_exact():
    # mil x 0.0254 -> mm, to the 4-decimal precision the preset carries.
    assert math.isclose(OSHPARK_2L.min_track_mm, 6 * 0.0254, rel_tol=0, abs_tol=1e-4)
    assert math.isclose(OSHPARK_2L.min_clearance_mm, 6 * 0.0254, rel_tol=0, abs_tol=1e-4)
    assert math.isclose(OSHPARK_2L.min_annular_ring_mm, 5 * 0.0254, rel_tol=0, abs_tol=1e-4)
    assert math.isclose(OSHPARK_2L.min_drill_mm, 10 * 0.0254, rel_tol=0, abs_tol=1e-4)
    assert math.isclose(OSHPARK_2L.board_edge_keepout_mm, 15 * 0.0254, rel_tol=0, abs_tol=1e-4)


def test_oshpark_via_ring_clears_annular_minimum():
    ring = (OSHPARK_2L.via_size_mm - OSHPARK_2L.via_drill_mm) / 2.0
    assert ring >= OSHPARK_2L.min_annular_ring_mm
    # the round-1 headline: 0.6/0.3 gives 0.15 mm >= 0.127
    assert math.isclose(ring, 0.15, abs_tol=1e-9)


def test_copper_thickness_from_weight():
    assert math.isclose(OSHPARK_2L.copper_thickness_mm, 0.035, abs_tol=1e-6)
    assert math.isclose(FabSpec("x", copper_weight_oz=2.0).copper_thickness_mm, 0.070, abs_tol=1e-6)


def test_invariant_track_below_min_raises():
    with pytest.raises(ValueError, match="track_width_mm"):
        FabSpec("bad", min_track_mm=0.3, track_width_mm=0.1)


def test_invariant_clearance_below_min_raises():
    with pytest.raises(ValueError, match="clearance_mm"):
        FabSpec("bad", min_clearance_mm=0.3, clearance_mm=0.1)


def test_invariant_drill_below_min_raises():
    with pytest.raises(ValueError, match="via_drill_mm"):
        FabSpec("bad", min_drill_mm=0.4, via_drill_mm=0.3, via_size_mm=1.0)


def test_invariant_annular_ring_too_small_raises():
    # KRT's own default via (0.5/0.3 -> ring 0.10) fails a 0.127 annular floor.
    with pytest.raises(ValueError, match="annular ring"):
        FabSpec("krt-default-via", via_size_mm=0.5, via_drill_mm=0.3,
                min_annular_ring_mm=0.127)


def test_resolve_none_and_false_off():
    assert resolve_fab_spec(None) is None
    assert resolve_fab_spec(False) is None


def test_resolve_true_is_oshpark():
    assert resolve_fab_spec(True) is OSHPARK_2L


def test_resolve_name_case_insensitive():
    assert resolve_fab_spec("oshpark-2l") is OSHPARK_2L
    assert resolve_fab_spec("OSHPark-2L") is OSHPARK_2L
    assert resolve_fab_spec("oshpark") is OSHPARK_2L


def test_resolve_instance_passthrough():
    spec = FabSpec("custom")
    assert resolve_fab_spec(spec) is spec


def test_resolve_unknown_name_raises_listing_presets():
    with pytest.raises(ValueError, match="unknown fab spec"):
        resolve_fab_spec("jlcpcb-6l")


# --------------------------------------------------------------------------
# WS-F4: fab_check against synthetic boards (one violation each)
# --------------------------------------------------------------------------

_HEADER = """(kicad_pcb (version 20241229) (generator "test")
  (net 0 "")
  (net 1 "V5")
  (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts") (width 0.1))
"""


def _board(body: str) -> str:
    return _HEADER + body + "\n)\n"


def _write(tmp_path, body):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_board(body), encoding="utf-8")
    return str(p)


def test_fab_check_clean_board_passes(tmp_path):
    body = (
        '  (segment (start 5 5) (end 20 5) (width 0.3) (layer "F.Cu") (net 1))\n'
        '  (via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
    )
    res = fab_check(_write(tmp_path, body), OSHPARK_2L, run_drc=False)
    assert res.ok, res.summary()
    assert not res.violations


def test_fab_check_thin_trace_flagged(tmp_path):
    body = '  (segment (start 5 5) (end 20 5) (width 0.1) (layer "F.Cu") (net 1))\n'
    res = fab_check(_write(tmp_path, body), OSHPARK_2L, run_drc=False)
    assert any(v.rule == "min_track" for v in res.violations)


def test_fab_check_undersized_drill_flagged(tmp_path):
    body = '  (via (at 10 10) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1))\n'
    res = fab_check(_write(tmp_path, body), OSHPARK_2L, run_drc=False)
    rules = {v.rule for v in res.violations}
    assert "min_drill" in rules


def test_fab_check_small_ring_flagged(tmp_path):
    # 0.5/0.3 -> ring 0.10 < 0.127 (the KRT-default-via failure this plan found)
    body = '  (via (at 10 10) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
    res = fab_check(_write(tmp_path, body), OSHPARK_2L, run_drc=False)
    assert any(v.rule == "annular_ring" for v in res.violations)


def test_fab_check_edge_encroachment_flagged(tmp_path):
    # segment endpoint at x=0.1 is only 0.1 mm from the x=0 edge (< 0.381)
    body = '  (segment (start 0.1 15) (end 20 15) (width 0.3) (layer "F.Cu") (net 1))\n'
    res = fab_check(_write(tmp_path, body), OSHPARK_2L, run_drc=False)
    assert any(v.rule == "edge_keepout" for v in res.violations)


def test_fab_check_no_outline_skips_size(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(
        '(kicad_pcb (version 20241229) (generator "t") (net 0 "")\n'
        '  (segment (start 5 5) (end 20 5) (width 0.3) (layer "F.Cu") (net 0))\n)\n',
        encoding="utf-8",
    )
    res = fab_check(str(p), OSHPARK_2L, run_drc=False)
    assert "board_size" not in res.checked
    assert any("no Edge.Cuts" in n for n in res.notes)


def test_fab_check_result_to_dict_shape(tmp_path):
    body = '  (segment (start 5 5) (end 20 5) (width 0.1) (layer "F.Cu") (net 1))\n'
    res = fab_check(_write(tmp_path, body), OSHPARK_2L, run_drc=False)
    d = res.to_dict()
    assert d["spec"] == "oshpark-2l"
    assert d["ok"] is False
    assert isinstance(d["violations"], list) and d["violations"]
    assert set(d["violations"][0]) == {"rule", "obj", "measured", "limit"}


# --------------------------------------------------------------------------
# WS-F2: writer stackup emission + default byte-identity
# --------------------------------------------------------------------------

from skidl_layout.writer import PlacedPart, write_kicad_pcb  # noqa: E402


class _Net:
    def __init__(self, name):
        self.name = name


class _Part:
    def __init__(self, ref):
        self.ref = ref
        self.hiername = ref
        self.hiertuple = (ref,)
        self.footprint = "TestLib:R_Test"
        self.lib = "TestLib"


class _Circuit:
    def __init__(self, nets, parts=("R1",)):
        self._nets = [_Net(n) for n in nets]
        self.parts = [_Part(r) for r in parts]

    def get_nets(self):
        return self._nets


def _fp_lib(tmp_path):
    lib = tmp_path / "TestLib.pretty"
    lib.mkdir()
    (lib / "R_Test.kicad_mod").write_text(
        '(footprint "R_Test"\n  (layer "F.Cu")\n'
        '  (property "Reference" "REF**" (at 0 -2) (layer "F.SilkS"))\n'
        '  (pad "1" smd (at -0.5 0) (size 0.6 1.0) (layers "F.Cu"))\n'
        '  (pad "2" smd (at  0.5 0) (size 0.6 1.0) (layers "F.Cu"))\n)\n'
    )
    return str(tmp_path)


def test_writer_default_path_has_no_stackup(tmp_path):
    circuit = _Circuit(["V5", "GND"])
    parts = [PlacedPart("R1", 10.0, 10.0, 0.0, "TestLib:R_Test")]
    out = str(tmp_path / "b.kicad_pcb")
    write_kicad_pcb(parts, circuit, [_fp_lib(tmp_path)], out)
    text = (tmp_path / "b.kicad_pcb").read_text()
    assert "(stackup" not in text
    assert "(thickness 1.6)" in text


def test_writer_default_byte_identical(tmp_path):
    """fab_spec=None must produce exactly the same bytes as omitting it."""
    circuit = _Circuit(["V5", "GND"])
    parts = [PlacedPart("R1", 10.0, 10.0, 0.0, "TestLib:R_Test")]
    root = _fp_lib(tmp_path)
    a = str(tmp_path / "a.kicad_pcb")
    b = str(tmp_path / "b.kicad_pcb")
    write_kicad_pcb(parts, circuit, [root], a)
    write_kicad_pcb(parts, circuit, [root], b, fab_spec=None)
    assert (tmp_path / "a.kicad_pcb").read_text() == (tmp_path / "b.kicad_pcb").read_text()


def test_writer_with_spec_emits_stackup(tmp_path):
    circuit = _Circuit(["V5", "GND"])
    parts = [PlacedPart("R1", 10.0, 10.0, 0.0, "TestLib:R_Test")]
    out = str(tmp_path / "c.kicad_pcb")
    write_kicad_pcb(parts, circuit, [_fp_lib(tmp_path)], out, fab_spec=OSHPARK_2L)
    text = (tmp_path / "c.kicad_pcb").read_text()
    # stackup present with both copper foils, core, finish, constraints
    assert "(stackup" in text
    assert '(layer "F.Cu"' in text and '(layer "B.Cu"' in text
    assert '(type "copper")' in text
    assert '(type "core")' in text
    assert '(copper_finish "ENIG")' in text
    assert "(dielectric_constraints no)" in text
    # board-level thickness reflects the spec
    assert "(thickness 1.6)" in text
    # still a valid s-expr
    board = Sexp(text)
    assert board[0] == "kicad_pcb"


# --------------------------------------------------------------------------
# WS-F3: KRT design-rule flag assembly
# --------------------------------------------------------------------------

from skidl_layout.krt import _design_rule_flags  # noqa: E402
from skidl_layout.power_copper import _spec_route_kwargs, _spec_pour_kwargs  # noqa: E402


def test_design_rule_flags_all_none_empty():
    assert _design_rule_flags(None, None, None, None, None) == []


def test_design_rule_flags_assembled_in_order():
    flags = _design_rule_flags(0.3, 0.25, 0.6, 0.3, 0.381)
    assert flags == [
        "--track-width", "0.3", "--clearance", "0.25",
        "--via-size", "0.6", "--via-drill", "0.3",
        "--board-edge-clearance", "0.381",
    ]


def test_design_rule_flags_partial():
    # board_edge omitted -> no flag; others present.
    assert _design_rule_flags(0.3, None, 0.6, None) == [
        "--track-width", "0.3", "--via-size", "0.6",
    ]


def test_spec_route_kwargs_off_is_empty():
    assert _spec_route_kwargs(None) == {}


def test_spec_route_kwargs_from_oshpark():
    kw = _spec_route_kwargs(OSHPARK_2L)
    assert kw == {
        "track_width": 0.3, "clearance": 0.25, "via_size": 0.6,
        "via_drill": 0.3, "board_edge_clearance": OSHPARK_2L.board_edge_keepout_mm,
    }


def test_spec_pour_kwargs_off_is_empty():
    assert _spec_pour_kwargs(None) == {}


def test_spec_pour_kwargs_from_oshpark():
    kw = _spec_pour_kwargs(OSHPARK_2L)
    assert kw == {
        "track_width": 0.3, "clearance": 0.25, "via_size": 0.6, "via_drill": 0.3,
    }


def test_route_and_check_appends_design_rule_flags(monkeypatch, tmp_path):
    """route_and_check must forward FabSpec design-rule values as CLI flags."""
    import skidl_layout.krt as krt
    fake = tmp_path / "krt"
    (fake / "rust_router").mkdir(parents=True)
    (fake / "route.py").write_text("x")
    (fake / "rust_router" / "grid_router.pyd").write_text("x")
    captured = {}

    def fake_run(args, krt_dir, timeout_s):
        captured.setdefault("args", args)

        class P:
            stdout = 'JSON_SUMMARY: {"successful": 1, "failed": 0}'
            stderr = ""
            returncode = 0
        if args[0] == "route.py":
            with open(args[2], "w", encoding="utf-8") as fh:
                fh.write('(net 1 "V5")\n')
        return P()

    monkeypatch.setattr(krt, "_run_krt", fake_run)
    krt.route_and_check(
        str(tmp_path / "in.kicad_pcb"), str(tmp_path / "work"),
        krt_dir=str(fake), track_width=0.3, clearance=0.25,
        via_size=0.6, via_drill=0.3, board_edge_clearance=0.381,
    )
    args = captured["args"]
    assert "--via-size" in args and args[args.index("--via-size") + 1] == "0.6"
    assert "--board-edge-clearance" in args

