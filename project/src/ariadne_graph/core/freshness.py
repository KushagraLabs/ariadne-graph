"""Index freshness tracking — monitors when a repository was last indexed."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import xxhash

from ariadne_graph.core.models import IndexStatus
from ariadne_graph.graphstores.base import GraphStore

logger = logging.getLogger(__name__)


class FreshnessTracker:
    """Tracks indexing status and freshness for repositories.

    Uses the graph store to persist last-indexed timestamps and
    supports incremental sync decisions.
    """

    def __init__(self, graph_store: GraphStore) -> None:
        self.graph_store = graph_store

    async def get_status(
        self,
        graph_id: str,
        repo_path: str | None = None,
    ) -> IndexStatus:
        """Return the current indexing status for a graph.

        Queries the graph store for metadata about the last indexing run,
        file count, and sync configuration. Computes dirty files by comparing
        stored content hashes with the current working-tree content.

        Args:
            graph_id: The repository graph identifier.
            repo_path: Optional repo path override. If omitted, the path
                stored in the graph catalog is used.

        Returns:
            IndexStatus with current state.
        """
        # Query for stored metadata
        rows = await self.graph_store.query(
            graph_id,
            "index_metadata",
            params={"graph_id": graph_id},
        )

        last_indexed: str | None = None
        file_count = 0
        resolved_repo_path = repo_path or ""
        sync_enabled = False

        if rows:
            meta = rows[0]
            last_indexed = meta.get("last_indexed")
            file_count = meta.get("file_count", 0)
            resolved_repo_path = repo_path or meta.get("repo_path", "")
            sync_enabled = bool(meta.get("sync_enabled", False))

        # Compute dirty files by comparing stored hashes with current content
        dirty_files: list[str] = []
        if resolved_repo_path:
            dirty_files = await self._compute_dirty_files(graph_id, resolved_repo_path)

        return IndexStatus(
            graph_id=graph_id,
            repo_path=resolved_repo_path,
            last_indexed=last_indexed,
            file_count=file_count,
            dirty_files=dirty_files,
            sync_enabled=sync_enabled,
        )

    async def _compute_dirty_files(
        self,
        graph_id: str,
        repo_path: str,
    ) -> list[str]:
        """Compare stored file hashes with files on disk.

        Args:
            graph_id: The repository graph identifier.
            repo_path: Absolute path to the repository root.

        Returns:
            List of file paths whose content has changed since indexing.
        """
        rows = await self.graph_store.query(
            graph_id,
            "file_hashes",
            params={"graph_id": graph_id},
        )
        if not rows:
            return []

        root = Path(repo_path)
        dirty: list[str] = []
        for row in rows:
            file_path = row.get("file_path", "")
            stored_hash = row.get("content_hash", "")
            if not file_path:
                continue
            path = Path(file_path)
            if not path.is_absolute():
                path = root / path
            try:
                content = path.read_bytes()
                current_hash = xxhash.xxh3_64_hexdigest(content)
                if current_hash != stored_hash:
                    dirty.append(file_path)
            except Exception as exc:
                logger.debug("Failed to hash %s during freshness check: %s", file_path, exc)
                dirty.append(file_path)
        return dirty

    async def mark_indexed(
        self,
        graph_id: str,
        repo_path: str,
        file_count: int | None = None,
        sync_enabled: bool = False,
    ) -> None:
        """Record that a graph has been freshly indexed.

        Updates the last_indexed timestamp and file count in the graph store.

        Args:
            graph_id: The repository graph identifier.
            repo_path: Absolute path to the repository root.
            file_count: Optional explicit file count. If omitted, it is counted
                from the nodes stored in the graph.
            sync_enabled: Whether background auto-sync is enabled.
        """
        now = datetime.now(UTC).isoformat()
        resolved = str(Path(repo_path).resolve())

        if file_count is None:
            # Count indexed files via query
            rows = await self.graph_store.query(
                graph_id,
                "count_files",
                params={"graph_id": graph_id},
            )
            file_count = rows[0].get("count", 0) if rows else 0

        # Store the metadata update — backends handle this via query
        await self.graph_store.query(
            graph_id,
            "set_index_metadata",
            params={
                "graph_id": graph_id,
                "repo_path": resolved,
                "last_indexed": now,
                "file_count": file_count,
                "sync_enabled": sync_enabled,
            },
        )

    async def is_fresh(
        self,
        graph_id: str,
        max_age_seconds: float = 3600.0,
    ) -> bool:
        """Check if the index is fresh (indexed within max_age_seconds).

        Args:
            graph_id: The repository graph identifier.
            max_age_seconds: Maximum age in seconds to consider fresh.

        Returns:
            True if the index was updated within the time window.
        """
        status = await self.get_status(graph_id)
        if status.last_indexed is None:
            return False

        try:
            last_indexed_dt = datetime.fromisoformat(status.last_indexed)
            now = datetime.now(UTC)
            age_seconds = (now - last_indexed_dt).total_seconds()
            return age_seconds <= max_age_seconds
        except (ValueError, TypeError):
            return False

    async def get_file_hashes(
        self,
        graph_id: str,
    ) -> dict[str, str]:
        """Get all stored content hashes for a graph.

        Args:
            graph_id: The repository graph identifier.

        Returns:
            Mapping of file_path -> content_hash.
        """
        rows = await self.graph_store.query(
            graph_id,
            "file_hashes",
            params={"graph_id": graph_id},
        )
        return {
            row.get("file_path", ""): row.get("content_hash", "")
            for row in rows
            if row.get("file_path")
        }
