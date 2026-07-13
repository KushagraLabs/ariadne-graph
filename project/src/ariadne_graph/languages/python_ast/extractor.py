"""Python AST fact extractor.

Traverses Python source code using the standard library ``ast`` module and
extracts a code knowledge graph containing modules, classes, functions,
methods, imports, inheritance, call relationships, and diagnostics.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

from ariadne_graph.core.diagnostics import DiagnosticsCollector
from ariadne_graph.core.models import CodeEdge, CodeGraphDelta, CodeNode
from ariadne_graph.languages.base import UniqueIdMixin

FASTAPI_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "trace"}
)


def _get_ast_node_source(
    source_lines: list[str], node: ast.AST
) -> str:
    """Extract the source text for an AST node."""
    try:
        end_lineno = cast(int | None, getattr(node, "end_lineno", None))
        lineno = cast(int | None, getattr(node, "lineno", None))
        col_offset = cast(int | None, getattr(node, "col_offset", None))
        if end_lineno is not None and lineno is not None:
            # Python 3.8+ has end_lineno
            lines = source_lines[lineno - 1 : end_lineno]
            if not lines:
                return ""
            # Handle column offsets for the first line
            if col_offset:
                lines[0] = lines[0][col_offset:]
            return "\n".join(lines)
        if lineno is not None:
            return source_lines[lineno - 1]
        return ""
    except (IndexError, AttributeError):
        return ""


def _param_names(args: ast.arguments, *, drop_receiver: bool = False) -> list[str]:
    """Ordered parameter names of a function (positional, *args, keyword-only,
    **kwargs). When ``drop_receiver`` is True (a real instance/class method — NOT
    a staticmethod or module-level function), the leading ``self``/``cls`` receiver
    is dropped; otherwise every parameter is kept, so a module-level
    ``def convert(cls, value)`` or a ``@staticmethod def convert(self, value)``
    keeps its real first argument. Used for embedding text (bead 42i)."""
    names: list[str] = []
    names.extend(a.arg for a in getattr(args, "posonlyargs", []))
    names.extend(a.arg for a in args.args)
    if args.vararg:
        names.append(args.vararg.arg)
    names.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        names.append(args.kwarg.arg)
    if drop_receiver and names and names[0] in ("self", "cls"):
        names = names[1:]
    return names


def _function_signature(name: str, args: ast.arguments, is_async: bool) -> str:
    """A compact one-line signature string for embedding text (bead 42i).

    ``ast.unparse`` renders the argument list including annotations/defaults; we
    prefix def/async def and the function name. Best-effort — a render failure
    falls back to just the name so extraction never breaks on an odd node."""
    prefix = "async def" if is_async else "def"
    try:
        rendered = ast.unparse(args)
    except Exception:
        return f"{prefix} {name}(...)"
    return f"{prefix} {name}({rendered})"


def _module_name_from_path(file_path: Path, repo_root: Path) -> str:
    """Derive a dotted module name from a file path relative to repo root.

    Examples:
        repo_root/src/mypackage/module.py -> mypackage.module
        repo_root/module.py -> module
    """
    try:
        rel = file_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel = file_path
    parts = list(rel.with_suffix("").parts)
    # Strip common source directory prefixes
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _node_qualname(
    module_name: str, class_name: str | None = None, func_name: str | None = None
) -> str:
    """Build a dot-separated qualified name."""
    parts = [module_name]
    if class_name:
        parts.append(class_name)
    if func_name:
        parts.append(func_name)
    return ".".join(parts)


def _name_from_expr(expr: ast.expr) -> str | None:
    """Extract a dotted name from an AST expression node."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        val = _name_from_expr(expr.value)
        if val:
            return f"{val}.{expr.attr}"
        return expr.attr
    return None


