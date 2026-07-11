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
from ariadne_graph.core.retrieval import GraphRetriever
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.languages.base import LanguageAdapter
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import (
    FindHotspotsInput,
    GetArchitectureInput,
    InspectFileInput,
    RetrieveInput,
)
from ariadne_graph.mcp.tools import ToolRegistry, _graph_id_from_repo_path

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


# ---------------------------------------------------------------------------
# C (bug, bead 11m): edge direction must be classified from the edge's own
# source/target relative to the requested node, not from which named query
# happened to return the row.
# ---------------------------------------------------------------------------

async def test_retrieve_edge_direction_relative_to_requested_node(tmp_path: Path) -> None:
    """An external node pointing AT the requested node must land in `incoming`.

    ``GraphRetriever._get_edges`` asks the store for e.g. "node_outgoing_edges";
    when that named query returns no rows it falls back to "node_edges", which
    every backend implements as bidirectional (source == node_id OR
    target == node_id). The old code kept whichever rows the query returned
    and labelled them by which query slot produced them ("outgoing" or
    "incoming"), so a bidirectional fallback row landed in the wrong bucket
    whenever the node had edges in only one direction — e.g. a cross-extractor
    stub / external node with an edge that points AT the requested node (N has
    only an incoming edge) still showed up under "outgoing".

    Acceptance criterion (bead 11m): for the requested node N, every edge in
    ``outgoing`` has source == N and every edge in ``incoming`` has
    target == N, regardless of which query produced the row.
    """
    graph_id = "g_edge_direction"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    target_node = CodeNode(
        id="N",
        graph_id=graph_id,
        labels=["CodeFunction"],
        properties={"name": "N"},
    )
    external_node = CodeNode(
        id="EXT",
        graph_id=graph_id,
        labels=["CodeFunction"],
        properties={"name": "EXT", "is_external": True},
    )
    await store.add_nodes_batch(graph_id, [target_node, external_node])

    # N has ONLY an incoming edge (external -> N), no outgoing edges at all.
    # This forces GraphRetriever._get_edges(direction="outgoing") through the
    # bidirectional "node_edges" fallback (since "node_outgoing_edges" finds
    # nothing), which is exactly the path that used to mislabel direction.
    await store.add_edges_batch(
        graph_id,
        [CodeEdge(source="EXT", target="N", graph_id=graph_id, rel_type="CALLS")],
    )

    retriever = GraphRetriever(store, SnippetExtractor(repo_root=tmp_path))
    result = await retriever.retrieve_node(graph_id, "N")

    outgoing = result["edges"]["outgoing"]
    incoming = result["edges"]["incoming"]

    for edge in outgoing:
        assert edge.get("source") == "N", (
            f"edge {edge} in 'outgoing' does not have source == requested node "
            "'N' (bead 11m: direction must be classified from the edge's own "
            "source/target, not from which query produced the row)."
        )
    for edge in incoming:
        assert edge.get("target") == "N", (
            f"edge {edge} in 'incoming' does not have target == requested node "
            "'N' (bead 11m)."
        )

    # The EXT -> N edge must be classified as incoming to N, and must NOT
    # also appear under outgoing.
    assert any(e.get("source") == "EXT" for e in incoming), (
        "expected the EXT -> N edge to appear in 'incoming'"
    )
    assert not any(e.get("source") == "EXT" for e in outgoing), (
        "EXT -> N edge incorrectly classified as 'outgoing' for node 'N'"
    )


# ---------------------------------------------------------------------------
# D (bead c7z): pagination + summary_only/include_edges + schema_version.
# ---------------------------------------------------------------------------

