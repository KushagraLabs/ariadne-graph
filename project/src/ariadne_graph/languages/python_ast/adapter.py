"""Python language adapter.

Implements the :class:`LanguageAdapter` protocol for Python source files using
the standard library ``ast`` module.

When ``scip-python`` is available, it is used as a *refinement* layer over the
ast extractor: the ast extractor still produces every node and most edges, and
SCIP rewrites the (otherwise fuzzy, bare-name) ``CALLS`` edge targets to the
exact node id of the resolved definition — correct even when two files define
the same name. When ``scip-python`` is unavailable, extraction falls back to the
ast extractor unchanged, preserving the existing bare-name ``CALLS`` behavior.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import xxhash

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.discovery import FileDiscovery
from ariadne_graph.core.models import CodeGraphDelta, CodeNode
from ariadne_graph.languages.base import ExtractionContext

from .extractor import PythonFactExtractor
from .scip_indexer import ScipPythonIndexer

if TYPE_CHECKING:
    # Imported lazily at runtime: it depends on protobuf (the optional
    # ``typescript`` extra). A base install without protobuf must still import
    # this adapter and fall back to the ast extractor.
    from .scip_resolver import ScipPythonResolver

logger = logging.getLogger(__name__)

# Labels whose nodes represent a definition that a CALLS edge may target.
_DEFINITION_LABELS = frozenset(
    {"CodeFunction", "CodeMethod", "CodeClass", "CodeVariable", "CodeAttribute"}
)


class PythonLanguageAdapter:
    """Adapter for extracting graph facts from Python ``.py`` files.

    This class conforms to the :class:`LanguageAdapter` protocol and is
    intended to be used by the indexing pipeline::

        adapter = PythonLanguageAdapter()
        paths = adapter.discover_files(repo_root, config)
        for path in paths:
            delta = adapter.extract_file(path, context)
            store.apply_delta(delta)
    """

    language: str = "python"
    parser_version: str = f"ast_{sys.version_info.major}.{sys.version_info.minor}+"
    extensions: tuple[str, ...] = (".py",)

    def __init__(self) -> None:
        # Lazily-built SCIP resolution state, keyed by resolved repo root so a
        # single adapter instance can serve multiple projects.
        self._resolvers: dict[Path, ScipPythonResolver | None] = {}
        # repo_root -> {(relative_path, def_line_1based): node_id}
        self._def_node_index: dict[Path, dict[tuple[str, int], str]] = {}

    def discover_files(self, root: Path, config: AnalyzerConfig) -> list[Path]:
        """Find all Python source files under *root* respecting *config*.

        Args:
            root: Repository root directory.
            config: Analyzer configuration with ignore patterns and limits.

        Returns:
            Sorted list of ``.py`` file paths.
        """
        discovery = FileDiscovery(config)
        return discovery.discover(self.extensions)

    def extract_file(self, path: Path, context: ExtractionContext) -> CodeGraphDelta:
        """Parse a single Python file and extract graph facts.

        The file content is hashed with XXH3 for incremental sync. On parse
        errors an empty delta is returned with the error description stored in
        the *parser_version* field. When ``scip-python`` is available, fuzzy
        ``CALLS`` targets are refined to resolved node ids.

        Args:
            path: Absolute path to the ``.py`` file.
            context: Extraction context carrying *graph_id*, *repo_root*, and
                optional *source_commit*.

        Returns:
            A :class:`CodeGraphDelta` containing all nodes and edges found in
            the file.
        """
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return CodeGraphDelta(
                graph_id=context.graph_id,
                file_path=str(path),
                nodes=[],
                edges=[],
                content_hash="",
                parser_version=f"error:read:{exc}",
            )

        content_hash = xxhash.xxh3_64_hexdigest(content)

        extractor = PythonFactExtractor(
            source=content,
            file_path=path,
            repo_root=context.repo_root,
            graph_id=context.graph_id,
            parser_version=self.parser_version,
            source_commit=context.source_commit,
        )
        delta = extractor.extract()
        delta.content_hash = content_hash

        # SCIP refinement: rewrite fuzzy bare-name CALLS targets to real node ids.
        self._refine_calls(delta, path, context)

        # Attach source_commit to every node and edge property bag
        if context.source_commit:
            for node in delta.nodes:
                node.properties.setdefault("source_commit", context.source_commit)
            for edge in delta.edges:
                edge.properties.setdefault("source_commit", context.source_commit)

        # Tag owner file on edges for later deletion queries
        for edge in delta.edges:
            edge.properties.setdefault("owner_file_path", str(path))

        return delta

    async def prepare_project(
        self,
        context: ExtractionContext,
        config: AnalyzerConfig,
        all_files: list[Path],
        changed_files: list[Path],
        graph_store: Any,
    ) -> None:
        """Pre-warm SCIP-Python resolution for the project, if available.

        Building it here (with *config*) lets the indexer honour configuration
        and fingerprinting. :meth:`extract_file` still builds resolution lazily
        when ``prepare_project`` was not called (e.g. single-file extraction).
        """
        if context.repo_root is None:
            return
        # Force a rebuild so an incremental re-sync re-indexes changed sources
        # instead of serving a previously-cached (now stale) resolver.
        self._ensure_resolver(
            context.repo_root, context.graph_id, config, all_files, force=True
        )

    # ------------------------------------------------------------------
    # SCIP refinement
    # ------------------------------------------------------------------

    def _refine_calls(
        self, delta: CodeGraphDelta, path: Path, context: ExtractionContext
    ) -> None:
        """Rewrite fuzzy ``CALLS`` targets in *delta* using SCIP resolution.

        Edges whose call site cannot be resolved keep their bare-name target
        (graceful degradation). Index this file's own definition nodes so a
        call resolving within the same file works without a project scan.
        """
        if context.repo_root is None:
            return

        repo_root = context.repo_root.resolve()
        resolver = self._ensure_resolver(repo_root, context.graph_id, None, None)
        if resolver is None:
            return

        # Make this file's definition nodes resolvable immediately.
        node_index = self._def_node_index.setdefault(repo_root, {})
        try:
            rel_path = str(path.resolve().relative_to(repo_root))
        except ValueError:
            return
        for node in delta.nodes:
            self._register_def_node(node_index, rel_path, node)

        for edge in delta.edges:
            if edge.rel_type != "CALLS":
                continue
            line = edge.properties.get("callee_line")
            col = edge.properties.get("callee_col")
            if line is None or col is None:
                continue
            symbol = resolver.resolve_call(rel_path, int(line), int(col))
            if symbol is None:
                continue
            node_id = self._node_id_for_symbol(repo_root, resolver, symbol)
            if node_id is not None:
                edge.target = node_id
                edge.properties["resolved_by"] = "scip-python"
                edge.properties["scip_symbol"] = symbol

    def _node_id_for_symbol(
        self, repo_root: Path, resolver: ScipPythonResolver, symbol: str
    ) -> str | None:
        """Map a resolved SCIP symbol to an Ariadne node id, or ``None``."""
        location = resolver.definition_location(symbol)
        if location is None:
            return None
        rel_path, def_line_0based, _def_col = location
        node_index = self._def_node_index.get(repo_root, {})
        # SCIP definition occurrences are 0-based; ast node line_start is 1-based.
        node_id = node_index.get((rel_path, def_line_0based + 1))
        if node_id is not None:
            return node_id
        # The definition lives in another file not yet extracted: extract it.
        self._index_file_definitions(repo_root, rel_path)
        return self._def_node_index.get(repo_root, {}).get(
            (rel_path, def_line_0based + 1)
        )

    @staticmethod
    def _register_def_node(
        node_index: dict[tuple[str, int], str], rel_path: str, node: CodeNode
    ) -> None:
        if not (_DEFINITION_LABELS & set(node.labels)):
            return
        line_start = node.properties.get("line_start")
        if line_start is None:
            return
        node_index.setdefault((rel_path, int(line_start)), node.id)

    def _index_file_definitions(self, repo_root: Path, rel_path: str) -> None:
        """Extract a sibling file's definition nodes into the def-node index."""
        node_index = self._def_node_index.setdefault(repo_root, {})
        abs_path = repo_root / rel_path
        try:
            content = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        extractor = PythonFactExtractor(
            source=content,
            file_path=abs_path,
            repo_root=repo_root,
            graph_id="",
            parser_version=self.parser_version,
        )
        sibling = extractor.extract()
        for node in sibling.nodes:
            self._register_def_node(node_index, rel_path, node)

    def _ensure_resolver(
        self,
        repo_root: Path,
        graph_id: str,
        config: AnalyzerConfig | None,
        all_files: list[Path] | None,
        force: bool = False,
    ) -> ScipPythonResolver | None:
        """Return a cached resolver for *repo_root*, building it on first use.

        A cached ``None`` means SCIP is unavailable/disabled for this project;
        callers then fall back to the ast extractor's bare-name behavior. When
        *force* is set, any cached resolver and definition index are discarded
        and the index is rebuilt (used by ``prepare_project`` so incremental
        re-syncs pick up source changes rather than serving a stale index).
        """
        repo_root = repo_root.resolve()
        if force:
            self._resolvers.pop(repo_root, None)
            self._def_node_index.pop(repo_root, None)
        elif repo_root in self._resolvers:
            return self._resolvers[repo_root]

        # The resolver depends on protobuf (optional ``typescript`` extra). If
        # it is absent, SCIP is unavailable and we fall back to the ast path.
        try:
            from .scip_resolver import ScipPythonResolver
        except ImportError as exc:
            logger.info("SCIP-Python resolution unavailable (%s); using ast only", exc)
            self._resolvers[repo_root] = None
            return None

        cfg = config or AnalyzerConfig(repo_root=repo_root)
        indexer = ScipPythonIndexer(repo_root, cfg)
        if all_files is None:
            all_files = sorted(repo_root.rglob("*.py"))

        try:
            index_path = indexer.ensure_index(all_files, graph_id, force=force)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("SCIP-Python indexing failed for %s: %s", repo_root, exc)
            self._resolvers[repo_root] = None
            return None

        if index_path is None:
            self._resolvers[repo_root] = None
            return None

        try:
            resolver = ScipPythonResolver.from_index(index_path)
        except Exception as exc:
            logger.warning("Failed to parse SCIP-Python index %s: %s", index_path, exc)
            self._resolvers[repo_root] = None
            return None

        self._resolvers[repo_root] = resolver
        return resolver
