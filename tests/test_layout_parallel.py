from __future__ import annotations

import pickle

import pytest

from skidl_layout import plan_layout
from skidl_layout.candidates import PlacementCandidate
from skidl_layout.context import LayoutContext
from skidl_layout.engine import _refine_candidate_trio, _resolve_parallel_workers
from skidl_layout.parallel import refine_candidate_worker
from skidl_layout.snapshot import snapshot_circuit
from skidl_layout.writer import PlacedPart


# --- fakes (mirror tests/test_layout_engine.py) ------------------------------


class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, net):
        self.part = part
        self.net = net
        net._pins.append(self)


class _Part:
    def __init__(self, ref, value="", footprint="", name="", nets=None, pins=2):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.name = name
        self.node = None
        self.pins = []
        for net in nets or []:
            self.pins.append(_Pin(self, net))
        while len(self.pins) < pins:
            self.pins.append(_Pin(self, _Net(f"{ref}_N{len(self.pins)}")))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = parts
        self.nets = nets

    def get_nets(self):
        return self.nets


BBOXES = {
    "Package_QFP:MCU": (12.0, 12.0),
    "Capacitor:C_0805": (2.0, 1.25),
    "Connector:USB": (10.0, 5.0),
}


def _circuit():
    vbus = _Net("VBUS")
    vcc = _Net("3V3")
    gnd = _Net("GND")
    u1 = _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[vcc, gnd], pins=2)
    c1 = _Part("C1", value="100nF", footprint="Capacitor:C_0805", nets=[vcc, gnd])
    j1 = _Part("J1", name="USB connector", footprint="Connector:USB", nets=[vbus, gnd])
    return _Circuit([u1, c1, j1], [vbus, vcc, gnd])


def _sig(result):
    return [
        (p.ref, round(p.x_mm, 6), round(p.y_mm, 6), round(p.rot_deg, 6), p.side)
        for p in result.placed_parts
    ]


# --- env resolution ----------------------------------------------------------


def test_parallel_env_resolution(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_PARALLEL", raising=False)
    assert _resolve_parallel_workers(None) is None
    assert _resolve_parallel_workers(4) == 4

    monkeypatch.setenv("SKIDL_LAYOUT_PARALLEL", "3")
    assert _resolve_parallel_workers(None) == 3  # env default
    assert _resolve_parallel_workers(8) == 8  # explicit kwarg wins

    monkeypatch.setenv("SKIDL_LAYOUT_PARALLEL", "notanint")
    with pytest.raises(ValueError):
        _resolve_parallel_workers(None)


# --- worker round-trip (in-process, no pool) ---------------------------------


def test_worker_roundtrip():
    circuit = _circuit()
    snap = snapshot_circuit(circuit)
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 14.0, 10.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 30.0, 10.0, 0.0, "Connector:USB"),
    ]
    cand_expected = PlacementCandidate(name="baseline", placed_parts=list(placed))
    ctx = LayoutContext.from_circuit(snap)
    _refine_candidate_trio(cand_expected, snap, BBOXES, {}, 0.5, 2, ctx, None)

    cand_worker = PlacementCandidate(name="baseline", placed_parts=list(placed))
    payload = pickle.dumps((cand_worker, snap, BBOXES, {}, 0.5, 2))
    got = pickle.loads(refine_candidate_worker(payload))

    expect_sig = [(p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in cand_expected.placed_parts]
    got_sig = [(p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in got.placed_parts]
    assert got_sig == expect_sig


# --- fallback paths (no real spawn) ------------------------------------------


def test_parallel_refuses_in_child(monkeypatch):
    """A truthy parent_process() means we are a spawn child: never parallelize."""
    import multiprocessing

    monkeypatch.setattr(multiprocessing, "parent_process", lambda: object())
    msgs: list[str] = []
    result = plan_layout(
        _circuit(), fp_bboxes=BBOXES, parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert not any("in parallel" in m for m in msgs)
    # still a valid sequential result
    assert result.validation.placed_parts == 3
    assert _sig(result) == _sig(plan_layout(_circuit(), fp_bboxes=BBOXES))


def test_parallel_fallback_on_error(monkeypatch):
    """Any failure building/dispatching the pool -> silent sequential fallback,
    byte-identical result, and a fallback message."""
    import skidl_layout.snapshot as snap_mod

    def boom(_circuit):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(snap_mod, "snapshot_circuit", boom)
    msgs: list[str] = []
    result = plan_layout(
        _circuit(), fp_bboxes=BBOXES, parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert any("falling back to sequential" in m for m in msgs)
    assert _sig(result) == _sig(plan_layout(_circuit(), fp_bboxes=BBOXES))


# --- real spawn pool: parallel == sequential ---------------------------------


def test_parallel_matches_sequential():
    seq = plan_layout(_circuit(), fp_bboxes=BBOXES)
    msgs: list[str] = []
    par = plan_layout(
        _circuit(), fp_bboxes=BBOXES, parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert any("in parallel" in m for m in msgs), "parallel path did not engage"
    assert _sig(par) == _sig(seq)
    assert par.score.to_dict() == seq.score.to_dict()
    assert par.report == seq.report
    assert [c.name for c in par.candidates] == [c.name for c in seq.candidates]


# --- round-8 WS30: combined refine+finalize (mode "full") --------------------


def test_combined_matches_sequential():
    """The default (round-8) combined path is byte-identical to sequential, and
    the combined rung actually engaged (not a silent two-phase fallback)."""
    seq = plan_layout(_circuit(), fp_bboxes=BBOXES, parallel_workers=1)
    msgs: list[str] = []
    par = plan_layout(
        _circuit(), fp_bboxes=BBOXES, parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert any("refine+finalize combined" in m for m in msgs), (
        "combined path did not engage"
    )
    assert _sig(par) == _sig(seq)
    assert par.score.to_dict() == seq.score.to_dict()
    assert par.report == seq.report
    assert [c.name for c in par.candidates] == [c.name for c in seq.candidates]


def test_combined_matches_sequential_with_outline():
    """Combined == sequential with a fixed BoardOutline, exercising the
    edge-anchor snap branch of the post-trio impl inside the worker."""
    from skidl_layout.constraints import BoardOutline, LayoutConstraints

    outline = BoardOutline(60.0, 40.0)
    seq = plan_layout(
        _circuit(), fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline), parallel_workers=1,
    )
    msgs: list[str] = []
    par = plan_layout(
        _circuit(), fp_bboxes=BBOXES,
        constraints=LayoutConstraints(outline=outline), parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert any("refine+finalize combined" in m for m in msgs)
    assert _sig(par) == _sig(seq)
    assert par.score.to_dict() == seq.score.to_dict()
    assert par.report == seq.report


def test_combined_fallback_on_error(monkeypatch):
    """A raise inside run_payloads trips the full hazard-#5 fallback ladder:
    combined -> two-phase (refine + finalize) -> sequential. Result stays
    byte-identical and every rung's fallback message is emitted."""
    import skidl_layout.parallel as par_mod

    seq = plan_layout(_circuit(), fp_bboxes=BBOXES, parallel_workers=1)

    def boom(*a, **k):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(par_mod, "run_payloads", boom)
    msgs: list[str] = []
    par = plan_layout(
        _circuit(), fp_bboxes=BBOXES, parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert _sig(par) == _sig(seq)
    assert par.score.to_dict() == seq.score.to_dict()
    assert any("combined parallel planning unavailable" in m for m in msgs)
    assert any("parallel refinement unavailable" in m for m in msgs)
    assert any("parallel finalize unavailable" in m for m in msgs)
