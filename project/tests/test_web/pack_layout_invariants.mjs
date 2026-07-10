// Regression guard for the circle-PACK graph layout in web/static/index.html.
//
//   Run:  node project/tests/test_web/pack_layout_invariants.mjs   (exit 0 = pass)
//
// The layout math lives in inline <script> (no JS test runner in this repo), so
// the pure tree-builder (`buildPackTree`) is mirrored here VERBATIM and fed the
// SAME node shape `renderScope` produces (dir hubs + file leaves carrying `dir`).
// The vendored d3.min.js loads under plain Node, so this exercises the GENUINE
// d3.hierarchy + d3.pack geometry (no browser needed for the math).
//
// It locks the invariants that make the pack read as a nested HIERARCHY:
//   1. CONTAINMENT — every file leaf sits inside its directory circle, and every
//      child directory sits inside its parent directory circle (no leakage).
//   2. RELAXED WEIGHT — a directory with more files is GENERALLY larger. NOT exact
//      area∝count: a single recursive d3.pack lets packing shape inflate an
//      enclosing radius (design "promise correction"), so we assert a monotone
//      TREND on well-separated sizes, not per-pair area equality.
//   3. HOME positions — every pack node (dir + file) receives a finite (hx,hy,r).
//   4. ROOT/FOREST — modules are the depth-1 dir circles PLUS a synthetic
//      "synthetic:root-files" module for depth-1 leaf files; the synthetic pack
//      root is never a module. The synthetic module appears iff root-level files
//      exist. Its id lives in a disjoint namespace from real dirs ("mod:"+dir),
//      so a real dir literally named "(root files)" cannot collide with it.
//   5. RIGID BODY — every descendant carries a frozen (ox,oy) from its module
//      center, so moving the module by (dx,dy) moves every descendant by exactly
//      (dx,dy). We verify the offset math (no force sim in this task).
// If you edit buildPackTree / computeModules in index.html, update the mirrors
// below and keep green. Proven RED when the builder/module logic is absent.

import { createRequire } from "module";
const require = createRequire(import.meta.url);
const d3 = require("../../src/ariadne_graph/web/static/d3.min.js");

// ---- mirrored VERBATIM from web/static/index.html ----
function parentDir(dir){ if(dir==="")return null; const i=dir.lastIndexOf("/"); return i>0?dir.slice(0,i):""; }

// Build a nested {name, dir, kind, children, value} tree from the flat scope
// nodes (dir hubs + file leaves, each carrying `dir`). Dirs nest by parentDir;
// files attach as value:1 leaves under their (collapsed) dir. Pure — no d3.
function buildPackTree(nodes) {
  const treeByDir = new Map();
  const mk = (dir, src) => ({ name: dir === "" ? "" : dir.split("/").pop(),
    dir, kind: "dir", src, children: [], value: 0 });
  for (const d of nodes) if (d.kind === "dir") treeByDir.set(d.dir, mk(d.dir, d));
  if (!treeByDir.has("")) treeByDir.set("", mk("", null));
  for (const d of nodes) {
    if (d.kind !== "dir" || d.dir === "") continue;
    (treeByDir.get(parentDir(d.dir)) || treeByDir.get("")).children.push(treeByDir.get(d.dir));
  }
  for (const d of nodes) {
    if (d.kind !== "file") continue;
    (treeByDir.get(d.dir) || treeByDir.get("")).children.push(
      { name: d.label, dir: d.dir, kind: "file", src: d, value: 1 });
  }
  return treeByDir.get("");
}

function packLayout(nodes, S) {
  const root = d3.hierarchy(buildPackTree(nodes))
    .sum(d => d.value || 0)
    .sort((a, b) => (b.value || 0) - (a.value || 0));
  if (!(root.value > 0)) root.value = 1;   // empty scope: floor root so d3.pack doesn't divide by 0 → NaN
  d3.pack().size([S, S]).padding(3)(root);
  return root;
}

