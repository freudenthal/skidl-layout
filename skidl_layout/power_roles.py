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
    "SW", "LX", "PH", "BOOT", "BST", "BOOST",
    # housekeeping unique to switchers
    "INTVCC", "RT", "FREQ", "FSW", "SS",
})

#: The controller pins that drive an external switch's gate or base.
#:
#: ⚠⚠ This table is what *anchors a stage* -- a part with one of these names and
#: a MOSFET on the far end becomes a power stage, and a false stage poisons every
#: phase downstream. Widen it only with a name that appears on a real shipping
#: part, and never with a generic one. ``OUT`` is the standing example of what
#: must stay out: it is a genuine UC384x pin name and also the most common pin
#: name in any symbol library, so it needs a compound rule (``OUT`` on a part
#: that *also* has ``FB`` and ``CS``) gated on its own (Phase-7 bail-out 3).
DRIVE_PIN_NAMES = frozenset({
    "GATE", "DRV", "DRIVE", "DRVH", "DRVL", "HO", "LO", "TG", "BG", "GH", "GL",
    "DR",             # LM3478 / LM3488 -- the whole gate-drive pin name
})

#: Drive-pin names admitted **only under a compound condition** (Phase 9, WS-4).
#:
#: ``OUT`` is a genuine UC384x gate-drive pin name and simultaneously the most
#: generic pin name in any symbol library, so it cannot join
#: :data:`DRIVE_PIN_NAMES` on its own: an op-amp, a comparator, a regulator and a
#: logic gate all have an ``OUT``, and a stage invented on one of those poisons
#: every phase downstream. It counts only on a part that *also* carries a
#: feedback pin **and** a current-sense pin -- ``OUT`` and ``FB`` and ``CS``
#: together are a switching controller's signature, not a coincidence.
#: ``Regulator_Controller:UC3844_DIP8`` ships ``COMP FB CS RC GND OUT VCC VREF``,
#: which is exactly that conjunction (verified against the shipped symbol, not
#: against a datasheet -- see ``FEEDBACK_PIN_NAMES``' note on ``SET``).
#:
#: Set this to ``frozenset()`` to measure the pre-WS-4 behaviour; that is what
#: ``drive_phase9.py`` does to keep "classifies silent *before* the rule"
#: permanently re-measurable rather than a recorded number.
COMPOUND_DRIVE_PIN_NAMES = frozenset({"OUT"})

#: What else the same part must carry before a :data:`COMPOUND_DRIVE_PIN_NAMES`
#: pin counts as gate drive. **Every** group must be present.
COMPOUND_DRIVE_REQUIRES: tuple[frozenset[str], ...] = ()   # bound below

#: Device kinds a stage anchor may walk **through**, one hop, between the
#: controller's drive pin and the switch's gate (Phase 9, WS-3).
#:
#: A series gate resistor is ubiquitous -- the LT3724's own front-page circuit
#: puts 10 ohm between ``TG`` and the MOSFET gate -- and before this hop existed
#: any such board classified as **0 stages** with "gate-drive pin reaches no
#: external switch device". It is the same one-hop walk ``_series_reachable``
#: already does for the rail code.
#:
#: ⚠⚠ Restricted to ``resistor`` on purpose, and this is the risky table in the
#: module. This hop runs at the **anchor**, so a false positive does not
#: mis-*describe* a stage, it **invents** one. A capacitor between a drive pin
#: and a gate is a different circuit (AC coupling, a snubber), and admitting any
#: 2-pin part is how a false anchor gets built. Set to ``frozenset()`` to
#: disable the hop entirely.
DRIVE_SERIES_HOP_KINDS = frozenset({"resistor"})

#: The controller's current-sense input. Unlike :data:`DRIVE_PIN_NAMES` this set
#: only *refines* a stage that has already been anchored, so widening it cannot
#: create a false positive.
#: ⚠ ``ISP``/``ISN`` are deliberately ABSENT. On the LT8710 they are a second,
#: differential sense pair that measures **output** current for its current-control
#: loop -- a different quantity from the cycle-by-cycle switch current this table
#: is looking for. Admitting them would let the classifier pick the output shunt
#: as "the sense resistor" on any part that has both.
SENSE_PIN_NAMES = frozenset({
    "SENSE", "ISENSE", "VSENSE", "CS", "CSENSE",
    "SENSE+", "SENSE-",   # LT3724, LT3844 (differential Kelvin sense pair)
    "CSP", "CSN",         # LT8710 (switch-current sense pair)
})

