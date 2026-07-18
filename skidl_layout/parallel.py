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

import os
import pickle
import shutil
import subprocess
import sys
import tempfile


def run_payloads(mode: str, payloads: dict, workers: int) -> dict:
    """Run ``payloads`` (``{index: pickled bytes}``) through the ``mode`` worker in
    plain ``subprocess`` children and return ``{index: result bytes}`` (round-7
    WS25, replaces the round-5/6 ``spawn`` ``ProcessPoolExecutor``).

    ``mode`` is ``"refine"`` or ``"finalize"`` (dispatched by name inside
    :mod:`skidl_layout._worker_main`). Children run
    ``python -m skidl_layout._worker_main`` — a plain module, never the caller's
    ``__main__`` — so an unguarded driver script is structurally safe.

    Payloads travel via files in a private temp dir (argv carries the paths), not
    stdin/stdout, so a worker that prints a stray warning cannot corrupt the
    protocol. Assignment is round-robin by sorted index (process ``j`` of ``k``
    gets the indices whose rank ``% k == j``), bounding interpreter-import
    overhead at ``k`` imports even when jobs > workers; results are keyed by
    index via per-index output files, so completion order cannot matter.

    Any nonzero exit, timeout (600 s/process; all processes killed first), or a
    missing/unreadable output file raises ``RuntimeError`` (with the worker's
    trailing stderr) — the callers' existing try/except turns that into the
    byte-identical sequential fallback. The temp dir is always removed.
    """
    tmp = tempfile.mkdtemp(prefix="skidl_layout_par_")
    procs: list = []
    try:
        indices = sorted(payloads)
        in_paths: dict = {}
        out_paths: dict = {}
        for i in indices:
            in_paths[i] = os.path.join(tmp, f"in_{i}.pkl")
            out_paths[i] = os.path.join(tmp, f"out_{i}.pkl")
            with open(in_paths[i], "wb") as f:
                f.write(payloads[i])

        k = min(workers, len(indices))
        batches: list = [[] for _ in range(k)]
        for rank, i in enumerate(indices):
            batches[rank % k].append(i)

        for batch in batches:
            args = [sys.executable, "-m", "skidl_layout._worker_main", mode]
            for i in batch:
                args.append(in_paths[i])
                args.append(out_paths[i])
            procs.append(
                subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=os.environ.copy(),
                )
            )

        for proc in procs:
            try:
                _, stderr = proc.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                for p in procs:
                    p.kill()
                raise RuntimeError("worker subprocess timed out after 600 s")
            if proc.returncode != 0:
                tail = (stderr or b"").decode("utf-8", "replace")[-200:]
                raise RuntimeError(
                    f"worker subprocess exited {proc.returncode}: {tail}"
                )

        results: dict = {}
        for i in indices:
            try:
                with open(out_paths[i], "rb") as f:
                    results[i] = f.read()
            except OSError as exc:
                raise RuntimeError(f"worker output missing for index {i}: {exc}")
        return results
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


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


def finalize_candidate_worker(payload: bytes) -> bytes:
    """Round-6 WS22 worker: finalize one canonical candidate's post-anchor pass.

    Mirrors :func:`refine_candidate_worker` — a single ``bytes`` argument so any
    pickling error surfaces in the parent at ``pickle.dumps`` time. Rebuilds the
    :class:`LayoutContext` from the snapshot (a pure function of it) rather than
    pickling the context, runs the extracted finalize impl with ``emit=None`` /
    ``progress=None`` (byte-identical to the sequential default), and returns the
    mutated ``_FinalizedCandidate`` as pickled bytes.
    """
    (candidate, snapshot, params) = pickle.loads(payload)

    from .context import LayoutContext
    from .engine import _finalize_candidate_impl

    ctx = LayoutContext.from_circuit(snapshot)
    finalized, _ = _finalize_candidate_impl(
        candidate, snapshot, params, ctx, emit=None, progress=None
    )
    return pickle.dumps(finalized)