// Identify TOP-LEVEL MODULES (rigid bodies) from a packed hierarchy `root`.
// A module is a depth-1 DIRECTORY pack node, PLUS one synthetic "(root files)"
// module holding any depth-1 leaf files (files directly under the scope root,
// with no owning directory circle). The synthetic pack root is NOT a module and
// is skipped when rendering. For every descendant of a module we FREEZE an offset
// (ox,oy) = (node.hx − module.hx, node.hy − module.hy) onto its flat src node and
// tag node.moduleId — so later a module can move as one body (module.x+ox, +oy).
// Home positions (hx,hy) are pack coords shifted by (offX,offY). Pure — mirrored
// VERBATIM in web/static/index.html (keep both in sync). Returns the module list.
function computeModules(root, offX, offY) {
  const modules = [];
  const rootFiles = [];
  for (const child of (root.children || [])) {
    if (child.data.kind === "dir") {
      const hx = child.x + offX, hy = child.y + offY;
      const id = "mod:" + child.data.dir;
      let n = 0;
      child.each(pn => {
        const d = pn.data.src; if (!d) return;
        d.moduleId = id;
        d.ox = (pn.x + offX) - hx;
        d.oy = (pn.y + offY) - hy;
        if (pn.data.kind === "file") n++;
      });
      // Drop degenerate empty dirs (no files anywhere in subtree): d3.pack gives
      // them r=0, a degenerate rigid body for the later force sim (Task 4).
      if (n > 0) modules.push({ id, homeX: hx, homeY: hy, R: child.r, n });
    } else if (child.data.kind === "file") {
      rootFiles.push(child);
    }
  }
  if (rootFiles.length) {
    // "synthetic:" prefix (not "mod:") so a real dir literally named "(root files)"
    // — whose id is "mod:(root files)" — can never collide with this module.
    const id = "synthetic:root-files";
    // n = root-files array length here; for real dirs above n = full-subtree file
    // count. Coincidentally both are "file count", but semantics differ. R is an
    // ad-hoc bounding circle over the scattered root files, unlike real dirs which
    // reuse child.r from d3.pack.
    const hx = rootFiles.reduce((s, f) => s + f.x + offX, 0) / rootFiles.length;
    const hy = rootFiles.reduce((s, f) => s + f.y + offY, 0) / rootFiles.length;
    let R = 0;
    for (const f of rootFiles) {
      const d = f.data.src; if (!d) continue;
      d.moduleId = id;
      d.ox = (f.x + offX) - hx;
      d.oy = (f.y + offY) - hy;
      R = Math.max(R, Math.hypot(f.x + offX - hx, f.y + offY - hy) + f.r);
    }
    modules.push({ id, homeX: hx, homeY: hy, R, n: rootFiles.length });
  }
  return modules;
}
// ---- end mirror ----

// ---- synthetic lumen_platform-shaped scope (same fixture family as the old harness) ----
// dir hubs of varying depth; some tiny, one 200-file giant; files carry a `dir`
// equal to their (collapsed) shown directory, exactly like renderScope emits.
const dirFileList = {
  "": 4, "organs": 12, "organs/knowledge": 200, "organs/data": 9, "organs/learning": 14,
  "adapters": 8, "adapters/http": 40, "adapters/db": 7, "adapters/cache": 5,
  "infra": 10, "infra/core": 18, "infra/net": 6, "utils": 6,
};
const dirs = Object.keys(dirFileList);
const nodes = [];
for (const dir of dirs) {
  const depth = dir === "" ? 0 : dir.split("/").length;
  nodes.push({ id: "dir:" + dir, kind: "dir", dir, depth, label: dir === "" ? "root" : dir.split("/").pop() });
  for (let k = 0; k < dirFileList[dir]; k++)
    nodes.push({ id: `${dir}#${k}`, kind: "file", dir, label: `f${k}.py`, worst: null, path: dir + "/f" + k });
}

const S = 1200;
const root = packLayout(nodes, S);

// index pack nodes by their source dir / file id for the assertions
const packByDir = new Map();
const packFiles = [];
root.each(pn => {
  if (pn.data.kind === "dir") packByDir.set(pn.data.dir, pn);
  else if (pn.data.kind === "file") packFiles.push(pn);
});

