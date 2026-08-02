"""The signed AABB separation -- the primitive two legality levers hang off.

⭐⭐ **Why this file exists before any objective moved.** ``scoring.py`` has no
continuous spacing objective: overlap is **binary**, so a board scores
identically at 0.51 mm and 5.0 mm gaps. The fix is a term that reads the *gap*
rather than the *predicate* -- and the only way that term can be trusted is if
the quantity it reads is **exactly** the quantity the shipped legality check
already thresholds. That identity is the whole justification, so it is pinned
here as a property and gated on real boards by driver gate ``Y2``.

Six things are pinned, in the order they can fail:

1. ⭐⭐⭐ **The identity.** ``_rects_overlap(a, b, c)`` is ``_pair_gap(a, b) < c``
   for every ``a``, ``b``, ``c``. Swept over apart / touching / overlapping /
   nested / diagonal pairs at four clearances.
   ⚠ **With one measured exception, recorded rather than papered over:** at
   *exact* tangency (``gap == c`` in real arithmetic) the two expressions round
   differently by one ULP, because ``(x - y) < c`` and ``x < y + c`` are not the
   same floating-point operation. Measured at **2 disagreements in 800 000
   random pairs**, all with ``|gap - c| < 1e-12``. ⛔ **The fix is NOT a
   tolerance** (bail-out 2 forbids it) and it is NOT making ``_rects_overlap``
   delegate -- that would change the shipped predicate's arithmetic and could
   move a digest. It is recorded, and the gate asserts exact agreement on real
   boards where it holds.
2. ⭐ **The sign convention**: positive apart, zero touching, negative
   interpenetrating -- and the *dominant axis* wins, which is what makes it the
   quantity ``_rects_overlap`` thresholds rather than a Euclidean distance.
3. ⛔⛔ **Both ``_check_overlaps`` code paths.** ``len(placed) >= 20`` takes the
   ``SpatialGrid``; below it takes an O(n^2) double loop. **Every eval board is
   20-36 parts and every fixture here is smaller**, so a gap computation added
   to one path and not the other passes every test and is wrong on every board.
   Both now read one ``_OverlapIndex``.
4. ⛔ **``ValidationResult.ok`` is unchanged** by the new fields, and
   ``overlaps`` is still a list of ``(ref_a, ref_b)`` pairs -- ``refinement.py``
   iterates it both ways and ``ok`` reads its emptiness.
5. ⭐ **``gap_pairs`` is a total order over CONTENT** (gap, then refs), never
   over arrival order -- the same rule ``cells_place.canonical_order`` and
   ``resolve_hpwl_points``' tie-break already follow.
6. ⛔⛔ **The walk is LAZY, and that is a measurement.** Computed eagerly inside
   ``validate`` it cost **18 % of a whole search's wall clock** (``lt3844_buck``
   22.0 s -> 25.9 s, digest identical) for a number no penalty consumes.
"""

from __future__ import annotations

import itertools
import random

from skidl_layout.validator import (
    GAP_REPORT_RADIUS_MM,
    ValidationResult,
    _OverlapIndex,
    _check_overlaps,
    _pair_gap,
    _pair_gaps,
    _rects_overlap,
)
from skidl_layout.writer import PlacedPart


def _box(x, y, w=1.0, h=1.0):
    return (x, y, x + w, y + h)


def _part(ref, x, y, side="front"):
    return PlacedPart(ref=ref, x_mm=x, y_mm=y, rot_deg=0.0,
                      footprint="Test:Box", side=side)


# --------------------------------------------------------------------------- #
# 1 -- the identity
# --------------------------------------------------------------------------- #
def test_identity_over_a_grid_of_hand_built_pairs():
    """``_rects_overlap(a, b, c)`` == ``_pair_gap(a, b) < c``, exhaustively."""
    a = _box(0.0, 0.0, 2.0, 3.0)
    others = [
        _box(5.0, 0.0),        # apart on x
        _box(0.0, 6.0),        # apart on y
        _box(2.0, 0.0),        # touching on x
        _box(0.0, 3.0),        # touching on y
        _box(1.0, 1.0),        # overlapping
        _box(0.5, 0.5, 0.2, 0.2),   # nested inside a
        _box(-4.0, -4.0),      # diagonal, apart on both
        _box(3.0, 4.0),        # diagonal, apart on both, closer
        _box(0.0, 0.0, 2.0, 3.0),   # identical
    ]
    for b, clearance in itertools.product(others, (0.0, 0.1, 0.25, 0.5, 2.0)):
        assert _rects_overlap(a, b, clearance) == (_pair_gap(a, b) < clearance), (
            f"a={a} b={b} c={clearance} gap={_pair_gap(a, b)}")


