# -*- coding: utf-8 -*-
"""Real polygon pour zones -- board *sections*, not Voronoi cells (Phase 15).

Every pour this arc has emitted so far came out of KRT's ``route_planes.py``,
which partitions the board into a **Voronoi cell per plane net** and pours each
cell. That is a reasonable default and it is not a choice anybody made: nothing
in the stack could express "pour this net over *these* parts".

⭐ **The blocker was never KRT.** ``kicad_writer`` is a pure-Python import that
needs no Rust core, and ``generate_zone_sexpr`` takes an **arbitrary** polygon,
thermal relief and a fill priority. The Voronoi partition is the behaviour of one
CLI script. This module computes the polygons itself and splices the zones into
the emitted board -- rungs 2 and 3 of the KRT escalation ladder, with divergence
left at zero.

Two things come out of that:

* **sections** -- a region encloses a named subset of parts (the power section,
  the analogue section) and pours one net inside it, at a chosen priority and
  with a chosen pad-connection style;
* **the escape carve** -- a rule area that keeps the *pour* off a fine-pitch
  controller's escape annulus, so an escape via has bare laminate to land on.

⛔⛔ **The carve is NOT the refuted keepout.** ``route.py --keepout`` blocks
**tracks**, on every layer, for every net, and it has been measured into the
ground three times: Phase 13 single-pass **-32 nets**, Phase 14 loop-first
**-22 nets**, Phase 14 escape-first **±0 with 8 DRC violations**. What this
module writes is ``kicad_writer.generate_keepout_zone_sexpr``, a rule area whose
``keepout`` block says ``(tracks allowed) (vias allowed) (pads allowed)
(copperpour not_allowed)``. Tracks and vias stay legal; only the *fill* is
excluded. That asymmetry is exactly what an escape needs, and none of the three
negatives above bears on it.

⚠⚠ **The shape rule, stated once.** A region's polygon is the **convex hull of
every member's courtyard rectangle expanded by ``margin_mm``**. The alternatives
were considered and rejected: a bounding box merges disjoint clusters into a slab
that swallows the neighbouring section, and a concave/alpha shape tracks the
parts closely but flips its outline on small placement moves -- and this arc has
already been bitten by a region metric that moved ±4 mm² between identical runs.
A convex hull is stable, cheap, and always encloses every member with its margin.
**Its weakness is real and is reported, never silent**: two interleaved sections
produce overlapping hulls, and :func:`plan_zone_regions` warns about every
overlapping pair it emits.

⚠⚠ **Fill priority is load-bearing, not cosmetic.** KiCad fills higher-priority
zones first and only lower-priority zones pull back, so two *overlapping* zones
at the same priority have **no defined winner** -- KiCad tie-breaks on zone
UUIDs, and a fresh ``uuid4`` is minted per zone per run. On a real board that
made the fill itself vary run to run over bit-identical copper, grading "1 net
unconnected" on **4 of 6 UUID rolls**. Overlapping regions are therefore
separated by ``kicad_writer.zone_overlap_priorities()`` rather than by hand.

⚠ **The KiCad-10 net form is detected from the board, not assumed.**
:func:`board_uses_name_nets` reads the file's own ``(version …)`` and applies
KRT's own threshold (``kicad_parser.KICAD_10_MIN_VERSION``), which is exactly what
``route_planes.py`` does at its five ``generate_zone_sexpr`` call sites. A KiCad-10
board gets ``(net "GND")``; a KiCad-9 board gets ``(net 3)`` + ``(net_name
"GND")``. ⛔ The Phase-15 plan said this "must be ``True`` because the stack is on
the KiCad-10 backend" -- that is the *schematic* backend. ``writer.py`` still
stamps ``(version 20241229) (generator_version "9.0")`` on the **board**, so a
hardcoded ``True`` would have written a KiCad-10 zone into a KiCad-9 file. KiCad
itself parses either form on either version (``parseNet`` dispatches on the token,
not the file version), but KRT's own parser is version-gated, and the graders
this arc runs go through KRT. Detecting is correct on both and needs no change
when the board writer moves to KiCad 10.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_MARGIN_MM",
    "ZonePlan",
    "ZoneRegion",
    "board_uses_name_nets",
    "convex_hull",
    "net_ids_from_board",
    "plan_zone_regions",
    "region_polygon",
    "splice_zones",
    "zone_sexprs",
]


#: Courtyard expansion applied to every member before hulling, in mm. One
#: millimetre is a pour-to-part gap a human would draw and is comfortably wider
#: than any clearance in the corpus, so the hull never lands *on* a courtyard
#: edge where a float comparison decides whether a part is inside its own region.
DEFAULT_MARGIN_MM = 1.0

#: KiCad's own layer name for the back copper, and the layer the escape carve is
#: written to: on a 2-layer board the back is the solid ground pour, and that
#: pour is the thing an escape via has nowhere to land in.
BACK_COPPER = "B.Cu"


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def convex_hull(points) -> list:
    """Counter-clockwise convex hull of ``points`` (Andrew's monotone chain).

    ⛔ **Written here rather than pulled from scipy/shapely.** ``skidl-layout``
    carries no geometry dependency, this is twenty lines, and the arc's rule that
    a polygon must be *identical* between runs is easier to hold with arithmetic
    we can read. Collinear points are dropped (``<= 0``), so a hull of an
    axis-aligned cluster is four corners rather than four corners plus every
    point that happens to lie on an edge.

    Input order does not affect the output: the points are sorted first.
    """
    pts = sorted({(round(float(x), 6), round(float(y), 6)) for x, y in points})
    if len(pts) <= 2:
        return list(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for point in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list = []
    for point in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _expanded_corners(rect, margin: float) -> list:
    x0, y0, x1, y1 = rect
    return [(x0 - margin, y0 - margin), (x1 + margin, y0 - margin),
            (x1 + margin, y1 + margin), (x0 - margin, y1 + margin)]


def region_polygon(refs, placed_parts, geometries, *,
                   margin_mm: float = DEFAULT_MARGIN_MM,
                   style: str = "rect_hull") -> list:
    """The polygon enclosing ``refs``, as the hull of their expanded courtyards.

    Args:
        refs: reference designators. Members with no placement or no loadable
            courtyard are **skipped** -- a flat-millimetre guess is not a
            substitute for a footprint, and the caller learns which were dropped
            from :func:`plan_zone_regions`'s warnings.
        placed_parts: the placement (anything with ``.ref``, ``.x_mm``, ``.y_mm``
            and ``.rot_deg``).
        geometries: ``{footprint_name: FootprintGeometry}``.
        margin_mm: courtyard expansion before hulling.
        style: only ``"rect_hull"`` in round one. ⛔ Concave/alpha shapes are out
            of scope by the plan's §5 -- they are unstable under small placement
            moves, which is the failure this arc already paid for once.

    Returns:
        The hull, counter-clockwise, or ``[]`` when fewer than one member
        resolved. **Deterministic under input reordering** -- the hull sorts its
        own points -- which is what makes a spliced board comparable run to run.
    """
    if style != "rect_hull":
        raise ValueError(
            f"style={style!r} is not supported in round one; only 'rect_hull'. "
            "A concave/alpha region is explicitly out of scope (plan §5): it "
            "flips its outline on small placement moves.")
    from .power_escape import part_rect

    wanted = {str(r) for r in (refs or ())}
    by_ref = {str(p.ref): p for p in (placed_parts or [])}
    margin = float(margin_mm or 0.0)
    corners: list = []
    for ref in sorted(wanted):
        part = by_ref.get(ref)
        if part is None:
            continue
        geometry = geometries.get(str(getattr(part, "footprint", "") or ""))
        if geometry is None:
            continue
        corners.extend(_expanded_corners(part_rect(part, geometry), margin))
    if not corners:
        return []
    return convex_hull(corners)


def _polygon_area(points) -> float:
    """Absolute shoelace area of a closed polygon, mm²."""
    if len(points or ()) < 3:
        return 0.0
    total = 0.0
    for i, (x0, y0) in enumerate(points):
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def _point_in_polygon(x: float, y: float, polygon) -> bool:
    """Ray-cast point-in-polygon; boundary counts as inside.

    Boundary-inclusive on purpose: a region's own member sits ``margin_mm``
    inside its hull by construction, so the only points that land exactly on an
    edge are the hull vertices themselves -- and a vertex of a part's own
    expanded courtyard reading as "outside its region" would be a gate failing on
    a float, not on a board.
    """
    if len(polygon or ()) < 3:
        return False
    inside = False
    n = len(polygon)
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        # On the segment?
        if (min(x0, x1) - 1e-9 <= x <= max(x0, x1) + 1e-9
                and min(y0, y1) - 1e-9 <= y <= max(y0, y1) + 1e-9):
            if abs((x1 - x0) * (y - y0) - (y1 - y0) * (x - x0)) <= 1e-6:
                return True
        if (y0 > y) != (y1 > y):
            t = (y - y0) / (y1 - y0)
            if x < x0 + t * (x1 - x0):
                inside = not inside
    return inside


def _rect_inside(rect, polygon) -> bool:
    """Every corner of ``rect`` inside ``polygon``.

    Convexity makes the corner test sufficient: for a convex hull, a rectangle
    whose four corners are inside is wholly inside. Round-one regions are always
    convex hulls, and an explicit ``polygon=`` override that is *not* convex
    fails this conservatively -- it can only under-report containment, never
    claim a part is enclosed when it is not.
    """
    x0, y0, x1, y1 = rect
    return all(_point_in_polygon(px, py, polygon)
               for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _polygons_overlap(a, b) -> bool:
    """Cheap conservative overlap test for two convex polygons.

    A vertex of either inside the other, or any pair of edges crossing. This is
    a *detector*, not a clipper: it decides whether two regions need distinct
    fill priorities, and over-reporting costs a priority number nobody notices
    while under-reporting costs a non-deterministic fill.
    """
    if len(a or ()) < 3 or len(b or ()) < 3:
        return False
    if any(_point_in_polygon(x, y, b) for x, y in a):
        return True
    if any(_point_in_polygon(x, y, a) for x, y in b):
        return True

    def _segments(poly):
        return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]

    def _cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    for p0, p1 in _segments(a):
        for q0, q1 in _segments(b):
            d1 = _cross(p0, p1, q0)
            d2 = _cross(p0, p1, q1)
            d3 = _cross(q0, q1, p0)
            d4 = _cross(q0, q1, p1)
            if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
                return True
    return False


# --------------------------------------------------------------------------- #
# The region model
# --------------------------------------------------------------------------- #

@dataclass
class ZoneRegion:
    """One poured section: a named subset of parts, and the net poured over it.

    ``polygon`` overrides the derived hull entirely. That is the documented bail
    -out for a board whose sections genuinely interleave (plan §6 bail-out 2):
    a convex hull cannot express two interlocking sections, and an explicit
    outline is honest where a cleverer default would be a guess.
    """

    name: str
    refs: list = field(default_factory=list)
    net: str = ""
    layer: str = BACK_COPPER
    margin_mm: float = DEFAULT_MARGIN_MM
    #: ⚠ Assigned by :func:`plan_zone_regions` via
    #: ``kicad_writer.zone_overlap_priorities`` when regions overlap. Setting it
    #: by hand is allowed and is not second-guessed.
    priority: int = 0
    #: ``False`` selects thermal relief. Which one was used is recorded in
    #: :meth:`to_dict`, because the arc's rule is that every number carries its
    #: source -- and a solid-connected pour and a thermally-relieved one are not
    #: the same board.
    direct_connect: bool = True
    polygon: list | None = None
    #: Where this region came from -- ``"explicit"`` (the caller named the
    #: parts) or ``"derived:<stage>"``. A derived region is a convenience, not a
    #: claim, and a report has to be able to say which it is reading.
    source: str = "explicit"
    #: Members that were dropped because they are not placed or carry no loadable
    #: courtyard. Never silent.
    missing_refs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "refs": list(self.refs),
            "net": self.net,
            "layer": self.layer,
            "margin_mm": round(float(self.margin_mm), 4),
            "priority": int(self.priority),
            "direct_connect": bool(self.direct_connect),
            "source": self.source,
            "missing_refs": list(self.missing_refs),
            "polygon": [[round(x, 4), round(y, 4)] for x, y in (self.polygon or [])],
            "area_mm2": round(_polygon_area(self.polygon or []), 3),
        }


@dataclass
class ZonePlan:
    """The regions to pour, the pour-exclusion polygons, and what was noticed."""

    regions: list = field(default_factory=list)
    #: Pour-exclusion polygons, as ``(layer, polygon, name)``. These become
    #: ``generate_keepout_zone_sexpr`` rule areas -- ⛔ **pour only**; tracks,
    #: vias and pads stay allowed, which is the whole point.
    carves: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    #: ``[(region_a, region_b)]`` -- every overlapping pair, so a reader can see
    #: why the priorities are what they are.
    overlaps: list = field(default_factory=list)

    @property
    def covered_nets(self) -> list:
        """Nets this plan pours itself. Everything else still goes to KRT."""
        out: list = []
        for region in self.regions:
            if region.net and region.net not in out:
                out.append(region.net)
        return out

    def to_dict(self) -> dict:
        return {
            "regions": [r.to_dict() for r in self.regions],
            "covered_nets": self.covered_nets,
            "carves": [{"layer": layer, "name": name,
                        "polygon": [[round(x, 4), round(y, 4)] for x, y in poly],
                        "area_mm2": round(_polygon_area(poly), 3)}
                       for layer, poly, name in self.carves],
            "carve_count": len(self.carves),
            "overlaps": [list(pair) for pair in self.overlaps],
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        if not self.regions and not self.carves:
            return ""
        lines = ["Zone plan:"]
        for region in self.regions:
            lines.append(
                f"  {region.name}: {region.net} on {region.layer}, "
                f"{len(region.refs)} part(s), "
                f"{_polygon_area(region.polygon or []):.1f} mm2, "
                f"priority {region.priority}, "
                + ("solid" if region.direct_connect else "thermal relief"))
        for layer, poly, name in self.carves:
            lines.append(f"  carve {name} on {layer}: "
                         f"{_polygon_area(poly):.2f} mm2 (pour excluded)")
        for warning in self.warnings:
            lines.append(f"  ⚠ {warning}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def _stage_regions(stage_plan, placed_refs) -> list:
    """``[(name, refs, net, source)]`` -- one region per classified power stage.

    ⛔ The element type is :class:`~skidl_layout.power_roles.PowerStage`. A stage
    names its own members: ``devices`` (each a ``PowerDevice`` with ``.ref``),
    ``controller_ref``, ``small_signal_refs``, and ``loops[*].member_refs``. No
    new heuristic is invented here -- the classifier already did this work.
    """
    out: list = []
    for index, stage in enumerate(getattr(stage_plan, "stages", None) or []):
        refs: list = []

        def _add(ref):
            ref = str(ref) if ref else ""
            if ref and ref in placed_refs and ref not in refs:
                refs.append(ref)

        _add(getattr(stage, "controller_ref", None))
        for device in (getattr(stage, "devices", None) or []):
            _add(getattr(device, "ref", None))
        for loop in (getattr(stage, "loops", None) or []):
            for ref in (getattr(loop, "member_refs", None) or []):
                _add(ref)
            for ref in (getattr(loop, "bulk_refs", None) or []):
                _add(ref)
        for ref in (getattr(stage, "small_signal_refs", None) or []):
            _add(ref)
        if not refs:
            continue
        controller = getattr(stage, "controller_ref", None) or f"stage{index}"
        out.append((f"power_{controller}", refs,
                    getattr(stage, "ground_net", None),
                    f"derived:{getattr(stage, 'topology', 'stage')}"))
    return out


def plan_zone_regions(result, circuit=None, fp_lib_dirs=None, *,
                      sections=None, board_layers: int = 2,
                      escape_carve: bool = False,
                      net: str | None = None,
                      layer: str | None = None,
                      margin_mm: float = DEFAULT_MARGIN_MM,
                      direct_connect: bool = True,
                      fp_geometries=None, fab_spec=None,
                      plane_nets=()) -> ZonePlan:
    """Build a :class:`ZonePlan` for a finished placement.

    **Where sections come from, in this order:**

    1. **Explicit** -- ``sections={"power": ["U1", "L1", "D1"], "analog": [...]}``.
       This is the primary path and the user's stated ask. A value may also be a
       dict to set that region's own ``net`` / ``layer`` / ``margin_mm`` /
       ``direct_connect`` / ``polygon``::

           sections={"power": {"refs": ["U1", "L1"], "net": "PGND",
                               "direct_connect": False}}

    2. **Derived** -- with ``sections=None``, one region per
       :class:`~skidl_layout.power_roles.PowerStage`, plus a ``"signal"`` region
       holding every placed part in no stage. ⚠ **Derived regions are a
       convenience, not a claim.** The report says what was derived *and* what a
       human would have written; the two are not the same thing.

    ⭐ **The signal region is not decoration -- it is a correctness requirement.**
    A region-poured net only reaches pads that lie inside some region, so a
    derived plan whose hulls leave part of the board bare would strand ground
    pads that had copper before. Sweeping every unclaimed part into ``"signal"``
    makes the union of the hulls cover the placement, which is what keeps the
    plan from *losing* copper it never meant to touch. Gate Z5 grades it.

    ``escape_carve=True`` adds one pour-exclusion rule area per escape-annulus
    rectangle, on the back copper layer, from Phase 13's own measured geometry
    (:func:`~skidl_layout.power_escape.measure_escape_rooms`). ⛔ It excludes
    **pour**, not tracks -- see this module's docstring.

    Returns a plan whose ``regions`` all carry a resolved ``polygon`` and, where
    they overlap, **distinct** priorities from
    ``kicad_writer.zone_overlap_priorities``.
    """
    from .power_escape import part_rect

    placed = list(getattr(result, "placed_parts", None) or [])
    warnings: list = []
    plan = ZonePlan(warnings=warnings)
    if not placed:
        warnings.append("no placed parts; zone plan is empty")
        return plan

    if fp_geometries is None:
        from .geometry import load_footprint_geometries

        fp_geometries = load_footprint_geometries(
            {str(p.footprint) for p in placed if getattr(p, "footprint", None)},
            list(fp_lib_dirs or []))
    by_ref = {str(p.ref): p for p in placed}

    stage_plan = (getattr(result, "power_stage_plan", None)
                  or getattr(result, "power_plan", None))
    default_layer = layer or BACK_COPPER
    # The net a region pours when nothing names one: the classifier's ground,
    # else the first plane net the copper stage is already pouring. ⛔ Never a
    # name match on "GND" -- that is the guess power_roles exists to replace.
    default_net = net
    if default_net is None:
        for stage in (getattr(stage_plan, "stages", None) or []):
            if getattr(stage, "ground_net", None):
                default_net = str(stage.ground_net)
                break
    if default_net is None and plane_nets:
        default_net = str(list(plane_nets)[0])

    specs: list = []          # (name, refs, net, layer, margin, direct, polygon, source)
    if sections:
        for name, value in sections.items():
            if isinstance(value, dict):
                refs = [str(r) for r in (value.get("refs") or [])]
                specs.append((
                    str(name), refs,
                    value.get("net") or default_net,
                    value.get("layer") or default_layer,
                    float(value.get("margin_mm", margin_mm)),
                    bool(value.get("direct_connect", direct_connect)),
                    value.get("polygon"),
                    "explicit",
                ))
            else:
                specs.append((str(name), [str(r) for r in (value or [])],
                              default_net, default_layer, float(margin_mm),
                              bool(direct_connect), None, "explicit"))
    else:
        claimed: set = set()
        for name, refs, ground, source in _stage_regions(stage_plan, set(by_ref)):
            claimed.update(refs)
            specs.append((name, refs, str(ground) if ground else default_net,
                          default_layer, float(margin_mm), bool(direct_connect),
                          None, source))
        if not specs:
            # ⛔ Declines rather than pouring one hull over the whole board. A
            # single region covering every part is not a *section*; it is a worse
            # Voronoi cell with an extra failure mode, and shipping it would let
            # a board with no classified converter silently take the new path.
            warnings.append(
                "no power stage was classified and no sections were given; "
                "zone plan is empty and the pour falls through to route_planes")
            return plan
        rest = [ref for ref in by_ref if ref not in claimed]
        if rest:
            specs.append(("signal", sorted(rest), default_net, default_layer,
                          float(margin_mm), bool(direct_connect), None,
                          "derived:unclaimed"))

    for (name, refs, region_net, region_layer, region_margin, region_direct,
         explicit_polygon, source) in specs:
        missing = [r for r in refs
                   if r not in by_ref
                   or fp_geometries.get(str(getattr(by_ref[r], "footprint", "") or ""))
                   is None]
        if explicit_polygon:
            polygon = [(float(x), float(y)) for x, y in explicit_polygon]
        else:
            polygon = region_polygon(refs, placed, fp_geometries,
                                     margin_mm=region_margin)
        if not polygon:
            warnings.append(
                f"region {name!r}: no member resolved to a placed footprint; "
                "no zone emitted")
            continue
        if not region_net:
            warnings.append(
                f"region {name!r}: no net to pour (none given, no classified "
                "ground, no plane net); no zone emitted")
            continue
        if missing:
            warnings.append(
                f"region {name!r}: {len(missing)} member(s) not placed or with "
                f"no loadable courtyard, dropped: {', '.join(sorted(missing))}")
        plan.regions.append(ZoneRegion(
            name=name, refs=list(refs), net=str(region_net),
            layer=str(region_layer), margin_mm=region_margin,
            direct_connect=region_direct, polygon=polygon, source=source,
            missing_refs=sorted(missing)))

    # -- containment, reported rather than assumed ---------------------------
    for region in plan.regions:
        outside = []
        for ref in region.refs:
            part = by_ref.get(ref)
            if part is None:
                continue
            geometry = fp_geometries.get(str(getattr(part, "footprint", "") or ""))
            if geometry is None:
                continue
            if not _rect_inside(part_rect(part, geometry), region.polygon):
                outside.append(ref)
        if outside:
            # Only reachable through an explicit ``polygon=`` override -- a
            # derived hull encloses its own members by construction.
            warnings.append(
                f"region {region.name!r}: {len(outside)} member(s) lie OUTSIDE "
                f"the region polygon: {', '.join(sorted(outside))}")

    _assign_priorities(plan, warnings)

    if escape_carve:
        _add_escape_carves(plan, result, fp_lib_dirs, fab_spec=fab_spec,
                           fp_geometries=fp_geometries, warnings=warnings)
    return plan


def _assign_priorities(plan: ZonePlan, warnings: list) -> None:
    """Distinct fill priorities for every overlapping pair, via KRT's own helper.

    ⛔ Not hand-assigned. ``zone_overlap_priorities`` already encodes the rule
    that matters -- smaller area wins, ties break on ``(net_id, index)`` so the
    result never depends on dict order, and zones that overlap nothing stay at 0
    so a board that never had the ambiguity is unaffected.
    """
    if len(plan.regions) < 2:
        return
    # Same-net overlaps merge harmlessly and KRT's helper skips them; record the
    # cross-net ones for the report either way.
    for i, a in enumerate(plan.regions):
        for b in plan.regions[i + 1:]:
            if a.layer != b.layer:
                continue
            if _polygons_overlap(a.polygon or [], b.polygon or []):
                plan.overlaps.append((a.name, b.name))
                if a.net != b.net:
                    warnings.append(
                        f"regions {a.name!r} and {b.name!r} overlap on "
                        f"{a.layer} carrying different nets ({a.net} / {b.net}) "
                        "-- separated by fill priority; consider an explicit "
                        "polygon= override if the sections truly interleave")

    writer = _kicad_writer()
    if writer is None:
        warnings.append(
            "KRT kicad_writer not importable; fill priorities left at 0. "
            "Overlapping zones at equal priority fill non-deterministically")
        return
    # ⚠ Net *ids* are not known until the board is in hand, and the helper only
    # uses them to decide "same net" and to break ties. Stable synthetic ids
    # derived from the net-name order give exactly that, deterministically.
    net_ids = {name: index for index, name
               in enumerate(sorted({r.net for r in plan.regions}))}
    zones = [(r.layer, net_ids[r.net], r.polygon or []) for r in plan.regions]
    for region, priority in zip(plan.regions,
                                writer.zone_overlap_priorities(zones)):
        region.priority = int(priority)


def _add_escape_carves(plan: ZonePlan, result, fp_lib_dirs, *, fab_spec,
                       fp_geometries, warnings: list) -> None:
    """One pour-exclusion rule area per escape-annulus rectangle, on the back.

    Reuses Phase 13's measured geometry rather than re-deriving it:
    ``EscapeRoom.annulus`` is four rectangles tiling the ring, deliberately not
    one keyhole outline, and the controller's own courtyard is excluded by
    construction so the carve never sits on the controller's pads.
    """
    from .power_escape import measure_escape_rooms

    rooms = measure_escape_rooms(result, fp_lib_dirs, fab_spec=fab_spec,
                                 fp_geometries=fp_geometries)
    if not rooms:
        warnings.append(
            "escape_carve requested but no escape annulus could be measured "
            "(no declared part, no classified controller); no carve emitted")
        return
    for room in rooms:
        for index, polygon in enumerate(room.annulus or []):
            plan.carves.append((BACK_COPPER, [(float(x), float(y))
                                              for x, y in polygon],
                                f"escape-{room.controller_ref}-{index}"))
    warnings.append(
        f"escape_carve: {len(plan.carves)} pour-exclusion rule area(s) on "
        f"{BACK_COPPER} around "
        + ", ".join(f"{r.controller_ref} (lane {r.lane_mm:.4f}mm)"
                    for r in rooms)
        + " -- these block POUR only; tracks, vias and pads stay allowed")


# --------------------------------------------------------------------------- #
# Emitting -- KRT's writer, called directly
# --------------------------------------------------------------------------- #

def _kicad_writer():
    """KRT's ``kicad_writer`` module, or ``None``.

    Imported here rather than shelled out to, because it is a **pure-Python**
    module needing no Rust core -- which is the finding this whole phase rests
    on. ``find_krt`` resolves the checkout the same way every other seam does
    (explicit arg -> ``SKIDL_LAYOUT_KRT_DIR`` -> the workspace sibling).
    """
    from .krt import find_krt

    resolved = find_krt()
    if resolved is None:
        return None
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    try:
        import kicad_writer  # noqa: PLC0415
    except ImportError:
        return None
    return kicad_writer


_VERSION_RE = re.compile(r"\(version\s+(\d+)\s*\)")

#: KRT's own threshold (``kicad_parser.KICAD_10_MIN_VERSION``), duplicated as a
#: fallback for a tree where KRT is not importable. Kept in sync by
#: ``test_power_zones.py``, which asserts the two agree when KRT *is* importable.
KICAD_10_MIN_VERSION = 20250000


def board_uses_name_nets(pcb_text: str) -> bool:
    """Does this board spell a zone's net as ``(net "NAME")`` (KiCad 10)?

    ⚠ **Detected, never assumed.** ``route_planes.py`` decides exactly this way
    at all five of its ``generate_zone_sexpr`` call sites
    (``use_net_name=pcb_data.kicad_version >= KICAD_10_MIN_VERSION``), and a zone
    that disagrees with the board it is spliced into is a zone KRT's grader will
    not associate with its net -- which reads as "the pour did nothing" rather
    than as a bug.

    Measured on this stack: ``skidl_layout.writer`` stamps ``(version 20241229)``
    / ``generator_version "9.0"`` on the **board** even though the *schematic*
    backend is KiCad 10, so these boards take the KiCad-9 form today and will
    take the KiCad-10 form, with no change here, the day the writer moves.
    """
    match = _VERSION_RE.search(pcb_text or "")
    version = int(match.group(1)) if match else 0
    return version >= KICAD_10_MIN_VERSION


def zone_sexprs(plan: ZonePlan, net_ids: dict, *, clearance=None,
                min_thickness=None, kicad10: bool = False) -> list:
    """The zone s-expressions for ``plan``, built by KRT's own writer.

    Args:
        plan: a :class:`ZonePlan`.
        net_ids: ``{net_name: net_id}`` from :func:`net_ids_from_board` on the
            board being spliced. A region whose net is absent is **skipped with
            no zone** -- a zone pointing at a net the board does not carry is a
            zone KiCad loads and nothing connects to. ⚠ On a KiCad-10 board the
            ids are synthetic (there is no net table) and are never written;
            ``kicad10=True`` emits ``(net "NAME")`` and no id.
        clearance / min_thickness: ``None`` uses ``generate_zone_sexpr``'s own
            defaults (0.2 / 0.1), so a knobless call matches KRT's shape.
        kicad10: the net-header form. **Pass
            :func:`board_uses_name_nets` on the target board's text** rather than
            a constant.

    Returns:
        Zone blocks first, then carve rule areas. ⚠ Carves are emitted **after**
        the pours so a reader scanning the file sees the exclusions applied to
        copper that already exists; KiCad's filler is order-independent, so this
        is legibility, not semantics.
    """
    writer = _kicad_writer()
    if writer is None:
        raise RuntimeError(
            "KiCadRoutingTools' kicad_writer is not importable; cannot emit "
            "zones (set SKIDL_LAYOUT_KRT_DIR or place a checkout at the "
            "workspace sibling KiCadRoutingTools/)")

    out: list = []
    kwargs = {}
    if clearance is not None:
        kwargs["clearance"] = float(clearance)
    if min_thickness is not None:
        kwargs["min_thickness"] = float(min_thickness)

    for region in plan.regions:
        net_id = net_ids.get(region.net)
        if net_id is None:
            plan.warnings.append(
                f"region {region.name!r}: net {region.net!r} is not on the "
                "board; no zone emitted")
            continue
        out.append(writer.generate_zone_sexpr(
            net_id=int(net_id), net_name=region.net, layer=region.layer,
            polygon_points=[(float(x), float(y)) for x, y in region.polygon],
            direct_connect=bool(region.direct_connect),
            use_net_name=bool(kicad10), priority=int(region.priority),
            **kwargs))
    for layer, polygon, name in plan.carves:
        out.append(writer.generate_keepout_zone_sexpr(
            layers=[layer], polygon_points=polygon, name=name,
            use_net_name=bool(kicad10)))
    return out


def splice_zones(pcb_path: str, out_path: str, sexprs) -> int:
    """Write ``pcb_path`` to ``out_path`` with ``sexprs`` added. Returns the count.

    Follows :func:`skidl_layout.copper_post.splice_vias`'s precedent -- a
    balanced-paren text splice rather than a re-serialised parse -- for the
    reason recorded there: KiCad-10 formatting is not cosmetic, and re-emitting a
    whole board from a parse risks changing spelling the file already uses. The
    blocks go in immediately **after the last existing top-level ``(zone …)``**
    when the board has one (so ours sit with the pours KRT wrote), else before
    the board's final closing paren.

    ⚠ **Carries the sibling ``.kicad_pro``.** The project file holds the DRC
    floor the chain routed to; stranding it manufactures phantom clearance
    violations at the very next grading step.
    """
    blocks = [s for s in (sexprs or []) if s and s.strip()]
    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if not blocks:
        if os.path.abspath(out_path) != os.path.abspath(pcb_path):
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            _copy_sibling_project(pcb_path, out_path)
        return 0

    insert_at = _insert_point(text)
    payload = "\n" + "\n".join(block.rstrip() for block in blocks)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(text[:insert_at] + payload + text[insert_at:])
    _copy_sibling_project(pcb_path, out_path)
    return len(blocks)


_ZONE_HEAD_RE = re.compile(r"\n([\t ]*)\(zone[\s\n]")


def _insert_point(text: str) -> int:
    """Where a new top-level block goes: after the last zone, else before EOF's ``)``."""
    from .copper_post import _balanced_end

    last = None
    for match in _ZONE_HEAD_RE.finditer(text):
        last = match
    if last is not None:
        body_start = last.end() - len("(zone") - 1
        return _balanced_end(text, body_start)
    end = text.rstrip()
    if not end.endswith(")"):
        raise ValueError("board text does not end in a closing paren")
    return len(end) - 1


def _copy_sibling_project(src_pcb: str, dst_pcb: str) -> None:
    import shutil

    sibling = os.path.splitext(src_pcb)[0] + ".kicad_pro"
    if (os.path.isfile(sibling)
            and os.path.abspath(src_pcb) != os.path.abspath(dst_pcb)):
        try:
            shutil.copyfile(sibling, os.path.splitext(dst_pcb)[0] + ".kicad_pro")
        except OSError:  # pragma: no cover - best effort
            pass


_NET_TABLE_RE = re.compile(r'\(net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\)')
_NET_NAME_ONLY_RE = re.compile(r'\(net\s+"((?:[^"\\]|\\.)*)"\)')


def net_ids_from_board(pcb_text: str) -> dict:
    """``{net_name: net_id}`` for every net the board carries.

    ⛔⛔ **KiCad 10 has no net table.** The ``(net <id> "<name>")`` block every
    pre-10 board opens with is **gone**; nets exist only as ``(net "NAME")`` on
    the items that use them, and pcbnew re-derives the codes on load. Measured
    directly: a board round-tripped through KiCad 10.0.4 came back with the
    entire table removed and exactly two ``(net "GND")`` / ``(net "VIN")``
    references left.

    That is why this reads **both** forms, the same way KRT's
    ``kicad_parser.extract_nets`` does -- legacy ids from the table, synthetic
    ids by first appearance on a KiCad-10 board. The synthetic id is never
    written anywhere: on a KiCad-10 board the zone header is ``(net "NAME")`` and
    carries no id at all, so the id's only job is to tell
    ``zone_overlap_priorities`` which regions share a net.

    ⚠ ``(net "")`` is the canonical no-net and maps to 0 rather than getting a
    synthetic id -- KRT learned that one on a real board whose dangling copper
    ended up on a phantom net.
    """
    out: dict = {}
    for match in _NET_TABLE_RE.finditer(pcb_text):
        out.setdefault(match.group(2).replace('\\"', '"'), int(match.group(1)))
    if out:
        return out
    out[""] = 0
    synthetic = 1
    for match in _NET_NAME_ONLY_RE.finditer(pcb_text):
        name = match.group(1).replace('\\"', '"')
        if name in out:
            continue
        out[name] = synthetic
        synthetic += 1
    return out