async def test_inspect_file_limit_bounds_payload(tmp_path: Path) -> None:
    """``limit`` must cap the number of nodes/edges returned, not just rank them.

    Acceptance criterion (bead c7z): requesting a small ``limit`` returns at
    most that many nodes/edges while ``total_nodes``/``total_edges`` still
    report the true counts and ``has_more`` is set.
    """
    graph_id = "g_paginate_inspect"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    file_path = str((tmp_path / "src" / "big.py").resolve())
    module = CodeNode(
        id="mod", graph_id=graph_id, labels=["CodeModule", "CodeFile"],
        properties={"name": "mod", "file_path": file_path},
    )
    members = [
        CodeNode(
            id=f"member{i}", graph_id=graph_id, labels=["CodeFunction"],
            properties={"name": f"member{i}", "file_path": file_path},
        )
        for i in range(10)
    ]
    edges = [
        CodeEdge(
            source="mod", target=f"member{i}", graph_id=graph_id, rel_type="CONTAINS",
            properties={"owner_file_path": file_path},
        )
        for i in range(10)
    ]
    await store.add_nodes_batch(graph_id, [module, *members])
    await store.add_edges_batch(graph_id, edges)

    result = await registry.handle_inspect_file(
        InspectFileInput(file_path=file_path, graph_id=graph_id, limit=3, offset=0)
    )

    assert len(result.nodes) == 3, f"expected 3 nodes with limit=3, got {len(result.nodes)}"
    assert len(result.edges) == 3, f"expected 3 edges with limit=3, got {len(result.edges)}"
    assert result.total_nodes == 11, f"expected total_nodes=11 (1 module + 10 members), got {result.total_nodes}"
    assert result.total_edges == 10, f"expected total_edges=10, got {result.total_edges}"
    assert result.has_more is True, "expected has_more=True when limit < total"


async def test_inspect_file_summary_only_omits_lists(tmp_path: Path) -> None:
    """``summary_only=True`` must omit the nodes/edges lists but keep counts."""
    graph_id = "g_summary_inspect"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    file_path = str((tmp_path / "src" / "small.py").resolve())
    module = CodeNode(
        id="mod2", graph_id=graph_id, labels=["CodeModule", "CodeFile"],
        properties={"name": "mod2", "file_path": file_path},
    )
    await store.add_nodes_batch(graph_id, [module])

    result = await registry.handle_inspect_file(
        InspectFileInput(file_path=file_path, graph_id=graph_id, summary_only=True)
    )

    assert result.nodes == [], "summary_only=True must omit the nodes list"
    assert result.edges == [], "summary_only=True must omit the edges list"
    assert result.total_nodes == 1, "summary_only=True must still report total_nodes"


async def test_inspect_file_include_edges_false_omits_edges(tmp_path: Path) -> None:
    """``include_edges=False`` must omit edges while still returning nodes."""
    graph_id = "g_no_edges_inspect"
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    file_path = str((tmp_path / "src" / "noedges.py").resolve())
    module = CodeNode(
        id="mod3", graph_id=graph_id, labels=["CodeModule", "CodeFile"],
        properties={"name": "mod3", "file_path": file_path},
    )
    member = CodeNode(
        id="member_x", graph_id=graph_id, labels=["CodeFunction"],
        properties={"name": "member_x", "file_path": file_path},
    )
    edge = CodeEdge(
        source="mod3", target="member_x", graph_id=graph_id, rel_type="CONTAINS",
        properties={"owner_file_path": file_path},
    )
    await store.add_nodes_batch(graph_id, [module, member])
    await store.add_edges_batch(graph_id, [edge])

    result = await registry.handle_inspect_file(
        InspectFileInput(file_path=file_path, graph_id=graph_id, include_edges=False)
    )

    assert len(result.nodes) == 2, "nodes must still be returned when include_edges=False"
    assert result.edges == [], "include_edges=False must omit the edges list"
    assert result.total_edges == 0, "include_edges=False must not count skipped edges as available"


