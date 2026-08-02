from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

from .geometry import FootprintGeometry
from .ratnest import is_plane_net
from .roles import is_nc_net
from .writer import PlacedPart


_MACOS_KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


@dataclass
class ValidationResult:
    overlaps: list[tuple[str, str]] = field(default_factory=list)
    outline_violations: list[str] = field(default_factory=list)
    keepout_violations: list[str] = field(default_factory=list)
    cutout_violations: list[str] = field(default_factory=list)
    worst_hpwl_nets: list[tuple[str, float]] = field(default_factory=list)
    worst_hpwl_refs: dict[str, list[str]] = field(default_factory=dict)
    missing_refs: list[str] = field(default_factory=list)
    extra_refs: list[str] = field(default_factory=list)
    total_parts: int = 0
    placed_parts: int = 0

    #: ⭐ Report-only plumbing for :attr:`gap_pairs` / :attr:`min_gap_mm`, set by
    #: :func:`validate`. ⛔ **Never read it directly** -- it is the index the two
    #: public properties walk, kept so the walk can be deferred.
    _gap_index: object | None = field(default=None, repr=False, compare=False)
    _gap_pairs_cache: list | None = field(default=None, repr=False, compare=False)

    #: ⭐ Report-only, added by the legality plan. ``gap_pairs`` is
    #: ``[(ref_a, ref_b, gap_mm), ...]`` for same-side pairs within
    #: :data:`GAP_REPORT_RADIUS_MM`, sorted by gap then refs so it is
    #: deterministic; ``min_gap_mm`` is the first row's gap (``None`` when there
    #: are < 2 placed parts, or when no pair is that close).
    #:
    #: ⛔⛔ **LAZY, and the laziness is a measurement, not a style choice.**
    #: :func:`validate` runs on every scored trial. Computed eagerly, the walk
    #: cost **18 % of a whole search's wall clock** (``lt3844_buck``
    #: 22.0 s -> 25.9 s, digest identical) -- an unconditional tax on every
    #: future search for a number no penalty consumes. The separation objective
    #: computes its own gaps in ``scoring``; these two are the *instrument* that
    #: measured whether it was worth building.
    #:
    #: ⛔⛔ **Neither enters :attr:`ok`, :meth:`summary`'s pass/fail lines, or
    #: any penalty**, and adding them changed no score. ``overlaps``' type is
    #: deliberately untouched -- it is consumed as a **pair list** by
    #: ``refinement.py`` (``if any(ref in pair for pair in validation.overlaps)``
    #: and ``for ref_a, ref_b in validation.overlaps``) and by :attr:`ok`.
    @property
    def gap_pairs(self) -> list[tuple[str, str, float]]:
        if self._gap_pairs_cache is None:
            index = self._gap_index
            self._gap_pairs_cache = (
                [] if index is None
                else _pair_gaps(list(index.placed_by_ref.values()), {},
                                index=index)
            )
        return self._gap_pairs_cache

    @property
    def min_gap_mm(self) -> float | None:
        pairs = self.gap_pairs
        return pairs[0][2] if pairs else None

    @property
    def ok(self) -> bool:
        return (
            not self.overlaps
            and not self.missing_refs
            and not self.outline_violations
            and not self.keepout_violations
            and not self.cutout_violations
        )

    def summary(self) -> str:
        lines = []
        lines.append(f"Parts: {self.placed_parts}/{self.total_parts} placed")
        if self.missing_refs:
            lines.append(f"MISSING: {', '.join(self.missing_refs[:20])}")
        if self.overlaps:
            lines.append(f"OVERLAPS ({len(self.overlaps)}):")
            for a, b in self.overlaps[:20]:
                lines.append(f"  {a} ↔ {b}")
        else:
            lines.append("No overlaps")
        if self.outline_violations:
            lines.append(f"OUTSIDE OUTLINE ({len(self.outline_violations)}):")
            for ref in self.outline_violations[:20]:
                lines.append(f"  {ref}")
        if self.keepout_violations:
            lines.append(f"INSIDE KEEPOUT ({len(self.keepout_violations)}):")
            for ref in self.keepout_violations[:20]:
                lines.append(f"  {ref}")
        if self.cutout_violations:
            lines.append(f"INTERSECTS CUTOUT ({len(self.cutout_violations)}):")
            for ref in self.cutout_violations[:20]:
                lines.append(f"  {ref}")
        if self.worst_hpwl_nets:
            lines.append("Worst HPWL nets:")
            for name, hpwl in self.worst_hpwl_nets[:10]:
                lines.append(f"  {name}: {hpwl:.1f}mm")
        return "\n".join(lines)


