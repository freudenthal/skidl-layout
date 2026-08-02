# -*- coding: utf-8 -*-
"""Tests for ``mark_cell`` -- the declaration seam (cell-toolchain plan, WS-U6).

⛔⛔ The load-bearing one is
:func:`test_the_declaration_survives_a_snapshot`. A worker rebuilds its context
from a :class:`~skidl_layout.snapshot.SnapshotCircuit`, so a field that is not on
``snapshot._SNAPSHOT_FIELDS`` is **silently lost** -- and parallelism engages
only at >= 30 parts, so the loss would corrupt exactly the biggest boards and
leave the small ones perfect. That is the precise shape of the
``crossing_objective`` worker defect 1100 tests did not catch.
"""

from __future__ import annotations

import pytest

from skidl_layout.cells import CellCache, CellMember, CellPad, CellPort, LayoutCell
from skidl_layout.cells_place import (
    CELL_FIELD,
    CellInstance,
    declared_cell_refs,
    declared_cells,
    mark_cell,
    resolve_cell_instances,
)


def _cell(name="fb_divider") -> LayoutCell:
    return LayoutCell(
        name=name, width=6.85, height=1.40,
        members=(CellMember("RFB1", "part", "Resistor_SMD:R_0805_2012Metric",
                            5.425, 0.7, 180, "top", 2.0, 1.4),
                 CellMember("RFB2", "part", "Resistor_SMD:R_0805_2012Metric",
                            1.425, 0.7, 180, "top", 2.0, 1.4)),
        pads=(CellPad("RFB1", "1", "VOUT", 6.3375, 0.7, 1.025, 1.4),
              CellPad("RFB1", "2", "VFB", 4.5125, 0.7, 1.025, 1.4),
              CellPad("RFB2", "1", "VFB", 2.3375, 0.7, 1.025, 1.4),
              CellPad("RFB2", "2", "GND", 0.5125, 0.7, 1.025, 1.4)),
        nets={"VOUT": ("RFB1.1",), "VFB": ("RFB1.2", "RFB2.1"),
              "GND": ("RFB2.2",)},
        internal_nets=frozenset({"VFB"}),
        ports=(CellPort("VOUT", "E", 0, "FAVORED", 6.85, 0.7),
               CellPort("GND", "W", 0, "FAVORED", 0.0, 0.7)),
        stackup=2,
    ).normalised()


class _Pin:
    def __init__(self, num, net):
        self.num = num
        self.net = net


class _Net:
    def __init__(self, name):
        self.name = name


class _Part:
    def __init__(self, ref, footprint, pins):
        self.ref = ref
        self.footprint = footprint
        self.pins = pins
        self.fields = {}


class _Circuit:
    def __init__(self, parts, nets=()):
        self.parts = parts
        self._nets = list(nets)

    def get_nets(self):
        return list(self._nets)


def _circuit():
    fp = "Resistor_SMD:R_0805_2012Metric"
    nets = {n: _Net(n) for n in ("VOUT", "VFB", "GND")}
    for net in nets.values():
        net._pins = []
        net.get_pins = (lambda n=net: n._pins)
    parts = [
        _Part("RFB1", fp, [_Pin("1", nets["VOUT"]), _Pin("2", nets["VFB"])]),
        _Part("RFB2", fp, [_Pin("1", nets["VFB"]), _Pin("2", nets["GND"])]),
    ]
    for part in parts:
        for pin in part.pins:
            pin.net._pins.append(pin)
    return _Circuit(parts, nets.values())


# --------------------------------------------------------------------------- #
# the declaration itself
# --------------------------------------------------------------------------- #
def test_mark_cell_writes_into_the_same_field_channel_the_others_use():
    circuit = _circuit()
    marked = mark_cell("fb_divider", *circuit.parts)
    assert marked == ["RFB1", "RFB2"]
    assert circuit.parts[0].fields[CELL_FIELD]["name"] == "fb_divider"
    assert circuit.parts[0].fields[CELL_FIELD]["digest"] == ""


def test_mark_cell_records_a_digest_when_one_is_given():
    circuit = _circuit()
    cell = _cell()
    mark_cell("fb_divider", *circuit.parts, cell=cell)
    assert circuit.parts[0].fields[CELL_FIELD]["digest"] == cell.digest
    assert circuit.parts[0]._layout_cell_object is cell


def test_mark_cell_accepts_a_bare_digest_string():
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts, cell="0123456789abcdef")
    assert circuit.parts[0].fields[CELL_FIELD]["digest"] == "0123456789abcdef"


def test_mark_cell_clears():
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts)
    mark_cell("fb_divider", *circuit.parts, clear=True)
    assert CELL_FIELD not in circuit.parts[0].fields


def test_a_part_with_no_field_store_is_skipped_not_fatal():
    class _NoFields:
        __slots__ = ("ref",)

        def __init__(self):
            self.ref = "X1"

    assert mark_cell("c", _NoFields()) == []


def test_declared_refs_group_by_name_and_digest():
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts)
    assert declared_cell_refs(circuit) == {("fb_divider", ""): ["RFB1", "RFB2"]}


def test_an_undeclared_circuit_has_no_groups():
    assert declared_cell_refs(_circuit()) == {}


