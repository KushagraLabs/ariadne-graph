"""Tests for CommunityAnalyzer."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from ariadne_graph.core.communities import CommunityAnalyzer
from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.base import SearchableGraphStore


class FakeSearchableGraphStore(SearchableGraphStore):
    """In-memory searchable store for community analysis tests."""

    def __init__(self) -> None:
        self._nodes: dict[str, CodeNode] = {}
        self._edges: list[CodeEdge] = []
        self._communities: dict[int, list[str]] = {}

    async def delete_graph(self, graph_id: str) -> None:
        pass

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        pass

    async def add_nodes_batch(self, graph_id: str, nodes: Sequence[CodeNode]) -> None:
        for node in nodes:
            self._nodes[node.id] = node

    async def add_edges_batch(self, graph_id: str, edges: Sequence[CodeEdge]) -> None:
        self._edges.extend(edges)

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if query == "nodes":
            return [{"n": node.model_dump()} for node in self._nodes.values()]
        if query == "edges":
            return [{"r": edge.model_dump()} for edge in self._edges]
        return []

    async def get_stored_hash(self, graph_id: str, file_path: str) -> str | None:
        return None

    async def update_hash(
        self, graph_id: str, file_path: str, content_hash: str
    ) -> None:
        pass

    async def close(self) -> None:
        pass

    async def upsert_embeddings(
        self, graph_id: str, rows: Sequence[Any]
    ) -> None:
        pass

    async def search_vector(
        self, graph_id: str, vector: Sequence[float], limit: int
    ) -> list[Any]:
        return []

    async def search_keyword(self, graph_id: str, query: str, limit: int) -> list[Any]:
        return []

    async def set_communities(self, graph_id: str, assignments: dict[str, int]) -> None:
        self._communities = {}
        for node_id, cid in assignments.items():
            self._communities.setdefault(cid, []).append(node_id)

    async def get_communities(self, graph_id: str) -> dict[int, list[str]]:
        return self._communities


@pytest.fixture
def store() -> FakeSearchableGraphStore:
    return FakeSearchableGraphStore()


@pytest.fixture
def analyzer(store: FakeSearchableGraphStore) -> CommunityAnalyzer:
    return CommunityAnalyzer(store)


class TestCommunityAnalyzer:
    """Tests for community detection and architecture summary."""

    @pytest.mark.asyncio
    async def test_detect_communities_empty_graph(
        self, analyzer: CommunityAnalyzer
    ) -> None:
        assignments = await analyzer.detect_communities("g")
        assert assignments == {}

    @pytest.mark.asyncio
    async def test_detect_communities_isolated_nodes(
        self, analyzer: CommunityAnalyzer, store: FakeSearchableGraphStore
    ) -> None:
        await store.add_nodes_batch(
            "g",
            [
                CodeNode(id="a", graph_id="g", labels=["CodeFunction"], properties={}),
                CodeNode(id="b", graph_id="g", labels=["CodeFunction"], properties={}),
            ],
        )
        assignments = await analyzer.detect_communities("g")
        assert len(assignments) == 2
        assert set(assignments.values()) == {0}

    @pytest.mark.asyncio
    async def test_detect_communities_two_groups(
        self, analyzer: CommunityAnalyzer, store: FakeSearchableGraphStore
    ) -> None:
        # Two tightly coupled groups with a single bridge edge.
        nodes = [
            CodeNode(id=f"n{i}", graph_id="g", labels=["CodeFunction"], properties={})
            for i in range(5)
        ]
        await store.add_nodes_batch("g", nodes)
        edges = [
            CodeEdge(source="n0", target="n1", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="n1", target="n2", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="n2", target="n0", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="n3", target="n4", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="n4", target="n3", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="n0", target="n3", graph_id="g", rel_type="CALLS"),
        ]
        await store.add_edges_batch("g", edges)

        assignments = await analyzer.detect_communities("g")
        assert len(set(assignments.values())) >= 1
        assert set(assignments.keys()) == {f"n{i}" for i in range(5)}

    @pytest.mark.asyncio
    async def test_get_architecture_summary(
        self, analyzer: CommunityAnalyzer, store: FakeSearchableGraphStore
    ) -> None:
        nodes = [
            CodeNode(id="a", graph_id="g", labels=["CodeFunction"], properties={}),
            CodeNode(id="b", graph_id="g", labels=["CodeFunction"], properties={}),
            CodeNode(id="c", graph_id="g", labels=["CodeFunction"], properties={}),
        ]
        await store.add_nodes_batch("g", nodes)
        edges = [
            CodeEdge(source="a", target="b", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="b", target="c", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="c", target="a", graph_id="g", rel_type="CALLS"),
        ]
        await store.add_edges_batch("g", edges)

        summary = await analyzer.get_architecture_summary("g")
        assert summary.total_entities == 3
        assert len(summary.communities) >= 1
        assert len(summary.hotspots) == 3

    @pytest.mark.asyncio
    async def test_find_hotspots(
        self, analyzer: CommunityAnalyzer, store: FakeSearchableGraphStore
    ) -> None:
        nodes = [
            CodeNode(id="hub", graph_id="g", labels=["CodeFunction"], properties={}),
            CodeNode(id="leaf1", graph_id="g", labels=["CodeFunction"], properties={}),
            CodeNode(id="leaf2", graph_id="g", labels=["CodeFunction"], properties={}),
        ]
        await store.add_nodes_batch("g", nodes)
        edges = [
            CodeEdge(source="leaf1", target="hub", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="leaf2", target="hub", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="hub", target="leaf1", graph_id="g", rel_type="CALLS"),
        ]
        await store.add_edges_batch("g", edges)

        hotspots = await analyzer.find_hotspots("g", top_n=3, metric="coupling")
        assert hotspots
        assert hotspots[0].node_id == "hub"
