"""Translator IMPORT-role handling for a subproject-prefixed file (unit).

SCOPE / CAVEAT: this drives ``ScipGraphTranslator`` with a SYNTHETIC
``SymbolRole.IMPORT`` occurrence. Real ``scip-typescript`` (v0.4.0) does NOT set
the IMPORT role on any occurrence, so this exercises the translator's is_import
branch IN ISOLATION -- a contract that only fires *if* SCIP ever emits the role.
It is NOT coverage of how mobile imports actually reach the graph today; the live
path grafts Tree-sitter imports in the enricher and is covered end-to-end (real
scip-typescript) by ``test_scip_subproject_imports_e2e.py``. See bead
code_hygiene_mcp-qb9 for why this distinction matters (a synthetic-input test
that "passes immediately" once masked c66 being broken on the live graph).

What this pins: given an IMPORT-role occurrence in a subproject-prefixed file,
the translator emits an IMPORTS_SYMBOL edge whose ``owner_file_path`` is the
prefix-rebased absolute path and whose relative import resolves to the sibling.
"""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.languages.typescript.scip_parser import (
    ScipDocument,
    ScipOccurrence,
    SymbolRole,
)
from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator

# A sibling component the screen imports (real repo file -> local).
BUTTON = "scip-typescript npm mobileapp 1.0.0 src/`Button.tsx`/Button#"


def _screen_doc() -> ScipDocument:
    """A mobile-style .tsx screen with a SYNTHETIC IMPORT-role occurrence.

    NOTE: real scip-typescript never sets SymbolRole.IMPORT; this is a
    hand-built input to exercise the translator's is_import branch only.
    """
    return ScipDocument(
        relative_path=Path("src/Screen.tsx"),
        language="typescript",
        symbols={},
        occurrences=[
            ScipOccurrence(
                start_line=0,
                start_col=9,
                end_line=0,
                end_col=15,
                symbol=BUTTON,
                symbol_roles=SymbolRole.IMPORT,
            ),
        ],
    )


def test_translator_import_role_emits_prefix_rebased_edge(tmp_path: Path) -> None:
    # Synthetic IMPORT-role input; real-path mobile coverage is in the e2e test.
    # Subproject layout: <repo>/mobile/src/*.tsx
    (tmp_path / "mobile" / "src").mkdir(parents=True)
    (tmp_path / "mobile" / "src" / "Button.tsx").write_text("export class Button {}\n")
    (tmp_path / "mobile" / "src" / "Screen.tsx").write_text("import { Button } from './Button';\n")

    # SCIP doc paths are project-relative; the translator rebases under 'mobile'.
    translator = ScipGraphTranslator(repo_root=tmp_path, graph_id="g", path_prefix="mobile")
    delta = translator.translate(_screen_doc())

    import_edges = [e for e in delta.edges if e.rel_type == "IMPORTS_SYMBOL"]
    assert import_edges, "mobile .tsx produced no IMPORTS_SYMBOL edges"

    expected_owner = str(tmp_path / "mobile" / "src" / "Screen.tsx")
    assert all(e.properties.get("owner_file_path") == expected_owner for e in import_edges), (
        "IMPORTS_SYMBOL owner_file_path is not the prefix-rebased path; "
        f"got {[e.properties.get('owner_file_path') for e in import_edges]}"
    )

    # And the local sibling import resolves to the real target file (via p25).
    target_file = str(tmp_path / "mobile" / "src" / "Button.tsx")
    assert any(e.properties.get("resolved_source") == target_file for e in import_edges), (
        "mobile relative import did not resolve to its sibling file"
    )
