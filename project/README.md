# Ariadne Graph

> A thread through the labyrinth of your codebase.

An MCP server that turns a Python/TypeScript repository into a queryable code knowledge graph, then exposes it to AI agents (Claude Code, Cursor, Windsurf, …) and a CLI. Unlike the many Tree-sitter-only graph servers, Ariadne uses **compiler-accurate symbol resolution** for TypeScript — and gracefully reconciles it with Tree-sitter when the compiler isn't available.

## Why Ariadne (vs. other code-graph MCP servers)

Most code-graph MCP servers build the graph purely from **Tree-sitter** ASTs. Tree-sitter is fast and incremental, but its call/inheritance edges are *syntactic heuristics* — it guesses what `foo()` resolves to from shape alone, and gets re-exports, overloads, and aliased imports wrong.

Ariadne is built around a **two-layer indexer**:

| Layer | Source | Gives you |
|-------|--------|-----------|
| **Precise** | [SCIP](https://scip-code.org/) via `scip-typescript` (the TypeScript compiler) | Compiler-accurate calls, references, inheritance, and types |
| **Structural** | Tree-sitter | Always-available fallback + enrichment (complexity, React/decorator labels, call-position ranges, snippets) |

The hard part isn't running both — it's making them agree. When some files are SCIP-indexed (symbol-string IDs) and others fall back to Tree-sitter (module-based IDs), edges that cross the boundary must still resolve to the *same* node. Ariadne reconciles these **mixed-ID boundaries** at retrieval time, so a trace from a compiler-indexed file reaches a fallback-indexed callee instead of dead-ending on a dangling stub. This boundary handling is covered by a live end-to-end test that doubles as a regression tripwire.

Everything degrades gracefully: no `scip-typescript` → Tree-sitter only; no `tree-sitter` → stub nodes + diagnostics; no embeddings → keyword search. Optional features report availability through `code_graph_capabilities`.

## Features

- **Two-layer code graph**: compiler-accurate (SCIP) + Tree-sitter for TypeScript/TSX; full AST extraction for Python (Pydantic, FastAPI, dataclass, enum detection)
- **Hybrid search**: semantic vector search + keyword search (FTS5/BM25) fused with Reciprocal Rank Fusion, plus structural graph traversal
- **16 MCP tools**: index, query, analyze, and inspect code over the Model Context Protocol
- **Incremental sync**: XXH3 content-hash fingerprinting — only changed files are re-parsed
- **Architecture analysis**: Louvain community detection, complexity/coupling hotspots, fan-in/fan-out
- **Pluggable backends**: SQLite (zero-config local, FTS5 + optional `sqlite-vec`) or Neo4j (production)

## Quick Start

```bash
pip install -e ".[all]"

# Optional: compiler-accurate TypeScript indexing
npm install -g @sourcegraph/scip-typescript

ariadne index /path/to/repo
ariadne search /path/to/repo "authentication middleware"
ariadne architecture /path/to/repo
ariadne mcp            # start the MCP server (stdio transport)
```

Add to an MCP client (e.g. Claude Code):

```json
{
  "mcpServers": {
    "ariadne": { "command": "ariadne", "args": ["mcp"] }
  }
}
```

## MCP Tools (16)

### Indexing
| Tool | Description |
|------|-------------|
| `code_graph_index` | Index a repository (discover, parse changed files, store facts) |
| `code_graph_index_status` | Index freshness, file count, dirty files, capability report |
| `code_graph_list_projects` | List all indexed projects |
| `code_graph_delete_project` | Remove an indexed project and its graph |

### Query
| Tool | Description |
|------|-------------|
| `code_graph_retrieve` | Retrieve a symbol by ID/name with neighborhood + source snippet |
| `code_graph_search_semantic` | Natural-language code search (vector embeddings) |
| `code_graph_search_code` | Keyword/pattern search (FTS5 / BM25) |
| `code_graph_trace_dependencies` | Trace dependency chains upstream/downstream/both |

### Analysis
| Tool | Description |
|------|-------------|
| `code_graph_impact_analysis` | Predict the blast radius of changing a symbol |
| `code_graph_detect_changes` | Detect files changed since last index (git- or hash-aware) |
| `code_graph_find_hotspots` | Complexity, coupling, and fan-in/fan-out hotspots |
| `code_graph_get_architecture` | Architecture summary with community detection |
| `code_graph_list_communities` | List detected communities and members |

### Inspection & Diagnostics
| Tool | Description |
|------|-------------|
| `code_graph_inspect_file` | Full graph subgraph (nodes + edges) for a file |
| `code_graph_list_diagnostics` | Unused imports, missing types, complexity, long-parameter findings |
| `code_graph_capabilities` | Report which optional features (typescript/semantic/vector/neo4j) are available |

A Lumen-compatible `lumen_code_graph_retrieve` alias is also exposed when enabled.

## Architecture

```
ariadne_graph/
  core/              # models, config, discovery, retrieval, search, embeddings, communities
  languages/         # adapters: python_ast/, typescript/ (Tree-sitter + SCIP)
  graphstores/       # backends: memory, sqlite, neo4j, lumen
  mcp/               # MCP server, tools, schemas
  cli.py             # command-line interface
```

### Graph Schema

**16 node types**: CodeRepo, CodeFile, CodeModule, CodeClass, CodeFunction, CodeMethod, CodeVariable, CodeAttribute, CodeImport, CodeExport, CodeInterface, CodeTypeAlias, CodeRoute, CodeReactComponent, CodeHook, CodeDiagnostic

**13 relationship types**: CONTAINS, DEFINES, IMPORTS, IMPORTS_SYMBOL, EXPORTS, CALLS, INHERITS, OVERRIDES, DECORATED_BY, USES_TYPE, RETURNS_TYPE, ROUTES_TO, HAS_DIAGNOSTIC

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ARIADNE_DB` | `.ariadne/graph.db` | SQLite database path |
| `ARIADNE_NEO4J_URI` | `bolt://localhost:7687` | Neo4j URI (enables Neo4j backend) |
| `ARIADNE_NEO4J_USER` | `neo4j` | Neo4j username |
| `ARIADNE_NEO4J_PASSWORD` | `password` | Neo4j password |
| `ARIADNE_EMBEDDING_PROVIDER` | `local` | Embedding provider for semantic search |
| `ARIADNE_SCIP_TYPESCRIPT_ENABLED` | unset | `true`/`false` to force SCIP indexing on/off |
| `ARIADNE_SCIP_TYPESCRIPT_PATH` | unset | Path to `scip-typescript` binary or `npx` |
| `ARIADNE_SCIP_TYPESCRIPT_ARGS` | unset | Comma-separated extra args for `scip-typescript index` |
| `ARIADNE_SCIP_TYPESCRIPT_INFER_TSCONFIG` | unset | `true`/`false` to force `--infer-tsconfig` |

### Optional extras

`pip install -e ".[typescript]"` (Tree-sitter + protobuf for SCIP), `".[semantic]"` (sentence-transformers + torch), `".[vector]"` (sqlite-vec), `".[neo4j]"`, or `".[all]"`.

> **Note:** SCIP parsing requires `protobuf>=6.30` — the vendored bindings embed a gen-runtime gate from `scip-typescript`'s protoc version. This is pinned in the `typescript`/`all` extras.

## Development

```bash
pip install -e ".[all]"
pytest -q              # 219 passed, 13 skipped (SCIP integration tests skip without scip-typescript)
ruff check src/
mypy src/
```

## Project Stats

- ~13,000 lines of Python across 42 source files
- 232 tests (Python AST, TypeScript/SCIP, retrieval, graph stores, MCP handlers, CLI)
- 16 MCP tools across 4 categories
- 4 graph backends: Memory, SQLite, Neo4j, Lumen
- 2 language adapters: Python (AST), TypeScript (Tree-sitter + optional SCIP)

## License

MIT
