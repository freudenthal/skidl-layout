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
_NET_DECL_RE = re.compile(r'\(net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\)')
_SEGMENT_RE = re.compile(
    r"\(segment\b.*?\(width\s+([0-9.]+)\).*?\(net\s+(\d+)\)",
    re.DOTALL,
)


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


def _parse_net_id_map(pcb_text: str) -> dict[int, str]:
    """Map net id -> net name from a board's ``(net ID "name")`` declarations."""
    net_map: dict[int, str] = {}
    for match in _NET_DECL_RE.finditer(pcb_text):
        net_map[int(match.group(1))] = match.group(2).replace('\\"', '"')
    return net_map


def _segment_widths_by_net(pcb_text: str) -> dict[str, float]:
    """Return the max ``(segment)`` width (mm) emitted for each net name.

    A net with no routed segments (e.g. a poured plane net) is absent from the
    result. Vias are ignored; this measures trace copper only.
    """
    net_map = _parse_net_id_map(pcb_text)
    widths: dict[str, float] = {}
    for match in _SEGMENT_RE.finditer(pcb_text):
        width = float(match.group(1))
        net_name = net_map.get(int(match.group(2)))
        if net_name is None:
            continue
        prev = widths.get(net_name)
        if prev is None or width > prev:
            widths[net_name] = width
    return widths


def _parse_zone_summary(pcb_text: str) -> dict:
    """Count poured copper in a board: zones, filled polygons, vias, segments.

    Counts are **token-bounded** (``\\(zone\\b``), not raw substrings. A plain
    ``count("(zone")`` also matches every pad's ``(zone_connect ...)``, which
    over-reported ``zone_count`` by one per such pad -- enough to mask
    ``power_copper``'s under-pour warning. ``\\b`` does not match before ``_``,
    so ``(zone_connect`` no longer counts as a zone.
    """
    return {
        "zone_count": len(re.findall(r"\(zone\b", pcb_text)),
        "filled_polygon_count": len(re.findall(r"\(filled_polygon\b", pcb_text)),
        "via_count": len(re.findall(r"\(via\b", pcb_text)),
        "segment_count": len(re.findall(r"\(segment\b", pcb_text)),
    }


_ZONE_NET_NAME_RE = re.compile(r'\(net_name\s+"?([^")\s]*)"?\s*\)')
#: The KiCad-10 spelling of a zone's net header: ``(net "GND")``, with no
#: ``net_name`` line at all. ⚠ Both forms must be read, because which one a zone
#: carries is a property of the BOARD's ``(version …)`` and not of this stack --
#: ``route_planes.py`` picks it per board, and Phase 15's own writer path does
#: the same. Reading only ``net_name`` made every zone on a KiCad-10 board count
#: under the empty name, which reads as "the pour wrote nothing".
_ZONE_NET_NAME10_RE = re.compile(r'\(net\s+"((?:[^"\\]|\\.)*)"\s*\)')
#: How far into a zone chunk the net header can be. A zone opens with its net on
#: the very next line; 200 characters is generous for it and far short of the
#: first pad of any footprint that follows the last zone in the file.
_ZONE_HEADER_CHARS = 200


def _zone_counts_by_net(pcb_text: str) -> dict[str, int]:
    """Zones written per net name, read off a poured board.

    A zone block opens with ``(zone`` and names its net a couple of lines in
    (``(net 3)`` / ``(net_name "GND")``). Splitting on the zone token and taking
    the first ``net_name`` in each chunk is enough -- nothing else in a
    ``.kicad_pcb`` emits ``net_name`` -- and it is what makes a promoted net
    that poured *nothing* visible instead of hidden inside a board total.
    """
    counts: dict[str, int] = {}
    chunks = re.split(r"\(zone\b", pcb_text)
    for chunk in chunks[1:]:
        match = _ZONE_NET_NAME_RE.search(chunk)
        if match is None:
            # KiCad 10: the zone opens ``(net "GND")`` and has no net_name line.
            # ⚠ Bounded to the zone HEADER, not the whole chunk: the final
            # chunk runs to end-of-file, and an unbounded search there would
            # happily read a pad's ``(net "…")`` out of a following footprint.
            match = _ZONE_NET_NAME10_RE.search(chunk[:_ZONE_HEADER_CHARS])
        name = match.group(1).replace('\\"', '"') if match else ""
        counts[name] = counts.get(name, 0) + 1
    return counts


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


