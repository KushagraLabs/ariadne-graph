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

    def __init__(
        self, repo_root: Path, graph_id: str, path_prefix: str = ""
    ) -> None:
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
            node = self._symbol_to_node(sym, abs_path)
            add_node(node)
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

            # Ensure the target node exists (external symbols may be referenced
            # but not defined in this document).
            target_id = occ.symbol
            if target_id not in seen_node_ids:
                target_node = self._external_symbol_node(target_id)
                add_node(target_node)

            if is_import:
                # Create a CodeImport node for this import site.
                import_id = f"{file_path}:import:{occ.start_line}:{occ.start_col}"
                import_node = CodeNode(
                    id=import_id,
                    graph_id=self.graph_id,
                    labels=["KnowledgeNode", "CodeImport"],
                    properties={
                        "name": self._symbol_name(occ.symbol),
                        "scip_symbol": occ.symbol,
                        "file_path": abs_path,
                        "line_start": occ.start_line + 1,
                        "line_end": occ.end_line + 1,
                    },
                )
                add_node(import_node)
                add_edge(
                    CodeEdge(
                        source=module_id,
                        target=target_id,
                        graph_id=self.graph_id,
                        rel_type="IMPORTS_SYMBOL",
                        properties={
                            "owner_file_path": abs_path,
                            "import_site": import_id,
                        },
                    )
                )
                continue

            if is_definition:
                # Update definition node location if we have a matching symbol.
                if occ.symbol in document.symbols:
                    for node in nodes:
                        if node.id == occ.symbol:
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

    def _symbol_to_node(self, sym: ScipSymbolInfo, abs_path: str) -> CodeNode:
        labels = self._labels_for_symbol(sym)
        name = sym.display_name or self._symbol_name(sym.symbol)
        qualname = self._symbol_qualname(sym.symbol)
        line_start = 0
        line_end = 0
        # Try to find a definition occurrence for line numbers.
        # The translator does not keep document occurrences here; line numbers
        # are best-effort from the symbol metadata.
        return CodeNode(
            id=sym.symbol,
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

    def _external_symbol_node(self, symbol: str) -> CodeNode:
        """Create a stub node for an externally referenced symbol."""
        return CodeNode(
            id=symbol,
            graph_id=self.graph_id,
            labels=["KnowledgeNode", "CodeImport"],
            properties={
                "name": self._symbol_name(symbol),
                "qualname": self._symbol_qualname(symbol),
                "scip_symbol": symbol,
                "is_external": True,
            },
        )

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
        if rel.is_implementation:
            return CodeEdge(
                source=sym.symbol,
                target=rel.symbol,
                graph_id=self.graph_id,
                rel_type="IMPLEMENTS",
                properties={"owner_file_path": str(self.repo_root / file_path)},
            )
        if rel.is_type_definition:
            return CodeEdge(
                source=sym.symbol,
                target=rel.symbol,
                graph_id=self.graph_id,
                rel_type="USES_TYPE",
                properties={"owner_file_path": str(self.repo_root / file_path)},
            )
        if rel.is_reference and rel.is_definition:
            # Reference + definition usually means override or alias.
            return CodeEdge(
                source=sym.symbol,
                target=rel.symbol,
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

