"""External/third-party SCIP symbols collapse to lightweight refs (bead 60t).

``_external_symbol_node`` used to materialize a full first-class node for every
referenced-but-undefined symbol, including npm library internals (e.g.
``useState`` inside ``@types/react``'s ``.d.ts``). That inflated the node count
~45% and produced thousands of null-file_path import nodes.

An npm-package internal must NOT become a full symbol node per library
internal. Legitimately cross-file *local* symbols (real repo ``.ts`` files that
this document imports) must still resolve to their real node.
"""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.languages.typescript.scip_parser import (
    ScipDocument,
    ScipOccurrence,
)
from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator

# A React hook defined in the @types/react declaration package — external.
NPM_SYMBOL = "scip-typescript npm @types/react 18.0.0 `index.d.ts`/useState()."
# A helper defined in a real sibling file that this document imports — local.
LOCAL_SYMBOL = "scip-typescript npm app 1.0.0 src/`util.ts`/helper()."


def _document_importing(repo: Path) -> ScipDocument:
    """A doc that references both an npm symbol and a real local sibling."""
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "util.ts").write_text("export const helper = 1;\n")
    return ScipDocument(
        relative_path=Path("src/main.ts"),
        language="typescript",
        symbols={},
        occurrences=[
            ScipOccurrence(
                start_line=0,
                start_col=0,
                end_line=0,
                end_col=8,
                symbol=NPM_SYMBOL,
                symbol_roles=0,
            ),
            ScipOccurrence(
                start_line=1,
                start_col=0,
                end_line=1,
                end_col=6,
                symbol=LOCAL_SYMBOL,
                symbol_roles=0,
            ),
        ],
    )


def test_npm_internal_not_a_full_symbol_node(tmp_path: Path) -> None:
    translator = ScipGraphTranslator(repo_root=tmp_path, graph_id="g")
    delta = translator.translate(_document_importing(tmp_path))

    npm_nodes = [n for n in delta.nodes if n.properties.get("scip_symbol") == NPM_SYMBOL]
    # The npm library internal must NOT be materialized as a full node.
    assert not npm_nodes, f"npm internal materialized as a full node: {[n.id for n in npm_nodes]}"

    # The real local sibling symbol must still resolve to its own node.
    local_nodes = [n for n in delta.nodes if n.properties.get("scip_symbol") == LOCAL_SYMBOL]
    assert local_nodes, "legitimate cross-file local symbol was dropped"
