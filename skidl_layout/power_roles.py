# -*- coding: utf-8 -*-
"""Power-electronics role vocabulary -- report-only (power-layout Phase 1).

This module answers *"which part is the switch, which net is the switch node,
which four parts form the commutation loop, which two resistors are the feedback
divider"* for a switching converter, **from library facts and connectivity
only**.

Why a new module rather than an extension of :mod:`~skidl_layout.roles` or
:mod:`~skidl_layout.power`:

* :mod:`~skidl_layout.roles` is the *dev-board / Eurorack* vocabulary (audio
  jacks, keyswitches, encoders, module sockets). It has **no transistor role at
  all** -- a 3-pin MOSFET classifies as ``"ic"`` -- and its ``"inductor"`` /
  ``"diode"`` roles are bare reference-designator prefix matches.
* :mod:`~skidl_layout.power` recognises power nets by **name regex**
  (``POWER_NET_RE`` / ``GND_NET_RE`` / ``HIGH_CURRENT_NET_RE``), which is exactly
  the thing this module must not do.

**The rule that shapes everything here: library facts are allowed, user naming
is not.**

* ALLOWED -- pin **names** (``G``/``D``/``S``, ``A``/``K``, ``FBX``, ``SENSE``,
  ``GATE``, ``GND``), pin count, the **symbol identity** ``part.name`` (``R``,
  ``C_Polarized``, ``L``, ``LT3757AEMSE``), and **connectivity** (which parts
  share which nets). Every user of that symbol sees the same facts.
* FORBIDDEN -- reference designators (``R7``, ``COUT3``, ``M1``) including their
  prefixes, **net names** (``SW``, ``VOUT``, ``GND``, ``FBX``), and
  ``part.value``. ``part.value`` is admitted in exactly **one** place, as a
  documented tie-break of last resort (:func:`_loop_capacitor`), never as a
  necessary condition.

The gate for that rule is not review: ``tests/test_power_roles.py`` re-runs every
assertion on a **ref- and net-scrambled twin** of each circuit and requires the
classification to come out identical modulo the renaming.

**Report-only.** Nothing here reads or writes a placement, and
:func:`classify_power_roles` is called from ``plan_layout`` only *after* the
placement is final, so it cannot influence it. It names things; it does not
measure, score, or move them (that is Phase 2 onwards).

    plan = classify_power_roles(circuit)
    for stage in plan.stages:
        print(stage.topology, stage.loops[0].member_refs)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .roles import cap_value_uf, is_nc_net

__all__ = [
    "CommutationLoop",
    "PowerDevice",
    "PowerStage",
    "PowerStagePlan",
    "classify_devices",
    "classify_power_roles",
]


# --------------------------------------------------------------------------- #
# Library-fact tables
# --------------------------------------------------------------------------- #

#: A three-terminal switch is identified by its terminal **pin names**, which are
#: a property of the symbol: ``{G,D,S}`` for a FET, ``{B,C,E}`` for a BJT.
#: Duplicated pins collapse (``Si7336ADP`` is ``S S S G D``).
FET_TERMINALS = frozenset({"G", "D", "S"})
BJT_TERMINALS = frozenset({"B", "C", "E"})

#: The control terminal of either -- the gate or the base.
SWITCH_CONTROL_PINS = frozenset({"G", "B"})

#: A rectifier is a two-pin part whose pins are anode and cathode. KiCad names
#: them ``A`` and ``K`` (``Device:D``, ``D_Schottky``, ``D_Zener``, ...).
DIODE_TERMINALS = frozenset({"A", "K"})

#: Winding terminals of a multi-winding magnetic: ``AA``/``AB`` are the two ends
#: of winding *A*, ``SA``/``SB`` of winding *S* (``Device:Transformer_1P_1S``).
WINDING_PIN_RE = re.compile(r"^([A-Z])[AB]$")

#: Symbol-identity families for the two-terminal passives, whose pin names are
#: empty or numeric and therefore carry no information (``Device:R`` is ``'' ''``,
#: ``Device:L`` is ``'1' '2'``). Matching the **symbol name** is still a library
#: fact -- unlike the reference designator, the author does not choose it.
CAP_SYMBOL_RE = re.compile(r"^C($|_)")
RES_SYMBOL_RE = re.compile(r"^R($|_)")
IND_SYMBOL_RE = re.compile(r"^L($|_)")

#: Symbols that must never be typed as a power device even though they would
#: otherwise match one of the rules above. ``LED`` is the important one: its pins
#: are literally ``K``/``A``, so without this an indicator LED types as a
#: rectifier. The rest are the plan's deny-list.
DENY_SYMBOL_RE = re.compile(
    r"^(CRYSTAL|RESONATOR|LED|FUSE|VARISTOR|THERMISTOR|NTC|PTC|RELAY"
    r"|CONN|SCREW_TERMINAL|SW_)"
)

#: Symbols whose pin count may exceed two and still be a plain resistor -- a
#: four-terminal Kelvin shunt is one part, not a network.
KELVIN_RES_SYMBOL_RE = re.compile(r"^R_SHUNT($|_)")

#: Pin names that mark a switching-**controller** IC. Deliberately narrow: the
#: generic rails (``VCC``, ``EN``, ``VIN``, ``VOUT``) and the SPI select names
#: (``CS``, chip-select) are **excluded** because they put ordinary MCUs, flash
#: chips and LDOs one coincidence away from being called a controller, and a
#: false power stage poisons every downstream phase (plan bail-out 2).
CONTROLLER_PIN_NAMES = frozenset({
    # feedback / compensation
    "FB", "FBX", "VFB", "FEEDBACK", "COMP", "VC", "VCOMP",
    # current sense
    "SENSE", "ISENSE", "VSENSE", "SENSE+", "SENSE-", "CS+", "CS-",
    # gate drive
    "GATE", "DRV", "DRIVE", "DRVH", "DRVL", "HO", "LO", "TG", "BG", "GH", "GL",
    # switch node / bootstrap brought out of an integrated switcher
    "SW", "LX", "PH", "BOOT", "BST",
    # housekeeping unique to switchers
    "INTVCC", "RT", "FREQ", "FSW", "SS",
})

#: The controller pins that drive an external switch's gate or base.
DRIVE_PIN_NAMES = frozenset({
    "GATE", "DRV", "DRIVE", "DRVH", "DRVL", "HO", "LO", "TG", "BG", "GH", "GL",
})

#: The controller's current-sense input.
SENSE_PIN_NAMES = frozenset({"SENSE", "ISENSE", "VSENSE", "CS", "CSENSE"})

#: The controller's feedback input.
FEEDBACK_PIN_NAMES = frozenset({"FB", "FBX", "VFB", "FEEDBACK"})

#: The controller's genuinely **high-impedance** pins -- feedback, loop
#: compensation, oscillator timing, soft start. The network hanging off these is
#: what a datasheet means by "keep small-signal components away from the switch
#: node", and it is exactly this set (not every non-power pin) that defines it:
#: ``INTVCC`` is a bypassed regulator output and ``UVLO`` a stiff divider off the
#: input rail, so neither belongs here.
SMALL_SIGNAL_PIN_NAMES = FEEDBACK_PIN_NAMES | frozenset({
    "VC", "VCOMP", "COMP", "RT", "FREQ", "FSW", "SS",
})

#: The controller's supply input.
SUPPLY_PIN_NAMES = frozenset({"VIN", "VCC", "VDD", "VS", "VBIAS", "VIN+"})

#: The controller's ground reference. Finding ground this way -- *the net on the
#: controller's pin named GND* -- is what replaces ``GND_NET_RE``.
GROUND_PIN_NAMES = frozenset({
    "GND", "VSS", "AGND", "DGND", "PGND", "SGND", "GNDA", "GNDD", "VEE",
})


# --------------------------------------------------------------------------- #
# Pin-name normalisation
# --------------------------------------------------------------------------- #

_SUBSCRIPT_RE = re.compile(r"_\{([^}]*)\}")


def pin_name_tokens(name) -> frozenset[str]:
    """Normalised alternatives a KiCad pin name stands for.

    KiCad decorates pin names: ``~{SHDN}/UVLO`` is an active-low ``SHDN`` that is
    also the ``UVLO`` input, ``V_{SS}`` is ``VSS``, ``GPIO26/ADC0`` is one pin
    with two roles. Overlines (``~{...}``) and subscripts (``_{...}``) are
    rendering, not identity, so they are stripped; ``/`` separates alternatives,
    so each side becomes its own token.

    >>> sorted(pin_name_tokens("~{SHDN}/UVLO"))
    ['SHDN', 'UVLO']
    >>> sorted(pin_name_tokens("V_{SS}"))
    ['VSS']
    """
    text = str(name or "").strip().upper()
    if not text:
        return frozenset()
    text = _SUBSCRIPT_RE.sub(r"\1", text)
    text = text.replace("~", "").replace("{", "").replace("}", "")
    return frozenset(part.strip() for part in text.split("/") if part.strip())


def _symbol_name(part) -> str:
    return str(getattr(part, "name", "") or "").strip().upper()


def _pin_count(part) -> int:
    try:
        return len(part)
    except Exception:
        return len(getattr(part, "pins", []) or [])


def _part_pin_tokens(part) -> frozenset[str]:
    """Union of every pin's normalised tokens (duplicates collapsed)."""
    tokens: set[str] = set()
    for pin in getattr(part, "pins", []) or []:
        tokens |= pin_name_tokens(getattr(pin, "name", None))
    return frozenset(tokens)