class PythonFactExtractor(UniqueIdMixin, ast.NodeVisitor):
    """AST visitor that extracts a code knowledge graph from Python source.

    Usage::

        extractor = PythonFactExtractor(source_code, file_path, repo_root, graph_id)
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

        self.source_lines = source.splitlines()
        self.module_name = _module_name_from_path(file_path, repo_root)

        # Accumulators
        self.nodes: list[CodeNode] = []
        self.edges: list[CodeEdge] = []

        # Diagnostics collected during extraction
        self._diagnostics = DiagnosticsCollector()

        # State tracking — class stack for nested classes
        self._class_stack: list[str] = []
        self._class_id_stack: list[str] = []
        self._current_function: str | None = None
        self._imports: list[dict[str, Any]] = []
        self._import_nodes: list[CodeNode] = []
        self._all_exports: list[str] = []

    @property
    def _current_class(self) -> str | None:
        """Return dot-separated qualified class name, e.g. 'Outer.Inner'."""
        return ".".join(self._class_stack) if self._class_stack else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self) -> CodeGraphDelta:
        """Parse the source and return the extracted graph delta.

        On parse errors returns an empty delta with the error message stored
        in *parser_version*.
        """
        try:
            tree = ast.parse(self.source, filename=str(self.file_path))
        except SyntaxError as exc:
            return CodeGraphDelta(
                graph_id=self.graph_id,
                file_path=str(self.file_path),
                nodes=[],
                edges=[],
                content_hash="",
                parser_version=f"error:syntax:{exc.msg}@{exc.lineno}",
            )

        # Module-level pass: collect exports first
        self._collect_exports(tree)

        # Visit the AST
        self.visit(tree)

        # Post-processing diagnostics
        self._emit_diagnostics()

        return CodeGraphDelta(
            graph_id=self.graph_id,
            file_path=str(self.file_path),
            nodes=self.nodes,
            edges=self.edges,
            content_hash="",
            parser_version=self.parser_version,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_exports(self, tree: ast.AST) -> None:
        """Collect __all__ exports for later edge creation."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__all__"
                    and isinstance(node.value, (ast.List, ast.Tuple))
                ):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(
                            elt.value, str
                        ):
                            self._all_exports.append(elt.value)

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
        edge = CodeEdge(
            source=source,
            target=target,
            graph_id=self.graph_id,
            rel_type=rel_type,
            properties=dict(properties or {}),
        )
        self.edges.append(edge)
        return edge

    def _line_range(self, node: ast.AST) -> dict[str, int]:
        """Return line_start / line_end dict for an AST node."""
        result: dict[str, int] = {}
        lineno = getattr(node, "lineno", None)
        if lineno is not None:
            result["line_start"] = lineno
        end_lineno = getattr(node, "end_lineno", None)
        if end_lineno is not None:
            result["line_end"] = end_lineno
        return result

    def _make_snippet(self, node: ast.AST) -> str:
        return _get_ast_node_source(self.source_lines, node)

    def _parent_module_id(self) -> str:
        return self.module_name

    def _current_class_id(self) -> str | None:
        if self._class_id_stack:
            return self._class_id_stack[-1]
        return None

    # ------------------------------------------------------------------
    # Top-level nodes
    # ------------------------------------------------------------------

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802
        # CodeFile node
        file_id = str(self.file_path.resolve())
        self._add_node(
            file_id,
            ["CodeFile"],
            {
                "name": self.file_path.name,
                "file_path": str(self.file_path),
                "module": self.module_name,
                **self._line_range(node),
            },
        )

        # CodeModule node
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

        # File CONTAINS Module
        self._add_edge(file_id, mod_id, "CONTAINS")

        # Continue into module body
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            name = alias.name
            asname = alias.asname or name
            base_id = f"{self.module_name}:import:{name}"
            node_id = self._unique_node_id(base_id, node)
            snippet = self._make_snippet(node)
            line_info = self._line_range(node)

            import_node = self._add_node(
                node_id,
                ["CodeImport"],
                {
                    "name": name,
                    "asname": asname,
                    "import_type": "module",
                    "snippet": snippet,
                    **line_info,
                },
            )
            self._import_nodes.append(import_node)
            self._add_edge(
                self._parent_module_id(),
                node_id,
                "IMPORTS",
                {"import_name": name},
            )
            self._imports.append(
                {
                    "name": name,
                    "asname": asname,
                    "node_id": node_id,
                    "used": False,
                }
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        level = node.level or 0

        for alias in node.names:
            name = alias.name
            asname = alias.asname or name

            # Build the full imported path
            if level > 0:
                import_path = f".{module}" if module else "."
            else:
                import_path = f"{module}.{name}" if module else name

            base_id = f"{self.module_name}:import:{import_path}:{name}"
            node_id = self._unique_node_id(base_id, node)
            snippet = self._make_snippet(node)
            line_info = self._line_range(node)

            import_node = self._add_node(
                node_id,
                ["CodeImport"],
                {
                    "name": name,
                    "asname": asname,
                    "import_type": "symbol",
                    "module": module,
                    "level": level,
                    "snippet": snippet,
                    **line_info,
                },
            )
            self._import_nodes.append(import_node)
            self._add_edge(
                self._parent_module_id(),
                node_id,
                "IMPORTS_SYMBOL",
                {
                    "symbol": name,
                    "asname": asname,
                    "from_module": module,
                    "level": level,
                },
            )
            self._imports.append(
                {
                    "name": asname,
                    "full_name": import_path,
                    "node_id": node_id,
                    "used": False,
                }
            )

    # ------------------------------------------------------------------
    # Class definitions
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        class_name = node.name
        self._class_stack.append(class_name)

        base_qualname = _node_qualname(self.module_name, class_name=self._current_class)
        qualname = self._unique_node_id(base_qualname, node)
        self._class_id_stack.append(qualname)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        # Build properties
        props: dict[str, Any] = {
            "name": class_name,
            "qualname": base_qualname,
            "snippet": snippet,
            **line_info,
        }
        # Class docstring as behavioural text for embeddings (bead 42i): a
        # differently-named class with a descriptive docstring should surface as an
        # equivalent (classes are in the default find_equivalent type set).
        class_doc = ast.get_docstring(node)
        if class_doc:
            props["docstring"] = class_doc

        # Detect decorators
        is_dataclass = False
        is_enum = False
        is_pydantic = False
        decorator_names: list[str] = []

        for dec in node.decorator_list:
            dec_name = _name_from_expr(dec)
            if dec_name:
                decorator_names.append(dec_name)
                # dataclass detection
                if dec_name in ("dataclass", "dataclasses.dataclass"):
                    is_dataclass = True
                # Decorated_BY edge
                self._add_edge(
                    qualname,
                    dec_name,
                    "DECORATED_BY",
                    {"decorator": dec_name},
                )

        # Detect enum / pydantic from bases
        base_names: list[str] = []
        for base in node.bases:
            base_name = _name_from_expr(base)
            if base_name:
                base_names.append(base_name)
                # INHERITS edge
                self._add_edge(
                    qualname,
                    base_name,
                    "INHERITS",
                    {"base": base_name},
                )
                if base_name in ("Enum", "enum.Enum", "IntEnum", "enum.IntEnum"):
                    is_enum = True
                if base_name in ("BaseModel", "pydantic.BaseModel"):
                    is_pydantic = True

        props["bases"] = base_names
        props["decorators"] = decorator_names
        if is_dataclass:
            props["is_dataclass"] = True
        if is_enum:
            props["is_enum"] = True
        if is_pydantic:
            props["is_pydantic_model"] = True

        self._add_node(qualname, ["CodeClass"], props)

        # Module CONTAINS / DEFINES Class
        self._add_edge(self._parent_module_id(), qualname, "CONTAINS")
        self._add_edge(self._parent_module_id(), qualname, "DEFINES")

        # Visit class body (methods, attributes, nested classes)
        for item in node.body:
            self.visit(item)

        # Check for __all__ exports on class
        if class_name in self._all_exports:
            self._add_edge(
                self._parent_module_id(),
                qualname,
                "EXPORTS",
                {"symbol": class_name},
            )

        self._class_id_stack.pop()
        self._class_stack.pop()

    # ------------------------------------------------------------------
    # Function / AsyncFunction definitions
    # ------------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._process_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._process_function(node)

    def _process_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        func_name = node.name
        is_async = isinstance(node, ast.AsyncFunctionDef)
        is_method = self._current_class is not None

        if is_method and self._current_class:
            base_qualname = _node_qualname(
                self.module_name, class_name=self._current_class, func_name=func_name
            )
            node_labels = ["CodeMethod"]
        else:
            base_qualname = _node_qualname(self.module_name, func_name=func_name)
            node_labels = ["CodeFunction"]

        node_id = self._unique_node_id(base_qualname, node)
        snippet = self._make_snippet(node)
        line_info = self._line_range(node)

        props: dict[str, Any] = {
            "name": func_name,
            "qualname": base_qualname,
            "snippet": snippet,
            "is_async": is_async,
            **line_info,
        }

        # Behavioural text for embeddings (bead 42i): the signature, parameter
        # names, and full docstring let semantic search catch differently-named
        # duplicates. Cheap to derive here from the AST node we already have; the
        # embedding text builder (core/embeddings._build_node_text) reads these.
        # A leading self/cls is a receiver ONLY on a real instance/class method
        # (inside a class and not @staticmethod) — drop it just there, so a
        # module-level or static function keeps its real first argument.
        decorated_static = any(
            _name_from_expr(d.func if isinstance(d, ast.Call) else d) == "staticmethod"
            for d in node.decorator_list
        )
        drop_receiver = is_method and not decorated_static
        param_names = _param_names(node.args, drop_receiver=drop_receiver)
        props["parameters"] = param_names
        props["signature"] = _function_signature(func_name, node.args, is_async)
        docstring = ast.get_docstring(node)
        if docstring:
            props["docstring"] = docstring

        # Detect decorators
        decorator_names: list[str] = []
        is_property = False
        is_staticmethod = False
        is_classmethod = False
        route_path: str | None = None
        route_method: str | None = None

        for dec in node.decorator_list:
            dec_name = _name_from_expr(dec)
            # Handle @app.get("/path") — the decorator is a Call node
            if dec_name is None and isinstance(dec, ast.Call):
                dec_name = _name_from_expr(dec.func)

            if dec_name:
                decorator_names.append(dec_name)

                if dec_name == "property":
                    is_property = True
                elif dec_name == "staticmethod":
                    is_staticmethod = True
                elif dec_name == "classmethod":
                    is_classmethod = True

                # FastAPI route detection — e.g. "app.get", "router.post"
                parts = dec_name.split(".")
                if len(parts) == 2 and parts[1] in FASTAPI_METHODS:
                    route_method = parts[1].upper()
                    # Try to extract path from decorator call
                    route_path = self._extract_route_path(dec)

                # DECORATED_BY edge
                self._add_edge(
                    node_id,
                    dec_name,
                    "DECORATED_BY",
                    {"decorator": dec_name},
                )

            # FastAPI route with explicit path argument
            if route_path and route_method:
                props["route_path"] = route_path
                props["route_method"] = route_method

        props["decorators"] = decorator_names
        if is_property:
            props["is_property"] = True
        if is_staticmethod:
            props["is_staticmethod"] = True
        if is_classmethod:
            props["is_classmethod"] = True

        # Type annotations
        arg_types: list[str] = []
        for arg in node.args.args + node.args.kwonlyargs:
            if arg.annotation:
                type_name = _name_from_expr(arg.annotation)
                if type_name:
                    arg_types.append(type_name)
                    self._add_edge(
                        node_id,
                        type_name,
                        "USES_TYPE",
                        {"parameter": arg.arg, "type": type_name},
                    )
        # *args and **kwargs
        if node.args.vararg and node.args.vararg.annotation:
            type_name = _name_from_expr(node.args.vararg.annotation)
            if type_name:
                arg_types.append(type_name)
                self._add_edge(node_id, type_name, "USES_TYPE", {"parameter": f"*{node.args.vararg.arg}"})
        if node.args.kwarg and node.args.kwarg.annotation:
            type_name = _name_from_expr(node.args.kwarg.annotation)
            if type_name:
                arg_types.append(type_name)
                self._add_edge(node_id, type_name, "USES_TYPE", {"parameter": f"**{node.args.kwarg.arg}"})

        if node.returns:
            return_type = _name_from_expr(node.returns)
            if return_type:
                props["return_type"] = return_type
                self._add_edge(
                    node_id,
                    return_type,
                    "RETURNS_TYPE",
                    {"return_type": return_type},
                )

        if arg_types:
            props["arg_types"] = arg_types

        # Cyclomatic complexity (simple count of branching nodes)
        complexity = self._compute_complexity(node)
        props["complexity"] = complexity

        # Create the function/method node
        self._add_node(node_id, node_labels, props)

        # Collect code-quality diagnostics for this function/method.
        self._collect_function_diagnostics(node_id, func_name, node)

        # Parent relationships
        class_id = self._current_class_id()
        if is_method and class_id is not None:
            self._add_edge(class_id, node_id, "CONTAINS")
            self._add_edge(self._parent_module_id(), node_id, "DEFINES")
        else:
            self._add_edge(self._parent_module_id(), node_id, "CONTAINS")
            self._add_edge(self._parent_module_id(), node_id, "DEFINES")

        # If FastAPI route, create CodeRoute node
        if route_path and route_method:
            route_id = f"{node_id}:route:{route_method}:{route_path}"
            self._add_node(
                route_id,
                ["CodeRoute"],
                {
                    "name": f"{route_method} {route_path}",
                    "handler": node_id,
                    "route_path": route_path,
                    "route_method": route_method,
                    "qualname": base_qualname,
                    **line_info,
                },
            )
            self._add_edge(node_id, route_id, "DEFINES")

        # Visit function body for call tracking
        prev_func = self._current_function
        self._current_function = node_id
        for stmt in node.body:
            self.visit(stmt)
        self._current_function = prev_func

        # Check for overrides if method
        if is_method and self._current_class_id():
            self._check_override(node_id, func_name, node)

    # ------------------------------------------------------------------
    # Assignments — module-level variables & class attributes
    # ------------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        self._process_assignment(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self._process_annotation_assignment(node)
        self.generic_visit(node)

    def _process_assignment(self, node: ast.Assign) -> None:
        for target in node.targets:
            names = self._names_from_target(target)
            for name in names:
                class_id = self._current_class_id()
                if class_id is not None and not self._current_function:
                    # Class attribute
                    base_attr_id = f"{class_id}.attr:{name}"
                    attr_id = self._unique_node_id(base_attr_id, node)
                    self._add_node(
                        attr_id,
                        ["CodeAttribute"],
                        {
                            "name": name,
                            "class": self._current_class,
                            **self._line_range(node),
                        },
                    )
                    self._add_edge(class_id, attr_id, "CONTAINS")
                elif not self._current_function:
                    # Module-level variable
                    base_var_id = f"{self.module_name}:var:{name}"
                    var_id = self._unique_node_id(base_var_id, node)
                    self._add_node(
                        var_id,
                        ["CodeVariable"],
                        {
                            "name": name,
                            "module": self.module_name,
                            **self._line_range(node),
                        },
                    )
                    self._add_edge(self._parent_module_id(), var_id, "CONTAINS")

    def _process_annotation_assignment(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            name = node.target.id
            type_name = _name_from_expr(node.annotation)

            class_id = self._current_class_id()
            if class_id is not None and not self._current_function:
                base_attr_id = f"{class_id}.attr:{name}"
                attr_id = self._unique_node_id(base_attr_id, node)
                props: dict[str, Any] = {
                    "name": name,
                    "class": self._current_class,
                    **self._line_range(node),
                }
                if type_name:
                    props["type_annotation"] = type_name
                self._add_node(attr_id, ["CodeAttribute"], props)
                self._add_edge(class_id, attr_id, "CONTAINS")
                if type_name:
                    self._add_edge(attr_id, type_name, "USES_TYPE", {"type": type_name})
            elif not self._current_function:
                base_var_id = f"{self.module_name}:var:{name}"
                var_id = self._unique_node_id(base_var_id, node)
                props = {
                    "name": name,
                    "module": self.module_name,
                    **self._line_range(node),
                }
                if type_name:
                    props["type_annotation"] = type_name
                self._add_node(var_id, ["CodeVariable"], props)
                self._add_edge(self._parent_module_id(), var_id, "CONTAINS")
                if type_name:
                    self._add_edge(var_id, type_name, "USES_TYPE", {"type": type_name})

    def _names_from_target(self, target: ast.expr) -> list[str]:
        """Extract variable names from an assignment target."""
        names: list[str] = []
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                names.extend(self._names_from_target(elt))
        return names

    # ------------------------------------------------------------------
    # Call tracking
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func_name = _name_from_expr(node.func)
        caller = self._current_function

        if func_name and caller:
            # Record the call-site position of the callee name so a SCIP
            # resolver can match this edge to a compiler-resolved occurrence
            # and rewrite the (fuzzy) bare-name target to a real node id.
            props: dict[str, Any] = {"callee": func_name}
            callee_pos = self._callee_position(node.func)
            if callee_pos is not None:
                props["callee_line"], props["callee_col"] = callee_pos
            self._add_edge(
                caller,
                func_name,
                "CALLS",
                props,
            )
            # Mark import as used
            self._mark_import_used(func_name)

        self.generic_visit(node)

    @staticmethod
    def _callee_position(func: ast.expr) -> tuple[int, int] | None:
        """Return the (1-based line, 0-based col) of the callee name token.

        For ``foo()`` this is the ``foo`` name; for ``a.b.foo()`` it is the
        trailing ``foo`` attribute. These coordinates align with the SCIP
        occurrence range of the reference, enabling positional matching.
        """
        target: ast.expr = func
        # For attribute access (a.b.foo), SCIP marks the trailing attribute.
        # ast does not expose the attribute token position directly, but the
        # end position of the whole expression bounds it; use the node's own
        # position for plain names and best-effort for attributes.
        if isinstance(target, ast.Attribute):
            # The attribute name ends at end_col_offset; its start is
            # end_col_offset - len(attr).
            end_line = getattr(target, "end_lineno", None)
            end_col = getattr(target, "end_col_offset", None)
            if end_line is not None and end_col is not None:
                return (end_line, end_col - len(target.attr))
            return None
        lineno = getattr(target, "lineno", None)
        col_offset = getattr(target, "col_offset", None)
        if lineno is not None and col_offset is not None:
            return (lineno, col_offset)
        return None

    def _mark_import_used(self, name: str) -> None:
        """Mark an import as used based on a name reference."""
        for imp in self._imports:
            if imp["name"] == name or name.startswith(imp["name"] + "."):
                imp["used"] = True
                break

    # ------------------------------------------------------------------
    # Name references — mark imports used
    # ------------------------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        for imp in self._imports:
            if imp["name"] == node.id:
                imp["used"] = True
                break
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _collect_function_diagnostics(
        self,
        node_id: str,
        func_name: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        """Run code-quality rules against a function/method."""
        # Aggregate missing type annotations into a single diagnostic per function.
        positional_and_kwonly = node.args.args + node.args.kwonlyargs
        missing_params = [
            arg.arg
            for arg in positional_and_kwonly
            if arg.annotation is None and arg.arg not in ("self", "cls")
        ]
        total_params = len(positional_and_kwonly)
        if node.returns is None or missing_params:
            self._diagnostics.add_missing_type_annotation(
                node_id,
                func_name,
                missing_return=node.returns is None,
                missing_parameters=missing_params,
                total_parameters=total_params,
            )

        # Long parameter list
        param_count = (
            len(node.args.args)
            + len(node.args.kwonlyargs)
            + (1 if node.args.vararg else 0)
            + (1 if node.args.kwarg else 0)
        )
        if param_count > DiagnosticsCollector.LONG_PARAMETER_LIST_COUNT:
            self._diagnostics.add_long_parameter_list(
                node_id,
                func_name,
                param_count,
                parameters=[a.arg for a in node.args.args + node.args.kwonlyargs],
            )

        # Complex function (by line count)
        line_start = getattr(node, "lineno", None)
        line_end = getattr(node, "end_lineno", None)
        if isinstance(line_start, int) and isinstance(line_end, int):
            line_count = line_end - line_start + 1
            if line_count > DiagnosticsCollector.COMPLEX_FUNCTION_LINES:
                self._diagnostics.add_complex_function(node_id, func_name, line_count)

    def _emit_diagnostics(self) -> None:
        """Emit collected diagnostics as CodeDiagnostic nodes and edges."""
        # Unused imports are tracked separately; add them to the collector first.
        for imp in self._imports:
            if not imp["used"]:
                self._diagnostics.add_unused_import(imp["node_id"], imp["name"])

        for diag in self._diagnostics.get_diagnostics():
            diag_id = f"{diag.node_id}:diagnostic:{diag.rule}"
            # Make IDs unique when multiple diagnostics share a node/rule.
            counter = 0
            base_diag_id = diag_id
            while diag_id in self._used_node_ids:
                counter += 1
                diag_id = f"{base_diag_id}:{counter}"

            self._add_node(
                diag_id,
                ["CodeDiagnostic"],
                {
                    "level": diag.level,
                    "rule": diag.rule,
                    "message": diag.message,
                    **diag.properties,
                },
            )
            self._add_edge(diag.node_id, diag_id, "HAS_DIAGNOSTIC")

    # ------------------------------------------------------------------
    # Override detection
    # ------------------------------------------------------------------

    def _check_override(
        self, method_id: str, method_name: str, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """Check if a method likely overrides a parent method.

        We add an OVERRIDES edge to a synthetic target; the caller can resolve
        the actual parent class later using the graph store.
        """
        # Get the parent class bases
        class_id = self._current_class_id()
        # Find base classes from existing edges
        base_classes: list[str] = []
        for edge in self.edges:
            if edge.source == class_id and edge.rel_type == "INHERITS":
                base_classes.append(edge.target)

        for base in base_classes:
            parent_method = f"{base}.{method_name}"
            self._add_edge(
                method_id,
                parent_method,
                "OVERRIDES",
                {"base_class": base, "method": method_name},
            )

    # ------------------------------------------------------------------
    # FastAPI helpers
    # ------------------------------------------------------------------

    def _extract_route_path(self, dec: ast.expr) -> str | None:
        """Extract the path string from a FastAPI route decorator call."""
        if isinstance(dec, ast.Call) and dec.args:
            first_arg = dec.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(
                first_arg.value, str
            ):
                return first_arg.value
        return "/"

    # ------------------------------------------------------------------
    # Complexity
    # ------------------------------------------------------------------

    def _compute_complexity(self, node: ast.AST) -> int:
        """Compute a simple cyclomatic complexity count.

        Counts branching nodes: If, For, While, ExceptHandler,
        With, Assert, comprehension, BoolOp, and lambda.
        """
        count = 1  # Base path
        for child in ast.walk(node):
            if isinstance(
                child,
                (
                    ast.If,
                    ast.For,
                    ast.While,
                    ast.ExceptHandler,
                    ast.With,
                    ast.Assert,
                    ast.BoolOp,
                    ast.Lambda,
                    ast.ListComp,
                    ast.SetComp,
                    ast.GeneratorExp,
                    ast.DictComp,
                ),
            ):
                count += 1
        return count
