"""Tests for the opt-in KiCadRoutingTools routability adapter (skidl_layout.krt).

Unit tests (always run) exercise discovery and the pure parse helpers with
canned CLI output. Integration tests run only when a built KRT checkout is
discoverable, and route the real cap_chain fixture end to end.
"""

from __future__ import annotations

import json
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
# power-copper parse helpers (pure)
# --------------------------------------------------------------------------

# Minimal board text: net declarations + segments at two widths + a poured zone.
POURED_BOARD = (
    '  (net 0 "")\n'
    '  (net 1 "GND")\n'
    '  (net 2 "VIN_12V")\n'
    '  (net 3 "V5")\n'
    '  (segment (start 1 1) (end 2 2) (width 0.8) (layer "F.Cu") (net 2))\n'
    '  (segment (start 2 2) (end 3 3) (width 0.85) (layer "F.Cu") (net 2))\n'
    '  (segment (start 4 4) (end 5 5) (width 0.25) (layer "F.Cu") (net 3))\n'
    '  (via (at 2 2) (size 0.6) (drill 0.3) (net 1))\n'
    '  (zone (net 1) (net_name "GND") (layer "B.Cu")\n'
    '    (filled_polygon (layer "B.Cu") (pts (xy 0 0) (xy 10 0) (xy 10 10)))\n'
    '  )\n'
)


def test_parse_net_id_map():
    net_map = krt._parse_net_id_map(POURED_BOARD)
    assert net_map == {0: "", 1: "GND", 2: "VIN_12V", 3: "V5"}


def test_segment_widths_by_net_takes_max():
    widths = krt._segment_widths_by_net(POURED_BOARD)
    # VIN_12V has two segments (0.8, 0.85) -> max 0.85; V5 -> 0.25; GND poured
    # (no segment) -> absent.
    assert widths == {"VIN_12V": 0.85, "V5": 0.25}
    assert "GND" not in widths


def test_parse_zone_summary_counts():
    summary = krt._parse_zone_summary(POURED_BOARD)
    assert summary["zone_count"] == 1
    assert summary["filled_polygon_count"] == 1
    assert summary["via_count"] == 1
    assert summary["segment_count"] == 3


# --------------------------------------------------------------------------
# pour_planes guard paths (no subprocess)
# --------------------------------------------------------------------------

def test_pour_planes_length_mismatch_raises(tmp_path):
    with pytest.raises(ValueError):
        krt.pour_planes(
            str(tmp_path / "x.kicad_pcb"),
            str(tmp_path / "out.kicad_pcb"),
            nets=["GND", "V5"],
            plane_layers=["B.Cu"],
            workdir=str(tmp_path / "work"),
        )


def test_pour_planes_missing_krt_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("SKIDL_LAYOUT_KRT_DIR", raising=False)
    monkeypatch.setattr(krt, "_is_usable_krt", lambda p: False)
    with pytest.raises(KrtNotFoundError):
        krt.pour_planes(
            str(tmp_path / "x.kicad_pcb"),
            str(tmp_path / "out.kicad_pcb"),
            nets=["GND"],
            plane_layers=["B.Cu"],
            workdir=str(tmp_path / "work"),
            krt_dir=str(tmp_path / "bogus"),
        )


def test_route_and_check_builds_power_net_args(monkeypatch, tmp_path):
    """power_net_widths must map to --power-nets/--power-nets-widths in order."""
    fake = _make_usable_krt(tmp_path / "krt")
    captured = {}

    def fake_run(args, krt_dir, timeout_s):
        captured.setdefault("args", args)
        # First call is route.py: fabricate a valid summary + output file so the
        # function proceeds; later checker calls get benign output.
        class P:
            stdout = 'JSON_SUMMARY: {"successful": 1, "failed": 0}'
            stderr = ""
            returncode = 0
        if args[0] == "route.py":
            with open(args[2], "w", encoding="utf-8") as fh:
                fh.write('(net 1 "V5")\n(segment (width 0.8) (net 1))\n')
        return P()

    monkeypatch.setattr(krt, "_run_krt", fake_run)
    route_and_check(
        str(tmp_path / "in.kicad_pcb"),
        str(tmp_path / "work"),
        krt_dir=str(fake),
        nets=["*", "!GND"],
        power_net_widths={"VIN_12V": 0.8, "V5": 0.5},
    )
    args = captured["args"]
    assert "--power-nets" in args and "--power-nets-widths" in args
    pn = args.index("--power-nets")
    pw = args.index("--power-nets-widths")
    assert args[pn + 1:pn + 3] == ["VIN_12V", "V5"]
    assert args[pw + 1:pw + 3] == ["0.8", "0.5"]
    # Phase 10: no map given -> no flag at all, so the argv is byte-identical to
    # every call made before the parameter existed.
    assert "--net-clearances" not in args


