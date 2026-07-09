from __future__ import annotations

import copy
import logging
import math
import os
import re
import uuid
from dataclasses import dataclass

from simp_sexp import Sexp

from .constraints import BoardCutout, BoardOutline

logger = logging.getLogger(__name__)

_NAMESPACE_UUID = uuid.UUID("7026fcc6-e1a0-409e-aaf4-6a17ea82654f")


def _q(value) -> str:
    text = str(value or "")
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _needs_quoting(s: str) -> bool:
    if not s:
        return True
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return False
    return any(c in s for c in ' "\t\n\r()')


def _requote_strings(sexp):
    """Quote string tokens that contain spaces or special characters."""
    for i, item in enumerate(sexp):
        if isinstance(item, list):
            _requote_strings(item)
        elif i > 0 and isinstance(item, str) and _needs_quoting(item):
            sexp[i] = _q(item)


_LAYERS = [
    (0,  _q("F.Cu"),      "signal"),
    (2,  _q("B.Cu"),      "signal"),
    (9,  _q("F.Adhes"),   "user",   _q("F.Adhesive")),
    (11, _q("B.Adhes"),   "user",   _q("B.Adhesive")),
    (13, _q("F.Paste"),   "user"),
    (15, _q("B.Paste"),   "user"),
    (5,  _q("F.SilkS"),   "user",   _q("F.Silkscreen")),
    (7,  _q("B.SilkS"),   "user",   _q("B.Silkscreen")),
    (1,  _q("F.Mask"),    "user"),
    (3,  _q("B.Mask"),    "user"),
    (17, _q("Dwgs.User"), "user",   _q("User.Drawings")),
    (19, _q("Cmts.User"), "user",   _q("User.Comments")),
    (21, _q("Eco1.User"), "user",   _q("User.Eco1")),
    (23, _q("Eco2.User"), "user",   _q("User.Eco2")),
    (25, _q("Edge.Cuts"), "user"),
    (27, _q("Margin"),    "user"),
    (31, _q("F.CrtYd"),   "user",   _q("F.Courtyard")),
    (29, _q("B.CrtYd"),   "user",   _q("B.Courtyard")),
    (35, _q("F.Fab"),     "user"),
    (33, _q("B.Fab"),     "user"),
]
_BOARD_LAYER_NAMES = {str(entry[1]).strip('"') for entry in _LAYERS}
_FOOTPRINT_LAYER_WILDCARDS = {"*.Cu", "*.Mask", "*.Paste"}
_FOOTPRINT_EDGE_CUTS_LAYER = "Dwgs.User"
_SILKSCREEN_LAYERS = {"F.SilkS", "B.SilkS"}
_SILKSCREEN_TEXT_MARGIN_MM = 2.0
_SMALL_SMD_PASSIVE_RE = re.compile(
    r"(?:^|[:_])(?:R|C|L|D)_?(?:0201|0402|0603|0805|"
    r"0603Metric|1005Metric|1608Metric|2012Metric)(?:_|$)",
    re.IGNORECASE,
)


@dataclass
class PlacedPart:
    ref: str
    x_mm: float
    y_mm: float
    rot_deg: float
    footprint: str  # "Library:Name" format
    side: str = "front"


def _part_uuid(part) -> str:
    return str(uuid.uuid5(_NAMESPACE_UUID, part.hiername))


def _sheet_uuid(level_name: str) -> str:
    return str(uuid.uuid5(_NAMESPACE_UUID, level_name))


def _kiid_path(part) -> str:
    hierarchy = part.hiertuple
    if len(hierarchy) <= 1:
        return f"/{_part_uuid(part)}"
    sheet_uuids = [_sheet_uuid(level) for level in hierarchy[1:]]
    return "/" + "/".join(sheet_uuids) + "/" + _part_uuid(part)


def parse_fp_lib_table(path: str, project_dir: str = None) -> dict[str, str]:
    """Parse fp-lib-table and return {lib_name: resolved_dir_path}."""
    if project_dir is None:
        project_dir = os.path.dirname(os.path.abspath(path))
    result = {}
    try:
        with open(path) as f:
            table = Sexp(f.read())
    except (FileNotFoundError, OSError):
        return result
    for lib in table.search("lib"):
        name_node = _find_child(lib, "name")
        uri_node = _find_child(lib, "uri")
        if name_node and uri_node and len(name_node) > 1 and len(uri_node) > 1:
            lib_name = str(name_node[1]).strip('"')
            uri = str(uri_node[1]).strip('"')
            uri = uri.replace("${KIPRJMOD}", project_dir)
            result[lib_name] = uri
    return result


