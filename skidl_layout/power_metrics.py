# -*- coding: utf-8 -*-
"""Power-layout geometry -- *"is this power placement any good"* as a number.

Power-layout **Phase 2, Stage A**. :mod:`~skidl_layout.power_roles` (Phase 1)
names things: it can tell you that ``COUT3 -> D1 -> M1 -> RS`` is the commutation
loop, that ``SW`` is the switch node, that ``(R1, R2)`` is the feedback divider.
It cannot tell you whether those parts sit 6 mm apart or 60 mm apart. This module
answers that, and only that.

**The split that keeps the objective honest.** The
:class:`~skidl_layout.power_roles.PowerStagePlan` is a pure function of the
*netlist*; these metrics are a pure function of ``(placement, plan)``. So a
placement is always scored against a plan computed independently of it -- change
the placement and the plan does not move underneath you. ``power_roles`` never
sees a coordinate and nothing here may make it.

**Report-only.** :func:`measure_power_layout` is called from ``plan_layout``
*after* the candidate is selected, exactly where ``classify_power_roles`` is, so
it cannot influence the placement. The opt-in scorer term that *does* consume
these numbers lives in ``engine._apply_power_loop_score`` and is off by default.

    result = plan_layout(circuit, ...)
    print(result.power_metrics.summary())

Every metric is ``None`` when its inputs are absent -- no stage, no such role, the
part was not placed. Never ``0.0``, which would read as "perfect".

--------------------------------------------------------------------------- #
The bow tie, and why a naive shoelace is worse than no loop-area metric at all
--------------------------------------------------------------------------- #

The commutation loop's vertices are visited in a fixed **electrical** order (the
capacitor, then the rectifier, then the switch, then the sense resistor). On a
scattered placement that order draws a self-intersecting bow tie whose two lobes
circulate oppositely, so the signed shoelace sum partly *cancels*: measured on
the Phase-0 corpus, the scattered variant A scored ``65.3 mm2`` against the tight
hand floorplan's ``57.0 mm2`` -- 15 % *better*, which is exactly backwards.

:func:`_self_intersects` is therefore not optional, and
``effective_area_mm2`` -- the field every consumer should read -- falls back to
the **convex hull** whenever the polygon crosses itself. On that same variant A
the hull reads ``119.1 mm2``, which is the honest answer.

--------------------------------------------------------------------------- #
Thresholds: see :mod:`~skidl_layout.layout_quality`
--------------------------------------------------------------------------- #

This module measures; it does not judge. The trip points, the population they
were calibrated against, and the honest note about how small that population is
all live beside the ``HOT_LOOP_*`` / ``FB_NODE_LENGTH`` / ... codes in
:mod:`~skidl_layout.layout_quality`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import transform_point

__all__ = [
    "LoopGeometry",
    "PowerMetrics",
    "StageMetrics",
    "measure_power_layout",
]


# --------------------------------------------------------------------------- #
# Polygon primitives -- ported from the Phase-0 instrument
# (skidl-eda/canaries/lt3757_boost/measure_power_layout.py), which keeps its own
# copies: the instrument reads a routed .kicad_pcb and must stay runnable with
# skidl-layout absent, and gate P6 of drive_phase0.py depends on it.
# --------------------------------------------------------------------------- #

def _shoelace(points) -> float:
    """Signed area of the closed polyline through ``points``."""
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _perimeter(points) -> float:
    return sum(
        _dist(points[i], points[(i + 1) % len(points)]) for i in range(len(points))
    )


def _segments_cross(p1, p2, p3, p4) -> bool:
    """True if the open segments ``p1p2`` and ``p3p4`` properly cross."""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 1e-9) - (v < -1e-9)

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return d1 * d2 < 0 and d3 * d4 < 0


def _self_intersects(points) -> bool:
    """Whether the closed polyline crosses itself -- see the module docstring."""
    n = len(points)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if _segments_cross(
                points[i], points[(i + 1) % n], points[j], points[(j + 1) % n]
            ):
                return True
    return False


def _convex_hull_area(points) -> float:
    """Area of the convex hull -- the honest fallback for a bow tie."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return 0.0

    def build(seq):
        hull = []
        for p in seq:
            while len(hull) >= 2:
                (x1, y1), (x2, y2) = hull[-2], hull[-1]
                if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) > 0:
                    break
                hull.pop()
            hull.append(p)
        return hull

    hull = build(pts)[:-1] + build(reversed(pts))[:-1]
    return abs(_shoelace(hull))


