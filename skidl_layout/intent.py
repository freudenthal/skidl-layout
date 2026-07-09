from __future__ import annotations

import re
from dataclasses import dataclass, field

from .backends import OptionalBackendStatus, optional_backend_status
from .constraints import (
    AlignConstraint,
    DistributeConstraint,
    EdgeAnchor,
    FaceEdgeConstraint,
    FarConstraint,
    FixedPosition,
    KeepOut,
    NearConstraint,
)
from .grid import grid_rows_for_refs
from .connector_metadata import (
    TERMINAL_BLOCK_RE,
    infer_connector_mating_face,
    infer_edge_mating_rotation,
    is_panel_style_audio_jack_text,
    rotation_for_local_exit,
)
from .roles import (
    GND_NET_RE,
    POWER_NET_RE,
    PartRole,
    classify_parts,
    is_audio_jack_part,
    pin_net_names,
)


CHANNEL_RE = re.compile(r"(?:^|[_/.-])(?:CH|CHAN|CHANNEL)(\d+)(?:[_/.-]|$)", re.I)
REF_SUFFIX_RE = re.compile(r"([A-Za-z]+)(\d+)$")
MUX_RE = re.compile(r"(mux|multiplex|tca954|pca954|switch)", re.I)
RF_RE = re.compile(
    r"(antenna|(?:^|[\s_/:.,-])rf(?:$|[\s_/:.,-])|wi[-\s]?fi|"
    r"(?:^|[\s_/:.,-])ble(?:$|[\s_/:.,-])|bluetooth|esp32|nrf52|wroom)",
    re.I,
)
UI_RE = re.compile(r"(button|switch|encoder|pot|display|oled|lcd|led)", re.I)
DEBUG_RE = re.compile(r"(swd|jtag|icsp|debug|program|uart|serial)", re.I)
POWER_INPUT_RE = re.compile(r"(usb|barrel|battery|batt|jst|terminal|power)", re.I)
EURORACK_POWER_RE = re.compile(r"(eurorack|doepfer|box.?header|idc|shrouded)", re.I)
BARREL_RE = re.compile(r"(barrel|dc jack|power jack)", re.I)
JST_RE = re.compile(r"\b(jst|battery|batt|lipo|li-po)\b", re.I)
FFC_RE = re.compile(r"\b(ffc|fpc|flat flex|ribbon)\b", re.I)
HEADER_RE = re.compile(r"\b(header|pinheader|pin header|tagconnect|swd|jtag)\b", re.I)
AUDIO_JACK_RE = re.compile(
    r"(audio.?jack|audio.?plug|3\.5\s*mm|3\.5mm|mono.?jack|"
    r"stereo.?jack|trs|trrs|pj320|6\.35\s*mm|6\.35mm|"
    r"1/4\s*(?:in|inch)?|quarter.?inch|phone.?jack)",
    re.I,
)
PANEL_MOUNT_JACK_RE = re.compile(
    r"(panel.?jack|guitar.?pedal|stompbox|6\.35\s*mm|6\.35mm|"
    r"1/4\s*(?:in|inch)?|quarter.?inch|phone.?jack|neutrik|lumberg)",
    re.I,
)
EDGE_AUDIO_JACK_RE = re.compile(
    r"(horizontal|right.?angle|edge.?mount|side.?entry|pj-?320|pj320d)",
    re.I,
)
INTERNAL_HEADER_RE = re.compile(
    r"\b(oled|lcd|display|tft|screen|daughter|mezzanine|board.?to.?board|b2b|module|socket)\b",
    re.I,
)
BUTTON_RE = re.compile(r"\b(button|pushbutton|tact|switch|footswitch)\b", re.I)
LED_RE = re.compile(r"\b(led|neopixel|indicator)\b", re.I)
DISPLAY_RE = re.compile(r"\b(display|oled|lcd|screen)\b", re.I)
POT_ENCODER_RE = re.compile(r"\b(pot|potentiometer|encoder|knob)\b", re.I)
KEY_RE = re.compile(
    r"\b(key.?switch|keyboard|keycap|cherry.?mx|kailh|mx.?key)\b",
    re.I,
)
SENSOR_RE = re.compile(
    r"\b(sensor|photodiode|photosensor|light.?dependent|lux|tof|"
    r"time.?of.?flight|temperature|humidity|pressure|imu|accelerometer|"
    r"gyro|magnetometer|mcp9808|bme280|bmp280|vl53|tsl25|veml|ads1115)\b",
    re.I,
)
COAX_RE = re.compile(r"(?:^|[\s_/:.,-])(coax|coaxial|sma|u\.?fl|ipex|antenna|rf.?conn)(?:[\s_/:.,-]|$)", re.I)
XTAL_PIN_RE = re.compile(
    r"(?:^|[/_.\s-])(?:XTAL(?:I|O|IN|OUT)?\d*|EXTAL\d*|XIN\d*|XOUT\d*|"
    r"XI\d*|XO\d*|OSC(?:_?IN|_?OUT)?\d*|HFXTAL_[IO])(?:$|[/_.\s-])",
    re.I,
)
AUDIO_IC_RE = re.compile(r"\b(dac|codec|audio|i2s|pcm510|wm874|max9814|sgtl5000|tlv320)\b", re.I)
DISPLAY_NET_RE = re.compile(r"(?:^|[_/.\s-])(eink|e.ink|oled|lcd|disp|tft|epd|dc|busy)(?:[_/.\s-]|$)", re.I)
NAV_RE = re.compile(r"\b(nav|joystick|d-pad|dpad|5.?way|4.?way)\b", re.I)
QWIIC_RE = re.compile(r"\b(qwiic|stemma\s*qt|stemmaqt)\b", re.I)
USB_CONNECTOR_RE = re.compile(
    r"(connector[_\s:/-]*usb|usb[_\s-]*(?:c|micro|mini|a|b)?[_\s-]*(?:connector|receptacle|socket)|"
    r"type[_\s-]?c|usb4105|type-c-31|usb_c_receptacle)",
    re.I,
)
INLINE_INPUT_RE = re.compile(r"(?:^|[\s_/:.,-])(in|input)(?:$|[\s_/:.,-])", re.I)
INLINE_OUTPUT_RE = re.compile(r"(?:^|[\s_/:.,-])(out|output)(?:$|[\s_/:.,-])", re.I)
MIDI_RE = re.compile(r"\bmidi\b", re.I)
LARGE_MODULE_RE = re.compile(
    r"\b(esp32|esp32-?s3|wroom|wrover|nrf52|pico|teensy|daisy|"
    r"module|castellated|stamp|s3-?mini)\b",
    re.I,
)
MODULE_SOCKET_RE = re.compile(
    r"(module.?socket|plug.?in.?module|daughter.?board|daughterboard|"
    r"mezzanine|board.?to.?board|b2b|carrier.?board|dev(?:elopment)?.?board|"
    r"feather|teensy|raspberry.?pi.?pico|arduino)",
    re.I,
)
PIN_SOCKET_RE = re.compile(
    r"(pin.?socket|pinsocket|socket.?strip|female.?header)",
    re.I,
)


@dataclass
class PlacementIntent:
    ref: str
    kind: str
    priority: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class ChannelSlot:
    channel_number: int
    slot_index: int
    refs: list[str] = field(default_factory=list)
    sensor_refs: list[str] = field(default_factory=list)
    passive_refs: list[str] = field(default_factory=list)
    connector_refs: list[str] = field(default_factory=list)
    other_refs: list[str] = field(default_factory=list)


@dataclass
class RepeatedChannelIntent:
    name: str
    refs: list[str] = field(default_factory=list)
    channel_numbers: list[int] = field(default_factory=list)
    refs_by_channel: dict[int, list[str]] = field(default_factory=dict)
    pattern: str = ""
    shared_refs: list[str] = field(default_factory=list)
    controller_refs: list[str] = field(default_factory=list)
    slots: list[ChannelSlot] = field(default_factory=list)
    backbone_nets: list[str] = field(default_factory=list)


@dataclass
class MatingIntent:
    ref: str
    kind: str
    edge_preference: str | None = None
    mating_side: str | None = None
    allowed_rotations: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)
    confidence: float = 0.5
    reasons: list[str] = field(default_factory=list)


@dataclass
class PlacementIntentPlan:
    intents: dict[str, list[PlacementIntent]] = field(default_factory=dict)
    edge_anchors: list[EdgeAnchor] = field(default_factory=list)
    face_edges: list[FaceEdgeConstraint] = field(default_factory=list)
    keepouts: list[KeepOut] = field(default_factory=list)
    fixed_positions: list[FixedPosition] = field(default_factory=list)
    near_constraints: list[NearConstraint] = field(default_factory=list)
    far_constraints: list[FarConstraint] = field(default_factory=list)
    align_constraints: list[AlignConstraint] = field(default_factory=list)
    distribute_constraints: list[DistributeConstraint] = field(default_factory=list)
    repeated_channels: list[RepeatedChannelIntent] = field(default_factory=list)
    mating_intents: list[MatingIntent] = field(default_factory=list)
    assembly_sides: dict[str, str] = field(default_factory=dict)
    assembly_policy: str = "single_sided"
    backend_status: OptionalBackendStatus = field(default_factory=optional_backend_status)
    warnings: list[str] = field(default_factory=list)

    def intents_for(self, ref: str) -> list[PlacementIntent]:
        return self.intents.get(ref, [])

    def refs_with_kind(self, kind: str) -> list[str]:
        return [
            ref
            for ref, intents in self.intents.items()
            if any(intent.kind == kind for intent in intents)
        ]

    def summary(self) -> str:
        lines = ["Placement intent:"]
        if self.backend_status.enabled:
            lines.append(
                "  optional backends: " + ", ".join(self.backend_status.enabled)
            )
        kind_counts: dict[str, int] = {}
        for intents in self.intents.values():
            for intent in intents:
                kind_counts[intent.kind] = kind_counts.get(intent.kind, 0) + 1
        for kind, count in sorted(kind_counts.items()):
            lines.append(f"  {kind}: {count}")
        if self.edge_anchors:
            lines.append(f"  inferred edge anchors: {len(self.edge_anchors)}")
        if self.face_edges:
            lines.append(f"  inferred face-edge constraints: {len(self.face_edges)}")
        if self.fixed_positions:
            lines.append(f"  inferred fixed positions: {len(self.fixed_positions)}")
        if self.near_constraints:
            lines.append(f"  near constraints: {len(self.near_constraints)}")
        if self.far_constraints:
            lines.append(f"  far constraints: {len(self.far_constraints)}")
        if self.align_constraints:
            lines.append(f"  align constraints: {len(self.align_constraints)}")
        if self.distribute_constraints:
            lines.append(
                f"  distribute constraints: {len(self.distribute_constraints)}"
            )
        if self.mating_intents:
            lines.append(f"  mating intents: {len(self.mating_intents)}")
        if self.repeated_channels:
            lines.append(f"  repeated channel groups: {len(self.repeated_channels)}")
        if self.assembly_sides:
            counts: dict[str, int] = {}
            for side in self.assembly_sides.values():
                counts[side] = counts.get(side, 0) + 1
            detail = ", ".join(f"{side}: {counts[side]}" for side in sorted(counts))
            lines.append(f"  assembly sides: {detail}")
        if self.assembly_policy != "single_sided":
            lines.append(f"  assembly policy: {self.assembly_policy}")
        if self.warnings:
            lines.append("Warnings:")
            for warning in self.warnings[:20]:
                lines.append(f"  {warning}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "intents": {
                ref: [
                    {
                        "kind": intent.kind,
                        "priority": intent.priority,
                        "reasons": list(intent.reasons),
                    }
                    for intent in intents
                ]
                for ref, intents in self.intents.items()
            },
            "edge_anchors": [
                {
                    "ref": anchor.ref,
                    "edge": anchor.edge,
                    "offset_mm": anchor.offset_mm,
                    "inset_mm": anchor.inset_mm,
                    "rot_deg": anchor.rot_deg,
                }
                for anchor in self.edge_anchors
            ],
            "align_constraints": [
                {
                    "refs": list(constraint.refs),
                    "axis": constraint.axis,
                    "value_mm": constraint.value_mm,
                }
                for constraint in self.align_constraints
            ],
            "distribute_constraints": [
                {
                    "refs": list(constraint.refs),
                    "axis": constraint.axis,
                    "start_mm": constraint.start_mm,
                    "end_mm": constraint.end_mm,
                }
                for constraint in self.distribute_constraints
            ],
            "fixed_positions": [
                {
                    "ref": fixed.ref,
                    "x_mm": fixed.x_mm,
                    "y_mm": fixed.y_mm,
                    "rot_deg": fixed.rot_deg,
                }
                for fixed in self.fixed_positions
            ],
            "assembly_sides": dict(self.assembly_sides),
            "assembly_policy": self.assembly_policy,
            "warnings": list(self.warnings),
        }


