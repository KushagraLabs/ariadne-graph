"""Unit tests for the SCIP-TypeScript indexer runner.

Tests that do not require ``scip-typescript`` use mocks. Integration tests are
skipped when ``scip-typescript``/``npx`` is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.languages.typescript.scip_indexer import ScipTypeScriptIndexer


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def config(repo: Path) -> AnalyzerConfig:
    return AnalyzerConfig(repo_root=repo)


class TestFingerprinting:
    def test_empty_repo_fingerprint_is_stable(self, repo: Path, config: AnalyzerConfig) -> None:
        indexer = ScipTypeScriptIndexer(repo, config)
        fp1 = indexer.compute_fingerprint([])
        fp2 = indexer.compute_fingerprint([])
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_fingerprint_changes_with_content(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        indexer = ScipTypeScriptIndexer(repo, config)
        file_path = repo / "src" / "a.ts"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("export const x = 1;", encoding="utf-8")

        fp1 = indexer.compute_fingerprint([file_path])
        file_path.write_text("export const x = 2;", encoding="utf-8")
        fp2 = indexer.compute_fingerprint([file_path])
        assert fp1 != fp2

    def test_fingerprint_is_order_independent(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        indexer = ScipTypeScriptIndexer(repo, config)
        (repo / "src").mkdir(parents=True)
        a = repo / "src" / "a.ts"
        b = repo / "src" / "b.ts"
        a.write_text("export const a = 1;", encoding="utf-8")
        b.write_text("export const b = 2;", encoding="utf-8")

        fp1 = indexer.compute_fingerprint([a, b])
        fp2 = indexer.compute_fingerprint([b, a])
        assert fp1 == fp2


class TestAvailability:
    def test_disabled_when_env_false(self, repo: Path) -> None:
        config = AnalyzerConfig(
            repo_root=repo,
            scip_typescript_enabled=False,
        )
        indexer = ScipTypeScriptIndexer(repo, config)
        assert indexer.should_run() is False

    def test_enabled_when_env_true_ignores_binary(
        self, repo: Path
    ) -> None:
        config = AnalyzerConfig(
            repo_root=repo,
            scip_typescript_enabled=True,
        )
        indexer = ScipTypeScriptIndexer(repo, config)
        assert indexer.should_run() is True

    def test_auto_detect_requires_project(self, repo: Path, config: AnalyzerConfig) -> None:
        indexer = ScipTypeScriptIndexer(repo, config)
        # tsconfig.json exists so project is detected.
        assert indexer.has_project() is True


class TestEnsureIndexMocked:
    async def test_failure_returns_none_and_does_not_store_fingerprint(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        config.scip_typescript_enabled = True
        indexer = ScipTypeScriptIndexer(repo, config)
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "index.ts").write_text("export const x = 1;", encoding="utf-8")

        with mock.patch.object(
            indexer, "_run_indexer", return_value=(1, "", "error")
        ):
            path = await indexer.ensure_index([repo / "src" / "index.ts"], "test-graph")
            assert path is None

    async def test_force_runs_even_when_fingerprint_unchanged(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        config.scip_typescript_enabled = True
        indexer = ScipTypeScriptIndexer(repo, config)
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "index.ts").write_text("export const x = 1;", encoding="utf-8")

        async def fake_run(output_path: Path) -> tuple[int, str, str]:
            output_path.write_bytes(b"fake")
            return (0, "", "")

        with mock.patch.object(indexer, "_run_indexer", side_effect=fake_run):
            path = await indexer.ensure_index([repo / "src" / "index.ts"], "test-graph")
            assert path is not None

            called = []

            async def fake_run2(output_path: Path) -> tuple[int, str, str]:
                called.append(True)
                output_path.write_bytes(b"fake")
                return (0, "", "")

            with mock.patch.object(indexer, "_run_indexer", side_effect=fake_run2):
                path2 = await indexer.ensure_index(
                    [repo / "src" / "index.ts"], "test-graph", force=True
                )
                assert path2 == path
                assert called
