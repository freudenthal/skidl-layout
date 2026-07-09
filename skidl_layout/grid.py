from __future__ import annotations

from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class AxisCluster:
    center: float
    indices: tuple[int, ...]


def choose_grid_columns(
    count: int,
    width_mm: float,
    height_mm: float,
    *,
    max_columns: int | None = None,
) -> int:
    """Choose a deterministic column count for a repeated human-facing grid."""
    if count <= 0:
        return 0
    if count <= 2:
        return count

    width = max(float(width_mm or 0.0), 1.0)
    height = max(float(height_mm or 0.0), 1.0)
    aspect = width / height
    limit = min(count, max_columns or count)
    best_columns = 1
    best_key: tuple[float, int, int] | None = None
    for columns in range(1, limit + 1):
        rows = ceil(count / columns)
        empty_cells = columns * rows - count
        aspect_error = abs((columns / rows) - aspect)
        raggedness = empty_cells / max(1, columns * rows)
        score = aspect_error + raggedness * 1.5
        if count >= 5 and rows == 1:
            score += 1.0
        if columns == 1 and aspect >= 0.9:
            score += 0.4
        key = (score, empty_cells, columns)
        if best_key is None or key < best_key:
            best_key = key
            best_columns = columns
    return best_columns


def grid_rows_for_refs(
    refs: list[str],
    width_mm: float,
    height_mm: float,
    *,
    single_row_limit: int = 4,
    max_columns: int | None = None,
) -> list[list[str]]:
    """Split ordered refs into balanced rows for a repeated UI/sensor grid."""
    ordered = list(refs)
    count = len(ordered)
    if count == 0:
        return []
    if count <= max(1, single_row_limit):
        return [ordered]

    columns = choose_grid_columns(
        count,
        width_mm,
        height_mm,
        max_columns=max_columns,
    )
    if columns <= 1:
        return [[ref] for ref in ordered]

    row_count = ceil(count / columns)
    base = count // row_count
    extra = count % row_count
    rows: list[list[str]] = []
    cursor = 0
    for row_idx in range(row_count):
        row_size = base + (1 if row_idx < extra else 0)
        rows.append(ordered[cursor : cursor + row_size])
        cursor += row_size
    return [row for row in rows if row]


def cluster_axis(values: list[float], tolerance_mm: float = 2.0) -> list[AxisCluster]:
    """Cluster nearly-equal axis values while preserving original indices."""
    if not values:
        return []

    ordered = sorted(
        enumerate(float(value) for value in values),
        key=lambda item: item[1],
    )
    clusters: list[list[tuple[int, float]]] = [[ordered[0]]]
    for idx, value in ordered[1:]:
        current = clusters[-1]
        center = sum(item[1] for item in current) / len(current)
        if abs(value - center) <= tolerance_mm:
            current.append((idx, value))
        else:
            clusters.append([(idx, value)])

    result: list[AxisCluster] = []
    for cluster in clusters:
        center = sum(value for _, value in cluster) / len(cluster)
        result.append(
            AxisCluster(
                center=center,
                indices=tuple(sorted(idx for idx, _ in cluster)),
            )
        )
    return result


def points_form_clean_grid(
    points: list[tuple[float, float]],
    *,
    tolerance_mm: float = 2.0,
) -> bool:
    """Return true when all points participate in a visible row/column grid."""
    if len(points) < 2:
        return True

    x_clusters = cluster_axis([point[0] for point in points], tolerance_mm)
    y_clusters = cluster_axis([point[1] for point in points], tolerance_mm)
    repeated_x = {
        idx
        for cluster in x_clusters
        if len(cluster.indices) >= 2
        for idx in cluster.indices
    }
    repeated_y = {
        idx
        for cluster in y_clusters
        if len(cluster.indices) >= 2
        for idx in cluster.indices
    }
    return len(repeated_x | repeated_y) == len(points)
