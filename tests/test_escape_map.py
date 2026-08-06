# -*- coding: utf-8 -*-
"""Tests for the per-pad escape map (construction arc, S1 -- WS-E1/WS-E2).

⛔ Everything here is **offline**: no router, no KRT, no board on disk. The
routed half -- the probe that grades the rules, the corpus sweep and the
renders -- lives in ``canaries/drive_escape_map.py``, which is where a router
belongs.

⛔⛔ :mod:`skidl_layout.escape_map` is a **leaf**. Nothing in the engine imports
it and nothing consumes the map yet, so no test here may reach the scorer, the
refiner or a placement digest.

⭐ The two behaviours the singleton wrap gets for free -- a chip passive blocked
on its opposite side, a QFP pad clear only on its own side -- are **asserted**
here rather than encoded in the module. If ``derive_access`` ever stops
producing them, these fail, which is the point.
"""

from __future__ import annotations

import pytest

from skidl_layout.cells import _rotate_side
from skidl_layout.cells_families import footprint_for
from skidl_layout.escape_map import (
    EscapeMap,
    PadEscape,
    escape_map_for,
    escape_map_to_dict,
    escape_maps_for,
    favored_side,
    pad_occupancy_side,
    rotate_escape,
    terminal_pads,
)

_FP_DIRS = None


def _fp_dirs():
    global _FP_DIRS
    if _FP_DIRS is None:
        from skidl_layout.metrics import discover_footprint_dir

        found = discover_footprint_dir()
        _FP_DIRS = [found] if found else []
    return _FP_DIRS


needs_footprints = pytest.mark.skipif(
    not _fp_dirs(), reason="no KiCad footprint library on this host")

#: ⛔ The two numbers every verdict in this file is stated against, and they
#: carry their source: the lane is ``via_size + 2 x min_clearance`` on
#: ``oshpark-2l`` (``power_escape.ESCAPE_LANE_MM``), the clearance is the arc's
#: design clearance. Never re-hardcoded inside a test body.
LANE_MM = 0.9048
CLEARANCE_MM = 0.25

#: ⛔⛔ **Asserted at all three sizes, deliberately.** The size-table trap has
#: fired four times: a rule that is right at 0805 and wrong at 0402 must die in
#: this file, not in S3.
SIZES = ("0402", "0603", "0805")
KINDS = ("R", "C", "L")

SOIC = "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm"
MSOP_EP = "Package_SO:MSOP-10-1EP_3x3mm_P0.5mm_EP1.68x1.88mm"
LQFP = "Package_QFP:LQFP-48_7x7mm_P0.5mm"
QFN_EP = "Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm_EP3.2x3.2mm"
HEADER = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"
USB_C = "Connector_USB:USB_C_Receptacle_XKB_U262-16XN-4BVC11"
LGA12 = "Package_LGA:LGA-12_2x2mm_P0.5mm"


def _map(footprint, **kwargs):
    return escape_map_for(footprint, _fp_dirs(), lane_mm=LANE_MM,
                          clearance_mm=CLEARANCE_MM, **kwargs)


# --------------------------------------------------------------------------- #
# WS-E1 -- the singleton wrap and the corridor map
# --------------------------------------------------------------------------- #
@needs_footprints
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("kind", KINDS)
def test_two_pin_passive_rules_are_emergent_at_every_size(kind, size):
    """The overview's two/three-pin rule, at 0402, 0603 and 0805, on R, C and L.

    ⭐ **Nothing in the module encodes this.** The opposite side is ``BLOCKED``
    because the straight corridor crosses the other pad, which under the
    singleton wrap is a foreign net.
    """
    emap = _map(footprint_for(kind, size))
    assert emap.pad_count == 2
    assert emap.pads == ("1", "2")
    for pad, own, opposite in (("1", "W", "E"), ("2", "E", "W")):
        assert emap.access(pad, own) == "FAVORED", (kind, size, pad)
        assert emap.access(pad, opposite) == "BLOCKED", (kind, size, pad)
        for orthogonal in ("N", "S"):
            assert emap.access(pad, orthogonal) == "ACCESSIBLE", (kind, size, pad)
        assert favored_side(emap, pad) == own


