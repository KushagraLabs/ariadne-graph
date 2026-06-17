"""Diagnostics collector for code quality audits during extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class CodeDiagnostic:
    """A single diagnostic finding about a code entity."""

    node_id: str
    level: str  # "error", "warning", "info"
    message: str
    rule: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    properties: dict[str, Any] = field(default_factory=dict)


class DiagnosticsCollector:
    """Collects diagnostic findings during code extraction.

    Provides standard audit rules for common code quality issues:
    - unused_import: An import that is not referenced in the module.
    - missing_type_annotation: A function parameter or return without type hints.
    - complex_function: A function body exceeding 50 lines.
    - long_parameter_list: A function with more than 7 parameters.
    """

    # Thresholds for standard rules
    COMPLEX_FUNCTION_LINES = 50
    LONG_PARAMETER_LIST_COUNT = 7

    def __init__(self) -> None:
        self._diagnostics: list[CodeDiagnostic] = []

    def add_diagnostic(
        self,
        node_id: str,
        level: str,
        message: str,
        rule: str,
        **kwargs: Any,
    ) -> CodeDiagnostic:
        """Add a diagnostic finding.

        Args:
            node_id: The code node ID this diagnostic applies to.
            level: Severity level - "error", "warning", or "info".
            message: Human-readable description of the issue.
            rule: Rule identifier (e.g. "unused_import", "complex_function").
            **kwargs: Additional properties to attach to the diagnostic.

        Returns:
            The created CodeDiagnostic instance.
        """
        diag = CodeDiagnostic(
            node_id=node_id,
            level=level,
            message=message,
            rule=rule,
            properties=dict(kwargs),
        )
        self._diagnostics.append(diag)
        return diag

    def add_unused_import(self, node_id: str, import_name: str) -> CodeDiagnostic:
        """Report an unused import."""
        return self.add_diagnostic(
            node_id=node_id,
            level="warning",
            message=f"Import '{import_name}' is not used in the module",
            rule="unused_import",
            import_name=import_name,
        )

    def add_missing_type_annotation(
        self,
        node_id: str,
        function_name: str,
        parameter: str | None = None,
    ) -> CodeDiagnostic:
        """Report a missing type annotation on a function or parameter."""
        if parameter:
            msg = f"Parameter '{parameter}' in '{function_name}' lacks type annotation"
        else:
            msg = f"Function '{function_name}' lacks return type annotation"
        return self.add_diagnostic(
            node_id=node_id,
            level="info",
            message=msg,
            rule="missing_type_annotation",
            function_name=function_name,
            parameter=parameter,
        )

    def add_complex_function(
        self,
        node_id: str,
        function_name: str,
        line_count: int,
    ) -> CodeDiagnostic:
        """Report a function that exceeds the complexity line threshold."""
        return self.add_diagnostic(
            node_id=node_id,
            level="warning" if line_count > self.COMPLEX_FUNCTION_LINES * 2 else "info",
            message=(
                f"Function '{function_name}' is {line_count} lines long "
                f"(threshold: {self.COMPLEX_FUNCTION_LINES})"
            ),
            rule="complex_function",
            function_name=function_name,
            line_count=line_count,
            threshold=self.COMPLEX_FUNCTION_LINES,
        )

    def add_long_parameter_list(
        self,
        node_id: str,
        function_name: str,
        param_count: int,
        parameters: list[str] | None = None,
    ) -> CodeDiagnostic:
        """Report a function with too many parameters."""
        return self.add_diagnostic(
            node_id=node_id,
            level="warning" if param_count > self.LONG_PARAMETER_LIST_COUNT * 2 else "info",
            message=(
                f"Function '{function_name}' has {param_count} parameters "
                f"(threshold: {self.LONG_PARAMETER_LIST_COUNT})"
            ),
            rule="long_parameter_list",
            function_name=function_name,
            param_count=param_count,
            parameters=parameters or [],
        )

    def get_diagnostics(
        self,
        node_id: str | None = None,
        level: str | None = None,
        rule: str | None = None,
    ) -> list[CodeDiagnostic]:
        """Get diagnostics, optionally filtered.

        Args:
            node_id: Filter by node ID.
            level: Filter by severity level.
            rule: Filter by rule name.

        Returns:
            Matching diagnostics.
        """
        results = self._diagnostics
        if node_id is not None:
            results = [d for d in results if d.node_id == node_id]
        if level is not None:
            results = [d for d in results if d.level == level]
        if rule is not None:
            results = [d for d in results if d.rule == rule]
        return results

    def clear(self) -> None:
        """Remove all collected diagnostics."""
        self._diagnostics.clear()

    def __len__(self) -> int:
        return len(self._diagnostics)
