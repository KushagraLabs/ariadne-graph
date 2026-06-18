# Architecture Plan — Ariadne Graph

## Purpose

Build a reusable code-graph MCP server that can be plugged into Python and TypeScript
repositories without requiring the Lumen platform runtime.

The current Lumen implementation is valuable and should remain untouched until
the standalone repo is at least as useful for Python analysis, persisted graph
retrieval, and agent-facing context generation.

## Design Principles

- Keep the analyzer repo-agnostic.
- Treat language parsing as an adapter, not the core identity.
- Treat graph persistence as a backend adapter.
- Preserve deterministic graph facts so parity can be tested.
- Prefer a dual-backend storage strategy: SQLite for zero-config local use and
  direct Neo4j for shared or production use.
- Make Lumen integration optional and reversible.
- Treat freshness as a correctness requirement. Interactive workflows need
  incremental sync, not only batch rebuilds.
- Support exact, semantic, keyword, and graph-neighborhood retrieval so agents
  can query by intent as well as by known symbol names.

## High-Level Shape

```text
ariadne_graph/
  src/ariadne_graph/
    core/
      config.py
      discovery.py
      retrieval.py
      snippets.py
      freshness.py
      incremental_sync.py
      search.py
      embeddings.py
      communities.py
      diagnostics.py
    languages/
      python_ast/
      typescript/
    graphstores/
      base.py
      memory.py
      sqlite.py
      neo4j.py
      lumen.py
    mcp/
      server.py
      tools.py
      schemas.py
    cli.py
  docs/
  tests/
```

## Core Responsibilities

> Note: `core/facts.py` is listed in early design notes as a language-agnostic
> fact-processing module but is **not currently implemented**. Extraction logic
> lives in the language adapters, and the core models in `core/models.py` define
> the common node/edge schema.

The core package owns:

- stable graph fact models
- repository root sandboxing
- file discovery and ignore rules
- source snippet extraction
- query term ranking
- semantic and keyword search ranking
- graph neighborhood formatting
- prompt context formatting
- freshness manifests
- content-hash based incremental sync
- architecture/community summaries
- diagnostics interface

The core package must not import:

- Lumen application modules
- Lumen DI containers
- Lumen KnowledgeGraphEngine
- FastAPI routers from application repos
- language-specific parser internals

## Language Adapter Contract

Each language adapter should expose a common contract:

```python
class LanguageAdapter(Protocol):
    language: str
    parser_version: str
    extensions: tuple[str, ...]

    def discover_files(self, root: Path, config: AnalyzerConfig) -> list[Path]: ...
    def extract_file(self, path: Path, context: ExtractionContext) -> CodeGraphDelta: ...
```

The Python AST adapter will be ported from the current implementation because it
preserves the Python-specific semantic depth needed for decorators, Pydantic,
FastAPI routes, type annotations, and diagnostics.

The TypeScript adapter should start with Tree-sitter TypeScript/TSX through
Python bindings. This avoids a mandatory Node helper process, handles incomplete
code during live editing, and creates a reusable path for future language
adapters. Type-aware refinement through the TypeScript compiler API, tsserver,
or another LSP pass can be added later behind the same adapter contract when
syntactic extraction is not accurate enough.

## Graph Schema

The graph schema should preserve the current high-value labels and
relationships:

Node labels:

- `CodeRepo`
- `CodeFile`
- `CodeModule`
- `CodeClass`
- `CodeFunction`
- `CodeMethod`
- `CodeVariable`
- `CodeAttribute`
- `CodeImport`
- `CodeExport`
- `CodeInterface`
- `CodeTypeAlias`
- `CodeReactComponent`
- `CodeHook`
- `CodeRoute`
- `CodeDiagnostic`

Relationships:

- `CONTAINS`
- `DEFINES`
- `IMPORTS`
- `IMPORTS_SYMBOL`
- `EXPORTS`
- `CALLS`
- `INHERITS`
- `OVERRIDES`
- `DECORATED_BY`
- `USES_TYPE`
- `RETURNS_TYPE`
- `ROUTES_TO`
- `HAS_DIAGNOSTIC`

The schema can include language-specific properties, but the top-level node and
edge contract should remain common.

## MCP Tools

The initial tool surface should be larger than basic retrieval because agents
need indexing, query, analysis, and code-inspection workflows without falling
back to file-by-file exploration.

- `code_graph_retrieve`
- `code_graph_index`
- `code_graph_index_status`
- `code_graph_list_projects`
- `code_graph_delete_project`
- `code_graph_capabilities`
- `code_graph_search_semantic`
- `code_graph_search_code`
- `code_graph_inspect_file`
- `code_graph_trace_dependencies`
- `code_graph_impact_analysis`
- `code_graph_detect_changes`
- `code_graph_find_hotspots`
- `code_graph_get_architecture`
- `code_graph_list_communities`
- `code_graph_list_diagnostics`

The Lumen-specific `lumen_code_graph_retrieve` compatibility alias is implemented
as an optional tool exposed when Lumen compatibility is enabled.

## Search Strategy

Retrieval should combine:

- graph traversal for exact structural relationships
- vector embeddings for natural-language and intent-based queries
- BM25 or equivalent keyword search for exact strings, endpoint paths, error
  messages, and symbol fragments
- result expansion that includes callers, callees, imports, type edges, source
  snippets, and diagnostics around the matched entity

Embedding generation should be provider-based so local, hosted, and offline
deployments can choose different embedding models without changing the MCP tool
contracts.

## Incremental Sync Strategy

The analyzer should maintain per-file content hashes and support both manual
indexing and automatic background synchronization.

Required behavior:

- compute fast non-cryptographic content hashes, preferably XXH3
- store the latest indexed hash per file in the active graph store
- re-parse and replace graph facts for changed files only
- delete graph facts for removed files
- expose dirty-file status through `code_graph_index_status`
- keep automatic polling configurable, with a default suitable for interactive
  local development

Timestamp-only freshness is not sufficient because branch switches and checkout
operations can make modification times misleading.

## Architecture Analysis

The graph should store community assignments and expose architecture summaries.
For Neo4j, prefer Neo4j Graph Data Science Louvain or label propagation. For
SQLite, use a Python library such as `igraph`, `networkx`, or
`python-louvain`.

The architecture summary should report major communities, representative files
or symbols, internal density, coupling between communities, and high fan-in or
high fan-out hotspots.

## Persistence Strategy

The standalone repo should support:

- in-memory graph for tests
- SQLite local graph as the default dev backend
- sqlite-vec or an equivalent vector index for local semantic search
- direct Neo4j graph for shared persistent retrieval
- Neo4j vector indexes where Neo4j is the active backend
- optional Lumen KG adapter for the existing platform

SQLite should be the default local backend because it has no service dependency.
Neo4j should remain the production or team-shared backend.

## Parity Strategy

The current Lumen implementation is the golden reference. Parity should be
measured by:

- graph payload snapshot comparison on fixture repos
- node and edge count deltas
- label and relationship coverage
- retrieval result quality for known queries
- prompt context shape
- source snippet correctness
- incremental indexing behavior
- semantic and keyword search quality
- impact analysis correctness for curated change scenarios
- architecture/community summary usefulness

No Lumen switch-over should happen until parity gates pass.