#: Device kinds the **sense node** may walk through, one hop, between the
#: controller's sense pin and the switch's return terminal (Phase 10, WS-4).
#:
#: A UC384x's leading-edge blanking filter is near-universal: a series resistor
#: from the shunt to ``CS`` with a small capacitor to ground, suppressing the
#: turn-on current spike. It puts the ``CS`` pin one resistor away from the
#: shunt, and the positional sense test requires the two to share a net -- so
#: ``uc3844_flyback`` reported ``sense_resistor=None`` while its
#: ``cs_filter=False`` twin reported ``RS``. Structurally the same defect
#: :data:`DRIVE_SERIES_HOP_KINDS` fixed at the anchor, one refinement step later.
#:
#: ⚠ This hop is **strictly safer than the anchor's**, and the reason is the one
#: in :data:`SENSE_PIN_NAMES`'s own docstring: the sense table only *refines* a
#: stage that is already anchored, so a false positive here cannot invent a
#: stage -- at worst it mis-names one part of a stage that exists either way.
#: Restricted to ``resistor`` regardless, for the same reason the anchor is: the
#: *capacitor* in an RC filter goes to ground, and a capacitor in the series
#: position is a different circuit. Set to ``frozenset()`` to disable the hop.
SENSE_SERIES_HOP_KINDS = frozenset({"resistor"})

#: The controller's feedback input. Also a refinement set, not an anchor.
#:
#: ⚠ ``SET`` is here because KiCad's own ``Regulator_Controller:LTC1624CS8``
#: spells the feedback pin that way. The **LTC1624 datasheet does not** -- p.2
#: gives ``V_FB``, which this table already accepted. So the classifier was
#: right and the shipped library symbol is wrong; ``SET`` is admitted because it
#: is what a user of this stack actually binds, not because a datasheet says so.
FEEDBACK_PIN_NAMES = frozenset({
    "FB", "FBX", "VFB", "FEEDBACK",
    "SET",            # Regulator_Controller:LTC1624CS8 (library spelling; see above)
})

#: The controller's genuinely **high-impedance** pins -- feedback, loop
#: compensation, oscillator timing, soft start. The network hanging off these is
#: what a datasheet means by "keep small-signal components away from the switch
#: node", and it is exactly this set (not every non-power pin) that defines it:
#: ``INTVCC`` is a bypassed regulator output and ``UVLO`` a stiff divider off the
#: input rail, so neither belongs here.
#: ``ITH`` is the same thing ``VC``/``COMP`` are -- the error amplifier's output,
#: the highest-impedance node on the part -- under Linear Technology's naming
#: (``LTC1624`` pin ``I_TH/RUN``, ``LTC1871`` pin ``ITH``). It is deliberately
#: **not** added to :data:`CONTROLLER_PIN_NAMES`: this set refines a stage that
#: already exists, while that one helps decide a part *is* a controller.
SMALL_SIGNAL_PIN_NAMES = FEEDBACK_PIN_NAMES | frozenset({
    "VC", "VCOMP", "COMP", "RT", "FREQ", "FSW", "SS", "ITH",
})

#: The controller's supply input.
SUPPLY_PIN_NAMES = frozenset({"VIN", "VCC", "VDD", "VS", "VBIAS", "VIN+"})

#: The controller's ground reference. Finding ground this way -- *the net on the
#: controller's pin named GND* -- is what replaces ``GND_NET_RE``.
GROUND_PIN_NAMES = frozenset({
    "GND", "VSS", "AGND", "DGND", "PGND", "SGND", "GNDA", "GNDD", "VEE",
})

# ``COMPOUND_DRIVE_REQUIRES`` is declared next to ``COMPOUND_DRIVE_PIN_NAMES``
# above, where it is documented, and bound here because it names two tables that
# are defined further down. A switching controller closes a voltage loop and a
# current loop; a part with a generic ``OUT`` that does neither is not one.
COMPOUND_DRIVE_REQUIRES = (FEEDBACK_PIN_NAMES, SENSE_PIN_NAMES)


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

    def pin_tokens(self, ref: str) -> frozenset[str]:
        """Every normalised pin-name token this part carries.

        What a *part* is, from its pin table alone -- the same library fact
        ``_device_kind`` reads, hoisted so the compound drive-pin rule
        (:data:`COMPOUND_DRIVE_PIN_NAMES`) can ask "does this part also have FB
        and CS" without re-walking the symbol.
        """
        out: set[str] = set()
        for tokens, _net in self.part_pins.get(ref, ()):
            out |= tokens
        return frozenset(out)

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
    #: **Every** ground this stage returns through, sorted (Phase 10, WS-4).
    #:
    #: ``ground_net`` above reports ONE, which is the right shape for a caller
    #: that wants "the" ground and wrong for a board that has two. On
    #: ``lt3724_buck`` the reported one is ``SGND`` while the hot loop physically
    #: returns through ``PGND``; Phase 9 shipped a ``split ground:`` warning
    #: naming both, which is honest but is prose. This is the field.
    #:
    #: ⛔ **Added, never renamed.** ``ground_net`` keeps its meaning and its
    #: value; on a single-ground board this list has exactly one member and it is
    #: that value, so nothing downstream can read a change that did not happen.
    ground_nets: list[str] = field(default_factory=list)
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
            "ground_nets": list(self.ground_nets),
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
                # Only when there is more than one: a single-ground board must
                # print exactly what it printed before this field existed.
                + (f" (grounds: {', '.join(stage.ground_nets)})"
                   if len(stage.ground_nets) > 1 else "")
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