# --------------------------------------------------------------------------- #
# Device typing (plan section 4.1)
# --------------------------------------------------------------------------- #

@dataclass
class PowerDevice:
    """One part, typed by its role in a power stage.

    ``kind`` is one of ``switch``, ``rectifier``, ``magnetics``, ``capacitor``,
    ``resistor``, ``controller``, ``sense_resistor``, ``fb_divider_top``,
    ``fb_divider_bottom``, ``input_cap``, ``output_cap``, ``unknown``. The first
    six are the *device type* (what the part is); the rest are *derived roles*
    (what the part does in this stage) and replace the device type on the stage's
    own device list.
    """

    ref: str
    kind: str
    confidence: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
            "reasons": list(self.reasons),
        }


def _device_kind(part) -> tuple[str, float, list[str]]:
    """``(kind, confidence, reasons)`` from library facts only.

    First match wins, in the plan's order. Anything unmatched is ``"unknown"`` --
    the reference-designator prefix is never consulted, so an unrecognised part
    stays unrecognised rather than being guessed at.
    """
    symbol = _symbol_name(part)
    n_pins = _pin_count(part)
    tokens = _part_pin_tokens(part)

    if DENY_SYMBOL_RE.match(symbol):
        return "unknown", 0.0, [f"symbol {symbol!r} is on the power deny-list"]

    if 3 <= n_pins <= 8:
        if FET_TERMINALS <= tokens:
            return "switch", 1.0, ["pin names include the FET triple G/D/S"]
        if BJT_TERMINALS <= tokens:
            return "switch", 1.0, ["pin names include the BJT triple B/C/E"]

    if n_pins == 2 and tokens == DIODE_TERMINALS:
        return "rectifier", 1.0, ["two pins named A (anode) and K (cathode)"]

    windings = _winding_groups(part)
    if len(windings) >= 2:
        return (
            "magnetics",
            1.0,
            [f"{len(windings)} windings from AA/AB/SA/SB-style pin names"],
        )
    if IND_SYMBOL_RE.match(symbol) and n_pins in (2, 4):
        return "magnetics", 0.9, [f"inductor symbol {symbol!r}"]

    if CAP_SYMBOL_RE.match(symbol) and n_pins == 2:
        return "capacitor", 0.9, [f"capacitor symbol {symbol!r}"]

    if RES_SYMBOL_RE.match(symbol) and (
        n_pins == 2 or (KELVIN_RES_SYMBOL_RE.match(symbol) and n_pins == 4)
    ):
        return "resistor", 0.9, [f"resistor symbol {symbol!r}"]

    if n_pins >= 5:
        hits = sorted(tokens & CONTROLLER_PIN_NAMES)
        if len(hits) >= 2:
            return (
                "controller",
                min(1.0, 0.6 + 0.1 * len(hits)),
                [f"{n_pins}-pin IC with switcher pins {', '.join(hits)}"],
            )

    return "unknown", 0.0, []