// Emit HOME positions onto the flat src nodes (mirrors index.html's write-back),
// then compute modules + freeze subtree offsets against a nonzero stage offset.
const S_offX = 50, S_offY = 50;
root.each(pn => {
  const d = pn.data.src; if (!d) return;
  d.hx = pn.x + S_offX; d.hy = pn.y + S_offY; d.r = pn.r;
});
const modules = computeModules(root, S_offX, S_offY);
const flatById = new Map(nodes.map(d => [d.id, d]));

// ---- assertions ----
let fails = 0;
function check(name, cond, detail){ if(!cond){fails++; console.log("FAIL:", name, detail||"");} else console.log("ok  :", name); }
const inside = (child, parent, slack=1e-6) =>
  Math.hypot(child.x - parent.x, child.y - parent.y) + child.r <= parent.r + slack;

// (0) HOME positions exist and are finite for every pack node.
let allFinite = true;
root.each(pn => { if(![pn.x,pn.y,pn.r].every(Number.isFinite)) allFinite = false; });
check("every pack node has finite (hx,hy,r)", allFinite);

// (1a) CONTAINMENT: every file leaf sits inside its directory circle.
let leafOk = true, leafWorst = 0;
for (const f of packFiles) {
  const dir = packByDir.get(f.data.dir);
  if (!dir || !inside(f, dir)) { leafOk = false;
    if (dir) leafWorst = Math.max(leafWorst, Math.hypot(f.x-dir.x,f.y-dir.y)+f.r-dir.r); }
}
check("every file leaf inside its directory circle", leafOk, `worst overflow=${leafWorst.toFixed(3)}px`);

// (1b) CONTAINMENT: every child directory sits inside its parent directory circle.
let dirOk = true, dirWorst = 0;
for (const [dir, pn] of packByDir) {
  const p = parentDir(dir); if (p === null) continue;
  const parent = packByDir.get(p);
  if (!parent || !inside(pn, parent)) { dirOk = false;
    if (parent) dirWorst = Math.max(dirWorst, Math.hypot(pn.x-parent.x,pn.y-parent.y)+pn.r-parent.r); }
}
check("every child dir inside its parent dir circle", dirOk, `worst overflow=${dirWorst.toFixed(3)}px`);

// (2) RELAXED WEIGHT: a dir with (well) more files is generally larger. Compare
//     three depth-1 dirs whose subtree file counts are clearly separated:
//       organs (12+200+9+14=235)  >  adapters (8+40+7+5=60)  >  utils (6).
//     Monotone TREND on separated sizes — NOT exact area∝count (packing shape
//     can inflate an enclosing radius; see design "promise correction").
const rOrgans = packByDir.get("organs").r, rAdapters = packByDir.get("adapters").r, rUtils = packByDir.get("utils").r;
check("bigger subtree ⇒ bigger dir circle (monotone trend)",
  rOrgans > rAdapters && rAdapters > rUtils,
  `organs=${rOrgans.toFixed(1)} adapters=${rAdapters.toFixed(1)} utils=${rUtils.toFixed(1)}`);

// (3) the 200-file giant is the largest depth-1-descendant dir but is STILL fully
//     contained in the root — a huge dir can't leak out of the frame (was the old
//     ring-blowout bug in a different guise).
const knowledge = packByDir.get("organs/knowledge"), rootPn = packByDir.get("");
check("200-file dir is fully contained in root", inside(knowledge, rootPn),
  `k=(${knowledge.x.toFixed(0)},${knowledge.y.toFixed(0)},r${knowledge.r.toFixed(0)}) root r=${rootPn.r.toFixed(0)}`);
check("root pack node fills the requested square", Math.abs(rootPn.r - S/2) < 1,
  `root r=${rootPn.r.toFixed(2)} expected ~${S/2}`);