@needs_footprints
@pytest.mark.parametrize("size", SIZES)
def test_favored_is_the_pads_own_side_not_the_alphabetical_tie_break(size):
    """⛔⛔ The finding this module exists to correct, pinned as a test.

    ``derive_access`` ranks on ``(cost, side, layer)`` and a flush pad's cost is
    **0.0 on every side it touches**, so its own ranking promotes ``N`` for pad
    1 of every chip passive at every size -- the alphabetical order of
    ``E < N < S < W``, not "the side the pad is nearest". The occupancy rule
    replaces that tie-break and this asserts the replacement did its job.
    """
    from skidl_layout.cells import synthesise_cell
    from skidl_layout.cells_compile import derive_access

    footprint = footprint_for("R", size)
    cell = synthesise_cell(f"@escape:{footprint}",
                           [("P1", footprint, 0.0, 0.0, 0)],
                           {"1": [("P1", "1")], "2": [("P1", "2")]},
                           fp_lib_dirs=_fp_dirs())
    raw = derive_access(cell, layers=(0,), lane_mm=LANE_MM,
                        clearance_mm=CLEARANCE_MM)
    raw_favoured = {p.local_net: p.side for p in raw if p.access == "FAVORED"}
    # The degenerate ranking, reproduced rather than described.
    assert raw_favoured == {"1": "N", "2": "E"}
    emap = _map(footprint)
    assert {pad: favored_side(emap, pad) for pad in emap.pads} == {"1": "W",
                                                                  "2": "E"}


@needs_footprints
def test_dual_row_pads_favour_their_own_row_and_are_blocked_opposite():
    emap = _map(SOIC)
    assert emap.detection == "dual_row"
    assert emap.pad_count == 8
    for pad in ("1", "2", "3", "4"):
        assert emap.access(pad, "W") == "FAVORED"
        assert emap.access(pad, "E") == "BLOCKED"
    for pad in ("5", "6", "7", "8"):
        assert emap.access(pad, "E") == "FAVORED"
        assert emap.access(pad, "W") == "BLOCKED"


@needs_footprints
def test_four_sided_pads_escape_only_on_their_own_side():
    """The QFP rule -- emergent, and the corner count is recorded not fought."""
    emap = _map(LQFP)
    assert emap.detection == "four_sided"
    assert emap.pad_count == 48
    two_sided = [pad for pad in emap.pads
                 if len(emap.escapable_sides(pad)) > 1]
    # ⭐ Exactly the eight corner pads -- two per corner, one on each arm.
    assert len(two_sided) == 8
    assert all(len(emap.escapable_sides(pad)) == 2 for pad in two_sided)
    for pad in emap.pads:
        assert favored_side(emap, pad) == emap.meta["occupancy"][pad]


@needs_footprints
def test_through_hole_part_derives_and_records_through_board():
    emap = _map(HEADER)
    assert emap.pad_count == 4
    assert emap.meta["through_board_pads"] == ["1", "2", "3", "4"]
    assert emap.layers == (0,)
    assert all(emap.escapable_sides(pad) for pad in emap.pads)


@needs_footprints
def test_the_map_is_total_over_pad_times_side():
    for footprint in (footprint_for("R", "0805"), SOIC, LQFP, QFN_EP, HEADER):
        emap = _map(footprint)
        assert len(emap.entries) == emap.pad_count * 4
        assert len({(e.pad, e.side, e.layer) for e in emap.entries}) \
            == len(emap.entries)
        assert list(emap.entries) == sorted(emap.entries, key=lambda e: e.key)


@needs_footprints
def test_derivation_is_deterministic_byte_for_byte():
    import json

    for footprint in (footprint_for("C", "0402"), SOIC, QFN_EP):
        first = json.dumps(escape_map_to_dict(_map(footprint)), sort_keys=True)
        second = json.dumps(escape_map_to_dict(_map(footprint)), sort_keys=True)
        assert first == second


@needs_footprints
def test_bare_names_are_resolved_and_the_artifact_never_carries_one():
    """⛔ Standing finding 6 -- every board this stack writes carries bare names."""
    emap = _map("R_0805_2012Metric")
    assert emap.footprint == "Resistor_SMD:R_0805_2012Metric"
    assert emap.meta["requested_name"] == "R_0805_2012Metric"
    assert ":" in emap.footprint


