"""Tests for graph store factory backend selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.graphstores.factory import create_graph_store
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore


class TestCreateGraphStore:
    """Tests for create_graph_store backend selection."""

    def test_defaults_to_sqlite(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        config = AnalyzerConfig(repo_root=repo)
        store = create_graph_store(config)
        assert isinstance(store, SQLiteGraphStore)

    def test_neo4j_env_selects_neo4j_if_driver_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("neo4j")
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("ARIADNE_NEO4J_URI", "bolt://test:7687")
        config = AnalyzerConfig(repo_root=repo)
        store = create_graph_store(config)
        # The factory instantiates Neo4jGraphStore when the URI is configured and
        # the driver is importable; it does not validate server reachability here.
        assert type(store).__name__ == "Neo4jGraphStore"

    def test_explicit_db_path_used_by_sqlite(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        db_path = str(tmp_path / "custom.db")
        config = AnalyzerConfig(repo_root=repo, db_path=db_path)
        store = create_graph_store(config)
        assert isinstance(store, SQLiteGraphStore)

    def test_lumen_wrapper_when_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("ariadne_graph.graphstores.lumen")
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv("LUMEN_CODE_GRAPH_PROVIDER", "standalone")
        config = AnalyzerConfig(repo_root=repo)
        store = create_graph_store(config)
        assert type(store).__name__ == "LumenGraphStore"
