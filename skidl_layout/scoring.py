from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .congestion import build_congestion_map
from .decaps import measure_decap_pad_distances
from .geometry import FootprintGeometry
from .grid import points_form_clean_grid
from .power import plan_power_routes
from .roles import (
    GND_NET_RE,
    POWER_NET_RE,
    PartRole,
    classify_parts,
    is_ui_grid_part,
    pin_net_names,
)
from .validator import validate
from .writer import PlacedPart


@dataclass
class LayoutScore:
    score: float
    total_hpwl_mm: float = 0.0
    overlap_count: int = 0
    outline_violation_count: int = 0
    keepout_violation_count: int = 0
    cutout_violation_count: int = 0
    missing_count: int = 0
    warning_count: int = 0
    weighted_hpwl_mm: float = 0.0
    crossing_count: int = 0
    congestion_score: float = 0.0
    power_corridor_count: int = 0
    role_counts: dict[str, int] = field(default_factory=dict)
    power_net_count: int = 0
    congestion_regions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    footprint_envelope_bbox_mm: dict[str, float] = field(default_factory=dict)
    footprint_envelope_area_ratio: float = 0.0
    compact_outline_mm: dict[str, float] = field(default_factory=dict)
    compact_outline_area_ratio: float = 0.0
    empty_margin_ratios: dict[str, float] = field(default_factory=dict)
    max_empty_margin_ratio: float = 0.0
    front_panel_trace_count: int = 0
    front_panel_trace_mm: float = 0.0

    @property
    def ok(self) -> bool:
        return (
            self.overlap_count == 0
            and self.outline_violation_count == 0
            and self.keepout_violation_count == 0
            and self.cutout_violation_count == 0
            and self.missing_count == 0
        )

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "total_hpwl_mm": self.total_hpwl_mm,
            "overlap_count": self.overlap_count,
            "outline_violation_count": self.outline_violation_count,
            "keepout_violation_count": self.keepout_violation_count,
            "cutout_violation_count": self.cutout_violation_count,
            "missing_count": self.missing_count,
            "warning_count": self.warning_count,
            "weighted_hpwl_mm": self.weighted_hpwl_mm,
            "crossing_count": self.crossing_count,
            "congestion_score": self.congestion_score,
            "power_corridor_count": self.power_corridor_count,
            "role_counts": dict(self.role_counts),
            "power_net_count": self.power_net_count,
            "congestion_regions": list(self.congestion_regions),
            "warnings": list(self.warnings),
            "footprint_envelope_bbox_mm": dict(self.footprint_envelope_bbox_mm),
            "footprint_envelope_area_ratio": self.footprint_envelope_area_ratio,
            "compact_outline_mm": dict(self.compact_outline_mm),
            "compact_outline_area_ratio": self.compact_outline_area_ratio,
            "empty_margin_ratios": dict(self.empty_margin_ratios),
            "max_empty_margin_ratio": self.max_empty_margin_ratio,
            "front_panel_trace_count": self.front_panel_trace_count,
            "front_panel_trace_mm": self.front_panel_trace_mm,
            "ok": self.ok,
        }

    def summary(self) -> str:
        lines = [f"Layout score: {self.score:.1f}/100"]
        lines.append(f"Total HPWL: {self.total_hpwl_mm:.1f}mm")
        if self.overlap_count:
            lines.append(f"Overlaps: {self.overlap_count}")
        if self.outline_violation_count:
            lines.append(f"Outside outline: {self.outline_violation_count}")
        if self.keepout_violation_count:
            lines.append(f"Inside keepout: {self.keepout_violation_count}")
        if self.cutout_violation_count:
            lines.append(f"Intersects cutout: {self.cutout_violation_count}")
        if self.missing_count:
            lines.append(f"Missing placements: {self.missing_count}")
        if self.crossing_count:
            lines.append(f"Estimated crossings: {self.crossing_count}")
        if self.congestion_score:
            lines.append(f"Pin escape congestion: {self.congestion_score:.1f}")
        if self.congestion_regions:
            lines.append("Top congested regions:")
            for region in self.congestion_regions[:5]:
                lines.append(f"  {region}")
        if self.power_corridor_count:
            lines.append(f"Power corridors: {self.power_corridor_count}")
        if self.front_panel_trace_count:
            lines.append(
                f"Visible front-panel trace spans: {self.front_panel_trace_count} "
                f"({self.front_panel_trace_mm:.1f}mm)"
            )
        if self.compact_outline_mm and self.compact_outline_area_ratio:
            lines.append(
                "Compact outline estimate: "
                f"{self.compact_outline_mm.get('width', 0.0):.1f}mm x "
                f"{self.compact_outline_mm.get('height', 0.0):.1f}mm "
                f"({self.compact_outline_area_ratio * 100:.0f}% of outline)"
            )
        if self.warnings:
            lines.append("Warnings:")
            for warning in self.warnings[:20]:
                lines.append(f"  {warning}")
        return "\n".join(lines)


