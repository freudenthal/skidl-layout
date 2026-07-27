"""Unit tests for skidl_layout.copper_fill (power-layout Phase 5, WS-1 / WS-2).

The KiCad-dependent half is exercised twice: once with the discovery
monkeypatched to ``None`` (the degrade-cleanly contract every caller relies on),
and once for real when a ``pcbnew``-bearing python is actually present. The
parsing half needs no KiCad at all -- it runs on synthetic board text.
"""

from __future__ import annotations

import os

import pytest

from skidl_layout import copper_fill


@pytest.fixture(autouse=True)
def _clear_cache():
    copper_fill.clear_cache()
    yield
    copper_fill.clear_cache()


# --- board fixtures --------------------------------------------------------

_FILLED_BOARD = """(kicad_pcb
  (net 0 "")
  (net 1 "GND")
  (net 2 "VIN")
  (zone
    (net 1)
    (net_name "GND")
    (layer "B.Cu")
    (fill yes)
    (filled_polygon
      (layer "B.Cu")
      (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10))
    )
    (filled_polygon
      (layer "B.Cu")
      (pts (xy 20 20) (xy 22 20) (xy 22 21) (xy 20 21))
    )
  )
  (zone
    (net "VIN")
    (layer "F.Cu")
    (fill yes)
    (filled_polygon
      (layer "F.Cu")
      (pts (xy 0 0) (xy 4 0) (xy 4 5) (xy 0 5))
    )
  )
)
"""

# A board KRT wrote: (fill yes) but no filled polygons -- KiCad fills at open.
# Complete enough for KiCad itself to load (layers / setup / outline), because
# the real-KiCad test at the bottom refills exactly this.
_UNFILLED_BOARD = """(kicad_pcb
\t(version 20241229)
\t(generator "skidl-layout-test")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
\t(setup (pad_to_mask_clearance 0))
\t(net 0 "")
\t(net 1 "GND")
\t(gr_rect (start 0 0) (end 20 20)
\t\t(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts")
\t\t(uuid "11111111-1111-1111-1111-111111111111"))
\t(zone
\t\t(net 1)
\t\t(net_name "GND")
\t\t(layer "B.Cu")
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(hatch edge 0.5)
\t\t(connect_pads yes (clearance 0.2))
\t\t(min_thickness 0.1)
\t\t(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
\t\t(polygon (pts (xy 1 1) (xy 19 1) (xy 19 19) (xy 1 19)))
\t)
)
"""

