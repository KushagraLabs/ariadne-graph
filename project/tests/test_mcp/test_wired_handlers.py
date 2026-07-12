"""Integration tests for wired ToolRegistry handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.base import LanguageAdapter
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import (
    DeleteProjectInput,
    FindHotspotsInput,
    GetArchitectureInput,
    ImpactAnalysisInput,
    IndexInput,
    IndexStatusInput,
    InspectFileInput,
    ListDiagnosticsInput,
    LumenRetrieveInput,
    RetrieveInput,
    SearchCodeInput,
    SearchSemanticInput,
    TraceDependenciesInput,
)
from ariadne_graph.mcp.tools import ToolRegistry


def _write_sample_repo(repo_root: Path) -> None:
    """Create a tiny Python repo for indexing tests."""
    (repo_root / "package").mkdir(parents=True)
    (repo_root / "package" / "__init__.py").write_text("")
    (repo_root / "package" / "core.py").write_text(
        '''"""Core module."""

from package.utils import helper


class Widget:
    """A widget."""

    def __init__(self, name: str) -> None:
        self.name = name

    def process(self) -> str:
        return helper(self.name)


def main() -> None:
    w = Widget("test")
    return w.process()
'''
    )
    (repo_root / "package" / "utils.py").write_text(
        '''"""Utility module."""


def helper(value: str) -> str:
    return value.upper()
'''
    )


def _write_repo_with_unused_import(repo_root: Path) -> None:
    """Create a repo with an unused import to produce diagnostics."""
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "app.py").write_text(
        '''"""App module."""

import os


def run() -> None:
    return "hello"
'''
    )


def _make_registry(graph_store: GraphStore, repo_root: Path) -> ToolRegistry:
    config = AnalyzerConfig(repo_root=repo_root)
    adapters: dict[str, LanguageAdapter] = {"python": PythonLanguageAdapter()}
    searchable = graph_store if isinstance(graph_store, SearchableGraphStore) else None
    return ToolRegistry(
        graph_store=graph_store,
        searchable_store=searchable,
        adapters=adapters,
        config=config,
        snippet_extractor=SnippetExtractor(repo_root=repo_root),
        embedding_provider=None,
    )


@pytest.mark.asyncio
async def test_index_and_retrieve_memory(tmp_path: Path) -> None:
    """Index a repo into memory and retrieve a class."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    assert index_result.status == "success"
    assert index_result.files_indexed == 3

    graph_id = index_result.graph_id
    registry.register_graph_meta(str(repo), graph_id)

    retrieve_result = await registry.handle_retrieve(
        RetrieveInput(query="package.core.Widget", graph_id=graph_id)
    )
    assert retrieve_result.results
    # GraphRetriever returns a single "retrieve" entry; fallback returns node/neighbor/edge items.
    node_ids = {
        item["data"].get("id") if item.get("type") != "retrieve" else item["data"].get("node", {}).get("id")
        for item in retrieve_result.results
    }
    assert "package.core.Widget" in node_ids


@pytest.mark.asyncio
async def test_retrieve_by_repo_path_memory(tmp_path: Path) -> None:
    """Retrieve should derive graph_id from repo_path when graph_id is omitted."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    assert index_result.status == "success"

    retrieve_result = await registry.handle_retrieve(
        RetrieveInput(query="package.core.Widget", repo_path=str(repo))
    )
    assert retrieve_result.results
    node_ids = {
        item["data"].get("id") if item.get("type") != "retrieve" else item["data"].get("node", {}).get("id")
        for item in retrieve_result.results
    }
    assert "package.core.Widget" in node_ids


@pytest.mark.asyncio
async def test_lumen_retrieve_defaults_to_workspace_root(tmp_path: Path) -> None:
    """Lumen alias should default repo_path to the configured workspace root."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    config = AnalyzerConfig(repo_root=repo, lumen_workspace_root=repo)
    adapters: dict[str, LanguageAdapter] = {"python": PythonLanguageAdapter()}
    store = MemoryGraphStore()
    registry = ToolRegistry(
        graph_store=store,
        searchable_store=store,
        adapters=adapters,
        config=config,
        snippet_extractor=SnippetExtractor(repo_root=repo),
        embedding_provider=None,
    )

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    assert index_result.status == "success"

    lumen_result = await registry.handle_lumen_code_graph_retrieve(
        LumenRetrieveInput(query="package.core.Widget")
    )
    assert lumen_result.results
    node_ids = {
        item["data"].get("id") if item.get("type") != "retrieve" else item["data"].get("node", {}).get("id")
        for item in lumen_result.results
    }
    assert "package.core.Widget" in node_ids
    assert lumen_result.lumen_context["workspace_restricted"] is True


