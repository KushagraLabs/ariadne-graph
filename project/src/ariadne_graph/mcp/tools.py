"""ToolRegistry — all 23 MCP tool handlers."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import xxhash

from ariadne_graph.core.architecture import (
    _dir_of,
    _organ,
    _read_code_files,
    _rel,
    _sql_connect,
    explain_edge,
    is_peripheral_path,
    persist_architecture_diagnostics,
    read_dependency_matrix,
    read_resolution_coverage,
)
from ariadne_graph.core.architecture_config import (
    ArchitectureConfig,
    ModuleSpec,
    load_architecture_config,
)
from ariadne_graph.core.auto_sync import AutoSyncManager

if TYPE_CHECKING:
    from ariadne_graph.core.watch_sync import WatchSyncManager
from ariadne_graph.core.capabilities import get_capabilities
from ariadne_graph.core.communities import CommunityAnalyzer
from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.embeddings import (
    EMBEDDING_TEXT_VERSION,
    EmbeddingProvider,
    EmbeddingService,
    LocalEmbeddingProvider,
    embedding_version_tag,
)
from ariadne_graph.core.freshness import FreshnessTracker
from ariadne_graph.core.incremental_sync import IncrementalSync
from ariadne_graph.core.models import CodeNode, ProjectRecord
from ariadne_graph.core.retrieval import GraphRetriever
from ariadne_graph.core.search import HybridSearcher
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.base import LanguageAdapter
from ariadne_graph.mcp.fallbacks import GraphStoreFallbacks
from ariadne_graph.mcp.schemas import (
    ArchitectureOutput,
    AuditPublicSurfacesInput,
    AuditPublicSurfacesOutput,
    CapabilitiesInput,
    CapabilitiesOutput,
    ChangeBriefingInput,
    ChangeBriefingOutput,
    CommunitiesOutput,
    DeleteProjectInput,
    DeleteProjectOutput,
    DependencyMatrixOutput,
    DetectChangesInput,
    DetectChangesOutput,
    EquivalentCandidate,
    ExplainEdgeInput,
    ExplainEdgeOutput,
    FindEquivalentInput,
    FindEquivalentOutput,
    FindHotspotsInput,
    FindHotspotsOutput,
    GetArchitectureInput,
    GetDependencyMatrixInput,
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
    PlacementCandidate,
    ProjectListOutput,
    RetrieveInput,
    RetrieveOutput,
    SearchCodeInput,
    SearchCodeOutput,
    SearchSemanticInput,
    SearchSemanticOutput,
    SuggestPlacementInput,
    SuggestPlacementOutput,
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


# Node labels that denote a DEFINITION of a symbol (as opposed to an import,
# usage, file, or diagnostic). Used by placement symbol resolution and as the
# default type filter for find_equivalent — both want real definitions, not the
# file/import/module nodes that also carry the name. Covers Python (function/
# method/class/variable) AND TypeScript (interface/type-alias/hook/react-component)
# definition kinds the extractors emit.
_DEFINITION_LABELS = frozenset(
    {
        "CodeFunction",
        "CodeMethod",
        "CodeClass",
        "CodeVariable",
        "CodeInterface",
        "CodeTypeAlias",
        "CodeHook",
        "CodeReactComponent",
    }
)

# Upper bound on the semantic pool find_equivalent fetches when the type filter
# means matching definitions may rank below non-matching nodes. We fetch the whole
# graph up to this ceiling so the post-fetch filter can't drop a match that only
# exists past a small window; beyond it the tool warns rather than silently caps.
_EQUIVALENT_POOL_CEILING = 5000


def _record_props(node: dict[str, Any]) -> dict[str, Any]:
    """Node properties from a store record, tolerant of both shapes: SQLite/Memory
    nest them under ``properties``; Neo4j's ``n {.*, id, labels}`` FLATTENS them to
    the top level. Prefer the nested dict; else treat the record itself as the
    property bag (dropping the structural id/labels keys)."""
    props = node.get("properties")
    if isinstance(props, dict):
        return props
    return {k: v for k, v in node.items() if k not in ("id", "labels", "properties")}


def _record_labels(node: dict[str, Any]) -> list[Any]:
    """Node labels from a store record (top-level on both shapes)."""
    labels = node.get("labels")
    return labels if isinstance(labels, list) else []


def _parse_embedding_version(tag: str | None) -> int | None:
    """Extract the text-schema version N from a "{model}#v{N}" embedding tag.

    Returns None for legacy vectors written before the tag existed (model was
    NULL) or any unrecognised tag — those are treated as stale.
    """
    if not tag or "#v" not in tag:
        return None
    suffix = tag.rsplit("#v", 1)[-1]
    return int(suffix) if suffix.isdigit() else None


def _norm_file_key(file_path: str | None) -> str | None:
    """Canonicalise a file path for identity comparison.

    Node ``file_path`` is written canonicalised (``str(repo_root.resolve()/rel)``)
    while edge ``owner_file_path`` comes from the raw filesystem walk. The two
    strings diverge for the *same* file when the repo root is reached via a
    non-canonical path (symlink, macOS firmlink, ``..``/``.`` segments), which
    made ``inspect_file`` match a file's edges without its nodes. Routing both
    the caller's query and every stored key through this normaliser keys them on
    one identity. ``resolve()`` collapses symlinks/firmlinks and ``.``/``..``
    even for paths that no longer exist on disk.
    """
    if not file_path:
        return file_path
    return str(Path(file_path).resolve())


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


# Fan-in at or above this many distinct external consumers marks an internal
# file as a "promote to public surface" candidate. Deliberately small: any
# external reach into internals more than once is worth a maintainer's look.
_HIGH_FAN_IN_THRESHOLD = 2

# Each `from <module> import <symbol>` statement's declared module path, keyed
# to the FILE that wrote it. This is the only signal that survives past symbol
# resolution to tell "imported through a re-exporting facade" apart from
# "imported the internal directly" -- see _audit_public_surfaces.
_IMPORT_FROM_MODULE_SQL = """
SELECT json_extract(sn.properties, '$.file_path') AS file_path,
       json_extract(e.properties, '$.from_module') AS from_module
FROM edges e
JOIN nodes sn ON sn.graph_id = e.graph_id AND sn.id = e.source
WHERE e.graph_id = ?
  AND e.rel_type = 'IMPORTS_SYMBOL'
  AND json_extract(sn.properties, '$.file_path') IS NOT NULL
  AND json_extract(e.properties, '$.from_module') IS NOT NULL
  AND json_extract(e.properties, '$.from_module') != ''
