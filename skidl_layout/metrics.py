"""Quantitative layout-quality metrics and scoring for skidl-layout.

Adapted from lachlanfysh/skidl's `benchmarks/evaluate_layout.py` and
`benchmarks/score.py` (same provenance as the layout engine — see AUTHORS.md).
Two changes vs. the originals:

* It drives the package's high-level :func:`skidl_layout.plan_layout` entry
  (which already computes total HPWL on the returned ``LayoutScore``) instead of
  the lower-level ``extract_groups``/``place_parts``/``validate`` calls and a
  ``validation.net_hpwl`` attribute that this snapshot of the engine does not
  expose.
* Hardcoded ``/usr/share/kicad`` paths are replaced by best-effort discovery so
  it runs cross-platform.

Usage:
    # in-process, from a skidl Circuit
    from skidl_layout.metrics import evaluate_circuit
    m = evaluate_circuit(circuit)
    print(m.layout_ok, m.overlaps, m.hpwl_total_mm, m.layout_score)

    # CLI, over a directory containing a generated circuit.py
    python -m skidl_layout.metrics <circuit_dir>
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# KiCad library discovery (cross-platform; mirrors tests/conftest.py)
# ---------------------------------------------------------------------------

_CANDIDATE_ROOTS = (
    r"C:\Program Files\KiCad",
    r"C:\Program Files (x86)\KiCad",
    "/usr/share/kicad",
    "/usr/local/share/kicad",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
)

_SYMBOL_ENV_VARS = (
    "KICAD_SYMBOL_DIR",
    "KICAD9_SYMBOL_DIR",
    "KICAD8_SYMBOL_DIR",
)


def _discover(leaf: str) -> Path | None:
    for root in _CANDIDATE_ROOTS:
        base = Path(root)
        if not base.exists():
            continue
        for hit in base.rglob(leaf):
            return hit
    return None


def discover_symbol_dir() -> str | None:
    hit = _discover("Device.kicad_sym")
    return str(hit.parent) if hit else None


def discover_footprint_dir() -> str | None:
    """Return the footprint-library *root* (parent of the *.pretty dirs)."""
    hit = _discover("R_0805_2012Metric.kicad_mod")
    return str(hit.parent.parent) if hit else None


def _ensure_symbol_libs() -> None:
    """Best-effort: make sure skidl can find the KiCad symbol libraries.

    skidl fixes its library search paths when it is imported (from the
    ``KICAD*_SYMBOL_DIR`` env vars), and importing this package already imports
    skidl — so setting the env here would be too late. Instead we inject the
    discovered symbols dir straight into skidl's live ``lib_search_paths`` (and
    setdefault the env vars for any later re-read). No-op if the environment is
    already configured. Needed by :func:`evaluate_circuit_dir`, which executes a
    ``circuit.py`` that instantiates real ``Part``s.
    """
    if not any(os.environ.get(v) for v in _SYMBOL_ENV_VARS):
        sym_dir = discover_symbol_dir()
        if not sym_dir:
            return
        for var in _SYMBOL_ENV_VARS:
            os.environ.setdefault(var, sym_dir)
        try:
            import skidl

            paths = getattr(skidl, "lib_search_paths", None)
            if isinstance(paths, dict):
                for plist in paths.values():
                    if isinstance(plist, list) and sym_dir not in plist:
                        plist.append(sym_dir)
        except Exception:
            pass


from .engine import plan_layout  # noqa: E402
from .writer import write_kicad_pcb  # noqa: E402


# ---------------------------------------------------------------------------
# Metrics + layout-quality score
# ---------------------------------------------------------------------------


@dataclass
class LayoutMetrics:
    """Placement-health metrics for one board, plus a 0-100 quality score."""

    layout_ok: bool = False
    overlaps: int = 0
    outline_violations: int = 0
    missing_refs: int = 0
    hpwl_total_mm: float = 0.0
    part_count_placed: int = 0
    pcb_written: bool = False
    errors: list = field(default_factory=list)

    @property
    def layout_score(self) -> float:
        """0-100 layout-quality score (lachlan's rubric from score.py).

        A clean, in-outline, non-overlapping placement scores 100; an invalid
        placement scores 0.
        """
        if not self.layout_ok:
            return 0.0
        score = 50.0
        if self.overlaps == 0:
            score += 25.0
        elif self.overlaps < 5:
            score += 10.0
        if self.outline_violations == 0:
            score += 15.0
        if self.missing_refs == 0:
            score += 10.0
        return score

    def to_dict(self) -> dict:
        d = asdict(self)
        d["layout_score"] = self.layout_score
        return d


def metrics_from_result(result, circuit=None) -> LayoutMetrics:
    """Build :class:`LayoutMetrics` from an existing ``LayoutResult``.

    The 0-100 grade and the health counts are a pure function of a planned
    result, so callers who already hold a ``plan_layout`` result can grade it
    without paying for a second placement pass. ``circuit`` is unused today and
    kept only for signature symmetry with :func:`evaluate_circuit`.
    """
    metrics = LayoutMetrics()
    validation = result.validation
    metrics.layout_ok = validation.ok
    metrics.overlaps = len(validation.overlaps)
    metrics.outline_violations = len(validation.outline_violations)
    metrics.missing_refs = len(validation.missing_refs)
    metrics.hpwl_total_mm = float(getattr(result.score, "total_hpwl_mm", 0.0) or 0.0)
    metrics.part_count_placed = len(result.placed_parts)
    return metrics


def evaluate_circuit(
    circuit,
    *,
    fp_lib_dirs: list[str] | None = None,
    outline=None,
    write_pcb_path: str | None = None,
    result=None,
    progress=None,
    **plan_kwargs,
) -> LayoutMetrics:
    """Plan a layout for ``circuit`` and return quantitative health metrics.

    ``circuit`` is any skidl ``Circuit`` (duck-typed: ``parts`` + ``get_nets``).
    If ``write_pcb_path`` is given, also emit a ``.kicad_pcb`` (footprint
    libraries are auto-discovered when ``fp_lib_dirs`` is omitted).

    Pass ``result=`` a precomputed ``LayoutResult`` to skip the (expensive)
    ``plan_layout`` call and grade that result directly — the metrics are a pure
    function of the result, so this is exact and avoids a second placement pass.
    ``progress`` is forwarded to ``plan_layout`` (stage-boundary callback).
    """
    if result is None:
        try:
            result = plan_layout(
                circuit, fp_lib_dirs=fp_lib_dirs, outline=outline,
                progress=progress, **plan_kwargs
            )
        except Exception:
            metrics = LayoutMetrics()
            metrics.errors.append(f"plan_layout failed: {traceback.format_exc()}")
            return metrics

    metrics = metrics_from_result(result, circuit)

    if write_pcb_path:
        fp_dirs = fp_lib_dirs
        if not fp_dirs:
            root = discover_footprint_dir()
            fp_dirs = [root] if root else []
        try:
            write_kicad_pcb(
                result.placed_parts,
                circuit,
                fp_dirs,
                write_pcb_path,
                outline=result.outline,
            )
            metrics.pcb_written = True
        except Exception as e:
            metrics.errors.append(f"PCB write failed: {e}")

    return metrics


def evaluate_circuit_dir(
    circuit_dir: str,
    *,
    fp_lib_dirs: list[str] | None = None,
    write_pcb: bool = True,
) -> LayoutMetrics:
    """Evaluate a generated ``<circuit_dir>/circuit.py`` end-to-end.

    Executes the circuit script against skidl's default circuit (the shape the
    benchmark corpus uses), then scores the resulting layout. Point skidl at the
    KiCad symbol libraries first if the environment has not already.
    """
    circuit_py = os.path.join(circuit_dir, "circuit.py")
    if not os.path.isfile(circuit_py):
        return LayoutMetrics(errors=["circuit.py not found"])

    _ensure_symbol_libs()
    try:
        import builtins

        import skidl  # noqa: F401  (populates builtins.default_circuit)

        builtins.default_circuit.reset()
        exec(open(circuit_py).read(), {"__name__": "__skidl_layout_metrics__"})
        ckt = builtins.default_circuit
    except Exception:
        return LayoutMetrics(errors=[f"circuit exec failed: {traceback.format_exc()}"])

    pcb_path = os.path.join(circuit_dir, "board.kicad_pcb") if write_pcb else None
    return evaluate_circuit(ckt, fp_lib_dirs=fp_lib_dirs, write_pcb_path=pcb_path)


# ---------------------------------------------------------------------------
# Batch aggregation (adapted from score.py)
# ---------------------------------------------------------------------------


def summary_table(rows: list[tuple[str, LayoutMetrics]]) -> str:
    """Markdown table for a batch of (board_name, LayoutMetrics)."""
    lines = [
        "| Board | OK | Score | Overlaps | Outline | Missing | HPWL(mm) | Parts |",
        "|-------|----|-------|----------|---------|---------|----------|-------|",
    ]
    for name, m in sorted(rows, key=lambda r: -r[1].layout_score):
        lines.append(
            f"| {name[:30]:<30} | {'Y' if m.layout_ok else 'N'} | "
            f"{m.layout_score:5.0f} | {m.overlaps:8d} | "
            f"{m.outline_violations:7d} | {m.missing_refs:7d} | "
            f"{m.hpwl_total_mm:8.1f} | {m.part_count_placed:5d} |"
        )
    if rows:
        n = len(rows)
        avg = sum(m.layout_score for _, m in rows) / n
        lines.append("")
        lines.append(f"**Boards: {n} · Avg layout score: {avg:.0f}/100**")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python -m skidl_layout.metrics <circuit_dir>")
        return 1
    circuit_dir = argv[1]
    metrics = evaluate_circuit_dir(circuit_dir)
    print(json.dumps(metrics.to_dict(), indent=2))
    out = os.path.join(circuit_dir, "layout_score.json")
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(metrics.to_dict(), f, indent=2)
        print(f"Saved to {out}")
    except OSError as e:
        print(f"(could not write {out}: {e})")
    return 0 if metrics.layout_ok else 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
