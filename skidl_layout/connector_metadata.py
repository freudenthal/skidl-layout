from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorMatingFace:
    kind: str
    local_exit: str
    confidence: float
    reasons: tuple[str, ...] = ()
    edge_inset_mm: float = 0.0
    local_face_offset_mm: float | None = None


AUDIO_JACK_RE = re.compile(
    r"(audio.?jack|audio.?plug|3\.5\s*mm|3\.5mm|mono.?jack|"
    r"stereo.?jack|trs|trrs|pj320|pj-?320|sj1-35|"
    r"6\.35\s*mm|6\.35mm|1/4\s*(?:in|inch)?|quarter.?inch|phone.?jack)",
    re.I,
)
PANEL_AUDIO_JACK_RE = re.compile(
    r"(thonk|thonkiconn|pj398|pj301|vertical|eurorack.?jack)",
    re.I,
)
BARREL_RE = re.compile(r"(barrel|dc.?jack|power.?jack)", re.I)
HEADER_RE = re.compile(
    r"(pin.?header|header|angled.?header|right.?angle.?header|tagconnect|swd|jtag)",
    re.I,
)
HORIZONTAL_RE = re.compile(r"(horizontal|right.?angle|angled|90.?degree|90deg)", re.I)
JST_RE = re.compile(r"\b(jst|qwiic|stemma\s*qt|stemmaqt)\b", re.I)
TERMINAL_BLOCK_RE = re.compile(
    r"(terminal.?block|screw.?terminal|bornier|phoenix|mkds|tb\d|4ucon|ctbp)",
    re.I,
)
USB_RE = re.compile(
    r"(connector[_\s:/-]*usb|usb[_\s-]*(?:c|micro|mini|a|b)?[_\s-]*"
    r"(?:connector|receptacle|socket)|type[_\s-]?c|usb\d+|usb_c_receptacle|"
    r"type-c-31)",
    re.I,
)


def _part_text(part) -> str:
    if part is None:
        return ""
    chunks = [
        getattr(part, "ref", ""),
        getattr(part, "name", ""),
        getattr(part, "value", ""),
        getattr(part, "foot", ""),
        getattr(part, "footprint", ""),
        getattr(part, "description", ""),
    ]
    return " ".join(str(chunk or "") for chunk in chunks)


def normalize_local_exit(value: object | None) -> str | None:
    text = str(value or "").strip().lower().replace("_", "").replace(" ", "")
    aliases = {
        "+x": "+x",
        "x+": "+x",
        "right": "+x",
        "east": "+x",
        "-x": "-x",
        "x-": "-x",
        "left": "-x",
        "west": "-x",
        "+y": "+y",
        "y+": "+y",
        "bottom": "+y",
        "down": "+y",
        "south": "+y",
        "-y": "-y",
        "y-": "-y",
        "top": "-y",
        "up": "-y",
        "north": "-y",
    }
    return aliases.get(text)


def is_panel_style_audio_jack_text(text: object) -> bool:
    """Return true when text names a front-panel vertical 3.5 mm jack style."""
    combined = str(text or "")
    return bool(
        AUDIO_JACK_RE.search(combined)
        and PANEL_AUDIO_JACK_RE.search(combined)
        and not HORIZONTAL_RE.search(combined)
    )


def is_panel_style_audio_jack_part(part) -> bool:
    return is_panel_style_audio_jack_text(_part_text(part))


def rotation_for_local_exit(edge: str | None, local_exit: str | None) -> float | None:
    """Return the KiCad rotation that points a local connector exit outward."""
    edge = str(edge or "").strip().lower()
    local_exit = normalize_local_exit(local_exit)
    if local_exit is None:
        return None

    local_vectors = {
        "+x": (1, 0),
        "-x": (-1, 0),
        "+y": (0, 1),
        "-y": (0, -1),
    }
    outward_vectors = {
        "right": (1, 0),
        "left": (-1, 0),
        "bottom": (0, 1),
        "top": (0, -1),
    }
    local = local_vectors[local_exit]
    outward = outward_vectors.get(edge)
    if outward is None:
        return None

    for rotation, (cos_v, sin_v) in (
        (0.0, (1, 0)),
        (90.0, (0, 1)),
        (180.0, (-1, 0)),
        (270.0, (0, -1)),
    ):
        x, y = local
        rotated = (x * cos_v + y * sin_v, -x * sin_v + y * cos_v)
        if rotated == outward:
            return rotation
    return None


