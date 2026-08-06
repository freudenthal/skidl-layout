# -*- coding: utf-8 -*-
"""Tests for the templating sweep (construction arc, S2 -- WS-P1/P2/P3/P4).

⛔ Everything here is **offline**: no router, no KRT, no board on disk, no
placement. The nine-board sweep, the ledger and the renders live in
``canaries/drive_templating_sweep.py``.

⛔⛔ :mod:`skidl_layout.cells_partition` is a **leaf**. Nothing in the engine
imports it and nothing consumes the partition yet, so no test here may reach the
scorer, the refiner or a placement digest.

⚠ The circuits are built from the small synthetic ``_Part`` / ``_Net`` /
``_Circuit`` harness the decap tests already use, not from live SKiDL parts.
That is deliberate: ``conftest.py``'s autouse ``mock_active_circuit`` fixture
makes a **second** ``@circuit`` build in one test a subcircuit of the first and
it dies with *"Reference collision"*, so one circuit per test would be a
constraint on every determinism assertion here -- and the determinism assertions
are the most valuable ones in the file. The footprint **names** are real, so the
geometry the pin template needs is the library's own.
"""

from __future__ import annotations

import json

import pytest

from skidl_layout.cells_families import FAMILIES
from skidl_layout.cells_partition import (
    ANCHOR_DEVICE_KINDS,
    CellGroup,
    NON_ANCHOR_DEVICE_KINDS,
    PinBinding,
    family_pattern_graph,
    part_kind,
    partition_circuit,
    partition_to_dict,
)

_FP_DIRS = None


def _fp_dirs():
    global _FP_DIRS
    if _FP_DIRS is None:
        from skidl_layout.metrics import discover_footprint_dir

        found = discover_footprint_dir()
        _FP_DIRS = [found] if found else []
    return _FP_DIRS


needs_footprints = pytest.mark.skipif(
    not _fp_dirs(), reason="no KiCad footprint library on this host")

#: Real names so that ``resolve_footprint_name`` + ``load_footprint_geometries``
#: return the library's own pads rather than the 2 x 2 mm silent fallback that
#: standing finding 6 is about.
SOIC8 = "Package_SO:SOIC-8_5.3x5.3mm_P1.27mm"
R0402 = "Resistor_SMD:R_0402_1005Metric"
R0805 = "Resistor_SMD:R_0805_2012Metric"
C0402 = "Capacitor_SMD:C_0402_1005Metric"
C0805 = "Capacitor_SMD:C_0805_2012Metric"
L0805 = "Inductor_SMD:L_0805_2012Metric"


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #
class _Net:
    def __init__(self, name):
        self.name = name
        self._pins = []

    def get_pins(self):
        return self._pins


class _Pin:
    def __init__(self, part, num, net, name=""):
        self.part = part
        self.num = str(num)
        self.name = name
        self.net = net
        net._pins.append(self)


class _Part:
    def __init__(self, ref, name="", value="", footprint="", pins=(),
                 description="", decouples=None):
        self.ref = ref
        self.name = name
        self.value = value
        self.footprint = footprint
        self.description = description
        if decouples is not None:
            self.decouples = decouples
        self.pins = []
        for entry in pins:
            num, net = entry[0], entry[1]
            pin_name = entry[2] if len(entry) > 2 else ""
            self.pins.append(_Pin(self, num, net, pin_name))

    def __len__(self):
        return len(self.pins)


class _Circuit:
    def __init__(self, parts, nets):
        self.parts = list(parts)
        self._nets = list(nets)

    def get_nets(self):
        return self._nets


def _nets(*names):
    return {name: _Net(name) for name in names}


#: A controller's pin names, so ``power_roles.classify_devices`` types the part
#: from a **library fact** rather than from its reference designator.
CONTROLLER_PINS = ("FB", "COMP", "SW", "VCC", "GND", "BOOT", "SENSE", "RT")


def _soic_controller(ref, net_map, *, name="LT9999", description=""):
    """An 8-pin SOIC whose pin *names* make it a controller."""
    pins = [(str(i + 1), net_map[i], CONTROLLER_PINS[i]) for i in range(8)]
    return _Part(ref, name=name, footprint=SOIC8, pins=pins,
                 description=description)


