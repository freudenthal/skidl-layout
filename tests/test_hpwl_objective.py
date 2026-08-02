"""The HPWL objective: centroid (shipped) -> pads (opt-in).

⛔⛔ **What this exists to protect.** With the crossing term promoted to MST,
HPWL is still the placer's only *continuous* quality signal -- and it was
measuring part **centres**. A centroid cannot see a rotation, so the objective
was structurally blind to one of only two degrees of freedom a part (or a layout
cell) has. ``hpwl_objective="pads"`` points the same two terms at the pad
positions ``_placement_pad_points`` already produces for the MST crossing term.

Three things are pinned here, in the order they can fail:

1. ⭐⭐ **The default is a TRUE no-op.** ``"centroid"`` must reproduce every
   historical number, and it must stay the default in every place the default
   is declared -- two defaults would make a hand-scored placement silently
   incomparable with a planned one.
2. ⭐⭐⭐ **``"pads"`` makes rotation visible**, including for a symmetric cell
   whose centroid is rotation-invariant by construction. That is the whole
   point of the change, so it is asserted directly rather than inferred from a
   corpus number.
3. ⛔ **The net SET does not move.** Exactly one thing changes -- where the
   points come from. A net still needs two distinct placed refs to contribute,
   so a widened net set cannot be smuggled in alongside.
"""

from __future__ import annotations

import inspect

import pytest

from skidl_layout.constraints import BoardOutline
from skidl_layout.engine import _resolve_hpwl_objective
from skidl_layout.geometry import FootprintGeometry, PadGeometry
from skidl_layout.scoring import (
    DEFAULT_HPWL_OBJECTIVE,
    HPWL_OBJECTIVE_CENTROID,
    HPWL_OBJECTIVE_PADS,
    HPWL_OBJECTIVES,
    _hpwl_by_net,
    _net_pad_extents,
    _placement_pad_points,
    _total_hpwl,
    _weighted_hpwl,
    score_placement,
)
from skidl_layout.writer import PlacedPart

from tests.test_layout_scoring import BBOXES, _Circuit, _Net, _Part


def _wide_pad_geometries():
    """A 12x12 part whose two pads sit 4 mm apart along its own X axis.

    ⭐ The pad offset is what makes the two modes differ at all: rotate this
    part 90 degrees and its centroid does not move one micron while its pads
    swap from an east-west pair to a north-south one.
    """
    return {
        "Package_QFP:MCU": FootprintGeometry(
            footprint="Package_QFP:MCU",
            pads=(
                PadGeometry(number="1", x_mm=-2.0, y_mm=0.0,
                            width_mm=1.0, height_mm=1.0),
                PadGeometry(number="2", x_mm=2.0, y_mm=0.0,
                            width_mm=1.0, height_mm=1.0),
            ),
        )
    }


def _two_part_board():
    """Two parts on one signal net, side by side on the X axis.

    ⭐ ``SIG`` deliberately lands on U1's **pad 2** (its east pad) and U2's
    **pad 1** (its west pad), i.e. on the two pads that FACE each other. If both
    parts carried it on the same pad number the two modes would agree by
    accident -- the pad offsets would cancel -- and the fixture would pass while
    exercising nothing. (They did, on the first draft of this file.)
    """
    sig = _Net("SIG")
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU",
              nets=[_Net("U1_NC"), sig]),
        _Part("U2", name="MCU", footprint="Package_QFP:MCU",
              nets=[sig, _Net("U2_NC")]),
    ]
    circuit = _Circuit(parts, [sig])
    placed = [
        PlacedPart("U1", 20.0, 40.0, 0.0, "Package_QFP:MCU"),
        PlacedPart("U2", 60.0, 40.0, 0.0, "Package_QFP:MCU"),
    ]
    return circuit, placed


