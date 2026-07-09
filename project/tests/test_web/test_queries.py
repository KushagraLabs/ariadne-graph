"""Unit tests for the browser-view query layer (``web/queries.py``).

Hard case first (per verification-first workflow): two graphs share one physical
``graph.db``. Every viewer query MUST scope by ``graph_id`` — a folder/file query
for graph A must never leak graph B's nodes. A fuzzy implementation that forgets
the ``graph_id`` predicate gets this wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.models import CodeNode
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.web import queries


@pytest.fixture
async def store(tmp_path: Path) -> SQLiteGraphStore:
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    try:
        yield store
    finally:
        await store.close()


def _file(node_id: str, graph_id: str, path: str) -> CodeNode:
    return CodeNode(
        id=node_id,
        graph_id=graph_id,
        labels=["KnowledgeNode", "CodeFile"],
        properties={"name": Path(path).name, "file_path": path},
    )


async def test_folders_scoped_by_graph_id(store: SQLiteGraphStore) -> None:
    """The hard case: graph A's folder list must exclude graph B's files."""
    repo_a = "/Users/x/Documents/repo_a"
    repo_b = "/Users/x/Documents/repo_b"
    await store.add_nodes_batch(
        "graphA",
        [
            _file("a1", "graphA", f"{repo_a}/src/one.py"),
            _file("a2", "graphA", f"{repo_a}/src/two.py"),
            _file("a3", "graphA", f"{repo_a}/tests/test_one.py"),
        ],
    )
    await store.add_nodes_batch(
        "graphB",
        [
            _file("b1", "graphB", f"{repo_b}/lib/other.py"),
            _file("b2", "graphB", f"{repo_b}/lib/more.py"),
        ],
    )

    folders_a = await queries.list_folders(store, "graphA", repo_root=repo_a)
    names = {f["folder"] for f in folders_a}

    # graphA has exactly src/ (2 files) and tests/ (1 file)
    assert names == {"src", "tests"}
    # graphB's lib/ must NOT appear
    assert "lib" not in names
    counts = {f["folder"]: f["file_count"] for f in folders_a}
    assert counts["src"] == 2
    assert counts["tests"] == 1


async def test_files_in_folder_scoped(store: SQLiteGraphStore) -> None:
    """File-altitude query is scoped to the folder subtree AND the graph."""
    repo_a = "/Users/x/Documents/repo_a"
    repo_b = "/Users/x/Documents/repo_b"
    await store.add_nodes_batch(
        "graphA",
        [
            _file("a1", "graphA", f"{repo_a}/src/one.py"),
            _file("a2", "graphA", f"{repo_a}/src/sub/two.py"),
            _file("a3", "graphA", f"{repo_a}/tests/t.py"),
        ],
    )
    await store.add_nodes_batch(
        "graphB",
        [_file("b1", "graphB", f"{repo_b}/src/leak.py")],
    )

    files = await queries.list_files(store, "graphA", repo_root=repo_a, folder="src")
    paths = {f["file_path"] for f in files}
    # src/ subtree of graphA only (both one.py and sub/two.py), never graphB's src/leak.py
    assert paths == {f"{repo_a}/src/one.py", f"{repo_a}/src/sub/two.py"}


async def test_files_carry_diagnostic_level(store: SQLiteGraphStore) -> None:
    """Hygiene paint: diagnostics attach to SYMBOLS carrying file_path (the real
    schema — HAS_DIAGNOSTIC never targets a CodeFile), aggregated to the file by
    path. A file with a warning + info reports worst_level=warning, count=2.
    """
    repo = "/Users/x/Documents/repo_a"
    await store.add_nodes_batch(
        "g",
        [
            _file("f1", "g", f"{repo}/src/one.py"),
            # two CodeDiagnostic nodes, both pointing at one.py via file_path
            CodeNode(
                id="d1", graph_id="g", labels=["KnowledgeNode", "CodeDiagnostic"],
                properties={"level": "warning", "rule": "complex_function",
                            "file_path": f"{repo}/src/one.py"},
            ),
            CodeNode(
                id="d2", graph_id="g", labels=["KnowledgeNode", "CodeDiagnostic"],
                properties={"level": "info", "rule": "long_line",
                            "file_path": f"{repo}/src/one.py"},
            ),
        ],
    )

    files = await queries.list_files(store, "g", repo_root=repo, folder="src")
    one = next(f for f in files if f["file_path"] == f"{repo}/src/one.py")
    assert one["diagnostic_count"] == 2
    assert one["worst_level"] == "warning"


async def test_mixed_absolute_and_relative_paths(store: SQLiteGraphStore) -> None:
    """Real-world trap: one graph stores some file_paths absolute, some relative
    (different languages/indexers). Both must bucket into the same folder.
    """
    repo = "/Users/x/Documents/repo_a"
    await store.add_nodes_batch(
        "g",
        [
            _file("f1", "g", f"{repo}/server/index.ts"),   # absolute
            _file("f2", "g", "server/routes.ts"),          # relative
            _file("f3", "g", "web/app.tsx"),               # relative, other folder
        ],
    )
    folders = await queries.list_folders(store, "g", repo_root=repo)
    counts = {f["folder"]: f["file_count"] for f in folders}
    assert counts.get("server") == 2  # absolute + relative both counted
    assert counts.get("web") == 1

    files = await queries.list_files(store, "g", repo_root=repo, folder="server")
    paths = {f["file_path"] for f in files}
    assert paths == {f"{repo}/server/index.ts", "server/routes.ts"}


async def test_cap_and_truncation(store: SQLiteGraphStore) -> None:
    """No silent capping: over-limit results report a truncated count."""
    repo = "/Users/x/Documents/repo_a"
    nodes = [_file(f"f{i}", "g", f"{repo}/src/file{i}.py") for i in range(10)]
    await store.add_nodes_batch("g", nodes)

    result = await queries.list_files(store, "g", repo_root=repo, folder="src", limit=4)
    assert len(result) == 4  # capped
    # the query layer exposes truncation via a sibling call
    total = await queries.count_files(store, "g", repo_root=repo, folder="src")
    assert total == 10
