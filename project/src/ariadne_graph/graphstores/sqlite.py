"""SQLite graph store with FTS5 keyword search and vector search support.

Implements the full SearchableGraphStore protocol using aiosqlite for
async operations.  Supports optional sqlite-vec for accelerated vector
similarity, falling back to brute-force cosine similarity in Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import numpy as np

from ariadne_graph.core.models import (
    CodeEdge,
    CodeNode,
    EmbeddingPayload,
    SearchHit,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional sqlite-vec integration
# ---------------------------------------------------------------------------
try:
    import sqlite_vec  # type: ignore[import-untyped]

    _HAS_SQLITE_VEC = True
except Exception:  # pragma: no cover
    _HAS_SQLITE_VEC = False

VEC_TABLE = "vec_items"


class _PooledConnection:
    """Wraps a persistent aiosqlite connection and releases a lock on close.

    This lets the rest of ``SQLiteGraphStore`` keep its
    ``db = await self._connect(); try: ... finally: await db.close()`` pattern
    while actually reusing one connection and serializing access through a lock.
    """

    def __init__(self, db: aiosqlite.Connection, lock: asyncio.Lock) -> None:
        self._db = db
        self._lock = lock

    async def close(self) -> None:
        """Release the access lock (the underlying connection stays open)."""
        self._lock.release()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_DDL = """
-- Graph metadata
CREATE TABLE IF NOT EXISTS graphs (
    graph_id TEXT PRIMARY KEY,
    repo_path TEXT,
    created_at TEXT,
    last_indexed TEXT,
    file_count INTEGER,
    sync_enabled INTEGER DEFAULT 0
);

