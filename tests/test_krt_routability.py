"""Tests for the opt-in KiCadRoutingTools routability adapter (skidl_layout.krt).

Unit tests (always run) exercise discovery and the pure parse helpers with
canned CLI output. Integration tests run only when a built KRT checkout is
discoverable, and route the real cap_chain fixture end to end.
"""

from __future__ import annotations

import os

import pytest

from skidl_layout import krt
from skidl_layout.krt import (
    KrtNotFoundError,
    find_krt,
    route_and_check,
)


# Real route.py JSON_SUMMARY captured from cap_chain (2026-07-19).
CAP_CHAIN_SUMMARY = (
    'JSON_SUMMARY: {"routed_single": ["DPA_N", "DPB_N", "DPA_P", "DPB_P"], '
    '"failed_single": [], "failed_multipoint": [], "multipoint_nets": 0, '
    '"multipoint_pads_connected": 0, "multipoint_pads_total": 0, '
    '"multipoint_edges_routed": 0, "multipoint_edges_failed": 0, '
    '"ripup_success_pairs": [], "rerouted_pairs": [], '
    '"single_ended_target_swaps": [], "layer_swaps": 0, "successful": 4, '
    '"failed": 0, "total_time": 0.01, "total_iterations": 304, '
    '"total_vias": 0, "cleanup_disconnected": [], "min_clearance_used": 0.25}'
)

CONNECTED_OK = (
    "Loading board.kicad_pcb...\n"
    "Found 8 segments, 0 vias, 8 pads\n"
    "Checking 4 routed nets\n"
    "\n============================================================\n"
    "ALL NETS FULLY CONNECTED!\n"
    "============================================================\n"
)

# Full-board check_connected on a partially routed board: 45 nets carry copper,
# 2 of them (GND, RAMPA) are broken; no zero-copper nets.
CONNECTED_BROKEN_ONLY = (
    "Found 1365 segments, 204 vias, 242 pads\n"
    "Checking 45 routed nets\n"
    "FOUND 2 ISSUES:\n"
    "\n"
    "  Connectivity issues (2):\n"
    "\n"
    "  GND (net 3):\n"
    "    Segments: 214, Vias: 29, Pads: 30\n"
    "    Disconnected components: 3\n"
    "\n"
    "  RAMPA (net 11):\n"
    "    Segments: 69, Vias: 15, Pads: 8\n"
)

# Full-board view with both blocks: 6 nets carry copper, 1 broken (NET_A), plus
# 2 nets that never got copper at all (NET_C, NET_D).
CONNECTED_MIXED = (
    "Checking 6 routed nets\n"
    "FOUND 3 ISSUES:\n"
    "\n"
    "  Unrouted nets (2):\n"
    "    NET_C (4 pads)\n"
    "    NET_D (2 pads)\n"
    "\n"
    "  Connectivity issues (1):\n"
    "\n"
    "  NET_A (net 5):\n"
    "    Segments: 3, Vias: 0, Pads: 4\n"
)

DRC_CLEAN = (
    "Found 8 segments and 0 vias\n"
    "\n============================================================\n"
    "NO DRC VIOLATIONS FOUND!\n"
    "============================================================\n"
)

DRC_VIOLATIONS = (
    "Found 12 segments and 1 vias\n"
    "FOUND 4 DRC VIOLATIONS:\n"
    "  segment-to-segment clearance 0.10mm < 0.20mm at (5.0, 5.0)\n"
)


# --------------------------------------------------------------------------
# find_krt
# --------------------------------------------------------------------------

def _make_usable_krt(root):
    (root / "rust_router").mkdir(parents=True)
    (root / "route.py").write_text("# stub\n")
    (root / "rust_router" / "grid_router.pyd").write_text("stub")
    return root


def test_find_krt_none_when_nothing_usable(monkeypatch, tmp_path):
    # Neutralize the real workspace-sibling fallback so the None path is
    # exercised deterministically on machines that do have a built KRT.
    monkeypatch.delenv("SKIDL_LAYOUT_KRT_DIR", raising=False)
    monkeypatch.setattr(krt, "_is_usable_krt", lambda p: False)
    assert find_krt(str(tmp_path / "nope")) is None
    assert find_krt() is None


def test_find_krt_explicit_arg_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("SKIDL_LAYOUT_KRT_DIR", raising=False)
    fake = _make_usable_krt(tmp_path / "krt")
    assert find_krt(str(fake)) == str(fake)


def test_find_krt_honors_env_var(monkeypatch, tmp_path):
    fake = _make_usable_krt(tmp_path / "krt_env")
    monkeypatch.setenv("SKIDL_LAYOUT_KRT_DIR", str(fake))
    # No explicit arg -> env var is the first candidate and is usable.
    assert find_krt() == str(fake)


def test_is_usable_requires_router_extension(tmp_path):
    fake = tmp_path / "krt_no_router"
    fake.mkdir()
    (fake / "route.py").write_text("# stub\n")
    assert krt._is_usable_krt(fake) is False
    (fake / "rust_router").mkdir()
    (fake / "rust_router" / "grid_router.pyd").write_text("stub")
    assert krt._is_usable_krt(fake) is True


# --------------------------------------------------------------------------
# parse helpers
# --------------------------------------------------------------------------

