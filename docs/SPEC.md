# SPEC.md — Ariadne Graph

## 1. Overview

A standalone MCP server for code graph analysis. Extracts graph facts from Python and TypeScript repos, persists to SQLite (local) or Neo4j (production), and exposes 16 canonical MCP tools (plus a Lumen compatibility alias) for indexing, querying, analysis, code inspection, and diagnostics.

## 2. Project Structure

```
ariadne_graph/
  src/ariadne_graph/
    __init__.py
    core/
      __init__.py
      models.py          # CodeNode, CodeEdge, CodeGraphDelta, etc.
      config.py          # AnalyzerConfig
      discovery.py       # File discovery and ignore rules
      retrieval.py       # Graph neighborhood retrieval
      snippets.py        # Source snippet extraction
      freshness.py       # Freshness manifest management
      incremental_sync.py # Content-hash incremental sync
      search.py          # Hybrid search (semantic + keyword + graph)
      embeddings.py      # Embedding provider abstraction
      communities.py     # Louvain community detection
      diagnostics.py     # Audit/diagnostic interface
    languages/
      __init__.py
      base.py            # LanguageAdapter protocol
      python_ast/
        __init__.py
        adapter.py       # PythonLanguageAdapter
        extractor.py     # AST visitor for fact extraction
      typescript/
        __init__.py
        adapter.py       # TypeScriptLanguageAdapter (Tree-sitter)
    graphstores/
      __init__.py
      base.py            # GraphStore + SearchableGraphStore protocols
      memory.py          # In-memory graph store
      sqlite.py          # SQLite graph store (default local)
      neo4j.py           # Neo4j graph store (production)
      lumen.py           # Optional Lumen KG compatibility wrapper
    mcp/
      __init__.py
      server.py          # MCP server (FastMCP)
      tools.py           # All 16 canonical tool handlers + Lumen alias
      schemas.py         # Input/output schemas per tool
    cli.py               # CLI entry point
  tests/
    fixtures/
    test_core/
    test_python/
    test_typescript/
    test_graphstores/
    test_mcp/
    test_acceptance/
  pyproject.toml
```

## 3. Data Models (core/models.py)

### CodeNode
```python
class CodeNode(BaseModel):
    id: str                    # Deterministic ID (e.g., "module.Class.method")
    graph_id: str              # Repository graph identifier
    labels: list[str]          # ["KnowledgeNode", "CodeFunction"]
    properties: dict[str, Any] # name, file_path, module, qualname, line_start, line_end, parser_version, content_hash, source_commit, first_seen_at, last_modified_at, community_id
```

### CodeEdge
```python
class CodeEdge(BaseModel):
    source: str                # Source node id
    target: str                # Target node id
    graph_id: str              # Repository graph identifier
    rel_type: str              # "IMPORTS", "CALLS", "INHERITS", etc.
    properties: dict[str, Any] # owner_file_path, source_commit, etc.
```

### CodeGraphDelta
```python
class CodeGraphDelta(BaseModel):
    graph_id: str
    file_path: str
    nodes: list[CodeNode]
    edges: list[CodeEdge]
    content_hash: str          # XXH3 of file content
    parser_version: str
```

### EmbeddingPayload
```python
class EmbeddingPayload(BaseModel):
    node_id: str
    graph_id: str
    text: str                  # Text representation for embedding
    embedding: list[float] | None
```

### SearchHit
```python
class SearchHit(BaseModel):
    node_id: str
    score: float
    node: CodeNode | None = None
```

## 4. GraphStore Protocol (graphstores/base.py)

### GraphStore (minimum interface)
```python
class GraphStore(Protocol):
    async def delete_graph(self, graph_id: str) -> None: ...
    async def delete_file_facts(self, graph_id: str, file_path: str) -> None: ...
    async def add_nodes_batch(self, graph_id: str, nodes: Sequence[CodeNode]) -> None: ...
    async def add_edges_batch(self, graph_id: str, edges: Sequence[CodeEdge]) -> None: ...
    async def query(self, graph_id: str, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    async def get_stored_hash(self, graph_id: str, file_path: str) -> str | None: ...
    async def update_hash(self, graph_id: str, file_path: str, content_hash: str) -> None: ...
```

### SearchableGraphStore (optional extension)
```python
class SearchableGraphStore(GraphStore, Protocol):
    async def upsert_embeddings(self, graph_id: str, rows: Sequence[EmbeddingPayload]) -> None: ...
    async def search_vector(self, graph_id: str, vector: Sequence[float], limit: int) -> list[SearchHit]: ...
    async def search_keyword(self, graph_id: str, query: str, limit: int) -> list[SearchHit]: ...
    async def set_communities(self, graph_id: str, assignments: dict[str, int]) -> None: ...
    async def get_communities(self, graph_id: str) -> dict[int, list[str]]: ...
```

