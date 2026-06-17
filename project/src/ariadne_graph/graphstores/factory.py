"""Factory for creating the configured graph store backend."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.graphstores.base import GraphStore

logger = logging.getLogger(__name__)


def _create_lumen_wrapper(config: AnalyzerConfig, store: GraphStore) -> GraphStore:
    """Wrap *store* in the Lumen compatibility adapter when enabled."""
    from ariadne_graph.graphstores.lumen import LumenGraphStore

    logger.info("Wrapping graph store in Lumen compatibility adapter")
    return LumenGraphStore(
        store,
        workspace_root=config.lumen_workspace_root,
        workspace_id=config.lumen_workspace_id,
    )


def create_graph_store(config: AnalyzerConfig | None = None) -> GraphStore:
    """Create a graph store based on environment variables and config.

    Priority:
      1. Neo4j if ARIADNE_NEO4J_URI or config.neo4j_uri is set.
      2. SQLite at ARIADNE_DB or config.db_path or .ariadne/graph.db.
      3. Memory graph store as a last resort.

    When ``config.lumen_enabled`` is True the selected backend is wrapped in
    ``LumenGraphStore`` for Lumen KG compatibility.

    Args:
        config: Optional analyzer config. If omitted, defaults are used.

    Returns:
        An instantiated GraphStore.
    """
    cfg = config or AnalyzerConfig(repo_root=Path(os.getcwd()))
    graph_store: GraphStore | None = None

    # Neo4j takes precedence when a URI is configured
    neo4j_uri = cfg.neo4j_uri or os.environ.get("ARIADNE_NEO4J_URI")
    if neo4j_uri:
        try:
            from ariadne_graph.graphstores.neo4j import Neo4jGraphStore

            user = cfg.neo4j_user or os.environ.get("ARIADNE_NEO4J_USER", "neo4j")
            password = cfg.neo4j_password or os.environ.get(
                "ARIADNE_NEO4J_PASSWORD", "password"
            )
            logger.info("Using Neo4j graph store at %s", neo4j_uri)
            graph_store = Neo4jGraphStore(uri=neo4j_uri, user=user, password=password)
        except ImportError as exc:
            logger.warning(
                "Neo4j requested but driver not installed; falling back to SQLite: %s", exc
            )
        except Exception as exc:
            logger.warning(
                "Failed to initialise Neo4j graph store; falling back to SQLite: %s", exc
            )

    # SQLite is the default local backend
    if graph_store is None:
        db_path = cfg.db_path or os.environ.get("ARIADNE_DB") or ".ariadne/graph.db"
        try:
            from ariadne_graph.graphstores.sqlite import SQLiteGraphStore

            logger.info("Using SQLite graph store at %s", db_path)
            graph_store = SQLiteGraphStore(
                db_path=db_path, embedding_dimensions=cfg.embedding_dimensions
            )
        except ImportError as exc:
            logger.warning("SQLite backend unavailable; falling back to memory store: %s", exc)
        except Exception as exc:
            logger.warning("Failed to initialise SQLite graph store; falling back to memory: %s", exc)

    # Fallback
    if graph_store is None:
        from ariadne_graph.graphstores.memory import MemoryGraphStore

        logger.warning("Using in-memory graph store — data will not persist")
        graph_store = MemoryGraphStore()

    # Optionally wrap the selected backend in the Lumen compatibility adapter.
    if cfg.lumen_enabled:
        graph_store = _create_lumen_wrapper(cfg, graph_store)

    return graph_store
