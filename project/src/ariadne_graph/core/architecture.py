"""Graph-level architecture hygiene analysis.

A pure analysis pass over the *resolved* graph: given repo-relative file paths
and file->file dependency edges, it emits ``CodeDiagnostic`` findings about
structural problems that only exist at the whole-graph level (cycles, deep
cross-organ imports, orphans, upward imports) — as opposed to the AST-local
rules in :mod:`ariadne_graph.core.diagnostics`, which run per file during
extraction.

The single entry point, :func:`analyze`, is deliberately pure and
store-agnostic: ``(files, edges) -> list[CodeDiagnostic]``. The caller converts
absolute store paths to repo-relative form and persists the findings as
``CodeDiagnostic`` nodes on the owning ``CodeFile`` — the same node kind the
extractors already emit, so MCP retrieval, the per-directory web rollup, and the
browser paint all consume these for free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ariadne_graph.core.diagnostics import CodeDiagnostic
from ariadne_graph.core.models import CodeEdge, CodeNode

if TYPE_CHECKING:
    from ariadne_graph.graphstores.sqlite import SQLiteGraphStore

# Peripheral organs (tests, scripts, and kin) are entry-point / glue code that
# legitimately reaches into everything it exercises or wires together. Dep edges
# ORIGINATING from these organs are exempt from layering rules — they are not
# violations. This is an architectural fact about the repo, so it lives here;
# the web layer imports it from this module rather than keeping its own copy.
PERIPHERAL_ORGANS = frozenset(
    {
        "tests", "test", "__tests__", "spec", "specs", "e2e",
        "scripts", "script",
    }
)

# File *stems* that are legitimately unreferenced: package/module entry points,
# test fixtures, and packaging glue. An unreferenced file with one of these stems
# is not dead code, so it is never reported as an orphan.
_ENTRY_POINT_STEMS = frozenset(
    {"__main__", "__init__", "cli", "conftest", "main", "setup"}
)


def _stem(rel_path: str) -> str:
    """File name without directory or extension ('src/a/cli.py' -> 'cli')."""
    name = rel_path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _rel(file_path: str, repo_root: str) -> str:
    """Repo-relative path (strip repo_root prefix; leave already-relative as-is).

    Mirrors the web layer's ``_rel``: paths absolute but outside the root land
    under a synthetic ``(external)`` root so they never masquerade as an organ.
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


def _organ(rel_dir: str) -> str:
    """Top-level organ (first path segment) of a repo-relative directory."""
    return rel_dir.split("/", 1)[0] if rel_dir else ""


def is_deep_import(src_dir: str, dst_dir: str) -> bool:
    """Single source of truth for the deep-import layering rule.

    Both the persisted analysis (:func:`_deep_imports`, file-level) and the
    browser's per-edge paint consume this. ``src_dir``/``dst_dir`` are the
    repo-relative parent directories of the importing and imported files.

    A deep import reaches into a *different* organ's internals (target dir one or
    more levels below its organ root). Same-organ imports, front-door imports
    (target IS an organ root), and root-level files are all allowed.
    """
    src_organ, dst_organ = _organ(src_dir), _organ(dst_dir)
    if not src_organ or not dst_organ:
        return False
    if src_organ == dst_organ:
        return False
    return "/" in dst_dir


def analyze(
    files: list[str],
    dep_edges: list[tuple[str, str]],
) -> list[CodeDiagnostic]:
    """Run every architecture rule and return the combined findings.

    Args:
        files: Repo-relative paths of every ``CodeFile`` in the graph.
        dep_edges: Resolved file->file dependency edges as
            ``(source_rel, target_rel)`` repo-relative path pairs.

    Returns:
        One ``CodeDiagnostic`` per finding, ``node_id`` = the offending file's
        repo-relative path. ``properties.file_path`` is set to that same path so
        the web rollup (which reads ``$.file_path``) picks it up.
    """
    diagnostics: list[CodeDiagnostic] = []
    diagnostics.extend(_dependency_cycles(files, dep_edges))
    diagnostics.extend(_deep_imports(dep_edges))
    diagnostics.extend(_orphan_modules(files, dep_edges))
    diagnostics.extend(_upward_imports(dep_edges))
    return diagnostics


def _upward_imports(dep_edges: list[tuple[str, str]]) -> list[CodeDiagnostic]:
    """Flag intra-organ direction inversions: a file reaching *up* to an ancestor
    module in its own subtree. A parent importing a child (down) is normal; a
    child importing a strictly higher ancestor (up) inverts the layering.

    Only same-organ edges are considered — cross-organ reach is
    :func:`_deep_imports`' concern. An import is upward when the target's
    directory is a strict ancestor of the source's directory.
    """
    findings: list[CodeDiagnostic] = []
    for src, dst in dep_edges:
        src_dir, dst_dir = _dir_of(src), _dir_of(dst)
        if not src_dir or not dst_dir or src_dir == dst_dir:
            continue
        if _organ(src_dir) != _organ(dst_dir):
            continue  # different organ — deep_import's job
        # dst_dir is a strict ancestor of src_dir => src reaches up to it.
        if src_dir.startswith(dst_dir + "/"):
            findings.append(
                CodeDiagnostic(
                    node_id=src,
                    level="warning",
                    message=f"Upward import to ancestor module: {src} -> {dst}",
                    rule="upward_import",
                    properties={"file_path": src, "from": src, "to": dst},
                )
            )
    return findings


