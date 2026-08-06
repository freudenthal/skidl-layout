# -*- coding: utf-8 -*-
"""S9 STAGES A/B/C/D -- the plane census, the plane route policy, the labelled
ring-2 fallback, and the ring's new L2 surface.

⛔⛔ **What this file is about.** ``ratnest.is_plane_net`` is a *preference* in
three call sites of :mod:`skidl_layout.construct` and a **hard exclusion** in
two -- :func:`~skidl_layout.construct.ring2_subanchor` (no fallback, a named
skip) and :func:`~skidl_layout.construct.route_set` (dropped with the reason
``"plane"``) -- and those two decide **whether a part is placed at all** and
**whether it gets any copper**. On the corpus that is 42 of 148 parts. These
tests pin the instrument that makes it countable and the two knobs that let a
run measure the alternative, ⛔ **without moving a single default**.

⛔ Pure functions over a hand-built ``Partition``: running them on a corpus
board would test the corpus. The corpus numbers are the S9 driver's gates
``PN1``/``PN3``/``PN4``.

⛔⛔ :mod:`skidl_layout.construct` is still a **leaf** -- these tests import it,
nothing in the package does.
"""

from __future__ import annotations

import pytest

from skidl_layout.construct import (
    ARC_EDGE_GAP,
    CORNER_OWNER,
    PLANE_POLICIES,
    SIDE_ASSIGNMENT,
    TIGHTEN_MODE,
    ConstructError,
    exclusion_census,
    plane_report,
    ring2_subanchor,
    route_set,
)


def _plane_partition():
    """One ``ic`` cell, one divider family, and **three plane-only parts**.

    ⛔ The fixture's whole point is the shape the corpus has and the older
    fixtures deliberately avoided: ``CIN``/``COUT`` carry **only** ``VIN``/
    ``GND``/``VOUT``, so they have no plane-free net at all and today's rules
    skip them by name and route nothing of theirs. ``CBULK`` is a second
    ``VIN``/``GND`` part **in the same partition group as** ``CIN``, which is
    what makes the fallback key's *"own group before a stranger's"* clause
    testable rather than decorative.
    """
    from skidl_layout.cells_partition import CellGroup, Partition

    groups = (
        CellGroup(name="ic:U1", kind="ic", refs=("M1", "U1"), anchor="U1",
                  family=None, topology=None),
        CellGroup(name="family:divider", kind="family", refs=("RFB1", "RFB2"),
                  anchor=None, family="divider", topology="junction"),
        CellGroup(name="family:bulk", kind="family", refs=("CBULK", "CIN"),
                  anchor=None, family="bulk", topology="parallel"),
        CellGroup(name="singleton:COUT", kind="singleton", refs=("COUT",),
                  anchor=None, family=None, topology=None),
    )
    nets_by_ref = {
        "U1": ["FB", "GATE", "GND", "VIN", "VOUT"],
        "M1": ["GATE", "GND", "SW"],
        "RFB1": ["FB", "SW"],
        "RFB2": ["FB", "GND"],
        "CIN": ["GND", "VIN"],
        "CBULK": ["GND", "VIN"],
        "COUT": ["GND", "VOUT"],
    }
    return Partition(board="planes", groups=groups, parts_total=7,
                     unassigned=(), meta={"nets_by_ref": nets_by_ref})


# --------------------------------------------------------------------------- #
# Stage A -- plane_report, the denominator that did not exist
# --------------------------------------------------------------------------- #
def test_the_plane_census_is_a_partition_of_the_parts():
    report = plane_report(_plane_partition())
    assert set(report["plane_only"]) == {"CBULK", "CIN", "COUT"}
    # ⛔ TOTAL: exactly one bucket per part, and the union is every part.
    assert not set(report["plane_only"]) & set(report["has_plane_free"])
    assert (sorted(report["plane_only"] + report["has_plane_free"])
            == sorted(report["parts"]))
    assert report["census"]["parts"] == 7
    assert report["census"]["plane_only"] == 3
    assert report["census"]["plane_only_share"] == round(3 / 7, 6)