def _winding_groups(part) -> dict[str, list[int]]:
    """Winding id -> pin indices, for a multi-winding magnetic.

    Recognises the ``AA``/``AB``/``SA``/``SB`` convention (``Transformer_1P_1S``)
    and the ``L_Coupled`` family's plain ``1 2 3 4``. The permuted
    ``L_Coupled_1324``-style variants are **not** decoded -- they fall back to a
    single unknown winding, which loses only the winding-partner hop.
    """
    groups: dict[str, list[int]] = {}
    for index, pin in enumerate(getattr(part, "pins", []) or []):
        for token in pin_name_tokens(getattr(pin, "name", None)):
            match = WINDING_PIN_RE.match(token)
            if match:
                groups.setdefault(match.group(1), []).append(index)
                break
    if groups:
        return groups
    # KiCad's plain L_Coupled numbers winding 1 as pins 1-2, winding 2 as 3-4.
    # The permuted L_Coupled_1324-family variants reorder them and are not
    # decoded here.
    if _symbol_name(part) in ("L_COUPLED", "L_COUPLED_SMALL") and _pin_count(part) == 4:
        return {"1": [0, 1], "2": [2, 3]}
    return {}


def classify_devices(circuit) -> dict[str, PowerDevice]:
    """Device type for every part in ``circuit``, keyed by reference.

    The device-typing half of the module on its own -- no connectivity, no
    stages. Useful for inspection and as the unit under test for the anti-cheat
    gate.
    """
    result: dict[str, PowerDevice] = {}
    for part in getattr(circuit, "parts", None) or []:
        ref = getattr(part, "ref", None)
        if ref is None:
            continue
        kind, confidence, reasons = _device_kind(part)
        result[str(ref)] = PowerDevice(str(ref), kind, confidence, reasons)
    return result


