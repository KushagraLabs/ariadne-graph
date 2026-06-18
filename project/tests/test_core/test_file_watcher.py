"""Tests for the filesystem watcher used in event-driven auto-sync."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig

pytest.importorskip("watchdog")

from ariadne_graph.core.file_watcher import FileWatcher  # noqa: E402


async def _wait_for_changes(watcher: FileWatcher, timeout: float = 5.0) -> set[Path]:
    """Helper to collect the first batch of changes with a timeout."""
    return await asyncio.wait_for(watcher.wait_for_changes(), timeout=timeout)


@pytest.fixture
def watcher_config(tmp_path: Path) -> AnalyzerConfig:
    """Return a config rooted at tmp_path with a short debounce."""
    return AnalyzerConfig(
        repo_root=tmp_path,
        watch_debounce=0.2,
        ignore_patterns=[".git", "__pycache__", "node_modules", ".venv"],
    )


async def test_watcher_detects_file_modification(
    tmp_path: Path, watcher_config: AnalyzerConfig
) -> None:
    source = tmp_path / "module.py"
    source.write_text("def foo(): pass\n")

    watcher = FileWatcher(watcher_config, extensions={".py"})
    await watcher.start(tmp_path)
    try:
        # Allow the observer to settle.
        await asyncio.sleep(0.3)
        source.write_text("def foo():\n    return 1\n")
        changed = await _wait_for_changes(watcher)
        assert any(p.name == "module.py" for p in changed)
    finally:
        await watcher.stop()


async def test_watcher_ignores_non_source_extensions(
    tmp_path: Path, watcher_config: AnalyzerConfig
) -> None:
    text = tmp_path / "readme.txt"
    text.write_text("hello\n")

    watcher = FileWatcher(watcher_config, extensions={".py"})
    await watcher.start(tmp_path)
    try:
        await asyncio.sleep(0.3)
        text.write_text("hello world\n")
        # No .txt event should be emitted; use a short timeout.
        with pytest.raises(TimeoutError):
            await _wait_for_changes(watcher, timeout=0.6)
    finally:
        await watcher.stop()


async def test_watcher_ignores_ignored_directories(
    tmp_path: Path, watcher_config: AnalyzerConfig
) -> None:
    ignored_dir = tmp_path / "node_modules"
    ignored_dir.mkdir()
    ignored_file = ignored_dir / "pkg.py"
    ignored_file.write_text("x = 1\n")

    watcher = FileWatcher(watcher_config, extensions={".py"})
    await watcher.start(tmp_path)
    try:
        await asyncio.sleep(0.3)
        ignored_file.write_text("x = 2\n")
        with pytest.raises(TimeoutError):
            await _wait_for_changes(watcher, timeout=0.6)
    finally:
        await watcher.stop()


async def test_watcher_debounces_multiple_changes(
    tmp_path: Path, watcher_config: AnalyzerConfig
) -> None:
    source = tmp_path / "module.py"
    source.write_text("a = 1\n")

    watcher = FileWatcher(watcher_config, extensions={".py"})
    await watcher.start(tmp_path)
    try:
        await asyncio.sleep(0.3)
        # Multiple rapid writes should be batched into one emission.
        for i in range(5):
            source.write_text(f"a = {i}\n")
            await asyncio.sleep(0.05)

        changed = await _wait_for_changes(watcher)
        assert any(p.name == "module.py" for p in changed)
    finally:
        await watcher.stop()


async def test_watcher_available_reflects_import_status() -> None:
    # watchdog was imported at the top via importorskip, so it should be True.
    assert FileWatcher.is_available() is True
