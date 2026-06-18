"""CLI entry point for Ariadne Graph.

Provides standalone commands for indexing, querying, and managing code graphs
without requiring an MCP client.

Usage:
    ariadne index <repo_path> [--force]
    ariadne status <repo_path>
    ariadne search <repo_path> <query> [--semantic] [--types ...] [--language ...] [--limit N]
    ariadne retrieve <repo_path> <symbol>
    ariadne architecture <repo_path>
    ariadne mcp [--transport {stdio,sse,streamable-http}]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.embeddings import LocalEmbeddingProvider
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import SearchableGraphStore
from ariadne_graph.graphstores.factory import create_graph_store
from ariadne_graph.mcp.schemas import (
    CapabilitiesInput,
    DeleteProjectInput,
    DetectChangesInput,
    FindHotspotsInput,
    GetArchitectureInput,
    ImpactAnalysisInput,
    IndexInput,
    IndexStatusInput,
    InspectFileInput,
    ListCommunitiesInput,
    ListDiagnosticsInput,
    RetrieveInput,
    SearchCodeInput,
    SearchSemanticInput,
    TraceDependenciesInput,
)
from ariadne_graph.mcp.server import _run_server_async, initialise_registry
from ariadne_graph.mcp.tools import ToolRegistry

logger = logging.getLogger("ariadne-cli")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI output."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def _init_registry(repo_path: str | None = None) -> ToolRegistry:
    """Initialise a ToolRegistry for CLI use.

    Args:
        repo_path: Optional repo path to use as the root.

    Returns:
        Configured ToolRegistry instance.
    """
    root = Path(repo_path).resolve() if repo_path else Path.cwd()
    config = AnalyzerConfig(repo_root=root)

    graph_store = create_graph_store(config)
    searchable_store = graph_store if isinstance(graph_store, SearchableGraphStore) else None

    # Try to load language adapters
    adapters: dict[str, Any] = {}
    try:
        from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter

        adapters["python"] = PythonLanguageAdapter()
    except ImportError:
        pass
    except Exception:
        pass

    try:
        from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter

        adapters["typescript"] = TypeScriptLanguageAdapter()
    except ImportError:
        pass
    except Exception:
        pass

    snippet_extractor = SnippetExtractor(repo_root=root)

    embedding_provider: LocalEmbeddingProvider | None = None
    if config.embedding_provider == "local":
        try:
            embedding_provider = LocalEmbeddingProvider(config=config)
        except Exception as exc:
            logger.warning("Failed to initialise local embedding provider: %s", exc)

    registry = ToolRegistry(
        graph_store=graph_store,
        searchable_store=searchable_store,
        adapters=adapters,
        config=config,
        snippet_extractor=snippet_extractor,
        embedding_provider=embedding_provider,
    )

    if repo_path:
        graph_id = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        registry.register_graph_meta(str(root), graph_id)

    return registry


@asynccontextmanager
async def _registry_scope(repo_path: str | None = None) -> AsyncIterator[ToolRegistry]:
    """Create a ToolRegistry and ensure its graph store is closed on exit."""
    registry = _init_registry(repo_path)
    try:
        yield registry
    finally:
        with contextlib.suppress(Exception):
            await registry.close()


def _print_json(data: dict[str, Any]) -> None:
    """Print data as formatted JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _handle_index(args: argparse.Namespace) -> int:
    """Handle the 'index' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = IndexInput(repo_path=args.repo_path, force_rebuild=args.force)
        result = await registry.handle_index(input_data)
        _print_json(result.model_dump())
        return 0 if result.status in ("success", "partial") else 1


async def _handle_status(args: argparse.Namespace) -> int:
    """Handle the 'status' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = IndexStatusInput(repo_path=args.repo_path)
        result = await registry.handle_index_status(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_capabilities(args: argparse.Namespace) -> int:
    """Handle the 'capabilities' command."""
    async with _registry_scope() as registry:
        input_data = CapabilitiesInput()
        result = await registry.handle_capabilities(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_search(args: argparse.Namespace) -> int:
    """Handle the 'search' command."""
    async with _registry_scope(args.repo_path) as registry:
        if args.semantic:
            semantic_input = SearchSemanticInput(
                query_text=args.query,
                repo_path=args.repo_path,
                limit=args.limit,
                types=list(args.types) if args.types else [],
            )
            semantic_result = await registry.handle_search_semantic(semantic_input)
            _print_json(semantic_result.model_dump())
        else:
            code_input = SearchCodeInput(
                pattern=args.query,
                repo_path=args.repo_path,
                language=args.language,
                limit=args.limit,
            )
            code_result = await registry.handle_search_code(code_input)
            _print_json(code_result.model_dump())

        return 0


def _derive_graph_id(repo_path: str) -> str:
    """Derive the graph_id from a repo path."""
    return hashlib.sha256(str(Path(repo_path).resolve()).encode()).hexdigest()[:16]


async def _handle_retrieve(args: argparse.Namespace) -> int:
    """Handle the 'retrieve' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = RetrieveInput(
            query=args.symbol,
            graph_id=_derive_graph_id(args.repo_path),
        )
        result = await registry.handle_retrieve(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_architecture(args: argparse.Namespace) -> int:
    """Handle the 'architecture' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = GetArchitectureInput(repo_path=args.repo_path)
        result = await registry.handle_get_architecture(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_communities(args: argparse.Namespace) -> int:
    """Handle the 'communities' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = ListCommunitiesInput(repo_path=args.repo_path)
        result = await registry.handle_list_communities(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_hotspots(args: argparse.Namespace) -> int:
    """Handle the 'hotspots' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = FindHotspotsInput(
            repo_path=args.repo_path,
            top_n=args.top_n,
            metric=args.metric,
        )
        result = await registry.handle_find_hotspots(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_impact(args: argparse.Namespace) -> int:
    """Handle the 'impact' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = ImpactAnalysisInput(
            symbol=args.symbol,
            graph_id=_derive_graph_id(args.repo_path),
        )
        result = await registry.handle_impact_analysis(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_trace(args: argparse.Namespace) -> int:
    """Handle the 'trace' command."""
    async with _registry_scope(args.repo_path) as registry:
        direction_map = {
            "upstream": "up",
            "downstream": "down",
            "both": "both",
        }
        input_data = TraceDependenciesInput(
            symbol=args.symbol,
            graph_id=_derive_graph_id(args.repo_path),
            direction=direction_map.get(args.direction, args.direction),
            max_depth=args.max_depth,
        )
        result = await registry.handle_trace_dependencies(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_changes(args: argparse.Namespace) -> int:
    """Handle the 'changes' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = DetectChangesInput(
            repo_path=args.repo_path,
            since_ref=args.since_ref,
        )
        result = await registry.handle_detect_changes(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_inspect(args: argparse.Namespace) -> int:
    """Handle the 'inspect' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = InspectFileInput(
            file_path=args.file_path,
            graph_id=_derive_graph_id(args.repo_path),
        )
        result = await registry.handle_inspect_file(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_diagnostics(args: argparse.Namespace) -> int:
    """Handle the 'diagnostics' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = ListDiagnosticsInput(
            repo_path=args.repo_path,
            level=args.level,
            rule=args.rule,
            file_path=args.file_path,
            limit=args.limit,
        )
        result = await registry.handle_list_diagnostics(input_data)
        _print_json(result.model_dump())
        return 0


async def _handle_delete(args: argparse.Namespace) -> int:
    """Handle the 'delete' command."""
    async with _registry_scope(args.repo_path) as registry:
        input_data = DeleteProjectInput(repo_path=args.repo_path)
        result = await registry.handle_delete_project(input_data)
        _print_json(result.model_dump())
        return 0 if result.deleted else 1


async def _handle_list_projects(args: argparse.Namespace) -> int:
    """Handle the 'list' command."""
    async with _registry_scope() as registry:
        result = await registry.handle_list_projects()
        _print_json(result.model_dump())
        return 0


async def _handle_mcp(args: argparse.Namespace) -> int:
    """Handle the 'mcp' command — start the MCP server.

    Runs inside the CLI's existing event loop, so we call the server's async
    runner directly instead of spawning a nested event loop.
    """
    initialise_registry()
    await _run_server_async(transport=args.transport, mount_path=None)
    return 0


async def _handle_watch(args: argparse.Namespace) -> int:
    """Handle the 'watch' command — index once then poll for changes."""
    async with _registry_scope(args.repo_path) as registry:
        # Immediate index
        input_data = IndexInput(repo_path=args.repo_path, force_rebuild=args.force)
        result = await registry.handle_index(input_data)
        _print_json(result.model_dump())
        if result.status == "error":
            return 1

        await registry.start_auto_sync()
        logger.info(
            "Watching %s for changes every %.1f seconds (Ctrl-C to stop)",
            args.repo_path,
            registry.config.incremental_sync_interval,
        )

        interval = registry.config.incremental_sync_interval
        try:
            while True:
                await asyncio.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopping watch...", file=sys.stderr)
        finally:
            await registry.stop_auto_sync()

    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="ariadne",
        description="Ariadne Graph — code graph analysis tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        exit_on_error=False,
        epilog="""
examples:
  ariadne index ./my-project
  ariadne index ./my-project --force
  ariadne status ./my-project
  ariadne capabilities
  ariadne search ./my-project "def parse"
  ariadne search ./my-project "authentication" --semantic
  ariadne retrieve ./my-project mymodule.MyClass
  ariadne architecture ./my-project
  ariadne communities ./my-project
  ariadne hotspots ./my-project --metric fan_in --top-n 20
  ariadne impact ./my-project mymodule.core_function
  ariadne changes ./my-project --since-ref HEAD~1
  ariadne inspect ./my-project src/mymodule/core.py
  ariadne delete ./my-project
  ariadne mcp
        """,
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- index ---
    index_parser = subparsers.add_parser("index", help="Index a repository")
    index_parser.add_argument("repo_path", help="Path to the repository root")
    index_parser.add_argument(
        "--force", action="store_true", help="Force re-index (delete existing graph)"
    )

    # --- status ---
    status_parser = subparsers.add_parser("status", help="Check index status")
    status_parser.add_argument("repo_path", help="Path to the repository root")

    # --- capabilities ---
    subparsers.add_parser("capabilities", help="Show runtime capability report")

    # --- search ---
    search_parser = subparsers.add_parser("search", help="Search the code graph")
    search_parser.add_argument("repo_path", help="Path to the repository root")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument(
        "--semantic", action="store_true", help="Use semantic (vector) search"
    )
    search_parser.add_argument(
        "--types",
        action="extend",
        nargs="+",
        help="Node type labels to filter semantic search (e.g. CodeFunction CodeClass)",
    )
    search_parser.add_argument(
        "--language",
        default=None,
        help="Language filter for keyword/code search (e.g. 'python')",
    )
    search_parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of results (default: 10)"
    )

    # --- retrieve ---
    retrieve_parser = subparsers.add_parser("retrieve", help="Retrieve a symbol")
    retrieve_parser.add_argument("repo_path", help="Path to the repository root")
    retrieve_parser.add_argument("symbol", help="Symbol/node ID to retrieve")

    # --- architecture ---
    arch_parser = subparsers.add_parser(
        "architecture", help="Show architecture summary"
    )
    arch_parser.add_argument("repo_path", help="Path to the repository root")

    # --- communities ---
    communities_parser = subparsers.add_parser(
        "communities", help="List detected communities"
    )
    communities_parser.add_argument("repo_path", help="Path to the repository root")
    communities_parser.add_argument(
        "--community-id", type=int, default=None, help="Filter to a specific community"
    )

    # --- hotspots ---
    hotspots_parser = subparsers.add_parser(
        "hotspots", help="Find code hotspots"
    )
    hotspots_parser.add_argument("repo_path", help="Path to the repository root")
    hotspots_parser.add_argument(
        "--metric",
        default="complexity",
        choices=["complexity", "coupling", "fan_in", "fan_out"],
        help="Metric to rank hotspots by (default: complexity)",
    )
    hotspots_parser.add_argument(
        "--top-n", type=int, default=10, help="Number of hotspots to show (default: 10)"
    )

    # --- impact ---
    impact_parser = subparsers.add_parser(
        "impact", help="Analyze impact of changing a symbol"
    )
    impact_parser.add_argument("repo_path", help="Path to the repository root")
    impact_parser.add_argument("symbol", help="Symbol/node ID to analyze")

    # --- trace ---
    trace_parser = subparsers.add_parser(
        "trace", help="Trace dependencies from a symbol"
    )
    trace_parser.add_argument("repo_path", help="Path to the repository root")
    trace_parser.add_argument("symbol", help="Symbol/node ID to trace from")
    trace_parser.add_argument(
        "--direction",
        default="both",
        choices=["both", "upstream", "downstream"],
        help="Direction to trace (default: both)",
    )
    trace_parser.add_argument(
        "--max-depth", type=int, default=3, help="Maximum traversal depth (default: 3)"
    )

    # --- changes ---
    changes_parser = subparsers.add_parser(
        "changes", help="Detect changes since last index or git ref"
    )
    changes_parser.add_argument("repo_path", help="Path to the repository root")
    changes_parser.add_argument(
        "--since-ref", default=None, help="Git ref (commit SHA, branch, or tag) for comparison"
    )

    # --- inspect ---
    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect a file in the graph"
    )
    inspect_parser.add_argument("repo_path", help="Path to the repository root")
    inspect_parser.add_argument("file_path", help="Path of the file to inspect")

    # --- diagnostics ---
    diagnostics_parser = subparsers.add_parser(
        "diagnostics", help="List code diagnostics for a repository"
    )
    diagnostics_parser.add_argument("repo_path", help="Path to the repository root")
    diagnostics_parser.add_argument(
        "--level", default=None, help="Filter by severity level"
    )
    diagnostics_parser.add_argument(
        "--rule", default=None, help="Filter by rule identifier"
    )
    diagnostics_parser.add_argument(
        "--file-path", default=None, help="Filter by source file path"
    )
    diagnostics_parser.add_argument(
        "--limit", type=int, default=100, help="Maximum diagnostics to return"
    )

    # --- delete ---
    delete_parser = subparsers.add_parser(
        "delete", help="Delete an indexed project"
    )
    delete_parser.add_argument("repo_path", help="Path to the repository root")

    # --- mcp ---
    mcp_parser = subparsers.add_parser("mcp", help="Start the MCP server")
    mcp_parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="Transport protocol (default: stdio)",
    )

    # --- watch ---
    watch_parser = subparsers.add_parser(
        "watch", help="Index a repository and poll for changes"
    )
    watch_parser.add_argument("repo_path", help="Path to the repository root")
    watch_parser.add_argument(
        "--force", action="store_true", help="Force initial re-index"
    )

    # --- list (alias for list_projects) ---
    subparsers.add_parser("list", help="List indexed projects")

    return parser


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], Awaitable[int]]] = {
    "index": _handle_index,
    "status": _handle_status,
    "capabilities": _handle_capabilities,
    "search": _handle_search,
    "retrieve": _handle_retrieve,
    "architecture": _handle_architecture,
    "communities": _handle_communities,
    "hotspots": _handle_hotspots,
    "impact": _handle_impact,
    "trace": _handle_trace,
    "changes": _handle_changes,
    "inspect": _handle_inspect,
    "diagnostics": _handle_diagnostics,
    "delete": _handle_delete,
    "mcp": _handle_mcp,
    "watch": _handle_watch,
    "list": _handle_list_projects,
}


async def _async_main(args: Sequence[str] | None = None) -> int:
    """Async main entry point."""
    parser = _build_parser()
    try:
        parsed = parser.parse_args(args)
    except argparse.ArgumentError as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 1

    _setup_logging(verbose=getattr(parsed, "verbose", False))

    if parsed.command is None:
        parser.print_help()
        return 0

    handler = _COMMAND_HANDLERS.get(parsed.command)
    if handler is None:
        print(f"Unknown command: {parsed.command}", file=sys.stderr)
        parser.print_help()
        return 1

    try:
        return await handler(parsed)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("Command failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(args: Sequence[str] | None = None) -> int:
    """Synchronous entry point for the CLI.

    Args:
        args: Optional command-line arguments. Defaults to sys.argv[1:].

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
