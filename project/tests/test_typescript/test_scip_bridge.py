"""Deterministic unit tests for the SCIP parser and translator.

These tests exercise the vendored SCIP protobuf bindings and the
``ScipIndexParser`` / ``ScipGraphTranslator`` against the committed binary
fixture at ``tests/fixtures/ts_scip_project/index.scip``. They do **not**
invoke ``scip-typescript`` at runtime, so they are stable in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.models import CodeGraphDelta
from ariadne_graph.languages.typescript.scip_parser import (
    ScipIndex,
    ScipIndexParser,
    SymbolRole,
)
from ariadne_graph.languages.typescript.scip_translator import ScipGraphTranslator


@pytest.fixture
def fixture_index_path() -> Path:
    return Path(__file__).parent.parent / "fixtures" / "ts_scip_project" / "index.scip"


@pytest.fixture
def parsed_index(fixture_index_path: Path) -> ScipIndex:
    parser = ScipIndexParser()
    return parser.parse(fixture_index_path)


class TestScipIndexParser:
    def test_metadata_tool_info(self, parsed_index: ScipIndex) -> None:
        assert parsed_index.metadata.tool_name == "scip-typescript"
        assert parsed_index.metadata.tool_version

    def test_documents_present(self, parsed_index: ScipIndex) -> None:
        paths = sorted(parsed_index.documents.keys())
        assert paths == [
            Path("src/animal.ts"),
            Path("src/barrel.ts"),
            Path("src/dog.ts"),
            Path("src/main.ts"),
            Path("src/utils.ts"),
        ]

    def test_symbols_and_occurrences(self, parsed_index: ScipIndex) -> None:
        doc = parsed_index.documents[Path("src/main.ts")]
        assert len(doc.symbols) > 0
        assert len(doc.occurrences) > 0

    def test_definition_occurrence_has_definition_role(
        self, parsed_index: ScipIndex
    ) -> None:
        doc = parsed_index.documents[Path("src/main.ts")]
        definition_occurrences = [
            occ for occ in doc.occurrences if occ.symbol_roles & SymbolRole.DEFINITION
        ]
        assert len(definition_occurrences) >= 1

    def test_range_fallback_parses(self, parsed_index: ScipIndex) -> None:
        """scip-typescript 0.4.0 still emits the deprecated range field."""
        doc = parsed_index.documents[Path("src/main.ts")]
        for occ in doc.occurrences:
            assert occ.start_line >= 0
            assert occ.start_col >= 0
            assert occ.end_line >= occ.start_line
            assert occ.end_col >= 0


class TestScipGraphTranslator:
    def test_translator_creates_module_and_symbols(
        self, parsed_index: ScipIndex
    ) -> None:
        translator = ScipGraphTranslator(
            repo_root=Path(__file__).parent.parent / "fixtures" / "ts_scip_project",
            graph_id="test-graph",
        )
        doc = parsed_index.documents[Path("src/dog.ts")]
        delta = translator.translate(doc)

        assert isinstance(delta, CodeGraphDelta)
        module_nodes = [n for n in delta.nodes if "CodeModule" in n.labels]
        assert module_nodes

    def test_class_labels(self, parsed_index: ScipIndex) -> None:
        translator = ScipGraphTranslator(
            repo_root=Path(__file__).parent.parent / "fixtures" / "ts_scip_project",
            graph_id="test-graph",
        )
        doc = parsed_index.documents[Path("src/dog.ts")]
        delta = translator.translate(doc)

        class_nodes = [n for n in delta.nodes if "CodeClass" in n.labels]
        assert class_nodes
        assert any(n.properties.get("name") == "Dog" for n in class_nodes)

    def test_method_labels(self, parsed_index: ScipIndex) -> None:
        translator = ScipGraphTranslator(
            repo_root=Path(__file__).parent.parent / "fixtures" / "ts_scip_project",
            graph_id="test-graph",
        )
        doc = parsed_index.documents[Path("src/dog.ts")]
        delta = translator.translate(doc)

        method_nodes = [n for n in delta.nodes if "CodeMethod" in n.labels]
        assert any(n.properties.get("name") == "sound" for n in method_nodes)

    def test_call_ranges_produce_calls_edges(
        self, parsed_index: ScipIndex
    ) -> None:
        translator = ScipGraphTranslator(
            repo_root=Path(__file__).parent.parent / "fixtures" / "ts_scip_project",
            graph_id="test-graph",
        )
        doc = parsed_index.documents[Path("src/main.ts")]
        # Simulate the call range for `bar(5)` on line 7 (0-based line 6).
        call_ranges = {(6, 9, 6, 12)}
        delta = translator.translate(doc, call_ranges=call_ranges)

        calls = [e for e in delta.edges if e.rel_type == "CALLS"]
        assert calls
        target = "scip-typescript npm scip-fixture 1.0.0 src/`utils.ts`/helper()."
        assert any(e.target == target for e in calls)

    def test_reference_edges_for_imports(self, parsed_index: ScipIndex) -> None:
        translator = ScipGraphTranslator(
            repo_root=Path(__file__).parent.parent / "fixtures" / "ts_scip_project",
            graph_id="test-graph",
        )
        doc = parsed_index.documents[Path("src/main.ts")]
        delta = translator.translate(doc)

        references = [e for e in delta.edges if e.rel_type == "REFERENCES"]
        targets = {e.target for e in references}
        assert any("Dog#" in t for t in targets)
        assert any("helper()." in t for t in targets)
