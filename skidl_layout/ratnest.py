"""Rat-nest analysis of a **placed** ``.kicad_pcb``.

Everything else in this package *generates* a board. This module reads one back
and measures the airwire structure a router will have to realise:

* per-net **minimum spanning tree over real pad positions** (not part centroids),
* **crossings** between those airwires,
* total **rat-nest length**,
* **part-pair TWIST** — two nets running between the same two parts whose
  airwires cross, with the rotation that would resolve that pair.

⛔⛔ **Twist is a DESCRIPTIVE metric, not a validated objective. Measured
2026-07-30 and it did not survive its first routing test:** on the hand-placed
``lt3724_buck`` the one net that failed to route (``VIN``) terminates at the
exact pad flagged by the twisted pair ``CIN<->J1`` — so the metric looked
predictive. Applying its own recommended repair (``CIN +180``) and re-routing
with identical settings **recovered nothing** (13/16 before and after, same
failing net), *relocated* the twist rather than removing it (``CIN<->J1``
disappeared, ``CIN<->U1`` appeared, count unchanged at 6), and made the board's
total rat-nest **longer**, 283.63 -> 287.49 mm. A control rotation of an
unrelated part scored the same 13/16. **Do not wire twist into a scorer, and do
not present a twist as a defect to fix, until a judged experiment says
otherwise.** This is the field's standing result -- proxy-routability
correlation is weak -- reproduced on a brand-new proxy within the hour.

Why this exists (measured 2026-07-30, see ``todo.md``): ``scoring``'s
``_estimate_crossings`` builds a **star** (every ref spoked to one anchor) over
**part centroids**. Both choices are wrong in ways that do not cancel — against
an MST-over-pads reference it ran 1.76x–3.62x high on seven boards, and on
signal-only nets it went both over *and* under. A non-constant error does not
merely mis-scale the objective, it **mis-ranks placements**, so a scorer built on
it can prefer the worse board. This module is the reference the fix is measured
against; it deliberately does not change ``scoring`` (that is a separate,
judged step).

**Determinism.** The MST here is our own Prim's with an explicit
``(distance, index)`` tie-break, *not* KRT's ``compute_mst_edges``. KRT's
placement stack is documented as not process-deterministic — set-order airwire
iteration feeds its MST tie-breaks, so it needs ``PYTHONHASHSEED=0``
(``placement/README.md``, KRT ``#457``). Every placement digest this stack gates
on assumes byte-identical results across processes, so the tie-break is pinned
here by construction instead.

**The one KRT dependency is parsing.** ``kicad_parser`` is a pure-Python import
(escalation rung 2 — call KRT's public functions), and it owns two details worth
not re-deriving: KiCad's rotation convention negates the angle, and pad globals
must be snapped to the integer-nanometre grid or two implementations disagree in
the last bits. The *geometry* in this module needs no KRT and is unit-tested
without it.
"""

from __future__ import annotations

import itertools
import math
import sys
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .roles import GND_NET_RE, POWER_NET_RE

__all__ = [
    "PadPoint",
    "Airwire",
    "TwistedPair",
    "RatNest",
    "segments_cross",
    "mst_edges",
    "net_airwires",
    "count_crossings",
    "is_plane_net",
    "read_pad_points",
    "twisted_pairs",
    "analyse_board",
]

#: Rotations offered when asking "would turning this part untwist the pair?".
#: KiCad's own placement lattice; a part on a 45-degree seed keeps its own
#: lattice because the deltas are applied *relative* to its current angle.
ROTATION_CANDIDATES = (90.0, 180.0, 270.0)

#: Endpoint-coincidence tolerance, mm. Two airwires meeting at a shared pad are
#: not a crossing. 1 um is far below any real pad pitch and far above the
#: nanometre grid the parser snaps to.
SHARED_ENDPOINT_TOL_MM = 1e-3


@dataclass(frozen=True)
class PadPoint:
    """One net-carrying pad, resolved to board coordinates."""

    ref: str
    pad: str
    net: str
    x: float
    y: float

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.pad}"


