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


async def test_add_nodes_batch_merges_labels_and_properties_on_conflict(
    store: SQLiteGraphStore,
) -> None:
    """A bare import stub sharing an id with a rich CodeFile must not clobber it.

    SCIP translate emits a bare CodeImport stub whose id equals a real CodeFile's
    node id. Blind INSERT OR REPLACE strips the CodeFile label + file properties.
    On conflict we must UNION labels and keep the richer node's properties.
    """
    rich = CodeNode(
        id="pkg.mod",
        graph_id="g1",
        labels=["CodeFile", "CodeModule", "KnowledgeNode"],
        properties={
            "name": "mod",
            "file_path": "/repo/pkg/mod.py",
            "qualname": "pkg.mod",
            "loc": 120,
        },
    )
    stub = CodeNode(
        id="pkg.mod",
        graph_id="g1",
        labels=["CodeImport"],
        properties={"name": "mod"},
    )

    await store.add_nodes_batch("g1", [rich])
    await store.add_nodes_batch("g1", [stub])

    rows = await store.query("g1", "node_by_id", {"node_id": "pkg.mod"})
    assert len(rows) == 1
    node = rows[0]["n"]

    # Labels are the UNION; the CodeFile/CodeModule anchor survives.
    assert set(node["labels"]) == {
        "CodeFile",
        "CodeModule",
        "KnowledgeNode",
        "CodeImport",
    }
    # Rich file-level properties are retained.
    assert node["properties"]["file_path"] == "/repo/pkg/mod.py"
    assert node["properties"]["qualname"] == "pkg.mod"
    assert node["properties"]["loc"] == 120

    # The file anchor is queryable by file (the audit skip regression).
    by_file = await store.query("g1", "nodes_by_file", {"file_path": "/repo/pkg/mod.py"})
    assert any(r["n"]["id"] == "pkg.mod" for r in by_file)


async def test_add_nodes_batch_merges_same_batch_collision(
    store: SQLiteGraphStore,
) -> None:
    """The fresh-index case: rich node + bare stub arrive in ONE batch call.

    On a fresh index neither node exists in SQLite yet, so merging only against
    stored rows leaves them both unmerged and INSERT OR REPLACE lets the later
    stub clobber the rich node. The batch must be folded by id in-memory too.
    Order-independent: the stub must not win whether it comes before or after.
    """
    rich = CodeNode(
        id="pkg.mod",
        graph_id="g1",
        labels=["CodeFile", "CodeModule", "KnowledgeNode"],
        properties={"name": "mod", "file_path": "/repo/pkg/mod.py", "loc": 120},
    )
    stub = CodeNode(
        id="pkg.mod",
        graph_id="g1",
        labels=["CodeImport"],
        properties={"name": "mod"},
    )

    # stub AFTER rich (the order SCIP translate emits) in a single batch.
    await store.add_nodes_batch("g1", [rich, stub])
    node = (await store.query("g1", "node_by_id", {"node_id": "pkg.mod"}))[0]["n"]
    assert set(node["labels"]) == {"CodeFile", "CodeModule", "KnowledgeNode", "CodeImport"}
    assert node["properties"]["file_path"] == "/repo/pkg/mod.py"
    assert node["properties"]["loc"] == 120

    # And the reverse order (stub BEFORE rich) must yield the same merged result.
    await store.add_nodes_batch(
        "g2",
        [stub.model_copy(update={"graph_id": "g2"}), rich.model_copy(update={"graph_id": "g2"})],
    )
    node2 = (await store.query("g2", "node_by_id", {"node_id": "pkg.mod"}))[0]["n"]
    assert set(node2["labels"]) == {"CodeFile", "CodeModule", "KnowledgeNode", "CodeImport"}
    assert node2["properties"]["file_path"] == "/repo/pkg/mod.py"


async def test_add_nodes_batch_update_removes_obsolete_properties(
    store: SQLiteGraphStore,
) -> None:
    """A legitimate re-index of the SAME logical node must REPLACE, not merge.

    The stub/anchor merge is narrow: it must not turn every conflict into a
    union. Updating a node from {name:old, obsolete:true} to {name:new} must
    drop ``obsolete`` and update ``name`` -- normal upsert semantics.
    """
    v1 = CodeNode(
        id="mod.func",
        graph_id="g1",
        labels=["CodeFunction", "KnowledgeNode"],
        properties={"name": "old", "obsolete": True, "file_path": "/repo/mod.py"},
    )
    v2 = CodeNode(
        id="mod.func",
        graph_id="g1",
        labels=["CodeFunction", "KnowledgeNode"],
        properties={"name": "new", "file_path": "/repo/mod.py"},
    )
    await store.add_nodes_batch("g1", [v1])
    await store.add_nodes_batch("g1", [v2])

    node = (await store.query("g1", "node_by_id", {"node_id": "mod.func"}))[0]["n"]
    assert node["properties"]["name"] == "new", "update did not overwrite name"
    assert "obsolete" not in node["properties"], "obsolete property survived a replace"


