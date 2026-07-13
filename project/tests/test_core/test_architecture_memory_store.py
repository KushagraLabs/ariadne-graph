"""Architecture analysis over the MemoryGraphStore via the dep_edges capability.

Bead code_hygiene_mcp-420: architecture persistence / dependency matrix were
gated on ``isinstance(store, SQLiteGraphStore)``, so Memory (and Neo4j) silently
received NO hygiene findings. The dep-edge SOURCING now lives behind a
``GraphStore.dep_edges`` capability; ``analyze`` stays pure. These pin that the
Memory backend produces the SAME dependency_cycle findings SQLite would — which
also makes architecture analysis unit-testable without a SQLite fixture.

RED headline: a synthetic 2-cycle over a MemoryGraphStore yields
``dependency_cycle`` diagnostics (fails while Memory is isinstance-gated out).
"""

from __future__ import annotations

from ariadne_graph.core.architecture import (
    persist_architecture_diagnostics,
    read_dependency_matrix,
)
from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.lumen import LumenGraphStore
from ariadne_graph.graphstores.memory import MemoryGraphStore

GRAPH = "gmem"
ROOT = "/repo"


def _file_node(rel: str) -> CodeNode:
    abs_path = f"{ROOT}/{rel}"
    return CodeNode(
        id=abs_path,
        graph_id=GRAPH,
        labels=["CodeFile"],
        properties={"file_path": abs_path, "name": rel.rsplit("/", 1)[-1]},
    )


def _symbol_node(node_id: str, rel: str) -> CodeNode:
    """A symbol node living in ``rel`` — the file_path anchors the dep edge."""
    return CodeNode(
        id=node_id,
        graph_id=GRAPH,
        labels=["CodeSymbol"],
        properties={"file_path": f"{ROOT}/{rel}", "name": node_id},
    )


async def _seed_scip_cycle(store: MemoryGraphStore) -> None:
    """app/a.py <-> app/b.py via SCIP REFERENCES edges (definition-resolved).

    Mirrors the SCIP branch of ``_DEP_EDGE_SQL``: a REFERENCES edge between two
    symbols whose owning files differ is a cross-file file->file dependency.
    """
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.py"),
            _file_node("app/b.py"),
            _symbol_node("a.fn", "app/a.py"),
            _symbol_node("b.fn", "app/b.py"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(
                source="a.fn", target="b.fn", graph_id=GRAPH,
                rel_type="REFERENCES", properties={},
            ),
            CodeEdge(
                source="b.fn", target="a.fn", graph_id=GRAPH,
                rel_type="REFERENCES", properties={},
            ),
        ],
    )


async def _diagnostic_nodes(store: MemoryGraphStore) -> list[dict]:
    rows = await store.query(GRAPH, "nodes_by_label", {"label": "CodeDiagnostic"})
    return [dict(r.get("n", r)) for r in rows]


async def test_memory_store_detects_cycle_via_capability() -> None:
    """A 2-cycle over MemoryGraphStore -> dependency_cycle diagnostics."""
    store = MemoryGraphStore()
    await _seed_scip_cycle(store)

    written = await persist_architecture_diagnostics(store, GRAPH, ROOT)
    assert written >= 2

    diags = await _diagnostic_nodes(store)
    cyc = [d for d in diags if d["properties"].get("rule") == "dependency_cycle"]
    assert {d["properties"]["file_path"] for d in cyc} == {
        f"{ROOT}/app/a.py",
        f"{ROOT}/app/b.py",
    }


async def test_memory_store_dependency_matrix_via_capability() -> None:
    """The same cycle drives a non-empty dependency matrix over Memory."""
    store = MemoryGraphStore()
    await _seed_scip_cycle(store)

    matrix = await read_dependency_matrix(store, GRAPH, ROOT)
    assert {(e.source, e.target) for e in matrix.edges} == {
        ("app/a.py", "app/b.py"),
        ("app/b.py", "app/a.py"),
    }


async def test_memory_dep_edges_capability_matches_semantics() -> None:
    """The store-level capability returns abs (sf, tf) tuples, cross-file only."""
    store = MemoryGraphStore()
    await _seed_scip_cycle(store)

    edges = await store.dep_edges(GRAPH)
    assert set(edges) == {
        (f"{ROOT}/app/a.py", f"{ROOT}/app/b.py"),
        (f"{ROOT}/app/b.py", f"{ROOT}/app/a.py"),
    }