@dataclass(frozen=True)
class Airwire:
    """One rat-nest edge: an MST edge of a single net, between two pads."""

    net: str
    a: PadPoint
    b: PadPoint

    @property
    def length_mm(self) -> float:
        return math.dist((self.a.x, self.a.y), (self.b.x, self.b.y))

    @property
    def p1(self) -> tuple[float, float]:
        return (self.a.x, self.a.y)

    @property
    def p2(self) -> tuple[float, float]:
        return (self.b.x, self.b.y)


@dataclass(frozen=True)
class TwistedPair:
    """Two parts joined by >=2 nets whose airwires cross.

    ``fixes`` lists ``(ref, delta_deg)`` rotations that resolve **this pair**.

    ⛔⛔ **The test is pair-local and that is not a footnote -- it is measured.**
    Rotating a part changes every pair it belongs to. On the hand-placed
    ``lt3724_buck``, applying this class's own recommended repair to ``CIN``
    resolved ``CIN<->J1`` and immediately created ``CIN<->U1``: twist count
    unchanged, total rat-nest 3.9 mm **longer**, routing completion unchanged.
    ``shortens_mm`` is likewise pair-local and was **+0.77 mm for the pair while
    the board got 3.86 mm worse**.

    ⚠ A high-pin-count part named in ``fixes`` is almost never actionable --
    turning an IC to fix one decoupling cap breaks its other pairs. Read a pair
    whose only listed fix rotates an IC as "no cheap repair", not as an
    instruction.
    """

    ref_a: str
    ref_b: str
    nets: tuple[str, ...]
    crossings: int
    fixes: tuple[tuple[str, float], ...] = ()
    length_mm: float = 0.0
    best_length_mm: float | None = None

    @property
    def shortens_mm(self) -> float:
        """How much the best fix also shortens the pair's wire (>=0 is a win)."""
        if self.best_length_mm is None:
            return 0.0
        return self.length_mm - self.best_length_mm


@dataclass
class RatNest:
    """Everything measurable about a placed board's rat-nest."""

    source: str
    kicad_version: int | None = None
    copper_layers: tuple[str, ...] = ()
    part_count: int = 0
    net_count: int = 0
    pad_count: int = 0
    plane_nets: tuple[str, ...] = ()
    airwires: tuple[Airwire, ...] = ()
    twisted: tuple[TwistedPair, ...] = ()
    pair_count: int = 0

    # -- derived ---------------------------------------------------------- #
    @property
    def length_mm(self) -> float:
        return sum(w.length_mm for w in self.airwires)

    @property
    def signal_airwires(self) -> tuple[Airwire, ...]:
        planes = set(self.plane_nets)
        return tuple(w for w in self.airwires if w.net not in planes)

    @property
    def signal_length_mm(self) -> float:
        return sum(w.length_mm for w in self.signal_airwires)

    @property
    def crossings(self) -> int:
        return count_crossings(self.airwires)

    @property
    def signal_crossings(self) -> int:
        return count_crossings(self.signal_airwires)

    def longest(self, n: int = 10) -> list[Airwire]:
        return sorted(self.airwires, key=lambda w: -w.length_mm)[:n]

    def summary(self) -> dict:
        """Flat dict for a metrics file or a gate."""
        return {
            "source": self.source,
            "kicad_version": self.kicad_version,
            "copper_layers": list(self.copper_layers),
            "parts": self.part_count,
            "nets": self.net_count,
            "pads": self.pad_count,
            "airwires": len(self.airwires),
            "crossings": self.crossings,
            "signal_crossings": self.signal_crossings,
            "length_mm": round(self.length_mm, 4),
            "signal_length_mm": round(self.signal_length_mm, 4),
            "pairs_multinet": self.pair_count,
            "twisted_pairs": len(self.twisted),
            "twisted": [
                {"a": t.ref_a, "b": t.ref_b, "nets": list(t.nets),
                 "crossings": t.crossings,
                 "fixes": [[r, d] for r, d in t.fixes],
                 "shortens_mm": round(t.shortens_mm, 4)}
                for t in self.twisted
            ],
        }


