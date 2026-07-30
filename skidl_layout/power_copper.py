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
    #: The via-in-pad relocation outcome (Phase 8), or ``None`` when
    #: ``relocate_via_in_pad=False``. Carries every flagged via with its
    #: verdict -- relocated, unresolved, or a foreign-net via left alone.
    via_relocation: dict | None = None
    #: Per-net spacing record (Phase 10), one row per net a voltage was given
    #: for -- ``{net: {volts, required_mm, applied_mm, applied, reason}}``.
    #: A net Table 6-1 does not ask to widen is recorded with ``applied=False``
    #: and its reason: graded, deliberately not moved. Empty unless
    #: ``voltage_spacing=True``.
    net_clearances: dict = field(default_factory=dict)
    #: Nets the Phase-14 pinning pass ADDED to ``width_map`` because the plan
    #: named them as power-carrying but gave them no wide-trace intent --
    #: ``{net: {width_mm, source, kind, from_plan_width}}``. The switch node is
    #: the case this exists for. Empty unless ``pin_power_widths=True``, and an
    #: empty dict with the flag on means every such net was already pinned.
    pinned_widths: dict = field(default_factory=dict)
    #: What the Phase-14 two-pass route did: the pass-1 net set and where it
    #: came from, both boards, both logs, and pass 2's argv. Empty unless
    #: ``loop_first`` was requested; ``{"requested": True, "ran": False, ...}``
    #: when it was requested and no commutation loop was classified, so "asked
    #: for and declined" can never read as "never asked for".
    loop_first: dict = field(default_factory=dict)
    #: What spacing plan B's per-pad clearance override did: the resolved
    #: targets and their values, which s-expression form was written, how many
    #: tokens landed, and where the refs came from. Empty unless
    #: ``pad_clearance=`` was given; ``{"requested": True, "written": 0,
    #: "reason": …}`` when it was given and no controller resolved, so "asked
    #: for and declined" can never read as "never asked for".
    pad_clearance: dict = field(default_factory=dict)
    #: What the Phase-14 fanout pre-pass did, per resolved controller: vias
    #: placed, vias dropped, and the nets it could **not** escape. Empty unless
    #: ``fanout_controller=True``.
    fanout: dict = field(default_factory=dict)
    #: What the Phase-14 escape keepout wrote: how many annulus polygons, on
    #: which layer, around which controllers, and whether it was applied after
    #: a loop pass (the only ordering in which it is not Phase 13's arm C).
    #: Empty unless ``keepout_escape=True``.
    keepout: dict = field(default_factory=dict)
    #: What the Phase-15 polygon-zone path did: the resolved
    #: :class:`~skidl_layout.power_zones.ZonePlan` as a dict, how many zones were
    #: spliced, which net form was used, and the board they went into. Empty
    #: unless ``zone_plan=`` was given; ``{"requested": True, "spliced": 0,
    #: "reason": …}`` when it was given and nothing resolved, so "asked for and
    #: declined" can never read as "never asked for".
    zone_plan: dict = field(default_factory=dict)
    #: ⭐ Phase 16: the copper stack handed to ``route.py --layers``, top to
    #: bottom, and the ``--layer-costs`` paired with it (``-1`` = FORBIDDEN).
    #: **Both ``None`` below four layers**, which is the byte-identical default
    #: path -- an absent stack means "no layer flag was emitted", not "two
    #: layers were assumed". They travel on the result so a driver can assert
    #: what was requested without re-deriving it, which is half of the gate that
    #: exists because Phase 12 reported a four-layer result it never measured.
    route_layers: list[str] | None = None
    route_layer_costs: list[float] | None = None

    def summary(self) -> str:
        lines = ["Power copper emitted:"]
        # planned -> emitted per wide-trace net (the honesty artifact)
        for net in sorted(self.width_map):
            planned = self.width_map[net]
            emitted = self.emitted_widths.get(net)
            emitted_str = f"{emitted:.2f}mm" if emitted is not None else "no trace"
            # A pinned net is flagged so a reader can tell a width the PLAN
            # asked for from one this pass added to stop a global --track-width
            # narrowing it. Same line, one extra word -- and absent entirely on
            # the default path, so existing output is unchanged.
            pin = self.pinned_widths.get(net)
            tag = f" [pinned {pin['source']}]" if pin else ""
            lines.append(
                f"  wide {net}: planned {planned:.2f}mm -> {emitted_str}{tag}")
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
        for net in sorted(self.net_clearances):
            row = self.net_clearances[net]
            required = row.get("required_mm")
            lines.append(
                f"  spacing {net}: {row['volts']:.0f}V -> Table 6-1 "
                + (f"{required:.2f}mm" if required is not None else "not stated")
                + (f", routed at {row['applied_mm']:.2f}mm" if row.get("applied")
                   else " (not widened)")
            )
        relocation = self.via_relocation or {}
        if relocation:
            lines.append(
                f"  via-in-pad: {relocation.get('relocated', 0)} of "
                f"{relocation.get('in_pad_before', 0)} relocated, "
                f"{relocation.get('in_pad_after', 0)} left in pads"
                + (f" ({relocation.get('outcome')})" if relocation.get("outcome")
                   else "")
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
            "via_relocation": (dict(self.via_relocation)
                               if self.via_relocation else None),
            "net_clearances": {k: dict(v) for k, v in self.net_clearances.items()},
            "pinned_widths": {k: dict(v) for k, v in self.pinned_widths.items()},
            "pad_clearance": dict(self.pad_clearance),
            "loop_first": dict(self.loop_first),
            "fanout": dict(self.fanout),
            "keepout": dict(self.keepout),
            "zone_plan": dict(self.zone_plan),
            "route_layers": (list(self.route_layers)
                             if self.route_layers is not None else None),
            "route_layer_costs": (list(self.route_layer_costs)
                                  if self.route_layer_costs is not None else None),
        }


