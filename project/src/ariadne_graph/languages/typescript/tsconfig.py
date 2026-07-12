"""TypeScript tsconfig path-alias resolution.

Provides a small helper that loads ``tsconfig.json`` (if present) and resolves
path-mapping aliases such as ``@/components/Foo`` to real source files relative
to ``compilerOptions.baseUrl``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Extensions a bare relative TS/JS specifier may resolve to, plus the index-file
# form for directory imports (``./foo`` -> ``./foo/index.ts``). Order matters:
# the first existing candidate wins, matching TS module resolution.
_RELATIVE_IMPORT_EXTS = (".ts", ".tsx", ".js", ".jsx")


def resolve_relative_import(source: str, file_path: Path) -> str | None:
    """Resolve a relative import specifier (``./x``/``../x``) to a real file.

    SSOT for relative-import resolution, shared by the base Tree-sitter extractor
    (SCIP-less path) and :class:`~ariadne_graph.languages.typescript.scip_enricher.TreeSitterEnricher`
    (SCIP path). ``TsConfigResolver.resolve`` only handles ``compilerOptions.paths``
    aliases; a relative specifier resolves against the importing file's directory
    instead. Tries the file as-is, common TS/JS extensions, and the directory
    ``index`` form; returns the absolute path of the first that exists on disk, or
    ``None`` when the specifier is not relative or nothing matches.
    """
    if not source.startswith("."):
        return None
    base = (file_path.parent / source).resolve()
    candidates = [base]
    for ext in _RELATIVE_IMPORT_EXTS:
        candidates.append(base.with_suffix(ext))
        candidates.append(base / f"index{ext}")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


class TsConfigResolver:
    """Resolve TypeScript path aliases using tsconfig ``compilerOptions.paths``.

    Only the most common wildcard form is supported, e.g.::

        "@/*": ["src/*"]
        "~/*": ["src/*"]

    Exact mappings without a wildcard are also supported.  If a resolved path
    does not exist on disk, the resolver still returns the computed path so
    that callers can record the intended target.
    """

    def __init__(self, repo_root: Path) -> None:
        """Load ``tsconfig.json`` from *repo_root* if it exists."""
        self.repo_root = repo_root.resolve()
        self.base_dir = self.repo_root
        self._patterns: list[tuple[re.Pattern[str], str]] = []

        config_path = self.repo_root / "tsconfig.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}

        compiler_options: dict[str, Any] = config.get("compilerOptions", {})

        base_url = compiler_options.get("baseUrl")
        if base_url:
            self.base_dir = (self.repo_root / base_url).resolve()

        paths = compiler_options.get("paths", {})
        for key, targets in paths.items():
            if not isinstance(targets, list):
                continue
            pattern = self._compile_pattern(key)
            for target in targets:
                if isinstance(target, str):
                    self._patterns.append((pattern, target))

    @staticmethod
    def _compile_pattern(key: str) -> re.Pattern[str]:
        """Turn a tsconfig paths key into a regex anchored at start/end.

        A single ``*`` wildcard captures one or more characters.  Keys without
        a wildcard match the whole string exactly.
        """
        escaped = re.escape(key)
        # re.escape turns '*' into '\\*'.
        pattern = (
            escaped.replace("\\*", r"(.+?)") + r"\Z"
            if "\\*" in escaped
            else escaped + r"\Z"
        )
        return re.compile(pattern)

    def resolve(self, source: str) -> str | None:
        """Resolve an import source string through tsconfig paths.

        Args:
            source: The module source from an import statement, e.g. ``"@/foo"``.

        Returns:
            Absolute path of the resolved target if a mapping matches, otherwise
            ``None``.
        """
        for regex, target_template in self._patterns:
            match = regex.match(source)
            if not match:
                continue

            if "*" in target_template:
                # Substitute each captured group into the target template.
                groups = match.groups()
                resolved_target = target_template
                for group in groups:
                    resolved_target = resolved_target.replace("*", group, 1)
            else:
                resolved_target = target_template

            candidate = (self.base_dir / resolved_target).resolve()

            # If the candidate is a directory, look for an index file.
            if candidate.is_dir():
                for ext in (".ts", ".tsx", ".js", ".jsx"):
                    index_file = candidate / f"index{ext}"
                    if index_file.exists():
                        return str(index_file)
                return str(candidate / "index")

            # Try common extensions if the file does not exist as-is.
            if not candidate.exists():
                for ext in (".ts", ".tsx", ".js", ".jsx"):
                    with_ext = candidate.with_suffix(ext)
                    if with_ext.exists():
                        return str(with_ext)

            return str(candidate)

        return None