def _fp_file_path(
    fp_name: str,
    fp_lib_dirs: list[str],
    lib_table: dict[str, str] = None,
) -> str:
    if ":" not in fp_name:
        raise FileNotFoundError(f"Invalid footprint name (no library prefix): {fp_name!r}")
    lib, name = fp_name.split(":", 1)
    file_name = f"{name}.kicad_mod"

    # Check fp-lib-table mapping first
    if lib_table and lib in lib_table:
        candidate = os.path.join(lib_table[lib], file_name)
        if os.path.isfile(candidate):
            return candidate

    lib_dir = f"{lib}.pretty"
    search_dirs = list(fp_lib_dirs)
    env_dir = os.environ.get("KICAD9_FOOTPRINT_DIR", "/usr/share/kicad/footprints")
    if env_dir:
        search_dirs.append(env_dir)

    for base in search_dirs:
        candidate = os.path.join(base, lib_dir, file_name)
        if os.path.isfile(candidate):
            return candidate

    raise FileNotFoundError(f"Footprint not found: {fp_name} (searched {search_dirs})")


def load_footprint(fp_name: str, fp_lib_dirs: list[str], lib_table: dict[str, str] = None) -> Sexp:
    """Load a .kicad_mod footprint file and return its S-expression."""
    path = _fp_file_path(fp_name, fp_lib_dirs, lib_table)
    with open(path) as f:
        return Sexp(f.read())


def _find_child(sexp, key: str):
    for child in sexp:
        if isinstance(child, list) and len(child) > 0 and child[0] == key:
            return child
    return None


def _strip_quotes(value) -> str:
    return str(value or "").strip('"')


def footprint_bbox(fp_sexp: Sexp) -> tuple[float, float]:
    """Compute bounding box (width_mm, height_mm) from a footprint's pads."""
    xs: list[float] = []
    ys: list[float] = []

    for pad in fp_sexp.search("pad"):
        pad_at = _find_child(pad, "at")
        pad_size = _find_child(pad, "size")
        if pad_at is None or pad_size is None:
            continue
        px, py = float(pad_at[1]), float(pad_at[2])
        pw, ph = float(pad_size[1]), float(pad_size[2])
        xs.extend([px - pw / 2, px + pw / 2])
        ys.extend([py - ph / 2, py + ph / 2])

    if not xs:
        return (0.0, 0.0)
    return (max(xs) - min(xs), max(ys) - min(ys))


def load_footprint_bboxes(
    fp_names: set[str],
    fp_lib_dirs: list[str],
    lib_table: dict[str, str] = None,
) -> dict[str, tuple[float, float]]:
    """Load bounding boxes for a set of footprint names."""
    result: dict[str, tuple[float, float]] = {}
    for name in fp_names:
        try:
            fp_sexp = load_footprint(name, fp_lib_dirs, lib_table)
            result[name] = footprint_bbox(fp_sexp)
        except FileNotFoundError:
            pass
    return result


def validate_footprints(
    fp_names: set[str],
    fp_lib_dirs: list[str],
    lib_table: dict[str, str] = None,
) -> tuple[set[str], set[str]]:
    """Check which footprints exist on the filesystem.

    Returns (valid, missing) sets of footprint names.
    """
    valid: set[str] = set()
    missing: set[str] = set()
    for name in fp_names:
        try:
            _fp_file_path(name, fp_lib_dirs, lib_table)
            valid.add(name)
        except FileNotFoundError:
            missing.add(name)
    return valid, missing


