"""Worker entry point for opt-in parallel candidate refinement (WS18).

A single top-level function so ``multiprocessing`` on Windows (``spawn``) can
import it by name in the child process. The parent ships a pre-pickled payload
(a :class:`~skidl_layout.snapshot.SnapshotCircuit`, the candidate to refine, and
the geometry inputs); the worker rebuilds the :class:`LayoutContext` itself (a
pure function of the snapshot) rather than pickling the context, runs the pass-1
refinement trio, and returns the mutated candidate as pickled bytes.

Keeping the executor-submission surface to a single ``bytes`` argument means any
pickling error is raised in the parent at ``pickle.dumps`` time (catchable there
for a clean sequential fallback), not deep inside the pool.
"""

from __future__ import annotations

import pickle


def refine_candidate_worker(payload: bytes) -> bytes:
    (
        candidate,
        snapshot,
        bboxes,
        fp_geometries,
        clearance_mm,
        board_layers,
    ) = pickle.loads(payload)

    from .context import LayoutContext
    from .engine import _refine_candidate_trio

    ctx = LayoutContext.from_circuit(snapshot)
    _refine_candidate_trio(
        candidate,
        snapshot,
        bboxes,
        fp_geometries,
        clearance_mm,
        board_layers,
        ctx,
        progress=None,
    )
    return pickle.dumps(candidate)
