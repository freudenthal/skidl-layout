from __future__ import annotations

from dataclasses import dataclass, field, replace

from .constraints import (
    AlignConstraint,
    AnchorZone,
    DistributeConstraint,
    LayoutConstraints,
    NearConstraint,
)
from .intent import PlacementIntentPlan, RepeatedChannelIntent
from .placer import place_parts
from .power import PowerTopology
from .writer import PlacedPart


@dataclass
class PlacementCandidate:
    name: str
    placed_parts: list[PlacedPart]
    reasons: list[str] = field(default_factory=list)
    ref_reasons: dict[str, list[str]] = field(default_factory=dict)
    constraints: LayoutConstraints | None = None
    score: float | None = None
    pin_gravity_anchored_refs: set[str] = field(default_factory=set)


def copy_constraints(constraints: LayoutConstraints | None) -> LayoutConstraints:
    constraints = constraints or LayoutConstraints()
    return LayoutConstraints(
        fixed=list(constraints.fixed or []),
        zones=list(constraints.zones or []),
        edge_anchors=list(constraints.edge_anchors or []),
        keepouts=list(constraints.keepouts or []),
        cutouts=list(constraints.cutouts or []),
        align=list(constraints.align or []),
        distribute=list(constraints.distribute or []),
        near=list(constraints.near or []),
        far=list(constraints.far or []),
        face_edges=list(constraints.face_edges or []),
        outline=constraints.outline,
        form_factor=constraints.form_factor,
    )


def _explicit_position_refs(constraints: LayoutConstraints) -> set[str]:
    """Refs whose placement was specified by the caller, not inferred intent."""
    refs = {fixed.ref for fixed in constraints.fixed or []}
    refs.update(anchor.ref for anchor in constraints.edge_anchors or [])
    refs.update(face.ref for face in constraints.face_edges or [])
    for zone in constraints.zones or []:
        refs.update(zone.refs or [])
    for constraint in constraints.align or []:
        refs.update(constraint.refs or [])
    for constraint in constraints.distribute or []:
        refs.update(constraint.refs or [])
    return refs


def _explicit_floorplan_refs(constraints: LayoutConstraints) -> set[str]:
    refs = _explicit_position_refs(constraints)
    for constraint in constraints.near or []:
        refs.add(constraint.ref)
        refs.add(constraint.target_ref)
    for constraint in constraints.far or []:
        refs.add(constraint.ref)
        refs.add(constraint.target_ref)
    return refs


