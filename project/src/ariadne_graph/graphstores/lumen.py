"""Lumen KG compatibility adapter.

Provides an optional ``LumenGraphStore`` that wraps a concrete ``GraphStore``
backend and adds Lumen-style metadata, query aliases, and workspace-root
restrictions.  The adapter keeps the core package repo-agnostic: Lumen-specific
behavior lives entirely in this module and is only loaded when enabled.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ariadne_graph.core.models import CodeEdge, CodeNode, EmbeddingPayload, SearchHit
from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore

logger = logging.getLogger(__name__)


class LumenGraphStore:
    """Compatibility shim that wraps a concrete graph store for Lumen integration.

    The delegate store does the actual persistence.  This layer adds:

    - ``lumen_*`` query aliases (``lumen_nodes``, ``lumen_edges``,
      ``lumen_project_metadata``) for callers expecting Lumen KG naming.
    - Workspace-root enforcement: queries can be restricted so that a Lumen
      workspace only sees projects under its configured root.
    - Lumen-style project metadata fields (``lumen_workspace_id``,
      ``lumen_project_key``) stored alongside the canonical catalog.

    If the delegate implements ``SearchableGraphStore`` this wrapper also
    implements the searchable methods by forwarding them.
    """

    def __init__(
        self,
        delegate: GraphStore,
        workspace_root: Path | str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        """Wrap a concrete graph store.

        Args:
            delegate: The real graph store backend (e.g. SQLiteGraphStore).
            workspace_root: Optional root path. When set, ``register_project``
                rejects repo paths outside this directory.
            workspace_id: Optional Lumen workspace identifier stored in metadata.
        """
        self._delegate = delegate
        self.workspace_root = (
            Path(workspace_root).resolve() if workspace_root else None
        )
        self.workspace_id = workspace_id

    @property
    def delegate(self) -> GraphStore:
        """Return the wrapped graph store."""
        return self._delegate

    async def close(self) -> None:
        """Close the delegate store."""
        await self._delegate.close()

    @property
    def supports_dep_edges(self) -> bool:
        """Mirror the delegate's dep_edges capability (bead code_hygiene_mcp-420).

        A Lumen wrapper around a SQLite/Memory delegate MUST NOT silently lose
        architecture hygiene — the exact silent-skip this bead removed. Reported
        as a property so it tracks the delegate rather than freezing at wrap time.
        """
        return getattr(self._delegate, "supports_dep_edges", False)

    async def dep_edges(self, graph_id: str) -> list[tuple[str, str]]:
        """Forward the dep-edge capability to the delegate."""
        return await self._delegate.dep_edges(graph_id)

    async def remove_arch_diagnostics(
        self, graph_id: str, rules: Sequence[str]
    ) -> None:
        """Forward generic stale-diagnostic deletion to the delegate.

        A Lumen-wrapped Memory delegate (factory fallback with Lumen enabled)
        runs the architecture pass via the forwarded ``dep_edges``; without this
        forward its ``_delete_arch_diagnostics`` generic branch would find no
        remover on the wrapper and leave stale findings, breaking re-index
        idempotency. Delegates that lack the helper (SQLite uses SQL delete)
        simply have nothing to forward.
        """
        remover = getattr(self._delegate, "remove_arch_diagnostics", None)
        if remover is not None:
            await remover(graph_id, rules)

    def _is_path_allowed(self, repo_path: str | Path) -> bool:
        """Return True when *repo_path* is under the configured workspace root."""
        if self.workspace_root is None:
            return True
        try:
            resolved = Path(repo_path).resolve()
            # relative_to raises ValueError when resolved is not under workspace_root.
            resolved.relative_to(self.workspace_root)
            return True
        except (ValueError, OSError):
            return False

    def _lumen_metadata(self) -> dict[str, Any]:
        """Return Lumen-specific metadata fields to merge into catalog rows."""
        meta: dict[str, Any] = {"lumen_adapter_version": "1"}
        if self.workspace_id:
            meta["lumen_workspace_id"] = self.workspace_id
        if self.workspace_root:
            meta["lumen_workspace_root"] = str(self.workspace_root)
        return meta

    # ------------------------------------------------------------------
    # GraphStore protocol
    # ------------------------------------------------------------------

    async def delete_graph(self, graph_id: str) -> None:
        """Delete all data for a graph."""
        await self._delegate.delete_graph(graph_id)

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        """Delete all facts originating from a file."""
        await self._delegate.delete_file_facts(graph_id, file_path)

    async def add_nodes_batch(
        self, graph_id: str, nodes: Sequence[CodeNode]
    ) -> None:
        """Add or update nodes."""
        await self._delegate.add_nodes_batch(graph_id, nodes)

    async def add_edges_batch(
        self, graph_id: str, edges: Sequence[CodeEdge]
    ) -> None:
        """Add or update edges."""
        await self._delegate.add_edges_batch(graph_id, edges)

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a query, supporting Lumen aliases.

        Lumen aliases are translated to the canonical query names and then
        forwarded to the delegate.  Results are annotated with Lumen metadata
        when appropriate.
        """
        params = params or {}

        # Translate Lumen-style aliases to canonical names.
        lumen_aliases = {
            "lumen_nodes": "nodes",
            "lumen_edges": "edges",
            "lumen_node_by_id": "node_by_id",
            "lumen_node_by_name": "node_by_name",
            "lumen_neighbors": "neighbors",
            "lumen_project_metadata": "index_metadata",
            "lumen_file_hashes": "file_hashes",
            "lumen_stored_file_paths": "stored_file_paths",
            "lumen_count_files": "count_files",
        }
        canonical_query = lumen_aliases.get(query, query)

        rows = await self._delegate.query(graph_id, canonical_query, params)

        # Annotate catalog metadata results with Lumen context.
        if canonical_query == "index_metadata" and rows:
            meta = self._lumen_metadata()
            rows = [{**row, **meta} for row in rows]

        return rows

    async def get_stored_hash(
        self, graph_id: str, file_path: str
    ) -> str | None:
        """Return the stored content hash for a file."""
        return await self._delegate.get_stored_hash(graph_id, file_path)

    async def update_hash(
        self, graph_id: str, file_path: str, content_hash: str
    ) -> None:
        """Update the stored content hash for a file."""
        await self._delegate.update_hash(graph_id, file_path, content_hash)

    async def register_project(
        self,
        graph_id: str,
        repo_path: str | Path,
        file_count: int | None = None,
        sync_enabled: bool = False,
    ) -> None:
        """Register a project, enforcing workspace-root restrictions."""
        if not self._is_path_allowed(repo_path):
            logger.warning(
                "Lumen workspace %s rejecting repo path outside workspace: %s",
                self.workspace_id,
                repo_path,
            )
            return

        await self._delegate.register_project(
            graph_id, repo_path, file_count=file_count, sync_enabled=sync_enabled
        )

    async def list_projects(self) -> list[dict[str, Any]]:
        """List projects, annotating with Lumen metadata."""
        projects = await self._delegate.list_projects()
        meta = self._lumen_metadata()
        return [{**project, **meta} for project in projects]

    # ------------------------------------------------------------------
    # SearchableGraphStore protocol (forwarded when delegate supports it)
    # ------------------------------------------------------------------

    async def upsert_embeddings(
        self,
        graph_id: str,
        rows: Sequence[EmbeddingPayload],
    ) -> None:
        """Store embedding vectors for nodes."""
        if isinstance(self._delegate, SearchableGraphStore):
            await self._delegate.upsert_embeddings(graph_id, rows)

    async def search_vector(
        self,
        graph_id: str,
        vector: Sequence[float],
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by vector similarity."""
        if isinstance(self._delegate, SearchableGraphStore):
            return await self._delegate.search_vector(graph_id, vector, limit=limit)
        return []

    async def search_keyword(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by keyword matching."""
        if isinstance(self._delegate, SearchableGraphStore):
            return await self._delegate.search_keyword(graph_id, query, limit=limit)
        return []

    async def set_communities(
        self,
        graph_id: str,
        assignments: dict[str, int],
    ) -> None:
        """Store community assignments."""
        if isinstance(self._delegate, SearchableGraphStore):
            await self._delegate.set_communities(graph_id, assignments)

    async def get_communities(
        self,
        graph_id: str,
    ) -> dict[int, list[str]]:
        """Return community memberships."""
        if isinstance(self._delegate, SearchableGraphStore):
            return await self._delegate.get_communities(graph_id)
        return {}