def _fallback_bounds(
    pp: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
) -> tuple[float, float, float, float]:
    w, h = fp_bboxes.get(pp.footprint, (2.0, 2.0))
    if pp.rot_deg % 180 == 90:
        w, h = h, w
    return pp.x_mm - w / 2, pp.y_mm - h / 2, pp.x_mm + w / 2, pp.y_mm + h / 2


def _placed_bounds(
    pp: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    *,
    physical: bool = False,
) -> tuple[float, float, float, float]:
    geometry = (fp_geometries or {}).get(pp.footprint)
    if geometry is not None:
        if physical:
            return geometry.transformed_physical_bounds(pp)
        return geometry.transformed_bounds(pp)
    return _fallback_bounds(pp, fp_bboxes)


def _pair_gap(a, b) -> float:
    """Signed AABB separation between two bounds tuples, in mm.

    Positive  = the boxes are apart by this much along the dominant axis.
    Zero      = touching.
    Negative  = they interpenetrate by this much.

    ⭐⭐ **EXACTLY the quantity** :func:`_rects_overlap` **thresholds**:
    ``_rects_overlap(a, b, c)`` is ``_pair_gap(a, b) < c`` for every ``a``,
    ``b``, ``c``. The algebra is one line per axis -- ``_rects_overlap``'s
    x-clause is ``ax_min < bx_max + c and ax_max > bx_min - c``, which is
    ``max(bx_min - ax_max, ax_min - bx_max) < c`` -- and taking the max over the
    two axes turns "both clauses hold" into "the separation is below ``c``".
    ⛔ That identity is **gated on real boards** (gate ``Y2``) rather than merely
    asserted here, because it is the entire justification for hanging a
    continuous separation objective off a primitive that has to keep agreeing
    with the shipped binary legality predicate.

    ⚠ Reads bounds tuples, not :class:`PlacedPart`s, so the caller decides
    whether they are ``physical`` (body u pads) or courtyard bounds. Every
    consumer in this codebase passes ``physical=True`` bounds, matching
    :func:`_check_overlaps`.
    """
    ax_min, ay_min, ax_max, ay_max = a
    bx_min, by_min, bx_max, by_max = b
    gap_x = max(bx_min - ax_max, ax_min - bx_max)
    gap_y = max(by_min - ay_max, ay_min - by_max)
    return max(gap_x, gap_y)


def _rects_overlap(a, b, clearance_mm: float = 0.0) -> bool:
    ax_min, ay_min, ax_max, ay_max = a
    bx_min, by_min, bx_max, by_max = b
    return (
        ax_min < bx_max + clearance_mm
        and ax_max > bx_min - clearance_mm
        and ay_min < by_max + clearance_mm
        and ay_max > by_min - clearance_mm
    )


def _assembly_side(pp: PlacedPart) -> str:
    side = str(getattr(pp, "side", "front") or "front").lower()
    if side not in {"front", "back", "mechanical"}:
        return "front"
    return side


def _same_physical_side(a: PlacedPart, b: PlacedPart) -> bool:
    a_side = _assembly_side(a)
    b_side = _assembly_side(b)
    if {a_side, b_side} == {"front", "back"}:
        return False
    return True