def test_parse_route_summary_cap_chain():
    summary = krt._parse_route_summary(CAP_CHAIN_SUMMARY)
    assert summary["successful"] == 4
    assert summary["failed"] == 0
    assert summary["total_vias"] == 0


def test_parse_route_summary_missing_raises():
    with pytest.raises(RuntimeError):
        krt._parse_route_summary("no summary here\n")


def test_parse_route_summary_takes_last_line():
    text = "JSON_SUMMARY: {\"successful\": 1, \"failed\": 9}\n" + CAP_CHAIN_SUMMARY
    summary = krt._parse_route_summary(text)
    assert summary["successful"] == 4  # the last line wins


def test_parse_connected_ok():
    routed_count, unrouted, broken = krt._parse_connected_output(CONNECTED_OK)
    assert routed_count == 4
    assert unrouted == []
    assert broken == []


def test_parse_connected_broken_only():
    routed_count, unrouted, broken = krt._parse_connected_output(
        CONNECTED_BROKEN_ONLY
    )
    assert routed_count == 45
    assert unrouted == []
    assert broken == ["GND", "RAMPA"]


def test_parse_connected_mixed():
    routed_count, unrouted, broken = krt._parse_connected_output(CONNECTED_MIXED)
    assert routed_count == 6
    assert unrouted == ["NET_C", "NET_D"]
    assert broken == ["NET_A"]


def test_parse_drc_clean():
    assert krt._parse_drc_output(DRC_CLEAN) == 0


def test_parse_drc_violations():
    assert krt._parse_drc_output(DRC_VIOLATIONS) == 4


def test_parse_drc_indeterminate_is_zero():
    assert krt._parse_drc_output("some unrelated text") == 0


# --------------------------------------------------------------------------
# feedback assembly (pure, no subprocess)
# --------------------------------------------------------------------------

def test_feedback_full_route():
    routed_text = "(segment (start ...))\n" * 8
    fb = krt._feedback_from_outputs(routed_text, CONNECTED_OK, DRC_CLEAN)
    assert fb.total_nets == 4
    assert fb.unrouted_count == 0
    assert fb.unrouted_nets == []
    assert fb.via_count == 0
    assert fb.track_count == 8
    assert fb.drc_violation_count == 0
    assert fb.source == "kicad_routing_tools"
    assert fb.completion_pct == 100.0


def test_feedback_broken_nets_counted_within_routed():
    # 45 nets carry copper; 2 broken. Broken nets are already inside the routed
    # count, so the denominator stays 45 and completion is 43/45.
    routed_text = "(segment x)\n" * 1365 + "(via y)\n" * 204
    fb = krt._feedback_from_outputs(routed_text, CONNECTED_BROKEN_ONLY, DRC_CLEAN)
    assert fb.total_nets == 45
    assert fb.unrouted_count == 2
    assert fb.unrouted_nets == ["GND", "RAMPA"]
    assert fb.track_count == 1365
    assert fb.via_count == 204
    assert fb.drc_violation_count == 0
    assert round(fb.completion_pct, 1) == 95.6


def test_feedback_mixed_extends_denominator():
    # 6 nets carry copper (1 broken) + 2 never-routed nets -> denominator 8.
    routed_text = "(segment a)\n(segment b)\n"
    fb = krt._feedback_from_outputs(routed_text, CONNECTED_MIXED, DRC_VIOLATIONS)
    assert fb.total_nets == 8  # 6 routed + 2 zero-copper
    assert fb.unrouted_nets == ["NET_C", "NET_D", "NET_A"]
    assert fb.unrouted_count == 3
    assert fb.track_count == 2
    assert fb.drc_violation_count == 4


# --------------------------------------------------------------------------
# route_and_check error path (no KRT)
# --------------------------------------------------------------------------

def test_route_and_check_missing_krt_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("SKIDL_LAYOUT_KRT_DIR", raising=False)
    monkeypatch.setattr(krt, "_is_usable_krt", lambda p: False)
    with pytest.raises(KrtNotFoundError):
        route_and_check(
            str(tmp_path / "x.kicad_pcb"),
            str(tmp_path / "work"),
            krt_dir=str(tmp_path / "also_bogus"),
        )


# --------------------------------------------------------------------------
# Integration (skipped when KRT is not discoverable)
# --------------------------------------------------------------------------

_KRT = find_krt()
_needs_krt = pytest.mark.skipif(_KRT is None, reason="KiCadRoutingTools not available")


@_needs_krt
def test_cap_chain_fully_routed(tmp_path):
    pcb = os.path.join(_KRT, "kicad_files", "cap_chain.kicad_pcb")
    fb = route_and_check(pcb, str(tmp_path))
    assert fb.completion_pct == 100.0
    assert fb.drc_violation_count == 0
    assert fb.via_count == 0
    assert fb.track_count > 0
    assert fb.source == "kicad_routing_tools"


@_needs_krt
def test_cap_chain_deterministic(tmp_path):
    pcb = os.path.join(_KRT, "kicad_files", "cap_chain.kicad_pcb")
    a = route_and_check(pcb, str(tmp_path / "a")).to_dict()
    b = route_and_check(pcb, str(tmp_path / "b")).to_dict()
    assert a == b