def normalize_assembly_policy(value: object | None) -> str:
    """Normalize MCP/layout assembly policy labels."""
    text = str(value or "single_sided").strip().lower().replace("-", "_")
    aliases = {
        "single": "single_sided",
        "single_side": "single_sided",
        "one_sided": "single_sided",
        "one_side": "single_sided",
        "front_only": "single_sided",
        "double": "double_sided",
        "double_side": "double_sided",
        "dual_sided": "double_sided",
        "dual_side": "double_sided",
        "two_sided": "double_sided",
        "two_side": "double_sided",
    }
    text = aliases.get(text, text)
    if text in {"single_sided", "double_sided"}:
        return text
    return "single_sided"


def normalize_assembly_side(value: object | None) -> str | None:
    """Normalize explicit per-part assembly side labels."""
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "f": "front",
        "top": "front",
        "component": "front",
        "component_side": "front",
        "b": "back",
        "bottom": "back",
        "rear": "back",
        "solder": "back",
        "solder_side": "back",
        "mech": "mechanical",
        "mounting": "mechanical",
    }
    text = aliases.get(text, text)
    if text in {"front", "back", "mechanical"}:
        return text
    return None


def normalize_board_edge(value: object | None) -> str | None:
    """Normalize explicit per-part board-edge labels."""
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "north": "top",
        "upper": "top",
        "south": "bottom",
        "lower": "bottom",
        "west": "left",
        "east": "right",
    }
    text = aliases.get(text, text)
    if text in {"top", "bottom", "left", "right"}:
        return text
    return None


def _explicit_part_assembly_side(part) -> str | None:
    for attr in ("assembly_side", "placement_side", "pcb_side", "side"):
        side = normalize_assembly_side(getattr(part, attr, None))
        if side is not None:
            return side
    return None


def _explicit_part_edge_anchor(part) -> tuple[str, float | None, float | None] | None:
    for attr in (
        "edge_preference",
        "edge_anchor",
        "placement_edge",
        "board_edge",
        "mating_edge",
    ):
        edge = normalize_board_edge(getattr(part, attr, None))
        if edge is None:
            continue
        offset = None
        for offset_attr in ("edge_offset_mm", "placement_offset_mm"):
            raw = getattr(part, offset_attr, None)
            if raw is None:
                continue
            try:
                offset = float(raw)
            except (TypeError, ValueError):
                offset = None
            break
        rot = None
        for rot_attr in (
            "edge_rot_deg",
            "edge_rotation_deg",
            "mating_rot_deg",
            "mating_rotation_deg",
        ):
            raw = getattr(part, rot_attr, None)
            if raw is None:
                continue
            try:
                rot = float(raw) % 360.0
            except (TypeError, ValueError):
                rot = None
            break
        return edge, offset, rot
    return None


def _rotation_for_local_exit(edge: str, local_exit: str) -> float | None:
    """Return KiCad rotation that points a known local connector exit outward."""
    return rotation_for_local_exit(edge, local_exit)


def _default_edge_rotation_for_part(
    part,
    text: str,
    mating_kind: str | None,
    edge: str | None,
) -> float | None:
    """Infer outward-facing rotation for common edge-mounted footprints."""
    edge = normalize_board_edge(edge)
    if edge is None:
        return None
    return infer_edge_mating_rotation(
        part,
        edge,
        text=text,
        mating_kind=mating_kind,
    )


def _default_edge_inset_for_part(
    text: str,
    mating_kind: str | None,
    part=None,
) -> float:
    """Use true board-edge placement for connectors that need cable access."""
    mating_face = infer_connector_mating_face(
        part,
        text=text,
        mating_kind=mating_kind,
    )
    if mating_face is not None:
        return mating_face.edge_inset_mm
    if str(mating_kind or "").lower() in {"usb", "jst", "audio_jack", "barrel"}:
        return 0.0
    if AUDIO_JACK_RE.search(text) or QWIIC_RE.search(text) or USB_CONNECTOR_RE.search(text):
        return 0.0
    return 0.5


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


def _part_ref(part) -> str:
    return str(getattr(part, "ref", "") or "")


def _part_pin_count(part) -> int:
    try:
        return len(part)
    except Exception:
        return len(getattr(part, "pins", []) or [])


def _has_explicit_floorplan_intent(circuit, floorplan_meta: dict | None) -> bool:
    meta = floorplan_meta or {}
    for key in (
        "fixed_positions",
        "edge_anchors",
        "keepouts",
        "zones",
        "align_constraints",
        "distribute_constraints",
        "grids",
        "grid_fixed_positions",
    ):
        if int(meta.get(key, 0) or 0) > 0:
            return True
    if meta.get("assembly_sides") or meta.get("edge_anchor_refs"):
        return True
    for part in getattr(circuit, "parts", []) or []:
        if (
            _explicit_part_edge_anchor(part) is not None
            or _explicit_part_assembly_side(part) is not None
        ):
            return True
        for attr in ("x_mm", "y_mm", "placement_x_mm", "placement_y_mm"):
            if getattr(part, attr, None) is not None:
                return True
    return False


def classify_floorplan_intent_gap(
    circuit,
    *,
    floorplan_meta: dict | None = None,
) -> dict:
    """Detect complex mechanical boards that need explicit placement intent.

    This is intentionally a conservative classifier for product/MCP preflight:
    it catches large module boards with several connector/mechanical subjects
    and no explicit floorplan, without making claims about final placement.
    """

    parts = list(getattr(circuit, "parts", []) or [])
    if not parts:
        return {"needs_floorplan": False, "reason": "empty circuit"}
    if _has_explicit_floorplan_intent(circuit, floorplan_meta):
        return {"needs_floorplan": False, "reason": "explicit floorplan intent present"}

    roles = classify_parts(circuit)
    large_modules: list[str] = []
    connector_refs: list[str] = []
    mechanical_refs: list[str] = []

    for part in parts:
        ref = _part_ref(part)
        if not ref:
            continue
        role = roles.get(ref)
        role_name = role.role if role is not None else ""
        text = _part_text(part)
        pin_count = _part_pin_count(part)
        footprint = str(getattr(part, "footprint", "") or getattr(part, "foot", "") or "")

        if (
            role_name == "module_socket"
            or LARGE_MODULE_RE.search(text)
            or "module" in footprint.lower()
            or (role_name == "ic" and pin_count >= 28)
        ):
            large_modules.append(ref)

        nets = pin_net_names(part)
        mating = _mating_intent_for_part(ref, text, role, nets)
        if role_name in {"connector", "panel_jack", "module_socket"} or mating is not None:
            connector_refs.append(ref)
        if role_name in {"connector", "panel_jack", "module_socket", "mounting_hole", "control"}:
            mechanical_refs.append(ref)

    connector_refs = sorted(set(connector_refs), key=_natural_ref_key)
    mechanical_refs = sorted(set(mechanical_refs), key=_natural_ref_key)
    large_modules = sorted(set(large_modules), key=_natural_ref_key)

    needs_floorplan = bool(
        large_modules
        and len(connector_refs) >= 2
        and len(mechanical_refs) >= 3
    )
    confidence = 0.0
    if needs_floorplan:
        confidence = min(
            0.95,
            0.72
            + min(len(large_modules), 2) * 0.05
            + min(len(connector_refs) - 2, 3) * 0.04
            + min(len(mechanical_refs) - 3, 4) * 0.03,
        )

    return {
        "needs_floorplan": needs_floorplan,
        "reason": (
            "large module plus multiple connector/mechanical subjects and no explicit floorplan"
            if needs_floorplan
            else "below large-module/mechanical-connectivity threshold"
        ),
        "confidence": round(confidence, 2),
        "large_module_refs": large_modules,
        "connector_refs": connector_refs,
        "mechanical_refs": mechanical_refs,
        "part_count": len(parts),
        "floorplan_intent": "none_or_weak",
    }


def _add_intent(
    plan: PlacementIntentPlan,
    ref: str,
    kind: str,
    priority: int,
    reason: str,
):
    plan.intents.setdefault(ref, []).append(
        PlacementIntent(ref=ref, kind=kind, priority=priority, reasons=[reason])
    )


