from __future__ import annotations

import re
from dataclasses import dataclass, field


POWER_NET_RE = re.compile(
    r"^("
    r"VCC|VDD|VDDA|VDDD|AVDD|DVDD|IOVDD|"
    r"VBUS|VIN|VOUT|VRAW|VBAT|BAT|BATT|VREF|V\d+|"
    r"[+-]?\d+(?:V\d*|\.\d+V)"
    r")$",
    re.IGNORECASE,
)
GND_NET_RE = re.compile(r"^(GND|VSS|DGND|AGND|GNDA|GNDD)$", re.IGNORECASE)
DECAP_VALUE_RE = re.compile(r"^(100n|0\.1u)", re.IGNORECASE)
AUDIO_JACK_RE = re.compile(
    r"(audio.?jack|audio.?plug|3\.5\s*mm|3\.5mm|mono.?jack|"
    r"stereo.?jack|trs|trrs|pj320|pj398|pj301|thonk)",
    re.IGNORECASE,
)
PANEL_JACK_RE = re.compile(
    r"(thonk|pj398|pj301|eurorack.?jack|vertical)",
    re.IGNORECASE,
)
EDGE_AUDIO_JACK_RE = re.compile(
    r"(horizontal|right.?angle|edge.?mount|side.?entry|pj-?320|pj320d)",
    re.IGNORECASE,
)
POWER_JACK_RE = re.compile(r"(dc.?jack|barrel|power.?jack)", re.IGNORECASE)
DAISY_SEED_RE = re.compile(
    r"(electrosmith.?daisy|daisy.?seed|electrosmith_daisy_seed)",
    re.IGNORECASE,
)
MODULE_SOCKET_RE = re.compile(
    r"(module.?socket|plug.?in.?module|daughter.?board|daughterboard|"
    r"mezzanine|board.?to.?board|b2b|carrier.?board|dev(?:elopment)?.?board|"
    r"feather|teensy|raspberry.?pi.?pico|arduino)",
    re.IGNORECASE,
)
PIN_SOCKET_RE = re.compile(
    r"(pin.?socket|pinsocket|socket.?strip|female.?header)",
    re.IGNORECASE,
)
PANEL_CONTROL_RE = re.compile(
    r"(switch|button|potentiometer|encoder|rotary|knob|trimmer|"
    r"key.?switch|keyboard|keycap|cherry.?mx|kailh|mx.?key|"
    r"alpha|bourns|songhuei)",
    re.IGNORECASE,
)
LED_UI_RE = re.compile(r"(led|neopixel|ws2812|apa102)", re.IGNORECASE)
SENSOR_UI_RE = re.compile(
    r"(sensor|photodiode|photosensor|light.?dependent|lux|tof|time.?of.?flight|"
    r"temperature|humidity|pressure|imu|accelerometer|gyro|magnetometer|"
    r"mcp9808|bme280|bmp280|vl53|tsl25|veml|ads1115)",
    re.IGNORECASE,
)
CONNECTOR_METADATA_RE = re.compile(
    r"(connector|header|jack|terminal|receptacle|socket)",
    re.IGNORECASE,
)


# The NCNet class, cached once at import (roles sits at the bottom of the
# skidl_layout import graph, so importing skidl here creates no cycle). The
# lookup is a plain module attribute — cheaper than the old per-call
# ``from skidl.net import NCNet`` inside every hot net-walk loop.
try:
    from skidl.net import NCNet as _NCNet
except Exception:  # pragma: no cover - skidl always present in practice
    _NCNet = None


def is_nc_net(net) -> bool:
    """Duck-typed no-connect test used everywhere in place of
    ``isinstance(net, NCNet)``.

    A :class:`skidl_layout.snapshot.SnapshotNet` carries an explicit
    ``is_ncnet`` marker (it can never be a live ``NCNet`` instance); a live net
    still falls through to the real ``isinstance`` check, so behaviour is
    identical on the sequential path.
    """
    # ``is True`` (not just truthy) so a MagicMock net — whose ``is_ncnet``
    # attribute auto-vivifies to a truthy Mock — is not mistaken for an NC net.
    # SnapshotNet always stores a real bool here.
    if getattr(net, "is_ncnet", False) is True:
        return True
    if _NCNet is None:
        return False
    return isinstance(net, _NCNet)


@dataclass
class PartRole:
    ref: str
    role: str
    confidence: float
    reasons: list[str] = field(default_factory=list)


def _ref_prefix(part) -> str:
    ref = str(getattr(part, "ref", "") or "")
    match = re.match(r"[A-Za-z]+", ref)
    return match.group(0).upper() if match else ""


def _is_capacitor_ref(prefix: str) -> bool:
    return prefix.startswith("C") and not prefix.startswith(("CN", "CON"))


def _is_resistor_ref(prefix: str) -> bool:
    return prefix.startswith("R") and prefix not in {"RV"}


def _part_text(part) -> str:
    chunks = [
        getattr(part, "ref", ""),
        getattr(part, "name", ""),
        getattr(part, "value", ""),
        getattr(part, "foot", ""),
        getattr(part, "footprint", ""),
        getattr(part, "description", ""),
    ]
    return " ".join(str(chunk or "") for chunk in chunks).lower()


def _pin_count(part) -> int:
    try:
        return len(part)
    except Exception:
        return len(getattr(part, "pins", []) or [])


def pin_net_names(part) -> list[str]:
    names = []
    for pin in getattr(part, "pins", []) or []:
        net = getattr(pin, "net", None)
        if net is not None and not is_nc_net(net):
            name = getattr(net, "name", None)
            if name:
                names.append(str(name))
    return names


def is_audio_jack_part(part) -> bool:
    text = _part_text(part)
    return bool(AUDIO_JACK_RE.search(text) and not POWER_JACK_RE.search(text))


