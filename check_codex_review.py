#!/usr/bin/env python3
"""Codex-review gate — canonical, shared across repos via pre-commit.

Commits that touch source code are BLOCKED unless the staged code carries a
fresh codex approval. The codex review loop records the approved staged-code
diff hash to ``<git-dir>/codex-approved`` on SHIP (per-worktree, so parallel
agents in linked worktrees don't clobber each other's approval); this recomputes
the staged-code diff hash and requires a match.

Docs/config/beads-only commits (no staged source files) are exempt.
Emergency bypass for a trivial change: ``CODEX_REVIEW_OK=1 git commit ...``.

This is the single source of truth. Consuming repos reference it as a published
pre-commit hook (``.pre-commit-hooks.yaml`` → ``id: codex-review-gate``) instead
of copying the script. The only per-repo variable is the set of source
extensions, passed via ``--code-suffixes`` (default covers Python + JS/TS); a
Python-only repo uses ``--code-suffixes=py``, a TS-only repo ``--code-suffixes=ts,tsx,js,jsx,mjs,cjs``.

See each repo's CLAUDE.md "Codex review gate" for the loop-until-clean protocol.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Default source extensions that must carry a codex approval. Repos override via
# --code-suffixes to match the languages they actually ship.
DEFAULT_SUFFIXES = "py,ts,tsx,js,jsx,mjs,cjs"


def _git(*args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        input=stdin,
    ).stdout


def _normalize_suffixes(raw: str) -> tuple[str, ...]:
    """Turn 'py,ts' or '.py,.ts' into ('.py', '.ts'); ignore blanks."""
    out = []
    for part in raw.split(","):
        part = part.strip().lstrip(".")
        if part:
            out.append("." + part)
    return tuple(out)


def staged_source_files(suffixes: tuple[str, ...]) -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACM").decode()
    files = [ln for ln in out.splitlines() if ln.strip()]
    return [f for f in files if f.endswith(suffixes)]


def staged_code_hash(files: list[str]) -> str:
    """Hash the staged diff restricted to the source files (order-stable)."""
    diff = _git("diff", "--cached", "--diff-filter=ACM", "--", *sorted(files))
    return _git("hash-object", "--stdin", stdin=diff).decode().strip()


def approved_marker_path() -> str:
    # --git-dir is the *per-worktree* admin dir: `.git` in the main checkout,
    # but `.git/worktrees/<name>` in a linked worktree. This isolates the marker
    # per worktree so parallel agents don't clobber one shared approval (they
    # each stage a different diff). --git-common-dir would collapse them all to
    # the shared parent .git — a last-writer-wins race. The marker is auto-
    # cleaned when its worktree is pruned via `git worktree remove`.
    git_dir = _git("rev-parse", "--git-dir").decode().strip()
    return os.path.join(git_dir, "codex-approved")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True, description=__doc__)
    parser.add_argument(
        "--code-suffixes",
        default=DEFAULT_SUFFIXES,
        help="Comma-separated source extensions requiring codex approval "
        f"(default: {DEFAULT_SUFFIXES}).",
    )
    # pre-commit passes staged filenames as positional args on some hook
    # configs; we compute the staged set ourselves, so swallow and ignore them.
    parser.add_argument("files", nargs="*")
    ns = parser.parse_args()

    if os.environ.get("CODEX_REVIEW_OK") == "1":
        return 0

    suffixes = _normalize_suffixes(ns.code_suffixes)
    source = staged_source_files(suffixes)
    if not source:  # docs/config/beads-only commit — exempt
        return 0

    expected = staged_code_hash(source)
    marker = approved_marker_path()
    try:
        with open(marker, encoding="utf-8") as fh:
            approved = fh.read().strip()
    except FileNotFoundError:
        approved = ""

    if expected == approved:
        return 0

    suffix_alt = "|".join(s.lstrip(".") for s in suffixes)
    record_cmd = (
        'echo "$(git diff --cached --diff-filter=ACM -- '
        "$(git diff --cached --name-only --diff-filter=ACM | "
        f"grep -E '\\.({suffix_alt})$') | git hash-object --stdin)\" "
        '> "$(git rev-parse --git-dir)/codex-approved"'
    )
    sys.stderr.write(
        "✗ Codex review gate: staged code has no fresh codex approval.\n"
        "  Format + re-stage FIRST (a post-approval reformat invalidates the\n"
        "  marker), run codex review on the staged diff, loop until SHIP\n"
        "  (resolve every finding), then record approval (worktree-safe path):\n"
        f"      {record_cmd}\n"
        "  Or bypass for a trivial/emergency change: CODEX_REVIEW_OK=1 git commit ...\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