# --------------------------------------------------------------------------- #
# Kinds -- a library fact, never a reference designator
# --------------------------------------------------------------------------- #
def test_part_kind_reads_the_symbol_and_not_the_reference():
    n = _nets("A", "B")
    resistor = _Part("C99", name="R", pins=[("1", n["A"]), ("2", n["B"])])
    capacitor = _Part("R99", name="C", pins=[("1", n["A"]), ("2", n["B"])])
    inductor = _Part("U99", name="L", pins=[("1", n["A"]), ("2", n["B"])])
    assert part_kind(resistor) == "R"      # ⛔ C99 is NOT a capacitor
    assert part_kind(capacitor) == "C"
    assert part_kind(inductor) == "L"


def test_part_kind_denies_an_led_whose_pins_look_like_a_diode():
    n = _nets("A", "B")
    led = _Part("D1", name="LED", pins=[("1", n["A"], "K"), ("2", n["B"], "A")])
    assert part_kind(led) is None


def test_part_kind_rejects_anything_that_is_not_two_terminal():
    n = _nets("A", "B", "C")
    shunt = _Part("R1", name="R_Shunt",
                  pins=[("1", n["A"]), ("2", n["B"]), ("3", n["C"])])
    assert part_kind(shunt) is None


# --------------------------------------------------------------------------- #
# The pattern side
# --------------------------------------------------------------------------- #
def test_family_pattern_graph_labels_by_kind_not_footprint():
    spec = next(s for s in FAMILIES if s.name == "divider")
    labels, adjacency = family_pattern_graph(spec)
    assert sorted(labels.values()) == ["R", "R"]
    assert adjacency["R1"] == frozenset({"R2"})


def test_family_pattern_graph_reads_divider_cap_as_a_triangle():
    spec = next(s for s in FAMILIES if s.name == "divider_cap")
    labels, adjacency = family_pattern_graph(spec)
    assert sorted(labels.values()) == ["C", "R", "R"]
    for ref in labels:
        assert len(adjacency[ref]) == 2, f"{ref} is not in a triangle"


def test_every_declared_family_has_two_or_three_members():
    #: The plan's expectation table: ``FAMILIES`` has no larger spec, so a
    #: ``family`` group can never exceed three members.
    assert {len(spec.parts) for spec in FAMILIES} <= {2, 3}


# --------------------------------------------------------------------------- #
# Small synthetic boards
# --------------------------------------------------------------------------- #
def _divider_board(upper_fp=R0402, lower_fp=R0805):
    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    r1 = _Part("R1", name="R", value="100k", footprint=upper_fp,
               pins=[("1", n["OUT"]), ("2", n["FB"])])
    r2 = _Part("R2", name="R", value="10k", footprint=lower_fp,
               pins=[("1", n["FB"]), ("2", n["GND"])])
    return _Circuit([u1, r1, r2], list(n.values()))


def test_a_divider_is_a_divider_at_any_size():
    """⭐ The whole reason ``cells_match.match_cell`` could not be reused."""
    part = partition_circuit(_divider_board(R0402, R0805), fp_lib_dirs=[],
                             board="mixed_sizes")
    families = [g for g in part.groups if g.kind == "family"]
    assert [g.family for g in families] == ["divider"]
    assert families[0].refs == ("R1", "R2")


def test_a_family_group_records_its_declared_topology():
    part = partition_circuit(_divider_board(), fp_lib_dirs=[], board="t")
    group = next(g for g in part.groups if g.kind == "family")
    assert group.topology in ("junction", "chain")


def test_the_partition_is_total_and_unassigned_is_empty():
    part = partition_circuit(_divider_board(), fp_lib_dirs=[], board="t")
    assert part.unassigned == ()
    seen = [ref for group in part.groups for ref in group.refs]
    assert sorted(seen) == ["R1", "R2", "U1"]
    assert len(seen) == len(set(seen)) == part.parts_total


