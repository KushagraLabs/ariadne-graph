"""Tests for FreshnessTracker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ariadne_graph.core.freshness import FreshnessTracker
from ariadne_graph.graphstores.memory import MemoryGraphStore


@pytest.fixture
def store() -> MemoryGraphStore:
    return MemoryGraphStore()


@pytest.fixture
def tracker(store: MemoryGraphStore) -> FreshnessTracker:
    return FreshnessTracker(store)


@pytest.mark.asyncio
async def test_get_status_empty_graph(tracker: FreshnessTracker) -> None:
    """Status for an unknown graph reports zero state."""
    status = await tracker.get_status("unknown-graph")

    assert status.graph_id == "unknown-graph"
    assert status.repo_path == ""
    assert status.last_indexed is None
    assert status.file_count == 0
    assert status.dirty_files == []
    assert status.sync_enabled is False


@pytest.mark.asyncio
async def test_mark_indexed_records_metadata(
    tracker: FreshnessTracker, store: MemoryGraphStore
) -> None:
    """mark_indexed writes project metadata into the graph store."""
    repo = Path("/tmp/repo").resolve()
    await tracker.mark_indexed("g1", str(repo), file_count=5, sync_enabled=True)

    rows = await store.query("g1", "index_metadata")
    assert len(rows) == 1
    assert rows[0]["repo_path"] == str(repo)
    assert rows[0]["file_count"] == 5
    assert rows[0]["sync_enabled"] is True
    assert rows[0]["last_indexed"] is not None


@pytest.mark.asyncio
async def test_mark_indexed_counts_files_from_nodes(
    tracker: FreshnessTracker, store: MemoryGraphStore
) -> None:
    """When file_count is omitted it is derived from distinct node file_path values."""
    from ariadne_graph.core.models import CodeNode

    nodes = [
        CodeNode(
            id="a",
            graph_id="g1",
            labels=["CodeFunction"],
            properties={"file_path": "/repo/a.py"},
        ),
        CodeNode(
            id="b",
            graph_id="g1",
            labels=["CodeFunction"],
            properties={"file_path": "/repo/b.py"},
        ),
        CodeNode(
            id="c",
            graph_id="g1",
            labels=["CodeFunction"],
            properties={"file_path": "/repo/a.py"},
        ),
    ]
    await store.add_nodes_batch("g1", nodes)

    await tracker.mark_indexed("g1", "/repo")

    rows = await store.query("g1", "index_metadata")
    assert rows[0]["file_count"] == 2


@pytest.mark.asyncio
async def test_get_status_returns_repo_path_override(
    tracker: FreshnessTracker, store: MemoryGraphStore
) -> None:
    """An explicit repo_path override is returned in the status."""
    await store.register_project("g1", "/stored/repo", file_count=1)

    status = await tracker.get_status("g1", repo_path="/override/repo")

    assert status.repo_path == "/override/repo"


@pytest.mark.asyncio
async def test_dirty_files_detect_content_change(
    tracker: FreshnessTracker, store: MemoryGraphStore, tmp_path: Path
) -> None:
    """Files whose content hash differs from the stored hash are dirty."""
    repo = tmp_path / "repo"
    repo.mkdir()
    file = repo / "module.py"
    file.write_text("x = 1\n")

    file_path = str(file.resolve())
    await store.update_hash("g1", file_path, "stale-hash")

    dirty = await tracker._compute_dirty_files("g1", str(repo.resolve()))

    assert dirty == [file_path]


@pytest.mark.asyncio
async def test_dirty_files_missing_file_reported(
    tracker: FreshnessTracker, store: MemoryGraphStore, tmp_path: Path
) -> None:
    """A stored file that no longer exists on disk is reported as dirty."""
    repo = tmp_path / "repo"
    repo.mkdir()
    missing_path = str((repo / "missing.py").resolve())

    await store.update_hash("g1", missing_path, "hash")

    dirty = await tracker._compute_dirty_files("g1", str(repo.resolve()))

    assert dirty == [missing_path]


@pytest.mark.asyncio
async def test_dirty_files_unchanged_not_reported(
    tracker: FreshnessTracker, store: MemoryGraphStore, tmp_path: Path
) -> None:
    """Files whose current hash matches the stored hash are not dirty."""
    import xxhash

    repo = tmp_path / "repo"
    repo.mkdir()
    file = repo / "module.py"
    file.write_text("x = 1\n")

    file_path = str(file.resolve())
    current_hash = xxhash.xxh3_64_hexdigest(file.read_bytes())
    await store.update_hash("g1", file_path, current_hash)

    dirty = await tracker._compute_dirty_files("g1", str(repo.resolve()))

    assert dirty == []


@pytest.mark.asyncio
async def test_get_status_computes_dirty_files(
    tracker: FreshnessTracker, store: MemoryGraphStore, tmp_path: Path
) -> None:
    """get_status combines metadata with dirty-file detection."""
    repo = tmp_path / "repo"
    repo.mkdir()
    file = repo / "module.py"
    file.write_text("x = 1\n")

    file_path = str(file.resolve())
    await store.update_hash("g1", file_path, "stale-hash")
    await store.register_project("g1", str(repo.resolve()), file_count=1)

    status = await tracker.get_status("g1")

    assert status.repo_path == str(repo.resolve())
    assert status.dirty_files == [file_path]


@pytest.mark.asyncio
async def test_is_fresh_true_for_recent_index(
    tracker: FreshnessTracker) -> None:
    """is_fresh returns True when the index is within the age window."""
    await tracker.mark_indexed("g1", "/repo", file_count=1)

    assert await tracker.is_fresh("g1", max_age_seconds=3600) is True


@pytest.mark.asyncio
async def test_is_fresh_false_for_old_index(
    tracker: FreshnessTracker, store: MemoryGraphStore
) -> None:
    """is_fresh returns False for a timestamp outside the age window."""
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    await store.query(
        "g1",
        "set_index_metadata",
        {"repo_path": "/repo", "last_indexed": old, "file_count": 1},
    )

    assert await tracker.is_fresh("g1", max_age_seconds=3600) is False


@pytest.mark.asyncio
async def test_is_fresh_false_when_not_indexed(
    tracker: FreshnessTracker) -> None:
    """is_fresh returns False when no last_indexed metadata exists."""
    assert await tracker.is_fresh("unknown", max_age_seconds=3600) is False


@pytest.mark.asyncio
async def test_is_fresh_false_for_invalid_timestamp(
    tracker: FreshnessTracker, store: MemoryGraphStore
) -> None:
    """is_fresh returns False when the stored timestamp is malformed."""
    await store.query(
        "g1",
        "set_index_metadata",
        {"repo_path": "/repo", "last_indexed": "not-a-timestamp", "file_count": 1},
    )

    assert await tracker.is_fresh("g1", max_age_seconds=3600) is False


@pytest.mark.asyncio
async def test_get_file_hashes(
    tracker: FreshnessTracker, store: MemoryGraphStore
) -> None:
    """get_file_hashes returns the stored path-to-hash mapping."""
    await store.update_hash("g1", "/repo/a.py", "hash_a")
    await store.update_hash("g1", "/repo/b.py", "hash_b")

    hashes = await tracker.get_file_hashes("g1")

    assert hashes == {"/repo/a.py": "hash_a", "/repo/b.py": "hash_b"}
