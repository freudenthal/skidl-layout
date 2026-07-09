from __future__ import annotations

import re
from dataclasses import dataclass, field

from .candidates import PlacementCandidate
from .power import PowerRoutePlan
from .routability import RoutabilityFeedback
from .scoring import LayoutScore
from .validator import ValidationResult


@dataclass
class CandidateReport:
    name: str
    score: float
    overlap_count: int = 0
    outline_violation_count: int = 0
    keepout_violation_count: int = 0
    cutout_violation_count: int = 0
    total_hpwl_mm: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": self.score,
            "overlap_count": self.overlap_count,
            "outline_violation_count": self.outline_violation_count,
            "keepout_violation_count": self.keepout_violation_count,
            "cutout_violation_count": self.cutout_violation_count,
            "total_hpwl_mm": self.total_hpwl_mm,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass
class PartExplanation:
    ref: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "violations": list(self.violations),
        }

    def summary(self) -> str:
        lines = [f"Part {self.ref}:"]
        if self.reasons:
            lines.append("  reasons: " + "; ".join(self.reasons[:6]))
        if self.violations:
            lines.append("  violations: " + "; ".join(self.violations[:6]))
        if self.warnings:
            lines.append("  warnings: " + "; ".join(self.warnings[:6]))
        if len(lines) == 1:
            lines.append("  no specific placement explanation recorded")
        return "\n".join(lines)


@dataclass
class NetExplanation:
    name: str
    hpwl_mm: float | None = None
    refs: list[str] = field(default_factory=list)
    power_corridors: list[str] = field(default_factory=list)
    congestion_regions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)

    @property
    def risk_score(self) -> float:
        return (
            (self.hpwl_mm or 0.0)
            + len(self.power_corridors) * 8.0
            + len(self.congestion_regions) * 12.0
            + len(self.warnings) * 10.0
            + len(self.risks) * 6.0
            + len(self.next_actions) * 4.0
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hpwl_mm": self.hpwl_mm,
            "refs": list(self.refs),
            "power_corridors": list(self.power_corridors),
            "congestion_regions": list(self.congestion_regions),
            "warnings": list(self.warnings),
            "risks": list(self.risks),
            "next_actions": list(self.next_actions),
            "risk_score": self.risk_score,
        }

    def summary(self) -> str:
        lines = [f"Net {self.name}:"]
        if self.refs:
            lines.append("  involved refs: " + " -> ".join(self.refs[:10]))
        if self.hpwl_mm is not None:
            lines.append(f"  estimated HPWL: {self.hpwl_mm:.1f}mm")
        if self.power_corridors:
            lines.append("  power corridors:")
            for corridor in self.power_corridors[:5]:
                lines.append(f"    {corridor}")
        if self.congestion_regions:
            lines.append("  congestion:")
            for region in self.congestion_regions[:5]:
                lines.append(f"    {region}")
        if self.risks:
            lines.append("  risks: " + "; ".join(self.risks[:6]))
        if self.next_actions:
            lines.append("  next actions: " + "; ".join(self.next_actions[:6]))
        if self.warnings:
            lines.append("  warnings: " + "; ".join(self.warnings[:6]))
        if len(lines) == 1:
            lines.append("  no specific net risk recorded")
        return "\n".join(lines)


