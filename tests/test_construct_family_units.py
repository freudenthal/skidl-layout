# -*- coding: utf-8 -*-
"""S8 STAGE C -- ``board_units(family_units=)``: a family becomes an L0 unit.

⛔⛔ **What this file locks down is the DEFAULT.** ``family_units="flattened"``
is S5B/S7's recorded behaviour and every byte of it -- including the ``reason``
string, which lives inside ``Unit.to_dict()`` and therefore inside the L2
determinism digest -- has to stay where it was, or the plan's gate ``PT4``
(bail-out 5: *"stop everything, find the write before adding one"*) has nothing
to stand on. The corpus behaviour of the ``template`` arm is the driver's gate
``PT3``; what is testable here is the **wiring**: validation, the fallback
naming, and the fact that the two arms differ only where they are supposed to.

⛔⛔ :mod:`skidl_layout.construct` is still a **leaf**.
"""

from __future__ import annotations

import pytest

from skidl_layout.construct import (
    FAMILY_UNITS,
    ConstructError,
    board_units,
)
from skidl_layout.geometry import FootprintGeometry, PadGeometry

from test_construct import _Footprint, _Pad


def _geometry(ref: str) -> FootprintGeometry:
    """An 0603-ish two-pad chip: a body box, a wider courtyard, two pads."""
    return FootprintGeometry(
        footprint=f"fp:{ref}",
        pads=[PadGeometry(number="1", x_mm=-0.8, y_mm=0.0, width_mm=0.9,
                          height_mm=0.95, layers=("F.Cu",)),
              PadGeometry(number="2", x_mm=0.8, y_mm=0.0, width_mm=0.9,
                          height_mm=0.95, layers=("F.Cu",))],
        body_bounds=(-0.8, -0.45, 0.8, 0.45),
        courtyard_bounds=(-1.5, -0.75, 1.5, 0.75))


def _footprint(ref: str, nets) -> object:
    return _Footprint([
        _Pad(pad_number="1", net_name=nets[0], component_ref=ref,
             local_x=-0.8, local_y=0.0, global_x=-0.8, global_y=0.0,
             size_x=0.9, size_y=0.95),
        _Pad(pad_number="2", net_name=nets[1], component_ref=ref,
             local_x=0.8, local_y=0.0, global_x=0.8, global_y=0.0,
             size_x=0.9, size_y=0.95)])


def _fixture():
    """One family of two, one singleton, and nothing that needs a router.

    ⚠ The ``ic`` group is deliberately **absent**: the constructed-cell branch
    needs a real :class:`~skidl_layout.route_session.RouteSession`, and this
    file is about the branch below it.
    """
    from skidl_layout.cells_partition import CellGroup, Partition

    groups = (
        CellGroup(name="family:divider:RFB1-RFB2", kind="family",
                  refs=("RFB1", "RFB2"), anchor=None, family="divider",
                  topology="junction"),
        CellGroup(name="singleton:C1", kind="singleton", refs=("C1",),
                  anchor=None, family=None, topology=None),
    )
    nets_by_ref = {"RFB1": ["VOUTX", "VFB"], "RFB2": ["VFB", "AGND"],
                   "C1": ["VOUTX", "AGND"]}
    partition = Partition(board="tiny", groups=groups, parts_total=3,
                          unassigned=(), meta={"nets_by_ref": nets_by_ref,
                                               "board": "tiny"})
    geometries = {ref: _geometry(ref) for ref in nets_by_ref}
    footprints = {ref: _footprint(ref, nets_by_ref[ref]) for ref in nets_by_ref}
    return partition, geometries, footprints


def _units(**kwargs):
    partition, geometries, footprints = _fixture()
    return board_units(partition, geometries=geometries, escape_maps={},
                       footprints=footprints, **kwargs)


# --------------------------------------------------------------------------- #
# Validation -- an undeclared arm and a shared session are both refusals
# --------------------------------------------------------------------------- #
def test_an_undeclared_family_units_arm_raises():
    with pytest.raises(ConstructError, match="family_units"):
        _units(family_units="rigid")


def test_the_template_arm_refuses_to_run_without_a_fresh_session_factory():
    """⛔ Standing finding 22: a shared session measures the session's history.
    The refusal names the finding rather than defaulting to one session."""
    with pytest.raises(ConstructError, match="standing finding 22"):
        _units(family_units="template", fab=object())


def test_the_template_arm_refuses_to_run_without_a_fab():
    with pytest.raises(ConstructError, match="family_units"):
        _units(family_units="template", template_session=lambda g: object())