def _default_setup() -> Sexp:
    return Sexp([
        "setup",
        ["pad_to_mask_clearance", 0],
        ["allow_soldermask_bridges_in_footprints", "no"],
        ["tenting", "front", "back"],
        [
            "pcbplotparams",
            ["layerselection", "0x00000000_00000000_55555555_5755f5ff"],
            ["plot_on_all_layers_selection", "0x00000000_00000000_00000000_00000000"],
            ["disableapertmacros", "no"],
            ["usegerberextensions", "no"],
            ["usegerberattributes", "yes"],
            ["usegerberadvancedattributes", "yes"],
            ["creategerberjobfile", "yes"],
            ["dashed_line_dash_ratio", 12.000000],
            ["dashed_line_gap_ratio", 3.000000],
            ["svgprecision", 4],
            ["plotframeref", "no"],
            ["mode", 1],
            ["useauxorigin", "no"],
            ["hpglpennumber", 1],
            ["hpglpenspeed", 20],
            ["hpglpendiameter", 15.000000],
            ["pdf_front_fp_property_popups", "yes"],
            ["pdf_back_fp_property_popups", "yes"],
            ["pdf_metadata", "yes"],
            ["pdf_single_document", "no"],
            ["dxfpolygonmode", "yes"],
            ["dxfimperialunits", "yes"],
            ["dxfusepcbnewfont", "yes"],
            ["psnegative", "no"],
            ["psa4output", "no"],
            ["plot_black_and_white", "yes"],
            ["sketchpadsonfab", "no"],
            ["plotpadnumbers", "no"],
            ["hidednponfab", "no"],
            ["sketchdnponfab", "yes"],
            ["crossoutdnponfab", "yes"],
            ["subtractmaskfromsilk", "no"],
            ["outputformat", 1],
            ["mirror", "no"],
            ["drillshape", 1],
            ["scaleselection", 1],
            ["outputdirectory", _q("")],
        ],
    ])


def _build_net_map(circuit) -> tuple[dict[str, int], list]:
    """Return (name→code, ordered_nets) where code starts at 1."""
    nets = circuit.get_nets()
    net_map = {n.name: i + 1 for i, n in enumerate(nets)}
    return net_map, nets


def _quote_layer_node(node):
    layer = _find_child(node, "layer")
    if layer is not None and len(layer) > 1:
        layer[1] = _q(layer[1])


def _walk_nodes(node):
    if isinstance(node, list):
        yield node
        for child in node:
            yield from _walk_nodes(child)


def _sanitize_layer_nodes(fp: Sexp):
    for node in _walk_nodes(fp):
        if not node:
            continue
        if node[0] == "layer" and len(node) > 1:
            node[1] = _q(node[1])
        elif node[0] == "layers":
            layers = [str(layer).strip('"') for layer in node[1:]]
            filtered = [
                layer
                for layer in layers
                if layer in _BOARD_LAYER_NAMES
                or layer in _FOOTPRINT_LAYER_WILDCARDS
            ]
            if filtered:
                node[:] = ["layers"] + [_q(layer) for layer in filtered]
            else:
                node[:] = ["layers"] + [_q(layer) for layer in layers]


def _demote_footprint_edge_cuts(fp: Sexp) -> None:
    """Prevent copied footprint graphics from becoming board-outline geometry."""
    for node in _walk_nodes(fp):
        if not isinstance(node, list) or not node:
            continue
        layer = _find_child(node, "layer")
        if layer is not None and len(layer) > 1 and _strip_quotes(layer[1]) == "Edge.Cuts":
            layer[1] = _q(_FOOTPRINT_EDGE_CUTS_LAYER)


_SIDE_LAYER_SWAP = {
    "F.Cu": "B.Cu",
    "B.Cu": "F.Cu",
    "F.Adhes": "B.Adhes",
    "B.Adhes": "F.Adhes",
    "F.Paste": "B.Paste",
    "B.Paste": "F.Paste",
    "F.SilkS": "B.SilkS",
    "B.SilkS": "F.SilkS",
    "F.Mask": "B.Mask",
    "B.Mask": "F.Mask",
    "F.CrtYd": "B.CrtYd",
    "B.CrtYd": "F.CrtYd",
    "F.Fab": "B.Fab",
    "B.Fab": "F.Fab",
}


def _flip_layer_name(layer: str) -> str:
    return _SIDE_LAYER_SWAP.get(str(layer).strip('"'), str(layer).strip('"'))


def _place_footprint_on_back(fp: Sexp) -> None:
    for node in _walk_nodes(fp):
        if not node:
            continue
        if node[0] == "layer" and len(node) > 1:
            node[1] = _q(_flip_layer_name(node[1]))
        elif node[0] == "layers":
            node[1:] = [
                _q(_flip_layer_name(layer))
                for layer in node[1:]
            ]


def _normalize_angle(angle: float) -> float:
    normalized = angle % 360.0
    if math.isclose(normalized, 0.0, abs_tol=1e-9) or math.isclose(
        normalized,
        360.0,
        abs_tol=1e-9,
    ):
        return 0.0
    return round(normalized, 4)