# --------------------------------------------------------------------------- #
# Pad lookup -- mirrors decaps._pads_for_net / _pad_world_xy
#
# Deliberately re-stated here rather than imported: ``decaps`` pulls in
# ``placer`` and the whole constraint stack, and this module is meant to stay a
# cheap, side-effect-free geometry import.
# --------------------------------------------------------------------------- #

def _pin_number(pin, index: int) -> str:
    for attr in ("num", "number", "pin_number", "name"):
        value = getattr(pin, attr, None)
        if value not in (None, ""):
            return str(value).strip('"')
    return str(index + 1)


def _part_pin_nets_by_number(part) -> dict:
    pin_nets: dict = {}
    for index, pin in enumerate(getattr(part, "pins", []) or []):
        net = getattr(pin, "net", None)
        name = getattr(net, "name", None)
        if name:
            pin_nets[_pin_number(pin, index)] = str(name)
    return pin_nets


def _pad_world_points(part, geometry, placed, net_name) -> list:
    """World coordinates of every pad of ``placed`` that sits on ``net_name``.

    Empty when the footprint geometry is unavailable or the part has no pad on
    that net -- callers fall back to the centroid and say so.
    """
    if part is None or geometry is None or placed is None or not net_name:
        return []
    pin_nets = _part_pin_nets_by_number(part)
    points = []
    for pad in geometry.pads:
        name = pad.net_name or pin_nets.get(pad.number)
        if name == net_name:
            points.append(
                transform_point(
                    placed.x_mm, placed.y_mm, placed.rot_deg, pad.x_mm, pad.y_mm
                )
            )
    return points


# --------------------------------------------------------------------------- #
# Result shapes
# --------------------------------------------------------------------------- #

def _round(value, digits=3):
    return None if value is None else round(value, digits)


@dataclass
class LoopGeometry:
    """One commutation loop, measured from footprint centroids.

    ``effective_area_mm2`` is the field downstream consumers should read: it is
    the shoelace area on a simple polygon and the **convex-hull** area on a
    self-intersecting one, so a bow tie can never report as tight.

    ``min_perimeter_mm`` is the **tightest perimeter these parts admit**: walking
    the loop's own edge order and adding, per edge, the two members' courtyard
    half-diagonals, which is the centre-to-centre distance they would have if
    their courtyards just touched corner-on. It is what turns a raw millimetre
    into a comparable number -- a loop containing a 44 mm transformer cannot be
    held to a 2010 sense resistor's budget.

    ``perimeter_ratio`` and ``looseness_ratio`` are that normalization: perimeter
    against the bound, and effective **area** against the area of a square with
    that bound as its perimeter. Both are ``None`` without footprint geometry.

    MEASURED, and the reason the normalizer is edge-wise rather than the summed
    courtyard *area* the plan first suggested: on ``avalanche_laser_driver`` one
    member (a 43.7 x 38.7 mm transformer) is 90 % of the summed courtyard area,
    so an area normalizer flattered every loop containing it to 0.13x while its
    perimeter ratio -- correctly -- reads 1.11x. The half-diagonal bound charges
    that transformer only to the two edges it actually touches.
    """

    member_refs: list
    bulk_refs: list = field(default_factory=list)
    area_mm2: float | None = None
    effective_area_mm2: float | None = None
    convex_hull_area_mm2: float | None = None
    perimeter_mm: float | None = None
    longest_edge_mm: float | None = None
    self_intersecting: bool = False
    edges_mm: dict = field(default_factory=dict)
    min_perimeter_mm: float | None = None
    perimeter_ratio: float | None = None
    looseness_ratio: float | None = None

    def to_dict(self) -> dict:
        return {
            "member_refs": list(self.member_refs),
            "bulk_refs": list(self.bulk_refs),
            "area_mm2": _round(self.area_mm2),
            "effective_area_mm2": _round(self.effective_area_mm2),
            "convex_hull_area_mm2": _round(self.convex_hull_area_mm2),
            "perimeter_mm": _round(self.perimeter_mm),
            "longest_edge_mm": _round(self.longest_edge_mm),
            "self_intersecting": bool(self.self_intersecting),
            "edges_mm": {k: _round(v) for k, v in self.edges_mm.items()},
            "min_perimeter_mm": _round(self.min_perimeter_mm),
            "perimeter_ratio": _round(self.perimeter_ratio),
            "looseness_ratio": _round(self.looseness_ratio),
        }


