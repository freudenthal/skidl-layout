# -*- coding: utf-8 -*-
"""Per-pad clearance on a classified controller (spacing arc, plan B).

⭐ **Why this module exists, and why it is the only mechanism of its kind.**

The reviewer's ask has two halves. *"Give the IC more room"* is a **placement**
problem -- spacing plans A, D and E. *"Then keep the routed wires away from the
pins"* is a **routing** problem, and the two-codebase survey found exactly one
lever for it:

* ⛔ **Track-to-pad clearance is not separately configurable from
  track-to-track.** Both come from the same ``clearance`` scalar
  (``obstacle_map.py``); there is no ``--pad-clearance``.
* ⛔ **A user-layer track keep-out is refuted three times** (Phase 13: −32 nets;
  Phase 14: −22, and ±0-with-8-DRC). ``add_user_keepout_obstacles`` blocks *all*
  copper layers for *every* routed net, so an annulus that protects a via site
  also forbids the controller's own escape from using it.
* ⛔ **No router reads courtyards.** Enlarging a courtyard buys nothing at route
  time.
* ⭐ **A per-pad ``(clearance …)`` override IS honoured, as a hard floor**, in
  KRT's track obstacle map *and* its via keep-out map (``obstacle_map.py``
  ``_add_pad_obstacle`` / ``_pad_via_keepout_cells``, both
  ``max(effective, pad.local_clearance)``), in ``check_drc``, and -- because it
  is a KiCad-native pad property -- by KiCad's own DRC.

⭐⭐ **The property that distinguishes this from the thrice-refuted keep-out:**
a clearance override is enforced **between two items of different nets**. KiCad
exempts a pad's own net, and KRT prices obstacles per foreign net id, so the
declaration holds *foreign* copper off the pins **without blocking the
controller's own escape**. That is precisely what the user-layer keep-out cannot
do, and no report should conflate the two.

**What this module does not do.** It does not move a part. If a neighbour is
already inside the escape lane -- and every controller in this corpus is, at
0.000-0.540 mm against a 0.9048 mm lane -- a declared clearance cannot create
room; it can only forbid copper from the room that exists. That asymmetry is why
plan A (placement clearance) is a separate experiment.

The lever is **escalation rung 3**: the declaration is written into the board
this stack already emits, after the writer has run and before any router sees
it, so ``writer.py`` is untouched and the two-layer byte-identity every
placement digest rests on cannot move.
"""

from __future__ import annotations

import math
import re

__all__ = [
    "PAD_CLEARANCE_FIELD",
    "PAD_CLEARANCE_FORMS",
    "apply_pad_clearance",
    "declared_pad_clearance_refs",
    "mark_pad_clearance",
    "pad_clearance_value",
    "resolve_pad_clearance_targets",
    "segment_rect_distance",
    "segment_segment_distance",
]

#: The ``Part.fields`` key a declaration is stored under -- the same channel
#: :func:`~skidl_layout.power_escape.mark_escape_room` uses, for the same
#: reason: ``fields`` is skidl's own per-part dict and it survives
#: ``Part.copy()``, so a declared part stays declared through a ``5 * U1``-style
#: replication.
PAD_CLEARANCE_FIELD = "pad_clearance"

#: The two s-expression forms KiCad accepts, both of which KRT's parser resolves
#: into ``pad.local_clearance`` (its issue #326):
#:
#: * ``"pad"`` -- ``(clearance <mm>)`` inside each ``(pad …)`` block;
#: * ``"footprint"`` -- one ``(clearance <mm>)`` in the footprint header, which
#:   every pad that does not override it inherits.
#:
#: ⭐ ``"pad"`` is the default because plan E needs **per-pad** anisotropy and a
#: footprint-level token cannot express it. Both are measured by the spacing-02
#: driver's gate B1 rather than assumed.
PAD_CLEARANCE_FORMS = ("pad", "footprint")


# --------------------------------------------------------------------------- #
# The DECLARATION -- the producer says which pins to hold copper off
# --------------------------------------------------------------------------- #

