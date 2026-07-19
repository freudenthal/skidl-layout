"""Round-6 finalize tests: WS21 picklability of the extracted finalize params /
result, and WS22 opt-in parallel finalize == sequential + fallback.

Reuses the fakes from tests/test_layout_parallel.py (spawn-safe module-level
worker + tiny 3-part circuit).
"""
from __future__ import annotations

import pickle

from skidl_layout import plan_layout
from skidl_layout.candidates import PlacementCandidate
from skidl_layout.context import LayoutContext
from skidl_layout.engine import (
    _FinalizeParams,
    _finalize_candidate_impl,
    _posttrio_candidate_impl,
    _refine_candidate_trio,
)
from skidl_layout.intent import infer_placement_intents
from skidl_layout.snapshot import snapshot_circuit

from tests.test_layout_parallel import BBOXES, _circuit, _sig


# --- WS21: picklability ------------------------------------------------------


def test_placement_intent_plan_pickles():
    """PlacementIntentPlan must pickle for the finalize worker payload (hazard
    #1 ships it from the parent rather than rebuilding it from the snapshot)."""
    plan = infer_placement_intents(_circuit())
    roundtrip = pickle.loads(pickle.dumps(plan))
    assert type(roundtrip) is type(plan)


def test_finalize_params_pickles():
    plan = infer_placement_intents(_circuit())
    params = _FinalizeParams(
        resolved_bboxes=dict(BBOXES),
        fp_geometries={},
        clearance_mm=0.5,
        board_layers=2,
        margin_mm=3.0,
        corner_radius_mm=None,
        form_factor=None,
        auto_outline=True,
        resolved_outline=None,
        resolved_constraints=None,
        density_outline=None,
        intent_plan=plan,
        derive_outline_if_missing=True,
        constraints=None,
    )
    got = pickle.loads(pickle.dumps(params))
    assert got.clearance_mm == 0.5
    assert got.board_layers == 2
    assert got.resolved_bboxes == BBOXES


def test_finalized_candidate_pickles():
    """A full _FinalizedCandidate (the worker's return value) round-trips."""
    result = plan_layout(_circuit(), fp_bboxes=BBOXES)
    # Rebuild one via the impl so we pickle the real dataclass, not LayoutResult.
    from skidl_layout.writer import PlacedPart

    circuit = _circuit()
    snap = snapshot_circuit(circuit)
    ctx = LayoutContext.from_circuit(snap)
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 14.0, 10.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 30.0, 10.0, 0.0, "Connector:USB"),
    ]
    cand = PlacementCandidate(name="baseline", placed_parts=list(placed))
    _refine_candidate_trio(cand, snap, BBOXES, {}, 0.5, 2, ctx, None)
    plan = infer_placement_intents(snap)
    params = _FinalizeParams(
        resolved_bboxes=dict(BBOXES),
        fp_geometries={},
        clearance_mm=0.5,
        board_layers=2,
        margin_mm=3.0,
        corner_radius_mm=None,
        form_factor=None,
        auto_outline=False,
        resolved_outline=None,
        resolved_constraints=None,
        density_outline=None,
        intent_plan=plan,
        derive_outline_if_missing=False,
        constraints=None,
    )
    finalized, _ = _finalize_candidate_impl(cand, snap, params, ctx, None, None)
    got = pickle.loads(pickle.dumps(finalized))
    assert got.candidate.name == "baseline"
    assert [p.ref for p in got.placed_parts] == [p.ref for p in finalized.placed_parts]


# --- WS29: post-trio impl extraction (byte-identical refactor) ---------------


def _prepared_candidate(snap, ctx):
    """A 3-part candidate with its refinement trio already run (post-trio impl
    input)."""
    from skidl_layout.writer import PlacedPart

    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 14.0, 10.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 30.0, 10.0, 0.0, "Connector:USB"),
    ]
    cand = PlacementCandidate(name="baseline", placed_parts=list(placed))
    _refine_candidate_trio(cand, snap, BBOXES, {}, 0.5, 2, ctx, None)
    return cand


def _posttrio_params(snap):
    plan = infer_placement_intents(snap)
    return _FinalizeParams(
        resolved_bboxes=dict(BBOXES),
        fp_geometries={},
        clearance_mm=0.5,
        board_layers=2,
        margin_mm=3.0,
        corner_radius_mm=None,
        form_factor=None,
        auto_outline=False,
        resolved_outline=None,
        resolved_constraints=None,
        density_outline=None,
        intent_plan=plan,
        derive_outline_if_missing=False,
        constraints=None,
    )


