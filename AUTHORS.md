# Authors & Provenance

## `skidl_layout/` — PCB placement/layout engine

The entire `skidl_layout/` package is a **code-only snapshot** of the
`src/skidl/layout/` package authored by **Lachlan Fysh**, taken from:

- **Repo:** https://github.com/lachlanfysh/skidl
- **Branch:** `feat/overnight-product-layer`
- **Pinned commit:** `11e45996a896276b78137e4e06b9045f20733b7d`
  ("Avoid RF intent on generic module sockets", 2026-06-17)

The snapshot excludes Lachlan's ~300-commit history and generated benchmark-board
artifacts; only the layout code and its unit tests were lifted. The package was
renamed `skidl.layout` → `skidl_layout` and repackaged as a peer package that
depends on `skidl` as a library (see `pyproject.toml`). No layout source lines
were modified in the lift beyond the package rename (the code uses relative
imports internally and reaches skidl core only via `skidl.net.NCNet` and
`skidl.node.HIER_SEP`).

Original SKiDL authorship (the `skidl` dependency): Dave Vandenbout.

## `skidl_layout/metrics.py`

Adapted from Lachlan Fysh's `benchmarks/evaluate_layout.py` and
`benchmarks/score.py` (same repo/commit as the engine above). Reworked to drive
this package's high-level `plan_layout` entry (which already computes HPWL) and
to auto-discover KiCad libraries instead of hardcoded `/usr/share/kicad` paths.

## Packaging / integration

- John Freudenthal — peer-package scaffolding, the `metrics` adaptation, and
  circuit-synth integration.

Both the upstream SKiDL project and Lachlan's fork are MIT-licensed; see
`LICENSE`.
