"""Enclosing-scope fallback must be a real node id, not a display name (bead sv5).

When a Tree-sitter scope (e.g. a ``.tsx`` arrow-function component) does not
line up with any SCIP definition node, the enricher used to fall back to the
component's bare display *name* as the enclosing id. That name is not a graph
node, so every REFERENCES edge routed through it dangled (source matched no
node). The fix falls back to the module/file node id instead — the same
default the translator uses when no enclosing definition is known.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.models import CodeGraphDelta
from ariadne_graph.languages.typescript.extractor import HAS_TREE_SITTER
from ariadne_graph.languages.typescript.scip_enricher import TreeSitterEnricher
from ariadne_graph.languages.typescript.scip_parser import (
    ScipDocument,
    ScipOccurrence,
)
from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator

pytestmark = pytest.mark.skipif(not HAS_TREE_SITTER, reason="tree-sitter not installed")

SOURCE = """\
import { helper } from "./util";

function Widget() {
  return helper();
}
"""


def _document(rel_path: str) -> ScipDocument:
    """A doc whose only occurrence is a bare reference to an imported symbol.

    The arrow-function component ``Widget`` is deliberately absent from SCIP
    symbols so the Tree-sitter scope has no matching SCIP node, forcing the
    fallback path.
    """
    helper_sym = "scip-typescript npm app 1.0.0 src/`util.ts`/helper()."
    # `helper()` is referenced on line 4 (0-based line 3), cols 9-15.
    return ScipDocument(
        relative_path=Path(rel_path),
        language="typescript",
        symbols={},
        occurrences=[
            ScipOccurrence(
                start_line=3,
                start_col=9,
                end_line=3,
                end_col=15,
                symbol=helper_sym,
                symbol_roles=0,
            ),
        ],
    )


def test_reference_source_is_real_node_not_display_name(tmp_path: Path) -> None:
    src = tmp_path / "widget.tsx"
    src.write_text(SOURCE, encoding="utf-8")

    translator = ScipGraphTranslator(repo_root=tmp_path, graph_id="g")
    doc = _document("widget.tsx")

    delta = translator.translate(doc)
    enricher = TreeSitterEnricher()
    _, call_ranges, enclosing_map = enricher.enrich(src, delta)
    delta = translator.translate(doc, call_ranges=call_ranges, enclosing_map=enclosing_map)

    assert isinstance(delta, CodeGraphDelta)
    node_ids = {n.id for n in delta.nodes}
    ref_edges = [e for e in delta.edges if e.rel_type in {"REFERENCES", "CALLS"}]
    assert ref_edges, "expected a reference/call edge for helper()"

    for e in ref_edges:
        assert e.source in node_ids, (
            f"reference edge source {e.source!r} is not a real node id; nodes={node_ids}"
        )
        # The regression was falling back to the bare component name.
        assert e.source != "Widget"