def plan_pinned_power_widths(
    power_plan,
    stage_plan,
    *,
    spec=None,
    width_map: dict[str, float] | None = None,
    plane_nets=(),
    routed_plane_nets=(),
) -> dict[str, dict]:
    """Which power-carrying nets the width map is *missing*, and at what width.

    ⭐ **Power-layout Phase 14, WS-14.0 -- open defect 3.** ``width_map`` gains a
    net only when its route intent is a wide strategy, or when a promoted plane
    net keeps its trunk. A switch node whose intent is ``fanout_only`` is
    therefore **absent**, so it is never passed to ``route.py --power-nets``, so
    a global ``--track-width`` applies to it. Measured consequence: ``SW`` on
    ``lt3757_sepic`` went **0.300 -> 0.1524 mm with DRC still 0**, because DRC
    does not check current. That single leak is what made Phase 12's arm D
    unadoptable.

    Returns ``{net: record}`` for the nets that should be pinned and are not,
    where each record carries ``width_mm``, ``source`` and ``kind`` -- so a
    caller can *say* what it pinned and why rather than silently widening argv.
    Pure logic: no board, no router, no I/O. That is deliberate, so the
    partition is unit-testable without spending a route.

    **Where each width comes from**, in order:

    1. the plan's own ``suggested_width_mm`` for that net, when the plan named it;
    2. otherwise the fab spec's ``track_width_mm`` -- which is *exactly the width
       the net gets today*. ⛔ The point of this pass is to make that width
       immune to a global ``--track-width``, **not** to change it. A net with no
       plan width and no spec is skipped rather than guessed at.

    Both are then floored to ``spec.min_track_mm``: a fab never draws below its
    own published limit.

    **What is deliberately skipped:**

    - a net already in ``width_map`` -- the plan, the simulated current and the
      human override all rank above this pass, which only ever *adds*;
    - a plane net that is poured rather than routed. Pinning it would pass
      ``--power-nets`` for a net that gets no tracks at all, and the emitted-width
      honesty check would then warn "planned ... but no track emitted" on every
      poured net. ``routed_plane_nets`` names the promoted ones that do keep a
      trunk, and those *are* pinned.
    """
    width_map = width_map or {}
    plane_nets = set(plane_nets or ())
    routed_plane_nets = set(routed_plane_nets or ())

    by_name = {n.name: n for n in (getattr(power_plan, "nets", None) or [])}

    # (net, kind, source) in a deterministic order: the plan's own net order
    # first, then the classifier's stages in the order it found them.
    wanted: list[tuple[str, str, str]] = []

    def _want(net, kind, source):
        if not net:
            return
        net = str(net)
        if any(net == existing for existing, _, _ in wanted):
            return
        wanted.append((net, kind, source))

    for net in (getattr(power_plan, "nets", None) or []):
        if net.kind in ("supply", "ground"):
            _want(net.name, net.kind, f"power_plan:{net.kind}")

    # ⭐ The switch node is the whole reason this exists: it is a *node*, not a
    # rail, so no name-based rule classifies it and the plan gives it no width
    # intent -- yet it carries the full switch current.
    for stage in (getattr(stage_plan, "stages", None) or []):
        for net in (getattr(stage, "switch_node_nets", None) or []):
            _want(net, "switch_node", "stage:switch_node")
        _want(getattr(stage, "input_rail", None), "supply", "stage:input_rail")
        _want(getattr(stage, "output_rail", None), "supply", "stage:output_rail")
        for net in (getattr(stage, "ground_nets", None) or []):
            _want(net, "ground", "stage:ground")
        # ⭐ And the commutation loop, which is power-carrying **by the
        # classifier's own definition** -- it is the path whose current stops
        # when the switch opens. This catches the current-sense node (``ISNS``,
        # ``CS1``) that sits in the loop between the switch and ground and
        # carries the full switch current while matching no name-based rule.
        # ⛔ Deliberately the same net set WS-14.1 routes first, read from the
        # same field, so the width lever and the ordering lever cannot drift
        # apart into two different opinions about what "the loop" is.
        for loop in (getattr(stage, "loops", None) or []):
            for net in (getattr(loop, "net_names", None) or []):
                _want(net, "loop", "stage:commutation_loop")

    pinned: dict[str, dict] = {}
    for net, kind, source in wanted:
        if net in width_map:
            continue                       # the plan/sim/override already owns it
        if net in plane_nets and net not in routed_plane_nets:
            continue                       # poured, not routed -- nothing to pin
        planned = getattr(by_name.get(net), "suggested_width_mm", None)
        width = planned
        if width is None:
            width = getattr(spec, "track_width_mm", None)
        if width is None:
            # No plan width and no fab spec: KRT's own default applies and we
            # cannot name it. Skipped rather than guessed -- an invented width
            # is exactly the failure mode this pass exists to remove.
            continue
        floor = getattr(spec, "min_track_mm", None)
        if floor is not None:
            width = max(width, floor)
        pinned[net] = {
            "width_mm": float(width),
            "source": source,
            "kind": kind,
            "from_plan_width": planned is not None,
        }
    return pinned


def plan_loop_first_nets(
    stage_plan,
    *,
    explicit=None,
    plane_nets=(),
    routed_plane_nets=(),
) -> tuple[list[str], str]:
    """``(pass_1_nets, source)`` -- the commutation-loop copper to commit first.

    ⭐ **Power-layout Phase 14, WS-14.1.** The router's own diagnosis of the four
    failing boards is that the pads are *"boxed in by static obstacles
    (neighboring pads + clearance), not by congestion"*, and Phase 12 measured
    rip-up gaining **+0 nets across 15 routes** -- rip-up moves tracks and the
    obstruction is pads. What *can* move is the **order copper is committed in**,
    and ``route.py``'s ``--ordering`` offers only four heuristics, none of which
    takes a caller-supplied list. Two passes are the only exact control, and they
    need nothing new from KRT.

    ⛔ **No new heuristic here.** The arc already names the loop:
    :class:`~skidl_layout.power_roles.CommutationLoop` carries ``net_names``, the
    nets spanning the parts whose current stops when the switch opens. This
    takes those, plus each stage's switch nodes and rails, and subtracts the nets
    that are **poured rather than routed** -- pass 1 cannot commit copper for a
    net the router is not routing.

    ``explicit`` (a list) overrides the derivation entirely and is returned
    verbatim minus the poured nets, so a caller can test a partition without
    editing the classifier. The returned ``source`` says which happened, because
    a derived set and a dictated one deserve different amounts of trust.

    Returns an **empty list** when there is no classified loop -- the caller must
    then fall back to a single pass rather than route "nothing" first.
    """
    plane_nets = set(plane_nets or ())
    routed_plane_nets = set(routed_plane_nets or ())
    poured = plane_nets - routed_plane_nets

    ordered: list[str] = []

    def _add(net):
        if net and str(net) not in ordered and str(net) not in poured:
            ordered.append(str(net))

    if explicit is not None and explicit is not True:
        for net in explicit:
            _add(net)
        return ordered, "explicit"

    for stage in (getattr(stage_plan, "stages", None) or []):
        # Loop first, in the loop's own order -- it is the high-di/dt path and
        # the arc's primary objective, so it gets the short channels.
        for loop in (getattr(stage, "loops", None) or []):
            for net in (getattr(loop, "net_names", None) or []):
                _add(net)
        for net in (getattr(stage, "switch_node_nets", None) or []):
            _add(net)
        _add(getattr(stage, "input_rail", None))
        _add(getattr(stage, "output_rail", None))
    return ordered, "power_stage_plan"


