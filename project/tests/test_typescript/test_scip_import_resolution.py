"""Translator resolves an IMPORT-role symbol to its target file (unit).

SCOPE / CAVEAT: this drives ``ScipGraphTranslator`` with a SYNTHETIC
``SymbolRole.IMPORT`` occurrence. Real ``scip-typescript`` (v0.4.0) does NOT set
the IMPORT role, so this exercises the translator's resolution branch IN
ISOLATION -- it is NOT how ``resolved_source`` gets onto the live graph today
(that comes from the enricher grafting Tree-sitter imports, covered end-to-end by
``test_scip_subproject_imports_e2e.py``). See bead code_hygiene_mcp-qb9.

What this pins: given an IMPORT-role occurrence whose SCIP symbol embeds a target
file path (``scip-... src/`util.ts`/helper().``), the translator resolves it to
the real repo file and records ``resolved_source`` (absolute path) on both the
CodeImport node and the IMPORTS_SYMBOL edge.
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
    # SYNTHETIC IMPORT-role occurrence: real scip-typescript never sets this bit.
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


def test_translator_import_role_resolves_to_target_file(tmp_path: Path) -> None:
    # Synthetic IMPORT-role input; real-path resolution coverage is in the e2e test.
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
