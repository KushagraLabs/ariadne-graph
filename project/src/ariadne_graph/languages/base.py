"""Language adapter protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.models import CodeGraphDelta


class UniqueIdMixin:
    """Mixin that provides stable, file-scoped unique node IDs.

    Extractors instantiate one instance per file, so disambiguation is scoped
    to that file while IDs remain stable across separate files. The first
    occurrence of a base ID keeps the clean form; later collisions receive a
    source-position suffix (``@L<line>C<col>``). If no source position is
    available, a sequential counter suffix is used as a last resort.
    """

    def __init__(self) -> None:
        self._used_node_ids: set[str] = set()
        self._collision_counters: dict[str, int] = {}

    def _unique_node_id(self, base_id: str, node: Any) -> str:
        """Return a stable ID that is unique within the current file.

        Keeps *base_id* unchanged on first use. Appends ``@L<line>C<col>`` when
        the same base ID is reused and a source position can be extracted from
        *node*. Falls back to a ``#N`` counter if the position is unavailable.
        """
        if base_id not in self._used_node_ids:
            return base_id

        line: int | None = None
        col: int | None = None

        # Tree-sitter nodes use 0-based (row, col) tuples.
        start_point = getattr(node, "start_point", None)
        if start_point is not None:
            line = start_point[0] + 1
            col = start_point[1] + 1
        else:
            # Python AST nodes use 1-based lineno and 0-based col_offset.
            lineno = getattr(node, "lineno", None)
            col_offset = getattr(node, "col_offset", None)
            if lineno is not None and col_offset is not None:
                line = lineno
                col = col_offset + 1

        if line is not None and col is not None:
            return f"{base_id}@L{line}C{col}"

        # Last-resort sequential counter when no source position is available.
        counter = self._collision_counters.get(base_id, 0) + 1
        self._collision_counters[base_id] = counter
        return f"{base_id}#{counter}"


class ExtractionContext(BaseModel):
    """Context passed to language adapters during file extraction."""

    graph_id: str
    repo_root: Path
    source_commit: str | None = Field(default=None)
    all_files: list[Path] = Field(default_factory=list)
    changed_files: list[Path] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


@runtime_checkable
class LanguageAdapter(Protocol):
    """Protocol for language-specific fact extractors.

    Each adapter handles file discovery and AST-based extraction
    for one language family.
    """

    language: str
    parser_version: str
    extensions: tuple[str, ...]

    def discover_files(
        self, root: Path, config: AnalyzerConfig
    ) -> list[Path]:
        """Find all source files for this language under root."""
        ...

    def extract_file(
        self, path: Path, context: ExtractionContext
    ) -> CodeGraphDelta:
        """Parse a single file and extract graph facts."""
        ...

    async def prepare_project(
        self,
        context: ExtractionContext,
        config: AnalyzerConfig,
        all_files: list[Path],
        changed_files: list[Path],
        graph_store: Any,
    ) -> None:
        """Optional project-wide setup before per-file extraction.

        Adapters that need to run a project-level indexer (for example,
        scip-typescript) can implement this hook. *config* provides the active
        analyzer configuration; *graph_store* is passed so the adapter can
        read/write project metadata such as fingerprints. The default
        implementation is a no-op.
        """
        ...
