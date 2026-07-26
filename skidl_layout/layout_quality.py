"""Board-level layout-quality taxonomy -- report-only (WS-H2c).

Adapted from ``lachlanfysh/skidl@feat/overnight-product-layer``
(``mcp_server/layout_quality.py``, MIT). What is harvested is the **taxonomy**:
a stable set of issue codes split into *blocking* and *advisory*, each issue
carrying ``{code, severity, message, evidence, recommendation}``, plus his
congestion / empty-margin / compactness thresholds. What is *not* harvested is
his plumbing -- his version reads an MCP response payload full of pydantic
``DesignException`` objects and run artifacts; ours reads the
:class:`~skidl_layout.LayoutResult` we already produce, so the port is a
classifier over data the scorer already computes, not new analysis.

Why a taxonomy on top of a score: :class:`~skidl_layout.scoring.LayoutScore`
answers *"how good is this board, 0-100"*, which is the wrong question when the
board is broken. This answers *"is anything wrong, what, and what do I do about
it"* -- so an overlap and a large empty margin stop being two numbers on the same
axis.

**Report-only, exactly like** :func:`~skidl_layout.fab_check`: this never
mutates a placement, never raises on a rule breach, and nothing calls it
automatically. ``.ok`` is the caller's to act on.

    quality = layout_quality(plan_layout(circuit, ...))
    if not quality.ok:
        print(quality.summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ADVISORY_CODES",
    "BLOCKING_CODES",
    "LayoutQualityResult",
    "QualityIssue",
    "layout_quality",
]


# --------------------------------------------------------------------------
# The taxonomy
# --------------------------------------------------------------------------

#: Codes that mean the board is not ready to be treated as finished. Harvested
#: verbatim from his ``PRODUCT_BLOCKING_EXCEPTION_CODES``, minus the ones that
#: describe *his* pipeline's own failures (CODE_EXEC_ERROR, ENGINE_CRASH,
#: ENGINE_TIMEOUT, POST_ARTIFACT_FAILURE, ...) rather than a board.
BLOCKING_CODES = frozenset({
    "DRC_CLEARANCE",
    "FOOTPRINT_MISSING",
    "LAYOUT_CUTOUT",
    "LAYOUT_KEEPOUT",
    "LAYOUT_MISSING_REF",
    "LAYOUT_OUTLINE_VIOLATION",
    "LAYOUT_OVERLAP",
    "ROUTE_UNCONNECTED",
})

#: Codes that are placement *feedback* -- a board can ship with these.
#: The ``BULK_CAP_DISTANCE`` / ``FB_NODE_LENGTH`` / ``HOT_LOOP_*`` /
#: ``KELVIN_TAP_TARGET`` / ``SENSE_RETURN_LENGTH`` / ``SW_NODE_SPAN`` family is
#: power-layout Phase 2 and reads :attr:`LayoutResult.power_metrics`; **all of it
#: is advisory**, so a board with a mediocre hot loop still ships and
#: ``LayoutQualityResult.ok`` is unchanged for every existing board.
ADVISORY_CODES = frozenset({
    "BULK_CAP_DISTANCE",
    "FB_NODE_LENGTH",
    "FRONT_PANEL_TRACE_SPAN",
    "HIGH_CONGESTION",
    "HOT_LOOP_AREA",
    "HOT_LOOP_SELF_INTERSECTING",
    "KELVIN_TAP_TARGET",
    "LOW_PART_SPREAD",
    "OUTLINE_UNDERUSED",
    "ROUTE_BROKEN_NET",
    "SENSE_RETURN_LENGTH",
    "SW_NODE_SPAN",
    "UNUSED_OUTLINE_REGION",
})

# --- thresholds ------------------------------------------------------------
# The ratio thresholds are his values unchanged, and they ARE comparable: his
# ``spread_area_ratio`` / ``compact_outline_area_ratio`` / ``max_margin_ratio``
# are computed the same way as our ``footprint_envelope_area_ratio`` /
# ``compact_outline_area_ratio`` / ``max_empty_margin_ratio`` (occupied area or
# margin over outline area).
LOW_SPREAD_RATIO = 0.25
UNDERUSED_OUTLINE_RATIO = 0.45
LARGE_MARGIN_RATIO = 0.35

#: Default congestion threshold, **on this engine's scale and NORMALIZED per
#: part**. Calibrated 2026-07-24 against the 10-board ``scripts/bench_layout.py``
#: population, all of which place cleanly:
#:
#:     board             parts   raw    per-part
#:     sipm_tia              5   22.2      4.44
#:     ads1115              12   29.7      2.48
#:     bme280               16   74.3      4.64
#:     trinket              18   90.3      5.02
#:     esp32c3              32  117.0      3.66
#:     stm32_bluepill       32  190.0      5.94
#:     funcgen              62  396.3      6.39
#:     qtpy_samd21          36  249.7      6.94
#:     feather_nrf52840     42  379.5      9.04
#:     feather_rp2040       49  460.7      9.40
#:
#: The harvested default was 80.0 on *his* RAW scale; ours reads 22-461 RAW,
#: tracking board SIZE almost perfectly (r ~ part count), so a raw threshold is a
#: "big board" flag with no congestion signal -- at 80.0 it fired on 8 of these
#: 10 clean boards. Dividing by part count flattens that: every clean board here
#: reads 2.48-9.40 per part regardless of size. The default sits just above that
#: clean-population max so HIGH_CONGESTION is a quiet advisory tail flag -- it
#: stays silent on anything as good as our worst clean board and only fires on a
#: placement denser-per-part than any we have shipped. Re-derive from a fresh
#: ``bench_layout.py --json`` if the placer's density changes. Pass ``None`` to
#: disable the check entirely.
HIGH_CONGESTION_PER_PART_THRESHOLD = 12.0

#: Back-compat alias for the constant's old name (still the documented knob name
#: in older callers); the value is now the normalized per-part trip point.
HIGH_CONGESTION_THRESHOLD = HIGH_CONGESTION_PER_PART_THRESHOLD

# --- power-layout thresholds (Phase 2, WS-6) -------------------------------
# CALIBRATED 2026-07-26 against the ONLY placements in the corpus that yield a
# power stage. **That is four placements of two netlists** -- honestly thin, and
# said so here so the next reader knows how much to trust these numbers. The
# variant-ranking oracle is what the scorer term is actually gated on; these
# trip points are a convenience over the numbers, not the numbers themselves.
#
#   placement                              loose  perim  bowtie  FB_pad  sense  SWspan  bulk  kelvin
#   lt3757_boost B  (hand floorplan)        0.82   0.99    no      2.40   7.50   16.62  15.13  COUT1
#   avalanche_laser_driver (default placer) 0.60   1.11    no      7.37  15.30   21.17  44.85  R9
#   lt3757_boost A  (default placer)        1.71   1.69   YES     13.17  14.79   15.21   7.61  J2
#   lt3757_boost A' (default, B's outline)  4.59   2.25    no     10.88  26.40   16.00   9.02  J2
#
# lt3757_boost B is the ground truth: it is the LT3757 datasheet Figure-11
# floorplan transcribed by hand, it beats A/A' 2-5x on every placement-driven
# requirement, and it is the only one of the three that routes to completion.
# A and A' are the known-bad controls. Each trip point below sits just outside
# B, so the code is a quiet tail flag rather than a "this is a power board"
# flag. Pass ``None`` to any of them to disable that check.

#: ``HOT_LOOP_AREA``, on the loop's **normalized** looseness -- effective area
#: over the area of a square whose perimeter is the tightest these parts admit
#: (see ``power_metrics.LoopGeometry``). Raw mm2 is NOT usable as a threshold:
#: hot-loop area on a 30 x 24 mm board and on a 100 x 80 mm board are not the
#: same quantity, which is the ``HIGH_CONGESTION`` lesson above repeated.
#: 1.20 clears B (0.82) and avalanche (0.60) and fires on A (1.71) and A' (4.59).
HOT_LOOP_LOOSENESS_THRESHOLD = 1.20

#: ``FB_NODE_LENGTH``, in **mm** -- divider top to the controller's FB pad. A
#: genuinely absolute quantity: a feedback node does not get to be longer
#: because the board is bigger. 8.0 clears B (2.40) and fires on A (13.17) and
#: A' (10.88); avalanche sits just under at 7.37.
FB_NODE_LENGTH_MM_THRESHOLD = 8.0

#: ``SENSE_RETURN_LENGTH``, in **mm** -- sense resistor to the switch it
#: measures. Absolute for the same reason. 10.0 clears B (7.50) and fires on
#: A (14.79), A' (26.40) and avalanche (15.30).
SENSE_RETURN_MM_THRESHOLD = 10.0

#: Small-signal separation **floor**, in mm -- the one number in this family
#: where BIGGER IS BETTER. No quality code carries it (the Phase 2 plan's code
#: list does not include one); it exists because the opt-in scorer term reads
#: it, and because putting it anywhere but beside its own calibration is how
#: thresholds drift. 14.0 clears B (16.62) and fires on A (8.95), A' (11.15)
#: and avalanche (7.90).
SMALL_SIGNAL_SEPARATION_FLOOR_MM = 14.0

#: ``SW_NODE_SPAN`` -- **DISABLED (None), deliberately.** MEASURED: the span of
#: the switch-node parts orders the corpus A 15.21 < A' 16.00 < B 16.62, i.e.
#: the hand floorplan is the *worst*, while the routed SW copper area it is
#: supposed to proxy for orders B 4.65 < A 5.75 < A' 7.53 -- exactly inverted.
#: Phase 2 plan bail-out 4 applies: the number ships, the code does not. The
#: honest measurement is ``SW_NODE_COPPER_AREA`` on a routed board (Phase 5).
#: Set a float to enable it anyway.
SW_NODE_SPAN_MM_THRESHOLD = None

#: ``BULK_CAP_DISTANCE`` -- **DISABLED (None), deliberately.** Same bail-out.
#: MEASURED: A 7.61 < A' 9.02 < B 15.13, again inverting the known ranking. The
#: cause is structural rather than a bad number -- on a boost the commutation
#: loop is output-side, so the *input* capacitor's distance to it encodes no
#: design intent, and it is the input cap that dominates this metric on all
#: three variants. A defensible version needs a per-rail loop, which is Phase 3
#: work. Set a float to enable it anyway.
BULK_CAP_DISTANCE_MM_THRESHOLD = None

# The spread/margin/compactness family is meaningless on a tiny board, so his
# code gates the whole family behind a minimum size. Same gate here.
MIN_PARTS_FOR_SPREAD_CHECKS = 5
MIN_AREA_MM2_FOR_SPREAD_CHECKS = 1000.0

_ERROR = "error"
_WARNING = "warning"


@dataclass(frozen=True)
class QualityIssue:
    """One typed finding. ``severity`` is ``"error"`` or ``"warning"``."""

    code: str
    severity: str
    message: str
    evidence: dict = field(default_factory=dict)
    recommendation: str = ""

    @property
    def blocking(self) -> bool:
        return self.code in BLOCKING_CODES

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "evidence": dict(self.evidence),
            "recommendation": self.recommendation,
        }

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.code}: {self.message}"


@dataclass
class LayoutQualityResult:
    """Outcome of :func:`layout_quality` -- report-only; callers decide to fail."""

    issues: list = field(default_factory=list)

    @property
    def blocking(self) -> list:
        return [i for i in self.issues if i.blocking]

    @property
    def advisory(self) -> list:
        return [i for i in self.issues if not i.blocking]

    @property
    def ok(self) -> bool:
        """True when nothing blocking was found. Advisories do not fail a board."""
        return not self.blocking

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "blocking_count": len(self.blocking),
            "advisory_count": len(self.advisory),
        }

    def summary(self) -> str:
        lines = [
            f"Layout quality: {'OK' if self.ok else 'BLOCKED'} "
            f"({len(self.blocking)} blocking, {len(self.advisory)} advisory)"
        ]
        for issue in self.issues:
            lines.append(f"  {issue}")
            if issue.recommendation:
                lines.append(f"      -> {issue.recommendation}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _outline_area_mm2(outline) -> float:
    if outline is None or not getattr(outline, "vertices", None):
        return 0.0
    width = outline.x_max - outline.x_min
    height = outline.y_max - outline.y_min
    return max(0.0, width) * max(0.0, height)


def _power_issues(
    result,
    hot_loop_looseness_threshold,
    fb_node_threshold_mm,
    sense_return_threshold_mm,
    sw_node_span_threshold_mm,
    bulk_cap_threshold_mm,
) -> list:
    """Power-layout advisories read off ``result.power_metrics`` (Phase 2).

    ``getattr`` rather than an attribute access so a ``LayoutResult`` built by an
    older caller, or a hand-rolled stand-in in a test, keeps working.
    """
    metrics = getattr(result, "power_metrics", None)
    stages = list(getattr(metrics, "stages", None) or [])
    if not stages:
        return []

    issues: list = []
    for stage in stages:
        who = stage.controller_ref or "power stage"
        for loop in stage.loops:
            members = " -> ".join(loop.member_refs)
            if loop.self_intersecting:
                issues.append(QualityIssue(
                    "HOT_LOOP_SELF_INTERSECTING", _WARNING,
                    f"{who}: the commutation loop {members} traces a bow tie",
                    evidence={"controller": stage.controller_ref,
                              "member_refs": list(loop.member_refs),
                              "shoelace_area_mm2": loop.area_mm2,
                              "convex_hull_area_mm2": loop.convex_hull_area_mm2,
                              "effective_area_mm2": loop.effective_area_mm2},
                    recommendation=(
                        f"Reorder the physical placement of {members} to follow "
                        "the current path -- a crossed loop encloses far more "
                        "area than its raw shoelace figure suggests."
                    ),
                ))
            if (hot_loop_looseness_threshold is not None
                    and loop.looseness_ratio is not None
                    and loop.looseness_ratio >= hot_loop_looseness_threshold):
                issues.append(QualityIssue(
                    "HOT_LOOP_AREA", _WARNING,
                    f"{who}: the commutation loop {members} encloses "
                    f"{loop.looseness_ratio:.2f}x the area these parts admit",
                    evidence={"controller": stage.controller_ref,
                              "member_refs": list(loop.member_refs),
                              "effective_area_mm2": loop.effective_area_mm2,
                              "perimeter_mm": loop.perimeter_mm,
                              "longest_edge_mm": loop.longest_edge_mm,
                              "min_perimeter_mm": loop.min_perimeter_mm,
                              "looseness_ratio": loop.looseness_ratio,
                              "threshold": hot_loop_looseness_threshold},
                    recommendation=(
                        f"Pull {members} together; the longest hop is "
                        f"{_longest_edge_label(loop)}. Loop inductance goes with "
                        "enclosed area, so this is the highest-value move on the "
                        "board."
                    ),
                ))

        fb = stage.fb_node or {}
        fb_mm = fb.get("fb_top_to_fb_pad_mm")
        if (fb_node_threshold_mm is not None and fb_mm is not None
                and fb_mm >= fb_node_threshold_mm):
            issues.append(QualityIssue(
                "FB_NODE_LENGTH", _WARNING,
                f"{who}: the feedback node runs {fb_mm:.1f}mm from "
                f"{fb.get('fb_top')} to the FB pin",
                evidence={"controller": stage.controller_ref,
                          "fb_top": fb.get("fb_top"),
                          "fb_bottom": fb.get("fb_bottom"),
                          "feedback_net": fb.get("feedback_net"),
                          "fb_top_to_fb_pad_mm": fb_mm,
                          "fb_top_to_fb_bottom_mm": fb.get("fb_top_to_fb_bottom_mm"),
                          "threshold_mm": fb_node_threshold_mm},
                recommendation=(
                    f"Move {fb.get('fb_top')} and {fb.get('fb_bottom')} against "
                    f"{stage.controller_ref}'s FB pin -- this node is high "
                    "impedance and picks up whatever the switch node radiates."
                ),
            ))

        sense = stage.sense_return or {}
        sense_mm = sense.get("sense_r_to_switch_mm")
        if (sense_return_threshold_mm is not None and sense_mm is not None
                and sense_mm >= sense_return_threshold_mm):
            issues.append(QualityIssue(
                "SENSE_RETURN_LENGTH", _WARNING,
                f"{who}: the sense resistor {sense.get('sense_resistor_ref')} "
                f"sits {sense_mm:.1f}mm from the switch it measures",
                evidence={"controller": stage.controller_ref,
                          "sense_resistor_ref": sense.get("sense_resistor_ref"),
                          "sense_r_to_switch_mm": sense_mm,
                          "sense_r_to_controller_gnd_pad_mm":
                              sense.get("sense_r_to_controller_gnd_pad_mm"),
                          "sense_r_to_controller_sense_pad_mm":
                              sense.get("sense_r_to_controller_sense_pad_mm"),
                          "threshold_mm": sense_return_threshold_mm},
                recommendation=(
                    f"Put {sense.get('sense_resistor_ref')} directly at the "
                    "switch's source/emitter and take the sense line back to the "
                    "controller as a pair -- this loop carries the full switch "
                    "current."
                ),
            ))

        kelvin = stage.kelvin or {}
        if kelvin.get("taps_output_cap") is False:
            issues.append(QualityIssue(
                "KELVIN_TAP_TARGET", _WARNING,
                f"{who}: the feedback divider {kelvin.get('fb_top')} taps "
                f"{kelvin.get('neighbour_ref')}, not an output capacitor",
                evidence={"controller": stage.controller_ref,
                          "fb_top": kelvin.get("fb_top"),
                          "output_rail": kelvin.get("output_rail"),
                          "neighbour_ref": kelvin.get("neighbour_ref"),
                          "neighbour_mm": kelvin.get("neighbour_mm"),
                          "nearest_output_cap_ref":
                              kelvin.get("nearest_output_cap_ref"),
                          "nearest_output_cap_mm":
                              kelvin.get("nearest_output_cap_mm")},
                recommendation=(
                    f"Move {kelvin.get('fb_top')} beside "
                    f"{kelvin.get('nearest_output_cap_ref') or 'an output cap'}: "
                    "the rail routes as a Euclidean MST, so the nearest part is "
                    "the one it actually senses, and sensing at the rectifier "
                    "puts the divider on the high-dV/dt end of the net."
                ),
            ))

        span = (stage.switch_node or {}).get("span_mm")
        if (sw_node_span_threshold_mm is not None and span is not None
                and span >= sw_node_span_threshold_mm):
            issues.append(QualityIssue(
                "SW_NODE_SPAN", _WARNING,
                f"{who}: the switch node spans {span:.1f}mm",
                evidence={"controller": stage.controller_ref,
                          "switch_node": stage.switch_node,
                          "threshold_mm": sw_node_span_threshold_mm},
                recommendation=(
                    "Tighten the switch, magnetics and rectifier around one "
                    "another; switch-node copper is the board's main radiator."
                ),
            ))

        bulk = stage.bulk_caps or {}
        bulk_mm = bulk.get("max_mm")
        if (bulk_cap_threshold_mm is not None and bulk_mm is not None
                and bulk_mm >= bulk_cap_threshold_mm):
            issues.append(QualityIssue(
                "BULK_CAP_DISTANCE", _WARNING,
                f"{who}: {bulk.get('worst_ref')} sits {bulk_mm:.1f}mm from the "
                "commutation loop",
                evidence={"controller": stage.controller_ref,
                          "worst_ref": bulk.get("worst_ref"),
                          "max_mm": bulk_mm,
                          "distances_mm": bulk.get("distances_mm"),
                          "threshold_mm": bulk_cap_threshold_mm},
                recommendation=(
                    f"Bring {bulk.get('worst_ref')} in toward the loop it "
                    "supplies."
                ),
            ))

    for warning in list(getattr(metrics, "warnings", None) or []):
        issues.append(QualityIssue(
            "HOT_LOOP_AREA", _WARNING,
            f"power metrics incomplete: {warning}",
            evidence={"warning": warning},
            recommendation="Check that every loop member was placed.",
        ))
    return issues


def _longest_edge_label(loop) -> str:
    if not loop.edges_mm:
        return "unknown"
    key = max(loop.edges_mm, key=lambda k: (loop.edges_mm[k], k))
    return f"{key} at {loop.edges_mm[key]:.1f}mm"


def layout_quality(
    result,
    route_summary=None,
    congestion_threshold=HIGH_CONGESTION_THRESHOLD,
    hot_loop_looseness_threshold=HOT_LOOP_LOOSENESS_THRESHOLD,
    fb_node_threshold_mm=FB_NODE_LENGTH_MM_THRESHOLD,
    sense_return_threshold_mm=SENSE_RETURN_MM_THRESHOLD,
    sw_node_span_threshold_mm=SW_NODE_SPAN_MM_THRESHOLD,
    bulk_cap_threshold_mm=BULK_CAP_DISTANCE_MM_THRESHOLD,
) -> LayoutQualityResult:
    """Classify a :class:`~skidl_layout.LayoutResult` into typed quality issues.

    Args:
        result: a ``LayoutResult`` from :func:`~skidl_layout.plan_layout`.
        route_summary: optionally, the ``plane_summary`` dict from
            :func:`~skidl_layout.emit_power_copper` (or the equivalent keys
            ``unrouted_nets`` / ``broken_nets`` / ``drc_violation_count``), so
            routing outcomes join the same taxonomy. ``None`` -> placement-only.
        congestion_threshold: the ``HIGH_CONGESTION`` trip point, compared
            against congestion **per placed part**. Defaults to
            :data:`HIGH_CONGESTION_PER_PART_THRESHOLD`, calibrated on this
            engine's scale from the 10-board benchmark population (see that
            constant). Pass ``None`` to skip the congestion check.
        hot_loop_looseness_threshold: ``HOT_LOOP_AREA`` trip point, on the
            **normalized** looseness ratio. See
            :data:`HOT_LOOP_LOOSENESS_THRESHOLD`.
        fb_node_threshold_mm: ``FB_NODE_LENGTH`` trip point in mm.
        sense_return_threshold_mm: ``SENSE_RETURN_LENGTH`` trip point in mm.
        sw_node_span_threshold_mm: ``SW_NODE_SPAN`` trip point in mm.
            **Defaults to ``None`` (off)** -- the metric was measured to invert
            the known ranking; see :data:`SW_NODE_SPAN_MM_THRESHOLD`.
        bulk_cap_threshold_mm: ``BULK_CAP_DISTANCE`` trip point in mm.
            **Defaults to ``None`` (off)**, same reason; see
            :data:`BULK_CAP_DISTANCE_MM_THRESHOLD`.

    Returns:
        A :class:`LayoutQualityResult`. Never raises on a rule breach. The
        power-layout codes are all advisory, so ``.ok`` is unaffected by them.
    """
    issues: list = []
    validation = getattr(result, "validation", None)
    score = getattr(result, "score", None)

    # -- placement correctness: every one of these is blocking ---------------
    hard = [
        ("overlaps", "LAYOUT_OVERLAP",
         "{n} footprint overlap(s) remain in placement",
         "Resolve placement before routing or growing the outline; move "
         "non-mechanical parts away from locked edge/mounting constraints."),
        ("outline_violations", "LAYOUT_OUTLINE_VIOLATION",
         "{n} part(s) sit outside the board outline",
         "Treat this as a placement/floorplan problem first, unless the "
         "outline really was specified too small."),
        ("keepout_violations", "LAYOUT_KEEPOUT",
         "{n} part(s) violate keepout geometry",
         "Move parts clear of mounting / mechanical / no-place regions."),
        ("cutout_violations", "LAYOUT_CUTOUT",
         "{n} part(s) intersect board cutout geometry",
         "Preserve physical cutouts and move parts clear of void geometry."),
        ("missing_refs", "LAYOUT_MISSING_REF",
         "{n} part(s) were not placed",
         "Usually a missing/unresolvable footprint -- fix it before treating "
         "the board as finished."),
    ]
    for attr, code, template, recommendation in hard:
        items = list(getattr(validation, attr, None) or [])
        if not items:
            continue
        issues.append(QualityIssue(
            code, _ERROR, template.format(n=len(items)),
            evidence={"count": len(items), "items": [str(i) for i in items[:20]]},
            recommendation=recommendation,
        ))

    # -- routing outcomes, when the caller ran the copper stage --------------
    plane = route_summary or {}
    unrouted = list(plane.get("unrouted_nets") or [])
    broken = list(plane.get("broken_nets") or [])
    drc = int(_num(plane.get("drc_violation_count")))
    if unrouted:
        issues.append(QualityIssue(
            "ROUTE_UNCONNECTED", _ERROR,
            f"{len(unrouted)} net(s) carry no copper at all",
            evidence={"count": len(unrouted), "nets": unrouted[:20]},
            recommendation=(
                "Re-place before growing the outline: an unroutable net is "
                "usually a placement problem, not a router problem."
            ),
        ))
    if broken:
        issues.append(QualityIssue(
            "ROUTE_BROKEN_NET", _WARNING,
            f"{len(broken)} net(s) are routed but not fully connected",
            evidence={"count": len(broken), "nets": broken[:20]},
            recommendation=(
                "Check plane coverage first -- a net poured into a zone often "
                "reports as broken when the zone fill is left for KiCad."
            ),
        ))
    if drc:
        issues.append(QualityIssue(
            "DRC_CLEARANCE", _ERROR,
            f"{drc} DRC violation(s) on the routed board",
            evidence={"drc_violation_count": drc},
            recommendation=(
                "Route at the fab's design rules; for a fine-pitch part also "
                "pin KRT's neck-down floor via write_krt_fab_overrides()."
            ),
        ))

    # -- power layout (Phase 2). Independent of `score`, so it runs before the
    # score-less early return: a result carrying power_metrics still gets them.
    issues.extend(_power_issues(
        result,
        hot_loop_looseness_threshold,
        fb_node_threshold_mm,
        sense_return_threshold_mm,
        sw_node_span_threshold_mm,
        bulk_cap_threshold_mm,
    ))

    if score is None:
        return LayoutQualityResult(issues=issues)

    # -- congestion (normalized per part; see the threshold constant) -------
    congestion = _num(getattr(score, "congestion_score", 0.0))
    n_placed = len(getattr(result, "placed_parts", None) or [])
    congestion_per_part = congestion / n_placed if n_placed else 0.0
    routed_clean = bool(plane) and not unrouted and not drc
    if (congestion_threshold is not None
            and congestion_per_part >= congestion_threshold and not routed_clean):
        regions = list(getattr(score, "congestion_regions", None) or [])
        issues.append(QualityIssue(
            "HIGH_CONGESTION", _WARNING,
            f"layout congestion is high ({congestion_per_part:.1f} per part)",
            evidence={"congestion_score": round(congestion, 1),
                      "congestion_per_part": round(congestion_per_part, 2),
                      "part_count": n_placed,
                      "regions": regions[:5]},
            recommendation=(
                "Placement feedback, not a board size problem: try group "
                "movement and connector orientation before growing the outline."
            ),
        ))

    # -- long visible front-panel spans -------------------------------------
    fp_traces = int(_num(getattr(score, "front_panel_trace_count", 0)))
    if fp_traces:
        issues.append(QualityIssue(
            "FRONT_PANEL_TRACE_SPAN", _WARNING,
            f"{fp_traces} long visible front-panel trace span(s)",
            evidence={"front_panel_trace_count": fp_traces,
                      "front_panel_trace_mm":
                          round(_num(getattr(score, "front_panel_trace_mm", 0.0)), 1)},
            recommendation=(
                "Move the user-facing parts toward their drivers so the visible "
                "span shortens."
            ),
        ))

    # -- outline utilisation (only meaningful above his size gate) ----------
    # MEASURED CAVEAT: on a plan_layout placement these three effectively never
    # fire, because our placer *spreads to fill whatever outline it is given*.
    # qtpy_samd21 (36 parts) at 30x34 / 60x60 / 90x90 / 140x140 mm holds an
    # envelope ratio of 1.00 / 0.88 / 0.84 / 0.82 and a max margin of
    # 0.02 / 0.10 / 0.11 / 0.11 -- never near the 0.25 / 0.45 / 0.35 trip points.
    # His placer clustered and left dead area; ours does not. They are kept
    # because layout_quality also accepts a result built from an existing board
    # (a human floorplan CAN leave a board half empty), and because dropping a
    # taxonomy code is harder than leaving one quiet.
    part_count = len(getattr(result, "placed_parts", None) or [])
    area = _outline_area_mm2(getattr(result, "outline", None))
    if part_count < MIN_PARTS_FOR_SPREAD_CHECKS or area < MIN_AREA_MM2_FOR_SPREAD_CHECKS:
        return LayoutQualityResult(issues=issues)

    compact_ratio = _num(getattr(score, "compact_outline_area_ratio", 0.0))
    if compact_ratio and compact_ratio < UNDERUSED_OUTLINE_RATIO:
        issues.append(QualityIssue(
            "OUTLINE_UNDERUSED", _WARNING,
            "the outline is much larger than the occupied footprint envelope",
            evidence={
                "compact_outline_area_ratio": round(compact_ratio, 3),
                "compact_outline_mm": dict(
                    getattr(score, "compact_outline_mm", None) or {}),
                "board_area_mm2": round(area, 1),
            },
            recommendation=(
                "If the outline is not mechanically fixed, shrink it toward the "
                "compact envelope; if it is fixed, use the area deliberately."
            ),
        ))

    envelope_ratio = _num(getattr(score, "footprint_envelope_area_ratio", 0.0))
    if envelope_ratio and envelope_ratio < LOW_SPREAD_RATIO:
        issues.append(QualityIssue(
            "LOW_PART_SPREAD", _WARNING,
            "parts occupy a small fraction of the board outline",
            evidence={"footprint_envelope_area_ratio": round(envelope_ratio, 3),
                      "board_area_mm2": round(area, 1)},
            recommendation=(
                "Shrink the outline, or redistribute user-facing parts across "
                "the meaningful area before routing."
            ),
        ))

    max_margin = _num(getattr(score, "max_empty_margin_ratio", 0.0))
    if max_margin >= LARGE_MARGIN_RATIO:
        issues.append(QualityIssue(
            "UNUSED_OUTLINE_REGION", _WARNING,
            "one or more board margins are very large relative to the outline",
            evidence={"max_empty_margin_ratio": round(max_margin, 3),
                      "empty_margin_ratios": dict(
                          getattr(score, "empty_margin_ratios", None) or {})},
            recommendation=(
                "Review whether the board should compact. On a fixed outline, "
                "use the empty region deliberately instead of growing further."
            ),
        ))

    return LayoutQualityResult(issues=issues)