def _apply_footprint_rotation_to_pads(fp: Sexp, rot_deg: float) -> None:
    """Make pad shape rotation explicit for rotated footprints.

    KiCad applies footprint rotation to pad positions, but DRC treats the pad's
    local angle as the copper shape orientation.  Without this, non-square pads
    in rotated SOIC/USB footprints can appear to overlap adjacent pins.
    """
    if math.isclose(rot_deg % 360.0, 0.0, abs_tol=1e-9):
        return
    for pad in fp.search("pad"):
        at = _find_child(pad, "at")
        if at is None or len(at) < 3:
            continue
        existing = float(at[3]) if len(at) > 3 else 0.0
        angle = _normalize_angle(existing + rot_deg)
        if len(at) > 3:
            at[3] = angle
        else:
            at.append(angle)


def _ensure_uuid(node, seed: str):
    if _find_child(node, "uuid") is None:
        node.append(Sexp(["uuid", _q(uuid.uuid5(_NAMESPACE_UUID, seed))]))


def _refresh_uuids(sexp: Sexp, seed: str):
    """Make footprint-local UUIDs unique after copying a library footprint."""
    for idx, node in enumerate(_walk_nodes(sexp)):
        if not node or node[0] != "uuid":
            continue
        new_uuid = _q(uuid.uuid5(_NAMESPACE_UUID, f"{seed}:uuid:{idx}"))
        if len(node) > 1:
            node[1] = new_uuid
        else:
            node.append(new_uuid)


_ALWAYS_QUOTE_FIELDS = frozenset({
    "uuid", "generator", "generator_version", "descr", "tags", "model",
})


def _quote_known_fields(sexp):
    """Quote values of KiCad fields that always require quoted strings."""
    for node in _walk_nodes(sexp):
        if len(node) >= 2 and node[0] in _ALWAYS_QUOTE_FIELDS:
            if isinstance(node[1], str):
                node[1] = _q(node[1])


def _prepare_footprint_for_board(fp: Sexp, fp_uuid: str):
    _requote_strings(fp)
    _quote_known_fields(fp)
    if len(fp) > 1:
        fp[1] = _q(fp[1])
    _sanitize_layer_nodes(fp)
    _demote_footprint_edge_cuts(fp)
    _ensure_uuid(fp, fp_uuid)

    for prop in fp.search("property"):
        if len(prop) > 1:
            prop[1] = _q(prop[1])
        if len(prop) > 2:
            prop[2] = _q(prop[2])
        _ensure_uuid(prop, f"{fp_uuid}:property:{prop[1] if len(prop) > 1 else ''}")

    for pad in fp.search("pad"):
        if len(pad) > 1:
            pad[1] = _q(pad[1])
        pad[:] = [
            node
            for node in pad
            if not (
                isinstance(node, list)
                and len(node) > 1
                and node[0] == "property"
                and _strip_quotes(node[1]).startswith("pad_prop_")
            )
        ]
        _ensure_uuid(pad, f"{fp_uuid}:pad:{pad[1] if len(pad) > 1 else ''}")

    _refresh_uuids(fp, fp_uuid)


def _text_kind(node) -> str | None:
    if not isinstance(node, list) or len(node) < 2:
        return None
    if node[0] == "property":
        return _strip_quotes(node[1])
    if node[0] == "fp_text":
        return _strip_quotes(node[1]).title()
    return None


def _is_silkscreen_text_node(node) -> bool:
    layer = _find_child(node, "layer")
    return layer is not None and len(layer) > 1 and _strip_quotes(layer[1]) in _SILKSCREEN_LAYERS


def _ensure_hidden(node) -> None:
    hidden = _find_child(node, "hide")
    if hidden is not None:
        if len(hidden) > 1:
            hidden[1] = "yes"
        else:
            hidden.append("yes")
        return
    node.append(Sexp(["hide", "yes"]))


def _is_mounting_hole(part, pp: PlacedPart) -> bool:
    fields = [
        pp.ref,
        pp.footprint,
        getattr(part, "name", ""),
        getattr(part, "value", ""),
        getattr(part, "description", ""),
    ]
    text = " ".join(str(field or "") for field in fields).lower()
    return "mountinghole" in text or "mounting hole" in text