def _natural_ref_key(ref: str) -> tuple[str, int, str]:
    match = REF_SUFFIX_RE.match(str(ref))
    if match:
        return (match.group(1), int(match.group(2)), str(ref))
    return (str(ref), -1, str(ref))


def _ref_suffix_number(ref: str) -> int | None:
    match = REF_SUFFIX_RE.match(str(ref))
    if match is None:
        return None
    return int(match.group(2))


def _add_array_intents(
    plan: PlacementIntentPlan,
    refs: list[str],
    reason: str,
    *,
    template_name: str | None = None,
) -> None:
    for ref in refs:
        _add_intent(plan, ref, "array_subject", 78, reason)
        if template_name is not None:
            _add_intent(
                plan,
                ref,
                "panel_template",
                83,
                f"corpus-derived panel template: {template_name}",
            )


def _is_eurorack_power_connector(
    text: str,
    role: PartRole | None,
    nets: list[str],
) -> bool:
    role_name = role.role if role is not None else ""
    has_eurorack_supply = {net.upper() for net in nets} & {
        "+12V",
        "-12V",
        "EURORACK_+12V",
        "EURORACK_-12V",
    }
    return (
        role_name == "connector"
        and (EURORACK_POWER_RE.search(text) or has_eurorack_supply)
        and any(GND_NET_RE.match(net) for net in nets)
    )


def _edge_for_part(text: str, role: PartRole, nets: list[str]) -> str | None:
    if role.role in {
        "panel_jack",
        "display_connector",
        "internal_connector",
        "module_socket",
    }:
        return None
    if _is_eurorack_power_connector(text, role, nets):
        return None
    if role.role == "connector" and USB_CONNECTOR_RE.search(text):
        return "bottom"
    if DEBUG_RE.search(text):
        return "right"
    if INTERNAL_HEADER_RE.search(text) or any(DISPLAY_NET_RE.search(n) for n in nets):
        return None
    if UI_RE.search(text) and role.role == "connector":
        return "right"
    if POWER_INPUT_RE.search(text) or any(
        POWER_NET_RE.match(net) and not GND_NET_RE.match(net) for net in nets
    ):
        return "bottom"
    if role.role == "connector":
        return "right"
    return None


def _is_panel_mounted_jack_text(text: str) -> bool:
    if not AUDIO_JACK_RE.search(text):
        return False
    if BARREL_RE.search(text) or "power jack" in text:
        return False
    return bool(
        is_panel_style_audio_jack_text(text)
        or (
            PANEL_MOUNT_JACK_RE.search(text)
            and not EDGE_AUDIO_JACK_RE.search(text)
        )
    )


def _looks_like_module_socket_text(ref: str, text: str) -> bool:
    match = re.match(r"[A-Za-z]+", str(ref or ""))
    prefix = match.group(0).upper() if match else ""
    if MODULE_SOCKET_RE.search(text):
        return True
    if PIN_SOCKET_RE.search(text) and (
        prefix == "U"
        or re.search(r"\b(module|daughter|carrier|mezzanine|breakout)\b", text, re.I)
    ):
        return True
    if prefix == "U" and re.search(r"\bconn_0[12]x\d+\b", text, re.I) and "socket" in text:
        return True
    return False


def _mating_intent_for_part(
    ref: str,
    text: str,
    role: PartRole | None,
    nets: list[str],
) -> MatingIntent | None:
    role_name = role.role if role is not None else ""
    module_socket_text = _looks_like_module_socket_text(ref, text)
    if role_name == "module_socket" or module_socket_text:
        return MatingIntent(
            ref=ref,
            kind="module_socket",
            edge_preference=None,
            mating_side="plug_in_module",
            allowed_rotations=(0.0, 180.0),
            confidence=0.9,
            reasons=["plug-in module/socket metadata"],
        )

    if _is_eurorack_power_connector(text, role, nets):
        return MatingIntent(
            ref=ref,
            kind="eurorack_power",
            edge_preference="bottom",
            mating_side="internal_power_cable",
            allowed_rotations=(0.0, 180.0),
            confidence=0.85,
            reasons=["Eurorack/internal power connector metadata"],
        )
    if role_name == "panel_jack":
        return MatingIntent(
            ref=ref,
            kind="panel_jack",
            edge_preference=None,
            mating_side="front_panel",
            allowed_rotations=(0.0, 180.0),
            confidence=0.9,
            reasons=["panel/audio jack metadata"],
        )
    if _is_panel_mounted_jack_text(text):
        return MatingIntent(
            ref=ref,
            kind="panel_jack",
            edge_preference=None,
            mating_side="front_panel",
            allowed_rotations=(0.0, 180.0),
            confidence=0.84,
            reasons=["panel-mounted audio jack metadata"],
        )
    if role_name == "connector" and USB_CONNECTOR_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="usb",
            edge_preference="bottom",
            mating_side="outside_board",
            allowed_rotations=(0.0, 180.0),
            confidence=0.95,
            reasons=["USB connector metadata"],
        )
    if BARREL_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="barrel",
            edge_preference="bottom",
            mating_side="outside_board",
            allowed_rotations=(0.0, 90.0, 180.0, 270.0),
            confidence=0.9,
            reasons=["barrel/power jack metadata"],
        )
    if role_name == "connector" and TERMINAL_BLOCK_RE.search(text):
        edge = _edge_for_part(text, role or PartRole(ref, "connector", 0.5), nets)
        return MatingIntent(
            ref=ref,
            kind="terminal_block",
            edge_preference=edge,
            mating_side="cable_exit",
            allowed_rotations=(0.0, 90.0, 180.0, 270.0),
            confidence=0.86,
            reasons=["terminal block wire-entry metadata"],
        )
    if role_name == "connector" and MIDI_RE.search(text):
        edge = _edge_for_part(text, role or PartRole(ref, "connector", 0.5), nets)
        return MatingIntent(
            ref=ref,
            kind="midi",
            edge_preference=edge,
            mating_side="outside_board",
            allowed_rotations=(0.0, 90.0, 180.0, 270.0),
            confidence=0.82,
            reasons=["MIDI connector metadata"],
        )
    if JST_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="jst",
            edge_preference="bottom",
            mating_side="cable_exit",
            allowed_rotations=(0.0, 180.0),
            confidence=0.85,
            reasons=["JST/battery connector metadata"],
        )
    if FFC_RE.search(text):
        is_display_fpc = any(DISPLAY_NET_RE.search(n) for n in nets)
        if is_display_fpc:
            return MatingIntent(
                ref=ref,
                kind="ffc",
                edge_preference="top",
                mating_side="cable_exit",
                allowed_rotations=(0.0, 180.0),
                confidence=0.9,
                reasons=["FFC/FPC connector with display nets"],
            )
        return MatingIntent(
            ref=ref,
            kind="ffc",
            edge_preference="bottom",
            mating_side="cable_exit",
            allowed_rotations=(0.0, 180.0),
            confidence=0.85,
            reasons=["FFC/FPC connector metadata"],
        )
    if (HEADER_RE.search(text) or role_name == "connector") and (
        INTERNAL_HEADER_RE.search(text) or any(DISPLAY_NET_RE.search(n) for n in nets)
    ):
        return MatingIntent(
            ref=ref,
            kind="internal_header",
            edge_preference=None,
            mating_side="daughterboard_or_display",
            allowed_rotations=(0.0, 90.0, 180.0, 270.0),
            confidence=0.75,
            reasons=["internal/display/daughterboard header metadata"],
        )
    if role_name == "connector" and AUDIO_JACK_RE.search(text):
        edge = _edge_for_part(text, role or PartRole(ref, "connector", 0.5), nets)
        return MatingIntent(
            ref=ref,
            kind="audio_jack",
            edge_preference=edge,
            mating_side="outside_board",
            allowed_rotations=(0.0, 90.0, 180.0, 270.0),
            confidence=0.82,
            reasons=["edge-mount audio jack metadata"],
        )
    if HEADER_RE.search(text) or role_name == "connector":
        edge = _edge_for_part(text, role or PartRole(ref, "connector", 0.5), nets)
        return MatingIntent(
            ref=ref,
            kind="header" if HEADER_RE.search(text) else "generic_connector",
            edge_preference=edge,
            mating_side="pin_access",
            confidence=0.75,
            reasons=["connector/header metadata"],
        )
    if DISPLAY_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="display",
            edge_preference="top",
            mating_side="visible_face",
            confidence=0.8,
            reasons=["display metadata"],
        )
    if POT_ENCODER_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="encoder" if "encoder" in text else "pot",
            edge_preference="right",
            mating_side="user_control",
            confidence=0.8,
            reasons=["panel control metadata"],
        )
    if NAV_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="nav_control",
            edge_preference="right",
            mating_side="user_control",
            confidence=0.8,
            reasons=["nav switch/joystick metadata"],
        )
    if KEY_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="key",
            edge_preference="right",
            mating_side="user_control",
            confidence=0.78,
            reasons=["key switch metadata"],
        )
    if BUTTON_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="button",
            edge_preference="right",
            mating_side="user_control",
            confidence=0.75,
            reasons=["button/switch metadata"],
        )
    if LED_RE.search(text):
        return MatingIntent(
            ref=ref,
            kind="led",
            edge_preference="right",
            mating_side="visible_face",
            confidence=0.7,
            reasons=["LED/indicator metadata"],
        )
    return None


def _has_eurorack_context(circuit, roles: dict[str, PartRole]) -> bool:
    for part in circuit.parts:
        ref = str(getattr(part, "ref", "") or "")
        role = roles.get(ref)
        text = _part_text(part)
        nets = pin_net_names(part)
        upper_nets = {net.upper() for net in nets}
        has_eurorack_supply = bool(
            upper_nets
            & {
                "+12V",
                "-12V",
                "EURORACK_+12V",
                "EURORACK_-12V",
            }
        )
        has_ground = any(GND_NET_RE.match(net) for net in nets)

        if "eurorack" in text or "doepfer" in text:
            return True
        if (
            role is not None
            and role.role == "connector"
            and has_eurorack_supply
            and has_ground
        ):
            return True
    return False


def _apply_eurorack_audio_policy(
    circuit,
    roles: dict[str, PartRole],
    plan: PlacementIntentPlan,
) -> dict[str, PartRole]:
    if not _has_eurorack_context(circuit, roles):
        return roles

    updated = dict(roles)
    for part in circuit.parts:
        if not is_audio_jack_part(part):
            continue
        ref = str(getattr(part, "ref", "") or "")
        role = updated.get(ref)
        if role is not None and role.role == "panel_jack":
            continue
        updated[ref] = PartRole(
            ref=ref,
            role="panel_jack",
            confidence=max(0.88, getattr(role, "confidence", 0.0) if role else 0.0),
            reasons=[
                "Eurorack context treats 3.5mm audio jacks as panel-mounted",
                "prefer Thonkiconn/PJ398 vertical jacks unless user specified edge-mount",
            ],
        )
        plan.warnings.append(
            f"{ref}: Eurorack context treats 3.5mm audio jacks as panel-mounted; "
            "prefer Thonkiconn/PJ398 vertical footprints unless the human explicitly "
            "asked for right-angle edge jacks."
        )
    return updated


