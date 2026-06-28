"""Neo4j graph store implementing SearchableGraphStore.

Uses the official Neo4j Python driver's async API.  Supports vector
indexes (via Neo4j 5.x ``db.index.vector.queryNodes``) and full-text
search (via ``db.index.fulltext.queryNodes``), with graceful fallbacks
to property-based search when indexes are not available.

Neo4j is an **optional** dependency — the module imports cleanly without
it, raising :exc:`ImportError` only when :class:`Neo4jGraphStore` is
instantiated without the driver installed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ariadne_graph.core.models import (
    CodeEdge,
    CodeNode,
    EmbeddingPayload,
    SearchHit,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy / optional neo4j import
# ---------------------------------------------------------------------------
try:
    import neo4j
    import neo4j.graph
    from neo4j import AsyncGraphDatabase

    _HAS_NEO4J = True
except Exception:  # pragma: no cover
    _HAS_NEO4J = False
    neo4j = None  # type: ignore[assignment]

# Query names shared with SQLiteGraphStore
_SUPPORTED_QUERIES: set[str] = {
    "nodes",
    "edges",
    "node_by_id",
    "node_by_name",
    "node_name_fuzzy",
    "neighbors",
    "node_neighbors",
    "node_edges",
    "node_outgoing_edges",
    "node_incoming_edges",
    "node_all_edges",
    "incoming",
    "outgoing",
    "nodes_by_label",
    "nodes_by_file",
    "stored_file_paths",
    "index_metadata",
    "set_index_metadata",
    "count_files",
    "file_hashes",
    "dirty_files",
}


class Neo4jGraphStore:
    """Neo4j graph store for production deployments.

    Uses Neo4j Python driver for async operations.
    Supports vector indexes and GDS community detection.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "password",
    ) -> None:
        if not _HAS_NEO4J:
            raise ImportError(
                "neo4j package is required for Neo4jGraphStore. "
                "Install it with: pip install neo4j"
            )
        self._uri = uri
        self._auth = (user, password)
        self._driver: Any = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async def close(self) -> None:
        """Close the driver and release all connections."""
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    # ------------------------------------------------------------------
    # Session helper
    # ------------------------------------------------------------------

    async def _execute_read(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a read query and return records as dicts."""
        parameters = parameters or {}
        try:
            async with self._driver.session() as session:
                result = await session.run(cypher, parameters)
                records = await result.data()
                await result.consume()
                return cast(list[dict[str, Any]], records)
        except Exception as exc:
            logger.error("Neo4j read query failed: %s", exc)
            return []

    async def _execute_write(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> None:
        """Execute a write query, swallowing non-fatal errors."""
        parameters = parameters or {}
        try:
            async with self._driver.session() as session:
                await session.run(cypher, parameters)
        except Exception as exc:
            logger.error("Neo4j write query failed: %s", exc)
            raise

    # ==================================================================
    # GraphStore interface
    # ==================================================================

    async def delete_graph(self, graph_id: str) -> None:
        """Delete all nodes (and their edges) and file-hash metadata."""
        # DETACH DELETE removes nodes and all attached relationships
        await self._execute_write(
            "MATCH (n:KnowledgeNode {_graph_id: $g}) DETACH DELETE n",
            {"g": graph_id},
        )
        # Also remove file-hash tracking nodes
        await self._execute_write(
            "MATCH (h:FileHash {_graph_id: $g}) DETACH DELETE h",
            {"g": graph_id},
        )
        # Remove project metadata node
        await self._execute_write(
            "MATCH (p:CodeProject {_graph_id: $g}) DETACH DELETE p",
            {"g": graph_id},
        )

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        """Delete all nodes and stored metadata originating from *file_path*.

        Relationships are deleted by ownership (``owner_file_path``), not by
        endpoint membership: a cross-file edge such as ``a.caller -> b.save`` is
        owned by ``a.py`` and must survive a reindex of ``b.py``. So we first
        delete the relationships this file produced, then delete this file's
        nodes that have no surviving (externally-owned) relationships. Nodes
        still referenced by another file's edge are left in place and will be
        MERGE-updated when re-extracted.
        """
        # 1. Delete relationships produced by this file.
        await self._execute_write(
            """
            MATCH (:KnowledgeNode {_graph_id: $g})-[r {owner_file_path: $p}]->()
            DELETE r
            """,
            {"g": graph_id, "p": file_path},
        )
        # 2. Delete this file's nodes that are now isolated; keep nodes still
        #    referenced by a surviving edge owned by another file.
        await self._execute_write(
            """
            MATCH (n:KnowledgeNode {_graph_id: $g})
            WHERE n.file_path = $p AND NOT (n)--()
            DELETE n
            """,
            {"g": graph_id, "p": file_path},
        )
        await self._execute_write(
            """
            MATCH (h:FileHash {_graph_id: $g, file_path: $p})
            DETACH DELETE h
            """,
            {"g": graph_id, "p": file_path},
        )

    async def add_nodes_batch(
        self, graph_id: str, nodes: Sequence[CodeNode]
    ) -> None:
        """Upsert nodes using ``MERGE`` with parameterised properties."""
        if not nodes:
            return

        # Group by primary label so we can apply the correct node label
        by_label: dict[str, list[CodeNode]] = {}
        for node in nodes:
            primary = node.labels[0] if node.labels else "CodeFile"
            by_label.setdefault(primary, []).append(node)

        for label, group in by_label.items():
            rows: list[dict[str, Any]] = []
            for node in group:
                row: dict[str, Any] = {
                    "id": node.id,
                    "properties": {
                        **node.properties,
                        "_labels": node.labels,
                    },
                }
                rows.append(row)

            # Sanitise label — only alphanumeric + underscore allowed
            safe_label = "".join(ch for ch in label if ch.isalnum() or ch == "_")

            await self._execute_write(
                f"""
                UNWIND $rows AS row
                MERGE (n:KnowledgeNode {{id: row.id, _graph_id: $g}})
                SET n += row.properties
                SET n:{safe_label}
                """,
                {"rows": rows, "g": graph_id},
            )

    async def add_edges_batch(
        self, graph_id: str, edges: Sequence[CodeEdge]
    ) -> None:
        """Upsert edges using ``MERGE``."""
        if not edges:
            return

        # Group by rel_type so we can use a concrete relationship type
        by_type: dict[str, list[CodeEdge]] = {}
        for edge in edges:
            by_type.setdefault(edge.rel_type, []).append(edge)

        for rel_type, group in by_type.items():
            rows: list[dict[str, Any]] = []
            for edge in group:
                rows.append({
                    "s": edge.source,
                    "t": edge.target,
                    "props": {
                        **edge.properties,
                        "_rel_type": edge.rel_type,
                    },
                })

            safe_rel = "".join(ch for ch in rel_type if ch.isalnum() or ch == "_")

            await self._execute_write(
                f"""
                UNWIND $rows AS row
                MATCH (a:KnowledgeNode {{_graph_id: $g, id: row.s}})
                MATCH (b:KnowledgeNode {{_graph_id: $g, id: row.t}})
                MERGE (a)-[r:{safe_rel}]->(b)
                SET r += row.props
                """,
                {"rows": rows, "g": graph_id},
            )

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a named query with automatic ``_graph_id`` injection.

        Supported query names (shared with SQLiteGraphStore):

        * ``"nodes"`` — all nodes for *graph_id*
        * ``"node_by_id"`` — params ``{"node_id": str}``
        * ``"neighbors"`` — params ``{"node_id": str}``
        * ``"incoming"`` — params ``{"node_id": str}`` (edges)
        * ``"outgoing"`` — params ``{"node_id": str}`` (edges)
        * ``"nodes_by_label"`` — params ``{"label": str}``
        * ``"nodes_by_file"`` — params ``{"file_path": str}``
        * ``"stored_file_paths"`` — all tracked file paths
        """
        params = dict(params or {})
        params["g"] = graph_id

        if query not in _SUPPORTED_QUERIES:
            logger.warning("Unsupported query: %s", query)
            return []

        if query == "nodes":
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g})
                RETURN n {.*, id: n.id, labels: n._labels} AS n
                """,
                params,
            )

        if query == "node_by_id":
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g, id: $node_id})
                RETURN n {.*, id: n.id, labels: n._labels} AS n
                """,
                params,
            )

        if query == "neighbors":
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g, id: $node_id})
                      -[]-(m:KnowledgeNode {_graph_id: $g})
                RETURN m {.*, id: m.id, labels: m._labels} AS n
                """,
                params,
            )

        if query == "incoming":
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (a:KnowledgeNode {_graph_id: $g})-[r]->(b:KnowledgeNode {_graph_id: $g, id: $node_id})
                RETURN a.id AS source, b.id AS target, type(r) AS rel_type, properties(r) AS properties
                """,
                params,
            )

        if query == "outgoing":
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (a:KnowledgeNode {_graph_id: $g, id: $node_id})-[r]->(b:KnowledgeNode {_graph_id: $g})
                RETURN a.id AS source, b.id AS target, type(r) AS rel_type, properties(r) AS properties
                """,
                params,
            )

        if query == "nodes_by_label":
            params["label"] = params.get("label", "")
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g})
                WHERE $label IN n._labels
                RETURN n {.*, id: n.id, labels: n._labels} AS n
                """,
                params,
            )

        if query == "nodes_by_file":
            params["file_path"] = params.get("file_path", "")
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g})
                WHERE n.file_path = $file_path
                RETURN n {.*, id: n.id, labels: n._labels} AS n
                """,
                params,
            )

        if query == "stored_file_paths":
            return await self._execute_read(
                """
                MATCH (h:FileHash {_graph_id: $g})
                RETURN h.file_path AS file_path
                """,
                params,
            )

        if query == "edges":
            return await self._execute_read(
                """
                MATCH (a:KnowledgeNode {_graph_id: $g})-[r]->(b:KnowledgeNode {_graph_id: $g})
                RETURN a.id AS source, b.id AS target, type(r) AS rel_type, properties(r) AS properties
                """,
                params,
            )

        if query == "node_by_name":
            params["name"] = params.get("name", "")
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g})
                WHERE n.name = $name
                RETURN n {.*, id: n.id, labels: n._labels} AS n
                """,
                params,
            )

        if query == "node_name_fuzzy":
            params["name"] = params.get("name", "")
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g})
                WHERE n.id =~ $pattern OR n.name =~ $pattern OR n.qualname =~ $pattern
                RETURN n {.*, id: n.id, labels: n._labels} AS n
                """,
                {**params, "pattern": f"(?i).*{params['name']}.*"},
            )

        if query in ("node_neighbors", "neighbors"):
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g, id: $node_id})-[]-(m:KnowledgeNode {_graph_id: $g})
                RETURN m {.*, id: m.id, labels: m._labels} AS n
                """,
                params,
            )

        if query == "node_edges":
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (a:KnowledgeNode {_graph_id: $g})-[r]->(b:KnowledgeNode {_graph_id: $g})
                WHERE a.id = $node_id OR b.id = $node_id
                RETURN a.id AS source, b.id AS target, type(r) AS rel_type, properties(r) AS properties
                """,
                params,
            )

        if query == "node_outgoing_edges":
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (a:KnowledgeNode {_graph_id: $g, id: $node_id})-[r]->(b:KnowledgeNode {_graph_id: $g})
                RETURN a.id AS source, b.id AS target, type(r) AS rel_type, properties(r) AS properties
                """,
                params,
            )

        if query == "node_incoming_edges":
            params["node_id"] = params.get("node_id", "")
            return await self._execute_read(
                """
                MATCH (a:KnowledgeNode {_graph_id: $g})-[r]->(b:KnowledgeNode {_graph_id: $g, id: $node_id})
                RETURN a.id AS source, b.id AS target, type(r) AS rel_type, properties(r) AS properties
                """,
                params,
            )

        if query == "node_all_edges":
            return await self.query(graph_id, "node_edges", params)

        if query == "index_metadata":
            return await self._execute_read(
                """
                MATCH (p:CodeProject {_graph_id: $g})
                RETURN p.repo_path AS repo_path,
                       p.last_indexed AS last_indexed,
                       p.file_count AS file_count,
                       p.sync_enabled AS sync_enabled
                """,
                params,
            )

        if query == "set_index_metadata":
            now = datetime.now(UTC).isoformat()
            await self._execute_write(
                """
                MERGE (p:CodeProject {_graph_id: $g})
                SET p.repo_path = $repo_path,
                    p.last_indexed = $last_indexed,
                    p.file_count = $file_count,
                    p.sync_enabled = $sync_enabled
                WITH p
                WHERE p.created_at IS NULL
                SET p.created_at = $now
                """,
                {
                    "g": graph_id,
                    "repo_path": str(params.get("repo_path", "")),
                    "last_indexed": params.get("last_indexed") or now,
                    "file_count": params.get("file_count", 0),
                    "sync_enabled": bool(params.get("sync_enabled")),
                    "now": now,
                },
            )
            return []

        if query == "count_files":
            return await self._execute_read(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g})
                WHERE n.file_path IS NOT NULL
                RETURN count(DISTINCT n.file_path) AS count
                """,
                params,
            )

        if query == "file_hashes":
            return await self._execute_read(
                """
                MATCH (h:FileHash {_graph_id: $g})
                RETURN h.file_path AS file_path, h.content_hash AS content_hash
                """,
                params,
            )

        if query == "dirty_files":
            current_hashes: dict[str, str] = params.get("current_hashes", {})
            rows = await self._execute_read(
                """
                MATCH (h:FileHash {_graph_id: $g})
                RETURN h.file_path AS file_path, h.content_hash AS content_hash
                """,
                params,
            )
            return [
                {"file_path": r["file_path"]}
                for r in rows
                if current_hashes.get(r["file_path"]) != r["content_hash"]
            ]

        return []

    async def get_stored_hash(
        self, graph_id: str, file_path: str
    ) -> str | None:
        """Return the last indexed content hash for a file."""
        rows = await self._execute_read(
            """
            MATCH (h:FileHash {_graph_id: $g, file_path: $p})
            RETURN h.content_hash AS hash
            """,
            {"g": graph_id, "p": file_path},
        )
        return rows[0]["hash"] if rows else None

    async def update_hash(
        self, graph_id: str, file_path: str, content_hash: str
    ) -> None:
        """Upsert the content hash for a file."""
        now = datetime.now(UTC).isoformat()
        await self._execute_write(
            """
            MERGE (h:FileHash {_graph_id: $g, file_path: $p})
            SET h.content_hash = $hash, h.updated_at = $now
            """,
            {"g": graph_id, "p": file_path, "hash": content_hash, "now": now},
        )

    async def register_project(
        self,
        graph_id: str,
        repo_path: str | Path,
        file_count: int | None = None,
        sync_enabled: bool = False,
    ) -> None:
        """Register or update project metadata as a CodeProject node."""
        now = datetime.now(UTC).isoformat()
        await self._execute_write(
            """
            MERGE (p:CodeProject {_graph_id: $g})
            SET p.repo_path = $repo_path,
                p.last_indexed = $now,
                p.file_count = $file_count,
                p.sync_enabled = $sync_enabled
            WITH p
            WHERE p.created_at IS NULL
            SET p.created_at = $now
            """,
            {
                "g": graph_id,
                "repo_path": str(Path(repo_path).resolve()),
                "now": now,
                "file_count": file_count if file_count is not None else 0,
                "sync_enabled": sync_enabled,
            },
        )

    async def list_projects(self) -> list[dict[str, Any]]:
        """Return all registered projects from CodeProject nodes."""
        rows = await self._execute_read(
            """
            MATCH (p:CodeProject)
            RETURN p._graph_id AS graph_id,
                   p.repo_path AS repo_path,
                   p.created_at AS created_at,
                   p.last_indexed AS last_indexed,
                   p.file_count AS file_count,
                   p.sync_enabled AS sync_enabled
            """
        )
        return [
            {
                "graph_id": r["graph_id"],
                "repo_path": r["repo_path"],
                "created_at": r["created_at"],
                "last_indexed": r["last_indexed"],
                "file_count": r["file_count"] or 0,
                "sync_enabled": bool(r["sync_enabled"]),
            }
            for r in rows
        ]

    # ==================================================================
    # SearchableGraphStore interface
    # ==================================================================

    async def upsert_embeddings(
        self,
        graph_id: str,
        rows: Sequence[EmbeddingPayload],
    ) -> None:
        """Store embedding vectors for nodes.

        If a Neo4j vector index exists the embeddings are stored as a
        ``embedding`` float-list property (the index picks them up
        automatically).  If no vector index is available we store the
        raw list and fall back to brute-force cosine similarity during
        search.
        """
        if not rows:
            return

        for row in rows:
            if row.embedding is None:
                continue
            await self._execute_write(
                """
                MATCH (n:KnowledgeNode {_graph_id: $g, id: $node_id})
                SET n.embedding = $vec
                SET n._embedding_model = $model
                """,
                {
                    "g": graph_id,
                    "node_id": row.node_id,
                    "vec": row.embedding,
                    "model": None,
                },
            )

    async def search_vector(
        self,
        graph_id: str,
        vector: Sequence[float],
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by vector similarity.

        Tries ``db.index.vector.queryNodes`` first (Neo4j 5.x),
        falls back to brute-force cosine similarity via Cypher.
        """
        # Attempt vector index query first
        try:
            rows = await self._execute_read(
                """
                CALL db.index.vector.queryNodes('embedding-index', $limit, $vector)
                YIELD node, score
                WHERE node._graph_id = $g
                RETURN node.id AS node_id, score
                """,
                {"g": graph_id, "vector": list(vector), "limit": limit},
            )
            if rows:
                return [
                    SearchHit(node_id=r["node_id"], score=r["score"])
                    for r in rows
                ]
        except Exception as exc:
            logger.debug("Vector index query failed, falling back: %s", exc)

        # Fallback: fetch all embeddings for this graph and compute cosine
        rows = await self._execute_read(
            """
            MATCH (n:KnowledgeNode {_graph_id: $g})
            WHERE n.embedding IS NOT NULL
            RETURN n.id AS node_id, n.embedding AS vec
            """,
            {"g": graph_id},
        )

        query_vec = list(vector)
        q_norm = sum(v * v for v in query_vec) ** 0.5
        if q_norm == 0:
            return []

        similarities: list[tuple[str, float]] = []
        for row in rows:
            vec = row.get("vec", [])
            if not vec:
                continue
            s_norm = sum(v * v for v in vec) ** 0.5
            if s_norm == 0:
                continue
            dot = sum(a * b for a, b in zip(query_vec, vec, strict=False))
            sim = dot / (q_norm * s_norm)
            similarities.append((row["node_id"], sim))

        similarities.sort(key=lambda x: x[1], reverse=True)
        return [
            SearchHit(node_id=node_id, score=score)
            for node_id, score in similarities[:limit]
        ]

    async def search_keyword(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by keyword/phrase matching.

        Tries Neo4j full-text index first, falls back to regex on
        ``id`` and ``name`` properties.
        """
        # Try full-text index first
        try:
            rows = await self._execute_read(
                """
                CALL db.index.fulltext.queryNodes('nodeContent', $query)
                YIELD node, score
                WHERE node._graph_id = $g
                RETURN node.id AS node_id, score
                LIMIT $limit
                """,
                {"g": graph_id, "query": query, "limit": limit},
            )
            if rows:
                return [
                    SearchHit(node_id=r["node_id"], score=r["score"])
                    for r in rows
                ]
        except Exception as exc:
            logger.debug("Fulltext index query failed, falling back: %s", exc)

        # Fallback: regex search on id and name
        rows = await self._execute_read(
            """
            MATCH (n:KnowledgeNode {_graph_id: $g})
            WHERE n.id =~ $pattern OR n.name =~ $pattern
            RETURN n.id AS node_id
            LIMIT $limit
            """,
            {
                "g": graph_id,
                "pattern": f"(?i).*{query}.*",
                "limit": limit,
            },
        )
        return [
            SearchHit(
                node_id=r["node_id"],
                score=1.0 if query.lower() in r["node_id"].lower() else 0.5,
            )
            for r in rows
        ]

    async def set_communities(
        self,
        graph_id: str,
        assignments: dict[str, int],
    ) -> None:
        """Store community assignments for nodes."""
        if not assignments:
            return

        rows = [
            {"id": node_id, "cid": cid}
            for node_id, cid in assignments.items()
        ]

        await self._execute_write(
            """
            UNWIND $rows AS row
            MATCH (n:KnowledgeNode {_graph_id: $g, id: row.id})
            SET n.community_id = row.cid
            """,
            {"rows": rows, "g": graph_id},
        )

    async def get_communities(
        self,
        graph_id: str,
    ) -> dict[int, list[str]]:
        """Get all community memberships as community_id -> [node_ids]."""
        rows = await self._execute_read(
            """
            MATCH (n:KnowledgeNode {_graph_id: $g})
            WHERE n.community_id IS NOT NULL
            RETURN n.id AS node_id, n.community_id AS cid
            """,
            {"g": graph_id},
        )

        result: dict[int, list[str]] = {}
        for row in rows:
            cid = row["cid"]
            result.setdefault(cid, []).append(row["node_id"])
        return result