def _find_grounds(view: _View, kinds: dict[str, PowerDevice], ground) -> frozenset[str]:
    """**Every** net a controller calls ground -- not just the primary one.

    ⚠⚠ Phase 9. ``_find_ground`` answers "which net do I report as *the* ground",
    which a stage still needs (a commutation loop returns through one net). But
    every *test* in this module -- does this rail have a capacitor to ground, does
    this rectifier freewheel from ground, is this the divider's bottom leg -- is
    asking a different question, and on a split-ground board the answer is "any
    of them".

    ``lt3724_buck`` is the board that proves it. Its LT3724 brings out ``SGND``
    and ``PGND`` as separate pins and no pin named ``GND`` at all, exactly as the
    datasheet's PCB Layout Checklist demands, and the two are tied at one point
    by a 0 ohm link. ``_find_ground`` picks ``SGND`` (10 pins beats 6) -- and then
    ``CIN`` returns to ``PGND``, ``D1``'s anode is on ``PGND``, and the classifier
    concluded there was no input rail, no output rail, no catch rectifier and no
    topology. It was not confused about the circuit; it was asking about one net
    on a board that has two.

    On every single-ground board this returns ``{ground}``, so nothing that
    already worked can move -- the same structural argument ``roles.GND_NET_RE``'s
    two named additions rest on.
    """
    found: list[str] = []
    for ref, dev in kinds.items():
        if dev.kind != "controller":
            continue
        for net in view.nets_of_pins(ref, GROUND_PIN_NAMES):
            if net not in found:
                found.append(net)
    if ground is not None and ground not in found:
        found.append(ground)
    return frozenset(found)


def _caps_to(view: _View, kinds, net: str, other) -> list[str]:
    """Capacitors bridging ``net`` and ``other`` (normally a rail and ground).

    ``other`` is a net name **or a set of them**. A split-ground board has more
    than one ground, and "does this rail have a capacitor to ground" has to be
    true when the capacitor returns to *either* of them: on ``lt3724_buck`` the
    input capacitor returns to ``PGND`` while the classifier's primary ground is
    ``SGND``, and asking about one net alone answered no to every rail on the
    board. With a single ground the set has one member and this is the original
    test exactly -- which is why the change cannot move a single-ground result.
    """
    targets = {other} if isinstance(other, str) else set(other or ())
    targets.discard(net)
    if not targets:
        return []
    found = []
    for ref in view.refs_on(net):
        dev = kinds.get(ref)
        if dev is None or dev.kind != "capacitor":
            continue
        if targets.intersection(view.nets_of(ref)):
            found.append(ref)
    return view.sorted_refs(found)


def _two_terminal_other_net(view: _View, ref: str, net: str) -> str | None:
    others = [n for n in view.nets_of(ref) if n != net]
    return others[0] if len(others) == 1 else None


def _series_reachable(view: _View, kinds, starts, ground) -> list[tuple[str, str | None]]:
    """``(net, through_ref)`` for each start net and each net one resistor away.

    A step-down's input rail is not something the magnetics can point at -- the
    inductor is on the *output* side -- so it has to be found from the switch's
    other power terminal instead. That terminal is either the input rail itself
    or one **high-side sense resistor** away from it (``LTC1624``: ``VIN -> RS ->
    the FET drain``), which is exactly the one extra hop this walk allows.
    ``through_ref`` is ``None`` for the start nets themselves, and the resistor's
    reference for the nets reached through one.

    Deliberately one hop deep: two would start walking feedback dividers and
    UVLO strings onto the power path.
    """
    grounds = {ground} if isinstance(ground, str) else set(ground or ())
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    for net in starts:
        if net is None or net in grounds or net in seen:
            continue
        seen.add(net)
        out.append((net, None))
    for net in starts:
        if net is None or net in grounds:
            continue
        for ref in view.refs_on(net):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "resistor":
                continue
            other = _two_terminal_other_net(view, ref, net)
            if other is None or other in grounds or other in seen:
                continue
            seen.add(other)
            out.append((other, ref))
    return out


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


