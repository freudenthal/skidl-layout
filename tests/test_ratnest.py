"""Tests for :mod:`skidl_layout.ratnest`.

The geometry half needs no KiCadRoutingTools and no board on disk; the reading
half is an integration test that skips when either is unavailable.
"""

from __future__ import annotations

import math
import os

import pytest

from skidl_layout.ratnest import (
    Airwire,
    PadPoint,
    _rotate,
    analyse_board,
    count_crossings,
    is_plane_net,
    mst_edges,
    net_airwires,
    segments_cross,
    twisted_pairs,
)


def _pad(ref, pad, net, x, y):
    return PadPoint(ref=ref, pad=pad, net=net, x=x, y=y)


# --------------------------------------------------------------------------- #
# segments_cross -- the predicate every other number rests on
# --------------------------------------------------------------------------- #
def test_proper_crossing_is_detected():
    assert segments_cross((0, 0), (10, 10), (0, 10), (10, 0))


def test_disjoint_segments_do_not_cross():
    assert not segments_cross((0, 0), (1, 1), (5, 5), (6, 6))


def test_parallel_segments_do_not_cross():
    assert not segments_cross((0, 0), (10, 0), (0, 1), (10, 1))


def test_shared_endpoint_is_not_a_crossing():
    # Two wires leaving the same pad touch; they do not cross.
    assert not segments_cross((0, 0), (10, 10), (0, 0), (10, 0))


def test_t_junction_is_not_a_crossing():
    # An endpoint landing ON the other segment is a touch, not a proper cross.
    assert not segments_cross((0, 0), (10, 0), (5, 0), (5, 10))


def test_collinear_overlap_is_not_a_crossing():
    assert not segments_cross((0, 0), (10, 0), (5, 0), (15, 0))


# --------------------------------------------------------------------------- #
# mst_edges -- and its determinism, which the digests depend on
# --------------------------------------------------------------------------- #
def test_mst_of_fewer_than_two_points_is_empty():
    assert mst_edges([]) == []
    assert mst_edges([(0.0, 0.0)]) == []


def test_mst_of_two_points_is_one_edge():
    assert mst_edges([(0.0, 0.0), (3.0, 4.0)]) == [(0, 1)]


def test_mst_of_a_line_is_a_chain_not_a_star():
    """The whole point of using an MST: collinear pads chain, they do not spoke.

    ``scoring._estimate_crossings`` would spoke all three to one anchor, which
    is what makes it over-count on high-fanout nets.
    """
    edges = mst_edges([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])
    assert sorted(tuple(sorted(e)) for e in edges) == [(0, 1), (1, 2)]


def test_mst_edge_count_is_n_minus_one():
    pts = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0), (2.5, 2.5)]
    assert len(mst_edges(pts)) == len(pts) - 1


def test_mst_total_length_is_minimal_on_a_known_case():
    # Unit square + centre: the MST is four spokes from the centre (4.0 * r).
    pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (1.0, 1.0)]
    total = sum(math.dist(pts[i], pts[j]) for i, j in mst_edges(pts))
    assert total == pytest.approx(4 * math.sqrt(2), rel=1e-9)


def test_mst_ties_break_on_index_and_are_reproducible():
    """Equidistant candidates must resolve by input index, never by hash order.

    KRT's equivalent is documented as needing ``PYTHONHASHSEED=0`` (its #457);
    ours is pinned by construction, so repeated calls are bit-identical.
    """
    pts = [(0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)]
    first = mst_edges(pts)
    for _ in range(20):
        assert mst_edges(pts) == first
    # every neighbour is equidistant from the origin -> all attach to index 0
    assert all(src == 0 for src, _ in first)


# --------------------------------------------------------------------------- #
# net_airwires / count_crossings
# --------------------------------------------------------------------------- #
def test_single_pad_net_has_no_airwires():
    assert net_airwires([_pad("R1", "1", "N", 0, 0)], "N") == []


