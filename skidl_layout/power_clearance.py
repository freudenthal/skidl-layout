"""IPC-2221B Table 6-1 spacing: a net's voltage -> the clearance it must route at.

Phase 8 shipped the **judge** for conductor spacing (:data:`fabspec.IPC2221B_TABLE_6_1_MM`,
:func:`fabspec.measure_voltage_spacing`) and used it to find that every HV board
this stack lays out is routed at *signal* spacing -- ``lt3758_flyback``'s 72 V
``VIN`` measured 0.200 mm against a 0.6 mm requirement. Nothing in the placer or
the router knew a net's voltage. This module is the **lever**: it turns the
``{net: peak_volts}`` dictionaries the boards already carry into the per-net
clearance map KRT's ``--net-clearances`` accepts.

Deliberately **pure** -- stdlib only, no board, no I/O, no subprocess. Voltages
are *data*, exactly as Phase 6 made currents data: a hand-written dict is as
valid a producer as anything else, and :mod:`skidl_layout` never learns where the
number came from. Every board's ``NET_PEAK_VOLTS`` today is a **datasheet
rating**, which is the right input for a clearance (a rating is what the
conductor must survive) but is *not* a measurement -- see
:func:`plan_net_clearances`'s ``reason`` strings, which say so per net.

**What the measurement in Phase 10's WS-2 established about the mechanism**, so
no caller re-derives it:

- ``route.py`` uses an explicit ``--net-clearances`` JSON **as-is**. The
  ``min(clearance, ceiling)`` clamp applies only to the net-class map KRT
  auto-reads from a sibling ``.kicad_pro``; the explicit map bypasses it
  (``route.py`` ~2732-2748). So ``--clearance`` being a "pure CEILING" does
  **not** cap a per-net entry, and no KRT change is needed to widen a net.
  Measured on ``lt3758_flyback``: ``SW``<->``VIN`` went **0.1984 mm -> 0.552 mm**
  with the ceiling still at 0.25.
- ⛔⛔ **RETRACTED by Phase 11 -- the widening does NOT bind pairwise.** Phase 10
  concluded from one board that "a widened net still meets an *unlisted* net at
  the board floor", because ``lt3758_flyback``'s ``VIN`` measured 0.200 mm to an
  unrated ``UVLO``. **The map binds board-WIDE.** KRT's
  ``RoutingConfig.set_net_clearances`` computes
  ``net_clearance_floor = max(config.clearance, max over the routed nets in the
  map)`` and ``obstacle_clearance()`` returns ``max(floor, ...)`` for **every**
  obstacle, named or not. Measured on ``uc3844_flyback``: the map named **5**
  nets and **all 17 nets on the board widened** (~0.20-0.25 -> ~0.55-0.61 mm),
  including ``AUX``, ``FB``, ``GATE``, ``RTCT`` and ``VREF``, which carry no
  entry at all. So there is nothing to feed the map that it does not already do.
  ⚠ The corollary is the real cost: the floor lifts **every** pair, so on a
  routing-limited board the lever does not add clearance, it **redistributes**
  it -- KRT rescues elsewhere to keep the board closing.
- ⚠ **What actually held those nets at 0.200 mm was the PACKAGE.**
  ``lt3758_flyback``'s ``VIN``/``UVLO`` limiting pair is ``U1.10``/``U1.9`` --
  adjacent pins of an **MSOP-10 at 0.5 mm pitch**. Column B2 asks 0.6 mm at
  72 V, which is wider than the pitch, so no layout of that package can comply
  and no lever here can help. :func:`fabspec.measure_voltage_spacing` now
  reports ``same_footprint`` so the distinction is visible in the number.
- ⚠ KRT's fine-pitch **rescue ladder necks down below the per-net value** when a
  net would otherwise fail to route (observed rescues at 0.1768 / 0.2256 mm).
  The lever raises the clearance the router *aims* for; it is not a hard floor,
  which is why :func:`fabspec.measure_voltage_spacing` stays the judge and this
  module never claims a board met the standard.
"""

from __future__ import annotations

from .fabspec import DEFAULT_SPACING_COLUMN, ipc2221_spacing_mm

__all__ = ["plan_net_clearances", "net_clearance_map"]


def plan_net_clearances(
    net_voltages: dict | None,
    base_clearance_mm: float | None = None,
    column: str = DEFAULT_SPACING_COLUMN,
) -> dict:
    """One record per net in ``net_voltages`` saying what Table 6-1 asks and why.

    ``{net: {volts, column, required_mm, applied_mm, applied, reason}}``.

    ``base_clearance_mm`` is the clearance the board would otherwise route at
    (the FabSpec's design clearance). A net whose requirement that already meets
    gets ``applied=False`` -- **the map must not narrow anything**, because
    handing KRT a per-net value *below* the board clearance would quietly relax a
    net that was fine. Widening is the only direction this module moves.

    ``required_mm is None`` (Table 6-1 states nothing for that voltage in that
    column -- above 500 V with no recorded slope) yields ``applied=False`` with a
    reason. "Not stated" is a different claim from "no spacing required" and the
    two are kept apart, exactly as :func:`fabspec.ipc2221_spacing_mm` keeps them.
    """
    records: dict = {}
    base = float(base_clearance_mm) if base_clearance_mm is not None else None
    for net, volts in sorted((net_voltages or {}).items()):
        try:
            v = float(volts)
        except (TypeError, ValueError):
            records[str(net)] = {
                "volts": None, "column": column, "required_mm": None,
                "applied_mm": None, "applied": False,
                "reason": f"voltage {volts!r} is not a number",
            }
            continue
        required = ipc2221_spacing_mm(v, column=column)
        row = {
            "volts": v, "column": column, "required_mm": required,
            "applied_mm": None, "applied": False, "reason": "",
        }
        if required is None:
            row["reason"] = (
                f"IPC-2221B Table 6-1 column {column} states no spacing for "
                f"{v:g}V (above 500V with no recorded slope for this column)"
            )
        elif base is not None and required <= base + 1e-9:
            row["reason"] = (
                f"the board already routes at {base:g}mm, which meets the "
                f"{required:g}mm column {column} asks for at {v:g}V"
            )
        else:
            row["applied_mm"] = required
            row["applied"] = True
            row["reason"] = (
                f"column {column} asks {required:g}mm at {v:g}V"
                + (f", above the board's {base:g}mm" if base is not None else "")
                + " -- widened (the voltage is a rating, not a measurement)"
            )
        records[str(net)] = row
    return records


def net_clearance_map(records: dict) -> dict:
    """The ``{net: mm}`` KRT's ``--net-clearances`` wants, from the records.

    Only the nets :func:`plan_net_clearances` actually widened. Empty when
    nothing needs widening -- and an empty map must mean "pass no flag at all",
    because an explicit ``--net-clearances`` **replaces** KRT's auto-read
    net-class map rather than adding to it.
    """
    return {net: row["applied_mm"] for net, row in (records or {}).items()
            if row.get("applied") and row.get("applied_mm") is not None}
