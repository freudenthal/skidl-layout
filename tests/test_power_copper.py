"""Unit tests for the emit_power_copper bridge (skidl_layout.power_copper).

These monkeypatch the KRT subprocess layer (route_and_check / pour_planes /
check_board) and the board writer, so they exercise the plan -> KRT mapping
(net selection, widths, plane layers, overrides, honesty warnings) without any
routing. The real end-to-end route+pour is the WS-B4 avalanche report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import os

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
        # Phase 10's lever arrives here; captured separately from the design
        # rules so a test can assert "no map was passed at all", which is the
        # byte-identical claim, rather than "the map was empty".
        cap["route_net_clearances"] = design_rules.pop("net_clearances", "absent")
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


# --------------------------------------------------------------------------- #
# Phase 6: the current merge. Currents are DATA -- these tests hand
# emit_power_copper a plain dict, exactly as a human or the skidl-eda producer
# would. Nothing here simulates anything.
# --------------------------------------------------------------------------- #
def test_currents_default_off_is_byte_identical(patched):
    """No dict -> the Phase-4/5 path verbatim, and an empty record."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "pour", 0.3, "F.Cu"),
        _intent("VIN_12V", "wide_trunk", 0.8, "F.Cu"),
    ))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2)
    assert cap["route_widths"] == {"VIN_12V": 0.8}
    assert out.current_widths == {}
    assert out.to_dict()["current_widths"] == {}


def test_current_widens_a_planned_net(patched):
    """2 A on a 0.3mm-planned net -> the IPC 0.781mm reaches the router."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VOUT", "trunk", 0.3, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"VOUT": 2.0})
    assert cap["route_widths"]["VOUT"] == pytest.approx(0.781, abs=0.01)
    row = out.current_widths["VOUT"]
    assert row["applied"] is True
    assert row["i_rms_a"] == 2.0
    assert row["planned_width_mm"] == 0.3
    assert row["ipc_width_mm"] == pytest.approx(0.781, abs=0.01)


def test_a_current_may_only_widen_never_narrow(patched):
    """A smaller IPC width than the plan's is ignored -- the floor stands."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN_12V", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"VIN_12V": 0.5})
    assert cap["route_widths"] == {"VIN_12V": 0.8}
    row = out.current_widths["VIN_12V"]
    assert row["applied_width_mm"] == 0.8
    assert "may only widen" in row["reason"]


def test_a_current_without_a_plan_entry_is_recorded_not_widened(patched):
    """The SW node's case: measured, deliberately left at signal width."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN_12V", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"SW": 4.44})
    assert "SW" not in cap["route_widths"]
    row = out.current_widths["SW"]
    assert row["applied"] is False
    assert row["applied_width_mm"] is None
    assert row["ipc_width_mm"] == pytest.approx(2.35, abs=0.01)
    assert "not in the power plan" in row["reason"]


def test_a_poured_net_is_recorded_not_widened(patched):
    """GND owns a layer; a trunk width for it is meaningless."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("GND", "pour", 0.3, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"GND": 4.4})
    assert cap["route_widths"] is None
    assert out.current_widths["GND"]["applied"] is False


def test_cap_warns_with_both_numbers(patched):
    """A silent clamp is a lie with units -- bail-out 2's honesty requirement."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN_12V", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"VIN_12V": 4.44},
                            current_max_width_mm=1.2)
    assert cap["route_widths"]["VIN_12V"] == pytest.approx(1.2)
    warning = next(w for w in out.warnings if "caps it" in w)
    assert "2.35mm" in warning and "1.20mm" in warning
    row = out.current_widths["VIN_12V"]
    assert row["capped"] is True
    assert row["ipc_width_mm"] == pytest.approx(2.35, abs=0.01)
    assert row["applied_width_mm"] == pytest.approx(1.2)


def test_human_override_still_beats_the_simulator(patched):
    """Phase 4's veto rule, unchanged: the human wins and the record says so."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN_12V", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"VIN_12V": 4.44},
                            overrides={"VIN_12V": 0.6})
    assert cap["route_widths"] == {"VIN_12V": 0.6}
    row = out.current_widths["VIN_12V"]
    assert row["overridden"] is True
    assert row["applied_width_mm"] == pytest.approx(0.6)
    assert any("the human veto wins" in w for w in out.warnings)


def test_delta_t_is_honoured(patched):
    """A hotter allowed rise buys a narrower track."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VOUT", "trunk", 0.3, "F.Cu")))
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                      net_currents={"VOUT": 2.0}, current_delta_t_c=20.0)
    hot = cap["route_widths"]["VOUT"]
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                      net_currents={"VOUT": 2.0}, current_delta_t_c=10.0)
    assert hot < cap["route_widths"]["VOUT"]


def test_spec_copper_weight_reaches_the_sizing(patched):
    """2 oz copper halves the width the same current asks for."""
    import dataclasses

    import skidl_layout as SL

    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VOUT", "trunk", 0.3, "F.Cu")))
    heavy = dataclasses.replace(SL.OSHPARK_2L, copper_weight_oz=2.0,
                                name="2oz(test)")
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                      fab_spec=heavy, net_currents={"VOUT": 2.0})
    assert cap["route_widths"]["VOUT"] == pytest.approx(0.781 / 2, abs=0.01)


def test_high_voltage_creepage_warning_is_report_only(patched):
    """The loud path: nothing on the boost twin exceeds 30V, so unit-test it."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("HV_RAIL", "wide_trunk", 0.8, "F.Cu"),
        _intent("GND", "pour", 0.3, "F.Cu"),
    ))
    before = emit_power_copper(result, object(), [], str(tmp_path),
                               board_layers=2, net_currents={"HV_RAIL": 0.5})
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"HV_RAIL": 0.5},
                            net_voltages={"HV_RAIL": 400.0, "GND": 0.0})
    warning = next(w for w in out.warnings if "creepage" in w)
    assert "400.0V" in warning
    # report-only: the copper is identical either way
    assert out.width_map == before.width_map