# --------------------------------------------------------------------------- #
# 1. The default is centroid, everywhere, and it is a true no-op
# --------------------------------------------------------------------------- #
def test_default_is_centroid_everywhere_it_is_declared():
    from skidl_layout.refinement import refine_candidate_placement, refine_placement

    assert DEFAULT_HPWL_OBJECTIVE == HPWL_OBJECTIVE_CENTROID
    for fn in (score_placement, refine_placement, refine_candidate_placement):
        assert (inspect.signature(fn).parameters["hpwl_objective"].default
                == HPWL_OBJECTIVE_CENTROID)


def test_implicit_default_scores_as_centroid_not_pads():
    circuit, placed = _two_part_board()
    outline = BoardOutline(100.0, 100.0)
    geometries = _wide_pad_geometries()

    implicit = score_placement(placed, circuit, BBOXES, outline=outline,
                               fp_geometries=geometries)
    as_centroid = score_placement(placed, circuit, BBOXES, outline=outline,
                                  fp_geometries=geometries,
                                  hpwl_objective=HPWL_OBJECTIVE_CENTROID)
    as_pads = score_placement(placed, circuit, BBOXES, outline=outline,
                              fp_geometries=geometries,
                              hpwl_objective=HPWL_OBJECTIVE_PADS)
    assert implicit.total_hpwl_mm == as_centroid.total_hpwl_mm
    assert implicit.weighted_hpwl_mm == as_centroid.weighted_hpwl_mm
    # U1 pad 2 at x=22, U2 pad 1 at x=58 -> a 36 mm span, not the 40 mm the
    # centroids report. The two modes must not agree here or the fixture is
    # not exercising the change.
    assert as_pads.total_hpwl_mm == pytest.approx(36.0)
    assert as_centroid.total_hpwl_mm == pytest.approx(40.0)


def test_centroid_mode_ignores_geometry_entirely():
    """⛔ The shipped path must not start consulting pads by accident."""
    circuit, placed = _two_part_board()
    outline = BoardOutline(100.0, 100.0)

    with_geometry = score_placement(placed, circuit, BBOXES, outline=outline,
                                    fp_geometries=_wide_pad_geometries(),
                                    crossing_objective="legacy")
    without = score_placement(placed, circuit, BBOXES, outline=outline,
                              crossing_objective="legacy")
    assert with_geometry.total_hpwl_mm == without.total_hpwl_mm
    assert with_geometry.weighted_hpwl_mm == without.weighted_hpwl_mm


# --------------------------------------------------------------------------- #
# 2. ⭐⭐⭐ Rotation becomes visible -- the reason the mode exists
# --------------------------------------------------------------------------- #
def test_pads_mode_sees_a_rotation_that_centroid_mode_cannot():
    circuit, placed = _two_part_board()
    outline = BoardOutline(100.0, 100.0)
    geometries = _wide_pad_geometries()
    turned = [PlacedPart("U1", 20.0, 40.0, 90.0, "Package_QFP:MCU"), placed[1]]

    for objective, expect_equal in ((HPWL_OBJECTIVE_CENTROID, True),
                                    (HPWL_OBJECTIVE_PADS, False)):
        flat = score_placement(placed, circuit, BBOXES, outline=outline,
                               fp_geometries=geometries,
                               hpwl_objective=objective)
        spun = score_placement(turned, circuit, BBOXES, outline=outline,
                               fp_geometries=geometries,
                               hpwl_objective=objective)
        assert ((flat.total_hpwl_mm == spun.total_hpwl_mm) is expect_equal), (
            f"{objective}: rotation-visibility is the point of this mode")