@dataclass
class PlacementReport:
    selected: str
    candidates: list[CandidateReport] = field(default_factory=list)
    hard_violations: list[str] = field(default_factory=list)
    risky_nets: list[tuple[str, float]] = field(default_factory=list)
    congestion_regions: list[str] = field(default_factory=list)
    power_corridors: list[str] = field(default_factory=list)
    power_topology: list[str] = field(default_factory=list)
    part_reasons: dict[str, list[str]] = field(default_factory=dict)
    net_explanations: dict[str, NetExplanation] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    routability: RoutabilityFeedback | None = None

    def part(self, ref: str) -> PartExplanation:
        ref_text = str(ref)
        ref_lower = ref_text.lower()
        return PartExplanation(
            ref=ref_text,
            reasons=list(self.part_reasons.get(ref_text, [])),
            warnings=[
                warning
                for warning in self.warnings
                if ref_lower in warning.lower()
            ],
            violations=[
                violation
                for violation in self.hard_violations
                if ref_lower in violation.lower()
            ],
        )

    def net(self, name: str) -> NetExplanation:
        name_text = str(name)
        for stored_name, explanation in self.net_explanations.items():
            if stored_name.upper() == name_text.upper():
                return explanation

        explanation = NetExplanation(name=name_text)
        for risky_name, hpwl in self.risky_nets:
            if risky_name.upper() == name_text.upper():
                explanation.hpwl_mm = hpwl
                explanation.risks.append(f"long estimated route span {hpwl:.1f}mm")
                explanation.next_actions.append(
                    "move the farthest connected refs closer or reserve a cleaner path"
                )
        for region in self.congestion_regions:
            if name_text.upper() in region.upper():
                explanation.congestion_regions.append(region)
                explanation.risks.append("appears in a congestion hotspot")
                explanation.next_actions.append(
                    "spread nearby refs or clear routing space through the hotspot"
                )
        for corridor in self.power_corridors:
            if corridor.upper().startswith(name_text.upper() + ":"):
                explanation.power_corridors.append(corridor)
                explanation.risks.append("power corridor needs reserved routing space")
                explanation.next_actions.append(
                    "reserve a wide trace or plane corridor before signal routing"
                )
        return explanation

    def top_risks(self, limit: int = 10) -> list[str]:
        risks: list[tuple[float, str]] = []
        if self.routability is not None and self.routability.unrouted_count > 0:
            for net in self.routability.unrouted_nets[:10]:
                risks.append((900.0, f"unrouted net: {net}"))
        for idx, violation in enumerate(self.hard_violations):
            risks.append((1000.0 - idx, f"hard violation: {violation}"))
        for warning in self.warnings:
            risks.append((500.0, f"warning: {warning}"))
        explanations = list(self.net_explanations.values())
        if not explanations:
            explanations = [self.net(name) for name, _ in self.risky_nets]
        for explanation in explanations:
            if explanation.risk_score <= 0:
                continue
            detail = (
                explanation.next_actions[0]
                if explanation.next_actions
                else explanation.risks[0]
                if explanation.risks
                else "review placement"
            )
            risks.append(
                (
                    explanation.risk_score,
                    f"net {explanation.name}: {detail}",
                )
            )
        for region in self.congestion_regions[:5]:
            risks.append((250.0, f"congestion: {region}"))

        risks.sort(key=lambda item: (-item[0], item[1]))
        ordered: list[str] = []
        for _, text in risks:
            if text not in ordered:
                ordered.append(text)
            if len(ordered) >= limit:
                break
        return ordered

    def to_dict(self) -> dict:
        result = {
            "selected": self.selected,
            "candidates": [c.to_dict() for c in self.candidates],
            "hard_violations": list(self.hard_violations),
            "risky_nets": [
                {"name": name, "hpwl_mm": hpwl} for name, hpwl in self.risky_nets
            ],
            "congestion_regions": list(self.congestion_regions),
            "power_corridors": list(self.power_corridors),
            "power_topology": list(self.power_topology),
            "part_reasons": {
                ref: list(reasons) for ref, reasons in self.part_reasons.items()
            },
            "net_explanations": {
                name: explanation.to_dict()
                for name, explanation in self.net_explanations.items()
            },
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
            "top_risks": self.top_risks(),
        }
        if self.routability is not None:
            result["routability"] = self.routability.to_dict()
        return result

    def summary(self) -> str:
        lines = [f"Selected placement candidate: {self.selected}"]
        if self.reasons:
            lines.append("Reasons:")
            for reason in self.reasons[:10]:
                lines.append(f"  {reason}")
        if self.candidates:
            lines.append("Candidate scorecard:")
            for candidate in self.candidates[:8]:
                lines.append(
                    f"  {candidate.name}: {candidate.score:.1f}/100, "
                    f"HPWL {candidate.total_hpwl_mm:.1f}mm, "
                    f"overlaps {candidate.overlap_count}, "
                    f"outline {candidate.outline_violation_count}, "
                    f"keepout {candidate.keepout_violation_count}"
                )
        if self.hard_violations:
            lines.append("Hard violations:")
            for violation in self.hard_violations[:20]:
                lines.append(f"  {violation}")
        if self.risky_nets:
            lines.append("Top risky nets:")
            for name, hpwl in self.risky_nets[:10]:
                lines.append(f"  {name}: {hpwl:.1f}mm")
        if self.congestion_regions:
            lines.append("Top congested regions:")
            for region in self.congestion_regions[:5]:
                lines.append(f"  {region}")
        if self.power_corridors:
            lines.append("Power corridors:")
            for corridor in self.power_corridors[:10]:
                lines.append(f"  {corridor}")
        if self.power_topology:
            lines.append("Power topology:")
            for chain in self.power_topology[:10]:
                lines.append(f"  {chain}")
        if self.part_reasons:
            lines.append("Part placement reasons:")
            for ref in sorted(self.part_reasons)[:12]:
                reason_text = "; ".join(self.part_reasons[ref][:3])
                lines.append(f"  {ref}: {reason_text}")
        if self.warnings:
            lines.append("Warnings:")
            for warning in self.warnings[:20]:
                lines.append(f"  {warning}")
        return "\n".join(lines)


_NET_IN_REGION_RE = re.compile(r"\bnet\s+([^;\]\s]+)")


def _ensure_net(
    explanations: dict[str, NetExplanation],
    name: str,
) -> NetExplanation:
    for stored_name, explanation in explanations.items():
        if stored_name.upper() == name.upper():
            return explanation
    explanations[name] = NetExplanation(name=name)
    return explanations[name]