@dataclass
class StageMetrics:
    """Every placement-derivable number for one power stage."""

    controller_ref: str | None = None
    topology: str = "unknown"
    loops: list = field(default_factory=list)
    fb_node: dict = field(default_factory=dict)
    sense_return: dict = field(default_factory=dict)
    small_signal: dict = field(default_factory=dict)
    bulk_caps: dict = field(default_factory=dict)
    switch_node: dict = field(default_factory=dict)
    kelvin: dict = field(default_factory=dict)
    #: False when ``fp_geometries`` was unavailable, so every pad-referenced
    #: distance degraded to a centroid. Reported, never silently assumed.
    pad_accurate: bool = False

    def to_dict(self) -> dict:
        return {
            "controller_ref": self.controller_ref,
            "topology": self.topology,
            "loops": [loop.to_dict() for loop in self.loops],
            "fb_node": dict(self.fb_node),
            "sense_return": dict(self.sense_return),
            "small_signal": dict(self.small_signal),
            "bulk_caps": dict(self.bulk_caps),
            "switch_node": dict(self.switch_node),
            "kelvin": dict(self.kelvin),
            "pad_accurate": bool(self.pad_accurate),
        }


@dataclass
class PowerMetrics:
    """What :func:`measure_power_layout` found. Empty on a non-power board."""

    stages: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "stages": [stage.to_dict() for stage in self.stages],
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        """Human-readable summary, or ``""`` when there is nothing to say.

        Silence on a board with no converter is the contract -- the caller only
        appends this block when it is non-empty.
        """
        if not self.stages and not self.warnings:
            return ""
        lines = ["Power layout metrics:"]
        for stage in self.stages:
            head = f"  {stage.topology} stage on {stage.controller_ref}"
            if not stage.pad_accurate:
                head += " (centroid-only: no footprint geometry)"
            lines.append(head)
            for loop in stage.loops:
                bow = " BOW TIE" if loop.self_intersecting else ""
                lines.append(
                    f"    loop {' -> '.join(loop.member_refs)}: "
                    f"area={_fmt(loop.effective_area_mm2)}mm2{bow} "
                    f"perimeter={_fmt(loop.perimeter_mm)}mm "
                    f"longest_edge={_fmt(loop.longest_edge_mm)}mm "
                    f"looseness={_fmt(loop.looseness_ratio, 2)}x "
                    f"perimeter_ratio={_fmt(loop.perimeter_ratio, 2)}x"
                )
            fb = stage.fb_node
            if fb.get("fb_top_to_fb_pad_mm") is not None or \
                    fb.get("fb_top_to_fb_bottom_mm") is not None:
                lines.append(
                    f"    feedback: node={_fmt(fb.get('fb_top_to_fb_pad_mm'))}mm "
                    f"divider={_fmt(fb.get('fb_top_to_fb_bottom_mm'))}mm"
                )
            sense = stage.sense_return
            if sense.get("sense_r_to_switch_mm") is not None:
                lines.append(
                    f"    sense return: to switch="
                    f"{_fmt(sense.get('sense_r_to_switch_mm'))}mm "
                    f"to controller GND="
                    f"{_fmt(sense.get('sense_r_to_controller_gnd_pad_mm'))}mm"
                )
            ss = stage.small_signal
            if ss.get("min_mm") is not None:
                lines.append(
                    f"    small-signal separation: {_fmt(ss.get('min_mm'))}mm "
                    f"(worst {ss.get('worst_pair')})"
                )
            sw = stage.switch_node
            if sw.get("span_mm") is not None:
                lines.append(
                    f"    switch node {sw.get('net')}: span="
                    f"{_fmt(sw.get('span_mm'))}mm over {len(sw.get('refs') or [])} parts"
                )
            kelvin = stage.kelvin
            if kelvin.get("neighbour_ref"):
                verdict = "output cap" if kelvin.get("taps_output_cap") else "NOT an output cap"
                lines.append(
                    f"    Kelvin tap: {kelvin.get('fb_top')} taps "
                    f"{kelvin.get('neighbour_ref')} at "
                    f"{_fmt(kelvin.get('neighbour_mm'))}mm -- {verdict}"
                )
            bulk = stage.bulk_caps
            if bulk.get("max_mm") is not None:
                lines.append(
                    f"    bulk/decoupling: worst {bulk.get('worst_ref')} at "
                    f"{_fmt(bulk.get('max_mm'))}mm from the loop"
                )
        if self.warnings:
            lines.append("  Warnings:")
            for warning in self.warnings[:20]:
                lines.append(f"    {warning}")
        return "\n".join(lines)


