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
as one unit. Once modules drift apart, an enclosing repository-root circle is geometrically
impossible — the model is a **forest of top-level module territories, not one enclosing root
circle**. A force simulation runs on **only the ~8 top-level module centers**:

- **Coupling attraction** — for each pair of top-level modules, an attractive force whose
  *target distance is radius-aware*: `distance = R_a + R_b + gap(weight)`, where a strongly-
  entangled pair gets a small gap and a weak pair keeps whitespace. Distance-40 style links are
  WRONG here (radii are hundreds of px → centers would coincide → everything compresses to a
  central touching-cluster, the exact failure this redesign avoids).
- **Collision** — module circles can't overlap (`R + margin`); independent modules push apart.
- **Home spring** — a **residual** pull back to each module's packed home:
  `homeStrength = 0.1 + 0.9 * (1 − slider)`. It never reaches 0, so even at full coupling the
  arrangement keeps its high-level orientation (preserves the mental map) rather than rotating
  into a generic force blob. The endpoint is therefore labelled **"strong coupling"**, not
  "literal reality" — a force layout is a distortion cue, not ground truth.

**Coupling metric = entanglement, normalized:** `weight = crossEdges / sqrt(nA * nB)` (nX = file
count of module X). This surfaces modules that are *disproportionately* entangled independent of
size — a small module tightly bound to a big one stands out — rather than raw dependency volume,
where big modules always look most coupled.

A **coupling slider (0–100%)** scales the coupling force vs. the residual home spring.
**Slider = 0 is an explicit static layout:** bypass the simulation entirely, set every module to
its packed home, render once. (Collision at `R + margin` demands more clearance than
`pack().padding(3)` provides, so leaving the sim running at 0 would still nudge modules off
home — violating the "pure ideal" endpoint.)

## Data flow (per scope, in `renderScope`)

1. **Tree** — build nested `{name, children}` from file paths → `d3.hierarchy().sum(leaf=1)` →
   `d3.pack().padding(k)`. Store each node's home `(hx, hy)` and `r`.
2. **Modules** — the depth-1 **directory** pack nodes, PLUS a synthetic `(root files)` module
   holding any files directly under the scope root (they are depth-1 leaves with no owning
   directory module). The synthetic pack **root** node is NOT a module and is NOT rendered.
   For each module: center, radius `R`, and descendants with a **frozen offset**
   `(ox, oy) = (node.hx − module.hx, node.hy − module.hy)`.
3. **Coupling** — map each dep edge's endpoints to their top-level module; skip same-module
   edges; accumulate `crossEdges` per unordered module pair; normalize →
   `moduleLinks {source, target, weight = crossEdges / sqrt(nA*nB)}`.

**Simulate (~8 bodies), slider > 0 only:**
`forceLink(moduleLinks).distance(l => l.source.R + l.target.R + gap(l.weight)).strength(l => couple(l.weight) * slider)`;
`forceCollide(R + margin)`; `forceX/Y(home).strength(0.1 + 0.9*(1 − slider))`.
Render (all pack nodes except the root): `x = module.x + ox`, `y = module.y + oy`.

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
- **Fit** — two-shot (early + settle); 8 bodies settle fast. **`fitView` must bound by circle
  extent, not centers:** `minX = min(node.x − node.r)`, `maxX = max(node.x + node.r)`, etc.
  With pack radii of hundreds of px, center-only bounds clip the largest modules.

## Testing (mirrors `tests/test_web/layout_invariants.mjs`)

On a synthetic lumen_platform-shaped tree, each asserted RED-without / GREEN-with where it
guards a real mechanism:

1. **Containment** — every leaf circle fully inside its directory circle; every child dir fully
   inside its parent.
2. **Weight (relaxed, not exact proportionality)** — leaf weights are equal per file; a module
   with more files is *generally* larger than one with fewer. We do NOT assert exact
   area ∝ file-count: with a single recursive `d3.pack`, an enclosing directory's radius also
   depends on packing efficiency and subtree shape, so equal-file-count modules with different
   nesting legitimately differ in radius. (Exact proportionality would need a two-stage layout —
   out of scope unless it becomes a hard requirement.)
3. **Rigid body** — after shifting a module center by (dx,dy), every descendant's rendered
   position shifts by exactly (dx,dy).
4. **Coupling aggregation** — cross-module edges roll up per module pair and normalize
   (`crossEdges / sqrt(nA*nB)`); same-module edges contribute zero.
5. **No overlap after distortion** — after the sim settles at slider=1, no two module circles
   overlap (collision holds).
6. **Slider endpoints** — at slider=0 the sim is bypassed and every module sits exactly on its
   home (assert the behavior, not force-strength values); at slider=1 the residual home spring
   is `0.1` (nonzero) so coupling dominates but orientation is preserved.

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
- **Radius-aware coupling distance, static slider-0, residual home spring, forest-not-root,
  radii-in-fitView** — five mechanism corrections from review (2026-07-10). Without the first,
  high coupling compresses to a central touching-cluster; without static slider-0, the "ideal"
  endpoint isn't actually the pack; without the residual spring, full coupling loses the mental
  map; without forest handling, the synthetic root breaks the tick formula and can't contain
  drifted modules; without radii in fit, big modules clip.
- **Entanglement over volume** — `weight = crossEdges / sqrt(nA*nB)` so size doesn't inflate
  coupling; surfaces disproportionately-entangled modules.
- **Area is NOT exactly proportional to file count** — single `d3.pack` makes leaf *area*
  weight-proportional, but enclosing-dir radius also depends on packing shape. Promise/test
  relaxed to "larger file-count → generally larger + contained + non-overlapping."

## Risk

At full zoom-out, 1900 file leaves are tiny — expected; drill to read (matches the approved
prototype).
