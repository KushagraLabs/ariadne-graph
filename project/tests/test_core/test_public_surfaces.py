"""Failing (RED) spec for the public-surfaces / encapsulation audit.

Scopes bead code_hygiene_mcp-5np: a facade/encapsulation audit over the
`public_surfaces` declared in `.ariadne/architecture.yml`
(:mod:`ariadne_graph.core.architecture_config`). For each module it must
report:

  * public exports — the module's declared `public_surfaces` files.
  * consumers importing THROUGH the public surface vs consumers that
    DEEP-IMPORT internals (a non-surface file owned by the module).
  * unused public exports — a public_surfaces file with zero external
    consumers.
  * all-exporting barrel detection — a public_surfaces file that is the
    module's ONLY file (or otherwise re-exports ~every internal file) provides
    no real encapsulation and must be flagged.

HARD CASE fixture (``_write_layered_repo``): module ``core`` declares
``src/core/__init__.py`` as its public surface and owns two internal files,
``engine.py`` (used through the surface) and ``deep_helper.py`` (imported
directly by an external consumer, bypassing the surface). A second module
``leaky`` has a public surface that is its ONLY owned file — a
degenerate/all-exporting barrel with zero encapsulation value.

This is a READ-ONLY reporting consumer: it must reuse the existing
``_DEP_EDGE_SQL``/``_FILE_SQL`` SSOT (:mod:`ariadne_graph.core.architecture`)
and ``ArchitectureConfig.module_of`` — no new resolver.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter
from ariadne_graph.mcp.schemas import AuditPublicSurfacesInput, IndexInput
from ariadne_graph.mcp.tools import ToolRegistry

_ARCH_YML = textwrap.dedent(
    """\
    version: 1
    modules:
      core:
        paths: ["src/core/**"]
        public_surfaces: ["src/core/__init__.py"]
      leaky:
        paths: ["src/leaky/**"]
        public_surfaces: ["src/leaky/__init__.py"]
      app:
        paths: ["app/**"]
        may_depend_on: ["core", "leaky"]
    """
)


def _write_layered_repo(repo_root: Path) -> None:
    (repo_root / ".ariadne").mkdir(parents=True)
    (repo_root / ".ariadne" / "architecture.yml").write_text(_ARCH_YML)

    # -- module "core": a real public surface with an internal it hides. --
    core = repo_root / "src" / "core"
    core.mkdir(parents=True)
    (core / "engine.py").write_text(
        "def run(x):\n    return x + 1\n"
    )
    (core / "deep_helper.py").write_text(
        "def helper(x):\n    return x - 1\n"
    )
    # __init__.py is the declared public surface: re-exports `run` only (NOT
    # deep_helper) -- a real facade, not a barrel.
    (core / "__init__.py").write_text(
        "from src.core.engine import run\n"
    )

    # -- module "leaky": public surface IS the module's only file -- a
    # degenerate barrel that provides no encapsulation (nothing to hide behind
    # it).
    leaky = repo_root / "src" / "leaky"
    leaky.mkdir(parents=True)
    (leaky / "__init__.py").write_text(
        "def leaky_fn(x):\n    return x * 2\n"
    )

    # -- module "app": two consumers. --
    app = repo_root / "app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    # via_surface.py imports the surface and CALLS through it (resolvable dep
    # edge core/__init__.py -> app/via_surface.py).
    (app / "via_surface.py").write_text(
        "from src.core import run\n"
        "\n"
        "\n"
        "def go():\n"
        "    return run(1)\n"
    )
    # deep_consumer.py bypasses the surface and calls straight into the
    # internal deep_helper.py.
    (app / "deep_consumer.py").write_text(
        "from src.core.deep_helper import helper\n"
        "\n"
        "\n"
        "def go2():\n"
        "    return helper(1)\n"
    )
    # leaky_consumer.py calls through leaky's "surface" -- which is also its
    # only file, so this both proves the barrel flag AND gives leaky a
    # consumer (so the barrel flag isn't confused with "unused").
    (app / "leaky_consumer.py").write_text(
        "from src.leaky import leaky_fn\n"
        "\n"
        "\n"
        "def go3():\n"
        "    return leaky_fn(1)\n"
    )


def _make_registry(store: SQLiteGraphStore, repo_root: Path) -> ToolRegistry:
    config = AnalyzerConfig(repo_root=repo_root)
    return ToolRegistry(
        graph_store=store,
        searchable_store=store,
        adapters={"python": PythonLanguageAdapter()},
        config=config,
        snippet_extractor=SnippetExtractor(repo_root=repo_root),
        embedding_provider=None,
    )


@pytest.mark.asyncio
async def test_audit_distinguishes_via_surface_from_deep_import(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_layered_repo(repo)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        index = await registry.handle_index(IndexInput(repo_path=str(repo)))
        assert index.status == "success"

        result = await registry.handle_audit_public_surfaces(
            AuditPublicSurfacesInput(repo_path=str(repo))
        )

        by_name = {m["module"]: m for m in result.modules}
        assert "core" in by_name
        core = by_name["core"]

        # Public exports: the declared public_surfaces file(s).
        assert core["public_exports"] == ["src/core/__init__.py"]

        # via-surface consumer: app/via_surface.py -> src/core/__init__.py.
        via_surface_consumers = {
            c["consumer"] for c in core["via_surface_consumers"]
        }
        assert "app/via_surface.py" in via_surface_consumers

        # deep-import consumer: app/deep_consumer.py -> src/core/deep_helper.py
        # (an internal, non-surface file owned by "core").
        deep_import_consumers = {
            c["consumer"] for c in core["deep_import_consumers"]
        }
        assert "app/deep_consumer.py" in deep_import_consumers

        # And the deep-import consumer must NOT also be counted as via-surface.
        assert "app/deep_consumer.py" not in via_surface_consumers
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_audit_flags_unused_public_export(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_layered_repo(repo)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await registry.handle_index(IndexInput(repo_path=str(repo)))

        result = await registry.handle_audit_public_surfaces(
            AuditPublicSurfacesInput(repo_path=str(repo))
        )
        by_name = {m["module"]: m for m in result.modules}

        # "leaky" IS consumed (via leaky_consumer.py), so its surface is not
        # unused -- this isolates the barrel assertion (below) from the
        # unused-export assertion.
        assert "src/leaky/__init__.py" not in by_name["leaky"]["unused_public_exports"]

        # core's surface (__init__.py) IS consumed -- not unused either.
        assert "src/core/__init__.py" not in by_name["core"]["unused_public_exports"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_audit_flags_all_exporting_barrel(tmp_path: Path) -> None:
    """A public_surfaces file that IS the module's only owned file hides
    nothing -- flag it as a barrel providing no real encapsulation.
    """
    repo = tmp_path / "repo"
    _write_layered_repo(repo)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await registry.handle_index(IndexInput(repo_path=str(repo)))

        result = await registry.handle_audit_public_surfaces(
            AuditPublicSurfacesInput(repo_path=str(repo))
        )
        by_name = {m["module"]: m for m in result.modules}

        assert by_name["leaky"]["is_all_exporting_barrel"] is True
        # core has a real internal it hides (deep_helper.py) -- not a barrel.
        assert by_name["core"]["is_all_exporting_barrel"] is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_audit_requires_architecture_config(tmp_path: Path) -> None:
    """With NO .ariadne/architecture.yml, the audit is meaningless -- must say so."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    pass\n")
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await registry.handle_index(IndexInput(repo_path=str(repo)))

        result = await registry.handle_audit_public_surfaces(
            AuditPublicSurfacesInput(repo_path=str(repo))
        )
        assert result.modules == []
        assert "public_surfaces" in result.message.lower()
        assert "architecture.yml" in result.message.lower()
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# TypeScript via-surface / deep-import split (bead code_hygiene_mcp-pzl)
# ---------------------------------------------------------------------------
#
# HARD CASE the Python-shaped resolver gets wrong: TS `from_module` is a RAW
# specifier ('./engine', '../core' , '../core/deep_helper'), not a dotted
# module path, so `_module_name_candidates` never matches it and EVERY
# import-edge consumer collapses to 'deep'. The via-surface consumer below
# imports from the barrel `index.ts`; the deep consumer reaches straight into
# an internal. They must be told apart.

