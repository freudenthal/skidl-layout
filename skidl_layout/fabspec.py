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

import math
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
# Voltage-banded conductor spacing (Phase 8, WS-E) -- MEASURED, NEVER ENFORCED
# --------------------------------------------------------------------------

#: **IPC-2221B, Table 6-1 "Electrical Conductor Spacing"**, minimum spacing in
#: **mm** by voltage band (V, DC or AC peak, between the two conductors).
#:
#: ⚠⚠ **This table is an INPUT, not a recalled number.** Phase 8's plan (WS-E)
#: is explicit that a number which cannot be cited must not be written down in a
#: manufacturability check. Every row below is transcribed from two independent
#: published renderings of Table 6-1, which agree on every value in every band
#: at or below 500 V:
#:
#:   - Sierra Circuits / protoexpress, "Applying IPC-2221 Standards in Circuit
#:     Board Design" -- https://www.protoexpress.com/blog/ipc-2221-circuit-board-design/
#:   - smpspowersupply.com, "IPC-2221B PCB Trace Spacing / Clearance by Voltage"
#:     -- https://www.smpspowersupply.com/ipc2221pcbclearance.html
#:
#: Columns are the standard's own: **B1** internal conductors, **B2** external
#: conductors uncoated at sea level to 3050 m, **B4** external conductors with a
#: permanent polymer coating. **B3** (external uncoated ABOVE 3050 m) is
#: deliberately ABSENT: only one of the two sources carried it, this stack has no
#: altitude input, and a single-sourced number in a fab check is exactly what the
#: plan forbids.
#:
#: Each entry is ``(band_max_volts, spacing_mm)``, ascending; a voltage is graded
#: by the first band whose maximum it does not exceed.
IPC2221B_TABLE_6_1_MM: dict = {
    # B1 -- internal conductors
    "B1": [(15.0, 0.05), (30.0, 0.05), (50.0, 0.1), (100.0, 0.1),
           (150.0, 0.2), (170.0, 0.2), (250.0, 0.2), (300.0, 0.2),
           (500.0, 0.25)],
    # B2 -- external conductors, uncoated, sea level to 3050 m. THE DEFAULT:
    # every board this stack ships is 2-layer, external, and unconformally
    # coated (soldermask is not a "permanent polymer coating" in B4's sense --
    # B4 means a conformal coat applied over the assembled board).
    "B2": [(15.0, 0.1), (30.0, 0.1), (50.0, 0.6), (100.0, 0.6),
           (150.0, 0.6), (170.0, 1.25), (250.0, 1.25), (300.0, 1.25),
           (500.0, 2.5)],
    # B4 -- external conductors with a permanent polymer coating
    "B4": [(15.0, 0.05), (30.0, 0.05), (50.0, 0.13), (100.0, 0.13),
           (150.0, 0.4), (170.0, 0.4), (250.0, 0.4), (300.0, 0.4),
           (500.0, 0.8)],
}

#: Above 500 V the standard gives a per-volt slope instead of a band. **Only
#: B2's is recorded**, because it is the only one both sources state
#: ("2.5 + (V - 500) x 0.005 mm"). A net above 500 V in a column with no slope
#: here is reported as ``required_mm=None`` with a reason -- not silently graded
#: at the 500 V row, and not given an invented number.
IPC2221B_ABOVE_500V_MM_PER_VOLT: dict = {"B2": 0.005}

#: Which column :func:`fab_check` grades against when the caller names none.
DEFAULT_SPACING_COLUMN = "B2"


