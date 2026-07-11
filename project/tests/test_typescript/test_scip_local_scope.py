"""SCIP ``local N`` symbols are document-scoped, not global (bead ae0).

``scip-typescript`` emits ``local 0``, ``local 1`` ... symbols for bindings
that never escape a single file. These ids are only unique *within* their
owning document, so using them verbatim as global graph node ids makes two
files that both happen to have a ``local 0`` collide -> phantom cross-file
REFERENCES/CALLS edges between unrelated files.

The fix namespaces every ``local `` symbol id by its owning document path at
the single point it becomes a node id. Non-local (npm/global) symbols carry
their file path inside the symbol string already and must NOT be scoped.
"""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.languages.typescript.scip_parser import (
    ScipDocument,
    ScipOccurrence,
    ScipSymbolInfo,
    SymbolRole,
)
from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator


def _doc_with_local_ref(rel_path: str) -> ScipDocument:
    """A document defining ``local 0`` and referencing it once."""
    local_sym = "local 0"
    return ScipDocument(
        relative_path=Path(rel_path),
        language="typescript",
        symbols={
            local_sym: ScipSymbolInfo(
                symbol=local_sym,
                kind_name="Function",
                display_name="thing",
            )
        },
        occurrences=[
            ScipOccurrence(
                start_line=0,
                start_col=0,
                end_line=0,
                end_col=5,
                symbol=local_sym,
                symbol_roles=SymbolRole.DEFINITION,
            ),
            # A bare reference to the same local symbol (no roles).
            ScipOccurrence(
                start_line=5,
                start_col=0,
                end_line=5,
                end_col=5,
                symbol=local_sym,
                symbol_roles=0,
            ),
        ],
    )


def test_local_symbols_do_not_create_cross_file_edges() -> None:
    """Two files that both define/reference ``local 0`` must share NO edges."""
    translator = ScipGraphTranslator(repo_root=Path("/repo"), graph_id="g")

    delta_a = translator.translate(_doc_with_local_ref("src/a.ts"))
    delta_b = translator.translate(_doc_with_local_ref("src/b.ts"))

    a_node_ids = {n.id for n in delta_a.nodes}
    b_node_ids = {n.id for n in delta_b.nodes}

    # The local symbol's node id must differ between the two documents.
    a_local = {i for i in a_node_ids if "local 0" in i}
    b_local = {i for i in b_node_ids if "local 0" in i}
    assert a_local and b_local
    assert a_local.isdisjoint(b_local), (
        f"local symbol ids collide across files: {a_local & b_local}"
    )

    # No reference/call edge in file A may point at a node owned by file B.
    cross = [
        e for e in delta_a.edges if e.rel_type in {"REFERENCES", "CALLS"} and e.target in b_node_ids
    ]
    assert not cross, f"phantom cross-file edges into b.ts: {cross}"