def _sepic_family(view: _View, kinds, part_by_ref, switch_node, grounds,
                  magnetics_ref):
    """Is this a SEPIC or a Cuk, and if so what carries the output? (Phase 9, WS-5.)

    Both topologies have the same skeleton and differ in exactly one place, which
    is why they are decided together: a **coupling capacitor** bridges the switch
    node and a second node, and that second node carries a **second magnetic**.
    What the second magnetic's far terminal is then separates them:

    * far terminal on **ground** -> the output leaves through a rectifier on the
      coupling node, cathode on a positive rail: a **SEPIC**;
    * far terminal on **a rail with capacitors to ground** -> that winding *is*
      the output leg and the rectifier faces ground: a **Cuk** (the dual-inductor
      inverting converter, whose output is negative).

    ⚠⚠ Both of these were called ``flyback`` before this function existed, because
    ``topology`` keyed on **winding count alone** -- so every coupled-inductor
    SEPIC / Cuk / inverting converter in existence was mislabelled, and a
    *discrete*-inductor one got no topology at all. The coupling capacitor is what
    a winding count cannot see.

    ⛔ The "second node carries a magnetic" test is the safety, not a detail. A
    bootstrap capacitor also bridges the switch node and another node
    (``BOOST``/``SW`` on any high-side driver, ``lt3724_buck``'s ``CB``), and that
    node carries no winding -- so it is rejected here rather than by a name.

    Returns ``(family, coupling_cap_ref, second_magnetics_ref, mid_node,
    output_rail, output_rectifier)``; ``family`` is ``None`` when this is neither.
    """
    blank = (None, None, None, None, None, None)
    if switch_node is None or not grounds or magnetics_ref is None:
        return blank

    for cap_ref in view.refs_on(switch_node):
        dev = kinds.get(cap_ref)
        if dev is None or dev.kind != "capacitor":
            continue
        mid = _two_terminal_other_net(view, cap_ref, switch_node)
        if mid is None or mid in grounds or mid == switch_node:
            continue

        # The second magnetic: the same coupled part's other winding, or a
        # genuinely separate inductor. Both are real builds of both topologies
        # (the LT3757's SEPIC couples them, the LT8710's Cuk does not).
        second = next((r for r in view.refs_on(mid)
                       if kinds.get(r) and kinds[r].kind == "magnetics"), None)
        if second is None:
            continue
        partner = _winding_partner_net(view, part_by_ref.get(second), second, mid)
        if partner == switch_node:
            continue

        if partner in grounds:
            # SEPIC: the second winding returns to ground and the rectifier on
            # the coupling node delivers a POSITIVE rail.
            for ref in view.refs_on(mid):
                rect = kinds.get(ref)
                if rect is None or rect.kind != "rectifier":
                    continue
                if mid not in view.nets_of_pins(ref, frozenset({"A"})):
                    continue
                cathode = next(iter(view.nets_of_pins(ref, frozenset({"K"}))), None)
                if cathode is None or cathode in grounds or not _caps_to(
                        view, kinds, cathode, grounds):
                    continue
                return "sepic", cap_ref, second, mid, cathode, ref
            continue

        if partner is not None and _caps_to(view, kinds, partner, grounds):
            # Cuk / dual-inductor inverting: the second winding IS the output
            # leg. This is Phase-1 limitation L-3's board -- the output sits
            # beyond a SECOND magnetic, which the rail walk never reaches.
            return "cuk", cap_ref, second, mid, partner, None

    return blank


def _small_signal_refs(view: _View, controller_ref: str, ground) -> list[str]:
    """Parts on the controller's high-impedance pins, plus private continuations.

    A compensation network is usually two parts deep (``VC -> RC -> CC1``), and
    the junction between them is a **private** net -- exactly two pins, nothing
    else on it. That privacy test is what lets the walk take the second hop
    without escaping onto the output rail through the feedback divider's top
    resistor, whose far net is shared with half the board.
    """
    grounds = {ground} if isinstance(ground, str) else set(ground or ())
    found: list[str] = []
    frontier: list[str] = []
    for net in view.nets_of_pins(controller_ref, SMALL_SIGNAL_PIN_NAMES):
        if net in grounds:
            continue
        for ref in view.refs_on(net):
            if ref != controller_ref and ref not in found:
                found.append(ref)
                frontier.append(ref)
    for ref in frontier:
        for net in view.nets_of(ref):
            if net in grounds or view.pin_count(net) != 2:
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
        # ⚠ Three values, not two. Every caller unpacks three, and this path is
        # reached whenever the loop's far rail has no capacitor to the net the
        # classifier picked as ground -- which is exactly what a SPLIT GROUND
        # produces (``lt3724_buck``: ``CIN`` returns to ``PGND`` while the
        # classifier chose ``SGND``). Returning a 2-tuple here raised
        # ``ValueError: not enough values to unpack (expected 3, got 2)`` at the
        # one moment the classifier was about to give up quietly.
        return None, [], False
    # Say so when the answer rested on the value, not on the symbol: a rail
    # shared with another consumer (an LDO's input cap sitting on the same node)
    # can hand the loop a capacitor that belongs to something else, and the
    # downstream phase should be able to see that and widen to ``bulk_refs``.
    by_value = len(ordered) > 1 and key(ordered[0])[0] == key(ordered[1])[0]
    return ordered[0], ordered[1:], by_value


# --------------------------------------------------------------------------- #
# Stage assembly
# --------------------------------------------------------------------------- #

def _drive_pin_names(view: _View, controller_ref: str) -> frozenset[str]:
    """The drive-pin table ``controller_ref`` earns, compound rule included.

    :data:`DRIVE_PIN_NAMES` always; a :data:`COMPOUND_DRIVE_PIN_NAMES` entry only
    when the same part carries every group in :data:`COMPOUND_DRIVE_REQUIRES`.
    """
    if not COMPOUND_DRIVE_PIN_NAMES:
        return DRIVE_PIN_NAMES
    tokens = view.pin_tokens(controller_ref)
    earned = tokens & COMPOUND_DRIVE_PIN_NAMES
    if not earned:
        return DRIVE_PIN_NAMES
    if not all(tokens & required for required in COMPOUND_DRIVE_REQUIRES):
        return DRIVE_PIN_NAMES
    return DRIVE_PIN_NAMES | earned


