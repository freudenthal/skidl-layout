"""Picklable stand-in for a live skidl ``Circuit``.

The layout worker-reachable stack (``refinement``, ``scoring``, ``validator``,
``congestion``, ``power``, ``roles``, ``context``, ``orientation``, ``decaps``)
reads the circuit through duck-typed ``getattr`` access only — it never depends
on the concrete skidl types.  A live ``Circuit`` (parts -> pins -> nets object
graph with library backrefs) does not pickle, which blocks shipping a layout
problem to a ``spawn``-ed worker process on Windows.

``snapshot_circuit(circuit)`` walks the live graph once in the parent and builds
plain, picklable classes that mirror exactly the attribute surface those modules
read.  A worker rebuilds ``LayoutContext.from_circuit(snapshot)`` itself (a pure
function) rather than pickling the context.

The one place the live code is NOT duck-typed is ``isinstance(net, NCNet)``; the
snapshot carries an explicit ``is_ncnet`` marker and the shared
``roles.is_nc_net`` helper honours it (see WS17.2).

Byte-identity through the whole stack is proven by ``tests/test_layout_snapshot.py``
and the DPSG ``verify_snapshot.py`` driver — that is the backstop for any
attribute this module might have missed.
"""

from __future__ import annotations


class SnapshotPinFunc:
    """Mirror of a live pin's ``func`` object.

    ``power._pin_is_power_output`` reads ``func.name`` first, then ``str(func)``
    (``power.py:222-227``).  We capture both at snapshot time so both branches
    reproduce the live result byte-for-byte.
    """

    __slots__ = ("name", "_text")

    def __init__(self, name, text: str):
        self.name = name
        self._text = text

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SnapshotPinFunc(name={self.name!r}, text={self._text!r})"


class SnapshotNet:
    __slots__ = ("name", "_pins", "is_ncnet")

    def __init__(self, name: str, is_ncnet: bool = False):
        self.name = name
        self._pins: list = []
        self.is_ncnet = is_ncnet

    def get_pins(self):
        return self._pins


class SnapshotPin:
    __slots__ = ("part", "net", "func", "num", "name")

    def __init__(self, part, num, name, func):
        self.part = part
        self.net: SnapshotNet | None = None
        self.func = func
        self.num = num
        self.name = name


class SnapshotPart:
    __slots__ = (
        "ref",
        "name",
        "value",
        "foot",
        "footprint",
        "description",
        "hierarchy",
        "pins",
        "_pin_len",
    )

    def __init__(
        self,
        ref,
        name,
        value,
        foot,
        footprint,
        description,
        hierarchy,
        pin_len: int,
    ):
        self.ref = ref
        self.name = name
        self.value = value
        self.foot = foot
        self.footprint = footprint
        self.description = description
        self.hierarchy = hierarchy
        self.pins: list[SnapshotPin] = []
        self._pin_len = pin_len

    def __len__(self) -> int:
        # context._part_pin_count / roles._pin_count / congestion._pin_count try
        # ``len(part)`` first; it must equal the live value exactly.
        return self._pin_len


class SnapshotCircuit:
    __slots__ = ("parts", "_nets")

    def __init__(self, parts, nets):
        self.parts: list[SnapshotPart] = parts
        self._nets: list[SnapshotNet] = nets

    def get_nets(self):
        return self._nets


def _pin_len(part) -> int:
    try:
        return len(part)
    except Exception:
        return len(getattr(part, "pins", []) or [])


def snapshot_circuit(circuit) -> SnapshotCircuit:
    """Build a picklable :class:`SnapshotCircuit` from a live skidl circuit.

    Order is preserved everywhere (``circuit.parts``, each ``part.pins``,
    ``circuit.get_nets()``, each ``net.get_pins()``) so downstream traversals are
    byte-identical to the live circuit.  Nets are interned by identity so a pin's
    ``net`` and the circuit's net list share objects.
    """
    try:
        from skidl.net import NCNet
    except Exception:  # pragma: no cover - skidl always present in practice
        NCNet = None

    # --- parts + pins (nets filled in below) --------------------------------
    snap_parts: list[SnapshotPart] = []
    pin_by_id: dict[int, SnapshotPin] = {}
    for part in circuit.parts:
        snap_part = SnapshotPart(
            ref=getattr(part, "ref", None),
            name=getattr(part, "name", ""),
            value=getattr(part, "value", ""),
            foot=getattr(part, "foot", ""),
            footprint=getattr(part, "footprint", ""),
            description=getattr(part, "description", ""),
            hierarchy=getattr(part, "hierarchy", ""),
            pin_len=_pin_len(part),
        )
        for pin in getattr(part, "pins", []) or []:
            live_func = getattr(pin, "func", None)
            snap_func = (
                None
                if live_func is None
                else SnapshotPinFunc(getattr(live_func, "name", None), str(live_func))
            )
            snap_pin = SnapshotPin(
                part=snap_part,
                num=getattr(pin, "num", None),
                name=getattr(pin, "name", None),
                func=snap_func,
            )
            snap_part.pins.append(snap_pin)
            pin_by_id[id(pin)] = snap_pin
        snap_parts.append(snap_part)

    # --- nets, interned by identity -----------------------------------------
    net_by_id: dict[int, SnapshotNet] = {}
    snap_nets: list[SnapshotNet] = []
    for net in circuit.get_nets():
        is_nc = NCNet is not None and isinstance(net, NCNet)
        snap_net = SnapshotNet(str(getattr(net, "name", "") or ""), is_ncnet=is_nc)
        net_by_id[id(net)] = snap_net
        snap_nets.append(snap_net)
        for pin in net.get_pins():
            snap_pin = pin_by_id.get(id(pin))
            if snap_pin is None:
                # Pin not reached via circuit.parts (defensive; shouldn't happen
                # for a well-formed circuit). Skip — it has no snapshot part.
                continue
            snap_net._pins.append(snap_pin)
            snap_pin.net = snap_net

    # --- resolve any pin whose net was not in circuit.get_nets() ------------
    # (e.g. an NC net or a floating net skidl does not return from get_nets()).
    for part in circuit.parts:
        for pin in getattr(part, "pins", []) or []:
            snap_pin = pin_by_id.get(id(pin))
            if snap_pin is None or snap_pin.net is not None:
                continue
            live_net = getattr(pin, "net", None)
            if live_net is None:
                continue
            key = id(live_net)
            snap_net = net_by_id.get(key)
            if snap_net is None:
                is_nc = NCNet is not None and isinstance(live_net, NCNet)
                snap_net = SnapshotNet(
                    str(getattr(live_net, "name", "") or ""), is_ncnet=is_nc
                )
                net_by_id[key] = snap_net
                # NOT appended to snap_nets: mirrors get_nets() not returning it.
            snap_net._pins.append(snap_pin)
            snap_pin.net = snap_net

    return SnapshotCircuit(snap_parts, snap_nets)
