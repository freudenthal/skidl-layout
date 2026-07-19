# skidl-layout

A PCB **placement / layout** engine for [SKiDL](https://github.com/devbisme/skidl)
circuits, packaged as a standalone peer tool.

This is a code-only snapshot of the `layout/` package from
[`lachlanfysh/skidl`](https://github.com/lachlanfysh/skidl) (commit
`11e45996`), lifted into its own package so it can be used as an external tool
over a SKiDL `Circuit`, keeping placement / routing / sim as external tools
driven via netlist / CLI / MCP. See [`AUTHORS.md`](AUTHORS.md) for full
provenance.

It is **complementary** to a schematic-generation path: given a SKiDL `Circuit`,
`skidl-layout` consumes the same object and plans a board placement.

## What it does

- Classifies parts (roles), infers placement intent, and places footprints on a
  board outline scored by congestion / routability / power topology.
- Emits a `.kicad_pcb` (footprint placement, no copper) via `write_kicad_pcb`.
- Optionally invokes `kicad-cli` DRC and a KiCadRoutingTools autoroute pass for
  routability feedback (both **external tools**, feature-gated, invoked via
  `subprocess` — not Python dependencies).

## Install

Editable install from local checkouts (Python 3.10+; verified on 3.13). Run from
**inside this `skidl-layout/` directory** — the `../skidl` path assumes the flat
layout where the `skidl` fork checkout sits directly beside this one. The base
dependency is the **`skidl` fork** (the KiCad-10 backend the loop uses is not on
PyPI), so install it editable from the sibling checkout:

```bash
uv venv --python 3.13 .venv
uv pip install -e ../skidl        # the fork (Circuit/Part/Net loop-boundary + KICAD10)
uv pip install -e .               # skidl-layout core (adds simp_sexp)
uv pip install -e ".[shapely]"    # exact polygon containment in the validator
uv pip install -e ".[test]"       # pytest
```

(plain `pip` works the same in an activated venv. If you already built the
`skidl-eda` environment, that venv has `skidl-layout` installed too.)

## Usage sketch

```python
from skidl import Circuit          # build/import a SKiDL circuit
from skidl_layout import plan_layout, write_kicad_pcb

result = plan_layout(circuit)      # LayoutResult: scored placement, no copper
# result.placed_parts -> write_kicad_pcb(...) to emit a .kicad_pcb
```

`plan_layout(circuit, ...)` reads only `circuit.parts` and duck-typed `Part`
attributes (defensively, via `getattr`) — the same loop-boundary shape the
SPICE path uses.

**Faster iteration.** By default `plan_layout` refines every applicable
candidate strategy (up to ~8), which is thorough but slow on large boards. To
trade breadth for speed while iterating, restrict the strategies:

```python
result = plan_layout(circuit, candidate_names=["baseline", "connector_edge_first"])
```

or set `SKIDL_LAYOUT_CANDIDATES="baseline,connector_edge_first"` in the
environment (an explicit `candidate_names=` always wins). Two strategies is
roughly 4× faster than the full set; leave it unset for the final board.

When you don't know which strategies fit the board, cap the count instead with
`plan_layout(circuit, max_candidates=2)` (or `SKIDL_LAYOUT_MAX_CANDIDATES=2`):
each candidate's *seed* placement is quick-scored and only the top N are
refined. The seed score is a heuristic predictor, not the refined quality — use
it for iteration, not the final board. `max_candidates` composes with
`candidate_names` (filter first, then cap).

To keep the full breadth but spend less wall-clock, the unique candidates are
refined concurrently. **This is the default on boards of 30 parts or more**
(`min(4, cpu_count)` workers) — no knob needed. The parallel path covers
**both** heavy phases — the pass-1 refinement trio and the finalize /
post-anchor pass — each running the unique candidates over a picklable snapshot
of the circuit, so the **output is identical to the sequential default** — this
is a speed knob, not a quality one — and any worker/pickling/subprocess error
falls back silently to the sequential path.

- **Kill switch:** `plan_layout(circuit, parallel_workers=1)` (or
  `SKIDL_LAYOUT_PARALLEL=1`) forces the fully sequential path.
- **Override:** `parallel_workers=N` / `SKIDL_LAYOUT_PARALLEL=N` sets the worker
  count explicitly and is honored regardless of part count. Precedence is
  explicit kwarg > env > implicit default; only a resolved value `>= 2` (with
  `>= 2` unique candidates) engages parallelism.
- **No `__main__` guard required.** Workers are plain
  `python -m skidl_layout._worker_main` subprocesses that never re-import the
  calling script (round-7 WS25), so unguarded driver scripts are safe.
- Per-ref `progress` lines are suppressed in parallel mode (candidate-level
  messages still emit).

`parallel_workers` composes with `candidate_names` / `max_candidates` (the cap
happens first, then the survivors refine in parallel).

When redirecting output, run with `python -u` and pass a `progress=` callback
(below) so a long placement stays observable.

## Layout-quality metrics

`skidl_layout.metrics` scores a placement quantitatively (adapted from the
upstream `benchmarks/evaluate_layout.py` + `score.py`):

```python
from skidl_layout import evaluate_circuit
m = evaluate_circuit(circuit)      # LayoutMetrics
print(m.layout_ok, m.overlaps, m.outline_violations,
      m.missing_refs, m.hpwl_total_mm, m.part_count_placed, m.layout_score)  # 0-100
```

- `evaluate_circuit(circuit, *, write_pcb_path=...)` — plan a layout and return
  a `LayoutMetrics` (overlaps, outline violations, missing refs, total HPWL,
  `part_count_placed`, plus a 0-100 `layout_score`); optionally emit a `.kicad_pcb`.
- **Already have a `LayoutResult`?** Pass it to avoid a second placement pass:
  `evaluate_circuit(circuit, result=plan_layout(circuit))`, or use the pure
  mapping `metrics_from_result(result)` directly. The 0-100 grade is a pure
  function of the planned result, so this is exact.
- `evaluate_circuit_dir(dir)` / `python -m skidl_layout.metrics <dir>` — execute
  a generated `<dir>/circuit.py`, score its layout, and write `layout_score.json`
  (+ `board.kicad_pcb`). KiCad symbol/footprint libraries are auto-discovered.
- `summary_table(rows)` — markdown scoreboard for a batch of boards.

### Two scores, one board

There are two distinct 0-100 numbers and they measure different things — do not
compare them:

- **`LayoutScore.score`** (from `score_placement`) is `max(0, 100 - penalty)`,
  the *internal search objective*. Its soft penalties (HPWL, crossings,
  congestion, warnings) saturate past 100 on dense boards, so a perfectly legal
  layout routinely clamps to `0.0`. Read **`LayoutScore.penalty`** (raw,
  unclamped) for the actual gradient the local search optimizes — a lower
  penalty is strictly better even when both scores read `0.0`.
- **`LayoutMetrics.layout_score`** (from `evaluate_circuit`) is a separate
  *structural rubric* (overlaps/violations/HPWL rolled into a grade). A clean
  board reads `100.0` here.

So the same board can legitimately report `score=0.0` (saturated search
objective) and `layout_score=100.0` (clean rubric) at once.

## Optional external tools

- **KiCad `kicad-cli`** — DRC feedback (`validator.run_kicad_drc`,
  `find_kicad_cli`). Skipped if not found.
- **KiCadRoutingTools (KRT)** — routability feedback (`krt.evaluate_routability`,
  `krt.route_and_check`, `krt.find_krt`). Request-only, never called from
  `plan_layout`. The checkout is discovered by path at runtime — explicit arg,
  env `SKIDL_LAYOUT_KRT_DIR`, or the workspace sibling `../KiCadRoutingTools` —
  and its `route.py` / `check_connected.py` / `check_drc.py` CLIs are invoked via
  `subprocess` (not imported, installed, or vendored). Skipped if not found
  (`find_krt()` returns `None`; `route_and_check` raises `KrtNotFoundError`).
  Populates the existing `RoutabilityFeedback` / `LayoutResult.routability` slot.

  > **Freerouting was never wired in and has been dropped.** The earlier
  > Freerouting-Java idea was never implemented; KRT replaces it. No Java, no
  > Freerouting install, and no new Python dependencies are required.

## Tests

```bash
pytest
```

Ported from the upstream `test_layout_*` suite. Two upstream test modules
(`test_layout_feedback.py`, `test_layout_polish.py`) were **not** lifted: they
depend on the upstream power-integrity `skidl.sim` layer, which is out of scope
for this layout-only package.

## Status

A standalone peer package derived from the MIT-licensed `layout/` snapshot; see
[`AUTHORS.md`](AUTHORS.md) and [`LICENSE`](LICENSE) for provenance and terms.
