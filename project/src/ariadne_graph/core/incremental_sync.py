"""Content-hash incremental sync for the Ariadne Graph code graph.

The IncrementalSync class manages efficient re-indexing by comparing
content hashes of source files. Only files whose content has changed
since the last indexing run are re-parsed and re-inserted into the
graph store.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles  # type: ignore[import-untyped]
import xxhash

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.models import ChangeReport
from ariadne_graph.languages.base import ExtractionContext

if TYPE_CHECKING:
    from ariadne_graph.graphstores.base import GraphStore
    from ariadne_graph.languages.base import LanguageAdapter

logger = logging.getLogger(__name__)


class IncrementalSync:
    """Manages incremental synchronization of a repository into a graph store.

    Uses content hashing (xxhash) to detect changed files and only re-parses
    those files, deleting old facts before inserting new ones.  Also handles
    files that have been removed from the repository.
    """

    def __init__(
        self,
        graph_store: GraphStore,
        config: AnalyzerConfig,
    ) -> None:
        self.graph_store = graph_store
        self.config = config
        self.graph_id = config.graph_id

    # ------------------------------------------------------------------
    # Hash computation
    # ------------------------------------------------------------------

    @staticmethod
    async def compute_file_hash(file_path: Path) -> str:
        """Read a file and return its xxhash3-64 hex digest.

        Args:
            file_path: Path to the file to hash.

        Returns:
            Hex-encoded xxhash3-64 digest of the file contents.
        """
        try:
            async with aiofiles.open(file_path, "rb") as f:
                content = await f.read()
            return xxhash.xxh3_64_hexdigest(content)
        except Exception as exc:
            logger.warning("Failed to hash %s: %s", file_path, exc)
            # Return a sentinel hash that will never match a real hash
            return f"error:{file_path}:{exc!s}"

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    async def get_changed_files(
        self,
        graph_id: str,
        file_paths: list[Path],
    ) -> tuple[list[Path], list[Path], list[Path]]:
        """Compare current files with stored hashes to find changes.

        Args:
            graph_id: The graph identifier.
            file_paths: Current list of file paths on disk.

        Returns:
            A tuple of (changed, unchanged, removed) file path lists.
            - changed: files whose content hash differs from stored
            - unchanged: files whose content hash matches stored
            - removed: files previously indexed but no longer present
        """
        # Build set of current file paths (as strings for comparison)
        current_paths = {str(p.resolve()) for p in file_paths}

        changed: list[Path] = []
        unchanged: list[Path] = []

        for file_path in file_paths:
            path_str = str(file_path.resolve())
            stored_hash = await self.graph_store.get_stored_hash(graph_id, path_str)
            current_hash = await self.compute_file_hash(file_path)

            if stored_hash is None or stored_hash != current_hash:
                changed.append(file_path)
            else:
                unchanged.append(file_path)

        # Find removed files: in store but not in current list
        removed_strs = await self._get_stored_files_not_in(graph_id, current_paths)
        removed = [Path(p) for p in removed_strs]

        return changed, unchanged, removed

    async def _git_resolve_ref(self, since_ref: str) -> tuple[str | None, str]:
        """Resolve a git ref to a commit SHA.

        Args:
            since_ref: The git ref to resolve (e.g. 'HEAD', a branch, or SHA).

        Returns:
            Tuple of (resolved_sha, error_message). On success error_message is empty.
        """
        repo_root = str(self.config.resolved_repo_root)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            repo_root,
            "rev-parse",
            f"{since_ref}^{{commit}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return None, stderr.decode().strip()
        return stdout.decode().strip(), ""

    async def _git_tracked_files(self) -> set[str]:
        """Return the set of absolute paths currently tracked by git."""
        repo_root = self.config.resolved_repo_root
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return set()
        return {str(repo_root / line.rstrip("\r")) for line in stdout.decode().splitlines()}

    async def _git_diff_name_status(
        self, resolved_ref: str
    ) -> tuple[dict[str, str], list[str], str]:
        """Run git diff --name-status against a resolved ref.

        Args:
            resolved_ref: The resolved commit SHA.

        Returns:
            Tuple of (path_to_status, deleted_paths, error_message).
            path_to_status maps absolute file paths to their one-letter git status.
            deleted_paths lists absolute paths reported as deleted by git.
        """
        repo_root = self.config.resolved_repo_root
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--name-status",
            resolved_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {}, [], stderr.decode().strip()

        path_status: dict[str, str] = {}
        deleted: list[str] = []
        for line in stdout.decode().splitlines():
            line = line.rstrip("\r")
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            if status.startswith("A"):
                path_status[str(repo_root / parts[1])] = "A"
            elif status.startswith("M"):
                path_status[str(repo_root / parts[1])] = "M"
            elif status.startswith("D"):
                deleted_path = str(repo_root / parts[1])
                path_status[deleted_path] = "D"
                deleted.append(deleted_path)
            elif status.startswith("R") and len(parts) >= 3:
                old_path = str(repo_root / parts[1])
                new_path = str(repo_root / parts[2])
                path_status[old_path] = "D"
                path_status[new_path] = "A"
                deleted.append(old_path)
            elif status.startswith("C") and len(parts) >= 3:
                new_path = str(repo_root / parts[2])
                path_status[new_path] = "A"

        return path_status, deleted, ""

    async def get_changed_files_since_ref(
        self,
        graph_id: str,
        file_paths: list[Path],
        since_ref: str,
    ) -> ChangeReport:
        """Detect changes by comparing the working tree against a git ref.

        Files tracked by git are classified using ``git diff --name-status``.
        Files not tracked by git (or not reported by git) are compared against
        their stored content hashes.

        Args:
            graph_id: The graph identifier.
            file_paths: Current list of file paths on disk.
            since_ref: The git ref to compare against.

        Returns:
            A ChangeReport summarizing added, modified, and deleted files.
        """
        resolved_ref, error = await self._git_resolve_ref(since_ref)

        if resolved_ref is None:
            if "not a git repository" in error.lower():
                changed, _unchanged, removed = await self.get_changed_files(
                    graph_id, file_paths
                )
                fallback_added, fallback_modified = await self._classify_by_stored_hash(
                    graph_id, changed
                )
                return ChangeReport(
                    added=fallback_added,
                    modified=fallback_modified,
                    deleted=[str(p) for p in removed],
                    since_ref=since_ref,
                    comparison_mode="stored_hash",
                    message=(
                        "Git repository not found; falling back to stored-hash comparison: "
                        f"{error}"
                    ),
                )
            return ChangeReport(
                since_ref=since_ref,
                comparison_mode="stored_hash",
                message=f"Invalid git ref '{since_ref}': {error}",
            )

        tracked_files = await self._git_tracked_files()
        path_status, git_deleted, diff_error = await self._git_diff_name_status(
            resolved_ref
        )
        if diff_error:
            changed, _unchanged, removed = await self.get_changed_files(
                graph_id, file_paths
            )
            fallback_added, fallback_modified = await self._classify_by_stored_hash(
                graph_id, changed
            )
            return ChangeReport(
                added=fallback_added,
                modified=fallback_modified,
                deleted=[str(p) for p in removed],
                since_ref=since_ref,
                resolved_ref=resolved_ref,
                comparison_mode="stored_hash",
                message=f"Git diff failed; falling back to stored-hash comparison: {diff_error}",
            )

        added: list[str] = []
        modified: list[str] = []

        for file_path in file_paths:
            path_str = str(file_path.resolve())
            if path_str in path_status:
                status = path_status[path_str]
                if status == "A":
                    added.append(path_str)
                elif status == "M":
                    modified.append(path_str)
                continue

            if path_str in tracked_files:
                # Tracked and unchanged relative to the ref.
                continue

            # Untracked file: fall back to stored-hash comparison.
            stored_hash = await self.graph_store.get_stored_hash(graph_id, path_str)
            current_hash = await self.compute_file_hash(file_path)
            if stored_hash is None:
                added.append(path_str)
            elif stored_hash != current_hash:
                modified.append(path_str)

        # Only report deletions for files the graph store knows about.
        deleted: list[str] = []
        for deleted_path in git_deleted:
            stored_hash = await self.graph_store.get_stored_hash(
                graph_id, deleted_path
            )
            if stored_hash is not None:
                deleted.append(deleted_path)

        return ChangeReport(
            added=added,
            modified=modified,
            deleted=deleted,
            since_ref=since_ref,
            resolved_ref=resolved_ref,
            comparison_mode="git_ref",
            message=(
                f"Changes since {since_ref} ({resolved_ref}): "
                f"+{len(added)} ~{len(modified)} -{len(deleted)}"
            ),
        )

    async def _classify_by_stored_hash(
        self, graph_id: str, changed: list[Path]
    ) -> tuple[list[str], list[str]]:
        """Classify changed paths into added (no stored hash) or modified."""
        added: list[str] = []
        modified: list[str] = []
        for file_path in changed:
            path_str = str(file_path.resolve())
            stored_hash = await self.graph_store.get_stored_hash(graph_id, path_str)
            if stored_hash is None:
                added.append(path_str)
            else:
                modified.append(path_str)
        return added, modified

    async def _get_stored_files_not_in(
        self, graph_id: str, current_paths: set[str]
    ) -> list[str]:
        """Return file paths stored for graph_id that are not in current_paths."""
        try:
            rows = await self.graph_store.query(
                graph_id,
                "stored_file_paths",
                {},
            )
            stored = [r["file_path"] for r in rows]
            return [p for p in stored if p not in current_paths]
        except Exception:
            # If the query isn't supported, fall back to an empty list.
            return []

    # ------------------------------------------------------------------
    # Single-file sync
    # ------------------------------------------------------------------

    async def sync_file(
        self,
        adapter: LanguageAdapter,
        file_path: Path,
        context: ExtractionContext,
    ) -> bool:
        """Synchronize a single file into the graph store.

        Deletes old facts for the file, extracts new facts, stores them,
        and updates the content hash.

        Args:
            adapter: Language adapter for extraction.
            file_path: Path to the source file.
            context: Extraction context (graph_id, repo_root, etc.).

        Returns:
            True if the file was successfully synced, False on error.
        """
        path_str = str(file_path.resolve())
        graph_id = context.graph_id

        try:
            # 1. Delete old facts for this file
            await self.graph_store.delete_file_facts(graph_id, path_str)

            # 2. Extract new facts (run sync extraction in thread pool)
            delta = await asyncio.to_thread(adapter.extract_file, file_path, context)

            # 3. Store new nodes and edges
            if delta.nodes:
                await self.graph_store.add_nodes_batch(graph_id, delta.nodes)
            if delta.edges:
                await self.graph_store.add_edges_batch(graph_id, delta.edges)

            # 4. Update the content hash
            await self.graph_store.update_hash(graph_id, path_str, delta.content_hash)

            logger.debug(
                "Synced %s (%d nodes, %d edges)",
                path_str,
                len(delta.nodes),
                len(delta.edges),
            )
            return True

        except Exception as exc:
            logger.warning("Failed to sync %s: %s", path_str, exc)
            return False

    # ------------------------------------------------------------------
    # Full repository sync
    # ------------------------------------------------------------------

    async def full_sync(
        self,
        adapter: LanguageAdapter,
        config: AnalyzerConfig | None = None,
        all_known_files: list[Path] | None = None,
    ) -> ChangeReport:
        """Run a full incremental sync of the repository.

        Discovers all source files, detects changes, syncs changed files,
        and cleans up removed files.

        Args:
            adapter: Language adapter for file discovery and extraction.
            config: Optional override config (uses self.config if None).
            all_known_files: Optional union of all current files across adapters.
                When provided, removed-file cleanup uses this set instead of
                only this adapter's files. This prevents one adapter from
                deleting facts stored by another adapter.

        Returns:
            A ChangeReport summarizing added, modified, deleted, and
            unchanged files.
        """
        cfg = config or self.config
        graph_id = cfg.graph_id
        repo_root = cfg.resolved_repo_root
        if graph_id is None:
            raise ValueError("AnalyzerConfig.graph_id must be set before syncing")

        # 1. Discover all source files. This is a synchronous, potentially slow
        # filesystem walk (scandir over the whole repo tree) — offload it to a
        # worker thread so the daemon's event loop stays free to serve HTTP
        # (/graph, /api/graph/*) while a large repo is being (re)discovered.
        all_files = await asyncio.to_thread(adapter.discover_files, repo_root, cfg)
        logger.info(
            "Discovered %d %s files in %s",
            len(all_files),
            adapter.language,
            repo_root,
        )

        # 2. Detect changes
        changed, unchanged, _removed = await self.get_changed_files(
            graph_id,
            all_files,
        )

        # Build extraction context
        context = ExtractionContext(
            graph_id=graph_id,
            repo_root=repo_root,
            source_commit=None,
            all_files=all_files,
            changed_files=changed,
        )

        # 3. Optional project-wide preparation (e.g. SCIP indexing)
        try:
            await adapter.prepare_project(
                context, cfg, all_files, changed, self.graph_store
            )
        except Exception as exc:
            logger.warning(
                "Project-wide preparation failed for %s adapter: %s",
                adapter.language,
                exc,
            )

        # Use the union of all known files for cleanup when multiple adapters
        # are indexed in the same run.
        if all_known_files is not None:
            known_paths = {str(p.resolve()) for p in all_known_files}
            removed_strs = await self._get_stored_files_not_in(graph_id, known_paths)
            removed = [Path(p) for p in removed_strs]
        else:
            removed = _removed

        logger.info(
            "Changes: %d changed, %d unchanged, %d removed",
            len(changed),
            len(unchanged),
            len(removed),
        )

        # Pre-classify changed files as added (no stored hash) vs modified
        added_paths: list[str] = []
        modified_paths: list[str] = []
        for file_path in changed:
            path_str = str(file_path.resolve())
            stored_hash = await self.graph_store.get_stored_hash(graph_id, path_str)
            if stored_hash is None:
                added_paths.append(path_str)
            else:
                modified_paths.append(path_str)

        # 3. Sync changed files
        success_count = 0
        for file_path in changed:
            ok = await self.sync_file(adapter, file_path, context)
            if ok:
                success_count += 1

        logger.info(
            "Successfully synced %d/%d changed files",
            success_count,
            len(changed),
        )

        # 4. Clean up removed files
        deleted_paths: list[str] = []
        for file_path in removed:
            path_str = str(file_path)
            try:
                await self.graph_store.delete_file_facts(graph_id, path_str)
                deleted_paths.append(path_str)
            except Exception as exc:
                logger.warning("Failed to clean up removed file %s: %s", path_str, exc)

        # 5. Build change report
        report = ChangeReport(
            added=added_paths,
            modified=modified_paths,
            deleted=deleted_paths,
            unchanged=[str(p.resolve()) for p in unchanged],
        )

        return report

    # ------------------------------------------------------------------
    # Targeted sync for a known set of changed files
    # ------------------------------------------------------------------

    async def targeted_sync(
        self,
        adapter: LanguageAdapter,
        changed_files: list[Path],
        all_known_files: list[Path] | None = None,
    ) -> ChangeReport:
        """Sync only a specific set of changed files for one adapter.

        Discovers the current file set so removed-file cleanup remains
        correct, but only re-parses the supplied ``changed_files``. This is
        the entry point used by the filesystem watcher.

        Args:
            adapter: Language adapter for file discovery and extraction.
            changed_files: Files that the watcher detected as changed.
            all_known_files: Optional union of all current files across
                adapters. Used for removed-file cleanup in multi-adapter
                repos so one adapter does not delete another adapter's
                facts.

        Returns:
            A ChangeReport summarizing the sync.
        """
        cfg = self.config
        graph_id = cfg.graph_id
        repo_root = cfg.resolved_repo_root
        if graph_id is None:
            raise ValueError("AnalyzerConfig.graph_id must be set before syncing")
        if not changed_files:
            return ChangeReport(
                added=[],
                modified=[],
                deleted=[],
                unchanged=[],
                message="No changed files to sync",
            )

        # 1. Discover all current files for this adapter (needed for context
        #    and removed-file detection).
        all_files = adapter.discover_files(repo_root, cfg)
        all_file_paths = {str(p.resolve()) for p in all_files}

        # 2. Validate supplied changed files: drop files this adapter does not
        #    own and de-duplicate.
        owned_changed: list[Path] = []
        seen: set[str] = set()
        for file_path in changed_files:
            path_str = str(file_path.resolve())
            if path_str not in all_file_paths:
                continue
            if path_str in seen:
                continue
            seen.add(path_str)
            owned_changed.append(file_path)

        if not owned_changed:
            return ChangeReport(
                added=[],
                modified=[],
                deleted=[],
                unchanged=[],
                message="None of the changed files are owned by this adapter",
            )

        # 3. Build extraction context with the exact changed set.
        context = ExtractionContext(
            graph_id=graph_id,
            repo_root=repo_root,
            source_commit=None,
            all_files=all_files,
            changed_files=owned_changed,
        )

        # 4. Optional project-wide preparation (e.g. SCIP for TypeScript).
        try:
            await adapter.prepare_project(
                context, cfg, all_files, owned_changed, self.graph_store
            )
        except Exception as exc:
            logger.warning(
                "Project-wide preparation failed for %s adapter during targeted sync: %s",
                adapter.language,
                exc,
            )

        # 5. Classify changed files as added vs modified.
        added_paths: list[str] = []
        modified_paths: list[str] = []
        for file_path in owned_changed:
            path_str = str(file_path.resolve())
            stored_hash = await self.graph_store.get_stored_hash(graph_id, path_str)
            if stored_hash is None:
                added_paths.append(path_str)
            else:
                modified_paths.append(path_str)

        # 6. Sync only the changed files.
        success_count = 0
        for file_path in owned_changed:
            ok = await self.sync_file(adapter, file_path, context)
            if ok:
                success_count += 1

        logger.info(
            "Targeted sync for %s: synced %d/%d changed files",
            adapter.language,
            success_count,
            len(owned_changed),
        )

        # 7. Clean up removed files.
        deleted_paths: list[str] = []
        if all_known_files is not None:
            known_paths = {str(p.resolve()) for p in all_known_files}
            removed_strs = await self._get_stored_files_not_in(graph_id, known_paths)
        else:
            current_paths = {str(p.resolve()) for p in all_files}
            removed_strs = await self._get_stored_files_not_in(graph_id, current_paths)

        for removed_path in removed_strs:
            try:
                await self.graph_store.delete_file_facts(graph_id, removed_path)
                deleted_paths.append(removed_path)
            except Exception as exc:
                logger.warning("Failed to clean up removed file %s: %s", removed_path, exc)

        unchanged = [p for p in all_files if str(p.resolve()) not in seen]
        return ChangeReport(
            added=added_paths,
            modified=modified_paths,
            deleted=deleted_paths,
            unchanged=[str(p.resolve()) for p in unchanged],
            message=(
                f"Targeted sync for {adapter.language}: "
                f"+{len(added_paths)} ~{len(modified_paths)} -{len(deleted_paths)}"
            ),
        )
