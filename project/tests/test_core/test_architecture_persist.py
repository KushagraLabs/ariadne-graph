"""Persistence of architecture findings into the graph store.

``persist_architecture_diagnostics`` reads resolved CodeFile nodes + file->file
dep edges from the store, runs the pure ``analyze``, and writes CodeDiagnostic
nodes (with HAS_DIAGNOSTIC edges) — the same node kind the extractors emit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.architecture import persist_architecture_diagnostics
from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore

GRAPH = "g1"
ROOT = "/repo"


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteGraphStore:
    s = SQLiteGraphStore(str(tmp_path / "graph.db"))
    try:
        yield s
    finally:
        await s.close()


def _file_node(rel: str) -> CodeNode:
    """A CodeFile node whose id is the absolute path (matches extractor convention)."""
    abs_path = f"{ROOT}/{rel}"
    return CodeNode(
        id=abs_path,
        graph_id=GRAPH,
        labels=["CodeFile"],
        properties={"file_path": abs_path, "name": rel.rsplit("/", 1)[-1]},
    )


def _symbol(node_id: str, rel: str) -> CodeNode:
    abs_path = f"{ROOT}/{rel}"
    return CodeNode(
        id=node_id,
        graph_id=GRAPH,
        labels=["CodeFunction"],
        properties={"file_path": abs_path, "name": node_id},
    )


async def _seed_cycle(store: SQLiteGraphStore) -> None:
    """a -> b -> a: a real resolved import ring across two files."""
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("pkg/a.py"), _file_node("pkg/b.py"),
            _symbol("fa", "pkg/a.py"), _symbol("fb", "pkg/b.py"),
        ],
    )
    # SCIP-resolved Python CALLS edges (the shape _XREF_SQL keys off).
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source="fa", target="fb", graph_id=GRAPH, rel_type="CALLS",
                     properties={"resolved_by": "scip-python"}),
            CodeEdge(source="fb", target="fa", graph_id=GRAPH, rel_type="CALLS",
                     properties={"resolved_by": "scip-python"}),
        ],
    )


async def _diagnostic_nodes(store: SQLiteGraphStore) -> list[dict]:
    rows = await store.query(GRAPH, "nodes_by_label", {"label": "CodeDiagnostic"})
    return [dict(r.get("n", r)) for r in rows]


async def test_persists_cycle_diagnostics_from_store(store: SQLiteGraphStore) -> None:
    await _seed_cycle(store)

    await persist_architecture_diagnostics(store, GRAPH, ROOT)

    diags = await _diagnostic_nodes(store)
    cyc = [d for d in diags if d["properties"].get("rule") == "dependency_cycle"]
    # Two files in the ring => two file-level findings, attached by abs file_path.
    assert {d["properties"]["file_path"] for d in cyc} == {
        f"{ROOT}/pkg/a.py", f"{ROOT}/pkg/b.py",
    }


async def test_reindex_does_not_duplicate_diagnostics(store: SQLiteGraphStore) -> None:
    await _seed_cycle(store)

    await persist_architecture_diagnostics(store, GRAPH, ROOT)
    await persist_architecture_diagnostics(store, GRAPH, ROOT)  # re-run

    diags = await _diagnostic_nodes(store)
    cyc = [d for d in diags if d["properties"].get("rule") == "dependency_cycle"]
    assert len(cyc) == 2  # not 4 — stale findings replaced, not appended
