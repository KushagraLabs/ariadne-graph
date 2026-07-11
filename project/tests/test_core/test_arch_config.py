"""Failing (RED) spec for `.ariadne/architecture.yml` config-driven layering.

Scopes beads Item 1: a repo-level architecture config that becomes the SSOT
feeding `is_deep_import`/`analyze`. When a config is present the layering
decision consults *declared modules* (path globs + `may_depend_on` directions)
instead of the directory-depth heuristic; when ABSENT, behavior is byte-for-byte
identical to today (backward-compat guard).

Both tests are RED today: `analyze` has no `arch_config` parameter and there is
no `ArchitectureConfig` loader. They pin the hard case a naive/absent
implementation gets wrong:

  * HARD CASE (config changes the decision): two modules `A` and `B` that live
    under the SAME top-level directory `src/`. The directory-depth heuristic sees
    one organ ("src") and ALLOWS the edge. Config declares them as two independent
    modules where A may_depend_on B but B must NOT depend on A — so a
    B -> A-internal edge MUST be flagged. This proves config, not the directory,
    drives the outcome.

  * BACKWARD-COMPAT: with NO config, `analyze(files, edges)` returns exactly what
    it returns today.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from ariadne_graph.core.architecture import analyze

# The config loader is the crux of Item 1. This import is RED today
# (core/architecture_config.py does not exist yet) — a hard ModuleNotFoundError,
# not a skip, so the failing signal is unambiguous.
from ariadne_graph.core import architecture_config as arch_config


def _rules(diags: list, rule: str) -> list:
    return [d for d in diags if d.rule == rule]


# Two modules under the SAME top-level dir `src/`, so the directory-depth
# heuristic collapses them into one organ ("src") and never flags cross-edges.
# Config splits them and forbids api -> internals of core.
_ARCH_YML = textwrap.dedent(
    """\
    version: 1
    modules:
      core:
        paths: ["src/core/**"]
        public_surfaces: ["src/core/__init__.py"]
      api:
        paths: ["src/api/**"]
        public_surfaces: ["src/api/__init__.py"]
        may_depend_on: ["core"]   # api -> core allowed; core -> api forbidden
    """
)


def _write_config(tmp_path: Path) -> object:
    cfg_dir = tmp_path / ".ariadne"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "architecture.yml").write_text(_ARCH_YML)
    # Discovery contract: load from a repo root, returning None when absent.
    return arch_config.load_architecture_config(tmp_path)


# --------------------------------------------------------------------------
# HARD CASE — config flips an ALLOWED directory-heuristic edge into a VIOLATION.
# --------------------------------------------------------------------------

def test_config_flags_forbidden_direction_the_directory_heuristic_allows(tmp_path):
    files = ["src/core/engine.py", "src/api/routes.py"]
    # core reaches into api's internals. Same top-level "src" => directory
    # heuristic allows it. Config: core has NO may_depend_on api => VIOLATION.
    edges = [("src/core/engine.py", "src/api/routes.py")]

    resolved = _write_config(tmp_path)

    # Baseline: directory-only heuristic sees one organ and allows the edge.
    baseline = analyze(files, edges)
    assert _rules(baseline, "deep_import") == []
    assert _rules(baseline, "layer_violation") == []

    # With config, the declared-module map must flag core -> api as a violation.
    with_cfg = analyze(files, edges, arch_config=resolved)
    violations = _rules(with_cfg, "layer_violation")
    assert [d.node_id for d in violations] == ["src/core/engine.py"]
    assert violations[0].level == "warning"
    assert violations[0].properties.get("from_module") == "core"
    assert violations[0].properties.get("to_module") == "api"


def test_config_allows_declared_direction(tmp_path):
    files = ["src/core/engine.py", "src/api/routes.py"]
    # api -> core is explicitly allowed by may_depend_on: ["core"].
    edges = [("src/api/routes.py", "src/core/engine.py")]

    resolved = _write_config(tmp_path)

    with_cfg = analyze(files, edges, arch_config=resolved)
    assert _rules(with_cfg, "layer_violation") == []


# --------------------------------------------------------------------------
# BACKWARD-COMPAT — no config => identical to today's pure (files, edges) call.
# --------------------------------------------------------------------------

def test_no_config_is_identical_to_today(tmp_path):
    files = ["src/x.py", "lib/deep/nested/y.py"]
    edges = [("src/x.py", "lib/deep/nested/y.py")]

    # No .ariadne/architecture.yml written => loader returns None.
    resolved = arch_config.load_architecture_config(tmp_path)
    assert resolved is None

    today = analyze(files, edges)
    with_none = analyze(files, edges, arch_config=None)

    def _key(diags):
        return sorted((d.rule, d.node_id, d.level) for d in diags)

    assert _key(with_none) == _key(today)
    # And the directory heuristic still fires deep_import on the cross-organ edge.
    assert [d.node_id for d in _rules(with_none, "deep_import")] == ["src/x.py"]
