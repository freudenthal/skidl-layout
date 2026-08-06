# -*- coding: utf-8 -*-
"""A geometry-only renderer: one **scene**, two emitters (SVG and PNG).

⭐⭐⭐ **This module exists because the renderer was DUPLICATED, and the
duplication is the exact seam standing finding 1 is about.** Roadmap next-steps
item 10, raised by the human on 2026-08-04 after S4's renders: the
scene/SVG/PNG emitter is a real repo artifact and not a spike, but it lived
**only** inside two canary drivers -- 194 lines in ``drive_construct_side.py``
and 185 in ``drive_construct_cell.py``, near-identical, the second a hand-copy
of the first plus ring-2 colours. Its one good property -- **one scene, two
emitters**, so the thing asserted in code and the thing a human looks at cannot
drift -- was preserved *within* each copy and **not across them**, which is how
the third consumer (S5B) got its scene adapter and how the fourth would have got
a third copy.

⛔⛔ **IT KNOWS NOTHING ABOUT THE CONSTRUCTION LOOP.** No
:class:`~skidl_layout.construct.CellResult`, no ``Unit``, no ``Neighbour``, no
``Placement`` -- and no import of :mod:`~skidl_layout.construct` at all, because
``construct`` is a leaf guarded by an import-aware test and by
``ESCAPE_MAP_CONSUMERS``, whose companion test requires every permitted consumer
to be a leaf too. A renderer inside ``skidl_layout`` that imported ``construct``
would break that guard; one that takes plain boxes, pads, lines, **segments**
and vias does not. ⭐ **This module imports nothing from this package at all** --
it is the strongest form of the property, and ``test_scene_render.py`` asserts
it.

⛔ **A render that draws nothing must RAISE** (standing finding 1, six
instances): :func:`scene_to_svg` refuses a scene with no boxes, and the caller
is expected to assert its own counts -- *"segments > 0 when copper was
routed"* -- before any eye is used. **Never read presence, or absence, off a
picture.**

The scene, stated once::

    scene = {
      "title":    str,                                   # <title> text
      "caption":  str,
      "legend":   str,
      "extent":   (x0, y0, x1, y1),                      # world mm
      "boxes":    [(ref, kind, courtyard_box, physical_box), ...],
      "pads":     [(ref, number, x, y, size_x, size_y), ...],
      "lines":    [(x1, y1, x2, y2, svg_label[, png_label]), ...],
      "segments": [(x1, y1, x2, y2, width_mm, layer), ...],   # optional
      "vias":     [(x, y, diameter_mm), ...],                 # optional
      "labels":   [(x, y, text), ...],                        # optional
      "style":    {...},                                      # optional
    }

⚠ **A ``lines`` entry is the PAIR, not the copper.** A ``PairResult`` carries no
path and inventing one is forbidden. ``segments`` and ``vias`` are the real
thing, parsed out of a really-routed board by the caller, and they are drawn
**beneath** the part boxes so the pair line stays legible on top.

⛔ **Style is data, not a fork.** The two shipped drivers differ in scale, font
sizes and label text; those differences are :data:`SIDE_STYLE` and
:data:`CELL_STYLE` rather than two functions, so the S3 and S4/S5/S5B renders
stay **byte-identical** across the promotion and the guard that says so is a
plain diff.
"""

from __future__ import annotations

__all__ = [
    "BOARD_STYLE",
    "CELL_STYLE",
    "DEFAULT_STYLE",
    "SIDE_STYLE",
    "SceneError",
    "resolve_style",
    "scene_to_png",
    "scene_to_svg",
]


class SceneError(RuntimeError):
    """⛔ A scene this module refuses to draw -- see standing finding 1."""