# --------------------------------------------------------------------------- #
# The connectivity view
# --------------------------------------------------------------------------- #

class _View:
    """One pass over the circuit, giving connectivity keyed by net *name*.

    Everything is ``getattr``-based so a
    :class:`~skidl_layout.snapshot.SnapshotCircuit` works identically to a live
    ``Circuit`` (that is what the anti-cheat scrambler is built on). The net walk
    is ``context.py``'s canonical one, NC filter included.
    """

    def __init__(self, circuit):
        self.parts = list(getattr(circuit, "parts", None) or [])
        self.order: dict[str, int] = {}
        self.part_by_ref: dict[str, object] = {}
        for index, part in enumerate(self.parts):
            ref = getattr(part, "ref", None)
            if ref is None:
                continue
            self.order[str(ref)] = index
            self.part_by_ref[str(ref)] = part

        # net name -> [(ref, pin tokens)] in net.get_pins() order
        self.net_pins: dict[str, list[tuple[str, frozenset[str]]]] = {}
        # ref -> [(pin tokens, net name)] in the order the nets were walked
        self.part_pins: dict[str, list[tuple[frozenset[str], str]]] = {}
        self.net_order: list[str] = []
        self.net_index: dict[str, int] = {}

        for net in getattr(circuit, "get_nets", list)():
            if is_nc_net(net):
                continue
            name = str(getattr(net, "name", "") or "")
            if not name:
                continue
            entries = self.net_pins.setdefault(name, [])
            if name not in self.net_index:
                self.net_index[name] = len(self.net_order)
                self.net_order.append(name)
            for pin in net.get_pins():
                ref = getattr(getattr(pin, "part", None), "ref", None)
                if ref is None:
                    continue
                tokens = pin_name_tokens(getattr(pin, "name", None))
                entries.append((str(ref), tokens))
                self.part_pins.setdefault(str(ref), []).append((tokens, name))

    # -- queries ------------------------------------------------------------
    def refs_on(self, net: str) -> list[str]:
        seen: list[str] = []
        for ref, _tokens in self.net_pins.get(net, ()):
            if ref not in seen:
                seen.append(ref)
        return seen

    def pin_count(self, net: str) -> int:
        return len(self.net_pins.get(net, ()))

    def nets_of(self, ref: str) -> list[str]:
        seen: list[str] = []
        for _tokens, net in self.part_pins.get(ref, ()):
            if net not in seen:
                seen.append(net)
        return seen

    def nets_of_pins(self, ref: str, wanted: frozenset[str]) -> list[str]:
        """Nets on ``ref``'s pins whose name tokens intersect ``wanted``."""
        seen: list[str] = []
        for tokens, net in self.part_pins.get(ref, ()):
            if tokens & wanted and net not in seen:
                seen.append(net)
        return seen

    def nets_excluding_pins(self, ref: str, unwanted: frozenset[str]) -> list[str]:
        seen: list[str] = []
        for tokens, net in self.part_pins.get(ref, ()):
            if not (tokens & unwanted) and net not in seen:
                seen.append(net)
        return seen

    def sorted_refs(self, refs) -> list[str]:
        """Refs in circuit-part order -- never alphabetical (that is naming)."""
        return sorted(set(refs), key=lambda r: self.order.get(r, 1 << 30))


# --------------------------------------------------------------------------- #
# Result shapes
# --------------------------------------------------------------------------- #

@dataclass
class CommutationLoop:
    """The high-di/dt loop: the parts whose current stops when the switch opens.

    ``member_refs`` is ordered around the loop starting at the capacitor. The
    loop closes **through ground** -- ``returns_through`` names that net -- so the
    return conductor is implied rather than listed.
    """

    member_refs: list[str]
    net_names: list[str]
    returns_through: str
    bulk_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "member_refs": list(self.member_refs),
            "net_names": list(self.net_names),
            "returns_through": self.returns_through,
            "bulk_refs": list(self.bulk_refs),
            "confidence": round(self.confidence, 3),
            "reasons": list(self.reasons),
        }


