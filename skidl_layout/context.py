from __future__ import annotations

from dataclasses import dataclass, field

from .roles import (
    GND_NET_RE,
    POWER_NET_RE,
    PartRole,
    classify_parts,
    pin_net_names,
)


@dataclass
class LayoutContext:
    """Circuit-invariant data computed once and reused across candidates."""

    roles: dict[str, PartRole] = field(default_factory=dict)
    pin_nets: dict[str, list[str]] = field(default_factory=dict)
    net_refs: dict[str, list[str]] = field(default_factory=dict)
    power_nets: set[str] = field(default_factory=set)
    ground_nets: set[str] = field(default_factory=set)

    @staticmethod
    def from_circuit(circuit) -> LayoutContext:
        if circuit is None:
            return LayoutContext()

        roles = classify_parts(circuit)
        pin_nets_map: dict[str, list[str]] = {}
        net_refs_map: dict[str, list[str]] = {}
        power_nets: set[str] = set()
        ground_nets: set[str] = set()

        for part in circuit.parts:
            ref = getattr(part, "ref", None)
            if ref is None:
                continue
            nets = pin_net_names(part)
            pin_nets_map[ref] = nets
            for net_name in nets:
                net_refs_map.setdefault(net_name, [])
                if ref not in net_refs_map[net_name]:
                    net_refs_map[net_name].append(ref)
                if POWER_NET_RE.match(net_name):
                    power_nets.add(net_name)
                if GND_NET_RE.match(net_name):
                    ground_nets.add(net_name)

        return LayoutContext(
            roles=roles,
            pin_nets=pin_nets_map,
            net_refs=net_refs_map,
            power_nets=power_nets,
            ground_nets=ground_nets,
        )