def test_every_part_carries_its_own_split_of_plane_and_plane_free_nets():
    parts = plane_report(_plane_partition())["parts"]
    assert parts["CIN"]["plane"] == ["GND", "VIN"]
    assert parts["CIN"]["plane_free"] == []
    assert parts["CIN"]["plane_only"] is True
    assert parts["RFB1"]["plane"] == []
    assert parts["RFB1"]["plane_free"] == ["FB", "SW"]
    assert parts["RFB1"]["plane_only"] is False
    # ⭐ A part with BOTH is the interesting third case and it is in the fixture.
    assert parts["U1"]["plane"] == ["GND", "VIN", "VOUT"]
    assert parts["U1"]["plane_free"] == ["FB", "GATE"]


def test_the_plane_nets_carry_their_own_part_counts():
    rows = {row["net"]: row for row in
            plane_report(_plane_partition())["plane_nets"]}
    assert sorted(rows) == ["GND", "VIN", "VOUT"]
    assert rows["VIN"]["parts"] == ["CBULK", "CIN", "U1"]
    assert rows["VIN"]["part_count"] == 3


def test_the_report_names_the_sub_anchor_the_fallback_would_choose():
    """⛔ ONE key, two consumers: the report and :func:`ring2_subanchor` must
    not be able to disagree about who the sub-anchor is (finding 29)."""
    report = plane_report(_plane_partition())
    row = report["nearest"]["CIN"]
    # ⭐ ``CBULK`` shares BOTH plane nets **and** is in ``CIN``'s own group;
    # ``U1`` shares both too, so the group clause is what decides it.
    assert row["ref"] == "CBULK"
    assert row["shared_plane_nets"] == ["GND", "VIN"]
    assert row["same_partition_group"] is True
    assert row["via"] == "plane_fallback"
    placed = sorted(_plane_partition().meta["nets_by_ref"])
    assert ring2_subanchor("CIN", placed,
                           _plane_partition().meta["nets_by_ref"],
                           plane_fallback=True,
                           group_of={ref: g.name
                                     for g in _plane_partition().groups
                                     for ref in g.refs})[0] == row["ref"]


def test_the_report_is_byte_identical_under_a_netlist_permutation():
    """⛔ Determinism: nothing in the census may be keyed on arrival order
    (standing finding 17 -- an artifact may not launder an unstable input)."""
    import json

    base = _plane_partition()
    flipped = _plane_partition()
    nets = flipped.meta["nets_by_ref"]
    flipped.meta["nets_by_ref"] = {
        ref: list(reversed(nets[ref])) for ref in reversed(list(nets))}
    assert (json.dumps(plane_report(base), sort_keys=True)
            == json.dumps(plane_report(flipped), sort_keys=True))


def test_a_census_over_nothing_raises():
    from skidl_layout.cells_partition import Partition

    empty = Partition(board="none", groups=(), parts_total=0, unassigned=(),
                      meta={"nets_by_ref": {}})
    with pytest.raises(ConstructError, match="NO parts"):
        plane_report(empty)


def test_the_report_reports_rather_than_asserts_the_zero_requested_subset():
    """⚠⚠ **The plan asked for an in-function assertion and it is not a
    theorem.** A part whose only plane-free net is an autoname has a plane-free
    net **and** no requested net, so *"parts_with_zero_requested_net is a subset
    of plane_only"* is a measurement. It is a **column**; the gate reads it."""
    from skidl_layout.cells_partition import CellGroup, Partition

    part = Partition(
        board="autoname", groups=(
            CellGroup(name="ic:U1", kind="ic", refs=("U1",), anchor="U1",
                      family=None, topology=None),
            CellGroup(name="singleton:TP1", kind="singleton", refs=("TP1",),
                      anchor=None, family=None, topology=None)),
        parts_total=2, unassigned=(),
        meta={"nets_by_ref": {"U1": ["GND", "VIN", "SIG"],
                              "TP1": ["N$3", "SIG"],
                              "TP2": ["N$3"]}})
    report = plane_report(part)
    assert "TP2" not in report["plane_only"]
    assert "TP2" in report["parts_with_zero_requested_net"]
    assert report["zero_requested_but_not_plane_only"] == ["TP2"]


