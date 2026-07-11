"""Module nodes must not DEFINES/CONTAINS themselves (bead 0ef).

The file-module symbol (``src/`x.ts`/``) is both the module node id *and* a
member of ``document.symbols``, so the CONTAINS/DEFINES loop emitted an edge
from the module to itself -> 607 DEFINES(x,x) + 607 CONTAINS(x,x) self-loops.
Self-loops are filtered at edge construction.
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

MODULE = "scip-typescript npm app 1.0.0 src/`x.ts`/"
FUNC = "scip-typescript npm app 1.0.0 src/`x.ts`/run()."


def _doc() -> ScipDocument:
    return ScipDocument(
        relative_path=Path("src/x.ts"),
        language="typescript",
        symbols={
            MODULE: ScipSymbolInfo(symbol=MODULE, kind_name="Module"),
            FUNC: ScipSymbolInfo(symbol=FUNC, kind_name="Function", display_name="run"),
        },
        occurrences=[
            ScipOccurrence(
                start_line=0,
                start_col=0,
                end_line=0,
                end_col=3,
                symbol=FUNC,
                symbol_roles=SymbolRole.DEFINITION,
            ),
        ],
    )


def test_no_module_self_loops() -> None:
    translator = ScipGraphTranslator(repo_root=Path("/repo"), graph_id="g")
    delta = translator.translate(_doc())

    self_loops = [
        e for e in delta.edges if e.rel_type in {"DEFINES", "CONTAINS"} and e.source == e.target
    ]
    assert not self_loops, f"module self-loop edges emitted: {self_loops}"

    # The real member edge (module -> run) must still exist.
    real = [e for e in delta.edges if e.rel_type == "DEFINES" and e.source != e.target]
    assert real, "DEFINES edge to the real member was dropped"