def _switches_on(view: _View, kinds, nets) -> list[str]:
    """Switch devices whose gate/base sits on one of ``nets``."""
    found: list[str] = []
    for net in nets:
        for ref in view.refs_on(net):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "switch" or ref in found:
                continue
            if net in view.nets_of_pins(ref, SWITCH_CONTROL_PINS):
                found.append(ref)
    return found


def _gate_series_hops(view: _View, kinds, drive_nets) -> list[tuple[str, str]]:
    """``(net, through_ref)`` one permitted series element out of each drive net.

    The gate-resistor hop (:data:`DRIVE_SERIES_HOP_KINDS`). Deliberately one hop
    and deliberately kind-restricted; see that constant for why. The element must
    be a genuine two-terminal part -- ``_two_terminal_other_net`` returns ``None``
    for anything with a third net, so a resistor network cannot smuggle the walk
    somewhere else.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for net in drive_nets:
        for ref in view.refs_on(net):
            dev = kinds.get(ref)
            if dev is None or dev.kind not in DRIVE_SERIES_HOP_KINDS:
                continue
            other = _two_terminal_other_net(view, ref, net)
            if other is None or other in seen or other in drive_nets:
                continue
            seen.add(other)
            out.append((other, ref))
    return out


def _build_stage(view: _View, kinds, ground, controller_ref, warnings,
                 grounds=None):
    """Assemble the stage driven by ``controller_ref``, or ``None``.

    The stage is **anchored on the controller-to-switch gate connection**: the
    controller's ``GATE``/``DRV`` pin and a switch device's gate or base must
    share a net, **or be one series resistor apart** (Phase 9, WS-3 -- see
    :data:`DRIVE_SERIES_HOP_KINDS`). That is still a deliberately strict entry
    condition -- it is what keeps an MCU board with a reset transistor, or an
    avalanche pulser's discharge transistor, from being reported as a converter
    (plan gate G3).

    ``ground`` is the one net the stage *reports* returning through; ``grounds``
    is every net the controller calls ground (:func:`_find_grounds`). Every test
    below asks the second question, because a split-ground board answers "does
    this return to ground" with "yes, to one of them". They are the same set on
    every single-ground board.
    """
    grounds = frozenset(grounds if grounds is not None
                        else ([ground] if ground is not None else []))
    reasons: list[str] = []
    drive_nets = view.nets_of_pins(controller_ref,
                                   _drive_pin_names(view, controller_ref))
    if not drive_nets:
        return None, "no gate-drive pin (integrated-switch topologies are not classified in Phase 1)"

    switches = _switches_on(view, kinds, drive_nets)
    gate_series_ref = None
    if switches:
        reasons.append(
            "controller drive pin shares a net with the gate/base of "
            + ", ".join(view.sorted_refs(switches))
        )
    elif DRIVE_SERIES_HOP_KINDS:
        # ⚠ Only after the direct test has failed. A board that anchors directly
        # must never change its answer because a resistor happens to hang off the
        # same drive net.
        for net, through in _gate_series_hops(view, kinds, drive_nets):
            hop = _switches_on(view, kinds, [net])
            if hop:
                switches = hop
                gate_series_ref = through
                reasons.append(
                    f"controller drive pin reaches the gate/base of "
                    f"{', '.join(view.sorted_refs(hop))} through the series "
                    f"{through}"
                )
                break
    if not switches:
        return None, "gate-drive pin reaches no external switch device"
    switches = view.sorted_refs(switches)

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

    # -- step-down or not: the catch rectifier decides -----------------------
    # A boost or flyback puts the rectifier's ANODE on the switch node and takes
    # the output off its cathode. A step-down inverts that -- the freewheel diode
    # conducts *from ground into the switch node*, so its CATHODE is the terminal
    # that sits there -- and the inductor's far terminal, not the diode, is the
    # output rail.
    #
    # Everything below hangs off this one test. Before Phase 7 the classifier had
    # only the boost shape, so on an ``LTC1624`` buck it reported ``input_rail =
    # VOUT``, ``output_rail = None``, no sense resistor, no divider, the output
    # cap typed ``input_cap`` and the diode not found -- confidently, with no
    # warning, and every downstream phase consumed it.
    catch_rectifier = None
    if grounds:
        for ref in view.refs_on(switch_node):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "rectifier":
                continue
            # ⚠ ANY ground: on a split-ground buck the catch diode's anode is on
            # the POWER ground by the datasheet's own instruction, while the
            # classifier's primary ground is the signal one.
            if switch_node in view.nets_of_pins(ref, frozenset({"K"})) and \
                    grounds.intersection(view.nets_of_pins(ref, frozenset({"A"}))):
                catch_rectifier = ref
                break
    step_down = catch_rectifier is not None
    if step_down:
        reasons.append(
            "a rectifier freewheels from ground into the switch node -- step-down"
        )

    # -- the sense node ------------------------------------------------------
    sense_nets = view.nets_of_pins(controller_ref, SENSE_PIN_NAMES)
    sense_node = next((n for n in return_nets if n in sense_nets), None)
    if sense_node is None and SENSE_SERIES_HOP_KINDS:
        # The CS-filter hop (Phase 10). The direct test above runs FIRST, so a
        # board whose sense pin already sits on the switch return can never
        # change its answer -- the same ordering guard the gate-resistor hop
        # uses. One hop, resistors only, and the far end must be a return net
        # the switch actually drives, so an arbitrary resistor to the sense pin
        # cannot pull the sense node somewhere the switch does not reach.
        for net in sense_nets:
            for ref in view.sorted_refs(view.refs_on(net)):
                dev = kinds.get(ref)
                if dev is None or dev.kind not in SENSE_SERIES_HOP_KINDS:
                    continue
                bridged = [n for n in view.nets_of(ref) if n != net]
                far = next((n for n in bridged if n in return_nets), None)
                if far is not None:
                    sense_node = far
                    reasons.append(
                        f"the sense pin reaches the switch return through the "
                        f"series {ref} -- leading-edge CS filter"
                    )
                    break
            if sense_node is not None:
                break

    # -- rails ---------------------------------------------------------------
    magnetics_part = view.part_by_ref.get(magnetics_ref)
    winding_partner = _winding_partner_net(view, magnetics_part, magnetics_ref, switch_node)
    supply_nets = [n for n in view.nets_of_pins(controller_ref, SUPPLY_PIN_NAMES)
                   if n not in grounds]

    input_rail = None
    output_rail = None
    output_rectifier = None
    output_through = None          # the series element between L and the rail

    if step_down:
        # The inductor is on the output side, so it cannot point at the input the
        # way a boost's does. Walk out of the switch's other power terminal
        # instead, allowing one high-side sense resistor in the way. A net the
        # controller calls a supply wins outright; otherwise a net with a
        # capacitor to ground will do.
        reachable = _series_reachable(view, kinds, return_nets, grounds)
        for net, _through in reachable:
            if net in supply_nets:
                input_rail = net
                break
        if input_rail is None:
            for net, _through in reachable:
                if _caps_to(view, kinds, net, grounds):
                    input_rail = net
                    break
        # The output rail is the inductor's far terminal -- **or one series
        # resistor beyond it**. That extra hop is not a convenience: an
        # output-leg sense resistor is a standard step-down build (the LT3724
        # puts ``SENSE+`` on the inductor side of ``R_SENSE`` and ``SENSE-`` on
        # the ``V_OUT`` side, p.6), and it is the exact mirror of the hop the
        # input side already allows for a high-side sense resistor. Without it
        # the classifier reported no output rail, no divider and no topology on
        # a textbook buck.
        if winding_partner is not None and winding_partner not in grounds:
            for net, through in _series_reachable(view, kinds, [winding_partner],
                                                  grounds):
                if net == input_rail:
                    continue
                if _caps_to(view, kinds, net, grounds):
                    output_rail = net
                    output_through = through
                    if through is not None:
                        reasons.append(
                            f"output rail {net} sits one series {through} beyond "
                            f"the inductor -- output-leg sense"
                        )
                    break
    else:
        for net in supply_nets:
            if magnetics_ref in view.refs_on(net) and _caps_to(view, kinds, net,
                                                               grounds):
                input_rail = net
                break
        if input_rail is None and winding_partner is not None \
                and winding_partner not in grounds:
            if _caps_to(view, kinds, winding_partner, grounds):
                input_rail = winding_partner

        # A rectifier fed from the switch node (non-isolated) or from a winding
        # terminal (isolated secondary) delivers the output rail on its cathode.
        magnet_nets = [n for n in view.nets_of(magnetics_ref) if n not in grounds]
        for net in [switch_node] + [n for n in magnet_nets if n != switch_node]:
            for ref in view.refs_on(net):
                dev = kinds.get(ref)
                if dev is None or dev.kind != "rectifier":
                    continue
                if net not in view.nets_of_pins(ref, frozenset({"A"})):
                    continue      # the anode must face the stage, not the load
                cathode = next(iter(view.nets_of_pins(ref, frozenset({"K"}))), None)
                if cathode is None or cathode in grounds or cathode == input_rail:
                    continue
                if not _caps_to(view, kinds, cathode, grounds):
                    continue
                output_rail, output_rectifier = cathode, ref
                break
            if output_rail:
                break

    # -- the SEPIC / Cuk family (Phase 9, WS-5) ------------------------------
    # Run before the sense resistor, the divider and the loop, because on a Cuk
    # it is what finds the output rail at all -- Phase-1 limitation L-3, whose
    # whole content is that the output sits beyond a SECOND magnetic the rail
    # walk never reaches. A step-down is excluded outright: its bootstrap
    # capacitor bridges the switch node too, and a buck is already decidable.
    sepic_family = coupling_cap_ref = second_magnetics_ref = coupling_node = None
    if not step_down:
        (sepic_family, coupling_cap_ref, second_magnetics_ref, coupling_node,
         family_rail, family_rectifier) = _sepic_family(
            view, kinds, view.part_by_ref, switch_node, grounds, magnetics_ref)
        if sepic_family:
            reasons.append(
                f"{coupling_cap_ref} couples the switch node to "
                f"{coupling_node}, which carries the second winding of "
                f"{second_magnetics_ref} -- {sepic_family}"
            )
            # Never overwrite a rail the rail walk already resolved: on the
            # SEPIC it gets the answer right on its own, and this branch exists
            # for the Cuk, where it gets nothing.
            if output_rail is None and family_rail is not None:
                output_rail = family_rail
                reasons.append(
                    f"output rail {family_rail} found beyond the second "
                    f"magnetic (Phase-1 limitation L-3)"
                )
            if output_rectifier is None and family_rectifier is not None:
                output_rectifier = family_rectifier

    # -- the sense resistor --------------------------------------------------
    # Low-side sense returns the switch current to ground; high-side sense sits
    # in the input leg instead (``LTC1624``: ``VIN -> RS -> the FET drain``, with
    # the controller's own ``VIN`` pin as the ``SENSE+`` side). Both are a
    # resistor bridging the sense node and a rail -- which rail is the topology's
    # business, so the input-leg case can only be tested once the rails are known.
    sense_resistor = None
    if sense_node is not None:
        for ref in view.refs_on(sense_node):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "resistor":
                continue
            bridged = view.nets_of(ref)
            if grounds.intersection(bridged):
                sense_resistor = ref
                reasons.append("sense resistor bridges the switch return and ground")
                break
            if input_rail is not None and input_rail in bridged:
                sense_resistor = ref
                reasons.append(
                    "sense resistor bridges the switch return and the input rail"
                )
                break

    if sense_resistor is None and len(sense_nets) >= 2:
        # ⚠ Output-leg sense (Phase 9). The two cases above both assume the sense
        # resistor sits in the *switch's* return, which is where a boost and a
        # high-side-sense buck put it. The LT3724 does not: ``R_SENSE`` is in the
        # output leg, ``SENSE+`` on the inductor side and ``SENSE-`` on the
        # ``V_OUT`` side (p.6), so no net the switch touches is a sense net at
        # all and the search above finds nothing.
        #
        # The rule is structural rather than positional: a resistor **straddled
        # by two of the controller's own sense pins** is a Kelvin-sensed shunt,
        # wherever in the circuit it sits. It needs a differential pair to fire,
        # so a part with one SENSE pin can never reach it -- which is why it runs
        # only after the two positional tests have failed, and cannot move a
        # board that already had an answer.
        wanted = set(sense_nets)
        for ref in view.sorted_refs(r for n in sense_nets for r in view.refs_on(n)):
            dev = kinds.get(ref)
            if dev is None or dev.kind != "resistor":
                continue
            bridged = set(view.nets_of(ref))
            if len(bridged) == 2 and bridged <= wanted:
                sense_resistor = ref
                sense_node = next((n for n in sense_nets
                                   if n in bridged and n != output_rail),
                                  sorted(bridged)[0])
                reasons.append(
                    "sense resistor is straddled by the controller's own "
                    "differential SENSE pair -- Kelvin-sensed shunt"
                )
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
            if other in grounds and bottom is None:
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
    if step_down:
        # ⚠ A step-down's high-di/dt loop is on the INPUT side, not the output.
        # The input capacitor sources the pulse, the switch chops it and the
        # catch diode carries it when the switch opens; the inductor conducts
        # continuously and is deliberately NOT a member. That is the mirror image
        # of the boost, whose loop is ``COUT -> D -> M`` on the output side, and
        # getting it backwards is what made the shipped classifier report
        # ``COUT -> L1 -> M1`` here -- a loop containing no switching edge at all.
        far_net = input_rail
        partner_ref = catch_rectifier
        loop_reasons.append(
            "catch diode freewheels into the switch node -- input-side loop"
        )
    elif output_rectifier is not None and switch_node in view.nets_of(output_rectifier):
        # Non-isolated: current alternates between the switch and the rectifier
        # that shares its node, and the loop closes on the output capacitor.
        partner_ref, far_net = output_rectifier, output_rail
        loop_reasons.append("rectifier shares the switch node -- output-side loop")
    elif winding_partner is not None and winding_partner not in grounds:
        # Isolated / transformer primary: nothing else sits on the switch node,
        # so the loop closes on the capacitor at the other end of the winding.
        partner_ref, far_net = magnetics_ref, winding_partner
        loop_reasons.append(
            "no rectifier on the switch node -- loop closes through the winding"
        )

    if partner_ref is not None and far_net is not None and grounds:
        cap_refs = _caps_to(view, kinds, far_net, grounds)
        cap_ref, bulk, by_value = _loop_capacitor(view, view.part_by_ref, cap_refs)
        if by_value:
            loop_reasons.append(
                "several candidate capacitors were separable only by value -- "
                "the rail may be shared with another consumer"
            )
        if cap_ref is not None:
            if step_down:
                # Walk the loop in conduction order from the capacitor:
                # ``CIN -> (RS) -> M1 -> D1`` and back through ground. The sense
                # resistor sits between the cap and the switch here, not after
                # the switch as it does on a low-side-sense boost, so it cannot
                # simply be appended.
                members = [cap_ref]
                nets = [far_net]
                # ⚠ Only when the sense resistor is genuinely IN this loop --
                # i.e. it bridges the input rail the capacitor sits on. An
                # output-leg sense resistor (the LT3724's) is downstream of the
                # inductor and carries no switching edge at all, so appending it
                # would put a part in the hot loop that is not in it, which is
                # exactly the error Phase 7 fixed on the other side.
                if sense_resistor is not None and far_net in view.nets_of(
                        sense_resistor):
                    members.append(sense_resistor)
                    nets.append(sense_node)
                members.extend([switch_ref, partner_ref])
                nets.append(switch_node)
            else:
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
    #
    # ⚠⚠ The connectivity test comes FIRST, and that ordering is the Phase-9 fix.
    # Winding count alone cannot tell a coupled-inductor SEPIC from a flyback --
    # both are "one magnetic, two windings" -- so keying on it named every
    # coupled SEPIC / Cuk / inverting converter ``flyback``. What separates them
    # is the coupling capacitor, which is connectivity, not a count. A flyback
    # has no capacitor bridging its switch node to its second winding; that is
    # the whole difference, and it is why the count survives as the fallback
    # rather than being deleted.
    topology = "unknown"
    if sepic_family:
        topology = sepic_family
    elif len(_winding_groups(magnetics_part)) >= 2:
        topology = "flyback"
    elif step_down:
        # Phase-1 limitation L-3 said the name resolves "when the inductor's far
        # terminal is the output rail" -- on a step-down that is exactly the
        # test, and the catch rectifier is what makes it decidable.
        # ⚠ ``output_through`` widens "is" to "is, or is one series resistor
        # away from" -- an output-leg sense resistor, not a different topology.
        if winding_partner is not None and output_rail is not None and (
                winding_partner == output_rail or output_through is not None):
            topology = "buck"
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
    if gate_series_ref:
        add(gate_series_ref, "gate_resistor",
            "sits in series between the controller's drive pin and the switch "
            "gate -- the stage is anchored THROUGH it")
    add(magnetics_ref)
    if second_magnetics_ref and second_magnetics_ref != magnetics_ref:
        add(second_magnetics_ref)
    if coupling_cap_ref:
        add(coupling_cap_ref, "coupling_cap",
            f"bridges the switch node and {coupling_node} -- the element that "
            f"makes this a {sepic_family} rather than a flyback")
    if output_rectifier:
        add(output_rectifier)
    if catch_rectifier:
        add(catch_rectifier)
    if sense_resistor:
        add(sense_resistor, "sense_resistor",
            "one terminal on the switch return / controller SENSE pin, the other on "
            + ("the input rail" if step_down and not grounds.intersection(
                view.nets_of(sense_resistor)) else "ground"))
    if divider:
        add(divider[0], "fb_divider_top", "feedback node to the output rail")
        add(divider[1], "fb_divider_bottom", "feedback node to ground")
    if input_rail:
        for ref in _caps_to(view, kinds, input_rail, grounds):
            add(ref, "input_cap", "bridges the input rail and ground")
    if output_rail:
        for ref in _caps_to(view, kinds, output_rail, grounds):
            add(ref, "output_cap", "bridges the output rail and ground")

    stage = PowerStage(
        controller_ref=controller_ref,
        topology=topology,
        switch_node_nets=[switch_node],
        input_rail=input_rail,
        output_rail=output_rail,
        ground_net=ground,
        # ⛔ Sorted so the field is deterministic, and it always CONTAINS
        # ``ground`` -- on a single-ground board it is exactly ``[ground]``.
        ground_nets=sorted(grounds),
        devices=devices,
        loops=loops,
        feedback_divider=divider,
        sense_resistor_ref=sense_resistor,
        feedback_net=feedback_node,
        sense_net=sense_node,
        small_signal_refs=_small_signal_refs(view, controller_ref, grounds),
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
    grounds = _find_grounds(view, kinds, ground)

    warnings: list[str] = []
    if len(grounds) > 1:
        # Not a defect -- a datasheet-mandated split. Said out loud because a
        # stage reports ONE ``ground_net`` and a reader deserves to know the
        # board has more than one, and which one the loop is quoted against.
        warnings.append(
            f"split ground: the controller(s) name "
            f"{', '.join(sorted(grounds))} as ground; the reported ground_net is "
            f"{ground} and every return test accepts any of them"
        )
    has_magnetics = any(dev.kind == "magnetics" for dev in kinds.values())
    stages: list[PowerStage] = []
    for ref in view.sorted_refs(
        r for r, dev in kinds.items() if dev.kind == "controller"
    ):
        stage, why_not = _build_stage(view, kinds, ground, ref, warnings,
                                      grounds=grounds)
        if stage is not None:
            stages.append(stage)
        elif has_magnetics:
            # Only worth saying on a board that has magnetics at all; otherwise
            # every MCU whose IC happens to match the controller pin set would
            # generate noise.
            warnings.append(f"{ref}: {why_not}")

    return PowerStagePlan(stages=stages, warnings=warnings)