#: ⛔ Every tunable the two shipped emitters differ in, as **data**. A key that
#: is absent from a caller's ``style`` takes the value here.
DEFAULT_STYLE: dict = {
    "scale": 9.0,
    "margin": 26.0,
    "background": "#fbfbfb",
    "fills": {"anchor": "#d6eaf8", "neighbour": "#d5f5e3",
              "ring2": "#ebdef0"},
    "strokes": {"anchor": "#2874a6", "neighbour": "#1e8449",
                "ring2": "#7d3c98"},
    "ref_font": 10,
    "ref_dy": -3.0,
    "pad_fill": "#b7950b",
    "pad_font": 5,
    "pad_dy": 2.0,
    "line_colour": "#c0392b",
    "line_width": 1.4,
    "line_font": 8,
    "line_dy": -3.0,
    "segment_colour": "#1f618d",
    "segment_opacity": 0.85,
    #: ⛔⛔ **S7: a STYLE key, and it exists because a picture lied while every
    #: count was right.** Copper is drawn first so boxes stay legible on top;
    #: at L1 the anchor box is a chip and that is correct. At **L2** the anchor
    #: box is a whole cell covering most of the board, and its 0.6 fill hid
    #: **268 of 268** routed segments on ``lt8710_sepic`` while
    #: ``segments_drawn == segments_routed`` said everything was fine. ⭐ The
    #: fix is DATA, not a fork: the default is the value the emitter has always
    #: written, so every recorded render stays byte-identical.
    "box_fill_opacity": 0.6,
    "png_box_fill": True,
    "via_colour": "#8e44ad",
    "label_colour": "#111",
    "label_font": 8,
    # -- the PNG emitter ------------------------------------------------- #
    "png_scale_factor": 2.5,
    "png_margin_factor": 2.0,
    "png_ref_dx": -12.0,
    "png_ref_dy": -14.0,
    "png_pad_numbers": False,
    "png_pad_dx": -4.0,
    "png_pad_dy": -5.0,
    "png_line_width": 3,
    "png_line_dx": -24.0,
    "png_line_dy": -12.0,
    "png_segment_width": 2,
    "png_extra_height": 60,
}

#: ⛔ S4/S5/S5B's style. Identical to :data:`DEFAULT_STYLE`; named so a driver
#: says which picture it is drawing rather than relying on a default.
CELL_STYLE: dict = dict(DEFAULT_STYLE)

#: ⛔ S7's L2 style. **The only differences are the two box-fill keys**, because
#: at board scale the anchor unit's box is most of the picture and an opaque
#: fill hides the copper underneath it. ⭐ Everything else is
#: :data:`DEFAULT_STYLE`, so this is a style and not a second renderer.
BOARD_STYLE: dict = dict(DEFAULT_STYLE, box_fill_opacity=0.10,
                         png_box_fill=False)

#: ⛔ S3's style. Every one of these seven overrides is a **measured** byte
#: difference against ``construct_out/renders/*.svg``; none of them is taste.
SIDE_STYLE: dict = dict(
    DEFAULT_STYLE,
    scale=14.0,
    ref_font=11,
    pad_font=7,
    pad_dy=3.0,
    line_width=1.6,
    line_font=9,
    line_dy=-4.0,
    png_scale_factor=3.0,
    png_ref_dy=-16.0,
    png_pad_numbers=True,
    png_line_width=4,
    png_line_dx=-30.0,
    png_line_dy=-16.0,
)


def resolve_style(scene) -> dict:
    """The scene's style, over :data:`DEFAULT_STYLE`. ⛔ Never mutates either."""
    style = dict(DEFAULT_STYLE)
    supplied = dict(scene.get("style") or {})
    for key in ("fills", "strokes"):
        if key in supplied:
            merged = dict(DEFAULT_STYLE[key])
            merged.update(supplied.pop(key))
            style[key] = merged
    unknown = sorted(set(supplied) - set(DEFAULT_STYLE))
    if unknown:
        raise SceneError(
            f"style key(s) {unknown} are not declared in DEFAULT_STYLE -- a "
            f"silently ignored style key is a picture that does not match the "
            f"caller's intent, and nothing downstream would say so")
    style.update(supplied)
    return style


