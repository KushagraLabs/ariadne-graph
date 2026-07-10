"""HTTP routes for the browser graph view, mounted on the FastMCP app.

Registered via ``register_routes(mcp)`` using ``@mcp.custom_route`` (verified
present on FastMCP 1.27.2). All routes are read-only and reach the live graph
store through the server's ToolRegistry, so they share the one connection the
daemon already holds.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.web import queries

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"


def _store() -> SQLiteGraphStore | None:
    """Return the live SQLite store, or None if the backend isn't SQLite."""
    # Imported lazily to avoid a circular import (server imports web).
    from ariadne_graph.mcp.server import _get_registry

    store = _get_registry().graph_store
    return store if isinstance(store, SQLiteGraphStore) else None


def _require(request: Request, key: str) -> str:
    value = request.query_params.get(key)
    if not value:
        raise ValueError(f"missing required query parameter: {key}")
    return value


def register_routes(mcp: FastMCP) -> None:
    """Mount the browser-view routes on the FastMCP streamable-http app."""

    @mcp.custom_route("/graph", methods=["GET"])
    async def graph_page(request: Request) -> Response:  # noqa: ARG001
        return FileResponse(_STATIC / "index.html", media_type="text/html")

    @mcp.custom_route("/graph/d3.min.js", methods=["GET"])
    async def graph_d3(request: Request) -> Response:  # noqa: ARG001
        return FileResponse(_STATIC / "d3.min.js", media_type="application/javascript")

    @mcp.custom_route("/api/graph/repos", methods=["GET"])
    async def api_repos(request: Request) -> Response:  # noqa: ARG001
        store = _store()
        if store is None:
            return JSONResponse({"error": "graph store is not SQLite"}, status_code=501)
        return JSONResponse({"repos": await queries.list_graphs(store)})

    @mcp.custom_route("/api/graph/full", methods=["GET"])
    async def api_full(request: Request) -> Response:
        store = _store()
        if store is None:
            return JSONResponse({"error": "graph store is not SQLite"}, status_code=501)
        try:
            graph_id = _require(request, "graph")
            repo_root = _require(request, "repo_root")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        payload = await queries.full_graph(store, graph_id, repo_root=repo_root)
        return JSONResponse(payload)
