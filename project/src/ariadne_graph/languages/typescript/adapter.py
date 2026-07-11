"""TypeScript/TSX language adapter.

Implements the :class:`LanguageAdapter` protocol for TypeScript and TSX source
files. When ``scip-typescript`` and ``protobuf`` are available, it uses SCIP as
a compiler-accurate refinement layer; otherwise it falls back to the Tree-sitter
fact extractor. Tree-sitter is always used to enrich SCIP nodes with properties
that SCIP does not provide (complexity, React labels, snippets, decorators).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

try:
    import tree_sitter_typescript

    _TS_PKG_VERSION = getattr(tree_sitter_typescript, "__version__", "unknown")
except ImportError:
    _TS_PKG_VERSION = "not-installed"

import xxhash

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.discovery import FileDiscovery
from ariadne_graph.core.models import CodeEdge, CodeGraphDelta, CodeNode
from ariadne_graph.languages.base import ExtractionContext

from .extractor import HAS_TREE_SITTER, TypeScriptFactExtractor
from .scip_enricher import TreeSitterEnricher
from .scip_indexer import ScipTypeScriptIndexer
from .scip_parser import ScipIndexParser
from .scip_translator import ScipGraphTranslator

logger = logging.getLogger(__name__)


class TypeScriptLanguageAdapter:
    """Adapter for extracting graph facts from TypeScript ``.ts``/``.tsx`` files.

    This class conforms to the :class:`LanguageAdapter` protocol and is intended
    to be used by the indexing pipeline::

        adapter = TypeScriptLanguageAdapter()
        paths = adapter.discover_files(repo_root, config)
        for path in paths:
            delta = adapter.extract_file(path, context)
            store.apply_delta(delta)

    When the optional ``tree-sitter`` and ``tree-sitter-typescript`` packages are
    not installed, extraction falls back to a single ``CodeFile`` node per file.
    When ``scip-typescript`` is unavailable, the Tree-sitter extractor is used
    directly and a diagnostic may be emitted.
    """

    language: str = "typescript"
    parser_version: str = f"tree-sitter-typescript_{_TS_PKG_VERSION}"
    extensions: tuple[str, ...] = (".ts", ".tsx")

    def __init__(self) -> None:
        self._scip_cache: dict[Path, CodeGraphDelta] = {}
        self._scip_enabled: bool = False
        self._scip_failure_message: str = ""

    def discover_files(self, root: Path, config: AnalyzerConfig) -> list[Path]:
        """Find all TypeScript source files under *root* respecting *config*.

        Args:
            root: Repository root directory.
            config: Analyzer configuration with ignore patterns and limits.

        Returns:
            Sorted list of ``.ts`` / ``.tsx`` file paths.
        """
        discovery = FileDiscovery(config)
        return discovery.discover(self.extensions)

    async def prepare_project(
        self,
        context: ExtractionContext,
        config: AnalyzerConfig,
        all_files: list[Path],
        changed_files: list[Path],
        graph_store: Any,
    ) -> None:
        """Run project-wide SCIP indexing when enabled.

        Parses the resulting ``index.scip`` and caches a :class:`CodeGraphDelta`
        for every document. Files not present in the cache fall back to
        Tree-sitter extraction in :meth:`extract_file`.
        """
        self._scip_cache.clear()
        self._scip_failure_message = ""

        if context.repo_root is None:
            return

        indexer = ScipTypeScriptIndexer(context.repo_root, config)

        if not indexer.should_run():
            logger.debug("SCIP-TypeScript not enabled or unavailable")
            return

        # Index every tsconfig project (root + subprojects like mobile/), not
        # just the root — otherwise a subproject the root tsconfig doesn't
        # `include` is silently uncovered.
        project_indexes = await indexer.ensure_project_indexes(
            all_files,
            context.graph_id,
            force=False,
        )
        if not project_indexes:
            self._scip_failure_message = "scip-typescript indexing failed or was skipped"
            logger.warning(
                "SCIP-TypeScript indexing failed or was skipped for %s", context.repo_root
            )
            return

        enricher = TreeSitterEnricher()
        parser = ScipIndexParser()

        for project_dir, index_path in project_indexes:
            # SCIP doc paths are relative to the project cwd; the prefix rebases
            # subproject paths to repo-root-relative so file_path/abs_path stay
            # unique and correct.
            prefix = (
                ""
                if project_dir == context.repo_root
                else str(project_dir.relative_to(context.repo_root))
            )
            try:
                scip_index = parser.parse(index_path)
            except Exception as exc:
                logger.warning("Failed to parse SCIP index %s: %s", index_path, exc)
                continue

            translator = ScipGraphTranslator(
                context.repo_root, context.graph_id, path_prefix=prefix
            )

            for doc_path, document in scip_index.documents.items():
                # Resolve against the PROJECT dir (that is what doc_path is
                # relative to), then keep only files that actually exist.
                abs_path = project_dir / doc_path
                if not abs_path.exists():
                    continue

                delta = translator.translate(document)
                try:
                    _, call_ranges, enclosing_map = enricher.enrich(
                        abs_path, delta, context.repo_root
                    )
                    delta = translator.translate(
                        document,
                        call_ranges=call_ranges,
                        enclosing_map=enclosing_map,
                    )
                    # Enrich the final delta with Tree-sitter-only properties.
                    delta, _, _ = enricher.enrich(abs_path, delta, context.repo_root)
                except Exception as exc:
                    logger.debug("Tree-sitter enrichment failed for %s: %s", abs_path, exc)

                self._add_source_commit(delta, context.source_commit)
                self._tag_owner_file(delta, abs_path)
                self._scip_cache[abs_path] = delta

        self._scip_enabled = bool(self._scip_cache)
        if self._scip_enabled:
            logger.info(
                "SCIP-TypeScript indexed %d files for %s",
                len(self._scip_cache),
                context.repo_root,
            )

    def extract_file(self, path: Path, context: ExtractionContext) -> CodeGraphDelta:
        """Parse a single TypeScript file and extract graph facts.

        If a SCIP delta is cached for *path*, it is returned after Tree-sitter
        enrichment. Otherwise the Tree-sitter extractor is used directly.
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
        relative_path = str(path.relative_to(context.repo_root))

        # SCIP path: return the cached, enriched delta if available.
        resolved_path = path.resolve()
        if resolved_path in self._scip_cache:
            delta = self._scip_cache[resolved_path]
            delta.content_hash = content_hash
            delta.parser_version = f"{self.parser_version}:scip"
            self._add_source_commit(delta, context.source_commit)
            self._tag_owner_file(delta, path)
            return delta

        # Tree-sitter fallback.
        if not HAS_TREE_SITTER:
            return self._stub_delta(path, relative_path, content_hash, context)

        extractor = TypeScriptFactExtractor(
            source=content,
            file_path=path,
            repo_root=context.repo_root,
            graph_id=context.graph_id,
            parser_version=self.parser_version,
            source_commit=context.source_commit,
        )
        delta = extractor.extract()
        delta.content_hash = content_hash

        # If SCIP failed project-wide, attach a diagnostic to the first file.
        if self._scip_failure_message:
            delta.nodes.append(
                CodeNode(
                    id=f"{relative_path}:diagnostic:scip_typescript",
                    graph_id=context.graph_id,
                    labels=["CodeDiagnostic", "KnowledgeNode"],
                    properties={
                        "level": "warning",
                        "rule": "scip_typescript",
                        "message": self._scip_failure_message,
                        "file_path": relative_path,
                        "language": self.language,
                        "source_commit": context.source_commit,
                    },
                )
            )

        self._add_source_commit(delta, context.source_commit)
        self._tag_owner_file(delta, path)

        return delta

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _stub_delta(
        self,
        path: Path,
        relative_path: str,
        content_hash: str,
        context: ExtractionContext,
    ) -> CodeGraphDelta:
        """Return a minimal stub delta when tree-sitter is not installed."""
        logger.warning(
            "TypeScript extraction is degraded for %s: "
            "tree-sitter-typescript is not installed. "
            'Install with: pip install -e ".[typescript]"',
            relative_path,
        )
        file_node = CodeNode(
            id=relative_path,
            graph_id=context.graph_id,
            labels=["CodeFile"],
            properties={
                "file_path": relative_path,
                "language": self.language,
                "lines": path.read_text(encoding="utf-8").count("\n") + 1,
                "source_commit": context.source_commit,
            },
        )
        diag_id = f"{relative_path}:diagnostic:missing_dependency"
        diagnostic = CodeNode(
            id=diag_id,
            graph_id=context.graph_id,
            labels=["CodeDiagnostic", "KnowledgeNode"],
            properties={
                "level": "warning",
                "rule": "missing_dependency",
                "message": (
                    "tree-sitter-typescript is not installed; "
                    "TypeScript extraction is limited to a stub CodeFile node. "
                    'Install with: pip install -e ".[typescript]"'
                ),
                "file_path": relative_path,
                "language": self.language,
                "source_commit": context.source_commit,
            },
        )
        diag_edge = CodeEdge(
            source=file_node.id,
            target=diag_id,
            graph_id=context.graph_id,
            rel_type="HAS_DIAGNOSTIC",
            properties={
                "owner_file_path": str(path),
                "source_commit": context.source_commit,
            },
        )
        return CodeGraphDelta(
            graph_id=context.graph_id,
            file_path=relative_path,
            nodes=[file_node, diagnostic],
            edges=[diag_edge],
            content_hash=content_hash,
            parser_version=f"{self.parser_version}:stub",
        )

    @staticmethod
    def _add_source_commit(delta: CodeGraphDelta, source_commit: str | None) -> None:
        if not source_commit:
            return
        for node in delta.nodes:
            node.properties.setdefault("source_commit", source_commit)
        for edge in delta.edges:
            edge.properties.setdefault("source_commit", source_commit)

    @staticmethod
    def _tag_owner_file(delta: CodeGraphDelta, path: Path) -> None:
        for edge in delta.edges:
            edge.properties.setdefault("owner_file_path", str(path))