def _is_small_smd_passive(part, pp: PlacedPart) -> bool:
    ref = str(getattr(pp, "ref", "") or getattr(part, "ref", "") or "")
    prefix = re.match(r"[A-Za-z]+", ref)
    if prefix is None or prefix.group(0).upper() not in {"R", "C", "L", "D"}:
        return False
    footprint = str(getattr(pp, "footprint", "") or getattr(part, "footprint", "") or "")
    return bool(_SMALL_SMD_PASSIVE_RE.search(footprint))


def _rotate_point(x: float, y: float, rot_deg: float) -> tuple[float, float]:
    if not rot_deg:
        return x, y
    angle = math.radians(rot_deg)
    return (
        x * math.cos(angle) + y * math.sin(angle),
        -x * math.sin(angle) + y * math.cos(angle),
    )


def _clamp(value: float, lo: float, hi: float) -> float:
    if lo > hi:
        return (lo + hi) / 2
    return min(max(value, lo), hi)


def _nudge_silkscreen_text_inside_outline(
    node,
    pp: PlacedPart,
    outline: BoardOutline | None,
) -> None:
    if outline is None or not outline.vertices or not _is_silkscreen_text_node(node):
        return
    at = _find_child(node, "at")
    if at is None or len(at) < 3:
        return

    local_x, local_y = float(at[1]), float(at[2])
    dx, dy = _rotate_point(local_x, local_y, pp.rot_deg)
    board_x = pp.x_mm + dx
    board_y = pp.y_mm + dy
    margin = min(
        _SILKSCREEN_TEXT_MARGIN_MM,
        max(0.0, outline.width_mm / 2),
        max(0.0, outline.height_mm / 2),
    )
    clamped_x = _clamp(board_x, outline.x_min + margin, outline.x_max - margin)
    clamped_y = _clamp(board_y, outline.y_min + margin, outline.y_max - margin)
    if clamped_x == board_x and clamped_y == board_y:
        return

    inv_dx, inv_dy = _rotate_point(clamped_x - pp.x_mm, clamped_y - pp.y_mm, -pp.rot_deg)
    at[1] = round(inv_dx, 4)
    at[2] = round(inv_dy, 4)


def _tidy_silkscreen_text(
    fp: Sexp,
    pp: PlacedPart,
    part,
    outline: BoardOutline | None,
) -> None:
    hide_mounting_hole_text = _is_mounting_hole(part, pp)
    hide_small_passive_text = _is_small_smd_passive(part, pp)
    for node in _walk_nodes(fp):
        kind = _text_kind(node)
        if kind not in {"Reference", "Value"}:
            continue
        if (hide_mounting_hole_text or hide_small_passive_text) and _is_silkscreen_text_node(node):
            _ensure_hidden(node)
            continue
        _nudge_silkscreen_text_inside_outline(node, pp, outline)


def _place_footprint(
    fp_sexp: Sexp,
    pp: PlacedPart,
    fp_uuid: str,
    net_map: dict[str, int],
    part,
    outline: BoardOutline = None,
) -> Sexp:
    fp = copy.deepcopy(fp_sexp)
    _prepare_footprint_for_board(fp, fp_uuid)
    if str(getattr(pp, "side", "front") or "front").lower() == "back":
        _place_footprint_on_back(fp)
    _apply_footprint_rotation_to_pads(fp, pp.rot_deg)

    at_val = [pp.x_mm, pp.y_mm]
    if pp.rot_deg:
        at_val.append(pp.rot_deg)
    fp.insert(2, Sexp(["at"] + at_val))

    for i, child in enumerate(fp):
        if not (isinstance(child, list) and len(child) >= 3 and child[0] == "property"):
            continue
        prop_name = str(child[1]).strip('"')
        if prop_name == "Reference":
            fp[i][2] = _q(pp.ref)
        elif prop_name == "Value":
            fp[i][2] = _q(getattr(part, "value", "") or pp.ref)

    for pad in fp.search("pad"):
        pad_num = str(pad[1])
        net_name = None
        if part is not None:
            try:
                pins = part.get_pins(pad_num, silent=True)
                if pins:
                    pin = pins[0] if isinstance(pins, list) else pins
                    n = getattr(pin, "net", None)
                    if n is not None:
                        net_name = getattr(n, "name", None)
            except Exception:
                pass
        if net_name and net_name in net_map:
            pad.append(Sexp(["net", net_map[net_name], _q(net_name)]))

    _tidy_silkscreen_text(fp, pp, part, outline)

    return fp


