"""Tests for the board-level layout-quality taxonomy (WS-H2c).

The classifier reads a ``LayoutResult``-shaped object, so most cases are built
from lightweight stand-ins: that keeps the taxonomy's behaviour pinned without
paying for a real placement, and lets the outline-utilisation codes be exercised
even though our placer spreads to fill its outline and never trips them for real
(documented in ``layout_quality.py``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skidl_layout.layout_quality import (
    ADVISORY_CODES,
    BLOCKING_CODES,
    HIGH_CONGESTION_THRESHOLD,
    QualityIssue,
    layout_quality,
)


# --------------------------------------------------------------------------
# Stand-ins
# --------------------------------------------------------------------------

def _score(**kw):
    base = dict(
        congestion_score=0.0,
        congestion_regions=[],
        front_panel_trace_count=0,
        front_panel_trace_mm=0.0,
        compact_outline_area_ratio=1.0,
        compact_outline_mm={},
        footprint_envelope_area_ratio=1.0,
        empty_margin_ratios={},
        max_empty_margin_ratio=0.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _validation(**kw):
    base = dict(
        overlaps=[], outline_violations=[], keepout_violations=[],
        cutout_violations=[], missing_refs=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _outline(width=60.0, height=60.0):
    return SimpleNamespace(
        vertices=[(0, 0), (width, 0), (width, height), (0, height)],
        x_min=0.0, y_min=0.0, x_max=width, y_max=height,
    )


def _result(*, validation=None, score=None, parts=10, outline=None):
    return SimpleNamespace(
        validation=validation or _validation(),
        score=score if score is not None else _score(),
        placed_parts=[SimpleNamespace(ref=f"R{i}") for i in range(parts)],
        outline=outline if outline is not None else _outline(),
    )


def _codes(quality):
    return [i.code for i in quality.issues]


# --------------------------------------------------------------------------
# Taxonomy shape
# --------------------------------------------------------------------------

def test_blocking_and_advisory_code_sets_are_disjoint():
    assert not (BLOCKING_CODES & ADVISORY_CODES)


def test_clean_placement_reports_nothing():
    quality = layout_quality(_result())
    assert quality.issues == []
    assert quality.ok is True
    assert quality.to_dict() == {
        "ok": True, "issues": [], "blocking_count": 0, "advisory_count": 0
    }


def test_issue_blocking_flag_follows_the_code_set():
    assert QualityIssue("LAYOUT_OVERLAP", "error", "x").blocking is True
    assert QualityIssue("HIGH_CONGESTION", "warning", "x").blocking is False


# --------------------------------------------------------------------------
# Placement correctness -> blocking
# --------------------------------------------------------------------------

@pytest.mark.parametrize("attr,code", [
    ("overlaps", "LAYOUT_OVERLAP"),
    ("outline_violations", "LAYOUT_OUTLINE_VIOLATION"),
    ("keepout_violations", "LAYOUT_KEEPOUT"),
    ("cutout_violations", "LAYOUT_CUTOUT"),
    ("missing_refs", "LAYOUT_MISSING_REF"),
])
def test_each_validation_failure_maps_to_its_blocking_code(attr, code):
    quality = layout_quality(_result(validation=_validation(**{attr: ["R1", "R2"]})))
    assert _codes(quality) == [code]
    issue = quality.issues[0]
    assert issue.severity == "error"
    assert issue.blocking is True
    assert issue.evidence["count"] == 2
    assert issue.recommendation
    assert quality.ok is False


def test_evidence_lists_are_capped():
    many = [f"R{i}" for i in range(50)]
    quality = layout_quality(_result(validation=_validation(overlaps=many)))
    assert quality.issues[0].evidence["count"] == 50
    assert len(quality.issues[0].evidence["items"]) == 20


# --------------------------------------------------------------------------
# Routing outcomes join the same taxonomy
# --------------------------------------------------------------------------

def test_unrouted_nets_and_drc_are_blocking_broken_nets_are_not():
    quality = layout_quality(_result(), route_summary={
        "unrouted_nets": ["SDA"],
        "broken_nets": ["GND"],
        "drc_violation_count": 3,
    })
    assert set(_codes(quality)) == {
        "ROUTE_UNCONNECTED", "ROUTE_BROKEN_NET", "DRC_CLEARANCE"
    }
    assert {i.code for i in quality.blocking} == {"ROUTE_UNCONNECTED", "DRC_CLEARANCE"}
    assert [i.code for i in quality.advisory] == ["ROUTE_BROKEN_NET"]
    assert quality.ok is False


def test_a_clean_route_reports_nothing():
    quality = layout_quality(_result(), route_summary={
        "unrouted_nets": [], "broken_nets": [], "drc_violation_count": 0,
    })
    assert quality.issues == []
    assert quality.ok is True


# --------------------------------------------------------------------------
# Congestion (threshold is a parameter -- the harvested default is his scale)
# --------------------------------------------------------------------------

def test_congestion_fires_above_the_threshold():
    result = _result(score=_score(congestion_score=HIGH_CONGESTION_THRESHOLD + 1))
    quality = layout_quality(result)
    assert _codes(quality) == ["HIGH_CONGESTION"]
    assert quality.ok is True          # advisory only


def test_congestion_is_suppressed_by_a_clean_route():
    """A board that routed clean is not congested in any way that matters."""
    result = _result(score=_score(congestion_score=500.0))
    quality = layout_quality(result, route_summary={
        "unrouted_nets": [], "broken_nets": [], "drc_violation_count": 0,
    })
    assert "HIGH_CONGESTION" not in _codes(quality)


def test_congestion_threshold_is_tunable_and_disablable():
    result = _result(score=_score(congestion_score=200.0))
    assert _codes(layout_quality(result, congestion_threshold=300.0)) == []
    assert _codes(layout_quality(result, congestion_threshold=None)) == []
    assert _codes(layout_quality(result, congestion_threshold=100.0)) == [
        "HIGH_CONGESTION"
    ]


# --------------------------------------------------------------------------
# Outline utilisation
# --------------------------------------------------------------------------

def test_underused_outline_codes_fire_on_a_clustered_placement():
    result = _result(score=_score(
        compact_outline_area_ratio=0.20,
        footprint_envelope_area_ratio=0.10,
        max_empty_margin_ratio=0.40,
        empty_margin_ratios={"left": 0.40, "right": 0.02,
                             "top": 0.01, "bottom": 0.03},
    ))
    assert set(_codes(result_q := layout_quality(result))) == {
        "OUTLINE_UNDERUSED", "LOW_PART_SPREAD", "UNUSED_OUTLINE_REGION"
    }
    assert result_q.ok is True         # all advisory


def test_outline_checks_are_gated_on_board_size():
    """A 4-part, 100 mm2 board is not judged on how it uses its outline."""
    score = _score(compact_outline_area_ratio=0.05,
                   footprint_envelope_area_ratio=0.05,
                   max_empty_margin_ratio=0.9)
    tiny = _result(score=score, parts=4, outline=_outline(10.0, 10.0))
    assert layout_quality(tiny).issues == []


def test_front_panel_trace_span_is_advisory():
    quality = layout_quality(_result(score=_score(
        front_panel_trace_count=2, front_panel_trace_mm=88.5)))
    assert _codes(quality) == ["FRONT_PANEL_TRACE_SPAN"]
    assert quality.ok is True
    assert quality.issues[0].evidence["front_panel_trace_mm"] == 88.5


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_summary_lists_every_issue_and_its_recommendation():
    quality = layout_quality(
        _result(validation=_validation(overlaps=[("R1", "R2")]),
                score=_score(congestion_score=999.0)))
    text = quality.summary()
    assert "BLOCKED" in text
    assert "LAYOUT_OVERLAP" in text and "HIGH_CONGESTION" in text
    assert "->" in text          # recommendations rendered


def test_to_dict_round_trips_counts():
    quality = layout_quality(
        _result(validation=_validation(overlaps=[("R1", "R2")]),
                score=_score(congestion_score=999.0)))
    data = quality.to_dict()
    assert data["ok"] is False
    assert data["blocking_count"] == 1
    assert data["advisory_count"] == 1
    assert {i["code"] for i in data["issues"]} == {
        "LAYOUT_OVERLAP", "HIGH_CONGESTION"
    }


# --------------------------------------------------------------------------
# Report-only contract (same as fab_check)
# --------------------------------------------------------------------------

def test_never_raises_on_a_degenerate_result():
    """Missing score / outline / validation degrade, they do not explode."""
    bare = SimpleNamespace(validation=None, score=None,
                           placed_parts=[], outline=None)
    assert layout_quality(bare).issues == []
