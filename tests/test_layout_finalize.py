"""Round-6 WS21 finalize tests: picklability of the extracted finalize params /
result, and a small-circuit form of the live-vs-snapshot finalize identity check.

Reuses the fakes from tests/test_layout_parallel.py (tiny 3-part circuit).
"""
from __future__ import annotations

import pickle

from skidl_layout import plan_layout
from skidl_layout.candidates import PlacementCandidate
from skidl_layout.context import LayoutContext
from skidl_layout.engine import (
    _FinalizeParams,
    _finalize_candidate_impl,
    _refine_candidate_trio,
)
from skidl_layout.intent import infer_placement_intents
from skidl_layout.snapshot import snapshot_circuit

from tests.test_layout_parallel import BBOXES, _circuit


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
    """A full _FinalizedCandidate (the finalize result) round-trips."""
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