def test_creepage_warning_is_quiet_below_the_threshold(patched):
    """The boost twin's own case: ~24V says nothing."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VOUT", "trunk", 0.3, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"VOUT": 2.0},
                            net_voltages={"VOUT": 23.9})
    assert not any("creepage" in w for w in out.warnings)


def test_current_widths_reach_summary_and_to_dict(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VOUT", "trunk", 0.3, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"VOUT": 2.0, "SW": 4.44})
    text = out.summary()
    assert "current VOUT: 2.000A -> IPC 0.78mm, applied" in text
    assert "current SW: 4.440A -> IPC 2.35mm, recorded only" in text
    assert set(out.to_dict()["current_widths"]) == {"VOUT", "SW"}


def test_zero_current_does_not_widen_or_appear(patched):
    """Absent != zero (Phase 5's rule) -- a 0A net is simply not sized."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VOUT", "trunk", 0.3, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_currents={"VOUT": 0.0})
    assert cap["route_widths"] == {"VOUT": 0.3}
    assert out.current_widths == {}


# --------------------------------------------------------------------------- #
# Phase 10 -- the spacing lever at the same seam
# --------------------------------------------------------------------------- #

def test_voltage_spacing_default_off_passes_no_map_at_all(patched):
    """⛔ S4's mechanism: the knob off means the flag never exists.

    Not "an empty map" -- ``None``. An explicit ``--net-clearances`` REPLACES
    KRT's auto-read net-class map, so the difference matters on a board that has
    real classes.
    """
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            net_voltages={"VIN": 72.0})
    assert cap["route_net_clearances"] is None
    assert out.net_clearances == {}
    assert out.to_dict()["net_clearances"] == {}


def test_voltage_spacing_widens_an_hv_net_to_the_table_value(patched):
    """72 V -> Table 6-1 column B2 asks 0.6 mm, above the spec's 0.25 mm."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            fab_spec="oshpark-2l",
                            net_voltages={"VIN": 72.0, "VOUT": 12.0},
                            voltage_spacing=True)
    assert cap["route_net_clearances"] == {"VIN": 0.6}
    assert out.net_clearances["VIN"]["applied"] is True
    assert out.net_clearances["VOUT"]["applied"] is False
    assert any("VIN: routing at 0.6mm clearance" in w for w in out.warnings)


def test_voltage_spacing_on_an_lv_board_says_so_and_passes_nothing(patched):
    """S4's other half: knob ON, no net above the cliff -> still no flag.

    And it must say so rather than pass silently -- "I did nothing" and "I was
    switched off" are different facts about a run.
    """
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            fab_spec="oshpark-2l",
                            net_voltages={"VIN": 24.0, "VOUT": 12.0},
                            voltage_spacing=True)
    assert cap["route_net_clearances"] is None
    assert any("no net needs widening" in w for w in out.warnings)
    # Graded, though: the records exist and say why each net was left alone.
    assert set(out.net_clearances) == {"VIN", "VOUT"}
    assert all(r["reason"] for r in out.net_clearances.values())


def test_the_lever_never_narrows_a_net_below_the_board_clearance(patched):
    """A 24 V net's 0.1 mm requirement must not become a 0.1 mm net class."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu")))
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                      fab_spec="oshpark-2l", net_voltages={"VIN": 24.0},
                      voltage_spacing=True)
    assert cap["route_net_clearances"] is None


def test_spacing_column_is_selectable_and_changes_the_answer(patched):
    """B4 (permanent conformal coat) asks 0.13 mm where B2 asks 0.6 mm at 72 V.

    ⚠ B2 stays the default because soldermask is not a conformal coat -- but a
    board that genuinely is coated should be able to say so.
    """
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            fab_spec="oshpark-2l", net_voltages={"VIN": 72.0},
                            voltage_spacing=True, spacing_column="B4")
    # 0.13mm is BELOW the board's own 0.25mm, so B4 asks for no widening at all.
    assert cap["route_net_clearances"] is None
    assert out.net_clearances["VIN"]["required_mm"] == 0.13
    assert out.net_clearances["VIN"]["column"] == "B4"


def test_net_clearances_reach_summary_and_to_dict(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu")))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            fab_spec="oshpark-2l",
                            net_voltages={"VIN": 72.0, "VOUT": 12.0},
                            voltage_spacing=True)
    text = out.summary()
    assert "spacing VIN: 72V -> Table 6-1 0.60mm, routed at 0.60mm" in text
    assert "spacing VOUT: 12V -> Table 6-1 0.10mm (not widened)" in text
    assert set(out.to_dict()["net_clearances"]) == {"VIN", "VOUT"}


# --------------------------------------------------------------------------- #
# Phase 14, WS-14.0 -- pinning every power-carrying net in the width map.
#
# The defect these cover, measured in Phase 12: a switch node's route intent is
# ``fanout_only``, so ``SW`` never entered ``width_map``, so it was never passed
# to ``route.py --power-nets``, so a global ``--track-width`` narrowed it
# **0.300 -> 0.1524 mm on lt3757_sepic with DRC still 0** -- because DRC does
# not check current.
#
# The partition is pure logic and is tested as such: no board, no router.
# --------------------------------------------------------------------------- #

