"""Human-facing HTTP view of the code graph.

Presentation layer sibling to ``mcp/`` (agent-facing tools). Both sit over
``core/`` + ``graphstores/``; neither duplicates the other. Read-only.

``register_routes(mcp)`` mounts the browser view on the existing FastMCP
streamable-http app so the shared daemon on :8848 serves it — no new process.
"""

from __future__ import annotations

from ariadne_graph.web.routes import register_routes

__all__ = ["register_routes"]