_TS_ARCH_YML = textwrap.dedent(
    """\
    version: 1
    modules:
      core:
        paths: ["src/core/**"]
        public_surfaces: ["src/core/index.ts"]
      app:
        paths: ["src/app/**"]
        may_depend_on: ["core"]
    """
)


def _write_ts_layered_repo(repo_root: Path) -> None:
    (repo_root / ".ariadne").mkdir(parents=True)
    (repo_root / ".ariadne" / "architecture.yml").write_text(_TS_ARCH_YML)

    core = repo_root / "src" / "core"
    core.mkdir(parents=True)
    (core / "engine.ts").write_text(
        "export function run(x: number): number {\n  return x + 1;\n}\n"
    )
    (core / "deep_helper.ts").write_text(
        "export function helper(x: number): number {\n  return x - 1;\n}\n"
    )
    # index.ts is the declared public surface: a barrel that re-exports `run`
    # only (NOT deep_helper) -- a real facade.
    (core / "index.ts").write_text(
        'export { run } from "./engine";\n'
    )

    app = repo_root / "src" / "app"
    app.mkdir(parents=True)
    # via_surface.ts imports THROUGH the barrel (specifier resolves to the
    # directory's index.ts).
    (app / "via_surface.ts").write_text(
        'import { run } from "../core";\n'
        "\n"
        "export function go(): number {\n  return run(1);\n}\n"
    )
    # deep_consumer.ts bypasses the surface and imports the internal directly.
    (app / "deep_consumer.ts").write_text(
        'import { helper } from "../core/deep_helper";\n'
        "\n"
        "export function go2(): number {\n  return helper(1);\n}\n"
    )