def _distance(a: PlacedPart, b: PlacedPart) -> float:
    return math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm)


def _total_hpwl(placed_parts: list[PlacedPart], circuit) -> float:
    if circuit is None:
        return 0.0

    try:
        from skidl.net import NCNet
    except Exception:
        NCNet = None

    pos_by_ref = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed_parts}
    total = 0.0
    for net in circuit.get_nets():
        if NCNet is not None and isinstance(net, NCNet):
            continue
        xs, ys = [], []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref in pos_by_ref:
                x, y = pos_by_ref[ref]
                xs.append(x)
                ys.append(y)
        if len(xs) >= 2:
            total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _net_weight(name: str) -> float:
    if GND_NET_RE.match(name):
        return 2.0
    if POWER_NET_RE.match(name):
        return 1.6
    if any(token in name.upper() for token in ("USB", "D+", "D-", "CLK", "XTAL")):
        return 1.5
    return 1.0


_PRIMARY_OWNER_ROLES = {"ic", "regulator", "module_socket"}


def _is_supply_or_ground_net(net_name: str) -> bool:
    return bool(POWER_NET_RE.match(net_name) or GND_NET_RE.match(net_name))


def _role_weight(role_name: str) -> float:
    return {
        "regulator": 6.0,
        "module_socket": 5.5,
        "ic": 5.0,
    }.get(role_name, 1.0)


def _alpha_tokens(text: str) -> set[str]:
    generic_tokens = {
        "CAP",
        "CAPACITOR",
        "DEVICE",
        "FOOTPRINT",
        "IC",
        "LGA",
        "MCU",
        "METRIC",
        "MODULE",
        "PACKAGE",
        "PKG",
        "RESISTOR",
        "SENSOR",
        "SMD",
    }
    tokens: set[str] = set()
    for match in re.finditer(r"[A-Za-z]{3,}", str(text or "")):
        token = match.group(0).upper()
        if token not in generic_tokens:
            tokens.add(token)
        if token[0] in {"C", "R", "L", "D", "U", "J", "Q"} and len(token) >= 4:
            stripped = token[1:]
            if stripped not in generic_tokens:
                tokens.add(stripped)
    return tokens


def _part_tokens(part) -> set[str]:
    tokens: set[str] = set()
    for field_name in ("ref", "name", "value", "footprint"):
        tokens.update(_alpha_tokens(str(getattr(part, field_name, "") or "")))
    return tokens


def _token_affinity(passive_part, owner_part) -> int:
    passive_tokens = _part_tokens(passive_part)
    owner_tokens = _part_tokens(owner_part)
    if not passive_tokens or not owner_tokens:
        return 0
    best = 0
    for passive_token in passive_tokens:
        for owner_token in owner_tokens:
            if passive_token == owner_token:
                best = max(best, 3)
            elif passive_token in owner_token or owner_token in passive_token:
                best = max(best, 2)
    return best


def _select_primary_owner_ref(
    ref: str,
    part_by_ref: dict,
    nets_by_ref: dict[str, set[str]],
    roles: dict[str, PartRole],
    placed_by_ref: dict[str, PlacedPart],
    *,
    require_signal: bool,
    require_power_and_ground: bool = False,
) -> str | None:
    part = part_by_ref.get(ref)
    placed = placed_by_ref.get(ref)
    if part is None or placed is None:
        return None
    passive_nets = nets_by_ref.get(ref, set())
    signal_nets = {
        net_name
        for net_name in passive_nets
        if not _is_supply_or_ground_net(net_name)
    }
    candidates = []
    for other_ref, other_role in roles.items():
        if other_ref == ref or other_ref not in placed_by_ref:
            continue
        role_name = other_role.role if other_role is not None else "unknown"
        if role_name not in _PRIMARY_OWNER_ROLES:
            continue
        shared = passive_nets & nets_by_ref.get(other_ref, set())
        if not shared:
            continue
        shared_signal = {
            net_name
            for net_name in shared
            if not _is_supply_or_ground_net(net_name)
        }
        if require_signal and signal_nets and not shared_signal:
            continue
        if require_power_and_ground:
            if not any(POWER_NET_RE.match(net_name) for net_name in shared):
                continue
            if not any(GND_NET_RE.match(net_name) for net_name in shared):
                continue
        other = placed_by_ref[other_ref]
        distance = math.hypot(placed.x_mm - other.x_mm, placed.y_mm - other.y_mm)
        if require_power_and_ground:
            candidates.append(
                (
                    -_token_affinity(part, part_by_ref.get(other_ref)),
                    distance,
                    -_role_weight(role_name),
                    other_ref,
                )
            )
        else:
            candidates.append(
                (
                    -_token_affinity(part, part_by_ref.get(other_ref)),
                    -len(shared_signal),
                    -_role_weight(role_name),
                    distance,
                    other_ref,
                )
            )
    if not candidates:
        return None
    return min(candidates)[-1]


