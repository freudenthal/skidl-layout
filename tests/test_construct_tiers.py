# -*- coding: utf-8 -*-
"""S7 A1/B1 -- the route set (the denominator) and the containment order.

⛔ Both are **pure functions over a Partition**, so they are tested on a
hand-built one: running them on a corpus board would test the corpus, not the
rule. The corpus numbers are the S7 driver's gates ``RT1``/``RT2``.

⛔⛔ :mod:`skidl_layout.construct` is still a **leaf** -- these tests import it,
nothing in the package does.
"""

from __future__ import annotations

import pytest

from skidl_layout.construct import (
    EXCLUSION_REASONS,
    TIER_NAMES,
    TIER_ORDERS,
    ConstructError,
    containment_cells,
    exclusion_census,
    net_tiers,
    nets_by_tier,
    route_set,
    tier_census,
)


def _tier_partition():
    """One ``ic`` cell (``U1`` + ``M1``), two family templates, one stranger.

    ``family:divider`` has a port on ``U1`` (``FB``), so it is INSIDE the L1
    cell; ``family:out`` shares **no** net with ``U1``, so it is not. ``TP1``
    carries a net no other part does, and ``J1`` carries an autoname.

    ⚠ Net names are chosen so that **alphabet and demand disagree** (``ZC`` has
    two parts, ``FB``/``GATE`` three, ``SW`` one). A fixture where the four
    order arms collapse onto two would let a broken knob pass.
    ⚠ And ``OUTX``/``SNUB`` are deliberately not called ``VOUT``: ``VOUT`` is a
    **plane** net by :func:`~skidl_layout.ratnest.is_plane_net`, so a fixture
    built on it would test the plane predicate instead of the rule.
    """
    from skidl_layout.cells_partition import CellGroup, Partition

    groups = (
        CellGroup(name="ic:U1", kind="ic", refs=("M1", "RG", "U1"),
                  anchor="U1", family=None, topology=None),
        CellGroup(name="family:divider", kind="family", refs=("RFB1", "RFB2"),
                  anchor=None, family="divider", topology="junction"),
        CellGroup(name="family:out", kind="family", refs=("COUT", "ROUT"),
                  anchor=None, family="rc", topology="junction"),
        CellGroup(name="singleton:J1", kind="singleton", refs=("J1",),
                  anchor=None, family=None, topology=None),
    )
    nets_by_ref = {
        "U1": ["FB", "GATE", "GND", "VIN", "ZC"],
        "M1": ["GATE", "GND", "SW"],
        "RG": ["GATE", "ZC"],
        "RFB1": ["FB", "OUTX"],
        "RFB2": ["FB", "GND"],
        "COUT": ["OUTX", "SNUB"],
        "ROUT": ["SNUB"],
        "J1": ["OUTX", "GND", "N$7"],
        "TP1": ["DANGLE"],
    }
    return Partition(board="tiny", groups=groups, parts_total=9,
                     unassigned=(), meta={"nets_by_ref": nets_by_ref})


# --------------------------------------------------------------------------- #
# A1 -- the denominator
# --------------------------------------------------------------------------- #
def test_the_route_set_names_a_reason_for_every_net_it_drops():
    part = _tier_partition()
    placed = set(part.meta["nets_by_ref"])
    requested, excluded = route_set(part, placed)
    # ⛔ TOTAL: every net the netlist carries is on exactly one side of the line.
    every = {net for nets in part.meta["nets_by_ref"].values() for net in nets}
    assert set(requested) | {row["net"] for row in excluded} == every
    assert len(requested) + len(excluded) == len(every)
    assert all(row["reason"] in EXCLUSION_REASONS for row in excluded)
    census = exclusion_census(excluded)
    assert census["plane"] == 2, "GND and VIN"
    assert census["autoname"] == 1, "N$7"
    assert census["single_part_net"] == 2, "SW and DANGLE"
    # ⛔ a declared reason that matched nothing is REPORTED as 0, never omitted.
    assert set(census) == set(EXCLUSION_REASONS)


def test_the_two_route_set_rules_converge_when_nothing_is_parked():
    """⛔ S7 A2, and it is gate ``RT6``'s assertion one level down."""
    part = _tier_partition()
    everything = set(part.meta["nets_by_ref"])
    pair, _e = route_set(part, everything, rule="placed_pair")
    allp, _e2 = route_set(part, everything, rule="all_placed")
    assert pair == allp


def test_all_placed_drops_a_net_whose_other_part_is_parked():
    part = _tier_partition()
    parked = set(part.meta["nets_by_ref"]) - {"J1"}
    pair, _e = route_set(part, parked, rule="placed_pair")
    allp, excluded = route_set(part, parked, rule="all_placed")
    # ``OUTX`` is COUT + J1 + RFB1: two of them are placed, so ``placed_pair``
    # keeps it and ``all_placed`` does not.
    assert "OUTX" in pair and "OUTX" not in allp
    assert set(allp) < set(pair)
    assert {row["reason"] for row in excluded
            if row["net"] == "OUTX"} == {"unplaced_part"}


