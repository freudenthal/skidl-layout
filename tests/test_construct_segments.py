# -*- coding: utf-8 -*-
"""S8 STAGE A -- ``segment_tiers``: a net decomposed into intra-cell pieces.

⛔⛔ **The thing being tested is a REFINEMENT of :func:`net_tiers`, not a
replacement.** S7's whole-net rule assigns *"the smallest cell covering EVERY
part"*, so a template's port net can never be tiered with its own template --
``VFB`` (``RFB1``, ``RFB2``, ``U1``) lands in ``ic:U1`` because it touches the
anchor by definition. Stage A's rule tiers **pad subsets**, and the assertion
that matters is that the two disagree in exactly that way.

⛔ Pure functions over a ``Partition``, tested on a hand-built one: running them
on a corpus board would test the corpus. The corpus numbers are the S8 driver's
gate ``PT1``.

⛔⛔ :mod:`skidl_layout.construct` is still a **leaf** -- these tests import it,
nothing in the package does.
"""

from __future__ import annotations

import pytest

from skidl_layout.construct import (
    BRIDGE_TIER_RULES,
    SEGMENT_KINDS,
    TIER_NAMES,
    TIER_ORDERS,
    ConstructError,
    net_tiers,
    segment_census,
    segment_tiers,
    segments_by_tier,
)

from test_construct_tiers import _tier_partition


def _two_template_partition():
    """Two L0 templates, one net touching **both** and nothing else.

    ⚠ Built separately and deliberately: this is the only shape that reaches the
    *"claimed segments with nothing left over"* bridge, and a branch reached by
    no test is a branch that is not there.
    """
    from skidl_layout.cells_partition import CellGroup, Partition

    groups = (
        CellGroup(name="ic:U1", kind="ic", refs=("U1",), anchor="U1",
                  family=None, topology=None),
        CellGroup(name="family:a", kind="family", refs=("RA1", "RA2"),
                  anchor=None, family="divider", topology="junction"),
        CellGroup(name="family:b", kind="family", refs=("RB1", "RB2"),
                  anchor=None, family="divider", topology="junction"),
    )
    nets_by_ref = {
        "U1": ["VIN2"],
        "RA1": ["LINK", "AINT"], "RA2": ["LINK", "AINT"],
        "RB1": ["LINK", "BINT"], "RB2": ["LINK", "BINT"],
    }
    return Partition(board="two", groups=groups, parts_total=5,
                     unassigned=(), meta={"nets_by_ref": nets_by_ref})


def _by_net(rows):
    out: dict = {}
    for row in rows:
        out.setdefault(row.net, []).append(row)
    return out


# --------------------------------------------------------------------------- #
# The motivating case -- and the disagreement with net_tiers that motivates it
# --------------------------------------------------------------------------- #
def test_the_ported_divider_net_splits_into_an_internal_and_a_bridge():
    """⭐⭐⭐ **The S8 motivating case**, in the fixture's own names: ``FB`` is
    ``lt3844_buck``'s ``VFB`` -- two divider resistors and the anchor."""
    part = _tier_partition()
    segments = _by_net(segment_tiers(part, ["FB"]))["FB"]
    assert len(segments) == 2, [s.to_dict() for s in segments]
    internal, bridge = segments
    assert internal.kind == "internal"
    assert (internal.tier, internal.cell) == (0, "family:divider")
    assert internal.parts == ("RFB1", "RFB2")
    assert internal.taps == ()
    assert bridge.kind == "bridge"
    assert (bridge.tier, bridge.cell) == (1, "ic:U1")
    assert bridge.parts == ("U1",)
    # ⛔ The tap is BESIDE the parts, never inside them -- ``RFB1`` is already
    # the divider segment's, and a part claimed twice is not a partition.
    assert bridge.taps == ("RFB1",)
    assert bridge.terminals == ("RFB1", "U1")