def _pad_collision_pairs(
    placed: list[PlacedPart],
    clearance_mm: float,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[tuple[str, str]]:
    if not fp_geometries:
        return []

    # Round-9 WS34: per-part precompute (index-parallel, NOT ref-keyed —
    # plan hazard #7). A pair can only collide when at least one part has
    # a through-board pad (_through_board_pads_collide requires
    # is_through_board on one side), so all-SMD pairs skip before any
    # per-pair work. tb by footprint name: geometry is per-footprint.
    tb_by_footprint: dict[str, bool] = {}
    geoms: list[FootprintGeometry | None] = []
    sides: list[str] = []
    tbs: list[bool] = []
    for pp in placed:
        g = fp_geometries.get(pp.footprint)
        if g is not None and not g.pads:
            g = None
        geoms.append(g)
        sides.append(_assembly_side(pp))
        if g is None:
            tbs.append(False)
            continue
        if pp.footprint not in tb_by_footprint:
            tb_by_footprint[pp.footprint] = any(
                pad.is_through_board for pad in g.pads
            )
        tbs.append(tb_by_footprint[pp.footprint])

    collisions: list[tuple[str, str]] = []
    for i, a in enumerate(placed):
        a_geometry = geoms[i]
        if a_geometry is None:
            continue
        a_side = sides[i]
        a_tb = tbs[i]
        for j in range(i + 1, len(placed)):
            b_geometry = geoms[j]
            if b_geometry is None:
                continue
            if not (a_tb or tbs[j]):
                continue
            if {a_side, sides[j]} != {"front", "back"}:
                continue  # same physical side -> pad check not applicable
            if _through_board_pads_collide(
                a, a_geometry, placed[j], b_geometry, clearance_mm
            ):
                collisions.append((a.ref, placed[j].ref))
    return collisions


def _through_board_pads_collide(
    a: PlacedPart,
    a_geometry: FootprintGeometry,
    b: PlacedPart,
    b_geometry: FootprintGeometry,
    clearance_mm: float,
) -> bool:
    for a_pad in a_geometry.pads:
        a_bounds = None
        for b_pad in b_geometry.pads:
            if not (a_pad.is_through_board or b_pad.is_through_board):
                continue
            if a_bounds is None:
                a_bounds = a_pad.transformed_bounds(a)
            if _rects_overlap(
                a_bounds, b_pad.transformed_bounds(b), clearance_mm
            ):
                return True
    return False


def _dedupe_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a, b in pairs:
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((a, b))
    return deduped


#: ⭐ Report-only. The radius within which :attr:`ValidationResult.gap_pairs`
#: lists a same-side pair, added by the legality plan.
#: ⛔ Deliberately finite rather than all-pairs: :func:`validate` runs on **every
#: scored trial**, and an unconditional O(n^2) walk in that hot path would be
#: paid by every search for numbers no penalty consumes. At 5 mm the tightest
#: pairs -- the only ones a separation objective can be charged for at any dose
#: this plan considers -- are all inside it.
GAP_REPORT_RADIUS_MM = 5.0


class _OverlapIndex:
    """Physical bounds (+ a spatial grid above 20 parts), computed once.

    ⛔⛔ **This exists because ``_check_overlaps`` has two code paths and the
    corpus and the fixtures exercise different ones** (every eval board is 20-36
    parts -> the grid path; every unit-test fixture is smaller -> the O(n^2)
    one). A gap computation bolted onto one path and not the other would pass
    every test and be wrong on every board. Both the legality check and the gap
    report now read **this one index**, so they cannot disagree about where a
    part is, and the index is built once per :func:`validate` rather than twice.
    """

    __slots__ = ("bounds_by_ref", "placed_by_ref", "grid")

    def __init__(self, placed, fp_bboxes, fp_geometries=None):
        self.bounds_by_ref: dict[str, tuple[float, float, float, float]] = {
            pp.ref: _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True)
            for pp in placed
        }
        self.placed_by_ref = {pp.ref: pp for pp in placed}
        self.grid = None
        if len(placed) >= 20:
            from .spatial import SpatialGrid

            grid = SpatialGrid(cell_size_mm=10.0)
            for pp in placed:
                b = self.bounds_by_ref[pp.ref]
                grid.insert(pp.ref, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2,
                            b[2] - b[0], b[3] - b[1])
            self.grid = grid

    def candidate_pairs(self, clearance: float) -> list[tuple[str, str]]:
        """Ref pairs that *may* be within ``clearance``; sorted, deduped.

        ⭐ The grid's proposal at a given clearance is **exactly** the set
        ``_pair_gap < clearance`` (its ``_overlaps`` is the centre-distance form
        of ``_rects_overlap`` over the same boxes), so the two paths agree by
        construction once the caller applies the same predicate -- which is what
        gate ``Y2`` asserts on real boards rather than assuming here.
        """
        if self.grid is not None:
            return self.grid.all_overlapping_pairs(clearance=clearance)
        refs = sorted(self.bounds_by_ref)
        return [(a, b) for i, a in enumerate(refs) for b in refs[i + 1:]]


