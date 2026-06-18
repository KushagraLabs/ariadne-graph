"""Tests for AnalyzerConfig and environment parsing helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig, _env_bool, _env_list


class TestEnvHelpers:
    """Unit tests for the environment variable parsers."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("true", True),
            ("True", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("false", False),
            ("False", False),
            ("0", False),
            ("no", False),
            ("off", False),
            ("maybe", None),
            ("", None),
            (None, None),
        ],
    )
    def test_env_bool(self, value: str | None, expected: bool | None) -> None:
        assert _env_bool(value) == expected

    def test_env_list_empty(self) -> None:
        assert _env_list("") == []
        assert _env_list(None) == []  # type: ignore[arg-type]

    def test_env_list_parses_comma_separated(self) -> None:
        assert _env_list("a, b, c") == ["a", "b", "c"]


class TestAnalyzerConfig:
    """Unit tests for AnalyzerConfig."""

    def test_graph_id_derived_from_repo_root(self, tmp_path: Path) -> None:
        config = AnalyzerConfig(repo_root=tmp_path)
        expected = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:16]
        assert config.graph_id == expected

    def test_graph_id_preserved_when_provided(self, tmp_path: Path) -> None:
        config = AnalyzerConfig(repo_root=tmp_path, graph_id="custom-id")
        assert config.graph_id == "custom-id"

    def test_resolved_repo_root(self, tmp_path: Path) -> None:
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.resolved_repo_root == tmp_path.resolve()

    def test_default_ignore_patterns(self, tmp_path: Path) -> None:
        config = AnalyzerConfig(repo_root=tmp_path)
        assert ".git" in config.ignore_patterns
        assert ".claude" in config.ignore_patterns

    def test_env_embedding_provider(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIADNE_EMBEDDING_PROVIDER", "openai")
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.embedding_provider == "openai"

    def test_env_db_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIADNE_DB", "/tmp/ariadne.db")
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.db_path == "/tmp/ariadne.db"

    def test_env_neo4j_uri(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIADNE_NEO4J_URI", "bolt://neo4j:7687")
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.neo4j_uri == "bolt://neo4j:7687"

    def test_env_scip_typescript_enabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIADNE_SCIP_TYPESCRIPT_ENABLED", "true")
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.scip_typescript_enabled is True

    def test_env_scip_typescript_args(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIADNE_SCIP_TYPESCRIPT_ARGS", "--yarn-workspaces, --infer-tsconfig")
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.scip_typescript_args == ["--yarn-workspaces", "--infer-tsconfig"]

    def test_lumen_enabled_by_provider(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LUMEN_CODE_GRAPH_PROVIDER", "standalone")
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.lumen_enabled is True

    def test_lumen_disabled_by_default(self, tmp_path: Path) -> None:
        config = AnalyzerConfig(repo_root=tmp_path)
        assert config.lumen_enabled is False
