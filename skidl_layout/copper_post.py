"""Copper this stack could not previously emit: a thermal via array under an exposed pad.

Power-layout Phase 5, WS-3. **Nothing in the toolchain emits one today** --
KRT's ``add_gnd_vias.py`` places ground vias beside *signal vias* and has no
notion of a thermal pad; upstream issue #485's via-stitching lattice is fully
specified and unimplemented. Phase 0 measured **0 vias inside** the LT3757's
exposed pad on all three placements and Phase 4 did not move it.

This is a **post-process** (rung 3 of the escalation ladder): it edits the
``.kicad_pcb`` KRT already wrote, then the caller re-grades with KRT's own
``check_drc`` / ``check_connected``. It deliberately does *not* re-enter the
pour stage -- ``route_planes.py`` rips nets to place plane vias and reroutes
them in-run, and Phase 4's 13/13-routed result depends on that chain.

**The refusal is the point on the shipped spec.** ``oshpark-2l`` declares
``via_in_pad = False``, and a via array under an exposed pad *is* via-in-pad.
:func:`plan_thermal_vias` refuses on such a spec: it emits nothing, says why,
and never crashes. The default path for every board this stack ships is
therefore the refusal, not the array -- and :func:`skidl_layout.fab_check` now
grades the rule (Phase 5, WS-4) so a board that does carry an array cannot be
blessed by a spec that forbids it.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field

__all__ = [
    "ThermalViaPlan",
    "find_exposed_pad",
    "plan_thermal_vias",
    "splice_vias",
]

#: Namespace for the deterministic via ids. Boards are already not
#: byte-deterministic -- KRT's ``generate_zone_sexpr`` / ``generate_via_sexpr``
#: mint a fresh ``uuid4`` per call -- but that is no reason for *our* additions
#: to add entropy, and KRT's own docstring records that uuid order once changed
#: a fill outcome on overlapping equal-priority zones.
_UUID_NAMESPACE = uuid.UUID("6f9f4d3a-7a25-5b1e-9c4d-5b1e7a256f9f")


@dataclass
class ThermalViaPlan:
    """What :func:`plan_thermal_vias` decided, whether or not it placed anything."""

    #: ``None`` when no exposed pad could be identified.
    pad: dict | None = None
    #: Via centres, board coordinates. Empty on every refusal.
    positions: list = field(default_factory=list)
    #: Array shape actually chosen, ``(columns, rows)``.
    shape: tuple = (0, 0)
    pitch_mm: float | None = None
    via_size_mm: float | None = None
    via_drill_mm: float | None = None
    net: str | None = None
    #: True when the spec forbids via-in-pad -- the shipped-spec path.
    refused: bool = False
    #: Human explanation; always set when nothing was placed.
    reason: str = ""
    #: Smallest distance from any via's copper edge to the pad edge (mm).
    edge_margin_mm: float | None = None

    @property
    def count(self) -> int:
        return len(self.positions)

    def to_dict(self) -> dict:
        return {
            "pad": dict(self.pad) if self.pad else None,
            "count": self.count,
            "positions": [[round(x, 4), round(y, 4)] for x, y in self.positions],
            "shape": list(self.shape),
            "pitch_mm": self.pitch_mm,
            "via_size_mm": self.via_size_mm,
            "via_drill_mm": self.via_drill_mm,
            "net": self.net,
            "refused": self.refused,
            "reason": self.reason,
            "edge_margin_mm": self.edge_margin_mm,
        }


def find_exposed_pad(pcb_path: str, controller_ref: str, ground_net: str) -> dict | None:
    """The controller's exposed pad: its **largest pad on the ground net**.

    Derived topologically -- the caller supplies ``controller_ref`` and
    ``ground_net`` from the ``PowerStagePlan`` :mod:`skidl_layout.power_roles`
    computed from library facts, so nothing here matches on reference
    designators, footprint names or pad numbers. Same derivation the Phase-0
    instrument uses, so the two agree by construction.

    Returns ``{ref, number, x, y, w, h, angle, layers, area_mm2}`` or ``None``.
    """
    from .fabspec import _smd_copper_pads

    from simp_sexp import Sexp

    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        board = Sexp(handle.read())

    nets = _pad_nets(board)
    candidates = []
    for pad in _smd_copper_pads(board):
        if pad["ref"] != controller_ref:
            continue
        if nets.get((pad["ref"], pad["number"])) != ground_net:
            continue
        candidates.append(pad)
    if not candidates:
        return None
    pad = max(candidates, key=lambda p: (p["w"] * p["h"], p["number"]))
    return {**pad, "area_mm2": round(pad["w"] * pad["h"], 4)}


def _pad_nets(board) -> dict:
    """``{(ref, pad_number): net_name}`` for every pad that names a net."""
    from .reader import _find_child, _find_children

    out: dict = {}
    for fp in board.search("footprint"):
        ref = "?"
        for prop in _find_children(fp, "property"):
            if len(prop) > 2 and str(prop[1]).strip('"') == "Reference":
                ref = str(prop[2]).strip('"')
        for pad in _find_children(fp, "pad"):
            net = _find_child(pad, "net")
            if net is None or len(net) < 3:
                continue
            out[(ref, str(pad[1]).strip('"'))] = str(net[2]).strip('"')
    return out


def plan_thermal_vias(
    pcb_path: str,
    controller_ref: str,
    ground_net: str,
    spec,
    pitch_mm: float | None = None,
    edge_margin_mm: float = 0.0,
    max_shape: tuple | None = None,
) -> ThermalViaPlan:
    """Decide the via array for ``controller_ref``'s exposed pad. Places nothing.

    The array is a **centred grid** at ``pitch_mm`` (default
    ``spec.via_size_mm + spec.clearance_mm``), sized so every via's *copper*
    stays inside the pad rectangle with ``edge_margin_mm`` to spare. Nothing is
    hardcoded to the LT3757: on its measured exposed pad (1.68 x 1.88 mm, 0.6 mm
    vias, 0.25 mm clearance -> 0.85 mm pitch, 1.45 mm outer extent) the
    computation yields **2 x 2 = 4**, which is the expectation rather than the
    rule.

    ``edge_margin_mm`` defaults to **0.0**: "fully inside the pad rectangle" is
    the containment rule, and the annular ring is a property of the via itself,
    already enforced by :meth:`FabSpec.__post_init__` and graded by
    :func:`fab_check`. Demanding an *extra* ring of pad copper outside each via
    is a stricter reading with a measurable cost -- on the LT3757 pad
    ``edge_margin_mm=0.127`` drops the array from 2 x 2 to 1 x 2 -- so it is a
    caller's choice, not a silent default.

    ``max_shape`` caps the grid (bail-out 2's shrink path: retry a smaller array
    when the full one costs DRC).

    **Refuses** -- returns a plan with ``refused=True`` and no positions --
    when ``spec`` is ``None`` or declares ``via_in_pad False``. That is the
    shipped ``oshpark-2l`` path.
    """
    plan = ThermalViaPlan(net=ground_net)
    if spec is None:
        plan.refused = True
        plan.reason = ("no FabSpec resolved: a thermal via array is via-in-pad, "
                       "which needs a spec that declares the capability")
        return plan
    plan.via_size_mm = spec.via_size_mm
    plan.via_drill_mm = spec.via_drill_mm
    if not getattr(spec, "via_in_pad", False):
        plan.refused = True
        plan.reason = (f"fab spec {spec.name!r} declares via_in_pad=False; a "
                       "thermal via array under an exposed pad IS via-in-pad, "
                       "so none were emitted")
        return plan

    pad = find_exposed_pad(pcb_path, controller_ref, ground_net)
    if pad is None:
        plan.reason = (f"no pad on {ground_net} found for {controller_ref}: "
                       "nothing to place an array in")
        return plan
    plan.pad = pad

    pitch = pitch_mm if pitch_mm else spec.via_size_mm + spec.clearance_mm
    plan.pitch_mm = round(pitch, 4)
    # A via's centre may sit at most this far from the pad centre and still keep
    # the whole via inside the pad (plus any requested margin).
    reach_x = pad["w"] / 2.0 - spec.via_size_mm / 2.0 - edge_margin_mm
    reach_y = pad["h"] / 2.0 - spec.via_size_mm / 2.0 - edge_margin_mm
    if reach_x < -1e-9 or reach_y < -1e-9:
        plan.reason = (f"{controller_ref}'s ground pad is "
                       f"{pad['w']:.2f}x{pad['h']:.2f}mm: a {spec.via_size_mm}mm "
                       "via does not fit inside it at all")
        return plan

    cols = _fit_count(reach_x, pitch)
    rows = _fit_count(reach_y, pitch)
    if max_shape:
        cols = min(cols, max(0, int(max_shape[0])))
        rows = min(rows, max(0, int(max_shape[1])))
    if cols < 1 or rows < 1:
        plan.reason = ("array shrunk to nothing; no via can be placed cleanly "
                       f"in a {pad['w']:.2f}x{pad['h']:.2f}mm pad")
        return plan
    plan.shape = (cols, rows)

    radians = math.radians(pad["angle"])
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    margin = min(reach_x - (cols - 1) * pitch / 2.0,
                 reach_y - (rows - 1) * pitch / 2.0) + edge_margin_mm
    plan.edge_margin_mm = round(margin, 4)
    for row in range(rows):
        for col in range(cols):
            local_x = (col - (cols - 1) / 2.0) * pitch
            local_y = (row - (rows - 1) / 2.0) * pitch
            # Forward of fabspec._point_in_pad's inverse rotation, i.e. the
            # transform geometry.transform_point applies.
            plan.positions.append((
                pad["x"] + local_x * cos_a + local_y * sin_a,
                pad["y"] - local_x * sin_a + local_y * cos_a,
            ))
    plan.reason = (f"{cols}x{rows} array at {pitch:.3f}mm pitch inside a "
                   f"{pad['w']:.2f}x{pad['h']:.2f}mm pad")
    return plan


def _fit_count(reach: float, pitch: float) -> int:
    """How many centred columns of ``pitch`` fit within +-``reach``."""
    if reach < -1e-9:
        return 0
    return int(math.floor(2.0 * reach / pitch + 1e-9)) + 1


def splice_vias(pcb_path: str, out_path: str, plan: ThermalViaPlan) -> int:
    """Write ``pcb_path`` to ``out_path`` with ``plan``'s vias added. Returns the count.

    The via s-expression is built by **mirroring an existing ``(via ...)`` block
    on the board** rather than from a guessed template. KiCad-10 formatting is
    not cosmetic here -- ``generate_via_sexpr`` emits a ``(tenting ...)`` child
    only when a net name is passed, and zone/via spelling differs between the
    board KRT writes and the board KiCad re-saves -- so copying the shape the
    file already uses is the only way to be sure the result loads.

    Each via gets a **deterministic** ``uuid5`` derived from (net, x, y, layers)
    so re-running produces the same ids.
    """
    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    if not plan.positions:
        if out_path != pcb_path:
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return 0

    template, insert_at = _via_template(text)
    net_id = _net_id_for(text, plan.net)
    if net_id is None:
        raise ValueError(f"net {plan.net!r} is not declared on {pcb_path}")

    blocks = []
    for x, y in plan.positions:
        ident = uuid.uuid5(_UUID_NAMESPACE,
                           f"thermal-via|{plan.net}|{x:.6f}|{y:.6f}")
        block = _AT_RE.sub(lambda m: f"{m.group(1)}{x:.6f} {y:.6f})", template, count=1)
        block = _NET_RE.sub(lambda m: f"{m.group(1)}{net_id})", block, count=1)
        block = _UUID_RE.sub(lambda m: f'{m.group(1)}"{ident}")', block, count=1)
        blocks.append(block)

    merged = text[:insert_at] + "".join(blocks) + text[insert_at:]
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(merged)
    return len(plan.positions)


_AT_RE = re.compile(r"(\(at\s+)[-\d.]+\s+[-\d.]+\s*\)")
_NET_RE = re.compile(r"(\(net\s+)\d+\s*\)")
_UUID_RE = re.compile(r'(\(uuid\s+)"[^"]*"\s*\)')
_VIA_HEAD_RE = re.compile(r"\n([\t ]*)\(via\b")


def _via_template(text: str) -> tuple:
    """An existing via block (verbatim, with its leading newline+indent) and the
    offset to splice copies in at, or a constructed block when the board has none."""
    last = None
    for match in _VIA_HEAD_RE.finditer(text):
        last = match
    if last is None:
        raise ValueError(
            "board has no (via ...) block to mirror; refusing to guess a "
            "KiCad-version-specific via shape"
        )
    indent = last.group(1)
    start = last.start()                      # at the newline
    body_start = last.end() - len("(via")
    end = _balanced_end(text, body_start)
    return text[start:end], end


def _balanced_end(text: str, start: int) -> int:
    """Index one past the closing paren of the block starting at ``text[start]``."""
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        char = text[i]
        if in_string:
            if char == '"' and text[i - 1] != "\\":
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unterminated (via ...) block")


def _net_id_for(text: str, net_name: str):
    for match in re.finditer(r'\(net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\)', text):
        if match.group(2).replace('\\"', '"') == net_name:
            return int(match.group(1))
    return None