def _check_overlaps(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    clearance_mm: float,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    index: "_OverlapIndex | None" = None,
) -> list[tuple[str, str]]:
    if index is None:
        index = _OverlapIndex(placed, fp_bboxes, fp_geometries)
    bounds_by_ref = index.bounds_by_ref
    placed_by_ref = index.placed_by_ref

    if index.grid is not None:
        body_overlaps = [
            (a_ref, b_ref)
            for a_ref, b_ref in index.candidate_pairs(clearance_mm)
            if _same_physical_side(placed_by_ref[a_ref], placed_by_ref[b_ref])
        ]
        return _dedupe_pairs(
            body_overlaps + _pad_collision_pairs(placed, clearance_mm, fp_geometries)
        )

    overlaps: list[tuple[str, str]] = []
    for i, a in enumerate(placed):
        a_bounds = bounds_by_ref[a.ref]
        for b in placed[i + 1:]:
            if not _same_physical_side(a, b):
                continue
            if _rects_overlap(a_bounds, bounds_by_ref[b.ref], clearance_mm):
                overlaps.append((a.ref, b.ref))
    return _dedupe_pairs(
        overlaps + _pad_collision_pairs(placed, clearance_mm, fp_geometries)
    )


def overlap_gaps(result: "ValidationResult") -> list[tuple[str, str, float | None]]:
    """``[(ref_a, ref_b, gap_mm | None), ...]`` for the pairs in ``overlaps``.

    ⭐ The accessor the depth-graded overlap objective needs, and deliberately
    the *only* new thing it needs: it reads the ``_OverlapIndex``
    :func:`validate` **already built** for the legality check, so a depth term
    costs one dictionary lookup and one ``_pair_gap`` per *overlapping* pair --
    never a walk over all pairs.
    ⛔⛔ **It must not touch :attr:`ValidationResult.gap_pairs`.** That property
    is lazy because populating it inside ``validate`` -- which runs on every
    scored trial -- measured **+18 % on a whole search's wall clock**.

    ⛔⛔ **``gap_mm`` is ``None`` when the pair has no AABB gap to report**, and
    that is a real case rather than an error path: ``_check_overlaps`` unions
    the same-side body test with :func:`_pad_collision_pairs`, which is a
    *through-board pad vs pad* test between parts on **opposite** sides of the
    board. A signed AABB separation between two parts on different sides is not
    a depth at all. ⭐ A consumer must charge those pairs the **full** binary
    cost; anything else would make a real pad collision cheaper than it is
    today. ``None`` is also returned when the index is absent (a hand-built
    ``ValidationResult``), for the same reason and with the same consequence.

    ⛔ Report-only in itself: nothing here enters :attr:`ok`, ``summary()`` or
    any penalty. The objective that consumes it lives in ``scoring``.
    """
    index = result._gap_index
    if index is None:
        return [(a, b, None) for a, b in result.overlaps]
    bounds = index.bounds_by_ref
    placed_by_ref = index.placed_by_ref
    rows: list[tuple[str, str, float | None]] = []
    for ref_a, ref_b in result.overlaps:
        a = placed_by_ref.get(ref_a)
        b = placed_by_ref.get(ref_b)
        if (a is None or b is None or ref_a not in bounds or ref_b not in bounds
                or not _same_physical_side(a, b)):
            rows.append((ref_a, ref_b, None))
            continue
        rows.append((ref_a, ref_b, _pair_gap(bounds[ref_a], bounds[ref_b])))
    return rows