def _assign_eurorack_assembly_sides(
    circuit,
    roles: dict[str, PartRole],
    plan: PlacementIntentPlan,
    assembly_policy: str,
) -> None:
    if not _has_eurorack_context(circuit, roles):
        return

    policy = normalize_assembly_policy(assembly_policy)
    plan.assembly_policy = policy
    if policy == "single_sided":
        plan.warnings.append(
            "Eurorack single-board modules commonly use front-panel controls "
            "with rear electronics. The single_sided policy avoids automatic "
            "rear-side SMD placement to keep fabrication/assembly cost down; "
            "choose run_options.assembly_policy='double_sided' only when that "
            "mechanical stack is acceptable, or model a two-board module."
        )

    mating_by_ref = {mating.ref: mating for mating in plan.mating_intents}
    for part in circuit.parts:
        ref = str(getattr(part, "ref", "") or "")
        if not ref:
            continue
        if ref in plan.assembly_sides:
            continue
        role = roles.get(ref)
        role_name = role.role if role is not None else ""
        text = _part_text(part)
        mating = mating_by_ref.get(ref)

        if role_name in {"panel_jack", "control"} or LED_RE.search(text):
            plan.assembly_sides[ref] = "front"
            _add_intent(plan, ref, "front_assembly", 86, "Eurorack panel-facing part")
        elif role_name == "mounting_hole":
            plan.assembly_sides[ref] = "mechanical"
        elif policy != "double_sided":
            plan.assembly_sides[ref] = "front"
            _add_intent(
                plan,
                ref,
                "front_assembly",
                62,
                "single-sided assembly policy avoids rear-side SMD cost",
            )
        else:
            plan.assembly_sides[ref] = "back"
            reason = "Eurorack rear-side internal/power/electronics part"
            if mating is not None and mating.kind == "eurorack_power":
                reason = "Eurorack rear-side power header"
            _add_intent(plan, ref, "back_assembly", 80, reason)


def _apply_eurorack_panel_grid_policy(
    circuit,
    roles: dict[str, PartRole],
    plan: PlacementIntentPlan,
) -> None:
    """Keep Eurorack front-panel subjects in panel grids, not board edges."""
    if not _has_eurorack_context(circuit, roles):
        return

    explicit_edge_refs = set(plan.refs_with_kind("explicit_edge_anchor"))
    mating_by_ref = {mating.ref: mating for mating in plan.mating_intents}
    panel_refs: set[str] = set()

    for part in circuit.parts:
        ref = str(getattr(part, "ref", "") or "")
        if not ref:
            continue
        role = roles.get(ref)
        role_name = role.role if role is not None else ""
        text = _part_text(part)
        nets = pin_net_names(part)

        if role_name in {"panel_jack", "control"} or LED_RE.search(text):
            panel_refs.add(ref)
            _add_intent(
                plan,
                ref,
                "panel_grid_subject",
                88,
                "Eurorack front-panel part should align to the panel grid",
            )
            mating = mating_by_ref.get(ref)
            if mating is not None:
                mating.edge_preference = None
                if role_name == "panel_jack":
                    mating.kind = "panel_jack"
                    mating.mating_side = "front_panel"
                elif mating.kind in {"button", "encoder", "pot", "nav_control"}:
                    mating.mating_side = "user_control"
                elif mating.kind == "led":
                    mating.mating_side = "visible_face"
            if (
                role_name == "panel_jack"
                and AUDIO_JACK_RE.search(text)
                and not is_panel_style_audio_jack_text(text)
            ):
                plan.warnings.append(
                    f"{ref}: Eurorack panel jack is using a non-panel footprint; "
                    "prefer a vertical Thonkiconn/PJ398/PJ301-style jack unless "
                    "the human explicitly specified edge-mounted hardware."
                )

        if _is_eurorack_power_connector(text, role, nets):
            _add_intent(
                plan,
                ref,
                "bottom_back_mechanical_context",
                84,
                "Eurorack power header belongs in bottom/rear mechanical context",
            )
            if plan.assembly_policy == "double_sided":
                _add_intent(
                    plan,
                    ref,
                    "rear_mechanical_context",
                    84,
                    "double-sided Eurorack policy allows rear-side power header",
                )

    removable_refs = panel_refs - explicit_edge_refs
    if removable_refs:
        plan.edge_anchors = [
            anchor for anchor in plan.edge_anchors if anchor.ref not in removable_refs
        ]
        plan.face_edges = [
            face for face in plan.face_edges if face.ref not in removable_refs
        ]


def _is_panel_subject(ref: str, roles: dict[str, PartRole], plan: PlacementIntentPlan) -> bool:
    role = roles.get(ref)
    if role is not None and role.role in {"panel_jack", "control"}:
        return True
    return any(
        intent.kind in {"panel_control", "panel_jack", "front_panel_subject"}
        for intent in plan.intents_for(ref)
    )


def _array_subject_kind(
    ref: str,
    roles: dict[str, PartRole],
    mating_by_ref: dict[str, MatingIntent],
    part_text_by_ref: dict[str, str] | None = None,
) -> str | None:
    role = roles.get(ref)
    text = (part_text_by_ref or {}).get(ref, "")
    if role is not None:
        if role.role == "panel_jack":
            return "jack"
        if role.role == "control":
            if KEY_RE.search(text):
                return "key"
            return "control"
        if role.role == "ic" and SENSOR_RE.search(text):
            return "sensor"

    mating = mating_by_ref.get(ref)
    if mating is None:
        return None
    if mating.kind == "led":
        return "led"
    if mating.kind == "panel_jack":
        return "jack"
    if mating.kind == "key":
        return "key"
    if mating.kind in {"button", "encoder", "pot", "nav_control"}:
        return "control"
    if SENSOR_RE.search(text):
        return "sensor"
    return None


def _arrange_array_subjects(
    plan: PlacementIntentPlan,
    roles: dict[str, PartRole],
    part_text_by_ref: dict[str, str] | None = None,
    outline=None,
) -> None:
    if outline is None:
        return
    mating_by_ref = {intent.ref: intent for intent in plan.mating_intents}
    groups: dict[str, list[str]] = {}
    for ref in sorted(plan.intents):
        kind = _array_subject_kind(ref, roles, mating_by_ref, part_text_by_ref)
        if kind is None and _is_panel_subject(ref, roles, plan):
            kind = "panel"
        if kind is None:
            continue
        groups.setdefault(kind, []).append(ref)

    refs = [ref for refs_for_kind in groups.values() for ref in refs_for_kind]
    if len(refs) < 2:
        return

    if _arrange_source_mined_panel_template(plan, groups, refs, outline):
        return

    panel_like_count = sum(
        len(groups.get(kind, [])) for kind in ("control", "jack", "panel")
    )
    tall_panel = (
        panel_like_count >= 2
        and outline.height_mm >= outline.width_mm * 1.6
        and outline.height_mm >= 60.0
    )

    if tall_panel:
        kinds = [kind for kind in ("control", "jack", "led", "panel") if groups.get(kind)]
        usable_kinds = [kind for kind in kinds if kind in {"control", "jack", "panel"}]
        if not usable_kinds:
            usable_kinds = kinds

        y_pad = max(8.0, outline.height_mm * 0.16)
        start_y = outline.y_min + y_pad
        end_y = outline.y_max - y_pad
        if start_y >= end_y:
            start_y = outline.y_min + outline.height_mm * 0.2
            end_y = outline.y_max - outline.height_mm * 0.2

        if len(usable_kinds) == 1:
            x_by_kind = {usable_kinds[0]: outline.x_min + outline.width_mm * 0.5}
        elif len(usable_kinds) == 2:
            x_by_kind = {
                usable_kinds[0]: outline.x_min + outline.width_mm * 0.40,
                usable_kinds[1]: outline.x_min + outline.width_mm * 0.60,
            }
        else:
            x_start = outline.x_min + outline.width_mm * 0.30
            x_end = outline.x_min + outline.width_mm * 0.70
            step = (x_end - x_start) / max(1, len(usable_kinds) - 1)
            x_by_kind = {
                kind: x_start + idx * step
                for idx, kind in enumerate(usable_kinds)
            }

        for kind in kinds:
            kind_refs = groups.get(kind, [])
            if not kind_refs:
                continue
            x = x_by_kind.get(kind, outline.x_min + outline.width_mm * 0.5)
            _add_array_intents(
                plan,
                kind_refs,
                "visible repeated part on tall panel",
            )
            plan.align_constraints.append(
                AlignConstraint(refs=kind_refs, axis="x", value_mm=x)
            )
            if len(kind_refs) > 1:
                plan.distribute_constraints.append(
                    DistributeConstraint(
                        refs=kind_refs,
                        axis="y",
                        start_mm=start_y,
                        end_mm=end_y,
                    )
                )
        return

    x_pad = max(4.0, outline.width_mm * 0.14)
    start_x = outline.x_min + x_pad
    end_x = outline.x_max - x_pad
    if start_x >= end_x:
        start_x = outline.x_min + outline.width_mm * 0.2
        end_x = outline.x_max - outline.width_mm * 0.2

    row_entries: list[tuple[str, list[str]]] = []
    grid_groups: list[tuple[str, list[list[str]]]] = []
    for kind in ("key", "control", "led", "jack", "sensor", "panel"):
        kind_refs = groups.get(kind, [])
        if not kind_refs:
            continue
        single_row_limit = 3 if kind in {"key", "sensor"} else 4
        rows_for_kind = grid_rows_for_refs(
            kind_refs,
            outline.width_mm,
            outline.height_mm,
            single_row_limit=single_row_limit,
        )
        grid_groups.append((kind, rows_for_kind))
        row_entries.extend((kind, row) for row in rows_for_kind)

    if len(row_entries) == 1:
        y_values = [outline.y_min + outline.height_mm * 0.42]
    else:
        y_start = outline.y_min + outline.height_mm * 0.32
        y_end = outline.y_min + outline.height_mm * 0.64
        step = (y_end - y_start) / max(1, len(row_entries) - 1)
        y_values = [y_start + idx * step for idx in range(len(row_entries))]

    for (kind, row_refs), y in zip(row_entries, y_values):
        if not row_refs:
            continue
        reason = (
            "visible repeated sensor grid"
            if kind == "sensor"
            else "visible repeated part"
        )
        _add_array_intents(plan, row_refs, reason)
        plan.align_constraints.append(
            AlignConstraint(refs=row_refs, axis="y", value_mm=y)
        )
        if len(row_refs) > 1:
            plan.distribute_constraints.append(
                DistributeConstraint(
                    refs=row_refs,
                    axis="x",
                    start_mm=start_x,
                    end_mm=end_x,
                )
            )

    for _kind, rows_for_kind in grid_groups:
        if len(rows_for_kind) < 2:
            continue
        max_cols = max(len(row) for row in rows_for_kind)
        if max_cols < 2 or any(len(row) != max_cols for row in rows_for_kind):
            continue
        for col_idx in range(max_cols):
            col_refs = [row[col_idx] for row in rows_for_kind]
            if len(col_refs) < 2:
                continue
            x = (
                start_x
                if max_cols == 1
                else start_x + (end_x - start_x) * col_idx / (max_cols - 1)
            )
            plan.align_constraints.append(
                AlignConstraint(refs=col_refs, axis="x", value_mm=x)
            )