def _design_rule_flags(
    track_width: float | None,
    clearance: float | None,
    via_size: float | None,
    via_drill: float | None,
    board_edge_clearance: float | None = None,
) -> list[str]:
    """Assemble the KRT design-rule CLI flags for the values that are set.

    A ``None`` value emits no flag (so the router keeps its own default and the
    argv stays byte-identical to a call that never passed a spec). Shared by
    :func:`route_and_check` and :func:`pour_planes` (both scripts accept the same
    ``--track-width/--clearance/--via-size/--via-drill`` names).
    """
    flags: list[str] = []
    if track_width is not None:
        flags += ["--track-width", f"{track_width:g}"]
    if clearance is not None:
        flags += ["--clearance", f"{clearance:g}"]
    if via_size is not None:
        flags += ["--via-size", f"{via_size:g}"]
    if via_drill is not None:
        flags += ["--via-drill", f"{via_drill:g}"]
    if board_edge_clearance is not None:
        flags += ["--board-edge-clearance", f"{board_edge_clearance:g}"]
    return flags


def route_and_check(
    pcb_path: str,
    workdir: str,
    krt_dir: str | None = None,
    nets: list[str] | None = None,
    timeout_s: int = 900,
    power_net_widths: dict[str, float] | None = None,
    out_path: str | None = None,
    route_extra_args: list[str] | None = None,
    track_width: float | None = None,
    clearance: float | None = None,
    via_size: float | None = None,
    via_drill: float | None = None,
    board_edge_clearance: float | None = None,
    net_clearances: dict[str, float] | None = None,
    route_log_path: str | None = None,
) -> RoutabilityFeedback:
    """Route ``pcb_path`` with KRT, verify connectivity + DRC, return feedback.

    Routes to a unique fresh basename inside ``workdir`` (the caller owns
    cleanup) to avoid the ``.kicad_pro`` DRC-floor readback gotcha. Raises
    :class:`KrtNotFoundError` if no KRT checkout is found and RuntimeError on a
    route.py timeout/crash (checker 'issues found' exits are data, not errors).

    ``power_net_widths`` maps net name -> track width (mm); those nets are routed
    at the requested width via route.py ``--power-nets``/``--power-nets-widths``
    (each name passed as an exact fnmatch pattern; KRT floors it to
    ``--track-width`` and neck-downs at pads by default). ``out_path`` pins the
    routed-board destination (default: fresh basename in ``workdir``) so a caller
    can chain a plane-pour pass on the routed board. The routed-board path is
    exposed on the returned feedback's ``.source`` unchanged; callers that need
    the file should pass ``out_path`` explicitly. ``route_extra_args`` are extra
    route.py flags appended verbatim (e.g. ``["--max-ripup", "10",
    "--max-iterations", "1000000"]`` for a congested 2-layer board).

    ``net_clearances`` maps net name -> copper clearance (mm), written to a JSON
    file in ``workdir`` and passed as route.py ``--net-clearances``. ⚠⚠ Three
    things about it, all measured rather than read off the CLI docs (Phase 10,
    WS-2):

    - An explicit map is used **as-is**. ``--clearance`` is a ceiling over the
      net-class map KRT *auto-reads* from a sibling ``.kicad_pro``, not over this
      one, so a per-net value above ``clearance`` genuinely widens the net
      (``SW``<->``VIN`` on ``lt3758_flyback``: 0.1984 mm -> 0.552 mm with the
      ceiling still at 0.25).
    - An explicit map **replaces** the auto-read one rather than adding to it,
      which is why an empty/``None`` map must emit no flag at all -- passing
      ``{}`` would discard whatever net classes the board carried.
    - ⛔ **The widening binds BOARD-WIDE, not pairwise.** This docstring said
      "pairwise between nets the map names" until Phase 12; Phase 11 measured it
      false and the line survived the retraction. KRT's
      ``RoutingConfig.set_net_clearances`` derives
      ``net_clearance_floor = max(clearance, max over the ROUTED nets in the
      map)`` and ``obstacle_clearance()`` returns ``max(floor, ...)`` for
      **every** obstacle. Measured on ``uc3844_flyback``: the map named 5 nets
      and **all 17 on the board widened**. The corollary is the cost -- on a
      routing-limited board the map does not *add* clearance, it
      **redistributes** it (``lt3724_buck`` moved 12 of 16 nets and 7 got
      *tighter*).
    - ⚠ KRT's fine-pitch rescue ladder still necks a rescued net below the
      requested value, so this raises what the router **aims for**; it is not a
      floor. Phase 12 derived the ladder exactly: a rescued net is shipped at one
      of ``0.1524 / 0.1768 / 0.2012 / 0.2256 / 0.25`` mm on this stack's fab
      numbers, whatever the map asked for. See
      ``workingdocs/design_considerations/krt-upstream-rescue-ladder-report.md``.
    """
    resolved = find_krt(krt_dir)
    if resolved is None:
        raise KrtNotFoundError(
            "KiCadRoutingTools not found (set SKIDL_LAYOUT_KRT_DIR or place a "
            "built checkout at the workspace sibling KiCadRoutingTools/)"
        )

    os.makedirs(workdir, exist_ok=True)
    in_abs = os.path.abspath(pcb_path)
    if out_path is not None:
        out_abs = os.path.abspath(out_path)
    else:
        out_abs = os.path.join(
            os.path.abspath(workdir), f"routed_{uuid.uuid4().hex[:8]}.kicad_pcb"
        )

    route_args = ["route.py", in_abs, out_abs]
    if nets:
        route_args.append("--nets")
        route_args.extend(nets)
    # Design-rule flags (from a FabSpec); each None value emits nothing, so a
    # call with no spec is byte-identical argv to before this change.
    route_args.extend(_design_rule_flags(
        track_width, clearance, via_size, via_drill, board_edge_clearance))
    if net_clearances:
        # Empty dict deliberately falls through to no flag: an explicit map
        # REPLACES KRT's auto-read net-class map, so passing "{}" would throw
        # away the board's own classes rather than mean "no opinion".
        nc_path = os.path.join(
            os.path.abspath(workdir), f"net_clearances_{uuid.uuid4().hex[:8]}.json")
        with open(nc_path, "w", encoding="utf-8") as handle:
            json.dump({str(k): float(v) for k, v in net_clearances.items()},
                      handle, indent=2, sort_keys=True)
        route_args += ["--net-clearances", nc_path]
    if route_extra_args:
        # verbatim route.py flags (e.g. ["--max-ripup", "10", "--max-iterations",
        # "1000000"] for a difficult 2-layer board, per plan-pcb-routing SKILL).
        route_args.extend(route_extra_args)
    if power_net_widths:
        # names first, then the matching widths in the same order (fnmatch
        # patterns; exact net names are literal since '+'/'_' aren't wildcards).
        names = list(power_net_widths)
        route_args.append("--power-nets")
        route_args.extend(names)
        route_args.append("--power-nets-widths")
        route_args.extend(f"{power_net_widths[name]:g}" for name in names)
    try:
        route_proc = _run_krt(route_args, resolved, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"route.py timed out after {timeout_s}s") from exc

    # Phase 11: route.py's own log is the ONLY place its fine-pitch rescue
    # ladder is visible ("rescued a gap: grid ..., clearance ..., track ..."),
    # and a rescue is what necks a net below the per-net clearance the caller
    # asked for. Opt-in and argv-neutral: nothing about the route changes, the
    # log is simply kept instead of discarded, so the evidence can be re-derived
    # rather than quoted from a past run.
    if route_log_path:
        os.makedirs(os.path.dirname(os.path.abspath(route_log_path)) or ".",
                    exist_ok=True)
        with open(route_log_path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(route_proc.stdout or "")
            handle.write(route_proc.stderr or "")

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


def check_board(
    pcb_path: str,
    krt_dir: str | None = None,
    timeout_s: int = 900,
) -> RoutabilityFeedback:
    """Grade an already-routed/poured board: connectivity + DRC, no routing.

    Runs ``check_connected.py`` + ``check_drc.py`` on ``pcb_path`` and assembles
    a :class:`RoutabilityFeedback` (zone-aware connectivity, so poured plane nets
    count as connected). Used to grade the FINAL board after route + pour.
    """
    resolved = find_krt(krt_dir)
    if resolved is None:
        raise KrtNotFoundError(
            "KiCadRoutingTools not found (set SKIDL_LAYOUT_KRT_DIR or place a "
            "built checkout at the workspace sibling KiCadRoutingTools/)"
        )
    in_abs = os.path.abspath(pcb_path)
    with open(in_abs, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    conn_proc = _run_krt(["check_connected.py", in_abs], resolved, timeout_s)
    drc_proc = _run_krt(["check_drc.py", in_abs], resolved, timeout_s)
    return _feedback_from_outputs(text, conn_proc.stdout, drc_proc.stdout)


def grade_pour(
    pcb_path: str,
    krt_dir: str | None = None,
    timeout_s: int = 900,
    min_clearance_used=None,
) -> dict:
    """The :func:`pour_planes` summary shape, for a board poured by other means.

    ⭐ **Phase 15.** When zones are authored by ``power_zones`` and spliced
    directly, ``route_planes.py`` never runs -- so nothing produces the summary
    every downstream gate reads (``zone_count``, ``connected_ok``,
    ``unrouted_nets``, ``broken_nets``, ``drc_violation_count``). This runs the
    *same two graders on the same final board* and assembles the *same dict*, so
    a caller cannot tell which pour path produced the numbers. That is the point:
    a driver comparing a Voronoi arm with a polygon arm must be comparing two
    measurements, not two measurement methods.

    ``min_clearance_used`` is carried through from a partial ``pour_planes`` run
    when one happened (a mixed board pours some nets each way); it is not
    re-derivable from the board text.
    """
    resolved = find_krt(krt_dir)
    if resolved is None:
        raise KrtNotFoundError(
            "KiCadRoutingTools not found (set SKIDL_LAYOUT_KRT_DIR or place a "
            "built checkout at the workspace sibling KiCadRoutingTools/)"
        )
    out_abs = os.path.abspath(pcb_path)
    with open(out_abs, "r", encoding="utf-8", errors="replace") as handle:
        poured_text = handle.read()
    conn_proc = _run_krt(["check_connected.py", out_abs], resolved, timeout_s)
    drc_proc = _run_krt(["check_drc.py", out_abs], resolved, timeout_s)
    _routed_count, unrouted, broken = _parse_connected_output(conn_proc.stdout)
    summary = _parse_zone_summary(poured_text)
    summary.update(
        min_clearance_used=min_clearance_used,
        connected_ok=(not unrouted and not broken),
        unrouted_nets=unrouted,
        broken_nets=broken,
        drc_violation_count=_parse_drc_output(drc_proc.stdout),
        out_path=out_abs,
    )
    return summary


def pour_planes(
    pcb_path: str,
    out_path: str,
    nets: list[str],
    plane_layers: list[str],
    workdir: str,
    krt_dir: str | None = None,
    timeout_s: int = 900,
    add_gnd_vias: bool = False,
    gnd_via_distance: float = 2.0,
    track_width: float | None = None,
    clearance: float | None = None,
    via_size: float | None = None,
    via_drill: float | None = None,
    zone_clearance: float | None = None,
    min_thickness: float | None = None,
) -> dict:
    """Pour copper planes on ``nets`` and re-verify the board.

    Runs KRT ``route_planes.py --nets <names> --plane-layers <layers>`` (one
    layer per net, paired positionally) to write real ``(zone ... (fill yes))``
    objects to ``out_path``, then re-runs ``check_connected.py`` +
    ``check_drc.py`` on the final board and folds the results into the returned
    summary. A pour that strands an island is a failure signal
    (``connected_ok`` False); KRT ships ``route_disconnected_planes.py`` for
    that repair, deliberately NOT auto-invoked here (round-1 scope).

    Ordering note (from ``KiCadRoutingTools/.claude/skills/plan-pcb-routing/``
    ``SKILL.md`` Steps 2->3): signals are routed FIRST (plane nets excluded from
    ``route.py --nets``, wide power carried inside it via ``--power-nets``), and
    planes are poured LAST so their stitching vias adapt around the finished
    tracks. Callers (``power_copper.emit_power_copper``) sequence route ->
    pour accordingly; this function is the pour half only.

    Returns a parsed summary dict: ``{"zone_count", "filled_polygon_count",
    "via_count", "segment_count", "min_clearance_used", "connected_ok",
    "unrouted_nets", "broken_nets", "drc_violation_count", "out_path"}``.
    Raises :class:`KrtNotFoundError` if no KRT checkout is found and RuntimeError
    on a route_planes.py timeout/crash.
    """
    if len(nets) != len(plane_layers):
        raise ValueError(
            f"nets ({len(nets)}) and plane_layers ({len(plane_layers)}) "
            "must be the same length (one layer per net)"
        )
    resolved = find_krt(krt_dir)
    if resolved is None:
        raise KrtNotFoundError(
            "KiCadRoutingTools not found (set SKIDL_LAYOUT_KRT_DIR or place a "
            "built checkout at the workspace sibling KiCadRoutingTools/)"
        )

    os.makedirs(workdir, exist_ok=True)
    in_abs = os.path.abspath(pcb_path)
    out_abs = os.path.abspath(out_path)

    args = ["route_planes.py", in_abs, out_abs, "--nets", *nets,
            "--plane-layers", *plane_layers]
    # Via/track/clearance design-rule flags (from a FabSpec); None emits nothing
    # so a no-spec pour keeps KRT's own defaults and byte-identical argv.
    args.extend(_design_rule_flags(track_width, clearance, via_size, via_drill))
    if zone_clearance is not None:
        args += ["--zone-clearance", f"{zone_clearance:g}"]
    if min_thickness is not None:
        args += ["--min-thickness", f"{min_thickness:g}"]
    if add_gnd_vias:
        args += ["--add-gnd-vias", "--gnd-via-distance", f"{gnd_via_distance:g}"]
    try:
        plane_proc = _run_krt(args, resolved, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"route_planes.py timed out after {timeout_s}s") from exc

    try:
        plane_summary = _parse_route_summary(plane_proc.stdout)
    except (RuntimeError, json.JSONDecodeError):
        tail = "\n".join((plane_proc.stdout + plane_proc.stderr).splitlines()[-30:])
        raise RuntimeError(
            f"route_planes.py produced no JSON_SUMMARY "
            f"(exit {plane_proc.returncode}):\n{tail}"
        )

    try:
        with open(out_abs, "r", encoding="utf-8", errors="replace") as handle:
            poured_text = handle.read()
    except OSError as exc:
        raise RuntimeError(f"route_planes.py did not write output {out_abs}") from exc

    conn_proc = _run_krt(["check_connected.py", out_abs], resolved, timeout_s)
    drc_proc = _run_krt(["check_drc.py", out_abs], resolved, timeout_s)
    _routed_count, unrouted, broken = _parse_connected_output(conn_proc.stdout)

    summary = _parse_zone_summary(poured_text)
    summary.update(
        min_clearance_used=plane_summary.get("min_clearance_used"),
        connected_ok=(not unrouted and not broken),
        unrouted_nets=unrouted,
        broken_nets=broken,
        drc_violation_count=_parse_drc_output(drc_proc.stdout),
        out_path=out_abs,
    )
    logger.info(
        "KRT planes: %d zone(s), %d filled polygon(s), connected=%s, DRC=%d",
        summary["zone_count"], summary["filled_polygon_count"],
        summary["connected_ok"], summary["drc_violation_count"],
    )
    return summary