def _run_fanout_prepass(
    in_pcb, workdir_abs, result, circuit, fp_lib_dirs, *, spec, krt_dir,
    timeout_s, escape_method, plane_nets, width_map, warnings,
) -> dict:
    """Fan out every resolved controller, chaining board to board.

    A board may declare more than one IC (Phase 13's ``mark_escape_room``), so
    each is fanned in turn and each writes a fresh board -- the next one reads
    the previous one's output, so two ICs on the same board cannot overwrite
    each other's escape copper.

    ⛔ **Net scope.** Only the housekeeping nets are fanned: the poured nets and
    the trunked power nets are excluded. Stubs on a poured net are wasted copper
    and can fence the pour into islands, which is the measured Phase-4 reason
    ``route_promoted`` defaults ``True``.
    """
    from .power_escape import fanout_controller as _fanout
    from .power_escape import resolve_escape_targets

    placed = {str(p.ref): p for p in (getattr(result, "placed_parts", None) or [])}
    targets, source = resolve_escape_targets(
        placed_refs=placed,
        circuit=circuit,
        power_stage_plan=getattr(result, "power_stage_plan", None),
    )
    if not targets:
        warnings.append(
            "fanout_controller requested but no controller resolved "
            "(no declaration, no classified stage); no fanout run")
        return {"requested": True, "ran": False, "source": source,
                "reason": "no controller resolved"}

    # ``!NAME`` exclusions, the same shape route.py takes.
    excluded = sorted(set(plane_nets) | set(width_map))
    nets = ["*"] + [f"!{n}" for n in excluded]

    board = in_pcb
    rows: list[dict] = []
    for index, (ref, _lane) in enumerate(targets):
        out_pcb = os.path.join(workdir_abs, f"fanout_{index}_{ref}.kicad_pcb")
        row = _fanout(
            board, out_pcb, ref,
            krt_dir=krt_dir, nets=nets, escape_method=escape_method,
            track_width=(spec.min_track_mm if spec is not None else None),
            # ⛔⛔ ``clearance_mm``, NOT ``min_clearance_mm`` -- and this cost a
            # DRC violation before it was found. ``qfn_fanout``'s ``--clearance``
            # is the margin its escape copper keeps from foreign pads/tracks, so
            # it must be the clearance the board is actually ROUTED and GRADED
            # at, not the fab's published floor. Handed the floor (0.1524 mm on
            # oshpark-2l) the pre-pass legally placed an escape via that the
            # board's own 0.25 mm rule then flagged: measured on
            # ``lt3757_sepic`` as ``Via:UVLO <-> Seg:INTVCC, overlap 0.036 mm``.
            # ⚠ The fab FLOOR is still the right bound for ``track_width`` -- a
            # thin escape stub is legal; copper too CLOSE to its neighbour is not.
            clearance=(spec.clearance_mm if spec is not None else None),
            via_size=(spec.via_size_mm if spec is not None else None),
            via_drill=(spec.via_drill_mm if spec is not None else None),
            board_edge_clearance=(spec.board_edge_keepout_mm
                                  if spec is not None else None),
            timeout_s=timeout_s,
        )
        rows.append(row)
        if row.get("ran"):
            board = out_pcb
            warnings.append(
                f"fanout {ref}: {row.get('vias_placed')} via(s) placed, "
                f"{row.get('vias_dropped')} dropped, "
                f"{len(row.get('failed_nets') or [])} net(s) not escaped "
                f"({escape_method})")
        else:
            # ⛔ Declining is an outcome, not a failure -- but a silent decline
            # reads as a pre-pass that worked.
            warnings.append(f"fanout {ref}: NOT run -- {row.get('reason')}")
    ran = [r for r in rows if r.get("ran")]
    return {
        "requested": True, "ran": bool(ran), "source": source,
        "escape_method": escape_method,
        "controllers": [ref for ref, _ in targets],
        "nets": list(nets),
        "rows": rows,
        # ``None`` when nothing ran, so the caller keeps its original input.
        "board": board if ran else None,
        "vias_placed": sum(int(r.get("vias_placed") or 0) for r in ran),
        "vias_dropped": sum(int(r.get("vias_dropped") or 0) for r in ran),
        "failed_nets": sorted({n for r in ran
                               for n in (r.get("failed_nets") or [])}),
    }


def _apply_pad_clearance(
    board_pcb, result, circuit, *, clearance_mm, form, spec, warnings,
) -> dict:
    """Write the per-pad clearance override into ``board_pcb``, in place.

    ⭐ **Spacing plan B.** This is the only mechanism in the stack that can hold
    *routed* copper off a controller's pins: track-to-pad clearance is not
    separately configurable from track-to-track, no router reads courtyards, and
    the user-layer track keep-out is refuted three times (−32, −22,
    ±0-with-8-DRC) because it blocks every net including the controller's own.

    ⛔ **A different mechanism from that keep-out, and the difference is
    net-exemption.** A clearance override binds between items of *different*
    nets, so the controller's own escape keeps using the room while foreign
    copper is pushed out of it.

    ⚠ Applied to the board the writer just emitted -- **before** the fanout
    pre-pass and every route pass -- so it is a property of the board rather
    than of one pass, and ``writer.py`` stays untouched (the two-layer
    byte-identity all three Phase-0 digests rest on cannot move).
    """
    from .power_pads import apply_pad_clearance, resolve_pad_clearance_targets

    placed = [str(p.ref) for p in (getattr(result, "placed_parts", None) or [])]
    targets, source = resolve_pad_clearance_targets(
        placed_refs=placed, circuit=circuit, clearance_mm=clearance_mm,
        power_stage_plan=getattr(result, "power_stage_plan", None),
        warnings=warnings)
    if not targets:
        warnings.append(
            "pad_clearance requested but no controller resolved "
            "(no declaration, no classified stage); no override written")
        return {"requested": True, "written": 0, "source": source,
                "reason": "no controller resolved"}
    record = apply_pad_clearance(board_pcb, targets, form=form, fab_spec=spec)
    record.update({"requested": True, "source": source,
                   "clearance_mm": float(clearance_mm) if clearance_mm else None,
                   "targets": {ref: value for ref, value in targets}})
    warnings.append(
        "pad_clearance: "
        + ", ".join(f"{ref} at {value:g}mm" for ref, value in targets)
        + f" ({record['written']} {form}-level token(s), {source})")
    return record


def _apply_escape_keepout(
    board_pcb, result, fp_lib_dirs, *, spec, layer, loop_ran, warnings,
) -> dict:
    """Draw each controller's escape annulus into ``board_pcb``, in place.

    Reuses Phase 13's measured geometry (``EscapeRoom.annulus``) and its writer
    (``write_keepout_polygons``, round-tripped through KRT's own parser) rather
    than re-deriving either.
    """
    from .power_escape import measure_escape_rooms, write_keepout_polygons

    if not loop_ran:
        # ⛔ Said out loud rather than refused. Reproducing Phase 13's arm C is
        # a legitimate thing to ask for -- the arc re-derives its negatives
        # instead of quoting them -- but nobody should reach it by accident.
        warnings.append(
            "keepout_escape without loop_first reproduces Phase 13's arm C, "
            "which measured -32 routed nets across the corpus: KRT's keepout is "
            "not net-scoped, so the annulus blocks the controller's own escape")

    rooms = measure_escape_rooms(result, fp_lib_dirs, fab_spec=spec)
    polygons = [poly for room in rooms for poly in (room.annulus or [])]
    if not polygons:
        warnings.append(
            "keepout_escape requested but no escape annulus could be measured; "
            "no polygon written and no --keepout flag emitted")
        return {"requested": True, "written": 0,
                "reason": "no annulus measured"}
    written = write_keepout_polygons(board_pcb, polygons, layer=layer)
    warnings.append(
        f"keepout_escape: {written} annulus polygon(s) drawn on {layer} around "
        + ", ".join(r.controller_ref for r in rooms))
    return {
        "requested": True, "written": written, "layer": layer,
        "board": board_pcb, "applied_after_loop_pass": bool(loop_ran),
        "controllers": [r.controller_ref for r in rooms],
        "lane_mm": [round(r.lane_mm, 4) for r in rooms],
    }


