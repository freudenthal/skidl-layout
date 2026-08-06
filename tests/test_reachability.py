# -*- coding: utf-8 -*-
"""Tests for the PASSABLE / CAGED / UNDETERMINED reachability verdict.

⛔ Everything here is **offline and synthetic**: no router, no KRT, no board on
disk. The module only needs an object with ``segments`` / ``vias`` /
``footprints`` / ``nets`` / ``board_info.copper_layers``, so the fixtures below
build exactly that — which is also the cheapest proof that the port did not
quietly acquire a dependency on the parser.

⭐ **The point of this module is a DISTINCTION, so the tests are built around
it**: a throat wide enough for the track is ``PASSABLE`` even when the router
failed (that is a *router* finding), a throat narrower than the track is
``CAGED`` (that one is geometry), and a view that never held the rest of the net
is ``UNDETERMINED`` (nothing was asked).

⛔⛔ **The third verdict is a CORRECTION to the source and it has its own
test.** KRT's version derives ``caged`` from ``bottleneck is None``, which is
also true when the view was simply too small — so a question that was never
asked reports the verdict that blames the placement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from skidl_layout.reachability import (  # noqa: E402
    VERDICTS,
    ReachabilityError,
    explain_route_failure,
    pad_reachability,
    slack_field,
    track_and_clearance,
    widest_path,
)

#: ⛔ The numbers every verdict here is stated against, and they carry their
#: source: ``oshpark-2l``'s track and its **fab floor** clearance. Never
#: re-hardcoded inside a test body.
TRACK_MM = 0.3
CLEARANCE_MM = 0.1524


# --------------------------------------------------------------------------- #
# the smallest board object the module can answer about
# --------------------------------------------------------------------------- #
@dataclass
class _Pad:
    global_x: float
    global_y: float
    size_x: float
    size_y: float
    net_id: int
    layers: tuple = ("F.Cu",)
    drill: float = 0.0
    pad_type: str = "smd"
    rect_rotation: float = 0.0


@dataclass
class _Footprint:
    pads: list


@dataclass
class _Net:
    name: str


@dataclass
class _BoardInfo:
    copper_layers: list = field(default_factory=lambda: ["F.Cu"])


@dataclass
class _PCB:
    footprints: dict
    nets: dict
    segments: list = field(default_factory=list)
    vias: list = field(default_factory=list)
    board_info: _BoardInfo = field(default_factory=_BoardInfo)


def _two_pads_with_a_gate(gate_mm: float, *, net=1, foreign=2):
    """Two pads of ``net`` at x=0 and x=4, walled apart by two foreign pads.

    The wall leaves a gap of ``gate_mm`` on the y axis at x=2, so the throat is
    a number the test chooses rather than one the fixture happens to have.
    """
    seed = _Pad(0.0, 0.0, 0.4, 0.4, net)
    far = _Pad(4.0, 0.0, 0.4, 0.4, net)
    half = gate_mm / 2.0
    # ⚠ The wall must run PAST the view, or the path simply goes around it and
    # the fixture measures the view instead of the gate. Caught by the CAGED
    # cases failing on a 3 mm wall inside a 8 mm-tall view — the fixture was
    # wrong, not the module.
    wall_h = 20.0
    top = _Pad(2.0, -(half + wall_h / 2.0), 0.6, wall_h, foreign)
    bottom = _Pad(2.0, +(half + wall_h / 2.0), 0.6, wall_h, foreign)
    pcb = _PCB(footprints={"U1": _Footprint([seed, far, top, bottom])},
               nets={net: _Net("SIG"), foreign: _Net("GND")})
    return pcb, seed


def _reach(pcb, seed, **kw):
    kw.setdefault("track_mm", TRACK_MM)
    kw.setdefault("base_clearance", CLEARANCE_MM)
    kw.setdefault("via_mm", 0.6)
    kw.setdefault("layers", ["F.Cu"])
    kw.setdefault("step", 0.02)
    kw.setdefault("view", (-1.0, -4.0, 5.0, 4.0))
    return pad_reachability(pcb, (seed.global_x, seed.global_y), net_id=1, **kw)


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #
def test_a_wide_gate_is_passable():
    """A 2 mm gate passes a 0.3 mm track with room to spare."""
    pcb, seed = _two_pads_with_a_gate(2.0)
    result = _reach(pcb, seed)
    assert result.verdict == "PASSABLE"
    assert result.caged is False
    assert result.determined is True
    assert result.margin_um > 0


def test_a_sealed_gate_is_caged_with_no_margin_to_report():
    """⛔ Geometry, not the router. ⭐ **The harder kind of CAGED**: at 0.1 mm
    the clearance alone seals the gap, so there is no positive-slack path at
    **any** width and ``bottleneck_mm`` is ``None``. ``margin_um`` is then
    ``None`` too — *how far short* is not a question a sealed gate answers, and
    reporting a number there would be inventing one."""
    pcb, seed = _two_pads_with_a_gate(0.1)
    result = _reach(pcb, seed)
    assert result.verdict == "CAGED"
    assert result.caged is True
    assert result.determined is True, "the far pad IS in the view"
    assert result.bottleneck_mm is None
    assert result.margin_um is None


def test_a_gate_that_passes_something_but_not_the_track_is_caged_with_a_margin():
    """⭐ The **softer** kind of CAGED, and the one worth reading: a path
    exists, it is just narrower than the track, so the shortfall is a number a
    human can act on."""
    pcb, seed = _two_pads_with_a_gate(0.45)
    result = _reach(pcb, seed)
    assert result.verdict == "CAGED"
    assert result.bottleneck_mm is not None
    assert result.margin_um < 0


def test_the_verdict_turns_over_where_the_arithmetic_says_it_should():
    """⭐ The boundary is ``gate >= track + 2 x clearance``, and it is asserted
    from **both** sides rather than at one convenient point."""
    needed = TRACK_MM + 2 * CLEARANCE_MM
    wide, seed_w = _two_pads_with_a_gate(needed + 0.20)
    tight, seed_t = _two_pads_with_a_gate(needed - 0.20)
    assert _reach(wide, seed_w).verdict == "PASSABLE"
    assert _reach(tight, seed_t).verdict == "CAGED"


def test_the_bottleneck_measures_the_gate_not_the_view():
    """The reported throat tracks the gate it was built from."""
    narrow = _reach(*_two_pads_with_a_gate(1.0)).bottleneck_mm
    wide = _reach(*_two_pads_with_a_gate(2.0)).bottleneck_mm
    assert narrow is not None and wide is not None
    assert wide > narrow


# --------------------------------------------------------------------------- #
# ⛔⛔ the third verdict — the correction to the source
# --------------------------------------------------------------------------- #
def test_a_view_without_the_rest_of_the_net_is_UNDETERMINED_not_CAGED():
    """⛔⛔ **Standing finding 1, in geometry's clothing.**

    ``bottleneck_mm`` is ``None`` both when nothing can get through *and* when
    the view never contained anything to get to. Collapsing those two into
    ``CAGED`` reports *"the placement is impossible"* about a question that was
    never asked — and CAGED is the direction that blames the board.
    """
    pcb, seed = _two_pads_with_a_gate(2.0)
    result = _reach(pcb, seed, view=(-1.0, -1.0, 1.0, 1.0))
    assert result.target_cells == 0
    assert result.determined is False
    assert result.verdict == "UNDETERMINED"
    assert result.caged is False, \
        "a question that was never asked must not answer 'the geometry is " \
        "impossible' — that is the whole reason this verdict exists"
    assert "widen" in result.note.lower()


def test_undetermined_blames_nobody():
    """⭐ Three answers, never two: ``unknown`` is not ``router``."""
    pcb, seed = _two_pads_with_a_gate(2.0)
    blob = explain_route_failure(
        pcb, (seed.global_x, seed.global_y), fab=None, net_id=1,
        track_mm=TRACK_MM, base_clearance=CLEARANCE_MM, via_mm=0.6,
        layers=["F.Cu"], step=0.02, view=(-1.0, -1.0, 1.0, 1.0))
    assert blob["blame"] == "unknown"
    assert blob["verdict"] == "UNDETERMINED"


def test_every_verdict_this_module_can_return_is_declared():
    """⛔ Standing finding 20: a declared constant that matches nothing, and an
    emitted value declared nowhere, are both failures."""
    seen = set()
    pcb, seed = _two_pads_with_a_gate(2.0)
    seen.add(_reach(pcb, seed).verdict)
    seen.add(_reach(pcb, seed, view=(-1.0, -1.0, 1.0, 1.0)).verdict)
    seen.add(_reach(*_two_pads_with_a_gate(0.1)).verdict)
    assert seen == set(VERDICTS), \
        f"emitted {sorted(seen)} against declared {sorted(VERDICTS)}"


# --------------------------------------------------------------------------- #
# blame
# --------------------------------------------------------------------------- #
def test_blame_is_the_router_when_the_geometry_passes():
    """⭐ The finding-21 question, answered: the router said no and the board
    says a track fits, so the lever is the ROUTER, not the placement."""
    pcb, seed = _two_pads_with_a_gate(2.0)
    blob = explain_route_failure(
        pcb, (seed.global_x, seed.global_y), fab=None, net_id=1,
        track_mm=TRACK_MM, base_clearance=CLEARANCE_MM, via_mm=0.6,
        layers=["F.Cu"], step=0.02, view=(-1.0, -4.0, 5.0, 4.0))
    assert blob["blame"] == "router"
    assert blob["verdict"] == "PASSABLE"


def test_blame_is_geometry_when_the_throat_is_too_narrow():
    pcb, seed = _two_pads_with_a_gate(0.1)
    blob = explain_route_failure(
        pcb, (seed.global_x, seed.global_y), fab=None, net_id=1,
        track_mm=TRACK_MM, base_clearance=CLEARANCE_MM, via_mm=0.6,
        layers=["F.Cu"], step=0.02, view=(-1.0, -4.0, 5.0, 4.0))
    assert blob["blame"] == "geometry"


# --------------------------------------------------------------------------- #
# the clearance model
# --------------------------------------------------------------------------- #
def test_a_looser_clearance_can_turn_passable_into_caged():
    """⭐ The verdict is a function of the clearance it was graded at, which is
    why the number carries its source and is never guessed."""
    pcb, seed = _two_pads_with_a_gate(0.7)
    assert _reach(pcb, seed, base_clearance=0.10).verdict == "PASSABLE"
    assert _reach(pcb, seed, base_clearance=0.30).verdict == "CAGED"


def test_per_class_clearance_is_honoured_per_net():
    """⛔ One grid per clearance class. A foreign net given a wide clearance
    eats the gate even though the base clearance would pass it."""
    pcb, seed = _two_pads_with_a_gate(0.7)
    tight = _reach(pcb, seed, base_clearance=0.10)
    loose = _reach(pcb, seed, base_clearance=0.10, net_clearances={2: 0.30})
    assert tight.verdict == "PASSABLE"
    assert loose.verdict == "CAGED"


def test_npth_pads_carry_no_copper():
    """⛔ Standing finding 15(c): an ``np_thru_hole`` has no copper even when
    its layers list ``*.Cu``. Blocking on it would manufacture a cage."""
    pcb, seed = _two_pads_with_a_gate(2.0)
    wall = _Pad(2.0, 0.0, 1.6, 1.6, 3, layers=("*.Cu",), drill=1.0,
                pad_type="np_thru_hole")
    pcb.footprints["U1"].pads.append(wall)
    pcb.nets[3] = _Net("MOUNT")
    assert _reach(pcb, seed).verdict == "PASSABLE"


def test_a_plated_barrel_blocks_every_layer():
    """⚠ The mirror of the test above: a *plated* through-hole is copper, and
    it blocks the same gate."""
    pcb, seed = _two_pads_with_a_gate(2.0)
    wall = _Pad(2.0, 0.0, 1.8, 1.8, 3, layers=("*.Cu",), drill=0.8,
                pad_type="thru_hole")
    pcb.footprints["U1"].pads.append(wall)
    pcb.nets[3] = _Net("MOUNT")
    assert _reach(pcb, seed).verdict == "CAGED"


# --------------------------------------------------------------------------- #
# refusals — an instrument that cannot answer must say so
# --------------------------------------------------------------------------- #
def test_it_refuses_to_guess_the_widths():
    """⛔ No FabSpec and no explicit numbers is a raise, never a default: a
    throat graded at a guessed clearance manufactures or hides a verdict."""
    pcb, seed = _two_pads_with_a_gate(2.0)
    with pytest.raises(ReachabilityError, match="will not guess"):
        pad_reachability(pcb, (seed.global_x, seed.global_y), net_id=1,
                         layers=["F.Cu"])


def test_a_board_reporting_zero_copper_layers_raises_and_names_the_issue():
    """⛔ Open issue 10: KRT parses every board this stack writes as ZERO
    copper layers, and an in-process consumer then gets "Cannot determine
    endpoints" on every route — which reads exactly like an empty board."""
    pcb, seed = _two_pads_with_a_gate(2.0)
    pcb.board_info.copper_layers = []
    with pytest.raises(ReachabilityError, match="open issue 10"):
        pad_reachability(pcb, (seed.global_x, seed.global_y), net_id=1,
                         track_mm=TRACK_MM, base_clearance=CLEARANCE_MM,
                         via_mm=0.6)