def test_a_circuit_with_no_parts_raises_rather_than_returning_an_empty_map():
    """⛔ Standing finding 1 -- five instances in six runs."""
    with pytest.raises(ValueError):
        partition_circuit(_Circuit([], []), fp_lib_dirs=[], board="empty")


def test_plane_nets_are_excluded_from_adjacency_so_two_grounded_resistors_do_not_match():
    """⛔ Standing finding 5. Without the exclusion *every* pair matches."""
    n = _nets("VCC", "GND")
    r1 = _Part("R1", name="R", footprint=R0402,
               pins=[("1", n["VCC"]), ("2", n["GND"])])
    r2 = _Part("R2", name="R", footprint=R0402,
               pins=[("1", n["VCC"]), ("2", n["GND"])])
    part = partition_circuit(_Circuit([r1, r2], list(n.values())),
                             fp_lib_dirs=[], board="planes")
    assert [g.kind for g in part.groups] == ["singleton", "singleton"]


def test_an_induced_pattern_non_edge_must_map_to_a_host_non_edge():
    """A chain of three resistors must not read as a ``divider_cap`` triangle."""
    n = _nets("A", "B", "C", "D")
    parts = [
        _Part("R1", name="R", footprint=R0402, pins=[("1", n["A"]), ("2", n["B"])]),
        _Part("R2", name="R", footprint=R0402, pins=[("1", n["B"]), ("2", n["C"])]),
        _Part("C1", name="C", footprint=C0402, pins=[("1", n["C"]), ("2", n["D"])]),
    ]
    part = partition_circuit(_Circuit(parts, list(n.values())),
                             fp_lib_dirs=[], board="chain")
    families = [g for g in part.groups if g.kind == "family"]
    #: R1-R2 is a legal ``divider``; the three together are NOT a triangle, so
    #: no ``divider_cap`` may be reported.
    assert "divider_cap" not in [g.family for g in families]


def test_the_declared_internal_net_is_consumed_not_ignored():
    """``rc_snubber``'s ``SNUB`` "touches nothing else" -- so an RC leg whose
    mid-node is shared with a third part is **not** a snubber."""
    n = _nets("SW", "GND", "MID")
    parts = [
        _Part("R1", name="R", footprint=R0402, pins=[("1", n["SW"]), ("2", n["MID"])]),
        _Part("C1", name="C", footprint=C0402, pins=[("1", n["MID"]), ("2", n["GND"])]),
        _Part("R2", name="R", footprint=R0402, pins=[("1", n["MID"]), ("2", n["SW"])]),
    ]
    part = partition_circuit(_Circuit(parts, list(n.values())),
                             fp_lib_dirs=[], board="shared_mid")
    assert "rc_snubber" not in [g.family for g in part.groups if g.family]
    assert part.meta["families"]["internal_net_rejections"] >= 1


def test_a_true_snubber_whose_mid_node_touches_nothing_else_does_match():
    n = _nets("SW", "GND", "SNUB")
    parts = [
        _Part("R1", name="R", footprint=R0402, pins=[("1", n["SW"]), ("2", n["SNUB"])]),
        _Part("C1", name="C", footprint=C0402, pins=[("1", n["SNUB"]), ("2", n["GND"])]),
    ]
    part = partition_circuit(_Circuit(parts, list(n.values())),
                             fp_lib_dirs=[], board="snubber")
    assert [g.family for g in part.groups if g.family] == ["rc_snubber"]


def test_an_lc_filter_matches_on_kinds_r_c_l():
    #: ⚠ The mid node is ``FILT`` and not ``VOUT`` on purpose: ``VOUT`` is a
    #: **plane** net by ``is_plane_net``, so the two members would share no
    #: plane-free edge at all and the family could not be found -- which is rule
    #: 5 doing its job, not a matcher weakness.
    n = _nets("SW", "FILT", "GND")
    parts = [
        _Part("L1", name="L", footprint=L0805, pins=[("1", n["SW"]), ("2", n["FILT"])]),
        _Part("C1", name="C", footprint=C0805, pins=[("1", n["FILT"]), ("2", n["GND"])]),
    ]
    part = partition_circuit(_Circuit(parts, list(n.values())),
                             fp_lib_dirs=[], board="lc")
    assert [g.family for g in part.groups if g.family] == ["lc_filter"]