@pytest.mark.asyncio
async def test_search_code_memory(tmp_path: Path) -> None:
    """Keyword search over an in-memory graph."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    registry.register_graph_meta(str(repo), index_result.graph_id)

    search_result = await registry.handle_search_code(
        SearchCodeInput(pattern="helper", limit=10)
    )
    assert search_result.matches
    node_ids = {m["node_id"] for m in search_result.matches}
    assert "package.utils.helper" in node_ids


@pytest.mark.asyncio
async def test_trace_dependencies_memory(tmp_path: Path) -> None:
    """Trace dependencies from a symbol."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    graph_id = index_result.graph_id
    registry.register_graph_meta(str(repo), graph_id)

    trace_result = await registry.handle_trace_dependencies(
        TraceDependenciesInput(symbol="package.core.Widget", graph_id=graph_id, max_depth=2)
    )
    paths = trace_result.paths
    assert any("package.core.Widget.process" in p for p in paths)


@pytest.mark.asyncio
async def test_impact_analysis_memory(tmp_path: Path) -> None:
    """Impact analysis on an in-memory graph."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    graph_id = index_result.graph_id
    registry.register_graph_meta(str(repo), graph_id)

    impact_result = await registry.handle_impact_analysis(
        ImpactAnalysisInput(symbol="package.utils.helper", graph_id=graph_id)
    )
    assert impact_result.total_affected > 0
    assert "package.core.Widget.process" in impact_result.transitive_affected


@pytest.mark.asyncio
async def test_find_hotspots_memory(tmp_path: Path) -> None:
    """Hotspot detection on an in-memory graph."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    await registry.handle_index(IndexInput(repo_path=str(repo)))

    hotspots_result = await registry.handle_find_hotspots(
        FindHotspotsInput(repo_path=str(repo), top_n=5, metric="fan_in")
    )
    assert hotspots_result.hotspots


@pytest.mark.asyncio
async def test_get_architecture_memory(tmp_path: Path) -> None:
    """Architecture summary on an in-memory graph."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    await registry.handle_index(IndexInput(repo_path=str(repo)))

    arch_result = await registry.handle_get_architecture(
        GetArchitectureInput(repo_path=str(repo))
    )
    assert arch_result.summary["total_entities"] > 0


@pytest.mark.asyncio
async def test_inspect_file_memory(tmp_path: Path) -> None:
    """Inspect a single file from an in-memory graph."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    graph_id = index_result.graph_id

    core_file = str(repo / "package" / "core.py")
    inspect_result = await registry.handle_inspect_file(
        InspectFileInput(file_path=core_file, graph_id=graph_id)
    )
    assert inspect_result.nodes
    assert any(n["id"] == "package.core.Widget" for n in inspect_result.nodes)


