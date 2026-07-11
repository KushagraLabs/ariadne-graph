"""MCP server using FastMCP for the Ariadne Graph.

Registers all canonical tools with the mcp.server.fastmcp.FastMCP framework.
Each tool validates input with Pydantic schemas and delegates to ToolRegistry.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path
from typing import Any, Literal

import anyio

# FastMCP imports
from mcp.server.fastmcp import FastMCP

# Project imports
from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.embeddings import LocalEmbeddingProvider
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore
from ariadne_graph.graphstores.factory import create_graph_store
from ariadne_graph.languages.base import LanguageAdapter
from ariadne_graph.mcp.schemas import (
    AuditPublicSurfacesInput,
    CapabilitiesInput,
    DeleteProjectInput,
    DetectChangesInput,
    ExplainEdgeInput,
    FindHotspotsInput,
    GetArchitectureInput,
    ImpactAnalysisInput,
    IndexInput,
    IndexStatusInput,
    InspectFileInput,
    ListCommunitiesInput,
    ListDiagnosticsInput,
    LumenRetrieveInput,
    RetrieveInput,
    SearchCodeInput,
    SearchSemanticInput,
    TraceDependenciesInput,
)
from ariadne_graph.mcp.tools import ToolRegistry

logger = logging.getLogger("ariadne-graph")

# ---------------------------------------------------------------------------
# Global server state (initialised in main)
# ---------------------------------------------------------------------------

_registry: ToolRegistry | None = None


def _get_registry() -> ToolRegistry:
    """Return the global ToolRegistry, raising if not initialised."""
    if _registry is None:
        raise RuntimeError("Server not initialised. Call initialise_registry() first.")
    return _registry


# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP("ariadne")

# Mount the read-only browser graph view (GET /graph + /api/graph/*) on the same
# streamable-http app, so the shared daemon serves it with no extra process.
from ariadne_graph.web import register_routes  # noqa: E402

register_routes(mcp)


# ============================================================================
# INDEXING TOOLS (4)
# ============================================================================

@mcp.tool()
async def code_graph_index(repo_path: str, force_rebuild: bool = False) -> dict[str, Any]:
    """Index a repository: discover files, parse changed ones, store graph facts.

    Args:
        repo_path: Absolute or relative path to the repository root.
        force_rebuild: If True, delete the existing graph and re-index from scratch.

    Returns:
        Dict with status, files_indexed, graph_id, and message.
    """
    registry = _get_registry()
    input_data = IndexInput(repo_path=repo_path, force_rebuild=force_rebuild)
    result = await registry.handle_index(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_index_status(repo_path: str) -> dict[str, Any]:
    """Check the indexing status for a repository.

    Args:
        repo_path: Absolute or relative path to the repository root.

    Returns:
        Dict with graph_id, last_indexed, file_count, dirty_files, sync_enabled,
        and capabilities.
    """
    registry = _get_registry()
    input_data = IndexStatusInput(repo_path=repo_path)
    result = await registry.handle_index_status(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_capabilities() -> dict[str, Any]:
    """Report runtime availability of optional features.

    Returns:
        Dict with feature flags, degraded status, and install hints for missing
        extras (typescript, semantic, vector, neo4j).
    """
    registry = _get_registry()
    input_data = CapabilitiesInput()
    result = await registry.handle_capabilities(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_list_projects() -> dict[str, Any]:
    """List all indexed projects.

    Returns:
        Dict with a list of project entries (graph_id and repo_path).
    """
    registry = _get_registry()
    result = await registry.handle_list_projects()
    return result.model_dump()


@mcp.tool()
async def code_graph_delete_project(repo_path: str) -> dict[str, Any]:
    """Delete an indexed project/graph.

    Args:
        repo_path: Absolute or relative path to the repository root.

    Returns:
        Dict with deleted flag, graph_id, and message.
    """
    registry = _get_registry()
    input_data = DeleteProjectInput(repo_path=repo_path)
    result = await registry.handle_delete_project(input_data)
    return result.model_dump()


# ============================================================================
# QUERY TOOLS (4)
# ============================================================================

@mcp.tool()
async def code_graph_retrieve(
    query: str,
    graph_id: str | None = None,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """Retrieve a symbol and its neighborhood from the code graph.

    Args:
        query: Node ID or symbol name to retrieve.
        graph_id: Optional graph ID. Derived from repo_path if omitted.
        repo_path: Optional repository path used to derive graph_id.

    Returns:
        Dict with results (nodes, neighbors, and edges).
    """
    registry = _get_registry()
    input_data = RetrieveInput(query=query, graph_id=graph_id, repo_path=repo_path)
    result = await registry.handle_retrieve(input_data)
    return result.model_dump()


@mcp.tool()
async def lumen_code_graph_retrieve(
    query: str,
    graph_id: str | None = None,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """Lumen-compatible alias for code_graph_retrieve.

    Args:
        query: Node ID or symbol name to retrieve.
        graph_id: Optional graph ID. Derived from repo_path if omitted.
        repo_path: Optional repository path used to derive graph_id.

    Returns:
        Dict with retrieved nodes/edges and a Lumen-style context block.
    """
    registry = _get_registry()
    if not registry.config.lumen_compat_aliases:
        return {
            "results": [],
            "lumen_context": {"enabled": False},
            "message": "Lumen compatibility aliases are disabled",
        }

    input_data = LumenRetrieveInput(query=query, graph_id=graph_id, repo_path=repo_path)
    result = await registry.handle_lumen_code_graph_retrieve(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_search_semantic(
    query_text: str,
    repo_path: str | None = None,
    limit: int = 10,
    types: list[str] | None = None,
) -> dict[str, Any]:
    """Search the code graph using semantic (vector) similarity.

    Args:
        query_text: Natural language query text.
        repo_path: Optional repository path to restrict search to a single graph.
        limit: Maximum number of results (1-100).
        types: Optional list of node type labels to filter by.

    Returns:
        Dict with ranked search hits.
    """
    registry = _get_registry()
    input_data = SearchSemanticInput(
        query_text=query_text,
        repo_path=repo_path,
        limit=limit,
        types=types or [],
    )
    result = await registry.handle_search_semantic(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_search_code(
    pattern: str,
    repo_path: str | None = None,
    language: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search code by pattern or keyword through the graph.

    Args:
        pattern: Code pattern or keyword to search for.
        repo_path: Optional repository path to restrict search to a single graph.
        language: Optional language filter (e.g. 'python').
        limit: Maximum number of results (1-100).

    Returns:
        Dict with matching code snippets/nodes.
    """
    registry = _get_registry()
    input_data = SearchCodeInput(
        pattern=pattern, repo_path=repo_path, language=language, limit=limit
    )
    result = await registry.handle_search_code(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_trace_dependencies(
    symbol: str,
    direction: str = "both",
    max_depth: int = 3,
    graph_id: str | None = None,
) -> dict[str, Any]:
    """Trace dependencies from a symbol through the code graph.

    Args:
        symbol: Symbol/node ID to start tracing from.
        direction: Direction to trace: "both", "upstream", or "downstream".
        max_depth: Maximum traversal depth (1-10).
        graph_id: Optional graph ID; uses registered projects if omitted.

    Returns:
        Dict with dependency paths as chains of node IDs.
    """
    registry = _get_registry()
    input_data = TraceDependenciesInput(
        symbol=symbol,
        direction=direction,
        max_depth=max_depth,
        graph_id=graph_id,
    )
    result = await registry.handle_trace_dependencies(input_data)
    return result.model_dump()


# ============================================================================
# ANALYSIS TOOLS (5)
# ============================================================================

@mcp.tool()
async def code_graph_impact_analysis(
    symbol: str, graph_id: str | None = None
) -> dict[str, Any]:
    """Analyze the impact of changing a symbol — transitive closure of dependencies.

    Args:
        symbol: Symbol/node ID to analyze.
        graph_id: Optional graph ID; uses registered projects if omitted.

    Returns:
        Dict with target_symbol, total_affected, direct_dependencies,
        transitive_affected, and coupling_scores.
    """
    registry = _get_registry()
    input_data = ImpactAnalysisInput(symbol=symbol, graph_id=graph_id)
    result = await registry.handle_impact_analysis(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_detect_changes(
    repo_path: str, since_ref: str | None = None
) -> dict[str, Any]:
    """Detect changes in a repository since a git ref.

    Args:
        repo_path: Absolute or relative path to the repository root.
        since_ref: Optional git ref (commit SHA, branch, or tag) for comparison.

    Returns:
        Dict with added, modified, and deleted symbol lists.
    """
    registry = _get_registry()
    input_data = DetectChangesInput(repo_path=repo_path, since_ref=since_ref)
    result = await registry.handle_detect_changes(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_find_hotspots(
    repo_path: str, top_n: int = 10, metric: str = "complexity"
) -> dict[str, Any]:
    """Find code hotspots ranked by complexity, coupling, fan_in, or fan_out.

    Args:
        repo_path: Absolute or relative path to the repository root.
        top_n: Number of top hotspots to return (1-100).
        metric: Metric to rank by: "complexity", "coupling", "fan_in", "fan_out".

    Returns:
        Dict with ranked hotspot entries.
    """
    registry = _get_registry()
    input_data = FindHotspotsInput(repo_path=repo_path, top_n=top_n, metric=metric)
    result = await registry.handle_find_hotspots(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_get_architecture(repo_path: str) -> dict[str, Any]:
    """Get an architecture summary of the codebase from community detection.

    Args:
        repo_path: Absolute or relative path to the repository root.

    Returns:
        Dict with architecture summary (communities, files, entities).
    """
    registry = _get_registry()
    input_data = GetArchitectureInput(repo_path=repo_path)
    result = await registry.handle_get_architecture(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_explain_edge(src_path: str, dst_path: str) -> dict[str, Any]:
    """Explain why a single file->file edge is or isn't a layering violation.

    Args:
        src_path: Repo-relative path of the importing file.
        dst_path: Repo-relative path of the imported file.

    Returns:
        Dict with allowed, reason, rule, and whether a front-door import would fix it.
    """
    registry = _get_registry()
    input_data = ExplainEdgeInput(src_path=src_path, dst_path=dst_path)
    result = await registry.handle_explain_edge(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_audit_public_surfaces(repo_path: str) -> dict[str, Any]:
    """Facade/encapsulation audit over declared `public_surfaces`.

    For each module declaring `public_surfaces` in `.ariadne/architecture.yml`:
    its public exports, which external consumers go through the surface vs.
    deep-import an internal, unused public exports, internal files with high
    external fan-in (promotion candidates), and whether the surface is an
    all-exporting barrel with no real encapsulation value. Requires a declared
    `public_surfaces` list -- with no `.ariadne/architecture.yml`, the message
    explains this tool needs one.

    Args:
        repo_path: Absolute or relative path to the repository root.

    Returns:
        Dict with a per-module `modules` report list and a status `message`.
    """
    registry = _get_registry()
    input_data = AuditPublicSurfacesInput(repo_path=repo_path)
    result = await registry.handle_audit_public_surfaces(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_list_communities(
    repo_path: str, community_id: int | None = None
) -> dict[str, Any]:
    """List communities detected in the code graph.

    Args:
        repo_path: Absolute or relative path to the repository root.
        community_id: Optional specific community ID to filter to.

    Returns:
        Dict with community entries (id, member count, members).
    """
    registry = _get_registry()
    input_data = ListCommunitiesInput(repo_path=repo_path, community_id=community_id)
    result = await registry.handle_list_communities(input_data)
    return result.model_dump()


# ============================================================================
# CODE TOOLS (1)
# ============================================================================

@mcp.tool()
async def code_graph_inspect_file(
    file_path: str, graph_id: str | None = None
) -> dict[str, Any]:
    """Inspect a file: return the graph subgraph (nodes and edges) for it.

    Args:
        file_path: Path of the file to inspect.
        graph_id: Optional graph ID. Auto-detected if omitted.

    Returns:
        Dict with nodes and edges for the file.
    """
    registry = _get_registry()
    input_data = InspectFileInput(file_path=file_path, graph_id=graph_id)
    result = await registry.handle_inspect_file(input_data)
    return result.model_dump()


@mcp.tool()
async def code_graph_list_diagnostics(
    repo_path: str,
    level: str | None = None,
    rule: str | None = None,
    file_path: str | None = None,
    production_only: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """List code-hygiene diagnostics for an indexed repository.

    Surfaces both **architecture / layering violations** — deep cross-organ
    imports, dependency cycles, orphan modules, and upward imports (the same
    findings the browser graph view paints as pink edges) — and per-file lint
    findings such as unused imports and missing type annotations. This is the
    tool for auditing architectural hygiene and reproducing the graph UI's
    violations programmatically.

    Args:
        repo_path: Absolute or relative path to the repository root.
        level: Optional severity filter ('error', 'warning', 'info').
        rule: Optional rule filter. Architecture rules: 'deep_import',
            'dependency_cycle', 'orphan_module', 'upward_import'. Lint rules:
            'unused_import', 'missing_type_annotation'.
        file_path: Optional source file path filter.
        production_only: Exclude diagnostics owned by peripheral organs
            (tests/scripts), leaving only production-code findings.
        limit: Maximum number of diagnostics to return (1-1000).

    Returns:
        Dict with diagnostic entries plus a ``counts`` rollup (``by_rule`` and
        ``by_production``) aggregated over all matching diagnostics.
    """
    registry = _get_registry()
    input_data = ListDiagnosticsInput(
        repo_path=repo_path,
        level=level,
        rule=rule,
        file_path=file_path,
        production_only=production_only,
        limit=limit,
    )
    result = await registry.handle_list_diagnostics(input_data)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    """Configure logging for the MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _init_adapters() -> dict[str, LanguageAdapter]:
    """Initialise language adapters (Python AST by default)."""
    adapters: dict[str, LanguageAdapter] = {}

    # Try to load Python adapter
    try:
        from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter

        adapters["python"] = PythonLanguageAdapter()
        logger.info("Loaded Python language adapter")
    except ImportError:
        logger.warning("Python adapter not available; it only requires the stdlib ast module")
    except Exception as exc:
        logger.warning("Failed to load Python adapter: %s", exc)

    # Try to load TypeScript adapter
    try:
        from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter

        adapters["typescript"] = TypeScriptLanguageAdapter()
        logger.info("Loaded TypeScript language adapter")
    except ImportError:
        logger.debug("TypeScript adapter not available")
    except Exception as exc:
        logger.warning("Failed to load TypeScript adapter: %s", exc)

    if not adapters:
        logger.warning("No language adapters loaded — indexing will not find source files")

    return adapters


def _init_graph_store(config: AnalyzerConfig | None = None) -> GraphStore:
    """Initialise the default graph store from config/env vars."""
    return create_graph_store(config)


def initialise_registry(
    graph_store: GraphStore | None = None,
    adapters: dict[str, LanguageAdapter] | None = None,
    config: AnalyzerConfig | None = None,
) -> ToolRegistry:
    """Initialise the global ToolRegistry.

    Args:
        graph_store: Graph store backend. Defaults to SQLite/Neo4j/Memory
            based on environment variables and config.
        adapters: Language adapters. Defaults to auto-detected adapters.
        config: Analyzer configuration. Defaults to default config.

    Returns:
        The initialised ToolRegistry.
    """
    global _registry

    if config is None:
        config = AnalyzerConfig(repo_root=Path.cwd())

    if graph_store is None:
        graph_store = _init_graph_store(config)

    if adapters is None:
        adapters = _init_adapters()

    # Check if graph_store also implements SearchableGraphStore
    searchable_store: SearchableGraphStore | None = None
    if isinstance(graph_store, SearchableGraphStore):
        searchable_store = graph_store

    snippet_extractor = SnippetExtractor(repo_root=config.resolved_repo_root)

    embedding_provider: LocalEmbeddingProvider | None = None
    if config.embedding_provider == "local":
        try:
            embedding_provider = LocalEmbeddingProvider(config=config)
        except Exception as exc:
            logger.warning("Failed to initialise local embedding provider: %s", exc)

    _registry = ToolRegistry(
        graph_store=graph_store,
        searchable_store=searchable_store,
        adapters=adapters,
        config=config,
        snippet_extractor=snippet_extractor,
        embedding_provider=embedding_provider,
    )

    return _registry


async def _run_server_async(
    transport: Literal["stdio", "sse", "streamable-http"],
    mount_path: str | None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Async server runner: starts auto-sync, runs MCP transport, cleans up.

    Runs inside the event loop created by ``anyio.run`` so that auto-sync and
    the server share the same loop and are shut down gracefully.

    ``host``/``port`` override the HTTP bind address for the ``sse`` and
    ``streamable-http`` transports so a single shared daemon can bind a fixed
    port. They are ignored for stdio.
    """
    if host is not None:
        mcp.settings.host = host
    if port is not None:
        mcp.settings.port = port

    registry = _get_registry()
    logger.info("ToolRegistry initialised with %d adapters", len(registry.adapters))

    if registry.config.auto_sync:
        try:
            await registry.start_auto_sync()
            logger.info("Auto-sync started")
        except Exception as exc:
            logger.warning("Failed to start auto-sync: %s", exc)

    try:
        match transport:
            case "stdio":
                await mcp.run_stdio_async()
            case "sse":
                await mcp.run_sse_async(mount_path)
            case "streamable-http":
                await mcp.run_streamable_http_async()
    finally:
        logger.info("Shutting down Ariadne Graph server")
        with contextlib.suppress(Exception):
            await registry.close()


def main(
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio",
    mount_path: str | None = None,
) -> None:
    """Main entry point: initialise and run the MCP server."""
    _setup_logging()
    logger.info("Starting Ariadne Graph server")
    initialise_registry()

    # anyio.run creates the event loop; _run_server_async starts auto-sync in
    # that same loop so the background task stays alive while the server runs.
    anyio.run(_run_server_async, transport, mount_path)


# ---------------------------------------------------------------------------
# Entry point for `python -m ariadne_graph.mcp.server`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