def resolve_spacing_column(
    declared_column=None,
    conformal_coating=None,
    source: str | None = None,
) -> dict:
    """Which Table 6-1 column a board is graded against, and **who said so**.

    Power-layout Phase 12, WS-5. ``spacing_column=`` has existed since Phase 8
    and nothing ever *chose* it: the column lived in a driver's argument list,
    which means the most consequential input to the spacing judge was the one
    input no board could state about itself.

    ⚠⚠ **This is a manufacturing declaration, not a compliance shortcut.**
    Column B4 asks **0.13 mm** where B2 asks **0.6 mm** at 72 V -- a 4.6x
    relaxation -- and it is only legitimate if a permanent polymer conformal
    coat is actually applied over the assembled board. Soldermask is **not**
    such a coating (see :data:`IPC2221B_TABLE_6_1_MM`'s B2 note). Declaring a
    coat that is not applied turns a real clearance into a paper one, so the
    returned record always names ``source`` -- the same rule
    ``power_clearance``'s reasons follow when they record that a voltage is a
    rating rather than a measurement.

    Resolution order, deliberately the same explicit-beats-implicit shape as
    :func:`~skidl_layout.engine._resolve_power_score`:

    1. ``declared_column`` -- an explicit column name wins outright, including
       over a contradicting ``conformal_coating`` (the contradiction is
       *recorded*, never silently resolved);
    2. ``conformal_coating=True`` -> ``"B4"``; ``False`` -> ``"B2"``;
    3. nothing declared -> :data:`DEFAULT_SPACING_COLUMN` (``"B2"``), the
       uncoated external-conductor column every board in this stack has always
       been graded at.

    Args:
        declared_column: an explicit Table 6-1 column name, or ``None``.
        conformal_coating: the board's own claim about itself, or ``None`` for
            "did not say".
        source: where the declaration came from (a module name, a filename, a
            person). Recorded verbatim; never interpreted.

    Returns:
        ``{"column", "declared", "conformal_coating", "source", "reason",
        "conflict"}``. ``conflict`` is True only when an explicit column and an
        explicit coating disagree.

    Raises:
        ValueError: if ``declared_column`` is not a column this stack carries a
            transcribed table for. ⛔ An unknown column must not fall back to
            the default -- silently grading a board at B2 because "B3" was
            misspelled is precisely the class of quiet-wrong-number this arc
            keeps paying for.
    """
    known = tuple(sorted(IPC2221B_TABLE_6_1_MM))
    declared = None if declared_column is None else str(declared_column).strip().upper()
    if declared is not None and declared not in IPC2221B_TABLE_6_1_MM:
        raise ValueError(
            f"unknown IPC-2221B Table 6-1 column {declared_column!r}; this stack "
            f"carries {', '.join(known)} (B3 is deliberately absent -- see "
            "IPC2221B_TABLE_6_1_MM)")

    coated = None if conformal_coating is None else bool(conformal_coating)
    implied = None if coated is None else ("B4" if coated else "B2")

    if declared is not None:
        column = declared
        conflict = implied is not None and implied != declared
        reason = f"declared column {declared}"
        if conflict:
            reason += (f"; ⚠ conflicts with conformal_coating={coated} which "
                       f"implies {implied} -- the explicit column wins and the "
                       "disagreement is recorded")
    elif implied is not None:
        column, conflict = implied, False
        reason = (f"conformal_coating={coated} -> column {implied} "
                  + ("(permanent polymer coating applied over the assembly)"
                     if coated else "(uncoated external conductors)"))
    else:
        column, conflict = DEFAULT_SPACING_COLUMN, False
        reason = (f"nothing declared -> default column {DEFAULT_SPACING_COLUMN} "
                  "(external conductors, uncoated)")

    if source:
        reason += f"  [declared by {source}]"
    return {"column": column, "declared": declared,
            "conformal_coating": coated, "source": source,
            "reason": reason, "conflict": conflict}