def _orphan_modules(
    files: list[str],
    dep_edges: list[tuple[str, str]],
) -> list[CodeDiagnostic]:
    """Flag files that nothing imports (fan-in 0) and that are not legitimate
    entry points. A sink that imports nothing is a normal leaf util and is NOT
    flagged — only the *unreferenced* flavor (dead-code candidate) is.
    """
    # No resolved dep edges at all (e.g. SCIP unavailable) => fan-in is
    # meaningless and every file would look orphaned. Stay silent rather than
    # emit pure noise.
    if not dep_edges:
        return []
    referenced = {dst for _, dst in dep_edges}
    findings: list[CodeDiagnostic] = []
    for rel in files:
        if rel in referenced:
            continue
        if _organ(_dir_of(rel)) in PERIPHERAL_ORGANS:
            continue
        if _stem(rel) in _ENTRY_POINT_STEMS:
            continue
        findings.append(
            CodeDiagnostic(
                node_id=rel,
                level="info",
                message=f"Orphan module: nothing imports '{rel}'",
                rule="orphan_module",
                properties={"file_path": rel},
            )
        )
    return findings


def _deep_imports(dep_edges: list[tuple[str, str]]) -> list[CodeDiagnostic]:
    """Flag imports that reach into a *different* organ's internals.

    Allowed: same-organ imports (any direction — the organ is the unit of
    encapsulation) and imports to another organ's top-level *front door* (target
    dir IS an organ root). A VIOLATION is reaching one or more levels below
    another organ's root, e.g. ``src/x -> lib/deep/y``. Edges originating from a
    peripheral organ (tests, scripts) are exempt entirely.
    """
    findings: list[CodeDiagnostic] = []
    for src, dst in dep_edges:
        src_dir, dst_dir = _dir_of(src), _dir_of(dst)
        if _organ(src_dir) in PERIPHERAL_ORGANS:
            continue  # edges from tests/scripts are exempt
        if not is_deep_import(src_dir, dst_dir):
            continue
        findings.append(
            CodeDiagnostic(
                node_id=src,
                level="warning",
                message=f"Deep import into '{_organ(dst_dir)}' internals: {src} -> {dst}",
                rule="deep_import",
                properties={
                    "file_path": src, "from": src, "to": dst, "organ": _organ(dst_dir),
                },
            )
        )
    return findings


def _dependency_cycles(
    files: list[str],
    dep_edges: list[tuple[str, str]],
) -> list[CodeDiagnostic]:
    """Flag every file in a dependency cycle (a strongly-connected component of
    size > 1). A well-abstracted repo is a DAG, so an empty result is the goal.
    """
    adjacency: dict[str, list[str]] = {f: [] for f in files}
    for src, dst in dep_edges:
        if src in adjacency and dst in adjacency:
            adjacency[src].append(dst)

    findings: list[CodeDiagnostic] = []
    for component in _strongly_connected_components(files, adjacency):
        if len(component) < 2:
            continue
        ring = sorted(component)
        for node in ring:
            findings.append(
                CodeDiagnostic(
                    node_id=node,
                    level="error",
                    message=f"File is in an import cycle: {' -> '.join(ring)}",
                    rule="dependency_cycle",
                    properties={"file_path": node, "cycle": ring},
                )
            )
    return findings


def _strongly_connected_components(
    nodes: list[str],
    adjacency: dict[str, list[str]],
) -> list[list[str]]:
    """Tarjan's SCC algorithm (iterative, to avoid recursion limits on large
    graphs). Returns each strongly-connected component as a list of node ids.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in nodes:
        if root in index_of:
            continue
        # Iterative DFS: work stack holds (node, iterator over its successors).
        work: list[tuple[str, int]] = [(root, 0)]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, child_idx = work[-1]
            successors = adjacency[node]
            if child_idx < len(successors):
                work[-1] = (node, child_idx + 1)
                child = successors[child_idx]
                if child not in index_of:
                    index_of[child] = lowlink[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, 0))
                elif child in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[child])
            else:
                # Done with this node — if it's an SCC root, pop the component.
                if lowlink[node] == index_of[node]:
                    component: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == node:
                            break
                    components.append(component)
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])

    return components


# File->file dependency edges, definition-resolved: TypeScript REFERENCES +
# SCIP-resolved Python CALLS only. Shared SSOT — the web layer's
# full_graph() (web/queries.py) imports this same query for its dep edges.
_DEP_EDGE_SQL = """
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

