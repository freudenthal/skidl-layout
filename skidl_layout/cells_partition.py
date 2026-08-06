# -*- coding: utf-8 -*-
"""The templating sweep -- partition a netlist into cells, totally.

Construction-arc stage **S2**. The artifact this module produces answers, for a
*circuit with no geometry and no placement*, the question the construction loop
asks before it places anything: *"which parts belong together, and which part
anchors each group?"*

⛔⛔ **This is a LEAF module.** Nothing in the engine, the scorer or the refiner
imports it, and nothing consumes the partition yet -- S3 (the first construction
loop) and S5 (the board level) are the consumers. Same discipline as
:mod:`~skidl_layout.escape_map` and :mod:`~skidl_layout.route_session`: it is
never added to a package re-export the engine reaches, and
``test_cells_partition.py::test_cells_partition_is_a_leaf_no_module_imports_it``
says so. ⚠ That guard reads **import statements**, never the module name as a
substring -- a docstring that says a neighbour's name out loud is documentation
and this arc wants more of it, not less (standing finding 16).

The sweep, smallest structures first
------------------------------------

1. **Families** (:data:`~skidl_layout.cells_families.FAMILIES`) -- the four
   declared R/L/C topologies, matched as **kind-labelled** induced subgraphs.
   ⛔ :func:`~skidl_layout.cells_match.match_cell` cannot be reused for this: it
   labels pattern nodes by **footprint**, so it can only find an R-R divider
   built from the exact sizes in the cell cache. Only the *host* side is reused
   -- :func:`~skidl_layout.cells_match.circuit_graph`, unmodified, with plane
   nets already excluded.
2. **IC cells** -- one complex IC per group, never two, with its servants taken
   from :func:`~skidl_layout.power_roles.classify_power_roles` where a power
   stage exists and from plane-free adjacency where none does.
3. **The single-part-against-a-power-pin template** -- the one shape nothing in
   the machinery expressed. A part whose only connections are to plane nets has
   no plane-free neighbour and therefore cannot be in any *pair* template at
   all; this binds it to **one pad** of an anchor instead. ⭐ Measured over the
   nine-board corpus, that is 8-40 % of the parts on the *power* boards and
   28-49 % on the MCU boards -- corpus-wide, not an MCU special case.
4. **Banks, connectors and singletons** -- what makes the partition **total**.
   *A singleton cell is a legal cell*, and making it so is what turns the
   recursion into one loop that runs at three levels instead of a special-cased
   two-tier scheme.

⛔⛔ **What is consumed rather than re-derived, and one thing that cannot be.**
:func:`~skidl_layout.decaps._select_parent` is a **placement-time** function --
it filters candidate parents to already-*placed* parts and tie-breaks on a
measured pad distance -- so S2, which runs before any placement exists, cannot
call it. Its netlist-only half *is* consumed, by name:
:func:`~skidl_layout.roles.classify_parts`,
:func:`~skidl_layout.roles.decouples_declaration`, the three net regexes, and
``decaps._role_priority`` / ``_token_affinity`` / ``_part_pin_nets_by_number`` /
``_pads_for_net``. That is the whole scoring ladder **minus** the placed filter
and the distance tie-break, and the tie-break is replaced by a total key over
content.

⛔ **Determinism.** Every sort in this module is over content -- a ref, a sorted
ref tuple, a net name -- and never over arrival order. This stack has made the
arrival-order mistake three separate times, once placing differently on 3 of 6
boards, so the driver's gate ``TS4`` permutes ``circuit.parts`` and asserts the
serialised partition is byte-identical.

⛔ **Plane nets are excluded from every adjacency** (standing finding 5, reached
five independent ways). Without it the host is a near-complete graph and *every*
pair of resistors matches *every* divider. ⚠ The exclusion's cost is *recorded*
-- :attr:`Partition.meta` carries the degree histogram both ways -- but never
traded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .cells_families import FAMILIES, FamilySpec, chain_order, junction_net
from .cells_match import circuit_graph
from .decaps import (
    _pads_for_net,
    _part_pin_nets_by_number,
    _role_priority,
    _supply_ground_for_decap,
    _token_affinity,
)
from .power_roles import (
    CAP_SYMBOL_RE,
    DENY_SYMBOL_RE,
    IND_SYMBOL_RE,
    RES_SYMBOL_RE,
    _pin_count,
    _symbol_name,
    classify_devices,
    classify_power_roles,
)
from .ratnest import is_plane_net
from .roles import (
    DECAP_VALUE_RE,
    GND_NET_RE,
    POWER_NET_RE,
    classify_parts,
    decouples_declaration,
)

__all__ = [
    "ANCHOR_ROLES",
    "COMPLEX_IC_ROLES",
    "CONNECTOR_ROLES",
    "NEVER_A_SERVANT_ROLES",
    "ANCHOR_DEVICE_KINDS",
    "MAX_FAMILY_STATES",
    "NON_ANCHOR_DEVICE_KINDS",
    "STAGE_DEVICE_KINDS",
    "STAGE_DEVICE_KINDS_HANDLED_ELSEWHERE",
    "CellGroup",
    "Kind",
    "Partition",
    "PinBinding",
    "family_pattern_graph",
    "part_kind",
    "partition_circuit",
    "partition_to_dict",
]

Kind = Literal["family", "ic", "bank", "connector", "singleton"]

#: ⛔ Overview section 5.3 rule 2 -- *one complex IC per cell, and never two*.
#: This is the set that decides what "complex IC" means, and it is
#: :func:`~skidl_layout.roles.classify_part`'s own vocabulary rather than a
#: pin-count threshold or a reference-designator prefix.
COMPLEX_IC_ROLES = frozenset({"ic", "regulator", "module_socket"})

#: The roles that may anchor a group. Identical to :data:`COMPLEX_IC_ROLES`
#: today; named separately because the two questions ("may this part be an
#: anchor?" and "may two of these share a group?") are not the same question and
#: S5 will want the first one widened to a completed cell.
ANCHOR_ROLES = COMPLEX_IC_ROLES

#: ⛔ Roles that are never folded into somebody else's group as a servant.
#: A **connector** is excluded because the overview's section 10 puts connector
#: placement out of scope -- *"they arrive as fixed positions the loop must
#: respect, not as things it chooses"* -- so handing S3 an IC cell with a
#: connector inside it would hand it a member it may not move. A **mounting
#: hole** is excluded for the same reason.
NEVER_A_SERVANT_ROLES = frozenset({"connector", "mounting_hole", "panel_jack"})

#: The subset of the above that earns a ``kind="connector"`` group of its own.
CONNECTOR_ROLES = frozenset({"connector", "panel_jack"})

#: The bound on the family search, per (spec, circuit). ⛔ Reported, never
#: silently applied -- a matcher that quietly gives up reads as "no match here",
#: which is a different claim (``cells_match``'s own rule).
MAX_FAMILY_STATES = 20000

#: The device kinds a power stage hands to its controller's cell, in the order
#: they are collected.
#:
#: ⛔⛔⛔ **THE PLAN'S OWN LIST HAD A DEAD ENTRY AND IT WAS INVISIBLE FOR A
#: WHOLE RUN.** S2's §7.2 named ``switch / rectifier / magnetics /
#: **capacitor**``. MEASURED 2026-08-03 over the six power boards:
#: :func:`~skidl_layout.power_roles.classify_power_roles` **never emits
#: ``capacitor``** — :func:`~skidl_layout.power_roles._build_stage` overrides
#: :func:`~skidl_layout.power_roles._device_kind`'s generic answer with the
#: *role* the cap plays (``input_cap`` / ``output_cap`` / ``coupling_cap``), and
#: it also emits ``gate_resistor``, which the list omitted. The consequence was
#: silent: **six parts the classifier had explicitly assigned to a stage were
#: scattered to singletons** — ``COUT1``/``COUT2``/``COUT3``/``CDC1`` on
#: ``ltc1871_dual_sepic``, and ``CBIAS`` plus the gate resistor ``RG`` on
#: ``lt3758_iso_flyback``, the last of which the overview names by hand as
#: belonging to the IC cell.
#: ⭐⭐ **A filter that matches nothing is the observes-nothing defect wearing a
#: filter's clothes** (standing finding 1, and this is its sixth shape). The
#: guard is now mechanical rather than careful: :data:`STAGE_DEVICE_KINDS` and
#: :data:`STAGE_DEVICE_KINDS_HANDLED_ELSEWHERE` must between them **partition**
#: every kind the classifier emits, and gate ``TS2`` fails on a declared kind
#: that is never emitted or an emitted kind that is declared nowhere.
STAGE_DEVICE_KINDS = ("switch", "rectifier", "magnetics", "gate_resistor",
                      "input_cap", "output_cap", "coupling_cap")

#: The kinds that are deliberately **not** collected here, each because
#: something else already collects it. ⛔ Kept as a named constant rather than a
#: comment so the completeness guard above can be written at all: the union of
#: this and :data:`STAGE_DEVICE_KINDS` is what "we have considered every kind
#: `power_roles` can emit" means.
STAGE_DEVICE_KINDS_HANDLED_ELSEWHERE = (
    "controller",            # it IS the anchor
    "sense_resistor",        # collected via PowerStage.sense_resistor_ref
    "fb_divider_top",        # collected via PowerStage.feedback_divider
    "fb_divider_bottom",     # collected via PowerStage.feedback_divider
    "resistor",              # the generic fallback; reaches the group, if at
    "capacitor",             # all, through small_signal_refs
    "unknown",
)

#: ⛔⛔ Device kinds that can **never** anchor a group, however many pins the
#: part has. See the long comment in :func:`partition_circuit` -- a discrete
#: switching MOSFET has three pins, so ``roles.classify_part``'s last-resort
#: pin-count branch calls it an ``ic``, and rule 2 would then evict it from the
#: very cell the plan says it belongs to. ``power_roles`` types it from its pin
#: names, which is the more specific library fact, and that answer wins.
NON_ANCHOR_DEVICE_KINDS = frozenset({"switch", "rectifier", "magnetics",
                                     "capacitor", "resistor"})

#: ⛔⛔ Device kinds that **always** anchor a group, whatever
#: ``roles.classify_part`` says. See the long comment in
#: :func:`partition_circuit`: ``roles.PANEL_CONTROL_RE`` is a **substring** rule
#: over the part *description*, and every switching regulator's description
#: contains the word "switching" -- so on ``lt3844_buck`` the ``LT3844``
#: controller classifies as a panel ``control`` and would never anchor anything.
#: A part ``power_roles`` types ``controller`` from its **pin names** is a
#: controller.
ANCHOR_DEVICE_KINDS = frozenset({"controller"})


# --------------------------------------------------------------------------- #
# Value types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PinBinding:
    """One part bound to **one pad** of the group's anchor.

    ⭐ The shape the overview's section 5.4 says nothing in the machinery
    expresses. It is not a pair template: the served part has no plane-free
    neighbour at all, so the only structure available is *"this part sits
    against that pad"*.
    """

    ref: str                  # the served part (a decap, a bulk cap, a pull-up)
    anchor_pad: str           # the anchor's pad NUMBER it sits against
    net: str                  # the net they share
    role: str                 # "decap" | "bulk" | "pullup" | "series" | "other"
    reason: str               # which clause bound it

    def to_dict(self) -> dict:
        return {"ref": self.ref, "anchor_pad": self.anchor_pad,
                "net": self.net, "role": self.role, "reason": self.reason}


@dataclass(frozen=True)
class CellGroup:
    """One cell's worth of parts, and the part that anchors them."""

    name: str                 # ⛔ derived from content -- never a counter
    kind: str
    refs: tuple[str, ...]     # SORTED; every member, anchor included
    anchor: str | None        # the complex IC, or the family's junction member
    family: str | None        # the FamilySpec name when kind == "family"
    topology: str | None      # "junction" | "chain" | None
    bindings: tuple[PinBinding, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.refs)

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "refs": list(self.refs),
                "anchor": self.anchor, "family": self.family,
                "topology": self.topology,
                "bindings": [b.to_dict() for b in self.bindings],
                "reasons": list(self.reasons)}


