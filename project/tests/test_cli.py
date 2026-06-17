"""Tests for the ariadne CLI."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ariadne_graph import cli
from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.memory import MemoryGraphStore


@pytest.fixture
def store() -> MemoryGraphStore:
    return MemoryGraphStore()


@pytest.fixture(autouse=True)
def patch_create_graph_store(
    monkeypatch: pytest.MonkeyPatch, store: MemoryGraphStore
) -> None:
    """Route all CLI graph-store creation to an isolated in-memory store."""
    monkeypatch.setattr(cli, "create_graph_store", lambda _config: store)


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a tiny Python repository for CLI commands."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("def hello():\n    return 42\n")
    return repo


def _run_json(args: list[str]) -> tuple[int, dict[str, Any]]:
    """Run the CLI with args and parse its JSON output."""
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        exit_code = cli.main(args)
        output = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout

    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"CLI output is not valid JSON: {exc}\n{output}") from exc
    return exit_code, data


def test_parser_knows_all_commands() -> None:
    """The argument parser exposes every registered command."""
    parser = cli._build_parser()
    subparser_action = next(
        action for action in parser._actions if getattr(action, "choices", None)
    )
    commands = set(subparser_action.choices)

    assert commands == set(cli._COMMAND_HANDLERS)


def test_main_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    """Running with no arguments prints help and exits 0."""
    exit_code = cli.main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "ariadne" in captured.out or "usage:" in captured.out


def test_main_unknown_command_exits_nonzero() -> None:
    """An unrecognised subcommand returns a non-zero exit code."""
    exit_code = cli.main(["does-not-exist"])
    assert exit_code != 0


def test_derive_graph_id(tmp_path: Path) -> None:
    """_derive_graph_id matches the SHA-256 short hash of the resolved path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    expected = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]

    assert cli._derive_graph_id(str(repo)) == expected


def test_cli_list_projects_empty() -> None:
    """list returns an empty project list when nothing is indexed."""
    exit_code, data = _run_json(["list"])

    assert exit_code == 0
    assert data["projects"] == []


def test_cli_index(tmp_repo: Path) -> None:
    """index populates the graph and reports success."""
    exit_code, data = _run_json(["index", str(tmp_repo)])

    assert exit_code == 0
    assert data["status"] == "success"
    assert data["files_indexed"] == 1
    assert data["graph_id"] == cli._derive_graph_id(str(tmp_repo))


def test_cli_status_after_index(tmp_repo: Path) -> None:
    """status reflects a freshly indexed repository."""
    cli.main(["index", str(tmp_repo)])

    exit_code, data = _run_json(["status", str(tmp_repo)])

    assert exit_code == 0
    assert data["file_count"] == 1
    assert data["graph_id"] == cli._derive_graph_id(str(tmp_repo))
    assert "indexed" in data["message"].lower()


def test_cli_status_not_indexed(tmp_path: Path) -> None:
    """status reports that an unindexed repository has not been indexed."""
    repo = tmp_path / "empty"
    repo.mkdir()

    exit_code, data = _run_json(["status", str(repo)])

    assert exit_code == 0
    assert data["file_count"] == 0
    assert "not been indexed" in data["message"]


def test_cli_delete_project(tmp_repo: Path) -> None:
    """delete removes a previously indexed project."""
    cli.main(["index", str(tmp_repo)])

    exit_code, data = _run_json(["delete", str(tmp_repo)])

    assert exit_code == 0
    assert data["deleted"] is True

    # After deletion status shows no data.
    _, status = _run_json(["status", str(tmp_repo)])
    assert "not been indexed" in status["message"]


def test_cli_retrieve_symbol(tmp_repo: Path, store: MemoryGraphStore) -> None:
    """retrieve returns a node matching the requested symbol."""
    graph_id = cli._derive_graph_id(str(tmp_repo))
    node = CodeNode(
        id="main.hello",
        graph_id=graph_id,
        labels=["CodeFunction", "KnowledgeNode"],
        properties={
            "name": "hello",
            "qualname": "main.hello",
            "file_path": str((tmp_repo / "main.py").resolve()),
            "line_start": 1,
            "line_end": 2,
        },
    )
    asyncio.run(store.add_nodes_batch(graph_id, [node]))
    asyncio.run(store.register_project(graph_id, str(tmp_repo.resolve()), file_count=1))

    exit_code, data = _run_json(["retrieve", str(tmp_repo), "main.hello"])

    assert exit_code == 0
    assert data["results"]
    # Results may be the canonical GraphRetriever shape or fallback scan items.
    node_ids = {
        item["data"].get("id")
        if item.get("type") != "retrieve"
        else item["data"].get("node", {}).get("id")
        for item in data["results"]
    }
    assert "main.hello" in node_ids