def _explicit_mating_face(part) -> ConnectorMatingFace | None:
    if part is None:
        return None
    for attr in (
        "mating_face_local_direction",
        "mating_face_direction",
        "connector_exit_direction",
        "cable_exit_direction",
        "local_mating_face",
        "mating_face",
        "local_exit",
    ):
        local_exit = normalize_local_exit(getattr(part, attr, None))
        if local_exit is None:
            continue
        return ConnectorMatingFace(
            kind="explicit",
            local_exit=local_exit,
            confidence=0.98,
            reasons=(f"explicit part.{attr}",),
        )
    return None


def infer_connector_mating_face(
    part=None,
    *,
    text: str = "",
    mating_kind: str | None = None,
) -> ConnectorMatingFace | None:
    """Infer which footprint-local side is the user/cable mating face."""
    explicit = _explicit_mating_face(part)
    if explicit is not None:
        return explicit

    combined = f"{_part_text(part)} {text}".lower()
    footprint = (
        str(getattr(part, "footprint", "") or "").lower()
        if part is not None
        else combined
    )
    kind = str(mating_kind or "").lower()

    is_audio = kind == "audio_jack" or AUDIO_JACK_RE.search(combined)
    if (kind == "panel_jack" or is_audio) and is_panel_style_audio_jack_text(
        combined
    ):
        return None
    if is_audio and ("cui_sj1" in footprint or "sj1-35" in footprint):
        return ConnectorMatingFace(
            kind="audio_jack",
            local_exit="+y",
            confidence=0.9,
            reasons=("CUI SJ1 horizontal jack PCB-edge marker is local +Y",),
        )
    if is_audio and (
        "pj320" in footprint
        or "pj-320" in footprint
        or HORIZONTAL_RE.search(footprint)
    ):
        return ConnectorMatingFace(
            kind="audio_jack",
            local_exit="-x",
            confidence=0.85,
            reasons=("PJ320D-style horizontal jack socket opening is local -X",),
        )

    if kind == "usb" or USB_RE.search(combined):
        face_offset = 3.1 if "usb4105" in footprint else None
        return ConnectorMatingFace(
            kind="usb",
            local_exit="+y",
            confidence=0.9,
            reasons=("KiCad USB receptacle PCB-edge marker is local +Y",),
            local_face_offset_mm=face_offset,
        )

    if kind == "barrel" or BARREL_RE.search(combined):
        return ConnectorMatingFace(
            kind="barrel",
            local_exit="+y",
            confidence=0.78,
            reasons=("common horizontal barrel jack cable entry is local +Y",),
        )

    if kind == "jst" or JST_RE.search(combined):
        face_offset = 2.575 if "jst_sh" in footprint and "horizontal" in footprint else None
        return ConnectorMatingFace(
            kind="jst",
            local_exit="+y",
            confidence=0.85,
            reasons=("side-entry JST/Qwiic connector exits local +Y",),
            local_face_offset_mm=face_offset,
        )

    if TERMINAL_BLOCK_RE.search(combined):
        return ConnectorMatingFace(
            kind="terminal_block",
            local_exit="+y",
            confidence=0.78,
            reasons=("KiCad horizontal terminal block wire-entry side is local +Y",),
        )

    if HEADER_RE.search(combined) and HORIZONTAL_RE.search(combined):
        return ConnectorMatingFace(
            kind="header",
            local_exit="+x",
            confidence=0.75,
            reasons=("right-angle pin header pins extend local +X",),
        )

    return None


def infer_edge_mating_rotation(
    part,
    edge: str | None,
    *,
    text: str = "",
    mating_kind: str | None = None,
) -> float | None:
    mating_face = infer_connector_mating_face(
        part,
        text=text,
        mating_kind=mating_kind,
    )
    if mating_face is None:
        return None
    return rotation_for_local_exit(edge, mating_face.local_exit)