@pytest.mark.asyncio
async def test_sqlite_keyword_search(tmp_path: Path) -> None:
    """Index into SQLite and use FTS-backed keyword search."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)

    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        assert index_result.status == "success"

        search_result = await registry.handle_search_code(
            SearchCodeInput(pattern="Widget", limit=10)
        )
        assert search_result.matches
        node_ids = {m["node_id"] for m in search_result.matches}
        assert "package.core.Widget" in node_ids
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_projects_memory(tmp_path: Path) -> None:
    """Indexing registers the project in an in-memory graph store catalog."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    assert index_result.status == "success"

    list_result = await registry.handle_list_projects()
    assert len(list_result.projects) == 1
    project = list_result.projects[0]
    assert project.graph_id == index_result.graph_id
    assert project.repo_path == str(repo.resolve())
    assert project.file_count == index_result.files_indexed
    assert project.last_indexed is not None


@pytest.mark.asyncio
async def test_list_projects_persisted_sqlite(tmp_path: Path) -> None:
    """A fresh ToolRegistry can list a project persisted in SQLite."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"

    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)
    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        assert index_result.status == "success"
        graph_id = index_result.graph_id
    finally:
        await store.close()

    # Fresh registry without register_graph_meta should still see the project.
    fresh_store = SQLiteGraphStore(str(db_path))
    fresh_registry = _make_registry(fresh_store, repo)
    try:
        list_result = await fresh_registry.handle_list_projects()
        assert len(list_result.projects) == 1
        project = list_result.projects[0]
        assert project.graph_id == graph_id
        assert project.repo_path == str(repo.resolve())

        # Index status should also work from the persisted catalog.
        status_result = await fresh_registry.handle_index_status(
            IndexStatusInput(repo_path=str(repo))
        )
        assert status_result.last_indexed is not None
        assert status_result.file_count == index_result.files_indexed
    finally:
        await fresh_store.close()


@pytest.mark.asyncio
async def test_delete_project_memory(tmp_path: Path) -> None:
    """Deleting a project removes it from the in-memory catalog."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    graph_id = index_result.graph_id
    assert await registry._graph_exists(graph_id)

    delete_result = await registry.handle_delete_project(
        DeleteProjectInput(repo_path=str(repo))
    )
    assert delete_result.deleted
    assert not await registry._graph_exists(graph_id)

    list_result = await registry.handle_list_projects()
    assert list_result.projects == []


@pytest.mark.asyncio
async def test_delete_project_sqlite(tmp_path: Path) -> None:
    """Deleting a project removes it from the SQLite catalog."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)

    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        graph_id = index_result.graph_id
        assert await registry._graph_exists(graph_id)

        delete_result = await registry.handle_delete_project(
            DeleteProjectInput(repo_path=str(repo))
        )
        assert delete_result.deleted
        assert not await registry._graph_exists(graph_id)
    finally:
        await store.close()

    # Fresh registry also sees an empty project list.
    fresh_store = SQLiteGraphStore(str(db_path))
    fresh_registry = _make_registry(fresh_store, repo)
    try:
        list_result = await fresh_registry.handle_list_projects()
        assert list_result.projects == []
    finally:
        await fresh_store.close()


@pytest.mark.asyncio
async def test_search_code_scoped_to_repo(tmp_path: Path) -> None:
    """Keyword search restricted to repo_path should only return that repo's nodes."""
    store = MemoryGraphStore()

    repo_a = tmp_path / "repo_a"
    _write_sample_repo(repo_a)
    registry_a = _make_registry(store, repo_a)
    index_a = await registry_a.handle_index(IndexInput(repo_path=str(repo_a)))
    registry_a.register_graph_meta(str(repo_a), index_a.graph_id)

    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    (repo_b / "other.py").write_text(
        '''"""Other module."""


def helper() -> str:
    return "other"
'''
    )
    registry_b = _make_registry(store, repo_b)
    index_b = await registry_b.handle_index(IndexInput(repo_path=str(repo_b)))
    registry_b.register_graph_meta(str(repo_b), index_b.graph_id)

    # Scoped search should only find the helper in repo_a.
    search_result = await registry_a.handle_search_code(
        SearchCodeInput(pattern="helper", repo_path=str(repo_a), limit=10)
    )
    assert search_result.matches
    for match in search_result.matches:
        assert match["graph_id"] == index_a.graph_id
    node_ids = {m["node_id"] for m in search_result.matches}
    assert "package.utils.helper" in node_ids


