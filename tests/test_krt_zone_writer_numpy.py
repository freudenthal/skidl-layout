"""Phase-8 WS-A: KRT's zone writer must survive **numpy** coordinates.

This is a regression test for a divergence skidl-layout deliberately carries in
the KiCadRoutingTools fork, and it exists so a future KRT re-sync cannot silently
revert it.

**The defect.** ``kicad_writer._segments_properly_cross``'s nested ``orient``
ended in::

    return (v > 1e-12) - (v < -1e-12)      # bool - bool

``bool - bool`` is the intended sign trick and is fine for Python floats, but
numpy >= 1.13 refuses it for ``np.bool_`` ("numpy boolean subtract, the ``-``
operator, is not supported"). The multi-net pour's Voronoi partition hands
``orient`` numpy scalars, so the call chain

    route_planes.create_plane
      -> _generate_multinet_layer_zones -> zone_overlap_priorities
      -> _polygons_overlap -> _segments_properly_cross -> orient

raised ``TypeError`` and ``route_planes.py`` exited 1 producing **no board at
all**. It is reached *only* when two different-net zones share a layer, so a
single-net pour steps around it -- which is why Phase 4's multi-net pour worked
on one board (the boost) and killed ``ltc1624_buck`` outright. Geometry-
dependent, not board-dependent: the same policy poured that very buck on its
first, smaller outline, so it was latent everywhere.

The fix is ``int()`` on both sides -- behaviour-identical for floats, the
smallest possible divergence. This test is skipped when no KRT checkout is
discoverable, exactly like every other KRT-backed test here.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from skidl_layout.krt import find_krt

numpy = pytest.importorskip("numpy")


def _kicad_writer():
    krt = find_krt()
    if krt is None:
        pytest.skip("no KiCadRoutingTools checkout discoverable")
    path = os.path.join(krt, "kicad_writer.py")
    if not os.path.isfile(path):
        pytest.skip("KRT checkout has no kicad_writer.py")
    # kicad_writer imports its sibling `kicad_parser`, which only resolves with
    # the checkout root on sys.path. Prepend rather than append: KRT owns these
    # module names and a same-named module elsewhere would load the wrong file.
    if krt not in sys.path:
        sys.path.insert(0, krt)
    spec = importlib.util.spec_from_file_location("_krt_kicad_writer", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # a KRT checkout missing its own deps
        pytest.skip(f"KRT kicad_writer is not importable here: {exc}")
    return module


#: ``(p1, p2, p3, p4, cross?)`` -- one transversal crossing and one clear miss.
#: Both are checked in *both* coordinate flavours, so the test pins the answer
#: and not merely the absence of an exception.
_CASES = [
    (((0.0, 0.0), (2.0, 2.0), (0.0, 2.0), (2.0, 0.0)), True),
    (((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)), False),
]


@pytest.mark.parametrize("points,expected", _CASES)
def test_segments_properly_cross_accepts_numpy_coordinates(points, expected):
    """The real defect: numpy coordinates used to raise TypeError here."""
    writer = _kicad_writer()
    arrays = [numpy.array(p, dtype=float) for p in points]

    got = writer._segments_properly_cross(*arrays)

    assert isinstance(got, bool)
    assert got is expected


@pytest.mark.parametrize("points,expected", _CASES)
def test_segments_properly_cross_unchanged_for_python_floats(points, expected):
    """...and the fix may not change the float answer it already gave."""
    writer = _kicad_writer()

    assert writer._segments_properly_cross(*points) is expected


def _poly(points):
    """A polygon in the shape ``zone_overlap_priorities`` actually passes down:
    a **list** of point objects, each of which here is a numpy array. (Not a 2-D
    numpy array -- ``_polygons_overlap`` opens with ``if not pa``, which is
    ambiguous for a multi-element array, and the pour never hands it one.)"""
    return [numpy.array(p, dtype=float) for p in points]


def test_polygons_overlap_accepts_numpy_points():
    """One rung up -- the frame the multi-net pour actually crashed in."""
    writer = _kicad_writer()
    square = _poly([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)])
    diamond = _poly([(2.0, -1.0), (5.0, 2.0), (2.0, 5.0), (-1.0, 2.0)])
    far = _poly([(20.0, 20.0), (24.0, 20.0), (24.0, 24.0), (20.0, 24.0)])

    # ⚠ Truthiness, not identity: with numpy points this returns ``np.True_``
    # rather than ``True``, because the trailing ``>= min_area`` compares numpy
    # scalars. That is harmless -- every caller uses it in a boolean context --
    # and pinning it to ``is True`` would fail for a reason that is not the bug.
    assert bool(writer._polygons_overlap(square, diamond)) is True
    assert bool(writer._polygons_overlap(square, far)) is False


def test_zone_overlap_priorities_with_numpy_geometry():
    """The exact entry point ``route_planes.create_plane`` calls: two zones of
    DIFFERENT nets sharing a layer, which is the only way this code is reached
    and precisely why a single-net pour stepped around the defect."""
    writer = _kicad_writer()
    zones = [
        ("F.Cu", 1, _poly([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)])),
        ("F.Cu", 2, _poly([(2.0, -1.0), (5.0, 2.0), (2.0, 5.0), (-1.0, 2.0)])),
    ]

    priorities = writer.zone_overlap_priorities(zones)

    assert len(priorities) == 2
    assert all(isinstance(p, int) for p in priorities)
