"""In-memory graph store for tests and local smoke checks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ariadne_graph.core.models import CodeEdge, CodeNode


class MemoryGraphStore:
    """Simple in-memory graph store.

    Stores nodes and edges in dicts keyed by (graph_id, node_id).
    Content hashes tracked separately.  No persistence across restarts.
    Suitable for tests and quick development checks.
    """

    # Architecture-hygiene capability (bead code_hygiene_mcp-420): the edge scan
    # below reproduces the SQL dep-edge SSOT, so architecture analysis is
    # unit-testable over this backend without a SQLite fixture.
    supports_dep_edges: bool = True

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str], CodeNode] = {}
        self._edges: list[CodeEdge] = []
        self._hashes: dict[tuple[str, str], str] = {}
        self._projects: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        """No-op for the in-memory store."""
        return None

    async def remove_arch_diagnostics(
        self, graph_id: str, rules: Sequence[str]
    ) -> None:
        """Delete CodeDiagnostic nodes (+ their edges) for the given rules.

        The generic-store counterpart to SQLite's SQL delete in
        ``_delete_arch_diagnostics``: lets the architecture pass stay idempotent
        over Memory (a stopped rule's stale finding is cleared, not just
        overwritten by the deterministic id).
        """
        rule_set = set(rules)
        removed_ids: set[str] = set()
        for key, node in list(self._nodes.items()):
            if (
                key[0] == graph_id
                and "CodeDiagnostic" in node.labels
                and node.properties.get("rule") in rule_set
            ):
                removed_ids.add(node.id)
                del self._nodes[key]
        if removed_ids:
            self._edges = [
                e
                for e in self._edges
                if not (e.graph_id == graph_id and e.target in removed_ids)
            ]

    async def dep_edges(self, graph_id: str) -> list[tuple[str, str]]:
        """Cross-file dependency edges — the Python mirror of ``_DEP_EDGE_SQL``.

        Two branches, unioned with multiplicity preserved (one tuple per
        contributing edge, matching the SQL's ``UNION ALL``):

        1. SCIP branch — REFERENCES + scip-resolved Python CALLS, mapped from the
           source/target nodes' owning files, cross-file only (``sf != tf``).
        2. IMPORTS fallback — for a source file with NO cross-file SCIP dep edge,
           an ``IMPORTS``/``IMPORTS_SYMBOL`` edge to a CodeImport node whose
           ``resolved_source`` is an indexed CodeFile contributes that file->file
           edge (import granularity). Gated per source file so a SCIP-covered
           file is served entirely by branch 1 (no double count).
        """
        nodes = {
            nid: node
            for (gid, nid), node in self._nodes.items()
            if gid == graph_id
        }
        edges = [e for e in self._edges if e.graph_id == graph_id]

        def _file_of(node_id: str) -> str | None:
            node = nodes.get(node_id)
            return node.properties.get("file_path") if node else None

        def _is_scip(edge: CodeEdge) -> bool:
            if edge.rel_type == "REFERENCES":
                return True
            return (
                edge.rel_type == "CALLS"
                and edge.properties.get("resolved_by") == "scip-python"
            )

        indexed_files = {
            node.properties["file_path"]
            for node in nodes.values()
            if "CodeFile" in node.labels and node.properties.get("file_path")
        }

        result: list[tuple[str, str]] = []
        scip_covered: set[str] = set()

        # Branch 1: SCIP edges (also seeds scip_covered for the fallback gate).
        for edge in edges:
            if not _is_scip(edge):
                continue
            sf, tf = _file_of(edge.source), _file_of(edge.target)
            if sf and tf and sf != tf:
                result.append((sf, tf))
                scip_covered.add(sf)

        # Branch 2: IMPORTS fallback for source files with no cross-file SCIP edge.
        for edge in edges:
            if edge.rel_type not in ("IMPORTS", "IMPORTS_SYMBOL"):
                continue
            sf = _file_of(edge.source)
            target = nodes.get(edge.target)
            tf = target.properties.get("resolved_source") if target else None
            if (
                sf
                and tf
                and sf != tf
                and tf in indexed_files
                and sf not in scip_covered
            ):
                result.append((sf, tf))

        return result

    async def delete_graph(self, graph_id: str) -> None:
        keys_to_remove = [k for k in self._nodes if k[0] == graph_id]
        for k in keys_to_remove:
            del self._nodes[k]
        self._edges = [e for e in self._edges if e.graph_id != graph_id]
        hash_keys = [k for k in self._hashes if k[0] == graph_id]
        for k in hash_keys:
            del self._hashes[k]
        self._projects.pop(graph_id, None)

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        node_ids_to_remove: set[str] = set()
        for key, node in list(self._nodes.items()):
            if key[0] == graph_id and node.properties.get("file_path") == file_path:
                node_ids_to_remove.add(node.id)
                del self._nodes[key]
        # Delete edges *produced by* this file (tracked via owner_file_path),
        # not every edge merely touching one of its nodes. A cross-file edge
        # such as ``a.caller -> b.save`` is owned by ``a.py``; reindexing
        # ``b.py`` must not remove it. Edges predating owner tracking fall back
        # to source/target membership.
        self._edges = [
            e
            for e in self._edges
            if not (
                e.graph_id == graph_id
                and self._edge_belongs_to_file(e, file_path, node_ids_to_remove)
            )
        ]
        self._hashes.pop((graph_id, file_path), None)

    @staticmethod
    def _edge_belongs_to_file(
        edge: CodeEdge, file_path: str, node_ids: set[str]
    ) -> bool:
        owner = edge.properties.get("owner_file_path")
        if owner is not None:
            return owner == file_path
        return edge.source in node_ids or edge.target in node_ids

    async def add_nodes_batch(
        self, graph_id: str, nodes: Sequence[CodeNode]
    ) -> None:
        for node in nodes:
            self._nodes[(graph_id, node.id)] = node

    async def add_edges_batch(
        self, graph_id: str, edges: Sequence[CodeEdge]
    ) -> None:
        for edge in edges:
            if not any(
                e.graph_id == edge.graph_id and e.source == edge.source
                and e.target == edge.target and e.rel_type == edge.rel_type
                for e in self._edges
            ):
                self._edges.append(edge)

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a simple query.

        Supports:
        - 'nodes': return all nodes for graph_id
        - 'edges': return all edges for graph_id
        - 'node_by_id': params={'node_id': str}
        - 'node_by_name': params={'name': str}
        - 'node_name_fuzzy': params={'name': str}
        - 'node_neighbors': params={'node_id': str} -> nodes connected to node_id
        - 'node_edges', 'node_outgoing_edges', 'node_incoming_edges',
          'node_all_edges': params={'node_id': str}
        - 'nodes_by_label': params={'label': str}
        - 'nodes_by_file': params={'file_path': str}
        - 'stored_file_paths': all tracked file paths
        - 'graphs': all graph ids
        - 'index_metadata', 'set_index_metadata', 'count_files',
          'dirty_files', 'file_hashes': freshness/catalog queries
        """
        params = params or {}

        def _node_row(node: CodeNode) -> dict[str, Any]:
            return {"n": {"id": node.id, "labels": node.labels, "properties": node.properties}}

        def _edge_row(edge: CodeEdge) -> dict[str, Any]:
            return {
                "r": {
                    "source": edge.source,
                    "target": edge.target,
                    "rel_type": edge.rel_type,
                    "properties": edge.properties,
                }
            }

        nodes_for_graph = [n for n in self._nodes.values() if n.graph_id == graph_id]
        edges_for_graph = [e for e in self._edges if e.graph_id == graph_id]

        if query == "nodes":
            return [_node_row(n) for n in nodes_for_graph]

        if query == "edges":
            return [_edge_row(e) for e in edges_for_graph]

        if query == "node_by_id":
            node_id = params.get("node_id", "")
            node = self._nodes.get((graph_id, node_id))
            return [_node_row(node)] if node else []

        if query == "node_by_name":
            name = params.get("name", "")
            for n in nodes_for_graph:
                if n.properties.get("name") == name:
                    return [_node_row(n)]
            return []

        if query == "node_name_fuzzy":
            name = params.get("name", "").lower()
            result: list[dict[str, Any]] = []
            for n in nodes_for_graph:
                nid = n.id.lower()
                props = n.properties
                node_name = str(props.get("name", "")).lower()
                qualname = str(props.get("qualname", "")).lower()
                if (
                    name in nid
                    or name in node_name
                    or name in qualname
                    or nid.endswith(f".{name}")
                    or nid.endswith(f":{name}")
                ):
                    result.append(_node_row(n))
            return result

        if query in ("node_neighbors", "neighbors"):
            node_id = params.get("node_id", "")
            connected: set[str] = set()
            for e in edges_for_graph:
                if e.source == node_id:
                    connected.add(e.target)
                if e.target == node_id:
                    connected.add(e.source)
            return [
                _node_row(self._nodes[(graph_id, cid)])
                for cid in connected
                if (graph_id, cid) in self._nodes
            ]

        if query == "node_edges":
            node_id = params.get("node_id", "")
            return [_edge_row(e) for e in edges_for_graph if e.source == node_id or e.target == node_id]

        if query == "node_outgoing_edges":
            node_id = params.get("node_id", "")
            return [_edge_row(e) for e in edges_for_graph if e.source == node_id]

        if query == "node_incoming_edges":
            node_id = params.get("node_id", "")
            return [_edge_row(e) for e in edges_for_graph if e.target == node_id]

        if query == "node_all_edges":
            return await self.query(graph_id, "node_edges", params)

        if query == "nodes_by_label":
            label = params.get("label", "")
            return [_node_row(n) for n in nodes_for_graph if label in n.labels]

        if query == "nodes_by_file":
            file_path = params.get("file_path", "")
            return [_node_row(n) for n in nodes_for_graph if n.properties.get("file_path") == file_path]

        if query == "stored_file_paths":
            return [{"file_path": p} for (g, p), _ in self._hashes.items() if g == graph_id]

        if query == "graphs":
            graph_ids = {g for g, _ in self._hashes}
            return [
                {"g": {"graph_id": g, "repo_path": "", "last_indexed": ""}}
                for g in graph_ids
            ]

        if query == "index_metadata":
            project = self._projects.get(graph_id)
            if project:
                return [
                    {
                        "repo_path": project.get("repo_path", ""),
                        "last_indexed": project.get("last_indexed"),
                        "file_count": project.get("file_count", 0),
                        "sync_enabled": project.get("sync_enabled", False),
                    }
                ]
            return []

        if query == "set_index_metadata":
            await self.register_project(
                graph_id,
                params.get("repo_path", ""),
                file_count=params.get("file_count", 0),
                sync_enabled=params.get("sync_enabled", False),
                last_indexed=params.get("last_indexed"),
            )
            return []

        if query == "count_files":
            files: set[str] = set()
            for n in nodes_for_graph:
                fp = n.properties.get("file_path")
                if fp:
                    files.add(fp)
            return [{"count": len(files)}]

        if query == "file_hashes":
            return [
                {"file_path": p, "content_hash": h}
                for (g, p), h in self._hashes.items()
                if g == graph_id
            ]

        if query == "dirty_files":
            # Caller supplies current_hashes: dict[str, str] in params.
            current_hashes: dict[str, str] = params.get("current_hashes", {})
            dirty: list[str] = []
            for (g, p), h in self._hashes.items():
                if g == graph_id and current_hashes.get(p) != h:
                    dirty.append(p)
            return [{"file_path": p} for p in dirty]

        return []

    async def get_stored_hash(self, graph_id: str, file_path: str) -> str | None:
        return self._hashes.get((graph_id, file_path))

    async def update_hash(self, graph_id: str, file_path: str, content_hash: str) -> None:
        self._hashes[(graph_id, file_path)] = content_hash

    async def register_project(
        self,
        graph_id: str,
        repo_path: str | Path,
        file_count: int | None = None,
        sync_enabled: bool = False,
        last_indexed: str | None = None,
    ) -> None:
        """Register or update project metadata."""
        now = datetime.now(UTC).isoformat()
        existing = self._projects.get(graph_id, {})
        self._projects[graph_id] = {
            "graph_id": graph_id,
            "repo_path": str(Path(repo_path).resolve()),
            "created_at": existing.get("created_at") or now,
            "last_indexed": last_indexed if last_indexed is not None else now,
            "file_count": file_count if file_count is not None else existing.get("file_count", 0),
            "sync_enabled": sync_enabled,
        }

    async def list_projects(self) -> list[dict[str, Any]]:
        """Return all registered projects."""
        return list(self._projects.values())