def _pair_gaps(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    *,
    radius_mm: float = GAP_REPORT_RADIUS_MM,
    index: "_OverlapIndex | None" = None,
) -> list[tuple[str, str, float]]:
    """``[(ref_a, ref_b, gap_mm), ...]`` for same-side pairs within ``radius_mm``.

    Sorted by ``(gap, ref_a, ref_b)``, so the list is a total order over content
    and never over arrival order -- the same rule ``cells_place.canonical_order``
    and ``resolve_hpwl_points``' tie-break already follow.

    ⛔ **Report-only.** Nothing here enters :attr:`ValidationResult.ok`, the
    ``summary()`` pass/fail lines, or any penalty. The separation *objective*
    lives in ``scoring``; this is the instrument that measured whether it was
    worth building.
    """
    if len(placed) < 2:
        return []
    if index is None:
        index = _OverlapIndex(placed, fp_bboxes, fp_geometries)
    bounds_by_ref = index.bounds_by_ref
    placed_by_ref = index.placed_by_ref

    rows: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()
    for a_ref, b_ref in index.candidate_pairs(radius_mm):
        key = (a_ref, b_ref) if a_ref <= b_ref else (b_ref, a_ref)
        if key in seen:
            continue
        seen.add(key)
        a = placed_by_ref.get(key[0])
        b = placed_by_ref.get(key[1])
        if a is None or b is None or not _same_physical_side(a, b):
            continue
        gap = _pair_gap(bounds_by_ref[key[0]], bounds_by_ref[key[1]])
        if gap < radius_mm:
            rows.append((key[0], key[1], gap))
    rows.sort(key=lambda row: (row[2], row[0], row[1]))
    return rows


