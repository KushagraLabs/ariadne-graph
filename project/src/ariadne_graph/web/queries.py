"""Read-only aggregate queries powering the browser graph view.

All queries are scoped by ``graph_id`` — one physical ``graph.db`` holds several
graphs (keyed by content hash), so an unscoped query would leak another repo's
nodes. Aggregation runs in SQL (not by pulling whole-graph node dumps into
Python) so it stays fast on the 590k-node graphs.

Concurrency: reuses the store's single persistent connection via ``_connect()``
(serialized by the store's asyncio lock), so viewer reads never race the
auto-sync writer with a second connection.
"""

from __future__ import annotations

from typing import Any

from ariadne_graph.graphstores.sqlite import SQLiteGraphStore

# Diagnostic levels ordered worst-first, for "worst_level" aggregation.
_LEVEL_RANK = {"error": 3, "warning": 2, "info": 1}


async def _rows(store: SQLiteGraphStore, sql: str, params: tuple[Any, ...]) -> list[Any]:
    db = await store._connect()
    try:
        cursor = await db.execute(sql, params)
        return list(await cursor.fetchall())
    finally:
        await db.close()  # releases the store lock; underlying conn stays open


# SCIP-resolved cross-file dependency edges, per language:
#   * TypeScript: the `REFERENCES` rel_type (every symbol occurrence that the
#     SCIP translator resolves to its definition in another file).
#   * Python: SCIP does not emit REFERENCES; it refines the target of `CALLS`
#     edges in place, tagging the resolved ones ``resolved_by=scip-python``.
#     Bare-name (unresolved) CALLS keep fuzzy targets, so they are excluded.
# Both are real, definition-resolved file→file links; import-level edges are
# deliberately NOT used (they would bypass SCIP resolution entirely).
_XREF_SQL = """
SELECT json_extract(sn.properties, '$.file_path') AS sf,
       json_extract(tn.properties, '$.file_path') AS tf
FROM edges e
JOIN nodes sn ON sn.graph_id = e.graph_id AND sn.id = e.source
JOIN nodes tn ON tn.graph_id = e.graph_id AND tn.id = e.target
WHERE e.graph_id = ?
  AND (
        e.rel_type = 'REFERENCES'
        OR (e.rel_type = 'CALLS'
            AND json_extract(e.properties, '$.resolved_by') = 'scip-python')
      )
  AND json_extract(sn.properties, '$.file_path') IS NOT NULL
  AND json_extract(tn.properties, '$.file_path') IS NOT NULL
  AND json_extract(sn.properties, '$.file_path') != json_extract(tn.properties, '$.file_path')
"""


def _rel(file_path: str, repo_root: str) -> str:
    """Repo-relative path, handling the graph's mixed absolute/relative formats.

    Mirrors ``_folder_of``'s prefix logic: strip ``repo_root`` from absolute paths
    under it, leave already-relative paths as-is. Paths absolute but outside the
    root keep their leading slash and land under a synthetic ``(external)`` root.
    """
    root = repo_root.rstrip("/") + "/"
    if file_path.startswith(root):
        return file_path[len(root):]
    if file_path.startswith("/"):
        return "(external)" + file_path
    return file_path


def _dir_of(rel_path: str) -> str:
    """Directory portion of a repo-relative file path ('' for a root-level file)."""
    head, sep, _ = rel_path.rpartition("/")
    return head if sep else ""


# Peripheral organs (tests, scripts, and kin) are entry-point / glue code that
# legitimately reaches into everything it exercises or wires together. Dep edges
# ORIGINATING from these organs are omitted entirely, so the graph focuses on the
# core wiring rather than being swamped by test/script fan-in.
_PERIPHERAL_ORGANS = {
    "tests", "test", "__tests__", "spec", "specs", "e2e",
    "scripts", "script",
}


def _is_peripheral_source(dir_path: str) -> bool:
    """True if a file in ``dir_path`` belongs to a peripheral organ (test/script)."""
    organ = dir_path.split("/", 1)[0] if dir_path else ""
    return organ in _PERIPHERAL_ORGANS


def _is_violation(dir_a: str, dir_b: str) -> bool:
    """Layering rule for an import from a file in ``dir_a`` to one in ``dir_b``.

    Only ever called for NON-peripheral sources (peripheral-source edges are
    dropped upstream). ``dir_*`` are repo-relative directory paths; the top-level
    *organ* is the first path segment. An import is ALLOWED when either:

      1. Same organ — A and B share the first segment. Any direction within your
         own organ is fine (organ is the unit of encapsulation).
      2. Organ front door — B sits directly at another organ's top level (its dir
         IS a top-level organ, no deeper), e.g. ``src/x -> tests``.

    A VIOLATION is reaching into a *different* organ's internals (B one or more
    levels below its organ root), e.g. ``src/x -> tests/unit/y``. Root-level files
    (empty dir) belong to no organ and never violate.
    """
    if dir_a == dir_b:
        return False
    organ_a = dir_a.split("/", 1)[0] if dir_a else ""
    organ_b = dir_b.split("/", 1)[0] if dir_b else ""
    if organ_a == organ_b:
        return False               # same organ — always fine
    # Different organ: OK only if B is that organ's top-level front door (no "/"),
    # a violation if it reaches into the organ's internals.
    return "/" in dir_b