@needs_footprints
def test_verdicts_are_computed_in_the_physical_box_and_the_courtyard_is_recorded():
    """⛔ Standing finding 13 -- say which box, in the rule."""
    from skidl_layout.geometry import load_footprint_geometries

    name = footprint_for("R", "0805")
    geometry = load_footprint_geometries({name}, _fp_dirs())[name]
    emap = _map(name)
    assert emap.physical_bounds == pytest.approx(geometry.physical_bounds)
    assert emap.courtyard_bounds == pytest.approx(geometry.courtyard_bounds)
    # ⭐ The two boxes are genuinely different, so confusing them would matter.
    assert emap.courtyard_bounds != emap.physical_bounds
    assert emap.meta["box_used_for_verdicts"].startswith("physical_bounds")


@needs_footprints
def test_paste_only_apertures_are_excluded_from_the_map_but_counted():
    """⚠ Trap 3 -- a legitimate exclusion, never a silent one."""
    emap = _map(QFN_EP)
    assert len(emap.meta["non_copper_pads"]) == 4
    assert all(entry["layers"] == ["F.Paste"]
               for entry in emap.meta["non_copper_pads"])
    assert emap.pad_count == 57
    assert emap.meta["pad_apertures"] == 61


@needs_footprints
def test_an_unresolvable_footprint_raises_rather_than_boxing_it_in_2x2mm():
    with pytest.raises(ValueError, match="does not resolve"):
        escape_map_for("NoSuchFootprintAnywhere_9x9", _fp_dirs(),
                       lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM)


def test_a_map_on_which_nothing_escapes_raises(monkeypatch):
    """⛔ The observes-nothing rule -- five instances in six runs."""
    import skidl_layout.escape_map as EM

    def _all_blocked(cell, pad, side, layer, **kwargs):
        return False, 1.0

    class _Port:
        def __init__(self, net, side, layer):
            self.local_net, self.side, self.layer = net, side, layer
            self.access = "BLOCKED"

    monkeypatch.setattr(EM, "escape_corridor_clear", _all_blocked)
    monkeypatch.setattr(EM, "derive_access", lambda cell, **kw: tuple(
        _Port(net, side, 0)
        for net in sorted({p.local_net for p in cell.pads if p.local_net})
        for side in ("N", "E", "S", "W")))
    if not _fp_dirs():
        pytest.skip("no KiCad footprint library on this host")
    with pytest.raises(ValueError, match="observes nothing"):
        escape_map_for(footprint_for("R", "0805"), _fp_dirs(),
                       lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM)


@needs_footprints
def test_favored_side_raises_for_a_pad_with_no_escape():
    """An exposed pad has no lateral escape -- the caller is told, not defaulted."""
    emap = _map(QFN_EP)
    assert emap.pads_without_escape == ("57",)
    with pytest.raises(ValueError, match="no FAVORED side"):
        favored_side(emap, "57")


@needs_footprints
def test_lookup_of_an_absent_triple_raises_because_the_map_is_total():
    emap = _map(footprint_for("R", "0805"))
    with pytest.raises(KeyError):
        emap.access("3", "N")


# --------------------------------------------------------------------------- #
# rotation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("side", ("N", "E", "S", "W"))
def test_rotate_escape_agrees_with_the_cells_transform(side):
    for deg in (0, 90, 180, 270):
        assert rotate_escape(side, deg) == _rotate_side(side, deg)


@pytest.mark.parametrize("side", ("N", "E", "S", "W"))
def test_four_quarter_turns_compose_to_identity(side):
    turned = side
    for _ in range(4):
        turned = rotate_escape(turned, 90)
    assert turned == side


# --------------------------------------------------------------------------- #
# WS-E2 -- the row channel, the one authored rule
# --------------------------------------------------------------------------- #
@needs_footprints
def test_row_detection_is_measured_not_named():
    """⛔ Never a substring match, never a pin-count threshold on the rule.

    The proof that no name is consulted: two parts whose names share nothing
    (``SOIC-8`` and a 4-pin header) land in the same detection class, and two
    that share the ``MSOP``/``SO`` prefix land in different channel states.
    """
    assert _map(SOIC).detection == "dual_row"
    assert _map(LQFP).detection == "four_sided"
    assert _map(QFN_EP).detection == "four_sided"
    assert _map(footprint_for("R", "0603")).detection == "two_pin"


