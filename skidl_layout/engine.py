from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, replace

from .candidates import (
    PlacementCandidate,
    copy_constraints,
    generate_placement_candidates,
)
from .connector_metadata import (
    infer_connector_mating_face,
    normalize_local_exit,
    rotation_for_local_exit,
)
from .constraints import (
    BoardCutout,
    BoardOutline,
    EdgeAnchor,
    FixedPosition,
    KeepOut,
    LayoutConstraints,
)
from .context import LayoutContext
from .decaps import refine_candidate_decaps
from .geometry import (
    FootprintGeometry,
    geometry_bboxes,
    load_footprint_geometries,
    transform_point,
)
from .hierarchy import PlacementGroup, extract_groups
from .intent import PlacementIntentPlan, infer_placement_intents
from .orientation import refine_candidate_orientations
from .placer import (
    derive_outline,
    derive_outline_from_circuit,
    _edge_anchor_origin_position,
    _footprint_name,
)
from .power import PowerRoutePlan, infer_power_topology, plan_power_routes
from .reader import read_board_outline
from .refinement import (
    _clone_placed,
    refine_candidate_placement,
    refine_placement,
)
from .report import PlacementReport, build_placement_report
from .roles import GND_NET_RE, POWER_NET_RE, classify_parts, is_ui_grid_part
from .routability import RoutabilityFeedback
from .scoring import LayoutScore, score_placement, score_placement_quick
from .grid import choose_grid_columns, points_form_clean_grid
from .validator import (
    ValidationResult,
    _same_physical_side,
    _through_board_pads_collide,
    validate,
)
from .writer import PlacedPart, load_footprint_bboxes


AUTO_OUTLINE_MAX_DENSITY_GROWTH = 1.12


@dataclass
class LayoutResult:
    placed_parts: list[PlacedPart]
    outline: BoardOutline | None
    validation: ValidationResult
    score: LayoutScore
    power_plan: PowerRoutePlan
    groups: dict[int | None, PlacementGroup]
    fp_bboxes: dict[str, tuple[float, float]]
    candidates: list[PlacementCandidate] | None = None
    intent_plan: PlacementIntentPlan | None = None
    report: PlacementReport | None = None
    fp_geometries: dict[str, FootprintGeometry] | None = None
    routability: RoutabilityFeedback | None = None
    cutouts: list[BoardCutout] | None = None

    @property
    def ok(self) -> bool:
        return self.validation.ok and self.score.ok

    def to_dict(self) -> dict:
        result = {
            "ok": self.ok,
            "placed_parts": [
                {
                    "ref": placed.ref,
                    "x_mm": placed.x_mm,
                    "y_mm": placed.y_mm,
                    "rot_deg": placed.rot_deg,
                    "footprint": placed.footprint,
                    "side": getattr(placed, "side", "front"),
                }
                for placed in self.placed_parts
            ],
            "score": self.score.to_dict(),
            "validation": {
                "ok": self.validation.ok,
                "overlaps": list(self.validation.overlaps),
                "outline_violations": list(self.validation.outline_violations),
                "keepout_violations": list(self.validation.keepout_violations),
                "cutout_violations": list(
                    getattr(self.validation, "cutout_violations", []) or []
                ),
                "missing_refs": list(self.validation.missing_refs),
                "total_parts": self.validation.total_parts,
                "placed_parts": self.validation.placed_parts,
            },
        }
        if self.report is not None:
            result["report"] = self.report.to_dict()
        if self.routability is not None:
            result["routability"] = self.routability.to_dict()
        if self.intent_plan is not None:
            result["intent_plan"] = self.intent_plan.to_dict()
        if self.outline is not None:
            result["outline"] = {
                "x_min_mm": self.outline.x_min,
                "y_min_mm": self.outline.y_min,
                "x_max_mm": self.outline.x_max,
                "y_max_mm": self.outline.y_max,
                "width_mm": self.outline.width_mm,
                "height_mm": self.outline.height_mm,
                "corner_radius_mm": getattr(self.outline, "corner_radius_mm", 0.0),
            }
        if self.cutouts:
            result["cutouts"] = [
                cutout.to_dict() if hasattr(cutout, "to_dict") else dict(cutout)
                for cutout in self.cutouts
            ]
        return result

    def summary(self) -> str:
        lines = [
            self.validation.summary(),
            self.score.summary(),
            self.power_plan.summary(),
        ]
        if self.report is not None:
            lines.append(self.report.summary())
        if self.routability is not None:
            lines.append(self.routability.summary())
        if self.intent_plan is not None:
            lines.append(self.intent_plan.summary())
        if self.outline is not None:
            lines.insert(
                0,
                (
                    f"Outline: {self.outline.width_mm:.1f}mm x "
                    f"{self.outline.height_mm:.1f}mm"
                ),
            )
        return "\n\n".join(lines)


@dataclass
class _FinalizedCandidate:
    candidate: PlacementCandidate
    placed_parts: list[PlacedPart]
    outline: BoardOutline | None
    constraints: LayoutConstraints
    validation: ValidationResult
    score: LayoutScore
    keepouts: list[KeepOut]


@dataclass(frozen=True)
class _FinalizeParams:
    """Frozen, picklable bundle of the plan_layout locals that the former
    ``_finalize_candidate`` closure read (round-6 WS21). ``circuit`` and ``ctx``
    stay separate args of :func:`_finalize_candidate_impl` so a worker can pass a
    snapshot + rebuilt context; callables (emit/progress) are never pickled."""

    resolved_bboxes: dict
    fp_geometries: dict
    clearance_mm: float
    board_layers: int
    margin_mm: float
    corner_radius_mm: float | None
    form_factor: object
    auto_outline: bool
    resolved_outline: BoardOutline | None
    resolved_constraints: LayoutConstraints
    density_outline: BoardOutline | None
    intent_plan: PlacementIntentPlan
    derive_outline_if_missing: bool
    constraints: LayoutConstraints | None


def _note_move(
    candidate: PlacementCandidate,
    placed_parts: list[PlacedPart],
    refs: list[str],
    reason: str,
    ref_reason: str,
) -> None:
    if not refs:
        return
    candidate.placed_parts = placed_parts
    candidate.reasons.append(reason)
    for ref in refs:
        candidate.ref_reasons.setdefault(ref, []).append(ref_reason)


def _finalize_candidate_impl(
    candidate: PlacementCandidate,
    circuit,
    params: _FinalizeParams,
    ctx,
    emit,
    progress,
) -> tuple[_FinalizedCandidate, BoardOutline | None]:
    """Module-level extraction of plan_layout's former ``_finalize_candidate``
    closure (round-6 WS21). Behavior is byte-identical to the closure. Returns
    ``(finalized, density_outline)``: the ``density_outline`` nonlocal is threaded
    back out (hazard #2) so the caller re-assigns its own copy. The worker path
    passes ``emit=None`` / ``progress=None`` (identical to the sequential default,
    where the progress lambda is only built when ``progress`` is set)."""
    resolved_bboxes = params.resolved_bboxes
    fp_geometries = params.fp_geometries
    clearance_mm = params.clearance_mm
    board_layers = params.board_layers
    margin_mm = params.margin_mm
    corner_radius_mm = params.corner_radius_mm
    form_factor = params.form_factor
    auto_outline = params.auto_outline
    resolved_outline = params.resolved_outline
    resolved_constraints = params.resolved_constraints
    density_outline = params.density_outline
    intent_plan = params.intent_plan
    derive_outline_if_missing = params.derive_outline_if_missing
    constraints = params.constraints
    _emit = emit

    candidate_outline = resolved_outline
    candidate_constraints = copy_constraints(
        candidate.constraints or resolved_constraints
    )
    placed_parts = list(candidate.placed_parts)

    if auto_outline:
        min_area = (
            density_outline.width_mm * density_outline.height_mm
            if density_outline is not None
            else 0.0
        )
        candidate_outline = _derive_outline_for_edge_anchors(
            placed_parts,
            resolved_bboxes,
            margin_mm=margin_mm,
            form_factor=form_factor,
            min_area_mm2=min_area,
            max_min_area_growth=AUTO_OUTLINE_MAX_DENSITY_GROWTH,
            intent_plan=intent_plan,
            constraints=candidate_constraints,
            fp_geometries=fp_geometries,
        )
        if corner_radius_mm is not None:
            candidate_outline.corner_radius_mm = max(
                0.0, float(corner_radius_mm)
            )
        candidate_constraints.outline = candidate_outline

        placed_parts, moved_mounting_refs = _snap_mounting_holes_to_outline_corners(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_mounting_refs,
            "mounting holes snapped to final auto-outline corners",
            "snapped to final auto-outline corner",
        )

        placed_parts, moved_edge_refs = _snap_edge_anchors_to_outline(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_edge_refs,
            "edge connectors snapped to final auto-outline edges",
            "snapped to final auto-outline edge",
        )

        placed_parts, gridded_passive_refs = _arrange_passive_grid_between_opposing_headers(
            placed_parts,
            circuit,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            gridded_passive_refs,
            "simple passives arranged on an even grid between opposing headers",
            "arranged on passive grid between opposing headers",
        )
        _lock_current_positions(
            candidate_constraints,
            placed_parts,
            gridded_passive_refs,
        )

        placed_parts, moved_neighbor_refs = _legalize_edge_anchor_neighbors(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_neighbor_refs,
            "near-edge parts nudged clear of final edge connectors",
            "nudged clear of final edge connector",
        )

        placed_parts, moved_interior_refs = _legalize_small_parts_from_outline(
            placed_parts,
            circuit,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_interior_refs,
            "small passive parts nudged away from board outline",
            "nudged away from board outline",
        )
    elif candidate_outline is None and derive_outline_if_missing:
        min_area = 0.0
        if not form_factor:
            density_outline = derive_outline_from_circuit(
                circuit, resolved_bboxes
            )
            min_area = density_outline.width_mm * density_outline.height_mm
        candidate_outline = derive_outline(
            placed_parts,
            resolved_bboxes,
            margin_mm=margin_mm,
            form_factor=form_factor,
            min_area_mm2=min_area,
            max_min_area_growth=AUTO_OUTLINE_MAX_DENSITY_GROWTH,
        )
        if corner_radius_mm is not None:
            candidate_outline.corner_radius_mm = max(
                0.0, float(corner_radius_mm)
            )
        candidate_constraints.outline = candidate_outline

    if candidate_outline is not None and not auto_outline:
        placed_parts, moved_mounting_refs = _snap_mounting_holes_to_outline_corners(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_mounting_refs,
            "mounting holes snapped to fixed-outline corners",
            "snapped to fixed-outline corner",
        )

        placed_parts, moved_edge_refs = _snap_edge_anchors_to_outline(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_edge_refs,
            "edge connectors snapped to fixed-outline edges",
            "snapped to fixed-outline edge",
        )

        placed_parts, moved_neighbor_refs = _legalize_edge_anchor_neighbors(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_neighbor_refs,
            "near-edge parts nudged clear of fixed-outline edge connectors",
            "nudged clear of fixed-outline edge connector",
        )

        placed_parts, gridded_subject_refs = _spread_grid_subjects_on_generous_outline(
            placed_parts,
            circuit,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            gridded_subject_refs,
            "visible grid subjects spread over generous fixed outline",
            "spread over generous fixed outline grid",
        )

        placed_parts, moved_interior_refs = _legalize_small_parts_from_outline(
            placed_parts,
            circuit,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_interior_refs,
            "small passive parts nudged away from fixed board outline",
            "nudged away from fixed board outline",
        )

    candidate_constraints = _final_outline_constraints(
        candidate_constraints,
        candidate_outline,
        intent_plan,
        lock_edge_anchors=not auto_outline,
    )
    placed_parts = _apply_assembly_sides(placed_parts, intent_plan)
    candidate.placed_parts = placed_parts
    candidate.constraints = candidate_constraints
    post_refinement_constraints = _constraints_with_effective_keepouts(
        candidate_constraints,
        placed_parts,
        intent_plan,
        resolved_bboxes,
        fp_geometries,
        candidate_outline,
    )
    post_refinement = refine_placement(
        placed_parts,
        circuit,
        resolved_bboxes,
        constraints=post_refinement_constraints,
        fp_geometries=fp_geometries,
        clearance_mm=clearance_mm,
        board_layers=board_layers,
        max_passes=1,
        max_movable_refs=32,
        max_pair_swaps=8,
        ctx=ctx,
        progress=(
            (lambda m, _n=candidate.name: _emit(f"[{_n}] post-anchor {m}"))
            if progress is not None
            else None
        ),
    )
    if post_refinement.accepted_count:
        placed_parts = post_refinement.placed_parts
        candidate.placed_parts = placed_parts
        candidate.reasons.append(
            (
                "post-anchor local refinement accepted "
                f"{post_refinement.accepted_count} score-gated adjustment(s): "
                f"score {post_refinement.start_score:.1f} -> "
                f"{post_refinement.final_score:.1f} "
                f"(penalty {post_refinement.start_penalty:.1f} -> "
                f"{post_refinement.final_penalty:.1f})"
            )
        )
        for ref, reasons in post_refinement.ref_reasons.items():
            candidate.ref_reasons.setdefault(ref, []).extend(reasons)

    if auto_outline:
        min_area = (
            density_outline.width_mm * density_outline.height_mm
            if density_outline is not None
            else 0.0
        )
        tightened_outline = _derive_outline_for_edge_anchors(
            placed_parts,
            resolved_bboxes,
            margin_mm=margin_mm,
            form_factor=form_factor,
            min_area_mm2=min_area,
            max_min_area_growth=AUTO_OUTLINE_MAX_DENSITY_GROWTH,
            intent_plan=intent_plan,
            constraints=candidate_constraints,
            fp_geometries=fp_geometries,
        )
        if corner_radius_mm is not None:
            tightened_outline.corner_radius_mm = max(
                0.0, float(corner_radius_mm)
            )
        candidate_outline = tightened_outline
        candidate_constraints.outline = candidate_outline

        placed_parts, moved_mounting_refs = _snap_mounting_holes_to_outline_corners(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_mounting_refs,
            "mounting holes snapped to tightened auto-outline corners",
            "snapped to tightened auto-outline corner",
        )

        placed_parts, moved_edge_refs = _snap_edge_anchors_to_outline(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_edge_refs,
            "edge connectors snapped to tightened auto-outline edges",
            "snapped to tightened auto-outline edge",
        )

        placed_parts, moved_neighbor_refs = _legalize_edge_anchor_neighbors(
            placed_parts,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_neighbor_refs,
            "near-edge parts nudged clear of tightened edge connectors",
            "nudged clear of tightened edge connector",
        )

        placed_parts, moved_interior_refs = _legalize_small_parts_from_outline(
            placed_parts,
            circuit,
            candidate_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
        )
        _note_move(
            candidate,
            placed_parts,
            moved_interior_refs,
            "small passive parts nudged away from tightened board outline",
            "nudged away from tightened board outline",
        )

        placed_parts = _apply_assembly_sides(placed_parts, intent_plan)
        candidate.placed_parts = placed_parts

    candidate_keepouts = _effective_keepouts(
        candidate_constraints,
        placed_parts,
        intent_plan,
        resolved_bboxes,
        fp_geometries,
        candidate_outline,
    )
    validation = validate(
        placed_parts,
        circuit,
        resolved_bboxes,
        clearance_mm=clearance_mm,
        outline=candidate_outline,
        keepouts=candidate_keepouts,
        cutouts=getattr(candidate_constraints, "cutouts", None),
        fp_geometries=fp_geometries,
    )
    raw_score = score_placement(
        placed_parts,
        circuit,
        resolved_bboxes,
        outline=candidate_outline,
        keepouts=candidate_keepouts,
        cutouts=getattr(candidate_constraints, "cutouts", None),
        fp_geometries=fp_geometries,
        clearance_mm=clearance_mm,
        board_layers=board_layers,
        ctx=ctx,
    )
    edge_score = _apply_edge_intent_score(
        raw_score,
        placed_parts,
        resolved_bboxes,
        candidate_outline,
        intent_plan,
        constraints=candidate_constraints,
        fp_geometries=fp_geometries,
    )
    score = _apply_panel_mechanical_outline_score(
        edge_score,
        placed_parts,
        resolved_bboxes,
        candidate_outline,
        intent_plan,
        fp_geometries=fp_geometries,
    )
    candidate.score = score.score
    finalized = _FinalizedCandidate(
        candidate=candidate,
        placed_parts=placed_parts,
        outline=candidate_outline,
        constraints=candidate_constraints,
        validation=validation,
        score=score,
        keepouts=candidate_keepouts,
    )
    return finalized, density_outline



