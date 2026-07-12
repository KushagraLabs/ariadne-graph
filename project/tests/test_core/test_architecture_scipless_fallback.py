"""SCIP-less IMPORTS-edge dependency fallback + resolution provenance (bead 1u5).

Without scip-typescript the tree-sitter TS extractor emits ZERO cross-file
CALLS/REFERENCES, so the file->file dep SSOT (``_DEP_EDGE_SQL``) sees nothing and
architecture/cycles/matrix silently go dark. But tree-sitter DOES emit IMPORTS
edges to CodeImport nodes carrying a tsconfig-resolved ``resolved_source`` — a
real file->file dependency. This suite pins:

- RED: a two-file TS import cycle with NO SCIP is detected via the IMPORTS
  fallback (cycle diagnostic + non-empty matrix), and coverage provenance
  reports imports-level resolution.
- Anti-double-count: WITH SCIP REFERENCES present, edge counts are UNCHANGED vs
  today (equality assertion) — the fallback contributes nothing for a source
  file that already has outgoing REFERENCES.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.architecture import (
    persist_architecture_diagnostics,
    read_dependency_matrix,
    read_resolution_coverage,
)
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
    abs_path = f"{ROOT}/{rel}"
    return CodeNode(
        id=abs_path,
        graph_id=GRAPH,
        labels=["CodeFile"],
        properties={"file_path": abs_path, "name": rel.rsplit("/", 1)[-1]},
    )


def _module_node(rel: str) -> CodeNode:
    """A CodeModule node whose file_path is its own source file (extractor convention)."""
    abs_path = f"{ROOT}/{rel}"
    return CodeNode(
        id=f"mod::{abs_path}",
        graph_id=GRAPH,
        labels=["CodeModule"],
        properties={"file_path": abs_path, "name": rel},
    )


def _import_node(node_id: str, src_rel: str, target_rel: str) -> CodeNode:
    """A CodeImport node: lives in ``src_rel`` (file_path), resolves to ``target_rel``."""
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


async def _seed_ts_imports_cycle(store: SQLiteGraphStore) -> None:
    """a.ts imports b.ts, b.ts imports a.ts — tree-sitter only, NO SCIP REFERENCES.

    Mirrors the real extractor shape: an IMPORTS edge from the source module to a
    CodeImport node that carries ``resolved_source`` = the target file.
    """
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.ts"),
            _file_node("app/b.ts"),
            _module_node("app/a.ts"),
            _module_node("app/b.ts"),
            _import_node("imp_a_to_b", "app/a.ts", "app/b.ts"),
            _import_node("imp_b_to_a", "app/b.ts", "app/a.ts"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(
                source=f"mod::{ROOT}/app/a.ts",
                target="imp_a_to_b",
                graph_id=GRAPH,
                rel_type="IMPORTS",
                properties={},
            ),
            CodeEdge(
                source=f"mod::{ROOT}/app/b.ts",
                target="imp_b_to_a",
                graph_id=GRAPH,
                rel_type="IMPORTS",
                properties={},
            ),
        ],
    )


async def _diagnostic_nodes(store: SQLiteGraphStore) -> list[dict]:
    rows = await store.query(GRAPH, "nodes_by_label", {"label": "CodeDiagnostic"})
    return [dict(r.get("n", r)) for r in rows]


async def test_scipless_ts_cycle_detected_via_imports_fallback(store: SQLiteGraphStore) -> None:
    """Two-file TS import cycle with NO SCIP -> cycle diagnostic + non-empty matrix."""
    await _seed_ts_imports_cycle(store)

    await persist_architecture_diagnostics(store, GRAPH, ROOT)

    diags = await _diagnostic_nodes(store)
    cyc = [d for d in diags if d["properties"].get("rule") == "dependency_cycle"]
    assert {d["properties"]["file_path"] for d in cyc} == {
        f"{ROOT}/app/a.ts",
        f"{ROOT}/app/b.ts",
    }

    matrix = await read_dependency_matrix(store, GRAPH, ROOT)
    assert matrix.edges, "matrix must be non-empty from IMPORTS fallback"
    assert {(e.source, e.target) for e in matrix.edges} == {
        ("app/a.ts", "app/b.ts"),
        ("app/b.ts", "app/a.ts"),
    }


async def test_scipless_coverage_reports_imports_resolution(store: SQLiteGraphStore) -> None:
    """Provenance: files resolved only via IMPORTS are reported imports-only."""
    await _seed_ts_imports_cycle(store)

    coverage = await read_resolution_coverage(store, GRAPH)
    assert coverage["references_files"] == 0
    assert coverage["imports_only_files"] == 2
    assert coverage["resolution"] == "tree-sitter-imports"


async def _seed_scip_refs(store: SQLiteGraphStore) -> None:
    """a.ts -> b.ts via a SCIP REFERENCES edge, PLUS a redundant IMPORTS edge.

    The IMPORTS edge from a.ts must NOT add a second file->file dep: a.ts already
    has an outgoing REFERENCES, so REFERENCES subsume its imports.
    """
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.ts"),
            _file_node("app/b.ts"),
            _symbol_node("sym_a", "app/a.ts"),
            _symbol_node("sym_b", "app/b.ts"),
            _module_node("app/a.ts"),
            _import_node("imp_a_to_b", "app/a.ts", "app/b.ts"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(
                source="sym_a", target="sym_b", graph_id=GRAPH, rel_type="REFERENCES", properties={}
            ),
            CodeEdge(
                source=f"mod::{ROOT}/app/a.ts",
                target="imp_a_to_b",
                graph_id=GRAPH,
                rel_type="IMPORTS",
                properties={},
            ),
        ],
    )


def _symbol_node(node_id: str, rel: str) -> CodeNode:
    abs_path = f"{ROOT}/{rel}"
    return CodeNode(
        id=node_id,
        graph_id=GRAPH,
        labels=["CodeFunction"],
        properties={"file_path": abs_path, "name": node_id},
    )


async def test_with_scip_no_double_count(store: SQLiteGraphStore) -> None:
    """Equality guard: a source file with outgoing REFERENCES gets NO imports fallback.

    a.ts -> b.ts appears once (from REFERENCES); the redundant IMPORTS edge from
    the SAME source file must be suppressed, so exactly ONE dep edge exists.
    """
    await _seed_scip_refs(store)

    matrix = await read_dependency_matrix(store, GRAPH, ROOT)
    dep_edges = {(e.source, e.target): e.import_count for e in matrix.edges}
    assert dep_edges == {("app/a.ts", "app/b.ts"): 1}


async def test_intra_file_reference_does_not_suppress_import_fallback(
    store: SQLiteGraphStore,
) -> None:
    """Hard case: a file with only an INTRA-file REFERENCES still falls back to its
    cross-file imports. An intra-file reference yields no file->file dep edge, so
    it must NOT be treated as SCIP coverage that suppresses the import fallback.

    a.ts has a local a->a REFERENCES (intra-file) and imports b.ts. The a->b
    dependency must survive via the IMPORTS fallback.
    """
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.ts"),
            _file_node("app/b.ts"),
            _symbol_node("sym_a1", "app/a.ts"),
            _symbol_node("sym_a2", "app/a.ts"),
            _module_node("app/a.ts"),
            _import_node("imp_a_to_b", "app/a.ts", "app/b.ts"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            # intra-file reference (a.ts -> a.ts): contributes no file->file dep
            CodeEdge(source="sym_a1", target="sym_a2", graph_id=GRAPH,
                     rel_type="REFERENCES", properties={}),
            CodeEdge(source=f"mod::{ROOT}/app/a.ts", target="imp_a_to_b",
                     graph_id=GRAPH, rel_type="IMPORTS", properties={}),
        ],
    )

    matrix = await read_dependency_matrix(store, GRAPH, ROOT)
    dep_edges = {(e.source, e.target) for e in matrix.edges}
    assert ("app/a.ts", "app/b.ts") in dep_edges, (
        "cross-file import lost because an intra-file reference wrongly gated the fallback"
    )

    coverage = await read_resolution_coverage(store, GRAPH)
    # a.ts is imports-resolved (its only cross-file dep is the fallback), and has
    # no cross-file SCIP edge, so it is NOT counted as references-covered.
    assert coverage["references_files"] == 0
    assert coverage["imports_only_files"] == 1
    assert coverage["resolution"] == "tree-sitter-imports"


async def test_scip_multiplicity_preserved(store: SQLiteGraphStore) -> None:
    """Regression guard: TWO REFERENCES between the same files -> import_count == 2.

    The fallback's cross-branch de-dup must NOT collapse SCIP occurrence count —
    dependency_matrix derives import_count from row multiplicity. A UNION (vs
    UNION ALL) would silently undercount this to 1.
    """
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.ts"),
            _file_node("app/b.ts"),
            _symbol_node("sym_a1", "app/a.ts"),
            _symbol_node("sym_a2", "app/a.ts"),
            _symbol_node("sym_b", "app/b.ts"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source="sym_a1", target="sym_b", graph_id=GRAPH,
                     rel_type="REFERENCES", properties={}),
            CodeEdge(source="sym_a2", target="sym_b", graph_id=GRAPH,
                     rel_type="REFERENCES", properties={}),
        ],
    )

    matrix = await read_dependency_matrix(store, GRAPH, ROOT)
    dep_edges = {(e.source, e.target): e.import_count for e in matrix.edges}
    assert dep_edges == {("app/a.ts", "app/b.ts"): 2}


async def test_imports_fallback_skips_phantom_target(store: SQLiteGraphStore) -> None:
    """An IMPORTS edge whose resolved_source is NOT an indexed CodeFile yields NO
    dep edge — TsConfigResolver returns intended paths even for absent files, so
    an unvalidated target would inject a phantom dependency.
    """
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.ts"),  # b.ts deliberately NOT indexed
            _module_node("app/a.ts"),
            _import_node("imp_a_to_ghost", "app/a.ts", "app/ghost.ts"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source=f"mod::{ROOT}/app/a.ts", target="imp_a_to_ghost",
                     graph_id=GRAPH, rel_type="IMPORTS", properties={}),
        ],
    )

    matrix = await read_dependency_matrix(store, GRAPH, ROOT)
    assert matrix.edges == []


async def test_coverage_ignores_phantom_import_target(store: SQLiteGraphStore) -> None:
    """Provenance parity: an import to an UNINDEXED target produces no dep edge, so
    it must NOT count as imports_only. Coverage stays 'none', matching the empty
    dependency output rather than falsely claiming 'tree-sitter-imports'.
    """
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("app/a.ts"),  # ghost.ts NOT indexed
            _module_node("app/a.ts"),
            _import_node("imp_a_to_ghost", "app/a.ts", "app/ghost.ts"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source=f"mod::{ROOT}/app/a.ts", target="imp_a_to_ghost",
                     graph_id=GRAPH, rel_type="IMPORTS", properties={}),
        ],
    )

    coverage = await read_resolution_coverage(store, GRAPH)
    assert coverage["imports_only_files"] == 0
    assert coverage["resolution"] == "none"


async def _seed_python_scip_calls(store: SQLiteGraphStore) -> None:
    """train.py -> model.py via a SCIP-resolved Python CALLS edge (no REFERENCES)."""
    await store.add_nodes_batch(
        GRAPH,
        [
            _file_node("src/train.py"),
            _file_node("src/model.py"),
            _symbol_node("caller", "src/train.py"),
            _symbol_node("callee", "src/model.py"),
        ],
    )
    await store.add_edges_batch(
        GRAPH,
        [
            CodeEdge(source="caller", target="callee", graph_id=GRAPH,
                     rel_type="CALLS", properties={"resolved_by": "scip-python"}),
        ],
    )


async def test_python_scip_calls_report_scip_resolution(store: SQLiteGraphStore) -> None:
    """Provenance: a Python graph resolved via scip-python CALLS reports 'scip',
    not 'none' — coverage must credit SCIP CALLS, not only REFERENCES.
    """
    await _seed_python_scip_calls(store)

    coverage = await read_resolution_coverage(store, GRAPH)
    assert coverage["references_files"] == 1  # train.py has a SCIP dep edge
    assert coverage["imports_only_files"] == 0
    assert coverage["resolution"] == "scip"
