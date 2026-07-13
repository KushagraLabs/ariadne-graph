"""Smoke tests for the MCP server lifecycle and tool registration."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp import server as server_module
from ariadne_graph.mcp.server import initialise_registry
from ariadne_graph.mcp.tools import ToolRegistry


class TestInitialiseRegistry:
    """Tests for the MCP server registry initialisation."""

    @pytest.mark.asyncio
    async def test_initialise_registry_with_memory_store(self, tmp_path: Path) -> None:
        graph_store = MemoryGraphStore()
        adapters: dict[str, object] = {"python": PythonLanguageAdapter()}
        registry = initialise_registry(
            graph_store=graph_store,
            adapters=adapters,  # type: ignore[arg-type]
            config=None,
        )
        assert isinstance(registry, ToolRegistry)
        assert registry.graph_store is graph_store
        assert "python" in registry.adapters
        await registry.close()

    def test_fastmcp_server_has_tools(self) -> None:
        """The module-level FastMCP app exposes the expected tools."""
        tools = server_module.mcp._tool_manager._tools
        assert "code_graph_index" in tools
        assert "code_graph_retrieve" in tools
        assert "code_graph_capabilities" in tools
        assert "code_graph_list_diagnostics" in tools
        assert "lumen_code_graph_retrieve" in tools
        assert "code_graph_suggest_placement" in tools
        assert "code_graph_find_equivalent" in tools
