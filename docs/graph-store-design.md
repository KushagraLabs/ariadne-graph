# Graph Store Design

## Decision

Use a dual-backend strategy for the standalone repository:

- SQLite is the default local development backend because it has no service
  dependency.
- Neo4j is the shared or production backend when native graph traversal,
  operations support, and team-scale persistence matter.
- Do not copy the full Lumen KnowledgeGraphEngine.

The current analyzer only needs a small graph-store surface:

```python
class GraphStore(Protocol):
    async def delete_graph(self, graph_id: str) -> None: ...
    async def delete_file_facts(self, graph_id: str, file_path: str) -> None: ...
    async def add_nodes_batch(self, graph_id: str, nodes: Sequence[CodeNodePayload]) -> None: ...
    async def add_edges_batch(self, graph_id: str, edges: Sequence[CodeEdgePayload]) -> None: ...
    async def query(self, graph_id: str, query: str, params: Mapping[str, Any] | None = None) -> list[Mapping[str, Any]]: ...
    async def get_stored_hash(self, graph_id: str, file_path: str) -> str | None: ...
    async def update_hash(self, graph_id: str, file_path: str, content_hash: str) -> None: ...
```

Search-capable stores should also implement an optional vector and keyword
surface. Keep this separate from the minimum graph protocol so tests and simple
in-memory stores can remain small.

```python
class SearchableGraphStore(GraphStore, Protocol):
    async def upsert_embeddings(self, graph_id: str, rows: Sequence[EmbeddingPayload]) -> None: ...
    async def search_vector(self, graph_id: str, vector: Sequence[float], limit: int) -> list[SearchHit]: ...
    async def search_keyword(self, graph_id: str, query: str, limit: int) -> list[SearchHit]: ...
```

## Required Neo4j Conventions

Each persisted node should have:

- `id`
- `_graph_id`
- one generic lookup label, proposed: `KnowledgeNode`
- one or more semantic labels, such as `CodeFile`, `CodeModule`, `CodeFunction`
- analyzer properties such as `file_path`, `module`, `qualname`, `line_start`,
  `line_end`, `parser_version`
- optional freshness and temporal properties such as `content_hash`,
  `first_seen_at`, `last_modified_at`, and `source_commit`
- optional `community_id` and architecture-summary properties

Each persisted relationship should have:

- relationship type matching the code graph relationship, such as `IMPORTS`
- analyzer properties, including `owner_file_path` where available
- optional freshness and temporal properties matching node metadata where useful

## SQLite Store

SQLite should be implemented as a first-class local backend, not only as a
throwaway cache.

Suggested tables:

- `graphs`: graph metadata
- `nodes`: node id, graph id, labels, file path, line range, properties JSON
- `edges`: source id, target id, graph id, relationship type, properties JSON
- `file_hashes`: graph id, file path, content hash, parser version, indexed time
- `snippets`: graph id, node id, source text or snippet metadata
- `embeddings`: graph id, node id, embedding model, embedding vector reference
- vector virtual table using sqlite-vec or an equivalent extension when present

Graph traversal can use adjacency-list queries and recursive CTEs. The expected
code-analysis depth is shallow enough for local workflows, while Neo4j remains
available for deeper traversal or shared deployments.

Keyword search should use SQLite FTS where available. Semantic search should use
sqlite-vec or a pluggable equivalent. If sqlite-vec is not installed, the store
may still operate without semantic search, but `code_graph_search_semantic`
should report the missing capability clearly.

## Constraint

```cypher
CREATE CONSTRAINT code_hygiene_node_identity IF NOT EXISTS
FOR (n:KnowledgeNode)
REQUIRE (n._graph_id, n.id) IS UNIQUE
```

## Batch Node Upsert

Expected pattern:

```cypher
UNWIND $rows AS row
MERGE (n:KnowledgeNode {id: row.id, _graph_id: row.graph_id})
SET n += row.properties
SET n:CodeFile
```

Because Neo4j labels cannot be parameterized directly, rows should be grouped by
semantic label before writing.

## Batch Edge Upsert

Expected pattern:

```cypher
UNWIND $rows AS row
MATCH (a:KnowledgeNode {id: row.source, _graph_id: $graph_id})
MATCH (b:KnowledgeNode {id: row.target, _graph_id: $graph_id})
MERGE (a)-[r:IMPORTS]->(b)
SET r += row.properties
```

Rows should be grouped by relationship type before writing.

## Retrieval Queries

The existing retrieval logic depends on:

- `MATCH (n {_graph_id: $graph_id})`
- `labels(n)`
- `properties(n)`
- `type(r)`
- relationship traversal within the same `_graph_id`

The direct Neo4j adapter should reproduce these semantics.

For semantic search, Neo4j should use native vector indexes when available. For
architecture summaries, Neo4j should use Graph Data Science Louvain or label
propagation when available; otherwise the service can compute communities in
Python and write `community_id` properties back to the graph.

## Store Implementations

Initial stores:

- `MemoryGraphStore`: tests and local smoke checks
- `SQLiteGraphStore`: default local persistent backend
- `Neo4jGraphStore`: shared or production persistent backend
- `LumenGraphStore`: optional adapter over current Lumen KG engine

JSON can remain useful for fixture snapshots, but it should not be the primary
local graph store once SQLite exists.

## Why Not Copy Lumen KG

Copying Lumen KG would bring in unrelated concerns:

- service container
- metadata service
- versioned fact store
- platform auth/API assumptions
- broader analytics integrations
- Lumen-specific lifecycle policies

The standalone analyzer needs only a compact persistence and query interface.