# --------------------------------------------------------------------------- #
# Geometry -- no KRT, no I/O, unit-testable on its own
# --------------------------------------------------------------------------- #
def segments_cross(a1, a2, b1, b2) -> bool:
    """True when segments a1-a2 and b1-b2 **properly** cross.

    Strict: collinear overlap and endpoint touching are NOT crossings. This is
    the same predicate as ``scoring._segment_intersects``, deliberately, so a
    number from this module is comparable with one from the scorer.

    ⚠ KRT's ``quench`` kernel uses a non-strict ``ccw`` comparison and so counts
    a few collinear cases this does not (measured: 76 vs 72 on one real board).
    Neither is wrong; they are different conventions. Do not mix them in one
    comparison.
    """
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return (orient(a1, a2, b1) * orient(a1, a2, b2) < 0
            and orient(b1, b2, a1) * orient(b1, b2, a2) < 0)


def mst_edges(points: Sequence[tuple[float, float]]) -> list[tuple[int, int]]:
    """Euclidean MST over ``points``; returns ``(i, j)`` index pairs.

    Prim's, O(n^2), which is the right complexity for a PCB net (rarely >30
    pads). ⛔ **The tie-break is load-bearing:** candidates are compared on
    ``(distance, index)``, so equidistant pads resolve by position in the input
    list and never by dict/set iteration order. That is what makes this
    reproducible across processes, which KRT's equivalent is not (its ``#457``).
    Callers must therefore pass ``points`` in a stable order.
    """
    n = len(points)
    if n < 2:
        return []
    in_tree = [False] * n
    in_tree[0] = True
    best_d = [math.dist(points[0], p) for p in points]
    best_src = [0] * n
    edges: list[tuple[int, int]] = []
    for _ in range(n - 1):
        pick, pick_d = -1, math.inf
        for j in range(n):
            # (distance, index): strict < keeps the LOWEST index on a tie.
            if not in_tree[j] and best_d[j] < pick_d:
                pick, pick_d = j, best_d[j]
        if pick < 0:
            break
        in_tree[pick] = True
        edges.append((best_src[pick], pick))
        for j in range(n):
            if not in_tree[j]:
                d = math.dist(points[pick], points[j])
                if d < best_d[j]:
                    best_d[j], best_src[j] = d, pick
    return edges


def net_airwires(pads: Sequence[PadPoint], net: str) -> list[Airwire]:
    """MST airwires for one net's pads, in deterministic order."""
    if len(pads) < 2:
        return []
    pts = [(p.x, p.y) for p in pads]
    return [Airwire(net=net, a=pads[i], b=pads[j]) for i, j in mst_edges(pts)]


def count_crossings(airwires: Iterable[Airwire]) -> int:
    """Properly-crossing pairs, skipping same-net and shared-endpoint pairs."""
    ws = list(airwires)
    total = 0
    for i in range(len(ws)):
        wi = ws[i]
        for j in range(i + 1, len(ws)):
            wj = ws[j]
            if wi.net == wj.net:
                continue
            if _shares_endpoint(wi, wj):
                continue
            if segments_cross(wi.p1, wi.p2, wj.p1, wj.p2):
                total += 1
    return total


def _shares_endpoint(u: Airwire, v: Airwire) -> bool:
    tol = SHARED_ENDPOINT_TOL_MM
    for p in (u.p1, u.p2):
        for q in (v.p1, v.p2):
            if abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol:
                return True
    return False


def is_plane_net(name: str) -> bool:
    """Would this net be POURED rather than routed as tracks?

    Uses the package's own net vocabulary (``roles.GND_NET_RE`` /
    ``POWER_NET_RE``) so one definition serves the whole stack.

    ⚠ **Not cosmetic.** Our boards are roughly half plane-net pins, and KRT
    records ``--ignore-nets`` for plane-routed nets as a *correctness*
    requirement for an honest airwire objective, not a refinement. Measured on
    ``lt3724_buck``: 160 of 184 crossings involved a plane net.
    """
    return bool(GND_NET_RE.match(name) or POWER_NET_RE.match(name))