def test_segment_tiers_disagrees_with_net_tiers_exactly_where_the_plan_says():
    """⛔⛔ The whole reason Stage A exists, asserted rather than described."""
    part = _tier_partition()
    whole = {row.net: row for row in net_tiers(part, ["FB"])}["FB"]
    # S7: one row, and it is the L1 cell -- the divider cannot own its own net.
    assert (whole.tier, whole.cell) == (1, "ic:U1")
    assert whole.parts == ("RFB1", "RFB2", "U1")
    # S8: the divider owns the half of it that is inside the divider.
    first = segment_tiers(part, ["FB"])[0]
    assert (first.tier, first.cell, first.kind) == (0, "family:divider",
                                                    "internal")


def test_a_net_wholly_inside_one_template_yields_one_internal_and_no_bridge():
    """⭐ ``lt3844_buck``'s ``VC_C`` -- the snubber's own internal node."""
    segments = _by_net(segment_tiers(_tier_partition(), ["SNUB"]))["SNUB"]
    assert len(segments) == 1
    assert (segments[0].kind, segments[0].tier, segments[0].cell) == (
        "internal", 0, "family:out")
    assert segments[0].parts == ("COUT", "ROUT")
    assert segments[0].taps == ()


def test_a_net_no_cell_holds_two_of_degenerates_to_todays_whole_net_behaviour():
    part = _tier_partition()
    segments = _by_net(segment_tiers(part, ["OUTX"]))["OUTX"]
    assert len(segments) == 1
    assert segments[0].kind == "bridge"
    assert (segments[0].tier, segments[0].cell) == (2, "")
    assert segments[0].parts == ("COUT", "J1", "RFB1")
    assert segments[0].taps == ()
    # ⛔ ...and that is exactly what net_tiers says about it, one row for one net.
    whole = net_tiers(part, ["OUTX"])[0]
    assert (whole.tier, whole.cell) == (2, "")


def test_an_internal_segment_can_live_at_l1_when_the_cell_holds_the_pair():
    """⚠ ``internal`` is not a synonym for ``l0_template``: an L1 cell holding
    two of the net's parts claims them just the same."""
    segments = _by_net(segment_tiers(_tier_partition(), ["GATE"]))["GATE"]
    assert len(segments) == 1
    assert (segments[0].kind, segments[0].tier, segments[0].cell) == (
        "internal", 1, "ic:U1")
    assert segments[0].parts == ("M1", "RG", "U1")


def test_a_remainder_no_cell_covers_bridges_at_board_tier_with_a_tap():
    segments = _by_net(segment_tiers(_tier_partition(), ["GND"]))["GND"]
    assert [s.kind for s in segments] == ["internal", "bridge"]
    assert (segments[0].tier, segments[0].cell) == (1, "ic:U1")
    assert segments[0].parts == ("M1", "RFB2", "U1")
    assert (segments[1].tier, segments[1].cell) == (2, "")
    assert segments[1].parts == ("J1",)
    assert segments[1].taps == ("M1",)


def test_two_internal_segments_with_nothing_left_over_still_get_a_join():
    """⛔ Two templates and no remainder: the bridge carries **no part of its
    own** and two taps. Without it the decomposition would claim the net is
    routed once each template is, which is false."""
    segments = _by_net(segment_tiers(_two_template_partition(),
                                     ["LINK"]))["LINK"]
    assert [s.kind for s in segments] == ["internal", "internal", "bridge"]
    assert segments[0].parts == ("RA1", "RA2")
    assert segments[1].parts == ("RB1", "RB2")
    assert segments[2].parts == ()
    assert segments[2].taps == ("RA1", "RB1")
    assert segments[2].terminals == ("RA1", "RB1")
    assert segments[2].tier == 2 and segments[2].cell == ""


