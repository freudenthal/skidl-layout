from __future__ import annotations

from dataclasses import dataclass, field

from skidl.node import HIER_SEP

from .roles import is_nc_net


__all__ = ["PlacementGroup", "extract_groups"]


@dataclass
class PlacementGroup:
    name: str
    parts: list = field(default_factory=list)
    adjacency: dict = field(default_factory=dict)  # ref → {other_ref: shared_net_count}


def extract_groups(circuit) -> dict:
    """Group parts by their immediate subcircuit node.

    Returns dict mapping id(node) → PlacementGroup.
    Each part belongs to exactly one group (its immediate node).
    Adjacency is built by iterating nets: if two parts share a net, they are adjacent.
    """
    groups: dict[int, PlacementGroup] = {}

    for part in circuit.parts:
        node = getattr(part, "node", None)
        key = id(node) if node is not None else None
        if key not in groups:
            if node is not None:
                name = HIER_SEP.join(part.hiertuple)
            else:
                name = ""
            groups[key] = PlacementGroup(name=name)
        groups[key].parts.append(part)

    for net in circuit.nets:
        if is_nc_net(net):
            continue
        pins = net.get_pins()
        parts_on_net = {pin.part for pin in pins if pin.part is not None}
        if len(parts_on_net) < 2:
            continue
        for part in parts_on_net:
            ref = part.ref
            key = id(getattr(part, "node", None)) if getattr(part, "node", None) is not None else None
            if key not in groups:
                continue
            adj = groups[key].adjacency
            if ref not in adj:
                adj[ref] = {}
            for p in parts_on_net:
                if p is not part:
                    adj[ref][p.ref] = adj[ref].get(p.ref, 0) + 1

    return groups
