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
ADVISORY_CODES = frozenset({
    "FRONT_PANEL_TRACE_SPAN",
    "HIGH_CONGESTION",
    "LOW_PART_SPREAD",
    "OUTLINE_UNDERUSED",
    "ROUTE_BROKEN_NET",
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


def layout_quality(
    result,
    route_summary=None,
    congestion_threshold=HIGH_CONGESTION_THRESHOLD,
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

    Returns:
        A :class:`LayoutQualityResult`. Never raises on a rule breach.
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