def _merge_inferred_edge_anchors(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
) -> LayoutConstraints:
    merged = copy_constraints(constraints)
    if intent_plan is None:
        return merged

    explicit_position_refs = _explicit_position_refs(constraints)
    explicit_floorplan_refs = _explicit_floorplan_refs(constraints)

    inferred_anchor_by_ref = {
        anchor.ref: anchor
        for anchor in intent_plan.edge_anchors
    }
    for idx, anchor in enumerate(merged.edge_anchors):
        inferred = inferred_anchor_by_ref.get(anchor.ref)
        if (
            inferred is not None
            and anchor.rot_deg is None
            and inferred.rot_deg is not None
        ):
            merged.edge_anchors[idx] = replace(anchor, rot_deg=inferred.rot_deg)

    explicit_refs = {anchor.ref for anchor in merged.edge_anchors}
    for anchor in intent_plan.edge_anchors:
        if (
            anchor.ref not in explicit_refs
            and anchor.ref not in explicit_position_refs
        ):
            merged.edge_anchors.append(anchor)
            explicit_refs.add(anchor.ref)

    explicit_face_refs = {face.ref for face in merged.face_edges}
    for face_edge in intent_plan.face_edges:
        if (
            face_edge.ref not in explicit_face_refs
            and face_edge.ref not in explicit_position_refs
        ):
            merged.face_edges.append(face_edge)
            explicit_face_refs.add(face_edge.ref)

    explicit_keepouts = {
        (keepout.x_min, keepout.y_min, keepout.x_max, keepout.y_max)
        for keepout in merged.keepouts
    }
    for keepout in intent_plan.keepouts:
        key = (keepout.x_min, keepout.y_min, keepout.x_max, keepout.y_max)
        if key not in explicit_keepouts:
            merged.keepouts.append(keepout)
            explicit_keepouts.add(key)

    explicit_fixed_refs = {fixed.ref for fixed in merged.fixed}
    for fixed in intent_plan.fixed_positions:
        if (
            fixed.ref not in explicit_fixed_refs
            and fixed.ref not in explicit_position_refs
        ):
            merged.fixed.append(fixed)
            explicit_fixed_refs.add(fixed.ref)

    # Merge near/far constraints from RF path inference and other intent
    # sources.  Avoid duplicates by (ref, target_ref) pair.
    existing_near = {(nc.ref, nc.target_ref) for nc in merged.near}
    for nc in intent_plan.near_constraints:
        key = (nc.ref, nc.target_ref)
        if key not in existing_near:
            merged.near.append(nc)
            existing_near.add(key)

    existing_far = {(fc.ref, fc.target_ref) for fc in merged.far}
    for fc in intent_plan.far_constraints:
        key = (fc.ref, fc.target_ref)
        if key not in existing_far:
            merged.far.append(fc)
            existing_far.add(key)

    existing_align = {
        (tuple(constraint.refs), constraint.axis, constraint.value_mm)
        for constraint in merged.align
    }
    for constraint in intent_plan.align_constraints:
        refs = [ref for ref in constraint.refs if ref not in explicit_floorplan_refs]
        if len(refs) < 2:
            continue
        constraint = AlignConstraint(
            refs=refs,
            axis=constraint.axis,
            value_mm=constraint.value_mm,
        )
        key = (tuple(constraint.refs), constraint.axis, constraint.value_mm)
        if key not in existing_align:
            merged.align.append(constraint)
            existing_align.add(key)

    existing_distribute = {
        (
            tuple(constraint.refs),
            constraint.axis,
            constraint.start_mm,
            constraint.end_mm,
        )
        for constraint in merged.distribute
    }
    for constraint in intent_plan.distribute_constraints:
        refs = [ref for ref in constraint.refs if ref not in explicit_floorplan_refs]
        if len(refs) < 2:
            continue
        constraint = DistributeConstraint(
            refs=refs,
            axis=constraint.axis,
            start_mm=constraint.start_mm,
            end_mm=constraint.end_mm,
        )
        key = (
            tuple(constraint.refs),
            constraint.axis,
            constraint.start_mm,
            constraint.end_mm,
        )
        if key not in existing_distribute:
            merged.distribute.append(constraint)
            existing_distribute.add(key)

    return merged


def _merge_inferred_fixed_positions(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
) -> LayoutConstraints:
    merged = copy_constraints(constraints)
    if intent_plan is None:
        return merged

    explicit_fixed_refs = {fixed.ref for fixed in merged.fixed}
    for fixed in intent_plan.fixed_positions:
        if fixed.ref not in explicit_fixed_refs:
            merged.fixed.append(fixed)
            explicit_fixed_refs.add(fixed.ref)
    return merged


def _with_power_zone(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
) -> LayoutConstraints:
    zoned = _merge_inferred_edge_anchors(constraints, intent_plan)
    if zoned.outline is None or intent_plan is None:
        return zoned

    power_refs = sorted(
        set(intent_plan.refs_with_kind("power_input"))
        | set(intent_plan.refs_with_kind("power_cluster"))
    )
    if not power_refs:
        return zoned

    outline = zoned.outline
    y_mid = outline.y_min + outline.height_mm * 0.55
    zoned.zones.append(
        AnchorZone(
            group_name="",
            x_min=outline.x_min,
            y_min=y_mid,
            x_max=outline.x_min + outline.width_mm * 0.55,
            y_max=outline.y_max,
            refs=power_refs,
        )
    )
    return zoned


def _with_power_topology(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
    power_topology: PowerTopology | None,
) -> LayoutConstraints:
    powered = _with_power_zone(constraints, intent_plan)
    if power_topology is None or not power_topology.chains:
        return powered

    refs = power_topology.refs()
    if powered.outline is not None and refs:
        outline = powered.outline
        powered.zones.append(
            AnchorZone(
                group_name="",
                x_min=outline.x_min,
                y_min=outline.y_min + outline.height_mm * 0.55,
                x_max=outline.x_min + outline.width_mm * 0.70,
                y_max=outline.y_max,
                refs=refs,
            )
        )

    for chain in power_topology.chains:
        ordered = chain.ordered_refs
        for target_ref, ref in zip(ordered, ordered[1:]):
            powered.near.append(
                NearConstraint(ref=ref, target_ref=target_ref, distance_mm=10.0)
            )
    return powered


