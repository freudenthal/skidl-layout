"""Alpha-annealed force relaxation for a placement seed (reverse transfer).

This is the REVERSE of the round-1/round-2 placement-scoring cross-pollination:
where the schematic placer borrowed skidl-layout's crossing/HPWL scorer, this
module ports the skidl *schematic* placer's ``push_and_pull`` alpha-continuation
(``skidl/src/skidl/schematics/place.py`` :1110-1178) into PlacedPart / mm space
as a deterministic post-transform for a layout candidate seed. The schedule
anneals ``alpha`` from 0 (all attractive net forces) to 1 (all repulsive part
overlap forces); starting attractive lets connected parts collapse together
before overlaps are resolved, which is a proven deterministic escape from the
greedy local minima the layout refinement can stall in.

Purity contract (mirrors ``sch_score``/``seed_place``/``relax_place``): pure
functions, no RNG, no wall-clock, no ``id()``-ordered iteration, and NO import of
scoring/engine internals — the caller passes the net topology in
(``net_ref_lists``) so the module stays trivially fake-testable and free of
import cycles. It is used ONLY when a candidate named ``alpha_relax`` is
explicitly requested; the default candidate set never calls it.
"""

from __future__ import annotations

import math
from dataclasses import replace

from .writer import PlacedPart  # noqa: F401  (type reference only)


# Ported verbatim from place.py:1110-1122 (the enabled rows only), as
# (speed, alpha, stability_coef). Starts all-attractive, ends all-repulsive.
_FORCE_SCHEDULE = [
    (0.50, 0.0, 0.1),
    (0.25, 0.0, 0.01),
    (0.25, 0.4, 0.1),
    (0.25, 0.8, 0.1),
    (0.25, 1.0, 0.01),
]

# Per-stage iteration cap. The schematic placer uses 1000; here 200 is enough
# for a seed that downstream refinement polishes, and bounds the request-only
# setup cost.
_MAX_STEPS_PER_STAGE = 200

# Extra clearance added to the summed half-dimensions when testing overlap.
_CLEARANCE_MM = 0.25


def _dims(pp, fp_bboxes):
    """(w, h) for a placed part, rotation-swapped like scoring._placement_bounds
    (scoring.py:498-500): swap w/h at 90 deg."""
    w, h = fp_bboxes.get(pp.footprint, (2.0, 2.0))
    if pp.rot_deg % 180 == 90:
        w, h = h, w
    return w, h