def _weighted_hpwl(placed_parts: list[PlacedPart], circuit) -> float:
    if circuit is None:
        return 0.0
    try:
        from skidl.net import NCNet
    except Exception:
        NCNet = None

    pos_by_ref = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed_parts}
    total = 0.0
    for net in circuit.get_nets():
        if NCNet is not None and isinstance(net, NCNet):
            continue
        xs, ys = [], []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref in pos_by_ref:
                x, y = pos_by_ref[ref]
                xs.append(x)
                ys.append(y)
        if len(xs) >= 2:
            hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
            total += hpwl * _net_weight(str(getattr(net, "name", "") or ""))
    return total


def _segment_intersects(a1, a2, b1, b2) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1 = orient(a1, a2, b1)
    o2 = orient(a1, a2, b2)
    o3 = orient(b1, b2, a1)
    o4 = orient(b1, b2, a2)
    return o1 * o2 < 0 and o3 * o4 < 0


def _estimate_crossings(placed_parts: list[PlacedPart], circuit) -> int:
    if circuit is None:
        return 0
    try:
        from skidl.net import NCNet
    except Exception:
        NCNet = None

    pos_by_ref = {pp.ref: (pp.x_mm, pp.y_mm) for pp in placed_parts}
    segments = []
    for net in circuit.get_nets():
        if NCNet is not None and isinstance(net, NCNet):
            continue
        refs = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref in pos_by_ref and ref not in refs:
                refs.append(ref)
        if len(refs) < 2:
            continue
        anchor = min(refs, key=lambda ref: (pos_by_ref[ref][0], pos_by_ref[ref][1], ref))
        for ref in refs:
            if ref != anchor:
                segments.append((anchor, ref, pos_by_ref[anchor], pos_by_ref[ref]))

    crossings = 0
    for idx, (a_ref, b_ref, a1, a2) in enumerate(segments):
        for c_ref, d_ref, b1, b2 in segments[idx + 1:]:
            if {a_ref, b_ref}.intersection({c_ref, d_ref}):
                continue
            if _segment_intersects(a1, a2, b1, b2):
                crossings += 1
    return crossings


def _pin_escape_congestion(placed_parts: list[PlacedPart], circuit) -> float:
    if circuit is None:
        return 0.0
    placed = {pp.ref: pp for pp in placed_parts}
    part_by_ref = {part.ref: part for part in circuit.parts if part.ref in placed}
    congestion = 0.0
    refs = sorted(part_by_ref)
    for i, ref in enumerate(refs):
        a = placed[ref]
        try:
            a_pins = len(part_by_ref[ref])
        except Exception:
            a_pins = 2
        for other_ref in refs[i + 1:]:
            b = placed[other_ref]
            dist = max(_distance(a, b), 0.1)
            if dist > 12.0:
                continue
            try:
                b_pins = len(part_by_ref[other_ref])
            except Exception:
                b_pins = 2
            congestion += (a_pins + b_pins) / dist
    return congestion


def _edge_distance(pp: PlacedPart, fp_bboxes, outline) -> float:
    w, h = fp_bboxes.get(pp.footprint, (2.0, 2.0))
    if pp.rot_deg % 180 == 90:
        w, h = h, w
    return min(
        abs(pp.x_mm - w / 2 - outline.x_min),
        abs(outline.x_max - (pp.x_mm + w / 2)),
        abs(pp.y_mm - h / 2 - outline.y_min),
        abs(outline.y_max - (pp.y_mm + h / 2)),
    )


