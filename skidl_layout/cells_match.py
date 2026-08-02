# -*- coding: utf-8 -*-
"""Finding a cell in a netlist (cell-toolchain plan, WS-U7 / the plan's WS-T8).

⭐⭐ **What matching is FOR in the reframed plan, and it is not placement.** A
matcher's obvious job is "bind this cached cell to those parts so the placer can
drop it in whole". Its *useful* job right now is **coverage measurement**: *"how
much of this board could be templated from the library we have?"* is a number
nothing else in the stack can produce, it is meaningful even where no matched
cell is applied, and it is exactly the kind of column the combinations ledger
needs before anyone can ask which templates work.

The model, kept small on purpose
--------------------------------
* **Pattern graph** -- a cell's part members are nodes labelled by footprint; an
  edge joins two members that share a local net.
* **Host graph** -- the circuit's parts are nodes labelled by footprint; an edge
  joins two parts that share a **non-plane** net.

⛔⛔ **Plane nets are excluded from the host's edges, and without that the
matcher is worthless.** Every part on these boards touches GND, so a plain
"shares a net" adjacency makes the host a near-complete graph and *every* pair
of resistors matches *every* two-resistor cell. This is the third time the same
diagnosis has been made in this codebase -- ``scoring._estimate_crossings``,
then ``propose_pair_cells``, now here -- and it has the same fix each time.

* Matching is **induced**: a pattern *non*-edge must map to a host non-edge. A
  cell that says "these two parts touch each other and nothing else in the
  group" must not be bound to three parts wired in a triangle.

⛔ **Bounded, and the bound is stated.** VF2 runs on patterns of at most
:data:`MAX_PATTERN_NODES` nodes, and the search abandons a cell after
:data:`MAX_STATES` states. Both are reported per cell
(:attr:`MatchReport.abandoned`) rather than silently truncating -- a matcher
that quietly gives up reads as "no match here", which is a different claim.

**Determinism**: nodes are visited in sorted order, candidate hosts in sorted
order, and matches are returned sorted by ``(cell digest, host refs)``. Greedy
first-fit claims parts in that order and never claims one twice
(``substitute_cells`` raises on a double claim, so a non-deterministic matcher
would surface as an intermittent exception rather than as a bad placement).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = [
    "MAX_PATTERN_NODES",
    "MAX_STATES",
    "CellMatch",
    "MatchReport",
    "cell_signature",
    "circuit_graph",
    "coverage",
    "greedy_bind",
    "index_by_signature",
    "match_cell",
    "pattern_graph",
]

#: ⛔ The plan's own bound. A pattern above this is skipped, loudly.
MAX_PATTERN_NODES = 8

#: States explored before a single (cell, circuit) search gives up.
MAX_STATES = 20000


# --------------------------------------------------------------------------- #
# Graphs
# --------------------------------------------------------------------------- #
def pattern_graph(cell) -> tuple[dict[str, str], dict[str, frozenset], dict]:
    """``(labels, adjacency, nets)`` for a cell's part members."""
    labels = {m.local_ref: str(m.footprint) for m in cell.part_members}
    refs_by_net: dict[str, set[str]] = {}
    for pad in cell.pads:
        if pad.local_net and pad.local_ref in labels:
            refs_by_net.setdefault(pad.local_net, set()).add(pad.local_ref)
    adjacency: dict[str, set[str]] = {ref: set() for ref in labels}
    for refs in refs_by_net.values():
        ordered = sorted(refs)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                adjacency[a].add(b)
                adjacency[b].add(a)
    return (labels, {ref: frozenset(neigh) for ref, neigh in adjacency.items()},
            {net: tuple(sorted(refs)) for net, refs in sorted(refs_by_net.items())})


def circuit_graph(circuit, *, plane_free: bool = True
                  ) -> tuple[dict[str, str], dict[str, frozenset], dict]:
    """``(labels, adjacency, nets_by_ref)`` for a live or snapshot circuit."""
    from .ratnest import is_plane_net

    labels: dict[str, str] = {}
    nets_by_ref: dict[str, set[str]] = {}
    refs_by_net: dict[str, set[str]] = {}
    for part in getattr(circuit, "parts", []) or []:
        ref = str(getattr(part, "ref", "") or "")
        if not ref:
            continue
        labels[ref] = str(getattr(part, "footprint", "") or "")
        nets_by_ref.setdefault(ref, set())
        for pin in getattr(part, "pins", []) or []:
            net = getattr(pin, "net", None)
            name = str(getattr(net, "name", "") or "")
            if not name:
                continue
            if plane_free and is_plane_net(name):
                continue
            nets_by_ref[ref].add(name)
            refs_by_net.setdefault(name, set()).add(ref)
    adjacency: dict[str, set[str]] = {ref: set() for ref in labels}
    for refs in refs_by_net.values():
        ordered = sorted(refs)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                adjacency[a].add(b)
                adjacency[b].add(a)
    return (labels, {ref: frozenset(neigh) for ref, neigh in adjacency.items()},
            {ref: frozenset(nets) for ref, nets in nets_by_ref.items()})


