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

    async def compute_freshness(
        self,
        graph_id: str,
        dirty_hint: int | None = None,
    ) -> dict[str, object] | None:
        """Cheap in-band freshness envelope for an analysis/query response.

        Returns ``{last_indexed, dirty_file_count, stale, sync_enabled}`` or
        ``None`` when the graph has never been indexed (no metadata row).

        Staleness source is an **mtime prefilter** (bead decision (a)): every
        stored file is ``stat``-ed and only those whose ``st_mtime`` is newer
        than the repo's ``last_indexed`` timestamp are re-hashed to confirm a
        real content change. Stat-only over the file set is O(repo) syscalls but
        avoids the O(repo) *reads+hashes* that :meth:`_compute_dirty_files` does
        unconditionally, so it stays cheap on large repos. When the repo path is
        unavailable (metadata has no path — bead fallback (c)) it degrades to a
        timestamp-only envelope: ``dirty_file_count`` is unknown so ``stale``
        cannot be asserted — both are reported ``None`` (unknown).

        ``dirty_hint`` lets a caller that already knows the dirty count (e.g. a
        watcher-tracked dirty set) skip the scan entirely; when given it is used
        verbatim and no filesystem work happens.
        """
        rows = await self.graph_store.query(
            graph_id,
            "index_metadata",
            params={"graph_id": graph_id},
        )
        if not rows:
            return None

        meta = rows[0]
        last_indexed = meta.get("last_indexed")
        sync_enabled = bool(meta.get("sync_enabled", False))
        repo_path = meta.get("repo_path", "")

        if dirty_hint is not None:
            return {
                "last_indexed": last_indexed,
                "dirty_file_count": dirty_hint,
                "stale": dirty_hint > 0,
                "sync_enabled": sync_enabled,
            }

        if not repo_path or last_indexed is None:
            # Fallback (c): timestamp-only. No path or no index time to compare
            # mtimes against, so dirty count is genuinely unknown. Report both
            # count and staleness as None (unknown) rather than a misleading
            # zero/False that would read as "confirmed fresh".
            return {
                "last_indexed": last_indexed,
                "dirty_file_count": None,
                "stale": None,
                "sync_enabled": sync_enabled,
            }

        dirty_count = await self._count_dirty_mtime_prefilter(graph_id, repo_path, last_indexed)
        return {
            "last_indexed": last_indexed,
            "dirty_file_count": dirty_count,
            "stale": dirty_count > 0,
            "sync_enabled": sync_enabled,
        }

    async def _count_dirty_mtime_prefilter(
        self,
        graph_id: str,
        repo_path: str,
        last_indexed: str,
    ) -> int:
        """Count stored files whose content changed since ``last_indexed``.

        Prefilters on ``st_mtime``: a file is only read+hashed when its mtime is
        NOT strictly older than the index timestamp, so an unchanged repo pays
        one ``stat`` per file and zero reads. An equal mtime is deliberately NOT
        skipped — coarse timestamp resolution, mtime-preserving tooling, or
        restored timestamps can leave a modified file at exactly the cutoff, so
        it is hash-verified rather than trusted. A missing file counts as dirty
        (it was indexed and is now gone).

        NOTE (known limitation, surfaced not silent): this counts only files
        that were indexed (have a ``file_hashes`` row). A brand-new untracked
        file on disk is NOT reflected here, because discovering the full current
        file set per analysis call would reintroduce the O(repo) walk this cheap
        path exists to avoid. New-file staleness is caught by
        ``code_graph_detect_changes`` / a reindex, not this envelope.
        """
        rows = await self.graph_store.query(
            graph_id,
            "file_hashes",
            params={"graph_id": graph_id},
        )
        if not rows:
            return 0

        try:
            cutoff = datetime.fromisoformat(last_indexed).timestamp()
        except (ValueError, TypeError):
            cutoff = None

        root = Path(repo_path)
        dirty = 0
        for row in rows:
            file_path = row.get("file_path", "")
            stored_hash = row.get("content_hash", "")
            if not file_path:
                continue
            path = Path(file_path)
            if not path.is_absolute():
                path = root / path
            try:
                st = path.stat()
            except OSError:
                dirty += 1  # indexed file now missing/unreadable
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue  # strictly older than index — trust the stored hash
            try:
                current_hash = xxhash.xxh3_64_hexdigest(path.read_bytes())
            except OSError:
                dirty += 1
                continue
            if current_hash != stored_hash:
                dirty += 1
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