def measure_voltage_spacing(
    pcb_path: str,
    net_voltages: dict,
    column: str = DEFAULT_SPACING_COLUMN,
) -> list:
    """What Table 6-1 asks of each voltage-carrying net, and what the board gives.

    One row per net in ``net_voltages`` that actually exists on the board::

        {net, volts, column, required_mm, measured_mm, nearest_net,
         meets_requirement, note,
         limiting_pair, limiting_objects, placement_bound, same_footprint}

    ``measured_mm`` is the **smallest copper-edge-to-copper-edge gap** between
    that net and any *other* net, computed from the board's own geometry --
    track segments (same layer only), vias (all layers) and SMD pads (same
    layer). Track widths, via diameters and pad outlines are all taken off the
    edges, not the centrelines, so the number is a real clearance.

    The last four fields say **what kind of copper** set that number and who
    could move it -- see :func:`_limiting_pair_facts`. A short gap between two
    pads of one footprint is the package's pin pitch and no lever in this stack
    can reach it; a short gap between two tracks is the router's to fix. Phase 8
    and Phase 10 both reported the number without the distinction and the
    difference turned out to be most of the remaining gap.

    ⚠ **Pad corners are modelled** (Phase 11). A ``roundrect``/``oval``/``circle``
    pad is measured against its real outline, not its bounding rectangle:
    :func:`_pad_corner_radius` explains why the rest of this package does the
    opposite and why spacing must not. This makes gaps at pad corners *larger*
    than Phase 8/Phase 10 reported, and those earlier numbers were pessimistic.

    ⚠⚠ **Three limitations, stated rather than papered over.**

    1. **Poured zones are not measured.** KRT writes ``(fill yes)`` with
       ``filled_polygon_count 0`` and KiCad fills at open time, so a zone's
       actual copper simply is not in the file this reads. On a poured board the
       dominant conductor on a plane net is therefore *invisible* here, and the
       gap reported for it is the gap to its tracks and vias only.
    2. **Creepage is not clearance.** Table 6-1 is through-air/over-surface
       spacing between conductors; this measures in-plane copper separation. It
       says nothing about slots, edge distance or the coating that would move a
       board from column B2 to B4.
    3. **Nothing is enforced.** This is the judge, deliberately without a lever
       (Phase-8 plan section 1.5). No geometry moves and ``FabCheckResult.ok`` is
       never downgraded by a row that fails.
    """
    from .via_relocate import (
        _board_geometry, _pad_corners, _point_to_pad_mm, _point_to_segment_mm,
        _segment_to_segment_mm, _segment_to_pad_mm,
    )

    geom = _board_geometry(pcb_path)
    name_to_id = {name: nid for nid, name in geom["net_map"].items()}

    def _objects(predicate):
        out = []
        for seg in geom["segments"]:
            if predicate(seg["net_id"]):
                out.append(("seg", (seg["x1"], seg["y1"], seg["x2"], seg["y2"]),
                            seg["width"] / 2.0, seg["layer"], seg["net_id"]))
        for via in geom["vias"]:
            if predicate(via["net_id"]):
                out.append(("via", (via["x"], via["y"]), via["size"] / 2.0,
                            None, via["net_id"]))
        for pad in geom["pads"]:
            if pad.get("net_id") is not None and predicate(pad["net_id"]):
                # A roundrect/oval pad is an inset rectangle plus a radius --
                # the same (geometry, radius) shape segments and vias already
                # use, so _gap subtracts it with no special case.
                inset, radius = _pad_as_capsule(pad)
                out.append(("pad", inset, radius, _pad_layer(pad), pad["net_id"]))
        return out

    def _gap(a, b) -> float:
        """Copper-edge gap between two objects; negative means overlap."""
        kind_a, ga, ra, la, _ = a
        kind_b, gb, rb, lb, _ = b
        if la is not None and lb is not None and la != lb:
            return float("inf")            # different layers never contend
        pair = (kind_a, kind_b)
        if pair == ("seg", "seg"):
            centre = _segment_to_segment_mm(ga, gb)
        elif pair == ("seg", "via"):
            centre = _point_to_segment_mm(gb[0], gb[1], *ga)
        elif pair == ("via", "seg"):
            centre = _point_to_segment_mm(ga[0], ga[1], *gb)
        elif pair == ("via", "via"):
            centre = math.hypot(ga[0] - gb[0], ga[1] - gb[1])
        elif pair == ("seg", "pad"):
            centre = _segment_to_pad_mm(ga, gb)
        elif pair == ("pad", "seg"):
            centre = _segment_to_pad_mm(gb, ga)
        elif pair == ("via", "pad"):
            centre = _point_to_pad_mm(ga[0], ga[1], gb)
        elif pair == ("pad", "via"):
            centre = _point_to_pad_mm(gb[0], gb[1], ga)
        else:                               # pad <-> pad
            centre = min(
                _segment_to_pad_mm((*_pad_corners(ga)[i],
                                    *_pad_corners(ga)[(i + 1) % 4]), gb)
                for i in range(4))
        return centre - ra - rb

    rows: list = []
    for net, volts in sorted((net_voltages or {}).items()):
        required = ipc2221_spacing_mm(volts, column=column)
        net_id = name_to_id.get(net)
        row = {
            "net": net, "volts": float(volts), "column": column,
            "required_mm": required, "measured_mm": None, "nearest_net": None,
            "meets_requirement": None, "note": "",
            "limiting_pair": None, "limiting_objects": None,
            "placement_bound": None, "same_footprint": None,
        }
        if net_id is None:
            row["note"] = "net is not declared on this board"
            rows.append(row)
            continue
        mine = _objects(lambda nid, want=net_id: nid == want)
        theirs = _objects(lambda nid, want=net_id: nid != want and nid != 0)
        if not mine or not theirs:
            row["note"] = ("no measurable copper on this net (a poured plane "
                           "writes no polygons into the file)" if not mine
                           else "no other net carries copper")
            rows.append(row)
            continue

        best = float("inf")
        nearest = None
        best_pair = (None, None)
        for a in mine:
            for b in theirs:
                gap = _gap(a, b)
                if gap < best:
                    best = gap
                    nearest = geom["net_map"].get(b[4])
                    best_pair = (a, b)
        if best == float("inf"):
            row["note"] = "no same-layer contender found"
            rows.append(row)
            continue
        row["measured_mm"] = round(best, 4)
        row["nearest_net"] = nearest
        row.update(_limiting_pair_facts(*best_pair))
        if required is None:
            row["note"] = (f"IPC-2221B Table 6-1 column {column} states no "
                           f"spacing for {volts:g}V (above 500V with no "
                           "recorded slope for this column)")
        else:
            row["meets_requirement"] = best >= required - 1e-6
        rows.append(row)
    return rows