# --------------------------------------------------------------------------- #
# ⛔⛔ the snapshot boundary
# --------------------------------------------------------------------------- #
def test_the_declaration_survives_a_snapshot():
    from skidl_layout.snapshot import _SNAPSHOT_FIELDS, snapshot_circuit

    assert CELL_FIELD in _SNAPSHOT_FIELDS
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts, cell="0123456789abcdef")
    snap = snapshot_circuit(circuit)
    assert declared_cell_refs(snap) == {
        ("fb_divider", "0123456789abcdef"): ["RFB1", "RFB2"]}


def test_the_declaration_survives_a_PICKLE_of_the_snapshot():
    """⭐ The worker boundary is a pickle, so test the pickle, not the object."""
    import pickle

    from skidl_layout.snapshot import snapshot_circuit

    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts)
    revived = pickle.loads(pickle.dumps(snapshot_circuit(circuit)))
    assert declared_cell_refs(revived) == {("fb_divider", ""): ["RFB1", "RFB2"]}


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def test_a_declaration_resolves_from_the_attached_object():
    circuit = _circuit()
    cell = _cell()
    mark_cell("fb_divider", *circuit.parts, cell=cell)
    instances, unresolved = declared_cells(circuit, None)
    assert not unresolved
    assert len(instances) == 1
    assert instances[0].cell.digest == cell.digest
    assert instances[0].member_refs == ("RFB1", "RFB2")


def test_a_declaration_resolves_from_the_cache_by_digest(tmp_path):
    cache = CellCache(str(tmp_path / "cache"))
    cell = _cell()
    cache.store(cell)
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts, cell=cell.digest)
    instances, unresolved = declared_cells(circuit, cache)
    assert not unresolved
    assert instances[0].cell.digest == cell.digest


def test_a_declaration_resolves_from_the_cache_by_NAME(tmp_path):
    cache = CellCache(str(tmp_path / "cache"))
    cache.store(_cell())
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts)
    instances, unresolved = declared_cells(circuit, cache)
    assert not unresolved and len(instances) == 1


def test_an_unresolvable_declaration_is_REPORTED_not_guessed():
    """⛔ A board that declared a template and did not get one must not look
    like a board that never declared one."""
    circuit = _circuit()
    mark_cell("nowhere", *circuit.parts, cell="deadbeefdeadbeef")
    instances, unresolved = declared_cells(circuit, None)
    assert instances == []
    assert len(unresolved) == 1 and "nowhere" in unresolved[0]


def test_a_net_override_survives_resolution():
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts, cell=_cell(),
              nets={"VOUT": "RAIL"})
    instances, _unresolved = declared_cells(circuit, None)
    assert instances[0].circuit_net("VOUT") == "RAIL"


# --------------------------------------------------------------------------- #
# resolve_cell_instances -- what ``plan_layout(cells=...)`` accepts
# --------------------------------------------------------------------------- #
def test_no_cells_is_a_true_no_op():
    assert resolve_cell_instances(_circuit(), None) == ([], [])
    assert resolve_cell_instances(_circuit(), False) == ([], [])
    assert resolve_cell_instances(_circuit(), []) == ([], [])


def test_explicit_instances_are_reindexed_from_one():
    """⛔ Two callers must not disagree about a pseudo-part's ref."""
    instances = [CellInstance(cell=_cell("a"), index=7),
                 CellInstance(cell=_cell("b"), index=7)]
    resolved, _unresolved = resolve_cell_instances(_circuit(), instances)
    assert [i.ref for i in resolved] == ["@CELL1", "@CELL2"]


def test_binding_ORDER_cannot_change_the_pseudo_parts():
    """⛔⛔ The defect gate U6 caught on its first run.

    ``CellInstance.ref`` is ``@CELL<index>`` and the placer's tie-breaks read
    refs, so the order a caller happens to list its cells in used to be part of
    the placement problem. Two callers meaning the same binding must produce the
    same pseudo-parts.
    """
    forward = [CellInstance(cell=_cell("alpha"), index=1),
               CellInstance(cell=_cell("beta"), index=2)]
    backward = list(reversed(forward))
    a, _ = resolve_cell_instances(_circuit(), forward)
    b, _ = resolve_cell_instances(_circuit(), backward)
    assert [(i.ref, i.footprint_key) for i in a] == [
        (i.ref, i.footprint_key) for i in b]


def test_canonical_order_sorts_on_content_not_arrival():
    from skidl_layout.cells_place import canonical_order

    instances = [CellInstance(cell=_cell("zulu")), CellInstance(cell=_cell("alpha"))]
    assert [i.cell.name for i in canonical_order(instances)] == ["alpha", "zulu"]


def test_a_cache_path_is_accepted(tmp_path):
    cache = CellCache(str(tmp_path / "cache"))
    cache.store(_cell())
    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts)
    resolved, unresolved = resolve_cell_instances(circuit, str(tmp_path / "cache"))
    assert not unresolved and len(resolved) == 1


def test_an_unsupported_cells_argument_raises():
    with pytest.raises(TypeError):
        resolve_cell_instances(_circuit(), 3.5)


# --------------------------------------------------------------------------- #
# the engine seam
# --------------------------------------------------------------------------- #
def test_the_engine_helper_returns_empty_for_no_cells():
    from skidl_layout.engine import _resolve_cells

    assert _resolve_cells(_circuit(), None) == ((), ())


def test_the_engine_helper_resolves_a_declaration():
    from skidl_layout.engine import _resolve_cells

    circuit = _circuit()
    mark_cell("fb_divider", *circuit.parts, cell=_cell())
    instances, unresolved = _resolve_cells(circuit, True)
    assert len(instances) == 1 and not unresolved