@dataclass(frozen=True)
class Partition:
    """A **total** assignment of a circuit's parts to cells."""

    board: str
    groups: tuple[CellGroup, ...]
    parts_total: int
    unassigned: tuple[str, ...]        # ⛔ MUST be empty; kept so a gate SEES it
    meta: dict

    @property
    def templated_fraction(self) -> float:
        """Parts in a group of **2 or more** members.

        ⚠ **"Coverage" had to be redefined for a total partition**: it is 100 %
        by construction, because singletons absorb whatever is left. What is
        worth measuring is how much of the board acquired *structure*.
        """
        if not self.parts_total:
            return 0.0
        inside = sum(g.size for g in self.groups if g.size >= 2)
        return round(inside / self.parts_total, 4)

    @property
    def by_kind(self) -> dict:
        out: dict = {}
        for group in self.groups:
            out[group.kind] = out.get(group.kind, 0) + 1
        return dict(sorted(out.items()))

    @property
    def largest_group_fraction(self) -> float:
        if not self.parts_total:
            return 0.0
        return round(max((g.size for g in self.groups), default=0)
                     / self.parts_total, 4)

    @property
    def singleton_refs(self) -> tuple[str, ...]:
        return tuple(sorted(g.refs[0] for g in self.groups
                            if g.kind == "singleton"))

    def group_of(self, ref: str) -> CellGroup | None:
        for group in self.groups:
            if ref in group.refs:
                return group
        return None


