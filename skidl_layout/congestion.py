from __future__ import annotations

import math
from dataclasses import dataclass, field

from .constraints import BoardOutline, KeepOut
from .power import PowerRoutePlan, plan_power_routes
from .roles import GND_NET_RE, POWER_NET_RE, classify_parts, is_nc_net
from .writer import PlacedPart


@dataclass
class CongestionRegion:
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    demand: float
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        reason = "; ".join(self.reasons[:3])
        return (
            f"({self.x_min:.1f},{self.y_min:.1f})-"
            f"({self.x_max:.1f},{self.y_max:.1f}): {self.demand:.1f}"
            + (f" [{reason}]" if reason else "")
        )


@dataclass
class CongestionMap:
    cell_size_mm: float
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    cols: int
    rows: int
    demand: list[list[float]]
    reasons: dict[tuple[int, int], list[str]] = field(default_factory=dict)

    @property
    def peak_demand(self) -> float:
        return max((max(row) for row in self.demand), default=0.0)

    @property
    def average_demand(self) -> float:
        cells = [value for row in self.demand for value in row]
        return sum(cells) / len(cells) if cells else 0.0

    def top_regions(self, limit: int = 5) -> list[CongestionRegion]:
        regions: list[CongestionRegion] = []
        for row in range(self.rows):
            for col in range(self.cols):
                value = self.demand[row][col]
                if value <= 0:
                    continue
                x_min = self.x_min + col * self.cell_size_mm
                y_min = self.y_min + row * self.cell_size_mm
                regions.append(
                    CongestionRegion(
                        x_min=x_min,
                        y_min=y_min,
                        x_max=min(self.x_max, x_min + self.cell_size_mm),
                        y_max=min(self.y_max, y_min + self.cell_size_mm),
                        demand=value,
                        reasons=self.reasons.get((row, col), []),
                    )
                )
        regions.sort(key=lambda region: (-region.demand, region.y_min, region.x_min))
        return regions[:limit]


def _net_weight(name: str) -> float:
    if GND_NET_RE.match(name):
        return 1.8
    if POWER_NET_RE.match(name):
        return 1.5
    upper = name.upper()
    if any(token in upper for token in ("USB", "D+", "D-", "CLK", "XTAL")):
        return 1.4
    return 1.0


def _outline_or_bounds(
    placed_parts: list[PlacedPart],
    outline: BoardOutline | None,
    margin_mm: float = 5.0,
) -> tuple[float, float, float, float]:
    if outline is not None and outline.vertices:
        return outline.x_min, outline.y_min, outline.x_max, outline.y_max
    if not placed_parts:
        return 0.0, 0.0, 10.0, 10.0
    return (
        min(part.x_mm for part in placed_parts) - margin_mm,
        min(part.y_mm for part in placed_parts) - margin_mm,
        max(part.x_mm for part in placed_parts) + margin_mm,
        max(part.y_mm for part in placed_parts) + margin_mm,
    )


def _make_map(
    placed_parts: list[PlacedPart],
    outline: BoardOutline | None,
    cell_size_mm: float,
) -> CongestionMap:
    x_min, y_min, x_max, y_max = _outline_or_bounds(placed_parts, outline)
    width = max(cell_size_mm, x_max - x_min)
    height = max(cell_size_mm, y_max - y_min)
    cols = max(1, math.ceil(width / cell_size_mm))
    rows = max(1, math.ceil(height / cell_size_mm))
    return CongestionMap(
        cell_size_mm=cell_size_mm,
        x_min=x_min,
        y_min=y_min,
        x_max=x_min + cols * cell_size_mm,
        y_max=y_min + rows * cell_size_mm,
        cols=cols,
        rows=rows,
        demand=[[0.0 for _ in range(cols)] for _ in range(rows)],
    )


