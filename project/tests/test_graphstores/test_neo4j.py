"""Unit tests for Neo4jGraphStore.

These tests require a running Neo4j server and the ``neo4j`` Python package.
They are skipped automatically when either is unavailable.
"""

from __future__ import annotations

import os

import pytest

from ariadne_graph.core.models import CodeEdge, CodeNode, EmbeddingPayload

try:
    from ariadne_graph.graphstores.neo4j import Neo4jGraphStore

    _HAS_NEO4J = True
except ImportError:
    _HAS_NEO4J = False


def _neo4j_available() -> bool:
    return _HAS_NEO4J and os.environ.get("ARIADNE_NEO4J_URI", "") != ""


pytestmark = pytest.mark.skipif(
    not _neo4j_available(),
    reason="Neo4j driver or ARIADNE_NEO4J_URI not configured",
)


@pytest.fixture
async def store() -> Neo4jGraphStore:
    uri = os.environ.get("ARIADNE_NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("ARIADNE_NEO4J_USER", "neo4j")
    password = os.environ.get("ARIADNE_NEO4J_PASSWORD", "password")
    s = Neo4jGraphStore(uri=uri, user=user, password=password)
    yield s
    await s.close()


@pytest.fixture
def sample_nodes() -> list[CodeNode]:
    return [
        CodeNode(
            id="mod.func",
            graph_id="g1",
            labels=["CodeFunction", "KnowledgeNode"],
            properties={"name": "func", "file_path": "/repo/mod.py", "qualname": "mod.func"},
        ),
        CodeNode(
            id="mod.cls",
            graph_id="g1",
            labels=["CodeClass", "KnowledgeNode"],
            properties={"name": "cls", "file_path": "/repo/mod.py"},
        ),
    ]


@pytest.fixture
def sample_edges() -> list[CodeEdge]:
    return [
        CodeEdge(
            source="mod.func",
            target="mod.cls",
            graph_id="g1",
            rel_type="CALLS",
            properties={"owner_file_path": "/repo/mod.py"},
        ),
    ]


async def _clean(store: Neo4jGraphStore) -> None:
    await store.delete_graph("g1")


async def test_add_nodes_and_query(store: Neo4jGraphStore, sample_nodes: list[CodeNode]) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes")
    assert len(rows) == 2


async def test_add_edges_and_query(
    store: Neo4jGraphStore, sample_nodes: list[CodeNode], sample_edges: list[CodeEdge]
) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_edges_batch("g1", sample_edges)
    rows = await store.query("g1", "edges")
    assert len(rows) == 1
    assert rows[0]["rel_type"] == "CALLS"


async def test_node_by_id(store: Neo4jGraphStore, sample_nodes: list[CodeNode]) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_by_id", {"node_id": "mod.func"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.func"


async def test_node_by_name(store: Neo4jGraphStore, sample_nodes: list[CodeNode]) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_by_name", {"name": "func"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.func"


async def test_edge_queries(
    store: Neo4jGraphStore, sample_nodes: list[CodeNode], sample_edges: list[CodeEdge]
) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_edges_batch("g1", sample_edges)

    out_edges = await store.query("g1", "node_outgoing_edges", {"node_id": "mod.func"})
    assert len(out_edges) == 1

    in_edges = await store.query("g1", "node_incoming_edges", {"node_id": "mod.cls"})
    assert len(in_edges) == 1


async def test_nodes_by_file(store: Neo4jGraphStore, sample_nodes: list[CodeNode]) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes_by_file", {"file_path": "/repo/mod.py"})
    assert len(rows) == 2


async def test_file_hashes_and_dirty_files(store: Neo4jGraphStore) -> None:
    await _clean(store)
    await store.update_hash("g1", "/repo/a.py", "hash_a")
    await store.update_hash("g1", "/repo/b.py", "hash_b")

    rows = await store.query("g1", "file_hashes")
    assert len(rows) == 2

    dirty = await store.query(
        "g1",
        "dirty_files",
        {"current_hashes": {"/repo/a.py": "hash_a", "/repo/b.py": "changed"}},
    )
    assert len(dirty) == 1
    assert dirty[0]["file_path"] == "/repo/b.py"


async def test_index_metadata(store: Neo4jGraphStore) -> None:
    await _clean(store)
    await store.query(
        "g1",
        "set_index_metadata",
        {"repo_path": "/repo", "last_indexed": "2024-01-01T00:00:00", "file_count": 7},
    )
    rows = await store.query("g1", "index_metadata")
    assert len(rows) == 1
    assert rows[0]["file_count"] == 7


async def test_delete_file_facts(
    store: Neo4jGraphStore, sample_nodes: list[CodeNode], sample_edges: list[CodeEdge]
) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_edges_batch("g1", sample_edges)
    await store.update_hash("g1", "/repo/mod.py", "hash")

    await store.delete_file_facts("g1", "/repo/mod.py")

    assert await store.query("g1", "nodes") == []
    assert await store.get_stored_hash("g1", "/repo/mod.py") is None


async def test_delete_graph(store: Neo4jGraphStore, sample_nodes: list[CodeNode]) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    await store.update_hash("g1", "/repo/mod.py", "hash")
    await store.register_project("g1", "/repo")

    await store.delete_graph("g1")

    assert await store.query("g1", "nodes") == []
    assert await store.query("g1", "file_hashes") == []


async def test_keyword_search(store: Neo4jGraphStore, sample_nodes: list[CodeNode]) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    hits = await store.search_keyword("g1", "func", limit=10)
    assert len(hits) >= 1


async def test_upsert_embeddings_and_vector_search(store: Neo4jGraphStore) -> None:
    await _clean(store)
    embedding = [1.0] + [0.0] * 383
    await store.upsert_embeddings(
        "g1",
        [EmbeddingPayload(node_id="mod.func", graph_id="g1", text="func", embedding=embedding)],
    )
    hits = await store.search_vector("g1", embedding, limit=10)
    assert len(hits) >= 1


async def test_communities(store: Neo4jGraphStore, sample_nodes: list[CodeNode]) -> None:
    await _clean(store)
    await store.add_nodes_batch("g1", sample_nodes)
    await store.set_communities("g1", {"mod.func": 1, "mod.cls": 1})
    groups = await store.get_communities("g1")
    assert groups[1] == ["mod.func", "mod.cls"]
