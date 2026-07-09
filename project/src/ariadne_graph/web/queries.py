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


def _folder_of(file_path: str, repo_root: str) -> str | None:
    """Top-level folder of ``file_path`` relative to ``repo_root``.

    Handles the graph's mixed path formats: some CodeFile paths are absolute
    (under ``repo_root``), others are already repo-relative (``server/x.ts``).
    A file directly under the root (no subfolder) returns ``None`` so it's
    grouped as a root-level file, not a folder.
    """
    root = repo_root.rstrip("/") + "/"
    if file_path.startswith(root):
        rel = file_path[len(root):]
    elif file_path.startswith("/"):
        return None  # absolute but outside this repo root — not ours
    else:
        rel = file_path  # already repo-relative
    head, sep, _ = rel.partition("/")
    return head if sep else None


async def _rows(store: SQLiteGraphStore, sql: str, params: tuple[Any, ...]) -> list[Any]:
    db = await store._connect()
    try:
        cursor = await db.execute(sql, params)
        return list(await cursor.fetchall())
    finally:
        await db.close()  # releases the store lock; underlying conn stays open


# SCIP-resolved cross-file dependency edges live in the REFERENCES rel_type
# (every symbol occurrence resolving to its definition in another file). The
# older AST-derived CALLS edges are NOT resolved (targets are bare names), so we
# deliberately use REFERENCES for real file→file links.
_XREF_SQL = """
SELECT json_extract(sn.properties, '$.file_path') AS sf,
       json_extract(tn.properties, '$.file_path') AS tf
FROM edges e
JOIN nodes sn ON sn.graph_id = e.graph_id AND sn.id = e.source
JOIN nodes tn ON tn.graph_id = e.graph_id AND tn.id = e.target
WHERE e.graph_id = ?
  AND e.rel_type = 'REFERENCES'
  AND json_extract(sn.properties, '$.file_path') IS NOT NULL
  AND json_extract(tn.properties, '$.file_path') IS NOT NULL
  AND json_extract(sn.properties, '$.file_path') != json_extract(tn.properties, '$.file_path')
"""


async def folder_edges(
    store: SQLiteGraphStore, graph_id: str, *, repo_root: str
) -> list[dict[str, Any]]:
    """Altitude 1 edges: folder→folder dependency, weighted by cross-file refs.

    A reference from a file in folder A to a file in folder B contributes to an
    A→B edge. Self-edges (within one folder) are dropped.
    """
    rows = await _rows(store, _XREF_SQL, (graph_id,))
    weights: dict[tuple[str, str], int] = {}
    for r in rows:
        src = _folder_of(r["sf"], repo_root)
        tgt = _folder_of(r["tf"], repo_root)
        if src is None or tgt is None or src == tgt:
            continue
        weights[(src, tgt)] = weights.get((src, tgt), 0) + 1
    return [
        {"source": s, "target": t, "weight": w} for (s, t), w in weights.items()
    ]


async def file_edges(
    store: SQLiteGraphStore, graph_id: str, *, repo_root: str, folder: str
) -> list[dict[str, Any]]:
    """Altitude 2 edges: file→file dependency within a folder's subtree.

    Only edges where BOTH endpoints are files inside ``folder`` are returned, so
    the edge set matches the file nodes shown at this altitude.
    """
    abs_prefix = f"{repo_root.rstrip('/')}/{folder}/"
    rel_prefix = f"{folder}/"

    def _in_folder(path: str) -> bool:
        return path.startswith(abs_prefix) or path.startswith(rel_prefix)

    rows = await _rows(store, _XREF_SQL, (graph_id,))
    weights: dict[tuple[str, str], int] = {}
    for r in rows:
        sf, tf = r["sf"], r["tf"]
        if not _in_folder(sf) or not _in_folder(tf):
            continue
        weights[(sf, tf)] = weights.get((sf, tf), 0) + 1
    return [
        {"source": s, "target": t, "weight": w} for (s, t), w in weights.items()
    ]


