"""Round-7 WS25: subprocess worker transport (`run_payloads`) tests.

`run_payloads` launches plain `python -m skidl_layout._worker_main` children
(never `multiprocessing`, so unguarded callers are structurally safe) and
returns results keyed by index via per-index output files. These tests exercise
the real subprocess transport end-to-end against the in-process worker.
"""
from __future__ import annotations

import os
import pickle

import pytest

from skidl_layout.candidates import PlacementCandidate
from skidl_layout.parallel import refine_candidate_worker, run_payloads
from skidl_layout.snapshot import snapshot_circuit
from skidl_layout.writer import PlacedPart

from tests.test_layout_parallel import BBOXES, _circuit


def _refine_payload():
    circuit = _circuit()
    snap = snapshot_circuit(circuit)
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 14.0, 10.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 30.0, 10.0, 0.0, "Connector:USB"),
    ]
    cand = PlacementCandidate(name="baseline", placed_parts=list(placed))
    return pickle.dumps((cand, snap, BBOXES, {}, 0.5, 2))


def _sig(cand):
    return [(p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in cand.placed_parts]


def test_run_payloads_refine_roundtrip():
    payloads = {0: _refine_payload(), 1: _refine_payload()}
    raw = run_payloads("refine", payloads, workers=2)
    assert set(raw) == {0, 1}
    for i, payload in payloads.items():
        expect = pickle.loads(refine_candidate_worker(payload))
        got = pickle.loads(raw[i])
        assert _sig(got) == _sig(expect)


def test_run_payloads_batching_more_jobs_than_workers():
    payloads = {0: _refine_payload(), 1: _refine_payload(), 2: _refine_payload()}
    raw = run_payloads("refine", payloads, workers=2)
    assert set(raw) == {0, 1, 2}
    for i, payload in payloads.items():
        expect = pickle.loads(refine_candidate_worker(payload))
        assert _sig(pickle.loads(raw[i])) == _sig(expect)


def test_run_payloads_failure_raises():
    """A garbage payload makes the worker exit 1 -> RuntimeError, and the private
    temp dir is cleaned up in the finally."""
    import tempfile

    with pytest.raises(RuntimeError):
        run_payloads("refine", {0: b"not a pickle"}, workers=1)
    # No skidl_layout_par_* temp dir should survive the RuntimeError.
    tmproot = tempfile.gettempdir()
    leftover = [n for n in os.listdir(tmproot) if n.startswith("skidl_layout_par_")]
    assert leftover == [], f"temp dirs leaked: {leftover}"


def test_plan_candidate_worker_roundtrip():
    """Round-8 WS30: in-process plan_candidate_worker == manual trio + posttrio +
    finalize on an identically-built candidate. The returned pass1 blob matches
    the post-trio stage; the finalized state matches the finalize stage."""
    from skidl_layout.context import LayoutContext
    from skidl_layout.engine import (
        _FinalizeParams,
        _finalize_candidate_impl,
        _posttrio_candidate_impl,
        _refine_candidate_trio,
    )
    from skidl_layout.intent import infer_placement_intents
    from skidl_layout.parallel import plan_candidate_worker

    circuit = _circuit()
    snap = snapshot_circuit(circuit)
    ctx = LayoutContext.from_circuit(snap)
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 14.0, 10.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 30.0, 10.0, 0.0, "Connector:USB"),
    ]
    params = _FinalizeParams(
        resolved_bboxes=dict(BBOXES), fp_geometries={}, clearance_mm=0.5,
        board_layers=2, margin_mm=3.0, corner_radius_mm=None, form_factor=None,
        auto_outline=False, resolved_outline=None, resolved_constraints=None,
        density_outline=None, intent_plan=infer_placement_intents(snap),
        derive_outline_if_missing=False, constraints=None,
    )

    # manual reference: trio -> post-trio (capture pass-1 sig) -> finalize
    cand_m = PlacementCandidate(name="baseline", placed_parts=list(placed))
    _refine_candidate_trio(cand_m, snap, BBOXES, {}, 0.5, 2, ctx, None)
    m_score, m_val = _posttrio_candidate_impl(cand_m, snap, params, ctx)
    manual_pass1_sig = _sig(cand_m)
    m_finalized, _ = _finalize_candidate_impl(cand_m, snap, params, ctx, None, None)

    # worker: one call runs the whole chain and returns both states
    cand_w = PlacementCandidate(name="baseline", placed_parts=list(placed))
    payload = pickle.dumps((cand_w, snap, params))
    pass1_blob, w_score, w_val, w_finalized = pickle.loads(
        plan_candidate_worker(payload)
    )
    pass1_cand = pickle.loads(pass1_blob)

    assert _sig(pass1_cand) == manual_pass1_sig
    assert w_score.to_dict() == m_score.to_dict()
    assert w_val.ok == m_val.ok
    fin_sig = lambda f: [
        (p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in f.placed_parts
    ]
    assert fin_sig(w_finalized) == fin_sig(m_finalized)
    assert w_finalized.score.to_dict() == m_finalized.score.to_dict()


def test_worker_main_import_light():
    """Hazard #10: the entry point must be spawn-inert — its only module-top-level
    import is `import sys`; `from . import parallel` lives indented inside main."""
    import skidl_layout._worker_main as wm

    src = open(wm.__file__, "r", encoding="utf-8").read()
    top_imports = [
        line
        for line in src.splitlines()
        if line and not line[0].isspace()
        and (line.startswith("import ") or line.startswith("from "))
    ]
    assert top_imports == ["import sys"], top_imports
    assert "        from . import parallel" in src or "    from . import parallel" in src