def _placement_envelope(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    margin_mm: float = 3.0,
) -> tuple[float, float, float] | None:
    bounds = _placement_bounds(placed_parts, fp_bboxes)
    if bounds is None:
        return None

    x_min, y_min, x_max, y_max = bounds
    width = max(0.0, x_max - x_min + 2 * margin_mm)
    height = max(0.0, y_max - y_min + 2 * margin_mm)
    return width, height, width * height


def _placement_bounds(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if len(placed_parts) < 2:
        return None

    x_min = float("inf")
    y_min = float("inf")
    x_max = float("-inf")
    y_max = float("-inf")
    for pp in placed_parts:
        w, h = fp_bboxes.get(pp.footprint, (2.0, 2.0))
        if pp.rot_deg % 180 == 90:
            w, h = h, w
        x_min = min(x_min, pp.x_mm - w / 2)
        y_min = min(y_min, pp.y_mm - h / 2)
        x_max = max(x_max, pp.x_mm + w / 2)
        y_max = max(y_max, pp.y_mm + h / 2)
    if not all(math.isfinite(value) for value in (x_min, y_min, x_max, y_max)):
        return None
    return x_min, y_min, x_max, y_max


def _outline_utilization_metrics(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
    *,
    compact_margin_mm: float = 3.0,
) -> dict:
    if outline is None:
        return {}
    bounds = _placement_bounds(placed_parts, fp_bboxes)
    if bounds is None:
        return {}

    x_min, y_min, x_max, y_max = bounds
    body_w = max(0.0, x_max - x_min)
    body_h = max(0.0, y_max - y_min)
    body_area = body_w * body_h
    compact_w = body_w + 2 * compact_margin_mm
    compact_h = body_h + 2 * compact_margin_mm
    compact_area = compact_w * compact_h
    outline_area = max(0.0, outline.width_mm) * max(0.0, outline.height_mm)
    if outline_area <= 0.0:
        return {}

    margin_ratios = {
        "left": max(0.0, x_min - outline.x_min) / max(outline.width_mm, 0.001),
        "right": max(0.0, outline.x_max - x_max) / max(outline.width_mm, 0.001),
        "top": max(0.0, y_min - outline.y_min) / max(outline.height_mm, 0.001),
        "bottom": max(0.0, outline.y_max - y_max) / max(outline.height_mm, 0.001),
    }
    return {
        "footprint_envelope_bbox_mm": {
            "width": body_w,
            "height": body_h,
            "area": body_area,
        },
        "footprint_envelope_area_ratio": min(body_area / outline_area, 1.0),
        "compact_outline_mm": {
            "width": compact_w,
            "height": compact_h,
            "area": compact_area,
        },
        "compact_outline_area_ratio": min(compact_area / outline_area, 1.0),
        "empty_margin_ratios": margin_ratios,
        "max_empty_margin_ratio": max(margin_ratios.values()),
    }


def _outline_oversize_warning(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
) -> str | None:
    if outline is None:
        return None
    metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    compact = metrics.get("compact_outline_mm", {})
    envelope_area = float(compact.get("area", 0.0) or 0.0)
    if not metrics or envelope_area <= 0.0:
        return None
    envelope_w = float(compact.get("width", 0.0) or 0.0)
    envelope_h = float(compact.get("height", 0.0) or 0.0)
    outline_area = max(0.0, outline.width_mm) * max(0.0, outline.height_mm)
    if outline_area <= 0.0:
        return None

    area_ratio = outline_area / envelope_area
    width_slack = outline.width_mm - envelope_w
    height_slack = outline.height_mm - envelope_h
    if area_ratio < 2.5 or width_slack < 10.0 or height_slack < 8.0:
        return None

    return (
        f"board outline is {area_ratio:.1f}x larger than compact footprint "
        f"envelope (estimated compact outline {envelope_w:.1f}x{envelope_h:.1f}mm); "
        "shrink auto-sized boards or redistribute parts if this outline is mechanically fixed"
    )


def _outline_oversize_penalty(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
) -> float:
    """Return a score penalty for sparse placements on generous outlines."""
    if outline is None:
        return 0.0
    if len(placed_parts) < 4:
        return 0.0
    metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    compact = metrics.get("compact_outline_mm", {})
    envelope_area = float(compact.get("area", 0.0) or 0.0)
    if not metrics or envelope_area <= 0.0:
        return 0.0
    envelope_w = float(compact.get("width", 0.0) or 0.0)
    envelope_h = float(compact.get("height", 0.0) or 0.0)
    outline_area = max(0.0, outline.width_mm) * max(0.0, outline.height_mm)
    if outline_area <= 0.0:
        return 0.0

    area_ratio = outline_area / envelope_area
    width_slack = max(0.0, outline.width_mm - envelope_w)
    height_slack = max(0.0, outline.height_mm - envelope_h)
    if area_ratio < 2.0 or width_slack < 8.0 or height_slack < 6.0:
        return 0.0

    ratio_penalty = (area_ratio - 2.0) * 4.0
    slack_penalty = max(0.0, width_slack - 8.0) / 6.0
    slack_penalty += max(0.0, height_slack - 6.0) / 6.0
    return min(ratio_penalty + slack_penalty, 28.0)


def _role_counts(roles: dict[str, PartRole]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for role in roles.values():
        counts[role.role] = counts.get(role.role, 0) + 1
    return counts


def _front_panel_trace_metrics(
    placed_parts: list[PlacedPart],
    circuit,
    roles: dict[str, PartRole],
    *,
    long_span_mm: float = 28.0,
) -> dict:
    if circuit is None:
        return {"count": 0, "span_mm": 0.0, "warnings": []}

    placed_by_ref = {pp.ref: pp for pp in placed_parts}
    has_panel_context = any(
        role.role == "panel_jack" and ref in placed_by_ref
        for ref, role in roles.items()
    )
    has_back_side_service = any(
        str(getattr(pp, "side", "front") or "front").lower() == "back"
        for pp in placed_parts
    )
    if not (has_panel_context or has_back_side_service):
        return {"count": 0, "span_mm": 0.0, "warnings": []}

    front_panel_refs = {
        ref
        for ref, role in roles.items()
        if role.role in {"panel_jack", "control"}
        and ref in placed_by_ref
        and str(getattr(placed_by_ref[ref], "side", "front") or "front").lower()
        == "front"
    }
    if not front_panel_refs:
        return {"count": 0, "span_mm": 0.0, "warnings": []}

    try:
        from skidl.net import NCNet
    except Exception:
        NCNet = None

    count = 0
    total_span = 0.0
    warnings: list[str] = []
    for net in circuit.get_nets():
        if NCNet is not None and isinstance(net, NCNet):
            continue
        refs: list[str] = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref in placed_by_ref and ref not in refs:
                refs.append(ref)
        panel_refs = [ref for ref in refs if ref in front_panel_refs]
        if not panel_refs:
            continue
        front_non_panel_refs = [
            ref
            for ref in refs
            if ref not in front_panel_refs
            and str(getattr(placed_by_ref[ref], "side", "front") or "front").lower()
            == "front"
        ]
        if not front_non_panel_refs:
            continue

        span = max(
            _distance(placed_by_ref[panel_ref], placed_by_ref[other_ref])
            for panel_ref in panel_refs
            for other_ref in front_non_panel_refs
        )
        if span < long_span_mm:
            continue
        count += 1
        total_span += span
        warnings.append(
            f"{getattr(net, 'name', 'net')}: front-panel trace span is "
            f"{span:.1f}mm; move service electronics to the back or route away "
            "from the control face"
        )

    return {"count": count, "span_mm": total_span, "warnings": warnings}


def _role_warnings(
    placed_parts: list[PlacedPart],
    circuit,
    roles: dict[str, PartRole],
    fp_bboxes: dict[str, tuple[float, float]],
    outline=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> list[str]:
    placed_by_ref = {pp.ref: pp for pp in placed_parts}
    warnings: list[str] = []

    if outline is not None:
        oversize_warning = _outline_oversize_warning(
            placed_parts, fp_bboxes, outline
        )
        if oversize_warning:
            warnings.append(oversize_warning)
        for ref, role in roles.items():
            if role.role != "connector" or ref not in placed_by_ref:
                continue
            distance = _edge_distance(placed_by_ref[ref], fp_bboxes, outline)
            if distance > 5.0:
                warnings.append(
                    f"{ref}: connector is {distance:.1f}mm from nearest board edge"
                )

    if circuit is None:
        return warnings

    part_by_ref = {part.ref: part for part in circuit.parts}
    nets_by_ref = {ref: set(pin_net_names(part)) for ref, part in part_by_ref.items()}
    decap_pad_distances = measure_decap_pad_distances(
        placed_parts,
        circuit,
        fp_geometries or {},
    )

    if outline is not None:
        panel_like_count = sum(
            1 for role in roles.values() if role.role in {"panel_jack", "control"}
        )
        primary_refs = [
            ref
            for ref, role in roles.items()
            if role.role in {"ic", "regulator"} and ref in placed_by_ref
        ]
        if (
            len(primary_refs) == 1
            and panel_like_count < 2
            and len(part_by_ref) <= 16
        ):
            primary = placed_by_ref[primary_refs[0]]
            center_x = outline.x_min + outline.width_mm / 2.0
            center_y = outline.y_min + outline.height_mm / 2.0
            distance = math.hypot(primary.x_mm - center_x, primary.y_mm - center_y)
            limit = max(5.0, min(outline.width_mm, outline.height_mm) * 0.18)
            if distance > limit:
                warnings.append(
                    f"{primary.ref}: primary IC/regulator is {distance:.1f}mm from board center"
                )

    parent_roles = {"ic", "regulator", "module_socket"}
    for ref, role in roles.items():
        if role.role != "decoupling_cap" or ref not in placed_by_ref:
            continue
        cap_nets = nets_by_ref.get(ref, set())
        candidates = [
            other_ref
            for other_ref, other_role in roles.items()
            if other_ref in placed_by_ref
            and other_role.role in parent_roles
            and cap_nets.intersection(nets_by_ref.get(other_ref, set()))
        ]
        if not candidates:
            warnings.append(f"{ref}: no placed IC/regulator shares its supply nets")
            continue
        pad_distance = decap_pad_distances.get(ref)
        if pad_distance is not None:
            if pad_distance.average_pad_distance_mm > 6.0:
                warnings.append(
                    f"{ref}: decoupling cap pads average "
                    f"{pad_distance.average_pad_distance_mm:.1f}mm from "
                    f"{pad_distance.parent_ref} supply pads"
                )
            continue
        owner_ref = _select_primary_owner_ref(
            ref,
            part_by_ref,
            nets_by_ref,
            roles,
            placed_by_ref,
            require_signal=False,
            require_power_and_ground=True,
        )
        nearest_ref = owner_ref or min(
            candidates,
            key=lambda other_ref: _distance(
                placed_by_ref[ref], placed_by_ref[other_ref]
            ),
        )
        distance = _distance(placed_by_ref[ref], placed_by_ref[nearest_ref])
        if distance > 5.0:
            warnings.append(
                f"{ref}: decoupling cap is {distance:.1f}mm from {nearest_ref}"
            )

    for ref, role in roles.items():
        if role.role != "signal_passive" or ref not in placed_by_ref:
            continue
        passive_nets = nets_by_ref.get(ref, set())
        signal_nets = {
            name
            for name in passive_nets
            if not POWER_NET_RE.match(name) and not GND_NET_RE.match(name)
        }
        if not signal_nets:
            continue
        candidates = [
            other_ref
            for other_ref, other_role in roles.items()
            if other_ref in placed_by_ref
            and other_role.role in parent_roles
            and signal_nets.intersection(nets_by_ref.get(other_ref, set()))
        ]
        if not candidates:
            continue
        owner_ref = _select_primary_owner_ref(
            ref,
            part_by_ref,
            nets_by_ref,
            roles,
            placed_by_ref,
            require_signal=True,
        )
        nearest_ref = owner_ref or min(
            candidates,
            key=lambda other_ref: _distance(
                placed_by_ref[ref], placed_by_ref[other_ref]
            ),
        )
        distance = _distance(placed_by_ref[ref], placed_by_ref[nearest_ref])
        if distance > 12.0:
            warnings.append(
                f"{ref}: signal passive is {distance:.1f}mm from {nearest_ref}"
            )

    for ref, role in roles.items():
        if role.role != "crystal" or ref not in placed_by_ref:
            continue
        ic_refs = [
            other_ref
            for other_ref, other_role in roles.items()
            if other_ref in placed_by_ref and other_role.role == "ic"
        ]
        if not ic_refs:
            continue
        nearest_ref = min(
            ic_refs,
            key=lambda other_ref: _distance(
                placed_by_ref[ref], placed_by_ref[other_ref]
            ),
        )
        distance = _distance(placed_by_ref[ref], placed_by_ref[nearest_ref])
        if distance > 10.0:
            warnings.append(
                f"{ref}: crystal is {distance:.1f}mm from nearest IC {nearest_ref}"
            )

    grid_refs = [
        ref
        for ref, role in roles.items()
        if ref in placed_by_ref
        and (
            role.role in {"panel_jack", "control"}
            or is_ui_grid_part(part_by_ref.get(ref))
        )
    ]
    if len(grid_refs) >= 2:
        xs = [placed_by_ref[ref].x_mm for ref in grid_refs]
        ys = [placed_by_ref[ref].y_mm for ref in grid_refs]
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)
        clean_grid = points_form_clean_grid(
            [
                (placed_by_ref[ref].x_mm, placed_by_ref[ref].y_mm)
                for ref in grid_refs
            ]
        )
        tall_panel = (
            outline is not None
            and outline.height_mm >= outline.width_mm * 1.6
            and outline.height_mm >= 60.0
        )
        if tall_panel:
            expected_y_span = min(55.0, outline.height_mm * 0.45)
            if not clean_grid and x_span > max(12.0, outline.width_mm * 0.45):
                warnings.append(
                    "visible/mechanical subjects are not aligned into clean columns"
                )
            if y_span < expected_y_span:
                warnings.append(
                    "visible/mechanical subjects are bunched instead of distributed vertically"
                )
        else:
            expected_x_span = min(20.0, outline.width_mm * 0.35) if outline else 12.0
            if len(grid_refs) <= 4 and y_span > 2.0 and not clean_grid:
                warnings.append(
                    "visible/mechanical subjects are not aligned into a clean row"
                )
            if x_span < expected_x_span:
                warnings.append(
                    "visible/mechanical subjects are bunched instead of distributed"
                )

    return warnings


def _warning_penalty(warnings: list[str]) -> float:
    penalty = min(len(warnings) * 5.0, 25.0)
    if any("bunched instead of distributed" in warning for warning in warnings):
        penalty += 18.0
    return penalty


def score_placement_quick(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    outline=None,
    keepouts=None,
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    clearance_mm: float = 0.5,
    ctx=None,
) -> LayoutScore:
    """Cheap scorer for candidates with known violations.

    Runs only validate + HPWL + penalty. Skips congestion, crossings, and
    power corridor analysis.
    """
    validation = validate(
        placed_parts,
        circuit,
        fp_bboxes,
        clearance_mm=clearance_mm,
        outline=outline,
        keepouts=keepouts,
        cutouts=cutouts,
        fp_geometries=fp_geometries,
    )
    roles = ctx.roles if ctx is not None else (classify_parts(circuit) if circuit is not None else {})
    warnings = _role_warnings(
        placed_parts,
        circuit,
        roles,
        fp_bboxes,
        outline,
        fp_geometries=fp_geometries,
    )
    front_panel_trace = _front_panel_trace_metrics(placed_parts, circuit, roles)
    warnings.extend(front_panel_trace["warnings"])
    outline_metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    total_hpwl = _total_hpwl(placed_parts, circuit)

    penalty = 0.0
    penalty += len(validation.overlaps) * 25.0
    penalty += len(validation.outline_violations) * 20.0
    penalty += len(validation.keepout_violations) * 25.0
    penalty += len(validation.cutout_violations) * 30.0
    penalty += len(validation.missing_refs) * 10.0
    penalty += min(total_hpwl / 50.0, 30.0)
    penalty += min(float(front_panel_trace["span_mm"]) / 12.0, 12.0)
    penalty += _warning_penalty(warnings)
    penalty += _outline_oversize_penalty(placed_parts, fp_bboxes, outline)

    return LayoutScore(
        score=max(0.0, 100.0 - penalty),
        total_hpwl_mm=total_hpwl,
        overlap_count=len(validation.overlaps),
        outline_violation_count=len(validation.outline_violations),
        keepout_violation_count=len(validation.keepout_violations),
        cutout_violation_count=len(validation.cutout_violations),
        missing_count=len(validation.missing_refs),
        warning_count=len(warnings),
        role_counts=_role_counts(roles),
        warnings=warnings,
        footprint_envelope_bbox_mm=dict(outline_metrics.get("footprint_envelope_bbox_mm", {})),
        footprint_envelope_area_ratio=float(outline_metrics.get("footprint_envelope_area_ratio", 0.0) or 0.0),
        compact_outline_mm=dict(outline_metrics.get("compact_outline_mm", {})),
        compact_outline_area_ratio=float(outline_metrics.get("compact_outline_area_ratio", 0.0) or 0.0),
        empty_margin_ratios=dict(outline_metrics.get("empty_margin_ratios", {})),
        max_empty_margin_ratio=float(outline_metrics.get("max_empty_margin_ratio", 0.0) or 0.0),
        front_panel_trace_count=int(front_panel_trace["count"]),
        front_panel_trace_mm=float(front_panel_trace["span_mm"]),
    )


def score_placement(
    placed_parts: list[PlacedPart],
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    outline=None,
    keepouts=None,
    cutouts=None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
    clearance_mm: float = 0.5,
    board_layers: int = 2,
    ctx=None,
) -> LayoutScore:
    validation = validate(
        placed_parts,
        circuit,
        fp_bboxes,
        clearance_mm=clearance_mm,
        outline=outline,
        keepouts=keepouts,
        cutouts=cutouts,
        fp_geometries=fp_geometries,
    )
    roles = ctx.roles if ctx is not None else (classify_parts(circuit) if circuit is not None else {})
    warnings = _role_warnings(
        placed_parts,
        circuit,
        roles,
        fp_bboxes,
        outline,
        fp_geometries=fp_geometries,
    )
    front_panel_trace = _front_panel_trace_metrics(placed_parts, circuit, roles)
    warnings.extend(front_panel_trace["warnings"])
    power_plan = None
    if circuit is not None:
        power_plan = plan_power_routes(circuit, placed_parts, board_layers=board_layers)
        warnings.extend(power_plan.warnings)
    outline_metrics = _outline_utilization_metrics(placed_parts, fp_bboxes, outline)
    total_hpwl = _total_hpwl(placed_parts, circuit)
    weighted_hpwl = _weighted_hpwl(placed_parts, circuit)
    crossing_count = _estimate_crossings(placed_parts, circuit)
    pin_escape_score = _pin_escape_congestion(placed_parts, circuit)
    congestion_map = build_congestion_map(
        placed_parts,
        circuit,
        outline=outline,
        keepouts=keepouts,
        power_plan=power_plan,
        board_layers=board_layers,
    )
    congestion_score = (
        pin_escape_score
        + congestion_map.peak_demand
        + congestion_map.average_demand * 0.5
    )
    congestion_regions = [
        region.label for region in congestion_map.top_regions(limit=5)
    ]

    penalty = 0.0
    penalty += len(validation.overlaps) * 25.0
    penalty += len(validation.outline_violations) * 20.0
    penalty += len(validation.keepout_violations) * 25.0
    penalty += len(validation.cutout_violations) * 30.0
    penalty += len(validation.missing_refs) * 10.0
    penalty += min(total_hpwl / 50.0, 30.0)
    penalty += min(weighted_hpwl / 120.0, 20.0)
    penalty += min(crossing_count * 2.0, 20.0)
    penalty += min(congestion_score / 8.0, 15.0)
    penalty += min(float(front_panel_trace["span_mm"]) / 12.0, 12.0)
    penalty += _warning_penalty(warnings)
    penalty += _outline_oversize_penalty(placed_parts, fp_bboxes, outline)
    if power_plan is not None:
        for intent in power_plan.route_intents:
            if intent.width_mm >= 0.8 and intent.span_mm > 50.0:
                layer_relief = 0.45 if board_layers >= 4 else 1.0
                penalty += min((intent.span_mm - 50.0) / 10.0, 10.0) * layer_relief

    return LayoutScore(
        score=max(0.0, 100.0 - penalty),
        total_hpwl_mm=total_hpwl,
        overlap_count=len(validation.overlaps),
        outline_violation_count=len(validation.outline_violations),
        keepout_violation_count=len(validation.keepout_violations),
        cutout_violation_count=len(validation.cutout_violations),
        missing_count=len(validation.missing_refs),
        warning_count=len(warnings),
        weighted_hpwl_mm=weighted_hpwl,
        crossing_count=crossing_count,
        congestion_score=congestion_score,
        role_counts=_role_counts(roles),
        power_net_count=len(power_plan.nets) if power_plan is not None else 0,
        congestion_regions=congestion_regions,
        power_corridor_count=(
            len(power_plan.corridors) if power_plan is not None else 0
        ),
        warnings=warnings,
        footprint_envelope_bbox_mm=dict(outline_metrics.get("footprint_envelope_bbox_mm", {})),
        footprint_envelope_area_ratio=float(outline_metrics.get("footprint_envelope_area_ratio", 0.0) or 0.0),
        compact_outline_mm=dict(outline_metrics.get("compact_outline_mm", {})),
        compact_outline_area_ratio=float(outline_metrics.get("compact_outline_area_ratio", 0.0) or 0.0),
        empty_margin_ratios=dict(outline_metrics.get("empty_margin_ratios", {})),
        max_empty_margin_ratio=float(outline_metrics.get("max_empty_margin_ratio", 0.0) or 0.0),
        front_panel_trace_count=int(front_panel_trace["count"]),
        front_panel_trace_mm=float(front_panel_trace["span_mm"]),
    )