def test_symmetric_two_part_cell_rotates_visibly_in_pads_mode():
    """⭐⭐⭐ The section-3.6 case, end to end through the cell masquerade.

    A cell presents ONE centroid for every net it touches, so centroid-HPWL
    reports the identical position for all of them and a cell's rotation is
    invisible by construction. Its ``fp_geometries`` entry already carries one
    synthetic pad per escaping net at that net's escape point, so ``"pads"``
    mode consumes the section-3.6 points with no cell-side work at all.
    """
    from skidl_layout.cells import cell_geometry

    from tests.test_cells import _divider_cell

    cell = _divider_cell()
    key = "@CELL:fb_divider"
    geometries = dict(_wide_pad_geometries())
    geometries[key] = cell_geometry(cell, key)

    # ⛔ Pin order must follow ``cell_pad_numbers`` (escaping nets, sorted,
    # numbered from 1) -- ``_placement_pad_points`` joins the circuit's pad->net
    # map to the geometry's pad->position map on the pad NUMBER, so a fixture
    # that disagrees would silently measure the wrong pad.
    nets = [_Net(name) for name in cell.escaping_nets]
    assert len(nets) >= 2, "the fixture cell must reach at least two nets"
    pseudo = _Part("@CELL1", name="cell", footprint=key, nets=nets)
    far = _Part("U9", name="MCU", footprint="Package_QFP:MCU", nets=nets)
    circuit = _Circuit([pseudo, far], nets)

    bboxes = dict(BBOXES)
    bboxes[key] = (cell.width, cell.height)

    # ⚠ ``U9`` sits on the cell's own row, NOT on the diagonal: a symmetric cell
    # against a diagonal partner has the two nets' errors cancel exactly, and
    # the rotated arm scores the identical total for the wrong reason.
    def _hpwl(rot, objective):
        placed = [PlacedPart("@CELL1", 30.0, 30.0, rot, key),
                  PlacedPart("U9", 70.0, 30.0, 0.0, "Package_QFP:MCU")]
        return score_placement(placed, circuit, bboxes,
                               outline=BoardOutline(100.0, 100.0),
                               fp_geometries=geometries,
                               hpwl_objective=objective).total_hpwl_mm

    assert (_hpwl(0.0, HPWL_OBJECTIVE_CENTROID)
            == _hpwl(90.0, HPWL_OBJECTIVE_CENTROID)), \
        "centroid mode is rotation-blind by design"
    assert (_hpwl(0.0, HPWL_OBJECTIVE_PADS)
            != _hpwl(90.0, HPWL_OBJECTIVE_PADS)), \
        "pads mode must see the cell turn"


# --------------------------------------------------------------------------- #
# 3. ⛔ Exactly one thing changes -- the net set does not
# --------------------------------------------------------------------------- #
def test_pads_mode_keeps_the_centroid_net_set():
    circuit, placed = _two_part_board()
    geometries = _wide_pad_geometries()
    points = _placement_pad_points(placed, circuit, geometries)
    extents = _net_pad_extents(points)

    centroid = _hpwl_by_net(placed, circuit,
                            hpwl_objective=HPWL_OBJECTIVE_CENTROID)
    pads = _hpwl_by_net(placed, circuit, hpwl_objective=HPWL_OBJECTIVE_PADS,
                        pad_extents=extents)
    assert [name for name, _ in centroid] == [name for name, _ in pads]


def test_a_net_on_one_part_contributes_nothing_in_either_mode():
    """⛔ A two-pad net on a single part has a non-zero pad box but no
    inter-part wire. Counting it would be a second change riding along."""
    solo = _Net("SOLO")
    part = _Part("U1", name="MCU", footprint="Package_QFP:MCU",
                 nets=[solo, solo])
    circuit = _Circuit([part], [solo])
    placed = [PlacedPart("U1", 20.0, 20.0, 0.0, "Package_QFP:MCU")]
    geometries = _wide_pad_geometries()
    extents = _net_pad_extents(
        _placement_pad_points(placed, circuit, geometries))

    assert _total_hpwl(placed, circuit,
                       hpwl_objective=HPWL_OBJECTIVE_CENTROID) == 0.0
    assert _total_hpwl(placed, circuit, hpwl_objective=HPWL_OBJECTIVE_PADS,
                       pad_extents=extents) == 0.0


