from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoutabilityFeedback:
    """Structured feedback from external routing.

    Populated by KiCadRoutingTools (route.py + check_connected.py +
    check_drc.py) via ``skidl_layout.krt.evaluate_routability`` /
    ``route_and_check``.
    """

    unrouted_count: int = 0
    total_nets: int = 0
    unrouted_nets: list[str] = field(default_factory=list)
    drc_violation_count: int = 0
    track_count: int = 0
    via_count: int = 0
    source: str = ""

    @property
    def completion_pct(self) -> float:
        if self.total_nets <= 0:
            return 100.0
        return max(0.0, (1.0 - self.unrouted_count / self.total_nets) * 100.0)

    def summary(self) -> str:
        lines = [f"Routability ({self.source or 'unknown'}):"]
        lines.append(
            f"  {self.total_nets - self.unrouted_count}/{self.total_nets} "
            f"nets routed ({self.completion_pct:.1f}%)"
        )
        if self.unrouted_count:
            lines.append(f"  Unrouted: {self.unrouted_count}")
            for net in self.unrouted_nets[:10]:
                lines.append(f"    {net}")
        if self.drc_violation_count:
            lines.append(f"  DRC violations: {self.drc_violation_count}")
        if self.track_count:
            lines.append(f"  Tracks: {self.track_count}, Vias: {self.via_count}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "unrouted_count": self.unrouted_count,
            "total_nets": self.total_nets,
            "unrouted_nets": list(self.unrouted_nets),
            "drc_violation_count": self.drc_violation_count,
            "track_count": self.track_count,
            "via_count": self.via_count,
            "source": self.source,
            "completion_pct": self.completion_pct,
        }