# --------------------------------------------------------------------------- #
# Board reading -- the only part that needs KRT
# --------------------------------------------------------------------------- #
def _kicad_parser(krt_dir: str | None = None):
    """Import KRT's pure-Python board parser, or raise with a usable message."""
    from .krt import KrtNotFoundError, find_krt

    resolved = find_krt(krt_dir)
    if resolved is None:
        raise KrtNotFoundError(
            "KiCadRoutingTools not found (set SKIDL_LAYOUT_KRT_DIR or place a "
            "built checkout at the workspace sibling KiCadRoutingTools/); "
            "ratnest needs it only to PARSE a board"
        )
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    import kicad_parser  # noqa: PLC0415

    return kicad_parser


def read_pad_points(pcb_path: str, *, krt_dir: str | None = None,
                    rotations: dict[str, float] | None = None,
                    ) -> dict[str, list[PadPoint]]:
    """``{ref: [PadPoint, ...]}`` for every net-carrying pad on a placed board.

    ``rotations`` overrides a part's angle (degrees, absolute) before resolving
    its pads -- this is how the twist repair test asks "where would the pads be
    if I turned it?" without writing a board.

    Pads are returned sorted by ``(ref, pad)`` so downstream MSTs are
    order-stable.
    """
    kp = _kicad_parser(krt_dir)
    pcb = kp.parse_kicad_pcb(pcb_path)
    out: dict[str, list[PadPoint]] = {}
    for ref, fp in pcb.footprints.items():
        rot = fp.rotation if not rotations or ref not in rotations else rotations[ref]
        pts = []
        for pad in fp.pads:
            if not pad.net_id or not pad.net_name:
                continue
            x, y = kp.local_to_global(fp.x, fp.y, rot, pad.local_x, pad.local_y)
            pts.append(PadPoint(ref=ref, pad=str(pad.pad_number),
                                net=pad.net_name, x=x, y=y))
        if pts:
            out[ref] = sorted(pts, key=lambda p: (p.ref, p.pad))
    return out


