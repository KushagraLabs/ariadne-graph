"""Tests for the Lumen compatibility graph-store adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.lumen import LumenGraphStore
from ariadne_graph.graphstores.memory import MemoryGraphStore


@pytest.fixture
def delegate() -> MemoryGraphStore:
    return MemoryGraphStore()


@pytest.fixture
def sample_node() -> CodeNode:
    return CodeNode(
        id="mod:func:foo",
        graph_id="g1",
        labels=["KnowledgeNode", "CodeFunction"],
        properties={"name": "foo", "file_path": "/workspace/proj/src/mod.py"},
    )


async def test_lumen_graph_store_forwards_nodes(delegate: MemoryGraphStore, sample_node: CodeNode) -> None:
    lumen = LumenGraphStore(delegate)
    await lumen.add_nodes_batch("g1", [sample_node])

    rows = await lumen.query("g1", "nodes")
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod:func:foo"


async def test_lumen_aliases(delegate: MemoryGraphStore, sample_node: CodeNode) -> None:
    lumen = LumenGraphStore(delegate)
    await lumen.add_nodes_batch("g1", [sample_node])

    rows = await lumen.query("g1", "lumen_nodes")
    assert len(rows) == 1
    assert rows[0]["n"]["id"] == "mod:func:foo"


async def test_workspace_root_restricts_registration(delegate: MemoryGraphStore) -> None:
    workspace = Path("/workspace")
    lumen = LumenGraphStore(delegate, workspace_root=workspace)

    await lumen.register_project("g1", "/workspace/proj")
    assert len(await delegate.list_projects()) == 1

    await lumen.register_project("g2", "/other/proj")
    # Outside workspace root should be rejected.
    assert len(await delegate.list_projects()) == 1


async def test_lumen_metadata_on_projects(delegate: MemoryGraphStore) -> None:
    lumen = LumenGraphStore(
        delegate, workspace_root=Path("/workspace"), workspace_id="ws-42"
    )
    await lumen.register_project("g1", "/workspace/proj", file_count=7)

    projects = await lumen.list_projects()
    assert len(projects) == 1
    assert projects[0]["lumen_workspace_id"] == "ws-42"
    assert projects[0]["lumen_workspace_root"] == str(Path("/workspace").resolve())


async def test_lumen_forwards_edges(delegate: MemoryGraphStore, sample_node: CodeNode) -> None:
    lumen = LumenGraphStore(delegate)
    edge = CodeEdge(
        source="mod:func:foo",
        target="mod:func:bar",
        graph_id="g1",
        rel_type="CALLS",
    )
    await lumen.add_nodes_batch("g1", [sample_node])
    await lumen.add_edges_batch("g1", [edge])

    rows = await lumen.query("g1", "lumen_edges")
    assert len(rows) == 1
    assert rows[0]["r"]["rel_type"] == "CALLS"


async def test_lumen_searchable_forwarding(delegate: MemoryGraphStore) -> None:
    """MemoryGraphStore is not searchable, so searchable methods return empty."""
    lumen = LumenGraphStore(delegate)
    assert await lumen.search_vector("g1", [1.0, 2.0]) == []
    assert await lumen.search_keyword("g1", "foo") == []
    assert await lumen.get_communities("g1") == {}
