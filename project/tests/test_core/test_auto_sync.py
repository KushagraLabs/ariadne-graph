"""Tests for AutoSyncManager background polling."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ariadne_graph.core.auto_sync import AutoSyncManager
from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import IndexInput, IndexOutput
from ariadne_graph.mcp.tools import ToolRegistry


class _FakeRegistry:
    """Minimal fake registry for AutoSyncManager tests."""

    def __init__(self) -> None:
        self.graph_store = MemoryGraphStore()
        self.calls: list[IndexInput] = []

    async def handle_index(self, input: IndexInput) -> IndexOutput:
        self.calls.append(input)
        return IndexOutput(status="success", files_indexed=1, graph_id="g1", message="ok")


async def test_manager_polls_registered_projects() -> None:
    registry = _FakeRegistry()
    await registry.graph_store.register_project("g1", "/repo/a", file_count=1)

    manager = AutoSyncManager(registry, interval=0.05)  # type: ignore[arg-type]
    await manager.start()
    await asyncio.sleep(0.12)
    await manager.stop()

    assert len(registry.calls) >= 1
    assert registry.calls[0].repo_path == "/repo/a"


async def test_manager_does_not_start_twice() -> None:
    registry = _FakeRegistry()
    manager = AutoSyncManager(registry, interval=1.0)  # type: ignore[arg-type]
    await manager.start()
    await manager.start()  # should log warning but not crash
    await manager.stop()


async def test_stop_is_idempotent() -> None:
    registry = _FakeRegistry()
    manager = AutoSyncManager(registry, interval=1.0)  # type: ignore[arg-type]
    await manager.stop()
    assert manager._task is None


async def test_auto_sync_with_real_registry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("def hello(): pass\n")

    config = AnalyzerConfig(repo_root=repo, auto_sync=True, incremental_sync_interval=0.05)
    store = MemoryGraphStore()
    registry = ToolRegistry(
        graph_store=store,
        searchable_store=None,
        adapters={"python": PythonLanguageAdapter()},
        config=config,
    )

    # Initial index registers the project
    result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    assert result.status in ("success", "partial")

    await registry.start_auto_sync()
    await asyncio.sleep(0.12)
    await registry.stop_auto_sync()

    projects = await store.list_projects()
    assert len(projects) >= 1
