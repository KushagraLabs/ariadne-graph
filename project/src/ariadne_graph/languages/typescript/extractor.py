"""Tree-sitter fact extractor for TypeScript and TSX.

Parses ``.ts`` and ``.tsx`` source files with tree-sitter and extracts a code
knowledge graph containing modules, classes, interfaces, functions, methods,
variables, imports, exports, call relationships, and diagnostics.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from tree_sitter_typescript import language_tsx, language_typescript

    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False
    language_typescript = None  # type: ignore[assignment]
    language_tsx = None  # type: ignore[assignment]

from ariadne_graph.core.models import CodeEdge, CodeGraphDelta, CodeNode
from ariadne_graph.languages.base import UniqueIdMixin
from ariadne_graph.languages.typescript.tsconfig import (
    TsConfigResolver,
    resolve_relative_import,
)

# Identifier tokens inside a rendered type expression (e.g. "Array<Widget>").
# Used to attribute type-position usage back to imports (bead
# code_hygiene_mcp-df7).
_TYPE_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _text_str(node: Any) -> str:
    """Return node.text decoded as UTF-8, falling back to empty string."""
    text = node.text
    if text is None:
        return ""
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return str(text)


class TypeScriptFactExtractor(UniqueIdMixin):
    """Tree-sitter extractor for TypeScript/TSX source files.

    Usage::

        extractor = TypeScriptFactExtractor(
            source=source_code,
            file_path=path,
            repo_root=repo_root,
            graph_id="graph-id",
            parser_version="tree-sitter-typescript_x.y.z",
            source_commit=None,
        )
        delta = extractor.extract()
    """

    def __init__(
        self,
        source: str,
        file_path: Path,
        repo_root: Path,
        graph_id: str,
        parser_version: str,
        source_commit: str | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.file_path = file_path
        self.repo_root = repo_root
        self.graph_id = graph_id
        self.parser_version = parser_version
        self.source_commit = source_commit

        self.source_bytes = source.encode("utf-8")
        self.source_lines = source.splitlines()
        self.module_name = _module_name_from_path(file_path, repo_root)
        self.is_tsx = file_path.suffix == ".tsx"
        self._tsconfig_resolver = TsConfigResolver(repo_root)

        # Accumulators
        self.nodes: list[CodeNode] = []
        self.edges: list[CodeEdge] = []

        # State tracking
        self._class_stack: list[str] = []
        self._function_stack: list[str] = []
        # Stack of value-namespace binding scopes (bead code_hygiene_mcp-ukr).
        # Each frame holds the locally-bound names in scope; a name that resolves
        # to one of these shadows an import of the same name, so
        # _mark_import_used must NOT mark the import used. See _push_scope.
        self._scope_stack: list[set[str]] = []
        self._imports: list[dict[str, Any]] = []
        self._import_nodes: list[CodeNode] = []

    @property
    def _current_class(self) -> str | None:
        """Return dot-separated qualified class name, e.g. 'Outer.Inner'."""
        return ".".join(self._class_stack) if self._class_stack else None

    @property
    def _current_function(self) -> str | None:
        """Return the innermost enclosing function id."""
        return self._function_stack[-1] if self._function_stack else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self) -> CodeGraphDelta:
        """Parse the source and return the extracted graph delta.

        On parse errors returns an empty delta with the error message stored
        in *parser_version*.
        """
        if not HAS_TREE_SITTER:
            return CodeGraphDelta(
                graph_id=self.graph_id,
                file_path=str(self.file_path),
                nodes=[],
                edges=[],
                content_hash="",
                parser_version="error:tree-sitter-not-installed",
            )

        try:
            tree = self._parse()
        except Exception as exc:
            return CodeGraphDelta(
                graph_id=self.graph_id,
                file_path=str(self.file_path),
                nodes=[],
                edges=[],
                content_hash="",
                parser_version=f"error:parse:{exc}",
            )

        root = tree.root_node
        self._emit_file_and_module(root)
        for child in root.children:
            self._visit_top_level(child)

        # Post-process diagnostics
        self._detect_unused_imports()

        return CodeGraphDelta(
            graph_id=self.graph_id,
            file_path=str(self.file_path),
            nodes=self.nodes,
            edges=self.edges,
            content_hash="",
            parser_version=self.parser_version,
        )

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self) -> Any:
        from tree_sitter import Language, Parser

        lang_func = language_tsx if self.is_tsx else language_typescript
        assert lang_func is not None
        language = Language(lang_func())
        parser = Parser(language)
        return parser.parse(self.source_bytes)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_node(
        self,
        node_id: str,
        labels: list[str],
        properties: dict[str, Any] | None = None,
    ) -> CodeNode:
        """Create and register a CodeNode."""
        props = dict(properties or {})
        props.setdefault("file_path", str(self.file_path))
        props.setdefault("module", self.module_name)
        if self.source_commit:
            props.setdefault("source_commit", self.source_commit)
        code_node = CodeNode(
            id=node_id,
            graph_id=self.graph_id,
            labels=["KnowledgeNode"] + labels,
            properties=props,
        )
        self.nodes.append(code_node)
        self._used_node_ids.add(node_id)
        return code_node

    def _add_edge(
        self,
        source: str,
        target: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> CodeEdge:
        """Create and register a CodeEdge."""
        props = dict(properties or {})
        props.setdefault("owner_file_path", str(self.file_path))
        if self.source_commit:
            props.setdefault("source_commit", self.source_commit)
        edge = CodeEdge(
            source=source,
            target=target,
            graph_id=self.graph_id,
            rel_type=rel_type,
            properties=props,
        )
        self.edges.append(edge)
        return edge

    def _line_range(self, node: Any) -> dict[str, int]:
        """Return line_start / line_end dict for a tree-sitter node."""
        result: dict[str, int] = {}
        if node.start_point is not None:
            result["line_start"] = node.start_point[0] + 1
        if node.end_point is not None:
            result["line_end"] = node.end_point[0] + 1
        return result

    def _node_text(self, node: Any) -> str:
        """Return the source text for a tree-sitter node."""
        try:
            return self.source_bytes[node.start_byte : node.end_byte].decode("utf-8")
        except (IndexError, UnicodeDecodeError):
            return ""

    def _make_snippet(self, node: Any) -> str:
        """Return the source text for a tree-sitter node."""
        return self._node_text(node)

    def _parent_module_id(self) -> str:
        return self.module_name

    # ------------------------------------------------------------------
    # File / module
    # ------------------------------------------------------------------

    def _emit_file_and_module(self, root: Any) -> None:
        file_id = str(self.file_path.resolve())
        self._add_node(
            file_id,
            ["CodeFile"],
            {
                "name": self.file_path.name,
                "file_path": str(self.file_path),
                "module": self.module_name,
                "language": "typescript",
                "is_tsx": self.is_tsx,
                "lines": len(self.source_lines),
            },
        )

        mod_id = self.module_name
        self._add_node(
            mod_id,
            ["CodeModule"],
            {
                "name": self.module_name,
                "file_path": str(self.file_path),
                "module": self.module_name,
            },
        )

        self._add_edge(file_id, mod_id, "CONTAINS")

    # ------------------------------------------------------------------
    # Top-level dispatch
    # ------------------------------------------------------------------

    def _visit_top_level(self, node: Any) -> None:
        node_type = node.type

        if node_type == "import_statement":
            self._process_import(node)
        elif node_type == "export_statement":
            self._process_export(node)
        elif node_type in ("class_declaration", "abstract_class_declaration"):
            self._process_class(node)
        elif node_type == "interface_declaration":
            self._process_interface(node)
        elif node_type == "type_alias_declaration":
            self._process_type_alias(node)
        elif node_type in ("function_declaration", "generator_function_declaration"):
            self._process_function(node)
        elif node_type in ("lexical_declaration", "variable_declaration"):
            self._process_variable_declaration(node)
        elif node_type == "expression_statement":
            self._process_expression_statement(node)
        elif node_type == "statement_block":
            for child in node.children:
                self._visit_top_level(child)

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def _process_import(self, node: Any) -> None:
        source_node = _child_by_type(node, "string")
        source = _string_value(source_node) if source_node else ""
        clause = _child_by_type(node, "import_clause")
        if clause is None:
            return

        # namespace import: import * as foo from "..."
        namespace_import = _child_by_type(clause, "namespace_import")
        if namespace_import is not None:
            name_node = _child_by_type(namespace_import, "identifier")
            name = _text_str(name_node) if name_node else ""
            self._register_import(name, "*", source, "namespace", node)
            return

        # default import: import foo from "..." or import foo, { ... } from "..."
        default_identifier = None
        for child in clause.children:
            if child.type == "identifier":
                default_identifier = child
                break

        if default_identifier is not None:
            name = _text_str(default_identifier)
            self._register_import(name, name, source, "default", node)

        # named imports: import { foo, bar as baz } from "..."
        named_imports = _child_by_type(clause, "named_imports")
        if named_imports is not None:
            for specifier in _children_by_type(named_imports, "import_specifier"):
                orig_node = _child_by_type(specifier, "identifier")
                if orig_node is None:
                    continue
                orig_name = _text_str(orig_node)
                alias_node = None
                for child in specifier.children:
                    if child.type == "identifier" and child != orig_node:
                        alias_node = child
                        break
                alias = _text_str(alias_node) if alias_node else orig_name
                self._register_import(alias, orig_name, source, "named", node)

    def _register_import(
        self,
        local_name: str,
        imported_name: str,
        source: str,
        import_type: str,
        node: Any,
    ) -> None:
        node_id = f"{self.module_name}:import:{source}:{imported_name}:{local_name}"
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        properties: dict[str, Any] = {
            "name": imported_name,
            "asname": local_name,
            "import_type": import_type,
            "source": source,
            "snippet": snippet,
            **line_info,
        }
        edge_properties: dict[str, Any] = {
            "symbol": imported_name,
            "asname": local_name,
            "from_module": source,
        }

        # tsconfig aliases first; fall back to relative-specifier resolution so a
        # SCIP-less run still records ``./x``/``../x`` targets (the common case).
        # Same SSOT the enricher uses on the SCIP path.
        resolved_source = self._tsconfig_resolver.resolve(source) or resolve_relative_import(
            source, self.file_path
        )
        if resolved_source:
            properties["resolved_source"] = resolved_source
            try:
                resolved_module = _module_name_from_path(Path(resolved_source), self.repo_root)
            except Exception:
                resolved_module = resolved_source
            properties["resolved_module"] = resolved_module
            edge_properties["resolved_source"] = resolved_source
            edge_properties["resolved_module"] = resolved_module

        import_node = self._add_node(
            node_id,
            ["CodeImport"],
            properties,
        )
        self._import_nodes.append(import_node)

        rel_type = "IMPORTS_SYMBOL" if import_type in ("named", "default") else "IMPORTS"
        self._add_edge(
            self._parent_module_id(),
            node_id,
            rel_type,
            edge_properties,
        )
        self._imports.append(
            {
                "name": local_name,
                "imported_name": imported_name,
                "node_id": node_id,
                "used": False,
            }
        )

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------

    def _process_export(self, node: Any) -> None:
        line_info = self._line_range(node)

        # export * from "..."
        if _child_by_type(node, "*") is not None and _child_by_type(node, "string") is not None:
            source_node = _child_by_type(node, "string")
            source = _string_value(source_node) if source_node else ""
            node_id = f"{self.module_name}:export:*:{source}"
            # Guard against double-dispatch of the same export statement.
            if node_id in self._used_node_ids:
                return
            self._add_node(
                node_id,
                ["CodeExport"],
                {"export_type": "reexport_all", "source": source, **line_info},
            )
            self._add_edge(
                self._parent_module_id(),
                node_id,
                "EXPORTS",
                {"symbol": "*", "source": source},
            )
            return

        declaration = node.child_by_field_name("declaration")
        is_default = _child_by_type(node, "default") is not None

        # export { foo, bar as baz } from "..."
        export_clause = _child_by_type(node, "export_clause")
        if export_clause is not None:
            source_node = _child_by_type(node, "string")
            source = _string_value(source_node) if source_node else ""
            for specifier in _children_by_type(export_clause, "export_specifier"):
                name_node = _child_by_type(specifier, "identifier")
                if name_node is None:
                    continue
                orig_name = _text_str(name_node)
                alias = orig_name
                for child in specifier.children:
                    if child.type == "identifier" and child != name_node:
                        alias = _text_str(child)
                        break
                node_id = f"{self.module_name}:export:{orig_name}"
                if node_id in self._used_node_ids:
                    continue
                self._add_node(
                    node_id,
                    ["CodeExport"],
                    {
                        "name": orig_name,
                        "asname": alias,
                        "export_type": "reexport_named",
                        "source": source,
                        **line_info,
                    },
                )
                self._add_edge(
                    self._parent_module_id(),
                    node_id,
                    "EXPORTS",
                    {"symbol": orig_name, "asname": alias, "source": source},
                )
            return

        if declaration is None:
            # `export default <expr>` (e.g. `export default defineConfig({...})`)
            # carries its expression in the `value` field, not `declaration`; the
            # TS `export = <expr>` assignment carries it as an unnamed child. Walk
            # it so value-position callees mark imports used (bead
            # code_hygiene_mcp-bhe). No export node is emitted here: anonymous
            # default expressions have no exportable symbol name.
            value = node.child_by_field_name("value") or _child_by_types(
                node, ["call_expression", "new_expression", "identifier", "member_expression"]
            )
            if value is not None:
                # `export default <expr>` is a value-position use of whatever it
                # names, but _visit_expression only marks imports on call/new
                # callees. A default export can wrap the imported binding in
                # arbitrary syntax -- `config`, `foo.bar`, `(config)`,
                # `config as C`, `{ config }` -- so mark every value-position
                # identifier in the export expression as a potential import use
                # (bead code_hygiene_mcp-bhe). Scoped to this subtree, not the
                # whole traversal, to avoid over-marking ordinary references.
                self._mark_export_value_identifiers(value)
                self._visit_expression(value)
            return

        # Let the declaration emit its own node, then add export edge.
        if declaration.type in ("class_declaration", "abstract_class_declaration"):
            self._process_class(declaration, exported=True, is_default=is_default)
        elif declaration.type == "interface_declaration":
            self._process_interface(declaration, exported=True, is_default=is_default)
        elif declaration.type == "type_alias_declaration":
            self._process_type_alias(declaration, exported=True, is_default=is_default)
        elif declaration.type in ("function_declaration", "generator_function_declaration"):
            self._process_function(declaration, exported=True, is_default=is_default)
        elif declaration.type in ("lexical_declaration", "variable_declaration"):
            self._process_variable_declaration(declaration, exported=True, is_default=is_default)

    # ------------------------------------------------------------------
    # Classes
    # ------------------------------------------------------------------

    def _process_class(self, node: Any, exported: bool = False, is_default: bool = False) -> None:
        name_node = _child_by_type(node, "type_identifier")
        if name_node is None:
            name_node = _child_by_type(node, "identifier")
        class_name = _text_str(name_node) if name_node else "<anonymous>"

        self._class_stack.append(class_name)
        qualname = _qualname(self.module_name, self._current_class)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        props: dict[str, Any] = {
            "name": class_name,
            "qualname": qualname,
            "snippet": snippet,
            **line_info,
        }
        if is_default:
            props["is_default_export"] = True

        base_names: list[str] = []
        implements: list[str] = []

        heritage = _child_by_type(node, "class_heritage")
        if heritage is not None:
            extends_clause = _child_by_type(heritage, "extends_clause")
            if extends_clause is not None:
                base_type = _child_by_types(extends_clause, ["identifier", "type_identifier"])
                if base_type is not None:
                    base_name = _text_str(base_type)
                    base_names.append(base_name)
                    self._mark_import_used(base_name)
                    self._add_edge(
                        qualname,
                        base_name,
                        "INHERITS",
                        {"base": base_name},
                    )

            implements_clause = _child_by_type(heritage, "implements_clause")
            if implements_clause is not None:
                for child in implements_clause.children:
                    if child.type in ("identifier", "type_identifier"):
                        iface_name = _text_str(child)
                        implements.append(iface_name)
                        self._mark_import_used(iface_name)
                        self._add_edge(
                            qualname,
                            iface_name,
                            "IMPLEMENTS",
                            {"interface": iface_name},
                        )

        decorators = _collect_decorators(node)
        for dec in decorators:
            self._add_edge(qualname, dec, "DECORATED_BY", {"decorator": dec})

        props["bases"] = base_names
        props["implements"] = implements
        props["decorators"] = decorators

        self._add_node(qualname, ["CodeClass"], props)
        self._add_edge(self._parent_module_id(), qualname, "CONTAINS")
        self._add_edge(self._parent_module_id(), qualname, "DEFINES")

        if exported:
            self._add_edge(
                self._parent_module_id(),
                qualname,
                "EXPORTS",
                {"symbol": class_name, "default": is_default},
            )

        body = _child_by_type(node, "class_body")
        if body is not None:
            for child in body.children:
                if child.type in (
                    "method_definition",
                    "abstract_method_signature",
                ):
                    self._process_method(child, qualname)
                elif child.type == "public_field_definition":
                    self._process_field(child, qualname)

        self._class_stack.pop()

    # ------------------------------------------------------------------
    # Interfaces
    # ------------------------------------------------------------------

    def _process_interface(
        self, node: Any, exported: bool = False, is_default: bool = False
    ) -> None:
        name_node = _child_by_type(node, "type_identifier")
        iface_name = _text_str(name_node) if name_node else "<anonymous>"
        qualname = _qualname(self.module_name, iface_name)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        props: dict[str, Any] = {
            "name": iface_name,
            "qualname": qualname,
            "snippet": snippet,
            **line_info,
        }
        if is_default:
            props["is_default_export"] = True

        extends_types: list[str] = []
        extends_clause = _child_by_type(node, "extends_clause")
        if extends_clause is not None:
            for child in extends_clause.children:
                if child.type in ("identifier", "type_identifier"):
                    base_name = _text_str(child)
                    extends_types.append(base_name)
                    self._mark_import_used(base_name)
                    self._add_edge(
                        qualname,
                        base_name,
                        "INHERITS",
                        {"base": base_name},
                    )
        props["bases"] = extends_types

        self._add_node(qualname, ["CodeInterface"], props)
        self._add_edge(self._parent_module_id(), qualname, "CONTAINS")
        self._add_edge(self._parent_module_id(), qualname, "DEFINES")

        if exported:
            self._add_edge(
                self._parent_module_id(),
                qualname,
                "EXPORTS",
                {"symbol": iface_name, "default": is_default},
            )

        body = _child_by_type(node, "interface_body")
        if body is not None:
            for child in body.children:
                if child.type == "property_signature":
                    self._process_interface_property(child, qualname)
                elif child.type == "method_signature":
                    self._process_method_signature(child, qualname)

    # ------------------------------------------------------------------
    # Type aliases
    # ------------------------------------------------------------------

    def _process_type_alias(
        self, node: Any, exported: bool = False, is_default: bool = False
    ) -> None:
        name_node = _child_by_type(node, "type_identifier")
        alias_name = _text_str(name_node) if name_node else "<anonymous>"
        base_id = f"{self.module_name}:type:{alias_name}"
        node_id = self._unique_node_id(base_id, node)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        value_node = None
        for child in node.children:
            if child.type == "=":
                continue
            if child.type in (
                "type",
                "type_identifier",
                ";",
            ):
                continue
            value_node = child
            break

        props: dict[str, Any] = {
            "name": alias_name,
            "qualname": alias_name,
            "snippet": snippet,
            **line_info,
        }
        if is_default:
            props["is_default_export"] = True
        if value_node is not None:
            props["type_text"] = self._node_text(value_node)
            used_types = _collect_type_identifiers(value_node)
            props["used_types"] = used_types
            # No AST USES_TYPE edge (target would be a raw type name, never a
            # resolved node -- bead code_hygiene_mcp-0b9); SCIP emits the
            # resolved form. Still mark type-position imports used (bead
            # code_hygiene_mcp-df7).
            for used_type in used_types:
                self._mark_import_used(used_type)

        self._add_node(node_id, ["CodeTypeAlias"], props)
        self._add_edge(self._parent_module_id(), node_id, "CONTAINS")
        self._add_edge(self._parent_module_id(), node_id, "DEFINES")

        if exported:
            self._add_edge(
                self._parent_module_id(),
                node_id,
                "EXPORTS",
                {"symbol": alias_name, "default": is_default},
            )

    # ------------------------------------------------------------------
    # Functions / methods / arrow functions
    # ------------------------------------------------------------------

    def _process_function(
        self,
        node: Any,
        exported: bool = False,
        is_default: bool = False,
        parent_class_id: str | None = None,
        parent_class_name: str | None = None,
        forced_name: str | None = None,
    ) -> str:
        name_node = _child_by_type(node, "identifier")
        if name_node is None:
            name_node = _child_by_type(node, "property_identifier")
        func_name = forced_name or (_text_str(name_node) if name_node else None) or "<anonymous>"

        is_method = parent_class_id is not None
        is_async = _child_by_type(node, "async") is not None
        is_generator = node.type == "generator_function_declaration"
        is_arrow = node.type == "arrow_function"

        if is_method and parent_class_id:
            qualname = _qualname(self.module_name, parent_class_name, func_name)
            labels = ["CodeMethod"]
        else:
            qualname = _qualname(self.module_name, func_name=func_name)
            labels = ["CodeFunction"]

        node_id = self._unique_node_id(qualname, node)

        # React component / hook detection
        if self.is_tsx and func_name[0:1].isupper():
            labels.append("CodeReactComponent")
        if self.is_tsx and func_name.startswith("use") and func_name != "use":
            labels.append("CodeHook")

        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        props: dict[str, Any] = {
            "name": func_name,
            "qualname": qualname,
            "snippet": snippet,
            **line_info,
        }
        if is_async:
            props["is_async"] = True
        if is_generator:
            props["is_generator"] = True
        if is_arrow:
            props["is_arrow"] = True
        if is_default:
            props["is_default_export"] = True

        decorators = _collect_decorators(node)
        for dec in decorators:
            self._add_edge(node_id, dec, "DECORATED_BY", {"decorator": dec})
        props["decorators"] = decorators

        # Parameters and return type
        arg_types: list[str] = []
        formal_params = _child_by_type(node, "formal_parameters")
        if formal_params is not None:
            for param in formal_params.children:
                if param.type in (
                    "required_parameter",
                    "optional_parameter",
                    "rest_parameter",
                ):
                    param_name, type_name = _extract_parameter_info(param)
                    if type_name:
                        arg_types.append(type_name)
                        self._mark_type_used(type_name)

        return_type_node = _child_by_type(node, "type_annotation")
        if return_type_node is not None:
            return_type = _type_annotation_name(return_type_node)
            if return_type:
                props["return_type"] = return_type
                self._mark_type_used(return_type)

        if arg_types:
            props["arg_types"] = arg_types

        complexity = self._compute_complexity(node)
        props["complexity"] = complexity

        self._add_node(node_id, labels, props)

        if is_method and parent_class_id:
            self._add_edge(parent_class_id, node_id, "CONTAINS")
            self._add_edge(self._parent_module_id(), node_id, "DEFINES")
        else:
            self._add_edge(self._parent_module_id(), node_id, "CONTAINS")
            self._add_edge(self._parent_module_id(), node_id, "DEFINES")

        if exported:
            self._add_edge(
                self._parent_module_id(),
                node_id,
                "EXPORTS",
                {"symbol": func_name, "default": is_default},
            )

        # Visit body for call tracking. Push this function's parameter scope so a
        # reference to a shadowing param is not mis-marked as an import use (bead
        # code_hygiene_mcp-ukr). The body block pushes its own frame for locals
        # (see _visit_expression). The `body` field covers both a
        # `statement_block` and an expression-bodied arrow (`() => foo`, whose
        # body is a bare identifier the old type-list lookup missed).
        body = node.child_by_field_name("body") or _child_by_types(
            node, ["statement_block", "expression"]
        )
        if body is not None:
            self._function_stack.append(node_id)
            self._push_scope(self._function_scope_bindings(node))
            self._visit_expression(body)
            self._pop_scope()
            self._function_stack.pop()

        return node_id

    def _process_method(self, node: Any, parent_class_id: str) -> None:
        name_node = _child_by_type(node, "property_identifier")
        if name_node is None:
            name_node = _child_by_type(node, "identifier")
        method_name = _text_str(name_node) if name_node else "<anonymous>"
        parent_class_name = parent_class_id.split(".")[-1]
        is_static = any(
            child.type in ("static", "readonly", "accessor", "async") for child in node.children
        )

        node_id = self._process_function(
            node,
            parent_class_id=parent_class_id,
            parent_class_name=parent_class_name,
        )

        # Override detection: if method name exists in any base class, emit OVERRIDES.
        base_classes: list[str] = []
        for edge in self.edges:
            if edge.source == parent_class_id and edge.rel_type == "INHERITS":
                base_classes.append(edge.target)
        for base in base_classes:
            self._add_edge(
                node_id,
                f"{base}.{method_name}",
                "OVERRIDES",
                {"base_class": base, "method": method_name},
            )

        if is_static:
            method_node = self.nodes[-1] if self.nodes else None
            if method_node and method_node.id == node_id:
                method_node.properties["is_static"] = True

    def _process_method_signature(self, node: Any, parent_interface_id: str) -> None:
        name_node = _child_by_type(node, "property_identifier")
        if name_node is None:
            return
        method_name = _text_str(name_node)
        parent_name = parent_interface_id.split(".")[-1]
        qualname = _qualname(self.module_name, parent_name, method_name)
        node_id = self._unique_node_id(qualname, node)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        self._add_node(
            node_id,
            ["CodeMethod"],
            {
                "name": method_name,
                "qualname": qualname,
                "snippet": snippet,
                **line_info,
            },
        )
        self._add_edge(parent_interface_id, node_id, "CONTAINS")
        self._add_edge(self._parent_module_id(), node_id, "DEFINES")

    # ------------------------------------------------------------------
    # Class fields / interface properties
    # ------------------------------------------------------------------

    def _process_field(self, node: Any, parent_class_id: str) -> None:
        name_node = _child_by_type(node, "property_identifier")
        if name_node is None:
            return
        field_name = _text_str(name_node)
        base_attr_id = f"{parent_class_id}.attr:{field_name}"
        attr_id = self._unique_node_id(base_attr_id, node)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        type_node = _child_by_type(node, "type_annotation")
        type_name = _type_annotation_name(type_node) if type_node else None

        props: dict[str, Any] = {
            "name": field_name,
            "class": parent_class_id.split(".")[-1],
            "snippet": snippet,
            **line_info,
        }
        if type_name:
            props["type_annotation"] = type_name

        self._add_node(attr_id, ["CodeAttribute"], props)
        self._add_edge(parent_class_id, attr_id, "CONTAINS")
        if type_name:
            self._mark_type_used(type_name)

    def _process_interface_property(self, node: Any, parent_interface_id: str) -> None:
        name_node = _child_by_type(node, "property_identifier")
        if name_node is None:
            return
        prop_name = _text_str(name_node)
        base_attr_id = f"{parent_interface_id}.attr:{prop_name}"
        attr_id = self._unique_node_id(base_attr_id, node)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        type_node = _child_by_type(node, "type_annotation")
        type_name = _type_annotation_name(type_node) if type_node else None

        props: dict[str, Any] = {
            "name": prop_name,
            "interface": parent_interface_id.split(".")[-1],
            "snippet": snippet,
            **line_info,
        }
        if type_name:
            props["type_annotation"] = type_name

        self._add_node(attr_id, ["CodeAttribute"], props)
        self._add_edge(parent_interface_id, attr_id, "CONTAINS")
        if type_name:
            self._mark_type_used(type_name)

    # ------------------------------------------------------------------
    # Variables / lexical declarations
    # ------------------------------------------------------------------

    def _process_variable_declaration(
        self, node: Any, exported: bool = False, is_default: bool = False
    ) -> None:
        for declarator in _children_by_type(node, "variable_declarator"):
            name_node = _child_by_type(declarator, "identifier")
            if name_node is None:
                continue
            var_name = _text_str(name_node)

            # Is this an arrow function assigned to a variable?
            arrow = _child_by_type(declarator, "arrow_function")
            if arrow is not None:
                self._process_function(
                    arrow,
                    exported=exported,
                    is_default=is_default,
                    forced_name=var_name,
                )
                continue

            # Detect require("...") imports
            require_source = _require_source(declarator)
            if require_source is not None:
                self._register_import(
                    var_name,
                    require_source,
                    require_source,
                    "require",
                    declarator,
                )

            var_id = self._unique_node_id(f"{self.module_name}:var:{var_name}", declarator)
            snippet = self._make_snippet(declarator)
            line_info = self._line_range(declarator)

            type_node = _child_by_type(declarator, "type_annotation")
            type_name = _type_annotation_name(type_node) if type_node else None

            props: dict[str, Any] = {
                "name": var_name,
                "module": self.module_name,
                "snippet": snippet,
                **line_info,
            }
            if is_default:
                props["is_default_export"] = True
            if type_name:
                props["type_annotation"] = type_name

            self._add_node(var_id, ["CodeVariable"], props)
            self._add_edge(self._parent_module_id(), var_id, "CONTAINS")
            if type_name:
                self._mark_type_used(type_name)

            if exported:
                self._add_edge(
                    self._parent_module_id(),
                    var_id,
                    "EXPORTS",
                    {"symbol": var_name, "default": is_default},
                )

            # Visit initializer for call tracking, but skip arrow functions
            # handled above.
            value = _child_by_types(
                declarator,
                [
                    "call_expression",
                    "new_expression",
                    "binary_expression",
                    "unary_expression",
                    "object",
                    "array",
                    "template_string",
                    "string",
                    "number",
                ],
            )
            if value is not None:
                self._function_stack.append(var_id)
                self._visit_expression(value)
                self._function_stack.pop()

    # ------------------------------------------------------------------
    # Expression statements / call tracking
    # ------------------------------------------------------------------

    def _process_expression_statement(self, node: Any) -> None:
        child = node.child_by_field_name("expression") or (
            node.children[0] if node.children else None
        )
        if child is not None:
            self._visit_expression(child)

    def _visit_body(self, node: Any) -> None:
        for child in node.children:
            self._visit_expression(child)

    def _visit_expression(self, node: Any) -> None:
        if node is None:
            return

        node_type = node.type

        # Handlers for declarations manage their own child traversal (and may
        # push/pop context such as the function/class stack). Return after them
        # so the generic loop below does not re-walk the same subtree.
        if node_type in (
            "function_declaration",
            "generator_function_declaration",
            "arrow_function",
        ):
            self._process_function(node)
            return
        # An anonymous `function (...) {}` expression (a callback) opens its own
        # parameter/var scope, but -- unlike a declaration or a named/assigned
        # arrow -- it produces no CodeFunction node. Walk it for import usage
        # under its own scope frame WITHOUT emitting a node, so a shadowing param
        # is not mis-marked as an import use (bead code_hygiene_mcp-ukr, codex
        # review) and the node set is unchanged.
        if node_type in ("function_expression", "generator_function_expression"):
            self._visit_anonymous_function(node)
            return
        if node_type == "class_declaration":
            self._process_class(node)
            return
        # An anonymous `class {}` expression emits no node here; its methods
        # still open parameter scopes, so walk it scope-aware without a node
        # (bead code_hygiene_mcp-ukr, codex review). A method_definition is a
        # function for scoping purposes.
        if node_type == "class":
            self._visit_anonymous_class(node)
            return
        if node_type == "method_definition":
            self._visit_anonymous_function(node)
            return
        if node_type in ("variable_declaration", "lexical_declaration"):
            self._process_variable_declaration(node)
            return

        # Nodes that open a lexical value scope get their own frame so a local
        # binding shadows an import only within its actual scope (bead
        # code_hygiene_mcp-ukr). A block's locals are pushed here rather than
        # pooled into the enclosing function, so a reference in an outer block
        # still resolves to the import (codex review finding).
        if node_type == "statement_block":
            self._push_scope(self._block_bindings(node))
            for child in node.children:
                self._visit_expression(child)
            self._pop_scope()
            return
        if node_type in ("for_statement", "for_in_statement"):
            self._visit_loop(node)
            return
        if node_type == "catch_clause":
            self._visit_catch_clause(node)
            return
        if node_type == "switch_body":
            # `case` declarations share one block scope (the switch body); a
            # `const` in a case can be referenced by a later case.
            self._push_scope(self._switch_body_bindings(node))
            for child in node.children:
                self._visit_expression(child)
            self._pop_scope()
            return

        # Call/new expressions mark their callee's import used; recursion into
        # their children is handled by the single loop below.
        if node_type == "call_expression":
            self._process_call(node)
        elif node_type == "new_expression":
            self._process_new_expression(node)
        elif node_type in ("as_expression", "satisfies_expression"):
            # `x as Foo` / `x satisfies Foo`: the type operand is a
            # type-position use of an import (bead code_hygiene_mcp-df7).
            for child in node.children:
                if child.type in ("type_identifier", "generic_type", "nested_type_identifier"):
                    self._mark_type_used(_text_str(child))
        elif node_type == "member_expression":
            # `ns.thing` / `foo.bar`: resolve to the root-qualified name so the
            # namespace-prefix match in _mark_import_used still fires (bead
            # code_hygiene_mcp-ukr). The `.property` is a property_identifier,
            # not a bare identifier, so the child walk below won't re-mark it.
            name = _expression_name(node)
            if name:
                self._mark_import_used(name)
        elif node_type == "identifier":
            # A bare value-position identifier is a use of its binding. Scope
            # resolution in _mark_import_used decides whether that binding is the
            # import or a closer param/local (bead code_hygiene_mcp-ukr). This is
            # what marks a genuine closure use -- `() => foo` -- of an import.
            self._mark_import_used(_text_str(node))
            return
        elif node_type == "shorthand_property_identifier":
            # `{ foo }` object-literal shorthand references the binding `foo`.
            self._mark_import_used(_text_str(node))
            return

        for child in node.children:
            self._visit_expression(child)

    def _process_call(self, node: Any) -> None:
        func = node.child_by_field_name("function")
        if func is None:
            return
        callee = _expression_name(func)
        # No AST CALLS edge is emitted (bead code_hygiene_mcp-0b9). Its target
        # would be either the raw callee string (dangling) or the local
        # CodeImport site (semantically wrong -- "calls the import declaration",
        # not the target symbol). This is a single-file extractor: it cannot
        # know the imported symbol's node id in another file. Resolved CALLS are
        # supplied by the SCIP translator when scip-typescript runs; in the
        # Tree-sitter-only fallback the call graph is degraded (a
        # scip_typescript diagnostic already flags this) rather than populated
        # with misleading edges. We still walk callees to mark imports used.
        if callee:
            self._mark_import_used(callee)

    def _process_new_expression(self, node: Any) -> None:
        # new Foo(): no AST CALLS edge (see _process_call, bead 0b9), but Foo's
        # import is still marked used for unused-import detection.
        callee = None
        for child in node.children:
            if child.type == "new":
                continue
            if child.type in ("identifier", "type_identifier", "member_expression"):
                callee = _expression_name(child)
                break
        if callee:
            self._mark_import_used(callee)

    def _mark_import_used(self, name: str, *, check_scope: bool = True) -> None:
        """Mark an import as used based on a value-position name reference.

        Scope-aware (bead code_hygiene_mcp-ukr): a value-position reference marks
        the import used only when its root name resolves to that import rather
        than to a closer binding (a param/local of the same name). If any active
        value scope binds the root name, the reference is a use of the
        *shadowing* binding, not the import, so nothing is marked -- keeping a
        genuine closure use (``() => foo``) marking ``foo`` used while a shadowing
        param (``(foo) => foo``) leaves the import unused.

        ``check_scope=False`` is used for type-position references
        (``_mark_type_used``): TS has separate type and value namespaces, so a
        value binding named ``Foo`` must not shadow a type use of imported
        ``Foo``. Type-namespace bindings (type params, local type/interface
        decls) are not tracked, so type references never consult the scope stack.
        """
        # The root of a member-expression name ("ns.thing" -> "ns") is what a
        # binding could shadow; the prefix match against imports is preserved.
        root = name.split(".", 1)[0]
        if check_scope and self._is_locally_bound(root):
            return
        for imp in self._imports:
            if imp["name"] == name or name.startswith(imp["name"] + "."):
                imp["used"] = True
                break

    # ------------------------------------------------------------------
    # Lexical scope tracking (value namespace) -- bead code_hygiene_mcp-ukr
    # ------------------------------------------------------------------
    #
    # The scope stack holds one frame per active lexical scope, each with only
    # that scope's own value bindings. Frames are pushed/popped as the walker
    # enters/leaves a scope (function bodies, blocks, catch/for/switch bodies),
    # so a binding shadows an import only for the lifetime of its actual scope --
    # a reference in an enclosing block still resolves to the import.

    def _is_locally_bound(self, name: str) -> bool:
        """Return True if *name* is bound by any active (non-import) scope."""
        return any(name in frame for frame in self._scope_stack)

    def _push_scope(self, names: set[str]) -> None:
        self._scope_stack.append(names)

    def _pop_scope(self) -> None:
        self._scope_stack.pop()

    def _function_scope_bindings(self, node: Any) -> set[str]:
        """Collect a function/arrow's parameter names plus hoisted ``var`` names.

        Parameters (including destructuring) are function-scoped, and ``var``
        declarations hoist to the nearest enclosing function scope -- so both
        belong in the function frame, shadowing an import across the whole body
        (codex review finding). ``let``/``const``/``class`` and block-scoped
        function declarations are collected per-block instead (_block_bindings).
        """
        names: set[str] = set()
        formal_params = _child_by_type(node, "formal_parameters")
        if formal_params is not None:
            for param in formal_params.children:
                if param.type in ("required_parameter", "optional_parameter", "rest_parameter"):
                    pattern = _child_by_types(
                        param,
                        ["identifier", "object_pattern", "array_pattern", "rest_pattern"],
                    )
                    _collect_pattern_names(pattern, names)
        # A single-identifier arrow param (`foo => ...`) is not wrapped in
        # formal_parameters; it sits under the `parameter` field (distinct from
        # the `body` field, so a bare-identifier body like `() => foo` is not
        # mistaken for a param).
        if node.type == "arrow_function":
            direct = node.child_by_field_name("parameter")
            if direct is not None:
                _collect_pattern_names(direct, names)
        body = node.child_by_field_name("body")
        if body is not None and body.type == "statement_block":
            self._collect_hoisted_vars(body, names)
        return names

    def _collect_hoisted_vars(self, node: Any, out: set[str]) -> None:
        """Collect ``var`` declarator names anywhere in a body, descending
        through nested blocks/statements but NOT into nested function/class
        scopes (which start their own function frame)."""
        for child in node.children:
            ctype = child.type
            if ctype == "variable_declaration":  # `var` (let/const are lexical_declaration)
                for declarator in _children_by_type(child, "variable_declarator"):
                    pattern = _child_by_types(
                        declarator, ["identifier", "object_pattern", "array_pattern"]
                    )
                    _collect_pattern_names(pattern, out)
            elif ctype in (
                "function_declaration",
                "generator_function_declaration",
                "arrow_function",
                "function_expression",
                "class_declaration",
                "class",
                "method_definition",
            ):
                continue  # nested function/class owns its own var scope
            else:
                self._collect_hoisted_vars(child, out)

    def _block_bindings(self, node: Any) -> set[str]:
        """Collect the block-scoped value bindings declared *directly* in a block.

        Covers the block's own ``let``/``const`` declarators, ``class`` and
        (block-scoped) function declaration names. Excludes ``var``, which hoists
        to the function frame (_function_scope_bindings), and nested blocks, which
        get their own frame -- so a binding shadows an import only for its actual
        lexical extent.
        """
        names: set[str] = set()
        for child in node.children:
            ctype = child.type
            if ctype == "lexical_declaration":  # let / const
                for declarator in _children_by_type(child, "variable_declarator"):
                    pattern = _child_by_types(
                        declarator, ["identifier", "object_pattern", "array_pattern"]
                    )
                    _collect_pattern_names(pattern, names)
            elif ctype in (
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
            ):
                name_node = _child_by_type(child, "identifier") or _child_by_type(
                    child, "type_identifier"
                )
                if name_node is not None:
                    names.add(_text_str(name_node))
        return names

    def _switch_body_bindings(self, node: Any) -> set[str]:
        """Collect value bindings declared directly in any of a switch's cases."""
        names: set[str] = set()
        for case in node.children:
            if case.type in ("switch_case", "switch_default"):
                names |= self._block_bindings(case)
        return names

    def _visit_loop(self, node: Any) -> None:
        """Walk a for / for-in / for-of, scoping its loop binding to the loop."""
        names: set[str] = set()
        if node.type == "for_in_statement":
            _collect_pattern_names(node.child_by_field_name("left"), names)
        else:  # for_statement
            initializer = node.child_by_field_name("initializer")
            if initializer is not None and initializer.type in (
                "lexical_declaration",
                "variable_declaration",
            ):
                for declarator in _children_by_type(initializer, "variable_declarator"):
                    pattern = _child_by_types(
                        declarator, ["identifier", "object_pattern", "array_pattern"]
                    )
                    _collect_pattern_names(pattern, names)
        self._push_scope(names)
        for child in node.children:
            self._visit_expression(child)
        self._pop_scope()

    def _visit_catch_clause(self, node: Any) -> None:
        """Walk a catch clause, scoping its parameter to the clause."""
        names: set[str] = set()
        param = node.child_by_field_name("parameter")
        _collect_pattern_names(param, names)
        self._push_scope(names)
        for child in node.children:
            self._visit_expression(child)
        self._pop_scope()

    def _visit_anonymous_class(self, node: Any) -> None:
        """Walk an anonymous `class {}` expression for import usage, node-free.

        Emits no node (unlike _process_class). Child methods are dispatched as
        method_definition -> _visit_anonymous_function (opening their parameter
        scope); field initializers and heritage clauses recurse generically so
        their value/type references still mark imports used.

        A NAMED class expression (`class foo {}`) binds its own name within the
        class body (self-reference); that name shadows an import of the same name
        (codex review), so it is pushed as a scope frame around the body walk.
        """
        names: set[str] = set()
        name_node = _child_by_type(node, "type_identifier") or _child_by_type(node, "identifier")
        if name_node is not None:
            names.add(_text_str(name_node))
        self._push_scope(names)
        for child in node.children:
            self._visit_expression(child)
        self._pop_scope()

    def _visit_anonymous_function(self, node: Any) -> None:
        """Walk an anonymous function expression under its own scope frame.

        Emits no CodeFunction node (unlike _process_function) -- anonymous
        function expressions were never node-bearing, and this walk exists only
        to scope import-usage. The body's statement_block pushes its own
        _block_bindings frame via _visit_expression.
        """
        names = self._function_scope_bindings(node)
        # A NAMED function expression (`function foo() {}`) binds its own name
        # within its body (self-reference); that name shadows an import of the
        # same name (codex review).
        name_node = _child_by_type(node, "identifier")
        if name_node is not None:
            names.add(_text_str(name_node))
        self._push_scope(names)
        body = node.child_by_field_name("body") or _child_by_types(
            node, ["statement_block", "expression"]
        )
        if body is not None:
            self._visit_expression(body)
        self._pop_scope()

    # Nodes that open a new binding scope: an import name referenced *inside* one
    # may actually be a shadowing parameter/local, so the export walker must not
    # mark identifiers within them (that would suppress a real unused-import
    # warning). Real-world `export default` is simple (a call, a bare identifier,
    # a router/config object) — this walker deliberately covers those and stops
    # at scope boundaries rather than reimplementing full scope resolution.
    _SCOPE_BOUNDARY_NODES = frozenset(
        {
            "function_declaration",
            "generator_function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
            "class_declaration",
            "class_body",
        }
    )

    def _mark_export_value_identifiers(self, node: Any) -> None:
        """Mark value-position identifiers in an export-default expression.

        A default export names its value through simple wrapping syntax
        (``config``, ``(config)``, ``config as C``, ``{ config }``, ``foo.bar``),
        and _visit_expression only marks call/new callees -- so bare identifier
        references would leave the import looking unused (bead
        code_hygiene_mcp-bhe). Walk the expression marking identifier / member
        roots and object-literal shorthands. Two deliberate exclusions keep this
        a value-position check without a full resolver: object-literal *keys*
        (property_identifier) are not references, and subtrees that open a new
        binding scope (functions/classes) are skipped since an identifier there
        may be a shadowing parameter/local, not the import.

        KNOWN LIMITATION (bead code_hygiene_mcp-ukr): skipping function bodies
        means a genuine closure use (``export default () => foo``) is not marked,
        so ``foo`` could be reported unused. This matches the extractor's
        existing scope-flat behaviour everywhere else and does not occur in
        practice (real ``export default`` is a call, an identifier, or a config
        object -- never a closure). It is the preferred error: descending would
        mark a *shadowing* parameter as an import use and suppress a real
        unused-import warning, which is worse.
        """
        if node is None or node.type in self._SCOPE_BOUNDARY_NODES:
            return
        # A `{ config }` object-literal shorthand references the binding `config`.
        if node.type == "shorthand_property_identifier":
            self._mark_import_used(_text_str(node))
            return
        # `foo.bar` is a member_expression -> resolve to its root name. Computed
        # access `obj[key]` is a distinct `subscript_expression` node (not a
        # member_expression), so it falls through to the generic child walk and
        # `key` gets visited as an identifier -- no special-casing needed.
        if node.type == "member_expression":
            name = _expression_name(node)
            if name:
                self._mark_import_used(name)
            return
        if node.type == "identifier":
            self._mark_import_used(_text_str(node))
            return
        for child in node.children:
            # In a `pair` (k: v) skip the key; only the value can reference an
            # import. Everything else recurses normally.
            if node.type == "pair" and child.type == "property_identifier":
                continue
            self._mark_export_value_identifiers(child)

    def _mark_type_used(self, type_text: str) -> None:
        """Mark imports used from a type-position reference (bead df7).

        A rendered type such as ``Array<Widget>`` or ``Config["x"]`` may embed
        several imported names, so every identifier token is checked -- unlike
        value positions, which reference a single callee/constructor name.

        Type references bypass value-scope resolution (``check_scope=False``): TS
        keeps type and value namespaces separate, so a value binding must not
        shadow a type-position use of an imported type (bead code_hygiene_mcp-ukr).
        """
        for token in _TYPE_IDENT_RE.findall(type_text):
            self._mark_import_used(token, check_scope=False)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _detect_unused_imports(self) -> None:
        """Create CodeDiagnostic nodes for unused imports."""
        for imp in self._imports:
            if not imp["used"]:
                diag_id = f"{imp['node_id']}:diagnostic:unused"
                self._add_node(
                    diag_id,
                    ["CodeDiagnostic"],
                    {
                        "level": "warning",
                        "rule": "unused_import",
                        "message": f"Import '{imp['name']}' is unused",
                        "name": imp["name"],
                    },
                )
                self._add_edge(imp["node_id"], diag_id, "HAS_DIAGNOSTIC")

    # ------------------------------------------------------------------
    # Complexity
    # ------------------------------------------------------------------

    def _compute_complexity(self, node: Any) -> int:
        """Compute a simple cyclomatic complexity count."""
        count = 1
        branch_types = {
            "if_statement",
            "for_statement",
            "for_in_statement",
            "while_statement",
            "do_statement",
            "catch_clause",
            "conditional_expression",
            "binary_expression",
            "logical_expression",
            "switch_case",
        }

        def walk(n: Any) -> None:
            nonlocal count
            if n.type in branch_types:
                count += 1
            for child in n.children:
                walk(child)

        walk(node)
        return count


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def _module_name_from_path(file_path: Path, repo_root: Path) -> str:
    """Derive a dotted module name from a file path relative to repo root."""
    try:
        rel = file_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel = file_path
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _qualname(module_name: str, class_name: str | None = None, func_name: str | None = None) -> str:
    """Build a dot-separated qualified name."""
    parts = [module_name]
    if class_name:
        parts.append(class_name)
    if func_name:
        parts.append(func_name)
    return ".".join(parts)


