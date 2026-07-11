"""Every file that yields symbols keeps its CodeFile node (bead hgm).

When file B imports a symbol that file A owns, the translator emits a bare
``CodeImport`` stub for that symbol *inside B's delta* — with the SAME node id
as A's real node. On persist (``add_nodes_batch`` uses ``INSERT OR REPLACE``,
last-writer-wins by id, no label union) the stub clobbers A's
``CodeFile``/``CodeModule`` labels, so ~30% of files end up with symbol nodes
but no ``CodeFile`` node and file-scoped queries skip them.

The stub is NOT safe to drop at the translator level — the live SCIP/Tree-sitter
boundary (``test_scip_live_mixed_id``) *requires* the symbol-string stub node to
exist so ``GraphRetriever._canonicalize_neighbor`` can redirect it to the
concrete same-named node. The fix (bead 6c7) is label union on upsert in
``graphstores/sqlite.py::add_nodes_batch``: on the stub-vs-anchor id collision it
unions labels and keeps the richer node's properties instead of clobbering. This
test drives that REAL persistence path (add_nodes_batch + query), not a dict
simulation, so it validates the fix end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.typescript.scip_parser import (
    ScipDocument,
    ScipOccurrence,
    ScipSymbolInfo,
    SymbolRole,
)
from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator

# File A owns a function and a file-module symbol.
A_MODULE = "scip-typescript npm app 1.0.0 src/`a.ts`/"
A_FUNC = "scip-typescript npm app 1.0.0 src/`a.ts`/doThing()."


def _doc_a() -> ScipDocument:
    return ScipDocument(
        relative_path=Path("src/a.ts"),
        language="typescript",
        symbols={
            A_MODULE: ScipSymbolInfo(symbol=A_MODULE, kind_name="Module"),
            A_FUNC: ScipSymbolInfo(symbol=A_FUNC, kind_name="Function", display_name="doThing"),
        },
        occurrences=[
            ScipOccurrence(
                start_line=0,
                start_col=0,
                end_line=0,
                end_col=7,
                symbol=A_FUNC,
                symbol_roles=SymbolRole.DEFINITION,
            ),
        ],
    )


def _doc_b() -> ScipDocument:
    """File B imports and references A's function and its file module."""
    return ScipDocument(
        relative_path=Path("src/b.ts"),
        language="typescript",
        symbols={},
        occurrences=[
            ScipOccurrence(
                start_line=0,
                start_col=0,
                end_line=0,
                end_col=7,
                symbol=A_FUNC,
                symbol_roles=SymbolRole.IMPORT,
            ),
            ScipOccurrence(
                start_line=1,
                start_col=0,
                end_line=1,
                end_col=7,
                symbol=A_MODULE,
                symbol_roles=SymbolRole.IMPORT,
            ),
        ],
    )


async def test_codefile_survives_cross_file_import(tmp_path: Path) -> None:
    # A real repo where src/a.ts exists so its symbols are local, not external.
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "a.ts").write_text("export function doThing() {}\n")
    (tmp_path / "src" / "b.ts").write_text("import { doThing } from './a';\n")

    translator = ScipGraphTranslator(repo_root=tmp_path, graph_id="g")
    delta_a = translator.translate(_doc_a())
    delta_b = translator.translate(_doc_b())

    # Persist through the REAL store: A's rich node then B's bare import stub
    # (same id). The fixed add_nodes_batch must union labels, not clobber.
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    try:
        await store.add_nodes_batch("g", delta_a.nodes)
        await store.add_nodes_batch("g", delta_b.nodes)
        rows = await store.query("g", "node_by_id", {"node_id": A_MODULE})
    finally:
        await store.close()

    assert rows, f"file A's node {A_MODULE!r} not found after cross-file import persist"
    labels = rows[0]["n"]["labels"]
    assert "CodeFile" in labels, (
        f"file A's CodeFile node was clobbered by a cross-file import stub; labels={labels}"
    )