def _net_names_from_region(region: str) -> list[str]:
    names: list[str] = []
    for match in _NET_IN_REGION_RE.finditer(region):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _dedupe(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        _append_unique(unique, value)
    return unique


def _hpwl_action(name: str, hpwl: float, refs: list[str]) -> str:
    if len(refs) >= 2:
        return (
            f"bring {refs[0]} and {refs[-1]} closer on {name}, or reserve "
            "a straighter route between them"
        )
    return f"shorten or reserve a cleaner route for {name} ({hpwl:.1f}mm HPWL)"


def build_placement_report(
    selected: PlacementCandidate,
    candidate_scores: dict[str, LayoutScore],
    candidate_validations: dict[str, ValidationResult],
    power_plan: PowerRoutePlan,
    routability: RoutabilityFeedback | None = None,
    intent_warnings: list[str] | None = None,
) -> PlacementReport:
    intent_warnings = list(intent_warnings or [])
    candidate_reports: list[CandidateReport] = []
    for candidate in sorted(
        candidate_scores,
        key=lambda name: (
            1 if candidate_scores[name].ok else 0,
            candidate_scores[name].score,
        ),
        reverse=True,
    ):
        score = candidate_scores[candidate]
        validation = candidate_validations[candidate]
        candidate_reports.append(
            CandidateReport(
                name=candidate,
                score=score.score,
                overlap_count=score.overlap_count,
                outline_violation_count=score.outline_violation_count,
                keepout_violation_count=score.keepout_violation_count,
                cutout_violation_count=score.cutout_violation_count,
                total_hpwl_mm=score.total_hpwl_mm,
                reasons=(
                    list(selected.reasons[:10])
                    if candidate == selected.name
                    else []
                ),
                warnings=_dedupe([*intent_warnings, *score.warnings])[:10],
            )
        )

    selected_score = candidate_scores[selected.name]
    selected_validation = candidate_validations[selected.name]
    hard_violations = [
        *(f"overlap: {a} <-> {b}" for a, b in selected_validation.overlaps),
        *(f"outside outline: {ref}" for ref in selected_validation.outline_violations),
        *(f"inside keepout: {ref}" for ref in selected_validation.keepout_violations),
        *(
            f"intersects cutout: {ref}"
            for ref in getattr(selected_validation, "cutout_violations", []) or []
        ),
    ]
    reasons = list(selected.reasons)
    valid_count = sum(1 for s in candidate_scores.values() if s.ok)
    if valid_count > 0:
        reasons.append(
            f"best valid candidate ({valid_count} valid of "
            f"{len(candidate_scores)})"
        )
    else:
        reasons.append(
            f"best candidate of {len(candidate_scores)} (none fully valid)"
        )
    power_corridors = [
        (
            f"{corridor.net_name}: {corridor.width_mm:.2f}mm on {corridor.layer} "
            f"across {len(corridor.refs)} refs"
        )
        for corridor in power_plan.corridors
    ]
    power_topology = [
        (
            f"{chain.source_net}: "
            + " -> ".join(chain.ordered_refs[:10])
        )
        for chain in power_plan.topology.chains
    ]
    net_explanations: dict[str, NetExplanation] = {}
    for name, hpwl in selected_validation.worst_hpwl_nets:
        explanation = _ensure_net(net_explanations, name)
        explanation.hpwl_mm = hpwl
        refs = selected_validation.worst_hpwl_refs.get(name, [])
        for ref in refs:
            _append_unique(explanation.refs, ref)
        _append_unique(explanation.risks, f"long estimated route span {hpwl:.1f}mm")
        _append_unique(explanation.next_actions, _hpwl_action(name, hpwl, refs))
    for corridor in power_plan.corridors:
        name = corridor.net_name
        explanation = _ensure_net(net_explanations, name)
        corridor_text = (
            f"{corridor.net_name}: {corridor.width_mm:.2f}mm on {corridor.layer} "
            f"across {len(corridor.refs)} refs"
        )
        _append_unique(explanation.power_corridors, corridor_text)
        for ref in corridor.refs:
            _append_unique(explanation.refs, ref)
        _append_unique(explanation.risks, "power corridor needs reserved routing space")
        _append_unique(
            explanation.next_actions,
            "reserve a wide trace or plane corridor before signal routing",
        )
    for region in selected_score.congestion_regions[:5]:
        for name in _net_names_from_region(region):
            explanation = _ensure_net(net_explanations, name)
            _append_unique(explanation.congestion_regions, region)
            _append_unique(explanation.risks, "appears in a congestion hotspot")
            _append_unique(
                explanation.next_actions,
                "spread nearby refs or clear routing space through the hotspot",
            )

    return PlacementReport(
        selected=selected.name,
        candidates=candidate_reports,
        hard_violations=hard_violations,
        risky_nets=list(selected_validation.worst_hpwl_nets),
        congestion_regions=list(selected_score.congestion_regions[:5]),
        power_corridors=power_corridors,
        power_topology=power_topology,
        part_reasons=dict(selected.ref_reasons),
        net_explanations=net_explanations,
        warnings=_dedupe(
            [
                *intent_warnings,
                *selected_score.warnings[:20],
                *power_plan.warnings[:20],
            ]
        ),
        reasons=reasons,
        routability=routability,
    )
