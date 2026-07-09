"""End-to-end smoke test for skidl-layout.

Builds a small *real* SKiDL Circuit (the loop-boundary object our fork's
schematic path produces), plans a board placement with plan_layout(), and writes
a .kicad_pcb via write_kicad_pcb(). This exercises the whole boundary:

    skidl.Circuit  ->  plan_layout()  ->  LayoutResult  ->  write_kicad_pcb()

Run:
    python smoke/smoke_end_to_end.py [output.kicad_pcb]

Requires KiCad symbol + footprint libraries on disk (auto-discovered below).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _discover(*leaf_names, roots=(r"C:\Program Files\KiCad", "/usr/share/kicad")):
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        for leaf in leaf_names:
            for hit in base.rglob(leaf):
                return hit
    return None


# --- point SKiDL at the KiCad symbol libraries before importing skidl --------
_sym = _discover("Device.kicad_sym")
if _sym is not None:
    for var in ("KICAD_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR"):
        os.environ.setdefault(var, str(_sym.parent))

# --- locate the footprint library root ---------------------------------------
_fp_hit = _discover("R_0805_2012Metric.kicad_mod")
_FP_ROOT = str(_fp_hit.parent.parent) if _fp_hit is not None else None

from skidl import Circuit, Net, Part  # noqa: E402
from skidl_layout import plan_layout, write_kicad_pcb  # noqa: E402


def build_circuit() -> Circuit:
    """A tiny RC voltage divider + bypass cap — real KiCad symbols/footprints."""
    ckt = Circuit()
    with ckt:
        vin, vout, gnd = Net("VIN"), Net("VOUT"), Net("GND")
        r1 = Part("Device", "R", value="10k",
                  footprint="Resistor_SMD:R_0805_2012Metric")
        r2 = Part("Device", "R", value="10k",
                  footprint="Resistor_SMD:R_0805_2012Metric")
        c1 = Part("Device", "C", value="100nF",
                  footprint="Capacitor_SMD:C_0805_2012Metric")
        r1[1] += vin
        r1[2] += vout
        r2[1] += vout
        r2[2] += gnd
        c1[1] += vout
        c1[2] += gnd
    return ckt


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "smoke_divider.kicad_pcb"

    ckt = build_circuit()
    print(f"[1] built Circuit: {len(ckt.parts)} parts, "
          f"{len(ckt.get_nets())} nets")

    result = plan_layout(ckt)
    print(f"[2] plan_layout -> {len(result.placed_parts)} placed parts; "
          f"score.ok={result.score.ok} validation.ok={result.validation.ok}")
    for pp in result.placed_parts:
        print(f"      {pp.ref:<4} @ ({pp.x_mm:.2f}, {pp.y_mm:.2f}) "
              f"rot={pp.rot_deg} fp={pp.footprint}")

    if _FP_ROOT is None:
        print("[3] SKIP write_kicad_pcb: KiCad footprint libraries not found")
        return 0

    write_kicad_pcb(
        result.placed_parts,
        ckt,
        fp_lib_dirs=[_FP_ROOT],
        output_path=out_path,
        outline=result.outline,
    )
    size = os.path.getsize(out_path)
    text = Path(out_path).read_text(encoding="utf-8", errors="replace")
    n_fp = text.count("(footprint ")
    print(f"[3] wrote {out_path} ({size} bytes, {n_fp} footprints)")

    ok = (len(result.placed_parts) == 3 and n_fp == 3
          and text.startswith("(kicad_pcb"))
    print(f"[4] RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