@needs_footprints
def test_the_soic_channel_is_open_and_upgrades_exactly_the_inner_pads():
    emap = _map(SOIC)
    assert emap.channel is not None
    assert emap.channel.axis == "NS"
    assert emap.channel.open is True
    assert emap.channel.clear_mm >= emap.lane_mm
    upgraded = sorted({e.pad for e in emap.entries if e.source == "row_channel"})
    # ⭐ Only the inner pads: 1/4/5/8 sit at the row ends and already had a
    # straight corridor N or S, and the rule never downgrades those.
    assert upgraded == ["2", "3", "6", "7"]
    for pad in upgraded:
        rows = [e for e in emap.entries_for(pad) if e.source == "row_channel"]
        assert len(rows) == 1
        assert rows[0].side in ("N", "S")
        assert rows[0].access == "ACCESSIBLE"


@needs_footprints
def test_the_row_channel_never_promotes_to_favored():
    """⛔ The overview says the between-the-rows escape is the detour."""
    for footprint in (SOIC, MSOP_EP, LQFP, QFN_EP, HEADER):
        emap = _map(footprint)
        assert not [e for e in emap.entries
                    if e.source == "row_channel" and e.access == "FAVORED"]


@needs_footprints
def test_the_row_channel_never_downgrades_a_corridor_verdict():
    emap = _map(SOIC)
    for entry in emap.entries:
        if entry.source == "row_channel":
            assert entry.access == "ACCESSIBLE"
    # every corridor FAVORED survived
    assert len([e for e in emap.entries if e.access == "FAVORED"]) == 8


@needs_footprints
@pytest.mark.parametrize("footprint", (MSOP_EP,
                                       "Package_SO:TSSOP-16-1EP_4.4x5mm_P0.65mm_EP3x3mm"))
def test_an_exposed_pad_closes_the_channel_with_no_special_case(footprint):
    """⭐ No ``EP`` substring is matched anywhere -- the EP's own inner edge is
    what narrows the lane, and the arithmetic is the same one the SOIC passes."""
    emap = _map(footprint)
    assert emap.channel is not None
    assert emap.channel.open is False
    assert emap.channel.clear_mm < emap.lane_mm
    assert not [e for e in emap.entries if e.source == "row_channel"]


@needs_footprints
def test_a_four_sided_part_gets_no_channel_and_no_upgrade():
    for footprint in (LQFP, QFN_EP):
        emap = _map(footprint)
        assert emap.channel is None
        assert not [e for e in emap.entries if e.source == "row_channel"]


@needs_footprints
def test_pad_occupancy_is_the_pad_side_counts_arithmetic_per_pad():
    """⭐ One rule, two consumers (FAVORED and row detection), and it is the
    library's own arithmetic rather than a second one."""
    from skidl_layout.geometry import load_footprint_geometries

    geometry = load_footprint_geometries({SOIC}, _fp_dirs())[SOIC]
    per_pad = {}
    for pad in terminal_pads(geometry):
        per_pad[pad_occupancy_side(pad.x_mm, pad.y_mm,
                                   geometry.physical_bounds)] = \
            per_pad.get(pad_occupancy_side(pad.x_mm, pad.y_mm,
                                           geometry.physical_bounds), 0) + 1
    summed = geometry.pad_side_counts()
    assert per_pad == {"W": summed["left"], "E": summed["right"]}


def test_pad_occupancy_ties_resolve_totally():
    """A pad dead in the centre reads ``E`` -- arbitrary, but stated and total."""
    box = (-1.0, -1.0, 1.0, 1.0)
    assert pad_occupancy_side(0.0, 0.0, box) == "E"
    assert pad_occupancy_side(-0.5, 0.0, box) == "W"
    assert pad_occupancy_side(0.0, -0.5, box) == "N"
    assert pad_occupancy_side(0.0, 0.5, box) == "S"


# --------------------------------------------------------------------------- #
# the sweep helper and the serialised view
# --------------------------------------------------------------------------- #
@needs_footprints
def test_escape_maps_for_keys_on_the_resolved_name():
    maps = escape_maps_for(["R_0805_2012Metric", SOIC], _fp_dirs(),
                           lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM)
    assert sorted(maps) == ["Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
                            "Resistor_SMD:R_0805_2012Metric"]
    assert all(name == emap.footprint for name, emap in maps.items())


@needs_footprints
def test_the_serialised_view_carries_every_number_with_its_source():
    emap = escape_map_for(footprint_for("R", "0805"), _fp_dirs(),
                          lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM,
                          lane_source="power_escape.lane_from_fab(oshpark-2l)",
                          clearance_source="FabSpec.clearance_mm")
    blob = escape_map_to_dict(emap)
    assert blob["lane_mm"] == LANE_MM
    assert blob["clearance_mm"] == CLEARANCE_MM
    assert blob["meta"]["lane_source"].startswith("power_escape")
    assert blob["meta"]["clearance_source"] == "FabSpec.clearance_mm"
    assert blob["counts"]["FAVORED"] == 2
    assert sum(blob["counts"][k] for k in ("FAVORED", "ACCESSIBLE", "BLOCKED")) \
        == len(blob["entries"])


