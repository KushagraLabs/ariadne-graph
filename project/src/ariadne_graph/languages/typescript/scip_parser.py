"""Parser for SCIP (Sourcegraph Code Intelligence Protocol) indexes.

Reads a protobuf ``index.scip`` file produced by ``scip-typescript`` and
converts it into plain Python dataclasses. The module depends only on the
vendored ``_scip_pb2`` bindings, so parser unit tests can run against a
committed fixture without invoking ``scip-typescript``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from ariadne_graph.languages.typescript import _scip_pb2 as scip_pb2


# SymbolRole bit flags from scip.proto
class SymbolRole:
    """Bit flags for Occurrence.symbol_roles."""

    DEFINITION = 0x1
    IMPORT = 0x2
    WRITE_ACCESS = 0x4
    READ_ACCESS = 0x8
    GENERATED = 0x10
    TEST = 0x20
    FORWARD_DEFINITION = 0x40


@dataclass
class ScipMetadata:
    """SCIP index metadata."""

    version: int
    tool_name: str
    tool_version: str
    tool_args: list[str]
    project_root: str
    text_encoding: int


@dataclass
class ScipRelationship:
    """Relationship between two SCIP symbols."""

    symbol: str
    is_reference: bool = False
    is_implementation: bool = False
    is_type_definition: bool = False
    is_definition: bool = False


@dataclass
class ScipSymbolInfo:
    """Metadata about a SCIP symbol."""

    symbol: str
    documentation: list[str] = field(default_factory=list)
    relationships: list[ScipRelationship] = field(default_factory=list)
    kind: int = 0
    kind_name: str = "UnspecifiedKind"
    display_name: str = ""
    signature_text: str = ""
    enclosing_symbol: str = ""


@dataclass
class ScipOccurrence:
    """A symbol occurrence inside a document."""

    start_line: int
    start_col: int
    end_line: int
    end_col: int
    symbol: str = ""
    symbol_roles: int = 0
    syntax_kind: int = 0
    diagnostics: list[Any] = field(default_factory=list)


@dataclass
class ScipDocument:
    """A single source file in a SCIP index."""

    relative_path: Path
    language: str = ""
    text: str = ""
    position_encoding: int = 0
    occurrences: list[ScipOccurrence] = field(default_factory=list)
    symbols: dict[str, ScipSymbolInfo] = field(default_factory=dict)


@dataclass
class ScipIndex:
    """In-memory representation of a SCIP index."""

    metadata: ScipMetadata
    documents: dict[Path, ScipDocument] = field(default_factory=dict)
    external_symbols: dict[str, ScipSymbolInfo] = field(default_factory=dict)


class ScipIndexParser:
    """Parse ``index.scip`` protobuf files into plain Python objects."""

    def parse(self, index_path: Path) -> ScipIndex:
        """Parse *index_path* and return a :class:`ScipIndex`."""
        raw = index_path.read_bytes()
        pb_index = scip_pb2.Index()  # type: ignore[attr-defined]
        pb_index.ParseFromString(raw)
        return self._convert_index(pb_index)

    def _convert_index(self, pb_index: scip_pb2.Index) -> ScipIndex:  # type: ignore[name-defined]
        metadata = self._convert_metadata(pb_index.metadata)
        index = ScipIndex(metadata=metadata)

        for pb_doc in pb_index.documents:
            doc = self._convert_document(pb_doc)
            index.documents[doc.relative_path] = doc

        for pb_sym in pb_index.external_symbols:
            sym = self._convert_symbol_info(pb_sym)
            index.external_symbols[sym.symbol] = sym

        return index

    def _convert_metadata(self, pb_metadata: scip_pb2.Metadata) -> ScipMetadata:  # type: ignore[name-defined]
        return ScipMetadata(
            version=pb_metadata.version,
            tool_name=pb_metadata.tool_info.name,
            tool_version=pb_metadata.tool_info.version,
            tool_args=list(pb_metadata.tool_info.arguments),
            project_root=pb_metadata.project_root,
            text_encoding=pb_metadata.text_document_encoding,
        )

    def _convert_document(self, pb_doc: scip_pb2.Document) -> ScipDocument:  # type: ignore[name-defined]
        doc = ScipDocument(
            relative_path=Path(pb_doc.relative_path),
            language=pb_doc.language,
            text=pb_doc.text,
            position_encoding=pb_doc.position_encoding,
        )
        for pb_occ in pb_doc.occurrences:
            doc.occurrences.append(self._convert_occurrence(pb_occ))
        for pb_sym in pb_doc.symbols:
            sym = self._convert_symbol_info(pb_sym)
            doc.symbols[sym.symbol] = sym
        return doc

    def _convert_occurrence(self, pb_occ: scip_pb2.Occurrence) -> ScipOccurrence:  # type: ignore[name-defined]
        start_line, start_col, end_line, end_col = self._read_range(pb_occ)
        return ScipOccurrence(
            start_line=start_line,
            start_col=start_col,
            end_line=end_line,
            end_col=end_col,
            symbol=pb_occ.symbol,
            symbol_roles=pb_occ.symbol_roles,
            syntax_kind=pb_occ.syntax_kind,
            diagnostics=list(pb_occ.diagnostics),
        )

    def _convert_symbol_info(self, pb_sym: scip_pb2.SymbolInformation) -> ScipSymbolInfo:  # type: ignore[name-defined]
        signature_text = ""
        if pb_sym.signature_documentation.text:
            signature_text = pb_sym.signature_documentation.text
        return ScipSymbolInfo(
            symbol=pb_sym.symbol,
            documentation=list(pb_sym.documentation),
            relationships=[self._convert_relationship(r) for r in pb_sym.relationships],
            kind=pb_sym.kind,
            kind_name=self._kind_name(pb_sym.kind),
            display_name=pb_sym.display_name,
            signature_text=signature_text,
            enclosing_symbol=pb_sym.enclosing_symbol,
        )

    @staticmethod
    def _convert_relationship(pb_rel: scip_pb2.Relationship) -> ScipRelationship:  # type: ignore[name-defined]
        return ScipRelationship(
            symbol=pb_rel.symbol,
            is_reference=pb_rel.is_reference,
            is_implementation=pb_rel.is_implementation,
            is_type_definition=pb_rel.is_type_definition,
            is_definition=pb_rel.is_definition,
        )

    def _read_range(self, pb_occ: scip_pb2.Occurrence) -> tuple[int, int, int, int]:  # type: ignore[name-defined]
        """Return 0-based (start_line, start_col, end_line, end_col).

        Prefer the typed oneof fields; fall back to the deprecated repeated
        ``range`` field for older indexes.
        """
        case = pb_occ.WhichOneof("typed_range")
        if case == "single_line_range":
            r = pb_occ.single_line_range
            return (r.line, r.start_character, r.line, r.end_character)
        if case == "multi_line_range":
            r = pb_occ.multi_line_range
            return (r.start_line, r.start_character, r.end_line, r.end_character)

        # Fall back to deprecated repeated int32 range.
        rng = list(pb_occ.range)
        if len(rng) == 3:
            start_line, start_col, end_col = rng
            return (start_line, start_col, start_line, end_col)
        if len(rng) == 4:
            return (rng[0], rng[1], rng[2], rng[3])

        return (0, 0, 0, 0)

    @staticmethod
    def _kind_name(kind_value: int) -> str:
        """Return the enum name for a SymbolInformation.Kind value."""
        descriptor = scip_pb2.SymbolInformation.DESCRIPTOR.enum_types_by_name["Kind"]  # type: ignore[attr-defined]
        value = descriptor.values_by_number.get(kind_value)
        if value is None:
            return "UnspecifiedKind"
        return cast(str, value.name)
