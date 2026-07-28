"""Unit tests for via-in-pad relocation (power-layout Phase 8, WS-B).

No KiCad and no KRT: planning and splicing run against synthetic board text, so
what is under test is the geometry, the same-net rule, the stub track, and the
determinism. The DRC/connectivity ladder that decides which moves *ship* lives
in ``power_copper._relocate_vias_in_pads`` and is exercised on real boards by
``canaries/drive_phase8.py`` (gate Q2).
"""

from __future__ import annotations

import math
import re

import pytest

from skidl_layout import OSHPARK_2L
from skidl_layout.fabspec import _point_in_pad, _smd_copper_pads
from skidl_layout.via_relocate import (
    apply_via_relocations,
    find_vias_in_pads,
    plan_via_relocations,
)


def _board(via_net=1, via_at="15.500000 14.000000", extra_pads="", outline=True):
    """One 20x20 board, an 0805-ish GND pad at (15.5, 14) with a via in it."""
    edge = """
\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.05))""" if outline else ""
    return f"""(kicad_pcb
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "SW"){edge}
\t(footprint "C_0805"
\t\t(at 15.5 13.0)
\t\t(property "Reference" "C1")
\t\t(pad "1" smd roundrect (at 0 -1.0) (size 1.15 1.4)
\t\t\t(layers "F.Cu" "F.Mask" "F.Paste") (net 2 "SW"))
\t\t(pad "2" smd roundrect (at 0 1.0) (size 1.15 1.4)
\t\t\t(layers "F.Cu" "F.Mask" "F.Paste") (net 1 "GND"))
\t){extra_pads}
\t(via
\t\t(at {via_at})
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net {via_net})
\t\t(uuid "aaaaaaaa-1111-2222-3333-444444444444")
\t)
)
"""