def partition_to_dict(part: Partition) -> dict:
    """The serialised form. ⛔ Sorted throughout -- gate ``TS4`` diffs bytes."""
    return {
        "board": part.board,
        "parts_total": part.parts_total,
        "groups": [group.to_dict() for group in part.groups],
        "unassigned": list(part.unassigned),
        "templated_fraction": part.templated_fraction,
        "largest_group_fraction": part.largest_group_fraction,
        "by_kind": part.by_kind,
        "meta": part.meta,
    }


# --------------------------------------------------------------------------- #
# Kinds, from the SYMBOL and never from the reference designator
# --------------------------------------------------------------------------- #
def part_kind(part) -> str | None:
    """``"R"`` / ``"C"`` / ``"L"`` for a two-terminal passive, else ``None``.

    ⛔⛔ **A part's kind is a library fact, not a reference designator.**
    ``power_roles``' own comment says it best: matching the **symbol name** is
    still a library fact because, unlike the reference designator, the author
    does not choose it. So ``C12`` is never parsed to decide something is a
    capacitor, and the deny-list runs first -- an indicator ``LED`` has pins
    literally named ``K``/``A`` and is exactly the part a looser rule mistypes.
    """
    symbol = _symbol_name(part)
    if not symbol or DENY_SYMBOL_RE.match(symbol):
        return None
    if _pin_count(part) != 2:
        return None
    if RES_SYMBOL_RE.match(symbol):
        return "R"
    if CAP_SYMBOL_RE.match(symbol):
        return "C"
    if IND_SYMBOL_RE.match(symbol):
        return "L"
    return None


def family_pattern_graph(spec: FamilySpec) -> tuple[dict, dict]:
    """``(labels, adjacency)`` for a declared family, labelled by **kind**.

    ⛔ The whole reason :func:`~skidl_layout.cells_match.pattern_graph` cannot be
    reused: it labels a node by its member's **footprint**, so a divider is only
    a divider at the size the cached cell was built at. Here a divider is a
    divider at any size.
    """
    labels = {ref: kind for ref, kind in spec.parts}
    adjacency: dict = {ref: set() for ref in labels}
    for _net, pins in sorted(spec.nets.items()):
        refs = sorted({str(ref) for ref, _pad in pins})
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                adjacency[a].add(b)
                adjacency[b].add(a)
    return labels, {ref: frozenset(neigh) for ref, neigh in adjacency.items()}


def _spec_topology(spec: FamilySpec) -> str | None:
    """``"junction"`` / ``"chain"`` / ``None`` -- the declared reader's answer."""
    if junction_net(spec) is not None:
        return "junction"
    return "chain" if chain_order(spec) is not None else None


# --------------------------------------------------------------------------- #
# The kind-labelled induced search
# --------------------------------------------------------------------------- #
def _induced_embeddings(labels, adjacency, host_by_kind, host_adjacency,
                        *, max_states: int = MAX_FAMILY_STATES):
    """Every induced embedding of a kind-labelled pattern. ``(maps, abandoned)``.

    Same semantics as :func:`~skidl_layout.cells_match.match_cell`: a pattern
    **non**-edge must map to a host non-edge, so a family that says "these two
    parts touch each other and nothing else in the group" is never bound to
    three parts wired in a triangle. ⛔ Bounded, and the bound is *reported*.
    """
    order = sorted(labels, key=lambda r: (-len(adjacency[r]), labels[r], r))
    found: list[dict] = []
    seen: set = set()
    state = {"n": 0, "abandoned": False}

    def _search(index: int, mapping: dict, used: set):
        if state["abandoned"]:
            return
        if index == len(order):
            key = tuple(sorted(mapping.items()))
            if key not in seen:
                seen.add(key)
                found.append(dict(mapping))
            return
        pattern_ref = order[index]
        for host_ref in host_by_kind.get(labels[pattern_ref], ()):
            state["n"] += 1
            if state["n"] > max_states:
                state["abandoned"] = True
                return
            if host_ref in used:
                continue
            ok = True
            for other, other_host in mapping.items():
                connected = other in adjacency[pattern_ref]
                host_connected = other_host in host_adjacency.get(host_ref, ())
                if connected != host_connected:        # induced, both ways
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
    return found, state["abandoned"]


class _CanonicalCircuit:
    """``circuit`` with ``parts`` and ``get_nets()`` in a **total content order**.

    ⛔⛔⛔ **This exists because a function S2 consumes is arrival-order
    dependent, and it is the general answer to that shape of problem: if a
    consumed function is order-dependent, feed it a canonical order.**

    MEASURED 2026-08-03, twice. :func:`~skidl_layout.power_roles.classify_power_roles`
    reaches ``_View.order``, which is the **index of a part in
    ``circuit.parts``**, and ``_View.sorted_refs`` ranks on it — so on
    ``lt8710_sepic``, the one corpus board with **two switches on one drive
    net**, reversing ``circuit.parts`` flips the reported topology
    ``flyback`` → ``cuk``, the switch node ``SW1`` → ``SW2``, and whether a
    ``coupling_cap`` device exists at all.

    ⚠ **The first time this was found, S2 was insulated from it by luck** —
    ``coupling_cap`` was not a collected servant kind, so only *prose* moved.
    The moment that list was corrected (it had a dead entry and was dropping six
    typed parts), the drift reached **group membership** and gate ``TS4``
    failed on the partition itself. ⭐ **The luck running out is the argument
    for fixing it properly rather than for narrowing the servant list again.**

    ⛔ It does **not** fix ``power_roles`` for anyone else — that module is a
    gated non-negotiable, the defect is recorded as standing finding 17, and the
    four-character fix belongs to whoever is allowed to move it. What this
    guarantees is narrower and sufficient: **the partition is a deterministic
    function of the netlist's content**, whatever order its caller happens to
    hold the parts in.
    """

    def __init__(self, circuit):
        self._circuit = circuit
        self.parts = sorted(getattr(circuit, "parts", []) or [],
                            key=lambda part: str(getattr(part, "ref", "") or ""))
        #: ⚠ Nets are canonicalised too. ``_View`` reads ``get_nets()`` into
        #: ``net_order`` / ``net_index`` and power_roles tie-breaks on those, so
        #: leaving net order to the caller would leave half the door open.
        self._nets = sorted(getattr(circuit, "get_nets", list)() or [],
                            key=lambda net: str(getattr(net, "name", "") or ""))

    def get_nets(self):
        return self._nets

    def __getattr__(self, name):
        return getattr(self._circuit, name)


