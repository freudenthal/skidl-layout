# -*- coding: utf-8 -*-
"""Per-pad clearance override (spacing plan B) -- declaration, edit, geometry.

⛔ The gate that matters is the round-trip: KRT's **own** parser must resolve
whatever form we write into ``pad.local_clearance``, because that is the field
its obstacle map and ``check_drc`` read. Verified here, never by eye.
"""

from __future__ import annotations

import os
import sys

import pytest

from skidl_layout.power_pads import (
    PAD_CLEARANCE_FIELD,
    apply_pad_clearance,
    declared_pad_clearance_refs,
    mark_pad_clearance,
    pad_clearance_value,
    resolve_pad_clearance_targets,
    segment_rect_distance,
    segment_segment_distance,
)


class _Part:
    def __init__(self, ref):
        self.ref = ref
        self.fields = {}


class _Circuit:
    def __init__(self, *parts):
        self.parts = list(parts)


class _Placed:
    def __init__(self, ref):
        self.ref = ref


class _Result:
    def __init__(self, *refs, stages=()):
        self.placed_parts = [_Placed(r) for r in refs]
        self.power_stage_plan = _StagePlan(stages)


class _Stage:
    def __init__(self, ref):
        self.controller_ref = ref


class _StagePlan:
    def __init__(self, refs):
        self.stages = [_Stage(r) for r in refs]


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #

def test_mark_pad_clearance_records_true_and_floats():
    a, b = _Part("U1"), _Part("U2")
    assert mark_pad_clearance(a, b) == ["U1", "U2"]
    assert a.fields[PAD_CLEARANCE_FIELD] is True
    assert mark_pad_clearance(b, clearance_mm=0.3) == ["U2"]
    assert b.fields[PAD_CLEARANCE_FIELD] == 0.3
    assert declared_pad_clearance_refs(_Circuit(a, b)) == {"U1": None, "U2": 0.3}
    assert mark_pad_clearance(b, clear=True) == ["U2"]
    assert declared_pad_clearance_refs(_Circuit(a, b)) == {"U1": None}


def test_mark_pad_clearance_accepts_a_per_side_dict_for_the_next_plan():
    part = _Part("U1")
    mark_pad_clearance(part, clearance_mm={"north": 0.2, "south": 0.4})
    assert part.fields[PAD_CLEARANCE_FIELD] == {"north": 0.2, "south": 0.4}
    warnings: list[str] = []
    # ⚠ Honoured isotropically at its MAXIMUM in this plan, and it says so.
    assert pad_clearance_value({"north": 0.2, "south": 0.4}, None, ref="U1",
                               warnings=warnings) == 0.4
    assert warnings and "NOT honoured per side" in warnings[0]


def test_pad_clearance_value_precedence():
    assert pad_clearance_value(0.3, 0.2) == 0.3        # the part's own number
    assert pad_clearance_value(None, 0.2) == 0.2       # the layout default
    assert pad_clearance_value(None, None) is None     # nothing to apply


def test_resolve_targets_prefers_explicit_then_declared_then_the_classifier():
    u1, u2 = _Part("U1"), _Part("U2")
    circuit = _Circuit(u1, u2)
    placed = ["U1", "U2", "R1"]

    targets, source = resolve_pad_clearance_targets(
        placed_refs=placed, circuit=circuit, clearance_mm=0.25,
        controller_ref="U2")
    assert (targets, source) == ([("U2", 0.25)], "explicit")

    mark_pad_clearance(u1, clearance_mm=0.4)
    targets, source = resolve_pad_clearance_targets(
        placed_refs=placed, circuit=circuit, clearance_mm=0.25)
    assert (targets, source) == ([("U1", 0.4)], "declared")

    # No declaration of ours -> the classifier's controller, at the default.
    targets, source = resolve_pad_clearance_targets(
        placed_refs=placed, circuit=_Circuit(u2), clearance_mm=0.25,
        power_stage_plan=_StagePlan(["U2"]))
    assert targets == [("U2", 0.25)]
    assert source.startswith("escape:")


def test_resolve_targets_drops_a_target_with_no_number_and_says_so():
    part = _Part("U1")
    mark_pad_clearance(part)
    warnings: list[str] = []
    targets, _source = resolve_pad_clearance_targets(
        placed_refs=["U1"], circuit=_Circuit(part), clearance_mm=None,
        warnings=warnings)
    assert targets == []
    assert warnings and "no clearance value resolved" in warnings[0]