def test_a_seed_off_its_own_copper_raises():
    pcb, seed = _two_pads_with_a_gate(2.0)
    with pytest.raises(ReachabilityError, match="not on"):
        pad_reachability(pcb, (1.0, 1.0), net_id=1, track_mm=TRACK_MM,
                         base_clearance=CLEARANCE_MM, via_mm=0.6,
                         layers=["F.Cu"], step=0.02,
                         view=(-1.0, -4.0, 5.0, 4.0))


def test_an_unknown_net_raises():
    pcb, seed = _two_pads_with_a_gate(2.0)
    with pytest.raises(ReachabilityError, match="no net named"):
        pad_reachability(pcb, (0.0, 0.0), net_name="NOPE", track_mm=TRACK_MM,
                         base_clearance=CLEARANCE_MM, via_mm=0.6)


def test_an_empty_view_raises_rather_than_measuring_nothing():
    pcb, _ = _two_pads_with_a_gate(2.0)
    with pytest.raises(ReachabilityError, match="observes nothing|empty view"):
        slack_field(pcb, 1, "F.Cu", (0.0, 0.0, 0.0, 0.0), CLEARANCE_MM,
                    None, 0.02)


# --------------------------------------------------------------------------- #
# determinism and provenance
# --------------------------------------------------------------------------- #
def test_the_answer_is_a_function_of_its_inputs():
    """⛔ No RNG, no clock, no set order. Two calls, one answer."""
    pcb, seed = _two_pads_with_a_gate(1.0)
    first = _reach(pcb, seed).to_dict()
    second = _reach(pcb, seed).to_dict()
    assert first == second


