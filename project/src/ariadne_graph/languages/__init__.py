"""Language adapters for code extraction.

Each adapter implements the :class:`LanguageAdapter` protocol and handles
file discovery and AST-based fact extraction for one language family.
"""

from __future__ import annotations

from ariadne_graph.languages.base import (
    ExtractionContext,
    LanguageAdapter,
)

# Adapters are optional — import only when their dependencies are installed
try:
    from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
except ImportError:
    PythonLanguageAdapter = None  # type: ignore[misc, assignment]

try:
    from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter
except ImportError:
    TypeScriptLanguageAdapter = None  # type: ignore[misc, assignment]

__all__ = [
    "ExtractionContext",
    "LanguageAdapter",
    "PythonLanguageAdapter",
    "TypeScriptLanguageAdapter",
]
