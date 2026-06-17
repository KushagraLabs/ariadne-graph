"""Unit tests for MemoryGraphStore."""

from __future__ import annotations

import pytest

from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.memory import MemoryGraphStore


@pytest.fixture
def store() -> MemoryGraphStore:
    return MemoryGraphStore()


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


async def test_add_nodes_and_query(store: MemoryGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes")
    assert len(rows) == 2
    ids = {r["n"]["id"] for r in rows}
    assert ids == {"mod.func", "mod.cls"}


async def test_add_edges_and_query(store: MemoryGraphStore, sample_edges: list[CodeEdge]) -> None:
    await store.add_edges_batch("g1", sample_edges)
    rows = await store.query("g1", "edges")
    assert len(rows) == 1
    assert rows[0]["r"]["rel_type"] == "CALLS"


async def test_node_by_id(store: MemoryGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_by_id", {"node_id": "mod.func"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.func"


async def test_node_by_name(store: MemoryGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_by_name", {"name": "func"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.func"


async def test_node_name_fuzzy(store: MemoryGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_name_fuzzy", {"name": "func"})
    assert any(r["n"]["id"] == "mod.func" for r in rows)


async def test_edge_queries(store: MemoryGraphStore, sample_nodes: list[CodeNode], sample_edges: list[CodeEdge]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_edges_batch("g1", sample_edges)

    out_edges = await store.query("g1", "node_outgoing_edges", {"node_id": "mod.func"})
    assert len(out_edges) == 1

    in_edges = await store.query("g1", "node_incoming_edges", {"node_id": "mod.cls"})
    assert len(in_edges) == 1

    all_edges = await store.query("g1", "node_all_edges", {"node_id": "mod.func"})
    assert len(all_edges) == 1


async def test_nodes_by_label(store: MemoryGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes_by_label", {"label": "CodeClass"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.cls"


async def test_nodes_by_file(store: MemoryGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes_by_file", {"file_path": "/repo/mod.py"})
    assert len(rows) == 2


async def test_file_hashes_and_dirty_files(store: MemoryGraphStore) -> None:
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


async def test_index_metadata(store: MemoryGraphStore) -> None:
    await store.register_project("g1", "/repo", file_count=7)
    rows = await store.query("g1", "index_metadata")
    assert len(rows) == 1
    assert rows[0]["file_count"] == 7
    assert rows[0]["repo_path"] == "/repo"


async def test_delete_file_facts(
    store: MemoryGraphStore, sample_nodes: list[CodeNode], sample_edges: list[CodeEdge]
) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_edges_batch("g1", sample_edges)
    await store.update_hash("g1", "/repo/mod.py", "hash")

    await store.delete_file_facts("g1", "/repo/mod.py")

    assert await store.query("g1", "nodes") == []
    assert await store.query("g1", "edges") == []
    assert await store.get_stored_hash("g1", "/repo/mod.py") is None


async def test_delete_graph(store: MemoryGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    await store.update_hash("g1", "/repo/mod.py", "hash")
    await store.register_project("g1", "/repo")

    await store.delete_graph("g1")

    assert await store.query("g1", "nodes") == []
    assert await store.query("g1", "file_hashes") == []
    assert await store.list_projects() == []
