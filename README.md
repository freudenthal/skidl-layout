# skidl-layout

A PCB **placement / layout** engine for [SKiDL](https://github.com/devbisme/skidl)
circuits, packaged as a standalone peer tool.

This is a code-only snapshot of the `layout/` package from
[`lachlanfysh/skidl`](https://github.com/lachlanfysh/skidl) (commit
`11e45996`), lifted into its own package so it can be used as an external tool
over a SKiDL `Circuit` — matching the "keep placement/routing/sim external,
driven via netlist/CLI/MCP" direction from
[devbisme/skidl #315](https://github.com/devbisme/skidl/discussions/315). See
[`AUTHORS.md`](AUTHORS.md) for full provenance.

It is **complementary** to the circ-synth schematic path: circ-synth emits a
schematic + netlist; `skidl-layout` consumes the same SKiDL `Circuit` and plans
a board placement.

## What it does

- Classifies parts (roles), infers placement intent, and places footprints on a
  board outline scored by congestion / routability / power topology.
- Emits a `.kicad_pcb` (footprint placement, no copper) via `write_kicad_pcb`.
- Optionally invokes `kicad-cli` DRC and a Freerouting pass for routability
  feedback (both **external tools**, feature-gated, invoked via `subprocess` —
  not Python dependencies).

## Install

```bash
pip install -e .            # core (skidl + simp_sexp)
pip install -e ".[shapely]" # exact polygon containment in the validator
pip install -e ".[test]"    # pytest
```

## Usage sketch

```python
from skidl import Circuit          # build/import a SKiDL circuit
from skidl_layout import plan_layout, write_kicad_pcb

result = plan_layout(circuit)      # LayoutResult: scored placement, no copper
# result.placed_parts -> write_kicad_pcb(...) to emit a .kicad_pcb
```

`plan_layout(circuit, ...)` reads only `circuit.parts` and duck-typed `Part`
attributes (defensively, via `getattr`) — the same loop-boundary shape the
circ-synth SPICE path uses.

## Layout-quality metrics

`skidl_layout.metrics` scores a placement quantitatively (adapted from the
upstream `benchmarks/evaluate_layout.py` + `score.py`):

```python
from skidl_layout import evaluate_circuit
m = evaluate_circuit(circuit)      # LayoutMetrics
print(m.layout_ok, m.overlaps, m.outline_violations,
      m.missing_refs, m.hpwl_total_mm, m.layout_score)  # 0-100
```

- `evaluate_circuit(circuit, *, write_pcb_path=...)` — plan a layout and return
  a `LayoutMetrics` (overlaps, outline violations, missing refs, total HPWL,
  placed-part count, plus a 0-100 `layout_score`); optionally emit a `.kicad_pcb`.
- `evaluate_circuit_dir(dir)` / `python -m skidl_layout.metrics <dir>` — execute
  a generated `<dir>/circuit.py`, score its layout, and write `layout_score.json`
  (+ `board.kicad_pcb`). KiCad symbol/footprint libraries are auto-discovered.
- `summary_table(rows)` — markdown scoreboard for a batch of boards.

## Optional external tools

- **KiCad `kicad-cli`** — DRC feedback (`validator.run_kicad_drc`,
  `find_kicad_cli`). Skipped if not found.
- **Freerouting** (external Java) — routability feedback. Feature-gated.

## Tests

```bash
pytest
```

Ported from the upstream `test_layout_*` suite. Two upstream test modules
(`test_layout_feedback.py`, `test_layout_polish.py`) were **not** lifted: they
depend on the upstream power-integrity `skidl.sim` layer, which is out of scope
for this layout-only package.

## Status

Local, unpublished peer package. Not pushed anywhere; no PR to devbisme/skidl or
lachlanfysh/skidl without explicit maintainer permission.