"""


def _module_name_candidates(rel_path: str) -> set[str]:
    """Dotted module-name spellings a file's path could appear as in a
    ``from <module> import ...`` statement.

    Mirrors the extractor's own naming (:func:`_module_name_from_path` in
    ``languages/python_ast/extractor.py``) PLUS the un-stripped form, since
    import statements are written by hand and may or may not include a `src`
    source-root prefix (both ``from src.core import x`` and
    ``from core import x`` occur in the wild depending on how the package is
    installed/run).
    """
    stem = rel_path[: -len(".py")] if rel_path.endswith(".py") else rel_path
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    dotted = stem.replace("/", ".")
    candidates = {dotted}
    if dotted.startswith("src."):
        candidates.add(dotted[len("src.") :])
    return candidates


# Extension/index spellings a bare TS/JS import specifier can resolve to, in the
# order tsc/node try them: exact file with each extension, then a directory
# barrel `index.<ext>`. Mirrors TsConfigResolver's own extension list.
_TS_MODULE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".d.ts")


def _resolve_ts_specifier(specifier: str, consumer_rel: str, files: set[str]) -> str | None:
    """Resolve a raw TS/JS import specifier to a repo-relative file in *files*.

    Unlike Python's dotted ``from X import Y``, a TS ``from_module`` is the
    literal specifier string (``'./engine'``, ``'../core'``, ``'../core/x'``).
    Relative specifiers are resolved against the importing file's directory and
    matched against the graph's known files by trying, in order: the path as
    written, the path with each source extension, and the path's ``index.<ext>``
    barrel. Bare specifiers that don't land on a known repo file are external
    (an npm package) and return ``None`` -- they are not consumers of this
    repo's own public surface.

    tsconfig ``paths``/``baseUrl`` aliases are already resolved at extraction
    time (:class:`TsConfigResolver`, surfaced as the edge's ``resolved_source``);
    this handles the relative/bare specifiers that resolver leaves untouched.
    """
    if not specifier.startswith("."):
        # Bare specifier: only a consumer if it happens to name a repo file
        # (rare, but honor an explicit in-repo bare path); otherwise external.
        base = specifier
    else:
        consumer_dir = _dir_of(consumer_rel)
        parts = consumer_dir.split("/") if consumer_dir else []
        for segment in specifier.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(segment)
        base = "/".join(parts)

    if base in files:
        return base
    for ext in _TS_MODULE_EXTS:
        candidate = base + ext
        if candidate in files:
            return candidate
    for ext in _TS_MODULE_EXTS:
        candidate = f"{base}/index{ext}" if base else f"index{ext}"
        if candidate in files:
            return candidate
    return None


def _audit_public_surfaces(
    files: list[str],
    dep_edges: list[tuple[str, str]],
    import_edges: list[tuple[str, str]],
    modules_with_surfaces: dict[str, ModuleSpec],
    arch_config: ArchitectureConfig,
) -> list[dict[str, Any]]:
    """Pure facade/encapsulation report over resolved file->file dep edges.

    For each module with a declared ``public_surfaces`` list: which of its
    files ARE the surface, which external consumers go through the surface vs.
    deep-import an internal, which surface files have zero external consumers,
    which internals have high external fan-in (promotion candidates), and
    whether the surface is a barrel that hides nothing (the module owns no
    internal files beyond its declared surface).

    ``dep_edges`` (file->file, SCIP/CALLS-resolved -- ``_DEP_EDGE_SQL``) drives
    consumer EXISTENCE and fan-in counts, but its edge target is always the
    symbol's *definition* file, even when the caller imported it through a
    re-exporting facade -- so it cannot by itself distinguish "went through the
    surface" from "reached the internal directly". ``import_edges``
    (importing file, dotted ``from_module`` of its ``from X import Y``
    statement -- ``IMPORTS_SYMBOL``/``IMPORTS``) supplies that missing signal:
    it is matched against each file's own possible dotted-module spellings to
    recover which *declared* module path the consumer actually wrote in source.
    """
    all_files = set(files)
    reports: list[dict[str, Any]] = []

    # module-name -> file path, built once over every file the graph knows about.
    # Keyed by both the file's dotted Python module spellings AND its own
    # repo-relative path, so an ``import_edges`` target may be either a dotted
    # module (Python `from X import Y`) or an already-resolved file path (a TS
    # specifier resolved by _resolve_ts_specifier at the handler edge).
    name_to_file: dict[str, str] = {}
    for f in all_files:
        name_to_file[f] = f
        for name in _module_name_candidates(f):
            name_to_file[name] = f

    # For each importing file, the set of file paths its `from_module`s resolve
    # to -- i.e. what it literally imported from, regardless of which symbol
    # inside eventually got called.
    imported_files_by_consumer: dict[str, set[str]] = {}
    for src, from_module in import_edges:
        target = name_to_file.get(from_module)
        if target is not None:
            imported_files_by_consumer.setdefault(src, set()).add(target)

    # External-consumer candidates: any file with at least one dep edge or
    # import edge, so a consumer is never missed just because one of the two
    # signals didn't fire for it.
    consumer_candidates = {src for src, _ in dep_edges} | {src for src, _ in import_edges}

    for mod_name, spec in modules_with_surfaces.items():
        # public_surfaces entries are glob patterns (mirrors ModuleSpec.paths),
        # so membership is a glob match, not exact-string containment.
        surface_files = sorted(
            f
            for f in all_files
            if arch_config.module_of(f) == mod_name
            and any(fnmatch.fnmatch(f, pat) for pat in spec.public_surfaces)
        )
        internal_files = sorted(
            f for f in all_files if arch_config.module_of(f) == mod_name and f not in surface_files
        )
        surface_set, internal_set = set(surface_files), set(internal_files)

        # Fan-in still comes from the resolved dep-edge graph (it answers "does
        # an external file reach this internal's SYMBOLS at all", independent
        # of which module path it imported through).
        fan_in: dict[str, set[str]] = {f: set() for f in internal_files}
        for src, dst in dep_edges:
            if dst in internal_set and arch_config.module_of(src) != mod_name:
                fan_in[dst].add(src)

        via_surface: set[tuple[str, str]] = set()
        deep_import: set[tuple[str, str]] = set()
        surface_consumers: dict[str, set[str]] = {f: set() for f in surface_files}

        for src in consumer_candidates:
            if arch_config.module_of(src) == mod_name:
                continue  # internal-to-internal traffic isn't a consumer
            imported = imported_files_by_consumer.get(src, set())
            for surface in imported & surface_set:
                via_surface.add((src, surface))
                surface_consumers[surface].add(src)
            for internal in imported & internal_set:
                deep_import.add((src, internal))

        unused_public_exports = sorted(f for f in surface_files if not surface_consumers[f])
        high_fan_in_internals = sorted(
            (
                {"internal": f, "external_fan_in": len(consumers)}
                for f, consumers in fan_in.items()
                if len(consumers) >= _HIGH_FAN_IN_THRESHOLD
            ),
            key=lambda r: (-r["external_fan_in"], r["internal"]),
        )

        reports.append(
            {
                "module": mod_name,
                "public_exports": surface_files,
                "via_surface_consumers": [
                    {"consumer": c, "surface": s} for c, s in sorted(via_surface)
                ],
                "deep_import_consumers": [
                    {"consumer": c, "internal": i} for c, i in sorted(deep_import)
                ],
                "unused_public_exports": unused_public_exports,
                "high_fan_in_internals": high_fan_in_internals,
                # A barrel with no encapsulation value: the surface hides zero
                # internal files, i.e. the module owns nothing beyond what it
                # already exports.
                "is_all_exporting_barrel": len(internal_files) == 0,
            }
        )

    return reports


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Holds all 23 MCP tool handlers and their shared resources."""

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

    async def _freshness_envelope(self, graph_id: str | None) -> dict[str, Any] | None:
        """Compute the shared in-band freshness envelope for an analysis response.

        Delegates to :meth:`FreshnessTracker.compute_freshness` (cheap mtime
        prefilter — see its docstring). Returns ``None`` for an unknown/unindexed
        graph so the response's optional ``freshness`` field simply stays absent.
        A watcher-enabled project needs no special dirty-set plumbing: the watcher
        reindexes on every change, so ``last_indexed`` advances and the mtime
        prefilter finds nothing dirty. Never raises — a freshness failure must not
        break the analysis result it rides along with.
        """
        if graph_id is None:
            return None
        try:
            return await self.freshness_tracker.compute_freshness(graph_id)
        except Exception as exc:
            logger.warning("Freshness envelope computation failed for %s: %s", graph_id, exc)
            return None

    async def _resolution_envelope(self, graph_id: str | None) -> dict[str, Any] | None:
        """Compute the shared in-band resolution-provenance envelope (bead 1u5).

        Delegates to :func:`read_resolution_coverage`; rides the response the same
        way as :meth:`_freshness_envelope`. Returns ``None`` for an unknown graph
        so the optional ``resolution`` field stays absent. Never raises — a
        provenance failure must not break the analysis result it rides along with.
        """
        if graph_id is None:
            return None
        try:
            return await read_resolution_coverage(self.graph_store, graph_id)
        except Exception as exc:
            logger.warning("Resolution envelope computation failed for %s: %s", graph_id, exc)
            return None

    async def _freshness_envelope_multi(self, graph_ids: list[str]) -> dict[str, Any] | None:
        """Aggregate freshness across every graph a result was drawn from.

        The cross-repo search/trace handlers combine hits from all known graphs,
        so reporting one graph's freshness would let a fresh graph mask a stale
        one. This rolls the per-graph envelopes up honestly, never letting a
        known-clean graph paper over a gap in another:

        - A graph whose lookup returns ``None`` (unindexed / lookup failed) is a
          coverage gap, tracked as ``missing`` so the aggregate cannot claim a
          confident ``False``.
        - ``stale`` is ``True`` if ANY graph is confirmed stale; else ``None``
          (unknown) if any contributor is stale-unknown OR missing; else
          ``False``.
        - ``dirty_file_count`` sums the per-graph counts, but is ``None`` if ANY
          contributor's count is unknown or missing (a partial sum would
          understate).
        - ``last_indexed`` is the OLDEST (the freshness floor); ``sync_enabled``
          is ``True`` only if every contributor has sync on.

        Returns ``None`` only when NO graph id was supplied at all.
        """
        if not graph_ids:
            return None
        envelopes = [await self._freshness_envelope(gid) for gid in graph_ids]
        present = [e for e in envelopes if e is not None]
        if not present:
            return None
        if len(envelopes) == 1 and present:
            return present[0]

        missing = len(envelopes) - len(present)  # graphs with no envelope at all

        stales = [e["stale"] for e in present]
        if any(s is True for s in stales):
            stale: bool | None = True
        elif missing or any(s is None for s in stales):
            stale = None
        else:
            stale = False

        counts = [e["dirty_file_count"] for e in present]
        # A partial sum understates, so any unknown/missing count makes the
        # aggregate count unknown rather than a misleadingly precise number.
        dirty_total = sum(counts) if (not missing and all(c is not None for c in counts)) else None

        indexed = [e["last_indexed"] for e in present if e["last_indexed"] is not None]
        oldest = min(indexed) if indexed else None  # ISO-8601 sorts lexicographically

        return {
            "last_indexed": oldest,
            "dirty_file_count": dirty_total,
            "stale": stale,
            "sync_enabled": all(e["sync_enabled"] for e in present),
        }

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
            target = _norm_file_key(file_path)
            nodes = await self.graph_store.query(graph_id, "nodes")
            result = []
            for row in nodes:
                node_data = row.get("n", row)
                props = node_data.get("properties", {})
                if _norm_file_key(props.get("file_path")) == target:
                    result.append(node_data)
            return result
        except Exception:
            return []

    async def _get_edges_for_file(self, graph_id: str, file_path: str) -> list[dict[str, Any]]:
        """Get all edges originating from a specific file."""
        try:
            target = _norm_file_key(file_path)
            edges = await self.graph_store.query(graph_id, "edges")
            result = []
            for row in edges:
                edge_data = row.get("r", row)
                props = edge_data.get("properties", {})
                if _norm_file_key(props.get("owner_file_path")) == target:
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

            self.auto_sync_manager = AutoSyncManager(self, self.config.incremental_sync_interval)
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
                # Offload the synchronous filesystem walk so the daemon event
                # loop keeps serving HTTP during discovery on large repos.
                found = await asyncio.to_thread(adapter.discover_files, Path(repo_path), config)
                all_current_files.extend(found)
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
        # dep_edges capability (SQLite + Memory); backends without it (e.g.
        # Neo4j today) skip, and their tool responses say so explicitly.
        if getattr(self.graph_store, "supports_dep_edges", False):
            try:
                written = await persist_architecture_diagnostics(
                    self.graph_store, graph_id, repo_path
                )
                logger.info("Architecture analysis wrote %d findings", written)
            except Exception as exc:
                logger.warning("Architecture analysis failed: %s", exc)
        else:
            # Explicit, not silent: a backend without the dep_edges capability
            # (Neo4j today) receives NO architecture hygiene — the bug this bead
            # removed. Surface it at WARNING so a Neo4j deployment is not left
            # quietly believing its graph is healthy.
            logger.warning(
                "Architecture analysis skipped: graph store %s does not "
                "implement the dep_edges capability (no hygiene findings).",
                type(self.graph_store).__name__,
            )

        # Also compute embeddings for changed files if a provider is available
        if (
            self.embedding_service is not None
            and self.searchable_store is not None
            and changed_files
        ):
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
        await self._record_index_meta(repo_path, graph_id, total_indexed, sync_enabled=sync_enabled)
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

    async def handle_targeted_sync(self, repo_path: str, changed_files: list[Path]) -> IndexOutput:
        """Sync a known set of changed files without rediscovering the repo.

        This is the entry point used by the filesystem watcher. It groups
        changed files by language adapter, runs a targeted incremental sync
        for each adapter, and re-computes embeddings for affected nodes.
        """
        resolved = str(Path(repo_path).resolve())
        graph_id = self._get_graph_id(resolved)
        config = self.config.model_copy(update={"repo_root": Path(resolved), "graph_id": graph_id})

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
                report = await sync.targeted_sync(adapter, owned, all_known_files=all_current_files)
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
        await self._record_index_meta(resolved, graph_id, total_indexed, sync_enabled=True)
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
            embeddings=await self._embedding_status(graph_id),
            message=f"Indexed {file_count} files; {len(dirty_files)} files changed since last index",
        )

    async def _embedding_status(self, graph_id: str) -> dict[str, Any]:
        """Embedding provenance for a graph: whether stored vectors match the
        CURRENT ``{model}#v{version}`` tag, or are stale (built by an older text
        schema OR a different embedding model — both make stored vectors unusable).

        Reads the per-vector ``model`` tags written by
        :meth:`EmbeddingService.embed_nodes` and compares the FULL tag against the
        current provider's expected tag (not just the version suffix — a model
        swap at the same schema version is just as stale). Decision (1): staleness
        is REPORTED here so the operator re-indexes; it is never silently
        auto-re-embedded inside this read-only status call, and a stale-but-reused
        vector is surfaced rather than hidden. Empty when the store is not SQLite.
        """
        current_version = EMBEDDING_TEXT_VERSION
        # Expected full tag for the current model, when a provider is configured.
        current_tag = (
            embedding_version_tag(self.embedding_provider.model_name)
            if self.embedding_provider is not None
            else None
        )
        base: dict[str, Any] = {
            "current_version": current_version,
            "current_tag": current_tag,
            "stored_tags": [],
            "stale": False,
            "embedded_count": 0,
            # Provenance is only queryable on SQLite (the model tag lives in the
            # SQLite embeddings table). For other backends we CANNOT read it, so
            # 'supported' is False and embedded_count 0 means "unknown", NOT
            # "zero embeddings" — callers must not treat it as an empty index.
            "supported": True,
        }
        if not isinstance(self.graph_store, SQLiteGraphStore):
            return {**base, "supported": False}
        try:
            db = await self.graph_store._connect()
            try:
                cur = await db.execute(
                    "SELECT model, COUNT(*) AS n FROM embeddings WHERE graph_id = ? GROUP BY model",
                    (graph_id,),
                )
                rows = await cur.fetchall()
                # Eligibility denominator = nodes the index CAN embed. The index
                # embeds nodes per (re)indexed FILE (via nodes_by_file), so a node
                # with no file_path — SCIP CodeExternalModule / external import nodes
                # — is never embeddable and must NOT count against coverage, or a
                # fully-rebuilt index would look permanently partial. Counting only
                # file-backed nodes lets us still detect a genuinely partial index
                # (semantic enabled on an already-indexed repo, only some files
                # re-embedded).
                ncur = await db.execute(
                    "SELECT COUNT(*) AS n FROM nodes "
                    "WHERE graph_id = ? AND file_path IS NOT NULL AND file_path != ''",
                    (graph_id,),
                )
                nrow = await ncur.fetchone()
                total_nodes = int(nrow["n"]) if nrow else 0
            finally:
                await db.close()
        except Exception as exc:
            logger.warning("Embedding status read failed for %s: %s", graph_id, exc)
            return {**base, "supported": False}

        stored_tags: list[str | None] = []
        embedded_count = 0
        stale = False
        for row in rows:
            tag = row["model"]
            embedded_count += int(row["n"])
            stored_tags.append(tag)
            # Stale if the version differs from current, or (when we know the
            # current provider's tag) the full model#version tag differs.
            version_mismatch = _parse_embedding_version(tag) != current_version
            tag_mismatch = current_tag is not None and tag != current_tag
            if version_mismatch or tag_mismatch:
                stale = True
        # Coverage: fraction of graph nodes that carry a vector. Materially <1.0
        # means the index is partial and find_equivalent would miss the unembedded
        # files. (A small slack absorbs benign off-by-a-few from node churn.)
        coverage = (embedded_count / total_nodes) if total_nodes else 0.0
        return {
            **base,
            "stored_tags": sorted({t for t in stored_tags if t is not None})
            + ([None] if any(t is None for t in stored_tags) else []),
            "stale": stale and embedded_count > 0,
            "embedded_count": embedded_count,
            "eligible_count": total_nodes,
            "coverage": round(coverage, 3),
            "coverage_complete": embedded_count > 0 and coverage >= 0.95,
        }

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
        freshness = await self._freshness_envelope(graph_id)

        # Use GraphRetriever when available
        if self.retriever is not None:
            try:
                result = await self.retriever.retrieve_node(graph_id, query)
                results: list[dict[str, Any]] = [{"type": "retrieve", "data": result}]
                return RetrieveOutput(results=results, freshness=freshness)
            except Exception as exc:
                logger.warning("GraphRetriever failed, falling back to scan: %s", exc)

        out = await GraphStoreFallbacks.retrieve(self.graph_store, graph_id, query)
        out.freshness = freshness
        return out

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
            freshness=canonical.freshness,
        )

    def _semantic_ready(self) -> bool:
        """Whether semantic search can actually run (not just is wired).

        A :class:`LocalEmbeddingProvider` imports ``sentence_transformers`` lazily,
        so its mere presence is not readiness — we check the runtime capability.
        A CUSTOM provider (any other :class:`EmbeddingProvider`) is trusted as
        ready: it does not depend on the local extra, so gating it on
        ``sentence_transformers`` would wrongly disable a valid configuration.
        """
        if self.searcher is None or self.embedding_provider is None:
            return False
        if isinstance(self.embedding_provider, LocalEmbeddingProvider):
            return bool(get_capabilities()["features"]["semantic_embeddings"]["available"])
        return True

    async def handle_search_semantic(self, input: SearchSemanticInput) -> SearchSemanticOutput:
        """Search the graph using semantic (vector) similarity."""
        if self.searchable_store is None:
            return SearchSemanticOutput(
                hits=[],
                message="Semantic search is not available. No SearchableGraphStore configured.",
            )

        if not self._semantic_ready():
            return SearchSemanticOutput(
                hits=[],
                message=(
                    "DEGRADED: semantic (vector) search is OFF — the semantic extra is "
                    "not installed, so retrieval quality is degraded and behavioural "
                    "matches will be missed. Keyword search (code_graph_search_code) "
                    'still works. Install with: pip install -e ".[semantic]" and re-index.'
                ),
            )

        # _semantic_ready() guarantees a searcher; bind a non-optional local so
        # the type is narrowed (mypy does not track the helper's postcondition).
        searcher = self.searcher
        assert searcher is not None

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
                hits = await searcher.search(
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
                hit
                for hit in all_hits
                if any(label.lower() in type_set for label in _hit_labels(hit))
            ]

        all_hits.sort(key=lambda h: h.get("score", 0.0), reverse=True)
        all_hits = all_hits[: input.limit]

        return SearchSemanticOutput(
            hits=all_hits,
            message=f"Found {len(all_hits)} semantic matches",
            freshness=await self._freshness_envelope_multi(graph_ids),
        )

    async def handle_search_code(self, input: SearchCodeInput) -> SearchCodeOutput:
        """Search code by pattern/keyword through the graph."""
        if input.repo_path:
            graph_ids = [self._get_graph_id(input.repo_path)]
        else:
            graph_ids = await self._known_graph_ids()
        if not graph_ids:
            return SearchCodeOutput(matches=[], message="No indexed graphs found")

        freshness = await self._freshness_envelope_multi(graph_ids)
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
                logger.warning("Failed to fetch node %s for language filter: %s", node_id, exc)
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
                            node = await _fetch_hit_node(graph_id, hit.get("node_id", ""))
                        node = node or {}
                        props = node.get("properties", {}) if isinstance(node, dict) else {}
                        if input.language:
                            node_lang = _language_from_props(props)
                            if input.language.lower() not in node_lang.lower():
                                continue
                        matches.append(
                            {
                                "graph_id": graph_id,
                                "node_id": hit.get("node_id"),
                                "labels": node.get("labels", []),
                                "properties": props,
                                "score": hit.get("score", 0.0),
                            }
                        )
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

                    searchable = " ".join(
                        [
                            str(props.get("name", "")),
                            str(props.get("qualname", "")),
                            str(props.get("file_path", "")),
                        ]
                    ).lower()

                    if pattern in searchable:
                        matches.append(
                            {
                                "graph_id": graph_id,
                                "node_id": node_data.get("id"),
                                "labels": node_data.get("labels", []),
                                "properties": props,
                            }
                        )
                        if len(matches) >= input.limit:
                            return SearchCodeOutput(
                                matches=matches,
                                message=f"Found {len(matches)} matches",
                                freshness=freshness,
                            )
            except Exception:
                continue

        matches.sort(key=lambda m: m.get("score", 0.0), reverse=True)
        matches = matches[: input.limit]

        return SearchCodeOutput(
            matches=matches,
            message=f"Found {len(matches)} matches" if matches else "No matches found",
            freshness=freshness,
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

        freshness = await self._freshness_envelope_multi(graph_ids)
        return TraceDependenciesOutput(paths=all_paths, freshness=freshness)

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
            freshness = await self._freshness_envelope(graph_id)
            if self.retriever is not None:
                try:
                    result = await self.retriever.impact_analysis(graph_id, symbol)
                    return ImpactAnalysisOutput(
                        target_symbol=result.target_symbol,
                        total_affected=result.total_affected,
                        direct_dependencies=result.direct_dependencies,
                        transitive_affected=result.transitive_affected,
                        coupling_scores=result.coupling_scores,
                        freshness=freshness,
                    )
                except Exception as exc:
                    logger.warning("GraphRetriever impact analysis failed: %s", exc)

            out = await GraphStoreFallbacks.impact_analysis(self.graph_store, graph_id, symbol)
            out.freshness = freshness
            return out

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
                added=[],
                modified=[],
                deleted=[],
                message="Repository has not been indexed yet",
            )

        config = self.config.model_copy(update={"repo_root": Path(repo_path), "graph_id": graph_id})
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

            report = await sync.get_changed_files_since_ref(graph_id, all_files, input.since_ref)
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

        # Neither CommunityAnalyzer.find_hotspots nor the GraphStoreFallbacks
        # equivalent support offset+limit natively (only a top_n cutoff), and
        # neither reports the true candidate count. Fetch one entry past the
        # requested page to page/has_more here without changing either
        # ranking implementation: an oversized result means more remain.
        fetch_n = input.offset + input.top_n + 1
        freshness = await self._freshness_envelope(graph_id)

        if self.community_analyzer is not None:
            try:
                analyzer_hotspots = await self.community_analyzer.find_hotspots(
                    graph_id, top_n=fetch_n, metric=input.metric
                )
                page = analyzer_hotspots[input.offset : input.offset + input.top_n]
                has_more = len(analyzer_hotspots) > input.offset + len(page)
                total = input.offset + len(analyzer_hotspots)
                hotspots = [] if input.summary_only else [h.model_dump() for h in page]
                return FindHotspotsOutput(
                    hotspots=hotspots,
                    total=total,
                    has_more=has_more,
                    message=f"Top {len(page)} hotspots by {input.metric}",
                    freshness=freshness,
                )
            except Exception as exc:
                logger.warning("CommunityAnalyzer.find_hotspots failed: %s", exc)

        fallback = await GraphStoreFallbacks.find_hotspots(
            self.graph_store, graph_id, fetch_n, input.metric
        )
        page = fallback.hotspots[input.offset : input.offset + input.top_n]
        has_more = len(fallback.hotspots) > input.offset + len(page)
        total = input.offset + len(fallback.hotspots)
        return FindHotspotsOutput(
            hotspots=[] if input.summary_only else page,
            total=total,
            has_more=has_more,
            message=fallback.message,
            freshness=freshness,
        )

    def _paginate_architecture(
        self, output: ArchitectureOutput, input: GetArchitectureInput
    ) -> ArchitectureOutput:
        """Apply offset/limit/summary_only to an ArchitectureOutput's communities list.

        Both the CommunityAnalyzer and GraphStoreFallbacks code paths build a
        ``summary`` dict with a ``"communities"`` list; paginating once here
        (rather than in each producer) keeps the ranking/detection logic
        untouched.
        """
        communities = output.summary.get("communities", [])
        total = len(communities)
        page = communities[input.offset : input.offset + input.limit]
        has_more = input.offset + len(page) < total

        summary = dict(output.summary)
        summary["communities"] = [] if input.summary_only else page
        return ArchitectureOutput(
            summary=summary,
            total=total,
            has_more=has_more,
            message=output.message,
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

        freshness = await self._freshness_envelope(graph_id)
        resolution = await self._resolution_envelope(graph_id)
        if self.community_analyzer is not None:
            try:
                summary_obj = await self.community_analyzer.get_architecture_summary(
                    graph_id, granularity=input.granularity
                )
                output = ArchitectureOutput(
                    summary=summary_obj.model_dump(),
                    message=(
                        f"Architecture: {summary_obj.total_communities} communities, "
                        f"{summary_obj.total_files} files, "
                        f"{summary_obj.total_entities} entities"
                    ),
                )
                paginated = self._paginate_architecture(output, input)
                paginated.freshness = freshness
                paginated.resolution = resolution
                return paginated
            except Exception as exc:
                logger.warning("CommunityAnalyzer architecture summary failed: %s", exc)

        fallback = await GraphStoreFallbacks.get_architecture(self.graph_store, graph_id)
        paginated = self._paginate_architecture(fallback, input)
        paginated.freshness = freshness
        paginated.resolution = resolution
        return paginated

    async def handle_explain_edge(self, input: ExplainEdgeInput) -> ExplainEdgeOutput:
        """Explain why a single file->file edge is or isn't a layering violation."""
        explanation = explain_edge(input.src_path, input.dst_path)
        # explain_edge is a pure static classification of two paths against the
        # layering charter — it touches no indexed graph, so there is no
        # freshness to report and the (optional) envelope stays None.
        return ExplainEdgeOutput(
            src=explanation.src,
            dst=explanation.dst,
            src_organ=explanation.src_organ,
            dst_organ=explanation.dst_organ,
            allowed=explanation.allowed,
            reason=explanation.reason,
            rule=explanation.rule,
            front_door_would_fix=explanation.front_door_would_fix,
            message=(
                f"{explanation.src} -> {explanation.dst}: {explanation.reason}"
                + ("" if explanation.allowed else " (violation)")
            ),
        )

    async def handle_get_dependency_matrix(
        self, input: GetDependencyMatrixInput
    ) -> DependencyMatrixOutput:
        """Get the file/directory/module-level dependency graph (nodes + edges).

        Requires the ``dep_edges`` capability (SQLite + Memory); a backend
        without it returns an explicit unsupported message, never a silent skip.
        """
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return DependencyMatrixOutput(message="Repository has not been indexed yet")

        if not getattr(self.graph_store, "supports_dep_edges", False):
            return DependencyMatrixOutput(
                message=(
                    "Dependency matrix is unsupported on this graph store "
                    f"({type(self.graph_store).__name__}): it does not implement "
                    "the dep_edges capability."
                )
            )

        matrix = await read_dependency_matrix(
            self.graph_store, graph_id, repo_path, group_by=input.group_by
        )
        return DependencyMatrixOutput(
            nodes=[
                {"id": n.id, "module": n.module, "production": n.production} for n in matrix.nodes
            ],
            edges=[
                {
                    "source": e.source,
                    "target": e.target,
                    "import_count": e.import_count,
                    "violation_count": e.violation_count,
                }
                for e in matrix.edges
            ],
            message=(
                f"Dependency matrix ({input.group_by}): {len(matrix.nodes)} nodes, "
                f"{len(matrix.edges)} edges"
            ),
            freshness=await self._freshness_envelope(graph_id),
            resolution=await self._resolution_envelope(graph_id),
        )

    async def handle_audit_public_surfaces(
        self, input: AuditPublicSurfacesInput
    ) -> AuditPublicSurfacesOutput:
        """Facade/encapsulation audit over declared `public_surfaces`.

        Read-only report over the same file->file dep-edge SSOT as
        :func:`~ariadne_graph.core.architecture.persist_architecture_diagnostics`
        (``_DEP_EDGE_SQL``/``_FILE_SQL``) plus the declared
        :class:`~ariadne_graph.core.architecture_config.ArchitectureConfig`.
        Meaningless without a declared ``public_surfaces`` list, so with no
        `.ariadne/architecture.yml` (or no module declares surfaces) this
        returns an empty report and says why.
        """
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        arch_config = load_architecture_config(Path(repo_path))
        if arch_config is None:
            return AuditPublicSurfacesOutput(
                modules=[],
                message=(
                    "No .ariadne/architecture.yml found. This audit requires "
                    "declared public_surfaces per module -- see "
                    "ArchitectureConfig.public_surfaces."
                ),
            )
        modules_with_surfaces = {
            name: spec for name, spec in arch_config.modules.items() if spec.public_surfaces
        }
        if not modules_with_surfaces:
            return AuditPublicSurfacesOutput(
                modules=[],
                message=(
                    ".ariadne/architecture.yml declares no public_surfaces on "
                    "any module. This audit requires at least one module with "
                    "a declared public_surfaces list."
                ),
            )

        exists = await self._graph_exists(graph_id)
        if not exists:
            return AuditPublicSurfacesOutput(
                modules=[], message="Repository has not been indexed yet"
            )
        # Needs the dep_edges capability AND the SQL ``from_module`` import query
        # (``_IMPORT_FROM_MODULE_SQL``, not part of the capability). A store
        # missing either is reported explicitly, never silently skipped.
        connect = _sql_connect(self.graph_store)
        if not getattr(self.graph_store, "supports_dep_edges", False) or connect is None:
            return AuditPublicSurfacesOutput(
                modules=[],
                message=(
                    "Public-surfaces audit is unsupported on this graph store "
                    f"({type(self.graph_store).__name__}): it needs the dep_edges "
                    "capability and the SQL from_module import query."
                ),
            )

        dep_rows = await self.graph_store.dep_edges(graph_id)
        file_rows = await _read_code_files(self.graph_store, graph_id)
        db = await connect()
        try:
            import_cursor = await db.execute(_IMPORT_FROM_MODULE_SQL, (graph_id,))
            import_rows = await import_cursor.fetchall()
        finally:
            await db.close()

        files = [_rel(fp, repo_path) for _nid, fp in file_rows]
        file_set = set(files)
        dep_edges = [(_rel(sf, repo_path), _rel(tf, repo_path)) for sf, tf in dep_rows]
        # (importing file, import target) -- the literal module path the consumer
        # WROTE, the only signal that survives past symbol resolution to tell a
        # via-surface import from a deep one (see _audit_public_surfaces
        # docstring: the resolved CALLS edge target is the symbol's *definition*
        # site regardless of which module path the caller imported through).
        #
        # Python `from_module` is already a dotted module the core matches
        # directly. A TS `from_module` is a raw relative/bare specifier
        # ('./x', '../core'); resolve it to a repo-relative file path here so
        # the core sees a target it can match (bead code_hygiene_mcp-pzl).
        import_edges: list[tuple[str, str]] = []
        for r in import_rows:
            if not r["from_module"]:
                continue
            consumer = _rel(r["file_path"], repo_path)
            specifier = r["from_module"]
            resolved = _resolve_ts_specifier(specifier, consumer, file_set)
            import_edges.append((consumer, resolved if resolved is not None else specifier))

        modules = _audit_public_surfaces(
            files, dep_edges, import_edges, modules_with_surfaces, arch_config
        )
        return AuditPublicSurfacesOutput(
            modules=modules,
            message=f"Audited {len(modules)} module(s) with declared public_surfaces",
            freshness=await self._freshness_envelope(graph_id),
        )

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
                assignments = await self.community_analyzer.detect_communities(
                    graph_id, granularity=input.granularity
                )
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
            communities.append(
                {
                    "community_id": comm_id,
                    "member_count": len(members),
                    "members": members[:50],  # Limit for output size
                }
            )

        return CommunitiesOutput(
            communities=communities,
            message=f"Found {len(communities)} communities",
            freshness=await self._freshness_envelope(graph_id),
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
                nodes=[],
                edges=[],
                message=f"File {file_path} not found in any indexed graph",
            )

        all_nodes = await self._get_nodes_for_file(graph_id, file_path)
        all_edges = (
            await self._get_edges_for_file(graph_id, file_path) if input.include_edges else []
        )

        total_nodes = len(all_nodes)
        total_edges = len(all_edges)

        if input.summary_only:
            nodes, edges = [], []
        else:
            nodes = all_nodes[input.offset : input.offset + input.limit]
            edges = all_edges[input.offset : input.offset + input.limit]

        has_more = (input.offset + len(nodes) < total_nodes) or (
            input.offset + len(edges) < total_edges
        )

        return InspectFileOutput(
            nodes=nodes,
            edges=edges,
            total_nodes=total_nodes,
            total_edges=total_edges,
            has_more=has_more,
            message=f"Found {total_nodes} nodes and {total_edges} edges in {file_path}",
            freshness=await self._freshness_envelope(graph_id),
        )

    async def handle_list_diagnostics(self, input: ListDiagnosticsInput) -> ListDiagnosticsOutput:
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

        # Prod/test is decided by `is_peripheral_path` — the SSOT that classifies
        # BOTH top-level test/script organs AND co-located tests
        # (server/routes/foo.test.ts, server/__tests__/x.ts). Reuse it (with the
        # analysis `_rel`) so counts here agree with the matrix/deep-import rules.
        def _is_test(props: dict[str, Any]) -> bool:
            fp = props.get("file_path")
            if not fp:
                return False
            return is_peripheral_path(_rel(fp, repo_path))

        # First pass: every diagnostic matching level/rule/file_path (and, when
        # requested, production_only). Counts roll up over this FULL match set so
        # they are not distorted by the `limit` page returned in `diagnostics`.
        matched: list[dict[str, Any]] = []
        by_rule: dict[str, int] = {}
        by_production: dict[str, int] = {"production": 0, "test": 0}
        for node_data in diagnostic_nodes:
            props = node_data.get("properties", {})

            if input.level and props.get("level") != input.level:
                continue
            if input.rule and props.get("rule") != input.rule:
                continue
            if input.file_path and props.get("file_path") != input.file_path:
                continue

            is_test = _is_test(props)
            if input.production_only and is_test:
                continue

            rule = props.get("rule")
            if rule:
                by_rule[rule] = by_rule.get(rule, 0) + 1
            by_production["test" if is_test else "production"] += 1

            matched.append(
                {
                    "node_id": node_data.get("id"),
                    "labels": node_data.get("labels", []),
                    "level": props.get("level"),
                    "rule": rule,
                    "message": props.get("message"),
                    "file_path": props.get("file_path"),
                    "timestamp": props.get("timestamp"),
                    "properties": props,
                }
            )

        results = matched[: input.limit]

        return ListDiagnosticsOutput(
            diagnostics=results,
            counts={"by_rule": by_rule, "by_production": by_production},
            message=f"Found {len(matched)} diagnostics ({len(results)} returned)",
            freshness=await self._freshness_envelope(graph_id),
        )

    # ==================================================================
    # BRIEFING TOOL (1) — digested pre-edit guidance
    # ==================================================================

    async def handle_change_briefing(
        self, input: ChangeBriefingInput
    ) -> ChangeBriefingOutput:
        """Digested pre-edit briefing for a file/symbol.

        Composes existing internals only (no new analysis engine): the retriever's
        symbol resolution + upstream trace for callers, the persisted architecture
        diagnostics via :meth:`handle_list_diagnostics` for cycle/layering, the
        public-surface audit, the hotspot ranker, and the shared freshness
        envelope. Output is agent-facing markdown plus structured fields, with
        names/paths only — never bare graph node ids in the prose.

        Input accepts EITHER a symbol (resolved to its owning file) OR a file_path
        (decision (1)). Caller lists are capped at ``max_callers`` per direction
        with the truncation reported explicitly (decision (2): no silent caps).
        """
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        if bool(input.symbol) == bool(input.file_path):
            return ChangeBriefingOutput(
                message="Supply exactly one of 'symbol' or 'file_path'."
            )

        exists = await self._graph_exists(graph_id)
        if not exists:
            return ChangeBriefingOutput(message="Repository has not been indexed yet")

        # ---- resolve target to an owning file (abs + rel) --------------------
        target_abs: str | None = None
        start_node_ids: list[str] = []
        if input.symbol:
            if self.retriever is None:
                return ChangeBriefingOutput(
                    message="Change briefing requires a searchable graph store."
                )
            node = await self.retriever._resolve_symbol(graph_id, input.symbol)
            if node is None:
                return ChangeBriefingOutput(
                    target_symbol=input.symbol,
                    message=f"Symbol {input.symbol!r} not found in the graph.",
                )
            sid = node.get("id", "")
            target_abs = node.get("properties", {}).get("file_path")
            start_node_ids = [sid] if sid else []
        else:
            # A file_path input: normalise to abs and gather its graph nodes so
            # callers trace works file-wide (decision (1): file or symbol input).
            raw = input.file_path or ""
            candidate = raw if Path(raw).is_absolute() else str(Path(repo_path) / raw)
            target_abs = str(Path(candidate).resolve())
            file_nodes = await self._get_nodes_for_file(graph_id, target_abs)
            if not file_nodes:
                return ChangeBriefingOutput(
                    message=f"File {input.file_path!r} not found in the indexed graph.",
                )
            start_node_ids = [n.get("id", "") for n in file_nodes if n.get("id")]

        if not target_abs:
            return ChangeBriefingOutput(
                target_symbol=input.symbol,
                message="Could not resolve the target to a source file.",
            )
        target_rel = _rel(target_abs, repo_path)

        arch_config = load_architecture_config(Path(repo_path))

        def _module_of(rel: str) -> str:
            """Module bucket for a repo-relative file: the declared module when a
            config exists, else the top-level directory (its 'organ')."""
            if arch_config is not None:
                mod = arch_config.module_of(rel)
                if mod:
                    return mod
            head = rel.split("/", 1)[0]
            return head if "/" in rel else "(root)"

        # ---- callers: upstream trace, grouped by module ----------------------
        # Bounded BFS depth: an explicit horizon on transitive callers. Reported
        # in `truncated.callers.max_depth` (never silent) so a consumer knows the
        # frontier was walked this far and no further.
        caller_max_depth = 5
        callers_by_module: dict[str, list[dict[str, Any]]] = {}
        caller_files: set[str] = set()
        callers_truncated = False
        depth_bounded = False
        total_callers = 0
        if start_node_ids and self.retriever is not None:
            # depth 0 is a start node itself; depth 1 = direct callers, >1
            # transitive. Union across every start node (a file has many symbols),
            # keeping the SHALLOWEST depth seen for each caller file.
            best_depth: dict[str, int] = {}
            for sid in start_node_ids:
                try:
                    trace = await self.retriever.trace_dependencies(
                        graph_id, sid, direction="up", max_depth=caller_max_depth
                    )
                except Exception as exc:
                    logger.warning("Change-briefing caller trace failed: %s", exc)
                    continue
                for hit in trace:
                    depth = hit.get("depth", 0)
                    if depth == 0:
                        continue
                    if depth >= caller_max_depth:
                        depth_bounded = True  # frontier hit the horizon
                    fp = hit.get("node", {}).get("properties", {}).get("file_path")
                    if not fp:
                        continue
                    rel = _rel(fp, repo_path)
                    if rel == target_rel:
                        continue
                    prev = best_depth.get(rel)
                    if prev is None or depth < prev:
                        best_depth[rel] = depth
            entries = sorted(best_depth.items(), key=lambda kv: (kv[1], kv[0]))
            total_callers = len(entries)
            shown = entries[: input.max_callers]
            callers_truncated = len(entries) > len(shown)
            for rel, depth in shown:
                caller_files.add(rel)
                callers_by_module.setdefault(_module_of(rel), []).append(
                    {"file": rel, "depth": depth, "direct": depth == 1}
                )

        # ---- diagnostics on this file: cycle + layering ----------------------
        diag_out = await self.handle_list_diagnostics(
            ListDiagnosticsInput(repo_path=repo_path, file_path=target_abs, limit=1000)
        )
        cycle: dict[str, Any] | None = None
        layering: list[dict[str, Any]] = []
        layer_rules = {"deep_import", "layer_violation", "upward_import"}
        for d in diag_out.diagnostics:
            props = d.get("properties", {})
            rule = props.get("rule")
            if rule == "dependency_cycle" and cycle is None:
                ring = props.get("cycle", [])
                cycle = {"scc_size": len(ring), "path": ring}
            elif rule in layer_rules:
                layering.append(
                    {
                        "rule": rule,
                        "message": d.get("message") or props.get("message"),
                        "from": props.get("from"),
                        "to": props.get("to"),
                    }
                )

        # ---- public-surface status ------------------------------------------
        public_surface = await self._briefing_public_surface(
            graph_id, repo_path, target_rel, arch_config
        )

        # ---- hotspot / coupling rank ----------------------------------------
        coupling_rank = await self._briefing_coupling_rank(graph_id, target_rel, repo_path)

        freshness = await self._freshness_envelope(graph_id)

        truncated: dict[str, Any] = {}
        if callers_truncated or depth_bounded:
            note: dict[str, Any] = {}
            if callers_truncated:
                note["shown"] = len(caller_files)
                note["total"] = total_callers
            if depth_bounded:
                # The transitive frontier reached the BFS horizon; callers beyond
                # caller_max_depth are not enumerated. Surfaced, not silent.
                note["max_depth"] = caller_max_depth
            truncated["callers"] = note

        summary = self._render_briefing_markdown(
            target_rel=target_rel,
            callers_by_module=callers_by_module,
            total_callers=total_callers,
            cycle=cycle,
            layering=layering,
            public_surface=public_surface,
            coupling_rank=coupling_rank,
            freshness=freshness,
            truncated=truncated,
        )

        return ChangeBriefingOutput(
            target_file=target_rel,
            target_symbol=input.symbol,
            summary=summary,
            callers_by_module=callers_by_module,
            caller_files=sorted(caller_files),
            cycle=cycle,
            layering=layering,
            public_surface=public_surface,
            coupling_rank=coupling_rank,
            truncated=truncated,
            message=f"Briefing for {target_rel}",
            freshness=freshness,
        )

    async def _briefing_public_surface(
        self,
        graph_id: str,
        repo_path: str,
        target_rel: str,
        arch_config: ArchitectureConfig | None,
    ) -> dict[str, Any]:
        """Whether ``target_rel`` is a declared public surface and who reaches it.

        Reuses :meth:`handle_audit_public_surfaces` verbatim so the surface/deep
        classification is the one SSOT — filtering its per-module report down to
        the target file. Empty when no module declares ``public_surfaces``.
        """
        if arch_config is None:
            return {}
        audit = await self.handle_audit_public_surfaces(
            AuditPublicSurfacesInput(repo_path=repo_path)
        )
        target_mod = arch_config.module_of(target_rel)
        for report in audit.modules:
            if report.get("module") != target_mod:
                continue
            is_surface = target_rel in report.get("public_exports", [])
            deep = [
                c["consumer"]
                for c in report.get("deep_import_consumers", [])
                if c.get("internal") == target_rel
            ]
            via = [
                c["consumer"]
                for c in report.get("via_surface_consumers", [])
                if c.get("surface") == target_rel
            ]
            return {
                "is_surface": is_surface,
                "module": target_mod,
                "deep_import_consumers": sorted(deep),
                "via_surface_consumers": sorted(via),
            }
        return {}

    async def _briefing_coupling_rank(
        self, graph_id: str, target_rel: str, repo_path: str
    ) -> dict[str, Any] | None:
        """File-level coupling rank of ``target_rel``, or None if not in the pool.

        Reuses the same hotspot ranker :meth:`handle_find_hotspots` composes.
        That ranker scores SYMBOL nodes, so a file with many symbols would occupy
        many positions; here the symbol scores are PROJECTED to file level by
        keeping each file's highest-scoring symbol, then files are ranked by that
        projected score. This is a projection of existing scores (no new metric).
        ``ranked_within`` is the size of the file pool the rank is relative to;
        ``pool_capped`` flags that the underlying symbol pool was itself capped so
        the file ranking may be incomplete. A file outside the pool is unranked
        (``None``) rather than given a false rank.
        """
        if self.community_analyzer is None:
            return None
        pool_size = 1000
        try:
            # Fetch one past the pool so an EXACT pool_size result (no overflow)
            # is not misreported as capped: capped iff more than pool_size came
            # back.
            hotspots = await self.community_analyzer.find_hotspots(
                graph_id, top_n=pool_size + 1, metric="coupling"
            )
        except Exception as exc:
            logger.warning("Change-briefing hotspot rank failed: %s", exc)
            return None
        pool_capped = len(hotspots) > pool_size

        # Project symbol scores down to file level: each file keeps its single
        # best (max) symbol score. Then rank files by that projected score. Only
        # the declared pool (first pool_size) is projected — the extra probe entry
        # exists solely to detect capping above and must NOT enter the ranking, or
        # a file present only via the 1001st symbol would get a spurious rank.
        best_by_file: dict[str, float] = {}
        for h in hotspots[:pool_size]:
            d = h.model_dump()
            fp = d.get("file_path") or d.get("properties", {}).get("file_path", "")
            if not fp:
                continue
            rel = _rel(fp, repo_path)
            score = float(d.get("score", 0.0))
            if rel not in best_by_file or score > best_by_file[rel]:
                best_by_file[rel] = score
        if target_rel not in best_by_file:
            return None
        ranked_files = sorted(best_by_file.items(), key=lambda kv: kv[1], reverse=True)
        rank = next(i for i, (rel, _) in enumerate(ranked_files) if rel == target_rel) + 1
        return {
            "rank": rank,
            "ranked_within": len(ranked_files),
            "pool_capped": pool_capped,
            "metric": "coupling",
            "score": best_by_file[target_rel],
        }

    @staticmethod
    def _render_briefing_markdown(
        *,
        target_rel: str,
        callers_by_module: dict[str, list[dict[str, Any]]],
        total_callers: int,
        cycle: dict[str, Any] | None,
        layering: list[dict[str, Any]],
        public_surface: dict[str, Any],
        coupling_rank: dict[str, Any] | None,
        freshness: dict[str, Any] | None,
        truncated: dict[str, Any],
    ) -> str:
        """Render the agent-facing markdown briefing.

        Facts + risk notes, no prescriptions (decision (3)). Names/paths only:
        callers are reported by FILE and MODULE, never by bare graph node id.
        """
        # The summary identifies the target by FILE only. The requested symbol is
        # returned in the structured ``target_symbol`` field, deliberately kept out
        # of the prose so no bare graph id (symbol or caller) ever appears here.
        lines: list[str] = []
        lines.append(f"# Change briefing: `{target_rel}`")

        # Freshness first — a stale briefing is worth flagging up top.
        if freshness is not None:
            stale = freshness.get("stale")
            dirty = freshness.get("dirty_file_count")
            if stale is True:
                lines.append(
                    f"> Risk: index is STALE ({dirty} file(s) changed since last "
                    "index). Findings below may not reflect the working tree."
                )
            elif stale is None:
                lines.append("> Note: index freshness is unknown for this graph.")

        # Callers. "found" rather than "total": the count is what the bounded
        # upstream walk discovered, which may be short of the true total when the
        # depth horizon was hit (see the depth note below).
        if total_callers:
            lines.append(f"\n## Callers ({total_callers} found)")
            for module in sorted(callers_by_module):
                group = callers_by_module[module]
                direct = sum(1 for c in group if c["direct"])
                lines.append(
                    f"- Module `{module}`: {len(group)} caller(s), {direct} direct"
                )
                for c in sorted(group, key=lambda x: (not x["direct"], x["file"])):
                    kind = "direct" if c["direct"] else f"transitive (depth {c['depth']})"
                    lines.append(f"  - `{c['file']}` — {kind}")
            t = truncated.get("callers")
            if t:
                if "total" in t:
                    lines.append(
                        f"- Note: caller list truncated to {t['shown']} of {t['total']}."
                    )
                if "max_depth" in t:
                    lines.append(
                        f"- Note: transitive callers walked to depth {t['max_depth']} "
                        "only; deeper callers are not listed."
                    )
        else:
            lines.append("\n## Callers\n- No callers found in the graph.")

        # Cycle
        if cycle is not None:
            lines.append("\n## Cycle membership")
            lines.append(
                f"- This file is in an import cycle (SCC size {cycle['scc_size']}): "
                + " -> ".join(f"`{p}`" for p in cycle["path"])
            )
            lines.append(
                "- Risk: edits here can ripple across every file in the ring."
            )

        # Layering
        if layering:
            lines.append("\n## Layering findings")
            for finding in layering:
                edge = ""
                if finding.get("from") and finding.get("to"):
                    edge = f" (`{finding['from']}` -> `{finding['to']}`)"
                lines.append(f"- `{finding['rule']}`{edge}: {finding.get('message', '')}")

        # Public surface
        if public_surface:
            lines.append("\n## Public surface")
            if public_surface.get("is_surface"):
                via = public_surface.get("via_surface_consumers", [])
                lines.append(
                    f"- This file IS a declared public surface of module "
                    f"`{public_surface.get('module')}` ({len(via)} consumer(s) via the surface)."
                )
            else:
                deep = public_surface.get("deep_import_consumers", [])
                if deep:
                    lines.append(
                        f"- Internal file of module `{public_surface.get('module')}` "
                        f"deep-imported by {len(deep)} external consumer(s): "
                        + ", ".join(f"`{c}`" for c in deep)
                    )
                else:
                    lines.append(
                        f"- Internal file of module `{public_surface.get('module')}` "
                        "with no external deep-importers."
                    )

        # Coupling rank
        if coupling_rank is not None:
            lines.append("\n## Coupling")
            pool = coupling_rank["ranked_within"]
            qualifier = " (ranked pool capped)" if coupling_rank.get("pool_capped") else ""
            lines.append(
                f"- Coupling hotspot rank {coupling_rank['rank']} of "
                f"{pool} ranked file(s){qualifier}."
            )

        return "\n".join(lines)

    # ==================================================================
    # PLACEMENT + DUPLICATE-CHECK TOOLS (bead code_hygiene_mcp-42i)
    # ==================================================================

    async def handle_suggest_placement(
        self, input: SuggestPlacementInput
    ) -> SuggestPlacementOutput:
        """Recommend where a described new component should live.

        Composes existing internals only (no new analysis engine): the declared
        module map (:class:`ArchitectureConfig`) plus the candidate modules derived
        from the graph's files (organ of each production file). References in
        ``depends_on``/``consumed_by`` are resolved to their owning modules — a
        file path directly, or a symbol name via the graph's symbol resolver.

        DECISION (2): works WITHOUT a `.ariadne/architecture.yml`, but its power
        DEPENDS on one. WITH a declared ``may_depend_on`` map it derives the
        layering VIOLATIONS each placement would create (candidate -> dep must be
        allowed; consumer -> candidate must be allowed) and ranks fewest-violations
        first. WITHOUT config, import DIRECTION is undecidable (module names
        collapse to bare organs), so it does NOT fabricate zero-violation verdicts:
        it ranks by co-location and states plainly that direction was not checked.
        Unresolved references are reported, never silently dropped. Guidance, never
        a verdict.
        """
        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)

        exists = await self._graph_exists(graph_id)
        if not exists:
            return SuggestPlacementOutput(message="Repository has not been indexed yet")

        arch_config = load_architecture_config(Path(repo_path))

        # Candidate module universe: declared modules (if any) UNION the organs of
        # every indexed production file. Peripheral organs (tests/scripts) are not
        # placement targets for production code — including a DECLARED module whose
        # owned paths are all peripheral (e.g. a config module for 'tests/'); adding
        # every configured module unconditionally would re-introduce it as a target.
        file_rows = await self._get_all_files(graph_id)
        rel_files = [_rel(fp, repo_path) for fp in file_rows]
        candidate_modules: set[str] = set()
        for rel in rel_files:
            if is_peripheral_path(rel):
                continue
            candidate_modules.add(self._module_of(rel, arch_config))
        if arch_config is not None:
            for name, spec in arch_config.modules.items():
                # Skip a module all of whose owned path globs are peripheral.
                if spec.paths and all(is_peripheral_path(p) for p in spec.paths):
                    continue
                candidate_modules.add(name)
        candidate_modules.discard("")

        if not candidate_modules:
            return SuggestPlacementOutput(
                config_used=arch_config is not None,
                message="No candidate modules found (repository has no production files).",
            )

        dep_modules, dep_problems = await self._modules_of_refs(
            graph_id, input.depends_on, repo_path, rel_files, arch_config
        )
        # Consumers: exclude peripheral-PATH consumers (co-located tests etc.) at
        # resolution, before the path collapses to a possibly-non-peripheral module.
        consumer_modules, con_problems = await self._modules_of_refs(
            graph_id, input.consumed_by, repo_path, rel_files, arch_config,
            exclude_peripheral=True,
        )
        unresolved = dep_problems + con_problems

        # Placement ranks by dependency DIRECTION + co-location — it needs at least
        # one usable dependency or consumer to say anything meaningful. With none
        # (description-only, or every reference unresolved), every candidate scores
        # equally and any order would be arbitrary. Say so rather than present an
        # alphabetical list as a recommendation. (The description is not a placement
        # signal — that is find_equivalent's job.)
        if not dep_modules and not consumer_modules:
            msg = (
                "Placement needs at least one resolvable dependency (depends_on) or "
                "consumer (consumed_by) to rank modules — a description alone cannot "
                "determine placement. Supply the files/symbols the component would "
                "import or that would import it."
            )
            if unresolved:
                msg += (
                    f" (all {len(unresolved)} supplied reference(s) were unresolved or "
                    f"ambiguous: {', '.join(unresolved)})"
                )
            return SuggestPlacementOutput(
                candidates=[],
                config_used=arch_config is not None,
                message=msg,
                freshness=await self._freshness_envelope(graph_id),
            )

        # Direction (layering) violations are only decidable with a declared
        # ``may_depend_on`` map. Without config, module names collapse to bare
        # organs and import DIRECTION is genuinely unknown — so we do NOT fabricate
        # zero-violation verdicts; we rank by co-location and say direction was not
        # checked. (Finding: the old heuristic silently reported zero violations.)
        check_direction = arch_config is not None

        candidates: list[PlacementCandidate] = []
        candidate_unknowns: dict[str, int] = {}
        for module in sorted(candidate_modules):
            violations: list[dict[str, Any]] = []
            unknowns = 0  # pairs whose direction could not be checked (undeclared endpoint)
            # check_direction is True iff arch_config is not None; bind a
            # non-optional local so the type is narrowed for _placement_check.
            if arch_config is not None and check_direction:
                # The component sits in `module` and imports each dependency: module -> dep.
                for dep_mod in sorted(dep_modules):
                    if dep_mod == module:
                        continue
                    verdict = self._placement_check(module, dep_mod, arch_config)
                    if verdict == "violation":
                        violations.append(
                            {"direction": "depends_on", "from": module, "to": dep_mod,
                             "rule": "config"}
                        )
                    elif verdict == "unknown":
                        unknowns += 1
                # Each consumer imports the component: consumer -> module.
                # Peripheral consumers were already dropped by _modules_of_refs on
                # the actual PATH (exclude_peripheral=True); we must NOT re-test the
                # MODULE NAME here — a production module legitimately named 'tests'
                # or 'scripts' would otherwise have its real constraint discarded.
                for con_mod in sorted(consumer_modules):
                    if con_mod == module:
                        continue
                    verdict = self._placement_check(con_mod, module, arch_config)
                    if verdict == "violation":
                        violations.append(
                            {"direction": "consumed_by", "from": con_mod, "to": module,
                             "rule": "config"}
                        )
                    elif verdict == "unknown":
                        unknowns += 1

            candidate_unknowns[module] = unknowns
            # Co-location bonus: sharing a module with a stated dependency or
            # consumer is the cheapest, most cohesive placement. A violation costs
            # 1.0; an UNKNOWN pair costs a smaller 0.4 — it is NOT verified-safe, so
            # it must rank below a fully-checked zero-violation candidate but above
            # a known violation.
            colocated = len((dep_modules | consumer_modules) & {module})
            score = -float(len(violations)) - 0.4 * unknowns + 0.25 * colocated
            rationale = self._placement_rationale(
                module, dep_modules, consumer_modules, violations, check_direction, unknowns
            )
            candidates.append(
                PlacementCandidate(
                    module=module, violations=violations, rationale=rationale, score=score
                )
            )

        candidates.sort(
            key=lambda c: (-c.score, len(c.violations), candidate_unknowns.get(c.module, 0), c.module)
        )
        candidates = candidates[: input.limit]

        parts = [f"Ranked {len(candidates)} candidate module(s)"]
        if not check_direction:
            parts.append(
                "no .ariadne/architecture.yml: ranked by co-location only — import "
                "direction (layering) violations were NOT checked (add a may_depend_on map to enable)"
            )
        if unresolved:
            parts.append(
                f"{len(unresolved)} reference(s) could not be used and were excluded "
                f"(unresolved or ambiguous — supply a file path or qualified symbol): "
                f"{', '.join(unresolved)}"
            )
        return SuggestPlacementOutput(
            candidates=candidates,
            config_used=check_direction,
            message="; ".join(parts),
            freshness=await self._freshness_envelope(graph_id),
        )

    def _module_of(self, rel: str, arch_config: ArchitectureConfig | None) -> str:
        """Module bucket for a repo-relative file: the declared module when a
        config matches, else the top-level organ (its directory 'organ'). A
        root-level file (no directory) buckets to a stable ``(root)`` module —
        never its own filename — so a flat repo yields one module, not one per
        file (mirrors change_briefing's ``_module_of``)."""
        if arch_config is not None:
            mod = arch_config.module_of(rel)
            if mod:
                return mod
        organ = _organ(_dir_of(rel))
        return organ if organ else "(root)"

    async def _modules_of_refs(
        self,
        graph_id: str,
        refs: list[str],
        repo_path: str,
        rel_files: list[str],
        arch_config: ArchitectureConfig | None,
        *,
        exclude_peripheral: bool = False,
    ) -> tuple[set[str], list[str]]:
        """Resolve dependency/consumer references (file paths OR symbol names) to
        their owning modules. Returns (modules, problems) — a ref that resolves to
        no file/symbol, or to an AMBIGUOUS symbol defined in several files, is
        reported in ``problems`` (never silently dropped, never silently guessed),
        so the caller can surface that placement was ranked on partial inputs.

        ``exclude_peripheral`` (used for CONSUMERS): a reference whose resolved
        PATH is peripheral (a co-located test like ``server/foo.test.ts`` or a
        ``tests/`` file) is exempt from layering and dropped here — its
        peripheral status must be judged on the path, BEFORE the path collapses to
        a (possibly non-peripheral) module name like ``server`` or ``qa``. Not a
        'problem' — it is a valid consumer that simply cannot cause a violation."""
        file_set = set(rel_files)
        out: set[str] = set()
        problems: list[str] = []
        for ref in refs:
            rel = self._ref_to_rel(ref, repo_path, file_set)
            if rel is None:
                # Not a file path — try resolving it as a symbol name to its file.
                rel, note = await self._symbol_ref_to_rel(graph_id, ref, repo_path)
                if rel is None:
                    problems.append(f"{ref} ({note})" if note else ref)
                    continue
            if exclude_peripheral and is_peripheral_path(rel):
                continue  # peripheral consumer — exempt from layering, judged by path
            mod = self._module_of(rel, arch_config)
            if mod:
                out.add(mod)
            else:
                problems.append(ref)
        return out, problems

    @staticmethod
    def _ref_to_rel(ref: str, repo_path: str, file_set: set[str]) -> str | None:
        """Normalise a FILE-path reference to a repo-relative path THAT IS IN THE
        INDEX. Only indexed paths resolve — a path (absolute or relative) that is
        not an indexed file returns None so the caller falls through to symbol
        resolution and, failing that, reports it unresolved. This prevents a typo
        like ``shraed/contracts.py`` from inventing a bogus ``shraed`` module."""
        if not ref:
            return None
        # Normalise BOTH absolute and relative refs against the repo root so that
        # './shared/x.py' or 'server/../shared/x.py' resolve to the same
        # repo-relative key the index stores. Falls back to the raw ref only when
        # it is already a clean relative path (keeps the unique-suffix match below
        # working for bare 'x.py' inputs).
        p = Path(ref)
        abs_path = p if p.is_absolute() else Path(repo_path) / p
        candidate = _rel(str(abs_path.resolve()), repo_path)
        if candidate in file_set:
            return candidate
        if ref in file_set:
            return ref
        # unique suffix match (e.g. 'contracts.py' -> 'shared/contracts.py')
        matches = [f for f in file_set if f == candidate or f.endswith("/" + candidate)]
        if len(matches) == 1:
            return matches[0]
        return None

    async def _symbol_ref_to_rel(
        self, graph_id: str, symbol: str, repo_path: str
    ) -> tuple[str | None, str | None]:
        """Resolve a bare symbol name to the repo-relative file that DEFINES it.

        Returns ``(rel, note)``: ``rel`` is the file when the symbol maps to
        EXACTLY ONE definition file; when several files define a symbol of that
        name it is AMBIGUOUS — we do NOT silently pick one (that would attribute a
        placement constraint to the wrong module), returning ``(None, "ambiguous:
        defined in N files")`` so the caller reports it and asks for a qualified
        symbol or file path. ``(None, "not found")`` when nothing matches."""
        if not symbol:
            return None, None
        # A fully-qualified symbol (a node id like 'server.util.helper') is
        # UNAMBIGUOUS — resolve it directly by id first, so the disambiguation we
        # advise ("supply a qualified symbol") actually works.
        try:
            id_rows = await self.graph_store.query(
                graph_id, "node_by_id", params={"node_id": symbol}
            )
        except Exception as exc:
            logger.warning("Placement symbol id lookup failed for %r: %s", symbol, exc)
            id_rows = []
        for row in id_rows:
            node = row.get("n", row)
            # Only accept a DEFINITION node — a qualified id that matches a
            # CodeImport/usage/diagnostic node must NOT be resolved to that usage's
            # file (that would attribute the constraint to the wrong module), same
            # as the name lookup below filters by definition labels.
            if not (set(_record_labels(node)) & _DEFINITION_LABELS):
                continue
            fp = _record_props(node).get("file_path")
            if fp:
                return _rel(fp, repo_path), None

        # Distinct DEFINITION files for this exact name (functions/classes/methods
        # /… — not imports/usages, which would over-count files that merely mention
        # it). SQLite and Neo4j ``node_by_name`` return EVERY match, so use it — a
        # cheap indexed lookup. ONLY MemoryGraphStore returns just the first match
        # (hiding ambiguity), so fall back to a full scan there (its graphs are
        # small). We must NOT silently pick one file when several define the name.
        if isinstance(self.graph_store, MemoryGraphStore):
            try:
                rows = await self.graph_store.query(graph_id, "nodes")
            except Exception as exc:
                logger.warning("Placement symbol resolution failed for %r: %s", symbol, exc)
                return None, "resolution error"
        else:
            try:
                rows = await self.graph_store.query(
                    graph_id, "node_by_name", params={"name": symbol}
                )
            except Exception as exc:
                logger.warning("Placement symbol resolution failed for %r: %s", symbol, exc)
                return None, "resolution error"

        def_files: set[str] = set()
        for row in rows:
            node = row.get("n", row)
            labels = set(_record_labels(node))
            if not (labels & _DEFINITION_LABELS):
                continue
            props = _record_props(node)
            if str(props.get("name", "")) != symbol:
                continue
            fp = props.get("file_path")
            if fp:
                def_files.add(_rel(fp, repo_path))

        if not def_files:
            return None, "not found"
        if len(def_files) > 1:
            return None, f"ambiguous: defined in {len(def_files)} files"
        return next(iter(def_files)), None

    def _placement_check(
        self, src_mod: str, dst_mod: str, arch_config: ArchitectureConfig
    ) -> str:
        """Tristate direction check for ``src_mod`` -> ``dst_mod`` against the
        declared module map: ``"allowed"``, ``"violation"``, or ``"unknown"``.

        A config may cover only PART of a repo: files outside every declared
        ``paths`` glob fall back to a directory-organ bucket that is NOT a declared
        module. For such an undeclared endpoint the config has no rule, so the
        direction is genuinely UNKNOWN — distinct from an explicitly ALLOWED pair.
        We must not collapse unknown into allowed (that would rank an unchecked
        candidate as verified-safe) nor into violation (that would invent a
        finding). Only when BOTH endpoints are declared do we return the map's
        allowed/violation verdict."""
        if src_mod == dst_mod:
            return "allowed"
        if src_mod not in arch_config.modules or dst_mod not in arch_config.modules:
            return "unknown"
        return "allowed" if arch_config.allows(src_mod, dst_mod) else "violation"

    @staticmethod
    def _placement_rationale(
        module: str,
        dep_modules: set[str],
        consumer_modules: set[str],
        violations: list[dict[str, Any]],
        check_direction: bool,
        unknowns: int,
    ) -> str:
        bits: list[str] = []
        if module in dep_modules:
            bits.append("co-located with a stated dependency")
        if module in consumer_modules:
            bits.append("co-located with a stated consumer")
        if not check_direction:
            bits.append("layering direction not checked (no architecture.yml)")
        else:
            if violations:
                bits.append(f"would introduce {len(violations)} layering violation(s)")
            if unknowns:
                # NOT verified-safe: some relationship touches an undeclared module.
                bits.append(
                    f"{unknowns} relationship(s) not checked (endpoint not a declared module)"
                )
            if not violations and not unknowns:
                bits.append("introduces no layering violations")
        return "; ".join(bits) if bits else "candidate module"

    async def _fetch_node_record(self, graph_id: str, node_id: str) -> dict[str, Any] | None:
        """Hydrate a node record by id (semantic hits carry only node_id+score)."""
        if not node_id:
            return None
        try:
            rows = await self.graph_store.query(
                graph_id, "node_by_id", params={"node_id": node_id}
            )
        except Exception as exc:
            logger.warning("find_equivalent node hydration failed for %s: %s", node_id, exc)
            return None
        if rows:
            record = rows[0].get("n", rows[0])
            return record if isinstance(record, dict) else None
        return None

    async def _hydrate_nodes_batch(
        self, graph_id: str, node_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch many node records at once -> {node_id: record}. Replaces an N+1 of
        per-hit ``node_by_id`` lookups (find_equivalent hydrates up to a whole-graph
        pool). SQLite runs a single chunked ``id IN (...)`` query; other stores fall
        back to one ``nodes`` scan filtered in Python (still one round trip)."""
        wanted = {nid for nid in node_ids if nid}
        if not wanted:
            return {}
        out: dict[str, dict[str, Any]] = {}
        if isinstance(self.graph_store, SQLiteGraphStore):
            id_list = list(wanted)
            db = await self.graph_store._connect()
            try:
                # Chunk to stay under SQLite's variable limit (999).
                for i in range(0, len(id_list), 800):
                    chunk = id_list[i : i + 800]
                    placeholders = ",".join("?" * len(chunk))
                    cur = await db.execute(
                        f"SELECT id, labels, properties FROM nodes "
                        f"WHERE graph_id = ? AND id IN ({placeholders})",
                        (graph_id, *chunk),
                    )
                    for row in await cur.fetchall():
                        out[row["id"]] = {
                            "id": row["id"],
                            "labels": json.loads(row["labels"]) if row["labels"] else [],
                            "properties": json.loads(row["properties"]) if row["properties"] else {},
                        }
            finally:
                await db.close()
            return out
        # Non-SQLite: one scan, filter to wanted ids. Normalise each record to the
        # nested {id,labels,properties} shape so downstream reads are backend-
        # agnostic (Neo4j flattens properties to the top level).
        with contextlib.suppress(Exception):
            for row in await self.graph_store.query(graph_id, "nodes"):
                rec = row.get("n", row)
                nid = str(rec.get("id", ""))
                if nid in wanted:
                    out[nid] = {
                        "id": nid,
                        "labels": _record_labels(rec),
                        "properties": _record_props(rec),
                    }
        return out

    async def _get_all_files(self, graph_id: str) -> list[str]:
        """Absolute file paths of every CodeFile in the graph.

        Sourced through the backend-agnostic ``_read_code_files`` helper (bead
        420's SSOT for indexed-file rows) so this works on any GraphStore, not
        just SQLite — the same (id, file_path) rows the old ``_FILE_SQL`` query
        produced, via the store protocol.
        """
        rows = await _read_code_files(self.graph_store, graph_id)
        return sorted({file_path for _nid, file_path in rows})

    async def handle_find_equivalent(
        self, input: FindEquivalentInput
    ) -> FindEquivalentOutput:
        """Surface existing symbols that may already implement a described component.

        Reuses the semantic search path (no second embedding pipeline): embeds the
        description (+ optional signature) and reranks the hits with a same-module
        bonus and rationale strings. Explicitly framed as needs-human-judgement —
        no auto-verdict, no ``is_duplicate`` flag.

        DEGRADED MODE: semantic search is off unless the semantic extra is
        installed. When it is missing this returns an EXPLICIT message (with the
        install hint) and ``semantic_available=False`` — never empty silence, so a
        consumer knows retrieval quality is degraded rather than seeing "no
        duplicates found".
        """
        # Loud degraded-mode announcement when semantic retrieval is unavailable.
        # Readiness is checked via _semantic_ready() (a lazy LocalEmbeddingProvider
        # needs the runtime extra; a custom provider is trusted) — provider!=None
        # is not the same as ready.
        if not self._semantic_ready():
            return FindEquivalentOutput(
                candidates=[],
                semantic_available=False,
                message=(
                    "DEGRADED: duplicate detection needs semantic (vector) search, which "
                    "requires the semantic extra. Without it this tool cannot find "
                    'behavioural duplicates. Install with: pip install -e ".[semantic]" '
                    "and re-index."
                ),
            )

        repo_path = str(Path(input.repo_path).resolve())
        graph_id = self._get_graph_id(repo_path)
        exists = await self._graph_exists(graph_id)
        if not exists:
            return FindEquivalentOutput(
                candidates=[], semantic_available=True,
                message="Repository has not been indexed yet",
            )

        query = input.description.strip()
        if input.signature:
            query = f"{query} {input.signature}".strip()
        if not query:
            return FindEquivalentOutput(
                candidates=[], semantic_available=True,
                message="Provide a description and/or signature to search for equivalents.",
            )

        # The semantic extra is installed but this graph may still have NO vectors
        # (indexed before the extra was added, or embedding previously failed), OR
        # STALE vectors from a different model/text-schema. Searching stale vectors
        # of a different-dimension model raises a shape mismatch in the brute-force
        # fallback, so we refuse and give the same force-rebuild guidance rather
        # than crash or silently report 0 matches.
        emb_status = await self._embedding_status(graph_id)
        if emb_status.get("supported"):
            if emb_status.get("embedded_count", 0) == 0:
                return FindEquivalentOutput(
                    candidates=[], semantic_available=True,
                    message=(
                        "DEGRADED: this repository has no stored embeddings, so duplicate "
                        "detection cannot run. A normal re-index will NOT backfill them (no files "
                        "changed) — run code_graph_index with force_rebuild=True (semantic extra "
                        "installed) to generate vectors for the whole graph."
                    ),
                    freshness=await self._freshness_envelope(graph_id),
                )
            if emb_status.get("stale"):
                return FindEquivalentOutput(
                    candidates=[], semantic_available=True,
                    message=(
                        "DEGRADED: stored embeddings are STALE (built by a different model or an "
                        "older text schema than the current provider), so duplicate detection "
                        "cannot safely search them — a model/dimension change would mismatch. "
                        "Run code_graph_index with force_rebuild=True to re-embed the whole graph."
                    ),
                    freshness=await self._freshness_envelope(graph_id),
                )

        # Default to real definitions (functions/methods/classes) — the tool
        # promises existing-SYMBOL candidates, so files/imports/modules/diagnostics
        # must not occupy the window and hide the relevant symbol. An explicit
        # ``types`` overrides this default.
        if input.types:
            type_set = {t.lower() for t in input.types}
        else:
            type_set = {label.lower() for label in _DEFINITION_LABELS}

        # The searcher has no label filter and matching definitions can rank below
        # non-matching nodes (files/imports/…), so a small fixed window would starve
        # a typed search. Fetch a pool sized to the WHOLE graph (bounded by its node
        # count) so every type-matching definition is in reach — the store returns
        # top-N by similarity, so this is a strict superset of any smaller window and
        # the post-fetch filter cannot then drop a match that only exists past the
        # cap. A ceiling keeps a pathological giant graph bounded (reported below).
        # _semantic_ready() above guarantees a searcher; bind a non-optional local
        # so the type is narrowed (mypy does not track the helper's postcondition).
        searcher = self.searcher
        assert searcher is not None
        node_count = int(emb_status.get("eligible_count") or 0)
        raw_limit = min(node_count or _EQUIVALENT_POOL_CEILING, _EQUIVALENT_POOL_CEILING)
        try:
            hits = await searcher.search(
                graph_id, query, limit=raw_limit, search_type="semantic"
            )
        except Exception as exc:
            logger.warning("find_equivalent semantic search failed for %s: %s", graph_id, exc)
            return FindEquivalentOutput(
                candidates=[], semantic_available=True,
                message=f"Semantic search failed: {exc}",
            )

        # On a backend where we cannot read embedding provenance (non-SQLite),
        # zero hits is genuinely AMBIGUOUS: it may mean "no similar symbol" OR "no
        # vectors indexed" (indexed before the extra; a normal re-index won't
        # backfill). Do not present that as a confident all-clear.
        if not hits and not emb_status.get("supported"):
            return FindEquivalentOutput(
                candidates=[], semantic_available=True,
                message=(
                    "DEGRADED: no semantic hits, and this backend cannot report whether "
                    "embeddings exist — a missing/never-generated vector index looks "
                    "identical to 'no duplicate' here. If the repo was indexed before the "
                    "semantic extra, re-index with force_rebuild=True before trusting an "
                    "empty result."
                ),
                freshness=await self._freshness_envelope(graph_id),
            )

        # The semantic searcher returns node_id + score only, so we must hydrate
        # labels/props. Doing that per hit is an N+1 (up to the whole pool); fetch
        # the needed records in ONE batch keyed by id instead.
        need_ids = [
            str(h.get("node_id", ""))
            for h in hits
            if not isinstance(h.get("node"), dict) or not h.get("node")
        ]
        hydrated = await self._hydrate_nodes_batch(graph_id, need_ids)

        candidates: list[EquivalentCandidate] = []
        for hit in hits:
            node = hit.get("node")
            if not isinstance(node, dict) or not node:
                node = hydrated.get(str(hit.get("node_id", "")))
            if not isinstance(node, dict) or not node:
                continue
            # Backend-agnostic reads (Neo4j flattens properties to the top level).
            labels = [str(x).lower() for x in _record_labels(node)]
            if not (type_set & set(labels)):
                continue
            props = _record_props(node)
            name = str(props.get("name", "")) or str(node.get("id", ""))
            file_abs = str(props.get("file_path", ""))
            file_rel = _rel(file_abs, repo_path) if file_abs else ""
            module = str(props.get("module", "")) or (
                _organ(_dir_of(file_rel)) if file_rel else ""
            )
            similarity = float(hit.get("score", 0.0))
            candidates.append(
                EquivalentCandidate(
                    name=name,
                    node_id=str(hit.get("node_id", node.get("id", ""))),
                    module=module,
                    file=file_rel,
                    similarity=similarity,
                    score=similarity,  # reranked below
                    rationale="",
                )
            )

        # Same-module bonus: if a signature hints a module/file, favour hits that
        # already live there (a duplicate is most likely near its neighbours). Match
        # on TOKEN BOUNDARIES, and ONLY when the hint is an actual QUALIFIED module
        # or path (it contains a '.' or '/') — a bare signature word like the
        # parameter in 'def transform(api)' must NOT earn module 'api' the bonus.
        # A qualified hint ('shared.datetime' / 'shared/datetime.ts') tokenizes to
        # {'shared','datetime',...}; award the bonus when ALL of the module's path
        # tokens appear in the hint's tokens.
        def _tokens(s: str) -> set[str]:
            return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if t}

        raw_hint = input.signature or ""
        # Only a hint that actually names a qualified module/path can locate a module.
        hint_is_qualified = ("." in raw_hint) or ("/" in raw_hint)
        hint_tokens = _tokens(raw_hint) if hint_is_qualified else set()
        for c in candidates:
            bonus = 0.0
            reasons = [f"semantic similarity {c.similarity:.3f} to the description"]
            mod_tokens = _tokens(c.module) if c.module else set()
            # Drop bare file extensions so 'shared/datetime.ts' matches a hint that
            # only names 'shared' and 'datetime'.
            mod_tokens -= {"ts", "tsx", "js", "jsx", "py", "mjs", "cjs"}
            if hint_is_qualified and mod_tokens and mod_tokens <= hint_tokens:
                bonus += 0.1
                reasons.append(f"same module as the hint ({c.module})")
            c.score = c.similarity + bonus
            c.rationale = "; ".join(reasons)

        candidates.sort(key=lambda c: c.score, reverse=True)
        candidates = candidates[: input.limit]

        msg = (
            f"{len(candidates)} candidate(s) that MIGHT already do this — "
            "similarity hits only; a human must judge true duplication."
        )
        # Partial-index caveat: after enabling semantic on an already-indexed repo,
        # an incremental index embeds only changed files, so most nodes have no
        # vector and unembedded files can't surface. Warn (results are real but
        # incomplete) rather than present them as an exhaustive search.
        if emb_status.get("supported") and not emb_status.get("coverage_complete"):
            cov = emb_status.get("coverage", 0.0)
            msg += (
                f" NOTE: only {cov:.0%} of graph nodes are embedded — this index is "
                "PARTIAL (likely embedded incrementally after enabling semantic), so "
                "duplicates in unembedded files are missed. Run code_graph_index with "
                "force_rebuild=True to embed the whole graph."
            )
        # Provenance caveat: on a backend where the embedding tag is not readable
        # (non-SQLite), we cannot verify the stored vectors match the current
        # model/schema — the SQLite path refuses stale vectors, but here we can
        # only WARN. Surface it rather than present the results as fully trusted.
        if not emb_status.get("supported"):
            msg += (
                " NOTE: embedding provenance is unreadable on this backend, so these "
                "vectors could be stale (a model/schema change would degrade or "
                "invalidate similarity). Re-index with force_rebuild=True if the "
                "embedding model changed."
            )
        # No silent caps: we fetch a whole-graph pool, so a match is missed only on
        # a pathologically large graph where the pool hit the hard _POOL_CEILING AND
        # the store returned a full page (more nodes exist beyond it). Warn only then.
        if raw_limit >= _EQUIVALENT_POOL_CEILING and len(hits) >= raw_limit:
            msg += (
                f" NOTE: this graph exceeds the {_EQUIVALENT_POOL_CEILING}-hit semantic pool "
                "ceiling, so a type-matching symbol ranked below it may be missing — "
                "narrow the description to widen effective coverage."
            )
        return FindEquivalentOutput(
            candidates=candidates,
            needs_human_judgement=True,
            semantic_available=True,
            message=msg,
            freshness=await self._freshness_envelope(graph_id),
        )