@needs_footprints
def test_non_plated_and_unnumbered_pads_are_excluded_but_still_obstruct():
    """⛔ The USB-C receptacle's two mounting holes.

    They carry ``*.Cu`` in their layer list and an **empty** pad number, so a
    filter that only asked "is there copper" made them terminals, keyed a map
    entry on ``""``, and raised. They are not terminals; they are still copper.
    """
    emap = _map(USB_C)
    excluded = emap.meta["excluded_copper_pads"]
    assert len(excluded) == 2
    assert all(e["pad_type"] == "np_thru_hole" for e in excluded)
    assert all(e["why"] == "unnumbered" for e in excluded)
    assert "" not in emap.pads
    assert emap.pad_count == len(emap.pads)


@needs_footprints
def test_a_fine_pitch_part_with_inset_pads_has_no_escape_at_all():
    """⛔⛔ MEASURED, structural, and the single most important thing S1 hands
    to S3: **the via lane is wider than the pad pitch.**

    ``LGA-12`` is 0.5 mm pitch with its pads inset 0.1 mm from the box edge, so
    every straight 0.9048 mm corridor overlaps a neighbour inflated by
    clearance -- on every pad, on every side. ``LQFP-48`` is the *same* pitch
    and escapes on 48 of 48, because its pads sit **flush** with the box edge
    and ``escape_corridor_clear`` short-circuits before it tests an obstruction.
    ⭐ Flush-versus-inset, not pitch, is the discriminator, and this test pins
    both halves so the successor cannot mistake it for a pitch threshold.
    """
    with pytest.raises(ValueError, match="observes nothing"):
        _map(LGA12)
    emap = _map(LGA12, allow_no_escape=True)
    assert emap.meta["no_escape_anywhere"] is True
    assert len(emap.pads_without_escape) == emap.pad_count
    assert all(e.access == "BLOCKED" for e in emap.entries)
    # the same pitch, flush pads, and it escapes everywhere
    flush = _map(LQFP)
    assert flush.meta["no_escape_anywhere"] is False
    assert not flush.pads_without_escape


@needs_footprints
def test_pads_are_ordered_numerically_not_lexically():
    emap = _map(LQFP)
    assert emap.pads[:11] == ("1", "2", "3", "4", "5", "6", "7", "8", "9",
                              "10", "11")


# --------------------------------------------------------------------------- #
# ⭐ WHY a pad has no escape -- the interior / lane split (adopted 2026-08-05)
# --------------------------------------------------------------------------- #
def _geometry(footprint):
    from skidl_layout.cells import load_footprint_geometries

    return load_footprint_geometries({footprint}, _fp_dirs())[footprint]


@needs_footprints
def test_the_two_blocked_reasons_license_different_actions():
    """⛔⛔ ``pads_without_escape`` hides **two structurally different**
    situations, and the difference is what a human would act on.

    ⭐ Adopted from KRT's per-face ledger, which counts an interior pad toward
    no face because *"it needs a via; rolling it into a face's demand would
    blame the face for a fanout problem."*
    """
    from skidl_layout.escape_map import BLOCKED_REASONS, blocked_reasons

    emap = _map(LGA12, allow_no_escape=True)
    reasons = blocked_reasons(emap, _geometry(LGA12))
    assert set(reasons) == set(emap.pads_without_escape)
    assert set(reasons.values()) <= set(BLOCKED_REASONS)


@needs_footprints
def test_the_split_does_NOT_answer_open_question_8_and_the_numbers_say_so():
    """⛔⛔⛔ **MEASURED BEFORE ADOPTING IT, and it bounds the claim.**

    Standing finding 14 names five footprints that get nothing from the map.
    Interior pads explain **one** of them. ``LGA-12`` is a *perimeter* part:
    every pad sits on the pad lattice's own edge, so a lateral corridor
    exists — it is merely narrower than the **via** lane. That is a different
    problem from a boxed-in pad, and a different fix.

    ⭐ **So this splits open question 8 into two questions with two answers**,
    which is worth more than a rule that would have quietly called all five
    "needs a via".
    """
    from skidl_layout.escape_map import blocked_reasons, interior_pads

    assert interior_pads(_geometry(LGA12)) == (), \
        "LGA-12 is a PERIMETER part -- if this ever reports interior pads, the "
    reasons = blocked_reasons(_map(LGA12, allow_no_escape=True),
                              _geometry(LGA12))
    assert reasons and set(reasons.values()) == {"lane"}


