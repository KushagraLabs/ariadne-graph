"""End-to-end: a SCIP-covered subproject emits IMPORTS_SYMBOL edges (bead c66).

The bead's title symptom was "mobile has zero import edges". Investigation
showed the deeper cause: ``scip-typescript`` (v0.4.0) never sets the ``IMPORT``
symbol-role bit on occurrences, so the translator's import branch never fires
for ANY file it covers. Mobile is 100% SCIP-covered, so it got zero import
edges; root-project files only had imports where they fell OUTSIDE SCIP coverage
and hit the Tree-sitter fallback, which masked the bug.

The earlier synthetic tests injected a fake ``SymbolRole.IMPORT`` occurrence and
were green while the real path was broken. This test drives the REAL adapter
path (real ``scip-typescript`` + ``prepare_project`` + ``extract_file``) over a
mobile-style subproject fixture and asserts real import edges are produced with
an absolute ``owner_file_path`` (matching how the live graph stores it).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.languages.base import ExtractionContext
from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter
from ariadne_graph.languages.typescript.extractor import HAS_TREE_SITTER

pytestmark = [
    pytest.mark.skipif(
        shutil.which("scip-typescript") is None,
        reason="scip-typescript binary not on PATH",
    ),
    pytest.mark.skipif(not HAS_TREE_SITTER, reason="tree-sitter-typescript not installed"),
]


def _write_subproject(repo_root: Path) -> Path:
    """Create a mobile-style subproject with its own tsconfig + local imports."""
    sub = repo_root / "mobile"
    (sub / "src").mkdir(parents=True)
    (sub / "tsconfig.json").write_text(
        """{
  "compilerOptions": {
    "strict": true,
    "baseUrl": ".",
    "paths": { "@/*": ["src/*"] },
    "moduleResolution": "node",
    "target": "ES2020",
    "module": "ESNext",
    "jsx": "react"
  },
  "include": ["src/**/*"]
}
"""
    )
    (sub / "src" / "button.ts").write_text("export const Button = () => 'b';\n")
    (sub / "src" / "util.ts").write_text("export const util = 1;\n")
    (sub / "src" / "screen.tsx").write_text(
        "import { Button } from './button';\n"
        "import { util } from '@/util';\n"
        "export const Screen = () => Button() + String(util);\n"
    )
    return sub / "src" / "screen.tsx"


def test_subproject_screen_emits_import_edges(tmp_path: Path) -> None:
    screen = _write_subproject(tmp_path)

    cfg = AnalyzerConfig(repo_root=tmp_path, graph_id="g", scip_typescript_enabled=True)
    adapter = TypeScriptLanguageAdapter()
    files = adapter.discover_files(tmp_path, cfg)
    assert screen.resolve() in {f.resolve() for f in files}, "fixture screen not discovered"

    ctx = ExtractionContext(
        graph_id="g",
        repo_root=tmp_path,
        source_commit=None,
        all_files=files,
        changed_files=files,
    )
    asyncio.run(adapter.prepare_project(ctx, cfg, files, files, graph_store=None))

    delta = adapter.extract_file(screen, ctx)

    # It must be the SCIP-backed delta, not a Tree-sitter fallback (which would
    # trivially have imports and hide the real bug).
    assert delta.parser_version.endswith(":scip"), (
        f"expected SCIP-backed delta, got parser_version={delta.parser_version!r}"
    )

    code_files = [n for n in delta.nodes if "CodeFile" in n.labels]
    assert code_files, "no CodeFile node for the subproject screen"

    import_edges = [e for e in delta.edges if e.rel_type == "IMPORTS_SYMBOL"]
    import_nodes = [n for n in delta.nodes if "CodeImport" in n.labels]
    assert import_edges, "SCIP-covered subproject file produced no IMPORTS_SYMBOL edges"
    assert import_nodes, "SCIP-covered subproject file produced no CodeImport nodes"

    # owner_file_path is stored ABSOLUTE in the live graph.
    expected_owner = str(screen.resolve())
    owners = {e.properties.get("owner_file_path") for e in import_edges}
    assert owners == {expected_owner}, (
        f"IMPORTS_SYMBOL owner_file_path wrong; expected {expected_owner!r}, got {owners!r}"
    )

    # The two local imports resolve to their real target files.
    resolved = {e.properties.get("resolved_source") for e in import_edges}
    assert str((tmp_path / "mobile" / "src" / "button.ts").resolve()) in resolved, (
        f"relative import did not resolve to button.ts; resolved={resolved!r}"
    )
    assert str((tmp_path / "mobile" / "src" / "util.ts").resolve()) in resolved, (
        f"@/ alias import did not resolve to util.ts; resolved={resolved!r}"
    )


def test_merge_imports_is_idempotent_on_repeated_enrich(tmp_path: Path) -> None:
    """Enriching the SAME delta twice must not duplicate import edges.

    The adapter calls enrich() more than once and deltas can be re-enriched
    across syncs; _merge_imports must skip import edges already present rather
    than append them again. This isolates the edge-idempotency guard: it fails
    (duplicated edges) if the guard is removed, while _merge_imports still runs.
    """
    from ariadne_graph.languages.typescript.scip_enricher import TreeSitterEnricher

    screen = _write_subproject(tmp_path)
    cfg = AnalyzerConfig(repo_root=tmp_path, graph_id="g", scip_typescript_enabled=True)
    adapter = TypeScriptLanguageAdapter()
    files = adapter.discover_files(tmp_path, cfg)
    ctx = ExtractionContext(
        graph_id="g",
        repo_root=tmp_path,
        source_commit=None,
        all_files=files,
        changed_files=files,
    )
    asyncio.run(adapter.prepare_project(ctx, cfg, files, files, graph_store=None))

    delta = adapter.extract_file(screen, ctx)

    def _import_edges(d):
        return [e for e in d.edges if e.rel_type in ("IMPORTS_SYMBOL", "IMPORTS")]

    before = _import_edges(delta)
    assert before, "fixture produced no import edges to test idempotency against"

    # Re-run the import merge on the already-enriched delta.
    enricher = TreeSitterEnricher()
    source_bytes = screen.read_bytes()
    enricher._merge_imports(screen, source_bytes, delta, tmp_path)

    after = _import_edges(delta)
    assert len(after) == len(before), (
        f"repeated _merge_imports duplicated import edges: {len(before)} -> {len(after)}"
    )
    ids = [(e.source, e.target, e.rel_type) for e in after]
    assert len(ids) == len(set(ids)), "duplicate import edge identities after re-enrich"


def test_merge_imports_node_ids_do_not_collide_across_subprojects(tmp_path: Path) -> None:
    """Same subpath in two subprojects must yield DISTINCT import node ids.

    ``mobile/src/screen.tsx`` and ``web/src/screen.tsx`` are structurally
    identical. If import node identity is rooted at the nearest tsconfig dir
    instead of the true repo root, both collapse to ``src.screen:import:...``
    and one subproject's nodes overwrite the other's in the shared graph.
    """
    from ariadne_graph.core.models import CodeGraphDelta
    from ariadne_graph.languages.typescript.scip_enricher import TreeSitterEnricher

    def _make(sub_name: str) -> Path:
        sub = tmp_path / sub_name
        (sub / "src").mkdir(parents=True)
        (sub / "tsconfig.json").write_text(
            '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}},"include":["src/**/*"]}\n'
        )
        (sub / "src" / "dep.ts").write_text("export const dep = 1;\n")
        (sub / "src" / "screen.tsx").write_text(
            "import { dep } from './dep';\nexport const S = () => dep;\n"
        )
        return sub / "src" / "screen.tsx"

    mobile_screen = _make("mobile")
    web_screen = _make("web")
    enricher = TreeSitterEnricher()

    def _import_ids(screen: Path) -> set[str]:
        delta = CodeGraphDelta(
            graph_id="g",
            file_path=str(screen),
            content_hash="",
            parser_version="test",
        )
        enricher._merge_imports(screen, screen.read_bytes(), delta, tmp_path)
        return {n.id for n in delta.nodes if "CodeImport" in n.labels}

    mobile_ids = _import_ids(mobile_screen)
    web_ids = _import_ids(web_screen)
    assert mobile_ids, "no mobile import nodes produced"
    assert web_ids, "no web import nodes produced"
    # The two subprojects' import node ids must be disjoint (repo-rooted, so one
    # is prefixed mobile/ and the other web/).
    assert mobile_ids.isdisjoint(web_ids), (
        f"cross-subproject import node id collision: {mobile_ids & web_ids}"
    )