def _resolve_zone_plan(zone_plan, result, circuit, fp_lib_dirs, *, spec,
                       plane_nets, warnings) -> tuple:
    """``(ZonePlan | None, record)`` from whatever the caller handed in.

    Accepts a ready-made :class:`~skidl_layout.power_zones.ZonePlan`, a kwargs
    dict forwarded to :func:`~skidl_layout.power_zones.plan_zone_regions`
    (``{"sections": …, "escape_carve": True}``), or ``True`` meaning "derive one
    with the defaults". ⛔ Never raises: a plan that cannot be built is a
    recorded outcome the caller routes past, exactly like the fanout's decline.
    """
    from .power_zones import ZonePlan, plan_zone_regions

    record: dict = {"requested": True, "spliced": 0}
    try:
        if isinstance(zone_plan, ZonePlan):
            plan = zone_plan
            record["source"] = "caller"
        else:
            kwargs = dict(zone_plan) if isinstance(zone_plan, dict) else {}
            record["source"] = "derived" if not kwargs.get("sections") else "sections"
            plan = plan_zone_regions(
                result, circuit, fp_lib_dirs, fab_spec=spec,
                plane_nets=list(plane_nets), **kwargs)
    except Exception as exc:                            # noqa: BLE001
        warnings.append(f"zone_plan could not be built: {type(exc).__name__}: {exc}")
        return None, {"requested": True, "spliced": 0,
                      "reason": f"{type(exc).__name__}: {exc}"}

    warnings.extend(f"zone_plan: {w}" for w in plan.warnings)
    record["plan"] = plan.to_dict()
    if not plan.regions and not plan.carves:
        # ⛔ Requested and empty is NOT the same as never requested; a gate that
        # cannot tell the two apart reads a vacuous pass.
        warnings.append(
            "zone_plan requested but no region or carve resolved; the pour "
            "falls through to route_planes unchanged")
        record["reason"] = "no region or carve resolved"
    return plan, record


