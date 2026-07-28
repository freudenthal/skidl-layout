"""Voltage-banded conductor spacing + the clearance-grading fix (Phase 8).

Two Phase-8 changes to :mod:`skidl_layout.fabspec` live here:

* **WS-E** -- IPC-2221B Table 6-1 as a cited constant plus a measurement that
  reports and enforces nothing.
* **The clearance-grading fix** -- ``fab_check`` grades DRC at the fab's
  *published floor* (``min_clearance_mm``), not at the *design* clearance the
  board is drawn to, and reports the design number as advice. Before the fix the
  two disagreed by 46 / 84 / 47 violations on three boards KRT graded at **0**,
  which made ``fab_must_pass=True`` unusable everywhere.
"""

from __future__ import annotations

import pytest

from skidl_layout import OSHPARK_2L
from skidl_layout.fabspec import (
    DEFAULT_SPACING_COLUMN,
    IPC2221B_ABOVE_500V_MM_PER_VOLT,
    IPC2221B_TABLE_6_1_MM,
    fab_check,
    ipc2221_spacing_mm,
    measure_voltage_spacing,
)


# --- the table -------------------------------------------------------------

def test_table_bands_are_ascending_and_monotone():
    for column, bands in IPC2221B_TABLE_6_1_MM.items():
        volts = [v for v, _ in bands]
        spacings = [s for _, s in bands]
        assert volts == sorted(volts), column
        assert spacings == sorted(spacings), column


def test_b3_is_deliberately_absent():
    """Only one source carried the >3050 m column, and this stack has no altitude
    input. A single-sourced number in a manufacturability check is exactly what
    the plan forbids."""
    assert "B3" not in IPC2221B_TABLE_6_1_MM


@pytest.mark.parametrize("volts,expected", [
    (0.0, 0.1), (15.0, 0.1), (24.0, 0.1), (30.0, 0.1),
    (31.0, 0.6), (50.0, 0.6), (72.0, 0.6), (150.0, 0.6),
    (151.0, 1.25), (300.0, 1.25),
    (301.0, 2.5), (500.0, 2.5),
])
def test_b2_external_uncoated_bands(volts, expected):
    assert ipc2221_spacing_mm(volts) == pytest.approx(expected)


def test_b2_above_500v_uses_the_stated_slope():
    # 2.5 + (1000 - 500) x 0.005 = 5.0 mm, the worked example both sources give.
    assert ipc2221_spacing_mm(1000.0) == pytest.approx(5.0)
    assert IPC2221B_ABOVE_500V_MM_PER_VOLT["B2"] == 0.005


def test_columns_without_a_recorded_slope_say_so_rather_than_guess():
    """``None`` means "not stated", which is a different claim from "no spacing
    required" -- and it is the honest answer when the slope was not dual-sourced."""
    assert ipc2221_spacing_mm(1000.0, column="B1") is None
    assert ipc2221_spacing_mm(1000.0, column="B4") is None
    assert ipc2221_spacing_mm(24.0, column="B1") == pytest.approx(0.05)


def test_unknown_column_is_none_not_an_exception():
    assert ipc2221_spacing_mm(24.0, column="Z9") is None


def test_the_default_column_is_the_one_this_stack_ships():
    """Every board here is 2-layer, external and not conformally coated."""
    assert DEFAULT_SPACING_COLUMN == "B2"


def test_negative_voltage_is_graded_on_magnitude():
    """A -5 V rail is 5 V of potential difference to ground, not zero."""
    assert ipc2221_spacing_mm(-400.0) == ipc2221_spacing_mm(400.0)


# --- the measurement -------------------------------------------------------

def _two_net_board(gap_mm: float) -> str:
    """Two 0.2 mm tracks on F.Cu whose copper edges sit ``gap_mm`` apart."""
    y2 = 10.0 + 0.2 + gap_mm          # centre-to-centre = width + gap
    return f"""(kicad_pcb
\t(net 0 "")
\t(net 1 "HV")
\t(net 2 "GND")
\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.05))
\t(segment (start 5 10.000000) (end 25 10.000000) (width 0.2)
\t\t(layer "F.Cu") (net 1))
\t(segment (start 5 {y2:.6f}) (end 25 {y2:.6f}) (width 0.2)
\t\t(layer "F.Cu") (net 2))
)
"""