@pytest.mark.asyncio
async def test_list_diagnostics_memory(tmp_path: Path) -> None:
    """Diagnostic nodes are queryable after indexing."""
    repo = tmp_path / "repo"
    _write_repo_with_unused_import(repo)
    registry = _make_registry(MemoryGraphStore(), repo)

    index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    assert index_result.status == "success"

    diag_result = await registry.handle_list_diagnostics(
        ListDiagnosticsInput(repo_path=str(repo))
    )
    assert diag_result.diagnostics
    assert any(d["rule"] == "unused_import" for d in diag_result.diagnostics)
    assert any("os" in str(d["message"]) for d in diag_result.diagnostics)

    # Level filter should work.
    warning_result = await registry.handle_list_diagnostics(
        ListDiagnosticsInput(repo_path=str(repo), level="warning")
    )
    assert warning_result.diagnostics
    assert all(d["level"] == "warning" for d in warning_result.diagnostics)


@pytest.mark.asyncio
async def test_search_semantic_type_filter_with_none_node(tmp_path: Path) -> None:
    """Semantic search type filter must tolerate hits that lack a node payload."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)

    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        assert index_result.status == "success"
        registry.register_graph_meta(str(repo), index_result.graph_id)

        # Simulate a keyword/semantic backend that returns node-less hits.
        assert registry.searcher is not None
        original_search = registry.searcher.search

        async def _fake_search(
            graph_id: str, query: str, limit: int = 10, search_type: str = "hybrid"
        ) -> list[dict[str, Any]]:
            return [
                {
                    "node_id": "package.core.Widget",
                    "score": 0.9,
                    "node": None,
                }
            ]

        registry.searcher.search = _fake_search  # type: ignore[method-assign]

        result = await registry.handle_search_semantic(
            SearchSemanticInput(
                repo_path=str(repo),
                query_text="widget",
                limit=10,
                types=["class"],
            )
        )
        # The type filter should match because the node_id ends with Widget,
        # but since the fake hit has no node, the old code crashed. The new
        # code tolerates None and drops the hit (no class label available).
        assert result.hits is not None
        registry.searcher.search = original_search  # type: ignore[method-assign]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_search_code_language_filter_sqlite_keyword_hits(tmp_path: Path) -> None:
    """Language filter works when SQLite keyword search returns node-less hits."""
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)

    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        assert index_result.status == "success"

        # SQLite search_keyword returns SearchHit objects without a node payload.
        # The handler should hydrate the node to apply the language filter.
        search_result = await registry.handle_search_code(
            SearchCodeInput(pattern="helper", language="python", limit=10)
        )
        assert search_result.matches
        node_ids = {m["node_id"] for m in search_result.matches}
        assert "package.utils.helper" in node_ids
        for match in search_result.matches:
            file_path = match["properties"].get("file_path", "")
            assert file_path.endswith(".py")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_freshness_envelope_marks_content_change_stale(tmp_path: Path) -> None:
    """A content change to one file must surface as stale + dirty_file_count>=1
    on an analysis response, and reindexing must clear it.

    This is the hard case: only a CONTENT change (not a mere touch) counts, and
    the envelope must ride along on impact_analysis without a separate
    index_status call. SQLite store so file_hashes/last_indexed are persisted.
    """
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)

    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        graph_id = index_result.graph_id

        # Fresh right after indexing: no dirty files, not stale.
        fresh = await registry.handle_impact_analysis(
            ImpactAnalysisInput(symbol="package.utils.helper", graph_id=graph_id)
        )
        assert fresh.freshness is not None
        assert fresh.freshness["stale"] is False
        assert fresh.freshness["dirty_file_count"] == 0
        assert fresh.freshness["last_indexed"] is not None

        # Content change to one file — bump mtime past last_indexed AND change bytes.
        import os
        import time

        target = repo / "package" / "utils.py"
        target.write_text(
            '''"""Utility module."""


def helper(value: str) -> str:
    return value.lower()  # changed
'''
        )
        future = time.time() + 10
        os.utime(target, (future, future))

        stale = await registry.handle_impact_analysis(
            ImpactAnalysisInput(symbol="package.utils.helper", graph_id=graph_id)
        )
        assert stale.freshness is not None
        assert stale.freshness["stale"] is True
        assert stale.freshness["dirty_file_count"] >= 1

        # Reindex clears staleness.
        await registry.handle_index(IndexInput(repo_path=str(repo)))
        refreshed = await registry.handle_impact_analysis(
            ImpactAnalysisInput(symbol="package.utils.helper", graph_id=graph_id)
        )
        assert refreshed.freshness is not None
        assert refreshed.freshness["stale"] is False
        assert refreshed.freshness["dirty_file_count"] == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_freshness_prefilter_detects_content_change_at_equal_mtime(tmp_path: Path) -> None:
    """A content change whose mtime lands exactly on the index cutoff must still
    be caught — an equal mtime is not proof of unchanged bytes.

    Guards the mtime-prefilter boundary directly (``st_mtime < cutoff`` skips,
    ``==`` does not): coarse fs resolution / mtime-preserving tools can leave a
    modified file at exactly last_indexed.
    """
    import os
    from datetime import datetime

    from ariadne_graph.core.freshness import FreshnessTracker

    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)

    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        graph_id = index_result.graph_id

        tracker = FreshnessTracker(store)
        # Read last_indexed straight from metadata to pin the exact cutoff.
        meta_rows = await store.query(graph_id, "index_metadata", params={"graph_id": graph_id})
        last_indexed = meta_rows[0]["last_indexed"]
        cutoff = datetime.fromisoformat(last_indexed).timestamp()

        target = repo / "package" / "utils.py"
        target.write_text('def helper(value: str) -> str:\n    return value  # changed\n')
        os.utime(target, (cutoff, cutoff))  # mtime EXACTLY at the cutoff

        env = await tracker.compute_freshness(graph_id)
        assert env is not None
        assert env["dirty_file_count"] >= 1
        assert env["stale"] is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_freshness_multi_graph_aggregation_is_honest(tmp_path: Path) -> None:
    """Cross-repo aggregation must not let one clean graph mask staleness or
    missing coverage in another.

    A stale graph alongside a fresh one must aggregate to stale=True; an
    unknown/missing contributor must knock stale to None and dirty count to None
    rather than a confident False/0.
    """
    repo = tmp_path / "repo"
    _write_sample_repo(repo)
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    registry = _make_registry(store, repo)

    try:
        index_result = await registry.handle_index(IndexInput(repo_path=str(repo)))
        real_gid = index_result.graph_id

        # One fresh real graph.
        clean = await registry._freshness_envelope_multi([real_gid])
        assert clean is not None
        assert clean["stale"] is False

        # Fresh real graph + a graph id that has no envelope (unindexed) ->
        # coverage gap: stale and dirty count both go unknown (None).
        with_gap = await registry._freshness_envelope_multi([real_gid, "unindexed-xyz"])
        assert with_gap is not None
        assert with_gap["stale"] is None
        assert with_gap["dirty_file_count"] is None

        # Make the real graph stale; any stale contributor forces stale=True even
        # when another contributor is missing.
        import os
        import time

        target = repo / "package" / "utils.py"
        target.write_text("def helper(v: str) -> str:\n    return v  # changed\n")
        os.utime(target, (time.time() + 10, time.time() + 10))
        stale_agg = await registry._freshness_envelope_multi([real_gid, "unindexed-xyz"])
        assert stale_agg is not None
        assert stale_agg["stale"] is True
    finally:
        await store.close()