def mark_pad_clearance(*parts, clearance_mm=None, clear: bool = False) -> list[str]:
    """Declare that foreign copper must stand off each part's pads.

    The producer-side half, called from the board source where the part is
    built::

        from skidl_eda import mark_pad_clearance

        U1 = Part("Regulator_Switching", "LT3757", footprint=...)
        mark_pad_clearance(U1)                       # at the layout-time value
        mark_pad_clearance(U1, clearance_mm=0.25)    # this part, this number

    Args:
        *parts: skidl ``Part`` objects (or anything with a ``fields`` dict).
        clearance_mm: the clearance to declare, in mm. ``None`` (the normal
            case) means **"declared, take the value from layout"** -- so a board
            does not hardcode a number that belongs to a sweep or a fab.
            ⭐ A ``{side: mm}`` dict is accepted and stored *now*, so plan E's
            anisotropic successor does not have to re-cut this seam; this plan
            honours only its **maximum** and says so in a warning
            (:func:`pad_clearance_value`).
        clear: remove the declaration instead of adding it.

    Returns:
        The refs marked, in the order given. A part with no usable field store
        is skipped rather than raising -- and is absent from the return, which
        is how a caller detects it.
    """
    if clearance_mm is None:
        value: object = True
    elif isinstance(clearance_mm, dict):
        value = {str(k): float(v) for k, v in clearance_mm.items()}
    else:
        value = float(clearance_mm)

    marked: list[str] = []
    for part in parts:
        store = getattr(part, "fields", None)
        if store is None:
            try:
                part.fields = store = {}
            except Exception:                      # noqa: BLE001
                continue
        try:
            if clear:
                store.pop(PAD_CLEARANCE_FIELD, None)
            else:
                store[PAD_CLEARANCE_FIELD] = value
        except Exception:                          # noqa: BLE001
            continue
        ref = getattr(part, "ref", None)
        marked.append(str(ref) if ref is not None else "")
    return marked


def _declared_on(part):
    """This part's declaration, or ``None``. ``(True | float | dict)``.

    Reads the same three holders skidl itself accepts for extra part data --
    ``fields``, ``_extra_fields`` and a plain attribute -- because a part loaded
    from a library, a part built in source and a snapshot part do not all carry
    the same one.
    """
    for holder in ("fields", "_extra_fields"):
        store = getattr(part, holder, None)
        if isinstance(store, dict) and PAD_CLEARANCE_FIELD in store:
            return store[PAD_CLEARANCE_FIELD]
    return getattr(part, PAD_CLEARANCE_FIELD, None)


def declared_pad_clearance_refs(circuit) -> dict:
    """``{ref: mm | dict | None}`` for every part the producer marked.

    ``None`` as a value means "declared, resolve the number at layout time" --
    distinct from the ref being absent, which means undeclared. Order follows
    the circuit's own part order, so the result is deterministic without
    comparing reference designators.
    """
    out: dict = {}
    for part in getattr(circuit, "parts", None) or []:
        ref = getattr(part, "ref", None)
        if ref is None:
            continue
        declared = _declared_on(part)
        if declared is None or declared is False:
            continue
        out[str(ref)] = None if declared is True else declared
    return out


def pad_clearance_value(declared, default_mm, *, ref="?",
                        warnings=None) -> float | None:
    """Resolve one part's declaration to a single mm value, or ``None``.

    Precedence, matching the ``power_escape_room`` pattern the arc already ships:
    **the part's own declared number > the layout-time default > nothing**.

    ⚠ A ``{side: mm}`` dict degrades to its **maximum** with a recorded warning.
    Isotropic-at-the-largest is the only reading that cannot *weaken* what the
    producer asked for, and saying so in ``warnings`` is what stops a later
    report claiming this plan measured anisotropy. Plan E is where the sides
    become real.
    """
    if isinstance(declared, dict):
        values = [float(v) for v in declared.values()]
        if not values:
            return None if default_mm is None else float(default_mm)
        widest = max(values)
        if warnings is not None:
            warnings.append(
                f"pad_clearance {ref}: per-side declaration "
                f"{{{', '.join(f'{k}: {float(v):g}' for k, v in declared.items())}}} "
                f"is NOT honoured per side in this plan; applied isotropically at "
                f"its maximum {widest:g}mm")
        return widest
    if declared is not None:
        return float(declared)
    if default_mm is None:
        return None
    return float(default_mm)


