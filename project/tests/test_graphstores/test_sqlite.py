"""Unit tests for SQLiteGraphStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.models import CodeEdge, CodeNode, EmbeddingPayload
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteGraphStore:
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    try:
        yield store
    finally:
        await store.close()


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


async def test_add_nodes_and_query(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes")
    assert len(rows) == 2
    ids = {r["n"]["id"] for r in rows}
    assert ids == {"mod.func", "mod.cls"}


async def test_add_edges_and_query(store: SQLiteGraphStore, sample_edges: list[CodeEdge]) -> None:
    await store.add_edges_batch("g1", sample_edges)
    rows = await store.query("g1", "edges")
    assert len(rows) == 1
    assert rows[0]["r"]["rel_type"] == "CALLS"


async def test_node_by_id(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_by_id", {"node_id": "mod.func"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.func"


async def test_node_by_name(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_by_name", {"name": "func"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.func"


async def test_node_name_fuzzy(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "node_name_fuzzy", {"name": "func"})
    assert any(r["n"]["id"] == "mod.func" for r in rows)


async def test_edge_queries(
    store: SQLiteGraphStore, sample_nodes: list[CodeNode], sample_edges: list[CodeEdge]
) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_edges_batch("g1", sample_edges)

    out_edges = await store.query("g1", "node_outgoing_edges", {"node_id": "mod.func"})
    assert len(out_edges) == 1

    in_edges = await store.query("g1", "node_incoming_edges", {"node_id": "mod.cls"})
    assert len(in_edges) == 1

    all_edges = await store.query("g1", "node_all_edges", {"node_id": "mod.func"})
    assert len(all_edges) == 1


async def test_nodes_by_label(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes_by_label", {"label": "CodeClass"})
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod.cls"


async def test_nodes_by_file(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "nodes_by_file", {"file_path": "/repo/mod.py"})
    assert len(rows) == 2


async def test_file_hashes_and_dirty_files(store: SQLiteGraphStore) -> None:
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


async def test_index_metadata(store: SQLiteGraphStore) -> None:
    await store.query(
        "g1",
        "set_index_metadata",
        {"repo_path": "/repo", "last_indexed": "2024-01-01T00:00:00", "file_count": 7},
    )
    rows = await store.query("g1", "index_metadata")
    assert len(rows) == 1
    assert rows[0]["file_count"] == 7
    assert rows[0]["repo_path"] == "/repo"


async def test_count_files(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    rows = await store.query("g1", "count_files")
    assert rows[0]["count"] == 1


async def test_delete_file_facts(
    store: SQLiteGraphStore, sample_nodes: list[CodeNode], sample_edges: list[CodeEdge]
) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_edges_batch("g1", sample_edges)
    await store.update_hash("g1", "/repo/mod.py", "hash")

    await store.delete_file_facts("g1", "/repo/mod.py")

    assert await store.query("g1", "nodes") == []
    assert await store.query("g1", "edges") == []
    assert await store.get_stored_hash("g1", "/repo/mod.py") is None


async def test_delete_graph(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    await store.update_hash("g1", "/repo/mod.py", "hash")
    await store.register_project("g1", "/repo")

    await store.delete_graph("g1")

    assert await store.query("g1", "nodes") == []
    assert await store.query("g1", "file_hashes") == []
    assert await store.list_projects() == []


async def test_keyword_search(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    hits = await store.search_keyword("g1", "func", limit=10)
    assert len(hits) >= 1
    assert any(h.node_id == "mod.func" for h in hits)


async def test_upsert_embeddings_and_vector_search(store: SQLiteGraphStore) -> None:
    embedding = [1.0] + [0.0] * 383
    await store.upsert_embeddings(
        "g1",
        [EmbeddingPayload(node_id="mod.func", graph_id="g1", text="func", embedding=embedding)],
    )
    hits = await store.search_vector("g1", embedding, limit=10)
    assert len(hits) >= 1
    assert hits[0].node_id == "mod.func"


async def test_vector_search_fallback_no_deadlock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the sqlite-vec query fails, search_vector falls back without deadlocking."""
    import ariadne_graph.graphstores.sqlite as sqlite_mod

    # Prevent vec_items from being created, but force the store to try the vec path.
    monkeypatch.setattr(sqlite_mod, "_HAS_SQLITE_VEC", False)
    db_path = tmp_path / "vec_fallback.db"
    store = SQLiteGraphStore(str(db_path), embedding_dimensions=4)
    try:
        embedding = [1.0, 0.0, 0.0, 0.0]
        await store.upsert_embeddings(
            "g1",
            [
                EmbeddingPayload(
                    node_id="node.a", graph_id="g1", text="alpha", embedding=embedding
                )
            ],
        )
        store._has_sqlite_vec = True
        hits = await store.search_vector("g1", embedding, limit=5)
        assert len(hits) == 1
        assert hits[0].node_id == "node.a"
        assert store._has_sqlite_vec is False
    finally:
        await store.close()


async def test_communities(store: SQLiteGraphStore, sample_nodes: list[CodeNode]) -> None:
    await store.add_nodes_batch("g1", sample_nodes)
    await store.set_communities("g1", {"mod.func": 1, "mod.cls": 1})
    groups = await store.get_communities("g1")
    assert set(groups[1]) == {"mod.func", "mod.cls"}


async def test_upsert_embeddings_custom_dimension(tmp_path: pytest.TempPathFactory) -> None:
    """SQLiteGraphStore honours a non-default embedding dimension."""
    db_path = tmp_path / "custom_dim.db"  # type: ignore[operator]
    store = SQLiteGraphStore(str(db_path), embedding_dimensions=8)
    embedding = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    await store.upsert_embeddings(
        "g1",
        [EmbeddingPayload(node_id="mod.func", graph_id="g1", text="func", embedding=embedding)],
    )
    hits = await store.search_vector("g1", embedding, limit=10)
    assert len(hits) >= 1
    assert hits[0].node_id == "mod.func"


async def test_legacy_fts_migration(tmp_path: Path) -> None:
    """A legacy node_fts table is migrated to rowid-keyed external content."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Pre-seed a database with the old non-external-content FTS5 schema.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE nodes (id TEXT, graph_id TEXT, labels TEXT, properties TEXT)")
        conn.execute(
            "CREATE VIRTUAL TABLE node_fts USING fts5(content, node_id, graph_id)"
        )
        conn.execute(
            "INSERT INTO node_fts (content, node_id, graph_id) VALUES (?, ?, ?)",
            ("legacy symbol content", "legacy.node", "g1"),
        )
        conn.commit()
    finally:
        conn.close()

    store = SQLiteGraphStore(str(db_path))
    try:
        hits = await store.search_keyword("g1", "legacy", limit=10)
        assert any(h.node_id == "legacy.node" for h in hits)
    finally:
        await store.close()
