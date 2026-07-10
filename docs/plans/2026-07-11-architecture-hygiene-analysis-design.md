# Architecture Hygiene Analysis — Design

**Date:** 2026-07-11
**Status:** Approved, ready for TDD implementation

## Problem

The code graph resolves file→file `dep` edges and a directory tree, but the only
structural intelligence it produces is a per-edge layering-violation boolean
computed *live in the web layer* (`web/queries.py::_is_violation`). It is
ephemeral: not queryable via MCP, not tracked across re-indexes, recomputed on
every request. We want the graph to answer: **is this repo well-abstracted?** —
where well-abstracted means files import from siblings or lower, never deep into
another directory's internals, no cycles, no dead orphans.

## Goal

One analysis pass that consumes the resolved graph and emits findings as
`CodeDiagnostic` nodes — the existing node kind already surfaced by MCP, rolled
up per-directory by the web view, and painted in the browser. **One producer,
three existing consumers. No new node kind, no new schema, no new MCP tool.**

## Placement

New file **`core/architecture.py`**, a *graph-level* pass, sibling to
`core/diagnostics.py`.

Why not extend `DiagnosticsCollector`: every existing rule there
(`unused_import`, `complex_function`, …) is **AST-local**, computed *during
extraction* inside the language extractors, before cross-file references are
resolved. Cycles / orphans / deep imports are **whole-graph** properties needing
the complete resolved `dep` edge set + directory tree, which only exists *after*
the store is populated. It emits the same `CodeDiagnostic` type, so it is a
second *producer* into the existing consumer chain — not a new pipeline.

Runs **after** dep-edge resolution in the index flow (post-build hook, after
extractors + SCIP resolution complete).

## The rules (v1)

`analyze(files, dep_edges) -> list[CodeDiagnostic]` — a **pure** function
(`(files, edges) → findings`, no store, no I/O, no async). Builds a directed dep
graph and a directory tree once, then:

1. **`dependency_cycle`** (error) — Tarjan SCC over the file dep graph. Any SCC
   with >1 file → emit one diagnostic per member, `properties.cycle = [ring]`.
   A well-abstracted repo is a DAG; empty result is the headline metric.
2. **`deep_import`** (warning) — the current `_is_violation` rule, moved here,
   run over every dep edge: source reaches into a *different* organ's internals
   (target dir below its organ root). `properties`: `from`, `to`, `organ`.
3. **`orphan_module`** (info) — fan-in 0 (nothing imports it), *unreferenced*
   flavor only (a sink that imports nothing is a normal leaf util — not
   flagged). Suppressed via name/path heuristics (below).
4. **`upward_import`** (warning) — intra-organ direction inversion: a file
   importing from a strictly *higher* level in its own subtree (child reaching
   back up to a parent module). Distinct from `deep_import` (cross-organ reach).

### Orphan suppression

Name/path heuristics, no config surface. Suppress files matching entry-point
patterns `{__main__, __init__, cli, conftest, main, setup}` and files in
peripheral organs. `_PERIPHERAL_ORGANS` **moves from `web/queries.py` to
`core/architecture.py`** (it is an architectural fact, not a web fact);
`web/queries.py` imports it from there. Kills the duplication rather than
forking it.

## Data flow

```
index pipeline (after dep edges resolved)
        │
        ▼
core/architecture.py :: analyze(files, dep_edges)   [pure]
    ├─ build dir tree + directed dep graph  (once)
    ├─ Tarjan SCC ──────────► dependency_cycle
    ├─ per-edge organ check ─► deep_import   (was _is_violation)
    ├─ per-edge level check ─► upward_import
    └─ fan-in==0 − suppress ─► orphan_module
        │
        ▼  list[CodeDiagnostic]   (same type diagnostics.py emits)
        │
    persisted as CodeDiagnostic nodes (existing path):
      node labels ["CodeDiagnostic"], HAS_DIAGNOSTIC edge to the CodeFile node,
      properties MUST include file_path  (web rollup reads $.file_path)
        │
        ├──► MCP: code_graph_retrieve surfaces them
        ├──► web dir rollup: existing _worse() rolls them up per directory (free)
        └──► browser: paints nodes by reading CodeDiagnostic (no compute)
```

**Critical schema note:** architecture findings are file-level and attach to the
`CodeFile` node id, but `web/queries.py` reads
`json_extract(properties, '$.file_path')` off diagnostic nodes. So each emitted
diagnostic **must set `properties.file_path`** for the existing rollup to pick
it up.

## Consequences / deletions

- `web/queries.py::_is_violation` and its live per-edge computation become
  redundant → deleted. The web view stops *computing* violations and just
  *reads* the persisted `deep_import` diagnostics like every other rule.
- `web/queries.py` imports `_PERIPHERAL_ORGANS` from `core/architecture.py`.

## Testing (TDD — failing hard-case first, per repo rule)

Pure `analyze` makes hard cases trivial with hand-built edge lists. Each rule
gets a red-then-green test on the input a naive implementation gets wrong:

- **cycle**: `a→b→c→a` caught; `a→b→c` (linear DAG) produces **zero** — the test
  a naive "any back-reference" impl fails.
- **deep_import**: `src/x → tests/unit/y` flagged; `src/x → tests` (front door)
  **not**.
- **orphan**: unreferenced `helper.py` flagged; unreferenced `cli.py` **not**
  (suppression works).
- **upward_import**: `pkg/sub/leaf.py → pkg/core.py` (up) flagged;
  `pkg/core.py → pkg/sub/leaf.py` (down) **not**.

## Explicitly NOT in v1 (YAGNI)

Fan-in/out ranking report, Martin instability/abstractness metrics,
misplaced-file mover, god-directory detection (would consume `communities.py`),
new MCP tool, config surface. These are *rankings*, not *violations* — different
UX (sortable report, not a red node). Defer until the four boolean rules prove
out.