// (4) EMPTY SCOPE: drilling into a dir whose whole subtree sums to zero weight
//     (no files anywhere under it). d3.pack divides by the root's summed value,
//     so a true-zero root yields r=NaN which propagates into hx/hy. The floor in
//     packLayout mirrors the old sunburst's `Math.max(1, sum)` — assert no NaN.
const emptyNodes = [{ id: "dir:", kind: "dir", dir: "", depth: 0, label: "root" }];
const emptyRoot = packLayout(emptyNodes, S);
const offX = 50, offY = 50;   // arbitrary nonzero stage offset, as renderScope applies
let emptyFinite = true;
emptyRoot.each(pn => {
  const hx = pn.x + offX, hy = pn.y + offY;
  if (![pn.r, hx, hy].every(Number.isFinite)) emptyFinite = false;
});
check("empty scope (zero-weight subtree) has no NaN in hx/hy/r", emptyFinite,
  `root r=${emptyRoot.r}`);

// (5) ROOT/FOREST: the synthetic pack root is NOT a module (not in `modules`) and
//     is not rendered (its src is null). Depth-1 directories ARE modules; and the
//     4 root-level files land in a synthetic "(root files)" module, each carrying a
//     valid moduleId + finite offset.
const modIds = new Set(modules.map(m => m.id));
// The pack root (dir "") is never collected as a module (modules come only from
// root.children), so no descendant carries the root's own moduleId and the root
// circle is never rendered as a module body.
check("synthetic pack root is NOT a module", !modIds.has("mod:"),
  `ids=[${[...modIds].join(", ")}]`);
check("every depth-1 directory is a module",
  ["organs", "adapters", "infra", "utils"].every(dir => modIds.has("mod:" + dir)),
  `ids=[${[...modIds].join(", ")}]`);
const rootFilesMod = modules.find(m => m.id === "synthetic:root-files");
check("root-level files form a synthetic (root files) module",
  !!rootFilesMod && rootFilesMod.n === 4,
  rootFilesMod ? `n=${rootFilesMod.n}` : "module absent");
const rootFileNodes = nodes.filter(d => d.kind === "file" && d.dir === "");
check("each root-level file has moduleId=synthetic:root-files + finite offset",
  rootFileNodes.length === 4 && rootFileNodes.every(d =>
    d.moduleId === "synthetic:root-files" && Number.isFinite(d.ox) && Number.isFinite(d.oy)),
  `tagged=${rootFileNodes.filter(d=>d.moduleId==="synthetic:root-files").length}/4`);
check("every module has finite home + R + positive leaf count n",
  modules.length > 0 && modules.every(m =>
    [m.homeX, m.homeY, m.R].every(Number.isFinite) && m.n > 0),
  `modules=${modules.length}`);

// (6) RIGID BODY: shifting a module center by (dx,dy) moves EVERY descendant by
//     exactly (dx,dy). We verify the OFFSET MATH (no sim yet): rendered position =
//     home + offset, so (module.home+delta)+offset − original-home === delta for
//     every descendant. Pick the "organs" module (a deep multi-level subtree).
const organsMod = modules.find(m => m.id === "mod:organs");
const [dx, dy] = [123.5, -87.25];
let rigidOk = true, rigidWorst = 0;
for (const d of nodes) {
  if (d.moduleId !== organsMod.id) continue;
  // original rendered home for this descendant
  const home0X = d.hx, home0Y = d.hy;
  // module moves to home+delta; descendant re-placed via frozen offset
  const newX = (organsMod.homeX + dx) + d.ox;
  const newY = (organsMod.homeY + dy) + d.oy;
  const errX = (newX - home0X) - dx, errY = (newY - home0Y) - dy;
  if (Math.abs(errX) > 1e-6 || Math.abs(errY) > 1e-6) rigidOk = false;
  rigidWorst = Math.max(rigidWorst, Math.abs(errX), Math.abs(errY));
}
const organsDescendants = nodes.filter(d => d.moduleId === organsMod.id).length;
check("rigid body: every organs descendant tracks module delta exactly",
  rigidOk && organsDescendants > 1,
  `descendants=${organsDescendants} worst_err=${rigidWorst.toExponential(2)}px`);