# --------------------------------------------------------------------------- #
# Anchors, and the two classifiers that disagree about them
# --------------------------------------------------------------------------- #
def _two_ic_board():
    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN",
              "FB2", "CP2", "SWN2", "BOOT2", "RT2", "OUT2")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    u2 = _soic_controller("U2", [n["FB2"], n["CP2"], n["SWN2"], n["OUT"],
                                 n["GND"], n["BOOT2"], n["OUT2"], n["RT2"]])
    return _Circuit([u1, u2], list(n.values()))


def test_no_group_ever_holds_two_complex_ics():
    """⛔ Overview 5.3 rule 2, and the driver's bail-out 3."""
    part = partition_circuit(_two_ic_board(), fp_lib_dirs=[], board="two_ics")
    ic_groups = [g for g in part.groups if g.kind == "ic"]
    assert sorted(g.anchor for g in ic_groups) == ["U1", "U2"]
    for group in ic_groups:
        assert len(group.refs) == 1


def test_a_three_pin_mosfet_is_a_device_and_not_an_anchor():
    """⛔⛔ ``roles.classify_part`` types *anything* with more than two pins as
    ``ic``; ``power_roles`` types this one ``switch`` from its G/D/S pin names.
    The library fact wins, and the demotion is recorded by name."""
    n = _nets("VIN", "GND", "SWN", "GATE", "OUT", "FB", "CP", "BOOTN", "RTN")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    m1 = _Part("M1", name="SI7450DP", footprint=SOIC8,
               pins=[("1", n["GATE"], "G"), ("2", n["SWN"], "D"),
                     ("3", n["GND"], "S")])
    part = partition_circuit(_Circuit([u1, m1], list(n.values())),
                             fp_lib_dirs=[], board="fet")
    assert [f.split(":")[0] for f in part.meta["anchor_demoted_to_device"]] == ["M1"]
    assert [g.anchor for g in part.groups if g.kind == "ic"] == ["U1"]


def test_a_controller_whose_description_says_switching_is_still_an_anchor():
    """⛔⛔ ``roles.PANEL_CONTROL_RE`` is a **substring** rule over the part
    description, and every switching regulator's description contains the word
    "switching" -- so ``classify_part`` calls the LT3844 a panel ``control``."""
    from skidl_layout.roles import classify_part

    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN")
    u1 = _soic_controller(
        "U1", [n["FB"], n["CP"], n["SWN"], n["VIN"], n["GND"], n["BOOTN"],
               n["OUT"], n["RTN"]],
        name="LT3844",
        description="High Voltage Synchronous Current Mode Step-Down "
                    "Switching Regulator")
    assert classify_part(u1).role == "control"          # the defect, pinned
    part = partition_circuit(_Circuit([u1], list(n.values())),
                             fp_lib_dirs=[], board="switching")
    assert [g.anchor for g in part.groups if g.kind == "ic"] == ["U1"]
    assert part.meta["anchor_promoted_from_device"]


def test_the_two_device_kind_sets_are_disjoint():
    assert not (ANCHOR_DEVICE_KINDS & NON_ANCHOR_DEVICE_KINDS)


# --------------------------------------------------------------------------- #
# The single-part-against-a-power-pin template
# --------------------------------------------------------------------------- #
def _decap_board(value="100n", cap_fp=C0402):
    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    c1 = _Part("C1", name="C", value=value, footprint=cap_fp,
               pins=[("1", n["VIN"]), ("2", n["GND"])])
    return _Circuit([u1, c1], list(n.values()))


@needs_footprints
def test_an_isolated_decap_binds_to_one_pad_of_the_anchor():
    part = partition_circuit(_decap_board(), fp_lib_dirs=_fp_dirs(),
                             board="decap")
    group = next(g for g in part.groups if g.kind == "ic")
    assert [b.ref for b in group.bindings] == ["C1"]
    binding = group.bindings[0]
    assert binding.role == "decap"
    assert binding.net == "VIN"
    assert binding.anchor_pad == "4"        # the SOIC pin carrying VIN
    assert binding.reason