def _rotate(pads: list[PadPoint], fp_x: float, fp_y: float,
            delta_deg: float) -> list[PadPoint]:
    """Rotate a part's pads about its own origin by ``delta_deg``.

    Mirrors KiCad's convention (the angle is negated) so a delta applied here
    matches what the parser would report after an equivalent board edit.
    """
    rad = math.radians(-delta_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    out = []
    for p in pads:
        dx, dy = p.x - fp_x, p.y - fp_y
        out.append(PadPoint(ref=p.ref, pad=p.pad, net=p.net,
                            x=fp_x + dx * cos_r - dy * sin_r,
                            y=fp_y + dx * sin_r + dy * cos_r))
    return out


def _pair_reps(a_pads: Sequence[PadPoint], b_pads: Sequence[PadPoint],
               ) -> dict[str, tuple[PadPoint, PadPoint]]:
    """Per shared net, the CLOSEST pad pair between two parts.

    The closest pair is the connection a router would actually make, and it is
    what makes the twist test well defined when either part has several pads on
    the same net (an IC with two ground pins, a bulk cap with a split pad).
    """
    by_net_a: dict[str, list[PadPoint]] = {}
    by_net_b: dict[str, list[PadPoint]] = {}
    for p in a_pads:
        by_net_a.setdefault(p.net, []).append(p)
    for p in b_pads:
        by_net_b.setdefault(p.net, []).append(p)
    reps: dict[str, tuple[PadPoint, PadPoint]] = {}
    for net in sorted(set(by_net_a) & set(by_net_b)):
        best = None
        for pa in by_net_a[net]:
            for pb in by_net_b[net]:
                d = math.dist((pa.x, pa.y), (pb.x, pb.y))
                if best is None or d < best[0]:
                    best = (d, pa, pb)
        if best is not None:
            reps[net] = (best[1], best[2])
    return reps


def _twist_count(reps: dict[str, tuple[PadPoint, PadPoint]]) -> int:
    if len(reps) < 2:
        return 0
    n = 0
    for n1, n2 in itertools.combinations(sorted(reps), 2):
        a1, a2 = reps[n1]
        b1, b2 = reps[n2]
        if segments_cross((a1.x, a1.y), (a2.x, a2.y),
                          (b1.x, b1.y), (b2.x, b2.y)):
            n += 1
    return n


def _reps_length(reps: dict[str, tuple[PadPoint, PadPoint]]) -> float:
    return sum(math.dist((a.x, a.y), (b.x, b.y)) for a, b in reps.values())


def twisted_pairs(pads_by_ref: dict[str, list[PadPoint]],
                  origins: dict[str, tuple[float, float]],
                  *, test_rotations: bool = True,
                  ) -> tuple[list[TwistedPair], int]:
    """Find every twisted part pair. Returns ``(twisted, pairs_examined)``.

    ``origins`` maps ref -> footprint origin, needed to rotate a part about its
    own anchor rather than about the board origin.
    """
    twisted: list[TwistedPair] = []
    examined = 0
    for ref_a, ref_b in itertools.combinations(sorted(pads_by_ref), 2):
        a_pads, b_pads = pads_by_ref[ref_a], pads_by_ref[ref_b]
        reps = _pair_reps(a_pads, b_pads)
        if len(reps) < 2:
            continue
        examined += 1
        crossings = _twist_count(reps)
        if not crossings:
            continue
        base_len = _reps_length(reps)
        fixes: list[tuple[str, float]] = []
        best_len: float | None = None
        if test_rotations:
            for ref in (ref_a, ref_b):
                if ref not in origins:
                    continue
                ox, oy = origins[ref]
                for delta in ROTATION_CANDIDATES:
                    if ref == ref_a:
                        trial = _pair_reps(_rotate(a_pads, ox, oy, delta), b_pads)
                    else:
                        trial = _pair_reps(a_pads, _rotate(b_pads, ox, oy, delta))
                    if _twist_count(trial) == 0:
                        fixes.append((ref, delta))
                        tl = _reps_length(trial)
                        if best_len is None or tl < best_len:
                            best_len = tl
                        break  # smallest resolving rotation for this part
        twisted.append(TwistedPair(
            ref_a=ref_a, ref_b=ref_b, nets=tuple(sorted(reps)),
            crossings=crossings, fixes=tuple(fixes),
            length_mm=base_len, best_length_mm=best_len))
    return twisted, examined


def analyse_board(pcb_path: str, *, krt_dir: str | None = None,
                  plane_nets: Iterable[str] | None = None,
                  test_rotations: bool = True) -> RatNest:
    """Read a placed ``.kicad_pcb`` and measure its rat-nest.

    ``plane_nets`` defaults to whatever :func:`is_plane_net` recognises; pass an
    explicit set to grade a board whose supplies are named unconventionally.
    """
    kp = _kicad_parser(krt_dir)
    pcb = kp.parse_kicad_pcb(pcb_path)
    pads_by_ref = read_pad_points(pcb_path, krt_dir=krt_dir)
    origins = {ref: (fp.x, fp.y) for ref, fp in pcb.footprints.items()}

    by_net: dict[str, list[PadPoint]] = {}
    for ref in sorted(pads_by_ref):
        for p in pads_by_ref[ref]:
            by_net.setdefault(p.net, []).append(p)

    wires: list[Airwire] = []
    for net in sorted(by_net):
        wires.extend(net_airwires(by_net[net], net))

    planes = (tuple(sorted(plane_nets)) if plane_nets is not None
              else tuple(sorted(n for n in by_net if is_plane_net(n))))
    twisted, examined = twisted_pairs(pads_by_ref, origins,
                                      test_rotations=test_rotations)

    return RatNest(
        source=pcb_path,
        kicad_version=getattr(pcb, "kicad_version", None),
        copper_layers=tuple(pcb.board_info.copper_layers or ()),
        part_count=len(pcb.footprints),
        net_count=sum(1 for pads in by_net.values() if len(pads) >= 2),
        pad_count=sum(len(v) for v in pads_by_ref.values()),
        plane_nets=planes,
        airwires=tuple(wires),
        twisted=tuple(twisted),
        pair_count=examined,
    )
