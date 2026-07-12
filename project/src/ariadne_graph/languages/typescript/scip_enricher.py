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
from ariadne_graph.languages.typescript.tsconfig import resolve_relative_import

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
        repo_root: Path | None = None,
    ) -> tuple[
        CodeGraphDelta, set[tuple[int, int, int, int]], dict[tuple[int, int, int, int], str]
    ]:
        """Return an enriched delta plus call ranges and an enclosing map.

        Args:
            file_path: Absolute path to the TypeScript source file.
            scip_delta: Delta produced by :class:`ScipGraphTranslator`.
            repo_root: True repository root, used so grafted import node ids stay
                rooted at the repo (not a subproject dir). Falls back to the
                file's parent when unknown.

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

        # scip-typescript (v0.4.0) never sets the IMPORT symbol-role bit, so the
        # SCIP translator emits no import nodes/edges for any file it covers
        # (bead c66). Supply them from the Tree-sitter extractor, re-sourcing the
        # edges to the SCIP module node so they attach to the file's real node.
        self._merge_imports(file_path, source_bytes, scip_delta, repo_root)

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

    def _merge_imports(
        self,
        file_path: Path,
        source_bytes: bytes,
        scip_delta: CodeGraphDelta,
        repo_root: Path | None = None,
    ) -> None:
        """Add Tree-sitter import nodes/edges to a SCIP delta.

        ``scip-typescript`` does not emit IMPORT-role occurrences, so a
        SCIP-covered file has no CodeImport nodes or IMPORTS_SYMBOL/IMPORTS
        edges. The Tree-sitter extractor already parses imports (with tsconfig
        alias/relative resolution); we run it, take only its import facts, and
        re-source the edges to the SCIP module node so they hang off the file's
        authoritative node rather than the extractor's dotted module id.

        Node identity (module id, import node ids) is derived from the TRUE
        ``repo_root`` so files with the same subpath in different subprojects
        (``mobile/src/x.ts`` vs ``web/src/x.ts``) don't collide. Alias (``@/``)
        resolution, which is a separate concern, uses the nearest tsconfig dir.

        Idempotent: import nodes/edges already present in the delta are skipped,
        so the adapter's repeated ``enrich`` calls don't duplicate them.
        """
        module_id = self._module_id(scip_delta)
        # Identity: true repo root (fall back to file parent only if unknown).
        identity_root = repo_root or file_path.parent
        # Alias (@/...) imports resolve against the nearest tsconfig, which for a
        # subproject file is the subproject dir, not the repo root. This is
        # independent of node identity.
        tsconfig_root = self._nearest_tsconfig_dir(file_path) or identity_root

        # Import the extractor lazily to avoid a module-level import cycle.
        from ariadne_graph.languages.typescript.extractor import TypeScriptFactExtractor
        from ariadne_graph.languages.typescript.tsconfig import TsConfigResolver

        extractor = TypeScriptFactExtractor(
            source=source_bytes.decode("utf-8", errors="replace"),
            file_path=file_path,
            repo_root=identity_root,
            graph_id=scip_delta.graph_id,
            parser_version="scip-enricher-imports",
        )
        # Point alias resolution at the subproject tsconfig without changing the
        # repo-rooted node identity the extractor already computed.
        if tsconfig_root != identity_root:
            extractor._tsconfig_resolver = TsConfigResolver(tsconfig_root)
        ts_delta = extractor.extract()

        import_nodes = {n.id: n for n in ts_delta.nodes if "CodeImport" in n.labels}
        if not import_nodes:
            return
        existing_ids = {n.id for n in scip_delta.nodes}
        abs_path = str(file_path)

        for node_id, node in import_nodes.items():
            # The extractor only resolves tsconfig aliases; add relative-import
            # resolution here so file-to-file dependency queries work for
            # ``./x`` imports too.
            self._resolve_relative(node, file_path)
            if node_id not in existing_ids:
                node.properties["file_path"] = abs_path
                scip_delta.nodes.append(node)
                existing_ids.add(node_id)

        # Identities of edges already in the delta, so a repeated enrich() call
        # doesn't append the same import edge twice (idempotency for edges, not
        # just nodes).
        existing_edges = {(e.source, e.target, e.rel_type) for e in scip_delta.edges}
        for edge in ts_delta.edges:
            # Keep only edges into an import node (IMPORTS_SYMBOL / IMPORTS),
            # re-sourced to the SCIP module node so they hang off the file node.
            target_node = import_nodes.get(edge.target)
            if target_node is None:
                continue
            edge.source = module_id
            edge.properties["owner_file_path"] = abs_path
            resolved = target_node.properties.get("resolved_source")
            if resolved is not None:
                edge.properties["resolved_source"] = resolved
            identity = (edge.source, edge.target, edge.rel_type)
            if identity in existing_edges:
                continue
            existing_edges.add(identity)
            scip_delta.edges.append(edge)

    @staticmethod
    def _nearest_tsconfig_dir(file_path: Path) -> Path | None:
        """Nearest ancestor directory of *file_path* containing a tsconfig.json."""
        for parent in file_path.parents:
            if (parent / "tsconfig.json").exists():
                return parent
        return None

    @staticmethod
    def _resolve_relative(import_node: CodeNode, file_path: Path) -> None:
        """Fill ``resolved_source`` for a relative (``./``/``../``) import.

        Delegates to the shared :func:`resolve_relative_import` SSOT (same logic
        the base extractor uses SCIP-less), so a relative specifier resolves
        against the importing file's directory and file-level dependency queries
        see the edge.
        """
        if import_node.properties.get("resolved_source"):
            return
        resolved = resolve_relative_import(str(import_node.properties.get("source", "")), file_path)
        if resolved:
            import_node.properties["resolved_source"] = resolved

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