@dataclass
class PowerStage:
    """One switching converter stage, anchored on a controller and its switch."""

    controller_ref: str | None
    topology: str
    switch_node_nets: list[str]
    input_rail: str | None
    output_rail: str | None
    ground_net: str | None
    devices: list[PowerDevice]
    loops: list[CommutationLoop]
    feedback_divider: tuple[str, str] | None
    sense_resistor_ref: str | None
    #: The high-impedance feedback node (the controller's FB/FBX pin net).
    feedback_net: str | None = None
    #: The current-sense node -- the switch return that the controller measures.
    sense_net: str | None = None
    #: Parts hanging off the controller's high-impedance pins. These are the ones
    #: a datasheet wants kept away from the switch node.
    small_signal_refs: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def device(self, ref: str) -> PowerDevice | None:
        for dev in self.devices:
            if dev.ref == ref:
                return dev
        return None

    def refs_of_kind(self, kind: str) -> list[str]:
        return [dev.ref for dev in self.devices if dev.kind == kind]

    def to_dict(self) -> dict:
        return {
            "controller_ref": self.controller_ref,
            "topology": self.topology,
            "switch_node_nets": list(self.switch_node_nets),
            "input_rail": self.input_rail,
            "output_rail": self.output_rail,
            "ground_net": self.ground_net,
            "devices": [dev.to_dict() for dev in self.devices],
            "loops": [loop.to_dict() for loop in self.loops],
            "feedback_divider": (
                list(self.feedback_divider) if self.feedback_divider else None
            ),
            "sense_resistor_ref": self.sense_resistor_ref,
            "feedback_net": self.feedback_net,
            "sense_net": self.sense_net,
            "small_signal_refs": list(self.small_signal_refs),
            "reasons": list(self.reasons),
        }


