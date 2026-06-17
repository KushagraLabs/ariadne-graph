"""Integration tests for wired ToolRegistry handlers."""

from __future__ import annotations

from pathlib import Path

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
    RetrieveInput,
    SearchCodeInput,
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