-- Nodes
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    labels TEXT,
    properties TEXT,
    file_path TEXT,
    PRIMARY KEY (graph_id, id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_graph ON nodes(graph_id);
CREATE INDEX IF NOT EXISTS idx_nodes_name
    ON nodes(graph_id, json_extract(properties, '$.name'));

-- Edges
CREATE TABLE IF NOT EXISTS edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    properties TEXT,
    PRIMARY KEY (graph_id, source, target, rel_type)
);
CREATE INDEX IF NOT EXISTS idx_edges_graph ON edges(graph_id);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(graph_id, source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(graph_id, target);

-- File hashes for incremental sync
CREATE TABLE IF NOT EXISTS file_hashes (
    graph_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at TEXT,
    PRIMARY KEY (graph_id, file_path)
);

-- Snippets
CREATE TABLE IF NOT EXISTS snippets (
    graph_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    snippet TEXT,
    PRIMARY KEY (graph_id, node_id)
);

-- FTS content backing table.  We keep node_id/graph_id here so the FTS5
-- virtual table can be external-content keyed by rowid.  Deletes by rowid
-- are O(1) in the FTS index, avoiding the super-linear scan caused by
-- DELETE ... WHERE node_id IN (...).
-- NOTE: this table is intentionally NOT named node_fts_content, because
-- FTS5 creates shadow tables called <fts_table>_content for its own use.
CREATE TABLE IF NOT EXISTS fts_node_content (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    node_id TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    UNIQUE(graph_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_fts_node_content_graph_node
    ON fts_node_content(graph_id, node_id);

-- FTS for keyword search (external content against fts_node_content).
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    content,
    node_id UNINDEXED,
    graph_id UNINDEXED,
    content='fts_node_content',
    content_rowid='rowid'
);

-- Embeddings for vector search (float blob storage, dimension-agnostic)
CREATE TABLE IF NOT EXISTS embeddings (
    graph_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    model TEXT,
    vector BLOB,
    dimensions INTEGER,
    PRIMARY KEY (graph_id, node_id)
);

-- Community assignments
CREATE TABLE IF NOT EXISTS communities (
    graph_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    community_id INTEGER NOT NULL,
    PRIMARY KEY (graph_id, node_id)
);
"""


class SQLiteGraphStore:
    """SQLite-backed graph store implementing SearchableGraphStore.

    Uses aiosqlite for async, thread-safe database access.  Stores nodes,
    edges, embeddings, and community assignments.  Supports keyword search
    via FTS5 (with LIKE fallback) and vector search via sqlite-vec (with
    brute-force Python fallback).
    """

    # Architecture-hygiene capability (bead code_hygiene_mcp-420): the dep-edge
    # SSOT is SQL here, so this backend serves it natively.
    supports_dep_edges: bool = True

    def __init__(
        self,
        db_path: str = ".ariadne/graph.db",
        embedding_dimensions: int = 384,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding_dimensions = embedding_dimensions
        self._has_sqlite_vec: bool = _HAS_SQLITE_VEC
        self._schema_ready: bool = False
        # A single persistent aiosqlite connection is reused across calls to
        # avoid the pathological per-call dlopen() overhead of loading the
        # sqlite-vec extension (and to avoid thousands of open/close cycles).
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._vec_loaded_on_db: bool = False

    async def _ensure_schema_once(self) -> None:
        """Create the schema on first use."""
        if self._schema_ready:
            return
        await self._ensure_schema()
        self._schema_ready = True

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    async def _load_sqlite_vec(self, db: aiosqlite.Connection) -> bool:
        """Load the sqlite-vec extension on the given connection.

        Returns True if the extension was loaded successfully.  Uses a SQL
        ``SELECT load_extension(...)`` so the operation runs on the aiosqlite
        worker thread and avoids cross-thread sqlite3 connection access.
        """
        if not self._has_sqlite_vec:
            return False
        try:
            await db.enable_load_extension(True)
            await db.execute(
                "SELECT load_extension(?)",
                (sqlite_vec.loadable_path(),),
            )
            return True
        except Exception as exc:
            logger.warning("sqlite-vec load failed, disabling: %s", exc)
            self._has_sqlite_vec = False
            return False

    async def _connect(self) -> aiosqlite.Connection:
        """Return the single persistent aiosqlite connection.

        Access is serialized through an asyncio lock; the returned connection
        wrapper releases the lock when ``close()`` is called.  This avoids
        opening a fresh connection (and re-dlopen-ing the sqlite-vec extension)
        on every store call.
        """
        await self._lock.acquire()
        try:
            if self._db is None:
                await self._ensure_schema_once()
                self._db = await aiosqlite.connect(str(self.db_path))
                self._db.row_factory = sqlite3.Row
                # Load sqlite-vec once on the persistent connection.  This is
                # the only dlopen() of the extension for the store's lifetime.
                if await self._load_sqlite_vec(self._db):
                    self._vec_loaded_on_db = True
            return _PooledConnection(self._db, self._lock)  # type: ignore[return-value]
        except Exception:
            self._lock.release()
            raise

    async def close(self) -> None:
        """Close the persistent connection and release the lock."""
        async with self._lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def dep_edges(self, graph_id: str) -> list[tuple[str, str]]:
        """Cross-file dependency edges via the ``_DEP_EDGE_SQL`` SSOT.

        Local import keeps ``_DEP_EDGE_SQL`` a single definition in
        ``core.architecture`` (avoiding a module-load cycle: architecture.py only
        type-hints this store). The 4 ``?`` placeholders bind the graph_id for the
        two CTEs + the two UNION-ALL branches.
        """
        from ariadne_graph.core.architecture import _DEP_EDGE_SQL

        db = await self._connect()
        try:
            cursor = await db.execute(
                _DEP_EDGE_SQL, (graph_id, graph_id, graph_id, graph_id)
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()
        return [(r["sf"], r["tf"]) for r in rows]

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _ensure_schema(self) -> None:
        """Create all tables, indexes, and virtual tables."""
        # Open a raw connection to avoid recursion through _connect().
        db = await aiosqlite.connect(str(self.db_path))
        try:
            db.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrency
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")

            # Execute schema DDL
            for stmt in _SCHEMA_DDL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)

            # Migrate existing graphs tables that lack file_count or sync_enabled
            with contextlib.suppress(aiosqlite.OperationalError):
                await db.execute("ALTER TABLE graphs ADD COLUMN file_count INTEGER")
            with contextlib.suppress(aiosqlite.OperationalError):
                await db.execute("ALTER TABLE graphs ADD COLUMN sync_enabled INTEGER DEFAULT 0")

            # Promote file_path from a JSON property to a real indexed column.
            # This fixes the O(N) json_extract scan in delete_file_facts on
            # large repositories.
            with contextlib.suppress(aiosqlite.OperationalError):
                await db.execute("ALTER TABLE nodes ADD COLUMN file_path TEXT")
            # Create the index after the column migration so legacy databases
            # that already have a nodes table without file_path don't fail.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_file_path ON nodes(graph_id, file_path)"
            )
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM nodes
                WHERE file_path IS NULL
                  AND json_extract(properties, '$.file_path') IS NOT NULL
                """
            )
            row = await cursor.fetchone()
            missing = row[0] if row else 0
            if missing:
                logger.info("Backfilling file_path for %d existing nodes", missing)
                await db.execute(
                    """
                    UPDATE nodes
                    SET file_path = json_extract(properties, '$.file_path')
                    WHERE file_path IS NULL
                      AND json_extract(properties, '$.file_path') IS NOT NULL
                    """
                )

            # Migrate legacy FTS5 tables to the rowid-keyed external-content
            # schema.  This fixes the super-linear DELETE ... WHERE node_id IN
            # (...) bottleneck observed on repos >~100k nodes.
            await self._migrate_old_fts(db)

            # Optionally load sqlite-vec extension.  _load_sqlite_vec uses a
            # SQL ``SELECT load_extension(...)`` so the operation runs on the
            # aiosqlite worker thread and avoids the "SQLite objects created
            # in a thread can only be used in that same thread" warning/failure.
            if await self._load_sqlite_vec(db):
                logger.info("sqlite-vec extension loaded")

                # Create vec0 virtual table for vector search
                await db.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0(
                        node_id TEXT PRIMARY KEY,
                        graph_id TEXT,
                        embedding FLOAT[{self.embedding_dimensions}]
                    )
                """)

            await db.commit()
        finally:
            await db.close()

    async def _migrate_old_fts(self, db: aiosqlite.Connection) -> None:
        """Migrate legacy FTS5 table to rowid-keyed external-content schema.

        The original ``node_fts`` stored its own content in an internal shadow
        table (``node_fts_content``), so ``DELETE FROM node_fts WHERE node_id IN
        (...)`` degraded super-linearly.  The new schema keeps a regular content
        table (``fts_node_content``) and an external-content FTS5 table keyed by
        ``rowid``.  Deletes by rowid are O(1) in the FTS index.

        Existing FTS data is preserved by copying it into the content table
        and rebuilding the index.  If migration fails, the old table is
        dropped and recreated empty; callers will need to re-index to
        repopulate keyword search.
        """
        # Detect whether node_fts exists with the legacy (internal-content)
        # schema.  FTS5 creates a ``<table>_content`` shadow table only when it
        # stores content internally; with external content that shadow table is
        # absent.  The ``content`` option is not stored in ``node_fts_config``,
        # so we must inspect sqlite_master instead.
        try:
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'node_fts_content'"
            )
            row = await cursor.fetchone()
            if row is None:
                return  # Already on the new external-content schema.
        except Exception:
            # node_fts may not exist yet.
            return

        logger.info("Migrating legacy FTS5 table to rowid-keyed external content")

        # Copy legacy FTS rows into the new content table.  We batch to keep
        # memory bounded on very large indexes.
        try:
            cursor = await db.execute("SELECT content, node_id, graph_id FROM node_fts")
            while True:
                batch = await cursor.fetchmany(1000)
                if not batch:
                    break
                await db.executemany(
                    """
                    INSERT OR REPLACE INTO fts_node_content
                    (content, node_id, graph_id)
                    VALUES (?, ?, ?)
                    """,
                    [(r["content"], r["node_id"], r["graph_id"]) for r in batch],
                )
            await db.execute("DROP TABLE node_fts")
        except Exception as exc:
            logger.warning("Failed to migrate legacy FTS data (will recreate empty): %s", exc)
            with contextlib.suppress(Exception):
                await db.execute("DROP TABLE IF EXISTS node_fts")

        # Recreate the external-content virtual table and rebuild from content.
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
                content,
                node_id UNINDEXED,
                graph_id UNINDEXED,
                content='fts_node_content',
                content_rowid='rowid'
            )
        """)
        try:
            await db.execute("INSERT INTO node_fts(node_fts) VALUES ('rebuild')")
        except Exception as exc:
            logger.warning("FTS rebuild after migration failed: %s", exc)
        await db.commit()

    # ==================================================================
    # GraphStore interface
    # ==================================================================

    async def delete_graph(self, graph_id: str) -> None:
        """Delete all data associated with a graph."""
        db = await self._connect()
        try:
            tables = [
                "nodes",
                "edges",
                "file_hashes",
                "snippets",
                "embeddings",
                "communities",
                "graphs",
            ]
            for table in tables:
                await db.execute(f"DELETE FROM {table} WHERE graph_id = ?", (graph_id,))
            # Clean up FTS entries for this graph by rowid (O(1) per row).
            await db.execute(
                """
                DELETE FROM node_fts
                WHERE rowid IN (
                    SELECT rowid FROM fts_node_content WHERE graph_id = ?
                )
                """,
                (graph_id,),
            )
            await db.execute("DELETE FROM fts_node_content WHERE graph_id = ?", (graph_id,))
            # Clean up vec_items if sqlite-vec is active
            if self._has_sqlite_vec:
                await db.execute(f"DELETE FROM {VEC_TABLE} WHERE graph_id = ?", (graph_id,))
            await db.commit()
        finally:
            await db.close()

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        """Delete all nodes, edges, and stored metadata originating from a file."""
        db = await self._connect()
        try:
            # Remove stored content hash for this file
            await db.execute(
                "DELETE FROM file_hashes WHERE graph_id = ? AND file_path = ?",
                (graph_id, file_path),
            )

            # Find node ids to remove using the indexed file_path column.
            cursor = await db.execute(
                "SELECT id FROM nodes WHERE graph_id = ? AND file_path = ?",
                (graph_id, file_path),
            )
            rows = await cursor.fetchall()
            node_ids = [r["id"] for r in rows]

            if not node_ids:
                await db.commit()
                return

            # Delete edges *produced by* this file (tracked via the
            # owner_file_path property), not every edge merely touching one of
            # its nodes. A cross-file edge such as ``a.caller -> b.save`` is
            # owned by ``a.py``; reindexing ``b.py`` must not remove it.
            await db.execute(
                """
                DELETE FROM edges
                WHERE graph_id = ?
                  AND json_extract(properties, '$.owner_file_path') = ?
                """,
                (graph_id, file_path),
            )

            # Legacy fallback: edges predating owner tracking have no
            # owner_file_path and are removed by node membership. Chunk size
            # keeps deletes under the SQLite 999-parameter limit.
            node_id_chunks = self._chunked(node_ids, size=400)
            for chunk, placeholders in node_id_chunks:
                await db.execute(
                    f"""
                    DELETE FROM edges
                    WHERE graph_id = ?
                      AND json_extract(properties, '$.owner_file_path') IS NULL
                      AND (source IN ({placeholders}) OR target IN ({placeholders}))
                    """,
                    (graph_id, *chunk, *chunk),
                )

            # Delete vec_items if sqlite-vec is active.  Must happen before
            # the rows in ``nodes`` are removed because the vec table is keyed
            # by node id, not by file_path.
            if self._has_sqlite_vec:
                for chunk, placeholders in self._chunked(node_ids):
                    await db.execute(
                        f"""
                        DELETE FROM {VEC_TABLE}
                        WHERE graph_id = ? AND node_id IN ({placeholders})
                        """,
                        (graph_id, *chunk),
                    )

            # Delete snippets, embeddings, communities, and nodes.
            for chunk, placeholders in self._chunked(node_ids):
                await db.execute(
                    f"""
                    DELETE FROM snippets
                    WHERE graph_id = ? AND node_id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )
                await db.execute(
                    f"""
                    DELETE FROM embeddings
                    WHERE graph_id = ? AND node_id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )
                await db.execute(
                    f"""
                    DELETE FROM communities
                    WHERE graph_id = ? AND node_id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )
                await db.execute(
                    f"""
                    DELETE FROM nodes
                    WHERE graph_id = ? AND id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )

            # Delete FTS entries for this file by rowid.  We resolve rowids
            # from the regular content table (indexed by graph_id+node_id) and
            # then delete from the FTS virtual table by rowid, which is O(1)
            # per row instead of the prior super-linear scan.
            for chunk, placeholders in self._chunked(node_ids):
                cursor = await db.execute(
                    f"""
                    SELECT rowid FROM fts_node_content
                    WHERE graph_id = ? AND node_id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )
                rowids = [str(r["rowid"]) for r in await cursor.fetchall()]
                for rchunk, rplaceholders in self._chunked(rowids):
                    await db.execute(
                        f"DELETE FROM node_fts WHERE rowid IN ({rplaceholders})",
                        rchunk,
                    )

            # Remove the external content rows for these nodes.
            for chunk, placeholders in self._chunked(node_ids):
                await db.execute(
                    f"""
                    DELETE FROM fts_node_content
                    WHERE graph_id = ? AND node_id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )

            await db.commit()
        finally:
            await db.close()

    # Node labels that anchor a file (a bare import stub must never strip these).
    _ANCHOR_LABELS = frozenset({"CodeFile", "CodeModule"})

    @classmethod
    def _is_stub_anchor_collision(
        cls, incoming_labels: list[str], existing_labels: list[str]
    ) -> bool:
        """True only for the SCIP bare-import-stub vs file-anchor id collision.

        SCIP translate emits a bare ``CodeImport`` stub whose id equals a real
        ``CodeFile``/``CodeModule`` node's id. That is the ONE case where a blind
        replace loses information and we must union/merge. Every other id
        conflict is a legitimate re-index of the same logical node and keeps
        normal upsert (incoming replaces) semantics -- otherwise stale/removed
        properties would wrongly survive an update.
        """
        incoming = set(incoming_labels)
        existing = set(existing_labels)
        if incoming == existing:
            return False
        # One side is the import stub, the other carries a file anchor.
        incoming_is_stub = "CodeImport" in incoming and not (incoming & cls._ANCHOR_LABELS)
        existing_is_stub = "CodeImport" in existing and not (existing & cls._ANCHOR_LABELS)
        incoming_is_anchor = bool(incoming & cls._ANCHOR_LABELS)
        existing_is_anchor = bool(existing & cls._ANCHOR_LABELS)
        return (incoming_is_stub and existing_is_anchor) or (
            existing_is_stub and incoming_is_anchor
        )

    @classmethod
    def _merge_node(
        cls,
        node: CodeNode,
        existing: tuple[list[str], dict[str, Any]] | None,
    ) -> CodeNode:
        """Upsert an incoming node against the row already stored under its id.

        Default is normal replace (incoming wins) so updates can remove obsolete
        properties. The ONE exception is the SCIP bare-import-stub vs
        file-anchor id collision (:meth:`_is_stub_anchor_collision`): there we
        UNION labels and keep the richer side's properties so the stub can't
        strip a real ``CodeFile``/``CodeModule`` anchor.
        """
        if existing is None:
            return node

        existing_labels, existing_props = existing

        if not cls._is_stub_anchor_collision(node.labels, existing_labels):
            # Legitimate re-index/update: incoming node replaces the stored row.
            return node

        # Order-preserving union: keep existing labels first, then any new ones.
        merged_labels = list(existing_labels)
        for label in node.labels:
            if label not in merged_labels:
                merged_labels.append(label)

        # The richer side (more properties) wins on key collisions; the other
        # side only contributes keys the richer side is missing.
        if len(node.properties) >= len(existing_props):
            richer, poorer = node.properties, existing_props
        else:
            richer, poorer = existing_props, node.properties
        merged_props = {**poorer, **richer}

        return CodeNode(
            id=node.id,
            graph_id=node.graph_id,
            labels=merged_labels,
            properties=merged_props,
        )

    @staticmethod
    def _build_fts_content(node: CodeNode) -> str:
        """Build a searchable text string from node identifiers and metadata."""
        parts = [node.id]
        name = node.properties.get("name")
        if name:
            parts.append(str(name))
        parts.extend(node.labels)
        file_path = node.properties.get("file_path")
        if file_path:
            parts.append(str(file_path))
        qualname = node.properties.get("qualname")
        if qualname:
            parts.append(str(qualname))
        return " ".join(parts)

    @staticmethod
    def _chunked(items: Sequence[str], size: int = 999) -> list[tuple[list[str], str]]:
        """Return (chunk, placeholders) tuples for safe SQLite IN clauses."""
        chunks: list[tuple[list[str], str]] = []
        for offset in range(0, len(items), size):
            chunk = list(items[offset : offset + size])
            placeholders = ",".join("?" * len(chunk))
            chunks.append((chunk, placeholders))
        return chunks

    async def add_nodes_batch(self, graph_id: str, nodes: Sequence[CodeNode]) -> None:
        """Upsert nodes (INSERT OR REPLACE) and keep FTS index in sync."""
        if not nodes:
            return

        db = await self._connect()
        try:
            node_ids = [node.id for node in nodes]

            # Read any existing rows for these ids so we can MERGE rather than
            # clobber on conflict.  SCIP translate emits bare CodeImport stubs
            # whose id equals a real CodeFile/CodeModule node id; a blind
            # INSERT OR REPLACE would strip the file anchor's labels and
            # richer properties (see code_hygiene_mcp-6c7).
            existing: dict[str, tuple[list[str], dict[str, Any]]] = {}
            for chunk, placeholders in self._chunked(node_ids):
                cursor = await db.execute(
                    f"""
                    SELECT id, labels, properties FROM nodes
                    WHERE graph_id = ? AND id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )
                for r in await cursor.fetchall():
                    existing[r["id"]] = (
                        json.loads(r["labels"] or "[]"),
                        json.loads(r["properties"] or "{}"),
                    )

            # Fold the batch incrementally BY ID so a same-batch collision (a
            # rich CodeFile node and its bare CodeImport stub in ONE call, the
            # fresh-index case) merges too -- not just conflicts against rows
            # already in SQLite. Seed each id from its stored row, then merge
            # every incoming node with the same id on top, preserving order.
            merged_by_id: dict[str, CodeNode] = {}
            for node in nodes:
                prior = merged_by_id.get(node.id)
                if prior is not None:
                    base = (prior.labels, prior.properties)
                else:
                    base = existing.get(node.id)
                merged_by_id[node.id] = self._merge_node(node, base)
            merged = list(merged_by_id.values())

            # Remove stale FTS index rows for the node ids we are about to
            # (re)insert.  With the rowid-keyed external-content schema this is
            # O(1) per row instead of the super-linear scan the old
            # ``node_id IN (...)`` delete performed.
            for chunk, placeholders in self._chunked(node_ids):
                cursor = await db.execute(
                    f"""
                    SELECT rowid FROM fts_node_content
                    WHERE graph_id = ? AND node_id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )
                rowids = [r["rowid"] for r in await cursor.fetchall()]
                for rchunk, rplaceholders in self._chunked([str(r) for r in rowids]):
                    await db.execute(
                        f"DELETE FROM node_fts WHERE rowid IN ({rplaceholders})",
                        rchunk,
                    )

            node_rows = [
                (
                    node.id,
                    graph_id,
                    json.dumps(node.labels),
                    json.dumps(node.properties),
                    node.properties.get("file_path"),
                )
                for node in merged
            ]
            await db.executemany(
                """
                INSERT OR REPLACE INTO nodes (id, graph_id, labels, properties, file_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                node_rows,
            )

            # Update the external content table, then rebuild the FTS index rows
            # for this batch using the stable rowids.
            content_rows = [(self._build_fts_content(node), node.id, graph_id) for node in merged]
            await db.executemany(
                """
                INSERT OR REPLACE INTO fts_node_content
                (content, node_id, graph_id)
                VALUES (?, ?, ?)
                """,
                content_rows,
            )

            for chunk, placeholders in self._chunked(node_ids):
                cursor = await db.execute(
                    f"""
                    SELECT rowid, content, node_id
                    FROM fts_node_content
                    WHERE graph_id = ? AND node_id IN ({placeholders})
                    """,
                    (graph_id, *chunk),
                )
                fts_rows = [
                    (r["rowid"], r["content"], r["node_id"], graph_id)
                    for r in await cursor.fetchall()
                ]
                await db.executemany(
                    """
                    INSERT INTO node_fts(rowid, content, node_id, graph_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    fts_rows,
                )

            await db.commit()
        finally:
            await db.close()

    async def add_edges_batch(self, graph_id: str, edges: Sequence[CodeEdge]) -> None:
        """Insert edges (INSERT OR IGNORE to avoid duplicates)."""
        if not edges:
            return

        db = await self._connect()
        try:
            params = [
                (edge.source, edge.target, graph_id, edge.rel_type, json.dumps(edge.properties))
                for edge in edges
            ]
            await db.executemany(
                """
                INSERT OR IGNORE INTO edges
                (source, target, graph_id, rel_type, properties)
                VALUES (?, ?, ?, ?, ?)
                """,
                params,
            )
            await db.commit()
        finally:
            await db.close()

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a supported query.

        Supported queries:
        - "nodes" -> all nodes for graph_id
        - "edges" -> all edges for graph_id
        - "node_by_id" params={"node_id": str} -> specific node
        - "neighbors" params={"node_id": str} -> connected nodes
        - "incoming" params={"node_id": str} -> edges where target=node_id
        - "outgoing" params={"node_id": str} -> edges where source=node_id
        - "nodes_by_label" params={"label": str} -> nodes with label
        - "nodes_by_file" params={"file_path": str} -> nodes for file
        """
        params = params or {}
        db = await self._connect()
        try:
            if query == "nodes":
                cursor = await db.execute(
                    "SELECT id, labels, properties FROM nodes WHERE graph_id = ?",
                    (graph_id,),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "n": {
                            "id": r["id"],
                            "labels": json.loads(r["labels"] or "[]"),
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "edges":
                cursor = await db.execute(
                    "SELECT source, target, rel_type, properties FROM edges WHERE graph_id = ?",
                    (graph_id,),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "r": {
                            "source": r["source"],
                            "target": r["target"],
                            "rel_type": r["rel_type"],
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "node_by_id":
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    "SELECT id, labels, properties FROM nodes WHERE graph_id = ? AND id = ?",
                    (graph_id, node_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    return []
                return [
                    {
                        "n": {
                            "id": row["id"],
                            "labels": json.loads(row["labels"] or "[]"),
                            "properties": json.loads(row["properties"] or "{}"),
                        }
                    }
                ]

            if query == "neighbors":
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    """
                    SELECT DISTINCT n.id, n.labels, n.properties
                    FROM nodes n
                    JOIN edges e ON (
                        (e.source = ? AND e.target = n.id)
                        OR (e.target = ? AND e.source = n.id)
                    )
                    WHERE n.graph_id = ? AND e.graph_id = ?
                    """,
                    (node_id, node_id, graph_id, graph_id),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "n": {
                            "id": r["id"],
                            "labels": json.loads(r["labels"] or "[]"),
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "incoming":
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    """
                    SELECT source, target, rel_type, properties FROM edges
                    WHERE graph_id = ? AND target = ?
                    """,
                    (graph_id, node_id),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "r": {
                            "source": r["source"],
                            "target": r["target"],
                            "rel_type": r["rel_type"],
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "outgoing":
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    """
                    SELECT source, target, rel_type, properties FROM edges
                    WHERE graph_id = ? AND source = ?
                    """,
                    (graph_id, node_id),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "r": {
                            "source": r["source"],
                            "target": r["target"],
                            "rel_type": r["rel_type"],
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "nodes_by_label":
                label = params.get("label", "")
                cursor = await db.execute(
                    """
                    SELECT id, labels, properties FROM nodes
                    WHERE graph_id = ? AND labels LIKE ?
                    """,
                    (graph_id, f'%"{label}"%'),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "n": {
                            "id": r["id"],
                            "labels": json.loads(r["labels"] or "[]"),
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "nodes_by_file":
                file_path = params.get("file_path", "")
                cursor = await db.execute(
                    """
                    SELECT id, labels, properties FROM nodes
                    WHERE graph_id = ? AND file_path = ?
                    """,
                    (graph_id, file_path),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "n": {
                            "id": r["id"],
                            "labels": json.loads(r["labels"] or "[]"),
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "stored_file_paths":
                """Return all file paths stored in file_hashes for a graph."""
                cursor = await db.execute(
                    "SELECT file_path FROM file_hashes WHERE graph_id = ?",
                    (graph_id,),
                )
                rows = await cursor.fetchall()
                return [{"file_path": r["file_path"]} for r in rows]

            if query == "node_by_name":
                name = params.get("name", "")
                cursor = await db.execute(
                    """
                    SELECT id, labels, properties FROM nodes
                    WHERE graph_id = ? AND json_extract(properties, '$.name') = ?
                    """,
                    (graph_id, name),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "n": {
                            "id": r["id"],
                            "labels": json.loads(r["labels"] or "[]"),
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "node_name_fuzzy":
                name = params.get("name", "")
                pattern = f"%{name}%"
                cursor = await db.execute(
                    """
                    SELECT id, labels, properties FROM nodes
                    WHERE graph_id = ?
                      AND (
                          id LIKE ?
                          OR json_extract(properties, '$.name') LIKE ?
                          OR json_extract(properties, '$.qualname') LIKE ?
                      )
                    """,
                    (graph_id, pattern, pattern, pattern),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "n": {
                            "id": r["id"],
                            "labels": json.loads(r["labels"] or "[]"),
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "node_edges":
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    """
                    SELECT source, target, rel_type, properties FROM edges
                    WHERE graph_id = ? AND (source = ? OR target = ?)
                    """,
                    (graph_id, node_id, node_id),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "r": {
                            "source": r["source"],
                            "target": r["target"],
                            "rel_type": r["rel_type"],
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "node_outgoing_edges":
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    """
                    SELECT source, target, rel_type, properties FROM edges
                    WHERE graph_id = ? AND source = ?
                    """,
                    (graph_id, node_id),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "r": {
                            "source": r["source"],
                            "target": r["target"],
                            "rel_type": r["rel_type"],
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "node_incoming_edges":
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    """
                    SELECT source, target, rel_type, properties FROM edges
                    WHERE graph_id = ? AND target = ?
                    """,
                    (graph_id, node_id),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "r": {
                            "source": r["source"],
                            "target": r["target"],
                            "rel_type": r["rel_type"],
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "node_all_edges":
                # Inline to avoid re-acquiring the store lock recursively.
                node_id = params.get("node_id", "")
                cursor = await db.execute(
                    """
                    SELECT source, target, rel_type, properties FROM edges
                    WHERE graph_id = ? AND (source = ? OR target = ?)
                    """,
                    (graph_id, node_id, node_id),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "r": {
                            "source": r["source"],
                            "target": r["target"],
                            "rel_type": r["rel_type"],
                            "properties": json.loads(r["properties"] or "{}"),
                        }
                    }
                    for r in rows
                ]

            if query == "index_metadata":
                cursor = await db.execute(
                    """
                    SELECT repo_path, last_indexed, file_count, sync_enabled
                    FROM graphs WHERE graph_id = ?
                    """,
                    (graph_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return []
                return [
                    {
                        "repo_path": row["repo_path"],
                        "last_indexed": row["last_indexed"],
                        "file_count": row["file_count"] or 0,
                        "sync_enabled": bool(row["sync_enabled"]),
                    }
                ]

            if query == "set_index_metadata":
                repo_path = params.get("repo_path", "")
                last_indexed = params.get("last_indexed") or datetime.now(UTC).isoformat()
                file_count = params.get("file_count", 0)
                sync_enabled = 1 if params.get("sync_enabled") else 0
                await db.execute(
                    """
                    INSERT OR REPLACE INTO graphs
                    (graph_id, repo_path, created_at, last_indexed, file_count, sync_enabled)
                    VALUES (
                        ?,
                        ?,
                        COALESCE((SELECT created_at FROM graphs WHERE graph_id = ?), ?),
                        ?,
                        ?,
                        ?
                    )
                    """,
                    (
                        graph_id,
                        repo_path,
                        graph_id,
                        last_indexed,
                        last_indexed,
                        file_count,
                        sync_enabled,
                    ),
                )
                await db.commit()
                return []

            if query == "count_files":
                cursor = await db.execute(
                    """
                    SELECT COUNT(DISTINCT file_path) AS count
                    FROM nodes WHERE graph_id = ?
                    """,
                    (graph_id,),
                )
                row = await cursor.fetchone()
                return [{"count": row["count"] if row else 0}]

            if query == "file_hashes":
                cursor = await db.execute(
                    "SELECT file_path, content_hash FROM file_hashes WHERE graph_id = ?",
                    (graph_id,),
                )
                rows = await cursor.fetchall()
                return [
                    {"file_path": r["file_path"], "content_hash": r["content_hash"]} for r in rows
                ]

            if query == "dirty_files":
                current_hashes: dict[str, str] = params.get("current_hashes", {})
                cursor = await db.execute(
                    "SELECT file_path, content_hash FROM file_hashes WHERE graph_id = ?",
                    (graph_id,),
                )
                rows = await cursor.fetchall()
                return [
                    {"file_path": r["file_path"]}
                    for r in rows
                    if current_hashes.get(r["file_path"]) != r["content_hash"]
                ]

            # Unknown query
            return []
        finally:
            await db.close()

    async def get_stored_hash(self, graph_id: str, file_path: str) -> str | None:
        """Return the last indexed content hash for a file."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT content_hash FROM file_hashes WHERE graph_id = ? AND file_path = ?",
                (graph_id, file_path),
            )
            row = await cursor.fetchone()
            return row["content_hash"] if row else None
        finally:
            await db.close()

    async def update_hash(self, graph_id: str, file_path: str, content_hash: str) -> None:
        """Upsert the content hash for a file."""
        from datetime import datetime

        db = await self._connect()
        try:
            now = datetime.now(UTC).isoformat()
            await db.execute(
                """
                INSERT OR REPLACE INTO file_hashes (graph_id, file_path, content_hash, indexed_at)
                VALUES (?, ?, ?, ?)
                """,
                (graph_id, file_path, content_hash, now),
            )
            await db.commit()
        finally:
            await db.close()

    # ==================================================================
    # SearchableGraphStore interface
    # ==================================================================

    async def upsert_embeddings(
        self,
        graph_id: str,
        rows: Sequence[EmbeddingPayload],
    ) -> None:
        """Store embedding vectors for nodes.

        Stores as numpy float32 bytes in the embeddings table.  If sqlite-vec
        is available, also upserts into the vec0 virtual table.
        """
        if not rows:
            return

        db = await self._connect()
        try:
            for row in rows:
                if row.embedding is None:
                    continue

                vec = np.array(row.embedding, dtype=np.float32)
                vec_blob = vec.tobytes()
                dimensions = len(row.embedding)

                await db.execute(
                    """
                    INSERT OR REPLACE INTO embeddings
                    (graph_id, node_id, model, vector, dimensions)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (graph_id, row.node_id, row.model, vec_blob, dimensions),
                )

                # Also upsert into sqlite-vec virtual table if available
                if self._has_sqlite_vec and vec.size > 0:
                    # Convert float32 array to the format sqlite-vec expects
                    # sqlite-vec expects a JSON array of floats for vec0
                    vec_json = json.dumps(row.embedding)
                    try:
                        await db.execute(
                            f"""
                            INSERT OR REPLACE INTO {VEC_TABLE} (node_id, graph_id, embedding)
                            VALUES (?, ?, ?)
                            """,
                            (row.node_id, graph_id, vec_json),
                        )
                    except Exception as exc:
                        logger.warning(
                            "sqlite-vec upsert failed for %s (disabling vec search): %s",
                            row.node_id,
                            exc,
                        )
                        self._has_sqlite_vec = False

            await db.commit()
        finally:
            await db.close()

    async def search_vector(
        self,
        graph_id: str,
        vector: Sequence[float],
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by vector similarity.

        Uses sqlite-vec vec0 virtual table when available; otherwise falls
        back to brute-force cosine similarity in Python.
        """
        query_vec = np.array(vector, dtype=np.float32)

        if self._has_sqlite_vec:
            return await self._search_vector_vec(graph_id, query_vec, limit)
        return await self._search_vector_bruteforce(graph_id, query_vec, limit)

    async def _search_vector_vec(
        self, graph_id: str, query_vec: np.ndarray, limit: int
    ) -> list[SearchHit]:
        """Vector search using sqlite-vec vec0 virtual table."""
        db = await self._connect()
        try:
            vec_json = json.dumps(query_vec.tolist())
            cursor = await db.execute(
                f"""
                SELECT v.node_id, distance
                FROM {VEC_TABLE} AS v
                WHERE v.graph_id = ?
                  AND v.embedding MATCH ?
                ORDER BY distance ASC
                LIMIT ?
                """,
                (graph_id, vec_json, limit),
            )
            rows = await cursor.fetchall()

            hits: list[SearchHit] = []
            for row in rows:
                score = 1.0 / (1.0 + row["distance"])
                hits.append(SearchHit(node_id=row["node_id"], score=score))
            return hits
        except Exception as exc:
            logger.warning("sqlite-vec search failed, falling back: %s", exc)
            self._has_sqlite_vec = False
        finally:
            await db.close()
        return await self._search_vector_bruteforce(graph_id, query_vec, limit)

    async def _search_vector_bruteforce(
        self, graph_id: str, query_vec: np.ndarray, limit: int
    ) -> list[SearchHit]:
        """Brute-force cosine similarity in Python."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                """
                SELECT node_id, vector FROM embeddings WHERE graph_id = ?
                """,
                (graph_id,),
            )
            rows = await cursor.fetchall()

            if not rows:
                return []

            query_norm = np.linalg.norm(query_vec)
            if query_norm == 0:
                return []

            similarities: list[tuple[str, float]] = []
            for row in rows:
                node_id = row["node_id"]
                vec_blob = row["vector"]
                if vec_blob is None:
                    continue
                stored_vec = np.frombuffer(vec_blob, dtype=np.float32)
                if stored_vec.size == 0:
                    continue
                stored_norm = np.linalg.norm(stored_vec)
                if stored_norm == 0:
                    continue
                # Cosine similarity = dot product / (norms product)
                dot = float(np.dot(query_vec, stored_vec))
                sim = float(dot / (query_norm * stored_norm))
                similarities.append((str(node_id), sim))

            # Sort by similarity descending
            similarities.sort(key=lambda x: x[1], reverse=True)

            return [
                SearchHit(node_id=node_id, score=score) for node_id, score in similarities[:limit]
            ]
        finally:
            await db.close()

    async def search_keyword(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
    ) -> list[SearchHit]:
        """Find nodes by keyword/phrase matching via FTS5 (or LIKE fallback)."""
        try:
            return await self._search_keyword_fts(graph_id, query, limit)
        except Exception as exc:
            logger.warning("FTS search failed, falling back to LIKE: %s", exc)
            return await self._search_keyword_like(graph_id, query, limit)

    async def _search_keyword_fts(self, graph_id: str, query: str, limit: int) -> list[SearchHit]:
        """Keyword search via FTS5."""
        db = await self._connect()
        try:
            # FTS5 query: escape quotes and use AND for multi-word
            escaped = query.replace('"', '""')
            fts_query = f'"{escaped}"'

            cursor = await db.execute(
                """
                SELECT c.node_id, node_fts.rank
                FROM node_fts
                JOIN fts_node_content c ON node_fts.rowid = c.rowid
                WHERE node_fts MATCH ? AND c.graph_id = ?
                ORDER BY node_fts.rank ASC
                LIMIT ?
                """,
                (fts_query, graph_id, limit),
            )
            rows = await cursor.fetchall()

            return [
                SearchHit(node_id=row["node_id"], score=1.0 / (1.0 + abs(row["rank"])))
                for row in rows
            ]
        finally:
            await db.close()

    async def _search_keyword_like(self, graph_id: str, query: str, limit: int) -> list[SearchHit]:
        """Fallback keyword search using LIKE on node properties."""
        db = await self._connect()
        try:
            pattern = f"%{query}%"
            cursor = await db.execute(
                """
                SELECT DISTINCT n.id
                FROM nodes n
                WHERE n.graph_id = ?
                  AND (n.id LIKE ? OR n.properties LIKE ? OR n.labels LIKE ?)
                LIMIT ?
                """,
                (graph_id, pattern, pattern, pattern, limit),
            )
            rows = await cursor.fetchall()

            # Simple scoring: exact matches score higher
            hits: list[SearchHit] = []
            for row in rows:
                node_id = row["id"]
                score = 1.0 if query.lower() in node_id.lower() else 0.5
                hits.append(SearchHit(node_id=node_id, score=score))
            return hits
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Snippets
    # ------------------------------------------------------------------

    async def upsert_snippet(self, graph_id: str, node_id: str, snippet: str) -> None:
        """Store or update a source snippet for a node."""
        db = await self._connect()
        try:
            await db.execute(
                """
                INSERT OR REPLACE INTO snippets (graph_id, node_id, snippet)
                VALUES (?, ?, ?)
                """,
                (graph_id, node_id, snippet),
            )
            await db.commit()
        finally:
            await db.close()

    async def get_snippet(self, graph_id: str, node_id: str) -> str | None:
        """Retrieve a stored snippet for a node."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT snippet FROM snippets WHERE graph_id = ? AND node_id = ?",
                (graph_id, node_id),
            )
            row = await cursor.fetchone()
            return row["snippet"] if row else None
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Communities
    # ------------------------------------------------------------------

    async def set_communities(self, graph_id: str, assignments: dict[str, int]) -> None:
        """Store community assignments for nodes (UPSERT)."""
        if not assignments:
            return

        db = await self._connect()
        try:
            for node_id, community_id in assignments.items():
                await db.execute(
                    """
                    INSERT OR REPLACE INTO communities (graph_id, node_id, community_id)
                    VALUES (?, ?, ?)
                    """,
                    (graph_id, node_id, community_id),
                )
            await db.commit()
        finally:
            await db.close()

    async def get_communities(self, graph_id: str) -> dict[int, list[str]]:
        """Get all community memberships as community_id -> [node_ids]."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT node_id, community_id FROM communities WHERE graph_id = ?",
                (graph_id,),
            )
            rows = await cursor.fetchall()

            result: dict[int, list[str]] = {}
            for row in rows:
                cid = row["community_id"]
                result.setdefault(cid, []).append(row["node_id"])
            return result
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Graph metadata helpers
    # ------------------------------------------------------------------

    async def register_project(
        self,
        graph_id: str,
        repo_path: str | Path,
        file_count: int | None = None,
        sync_enabled: bool = False,
    ) -> None:
        """Register or update graph metadata."""
        from datetime import datetime

        db = await self._connect()
        try:
            now = datetime.now(UTC).isoformat()
            resolved_path = str(Path(repo_path).resolve())
            sync_val = 1 if sync_enabled else 0

            # Preserve created_at for existing records
            cursor = await db.execute(
                "SELECT created_at FROM graphs WHERE graph_id = ?", (graph_id,)
            )
            row = await cursor.fetchone()
            created_at = row["created_at"] if row else now

            await db.execute(
                """
                INSERT OR REPLACE INTO graphs
                (graph_id, repo_path, created_at, last_indexed, file_count, sync_enabled)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    resolved_path,
                    created_at,
                    now,
                    file_count if file_count is not None else 0,
                    sync_val,
                ),
            )
            await db.commit()
        finally:
            await db.close()

    async def list_projects(self) -> list[dict[str, Any]]:
        """List all registered graphs."""
        db = await self._connect()
        try:
            cursor = await db.execute(
                "SELECT graph_id, repo_path, created_at, last_indexed, file_count, sync_enabled FROM graphs"
            )
            rows = await cursor.fetchall()
            return [
                {
                    "graph_id": r["graph_id"],
                    "repo_path": r["repo_path"],
                    "sync_enabled": bool(r["sync_enabled"]),
                    "created_at": r["created_at"],
                    "last_indexed": r["last_indexed"],
                    "file_count": r["file_count"] or 0,
                }
                for r in rows
            ]
        finally:
            await db.close()
