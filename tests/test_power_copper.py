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
                   power_net_widths=None, out_path=None, route_extra_args=None,
                   **design_rules):
        cap["route_nets"] = nets
        cap["route_widths"] = power_net_widths
        cap["route_extra_args"] = route_extra_args
        cap["route_design_rules"] = design_rules
        # emit a routed board with a wide VIN track for the honesty check
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write('(net 1 "VIN_12V")\n'
                     '(segment (width 0.8) (layer "F.Cu") (net 1))\n')
        return RoutabilityFeedback(source="kicad_routing_tools")

    def fake_pour(pcb_path, out_path, nets, plane_layers, workdir, krt_dir=None,
                  timeout_s=900, add_gnd_vias=False, gnd_via_distance=2.0,
                  **kwargs):
        cap["pour_nets"] = nets
        cap["pour_layers"] = plane_layers
        cap["add_gnd_vias"] = add_gnd_vias
        cap["pour_kwargs"] = kwargs
        # One zone per requested net, named the way KiCad names them, so the
        # per-net read-back in emit_power_copper sees real data. A net listed in
        # cap["pour_empty"] is poured but written no zone (the failure mode the
        # promoted-but-silent warning exists for).
        with open(out_path, "w", encoding="utf-8") as fh:
            for net in nets:
                if net in cap.get("pour_empty", ()):
                    continue
                fh.write(f'(zone\n\t(net_name "{net}")\n\t(fill yes)\n)\n')
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
    # a congested 2-layer board gets the SKILL rip-up budget by default
    assert cap["route_extra_args"] == ["--max-ripup", "10", "--max-iterations", "1000000"]


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


# --------------------------------------------------------------------------- #
# Phase 4: the pour policy, suffixed recognition, layer assignment and the
# zone_clearance / min_thickness pass-through. All default OFF.
# --------------------------------------------------------------------------- #
class _Net:
    def __init__(self, name, pins=()):
        self.name = name
        self._pins = list(pins)

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, net):
        self.part = part
        self.net = net
        self.func = None
        net._pins.append(self)


class _Part:
    def __init__(self, ref, nets):
        self.ref = ref
        self.value = ""
        self.name = ""
        self.footprint = ""
        self.pins = [_Pin(self, n) for n in nets]


class _Circuit:
    """A boost-shaped netlist: GND everywhere, VOUT on six refs, VIN on five."""

    def __init__(self):
        self.gnd = _Net("GND")
        self.vout = _Net("VOUT")
        self.vin_12v = _Net("VIN_12V")
        self.sw = _Net("SW")
        self.parts = []
        for ref, nets in (
            ("U1", [self.gnd, self.vin_12v, self.sw]),
            ("D1", [self.sw, self.vout]),
            ("C1", [self.vout, self.gnd]),
            ("C2", [self.vout, self.gnd]),
            ("C3", [self.vout, self.gnd]),
            ("C4", [self.vout, self.gnd]),
            ("R1", [self.vout, self.gnd]),
            ("C5", [self.vin_12v, self.gnd]),
            ("C6", [self.vin_12v, self.gnd]),
            ("L1", [self.vin_12v, self.sw]),
            ("J1", [self.vin_12v, self.gnd]),
        ):
            self.parts.append(_Part(ref, nets))

    def get_nets(self):
        return [self.gnd, self.vout, self.vin_12v, self.sw]


def _placed(circuit):
    from skidl_layout.writer import PlacedPart

    return [PlacedPart(p.ref, float(i * 3), 0.0, 0.0, "")
            for i, p in enumerate(circuit.parts)]


def _result_for(circuit):
    from skidl_layout.power import plan_power_routes

    placed = _placed(circuit)
    return _FakeResult(plan_power_routes(circuit, placed), placed_parts=placed)


def test_knobs_off_reads_the_default_plan(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    out = emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2)
    # Only GND pours; VOUT is a trunk, VIN_12V is invisible to the plan today.
    assert cap["pour_nets"] == ["GND"]
    assert cap["pour_layers"] == ["B.Cu"]
    assert out.promoted_nets == []
    assert "VIN_12V" not in cap["route_widths"]
    # no zone_clearance / min_thickness flags reach KRT
    assert cap["pour_kwargs"] == {}


def test_pour_policy_auto_promotes_supplies_to_fcu(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    out = emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                            pour_policy="auto")
    # VOUT has 6 placed refs -> promoted. GND keeps the back copper; the
    # promoted supply pours on the front, which is Figure 11's shape.
    assert set(cap["pour_nets"]) == {"GND", "VOUT"}
    layers = dict(zip(cap["pour_nets"], cap["pour_layers"]))
    assert layers == {"GND": "B.Cu", "VOUT": "F.Cu"}
    assert out.promoted_nets == ["VOUT"]
    # A promoted net keeps its trunk (route_promoted default True), so only the
    # ground plane is excluded from routing; the pour is additive.
    assert set(cap["route_nets"]) == {"*", "!GND"}
    assert cap["route_widths"]["VOUT"] == 0.3
    assert out.zones_by_net == {"GND": 1, "VOUT": 1}
    assert "promoted VOUT: pour on F.Cu (1 zone(s))" in out.summary()