def _write(tmp_path, text, name="b.kicad_pcb"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- detection agrees with the fab_check rule ------------------------------

def test_finds_the_via_in_the_pad(tmp_path):
    path = _write(tmp_path, _board())
    hits = find_vias_in_pads(path, OSHPARK_2L)
    assert len(hits) == 1
    via, pad = hits[0]
    assert (pad["ref"], pad["number"]) == ("C1", "2")


def test_a_via_outside_every_pad_is_not_flagged(tmp_path):
    path = _write(tmp_path, _board(via_at="30.000000 30.000000"))
    assert find_vias_in_pads(path, OSHPARK_2L) == []


# --- the same-net rule (step 1 of the algorithm) ---------------------------

def test_a_foreign_net_via_is_left_alone_and_reported(tmp_path):
    """A via of a DIFFERENT net inside a pad is a short, not a relocation job.

    Moving it would paper over a genuine DRC problem, so it is reported and
    never touched.
    """
    path = _write(tmp_path, _board(via_net=2))
    plan = plan_via_relocations(path, OSHPARK_2L)

    assert plan.moves == []
    assert len(plan.foreign) == 1
    assert plan.foreign[0].status == "foreign_net"
    assert "short" in plan.foreign[0].reason
    assert plan.in_pad_count == 1


# --- the ring geometry -----------------------------------------------------

def test_relocated_via_leaves_the_pad_and_clears_it(tmp_path):
    path = _write(tmp_path, _board())
    plan = plan_via_relocations(path, OSHPARK_2L)

    assert len(plan.relocatable) == 1
    move = plan.relocatable[0]
    pads = {(p["ref"], p["number"]): p for p in _smd_copper_pads(
        __import__("simp_sexp").Sexp(open(path, encoding="utf-8").read()))}
    pad = pads[("C1", "2")]

    # Out of its own pad...
    assert not _point_in_pad(move.new_x, move.new_y, pad)
    # ...and out of every other pad too.
    for other in pads.values():
        assert not _point_in_pad(move.new_x, move.new_y, other)
    # ...at a radius that keeps the whole via body outside the rectangle.
    reach = math.hypot(move.new_x - pad["x"], move.new_y - pad["y"])
    assert reach >= math.hypot(pad["w"], pad["h"]) / 2.0


def test_candidate_order_is_deterministic_by_angle(tmp_path):
    """Angle ascending from +x, never nearest-first-with-ties: the pour boundary
    already moves run-to-run, and a distance tie-break would add more drift."""
    path = _write(tmp_path, _board())
    first = plan_via_relocations(path, OSHPARK_2L).relocatable[0]
    second = plan_via_relocations(path, OSHPARK_2L).relocatable[0]

    assert (first.new_x, first.new_y) == (second.new_x, second.new_y)
    assert first.angle_deg == 0.0          # +x is tried first and is clear here


def test_no_legal_position_leaves_the_via_in_place(tmp_path):
    """⛔ Nothing is ever dropped. These are plane STITCHING vias: an unresolved
    via-in-pad is a note, an orphaned ground pad is a broken board."""
    # Box the pad in with foreign-net copper on every side.
    walls = "".join(
        f"""
\t(footprint "WALL{i}"
\t\t(at {15.5 + dx} {14.0 + dy})
\t\t(property "Reference" "W{i}")
\t\t(pad "1" smd rect (at 0 0) (size 3.0 3.0)
\t\t\t(layers "F.Cu" "F.Mask") (net 2 "SW"))
\t)"""
        for i, (dx, dy) in enumerate(
            [(2.2, 0), (-2.2, 0), (0, 2.2), (0, -2.2),
             (1.8, 1.8), (-1.8, 1.8), (1.8, -1.8), (-1.8, -1.8)]))
    path = _write(tmp_path, _board(extra_pads=walls))

    plan = plan_via_relocations(path, OSHPARK_2L)

    assert plan.relocatable == []
    assert len(plan.unresolved) == 1
    assert "STAYS where it is" in plan.unresolved[0].reason


def test_board_outline_is_respected(tmp_path):
    """A candidate outside the Edge.Cuts keepout is not a candidate."""
    # Pad hard against the right edge: the +x candidates fall off the board.
    text = _board().replace("(at 15.5 13.0)", "(at 39.4 13.0)").replace(
        "15.500000 14.000000", "39.400000 14.000000")
    plan = plan_via_relocations(_write(tmp_path, text), OSHPARK_2L)

    if plan.relocatable:
        move = plan.relocatable[0]
        assert move.new_x <= 40.0 - OSHPARK_2L.board_edge_keepout_mm
        assert move.angle_deg != 0.0        # +x was rejected by the outline


# --- splicing --------------------------------------------------------------

def test_apply_moves_the_via_and_adds_one_stub(tmp_path):
    """The via's ``(at ...)`` is rewritten in place -- count, net, size, drill
    all untouched -- and exactly one stub segment joins the pad to it."""
    path = _write(tmp_path, _board())
    out = str(tmp_path / "out.kicad_pcb")
    plan = plan_via_relocations(path, OSHPARK_2L)

    moved = apply_via_relocations(path, out, plan)

    assert moved == 1
    before = open(path, encoding="utf-8").read()
    after = open(out, encoding="utf-8").read()
    assert len(re.findall(r"\(via\b", after)) == len(re.findall(r"\(via\b", before))
    assert len(re.findall(r"\(segment\b", after)) == 1
    assert find_vias_in_pads(out, OSHPARK_2L) == []
    # The stub runs from the via's ORIGINAL position (inside the pad) to the new
    # one, on the pad's own copper layer, on the pad's net.
    stub = re.search(r"\(segment(.*?)\n\t\)", after, re.S).group(1)
    assert "15.500000 14.000000" in stub
    assert f"{plan.relocatable[0].new_x:.6f}" in stub
    assert '(layer "F.Cu")' in stub
    assert "(net 1)" in stub


def test_splice_is_byte_deterministic(tmp_path):
    """Deterministic ``uuid5`` ids -- re-running produces the same file."""
    path = _write(tmp_path, _board())
    a, b = str(tmp_path / "a.kicad_pcb"), str(tmp_path / "b2.kicad_pcb")
    apply_via_relocations(path, a, plan_via_relocations(path, OSHPARK_2L))
    apply_via_relocations(path, b, plan_via_relocations(path, OSHPARK_2L))

    assert open(a, encoding="utf-8").read() == open(b, encoding="utf-8").read()


def test_applying_nothing_copies_the_board_unchanged(tmp_path):
    path = _write(tmp_path, _board(via_net=2))       # foreign net -> no moves
    out = str(tmp_path / "out.kicad_pcb")

    assert apply_via_relocations(path, out, plan_via_relocations(path, OSHPARK_2L)) == 0
    assert open(out, encoding="utf-8").read() == open(path, encoding="utf-8").read()


def test_only_filter_applies_a_subset(tmp_path):
    """The caller's per-via fallback ladder needs to accept moves one at a time."""
    path = _write(tmp_path, _board())
    out = str(tmp_path / "out.kicad_pcb")
    plan = plan_via_relocations(path, OSHPARK_2L)

    assert apply_via_relocations(path, out, plan, only=set()) == 0
    assert open(out, encoding="utf-8").read() == open(path, encoding="utf-8").read()


def test_no_spec_is_a_reported_refusal_not_a_crash(tmp_path):
    plan = plan_via_relocations(_write(tmp_path, _board()), None)

    assert plan.moves == []
    assert plan.notes and "FabSpec" in plan.notes[0]