@needs_footprints
def test_the_anchor_pad_is_a_real_pad_of_the_anchor_footprint():
    from skidl_layout.geometry import load_footprint_geometries

    part = partition_circuit(_decap_board(), fp_lib_dirs=_fp_dirs(),
                             board="decap")
    geometry = load_footprint_geometries({SOIC8}, _fp_dirs())[SOIC8]
    numbers = {str(pad.number) for pad in geometry.pads}
    for group in part.groups:
        for binding in group.bindings:
            assert binding.anchor_pad in numbers


@needs_footprints
def test_a_bulk_cap_binds_with_role_bulk_and_a_decap_with_role_decap():
    bulk = partition_circuit(_decap_board(value="22u", cap_fp=C0805),
                             fp_lib_dirs=_fp_dirs(), board="bulk")
    decap = partition_circuit(_decap_board(value="100n"),
                              fp_lib_dirs=_fp_dirs(), board="decap")
    assert [b.role for g in bulk.groups for b in g.bindings] == ["bulk"]
    assert [b.role for g in decap.groups for b in g.bindings] == ["decap"]


@needs_footprints
def test_a_part_between_a_rail_and_a_non_ground_net_binds_as_a_pullup():
    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN", "NRST")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    r1 = _Part("R1", name="R", value="10k", footprint=R0402,
               pins=[("1", n["VIN"]), ("2", n["NRST"])])
    part = partition_circuit(_Circuit([u1, r1], list(n.values())),
                             fp_lib_dirs=_fp_dirs(), board="pullup")
    assert [b.role for g in part.groups for b in g.bindings] == ["pullup"]


@needs_footprints
def test_an_explicit_decouples_declaration_wins_outright():
    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN",
              "FB2", "CP2", "SWN2", "BOOT2", "RT2", "OUT2")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    u2 = _soic_controller("U2", [n["FB2"], n["CP2"], n["SWN2"], n["VIN"],
                                 n["GND"], n["BOOT2"], n["OUT2"], n["RT2"]])
    c1 = _Part("C1", name="C", value="100n", footprint=C0402,
               pins=[("1", n["VIN"]), ("2", n["GND"])],
               decouples=("U2", None))
    part = partition_circuit(_Circuit([u1, u2, c1], list(n.values())),
                             fp_lib_dirs=_fp_dirs(), board="declared")
    owner = part.group_of("C1")
    assert owner is not None and owner.anchor == "U2"
    assert "decouples=" in owner.bindings[0].reason


def test_an_unresolvable_anchor_footprint_is_reported_and_never_guessed():
    """⛔⛔ Standing finding 6 -- the silent 2 x 2 mm fallback."""
    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    u1.footprint = "NoSuchFootprintAnywhere"
    part = partition_circuit(_Circuit([u1], list(n.values())),
                             fp_lib_dirs=_fp_dirs(), board="unresolved")
    assert part.meta["unresolved_footprints"]


# --------------------------------------------------------------------------- #
# Banks, connectors, singletons
# --------------------------------------------------------------------------- #
def test_two_unanchored_capacitors_on_one_rail_pair_form_a_bank():
    n = _nets("VIN", "GND")
    parts = [
        _Part("C1", name="C", value="22u", footprint=C0805,
              pins=[("1", n["VIN"]), ("2", n["GND"])]),
        _Part("C2", name="C", value="22u", footprint=C0805,
              pins=[("1", n["VIN"]), ("2", n["GND"])]),
    ]
    part = partition_circuit(_Circuit(parts, list(n.values())),
                             fp_lib_dirs=[], board="bank")
    banks = [g for g in part.groups if g.kind == "bank"]
    assert len(banks) == 1
    assert banks[0].refs == ("C1", "C2")
    assert banks[0].name == "bank:VIN+GND"       # ⛔ content, not a counter