def test_cli_trace_dependencies(tmp_repo: Path, store: MemoryGraphStore) -> None:
    """trace reports dependency paths from a symbol."""
    graph_id = cli._derive_graph_id(str(tmp_repo))
    nodes = [
        CodeNode(
            id="main.hello",
            graph_id=graph_id,
            labels=["CodeFunction"],
            properties={"name": "hello"},
        ),
        CodeNode(
            id="main.callee",
            graph_id=graph_id,
            labels=["CodeFunction"],
            properties={"name": "callee"},
        ),
    ]
    edges = [
        CodeEdge(
            source="main.hello",
            target="main.callee",
            graph_id=graph_id,
            rel_type="CALLS",
        ),
    ]
    asyncio.run(store.add_nodes_batch(graph_id, nodes))
    asyncio.run(store.add_edges_batch(graph_id, edges))
    asyncio.run(store.register_project(graph_id, str(tmp_repo.resolve()), file_count=1))

    exit_code, data = _run_json(["trace", str(tmp_repo), "main.hello"])

    assert exit_code == 0
    assert len(data["paths"]) >= 1


def test_cli_impact_analysis(tmp_repo: Path, store: MemoryGraphStore) -> None:
    """impact returns affected symbols for a change."""
    graph_id = cli._derive_graph_id(str(tmp_repo))
    nodes = [
        CodeNode(id="main.hello", graph_id=graph_id, labels=["CodeFunction"]),
        CodeNode(id="main.callee", graph_id=graph_id, labels=["CodeFunction"]),
    ]
    edges = [
        CodeEdge(
            source="main.hello",
            target="main.callee",
            graph_id=graph_id,
            rel_type="CALLS",
        ),
    ]
    asyncio.run(store.add_nodes_batch(graph_id, nodes))
    asyncio.run(store.add_edges_batch(graph_id, edges))
    asyncio.run(store.register_project(graph_id, str(tmp_repo.resolve()), file_count=1))

    exit_code, data = _run_json(["impact", str(tmp_repo), "main.hello"])

    assert exit_code == 0
    assert data["target_symbol"] == "main.hello"
    assert "main.callee" in data["direct_dependencies"]


def test_cli_inspect_file(tmp_repo: Path, store: MemoryGraphStore) -> None:
    """inspect returns nodes for the requested file path."""
    graph_id = cli._derive_graph_id(str(tmp_repo))
    file_path = str((tmp_repo / "main.py").resolve())
    node = CodeNode(
        id="main.hello",
        graph_id=graph_id,
        labels=["CodeFunction"],
        properties={"name": "hello", "file_path": file_path},
    )
    asyncio.run(store.add_nodes_batch(graph_id, [node]))
    asyncio.run(store.register_project(graph_id, str(tmp_repo.resolve()), file_count=1))

    exit_code, data = _run_json(["inspect", str(tmp_repo), file_path])

    assert exit_code == 0
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["id"] == "main.hello"


def test_cli_diagnostics_not_indexed(tmp_path: Path) -> None:
    """diagnostics on an unindexed repo returns an empty list."""
    repo = tmp_path / "empty"
    repo.mkdir()

    exit_code, data = _run_json(["diagnostics", str(repo)])

    assert exit_code == 0
    assert data["diagnostics"] == []
    assert "not been indexed" in data["message"]


def test_cli_search_keyword(tmp_repo: Path, store: MemoryGraphStore) -> None:
    """search with a keyword query returns matching nodes."""
    graph_id = cli._derive_graph_id(str(tmp_repo))
    node = CodeNode(
        id="main.hello",
        graph_id=graph_id,
        labels=["CodeFunction"],
        properties={"name": "hello", "file_path": str((tmp_repo / "main.py").resolve())},
    )
    asyncio.run(store.add_nodes_batch(graph_id, [node]))
    asyncio.run(store.register_project(graph_id, str(tmp_repo.resolve()), file_count=1))

    exit_code, data = _run_json(["search", str(tmp_repo), "hello"])

    assert exit_code == 0
    assert len(data["matches"]) >= 1


def test_cli_communities_not_indexed(tmp_path: Path) -> None:
    """communities on an unindexed repo reports that fact."""
    repo = tmp_path / "empty"
    repo.mkdir()

    exit_code, data = _run_json(["communities", str(repo)])

    assert exit_code == 0
    assert data["communities"] == []
    assert "not been indexed" in data["message"]


def test_cli_hotspots_not_indexed(tmp_path: Path) -> None:
    """hotspots on an unindexed repo reports that fact."""
    repo = tmp_path / "empty"
    repo.mkdir()

    exit_code, data = _run_json(["hotspots", str(repo)])

    assert exit_code == 0
    assert data["hotspots"] == []
    assert "not been indexed" in data["message"]


def test_cli_changes_not_indexed(tmp_path: Path) -> None:
    """changes on an unindexed repo reports that fact."""
    repo = tmp_path / "empty"
    repo.mkdir()

    exit_code, data = _run_json(["changes", str(repo)])

    assert exit_code == 0
    assert data["added"] == []
    assert data["modified"] == []
    assert data["deleted"] == []
    assert "not been indexed" in data["message"]