def _capture_route_argv(monkeypatch, tmp_path, **kwargs):
    """Run ``route_and_check`` against a stubbed KRT and return route.py's argv."""
    fake = _make_usable_krt(tmp_path / "krt")
    captured = {}

    def fake_run(args, krt_dir, timeout_s):
        captured.setdefault("args", args)

        class P:
            stdout = 'JSON_SUMMARY: {"successful": 1, "failed": 0}'
            stderr = ""
            returncode = 0
        if args[0] == "route.py":
            with open(args[2], "w", encoding="utf-8") as fh:
                fh.write('(net 1 "V5")\n(segment (width 0.8) (net 1))\n')
        return P()

    monkeypatch.setattr(krt, "_run_krt", fake_run)
    route_and_check(
        str(tmp_path / "in.kicad_pcb"), str(tmp_path / "work"),
        krt_dir=str(fake), **kwargs)
    return captured["args"]


def test_net_clearances_are_written_as_json_and_passed_to_route(monkeypatch, tmp_path):
    """Phase 10's lever reaches KRT as ``--net-clearances <json>``.

    ⚠⚠ It is passed **alongside** ``--clearance``, and that combination is the
    whole question the phase had to settle by measurement: KRT documents
    ``--clearance`` as a pure CEILING over every net class, but the clamp applies
    only to the map it auto-reads from a sibling ``.kicad_pro`` -- an explicit
    map is used as-is (``route.py`` ~2732-2748). Measured on ``lt3758_flyback``:
    ``SW``<->``VIN`` went 0.1984 mm -> 0.552 mm with the ceiling still 0.25.
    """
    args = _capture_route_argv(
        monkeypatch, tmp_path, clearance=0.25,
        net_clearances={"VIN": 0.6, "SW": 0.6})
    assert "--clearance" in args and args[args.index("--clearance") + 1] == "0.25"
    assert "--net-clearances" in args
    path = args[args.index("--net-clearances") + 1]
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == {"SW": 0.6, "VIN": 0.6}


def test_an_empty_net_clearance_map_emits_no_flag(monkeypatch, tmp_path):
    """⛔ ``{}`` must mean "pass nothing", never "every net has no class".

    An explicit ``--net-clearances`` REPLACES KRT's auto-read net-class map
    rather than adding to it, so emitting the flag with an empty file would
    throw away whatever classes the board carried.
    """
    for empty in ({}, None):
        args = _capture_route_argv(
            monkeypatch, tmp_path / str(empty), net_clearances=empty)
        assert "--net-clearances" not in args


def test_route_log_is_kept_when_asked_and_the_argv_is_unchanged(monkeypatch, tmp_path):
    """Phase 11: KRT's fine-pitch **rescue ladder** announces itself on
    ``route.py``'s stdout and nowhere else, and a rescue is what necks a net
    below the per-net clearance the spacing lever asked for. Keeping the log is
    how that mechanism gets re-derived instead of quoted.

    ⛔ The route itself must not change -- no new flag, same argv.
    """
    log = tmp_path / "logs" / "route_log.txt"
    args = _capture_route_argv(monkeypatch, tmp_path, route_log_path=str(log))

    assert log.is_file()
    assert "rescued a gap" not in log.read_text()      # the stub rescues nothing
    assert not any(str(a).startswith("--route-log") for a in args)
    assert "--net-clearances" not in args


def test_no_route_log_path_writes_nothing(monkeypatch, tmp_path):
    """Default OFF -> byte-identical to every call made before it existed."""
    _capture_route_argv(monkeypatch, tmp_path)

    assert not list((tmp_path / "work").glob("route_log*"))


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


# --------------------------------------------------------------------------
# Phase 4: the counts are token-bounded, and zones are tallied per net.
# --------------------------------------------------------------------------

# A pad's (zone_connect 2) is the substring that used to inflate zone_count by
# one per pad -- enough to mask power_copper's under-pour warning.
ZONE_CONNECT_BOARD = """
(kicad_pcb
  (footprint "R_0603"
    (pad "1" smd rect (at 0 0) (zone_connect 2) (net 3 "GND"))
    (pad "2" smd rect (at 1 0) (zone_connect 2) (net 4 "VOUT"))
  )
  (zone
    (net 3)
    (net_name "GND")
    (layer "B.Cu")
    (fill yes)
  )
  (zone
    (net 4)
    (net_name "VOUT")
    (layer "F.Cu")
    (fill yes)
  )
)
"""