def _finalize_identity_probe(circuit, fp_lib_dirs):
    """WS21.5 backstop: run :func:`_finalize_candidate_impl` on one refined
    candidate with (live circuit + live ctx) vs (snapshot + snapshot-rebuilt
    ctx); the caller asserts the two ``_FinalizedCandidate`` results are
    byte-equal (hazard #7). Mirrors the plan_layout finalize setup for one
    candidate under the default (auto-outline) path."""
    import copy

    from .snapshot import snapshot_circuit

    fp_geometries = _resolve_geometries(circuit, fp_lib_dirs)
    resolved_bboxes = _resolve_bboxes(circuit, None, fp_lib_dirs)
    resolved_bboxes.update(geometry_bboxes(fp_geometries))
    resolved_outline = _resolve_outline(None, None, None)
    resolved_constraints = _copy_constraints(None, resolved_outline)
    auto_outline = resolved_outline is None
    density_outline = None
    form_factor = getattr(resolved_constraints, "form_factor", None)
    if auto_outline and not form_factor:
        density_outline = _compact_auto_outline_seed(
            circuit, derive_outline_from_circuit(circuit, resolved_bboxes)
        )
        resolved_outline = density_outline
        resolved_constraints.outline = resolved_outline
    groups = extract_groups(circuit)
    intent_plan = infer_placement_intents(circuit, outline=resolved_outline)
    power_topology = infer_power_topology(circuit)
    candidates = generate_placement_candidates(
        groups,
        resolved_constraints,
        resolved_bboxes,
        intent_plan=intent_plan,
        power_topology=power_topology,
        fp_geometries=fp_geometries,
    )
    candidate = candidates[0]
    live_ctx = LayoutContext.from_circuit(circuit)
    _refine_candidate_trio(
        candidate, circuit, resolved_bboxes, fp_geometries, 0.5, 2, live_ctx, None
    )
    params = _FinalizeParams(
        resolved_bboxes=resolved_bboxes,
        fp_geometries=fp_geometries,
        clearance_mm=0.5,
        board_layers=2,
        margin_mm=3.0,
        corner_radius_mm=None,
        form_factor=form_factor,
        auto_outline=auto_outline,
        resolved_outline=resolved_outline,
        resolved_constraints=resolved_constraints,
        density_outline=density_outline,
        intent_plan=intent_plan,
        derive_outline_if_missing=True,
        constraints=None,
    )
    live_fin, _ = _finalize_candidate_impl(
        copy.deepcopy(candidate), circuit, params, live_ctx, None, None
    )
    snap = snapshot_circuit(circuit)
    snap_ctx = LayoutContext.from_circuit(snap)
    snap_fin, _ = _finalize_candidate_impl(
        copy.deepcopy(candidate), snap, params, snap_ctx, None, None
    )
    return live_fin, snap_fin


def _filter_candidates(
    candidates: list[PlacementCandidate],
    candidate_names: list[str] | None,
) -> list[PlacementCandidate]:
    """Restrict candidates to a requested subset (speed knob).

    Resolution order: the explicit ``candidate_names`` argument, else the
    ``SKIDL_LAYOUT_CANDIDATES`` env var (comma-separated), else no filter.
    Unknown names raise ``ValueError`` listing the available strategies so a
    typo fails loudly instead of silently planning everything.
    """
    if candidate_names is None:
        env = os.environ.get("SKIDL_LAYOUT_CANDIDATES")
        if env:
            candidate_names = [name.strip() for name in env.split(",") if name.strip()]
    if not candidate_names:
        return candidates

    available = {candidate.name for candidate in candidates}
    requested = list(dict.fromkeys(candidate_names))  # de-dup, keep order
    unknown = [name for name in requested if name not in available]
    if unknown:
        raise ValueError(
            f"unknown candidate name(s) {unknown}; available for this circuit: "
            f"{sorted(available)}"
        )
    wanted = set(requested)
    return [candidate for candidate in candidates if candidate.name in wanted]


def _resolve_max_candidates(max_candidates: int | None) -> int | None:
    """Explicit kwarg wins, else SKIDL_LAYOUT_MAX_CANDIDATES env default."""
    if max_candidates is not None:
        return max_candidates
    env = os.environ.get("SKIDL_LAYOUT_MAX_CANDIDATES")
    if env:
        try:
            return int(env)
        except ValueError:
            raise ValueError(
                f"SKIDL_LAYOUT_MAX_CANDIDATES must be an integer, got {env!r}"
            )
    return None


def _prune_candidates(
    candidates: list[PlacementCandidate],
    max_candidates: int | None,
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    outline,
    keepouts,
    cutouts,
    fp_geometries,
    clearance_mm: float,
    ctx,
) -> list[PlacementCandidate]:
    """Keep only the top ``max_candidates`` by a cheap seed quick-score.

    The seed score (validate + HPWL + warnings, no refinement) is a heuristic
    predictor of the refined result — good enough for fast iteration, NOT a
    substitute for scoring the final board. Ranking is
    ``(ok desc, penalty asc, name asc)`` and fully deterministic.
    """
    if max_candidates is None or max_candidates >= len(candidates) or max_candidates <= 0:
        return candidates
    scored = []
    for candidate in candidates:
        seed_score = score_placement_quick(
            candidate.placed_parts,
            circuit,
            fp_bboxes,
            outline=outline,
            keepouts=keepouts,
            cutouts=cutouts,
            fp_geometries=fp_geometries,
            clearance_mm=clearance_mm,
            ctx=ctx,
        )
        scored.append((candidate, seed_score))
    scored.sort(key=lambda item: (not item[1].ok, item[1].penalty, item[0].name))
    return [candidate for candidate, _ in scored[:max_candidates]]


def _candidate_seed_key(candidate: PlacementCandidate) -> tuple:
    """Dedup key for a candidate before refinement.

    The whole per-candidate pipeline (orientation/decap/placement refinement →
    finalization) is a deterministic pure function of the seed placement and the
    candidate constraints, so two candidates with an equal key produce an equal
    result. `optional_backend_ready` is a byte-identical rebuild of
    `cluster_first`, and intent-less strategies collapse onto
    `connector_edge_first`; refining those again is wasted work. Key components:
    the seed placement (ref/x/y/rot/side/footprint) and the constraint tree's
    repr (LayoutConstraints is a dataclass, so repr is stable and order-fixed).
    """
    placement = tuple(
        (
            part.ref,
            round(part.x_mm, 4),
            round(part.y_mm, 4),
            round(part.rot_deg, 4),
            getattr(part, "side", None),
            part.footprint,
        )
        for part in candidate.placed_parts
    )
    return (placement, repr(candidate.constraints))


def _copy_constraints(
    constraints: LayoutConstraints | None,
    outline: BoardOutline | None,
) -> LayoutConstraints:
    copied = copy_constraints(constraints)
    copied.outline = outline
    return copied


def _footprint_names(circuit) -> set[str]:
    names = set()
    for part in circuit.parts:
        fp = _footprint_name(part)
        if fp:
            names.add(fp)
    return names


def _resolve_bboxes(
    circuit,
    fp_bboxes: dict[str, tuple[float, float]] | None,
    fp_lib_dirs: list[str] | None,
) -> dict[str, tuple[float, float]]:
    if fp_bboxes is not None:
        return dict(fp_bboxes)
    if fp_lib_dirs is None:
        return {}
    return load_footprint_bboxes(_footprint_names(circuit), fp_lib_dirs)


def _resolve_geometries(
    circuit,
    fp_lib_dirs: list[str] | None,
) -> dict[str, FootprintGeometry]:
    if fp_lib_dirs is None:
        return {}
    return load_footprint_geometries(_footprint_names(circuit), fp_lib_dirs)


def _resolve_outline(
    constraints: LayoutConstraints | None,
    outline: BoardOutline | None,
    existing_pcb_path: str | None,
) -> BoardOutline | None:
    if outline is not None:
        return outline
    if constraints is not None and constraints.outline is not None:
        return constraints.outline
    if existing_pcb_path is not None:
        return read_board_outline(existing_pcb_path)
    return None


def _auto_outline_from_circuit(
    circuit,
    fp_bboxes: dict[str, tuple[float, float]],
    form_factor: str | None,
) -> BoardOutline:
    if form_factor:
        return derive_outline([], fp_bboxes, form_factor=form_factor)
    return derive_outline_from_circuit(circuit, fp_bboxes)


def _compact_auto_outline_seed(
    circuit,
    outline: BoardOutline | None,
) -> BoardOutline | None:
    """Trim the provisional auto outline when the board is mostly visible UI."""

    if outline is None or circuit is None:
        return outline

    visible_count = sum(
        1 for part in getattr(circuit, "parts", []) or [] if is_ui_grid_part(part)
    )
    if visible_count < 2:
        return outline

    if visible_count <= 3:
        scale = 0.92
    elif visible_count <= 5:
        scale = 0.88
    else:
        scale = 0.84

    return BoardOutline(
        width_mm=max(18.0, outline.width_mm * scale),
        height_mm=max(18.0, outline.height_mm * scale),
        corner_radius_mm=getattr(outline, "corner_radius_mm", 0.0),
    )