async def list_folders(
    store: SQLiteGraphStore, graph_id: str, *, repo_root: str
) -> list[dict[str, Any]]:
    """Altitude 1: top-level folders as nodes, with file count + hygiene rollup.

    Returns one entry per top-level folder under ``repo_root`` for this graph,
    each carrying ``file_count``, ``diagnostic_count`` and ``worst_level``.
    """
    # Diagnostics attach to SYMBOLS (function/method/import), not to CodeFile
    # nodes, and every CodeDiagnostic node carries properties.file_path. So we
    # aggregate diagnostics by file_path (a plain node scan), independent of the
    # file-node scan, then bucket both into folders in Python.
    file_rows = await _rows(
        store,
        """
        SELECT file_path FROM nodes
        WHERE graph_id = ? AND labels LIKE '%CodeFile%' AND file_path IS NOT NULL
        """,
        (graph_id,),
    )
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

    folders: dict[str, dict[str, Any]] = {}
    for r in file_rows:
        folder = _folder_of(r["file_path"], repo_root)
        if folder is None:
            continue  # root-level file; not a folder node in v1
        bucket = folders.setdefault(
            folder, {"folder": folder, "file_count": 0, "diagnostic_count": 0, "worst_level": None}
        )
        bucket["file_count"] += 1

    for r in diag_rows:
        if not r["file_path"]:
            continue
        folder = _folder_of(r["file_path"], repo_root)
        if folder is None or folder not in folders:
            continue
        bucket = folders[folder]
        bucket["diagnostic_count"] += 1
        level = r["level"]
        if level in _LEVEL_RANK:
            cur = bucket["worst_level"]
            if cur is None or _LEVEL_RANK[level] > _LEVEL_RANK[cur]:
                bucket["worst_level"] = level

    return sorted(folders.values(), key=lambda f: f["file_count"], reverse=True)


async def count_files(
    store: SQLiteGraphStore, graph_id: str, *, repo_root: str, folder: str
) -> int:
    """Total CodeFiles in ``folder``'s subtree for this graph (for truncation)."""
    abs_prefix = f"{repo_root.rstrip('/')}/{folder}/%"
    rel_prefix = f"{folder}/%"
    rows = await _rows(
        store,
        """
        SELECT COUNT(*) AS c FROM nodes
        WHERE graph_id = ? AND labels LIKE '%CodeFile%'
          AND file_path IS NOT NULL AND (file_path LIKE ? OR file_path LIKE ?)
        """,
        (graph_id, abs_prefix, rel_prefix),
    )
    return int(rows[0]["c"])


async def list_files(
    store: SQLiteGraphStore,
    graph_id: str,
    *,
    repo_root: str,
    folder: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Altitude 2: CodeFiles in ``folder``'s subtree, with hygiene paint.

    Scoped by graph AND folder prefix. Each file carries ``diagnostic_count`` and
    ``worst_level``. Capped at ``limit`` (pair with ``count_files`` for the
    truncated total).
    """
    abs_prefix = f"{repo_root.rstrip('/')}/{folder}/%"
    rel_prefix = f"{folder}/%"
    # Per-file diagnostic rollup, keyed by the diagnostic node's file_path
    # (diagnostics attach to symbols, not files — see list_folders).
    diag_rows = await _rows(
        store,
        """
        SELECT json_extract(properties, '$.file_path') AS file_path,
               json_extract(properties, '$.level') AS level
        FROM nodes
        WHERE graph_id = ? AND labels LIKE '%CodeDiagnostic%'
          AND (json_extract(properties, '$.file_path') LIKE ?
               OR json_extract(properties, '$.file_path') LIKE ?)
        """,
        (graph_id, abs_prefix, rel_prefix),
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
        SELECT id, file_path, json_extract(properties, '$.name') AS name
        FROM nodes
        WHERE graph_id = ? AND labels LIKE '%CodeFile%'
          AND file_path IS NOT NULL AND (file_path LIKE ? OR file_path LIKE ?)
        ORDER BY file_path
        LIMIT ?
        """,
        (graph_id, abs_prefix, rel_prefix, limit),
    )

    files: list[dict[str, Any]] = []
    for r in file_rows:
        diag = diag_by_file.get(r["file_path"], {"count": 0, "worst": None})
        files.append(
            {
                "id": r["id"],
                "file_path": r["file_path"],
                "name": r["name"],
                "diagnostic_count": diag["count"],
                "worst_level": diag["worst"],
            }
        )
    # Show the most-diagnosed files first (the hygiene focus).
    files.sort(key=lambda f: f["diagnostic_count"], reverse=True)
    return files


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