from skidl_layout.power import PowerNet  # noqa: E402
from skidl_layout.power_copper import plan_pinned_power_widths  # noqa: E402
from skidl_layout.power_roles import (  # noqa: E402
    CommutationLoop, PowerStage, PowerStagePlan,
)


def _spec():
    from skidl_layout.fabspec import resolve_fab_spec

    return resolve_fab_spec("oshpark-2l")


def _stage(**kw):
    base = dict(
        controller_ref="U1", topology="boost", switch_node_nets=["SW"],
        input_rail="VIN", output_rail="VOUT", ground_net="GND", devices=[],
        loops=[], feedback_divider=None, sense_resistor_ref=None,
        ground_nets=["GND"],
    )
    base.update(kw)
    return PowerStage(**base)


def _plan_with_nets(*nets):
    plan = PowerRoutePlan(route_intents=[])
    plan.nets = list(nets)
    return plan


def test_pin_adds_the_switch_node_and_nothing_else():
    """The headline case: SW is named by the classifier, absent from the map."""
    plan = _plan_with_nets(
        PowerNet(name="VIN", kind="supply", suggested_width_mm=0.8),
        PowerNet(name="GND", kind="ground", suggested_width_mm=0.3),
    )
    stage_plan = PowerStagePlan(stages=[_stage()])
    pinned = plan_pinned_power_widths(
        plan, stage_plan,
        spec=_spec(),
        width_map={"VIN": 0.8},          # VIN already wide; GND is poured
        plane_nets=["GND"], routed_plane_nets=[],
    )
    assert set(pinned) == {"SW", "VOUT"}
    assert pinned["SW"]["source"] == "stage:switch_node"
    # No plan width for a switch node -> the fab's own routing width, which is
    # EXACTLY what the net already gets. Pinning must not change the width.
    assert pinned["SW"]["width_mm"] == 0.3
    assert pinned["SW"]["from_plan_width"] is False


def test_pin_never_overrides_a_width_the_plan_or_sim_already_set():
    plan = _plan_with_nets(PowerNet(name="VIN", kind="supply",
                                    suggested_width_mm=0.8))
    pinned = plan_pinned_power_widths(
        plan, PowerStagePlan(stages=[]),
        spec=_spec(), width_map={"VIN": 2.131},   # the current-sized width
    )
    assert "VIN" not in pinned


def test_pin_skips_a_poured_plane_net_but_keeps_a_routed_promoted_one():
    """A poured net gets no tracks, so pinning it would only manufacture a
    'planned ... but no track emitted' warning on every board."""
    plan = _plan_with_nets(
        PowerNet(name="GND", kind="ground", suggested_width_mm=0.3),
        PowerNet(name="VOUT", kind="supply", suggested_width_mm=0.5),
    )
    pinned = plan_pinned_power_widths(
        plan, PowerStagePlan(stages=[]), spec=_spec(), width_map={},
        plane_nets=["GND", "VOUT"], routed_plane_nets=["VOUT"],
    )
    assert "GND" not in pinned
    assert pinned["VOUT"]["width_mm"] == 0.5


def test_pin_picks_up_the_commutation_loop_sense_node():
    """``ISNS`` sits in the loop between the switch and ground and carries the
    full switch current, but matches no name-based rule."""
    stage = _stage(loops=[CommutationLoop(
        member_refs=["CIN", "L1", "M1", "RS"],
        net_names=["VIN", "SW", "ISNS"], returns_through="GND")])
    pinned = plan_pinned_power_widths(
        _plan_with_nets(), PowerStagePlan(stages=[stage]),
        spec=_spec(), width_map={"VIN": 0.8}, plane_nets=["GND"],
    )
    assert pinned["ISNS"]["source"] == "stage:commutation_loop"
    assert "GND" not in pinned          # the loop returns through a poured net


def test_pin_floors_a_thin_plan_width_to_the_fab_minimum():
    plan = _plan_with_nets(PowerNet(name="VAUX", kind="supply",
                                    suggested_width_mm=0.05))
    pinned = plan_pinned_power_widths(
        plan, PowerStagePlan(stages=[]), spec=_spec(), width_map={})
    assert pinned["VAUX"]["width_mm"] == 0.1524
    assert pinned["VAUX"]["from_plan_width"] is True


def test_pin_skips_a_net_it_cannot_name_a_width_for():
    """No plan width and no fab spec -> KRT's own default applies and we cannot
    name it. Skipped rather than guessed at."""
    pinned = plan_pinned_power_widths(
        _plan_with_nets(), PowerStagePlan(stages=[_stage()]),
        spec=None, width_map={})
    assert "SW" not in pinned


def test_pin_is_deterministic_in_plan_then_stage_order():
    plan = _plan_with_nets(
        PowerNet(name="VCC", kind="supply", suggested_width_mm=0.4),
        PowerNet(name="VBIAS", kind="supply", suggested_width_mm=0.4),
    )
    stage_plan = PowerStagePlan(stages=[_stage()])
    first = plan_pinned_power_widths(plan, stage_plan, spec=_spec(), width_map={})
    second = plan_pinned_power_widths(plan, stage_plan, spec=_spec(), width_map={})
    assert list(first) == list(second)
    assert list(first)[:2] == ["VCC", "VBIAS"]