# --------------------------------------------------------------------------- #
# Stage B -- route_set(planes=), and it moves the DENOMINATOR
# --------------------------------------------------------------------------- #
def test_the_plane_policy_is_declared_and_an_undeclared_one_raises():
    part = _plane_partition()
    placed = set(part.meta["nets_by_ref"])
    assert PLANE_POLICIES == ("exclude", "route")
    with pytest.raises(ConstructError, match="planes="):
        route_set(part, placed, planes="pour")


def test_the_default_plane_policy_is_todays_recorded_behaviour():
    part = _plane_partition()
    placed = set(part.meta["nets_by_ref"])
    assert route_set(part, placed) == route_set(part, placed,
                                                planes="exclude")
    excluded = route_set(part, placed)[1]
    assert exclusion_census(excluded)["plane"] == 3


def test_routing_the_planes_moves_the_denominator_and_keeps_it_total():
    part = _plane_partition()
    placed = set(part.meta["nets_by_ref"])
    off_req, off_exc = route_set(part, placed, planes="exclude")
    on_req, on_exc = route_set(part, placed, planes="route")
    every = {net for nets in part.meta["nets_by_ref"].values() for net in nets}
    for requested, excluded in ((off_req, off_exc), (on_req, on_exc)):
        assert set(requested) | {row["net"] for row in excluded} == every
        assert not set(requested) & {row["net"] for row in excluded}
    # ⛔⛔ The whole methodological point of S9 section 5's two-column rule.
    assert set(on_req) - set(off_req) == {"GND", "VIN", "VOUT"}
    assert exclusion_census(on_exc)["plane"] == 0


def test_a_plane_net_still_meets_every_other_filter():
    """⛔ ``planes="route"`` removes ONE branch, it does not open a second code
    path: a plane net carried by a single part is still dropped, by its own
    reason."""
    from skidl_layout.cells_partition import CellGroup, Partition

    part = Partition(
        board="lonely", groups=(
            CellGroup(name="ic:U1", kind="ic", refs=("U1",), anchor="U1",
                      family=None, topology=None),
            CellGroup(name="singleton:R1", kind="singleton", refs=("R1",),
                      anchor=None, family=None, topology=None)),
        parts_total=2, unassigned=(),
        meta={"nets_by_ref": {"U1": ["GND", "SIG", "V5"], "R1": ["SIG"]}})
    requested, excluded = route_set(part, {"U1", "R1"}, planes="route")
    reasons = {row["net"]: row["reason"] for row in excluded}
    assert requested == ["SIG"]
    assert reasons == {"GND": "single_part_net", "V5": "single_part_net"}


# --------------------------------------------------------------------------- #
# Stage C -- the LABELLED ring-2 plane fallback
# --------------------------------------------------------------------------- #
def _nets():
    return _plane_partition().meta["nets_by_ref"]


def _groups():
    return {ref: g.name for g in _plane_partition().groups for ref in g.refs}


def test_the_fallback_is_off_by_default_and_the_return_shape_is_unchanged():
    """⛔ The OFF arm must be byte-identical to S8's recorded constructions, and
    the two-tuple is part of that."""
    assert ring2_subanchor("CIN", ["U1", "RFB1"], _nets()) is None
    assert ring2_subanchor("RFB2", ["U1", "RFB1"], _nets()) == ("RFB1", ["FB"])


def test_a_plane_free_link_still_wins_and_is_labelled_as_such():
    answer = ring2_subanchor("RFB2", ["U1", "RFB1"], _nets(),
                             plane_fallback=True, group_of=_groups())
    assert answer == ("RFB1", ["FB"], "plane_free")


def test_the_fallback_places_a_plane_only_part_against_a_NAMED_plane_net():
    answer = ring2_subanchor("COUT", ["U1", "M1"], _nets(),
                             plane_fallback=True, group_of=_groups())
    # ⛔ The label is not optional: *"placed against a plane net"* and *"placed
    # against a real link"* are different facts and a consumer must be able to
    # tell them apart.
    assert answer == ("U1", ["GND", "VOUT"], "plane_fallback")


