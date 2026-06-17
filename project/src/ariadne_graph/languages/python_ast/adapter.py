"""Python language adapter.

Implements the :class:`LanguageAdapter` protocol for Python source files
using the standard library ``ast`` module.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import xxhash

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.discovery import FileDiscovery
from ariadne_graph.core.models import CodeGraphDelta
from ariadne_graph.languages.base import ExtractionContext

from .extractor import PythonFactExtractor


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

        The file content is hashed with XXH3 for incremental sync.  On parse
        errors an empty delta is returned with the error description stored
        in the *parser_version* field.

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
        """No-op project-wide setup for Python extraction."""