# --------------------------------------------------------------------------- #
# Totality, disjointness, determinism -- the net_tiers standard
# --------------------------------------------------------------------------- #
def test_every_nets_segments_partition_its_parts_exactly():
    part = _tier_partition()
    nets = ["FB", "GATE", "GND", "OUTX", "SNUB", "SW", "ZC", "VIN"]
    rows = segment_tiers(part, nets)
    carried: dict = {}
    for ref, its_nets in part.meta["nets_by_ref"].items():
        for net in its_nets:
            carried.setdefault(net, set()).add(ref)
    for net, segments in _by_net(rows).items():
        claimed = [ref for s in segments for ref in s.parts]
        assert sorted(claimed) == sorted(carried[net]), net
        assert len(set(claimed)) == len(claimed), f"{net}: a part claimed twice"
        assert segments, net
        # ⛔ A tap is always owned by a SIBLING segment of the same net.
        owned = set(claimed)
        for segment in segments:
            assert set(segment.taps) <= owned - set(segment.parts)
            assert segment.kind in SEGMENT_KINDS


def test_segment_tiers_is_blind_to_arrival_order_of_the_nets():
    part = _tier_partition()
    nets = ["FB", "GATE", "GND", "OUTX", "SNUB", "ZC"]
    first = [row.to_dict() for row in segment_tiers(part, nets)]
    second = [row.to_dict() for row in segment_tiers(part, list(reversed(nets)))]
    assert first == second


def test_segment_tiers_is_blind_to_the_order_of_the_groups_and_the_netlist():
    """⛔ The ``net_tiers`` standard: a permutation of the partition's own
    containers may not move a segment (standing finding 17's shape)."""
    from skidl_layout.cells_partition import Partition

    part = _tier_partition()
    nets = ["FB", "GATE", "GND", "OUTX", "SNUB", "ZC"]
    first = [row.to_dict() for row in segment_tiers(part, nets)]
    flipped = Partition(board=part.board, groups=tuple(reversed(part.groups)),
                        parts_total=part.parts_total,
                        unassigned=part.unassigned,
                        meta={"nets_by_ref": dict(reversed(list(
                            part.meta["nets_by_ref"].items())))})
    assert [row.to_dict() for row in segment_tiers(flipped, nets)] == first


def test_segment_tiers_raises_on_an_empty_set_and_on_a_net_no_part_carries():
    part = _tier_partition()
    with pytest.raises(ConstructError, match="EMPTY"):
        segment_tiers(part, [])
    with pytest.raises(ConstructError, match="NO part"):
        segment_tiers(part, ["NOT_A_NET"])


def test_net_tiers_is_untouched_by_stage_a():
    """⛔ S7's recorded arms must stay reproducible: ``net_tiers`` still gives
    one row per net and still puts ``FB`` in the L1 cell."""
    part = _tier_partition()
    rows = net_tiers(part, ["FB", "GATE", "SNUB", "OUTX"])
    assert len(rows) == 4
    assert {row.net for row in rows} == {"FB", "GATE", "SNUB", "OUTX"}


# --------------------------------------------------------------------------- #
# The census and the order knob
# --------------------------------------------------------------------------- #
def test_the_census_counts_every_declared_key_including_the_zeroes():
    rows = segment_tiers(_tier_partition(), ["FB", "GATE", "SNUB", "OUTX"])
    census = segment_census(rows)
    assert census["nets"] == 4
    assert census["segments"] == census["internal"] + census["bridge"]
    assert census["segments"] == len(rows)
    # FB, GATE and SNUB each gain one; OUTX gains none.
    assert census["nets_with_an_internal_segment"] == 3
    assert set(census["internal_by_tier"]) == set(TIER_NAMES)
    assert set(census["bridge_by_tier"]) == set(TIER_NAMES)
    assert census["internal_by_tier"]["l0_template"] == 2
    assert census["bridge_by_tier"]["board"] == 1


def test_the_order_knob_permutes_within_a_tier_and_never_across_one():
    rows = segment_tiers(_tier_partition(),
                         ["FB", "GATE", "GND", "OUTX", "SNUB", "ZC"])
    members = {index: {(s.net, s.kind) for s in segments}
               for index, segments in
               segments_by_tier(rows, order="canonical").items()}
    for order in TIER_ORDERS:
        got = segments_by_tier(rows, order=order)
        assert {index: {(s.net, s.kind) for s in segments}
                for index, segments in got.items()} == members, order
    canonical = segments_by_tier(rows, order="canonical")[1]
    assert segments_by_tier(rows, order="reversed")[1] == list(
        reversed(canonical))
    demand = [len(s.terminals)
              for s in segments_by_tier(rows, order="demand")[1]]
    assert demand == sorted(demand, reverse=True)
    narrow = [len(s.terminals)
              for s in segments_by_tier(rows, order="narrow")[1]]
    assert narrow == sorted(narrow)