def _pad_layer(pad) -> str:
    for name in pad.get("layers") or []:
        if name.endswith(".Cu"):
            return name
    return "F.Cu"


def _describe_object(obj) -> str:
    kind, geometry, radius, layer, _ = obj
    if kind == "pad":
        return f"pad {geometry.get('ref')}.{geometry.get('number')} on {layer}"
    if kind == "via":
        return f"via at ({geometry[0]:.2f}, {geometry[1]:.2f})"
    return f"track on {layer}, {radius * 2.0:.4f}mm wide"


def _limiting_pair_facts(a, b) -> dict:
    """**What kind of copper** sets a net's smallest gap, and who could move it.

    A spacing number without this is not actionable, and Phase 11 measured why:
    ``lt3758_flyback``'s 72 V ``VIN`` sat at 0.2000 mm against ``UVLO`` and no
    clearance setting could shift it, because the limiting pair is **pads
    ``U1.9`` and ``U1.10`` -- two adjacent pins of the same TSSOP-16**. That gap
    is the package's pin pitch. No router widens it, no placer widens it, and
    ``power_clearance``'s lever cannot either: the only fixes are a different
    package or a different pin assignment, both of them schematic decisions.

    Three fields, each a plain fact rather than a judgement:

    - ``limiting_pair`` -- ``"pad<->pad"``, ``"track<->pad"``, ``"via<->track"``…
    - ``placement_bound`` -- both sides are pads, so **routing** cannot change
      it. Placement might, unless…
    - ``same_footprint`` -- …both pads belong to one footprint, in which case
      **nothing in this stack can change it**. This is the strongest statement
      the judge can make and the one worth acting on.
    """
    if a is None or b is None:
        return {}
    kinds = {"seg": "track", "via": "via", "pad": "pad"}
    both_pads = a[0] == "pad" and b[0] == "pad"
    same_fp = bool(both_pads and a[1].get("ref") and a[1].get("ref") == b[1].get("ref"))
    return {
        "limiting_pair": f"{kinds.get(a[0], a[0])}<->{kinds.get(b[0], b[0])}",
        "limiting_objects": f"{_describe_object(a)} | {_describe_object(b)}",
        "placement_bound": both_pads,
        "same_footprint": same_fp,
    }


