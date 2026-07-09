"""Test bootstrap for skidl-layout.

Several tests instantiate real SKiDL parts (e.g. ``Part("Device", "R", ...)``),
which requires SKiDL to find the KiCad symbol libraries. When none of the
``KICAD*_SYMBOL_DIR`` environment variables are set, SKiDL cannot resolve the
"Device" library and those tests fail with ``FileNotFoundError: Can't open
file: Device`` — an environment problem, not a layout-engine defect.

To keep the suite green out-of-the-box on a machine with KiCad installed, this
conftest best-effort discovers a symbols directory and exports the env vars
*before* SKiDL is imported. If nothing is found, the env is left untouched and
the part-instantiating tests are skipped (they need the libraries to run).
"""

from __future__ import annotations

import os
from pathlib import Path

# Env var names SKiDL checks (newest KiCad first).
_SYMBOL_ENV_VARS = (
    "KICAD_SYMBOL_DIR",
    "KICAD9_SYMBOL_DIR",
    "KICAD8_SYMBOL_DIR",
    "KICAD7_SYMBOL_DIR",
    "KICAD6_SYMBOL_DIR",
)

# Common install roots to probe for a symbols/ dir containing Device.kicad_sym.
_CANDIDATE_ROOTS = (
    r"C:\Program Files\KiCad",
    r"C:\Program Files (x86)\KiCad",
    "/usr/share/kicad",
    "/usr/local/share/kicad",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
)


def _discover_symbol_dir() -> str | None:
    for root in _CANDIDATE_ROOTS:
        base = Path(root)
        if not base.exists():
            continue
        # Match both flat (share/kicad/symbols) and versioned (10.0/share/...) layouts.
        for hit in base.rglob("Device.kicad_sym"):
            return str(hit.parent)
    return None


def _ensure_symbol_env() -> None:
    if any(os.environ.get(v) for v in _SYMBOL_ENV_VARS):
        return  # already configured — respect the user's environment
    sym_dir = _discover_symbol_dir()
    if sym_dir:
        for var in _SYMBOL_ENV_VARS:
            os.environ.setdefault(var, sym_dir)


# Run at import time, before any test module imports skidl.
_ensure_symbol_env()