@needs_footprints
def test_an_exposed_pad_reads_INTERIOR_with_no_special_case():
    """⭐⭐ The nicest thing the split does, and it was not designed in.

    ``MSOP-10-1EP``'s exposed pad is the one pad on that footprint with no
    lateral escape, and the lattice test calls it ``interior`` — *"it leaves
    through a via"*, which is exactly what ``pads_without_escape``'s docstring
    says in prose. ⛔ **No ``EP`` substring is matched anywhere**, the same way
    S1's exposed-pad rule turned out to be emergent rather than authored.
    """
    from skidl_layout.escape_map import blocked_reasons

    emap = _map(MSOP_EP)
    reasons = blocked_reasons(emap, _geometry(MSOP_EP))
    assert list(reasons.values()) == ["interior"]
    assert set(reasons) == set(emap.pads_without_escape)


@needs_footprints
def test_a_footprint_that_escapes_everywhere_has_no_reasons_to_give():
    """⚠ The empty answer is the right answer here, and it is asserted rather
    than left to chance: ``LQFP-48`` escapes on 48 of 48."""
    from skidl_layout.escape_map import blocked_reasons

    assert blocked_reasons(_map(LQFP), _geometry(LQFP)) == {}


@needs_footprints
def test_a_two_pin_passive_has_no_interior_pads():
    """⛔ The lattice test needs three distinct coordinates on **both** axes;
    a passive has two pads, so the honest answer is 'none', not a crash."""
    from skidl_layout.escape_map import interior_pads

    for size in SIZES:
        assert interior_pads(_geometry(footprint_for("R", size))) == ()


@needs_footprints
def test_the_reason_refuses_a_map_and_geometry_that_disagree():
    """⛔ A reason derived from the wrong pad lattice is worse than no reason —
    and this is the mismatch S3's bail-out 6 exists for, one level down."""
    from skidl_layout.escape_map import blocked_reasons

    with pytest.raises(ValueError, match="describes"):
        blocked_reasons(_map(LQFP), _geometry(LGA12))


@needs_footprints
def test_the_split_stores_nothing_and_moves_no_recorded_artifact():
    """⛔⛔ The reason is a **free function**, deliberately.

    Running it must not change ``escape_map_to_dict`` by one byte: adding a
    field to a shipped artifact to carry a new derivation is how a
    re-measurement turns into a diff nobody asked for, and S1's
    ``escape_maps.json`` is a recorded control four later stages assert
    against.
    """
    from skidl_layout.escape_map import blocked_reasons

    emap = _map(MSOP_EP)
    before = escape_map_to_dict(emap)
    blocked_reasons(emap, _geometry(MSOP_EP))
    assert escape_map_to_dict(emap) == before


def test_the_module_is_a_leaf_the_engine_never_re_exports():
    """⛔ Trap 7 -- the same rule the routing-session module lives under."""
    import skidl_layout

    assert not hasattr(skidl_layout, "escape_map_for")
    assert not hasattr(skidl_layout, "EscapeMap")


#: ⭐⭐ **S3 IS THE CONSUMER S1 NAMED, AND THIS GUARD IS HOW THE HANDOVER WAS
#: NOTICED.** When S1 shipped, ``escape_map`` was consumed by *nothing* and the
#: assertion below was ``importers == []``. On 2026-08-03 the first construction
#: loop (``construct.py``) landed and the guard went red on the very run that
#: built it -- which is the guard working, not failing.
#: ⛔ The property being guarded has **not** been weakened: nothing in the
#: engine, the scorer or the refiner may import the map. What is permitted is
#: the one module S1's own docstring names as its consumer, and that module is
#: itself a leaf with its own import-aware guard. ⛔ **Add a name here only when
#: the module added is a leaf too** -- an entry that is reachable from
#: ``engine.py`` turns this test into a comment.
ESCAPE_MAP_CONSUMERS = {"construct.py"}


