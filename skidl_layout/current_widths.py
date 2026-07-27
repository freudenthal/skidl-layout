"""IPC-2221 track sizing: a measured current -> the copper width it needs.

``power.py::_suggest_width`` picks a power-track width from three magic numbers
(0.8 mm for a name-matched high-current rail, 0.3 mm for ground or a >= 6-ref
net, 0.25 mm otherwise). They encode no current at all. This module is the
physics that does: give it amps and the fab's own copper weight, and it returns
the width IPC-2221 asks for.

Deliberately **pure** -- stdlib only, no simulation, no board, no I/O. Currents
are *data* (Phase-6 plan section 1.1): ``skidl_layout`` never imports
``skidl.sim`` or shells out to ngspice, so a hand-written ``{net: amps}`` dict
is exactly as valid a producer as a live transient. :mod:`skidl_eda.net_currents`
is the simulation-backed producer.

The rule (IPC-2221 Fig. 6-4, the standard curve-fit form)::

    A[mil^2] = (I / (k * dT^0.44)) ** (1 / 0.725)
    width[mil] = A / thickness[mil]

with ``k = 0.048`` for an **external** layer and ``0.024`` internal. Copper
thickness comes from the FabSpec's weight (1 oz -> 0.035 mm -> 1.378 mil).

A pleasing calibration fact worth knowing before you trust any of this: at
1 oz / dT 10 C / external, **1 A sizes to 0.300 mm** -- exactly the middle magic
number the ladder has used all along. The ladder was an unstated "<= 1 A, or
~2 A if the name looks big" assumption; this module makes the assumption an
input.
"""

from __future__ import annotations

#: IPC-2221 constant for an external (surface) layer.
IPC_K_EXTERNAL = 0.048
#: IPC-2221 constant for an internal layer -- half the external one, because
#: buried copper has no air to convect into.
IPC_K_INTERNAL = 0.024
#: The curve's exponents.
IPC_DELTA_T_EXPONENT = 0.44
IPC_AREA_EXPONENT = 0.725

MM_PER_MIL = 0.0254
#: 1 oz copper, the weight every shipped preset uses.
DEFAULT_COPPER_THICKNESS_MM = 0.035
#: Temperature rise the sizing assumes when the caller names none. 10 C is the
#: conservative end of the usual 10-30 C design range.
DEFAULT_DELTA_T_C = 10.0


def ipc2221_width_mm(
    i_rms_a: float,
    copper_thickness_mm: float = DEFAULT_COPPER_THICKNESS_MM,
    delta_t_c: float = DEFAULT_DELTA_T_C,
    internal: bool = False,
) -> float:
    """Width (mm) IPC-2221 asks for to carry ``i_rms_a`` at ``delta_t_c`` rise.

    ``0.0`` for a zero or negative current -- a net carrying nothing needs no
    copper, and returning a floor here would quietly invent one. Monotone
    increasing in current; monotone *decreasing* in thickness and in the
    allowed temperature rise.

    Calibration anchors (1 oz / dT 10 C / external), formula-exact::

        1.00 A -> 0.300 mm
        2.00 A -> 0.781 mm
        4.44 A -> 2.349 mm

    An internal layer is ~2.60x wider for the same current
    (``2 ** (1 / 0.725)``), because k halves.
    """
    current = float(i_rms_a or 0.0)
    if current <= 0.0:
        return 0.0
    if copper_thickness_mm <= 0.0:
        raise ValueError(f"copper_thickness_mm must be > 0 (got {copper_thickness_mm})")
    if delta_t_c <= 0.0:
        raise ValueError(f"delta_t_c must be > 0 (got {delta_t_c})")

    k = IPC_K_INTERNAL if internal else IPC_K_EXTERNAL
    area_mil2 = (current / (k * delta_t_c**IPC_DELTA_T_EXPONENT)) ** (
        1.0 / IPC_AREA_EXPONENT
    )
    thickness_mil = copper_thickness_mm / MM_PER_MIL
    return (area_mil2 / thickness_mil) * MM_PER_MIL


def widths_from_currents(
    net_currents: dict,
    spec=None,
    delta_t_c: float = DEFAULT_DELTA_T_C,
    max_width_mm: float | None = None,
    internal: bool = False,
) -> dict:
    """``{net: width_mm}`` sized from ``{net: amps_rms}``.

    - Copper thickness comes from ``spec.copper_thickness_mm`` when a
      :class:`~skidl_layout.FabSpec` is given, else 1 oz is assumed.
    - Each width is floored at ``spec.min_track_mm`` (a fab never draws below
      its own minimum) and capped at ``max_width_mm`` when one is given.
      **The cap is silent here** -- it is the caller's job to say that it bit,
      because only the caller knows whether a clamp is a policy or a surprise
      (:func:`skidl_layout.emit_power_copper` warns with both numbers).
    - A net whose current is ``None``, zero or negative is **absent** from the
      result rather than present with a zero. Absent != zero: Phase 5's rule,
      because "no copper needed" and "no measurement" are different claims and
      only one of them should be able to widen a track.
    """
    thickness = DEFAULT_COPPER_THICKNESS_MM
    floor = 0.0
    if spec is not None:
        thickness = float(getattr(spec, "copper_thickness_mm", thickness) or thickness)
        floor = float(getattr(spec, "min_track_mm", 0.0) or 0.0)

    widths: dict[str, float] = {}
    for net, current in (net_currents or {}).items():
        if current is None:
            continue
        try:
            amps = float(current)
        except (TypeError, ValueError):
            continue
        if amps <= 0.0:
            continue
        width = ipc2221_width_mm(
            amps,
            copper_thickness_mm=thickness,
            delta_t_c=delta_t_c,
            internal=internal,
        )
        if floor:
            width = max(width, floor)
        if max_width_mm is not None:
            width = min(width, float(max_width_mm))
        widths[str(net)] = width
    return widths
