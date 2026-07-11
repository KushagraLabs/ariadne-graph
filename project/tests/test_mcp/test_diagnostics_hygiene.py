"""Architecture-hygiene discoverability + aggregation on ``list_diagnostics``.

These tests pin two things about the EXISTING ``code_graph_list_diagnostics``
surface (we deliberately do NOT add a parallel ``list_hygiene_violations`` tool —
the ``CodeDiagnostic`` node is the single source of truth for every rule,
including the architecture rules from :mod:`ariadne_graph.core.architecture`):

1. **Equivalence (already true).** The ``(source, target)`` edges that
   ``handle_list_diagnostics(rule="deep_import")`` returns are exactly the
   file->file edges the browser view paints pink via ``is_deep_import`` in
   :func:`ariadne_graph.web.queries.full_graph`. This proves the MCP already
   exposes what the UI shows — no UI-only data.

2. **The gap (currently failing).** ``cosmic_lens`` asked for aggregate counts
   (grouped by rule / source->target / prod vs test) and a ``production_only``
   filter. ``list_diagnostics`` does not provide either yet.

Uses SQLite (not memory) because the pink-edge query and the deep_import
analysis both read SCIP-resolved file->file edges via raw SQL over the store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import IndexInput, ListDiagnosticsInput
from ariadne_graph.mcp.tools import ToolRegistry
from ariadne_graph.web.queries import full_graph


def _write_layered_repo(repo_root: Path) -> None:
    """A repo with a cross-organ deep import and a test-organ diagnostic.

    ``app/main.py`` reaches into ``lib/deep/`` internals (one level below the
    ``lib`` organ root) => a ``deep_import`` violation and a pink edge. It also
    has an unused ``os`` import (a production ``unused_import``). ``tests/`` adds
    diagnostics on a peripheral organ so prod/test aggregation is exercised.
    """
    (repo_root / "lib" / "deep").mkdir(parents=True)
    (repo_root / "lib" / "__init__.py").write_text("")
    (repo_root / "lib" / "deep" / "__init__.py").write_text("")
    (repo_root / "lib" / "deep" / "engine.py").write_text(
        "def run(x):\n    return x + 1\n"
    )
    (repo_root / "app").mkdir()
    (repo_root / "app" / "__init__.py").write_text("")
    (repo_root / "app" / "main.py").write_text(
        "import os\n"
        "from lib.deep.engine import run\n"
        "\n"
        "\n"
        "def go():\n"
        "    return run(1)\n"
    )
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "__init__.py").write_text("")
    (repo_root / "tests" / "test_it.py").write_text(
        "import sys\n\n\ndef test_x():\n    assert True\n"
    )


def _make_registry(store: SQLiteGraphStore, repo_root: Path) -> ToolRegistry:
    config = AnalyzerConfig(repo_root=repo_root)
    return ToolRegistry(
        graph_store=store,
        searchable_store=store,
        adapters={"python": PythonLanguageAdapter()},
        config=config,
        snippet_extractor=SnippetExtractor(repo_root=repo_root),
        embedding_provider=None,
    )


def _rel(abs_or_rel: str, repo_root: str) -> str:
    """Repo-relative form (mirror of the analysis/web ``_rel`` used to compare)."""
    root = repo_root.rstrip("/") + "/"
    return abs_or_rel[len(root):] if abs_or_rel.startswith(root) else abs_or_rel


@pytest.mark.asyncio
async def test_deep_import_equivalence_mcp_vs_pink_edges(tmp_path: Path) -> None:
    """The MCP's deep_import edges == the browser's pink edges (same SSOT)."""
    repo = tmp_path / "repo"
    _write_layered_repo(repo)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        index = await registry.handle_index(IndexInput(repo_path=str(repo)))
        assert index.status == "success"
        repo_root = str(repo.resolve())

        diag = await registry.handle_list_diagnostics(
            ListDiagnosticsInput(repo_path=str(repo), rule="deep_import")
        )
        mcp_edges = {
            (d["properties"]["from"], d["properties"]["to"])
            for d in diag.diagnostics
        }
        assert mcp_edges, "expected a deep_import diagnostic for app -> lib/deep"

        graph = await full_graph(store, index.graph_id, repo_root=repo_root)
        pink_edges = {
            (_rel(e["source"], repo_root), _rel(e["target"], repo_root))
            for e in graph["edges"]
            if e.get("kind") == "dep" and e.get("violation")
        }

        # The proof: nothing the UI paints pink is invisible to the MCP tool.
        assert mcp_edges == pink_edges
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_diagnostics_returns_aggregate_counts(tmp_path: Path) -> None:
    """cosmic_lens needs counts grouped by rule and by prod/test. (GAP)"""
    repo = tmp_path / "repo"
    _write_layered_repo(repo)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await registry.handle_index(IndexInput(repo_path=str(repo)))

        result = await registry.handle_list_diagnostics(
            ListDiagnosticsInput(repo_path=str(repo))
        )

        # NEW: an aggregate rollup over ALL matching diagnostics (not just the
        # page returned in `diagnostics`). Grouped by rule and by prod/test.
        counts = result.counts  # type: ignore[attr-defined]
        assert counts["by_rule"]["deep_import"] == 1
        assert counts["by_rule"]["unused_import"] == 2  # app + tests
        assert counts["by_production"]["production"] >= 1
        assert counts["by_production"]["test"] >= 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_diagnostics_production_only_filter(tmp_path: Path) -> None:
    """production_only must drop diagnostics owned by peripheral organs. (GAP)"""
    repo = tmp_path / "repo"
    _write_layered_repo(repo)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await registry.handle_index(IndexInput(repo_path=str(repo)))
        repo_root = str(repo.resolve())

        prod = await registry.handle_list_diagnostics(
            ListDiagnosticsInput(repo_path=str(repo), production_only=True)  # type: ignore[call-arg]
        )

        organs = {
            _rel(d["file_path"], repo_root).split("/", 1)[0]
            for d in prod.diagnostics
        }
        assert "tests" not in organs
        assert "app" in organs  # production diagnostics survive
    finally:
        await store.close()
