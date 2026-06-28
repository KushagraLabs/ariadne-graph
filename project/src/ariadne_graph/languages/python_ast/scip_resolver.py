"""Resolve Python call sites to definitions using a SCIP-Python index.

This is the *real* (compiler-accurate) cross-file resolution layer. It parses a
``scip-python`` index (reusing the vendored SCIP protobuf bindings and parser in
the ``typescript`` package — no duplicate protobuf) and exposes two lookups:

* :meth:`resolve_call` — given a call site (file + position), return the SCIP
  symbol the callee resolves to.
* :meth:`definition_location` — given a SCIP symbol, return where it is defined
  (file + position), so the caller can map the symbol back to an Ariadne node id.

The Ariadne Python AST extractor already produces node ids keyed by a
definition's file and source line; the adapter combines these two lookups to
rewrite a fuzzy bare-name ``CALLS`` target into the exact node id of the
resolved definition — even when two files define the same name.
"""

from __future__ import annotations

import logging
from pathlib import Path

# Reuse the existing SCIP parser + vendored protobuf bindings (typescript pkg).
from ariadne_graph.languages.typescript.scip_parser import ScipIndexParser, SymbolRole

logger = logging.getLogger(__name__)


class ScipPythonResolver:
    """Positional call->definition resolver backed by a SCIP-Python index."""

    def __init__(self) -> None:
        # SCIP symbol -> (relative_path, def_line_0based, def_col_0based)
        self._def_location: dict[str, tuple[str, int, int]] = {}
        # (relative_path, line_0based, col_0based) -> resolved SCIP symbol
        self._ref_at: dict[tuple[str, int, int], str] = {}

    @classmethod
    def from_index(cls, index_path: Path) -> ScipPythonResolver:
        """Build a resolver by parsing the SCIP index at *index_path*."""
        resolver = cls()
        index = ScipIndexParser().parse(index_path)
        for rel_path, document in index.documents.items():
            rel = str(rel_path)
            for occ in document.occurrences:
                if not occ.symbol:
                    continue
                is_definition = bool(occ.symbol_roles & SymbolRole.DEFINITION)
                if is_definition:
                    # Keep the first (and normally only) definition occurrence.
                    resolver._def_location.setdefault(
                        occ.symbol, (rel, occ.start_line, occ.start_col)
                    )
                else:
                    resolver._ref_at[(rel, occ.start_line, occ.start_col)] = occ.symbol
        return resolver

    def resolve_call(
        self, relative_path: str, callee_line_1based: int, callee_col_0based: int
    ) -> str | None:
        """Return the SCIP symbol for a call site, or ``None`` if unresolved.

        *callee_line_1based* / *callee_col_0based* are the coordinates of the
        callee name token (matching ast's ``lineno`` / ``col_offset``). SCIP
        occurrence lines are 0-based, hence the ``- 1``.
        """
        key = (relative_path, callee_line_1based - 1, callee_col_0based)
        return self._ref_at.get(key)

    def definition_location(self, symbol: str) -> tuple[str, int, int] | None:
        """Return (relative_path, def_line_0based, def_col_0based) for *symbol*."""
        return self._def_location.get(symbol)
