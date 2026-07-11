"""Mobile/.tsx files still emit per-file IMPORTS_SYMBOL edges (bead c66).

The mobile subtree and some server entrypoints (e.g. server/index.ts) were
getting ZERO IMPORTS_SYMBOL edges. This pins the invariant that an import
occurrence in a subproject-prefixed ``.tsx`` file produces an IMPORTS_SYMBOL
edge whose ``owner_file_path`` is the correct (prefix-rebased) absolute path.
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
    """A mobile-style .tsx screen importing a sibling component."""
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


def test_mobile_tsx_emits_imports_symbol_edge(tmp_path: Path) -> None:
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