def _looks_like_module_socket(prefix: str, text: str) -> bool:
    if DAISY_SEED_RE.search(text):
        return True
    if MODULE_SOCKET_RE.search(text):
        return True
    if PIN_SOCKET_RE.search(text) and (
        prefix == "U"
        or re.search(r"\b(module|daughter|carrier|mezzanine|breakout)\b", text)
    ):
        return True
    if prefix == "U" and re.search(r"\bconn_0[12]x\d+\b", text) and "socket" in text:
        return True
    return False


def is_ui_grid_part(part) -> bool:
    """Return true for parts whose panel/front-face grid should be authoritative."""
    prefix = _ref_prefix(part)
    text = _part_text(part)
    if is_audio_jack_part(part):
        return True
    if prefix in {"SW", "S", "RV", "POT", "K", "KEY"} or PANEL_CONTROL_RE.search(
        text
    ):
        return True
    if prefix == "LED" or LED_UI_RE.search(text):
        return True
    if SENSOR_UI_RE.search(text):
        return True
    return False


def has_power_and_ground(part) -> bool:
    nets = pin_net_names(part)
    return any(POWER_NET_RE.match(n) for n in nets) and any(
        GND_NET_RE.match(n) for n in nets
    )


def classify_part(part) -> PartRole:
    ref = str(getattr(part, "ref", "") or "")
    prefix = _ref_prefix(part)
    text = _part_text(part)
    pin_count = _pin_count(part)
    reasons: list[str] = []

    if (
        _is_capacitor_ref(prefix)
        and pin_count == 2
        and DECAP_VALUE_RE.match(str(getattr(part, "value", "") or ""))
    ):
        if has_power_and_ground(part):
            return PartRole(
                ref,
                "decoupling_cap",
                0.95,
                ["2-pin capacitor on power and ground"],
            )

    normalized_text = text.replace("_", " ").replace("-", " ")
    if (
        prefix in {"H", "MH"}
        or "mountinghole" in text
        or "mounting hole" in normalized_text
        or "mountinghole" in str(getattr(part, "footprint", "") or "").lower()
    ):
        reasons.append("mechanical mounting-hole reference or footprint")
        return PartRole(ref, "mounting_hole", 0.95, reasons)

    if prefix in {"SW", "S", "RV", "POT", "K", "KEY"} or PANEL_CONTROL_RE.search(
        text
    ):
        reasons.append("panel/user-control reference or metadata")
        return PartRole(ref, "control", 0.85, reasons)

    if is_audio_jack_part(part):
        if PANEL_JACK_RE.search(text) and not EDGE_AUDIO_JACK_RE.search(text):
            reasons.append("panel/audio jack metadata")
            return PartRole(ref, "panel_jack", 0.9, reasons)
        reasons.append("edge/audio jack metadata")
        return PartRole(ref, "connector", 0.9, reasons)

    if _looks_like_module_socket(prefix, text):
        reasons.append("plug-in module/socket metadata")
        return PartRole(ref, "module_socket", 0.95, reasons)

    if prefix in {"J", "P", "CON", "CN"} or CONNECTOR_METADATA_RE.search(text):
        reasons.append("connector-like reference or metadata")
        return PartRole(ref, "connector", 0.9, reasons)

    if prefix in {"Y", "X"} or any(
        term in text for term in ("crystal", "resonator", "oscillator")
    ):
        reasons.append("timing-source reference or metadata")
        return PartRole(ref, "crystal", 0.85, reasons)

    if prefix == "L":
        return PartRole(ref, "inductor", 0.85, ["inductor reference prefix"])

    if prefix == "D":
        return PartRole(ref, "diode", 0.8, ["diode reference prefix"])

    if any(
        term in text
        for term in ("regulator", "ldo", "buck", "boost", "dcdc", "dc-dc", "converter")
    ):
        return PartRole(ref, "regulator", 0.85, ["power-regulator metadata"])

    if prefix == "U" or pin_count > 2:
        reasons.append("IC-like reference or pin count")
        return PartRole(ref, "ic", 0.75, reasons)

    if (_is_resistor_ref(prefix) or _is_capacitor_ref(prefix)) and pin_count == 2:
        return PartRole(ref, "signal_passive", 0.7, ["2-pin passive"])

    return PartRole(ref, "unknown", 0.1, [])


def classify_parts(circuit) -> dict[str, PartRole]:
    return {part.ref: classify_part(part) for part in circuit.parts}


# --- part-name tokenization (used by scoring's owner-affinity heuristic) -----
# Lives here (not scoring.py) so LayoutContext can precompute per-ref tokens
# without importing scoring (which would create an import cycle). scoring.py
# re-exports these names for backward compatibility.

_TOKEN_GENERIC = {
    "CAP",
    "CAPACITOR",
    "DEVICE",
    "FOOTPRINT",
    "IC",
    "LGA",
    "MCU",
    "METRIC",
    "MODULE",
    "PACKAGE",
    "PKG",
    "RESISTOR",
    "SENSOR",
    "SMD",
}


def _alpha_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in re.finditer(r"[A-Za-z]{3,}", str(text or "")):
        token = match.group(0).upper()
        if token not in _TOKEN_GENERIC:
            tokens.add(token)
        if token[0] in {"C", "R", "L", "D", "U", "J", "Q"} and len(token) >= 4:
            stripped = token[1:]
            if stripped not in _TOKEN_GENERIC:
                tokens.add(stripped)
    return tokens


def _part_tokens(part) -> set[str]:
    tokens: set[str] = set()
    for field_name in ("ref", "name", "value", "footprint"):
        tokens.update(_alpha_tokens(str(getattr(part, field_name, "") or "")))
    return tokens