def test_pin_flag_off_is_byte_identical_argv(patched):
    """The arc's structural rule: the flag off emits exactly today's argv."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "pour", 0.3, "F.Cu"),
        _intent("VIN", "wide_trunk", 0.8, "F.Cu"),
    ))
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=2)
    off_widths, off_nets = cap["route_widths"], cap["route_nets"]
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2)
    assert cap["route_widths"] == off_widths
    assert cap["route_nets"] == off_nets
    assert out.pinned_widths == {}
    assert out.to_dict()["pinned_widths"] == {}


def test_pin_flag_on_reaches_the_router_and_the_result(patched):
    cap, tmp_path = patched
    plan = _plan(_intent("GND", "pour", 0.3, "F.Cu"),
                 _intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    plan.nets = [PowerNet(name="VIN", kind="supply", suggested_width_mm=0.8),
                 PowerNet(name="GND", kind="ground", suggested_width_mm=0.3)]
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[_stage()])
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            fab_spec="oshpark-2l", pin_power_widths=True)
    # SW now reaches route.py --power-nets; GND (poured) still does not.
    assert cap["route_widths"]["SW"] == 0.3
    assert "GND" not in cap["route_widths"]
    assert out.pinned_widths["SW"]["source"] == "stage:switch_node"
    assert any("SW: pinned at 0.3mm" in w for w in out.warnings)


def test_pin_says_so_when_it_had_nothing_to_add(patched):
    """A knob that silently did nothing reads as a knob that worked."""
    cap, tmp_path = patched
    plan = _plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    plan.nets = [PowerNet(name="VIN", kind="supply", suggested_width_mm=0.8)]
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[])
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            fab_spec="oshpark-2l", pin_power_widths=True)
    assert out.pinned_widths == {}
    assert any("no net was added" in w for w in out.warnings)


def test_human_override_still_beats_the_pin(patched):
    """The veto order the arc has kept since Phase 6."""
    cap, tmp_path = patched
    plan = _plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    plan.nets = [PowerNet(name="VIN", kind="supply", suggested_width_mm=0.8)]
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[_stage()])
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                      fab_spec="oshpark-2l", pin_power_widths=True,
                      overrides={"SW": 1.25})
    assert cap["route_widths"]["SW"] == 1.25


# --------------------------------------------------------------------------- #
# Phase 14, WS-14.1 -- two-pass, power-loop-first routing.
#
# The partition is pure logic, so it is tested without spending a route. The
# two-pass plumbing is tested against the captured argv, which is where the
# three failure modes the plan named would show up.
# --------------------------------------------------------------------------- #

from skidl_layout.power_copper import plan_loop_first_nets  # noqa: E402


def _looped_stage(**kw):
    return _stage(loops=[CommutationLoop(
        member_refs=["CIN", "L1", "M1", "RS"],
        net_names=["VIN", "SW", "ISNS"], returns_through="GND")], **kw)


def test_loop_nets_come_from_the_classifier_not_a_new_heuristic():
    nets, source = plan_loop_first_nets(
        PowerStagePlan(stages=[_looped_stage()]), plane_nets=["GND"])
    assert source == "power_stage_plan"
    # loop order first, then the stage's switch node / rails; GND is poured.
    assert nets == ["VIN", "SW", "ISNS", "VOUT"]


def test_loop_nets_exclude_a_poured_net_but_keep_a_routed_promoted_one():
    """Pass 1 cannot commit copper for a net the router is not routing."""
    nets, _ = plan_loop_first_nets(
        PowerStagePlan(stages=[_looped_stage()]),
        plane_nets=["GND", "VOUT"], routed_plane_nets=["VOUT"])
    assert "GND" not in nets
    assert "VOUT" in nets


def test_loop_nets_explicit_list_wins_and_is_labelled():
    nets, source = plan_loop_first_nets(
        PowerStagePlan(stages=[_looped_stage()]),
        explicit=["SW", "VIN", "GND"], plane_nets=["GND"])
    assert source == "explicit"
    assert nets == ["SW", "VIN"]          # poured nets still dropped


def test_loop_nets_empty_when_nothing_was_classified():
    nets, _ = plan_loop_first_nets(PowerStagePlan(stages=[]))
    assert nets == []


def test_loop_first_off_routes_exactly_once(patched):
    """The byte-identical claim: one call, the placed board, unchanged argv."""
    cap, tmp_path = patched
    calls = []
    original = power_copper.krt.route_and_check

    def counting(pcb_path, workdir, **kw):
        calls.append((pcb_path, kw.get("nets"), kw.get("route_extra_args")))
        return original(pcb_path, workdir, **kw)

    plan = _plan(_intent("GND", "pour", 0.3, "F.Cu"),
                 _intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    plan.nets = [PowerNet(name="VIN", kind="supply", suggested_width_mm=0.8)]
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[_looped_stage()])
    import unittest.mock as _mock
    with _mock.patch.object(power_copper.krt, "route_and_check", counting):
        out = emit_power_copper(result, object(), [], str(tmp_path),
                                board_layers=2)
    assert len(calls) == 1
    assert calls[0][1] == ["*", "!GND"]
    assert "--keep-input-copper" not in (calls[0][2] or [])
    assert out.loop_first == {}


def test_loop_first_routes_twice_and_chains_the_boards(patched):
    cap, tmp_path = patched
    calls = []
    original = power_copper.krt.route_and_check

    def counting(pcb_path, workdir, **kw):
        calls.append({"in": pcb_path, "nets": kw.get("nets"),
                      "extra": kw.get("route_extra_args"),
                      "out": kw.get("out_path"),
                      "log": kw.get("route_log_path")})
        return original(pcb_path, workdir, **kw)

    plan = _plan(_intent("GND", "pour", 0.3, "F.Cu"),
                 _intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    plan.nets = [PowerNet(name="VIN", kind="supply", suggested_width_mm=0.8)]
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[_looped_stage()])
    log = str(tmp_path / "route_log.txt")
    import unittest.mock as _mock
    with _mock.patch.object(power_copper.krt, "route_and_check", counting):
        out = emit_power_copper(result, object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                route_extra_args=["--fab-overrides", "f.txt"],
                                route_log_path=log, loop_first=True)

    assert len(calls) == 2
    p1, p2 = calls
    # Pass 1 routes ONLY the loop, off the placed board.
    assert p1["nets"] == ["VIN", "SW", "ISNS", "VOUT"]
    assert p1["in"].endswith("placed.kicad_pcb")
    # Pass 2's INPUT is pass 1's OUTPUT -- the chain, not two independent routes.
    assert p2["in"] == p1["out"]
    assert p2["nets"] == ["*", "!VIN", "!SW", "!ISNS", "!VOUT", "!GND"]
    # The fab floor is APPENDED to, never replaced.
    assert p2["extra"] == ["--fab-overrides", "f.txt", "--keep-input-copper"]
    assert p1["extra"] == ["--fab-overrides", "f.txt"]
    # Two logs, and the caller's own path stays the FINAL pass's.
    assert p1["log"] == str(tmp_path / "route_log.pass1.txt")
    assert p2["log"] == log
    assert out.loop_first["ran"] is True
    assert out.loop_first["pass1_nets"] == ["VIN", "SW", "ISNS", "VOUT"]
    assert out.to_dict()["loop_first"]["source"] == "power_stage_plan"


def test_loop_first_falls_back_to_one_pass_when_nothing_is_classified(patched):
    """Routing 'nothing' first is not a null experiment -- it is a second route
    from a different input board. Declined, and said out loud."""
    cap, tmp_path = patched
    calls = []
    original = power_copper.krt.route_and_check

    def counting(pcb_path, workdir, **kw):
        calls.append(pcb_path)
        return original(pcb_path, workdir, **kw)

    plan = _plan(_intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[])
    import unittest.mock as _mock
    with _mock.patch.object(power_copper.krt, "route_and_check", counting):
        out = emit_power_copper(result, object(), [], str(tmp_path),
                                board_layers=2, loop_first=True)
    assert len(calls) == 1
    assert out.loop_first == {"requested": True, "ran": False,
                              "reason": "no commutation loop classified"}
    assert any("no commutation loop was classified" in w for w in out.warnings)


def test_loop_first_accepts_an_explicit_partition(patched):
    cap, tmp_path = patched
    calls = []
    original = power_copper.krt.route_and_check

    def counting(pcb_path, workdir, **kw):
        calls.append(kw.get("nets"))
        return original(pcb_path, workdir, **kw)

    plan = _plan(_intent("GND", "pour", 0.3, "F.Cu"),
                 _intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[_looped_stage()])
    import unittest.mock as _mock
    with _mock.patch.object(power_copper.krt, "route_and_check", counting):
        out = emit_power_copper(result, object(), [], str(tmp_path),
                                board_layers=2, loop_first=["SW", "VIN"])
    assert calls[0] == ["SW", "VIN"]
    assert calls[1] == ["*", "!SW", "!VIN", "!GND"]
    assert out.loop_first["source"] == "explicit"


# --------------------------------------------------------------------------- #
# Phase 14, WS-14.3 / WS-14.5 -- the fanout pre-pass and the escape keepout.
#
# ⛔ The keepout is the arm Phase 13 measured at **-32 routed nets** when it was
# applied to a SINGLE pass, because KRT's keepout is not net-scoped and blocks
# the controller's own escape. These tests pin the ordering that makes it mean
# something else: the keepout must reach pass 2 only, and never pass 1.
# --------------------------------------------------------------------------- #

import unittest.mock as _mock  # noqa: E402

from skidl_layout import power_escape as _pe  # noqa: E402


class _FakePlaced:
    def __init__(self, ref):
        self.ref = ref
        self.footprint = "F"


class _FakeRoom:
    controller_ref = "U1"
    lane_mm = 0.9048
    annulus = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]]


def _looped_result():
    plan = _plan(_intent("GND", "pour", 0.3, "F.Cu"),
                 _intent("VIN", "wide_trunk", 0.8, "F.Cu"))
    plan.nets = [PowerNet(name="VIN", kind="supply", suggested_width_mm=0.8)]
    result = _FakeResult(plan)
    result.power_stage_plan = PowerStagePlan(stages=[_looped_stage()])
    result.placed_parts = [_FakePlaced("U1")]
    return result


def test_fanout_off_and_keepout_off_add_nothing(patched):
    cap, tmp_path = patched
    out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                            board_layers=2)
    assert out.fanout == {}
    assert out.keepout == {}
    assert "--keepout" not in (cap["route_extra_args"] or [])
    assert out.to_dict()["fanout"] == {}


def test_fanout_runs_before_pass_1_and_chains_its_board(patched):
    cap, tmp_path = patched
    order = []

    def fake_fanout(pcb, out_pcb, ref, **kw):
        order.append(("fanout", ref, kw.get("nets"), kw.get("escape_method")))
        with open(out_pcb, "w", encoding="utf-8") as fh:
            fh.write("(fanned)\n")
        return {"ran": True, "vias_placed": 10, "vias_dropped": 0,
                "failed_nets": [], "controller": ref}

    original = power_copper.krt.route_and_check

    def counting(pcb_path, workdir, **kw):
        order.append(("route", pcb_path, kw.get("nets")))
        return original(pcb_path, workdir, **kw)

    with _mock.patch.object(_pe, "fanout_controller", fake_fanout), \
         _mock.patch.object(power_copper.krt, "route_and_check", counting):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                loop_first=True, fanout_controller=True)

    assert order[0][0] == "fanout"              # BEFORE any route
    assert order[0][1] == "U1"
    assert order[0][3] == "underpad"
    # Housekeeping nets only: the poured net and the trunked power net are out.
    assert order[0][2] == ["*", "!GND", "!VIN"]
    # Pass 1 reads the FANNED board, not the placed one.
    assert order[1][0] == "route"
    assert order[1][1].endswith("fanout_0_U1.kicad_pcb")
    assert out.fanout["vias_placed"] == 10
    assert out.fanout["ran"] is True


def test_fanout_uses_the_routing_clearance_not_the_fab_floor(patched):
    """⛔⛔ Regression test for a measured DRC violation.

    ``qfn_fanout``'s ``--clearance`` is the margin its escape copper keeps from
    foreign pads and tracks, so it must be the clearance the board is actually
    ROUTED and GRADED at -- not the fab's published minimum. Handed the floor
    (0.1524 mm on ``oshpark-2l``) the pre-pass legally placed an escape via that
    the board's own 0.25 mm rule then flagged: measured on ``lt3757_sepic`` as
    ``Via:UVLO <-> Seg:INTVCC, overlap 0.036 mm``.

    ⚠ ``track_width`` keeps the fab FLOOR on purpose -- a thin escape stub is
    legal; copper too *close* to its neighbour is not.
    """
    cap, tmp_path = patched
    seen = {}

    def capture(pcb, out_pcb, ref, **kw):
        seen.update(kw)
        with open(out_pcb, "w", encoding="utf-8") as fh:
            fh.write("(fanned)\n")
        return {"ran": True, "vias_placed": 1, "vias_dropped": 0,
                "failed_nets": [], "controller": ref}

    with _mock.patch.object(_pe, "fanout_controller", capture):
        emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                          board_layers=2, fab_spec="oshpark-2l",
                          fanout_controller=True)
    from skidl_layout.fabspec import resolve_fab_spec

    spec = resolve_fab_spec("oshpark-2l")
    assert seen["clearance"] == spec.clearance_mm == 0.25
    assert seen["clearance"] != spec.min_clearance_mm
    # The floor is still right for the stub width, and the via matches the board.
    assert seen["track_width"] == spec.min_track_mm
    assert seen["via_size"] == spec.via_size_mm
    assert seen["via_drill"] == spec.via_drill_mm


def _capture_fanout(seen):
    """A ``fanout_controller`` stand-in that records its kwargs and writes a board."""
    def capture(pcb, out_pcb, ref, **kw):
        seen.update(kw)
        with open(out_pcb, "w", encoding="utf-8") as fh:
            fh.write("(fanned)\n")
        return {"ran": True, "vias_placed": 1, "vias_dropped": 0,
                "failed_nets": [], "controller": ref,
                # what KRT reports back: the ledger never fires in a fanout
                # process, so this equals the --clearance it was handed.
                "min_clearance_used": kw.get("clearance")}
    return capture


# --------------------------------------------------------------------------- #
# Spacing plan C -- the fanout's clearance, sized from IPC-2221B
# --------------------------------------------------------------------------- #
HV_VOLTS = {"VIN": 72.0, "SW": 150.0, "VOUT": 12.0}


def test_fanout_voltage_spacing_off_leaves_the_clearance_at_the_fab_spec(patched):
    """⛔ The byte-identity contract: with the flag off, the scalar is the
    pre-plan-C one even on a board whose voltages ask for much more."""
    cap, tmp_path = patched
    seen = {}
    with _mock.patch.object(_pe, "fanout_controller", _capture_fanout(seen)):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                fanout_controller=True, net_voltages=HV_VOLTS)
    assert seen["clearance"] == 0.25
    assert out.fanout["clearance_requested_mm"] is None
    assert not any("fanout_voltage_spacing" in w for w in out.warnings)


def test_the_deficit_judge_runs_even_with_the_lever_off(patched):
    """⭐ "Judge before lever", made structural: the defect is measured on the
    default fanout path, not only in the arm that fixes it."""
    cap, tmp_path = patched
    seen = {}
    with _mock.patch.object(_pe, "fanout_controller", _capture_fanout(seen)):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                fanout_controller=True, net_voltages=HV_VOLTS)
    deficits = out.fanout["deficits"]
    assert sorted(deficits) == ["SW", "VIN"]         # VOUT's 12V asks 0.1mm
    assert deficits["SW"]["used_mm"] == 0.25
    assert deficits["SW"]["required_mm"] == 0.6
    assert out.fanout["min_clearance_used"] == 0.25
    assert any("below what IPC-2221B asks" in w for w in out.warnings)


def test_fanout_voltage_spacing_on_widens_the_clearance_and_clears_the_deficit(patched):
    """The lever: ``max(board clearance, worst declared requirement)``."""
    cap, tmp_path = patched
    seen = {}
    with _mock.patch.object(_pe, "fanout_controller", _capture_fanout(seen)):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                fanout_controller=True, net_voltages=HV_VOLTS,
                                fanout_voltage_spacing=True)
    assert seen["clearance"] == 0.6
    assert out.fanout["clearance_requested_mm"] == 0.6
    assert out.fanout["deficits"] == {}
    assert any("escaping at 0.6mm instead of the board's 0.25mm" in w
               for w in out.warnings)
    # ⚠ The stub width and the via are untouched -- only the spacing moved.
    assert seen["track_width"] == 0.1524


def test_fanout_voltage_spacing_on_a_board_with_no_declared_voltage_is_inert(patched):
    """⛔ Gate C3's property: no voltages -> the max collapses -> the argv is the
    default path's argv, and the no-op is RECORDED rather than silent."""
    cap, tmp_path = patched
    seen = {}
    with _mock.patch.object(_pe, "fanout_controller", _capture_fanout(seen)):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                fanout_controller=True,
                                fanout_voltage_spacing=True)
    assert seen["clearance"] == 0.25
    assert out.fanout["clearance_requested_mm"] is None
    assert out.fanout["deficits"] == {}
    assert any("no net_voltages were given" in w for w in out.warnings)


