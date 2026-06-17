"""Tests for IncrementalSync change detection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.incremental_sync import IncrementalSync
from ariadne_graph.graphstores.memory import MemoryGraphStore


def _init_git_repo(repo_root: Path) -> None:
    """Initialize a git repo with a test user configured."""
    subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def _commit_all(repo_root: Path, message: str) -> None:
    """Stage and commit all changes in a git repo."""
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_detect_changes_against_git_ref(tmp_path: Path) -> None:
    """Classify added, modified, and deleted files relative to a git ref."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    old_file = repo / "old.py"
    stable_file = repo / "stable.py"
    old_file.write_text("x = 1\n")
    stable_file.write_text("y = 2\n")
    _commit_all(repo, "initial commit")

    # Working-tree changes: modify stable, add new, delete old.
    stable_file.write_text("y = 3\n")
    new_file = repo / "new.py"
    new_file.write_text("z = 4\n")
    old_file.unlink()

    config = AnalyzerConfig(repo_root=repo)
    store = MemoryGraphStore()
    sync = IncrementalSync(store, config)
    graph_id = config.graph_id
    assert graph_id is not None

    # Seed stored hashes so the files look previously indexed.
    await store.update_hash(graph_id, str(stable_file.resolve()), "stale_hash")
    await store.update_hash(graph_id, str(old_file.resolve()), "stale_hash")

    discovered = [stable_file, new_file]
    report = await sync.get_changed_files_since_ref(graph_id, discovered, "HEAD")

    assert report.comparison_mode == "git_ref"
    assert report.resolved_ref is not None
    assert len(report.resolved_ref) == 40
    assert str(new_file.resolve()) in report.added
    assert str(stable_file.resolve()) in report.modified
    assert str(old_file.resolve()) in report.deleted


@pytest.mark.asyncio
async def test_detect_changes_untracked_fallback(tmp_path: Path) -> None:
    """Untracked files fall back to stored-hash comparison within a git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    tracked_file = repo / "tracked.py"
    tracked_file.write_text("a = 1\n")
    _commit_all(repo, "initial commit")

    untracked_file = repo / "untracked.py"
    untracked_file.write_text("b = 1\n")

    config = AnalyzerConfig(repo_root=repo)
    store = MemoryGraphStore()
    sync = IncrementalSync(store, config)
    graph_id = config.graph_id
    assert graph_id is not None

    old_hash = await sync.compute_file_hash(untracked_file)
    await store.update_hash(graph_id, str(untracked_file.resolve()), old_hash)

    untracked_file.write_text("b = 2\n")

    discovered = [tracked_file, untracked_file]
    report = await sync.get_changed_files_since_ref(graph_id, discovered, "HEAD")

    assert report.comparison_mode == "git_ref"
    assert str(untracked_file.resolve()) in report.modified
    assert str(tracked_file.resolve()) not in report.added
    assert str(tracked_file.resolve()) not in report.modified
    assert str(tracked_file.resolve()) not in report.deleted


@pytest.mark.asyncio
async def test_detect_changes_non_git_fallback(tmp_path: Path) -> None:
    """A non-git repo falls back to stored-hash comparison."""
    repo = tmp_path / "repo"
    repo.mkdir()
    file = repo / "module.py"
    file.write_text("x = 1\n")

    config = AnalyzerConfig(repo_root=repo)
    store = MemoryGraphStore()
    sync = IncrementalSync(store, config)
    graph_id = config.graph_id
    assert graph_id is not None

    old_hash = await sync.compute_file_hash(file)
    await store.update_hash(graph_id, str(file.resolve()), old_hash)

    file.write_text("x = 2\n")

    report = await sync.get_changed_files_since_ref(graph_id, [file], "HEAD")

    assert report.comparison_mode == "stored_hash"
    assert "Git repository not found" in report.message
    assert str(file.resolve()) in report.modified


@pytest.mark.asyncio
async def test_detect_changes_invalid_ref(tmp_path: Path) -> None:
    """An invalid git ref returns empty lists and a clear message."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    file = repo / "module.py"
    file.write_text("x = 1\n")
    _commit_all(repo, "initial commit")

    config = AnalyzerConfig(repo_root=repo)
    store = MemoryGraphStore()
    sync = IncrementalSync(store, config)
    graph_id = config.graph_id
    assert graph_id is not None

    report = await sync.get_changed_files_since_ref(
        graph_id, [file], "this-ref-does-not-exist"
    )

    assert report.comparison_mode == "stored_hash"
    assert "Invalid git ref" in report.message
    assert report.added == []
    assert report.modified == []
    assert report.deleted == []


@pytest.mark.asyncio
async def test_detect_changes_renamed_file(tmp_path: Path) -> None:
    """A renamed file is reported as deleted (old path) and added (new path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    old_file = repo / "old.py"
    old_file.write_text("x = 1\n")
    _commit_all(repo, "initial commit")

    new_file = repo / "new.py"
    old_file.rename(new_file)

    config = AnalyzerConfig(repo_root=repo)
    store = MemoryGraphStore()
    sync = IncrementalSync(store, config)
    graph_id = config.graph_id
    assert graph_id is not None

    await store.update_hash(graph_id, str(old_file.resolve()), "stale_hash")

    discovered = [new_file]
    report = await sync.get_changed_files_since_ref(graph_id, discovered, "HEAD")

    assert report.comparison_mode == "git_ref"
    assert str(new_file.resolve()) in report.added
    assert str(old_file.resolve()) in report.deleted
