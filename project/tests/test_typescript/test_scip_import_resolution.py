"""Relative-import IMPORTS_SYMBOL edges resolve to a target file (bead p25).

SCIP import occurrences never carried a resolved target, so ``resolved_source``
was non-null on only ~0.4% of ``IMPORTS_SYMBOL`` edges and file-to-file
dependency queries came up empty for TypeScript.

For a relative/local import the imported SCIP *symbol* already embeds the
target file path (``scip-... src/`util.ts`/helper().``). The translator resolves
that to the real repo file and records ``resolved_source`` (absolute path) on
the CodeImport node and the IMPORTS_SYMBOL edge — matching the property name the
Tree-sitter extractor already emits.
"""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.languages.typescript.scip_parser import (
    ScipDocument,
    ScipOccurrence,
    SymbolRole,
)
from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator

HELPER = "scip-typescript npm app 1.0.0 src/`util.ts`/helper()."


def _importing_doc() -> ScipDocument:
    return ScipDocument(
        relative_path=Path("src/main.ts"),
        language="typescript",
        symbols={},
        occurrences=[
            ScipOccurrence(
                start_line=0,
                start_col=9,
                end_line=0,
                end_col=15,
                symbol=HELPER,
                symbol_roles=SymbolRole.IMPORT,
            ),
        ],
    )


def test_relative_import_resolves_to_target_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "util.ts").write_text("export const helper = 1;\n")
    (tmp_path / "src" / "main.ts").write_text("import { helper } from './util';\n")

    translator = ScipGraphTranslator(repo_root=tmp_path, graph_id="g")
    delta = translator.translate(_importing_doc())

    import_edges = [e for e in delta.edges if e.rel_type == "IMPORTS_SYMBOL"]
    assert import_edges, "expected an IMPORTS_SYMBOL edge"

    target_file = str(tmp_path / "src" / "util.ts")
    resolved_edges = [e for e in import_edges if e.properties.get("resolved_source") == target_file]
    assert resolved_edges, (
        "IMPORTS_SYMBOL edge did not resolve to the target file; "
        f"edge props: {[e.properties for e in import_edges]}"
    )

    # The CodeImport node also carries the resolved target for file-level queries.
    import_nodes = [n for n in delta.nodes if "CodeImport" in n.labels]
    assert any(n.properties.get("resolved_source") == target_file for n in import_nodes), (
        f"no CodeImport node carried resolved_source={target_file}"
    )
