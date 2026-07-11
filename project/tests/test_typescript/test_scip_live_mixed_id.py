"""Live mixed-ID boundary test driven through the real adapter.

The sibling ``test_scip_mixed_id.py`` pins the boundary on ``GraphRetriever``
using a *hand-built* store whose SCIP-id and legacy-id nodes are already
aligned by construction. That proves BFS mechanically follows a boundary edge
*once the target node exists under the asserted id* — but it cannot catch the
higher-risk failure: a real adapter run emitting a SCIP edge whose target id
does **not** match the id the Tree-sitter fallback gives the same symbol.

This test closes that gap. It runs the real
:class:`TypeScriptLanguageAdapter` over the committed ``ts_scip_project``
fixture with ``index.scip`` injected (no ``scip-typescript``/``npx``), forcing
``src/utils.ts`` onto the Tree-sitter fallback while ``src/main.ts`` is
SCIP-indexed. The two extractors then meet at a real CALLS/REFERENCES edge that
crosses the SCIP-id / legacy-id boundary, and we assert ``trace_dependencies``
resolves across it.

Ground truth observed from a real run of this fixture:

* SCIP edge target (from ``main.ts``):
  ``scip-typescript npm scip-fixture 1.0.0 src/`utils.ts`/helper().``
* Tree-sitter fallback node id (for ``utils.ts``): ``utils.helper``

These do not match by string equality. The SCIP translator emits a dangling
external stub (``is_external=True``, no ``file_path``) for the symbol-string
target, so a SCIP -> Tree-sitter edge would land on the stub and the trace
would stop there *silently*. ``GraphRetriever._canonicalize_neighbor``
redirects such a stub to the concrete same-named node, which is what this test
verifies end to end. Disabling that redirection makes this test fail, so it
doubles as a regression tripwire for the boundary fix.

See docs/scip-typescript-integration-plan.md §13.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest import mock

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.retrieval import GraphRetriever
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.languages.base import ExtractionContext
from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter
from ariadne_graph.languages.typescript.extractor import HAS_TREE_SITTER
from ariadne_graph.languages.typescript.scip_indexer import ScipTypeScriptIndexer
from ariadne_graph.languages.typescript.scip_parser import ScipIndexParser

from ..test_core.test_retrieval import FakeSearchableGraphStore

pytestmark = pytest.mark.skipif(
    not HAS_TREE_SITTER,
    reason="tree-sitter-typescript required for the Tree-sitter fallback half",
)

GRAPH = "live-mixed-id-repo"
FIXTURE = Path(__file__).parent.parent / "fixtures" / "ts_scip_project"

# The single file forced onto the Tree-sitter fallback. main.ts (SCIP-indexed)
# references its `helper` symbol, so the CALLS/REFERENCES edge from main.ts must
# resolve to this file's Tree-sitter node id.
FALLBACK_DOC = Path("src/utils.ts")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A writable copy of the fixture project (adapter resolves abs paths)."""
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURE, dst)
    return dst


async def _run_adapter(repo: Path) -> FakeSearchableGraphStore:
    """Drive the real adapter over *repo*, forcing utils.ts to fall back.

    Returns a store populated with the merged SCIP + Tree-sitter deltas.
    """
    config = AnalyzerConfig(repo_root=repo, scip_typescript_enabled=True)
    context = ExtractionContext(graph_id=GRAPH, repo_root=repo)
    adapter = TypeScriptLanguageAdapter()
    files = adapter.discover_files(repo, config)
    store = FakeSearchableGraphStore()

    fixture_scip = FIXTURE / "index.scip"
    real_parse = ScipIndexParser.parse

    async def fake_run(
        self: ScipTypeScriptIndexer, output_path: Path, cwd: Path | None = None
    ) -> tuple[int, str, str]:
        # Inject the committed index instead of shelling out to scip-typescript.
        shutil.copyfile(fixture_scip, output_path)
        return (0, "", "")

    def parse_without_fallback_doc(self: ScipIndexParser, path: Path):
        index = real_parse(self, path)
        # Drop utils.ts so the adapter has no SCIP delta for it and routes it to
        # the Tree-sitter extractor in extract_file — producing the mixed state.
        index.documents.pop(FALLBACK_DOC, None)
        return index

    with (
        mock.patch.object(ScipTypeScriptIndexer, "_run_indexer", fake_run),
        mock.patch.object(ScipIndexParser, "parse", parse_without_fallback_doc),
    ):
        await adapter.prepare_project(context, config, files, files, store)
        for path in files:
            delta = adapter.extract_file(path, context)
            await store.add_nodes_batch(GRAPH, delta.nodes)
            await store.add_edges_batch(GRAPH, delta.edges)
    return store


async def test_utils_takes_tree_sitter_fallback(repo: Path) -> None:
    """Precondition: utils.ts is Tree-sitter-indexed, main.ts is SCIP-indexed."""
    store = await _run_adapter(repo)
    nodes = {n["n"]["id"]: n["n"] for n in await store.query(GRAPH, "nodes")}

    # Tree-sitter fallback id for utils.helper (legacy module-based form).
    assert "utils.helper" in nodes, (
        "utils.ts should have fallen back to Tree-sitter (legacy ids); "
        f"present ids: {sorted(nodes)[:20]}"
    )
    # main.ts kept its SCIP id (symbol string), proving the run is genuinely mixed.
    assert any(
        nid.startswith("scip-typescript npm") and "main.ts" in nid for nid in nodes
    ), "main.ts should be SCIP-indexed (symbol-string ids)"


async def test_trace_crosses_scip_to_tree_sitter_boundary(repo: Path) -> None:
    """Downstream trace from the SCIP-indexed main reaches the Tree-sitter helper.

    This is the assertion the fake-store test cannot make: it depends on the two
    *real* extractors agreeing on the boundary node id.
    """
    store = await _run_adapter(repo)
    retriever = GraphRetriever(store, SnippetExtractor(repo_root=repo))

    # Target the SCIP-indexed main.ts module node directly (its REFERENCES edge
    # to utils.ts/helper is the boundary-crossing edge). Resolving by the bare
    # name "main" is ambiguous across re-exporting modules, so pin the id.
    nodes = {n["n"]["id"]: n["n"] for n in await store.query(GRAPH, "nodes")}
    main_id = next(
        nid
        for nid in nodes
        if nid.startswith("scip-typescript npm") and "main.ts`/" in nid and nid.endswith("`/")
    )

    paths = await retriever.trace_dependencies(
        GRAPH, main_id, direction="down", max_depth=4
    )
    reached_files = {
        (p["node"].get("properties", {}) or {}).get("file_path") or "" for p in paths
    }
    assert any("utils.ts" in f for f in reached_files), (
        "trace_dependencies from the SCIP-indexed main() did not reach the "
        "Tree-sitter-indexed utils.ts: the SCIP edge target id and the "
        "Tree-sitter node id for `helper` do not align. "
        f"reached files: {sorted(reached_files)}"
    )