def ipc2221_spacing_mm(volts: float, column: str = DEFAULT_SPACING_COLUMN):
    """Minimum conductor spacing (mm) IPC-2221B Table 6-1 asks for at ``volts``.

    Returns ``None`` when the table cannot answer -- an unknown column, or a
    voltage above 500 V in a column whose per-volt slope is not recorded here.
    ``None`` means "not stated", which is a different claim from "no spacing
    required", and callers must keep the two apart.
    """
    bands = IPC2221B_TABLE_6_1_MM.get(column)
    if bands is None:
        return None
    v = abs(float(volts or 0.0))
    for band_max, spacing in bands:
        if v <= band_max + 1e-9:
            return spacing
    slope = IPC2221B_ABOVE_500V_MM_PER_VOLT.get(column)
    if slope is None:
        return None
    return bands[-1][1] + (v - bands[-1][0]) * slope


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
    #: Clearance DRC graded at the fab's **published floor** -- the number that
    #: decides ``ok``. See :func:`fab_check` for why this is not the design
    #: clearance.
    drc_violation_count: int = 0
    checked: list = field(default_factory=list)  # rule names actually evaluated
    edge_check_coarse: bool = False  # True when edge-keepout fell back to bbox
    notes: list = field(default_factory=list)
    #: The clearance ``drc_violation_count`` was graded at (mm), so a reader
    #: never has to guess which tier produced the number.
    drc_clearance_mm: float | None = None
    #: **Advisory only.** The same board graded at the spec's *design*
    #: clearance, which is tighter than anything the router was ever told and
    #: therefore reports grazes on copper that breaches no fab rule. Never folded
    #: into ``ok``. ``None`` when the two tiers coincide (nothing to say).
    design_clearance_drc_count: int | None = None
    design_clearance_mm: float | None = None
    #: **Advisory only** (Phase 8, WS-E). One row per voltage-carrying net:
    #: what IPC-2221B Table 6-1 asks of it and what the board's copper measures.
    #: Never folded into ``ok`` -- WS-E ships the judge, not the lever.
    voltage_spacing: list = field(default_factory=list)

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
            lines.append(
                f"  clearance DRC: {self.drc_violation_count} violation(s) "
                f"(graded at the fab floor "
                f"{self.drc_clearance_mm:g}mm)" if self.drc_clearance_mm
                else f"  clearance DRC: {self.drc_violation_count} violation(s)")
        if self.design_clearance_drc_count:
            lines.append(
                f"  advisory: {self.design_clearance_drc_count} graze(s) at the "
                f"DESIGN clearance {self.design_clearance_mm:g}mm "
                "(not a fab-rule breach; does not affect ok)")
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
            "drc_clearance_mm": self.drc_clearance_mm,
            "design_clearance_drc_count": self.design_clearance_drc_count,
            "design_clearance_mm": self.design_clearance_mm,
            "voltage_spacing": [dict(r) for r in self.voltage_spacing],
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
    drc_clearance_mm: float | None = None,
    net_voltages: dict | None = None,
    spacing_column: str = DEFAULT_SPACING_COLUMN,
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
      ``check_drc.py`` graded at the fab's **published floor**
      (``spec.min_clearance_mm``, or ``drc_clearance_mm`` when given) and fold
      the count into ``ok``.

    ⚠ **Which clearance this grades at, and why it changed (Phase 8).** Until
    Phase 8 this rule graded at ``spec.clearance_mm`` -- the *design* clearance
    the board is drawn to (0.25 mm on ``oshpark-2l``) -- while KRT's own
    ``check_drc`` gate grades at the manufacturing floor the copper was actually
    routed to (0.1768 mm there). The two therefore disagreed by **46 violations
    on the Phase-6 boost, 84 on the flyback and 47 on the inverting board, with
    KRT reporting 0 on all three**, and ``fab_must_pass=True`` was unusable on
    every board this stack ships. KRT's own rule says it plainly: grading
    stricter than the route used manufactures phantom sub-clearance grazes.

    The floor is also the *consistent* tier -- every other rule here already
    grades against a ``min_*`` published limit (``min_track_mm``,
    ``min_drill_mm``, ``min_annular_ring_mm``); clearance was the lone rule
    reading the nominal value instead. The design-clearance count is still
    computed and reported as ``design_clearance_drc_count``, **advisory only**,
    because "how much copper sits below the width we drew to" is a real
    question -- it is just not a manufacturability verdict.

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

    # -- via-in-pad (Phase 5, WS-4) --------------------------------------
    # A via whose centre lands inside an SMD pad is via-in-pad, which needs
    # filled-and-capped plating the fab must actually offer. Round 1 checked
    # only blind/buried here, so a thermal-via array under an exposed pad --
    # exactly what emit_power_copper(thermal_vias=True) can now produce --
    # would have been graded clean on a spec that forbids it.
    if not spec.via_in_pad:
        result.checked.append("via_in_pad")
        pads = _smd_copper_pads(board)
        if pads:
            for via in board.search("via"):
                at = _find_child(via, "at")
                if at is None or len(at) < 3:
                    continue
                vx, vy = float(at[1]), float(at[2])
                for pad in pads:
                    if _point_in_pad(vx, vy, pad):
                        result.violations.append(FabViolation(
                            "via_in_pad",
                            f"via@({vx:.2f},{vy:.2f}) inside pad "
                            f"{pad['ref']}.{pad['number']}",
                            1.0, 0.0))
                        break

    # -- voltage-banded spacing (Phase 8, WS-E) -- MEASURED, NOT ENFORCED ---
    # Deliberately does NOT append to `violations`, so `ok` cannot move. WS-E
    # ships the judge; the lever is a later phase's problem, and enforcing a
    # spacing rule the placer has no way to satisfy would only make
    # fab_must_pass unusable again in a new way.
    if net_voltages:
        result.checked.append("voltage_spacing")
        try:
            result.voltage_spacing = measure_voltage_spacing(
                pcb_path, net_voltages, column=spacing_column)
        except Exception as exc:  # noqa: BLE001 - a measurement must not crash
            result.notes.append(f"voltage spacing skipped: {exc}")
        else:
            short = [r for r in result.voltage_spacing
                     if r.get("meets_requirement") is False]
            for row in short:
                result.notes.append(
                    f"voltage spacing (advisory, not enforced): {row['net']} at "
                    f"{row['volts']:g}V wants {row['required_mm']:g}mm "
                    f"(IPC-2221B Table 6-1 col {row['column']}) but measures "
                    f"{row['measured_mm']:g}mm to {row['nearest_net']}")

    # -- clearance DRC, graded at the fab's PUBLISHED floor ----------------
    # Phase-8 fix. This used to grade at ``spec.clearance_mm`` -- the *design*
    # clearance -- which is stricter than anything the router is ever told, so
    # it manufactured phantom grazes on copper that breaches no fab rule and
    # made ``fab_must_pass=True`` unusable on every board the stack ships.
    floor = float(drc_clearance_mm) if drc_clearance_mm is not None else float(
        spec.min_clearance_mm)
    design = float(spec.clearance_mm)
    if run_drc:
        try:
            _krt, resolved = _resolve_krt_for_drc(krt_dir)
            if resolved is not None:
                result.drc_violation_count = _graded_drc(
                    _krt, resolved, pcb_path, floor, timeout_s)
                result.drc_clearance_mm = floor
                result.checked.append("clearance_drc")
                # The design number stays visible as ADVICE. It is a real
                # quantity ("how much copper sits below the value we drew to"),
                # it is simply not a manufacturability verdict.
                if design > floor + 1e-9:
                    result.design_clearance_mm = design
                    result.design_clearance_drc_count = _graded_drc(
                        _krt, resolved, pcb_path, design, timeout_s)
            else:
                result.notes.append("KRT not found: clearance DRC skipped")
        except Exception as exc:  # noqa: BLE001 - grading must not crash the check
            result.notes.append(f"clearance DRC skipped: {exc}")

    return result