def test_resolve_targets_ignores_unplaced_refs():
    part = _Part("U9")
    mark_pad_clearance(part, clearance_mm=0.25)
    targets, source = resolve_pad_clearance_targets(
        placed_refs=["U1"], circuit=_Circuit(part), clearance_mm=0.25)
    assert (targets, source) == ([], "none")


# --------------------------------------------------------------------------- #
# the board edit
# --------------------------------------------------------------------------- #

#: ⚠ The ``(descr …)`` URL carries parentheses INSIDE a quoted string, exactly as
#: the corpus's MSOP-10 controller does. Naive paren counting walks out of the
#: block here, which is why the scanner is string-aware.
_PCB = '''(kicad_pcb
\t(version 20241229)
\t(footprint "MSOP-10"
\t\t(at 10 10)
\t\t(layer "F.Cu")
\t\t(descr "MSOP, 10 Pin (https://example.com/ds.pdf#page=18)")
\t\t(property "Reference" "U1"
\t\t\t(at 0 -2.45 0)
\t\t\t(uuid "aaaa"))
\t\t(attr smd)
\t\t(fp_line
\t\t\t(start -1.6 -1.6)
\t\t\t(end 1.6 -1.6)
\t\t\t(layer "F.SilkS"))
\t\t(pad "1" smd roundrect
\t\t\t(at -1.4 -1)
\t\t\t(size 1.2 0.3)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(roundrect_rratio 0.25)
\t\t\t(net 1 "VC")
\t\t\t(uuid "bbbb"))
\t\t(pad "2" smd roundrect
\t\t\t(at -1.4 -0.5)
\t\t\t(size 1.2 0.3)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 2 "FBX")
\t\t\t(uuid "cccc"))
\t)
\t(footprint "R_0402"
\t\t(at 20 20)
\t\t(layer "F.Cu")
\t\t(property "Reference" "R1"
\t\t\t(at 0 0 0)
\t\t\t(uuid "dddd"))
\t\t(attr smd)
\t\t(pad "1" smd roundrect
\t\t\t(at -0.5 0)
\t\t\t(size 0.6 0.5)
\t\t\t(layers "F.Cu" "F.Paste" "F.Mask")
\t\t\t(net 2 "FBX")
\t\t\t(uuid "eeee"))
\t)
\t(net 0 "")
)
'''


def _board(tmp_path, name="b.kicad_pcb"):
    path = tmp_path / name
    path.write_text(_PCB, encoding="utf-8")
    return str(path)


def test_pad_form_writes_one_token_per_pad_of_the_named_part_only(tmp_path):
    board = _board(tmp_path)
    record = apply_pad_clearance(board, [("U1", 0.25)], form="pad")
    assert record["written"] == 2
    assert record["rows"] == {"U1": {"clearance_mm": 0.25, "form": "pad",
                                    "tokens": 2}}
    text = open(board, encoding="utf-8").read()
    assert text.count("(clearance 0.25)") == 2
    # ⛔ The neighbour is untouched -- the edit is scoped by footprint block, and
    # the (descr) URL's parentheses must not have widened that block.
    r1 = text[text.index('"R_0402"'):]
    assert "(clearance" not in r1


def test_footprint_form_writes_one_token_in_the_header(tmp_path):
    board = _board(tmp_path)
    record = apply_pad_clearance(board, [("U1", 0.2)], form="footprint")
    assert record["written"] == 1
    text = open(board, encoding="utf-8").read()
    assert text.count("(clearance 0.2)") == 1
    # ⛔ Before the first graphic/pad child: that is the window KRT's
    # footprint-level search is bounded to, and a token after it parses as
    # nothing at all.
    assert text.index("(clearance 0.2)") < text.index("(fp_line")


def test_reapplying_replaces_rather_than_stacks(tmp_path):
    board = _board(tmp_path)
    apply_pad_clearance(board, [("U1", 0.25)], form="pad")
    apply_pad_clearance(board, [("U1", 0.35)], form="pad")
    text = open(board, encoding="utf-8").read()
    assert text.count("(clearance") == 2
    assert text.count("(clearance 0.35)") == 2


