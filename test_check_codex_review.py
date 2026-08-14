#!/usr/bin/env python3
"""Regression tests for check_codex_review.py -- the shared codex-review-gate hook.

NON-VACUITY (see the issue this fixes): a test that only stages a MODIFICATION
passes today and proves nothing -- the bug only affects DELETE and RENAME
because the hook enumerated staged files with --diff-filter=ACM. These tests
stage a real deletion-only and a real rename-only commit in a throwaway git
repo and invoke the actual hook script as a subprocess, exactly the way
pre-commit invokes it -- not a reimplementation of its enumeration logic.

Run: python3 test_check_codex_review.py
 or: pytest test_check_codex_review.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "check_codex_review.py"


def _load_hook_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_codex_review", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo_with_gated_file(repo: Path, filename: str = "docs/POLICY.md") -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("line one\nline two\nline three\n")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", "init")


def _run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("CODEX_REVIEW_OK", None)
    return subprocess.run(
        [sys.executable, str(HOOK), "--code-suffixes=py,md"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


class CodexGateEnumeratesDeletesAndRenames(unittest.TestCase):
    """NON-VACUITY: stage the destructive op with NO marker, assert non-zero exit."""

    def test_deletion_only_commit_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "rm", "-q", "docs/POLICY.md")
            result = _run_hook(repo)
            self.assertNotEqual(
                result.returncode,
                0,
                "deletion-only commit of a gated file passed the gate with "
                f"no approval; stderr={result.stderr!r}",
            )

    def test_rename_only_commit_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "mv", "docs/POLICY.md", "docs/POLICY_RENAMED.md")
            result = _run_hook(repo)
            self.assertNotEqual(
                result.returncode,
                0,
                "rename-only commit of a gated file passed the gate with "
                f"no approval; stderr={result.stderr!r}",
            )

    def test_modification_only_commit_is_still_blocked(self) -> None:
        # Sanity: the case the gate already covered must keep working.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            (repo / "docs/POLICY.md").write_text("line one\nline two\nCHANGED\n")
            _git(repo, "add", "docs/POLICY.md")
            result = _run_hook(repo)
            self.assertNotEqual(result.returncode, 0)

    def test_deletion_only_commit_passes_with_a_fresh_marker(self) -> None:
        # Proves the block above is really the missing-approval gate (and
        # that a correctly recorded marker unblocks a deletion), not some
        # unrelated crash on a path that no longer exists on disk.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "rm", "-q", "docs/POLICY.md")
            module = _load_hook_module()
            diff = subprocess.run(
                [
                    "git",
                    "diff",
                    "--cached",
                    f"--diff-filter={module.DIFF_FILTER}",
                    "--",
                    "docs/POLICY.md",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            ).stdout
            digest = (
                subprocess.run(
                    ["git", "hash-object", "--stdin"],
                    cwd=repo,
                    input=diff,
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
            git_dir = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            (repo / git_dir / "codex-approved").write_text(digest)
            result = _run_hook(repo)
            self.assertEqual(result.returncode, 0, result.stderr)


class PrintedRecordCommandStaysInSyncWithEnumeration(unittest.TestCase):
    """The marker-recording command printed on failure must use the SAME
    --diff-filter the hook enumerated with -- CLAUDE.md in consuming repos
    documents that printed command as the SSOT consumers copy verbatim, so a
    silent divergence there is a second bug hiding behind the first fix.

    This inspects the ACTUAL stderr text the script emits (a real subprocess
    run), not the source, so a future edit that changes DIFF_FILTER (or the
    enumeration call) without updating the printed command fails this test.
    """

    def test_printed_command_diff_filter_matches_enumeration_filter(self) -> None:
        module = _load_hook_module()

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "rm", "-q", "docs/POLICY.md")
            result = _run_hook(repo)

        expected_flag = f"--diff-filter={module.DIFF_FILTER}"
        occurrences = result.stderr.count(expected_flag)
        # The record command invokes --diff-filter twice: once to list the
        # gated filenames, once to hash their diff. Both occurrences must
        # carry the filter the hook itself enumerated with.
        self.assertEqual(
            occurrences,
            2,
            f"expected 2 occurrences of {expected_flag!r} in the printed "
            f"record command, found {occurrences}; stderr={result.stderr!r}",
        )


if __name__ == "__main__":
    unittest.main()
