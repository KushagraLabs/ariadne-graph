"""GraphStore protocol definitions.

All graph storage backends implement GraphStore.
Backends supporting vector search also implement SearchableGraphStore.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ariadne_graph.core.models import (
    CodeEdge,
    CodeNode,
    EmbeddingPayload,
    SearchHit,
)


@runtime_checkable
class GraphStore(Protocol):
    """Minimum graph storage interface.

    All persistence backends (memory, SQLite, Neo4j) implement this.
    """

    async def delete_graph(self, graph_id: str) -> None:
        """Delete all nodes and edges for a graph."""
        ...

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        """Delete all nodes, edges, and stored metadata originating from a specific file.

        Implementations should also remove any stored content hash for the file.
        """
        ...

    async def add_nodes_batch(
        self, graph_id: str, nodes: Sequence[CodeNode]
    ) -> None:
        """Add or update nodes in batch (upsert by id)."""
        ...

    async def add_edges_batch(
        self, graph_id: str, edges: Sequence[CodeEdge]
    ) -> None:
        """Add or update edges in batch."""
        ...

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a backend-specific query string.

        Common named queries that all backends should support:

        - ``"nodes"`` — all nodes for *graph_id*
        - ``"edges"`` — all edges for *graph_id*
        - ``"node_by_id"`` — params ``{"node_id": str}``
        - ``"node_by_name"`` — params ``{"name": str}``
        - ``"node_name_fuzzy"`` — params ``{"name": str}``
        - ``"neighbors"`` / ``"node_neighbors"`` — params ``{"node_id": str}``
        - ``"node_edges"`` / ``"node_all_edges"`` — params ``{"node_id": str}``
        - ``"node_outgoing_edges"`` — params ``{"node_id": str}``
        - ``"node_incoming_edges"`` — params ``{"node_id": str}``
        - ``"nodes_by_label"`` — params ``{"label": str}``
        - ``"nodes_by_file"`` — params ``{"file_path": str}``
        - ``"stored_file_paths"`` — all tracked file paths
        - ``"index_metadata"`` — project catalog metadata
        - ``"set_index_metadata"`` — upsert project metadata
        - ``"count_files"`` — distinct file count
        - ``"file_hashes"`` — stored content hashes
        - ``"dirty_files"`` — params ``{"current_hashes": dict[str, str]}``

        Returns list of row dicts. Unknown query names should return an empty list.
        """
        ...

    async def get_stored_hash(
        self, graph_id: str, file_path: str
    ) -> str | None:
        """Return the last indexed content hash for a file, or None."""
        ...

    async def update_hash(
        self, graph_id: str, file_path: str, content_hash: str
    ) -> None:
        """Update the stored content hash for a file."""
        ...

    async def register_project(
        self,
        graph_id: str,
        repo_path: str | Path,
        file_count: int | None = None,
        sync_enabled: bool = False,
    ) -> None:
        """Register or update project metadata in the graph store catalog."""
        ...

    async def list_projects(self) -> list[dict[str, Any]]:
        """Return all registered projects from the graph store catalog.

        Each dict should contain at least ``graph_id`` and ``repo_path``.
        """
        ...

    async def close(self) -> None:
        """Release any resources held by the store (connections, drivers, etc.)."""
        ...


@runtime_checkable
class SearchableGraphStore(GraphStore, Protocol):
    """Extended graph store with vector and keyword search.

    SQLite and Neo4j backends implement this.
    MemoryGraphStore does not (it's for tests only).
    """

    async def upsert_embeddings(
        self,
        graph_id: str,
        rows: Sequence[EmbeddingPayload],
    ) -> None:
        """Store or update embedding vectors for nodes."""
        ...

    async def search_vector(
        self,
        graph_id: str,
        vector: Sequence[float],
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by vector similarity."""
        ...

    async def search_keyword(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by keyword/phrase matching (BM25/FTS)."""
        ...

    async def set_communities(
        self,
        graph_id: str,
        assignments: dict[str, int],
    ) -> None:
        """Store community assignments for nodes."""
        ...

    async def get_communities(
        self,
        graph_id: str,
    ) -> dict[int, list[str]]:
        """Get all community memberships."""
        ...