def _arrange_source_mined_panel_template(
    plan: PlacementIntentPlan,
    groups: dict[str, list[str]],
    refs: list[str],
    outline,
) -> bool:
    jack_refs = sorted(groups.get("jack", []), key=_natural_ref_key)
    if _arrange_compact_four_jack_grid(plan, jack_refs, refs, outline):
        return True
    if _arrange_long_panel_jack_rows(plan, groups, jack_refs, refs, outline):
        return True
    return False


def _arrange_compact_four_jack_grid(
    plan: PlacementIntentPlan,
    jack_refs: list[str],
    refs: list[str],
    outline,
) -> bool:
    is_compact = (
        min(outline.width_mm, outline.height_mm) <= 45.0
        and max(outline.width_mm, outline.height_mm) <= 70.0
    )
    if len(jack_refs) != 4 or set(jack_refs) != set(refs) or not is_compact:
        return False

    left_x = outline.x_min + outline.width_mm * 0.25
    right_x = outline.x_min + outline.width_mm * 0.75
    top_y = outline.y_min + outline.height_mm * 0.34
    bottom_y = outline.y_min + outline.height_mm * 0.66
    top_refs = jack_refs[:2]
    bottom_refs = jack_refs[2:]
    left_refs = [top_refs[0], bottom_refs[0]]
    right_refs = [top_refs[1], bottom_refs[1]]

    _add_array_intents(
        plan,
        jack_refs,
        "source corpus compact 2x2 panel jack grid",
        template_name="compact_2x2_panel_jacks",
    )
    plan.warnings.append(
        "selected corpus-derived compact 2x2 panel jack template"
    )
    for row_refs, y in ((top_refs, top_y), (bottom_refs, bottom_y)):
        plan.align_constraints.append(
            AlignConstraint(refs=row_refs, axis="y", value_mm=y)
        )
        plan.distribute_constraints.append(
            DistributeConstraint(
                refs=row_refs,
                axis="x",
                start_mm=left_x,
                end_mm=right_x,
            )
        )
    for col_refs, x in ((left_refs, left_x), (right_refs, right_x)):
        plan.align_constraints.append(
            AlignConstraint(refs=col_refs, axis="x", value_mm=x)
        )
        plan.distribute_constraints.append(
            DistributeConstraint(
                refs=col_refs,
                axis="y",
                start_mm=top_y,
                end_mm=bottom_y,
            )
        )
    return True


def _arrange_long_panel_jack_rows(
    plan: PlacementIntentPlan,
    groups: dict[str, list[str]],
    jack_refs: list[str],
    refs: list[str],
    outline,
) -> bool:
    is_long_panel = (
        outline.width_mm >= 120.0
        and outline.width_mm >= outline.height_mm * 4.0
        and outline.height_mm <= 70.0
    )
    mostly_jacks = len(jack_refs) >= max(6, int(len(refs) * 0.7))
    if not is_long_panel or not mostly_jacks:
        return False

    x_pad = max(8.0, min(30.0, outline.width_mm * 0.055))
    start_x = outline.x_min + x_pad
    end_x = outline.x_max - x_pad
    top_y = outline.y_min + outline.height_mm * 0.35
    bottom_y = outline.y_min + outline.height_mm * 0.65
    split = (len(jack_refs) + 1) // 2
    jack_rows = [jack_refs[:split], jack_refs[split:]]

    _add_array_intents(
        plan,
        jack_refs,
        "source corpus long two-row panel jack grid",
        template_name="long_two_row_panel_jacks",
    )
    plan.warnings.append(
        "selected corpus-derived long two-row panel jack template"
    )
    for row_refs, y in zip(jack_rows, (top_y, bottom_y)):
        if len(row_refs) < 2:
            continue
        plan.align_constraints.append(
            AlignConstraint(refs=row_refs, axis="y", value_mm=y)
        )
        plan.distribute_constraints.append(
            DistributeConstraint(
                refs=row_refs,
                axis="x",
                start_mm=start_x,
                end_mm=end_x,
            )
        )

    other_refs = sorted(
        [ref for ref in refs if ref not in set(jack_refs)],
        key=_natural_ref_key,
    )
    if len(other_refs) >= 2:
        _add_array_intents(
            plan,
            other_refs,
            "visible repeated part on long panel",
            template_name="long_panel_secondary_row",
        )
        plan.align_constraints.append(
            AlignConstraint(
                refs=other_refs,
                axis="y",
                value_mm=outline.y_min + outline.height_mm * 0.5,
            )
        )
        plan.distribute_constraints.append(
            DistributeConstraint(
                refs=other_refs,
                axis="x",
                start_mm=start_x,
                end_mm=end_x,
            )
        )
    return True


def _add_simple_ic_passive_near_constraints(
    circuit,
    plan: PlacementIntentPlan,
    roles: dict[str, PartRole],
) -> None:
    panel_like_count = sum(
        1 for role in roles.values() if role.role in {"panel_jack", "control"}
    )
    if panel_like_count >= 2 or len(getattr(circuit, "parts", []) or []) > 16:
        return

    primary_refs = [
        ref
        for ref, role in roles.items()
        if role.role in {"ic", "regulator", "module_socket"}
    ]
    if len(primary_refs) != 1:
        return

    primary_ref = primary_refs[0]
    part_by_ref = {part.ref: part for part in circuit.parts}
    primary_nets = set(pin_net_names(part_by_ref[primary_ref]))
    if not primary_nets:
        return

    existing = {(c.ref, c.target_ref) for c in plan.near_constraints}
    for ref, role in roles.items():
        if ref == primary_ref or role.role not in {
            "decoupling_cap",
            "signal_passive",
            "crystal",
        }:
            continue
        nets = set(pin_net_names(part_by_ref.get(ref)))
        if not primary_nets.intersection(nets):
            continue
        key = (ref, primary_ref)
        if key in existing:
            continue
        distance = 5.0 if role.role == "decoupling_cap" else 8.0
        plan.near_constraints.append(
            NearConstraint(ref=ref, target_ref=primary_ref, distance_mm=distance)
        )
        existing.add(key)


def _is_coax_connector(part, role: PartRole | None = None) -> bool:
    """Return True if *part* looks like a coaxial/antenna connector."""
    text = _part_text(part)
    if not COAX_RE.search(text):
        return False

    ref = str(getattr(part, "ref", "") or "").upper()
    connector_role = role is not None and role.role in {"connector", "panel_jack"}
    connector_ref = ref.startswith(("J", "P", "CN", "CON", "ANT"))
    connector_metadata = bool(
        re.search(
            r"(connector|conn[_\s:/-]*coax|coaxial|sma|u\.?fl|ipex)",
            text,
            re.I,
        )
    )
    module_metadata = bool(
        ref.startswith("U")
        or re.search(r"\b(rf[_\s:/-]*module|module|transceiver|receiver|esp32)\b", text, re.I)
    )
    if module_metadata and not (connector_role or connector_ref or connector_metadata):
        return False
    return connector_role or connector_ref or connector_metadata


def _find_rf_ic(antenna_part, circuit):
    """Follow the signal net from an antenna connector to find the RF IC.

    The signal pin is typically named ``In``, ``Signal``, or ``1``.  We walk
    the net looking for a non-passive, non-connector IC (ref starting with
    ``U``).
    """
    signal_pin = None
    for pin in antenna_part.pins:
        pin_name = getattr(pin, "name", None) or ""
        if pin_name in ("In", "Signal", "1"):
            signal_pin = pin
            break
    if signal_pin is None and antenna_part.pins:
        # Fallback: use first pin that is not on GND
        for pin in antenna_part.pins:
            net = getattr(pin, "net", None)
            net_name = getattr(net, "name", "") if net else ""
            if not GND_NET_RE.match(net_name):
                signal_pin = pin
                break
    if signal_pin is None:
        return None

    net = getattr(signal_pin, "net", None)
    if net is None:
        return None

    # Walk all pins on this net to find an IC
    net_pins = getattr(net, "_pins", None) or getattr(net, "pins", [])
    if callable(net_pins):
        net_pins = net_pins()
    for pin in net_pins:
        part = getattr(pin, "part", None)
        if part is None or part is antenna_part:
            continue
        ref = str(getattr(part, "ref", ""))
        if ref.startswith("U"):
            return part
    return None


def _find_crystal_for_ic(ic_part, circuit):
    """Find a crystal connected to the IC's XTAL/OSC pins."""
    for pin in ic_part.pins:
        pin_name = getattr(pin, "name", None) or ""
        if not XTAL_PIN_RE.search(pin_name):
            continue
        net = getattr(pin, "net", None)
        if net is None:
            continue
        net_pins = getattr(net, "_pins", None) or getattr(net, "pins", [])
        if callable(net_pins):
            net_pins = net_pins()
        for other_pin in net_pins:
            other_part = getattr(other_pin, "part", None)
            if other_part is None or other_part is ic_part:
                continue
            other_text = _part_text(other_part)
            other_ref = str(getattr(other_part, "ref", ""))
            if (
                "crystal" in other_text
                or "resonator" in other_text
                or other_ref.startswith(("Y", "X"))
            ):
                return other_part
    return None


def _find_audio_ics(circuit):
    """Return parts that look like DAC, codec, or audio amplifier ICs."""
    audio_parts = []
    for part in circuit.parts:
        text = _part_text(part)
        if AUDIO_IC_RE.search(text):
            audio_parts.append(part)
    return audio_parts


