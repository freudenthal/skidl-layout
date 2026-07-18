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
