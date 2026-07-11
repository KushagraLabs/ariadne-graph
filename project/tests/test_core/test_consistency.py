"""Data-consistency regression tests for the ariadne code-graph MCP server.

These tests pin down concrete inconsistencies a reviewer observed against the
live daemon. They are written to FAIL against the current code and to turn
green once the corresponding fix lands. Each test documents the bead it maps
to (A..E in the scoping doc).

The two hard cases exercised here:

* **A (bug):** ``code_graph_inspect_file`` returned zero NODES but N EDGES for a
  file. Nodes are matched on ``properties.file_path`` while edges are matched on
  ``properties.owner_file_path``. In the SCIP/TypeScript path these two strings
  are constructed independently — the node ``file_path`` is
  ``str(repo_root.resolve() / rel)`` (canonicalised) while the edge
  ``owner_file_path`` is ``str(path)`` from the raw filesystem walk. When the
  repo is reached through a non-canonical path (symlink / macOS firmlink) the
  two keys diverge for the *same* logical file, so ``inspect_file`` can match a
  file's edges without matching its nodes (or vice versa).

* **B:** ``code_graph_retrieve`` fails for obvious filenames (``schema.ts``)
  because ``GraphRetriever._resolve_symbol`` only matches node id / name /
  qualname, never a node's ``file_path`` (or its basename).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.languages.base import LanguageAdapter
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import InspectFileInput, RetrieveInput
from ariadne_graph.mcp.tools import ToolRegistry

pytestmark = pytest.mark.asyncio


def _make_registry(graph_store: GraphStore, repo_root: Path) -> ToolRegistry:
    config = AnalyzerConfig(repo_root=repo_root)
    adapters: dict[str, LanguageAdapter] = {"python": PythonLanguageAdapter()}
    searchable = graph_store if isinstance(graph_store, SearchableGraphStore) else None
    return ToolRegistry(
        graph_store=graph_store,
        searchable_store=searchable,
        adapters=adapters,
        config=config,
        snippet_extractor=SnippetExtractor(repo_root=repo_root),
        embedding_provider=None,
    )


def _scip_like_delta(
    graph_id: str,
    node_file_path: str,
    edge_owner_file_path: str,
) -> tuple[list[CodeNode], list[CodeEdge]]:
    """Build a file's nodes + edges the way the SCIP/TypeScript path does.

    ``node_file_path`` is what the translator writes to node
    ``properties.file_path`` (``str(repo_root.resolve() / rel)``); the
    ``edge_owner_file_path`` is what ``adapter._tag_owner_file`` writes to edge
    ``properties.owner_file_path`` (``str(path)`` from the raw walk). Callers
    pass the two strings to model whether they agree.
    """
    module = CodeNode(
        id="scip go . schema/",
        graph_id=graph_id,
        labels=["KnowledgeNode", "CodeModule", "CodeFile"],
        properties={"name": "schema", "file_path": node_file_path},
    )
    symbol = CodeNode(
        id="scip go . schema/User#",
        graph_id=graph_id,
        labels=["KnowledgeNode", "CodeClass"],
        properties={"name": "User", "file_path": node_file_path},
    )
    nodes = [module, symbol]
    edges = [
        CodeEdge(
            source=module.id,
            target=symbol.id,
            graph_id=graph_id,
            rel_type="CONTAINS",
            properties={"owner_file_path": edge_owner_file_path},
        ),
        CodeEdge(
            source=module.id,
            target=symbol.id,
            graph_id=graph_id,
            rel_type="DEFINES",
            properties={"owner_file_path": edge_owner_file_path},
        ),
    ]
    return nodes, edges


# ---------------------------------------------------------------------------
# A (bug): inspect_file must never return edges for a file without its nodes.
# ---------------------------------------------------------------------------

async def test_inspect_file_no_orphaned_edges_when_paths_diverge(tmp_path: Path) -> None:
    """The reviewer's "0 nodes / N edges" case.

    Node ``file_path`` is canonicalised (``str(repo_root.resolve() / rel)``)
    while edge ``owner_file_path`` is the raw walk path (``str(path)``). When the
    repo is reached via a non-canonical path these differ for the same file.
    ``inspect_file`` matches the edges (owner_file_path) but not the nodes
    (file_path), producing an orphaned-edge response.

    Acceptance criterion (bead A): if inspect_file returns any edges for a file
    it MUST also return that file's nodes — the two views are keyed on the same
    normalised file identity.
    """
    graph_id = "g_consistency"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    # Same logical file, two path spellings for the same location.
    canonical = str((tmp_path / "src" / "schema.ts").resolve())
    walk_path = str(tmp_path / "src" / "schema.ts")  # not run through .resolve()

    # Force a divergence the way a symlinked / firmlinked repo root would:
    # nodes keyed on the canonical form, edges on a differently-spelled form.
    diverged_walk = walk_path + "/."  # trivially non-canonical spelling
    nodes, edges = _scip_like_delta(
        graph_id,
        node_file_path=canonical,
        edge_owner_file_path=diverged_walk,
    )
    await store.add_nodes_batch(graph_id, nodes)
    await store.add_edges_batch(graph_id, edges)

    # The caller inspects the file by the edge-owner spelling (as the daemon
    # observed): edges come back, nodes do not.
    result = await registry.handle_inspect_file(
        InspectFileInput(file_path=diverged_walk, graph_id=graph_id)
    )

    # HARD assertion: no orphaned-edge responses. Edges present ⇒ nodes present.
    if result.edges:
        assert result.nodes, (
            "inspect_file returned "
            f"{len(result.edges)} edges but {len(result.nodes)} nodes for the "
            "same file — node file_path and edge owner_file_path keys diverged "
            "(bead A)."
        )


async def test_inspect_file_nodes_and_edges_agree_on_normalized_key(tmp_path: Path) -> None:
    """Whichever spelling the caller uses, nodes and edges are consistent.

    Complements the case above: inspecting by the *node* spelling must also
    surface the file's edges, once both queries normalise the file key.
    """
    graph_id = "g_consistency2"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    canonical = str((tmp_path / "src" / "schema.ts").resolve())
    diverged_walk = canonical + "/."

    nodes, edges = _scip_like_delta(
        graph_id,
        node_file_path=canonical,
        edge_owner_file_path=diverged_walk,
    )
    await store.add_nodes_batch(graph_id, nodes)
    await store.add_edges_batch(graph_id, edges)

    result = await registry.handle_inspect_file(
        InspectFileInput(file_path=canonical, graph_id=graph_id)
    )

    assert result.nodes, "expected the file's nodes for the canonical spelling"
    # Nodes exist for this file ⇒ its edges must come back too.
    assert result.edges, (
        f"inspect_file returned {len(result.nodes)} nodes but 0 edges — edge "
        "owner_file_path did not normalise to the same key as node file_path "
        "(bead A)."
    )


# ---------------------------------------------------------------------------
# B: retrieve must resolve a plain filename, not only exact symbol ids.
# Tracked separately as bead w2b (filename/fuzzy retrieve) — not part of the
# inspect_file path-key bug (8h2). Left here as an executable spec, marked xfail
# until w2b lands so it documents the gap without failing 8h2's verification.
# ---------------------------------------------------------------------------

async def test_retrieve_by_filename_resolves_module_node(tmp_path: Path) -> None:
    """``code_graph_retrieve('schema.ts')`` should find the file's module node.

    Reproduces the reviewer's report: retrieval works for exact SCIP symbols but
    fails for obvious filenames. The module node's ``name`` is the stem
    (``"schema"``) with no extension, and no node carries ``name == "schema.ts"``,
    so the id/name/qualname-only resolver returns not-found.

    Acceptance criterion (bead B): a filename / basename query resolves to the
    node whose ``file_path`` basename matches (and returns candidate matches when
    several files share the basename).
    """
    graph_id = "g_retrieve"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    file_path = str((tmp_path / "src" / "schema.ts").resolve())
    nodes, edges = _scip_like_delta(
        graph_id, node_file_path=file_path, edge_owner_file_path=file_path
    )
    await store.add_nodes_batch(graph_id, nodes)
    await store.add_edges_batch(graph_id, edges)

    result = await registry.handle_retrieve(
        RetrieveInput(query="schema.ts", graph_id=graph_id)
    )

    assert result.results, "retrieve returned no results for filename 'schema.ts'"
    entry = result.results[0]
    data = entry.get("data", {})
    assert data.get("found") is True, (
        "retrieve could not resolve the filename 'schema.ts' to any node — "
        "resolver matches only id/name/qualname, not file_path basename (bead B)."
    )


async def test_retrieve_by_filename_ambiguous_returns_candidates(tmp_path: Path) -> None:
    """Two files sharing a basename must yield candidates, not a silent guess.

    Complements the single-match case: when ``schema.ts`` exists under two
    different directories, ``retrieve`` cannot know which one the caller
    means, so it must report ``found: False`` plus a ``candidates`` list
    covering both files instead of arbitrarily picking one (bead w2b).
    """
    graph_id = "g_retrieve_ambiguous"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    path_a = str((tmp_path / "src" / "schema.ts").resolve())
    path_b = str((tmp_path / "lib" / "schema.ts").resolve())
    nodes_a, edges_a = _scip_like_delta(graph_id, node_file_path=path_a, edge_owner_file_path=path_a)
    nodes_b, edges_b = _scip_like_delta(graph_id, node_file_path=path_b, edge_owner_file_path=path_b)
    # Distinct ids per file so nodes don't collide in the store.
    for node in nodes_b:
        node.id = node.id + "#b"
    for edge in edges_b:
        edge.source = edge.source + "#b"
        edge.target = edge.target + "#b"
    await store.add_nodes_batch(graph_id, nodes_a + nodes_b)
    await store.add_edges_batch(graph_id, edges_a + edges_b)

    result = await registry.handle_retrieve(
        RetrieveInput(query="schema.ts", graph_id=graph_id)
    )

    assert result.results, "expected a result entry documenting the ambiguity"
    data = result.results[0].get("data", {})
    assert data.get("found") is False, "ambiguous basename must not silently resolve"
    candidates = data.get("candidates", [])
    assert len(candidates) == 2, f"expected 2 candidate files, got {len(candidates)}"
    candidate_files = {c.get("properties", {}).get("file_path") for c in candidates}
    assert candidate_files == {path_a, path_b}