# ----------------------------------------------------------------------
# Tree-sitter node helpers
# ----------------------------------------------------------------------


def _child_by_type(node: Any, node_type: str) -> Any | None:
    for child in node.children:
        if child.type == node_type:
            return child
    return None


def _child_by_types(node: Any, node_types: list[str]) -> Any | None:
    for child in node.children:
        if child.type in node_types:
            return child
    return None


def _children_by_type(node: Any, node_type: str) -> list[Any]:
    return [child for child in node.children if child.type == node_type]


def _collect_pattern_names(node: Any, out: set[str]) -> None:
    """Collect value-namespace binding names from a binding pattern.

    Handles plain identifiers and destructuring patterns (object/array),
    including rest elements, defaults, and shorthand -- e.g. ``{ foo }``,
    ``{ a: b }`` (binds ``b``, not ``a``), ``[x, ...rest]``, ``foo = 1``. Used to
    populate scope frames for import-shadowing detection (bead
    code_hygiene_mcp-ukr). Property *keys* in an object pattern are not
    bindings; only the value side (the bound name) is collected.
    """
    if node is None:
        return
    node_type = node.type
    if node_type in ("identifier", "shorthand_property_identifier_pattern"):
        out.add(_text_str(node))
        return
    if node_type == "pair_pattern":
        # `{ key: value }` binds `value`; the key is a property name, not a
        # binding. Recurse only into the value side.
        value = node.child_by_field_name("value")
        _collect_pattern_names(value if value is not None else node, out)
        return
    if node_type in ("object_pattern", "array_pattern", "rest_pattern", "assignment_pattern"):
        for child in node.children:
            _collect_pattern_names(child, out)
        return
    # Generic descent for wrappers we don't special-case (keeps rare tree shapes
    # from silently dropping a binding).
    for child in node.children:
        if child.type in (
            "identifier",
            "shorthand_property_identifier_pattern",
            "object_pattern",
            "array_pattern",
            "rest_pattern",
            "assignment_pattern",
            "pair_pattern",
        ):
            _collect_pattern_names(child, out)


