"""Graph storage backends.

All backends implement the :class:`GraphStore` protocol.
Production-capable backends also implement :class:`SearchableGraphStore`.
"""

from __future__ import annotations

from ariadne_graph.graphstores.base import GraphStore, SearchableGraphStore
from ariadne_graph.graphstores.memory import MemoryGraphStore

# SQLiteGraphStore is optional — requires aiosqlite + numpy
try:
    from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
except ImportError:
    SQLiteGraphStore = None  # type: ignore[misc, assignment]

# Neo4jGraphStore is optional — requires neo4j driver
try:
    from ariadne_graph.graphstores.neo4j import Neo4jGraphStore
except ImportError:
    Neo4jGraphStore = None  # type: ignore[misc, assignment]

__all__ = [
    "GraphStore",
    "SearchableGraphStore",
    "MemoryGraphStore",
    "SQLiteGraphStore",
    "Neo4jGraphStore",
]
