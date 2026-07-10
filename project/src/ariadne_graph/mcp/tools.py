"""ToolRegistry — all 14 MCP tool handlers."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import xxhash

from ariadne_graph.core.architecture import persist_architecture_diagnostics
from ariadne_graph.core.auto_sync import AutoSyncManager

if TYPE_CHECKING:
    from ariadne_graph.core.watch_sync import WatchSyncManager
from ariadne_graph.core.capabilities import get_capabilities
from ariadne_graph.core.communities import CommunityAnalyzer
from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.embeddings import EmbeddingProvider, EmbeddingService
from ariadne_graph.core.freshness import FreshnessTracker
from ariadne_graph.core.incremental_sync import IncrementalSync
from ariadne_graph.core.models import CodeNode, ProjectRecord
from ariadne_graph.core.retrieval import GraphRetriever
from ariadne_graph.core.search import HybridSearcher
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.base import LanguageAdapter
from ariadne_graph.mcp.fallbacks import GraphStoreFallbacks
from ariadne_graph.mcp.schemas import (
    ArchitectureOutput,
    CapabilitiesInput,
    CapabilitiesOutput,
    CommunitiesOutput,
    DeleteProjectInput,
    DeleteProjectOutput,
    DetectChangesInput,
    DetectChangesOutput,
    FindHotspotsInput,
    FindHotspotsOutput,
    GetArchitectureInput,
    ImpactAnalysisInput,
    ImpactAnalysisOutput,
    IndexInput,
    IndexOutput,
    IndexStatusInput,
    IndexStatusOutput,
    InspectFileInput,
    InspectFileOutput,
    ListCommunitiesInput,
    ListDiagnosticsInput,
    ListDiagnosticsOutput,
    LumenRetrieveInput,
    LumenRetrieveOutput,
    ProjectListOutput,
    RetrieveInput,
    RetrieveOutput,
    SearchCodeInput,
    SearchCodeOutput,
    SearchSemanticInput,
    SearchSemanticOutput,
    TraceDependenciesInput,
    TraceDependenciesOutput,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _graph_id_from_repo_path(repo_path: str) -> str:
    """Derive a stable graph_id from a repository path."""
    resolved = str(Path(repo_path).resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def _read_file_bytes(path: Path) -> bytes:
    """Read file contents as bytes."""
    return path.read_bytes()


def _file_content_hash(content: bytes) -> str:
    """Compute XXH3 hash of file content."""
    return xxhash.xxh3_64_hexdigest(content)


def _find_adapter_for_file(
    file_path: Path, adapters: dict[str, LanguageAdapter]
) -> LanguageAdapter | None:
    """Find a language adapter that handles the given file extension."""
    for adapter in adapters.values():
        if any(str(file_path).endswith(ext) for ext in adapter.extensions):
            return adapter
    return None


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Holds all 14 MCP tool handlers and their shared resources."""

    def __init__(
        self,
        graph_store: GraphStore,
        searchable_store: SearchableGraphStore | None,
        adapters: dict[str, LanguageAdapter],
        config: AnalyzerConfig,
        snippet_extractor: SnippetExtractor | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.graph_store = graph_store
        self.searchable_store = searchable_store
        self.adapters = adapters
        self.config = config
        self.snippet_extractor = snippet_extractor
        self.embedding_provider = embedding_provider
        self.freshness_tracker = FreshnessTracker(graph_store)
        self.auto_sync_manager: AutoSyncManager | WatchSyncManager | None = None
        # Track graph metadata (repo_path -> {graph_id, indexed_files, last_indexed})
        self._graph_meta: dict[str, dict[str, Any]] = {}

        # Wire up core services when a searchable store is available
        self.retriever: GraphRetriever | None = None
        self.searcher: HybridSearcher | None = None
        self.community_analyzer: CommunityAnalyzer | None = None
        self.embedding_service: EmbeddingService | None = None

        if self.searchable_store is not None:
            self.retriever = GraphRetriever(
                self.searchable_store,
                self.snippet_extractor or SnippetExtractor(repo_root=config.resolved_repo_root),
            )
            self.searcher = HybridSearcher(
                self.searchable_store,
                self.embedding_provider,
                self.snippet_extractor or SnippetExtractor(repo_root=config.resolved_repo_root),
            )
            self.community_analyzer = CommunityAnalyzer(self.searchable_store)
            if self.embedding_provider is not None:
                self.embedding_service = EmbeddingService(
                    self.embedding_provider, self.searchable_store
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_graph_id(self, repo_path: str) -> str:
        """Derive graph_id from repo_path."""
        return _graph_id_from_repo_path(repo_path)

    async def _graph_exists(self, graph_id: str) -> bool:
        """Check whether any data exists for the given graph."""
        try:
            result = await self.graph_store.query(graph_id, "nodes")
            return len(result) > 0
        except Exception:
            return False

    async def _known_graph_ids(self) -> list[str]:
        """Return known graph IDs from in-memory cache or the backend catalog."""
        if self._graph_meta:
            return list(self._graph_meta.keys())
        try:
            projects = await self.graph_store.list_projects()
            return [p["graph_id"] for p in projects if p.get("graph_id")]
        except Exception:
            return []

    async def _count_indexed_files(self, graph_id: str) -> int:
        """Count unique files indexed for a graph."""
        try:
            nodes = await self.graph_store.query(graph_id, "nodes")
            files: set[str] = set()
            for row in nodes:
                node_data = row.get("n", row)
                props = node_data.get("properties", {})
                fp = props.get("file_path")
                if fp:
                    files.add(fp)
            return len(files)
        except Exception:
            return 0

    async def _get_nodes_for_file(self, graph_id: str, file_path: str) -> list[dict[str, Any]]:
        """Get all nodes that belong to a specific file."""
        try:
            nodes = await self.graph_store.query(graph_id, "nodes")
            result = []
            for row in nodes:
                node_data = row.get("n", row)
                props = node_data.get("properties", {})
                if props.get("file_path") == file_path:
                    result.append(node_data)
            return result
        except Exception:
            return []

    async def _get_edges_for_file(self, graph_id: str, file_path: str) -> list[dict[str, Any]]:
        """Get all edges originating from a specific file."""
        try:
            edges = await self.graph_store.query(graph_id, "edges")
            result = []
            for row in edges:
                edge_data = row.get("r", row)
                props = edge_data.get("properties", {})
                if props.get("owner_file_path") == file_path:
                    result.append(edge_data)
            return result
        except Exception:
            return []

    def register_graph_meta(self, repo_path: str, graph_id: str) -> None:
        """Register graph metadata for commands that need to resolve repo_path.

        Deprecated: project metadata is now persisted via
        :meth:`_record_index_meta`. This helper remains as a cache-only
        convenience for callers that explicitly populate it.
        """
        self._graph_meta[graph_id] = {
            "repo_path": str(Path(repo_path).resolve()),
            "graph_id": graph_id,
            "file_count": 0,
            "last_indexed": None,
        }

    async def _record_index_meta(
        self,
        repo_path: str,
        graph_id: str,
        file_count: int,
        sync_enabled: bool = False,
    ) -> None:
        """Record metadata about an indexing run in the graph store catalog."""
        resolved = str(Path(repo_path).resolve())
        self._graph_meta[graph_id] = {
            "repo_path": resolved,
            "graph_id": graph_id,
            "file_count": file_count,
            "last_indexed": datetime.now(UTC).isoformat(),
            "sync_enabled": sync_enabled,
        }
        try:
            await self.graph_store.register_project(
                graph_id, resolved, file_count, sync_enabled=sync_enabled
            )
        except Exception as exc:
            logger.warning("Failed to persist project metadata: %s", exc)

    async def start_auto_sync(self) -> None:
        """Start the background auto-sync task.

        Uses a filesystem watcher when ``watch_mode`` is ``auto`` and
        ``watchdog`` is installed; otherwise falls back to interval polling.
        """
        if self.config.watch_mode == "off":
            logger.info("Auto-sync is disabled (watch_mode=off)")
            return

        if self.config.watch_mode == "auto":
            from ariadne_graph.core.watch_sync import WatchSyncManager

            if WatchSyncManager.is_available():
                self.auto_sync_manager = WatchSyncManager(self)
            else:
                logger.warning(
                    "watch_mode=auto but watchdog is not installed; falling back to polling"
                )
                from ariadne_graph.core.auto_sync import AutoSyncManager

                self.auto_sync_manager = AutoSyncManager(
                    self, self.config.incremental_sync_interval
                )
        else:
            from ariadne_graph.core.auto_sync import AutoSyncManager

            self.auto_sync_manager = AutoSyncManager(
                self, self.config.incremental_sync_interval
            )
        await self.auto_sync_manager.start()

    async def stop_auto_sync(self) -> None:
        """Stop the background auto-sync task (watcher or polling)."""
        if self.auto_sync_manager is not None:
            await self.auto_sync_manager.stop()

    async def close(self) -> None:
        """Close the underlying graph store and release resources."""
        await self.stop_auto_sync()
        await self.graph_store.close()

    # ==================================================================
    # INDEXING TOOLS (4)
    # ==================================================================

    async def handle_index(self, input: IndexInput) -> IndexOutput:
        """Index a repository: discover files, parse changed ones, store facts."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        # Force rebuild: delete existing graph
        if input.force_rebuild:
            with contextlib.suppress(Exception):
                await self.graph_store.delete_graph(graph_id)

        # Set up config for this repo
        config = self.config.model_copy(update={"repo_root": Path(repo_path), "graph_id": graph_id})

        total_indexed = 0
        errors: list[str] = []
        changed_files: set[str] = set()

        # Discover all current files across adapters once, so that cleanup of
        # removed files respects the union and one adapter does not wipe another.
        all_current_files: list[Path] = []
        for adapter in self.adapters.values():
            try:
                all_current_files.extend(adapter.discover_files(Path(repo_path), config))
            except Exception as exc:
                logger.warning("File discovery failed: %s", exc)

        logger.info(
            "Discovered %d unique files across all adapters",
            len({str(p.resolve()) for p in all_current_files}),
        )

        # Process each language adapter using IncrementalSync
        changed_count = 0
        for lang_name, adapter in self.adapters.items():
            try:
                sync = IncrementalSync(self.graph_store, config)
                report = await sync.full_sync(adapter, config, all_known_files=all_current_files)
                changed_count += len(report.added) + len(report.modified)
                changed_files.update(report.added)
                changed_files.update(report.modified)
            except Exception as exc:
                errors.append(f"{lang_name} sync failed: {exc}")

        # Whole-graph architecture analysis — runs once, after every adapter has
        # synced and SCIP has resolved dep edges. Persists cycle/deep-import/
        # orphan/upward-import findings as CodeDiagnostic nodes. Requires the
        # SQLite store (the dep-edge join is SQL); other stores skip it.
        if isinstance(self.graph_store, SQLiteGraphStore):
            try:
                written = await persist_architecture_diagnostics(
                    self.graph_store, graph_id, repo_path
                )
                logger.info("Architecture analysis wrote %d findings", written)
            except Exception as exc:
                logger.warning("Architecture analysis failed: %s", exc)

        # Also compute embeddings for changed files if a provider is available
        if self.embedding_service is not None and self.searchable_store is not None and changed_files:
            try:
                nodes_to_embed: list[CodeNode] = []
                for file_path in changed_files:
                    try:
                        rows = await self.searchable_store.query(
                            graph_id, "nodes_by_file", {"file_path": file_path}
                        )
                    except Exception:
                        rows = []
                    for row in rows:
                        node_data = dict(row.get("n", row))
                        if node_data:
                            node_data.setdefault("graph_id", graph_id)
                            nodes_to_embed.append(CodeNode(**node_data))

                if nodes_to_embed:
                    await self.embedding_service.embed_nodes(graph_id, nodes_to_embed)
            except Exception as exc:
                logger.warning("Failed to compute embeddings during index: %s", exc)

        # Authoritative file count from the graph store after syncing.
        try:
            count_rows = await self.graph_store.query(graph_id, "count_files")
            total_indexed = count_rows[0].get("count", 0) if count_rows else changed_count
        except Exception as exc:
            logger.warning("Failed to count indexed files: %s", exc)
            total_indexed = changed_count

        sync_enabled = self.config.auto_sync
        await self._record_index_meta(
            repo_path, graph_id, total_indexed, sync_enabled=sync_enabled
        )
        try:
            await self.freshness_tracker.mark_indexed(
                graph_id, repo_path, file_count=total_indexed, sync_enabled=sync_enabled
            )
        except Exception as exc:
            logger.warning("Failed to record freshness metadata: %s", exc)

        status = "success" if not errors else ("partial" if total_indexed > 0 else "error")
        message = f"Indexed {total_indexed} files"
        if changed_count != total_indexed:
            message += f" ({changed_count} changed this run)"
        if errors:
            message += f" with {len(errors)} errors"

        return IndexOutput(
            status=status,
            files_indexed=total_indexed,
            graph_id=graph_id,
            message=message,
        )

    async def handle_targeted_sync(
        self, repo_path: str, changed_files: list[Path]
    ) -> IndexOutput:
        """Sync a known set of changed files without rediscovering the repo.

        This is the entry point used by the filesystem watcher. It groups
        changed files by language adapter, runs a targeted incremental sync
        for each adapter, and re-computes embeddings for affected nodes.
        """
        resolved = str(Path(repo_path).resolve())
        graph_id = self._get_graph_id(resolved)
        config = self.config.model_copy(
            update={"repo_root": Path(resolved), "graph_id": graph_id}
        )

        if not changed_files:
            return IndexOutput(
                status="success",
                files_indexed=0,
                graph_id=graph_id,
                message="No changed files to sync",
            )

        # Discover all current files once for removed-file cleanup.
        all_current_files: list[Path] = []
        for adapter in self.adapters.values():
            try:
                all_current_files.extend(adapter.discover_files(Path(resolved), config))
            except Exception as exc:
                logger.warning("File discovery failed during targeted sync: %s", exc)

        changed_paths = {str(p.resolve()) for p in changed_files}
        errors: list[str] = []
        changed_count = 0

        for lang_name, adapter in self.adapters.items():
            owned = [p for p in changed_files if str(p.resolve()) in changed_paths]
            if not owned:
                continue
            try:
                sync = IncrementalSync(self.graph_store, config)
                report = await sync.targeted_sync(
                    adapter, owned, all_known_files=all_current_files
                )
                changed_count += len(report.added) + len(report.modified)
            except Exception as exc:
                errors.append(f"{lang_name} targeted sync failed: {exc}")

        # Re-compute embeddings for changed nodes.
        if (
            self.embedding_service is not None
            and self.searchable_store is not None
            and changed_paths
        ):
            try:
                nodes_to_embed: list[CodeNode] = []
                for file_path in changed_paths:
                    try:
                        rows = await self.searchable_store.query(
                            graph_id, "nodes_by_file", {"file_path": file_path}
                        )
                    except Exception:
                        rows = []
                    for row in rows:
                        node_data = dict(row.get("n", row))
                        if node_data:
                            node_data.setdefault("graph_id", graph_id)
                            nodes_to_embed.append(CodeNode(**node_data))
                if nodes_to_embed:
                    await self.embedding_service.embed_nodes(graph_id, nodes_to_embed)
            except Exception as exc:
                logger.warning("Failed to compute embeddings during targeted sync: %s", exc)

        # Authoritative file count from the graph store.
        try:
            count_rows = await self.graph_store.query(graph_id, "count_files")
            total_indexed = count_rows[0].get("count", 0) if count_rows else changed_count
        except Exception as exc:
            logger.warning("Failed to count indexed files: %s", exc)
            total_indexed = changed_count

        # Mark sync as enabled because this path is only used by auto-sync.
        await self._record_index_meta(
            resolved, graph_id, total_indexed, sync_enabled=True
        )
        try:
            await self.freshness_tracker.mark_indexed(
                graph_id, resolved, file_count=total_indexed, sync_enabled=True
            )
        except Exception as exc:
            logger.warning("Failed to record freshness metadata: %s", exc)

        status = "success" if not errors else ("partial" if changed_count > 0 else "error")
        message = f"Targeted sync: {changed_count} files updated"
        if errors:
            message += f" with {len(errors)} errors"
        return IndexOutput(
            status=status,
            files_indexed=total_indexed,
            graph_id=graph_id,
            message=message,
        )

    async def handle_index_status(self, input: IndexStatusInput) -> IndexStatusOutput:
        """Check index status for a repository."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return IndexStatusOutput(
                graph_id=graph_id,
                repo_path=repo_path,
                last_indexed=None,
                file_count=0,
                dirty_files=[],
                sync_enabled=False,
                capabilities=get_capabilities(),
                message="Repository has not been indexed yet",
            )

        sync_enabled = False
        try:
            status = await self.freshness_tracker.get_status(graph_id, repo_path=repo_path)
            last_indexed = status.last_indexed
            file_count = status.file_count
            dirty_files = status.dirty_files
            sync_enabled = status.sync_enabled
        except Exception as exc:
            logger.warning("FreshnessTracker failed, falling back to scan: %s", exc)
            # Fallback to the legacy scan-based logic
            file_count = await self._count_indexed_files(graph_id)
            meta = self._graph_meta.get(graph_id, {})
            last_indexed = meta.get("last_indexed")
            sync_enabled = bool(meta.get("sync_enabled", False))
            if not last_indexed:
                try:
                    projects = await self.graph_store.list_projects()
                    for project in projects:
                        if project.get("graph_id") == graph_id:
                            last_indexed = project.get("last_indexed")
                            sync_enabled = bool(project.get("sync_enabled", sync_enabled))
                            if project.get("file_count"):
                                file_count = project["file_count"]
                            break
                except Exception:
                    pass

            dirty_files = []
            for _lang_name, adapter in self.adapters.items():
                try:
                    config = self.config.model_copy(
                        update={"repo_root": Path(repo_path), "graph_id": graph_id}
                    )
                    files = adapter.discover_files(Path(repo_path), config)
                    for file_path in files:
                        try:
                            content = _read_file_bytes(file_path)
                            current_hash = _file_content_hash(content)
                            stored_hash = await self.graph_store.get_stored_hash(
                                graph_id, str(file_path)
                            )
                            if stored_hash != current_hash:
                                dirty_files.append(str(file_path))
                        except Exception:
                            dirty_files.append(str(file_path))
                except Exception:
                    pass

        # If auto-sync is actively running for this registry, report it as enabled.
        if self.auto_sync_manager is not None and self.auto_sync_manager.is_running():
            sync_enabled = True

        return IndexStatusOutput(
            graph_id=graph_id,
            repo_path=repo_path,
            last_indexed=last_indexed,
            file_count=file_count,
            dirty_files=dirty_files,
            sync_enabled=sync_enabled,
            capabilities=get_capabilities(),
            message=f"Indexed {file_count} files; {len(dirty_files)} files changed since last index",
        )

    async def handle_capabilities(self, input: CapabilitiesInput) -> CapabilitiesOutput:
        """Return the runtime capability report for optional features."""
        report = get_capabilities()
        return CapabilitiesOutput(
            capabilities=report,
            message=report.get("message", ""),
        )

    async def handle_list_projects(self) -> ProjectListOutput:
        """List all indexed projects from the graph store catalog."""
        try:
            projects = await self.graph_store.list_projects()
        except Exception as exc:
            logger.warning("Failed to list projects from graph store: %s", exc)
            projects = []

        if not projects:
            projects = [
                {
                    "graph_id": meta["graph_id"],
                    "repo_path": meta["repo_path"],
                    "created_at": None,
                    "last_indexed": meta.get("last_indexed"),
                    "file_count": meta.get("file_count", 0),
                }
                for meta in self._graph_meta.values()
            ]

        records = [ProjectRecord(**project) for project in projects]
        return ProjectListOutput(projects=records)

    async def handle_delete_project(self, input: DeleteProjectInput) -> DeleteProjectOutput:
        """Delete a project/graph."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        try:
            await self.graph_store.delete_graph(graph_id)
            # Also remove from metadata
            self._graph_meta.pop(graph_id, None)
            return DeleteProjectOutput(
                deleted=True,
                graph_id=graph_id,
                message=f"Project {graph_id} deleted successfully",
            )
        except Exception as exc:
            return DeleteProjectOutput(
                deleted=False,
                graph_id=graph_id,
                message=f"Failed to delete project: {exc}",
            )

    # ==================================================================
    # QUERY TOOLS (4)
    # ==================================================================

    async def handle_retrieve(self, input: RetrieveInput) -> RetrieveOutput:
        """Retrieve a symbol and its neighborhood from the graph."""
        graph_id = input.graph_id
        if graph_id is None:
            if input.repo_path:
                graph_id = _graph_id_from_repo_path(input.repo_path)
            else:
                # For backwards compatibility, treat the query itself as a
                # repo path only when no other identifier is supplied.
                graph_id = _graph_id_from_repo_path(input.query)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return RetrieveOutput(results=[])

        query = input.query

        # Use GraphRetriever when available
        if self.retriever is not None:
            try:
                result = await self.retriever.retrieve_node(graph_id, query)
                results: list[dict[str, Any]] = [{"type": "retrieve", "data": result}]
                return RetrieveOutput(results=results)
            except Exception as exc:
                logger.warning("GraphRetriever failed, falling back to scan: %s", exc)

        return await GraphStoreFallbacks.retrieve(self.graph_store, graph_id, query)

    async def handle_lumen_code_graph_retrieve(
        self, input: LumenRetrieveInput
    ) -> LumenRetrieveOutput:
        """Lumen-compatible alias for code_graph_retrieve.

        Derives the graph_id from *repo_path* when provided, then delegates to
        the canonical retrieve handler.  The response is augmented with a small
        Lumen-style context block.
        """
        repo_path = input.repo_path
        if repo_path is None and self.config.lumen_workspace_root:
            repo_path = str(self.config.lumen_workspace_root)

        graph_id = input.graph_id
        if graph_id is None and repo_path:
            graph_id = _graph_id_from_repo_path(repo_path)

        retrieve_input = RetrieveInput(query=input.query, graph_id=graph_id, repo_path=repo_path)
        canonical = await self.handle_retrieve(retrieve_input)

        lumen_context: dict[str, Any] = {
            "tool_alias": "lumen_code_graph_retrieve",
            "canonical_tool": "code_graph_retrieve",
            "workspace_restricted": bool(self.config.lumen_workspace_root),
        }
        if self.config.lumen_workspace_id:
            lumen_context["workspace_id"] = self.config.lumen_workspace_id

        return LumenRetrieveOutput(
            results=canonical.results,
            lumen_context=lumen_context,
        )

    async def handle_search_semantic(self, input: SearchSemanticInput) -> SearchSemanticOutput:
        """Search the graph using semantic (vector) similarity."""
        if self.searchable_store is None:
            return SearchSemanticOutput(
                hits=[],
                message="Semantic search is not available. No SearchableGraphStore configured.",
            )

        if self.searcher is None or self.embedding_provider is None:
            return SearchSemanticOutput(
                hits=[],
                message=(
                    "Semantic search requires the semantic extra. "
                    "Install with: pip install -e \".[semantic]\""
                ),
            )

        # Search across the requested repo, or all known graphs if none specified.
        if input.repo_path:
            graph_ids = [self._get_graph_id(input.repo_path)]
        else:
            graph_ids = await self._known_graph_ids()
        if not graph_ids:
            return SearchSemanticOutput(
                hits=[],
                message="No indexed graphs found. Run code_graph_index first.",
            )

        all_hits: list[dict[str, Any]] = []
        for graph_id in graph_ids:
            try:
                hits = await self.searcher.search(
                    graph_id,
                    input.query_text,
                    limit=input.limit,
                    search_type="semantic",
                )
                for hit in hits:
                    hit["graph_id"] = graph_id
                all_hits.extend(hits)
            except Exception as exc:
                logger.warning("Semantic search failed for graph %s: %s", graph_id, exc)

        # Apply type filter post-search if requested
        if input.types:
            type_set = {t.lower() for t in input.types}

            def _hit_labels(hit: dict[str, Any]) -> list[str]:
                node = hit.get("node")
                if not isinstance(node, dict):
                    return []
                return node.get("labels", []) or []

            all_hits = [
                hit for hit in all_hits
                if any(label.lower() in type_set for label in _hit_labels(hit))
            ]

        all_hits.sort(key=lambda h: h.get("score", 0.0), reverse=True)
        all_hits = all_hits[: input.limit]

        return SearchSemanticOutput(
            hits=all_hits,
            message=f"Found {len(all_hits)} semantic matches",
        )

    async def handle_search_code(self, input: SearchCodeInput) -> SearchCodeOutput:
        """Search code by pattern/keyword through the graph."""
        if input.repo_path:
            graph_ids = [self._get_graph_id(input.repo_path)]
        else:
            graph_ids = await self._known_graph_ids()
        if not graph_ids:
            return SearchCodeOutput(matches=[], message="No indexed graphs found")

        matches: list[dict[str, Any]] = []

        def _language_from_props(props: dict[str, Any]) -> str:
            """Return a node's language, inferring from file_path when absent."""
            lang = str(props.get("language", ""))
            if lang:
                return lang
            file_path = str(props.get("file_path", ""))
            if file_path.endswith((".ts", ".tsx")):
                return "typescript"
            if file_path.endswith((".js", ".jsx")):
                return "javascript"
            if file_path.endswith(".py"):
                return "python"
            return ""

        async def _fetch_hit_node(graph_id: str, node_id: str) -> dict[str, Any] | None:
            """Hydrate a keyword-search hit with its node record if available."""
            try:
                rows = await self.graph_store.query(
                    graph_id,
                    "node_by_id",
                    params={"node_id": node_id},
                )
                if rows:
                    return cast(dict[str, Any], rows[0].get("n", rows[0]))
            except Exception as exc:
                logger.warning(
                    "Failed to fetch node %s for language filter: %s", node_id, exc
                )
            return None

        for graph_id in graph_ids:
            # Use HybridSearcher keyword search when available
            if self.searcher is not None:
                try:
                    hits = await self.searcher.search(
                        graph_id,
                        input.pattern,
                        limit=input.limit,
                        search_type="keyword",
                    )
                    for hit in hits:
                        node = hit.get("node")
                        if not node:
                            node = await _fetch_hit_node(
                                graph_id, hit.get("node_id", "")
                            )
                        node = node or {}
                        props = node.get("properties", {}) if isinstance(node, dict) else {}
                        if input.language:
                            node_lang = _language_from_props(props)
                            if input.language.lower() not in node_lang.lower():
                                continue
                        matches.append({
                            "graph_id": graph_id,
                            "node_id": hit.get("node_id"),
                            "labels": node.get("labels", []),
                            "properties": props,
                            "score": hit.get("score", 0.0),
                        })
                        if len(matches) >= input.limit:
                            break
                    continue
                except Exception as exc:
                    logger.warning("Keyword search failed for graph %s: %s", graph_id, exc)

            # Fallback: substring scan
            pattern = input.pattern.lower()
            try:
                nodes = await self.graph_store.query(graph_id, "nodes")
                for row in nodes:
                    node_data = row.get("n", row)
                    props = node_data.get("properties", {})

                    if input.language:
                        node_lang = _language_from_props(props)
                        if input.language.lower() not in node_lang.lower():
                            continue

                    searchable = " ".join([
                        str(props.get("name", "")),
                        str(props.get("qualname", "")),
                        str(props.get("file_path", "")),
                    ]).lower()

                    if pattern in searchable:
                        matches.append({
                            "graph_id": graph_id,
                            "node_id": node_data.get("id"),
                            "labels": node_data.get("labels", []),
                            "properties": props,
                        })
                        if len(matches) >= input.limit:
                            return SearchCodeOutput(
                                matches=matches,
                                message=f"Found {len(matches)} matches",
                            )
            except Exception:
                continue

        matches.sort(key=lambda m: m.get("score", 0.0), reverse=True)
        matches = matches[: input.limit]

        return SearchCodeOutput(
            matches=matches,
            message=f"Found {len(matches)} matches" if matches else "No matches found",
        )

    async def handle_trace_dependencies(
        self, input: TraceDependenciesInput
    ) -> TraceDependenciesOutput:
        """Trace dependencies from a symbol using BFS through the graph."""
        symbol = input.symbol
        direction = input.direction
        max_depth = input.max_depth

        # Need graph_id — use provided or search across all known graphs
        if input.graph_id:
            graph_ids = [input.graph_id]
        else:
            graph_ids = await self._known_graph_ids()
        all_paths: list[list[str]] = []

        for graph_id in graph_ids:
            if self.retriever is not None:
                try:
                    results = await self.retriever.trace_dependencies(
                        graph_id,
                        symbol,
                        direction=direction,
                        max_depth=max_depth,
                    )
                    for result in results:
                        path = result.get("path", [])
                        if len(path) > 1:
                            all_paths.append(path)
                    continue
                except Exception as exc:
                    logger.warning("GraphRetriever trace failed: %s", exc)

            fallback = await GraphStoreFallbacks.trace_dependencies(
                self.graph_store,
                graph_id,
                symbol,
                direction,
                max_depth,
            )
            for path in fallback.paths:
                if len(path) > 1:
                    all_paths.append(path)

        return TraceDependenciesOutput(paths=all_paths)

    # ==================================================================
    # ANALYSIS TOOLS (5)
    # ==================================================================

    async def handle_impact_analysis(self, input: ImpactAnalysisInput) -> ImpactAnalysisOutput:
        """Compute transitive closure of dependencies from a symbol."""
        symbol = input.symbol

        if input.graph_id:
            graph_ids = [input.graph_id]
        else:
            graph_ids = await self._known_graph_ids()
        if not graph_ids:
            return ImpactAnalysisOutput(
                target_symbol=symbol,
                total_affected=0,
                message="No graph found for impact analysis",
            )

        for graph_id in graph_ids:
            if self.retriever is not None:
                try:
                    result = await self.retriever.impact_analysis(graph_id, symbol)
                    return ImpactAnalysisOutput(
                        target_symbol=result.target_symbol,
                        total_affected=result.total_affected,
                        direct_dependencies=result.direct_dependencies,
                        transitive_affected=result.transitive_affected,
                        coupling_scores=result.coupling_scores,
                    )
                except Exception as exc:
                    logger.warning("GraphRetriever impact analysis failed: %s", exc)

            return await GraphStoreFallbacks.impact_analysis(
                self.graph_store, graph_id, symbol
            )

        return ImpactAnalysisOutput(
            target_symbol=symbol,
            total_affected=0,
            message="No graph found for impact analysis",
        )

    async def handle_detect_changes(self, input: DetectChangesInput) -> DetectChangesOutput:
        """Detect changed files by comparing current hashes with stored hashes."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return DetectChangesOutput(
                added=[], modified=[], deleted=[],
                message="Repository has not been indexed yet",
            )

        config = self.config.model_copy(
            update={"repo_root": Path(repo_path), "graph_id": graph_id}
        )
        sync = IncrementalSync(self.graph_store, config)

        if input.since_ref:
            all_files: list[Path] = []
            for adapter in self.adapters.values():
                try:
                    all_files.extend(adapter.discover_files(Path(repo_path), config))
                except Exception as exc:
                    logger.warning(
                        "File discovery failed for %s adapter: %s", adapter.language, exc
                    )

            report = await sync.get_changed_files_since_ref(
                graph_id, all_files, input.since_ref
            )
            return DetectChangesOutput(
                added=report.added,
                modified=report.modified,
                deleted=report.deleted,
                since_ref=input.since_ref,
                resolved_ref=report.resolved_ref,
                comparison_mode=report.comparison_mode,
                message=report.message,
            )

        added: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []

        for lang_name, adapter in self.adapters.items():
            try:
                files = adapter.discover_files(Path(repo_path), config)
                changed, _unchanged, removed = await sync.get_changed_files(graph_id, files)
                for file_path in changed:
                    path_str = str(file_path.resolve())
                    stored_hash = await self.graph_store.get_stored_hash(graph_id, path_str)
                    if stored_hash is None:
                        added.append(path_str)
                    else:
                        modified.append(path_str)
                deleted.extend(str(p) for p in removed)
            except Exception as exc:
                logger.warning("Change detection failed for %s adapter: %s", lang_name, exc)

        return DetectChangesOutput(
            added=added,
            modified=modified,
            deleted=deleted,
            message=f"Changes: +{len(added)} ~{len(modified)} -{len(deleted)}",
        )

    async def handle_find_hotspots(self, input: FindHotspotsInput) -> FindHotspotsOutput:
        """Find code hotspots by computing fan-in/fan-out or coupling metrics."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return FindHotspotsOutput(
                hotspots=[],
                message="Repository has not been indexed yet",
            )

        if self.community_analyzer is not None:
            try:
                analyzer_hotspots = await self.community_analyzer.find_hotspots(
                    graph_id,
                    top_n=input.top_n,
                    metric=input.metric,
                )
                return FindHotspotsOutput(
                    hotspots=[h.model_dump() for h in analyzer_hotspots],
                    message=f"Top {len(analyzer_hotspots)} hotspots by {input.metric}",
                )
            except Exception as exc:
                logger.warning("CommunityAnalyzer.find_hotspots failed: %s", exc)

        return await GraphStoreFallbacks.find_hotspots(
            self.graph_store, graph_id, input.top_n, input.metric
        )

    async def handle_get_architecture(self, input: GetArchitectureInput) -> ArchitectureOutput:
        """Get architecture summary from community detection."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return ArchitectureOutput(
                summary={},
                message="Repository has not been indexed yet",
            )

        if self.community_analyzer is not None:
            try:
                summary_obj = await self.community_analyzer.get_architecture_summary(graph_id)
                return ArchitectureOutput(
                    summary=summary_obj.model_dump(),
                    message=(
                        f"Architecture: {summary_obj.total_communities} communities, "
                        f"{summary_obj.total_files} files, "
                        f"{summary_obj.total_entities} entities"
                    ),
                )
            except Exception as exc:
                logger.warning("CommunityAnalyzer architecture summary failed: %s", exc)

        return await GraphStoreFallbacks.get_architecture(self.graph_store, graph_id)

    async def handle_list_communities(self, input: ListCommunitiesInput) -> CommunitiesOutput:
        """List communities in the code graph."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return CommunitiesOutput(
                communities=[],
                message="Repository has not been indexed yet",
            )

        communities_data: dict[int, list[str]] = {}

        # Try SearchableGraphStore first
        if self.searchable_store is not None:
            with contextlib.suppress(Exception):
                communities_data = await self.searchable_store.get_communities(graph_id)

        # On-demand detection if none stored and analyzer is available
        if not communities_data and self.community_analyzer is not None:
            try:
                assignments = await self.community_analyzer.detect_communities(graph_id)
                communities_data = {}
                for node_id, comm_id in assignments.items():
                    communities_data.setdefault(comm_id, []).append(node_id)
            except Exception as exc:
                logger.warning("Community detection failed: %s", exc)

        # Fallback: no communities available
        if not communities_data:
            return CommunitiesOutput(
                communities=[],
                message="No communities detected. Run indexing with community detection enabled.",
            )

        # Build output
        community_id_filter = input.community_id
        communities: list[dict[str, Any]] = []

        for comm_id, members in communities_data.items():
            if community_id_filter is not None and comm_id != community_id_filter:
                continue
            communities.append({
                "community_id": comm_id,
                "member_count": len(members),
                "members": members[:50],  # Limit for output size
            })

        return CommunitiesOutput(
            communities=communities,
            message=f"Found {len(communities)} communities",
        )

    # ==================================================================
    # CODE TOOLS (1)
    # ==================================================================

    async def handle_inspect_file(self, input: InspectFileInput) -> InspectFileOutput:
        """Inspect a file: return all nodes and edges for it."""
        file_path = input.file_path
        graph_id = input.graph_id

        if graph_id is None:
            # Try each known graph to find the file
            known_graph_ids = await self._known_graph_ids()
            for gid in known_graph_ids:
                nodes = await self._get_nodes_for_file(gid, file_path)
                if nodes:
                    graph_id = gid
                    break

        if graph_id is None:
            return InspectFileOutput(
                nodes=[], edges=[],
                message=f"File {file_path} not found in any indexed graph",
            )

        nodes = await self._get_nodes_for_file(graph_id, file_path)
        edges = await self._get_edges_for_file(graph_id, file_path)

        return InspectFileOutput(
            nodes=nodes,
            edges=edges,
            message=f"Found {len(nodes)} nodes and {len(edges)} edges in {file_path}",
        )

    async def handle_list_diagnostics(
        self, input: ListDiagnosticsInput
    ) -> ListDiagnosticsOutput:
        """List diagnostics for a repository, with optional filters."""
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return ListDiagnosticsOutput(
                diagnostics=[],
                message="Repository has not been indexed yet",
            )

        diagnostic_nodes: list[dict[str, Any]] = []

        # Try labelled query first
        try:
            rows = await self.graph_store.query(
                graph_id, "nodes_by_label", params={"label": "CodeDiagnostic"}
            )
            for row in rows:
                node_data = row.get("n", row)
                diagnostic_nodes.append(node_data)
        except Exception as exc:
            logger.warning("nodes_by_label query failed, falling back to scan: %s", exc)

        # Fallback: scan all nodes for CodeDiagnostic labels
        if not diagnostic_nodes:
            try:
                rows = await self.graph_store.query(graph_id, "nodes")
                for row in rows:
                    node_data = row.get("n", row)
                    labels = node_data.get("labels", [])
                    if "CodeDiagnostic" in labels:
                        diagnostic_nodes.append(node_data)
            except Exception as exc:
                logger.warning("Failed to list diagnostics: %s", exc)

        results: list[dict[str, Any]] = []
        for node_data in diagnostic_nodes:
            props = node_data.get("properties", {})

            if input.level and props.get("level") != input.level:
                continue
            if input.rule and props.get("rule") != input.rule:
                continue
            if input.file_path and props.get("file_path") != input.file_path:
                continue

            results.append({
                "node_id": node_data.get("id"),
                "labels": node_data.get("labels", []),
                "level": props.get("level"),
                "rule": props.get("rule"),
                "message": props.get("message"),
                "file_path": props.get("file_path"),
                "timestamp": props.get("timestamp"),
                "properties": props,
            })

            if len(results) >= input.limit:
                break

        return ListDiagnosticsOutput(
            diagnostics=results,
            message=f"Found {len(results)} diagnostics",
        )