def test_fanout_voltage_spacing_records_a_no_op_when_the_board_already_complies(patched):
    """A board whose every declared net sits under 30 V needs nothing; the flag
    must say it changed nothing rather than imply it bit."""
    cap, tmp_path = patched
    seen = {}
    with _mock.patch.object(_pe, "fanout_controller", _capture_fanout(seen)):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                fanout_controller=True,
                                net_voltages={"VIN": 12.0, "VOUT": 5.0},
                                fanout_voltage_spacing=True)
    assert seen["clearance"] == 0.25
    assert out.fanout["clearance_requested_mm"] is None
    assert any("already meets every declared net" in w for w in out.warnings)


# --------------------------------------------------------------------------- #
# Spacing plan C, unplanned -- the placed board's missing sibling project
# --------------------------------------------------------------------------- #
def test_the_placed_board_ships_with_no_sibling_project_by_default(patched):
    """⛔⛔ The defect, pinned as a characterisation test.

    This is the root cause of open defect 8: with no ``.kicad_pro`` beside it,
    whichever KRT stage reads ``placed.kicad_pcb`` first calls
    ``fix_project_for_output``, finds nothing to carry over, and seeds KiCad's
    **stock 0.2 mm** ``Default`` net class. That writeback only ever *lowers*, so
    the board's real 0.25 mm design clearance can never be recovered, and
    ``route.py`` routes every net against a 0.2 mm nominal.
    """
    cap, tmp_path = patched
    out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                            board_layers=2, fab_spec="oshpark-2l")
    placed = os.path.join(str(tmp_path), "placed.kicad_pcb")
    assert os.path.isfile(placed)
    assert not os.path.isfile(os.path.join(str(tmp_path), "placed.kicad_pro"))
    assert out.seed_project == {}
    assert out.to_dict()["seed_project"] == {}