def _resolve_krt_for_drc(krt_dir):
    """``(krt_module, checkout_path_or_None)``. Its own function so a test can
    stand in for the whole subprocess layer without a KRT checkout."""
    from . import krt as _krt

    return _krt, _krt.find_krt(krt_dir)


def _graded_drc(krt_module, resolved: str, pcb_path: str, clearance: float,
                timeout_s: int) -> int:
    """KRT ``check_drc.py`` violation count at one explicit clearance."""
    proc = krt_module._run_krt(
        ["check_drc.py", "--clearance", f"{clearance:g}",
         os.path.abspath(pcb_path)],
        resolved, timeout_s,
    )
    return krt_module._parse_drc_output(proc.stdout)


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


def _smd_copper_pads(board) -> list:
    """Every SMD pad with copper, as board-space rectangles.

    Returns dicts of ``{ref, number, x, y, w, h, angle}`` where ``x``/``y`` are
    the pad centre in board coordinates and ``angle`` is the pad's absolute
    rotation in degrees.

    Two conventions matter and both were verified against a board this stack
    emits: a pad's ``(at ...)`` **position** is in the footprint's *unrotated*
    local frame, so it needs :func:`skidl_layout.geometry.transform_point` with
    the footprint's rotation; a pad's ``(at ... angle)`` **angle** is already
    absolute (KiCad bakes the footprint rotation into it -- a footprint at 90
    degrees has pads at 90 degrees). Paste-only apertures are skipped: an
    exposed pad's stencil openings are ``F.Paste`` rectangles with no copper,
    and counting them would flag vias that sit in no copper pad at all.
    """
    from .geometry import transform_point
    from .reader import _find_child, _find_children

    pads: list = []
    for fp in board.search("footprint"):
        fp_at = _find_child(fp, "at")
        if fp_at is None or len(fp_at) < 3:
            continue
        fx, fy = float(fp_at[1]), float(fp_at[2])
        frot = float(fp_at[3]) if len(fp_at) > 3 else 0.0
        ref = "?"
        for prop in _find_children(fp, "property"):
            if len(prop) > 2 and str(prop[1]).strip('"') == "Reference":
                ref = str(prop[2]).strip('"')
        for pad in _find_children(fp, "pad"):
            if len(pad) < 3 or str(pad[2]).strip('"') != "smd":
                continue
            layers = _find_child(pad, "layers")
            names = [str(v).strip('"') for v in layers[1:]] if layers else []
            if not any(n.endswith(".Cu") or n == "*.Cu" for n in names):
                continue
            at = _find_child(pad, "at")
            size = _find_child(pad, "size")
            if at is None or len(at) < 3 or size is None or len(size) < 3:
                continue
            cx, cy = transform_point(fx, fy, frot, float(at[1]), float(at[2]))
            rratio = _find_child(pad, "roundrect_rratio")
            pads.append({
                "ref": ref,
                "number": str(pad[1]).strip('"'),
                "x": cx, "y": cy,
                "w": float(size[1]), "h": float(size[2]),
                "angle": float(at[3]) if len(at) > 3 else frot,
                "layers": names,
                # Shape is recorded but the rectangle stays the pad's geometry
                # everywhere it was before -- see _pad_corner_radius, which is
                # the only consumer, and only for spacing.
                "shape": str(pad[3]).strip('"') if len(pad) > 3 else "rect",
                "rratio": (float(rratio[1]) if rratio is not None
                           and len(rratio) > 1 else None),
            })
    return pads


