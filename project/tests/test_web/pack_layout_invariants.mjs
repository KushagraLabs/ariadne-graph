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
// If you edit buildPackTree in index.html, update the mirror below and keep green.
// It is proven to go RED when the builder is absent / the tree is mis-nested.

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

console.log(`\n${fails===0?"ALL PASS":fails+" FAILURES"}`);
process.exit(fails===0?0:1);
