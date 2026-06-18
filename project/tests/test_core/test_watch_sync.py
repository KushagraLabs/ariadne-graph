"""Tests for event-driven watch sync manager."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import IndexInput
from ariadne_graph.mcp.tools import ToolRegistry

pytest.importorskip("watchdog")

from ariadne_graph.core.watch_sync import WatchSyncManager  # noqa: E402


async def test_watch_sync_triggers_targeted_sync(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    module = repo / "module.py"
    module.write_text("def hello(): pass\n")

    config = AnalyzerConfig(
        repo_root=repo,
        auto_sync=True,
        watch_mode="auto",
        watch_debounce=0.2,
    )
    store = MemoryGraphStore()
    registry = ToolRegistry(
        graph_store=store,
        searchable_store=None,
        adapters={"python": PythonLanguageAdapter()},
        config=config,
    )

    # Initial full index registers the project.
    result = await registry.handle_index(IndexInput(repo_path=str(repo)))
    assert result.status in ("success", "partial")

    manager = WatchSyncManager(registry)
    await manager.start()
    try:
        # Wait for watcher to start and settle.
        await asyncio.sleep(0.5)
        module.write_text("def hello():\n    return 1\n")
        # Give the watcher time to detect, debounce, and sync.
        await asyncio.sleep(1.5)
    finally:
        await manager.stop()

    # The targeted sync should have updated the graph metadata.
    projects = await store.list_projects()
    assert len(projects) == 1
    assert projects[0].get("sync_enabled") is True


async def test_watch_sync_falls_back_when_watch_mode_is_poll(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("def hello(): pass\n")

    config = AnalyzerConfig(
        repo_root=repo,
        auto_sync=True,
        watch_mode="poll",
        incremental_sync_interval=0.05,
    )
    store = MemoryGraphStore()
    registry = ToolRegistry(
        graph_store=store,
        searchable_store=None,
        adapters={"python": PythonLanguageAdapter()},
        config=config,
    )

    await registry.handle_index(IndexInput(repo_path=str(repo)))
    await registry.start_auto_sync()
    try:
        await asyncio.sleep(0.15)
    finally:
        await registry.stop_auto_sync()

    # The polling manager should have run at least once.
    projects = await store.list_projects()
    assert len(projects) == 1