def test_parse_zone_summary_ignores_zone_connect_pads():
    summary = krt._parse_zone_summary(ZONE_CONNECT_BOARD)
    # Two real zones; the two (zone_connect 2) pads must not count.
    assert summary["zone_count"] == 2
    assert ZONE_CONNECT_BOARD.count("(zone") == 4  # what the old count returned


def test_zone_counts_by_net():
    assert krt._zone_counts_by_net(ZONE_CONNECT_BOARD) == {"GND": 1, "VOUT": 1}
    assert krt._zone_counts_by_net("(kicad_pcb)") == {}
    # a zone with no net_name is tallied under "" rather than dropped silently
    assert krt._zone_counts_by_net("(zone (fill yes))") == {"": 1}


def test_zone_counts_by_net_multi_zone_same_net():
    text = (ZONE_CONNECT_BOARD
            + '(zone (net 3) (net_name "GND") (layer "F.Cu") (fill yes))\n')
    assert krt._zone_counts_by_net(text)["GND"] == 2


# --------------------------------------------------------------------------
# Power-layout Phase 16: the router layer flags
#
# ⛔ Both parameters default to ``None`` and emit NOTHING, so every call made
# before Phase 16 produces byte-identical argv. That is the contract gate F1
# rests on, and it is tested here rather than asserted in prose.
# --------------------------------------------------------------------------

def test_layers_none_emits_neither_flag(monkeypatch, tmp_path):
    args = _capture_route_argv(monkeypatch, tmp_path)
    assert "--layers" not in args
    assert "--layer-costs" not in args


def test_layers_emitted_as_space_separated_names(monkeypatch, tmp_path):
    args = _capture_route_argv(
        monkeypatch, tmp_path, layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    i = args.index("--layers")
    assert args[i + 1:i + 5] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert "--layer-costs" not in args


def test_layer_costs_emitted_in_the_same_order(monkeypatch, tmp_path):
    """⚠ ``route.py`` pairs ``--layer-costs`` with ``--layers`` BY POSITION, and
    ``-1`` is its FORBIDDEN sentinel."""
    args = _capture_route_argv(
        monkeypatch, tmp_path,
        layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        layer_costs=[1.0, -1, -1, 3.0])
    i = args.index("--layer-costs")
    assert args[i + 1:i + 5] == ["1", "-1", "-1", "3"]
    # order preserved and the two lists still adjacent-and-matched
    j = args.index("--layers")
    assert args[j + 1:j + 5] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_layer_costs_length_mismatch_raises(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="layer_costs"):
        _capture_route_argv(monkeypatch, tmp_path,
                            layers=["F.Cu", "B.Cu"], layer_costs=[1.0])


def test_layer_costs_without_layers_raises(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="layer_costs requires layers"):
        _capture_route_argv(monkeypatch, tmp_path, layer_costs=[1.0, 3.0])


def _capture_pour_argv(monkeypatch, tmp_path, **kwargs):
    """``route_planes.py``'s argv, against a stubbed KRT."""
    fake = _make_usable_krt(tmp_path / "krt_pour")
    captured = {}

    def fake_run(args, krt_dir, timeout_s):
        captured.setdefault("args", args)

        class P:
            stdout = 'JSON_SUMMARY: {"successful": 1, "failed": 0}'
            stderr = ""
            returncode = 0
        # ⚠ Only route_planes.py takes an output path; the checkers that run
        # after it are invoked with the board alone.
        if args[0] == "route_planes.py":
            with open(args[2], "w", encoding="utf-8") as fh:
                fh.write('(zone (net_name "GND") (fill yes))\n')
        return P()

    monkeypatch.setattr(krt, "_run_krt", fake_run)
    krt.pour_planes(str(tmp_path / "in.kicad_pcb"),
                    str(tmp_path / "out.kicad_pcb"),
                    nets=["GND"], plane_layers=["In1.Cu"],
                    workdir=str(tmp_path / "work"), krt_dir=str(fake), **kwargs)
    return captured["args"]


def test_pour_planes_layers_none_emits_no_flag(monkeypatch, tmp_path):
    args = _capture_pour_argv(monkeypatch, tmp_path)
    assert "--layers" not in args
    assert "--plane-layers" in args


def test_pour_planes_emits_layers_after_plane_layers(monkeypatch, tmp_path):
    """⛔ No cost list here: ``route_planes.py`` is what routes plane-net taps
    from pads DOWN to the plane layer, and forbidding that layer would
    disconnect every ground pad on the board."""
    args = _capture_pour_argv(monkeypatch, tmp_path,
                              layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert args.index("--layers") > args.index("--plane-layers")
    i = args.index("--layers")
    assert args[i + 1:i + 5] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert "--layer-costs" not in args