def test_an_unknown_segment_order_arm_raises():
    rows = segment_tiers(_tier_partition(), ["FB"])
    with pytest.raises(ConstructError):
        segments_by_tier(rows, order="whatever")


# --------------------------------------------------------------------------- #
# The bridge-tier arm -- declared because the plan's sentence does not decide it
# --------------------------------------------------------------------------- #
def test_both_bridge_tier_rules_agree_on_the_plans_own_motivating_case():
    """⛔ Neither rule can be chosen by reading the plan: on ``FB`` they give
    the same answer, which is the only case the plan states."""
    part = _tier_partition()
    for rule in BRIDGE_TIER_RULES:
        bridge = _by_net(segment_tiers(part, ["FB"],
                                       bridge_tier_rule=rule))["FB"][1]
        assert (bridge.tier, bridge.cell, bridge.parts, bridge.taps) == (
            1, "ic:U1", ("U1",), ("RFB1",)), rule


def test_the_two_bridge_tier_rules_differ_when_the_remainder_is_empty():
    """⭐ ``lt3844_buck``'s ``SW`` in miniature: two claimed segments, nothing
    left over, and both taps inside one cell. ``remainder`` has nothing to
    cover and lands at ``board``; ``terminals`` asks the taps to fit."""
    from skidl_layout.cells_partition import CellGroup, Partition

    groups = (
        CellGroup(name="ic:U1", kind="ic", refs=("U1", "RA1", "RA2"),
                  anchor="U1", family=None, topology=None),
        CellGroup(name="family:a", kind="family", refs=("RA1", "RA2"),
                  anchor=None, family="divider", topology="junction"),
        CellGroup(name="family:b", kind="family", refs=("RB1", "RB2"),
                  anchor=None, family="divider", topology="junction"),
    )
    nets_by_ref = {"U1": ["PORTA", "PORTB"],
                   "RA1": ["PORTA", "SPAN"], "RA2": ["PORTA", "SPAN"],
                   "RB1": ["PORTB", "SPAN"], "RB2": ["PORTB", "SPAN"]}
    part = Partition(board="span", groups=groups, parts_total=5,
                     unassigned=(), meta={"nets_by_ref": nets_by_ref})
    # family:b has a port on U1 (PORTB), so the L1 cell swallows all four.
    plan = _by_net(segment_tiers(part, ["SPAN"],
                                 bridge_tier_rule="remainder"))["SPAN"][-1]
    other = _by_net(segment_tiers(part, ["SPAN"],
                                  bridge_tier_rule="terminals"))["SPAN"][-1]
    assert plan.kind == other.kind == "bridge"
    assert plan.parts == other.parts == ()
    assert plan.taps == other.taps == ("RA1", "RB1")
    assert (plan.tier, plan.cell) == (2, "")
    assert (other.tier, other.cell) == (1, "ic:U1")


def test_the_bridge_tier_rule_never_moves_an_internal_segment():
    part = _tier_partition()
    nets = ["FB", "GATE", "GND", "OUTX", "SNUB", "ZC"]
    inner = {rule: [s.to_dict() for s in
                    segment_tiers(part, nets, bridge_tier_rule=rule)
                    if s.kind == "internal"]
             for rule in BRIDGE_TIER_RULES}
    assert inner["remainder"] == inner["terminals"]


def test_an_unknown_bridge_tier_rule_raises():
    with pytest.raises(ConstructError, match="bridge_tier_rule"):
        segment_tiers(_tier_partition(), ["FB"], bridge_tier_rule="whatever")