def test_every_number_carries_its_source():
    """⛔ The house rule since S1. A width quoted without its origin is a
    constant wearing a formula's clothes."""
    from skidl_layout.fabspec import resolve_fab_spec

    fab = resolve_fab_spec("oshpark-2l")
    track, clearance, t_src, c_src = track_and_clearance(fab)
    assert track == fab.track_width_mm
    assert clearance == fab.min_clearance_mm, \
        "the throat is graded at the FAB FLOOR, not at the design clearance"
    assert "FabSpec" in t_src and "FabSpec" in c_src

    pcb, seed = _two_pads_with_a_gate(2.0)
    result = pad_reachability(pcb, (seed.global_x, seed.global_y), net_id=1,
                              fab=fab, layers=["F.Cu"], step=0.02,
                              view=(-1.0, -4.0, 5.0, 4.0))
    assert result.track_source == t_src
    assert result.clearance_source == c_src

    explicit = _reach(pcb, seed)
    assert explicit.track_source == "caller"
    assert explicit.clearance_source == "caller"


def test_widest_path_returns_none_when_nothing_is_reachable():
    """⚠ The primitive's own contract, separate from the verdict's."""
    slacks = [np.full((5, 5), -1.0)]
    targets = [np.zeros((5, 5), dtype=bool)]
    targets[0][0, 4] = True
    via_ok = np.zeros((5, 5), dtype=bool)
    got = widest_path(slacks, targets, via_ok, (0.0, 0.0), 0,
                      (0.0, 0.0, 5.0, 5.0), 1.0)
    assert got is None