async def test_find_hotspots_limit_and_offset(tmp_path: Path) -> None:
    """``top_n``/``offset`` on find_hotspots must page ranked results."""
    repo_path = str(tmp_path)
    graph_id = _graph_id_from_repo_path(repo_path)
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    # Chain: n0 -> n1 -> n2 -> n3 -> n4 gives each node a distinct fan_in.
    nodes = [
        CodeNode(id=f"n{i}", graph_id=graph_id, labels=["CodeFunction"], properties={"name": f"n{i}"})
        for i in range(5)
    ]
    edges = [
        CodeEdge(source=f"n{i}", target=f"n{i+1}", graph_id=graph_id, rel_type="CALLS")
        for i in range(4)
    ]
    await store.add_nodes_batch(graph_id, nodes)
    await store.add_edges_batch(graph_id, edges)

    page1 = await registry.handle_find_hotspots(
        FindHotspotsInput(repo_path=repo_path, top_n=2, offset=0, metric="fan_in")
    )
    assert len(page1.hotspots) == 2, f"expected 2 hotspots on page 1, got {len(page1.hotspots)}"
    assert page1.has_more is True

    page2 = await registry.handle_find_hotspots(
        FindHotspotsInput(repo_path=repo_path, top_n=2, offset=2, metric="fan_in")
    )
    assert len(page2.hotspots) == 2, f"expected 2 hotspots on page 2, got {len(page2.hotspots)}"

    page1_ids = {h.get("node_id") for h in page1.hotspots}
    page2_ids = {h.get("node_id") for h in page2.hotspots}
    assert page1_ids.isdisjoint(page2_ids), "offset must move the window, not repeat entries"


async def test_get_architecture_pagination_and_summary_only(tmp_path: Path) -> None:
    """``limit``/``offset``/``summary_only`` must page and trim the communities list."""
    repo_path = str(tmp_path)
    graph_id = _graph_id_from_repo_path(repo_path)
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    # Three disjoint pairs -> three separate communities under the
    # connected-component fallback.
    nodes = []
    edges = []
    for i in range(3):
        a, b = f"c{i}a", f"c{i}b"
        nodes.append(CodeNode(id=a, graph_id=graph_id, labels=["CodeFunction"], properties={"name": a}))
        nodes.append(CodeNode(id=b, graph_id=graph_id, labels=["CodeFunction"], properties={"name": b}))
        edges.append(CodeEdge(source=a, target=b, graph_id=graph_id, rel_type="CALLS"))
    await store.add_nodes_batch(graph_id, nodes)
    await store.add_edges_batch(graph_id, edges)

    paged = await registry.handle_get_architecture(
        GetArchitectureInput(repo_path=repo_path, limit=1, offset=0)
    )
    assert len(paged.summary.get("communities", [])) == 1, "limit=1 must cap communities to 1"
    assert paged.total == 3, f"expected total=3 communities, got {paged.total}"
    assert paged.has_more is True

    summarized = await registry.handle_get_architecture(
        GetArchitectureInput(repo_path=repo_path, summary_only=True)
    )
    assert summarized.summary.get("communities") == [], "summary_only=True must omit the communities list"
    assert summarized.total == 3, "summary_only=True must still report the total"


async def test_shared_outputs_carry_schema_version(tmp_path: Path) -> None:
    """Every shared output (retrieve/inspect_file/architecture/hotspots) carries schema_version."""
    graph_id = "g_schema_version"
    repo_path = str(tmp_path)
    store = MemoryGraphStore()
    registry = _make_registry(store, tmp_path)

    node = CodeNode(id="only_node", graph_id=graph_id, labels=["CodeFunction"], properties={"name": "only_node"})
    await store.add_nodes_batch(graph_id, [node])

    retrieve_result = await registry.handle_retrieve(
        RetrieveInput(query="only_node", graph_id=graph_id)
    )
    assert retrieve_result.schema_version, "RetrieveOutput must carry schema_version"

    inspect_result = await registry.handle_inspect_file(
        InspectFileInput(file_path="/does/not/exist.py", graph_id=graph_id)
    )
    assert inspect_result.schema_version, "InspectFileOutput must carry schema_version"

    hotspots_result = await registry.handle_find_hotspots(
        FindHotspotsInput(repo_path=repo_path)
    )
    assert hotspots_result.schema_version, "FindHotspotsOutput must carry schema_version"

    architecture_result = await registry.handle_get_architecture(
        GetArchitectureInput(repo_path=repo_path)
    )
    assert architecture_result.schema_version, "ArchitectureOutput must carry schema_version"
