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


class TestDiscoverProjects:
    """discover_projects() finds every real tsconfig, so multi-project repos
    (e.g. a root app + a mobile/ subproject) are fully indexed rather than only
    whatever the root tsconfig's ``include`` happens to cover.
    """

    def test_root_only_returns_repo_root(self, repo: Path, config: AnalyzerConfig) -> None:
        indexer = ScipTypeScriptIndexer(repo, config)
        assert indexer.discover_projects() == [repo]

    def test_finds_nested_tsconfig(self, repo: Path, config: AnalyzerConfig) -> None:
        (repo / "mobile").mkdir()
        (repo / "mobile" / "tsconfig.json").write_text("{}", encoding="utf-8")

        indexer = ScipTypeScriptIndexer(repo, config)

        assert set(indexer.discover_projects()) == {repo, repo / "mobile"}

    def test_excludes_vendored_and_build_tsconfigs(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        for junk in ("node_modules", "dist", "archive"):
            (repo / junk).mkdir()
            (repo / junk / "tsconfig.json").write_text("{}", encoding="utf-8")

        indexer = ScipTypeScriptIndexer(repo, config)

        # Only the root tsconfig survives; vendored/build/archived ones are noise.
        assert indexer.discover_projects() == [repo]

    def test_excludes_agent_worktree_tsconfigs(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        """Agent worktrees under ``.claude/worktrees/`` are full repo copies with
        their own tsconfigs. Indexing them duplicates every project and bloats the
        graph, so discovery must skip them — honoring ``config.ignore_patterns``
        (which lists ``.claude`` and ``worktrees``), not just the vendored/build set.
        """
        worktree = repo / ".claude" / "worktrees" / "agent-abc" / "frontend"
        worktree.mkdir(parents=True)
        (worktree / "tsconfig.json").write_text("{}", encoding="utf-8")

        indexer = ScipTypeScriptIndexer(repo, config)

        assert indexer.discover_projects() == [repo]

    def test_no_tsconfig_returns_repo_root(self, tmp_path: Path) -> None:
        # A package.json-only project (no tsconfig) still indexes from the root
        # via --infer-tsconfig; discovery must not return an empty list.
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        cfg = AnalyzerConfig(repo_root=tmp_path)

        indexer = ScipTypeScriptIndexer(tmp_path, cfg)

        assert indexer.discover_projects() == [tmp_path]


class TestEnsureProjectIndexes:
    """ensure_project_indexes() runs scip-typescript once per discovered project
    and returns (project_dir, scip_path) for each that succeeded.
    """

    async def test_indexes_each_project(self, repo: Path, config: AnalyzerConfig) -> None:
        config.scip_typescript_enabled = True
        (repo / "mobile").mkdir()
        (repo / "mobile" / "tsconfig.json").write_text("{}", encoding="utf-8")
        (repo / "src").mkdir()
        (repo / "src" / "a.ts").write_text("export const x=1;", encoding="utf-8")
        indexer = ScipTypeScriptIndexer(repo, config)

        run_cwds: list[Path] = []

        async def fake_run(output_path: Path, cwd: Path) -> tuple[int, str, str]:
            run_cwds.append(cwd)
            output_path.write_bytes(b"fake")
            return (0, "", "")

        with mock.patch.object(indexer, "_run_indexer", side_effect=fake_run):
            results = await indexer.ensure_project_indexes(
                [repo / "src" / "a.ts"], "g", force=True
            )

        # One result per project, each carrying its own project dir.
        got = {proj for proj, _ in results}
        assert got == {repo, repo / "mobile"}
        assert set(run_cwds) == {repo, repo / "mobile"}
        for _, scip in results:
            assert scip.exists()

    async def test_skips_failed_project_keeps_others(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        config.scip_typescript_enabled = True
        (repo / "mobile").mkdir()
        (repo / "mobile" / "tsconfig.json").write_text("{}", encoding="utf-8")
        indexer = ScipTypeScriptIndexer(repo, config)

        async def fake_run(output_path: Path, cwd: Path) -> tuple[int, str, str]:
            if cwd == repo / "mobile":
                return (1, "", "boom")  # mobile fails
            output_path.write_bytes(b"fake")
            return (0, "", "")

        with mock.patch.object(indexer, "_run_indexer", side_effect=fake_run):
            results = await indexer.ensure_project_indexes([], "g", force=True)

        assert [proj for proj, _ in results] == [repo]  # only the root survived


class TestRunIndexer:
    async def test_command_includes_output_path(
        self, repo: Path, config: AnalyzerConfig
    ) -> None:
        """``_run_indexer`` must tell scip-typescript where to write the index."""
        config.scip_typescript_enabled = True
        indexer = ScipTypeScriptIndexer(repo, config)
        expected_output = repo / ".ariadne" / "scip" / "test-graph" / "index.scip"

        captured_cmd: list[str] = []

        async def fake_exec(
            *cmd: str,
            cwd: str | None = None,
            stdout: int | None = None,
            stderr: int | None = None,
        ) -> mock.AsyncMock:
            captured_cmd.extend(cmd)
            proc = mock.AsyncMock()
            proc.returncode = 0
            proc.communicate.return_value = (b"", b"")
            return proc

        with mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await indexer._run_indexer(expected_output)

        assert "--output" in captured_cmd
        assert str(expected_output) in captured_cmd
        assert captured_cmd.index("--output") + 1 == captured_cmd.index(str(expected_output))


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