def _nets_by_ref_all(circuit) -> dict:
    """``{ref: {net names}}`` over **every** net, plane nets included.

    ⚠ Used for exactly two things and neither is an adjacency: the internal-net
    test below, and the report-only "both ways" degree histogram that makes the
    plane exclusion's cost visible.
    """
    out: dict = {}
    for part in getattr(circuit, "parts", []) or []:
        ref = str(getattr(part, "ref", "") or "")
        if not ref:
            continue
        out.setdefault(ref, set())
        for net in _part_pin_nets_by_number(part).values():
            out[ref].add(net)
    return out


def _parts_by_net(nets_by_ref: dict) -> dict:
    out: dict = {}
    for ref, nets in nets_by_ref.items():
        for net in nets:
            out.setdefault(net, set()).add(ref)
    return out


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def partition_circuit(circuit, *, fp_lib_dirs=None, board: str = "") -> Partition:
    """Partition ``circuit`` into cells. **Total** and deterministic.

    ⛔ Raises rather than returning a falsy result on a circuit with no parts
    (standing finding 1: an instrument that can observe nothing must raise --
    five instances in six runs).
    """
    parts = list(getattr(circuit, "parts", []) or [])
    by_ref = {}
    for part in parts:
        ref = str(getattr(part, "ref", "") or "")
        if ref:
            by_ref[ref] = part
    if not by_ref:
        raise ValueError(
            f"{board or 'circuit'}: zero parts -- an instrument that can "
            f"observe nothing must raise, not return an empty partition")

    #: ⛔⛔ **Everything downstream reads the CANONICAL view, never the caller's
    #: order.** ``circuit_graph`` and ``classify_parts`` are already
    #: order-independent (both key by ref and sort internally);
    #: ``classify_power_roles`` is **not**, measured twice. Canonicalising once
    #: here rather than at the one unstable call site is deliberate: it makes
    #: "the partition is a function of the netlist's content" a property of the
    #: function rather than a property of which call sites someone remembered.
    circuit = _CanonicalCircuit(circuit)

    labels_fp, adjacency, _nets_by_ref_free = circuit_graph(circuit,
                                                            plane_free=True)
    all_refs = sorted(by_ref)
    roles = classify_parts(circuit)
    kinds = {ref: part_kind(by_ref[ref]) for ref in all_refs}
    nets_all = _nets_by_ref_all(circuit)
    parts_on_net = _parts_by_net(nets_all)

    claimed: dict = {}                    # ref -> group name
    groups: list[CellGroup] = []
    meta: dict = {"board": board}

    def _role(ref: str) -> str:
        entry = roles.get(ref)
        return entry.role if entry is not None else "unknown"

    def _claim(group: CellGroup):
        for ref in group.refs:
            if ref in claimed:            # pragma: no cover -- bail-out 2
                raise ValueError(f"{board}: {ref} claimed twice "
                                 f"({claimed[ref]} and {group.name})")
            claimed[ref] = group.name
        groups.append(group)

    # ----------------------------------------------------------------- #
    # Step 1 -- the family templates, smallest structures first
    # ----------------------------------------------------------------- #
    host_by_kind: dict = {}
    for ref in all_refs:
        if kinds[ref]:
            host_by_kind.setdefault(kinds[ref], []).append(ref)
    for bucket in host_by_kind.values():
        bucket.sort()

    family_rows: list = []
    internal_rejections = 0
    abandoned: list = []
    #: ⛔ Largest pattern first, then by ``(family name, sorted refs)`` -- a
    #: total key over content. A three-member structure is more specific than
    #: any two-member one inside it, so it must claim first.
    for spec in sorted(FAMILIES, key=lambda s: (-len(s.parts), s.name)):
        labels, pattern_adj = family_pattern_graph(spec)
        embeddings, gave_up = _induced_embeddings(labels, pattern_adj,
                                                  host_by_kind, adjacency)
        if gave_up:
            abandoned.append(spec.name)
        #: ⭐ ``FamilySpec.internal`` is *declared* and is consumed rather than
        #: ignored: ``rc_snubber``'s ``SNUB`` "touches nothing else", so an
        #: embedding qualifies only if some host net is touched by **exactly**
        #: the mapped members. ⚠ Without this every RC compensation leg in the
        #: corpus reads as a snubber; the count it rejects is recorded below so
        #: the constraint's cost is visible rather than assumed.
        candidates = []
        for mapping in embeddings:
            refs = tuple(sorted(mapping.values()))
            if spec.internal:
                shared = [net for net, on in sorted(parts_on_net.items())
                          if on == set(refs)]
                if not shared:
                    internal_rejections += 1
                    continue
            candidates.append(refs)
        for refs in sorted(set(candidates)):
            if any(ref in claimed for ref in refs):
                continue
            topology = _spec_topology(spec)
            _claim(CellGroup(
                name=f"family:{spec.name}:{'-'.join(refs)}",
                kind="family", refs=refs, anchor=None, family=spec.name,
                topology=topology,
                reasons=(f"induced kind-labelled match of FamilySpec "
                         f"{spec.name!r} ({topology})",)))
            family_rows.append({"family": spec.name, "refs": list(refs),
                                "topology": topology})
    meta["families"] = {
        "matched": family_rows,
        "internal_net_rejections": internal_rejections,
        "abandoned_specs": sorted(abandoned),
        "specs_considered": sorted(spec.name for spec in FAMILIES),
    }

    # ----------------------------------------------------------------- #
    # Step 2 -- the IC cells
    # ----------------------------------------------------------------- #
    #: ⛔⛔ **Two classifiers disagree about what a "complex IC" is, and the
    #: more specific one wins.** MEASURED 2026-08-03 on ``lt3844_buck``:
    #: :func:`~skidl_layout.roles.classify_part`'s last-resort branch types
    #: *anything with more than two pins* as ``ic`` (its own reason string is
    #: "IC-like reference or pin count", at the ladder's **lowest** confidence,
    #: 0.75), so the stage's switching MOSFET ``M1`` came out an ``ic``. Rule 2
    #: then forbids it from the controller's cell -- while the very next clause
    #: of the plan lists ``devices`` of kind ``switch`` among that cell's
    #: servants. The two clauses contradict each other on every board with a
    #: discrete switch.
    #: ⭐ The resolution consumes the classifier the plan already says to
    #: consume: :func:`~skidl_layout.power_roles.classify_devices` types ``M1``
    #: as a ``switch`` from its **pin names** (the G/D/S triple, a library fact
    #: at confidence 1.0). A part typed as a power *device* is a device, not an
    #: anchor, whatever its pin count. Every demotion is recorded by name in
    #: ``meta["anchor_demoted_to_device"]`` -- this may not happen silently.
    #: ⛔⛔ **And it cuts the other way too, on the same board.**
    #: ``roles.PANEL_CONTROL_RE`` is a **substring** rule (``switch|button|
    #: potentiometer|...``) applied to ``_part_text``, which includes the part's
    #: *description* -- and a switching regulator's description contains the
    #: word "switching". MEASURED 2026-08-03: the ``LT3844`` on ``lt3844_buck``
    #: classifies as a panel ``control`` at confidence 0.85, before the
    #: ``regulator`` and ``ic`` branches are ever reached, so the board's only
    #: controller would never anchor anything and its cell would not exist. The
    #: ``1N4148W`` ("switching diode") goes the same way.
    #: ⭐ Same resolution, same principle: ``classify_devices`` types the part
    #: ``controller`` from its **pin names** at confidence 1.0, and that answer
    #: wins. Every promotion is recorded in ``meta["anchor_promoted_from_device"]``.
    device_kinds = classify_devices(circuit)
    demoted, promoted = [], []

    def _device_kind(ref: str) -> str:
        entry = device_kinds.get(ref)
        return entry.kind if entry is not None else "unknown"

    def _is_complex_ic(ref: str) -> bool:
        kind = _device_kind(ref)
        if kind in NON_ANCHOR_DEVICE_KINDS:
            return False
        if kind in ANCHOR_DEVICE_KINDS:
            return True
        return _role(ref) in COMPLEX_IC_ROLES

    for ref in all_refs:
        if _role(ref) in COMPLEX_IC_ROLES and not _is_complex_ic(ref):
            demoted.append(f"{ref}: roles.classify_part says "
                           f"{_role(ref)!r} but power_roles.classify_devices "
                           f"says {_device_kind(ref)!r} -- a power device, not "
                           f"an anchor")
        elif _role(ref) not in COMPLEX_IC_ROLES and _is_complex_ic(ref):
            promoted.append(f"{ref}: roles.classify_part says {_role(ref)!r} "
                            f"but power_roles.classify_devices says "
                            f"{_device_kind(ref)!r} from its pin names -- an "
                            f"anchor")
    meta["anchor_demoted_to_device"] = sorted(demoted)
    meta["anchor_promoted_from_device"] = sorted(promoted)

    anchors = [ref for ref in all_refs if _is_complex_ic(ref)]
    if not anchors:
        meta["anchors_none"] = True

    def _servant_ok(ref: str) -> str | None:
        """``None`` when ``ref`` may join a group, else why it may not."""
        if ref not in by_ref:
            return "not a part of this circuit"
        if ref in claimed:
            return f"already claimed by {claimed[ref]}"
        if _is_complex_ic(ref):
            return "a complex IC gets its own group (overview 5.3 rule 2)"
        if _role(ref) in NEVER_A_SERVANT_ROLES:
            return f"role {_role(ref)!r} is placed as a fixed position, not by "\
                   f"this loop"
        return None

    stage_plan = classify_power_roles(circuit)
    #: ⛔⛔⛔ **THE WARNING PROSE IS DELIBERATELY NOT COPIED INTO THE
    #: ARTIFACT, AND THE REASON IS A DEFECT UPSTREAM.** MEASURED 2026-08-03 by
    #: gate ``TS4``: permuting ``circuit.parts`` on ``lt8710_sepic`` changes
    #: ``classify_power_roles``' answer -- topology ``flyback`` -> ``cuk``,
    #: switch node ``SW1`` -> ``SW2``, and a ``coupling_cap`` device appears.
    #: The cause is exact: ``_switches_on`` (``power_roles.py:1009``) collects
    #: in ``view.refs_on(net)`` order, which is the **arrival** order, and
    #: ``_build_stage`` (``:1099``) then takes ``switches[0]`` -- so on the one
    #: board with **two switches on one drive net** (its own warning names the
    #: tie) the choice is decided by the order the parts were listed in.
    #: ⛔ Fixing it is out of S2's scope: ``power_roles`` is a gated
    #: non-negotiable and this plan consumes it. ⭐ What **is** S2's job is not
    #: to launder an unstable input into a deterministic-looking artifact. Only
    #: the fields verified stable are recorded here; the warning text is a
    #: *measurement*, and the driver publishes it in the ledger where a
    #: measurement belongs.
    #: ⛔ ``stage_device_kinds_emitted`` exists so gate ``TS2`` can check the
    #: servant list against what the classifier ACTUALLY emits rather than
    #: against what a plan said it would. A declared kind that never appears is
    #: a filter matching nothing; an emitted kind declared nowhere is a part
    #: silently dropped. Both are failures, and neither is visible from the
    #: partition's output alone.
    emitted: dict = {}
    for stage in stage_plan.stages:
        for dev in stage.devices:
            emitted[dev.kind] = emitted.get(dev.kind, 0) + 1
    meta["power_roles"] = {
        "stages": len(stage_plan.stages),
        "warning_count": len(stage_plan.warnings),
        "controllers": sorted(s.controller_ref for s in stage_plan.stages
                              if s.controller_ref),
        "stage_device_kinds_emitted": dict(sorted(emitted.items())),
    }

    stage_by_anchor = {}
    for stage in stage_plan.stages:
        if stage.controller_ref and stage.controller_ref in by_ref:
            stage_by_anchor.setdefault(stage.controller_ref, stage)

    skips: list = []

    def _collect(anchor: str, wanted, source: str):
        members, reasons = [], []
        for ref in wanted:
            if ref == anchor:
                continue
            why = _servant_ok(ref)
            if why is None and ref not in members:
                members.append(ref)
                reasons.append(f"{ref}: {source}")
            elif why is not None:
                skips.append(f"{anchor} <- {ref}: {why}")
        return members, reasons

    # ----------------------------------------------------------------- #
    # Step 2.4 -- the single-part-against-a-power-pin template
    # ----------------------------------------------------------------- #
    #: ⛔ The bindings are chosen **before** any anchor group is emitted, because
    #: the ladder that picks a binding's anchor is global (it ranks every anchor
    #: on the corpus of its pads) and must not depend on which anchor happened to
    #: be processed first. A binding whose part is nevertheless claimed by an
    #: earlier group is dropped and recorded -- never silently duplicated.
    geometries, unresolved = _anchor_geometries(by_ref, anchors, fp_lib_dirs)
    meta["unresolved_footprints"] = unresolved

    isolated = [ref for ref in all_refs if not adjacency.get(ref)]
    bindings_by_anchor: dict = {}
    pin_rows, unbound = [], []
    bank_candidates: dict = {}
    for ref in isolated:
        if ref in claimed:
            continue
        part = by_ref[ref]
        if _pin_count(part) != 2:
            unbound.append({"ref": ref, "why": "not a two-pin part"})
            continue
        role_name = _role(ref)
        if _is_complex_ic(ref) or role_name in NEVER_A_SERVANT_ROLES:
            unbound.append({"ref": ref, "why": f"role {role_name!r}"})
            continue
        pair = _supply_and_other(part)
        if pair is None:
            unbound.append({"ref": ref,
                            "why": "no POWER_NET_RE net on either pin"})
            continue
        supply, other, binding_role = pair
        if binding_role in ("decap", "bulk"):
            bank_candidates.setdefault((supply, other), []).append(ref)

        chosen = _choose_anchor(ref, part, supply, other, anchors, by_ref,
                                roles, geometries)
        if chosen is None:
            unbound.append({"ref": ref, "why": f"no anchor carries a pad on "
                                               f"{supply!r}"})
            continue
        anchor, anchor_pad, reason = chosen
        bindings_by_anchor.setdefault(anchor, []).append(
            PinBinding(ref=ref, anchor_pad=anchor_pad, net=supply,
                       role=binding_role, reason=reason))
        pin_rows.append({"ref": ref, "anchor": anchor, "pad": anchor_pad,
                         "net": supply, "role": binding_role,
                         "reason": reason})

    meta["pin_template"] = {
        "isolated_candidates": [r for r in isolated if r not in claimed],
        "bound": pin_rows,
        "unbound": unbound,
        "bound_count": len(pin_rows),
    }

    # -- the anchors, in a total order, each claiming as it is built ------- #
    #: ⛔ **Claimed incrementally, and this is not cosmetic.** MEASURED: with
    #: the groups built first and claimed afterwards, ``lt3844_buck``'s ``D1``
    #: landed in two of them and only the double-claim guard caught it. The
    #: order is total over content: a power-stage controller first (a typed
    #: classification beats an adjacency walk), then by role priority, then by
    #: the ref.
    stage_anchor_adjacent: dict = {}
    dropped_bindings: list = []
    anchor_order = sorted(
        anchors,
        key=lambda ref: (0 if ref in stage_by_anchor else 1,
                         -_role_priority(_role(ref)), ref))
    for anchor in anchor_order:
        stage = stage_by_anchor.get(anchor)
        if stage is not None:
            wanted: list = []
            wanted.extend(sorted(stage.small_signal_refs or ()))
            if stage.feedback_divider:
                wanted.extend(sorted(str(r) for r in stage.feedback_divider))
            if stage.sense_resistor_ref:
                wanted.append(str(stage.sense_resistor_ref))
            for kind in STAGE_DEVICE_KINDS:
                wanted.extend(sorted(dev.ref for dev in stage.devices
                                     if dev.kind == kind))
            #: ⛔⛔ **``stage.topology`` is NOT quoted in the reason**, and
            #: that is the same finding as the warning text above rather than a
            #: style choice: on ``lt8710_sepic`` a permutation of
            #: ``circuit.parts`` turns ``flyback`` into ``cuk``, and with the
            #: name in the prose the *whole artifact* stopped being
            #: byte-identical while not one group, member or binding moved.
            #: ⭐ The topology is a **measurement of an upstream classifier**;
            #: the driver's ledger publishes it, flagged. The artifact records
            #: what S2 did.
            members, reasons = _collect(anchor, wanted,
                                        "named by the power_roles stage plan")
            head = (f"anchor {anchor}: power_roles names it the controller of "
                    f"a power stage")
            #: ⚠ **Report-only, and it is a measurement S3 asked for**: which
            #: parts adjacent to a stage anchor the stage plan did *not* name. A
            #: power stage is a typed classification, not an adjacency walk, so
            #: the two disagree -- whether that matters is S3's question.
            stage_anchor_adjacent[anchor] = sorted(
                ref for ref in adjacency.get(anchor, ())
                if ref not in members and ref not in claimed and ref != anchor)
        else:
            # ⭐ What the three MCU boards need: ``classify_power_roles``
            # returns **zero** stages on all three, measured, so without this
            # clause step 2 contributes nothing there at all.
            #
            # ⚠ **A STATED READING of S2's §7.2 step 3, which says "on a board
            # with no stage".** This implements the narrower and, I think,
            # intended condition: *any anchor that did not receive a stage*,
            # whether or not some other anchor on the board did. It matters on
            # exactly one corpus board -- ``lt3758_iso_flyback``, where ``U1``
            # is a stage controller and ``U2``/``U3`` (the optocoupler and the
            # secondary-side reference across the galvanic barrier) are not.
            # Under the literal board-wide reading those two would get **no**
            # servants at all and their five members would scatter. ⛔ Recorded
            # here and in the run report as a deviation rather than left as a
            # silent choice.
            members, reasons = _collect(anchor, sorted(adjacency.get(anchor, ())),
                                        "plane-free adjacency to the anchor")
            head = (f"anchor {anchor}: role {_role(anchor)!r}, no power stage "
                    f"-- servants from plane-free adjacency")

        bindings = []
        for binding in sorted(bindings_by_anchor.get(anchor, ()),
                              key=lambda b: b.ref):
            if binding.ref in claimed:
                dropped_bindings.append(f"{anchor} <- {binding.ref}: "
                                        f"already claimed by "
                                        f"{claimed[binding.ref]}")
                continue
            bindings.append(binding)
        bindings = tuple(bindings)
        refs = tuple(sorted({anchor, *members, *(b.ref for b in bindings)}))
        all_reasons = tuple([head] + reasons
                            + [f"{b.ref}: {b.reason}" for b in bindings])
        _claim(CellGroup(name=f"ic:{anchor}", kind="ic", refs=refs,
                         anchor=anchor, family=None, topology=None,
                         bindings=bindings, reasons=all_reasons))
    meta["stage_anchor_adjacent_unclaimed"] = stage_anchor_adjacent
    meta["pin_template"]["dropped_because_claimed"] = sorted(dropped_bindings)
    meta["anchor_order"] = anchor_order

    # ----------------------------------------------------------------- #
    # Step 3 -- connectors, banks, singletons, and TOTALITY
    # ----------------------------------------------------------------- #
    #: ⚠ ``mounting_hole`` is in :data:`NEVER_A_SERVANT_ROLES` too but is not a
    #: connector; it falls through to a singleton, which is the right answer for
    #: a part with no electrical connection at all.
    for ref in all_refs:
        if ref in claimed or _role(ref) not in CONNECTOR_ROLES:
            continue
        _claim(CellGroup(
            name=f"connector:{ref}", kind="connector", refs=(ref,),
            anchor=ref, family=None, topology=None,
            reasons=(f"role {_role(ref)!r} -- a fixed position the loop must "
                     f"respect, not one it chooses (overview section 10)",)))

    #: ⚠ **The bank sweep is over every still-unclaimed capacitor, not only the
    #: plane-free-isolated ones**, and the plan's own wording is what says so:
    #: *"two or more **still-unclaimed** capacitors that share the same
    #: (supply, ground) net pair and found no anchor"*. MEASURED: restricting it
    #: to the pin template's degree-0 candidate set left ``ltc1871_dual_sepic``
    #: with **six** ``COUT*`` singletons -- they sit on ``VOUT1``/``VOUT2``,
    #: which ``is_plane_net`` does not match, so they form a plane-free clique
    #: with each other, are adjacent to no anchor, and fall out of the degree-0
    #: set for a reason that has nothing to do with whether they are a bank.
    unreadable = []
    for ref in all_refs:
        if ref in claimed or kinds.get(ref) != "C":
            continue
        pair = _supply_and_other(by_ref[ref])
        if pair is None:
            #: ⛔⛔ **Recorded loudly rather than dropped.** MEASURED over the
            #: nine-board corpus: ``roles.POWER_NET_RE`` is a list of named
            #: alternatives anchored ``^...$``, so it matches ``VCC`` and
            #: ``VOUT`` but **not** ``INTVCC`` (on 5 of 6 power boards),
            #: ``INTVEE``, ``VBIAS``, or ``ltc1871_dual_sepic``'s ``VOUT_P`` /
            #: ``VOUT_N`` -- whose six ``COUT*`` capacitors therefore have no
            #: readable (supply, ground) pair and stay singletons. ⚠ Not fixed
            #: here: that regex reaches ``plan_power_routes``, which runs inside
            #: ``score_placement``, so widening it can move a placement digest.
            #: It is S2's *output*, not S2's licence.
            unreadable.append({"ref": ref,
                               "nets": sorted(set(nets_all.get(ref, ())))})
            continue
        if pair[2] not in ("decap", "bulk"):
            continue
        key = (pair[0], pair[1])
        if ref not in bank_candidates.setdefault(key, []):
            bank_candidates[key].append(ref)
    meta["bank_unreadable_net_pair"] = sorted(unreadable,
                                              key=lambda row: row["ref"])

    bank_rows = []
    for pair in sorted(bank_candidates):
        members = tuple(sorted(r for r in bank_candidates[pair]
                               if r not in claimed))
        if len(members) < 2:
            continue
        supply, other = pair
        _claim(CellGroup(
            name=f"bank:{supply}+{other}", kind="bank", refs=members,
            anchor=None, family=None, topology=None,
            reasons=(f"{len(members)} unclaimed capacitors share "
                     f"({supply}, {other}) and found no anchor",)))
        bank_rows.append({"nets": [supply, other], "refs": list(members)})
    meta["banks"] = bank_rows

    for ref in all_refs:
        if ref in claimed:
            continue
        _claim(CellGroup(
            name=f"singleton:{ref}", kind="singleton", refs=(ref,),
            anchor=ref, family=None, topology=None,
            reasons=(f"nothing claimed it -- a singleton cell is a legal cell "
                     f"(overview section 3); role {_role(ref)!r}, plane-free "
                     f"degree {len(adjacency.get(ref, ()))}",)))

    unassigned = tuple(ref for ref in all_refs if ref not in claimed)
    meta["skips"] = sorted(skips)
    meta["degree_histogram_plane_free"] = _histogram(
        {ref: len(adjacency.get(ref, ())) for ref in all_refs})
    labels_all, adjacency_all, _n = circuit_graph(circuit, plane_free=False)
    meta["degree_histogram_all_nets"] = _histogram(
        {ref: len(adjacency_all.get(ref, ())) for ref in all_refs})
    meta["isolated_plane_free"] = list(isolated)
    meta["plane_nets"] = sorted({net for nets in nets_all.values()
                                 for net in nets if is_plane_net(net)})
    meta["roles"] = {ref: _role(ref) for ref in all_refs}
    #: ⭐ Both classifiers are published side by side, deliberately: a gate that
    #: wants to re-derive the anchor predicate independently (rather than read
    #: back the ``kind`` label this module wrote) needs both, and a guard that
    #: reads the field the code wrote is not a guard.
    meta["device_kinds"] = {ref: _device_kind(ref) for ref in all_refs
                            if _device_kind(ref) != "unknown"}
    meta["nets_by_ref"] = {ref: sorted(nets_all.get(ref, ()))
                           for ref in all_refs}
    meta["kinds"] = {ref: kinds[ref] for ref in all_refs if kinds[ref]}
    meta["footprints"] = {ref: labels_fp.get(ref, "") for ref in all_refs}

    ordered = tuple(sorted(groups, key=lambda g: (g.kind, g.refs)))
    return Partition(board=board, groups=ordered, parts_total=len(all_refs),
                     unassigned=unassigned, meta=meta)