def test_route_promoted_false_excludes_the_promoted_net(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    out = emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                            pour_policy="auto", route_promoted=False)
    assert set(cap["route_nets"]) == {"*", "!GND", "!VOUT"}
    assert cap["route_widths"] is None  # nothing left to carry as a wide trace
    assert out.promoted_nets == ["VOUT"]


def test_include_suffixed_reaches_the_copper(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                      include_suffixed=True)
    # VIN_12V now carries its high-current width instead of routing at signal
    assert cap["route_widths"]["VIN_12V"] == 0.8
    assert cap["pour_nets"] == ["GND"]


def test_include_suffixed_plus_auto_promotes_the_suffixed_rail(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    out = emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                            include_suffixed=True, pour_policy="auto")
    assert set(out.promoted_nets) == {"VOUT", "VIN_12V"}  # 6 and 5 refs
    assert set(cap["pour_nets"]) == {"GND", "VOUT", "VIN_12V"}
    layers = dict(zip(cap["pour_nets"], cap["pour_layers"]))
    assert layers["GND"] == "B.Cu"
    assert layers["VOUT"] == layers["VIN_12V"] == "F.Cu"  # shared, Voronoi-split
    # both promoted nets keep their trunks under the routed pour
    assert cap["route_widths"] == {"VOUT": 0.3, "VIN_12V": 0.8}


def test_supply_pour_layer_is_the_bailout_fallback(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                      pour_policy="auto", supply_pour_layer="B.Cu")
    assert dict(zip(cap["pour_nets"], cap["pour_layers"])) == {
        "GND": "B.Cu", "VOUT": "B.Cu"}


def test_override_still_beats_a_promotion(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    out = emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                            pour_policy="auto", overrides={"VOUT": 1.5})
    assert cap["pour_nets"] == ["GND"]
    assert out.promoted_nets == []
    assert cap["route_widths"]["VOUT"] == 1.5
    assert any("demoted" in w for w in out.warnings)


def test_promotion_that_pours_nothing_warns(patched):
    cap, tmp_path = patched
    cap["pour_empty"] = {"VOUT"}
    circuit = _Circuit()
    result = _result_for(circuit)
    out = emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                            pour_policy="auto")
    assert any("VOUT: promoted to a pour but no zone was written" in w
               for w in out.warnings)
    assert out.zones_by_net == {"GND": 1}


def test_zone_clearance_and_min_thickness_pass_through(patched):
    cap, tmp_path = patched
    circuit = _Circuit()
    result = _result_for(circuit)
    emit_power_copper(result, circuit, [], str(tmp_path), board_layers=2,
                      zone_clearance=0.3, min_thickness=0.2)
    assert cap["pour_kwargs"] == {"zone_clearance": 0.3, "min_thickness": 0.2}


def test_plane_layer_for_promoted():
    intent = _intent("VOUT", "pour", 0.3, "F.Cu")
    assert _plane_layer_for(intent, 2) == "B.Cu"
    assert _plane_layer_for(intent, 2, promoted=True) == "F.Cu"
    assert _plane_layer_for(intent, 2, promoted=True,
                            supply_pour_layer="B.Cu") == "B.Cu"
    # 4+ layers still honour the plan's own layer, promoted or not
    inner = _intent("VOUT", "plane", 0.3, "In2.Cu")
    assert _plane_layer_for(inner, 4, promoted=True) == "In2.Cu"


# --------------------------------------------------------------------------
# Thermal vias under the exposed pad (power-layout Phase 5, WS-3)
# --------------------------------------------------------------------------

import dataclasses  # noqa: E402

from skidl_layout import OSHPARK_2L  # noqa: E402

_VIA_IN_PAD = dataclasses.replace(OSHPARK_2L, via_in_pad=True, name="test-vip")


@dataclass
class _FakeStage:
    controller_ref: str = "U1"
    ground_net: str = "GND"


@dataclass
class _FakeStagePlan:
    stages: list = field(default_factory=lambda: [_FakeStage()])


def _thermal_result():
    result = _FakeResult(_plan(_intent("GND", "pour", 0.3, "F.Cu")))
    result.power_stage_plan = _FakeStagePlan()
    return result


def test_thermal_vias_default_off_leaves_the_board_alone(patched):
    cap, tmp_path = patched
    out = emit_power_copper(_thermal_result(), object(), [], str(tmp_path),
                            board_layers=2, fab_spec=_VIA_IN_PAD)
    assert out.thermal_vias is None
    assert "thermal" not in out.summary().lower()
    assert "thermal_vias" in out.to_dict()
    assert out.to_dict()["thermal_vias"] is None