def _fmt(value, digits=2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #

class _Placement:
    """Placement + netlist + footprint geometry, as one lookup surface.

    Everything is ``getattr``-based so a
    :class:`~skidl_layout.snapshot.SnapshotCircuit` behaves identically to a live
    ``Circuit`` -- the same duck-typed contract ``power_roles`` holds to.
    """

    def __init__(self, placed_parts, circuit=None, fp_geometries=None):
        self.placed = {p.ref: p for p in (placed_parts or [])}
        self.fp_geometries = fp_geometries or {}
        self.part_by_ref = {}
        self.net_refs: dict = {}
        for part in (getattr(circuit, "parts", None) or []):
            ref = getattr(part, "ref", None)
            if ref is not None:
                self.part_by_ref[str(ref)] = part
        # net name -> refs, in circuit-part order (never alphabetical: that is
        # naming, and gate G3 scrambles names).
        for ref, part in self.part_by_ref.items():
            for net_name in _part_pin_nets_by_number(part).values():
                bucket = self.net_refs.setdefault(net_name, [])
                if ref not in bucket:
                    bucket.append(ref)

    # -- queries ------------------------------------------------------------
    def centroid(self, ref):
        placed = self.placed.get(ref)
        return None if placed is None else (placed.x_mm, placed.y_mm)

    def geometry(self, ref):
        placed = self.placed.get(ref)
        if placed is None:
            return None
        return self.fp_geometries.get(placed.footprint)

    def pads_on(self, ref, net_name) -> list:
        return _pad_world_points(
            self.part_by_ref.get(ref), self.geometry(ref), self.placed.get(ref), net_name
        )

    def anchor(self, ref, net_name):
        """A pad on ``net_name`` if we have one, else the centroid.

        Returns ``(point, pad_accurate)``; ``(None, False)`` when unplaced.
        """
        pads = self.pads_on(ref, net_name)
        if pads:
            return pads[0], True
        return self.centroid(ref), False

    def nearest_pad_distance(self, from_xy, ref, net_name):
        """Distance from ``from_xy`` to ``ref``'s nearest pad on ``net_name``."""
        pads = self.pads_on(ref, net_name)
        if not pads:
            return None
        return min(_dist(from_xy, pad) for pad in pads)

    def refs_on(self, net_name) -> list:
        return list(self.net_refs.get(net_name, ()))

    def courtyard_radius(self, ref):
        """Half the courtyard diagonal -- the part's own corner-on reach."""
        geometry = self.geometry(ref)
        if geometry is None:
            return None
        x_min, y_min, x_max, y_max = geometry.bounds
        return math.hypot(max(0.0, x_max - x_min), max(0.0, y_max - y_min)) / 2.0

    def has_geometry(self) -> bool:
        return bool(self.fp_geometries)


def _loop_geometry(view, loop) -> LoopGeometry:
    """Measure one :class:`~skidl_layout.power_roles.CommutationLoop`."""
    refs = list(loop.member_refs)
    result = LoopGeometry(member_refs=refs, bulk_refs=list(loop.bulk_refs))
    points = [view.centroid(ref) for ref in refs]
    if len(points) < 3 or any(p is None for p in points):
        # Fewer than three placed members is not a polygon. Leave every field
        # None -- 0.0 would read as a perfect loop.
        return result

    result.area_mm2 = abs(_shoelace(points))
    result.self_intersecting = _self_intersects(points)
    result.convex_hull_area_mm2 = _convex_hull_area(points)
    result.effective_area_mm2 = (
        result.convex_hull_area_mm2 if result.self_intersecting else result.area_mm2
    )
    result.perimeter_mm = _perimeter(points)
    edges = {}
    for i, ref in enumerate(refs):
        nxt = refs[(i + 1) % len(refs)]
        edges[f"{ref}-{nxt}"] = _dist(points[i], points[(i + 1) % len(points)])
    result.edges_mm = edges
    result.longest_edge_mm = max(edges.values()) if edges else None

    radii = [view.courtyard_radius(ref) for ref in refs]
    if all(r is not None for r in radii):
        # Edge-wise: what each hop would measure if the two courtyards touched.
        floor = sum(
            radii[i] + radii[(i + 1) % len(radii)] for i in range(len(radii))
        )
        if floor > 0.0:
            result.min_perimeter_mm = floor
            result.perimeter_ratio = result.perimeter_mm / floor
            # The tightest area these parts admit is a square of that perimeter.
            result.looseness_ratio = result.effective_area_mm2 / (floor / 4.0) ** 2
    return result


def _fb_node(view, stage) -> dict:
    """Section 3.2 -- the high-impedance feedback node."""
    out = {
        "fb_top": None,
        "fb_bottom": None,
        "feedback_net": stage.feedback_net,
        "fb_top_to_fb_pad_mm": None,
        "fb_top_to_fb_bottom_mm": None,
        "pad_accurate": False,
    }
    if not stage.feedback_divider:
        return out
    top, bottom = stage.feedback_divider
    out["fb_top"], out["fb_bottom"] = top, bottom
    top_xy = view.centroid(top)
    bottom_xy = view.centroid(bottom)
    if top_xy is not None and bottom_xy is not None:
        out["fb_top_to_fb_bottom_mm"] = _dist(top_xy, bottom_xy)
    if top_xy is not None and stage.controller_ref and stage.feedback_net:
        pad_mm = view.nearest_pad_distance(
            top_xy, stage.controller_ref, stage.feedback_net
        )
        if pad_mm is not None:
            out["fb_top_to_fb_pad_mm"] = pad_mm
            out["pad_accurate"] = True
        else:
            controller_xy = view.centroid(stage.controller_ref)
            if controller_xy is not None:
                out["fb_top_to_fb_pad_mm"] = _dist(top_xy, controller_xy)
    return out


def _sense_return(view, stage) -> dict:
    """Section 3.3 -- the current-sense return path."""
    out = {
        "sense_resistor_ref": stage.sense_resistor_ref,
        "sense_r_to_switch_mm": None,
        "sense_r_to_controller_gnd_pad_mm": None,
        "sense_r_to_controller_sense_pad_mm": None,
        "pad_accurate": False,
    }
    ref = stage.sense_resistor_ref
    if not ref:
        return out
    sense_xy = view.centroid(ref)
    if sense_xy is None:
        return out

    switches = stage.refs_of_kind("switch")
    if switches:
        switch_xy = view.centroid(switches[0])
        if switch_xy is not None:
            out["sense_r_to_switch_mm"] = _dist(sense_xy, switch_xy)

    controller = stage.controller_ref
    if controller:
        gnd = view.nearest_pad_distance(sense_xy, controller, stage.ground_net)
        if gnd is not None:
            out["sense_r_to_controller_gnd_pad_mm"] = gnd
            out["pad_accurate"] = True
        sense_pad = view.nearest_pad_distance(sense_xy, controller, stage.sense_net)
        if sense_pad is not None:
            out["sense_r_to_controller_sense_pad_mm"] = sense_pad
            out["pad_accurate"] = True
    return out


def _small_signal(view, stage, switch_refs) -> dict:
    """Section 3.4 -- separation is a **floor**: bigger is better.

    Deliberately given its own comparison direction in the docstring because
    every other metric in this module is a ceiling, and mixing them up silently
    inverts the objective.
    """
    out = {"min_mm": None, "worst_pair": None, "pairs_mm": {}}
    pairs = {}
    for ss in stage.small_signal_refs:
        a = view.centroid(ss)
        if a is None:
            continue
        for sw in switch_refs:
            if sw == ss:
                continue
            b = view.centroid(sw)
            if b is not None:
                pairs[f"{ss}-{sw}"] = _dist(a, b)
    if not pairs:
        return out
    worst = min(pairs, key=lambda k: (pairs[k], k))
    out["pairs_mm"] = pairs
    out["min_mm"] = pairs[worst]
    out["worst_pair"] = worst
    return out


def _bulk_caps(view, stage) -> dict:
    """Section 3.5 -- how far each rail capacitor sits from the hot loop.

    Measured against ``member_refs | bulk_refs``, per Phase-1 limitation **L-2**:
    where a rail is shared with another consumer, nothing topological separates
    the candidate loop capacitors, so scoring against the single chosen member
    would punish a placement for a tie-break it never made.
    """
    out = {"distances_mm": {}, "max_mm": None, "worst_ref": None}
    anchors = []
    for loop in stage.loops:
        for ref in list(loop.member_refs) + list(loop.bulk_refs):
            xy = view.centroid(ref)
            if xy is not None and (ref, xy) not in anchors:
                anchors.append((ref, xy))
    if not anchors:
        return out

    anchor_refs = {ref for ref, _ in anchors}
    distances = {}
    for ref in stage.refs_of_kind("input_cap") + stage.refs_of_kind("output_cap"):
        if ref in anchor_refs:
            continue
        xy = view.centroid(ref)
        if xy is None:
            continue
        distances[ref] = min(_dist(xy, other) for _r, other in anchors)
    if not distances:
        return out
    worst = max(distances, key=lambda k: (distances[k], k))
    out["distances_mm"] = distances
    out["max_mm"] = distances[worst]
    out["worst_ref"] = worst
    return out


def _switch_node(view, stage, switch_refs) -> dict:
    """Section 3.6 -- bounding-box span of the parts on the switch node.

    The placement-honest proxy for switch-node copper area. The copper number
    itself (``sum(segment_length * width)``) needs a **routed** board and belongs
    to Phase 5; emitting a ``..._COPPER_AREA`` from a placement would be a lying
    name.
    """
    out = {
        "net": stage.switch_node_nets[0] if stage.switch_node_nets else None,
        "refs": list(switch_refs),
        "span_mm": None,
        "bbox_area_mm2": None,
        "bbox_mm": {},
    }
    points = [view.centroid(ref) for ref in switch_refs]
    points = [p for p in points if p is not None]
    if len(points) < 2:
        return out
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    out["span_mm"] = math.hypot(width, height)
    out["bbox_area_mm2"] = width * height
    out["bbox_mm"] = {
        "x_min": min(xs), "y_min": min(ys), "x_max": max(xs), "y_max": max(ys),
        "width": width, "height": height,
    }
    return out


def _kelvin(view, stage) -> dict:
    """Section 3.7 -- which part the feedback divider actually taps.

    Every multi-pad net routes as a Euclidean MST, so the divider top's *nearest*
    neighbour on the output rail is the part it electrically taps. The datasheet
    wants that to be an output capacitor; if it is the rectifier, the sense
    divider hangs off the high-dV/dt end of the net.
    """
    out = {
        "fb_top": None,
        "output_rail": stage.output_rail,
        "neighbour_ref": None,
        "neighbour_mm": None,
        "taps_output_cap": None,
        "nearest_output_cap_ref": None,
        "nearest_output_cap_mm": None,
        "pad_accurate": False,
    }
    if not stage.feedback_divider or not stage.output_rail:
        return out
    top = stage.feedback_divider[0]
    out["fb_top"] = top
    origin, pad_accurate = view.anchor(top, stage.output_rail)
    if origin is None:
        return out
    out["pad_accurate"] = pad_accurate

    output_caps = set(stage.refs_of_kind("output_cap"))
    candidates = []
    for ref in view.refs_on(stage.output_rail):
        if ref == top:
            continue
        pads = view.pads_on(ref, stage.output_rail)
        if pads:
            distance = min(_dist(origin, pad) for pad in pads)
        else:
            centroid = view.centroid(ref)
            if centroid is None:
                continue
            distance = _dist(origin, centroid)
        candidates.append((distance, ref))
    if not candidates:
        return out
    candidates.sort(key=lambda item: (item[0], item[1]))
    out["neighbour_mm"], out["neighbour_ref"] = candidates[0]
    out["taps_output_cap"] = candidates[0][1] in output_caps
    for distance, ref in candidates:
        if ref in output_caps:
            out["nearest_output_cap_mm"], out["nearest_output_cap_ref"] = distance, ref
            break
    return out


def measure_power_layout(
    placed_parts,
    power_stage_plan,
    circuit=None,
    fp_geometries=None,
) -> PowerMetrics:
    """Measure a placement against a :class:`PowerStagePlan`.

    Args:
        placed_parts: the final ``PlacedPart`` list -- ``.ref .x_mm .y_mm
            .rot_deg .side .footprint``.
        power_stage_plan: what :func:`~skidl_layout.power_roles.classify_power_roles`
            found. ``None`` or an empty plan -> an empty result.
        circuit: the netlist, for connectivity (which parts sit on the switch
            node) and pad-to-net resolution. Duck-typed; a
            ``SnapshotCircuit`` works identically.
        fp_geometries: ``footprint name -> FootprintGeometry``, as carried on
            ``LayoutResult.fp_geometries``. ``None`` degrades every
            pad-referenced distance to a centroid and sets
            ``StageMetrics.pad_accurate`` False.

    Returns:
        A :class:`PowerMetrics`. Never raises on a missing part or role -- the
        affected field comes back ``None``.
    """
    metrics = PowerMetrics()
    stages = list(getattr(power_stage_plan, "stages", None) or [])
    if not stages:
        return metrics

    view = _Placement(placed_parts, circuit=circuit, fp_geometries=fp_geometries)

    for stage in stages:
        switch_net = stage.switch_node_nets[0] if stage.switch_node_nets else None
        switch_refs = view.refs_on(switch_net) if switch_net else []
        if switch_net and not switch_refs:
            metrics.warnings.append(
                f"{stage.controller_ref}: no parts found on switch node "
                f"{switch_net!r} (was a circuit passed?)"
            )
        loops = [_loop_geometry(view, loop) for loop in stage.loops]
        unplaced = [
            ref
            for loop in stage.loops
            for ref in loop.member_refs
            if view.centroid(ref) is None
        ]
        if unplaced:
            metrics.warnings.append(
                f"{stage.controller_ref}: loop member(s) not placed: "
                f"{', '.join(sorted(set(unplaced)))}"
            )
        fb = _fb_node(view, stage)
        sense = _sense_return(view, stage)
        kelvin = _kelvin(view, stage)
        metrics.stages.append(StageMetrics(
            controller_ref=stage.controller_ref,
            topology=stage.topology,
            loops=loops,
            fb_node=fb,
            sense_return=sense,
            small_signal=_small_signal(view, stage, switch_refs),
            bulk_caps=_bulk_caps(view, stage),
            switch_node=_switch_node(view, stage, switch_refs),
            kelvin=kelvin,
            pad_accurate=bool(
                view.has_geometry()
                and (fb["pad_accurate"] or sense["pad_accurate"] or kelvin["pad_accurate"])
            ),
        ))

    return metrics