def _point_in_polygon(x: float, y: float, vertices: list[tuple[float, float]]) -> bool:
    inside = False
    count = len(vertices)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = vertices[i]
        xj, yj = vertices[j]
        intersects = ((yi > y) != (yj > y)) and (
            x <= (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _outline_contains_bounds(bounds, outline) -> bool:
    x_min, y_min, x_max, y_max = bounds
    if (
        x_min < outline.x_min
        or y_min < outline.y_min
        or x_max > outline.x_max
        or y_max > outline.y_max
    ):
        return False
    vertices = getattr(outline, "vertices", []) or []
    if len(vertices) <= 4:
        return True
    shapely_result = _shapely_outline_contains_bounds(bounds, vertices)
    if shapely_result is not None:
        return shapely_result
    corners = [
        (x_min, y_min),
        (x_max, y_min),
        (x_max, y_max),
        (x_min, y_max),
    ]
    return all(_point_in_polygon(x, y, vertices) for x, y in corners)


def _shapely_outline_contains_bounds(
    bounds,
    vertices: list[tuple[float, float]],
) -> bool | None:
    try:
        from shapely.geometry import Polygon, box
    except Exception:
        return None
    try:
        polygon = Polygon(vertices)
        if polygon.is_empty or not polygon.is_valid:
            return None
        return bool(polygon.covers(box(*bounds)))
    except Exception:
        return None


def _check_outline_violations(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[str]:
    if outline is None:
        return []

    violations = []
    for pp in placed:
        if not _outline_contains_bounds(
            _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True), outline
        ):
            violations.append(pp.ref)
    return violations


def _check_keepout_violations(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    keepouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[str]:
    if not keepouts:
        return []
    violations = []
    keepout_bounds = [
        (
            keepout.x_min,
            keepout.y_min,
            keepout.x_max,
            keepout.y_max,
            set(getattr(keepout, "allowed_refs", []) or []),
        )
        for keepout in keepouts
    ]
    for pp in placed:
        bounds = _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True)
        if any(
            pp.ref not in allowed_refs
            and _rects_overlap(bounds, (x_min, y_min, x_max, y_max))
            for x_min, y_min, x_max, y_max, allowed_refs in keepout_bounds
        ):
            violations.append(pp.ref)
    return violations


def _check_cutout_violations(
    placed: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[str]:
    if not cutouts:
        return []
    violations = []
    cutout_bounds = [
        getattr(cutout, "bounds", None)
        or (
            getattr(cutout, "x_min"),
            getattr(cutout, "y_min"),
            getattr(cutout, "x_max"),
            getattr(cutout, "y_max"),
        )
        for cutout in cutouts
    ]
    for pp in placed:
        bounds = _placed_bounds(pp, fp_bboxes, fp_geometries, physical=True)
        if any(_rects_overlap(bounds, cutout) for cutout in cutout_bounds):
            violations.append(pp.ref)
    return violations


def _compute_hpwl(
    placed: list[PlacedPart],
    circuit,
    net_pins: list[tuple[str, list[str]]] | None = None,
    *,
    hpwl_objective: str = "centroid",
    hpwl_nets: str = "all",
    pad_points=None,
) -> list[tuple[str, float, list[str]]]:
    """The ten worst nets by half-perimeter, for the report.

    ⭐ ``hpwl_objective="pads"`` measures each net's box over **pad positions**
    instead of part centres, so the report and the objective say the same thing
    -- the ``E0`` lesson from the MST promotion, where the whole justification
    for the swap was that the objective and the judge became one number.
    ⭐ ``hpwl_nets="plane_free"`` drops the poured nets for the same reason, the
    third application of the same lesson: a "worst nets" report whose top four
    rows are planes the objective is no longer optimising would be actively
    misleading about what the placer was trying to do.
    ⚠ It stays a *pin-level* walk (duplicates kept, a two-pin-one-part net still
    listed at 0.0 in centroid mode): that shape is asserted by
    ``tests/test_layout_validate_cache.py`` and is not this plan's variable.

    ⛔ **``hpwl_weights`` deliberately does NOT reach here, and the asymmetry
    with the two knobs above is intentional -- read it as a statement, not an
    omission.** This function ranks the ten worst nets by *unweighted* HPWL;
    ``scoring._net_weight`` has never entered it, so a plane-net dose has
    nothing to change. The two knobs above were threaded because they change
    *which points* and *which nets* the report measures, which is exactly what a
    report must agree with the objective about.
    """
    pos_by_ref = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed}
    #: ``{net: {ref: [(x, y), ...]}}`` -- every pad of every placed part, so a
    #: net's box can be taken over pads while the *membership* rule below stays
    #: exactly the pin-level one.
    pads_by_net_ref: dict[str, dict[str, list[tuple[float, float]]]] = {}
    if hpwl_objective == "pads" and pad_points:
        for point in pad_points:
            pads_by_net_ref.setdefault(point.net, {}).setdefault(
                point.ref, []).append((point.x, point.y))
    # Round-9 WS33: net_pins is the ctx-cached connectivity walk
    # (LayoutContext.hpwl_net_pins). None -> build the identical pairs
    # live (single source of truth for the loop below; plan hazard #3).
    if net_pins is None:
        net_pins = []
        for net in circuit.get_nets():
            if is_nc_net(net):
                continue
            pin_refs = [
                ref
                for pin in net.get_pins()
                if (ref := getattr(getattr(pin, "part", None), "ref", None))
            ]
            net_pins.append((net.name, pin_refs))

    plane_free = hpwl_nets == "plane_free"
    net_hpwl: list[tuple[str, float]] = []
    for name, pin_refs in net_pins:
        if plane_free and is_plane_net(str(name)):
            continue
        xs, ys = [], []
        refs = []
        net_pads = pads_by_net_ref.get(name, {})
        for ref in pin_refs:
            if ref in pos_by_ref:
                points = net_pads.get(ref)
                if points:
                    xs.extend(p[0] for p in points)
                    ys.extend(p[1] for p in points)
                else:
                    x, y = pos_by_ref[ref]
                    xs.append(x)
                    ys.append(y)
                if ref not in refs:
                    refs.append(ref)
        if len(xs) < 2:
            continue
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        net_hpwl.append((name, hpwl, refs))

    net_hpwl.sort(key=lambda t: t[1], reverse=True)
    return net_hpwl[:10]


def validate(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    clearance_mm: float = 0.5,
    outline=None,
    keepouts=None,
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    ctx=None,
    *,
    hpwl_objective: str = "centroid",
    hpwl_nets: str = "all",
    pad_points=None,
) -> ValidationResult:
    result = ValidationResult(placed_parts=len(placed_parts))

    # ⭐ ONE index feeds the legality check and the report-only gap walk, so the
    # two cannot disagree about where a part is and the bounds are transformed
    # once rather than twice (§5.3 -- ``_check_overlaps``' two code paths).
    index = _OverlapIndex(placed_parts, fp_bboxes, fp_geometries)
    result.overlaps = _check_overlaps(
        placed_parts, fp_bboxes, clearance_mm, fp_geometries, index=index
    )
    # ⛔ The index is HANDED OVER, not walked -- see ``gap_pairs``' docstring for
    # the 18 %-of-a-search measurement that made the walk lazy.
    result._gap_index = index
    result.outline_violations = _check_outline_violations(
        placed_parts, fp_bboxes, outline, fp_geometries
    )
    result.keepout_violations = _check_keepout_violations(
        placed_parts, fp_bboxes, keepouts, fp_geometries
    )
    result.cutout_violations = _check_cutout_violations(
        placed_parts, fp_bboxes, cutouts, fp_geometries
    )

    if circuit is not None:
        result.total_parts = len(circuit.parts)
        circuit_refs = {getattr(p, "ref", None) for p in circuit.parts}
        placed_refs = {pp.ref for pp in placed_parts}
        result.missing_refs = sorted(circuit_refs - placed_refs - {None})
        result.extra_refs = sorted(placed_refs - circuit_refs)
        memo = getattr(ctx, "hpwl_net_pins", None) if ctx is not None else None
        worst_hpwl = _compute_hpwl(placed_parts, circuit, net_pins=memo,
                                   hpwl_objective=hpwl_objective,
                                   hpwl_nets=hpwl_nets,
                                   pad_points=pad_points)
        result.worst_hpwl_nets = [(name, hpwl) for name, hpwl, _ in worst_hpwl]
        result.worst_hpwl_refs = {name: refs for name, _, refs in worst_hpwl}

    return result


def find_kicad_cli() -> str | None:
    return shutil.which("kicad-cli") or (
        _MACOS_KICAD_CLI if os.path.isfile(_MACOS_KICAD_CLI) else None
    )


def run_kicad_drc(pcb_path: str) -> tuple[bool, str]:
    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return True, "kicad-cli not available"

    try:
        result = subprocess.run(
            [kicad_cli, "pcb", "drc", "--output", pcb_path + ".drc.json", pcb_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        report = result.stdout + result.stderr
        passed = result.returncode == 0
        return passed, report
    except FileNotFoundError:
        return True, "kicad-cli not available"
    except subprocess.TimeoutExpired:
        return False, "DRC timed out after 60s"