def _infer_rf_intents(
    circuit,
    plan: PlacementIntentPlan,
    outline,
    roles: dict[str, PartRole],
) -> None:
    """Detect antenna connectors and emit RF path constraints."""
    explicit_refs = set(plan.refs_with_kind("explicit_edge_anchor"))
    for part in circuit.parts:
        ref = str(getattr(part, "ref", ""))
        if not _is_coax_connector(part, roles.get(ref)):
            continue

        existing_anchor = next((a for a in plan.edge_anchors if a.ref == ref), None)
        edge = existing_anchor.edge if ref in explicit_refs and existing_anchor else "top"
        offset = existing_anchor.offset_mm if existing_anchor is not None else None
        if offset is None and outline is not None and ref not in explicit_refs:
            offset = (outline.x_min + outline.x_max) / 2

        if ref not in explicit_refs:
            # EdgeAnchor for the antenna connector — replace any generic
            # connector anchor that was already emitted for this ref.
            plan.edge_anchors = [a for a in plan.edge_anchors if a.ref != ref]
            plan.edge_anchors.append(
                EdgeAnchor(ref=ref, edge=edge, offset_mm=offset)
            )
            plan.face_edges = [f for f in plan.face_edges if f.ref != ref]
            plan.face_edges.append(FaceEdgeConstraint(ref=ref, edge=edge))

        # Mating intent for the coaxial connector
        plan.mating_intents.append(
            MatingIntent(
                ref=ref,
                kind="coaxial",
                edge_preference=edge,
                mating_side="outside_board",
                allowed_rotations=(0.0, 90.0, 180.0, 270.0),
                confidence=0.9,
                reasons=["coaxial/antenna connector metadata"],
            )
        )
        _add_intent(plan, ref, "edge_connector", 90, "antenna/coaxial connector")
        _add_intent(plan, ref, "rf_module", 85, "antenna connector")
        _add_intent(plan, ref, "mechanical_mating", 88, "coaxial mating intent")

        # Find the RF IC
        rf_ic = _find_rf_ic(part, circuit)
        if rf_ic is None:
            continue
        rf_ic_ref = str(getattr(rf_ic, "ref", ""))

        # NearConstraint: RF IC close to antenna
        plan.near_constraints.append(
            NearConstraint(ref=rf_ic_ref, target_ref=ref, distance_mm=8.0)
        )
        _add_intent(
            plan, rf_ic_ref, "rf_module", 85, f"RF IC near antenna {ref}"
        )

        # Find crystal on the RF IC
        crystal = _find_crystal_for_ic(rf_ic, circuit)
        if crystal is not None:
            xtal_ref = str(getattr(crystal, "ref", ""))
            plan.near_constraints.append(
                NearConstraint(
                    ref=xtal_ref, target_ref=rf_ic_ref, distance_mm=4.0
                )
            )
            _add_intent(
                plan,
                xtal_ref,
                "crystal_network",
                80,
                f"crystal near RF IC {rf_ic_ref}",
            )

        # Analog separation: audio ICs far from RF IC
        audio_ics = _find_audio_ics(circuit)
        for audio_part in audio_ics:
            audio_ref = str(getattr(audio_part, "ref", ""))
            if audio_ref == rf_ic_ref:
                continue
            plan.far_constraints.append(
                FarConstraint(
                    ref=audio_ref, target_ref=rf_ic_ref, distance_mm=15.0
                )
            )
            _add_intent(
                plan,
                audio_ref,
                "analog_separation",
                75,
                f"audio IC separated from RF IC {rf_ic_ref}",
            )


def _infer_crystal_intents(
    circuit,
    plan: PlacementIntentPlan,
    roles: dict[str, PartRole],
) -> None:
    """Keep crystals and resonators close to IC clock pins."""

    existing = {(c.ref, c.target_ref) for c in plan.near_constraints}
    for part in circuit.parts:
        ref = str(getattr(part, "ref", "") or "")
        role = roles.get(ref)
        if role is None or role.role != "ic":
            continue
        crystal = _find_crystal_for_ic(part, circuit)
        if crystal is None:
            continue
        xtal_ref = str(getattr(crystal, "ref", "") or "")
        if not xtal_ref or xtal_ref == ref:
            continue
        key = (xtal_ref, ref)
        if key not in existing:
            plan.near_constraints.append(
                NearConstraint(ref=xtal_ref, target_ref=ref, distance_mm=4.0)
            )
            existing.add(key)
        _add_intent(
            plan,
            xtal_ref,
            "crystal_network",
            86,
            f"crystal near clock pins on {ref}",
        )


def _slot_for_channel(
    channel_number: int,
    slot_index: int,
    refs: list[str],
    roles: dict[str, PartRole],
) -> ChannelSlot:
    sensor_refs: list[str] = []
    passive_refs: list[str] = []
    connector_refs: list[str] = []
    other_refs: list[str] = []

    for ref in refs:
        role = roles.get(ref)
        role_name = role.role if role is not None else "unknown"
        if role_name == "connector":
            connector_refs.append(ref)
        elif role_name in {"signal_passive", "decoupling_cap"}:
            passive_refs.append(ref)
        elif role_name == "ic":
            sensor_refs.append(ref)
        else:
            other_refs.append(ref)

    return ChannelSlot(
        channel_number=channel_number,
        slot_index=slot_index,
        refs=refs,
        sensor_refs=sensor_refs,
        passive_refs=passive_refs,
        connector_refs=connector_refs,
        other_refs=other_refs,
    )


def _infer_repeated_channels(
    circuit,
    roles: dict[str, PartRole],
) -> list[RepeatedChannelIntent]:
    channel_refs: dict[int, set[str]] = {}
    for part in circuit.parts:
        for net_name in pin_net_names(part):
            match = CHANNEL_RE.search(net_name)
            if match is None:
                continue
            channel_refs.setdefault(int(match.group(1)), set()).add(part.ref)

    if len(channel_refs) < 2:
        return []

    initial_ref_counts: dict[str, int] = {}
    for refs_for_ch in channel_refs.values():
        for ref in refs_for_ch:
            initial_ref_counts[ref] = initial_ref_counts.get(ref, 0) + 1
    decaps_by_number = {
        number: ref
        for ref, role in roles.items()
        if role.role == "decoupling_cap"
        for number in [_ref_suffix_number(ref)]
        if number is not None
    }
    channel_ref_set = {ref for refs_for_ch in channel_refs.values() for ref in refs_for_ch}
    for refs_for_ch in channel_refs.values():
        sensor_numbers = {
            number
            for ref in refs_for_ch
            if roles.get(ref) is not None
            and roles[ref].role == "ic"
            and initial_ref_counts.get(ref, 0) == 1
            for number in [_ref_suffix_number(ref)]
            if number is not None
        }
        for number in sensor_numbers:
            decap_ref = decaps_by_number.get(number)
            if decap_ref is not None and decap_ref not in channel_ref_set:
                refs_for_ch.add(decap_ref)
                channel_ref_set.add(decap_ref)

    refs = sorted({ref for refs_for_ch in channel_refs.values() for ref in refs_for_ch})
    ref_counts: dict[str, int] = {}
    for refs_for_ch in channel_refs.values():
        for ref in refs_for_ch:
            ref_counts[ref] = ref_counts.get(ref, 0) + 1
    shared_refs = sorted(ref for ref, count in ref_counts.items() if count > 1)
    controller_refs = sorted(
        ref
        for ref in shared_refs
        if roles.get(ref) is not None and roles[ref].role in {"ic", "connector"}
    )
    slots = []
    refs_by_channel = {
        channel: sorted(refs_for_ch)
        for channel, refs_for_ch in sorted(channel_refs.items())
    }
    for slot_index, channel in enumerate(sorted(refs_by_channel)):
        slot_refs = [
            ref
            for ref in refs_by_channel[channel]
            if ref_counts.get(ref, 0) == 1
        ]
        slots.append(_slot_for_channel(channel, slot_index, slot_refs, roles))

    return [
        RepeatedChannelIntent(
            name="channel",
            refs=refs,
            channel_numbers=sorted(channel_refs),
            refs_by_channel=refs_by_channel,
            pattern="channel-numbered net names",
            shared_refs=shared_refs,
            controller_refs=controller_refs,
            slots=slots,
        )
    ]


def _add_repeated_channel_near_constraints(
    plan: PlacementIntentPlan,
    roles: dict[str, PartRole],
) -> None:
    """Keep per-channel support parts local to their channel subject."""
    existing = {(c.ref, c.target_ref) for c in plan.near_constraints}
    for channel in plan.repeated_channels:
        for slot in channel.slots:
            if not slot.sensor_refs:
                continue
            target_ref = slot.sensor_refs[0]
            for ref in slot.passive_refs:
                role = roles.get(ref)
                role_name = role.role if role is not None else "unknown"
                distance_mm = 5.0 if role_name == "decoupling_cap" else 8.0
                key = (ref, target_ref)
                if key in existing:
                    continue
                plan.near_constraints.append(
                    NearConstraint(
                        ref=ref,
                        target_ref=target_ref,
                        distance_mm=distance_mm,
                    )
                )
                existing.add(key)


def _colocate_display_and_controls(
    plan: PlacementIntentPlan,
    outline=None,
) -> None:
    """Ensure display and user-control parts share the same board edge.

    Finds display-related mating intents (kind "display" or "ffc" with display
    in the reasons) and control-related intents (button, encoder, pot,
    nav_control).  When both exist, the display edge is authoritative: all
    controls are moved to the same edge.  An ``AlignConstraint`` is emitted so
    the placer keeps them on the same line along that edge.
    """
    _DISPLAY_KINDS = {"display"}
    _CONTROL_KINDS = {"button", "encoder", "pot", "nav_control"}

    display_refs: list[str] = []
    control_refs: list[str] = []

    for mi in plan.mating_intents:
        if mi.kind in _DISPLAY_KINDS:
            display_refs.append(mi.ref)
        elif mi.kind == "ffc" and any(
            "display" in r.lower() for r in mi.reasons
        ):
            display_refs.append(mi.ref)
        elif mi.kind in _CONTROL_KINDS:
            control_refs.append(mi.ref)

    if not display_refs or not control_refs:
        return

    # Use the first display's edge as the authority.
    display_edge = next(
        (
            mi.edge_preference
            for mi in plan.mating_intents
            if mi.ref == display_refs[0]
        ),
        "top",
    )

    # Move controls to the same edge.
    for ref in control_refs:
        for mi in plan.mating_intents:
            if mi.ref == ref:
                mi.edge_preference = display_edge
        for ea in plan.edge_anchors:
            if ea.ref == ref:
                ea.edge = display_edge
        for fe in plan.face_edges:
            if fe.ref == ref:
                fe.edge = display_edge

    # Emit AlignConstraint for display + controls on the shared edge.
    axis = "y" if display_edge in {"top", "bottom"} else "x"
    plan.align_constraints.append(
        AlignConstraint(
            refs=display_refs + control_refs,
            axis=axis,
        )
    )