def test_measures_the_real_edge_to_edge_gap(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.5))

    rows = measure_voltage_spacing(str(path), {"HV": 72.0})

    assert len(rows) == 1
    row = rows[0]
    assert row["measured_mm"] == pytest.approx(0.5, abs=1e-3)
    assert row["required_mm"] == pytest.approx(0.6)
    assert row["meets_requirement"] is False
    assert row["nearest_net"] == "GND"


def test_a_compliant_board_passes(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.8))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["measured_mm"] == pytest.approx(0.8, abs=1e-3)
    assert row["meets_requirement"] is True


def test_different_layers_never_contend(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.1).replace(
        '(layer "F.Cu") (net 2)', '(layer "B.Cu") (net 2)'))

    row = measure_voltage_spacing(str(path), {"HV": 72.0})[0]

    assert row["measured_mm"] is None
    assert "same-layer" in row["note"]


def test_a_net_not_on_the_board_says_so(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.5))

    row = measure_voltage_spacing(str(path), {"NOPE": 72.0})[0]

    assert row["measured_mm"] is None
    assert "not declared" in row["note"]


# --- report-only: `ok` may never move -------------------------------------

def test_voltage_spacing_never_downgrades_ok(tmp_path):
    """WS-E ships the judge, deliberately without the lever. A board that fails
    the spacing requirement must still pass ``fab_check`` on that account."""
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.2))     # well under the 0.6 mm B2 band

    res = fab_check(str(path), OSHPARK_2L, run_drc=False,
                    net_voltages={"HV": 72.0})

    assert res.voltage_spacing
    assert res.voltage_spacing[0]["meets_requirement"] is False
    assert not [v for v in res.violations if "spacing" in v.rule]
    assert res.ok is True
    assert any("advisory" in n for n in res.notes)


def test_no_voltages_means_the_rule_does_not_run(tmp_path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.2))

    res = fab_check(str(path), OSHPARK_2L, run_drc=False)

    assert res.voltage_spacing == []
    assert "voltage_spacing" not in res.checked


# --- the clearance-grading fix --------------------------------------------

def test_fab_check_grades_drc_at_the_published_floor(tmp_path, monkeypatch):
    """The floor, not the design clearance -- and the design number survives as
    advice, because "how much copper sits below the width we drew to" is a real
    question that is simply not a manufacturability verdict."""
    from skidl_layout import fabspec

    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.5))

    graded: list = []

    def fake(krt_module, resolved, pcb_path, clearance, timeout_s):
        graded.append(clearance)
        # Pretend the strict grading finds grazes and the floor finds none.
        return 0 if clearance <= OSHPARK_2L.min_clearance_mm + 1e-9 else 46

    monkeypatch.setattr(fabspec, "_graded_drc", fake)
    monkeypatch.setattr(fabspec, "_resolve_krt_for_drc",
                        lambda krt_dir: ("krt-module", "/fake/krt"))

    res = fab_check(str(path), OSHPARK_2L)

    assert graded == [OSHPARK_2L.min_clearance_mm, OSHPARK_2L.clearance_mm]
    assert res.drc_violation_count == 0
    assert res.drc_clearance_mm == pytest.approx(OSHPARK_2L.min_clearance_mm)
    assert res.design_clearance_drc_count == 46
    assert res.design_clearance_mm == pytest.approx(OSHPARK_2L.clearance_mm)
    assert res.ok is True                    # the advisory count cannot fail it


def test_an_explicit_drc_clearance_overrides_the_floor(tmp_path, monkeypatch):
    from skidl_layout import fabspec

    path = tmp_path / "b.kicad_pcb"
    path.write_text(_two_net_board(0.5))
    graded: list = []

    monkeypatch.setattr(fabspec, "_graded_drc",
                        lambda m, r, p, clearance, t: graded.append(clearance) or 0)
    monkeypatch.setattr(fabspec, "_resolve_krt_for_drc",
                        lambda krt_dir: ("krt-module", "/fake/krt"))

    res = fab_check(str(path), OSHPARK_2L, drc_clearance_mm=0.1768)

    assert graded[0] == pytest.approx(0.1768)
    assert res.drc_clearance_mm == pytest.approx(0.1768)
