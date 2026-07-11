"""Enrich SCIP-derived deltas with Tree-sitter-only properties.

Runs a lightweight Tree-sitter pass over a TypeScript file to collect:

- call-position ranges (for ``CALLS`` vs ``REFERENCES`` edges)
- an enclosing-definition map (for routing reference edges to the right source)
- extra node properties such as complexity, React labels, decorators, snippets
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ariadne_graph.core.models import CodeGraphDelta, CodeNode
from ariadne_graph.languages.typescript.extractor import HAS_TREE_SITTER

if HAS_TREE_SITTER:
    from tree_sitter import Language, Parser
    from tree_sitter_typescript import language_tsx, language_typescript


def _node_text(node: Any, source_bytes: bytes) -> str:
    text = node.text
    if text is None:
        return ""
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return str(text)


def _ts_node_name(node: Any, source_bytes: bytes) -> str:
    """Return the identifier name for a function/method/class node."""
    # Find an identifier child.
    for child in node.children:
        if child.type == "identifier":
            return _node_text(child, source_bytes)
        if child.type == "property_identifier":
            return _node_text(child, source_bytes)
    return ""


class TreeSitterEnricher:
    """Merge Tree-sitter structural information into a SCIP delta."""

    def enrich(
        self,
        file_path: Path,
        scip_delta: CodeGraphDelta,
    ) -> tuple[
        CodeGraphDelta, set[tuple[int, int, int, int]], dict[tuple[int, int, int, int], str]
    ]:
        """Return an enriched delta plus call ranges and an enclosing map.

        Args:
            file_path: Absolute path to the TypeScript source file.
            scip_delta: Delta produced by :class:`ScipGraphTranslator`.

        Returns:
            A tuple of (enriched_delta, call_ranges, enclosing_map).
        """
        if not HAS_TREE_SITTER:
            return scip_delta, set(), {}

        try:
            source_bytes = file_path.read_bytes()
        except OSError:
            return scip_delta, set(), {}

        tree = self._parse(file_path, source_bytes)
        if tree is None:
            return scip_delta, set(), {}

        call_ranges: set[tuple[int, int, int, int]] = set()
        enclosing_map: dict[tuple[int, int, int, int], str] = {}

        # Build a lookup from (name, start_line, end_line) to SCIP node.
        scip_lookup: dict[tuple[str, int, int], CodeNode] = {}
        for node in scip_delta.nodes:
            name = node.properties.get("name", "")
            line_start = node.properties.get("line_start", 0)
            line_end = node.properties.get("line_end", 0)
            if name:
                scip_lookup[(name, line_start, line_end)] = node

        # The module/file node id is the fallback enclosing id: when a
        # Tree-sitter scope has no matching SCIP definition node, reference
        # edges must still originate from a real node (the file), not a bare
        # display name that matches nothing in the graph.
        module_id = self._module_id(scip_delta)

        # Walk the tree to collect call ranges, enclosing definitions, and
        # extra properties.
        self._walk(
            tree.root_node,
            source_bytes,
            [],
            scip_lookup,
            call_ranges,
            enclosing_map,
            module_id,
        )

        return scip_delta, call_ranges, enclosing_map

    def _parse(self, file_path: Path, source_bytes: bytes) -> Any | None:
        """Parse the file with Tree-sitter."""
        if not HAS_TREE_SITTER:
            return None
        lang_func = language_tsx if file_path.suffix == ".tsx" else language_typescript
        assert lang_func is not None
        language = Language(lang_func())
        parser = Parser(language)
        return parser.parse(source_bytes)

    @staticmethod
    def _module_id(scip_delta: CodeGraphDelta) -> str:
        """Return the module/file node id used as the enclosing fallback."""
        for node in scip_delta.nodes:
            if "CodeModule" in node.labels or "CodeFile" in node.labels:
                return node.id
        # Fallback to the delta's file_path key if no module node was emitted.
        return scip_delta.file_path

    def _walk(
        self,
        node: Any,
        source_bytes: bytes,
        scope_stack: list[tuple[str, int, int, str]],
        scip_lookup: dict[tuple[str, int, int], CodeNode],
        call_ranges: set[tuple[int, int, int, int]],
        enclosing_map: dict[tuple[int, int, int, int], str],
        module_id: str,
    ) -> None:
        """Recursively walk the Tree-sitter AST."""
        start_line, start_col = node.start_point
        end_line, end_col = node.end_point

        # Track call expressions.
        if node.type in {"call_expression", "new_expression"}:
            call_ranges.add((start_line, start_col, end_line, end_col))

        # Track function/method/class scope entries.
        scope_info = None
        if node.type in {
            "function_declaration",
            "method_definition",
            "class_declaration",
            "arrow_function",
            "function",
        }:
            name = _ts_node_name(node, source_bytes)
            if name:
                # Try to find a matching SCIP node to use its ID as the
                # enclosing definition. Fallback to the raw name.
                key = (name, start_line + 1, end_line + 1)
                scip_node = scip_lookup.get(key)
                # Fall back to the module/file node id (a real node) rather than
                # the bare display name, which matches no node in the graph.
                scope_id = scip_node.id if scip_node else module_id
                scope_info = (name, start_line, end_line, scope_id)
                scope_stack.append(scope_info)

        # Map this node's range to the innermost enclosing scope.
        if scope_stack:
            enclosing_id = scope_stack[-1][3]
            enclosing_map[(start_line, start_col, end_line, end_col)] = enclosing_id

        # Copy Tree-sitter-only properties to matching SCIP nodes.
        if scope_info:
            name, start_line, end_line, scope_id = scope_info
            key = (name, start_line + 1, end_line + 1)
            scip_node = scip_lookup.get(key)
            if scip_node:
                self._copy_properties(scip_node, node, source_bytes)

        for child in node.children:
            self._walk(
                child,
                source_bytes,
                scope_stack,
                scip_lookup,
                call_ranges,
                enclosing_map,
                module_id,
            )

        if scope_info:
            scope_stack.pop()

    def _copy_properties(
        self,
        scip_node: CodeNode,
        ts_node: Any,
        source_bytes: bytes,
    ) -> None:
        """Copy Tree-sitter-only properties onto a SCIP node."""
        # Snippet: the source text of the definition node.
        if not scip_node.properties.get("snippet"):
            scip_node.properties["snippet"] = _node_text(ts_node, source_bytes)

        # React heuristics.
        name = scip_node.properties.get("name", "")
        if name and name[0].isupper():
            scip_node.properties["is_react_component"] = True
        if name and name.startswith("use") and len(name) > 3 and name[3].isupper():
            scip_node.properties["is_hook"] = True

        # Decorators (TS/JS decorators are not yet widely supported in
        # tree-sitter-typescript, but we record them if present).
        decorators = []
        for child in ts_node.children:
            if child.type == "decorator":
                decorators.append(_node_text(child, source_bytes))
        if decorators:
            scip_node.properties["decorators"] = decorators

        # Complexity: count branching statements inside the function body.
        complexity = self._complexity(ts_node)
        if complexity > 0:
            scip_node.properties["complexity"] = complexity

    def _complexity(self, node: Any) -> int:
        """Compute a simple cyclomatic complexity for a function node."""
        branches = {
            "if_statement",
            "switch_case",
            "for_statement",
            "for_in_statement",
            "while_statement",
            "do_statement",
            "catch_clause",
            "conditional_expression",
        }
        count = 1  # base path
        for child in self._iter_descendants(node):
            if child.type in branches:
                count += 1
        return count

    def _iter_descendants(self, node: Any) -> Iterator[Any]:
        """Yield all descendant Tree-sitter nodes."""
        for child in node.children:
            yield child
            yield from self._iter_descendants(child)