## 5. LanguageAdapter Protocol (languages/base.py)

```python
class LanguageAdapter(Protocol):
    language: str
    parser_version: str
    extensions: tuple[str, ...]

    def discover_files(self, root: Path, config: AnalyzerConfig) -> list[Path]: ...
    def extract_file(self, path: Path, context: ExtractionContext) -> CodeGraphDelta: ...
```

```python
class ExtractionContext(BaseModel):
    graph_id: str
    repo_root: Path
    source_commit: str | None = None
```

## 6. Config (core/config.py)

```python
class AnalyzerConfig(BaseModel):
    repo_root: Path
    graph_id: str | None = None
    ignore_patterns: list[str] = [".git", "__pycache__", "*.pyc", "node_modules", ".venv", "venv"]
    python_paths: list[str] = []
    max_file_size: int = 1_000_000  # bytes
    embedding_provider: str = "local"  # local, openai, ollama
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimensions: int = 384
    incremental_sync_interval: int = 30  # seconds
    auto_sync: bool = False
```

## 7. MCP Tools (16 canonical tools + 1 Lumen alias)

### Indexing (4 tools)
- `code_graph_index(repo_path, force_rebuild=False)` -> status, files_indexed
- `code_graph_index_status(repo_path)` -> last_indexed, file_count, dirty_files
- `code_graph_list_projects()` -> project_ids, repo_paths
- `code_graph_delete_project(repo_path)` -> deleted

### Capabilities (1 tool)
- `code_graph_capabilities()` -> runtime availability report for optional extras

### Query (4 tools)
- `code_graph_retrieve(query: str, graph_id=None)` -> node + edges + snippets
- `code_graph_search_semantic(query_text, repo_path=None, limit=10, types=[])` -> ranked entities
- `code_graph_search_code(pattern, repo_path=None, language=None, limit=10)` -> matching snippets
- `code_graph_trace_dependencies(symbol, direction="both", max_depth=3, graph_id=None)` -> path list

### Analysis (5 tools)
- `code_graph_impact_analysis(symbol, graph_id=None)` -> transitive closure ranked by coupling
- `code_graph_detect_changes(repo_path, since_ref=None)` -> added, modified, deleted entities
- `code_graph_find_hotspots(repo_path, top_n=10, metric="complexity")` -> ranked hotspots
- `code_graph_get_architecture(repo_path)` -> community summary
- `code_graph_list_communities(repo_path, community_id=None)` -> community members

### Code (1 tool)
- `code_graph_inspect_file(file_path, graph_id=None)` -> graph subgraph for file

### Diagnostics (1 tool)
- `code_graph_list_diagnostics(repo_path, level=None, rule=None, file_path=None, limit=100)` -> diagnostic entries

### Lumen compatibility alias
- `lumen_code_graph_retrieve(query, graph_id=None, repo_path=None)` -> Lumen-style retrieve response

## 8. Dependencies

```toml
[project]
name = "ariadne-graph"
version = "0.1.0"
dependencies = [
    "mcp>=1.0.0",
    "pydantic>=2.0",
    "xxhash>=3.0",
    "aiofiles>=23.0",
    "aiosqlite>=0.20",
    "networkx>=3.0",
    "python-louvain>=0.16",
    "numpy>=1.24",
]

[project.optional-dependencies]
semantic = ["sentence-transformers>=2.0", "torch>=2.0"]
neo4j = ["neo4j>=5.0"]
vector = ["sqlite-vec>=0.1"]
typescript = ["tree-sitter>=0.23", "tree-sitter-typescript>=0.23", "protobuf>=6.30"]
dev = ["pytest>=7.0", "pytest-asyncio>=0.21", "ruff>=0.1.0", "mypy>=1.0"]
```

## 9. Notes

- `core/facts.py` is listed in the original architecture as a language-agnostic
  fact-processing module but is **not currently implemented**. Extraction logic
  lives directly in the language adapters (`languages/python_ast/`,
  `languages/typescript/`).

## 10. Implementation Order

1. models.py + config.py (foundation)
2. graphstores/base.py + graphstores/memory.py
3. languages/base.py
4. core/discovery.py
5. languages/python_ast/* (full adapter)
6. graphstores/sqlite.py + SearchableGraphStore
7. core/incremental_sync.py
8. core/retrieval.py + core/snippets.py
9. core/embeddings.py + core/search.py
10. core/communities.py
11. mcp/schemas.py + mcp/tools.py + mcp/server.py
12. cli.py