def test_every_declared_arm_is_reachable_and_flattened_is_the_default():
    """⛔ Standing finding 20: a declared arm nothing can select is a constant."""
    assert FAMILY_UNITS == ("flattened", "template")
    import inspect

    default = inspect.signature(board_units).parameters["family_units"].default
    assert default == "flattened", "S8 may not move a default"


# --------------------------------------------------------------------------- #
# The default arm -- the regression lock, in the bytes
# --------------------------------------------------------------------------- #
def test_the_default_arm_flattens_the_family_into_one_unit_per_member():
    units, skipped = _units()
    assert skipped == ()
    by_name = {unit.name: unit for unit in units}
    assert set(by_name) == {"family:divider:RFB1-RFB2|RFB1",
                            "family:divider:RFB1-RFB2|RFB2",
                            "singleton:C1"}
    for ref in ("RFB1", "RFB2"):
        unit = by_name[f"family:divider:RFB1-RFB2|{ref}"]
        assert unit.source == "flattened"
        assert unit.refs == (ref,)
        assert unit.group == "family:divider:RFB1-RFB2"
        assert unit.meta == {}


def test_the_flattened_reason_string_is_verbatim_and_is_in_the_digest():
    """⛔⛔ **The regression lock, stated as bytes.** ``reason`` is inside
    ``Unit.to_dict()`` and therefore inside the L2 determinism digest, so this
    exact sentence is what makes ``family_units="flattened"`` byte-identical to
    the recorded S5B/S7 artifacts. ⚠ Its *"rigidity is a refuted mechanism"*
    citation is the one the S8 plan section A unbinds -- the correction lives in
    the code comment and in ``FAMILY_UNITS``' docstring, which are **not** in
    the bytes. Changing this string is a corpus move and must be run as one."""
    units, _skipped = _units()
    unit = next(u for u in units if u.name.endswith("|RFB1"))
    assert unit.reason == (
        "FLATTENED out of family:divider:RFB1-RFB2 (2 members): rigidity is a "
        "refuted mechanism, so the members enter L2 individually and keep the "
        "group name for priority")
    assert "reason" in unit.to_dict()


def test_a_one_member_group_is_untouched_by_either_arm():
    plain = {u.name: u.to_dict() for u in _units()[0]}
    with_templates = {u.name: u.to_dict() for u in _units(
        family_units="template", fab=object(),
        template_session=lambda g: (_ for _ in ()).throw(
            ConstructError("no session in this test")))[0]}
    assert plain["singleton:C1"] == with_templates["singleton:C1"]


# --------------------------------------------------------------------------- #
# The template arm -- the fallback is NAMED, never silent
# --------------------------------------------------------------------------- #
def test_a_family_the_l0_loop_refuses_falls_back_and_says_so_on_its_face():
    """⛔ A fallback that looks like the default arm is a defect that reports
    itself as a success. It has to be visible in the unit, not in a log."""
    def _refuse(group):
        raise ConstructError(f"no session for {group.name} in this test")

    units, skipped = _units(family_units="template", fab=object(),
                            template_session=_refuse)
    assert skipped == ()
    flattened = [u for u in units if u.group == "family:divider:RFB1-RFB2"]
    assert len(flattened) == 2, "the members may not vanish with the template"
    for unit in flattened:
        assert unit.source == "flattened"
        assert "FELL BACK to flattened" in unit.reason
        assert "no session for" in unit.reason
        assert unit.meta["family_units_fallback"] == unit.reason


def test_a_template_that_leaves_a_member_unplaced_falls_back_rather_than_shrink():
    """⛔⛔ A ``Unit`` built from a partial cell would be a SUBSET of the group,
    and its missing members would leave L2 with nobody naming them -- standing
    finding 1 wearing an inventory's hat. The group falls back whole."""
    import skidl_layout.construct as C

    class _Partial:
        anchor = "RFB1"
        placements = ()
        meta = {"l0": {"anchor": "RFB1", "anchor_why": "", "anchor_x_mm": 0.0,
                       "anchor_y_mm": 0.0, "anchor_rot_deg": 0}}

    original = C.construct_template
    C.construct_template = lambda *a, **k: _Partial()
    try:
        units, _skipped = _units(family_units="template", fab=object(),
                                 template_session=lambda g: object())
    finally:
        C.construct_template = original
    flattened = [u for u in units if u.group == "family:divider:RFB1-RFB2"]
    assert len(flattened) == 2
    assert all("left ['RFB2'] unplaced" in u.reason for u in flattened)
    assert all("FELL BACK to flattened" in u.reason for u in flattened)