def cell_signature(cell) -> str:
    """A cheap, canonical index key: footprints + the sorted degree sequence.

    ⭐ Two cells with different signatures cannot match the same parts, so the
    signature prunes the cache before VF2 runs. It is deliberately *coarse* --
    it never rejects a real match, it only skips impossible ones.
    """
    labels, adjacency, _nets = pattern_graph(cell)
    footprints = ",".join(sorted(labels.values()))
    degrees = ",".join(str(len(adjacency[ref])) for ref in sorted(
        adjacency, key=lambda r: (len(adjacency[r]), labels[r], r)))
    return f"{len(labels)}|{footprints}|{degrees}"


def index_by_signature(cells: Sequence) -> dict[str, tuple]:
    """``{signature: (cell, ...)}``, every bucket sorted by digest."""
    buckets: dict[str, list] = {}
    for cell in cells:
        buckets.setdefault(cell_signature(cell), []).append(cell)
    return {sig: tuple(sorted(items, key=lambda c: c.digest))
            for sig, items in sorted(buckets.items())}


# --------------------------------------------------------------------------- #
# The bounded VF2
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CellMatch:
    """One induced-subgraph binding of a cell into a circuit."""

    cell_name: str
    cell_digest: str
    ref_map: Mapping[str, str]          # local ref -> circuit ref
    net_map: Mapping[str, str]          # local net -> circuit net

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(sorted(self.ref_map.values()))

    def to_dict(self) -> dict:
        return {"cell": self.cell_name, "digest": self.cell_digest,
                "ref_map": dict(self.ref_map), "net_map": dict(self.net_map),
                "refs": list(self.refs)}


@dataclass
class MatchReport:
    """What the matcher did to one circuit, bounds included."""

    parts_total: int = 0
    parts_matched: int = 0
    matches: list = field(default_factory=list)
    bound_skipped: list = field(default_factory=list)
    abandoned: list = field(default_factory=list)
    cells_considered: int = 0

    @property
    def coverage(self) -> float:
        return (0.0 if not self.parts_total
                else round(self.parts_matched / self.parts_total, 4))

    def to_dict(self) -> dict:
        return {"parts_total": self.parts_total,
                "parts_matched": self.parts_matched,
                "coverage": self.coverage,
                "cells_considered": self.cells_considered,
                "matches": [m.to_dict() for m in self.matches],
                "bound_skipped": list(self.bound_skipped),
                "abandoned": list(self.abandoned)}


def _net_binding(cell, ref_map, host_nets_by_pin) -> dict[str, str] | None:
    """Local net -> circuit net, or ``None`` if the cell's nets do not agree.

    ⛔ The join is on **pad numbers**, the same key ``scoring._placement_pad_points``
    uses -- a cell's pad ``"2"`` must land on the circuit pin ``"2"`` of the part
    it was bound to, or the arrangement is being applied to a differently-wired
    part that merely has the same footprint.
    """
    binding: dict[str, str] = {}
    for pad in cell.pads:
        if not pad.local_net or pad.local_ref not in ref_map:
            continue
        host_net = host_nets_by_pin.get((ref_map[pad.local_ref], pad.pad))
        if host_net is None:
            return None
        current = binding.setdefault(pad.local_net, host_net)
        if current != host_net:
            return None
    # ⛔ Injective: two distinct local nets must not collapse onto one circuit
    # net, or the cell's crossing structure is not the circuit's.
    if len(set(binding.values())) != len(binding):
        return None
    return binding