async def test_memory_supports_dep_edges_flag() -> None:
    assert MemoryGraphStore().supports_dep_edges is True


async def test_lumen_wrapper_forwards_dep_edges_capability() -> None:
    """A Lumen wrapper must NOT silently drop the delegate's dep_edges capability."""
    delegate = MemoryGraphStore()
    await _seed_scip_cycle(delegate)
    lumen = LumenGraphStore(delegate)

    assert lumen.supports_dep_edges is True
    assert set(await lumen.dep_edges(GRAPH)) == {
        (f"{ROOT}/app/a.py", f"{ROOT}/app/b.py"),
        (f"{ROOT}/app/b.py", f"{ROOT}/app/a.py"),
    }
    # And the full pass runs over the wrapper (cycles land as diagnostics).
    await persist_architecture_diagnostics(lumen, GRAPH, ROOT)
    cyc = [d for d in await _diagnostic_nodes(delegate)
           if d["properties"].get("rule") == "dependency_cycle"]
    assert {d["properties"]["file_path"] for d in cyc} == {
        f"{ROOT}/app/a.py", f"{ROOT}/app/b.py",
    }


async def test_lumen_wrapped_memory_reindex_clears_stale_diagnostics() -> None:
    """Idempotency holds through the Lumen wrapper (factory Memory-fallback path).

    A fixed cycle's stale findings must be cleared on re-index even when the pass
    runs over a Lumen-wrapped Memory delegate — remove_arch_diagnostics forwards.
    """
    delegate = MemoryGraphStore()
    await _seed_scip_cycle(delegate)
    lumen = LumenGraphStore(delegate)

    await persist_architecture_diagnostics(lumen, GRAPH, ROOT)
    assert [d for d in await _diagnostic_nodes(delegate)
            if d["properties"].get("rule") == "dependency_cycle"]

    # Break the cycle, re-run over the wrapper.
    delegate._edges = [
        e for e in delegate._edges if not (e.source == "b.fn" and e.target == "a.fn")
    ]
    await persist_architecture_diagnostics(lumen, GRAPH, ROOT)

    cyc = [d for d in await _diagnostic_nodes(delegate)
           if d["properties"].get("rule") == "dependency_cycle"]
    assert cyc == [], "stale diagnostics must clear through the Lumen wrapper too"


# --- hard cases: the exact _DEP_EDGE_SQL semantics that a fuzzy scan drops -----


def _import_node(node_id: str, src_rel: str, target_rel: str) -> CodeNode:
    return CodeNode(
        id=node_id,
        graph_id=GRAPH,
        labels=["CodeImport"],
        properties={
            "file_path": f"{ROOT}/{src_rel}",
            "resolved_source": f"{ROOT}/{target_rel}",
            "name": target_rel,
        },
    )


async def test_scip_covered_source_suppresses_its_imports_fallback() -> None:
    """A source file with a cross-file SCIP edge is served by branch 1 ONLY.

    a.py has a REFERENCES edge to b.py AND a redundant IMPORTS edge to c.py. The
    IMPORTS fallback must NOT fire for a.py (it is scip_covered), so a->c is
    dropped — mirrors the SQL's per-source-file gate (anti-double-count).
    """
    store = MemoryGraphStore()
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.py"),
            _file_node("app/b.py"),
            _file_node("app/c.py"),
            _symbol_node("a.sym", "app/a.py"),
            _symbol_node("b.sym", "app/b.py"),
            _import_node("imp_a_to_c", "app/a.py", "app/c.py"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source="a.sym", target="b.sym", graph_id=GRAPH,
                     rel_type="REFERENCES", properties={}),
            CodeEdge(source=f"{ROOT}/app/a.py", target="imp_a_to_c", graph_id=GRAPH,
                     rel_type="IMPORTS", properties={}),
        ],
    )

    edges = await store.dep_edges(GRAPH)
    assert edges == [(f"{ROOT}/app/a.py", f"{ROOT}/app/b.py")]
    assert (f"{ROOT}/app/a.py", f"{ROOT}/app/c.py") not in edges


