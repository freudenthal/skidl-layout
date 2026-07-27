# -*- coding: utf-8 -*-
"""Phase 6 WS-1: the IPC-2221 sizing rule (gate F1).

The calibration anchors here are the plan's, computed from the formula rather
than copied from a chart: at 1 oz / dT 10 C / external, 1 A -> 0.300 mm,
2 A -> 0.781 mm, 4.44 A -> 2.35 mm. The first of those is the whole point of
the phase -- ``power.py``'s 0.3 mm magic number IS the IPC width of a 1 A trace.
"""

from __future__ import annotations

import dataclasses

import pytest

import skidl_layout as SL
from skidl_layout.current_widths import (
    DEFAULT_COPPER_THICKNESS_MM,
    ipc2221_width_mm,
    widths_from_currents,
)


# --------------------------------------------------------------------------- #
# The anchors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "amps,expected_mm",
    [(1.0, 0.300), (2.0, 0.781), (4.44, 2.349)],
)
def test_calibration_anchors(amps, expected_mm):
    """1 oz / dT 10 C / external -- the plan's three anchors, +/- 0.01 mm."""
    assert ipc2221_width_mm(amps) == pytest.approx(expected_mm, abs=0.01)


def test_one_amp_is_the_magic_number():
    """The finding: ``power.py``'s 0.3 mm ladder rung is a 1 A trace."""
    from skidl_layout.power import _suggest_width

    ladder = _suggest_width("GND", "ground", ["U1", "C1"])
    assert ladder == pytest.approx(0.3)
    assert ipc2221_width_mm(1.0) == pytest.approx(ladder, abs=0.005)


# --------------------------------------------------------------------------- #
# Shape of the curve
# --------------------------------------------------------------------------- #
def test_monotone_in_current():
    widths = [ipc2221_width_mm(i / 4.0) for i in range(1, 40)]
    assert widths == sorted(widths)
    assert len(set(widths)) == len(widths)


def test_zero_and_negative_need_no_copper():
    assert ipc2221_width_mm(0.0) == 0.0
    assert ipc2221_width_mm(-3.0) == 0.0


def test_internal_layer_is_about_2_6x_wider():
    """k halves -> area scales by 2 ** (1 / 0.725) ~= 2.60."""
    external = ipc2221_width_mm(2.0)
    internal = ipc2221_width_mm(2.0, internal=True)
    assert internal / external == pytest.approx(2 ** (1 / 0.725), rel=1e-9)
    assert internal / external == pytest.approx(2.60, abs=0.01)


def test_thicker_copper_needs_less_width():
    """2 oz copper carries the same current in half the width."""
    one_oz = ipc2221_width_mm(2.0, copper_thickness_mm=0.035)
    two_oz = ipc2221_width_mm(2.0, copper_thickness_mm=0.070)
    assert two_oz == pytest.approx(one_oz / 2.0, rel=1e-9)


def test_hotter_allowed_rise_needs_less_width():
    assert ipc2221_width_mm(2.0, delta_t_c=20.0) < ipc2221_width_mm(2.0, delta_t_c=10.0)


@pytest.mark.parametrize("kwargs", [{"copper_thickness_mm": 0.0}, {"delta_t_c": 0.0}])
def test_degenerate_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        ipc2221_width_mm(1.0, **kwargs)


# --------------------------------------------------------------------------- #
# widths_from_currents
# --------------------------------------------------------------------------- #
def test_widths_from_currents_uses_the_spec_thickness():
    spec = SL.OSHPARK_2L
    assert spec.copper_thickness_mm == pytest.approx(DEFAULT_COPPER_THICKNESS_MM)
    widths = widths_from_currents({"VOUT": 2.0}, spec=spec)
    assert widths["VOUT"] == pytest.approx(0.781, abs=0.01)


def test_widths_from_currents_honours_the_spec_floor():
    """A tiny current still cannot draw below the fab's own minimum track."""
    spec = SL.OSHPARK_2L
    widths = widths_from_currents({"SIG": 0.01}, spec=spec)
    assert widths["SIG"] == pytest.approx(spec.min_track_mm)
    assert ipc2221_width_mm(0.01) < spec.min_track_mm


def test_widths_from_currents_honours_the_cap():
    widths = widths_from_currents({"VIN": 4.44}, max_width_mm=1.0)
    assert widths["VIN"] == pytest.approx(1.0)


def test_two_oz_spec_narrows_the_result():
    spec = dataclasses.replace(SL.OSHPARK_2L, copper_weight_oz=2.0, name="2oz(test)")
    one = widths_from_currents({"VIN": 4.44}, spec=SL.OSHPARK_2L)["VIN"]
    two = widths_from_currents({"VIN": 4.44}, spec=spec)["VIN"]
    assert two == pytest.approx(one / 2.0, rel=1e-6)


def test_absent_is_not_zero():
    """None / zero / negative / unparseable -> the net is missing, not 0.0."""
    widths = widths_from_currents(
        {"A": None, "B": 0.0, "C": -1.0, "D": "not-a-number", "E": 1.0}
    )
    assert set(widths) == {"E"}


def test_empty_and_none_input():
    assert widths_from_currents({}) == {}
    assert widths_from_currents(None) == {}


def test_internal_flag_flows_through():
    external = widths_from_currents({"VIN": 2.0})["VIN"]
    internal = widths_from_currents({"VIN": 2.0}, internal=True)["VIN"]
    assert internal > external


def test_exported_from_the_package():
    assert SL.ipc2221_width_mm is ipc2221_width_mm
    assert SL.widths_from_currents is widths_from_currents
