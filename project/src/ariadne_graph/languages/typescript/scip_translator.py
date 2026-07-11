"""Translate a SCIP document into ``CodeNode``/``CodeEdge`` deltas."""

from __future__ import annotations

import logging
from pathlib import Path

from ariadne_graph.core.models import CodeEdge, CodeGraphDelta, CodeNode
from ariadne_graph.languages.typescript.scip_parser import (
    ScipDocument,
    ScipRelationship,
    ScipSymbolInfo,
    SymbolRole,
)

logger = logging.getLogger(__name__)


class ScipGraphTranslator:
    """Convert one :class:`ScipDocument` into a :class:`CodeGraphDelta`."""

    def __init__(self, repo_root: Path, graph_id: str, path_prefix: str = "") -> None:
        self.repo_root = repo_root.resolve()
        self.graph_id = graph_id
        # Repo-relative directory of the subproject this index was produced in
        # (e.g. "mobile"). SCIP doc paths are relative to the project cwd, so for
        # a subproject they must be rebased under this prefix to stay unique and
        # repo-root-relative. Empty for the repo-root project.
        self.path_prefix = path_prefix.strip("/")

    def _rel_path(self, document: ScipDocument) -> str:
        """Repo-root-relative path of *document*, applying the subproject prefix."""
        rel = str(document.relative_path)
        return f"{self.path_prefix}/{rel}" if self.path_prefix else rel

    @staticmethod
    def _node_id(symbol: str, file_path: str) -> str:
        """Map a SCIP *symbol* to its global graph node id.

        ``scip-typescript`` emits ``local N`` symbols that are unique only
        within their owning document. Using them verbatim as global ids makes
        same-named locals in different files collide (phantom cross-file
        edges), so we namespace them by *file_path*. Non-local (npm/global)
        symbols already carry their file path inside the symbol string and are
        returned unchanged.
        """
        if symbol.startswith("local "):
            return f"{file_path}::{symbol}"
        return symbol

    def translate(
        self,
        document: ScipDocument,
        call_ranges: set[tuple[int, int, int, int]] | None = None,
        enclosing_map: dict[tuple[int, int, int, int], str] | None = None,
    ) -> CodeGraphDelta:
        """Translate *document* into a graph delta.

        Args:
            document: The SCIP document to translate.
            call_ranges: Optional set of 0-based call-position ranges. A
                reference occurrence whose range lies inside one of these
                ranges is emitted as a ``CALLS`` edge; otherwise it becomes a
                ``REFERENCES`` edge.
            enclosing_map: Optional mapping from occurrence range to the ID of
                the enclosing definition. When provided, reference edges are
                emitted from that definition; otherwise they are emitted from
                the module node.

        Returns:
            A :class:`CodeGraphDelta` containing nodes and edges for the file.
        """
        call_ranges = call_ranges or set()
        enclosing_map = enclosing_map or {}
        file_path = self._rel_path(document)
        abs_path = str(self.repo_root / file_path)

        nodes: list[CodeNode] = []
        edges: list[CodeEdge] = []
        seen_node_ids: set[str] = set()

        def add_node(node: CodeNode) -> None:
            if node.id not in seen_node_ids:
                nodes.append(node)
                seen_node_ids.add(node.id)

        def add_edge(edge: CodeEdge) -> None:
            edges.append(edge)

        # Module/file node represents the document itself.
        module_symbol = self._module_symbol(document)
        module_id = module_symbol or file_path
        module_node = CodeNode(
            id=module_id,
            graph_id=self.graph_id,
            labels=["KnowledgeNode", "CodeModule", "CodeFile"],
            properties={
                "name": document.relative_path.stem,
                "qualname": file_path,
                "file_path": abs_path,
                "language": document.language or "typescript",
                "scip_symbol": module_symbol or "",
                "scip_kind": "File",
            },
        )
        add_node(module_node)

        # Definition nodes from SymbolInformation.
        for sym in document.symbols.values():
            node = self._symbol_to_node(sym, abs_path, file_path)
            add_node(node)
            # The file-module symbol is both the module node and a member; skip
            # the module->module CONTAINS/DEFINES self-loop (bead 0ef).
            if node.id == module_id:
                continue
            # CONTAINS edge from module to top-level symbols.
            add_edge(
                CodeEdge(
                    source=module_id,
                    target=node.id,
                    graph_id=self.graph_id,
                    rel_type="CONTAINS",
                    properties={"owner_file_path": abs_path},
                )
            )
            # DEFINES edge from module to the symbol.
            add_edge(
                CodeEdge(
                    source=module_id,
                    target=node.id,
                    graph_id=self.graph_id,
                    rel_type="DEFINES",
                    properties={"owner_file_path": abs_path},
                )
            )

        # Occurrences → edges and property updates.
        for occ in document.occurrences:
            if not occ.symbol:
                continue

            is_definition = bool(occ.symbol_roles & SymbolRole.DEFINITION)
            is_import = bool(occ.symbol_roles & SymbolRole.IMPORT)

            # Resolve the edge target node.
            #   * Third-party/npm-package symbols collapse to one lightweight
            #     per-package external-module node (never one node per library
            #     internal).
            #   * Otherwise the target is the symbol's own node; if it is not
            #     defined in this document a lightweight stub is emitted so the
            #     edge has a target. The stub deliberately carries no CodeFile/
            #     CodeModule label; the owning file's authoritative node is
            #     merged in via label union at persist time (see
            #     add_nodes_batch), which is also what keeps a cross-file
            #     import from stripping the owner's CodeFile label.
            defined_here = occ.symbol in document.symbols
            if not defined_here and self._is_external_package_symbol(occ.symbol):
                external_node = self._external_module_node(occ.symbol)
                target_id = external_node.id
                add_node(external_node)
            else:
                target_id = self._node_id(occ.symbol, file_path)
                if target_id not in seen_node_ids:
                    add_node(self._external_symbol_node(occ.symbol, file_path))

            if is_import:
                # Create a CodeImport node for this import site.
                import_id = f"{file_path}:import:{occ.start_line}:{occ.start_col}"
                import_props: dict[str, object] = {
                    "name": self._symbol_name(occ.symbol),
                    "scip_symbol": occ.symbol,
                    "file_path": abs_path,
                    "line_start": occ.start_line + 1,
                    "line_end": occ.end_line + 1,
                }
                edge_props: dict[str, object] = {
                    "owner_file_path": abs_path,
                    "import_site": import_id,
                }
                # Resolve a local (relative) import to its target file. The
                # imported SCIP symbol embeds the owning file path, so no raw
                # specifier string is needed; store the absolute target path
                # under ``resolved_source`` to match the Tree-sitter extractor.
                resolved = self._resolved_import_target(occ.symbol)
                if resolved is not None:
                    import_props["resolved_source"] = resolved
                    edge_props["resolved_source"] = resolved
                import_node = CodeNode(
                    id=import_id,
                    graph_id=self.graph_id,
                    labels=["KnowledgeNode", "CodeImport"],
                    properties=import_props,
                )
                add_node(import_node)
                add_edge(
                    CodeEdge(
                        source=module_id,
                        target=target_id,
                        graph_id=self.graph_id,
                        rel_type="IMPORTS_SYMBOL",
                        properties=edge_props,
                    )
                )
                continue

            if is_definition:
                # Update definition node location if we have a matching symbol.
                if occ.symbol in document.symbols:
                    for node in nodes:
                        if node.id == target_id:
                            node.properties["line_start"] = occ.start_line + 1
                            node.properties["line_end"] = occ.end_line + 1
                continue

            # Reference occurrence: determine if it is a call.
            occ_range = (occ.start_line, occ.start_col, occ.end_line, occ.end_col)
            source_id = enclosing_map.get(occ_range, module_id)
            rel_type = "CALLS" if occ_range in call_ranges else "REFERENCES"
            add_edge(
                CodeEdge(
                    source=source_id,
                    target=target_id,
                    graph_id=self.graph_id,
                    rel_type=rel_type,
                    properties={
                        "owner_file_path": abs_path,
                        "line_start": occ.start_line + 1,
                        "line_end": occ.end_line + 1,
                    },
                )
            )

        # Relationship edges from SymbolInformation.
        for sym in document.symbols.values():
            for rel in sym.relationships:
                edge = self._relationship_to_edge(sym, rel, file_path)
                if edge:
                    add_edge(edge)

        return CodeGraphDelta(
            graph_id=self.graph_id,
            file_path=file_path,
            nodes=nodes,
            edges=edges,
            content_hash="",
            parser_version="scip-typescript",
        )

    # ------------------------------------------------------------------
    # Symbol → node conversion
    # ------------------------------------------------------------------

    def _symbol_to_node(self, sym: ScipSymbolInfo, abs_path: str, file_path: str) -> CodeNode:
        labels = self._labels_for_symbol(sym)
        name = sym.display_name or self._symbol_name(sym.symbol)
        qualname = self._symbol_qualname(sym.symbol)
        line_start = 0
        line_end = 0
        # Try to find a definition occurrence for line numbers.
        # The translator does not keep document occurrences here; line numbers
        # are best-effort from the symbol metadata.
        return CodeNode(
            id=self._node_id(sym.symbol, file_path),
            graph_id=self.graph_id,
            labels=labels,
            properties={
                "name": name,
                "qualname": qualname,
                "scip_symbol": sym.symbol,
                "scip_kind": sym.kind_name,
                "file_path": abs_path,
                "line_start": line_start,
                "line_end": line_end,
                "documentation": "\n".join(sym.documentation),
                "signature": sym.signature_text,
                "is_external": self._is_external_symbol(sym.symbol),
            },
        )

    def _external_symbol_node(self, symbol: str, file_path: str) -> CodeNode:
        """Create a stub node for an externally referenced symbol."""
        return CodeNode(
            id=self._node_id(symbol, file_path),
            graph_id=self.graph_id,
            labels=["KnowledgeNode", "CodeImport"],
            properties={
                "name": self._symbol_name(symbol),
                "qualname": self._symbol_qualname(symbol),
                "scip_symbol": symbol,
                "is_external": True,
            },
        )

    def _external_module_node(self, symbol: str) -> CodeNode:
        """Collapse a third-party symbol to one lightweight per-package node.

        All internals of an npm library share a single ``CodeExternalModule``
        node keyed by ``<package>@<version>`` so we never inflate the graph with
        a first-class node per library symbol.
        """
        package = self._external_package(symbol)
        return CodeNode(
            id=f"external:{package}",
            graph_id=self.graph_id,
            labels=["KnowledgeNode", "CodeExternalModule"],
            properties={
                "name": package,
                "qualname": package,
                "is_external": True,
            },
        )

    def _resolved_import_target(self, symbol: str) -> str | None:
        """Absolute path of the repo file an imported *symbol* is defined in.

        Local/relative imports name a real repo source file inside the SCIP
        symbol; this resolves that to an absolute path (the ``resolved_source``
        the file-to-file dependency layer consumes). Returns ``None`` for
        third-party or unresolvable imports.
        """
        if symbol.startswith("local "):
            return None
        return self._resolve_symbol_file(symbol)

    def _resolve_symbol_file(self, symbol: str) -> str | None:
        """Absolute path of the real repo file a SCIP *symbol* is defined in.

        SCIP symbol paths are project-relative; for a subproject index they are
        rebased under the same prefix as the document path so mobile/.tsx
        symbols resolve to the real subproject file (bead c66). Returns ``None``
        when the symbol carries no file path or the file does not exist.
        """
        source_file = self._symbol_source_file(symbol)
        if source_file is None:
            return None
        candidates = [self.repo_root / source_file]
        if self.path_prefix:
            candidates.insert(0, self.repo_root / self.path_prefix / source_file)
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    def _is_external_package_symbol(self, symbol: str) -> bool:
        """Return True if *symbol* belongs to a third-party package.

        A local-project symbol names a real source file inside the repo
        (backtick-escaped path, e.g. ``src/`util.ts```). A third-party symbol
        either references ``node_modules``/a ``.d.ts`` declaration file, or a
        path that does not exist under the repo root. ``local N`` symbols are
        document-scoped, never external.
        """
        if symbol.startswith("local ") or not symbol:
            return False
        source_file = self._symbol_source_file(symbol)
        if source_file is None:
            return False
        if "node_modules" in source_file or source_file.endswith(".d.ts"):
            return True
        # Local-project symbols point at a real repo source file (prefix-aware).
        return self._resolve_symbol_file(symbol) is None

    @staticmethod
    def _external_package(symbol: str) -> str:
        """Return ``<package>@<version>`` for an npm-manager SCIP symbol."""
        parts = symbol.split(" ")
        if len(parts) >= 4:
            return f"{parts[2]}@{parts[3]}"
        return parts[2] if len(parts) >= 3 else symbol

    @staticmethod
    def _symbol_source_file(symbol: str) -> str | None:
        """Extract the repo-relative source path a SCIP symbol is defined in.

        SCIP file descriptors escape path segments with backticks, e.g.
        ``src/`util.ts`/helper().`` -> ``src/util.ts``. Returns ``None`` when
        the descriptor carries no file path.
        """
        # The descriptor is the portion after '<scheme> <manager> <name>
        # <version>' (the first four space-separated fields).
        parts = symbol.split(" ")
        descriptor = " ".join(parts[4:]) if len(parts) >= 5 else ""
        if "`" not in descriptor:
            return None
        # Concatenate the components up to and including the file segment,
        # stripping backticks. The file segment is the one carrying an
        # extension (a '.').
        rebuilt: list[str] = []
        for component in descriptor.split("/"):
            unquoted = component.strip("`")
            rebuilt.append(unquoted)
            if "`" in component and "." in unquoted:
                return "/".join(rebuilt)
        return None

    # ------------------------------------------------------------------
    # Label mapping
    # ------------------------------------------------------------------

    _KIND_LABEL_MAP: dict[str, list[str]] = {
        "Class": ["CodeClass"],
        "Interface": ["CodeInterface"],
        "Method": ["CodeMethod"],
        "Function": ["CodeFunction"],
        "TypeAlias": ["CodeTypeAlias"],
        "Variable": ["CodeVariable"],
        "Constant": ["CodeVariable"],
        "Field": ["CodeAttribute"],
        "Property": ["CodeAttribute"],
        "Module": ["CodeModule"],
        "Namespace": ["CodeModule"],
        "Constructor": ["CodeMethod"],
        "Enum": ["CodeClass"],
        "EnumMember": ["CodeAttribute"],
    }

    def _labels_for_symbol(self, sym: ScipSymbolInfo) -> list[str]:
        """Return graph labels for a SCIP symbol."""
        if sym.kind_name and sym.kind_name != "UnspecifiedKind":
            labels = self._KIND_LABEL_MAP.get(sym.kind_name, ["CodeFunction"])
        else:
            labels = self._labels_from_descriptor(sym.symbol)
        return ["KnowledgeNode"] + labels

    def _labels_from_descriptor(self, symbol: str) -> list[str]:
        """Infer labels from the trailing descriptor of a SCIP symbol."""
        # Strip the package/manager prefix and look at the descriptor suffix.
        # Examples:
        #   ... src/`file.ts`/                 -> CodeModule
        #   ... src/`file.ts`/MyClass#         -> CodeClass
        #   ... src/`file.ts`/MyClass#method(). -> CodeMethod
        #   ... src/`file.ts`/helper().        -> CodeFunction
        #   ... src/`file.ts`/helper().(n)     -> CodeVariable (parameter)
        descriptor = self._symbol_qualname(symbol)
        if descriptor.endswith("#"):
            return ["CodeClass"]
        if "#" in descriptor:
            base = descriptor.split("#")[-1]
            if base.endswith("().") or base.endswith(")."):
                return ["CodeMethod"]
            return ["CodeAttribute"]
        if descriptor.endswith("().") or descriptor.endswith("()"):
            return ["CodeFunction"]
        if descriptor.endswith("/"):
            return ["CodeModule"]
        return ["CodeFunction"]

    # ------------------------------------------------------------------
    # Relationship edges
    # ------------------------------------------------------------------

    def _relationship_to_edge(
        self, sym: ScipSymbolInfo, rel: ScipRelationship, file_path: str
    ) -> CodeEdge | None:
        source = self._node_id(sym.symbol, file_path)
        target = self._node_id(rel.symbol, file_path)
        if rel.is_implementation:
            return CodeEdge(
                source=source,
                target=target,
                graph_id=self.graph_id,
                rel_type="IMPLEMENTS",
                properties={"owner_file_path": str(self.repo_root / file_path)},
            )
        if rel.is_type_definition:
            return CodeEdge(
                source=source,
                target=target,
                graph_id=self.graph_id,
                rel_type="USES_TYPE",
                properties={"owner_file_path": str(self.repo_root / file_path)},
            )
        if rel.is_reference and rel.is_definition:
            # Reference + definition usually means override or alias.
            return CodeEdge(
                source=source,
                target=target,
                graph_id=self.graph_id,
                rel_type="OVERRIDES",
                properties={"owner_file_path": str(self.repo_root / file_path)},
            )
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _module_symbol(document: ScipDocument) -> str | None:
        """Return the SCIP symbol for the document module, if present.

        SCIP file symbols escape path components with backticks, so a simple
        suffix match against the relative path is unreliable. We return the
        first symbol whose descriptor looks like a file module (no class/
        method/function descriptors).
        """
        for sym in document.symbols.values():
            descriptor = ScipGraphTranslator._symbol_qualname(sym.symbol)
            if descriptor.endswith("/") and "#" not in descriptor and "()." not in descriptor:
                return sym.symbol
        return None

    @staticmethod
    def _symbol_name(symbol: str) -> str:
        """Extract a short display name from a SCIP symbol."""
        # SCIP symbols look like:
        #   <scheme> <manager> <name> <version> <descriptor>
        parts = symbol.split(" ")
        descriptor = " ".join(parts[4:]) if len(parts) >= 5 else symbol

        # The descriptor is a chain of '/'-separated components, where the last
        # component carries the local symbol descriptor. Backticks escape path
        # segments but do not affect the trailing descriptor.
        components = descriptor.split("/")
        last = components[-1] if components else descriptor

        # Strip common descriptor suffixes to recover the short name.
        last = last.removesuffix("#")
        for suffix in ("().", "()", ").", ")", "(n)"):
            if last.endswith(suffix):
                last = last[: -len(suffix)]
                break
        # Method descriptors include the class name before '#'.
        if "#" in last:
            last = last.split("#")[-1]
        return last or symbol

    @staticmethod
    def _symbol_qualname(symbol: str) -> str:
        """Return the descriptor-qualified portion of a SCIP symbol."""
        parts = symbol.split(" ", 2)
        if len(parts) >= 3:
            return parts[2]
        return symbol

    @staticmethod
    def _is_external_symbol(symbol: str) -> bool:
        """Return True if the symbol is defined outside the local project.

        Currently conservatively returns False because the local project name
        is not available at document translation time. Callers may override
        this flag later using package-level information from the index metadata.
        """
        return symbol.startswith("local ") is False and "node_modules" in symbol