def _make_ts_registry(store: SQLiteGraphStore, repo_root: Path) -> ToolRegistry:
    config = AnalyzerConfig(repo_root=repo_root)
    return ToolRegistry(
        graph_store=store,
        searchable_store=store,
        adapters={"typescript": TypeScriptLanguageAdapter()},
        config=config,
        snippet_extractor=SnippetExtractor(repo_root=repo_root),
        embedding_provider=None,
    )


@pytest.mark.asyncio
async def test_audit_ts_distinguishes_via_surface_from_deep_import(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _write_ts_layered_repo(repo)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_ts_registry(store, repo)
    try:
        index = await registry.handle_index(IndexInput(repo_path=str(repo)))
        assert index.status == "success"

        result = await registry.handle_audit_public_surfaces(
            AuditPublicSurfacesInput(repo_path=str(repo))
        )

        by_name = {m["module"]: m for m in result.modules}
        assert "core" in by_name
        core = by_name["core"]
        assert core["public_exports"] == ["src/core/index.ts"]

        via_surface_consumers = {
            c["consumer"] for c in core["via_surface_consumers"]
        }
        deep_import_consumers = {
            c["consumer"] for c in core["deep_import_consumers"]
        }

        # barrel import ("../core" -> src/core/index.ts) is via-surface.
        assert "src/app/via_surface.ts" in via_surface_consumers
        # deep import ("../core/deep_helper") is deep, and NOT via-surface.
        assert "src/app/deep_consumer.ts" in deep_import_consumers
        assert "src/app/deep_consumer.ts" not in via_surface_consumers
    finally:
        await store.close()