def _placed_bounds(
    placed: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> tuple[float, float, float, float]:
    geometry = (fp_geometries or {}).get(placed.footprint)
    if geometry is not None:
        return geometry.transformed_bounds(placed)
    width, height = fp_bboxes.get(placed.footprint, (2.0, 2.0))
    if placed.rot_deg % 180 == 90:
        width, height = height, width
    return (
        placed.x_mm - width / 2,
        placed.y_mm - height / 2,
        placed.x_mm + width / 2,
        placed.y_mm + height / 2,
    )


def _edge_anchor_map(
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None = None,
):
    anchors = {
        anchor.ref: anchor
        for anchor in ((intent_plan.edge_anchors if intent_plan else []) or [])
    }
    for anchor in ((constraints.edge_anchors if constraints else []) or []):
        anchors[anchor.ref] = anchor
    return anchors


def _edge_parallel(edge: str, bounds: tuple[float, float, float, float]) -> bool:
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    if edge in {"top", "bottom"}:
        return width + 0.2 >= height
    if edge in {"left", "right"}:
        return height + 0.2 >= width
    return True


def _edge_distance(
    edge: str,
    bounds: tuple[float, float, float, float],
    outline: BoardOutline,
    inset_mm: float,
) -> float | None:
    if edge == "top":
        return abs(bounds[1] - (outline.y_min + inset_mm))
    if edge == "bottom":
        return abs(bounds[3] - (outline.y_max - inset_mm))
    if edge == "left":
        return abs(bounds[0] - (outline.x_min + inset_mm))
    if edge == "right":
        return abs(bounds[2] - (outline.x_max - inset_mm))
    return None


def _mating_intent_by_ref(intent_plan: PlacementIntentPlan | None):
    if intent_plan is None:
        return {}
    return {
        intent.ref: intent
        for intent in intent_plan.mating_intents
    }


def _connector_mating_face_for_ref(
    ref: str,
    footprint: str,
    intent_plan: PlacementIntentPlan | None,
):
    intent = _mating_intent_by_ref(intent_plan).get(ref)
    kind = intent.kind if intent is not None else None
    text = f"{ref} {footprint}"
    return infer_connector_mating_face(None, text=text, mating_kind=kind)


def _local_bounds_for_face(
    width: float,
    height: float,
    geometry: FootprintGeometry | None,
):
    if geometry is not None:
        return geometry.body_bounds or geometry.bounds
    return (-width / 2, -height / 2, width / 2, height / 2)


def _face_local_points(
    bounds: tuple[float, float, float, float],
    local_exit: str,
    local_face_offset_mm: float | None,
) -> list[tuple[float, float]]:
    x_min, y_min, x_max, y_max = bounds
    local_exit = normalize_local_exit(local_exit)
    if local_exit == "+x":
        x = x_max if local_face_offset_mm is None else local_face_offset_mm
        return [(x, y_min), (x, y_max)]
    if local_exit == "-x":
        x = x_min if local_face_offset_mm is None else local_face_offset_mm
        return [(x, y_min), (x, y_max)]
    if local_exit == "+y":
        y = y_max if local_face_offset_mm is None else local_face_offset_mm
        return [(x_min, y), (x_max, y)]
    if local_exit == "-y":
        y = y_min if local_face_offset_mm is None else local_face_offset_mm
        return [(x_min, y), (x_max, y)]
    return []


def _bounds_at_origin(
    ref: str,
    footprint: str,
    width: float,
    height: float,
    rot_deg: float,
    geometry: FootprintGeometry | None,
):
    return _placed_bounds(
        PlacedPart(
            ref=ref,
            x_mm=0.0,
            y_mm=0.0,
            rot_deg=rot_deg,
            footprint=footprint,
        ),
        {footprint: (width, height)},
        {footprint: geometry} if geometry is not None else None,
    )


def _face_world_points(
    placed: PlacedPart,
    face,
    local_bounds: tuple[float, float, float, float],
    *,
    use_face_offset: bool,
) -> list[tuple[float, float]]:
    local_points = _face_local_points(
        local_bounds,
        face.local_exit,
        face.local_face_offset_mm if use_face_offset else None,
    )
    return [
        transform_point(placed.x_mm, placed.y_mm, placed.rot_deg, x, y)
        for x, y in local_points
    ]


def _edge_face_distance(
    edge: str,
    placed: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
    outline: BoardOutline,
    inset_mm: float,
    intent_plan: PlacementIntentPlan | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> float | None:
    width, height = fp_bboxes.get(placed.footprint, (2.0, 2.0))
    geometry = (fp_geometries or {}).get(placed.footprint)
    if geometry is None:
        return _edge_distance(
            edge,
            _placed_bounds(placed, fp_bboxes, fp_geometries),
            outline,
            inset_mm,
        )
    face = _connector_mating_face_for_ref(placed.ref, placed.footprint, intent_plan)
    if face is None:
        return _edge_distance(
            edge,
            _placed_bounds(placed, fp_bboxes, fp_geometries),
            outline,
            inset_mm,
        )

    local_bounds = _local_bounds_for_face(width, height, geometry)
    points = _face_world_points(
        placed,
        face,
        local_bounds,
        use_face_offset=geometry is not None,
    )
    if not points:
        return None
    if edge == "top":
        return abs((sum(y for _, y in points) / len(points)) - (outline.y_min + inset_mm))
    if edge == "bottom":
        return abs((sum(y for _, y in points) / len(points)) - (outline.y_max - inset_mm))
    if edge == "left":
        return abs((sum(x for x, _ in points) / len(points)) - (outline.x_min + inset_mm))
    if edge == "right":
        return abs((sum(x for x, _ in points) / len(points)) - (outline.x_max - inset_mm))
    return None


def _rotation_delta_deg(a: float, b: float) -> float:
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def _inferred_edge_rotation_for_ref(
    ref: str,
    edge: str,
    intent_plan: PlacementIntentPlan | None,
) -> float | None:
    if intent_plan is None:
        return None
    edge = str(edge or "").lower()
    for anchor in intent_plan.edge_anchors:
        if anchor.ref == ref and anchor.edge.lower() == edge:
            return anchor.rot_deg
    return None


def _expected_edge_rotation(
    edge: str,
    ref: str,
    face,
    intent_plan: PlacementIntentPlan | None,
) -> float | None:
    if face is not None:
        expected = rotation_for_local_exit(edge, face.local_exit)
        if expected is not None:
            return expected
    return _inferred_edge_rotation_for_ref(ref, edge, intent_plan)


def _rotation_faces_edge(rot_deg: float, expected: float | None) -> bool:
    if expected is None:
        return True
    return _rotation_delta_deg(rot_deg, expected) <= 1e-6


def _edge_anchor_origin_position_for_mating_face(
    anchor: EdgeAnchor,
    width: float,
    height: float,
    outline: BoardOutline,
    *,
    geometry: FootprintGeometry | None,
    ref: str,
    footprint: str,
    intent_plan: PlacementIntentPlan | None,
) -> tuple[float, float, float, float, float, float, float]:
    original = _edge_anchor_origin_position(
        anchor,
        width,
        height,
        outline,
        geometry=geometry,
        ref=ref,
        footprint=footprint,
    )
    if geometry is None:
        return original
    face = _connector_mating_face_for_ref(ref, footprint, intent_plan)
    if face is None:
        return original

    _, _, rot_deg, *_ = original
    local_bounds = _local_bounds_for_face(width, height, geometry)
    face_points = _face_world_points(
        PlacedPart(ref, 0.0, 0.0, rot_deg, footprint),
        face,
        local_bounds,
        use_face_offset=geometry is not None,
    )
    if not face_points:
        return original

    bounds = _bounds_at_origin(ref, footprint, width, height, rot_deg, geometry)
    face_x = sum(x for x, _ in face_points) / len(face_points)
    face_y = sum(y for _, y in face_points) / len(face_points)
    edge = anchor.edge.lower()
    x_mid = (outline.x_min + outline.x_max) / 2
    y_mid = (outline.y_min + outline.y_max) / 2

    origin_x = 0.0
    origin_y = 0.0
    if edge in {"top", "bottom"}:
        desired_x = anchor.offset_mm if anchor.offset_mm is not None else x_mid
        target_y = (
            outline.y_min + anchor.inset_mm
            if edge == "top"
            else outline.y_max - anchor.inset_mm
        )
        origin_x = desired_x - face_x
        origin_y = target_y - face_y
        moved = _translated_bounds(bounds, origin_x, origin_y)
        if moved[0] < outline.x_min:
            origin_x += outline.x_min - moved[0]
        if moved[2] > outline.x_max:
            origin_x -= moved[2] - outline.x_max
    elif edge in {"left", "right"}:
        desired_y = anchor.offset_mm if anchor.offset_mm is not None else y_mid
        target_x = (
            outline.x_min + anchor.inset_mm
            if edge == "left"
            else outline.x_max - anchor.inset_mm
        )
        origin_x = target_x - face_x
        origin_y = desired_y - face_y
        moved = _translated_bounds(bounds, origin_x, origin_y)
        if moved[1] < outline.y_min:
            origin_y += outline.y_min - moved[1]
        if moved[3] > outline.y_max:
            origin_y -= moved[3] - outline.y_max
    else:
        return original

    final_bounds = _translated_bounds(bounds, origin_x, origin_y)
    center_x = (final_bounds[0] + final_bounds[2]) / 2
    center_y = (final_bounds[1] + final_bounds[3]) / 2
    return (
        origin_x,
        origin_y,
        rot_deg,
        center_x,
        center_y,
        final_bounds[2] - final_bounds[0],
        final_bounds[3] - final_bounds[1],
    )


def _edge_parallel_required_refs(
    intent_plan: PlacementIntentPlan | None,
) -> set[str]:
    """Refs where the physical connector row should run parallel to an edge."""
    if intent_plan is None:
        return set()
    return {
        intent.ref
        for intent in intent_plan.mating_intents
        if intent.kind in {"header", "generic_connector"}
        and intent.mating_side == "pin_access"
    }


_CONNECTOR_EDGE_WARNING_RE = re.compile(
    r"^([^:]+): connector is [0-9.]+mm from nearest board edge$"
)


def _filter_fixed_floorplan_connector_warnings(
    score: LayoutScore,
    constraints: LayoutConstraints | None,
) -> LayoutScore:
    if constraints is None:
        return score
    fixed_refs = {fixed.ref for fixed in (constraints.fixed or [])}
    explicit_edge_refs = {anchor.ref for anchor in (constraints.edge_anchors or [])}
    fixed_only_refs = fixed_refs - explicit_edge_refs
    if not fixed_only_refs:
        return score

    warnings = []
    removed = 0
    for warning in score.warnings:
        match = _CONNECTOR_EDGE_WARNING_RE.match(warning)
        if match is not None and match.group(1) in fixed_only_refs:
            removed += 1
            continue
        warnings.append(warning)
    if removed == 0:
        return score
    return replace(
        score,
        score=min(100.0, score.score + removed * 5.0),
        warning_count=len(warnings),
        warnings=warnings,
    )


def _apply_edge_intent_score(
    score: LayoutScore,
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None = None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> LayoutScore:
    """Treat violated edge-mating intent as product risk, not decoration."""
    score = _filter_fixed_floorplan_connector_warnings(score, constraints)
    if outline is None:
        return score

    anchors = _edge_anchor_map(intent_plan, constraints)
    if not anchors:
        return score

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    warnings = list(score.warnings)
    penalty = 0.0
    parallel_required = _edge_parallel_required_refs(intent_plan)
    fixed_refs = {fixed.ref for fixed in (constraints.fixed if constraints else []) or []}
    explicit_edge_refs = {
        anchor.ref for anchor in ((constraints.edge_anchors if constraints else []) or [])
    }
    for ref, anchor in sorted(anchors.items()):
        placed = placed_by_ref.get(ref)
        if placed is None:
            continue
        if ref in fixed_refs and ref not in explicit_edge_refs:
            continue
        edge = anchor.edge.lower()
        bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
        distance = _edge_face_distance(
            edge,
            placed,
            fp_bboxes,
            outline,
            anchor.inset_mm,
            intent_plan,
            fp_geometries,
        )
        if distance is not None and distance > 1.0:
            warnings.append(
                f"{ref}: violates {edge}-edge mating intent "
                f"by {distance:.1f}mm"
            )
            penalty += 30.0 + min(distance * 2.0, 20.0)
        if ref in parallel_required and not _edge_parallel(edge, bounds):
            warnings.append(
                f"{ref}: connector row is not parallel to the {edge} edge"
            )
            penalty += 30.0

    if penalty <= 0.0:
        return score
    return replace(
        score,
        score=max(0.0, score.score - penalty),
        warning_count=len(warnings),
        warnings=warnings,
    )


def _bounds_inside_outline(
    bounds: tuple[float, float, float, float],
    outline: BoardOutline,
) -> bool:
    return (
        bounds[0] >= outline.x_min
        and bounds[1] >= outline.y_min
        and bounds[2] <= outline.x_max
        and bounds[3] <= outline.y_max
    )


def _panel_mechanical_refs(intent_plan: PlacementIntentPlan | None) -> set[str]:
    if intent_plan is None:
        return set()
    refs = set(intent_plan.refs_with_kind("front_panel_subject"))
    refs.update(intent_plan.refs_with_kind("panel_control"))
    refs.update(intent_plan.refs_with_kind("panel_jack"))
    refs.update(intent_plan.refs_with_kind("sensor_grid_subject"))
    for mating in intent_plan.mating_intents:
        if mating.kind in {
            "button",
            "encoder",
            "key",
            "nav_control",
            "panel_jack",
            "pot",
        }:
            refs.add(mating.ref)
    return refs


def _physical_or_placed_bounds(
    placed: PlacedPart,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[float, float, float, float]:
    geometry = (fp_geometries or {}).get(placed.footprint)
    if geometry is not None:
        return geometry.transformed_physical_bounds(placed)
    return _placed_bounds(placed, fp_bboxes, fp_geometries)


def _apply_panel_mechanical_outline_score(
    score: LayoutScore,
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> LayoutScore:
    """Panel subjects are invalid when their physical body crosses the board."""
    if outline is None:
        return score

    panel_refs = _panel_mechanical_refs(intent_plan)
    if not panel_refs:
        return score

    violating_refs: list[str] = []
    for placed in placed_parts:
        if placed.ref not in panel_refs:
            continue
        bounds = _physical_or_placed_bounds(placed, fp_bboxes, fp_geometries)
        if not _bounds_inside_outline(bounds, outline):
            violating_refs.append(placed.ref)

    if not violating_refs:
        return score

    warnings = list(score.warnings)
    for ref in violating_refs:
        message = f"{ref}: panel/mechanical body crosses board outline"
        if message not in warnings:
            warnings.append(message)

    penalty = 40.0 + min(10.0 * (len(violating_refs) - 1), 30.0)
    return replace(
        score,
        score=max(0.0, score.score - penalty),
        outline_violation_count=max(
            score.outline_violation_count,
            len(violating_refs),
        ),
        warning_count=len(warnings),
        warnings=warnings,
    )


def _derive_outline_for_edge_anchors(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    *,
    margin_mm: float,
    form_factor: str | None,
    min_area_mm2: float,
    max_min_area_growth: float,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> BoardOutline:
    anchors = _edge_anchor_map(intent_plan, constraints)
    effective_margin_mm = margin_mm
    if anchors:
        effective_margin_mm = min(margin_mm, 1.5)

    outline = _derive_outline_from_placed_bounds(
        placed_parts,
        fp_bboxes,
        margin_mm=effective_margin_mm,
        form_factor=form_factor,
        min_area_mm2=min_area_mm2,
        max_min_area_growth=max_min_area_growth,
        fp_geometries=fp_geometries,
    )
    if form_factor or not placed_parts:
        return outline

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    bounds_by_ref = {
        placed.ref: _placed_bounds(placed, fp_bboxes, fp_geometries)
        for placed in placed_parts
    }
    edge_refs = {
        edge: {ref for ref, anchor in anchors.items() if anchor.edge.lower() == edge}
        for edge in ("top", "bottom", "left", "right")
    }
    if not any(edge_refs.values()):
        return outline

    pin_access_refs = {
        intent.ref
        for intent in (intent_plan.mating_intents if intent_plan else [])
        if intent.kind in {"header", "generic_connector"}
        and intent.mating_side == "pin_access"
    }

    x_min = outline.x_min
    y_min = outline.y_min
    x_max = outline.x_max
    y_max = outline.y_max

    top_refs = [
        ref for ref in edge_refs["top"] if ref in bounds_by_ref and ref in anchors
    ]
    if top_refs:
        desired = min(
            bounds_by_ref[ref][1] - anchors[ref].inset_mm
            for ref in top_refs
        )
        y_min = min(y_min, desired)

    bottom_refs = [
        ref for ref in edge_refs["bottom"] if ref in bounds_by_ref and ref in anchors
    ]
    if bottom_refs:
        desired = max(
            bounds_by_ref[ref][3] + anchors[ref].inset_mm
            for ref in bottom_refs
        )
        y_max = max(y_max, desired)

    left_refs = [
        ref for ref in edge_refs["left"] if ref in bounds_by_ref and ref in anchors
    ]
    if left_refs:
        desired = min(
            bounds_by_ref[ref][0] - anchors[ref].inset_mm
            for ref in left_refs
        )
        x_min = min(x_min, desired)

    right_refs = [
        ref for ref in edge_refs["right"] if ref in bounds_by_ref and ref in anchors
    ]
    if right_refs:
        desired = max(
            bounds_by_ref[ref][2] + anchors[ref].inset_mm
            for ref in right_refs
        )
        x_max = max(x_max, desired)

    def _center_limits(low: float, high: float, center: float) -> tuple[float, float]:
        half_span = max(center - low, high - center)
        return center - half_span, center + half_span

    horizontal_refs = top_refs + bottom_refs
    if len(horizontal_refs) == 1 and horizontal_refs[0] in pin_access_refs:
        bounds = bounds_by_ref[horizontal_refs[0]]
        center = (bounds[0] + bounds[2]) / 2
        x_min, x_max = _center_limits(x_min, x_max, center)

    vertical_refs = left_refs + right_refs
    if vertical_refs:
        centers = [
            (bounds_by_ref[ref][1] + bounds_by_ref[ref][3]) / 2
            for ref in vertical_refs
        ]
        if max(centers) - min(centers) <= 0.2:
            y_min, y_max = _center_limits(
                y_min,
                y_max,
                sum(centers) / len(centers),
            )

    if x_max <= x_min or y_max <= y_min:
        return outline
    return BoardOutline(
        vertices=[
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ],
        corner_radius_mm=getattr(outline, "corner_radius_mm", 0.0),
    )


def _derive_outline_from_placed_bounds(
    placed_parts: list[PlacedPart],
    fp_bboxes: dict[str, tuple[float, float]],
    *,
    margin_mm: float = 3.0,
    form_factor: str | None = None,
    min_area_mm2: float = 0.0,
    max_min_area_growth: float | None = None,
    fp_geometries: dict[str, FootprintGeometry] | None = None,
) -> BoardOutline:
    """Return an auto outline from final transformed placement bounds."""
    if form_factor:
        return derive_outline([], fp_bboxes, form_factor=form_factor)
    if not placed_parts:
        return BoardOutline(50.0, 50.0)

    x_min = float("inf")
    y_min = float("inf")
    x_max = float("-inf")
    y_max = float("-inf")
    for placed in placed_parts:
        bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
        x_min = min(x_min, bounds[0])
        y_min = min(y_min, bounds[1])
        x_max = max(x_max, bounds[2])
        y_max = max(y_max, bounds[3])

    x_min -= margin_mm
    y_min -= margin_mm
    x_max += margin_mm
    y_max += margin_mm

    width = x_max - x_min
    height = y_max - y_min
    area = width * height
    if min_area_mm2 > 0 and area > 0.0 and area < min_area_mm2:
        if max_min_area_growth is not None and max_min_area_growth > 0:
            min_area_mm2 = min(min_area_mm2, area * max_min_area_growth)
        scale = math.sqrt(min_area_mm2 / area)
        cx, cy = (x_min + x_max) / 2, (y_min + y_max) / 2
        width *= scale
        height *= scale
        x_min = cx - width / 2
        x_max = cx + width / 2
        y_min = cy - height / 2
        y_max = cy + height / 2

    return BoardOutline(
        vertices=[
            (x_min, y_min),
            (x_max, y_min),
            (x_max, y_max),
            (x_min, y_max),
        ]
    )


def _final_outline_constraints(
    constraints: LayoutConstraints | None,
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    *,
    lock_edge_anchors: bool = True,
) -> LayoutConstraints:
    final = copy_constraints(constraints or LayoutConstraints())
    final.outline = outline
    if intent_plan is None:
        return final

    fixed_refs = {fixed.ref for fixed in final.fixed or []}
    for fixed in intent_plan.fixed_positions:
        if fixed.ref not in fixed_refs:
            final.fixed.append(fixed)
            fixed_refs.add(fixed.ref)

    if lock_edge_anchors:
        anchors_by_ref = {anchor.ref: anchor for anchor in final.edge_anchors or []}
        for anchor in intent_plan.edge_anchors:
            existing = anchors_by_ref.get(anchor.ref)
            if existing is None:
                final.edge_anchors.append(anchor)
                anchors_by_ref[anchor.ref] = anchor
            elif existing.rot_deg is None and anchor.rot_deg is not None:
                existing.rot_deg = anchor.rot_deg

    face_refs = {face.ref for face in final.face_edges or []}
    for face in intent_plan.face_edges:
        if face.ref not in face_refs:
            final.face_edges.append(face)
            face_refs.add(face.ref)

    return final


def _mounting_hole_corner_inset(
    placed: PlacedPart,
    outline: BoardOutline,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> float:
    bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
    hole_radius = max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / 2.0
    inset = hole_radius + 0.5
    rounded_corner_radius = getattr(outline, "corner_radius_mm", 0.0) or 0.0
    if rounded_corner_radius > 0:
        inset = max(inset, float(rounded_corner_radius))
    return min(inset, outline.width_mm / 2, outline.height_mm / 2)


def _snap_mounting_holes_to_outline_corners(
    placed_parts: list[PlacedPart],
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[list[PlacedPart], list[str]]:
    if outline is None or intent_plan is None:
        return placed_parts, []

    mounting_refs = set(intent_plan.refs_with_kind("mounting_hole"))
    if not mounting_refs:
        return placed_parts, []

    placed_mounting_parts = [
        placed for placed in placed_parts if placed.ref in mounting_refs
    ]
    if len(placed_mounting_parts) < 2:
        return placed_parts, []

    center_x = (outline.x_min + outline.x_max) / 2
    center_y = (outline.y_min + outline.y_max) / 2
    hole_inset = max(
        _mounting_hole_corner_inset(placed, outline, fp_bboxes, fp_geometries)
        for placed in placed_mounting_parts
    )
    moved: list[str] = []
    snapped: list[PlacedPart] = []

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    if len(placed_mounting_parts) == 2:
        h0, h1 = placed_mounting_parts
        edge = None
        corner_threshold = max(4.0, hole_inset + 0.75)
        if abs(h0.y_mm - h1.y_mm) <= 2.0:
            average_y = (h0.y_mm + h1.y_mm) / 2
            if min(average_y - outline.y_min, outline.y_max - average_y) <= corner_threshold:
                edge = "top" if average_y <= center_y else "bottom"
                ordered_refs = [
                    ref
                    for ref in sorted(
                        (h0.ref, h1.ref),
                        key=lambda ref: (placed_by_ref[ref].x_mm, _natural_ref_key(ref)),
                    )
                ]
                if edge == "top":
                    positions = [
                        (outline.x_min + hole_inset, outline.y_min + hole_inset),
                        (outline.x_max - hole_inset, outline.y_min + hole_inset),
                    ]
                else:
                    positions = [
                        (outline.x_min + hole_inset, outline.y_max - hole_inset),
                        (outline.x_max - hole_inset, outline.y_max - hole_inset),
                    ]
        elif abs(h0.x_mm - h1.x_mm) <= 2.0:
            average_x = (h0.x_mm + h1.x_mm) / 2
            if min(average_x - outline.x_min, outline.x_max - average_x) <= corner_threshold:
                edge = "left" if average_x <= center_x else "right"
                ordered_refs = [
                    ref
                    for ref in sorted(
                        (h0.ref, h1.ref),
                        key=lambda ref: (placed_by_ref[ref].y_mm, _natural_ref_key(ref)),
                    )
                ]
                if edge == "left":
                    positions = [
                        (outline.x_min + hole_inset, outline.y_min + hole_inset),
                        (outline.x_min + hole_inset, outline.y_max - hole_inset),
                    ]
                else:
                    positions = [
                        (outline.x_max - hole_inset, outline.y_min + hole_inset),
                        (outline.x_max - hole_inset, outline.y_max - hole_inset),
                    ]
        if edge is None:
            return placed_parts, []

        positions_by_ref = {
            ref: position for ref, position in zip(ordered_refs, positions)
        }
        for placed in placed_parts:
            if placed.ref not in mounting_refs:
                snapped.append(placed)
                continue
            x_mm, y_mm = positions_by_ref.get(
                placed.ref,
                (placed.x_mm, placed.y_mm),
            )
            if abs(x_mm - placed.x_mm) > 1e-6 or abs(y_mm - placed.y_mm) > 1e-6:
                moved.append(placed.ref)
                snapped.append(
                    PlacedPart(
                        ref=placed.ref,
                        x_mm=x_mm,
                        y_mm=y_mm,
                        rot_deg=placed.rot_deg,
                        footprint=placed.footprint,
                        side=getattr(placed, "side", "front"),
                    )
                )
            else:
                snapped.append(placed)
        return snapped, moved

    # Four-hole corner-mount patterns always use the outer corners.
    corner_mounting_parts = placed_mounting_parts[:4]
    corner_mounting_refs = {part.ref for part in corner_mounting_parts}
    for placed in placed_parts:
        if placed.ref not in mounting_refs:
            snapped.append(placed)
            continue

        if placed.ref not in corner_mounting_refs:
            snapped.append(placed)
            continue

        if placed.x_mm <= center_x:
            x_mm = outline.x_min + hole_inset
        else:
            x_mm = outline.x_max - hole_inset

        if placed.y_mm <= center_y:
            y_mm = outline.y_min + hole_inset
        else:
            y_mm = outline.y_max - hole_inset

        if abs(x_mm - placed.x_mm) > 1e-6 or abs(y_mm - placed.y_mm) > 1e-6:
            moved.append(placed.ref)
            snapped.append(
                PlacedPart(
                    ref=placed.ref,
                    x_mm=x_mm,
                    y_mm=y_mm,
                    rot_deg=placed.rot_deg,
                    footprint=placed.footprint,
                    side=getattr(placed, "side", "front"),
                )
            )
        else:
            snapped.append(placed)

    return snapped, moved


def _separate_same_edge_anchors(
    placed_parts: list[PlacedPart],
    outline: BoardOutline,
    anchors: dict[str, EdgeAnchor],
    fixed_refs: set[str],
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    *,
    keepouts: list | None = None,
    clearance_mm: float = 0.75,
) -> tuple[list[PlacedPart], list[str]]:
    """Keep connectors on the same board edge from collapsing to one fallback."""

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    replacements: dict[str, PlacedPart] = {}
    moved: list[str] = []

    for edge in ("top", "bottom", "left", "right"):
        refs = [
            ref
            for ref, anchor in anchors.items()
            if anchor.edge.lower() == edge
            and ref in placed_by_ref
            and ref not in fixed_refs
        ]
        if len(refs) < 2:
            continue

        horizontal = edge in {"top", "bottom"}
        axis_min = outline.x_min if horizontal else outline.y_min
        axis_max = outline.x_max if horizontal else outline.y_max
        items = []
        for ref in refs:
            placed = replacements.get(ref, placed_by_ref[ref])
            bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
            span = (bounds[2] - bounds[0]) if horizontal else (bounds[3] - bounds[1])
            half_span = span / 2
            current = (
                (bounds[0] + bounds[2]) / 2
                if horizontal
                else (bounds[1] + bounds[3]) / 2
            )
            desired = anchors[ref].offset_mm if anchors[ref].offset_mm is not None else current
            items.append(
                {
                    "ref": ref,
                    "placed": placed,
                    "bounds": bounds,
                    "half": half_span,
                    "current": current,
                    "desired": float(desired),
                    "min": axis_min + half_span,
                    "max": axis_max - half_span,
                }
            )

        items.sort(key=lambda item: (item["desired"], item["current"], item["ref"]))
        total_span = sum(item["half"] * 2 for item in items) + clearance_mm * (len(items) - 1)
        available = axis_max - axis_min
        if total_span > available + 1e-6:
            if len(items) == 1:
                positions = [(axis_min + axis_max) / 2]
            else:
                positions = [
                    axis_min + available * index / (len(items) - 1)
                    for index in range(len(items))
                ]
        else:
            positions = [
                min(max(item["desired"], item["min"]), item["max"])
                for item in items
            ]
            for index in range(1, len(items)):
                prev = items[index - 1]
                item = items[index]
                min_position = positions[index - 1] + prev["half"] + item["half"] + clearance_mm
                positions[index] = max(positions[index], min_position)
            if positions[-1] > items[-1]["max"]:
                overflow = positions[-1] - items[-1]["max"]
                positions = [position - overflow for position in positions]
            for index in range(len(items) - 2, -1, -1):
                item = items[index]
                nxt = items[index + 1]
                max_position = positions[index + 1] - item["half"] - nxt["half"] - clearance_mm
                positions[index] = min(positions[index], max_position)
            if positions[0] < items[0]["min"]:
                underflow = items[0]["min"] - positions[0]
                positions = [position + underflow for position in positions]
            positions = _slide_edge_anchor_positions_away_from_keepouts(
                items,
                positions,
                edge,
                axis_min,
                axis_max,
                keepouts or [],
                clearance_mm,
            )

        for item, position in zip(items, positions):
            placed = item["placed"]
            delta = position - item["current"]
            if abs(delta) <= 1e-6:
                continue
            moved.append(item["ref"])
            replacements[item["ref"]] = PlacedPart(
                ref=placed.ref,
                x_mm=placed.x_mm + (delta if horizontal else 0.0),
                y_mm=placed.y_mm + (0.0 if horizontal else delta),
                rot_deg=placed.rot_deg,
                footprint=placed.footprint,
                side=getattr(placed, "side", "front"),
            )

    if not replacements:
        return placed_parts, []
    return [replacements.get(placed.ref, placed) for placed in placed_parts], moved


def _slide_edge_anchor_positions_away_from_keepouts(
    items: list[dict],
    positions: list[float],
    edge: str,
    axis_min: float,
    axis_max: float,
    keepouts: list,
    clearance_mm: float,
) -> list[float]:
    """Nudge same-edge connector positions out of keepout intervals when possible."""

    if not keepouts:
        return positions

    horizontal = edge in {"top", "bottom"}

    def forbidden_intervals(item: dict) -> list[tuple[float, float]]:
        bounds = item["bounds"]
        intervals: list[tuple[float, float]] = []
        fixed_min, fixed_max = (bounds[1], bounds[3]) if horizontal else (bounds[0], bounds[2])
        for keepout in keepouts:
            ko_fixed_min, ko_fixed_max = (
                (keepout.y_min, keepout.y_max)
                if horizontal
                else (keepout.x_min, keepout.x_max)
            )
            if fixed_max <= ko_fixed_min or fixed_min >= ko_fixed_max:
                continue
            ko_axis_min, ko_axis_max = (
                (keepout.x_min, keepout.x_max)
                if horizontal
                else (keepout.y_min, keepout.y_max)
            )
            intervals.append((
                ko_axis_min - item["half"] - clearance_mm,
                ko_axis_max + item["half"] + clearance_mm,
            ))
        return intervals

    def allowed(item: dict, position: float) -> bool:
        if position < item["min"] - 1e-6 or position > item["max"] + 1e-6:
            return False
        return all(
            position <= lo + 1e-6 or position >= hi - 1e-6
            for lo, hi in forbidden_intervals(item)
        )

    adjusted = list(positions)
    for _ in range(3):
        changed = False
        for index, item in enumerate(items):
            if allowed(item, adjusted[index]):
                continue
            candidates = [item["min"], item["max"], item["desired"], item["current"]]
            for lo, hi in forbidden_intervals(item):
                candidates.extend((lo, hi))
            if index > 0:
                prev = items[index - 1]
                candidates.append(adjusted[index - 1] + prev["half"] + item["half"] + clearance_mm)
            if index < len(items) - 1:
                nxt = items[index + 1]
                candidates.append(adjusted[index + 1] - nxt["half"] - item["half"] - clearance_mm)
            valid = [
                min(max(float(candidate), item["min"]), item["max"])
                for candidate in candidates
            ]
            valid = [candidate for candidate in valid if allowed(item, candidate)]
            if not valid:
                continue
            best = min(valid, key=lambda candidate: abs(candidate - item["desired"]))
            if abs(best - adjusted[index]) > 1e-6:
                adjusted[index] = best
                changed = True

        for index in range(1, len(items)):
            prev = items[index - 1]
            item = items[index]
            min_position = adjusted[index - 1] + prev["half"] + item["half"] + clearance_mm
            if adjusted[index] < min_position:
                adjusted[index] = min(min_position, item["max"])
                changed = True
        for index in range(len(items) - 2, -1, -1):
            item = items[index]
            nxt = items[index + 1]
            max_position = adjusted[index + 1] - item["half"] - nxt["half"] - clearance_mm
            if adjusted[index] > max_position:
                adjusted[index] = max(max_position, item["min"])
                changed = True
        if not changed:
            break

    return [
        min(max(position, axis_min + item["half"]), axis_max - item["half"])
        for item, position in zip(items, adjusted)
    ]


def _snap_edge_anchors_to_outline(
    placed_parts: list[PlacedPart],
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[list[PlacedPart], list[str]]:
    if outline is None:
        return placed_parts, []

    anchors = _edge_anchor_map(intent_plan, constraints)
    if not anchors:
        return placed_parts, []

    fixed_refs = {
        fixed.ref for fixed in (constraints.fixed if constraints else []) or []
    }
    explicit_edge_offset_refs = {
        anchor.ref
        for anchor in ((constraints.edge_anchors if constraints else []) or [])
        if anchor.offset_mm is not None
    }
    pin_access_refs = {
        intent.ref
        for intent in (intent_plan.mating_intents if intent_plan else [])
        if intent.kind in {"header", "generic_connector"}
        and intent.mating_side == "pin_access"
    }
    edge_refs = {
        edge: [ref for ref, anchor in anchors.items() if anchor.edge.lower() == edge]
        for edge in ("top", "bottom", "left", "right")
    }
    vertical_pair_refs = edge_refs["left"] + edge_refs["right"]
    align_vertical_pair = (
        len(edge_refs["left"]) == 1
        and len(edge_refs["right"]) == 1
        and set(vertical_pair_refs).issubset(pin_access_refs)
    )

    moved: list[str] = []
    snapped: list[PlacedPart] = []
    mounting_keepouts = _mounting_hole_keepouts(
        placed_parts,
        intent_plan,
        fp_bboxes,
        fp_geometries,
    )
    for placed in placed_parts:
        anchor = anchors.get(placed.ref)
        if anchor is None or placed.ref in fixed_refs:
            snapped.append(placed)
            continue

        width, height = fp_bboxes.get(placed.footprint, (2.0, 2.0))
        geometry = (fp_geometries or {}).get(placed.footprint)
        edge = anchor.edge.lower()
        offset = anchor.offset_mm
        if (
            placed.ref not in explicit_edge_offset_refs
            and align_vertical_pair
            and edge in {"left", "right"}
        ):
            offset = (outline.y_min + outline.y_max) / 2
        elif (
            placed.ref not in explicit_edge_offset_refs
            and placed.ref in pin_access_refs
            and edge in {"top", "bottom"}
        ):
            refs = edge_refs["top"] + edge_refs["bottom"]
            if len(refs) == 1:
                offset = (outline.x_min + outline.x_max) / 2

        final_anchor = EdgeAnchor(
            ref=anchor.ref,
            edge=anchor.edge,
            offset_mm=offset,
            inset_mm=anchor.inset_mm,
            rot_deg=anchor.rot_deg,
        )
        x_mm, y_mm, rot_deg, *_ = _edge_anchor_position_avoiding_keepouts(
            final_anchor,
            width,
            height,
            outline,
            geometry=geometry,
            ref=placed.ref,
            footprint=placed.footprint,
            intent_plan=intent_plan,
            keepouts=[
                *((constraints.keepouts if constraints else []) or []),
                *mounting_keepouts,
            ],
        )
        if placed.ref in pin_access_refs:
            x_mm, y_mm, rot_deg, *_ = _prefer_parallel_edge_anchor_position(
                final_anchor,
                width,
                height,
                outline,
                geometry=geometry,
                ref=placed.ref,
                footprint=placed.footprint,
                intent_plan=intent_plan,
                keepouts=[
                    *((constraints.keepouts if constraints else []) or []),
                    *mounting_keepouts,
                ],
                current=(x_mm, y_mm, rot_deg),
            )
        candidate_part = PlacedPart(
            ref=placed.ref,
            x_mm=x_mm,
            y_mm=y_mm,
            rot_deg=rot_deg,
            footprint=placed.footprint,
            side=getattr(placed, "side", "front"),
        )
        bounds = _placed_bounds(candidate_part, fp_bboxes, fp_geometries)
        dx, dy = _clamp_delta_to_outline(bounds, 0.0, 0.0, outline, 0.0)
        if _connector_mating_face_for_ref(placed.ref, placed.footprint, intent_plan):
            if edge in {"top", "bottom"}:
                dy = 0.0
            elif edge in {"left", "right"}:
                dx = 0.0
        if abs(dx) > 1e-6 or abs(dy) > 1e-6:
            x_mm += dx
            y_mm += dy
        if (
            abs(x_mm - placed.x_mm) > 1e-6
            or abs(y_mm - placed.y_mm) > 1e-6
            or abs(rot_deg - placed.rot_deg) > 1e-6
        ):
            moved.append(placed.ref)
            snapped.append(
                PlacedPart(
                    ref=placed.ref,
                    x_mm=x_mm,
                    y_mm=y_mm,
                    rot_deg=rot_deg,
                    footprint=placed.footprint,
                    side=getattr(placed, "side", "front"),
                )
            )
        else:
            snapped.append(placed)

    separated, separated_refs = _separate_same_edge_anchors(
        snapped,
        outline,
        anchors,
        fixed_refs,
        fp_bboxes,
        fp_geometries,
        keepouts=[
            *((constraints.keepouts if constraints else []) or []),
            *mounting_keepouts,
        ],
    )
    for ref in separated_refs:
        if ref not in moved:
            moved.append(ref)
    return separated, moved


def _mounting_hole_keepouts(
    placed_parts: list[PlacedPart],
    intent_plan: PlacementIntentPlan | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    *,
    clearance_mm: float = 2.0,
) -> list[KeepOut]:
    if intent_plan is None:
        return []
    mounting_refs = set(intent_plan.refs_with_kind("mounting_hole"))
    if not mounting_refs:
        return []

    keepouts: list[KeepOut] = []
    for placed in placed_parts:
        if placed.ref not in mounting_refs:
            continue
        bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
        keepouts.append(
            KeepOut(
                bounds[0] - clearance_mm,
                bounds[1] - clearance_mm,
                bounds[2] + clearance_mm,
                bounds[3] + clearance_mm,
                allowed_refs=[placed.ref],
            )
        )
    return keepouts


def _cutout_keepouts(cutouts: list[BoardCutout] | None) -> list[KeepOut]:
    return [
        cutout.to_keepout() if hasattr(cutout, "to_keepout") else KeepOut(
            cutout.x_min,
            cutout.y_min,
            cutout.x_max,
            cutout.y_max,
        )
        for cutout in cutouts or []
    ]


def _effective_keepouts(
    constraints: LayoutConstraints | None,
    placed_parts: list[PlacedPart],
    intent_plan: PlacementIntentPlan | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    outline: BoardOutline | None = None,
) -> list[KeepOut]:
    explicit_keepouts = [
        *list((constraints.keepouts if constraints else []) or []),
        *_cutout_keepouts((constraints.cutouts if constraints else []) or []),
    ]
    if outline is not None and intent_plan is not None:
        mounting_refs = set(intent_plan.refs_with_kind("mounting_hole"))
        if mounting_refs:
            allowed_mounting_refs = sorted(mounting_refs)
            adjusted: list[KeepOut] = []
            for keepout in explicit_keepouts:
                if _is_outline_edge_band(keepout, outline):
                    allowed_refs = sorted(
                        set(getattr(keepout, "allowed_refs", []) or [])
                        | set(allowed_mounting_refs)
                    )
                    adjusted.append(
                        KeepOut(
                            keepout.x_min,
                            keepout.y_min,
                            keepout.x_max,
                            keepout.y_max,
                            allowed_refs=allowed_refs,
                        )
                    )
                else:
                    adjusted.append(keepout)
            explicit_keepouts = adjusted
    return [
        *explicit_keepouts,
        *_mounting_hole_keepouts(
            placed_parts,
            intent_plan,
            fp_bboxes,
            fp_geometries,
        ),
    ]


def _constraints_with_effective_keepouts(
    constraints: LayoutConstraints | None,
    placed_parts: list[PlacedPart],
    intent_plan: PlacementIntentPlan | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    outline: BoardOutline | None = None,
) -> LayoutConstraints:
    effective = copy_constraints(constraints or LayoutConstraints())
    effective.keepouts = _effective_keepouts(
        constraints,
        placed_parts,
        intent_plan,
        fp_bboxes,
        fp_geometries,
        outline,
    )
    return effective


def _is_outline_edge_band(keepout: KeepOut, outline: BoardOutline) -> bool:
    tol = 1e-6
    spans_width = (
        keepout.x_min <= outline.x_min + tol
        and keepout.x_max >= outline.x_max - tol
    )
    spans_height = (
        keepout.y_min <= outline.y_min + tol
        and keepout.y_max >= outline.y_max - tol
    )
    touches_horizontal_edge = (
        abs(keepout.y_min - outline.y_min) <= tol
        or abs(keepout.y_max - outline.y_max) <= tol
    )
    touches_vertical_edge = (
        abs(keepout.x_min - outline.x_min) <= tol
        or abs(keepout.x_max - outline.x_max) <= tol
    )
    return (spans_width and touches_horizontal_edge) or (
        spans_height and touches_vertical_edge
    )


def _bounds_touch_keepout(bounds: tuple[float, float, float, float], keepout) -> bool:
    return not (
        bounds[2] <= keepout.x_min
        or bounds[0] >= keepout.x_max
        or bounds[3] <= keepout.y_min
        or bounds[1] >= keepout.y_max
    )


def _bounds_keepout_overlap_area(
    bounds: tuple[float, float, float, float],
    keepout,
) -> float:
    x_overlap = min(bounds[2], keepout.x_max) - max(bounds[0], keepout.x_min)
    y_overlap = min(bounds[3], keepout.y_max) - max(bounds[1], keepout.y_min)
    if x_overlap <= 0.0 or y_overlap <= 0.0:
        return 0.0
    return x_overlap * y_overlap


def _edge_anchor_position_avoiding_keepouts(
    anchor: EdgeAnchor,
    width: float,
    height: float,
    outline: BoardOutline,
    *,
    geometry: FootprintGeometry | None,
    ref: str,
    footprint: str,
    intent_plan: PlacementIntentPlan | None,
    keepouts: list | None,
    clearance_mm: float = 0.5,
) -> tuple[float, float, float, float, float, float, float]:
    original = _edge_anchor_origin_position_for_mating_face(
        anchor,
        width,
        height,
        outline,
        geometry=geometry,
        ref=ref,
        footprint=footprint,
        intent_plan=intent_plan,
    )
    if not keepouts:
        return original

    def _bounds(candidate):
        _, _, _, center_x, center_y, ew, eh = candidate
        return (
            center_x - ew / 2,
            center_y - eh / 2,
            center_x + ew / 2,
            center_y + eh / 2,
        )

    if not any(_bounds_touch_keepout(_bounds(original), ko) for ko in keepouts):
        return original

    edge = anchor.edge.lower()
    offsets: list[float] = []
    if anchor.offset_mm is not None:
        offsets.append(anchor.offset_mm)
    _, _, _, _, _, original_w, original_h = original
    if edge in {"left", "right"}:
        for ko in keepouts:
            offsets.extend((
                ko.y_min - original_h / 2 - clearance_mm,
                ko.y_max + original_h / 2 + clearance_mm,
            ))
        offsets.append((outline.y_min + outline.y_max) / 2)
    elif edge in {"top", "bottom"}:
        for ko in keepouts:
            offsets.extend((
                ko.x_min - original_w / 2 - clearance_mm,
                ko.x_max + original_w / 2 + clearance_mm,
            ))
        offsets.append((outline.x_min + outline.x_max) / 2)

    original_bounds = _bounds(original)
    best = original
    best_key = (
        sum(_bounds_keepout_overlap_area(original_bounds, ko) for ko in keepouts),
        sum(_bounds_touch_keepout(original_bounds, ko) for ko in keepouts),
        0.0,
    )
    seen: set[float] = set()
    for offset in offsets:
        rounded = round(float(offset), 6)
        if rounded in seen:
            continue
        seen.add(rounded)
        candidate_anchor = EdgeAnchor(
            ref=anchor.ref,
            edge=anchor.edge,
            offset_mm=float(offset),
            inset_mm=anchor.inset_mm,
            rot_deg=anchor.rot_deg,
        )
        candidate = _edge_anchor_origin_position_for_mating_face(
            candidate_anchor,
            width,
            height,
            outline,
            geometry=geometry,
            ref=ref,
            footprint=footprint,
            intent_plan=intent_plan,
        )
        candidate_bounds = _bounds(candidate)
        hits = sum(_bounds_touch_keepout(candidate_bounds, ko) for ko in keepouts)
        overlap_area = sum(
            _bounds_keepout_overlap_area(candidate_bounds, ko)
            for ko in keepouts
        )
        candidate_key = (overlap_area, hits, abs(float(offset) - (anchor.offset_mm or 0.0)))
        if candidate_key < best_key:
            best = candidate
            best_key = candidate_key
            if overlap_area <= 1e-9 and hits == 0:
                break
    return best


def _prefer_parallel_edge_anchor_position(
    anchor: EdgeAnchor,
    width: float,
    height: float,
    outline: BoardOutline,
    *,
    geometry: FootprintGeometry | None,
    ref: str,
    footprint: str,
    intent_plan: PlacementIntentPlan | None,
    keepouts: list | None,
    current: tuple[float, float, float],
) -> tuple[float, float, float, float, float, float, float]:
    """Repair contradictory edge-anchor rotations for pin-access connectors."""
    face = _connector_mating_face_for_ref(ref, footprint, intent_plan)

    def _candidate_for(rotation: float):
        candidate_anchor = EdgeAnchor(
            ref=anchor.ref,
            edge=anchor.edge,
            offset_mm=anchor.offset_mm,
            inset_mm=anchor.inset_mm,
            rot_deg=rotation,
        )
        return _edge_anchor_position_avoiding_keepouts(
            candidate_anchor,
            width,
            height,
            outline,
            geometry=geometry,
            ref=ref,
            footprint=footprint,
            intent_plan=intent_plan,
            keepouts=keepouts,
        )

    def _candidate_bounds(candidate) -> tuple[float, float, float, float]:
        x_mm, y_mm, rot_deg, *_ = candidate
        return _placed_bounds(
            PlacedPart(
                ref=ref,
                x_mm=x_mm,
                y_mm=y_mm,
                rot_deg=rot_deg,
                footprint=footprint,
            ),
            {footprint: (width, height)},
            {footprint: geometry} if geometry is not None else None,
        )

    def _candidate_part(candidate) -> PlacedPart:
        x_mm, y_mm, rot_deg, *_ = candidate
        return PlacedPart(
            ref=ref,
            x_mm=x_mm,
            y_mm=y_mm,
            rot_deg=rot_deg,
            footprint=footprint,
        )

    def _candidate_edge_distance(candidate, bounds) -> float:
        distance = _edge_face_distance(
            edge,
            _candidate_part(candidate),
            {footprint: (width, height)},
            outline,
            anchor.inset_mm,
            intent_plan,
            {footprint: geometry} if geometry is not None else None,
        )
        if distance is None:
            distance = _edge_distance(edge, bounds, outline, anchor.inset_mm)
        return distance or 0.0

    edge = anchor.edge.lower()
    expected_rotation = _expected_edge_rotation(edge, ref, face, intent_plan)
    current_full = (*current, current[0], current[1], width, height)
    current_bounds = _candidate_bounds(current_full)
    if _edge_parallel(edge, current_bounds) and _rotation_faces_edge(
        current[2],
        expected_rotation,
    ):
        return current_full

    rotations: list[float] = []
    for rotation in (
        expected_rotation,
        current[2] + 90.0,
        current[2] - 90.0,
        0.0,
        90.0,
        180.0,
        270.0,
    ):
        if rotation is None:
            continue
        normalized = float(rotation % 360)
        if normalized not in rotations:
            rotations.append(normalized)

    current_distance = _candidate_edge_distance(current_full, current_bounds)
    best = current_full
    best_key = (
        0 if _edge_parallel(edge, current_bounds) else 1,
        0 if _rotation_faces_edge(current[2], expected_rotation) else 1,
        current_distance,
        0.0,
    )
    for rotation in rotations:
        candidate = _candidate_for(rotation)
        bounds = _candidate_bounds(candidate)
        parallel = _edge_parallel(edge, bounds)
        faces_outward = _rotation_faces_edge(rotation, expected_rotation)
        distance = _candidate_edge_distance(candidate, bounds)
        rotation_delta = _rotation_delta_deg(rotation, current[2])
        key = (
            0 if parallel else 1,
            0 if faces_outward else 1,
            distance,
            rotation_delta,
        )
        if key < best_key:
            best = candidate
            best_key = key
            if parallel and faces_outward and distance <= 1e-6:
                break

    return best


def _apply_assembly_sides(
    placed_parts: list[PlacedPart],
    intent_plan: PlacementIntentPlan | None,
) -> list[PlacedPart]:
    sides = getattr(intent_plan, "assembly_sides", None) or {}
    if not sides:
        return placed_parts

    result: list[PlacedPart] = []
    for placed in placed_parts:
        side = sides.get(placed.ref, getattr(placed, "side", "front"))
        side = str(side or "front").lower()
        if side not in {"front", "back", "mechanical"}:
            side = "front"
        if side == getattr(placed, "side", "front"):
            result.append(placed)
            continue
        result.append(
            PlacedPart(
                ref=placed.ref,
                x_mm=placed.x_mm,
                y_mm=placed.y_mm,
                rot_deg=placed.rot_deg,
                footprint=placed.footprint,
                side=side,
            )
        )
    return result


def _bounds_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    clearance_mm: float,
) -> bool:
    return not (
        a[2] + clearance_mm <= b[0]
        or b[2] + clearance_mm <= a[0]
        or a[3] + clearance_mm <= b[1]
        or b[3] + clearance_mm <= a[1]
    )


def _physically_blocks(
    a: PlacedPart,
    b: PlacedPart,
    a_bounds: tuple[float, float, float, float],
    b_bounds: tuple[float, float, float, float],
    clearance_mm: float,
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> bool:
    if _same_physical_side(a, b):
        return _bounds_overlap(a_bounds, b_bounds, clearance_mm)
    a_geometry = (fp_geometries or {}).get(a.footprint)
    b_geometry = (fp_geometries or {}).get(b.footprint)
    if a_geometry is None or b_geometry is None:
        return _bounds_overlap(a_bounds, b_bounds, clearance_mm)
    return _through_board_pads_collide(
        a,
        a_geometry,
        b,
        b_geometry,
        clearance_mm,
    )


def _translated_bounds(
    bounds: tuple[float, float, float, float],
    dx: float,
    dy: float,
) -> tuple[float, float, float, float]:
    return bounds[0] + dx, bounds[1] + dy, bounds[2] + dx, bounds[3] + dy


def _lock_current_positions(
    constraints: LayoutConstraints,
    placed_parts: list[PlacedPart],
    refs: list[str],
) -> None:
    if not refs:
        return
    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    fixed_refs = {fixed.ref for fixed in constraints.fixed or []}
    for ref in refs:
        placed = placed_by_ref.get(ref)
        if placed is None or ref in fixed_refs:
            continue
        constraints.fixed.append(
            FixedPosition(
                ref=ref,
                x_mm=placed.x_mm,
                y_mm=placed.y_mm,
                rot_deg=placed.rot_deg,
            )
        )
        fixed_refs.add(ref)


def _clamp_delta_to_outline(
    bounds: tuple[float, float, float, float],
    dx: float,
    dy: float,
    outline: BoardOutline,
    clearance_mm: float,
) -> tuple[float, float]:
    moved = _translated_bounds(bounds, dx, dy)
    if moved[0] < outline.x_min + clearance_mm:
        dx += outline.x_min + clearance_mm - moved[0]
    if moved[2] > outline.x_max - clearance_mm:
        dx -= moved[2] - (outline.x_max - clearance_mm)
    moved = _translated_bounds(bounds, dx, dy)
    if moved[1] < outline.y_min + clearance_mm:
        dy += outline.y_min + clearance_mm - moved[1]
    if moved[3] > outline.y_max - clearance_mm:
        dy -= moved[3] - (outline.y_max - clearance_mm)
    return dx, dy


def _legalize_edge_anchor_neighbors(
    placed_parts: list[PlacedPart],
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    clearance_mm: float,
) -> tuple[list[PlacedPart], list[str]]:
    if outline is None:
        return placed_parts, []

    anchors = _edge_anchor_map(intent_plan, constraints)
    if not anchors:
        return placed_parts, []

    mounting_refs = set(intent_plan.refs_with_kind("mounting_hole")) if intent_plan else set()
    anchor_refs = set(anchors)
    explicit_floorplan_refs = _constraint_floorplan_refs(constraints)
    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    moved_refs: set[str] = set()

    for _ in range(2):
        changed = False
        bounds_by_ref = {
            ref: _placed_bounds(placed, fp_bboxes, fp_geometries)
            for ref, placed in placed_by_ref.items()
        }
        for anchor_ref, anchor in anchors.items():
            anchor_part = placed_by_ref.get(anchor_ref)
            anchor_bounds = bounds_by_ref.get(anchor_ref)
            if anchor_part is None or anchor_bounds is None:
                continue
            edge = anchor.edge.lower()
            for ref, placed in list(placed_by_ref.items()):
                if (
                    ref in anchor_refs
                    or ref in mounting_refs
                    or ref in explicit_floorplan_refs
                ):
                    continue
                bounds = bounds_by_ref[ref]
                if not _physically_blocks(
                    anchor_part,
                    placed,
                    anchor_bounds,
                    bounds,
                    clearance_mm,
                    fp_geometries,
                ):
                    continue
                dx = dy = 0.0
                if edge == "left":
                    dx = anchor_bounds[2] + clearance_mm - bounds[0]
                elif edge == "right":
                    dx = anchor_bounds[0] - clearance_mm - bounds[2]
                elif edge == "top":
                    dy = anchor_bounds[3] + clearance_mm - bounds[1]
                elif edge == "bottom":
                    dy = anchor_bounds[1] - clearance_mm - bounds[3]
                else:
                    continue
                dx, dy = _clamp_delta_to_outline(bounds, dx, dy, outline, clearance_mm)
                if abs(dx) <= 1e-6 and abs(dy) <= 1e-6:
                    continue
                placed_by_ref[ref] = PlacedPart(
                    ref=placed.ref,
                    x_mm=placed.x_mm + dx,
                    y_mm=placed.y_mm + dy,
                    rot_deg=placed.rot_deg,
                    footprint=placed.footprint,
                    side=getattr(placed, "side", "front"),
                )
                moved_refs.add(ref)
                changed = True
        if not changed:
            break

    if not moved_refs:
        return placed_parts, []
    return [placed_by_ref[placed.ref] for placed in placed_parts], sorted(moved_refs)


def _legalize_small_parts_from_outline(
    placed_parts: list[PlacedPart],
    circuit,
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
    clearance_mm: float,
) -> tuple[list[PlacedPart], list[str]]:
    """Keep movable parts clear of board edges and mounting-hole halos."""
    if outline is None or circuit is None:
        return placed_parts, []

    roles = classify_parts(circuit)
    small_roles = {"decoupling_cap", "signal_passive", "crystal", "diode", "inductor"}
    anchors = _edge_anchor_map(intent_plan, constraints)
    protected_refs = set(anchors)
    protected_refs.update(
        intent_plan.refs_with_kind("mounting_hole") if intent_plan else []
    )
    explicit_floorplan_refs = _constraint_floorplan_refs(constraints)

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    moved_refs: set[str] = set()
    interior_clearance = max(clearance_mm, 1.5)
    mounting_halo_clearance = max(clearance_mm, 2.0)

    def _bounds_for(ref: str) -> tuple[float, float, float, float]:
        return _placed_bounds(placed_by_ref[ref], fp_bboxes, fp_geometries)

    mounting_halos: list[tuple[str, tuple[float, float, float, float]]] = []
    for ref in intent_plan.refs_with_kind("mounting_hole") if intent_plan else []:
        placed = placed_by_ref.get(ref)
        if placed is None:
            continue
        bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
        mounting_halos.append((
            ref,
            (
                bounds[0] - mounting_halo_clearance,
                bounds[1] - mounting_halo_clearance,
                bounds[2] + mounting_halo_clearance,
                bounds[3] + mounting_halo_clearance,
            ),
        ))

    def _overlaps_others(
        ref: str,
        bounds: tuple[float, float, float, float],
    ) -> bool:
        placed = placed_by_ref[ref]
        for other_ref in placed_by_ref:
            if other_ref == ref:
                continue
            other = placed_by_ref[other_ref]
            if _physically_blocks(
                placed,
                other,
                bounds,
                _bounds_for(other_ref),
                clearance_mm,
                fp_geometries,
            ):
                return True
        return False

    def _overlapping_mounting_halos(
        bounds: tuple[float, float, float, float],
    ) -> list[tuple[str, tuple[float, float, float, float]]]:
        return [
            (hole_ref, halo)
            for hole_ref, halo in mounting_halos
            if _bounds_overlap(bounds, halo, 0.0)
        ]

    def _mounting_hole_escape_delta(
        bounds: tuple[float, float, float, float],
    ) -> tuple[float, float]:
        dx = dy = 0.0
        for _hole_ref, halo in _overlapping_mounting_halos(bounds):
            options = [
                (halo[0] - bounds[2], 0.0),
                (halo[2] - bounds[0], 0.0),
                (0.0, halo[1] - bounds[3]),
                (0.0, halo[3] - bounds[1]),
            ]
            move_x, move_y = min(
                options,
                key=lambda pair: abs(pair[0]) + abs(pair[1]),
            )
            dx += move_x
            dy += move_y
            bounds = _translated_bounds(bounds, move_x, move_y)
        return dx, dy

    def _candidate_deltas(
        bounds: tuple[float, float, float, float],
        base_dx: float,
        base_dy: float,
    ) -> list[tuple[float, float]]:
        candidates = [(base_dx, base_dy)]
        for radius in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0):
            for angle in range(0, 360, 45):
                dx = base_dx + radius * math.cos(math.radians(angle))
                dy = base_dy + radius * math.sin(math.radians(angle))
                candidates.append(
                    _clamp_delta_to_outline(
                        bounds,
                        dx,
                        dy,
                        outline,
                        interior_clearance,
                    )
                )
        return candidates

    for ref in sorted(placed_by_ref, key=_natural_ref_key):
        if ref in protected_refs:
            continue
        role = roles.get(ref)
        is_small = role is not None and role.role in small_roles
        if ref in explicit_floorplan_refs and not is_small:
            continue

        placed = placed_by_ref[ref]
        bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
        halo_overlap = bool(_overlapping_mounting_halos(bounds))
        if not is_small and not halo_overlap:
            continue
        outline_clearance = interior_clearance if is_small else clearance_mm
        base_dx, base_dy = _clamp_delta_to_outline(
            bounds,
            0.0,
            0.0,
            outline,
            outline_clearance,
        )
        hole_dx, hole_dy = _mounting_hole_escape_delta(
            _translated_bounds(bounds, base_dx, base_dy)
        )
        base_dx += hole_dx
        base_dy += hole_dy
        base_dx, base_dy = _clamp_delta_to_outline(
            bounds,
            base_dx,
            base_dy,
            outline,
            outline_clearance,
        )
        if (
            abs(base_dx) <= 1e-6
            and abs(base_dy) <= 1e-6
            and not halo_overlap
        ):
            continue

        best: tuple[float, float] | None = None
        best_distance = float("inf")
        seen: set[tuple[float, float]] = set()
        for dx, dy in _candidate_deltas(bounds, base_dx, base_dy):
            key = (round(dx, 6), round(dy, 6))
            if key in seen:
                continue
            seen.add(key)
            candidate_bounds = _translated_bounds(bounds, dx, dy)
            if _overlapping_mounting_halos(candidate_bounds):
                continue
            if _overlaps_others(ref, candidate_bounds):
                continue
            distance = math.hypot(dx, dy)
            if distance < best_distance:
                best = (dx, dy)
                best_distance = distance
                if abs(dx - base_dx) <= 1e-6 and abs(dy - base_dy) <= 1e-6:
                    break

        if best is None:
            continue
        dx, dy = best
        if abs(dx) <= 1e-6 and abs(dy) <= 1e-6:
            continue
        placed_by_ref[ref] = PlacedPart(
            ref=placed.ref,
            x_mm=placed.x_mm + dx,
            y_mm=placed.y_mm + dy,
            rot_deg=placed.rot_deg,
            footprint=placed.footprint,
            side=getattr(placed, "side", "front"),
        )
        moved_refs.add(ref)

    if not moved_refs:
        return placed_parts, []
    return [placed_by_ref[placed.ref] for placed in placed_parts], sorted(moved_refs)


def _natural_ref_key(ref: str) -> tuple[str, int, str]:
    match = re.match(r"([A-Za-z]+)(\d+)", str(ref))
    if match:
        return match.group(1), int(match.group(2)), str(ref)
    return str(ref), 0, str(ref)


def _pin_net_names_for_part(part) -> list[str]:
    names: list[str] = []
    for pin in getattr(part, "pins", []) or []:
        net = getattr(pin, "net", None)
        name = str(getattr(net, "name", "") or "")
        if name:
            names.append(name)
    return names


def _constraint_floorplan_refs(constraints: LayoutConstraints | None) -> set[str]:
    if constraints is None:
        return set()
    refs = {fixed.ref for fixed in constraints.fixed or []}
    refs.update(anchor.ref for anchor in constraints.edge_anchors or [])
    refs.update(face.ref for face in constraints.face_edges or [])
    for zone in constraints.zones or []:
        refs.update(zone.refs or [])
    for constraint in constraints.align or []:
        refs.update(constraint.refs or [])
    for constraint in constraints.distribute or []:
        refs.update(constraint.refs or [])
    for constraint in constraints.near or []:
        refs.add(constraint.ref)
        refs.add(constraint.target_ref)
    for constraint in constraints.far or []:
        refs.add(constraint.ref)
        refs.add(constraint.target_ref)
    return refs


def _constraint_pattern_refs(constraints: LayoutConstraints | None) -> set[str]:
    """Refs that are already part of an intentional alignment/distribution."""

    if constraints is None:
        return set()
    refs: set[str] = set()
    for constraint in constraints.align or []:
        refs.update(constraint.refs or [])
    for constraint in constraints.distribute or []:
        refs.update(constraint.refs or [])
    return refs


def _intent_pattern_refs(intent_plan: PlacementIntentPlan | None) -> set[str]:
    """Refs covered by mechanical/front-panel grid intent."""

    if intent_plan is None:
        return set()
    protected_mating_kinds = {
        "audio_jack",
        "barrel",
        "button",
        "coaxial",
        "display",
        "encoder",
        "eurorack_power",
        "ffc",
        "generic_connector",
        "header",
        "internal_header",
        "jst",
        "key",
        "midi",
        "module_socket",
        "nav_control",
        "panel_jack",
        "pot",
        "terminal_block",
        "usb",
    }
    mating_kinds = {
        mating.ref: mating.kind for mating in intent_plan.mating_intents or []
    }
    mechanical_refs = {
        ref
        for ref, intents in (intent_plan.intents or {}).items()
        if any(
            intent.kind in {"panel_control", "panel_jack"}
            or (
                intent.kind in {"front_panel_subject", "mechanical_mating"}
                and mating_kinds.get(ref) in protected_mating_kinds
            )
            for intent in intents
        )
    }
    refs: set[str] = set()
    for constraint in intent_plan.align_constraints or []:
        refs.update(ref for ref in (constraint.refs or []) if ref in mechanical_refs)
    for constraint in intent_plan.distribute_constraints or []:
        refs.update(ref for ref in (constraint.refs or []) if ref in mechanical_refs)
    return refs


def _arrange_passive_grid_between_opposing_headers(
    placed_parts: list[PlacedPart],
    circuit,
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[list[PlacedPart], list[str]]:
    if outline is None or circuit is None:
        return placed_parts, []

    anchors = _edge_anchor_map(intent_plan, constraints)
    left_refs = [ref for ref, anchor in anchors.items() if anchor.edge.lower() == "left"]
    right_refs = [ref for ref, anchor in anchors.items() if anchor.edge.lower() == "right"]
    if len(left_refs) != 1 or len(right_refs) != 1:
        return placed_parts, []

    mounting_refs = set(intent_plan.refs_with_kind("mounting_hole")) if intent_plan else set()
    edge_refs = set(left_refs + right_refs)
    explicit_floorplan_refs = _constraint_floorplan_refs(constraints)
    excluded_refs = mounting_refs | edge_refs | explicit_floorplan_refs

    part_by_ref = {
        str(getattr(part, "ref", "") or ""): part
        for part in getattr(circuit, "parts", []) or []
    }

    passive_refs: list[str] = []
    primary_refs: list[str] = []
    for ref, part in part_by_ref.items():
        if ref in excluded_refs:
            continue
        try:
            pin_count = len(part)
        except Exception:
            pin_count = 0
        prefix = re.match(r"[A-Za-z]+", ref)
        prefix_text = prefix.group(0).upper() if prefix else ""
        if pin_count == 2 and prefix_text in {"R", "C", "L", "FB", "D"}:
            passive_refs.append(ref)
        elif pin_count > 2:
            primary_refs.append(ref)

    if len(passive_refs) < 3 or primary_refs:
        return placed_parts, []

    placed_by_ref = {placed.ref: placed for placed in placed_parts}
    if not all(ref in placed_by_ref for ref in passive_refs + left_refs + right_refs):
        return placed_parts, []

    left_bounds = _placed_bounds(placed_by_ref[left_refs[0]], fp_bboxes, fp_geometries)
    right_bounds = _placed_bounds(placed_by_ref[right_refs[0]], fp_bboxes, fp_geometries)
    usable_x_min = left_bounds[2] + 4.0
    usable_x_max = right_bounds[0] - 4.0
    usable_y_min = outline.y_min + max(5.0, outline.height_mm * 0.24)
    usable_y_max = outline.y_max - max(5.0, outline.height_mm * 0.24)
    if usable_x_max <= usable_x_min or usable_y_max <= usable_y_min:
        return placed_parts, []

    header_y_by_net: dict[str, list[float]] = {}
    for ref in edge_refs:
        part = part_by_ref.get(ref)
        placed = placed_by_ref.get(ref)
        geometry = (fp_geometries or {}).get(placed.footprint) if placed else None
        if part is None or placed is None or geometry is None:
            continue
        centers = geometry.pad_world_centers(placed)
        pins = list(getattr(part, "pins", []) or [])
        for index, pin in enumerate(pins, start=1):
            net = getattr(pin, "net", None)
            name = str(getattr(net, "name", "") or "")
            if not name:
                continue
            pin_num = str(getattr(pin, "num", "") or index)
            center = centers.get(pin_num)
            if center is not None:
                header_y_by_net.setdefault(name, []).append(center[1])

    def _passive_target_y(ref: str) -> float:
        part = part_by_ref.get(ref)
        ys: list[float] = []
        for name in _pin_net_names_for_part(part):
            ys.extend(header_y_by_net.get(name, []))
        if ys:
            return sum(ys) / len(ys)
        return (placed_by_ref[ref].y_mm if ref in placed_by_ref else 0.0)

    def _passive_group_key(ref: str) -> tuple[str, str]:
        part = part_by_ref.get(ref)
        nets = _pin_net_names_for_part(part)
        signal_nets = [
            name
            for name in nets
            if not POWER_NET_RE.match(name) and not GND_NET_RE.match(name)
        ]
        if signal_nets:
            return "signal", sorted(signal_nets)[0]
        if any(POWER_NET_RE.match(name) for name in nets):
            return "power", "supply"
        if any(GND_NET_RE.match(name) for name in nets):
            return "ground", "ground"
        return "misc", ref

    grouped: dict[tuple[str, str], list[str]] = {}
    for ref in passive_refs:
        grouped.setdefault(_passive_group_key(ref), []).append(ref)

    groups = [
        sorted(refs, key=_natural_ref_key)
        for _, refs in sorted(
            grouped.items(),
            key=lambda item: (
                sum(_passive_target_y(ref) for ref in item[1]) / len(item[1]),
                item[0],
            ),
        )
    ]

    group_count = len(groups)
    group_columns = 1 if group_count <= 3 else 2

    def _part_size(ref: str) -> tuple[float, float]:
        placed = placed_by_ref[ref]
        bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
        return bounds[2] - bounds[0], bounds[3] - bounds[1]

    def _group_metrics(refs: list[str]) -> tuple[int, int, float, float, float, float]:
        widths, heights = zip(*(_part_size(ref) for ref in refs))
        max_width = max(widths)
        max_height = max(heights)
        local_cols = min(len(refs), 2)
        local_rows = math.ceil(len(refs) / local_cols)
        x_step = max(3.2, max_width + 0.9)
        y_step = max(3.2, max_height + 0.9)
        span_width = max_width + (local_cols - 1) * x_step
        span_height = max_height + (local_rows - 1) * y_step
        return local_cols, local_rows, x_step, y_step, span_width, span_height

    metrics_by_group = [_group_metrics(refs) for refs in groups]
    max_group_width = max(metric[4] for metric in metrics_by_group)
    max_group_height = max(metric[5] for metric in metrics_by_group)
    usable_width = usable_x_max - usable_x_min
    usable_height = usable_y_max - usable_y_min

    def _grid_fits(columns: int) -> bool:
        rows = math.ceil(group_count / columns)
        if columns > 1 and usable_width / (columns - 1) < max_group_width + 1.0:
            return False
        if rows > 1 and usable_height / (rows - 1) < max_group_height + 1.0:
            return False
        return usable_width >= max_group_width and usable_height >= max_group_height

    if group_columns > 1 and not _grid_fits(group_columns):
        group_columns = 1
    if not _grid_fits(group_columns):
        return placed_parts, []

    group_rows = math.ceil(group_count / group_columns)

    def _group_center(index: int) -> tuple[float, float]:
        _, _, _, _, span_width, span_height = metrics_by_group[index]
        row = index // group_columns
        col = index % group_columns
        if group_columns == 1:
            x = (usable_x_min + usable_x_max) / 2
        else:
            x = usable_x_min + (usable_x_max - usable_x_min) * col / (group_columns - 1)
        if group_rows == 1:
            y = (usable_y_min + usable_y_max) / 2
        else:
            y = usable_y_min + (usable_y_max - usable_y_min) * row / (group_rows - 1)
        x_min = usable_x_min + span_width / 2
        x_max = usable_x_max - span_width / 2
        y_min = usable_y_min + span_height / 2
        y_max = usable_y_max - span_height / 2
        return (
            max(x_min, min(x_max, x)) if x_min <= x_max else x,
            max(y_min, min(y_max, y)) if y_min <= y_max else y,
        )

    def _local_position(
        group_center: tuple[float, float],
        index: int,
        metrics: tuple[int, int, float, float, float, float],
    ) -> tuple[float, float]:
        local_cols, local_rows, x_step, y_step, _, _ = metrics
        if local_cols == 1 and local_rows == 1:
            return group_center
        row = index // local_cols
        col = index % local_cols
        x = group_center[0] + (col - (local_cols - 1) / 2) * x_step
        y = group_center[1] + (row - (local_rows - 1) / 2) * y_step
        return (
            max(usable_x_min, min(usable_x_max, x)),
            max(usable_y_min, min(usable_y_max, y)),
        )

    replacements: dict[str, PlacedPart] = {}
    moved_refs: list[str] = []
    for group_index, refs in enumerate(groups):
        center = _group_center(group_index)
        metrics = metrics_by_group[group_index]
        for local_index, ref in enumerate(refs):
            placed = placed_by_ref[ref]
            x_mm, y_mm = _local_position(center, local_index, metrics)
            replacements[ref] = PlacedPart(
                ref=placed.ref,
                x_mm=x_mm,
                y_mm=y_mm,
                rot_deg=placed.rot_deg,
                footprint=placed.footprint,
                side=getattr(placed, "side", "front"),
            )
            moved_refs.append(ref)

    return [replacements.get(placed.ref, placed) for placed in placed_parts], moved_refs


def _spread_grid_subjects_on_generous_outline(
    placed_parts: list[PlacedPart],
    circuit,
    outline: BoardOutline | None,
    intent_plan: PlacementIntentPlan | None,
    constraints: LayoutConstraints | None,
    user_constraints: LayoutConstraints | None,
    fp_bboxes: dict[str, tuple[float, float]],
    fp_geometries: dict[str, FootprintGeometry] | None,
) -> tuple[list[PlacedPart], list[str]]:
    """Use a generous fixed outline for visible/grid subjects instead of bunching."""

    if outline is None or circuit is None:
        return placed_parts, []

    anchors = _edge_anchor_map(intent_plan, constraints)
    protected_refs = set(anchors)
    protected_refs.update(
        intent_plan.refs_with_kind("mounting_hole") if intent_plan else []
    )
    protected_refs.update(_intent_pattern_refs(intent_plan))
    protected_refs.update(
        fixed.ref for fixed in (constraints.fixed if constraints else []) or []
    )
    protected_refs.update(
        _constraint_pattern_refs(constraints) & _intent_pattern_refs(intent_plan)
    )
    protected_refs.update(_constraint_floorplan_refs(user_constraints))

    roles = classify_parts(circuit)
    part_by_ref = {
        str(getattr(part, "ref", "") or ""): part
        for part in getattr(circuit, "parts", []) or []
    }
    placed_by_ref = {placed.ref: placed for placed in placed_parts}

    subject_refs: list[str] = []
    for ref, part in part_by_ref.items():
        if ref in protected_refs or ref not in placed_by_ref:
            continue
        role = roles.get(ref)
        role_name = role.role if role is not None else ""
        intents = intent_plan.intents_for(ref) if intent_plan else []
        intent_kinds = {intent.kind for intent in intents}
        if (
            is_ui_grid_part(part)
            or role_name in {"panel_jack", "control"}
            or intent_kinds
            & {
                "front_panel_subject",
                "panel_control",
                "panel_jack",
                "sensor_grid_subject",
            }
        ):
            subject_refs.append(ref)

    subject_refs = sorted(subject_refs, key=_natural_ref_key)
    if len(subject_refs) < 2:
        return placed_parts, []

    subject_bounds = [
        _placed_bounds(placed_by_ref[ref], fp_bboxes, fp_geometries)
        for ref in subject_refs
    ]
    x_min = min(bounds[0] for bounds in subject_bounds)
    y_min = min(bounds[1] for bounds in subject_bounds)
    x_max = max(bounds[2] for bounds in subject_bounds)
    y_max = max(bounds[3] for bounds in subject_bounds)
    current_w = max(0.0, x_max - x_min)
    current_h = max(0.0, y_max - y_min)
    compact_area = (current_w + 6.0) * (current_h + 6.0)
    outline_area = outline.width_mm * outline.height_mm
    if outline_area <= 0.0 or compact_area <= 0.0:
        return placed_parts, []

    area_ratio = outline_area / compact_area
    if area_ratio < 1.4:
        return placed_parts, []

    points = [(placed_by_ref[ref].x_mm, placed_by_ref[ref].y_mm) for ref in subject_refs]
    if points_form_clean_grid(points, tolerance_mm=1.0):
        dominant_span_ratio = max(
            current_w / max(outline.width_mm, 1.0),
            current_h / max(outline.height_mm, 1.0),
        )
        if dominant_span_ratio >= (0.35 if len(subject_refs) <= 3 else 0.45):
            return placed_parts, []

    count = len(subject_refs)
    max_width = max(bounds[2] - bounds[0] for bounds in subject_bounds)
    max_height = max(bounds[3] - bounds[1] for bounds in subject_bounds)
    x_pad = max(6.0, outline.width_mm * 0.16, max_width / 2 + 1.0)
    y_pad = max(6.0, outline.height_mm * 0.16, max_height / 2 + 1.0)
    x_start = outline.x_min + x_pad
    x_end = outline.x_max - x_pad
    y_start = outline.y_min + y_pad
    y_end = outline.y_max - y_pad
    if x_start >= x_end or y_start >= y_end:
        return placed_parts, []

    preferred_cols = choose_grid_columns(
        count,
        outline.width_mm,
        outline.height_mm,
        max_columns=min(count, 4),
    )
    if count == 2:
        preferred_cols = 2 if outline.width_mm >= outline.height_mm else 1

    cols = None
    rows = None
    for candidate_cols in range(preferred_cols, 0, -1):
        candidate_rows = math.ceil(count / candidate_cols)
        x_step = 0.0 if candidate_cols <= 1 else (x_end - x_start) / (candidate_cols - 1)
        y_step = 0.0 if candidate_rows <= 1 else (y_end - y_start) / (candidate_rows - 1)
        if candidate_cols > 1 and x_step < max_width + 2.0:
            continue
        if candidate_rows > 1 and y_step < max_height + 2.0:
            continue
        cols = candidate_cols
        rows = candidate_rows
        break

    if cols is None or rows is None:
        return placed_parts, []

    replacements: dict[str, PlacedPart] = {}
    moved_refs: list[str] = []
    for index, ref in enumerate(subject_refs):
        row = index // cols
        col = index % cols
        row_count = min(cols, count - row * cols)
        if row_count == 1:
            x_mm = (x_start + x_end) / 2
        else:
            x_mm = x_start + (x_end - x_start) * col / (row_count - 1)
        if rows == 1:
            y_mm = (y_start + y_end) / 2
        else:
            y_mm = y_start + (y_end - y_start) * row / (rows - 1)

        placed = placed_by_ref[ref]
        bounds = _placed_bounds(placed, fp_bboxes, fp_geometries)
        dx = x_mm - placed.x_mm
        dy = y_mm - placed.y_mm
        dx, dy = _clamp_delta_to_outline(bounds, dx, dy, outline, 0.8)
        if abs(dx) <= 1e-6 and abs(dy) <= 1e-6:
            continue
        replacements[ref] = PlacedPart(
            ref=placed.ref,
            x_mm=placed.x_mm + dx,
            y_mm=placed.y_mm + dy,
            rot_deg=placed.rot_deg,
            footprint=placed.footprint,
            side=getattr(placed, "side", "front"),
        )
        moved_refs.append(ref)

    if not moved_refs:
        return placed_parts, []
    return [replacements.get(placed.ref, placed) for placed in placed_parts], moved_refs


# Round-7 WS26: implicit default-on threshold. Below this part count the
# subprocess+interpreter-import overhead (~2-4 s) dwarfs the plan time, so tiny
# boards and the test suites stay sequential unless an explicit kwarg/env asks
# for parallelism. An explicit request is always honored regardless of size.
_PARALLEL_DEFAULT_MIN_PARTS = 30


def _resolve_parallel_workers(parallel_workers: int | None) -> int | None:
    """Explicit kwarg wins, else SKIDL_LAYOUT_PARALLEL env default."""
    if parallel_workers is not None:
        return parallel_workers
    env = os.environ.get("SKIDL_LAYOUT_PARALLEL")
    if env:
        try:
            return int(env)
        except ValueError:
            raise ValueError(
                f"SKIDL_LAYOUT_PARALLEL must be an integer, got {env!r}"
            )
    return None


def _effective_parallel_workers(parallel_workers: int | None, circuit) -> int | None:
    """Resolve the worker count with round-7 WS26 default-on semantics.

    Precedence: explicit kwarg > ``SKIDL_LAYOUT_PARALLEL`` env > implicit
    default. When neither kwarg nor env is set, engage parallelism implicitly on
    boards with ``>= _PARALLEL_DEFAULT_MIN_PARTS`` parts using
    ``min(4, cpu_count)`` workers (4 is where DPSG plateaus; only unique
    candidates parallelize, so more buys nothing). Smaller boards stay ``None``
    (sequential). ``parallel_workers=1`` / env ``1`` is the kill switch (returns
    a value ``< 2`` that the caller's ``>= 2`` engage check leaves sequential).
    """
    resolved = _resolve_parallel_workers(parallel_workers)
    if resolved is None:
        part_count = len(getattr(circuit, "parts", []) or [])
        if part_count >= _PARALLEL_DEFAULT_MIN_PARTS:
            resolved = min(4, os.cpu_count() or 1)
    return resolved


def _refine_candidate_trio(
    candidate,
    circuit,
    resolved_bboxes,
    fp_geometries,
    clearance_mm,
    board_layers,
    ctx,
    progress,
):
    """Run the pass-1 refinement trio on one candidate, mutating it in place.

    Module-level (not a closure) so a spawn worker can import it by name — see
    ``parallel.refine_candidate_worker``. Behaviour is exactly the three calls
    the sequential candidate loop used to make inline.
    """
    refine_candidate_orientations(candidate, circuit, fp_geometries)
    refine_candidate_decaps(
        candidate,
        circuit,
        fp_geometries,
        resolved_bboxes,
        ctx=ctx,
    )
    refine_candidate_placement(
        candidate,
        circuit,
        resolved_bboxes,
        fp_geometries=fp_geometries,
        clearance_mm=clearance_mm,
        board_layers=board_layers,
        ctx=ctx,
        progress=progress,
    )
    return candidate


def _posttrio_candidate_impl(
    candidate,
    circuit,
    params: _FinalizeParams,
    ctx,
) -> tuple[LayoutScore, ValidationResult]:
    """Module-level extraction of plan_layout's pass-1 post-trio block (round-8
    WS29). Runs, in order, on a candidate whose refinement trio already ran:
    ``_apply_assembly_sides`` -> (edge-anchor snap / neighbor legalize, only when
    a real outline is fixed) -> ``_effective_keepouts`` -> ``validate`` ->
    ``score_placement`` (or ``score_placement_quick`` when validation failed) ->
    ``_apply_edge_intent_score`` -> ``_apply_panel_mechanical_outline_score``.
    Mutates ``candidate.placed_parts`` / ``.reasons`` / ``.ref_reasons`` /
    ``.score`` in place and returns ``(score, validation)``. Byte-identical to the
    former inline block; shared verbatim by the sequential pass-1 loop and the
    round-8 combined worker (``parallel.plan_candidate_worker``). Reads every
    positional-dependent input from ``params`` (a :class:`_FinalizeParams`); the
    combined payload reuses that bundle as-is."""
    resolved_bboxes = params.resolved_bboxes
    fp_geometries = params.fp_geometries
    clearance_mm = params.clearance_mm
    board_layers = params.board_layers
    intent_plan = params.intent_plan
    resolved_outline = params.resolved_outline
    auto_outline = params.auto_outline
    resolved_constraints = params.resolved_constraints

    candidate.placed_parts = _apply_assembly_sides(
        candidate.placed_parts,
        intent_plan,
    )
    candidate_constraints = candidate.constraints or resolved_constraints
    if resolved_outline is not None and not auto_outline:
        candidate.placed_parts, moved_edge_refs = _snap_edge_anchors_to_outline(
            candidate.placed_parts,
            resolved_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
        )
        if moved_edge_refs:
            candidate.reasons.append("edge connectors snapped to outline edges")
            for ref in moved_edge_refs:
                candidate.ref_reasons.setdefault(ref, []).append(
                    "snapped to outline edge"
                )
        candidate.placed_parts, moved_neighbor_refs = _legalize_edge_anchor_neighbors(
            candidate.placed_parts,
            resolved_outline,
            intent_plan,
            candidate_constraints,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
        )
        if moved_neighbor_refs:
            candidate.reasons.append(
                "near-edge parts nudged clear of edge connectors"
            )
            for ref in moved_neighbor_refs:
                candidate.ref_reasons.setdefault(ref, []).append(
                    "nudged clear of edge connector"
                )
    candidate_keepouts = _effective_keepouts(
        candidate_constraints,
        candidate.placed_parts,
        intent_plan,
        resolved_bboxes,
        fp_geometries,
        resolved_outline,
    )
    validation = validate(
        candidate.placed_parts,
        circuit,
        resolved_bboxes,
        clearance_mm=clearance_mm,
        outline=resolved_outline,
        keepouts=candidate_keepouts,
        cutouts=getattr(candidate_constraints, "cutouts", None),
        fp_geometries=fp_geometries,
    )
    if not validation.ok:
        raw_score = score_placement_quick(
            candidate.placed_parts,
            circuit,
            resolved_bboxes,
            outline=resolved_outline,
            keepouts=candidate_keepouts,
            cutouts=getattr(candidate_constraints, "cutouts", None),
            fp_geometries=fp_geometries,
            clearance_mm=clearance_mm,
            ctx=ctx,
        )
    else:
        raw_score = score_placement(
            candidate.placed_parts,
            circuit,
            resolved_bboxes,
            outline=resolved_outline,
            keepouts=candidate_keepouts,
            cutouts=getattr(candidate_constraints, "cutouts", None),
            fp_geometries=fp_geometries,
            clearance_mm=clearance_mm,
            board_layers=board_layers,
            ctx=ctx,
        )
    edge_score = _apply_edge_intent_score(
        raw_score,
        candidate.placed_parts,
        resolved_bboxes,
        resolved_outline,
        intent_plan,
        constraints=candidate.constraints,
        fp_geometries=fp_geometries,
    )
    score = _apply_panel_mechanical_outline_score(
        edge_score,
        candidate.placed_parts,
        resolved_bboxes,
        resolved_outline,
        intent_plan,
        fp_geometries=fp_geometries,
    )
    candidate.score = score.score
    return score, validation


def _prerefine_candidates_parallel(
    candidates,
    seed_keys,
    workers,
    circuit,
    resolved_bboxes,
    fp_geometries,
    clearance_mm,
    board_layers,
    emit,
    snapshot,
):
    """Refine each unique (seed-key) candidate's pass-1 trio in a ``spawn``
    worker pool, replacing the canonical entries of ``candidates`` in place with
    the worker-refined objects.

    Returns the set of refined canonical names, or ``None`` to tell the caller
    to run the plain sequential loop instead (child process, too few unique
    jobs, or ANY error — pickling, spawn, worker crash). On the ``None`` path
    ``candidates`` is left untouched, so the fallback is byte-identical.

    ``snapshot`` is the picklable :class:`~skidl_layout.snapshot.SnapshotCircuit`
    built once by the caller (round-6 WS22) and reused across both parallel
    phases; the caller passes ``None`` only when it could not be built, in which
    case this falls back to sequential.

    The canonical selection is first-seen-wins over ``seed_keys`` in candidate
    order — identical to the sequential loop's dedup — so the loop's own dedup
    reproduces the same canonical/dup assignment against the same keys.
    """
    import multiprocessing

    # Belt-and-braces: never parallelize inside a spawn child (an unguarded
    # driver script re-imports its module top level in every worker).
    if multiprocessing.parent_process() is not None:
        return None
    if snapshot is None:
        return None

    seen: dict = {}
    canonical_indices: list[int] = []
    for i, key in enumerate(seed_keys):
        if key not in seen:
            seen[key] = i
            canonical_indices.append(i)
    if len(canonical_indices) < 2:
        return None  # nothing to parallelize

    try:
        import pickle

        from .parallel import run_payloads

        payloads = {
            i: pickle.dumps(
                (
                    candidates[i],
                    snapshot,
                    resolved_bboxes,
                    fp_geometries,
                    clearance_mm,
                    board_layers,
                )
            )
            for i in canonical_indices
        }
        k = min(workers, len(canonical_indices))
        emit(
            f"refining {len(canonical_indices)} unique candidate(s) in parallel "
            f"({k} workers); per-ref progress is suppressed in parallel mode"
        )
        # Round-7 WS25: plain-subprocess transport (safe for unguarded callers).
        # Results come back keyed by candidate index via per-index output files,
        # so completion order cannot matter.
        raw = run_payloads("refine", payloads, workers)
        refined_by_index: dict[int, object] = {
            i: pickle.loads(b) for i, b in raw.items()
        }
    except Exception as exc:  # noqa: BLE001 - any failure -> sequential fallback
        emit(
            f"parallel refinement unavailable ({exc}); falling back to sequential"
        )
        return None

    prerefined_names: set[str] = set()
    for i in canonical_indices:
        refined = refined_by_index[i]
        candidates[i] = refined
        prerefined_names.add(refined.name)
        emit(f"[parallel] {refined.name}: refined")
    return prerefined_names


def _finalize_candidates_parallel(
    candidates,
    dup_canonical_name,
    workers,
    circuit,
    params,
    snapshot,
    emit,
):
    """Finalize each *canonical* (non-dup) candidate's post-anchor pass in a
    ``spawn`` worker pool (round-6 WS22), replacing the canonical entries of
    ``candidates`` in place with the worker-mutated candidate objects and
    returning ``{candidate_name: _FinalizedCandidate}`` for those canonicals.

    Returns ``None`` to tell the caller to run the plain sequential finalize
    loop instead (child process, no snapshot, < 2 canonicals, or ANY error —
    pickling, spawn, worker crash). On the ``None`` path ``candidates`` is left
    untouched, so the fallback is byte-identical.

    Each canonical's finalize is independent of the others: the only nonlocal it
    threaded (``density_outline``) is either read-only from the pre-loop value
    (auto-outline path) or recomputed from a pure function inside the closure
    (derive path), so shipping the same ``params`` to every worker and
    discarding the workers' returned outline is exact (plan hazard #2).
    """
    import multiprocessing

    if multiprocessing.parent_process() is not None:
        return None
    if snapshot is None:
        return None

    canonical_indices = [
        i
        for i, candidate in enumerate(candidates)
        if candidate.name not in dup_canonical_name
    ]
    if len(canonical_indices) < 2:
        return None

    try:
        import pickle

        from .parallel import run_payloads

        payloads = {
            i: pickle.dumps((candidates[i], snapshot, params))
            for i in canonical_indices
        }
        k = min(workers, len(canonical_indices))
        emit(
            f"finalizing {len(canonical_indices)} unique candidate(s) in "
            f"parallel ({k} workers)"
        )
        # Round-7 WS25: plain-subprocess transport (safe for unguarded callers).
        raw = run_payloads("finalize", payloads, workers)
        finalized_by_index: dict[int, _FinalizedCandidate] = {
            i: pickle.loads(b) for i, b in raw.items()
        }
    except Exception as exc:  # noqa: BLE001 - any failure -> sequential fallback
        emit(
            f"parallel finalize unavailable ({exc}); falling back to sequential"
        )
        return None

    finalized_by_name: dict[str, _FinalizedCandidate] = {}
    for i in canonical_indices:
        finalized = finalized_by_index[i]
        # The worker mutated & returned the whole candidate (finalize appends
        # reasons and sets placed_parts/constraints/score); adopt it in place so
        # the dup-reuse branch reads the finalized canonical (plan hazard #4).
        candidates[i] = finalized.candidate
        finalized_by_name[finalized.candidate.name] = finalized
    return finalized_by_name


def plan_layout(
    circuit,
    fp_bboxes: dict[str, tuple[float, float]] | None = None,
    fp_lib_dirs: list[str] | None = None,
    constraints: LayoutConstraints | None = None,
    outline: BoardOutline | None = None,
    existing_pcb_path: str | None = None,
    board_layers: int = 2,
    margin_mm: float = 3.0,
    clearance_mm: float = 0.5,
    derive_outline_if_missing: bool = True,
    routability: RoutabilityFeedback | None = None,
    assembly_policy: str | None = None,
    corner_radius_mm: float | None = None,
    candidate_names: list[str] | None = None,
    max_candidates: int | None = None,
    parallel_workers: int | None = None,
    progress=None,
) -> LayoutResult:
    """Place and score a board attempt without writing copper geometry.

    ``candidate_names`` restricts placement to the named candidate strategies
    (e.g. ``["baseline", "connector_edge_first"]``) for faster iteration —
    fewer strategies refined means proportionally less time. Unknown names
    raise ``ValueError``. When omitted, the ``SKIDL_LAYOUT_CANDIDATES`` env var
    (comma-separated) is honored as a default; an explicit kwarg always wins.
    Leave both unset (the default) to evaluate every strategy.

    ``max_candidates`` caps how many strategies are refined by pre-scoring each
    candidate's *seed* placement (a cheap heuristic predictor — not the refined
    quality) and keeping the top N. The ``SKIDL_LAYOUT_MAX_CANDIDATES`` env var
    is the default; an explicit kwarg wins. Default ``None`` refines all (after
    ``candidate_names`` filtering). Use for fast iteration, not final boards.

    ``progress`` is an optional ``Callable[[str], None]`` invoked with a short
    human-readable message at each stage boundary (candidate refinement,
    finalization, selection). Default ``None`` is silent and has zero behavioral
    effect. Placement can take minutes on a large board; a callback that prints
    (with ``flush=True``) makes it observable when stdout is redirected.

    ``parallel_workers`` controls refining the unique candidates' pass-1 trio
    (orientation/decap/placement) and their post-anchor finalize concurrently in
    plain subprocess workers. **Parallelism is the DEFAULT on boards >= 30
    parts** (``min(4, cpu_count)`` workers); pass ``parallel_workers=1`` (or set
    ``SKIDL_LAYOUT_PARALLEL=1``) to force sequential — that is the kill switch.
    Precedence is explicit kwarg > ``SKIDL_LAYOUT_PARALLEL`` env > implicit
    default; only a resolved value ``>= 2`` (with ``>= 2`` unique candidates)
    engages it, so tiny boards stay sequential. Output is **identical** to the
    sequential default (each worker refines a picklable
    :class:`~skidl_layout.snapshot.SnapshotCircuit`, proven byte-identical), and
    ANY worker/pickling/subprocess error falls back silently to the sequential
    loop. The workers are plain ``python -m skidl_layout._worker_main``
    subprocesses that never re-import the calling script, so **no
    ``if __name__ == "__main__":`` guard is required** (round-7 WS25). Per-ref
    ``progress`` lines are suppressed in parallel mode.
    """
    def _emit(message: str) -> None:
        if progress is not None:
            progress(message)
    fp_geometries = _resolve_geometries(circuit, fp_lib_dirs)
    resolved_bboxes = _resolve_bboxes(circuit, fp_bboxes, fp_lib_dirs)
    geometry_boxes = geometry_bboxes(fp_geometries)
    if fp_bboxes is None:
        resolved_bboxes.update(geometry_boxes)
    else:
        for footprint, bbox in geometry_boxes.items():
            resolved_bboxes.setdefault(footprint, bbox)

    resolved_outline = _resolve_outline(constraints, outline, existing_pcb_path)
    if resolved_outline is not None and corner_radius_mm is not None:
        resolved_outline.corner_radius_mm = max(0.0, float(corner_radius_mm))
    resolved_constraints = _copy_constraints(constraints, resolved_outline)
    auto_outline = resolved_outline is None and derive_outline_if_missing
    density_outline: BoardOutline | None = None
    form_factor = getattr(resolved_constraints, "form_factor", None)
    if auto_outline:
        if form_factor:
            resolved_outline = _auto_outline_from_circuit(
                circuit,
                resolved_bboxes,
                form_factor,
            )
        else:
            density_outline = _compact_auto_outline_seed(
                circuit,
                derive_outline_from_circuit(circuit, resolved_bboxes),
            )
            resolved_outline = density_outline
        if resolved_outline is not None and corner_radius_mm is not None:
            resolved_outline.corner_radius_mm = max(0.0, float(corner_radius_mm))
        resolved_constraints.outline = resolved_outline

    groups = extract_groups(circuit)
    intent_plan = infer_placement_intents(
        circuit,
        outline=resolved_outline,
        assembly_policy=assembly_policy,
    )
    power_topology = infer_power_topology(circuit)
    candidates = generate_placement_candidates(
        groups,
        resolved_constraints,
        resolved_bboxes,
        intent_plan=intent_plan,
        power_topology=power_topology,
        fp_geometries=fp_geometries,
    )
    candidates = _filter_candidates(candidates, candidate_names)

    ctx = LayoutContext.from_circuit(circuit)

    resolved_max_candidates = _resolve_max_candidates(max_candidates)
    if resolved_max_candidates is not None and resolved_max_candidates < len(candidates):
        pruned = _prune_candidates(
            candidates,
            resolved_max_candidates,
            circuit,
            resolved_bboxes,
            resolved_outline,
            resolved_constraints.keepouts,
            resolved_constraints.cutouts,
            fp_geometries,
            clearance_mm,
            ctx,
        )
        _emit(
            f"pruned {len(candidates)} -> {len(pruned)} candidate(s) by seed "
            "quick-score (max_candidates)"
        )
        candidates = pruned
    _emit(f"generated {len(candidates)} candidate strategy(ies); refining")

    # Seed keys precomputed ONCE, before any refinement mutates placed_parts, so
    # the loop's dedup is stable even when parallel pre-refinement has already
    # replaced the canonical candidates with their refined (mutated) selves.
    seed_keys = [_candidate_seed_key(candidate) for candidate in candidates]

    # Round-8 WS29: build the picklable _FinalizeParams bundle ONCE, before the
    # pass-1 loop, and reuse it at all three consumers — the pass-1 post-trio
    # block (_posttrio_candidate_impl), the sequential finalize closure, and the
    # parallel finalize/combined dispatch. `density_outline` holds its pre-loop
    # value here on every path (set once above; nothing writes it before the
    # finalize builds — plan hazard #7), so a single build cannot drift.
    params_early = _FinalizeParams(
        resolved_bboxes=resolved_bboxes,
        fp_geometries=fp_geometries,
        clearance_mm=clearance_mm,
        board_layers=board_layers,
        margin_mm=margin_mm,
        corner_radius_mm=corner_radius_mm,
        form_factor=form_factor,
        auto_outline=auto_outline,
        resolved_outline=resolved_outline,
        resolved_constraints=resolved_constraints,
        density_outline=density_outline,
        intent_plan=intent_plan,
        derive_outline_if_missing=derive_outline_if_missing,
        constraints=constraints,
    )

    # WS18/WS22: opt-in parallel machinery. The picklable snapshot is built at
    # most ONCE per plan_layout call (round-6 WS22) and reused by both the pass-1
    # and finalize parallel phases; a build failure emits the fallback message
    # and leaves both phases sequential (byte-identical). `resolved_workers` and
    # `layout_snapshot` are hoisted here so the finalize dispatch below reuses
    # them.
    # Round-7 WS26: default-on. With no explicit kwarg/env, parallelism engages
    # implicitly on boards >= 30 parts (min(4, cpu_count) workers). Precedence is
    # kwarg > env > implicit default; parallel_workers=1 / SKIDL_LAYOUT_PARALLEL=1
    # is the documented kill switch (falls out of the >= 2 engage check). Plain
    # subprocess workers (WS25) never re-import the calling script, so no
    # __main__-guard is required.
    resolved_workers = _effective_parallel_workers(parallel_workers, circuit)
    parallel_enabled = resolved_workers is not None and resolved_workers >= 2
    layout_snapshot = None
    if parallel_enabled:
        import multiprocessing

        # Never parallelize inside a spawn child (an unguarded driver re-imports
        # its module top level in every worker).
        if multiprocessing.parent_process() is None:
            try:
                from .snapshot import snapshot_circuit

                layout_snapshot = snapshot_circuit(circuit)
            except Exception as exc:  # noqa: BLE001 - any failure -> sequential
                _emit(
                    f"parallel layout unavailable ({exc}); falling back to "
                    "sequential"
                )
                layout_snapshot = None

    # WS18: opt-in parallel pass-1 refinement of the unique candidates. Byte-
    # identical to sequential; returns the set of already-refined canonical
    # names (empty when not engaged / on fallback), and mutates `candidates` in
    # place with the worker-refined objects.
    prerefined_names: set[str] = set()
    if parallel_enabled and layout_snapshot is not None:
        result_names = _prerefine_candidates_parallel(
            candidates,
            seed_keys,
            resolved_workers,
            circuit,
            resolved_bboxes,
            fp_geometries,
            clearance_mm,
            board_layers,
            _emit,
            layout_snapshot,
        )
        if result_names is not None:
            prerefined_names = result_names

    candidate_scores: dict[str, LayoutScore] = {}
    candidate_validations: dict[str, ValidationResult] = {}
    # WS1: candidates whose (seed placement, constraints) match an already-refined
    # candidate produce a byte-identical result deterministically — reuse it
    # instead of re-refining. Maps dup name -> canonical (already-refined) name.
    canonical_by_key: dict[tuple, PlacementCandidate] = {}
    dup_canonical_name: dict[str, str] = {}
    for cand_index, candidate in enumerate(candidates, start=1):
        seed_key = seed_keys[cand_index - 1]
        canonical = canonical_by_key.get(seed_key)
        if canonical is not None:
            dup_canonical_name[candidate.name] = canonical.name
            _emit(
                f"[{cand_index}/{len(candidates)}] {candidate.name}: "
                f"reused refinement of '{canonical.name}'"
            )
            candidate.placed_parts = _clone_placed(canonical.placed_parts)
            candidate.constraints = canonical.constraints
            candidate.pin_gravity_anchored_refs = set(
                canonical.pin_gravity_anchored_refs
            )
            candidate.score = canonical.score
            candidate_scores[candidate.name] = candidate_scores[canonical.name]
            candidate_validations[candidate.name] = candidate_validations[
                canonical.name
            ]
            candidate.reasons.append(
                f"identical to candidate '{canonical.name}'; refinement reused"
            )
            continue
        if candidate.name in prerefined_names:
            _emit(
                f"[{cand_index}/{len(candidates)}] {candidate.name}: "
                "refined (parallel)"
            )
        else:
            _emit(f"[{cand_index}/{len(candidates)}] refining {candidate.name}")
            _refine_candidate_trio(
                candidate,
                circuit,
                resolved_bboxes,
                fp_geometries,
                clearance_mm,
                board_layers,
                ctx,
                progress=(
                    (lambda m, _n=candidate.name: _emit(f"[{_n}] {m}"))
                    if progress is not None
                    else None
                ),
            )
        canonical_by_key[seed_key] = candidate
        # Round-8 WS29: the pass-1 post-trio block is now a shared module-level
        # impl (byte-identical), so the sequential loop and the combined worker
        # run the SAME code (plan hazard #2).
        score, validation = _posttrio_candidate_impl(
            candidate, circuit, params_early, ctx
        )
        candidate_scores[candidate.name] = score
        candidate_validations[candidate.name] = validation

    any_valid = any(
        candidate_validations[c.name].ok for c in candidates
    )
    if not any_valid:
        for candidate in candidates:
            candidate_constraints = candidate.constraints or resolved_constraints
            candidate_keepouts = _effective_keepouts(
                candidate_constraints,
                candidate.placed_parts,
                intent_plan,
                resolved_bboxes,
                fp_geometries,
                resolved_outline,
            )
            raw_score = score_placement(
                candidate.placed_parts,
                circuit,
                resolved_bboxes,
                outline=resolved_outline,
                keepouts=candidate_keepouts,
                cutouts=getattr(candidate_constraints, "cutouts", None),
                fp_geometries=fp_geometries,
                clearance_mm=clearance_mm,
                board_layers=board_layers,
                ctx=ctx,
            )
            edge_score = _apply_edge_intent_score(
                raw_score,
                candidate.placed_parts,
                resolved_bboxes,
                resolved_outline,
                intent_plan,
                constraints=candidate.constraints,
                fp_geometries=fp_geometries,
            )
            candidate_scores[candidate.name] = _apply_panel_mechanical_outline_score(
                edge_score,
                candidate.placed_parts,
                resolved_bboxes,
                resolved_outline,
                intent_plan,
                fp_geometries=fp_geometries,
            )
            candidate.score = candidate_scores[candidate.name].score

    def _finalize_candidate(candidate: PlacementCandidate) -> _FinalizedCandidate:
        nonlocal density_outline
        # Round-8 WS29: reuse the single params_early build (field-for-field
        # identical to the former per-call build; density_outline is the stable
        # pre-loop value on every path — plan hazard #7).
        finalized, density_outline = _finalize_candidate_impl(
            candidate, circuit, params_early, ctx, emit=_emit, progress=progress
        )
        return finalized

    # WS1: finalization is a pure function of a candidate's (post-refinement)
    # placement + constraints, so duplicates reuse the canonical's finalized
    # result with only the candidate identity swapped.
    _emit("finalizing candidates (snap / legalize / re-score)")
    finalized_candidates: dict[str, _FinalizedCandidate] = {}
    finalized_by_canonical: dict[str, _FinalizedCandidate] = {}

    # WS22: opt-in parallel finalize of the canonical candidates. Each finalize
    # is independent (plan hazard #2), so the canonicals run in the reused spawn
    # pool while the dup-reuse branch stays sequential and untouched. ANY failure
    # (or too few canonicals / no snapshot) -> `None` and the plain loop runs.
    # Byte-identical to the sequential default; `candidates` canonicals are
    # replaced in place with the worker-mutated objects (plan hazard #4).
    parallel_finalized = None
    if parallel_enabled:
        # Round-8 WS29: reuse the single params_early build (one build, all uses).
        parallel_finalized = _finalize_candidates_parallel(
            candidates,
            dup_canonical_name,
            resolved_workers,
            circuit,
            params_early,
            layout_snapshot,
            _emit,
        )

    for candidate in candidates:
        canonical_name = dup_canonical_name.get(candidate.name)
        if canonical_name is not None:
            base = finalized_by_canonical[canonical_name]
            base_candidate = base.candidate
            # The reused placement IS the canonical's, so adopt its full
            # placement explanation (including finalize-stage reasons like
            # edge snapping / passive gridding), keeping this candidate's own
            # dedup note so the reuse stays visible in the report.
            dedup_notes = [
                reason
                for reason in candidate.reasons
                if "refinement reused" in reason
            ]
            candidate.reasons = list(base_candidate.reasons) + dedup_notes
            candidate.ref_reasons = {
                ref: list(reasons)
                for ref, reasons in base_candidate.ref_reasons.items()
            }
            candidate.placed_parts = _clone_placed(base.placed_parts)
            candidate.constraints = base.constraints
            candidate.score = base.score.score
            finalized = _FinalizedCandidate(
                candidate=candidate,
                placed_parts=_clone_placed(base.placed_parts),
                outline=base.outline,
                constraints=base.constraints,
                validation=base.validation,
                score=base.score,
                keepouts=base.keepouts,
            )
        else:
            if parallel_finalized is not None:
                # `candidates[i]` was replaced in place with the worker's
                # mutated candidate, so `candidate` here IS that object and its
                # name keys the worker result.
                finalized = parallel_finalized[candidate.name]
            else:
                finalized = _finalize_candidate(candidate)
            finalized_by_canonical[candidate.name] = finalized
        finalized_candidates[candidate.name] = finalized
    candidate_validations = {
        name: finalized.validation
        for name, finalized in finalized_candidates.items()
    }
    candidate_scores = {
        name: finalized.score
        for name, finalized in finalized_candidates.items()
    }

    selected_final = max(
        finalized_candidates.values(),
        key=lambda finalized: (
            1 if finalized.score.ok else 0,
            # Prefer lower raw penalty (finer than the 0-clamped score, which
            # ties every legal placement at 0 on a dense board).
            -finalized.score.penalty,
            finalized.candidate.name,
        ),
    )
    selected_candidate = selected_final.candidate
    placed_parts = selected_final.placed_parts
    selected_constraints = selected_final.constraints
    resolved_outline = selected_final.outline
    validation = selected_final.validation
    score = selected_final.score
    _emit(
        f"selected '{selected_candidate.name}' "
        f"(ok={score.ok}, penalty={score.penalty:.1f}, hpwl={score.total_hpwl_mm:.0f}mm)"
    )
    power_plan = plan_power_routes(
        circuit,
        placed_parts,
        board_layers=board_layers,
        ctx=ctx,
    )
    candidate_validations[selected_candidate.name] = validation
    candidate_scores[selected_candidate.name] = score
    report = build_placement_report(
        selected_candidate,
        candidate_scores,
        candidate_validations,
        power_plan,
        routability=routability,
        intent_warnings=intent_plan.warnings if intent_plan is not None else None,
    )

    return LayoutResult(
        placed_parts=placed_parts,
        outline=resolved_outline,
        validation=validation,
        score=score,
        power_plan=power_plan,
        groups=groups,
        fp_bboxes=resolved_bboxes,
        candidates=candidates,
        intent_plan=intent_plan,
        report=report,
        fp_geometries=fp_geometries,
        routability=routability,
        cutouts=list(getattr(selected_constraints, "cutouts", []) or []),
    )
