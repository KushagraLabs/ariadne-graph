"""Tests for runtime capability detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.capabilities import RuntimeCapabilities, get_capabilities
from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import CapabilitiesInput
from ariadne_graph.mcp.tools import ToolRegistry


def test_runtime_capabilities_report_structure() -> None:
    """The capability report contains the expected feature flags."""
    report = get_capabilities()

    assert "features" in report
    assert "degraded" in report
    assert "missing_features" in report
    assert "message" in report

    features = report["features"]
    for feature in (
        "typescript_extraction",
        "semantic_embeddings",
        "sqlite_vector_search",
        "neo4j_backend",
    ):
        assert feature in features
        assert "available" in features[feature]
        assert "extra" in features[feature]
        assert "install" in features[feature]


def test_runtime_capabilities_probe_returns_booleans() -> None:
    """RuntimeCapabilities.probe() returns booleans for each feature."""
    caps = RuntimeCapabilities.probe()
    assert isinstance(caps.typescript_extraction, bool)
    assert isinstance(caps.semantic_embeddings, bool)
    assert isinstance(caps.sqlite_vector_search, bool)
    assert isinstance(caps.neo4j_backend, bool)


@pytest.mark.asyncio
async def test_capabilities_tool_returns_report(tmp_path: Path) -> None:
    """ToolRegistry.handle_capabilities returns a capability report."""
    config = AnalyzerConfig(repo_root=tmp_path)
    registry = ToolRegistry(
        graph_store=MemoryGraphStore(),
        searchable_store=None,
        adapters={"python": PythonLanguageAdapter()},
        config=config,
    )

    result = await registry.handle_capabilities(CapabilitiesInput())
    assert result.capabilities
    assert "features" in result.capabilities
    assert "typescript_extraction" in result.capabilities["features"]
    assert "message" in result.model_dump()
