"""Phase 10 -- the spacing lever: net voltage -> per-net routing clearance.

The judge (``fabspec.measure_voltage_spacing``) shipped in Phase 8 and is tested
in ``test_fab_spacing.py``. These tests cover the *lever*: that the map handed to
KRT widens only what Table 6-1 actually asks to widen, never narrows anything,
and stays empty -- so no flag is passed at all -- on a board with no HV net.
"""

from __future__ import annotations

import pytest

from skidl_layout.fabspec import OSHPARK_2L
from skidl_layout.power_clearance import net_clearance_map, plan_net_clearances

BASE = OSHPARK_2L.clearance_mm          # 0.25 mm, the design clearance we route at


def test_a_low_voltage_net_is_left_alone():
    """The 6x cliff is at 30->31 V; below it the board is already compliant."""
    rows = plan_net_clearances({"VIN": 24.0, "VOUT": 12.0}, BASE)
    assert [r["required_mm"] for r in rows.values()] == [0.1, 0.1]
    assert not any(r["applied"] for r in rows.values())
    assert net_clearance_map(rows) == {}


def test_an_hv_net_is_widened_to_the_table_value():
    rows = plan_net_clearances({"VIN": 72.0, "SW": 150.0, "VOUT": 12.0}, BASE)
    assert net_clearance_map(rows) == {"VIN": 0.6, "SW": 0.6}
    assert rows["VOUT"]["applied"] is False
    assert "already routes at 0.25mm" in rows["VOUT"]["reason"]


def test_the_map_never_narrows_a_net():
    """A per-net entry BELOW the board clearance would quietly relax a good net.

    Table 6-1 asks 0.1 mm at 24 V and the board routes at 0.25 mm; emitting
    ``{"VIN": 0.1}`` would tell KRT it may come closer than it otherwise would.
    Widening is the only direction this module moves.
    """
    rows = plan_net_clearances({"VIN": 24.0}, BASE)
    assert net_clearance_map(rows) == {}
    for value in net_clearance_map(plan_net_clearances(
            {"VIN": 24.0, "SW": 60.0}, BASE)).values():
        assert value >= BASE


def test_the_cliff_is_reproduced_exactly_and_not_smoothed():
    """30 V -> 0.1 mm, 31 V -> 0.6 mm. No interpolation, no taper (plan 1.2)."""
    rows = plan_net_clearances({"A": 30.0, "B": 31.0}, BASE)
    assert rows["A"]["required_mm"] == 0.1
    assert rows["B"]["required_mm"] == 0.6
    assert net_clearance_map(rows) == {"B": 0.6}


def test_a_voltage_the_table_cannot_answer_is_not_invented():
    """Above 500 V in a column with no recorded slope: 'not stated' != 'none'."""
    rows = plan_net_clearances({"HV": 900.0}, BASE, column="B1")
    assert rows["HV"]["required_mm"] is None
    assert rows["HV"]["applied"] is False
    assert "states no spacing" in rows["HV"]["reason"]
    assert net_clearance_map(rows) == {}
    # B2 DOES carry the slope, so the same voltage is answerable there.
    b2 = plan_net_clearances({"HV": 900.0}, BASE)
    assert b2["HV"]["required_mm"] == pytest.approx(2.5 + 400 * 0.005)
    assert b2["HV"]["applied"] is True


def test_no_base_clearance_means_every_stated_requirement_is_emitted():
    """With no board clearance to compare against, nothing can be ruled out."""
    rows = plan_net_clearances({"VIN": 24.0}, None)
    assert net_clearance_map(rows) == {"VIN": 0.1}


def test_empty_and_malformed_input_are_quiet():
    assert plan_net_clearances(None, BASE) == {}
    assert plan_net_clearances({}, BASE) == {}
    rows = plan_net_clearances({"VIN": "not a number"}, BASE)
    assert rows["VIN"]["applied"] is False
    assert net_clearance_map(rows) == {}


def test_every_record_says_why_including_that_a_rating_is_not_a_measurement():
    """The arc's rule: a number that ships must carry where it came from."""
    rows = plan_net_clearances({"VIN": 72.0}, BASE)
    assert all(r["reason"] for r in rows.values())
    assert "rating, not a measurement" in rows["VIN"]["reason"]


# --- Phase 11: a board-wide floor needs no engine feature -------------------

def test_a_board_wide_floor_is_expressible_with_the_shipped_api():
    """The Phase-11 plan offered two semantics for unrated nets and recommended
    (a), "a board-wide floor". It turns out to need **no code**: voltages are
    data, so a producer that hands every net the board's peak voltage gets a map
    naming every net. This pins that the API composes that way.

    ⚠ And it is redundant in practice. KRT already derives a routing-side floor
    from the map -- ``net_clearance_floor = max(clearance, max over routed nets
    in the map)`` -- and applies it to every obstacle. Measured on
    ``uc3844_flyback``: the lever named 5 nets and all 17 widened. See
    :mod:`skidl_layout.power_clearance`'s module docstring for the retraction.
    """
    every_net = {n: 72.0 for n in ("VIN", "UVLO", "INTVCC", "SS", "RT_N")}

    records = plan_net_clearances(every_net, base_clearance_mm=0.25)
    mapping = net_clearance_map(records)

    assert set(mapping) == set(every_net)
    assert set(mapping.values()) == {0.6}


def test_widening_stays_the_only_direction_even_at_board_scale():
    """The floor must never relax a net that was already fine -- the property
    that makes a board-wide map safe to hand to the router."""
    records = plan_net_clearances(
        {"HV": 72.0, "SIG": 5.0}, base_clearance_mm=0.25)

    assert records["HV"]["applied"] is True
    assert records["SIG"]["applied"] is False        # 0.1mm asked < 0.25 board
    assert net_clearance_map(records) == {"HV": 0.6}
