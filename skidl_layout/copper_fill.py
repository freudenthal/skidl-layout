"""Measure the copper a board actually *has*: KiCad's own fill, plus routed area.

Two questions this module answers, neither of which anything in the stack could
answer before power-layout Phase 5:

1. **How much poured copper is in a zone?**  KRT writes ``(fill yes)`` with
   **zero** ``(filled_polygon ...)`` blocks -- KiCad fills at open time -- so
   every zone-area reader in this tree has reported ``0`` for every zone on
   every board to date. :func:`fill_board` shells out to KiCad's *own*
   ``pcbnew.ZONE_FILLER``, saves the filled board, and reads the polygons back.
   That is the first real number for "how much copper is in the VIN region".
2. **How much routed copper is on a net?**  :func:`read_routed_copper` sums
   ``length x width`` over a net's ``(segment ...)`` blocks -- the quantity the
   ``SW_NODE_COPPER_AREA`` advisory (see :mod:`skidl_layout.layout_quality`) is
   calibrated on, and the honest replacement for the placement-time switch-node
   *span* proxy that Phase 2 measured backwards.

**Request-only**, exactly like :func:`skidl_layout.krt.evaluate_routability`:
nothing here is called from ``plan_layout`` / ``evaluate_circuit`` /
``generate()``, and importing this module costs nothing (KiCad is discovered
lazily, on the first :func:`fill_board`).

**Degrades, never raises.**  :func:`fill_board` returns ``None`` when no KiCad
python is discoverable, when ``pcbnew`` will not import, or when the refill
fails -- callers must treat the measurement as optional.

The refill recipe (stage the board *with its sibling* ``.kicad_pro``, run a
straight-line module-scope pcbnew script, parse each ``(filled_polygon ...)`` as
one connected island) is KRT's, from ``kicad_exact_fill.py``.  It is
re-implemented here rather than imported because KRT's own
``find_kicad_python()`` hardcodes the *unversioned*
``C:\\Program Files\\KiCad\\bin\\python.exe``, which does not exist on a
versioned Windows install -- so KRT's helper silently degrades to ``None`` on
exactly the machine this was built for.  Patching KRT for a path list is rung 4
of the escalation ladder for a one-line problem; discovering the interpreter
here is rung 1.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

__all__ = [
    "FilledBoard",
    "NetCopper",
    "fill_board",
    "find_kicad_python",
    "read_routed_copper",
]


#: Environment escape hatch: point this at a python that can ``import pcbnew``
#: and discovery stops there.  Exists so a machine with an unusual KiCad layout
#: never needs a code change.
KICAD_PYTHON_ENV = "SKIDL_LAYOUT_KICAD_PYTHON"

#: Seconds allowed for the one-liner ``import pcbnew`` probe.
_PROBE_TIMEOUT_S = 60

#: Default seconds allowed for a refill.  KRT uses the same figure.
FILL_TIMEOUT_S = 300

_REFILL_SCRIPT = """\
import sys
import pcbnew
src = sys.argv[1]
dst = sys.argv[2]
board = pcbnew.LoadBoard(src)
filler = pcbnew.ZONE_FILLER(board)
zones = board.Zones()
filler.Fill(zones)
pcbnew.SaveBoard(dst, board)
print("REFILL_OK", len(list(zones)))
"""

# Resolved-interpreter memo: {} unset, {"path": str|None} once probed. A dict
# rather than a bare global so ``clear_cache()`` and the tests can reset it.
_PYTHON_CACHE: dict = {}


# --------------------------------------------------------------------------
# Interpreter discovery
# --------------------------------------------------------------------------


def _windows_candidates() -> list[str]:
    """KiCad pythons on Windows, newest version first.

    KiCad installs to ``C:\\Program Files\\KiCad\\<major>.<minor>\\bin`` from
    KiCad 6 on; the *unversioned* ``…\\KiCad\\bin\\python.exe`` that KRT looks
    for belongs to much older layouts.  Both are offered, versioned first, so a
    machine with several KiCads gets the newest.
    """
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            roots.append(os.path.join(base, "KiCad"))
    roots.append(r"C:\Program Files\KiCad")

    versioned: list[tuple[tuple, str]] = []
    plain: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        plain.append(os.path.join(root, "bin", "python.exe"))
        for path in glob.glob(os.path.join(root, "*", "bin", "python.exe")):
            version = os.path.basename(os.path.dirname(os.path.dirname(path)))
            versioned.append((_version_key(version), path))
    # Highest version wins; ties broken by path so discovery is deterministic.
    versioned.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _key, path in versioned] + plain


def _version_key(text: str) -> tuple:
    """``"10.0"`` -> ``(10, 0)``; anything unparsable sorts lowest."""
    parts: list[int] = []
    for chunk in str(text).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (-1,)


def _candidates() -> list[str]:
    """Every plausible pcbnew-bearing interpreter, best guess first."""
    found: list[str] = []
    override = os.environ.get(KICAD_PYTHON_ENV)
    if override:
        found.append(override)
    if sys.platform.startswith("win"):
        found.extend(_windows_candidates())
    elif sys.platform == "darwin":
        found.extend(sorted(glob.glob(
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
            "Python.framework/Versions/*/bin/python3"), reverse=True))
        found.append(
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
            "Python.framework/Versions/Current/bin/python3")
    else:
        # Linux distro packages put pcbnew on the system interpreter.
        for name in ("python3", "python"):
            which = shutil.which(name)
            if which:
                found.append(which)
        found.append("/usr/bin/python3")
    # This interpreter last: a venv that happens to see pcbnew is a legitimate
    # answer, but never the preferred one (it is usually the wrong ABI).
    found.append(sys.executable)

    ordered: list[str] = []
    for path in found:
        if path and path not in ordered:
            ordered.append(path)
    return ordered


def _imports_pcbnew(python: str) -> bool:
    try:
        proc = subprocess.run(
            [python, "-c", "import pcbnew"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def find_kicad_python(verify: bool = True) -> str | None:
    """Path of a python that can ``import pcbnew``, or ``None``.

    Mirrors :func:`skidl_layout.validator.find_kicad_cli`'s shape -- an
    environment/``which`` lookup plus known-location fallbacks -- with one
    addition: existing on disk is not enough, because a KiCad install can ship
    a python without the scripting module.  With ``verify=True`` (the default)
    each candidate is probed by actually importing ``pcbnew``, and the answer is
    memoised for the process.  Set ``SKIDL_LAYOUT_KICAD_PYTHON`` to skip the
    search entirely.

    Windows candidates are the **versioned** install dirs
    (``…\\KiCad\\10.0\\bin\\python.exe``), newest first, then the unversioned
    legacy path.  No version is hardcoded.
    """
    if not verify:
        for path in _candidates():
            if os.path.isfile(path):
                return path
        return None

    if "path" in _PYTHON_CACHE:
        return _PYTHON_CACHE["path"]
    resolved = None
    for path in _candidates():
        if not os.path.isfile(path):
            continue
        if _imports_pcbnew(path):
            resolved = path
            break
    _PYTHON_CACHE["path"] = resolved
    return resolved


def clear_cache() -> None:
    """Forget the memoised interpreter (tests; a KiCad install mid-session)."""
    _PYTHON_CACHE.clear()


# --------------------------------------------------------------------------
# The filled board
# --------------------------------------------------------------------------


@dataclass
class FilledBoard:
    """Real poured-copper geometry, per ``(net, layer)``.

    ``islands`` holds KiCad's fracture output: each entry of the list is one
    *connected* island polygon, so ``island_count > 1`` on a net means its pour
    fragmented -- the failure mode Phase 4 measured when a promoted supply was
    excluded from routing.
    """

    path: str
    islands: dict = field(default_factory=dict)       # (net, layer) -> [poly]
    area_mm2: dict = field(default_factory=dict)      # (net, layer) -> mm2
    island_count: dict = field(default_factory=dict)  # (net, layer) -> int
    #: Set when a polygon's shoelace came out negative before ``abs()`` -- a
    #: reversed winding.  Harmless for area, recorded rather than assumed away.
    reversed_windings: int = 0

    @property
    def total_area_mm2(self) -> float:
        return round(sum(self.area_mm2.values()), 3)

    def area_by_net(self) -> dict:
        """``{net: mm2}`` summed across layers."""
        out: dict[str, float] = {}
        for (net, _layer), area in self.area_mm2.items():
            out[net] = round(out.get(net, 0.0) + area, 3)
        return out

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "zones": [
                {
                    "net": net,
                    "layer": layer,
                    "area_mm2": self.area_mm2[(net, layer)],
                    "island_count": self.island_count[(net, layer)],
                }
                for (net, layer) in sorted(self.area_mm2)
            ],
            "area_by_net": self.area_by_net(),
            "total_area_mm2": self.total_area_mm2,
            "reversed_windings": self.reversed_windings,
        }

    def summary(self) -> str:
        lines = [f"Filled copper ({os.path.basename(self.path)}):"]
        for (net, layer) in sorted(self.area_mm2):
            lines.append(
                f"  {net} on {layer}: {self.area_mm2[(net, layer)]:.3f} mm2 "
                f"in {self.island_count[(net, layer)]} island(s)"
            )
        lines.append(f"  total {self.total_area_mm2:.3f} mm2")
        return "\n".join(lines)


def fill_board(
    pcb_path: str,
    out_path: str | None = None,
    project_from: str | None = None,
    timeout_s: int = FILL_TIMEOUT_S,
    verbose: bool = False,
) -> FilledBoard | None:
    """Run KiCad's ``ZONE_FILLER`` over ``pcb_path`` and measure the result.

    Returns a :class:`FilledBoard`, or ``None`` when no ``pcbnew``-bearing
    python is discoverable or the refill fails.  **Never raises** on an
    unavailable KiCad -- an unreadable ``pcb_path`` still raises, since that is
    a caller bug rather than a missing tool.

    The board is staged into a temp dir **with its sibling ``.kicad_pro``**
    before filling.  This matters: a bare board refills at *stock* netclasses
    and shrinks tight pours, which KRT's docstring calls the phantom-divergence
    trap.  ``project_from`` names a board whose sibling project to borrow when
    ``pcb_path`` has none of its own.

    ``out_path`` keeps the filled board (a real ``.kicad_pcb`` with
    ``(filled_polygon ...)`` blocks KiCad wrote itself); by default it is
    discarded and only the measurement survives.
    """
    if not os.path.isfile(pcb_path):
        raise FileNotFoundError(pcb_path)
    python = find_kicad_python()
    if python is None:
        if verbose:
            print("  (copper_fill: no python that imports pcbnew was found)")
        return None

    tmpdir = tempfile.mkdtemp(prefix="skidl_layout_fill_")
    try:
        stem = os.path.splitext(os.path.basename(pcb_path))[0]
        staged = os.path.join(tmpdir, stem + ".kicad_pcb")
        shutil.copyfile(pcb_path, staged)
        sibling_pro = os.path.splitext(pcb_path)[0] + ".kicad_pro"
        if not os.path.isfile(sibling_pro) and project_from:
            sibling_pro = os.path.splitext(project_from)[0] + ".kicad_pro"
        if os.path.isfile(sibling_pro):
            shutil.copyfile(sibling_pro, os.path.join(tmpdir, stem + ".kicad_pro"))
        script = os.path.join(tmpdir, "refill.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(_REFILL_SCRIPT)
        filled = os.path.join(tmpdir, stem + "_filled.kicad_pcb")
        proc = subprocess.run(
            [python, script, staged, filled],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if "REFILL_OK" not in (proc.stdout or "") or not os.path.isfile(filled):
            if verbose:
                print(f"  (copper_fill: refill failed rc={proc.returncode} "
                      f"{(proc.stderr or '').strip()[-200:]})")
            return None
        with open(filled, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if out_path:
            directory = os.path.dirname(os.path.abspath(out_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            shutil.copyfile(filled, out_path)
            if os.path.isfile(sibling_pro):
                shutil.copyfile(
                    sibling_pro, os.path.splitext(out_path)[0] + ".kicad_pro")
    except (OSError, subprocess.SubprocessError) as exc:
        if verbose:
            print(f"  (copper_fill: unavailable: {exc})")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return parse_filled_board(text, path=out_path or pcb_path)


def parse_filled_board(pcb_text: str, path: str = "") -> FilledBoard:
    """Measure ``(filled_polygon ...)`` geometry out of saved board text.

    Split out from :func:`fill_board` so the parsing is testable without KiCad.
    Works on any board text; a board KRT wrote (no filled polygons) simply
    measures as empty, which is the finding Phase 4 recorded.
    """
    from simp_sexp import Sexp

    from .reader import _find_child, _find_children

    board = Sexp(pcb_text)
    net_names = _net_id_to_name(board)

    result = FilledBoard(path=path)
    for zone in board.search("zone"):
        net = _zone_net_name(zone, net_names, _find_child)
        for poly in _find_children(zone, "filled_polygon"):
            layer_node = _find_child(poly, "layer")
            if layer_node is None or len(layer_node) < 2:
                continue
            layer = str(layer_node[1]).strip('"')
            points = _poly_points(poly, _find_child, _find_children)
            if len(points) < 3:
                continue
            signed = _shoelace(points)
            if signed < 0:
                result.reversed_windings += 1
            key = (net, layer)
            result.islands.setdefault(key, []).append(points)
            result.area_mm2[key] = round(
                result.area_mm2.get(key, 0.0) + abs(signed), 3)
            result.island_count[key] = result.island_count.get(key, 0) + 1
    return result


def _zone_net_name(zone, net_names: dict, find_child) -> str:
    """A zone's net, however this KiCad version chose to spell it.

    Three spellings are live at once and all three occur on boards this stack
    produces: KRT writes ``(net <id>)`` + ``(net_name "GND")``; KiCad 10 re-saves
    the same zone as ``(net "GND")`` with no ``net_name`` at all; and an id with
    neither resolves through the board's net table.
    """
    named = find_child(zone, "net_name")
    if named is not None and len(named) > 1:
        return str(named[1]).strip('"')
    net = find_child(zone, "net")
    if net is not None and len(net) > 1:
        token = str(net[1]).strip('"')
        try:
            return net_names.get(int(token), token)
        except ValueError:
            return token
    return ""


def _poly_points(poly, find_child, find_children) -> list:
    pts = find_child(poly, "pts")
    if pts is None:
        return []
    out = []
    for xy in find_children(pts, "xy"):
        if len(xy) >= 3:
            out.append((float(xy[1]), float(xy[2])))
    return out


def _shoelace(points) -> float:
    total = 0.0
    count = len(points)
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _net_id_to_name(board) -> dict:
    """``{id: name}`` from the board's top-level ``(net ID "name")`` table."""
    out: dict[int, str] = {}
    for node in board:
        if isinstance(node, list) and len(node) >= 3 and node[0] == "net":
            try:
                out[int(node[1])] = str(node[2]).strip('"')
            except (TypeError, ValueError):
                continue
    return out