// (7) FOREST edge cases — the synthetic (root files) module appears iff there are
//     depth-1 leaf files:
//   (a) scope with subdirs but NO root-level files ⇒ no synthetic module.
//   (b) scope with ONLY root-level files (no subdirs) ⇒ exactly the synthetic one.
function modulesFor(dirFileMap) {
  const ns = [];
  for (const dir of Object.keys(dirFileMap)) {
    ns.push({ id: "dir:" + dir, kind: "dir", dir, label: dir || "root" });
    for (let k = 0; k < dirFileMap[dir]; k++)
      ns.push({ id: `${dir}#${k}`, kind: "file", dir, label: `f${k}.py`, path: dir + "/f" + k });
  }
  const r = packLayout(ns, S);
  r.each(pn => { const d = pn.data.src; if (d) { d.hx = pn.x + 50; d.hy = pn.y + 50; d.r = pn.r; } });
  return { modules: computeModules(r, 50, 50), nodes: ns };
}
const noRootFiles = modulesFor({ "": 0, "a": 3, "b": 5 }).modules;
check("no root-level files ⇒ no synthetic (root files) module",
  !noRootFiles.some(m => m.id === "synthetic:root-files") &&
  noRootFiles.length === 2,
  `ids=[${noRootFiles.map(m=>m.id).join(", ")}]`);
const onlyRootFiles = modulesFor({ "": 6 }).modules;
check("only root-level files ⇒ exactly the synthetic module",
  onlyRootFiles.length === 1 && onlyRootFiles[0].id === "synthetic:root-files" && onlyRootFiles[0].n === 6,
  `ids=[${onlyRootFiles.map(m=>`${m.id}(n=${m.n})`).join(", ")}]`);

// (8) ID COLLISION: a real directory literally named "(root files)" must NOT
//     collide with the synthetic root-files module. Real dir ids are "mod:"+dir;
//     the synthetic id uses a "synthetic:" prefix, so they occupy disjoint
//     namespaces. Build a scope with such a dir AND root-level files → two
//     DISTINCT module ids, each descendant tagged with the right one.
const collisionRun = (() => {
  const ns = [];
  ns.push({ id: "dir:(root files)", kind: "dir", dir: "(root files)", label: "(root files)" });
  for (let k = 0; k < 3; k++)
    ns.push({ id: `(root files)#${k}`, kind: "file", dir: "(root files)", label: `f${k}.py`, path: "(root files)/f" + k });
  for (let k = 0; k < 4; k++)
    ns.push({ id: `root#${k}`, kind: "file", dir: "", label: `g${k}.py`, path: "g" + k });
  const r = packLayout(ns, S);
  r.each(pn => { const d = pn.data.src; if (d) { d.hx = pn.x + 50; d.hy = pn.y + 50; d.r = pn.r; } });
  return { modules: computeModules(r, 50, 50), nodes: ns };
})();
const collisionIds = collisionRun.modules.map(m => m.id);
check("real dir named '(root files)' does NOT collide with synthetic module id",
  new Set(collisionIds).size === collisionIds.length &&
  collisionIds.includes("mod:(root files)") &&
  collisionIds.includes("synthetic:root-files"),
  `ids=[${collisionIds.join(", ")}]`);

// (9) ZERO-FILE DEPTH-1 DIR: an empty directory (no files anywhere in its
//     subtree) gets r=0 from d3.pack — a degenerate rigid body — and must be
//     EXCLUDED from `modules`. A sibling dir with files stays.
const zeroFileRun = modulesFor({ "": 0, "empty": 0, "full": 5 });
const zeroFileIds = zeroFileRun.modules.map(m => m.id);
check("zero-file depth-1 dir is excluded from modules",
  !zeroFileIds.includes("mod:empty") && zeroFileIds.includes("mod:full"),
  `ids=[${zeroFileIds.join(", ")}]`);

console.log(`\n${fails===0?"ALL PASS":fails+" FAILURES"}`);
process.exit(fails===0?0:1);