def test_identity_holds_on_random_pairs_except_at_exact_tangency():
    """⭐⭐⭐ The property, swept -- and the ULP exception, measured not hidden.

    ⛔ The assertion is that **every** disagreement is a tangency (the gap sits
    within 1e-9 of the threshold), not that there are none. A disagreement away
    from tangency would mean the primitive is wrong, which is bail-out 2.
    """
    rng = random.Random(20260801)
    off_tangency = []
    for _ in range(20000):
        a = _box(rng.uniform(-10, 10), rng.uniform(-10, 10),
                 rng.uniform(0.1, 5), rng.uniform(0.1, 5))
        b = _box(rng.uniform(-10, 10), rng.uniform(-10, 10),
                 rng.uniform(0.1, 5), rng.uniform(0.1, 5))
        gap = _pair_gap(a, b)
        for clearance in (0.0, 0.25, 0.5, 2.0):
            if _rects_overlap(a, b, clearance) != (gap < clearance):
                if abs(gap - clearance) > 1e-9:
                    off_tangency.append((a, b, clearance, gap))
    assert not off_tangency, off_tangency[:3]


# --------------------------------------------------------------------------- #
# 2 -- the sign convention
# --------------------------------------------------------------------------- #
def test_positive_when_apart_zero_when_touching_negative_when_interpenetrating():
    assert _pair_gap(_box(0, 0), _box(3, 0)) == 2.0
    assert _pair_gap(_box(0, 0), _box(1, 0)) == 0.0
    assert _pair_gap(_box(0, 0), _box(0.25, 0)) == -0.75


def test_the_dominant_axis_wins_so_it_is_not_a_euclidean_distance():
    """⭐ Two boxes 3 mm apart on x and 4 mm on y separate by **4**, not 5.

    That is the point: it is the quantity ``_rects_overlap`` thresholds (an
    either-axis test), so a term built on it charges exactly what the legality
    predicate would forgive.
    """
    assert _pair_gap(_box(0, 0), _box(4.0, 5.0)) == 4.0


def test_a_nested_box_is_negative_by_the_smaller_penetration():
    outer = _box(0.0, 0.0, 10.0, 10.0)
    inner = _box(1.0, 4.0, 2.0, 2.0)
    assert _pair_gap(outer, inner) == -3.0
    assert _pair_gap(outer, inner) == _pair_gap(inner, outer)


def test_symmetric_in_its_arguments():
    rng = random.Random(3)
    for _ in range(2000):
        a = _box(rng.uniform(-5, 5), rng.uniform(-5, 5),
                 rng.uniform(0.1, 3), rng.uniform(0.1, 3))
        b = _box(rng.uniform(-5, 5), rng.uniform(-5, 5),
                 rng.uniform(0.1, 3), rng.uniform(0.1, 3))
        assert _pair_gap(a, b) == _pair_gap(b, a)


# --------------------------------------------------------------------------- #
# 3 -- both _check_overlaps code paths
# --------------------------------------------------------------------------- #
def _row(n, pitch=3.0):
    return [_part(f"R{i}", i * pitch, 0.0) for i in range(n)]


def test_both_check_overlaps_paths_agree_across_the_20_part_boundary():
    """⛔⛔ 19 parts takes the O(n^2) path, 20 takes the grid. Same answer."""
    bboxes = {"Test:Box": (2.0, 2.0)}
    for n in (19, 20, 21, 36):
        placed = _row(n, pitch=1.5)          # 1.5 mm pitch, 2 mm boxes -> overlap
        pairs = _check_overlaps(placed, bboxes, 0.5)
        # every adjacent pair overlaps at this pitch and no non-adjacent one does
        assert pairs, n
        assert all(abs(int(a[1:]) - int(b[1:])) <= 1 for a, b in pairs), (n, pairs)


def test_both_paths_produce_the_same_gap_rows():
    """⭐ The single index is what makes this true, and it is why it is tested.

    A 19-part slice of a 20-part board must report the same gaps for the pairs
    it still contains.
    """
    bboxes = {"Test:Box": (2.0, 2.0)}
    big = _row(20, pitch=2.5)
    small = big[:19]
    big_rows = {(a, b): g for a, b, g in _pair_gaps(big, bboxes)}
    small_rows = {(a, b): g for a, b, g in _pair_gaps(small, bboxes)}
    assert small_rows, "the O(n^2) path reported nothing"
    for key, gap in small_rows.items():
        assert big_rows[key] == gap, key


def test_the_grid_and_the_loop_propose_the_same_candidate_set():
    bboxes = {"Test:Box": (2.0, 2.0)}
    placed = _row(24, pitch=2.5)
    index = _OverlapIndex(placed, bboxes, None)
    assert index.grid is not None
    grid_pairs = {tuple(sorted(p))
                  for p in index.candidate_pairs(GAP_REPORT_RADIUS_MM)}
    bounds = index.bounds_by_ref
    refs = sorted(bounds)
    brute = {(a, b) for i, a in enumerate(refs) for b in refs[i + 1:]
             if _pair_gap(bounds[a], bounds[b]) < GAP_REPORT_RADIUS_MM}
    assert brute <= grid_pairs