# --------------------------------------------------------------------------
# Routed copper (no KiCad needed)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NetCopper:
    """Track copper drawn for one net.  ``copper_area_mm2`` is the headline.

    ``sum(length x width)`` over the net's segments -- the radiating-surface
    proxy a switching supply's datasheet is really constraining when it says
    "minimise switch-node copper".  Vias are excluded: this measures trace
    copper, matching :func:`skidl_layout.krt._segment_widths_by_net`.
    """

    net: str
    max_width_mm: float
    segments: int
    length_mm: float
    copper_area_mm2: float

    def to_dict(self) -> dict:
        return {
            "net": self.net,
            "max_width_mm": self.max_width_mm,
            "segments": self.segments,
            "length_mm": self.length_mm,
            "copper_area_mm2": self.copper_area_mm2,
        }


def read_routed_copper(pcb_path: str, nets=None) -> dict:
    """``{net_name: NetCopper}`` for every net with routed track copper.

    Reads the board directly (``simp_sexp`` + :mod:`skidl_layout.reader`
    helpers, the pattern :func:`skidl_layout.fab_check` established) -- no KiCad
    and no KRT subprocess.  ``nets`` restricts the result to the named nets.

    A net with **no** segments is absent from the result rather than present
    with a zero: a poured plane and an unrouted net are different things, and
    only the caller knows which it is looking at.
    """
    import math

    from simp_sexp import Sexp

    from .reader import _find_child

    with open(pcb_path, "r", encoding="utf-8", errors="replace") as handle:
        board = Sexp(handle.read())

    net_names = _net_id_to_name(board)
    wanted = set(nets) if nets else None

    acc: dict[str, dict] = {}
    for seg in board.search("segment"):
        start = _find_child(seg, "start")
        end = _find_child(seg, "end")
        width_node = _find_child(seg, "width")
        net_node = _find_child(seg, "net")
        if start is None or end is None or width_node is None or net_node is None:
            continue
        try:
            net = net_names.get(int(str(net_node[1]).strip('"')))
        except (TypeError, ValueError):
            net = str(net_node[1]).strip('"')
        if net is None or (wanted is not None and net not in wanted):
            continue
        width = float(width_node[1])
        length = math.hypot(float(end[1]) - float(start[1]),
                            float(end[2]) - float(start[2]))
        row = acc.setdefault(
            net, {"max": 0.0, "segments": 0, "length": 0.0, "area": 0.0})
        row["max"] = max(row["max"], width)
        row["segments"] += 1
        row["length"] += length
        row["area"] += length * width

    return {
        net: NetCopper(
            net=net,
            max_width_mm=round(row["max"], 3),
            segments=row["segments"],
            length_mm=round(row["length"], 3),
            copper_area_mm2=round(row["area"], 3),
        )
        for net, row in acc.items()
    }