def _place_opposing_header_pair(plan: PlacementIntentPlan) -> None:
    """Put two generic pin-access headers on opposing board edges."""
    pin_headers = [
        intent
        for intent in plan.mating_intents
        if intent.kind in {"header", "generic_connector"}
        and intent.mating_side == "pin_access"
        and intent.edge_preference is not None
    ]
    if len(pin_headers) != 2:
        return

    refs = sorted(intent.ref for intent in pin_headers)
    if any(
        "explicit_edge_anchor" in {i.kind for i in plan.intents_for(ref)}
        for ref in refs
    ):
        return
    existing = {
        anchor.ref: anchor
        for anchor in plan.edge_anchors
        if anchor.ref in refs
    }
    if set(existing) != set(refs):
        return

    for ref, edge in zip(refs, ("left", "right")):
        existing[ref].edge = edge
        existing[ref].offset_mm = None
        for face_edge in plan.face_edges:
            if face_edge.ref == ref:
                face_edge.edge = edge
        for mating in plan.mating_intents:
            if mating.ref == ref:
                mating.edge_preference = edge
    plan.align_constraints.append(AlignConstraint(refs=refs, axis="y"))


def _inline_direction_for_text(text: str) -> str | None:
    has_input = INLINE_INPUT_RE.search(text) is not None
    has_output = INLINE_OUTPUT_RE.search(text) is not None
    if has_input == has_output:
        return None
    return "input" if has_input else "output"


def _place_opposing_inline_connector_pair(
    plan: PlacementIntentPlan,
    part_by_ref: dict[str, object],
    outline=None,
) -> None:
    """Split paired directional edge connectors to opposing left/right edges."""
    explicit_refs = set(plan.refs_with_kind("explicit_edge_anchor"))
    mating_by_ref = {mating.ref: mating for mating in plan.mating_intents}
    anchors_by_ref = {anchor.ref: anchor for anchor in plan.edge_anchors}
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    eligible_kinds = {"audio_jack", "generic_connector", "midi", "usb"}

    for ref, part in part_by_ref.items():
        if ref in explicit_refs or ref not in anchors_by_ref:
            continue
        mating = mating_by_ref.get(ref)
        if mating is None or mating.kind not in eligible_kinds:
            continue
        direction = _inline_direction_for_text(_part_text(part))
        if direction is None:
            continue
        footprint = str(
            getattr(part, "footprint", None)
            or getattr(part, "foot", "")
            or ""
        )
        groups.setdefault((mating.kind, footprint), []).append((direction, ref))

    for (_kind, _footprint), entries in groups.items():
        if len(entries) != 2:
            continue
        directions = {direction for direction, _ref in entries}
        if directions != {"input", "output"}:
            continue

        refs_by_direction = {direction: ref for direction, ref in entries}
        offset = None
        if outline is not None:
            offset = (outline.y_min + outline.y_max) / 2
        for direction, edge in (("input", "left"), ("output", "right")):
            ref = refs_by_direction[direction]
            anchor = anchors_by_ref.get(ref)
            mating = mating_by_ref.get(ref)
            if anchor is None:
                continue
            _set_edge_anchor(
                plan,
                anchor,
                edge=edge,
                offset=offset,
                part=part_by_ref.get(ref),
                mating=mating,
            )
            _add_intent(
                plan,
                ref,
                "opposing_inline_connector_pair",
                87,
                "paired inline input/output connector",
            )

        refs = [refs_by_direction["input"], refs_by_direction["output"]]
        if not any(set(constraint.refs) == set(refs) for constraint in plan.align_constraints):
            plan.align_constraints.append(AlignConstraint(refs=refs, axis="y"))


def _spread_edge_anchor_offsets(plan: PlacementIntentPlan, outline=None) -> None:
    """Assign stable, spaced offsets to inferred edge anchors.

    Edge anchors express "this part should mate with this board edge", not
    "every part should sit at the edge midpoint".  When several connectors
    share an edge, midpoint placement creates avoidable overlaps and pushes
    adjacent-edge connectors into the same corner.  Spread inferred anchors
    along the available edge while keeping single anchors centered.
    """
    if outline is None or not plan.edge_anchors:
        return

    anchors_by_edge: dict[str, list[EdgeAnchor]] = {}
    for anchor in plan.edge_anchors:
        anchors_by_edge.setdefault(anchor.edge.lower(), []).append(anchor)

    for edge, anchors in anchors_by_edge.items():
        if edge in {"top", "bottom"}:
            start = outline.x_min
            length = outline.width_mm
        elif edge in {"left", "right"}:
            start = outline.y_min
            length = outline.height_mm
        else:
            continue
        if length <= 0:
            continue

        pinned_offset_refs = set(plan.refs_with_kind("explicit_edge_anchor"))
        pinned_offset_refs.update(plan.refs_with_kind("connector_between_mounting_holes"))
        movable = [
            anchor
            for anchor in anchors
            if not (anchor.ref in pinned_offset_refs and anchor.offset_mm is not None)
        ]
        movable.sort(key=lambda anchor: anchor.ref)
        if not movable:
            continue
        if len(anchors) == 1:
            if movable[0].offset_mm is None:
                movable[0].offset_mm = start + length / 2
            continue

        pad = min(max(length * 0.12, 5.0), length * 0.30)
        usable = max(0.0, length - 2 * pad)
        step = usable / max(1, len(movable) - 1)
        for idx, anchor in enumerate(movable):
            anchor.offset_mm = start + pad + step * idx


def _place_mounting_holes(
    plan: PlacementIntentPlan,
    refs: list[str],
    outline=None,
) -> None:
    if outline is None or not refs:
        return

    rounded_corner_radius = getattr(outline, "corner_radius_mm", 0.0) or 0.0
    if rounded_corner_radius > 0:
        x_inset = min(float(rounded_corner_radius), outline.width_mm / 2)
        y_inset = min(float(rounded_corner_radius), outline.height_mm / 2)
    else:
        base_inset = min(3.5, max(2.0, min(outline.width_mm, outline.height_mm) * 0.08))
        edge_set = {anchor.edge.lower() for anchor in plan.edge_anchors}
        x_inset = base_inset + (2.5 if edge_set & {"left", "right"} else 0.0)
        needs_bottom_row = len(refs) > 2
        y_edge_conflict = "top" in edge_set or (needs_bottom_row and "bottom" in edge_set)
        y_inset = base_inset + (2.5 if y_edge_conflict else 0.0)
        x_inset = min(x_inset, max(base_inset, outline.width_mm * 0.32))
        y_inset = min(y_inset, max(base_inset, outline.height_mm * 0.32))
    x0 = outline.x_min + x_inset
    x1 = outline.x_max - x_inset
    y0 = outline.y_min + y_inset
    y1 = outline.y_max - y_inset
    if len(refs) == 2:
        # With only two holes, prefer one mechanical side instead of an
        # awkward diagonal pair.  Four-hole patterns still use all corners.
        positions = [(x0, y0), (x1, y0)]
    else:
        positions = [
            (x0, y0),
            (x1, y1),
            (x1, y0),
            (x0, y1),
        ]
    for ref, (x, y) in zip(refs[:4], positions):
        plan.fixed_positions.append(FixedPosition(ref=ref, x_mm=x, y_mm=y))


def _edge_opposite(edge: str) -> str:
    return {
        "top": "bottom",
        "bottom": "top",
        "left": "right",
        "right": "left",
    }.get(edge, edge)


def _set_edge_anchor(
    plan: PlacementIntentPlan,
    anchor: EdgeAnchor,
    *,
    edge: str,
    offset: float,
    part,
    mating: MatingIntent | None,
) -> None:
    text = _part_text(part) if part is not None else ""
    anchor.edge = edge
    anchor.offset_mm = offset
    anchor.inset_mm = _default_edge_inset_for_part(
        text,
        mating.kind if mating else None,
        part=part,
    )
    anchor.rot_deg = _default_edge_rotation_for_part(
        part,
        text,
        mating.kind if mating else None,
        edge,
    )

    for face_edge in plan.face_edges:
        if face_edge.ref == anchor.ref:
            face_edge.edge = edge
            face_edge.rot_deg = anchor.rot_deg
    if mating is not None:
        mating.edge_preference = edge