_ROUTED_BOARD = """(kicad_pcb
  (net 0 "")
  (net 1 "SW")
  (net 2 "GND")
  (segment (start 0 0) (end 3 0) (width 0.5) (layer "F.Cu") (net 1))
  (segment (start 3 0) (end 3 4) (width 0.3) (layer "F.Cu") (net 1))
  (segment (start 0 0) (end 0 2) (width 0.2) (layer "B.Cu") (net 2))
  (via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""


# --- discovery -------------------------------------------------------------

def test_find_kicad_python_honours_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_text("")
    monkeypatch.setenv(copper_fill.KICAD_PYTHON_ENV, str(fake))
    monkeypatch.setattr(copper_fill, "_imports_pcbnew", lambda p: p == str(fake))
    assert copper_fill.find_kicad_python() == str(fake)


def test_find_kicad_python_skips_a_python_without_pcbnew(monkeypatch, tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_text("")
    monkeypatch.setenv(copper_fill.KICAD_PYTHON_ENV, str(fake))
    monkeypatch.setattr(copper_fill, "_imports_pcbnew", lambda p: False)
    assert copper_fill.find_kicad_python() is None


def test_find_kicad_python_memoises(monkeypatch, tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_text("")
    monkeypatch.setenv(copper_fill.KICAD_PYTHON_ENV, str(fake))
    calls = []

    def probe(path):
        calls.append(path)
        return True

    monkeypatch.setattr(copper_fill, "_imports_pcbnew", probe)
    copper_fill.find_kicad_python()
    copper_fill.find_kicad_python()
    assert len(calls) == 1


def test_windows_candidates_prefer_the_highest_version(monkeypatch, tmp_path):
    root = tmp_path / "KiCad"
    for version in ("7.0", "10.0", "9.0"):
        target = root / version / "bin"
        target.mkdir(parents=True)
        (target / "python.exe").write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    found = copper_fill._windows_candidates()
    versioned = [p for p in found if os.path.isfile(p)]
    # 10.0 must beat 9.0 -- a string sort would put "9.0" first.
    assert versioned[0].replace("\\", "/").split("/")[-3] == "10.0"


def test_version_key_orders_numerically():
    assert copper_fill._version_key("10.0") > copper_fill._version_key("9.0")
    assert copper_fill._version_key("nightly") == (-1,)


# --- degrade-cleanly contract ---------------------------------------------

def test_fill_board_returns_none_without_kicad(monkeypatch, tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_UNFILLED_BOARD)
    monkeypatch.setattr(copper_fill, "find_kicad_python", lambda *a, **k: None)
    assert copper_fill.fill_board(str(board)) is None


def test_fill_board_returns_none_when_the_refill_fails(monkeypatch, tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_UNFILLED_BOARD)
    monkeypatch.setattr(copper_fill, "find_kicad_python", lambda *a, **k: "py")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(copper_fill.subprocess, "run", lambda *a, **k: _Proc())
    assert copper_fill.fill_board(str(board)) is None


def test_fill_board_still_raises_on_a_missing_file(tmp_path):
    # A missing tool degrades; a missing input is a caller bug.
    with pytest.raises(FileNotFoundError):
        copper_fill.fill_board(str(tmp_path / "nope.kicad_pcb"))


# --- parsing ---------------------------------------------------------------

def test_parse_filled_board_areas_and_islands():
    filled = copper_fill.parse_filled_board(_FILLED_BOARD, path="b.kicad_pcb")
    assert filled.area_mm2[("GND", "B.Cu")] == pytest.approx(102.0)
    assert filled.island_count[("GND", "B.Cu")] == 2
    assert filled.area_mm2[("VIN", "F.Cu")] == pytest.approx(20.0)
    assert filled.island_count[("VIN", "F.Cu")] == 1
    assert filled.total_area_mm2 == pytest.approx(122.0)
    assert filled.area_by_net() == {"GND": 102.0, "VIN": 20.0}


def test_parse_filled_board_resolves_all_three_net_spellings():
    # net_name (KRT), bare (net "NAME") (KiCad 10 re-save), and a numeric id.
    text = """(kicad_pcb
      (net 5 "RAIL")
      (zone (net 5) (layer "F.Cu")
        (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1))))
    )"""
    filled = copper_fill.parse_filled_board(text)
    assert ("RAIL", "F.Cu") in filled.area_mm2


def test_parse_filled_board_on_an_unfilled_krt_board_is_empty():
    # The Phase-4 finding, pinned: a board KRT wrote measures zero area.
    filled = copper_fill.parse_filled_board(_UNFILLED_BOARD)
    assert filled.area_mm2 == {}
    assert filled.total_area_mm2 == 0.0


def test_filled_board_to_dict_is_json_shaped():
    filled = copper_fill.parse_filled_board(_FILLED_BOARD, path="b.kicad_pcb")
    payload = filled.to_dict()
    assert payload["total_area_mm2"] == pytest.approx(122.0)
    assert {z["net"] for z in payload["zones"]} == {"GND", "VIN"}
    assert all(isinstance(z["island_count"], int) for z in payload["zones"])


# --- routed copper (WS-2) --------------------------------------------------

def test_read_routed_copper_sums_length_times_width(tmp_path):
    board = tmp_path / "r.kicad_pcb"
    board.write_text(_ROUTED_BOARD)
    copper = copper_fill.read_routed_copper(str(board))
    sw = copper["SW"]
    assert sw.segments == 2
    assert sw.length_mm == pytest.approx(7.0)
    assert sw.copper_area_mm2 == pytest.approx(3.0 * 0.5 + 4.0 * 0.3)
    assert sw.max_width_mm == pytest.approx(0.5)


def test_read_routed_copper_ignores_vias(tmp_path):
    board = tmp_path / "r.kicad_pcb"
    board.write_text(_ROUTED_BOARD)
    copper = copper_fill.read_routed_copper(str(board))
    # The via on SW must not add area; only the two segments count.
    assert copper["SW"].segments == 2


def test_read_routed_copper_filters_to_requested_nets(tmp_path):
    board = tmp_path / "r.kicad_pcb"
    board.write_text(_ROUTED_BOARD)
    copper = copper_fill.read_routed_copper(str(board), nets={"GND"})
    assert set(copper) == {"GND"}


def test_read_routed_copper_omits_nets_with_no_segments(tmp_path):
    board = tmp_path / "r.kicad_pcb"
    board.write_text(_ROUTED_BOARD)
    copper = copper_fill.read_routed_copper(str(board))
    # A poured net and an unrouted net are different things; neither is a zero.
    assert "VIN" not in copper


# --- the real thing, when KiCad is installed -------------------------------

@pytest.mark.skipif(copper_fill.find_kicad_python() is None,
                    reason="no python that imports pcbnew")
def test_fill_board_against_real_kicad(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(_UNFILLED_BOARD)
    assert copper_fill.parse_filled_board(_UNFILLED_BOARD).total_area_mm2 == 0.0
    filled = copper_fill.fill_board(str(board))
    assert filled is not None
    # KiCad fills the 18x18 mm polygon; the exact area depends on its own
    # clearance/edge rules, so assert only that real copper appeared where the
    # file had none -- which is the whole point of this module.
    assert filled.total_area_mm2 > 0.0
    assert filled.island_count[("GND", "B.Cu")] >= 1