def test_the_fallback_key_prefers_the_parts_own_partition_group():
    """⛔ ``(-shared_plane_nets, not same_partition_group, ref)`` -- and both
    ``CBULK`` and ``U1`` share two plane nets with ``CIN``, so **only** the
    group clause separates them (and it must beat the smaller ref)."""
    assert ring2_subanchor("CIN", ["CBULK", "U1"], _nets(),
                           plane_fallback=True,
                           group_of=_groups())[0] == "CBULK"
    # ⚠ Without the group map the key degenerates to (-shared, ref) and the
    # alphabet decides -- which is exactly why the map is threaded through.
    assert ring2_subanchor("CIN", ["CBULK", "U1"], _nets(),
                           plane_fallback=True)[0] == "CBULK"
    assert ring2_subanchor("COUT", ["CBULK", "U1"], _nets(),
                           plane_fallback=True, group_of=_groups())[0] == "U1"


def test_the_fallback_still_names_a_skip_when_nothing_shares_anything():
    assert ring2_subanchor("CIN", ["RFB1"], _nets(), plane_fallback=True,
                           group_of=_groups()) is None


# --------------------------------------------------------------------------- #
# Stage D -- the ring's L2 surface, VALIDATED
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs,message", [
    ({"ring": True, "arc_edge_gap": "nope"}, "arc_edge_gap"),
    ({"ring": True, "corner_owner": "biggest"}, "corner_owner"),
    ({"tighten_mode": "radius"}, "tighten_mode"),
    ({"side_assignment": "loaded"}, "side_assignment"),
])
def test_construct_board_validates_every_new_declared_value(kwargs, message):
    """⛔ Declared values are validated **before** anything is built, exactly
    like ``unit_source``/``port_side`` -- so an undeclared arm is a caller error
    and never a silently different run."""
    from skidl_layout.construct import construct_board

    with pytest.raises(ConstructError, match=message):
        construct_board(_plane_partition(), units=(), session=None,
                        geometries={}, footprints={}, fab=None, **kwargs)


def test_the_rings_declared_values_are_the_same_tuples_l1_uses():
    """⛔ One vocabulary, two levels. A second tuple is how the two levels drift
    into meaning different things by the same name (finding 29)."""
    assert ARC_EDGE_GAP == ("edge_gap", "fanout")
    assert CORNER_OWNER == ("none", "heavier", "split")
    assert TIGHTEN_MODE == ("part", "ring")
    assert SIDE_ASSIGNMENT == ("favored", "escapable")


def test_the_ring_tighten_mode_refuses_a_ringless_board():
    """⛔ ``tighten_mode="ring"`` with ``ring=False`` is a caller error, not a
    silent fall-back to the per-part pass -- L1's own sentence, at L2."""
    from skidl_layout.construct import construct_board

    with pytest.raises(ConstructError, match="needs ring=True"):
        construct_board(_plane_partition(), units=(), session=None,
                        geometries={}, footprints={}, fab=None,
                        tighten_mode="ring")


# --------------------------------------------------------------------------- #
# ⭐⭐⭐ S10 §2 -- orphan_units, and the accounting that is blind without it
# --------------------------------------------------------------------------- #
def _orphan_fixture():
    """An ``ic`` group of three whose third member shares **no net** with the
    other two -- ``lt3758_iso_flyback``/``RLED``'s exact shape.

    ⛔ ``RLED``'s nets are ``LED_A`` (carried only by ``U2``, which is in a
    different group) and ``VOUT`` (the secondary side), so the L1 cell has
    nothing to place it *against* and skips it by name at ring 2, correctly.
    The defect is what happens next: L2's accounting universe is the set of
    UNIT NAMES, so the part disappears with nobody naming it.
    """
    from skidl_layout.cells_partition import CellGroup, Partition
    from skidl_layout.geometry import FootprintGeometry, PadGeometry

    from test_construct import _Footprint, _Pad

    groups = (
        CellGroup(name="ic:U1", kind="ic", refs=("M1", "RLED", "U1"),
                  anchor="U1", family=None, topology=None),
        CellGroup(name="singleton:U2", kind="singleton", refs=("U2",),
                  anchor=None, family=None, topology=None),
    )
    nets_by_ref = {"U1": ["GATE", "GND"], "M1": ["GATE", "GND"],
                   "RLED": ["LED_A", "VOUT"], "U2": ["LED_A", "GND"]}
    partition = Partition(board="orphan", groups=groups, parts_total=4,
                          unassigned=(), meta={"nets_by_ref": nets_by_ref,
                                               "board": "orphan"})

    def _geometry(ref):
        return FootprintGeometry(
            footprint=f"fp:{ref}",
            pads=[PadGeometry(number="1", x_mm=-0.8, y_mm=0.0, width_mm=0.9,
                              height_mm=0.95, layers=("F.Cu",)),
                  PadGeometry(number="2", x_mm=0.8, y_mm=0.0, width_mm=0.9,
                              height_mm=0.95, layers=("F.Cu",))],
            body_bounds=(-0.8, -0.45, 0.8, 0.45),
            courtyard_bounds=(-1.5, -0.75, 1.5, 0.75))

    def _footprint(ref):
        nets = nets_by_ref[ref]
        return _Footprint([
            _Pad(pad_number="1", net_name=nets[0], component_ref=ref,
                 local_x=-0.8, local_y=0.0, global_x=-0.8, global_y=0.0),
            _Pad(pad_number="2", net_name=nets[1], component_ref=ref,
                 local_x=0.8, local_y=0.0, global_x=0.8, global_y=0.0)])

    geometries = {ref: _geometry(ref) for ref in nets_by_ref}
    footprints = {ref: _footprint(ref) for ref in nets_by_ref}
    return partition, geometries, footprints


