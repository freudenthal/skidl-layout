"""A board declares its own coating, and the declaration carries its source.

Power-layout Phase 12, WS-5. ``spacing_column=`` had existed since Phase 8 and
nothing ever *chose* it: the most consequential input to the spacing judge was
the one input no board could state about itself.

⚠⚠ Column B4 asks **0.13 mm** where B2 asks **0.6 mm** at 72 V. That 4.6x
relaxation is legitimate only when a permanent polymer conformal coat is
actually applied over the assembly -- soldermask is not one. So the resolver's
job is not to be permissive; it is to make the claim, its source and any
contradiction **explicit**, and to refuse rather than guess.
"""

from __future__ import annotations

import pytest

from skidl_layout import resolve_spacing_column
from skidl_layout.fabspec import (
    DEFAULT_SPACING_COLUMN,
    IPC2221B_TABLE_6_1_MM,
    ipc2221_spacing_mm,
)


# --- the three resolution paths --------------------------------------------

def test_nothing_declared_stays_at_the_uncoated_default():
    """⛔ The default must not move. Every board this arc has ever graded was
    graded at B2, and a silent relaxation would re-grade the whole corpus."""
    got = resolve_spacing_column()
    assert got["column"] == DEFAULT_SPACING_COLUMN == "B2"
    assert got["declared"] is None
    assert got["conformal_coating"] is None
    assert got["conflict"] is False
    assert "nothing declared" in got["reason"]


def test_declaring_a_coat_moves_the_board_to_b4():
    got = resolve_spacing_column(conformal_coating=True)
    assert got["column"] == "B4"
    assert got["conformal_coating"] is True
    assert got["conflict"] is False


def test_declaring_no_coat_is_recorded_and_is_not_the_same_as_silence():
    """"We checked, and it is uncoated" and "nobody said" both grade at B2, but
    they are different facts and the record must be able to tell them apart."""
    said = resolve_spacing_column(conformal_coating=False)
    silent = resolve_spacing_column()
    assert said["column"] == silent["column"] == "B2"
    assert said["conformal_coating"] is False
    assert silent["conformal_coating"] is None
    assert said["reason"] != silent["reason"]


def test_an_explicit_column_wins_and_the_disagreement_is_recorded():
    """⛔ Never silently resolved. A board whose column and coating disagree has
    a real problem in its own metadata, and the resolver's job is to surface it,
    not to average it away."""
    got = resolve_spacing_column(declared_column="B2", conformal_coating=True)
    assert got["column"] == "B2"
    assert got["conflict"] is True
    assert "conflicts with" in got["reason"]


def test_an_agreeing_column_and_coating_is_not_a_conflict():
    got = resolve_spacing_column(declared_column="B4", conformal_coating=True)
    assert got["column"] == "B4"
    assert got["conflict"] is False


def test_column_names_are_case_and_whitespace_tolerant():
    assert resolve_spacing_column(declared_column=" b4 ")["column"] == "B4"


# --- the refusals ----------------------------------------------------------

def test_an_unknown_column_raises_rather_than_falling_back():
    """⛔⛔ The whole point. Silently grading at B2 because "B3" was misspelled is
    the quiet-wrong-number failure mode this arc keeps paying for -- and B3 is
    the *likeliest* misspelling, because it is a real IPC column this stack
    deliberately does not carry."""
    with pytest.raises(ValueError) as excinfo:
        resolve_spacing_column(declared_column="B3")
    assert "B3" in str(excinfo.value)
    # The message must name what IS available, or the caller cannot recover.
    for known in IPC2221B_TABLE_6_1_MM:
        assert known in str(excinfo.value)


def test_an_unknown_column_raises_even_with_a_coating_that_would_imply_one():
    with pytest.raises(ValueError):
        resolve_spacing_column(declared_column="B9", conformal_coating=True)


# --- the source, which is the safety-relevant half -------------------------

def test_the_source_is_recorded_verbatim_in_the_reason():
    got = resolve_spacing_column(conformal_coating=True, source="lt3758_flyback.py")
    assert got["source"] == "lt3758_flyback.py"
    assert "lt3758_flyback.py" in got["reason"]


def test_an_unsourced_declaration_still_resolves_but_names_nobody():
    got = resolve_spacing_column(conformal_coating=True)
    assert got["source"] is None
    assert "declared by" not in got["reason"]


# --- and the reason the declaration matters at all -------------------------

def test_the_two_columns_really_do_differ_by_the_amount_the_docstring_claims():
    """The 4.6x at 72 V is the number that makes a false declaration dangerous,
    so it is pinned here rather than left as prose."""
    b2 = ipc2221_spacing_mm(72.0, column="B2")
    b4 = ipc2221_spacing_mm(72.0, column="B4")
    assert b2 == pytest.approx(0.6)
    assert b4 == pytest.approx(0.13)
    assert b2 / b4 > 4.0