def test_thermal_vias_refuse_on_the_shipped_spec(patched, monkeypatch):
    cap, tmp_path = patched
    calls = []
    monkeypatch.setattr(
        power_copper, "_apply_thermal_vias",
        lambda *a, **k: calls.append(a) or (a[0], a[4], None, []))
    emit_power_copper(_thermal_result(), object(), [], str(tmp_path),
                      board_layers=2, fab_spec="oshpark-2l", thermal_vias=True)
    # The spec the post-process is handed is the resolved one, so its
    # via_in_pad=False is what drives the refusal.
    assert calls and calls[0][3] is OSHPARK_2L


def test_thermal_vias_refusal_is_recorded_not_silent(patched, monkeypatch):
    cap, tmp_path = patched

    def fake_plan(pcb, controller, ground, spec, **kw):
        from skidl_layout.copper_post import ThermalViaPlan
        return ThermalViaPlan(net=ground, refused=True,
                              reason="spec 'oshpark-2l' declares via_in_pad=False")

    monkeypatch.setattr("skidl_layout.copper_post.plan_thermal_vias", fake_plan)
    out = emit_power_copper(_thermal_result(), object(), [], str(tmp_path),
                            board_layers=2, fab_spec="oshpark-2l",
                            thermal_vias=True)
    assert out.thermal_vias["refused"] is True
    assert out.thermal_vias["count"] == 0
    assert any("thermal vias not emitted" in w for w in out.warnings)
    assert "REFUSED" in out.summary()


def test_thermal_vias_without_a_power_stage_warns(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("GND", "pour", 0.3, "F.Cu")))
    result.power_stage_plan = None
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            fab_spec=_VIA_IN_PAD, thermal_vias=True)
    assert out.thermal_vias is None
    assert any("no power stage" in w for w in out.warnings)


def test_thermal_vias_accepted_when_drc_does_not_worsen(patched, monkeypatch):
    cap, tmp_path = patched
    from skidl_layout.copper_post import ThermalViaPlan

    plan = ThermalViaPlan(net="GND", shape=(2, 2), pitch_mm=0.85,
                          positions=[(0, 0), (1, 0), (0, 1), (1, 1)],
                          pad={"ref": "U1", "number": "11", "w": 1.68, "h": 1.88},
                          reason="2x2 array")
    monkeypatch.setattr("skidl_layout.copper_post.plan_thermal_vias",
                        lambda *a, **k: plan)
    spliced = {}

    def fake_splice(src, dst, p):
        spliced["dst"] = dst
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write("(spliced)\n")
        return len(p.positions)

    monkeypatch.setattr("skidl_layout.copper_post.splice_vias", fake_splice)
    out = emit_power_copper(_thermal_result(), object(), [], str(tmp_path),
                            board_layers=2, fab_spec=_VIA_IN_PAD,
                            thermal_vias=True)
    assert out.thermal_vias["count"] == 4
    # The board that ships is the spliced one -- and it is what got graded.
    assert out.routed_pcb_path == spliced["dst"]
    assert cap["checked"] == spliced["dst"]


def test_thermal_vias_are_dropped_when_every_size_costs_drc(patched, monkeypatch):
    cap, tmp_path = patched
    from skidl_layout.copper_post import ThermalViaPlan

    def fake_plan(pcb, controller, ground, spec, pitch_mm=None,
                  edge_margin_mm=0.0, max_shape=None):
        cols, rows = max_shape or (2, 2)
        return ThermalViaPlan(net=ground, shape=(cols, rows), pitch_mm=0.85,
                              positions=[(0, 0)] * (cols * rows), reason="a")

    monkeypatch.setattr("skidl_layout.copper_post.plan_thermal_vias", fake_plan)
    monkeypatch.setattr("skidl_layout.copper_post.splice_vias",
                        lambda src, dst, p: open(dst, "w").write("(x)") and 0)

    dirty = RoutabilityFeedback(total_nets=3, unrouted_count=0,
                                drc_violation_count=7, source="krt")
    clean = RoutabilityFeedback(total_nets=3, unrouted_count=0, source="krt")
    seen = {"n": 0}

    def fake_check(pcb_path, krt_dir=None, timeout_s=900):
        seen["n"] += 1
        cap["checked"] = pcb_path
        return clean if seen["n"] == 1 else dirty

    monkeypatch.setattr(power_copper.krt, "check_board", fake_check)
    out = emit_power_copper(_thermal_result(), object(), [], str(tmp_path),
                            board_layers=2, fab_spec=_VIA_IN_PAD,
                            thermal_vias=True)
    # A board with a DRC violation is never shipped in exchange for vias.
    assert out.thermal_vias["count"] == 0
    assert out.feedback.drc_violation_count == 0
    assert any("dropped entirely" in w for w in out.warnings)
    assert out.routed_pcb_path.endswith("power_copper.kicad_pcb")


def test_shrink_ladder_walks_down_to_one_via():
    ladder = power_copper._shrink_ladder(2, 2)
    assert ladder[0] == (2, 2)
    assert ladder[-1] == (1, 1)
    assert all(c >= 1 and r >= 1 for c, r in ladder)
    assert power_copper._shrink_ladder(1, 1) == [(1, 1)]