def _cell_range(
    congestion: CongestionMap,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
) -> tuple[range, range]:
    col_min = max(0, int((x_min - congestion.x_min) // congestion.cell_size_mm))
    row_min = max(0, int((y_min - congestion.y_min) // congestion.cell_size_mm))
    col_max = min(
        congestion.cols - 1,
        int((x_max - congestion.x_min) // congestion.cell_size_mm),
    )
    row_max = min(
        congestion.rows - 1,
        int((y_max - congestion.y_min) // congestion.cell_size_mm),
    )
    return range(row_min, row_max + 1), range(col_min, col_max + 1)


def _add_demand(
    congestion: CongestionMap,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    amount: float,
    reason: str,
) -> None:
    rows, cols = _cell_range(congestion, x_min, y_min, x_max, y_max)
    for row in rows:
        for col in cols:
            congestion.demand[row][col] += amount
            reasons = congestion.reasons.setdefault((row, col), [])
            if reason not in reasons:
                reasons.append(reason)


def _net_refs(
    circuit,
    placed_by_ref: dict[str, PlacedPart],
    ctx=None,
) -> list[tuple[str, list[str]]]:
    if ctx is not None and ctx.net_ref_lists:
        # Cached topology (all nets with >=2 distinct refs). Filter to placed
        # refs per call — identical result to the live traversal below.
        result: list[tuple[str, list[str]]] = []
        for name, all_refs in ctx.net_ref_lists:
            refs = [ref for ref in all_refs if ref in placed_by_ref]
            if len(refs) >= 2:
                result.append((name, refs))
        return result
    if circuit is None:
        return []

    result = []
    for net in circuit.get_nets():
        if is_nc_net(net):
            continue
        name = str(getattr(net, "name", "") or "")
        refs = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref in placed_by_ref and ref not in refs:
                refs.append(ref)
        if len(refs) >= 2:
            result.append((name, refs))
    return result


def _pin_count(part) -> int:
    try:
        return len(part)
    except Exception:
        return len(getattr(part, "pins", []) or [])


def build_congestion_map(
    placed_parts: list[PlacedPart],
    circuit=None,
    *,
    outline: BoardOutline | None = None,
    keepouts: list[KeepOut] | None = None,
    power_plan: PowerRoutePlan | None = None,
    board_layers: int = 2,
    cell_size_mm: float = 10.0,
    ctx=None,
) -> CongestionMap:
    """Estimate routing pressure on a deterministic board grid."""
    congestion = _make_map(placed_parts, outline, cell_size_mm)
    placed_by_ref = {part.ref: part for part in placed_parts}
    layer_relief = 0.55 if board_layers >= 4 else 1.0

    for name, refs in _net_refs(circuit, placed_by_ref, ctx):
        xs = [placed_by_ref[ref].x_mm for ref in refs]
        ys = [placed_by_ref[ref].y_mm for ref in refs]
        margin = max(1.0, min(8.0, len(refs) * 0.75))
        _add_demand(
            congestion,
            min(xs) - margin,
            min(ys) - margin,
            max(xs) + margin,
            max(ys) + margin,
            _net_weight(name) * layer_relief,
            f"net {name}",
        )

    if circuit is not None:
        roles = ctx.roles if ctx is not None else classify_parts(circuit)
        pin_counts = ctx.pin_counts if ctx is not None else None
        part_by_ref = {part.ref: part for part in circuit.parts}
        for ref, part in part_by_ref.items():
            placed = placed_by_ref.get(ref)
            if placed is None:
                continue
            pins = pin_counts[ref] if pin_counts is not None and ref in pin_counts else _pin_count(part)
            role = roles.get(ref)
            role_factor = 1.4 if role is not None and role.role == "connector" else 1.0
            amount = min(6.0, pins / 8.0) * role_factor
            _add_demand(
                congestion,
                placed.x_mm - cell_size_mm / 2,
                placed.y_mm - cell_size_mm / 2,
                placed.x_mm + cell_size_mm / 2,
                placed.y_mm + cell_size_mm / 2,
                amount,
                f"pin escape {ref}",
            )

    if power_plan is None and circuit is not None:
        power_plan = plan_power_routes(
            circuit, placed_parts, board_layers=board_layers, ctx=ctx
        )
    if power_plan is not None:
        for corridor in power_plan.corridors:
            amount = (corridor.width_mm * corridor.priority / 25.0) * layer_relief
            _add_demand(
                congestion,
                corridor.x_min,
                corridor.y_min,
                corridor.x_max,
                corridor.y_max,
                amount,
                f"power {corridor.net_name}",
            )

    for keepout in keepouts or []:
        _add_demand(
            congestion,
            keepout.x_min,
            keepout.y_min,
            keepout.x_max,
            keepout.y_max,
            5.0,
            "keepout",
        )

    return congestion