def _find_circuit_part(circuit, ref: str):
    for part in circuit.parts:
        if getattr(part, "ref", None) == ref:
            return part
    return None


def _is_schematic_only_power_marker(part) -> bool:
    """Return true for symbols that should never become PCB footprints."""
    if part is None:
        return False
    footprint = str(getattr(part, "footprint", "") or "").strip()
    if footprint:
        return False
    fields = {
        str(getattr(part, attr, "") or "").strip().upper()
        for attr in ("name", "value", "part")
    }
    lib = str(getattr(part, "lib", "") or getattr(part, "libname", "") or "").lower()
    ref = str(getattr(part, "ref", "") or "").upper()
    return (
        "PWR_FLAG" in fields
        or ref.startswith("#FLG")
        or (lib == "power" and any(value.startswith("#PWR") for value in fields | {ref}))
    )


def _is_rectangular_outline(outline: BoardOutline) -> bool:
    if len(outline.vertices) != 4:
        return False
    expected = {
        (outline.x_min, outline.y_min),
        (outline.x_max, outline.y_min),
        (outline.x_max, outline.y_max),
        (outline.x_min, outline.y_max),
    }
    return set(outline.vertices) == expected


def _points_coincident(
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    tol: float = 1e-6,
) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _append_rounded_rect_outline(board: Sexp, outline: BoardOutline, radius: float):
    x0, y0 = outline.x_min, outline.y_min
    x1, y1 = outline.x_max, outline.y_max
    radius = min(radius, outline.width_mm / 2, outline.height_mm / 2)
    if radius <= 0:
        return False

    segments_per_corner = 8
    centers = [
        (x1 - radius, y0 + radius, -90.0, 0.0),
        (x1 - radius, y1 - radius, 0.0, 90.0),
        (x0 + radius, y1 - radius, 90.0, 180.0),
        (x0 + radius, y0 + radius, 180.0, 270.0),
    ]
    points: list[tuple[float, float]] = [(x0 + radius, y0), (x1 - radius, y0)]
    for cx, cy, start_deg, end_deg in centers:
        for step in range(1, segments_per_corner + 1):
            angle = math.radians(
                start_deg + (end_deg - start_deg) * step / segments_per_corner
            )
            points.append(
                (
                    round(cx + radius * math.cos(angle), 4),
                    round(cy + radius * math.sin(angle), 4),
                )
            )

    for idx, (start, end) in enumerate(zip(points, points[1:] + points[:1])):
        if _points_coincident(start, end):
            continue
        board.append(Sexp([
            "gr_line",
            ["start", start[0], start[1]],
            ["end", end[0], end[1]],
            ["stroke", ["width", 0.1], ["type", "solid"]],
            ["layer", _q("Edge.Cuts")],
            ["uuid", _q(uuid.uuid5(_NAMESPACE_UUID, f"outline:round:{idx}"))],
        ]))
    return True


def _append_outline(board: Sexp, outline: BoardOutline):
    if outline is None or not outline.vertices:
        return

    if _is_rectangular_outline(outline):
        radius = getattr(outline, "corner_radius_mm", 0.0)
        if _append_rounded_rect_outline(board, outline, radius):
            return
        board.append(Sexp([
            "gr_rect",
            ["start", outline.x_min, outline.y_min],
            ["end", outline.x_max, outline.y_max],
            ["stroke", ["width", 0.1], ["type", "solid"]],
            ["fill", "no"],
            ["layer", _q("Edge.Cuts")],
            ["uuid", _q(uuid.uuid5(_NAMESPACE_UUID, "outline:rect"))],
        ]))
        return

    vertices = outline.vertices
    for idx, (start, end) in enumerate(zip(vertices, vertices[1:] + vertices[:1])):
        if _points_coincident(start, end):
            continue
        board.append(Sexp([
            "gr_line",
            ["start", start[0], start[1]],
            ["end", end[0], end[1]],
            ["stroke", ["width", 0.1], ["type", "solid"]],
            ["layer", _q("Edge.Cuts")],
            ["uuid", _q(uuid.uuid5(_NAMESPACE_UUID, f"outline:line:{idx}"))],
        ]))


