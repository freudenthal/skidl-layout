"""Opt-in bridge: PowerRoutePlan -> real copper (wide power tracks + poured planes).

``plan_power_routes`` (``power.py``) already *plans* per-net widths and
strategies, but ``write_kicad_pcb`` emits only footprints + Edge.Cuts and the
KRT adapter routes every net at the signal width with no zones. This module
spends that plan: it emits the placed board, routes signals + wide power tracks
via KRT (``route.py --power-nets``), then pours the plane/pour-strategy nets as
genuine ``(zone ... (fill yes))`` copper via ``route_planes.py``, and grades the
final board.

Like :func:`skidl_layout.krt.evaluate_routability`, this is **request-only**: it
is never called from ``plan_layout`` / ``evaluate_circuit`` / ``generate()``.
Defaults elsewhere stay byte-identical. KRT is a subprocess, never imported.

Ordering follows ``KiCadRoutingTools/.claude/skills/plan-pcb-routing/SKILL.md``
(Steps 2 -> 3): route signals FIRST with plane nets excluded and wide power
carried inside the route via ``--power-nets``, then pour planes LAST so their
stitching vias adapt around the finished tracks (SKILL Step 8: on a dense
2-layer board treat B.Cu as a real routing layer and pour GND around the routes
afterwards -- exactly this route->pour sequence).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from . import krt
from .routability import RoutabilityFeedback

logger = logging.getLogger(__name__)

# route_intent.strategy -> handling
_PLANE_STRATEGIES = {"plane", "pour"}
_WIDE_STRATEGIES = {"wide_trunk", "trunk", "internal_rail"}
# "fanout_only" and anything else: no special power treatment.

#: Above this peak node voltage (Phase 6) a poured or current-widened net earns
#: a report-only creepage/clearance warning. Nothing in this stack models
#: creepage geometry; the warning exists so a board that needs it says so
#: instead of passing silently. 30 V is the usual "beyond this, spacing tables
#: start to matter" line (IPC-2221 Table 6-1 crosses into its first widened
#: band there); it is a *notice* threshold, not a design rule.
CREEPAGE_WARN_VOLTS = 30.0


@dataclass
class PowerCopperResult:
    """Outcome of :func:`emit_power_copper` (parsed, never raw bytes)."""

    routed_pcb_path: str
    plane_nets: list[str] = field(default_factory=list)
    plane_layers: list[str] = field(default_factory=list)
    width_map: dict[str, float] = field(default_factory=dict)
    emitted_widths: dict[str, float] = field(default_factory=dict)
    plane_summary: dict = field(default_factory=dict)
    feedback: RoutabilityFeedback | None = None
    warnings: list[str] = field(default_factory=list)
    #: Supply nets the pour policy promoted from a trunk to poured copper
    #: (Phase 4). Empty on the default path.
    promoted_nets: list[str] = field(default_factory=list)
    #: Zones actually written per net, read back off the final board. Empty
    #: when nothing was poured.
    zones_by_net: dict[str, int] = field(default_factory=dict)
    #: The thermal-via array's outcome (Phase 5), or ``None`` when
    #: ``thermal_vias=False``. On the shipped ``oshpark-2l`` spec this is the
    #: **refusal**: ``refused=True``, ``count=0``, with the reason recorded.
    thermal_vias: dict | None = None
    #: Per-net sizing record (Phase 6), one row per net a current was measured
    #: for -- ``{net: {i_rms_a, ipc_width_mm, applied_width_mm, applied}}``.
    #: A net with a current but no plan entry (the SW node, a signal net) is
    #: recorded with ``applied=False``: measured, deliberately not widened.
    #: Empty on the default path.
    current_widths: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = ["Power copper emitted:"]
        # planned -> emitted per wide-trace net (the honesty artifact)
        for net in sorted(self.width_map):
            planned = self.width_map[net]
            emitted = self.emitted_widths.get(net)
            emitted_str = f"{emitted:.2f}mm" if emitted is not None else "no trace"
            lines.append(f"  wide {net}: planned {planned:.2f}mm -> {emitted_str}")
        # measured current -> the width the physics asks for (Phase 6)
        for net in sorted(self.current_widths):
            row = self.current_widths[net]
            verdict = (
                f"applied {row['applied_width_mm']:.2f}mm"
                if row.get("applied") else "recorded only (not in the power plan)"
            )
            lines.append(
                f"  current {net}: {row['i_rms_a']:.3f}A -> IPC "
                f"{row['ipc_width_mm']:.2f}mm, {verdict}"
            )
        thermal = self.thermal_vias or {}
        if thermal:
            if thermal.get("refused"):
                lines.append(f"  thermal vias: REFUSED -- {thermal.get('reason')}")
            else:
                lines.append(
                    f"  thermal vias: {thermal.get('count', 0)} on "
                    f"{thermal.get('net')} ({thermal.get('reason')})"
                )
        for net, layer in zip(self.plane_nets, self.plane_layers):
            total = self.plane_summary.get("zone_count", 0)
            # Per-net when we could read it back, so a promotion that poured
            # nothing shows a 0 rather than hiding behind the board total.
            mine = self.zones_by_net.get(net)
            kind = "promoted" if net in self.promoted_nets else "plane"
            count = (
                f"{mine} zone(s)" if mine is not None else f"{total} zone(s) total"
            )
            lines.append(f"  {kind} {net}: pour on {layer} ({count})")
        if self.feedback is not None:
            lines.append(self.feedback.summary())
        if self.warnings:
            lines.append("Warnings:")
            for warning in self.warnings:
                lines.append(f"  {warning}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "routed_pcb_path": self.routed_pcb_path,
            "plane_nets": list(self.plane_nets),
            "plane_layers": list(self.plane_layers),
            "width_map": dict(self.width_map),
            "emitted_widths": dict(self.emitted_widths),
            "plane_summary": dict(self.plane_summary),
            "feedback": self.feedback.to_dict() if self.feedback else None,
            "warnings": list(self.warnings),
            "promoted_nets": list(self.promoted_nets),
            "zones_by_net": dict(self.zones_by_net),
            "thermal_vias": dict(self.thermal_vias) if self.thermal_vias else None,
            "current_widths": {k: dict(v) for k, v in self.current_widths.items()},
        }


def _spec_route_kwargs(spec) -> dict:
    """Route.py design-rule kwargs for ``spec`` (empty dict when off).

    Empty when ``spec is None`` so the route call is byte-identical argv to the
    pre-FabSpec path.
    """
    if spec is None:
        return {}
    return {
        "track_width": spec.track_width_mm,
        "clearance": spec.clearance_mm,
        "via_size": spec.via_size_mm,
        "via_drill": spec.via_drill_mm,
        "board_edge_clearance": spec.board_edge_keepout_mm,
    }


def _spec_pour_kwargs(spec) -> dict:
    """route_planes.py design-rule kwargs for ``spec`` (empty dict when off).

    Only the via/track/clearance floor is set from the spec; the zone-clearance
    and min-thickness pour defaults (KRT 0.2/0.1) are kept unless the spec's own
    clearance is tighter (it never is for OSHPark), so pours stay proven.
    """
    if spec is None:
        return {}
    return {
        "track_width": spec.track_width_mm,
        "clearance": spec.clearance_mm,
        "via_size": spec.via_size_mm,
        "via_drill": spec.via_drill_mm,
    }


def _plane_layer_for(
    intent,
    board_layers: int,
    promoted: bool = False,
    supply_pour_layer: str | None = None,
) -> str:
    """Copper layer to pour a plane/pour-strategy net on.

    4+ layers: honour the plan's ``suggested_layer`` (e.g. In1.Cu). 2-layer:
    force B.Cu so F.Cu stays free for signal routing (``_suggest_layer`` biases
    signals to F.Cu; a GND pour belongs on the back), matching SKILL Step 8.

    ``promoted`` (Phase 4, default ``False`` -> byte-identical) marks a *supply*
    net the pour policy lifted out of the trunk ladder. On a 2-layer board those
    pour on **F.Cu** while GND keeps B.Cu -- Figure 11's shape: a ground pour
    underneath, VIN/VOUT regions on top. Several promoted nets share F.Cu; KRT
    Voronoi-partitions a multi-net layer. ``supply_pour_layer`` overrides that
    choice (``"B.Cu"`` shares the back copper with GND -- the measured fallback
    if F.Cu regions cost routing completion).
    """
    if board_layers >= 4:
        return intent.layer
    if promoted:
        return supply_pour_layer or "F.Cu"
    return "B.Cu"


def emit_power_copper(
    result,
    circuit,
    fp_lib_dirs: list[str],
    workdir: str,
    krt_dir: str | None = None,
    board_layers: int = 2,
    lib_table: dict | None = None,
    overrides: dict[str, float] | None = None,
    add_gnd_vias: bool = False,
    gnd_via_distance: float = 2.0,
    route_extra_args: list[str] | None = None,
    timeout_s: int = 900,
    strict_missing_footprints: bool = False,
    fab_spec=None,
    pour_policy=None,
    include_suffixed: bool = False,
    supply_pour_layer: str | None = None,
    route_promoted: bool = True,
    zone_clearance: float | None = None,
    min_thickness: float | None = None,
    thermal_vias: bool = False,
    thermal_via_pitch_mm: float | None = None,
    thermal_via_edge_margin_mm: float = 0.0,
    net_currents: dict[str, float] | None = None,
    current_delta_t_c: float = 10.0,
    current_max_width_mm: float | None = None,
    net_voltages: dict[str, float] | None = None,
) -> PowerCopperResult:
    """Emit real power copper for a placed ``result``; grade the final board.

    Reads ``result.power_plan.route_intents`` to decide, per net:

    - ``plane`` / ``pour`` -> poured as a real zone (``pour_planes``).
    - ``wide_trunk`` / ``trunk`` / ``internal_rail`` -> routed at
      ``suggested_width_mm`` via ``--power-nets`` (``overrides`` win).
    - ``fanout_only`` / other -> no special treatment.

    Writes the placed board, routes signals + wide power (plane nets excluded),
    pours the plane nets, grades the poured board, and sets
    ``result.routability`` to the final feedback. Returns a
    :class:`PowerCopperResult` whose ``summary()`` shows planned -> emitted per
    net (any net whose plan couldn't be honoured gets a warning line -- never
    silence). Not called from ``plan_layout`` / ``evaluate_circuit``.

    ``strict_missing_footprints`` (default ``False``) matches the lenient
    placement write: a placed part with no resolvable footprint (e.g. a
    ``Simulation_SPICE`` stimulus source that slipped through) is dropped with a
    warning instead of raising, so the copper stage is never *stricter* than the
    board it was handed. Set ``True`` for a physical-BOM board where a missing
    footprint must be a hard error. On a fully-footprinted board the two modes
    are byte-identical. Strip sim-only parts up front
    (:func:`skidl_layout.strip_sim_only_parts`) to keep them off the board
    entirely.

    ``fab_spec`` (default ``None`` -> byte-identical): a
    :class:`~skidl_layout.FabSpec`, preset name, or ``True`` (-> OSHPark 2-layer).
    When engaged it (a) stamps the board stackup via the writer, (b) routes
    signals + pours planes at the spec's track/clearance/via/board-edge values,
    and (c) clamps every planned power-track width UP to ``spec.min_track_mm``
    (an explicit ``overrides=`` width still wins). Resolved once via
    :func:`skidl_layout.resolve_fab_spec`.

    **Phase-4 knobs (all default OFF -> byte-identical).** ``pour_policy`` and
    ``include_suffixed`` are handed to a *recomputed*
    :func:`~skidl_layout.power.plan_power_routes` (this function holds both
    ``result`` and ``circuit`` already); with neither engaged the default-path
    ``result.power_plan`` is read exactly as before. That recompute is why the
    knobs cannot reach placement: ``plan_power_routes`` is also called from
    inside ``score_placement``, and those call sites never pass them.

    - ``pour_policy``: ``None`` (historical ladder), ``"auto"`` (a supply net
      with >= ``power.POUR_AUTO_MIN_REFS`` placed refs earns poured copper), or
      an explicit list of net names. An ``overrides`` width still demotes a
      promoted net back to a wide trace -- the human veto stays strongest.
    - ``include_suffixed``: recognise suffixed rail names (``VIN_12V``,
      ``HV_RAIL``) as supplies so they stop routing at signal width.
    - ``supply_pour_layer``: where promoted supply nets pour on a 2-layer board
      (default ``"F.Cu"``; ``"B.Cu"`` shares the back copper with GND).
    - ``route_promoted`` (default ``True``): a promoted net keeps its routed
      trunk *and* gets the pour, instead of being excluded from routing the way
      a ground plane is. Measured on the Figure-11 boost: excluded, VOUT's F.Cu
      region fragments behind the signal tracks and ``check_connected`` reports
      it broken; with the trunk kept, every net closes. No effect when nothing
      is promoted.
    - ``zone_clearance`` / ``min_thickness``: forwarded to ``pour_planes``
      (``None`` emits no flag, so KRT's own pour defaults stand).

    **Phase-5 knob (default OFF -> byte-identical).** ``thermal_vias`` stamps a
    via array into the controller's exposed pad *after* the pour and *before*
    the board is graded, so the graded board is the final one. The pad is found
    topologically (the ``PowerStagePlan``'s ``controller_ref`` +
    ``ground_net``, then that footprint's largest pad on ground) -- never by
    reference or footprint name.

    ⚠ **A thermal via array under an exposed pad IS via-in-pad**, and the
    shipped ``oshpark-2l`` spec declares ``via_in_pad=False``. On that spec the
    array **refuses**: nothing is emitted, a warning names the capability, and
    ``result.thermal_vias["refused"]`` is True. That is the default outcome for
    every board this stack currently ships, and :func:`skidl_layout.fab_check`
    now grades the rule so a board carrying an array cannot pass a spec that
    forbids one. ``thermal_via_pitch_mm`` overrides the default
    ``via_size + clearance`` pitch; ``thermal_via_edge_margin_mm`` demands that
    much pad copper outside each via's own copper (default 0.0 -- "fully inside
    the pad"; the annular ring is a via-internal rule the spec already
    enforces). Should the array cost DRC, it is retried smaller and, failing
    that, dropped entirely -- a board with a DRC violation is never shipped in
    exchange for vias.

    **Phase-6 knobs (all default OFF -> byte-identical).** ``net_currents`` is a
    plain ``{net_name: amps_rms}`` dict -- **data, not a simulation
    dependency**. This module never imports ``skidl.sim`` and never runs
    ngspice; a measured dict from :func:`skidl_eda.measure_net_currents` and a
    hand-written one are equally valid, and the caller owns the mapping from
    whatever circuit was simulated onto this board's net names.

    When given, each named net **that the power plan already carries a width
    for** is widened to ``max(planned, ipc2221_width_mm(current))``
    (:mod:`skidl_layout.current_widths`). Three properties are deliberate:

    - **A current can only widen.** A narrower IPC width than the plan's is
      ignored: the magic ladder's floor also encodes drop and impedance
      judgement, not only heat.
    - **A human ``overrides`` width still wins absolutely.** The merge runs
      *before* the veto, so a deliberate demote beats the simulator -- Phase
      4's rule, unchanged.
    - **Only planned nets are widened.** A net with a measured current but no
      plan entry -- the switch node, ground-as-a-plane, a signal net -- is
      *recorded* in ``result.current_widths`` with ``applied=False`` and left
      alone. Widening the SW node collides head-on with
      ``SW_NODE_COPPER_AREA``, a trade no gate can referee yet.

    ``current_delta_t_c`` (default 10 C) is the allowed temperature rise;
    ``current_max_width_mm`` caps the result and **warns with both numbers**
    when it bites, because a silent clamp is a lie with units. ``net_voltages``
    (``{net: v_peak}``) drives one report-only warning: a poured or
    current-widened net above :data:`CREEPAGE_WARN_VOLTS` says that creepage
    and clearance are not modeled here. It never changes behavior.
    """
    from .writer import write_kicad_pcb
    from .fabspec import resolve_fab_spec

    spec = resolve_fab_spec(fab_spec)

    os.makedirs(workdir, exist_ok=True)
    workdir_abs = os.path.abspath(workdir)
    placed_pcb = os.path.join(workdir_abs, "placed.kicad_pcb")
    write_kicad_pcb(
        result.placed_parts,
        circuit,
        fp_lib_dirs,
        placed_pcb,
        outline=result.outline,
        cutouts=getattr(result, "cutouts", None),
        lib_table=lib_table,
        strict_missing_footprints=strict_missing_footprints,
        fab_spec=spec,
    )

    warnings: list[str] = []

    # Phase-4 recognition/policy knobs recompute the plan here rather than
    # anywhere placement can see. With neither engaged this is the default path
    # verbatim.
    power_plan = result.power_plan
    if pour_policy is not None or include_suffixed:
        from .power import plan_power_routes

        power_plan = plan_power_routes(
            circuit,
            result.placed_parts,
            board_layers=board_layers,
            include_suffixed=include_suffixed,
            pour_policy=pour_policy,
        )
    intents = list(getattr(power_plan, "route_intents", []) or [])
    net_kinds = {
        n.name: n.kind for n in (getattr(power_plan, "nets", None) or [])
    }

    # Partition the plan. When a fab spec is engaged, clamp each planned power
    # width UP to the fab's minimum track (a fab never draws below its own floor;
    # an explicit override still wins, applied after this).
    plane_nets: list[str] = []
    plane_layers: list[str] = []
    promoted_nets: list[str] = []
    width_map: dict[str, float] = {}
    for intent in intents:
        if intent.strategy in _PLANE_STRATEGIES:
            # A *supply* net that pours only ever got there via the policy --
            # ground pours on the historical path too.
            promoted = net_kinds.get(intent.net_name) == "supply"
            plane_nets.append(intent.net_name)
            plane_layers.append(
                _plane_layer_for(
                    intent,
                    board_layers,
                    promoted=promoted,
                    supply_pour_layer=supply_pour_layer,
                )
            )
            if promoted:
                promoted_nets.append(intent.net_name)
                if route_promoted:
                    # Keep the trunk AND pour the region. A ground plane owns a
                    # whole layer, so excluding it from routing is right; a
                    # promoted supply pours on a *signal* layer whose tracks
                    # fence its region into islands, and a 2-layer board has no
                    # second copper for the pour to stitch back through. The
                    # routed backbone is what makes the pour additive.
                    width = intent.width_mm
                    if spec is not None:
                        width = max(width, spec.min_track_mm)
                    width_map[intent.net_name] = width
        elif intent.strategy in _WIDE_STRATEGIES:
            width = intent.width_mm
            if spec is not None:
                width = max(width, spec.min_track_mm)
            width_map[intent.net_name] = width

    # Measured currents (Phase 6): the physics widens what the ladder guessed.
    # Runs BEFORE the override veto so a human demote still beats the sim, and
    # touches only nets the plan already carries -- see the docstring.
    current_records: dict[str, dict] = {}
    if net_currents:
        current_records = _merge_current_widths(
            width_map, net_currents, spec,
            delta_t_c=current_delta_t_c,
            max_width_mm=current_max_width_mm,
            warnings=warnings,
        )
    _warn_high_voltage(
        net_voltages,
        poured=plane_nets,
        widened=[n for n, r in current_records.items() if r["applied"]],
        warnings=warnings,
    )

    # Human vetoes: an override for any net wins (and can force a width on a net
    # the plan left at signal width). Overriding a plane net to a width demotes
    # it to a wide trace.
    if overrides:
        for net, width in overrides.items():
            width_map[net] = width
            if net in plane_nets:
                idx = plane_nets.index(net)
                plane_nets.pop(idx)
                plane_layers.pop(idx)
                if net in promoted_nets:
                    promoted_nets.remove(net)
                warnings.append(f"{net}: override width demoted plane -> wide trace")
            # The veto is stronger than the sim; the record must say so rather
            # than keep claiming a width the board will not carry.
            record = current_records.get(net)
            if record is not None and record["applied"]:
                if abs(width - record["applied_width_mm"]) > 1e-9:
                    warnings.append(
                        f"{net}: override {width:.2f}mm overrode the "
                        f"current-sized {record['applied_width_mm']:.2f}mm "
                        f"(IPC {record['ipc_width_mm']:.2f}mm at "
                        f"{record['i_rms_a']:.3f}A) -- the human veto wins")
                record["applied_width_mm"] = width
                record["overridden"] = True

    # Route: all nets except the plane nets (poured next), wide power at width.
    # A congested 2-layer board needs the SKILL's rip-up budget to close every
    # signal net; harmless on an easy board. Caller can override.
    if route_extra_args is None:
        route_extra_args = ["--max-ripup", "10", "--max-iterations", "1000000"]
    # A promoted net that keeps its trunk stays IN the route selection; the
    # pour then adds area around a backbone that already reaches every pad.
    routed_promoted = (
        {n for n in promoted_nets if n in width_map} if route_promoted else set()
    )
    net_selection = ["*"] + [
        f"!{n}" for n in plane_nets if n not in routed_promoted
    ]
    routed_pcb = os.path.join(workdir_abs, "routed_power.kicad_pcb")
    dr = _spec_route_kwargs(spec)
    krt.route_and_check(
        placed_pcb,
        workdir_abs,
        krt_dir=krt_dir,
        nets=net_selection,
        timeout_s=timeout_s,
        power_net_widths=width_map or None,
        out_path=routed_pcb,
        route_extra_args=route_extra_args,
        **dr,
    )

    # Honesty: planned -> emitted widths from the routed board.
    with open(routed_pcb, "r", encoding="utf-8", errors="replace") as handle:
        routed_text = handle.read()
    emitted_widths = krt._segment_widths_by_net(routed_text)
    for net, planned in width_map.items():
        emitted = emitted_widths.get(net)
        if emitted is None:
            warnings.append(f"{net}: planned {planned:.2f}mm but no track emitted")
        elif emitted + 1e-6 < planned:
            warnings.append(
                f"{net}: emitted {emitted:.2f}mm < planned {planned:.2f}mm "
                "(router necked down or floored)"
            )

    # Pour the plane nets on the routed board, or fall through if none.
    plane_summary: dict = {}
    zones_by_net: dict[str, int] = {}
    final_pcb = routed_pcb
    if plane_nets:
        final_pcb = os.path.join(workdir_abs, "power_copper.kicad_pcb")
        pour_kwargs = dict(_spec_pour_kwargs(spec))
        # None emits no flag at all, so a knobless pour keeps KRT's defaults and
        # byte-identical argv.
        if zone_clearance is not None:
            pour_kwargs["zone_clearance"] = zone_clearance
        if min_thickness is not None:
            pour_kwargs["min_thickness"] = min_thickness
        plane_summary = krt.pour_planes(
            routed_pcb,
            final_pcb,
            nets=plane_nets,
            plane_layers=plane_layers,
            workdir=workdir_abs,
            krt_dir=krt_dir,
            timeout_s=timeout_s,
            add_gnd_vias=add_gnd_vias,
            gnd_via_distance=gnd_via_distance,
            **pour_kwargs,
        )
        # Read the zones back per net: a promotion that poured nothing must be
        # visible, not averaged into a board total.
        try:
            with open(final_pcb, "r", encoding="utf-8", errors="replace") as handle:
                zones_by_net = krt._zone_counts_by_net(handle.read())
        except OSError:
            zones_by_net = {}
        for net in plane_nets:
            if zones_by_net and not zones_by_net.get(net):
                kind = "promoted" if net in promoted_nets else "plane"
                warnings.append(f"{net}: {kind} to a pour but no zone was written")
        if plane_summary.get("zone_count", 0) < len(plane_nets):
            warnings.append(
                f"poured {plane_summary.get('zone_count', 0)} zone(s) for "
                f"{len(plane_nets)} plane net(s): "
                + ", ".join(plane_nets)
            )
        if not plane_summary.get("connected_ok", True):
            stranded = (
                plane_summary.get("unrouted_nets", [])
                + plane_summary.get("broken_nets", [])
            )
            warnings.append(
                "pour left disconnected copper on: " + ", ".join(stranded)
                + " (KRT route_disconnected_planes.py can repair; not auto-run)"
            )

    # Grade the final board (zone-aware connectivity) and attach it.
    feedback = krt.check_board(final_pcb, krt_dir=krt_dir, timeout_s=timeout_s)

    # -- thermal vias under the exposed pad (Phase 5, WS-3) ------------------
    # Runs AFTER the pour and BEFORE the graded board is settled, so what gets
    # graded is what ships. The refusal path costs nothing: it never touches the
    # board, so `feedback` above stands unchanged.
    thermal_dict = None
    if thermal_vias:
        final_pcb, feedback, thermal_dict, thermal_warnings = _apply_thermal_vias(
            final_pcb, workdir_abs, result, spec, feedback,
            pitch_mm=thermal_via_pitch_mm,
            edge_margin_mm=thermal_via_edge_margin_mm,
            krt_dir=krt_dir, timeout_s=timeout_s,
        )
        warnings.extend(thermal_warnings)

    result.routability = feedback

    outcome = PowerCopperResult(
        routed_pcb_path=final_pcb,
        plane_nets=plane_nets,
        plane_layers=plane_layers,
        width_map=width_map,
        emitted_widths=emitted_widths,
        plane_summary=plane_summary,
        feedback=feedback,
        warnings=warnings,
        promoted_nets=promoted_nets,
        zones_by_net=zones_by_net,
        thermal_vias=thermal_dict,
        current_widths=current_records,
    )
    logger.info("Power copper: %s", outcome.summary().replace("\n", " | "))
    return outcome


def _merge_current_widths(
    width_map: dict,
    net_currents: dict,
    spec,
    delta_t_c: float,
    max_width_mm: float | None,
    warnings: list,
) -> dict:
    """Widen ``width_map`` in place from measured currents; return the record.

    The record is per **measured** net, not per widened net: a current that did
    not reach the board is the interesting half of the story (the SW node), and
    dropping it would make the result look as though nothing was measured.
    """
    from .current_widths import widths_from_currents

    # Uncapped first, so the cap has an honest number to be reported against.
    honest = widths_from_currents(net_currents, spec=spec, delta_t_c=delta_t_c)

    records: dict[str, dict] = {}
    for net, ipc_width in sorted(honest.items()):
        applied_width = ipc_width
        if max_width_mm is not None and ipc_width > float(max_width_mm) + 1e-9:
            applied_width = float(max_width_mm)
        planned = width_map.get(net)
        row = {
            "i_rms_a": float(net_currents[net]),
            "ipc_width_mm": round(ipc_width, 4),
            "planned_width_mm": round(planned, 4) if planned is not None else None,
            "applied_width_mm": None,
            "applied": False,
            "capped": applied_width < ipc_width - 1e-9,
            "delta_t_c": float(delta_t_c),
        }
        if planned is None:
            # Measured, deliberately not widened: no plan entry to widen.
            row["reason"] = (
                "not in the power plan's width map (a plane, the switch node, "
                "or a signal net) -- recorded, not widened")
            records[net] = row
            continue
        merged = max(planned, applied_width)
        width_map[net] = merged
        row["applied_width_mm"] = round(merged, 4)
        row["applied"] = True
        if row["capped"]:
            warnings.append(
                f"{net}: current {row['i_rms_a']:.3f}A needs {ipc_width:.2f}mm "
                f"(IPC-2221, dT {delta_t_c:g}C) but current_max_width_mm "
                f"caps it at {float(max_width_mm):.2f}mm -- the track is "
                "narrower than the physics asks for")
        elif merged <= planned + 1e-9:
            row["reason"] = (
                f"IPC width {ipc_width:.3f}mm <= planned {planned:.3f}mm; "
                "the plan's floor stands (a current may only widen)")
        records[net] = row
    return records


def _warn_high_voltage(
    net_voltages: dict | None,
    poured: list,
    widened: list,
    warnings: list,
) -> None:
    """Report-only creepage notice for high-voltage copper (Phase 6).

    Never changes behavior. Creepage/clearance geometry is out of scope for
    this phase -- the point of the warning is that a board needing it stops
    passing *silently*.
    """
    if not net_voltages:
        return
    interesting = set(poured) | set(widened)
    for net in sorted(interesting):
        v_peak = net_voltages.get(net)
        if v_peak is None:
            continue
        try:
            volts = abs(float(v_peak))
        except (TypeError, ValueError):
            continue
        if volts > CREEPAGE_WARN_VOLTS:
            warnings.append(
                f"{net}: peak {volts:.1f}V exceeds {CREEPAGE_WARN_VOLTS:g}V -- "
                "creepage/clearance spacing is NOT modeled by this stack "
                "(report-only; check the fab's spacing table by hand)")


def _apply_thermal_vias(
    final_pcb: str,
    workdir_abs: str,
    result,
    spec,
    feedback,
    pitch_mm: float | None,
    edge_margin_mm: float,
    krt_dir: str | None,
    timeout_s: int,
):
    """Stamp the exposed-pad via array, keeping only a DRC-clean board.

    Returns ``(board_path, feedback, thermal_dict, warnings)``. ``feedback`` is
    re-graded only when vias were actually written; on the refusal path the
    caller's existing grading stands and no KRT run is spent.

    Bail-out 2 of the Phase-5 plan is implemented here: if the full array costs
    a DRC violation the array is retried smaller, and if nothing is clean the
    **pre-splice board is kept**. A mechanism that correctly declines is a
    result; a board with a DRC violation is not.
    """
    from .copper_post import plan_thermal_vias, splice_vias

    warnings: list[str] = []
    stage_plan = getattr(result, "power_stage_plan", None)
    stages = list(getattr(stage_plan, "stages", None) or [])
    if not stages:
        warnings.append(
            "thermal_vias requested but no power stage was classified; "
            "nothing to place an array under")
        return final_pcb, feedback, None, warnings

    stage = stages[0]
    controller = getattr(stage, "controller_ref", None)
    ground = getattr(stage, "ground_net", None)
    if not controller or not ground:
        warnings.append(
            "thermal_vias requested but the power stage names no controller / "
            "ground net; nothing to place an array under")
        return final_pcb, feedback, None, warnings

    plan = plan_thermal_vias(
        final_pcb, controller, ground, spec,
        pitch_mm=pitch_mm, edge_margin_mm=edge_margin_mm)
    if not plan.positions:
        # Refusal (the shipped oshpark-2l path) or "no pad" -- both are
        # outcomes, not failures. Say so; never comply silently.
        warnings.append(f"thermal vias not emitted: {plan.reason}")
        return final_pcb, feedback, plan.to_dict(), warnings

    baseline_drc = int(getattr(feedback, "drc_violation_count", 0) or 0)
    cols, rows = plan.shape
    attempts = _shrink_ladder(cols, rows)
    for shape in attempts:
        candidate_plan = plan if shape == (cols, rows) else plan_thermal_vias(
            final_pcb, controller, ground, spec, pitch_mm=pitch_mm,
            edge_margin_mm=edge_margin_mm, max_shape=shape)
        if not candidate_plan.positions:
            continue
        candidate = os.path.join(workdir_abs, "power_copper_thermal.kicad_pcb")
        splice_vias(final_pcb, candidate, candidate_plan)
        _copy_sibling_project(final_pcb, candidate)
        graded = krt.check_board(candidate, krt_dir=krt_dir, timeout_s=timeout_s)
        graded_drc = int(getattr(graded, "drc_violation_count", 0) or 0)
        worse_conn = (graded.unrouted_count > feedback.unrouted_count)
        if graded_drc <= baseline_drc and not worse_conn:
            if shape != (cols, rows):
                warnings.append(
                    f"thermal via array shrunk {cols}x{rows} -> "
                    f"{shape[0]}x{shape[1]} to stay DRC-clean")
            return candidate, graded, candidate_plan.to_dict(), warnings
        warnings.append(
            f"thermal via array {shape[0]}x{shape[1]} rejected: DRC "
            f"{baseline_drc} -> {graded_drc}"
            + (f", unrouted {feedback.unrouted_count} -> {graded.unrouted_count}"
               if worse_conn else ""))

    dropped = dict(plan.to_dict())
    dropped.update(count=0, positions=[], shape=[0, 0],
                   reason="every array size cost DRC or connectivity; dropped")
    warnings.append(
        "thermal vias dropped entirely: no array size was DRC-clean "
        "(the pre-splice board ships unchanged)")
    return final_pcb, feedback, dropped, warnings


def _shrink_ladder(cols: int, rows: int) -> list:
    """Array shapes to try, largest first, down to a single via."""
    shapes = []
    c, r = cols, rows
    while c >= 1 and r >= 1:
        shapes.append((c, r))
        if c == 1 and r == 1:
            break
        # Shrink the longer axis first so the array stays as square as it can.
        if c >= r:
            c -= 1
        else:
            r -= 1
    return shapes


def _copy_sibling_project(src_pcb: str, dst_pcb: str) -> None:
    """Carry a board's sibling ``.kicad_pro`` across a post-process copy.

    KRT's own note: a bare board loses the DRC floor its copper was routed to,
    and the next tool resolves stock (looser) netclasses instead -- which grades
    correct sub-floor copper as phantom clearance violations.
    """
    import shutil

    sibling = os.path.splitext(src_pcb)[0] + ".kicad_pro"
    if os.path.isfile(sibling):
        try:
            shutil.copyfile(sibling, os.path.splitext(dst_pcb)[0] + ".kicad_pro")
        except OSError:  # pragma: no cover - best effort
            pass