def _splice_zone_plan(plan, final_pcb, workdir_abs, plane_summary, *,
                      zone_clearance, min_thickness, krt_dir, timeout_s,
                      record, warnings) -> tuple:
    """Splice ``plan``'s zones into ``final_pcb``; re-grade. ``(path, summary)``.

    ⚠ The board is **re-graded after the splice**, with the same two KRT
    checkers ``pour_planes`` runs, so the summary a caller reads always describes
    the board that ships rather than the board before the zones went in.
    """
    from .power_zones import (
        board_uses_name_nets, net_ids_from_board, splice_zones, zone_sexprs,
    )

    if not plan.regions and not plan.carves:
        return final_pcb, plane_summary
    try:
        with open(final_pcb, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        kicad10 = board_uses_name_nets(text)
        sexprs = zone_sexprs(
            plan, net_ids_from_board(text),
            clearance=zone_clearance, min_thickness=min_thickness,
            kicad10=kicad10)
        # ⚠ zone_sexprs appends to plan.warnings when a region's net is not on
        # the board, so the warning list is re-read AFTER the call.
        warnings.extend(f"zone_plan: {w}" for w in plan.warnings
                        if f"zone_plan: {w}" not in warnings)
        if not sexprs:
            record["reason"] = "no zone s-expression emitted"
            return final_pcb, plane_summary
        zoned = os.path.join(workdir_abs, "power_zones.kicad_pcb")
        count = splice_zones(final_pcb, zoned, sexprs)
    except Exception as exc:                            # noqa: BLE001
        warnings.append(
            f"zone_plan splice failed, board unchanged: "
            f"{type(exc).__name__}: {exc}")
        record["reason"] = f"{type(exc).__name__}: {exc}"
        return final_pcb, plane_summary

    record.update(spliced=count, board=zoned, kicad10_net_form=kicad10,
                  regions=len(plan.regions), carves=len(plan.carves))
    warnings.append(
        f"zone_plan: spliced {count} zone(s) -- {len(plan.regions)} region "
        f"pour(s) + {len(plan.carves)} pour-exclusion rule area(s) -- in the "
        + ("KiCad 10" if kicad10 else "KiCad 9")
        + " net form")
    summary = krt.grade_pour(
        zoned, krt_dir=krt_dir, timeout_s=timeout_s,
        min_clearance_used=(plane_summary or {}).get("min_clearance_used"))
    return zoned, summary


def _pass_log_path(route_log_path: str | None, tag: str) -> str | None:
    """``<stem>.<tag><ext>`` beside ``route_log_path``, or ``None``.

    The caller's own path stays the FINAL pass's log, so every existing parser
    (the rescue-line counter, the keepout-line check) keeps reading the file it
    always read. The extra pass gets a sibling, which is what lets a gate assert
    that two passes genuinely happened rather than infer it from argv.
    """
    if not route_log_path:
        return None
    stem, ext = os.path.splitext(route_log_path)
    return f"{stem}.{tag}{ext or '.txt'}"


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
    voltage_spacing: bool = False,
    spacing_column: str = "B2",
    relocate_via_in_pad: bool = False,
    route_log_path: str | None = None,
    pin_power_widths: bool = False,
    loop_first: bool | list | None = False,
    fanout_controller: bool = False,
    fanout_escape_method: str = "underpad",
    keepout_escape: bool = False,
    keepout_layer: str = "User.2",
    zone_plan=None,
    reserve_plane_layers: bool = False,
    pad_clearance: float | None = None,
    pad_clearance_form: str = "pad",
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

    **Phase-8 knob (default OFF -> byte-identical).** ``relocate_via_in_pad``
    moves KRT's plane-stitching vias **out of** the SMD pads they land in and
    ties each pad back to its via with a short stub track on the pad's own layer
    (:mod:`skidl_layout.via_relocate`). Every poured board this stack has
    produced is via-in-pad -- 11 on the Phase-4 boost, 12 on the Phase-6 board,
    37 on avalanche -- and ``oshpark-2l`` declares ``via_in_pad=False``.

    ⚠ These are **stitching** vias, not additive thermal ones: a via at a ground
    pad IS that pad's only path to the plane. So nothing is ever dropped. A via
    with no legal ring position **stays where it is** and is counted as
    unresolved, and the whole change is reverted rather than shipped if DRC or
    connectivity gets worse -- an honest "9 of 11 relocated" is a good result
    and an orphaned ground pad is not. Runs after the pour and after any thermal
    array, so the graded board is the shipped board.

    ``current_delta_t_c`` (default 10 C) is the allowed temperature rise;
    ``current_max_width_mm`` caps the result and **warns with both numbers**
    when it bites, because a silent clamp is a lie with units. ``net_voltages``
    (``{net: v_peak}``) drives one report-only warning: a poured or
    current-widened net above :data:`CREEPAGE_WARN_VOLTS` says that creepage
    and clearance are not modeled here. It never changes behavior.

    **Phase-10 knob (default OFF -> byte-identical).** ``voltage_spacing=True``
    turns the same ``net_voltages`` into the *lever* Phase 8's judge was missing:
    each net's peak voltage is sized through IPC-2221B Table 6-1
    (:mod:`skidl_layout.power_clearance`, ``spacing_column`` selects the column,
    default **B2** = external uncoated, which is every board this stack ships)
    and any net the table asks *more* of than the FabSpec's design clearance is
    routed at that wider clearance via KRT's ``--net-clearances``.

    Three deliberate properties, mirroring the current-width merge above:

    - **Spacing can only widen.** A net Table 6-1 is happy with at the board's
      own clearance is *recorded* in ``result.net_clearances`` with
      ``applied=False`` and left alone -- a per-net entry below the board
      clearance would quietly *relax* a net that was already fine.
    - **The lever and the judge read the same table**, so ``fab_check``'s
      spacing rows and this cannot drift apart.
    - **It is not a hard floor and does not claim to be.** KRT's fine-pitch
      rescue ladder can still neck a rescued net below the requested value. What
      a board actually achieved stays a question for
      :func:`fabspec.measure_voltage_spacing`, measured off the routed copper.

    ⛔⛔ **Phase-11 retraction: the map binds board-WIDE, not pairwise.** This
    docstring used to say the widening bound "pairwise only between nets the map
    names". It does not -- KRT derives a routing-side *floor* from the map and
    applies it to every obstacle, so naming one 150 V net widens the whole
    board. Measured on ``uc3844_flyback``: 5 nets named, **17 nets widened**.
    The consequence worth planning for is that on a congested board the lever
    **redistributes** clearance rather than adding it. Full mechanism and
    numbers: :mod:`skidl_layout.power_clearance`'s module docstring.

    ⚠ Spacing is **not** added to ``fab_must_pass``: the judge stays report-only
    for this phase, per the arc's rule that a rule promoted in the same phase
    that first moved it is a rule measured wrong.

    **Phase-11 knob (default OFF -> byte-identical).** ``route_log_path`` keeps
    ``route.py``'s own stdout instead of discarding it. Nothing about the route
    changes -- the argv is untouched -- but KRT's fine-pitch **rescue ladder**
    announces itself only there (``rescued a gap: grid ..., clearance ...``),
    and a rescue is precisely what necks a net below the per-net clearance
    ``voltage_spacing`` asked for. Without the log that mechanism can only be
    quoted from a past run; with it, any driver re-derives it.

    **Phase-14 knobs (all default OFF -> byte-identical).** Four levers aimed at
    one diagnosis: the failing pads are *"boxed in by static obstacles
    (neighboring pads + clearance), not by congestion"* -- KRT's own words -- and
    Phase 12 measured rip-up gaining **+0 nets across 15 routes**, because
    rip-up moves tracks and cannot move a pad.

    - ``pin_power_widths`` closes a measured leak. ``width_map`` gains a net only
      from a *wide-trace intent*, so a switch node (intent ``fanout_only``) was
      never passed to ``--power-nets`` and a global ``--track-width`` narrowed
      ``SW`` **0.300 -> 0.1524 mm on lt3757_sepic with DRC still 0** -- DRC does
      not check current. The pinned width is the plan's own, or the fab's
      ``track_width_mm``, i.e. *exactly what the net already gets*: the pass
      makes a width immune to narrowing, it does not change it. See
      :func:`plan_pinned_power_widths`.
    - ``loop_first`` routes the commutation loop in its own pass, then routes
      the rest from that board with ``--keep-input-copper``. ⭐ Two passes are
      the ONLY exact ordering control -- ``--ordering`` offers four heuristics
      and none takes a caller-supplied list. ⚠ Pass 2 has *less* room, not more
      (the loop copper is now a static obstacle), so grade completion per board
      in both directions. See :func:`plan_loop_first_nets`.
    - ``fanout_controller`` authors the controller's escape copper before any
      route pass, via KRT's ``qfn_fanout.py``. Housekeeping nets only.
    - ``keepout_escape`` draws the escape annulus and passes ``--keepout`` to the
      **final** pass. ⛔⛔ Meaningful only *with* ``loop_first``: KRT's keepout is
      not net-scoped, so applied to a single pass it blocks the controller's own
      escape -- Phase 13 measured that at **-32 routed nets across the corpus**
      (68/81 -> 36/81, DRC 0 throughout). Used alone it reproduces that arm and
      says so in ``warnings``.

    **Spacing plan B knob (default ``None`` -> byte-identical).**
    ``pad_clearance`` (mm) writes a per-pad ``(clearance …)`` override onto every
    resolved controller's pads, in the form ``pad_clearance_form`` selects
    (``"pad"``, the default, or ``"footprint"``; KRT's parser resolves both into
    ``pad.local_clearance``). ⭐ It is the **only** mechanism here that holds
    *routed* copper off a controller's pins -- track-to-pad clearance is not
    separately configurable from track-to-track, and no router reads courtyards.
    ⛔ **Not the thrice-refuted user-layer keep-out:** a clearance override binds
    only between items of *different* nets, so the controller's own escape still
    uses the room. ⚠ It is a **floor**, so it also stops KRT's fine-pitch rescue
    ladder necking near those pads -- which is the quality it buys and the
    completion it can cost. See :mod:`skidl_layout.power_pads`.

    **Phase-15 knob (default OFF -> byte-identical).** ``zone_plan`` pours
    **polygons we choose** instead of accepting ``route_planes.py``'s Voronoi
    partition. Accepts a :class:`~skidl_layout.power_zones.ZonePlan`, a kwargs
    dict for :func:`~skidl_layout.power_zones.plan_zone_regions`
    (``{"sections": {"power": ["U1", "L1"], "analog": [...]},
    "escape_carve": True}``), or ``True`` for a derived plan.

    - The nets a region covers are poured through KRT's ``kicad_writer``, called
      **directly** -- it is pure Python and takes an arbitrary polygon.
      **Every plane net no region covers still goes through ``pour_planes``
      exactly as today**, so a mixed board works and this is adoptable
      incrementally rather than as a flag day.
    - ``escape_carve`` adds a pour-exclusion **rule area** over each controller's
      escape annulus. ⛔⛔ **This is not ``keepout_escape``.** That flag blocks
      *tracks* (refuted three times: -32, -22, and ±0-with-8-DRC). A rule area
      blocks *copper pour only* -- tracks, vias and pads stay allowed, which is
      precisely the asymmetry an escape via needs.
    - The net header form (KiCad 9 ``(net id)`` + ``(net_name …)`` vs KiCad 10
      ``(net "NAME")``) is **detected from the board being spliced**, the same
      way ``route_planes.py`` decides it. Nothing here assumes a version.

    **Phase-16: ``board_layers`` finally reaches the copper.** The placed board
    written here now declares ``board_layers`` copper layers (the writer raises
    if that contradicts ``fab_spec``), and the resolved stack is passed to every
    route pass as ``--layers`` and to the pour as ``--layers``. Below four
    layers nothing changes -- no layer keyword is passed at all, so the KRT
    calls are literally the pre-Phase-16 calls.

    ``reserve_plane_layers`` (default ``False``) additionally forbids the router
    from routing on any layer that carries a plane, via ``--layer-costs`` with
    KRT's ``-1`` FORBIDDEN sentinel. ⚠ **Both settings are real questions.**
    Reserving the planes is what makes a 4-layer power board electrically
    right; leaving them routable is the maximum-completion case. ⛔ It is
    meaningless below four layers and is recorded in ``warnings`` if asked for
    there.
    """
    from .writer import write_kicad_pcb, _copper_layer_names
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
        # ⭐ Phase 16: the board KRT actually routes is written HERE, so this is
        # where the four-layer request has to land. ⛔ The writer raises if this
        # contradicts the spec -- a board that asks for four layers and declares
        # two is exactly what voided Phase 12's four-layer arm.
        copper_layers=board_layers,
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

    # ⭐ Phase 14 / WS-14.0: pin every power-carrying net the plan names, not
    # only the ones that earned a wide-trace intent. Opt-in and default OFF, so
    # with the flag off ``width_map`` -- and therefore the emitted
    # ``--power-nets`` argv -- is byte-identical.
    #
    # Placed BEFORE the current merge on purpose: ``_merge_current_widths``
    # touches only nets the map already carries, so pinning first is what lets a
    # caller who supplies ``net_currents`` have the physics reach the switch
    # node too (Phase 6 measured ``SW`` at 4.1386 A). Placed BEFORE the override
    # block for the same reason it always was -- the human veto wins.
    pinned_widths: dict[str, dict] = {}
    if pin_power_widths:
        # A promoted plane net that keeps its trunk is routed, so it may be
        # pinned; a poured one may not (see the helper's docstring).
        routed_planes = (
            {n for n in promoted_nets if n in width_map} if route_promoted else set()
        )
        pinned_widths = plan_pinned_power_widths(
            power_plan,
            getattr(result, "power_stage_plan", None),
            spec=spec,
            width_map=width_map,
            plane_nets=plane_nets,
            routed_plane_nets=routed_planes,
        )
        for net, row in sorted(pinned_widths.items()):
            width_map[net] = row["width_mm"]
            warnings.append(
                f"{net}: pinned at {row['width_mm']:g}mm ({row['source']}) -- "
                "a global --track-width can no longer narrow it")
        if not pinned_widths:
            warnings.append(
                "pin_power_widths requested but every power-carrying net the "
                "plan names is already in the width map; no net was added")

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

    # The spacing lever (Phase 10). Opt-in and default OFF, so every existing
    # caller's argv is byte-identical: with ``voltage_spacing=False`` no map is
    # built and no ``--net-clearances`` flag is emitted. Sized purely from the
    # net's voltage through IPC-2221B Table 6-1 -- the same table Phase 8's
    # ``measure_voltage_spacing`` judges against, so lever and judge cannot drift.
    clearance_records: dict = {}
    clearance_map: dict = {}
    if voltage_spacing and net_voltages:
        from .power_clearance import net_clearance_map, plan_net_clearances

        clearance_records = plan_net_clearances(
            net_voltages,
            base_clearance_mm=(spec.clearance_mm if spec is not None else None),
            column=spacing_column,
        )
        clearance_map = net_clearance_map(clearance_records)
        for net, row in sorted(clearance_records.items()):
            if row["applied"]:
                warnings.append(
                    f"{net}: routing at {row['applied_mm']:g}mm clearance -- "
                    f"{row['reason']}")
        if not clearance_map:
            warnings.append(
                "voltage_spacing requested but no net needs widening: "
                "every voltage given is inside the band the board already routes "
                "at, so no --net-clearances flag was passed")

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

    # ⭐ Power-layout Phase 16: resolve the routing stack ONCE, here, and hand
    # the same answer to every route pass and to the pour.
    #
    # ⛔ Below four layers this is ``None``/``None`` and NO layer flag is
    # emitted, so the 2-layer argv is byte-identical to every run before
    # Phase 16.
    #
    # ⭐ The reservation rule reads as one line of English: **a layer that
    # carries a plane is not a routing layer**, ``F.Cu`` is preferred, and
    # everything else costs more. It generalises to six layers without another
    # edit. ``-1`` is KRT's FORBIDDEN sentinel; ``1.0``/``3.0`` reproduce KRT's
    # own 2-layer bias. ⚠ Without it ``route.py`` gives every layer cost 1.0 at
    # four or more layers -- a power board with signal tracks cut through its
    # ground plane is not datasheet quality, it is worse than a 2-layer board.
    route_layers: list[str] | None = None
    route_layer_costs: list[float] | None = None
    if board_layers >= 4:
        route_layers = _copper_layer_names(board_layers)
        if reserve_plane_layers:
            poured = set(plane_layers)
            route_layer_costs = [
                -1.0 if name in poured else (1.0 if name == "F.Cu" else 3.0)
                for name in route_layers
            ]
    elif reserve_plane_layers:
        # Asked for and declined, recorded rather than silently ignored: on two
        # layers there is no plane layer to withhold from the router.
        warnings.append(
            f"reserve_plane_layers requested at board_layers={board_layers}; "
            "the concept needs 4+ copper layers, so no layer flags were passed")
    # ⛔ Built as dicts rather than passed as ``layers=None`` so that below four
    # layers the KRT calls are LITERALLY the pre-Phase-16 calls -- same keyword
    # set, not merely the same argv. The kwargs a caller sees are part of the
    # byte-identity claim, and two existing tests assert them exactly.
    route_layer_kwargs: dict = {}
    pour_layer_kwargs: dict = {}
    if route_layers is not None:
        route_layer_kwargs["layers"] = route_layers
        # ⛔ The stack goes to the pour; the COSTS never do. ``route_planes.py``
        # routes plane-net taps from pads down to the plane layer, and
        # forbidding that layer there disconnects every ground pad.
        pour_layer_kwargs["layers"] = route_layers
        if route_layer_costs is not None:
            route_layer_kwargs["layer_costs"] = route_layer_costs

    # ⭐ Phase 14 / WS-14.1: commit the commutation loop's copper BEFORE anything
    # else competes for the channels. Opt-in and default OFF -- with
    # ``loop_first=False`` the single call below is the pre-Phase-14 call
    # verbatim, same argv, same input board.
    loop_first_record: dict = {}
    fanout_record: dict = {}
    keepout_record: dict = {}
    pad_clearance_record: dict = {}
    route_input = placed_pcb
    pass2_extra = route_extra_args

    # ⭐ Spacing plan B: the per-pad clearance override goes onto the board
    # FIRST, before the fanout pre-pass and every route pass, because it is a
    # property of the board and not of a pass. Opt-in and default ``None`` ->
    # not a byte is touched.
    if pad_clearance is not None and pad_clearance is not False:
        pad_clearance_record = _apply_pad_clearance(
            placed_pcb, result, circuit, clearance_mm=pad_clearance,
            form=pad_clearance_form, spec=spec, warnings=warnings)

    # ⭐ Phase 14 / WS-14.3: the fanout pre-pass runs BEFORE pass 1, so the
    # router routes from a pad that has already left the package. Opt-in and
    # default OFF.
    if fanout_controller:
        fanout_record = _run_fanout_prepass(
            route_input, workdir_abs, result, circuit, fp_lib_dirs,
            spec=spec, krt_dir=krt_dir, timeout_s=timeout_s,
            escape_method=fanout_escape_method,
            plane_nets=plane_nets, width_map=width_map, warnings=warnings)
        if fanout_record.get("board"):
            route_input = fanout_record["board"]
    if loop_first is not False and loop_first is not None:
        loop_nets, loop_source = plan_loop_first_nets(
            getattr(result, "power_stage_plan", None),
            explicit=(loop_first if loop_first is not True else None),
            plane_nets=plane_nets,
            routed_plane_nets=routed_promoted,
        )
        if not loop_nets:
            # ⛔ No classified loop -> fall back to ONE pass and say so. Routing
            # "nothing" first and then everything is not a null experiment: it
            # is a second route from a different input board.
            warnings.append(
                "loop_first requested but no commutation loop was classified; "
                "routed in a single pass (the default path)")
            loop_first_record = {"requested": True, "ran": False,
                                 "reason": "no commutation loop classified"}
        else:
            loop_pcb = os.path.join(workdir_abs, "routed_loop.kicad_pcb")
            pass1_log = _pass_log_path(route_log_path, "pass1")
            pass1_input = route_input        # the fanned board when WS-14.3 ran
            krt.route_and_check(
                pass1_input,
                workdir_abs,
                krt_dir=krt_dir,
                nets=list(loop_nets),
                timeout_s=timeout_s,
                power_net_widths=width_map or None,
                out_path=loop_pcb,
                route_extra_args=route_extra_args,
                net_clearances=clearance_map or None,
                route_log_path=pass1_log,
                **route_layer_kwargs,
                **dr,
            )
            # ⚠ The sibling ``.kicad_pro`` carries the DRC floor pass 1 routed
            # to. Pass 2's input is pass 1's OUTPUT, so stranding it here would
            # make pass 2 resolve stock netclasses and manufacture phantom
            # clearance violations -- the gotcha that has cost this arc a run
            # more than once.
            _copy_sibling_project(pass1_input, loop_pcb)
            route_input = loop_pcb
            # ⛔ APPENDED, never substituted. ``write_krt_fab_overrides``
            # *returns* the ``["--fab-overrides", <file>]`` fragment, and
            # dropping it lets KRT's rescue ladder neck below the fab's
            # published minimum -- a different and worse experiment.
            pass2_extra = list(route_extra_args or []) + ["--keep-input-copper"]
            # Pass 1's copper is foreign to every pass-2 net, so it is already
            # an obstacle; ``--keep-input-copper`` is what stops the post-route
            # CLEANUP passes rewriting it.
            net_selection = (["*"]
                             + [f"!{n}" for n in loop_nets]
                             + [f"!{n}" for n in plane_nets
                                if n not in routed_promoted])
            loop_first_record = {
                "requested": True, "ran": True, "source": loop_source,
                "pass1_nets": list(loop_nets),
                "pass1_board": loop_pcb, "pass1_log": pass1_log,
                "pass2_nets": list(net_selection),
                "pass2_extra_args": list(pass2_extra),
            }
            warnings.append(
                "loop_first: pass 1 committed "
                + ", ".join(loop_nets)
                + f" ({loop_source}); pass 2 routes the rest with "
                  "--keep-input-copper")

    # ⭐ Phase 14 / WS-14.5: the escape annulus, applied to the board the FINAL
    # pass reads. Opt-in and default OFF.
    #
    # ⛔ Why this is only meaningful after ordering: KRT's keepout is **not
    # net-scoped** (``obstacle_map.add_user_keepout_obstacles`` blocks all copper
    # layers for every net being routed), so an annulus drawn before a
    # single-pass route blocks the controller's own escape. Phase 13 measured
    # exactly that -- **-32 nets across the corpus** (68/81 -> 36/81, DRC 0
    # throughout), with the control board losing precisely its three controller
    # housekeeping nets. With the escapes already committed by pass 1, the same
    # polygons protect the remaining via sites instead of destroying them.
    if keepout_escape:
        keepout_record = _apply_escape_keepout(
            route_input, result, fp_lib_dirs, spec=spec, layer=keepout_layer,
            loop_ran=bool(loop_first_record.get("ran")), warnings=warnings)
        if keepout_record.get("written"):
            pass2_extra = list(pass2_extra or []) + [
                "--keepout", "--keepout-layer", keepout_layer]
            if loop_first_record:
                loop_first_record["pass2_extra_args"] = list(pass2_extra)

    krt.route_and_check(
        route_input,
        workdir_abs,
        krt_dir=krt_dir,
        nets=net_selection,
        timeout_s=timeout_s,
        power_net_widths=width_map or None,
        out_path=routed_pcb,
        route_extra_args=pass2_extra,
        net_clearances=clearance_map or None,
        route_log_path=route_log_path,
        **route_layer_kwargs,
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

    # ⭐ Phase 15 / WS-15.1--15.3: resolve the polygon zone plan BEFORE the pour,
    # because it decides which plane nets ``route_planes.py`` still handles.
    # ⛔ ``zone_plan=None`` -> ``covered`` is empty -> the pour call below is the
    # pre-Phase-15 call verbatim, same argv, same nets, same board.
    zone_record: dict = {}
    resolved_zone_plan = None
    covered_nets: set = set()
    if zone_plan is not None and zone_plan is not False:
        # ⛔⛔ Phase 16 limitation, recorded rather than fixed. Every region a
        # zone plan builds pours on ``power_zones.BACK_COPPER`` ("B.Cu") unless
        # a caller names a layer -- and on four layers B.Cu is a *signal* layer,
        # not the ground plane. ``plan_zone_regions`` accepts ``board_layers``
        # and never reads it. The zone plan is opt-in, off in every Phase-16
        # arm, and building a 4-layer story for it is a later phase's work; what
        # must not happen is a 4-layer board quietly pouring a region onto its
        # escape-via landing area.
        if board_layers >= 4:
            warnings.append(
                f"zone_plan requested at board_layers={board_layers}: regions "
                "default to B.Cu, which is a SIGNAL layer on a 4+ layer board. "
                "plan_zone_regions has no 4-layer story (Phase 16 §9.5) -- name "
                "an explicit layer= or expect the region on the back copper")
        resolved_zone_plan, zone_record = _resolve_zone_plan(
            zone_plan, result, circuit, fp_lib_dirs, spec=spec,
            plane_nets=plane_nets, warnings=warnings)
        if resolved_zone_plan is not None:
            covered_nets = {n for n in resolved_zone_plan.covered_nets
                            if n in plane_nets}
            uncovered_regions = [r.net for r in resolved_zone_plan.regions
                                 if r.net not in plane_nets]
            if uncovered_regions:
                # A region may legitimately pour a net the plan never promoted --
                # said out loud, because it means this board pours copper the
                # baseline arm has none of, and an area comparison must know.
                warnings.append(
                    "zone_plan: region(s) pour net(s) the power plan did not "
                    "promote: " + ", ".join(sorted(set(uncovered_regions))))
            # ⛔⛔ THE MIXED-BOARD DEFECT, measured in Phase 15 and unfixed.
            #
            # A plane net no region covers still goes to ``route_planes.py``
            # below -- and KRT sizes its Voronoi cell against the board as it is
            # *before* our zones are spliced in, because the splice happens
            # after the pour. The two pour paths therefore have **no
            # arbitration**: they both lay claim to the same free copper.
            #
            # Measured on ``lt3724_buck``, the corpus's only two-ground board,
            # where the regions pour SGND and PGND is left to KRT: one ground is
            # ANNIHILATED -- PGND fell to 4.4 mm2 under 1 mm regions, and SGND to
            # 2.8 mm2 under 4 mm regions -- and **which of the two loses is not
            # stable between runs**, which moved that board's routing completion
            # between two otherwise identical 35-route sweeps.
            #
            # The fix is to pour EVERY plane net through the region path, or to
            # hand KRT the region outlines as obstacles. Neither exists yet, so
            # the honest thing is to say so loudly at the seam rather than only
            # in a report nobody reads at 2 a.m.
            leftover = [n for n in plane_nets if n not in covered_nets]
            if covered_nets and leftover:
                warnings.append(
                    "zone_plan covers " + ", ".join(sorted(covered_nets))
                    + " but leaves " + ", ".join(leftover)
                    + " to route_planes -- ⛔ the two pour paths do NOT arbitrate "
                      "(KRT sizes its Voronoi cells before these zones are "
                      "spliced in). Measured on a two-ground board, one ground "
                      "was starved to under 5 mm2 and which one was not stable "
                      "run to run. Cover every plane net, or expect it")

    # Pour the plane nets on the routed board, or fall through if none.
    # Nets a zone region covers are poured by the writer path below instead;
    # everything else still goes through route_planes exactly as before, so a
    # MIXED board works and the change is adoptable incrementally.
    plane_summary: dict = {}
    zones_by_net: dict[str, int] = {}
    final_pcb = routed_pcb
    pour_pairs = [(net, layer) for net, layer in zip(plane_nets, plane_layers)
                  if net not in covered_nets]
    pour_nets = [net for net, _layer in pour_pairs]
    pour_layers = [layer for _net, layer in pour_pairs]
    if pour_nets:
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
            nets=pour_nets,
            plane_layers=pour_layers,
            workdir=workdir_abs,
            krt_dir=krt_dir,
            timeout_s=timeout_s,
            add_gnd_vias=add_gnd_vias,
            gnd_via_distance=gnd_via_distance,
            **pour_layer_kwargs,
            **pour_kwargs,
        )

    # ⭐ Phase 15 / WS-15.4: our own zones, spliced onto whatever KRT poured.
    if resolved_zone_plan is not None:
        final_pcb, plane_summary = _splice_zone_plan(
            resolved_zone_plan, final_pcb, workdir_abs, plane_summary,
            zone_clearance=zone_clearance, min_thickness=min_thickness,
            krt_dir=krt_dir, timeout_s=timeout_s,
            record=zone_record, warnings=warnings)

    if plane_nets or zone_record.get("spliced"):
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

    # -- via-in-pad relocation (Phase 8, WS-B) -------------------------------
    # Last, so it sees the final copper -- the pour's stitching vias and the
    # thermal array both -- and so the board it grades is the board that ships.
    relocation_dict = None
    if relocate_via_in_pad:
        final_pcb, feedback, relocation_dict, relocation_warnings = (
            _relocate_vias_in_pads(
                final_pcb, workdir_abs, spec, feedback,
                krt_dir=krt_dir, timeout_s=timeout_s,
            )
        )
        warnings.extend(relocation_warnings)

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
        via_relocation=relocation_dict,
        net_clearances=clearance_records,
        pinned_widths=pinned_widths,
        pad_clearance=pad_clearance_record,
        loop_first=loop_first_record,
        fanout=fanout_record,
        keepout=keepout_record,
        zone_plan=zone_record,
        route_layers=route_layers,
        route_layer_costs=route_layer_costs,
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


def _relocate_vias_in_pads(
    final_pcb: str,
    workdir_abs: str,
    spec,
    feedback,
    krt_dir: str | None,
    timeout_s: int,
):
    """Move plane-stitching vias out of SMD pads. Returns
    ``(board_path, feedback, relocation_dict, warnings)``.

    **The ladder, in the order Phase-8 plan section 6 bail-out 2 fixes:**

    1. **All at once.** Splice every geometrically legal move and grade. This is
       the common case and costs one KRT grading.
    2. **One at a time.** If the batch makes DRC or connectivity worse, rebuild
       from the original board accepting moves **one by one in file order**,
       grading after each and reverting any that regresses. Partial success is
       an explicitly good outcome here -- "9 of 11 relocated, 2 left in place"
       beats an all-or-nothing revert.
    3. **Keep the original.** If not a single via can be moved cleanly, the
       pre-change board ships unchanged and the count is reported as unresolved.

    ⛔ **A via is never dropped**, which is where this deliberately parts company
    with Phase 5's thermal-via rule. A thermal via is additive; these are the
    pads' only path to the plane, so dropping one can orphan a ground pad --
    a failure ``check_drc`` cannot see and ``check_connected`` can. Never trade
    connectivity for a lower via-in-pad number.
    """
    from .via_relocate import apply_via_relocations, plan_via_relocations

    warnings: list[str] = []
    plan = plan_via_relocations(final_pcb, spec)
    report = plan.to_dict()
    report["in_pad_before"] = plan.in_pad_count
    report["relocated"] = 0
    report["in_pad_after"] = plan.in_pad_count
    report["applied_indices"] = []
    report["drc_before"] = int(getattr(feedback, "drc_violation_count", 0) or 0)
    report["unrouted_before"] = int(getattr(feedback, "unrouted_count", 0) or 0)

    for note in plan.notes:
        warnings.append(f"via-in-pad relocation: {note}")
    for move in plan.foreign:
        warnings.append(f"via-in-pad: {move.reason}")

    if not plan.relocatable:
        report["outcome"] = (
            "nothing relocatable" if plan.in_pad_count
            else "no via-in-pad found")
        if plan.in_pad_count:
            warnings.append(
                f"via-in-pad: {len(plan.unresolved)} via(s) had no legal ring "
                "position and stay in their pads (the board is unchanged)")
        return final_pcb, feedback, report, warnings

    baseline_drc = report["drc_before"]
    baseline_unrouted = report["unrouted_before"]
    candidate_path = os.path.join(workdir_abs, "power_copper_via_relocated.kicad_pcb")

    def _try(indices):
        """Splice ``indices`` onto the ORIGINAL board and grade. Never mutates."""
        moved = apply_via_relocations(final_pcb, candidate_path, plan, only=indices)
        _copy_sibling_project(final_pcb, candidate_path)
        graded = krt.check_board(candidate_path, krt_dir=krt_dir,
                                 timeout_s=timeout_s)
        drc = int(getattr(graded, "drc_violation_count", 0) or 0)
        ok = drc <= baseline_drc and graded.unrouted_count <= baseline_unrouted
        return ok, moved, graded, drc

    order = [m.via_index for m in plan.relocatable]

    ok, moved, graded, drc = _try(set(order))
    accepted = set(order) if ok else set()
    if not ok:
        warnings.append(
            f"via-in-pad: relocating all {len(order)} at once cost DRC "
            f"{baseline_drc} -> {drc} / unrouted {baseline_unrouted} -> "
            f"{graded.unrouted_count}; retrying one via at a time")
        for index in order:
            trial = accepted | {index}
            ok_one, _moved, graded_one, drc_one = _try(trial)
            if ok_one:
                accepted = trial
            else:
                move = next(m for m in plan.relocatable if m.via_index == index)
                move.status = "unresolved"
                move.reason = (
                    f"relocation to ({move.new_x:.3f}, {move.new_y:.3f}) cost "
                    f"DRC {baseline_drc} -> {drc_one} / unrouted "
                    f"{baseline_unrouted} -> {graded_one.unrouted_count}; "
                    "left in place")
        if accepted:
            ok, moved, graded, drc = _try(accepted)
            if not ok:  # pragma: no cover - the accumulator was graded green
                accepted = set()

    if not accepted:
        report.update(outcome="reverted: no via could be moved without cost",
                      relocated=0, in_pad_after=plan.in_pad_count,
                      moves=[m.to_dict() for m in plan.moves])
        warnings.append(
            "via-in-pad: every relocation cost DRC or connectivity; the "
            "pre-change board ships unchanged")
        return final_pcb, feedback, report, warnings

    from .via_relocate import find_vias_in_pads

    report.update(
        outcome="relocated",
        relocated=len(accepted),
        applied_indices=sorted(accepted),
        in_pad_after=len(find_vias_in_pads(candidate_path, spec)),
        drc_after=drc,
        unrouted_after=graded.unrouted_count,
        moves=[m.to_dict() for m in plan.moves],
        # Recount AFTER the ladder: the per-via fallback demotes a move to
        # "unresolved" when it costs DRC or connectivity, so the counts taken
        # from the pre-ladder plan are stale by this point.
        relocatable=len(plan.relocatable),
        unresolved=len(plan.unresolved),
    )
    still = report["in_pad_after"]
    if still:
        warnings.append(
            f"via-in-pad: {len(accepted)} of {plan.in_pad_count} relocated; "
            f"{still} via(s) remain in pads (no legal position, or the move "
            "cost DRC/connectivity) -- reported, never dropped")
    return candidate_path, graded, report, warnings


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