def test_a_net_with_one_placed_part_is_dropped_by_both_rules():
    part = _tier_partition()
    placed = set(part.meta["nets_by_ref"]) - {"ROUT"}
    for rule in ("placed_pair", "all_placed"):
        requested, excluded = route_set(part, placed, rule=rule)
        assert "SNUB" not in requested
        assert {row["reason"] for row in excluded
                if row["net"] == "SNUB"} == {"single_placed_part"}


def test_an_empty_route_set_raises_rather_than_reporting_no_failures():
    with pytest.raises(ConstructError, match="EMPTY"):
        route_set(_tier_partition(), set())


def test_an_unknown_route_set_rule_raises():
    with pytest.raises(ConstructError):
        route_set(_tier_partition(), {"U1"}, rule="whatever")


# --------------------------------------------------------------------------- #
# B1 -- the containment order
# --------------------------------------------------------------------------- #
def test_the_containment_order_puts_a_ported_family_inside_the_l1_cell():
    """⛔ The L1 membership rule IS ``side_neighbours``' candidate list: the
    anchor group's own refs plus every family with a port on the anchor."""
    l0, l1 = containment_cells(_tier_partition())
    assert set(l0) == {"family:divider", "family:out"}
    assert "ic:U1" not in l0, "an ic group CONTAINS templates; it is not a leaf"
    assert "singleton:J1" not in l0, "a 1-member group is not a template"
    assert l1["ic:U1"] == frozenset({"U1", "M1", "RG", "RFB1", "RFB2"})
    assert "COUT" not in l1["ic:U1"], "family:out has no port on U1"


def test_net_tiers_assigns_the_smallest_covering_cell():
    part = _tier_partition()
    rows = net_tiers(part, ["FB", "GATE", "SNUB", "OUTX"])
    by_net = {row.net: row for row in rows}
    # SNUB is internal to family:out -> tier 0.
    assert (by_net["SNUB"].tier, by_net["SNUB"].cell) == (0, "family:out")
    # FB is U1 + RFB1 + RFB2 -- no L0 covers it, the L1 cell does.
    assert (by_net["FB"].tier, by_net["FB"].cell) == (1, "ic:U1")
    assert by_net["GATE"].tier == 1
    # OUTX reaches J1, which is in no cell at all.
    assert (by_net["OUTX"].tier, by_net["OUTX"].cell) == (2, "")
    assert by_net["OUTX"].tier_name == TIER_NAMES[2]
    assert tier_census(rows) == {"l0_template": 1, "l1_cell": 2, "board": 1}


def test_net_tiers_is_a_partition_of_its_input_and_is_order_blind():
    part = _tier_partition()
    nets = ["FB", "GATE", "SNUB", "OUTX", "SW"]
    first = [row.to_dict() for row in net_tiers(part, nets)]
    second = [row.to_dict() for row in net_tiers(part, list(reversed(nets)))]
    assert first == second, "the tiering may not depend on arrival order"
    assert len(first) == len(set(nets))
    assert sorted(row["net"] for row in first) == sorted(set(nets))


def test_net_tiers_raises_on_an_empty_set_and_on_a_net_no_part_carries():
    part = _tier_partition()
    with pytest.raises(ConstructError, match="EMPTY"):
        net_tiers(part, [])
    with pytest.raises(ConstructError, match="NO part"):
        net_tiers(part, ["NOT_A_NET"])


# --------------------------------------------------------------------------- #
# B3 -- the order knob
# --------------------------------------------------------------------------- #
def test_the_order_knob_permutes_within_a_tier_and_never_across_one():
    part = _tier_partition()
    rows = net_tiers(part, ["FB", "GATE", "SW", "SNUB", "OUTX", "ZC"])
    members = {index: set(nets)
               for index, nets in nets_by_tier(rows, order="canonical").items()}
    for order in TIER_ORDERS:
        got = nets_by_tier(rows, order=order)
        assert {index: set(nets) for index, nets in got.items()} == members, \
            "an order arm may not move a net between tiers"
    canonical = nets_by_tier(rows, order="canonical")[1]
    assert nets_by_tier(rows, order="reversed")[1] == list(reversed(canonical))
    # ``demand`` is widest-net-first: FB and GATE (3 parts) lead ZC (2), SW (1).
    assert nets_by_tier(rows, order="demand")[1] == ["FB", "GATE", "ZC", "SW"]
    assert nets_by_tier(rows, order="narrow")[1] == ["SW", "ZC", "FB", "GATE"]
    assert nets_by_tier(rows, order="canonical")[1] == ["FB", "GATE", "SW",
                                                       "ZC"]


def test_every_declared_order_arm_is_reachable_and_none_is_a_duplicate():
    """⛔ Standing finding 20: a declared arm that is a copy of another is a
    constant wearing an arm's hat."""
    part = _tier_partition()
    rows = net_tiers(part, ["FB", "GATE", "SW", "SNUB", "OUTX", "ZC"])
    seen = {order: tuple(nets_by_tier(rows, order=order)[1])
            for order in TIER_ORDERS}
    assert len(set(seen.values())) == len(TIER_ORDERS), seen


def test_an_unknown_order_arm_raises():
    rows = net_tiers(_tier_partition(), ["FB"])
    with pytest.raises(ConstructError):
        nets_by_tier(rows, order="whatever")