class _CellThatDropped:
    """A :class:`CellResult` stand-in that placed ``M1`` and skipped ``RLED``.

    ⛔ The shape the real ladder produces: the skipped member is in the cell's
    own log, where it is attributable, and **not** in ``placements`` -- which is
    exactly why ``unit_from_cell`` cannot see it.
    """

    anchor = "U1"
    legal = True
    routed_fraction = 1.0

    class _Placement:
        ref, x_mm, y_mm, rot_deg = "M1", 3.0, 0.0, 0

    placements = (_Placement(),)
    meta = {"accounting": {"failed": [], "skipped": ["RLED"]},
            "side_order": ["E", "W", "N", "S"]}


def _orphan_units(**kwargs):
    import skidl_layout.construct as C

    partition, geometries, footprints = _orphan_fixture()
    original = C.construct_cell
    C.construct_cell = lambda *a, **k: _CellThatDropped()
    try:
        return C.board_units(partition, geometries=geometries, escape_maps={},
                             footprints=footprints, fab=object(),
                             cell_session=lambda g: object(), **kwargs)
    finally:
        C.construct_cell = original


def test_orphan_units_is_OFF_by_default_and_the_member_vanishes():
    """⛔⛔ **The defect, pinned as behaviour so the fix cannot be undone
    quietly.** With the flag off, ``RLED`` is in no unit at all -- and every
    unit-name counter reads clean, which is what made it invisible."""
    import inspect

    from skidl_layout.construct import board_units

    assert inspect.signature(board_units).parameters[
        "orphan_units"].default is False
    units, _skips = _orphan_units()
    assert {unit.name for unit in units} == {"ic:U1", "singleton:U2"}
    claimed = {ref for unit in units for ref in unit.refs}
    assert "RLED" not in claimed, "the defect, and it is still the OFF arm"
    assert claimed == {"M1", "U1", "U2"}


def test_orphan_units_ON_re_emits_the_dropped_member_as_its_own_unit():
    units, _skips = _orphan_units(orphan_units=True)
    by_name = {unit.name: unit for unit in units}
    assert set(by_name) == {"ic:U1", "orphan:RLED", "singleton:U2"}
    orphan = by_name["orphan:RLED"]
    assert orphan.refs == ("RLED",)
    assert orphan.kind == "singleton"
    assert orphan.group == "ic:U1"
    assert orphan.meta["orphan_of"] == "ic:U1"
    # ⭐ The whole diagnosis, on the unit's face: it shares NO net with the
    # members the cell placed, which is why the cell was right to skip it.
    assert orphan.meta["shared_with_cell"] == []
    assert "shares NO net" in orphan.reason
    assert by_name["ic:U1"].meta["dropped_by_cell"] == ["RLED"]


