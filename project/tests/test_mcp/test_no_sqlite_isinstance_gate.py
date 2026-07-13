"""Lint-style guard: no ``isinstance(store, SQLiteGraphStore)`` hygiene gates.

Bead code_hygiene_mcp-420 replaced the analysis-path ``isinstance`` gates in
``mcp/tools.py`` with a ``dep_edges`` capability check. This pins that they do
not creep back — an isinstance gate silently excludes Memory/Neo4j from the
architecture-intelligence promise, the exact bug this bead killed.
"""

from __future__ import annotations

import re
from pathlib import Path

import ariadne_graph.mcp.tools as tools_mod

_ISINSTANCE_SQLITE = re.compile(r"isinstance\([^)]*SQLiteGraphStore")


def test_no_sqlite_isinstance_gate_in_tools() -> None:
    src = Path(tools_mod.__file__).read_text()
    hits = _ISINSTANCE_SQLITE.findall(src)
    assert not hits, (
        "isinstance(..., SQLiteGraphStore) gate found in mcp/tools.py; "
        "use the dep_edges capability check instead so Memory/Neo4j are not "
        f"silently excluded. Occurrences: {len(hits)}"
    )