# --------------------------------------------------------------------------- #
# The unit-from-cell expansion -- an L0 unit inside a cell is n PARTS
# --------------------------------------------------------------------------- #
def test_a_cell_placement_that_is_an_l0_unit_expands_to_its_members():
    """⭐⭐⭐ **S8's other half.** When the L1 cell ran with
    ``template_units=True`` a placement's ``ref`` is a GROUP NAME. Left
    unexpanded there is no ``geometries[name]`` a board writer could use and no
    ``footprints[name]`` at all -- and every member would silently leave L2."""
    from skidl_layout.construct import CellResult, Placement, unit_from_cell

    geometries = {ref: _geometry(ref) for ref in ("U1", "RA1", "RA2")}
    footprints = {"U1": _footprint("U1", ("VIN", "FB")),
                  "RA1": _footprint("RA1", ("FB", "MID")),
                  "RA2": _footprint("RA2", ("MID", "GND"))}
    placement = Placement(
        ref="family:a", x_mm=14.0, y_mm=10.0, rot_deg=90, standoff_mm=2.0,
        slide_steps=0, routed=True, route_length_mm=1.0, route_iterations=1,
        net="FB", anchor_pad="2")
    result = CellResult(
        board="tiny", anchor="U1", sides=(), ring2=(placement,),
        ring2_failures=(), tighten=(), skipped=(),
        meta={"accounting": {"failed": []}, "side_order": ["E"],
              "l0": {"offsets": {"family:a": {"RA1": [0.0, 0.0, 0],
                                              "RA2": [2.0, 0.0, 0]}}}})
    unit = unit_from_cell(result, geometries, {}, name="ic:U1", kind="ic",
                          footprints=footprints,
                          outside_nets={"VIN", "GND"},
                          anchor_x_mm=10.0, anchor_y_mm=10.0)
    # ⛔ The GROUP NAME is gone and both members are there, at the composed
    # position. The unit sat at (14, 10) rotated 90, so RA2's local (+2, 0)
    # goes through ``geometry.transform_point`` -- the SAME function
    # ``drive_construct_arc._member_positions`` composes L0 units with one level
    # down -- and lands at (14, 8), i.e. (4, -2) from the anchor at (10, 10).
    assert "family:a" not in unit.offsets
    assert unit.refs == ("RA1", "RA2", "U1")
    assert unit.offsets["RA1"] == (4.0, 0.0, 90)
    assert unit.offsets["RA2"] == (4.0, -2.0, 90)


def test_a_cell_with_no_l0_units_is_byte_identical_to_before_the_expansion():
    """⛔ Additive: today's recorded cells carry no ``meta["l0"]`` offsets."""
    from skidl_layout.construct import CellResult, Placement, unit_from_cell

    geometries = {ref: _geometry(ref) for ref in ("U1", "RA1")}
    footprints = {"U1": _footprint("U1", ("VIN", "FB")),
                  "RA1": _footprint("RA1", ("FB", "GND"))}
    placement = Placement(
        ref="RA1", x_mm=14.0, y_mm=10.0, rot_deg=0, standoff_mm=2.0,
        slide_steps=0, routed=True, route_length_mm=1.0, route_iterations=1,
        net="FB", anchor_pad="2")
    result = CellResult(
        board="tiny", anchor="U1", sides=(), ring2=(placement,),
        ring2_failures=(), tighten=(), skipped=(),
        meta={"accounting": {"failed": []}, "side_order": ["E"]})
    unit = unit_from_cell(result, geometries, {}, name="ic:U1", kind="ic",
                          footprints=footprints, outside_nets={"VIN", "GND"},
                          anchor_x_mm=10.0, anchor_y_mm=10.0)
    assert unit.offsets == {"U1": (0.0, 0.0, 0), "RA1": (4.0, 0.0, 0)}


def test_the_template_arm_never_drops_a_part_however_it_resolves():
    """⛔ The inventory is TOTAL under both arms: every ref is in exactly one
    unit or in a named skip."""
    partition, _g, _f = _fixture()
    every = {ref for group in partition.groups for ref in group.refs}
    for kwargs in ({}, {"family_units": "template", "fab": object(),
                        "template_session": lambda g: (_ for _ in ()).throw(
                            ConstructError("refused"))}):
        units, skipped = _units(**kwargs)
        seen = [ref for unit in units for ref in unit.refs]
        assert sorted(seen) + sorted(r["ref"] for r in skipped) == sorted(every)
        assert len(set(seen)) == len(seen), "a ref may not be in two units"
