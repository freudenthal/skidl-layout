"""Fabricator capability / stackup / design-rule model (``FabSpec``).

Pure-data module: a frozen :class:`FabSpec` dataclass captures one fabricator's
published limits (copper/drill/annular minimums, board size, stackup/material)
and the routing values a board is actually drawn at (which must be >= those
minimums). One preset ships in round 1 -- :data:`OSHPARK_2L`, OSHPark's 2-layer
service -- and is the default when a spec is *requested* without naming one.

Everything downstream is opt-in behind a ``fab_spec=None`` (feature-off) knob:

- :func:`skidl_layout.write_kicad_pcb` emits a ``(stackup ...)`` block from it
  (``fabspec``-driven, WS-F2);
- :func:`skidl_layout.emit_power_copper` threads its route/pour values into KRT
  and clamps every planned power-track width up to ``min_track_mm`` (WS-F3);
- :func:`fab_check` grades a finished board against the published limits (WS-F4);
- ``skidl_eda.plan_pcb(fab=...)`` resolves + stamps + gates (WS-F5).

All lengths are **mm**. The fab tables are published in mils; the preset carries
the converted values (mil x 0.0254) with the mil source in a comment.

The dataclass is the extension point for other fabs (JLC, PCBWay, OSHPark
4-layer/flex): add another ``FabSpec(...)`` preset and register it in
:data:`_PRESETS`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

# --------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FabSpec:
    """One fabricator's design rules + stackup, all lengths in mm.

    Two tiers of copper number live here on purpose:

    - the ``min_*`` fields are the fab's *published limits* -- the floor
      :func:`fab_check` grades against;
    - ``track_width_mm`` / ``clearance_mm`` / ``via_size_mm`` / ``via_drill_mm``
      are the values a board is *actually routed at*, chosen >= the limits with
      manufacturing margin. ``__post_init__`` enforces that ordering, so a
      self-inconsistent spec never reaches a board.

    Several fields are **informational only** -- carried for the stackup emission
    / BOM metadata but NOT gated by :func:`fab_check` in round 1: ``substrate``,
    ``dielectric_constant``, ``finish``, ``soldermask_color``, ``silkscreen``,
    ``copper_weight_oz``, ``board_thickness_mm``, ``core_thickness_mm``,
    ``min_slot_mm``. Soldermask web/alignment, silkscreen line width, drill
    tolerances, milling diameters, max drill size and overlapping-drill spacing
    are not modelled at all.
    """

    name: str
    # --- copper rules (published limits: the fab_check floor) ---------------
    copper_layers: int = 2
    min_track_mm: float = 0.1524      # 6 mil trace width
    min_clearance_mm: float = 0.1524  # 6 mil trace spacing
    min_annular_ring_mm: float = 0.127   # 5 mil
    min_drill_mm: float = 0.254       # 10 mil
    min_slot_mm: float = 0.508        # 20 mil (drill slots only; informational)
    board_edge_keepout_mm: float = 0.381  # 15 mil copper-from-edge
    copper_weight_oz: float = 1.0     # ~1.4 mil = 0.035 mm foil (informational)
    # --- routing values actually used (>= the minimums above) ---------------
    track_width_mm: float = 0.3       # keep KRT's proven default
    clearance_mm: float = 0.25        # keep KRT's proven default
    via_size_mm: float = 0.6          # 0.6/0.3 -> ring 0.15 mm >= 0.127  OK
    via_drill_mm: float = 0.3
    # --- stackup / material (metadata + (stackup ...) emission) -------------
    board_thickness_mm: float = 1.6   # 63 mil nominal
    core_thickness_mm: float = 1.524  # 60 mil core
    substrate: str = "FR4 175Tg (Kingboard KB6167F)"
    dielectric_constant: float = 4.5  # at 10 MHz
    finish: str = "ENIG"              # IPC-4552
    soldermask_color: str = "purple"
    silkscreen: str = "both"          # both sides
    # --- board limits -------------------------------------------------------
    min_board_mm: tuple = (6.35, 6.35)      # 0.25 in square
    max_board_mm: tuple = (406.4, 558.8)    # 16 x 22 in
    # --- capabilities (booleans the gate can assert absent) -----------------
    blind_vias: bool = False
    buried_vias: bool = False
    via_in_pad: bool = False
    castellations: bool = False       # allowed-not-guaranteed -> treat as False

    def __post_init__(self) -> None:
        # Derived invariant: the routed values must satisfy the published floor,
        # so a spec can never ask the router to draw copper the fab rejects.
        if self.track_width_mm < self.min_track_mm - 1e-9:
            raise ValueError(
                f"{self.name}: track_width_mm ({self.track_width_mm}) < "
                f"min_track_mm ({self.min_track_mm})"
            )
        if self.clearance_mm < self.min_clearance_mm - 1e-9:
            raise ValueError(
                f"{self.name}: clearance_mm ({self.clearance_mm}) < "
                f"min_clearance_mm ({self.min_clearance_mm})"
            )
        if self.via_drill_mm < self.min_drill_mm - 1e-9:
            raise ValueError(
                f"{self.name}: via_drill_mm ({self.via_drill_mm}) < "
                f"min_drill_mm ({self.min_drill_mm})"
            )
        ring = (self.via_size_mm - self.via_drill_mm) / 2.0
        if ring < self.min_annular_ring_mm - 1e-9:
            raise ValueError(
                f"{self.name}: via annular ring ({ring:.4f}) < "
                f"min_annular_ring_mm ({self.min_annular_ring_mm})"
            )
        if self.copper_layers < 1:
            raise ValueError(f"{self.name}: copper_layers must be >= 1")

    @property
    def copper_thickness_mm(self) -> float:
        """Copper foil thickness (mm) from the weight in oz (1 oz ~= 0.035 mm)."""
        return round(self.copper_weight_oz * 0.035, 4)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

# OSHPark 2-layer service (published common/copper/drill specs, captured
# 2026-07-23). mil source in the trailing comment; value is mil x 0.0254.
OSHPARK_2L = FabSpec(
    name="oshpark-2l",
    copper_layers=2,
    min_track_mm=0.1524,        # 6 mil
    min_clearance_mm=0.1524,    # 6 mil
    min_annular_ring_mm=0.127,  # 5 mil
    min_drill_mm=0.254,         # 10 mil
    min_slot_mm=0.508,          # 20 mil
    board_edge_keepout_mm=0.381,  # 15 mil
    copper_weight_oz=1.0,       # 1 oz outer
    track_width_mm=0.3,
    clearance_mm=0.25,
    via_size_mm=0.6,            # 0.6/0.3 -> ring 0.15 >= 0.127
    via_drill_mm=0.3,
    board_thickness_mm=1.6,     # 63 mil
    core_thickness_mm=1.524,    # 60 mil
    substrate="FR4 175Tg (Kingboard KB6167F)",
    dielectric_constant=4.5,
    finish="ENIG",
    soldermask_color="purple",
    silkscreen="both",
    min_board_mm=(6.35, 6.35),      # 0.25 in
    max_board_mm=(406.4, 558.8),    # 16 x 22 in
)

_PRESETS: dict[str, FabSpec] = {
    "oshpark-2l": OSHPARK_2L,
    "oshpark": OSHPARK_2L,  # bare name -> the 2-layer default service
}


def resolve_fab_spec(spec) -> FabSpec | None:
    """Coerce a user-supplied ``fab`` argument to a :class:`FabSpec` or ``None``.

    Accepts:

    - ``None`` -> ``None`` (feature off; the byte-identical default path);
    - a :class:`FabSpec` -> returned unchanged;
    - ``True`` -> :data:`OSHPARK_2L` (the "OSHPark as default fallback" rule);
    - ``False`` -> ``None`` (explicit off);
    - a preset-name ``str`` -> the matching preset (case-insensitive).

    An unknown name raises :class:`ValueError` listing the known presets.
    """
    if spec is None or spec is False:
        return None
    if spec is True:
        return OSHPARK_2L
    if isinstance(spec, FabSpec):
        return spec
    if isinstance(spec, str):
        key = spec.strip().lower()
        if key in _PRESETS:
            return _PRESETS[key]
        known = ", ".join(sorted(_PRESETS))
        raise ValueError(f"unknown fab spec {spec!r}; known presets: {known}")
    raise TypeError(
        f"fab spec must be a FabSpec, preset name, bool, or None; got {type(spec).__name__}"
    )


def write_krt_fab_overrides(spec, path: str) -> list[str]:
    """Write ``spec``'s published floors as a KRT ``--fab-overrides`` file.

    Returns the CLI fragment ``["--fab-overrides", <path>]`` ready to hand to
    ``emit_power_copper(route_extra_args=...)`` / ``plan_pcb(route_extra_args=...)``.

    **Why this exists.** The design-rule flags :func:`emit_power_copper` passes
    KRT (``--track-width`` / ``--clearance`` / ``--via-size`` / ``--via-drill``)
    set the *nominal* values a board is drawn at.  They do **not** set the floor
    KRT's fine-pitch pad-escape ladder necks down toward -- that comes from KRT's
    own JLC-derived tier table, and on a fine-pitch part (TQFP/QFN) the escape
    routinely lands below a fab's published minimum.  That is exactly the
    limitation the FabSpec round recorded on the avalanche LT3757 board and
    deferred as "would need KRT edits".  It does not: KRT ships
    ``--fab-overrides <file>``, which pins the floor to the listed values and
    disables the automatic standard->advanced escalation.

    Measured on the ``qtpy_samd21`` canary (TQFP-32, 0.8 mm pitch, 2 layers)::

        without            69 min_track violations, 23 routed-DRC, 5 unrouted
        with this file      0 min_track violations,  0 routed-DRC, 1 unrouted

    Report-only and entirely opt-in: nothing calls this automatically, so every
    existing path stays byte-identical.
    """
    spec = resolve_fab_spec(spec)
    if spec is None:
        raise ValueError("write_krt_fab_overrides needs a FabSpec, not None")
    lines = [
        f"# KRT fab-floor overrides generated from FabSpec {spec.name!r}.",
        "# Pins KRT's fine-pitch neck-down floor to the fab's published limits",
        "# (without this, the pad-escape ladder necks below them).",
        f"track_width  = {spec.min_track_mm:g}",
        f"clearance    = {spec.min_clearance_mm:g}",
        f"via_drill    = {spec.via_drill_mm:g}",
        f"via_diameter = {spec.via_size_mm:g}",
        "",
    ]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return ["--fab-overrides", path]


# --------------------------------------------------------------------------
# The fab-limits gate (WS-F4)
# --------------------------------------------------------------------------


@dataclass
class FabViolation:
    """One design-rule breach found by :func:`fab_check`."""

    rule: str            # "min_track", "min_drill", "annular_ring", ...
    obj: str             # human locator, e.g. "segment@(12.3,4.5) net V5"
    measured: float
    limit: float

    def __str__(self) -> str:
        return (
            f"{self.rule}: {self.obj} measured {self.measured:.4f}mm "
            f"< limit {self.limit:.4f}mm"
        )


@dataclass
class FabCheckResult:
    """Outcome of :func:`fab_check` -- report-only; callers decide to fail."""

    spec_name: str
    violations: list = field(default_factory=list)
    drc_violation_count: int = 0
    checked: list = field(default_factory=list)  # rule names actually evaluated
    edge_check_coarse: bool = False  # True when edge-keepout fell back to bbox
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and self.drc_violation_count == 0

    def summary(self) -> str:
        head = (
            f"FabCheck [{self.spec_name}]: "
            + ("PASS" if self.ok else f"FAIL ({len(self.violations)} violation(s)"
               + (f" + {self.drc_violation_count} DRC" if self.drc_violation_count else "")
               + ")")
        )
        lines = [head]
        for v in self.violations:
            lines.append(f"  {v}")
        if self.drc_violation_count:
            lines.append(f"  clearance DRC: {self.drc_violation_count} violation(s) "
                         f"(graded at spec clearance)")
        if self.edge_check_coarse:
            lines.append("  (edge-keepout checked coarse: bbox only)")
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "spec": self.spec_name,
            "ok": self.ok,
            "violations": [
                {"rule": v.rule, "obj": v.obj, "measured": v.measured, "limit": v.limit}
                for v in self.violations
            ],
            "drc_violation_count": self.drc_violation_count,
            "checked": list(self.checked),
            "edge_check_coarse": self.edge_check_coarse,
            "notes": list(self.notes),
        }


def fab_check(
    pcb_path: str,
    spec: FabSpec,
    krt_dir: str | None = None,
    run_drc: bool = True,
    timeout_s: int = 900,
) -> FabCheckResult:
    """Grade a finished ``.kicad_pcb`` against ``spec``'s published limits.

    Parses the board (s-expr, reusing :mod:`skidl_layout.reader` helpers) and
    reports, per rule:

    - every routed ``(segment ... (width w))``: ``w >= min_track_mm``;
    - every ``(via (size s)(drill d))``: ``d >= min_drill_mm`` AND
      ``(s-d)/2 >= min_annular_ring_mm``;
    - board bbox (from the Edge.Cuts outline) within
      ``min_board_mm`` / ``max_board_mm``;
    - copper (segments/vias) to the board-edge outline ``>= board_edge_keepout_mm``
      (coarse bbox pre-filter, exact only on near-edge candidates; on a
      non-rectangular outline the check degrades to bbox-coarse and flags it);
    - clearance: when ``run_drc`` and a KRT checkout is discoverable, run
      ``check_drc.py`` graded at ``spec.clearance_mm`` and fold the count in.

    **Report-only**: returns a :class:`FabCheckResult`; ``.ok`` is the verdict,
    ``.violations`` the typed list. Never raises on a rule breach (only on an
    unreadable file). Capability flags (blind/buried/via-in-pad) are asserted
    absent -- a plain 2-layer board can't express them, but the assertion is
    cheap insurance.
    """
    from simp_sexp import Sexp
    from .reader import _find_child, _find_children  # s-expr helpers

    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    board = Sexp(text)

    result = FabCheckResult(spec_name=spec.name)
    net_map = _net_id_to_name(board)

    # -- tracks -----------------------------------------------------------
    result.checked.append("min_track")
    for seg in board.search("segment"):
        width_node = _find_child(seg, "width")
        if width_node is None or len(width_node) < 2:
            continue
        width = float(width_node[1])
        if width < spec.min_track_mm - 1e-6:
            net_node = _find_child(seg, "net")
            net_name = net_map.get(int(net_node[1])) if net_node and len(net_node) > 1 else "?"
            result.violations.append(FabViolation(
                "min_track", f"segment net {net_name}", width, spec.min_track_mm))

    # -- vias: drill + annular ring --------------------------------------
    result.checked.append("min_drill")
    result.checked.append("annular_ring")
    for via in board.search("via"):
        size_node = _find_child(via, "size")
        drill_node = _find_child(via, "drill")
        if size_node is None or drill_node is None:
            continue
        size = float(size_node[1])
        drill = float(drill_node[1])
        at = _find_child(via, "at")
        loc = f"via@({float(at[1]):.1f},{float(at[2]):.1f})" if at and len(at) > 2 else "via"
        if drill < spec.min_drill_mm - 1e-6:
            result.violations.append(FabViolation(
                "min_drill", loc, drill, spec.min_drill_mm))
        ring = (size - drill) / 2.0
        if ring < spec.min_annular_ring_mm - 1e-6:
            result.violations.append(FabViolation(
                "annular_ring", loc, ring, spec.min_annular_ring_mm))

    # -- board size + edge keepout ---------------------------------------
    outline = _edge_bbox(board, _find_children)
    if outline is not None:
        result.checked.append("board_size")
        result.checked.append("edge_keepout")
        x0, y0, x1, y1 = outline
        bw, bh = abs(x1 - x0), abs(y1 - y0)
        min_w, min_h = spec.min_board_mm
        max_w, max_h = spec.max_board_mm
        if bw + 1e-6 < min_w or bh + 1e-6 < min_h:
            result.violations.append(FabViolation(
                "board_size_min", f"board {bw:.2f}x{bh:.2f}mm", min(bw, bh), min(min_w, min_h)))
        if bw > max_w + 1e-6 or bh > max_h + 1e-6:
            result.violations.append(FabViolation(
                "board_size_max", f"board {bw:.2f}x{bh:.2f}mm", max(bw, bh), max(max_w, max_h)))
        # copper-to-edge: coarse bbox distance of each segment endpoint / via
        # center to the outline bbox. Round-1 uses the axis-aligned bbox; a
        # non-rectangular outline is flagged coarse (still catches gross
        # encroachment, never false-passes a compliant board).
        keep = spec.board_edge_keepout_mm
        is_rect = _outline_is_rectangular(board, _find_children)
        if not is_rect:
            result.edge_check_coarse = True
            result.notes.append("non-rectangular outline: edge-keepout is bbox-coarse")
        for px, py, label in _copper_points(board, net_map):
            d = min(px - x0, x1 - px, py - y0, y1 - py)
            if d < keep - 1e-6:
                result.violations.append(FabViolation(
                    "edge_keepout", label, max(d, 0.0), keep))
    else:
        result.notes.append("no Edge.Cuts outline found: board-size + edge-keepout skipped")

    # -- capability flags (assert absent) --------------------------------
    result.checked.append("capabilities")
    if not spec.blind_vias and not spec.buried_vias:
        for via in board.search("via"):
            if _find_child(via, "blind") is not None or _find_child(via, "buried") is not None:
                at = _find_child(via, "at")
                loc = f"via@({float(at[1]):.1f},{float(at[2]):.1f})" if at and len(at) > 2 else "via"
                result.violations.append(FabViolation("blind_buried_via", loc, 1.0, 0.0))

    # -- clearance DRC graded at the spec clearance ----------------------
    if run_drc:
        try:
            from . import krt as _krt
            resolved = _krt.find_krt(krt_dir)
            if resolved is not None:
                proc = _krt._run_krt(
                    ["check_drc.py", "--clearance", f"{spec.clearance_mm:g}",
                     os.path.abspath(pcb_path)],
                    resolved, timeout_s,
                )
                result.drc_violation_count = _krt._parse_drc_output(proc.stdout)
                result.checked.append("clearance_drc")
            else:
                result.notes.append("KRT not found: clearance DRC skipped")
        except Exception as exc:  # noqa: BLE001 - grading must not crash the check
            result.notes.append(f"clearance DRC skipped: {exc}")

    return result


# -- fab_check parse helpers -------------------------------------------------


def _net_id_to_name(board) -> dict:
    net_map: dict[int, str] = {}
    for node in board:
        if isinstance(node, list) and len(node) >= 3 and node[0] == "net":
            try:
                net_map[int(node[1])] = str(node[2]).strip('"')
            except (ValueError, TypeError):
                continue
    return net_map


def _edge_bbox(board, find_children):
    """Axis-aligned bbox (x0,y0,x1,y1) of all Edge.Cuts graphics, or None."""
    xs: list[float] = []
    ys: list[float] = []
    for tag in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"):
        for node in board.search(tag):
            layer = None
            for child in node:
                if isinstance(child, list) and child and child[0] == "layer":
                    layer = str(child[1]).strip('"') if len(child) > 1 else None
            if layer != "Edge.Cuts":
                continue
            for child in node:
                if isinstance(child, list) and child and child[0] in ("start", "end", "center", "mid"):
                    if len(child) >= 3:
                        xs.append(float(child[1]))
                        ys.append(float(child[2]))
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _outline_is_rectangular(board, find_children) -> bool:
    """True if the Edge.Cuts outline is a single gr_rect (axis-aligned box)."""
    rects = [n for n in board.search("gr_rect")
             if any(isinstance(c, list) and c and c[0] == "layer"
                    and len(c) > 1 and str(c[1]).strip('"') == "Edge.Cuts" for c in n)]
    lines = [n for n in board.search("gr_line")
             if any(isinstance(c, list) and c and c[0] == "layer"
                    and len(c) > 1 and str(c[1]).strip('"') == "Edge.Cuts" for c in n)]
    arcs = [n for n in board.search("gr_arc")
            if any(isinstance(c, list) and c and c[0] == "layer"
                   and len(c) > 1 and str(c[1]).strip('"') == "Edge.Cuts" for c in n)]
    if rects and not arcs:
        return True
    # Exactly 4 axis-aligned lines forming a box also counts as rectangular.
    if len(lines) == 4 and not arcs:
        return True
    return False


def _copper_points(board, net_map):
    """Yield (x, y, label) for segment endpoints and via centers."""
    from .reader import _find_child
    for seg in board.search("segment"):
        for key in ("start", "end"):
            node = _find_child(seg, key)
            if node is not None and len(node) >= 3:
                net_node = _find_child(seg, "net")
                net_name = net_map.get(int(net_node[1])) if net_node and len(net_node) > 1 else "?"
                yield float(node[1]), float(node[2]), f"segment net {net_name}"
    for via in board.search("via"):
        at = _find_child(via, "at")
        if at is not None and len(at) >= 3:
            yield float(at[1]), float(at[2]), f"via@({float(at[1]):.1f},{float(at[2]):.1f})"