def _with_cluster_zone(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
) -> LayoutConstraints:
    zoned = _merge_inferred_edge_anchors(constraints, intent_plan)
    if zoned.outline is None or intent_plan is None:
        return zoned

    service_refs = sorted(
        set(intent_plan.refs_with_kind("edge_connector"))
        | set(intent_plan.refs_with_kind("board_ui"))
        | set(intent_plan.refs_with_kind("power_input"))
        | set(intent_plan.refs_with_kind("power_cluster"))
        | set(intent_plan.refs_with_kind("test_debug"))
    )
    if not service_refs:
        return zoned

    outline = zoned.outline
    zoned.zones.append(
        AnchorZone(
            group_name="",
            x_min=outline.x_min,
            y_min=outline.y_min + outline.height_mm * 0.60,
            x_max=outline.x_max,
            y_max=outline.y_max,
            refs=service_refs,
        )
    )
    return zoned


def _with_module_socket_zone(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
) -> LayoutConstraints:
    zoned = _merge_inferred_edge_anchors(constraints, intent_plan)
    if zoned.outline is None or intent_plan is None:
        return zoned

    explicit_refs = _explicit_position_refs(constraints)
    module_refs = [
        ref
        for ref in sorted(intent_plan.refs_with_kind("module_socket"))
        if ref not in explicit_refs
    ]
    if not module_refs:
        return zoned

    outline = zoned.outline
    zoned.zones.append(
        AnchorZone(
            group_name="",
            x_min=outline.x_min + outline.width_mm * 0.25,
            y_min=outline.y_min + outline.height_mm * 0.04,
            x_max=outline.x_min + outline.width_mm * 0.75,
            y_max=outline.y_min + outline.height_mm * 0.96,
            refs=module_refs,
        )
    )
    return zoned


def _with_panel_template(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
) -> LayoutConstraints:
    return _merge_inferred_edge_anchors(constraints, intent_plan)


def _channel_slot_refs(channel: RepeatedChannelIntent) -> list[str]:
    if channel.slots:
        slot_refs: list[str] = []
        for slot in sorted(
            channel.slots,
            key=lambda item: (item.slot_index, item.channel_number),
        ):
            primary_refs = [
                *slot.sensor_refs,
                *slot.connector_refs,
                *slot.other_refs,
            ]
            refs = primary_refs or slot.refs
            for ref in sorted(refs):
                if ref not in slot.passive_refs and ref not in slot_refs:
                    slot_refs.append(ref)
        if slot_refs:
            return slot_refs

    ref_counts: dict[str, int] = {}
    for refs in channel.refs_by_channel.values():
        for ref in refs:
            ref_counts[ref] = ref_counts.get(ref, 0) + 1

    slot_refs: list[str] = []
    for channel_number in sorted(channel.refs_by_channel):
        unique_refs = [
            ref
            for ref in channel.refs_by_channel[channel_number]
            if ref_counts.get(ref, 0) == 1
        ]
        refs = unique_refs or channel.refs_by_channel[channel_number]
        for ref in sorted(refs):
            if ref not in slot_refs:
                slot_refs.append(ref)
    return slot_refs


def _with_repeated_channel_array(
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
) -> LayoutConstraints:
    arrayed = _merge_inferred_edge_anchors(constraints, intent_plan)
    if arrayed.outline is None or intent_plan is None:
        return arrayed

    outline = arrayed.outline
    edge_refs = {anchor.ref for anchor in arrayed.edge_anchors or []}
    for channel in intent_plan.repeated_channels:
        slot_refs = [
            ref for ref in _channel_slot_refs(channel) if ref not in edge_refs
        ]
        if len(slot_refs) < 2:
            continue

        x_pad = outline.width_mm * 0.12
        y = outline.y_min + outline.height_mm * 0.25
        start_x = outline.x_min + x_pad
        end_x = outline.x_max - x_pad
        arrayed.distribute.append(
            DistributeConstraint(
                refs=slot_refs,
                axis="x",
                start_mm=start_x,
                end_mm=end_x,
            )
        )
        arrayed.align.append(AlignConstraint(refs=slot_refs, axis="y", value_mm=y))
        arrayed.zones.append(
            AnchorZone(
                group_name="",
                x_min=outline.x_min,
                y_min=outline.y_min,
                x_max=outline.x_max,
                y_max=outline.y_min + outline.height_mm * 0.55,
                refs=slot_refs,
            )
        )

        slots = [slot for slot in channel.slots if slot.refs]
        if slots:
            slot_width = (end_x - start_x) / max(len(slots), 1)
            for idx, slot in enumerate(slots):
                refs = [ref for ref in slot.refs if ref not in edge_refs]
                if not refs:
                    continue
                slot_x_min = start_x + idx * slot_width - slot_width * 0.35
                slot_x_max = start_x + (idx + 1) * slot_width + slot_width * 0.35
                arrayed.zones.append(
                    AnchorZone(
                        group_name="",
                        x_min=max(outline.x_min, slot_x_min),
                        y_min=outline.y_min,
                        x_max=min(outline.x_max, slot_x_max),
                        y_max=outline.y_min + outline.height_mm * 0.62,
                        refs=refs,
                    )
                )

        if channel.controller_refs:
            bank_x_min = outline.x_min + outline.width_mm * 0.35
            bank_x_max = outline.x_min + outline.width_mm * 0.65
            arrayed.zones.append(
                AnchorZone(
                    group_name="",
                    x_min=bank_x_min,
                    y_min=outline.y_min + outline.height_mm * 0.42,
                    x_max=bank_x_max,
                    y_max=outline.y_min + outline.height_mm * 0.75,
                    refs=channel.controller_refs,
                )
            )
    return arrayed