def alpha_relax_placement(
    placed_parts: list,
    net_ref_lists: list,
    fp_bboxes: dict,
    constraints=None,
) -> list:
    """Return a new placed-parts list with mm positions relaxed by the
    alpha-annealed continuation. Input order, refs, rotations and sides are
    preserved; only ``x_mm``/``y_mm`` change (rounded to 0.001 mm).

    Args:
        placed_parts: seed placement (e.g. the cluster-zone seed).
        net_ref_lists: ``[(net_name, [ref, ...]), ...]`` topology (from
            ``scoring._net_ref_lists``); positions are NOT pre-filtered.
        fp_bboxes: ``{footprint -> (w_mm, h_mm)}``.
        constraints: optional ``LayoutConstraints``; ``fixed`` refs exert forces
            but never move, and ``outline`` (if set) clamps every mobile ref.
    """
    # Positions in input order (dict preserves insertion order -> deterministic).
    pos: dict[str, list] = {}
    dims: dict[str, tuple] = {}
    for pp in placed_parts:
        pos[pp.ref] = [float(pp.x_mm), float(pp.y_mm)]
        dims[pp.ref] = _dims(pp, fp_bboxes)

    fixed_refs = {
        getattr(fp, "ref", None)
        for fp in (getattr(constraints, "fixed", None) or [])
    }
    fixed_refs.discard(None)
    outline = getattr(constraints, "outline", None)

    mobile_refs = [ref for ref in pos if ref not in fixed_refs]
    if not mobile_refs:
        return [
            replace(pp, x_mm=round(pos[pp.ref][0], 3), y_mm=round(pos[pp.ref][1], 3))
            for pp in placed_parts
        ]

    # Precompute, per ref, the list of OTHER placed members for each net it
    # touches (membership is position-independent). Nets with <2 placed members
    # contribute nothing.
    placed_set = set(pos)
    nets_for_ref: dict[str, list] = {ref: [] for ref in pos}
    for _name, refs in net_ref_lists:
        present = [s for s in refs if s in placed_set]
        if len(present) < 2:
            continue
        for r in present:
            others = [s for s in present if s != r]
            nets_for_ref[r].append(others)

    sorted_refs = sorted(pos)  # deterministic repulsion iteration order

    def attract(ref):
        contributing = nets_for_ref[ref]
        if not contributing:
            return 0.0, 0.0
        ax = ay = 0.0
        px, py = pos[ref]
        for others in contributing:
            n = len(others)
            cx = sum(pos[s][0] for s in others) / n
            cy = sum(pos[s][1] for s in others) / n
            ax += cx - px
            ay += cy - py
        m = len(contributing)
        return ax / m, ay / m

    def repulse(ref):
        rx = ry = 0.0
        wR, hR = dims[ref]
        xR, yR = pos[ref]
        for other in sorted_refs:
            if other == ref:
                continue
            wS, hS = dims[other]
            xS, yS = pos[other]
            dx = xR - xS
            dy = yR - yS
            pen_x = (wR / 2 + wS / 2 + _CLEARANCE_MM) - abs(dx)
            pen_y = (hR / 2 + hS / 2 + _CLEARANCE_MM) - abs(dy)
            if pen_x <= 0 or pen_y <= 0:
                continue
            depth = min(pen_x, pen_y)
            dist = math.hypot(dx, dy)
            if dist == 0.0:
                # Centers coincide: deterministic symmetry break (no RNG).
                rx += depth if ref < other else -depth
            else:
                rx += (dx / dist) * depth
                ry += (dy / dist) * depth
        return rx, ry

    # Scale factor between attractive and repulsive forces, at the start state
    # (mirrors place.scale_attractive_repulsive_forces).
    sum_attract = sum(math.hypot(*attract(ref)) for ref in mobile_refs)
    sum_repulse = sum(math.hypot(*repulse(ref)) for ref in mobile_refs)
    scale = sum_attract / sum_repulse if sum_repulse != 0.0 else 1.0

    rmv_drift = not fixed_refs
    n_mobile = len(mobile_refs)

    for base_speed, alpha, stability_coef in _FORCE_SCHEDULE:
        speed = base_speed
        stable_threshold = -1.0
        initial_sum = 0.0
        for _step in range(_MAX_STEPS_PER_STAGE):
            forces: dict[str, list] = {}
            sum_of_forces = 0.0
            for ref in mobile_refs:
                ax, ay = attract(ref)
                rx, ry = repulse(ref)
                fx = (1.0 - alpha) * ax + alpha * scale * rx
                fy = (1.0 - alpha) * ay + alpha * scale * ry
                forces[ref] = [fx, fy]
                sum_of_forces += math.hypot(fx, fy)

            if rmv_drift:
                # Remove net drift so mobile parts don't march in one direction
                # (place.py:1152-1159). Only when nothing is pinned.
                mx = sum(f[0] for f in forces.values()) / n_mobile
                my = sum(f[1] for f in forces.values()) / n_mobile
                for ref in mobile_refs:
                    forces[ref][0] -= mx
                    forces[ref][1] -= my

            for ref in mobile_refs:
                px = pos[ref][0] + forces[ref][0] * speed
                py = pos[ref][1] + forces[ref][1] * speed
                if outline is not None:
                    wR, hR = dims[ref]
                    lo_x, hi_x = outline.x_min + wR / 2, outline.x_max - wR / 2
                    if lo_x <= hi_x:
                        px = min(max(px, lo_x), hi_x)
                    lo_y, hi_y = outline.y_min + hR / 2, outline.y_max - hR / 2
                    if lo_y <= hi_y:
                        py = min(max(py, lo_y), hi_y)
                pos[ref][0] = px
                pos[ref][1] = py

            if stable_threshold < 0:
                initial_sum = sum_of_forces
                stable_threshold = sum_of_forces * stability_coef
            elif sum_of_forces <= stable_threshold:
                break
            elif sum_of_forces > 10 * initial_sum:
                speed *= 0.50

    return [
        replace(pp, x_mm=round(pos[pp.ref][0], 3), y_mm=round(pos[pp.ref][1], 3))
        for pp in placed_parts
    ]
