#!/usr/bin/env python3
"""Regression tests for check_codex_review.py -- the shared codex-review-gate hook.

NON-VACUITY (see the issue this fixes): a test that only stages a MODIFICATION
passes today and proves nothing -- the bug only affects DELETE and RENAME
because the hook enumerated staged files with --diff-filter=ACM. These tests
stage a real deletion-only and a real rename-only commit in a throwaway git
repo and invoke the actual hook script as a subprocess, exactly the way
pre-commit invokes it -- not a reimplementation of its enumeration logic.

The hard case is the SUFFIX-ESCAPING rename (POLICY.md -> POLICY.txt): widening
the filter to ACMRD alone does NOT catch it, because `--name-only` reports only
the rename destination, which fails the suffix test. It needs --no-renames as
well. A rename-only test that renames .md -> .md passes with the filter fix
alone and would hide that second hole.

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

    def test_rename_that_escapes_the_gated_suffix_is_blocked(self) -> None:
        # THE HARD CASE. Renaming a gated file to a non-gated suffix removes it
        # from the gated set as surely as `git rm` does. With rename detection
        # on (git's default), `--name-only` prints ONLY `docs/POLICY.txt`, which
        # fails the .md/.py suffix test -- so ACMRD alone still exempts this
        # commit. Only --no-renames (D(old) + A(new)) surfaces the .md path.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "mv", "docs/POLICY.md", "docs/POLICY.txt")
            result = _run_hook(repo)
            self.assertNotEqual(
                result.returncode,
                0,
                "rename of a gated file OUT of the gated suffix set passed "
                f"the gate with no approval; stderr={result.stderr!r}",
            )

    def test_rename_that_escapes_the_suffix_is_blocked_with_renames_forced_on(
        self,
    ) -> None:
        # Non-vacuity guard for the test above: with diff.renames explicitly
        # forced ON in repo config, the D+A decomposition must still happen --
        # i.e. the hook's own --no-renames flag is what does the work, not an
        # accident of the throwaway repo's default config.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "config", "diff.renames", "true")
            _git(repo, "mv", "docs/POLICY.md", "docs/POLICY.txt")
            result = _run_hook(repo)
            self.assertNotEqual(
                result.returncode,
                0,
                "suffix-escaping rename passed the gate with diff.renames=true; "
                f"stderr={result.stderr!r}",
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
    """The marker-recording command printed on failure must use the SAME diff
    flags the hook enumerated and hashed with -- CLAUDE.md in consuming repos
    documents that printed command as the SSOT consumers copy verbatim, so a
    silent divergence there is a second bug hiding behind the first fix.

    Two independent checks, because they fail for different reasons:
      - textual: the printed command carries the same DIFF_FLAGS (cheap, and
        pinpoints WHICH half drifted);
      - behavioural: actually EXECUTING the printed command yields a marker the
        hook then accepts, on the hardest case (the suffix-escaping rename).
        This is the one that proves runtime equivalence rather than string
        equality -- it would catch a drift in argument ORDER or in the pathspec
        the two sides feed to `git diff`, which the textual check cannot see.
    """

    def test_printed_command_diff_flags_match_enumeration_flags(self) -> None:
        module = _load_hook_module()

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "rm", "-q", "docs/POLICY.md")
            result = _run_hook(repo)

        expected_flags = " ".join(module.DIFF_FLAGS)
        occurrences = result.stderr.count(expected_flags)
        # The record command invokes git diff twice: once to list the gated
        # filenames, once to hash their diff. Both must carry the flags the
        # hook itself enumerated and hashed with.
        self.assertEqual(
            occurrences,
            2,
            f"expected 2 occurrences of {expected_flags!r} in the printed "
            f"record command, found {occurrences}; stderr={result.stderr!r}",
        )

    def test_executing_the_printed_command_unblocks_a_suffix_escaping_rename(
        self,
    ) -> None:
        # End-to-end: take the command the hook PRINTS, run it verbatim in a
        # shell, and confirm the hook then passes. On the suffix-escaping
        # rename -- the case where enumeration and the printed command are
        # most likely to disagree about which paths are in scope.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_gated_file(repo)
            _git(repo, "mv", "docs/POLICY.md", "docs/POLICY.txt")

            blocked = _run_hook(repo)
            self.assertNotEqual(blocked.returncode, 0, "expected a block first")

            printed = [
                ln.strip()
                for ln in blocked.stderr.splitlines()
                if ln.strip().startswith("echo ")
            ]
            self.assertEqual(
                len(printed), 1, f"could not find the record command in {blocked.stderr!r}"
            )
            subprocess.run(
                printed[0], cwd=repo, shell=True, check=True, capture_output=True
            )

            after = _run_hook(repo)
            self.assertEqual(
                after.returncode,
                0,
                "the record command the hook printed did not produce a marker "
                f"the hook accepts; stderr={after.stderr!r}",
            )


if __name__ == "__main__":
    unittest.main()