def test_posttrio_impl_matches_inline_expectations():
    """WS29: _posttrio_candidate_impl returns (LayoutScore, ValidationResult),
    sets candidate.score to score.score, and is deterministic across two
    identically-prepared candidates."""
    from skidl_layout.scoring import LayoutScore
    from skidl_layout.validator import ValidationResult

    circuit = _circuit()
    snap = snapshot_circuit(circuit)
    ctx = LayoutContext.from_circuit(snap)
    params = _posttrio_params(snap)

    cand_a = _prepared_candidate(snap, ctx)
    score_a, val_a = _posttrio_candidate_impl(cand_a, snap, params, ctx)
    assert isinstance(score_a, LayoutScore)
    assert isinstance(val_a, ValidationResult)
    assert cand_a.score == score_a.score

    cand_b = _prepared_candidate(snap, ctx)
    score_b, val_b = _posttrio_candidate_impl(cand_b, snap, params, ctx)
    assert score_b.to_dict() == score_a.to_dict()
    assert val_b.ok == val_a.ok


# --- WS21.5: finalize impl live vs snapshot ----------------------------------


def test_finalize_impl_live_equals_snapshot():
    """_finalize_candidate_impl on the live circuit vs a snapshot-rebuilt ctx
    is byte-equal (hazard #7 backstop, small-circuit form of verify_finalize)."""
    from skidl_layout import engine

    live, snap = engine._finalize_identity_probe(_circuit(), None)

    def sig(fin):
        return (
            [(p.ref, round(p.x_mm, 6), round(p.y_mm, 6), round(p.rot_deg, 6), p.side)
             for p in fin.placed_parts],
            fin.score.to_dict(),
            fin.validation.ok,
            list(fin.candidate.reasons),
        )

    assert sig(live) == sig(snap)


# --- WS22: opt-in parallel finalize == sequential ----------------------------


def test_parallel_finalize_matches_sequential():
    seq = plan_layout(_circuit(), fp_bboxes=BBOXES)
    msgs: list[str] = []
    par = plan_layout(
        _circuit(), fp_bboxes=BBOXES, parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert _sig(par) == _sig(seq)
    assert par.score.to_dict() == seq.score.to_dict()
    assert par.report == seq.report
    assert [c.name for c in par.candidates] == [c.name for c in seq.candidates]


def test_finalize_worker_roundtrip():
    """finalize_candidate_worker (in-process) == _finalize_candidate_impl on the
    same snapshot payload."""
    from skidl_layout.parallel import finalize_candidate_worker
    from skidl_layout.writer import PlacedPart

    circuit = _circuit()
    snap = snapshot_circuit(circuit)
    ctx = LayoutContext.from_circuit(snap)
    placed = [
        PlacedPart("U1", 10.0, 10.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("C1", 14.0, 10.0, 0.0, "Capacitor:C_0805"),
        PlacedPart("J1", 30.0, 10.0, 0.0, "Connector:USB"),
    ]
    plan = infer_placement_intents(snap)
    params = _FinalizeParams(
        resolved_bboxes=dict(BBOXES),
        fp_geometries={},
        clearance_mm=0.5,
        board_layers=2,
        margin_mm=3.0,
        corner_radius_mm=None,
        form_factor=None,
        auto_outline=False,
        resolved_outline=None,
        resolved_constraints=None,
        density_outline=None,
        intent_plan=plan,
        derive_outline_if_missing=False,
        constraints=None,
    )
    cand_a = PlacementCandidate(name="baseline", placed_parts=list(placed))
    _refine_candidate_trio(cand_a, snap, BBOXES, {}, 0.5, 2, ctx, None)
    cand_b = PlacementCandidate(name="baseline", placed_parts=list(placed))
    _refine_candidate_trio(cand_b, snap, BBOXES, {}, 0.5, 2, ctx, None)

    expected, _ = _finalize_candidate_impl(cand_a, snap, params, ctx, None, None)
    payload = pickle.dumps((cand_b, snap, params))
    got = pickle.loads(finalize_candidate_worker(payload))

    esig = [(p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in expected.placed_parts]
    gsig = [(p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in got.placed_parts]
    assert gsig == esig
    assert got.score.to_dict() == expected.score.to_dict()


def test_parallel_finalize_fallback_on_error(monkeypatch):
    """An error raised while dispatching the finalize workers is caught inside
    _finalize_candidates_parallel -> sequential fallback, byte-identical result,
    fallback message. Patching run_payloads (resolved at call time via a local
    import, round-7 WS25) trips the internal try/except without a real
    subprocess."""
    import skidl_layout.parallel as par_mod

    seq = plan_layout(_circuit(), fp_bboxes=BBOXES)

    def boom(*a, **k):
        raise RuntimeError("pool exploded")

    monkeypatch.setattr(par_mod, "run_payloads", boom)
    msgs: list[str] = []
    par = plan_layout(
        _circuit(), fp_bboxes=BBOXES, parallel_workers=2,
        progress=lambda m: msgs.append(m),
    )
    assert _sig(par) == _sig(seq)
    assert par.score.to_dict() == seq.score.to_dict()
    assert any(
        "parallel finalize unavailable" in m and "sequential" in m for m in msgs
    )