def resolve_pad_clearance_targets(*, placed_refs, circuit=None, clearance_mm=None,
                                  controller_ref=None, power_stage_plan=None,
                                  warnings=None) -> tuple[list, str]:
    """``([(ref, mm)], source)`` -- whose pads get a clearance, and who said so.

    ⛔ **No new identification path.** Who the controller *is* comes from
    :func:`~skidl_layout.power_escape.resolve_escape_targets` -- the classifier's
    own ``controller_ref`` per switching stage, or the producer's
    ``mark_escape_room`` declaration -- because the arc already resolved that
    question and getting a second opinion is how Phase 13's retraction R-1
    happened. This module only adds the *value*.

    The precedence:

    1. an explicit ``controller_ref`` (a str or an iterable) -- the caller wins;
    2. **this module's own declarations** (:func:`mark_pad_clearance`), which may
       carry per-part numbers;
    3. whatever ``resolve_escape_targets`` resolves, at ``clearance_mm``.

    A target with no resolvable number is dropped and recorded in ``warnings``:
    a declaration with no value and no layout-time default is a request nobody
    can satisfy, and silently applying a made-up number is worse.
    """
    from .power_escape import resolve_escape_targets

    placed = set(str(r) for r in (placed_refs or ()))

    def _finish(pairs, source):
        out = []
        for ref, declared in pairs:
            if ref not in placed:
                continue
            value = pad_clearance_value(declared, clearance_mm, ref=ref,
                                        warnings=warnings)
            if value is None or value <= 0.0:
                if warnings is not None:
                    warnings.append(
                        f"pad_clearance {ref}: declared but no clearance value "
                        "resolved (no per-part number and no layout default); "
                        "no override written")
                continue
            out.append((ref, float(value)))
        return out, source

    if controller_ref is not None:
        refs = ([str(controller_ref)] if isinstance(controller_ref, str)
                else [str(r) for r in controller_ref])
        kept, source = _finish([(ref, None) for ref in refs], "explicit")
        if kept:
            return kept, source

    if circuit is not None:
        declared = list(declared_pad_clearance_refs(circuit).items())
        if declared:
            kept, source = _finish(declared, "declared")
            if kept:
                return kept, source

    inherited, escape_source = resolve_escape_targets(
        placed_refs=placed, circuit=circuit, power_stage_plan=power_stage_plan)
    if inherited:
        kept, source = _finish([(ref, None) for ref, _lane in inherited],
                               f"escape:{escape_source}")
        if kept:
            return kept, source
    return [], "none"


# --------------------------------------------------------------------------- #
# The BOARD EDIT -- rung 3, on the board the router is about to read
# --------------------------------------------------------------------------- #

def _skip_string(text: str, index: int) -> int:
    """Index just past the quoted string starting at ``text[index] == '"'``."""
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        index += 1
    return index


