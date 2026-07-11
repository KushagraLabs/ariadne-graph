"""File discovery and ignore-rule logic."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from ariadne_graph.core.config import AnalyzerConfig


class FileDiscovery:
    """Discovers source files in a repository, respecting ignore rules."""

    def __init__(self, config: AnalyzerConfig) -> None:
        self.config = config

    def _should_ignore(self, path: Path) -> bool:
        """Check if a path matches any ignore pattern.

        Non-glob patterns match whole path *components* (directory or file
        names), not arbitrary substrings — so ``_tmp`` ignores a ``_tmp/`` dir
        but not ``my_tmp_helper.py``, and ``external`` ignores ``external/`` but
        not ``external_api.py``. Glob patterns (``*.pyc``) match the file name.
        """
        parts = path.parts
        name = path.name
        for pattern in self.config.ignore_patterns:
            if pattern.startswith("*"):
                if fnmatch.fnmatch(name, pattern):
                    return True
            elif pattern in parts or fnmatch.fnmatch(name, pattern):
                return True
        return False

    def discover(
        self,
        extensions: tuple[str, ...],
    ) -> list[Path]:
        """Find all files with given extensions under repo_root.

        Args:
            extensions: File extensions to include, e.g. (".py",)

        Returns:
            Sorted list of file paths matching extensions and not ignored.
        """
        root = self.config.resolved_repo_root
        results: list[Path] = []

        for ext in extensions:
            if not ext.startswith("."):
                ext = "." + ext
            for path in root.rglob(f"*{ext}"):
                if self._should_ignore(path):
                    continue
                if path.stat().st_size > self.config.max_file_size:
                    continue
                results.append(path)

        return sorted(results)
