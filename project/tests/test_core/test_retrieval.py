"""Tests for GraphRetriever."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ariadne_graph.core.models import CodeEdge, CodeNode, EmbeddingPayload, SearchHit
from ariadne_graph.core.retrieval import GraphRetriever
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import SearchableGraphStore


class FakeSearchableGraphStore(SearchableGraphStore):
    """In-memory searchable graph store sufficient for GraphRetriever tests."""

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self._edges: list[dict[str, Any]] = []
        self._hashes: dict[tuple[str, str], str] = {}
        self._projects: dict[str, dict[str, Any]] = {}

    def add(
        self,
        graph_id: str,
        node_id: str,
        labels: list[str] | None = None,
        **properties: Any,
    ) -> None:
        """Helper to add a node directly to the fake store."""
        self._nodes[(graph_id, node_id)] = {
            "id": node_id,
            "labels": labels or ["KnowledgeNode"],
            "properties": dict(properties),
        }

    def edge(
        self,
        graph_id: str,
        source: str,
        target: str,
        rel_type: str,
        **properties: Any,
    ) -> None:
        """Helper to add an edge directly to the fake store."""
        self._edges.append({
            "source": source,
            "target": target,
            "graph_id": graph_id,
            "rel_type": rel_type,
            "properties": dict(properties),
        })

    async def delete_graph(self, graph_id: str) -> None:
        self._nodes = {
            k: v for k, v in self._nodes.items() if k[0] != graph_id
        }
        self._edges = [e for e in self._edges if e["graph_id"] != graph_id]
        self._hashes = {k: v for k, v in self._hashes.items() if k[0] != graph_id}
        self._projects.pop(graph_id, None)

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        node_ids = {
            nid for (g, nid), n in self._nodes.items()
            if g == graph_id and n["properties"].get("file_path") == file_path
        }
        self._nodes = {
            k: v for k, v in self._nodes.items()
            if not (k[0] == graph_id and v["id"] in node_ids)
        }
        self._edges = [
            e for e in self._edges
            if not (e["graph_id"] == graph_id and (
                e["source"] in node_ids or e["target"] in node_ids
            ))
        ]
        self._hashes.pop((graph_id, file_path), None)

    async def add_nodes_batch(
        self, graph_id: str, nodes: Sequence[CodeNode]
    ) -> None:
        for node in nodes:
            self._nodes[(graph_id, node.id)] = {
                "id": node.id,
                "labels": node.labels,
                "properties": node.properties,
            }

    async def add_edges_batch(
        self, graph_id: str, edges: Sequence[CodeEdge]
    ) -> None:
        for edge in edges:
            self._edges.append({
                "source": edge.source,
                "target": edge.target,
                "graph_id": edge.graph_id,
                "rel_type": edge.rel_type,
                "properties": edge.properties,
            })

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = params or {}
        nodes_for_graph = [
            {"n": n} for (g, _), n in self._nodes.items() if g == graph_id
        ]
        edges_for_graph = [
            {"r": e} for e in self._edges if e["graph_id"] == graph_id
        ]

        if query == "nodes":
            return nodes_for_graph

        if query == "edges":
            return edges_for_graph

        if query == "node_by_id":
            node_id = params.get("node_id", "")
            node = self._nodes.get((graph_id, node_id))
            return [{"n": node}] if node else []

        if query == "node_by_name":
            name = params.get("name", "")
            results = [
                row for row in nodes_for_graph
                if row["n"]["properties"].get("name") == name
            ]
            return results

        if query == "node_name_fuzzy":
            name = params.get("name", "").lower()
            results: list[dict[str, Any]] = []
            for row in nodes_for_graph:
                node = row["n"]
                if name in node["id"].lower():
                    results.append(row)
                    continue
                props = node["properties"]
                if (
                    name in str(props.get("name", "")).lower()
                    or name in str(props.get("qualname", "")).lower()
                ):
                    results.append(row)
            return results

        if query == "node_outgoing_edges":
            node_id = params.get("node_id", "")
            return [{"r": e} for e in self._edges if e["graph_id"] == graph_id and e["source"] == node_id]

        if query == "node_incoming_edges":
            node_id = params.get("node_id", "")
            return [{"r": e} for e in self._edges if e["graph_id"] == graph_id and e["target"] == node_id]

        if query == "node_all_edges":
            node_id = params.get("node_id", "")
            return [{"r": e} for e in self._edges if e["graph_id"] == graph_id and (e["source"] == node_id or e["target"] == node_id)]

        if query == "node_edges":
            return await self.query(graph_id, "node_all_edges", params)

        if query == "stored_file_paths":
            return [
                {"file_path": p} for (g, p) in self._hashes if g == graph_id
            ]

        if query == "index_metadata":
            project = self._projects.get(graph_id)
            return [project] if project else []

        if query == "set_index_metadata":
            self._projects[graph_id] = {
                "repo_path": params.get("repo_path", ""),
                "last_indexed": params.get("last_indexed"),
                "file_count": params.get("file_count", 0),
                "sync_enabled": params.get("sync_enabled", False),
            }
            return []

        if query == "count_files":
            files = {
                n["properties"].get("file_path")
                for (g, _), n in self._nodes.items()
                if g == graph_id
            }
            return [{"count": len(files - {None})}]

        if query == "file_hashes":
            return [
                {"file_path": p, "content_hash": h}
                for (g, p), h in self._hashes.items() if g == graph_id
            ]

        if query == "dirty_files":
            current_hashes: dict[str, str] = params.get("current_hashes", {})
            return [
                {"file_path": p}
                for (g, p), h in self._hashes.items()
                if g == graph_id and current_hashes.get(p) != h
            ]

        return []

    async def get_stored_hash(self, graph_id: str, file_path: str) -> str | None:
        return self._hashes.get((graph_id, file_path))

    async def update_hash(
        self, graph_id: str, file_path: str, content_hash: str
    ) -> None:
        self._hashes[(graph_id, file_path)] = content_hash

    async def register_project(
        self,
        graph_id: str,
        repo_path: str | Path,
        file_count: int | None = None,
        sync_enabled: bool = False,
    ) -> None:
        existing = self._projects.get(graph_id, {})
        self._projects[graph_id] = {
            "repo_path": str(Path(repo_path).resolve()),
            "last_indexed": existing.get("last_indexed"),
            "file_count": file_count if file_count is not None else existing.get("file_count", 0),
            "sync_enabled": sync_enabled,
        }

    async def list_projects(self) -> list[dict[str, Any]]:
        return list(self._projects.values())

    async def upsert_embeddings(
        self, graph_id: str, rows: Sequence[EmbeddingPayload]
    ) -> None:
        return None

    async def search_vector(
        self, graph_id: str, vector: Sequence[float], limit: int = 10
    ) -> list[SearchHit]:
        return []

    async def search_keyword(
        self, graph_id: str, query: str, limit: int = 10
    ) -> list[SearchHit]:
        return []

    async def set_communities(
        self, graph_id: str, assignments: dict[str, int]
    ) -> None:
        return None

    async def get_communities(self, graph_id: str) -> dict[int, list[str]]:
        return {}


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture
def snippet_extractor(tmp_repo: Path) -> SnippetExtractor:
    return SnippetExtractor(repo_root=tmp_repo)


@pytest.fixture
def store() -> FakeSearchableGraphStore:
    return FakeSearchableGraphStore()


@pytest.fixture
def retriever(
    store: FakeSearchableGraphStore, snippet_extractor: SnippetExtractor
) -> GraphRetriever:
    return GraphRetriever(store, snippet_extractor)


@pytest.mark.asyncio
async def test_retrieve_node_not_found(
    retriever: GraphRetriever
) -> None:
    """A missing symbol returns a structured not-found response."""
    result = await retriever.retrieve_node("g1", "missing")

    assert result["found"] is False
    assert result["query"] == "missing"
    assert result["node"] is None
    assert result["edges"] == {"outgoing": [], "incoming": []}
    assert result["snippet"] == ""


@pytest.mark.asyncio
async def test_retrieve_node_by_id(
    store: FakeSearchableGraphStore,
    retriever: GraphRetriever,
    tmp_repo: Path,
) -> None:
    """retrieve_node resolves by exact node ID and returns edges + snippet."""
    src = tmp_repo / "mod.py"
    src.write_text("def func():\n    pass\n")

    store.add(
        "g1",
        "mod.func",
        labels=["CodeFunction", "KnowledgeNode"],
        name="func",
        file_path=str(src),
        line_start=1,
        line_end=1,
    )
    store.add("g1", "mod.cls", labels=["CodeClass", "KnowledgeNode"], name="cls")
    store.edge("g1", "mod.func", "mod.cls", "CALLS")

    result = await retriever.retrieve_node("g1", "mod.func")

    assert result["found"] is True
    assert result["node"]["id"] == "mod.func"
    assert len(result["edges"]["outgoing"]) == 1
    assert result["edges"]["outgoing"][0]["target"] == "mod.cls"
    assert "def func()" in result["snippet"]


@pytest.mark.asyncio
async def test_retrieve_node_by_name(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """retrieve_node falls back to name-based lookup."""
    store.add("g1", "mod.helper", name="helper")

    result = await retriever.retrieve_node("g1", "helper")

    assert result["found"] is True
    assert result["node"]["id"] == "mod.helper"


@pytest.mark.asyncio
async def test_retrieve_node_fuzzy_fallback(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """retrieve_node uses fuzzy lookup when exact and name matches fail."""
    store.add("g1", "my_module.do_work", name="do_work")

    result = await retriever.retrieve_node("g1", "do_work")

    assert result["found"] is True


@pytest.mark.asyncio
async def test_trace_dependencies_downstream(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """trace_dependencies direction='down' follows outgoing edges."""
    store.add("g1", "a")
    store.add("g1", "b")
    store.add("g1", "c")
    store.edge("g1", "a", "b", "CALLS")
    store.edge("g1", "b", "c", "CALLS")

    paths = await retriever.trace_dependencies("g1", "a", direction="down", max_depth=3)

    ids = {p["node_id"] for p in paths}
    assert ids == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_trace_dependencies_upstream(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """trace_dependencies direction='up' follows incoming edges."""
    store.add("g1", "a")
    store.add("g1", "b")
    store.add("g1", "c")
    store.edge("g1", "a", "b", "CALLS")
    store.edge("g1", "b", "c", "CALLS")

    paths = await retriever.trace_dependencies("g1", "c", direction="up", max_depth=3)

    ids = {p["node_id"] for p in paths}
    assert ids == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_trace_dependencies_max_depth(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """trace_dependencies respects max_depth."""
    store.add("g1", "a")
    store.add("g1", "b")
    store.add("g1", "c")
    store.edge("g1", "a", "b", "CALLS")
    store.edge("g1", "b", "c", "CALLS")

    paths = await retriever.trace_dependencies("g1", "a", direction="down", max_depth=1)

    ids = {p["node_id"] for p in paths}
    assert ids == {"a", "b"}


@pytest.mark.asyncio
async def test_trace_dependencies_symbol_not_found(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """Tracing an unknown symbol returns an empty list."""
    result = await retriever.trace_dependencies("g1", "missing", direction="both")
    assert result == []


@pytest.mark.asyncio
async def test_impact_analysis(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """impact_analysis returns transitive closure and coupling scores."""
    store.add("g1", "core")
    store.add("g1", "dep1")
    store.add("g1", "dep2")
    store.add("g1", "caller")
    store.edge("g1", "core", "dep1", "CALLS")
    store.edge("g1", "core", "dep2", "CALLS")
    store.edge("g1", "caller", "core", "CALLS")

    result = await retriever.impact_analysis("g1", "core")

    assert result.target_symbol == "core"
    assert result.total_affected == 3
    assert set(result.direct_dependencies) == {"dep1", "dep2"}
    assert "caller" in result.transitive_affected
    assert result.coupling_scores["caller"] == 1.0
    assert result.coupling_scores["dep1"] == 1.0


@pytest.mark.asyncio
async def test_impact_analysis_not_found(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """impact_analysis for an unknown symbol returns an empty result."""
    result = await retriever.impact_analysis("g1", "missing")

    assert result.target_symbol == "missing"
    assert result.total_affected == 0
    assert result.direct_dependencies == []
    assert result.transitive_affected == []
    assert result.coupling_scores == {}


@pytest.mark.asyncio
async def test_get_edges_directions(
    store: FakeSearchableGraphStore, retriever: GraphRetriever
) -> None:
    """_get_edges filters by direction correctly."""
    store.add("g1", "src")
    store.add("g1", "dst")
    store.add("g1", "other")
    store.edge("g1", "src", "dst", "CALLS")
    store.edge("g1", "other", "src", "CALLS")

    outgoing = await retriever._get_edges("g1", "src", "outgoing")
    assert [e["target"] for e in outgoing] == ["dst"]

    incoming = await retriever._get_edges("g1", "src", "incoming")
    assert [e["source"] for e in incoming] == ["other"]

    both = await retriever._get_edges("g1", "src", "both")
    assert len(both) == 2