#: Fallback corner ratio for a ``roundrect`` pad that records no
#: ``roundrect_rratio``. KiCad's own default, and every footprint in this
#: stack's corpus states the token explicitly -- this is belt and braces.
DEFAULT_ROUNDRECT_RRATIO = 0.25


def _pad_corner_radius(pad: dict) -> float:
    """The pad outline's corner radius (mm); 0.0 for a true rectangle.

    ⚠ **Why this exists, and why only spacing uses it.** Every other pad
    consumer in this package models a pad as its bounding *rectangle*, and
    :func:`_point_in_pad` documents that as "the safe direction for a
    manufacturability gate". That is true for a **containment** test -- asking
    "is this via in the pad" (:mod:`skidl_layout.via_relocate`), where an
    over-large pad can only over-report a via-in-pad violation.

    It is exactly **backwards for a spacing test**. A ``roundrect`` pad's copper
    is *absent* from the corner, so measuring to the sharp bounding corner
    reports a gap **smaller** than the copper actually leaves -- a phantom
    "SHORT". Measured on ``lt3724_buck``: a 0.1524 mm ``VCC`` track passing the
    ``SGND`` pad ``CC.2`` (0805, ``roundrect_rratio 0.25``) measured **0.0634 mm**
    against the sharp corner and **0.1670 mm** against the real rounded outline,
    while KRT's own ``check_drc`` -- which models the roundrect -- reported no
    violation at the 0.1524 mm fab floor. The judge was wrong, not the router.

    ``roundrect`` -> ``rratio * min(w, h)``; ``oval``/``circle`` -> ``min(w, h)/2``
    (a stadium/circle is the limit case of a roundrect); everything else 0.0,
    which reproduces the old rectangle exactly. A ``custom`` pad's primitives are
    not parsed, so it stays a conservative rectangle rather than guessing.
    """
    shape = str(pad.get("shape") or "rect").lower()
    w, h = float(pad.get("w") or 0.0), float(pad.get("h") or 0.0)
    if shape == "roundrect":
        rratio = pad.get("rratio")
        rratio = DEFAULT_ROUNDRECT_RRATIO if rratio is None else float(rratio)
        return max(0.0, rratio) * min(w, h)
    if shape in ("oval", "circle"):
        return min(w, h) / 2.0
    return 0.0