def _center_breakout_connectors_with_two_mounting_holes(
    plan: PlacementIntentPlan,
    mounting_refs: list[str],
    part_by_ref: dict[str, object],
    outline=None,
) -> None:
    if outline is None or len(mounting_refs) != 2:
        return

    explicit_refs = set(plan.refs_with_kind("explicit_edge_anchor"))
    mating_by_ref = {mating.ref: mating for mating in plan.mating_intents}
    eligible_kinds = {"header", "generic_connector", "jst"}
    eligible = [
        anchor
        for anchor in plan.edge_anchors
        if anchor.ref not in explicit_refs
        and (mating := mating_by_ref.get(anchor.ref)) is not None
        and mating.kind in eligible_kinds
    ]
    if not eligible:
        return

    holes = {
        fixed.ref: fixed
        for fixed in plan.fixed_positions
        if fixed.ref in set(mounting_refs)
    }
    if len(holes) != 2:
        return

    hole_positions = [holes[ref] for ref in mounting_refs]
    h0, h1 = hole_positions
    center_x = (outline.x_min + outline.x_max) / 2
    center_y = (outline.y_min + outline.y_max) / 2
    edge = None
    offset = None
    if abs(h0.y_mm - h1.y_mm) <= 2.0:
        edge = "top" if (h0.y_mm + h1.y_mm) / 2 <= center_y else "bottom"
        offset = (h0.x_mm + h1.x_mm) / 2
        if abs(offset - center_x) <= max(1.0, outline.width_mm * 0.08):
            offset = center_x
    elif abs(h0.x_mm - h1.x_mm) <= 2.0:
        edge = "left" if (h0.x_mm + h1.x_mm) / 2 <= center_x else "right"
        offset = (h0.y_mm + h1.y_mm) / 2
        if abs(offset - center_y) <= max(1.0, outline.height_mm * 0.08):
            offset = center_y
    if edge is None or offset is None:
        return

    if len(eligible) == 1:
        anchor = eligible[0]
        mating = mating_by_ref.get(anchor.ref)
        _set_edge_anchor(
            plan,
            anchor,
            edge=edge,
            offset=offset,
            part=part_by_ref.get(anchor.ref),
            mating=mating,
        )
        _add_intent(
            plan,
            anchor.ref,
            "connector_between_mounting_holes",
            86,
            "single main connector centered between two mounting holes",
        )
        return

    headers = [
        anchor
        for anchor in eligible
        if (mating_by_ref.get(anchor.ref) is not None)
        and mating_by_ref[anchor.ref].kind in {"header", "generic_connector"}
    ]
    cable_connectors = [
        anchor
        for anchor in eligible
        if (mating_by_ref.get(anchor.ref) is not None)
        and mating_by_ref[anchor.ref].kind in {"jst"}
    ]

    if len(headers) == 1:
        header = headers[0]
        _set_edge_anchor(
            plan,
            header,
            edge=edge,
            offset=offset,
            part=part_by_ref.get(header.ref),
            mating=mating_by_ref.get(header.ref),
        )
        _add_intent(
            plan,
            header.ref,
            "connector_between_mounting_holes",
            86,
            "breakout header centered between two mounting holes",
        )

    if cable_connectors:
        cable_edge = _edge_opposite(edge)
        count = len(cable_connectors)
        if cable_edge in {"top", "bottom"}:
            span = outline.width_mm
            start = outline.x_min + span * 0.5
        else:
            span = outline.height_mm
            start = outline.y_min + span * 0.5
        if count == 1:
            offsets = [start]
        else:
            spread = min(span * 0.28, max(4.0, span / max(2, count + 1)))
            first = start - spread * (count - 1) / 2
            offsets = [first + spread * idx for idx in range(count)]
        for anchor, cable_offset in zip(
            sorted(cable_connectors, key=lambda item: item.ref),
            offsets,
        ):
            _set_edge_anchor(
                plan,
                anchor,
                edge=cable_edge,
                offset=cable_offset,
                part=part_by_ref.get(anchor.ref),
                mating=mating_by_ref.get(anchor.ref),
            )
            _add_intent(
                plan,
                anchor.ref,
                "connector_opposite_mounting_hole_header",
                82,
                "cable connector centered on edge opposite two-hole breakout header",
            )


def infer_placement_intents(
    circuit,
    outline=None,
    backend_status: OptionalBackendStatus | None = None,
    assembly_policy: str | None = None,
) -> PlacementIntentPlan:
    """Infer first-draft placement intent from schematic roles and net names."""
    plan = PlacementIntentPlan(
        assembly_policy=normalize_assembly_policy(assembly_policy),
        backend_status=backend_status or optional_backend_status()
    )
    roles = classify_parts(circuit)
    roles = _apply_eurorack_audio_policy(circuit, roles, plan)
    part_by_ref = {str(getattr(part, "ref", "") or ""): part for part in circuit.parts}
    mounting_refs: list[str] = []

    for part in circuit.parts:
        ref = str(getattr(part, "ref", "") or "")
        role = roles.get(ref)
        text = _part_text(part)
        nets = pin_net_names(part)
        explicit_side = _explicit_part_assembly_side(part)
        explicit_edge = _explicit_part_edge_anchor(part)
        if explicit_side is not None:
            side = explicit_side
            if side == "back" and plan.assembly_policy != "double_sided":
                plan.warnings.append(
                    f"{ref}: explicit back-side assembly request overrides "
                    "single_sided policy. Automatic placement remains front-only "
                    "unless a part declares assembly_side='back'."
                )
            plan.assembly_sides[ref] = side
            if side == "front":
                _add_intent(plan, ref, "front_assembly", 92, "explicit part.assembly_side")
            elif side == "back":
                _add_intent(plan, ref, "back_assembly", 92, "explicit part.assembly_side")
        mating_intent = _mating_intent_for_part(ref, text, role, nets)
        if mating_intent is not None:
            plan.mating_intents.append(mating_intent)
            _add_intent(
                plan,
                ref,
                "mechanical_mating",
                88,
                f"{mating_intent.kind} mating intent",
                )
            if mating_intent.edge_preference is not None:
                rot = _default_edge_rotation_for_part(
                    part,
                    text,
                    mating_intent.kind,
                    mating_intent.edge_preference,
                )
                plan.face_edges.append(
                    FaceEdgeConstraint(
                        ref=ref,
                        edge=mating_intent.edge_preference,
                        rot_deg=rot,
                    )
                )

        panel_mounted_jack = _is_panel_mounted_jack_text(text)
        module_socket_text = _looks_like_module_socket_text(ref, text)

        if (
            role is not None
            and role.role == "connector"
            and not panel_mounted_jack
            and not module_socket_text
        ):
            edge = _edge_for_part(text, role, nets)
            _add_intent(plan, ref, "edge_connector", 90, "connector-like part")
            if edge is not None:
                rot = _default_edge_rotation_for_part(
                    part,
                    text,
                    mating_intent.kind if mating_intent is not None else None,
                    edge,
                )
                inset = _default_edge_inset_for_part(
                    text,
                    mating_intent.kind if mating_intent is not None else None,
                    part=part,
                )
                offset = None
                if outline is not None and edge in {"top", "bottom"}:
                    offset = (outline.x_min + outline.x_max) / 2
                elif outline is not None:
                    offset = (outline.y_min + outline.y_max) / 2
                plan.edge_anchors.append(
                    EdgeAnchor(
                        ref=ref,
                        edge=edge,
                        offset_mm=offset,
                        inset_mm=inset,
                        rot_deg=rot,
                    )
                )

        if explicit_edge is not None:
            edge, offset, rot = explicit_edge
            if rot is None:
                rot = _default_edge_rotation_for_part(
                    part,
                    text,
                    mating_intent.kind if mating_intent is not None else None,
                    edge,
                )
            inset = _default_edge_inset_for_part(
                text,
                mating_intent.kind if mating_intent is not None else None,
                part=part,
            )
            plan.edge_anchors = [
                anchor for anchor in plan.edge_anchors if anchor.ref != ref
            ]
            plan.face_edges = [
                face_edge for face_edge in plan.face_edges if face_edge.ref != ref
            ]
            plan.edge_anchors.append(
                EdgeAnchor(
                    ref=ref,
                    edge=edge,
                    offset_mm=offset,
                    inset_mm=inset,
                    rot_deg=rot,
                )
            )
            plan.face_edges.append(FaceEdgeConstraint(ref=ref, edge=edge, rot_deg=rot))
            for mating in plan.mating_intents:
                if mating.ref == ref:
                    mating.edge_preference = edge
            _add_intent(
                plan,
                ref,
                "explicit_edge_anchor",
                94,
                f"explicit part edge preference: {edge}",
            )

        if UI_RE.search(text):
            _add_intent(plan, ref, "board_ui", 75, "UI-like metadata")

        if role is not None and role.role == "ic" and SENSOR_RE.search(text):
            _add_intent(plan, ref, "sensor_grid_subject", 76, "sensor-like metadata")

        if role is not None and role.role in {"regulator", "inductor", "diode"}:
            _add_intent(plan, ref, "power_cluster", 85, f"{role.role} role")

        if role is not None and role.role == "decoupling_cap":
            _add_intent(plan, ref, "decoupling", 80, "decoupling capacitor")

        if role is not None and role.role == "mounting_hole":
            _add_intent(plan, ref, "mounting_hole", 82, "mechanical mounting hole")
            mounting_refs.append(ref)

        if (
            MUX_RE.search(text)
            or sum(1 for net in nets if CHANNEL_RE.search(net)) >= 4
        ):
            _add_intent(plan, ref, "mux_bank_controller", 80, "channelized mux nets")

        if RF_RE.search(text):
            _add_intent(plan, ref, "rf_module", 85, "RF/antenna-like metadata")

        if role is not None and role.role == "crystal":
            _add_intent(plan, ref, "crystal_network", 80, "timing source")

        if DEBUG_RE.search(text):
            _add_intent(plan, ref, "test_debug", 80, "debug connector metadata")

        role_name = role.role if role is not None else "unknown"
        power_input_like = role_name in {"connector", "module_socket"} or (
            role_name not in {"ic", "regulator"}
            and POWER_INPUT_RE.search(text)
        )
        if any(POWER_NET_RE.match(net) for net in nets) and power_input_like:
            _add_intent(plan, ref, "power_input", 85, "connector on supply net")

        if (
            role is not None and role.role == "panel_jack"
        ) or (
            mating_intent is not None and mating_intent.kind == "panel_jack"
        ):
            _add_intent(plan, ref, "panel_jack", 86, "panel/audio jack")
            _add_intent(plan, ref, "front_panel_subject", 84, "panel jack")

        if role is not None and role.role == "control":
            _add_intent(plan, ref, "panel_control", 84, "front-panel control")
            _add_intent(plan, ref, "front_panel_subject", 82, "panel control")

        if role is not None and role.role == "module_socket":
            _add_intent(plan, ref, "module_socket", 86, "plug-in module/socket")
            _add_intent(plan, ref, "internal_connector", 82, "module socket")

        if mating_intent is not None and mating_intent.kind == "internal_header":
            _add_intent(
                plan,
                ref,
                "internal_connector",
                80,
                "display/daughterboard/internal header",
            )

    plan.repeated_channels = _infer_repeated_channels(circuit, roles)
    _add_repeated_channel_near_constraints(plan, roles)
    _infer_rf_intents(circuit, plan, outline, roles)
    _infer_crystal_intents(circuit, plan, roles)
    _add_simple_ic_passive_near_constraints(circuit, plan, roles)
    _colocate_display_and_controls(plan, outline)
    _place_opposing_header_pair(plan)
    _place_opposing_inline_connector_pair(plan, part_by_ref, outline)
    _place_mounting_holes(plan, mounting_refs, outline)
    _center_breakout_connectors_with_two_mounting_holes(
        plan,
        mounting_refs,
        part_by_ref,
        outline,
    )
    _assign_eurorack_assembly_sides(circuit, roles, plan, plan.assembly_policy)
    _apply_eurorack_panel_grid_policy(circuit, roles, plan)
    part_text_by_ref = {
        str(getattr(part, "ref", "") or ""): _part_text(part)
        for part in circuit.parts
    }
    _arrange_array_subjects(
        plan,
        roles,
        part_text_by_ref=part_text_by_ref,
        outline=outline,
    )
    _spread_edge_anchor_offsets(plan, outline)
    return plan