def match_cell(cell, circuit, *, host=None, host_nets_by_pin=None,
               max_states: int = MAX_STATES,
               max_nodes: int = MAX_PATTERN_NODES,
               ) -> tuple[list[CellMatch], str]:
    """Every induced binding of ``cell`` into ``circuit``. ``(matches, note)``.

    ``note`` is ``""``, ``"bound: N nodes"`` or ``"abandoned at N states"`` --
    the bound is always reported, never silently applied.
    """
    labels, adjacency, _pnets = pattern_graph(cell)
    order = sorted(labels, key=lambda r: (-len(adjacency[r]), labels[r], r))
    if not order:
        return [], "empty pattern"
    if len(order) > max_nodes:
        return [], f"bound: {len(order)} nodes > {max_nodes}"

    if host is None:
        host = circuit_graph(circuit)
    host_labels, host_adjacency, _hnets = host
    if host_nets_by_pin is None:
        host_nets_by_pin = _host_pin_nets(circuit)

    by_label: dict[str, list[str]] = {}
    for ref, footprint in host_labels.items():
        by_label.setdefault(footprint, []).append(ref)
    for refs in by_label.values():
        refs.sort()

    matches: list[CellMatch] = []
    seen: set[tuple] = set()
    states = 0
    abandoned = False

    def _search(index: int, mapping: dict[str, str], used: set[str]):
        nonlocal states, abandoned
        if abandoned:
            return
        if index == len(order):
            key = tuple(sorted(mapping.items()))
            if key in seen:
                return
            seen.add(key)
            net_map = _net_binding(cell, mapping, host_nets_by_pin)
            if net_map is None:
                return
            matches.append(CellMatch(cell_name=cell.name,
                                     cell_digest=cell.digest,
                                     ref_map=dict(mapping), net_map=net_map))
            return
        pattern_ref = order[index]
        for host_ref in by_label.get(labels[pattern_ref], ()):
            states += 1
            if states > max_states:
                abandoned = True
                return
            if host_ref in used:
                continue
            ok = True
            for other, other_host in mapping.items():
                connected = other in adjacency[pattern_ref]
                host_connected = other_host in host_adjacency.get(host_ref, ())
                if connected != host_connected:      # induced, both directions
                    ok = False
                    break
            if not ok:
                continue
            mapping[pattern_ref] = host_ref
            used.add(host_ref)
            _search(index + 1, mapping, used)
            used.discard(host_ref)
            del mapping[pattern_ref]

    _search(0, {}, set())
    matches.sort(key=lambda m: (m.cell_digest, m.refs))
    note = f"abandoned at {max_states} states" if abandoned else ""
    return matches, note


def _host_pin_nets(circuit) -> dict[tuple[str, str], str]:
    """``{(ref, pad number): net name}`` for a live or snapshot circuit."""
    out: dict[tuple[str, str], str] = {}
    for part in getattr(circuit, "parts", []) or []:
        ref = str(getattr(part, "ref", "") or "")
        for pin in getattr(part, "pins", []) or []:
            net = getattr(pin, "net", None)
            name = str(getattr(net, "name", "") or "")
            if name:
                out[(ref, str(getattr(pin, "num", "")))] = name
    return out


# --------------------------------------------------------------------------- #
# Greedy first-fit + coverage
# --------------------------------------------------------------------------- #
def greedy_bind(cells: Sequence, circuit, *, max_states: int = MAX_STATES,
                max_nodes: int = MAX_PATTERN_NODES) -> MatchReport:
    """Bind as many cells as fit, claiming no part twice. ⭐ The coverage number.

    Cells are tried **largest pattern first, then by digest** -- a bigger
    template says more about the board, and the digest makes the order total.
    """
    host = circuit_graph(circuit)
    pin_nets = _host_pin_nets(circuit)
    report = MatchReport(parts_total=len(host[0]))
    ordered = sorted(cells, key=lambda c: (-len(c.part_members), c.digest))
    claimed: set[str] = set()
    for cell in ordered:
        report.cells_considered += 1
        found, note = match_cell(cell, circuit, host=host,
                                 host_nets_by_pin=pin_nets,
                                 max_states=max_states, max_nodes=max_nodes)
        if note.startswith("bound:"):
            report.bound_skipped.append(f"{cell.name} ({cell.digest}): {note}")
            continue
        if note:
            report.abandoned.append(f"{cell.name} ({cell.digest}): {note}")
        for match in found:
            if claimed & set(match.refs):
                continue
            claimed.update(match.refs)
            report.matches.append(match)
    report.parts_matched = len(claimed)
    return report


def coverage(cells: Sequence, circuit, **kwargs) -> dict:
    """``greedy_bind`` reduced to the ledger's coverage columns."""
    return greedy_bind(cells, circuit, **kwargs).to_dict()