def _escape(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _checked(scene) -> tuple:
    """⛔ The observes-nothing gate, in one place for both emitters."""
    boxes = list(scene.get("boxes") or ())
    if not boxes:
        raise SceneError(
            "the scene carries ZERO boxes -- a render that draws nothing is "
            "indistinguishable from one that drew everything (standing "
            "finding 1, whose fifth instance was a viewer that drew 0 segments "
            "against a cache holding 482)")
    extent = tuple(scene.get("extent") or ())
    if len(extent) != 4:
        raise SceneError(f"the scene's extent is {extent!r}, not (x0,y0,x1,y1)")
    if extent[2] - extent[0] <= 0 or extent[3] - extent[1] <= 0:
        raise SceneError(f"the scene's extent {extent!r} has no area")
    return (boxes, list(scene.get("pads") or ()),
            list(scene.get("lines") or ()), list(scene.get("segments") or ()),
            list(scene.get("vias") or ()), list(scene.get("labels") or ()),
            extent)


def _labels_of(line) -> tuple:
    """``(svg label, png label)`` from a 5- or 6-tuple line."""
    svg = str(line[4]) if len(line) > 4 else ""
    png = str(line[5]) if len(line) > 5 else svg
    return svg, png


def scene_to_svg(scene) -> str:
    """The scene as SVG text. ⛔ One walk of the data, never two."""
    boxes, pads, lines, segments, vias, labels, extent = _checked(scene)
    style = resolve_style(scene)
    scale, margin = float(style["scale"]), float(style["margin"])
    x0, y0, x1, y1 = extent
    width = (x1 - x0) * scale + 2 * margin
    height = (y1 - y0) * scale + 2 * margin + 40.0
    fills, strokes = style["fills"], style["strokes"]

    def _sx(value):
        return margin + (value - x0) * scale

    def _sy(value):
        return margin + (value - y0) * scale

    body = [f'<rect x="0" y="0" width="{width:.0f}" height="{height:.0f}" '
            f'fill="{style["background"]}"/>']
    # ⛔ Copper FIRST, so the part boxes and the pair lines stay legible on top.
    for sx1, sy1, sx2, sy2, swidth, layer in segments:
        body.append(
            f'<line class="segment" x1="{_sx(sx1):.1f}" y1="{_sy(sy1):.1f}" '
            f'x2="{_sx(sx2):.1f}" y2="{_sy(sy2):.1f}" '
            f'stroke="{style["segment_colour"]}" '
            f'stroke-opacity="{style["segment_opacity"]}" '
            f'stroke-width="{max(1.0, float(swidth) * scale):.1f}" '
            f'stroke-linecap="round" data-layer="{_escape(layer)}"/>')
    for vx, vy, diameter in vias:
        body.append(
            f'<circle class="via" cx="{_sx(vx):.1f}" cy="{_sy(vy):.1f}" '
            f'r="{max(1.5, float(diameter) * scale / 2.0):.1f}" '
            f'fill="none" stroke="{style["via_colour"]}" stroke-width="1.2"/>')
    for ref, kind, court, phys in boxes:
        stroke = strokes[kind]
        body.append(
            f'<rect class="courtyard" x="{_sx(court[0]):.1f}" '
            f'y="{_sy(court[1]):.1f}" '
            f'width="{(court[2] - court[0]) * scale:.1f}" '
            f'height="{(court[3] - court[1]) * scale:.1f}" fill="none" '
            f'stroke="{stroke}" stroke-width="1.0" stroke-dasharray="4 3"/>')
        body.append(
            f'<rect class="part" x="{_sx(phys[0]):.1f}" '
            f'y="{_sy(phys[1]):.1f}" '
            f'width="{(phys[2] - phys[0]) * scale:.1f}" '
            f'height="{(phys[3] - phys[1]) * scale:.1f}" '
            f'fill="{fills[kind]}" '
            f'fill-opacity="{style["box_fill_opacity"]}" stroke="{stroke}" '
            f'stroke-width="1.4"/>')
        body.append(
            f'<text x="{_sx((phys[0] + phys[2]) / 2):.1f}" '
            f'y="{_sy(phys[1]) + style["ref_dy"]:.1f}" '
            f'font-size="{style["ref_font"]}" text-anchor="middle" '
            f'font-family="monospace" fill="{stroke}">{_escape(ref)}</text>')
    for _ref, number, px, py, psx, psy in pads:
        body.append(
            f'<rect x="{_sx(px - psx / 2):.1f}" y="{_sy(py - psy / 2):.1f}" '
            f'width="{psx * scale:.1f}" height="{psy * scale:.1f}" '
            f'fill="{style["pad_fill"]}" fill-opacity="0.55" stroke="none"/>')
        body.append(
            f'<text x="{_sx(px):.1f}" y="{_sy(py) + style["pad_dy"]:.1f}" '
            f'font-size="{style["pad_font"]}" text-anchor="middle" '
            f'font-family="monospace" fill="#111">{_escape(number)}</text>')
    for line in lines:
        ax, ay, bx, by = line[0], line[1], line[2], line[3]
        label, _png = _labels_of(line)
        body.append(
            f'<line class="pair" x1="{_sx(ax):.1f}" y1="{_sy(ay):.1f}" '
            f'x2="{_sx(bx):.1f}" y2="{_sy(by):.1f}" '
            f'stroke="{style["line_colour"]}" '
            f'stroke-width="{style["line_width"]}"/>')
        body.append(
            f'<text x="{_sx((ax + bx) / 2):.1f}" '
            f'y="{_sy((ay + by) / 2) + style["line_dy"]:.1f}" '
            f'font-size="{style["line_font"]}" text-anchor="middle" '
            f'font-family="monospace" fill="{style["line_colour"]}">'
            f'{_escape(label)}</text>')
    for lx, ly, text in labels:
        body.append(
            f'<text class="note" x="{_sx(lx):.1f}" y="{_sy(ly):.1f}" '
            f'font-size="{style["label_font"]}" text-anchor="middle" '
            f'font-family="monospace" fill="{style["label_colour"]}">'
            f'{_escape(text)}</text>')
    body.append(
        f'<text x="{margin:.1f}" y="{height - 22:.1f}" font-size="12" '
        f'font-family="monospace" fill="#111">'
        f'{_escape(scene.get("caption", ""))}</text>')
    body.append(
        f'<text x="{margin:.1f}" y="{height - 8:.1f}" font-size="10" '
        f'font-family="monospace" fill="#555">'
        f'{_escape(scene.get("legend", ""))}</text>')
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
            f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">'
            f'<title>{_escape(scene.get("title", ""))}</title>')
    return head + "".join(body) + "</svg>"