# --------------------------------------------------------------------------- #
# The netlist-only half of ``decaps._select_parent``
# --------------------------------------------------------------------------- #
def _supply_and_other(part) -> tuple[str, str, str] | None:
    """``(supply net, other net, binding role)`` for a two-pin part, or ``None``.

    ⛔ The role vocabulary is the plan's: ``"pullup"`` when one net is a power
    net and the other is **not** a ground net; otherwise ``"decap"`` when
    :data:`~skidl_layout.roles.DECAP_VALUE_RE` matches the value and ``"bulk"``
    when it does not -- and ``"other"`` for anything that is not a capacitor at
    all, because calling a resistor across a rail a "bulk cap" would be a units
    error of exactly the kind this arc keeps finding.

    ⚠ :func:`~skidl_layout.decaps._supply_ground_for_decap` is consulted first
    and is authoritative when it answers -- it is the same precedence
    ``_select_parent`` uses, declaration included -- but it deliberately gates on
    the decap value regex, so the bulk ``CIN*``/``COUT*`` capacitors that are
    8-40 % of the *power* boards fall through to the direct net test.
    """
    declared = _supply_ground_for_decap(part)
    nets = _part_pin_nets_by_number(part)
    values = sorted(set(nets.values()))
    if declared is not None:
        supply, ground = declared
        return supply, ground, _cap_role(part)
    if len(values) != 2:
        return None
    supplies = [net for net in values if POWER_NET_RE.match(net)]
    if not supplies:
        return None
    supply = supplies[0]
    other = [net for net in values if net != supply]
    if not other:                                          # both pins one rail
        return None
    other_net = other[0]
    if GND_NET_RE.match(other_net):
        return supply, other_net, _cap_role(part)
    return supply, other_net, "pullup"