def test_a_connector_gets_its_own_group_and_is_never_a_servant():
    """Overview section 10: a connector arrives as a fixed position."""
    n = _nets("VIN", "GND", "OUT", "FB", "SWN", "CP", "BOOTN", "RTN")
    u1 = _soic_controller("U1", [n["FB"], n["CP"], n["SWN"], n["VIN"],
                                 n["GND"], n["BOOTN"], n["OUT"], n["RTN"]])
    j1 = _Part("J1", name="Conn_01x02", footprint=R0402,
               pins=[("1", n["OUT"]), ("2", n["GND"])])
    part = partition_circuit(_Circuit([u1, j1], list(n.values())),
                             fp_lib_dirs=[], board="conn")
    connectors = [g for g in part.groups if g.kind == "connector"]
    assert [g.refs for g in connectors] == [("J1",)]
    assert "J1" not in next(g for g in part.groups if g.kind == "ic").refs


def test_a_singleton_cell_is_a_legal_cell():
    n = _nets("A", "B")
    lone = _Part("R1", name="R", footprint=R0402,
                 pins=[("1", n["A"]), ("2", n["B"])])
    part = partition_circuit(_Circuit([lone], list(n.values())),
                             fp_lib_dirs=[], board="lone")
    assert [g.kind for g in part.groups] == ["singleton"]
    assert part.groups[0].anchor == "R1"
    assert part.singleton_refs == ("R1",)


# --------------------------------------------------------------------------- #
# Arithmetic, serialisation, determinism
# --------------------------------------------------------------------------- #
def test_the_ledger_fractions_are_computed_over_the_whole_board():
    part = partition_circuit(_divider_board(), fp_lib_dirs=[], board="t")
    assert part.parts_total == 3
    assert part.templated_fraction == round(2 / 3, 4)
    assert part.largest_group_fraction == round(2 / 3, 4)
    assert part.by_kind == {"family": 1, "ic": 1}


def test_partition_to_dict_round_trips_through_json():
    part = partition_circuit(_divider_board(), fp_lib_dirs=[], board="t")
    blob = json.dumps(partition_to_dict(part), sort_keys=True, default=str)
    assert json.loads(blob)["board"] == "t"
    assert json.loads(blob)["unassigned"] == []


def test_group_names_are_derived_from_content_and_never_from_a_counter():
    part = partition_circuit(_divider_board(), fp_lib_dirs=[], board="t")
    for group in part.groups:
        assert any(ref in group.name for ref in group.refs), group.name
        assert not group.name.rstrip("0123456789").endswith("#")


def test_two_runs_are_byte_identical():
    circuit = _divider_board()
    first = json.dumps(partition_to_dict(
        partition_circuit(circuit, fp_lib_dirs=[], board="t")),
        sort_keys=True, default=str)
    second = json.dumps(partition_to_dict(
        partition_circuit(circuit, fp_lib_dirs=[], board="t")),
        sort_keys=True, default=str)
    assert first == second


def test_permuting_circuit_parts_does_not_change_the_partition():
    """⛔⛔ Standing finding 8, third instance -- a total key over CONTENT.

    ⭐ The single most valuable assertion in this file: the same mistake placed
    differently on 3 of 6 boards the last time it was made.
    """
    circuit = _divider_board()
    base = json.dumps(partition_to_dict(
        partition_circuit(circuit, fp_lib_dirs=[], board="t")),
        sort_keys=True, default=str)
    for order in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
        circuit.parts = [circuit.parts[i] for i in order]
        again = json.dumps(partition_to_dict(
            partition_circuit(circuit, fp_lib_dirs=[], board="t")),
            sort_keys=True, default=str)
        assert again == base, f"permutation {order} moved the partition"