def scene_to_png(scene, path) -> str | None:
    """⭐ **The same scene, rasterised, so the eyes-on pass is a real look.**

    ⛔ A second *emitter* over one *scene*, never a second walk of the source
    data. Returns ``None`` when Pillow is absent -- the SVG is still written and
    the caller says the PNG is missing rather than pretending it is there.
    """
    boxes, pads, lines, segments, vias, labels, extent = _checked(scene)
    try:
        from PIL import Image, ImageDraw
    except Exception:                                           # noqa: BLE001
        return None

    style = resolve_style(scene)
    scale = float(style["scale"]) * float(style["png_scale_factor"])
    margin = float(style["margin"]) * float(style["png_margin_factor"])
    x0, y0, x1, y1 = extent
    width = int((x1 - x0) * scale + 2 * margin)
    height = int((y1 - y0) * scale + 2 * margin
                 + int(style["png_extra_height"]))
    image = Image.new("RGB", (width, height), style["background"])
    draw = ImageDraw.Draw(image)
    fills, strokes = style["fills"], style["strokes"]

    def _sx(value):
        return margin + (value - x0) * scale

    def _sy(value):
        return margin + (value - y0) * scale

    for sx1, sy1, sx2, sy2, swidth, _layer in segments:
        draw.line([_sx(sx1), _sy(sy1), _sx(sx2), _sy(sy2)],
                  fill=style["segment_colour"],
                  width=max(1, int(round(float(swidth) * scale))))
    for vx, vy, diameter in vias:
        radius = max(2.0, float(diameter) * scale / 2.0)
        draw.ellipse([_sx(vx) - radius, _sy(vy) - radius,
                      _sx(vx) + radius, _sy(vy) + radius],
                     outline=style["via_colour"], width=2)
    for ref, kind, court, phys in boxes:
        draw.rectangle([_sx(court[0]), _sy(court[1]), _sx(court[2]),
                        _sy(court[3])], outline=strokes[kind], width=2)
        draw.rectangle([_sx(phys[0]), _sy(phys[1]), _sx(phys[2]),
                        _sy(phys[3])],
                       fill=(fills[kind] if style["png_box_fill"] else None),
                       outline=strokes[kind], width=3)
        draw.text((_sx((phys[0] + phys[2]) / 2) + style["png_ref_dx"],
                   _sy(phys[1]) + style["png_ref_dy"]),
                  str(ref), fill=strokes[kind])
    for _ref, number, px, py, psx, psy in pads:
        draw.rectangle([_sx(px - psx / 2), _sy(py - psy / 2),
                        _sx(px + psx / 2), _sy(py + psy / 2)],
                       fill=style["pad_fill"])
        if style["png_pad_numbers"]:
            draw.text((_sx(px) + style["png_pad_dx"],
                       _sy(py) + style["png_pad_dy"]), str(number),
                      fill="#111")
    for line in lines:
        ax, ay, bx, by = line[0], line[1], line[2], line[3]
        _svg, label = _labels_of(line)
        draw.line([_sx(ax), _sy(ay), _sx(bx), _sy(by)],
                  fill=style["line_colour"], width=int(style["png_line_width"]))
        draw.text(((_sx(ax) + _sx(bx)) / 2 + style["png_line_dx"],
                   (_sy(ay) + _sy(by)) / 2 + style["png_line_dy"]),
                  label, fill=style["line_colour"])
    for lx, ly, text in labels:
        draw.text((_sx(lx), _sy(ly)), str(text), fill=style["label_colour"])
    draw.text((margin, height - 40), scene.get("caption", ""), fill="#111")
    draw.text((margin, height - 24), scene.get("legend", ""), fill="#555")
    image.save(path)
    return path