def test_weighted_mode_applies_the_same_weights_to_the_pad_box():
    gnd = _Net("GND")
    parts = [
        _Part("U1", name="MCU", footprint="Package_QFP:MCU", nets=[gnd]),
        _Part("U2", name="MCU", footprint="Package_QFP:MCU", nets=[gnd]),
    ]
    circuit = _Circuit(parts, [gnd])
    placed = [PlacedPart("U1", 20.0, 40.0, 0.0, "Package_QFP:MCU"),
              PlacedPart("U2", 60.0, 40.0, 0.0, "Package_QFP:MCU")]
    extents = _net_pad_extents(_placement_pad_points(
        placed, circuit, _wide_pad_geometries()))

    total = _total_hpwl(placed, circuit, hpwl_objective=HPWL_OBJECTIVE_PADS,
                        pad_extents=extents)
    weighted = _weighted_hpwl(placed, circuit,
                              hpwl_objective=HPWL_OBJECTIVE_PADS,
                              pad_extents=extents)
    assert weighted == pytest.approx(total * 2.0)  # GND weight


def test_a_part_without_geometry_falls_back_to_its_centroid():
    """⚠ Keep the fallback and REPORT it. It is what keeps a board with an
    unresolvable footprint scoreable at all, and it is silent by nature."""
    circuit, placed = _two_part_board()
    extents = _net_pad_extents(
        _placement_pad_points(placed, circuit, {}))  # no geometry at all
    assert _total_hpwl(placed, circuit, hpwl_objective=HPWL_OBJECTIVE_PADS,
                       pad_extents=extents) == pytest.approx(
        _total_hpwl(placed, circuit,
                    hpwl_objective=HPWL_OBJECTIVE_CENTROID))


# --------------------------------------------------------------------------- #
# 4. The resolver -- explicit kwarg > env > default, unknown names raise
# --------------------------------------------------------------------------- #
def test_resolver_precedence_and_strictness(monkeypatch):
    monkeypatch.delenv("SKIDL_LAYOUT_HPWL_OBJECTIVE", raising=False)
    assert _resolve_hpwl_objective(None) == HPWL_OBJECTIVE_CENTROID
    assert _resolve_hpwl_objective("PADS") == HPWL_OBJECTIVE_PADS

    monkeypatch.setenv("SKIDL_LAYOUT_HPWL_OBJECTIVE", "pads")
    assert _resolve_hpwl_objective(None) == HPWL_OBJECTIVE_PADS
    assert _resolve_hpwl_objective("centroid") == HPWL_OBJECTIVE_CENTROID

    monkeypatch.setenv("SKIDL_LAYOUT_HPWL_OBJECTIVE", "")
    assert _resolve_hpwl_objective(None) == HPWL_OBJECTIVE_CENTROID


def test_unknown_objective_raises_rather_than_falling_back():
    """⛔ A typo that silently selected the shipped objective would make an A/B
    read as 'no effect' -- the one failure mode a graded lever cannot survive."""
    with pytest.raises(ValueError):
        _resolve_hpwl_objective("padz")
    assert set(HPWL_OBJECTIVES) == {HPWL_OBJECTIVE_CENTROID, HPWL_OBJECTIVE_PADS}


# --------------------------------------------------------------------------- #
# 5. ⛔⛔ The worker boundary -- the defect 1100 tests missed, one class up
# --------------------------------------------------------------------------- #
def test_worker_payload_unpacks_hpwl_objective_and_tolerates_short_payloads():
    """The ``crossing_objective`` promotion shipped a worker that scored against
    the MODULE default while the parent asked for another objective, and it
    corrupted exactly the >= 30-part boards. This pins the same slot for the
    HPWL knob: present when sent, defaulted when a stale payload omits it."""
    import pickle

    from skidl_layout.parallel import refine_candidate_worker

    source = inspect.getsource(refine_candidate_worker)
    assert "hpwl_objective=hpwl_objective" in source
    assert "fields[8]" in source

    short = pickle.loads(pickle.dumps((1, 2, 3, 4, 5.0, 2)))
    assert len(short) == 6  # a pre-knob payload still unpacks its first six


def test_plan_layout_exposes_the_knob():
    import skidl_layout as SL

    params = inspect.signature(SL.plan_layout).parameters
    assert "hpwl_objective" in params
    assert params["hpwl_objective"].default is None  # None -> env -> centroid
