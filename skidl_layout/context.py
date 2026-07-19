from __future__ import annotations

from dataclasses import dataclass, field

from .roles import (
    GND_NET_RE,
    POWER_NET_RE,
    PartRole,
    _part_tokens,
    classify_parts,
    is_nc_net,
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
    # ref -> _part_tokens(part), the owner-affinity token set used by scoring's
    # _role_warnings. Circuit-invariant (derived from ref/name/value/footprint).
    part_tokens: dict[str, set[str]] = field(default_factory=dict)
    # Round-9 WS33: per-net pin->ref walk for validator._compute_hpwl,
    # cached because connectivity never changes during a plan. For each
    # non-NC net in circuit.get_nets() ORDER: (net.name, [ref for each pin
    # in net.get_pins() order — duplicates KEPT, pins without a part ref
    # dropped]). None (the default) means "not built" — validate falls back
    # to the live walk (hand-built LayoutContext() test fakes hit this).
    hpwl_net_pins: list[tuple[str, list[str]]] | None = None
    # Lazy memos for plan_power_routes' circuit-invariant inputs. Built on
    # first use (not in from_circuit) so non-power users pay nothing. The
    # cached objects are shared across PowerRoutePlan results — read-only by
    # contract (callers must not mutate them).
    power_topology: object | None = None
    power_nets_by_layers: dict[int, list] = field(default_factory=dict)

    def power_nets_for(self, circuit, board_layers: int):
        if board_layers not in self.power_nets_by_layers:
            from .power import identify_power_nets

            self.power_nets_by_layers[board_layers] = identify_power_nets(
                circuit, board_layers=board_layers
            )
        return self.power_nets_by_layers[board_layers]

    def power_topology_for(self, circuit):
        if self.power_topology is None:
            from .power import infer_power_topology

            self.power_topology = infer_power_topology(circuit)
        return self.power_topology

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

        part_tokens = {
            ref: _part_tokens(part)
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
            part_tokens=part_tokens,
            hpwl_net_pins=_build_hpwl_net_pins(circuit),
        )


def _build_hpwl_net_pins(circuit) -> list[tuple[str, list[str]]]:
    """Walk circuit.get_nets() exactly as validator._compute_hpwl does,
    minus the placement filter (positions vary; connectivity does not).
    Pin-level, order-preserved, duplicates kept (plan hazard #4)."""
    if circuit is None:
        return []
    result: list[tuple[str, list[str]]] = []
    for net in circuit.get_nets():
        if is_nc_net(net):
            continue
        pin_refs = [
            ref
            for pin in net.get_pins()
            if (ref := getattr(getattr(pin, "part", None), "ref", None))
        ]
        result.append((net.name, pin_refs))
    return result


def _build_net_ref_lists(circuit) -> list[tuple[str, list[str]]]:
    """Walk circuit.get_nets() exactly as congestion._net_refs does, minus the
    per-placement filter (positions vary; topology does not)."""
    if circuit is None:
        return []

    result: list[tuple[str, list[str]]] = []
    for net in circuit.get_nets():
        if is_nc_net(net):
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
