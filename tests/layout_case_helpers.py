"""Mock circuit primitives for layout benchmark tests.

These mock _Part/_Net/_Pin/_Circuit without importing SKiDL core,
so tests run fast and don't need KiCad libraries installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class _Net:
    def __init__(self, name: str):
        self.name = name
        self._pins: list[_Pin] = []

    def get_pins(self):
        return self._pins

    def __repr__(self):
        return f"_Net({self.name!r})"


class _Pin:
    def __init__(self, part: _Part, net: _Net):
        self.part = part
        self.net = net
        net._pins.append(self)


class _Part:
    def __init__(
        self,
        ref: str,
        value: str = "",
        footprint: str = "",
        name: str = "",
        nets: list[_Net] | None = None,
        pins: int = 2,
        description: str = "",
    ):
        self.ref = ref
        self.value = value
        self.footprint = footprint
        self.name = name
        self.node = None
        self.description = description
        self.pins: list[_Pin] = []
        for net in nets or []:
            self.pins.append(_Pin(self, net))
        while len(self.pins) < pins:
            self.pins.append(_Pin(self, _Net(f"{ref}_N{len(self.pins)}")))

    def __len__(self):
        return len(self.pins)

    def __repr__(self):
        return f"_Part({self.ref!r})"


class _Circuit:
    def __init__(self, parts: list[_Part], nets: list[_Net]):
        self.parts = parts
        self.nets = nets

    def get_nets(self):
        return self.nets


# ---------------------------------------------------------------------------
# Common footprint bounding boxes (mm)
# ---------------------------------------------------------------------------
COMMON_BBOXES: dict[str, tuple[float, float]] = {
    # Passives
    "Resistor_SMD:R_0402_1005Metric": (1.0, 0.5),
    "Resistor_SMD:R_0603_1608Metric": (1.7, 0.9),
    "Resistor_SMD:R_0805_2012Metric": (2.0, 1.25),
    "Capacitor_SMD:C_0402_1005Metric": (1.0, 0.5),
    "Capacitor_SMD:C_0603_1608Metric": (1.7, 0.9),
    "Capacitor_SMD:C_0805_2012Metric": (2.0, 1.25),
    "Capacitor_SMD:C_1206_3216Metric": (3.2, 1.6),
    "Inductor_SMD:L_0805_2012Metric": (2.0, 1.25),
    # ICs
    "Package_QFP:LQFP-48_7x7mm_P0.5mm": (9.0, 9.0),
    "Package_QFP:TQFP-32_7x7mm_P0.8mm": (9.0, 9.0),
    "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm": (6.0, 5.0),
    "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm": (6.0, 12.0),
    "Package_SO:MSOP-8_3x3mm_P0.65mm": (5.0, 3.3),
    "Package_DFN:DFN-8_3x2mm_P0.5mm": (3.0, 2.0),
    "Package_TO_SOT:SOT-23-5": (3.0, 1.75),
    "Package_TO_SOT:SOT-23": (3.0, 1.4),
    # Connectors
    "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12": (9.0, 7.5),
    "Connector_USB:USB_Micro-B_Molex": (8.0, 5.5),
    "Connector_PinHeader:PinHeader_2x05_P2.54mm": (12.7, 5.08),
    "Connector_PinHeader:PinHeader_1x04_P2.54mm": (10.16, 2.54),
    "Connector_PinHeader:PinHeader_1x06_P2.54mm": (15.24, 2.54),
    "Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm": (6.0, 4.5),
    # Crystals
    "Crystal:Crystal_SMD_3215-2Pin_3.2x1.5mm": (3.2, 1.5),
    # Buttons / UI
    "Button_Switch_SMD:SW_SPST_TL3342": (4.5, 3.5),
    "LED_SMD:LED_0805_2012Metric": (2.0, 1.25),
    "LED_SMD:LED_0603_1608Metric": (1.7, 0.9),
    # Larger passives / power
    "Capacitor_SMD:C_Elec_6.3x7.7mm": (6.3, 7.7),
    "Capacitor_SMD:C_Elec_8x10.2mm": (8.0, 10.2),
    # Display
    "Display_OLED:SSD1306_0.96in": (27.0, 19.5),
    # Potentiometer
    "Potentiometer_SMD:POT_Bourns_3362P": (7.0, 7.5),
    # RF
    "RF_Module:ESP32-WROOM-32E": (18.0, 25.5),
    "Antenna:ANT_2.4G_Chip": (3.2, 1.6),
}


# ---------------------------------------------------------------------------
# Board builder helpers
# ---------------------------------------------------------------------------


def make_power_nets() -> tuple[_Net, _Net]:
    """Return (VCC, GND) nets."""
    return _Net("VCC"), _Net("GND")


def make_decap(
    ref: str, vcc: _Net, gnd: _Net, footprint: str = "Capacitor_SMD:C_0603_1608Metric"
) -> _Part:
    return _Part(ref, value="100nF", footprint=footprint, nets=[vcc, gnd])


def make_ic(
    ref: str,
    name: str,
    footprint: str,
    signal_nets: list[_Net] | None = None,
    power_nets: list[_Net] | None = None,
    pins: int = 8,
) -> _Part:
    all_nets = list(power_nets or []) + list(signal_nets or [])
    return _Part(ref, name=name, footprint=footprint, nets=all_nets, pins=pins)


def make_connector(
    ref: str,
    name: str,
    footprint: str,
    nets: list[_Net] | None = None,
    pins: int = 4,
    description: str = "",
) -> _Part:
    return _Part(
        ref, name=name, footprint=footprint, nets=nets, pins=pins, description=description
    )


def make_passive(
    ref: str,
    value: str,
    footprint: str,
    nets: list[_Net] | None = None,
) -> _Part:
    return _Part(ref, value=value, footprint=footprint, nets=nets, pins=2)