def _block_end(text: str, start: int) -> int:
    """Index just past the s-expression opening at ``text[start] == '('``.

    ⚠ **String-aware on purpose.** A footprint's own ``(descr "… (https://…)")``
    carries parentheses inside a quoted string -- the corpus's MSOP-10
    controller does, in its datasheet URL -- so naive paren counting walks off
    the end of the block and the edit lands in the wrong footprint.
    """
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _skip_string(text, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _footprint_spans(text: str) -> list[tuple[int, int, str]]:
    """``[(start, end, ref)]`` for every footprint block, in file order."""
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\(footprint\s", text):
        start = match.start()
        end = _block_end(text, start)
        block = text[start:end]
        ref_match = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', block)
        spans.append((start, end, ref_match.group(1) if ref_match else ""))
    return spans


def _pad_spans(block: str) -> list[tuple[int, int]]:
    """``[(start, end)]`` for every ``(pad …)`` child of one footprint block."""
    spans: list[tuple[int, int]] = []
    index = 0
    while True:
        match = re.compile(r"\(pad\s").search(block, index)
        if match is None:
            return spans
        start = match.start()
        end = _block_end(block, start)
        spans.append((start, end))
        index = end


#: Where a footprint-level override may be written. ⛔ Load-bearing: KRT bounds
#: its footprint-level ``(clearance …)`` search at the first of these tokens (so
#: that a *pad's* own override cannot match), and KiCad itself writes the header
#: overrides here. A token placed after the first ``(fp_line`` is parsed by
#: nobody.
_FP_HEADER_TERMINATORS = ("(pad ", "(fp_", "(zone", "(model")

_CLEARANCE_RE = re.compile(r"\n?[ \t]*\(clearance\s+-?[\d.]+\)")


def apply_pad_clearance(pcb_path: str, targets, *, form: str = "pad",
                        out_path: str | None = None,
                        fab_spec=None) -> dict:
    """Write a per-pad (or per-footprint) clearance override into a board.

    ⚠ **In place by default.** Call it on the board the router is about to read,
    and remember that a ``.kicad_pcb`` copied without its sibling
    ``.kicad_pro`` loses the DRC floor the chain routed to.

    Args:
        pcb_path: the board to edit.
        targets: ``[(ref, mm)]`` as :func:`resolve_pad_clearance_targets`
            returns. A ref the board does not carry is recorded, not raised.
        form: ``"pad"`` (default) or ``"footprint"`` -- see
            :data:`PAD_CLEARANCE_FORMS`.
        out_path: write here instead of over ``pcb_path``.
        fab_spec: when given, a requested clearance **below**
            ``spec.min_clearance_mm`` raises ``ValueError``. ⛔ A declared
            override under the fab's published spacing is not a routing hint, it
            is a fab violation -- and Phase 16 set the precedent that a
            spec contradiction raises rather than silently narrowing.

    Returns:
        A record: ``{"form", "written", "rows": {ref: {...}}, "missing": [...]}``
        where ``written`` counts the ``(clearance …)`` tokens actually emitted.
    """
    if form not in PAD_CLEARANCE_FORMS:
        raise ValueError(
            f"pad clearance form {form!r} not one of {PAD_CLEARANCE_FORMS}")

    floor = getattr(fab_spec, "min_clearance_mm", None) if fab_spec is not None else None
    wanted = {}
    for ref, value in (targets or ()):
        value = float(value)
        if floor is not None and value < float(floor) - 1e-9:
            raise ValueError(
                f"pad clearance {value:g}mm on {ref} is below the fab's "
                f"min_clearance_mm ({float(floor):g}mm) -- a declared override "
                "under the published spacing is a fab violation, not a routing "
                "hint")
        wanted[str(ref)] = value
    if not wanted:
        return {"form": form, "written": 0, "rows": {}, "missing": []}

    with open(pcb_path, "r", encoding="utf-8") as handle:
        text = handle.read()

    rows: dict[str, dict] = {}
    written = 0
    # Rebuild the file from the tail backwards so every span index stays valid.
    for start, end, ref in reversed(_footprint_spans(text)):
        if ref not in wanted:
            continue
        value = wanted[ref]
        block = text[start:end]
        if form == "pad":
            new_block, count = _inject_into_pads(block, value)
        else:
            new_block, count = _inject_into_footprint_header(block, value)
        rows[ref] = {"clearance_mm": value, "form": form, "tokens": count}
        written += count
        text = text[:start] + new_block + text[end:]

    with open(out_path or pcb_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    return {
        "form": form,
        "written": written,
        "rows": {ref: rows[ref] for ref in sorted(rows)},
        "missing": sorted(set(wanted) - set(rows)),
    }


def _inject_into_pads(block: str, value: float) -> tuple[str, int]:
    """One ``(clearance …)`` per pad of ``block``. Returns ``(block, count)``.

    The token is written as the pad's **last child before its ``(uuid …)``**,
    which is where KiCad itself puts it. Both parsers are token-driven, so the
    position is a readability choice rather than a correctness one -- except
    that it must be *inside* the pad block, which is why the spans are computed
    rather than pattern-matched.
    """
    count = 0
    for start, end in reversed(_pad_spans(block)):
        pad = block[start:end]
        stripped = _CLEARANCE_RE.sub("", pad)      # idempotent: replace, never stack
        indent = _child_indent(stripped)
        uuid_match = re.search(r"\n[ \t]*\(uuid\s", stripped)
        token = f"\n{indent}(clearance {value:g})"
        if uuid_match:
            new_pad = (stripped[:uuid_match.start()] + token
                       + stripped[uuid_match.start():])
        else:
            # No uuid child (a hand-written or minimal pad): append as the last
            # child, immediately before the pad's own closing paren.
            close = stripped.rstrip()
            trailing = stripped[len(close):]
            new_pad = close[:-1].rstrip() + token + ")" + trailing
        count += 1
        block = block[:start] + new_pad + block[end:]
    return block, count


def _child_indent(block: str) -> str:
    """The indentation this block's children are written at."""
    match = re.search(r"\n([ \t]+)\S", block)
    return match.group(1) if match else "      "


def _inject_into_footprint_header(block: str, value: float) -> tuple[str, int]:
    """One footprint-level ``(clearance …)``. Returns ``(block, count)``.

    ⛔ Inserted **before the first graphic/pad/zone/model child**, because that
    is the window KRT's footprint-level search is bounded to and where KiCad
    writes it. A token after the first ``(fp_line`` parses as nothing.
    """
    header_end = len(block)
    for token in _FP_HEADER_TERMINATORS:
        index = block.find(token)
        if index != -1:
            header_end = min(header_end, index)
    header = _CLEARANCE_RE.sub("", block[:header_end])
    rest = block[header_end:]
    indent = _child_indent(block)
    token = f"\n{indent}(clearance {value:g})"
    # The header ends in the whitespace that indents the first child; put the
    # token on its own line just before it.
    tail = re.search(r"\n[ \t]*$", header)
    if tail:
        return header[:tail.start()] + token + header[tail.start():] + rest, 1
    return header + token + f"\n{indent}" + rest, 1


# --------------------------------------------------------------------------- #
# The JUDGE's geometry -- shared so a driver does not re-derive it
# --------------------------------------------------------------------------- #

def segment_rect_distance(rect, start, end) -> float:
    """Distance from an axis-aligned ``rect`` to the segment ``start``-``end``.

    ``rect`` is ``(x0, y0, x1, y1)``. Zero when they touch or overlap. Exact --
    no sampling -- because the number this returns is the plan's own gate B5 and
    a sampled minimum reads low by an unknown amount.
    """
    x0, y0, x1, y1 = rect
    if _segment_hits_rect(rect, start, end):
        return 0.0
    best = min(_point_rect_distance(rect, start), _point_rect_distance(rect, end))
    for corner in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        best = min(best, _point_segment_distance(corner, start, end))
    return best


def segment_segment_distance(a0, a1, b0, b1) -> float:
    """Distance between two 2-D segments. Zero when they cross or touch.

    ⭐ Needed because an **oval or circular pad is a capsule, not a box**, and
    grading a DIP-8's oval pins by their bounding box reads foreign copper as
    *touching* a pad it clears by more than the design clearance — measured on
    ``uc3844_flyback``, where the box model reported −0.004 mm at DRC 0.
    """
    if _segments_cross(a0, a1, b0, b1):
        return 0.0
    return min(_point_segment_distance(a0, b0, b1),
               _point_segment_distance(a1, b0, b1),
               _point_segment_distance(b0, a0, a1),
               _point_segment_distance(b1, a0, a1))


def _point_rect_distance(rect, point) -> float:
    x0, y0, x1, y1 = rect
    dx = max(x0 - point[0], 0.0, point[0] - x1)
    dy = max(y0 - point[1], 0.0, point[1] - y1)
    return math.hypot(dx, dy)


def _point_segment_distance(point, start, end) -> float:
    px, py = point
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segment_hits_rect(rect, start, end) -> bool:
    x0, y0, x1, y1 = rect
    for point in (start, end):
        if x0 <= point[0] <= x1 and y0 <= point[1] <= y1:
            return True
    edges = (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
             ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0)))
    return any(_segments_cross(start, end, a, b) for a, b in edges)


def _segments_cross(p1, p2, p3, p4) -> bool:
    def _orient(a, b, c):
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(value) < 1e-12:
            return 0
        return 1 if value > 0 else -1

    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if d1 != d2 and d3 != d4:
        return True
    return False