def _annotate_ref_reasons(
    candidate: PlacementCandidate,
    constraints: LayoutConstraints,
    intent_plan: PlacementIntentPlan | None,
    power_topology: PowerTopology | None = None,
) -> None:
    fixed_refs = {fixed.ref for fixed in constraints.fixed or []}
    edge_by_ref = {anchor.ref: anchor for anchor in constraints.edge_anchors or []}
    face_refs = {face.ref for face in constraints.face_edges or []}
    mating_by_ref = {
        mating.ref: mating for mating in (intent_plan.mating_intents if intent_plan else [])
    }
    power_chain_by_ref = {}
    for chain in power_topology.chains if power_topology else []:
        for ref in chain.ordered_refs:
            power_chain_by_ref[ref] = chain
    slot_by_ref = {}
    if intent_plan is not None:
        for channel in intent_plan.repeated_channels:
            for slot in channel.slots:
                for ref in slot.refs:
                    slot_by_ref[ref] = slot
    zone_by_ref = {}
    for zone in constraints.zones or []:
        for ref in zone.refs or []:
            zone_by_ref[ref] = zone

    for placed in candidate.placed_parts:
        reasons: list[str] = []
        if placed.ref in fixed_refs:
            reasons.append("locked by fixed-position constraint")
        if placed.ref in edge_by_ref:
            reasons.append(f"anchored to {edge_by_ref[placed.ref].edge} board edge")
        if placed.ref in zone_by_ref:
            reasons.append("assigned to a placement zone")
        if placed.ref in face_refs:
            reasons.append("rotation constrained by face-edge intent")
        if placed.ref in mating_by_ref:
            mating = mating_by_ref[placed.ref]
            detail = mating.kind
            if mating.edge_preference:
                detail += f" facing {mating.edge_preference}"
            if mating.mating_side:
                detail += f" ({mating.mating_side})"
            reasons.append(f"mating intent: {detail}")
        if placed.ref in power_chain_by_ref:
            chain = power_chain_by_ref[placed.ref]
            reasons.append(
                f"power chain: {chain.source_net} from {chain.source_ref}"
            )
        if placed.ref in slot_by_ref:
            slot = slot_by_ref[placed.ref]
            reasons.append(f"channel slot: CH{slot.channel_number}")
        if intent_plan is not None:
            kinds = sorted(
                {intent.kind for intent in intent_plan.intents_for(placed.ref)}
            )
            if kinds:
                reasons.append("inferred intent: " + ", ".join(kinds))
        if not reasons:
            reasons.append(f"placed by {candidate.name} strategy")
        candidate.ref_reasons[placed.ref] = reasons


def _append_candidate(
    candidates: list[PlacementCandidate],
    name: str,
    groups: dict,
    constraints: LayoutConstraints,
    fp_bboxes: dict[str, tuple[float, float]],
    reasons: list[str],
    intent_plan: PlacementIntentPlan | None = None,
    power_topology: PowerTopology | None = None,
    fp_geometries: dict[str, object] | None = None,
    seed_memo: list[tuple[LayoutConstraints, list[PlacedPart]]] | None = None,
):
    # Round-10 WS37: place_parts is deterministic in its inputs, and on
    # boards where a strategy's intents are absent several strategies
    # produce IDENTICAL LayoutConstraints (verified on DPSG: 8 builds ->
    # 5 unique). Memoize the seed placement by constraints equality
    # (eq=True dataclass; unhashable -> linear scan, <=9 entries). Copy on
    # BOTH store and reuse (plan hazard #2) so no two candidates — and no
    # candidate + memo entry — ever share a mutable PlacedPart instance.
    placed = None
    if seed_memo is not None:
        for known_constraints, known_placed in seed_memo:
            if known_constraints == constraints:
                placed = [replace(p) for p in known_placed]
                break
    if placed is None:
        placed = place_parts(
            groups, constraints, fp_bboxes, fp_geometries=fp_geometries
        )
        if seed_memo is not None:
            seed_memo.append((constraints, [replace(p) for p in placed]))
    candidate = PlacementCandidate(
        name=name,
        placed_parts=placed,
        reasons=reasons,
        constraints=constraints,
    )
    _annotate_ref_reasons(candidate, constraints, intent_plan, power_topology)
    candidates.append(candidate)