async def test_add_nodes_batch_reindex_is_idempotent(
    store: SQLiteGraphStore, sample_nodes: list[CodeNode]
) -> None:
    """Re-indexing an unchanged node must not duplicate labels or grow the row."""
    await store.add_nodes_batch("g1", sample_nodes)
    await store.add_nodes_batch("g1", sample_nodes)

    rows = await store.query("g1", "node_by_id", {"node_id": "mod.func"})
    assert len(rows) == 1
    labels = rows[0]["n"]["labels"]
    assert labels == ["CodeFunction", "KnowledgeNode"]
    assert len(labels) == len(set(labels))

    all_rows = await store.query("g1", "nodes")
    assert len(all_rows) == 2


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


async def test_scip_translated_file_is_deletable_by_absolute_path(
    store: SQLiteGraphStore,
) -> None:
    """Regression: SCIP CodeFile nodes must be removable via delete_file_facts.

    incremental_sync calls delete_file_facts with str(path.resolve()) — an
    ABSOLUTE path. Previously the SCIP translator stored a repo-relative
    file_path, so the delete found nothing and stale nodes leaked on every
    re-sync. This drives a real SCIP delta through the store and re-syncs it.
    """
    from pathlib import Path

    from ariadne_graph.languages.typescript.scip_parser import ScipIndexParser
    from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator

    repo_root = Path(__file__).parent.parent / "fixtures" / "ts_scip_project"
    index = ScipIndexParser().parse(repo_root / "index.scip")
    translator = ScipGraphTranslator(repo_root=repo_root, graph_id="g1")
    doc = index.documents[Path("src/dog.ts")]
    delta = translator.translate(doc)
    await store.add_nodes_batch("g1", delta.nodes)

    abs_path = str((repo_root / "src" / "dog.ts").resolve())
    before = await store.query("g1", "nodes_by_label", {"label": "CodeFile"})
    assert before, "expected the SCIP CodeFile to be stored"

    # incremental_sync deletes by the absolute resolved path.
    await store.delete_file_facts("g1", abs_path)

    after = await store.query("g1", "nodes_by_label", {"label": "CodeFile"})
    assert after == [], "SCIP CodeFile nodes leaked — not deleted by absolute key"


async def test_delete_file_facts_preserves_cross_file_edge(
    store: SQLiteGraphStore,
) -> None:
    """A cross-file edge owned by ``a.py`` must survive a reindex of ``b.py``.

    Regression: once call resolution targets the real callee node (``b.save``)
    instead of a bare name, deleting edges by endpoint membership wrongly
    removed the caller-owned edge when only the callee file was reindexed.
    Deletion is now keyed on ``owner_file_path``.
    """
    await store.add_nodes_batch(
        "g1",
        [
            CodeNode(
                id="a.caller",
                graph_id="g1",
                labels=["CodeFunction", "KnowledgeNode"],
                properties={"name": "caller", "file_path": "/repo/a.py"},
            ),
            CodeNode(
                id="b.save",
                graph_id="g1",
                labels=["CodeFunction", "KnowledgeNode"],
                properties={"name": "save", "file_path": "/repo/b.py"},
            ),
        ],
    )
    await store.add_edges_batch(
        "g1",
        [
            CodeEdge(
                source="a.caller",
                target="b.save",
                graph_id="g1",
                rel_type="CALLS",
                properties={"owner_file_path": "/repo/a.py"},
            )
        ],
    )

    # Reindex only the callee file: the caller-owned edge must remain.
    await store.delete_file_facts("g1", "/repo/b.py")
    calls = [r for r in await store.query("g1", "edges") if r["r"]["rel_type"] == "CALLS"]
    assert len(calls) == 1
    assert calls[0]["r"]["target"] == "b.save"

    # Reindexing the owner file removes it.
    await store.delete_file_facts("g1", "/repo/a.py")
    assert not [r for r in await store.query("g1", "edges") if r["r"]["rel_type"] == "CALLS"]


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
            [EmbeddingPayload(node_id="node.a", graph_id="g1", text="alpha", embedding=embedding)],
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
        conn.execute("CREATE VIRTUAL TABLE node_fts USING fts5(content, node_id, graph_id)")
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