def test_escape_map_is_a_leaf_only_its_named_consumer_imports_it():
    """⛔ Nothing in ``skidl_layout`` may import this module except S3's loop.

    ⚠ **Import-aware on purpose.** The sibling leaf guard next door used to
    scan for the bare module name as a **substring**, so a module that merely
    *mentioned* its neighbour in a docstring failed it -- a false positive, and
    it cost a suite run. This one matches import statements.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    pattern = re.compile(
        r"^\s*(?:from\s+\.?escape_map\s+import|import\s+.*\bescape_map\b"
        r"|from\s+\.\s+import\s+.*\bescape_map\b)", re.MULTILINE)
    importers = [path.name for path in sorted(root.glob("*.py"))
                 if path.name != "escape_map.py"
                 and pattern.search(path.read_text(encoding="utf-8"))]
    unexpected = sorted(set(importers) - ESCAPE_MAP_CONSUMERS)
    assert unexpected == [], f"escape_map is imported by {unexpected}"


def test_every_permitted_consumer_of_the_escape_map_is_itself_a_leaf():
    """⭐ The exemption's own guard. A permitted consumer that the **engine**
    can reach would launder the leaf rule through one indirection, which is
    exactly the shape of mistake this arc keeps paying for."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    for consumer in sorted(ESCAPE_MAP_CONSUMERS):
        stem = consumer[:-3]
        pattern = re.compile(
            rf"^[ \t]*(?:from[ \t]+[.\w]*\b{stem}\b[ \t]+import"
            rf"|from[ \t]+[.\w]+[ \t]+import[ \t]+[^#\n]*\b{stem}\b"
            rf"|import[ \t]+[^#\n]*\b{stem}\b)", re.MULTILINE)
        importers = [path.name for path in sorted(root.glob("*.py"))
                     if path.name != consumer
                     and pattern.search(path.read_text(encoding="utf-8"))]
        assert importers == [], \
            f"{consumer} is permitted to import escape_map but is NOT a leaf " \
            f"-- it is imported by {importers}"


def test_pad_escape_and_escape_map_are_frozen_value_types():
    entry = PadEscape(pad="1", side="N", layer=0, access="FAVORED",
                      distance_mm=0.0)
    with pytest.raises(Exception):
        entry.pad = "2"
    emap = EscapeMap(footprint="Lib:Name", pad_count=1, entries=(entry,),
                     channel=None, physical_bounds=(0.0, 0.0, 1.0, 1.0),
                     courtyard_bounds=None, lane_mm=LANE_MM,
                     clearance_mm=CLEARANCE_MM)
    assert emap.pads == ("1",)
    assert emap.escapable_sides("1") == ("N",)
    assert emap.pads_without_escape == ()


# --------------------------------------------------------------------------- #
# S5C -- ``escapable_sides_ranked``. ⛔ ADDITIVE: the map itself is untouched.
# --------------------------------------------------------------------------- #
from skidl_layout.escape_map import escapable_sides_ranked          # noqa: E402


def test_the_ranked_list_never_offers_a_blocked_side():
    """⛔⛔ **S1's soundness is the most valuable property this arc owns** --
    127 + 373 routed probes, **zero over-reports** -- and a rule that handed
    back a side the corridor pass asserted was blocked is the first thing that
    could cost it."""
    emap = EscapeMap(
        footprint="Lib:Fake", pad_count=1,
        entries=(PadEscape(pad="1", side="E", layer=0, access="FAVORED",
                           distance_mm=0.0),
                 PadEscape(pad="1", side="N", layer=0, access="ACCESSIBLE",
                           distance_mm=0.4),
                 PadEscape(pad="1", side="S", layer=0, access="BLOCKED",
                           distance_mm=0.0),
                 PadEscape(pad="1", side="W", layer=0, access="BLOCKED",
                           distance_mm=0.0)),
        channel=None, physical_bounds=(0.0, 0.0, 1.0, 1.0),
        courtyard_bounds=None, lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM)
    assert escapable_sides_ranked(emap, "1") == (("E", 0.0), ("N", 0.4))


def test_the_ranked_list_puts_FAVORED_first_then_the_corridor_cost():
    emap = EscapeMap(
        footprint="Lib:Fake", pad_count=1,
        entries=(PadEscape(pad="1", side="E", layer=0, access="ACCESSIBLE",
                           distance_mm=0.1),
                 PadEscape(pad="1", side="S", layer=0, access="ACCESSIBLE",
                           distance_mm=0.9),
                 PadEscape(pad="1", side="N", layer=0, access="FAVORED",
                           distance_mm=5.0),
                 PadEscape(pad="1", side="W", layer=0, access="ACCESSIBLE",
                           distance_mm=0.1)),
        channel=None, physical_bounds=(0.0, 0.0, 1.0, 1.0),
        courtyard_bounds=None, lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM)
    #: ⛔ FAVORED first even at a **50x** worse corridor, then cost, then the
    #: side letter -- a total key over content (standing finding 8).
    assert [side for side, _cost in escapable_sides_ranked(emap, "1")] == \
        ["N", "E", "W", "S"]