def test_the_owning_unit_is_otherwise_UNCHANGED_by_the_flag():
    """⛔ The flag adds a unit and one meta key; it may not move the cell."""
    off = {u.name: u.to_dict() for u in _orphan_units()[0]}
    on = {u.name: u.to_dict() for u in _orphan_units(orphan_units=True)[0]}
    assert off["singleton:U2"] == on["singleton:U2"]
    moved = {key for key in set(off["ic:U1"]) | set(on["ic:U1"])
             if off["ic:U1"].get(key) != on["ic:U1"].get(key)}
    assert moved == {"meta"}, f"the flag moved {moved} on the owning unit"
    assert (set(on["ic:U1"]["meta"]) - set(off["ic:U1"]["meta"])
            == {"dropped_by_cell"})


# --------------------------------------------------------------------------- #
# ⭐⭐⭐ S10 §2 -- the accounting, totalled over PARTS
# --------------------------------------------------------------------------- #
def _accounting(units, *, landed, failed=(), skipped=()):
    """:func:`_parts_accounting` with the loop's four outputs stated directly."""
    from skidl_layout.construct import _parts_accounting

    partition, _g, _f = _orphan_fixture()
    host = next(unit for unit in units if unit.name == "ic:U1")
    return _parts_accounting(
        partition, units,
        placements={name: object() for name in landed}, ring2={},
        failures=[{"ref": name} for name in failed],
        ring2_failures=[], skipped=[{"ref": name} for name in skipped],
        host=host)


def test_the_parts_accounting_NAMES_what_the_unit_accounting_cannot_see():
    """⭐⭐⭐ The point of the whole block: with ``orphan_units`` OFF the unit
    accounting is total and correct **and the board has lost a part**."""
    units, _skips = _orphan_units()
    blob = _accounting(units, landed=["singleton:U2"])
    assert blob["universe_size"] == 4
    assert blob["unaccounted_parts"] == ["RLED"]
    assert blob["total"] is False


def test_the_parts_accounting_is_total_once_the_orphan_is_emitted():
    units, _skips = _orphan_units(orphan_units=True)
    blob = _accounting(units, landed=["orphan:RLED", "singleton:U2"])
    assert blob["unaccounted_parts"] == []
    assert blob["total"] is True
    assert blob["in_a_landed_unit"] == 4      # M1, U1 (anchor unit), RLED, U2


def test_a_part_in_a_FAILED_or_SKIPPED_unit_is_accounted_for_and_named():
    """⛔ Accounted for is not the same as placed. A named failure is
    accounting; only silence is not."""
    units, _skips = _orphan_units(orphan_units=True)
    failed = _accounting(units, landed=["singleton:U2"], failed=["orphan:RLED"])
    assert failed["unaccounted_parts"] == [] and failed["total"] is True
    assert failed["in_a_failed_unit"] == ["RLED"]
    skipped = _accounting(units, landed=["singleton:U2"],
                          skipped=["orphan:RLED"])
    assert skipped["in_a_skipped_unit"] == ["RLED"]
    assert skipped["total"] is True


def test_the_parts_accounting_also_names_a_part_claimed_by_TWO_units():
    """⛔ S8's other way the two levels disagree: a part in two units is placed
    twice and ends up wherever the alphabetically-later one put it."""
    import dataclasses

    units, _skips = _orphan_units(orphan_units=True)
    orphan = next(u for u in units if u.name == "orphan:RLED")
    doubled = tuple(units) + (dataclasses.replace(orphan,
                                                  name="orphan:RLED:again"),)
    blob = _accounting(doubled, landed=["orphan:RLED", "singleton:U2"])
    assert blob["claimed_by_more_than_one_unit"] == ["RLED"]


def test_the_universe_is_the_PARTITION_and_not_the_units():
    """⛔⛔ The blindness, one level out: deriving the universe from the units
    would let a part no unit mentions define itself out of existence."""
    from skidl_layout.construct import _parts_accounting

    partition, _g, _f = _orphan_fixture()
    units, _skips = _orphan_units()
    host = next(unit for unit in units if unit.name == "ic:U1")
    blob = _parts_accounting(partition, (), {}, {}, [], [], [], host)
    assert blob["universe_size"] == 4
    assert blob["unaccounted_parts"] == ["M1", "RLED", "U1", "U2"]