def _pad_as_capsule(pad: dict) -> tuple:
    """``(inset_pad, radius)`` -- the pad as a rectangle swept by a radius.

    Exactly the model the rest of this module already uses for tracks and vias
    (a centreline plus a radius), so the shared distance helpers need no change:
    shrink the rectangle by the corner radius on every side, measure to *that*,
    then subtract the radius. A rect pad insets by 0 and is byte-identical to
    the previous behaviour.
    """
    r = _pad_corner_radius(pad)
    if r <= 0.0:
        return pad, 0.0
    return ({**pad,
             "w": max(float(pad["w"]) - 2.0 * r, 0.0),
             "h": max(float(pad["h"]) - 2.0 * r, 0.0)}, r)


def _point_in_pad(px: float, py: float, pad: dict, margin: float = 0.0) -> bool:
    """Is ``(px, py)`` inside ``pad``'s rectangle, the pad's rotation undone?

    ``margin`` shrinks the rectangle on every side (a positive value demands the
    point sit that far *inside* the pad edge). The rectangle is used for every
    shape: a roundrect/oval pad's true outline is smaller at the corners, so
    this over-reports rather than under-reports, which is the safe direction for
    a manufacturability gate.
    """
    import math

    dx, dy = px - pad["x"], py - pad["y"]
    radians = math.radians(pad["angle"])
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    # Inverse of geometry.transform_point's rotation.
    local_x = dx * cos_a - dy * sin_a
    local_y = dx * sin_a + dy * cos_a
    return (abs(local_x) <= pad["w"] / 2.0 - margin
            and abs(local_y) <= pad["h"] / 2.0 - margin)