def _string_value(node: Any) -> str:
    """Extract the string value from a string literal node."""
    if node is None:
        return ""
    text = _text_str(node)
    if len(text) >= 2 and text[0] in ("'", '"', "`") and text[-1] == text[0]:
        return text[1:-1]
    return text


def _expression_name(node: Any) -> str | None:
    """Best-effort extraction of a callee / expression name."""
    if node is None:
        return None
    node_type = node.type
    if node_type in ("identifier", "property_identifier", "type_identifier"):
        return _text_str(node)
    if node_type == "member_expression":
        obj = _child_by_types(node, ["identifier", "member_expression"])
        prop = _child_by_type(node, "property_identifier")
        obj_name = _expression_name(obj) if obj else None
        prop_name = _text_str(prop) if prop else None
        if obj_name and prop_name:
            return f"{obj_name}.{prop_name}"
        return prop_name
    if node_type == "parenthesized_expression":
        inner = node.child_by_field_name("expression") or (
            node.children[1] if len(node.children) >= 2 else None
        )
        return _expression_name(inner)
    return None


def _collect_type_identifiers(node: Any) -> list[str]:
    """Collect all identifier/type_identifier names from a type node."""
    names: list[str] = []

    def walk(n: Any) -> None:
        if n.type in ("type_identifier", "identifier"):
            names.append(_text_str(n))
        for child in n.children:
            walk(child)

    walk(node)
    return names


