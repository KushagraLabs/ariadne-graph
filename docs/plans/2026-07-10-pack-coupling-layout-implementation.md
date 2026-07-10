# Circle-Pack + Module-Coupling Layout — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the radial ring layout in the graph file-view with `d3.pack()` (weighted, nested) as the ideal rest state, plus a ~8-body force sim on top-level module centers that drift by aggregate cross-module coupling.

**Architecture:** All changes are in one file, `project/src/ariadne_graph/web/static/index.html` (inline JS), served fresh each request (no build step — a browser reload picks up edits). Pure layout math is guarded by a Node harness at `project/tests/test_web/layout_invariants.mjs` (run with `node`, red/green). Visual + interaction behavior is verified live in-browser (inject/reload → screenshot → assert numerically via the page's own sim state). No backend change.

**Tech Stack:** D3 v7 (vendored `web/static/d3.min.js`, already includes `d3.pack`/`d3.hierarchy`/`d3.zoom`/`d3.forceSimulation`). Node 24 for the test harness. Chrome MCP tools for live verification.

**Design:** `docs/plans/2026-07-10-pack-coupling-layout-design.md`

---

## Preconditions

- Daemon serving at `http://127.0.0.1:8848/graph` (launchd; if wedged: `launchctl kickstart -k gui/$(id -u)/com.ariadne.mcp`, wait ~12s to bind).
- Current `index.html` `hasHubs` branch is the radial layout being replaced. Keep the non-hub (folder top-level) branch untouched.
- Work on branch `feat/browser-graph-view-and-index-hygiene` (already checked out).

---

## Task 1: Build the pack tree + home positions (pure math)

**Files:**
- Modify: `project/src/ariadne_graph/web/static/index.html` (the `renderScope`/`render` layout section)
- Test: `project/tests/test_web/pack_layout_invariants.mjs` (new)

**Step 1: Write the failing test** — new harness that mirrors the pack-tree builder + `d3.pack`. Since the harness has no DOM/d3, vendor a minimal pack? No — instead extract the tree-builder (pure) and assert tree shape; assert containment against d3 by loading d3 in Node. Check first whether `d3.min.js` loads under Node:

Run: `node -e "const d3=require('./project/src/ariadne_graph/web/static/d3.min.js'); console.log(typeof d3.pack)"`
Expected: prints `function` (UMD export works) OR errors (browser-only build).

- If it loads: harness uses real `d3.pack` and asserts **containment** (every leaf inside its dir, every child dir inside parent) and **weight ∝ area** (top-level module area ratio ≈ file-count ratio within tolerance).
- If it does NOT load under Node: the harness asserts only the pure tree-builder (correct nesting, leaf counts, offsets); containment/weight are verified live in-browser in Task 6 instead. Document which path was taken in the harness header.

**Step 2: Run to verify it fails**

Run: `node project/tests/test_web/pack_layout_invariants.mjs`
Expected: FAIL (builder not yet extracted / assertions unmet).

**Step 3: Implement the tree-builder + pack in `index.html`**

Replace the body of the `hasHubs` branch's layout with: build nested `{name, children, _file?}` from `data.files` paths relative to scope; `d3.hierarchy(root).sum(d=>d.value||0).sort(...)`; `d3.pack().size([S,S]).padding(3)`. Store `d.hx=d.x, d.hy=d.y` (home) on each pack node. Keep node objects carrying `{kind, worst, path, label, dir}` so existing color/label/detail code still works.

**Step 4: Run harness to verify pass**

Run: `node project/tests/test_web/pack_layout_invariants.mjs`
Expected: ALL PASS (containment + weight, or builder-only per the Node/d3 decision).

**Step 5: Commit**

```bash
git add project/src/ariadne_graph/web/static/index.html project/tests/test_web/pack_layout_invariants.mjs
git commit -m "feat(web): circle-pack tree + home positions for file view"
```

---

## Task 2: Identify modules + freeze subtree offsets (rigid bodies)

**Files:** Modify `index.html`; extend `pack_layout_invariants.mjs`.

**Step 1: Failing test** — RIGID-BODY invariant: after shifting a module center by (dx,dy), every descendant's rendered position = home + (dx,dy). In the harness: build modules, pick one, shift its center, recompute `x=module.x+ox`, assert every descendant moved by exactly (dx,dy).

**Step 2: Run → FAIL** (offsets not computed yet).

**Step 3: Implement** — collect depth-1 pack nodes as `modules`. For each module, for every descendant node set `ox = node.hx - module.hx`, `oy = node.hy - module.hy`, and tag `node.moduleId`. Store module `{id, hx, hy, R=node.r}`.

**Step 4: Run → PASS.**

**Step 5: Commit** `feat(web): freeze module subtree offsets as rigid bodies`

---

## Task 3: Aggregate cross-module coupling

**Files:** Modify `index.html`; extend harness.

**Step 1: Failing test** — COUPLING-AGGREGATION: given synthetic dep edges, cross-module pairs roll up to weighted `moduleLinks`; same-module edges contribute 0.

**Step 2: Run → FAIL.**

**Step 3: Implement** — map each dep edge (`data.edges`, source/target file paths → node ids → `moduleId`). Skip same-module. Accumulate `moduleLinks` keyed by unordered `{a,b}` with `weight = count`. (Reuse existing `byPath` map.)

**Step 4: Run → PASS.**

**Step 5: Commit** `feat(web): aggregate cross-module coupling into module links`

---

## Task 4: The 8-body force sim + rigid render

**Files:** Modify `index.html`; extend harness.

**Step 1: Failing test** — SLIDER-BOUNDS: at slider=0, module centers equal home (pure ideal); at slider=1, home-spring strength≈0 (coupling dominates). Assert the force config, not a full sim run (deterministic config check).

**Step 2: Run → FAIL.**

**Step 3: Implement** — replace the current `d3.forceSimulation(nodes)` with a sim over **module centers only**:
```
sim = d3.forceSimulation(modules)
  .force("couple", d3.forceLink(moduleLinks).id(m=>m.id).distance(40)
      .strength(l => coupleStrength(l.weight) * slider))
  .force("collide", d3.forceCollide(m => m.R + 8))
  .force("hx", d3.forceX(m=>m.homeX).strength(1 - slider))
  .force("hy", d3.forceY(m=>m.homeY).strength(1 - slider));
```
On tick: `for each node: node.x = module.x + node.ox; node.y = module.y + node.oy`. Draw circles from ALL pack nodes (not just modules). `state.slider` defaults 0.35.

**Step 4: Run harness → PASS. Then live check:**
- Reload browser, drive to `lumen_platform`, `await settle`, screenshot.
- Assert via injected JS: module count ~8, every file `hypot(x-(mod.x+ox)) < 1` (rigid), no NaN.
Expected: packed modules visible, files inside their dir circles.

**Step 5: Commit** `feat(web): 8-body coupling sim with rigid-body module render`

---

## Task 5: Rendering polish — hygiene color, tinted dirs, labels

**Files:** Modify `index.html`.

**Step 1–3: Implement** (visual — verified live, no unit test):
- File leaves filled by `LEVEL_COLOR[worst]`; dir circles faint module-hue fill + `#3a4256` stroke; dir stroke tinted by worst-level.
- Draw order = pack `depth` ascending.
- Top-level module labels always; sub-dir labels on hover / zoom threshold (reuse dense-label logic).
- Keep `<title>` tooltips, detail panel, hygiene legend.

**Step 4: Live check** — screenshot; assert legend colors present, module labels rendered, zero console errors (`read_console_messages onlyErrors`).

**Step 5: Commit** `feat(web): hygiene-colored leaves + tinted nested dir territories`

---

## Task 6: Edges on hover/select + layering-violation paint

**Files:** Modify `index.html`.

**Step 1–3: Implement** — edges hidden by default. On node hover/click, draw only that node's dep edges as arcs to targets; a hovered edge paints red (`VIOL`) if `isViolation(dirA,dirB)`. Reuse existing `isViolation`/`treeDistance`. Clear on mouseleave/deselect.

**Step 4: Live check** — hover a known-coupled file; assert its edges appear (count>0) and disappear on leave; a cross-module edge is red.

**Step 5: Commit** `feat(web): trace a node's dependency edges on hover`

---

## Task 7: Zoom-to-drill + coupling slider + retire Depth/Soft toggle

**Files:** Modify `index.html` (header markup + handlers).

**Step 1–3: Implement:**
- Click a dir circle → `zoom.transform` transition to fit that circle's bounds (compute from `d.x,d.y,d.r` + current sim offset). Click empty space / breadcrumb → zoom out. Breadcrumbs drive zoom targets.
- Header: replace **Soft coupling** button + **Depth** dropdown with a **coupling range slider** (`<input type=range 0..100>`, label "Coupling: ideal↔real"); `oninput` sets `state.slider`, re-runs the 8-body sim (`sim.alpha(0.6).restart()`), re-fits. Keep Names, Reset, repo picker.
- Remove now-dead code paths: `MAX_DEPTH`/`maxDepth()` control, drill-by-refetch, sunburst/hub layout remnants in the `hasHubs` branch. Keep the flat folder view branch.

**Step 4: Live check** — drag slider 0→100 (modules spread→cluster by coupling); click a module (zooms in); Reset (zooms out); zero console errors. Screenshot at slider 0 and slider 1 to show ideal-vs-real.

**Step 5: Commit** `feat(web): zoom-to-drill + coupling slider, retire depth/soft toggle`

---

## Task 8: Update the harness header + final full verification

**Files:** Modify `pack_layout_invariants.mjs`; optionally retire `layout_invariants.mjs` (old ring layout gone).

**Step 1–3:**
- Ensure `pack_layout_invariants.mjs` header documents the invariants (containment, weight, rigid-body, coupling aggregation, slider bounds) and the run command.
- Delete `layout_invariants.mjs` (guards removed ring code) OR keep if any shared helper is still used — check first with `grep`.

**Step 4: Full verification:**
- `node project/tests/test_web/pack_layout_invariants.mjs` → ALL PASS.
- Live on lumen_platform: slider 0 and 1 screenshots, drill a module, hover a file's edges, zero console errors.
- `python -m pytest project/tests/test_web -q` (query-layer tests unaffected) → pass.

**Step 5: Commit** `test(web): pack-layout invariants; retire ring-layout harness`

---

## Rollback

Each task is one commit; `git revert` any. The whole feature is one file + one harness — reverting to the ring layout is `git checkout <pre-Task-1> -- project/src/ariadne_graph/web/static/index.html`.

## Notes for the implementer

- **No build step:** edit `index.html`, reload the browser tab — changes are live. Static file served by `routes.py`.
- **Live verify pattern:** drive the page via its own functions (`selectRepo`, `showFiles`) injected through the Chrome `javascript_tool`, `await` a settle, then read `state.sim.nodes()` for numeric assertions and screenshot for the visual. This has been the reliable loop all session.
- **Daemon caveat:** `/api/graph/*` can be slow (~20s) during an active auto-sync cycle (store-lock contention), fast (~0.3s) otherwise — not a failure; wait it out.
- **DRY:** reuse existing `LEVEL_COLOR`, `DIR_PALETTE`/`dirColor`, `isViolation`, `treeDistance`, `byPath`, dense-label logic, two-shot `fitView`. Do not reimplement.
