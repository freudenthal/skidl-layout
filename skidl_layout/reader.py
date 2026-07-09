from __future__ import annotations

from simp_sexp import Sexp

from .constraints import BoardOutline, FixedPosition

_ORIGIN_EPSILON = 0.001


def _find_child(sexp, key: str):
    for child in sexp:
        if isinstance(child, list) and len(child) > 0 and child[0] == key:
            return child
    return None


def _find_children(sexp, key: str):
    return [child for child in sexp if isinstance(child, list) and len(child) > 0 and child[0] == key]


def _is_on_layer(sexp, layer_name: str) -> bool:
    layer = _find_child(sexp, "layer")
    return layer is not None and len(layer) > 1 and str(layer[1]) == layer_name


def _point(child) -> tuple[float, float] | None:
    if child is None or len(child) < 3:
        return None
    return (float(child[1]), float(child[2]))


def _same_point(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _order_segments(segments: list[tuple[tuple[float, float], tuple[float, float]]]):
    if not segments:
        return []

    remaining = segments[1:]
    vertices = [segments[0][0], segments[0][1]]
    while remaining:
        last = vertices[-1]
        for i, (start, end) in enumerate(remaining):
            if _same_point(start, last):
                vertices.append(end)
                remaining.pop(i)
                break
            if _same_point(end, last):
                vertices.append(start)
                remaining.pop(i)
                break
        else:
            start, end = remaining.pop(0)
            vertices.extend([start, end])

    if len(vertices) > 1 and _same_point(vertices[0], vertices[-1]):
        vertices.pop()
    return vertices


def _fp_reference(fp_sexp) -> str | None:
    for child in fp_sexp:
        if (
            isinstance(child, list)
            and len(child) >= 3
            and child[0] == "property"
            and child[1] == "Reference"
        ):
            return str(child[2])
    return None


def read_placed_positions(pcb_path: str) -> list:
    """Parse .kicad_pcb, extract positions of non-origin footprints as FixedPosition objects.

    Parts at (at 0 0) or (at 0 0 0) are treated as "unplaced" (KiCad dumps them at origin).
    Returns only parts that have been deliberately placed (not at origin).
    """
    with open(pcb_path) as f:
        board = Sexp(f.read())

    result = []
    for fp in board.search("footprint"):
        at = _find_child(fp, "at")
        if at is None:
            continue

        x = float(at[1])
        y = float(at[2])
        angle = float(at[3]) if len(at) > 3 else 0.0

        if abs(x) < _ORIGIN_EPSILON and abs(y) < _ORIGIN_EPSILON:
            continue

        ref = _fp_reference(fp)
        if ref is None:
            continue

        result.append(FixedPosition(ref=ref, x_mm=x, y_mm=y, rot_deg=angle))

    return result


def read_board_outline(pcb_path: str) -> BoardOutline | None:
    """Extract an Edge.Cuts board outline from a .kicad_pcb file."""
    with open(pcb_path) as f:
        board = Sexp(f.read())

    for rect in board.search("gr_rect"):
        if not _is_on_layer(rect, "Edge.Cuts"):
            continue
        start = _point(_find_child(rect, "start"))
        end = _point(_find_child(rect, "end"))
        if start is None or end is None:
            continue
        x1, y1 = start
        x2, y2 = end
        return BoardOutline(vertices=[
            (x1, y1),
            (x2, y1),
            (x2, y2),
            (x1, y2),
        ])

    segments = []
    for line in board.search("gr_line"):
        if not _is_on_layer(line, "Edge.Cuts"):
            continue
        start = _point(_find_child(line, "start"))
        end = _point(_find_child(line, "end"))
        if start is not None and end is not None:
            segments.append((start, end))

    vertices = _order_segments(segments)
    if vertices:
        return BoardOutline(vertices=vertices)
    return None


def read_footprint_bboxes(pcb_path: str) -> dict:
    """Extract footprint bounding boxes from placed parts in existing board.

    Returns dict mapping footprint_name → (width_mm, height_mm).
    Bounding box is computed from pad extents in each footprint.
    """
    with open(pcb_path) as f:
        board = Sexp(f.read())

    bboxes: dict[str, tuple[float, float]] = {}

    for fp in board.search("footprint"):
        fp_name = str(fp[1])
        if fp_name in bboxes:
            continue

        pads = _find_children(fp, "pad")
        if not pads:
            continue

        xs: list[float] = []
        ys: list[float] = []

        for pad in pads:
            pad_at = _find_child(pad, "at")
            pad_size = _find_child(pad, "size")
            if pad_at is None or pad_size is None:
                continue

            px, py = float(pad_at[1]), float(pad_at[2])
            pw, ph = float(pad_size[1]), float(pad_size[2])

            xs.extend([px - pw / 2, px + pw / 2])
            ys.extend([py - ph / 2, py + ph / 2])

        if xs and ys:
            bboxes[fp_name] = (max(xs) - min(xs), max(ys) - min(ys))

    return bboxes