def test_airwire_length_is_euclidean():
    wires = net_airwires([_pad("R1", "1", "N", 0, 0),
                          _pad("R2", "1", "N", 3, 4)], "N")
    assert len(wires) == 1
    assert wires[0].length_mm == pytest.approx(5.0)


def test_same_net_airwires_never_count_as_crossing():
    a = Airwire("N", _pad("A", "1", "N", 0, 0), _pad("B", "1", "N", 10, 10))
    b = Airwire("N", _pad("C", "1", "N", 0, 10), _pad("D", "1", "N", 10, 0))
    assert count_crossings([a, b]) == 0


def test_different_net_crossing_counts_once():
    a = Airwire("N1", _pad("A", "1", "N1", 0, 0), _pad("B", "1", "N1", 10, 10))
    b = Airwire("N2", _pad("C", "1", "N2", 0, 10), _pad("D", "1", "N2", 10, 0))
    assert count_crossings([a, b]) == 1


def test_crossing_at_a_shared_pad_is_skipped():
    shared = _pad("A", "1", "N1", 0, 0)
    a = Airwire("N1", shared, _pad("B", "1", "N1", 10, 10))
    b = Airwire("N2", _pad("A", "1", "N2", 0, 0), _pad("D", "1", "N2", 10, 0))
    assert count_crossings([a, b]) == 0


# --------------------------------------------------------------------------- #
# is_plane_net -- reuses the package's own net vocabulary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["GND", "SGND", "PGND", "VSS", "AGND",
                                  "VIN", "VOUT", "VCC", "+3.3V", "+5V"])
def test_plane_nets_are_recognised(name):
    assert is_plane_net(name)


@pytest.mark.parametrize("name", ["BOOST", "SW", "VFB", "ISENSE", "SHDN_N",
                                  "CSS_MID", "TG", "SEPIC_MID"])
def test_signal_nets_are_not_plane_nets(name):
    assert not is_plane_net(name)


# --------------------------------------------------------------------------- #
# rotation -- KiCad's convention (the angle is negated)
# --------------------------------------------------------------------------- #
def test_rotate_90_follows_kicad_convention():
    pads = [_pad("C1", "1", "A", 1.0, 0.0)]
    (moved,) = _rotate(pads, 0.0, 0.0, 90.0)
    assert moved.x == pytest.approx(0.0, abs=1e-9)
    assert moved.y == pytest.approx(-1.0, abs=1e-9)


def test_rotate_180_mirrors_through_the_origin():
    pads = [_pad("C1", "1", "A", 2.0, 3.0)]
    (moved,) = _rotate(pads, 0.0, 0.0, 180.0)
    assert moved.x == pytest.approx(-2.0, abs=1e-9)
    assert moved.y == pytest.approx(-3.0, abs=1e-9)


