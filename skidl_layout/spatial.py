from __future__ import annotations

import math


class SpatialGrid:
    """Grid index for axis-aligned bounding box overlap queries."""

    def __init__(self, cell_size_mm: float = 10.0):
        self._cell = max(cell_size_mm, 1.0)
        self._cells: dict[tuple[int, int], list[tuple[str, float, float, float, float]]] = {}
        self._entries: dict[str, tuple[float, float, float, float]] = {}

    def _cell_range(self, x, y, w, h, clearance=0.0):
        x_min = x - w / 2 - clearance
        y_min = y - h / 2 - clearance
        x_max = x + w / 2 + clearance
        y_max = y + h / 2 + clearance
        c_x_min = int(math.floor(x_min / self._cell))
        c_y_min = int(math.floor(y_min / self._cell))
        c_x_max = int(math.floor(x_max / self._cell))
        c_y_max = int(math.floor(y_max / self._cell))
        return c_x_min, c_y_min, c_x_max, c_y_max

    def insert(self, key: str, x: float, y: float, w: float, h: float):
        self._entries[key] = (x, y, w, h)
        cx0, cy0, cx1, cy1 = self._cell_range(x, y, w, h)
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                self._cells.setdefault((cx, cy), []).append((key, x, y, w, h))

    def _overlaps(self, x1, y1, w1, h1, x2, y2, w2, h2, clearance):
        return (
            abs(x1 - x2) < (w1 + w2) / 2 + clearance
            and abs(y1 - y2) < (h1 + h2) / 2 + clearance
        )

    def query_overlaps(
        self, x: float, y: float, w: float, h: float, clearance: float = 0.5,
    ) -> list[str]:
        cx0, cy0, cx1, cy1 = self._cell_range(x, y, w, h, clearance)
        seen: set[str] = set()
        result: list[str] = []
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                for key, ex, ey, ew, eh in self._cells.get((cx, cy), ()):
                    if key in seen:
                        continue
                    seen.add(key)
                    if self._overlaps(x, y, w, h, ex, ey, ew, eh, clearance):
                        result.append(key)
        return result

    def check_any_overlap(
        self, x: float, y: float, w: float, h: float, clearance: float = 0.5,
    ) -> bool:
        cx0, cy0, cx1, cy1 = self._cell_range(x, y, w, h, clearance)
        seen: set[str] = set()
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                for key, ex, ey, ew, eh in self._cells.get((cx, cy), ()):
                    if key in seen:
                        continue
                    seen.add(key)
                    if self._overlaps(x, y, w, h, ex, ey, ew, eh, clearance):
                        return True
        return False

    def all_overlapping_pairs(self, clearance: float = 0.5) -> list[tuple[str, str]]:
        keys = sorted(self._entries.keys())
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for key in keys:
            x, y, w, h = self._entries[key]
            for other in self.query_overlaps(x, y, w, h, clearance):
                if other == key:
                    continue
                pair = (min(key, other), max(key, other))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append(pair)
        return pairs