# --------------------------------------------------------------------------- #
# ⛔ the leaf rule
# --------------------------------------------------------------------------- #
#: ⛔⛔ **The named consumers of the reachability instrument, and the list moved
#: for the first time on 2026-08-05 (S5B).** This module shipped saying *"nothing
#: imports it **yet**"*, and S5B's plan then named
#: ``reachability.explain_route_failure`` as the **blame column** of gates
#: ``BD3``/``BD4`` — so the plan mandated a consumer for a module whose own test
#: asserted it had none, and the full suite is what said so.
#:
#: ⭐ The exemption takes ``ESCAPE_MAP_CONSUMERS``' shape exactly, including its
#: second guard: **a permitted consumer must itself be a leaf**, or the leaf rule
#: is laundered through one indirection.
#: ⛔ ``construct.py`` calls it **on failures only** and **never steers on the
#: verdict** (overview §5.8) — it is a diagnostic, not a judge.
REACHABILITY_CONSUMERS = {"construct.py"}


def test_reachability_is_a_leaf_only_its_named_consumer_imports_it():
    """⛔ Nothing in ``skidl_layout`` may import this module except the
    constructive loop's blame column.

    ⚠ **Import-aware, never a substring scan** (standing finding 16): a
    docstring that *mentions* the module is documentation, and this arc wants
    more of it, not less.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    pattern = re.compile(
        r"^\s*(?:from\s+\.?reachability\s+import|import\s+.*\breachability\b"
        r"|from\s+\.\s+import\s+.*\breachability\b)", re.MULTILINE)
    importers = [path.name for path in sorted(root.glob("*.py"))
                 if path.name != "reachability.py"
                 and pattern.search(path.read_text(encoding="utf-8"))]
    unexpected = sorted(set(importers) - REACHABILITY_CONSUMERS)
    assert unexpected == [], f"reachability is imported by {unexpected}"


def test_every_permitted_consumer_of_reachability_is_itself_a_leaf():
    """⭐ The exemption's own guard. A permitted consumer the **engine** can
    reach would launder the leaf rule through one indirection, which is exactly
    the shape of mistake this arc keeps paying for."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    for consumer in sorted(REACHABILITY_CONSUMERS):
        stem = consumer[:-3]
        pattern = re.compile(
            rf"^[ \t]*(?:from[ \t]+[.\w]*\b{stem}\b[ \t]+import"
            rf"|from[ \t]+[.\w]+[ \t]+import[ \t]+[^#\n]*\b{stem}\b"
            rf"|import[ \t]+[^#\n]*\b{stem}\b)", re.MULTILINE)
        importers = [path.name for path in sorted(root.glob("*.py"))
                     if path.name != consumer
                     and pattern.search(path.read_text(encoding="utf-8"))]
        assert importers == [], \
            f"{consumer} is permitted to import reachability but is NOT a " \
            f"leaf -- it is imported by {importers}"