def _append_cutouts(board: Sexp, cutouts: list[BoardCutout] | None):
    for idx, cutout in enumerate(cutouts or []):
        seed = f"cutout:{idx}:{getattr(cutout, 'name', '')}"
        shape = str(getattr(cutout, "shape", "rect") or "rect").lower()
        vertices = list(getattr(cutout, "vertices", []) or [])
        if vertices:
            for line_idx, (start, end) in enumerate(zip(vertices, vertices[1:] + vertices[:1])):
                if _points_coincident(start, end):
                    continue
                board.append(Sexp([
                    "gr_line",
                    ["start", start[0], start[1]],
                    ["end", end[0], end[1]],
                    ["stroke", ["width", 0.1], ["type", "solid"]],
                    ["layer", _q("Edge.Cuts")],
                    ["uuid", _q(uuid.uuid5(_NAMESPACE_UUID, f"{seed}:line:{line_idx}"))],
                ]))
            continue
        if shape == "circle" and getattr(cutout, "radius_mm", None):
            cx = cutout.center_x_mm
            cy = cutout.center_y_mm
            radius = float(cutout.radius_mm)
            board.append(Sexp([
                "gr_circle",
                ["center", cx, cy],
                ["end", cx + radius, cy],
                ["stroke", ["width", 0.1], ["type", "solid"]],
                ["fill", "no"],
                ["layer", _q("Edge.Cuts")],
                ["uuid", _q(uuid.uuid5(_NAMESPACE_UUID, seed))],
            ]))
            continue
        board.append(Sexp([
            "gr_rect",
            ["start", cutout.x_min, cutout.y_min],
            ["end", cutout.x_max, cutout.y_max],
            ["stroke", ["width", 0.1], ["type", "solid"]],
            ["fill", "no"],
            ["layer", _q("Edge.Cuts")],
            ["uuid", _q(uuid.uuid5(_NAMESPACE_UUID, seed))],
        ]))


def write_kicad_pcb(
    placed_parts: list,
    circuit,
    fp_lib_dirs: list[str],
    output_path: str,
    outline: BoardOutline = None,
    cutouts: list[BoardCutout] | None = None,
    version: int = 20241229,
    strict_missing_footprints: bool = True,
    lib_table: dict[str, str] = None,
):
    """Write a complete .kicad_pcb file."""
    net_map, nets = _build_net_map(circuit)

    board = Sexp(["kicad_pcb"])
    board.append(Sexp(["version", version]))
    board.append(Sexp(["generator", _q("skidl")]))
    board.append(Sexp(["generator_version", _q("9.0")]))
    board.append(Sexp(["general", ["thickness", 1.6], ["legacy_teardrops", "no"]]))
    board.append(Sexp(["paper", _q("A4")]))

    layers = Sexp(["layers"])
    for entry in _LAYERS:
        row = Sexp([entry[0], entry[1], entry[2]])
        if len(entry) == 4:
            row.append(entry[3])
        layers.append(row)
    board.append(layers)

    board.append(_default_setup())

    board.append(Sexp(["net", 0, _q("")]))
    for net in nets:
        board.append(Sexp(["net", net_map[net.name], _q(net.name)]))

    missing_fps = []
    for pp in placed_parts:
        part = _find_circuit_part(circuit, pp.ref)
        if _is_schematic_only_power_marker(part):
            logger.debug("Skipping schematic-only power marker %s in PCB writer", pp.ref)
            continue
        try:
            fp_sexp = load_footprint(pp.footprint, fp_lib_dirs, lib_table)
        except FileNotFoundError:
            missing_fps.append((pp.ref, pp.footprint))
            logger.warning(
                "MISSING FOOTPRINT: %s (%s) - skipped, will not appear in PCB",
                pp.ref,
                pp.footprint,
            )
            continue

        fp_uuid = _part_uuid(part) if part is not None else str(uuid.uuid4())

        fp = _place_footprint(fp_sexp, pp, fp_uuid, net_map, part, outline)
        board.append(fp)

    if missing_fps:
        missing_refs = ", ".join(ref for ref, _ in missing_fps[:20])
        message = (
            f"INCOMPLETE PCB: {len(missing_fps)}/{len(placed_parts)} parts "
            f"missing footprints: {missing_refs}"
        )
        logger.warning(
            "INCOMPLETE PCB: %d/%d parts missing footprints: %s",
            len(missing_fps), len(placed_parts), missing_refs,
        )
        if strict_missing_footprints:
            raise FileNotFoundError(message)

    _append_outline(board, outline)
    _append_cutouts(board, cutouts)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(board.to_str())
        f.write("\n")