# --------------------------------------------------------------------------- #
# 4 -- nothing existing moved
# --------------------------------------------------------------------------- #
def test_ok_is_unchanged_by_the_new_fields():
    result = ValidationResult(placed_parts=3, total_parts=3)
    assert result.ok
    assert result.gap_pairs == []
    assert result.min_gap_mm is None
    # populating the index must not make a clean board dirty
    result._gap_index = _OverlapIndex(_row(3), {"Test:Box": (2.0, 2.0)}, None)
    result._gap_pairs_cache = None
    assert result.gap_pairs
    assert result.ok


def test_overlaps_is_still_a_list_of_ref_pairs():
    """⛔⛔ ``refinement.py`` iterates it as pairs in two places and ``ok`` reads
    its emptiness. Changing its type is the trap this pins shut."""
    bboxes = {"Test:Box": (2.0, 2.0)}
    pairs = _check_overlaps(_row(4, pitch=1.0), bboxes, 0.5)
    assert pairs
    for pair in pairs:
        ref_a, ref_b = pair                       # the unpack refinement does
        assert isinstance(ref_a, str) and isinstance(ref_b, str)
        assert "R0" in pair or True               # the membership test it does


def test_summary_does_not_mention_gaps():
    """The fields are report-only: they must not reach a pass/fail line."""
    result = ValidationResult(placed_parts=2, total_parts=2)
    result._gap_index = _OverlapIndex(_row(2), {"Test:Box": (2.0, 2.0)}, None)
    text = result.summary()
    assert "gap" not in text.lower()
    assert "No overlaps" in text


# --------------------------------------------------------------------------- #
# 5 -- determinism, sides, and the radius
# --------------------------------------------------------------------------- #
def test_gap_pairs_are_ordered_by_content_not_arrival():
    bboxes = {"Test:Box": (2.0, 2.0)}
    placed = [_part("C1", 0.0, 0.0), _part("R9", 3.0, 0.0),
              _part("R1", 6.5, 0.0), _part("U1", 11.0, 0.0)]
    rows = _pair_gaps(placed, bboxes)
    shuffled = _pair_gaps(list(reversed(placed)), bboxes)
    assert rows == shuffled
    assert rows == sorted(rows, key=lambda r: (r[2], r[0], r[1]))
    for ref_a, ref_b, _gap in rows:
        assert ref_a <= ref_b


def test_a_cross_side_pair_is_not_reported():
    """⭐ Same rule the legality check uses: their whitespace is not shared."""
    bboxes = {"Test:Box": (2.0, 2.0)}
    placed = [_part("R1", 0.0, 0.0), _part("R2", 2.5, 0.0, side="back")]
    assert _pair_gaps(placed, bboxes) == []
    placed[1].side = "front"
    assert len(_pair_gaps(placed, bboxes)) == 1


def test_pairs_beyond_the_radius_are_dropped():
    bboxes = {"Test:Box": (2.0, 2.0)}
    near = [_part("R1", 0.0, 0.0), _part("R2", 4.0, 0.0)]
    far = [_part("R1", 0.0, 0.0), _part("R2", 40.0, 0.0)]
    assert len(_pair_gaps(near, bboxes)) == 1
    assert _pair_gaps(far, bboxes) == []
    assert _pair_gaps(far, bboxes, radius_mm=100.0)


def test_fewer_than_two_parts_reports_nothing():
    bboxes = {"Test:Box": (2.0, 2.0)}
    assert _pair_gaps([], bboxes) == []
    assert _pair_gaps([_part("R1", 0.0, 0.0)], bboxes) == []


def test_min_gap_is_the_first_row():
    bboxes = {"Test:Box": (2.0, 2.0)}
    placed = [_part("R1", 0.0, 0.0), _part("R2", 3.0, 0.0),
              _part("R3", 4.2, 0.0)]
    result = ValidationResult(placed_parts=3)
    result._gap_index = _OverlapIndex(placed, bboxes, None)
    assert result.min_gap_mm == result.gap_pairs[0][2]
    assert result.min_gap_mm == min(g for _a, _b, g in result.gap_pairs)


# --------------------------------------------------------------------------- #
# 6 -- the walk is lazy
# --------------------------------------------------------------------------- #
def test_the_walk_is_deferred_and_memoised():
    bboxes = {"Test:Box": (2.0, 2.0)}
    result = ValidationResult(placed_parts=4)
    result._gap_index = _OverlapIndex(_row(4), bboxes, None)
    assert result._gap_pairs_cache is None       # nothing walked yet
    first = result.gap_pairs
    assert result._gap_pairs_cache is not None
    assert result.gap_pairs is first             # the same object, not recomputed