def test_permuting_the_NET_order_does_not_change_the_partition():
    """⛔⛔ The other half of standing finding 8, and it is not hypothetical.

    ``power_roles._View`` reads ``circuit.get_nets()`` into ``net_order`` /
    ``net_index`` and tie-breaks on them, so leaving net order to the caller
    would leave half the door open. ``_CanonicalCircuit`` sorts both.
    """
    circuit = _divider_board()
    base = json.dumps(partition_to_dict(
        partition_circuit(circuit, fp_lib_dirs=[], board="t")),
        sort_keys=True, default=str)
    circuit._nets = list(reversed(circuit._nets))
    again = json.dumps(partition_to_dict(
        partition_circuit(circuit, fp_lib_dirs=[], board="t")),
        sort_keys=True, default=str)
    assert again == base


def test_the_canonical_view_sorts_parts_and_nets_by_content():
    from skidl_layout.cells_partition import _CanonicalCircuit

    circuit = _divider_board()
    circuit.parts = list(reversed(circuit.parts))
    circuit._nets = list(reversed(circuit._nets))
    canon = _CanonicalCircuit(circuit)
    assert [p.ref for p in canon.parts] == sorted(p.ref for p in circuit.parts)
    assert [n.name for n in canon.get_nets()] == \
        sorted(n.name for n in circuit.get_nets())
    # ⛔ Everything else still reaches the wrapped circuit.
    assert canon.parts is not circuit.parts


def test_the_servant_kind_sets_partition_what_power_roles_can_emit():
    """⛔⛔ **A filter that matches nothing is the observes-nothing defect
    wearing a filter's clothes.**

    The plan declared ``capacitor`` as a stage servant kind; the classifier
    never emits it (it emits ``input_cap`` / ``output_cap`` / ``coupling_cap``),
    and six typed parts were scattered to singletons with nothing complaining.
    The *corpus-wide* version of this guard is gate ``TS2``; this is the cheap
    structural half — the two sets must not overlap and must not be empty.
    """
    from skidl_layout.cells_partition import (
        STAGE_DEVICE_KINDS, STAGE_DEVICE_KINDS_HANDLED_ELSEWHERE)

    collected = set(STAGE_DEVICE_KINDS)
    excused = set(STAGE_DEVICE_KINDS_HANDLED_ELSEWHERE)
    assert collected and excused
    assert not (collected & excused), \
        f"{sorted(collected & excused)} is both collected and excused"
    # The kinds ``power_roles`` is known to emit, pinned here so that adding one
    # upstream fails loudly instead of being silently dropped.
    known = {"controller", "switch", "rectifier", "magnetics", "gate_resistor",
             "sense_resistor", "fb_divider_top", "fb_divider_bottom",
             "input_cap", "output_cap", "coupling_cap", "resistor",
             "capacitor", "unknown"}
    assert known <= (collected | excused), \
        f"undecided kinds: {sorted(known - collected - excused)}"


def test_the_degree_histogram_is_recorded_both_ways():
    """⚠ Rule 5's cost is made visible, never traded."""
    part = partition_circuit(_decap_board(), fp_lib_dirs=[], board="t")
    assert part.meta["degree_histogram_plane_free"]
    assert part.meta["degree_histogram_all_nets"]
    assert part.meta["degree_histogram_plane_free"] \
        != part.meta["degree_histogram_all_nets"]


def test_the_artifact_does_not_carry_the_unstable_upstream_prose():
    """⛔⛔ MEASURED 2026-08-03 by gate ``TS4``: permuting ``circuit.parts`` on
    ``lt8710_sepic`` turns ``classify_power_roles``' topology from ``flyback``
    into ``cuk`` (``_switches_on``, ``power_roles.py:1009``, collects in arrival
    order and ``_build_stage:1099`` takes ``switches[0]``). With that name in a
    reason string the whole artifact stopped being byte-identical while not one
    group, member or binding moved. ⛔ An artifact may not launder an unstable
    input -- so the topology and the warning prose live in the driver's
    **ledger**, which is a measurement, and never here."""
    part = partition_circuit(_divider_board(), fp_lib_dirs=[], board="t")
    assert "warnings" not in part.meta["power_roles"]
    assert "warning_count" in part.meta["power_roles"]
    blob = json.dumps(partition_to_dict(part), sort_keys=True, default=str)
    for topology in ("flyback", "cuk", "sepic", "boost", "buck"):
        assert topology not in blob, \
            f"an upstream topology name reached the artifact: {topology}"


