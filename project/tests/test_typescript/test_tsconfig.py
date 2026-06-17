"""Tests for TypeScript tsconfig path-alias resolution."""

from __future__ import annotations

import json
from pathlib import Path

from ariadne_graph.languages.typescript.tsconfig import TsConfigResolver


def test_resolve_alias_with_baseurl(tmp_path: Path) -> None:
    """A basic '@/*' alias resolves through baseUrl."""
    src = tmp_path / "src"
    src.mkdir()
    helper_file = src / "utils" / "helper.ts"
    helper_file.parent.mkdir()
    helper_file.write_text("export const helper = () => {};")

    tsconfig = tmp_path / "tsconfig.json"
    tsconfig.write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "baseUrl": ".",
                    "paths": {"@/*": ["src/*"]},
                }
            }
        )
    )

    resolver = TsConfigResolver(tmp_path)
    assert resolver.resolve("@/utils/helper") == str(helper_file)


def test_resolve_missing_file_returns_candidate(tmp_path: Path) -> None:
    """When the target file does not exist, the computed path is still returned."""
    tsconfig = tmp_path / "tsconfig.json"
    tsconfig.write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "baseUrl": ".",
                    "paths": {"@/*": ["src/*"]},
                }
            }
        )
    )

    resolver = TsConfigResolver(tmp_path)
    assert resolver.resolve("@/missing") == str((tmp_path / "src" / "missing").resolve())


def test_no_tsconfig_returns_none(tmp_path: Path) -> None:
    """Without a tsconfig, no aliases are resolved."""
    resolver = TsConfigResolver(tmp_path)
    assert resolver.resolve("@/foo") is None
