# Circle-Pack + Module-Coupling Layout — Design

**Date:** 2026-07-10
**Scope:** `project/src/ariadne_graph/web/static/index.html` (file/scope view only). No backend change.
**Supersedes:** the radial "hub-satellite / sunburst" force layout for the file view.

## Problem

The radial layout (depth = radius, weight = angle) forces file nodes onto concentric
shells, producing "rings of circles" rather than a per-directory territorial partition.
It shows neither true weight distribution nor a clean ideal-vs-reality read. Circle-packing
shows the *ideal* structure (weighted, nested), but on its own it hides the coupling reality
the user wants to see: which modules are actually entangled.

## Model: two layers

**Rest state (ideal).** `d3.pack()` on the directory tree. Every directory is a circle with
area ∝ its file count, nested by hierarchy; files are leaf circles inside. Each node gets a
frozen **home** position `(hx, hy)` and radius from the pack.

**Reality distortion (overlay).** Top-level module circles are **rigid bodies**: each module's
entire packed subtree (sub-dirs + files) is frozen relative to the module center and travels
as one unit. A force simulation runs on **only the ~8 top-level module centers**:

- **Coupling attraction** — for each pair of top-level modules, an attractive force ∝ the
  number of cross-module dependency edges between them (aggregate coupling). Entangled modules
  pull together.
- **Collision** — module circles can't overlap (pack radius + margin); independent modules
  push apart.
- **Home spring** — a weak pull back to each module's packed position, so the arrangement stays
  on-screen and there is a stable "ideal" to compare against.

A **coupling slider (0–100%)** scales coupling force vs. home spring: 0 = pure pack (ideal),
100 = full module-coupling distortion (reality).

## Data flow (per scope, in `renderScope`)

1. **Tree** — build nested `{name, children}` from file paths → `d3.hierarchy().sum(leaf=1)` →
   `d3.pack().padding(k)`. Store each node's home `(hx, hy)` and `r`.
2. **Modules** — depth-1 pack nodes. For each: center, radius `R`, and descendants with a
   **frozen offset** `(ox, oy) = (node.hx − module.hx, node.hy − module.hy)`.
3. **Aggregate coupling** — map each dep edge's endpoints to their top-level module; count
   cross-module pairs → `moduleLinks {source, target, weight}`. Same-module edges ignored.

**Simulate (~8 bodies):** `forceLink(moduleLinks)` strength ∝ weight × slider; `forceCollide(R+margin)`;
`forceX/Y(home)` strength = (1 − slider).

**Render each tick:** for every node, `x = module.x + ox`, `y = module.y + oy`. Moving a module
center rigidly translates its whole subtree. Files/sub-dirs never compute their own physics —
only ~8 module centers are simulated, so it is fast and cannot re-ball toward center.

## Rendering

- **Directory circles** — faint fill tinted by top-level module hue (depth-1 ~12% opacity,
  deeper less), thin `#3a4256` stroke. Nested-territory look.
- **File leaves** — filled by **hygiene** color (green/blue/amber/red). Draw order = pack depth
  ascending so children render inside parents.
- **Directory stroke** may carry its worst-level tint so unhealthy modules read at dir level.
- **Labels** — top-level module names always shown; sub-dir labels on hover / past a zoom
  threshold (existing dense-label rule).
- **Dependency edges** — hidden by default (all 1866 at once buries the pack). On hover/select
  of a file or dir, draw only that node's edges as arcs to their targets (trace one at a time).
  A hovered edge still paints red if it is an up/cross-tree layering violation. Module-level
  coupling is already shown structurally by how close modules drift.

## Interaction & controls

- **Drill-down = zoom-to-fit** (d3-pack idiom). Click a directory circle → transition
  `zoom.transform` so it fills the viewport. The whole tree is always laid out; drilling is
  camera movement (instant). Breadcrumbs drive zoom targets. Click empty space / breadcrumb to
  zoom out.
- **Retire the Depth dropdown** — packing shows all levels; zoom handles density.
- **Coupling slider (0–100%)** replaces the Soft/Rigid toggle. Dragging re-runs only the 8-body
  sim and re-fits.
- **Names auto/on/off**, **Reset view**, repo picker, hygiene legend, detail panel — preserved.
- **Fit** — two-shot (early + settle); 8 bodies settle fast.

## Testing (mirrors `tests/test_web/layout_invariants.mjs`)

On a synthetic lumen_platform-shaped tree, each asserted RED-without / GREEN-with where it
guards a real mechanism:

1. **Containment** — every leaf circle fully inside its directory circle; every child dir fully
   inside its parent.
2. **Weight distribution** — top-level module area ∝ its file count (the core ask).
3. **Rigid body** — after shifting a module center by (dx,dy), every descendant's rendered
   position shifts by exactly (dx,dy).
4. **Coupling aggregation** — cross-module edge counts roll up correctly; same-module edges
   contribute zero drift.
5. **Slider bounds** — at 0, module centers sit on home positions (pure ideal); at 1, home
   spring ≈ 0 so coupling dominates.

## Rollout

Swap the `hasHubs` branch in `index.html`. Folder (top-level) view unchanged. Update the harness.
Verify live on lumen_platform: slide coupling 0→1, drill a module, hover a file's edges; zero
console errors. No backend change.

## Decisions & rationale

- **Coupling acts at module level, not per-file** — files stay in their packed slots (structure
  never breaks); directories drift by aggregate coupling. Keeps the pack readable while showing
  the real signal.
- **Only top-level modules drift** — their internal packing stays rigid. Avoids the fiddly
  nested-constraint physics of drifting every directory, and reads as "which organs are
  entangled."
- **Replace, not coexist** — packing subsumes what the ring layout reached for. Keeping the old
  force path as a toggle is speculative flexibility; remove it.

## Risk

At full zoom-out, 1900 file leaves are tiny — expected; drill to read (matches the approved
prototype).