def test_rotate_is_about_the_part_origin_not_the_board_origin():
    pads = [_pad("C1", "1", "A", 11.0, 10.0)]
    (moved,) = _rotate(pads, 10.0, 10.0, 180.0)
    assert moved.x == pytest.approx(9.0, abs=1e-9)
    assert moved.y == pytest.approx(10.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# twisted_pairs -- the metric this module exists for
# --------------------------------------------------------------------------- #
def _twist_fixture():
    """A 2-pad cap wired to a 2-pad part with its nets swapped end for end."""
    cap = [_pad("C1", "1", "VCC", 0.0, 1.0), _pad("C1", "2", "GND", 0.0, -1.0)]
    ic = [_pad("U1", "1", "GND", 10.0, 1.0), _pad("U1", "2", "VCC", 10.0, -1.0)]
    return {"C1": cap, "U1": ic}, {"C1": (0.0, 0.0), "U1": (10.0, 0.0)}


def test_twist_is_detected():
    pads, origins = _twist_fixture()
    twisted, examined = twisted_pairs(pads, origins)
    assert examined == 1
    assert len(twisted) == 1
    assert twisted[0].nets == ("GND", "VCC")
    assert twisted[0].crossings == 1


def test_a_reported_fix_actually_resolves_the_twist():
    """The contract is 'this rotation resolves the pair', not a fixed angle.

    Only the SMALLEST resolving rotation per part is reported, so a symmetric
    two-pad fixture legitimately reports 90 rather than 180.
    """
    pads, origins = _twist_fixture()
    twisted, _ = twisted_pairs(pads, origins)
    fixes = dict(twisted[0].fixes)
    assert "C1" in fixes
    delta = fixes["C1"]
    applied = dict(pads)
    applied["C1"] = _rotate(pads["C1"], *origins["C1"], delta)
    again, _ = twisted_pairs(applied, origins)
    assert again == []


def test_untwisting_two_swapped_wires_also_shortens_them():
    pads, origins = _twist_fixture()
    twisted, _ = twisted_pairs(pads, origins)
    assert twisted[0].shortens_mm > 0


def test_an_untwisted_pair_is_not_reported():
    pads, origins = _twist_fixture()
    pads["U1"] = [_pad("U1", "1", "VCC", 10.0, 1.0),
                  _pad("U1", "2", "GND", 10.0, -1.0)]
    twisted, examined = twisted_pairs(pads, origins)
    assert examined == 1
    assert twisted == []


def test_a_pair_sharing_one_net_is_not_examined():
    pads = {"C1": [_pad("C1", "1", "GND", 0.0, 0.0)],
            "U1": [_pad("U1", "1", "GND", 10.0, 0.0)]}
    twisted, examined = twisted_pairs(pads, {"C1": (0, 0), "U1": (10, 0)})
    assert (twisted, examined) == ([], 0)


def test_the_closest_pad_pair_is_the_representative():
    """An IC with two ground pins must connect via the NEAR one."""
    pads, origins = _twist_fixture()
    pads["U1"].append(_pad("U1", "3", "GND", 10.0, -50.0))
    twisted, _ = twisted_pairs(pads, origins)
    # The far GND pad must not become the representative and hide the twist.
    assert len(twisted) == 1


def test_rotation_testing_can_be_disabled():
    pads, origins = _twist_fixture()
    twisted, _ = twisted_pairs(pads, origins, test_rotations=False)
    assert twisted[0].fixes == ()
    assert twisted[0].shortens_mm == 0.0


# --------------------------------------------------------------------------- #
# Integration -- read a real placed board
# --------------------------------------------------------------------------- #
_CANARY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skidl-eda", "canaries", "phase16_out", "a_lt3724_buck",
    "lt3724_buck.kicad_pcb_power_copper", "placed.kicad_pcb")


def _krt_available():
    from skidl_layout.krt import find_krt
    return find_krt() is not None


needs_board = pytest.mark.skipif(
    not os.path.isfile(_CANARY) or not _krt_available(),
    reason="needs a built KiCadRoutingTools checkout and the phase-16 canary board")


@needs_board
def test_analyse_a_real_placed_board():
    rn = analyse_board(_CANARY)
    assert rn.part_count == 23
    assert rn.pad_count == 66
    assert rn.net_count == 16
    # one MST edge per net per (pads - 1)
    assert len(rn.airwires) == 50
    assert rn.length_mm == pytest.approx(516.86, abs=0.05)
    assert set(rn.plane_nets) == {"PGND", "SGND", "VCC", "VIN", "VOUT"}


@needs_board
def test_real_board_crossings_and_twists():
    rn = analyse_board(_CANARY)
    assert rn.crossings == 72
    assert rn.signal_crossings == 14
    assert len(rn.twisted) == 6
    # Every twisted pair on this board involves the controller or a 2-pad part,
    # and each one has at least one rotation that resolves it.
    assert all(t.fixes for t in rn.twisted)


@needs_board
def test_analysis_is_reproducible_across_calls():
    a, b = analyse_board(_CANARY), analyse_board(_CANARY)
    assert a.summary() == b.summary()


@needs_board
def test_summary_is_json_serialisable():
    import json
    json.dumps(analyse_board(_CANARY).summary())
