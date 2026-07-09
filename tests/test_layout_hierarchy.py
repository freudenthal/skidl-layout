from __future__ import annotations

import pytest
from skidl import Circuit, Net, Part, subcircuit

from skidl_layout.hierarchy import PlacementGroup, extract_groups


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_part(value="1k", footprint="Resistor_SMD:R_0805_2012Metric"):
    return Part("Device", "R", value=value, footprint=footprint)


# ---------------------------------------------------------------------------
# Basic grouping
# ---------------------------------------------------------------------------

def test_extract_groups_basic():
    """Parts inside nested subcircuits land in separate groups."""

    @subcircuit
    def bypass_cap(vcc, gnd):
        c = Part("Device", "C", value="100nF", footprint="Capacitor_SMD:C_0805_2012Metric")
        c[1] += vcc
        c[2] += gnd

    @subcircuit
    def ic_block(vcc, gnd, sig_out):
        u = _make_part()
        u[1] += vcc
        u[2] += sig_out
        bypass_cap(vcc, gnd)

    with Circuit() as ckt:
        vcc, gnd, sig = Net("VCC"), Net("GND"), Net("SIG")
        ic_block(vcc, gnd, sig)

    groups = extract_groups(ckt)

    assert len(groups) >= 2
    total_parts = sum(len(g.parts) for g in groups.values())
    assert total_parts == len(ckt.parts)


def test_extract_groups_all_parts_accounted():
    """Every part in the circuit appears in exactly one group."""

    @subcircuit
    def sub_a(net_in, net_out):
        r = _make_part("10k")
        r[1] += net_in
        r[2] += net_out

    @subcircuit
    def sub_b(net_in, net_out):
        r = _make_part("22k")
        r[1] += net_in
        r[2] += net_out

    with Circuit() as ckt:
        n1, n2, n3 = Net("N1"), Net("N2"), Net("N3")
        sub_a(n1, n2)
        sub_b(n2, n3)

    groups = extract_groups(ckt)
    all_refs = [part.ref for g in groups.values() for part in g.parts]
    circuit_refs = [part.ref for part in ckt.parts]

    assert sorted(all_refs) == sorted(circuit_refs)


# ---------------------------------------------------------------------------
# Adjacency
# ---------------------------------------------------------------------------

def test_adjacency_shared_net():
    """Two parts sharing a net appear in each other's adjacency sets."""

    @subcircuit
    def voltage_divider(vin, vout, gnd):
        r1 = _make_part("10k")
        r2 = _make_part("10k")
        r1[1] += vin
        r1[2] += vout
        r2[1] += vout
        r2[2] += gnd

    with Circuit() as ckt:
        vin, vout, gnd = Net("VIN"), Net("VOUT"), Net("GND")
        voltage_divider(vin, vout, gnd)

    groups = extract_groups(ckt)

    # Both parts should be in one group and adjacent via VOUT
    all_parts = ckt.parts
    assert len(all_parts) == 2
    r1, r2 = all_parts[0], all_parts[1]

    group = next(iter(groups.values()))
    assert r1.ref in group.adjacency
    assert r2.ref in group.adjacency[r1.ref]
    assert group.adjacency[r1.ref][r2.ref] >= 1
    assert r1.ref in group.adjacency[r2.ref]


def test_adjacency_no_cross_contamination():
    """Parts not sharing any net are not adjacent."""

    @subcircuit
    def isolated(net_a1, net_a2, net_b1, net_b2):
        r1 = _make_part()
        r2 = _make_part()
        r1[1] += net_a1
        r1[2] += net_a2
        r2[1] += net_b1
        r2[2] += net_b2

    with Circuit() as ckt:
        a1, a2, b1, b2 = Net("A1"), Net("A2"), Net("B1"), Net("B2")
        isolated(a1, a2, b1, b2)

    groups = extract_groups(ckt)
    group = next(iter(groups.values()))

    r1, r2 = ckt.parts[0], ckt.parts[1]
    assert r2.ref not in group.adjacency.get(r1.ref, {})
    assert r1.ref not in group.adjacency.get(r2.ref, {})


# ---------------------------------------------------------------------------
# Flat circuit (no subcircuits)
# ---------------------------------------------------------------------------

def test_flat_circuit_single_group():
    """A flat circuit with no subcircuits produces a single group."""
    with Circuit() as ckt:
        n1, n2 = Net("N1"), Net("N2")
        r = _make_part()
        r[1] += n1
        r[2] += n2

    groups = extract_groups(ckt)
    assert len(groups) == 1
    assert len(list(groups.values())[0].parts) == len(ckt.parts)


# ---------------------------------------------------------------------------
# Empty circuit
# ---------------------------------------------------------------------------

def test_empty_circuit():
    """An empty circuit returns an empty dict."""
    with Circuit() as ckt:
        pass

    groups = extract_groups(ckt)
    assert groups == {}


# ---------------------------------------------------------------------------
# NC net skipped
# ---------------------------------------------------------------------------

def test_nc_net_not_in_adjacency():
    """Parts connected only via NC net are not made adjacent."""
    with Circuit() as ckt:
        r1 = _make_part()
        r2 = _make_part()
        shared = Net("SIG")
        r1[1] += shared
        r2[1] += shared
        r1[2] += ckt.NC
        r2[2] += ckt.NC

    groups = extract_groups(ckt)
    group = next(iter(groups.values()))

    # r1 and r2 share SIG so must be adjacent
    assert r2.ref in group.adjacency.get(r1.ref, {})
    assert len(group.adjacency.get(r1.ref, {})) == 1


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

def test_returns_placement_group_instances():
    """Values in the returned dict are PlacementGroup instances."""
    with Circuit() as ckt:
        n1, n2 = Net("N1"), Net("N2")
        r = _make_part()
        r[1] += n1
        r[2] += n2

    groups = extract_groups(ckt)
    for g in groups.values():
        assert isinstance(g, PlacementGroup)