def test_seed_placed_project_writes_the_boards_real_design_clearance(patched):
    """The one-file fix: a COMPLETE Default class at the spec's clearance.

    ⚠ Complete, not sparse -- KiCad ignores a partial net class and falls back to
    its stock 0.2 mm, which is exactly the failure being fixed.
    """
    import json

    from skidl_layout.power_copper import _STOCK_NETCLASS_CLEARANCE_MM

    cap, tmp_path = patched
    out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                            board_layers=2, fab_spec="oshpark-2l",
                            seed_placed_project=True)
    path = os.path.join(str(tmp_path), "placed.kicad_pro")
    assert out.seed_project["written"] is True
    assert out.seed_project["clearance_mm"] == 0.25
    project = json.load(open(path, encoding="utf-8"))
    default = next(c for c in project["net_settings"]["classes"]
                   if c["name"] == "Default")
    assert default["clearance"] == 0.25 != _STOCK_NETCLASS_CLEARANCE_MM
    assert default["track_width"] == 0.1524
    assert project["board"]["design_settings"]["rules"]["min_clearance"] == 0.25
    # ⛔ KiCad ignores a sparse class; every field the stock class carries must be
    # present or the fallback re-appears.
    assert {"via_diameter", "via_drill", "diff_pair_gap", "priority"} <= set(default)
    assert any("seed_placed_project" in w for w in out.warnings)


