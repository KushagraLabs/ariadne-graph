# Browser graph view — design

**Date:** 2026-07-09
**Status:** approved, pre-implementation

## Goal

A read-only browser view of the code graph that lets you explore a repo's
structure **and** see hygiene problems (diagnostics, hotspots) painted onto that
same structure — one workflow, not two. Served by the already-running ariadne
daemon on `:8848`. No new process, no Neo4j, no Docker.

## Why this shape (verified facts, 2026-07-09)

- The daemon runs on **SQLite** (`.ariadne/graph.db`), not Neo4j.
- One physical `graph.db` holds **multiple graphs** keyed by `graph_id` (a content
  hash). cosmic_lens's db alone has 6 graphs. **Every query MUST scope by
  `graph_id`** or it mixes repos.
- Graph sizes span 3 orders of magnitude: `enterprise_tabular_ad` 18k nodes,
  `Kushagra` 33k, `cosmic_lens` biggest graph 590k nodes / 1.7M edges / 5,379
  files. A flat force-layout (like the ~600-node reference the user likes) is
  impossible for the big graph → **drill-down by altitude** is required.
- Raw Louvain communities are unusable as a top unit: 13,120 distinct, power-law
  (one has 21,780 nodes, ~12,670 are singletons).
- **Folder structure is the clean top altitude**: `CodeFile.file_path` is a
  populated absolute path; top-level dirs under repo root are ~10–15 per repo
  (e.g. lumen graph: tests/2020, lumen_platform/1848, scripts/964,
  lumen_frontend/430, …).
- Hygiene paint sources exist: `HAS_DIAGNOSTIC` edges (91k), and each
  `CodeDiagnostic` node carries `properties.level` (info/warning/error) + `rule`.

## Schema (real column names, not model field names)

- `nodes(id, graph_id, labels, properties, file_path)` — `labels` is a JSON array
  e.g. `["KnowledgeNode","CodeFile"]`; `properties` is JSON.
- `edges(source, target, graph_id, rel_type, properties)` — `rel_type` ∈
  {CONTAINS, CALLS, REFERENCES, DEFINES, HAS_DIAGNOSTIC, IMPORTS, IMPORTS_SYMBOL, …}.
- `communities(graph_id, node_id, community_id)` — node→community map (not used in v1).

## Module placement

New sub-package `project/src/ariadne_graph/web/` — sibling of `mcp/`, `core/`,
`graphstores/`. `mcp/` owns the agent-facing tool surface; `web/` owns the
human-facing HTTP view. Both are presentation layers over the same
`core/`+`graphstores/`; neither duplicates the other.

```
web/
  __init__.py       # register_routes(mcp)
  routes.py         # @mcp.custom_route handlers (verified API on FastMCP 1.27.2)
  queries.py        # read-only SQL helpers over the graphstore (scoped by graph_id)
  static/
    index.html      # one self-contained page: inlined CSS + JS + vendored cytoscape
    cytoscape.min.js  # vendored, no CDN (CSP-safe, offline-safe)
```

Wiring: `server.py` calls `web.register_routes(mcp)` once after
`mcp = FastMCP("ariadne")`. Routes ride the existing streamable-http app on
`:8848`, same event loop, same graphstore.

## Endpoints (all GET, read-only, all take `?graph=<graph_id>`)

- `GET /graph` → the static HTML page.
- `GET /api/graph/repos` → list of `{graph_id, repo_root, file_count}` across the db.
- `GET /api/graph/folders?graph=…` → altitude 1: top-level folders as nodes
  `{id, label, file_count, hotspot_score, worst_level}` + folder↔folder import
  edges (aggregated from file-level IMPORTS).
- `GET /api/graph/files?graph=…&folder=…` → altitude 2: CodeFile nodes in that
  folder subtree + IMPORTS edges between them, each with diagnostic count + level.
- `GET /api/graph/symbols?graph=…&file=…` → altitude 3: functions/classes in the
  file + CALLS edges, each with its diagnostics.

Every endpoint caps returned nodes (default 500) and returns a `truncated`
count so the UI shows "N more hidden" — no silent capping.

## Rendering (matches the user's reference aesthetic)

Cytoscape.js, vendored inline. Force layout (cose) at every altitude → the
"flower cluster" look of the reference image, but bounded per altitude so it
never becomes a hairball. Node **color** = worst diagnostic level (green→amber→red),
node **size** = hotspot score (aggregated at folder/file altitude). Breadcrumb
navigation (repo ▸ folder ▸ file) to climb back up. A fixed side panel shows the
selected node's diagnostics list and the color/size legend — hygiene is always
visible, not toggled.

## Testing (verification-first)

- **queries.py unit tests** against a tiny fixture db: assert graph_id scoping
  (a query for graph A never returns graph B's nodes — the real correctness
  trap), folder aggregation counts, cap+truncated behavior.
- **route smoke tests**: each endpoint returns valid JSON with expected keys;
  `/graph` returns the HTML.
- **Hard case first**: a test that seeds two graphs in one db and asserts
  `/api/graph/folders?graph=A` excludes B's files. This is the bug a fuzzy
  implementation would get wrong.
- Manual E2E target: `enterprise_tabular_ad` (18k, small) then cosmic_lens graph
  `e11b4c88` (5,379 files). lumen_ai + code_hygiene_mcp graphs are EMPTY (0 nodes)
  — not valid test targets.

## Build sequence

1. `web/queries.py` + unit tests (graph_id scoping hard-case first, red→green).
2. `web/routes.py` + `register_routes`, wired into server.py; route smoke tests.
3. `web/static/index.html` + vendored cytoscape; altitude 1 only.
4. Altitudes 2 + 3 (drill-down) + hygiene paint.
5. Manual E2E on enterprise_tabular_ad, then cosmic_lens.

## Explicitly NOT in v1 (YAGNI)

- No communities view (data is lopsided; folders are the better top unit).
- No editing/write endpoints (read-only).
- No node-budget slider (fixed cap + drill-down covers it).
- No separate web server / port (rides the daemon).
- No auth (127.0.0.1 single-user, same trust boundary as the MCP daemon).
