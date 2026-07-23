"""Unit tests for the emit_power_copper bridge (skidl_layout.power_copper).

These monkeypatch the KRT subprocess layer (route_and_check / pour_planes /
check_board) and the board writer, so they exercise the plan -> KRT mapping
(net selection, widths, plane layers, overrides, honesty warnings) without any
routing. The real end-to-end route+pour is the WS-B4 avalanche report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from skidl_layout import power_copper
from skidl_layout.power import PowerRouteIntent, PowerRoutePlan
from skidl_layout.power_copper import PowerCopperResult, emit_power_copper, _plane_layer_for
from skidl_layout.routability import RoutabilityFeedback


@dataclass
class _FakeResult:
    power_plan: PowerRoutePlan
    placed_parts: list = field(default_factory=list)
    outline: object = None
    cutouts: object = None
    routability: object = None


def _intent(net, strategy, width, layer):
    return PowerRouteIntent(
        net_name=net, strategy=strategy, layer=layer, width_mm=width, priority=90
    )


def _plan(*intents):
    return PowerRoutePlan(route_intents=list(intents))


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Patch KRT + writer; capture the args emit_power_copper passes."""
    cap = {}

    monkeypatch.setattr(power_copper, "krt", power_copper.krt)

    def fake_write(placed, circuit, fp_lib_dirs, out, **kw):
        with open(out, "w", encoding="utf-8") as fh:
            fh.write('(net 0 "")\n')

    def fake_route(pcb_path, workdir, krt_dir=None, nets=None, timeout_s=900,
                   power_net_widths=None, out_path=None):
        cap["route_nets"] = nets
        cap["route_widths"] = power_net_widths
        # emit a routed board with a wide VIN track for the honesty check
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write('(net 1 "VIN_12V")\n'
                     '(segment (width 0.8) (layer "F.Cu") (net 1))\n')
        return RoutabilityFeedback(source="kicad_routing_tools")

    def fake_pour(pcb_path, out_path, nets, plane_layers, workdir, krt_dir=None,
                  timeout_s=900, add_gnd_vias=False, gnd_via_distance=2.0):
        cap["pour_nets"] = nets
        cap["pour_layers"] = plane_layers
        cap["add_gnd_vias"] = add_gnd_vias
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("(zone (fill yes))\n")
        return {"zone_count": len(nets), "filled_polygon_count": len(nets),
                "connected_ok": True, "unrouted_nets": [], "broken_nets": [],
                "drc_violation_count": 0, "via_count": 0, "segment_count": 1}

    def fake_check(pcb_path, krt_dir=None, timeout_s=900):
        cap["checked"] = pcb_path
        return RoutabilityFeedback(total_nets=3, unrouted_count=0,
                                   source="kicad_routing_tools")

    from skidl_layout import writer as writer_mod
    monkeypatch.setattr(writer_mod, "write_kicad_pcb", fake_write)
    monkeypatch.setattr(power_copper.krt, "route_and_check", fake_route)
    monkeypatch.setattr(power_copper.krt, "pour_planes", fake_pour)
    monkeypatch.setattr(power_copper.krt, "check_board", fake_check)
    return cap, tmp_path


def test_partition_2layer(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "pour", 0.3, "F.Cu"),
        _intent("VIN_12V", "wide_trunk", 0.8, "F.Cu"),
        _intent("V5", "trunk", 0.4, "F.Cu"),
        _intent("SDA", "fanout_only", 0.25, "F.Cu"),
    ))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2)
    # GND poured (excluded from route), on B.Cu for a 2-layer board.
    assert cap["route_nets"] == ["*", "!GND"]
    assert cap["pour_nets"] == ["GND"]
    assert cap["pour_layers"] == ["B.Cu"]
    # wide/trunk nets carried as power widths; fanout_only is not.
    assert cap["route_widths"] == {"VIN_12V": 0.8, "V5": 0.4}
    assert "SDA" not in cap["route_widths"]
    assert result.routability is out.feedback
    assert out.feedback.total_nets == 3


def test_partition_4layer_honors_suggested_layer(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "plane", 0.3, "In1.Cu"),
        _intent("V5", "internal_rail", 0.5, "In2.Cu"),
    ))
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=4)
    assert cap["pour_nets"] == ["GND"]
    assert cap["pour_layers"] == ["In1.Cu"]  # honoured, not forced to B.Cu
    assert cap["route_widths"] == {"V5": 0.5}


def test_override_wins_and_demotes_plane(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "pour", 0.3, "F.Cu"),
        _intent("VIN_12V", "wide_trunk", 0.8, "F.Cu"),
    ))
    out = emit_power_copper(
        result, object(), [], str(tmp_path), board_layers=2,
        overrides={"VIN_12V": 1.2, "GND": 0.6},
    )
    # VIN width overridden; GND demoted from plane to a 0.6mm wide trace, which
    # empties the plane set -> the pour step is skipped entirely.
    assert cap["route_widths"] == {"VIN_12V": 1.2, "GND": 0.6}
    assert "pour_nets" not in cap
    assert cap["route_nets"] == ["*"]  # no plane nets left to exclude
    assert any("demoted" in w for w in out.warnings)


def test_honesty_warning_when_no_track_emitted(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("V5", "wide_trunk", 0.8, "F.Cu"),  # fake router emits VIN, not V5
    ))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2)
    assert any("no track emitted" in w for w in out.warnings)


def test_no_plane_nets_skips_pour(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("VIN_12V", "wide_trunk", 0.8, "F.Cu"),
    ))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2)
    assert "pour_nets" not in cap  # pour_planes never called
    assert out.plane_nets == []
    assert out.routed_pcb_path.endswith("routed_power.kicad_pcb")


def test_plane_layer_for_helper():
    intent = _intent("GND", "pour", 0.3, "In1.Cu")
    assert _plane_layer_for(intent, 2) == "B.Cu"
    assert _plane_layer_for(intent, 4) == "In1.Cu"


def test_result_summary_and_to_dict():
    fb = RoutabilityFeedback(total_nets=2, source="kicad_routing_tools")
    res = PowerCopperResult(
        routed_pcb_path="/x/power_copper.kicad_pcb",
        plane_nets=["GND"], plane_layers=["B.Cu"],
        width_map={"VIN_12V": 0.8}, emitted_widths={"VIN_12V": 0.8},
        plane_summary={"zone_count": 1}, feedback=fb,
    )
    text = res.summary()
    assert "wide VIN_12V: planned 0.80mm -> 0.80mm" in text
    assert "plane GND: pour on B.Cu" in text
    d = res.to_dict()
    assert d["plane_nets"] == ["GND"]
    assert d["feedback"]["total_nets"] == 2
