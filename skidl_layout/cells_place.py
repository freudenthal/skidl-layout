# -*- coding: utf-8 -*-
"""Placing cells -- the footprint masquerade, wired up (plan section WS-T3).

⭐⭐ **The seam, restated because it is the plan's single most important
finding.** ``fp_geometries`` is a plain ``dict[str, FootprintGeometry]`` threaded
through ``plan_layout -> candidates -> placer -> refinement -> validator ->
scorer``. Register a synthetic key (``"@cell:fb_divider#1"``) mapping to a
synthesised geometry whose ``body_bounds`` is the cell box and whose pads are the
escaping nets' exit points, put a matching pseudo-part in the circuit view, and
the cell becomes **one rigid body** the engine translates and rotates. Nothing
can shear it, because it *is* a single ``PlacedPart``.

What that buys, with **no change to ``refinement.py``'s hot loop and no change
to ``scoring.py`` at all**:

===========================================  =====================================
rigid translation + rotation                 it is one ``PlacedPart``
AABB overlap + clearance                     ``validator._placed_bounds``
outline containment                          ``validator._check_outline_violations``
orientation trials                           ``orientation.py``'s 90-degree pass
**MST crossings over real port positions**   ``scoring._placement_pad_points``
===========================================  =====================================

⛔⛔ **What it does NOT buy ON THE DEFAULT PATH, stated as a limitation rather
than discovered as a bug: HPWL is blind to a cell's rotation.**
``scoring._total_hpwl``, ``_weighted_hpwl`` and ``validator._compute_hpwl`` are
**centroid**-based by default, so a cell reports the same position for every net
it touches and rotating it moves no HPWL term. The plan's section 3.6 answer --
one precomputed point per escaping net -- is **computed and carried** on every
compiled cell (:attr:`~skidl_layout.cells.LayoutCell.hpwl_points`, gated for
rotation-invariance).

⭐⭐ **UPDATED 2026-08-01: those points now HAVE a consumer, opt-in.**
``plan_layout(hpwl_objective="pads")`` points both HPWL terms at
``scoring._placement_pad_points``, and a cell's synthetic ``fp_geometries``
entry already carries one pad per escaping net **at its escape point**
(:func:`~skidl_layout.cells.cell_pad_geometries`) -- so the section-3.6 points
are consumed with no cell-side work at all and a cell's rotation becomes
visible to HPWL. ⛔ **The default is still ``"centroid"``**, so everything
below still describes what an unflagged run does.

⭐ Why that split is the right order and not a shortcut: the plan's own risk 10.4
records that the metrics WS-T4 ranks on -- **vias, router effort, signal
crossings** -- are untouched by section 3.6, so **the bail-out verdict does not
depend on it**; HPWL only decides *candidate selection*. Consuming the points
means editing the one module that 1035 byte-identity tests gate, to change a
number that cannot change the verdict. With the points unconsumed, ``cells=None``
byte-identity is guaranteed **by construction** rather than by testing.
⚠ WS-T4 therefore reports HPWL **both ways**, which is exactly the control risk
10.4 asks for.

The circuit view
----------------
⭐ The substituted circuit is built by **snapshotting the live circuit and then
editing the snapshot** (:func:`substitute_cells`).
:class:`~skidl_layout.snapshot.SnapshotCircuit` already mirrors the exact
attribute surface the whole layout stack reads, is proven byte-identical through
it, and is what the parallel workers receive anyway -- so the cell binding
crosses the worker boundary by construction instead of needing an entry in
``__slots__`` and ``_SNAPSHOT_FIELDS``. ⛔ That is the plan's "snapshot hazard"
(section 3.5) answered by not creating it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .cells import (
    LayoutCell,
    cell_geometry,
    cell_pad_numbers,
    member_placed_parts,
)
from .writer import PlacedPart

__all__ = [
    "CELL_FIELD",
    "CellInstance",
    "canonical_order",
    "cell_fp_geometries",
    "declared_cell_refs",
    "declared_cells",
    "expand_cell_placements",
    "mark_cell",
    "resolve_cell_instances",
    "substitute_cells",
]

#: The prefix that marks a synthetic footprint key. ⭐ A character no KiCad
#: library name can contain, so ``footprint.startswith(CELL_PREFIX)`` is a safe
#: test anywhere in the stack.
CELL_PREFIX = "@cell:"


@dataclass(frozen=True)
class CellInstance:
    """One placement of one cell, bound to a circuit.

    ``ref_map`` binds the cell's local refs to circuit refs; ``net_map`` binds
    local nets to circuit nets (identity for a harvested cell, whose local names
    *are* the circuit's). ``ref`` is the pseudo-part's reference -- it must not
    collide with a real one, hence the ``@`` sigil.
    """

    cell: LayoutCell
    ref_map: Mapping[str, str] = field(default_factory=dict)
    net_map: Mapping[str, str] = field(default_factory=dict)
    index: int = 1

    @property
    def ref(self) -> str:
        return f"@CELL{self.index}"

    @property
    def footprint_key(self) -> str:
        return f"{CELL_PREFIX}{self.cell.name}#{self.index}"

    @property
    def member_refs(self) -> tuple[str, ...]:
        return tuple(sorted(self.ref_map.get(m.local_ref, m.local_ref)
                            for m in self.cell.part_members))

    def circuit_net(self, local_net: str) -> str:
        return str(self.net_map.get(local_net, local_net))


def cell_fp_geometries(instances: Sequence[CellInstance]) -> dict:
    """``{synthetic key: FootprintGeometry}`` for every instance."""
    return {inst.footprint_key: cell_geometry(inst.cell, inst.footprint_key)
            for inst in instances}


def _cell_circuit_class():
    """``SnapshotCircuit`` plus the two attributes the *parent* process reads.

    ⛔ MEASURED: ``plan_layout`` calls ``hierarchy.extract_groups(circuit)``,
    which reads ``circuit.nets`` -- an attribute :class:`SnapshotCircuit` does
    not have, because a worker never calls ``extract_groups`` (the parent does,
    on the live circuit, and passes the result down). A snapshot handed to
    ``plan_layout`` directly therefore hits a code path no worker ever hits.
    ⭐ Worth recording on its own: **the snapshot is not a drop-in for a live
    circuit at the ``plan_layout`` seam**, only at the worker seam.
    """
    from .snapshot import SnapshotCircuit

    class _CellCircuit(SnapshotCircuit):
        __slots__ = ()

        @property
        def nets(self):
            return self.get_nets()

    return _CellCircuit


def substitute_cells(circuit, instances: Sequence[CellInstance]):
    """A circuit view in which each cell's members are one pseudo-part.

    ⛔ **Only the escaping nets become pins.** An internal net exposed as a pin
    would contribute a phantom airwire to every crossing count and every HPWL
    term, on a net that never leaves the box.

    ⛔ A member ref bound by more than one instance raises: a part claimed twice
    is a matcher bug, and a greedy first-fit that silently double-claims would
    produce a placement whose parts are in two places.
    """
    from .snapshot import SnapshotCircuit, SnapshotPart, SnapshotPin
    from .snapshot import snapshot_circuit

    snap = circuit if isinstance(circuit, SnapshotCircuit) else snapshot_circuit(circuit)

    claimed: dict[str, str] = {}
    for inst in instances:
        for ref in inst.member_refs:
            if ref in claimed:
                raise ValueError(
                    f"{ref} is claimed by both {claimed[ref]} and "
                    f"{inst.footprint_key}; a part cannot be in two cells")
            claimed[ref] = inst.footprint_key

    nets_by_name = {net.name: net for net in snap.get_nets()}
    absorbed = set(claimed)

    parts = [part for part in snap.parts if getattr(part, "ref", None) not in absorbed]
    for inst in instances:
        pseudo = SnapshotPart(
            ref=inst.ref,
            name=inst.cell.name,
            value=inst.cell.name,
            foot=inst.footprint_key,
            footprint=inst.footprint_key,
            description=f"layout cell {inst.cell.name} ({inst.cell.digest})",
            hierarchy="",
            pin_len=len(inst.cell.escaping_nets),
        )
        numbers = cell_pad_numbers(inst.cell)
        for local_net in inst.cell.escaping_nets:
            net_name = inst.circuit_net(local_net)
            net = nets_by_name.get(net_name)
            if net is None:
                # A cell net the circuit does not carry: skip rather than
                # invent, and let the caller's binding check report it.
                continue
            pin = SnapshotPin(part=pseudo, num=numbers[local_net],
                              name=local_net, func=None)
            pin.net = net
            pseudo.pins.append(pin)
            net._pins.append(pin)
        parts.append(pseudo)

    # ⛔ Every pin of an absorbed member must leave its net, or the net still
    # believes it touches a part that is no longer in ``circuit.parts`` and
    # every ref-list traversal (``scoring._net_ref_lists``,
    # ``congestion._net_refs``) carries a ref with no position.
    for net in snap.get_nets():
        net._pins = [pin for pin in net.get_pins()
                     if getattr(getattr(pin, "part", None), "ref", None)
                     not in absorbed]

    return _cell_circuit_class()(parts, list(snap.get_nets()))


# --------------------------------------------------------------------------- #
# The declaration seam (cell-toolchain plan, WS-U6) -- ``mark_cell``
# --------------------------------------------------------------------------- #
#: The ``Part.fields`` key a cell declaration is stored under. ⭐ Same channel
#: ``power_escape.mark_escape_room`` and ``power_pads.mark_pad_clearance`` use,
#: for the same reason: ``fields`` is skidl's own per-part dict and it survives
#: ``Part.copy()``, so a declared part stays declared through a ``5 * U1``-style
#: replication.
#:
#: ⛔⛔ **It must also be listed in ``snapshot._SNAPSHOT_FIELDS``.** The executed
#: plan dodged the snapshot hazard because binding was explicit ``CellInstance``
#: objects; a declaration carried on a part does **not** dodge it. A worker
#: rebuilds its context from a snapshot, so an untracked field is silently lost
#: -- and parallelism engages only at >= 30 parts, so the loss would corrupt
#: exactly the biggest boards and leave the small ones perfect. That is the
#: shape of the ``crossing_objective`` defect that 1100 tests missed.
CELL_FIELD = "layout_cell"


def mark_cell(name, *parts, cell=None, nets=None, clear: bool = False) -> list[str]:
    """Declare that ``parts`` form the layout template ``name``.

    The producer-side half, written where the circuit is built::

        from skidl_eda import mark_cell

        RFB1 = Part("Device", "R", footprint=...)
        RFB2 = Part("Device", "R", footprint=...)
        mark_cell("fb_divider", RFB1, RFB2)               # resolve at layout time
        mark_cell("fb_divider", RFB1, RFB2, cell=digest)  # this exact artifact

    ⭐ **This is the point of the whole toolchain: the circuit says which
    templates it uses.** Everything before it inferred a grouping (the driver's
    ``propose_pair_cells``) or was handed one (explicit :class:`CellInstance`
    objects); a declaration is the author's own statement, it lives with the
    circuit, and it is what makes "which template combinations lead to success"
    a question about recorded data rather than about a driver's policy.

    Args:
        name: the template's name. Cells are resolved by ``(name, digest)``.
        *parts: skidl ``Part`` objects (or anything with a ``fields`` dict).
        cell: a cell **digest** in the cache, or a
            :class:`~skidl_layout.cells.LayoutCell` (its digest is taken and the
            object is stored alongside for callers that pass no cache).
            ``None`` means *"resolve by name at layout time"*.
        nets: optional ``{local net: circuit net}`` override. Omit it and the
            binding is derived from the match, which is what a harvested cell
            whose local names *are* the circuit's needs.
        clear: remove the declaration instead of adding it.

    Returns:
        The refs marked, in the order given. A part with no usable field store is
        skipped rather than raising -- and is absent from the return, which is
        how a caller detects it.
    """
    digest = ""
    payload_cell = None
    if cell is not None:
        if isinstance(cell, str):
            digest = str(cell)
        else:
            digest = str(getattr(cell, "digest", "") or "")
            payload_cell = cell
    value = {"name": str(name), "digest": digest,
             "nets": {str(k): str(v) for k, v in dict(nets or {}).items()}}

    marked: list[str] = []
    for part in parts:
        store = getattr(part, "fields", None)
        if store is None:
            try:
                part.fields = store = {}
            except Exception:                                  # noqa: BLE001
                continue
        try:
            if clear:
                store.pop(CELL_FIELD, None)
            else:
                store[CELL_FIELD] = dict(value)
        except Exception:                                      # noqa: BLE001
            continue
        if payload_cell is not None:
            # ⚠ Held OUTSIDE ``fields`` on purpose: the snapshot allowlist
            # carries small JSON-ish values across a process boundary, and a
            # whole ``LayoutCell`` in every part's snapshot would be paid for on
            # every pickle. A cache lookup by digest is the supported path; this
            # attribute is the convenience for a caller with no cache.
            try:
                part._layout_cell_object = payload_cell
            except Exception:                                  # noqa: BLE001
                pass
        ref = getattr(part, "ref", None)
        marked.append(str(ref) if ref is not None else "")
    return marked


def _declared_on(part):
    """This part's declaration dict, or ``None``.

    Reads the same holders the other ``mark_*`` helpers accept -- ``fields`` and
    ``_extra_fields`` -- because a part built in source and a snapshot part do
    not carry the same one.
    """
    for holder in ("fields", "_extra_fields"):
        store = getattr(part, holder, None)
        if isinstance(store, dict) and isinstance(store.get(CELL_FIELD), dict):
            return store[CELL_FIELD]
    return None


def declared_cell_refs(circuit) -> dict:
    """``{(name, digest): [refs, ...]}`` declared on ``circuit``, refs sorted."""
    groups: dict[tuple[str, str], list[str]] = {}
    for part in getattr(circuit, "parts", []) or []:
        declaration = _declared_on(part)
        if not declaration:
            continue
        ref = str(getattr(part, "ref", "") or "")
        if not ref:
            continue
        key = (str(declaration.get("name", "")), str(declaration.get("digest", "")))
        groups.setdefault(key, []).append(ref)
    return {key: sorted(refs) for key, refs in sorted(groups.items())}


def declared_cells(circuit, cache=None) -> tuple[list, list[str]]:
    """``(instances, unresolved)`` from a circuit's ``mark_cell`` declarations.

    Resolution order per declared group, and the order is the contract:

    1. an explicit **digest** in the declaration, looked up in ``cache``;
    2. the cell object a caller attached via ``mark_cell(cell=<LayoutCell>)``;
    3. the cache's cells with that **name**, smallest area first then digest.

    ⛔ **A group that resolves to nothing is REPORTED, never guessed at.** A
    declaration the library cannot honour must show up as a named string in
    ``unresolved`` -- silently placing those parts loose would make a board that
    declared a template and did not get one indistinguishable from one that
    never declared it.
    """
    from .cells_match import match_cell

    parts_by_ref = {str(getattr(p, "ref", "") or ""): p
                    for p in (getattr(circuit, "parts", []) or [])}
    instances: list[CellInstance] = []
    unresolved: list[str] = []
    index = 0
    for (name, digest), refs in declared_cell_refs(circuit).items():
        cell = None
        if digest and cache is not None:
            cell = cache.load(digest)
        if cell is None:
            for ref in refs:
                attached = getattr(parts_by_ref.get(ref), "_layout_cell_object",
                                   None)
                if attached is not None and (not digest
                                             or attached.digest == digest):
                    cell = attached
                    break
        if cell is None and cache is not None and not digest:
            named = sorted(cache.by_name(name),
                           key=lambda c: (c.area_mm2, c.digest))
            cell = named[0] if named else None
        if cell is None:
            unresolved.append(f"{name} ({digest or 'by name'}) on {refs}")
            continue

        member_refs = list(cell.member_refs)
        declaration = _declared_on(parts_by_ref[refs[0]]) or {}
        override = {str(k): str(v) for k, v in dict(declaration.get("nets") or {}).items()}
        if sorted(member_refs) == refs:
            ref_map = {ref: ref for ref in member_refs}
            net_map = override
        else:
            # The declared parts are not the cell's own refs (a generated or a
            # foreign-board cell): ask the matcher for the binding, restricted to
            # the declared parts by construction -- ``match_cell`` searches the
            # whole circuit, so the first binding whose refs are exactly this
            # group is the one meant.
            found, _note = match_cell(cell, circuit)
            chosen = next((m for m in found if list(m.refs) == refs), None)
            if chosen is None:
                unresolved.append(
                    f"{name} ({cell.digest}) declared on {refs} but no binding "
                    f"of its {len(member_refs)} member(s) matches those parts")
                continue
            ref_map = dict(chosen.ref_map)
            net_map = {**dict(chosen.net_map), **override}
        index += 1
        instances.append(CellInstance(cell=cell, ref_map=ref_map,
                                      net_map=net_map, index=index))
    return instances, unresolved


def canonical_order(instances: Sequence[CellInstance]) -> list:
    """Re-order and re-index a binding so it cannot depend on how it was built.

    ⛔⛔ **MEASURED 2026-07-31, and it is a real ordering dependency in the
    shipped masquerade.** :attr:`CellInstance.ref` is ``f"@CELL{index}"`` and
    :attr:`CellInstance.footprint_key` carries the same index, so the *order* a
    caller happens to list its cells in becomes part of the pseudo-parts'
    identity -- and refs are what the placer's tie-breaks read. Gate ``U6``
    caught it the first time it ran: the driver's harvest policy lists cells by
    **tightest pair first** while a ``mark_cell`` declaration resolves them
    **by name**, and on 3 of 6 boards the two orders placed differently while
    binding exactly the same parts to exactly the same cells.

    ⭐ The fix is to sort on **content** -- ``(cell name, cell digest, bound
    member refs)`` -- so any two callers who mean the same binding get the same
    pseudo-parts. This is the same class of defect as the ``id()``-ordering
    leaks in the renderer and in ``resolve_hpwl_points``' tie-break, and it gets
    the same answer: a total key over content, never over arrival order.

    ⚠ :func:`substitute_cells` deliberately does **not** do this. It is the raw
    primitive and it honours the caller's list exactly -- which is what keeps the
    executed layout-templates plan's recorded ``T3``/``T4`` digests
    reproducible byte for byte. Canonicalisation belongs to the *resolution*
    seam, where a declaration and an explicit list have to agree.
    """
    from dataclasses import replace as _replace

    ordered = sorted(instances,
                     key=lambda inst: (inst.cell.name, inst.cell.digest,
                                       inst.member_refs))
    return [_replace(inst, index=i) for i, inst in enumerate(ordered, start=1)]


def resolve_cell_instances(circuit, cells) -> tuple[list, list[str]]:
    """Normalise ``plan_layout(cells=...)`` into ``(instances, unresolved)``.

    Accepts, in this order: ``None``/``False`` (no cells), a sequence of
    :class:`CellInstance` (explicit binding), ``True`` (read declarations, no
    cache), a :class:`~skidl_layout.cells.CellCache`, or a path to one.

    ⭐ Every route ends in :func:`canonical_order`, so an explicit binding and a
    ``mark_cell`` declaration of the same cells are the **same placement
    problem** rather than two orderings of it.
    """
    from .cells import CellCache

    if not cells:
        return [], []
    if isinstance(cells, (list, tuple)):
        return canonical_order(cells), []
    if cells is True:
        instances, unresolved = declared_cells(circuit, None)
    elif isinstance(cells, CellCache):
        instances, unresolved = declared_cells(circuit, cells)
    elif isinstance(cells, (str, os.PathLike)):
        instances, unresolved = declared_cells(circuit, CellCache(str(cells)))
    else:
        raise TypeError(
            f"plan_layout(cells=...) does not accept {type(cells).__name__}")
    return canonical_order(instances), unresolved


def expand_cell_placements(placed_parts: Sequence[PlacedPart],
                           instances: Sequence[CellInstance],
                           ) -> list[PlacedPart]:
    """Replace each cell pseudo-part with its members' real ``PlacedPart``s.

    ⛔ **This runs at emission, before any digest is taken.** The canonical
    placement digest feeds on ``ref, x, y, rot, side`` and nothing else, so a
    digest over pseudo-parts is not comparable to the three recorded Phase-0
    goldens -- it would be a different set of refs entirely.
    """
    by_ref = {inst.ref: inst for inst in instances}
    out: list[PlacedPart] = []
    for placed in placed_parts:
        inst = by_ref.get(placed.ref)
        if inst is None:
            out.append(placed)
            continue
        out.extend(member_placed_parts(inst.cell, placed.x_mm, placed.y_mm,
                                       int(round(placed.rot_deg)) % 360,
                                       ref_map=inst.ref_map))
    return sorted(out, key=lambda p: p.ref)
