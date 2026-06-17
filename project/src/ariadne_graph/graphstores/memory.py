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

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str], CodeNode] = {}
        self._edges: list[CodeEdge] = []
        self._hashes: dict[tuple[str, str], str] = {}
        self._projects: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        """No-op for the in-memory store."""
        return None

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
        self._edges = [
            e for e in self._edges
            if not (e.graph_id == graph_id and (e.source in node_ids_to_remove or e.target in node_ids_to_remove))
        ]
        self._hashes.pop((graph_id, file_path), None)

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