def generate_placement_candidates(
    groups: dict,
    constraints: LayoutConstraints,
    fp_bboxes: dict[str, tuple[float, float]],
    intent_plan: PlacementIntentPlan | None = None,
    power_topology: PowerTopology | None = None,
    fp_geometries: dict[str, object] | None = None,
) -> list[PlacementCandidate]:
    """Generate deterministic placement candidates from available intent."""
    candidates: list[PlacementCandidate] = []
    seed_memo: list[tuple[LayoutConstraints, list[PlacedPart]]] = []

    _append_candidate(
        candidates,
        "baseline",
        groups,
        _merge_inferred_fixed_positions(constraints, intent_plan),
        fp_bboxes,
        ["explicit constraints, fixed mechanics, and default placement order"],
        intent_plan,
        power_topology,
        fp_geometries,
        seed_memo=seed_memo,
    )
    _append_candidate(
        candidates,
        "connector_edge_first",
        groups,
        _merge_inferred_edge_anchors(constraints, intent_plan),
        fp_bboxes,
        ["inferred connector edge anchors applied before primary parts"],
        intent_plan,
        power_topology,
        fp_geometries,
        seed_memo=seed_memo,
    )
    if intent_plan is not None and intent_plan.refs_with_kind("panel_template"):
        _append_candidate(
            candidates,
            "panel_template_grid",
            groups,
            _with_panel_template(constraints, intent_plan),
            fp_bboxes,
            ["corpus-derived panel/UI grid constraints applied"],
            intent_plan,
            power_topology,
            fp_geometries,
            seed_memo=seed_memo,
        )
    _append_candidate(
        candidates,
        "power_first",
        groups,
        _with_power_zone(constraints, intent_plan),
        fp_bboxes,
        ["power input and regulator-like parts biased into a power zone"],
        intent_plan,
        power_topology,
        fp_geometries,
        seed_memo=seed_memo,
    )
    _append_candidate(
        candidates,
        "power_topology_first",
        groups,
        _with_power_topology(constraints, intent_plan, power_topology),
        fp_bboxes,
        ["source/protection/conversion/storage/load power chains biased together"],
        intent_plan,
        power_topology,
        fp_geometries,
        seed_memo=seed_memo,
    )
    _append_candidate(
        candidates,
        "cluster_first",
        groups,
        _with_cluster_zone(constraints, intent_plan),
        fp_bboxes,
        ["edge/UI/power/debug refs biased into a shared service zone"],
        intent_plan,
        power_topology,
        fp_geometries,
        seed_memo=seed_memo,
    )
    _append_candidate(
        candidates,
        "module_socket_central",
        groups,
        _with_module_socket_zone(constraints, intent_plan),
        fp_bboxes,
        ["plug-in module sockets biased into the internal board area"],
        intent_plan,
        power_topology,
        fp_geometries,
        seed_memo=seed_memo,
    )
    _append_candidate(
        candidates,
        "repeated_channel_array",
        groups,
        _with_repeated_channel_array(constraints, intent_plan),
        fp_bboxes,
        ["repeated channel refs aligned and distributed as an ordered array"],
        intent_plan,
        power_topology,
        fp_geometries,
        seed_memo=seed_memo,
    )

    if intent_plan is not None and intent_plan.backend_status.enabled:
        _append_candidate(
            candidates,
            "optional_backend_ready",
            groups,
            _with_cluster_zone(constraints, intent_plan),
            fp_bboxes,
            [
                "optional optimization backends detected; using deterministic "
                "core strategy until backend-specific solvers are enabled"
            ],
            intent_plan,
            power_topology,
            fp_geometries,
            seed_memo=seed_memo,
        )

    return candidates