_FILE_SQL = """
SELECT id, json_extract(properties, '$.file_path') AS file_path
FROM nodes
WHERE graph_id = ? AND labels LIKE '%"CodeFile"%'
  AND json_extract(properties, '$.file_path') IS NOT NULL
"""

# rules whose findings this pass owns — cleared before each re-run so re-indexing
# replaces rather than appends.
_ARCH_RULES = ("dependency_cycle", "deep_import", "orphan_module", "upward_import")


async def persist_architecture_diagnostics(
    store: SQLiteGraphStore,
    graph_id: str,
    repo_root: str,
) -> int:
    """Run the whole-graph architecture analysis and persist findings.

    Reads the resolved CodeFile nodes + file->file dep edges from ``store``, runs
    the pure :func:`analyze`, and writes each finding as a ``CodeDiagnostic`` node
    (with a ``HAS_DIAGNOSTIC`` edge to the owning CodeFile) — the same node kind
    the extractors emit, so MCP retrieval, the web dir rollup, and the browser
    paint consume them for free. Idempotent: stale architecture findings are
    deleted before writing, so re-indexing replaces rather than accumulates.

    Returns the number of findings written.
    """
    db = await store._connect()
    try:
        # Map abs file_path -> CodeFile node id (the diagnostic attaches here).
        file_cursor = await db.execute(_FILE_SQL, (graph_id,))
        file_rows = await file_cursor.fetchall()
        abs_to_file_id: dict[str, str] = {r["file_path"]: r["id"] for r in file_rows}

        dep_cursor = await db.execute(_DEP_EDGE_SQL, (graph_id,))
        dep_rows = await dep_cursor.fetchall()
    finally:
        await db.close()

    # Analysis speaks repo-relative paths; keep a rel->abs map to route findings
    # back to the CodeFile node ids.
    rel_to_abs: dict[str, str] = {
        _rel(abs_path, repo_root): abs_path for abs_path in abs_to_file_id
    }
    files = list(rel_to_abs)
    dep_edges = [
        (_rel(r["sf"], repo_root), _rel(r["tf"], repo_root)) for r in dep_rows
    ]

    findings = analyze(files, dep_edges)

    # Clear this pass's prior findings (idempotent re-index).
    await _delete_arch_diagnostics(store, graph_id)

    nodes: list[CodeNode] = []
    edges: list[CodeEdge] = []
    seen_ids: set[str] = set()
    for diag in findings:
        abs_path = rel_to_abs.get(diag.node_id)
        file_id = abs_to_file_id.get(abs_path) if abs_path else None
        if file_id is None:
            continue  # finding for a file no longer in the graph
        diag_id = f"{file_id}:arch:{diag.rule}"
        base, n = diag_id, 0
        while diag_id in seen_ids:
            n += 1
            diag_id = f"{base}:{n}"
        seen_ids.add(diag_id)
        nodes.append(
            CodeNode(
                id=diag_id,
                graph_id=graph_id,
                labels=["CodeDiagnostic"],
                properties={
                    "level": diag.level,
                    "rule": diag.rule,
                    "message": diag.message,
                    # abs file_path so the web rollup ($.file_path) picks it up.
                    "file_path": abs_path,
                    **{k: v for k, v in diag.properties.items() if k != "file_path"},
                },
            )
        )
        edges.append(
            CodeEdge(
                source=file_id, target=diag_id, graph_id=graph_id,
                rel_type="HAS_DIAGNOSTIC", properties={},
            )
        )

    await store.add_nodes_batch(graph_id, nodes)
    await store.add_edges_batch(graph_id, edges)
    return len(nodes)


async def _delete_arch_diagnostics(store: SQLiteGraphStore, graph_id: str) -> None:
    """Remove CodeDiagnostic nodes (and their edges) owned by this pass."""
    db = await store._connect()
    try:
        placeholders = ",".join("?" for _ in _ARCH_RULES)
        cursor = await db.execute(
            f"""
            SELECT id FROM nodes
            WHERE graph_id = ? AND labels LIKE '%"CodeDiagnostic"%'
              AND json_extract(properties, '$.rule') IN ({placeholders})
            """,
            (graph_id, *_ARCH_RULES),
        )
        ids = [r["id"] for r in await cursor.fetchall()]
        if not ids:
            return
        id_ph = ",".join("?" for _ in ids)
        await db.execute(
            f"DELETE FROM edges WHERE graph_id = ? AND target IN ({id_ph})",
            (graph_id, *ids),
        )
        await db.execute(
            f"DELETE FROM nodes WHERE graph_id = ? AND id IN ({id_ph})",
            (graph_id, *ids),
        )
        await db.commit()
    finally:
        await db.close()