def _cap_role(part) -> str:
    if part_kind(part) != "C":
        return "other"
    value = str(getattr(part, "value", "") or "").strip()
    return "decap" if DECAP_VALUE_RE.match(value) else "bulk"


def _anchor_geometries(by_ref, anchors, fp_lib_dirs):
    """``({ref: FootprintGeometry}, [unresolved])`` for the anchors.

    ⛔⛔ Standing finding 6 -- report ``unresolved_footprints``, never guess. A
    reader that hands a **bare** name to the geometry loader resolves nothing and
    silently boxes the part in a 2 x 2 mm fallback that is *self-consistent*, so
    nothing downstream catches it. ⚠ Circuit-side footprints are usually already
    ``Library:Name``; they are resolved anyway.
    """
    from .cells import resolve_footprint_name
    from .geometry import load_footprint_geometries

    dirs = list(fp_lib_dirs or [])
    if not dirs:
        from .metrics import discover_footprint_dir

        root = discover_footprint_dir()
        dirs = [root] if root else []

    wanted, unresolved = {}, []
    for ref in anchors:
        bare = str(getattr(by_ref[ref], "footprint", "") or "")
        if not bare:
            unresolved.append(f"{ref}: no footprint declared")
            continue
        resolved = resolve_footprint_name(bare, dirs)
        if ":" not in str(resolved):
            unresolved.append(f"{ref}: {bare!r} unresolved")
            continue
        wanted[ref] = resolved
    loaded = load_footprint_geometries(set(wanted.values()), dirs)
    out = {}
    for ref, name in wanted.items():
        geometry = loaded.get(name)
        if geometry is None:
            unresolved.append(f"{ref}: {name!r} has no geometry")
            continue
        out[ref] = geometry
    return out, sorted(unresolved)


