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

    def summary(self) -> str:
        lines = ["Power copper emitted:"]
        # planned -> emitted per wide-trace net (the honesty artifact)
        for net in sorted(self.width_map):
            planned = self.width_map[net]
            emitted = self.emitted_widths.get(net)
            emitted_str = f"{emitted:.2f}mm" if emitted is not None else "no trace"
            lines.append(f"  wide {net}: planned {planned:.2f}mm -> {emitted_str}")
        for net, layer in zip(self.plane_nets, self.plane_layers):
            zones = self.plane_summary.get("zone_count", 0)
            lines.append(f"  plane {net}: pour on {layer} ({zones} zone(s) total)")
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


def _plane_layer_for(intent, board_layers: int) -> str:
    """Copper layer to pour a plane/pour-strategy net on.

    4+ layers: honour the plan's ``suggested_layer`` (e.g. In1.Cu). 2-layer:
    force B.Cu so F.Cu stays free for signal routing (``_suggest_layer`` biases
    signals to F.Cu; a GND pour belongs on the back), matching SKILL Step 8.
    """
    if board_layers >= 4:
        return intent.layer
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
    intents = list(getattr(result.power_plan, "route_intents", []) or [])

    # Partition the plan. When a fab spec is engaged, clamp each planned power
    # width UP to the fab's minimum track (a fab never draws below its own floor;
    # an explicit override still wins, applied after this).
    plane_nets: list[str] = []
    plane_layers: list[str] = []
    width_map: dict[str, float] = {}
    for intent in intents:
        if intent.strategy in _PLANE_STRATEGIES:
            plane_nets.append(intent.net_name)
            plane_layers.append(_plane_layer_for(intent, board_layers))
        elif intent.strategy in _WIDE_STRATEGIES:
            width = intent.width_mm
            if spec is not None:
                width = max(width, spec.min_track_mm)
            width_map[intent.net_name] = width

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
                warnings.append(f"{net}: override width demoted plane -> wide trace")

    # Route: all nets except the plane nets (poured next), wide power at width.
    # A congested 2-layer board needs the SKILL's rip-up budget to close every
    # signal net; harmless on an easy board. Caller can override.
    if route_extra_args is None:
        route_extra_args = ["--max-ripup", "10", "--max-iterations", "1000000"]
    net_selection = ["*"] + [f"!{n}" for n in plane_nets]
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
    final_pcb = routed_pcb
    if plane_nets:
        final_pcb = os.path.join(workdir_abs, "power_copper.kicad_pcb")
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
            **_spec_pour_kwargs(spec),
        )
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
    )
    logger.info("Power copper: %s", outcome.summary().replace("\n", " | "))
    return outcome