def test_the_value_types_are_frozen():
    binding = PinBinding(ref="C1", anchor_pad="4", net="VIN", role="decap",
                         reason="because")
    group = CellGroup(name="ic:U1", kind="ic", refs=("C1", "U1"), anchor="U1",
                      family=None, topology=None, bindings=(binding,))
    with pytest.raises(Exception):
        binding.ref = "C2"
    with pytest.raises(Exception):
        group.anchor = "U2"
    assert group.size == 2


# --------------------------------------------------------------------------- #
# ⛔ The leaf guard -- import-aware from the start (standing finding 16)
# --------------------------------------------------------------------------- #
#: ⚠ **This guard reads IMPORT STATEMENTS, never the module name as a
#: substring.** Its sibling used to do the latter, and a *docstring* that said
#: ``escape_map`` out loud failed it and cost a full ~9-minute suite run. The
#: property being guarded is "nothing imports it"; prose about a module is
#: documentation and this arc wants more of it, not less. ⛔ Do not "simplify"
#: this back to ``name in text``.
import re                                                       # noqa: E402

_CELLS_PARTITION_IMPORT_RE = re.compile(
    r"^[ \t]*(?:"
    r"from[ \t]+[.\w]*\bcells_partition\b[ \t]+import"
    #: ⚠ ``[^#\n]`` and not ``[^\n]``: a trailing comment on an unrelated import
    #: is prose, and prose is allowed.
    r"|from[ \t]+[.\w]+[ \t]+import[ \t]+[^#\n]*\bcells_partition\b"
    r"|import[ \t]+[^#\n]*\bcells_partition\b"
    r")"
    r"|import_module\([^)]*\bcells_partition\b",
    re.MULTILINE,
)


def test_cells_partition_is_a_leaf_no_module_imports_it():
    """⛔ Nothing in ``skidl_layout`` may import this module."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "skidl_layout"
    importers = []
    for path in sorted(root.glob("*.py")):
        if path.name == "cells_partition.py":
            continue
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if _CELLS_PARTITION_IMPORT_RE.search(line):
                importers.append(f"{path.name}:{number}: {line.strip()}")
    assert importers == [], f"cells_partition is imported by {importers}"


def test_the_leaf_guard_matches_imports_and_not_prose():
    """⭐ The guard's own guard -- a pattern that is too narrow stops guarding
    *silently*, which is worse than the false positive that started this."""
    imports = [
        "from .cells_partition import partition_circuit",
        "from cells_partition import partition_circuit",
        "from skidl_layout.cells_partition import Partition",
        "    from .cells_partition import Partition",        # function-local
        "from . import cells_partition",
        "from skidl_layout import cells_partition",
        "import cells_partition",
        "import skidl_layout.cells_partition",
        "import skidl_layout.cells_partition as CP",
        '    mod = importlib.import_module("skidl_layout.cells_partition")',
    ]
    prose = [
        "# same leaf discipline as cells_partition",
        '    """Consumed by :mod:`~skidl_layout.cells_partition`."""',
        "    # ⛔ do not import cells_partition here",
        "PARTITION_NOTE = 'cells_partition stays a leaf'",
        "#: cells_partition is the sibling this mirrors",
        "from typing import Literal  # cells_partition mirrors this vocabulary",
        "import os  # cells_partition does its own path handling",
    ]
    missed = [line for line in imports
              if not _CELLS_PARTITION_IMPORT_RE.search(line)]
    tripped = [line for line in prose
               if _CELLS_PARTITION_IMPORT_RE.search(line)]
    assert not missed, f"the guard would MISS these real imports: {missed}"
    assert not tripped, f"the guard falsely trips on this prose: {tripped}"


def test_the_package_does_not_re_export_the_partition_api():
    import skidl_layout

    for name in ("partition_circuit", "Partition", "CellGroup", "PinBinding"):
        assert not hasattr(skidl_layout, name), \
            f"skidl_layout re-exports {name} -- the leaf rule is broken"
