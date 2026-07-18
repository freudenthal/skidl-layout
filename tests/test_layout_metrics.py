"""Tests for skidl_layout.metrics (adapted from lachlan's evaluate_layout/score)."""

from __future__ import annotations

import pytest

from skidl import Circuit, Net, Part
from skidl_layout import plan_layout
from skidl_layout.metrics import (
    LayoutMetrics,
    evaluate_circuit,
    metrics_from_result,
    summary_table,
)


def _divider_circuit() -> Circuit:
    ckt = Circuit()
    with ckt:
        vin, vout, gnd = Net("VIN"), Net("VOUT"), Net("GND")
        r1 = Part("Device", "R", value="10k",
                  footprint="Resistor_SMD:R_0805_2012Metric")
        r2 = Part("Device", "R", value="10k",
                  footprint="Resistor_SMD:R_0805_2012Metric")
        c1 = Part("Device", "C", value="100nF",
                  footprint="Capacitor_SMD:C_0805_2012Metric")
        r1[1] += vin
        r1[2] += vout
        r2[1] += vout
        r2[2] += gnd
        c1[1] += vout
        c1[2] += gnd
    return ckt


def test_evaluate_circuit_produces_clean_metrics():
    metrics = evaluate_circuit(_divider_circuit())
    assert isinstance(metrics, LayoutMetrics)
    assert metrics.part_count_placed == 3
    assert metrics.errors == []
    # a 3-part divider must place cleanly
    assert metrics.layout_ok
    assert metrics.overlaps == 0
    assert metrics.missing_refs == 0
    # HPWL is a real, positive wire-length estimate
    assert metrics.hpwl_total_mm > 0.0


def test_evaluate_circuit_reuses_result_without_replanning():
    """WS4: evaluate_circuit(result=...) must grade an existing LayoutResult and
    return metrics identical to the re-planning path (no second placement)."""
    ckt = _divider_circuit()
    result = plan_layout(ckt, fp_lib_dirs=None)

    reused = evaluate_circuit(ckt, result=result)
    replanned = evaluate_circuit(ckt)

    assert reused.to_dict() == replanned.to_dict()
    # metrics_from_result is the underlying pure mapping.
    assert metrics_from_result(result).to_dict() == reused.to_dict()


def test_layout_score_rubric():
    # clean placement -> full marks
    clean = LayoutMetrics(layout_ok=True, overlaps=0,
                          outline_violations=0, missing_refs=0)
    assert clean.layout_score == 100.0
    # invalid placement -> zero
    bad = LayoutMetrics(layout_ok=False)
    assert bad.layout_score == 0.0
    # a few overlaps dock points but stay > 50
    minor = LayoutMetrics(layout_ok=True, overlaps=3,
                          outline_violations=0, missing_refs=0)
    assert 50.0 < minor.layout_score < 100.0


def test_to_dict_includes_score():
    d = evaluate_circuit(_divider_circuit()).to_dict()
    assert "layout_score" in d
    assert d["part_count_placed"] == 3


def test_summary_table_aggregates():
    m = evaluate_circuit(_divider_circuit())
    table = summary_table([("divider", m)])
    assert "divider" in table
    assert "Avg layout score" in table


def test_evaluate_circuit_bad_input_reports_error():
    metrics = evaluate_circuit(object())  # not a circuit
    assert metrics.errors
    assert not metrics.layout_ok