def _choose_anchor(ref, part, supply, other, anchors, by_ref, roles, geometries):
    """``(anchor ref, anchor pad, reason)`` -- the netlist-only ladder.

    ⛔⛔ **This mirrors** :func:`~skidl_layout.decaps._select_parent`'s scoring
    ladder and stops exactly where placement would begin. It keeps: the explicit
    ``decouples=`` declaration winning outright, the role priority, the count of
    the anchor's pads on the supply net, the ground-pad count, and the token
    affinity. It drops: the *placed* filter and ``_average_candidate_pad_distance``
    -- there is no placement here. ⛔ In place of the distance tie-break it uses
    **the lexicographically smallest anchor ref**, a total key over content.
    """
    declared = decouples_declaration(part)
    declared_ref = declared[0] if declared else None
    if declared_ref and declared_ref in geometries:
        pads = _pads_for_net(by_ref[declared_ref], geometries[declared_ref],
                             supply)
        if pads:
            return (declared_ref, str(pads[0].number),
                    f"explicit decouples= declaration names {declared_ref}")

    scored = []
    for anchor in anchors:
        geometry = geometries.get(anchor)
        if geometry is None or anchor == ref:
            continue
        anchor_part = by_ref[anchor]
        power_pads = _pads_for_net(anchor_part, geometry, supply)
        if not power_pads:
            continue
        other_pads = _pads_for_net(anchor_part, geometry, other)
        role_entry = roles.get(anchor)
        role_name = role_entry.role if role_entry is not None else "unknown"
        scored.append((
            -_role_priority(role_name),
            -len(power_pads),
            -len(other_pads),
            -_token_affinity(part, anchor_part),
            anchor,                       # ⛔ the total key over content
            str(power_pads[0].number),
            role_name,
            len(power_pads),
            len(other_pads),
        ))
    if not scored:
        return None
    best = min(scored)
    return (best[4], best[5],
            f"netlist-only _select_parent ladder: role {best[6]!r} "
            f"(priority {_role_priority(best[6])}), {best[7]} pad(s) on "
            f"{supply}, {best[8]} on {other}, affinity {-best[3]}; "
            f"ties broken on the smallest anchor ref")


def _histogram(degrees: dict) -> dict:
    out: dict = {}
    for value in degrees.values():
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))
