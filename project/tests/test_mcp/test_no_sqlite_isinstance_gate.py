"""Lint-style guard: no ``isinstance(store, SQLiteGraphStore)`` hygiene gates.

Bead code_hygiene_mcp-420 replaced the analysis-path ``isinstance`` gates in
``mcp/tools.py`` with a ``dep_edges`` capability check. This pins that they do
not creep back — an isinstance gate in a HYGIENE/ANALYSIS handler silently
excludes Memory/Neo4j from the architecture-intelligence promise, the exact bug
that bead killed.

Scope (refined for bead code_hygiene_mcp-42i): the guard targets the analysis
handlers 420 protected, NOT the whole file. 42i's ``suggest_placement`` /
``find_equivalent`` legitimately use ``isinstance(SQLiteGraphStore)`` for SQLite
FAST-PATHS (chunked ``id IN (...)`` hydration, embedding-provenance reads on the
SQLite embeddings table) — each with an explicit non-SQLite fallback or an
explicit ``supported: False``, so they do NOT silently exclude a backend. Those
are out of scope here; whether they should unify behind a store capability is
tracked as follow-up bead code_hygiene_mcp-cpc. This test guards the place where
an isinstance gate is genuinely a silent-exclusion bug.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import ariadne_graph.mcp.tools as tools_mod

# The hygiene/analysis handlers whose backend-gating 420 removed. An
# isinstance(SQLiteGraphStore) inside any of these bodies is the silent-exclusion
# regression this guard exists to catch.
_GUARDED_HANDLERS = frozenset(
    {
        "handle_get_architecture",
        "handle_get_dependency_matrix",
        "handle_audit_public_surfaces",
        "handle_list_diagnostics",
    }
)


def _guarded_handlers_with_sqlite_isinstance(tree: ast.AST) -> list[str]:
    """Guarded-handler names that contain an ``isinstance(..., SQLiteGraphStore)``."""
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if fn.name not in _GUARDED_HANDLERS:
            continue
        for call in ast.walk(fn):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "isinstance"
                and any(
                    isinstance(a, ast.Name) and a.id == "SQLiteGraphStore"
                    for a in call.args
                )
            ):
                offenders.append(fn.name)
                break
    return offenders


def test_no_sqlite_isinstance_gate_in_analysis_handlers() -> None:
    src = Path(inspect.getfile(tools_mod)).read_text()
    tree = ast.parse(src)
    offenders = _guarded_handlers_with_sqlite_isinstance(tree)
    assert not offenders, (
        "isinstance(..., SQLiteGraphStore) gate found in a hygiene/analysis "
        "handler; use the dep_edges capability check instead so Memory/Neo4j "
        f"are not silently excluded. Handlers: {sorted(offenders)}"
    )
