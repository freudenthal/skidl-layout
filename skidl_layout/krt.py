"""Opt-in routability feedback via KiCadRoutingTools (KRT).

This module wires an *external* autorouter into skidl-layout as a request-only
feedback stage. It is never called from ``plan_layout`` / ``evaluate_circuit``;
callers invoke :func:`evaluate_routability` (or the lower-level
:func:`route_and_check`) explicitly, exactly like the ``alpha_relax`` precedent.

KRT is discovered by path at runtime (like ``kicad-cli`` in ``validator.py``);
it is not imported, installed, or vendored. The three KRT CLIs are invoked as
subprocesses because their ``main()`` entry points carry post-passes that the
bare engine functions lack (per KRT's own CLAUDE.md):

    route.py <in.kicad_pcb> <out.kicad_pcb>   -> autoroute, prints JSON_SUMMARY
    check_connected.py <pcb> [--routed-only]  -> connectivity verification
    check_drc.py <pcb>                        -> clearance/DRC grading

Freerouting (the never-implemented Java idea) is dropped: it is not installed on
this machine and this path requires no Java.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from .routability import RoutabilityFeedback

logger = logging.getLogger(__name__)

_SOURCE = "kicad_routing_tools"


class KrtNotFoundError(RuntimeError):
    """Raised when no usable KiCadRoutingTools checkout can be located."""


# ---------------------------------------------------------------------------
# Discovery (mirrors validator.find_kicad_cli: return None if unavailable)
# ---------------------------------------------------------------------------

def _is_usable_krt(path: Path) -> bool:
    if not (path / "route.py").is_file():
        return False
    router = path / "rust_router"
    return (router / "grid_router.pyd").is_file() or (
        router / "grid_router.so"
    ).is_file()


def find_krt(krt_dir: str | None = None) -> str | None:
    """Locate a usable KRT checkout; return its path or None if unavailable.

    Resolution order: explicit ``krt_dir`` arg -> env ``SKIDL_LAYOUT_KRT_DIR``
    -> the workspace sibling ``<parents[2]>/KiCadRoutingTools``. 'Usable' means
    the directory holds ``route.py`` and a built ``rust_router/grid_router``
    extension (``.pyd`` on Windows, ``.so`` elsewhere).
    """
    candidates = []
    if krt_dir:
        candidates.append(krt_dir)
    env_dir = os.environ.get("SKIDL_LAYOUT_KRT_DIR")
    if env_dir:
        candidates.append(env_dir)
    candidates.append(
        str(Path(__file__).resolve().parents[2] / "KiCadRoutingTools")
    )
    for candidate in candidates:
        try:
            path = Path(candidate)
        except (TypeError, ValueError):
            continue
        if path.is_dir() and _is_usable_krt(path):
            return str(path)
    return None


# ---------------------------------------------------------------------------
# Pure parse helpers (testable without any subprocess)
# ---------------------------------------------------------------------------

_ROUTED_COUNT_RE = re.compile(r"Checking (\d+) routed nets")
_UNROUTED_NET_RE = re.compile(r"^    (.+?) \(\d+ pads\)$")
_CONNECTIVITY_NET_RE = re.compile(r"^  (.+?) \(net \d+\):$")
_DRC_COUNT_RE = re.compile(r"FOUND (\d+) DRC VIOLATIONS")


def _parse_route_summary(stdout: str) -> dict:
    """Extract the JSON payload from the last ``JSON_SUMMARY:`` line.

    Raises RuntimeError if no such line is present (route.py did not complete).
    Used only as a "route.py ran" sentinel: its ``successful``/``failed`` net
    tallies are a per-run heuristic proxy (they count only single-ended /
    multipoint *phases*, not the whole board) and KRT's own docs warn routers
    can report false success — so the feedback *counts* come from the
    authoritative ``check_connected.py`` verifier, not from here.
    """
    payload = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("JSON_SUMMARY:"):
            payload = stripped[len("JSON_SUMMARY:"):].strip()
    if payload is None:
        raise RuntimeError("no JSON_SUMMARY line in route.py output")
    return json.loads(payload)


def _parse_connected_output(text: str) -> tuple[int | None, list[str], list[str]]:
    """Parse ``check_connected.py`` (full-board) output.

    Returns ``(routed_net_count, unrouted_nets, broken_nets)``:

    - ``routed_net_count`` from the ``Checking N routed nets`` line (nets that
      carry copper); ``None`` if the line is absent.
    - ``unrouted_nets`` — nets with pads but *no* copper at all, from the
      ``Unrouted nets (N):`` block (``    NAME (P pads)``); these are not in
      ``routed_net_count``.
    - ``broken_nets`` — routed-but-disconnected nets, from the
      ``Connectivity issues (K):`` block (``  NAME (net ID):``); these *are*
      already counted in ``routed_net_count``.
    """
    match = _ROUTED_COUNT_RE.search(text)
    routed_count = int(match.group(1)) if match else None
    if "ALL NETS FULLY CONNECTED" in text:
        return routed_count, [], []
    unrouted: list[str] = []
    broken: list[str] = []
    for line in text.splitlines():
        match = _UNROUTED_NET_RE.match(line)
        if match:
            unrouted.append(match.group(1))
            continue
        match = _CONNECTIVITY_NET_RE.match(line)
        if match:
            broken.append(match.group(1))
    return routed_count, unrouted, broken


def _parse_drc_output(text: str) -> int:
    """Return the DRC violation count from check_drc output.

    ``FOUND N DRC VIOLATIONS`` -> N; ``NO DRC VIOLATIONS FOUND!`` -> 0;
    otherwise 0 (grader did not report a count).
    """
    match = _DRC_COUNT_RE.search(text)
    if match:
        return int(match.group(1))
    if "NO DRC VIOLATIONS FOUND" in text:
        return 0
    return 0


def _feedback_from_outputs(
    routed_pcb_text: str,
    connected_output: str,
    drc_output: str,
) -> RoutabilityFeedback:
    """Assemble a RoutabilityFeedback from the authoritative verifier outputs.

    Counts come from ``check_connected.py`` (full board) plus a copper tally of
    the routed file, not from route.py's JSON_SUMMARY (see
    :func:`_parse_route_summary`). ``total_nets`` = nets needing routing =
    copper-carrying nets + never-routed nets; ``unrouted_count`` = never-routed
    + routed-but-broken.
    """
    routed_count, unrouted, broken = _parse_connected_output(connected_output)
    unrouted_nets = unrouted + broken
    unrouted_count = len(unrouted_nets)
    # broken nets already sit inside routed_count (they carry copper); only the
    # zero-copper 'unrouted' nets extend the denominator.
    total_nets = (routed_count or 0) + len(unrouted)
    track_count = routed_pcb_text.count("(segment")
    via_count = routed_pcb_text.count("(via")

    return RoutabilityFeedback(
        unrouted_count=unrouted_count,
        total_nets=total_nets,
        unrouted_nets=unrouted_nets,
        drc_violation_count=_parse_drc_output(drc_output),
        track_count=track_count,
        via_count=via_count,
        source=_SOURCE,
    )


# ---------------------------------------------------------------------------
# Subprocess orchestration
# ---------------------------------------------------------------------------

def _run_krt(args: list[str], krt_dir: str, timeout_s: int) -> subprocess.CompletedProcess:
    """Run a KRT CLI in ``krt_dir`` with the current interpreter and utf-8."""
    cmd = [sys.executable, "-X", "utf8"] + args
    logger.debug("KRT run: %s (cwd=%s)", cmd, krt_dir)
    return subprocess.run(
        cmd,
        cwd=krt_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )


def route_and_check(
    pcb_path: str,
    workdir: str,
    krt_dir: str | None = None,
    nets: list[str] | None = None,
    timeout_s: int = 900,
) -> RoutabilityFeedback:
    """Route ``pcb_path`` with KRT, verify connectivity + DRC, return feedback.

    Routes to a unique fresh basename inside ``workdir`` (the caller owns
    cleanup) to avoid the ``.kicad_pro`` DRC-floor readback gotcha. Raises
    :class:`KrtNotFoundError` if no KRT checkout is found and RuntimeError on a
    route.py timeout/crash (checker 'issues found' exits are data, not errors).
    """
    resolved = find_krt(krt_dir)
    if resolved is None:
        raise KrtNotFoundError(
            "KiCadRoutingTools not found (set SKIDL_LAYOUT_KRT_DIR or place a "
            "built checkout at the workspace sibling KiCadRoutingTools/)"
        )

    os.makedirs(workdir, exist_ok=True)
    in_abs = os.path.abspath(pcb_path)
    out_abs = os.path.join(
        os.path.abspath(workdir), f"routed_{uuid.uuid4().hex[:8]}.kicad_pcb"
    )

    route_args = ["route.py", in_abs, out_abs]
    if nets:
        route_args.append("--nets")
        route_args.extend(nets)
    try:
        route_proc = _run_krt(route_args, resolved, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"route.py timed out after {timeout_s}s") from exc

    try:
        summary_ok = True
        _parse_route_summary(route_proc.stdout)
    except (RuntimeError, json.JSONDecodeError):
        summary_ok = False
    if not summary_ok:
        tail = "\n".join((route_proc.stdout + route_proc.stderr).splitlines()[-30:])
        raise RuntimeError(
            f"route.py produced no JSON_SUMMARY (exit {route_proc.returncode}):\n{tail}"
        )

    try:
        with open(out_abs, "r", encoding="utf-8", errors="replace") as handle:
            routed_text = handle.read()
    except OSError as exc:
        raise RuntimeError(f"route.py did not write output {out_abs}") from exc

    # Full-board connectivity (NOT --routed-only): the authoritative verifier
    # reports both never-routed nets and routed-but-broken nets, and is the
    # source of the feedback counts (route.py's own tally is an unreliable
    # proxy per KRT's docs). check_connected/check_drc exit 1 on 'issues found'
    # -- that is data, not an error, so their return codes are not raised on.
    conn_proc = _run_krt(["check_connected.py", out_abs], resolved, timeout_s)
    drc_proc = _run_krt(["check_drc.py", out_abs], resolved, timeout_s)

    feedback = _feedback_from_outputs(
        routed_text, conn_proc.stdout, drc_proc.stdout
    )
    logger.info("KRT routability: %s", feedback.summary().replace("\n", " | "))
    return feedback


def evaluate_routability(
    result,
    circuit,
    fp_lib_dirs: list[str],
    workdir: str,
    krt_dir: str | None = None,
    lib_table: dict | None = None,
) -> RoutabilityFeedback:
    """Emit ``result``'s placement to a board, route it, and attach feedback.

    Writes ``<workdir>/placed.kicad_pcb`` from ``result.placed_parts`` (with the
    same outline/cutouts the placement used), routes it via
    :func:`route_and_check`, sets ``result.routability`` to the returned
    feedback, and returns it. Request-only; not called from ``plan_layout``.
    """
    from .writer import write_kicad_pcb

    os.makedirs(workdir, exist_ok=True)
    placed_pcb = os.path.join(os.path.abspath(workdir), "placed.kicad_pcb")
    write_kicad_pcb(
        result.placed_parts,
        circuit,
        fp_lib_dirs,
        placed_pcb,
        outline=result.outline,
        cutouts=getattr(result, "cutouts", None),
        lib_table=lib_table,
    )
    feedback = route_and_check(placed_pcb, workdir, krt_dir=krt_dir)
    result.routability = feedback
    return feedback