async def full_graph(store: SQLiteGraphStore, graph_id: str, *, repo_root: str) -> dict[str, Any]:
    """Directory-driven node-link view: directory hubs + file leaves + real deps.

    Two node kinds:
      * ``dir``  — one per directory path segment (full nesting), a containment
        hub. Carries ``depth`` and a subtree hygiene rollup (worst of its files).
      * ``file`` — a leaf, carrying ``module`` (top-level ancestor, for color) and
        its own hygiene rollup.

    Two edge kinds:
      * ``tree`` — file→parent-dir and dir→parent-dir containment (the skeleton;
        the link force pulls each directory's files into a cluster around its hub).
      * ``dep``  — the SCIP-resolved file→file references from ``_XREF_SQL``.

    Every file is kept (its tree edge is its reason to exist — a dependency-free
    file is still an "employee" of its directory), unlike the flat view which
    dropped isolates.
    """
    # file_path -> hygiene rollup (diagnostics attach to symbols, keyed by file_path).
    diag_rows = await _rows(
        store,
        """
        SELECT json_extract(properties, '$.file_path') AS file_path,
               json_extract(properties, '$.level') AS level
        FROM nodes
        WHERE graph_id = ? AND labels LIKE '%CodeDiagnostic%'
        """,
        (graph_id,),
    )
    diag_by_file: dict[str, dict[str, Any]] = {}
    for r in diag_rows:
        fp = r["file_path"]
        if not fp:
            continue
        entry = diag_by_file.setdefault(fp, {"count": 0, "worst": None})
        entry["count"] += 1
        level = r["level"]
        if level in _LEVEL_RANK and (
            entry["worst"] is None or _LEVEL_RANK[level] > _LEVEL_RANK[entry["worst"]]
        ):
            entry["worst"] = level

    file_rows = await _rows(
        store,
        """
        SELECT file_path, json_extract(properties, '$.name') AS name
        FROM nodes
        WHERE graph_id = ? AND labels LIKE '%CodeFile%' AND file_path IS NOT NULL
        """,
        (graph_id,),
    )

    nodes: dict[str, dict[str, Any]] = {}   # id -> node
    tree_edges: list[dict[str, Any]] = []
    files: dict[str, dict[str, Any]] = {}   # abs file_path -> file node (for dep join)

    def _worse(a: str | None, b: str | None) -> str | None:
        ra, rb = _LEVEL_RANK.get(a or "", 0), _LEVEL_RANK.get(b or "", 0)
        return a if ra >= rb else b

    for r in file_rows:
        fp = r["file_path"]
        rel = _rel(fp, repo_root)
        parts = rel.split("/")
        segments, leaf = parts[:-1], parts[-1]
        module = segments[0] if segments else "(root)"

        # Walk the directory chain, creating each hub once and linking it to its parent.
        parent_id = None
        for depth, seg in enumerate(segments):
            dir_id = "/".join(segments[: depth + 1])
            hub = nodes.get(dir_id)
            if hub is None:
                hub = nodes[dir_id] = {
                    "id": dir_id, "kind": "dir", "name": seg,
                    "module": module, "depth": depth, "worst_level": None,
                }
                if parent_id is not None:
                    tree_edges.append({"source": dir_id, "target": parent_id, "kind": "tree"})
            parent_id = dir_id

        diag = diag_by_file.get(fp, {"count": 0, "worst": None})
        node = nodes[fp] = {
            "id": fp, "kind": "file", "name": r["name"] or leaf,
            "module": module, "worst_level": diag["worst"],
            "diagnostic_count": diag["count"],
            # Repo-relative parent dir: drives the layering rule AND client-side
            # subtree drill-down (a dir hub's id is the same rel-path form).
            "dir": _dir_of(rel),
        }
        files[fp] = node
        if parent_id is not None:
            tree_edges.append({"source": fp, "target": parent_id, "kind": "tree"})

        # Roll the file's hygiene up every ancestor directory (dept shows worst employee).
        for depth in range(len(segments)):
            dir_id = "/".join(segments[: depth + 1])
            nodes[dir_id]["worst_level"] = _worse(nodes[dir_id]["worst_level"], diag["worst"])

    xref = await _rows(store, _XREF_SQL, (graph_id,))
    weights: dict[tuple[str, str], int] = {}
    for r in xref:
        sf, tf = r["sf"], r["tf"]
        if sf not in files or tf not in files:
            continue
        # Drop edges originating from peripheral organs (tests/scripts/…) — they
        # are entry-point glue and swamp the core wiring otherwise.
        if _is_peripheral_source(files[sf]["dir"]):
            continue
        weights[(sf, tf)] = weights.get((sf, tf), 0) + 1
    dep_edges = [
        {
            "source": s, "target": t, "weight": w, "kind": "dep",
            "violation": _is_violation(files[s]["dir"], files[t]["dir"]),
        }
        for (s, t), w in weights.items()
    ]

    return {"nodes": list(nodes.values()), "edges": tree_edges + dep_edges}


async def list_graphs(store: SQLiteGraphStore) -> list[dict[str, Any]]:
    """List graphs that have indexed files, with their authoritative repo_root.

    ``repo_root`` comes from the ``graphs`` table (``repo_path``), which the
    indexer records — NOT a common-prefix guess. File paths in the graph are
    inconsistent (some absolute, some repo-relative across languages), so a
    prefix heuristic collapses to "/"; the recorded root is authoritative.
    """
    rows = await _rows(
        store,
        """
        SELECT g.graph_id AS graph_id,
               g.repo_path AS repo_path,
               COUNT(n.id) AS file_count
        FROM graphs g
        JOIN nodes n
          ON n.graph_id = g.graph_id
         AND n.labels LIKE '%CodeFile%'
         AND n.file_path IS NOT NULL
        GROUP BY g.graph_id
        HAVING file_count > 0
        ORDER BY file_count DESC
        """,
        (),
    )
    return [
        {"graph_id": r["graph_id"], "repo_root": r["repo_path"], "file_count": r["file_count"]}
        for r in rows
    ]
