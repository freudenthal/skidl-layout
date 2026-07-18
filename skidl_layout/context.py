from __future__ import annotations

from dataclasses import dataclass, field

from .roles import (
    GND_NET_RE,
    POWER_NET_RE,
    PartRole,
    classify_parts,
    pin_net_names,
)


def _part_pin_count(part) -> int:
    """Pin count mirroring congestion._pin_count semantics exactly."""
    try:
        return len(part)
    except Exception:
        return len(getattr(part, "pins", []) or [])


@dataclass
class LayoutContext:
    """Circuit-invariant data computed once and reused across candidates."""

    roles: dict[str, PartRole] = field(default_factory=dict)
    pin_nets: dict[str, list[str]] = field(default_factory=dict)
    net_refs: dict[str, list[str]] = field(default_factory=dict)
    power_nets: set[str] = field(default_factory=set)
    ground_nets: set[str] = field(default_factory=set)
    # Net topology walked the same way as congestion._net_refs / scoring's
    # HPWL/crossings helpers: (net_name, deduped ref list) for nets touching
    # >=2 distinct placed-able refs. Built from circuit.get_nets() (NOT from
    # pin_net_names) so it is byte-equivalent to the live traversal.
    net_ref_lists: list[tuple[str, list[str]]] = field(default_factory=list)
    pin_counts: dict[str, int] = field(default_factory=dict)  # ref -> pin count

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

        pin_counts = {
            ref: _part_pin_count(part)
            for part in circuit.parts
            if (ref := getattr(part, "ref", None)) is not None
        }

        return LayoutContext(
            roles=roles,
            pin_nets=pin_nets_map,
            net_refs=net_refs_map,
            power_nets=power_nets,
            ground_nets=ground_nets,
            net_ref_lists=_build_net_ref_lists(circuit),
            pin_counts=pin_counts,
        )


def _build_net_ref_lists(circuit) -> list[tuple[str, list[str]]]:
    """Walk circuit.get_nets() exactly as congestion._net_refs does, minus the
    per-placement filter (positions vary; topology does not)."""
    if circuit is None:
        return []
    try:
        from skidl.net import NCNet
    except Exception:
        NCNet = None

    result: list[tuple[str, list[str]]] = []
    for net in circuit.get_nets():
        if NCNet is not None and isinstance(net, NCNet):
            continue
        name = str(getattr(net, "name", "") or "")
        refs: list[str] = []
        for pin in net.get_pins():
            ref = getattr(getattr(pin, "part", None), "ref", None)
            if ref is not None and ref not in refs:
                refs.append(ref)
        if len(refs) >= 2:
            result.append((name, refs))
    return result
