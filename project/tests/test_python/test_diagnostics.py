"""Tests for diagnostics emitted by the Python extractor."""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.core.diagnostics import DiagnosticsCollector
from ariadne_graph.languages.python_ast.extractor import PythonFactExtractor


def _extract(source: str, path: str = "src/mod.py") -> PythonFactExtractor:
    extractor = PythonFactExtractor(
        source=source,
        file_path=Path(path),
        repo_root=Path("src"),
        graph_id="test-graph",
        parser_version="ast_test",
    )
    extractor.extract()
    return extractor


def _diagnostics(extractor: PythonFactExtractor) -> list[dict[str, object]]:
    return [
        {
            "node_id": n.id,
            "labels": n.labels,
            "properties": n.properties,
        }
        for n in extractor.nodes
        if "CodeDiagnostic" in n.labels
    ]


class TestDiagnosticsCollector:
    """Unit tests for the standalone DiagnosticsCollector."""

    def test_unused_import(self) -> None:
        collector = DiagnosticsCollector()
        diag = collector.add_unused_import("node:os", "os")
        assert diag.rule == "unused_import"
        assert diag.level == "warning"
        assert "os" in diag.message

    def test_missing_type_annotation_return(self) -> None:
        collector = DiagnosticsCollector()
        diag = collector.add_missing_type_annotation(
            "node:func", "my_func", missing_return=True
        )
        assert diag.rule == "missing_type_annotation"
        assert diag.level == "info"
        assert "return" in diag.message

    def test_missing_type_annotation_parameters(self) -> None:
        collector = DiagnosticsCollector()
        diag = collector.add_missing_type_annotation(
            "node:func",
            "my_func",
            missing_parameters=["x", "y"],
            total_parameters=3,
        )
        assert diag.rule == "missing_type_annotation"
        assert "x" in str(diag.properties.get("missing_parameters", []))
        assert "2 of 3 parameters" in diag.message

    def test_missing_type_annotation_combined(self) -> None:
        collector = DiagnosticsCollector()
        diag = collector.add_missing_type_annotation(
            "node:func",
            "my_func",
            missing_return=True,
            missing_parameters=["x"],
            total_parameters=2,
        )
        assert diag.rule == "missing_type_annotation"
        assert "return value" in diag.message
        assert "1 of 2 parameters" in diag.message

    def test_complex_function(self) -> None:
        collector = DiagnosticsCollector()
        diag = collector.add_complex_function("node:func", "big_func", line_count=75)
        assert diag.rule == "complex_function"
        assert diag.level == "info"
        assert "75" in diag.message

    def test_long_parameter_list(self) -> None:
        collector = DiagnosticsCollector()
        diag = collector.add_long_parameter_list(
            "node:func", "many_args", param_count=9
        )
        assert diag.rule == "long_parameter_list"
        assert diag.level == "info"
        assert "9" in diag.message

    def test_get_diagnostics_filters(self) -> None:
        collector = DiagnosticsCollector()
        collector.add_unused_import("node:a", "a")
        collector.add_missing_type_annotation("node:b", "b")
        assert len(collector.get_diagnostics()) == 2
        assert len(collector.get_diagnostics(rule="unused_import")) == 1
        assert len(collector.get_diagnostics(level="info")) == 1


class TestExtractorDiagnostics:
    """Tests for diagnostics emitted during Python extraction."""

    def test_unused_import_diagnostic(self) -> None:
        extractor = _extract(
            '''"""Module."""
import os


def run() -> str:
    return "hello"
'''
        )
        diags = _diagnostics(extractor)
        assert any(d["properties"].get("rule") == "unused_import" for d in diags)

    def test_missing_type_annotation_diagnostic(self) -> None:
        extractor = _extract(
            '''"""Module."""
def run(x):
    return x
'''
        )
        diags = _diagnostics(extractor)
        assert any(
            d["properties"].get("rule") == "missing_type_annotation"
            and "run" in str(d["properties"].get("message", ""))
            and "x" in str(d["properties"].get("missing_parameters", []))
            for d in diags
        )

    def test_complex_function_diagnostic(self) -> None:
        lines = ["def big():"] + ["    x = 1"] * 60 + ["    return x"]
        extractor = _extract("\n".join(lines))
        diags = _diagnostics(extractor)
        assert any(d["properties"].get("rule") == "complex_function" for d in diags)

    def test_long_parameter_list_diagnostic(self) -> None:
        params = ", ".join(f"p{i}" for i in range(8))
        extractor = _extract(f"def many({params}):\n    pass")
        diags = _diagnostics(extractor)
        assert any(
            d["properties"].get("rule") == "long_parameter_list" for d in diags
        )

    def test_no_diagnostic_for_typed_function(self) -> None:
        extractor = _extract(
            '''"""Module."""
def run(x: int) -> str:
    return str(x)
'''
        )
        diags = _diagnostics(extractor)
        assert not any(
            d["properties"].get("rule") == "missing_type_annotation"
            and "run" in str(d["properties"].get("message", ""))
            for d in diags
        )

    def test_has_diagnostic_edges(self) -> None:
        extractor = _extract(
            '''"""Module."""
import os


def run() -> str:
    return "hello"
'''
        )
        diag_edges = [e for e in extractor.edges if e.rel_type == "HAS_DIAGNOSTIC"]
        assert diag_edges
