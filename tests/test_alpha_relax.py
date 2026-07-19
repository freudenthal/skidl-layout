"""Tests for the opt-in ``alpha_relax`` candidate strategy (round-2 WS2).

``alpha_relax`` is a REQUEST-ONLY placement candidate whose seed is the
cluster-zone seed post-processed by the alpha-annealed force relaxation ported
from the skidl schematic placer (``alpha_relax.alpha_relax_placement``). The
load-bearing safety property is that the DEFAULT candidate set is byte-identical
to before -- the strategy never emits unless explicitly requested, and its
transform runs AFTER the seed memo so it never pollutes a sibling's raw seed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from skidl_layout.alpha_relax import alpha_relax_placement
from skidl_layout.candidates import (
    _with_cluster_zone,
    generate_placement_candidates,
)
from skidl_layout.constraints import BoardOutline, FixedPosition, LayoutConstraints
from skidl_layout.engine import plan_layout
from skidl_layout.hierarchy import extract_groups
from skidl_layout.intent import infer_placement_intents
from skidl_layout.placer import place_parts
from skidl_layout.power import infer_power_topology
from skidl_layout.writer import PlacedPart

from test_layout_engine import BBOXES, _Circuit, _Net, _Part


# ---------------------------------------------------------------------------
# Pure relaxation (fake PlacedParts, no engine)
# ---------------------------------------------------------------------------
_FP = "Capacitor:C_0805"  # (2.0, 1.25) in BBOXES
_FP_BBOXES = {_FP: (2.0, 1.25)}


def _pp(ref, x, y, fp=_FP, rot=0.0):
    return PlacedPart(ref=ref, x_mm=x, y_mm=y, rot_deg=rot, footprint=fp)


def test_relax_deterministic():
    parts = [_pp("A", 0.0, 0.0), _pp("B", 30.0, 0.0), _pp("C", 15.0, 25.0)]
    nets = [("SIG", ["A", "B", "C"])]
    out1 = alpha_relax_placement(parts, nets, _FP_BBOXES)
    out2 = alpha_relax_placement(parts, nets, _FP_BBOXES)
    assert [(p.ref, p.x_mm, p.y_mm) for p in out1] == [
        (p.ref, p.x_mm, p.y_mm) for p in out2
    ]
    # Input list untouched (pure).
    assert (parts[0].x_mm, parts[0].y_mm) == (0.0, 0.0)


def _dist(a, b):
    return ((a.x_mm - b.x_mm) ** 2 + (a.y_mm - b.y_mm) ** 2) ** 0.5


def test_relax_pulls_connected_parts_together():
    # A,B share a net and start far apart; C is unconnected and far away.
    parts = [_pp("A", 0.0, 0.0), _pp("B", 40.0, 0.0), _pp("C", 100.0, 100.0)]
    nets = [("SIG", ["A", "B"])]
    out = alpha_relax_placement(parts, nets, _FP_BBOXES)
    by = {p.ref: p for p in out}
    before_ab = _dist(parts[0], parts[1])
    after_ab = _dist(by["A"], by["B"])
    assert after_ab < before_ab  # connected pair pulled together
    # Order + refs preserved.
    assert [p.ref for p in out] == ["A", "B", "C"]
    # Unconnected C barely moves (drift removal keeps it roughly in place).
    assert _dist(parts[2], by["C"]) < 5.0


def test_relax_fixed_refs_immobile():
    parts = [_pp("A", 0.0, 0.0), _pp("B", 40.0, 0.0)]
    nets = [("SIG", ["A", "B"])]
    constraints = LayoutConstraints(fixed=[FixedPosition("A", 0.0, 0.0)])
    out = alpha_relax_placement(parts, nets, _FP_BBOXES, constraints=constraints)
    by = {p.ref: p for p in out}
    assert (by["A"].x_mm, by["A"].y_mm) == (0.0, 0.0)  # bit-unchanged
    assert by["B"].x_mm != 40.0  # the mobile one relaxed toward A


def test_relax_respects_outline_clamp():
    outline = BoardOutline(20.0, 20.0)
    # Two connected parts that would fly apart; clamp keeps them inside.
    parts = [_pp("A", 2.0, 2.0), _pp("B", 18.0, 18.0)]
    nets = [("SIG", ["A", "B"])]
    constraints = LayoutConstraints(outline=outline)
    out = alpha_relax_placement(parts, nets, _FP_BBOXES, constraints=constraints)
    for p in out:
        w, h = _FP_BBOXES[p.footprint]
        assert outline.x_min + w / 2 - 1e-6 <= p.x_mm <= outline.x_max - w / 2 + 1e-6
        assert outline.y_min + h / 2 - 1e-6 <= p.y_mm <= outline.y_max - h / 2 + 1e-6


# ---------------------------------------------------------------------------
# Engine wiring: default set unchanged, request-only emission, memo safety
# ---------------------------------------------------------------------------
def _relax_circuit():
    """Five same-footprint caps on one star net + GND, laid out far apart by the
    seed so the alpha relaxation has something to move."""
    sig = _Net("SIG")
    gnd = _Net("GND")
    parts = [
        _Part(f"C{i}", value="100nF", footprint=_FP, nets=[sig, gnd])
        for i in range(1, 6)
    ]
    return _Circuit(parts, [sig, gnd])


def _setup(circuit):
    groups = extract_groups(circuit)
    constraints = LayoutConstraints()
    intent_plan = infer_placement_intents(circuit)
    power_topology = infer_power_topology(circuit)
    return groups, constraints, intent_plan, power_topology


def test_default_candidate_set_unchanged():
    circuit = _relax_circuit()
    groups, constraints, intent_plan, power_topology = _setup(circuit)

    without = generate_placement_candidates(
        groups, constraints, BBOXES,
        intent_plan=intent_plan, power_topology=power_topology,
    )
    with_circuit = generate_placement_candidates(
        groups, constraints, BBOXES,
        intent_plan=intent_plan, power_topology=power_topology,
        circuit=circuit,
    )
    # Same names, no alpha_relax, in the same order.
    assert [c.name for c in without] == [c.name for c in with_circuit]
    assert "alpha_relax" not in [c.name for c in with_circuit]
    # Deep: passing circuit alone changed no seed.
    for a, b in zip(without, with_circuit):
        assert [(p.ref, p.x_mm, p.y_mm, p.rot_deg) for p in a.placed_parts] == [
            (p.ref, p.x_mm, p.y_mm, p.rot_deg) for p in b.placed_parts
        ]


def test_alpha_relax_requested_emits_and_transforms():
    circuit = _relax_circuit()
    groups, constraints, intent_plan, power_topology = _setup(circuit)

    cands = generate_placement_candidates(
        groups, constraints, BBOXES,
        intent_plan=intent_plan, power_topology=power_topology,
        requested=["cluster_first", "alpha_relax"], circuit=circuit,
    )
    by_name = {c.name: c for c in cands}
    assert set(by_name) == {"cluster_first", "alpha_relax"}

    cluster = by_name["cluster_first"]
    alpha = by_name["alpha_relax"]

    # cluster_first's seed is the RAW place_parts build (memo non-pollution).
    raw = place_parts(groups, _with_cluster_zone(constraints, intent_plan), BBOXES)
    assert [(p.ref, p.x_mm, p.y_mm, p.rot_deg) for p in cluster.placed_parts] == [
        (p.ref, p.x_mm, p.y_mm, p.rot_deg) for p in raw
    ]
    # id-disjoint PlacedParts between the two candidates.
    for a, b in zip(cluster.placed_parts, alpha.placed_parts):
        assert a is not b

    # alpha_relax actually transformed the seed (fixture is not at equilibrium).
    assert [(p.ref, p.x_mm, p.y_mm) for p in alpha.placed_parts] != [
        (p.ref, p.x_mm, p.y_mm) for p in cluster.placed_parts
    ]
    # Same refs / order preserved.
    assert [p.ref for p in alpha.placed_parts] == [p.ref for p in cluster.placed_parts]


def test_alpha_relax_unknown_when_no_circuit():
    circuit = _relax_circuit()
    groups, constraints, intent_plan, power_topology = _setup(circuit)
    with pytest.raises(ValueError, match="unknown candidate name"):
        generate_placement_candidates(
            groups, constraints, BBOXES,
            intent_plan=intent_plan, power_topology=power_topology,
            requested=["alpha_relax"],  # circuit=None
        )


def test_alpha_relax_engine_end_to_end():
    circuit = _relax_circuit()
    r1 = plan_layout(
        circuit, fp_bboxes=BBOXES, candidate_names=["baseline", "alpha_relax"]
    )
    r2 = plan_layout(
        circuit, fp_bboxes=BBOXES, candidate_names=["baseline", "alpha_relax"]
    )
    assert r1.validation.missing_refs == []
    assert r1.validation.placed_parts == 5
    # Deterministic across two identical requested runs.
    sig1 = [(p.ref, p.x_mm, p.y_mm, p.rot_deg) for p in r1.placed_parts]
    sig2 = [(p.ref, p.x_mm, p.y_mm, p.rot_deg) for p in r2.placed_parts]
    assert sig1 == sig2