def test_seed_placed_project_never_overwrites_an_existing_project(patched):
    """⛔ It may be a real user project, and the whole point is that ours is
    ABSENT -- so an existing file is left alone and the decline is recorded."""
    cap, tmp_path = patched
    path = os.path.join(str(tmp_path), "placed.kicad_pro")
    os.makedirs(str(tmp_path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"mine": true}\n')
    out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                            board_layers=2, fab_spec="oshpark-2l",
                            seed_placed_project=True)
    assert out.seed_project["written"] is False
    assert open(path, encoding="utf-8").read() == '{"mine": true}\n'
    assert any("already" in w and "untouched" in w for w in out.warnings)


def test_a_declining_fanout_is_recorded_and_routed_past(patched):
    """A fanout that declines is an outcome; a silent decline reads as success."""
    cap, tmp_path = patched

    def refusing(pcb, out_pcb, ref, **kw):
        return {"ran": False, "reason": "doesn't appear to be a QFN/QFP",
                "controller": ref}

    with _mock.patch.object(_pe, "fanout_controller", refusing):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                fanout_controller=True)
    assert out.fanout["ran"] is False
    assert out.fanout["board"] is None
    assert any("NOT run" in w for w in out.warnings)


def test_keepout_reaches_pass_2_only(patched):
    cap, tmp_path = patched
    calls = []
    written = []
    original = power_copper.krt.route_and_check

    def counting(pcb_path, workdir, **kw):
        calls.append({"in": pcb_path, "extra": list(kw.get("route_extra_args") or []),
                      "out": kw.get("out_path")})
        return original(pcb_path, workdir, **kw)

    def fake_write(pcb_path, polygons, layer="User.2"):
        written.append((pcb_path, len(polygons), layer))
        return len(polygons)

    with _mock.patch.object(_pe, "measure_escape_rooms", lambda *a, **k: [_FakeRoom()]), \
         _mock.patch.object(_pe, "write_keepout_polygons", fake_write), \
         _mock.patch.object(power_copper.krt, "route_and_check", counting):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                route_extra_args=["--fab-overrides", "f.txt"],
                                loop_first=True, keepout_escape=True)

    p1, p2 = calls
    # ⛔ Pass 1 must NOT see the keepout -- that is the whole ordering claim.
    assert "--keepout" not in p1["extra"]
    assert p2["extra"] == ["--fab-overrides", "f.txt", "--keep-input-copper",
                           "--keepout", "--keepout-layer", "User.2"]
    # The polygons are drawn into the board pass 2 reads: pass 1's OUTPUT.
    assert written[0][0] == p1["out"] == p2["in"]
    assert out.keepout["written"] == 1
    assert out.keepout["applied_after_loop_pass"] is True