def test_out_path_leaves_the_input_untouched(tmp_path):
    board = _board(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    apply_pad_clearance(board, [("U1", 0.25)], form="pad", out_path=out)
    assert open(board, encoding="utf-8").read() == _PCB
    assert "(clearance 0.25)" in open(out, encoding="utf-8").read()


def test_no_targets_is_a_no_op_and_writes_nothing(tmp_path):
    board = _board(tmp_path)
    record = apply_pad_clearance(board, [], form="pad")
    assert record == {"form": "pad", "written": 0, "rows": {}, "missing": []}
    assert open(board, encoding="utf-8").read() == _PCB


def test_a_ref_the_board_does_not_carry_is_recorded_not_raised(tmp_path):
    board = _board(tmp_path)
    record = apply_pad_clearance(board, [("U9", 0.25)], form="pad")
    assert record["missing"] == ["U9"]
    assert record["written"] == 0


def test_an_unknown_form_raises(tmp_path):
    with pytest.raises(ValueError):
        apply_pad_clearance(_board(tmp_path), [("U1", 0.25)], form="courtyard")


def test_below_the_fab_minimum_raises(tmp_path):
    from skidl_layout.fabspec import resolve_fab_spec

    spec = resolve_fab_spec("oshpark-2l")
    board = _board(tmp_path)
    with pytest.raises(ValueError, match="min_clearance_mm"):
        apply_pad_clearance(board, [("U1", 0.05)], form="pad", fab_spec=spec)
    # ⛔ And nothing was written before it refused.
    assert open(board, encoding="utf-8").read() == _PCB


# --------------------------------------------------------------------------- #
# the round-trip -- KRT's own parser
# --------------------------------------------------------------------------- #

def _krt_parser():
    root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "KiCadRoutingTools"))
    if not os.path.isdir(root):
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import kicad_parser
    except Exception:                          # noqa: BLE001
        return None
    return kicad_parser


@pytest.mark.parametrize("form", ["pad", "footprint"])
def test_both_forms_reach_pad_local_clearance_in_krts_parser(tmp_path, form):
    """⛔ The gate: ``pad.local_clearance`` is the field the router reads."""
    kicad_parser = _krt_parser()
    if kicad_parser is None:
        pytest.skip("no importable KiCadRoutingTools checkout")

    board = _board(tmp_path, f"{form}.kicad_pcb")
    base = kicad_parser.parse_kicad_pcb(board)
    assert all(pad.local_clearance == 0.0
               for pad in base.footprints["U1"].pads)

    apply_pad_clearance(board, [("U1", 0.25)], form=form)
    pcb = kicad_parser.parse_kicad_pcb(board)
    assert all(pad.local_clearance == pytest.approx(0.25)
               for pad in pcb.footprints["U1"].pads)
    # ⛔ Scoped: the neighbour keeps the global clearance.
    assert all(pad.local_clearance == 0.0
               for pad in pcb.footprints["R1"].pads)


# --------------------------------------------------------------------------- #
# the judge's geometry
# --------------------------------------------------------------------------- #

def test_segment_rect_distance_is_exact_for_the_cases_that_bite():
    rect = (0.0, 0.0, 1.0, 1.0)
    # parallel, clear of the edge
    assert segment_rect_distance(rect, (-1.0, 2.0), (2.0, 2.0)) == pytest.approx(1.0)
    # crossing -> zero
    assert segment_rect_distance(rect, (-1.0, 0.5), (2.0, 0.5)) == 0.0
    # endpoint inside -> zero
    assert segment_rect_distance(rect, (0.5, 0.5), (5.0, 5.0)) == 0.0
    # diagonal near a corner: nearest point is the corner
    assert segment_rect_distance(rect, (2.0, 1.0), (1.0, 2.0)) == pytest.approx(
        0.5 * (2 ** 0.5))
    # degenerate segment behaves as a point
    assert segment_rect_distance(rect, (3.0, 0.5), (3.0, 0.5)) == pytest.approx(2.0)


def test_segment_segment_distance_models_an_oval_pads_capsule():
    """⭐ The oval-pin case: a box model read −0.004 mm at DRC 0 on a DIP-8."""
    # parallel segments
    assert segment_segment_distance((0, 0), (2, 0), (0, 1), (2, 1)) == \
        pytest.approx(1.0)
    # crossing
    assert segment_segment_distance((0, 0), (2, 0), (1, -1), (1, 1)) == 0.0
    # end-to-end, collinear
    assert segment_segment_distance((0, 0), (1, 0), (3, 0), (4, 0)) == \
        pytest.approx(2.0)
    # skew, nearest pair is endpoint-to-interior
    assert segment_segment_distance((0, 0), (0, 2), (1, 1), (3, 3)) == \
        pytest.approx(1.0)