@dataclass
class PowerStagePlan:
    """What :func:`classify_power_roles` found. Empty is the normal answer for a
    board that has no switching converter on it."""

    stages: list[PowerStage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.warnings

    def to_dict(self) -> dict:
        return {
            "stages": [stage.to_dict() for stage in self.stages],
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        """Human-readable summary, or ``""`` when nothing was found.

        Silence is deliberate: a report-only classifier that says nothing about a
        non-power board is doing its job, and its caller appends this line only
        when it is non-empty.
        """
        if not self.stages and not self.warnings:
            return ""
        lines = ["Power stage plan:"]
        for stage in self.stages:
            lines.append(
                f"  {stage.topology} stage on {stage.controller_ref}: "
                f"in={stage.input_rail} out={stage.output_rail} "
                f"sw={', '.join(stage.switch_node_nets) or 'n/a'} "
                f"gnd={stage.ground_net}"
            )
            for loop in stage.loops:
                lines.append(
                    f"    commutation loop: {' -> '.join(loop.member_refs)} "
                    f"(returns through {loop.returns_through})"
                )
                if loop.bulk_refs:
                    lines.append(f"      bulk: {', '.join(loop.bulk_refs)}")
            if stage.sense_resistor_ref:
                lines.append(f"    sense resistor: {stage.sense_resistor_ref}")
            if stage.feedback_divider:
                top, bottom = stage.feedback_divider
                lines.append(f"    feedback divider: {top} (top) / {bottom} (bottom)")
        if self.warnings:
            lines.append("  Warnings:")
            for warning in self.warnings[:20]:
                lines.append(f"    {warning}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Net roles (plan section 4.2)
# --------------------------------------------------------------------------- #

def _find_ground(view: _View, kinds: dict[str, PowerDevice]) -> str | None:
    """The ground net, found as *the net on a controller's ``GND``/``VSS`` pin*.

    That is a library fact; ``GND_NET_RE`` is not. Where several controllers
    disagree, or where there is no controller at all, the fallback is the net
    with the most pins -- ground is the hub of any real board.
    """
    candidates: list[str] = []
    for ref, dev in kinds.items():
        if dev.kind != "controller":
            continue
        candidates.extend(view.nets_of_pins(ref, GROUND_PIN_NAMES))
    if not candidates:
        candidates = list(view.net_order)
    if not candidates:
        return None
    # Most pins wins; ties broken by the circuit's own net order, never by name.
    return max(
        dict.fromkeys(candidates),
        key=lambda net: (view.pin_count(net), -view.net_index.get(net, 1 << 30)),
    )


def _caps_to(view: _View, kinds, net: str, other: str) -> list[str]:
    """Capacitors bridging ``net`` and ``other`` (normally a rail and ground)."""
    found = []
    for ref in view.refs_on(net):
        dev = kinds.get(ref)
        if dev is None or dev.kind != "capacitor":
            continue
        if other in view.nets_of(ref) and other != net:
            found.append(ref)
    return view.sorted_refs(found)


def _two_terminal_other_net(view: _View, ref: str, net: str) -> str | None:
    others = [n for n in view.nets_of(ref) if n != net]
    return others[0] if len(others) == 1 else None


def _winding_partner_net(view: _View, part, ref: str, net: str) -> str | None:
    """The other end of the winding whose one end sits on ``net``.

    A 2-pin inductor's partner is simply its other terminal. A transformer's is
    the terminal sharing its winding id (``AB`` -> ``AA``, ``SA`` -> ``SB``).
    """
    groups = _winding_groups(part)
    if not groups:
        return _two_terminal_other_net(view, ref, net)
    pins = list(getattr(part, "pins", []) or [])
    index_of_net: dict[int, str] = {}
    for index, pin in enumerate(pins):
        pin_net = getattr(getattr(pin, "net", None), "name", None)
        if pin_net:
            index_of_net[index] = str(pin_net)
    for _group, indices in groups.items():
        nets = [index_of_net.get(i) for i in indices]
        if net in nets:
            partners = [n for n in nets if n and n != net]
            if len(partners) == 1:
                return partners[0]
    return None


def _small_signal_refs(view: _View, controller_ref: str, ground) -> list[str]:
    """Parts on the controller's high-impedance pins, plus private continuations.

    A compensation network is usually two parts deep (``VC -> RC -> CC1``), and
    the junction between them is a **private** net -- exactly two pins, nothing
    else on it. That privacy test is what lets the walk take the second hop
    without escaping onto the output rail through the feedback divider's top
    resistor, whose far net is shared with half the board.
    """
    found: list[str] = []
    frontier: list[str] = []
    for net in view.nets_of_pins(controller_ref, SMALL_SIGNAL_PIN_NAMES):
        if net == ground:
            continue
        for ref in view.refs_on(net):
            if ref != controller_ref and ref not in found:
                found.append(ref)
                frontier.append(ref)
    for ref in frontier:
        for net in view.nets_of(ref):
            if net == ground or view.pin_count(net) != 2:
                continue
            for neighbour in view.refs_on(net):
                if neighbour != ref and neighbour != controller_ref \
                        and neighbour not in found:
                    found.append(neighbour)
    return view.sorted_refs(found)


def _loop_capacitor(view: _View, part_by_ref, candidates: list[str]):
    """Pick the capacitor that closes the commutation loop, and the rest as bulk.

    Order of preference, most defensible first:

    1. **Symbol identity** -- a non-polarized ``C`` before a ``C_Polarized``. An
       electrolytic is never the high-frequency element of a hot loop, and the
       symbol says which is which. This is a library fact and on the LT3757
       reference board it decides the answer on its own.
    2. **Value** -- smallest capacitance, i.e. the HF ceramic. This is the *only*
       use of ``part.value`` in the module and it is a tie-break, never a
       necessary condition.
    3. Circuit part order, so the result is deterministic. Never the reference
       designator.

    Returns ``(chosen, bulk, chosen_by_value)`` -- the last flag is true when the
    symbol identity did not separate the candidates and the value alone decided.
    """
    def key(ref):
        part = part_by_ref.get(ref)
        polarized = "POLARIZED" in _symbol_name(part)
        value = cap_value_uf(part)
        return (
            1 if polarized else 0,
            value if value is not None else float("inf"),
            view.order.get(ref, 1 << 30),
        )

    ordered = sorted(candidates, key=key)
    if not ordered:
        return None, []
    # Say so when the answer rested on the value, not on the symbol: a rail
    # shared with another consumer (an LDO's input cap sitting on the same node)
    # can hand the loop a capacitor that belongs to something else, and the
    # downstream phase should be able to see that and widen to ``bulk_refs``.
    by_value = len(ordered) > 1 and key(ordered[0])[0] == key(ordered[1])[0]
    return ordered[0], ordered[1:], by_value


# --------------------------------------------------------------------------- #
# Stage assembly
# --------------------------------------------------------------------------- #

def _build_stage(view: _View, kinds, ground, controller_ref, warnings):
    """Assemble the stage driven by ``controller_ref``, or ``None``.

    The stage is **anchored on the controller-to-switch gate connection**: the
    controller's ``GATE``/``DRV`` pin and a switch device's gate or base must
    share a net. That is a deliberately strict entry condition -- it is what keeps
    an MCU board with a reset transistor, or an avalanche pulser's discharge
    transistor, from being reported as a converter (plan gate G3).
    """
    reasons: list[str] = []
    drive_nets = view.nets_of_pins(controller_ref, DRIVE_PIN_NAMES)
    if not drive_nets:
        return None, "no gate-drive pin (integrated-switch topologies are not classified in Phase 1)"

    switches = []
    for net in drive_nets:
        for ref in view.refs_on(net):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "switch" or ref in switches:
                continue
            if net in view.nets_of_pins(ref, SWITCH_CONTROL_PINS):
                switches.append(ref)
    if not switches:
        return None, "gate-drive pin reaches no external switch device"
    switches = view.sorted_refs(switches)
    reasons.append(
        f"controller drive pin shares a net with the gate/base of {', '.join(switches)}"
    )

    # -- the switch node: a switch power terminal that also touches magnetics --
    switch_ref = switches[0]
    power_nets = view.nets_excluding_pins(switch_ref, SWITCH_CONTROL_PINS)
    switch_node = None
    magnetics_ref = None
    for net in power_nets:
        for ref in view.refs_on(net):
            if ref != switch_ref and kinds.get(ref) and kinds[ref].kind == "magnetics":
                switch_node, magnetics_ref = net, ref
                break
        if switch_node:
            break
    if switch_node is None:
        return None, "no net joins the switch to a magnetic component"
    reasons.append("switch node carries both a switch power terminal and a winding")

    return_nets = [n for n in power_nets if n != switch_node]

    # -- the sense node and its resistor ------------------------------------
    sense_nets = view.nets_of_pins(controller_ref, SENSE_PIN_NAMES)
    sense_node = next((n for n in return_nets if n in sense_nets), None)
    sense_resistor = None
    if sense_node is not None:
        for ref in view.refs_on(sense_node):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "resistor":
                continue
            if ground is not None and ground in view.nets_of(ref):
                sense_resistor = ref
                break
        if sense_resistor:
            reasons.append("sense resistor bridges the switch return and ground")

    # -- rails ---------------------------------------------------------------
    magnetics_part = view.part_by_ref.get(magnetics_ref)
    winding_partner = _winding_partner_net(view, magnetics_part, magnetics_ref, switch_node)

    input_rail = None
    for net in view.nets_of_pins(controller_ref, SUPPLY_PIN_NAMES):
        if net == ground:
            continue
        if magnetics_ref in view.refs_on(net) and _caps_to(view, kinds, net, ground):
            input_rail = net
            break
    if input_rail is None and winding_partner not in (None, ground):
        if _caps_to(view, kinds, winding_partner, ground):
            input_rail = winding_partner

    # A rectifier fed from the switch node (non-isolated) or from a winding
    # terminal (isolated secondary) delivers the output rail on its cathode.
    magnet_nets = [n for n in view.nets_of(magnetics_ref) if n != ground]
    output_rail = None
    output_rectifier = None
    for net in [switch_node] + [n for n in magnet_nets if n != switch_node]:
        for ref in view.refs_on(net):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "rectifier":
                continue
            if net not in view.nets_of_pins(ref, frozenset({"A"})):
                continue          # the anode must face the stage, not the load
            cathode = next(iter(view.nets_of_pins(ref, frozenset({"K"}))), None)
            if cathode in (None, ground) or cathode == input_rail:
                continue
            if not _caps_to(view, kinds, cathode, ground):
                continue
            output_rail, output_rectifier = cathode, ref
            break
        if output_rail:
            break

    # -- the feedback divider ------------------------------------------------
    feedback_nets = view.nets_of_pins(controller_ref, FEEDBACK_PIN_NAMES)
    feedback_node = feedback_nets[0] if feedback_nets else None
    divider = None
    if feedback_node is not None:
        top = bottom = None
        for ref in view.sorted_refs(view.refs_on(feedback_node)):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "resistor":
                continue
            other = _two_terminal_other_net(view, ref, feedback_node)
            if other is None:
                continue
            if other == ground and bottom is None:
                bottom = ref
            elif output_rail is not None and other == output_rail and top is None:
                top = ref
        if top and bottom:
            divider = (top, bottom)
            reasons.append(
                "feedback divider straddles the output rail and ground at the FB pin"
            )

    # -- the commutation loop ------------------------------------------------
    loops = []
    loop_reasons: list[str] = []
    partner_ref = None
    far_net = None
    if output_rectifier is not None and switch_node in view.nets_of(output_rectifier):
        # Non-isolated: current alternates between the switch and the rectifier
        # that shares its node, and the loop closes on the output capacitor.
        partner_ref, far_net = output_rectifier, output_rail
        loop_reasons.append("rectifier shares the switch node -- output-side loop")
    elif winding_partner not in (None, ground):
        # Isolated / transformer primary: nothing else sits on the switch node,
        # so the loop closes on the capacitor at the other end of the winding.
        partner_ref, far_net = magnetics_ref, winding_partner
        loop_reasons.append(
            "no rectifier on the switch node -- loop closes through the winding"
        )

    if partner_ref is not None and far_net is not None and ground is not None:
        cap_refs = _caps_to(view, kinds, far_net, ground)
        cap_ref, bulk, by_value = _loop_capacitor(view, view.part_by_ref, cap_refs)
        if by_value:
            loop_reasons.append(
                "several candidate capacitors were separable only by value -- "
                "the rail may be shared with another consumer"
            )
        if cap_ref is not None:
            members = [cap_ref, partner_ref, switch_ref]
            nets = [far_net, switch_node]
            if sense_resistor is not None:
                members.append(sense_resistor)
                nets.append(sense_node)
            loops.append(CommutationLoop(
                member_refs=members,
                net_names=nets,
                returns_through=ground,
                bulk_refs=bulk,
                confidence=0.9 if sense_resistor else 0.7,
                reasons=loop_reasons,
            ))

    # -- topology (a stretch goal; "unknown" is an acceptable answer) --------
    topology = "unknown"
    if len(_winding_groups(magnetics_part)) >= 2:
        topology = "flyback"
    elif output_rectifier is not None and switch_node in view.nets_of(output_rectifier):
        if winding_partner is not None and winding_partner == input_rail:
            topology = "boost"
        elif winding_partner is not None and winding_partner == output_rail:
            topology = "buck"

    # -- assemble the device list -------------------------------------------
    devices: list[PowerDevice] = []

    def add(ref, kind=None, reason=None):
        if ref is None or any(d.ref == ref for d in devices):
            return
        base = kinds.get(ref)
        if base is None:
            return
        dev = PowerDevice(ref, kind or base.kind, base.confidence, list(base.reasons))
        if kind and kind != base.kind:
            dev.reasons.append(reason or f"derived role in this stage: {kind}")
            dev.confidence = 0.9
        devices.append(dev)

    add(controller_ref)
    for ref in switches:
        add(ref)
    add(magnetics_ref)
    if output_rectifier:
        add(output_rectifier)
    if sense_resistor:
        add(sense_resistor, "sense_resistor",
            "one terminal on the switch return / controller SENSE pin, the other on ground")
    if divider:
        add(divider[0], "fb_divider_top", "feedback node to the output rail")
        add(divider[1], "fb_divider_bottom", "feedback node to ground")
    if input_rail:
        for ref in _caps_to(view, kinds, input_rail, ground):
            add(ref, "input_cap", "bridges the input rail and ground")
    if output_rail:
        for ref in _caps_to(view, kinds, output_rail, ground):
            add(ref, "output_cap", "bridges the output rail and ground")

    stage = PowerStage(
        controller_ref=controller_ref,
        topology=topology,
        switch_node_nets=[switch_node],
        input_rail=input_rail,
        output_rail=output_rail,
        ground_net=ground,
        devices=devices,
        loops=loops,
        feedback_divider=divider,
        sense_resistor_ref=sense_resistor,
        feedback_net=feedback_node,
        sense_net=sense_node,
        small_signal_refs=_small_signal_refs(view, controller_ref, ground),
        reasons=reasons,
    )
    if len(switches) > 1:
        warnings.append(
            f"{controller_ref}: {len(switches)} switches on the drive net; "
            f"the loop is reported for {switch_ref} only"
        )
    return stage, None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def classify_power_roles(circuit, ctx=None) -> PowerStagePlan:
    """Name the power-electronics roles in ``circuit``.

    ``ctx`` is accepted for symmetry with
    :func:`~skidl_layout.power.plan_power_routes` and is currently unused: this
    module needs pin *names*, which :class:`~skidl_layout.context.LayoutContext`
    does not carry, so it does its own single walk.

    Returns an empty :class:`PowerStagePlan` for anything that is not a switching
    converter. That is the intended answer, not a failure -- a quiet classifier is
    shippable, a noisy one poisons every phase that consumes it.
    """
    if circuit is None:
        return PowerStagePlan()

    view = _View(circuit)
    kinds = classify_devices(circuit)
    ground = _find_ground(view, kinds)

    warnings: list[str] = []
    has_magnetics = any(dev.kind == "magnetics" for dev in kinds.values())
    stages: list[PowerStage] = []
    for ref in view.sorted_refs(
        r for r, dev in kinds.items() if dev.kind == "controller"
    ):
        stage, why_not = _build_stage(view, kinds, ground, ref, warnings)
        if stage is not None:
            stages.append(stage)
        elif has_magnetics:
            # Only worth saying on a board that has magnetics at all; otherwise
            # every MCU whose IC happens to match the controller pin set would
            # generate noise.
            warnings.append(f"{ref}: {why_not}")

    return PowerStagePlan(stages=stages, warnings=warnings)