def test_keepout_without_loop_first_says_it_is_phase_13s_arm_c(patched):
    cap, tmp_path = patched
    with _mock.patch.object(_pe, "measure_escape_rooms", lambda *a, **k: [_FakeRoom()]), \
         _mock.patch.object(_pe, "write_keepout_polygons", lambda *a, **k: 1):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                keepout_escape=True)
    assert out.keepout["applied_after_loop_pass"] is False
    assert any("-32 routed nets" in w for w in out.warnings)


def test_keepout_with_no_measurable_annulus_emits_no_flag(patched):
    cap, tmp_path = patched
    with _mock.patch.object(_pe, "measure_escape_rooms", lambda *a, **k: []):
        out = emit_power_copper(_looped_result(), object(), [], str(tmp_path),
                                board_layers=2, fab_spec="oshpark-2l",
                                loop_first=True, keepout_escape=True)
    assert out.keepout["written"] == 0
    assert "--keepout" not in (cap["route_extra_args"] or [])
    assert any("no polygon written" in w for w in out.warnings)


# --------------------------------------------------------------------------
# Power-layout Phase 16: the routing stack and the plane-layer reservation
#
# ⚠ The ~90 existing ``board_layers=2`` call sites above guard the default
# path; they must all still pass unchanged.
# --------------------------------------------------------------------------

def test_two_layer_board_passes_no_layer_flags(patched):
    """⛔ Byte-identity: at two layers nothing about the router call changes."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "pour", 0.3, "F.Cu"),
        _intent("VIN_12V", "wide_trunk", 0.8, "F.Cu"),
    ))
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=2)
    assert cap["route_design_rules"].get("layers") is None
    assert cap["route_design_rules"].get("layer_costs") is None
    assert cap["pour_kwargs"].get("layers") is None


def test_four_layer_board_passes_the_full_stack(patched):
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "plane", 0.3, "In1.Cu"),
        _intent("V5", "internal_rail", 0.5, "In2.Cu"),
    ))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=4)
    stack = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert cap["route_design_rules"]["layers"] == stack
    # ⛔ No costs unless the reservation is asked for: arm L4 is the
    # maximum-completion case and every layer is routable there.
    assert "layer_costs" not in cap["route_design_rules"]
    assert cap["pour_kwargs"]["layers"] == stack
    assert "In1.Cu" in cap["pour_layers"]
    # the resolved stack travels on the result, so a driver reads it rather
    # than re-deriving it
    assert out.route_layers == stack
    assert out.route_layer_costs is None
    assert out.to_dict()["route_layers"] == stack


def test_reserve_plane_layers_forbids_exactly_the_poured_layers(patched):
    """⭐ The rule, in one line of English: a layer that carries a plane is not
    a routing layer, F.Cu is preferred, everything else costs more.

    ``-1`` is KRT's FORBIDDEN sentinel; ``1.0``/``3.0`` reproduce KRT's own
    2-layer bias. ⛔ Without this, ``route.py`` gives every layer cost 1.0 at
    four layers and runs signal tracks straight through the ground plane.
    """
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "plane", 0.3, "In1.Cu"),
        _intent("V5", "internal_rail", 0.5, "In2.Cu"),
    ))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=4,
                            reserve_plane_layers=True)
    assert cap["route_design_rules"]["layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert cap["route_design_rules"]["layer_costs"] == [1.0, -1.0, 3.0, 3.0]
    assert out.route_layer_costs == [1.0, -1.0, 3.0, 3.0]
    # ⛔ NEVER on the pour: route_planes.py routes plane-net taps down to the
    # plane layer, and forbidding it there disconnects every ground pad.
    assert "layer_costs" not in cap["pour_kwargs"]


def test_reserve_plane_layers_forbids_two_poured_layers(patched):
    """Both a ground plane and a promoted supply plane get reserved."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "plane", 0.3, "In1.Cu"),
        _intent("VIN_12V", "plane", 0.8, "In2.Cu"),
    ))
    emit_power_copper(result, object(), [], str(tmp_path), board_layers=4,
                      reserve_plane_layers=True)
    assert cap["route_design_rules"]["layer_costs"] == [1.0, -1.0, -1.0, 3.0]


def test_reserve_plane_layers_at_two_layers_is_a_recorded_no_op(patched):
    """⚠ Meaningless on two layers -> a warning, and argv byte-identical."""
    cap, tmp_path = patched
    result = _FakeResult(_plan(
        _intent("GND", "pour", 0.3, "F.Cu"),
        _intent("VIN_12V", "wide_trunk", 0.8, "F.Cu"),
    ))
    out = emit_power_copper(result, object(), [], str(tmp_path), board_layers=2,
                            reserve_plane_layers=True)
    assert cap["route_design_rules"].get("layers") is None
    assert cap["route_design_rules"].get("layer_costs") is None
    assert any("reserve_plane_layers" in w for w in out.warnings)