def _type_annotation_name(type_annotation: Any) -> str | None:
    """Return a textual representation of a type annotation."""
    if type_annotation is None:
        return None
    # Skip the leading ':'
    type_node = None
    for child in type_annotation.children:
        if child.type != ":":
            type_node = child
            break
    if type_node is None:
        return None
    return _text_str(type_node)


def _extract_parameter_info(param: Any) -> tuple[str, str | None]:
    """Extract (name, type_name) from a formal parameter."""
    name_node = _child_by_type(param, "identifier")
    if name_node is None:
        name_node = _child_by_type(param, "property_identifier")
    name = _text_str(name_node) if name_node else ""
    type_node = _child_by_type(param, "type_annotation")
    type_name = _type_annotation_name(type_node) if type_node else None
    return name, type_name


def _collect_decorators(node: Any) -> list[str]:
    """Collect decorator names from a class/function/method node."""
    decorators: list[str] = []
    decs_node = _child_by_type(node, "decorators")
    if decs_node is None:
        return decorators
    for dec in _children_by_type(decs_node, "decorator"):
        call = _child_by_type(dec, "call_expression")
        if call is not None:
            func = _child_by_type(call, "function")
            name = _expression_name(func) if func else None
            if name:
                decorators.append(name)
        else:
            expr = dec.child_by_field_name("expression") or (
                dec.children[-1] if dec.children else None
            )
            name = _expression_name(expr)
            if name:
                decorators.append(name)
    return decorators


def _require_source(declarator: Any) -> str | None:
    """Return the module string if a declarator is `name = require("...")`."""
    value = _child_by_type(declarator, "call_expression")
    if value is None:
        return None
    func = value.child_by_field_name("function")
    if func is None or func.type != "identifier":
        return None
    if _text_str(func) != "require":
        return None
    args = value.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.children:
        if arg.type == "string":
            return _string_value(arg)
    return None