async def test_imports_fallback_requires_indexed_target() -> None:
    """An IMPORTS edge whose resolved_source is NOT an indexed CodeFile is dropped.

    Guards against injecting a phantom dependency the matrix can't map (the SQL's
    ``indexed_files`` gate). b.py is unindexed -> a->b must not appear.
    """
    store = MemoryGraphStore()
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.py"),  # b.py deliberately NOT added as a CodeFile
            _import_node("imp_a_to_b", "app/a.py", "app/b.py"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source=f"{ROOT}/app/a.py", target="imp_a_to_b", graph_id=GRAPH,
                     rel_type="IMPORTS", properties={}),
        ],
    )
    assert await store.dep_edges(GRAPH) == []


async def test_scip_python_calls_resolve_but_plain_calls_do_not() -> None:
    """Branch 1 includes scip-python CALLS but NOT unresolved CALLS.

    Mirrors ``_scip_edge_predicate``: a CALLS edge counts only when
    ``resolved_by == 'scip-python'``.
    """
    store = MemoryGraphStore()
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.py"),
            _file_node("app/b.py"),
            _file_node("app/c.py"),
            _symbol_node("a.sym", "app/a.py"),
            _symbol_node("b.sym", "app/b.py"),
            _symbol_node("c.sym", "app/c.py"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source="a.sym", target="b.sym", graph_id=GRAPH,
                     rel_type="CALLS", properties={"resolved_by": "scip-python"}),
            CodeEdge(source="a.sym", target="c.sym", graph_id=GRAPH,
                     rel_type="CALLS", properties={}),  # unresolved -> dropped
        ],
    )
    edges = await store.dep_edges(GRAPH)
    assert edges == [(f"{ROOT}/app/a.py", f"{ROOT}/app/b.py")]


async def test_intrafile_reference_yields_no_dep_and_no_coverage() -> None:
    """An intra-file REFERENCES (sf == tf) produces no file->file edge.

    So the source file is NOT scip_covered and still falls back to its imports.
    """
    store = MemoryGraphStore()
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.py"),
            _file_node("app/b.py"),
            _symbol_node("a.one", "app/a.py"),
            _symbol_node("a.two", "app/a.py"),  # same file
            _import_node("imp_a_to_b", "app/a.py", "app/b.py"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source="a.one", target="a.two", graph_id=GRAPH,
                     rel_type="REFERENCES", properties={}),
            CodeEdge(source=f"{ROOT}/app/a.py", target="imp_a_to_b", graph_id=GRAPH,
                     rel_type="IMPORTS", properties={}),
        ],
    )
    # intra-file ref drops; a.py is not covered so the IMPORTS fallback fires.
    assert await store.dep_edges(GRAPH) == [(f"{ROOT}/app/a.py", f"{ROOT}/app/b.py")]


async def test_dep_edges_preserve_multiplicity() -> None:
    """UNION ALL, not UNION: two REFERENCES a->b yield TWO tuples (weight source)."""
    store = MemoryGraphStore()
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.py"),
            _file_node("app/b.py"),
            _symbol_node("a.one", "app/a.py"),
            _symbol_node("a.two", "app/a.py"),
            _symbol_node("b.sym", "app/b.py"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source="a.one", target="b.sym", graph_id=GRAPH,
                     rel_type="REFERENCES", properties={}),
            CodeEdge(source="a.two", target="b.sym", graph_id=GRAPH,
                     rel_type="REFERENCES", properties={}),
        ],
    )
    edges = await store.dep_edges(GRAPH)
    assert edges.count((f"{ROOT}/app/a.py", f"{ROOT}/app/b.py")) == 2


async def test_memory_reindex_clears_stale_diagnostics() -> None:
    """Idempotency: a fixed cycle's stale diagnostics are cleared on re-run."""
    store = MemoryGraphStore()
    await _seed_scip_cycle(store)
    await persist_architecture_diagnostics(store, GRAPH, ROOT)
    assert [d for d in await _diagnostic_nodes(store)
            if d["properties"].get("rule") == "dependency_cycle"]

    # Break the cycle: drop b->a. Re-run must remove the now-stale cycle findings.
    store._edges = [e for e in store._edges if not (e.source == "b.fn" and e.target == "a.fn")]
    await persist_architecture_diagnostics(store, GRAPH, ROOT)

    cyc = [d for d in await _diagnostic_nodes(store)
           if d["properties"].get("rule") == "dependency_cycle"]
    assert cyc == [], "stale cycle diagnostics must be cleared on re-index"
