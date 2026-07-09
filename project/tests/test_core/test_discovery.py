"""Tests for FileDiscovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.discovery import FileDiscovery


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a small repository tree with varied files."""
    root = tmp_path / "repo"
    root.mkdir()

    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n")
    (root / "src" / "utils.py").write_text("pass\n")
    (root / "src" / "helper.pyc").write_text("bytes\n")

    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text("pass\n")

    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cache.pyc").write_text("bytes\n")

    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("git config\n")

    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg").mkdir()
    (root / "node_modules" / "pkg" / "index.ts").write_text("export {}\n")

    (root / ".claude").mkdir()
    (root / ".claude" / "worktrees").mkdir()
    (root / ".claude" / "worktrees" / "agent-1").mkdir()
    (root / ".claude" / "worktrees" / "agent-1" / "main.py").write_text("pass\n")

    # Throwaway/agent dirs that previously leaked into the index:
    # a top-level `worktrees` (NO leading dot — the old `.worktrees` pattern
    # missed it), a `.gc` agent overlay, and a `_tmp` scratch dir.
    (root / "worktrees" / "wt-1").mkdir(parents=True)
    (root / "worktrees" / "wt-1" / "copy.py").write_text("pass\n")
    (root / ".gc" / "system").mkdir(parents=True)
    (root / ".gc" / "system" / "hook.py").write_text("pass\n")
    (root / "_tmp").mkdir()
    (root / "_tmp" / "scratch.py").write_text("pass\n")

    (root / "README.md").write_text("# readme\n")
    return root


def test_discover_python_files(repo: Path) -> None:
    """Discovery returns Python files and ignores non-matching extensions."""
    config = AnalyzerConfig(repo_root=repo)
    discovery = FileDiscovery(config)

    found = discovery.discover((".py",))

    assert len(found) == 3
    ids = {str(p.relative_to(repo)) for p in found}
    assert ids == {"src/main.py", "src/utils.py", "tests/test_main.py"}


def test_discover_without_leading_dot(repo: Path) -> None:
    """Extensions are normalised to include a leading dot."""
    config = AnalyzerConfig(repo_root=repo)
    discovery = FileDiscovery(config)

    found = discovery.discover(("py",))

    assert len(found) == 3


def test_discover_multiple_extensions(repo: Path) -> None:
    """Multiple extensions are discovered and merged."""
    config = AnalyzerConfig(repo_root=repo)
    discovery = FileDiscovery(config)

    found = discovery.discover((".py", ".md"))

    assert len(found) == 4
    ids = {str(p.relative_to(repo)) for p in found}
    assert "README.md" in ids
    assert "src/main.py" in ids


def test_default_ignore_patterns(repo: Path) -> None:
    """Built-in ignore patterns skip .git, __pycache__, node_modules, *.pyc, .claude."""
    config = AnalyzerConfig(repo_root=repo)
    discovery = FileDiscovery(config)

    found = discovery.discover((".py", ".pyc", ".ts"))

    paths = {str(p.relative_to(repo)) for p in found}
    assert ".git/config" not in paths
    assert "__pycache__/cache.pyc" not in paths
    assert "src/helper.pyc" not in paths
    assert "node_modules/pkg/index.ts" not in paths
    assert ".claude/worktrees/agent-1/main.py" not in paths


def test_ignores_throwaway_worktree_and_agent_dirs(repo: Path) -> None:
    """Hard case: a top-level `worktrees` dir (no leading dot), `.gc` agent
    overlay, and `_tmp` scratch must NOT be indexed. These leaked before because
    the default pattern was `.worktrees` (with a dot) which never matched
    `worktrees/`.
    """
    config = AnalyzerConfig(repo_root=repo)
    discovery = FileDiscovery(config)

    paths = {str(p.relative_to(repo)) for p in discovery.discover((".py",))}

    assert "worktrees/wt-1/copy.py" not in paths
    assert ".gc/system/hook.py" not in paths
    assert "_tmp/scratch.py" not in paths
    # core files still discovered
    assert "src/main.py" in paths


def test_ignore_patterns_match_components_not_substrings(repo: Path) -> None:
    """Component-level matching: `_tmp`/`external` ignore dirs of that name but
    NOT files whose names merely contain the pattern as a substring.
    """
    (repo / "src" / "my_tmp_helper.py").write_text("pass\n")
    (repo / "src" / "external_api.py").write_text("pass\n")
    config = AnalyzerConfig(repo_root=repo)
    discovery = FileDiscovery(config)

    paths = {str(p.relative_to(repo)) for p in discovery.discover((".py",))}

    assert "src/my_tmp_helper.py" in paths  # not eaten by the `_tmp` pattern
    assert "src/external_api.py" in paths   # not eaten by the `external` pattern
    assert "_tmp/scratch.py" not in paths   # the real _tmp dir is still ignored


def test_custom_ignore_patterns(repo: Path) -> None:
    """User-provided ignore patterns are respected."""
    config = AnalyzerConfig(repo_root=repo, ignore_patterns=["tests", "*.md"])
    discovery = FileDiscovery(config)

    found = discovery.discover((".py", ".md"))

    paths = {str(p.relative_to(repo)) for p in found}
    assert "tests/test_main.py" not in paths
    assert "README.md" not in paths
    assert "src/main.py" in paths


def test_max_file_size_filter(repo: Path) -> None:
    """Files larger than max_file_size are skipped."""
    big = repo / "src" / "big.py"
    big.write_text("x\n" * 10_000)

    config = AnalyzerConfig(repo_root=repo, max_file_size=20)
    discovery = FileDiscovery(config)

    found = discovery.discover((".py",))

    paths = {str(p.relative_to(repo)) for p in found}
    assert "src/big.py" not in paths
    assert "src/main.py" in paths


def test_discover_returns_sorted_paths(repo: Path) -> None:
    """Results are returned in sorted order."""
    config = AnalyzerConfig(repo_root=repo)
    discovery = FileDiscovery(config)

    found = discovery.discover((".py",))

    assert found == sorted(found)


def test_should_ignore_glob_prefix(repo: Path) -> None:
    """Patterns starting with * match only the file/directory name."""
    config = AnalyzerConfig(repo_root=repo, ignore_patterns=["*.pyc"])
    discovery = FileDiscovery(config)

    # src/helper.pyc should be ignored because its name matches *.pyc
    assert discovery._should_ignore(repo / "src" / "helper.pyc") is True
    # README.md should not be ignored
    assert discovery._should_ignore(repo / "README.md") is False


def test_should_ignore_path_substring(repo: Path) -> None:
    """Non-glob patterns match anywhere in the path."""
    config = AnalyzerConfig(repo_root=repo, ignore_patterns=["node_modules"])
    discovery = FileDiscovery(config)

    assert discovery._should_ignore(repo / "node_modules" / "pkg" / "index.ts") is True
