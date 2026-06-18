"""Tests for HybridSearcher."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from ariadne_graph.core.embeddings import EmbeddingProvider
from ariadne_graph.core.models import CodeNode, SearchHit
from ariadne_graph.core.search import HybridSearcher
from ariadne_graph.graphstores.base import SearchableGraphStore


class FakeEmbeddingProvider(EmbeddingProvider):
    """Embedding provider that returns deterministic vectors."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Return a simple one-hot-ish vector per text based on length.
        return [[float(len(t)) / 100.0] * 384 for t in texts]

    @property
    def dimensions(self) -> int:
        return 384


class FakeSearchableGraphStore(SearchableGraphStore):
    """In-memory searchable store for testing HybridSearcher."""

    def __init__(self) -> None:
        self._nodes: dict[str, CodeNode] = {}
        self._embeddings: dict[str, list[float]] = {}

    async def delete_graph(self, graph_id: str) -> None:
        pass

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        pass

    async def add_nodes_batch(self, graph_id: str, nodes: Sequence[CodeNode]) -> None:
        for node in nodes:
            self._nodes[node.id] = node

    async def add_edges_batch(self, graph_id: str, edges: Sequence[Any]) -> None:
        pass

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = params or {}
        if query == "nodes":
            return [{"n": node.model_dump()} for node in self._nodes.values()]
        if query == "node_by_id":
            node_id = params.get("node_id", "")
            node = self._nodes.get(node_id)
            return [{"n": node.model_dump()}] if node else []
        if query == "node_by_name":
            name = params.get("name", "")
            for node in self._nodes.values():
                if node.properties.get("name") == name:
                    return [{"n": node.model_dump()}]
            return []
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
        for row in rows:
            self._embeddings[row.node_id] = row.embedding

    async def search_vector(
        self, graph_id: str, vector: Sequence[float], limit: int
    ) -> list[SearchHit]:
        # Return all nodes with embeddings, sorted by cosine similarity to vector.
        import math

        def norm(v: Sequence[float]) -> float:
            return math.sqrt(sum(x * x for x in v)) or 1.0

        query_norm = norm(vector)
        scored: list[tuple[str, float]] = []
        for node_id, emb in self._embeddings.items():
            dot = sum(a * b for a, b in zip(vector, emb, strict=False))
            score = dot / (query_norm * norm(emb))
            scored.append((node_id, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            SearchHit(
                node_id=node_id,
                score=score,
                node=self._nodes.get(node_id),
            )
            for node_id, score in scored[:limit]
        ]

    async def search_keyword(self, graph_id: str, query: str, limit: int) -> list[SearchHit]:
        query_lower = query.lower()
        hits: list[SearchHit] = []
        for node in self._nodes.values():
            text = " ".join(
                str(v)
                for v in [node.id, node.properties.get("name", "")]
                if v is not None
            ).lower()
            if query_lower in text:
                hits.append(SearchHit(node_id=node.id, score=1.0, node=node))
        return hits[:limit]

    async def set_communities(self, graph_id: str, assignments: dict[str, int]) -> None:
        pass

    async def get_communities(self, graph_id: str) -> dict[int, list[str]]:
        return {}


@pytest.fixture
def store() -> FakeSearchableGraphStore:
    return FakeSearchableGraphStore()


@pytest.fixture
def embeddings() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


@pytest.fixture
def searcher(
    store: FakeSearchableGraphStore, embeddings: FakeEmbeddingProvider
) -> HybridSearcher:
    return HybridSearcher(store, embeddings)


class TestHybridSearcher:
    """Tests for HybridSearcher search modes."""

    @pytest.mark.asyncio
    async def test_symbol_search_exact_id(self, searcher: HybridSearcher, store: FakeSearchableGraphStore) -> None:
        node = CodeNode(
            id="mod.func",
            graph_id="g",
            labels=["KnowledgeNode", "CodeFunction"],
            properties={"name": "func"},
        )
        await store.add_nodes_batch("g", [node])

        results = await searcher.search("g", "mod.func", search_type="symbol")
        assert len(results) == 1
        assert results[0]["node_id"] == "mod.func"

    @pytest.mark.asyncio
    async def test_keyword_search(self, searcher: HybridSearcher, store: FakeSearchableGraphStore) -> None:
        node = CodeNode(
            id="mod.helper",
            graph_id="g",
            labels=["KnowledgeNode", "CodeFunction"],
            properties={"name": "helper"},
        )
        await store.add_nodes_batch("g", [node])

        results = await searcher.search("g", "helper", search_type="keyword")
        assert len(results) == 1
        assert results[0]["node_id"] == "mod.helper"

    @pytest.mark.asyncio
    async def test_semantic_search_requires_embeddings(
        self, store: FakeSearchableGraphStore
    ) -> None:
        searcher_no_emb = HybridSearcher(store, None)
        results = await searcher_no_emb.search("g", "query", search_type="semantic")
        assert results == []

    @pytest.mark.asyncio
    async def test_hybrid_search_combines_results(
        self,
        searcher: HybridSearcher,
        store: FakeSearchableGraphStore,
        embeddings: FakeEmbeddingProvider,
    ) -> None:
        nodes = [
            CodeNode(
                id="mod.alpha",
                graph_id="g",
                labels=["KnowledgeNode", "CodeFunction"],
                properties={"name": "alpha"},
            ),
            CodeNode(
                id="mod.beta",
                graph_id="g",
                labels=["KnowledgeNode", "CodeFunction"],
                properties={"name": "beta"},
            ),
        ]
        await store.add_nodes_batch("g", nodes)
        alpha_vec = (await embeddings.embed(["alpha"]))[0]
        beta_vec = (await embeddings.embed(["beta"]))[0]
        await store.upsert_embeddings(
            "g",
            [
                type("Row", (), {"node_id": "mod.alpha", "embedding": alpha_vec}),
                type("Row", (), {"node_id": "mod.beta", "embedding": beta_vec}),
            ],
        )

        results = await searcher.search("g", "alpha", search_type="hybrid")
        node_ids = {r["node_id"] for r in results}
        assert "mod.alpha" in node_ids

    @pytest.mark.asyncio
    async def test_unknown_search_type_falls_back_to_hybrid(
        self, searcher: HybridSearcher, store: FakeSearchableGraphStore
    ) -> None:
        node = CodeNode(
            id="mod.func",
            graph_id="g",
            labels=["KnowledgeNode", "CodeFunction"],
            properties={"name": "func"},
        )
        await store.add_nodes_batch("g", [node])

        results = await searcher.search("g", "func", search_type="unknown")
        assert any(r["node_id"] == "mod.func" for r in results)

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, searcher: HybridSearcher) -> None:
        results = await searcher.search("g", "   ", search_type="keyword")
        assert results == []
