"""Mixed-ID (intra-TypeScript) acceptance test.

The highest-risk case for the SCIP integration is *within* TypeScript, not
across languages: when SCIP indexes some TS files (canonical IDs = SCIP symbol
strings) but others fall back to Tree-sitter (legacy module-based IDs), an edge
that crosses the two ID schemes must still resolve to a real target node.

This fails *silently* if broken — BFS in ``trace_dependencies`` drops an
unresolvable neighbour and returns a short path with no error, so translator-
level unit tests stay green while real cross-file queries return nothing. This
test pins the boundary directly on ``GraphRetriever``.

See docs/scip-typescript-integration-plan.md §13.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.retrieval import GraphRetriever
from ariadne_graph.core.snippets import SnippetExtractor

from ..test_core.test_retrieval import FakeSearchableGraphStore

GRAPH = "mixed-id-repo"

# SCIP-indexed node: canonical ID is the SCIP symbol string.
MAIN_ID = "scip-typescript npm repo 1.0.0 src/main.ts/run()."
# Tree-sitter-fallback node: canonical ID is the legacy module-based form.
UTIL_ID = "src.utils:helper"


@pytest.fixture
def retriever(tmp_path: Path) -> GraphRetriever:
    """Graph with a CALLS edge crossing the SCIP-ID / legacy-ID boundary."""
    store = FakeSearchableGraphStore()
    store.add(
        GRAPH,
        MAIN_ID,
        labels=["CodeFunction"],
        name="run",
        qualname="main.run",
        file_path="src/main.ts",
    )
    store.add(
        GRAPH,
        UTIL_ID,
        labels=["CodeFunction"],
        name="helper",
        qualname="utils.helper",
        file_path="src/utils.ts",
    )
    # main.ts (SCIP id) -> utils.ts (legacy id): the boundary-crossing edge.
    store.edge(GRAPH, MAIN_ID, UTIL_ID, "CALLS")
    return GraphRetriever(store, SnippetExtractor(repo_root=tmp_path))


async def test_trace_crosses_scip_to_legacy_boundary(
    retriever: GraphRetriever,
) -> None:
    """Downstream trace from a SCIP-id node reaches the legacy-id callee."""
    paths = await retriever.trace_dependencies(
        GRAPH, MAIN_ID, direction="down", max_depth=3
    )
    reached = {p["node_id"] for p in paths}
    assert UTIL_ID in reached, (
        "trace_dependencies did not cross the SCIP-id -> legacy-id boundary; "
        f"reached only {reached}"
    )


async def test_impact_returns_scip_caller_of_legacy_node(
    retriever: GraphRetriever,
) -> None:
    """Impact of the legacy-id callee includes the SCIP-id caller upstream."""
    result = await retriever.impact_analysis(GRAPH, UTIL_ID)
    affected = set(result.transitive_affected) | set(result.direct_dependencies)
    assert MAIN_ID in affected, (
        "impact_analysis did not surface the SCIP-id caller of a legacy-id "
        f"node; affected={affected}"
    )


async def test_search_resolves_scip_node_by_name(
    retriever: GraphRetriever,
) -> None:
    """A SCIP-id node is still resolvable by its human name, not just its ID."""
    node = await retriever._resolve_symbol(GRAPH, "run")
    assert node is not None and node.get("id") == MAIN_ID