def test_a_pad_that_escapes_nowhere_RAISES_rather_than_returning_empty():
    """⛔ Rule 3, six instances in seven runs: an instrument that observes
    nothing is indistinguishable from one that found everything."""
    emap = EscapeMap(
        footprint="Lib:Fake", pad_count=1,
        entries=(PadEscape(pad="1", side=side, layer=0, access="BLOCKED",
                           distance_mm=0.0) for side in ("N", "E", "S", "W")),
        channel=None, physical_bounds=(0.0, 0.0, 1.0, 1.0),
        courtyard_bounds=None, lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM)
    with pytest.raises(ValueError):
        escapable_sides_ranked(emap, "1")


@needs_footprints
def test_the_first_element_IS_favored_side_on_every_pad_of_every_class():
    """⭐ The whole point of the addition: it **widens** the answer without
    changing the one S3/S4/S5/S5B already consume."""
    for footprint in (SOIC, MSOP_EP, LQFP, HEADER):
        emap = _map(footprint, allow_no_escape=True)
        for pad in emap.pads:
            try:
                ranked = escapable_sides_ranked(emap, pad)
            except ValueError:
                with pytest.raises(ValueError):
                    favored_side(emap, pad)
                continue
            assert ranked[0][0] == favored_side(emap, pad), \
                f"{footprint} pad {pad}"


@needs_footprints
def test_the_dual_row_part_offers_MORE_than_one_side_and_that_is_the_point():
    """⛔⛔ **This is why N and S are EMPTY side lists on 3 of 4 subjects.**
    ``favored_side`` reads one entry; the map has always carried the rest."""
    emap = _map(SOIC)
    wider = [pad for pad in emap.pads
             if len(escapable_sides_ranked(emap, pad)) > 1]
    assert wider == list(emap.pads), \
        "every SOIC-8 pad escapes on more than one side"
    #: ⭐ and the human's *"the top and bottom pads escape U/D"* rule is already
    #: TRUE and EMERGENT -- pads 1, 2, 7 and 8 reach N.
    north = sorted(pad for pad in emap.pads
                   if "N" in [side for side, _c in
                              escapable_sides_ranked(emap, pad)])
    assert north == ["1", "2", "7", "8"]


@needs_footprints
def test_the_exposed_pad_part_grants_exactly_one_pad_per_row_end():
    """⭐ The requirement **exactly**, where the EP closes the row channel --
    and ⛔ the *">= 16 pins => the second pad too"* clause is NOT built:
    the measurement runs the other way and a straight corridor cannot serve a
    pad that sits directly behind another (open question 21)."""
    emap = _map(MSOP_EP, allow_no_escape=True)

    def _sides(pad):
        # ⚠ The exposed pad itself escapes NOWHERE and the function says so by
        # raising -- that is the answer, not an omission (see
        # ``pads_without_escape``), so the census steps over it deliberately.
        try:
            return [side for side, _cost in escapable_sides_ranked(emap, pad)]
        except ValueError:
            assert pad in emap.pads_without_escape
            return []

    north = sorted((pad for pad in emap.pads if "N" in _sides(pad)),
                   key=lambda t: (len(t), t))
    assert north == ["1", "10"]


def test_the_ranked_list_is_a_derivation_and_stores_nothing():
    """⛔ It must not touch the recorded artifact -- four later stages assert
    against ``escape_maps.json``."""
    emap = EscapeMap(
        footprint="Lib:Fake", pad_count=1,
        entries=(PadEscape(pad="1", side="E", layer=0, access="FAVORED",
                           distance_mm=0.0),),
        channel=None, physical_bounds=(0.0, 0.0, 1.0, 1.0),
        courtyard_bounds=None, lane_mm=LANE_MM, clearance_mm=CLEARANCE_MM)
    before = escape_map_to_dict(emap)
    escapable_sides_ranked(emap, "1")
    assert escape_map_to_dict(emap) == before
