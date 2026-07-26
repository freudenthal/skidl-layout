# -*- coding: utf-8 -*-
"""The anti-cheat fixture for :mod:`skidl_layout.power_roles` (plan gate G2).

``power_roles`` claims to classify a switching converter **topologically, from
library facts** -- pin names, symbol identity, connectivity -- and never from
user naming. The claim is cheap to make and easy to violate by accident: one
``ref.startswith("R")`` or one ``"SW" in net.name`` and the module quietly
becomes ``power.py`` with more steps.

:func:`scramble_circuit` makes the claim falsifiable. It takes a circuit and
returns a picklable twin in which **every reference designator is ``X<n>`` and
every net name is ``N<n>``**, in circuit order, with nothing else touched:
symbols, pin names, pin order, values and connectivity are identical. A
classifier that reads only library facts must produce the same answer on both,
modulo the renaming; a classifier that peeks at a name cannot.

    twin, refs, nets = scramble_circuit(circuit)
    assert translate(classify(circuit), refs, nets) == classify(twin)

It is deliberately built on :func:`skidl_layout.snapshot.snapshot_circuit`, the
same picklable stand-in the parallel-placement workers use, so the twin exercises
exactly the duck-typed attribute surface the real code contracts to.
"""

from __future__ import annotations

from skidl_layout.snapshot import snapshot_circuit


def scramble_circuit(circuit):
    """Return ``(twin, ref_map, net_map)`` -- a ref- and net-anonymised copy.

    ``ref_map`` maps an original reference designator to its ``X<n>`` stand-in and
    ``net_map`` an original net name to its ``N<n>`` stand-in, so an assertion
    written against the real circuit can be translated rather than rewritten.

    The original circuit is never mutated: the snapshot is a fresh object graph.
    """
    twin = snapshot_circuit(circuit)

    ref_map: dict[str, str] = {}
    for index, part in enumerate(twin.parts):
        original = str(part.ref)
        ref_map[original] = f"X{index}"
        part.ref = ref_map[original]

    net_map: dict[str, str] = {}
    # Nets are interned by identity in a snapshot and reachable twice (via
    # get_nets() and via pin.net), so renaming is tracked by object identity --
    # a name-based guard would double-rename a net whose author-given name
    # happened to look like an ``N<n>`` stand-in.
    renamed: set[int] = set()

    def rename(net):
        if net is None or id(net) in renamed:
            return
        renamed.add(id(net))
        original = str(net.name)
        if original not in net_map:
            net_map[original] = f"N{len(net_map)}"
        net.name = net_map[original]

    for net in twin.get_nets():
        rename(net)
    # Pins can reference a net that get_nets() did not return (snapshot.py's
    # second pass builds those); they need a name too or they would keep the
    # author's.
    for part in twin.parts:
        for pin in part.pins:
            rename(getattr(pin, "net", None))

    return twin, ref_map, net_map


def translate_refs(refs, ref_map):
    """Map a sequence of original refs through ``ref_map`` (order preserved)."""
    return [ref_map[ref] for ref in refs]
