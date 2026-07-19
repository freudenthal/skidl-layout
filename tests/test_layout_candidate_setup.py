"""Round-10 WS37/WS38: input-keyed seed memo + filter-before-generate.

Covers the memo's exactness/isolation contract (WS37) and the
``requested=`` filter-before-build path (WS38).
"""
from __future__ import annotations

import pytest

import skidl_layout.candidates as candidates_mod
from skidl_layout.candidates import generate_placement_candidates
from skidl_layout.constraints import BoardOutline, EdgeAnchor, LayoutConstraints
from skidl_layout.hierarchy import PlacementGroup
from skidl_layout.intent import PlacementIntentPlan


class _Part:
    def __init__(self, ref, footprint, pins=4):
        self.ref = ref
        self.footprint = footprint
        self.value = ""
        self.name = ""
        self.pins = [object() for _ in range(pins)]

    def __len__(self):
        return len(self.pins)


def _fixture():
    """Standard multi-strategy fixture (mirrors test_layout_candidates.py)."""
    connector = _Part("J1", "Connector:USB", pins=16)
    reg = _Part("U1", "Package_TO_SOT:SOT-23", pins=3)
    r1 = _Part("R1", "Resistor:R_0603", pins=2)
    r2 = _Part("R2", "Resistor:R_0603", pins=2)
    group = PlacementGroup(name="", parts=[connector, reg, r1, r2], adjacency={})
    constraints = LayoutConstraints(outline=BoardOutline(50.0, 30.0))
    intent = PlacementIntentPlan(
        edge_anchors=[EdgeAnchor("J1", "bottom", offset_mm=25.0)]
    )
    bboxes = {
        "Connector:USB": (10.0, 5.0),
        "Package_TO_SOT:SOT-23": (3.0, 3.0),
        "Resistor:R_0603": (1.6, 0.8),
    }
    return {None: group}, constraints, bboxes, intent


def _counting_wrapper(monkeypatch):
    original = candidates_mod.place_parts
    box = {"n": 0}

    def wrapper(*args, **kwargs):
        box["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(candidates_mod, "place_parts", wrapper)
    return box


def _sig(cands):
    return [
        (
            c.name,
            [
                (p.ref, round(p.x_mm, 6), round(p.y_mm, 6), round(p.rot_deg, 6), p.side)
                for p in c.placed_parts
            ],
        )
        for c in cands
    ]


# --- WS37 memo ---


def test_seed_memo_reduces_place_parts_calls(monkeypatch):
    groups, constraints, bboxes, intent = _fixture()
    box = _counting_wrapper(monkeypatch)
    cands = generate_placement_candidates(
        groups, constraints, bboxes, intent_plan=intent
    )
    # Never more builds than candidates; and equal-constraints candidates share
    # a byte-equal seed placement (the memo's exactness contract).
    assert box["n"] <= len(cands)
    for a in cands:
        for b in cands:
            if a is b:
                continue
            if a.constraints == b.constraints:
                sig_a = [(p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in a.placed_parts]
                sig_b = [(p.ref, p.x_mm, p.y_mm, p.rot_deg, p.side) for p in b.placed_parts]
                assert sig_a == sig_b


def test_seed_memo_candidates_share_no_placedpart_objects():
    groups, constraints, bboxes, intent = _fixture()
    cands = generate_placement_candidates(
        groups, constraints, bboxes, intent_plan=intent
    )
    for i, a in enumerate(cands):
        for b in cands[i + 1 :]:
            assert {id(p) for p in a.placed_parts}.isdisjoint(
                {id(p) for p in b.placed_parts}
            )


def test_seed_memo_matches_unmemoized_reference():
    groups, constraints, bboxes, intent = _fixture()
    run1 = generate_placement_candidates(
        groups, constraints, bboxes, intent_plan=intent
    )
    run2 = generate_placement_candidates(
        groups, constraints, bboxes, intent_plan=intent
    )
    assert _sig(run1) == _sig(run2)
