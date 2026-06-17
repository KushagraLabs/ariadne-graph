# Migration Plan

## Objective

Develop the standalone Code Hygiene MCP without disturbing the current working
implementation in `lumen_ai`. Switch Lumen to the new implementation only after
parity is demonstrated.

## Phase 0: Planning

Deliverables:

- architecture plan
- graph store design
- Python parity checklist
- TypeScript adapter plan
- Lumen integration plan
- Linear project and execution issues
- remediation plan for review gaps: expanded MCP tools, semantic search,
  incremental sync, SQLite/vector storage, Tree-sitter TypeScript parsing, and
  architecture summaries

Exit criteria:

- scope is explicit
- migration gates are written
- reviewed gaps are mapped to milestones and acceptance gates
- no changes required in `lumen_ai`

## Phase 1: Standalone Repo Scaffold

Deliverables:

- Python package scaffold
- CLI entrypoint
- MCP server skeleton
- config loader
- test setup
- dev docs

Exit criteria:

- repo installs locally
- MCP server starts
- empty index-status tool works
- tests run in isolation

## Phase 2: Core Graph Model and Python Adapter

Deliverables:

- core graph fact model
- file discovery and ignore rules
- Python AST adapter ported from `lumen_ai`
- graph extraction CLI
- fixture tests copied or recreated from current behavior

Exit criteria:

- deterministic graph extraction passes fixtures
- Python labels and relationships match current implementation for critical
  fixtures
- no Lumen imports in core or Python adapter

## Phase 3: Graph Stores

Deliverables:

- `GraphStore` protocol
- in-memory graph store
- SQLite local graph store as the default development backend
- sqlite-vec or equivalent vector index support where available
- direct Neo4j graph store
- hash manifest operations for incremental sync
- graph lifecycle and query helpers

Exit criteria:

- graph payload persists and retrieves from SQLite without a service dependency
- graph payload persists and retrieves from Neo4j
- Cypher queries used by retrieval work without Lumen KG
- `_graph_id`, `id`, lookup label, semantic labels, and properties are stored
  consistently
- per-file hashes are stored and retrievable through every persistent backend

## Phase 4: Incremental Sync

Deliverables:

- XXH3 or equivalent fast content-hash change detector
- configurable polling watcher for interactive workflows
- manual re-index path for deterministic CI or one-shot runs
- file-level graph delta replacement for changed files
- deleted-file cleanup
- dirty-file reporting through status tools
- incremental refresh hooks for search indexes and community assignments

Exit criteria:

- changed files are re-indexed without rebuilding the whole graph
- removed files remove their nodes, edges, snippets, and embeddings
- status reports clean and dirty states accurately
- graph updates become visible to retrieval tools within the configured polling
  window
- full rebuild remains available and deterministic

## Phase 5: Retrieval, Search, and MCP Tool Expansion

Deliverables:

- local graph retrieval
- persisted Neo4j retrieval
- source snippets
- prompt context generation
- runtime-query diagnostics where portable
- 14-tool MCP surface across indexing, query, analysis, and code inspection
- semantic search over indexed entities
- keyword/code search for exact text and symbol fragments
- impact analysis and dependency tracing
- change detection against a stored snapshot or git ref
- hotspot analysis

Exit criteria:

- known `lumen_ai` queries return comparable matches
- natural-language search returns useful ranked entities for curated queries
- prompt context includes matches, neighborhoods, relationships, snippets,
  diagnostics, and probe suggestions where applicable
- source snippets are path-safe and line-accurate
- expanded MCP tool schemas are documented and covered by smoke tests
- analysis tools work against the Python fixtures and local SQLite backend

## Phase 6: Validate on External Python Repo

Target:

- `/Users/amitkumarsingh/Documents/enterprise_tabular_ad`

Deliverables:

- repo-specific config
- indexing smoke test
- retrieval smoke test
- semantic search smoke test
- impact-analysis smoke test
- issue log for missing semantics

Exit criteria:

- analyzer indexes the repo without special Lumen assumptions
- retrieval is useful for coding-agent navigation
- incremental sync remains correct during local edits

## Phase 7: TypeScript Adapter

Target:

- `/Users/amitkumarsingh/Documents/cosmic_lens`

Deliverables:

- Tree-sitter TypeScript/TSX parser adapter
- tsconfig-aware path alias resolver
- import/export graph
- functions/classes/components/hooks extraction
- route and framework hints where feasible
- source snippets and retrieval support
- documented gap list for type-aware edges that require a later LSP/compiler
  refinement pass

Exit criteria:

- analyzer indexes TS/TSX files
- import graph respects tsconfig path aliases
- React component and route queries are useful
- TypeScript support does not require a mandatory Node helper process
- syntactic extraction gaps are measured before adding TypeScript-native runtime
  dependencies

## Phase 8: Architecture Analysis and Community Detection

Deliverables:

- community assignment storage on graph nodes
- Louvain or label-propagation implementation for SQLite
- Neo4j Graph Data Science integration where available
- `code_graph_get_architecture`
- `code_graph_list_communities`
- community-aware hotspot and coupling summaries
- incremental refresh policy for affected communities after file changes

Exit criteria:

- architecture summaries identify major modules and their coupling
- community listing reports members, representative symbols, and edge density
- architecture tools work on the Python fixtures and external validation repo
- missing GDS support in Neo4j has a documented Python fallback

## Phase 9: Lumen Compatibility Adapter

Deliverables:

- optional Lumen GraphStore adapter
- optional `lumen_code_graph_retrieve` alias
- side-by-side invocation path
- parity report comparing current and standalone implementations

Exit criteria:

- Lumen can call the standalone implementation without replacing the old one
- fall back to current implementation remains trivial

## Phase 10: Switch-Over

Deliverables:

- Lumen config flag
- migration notes
- rollback notes
- old implementation deprecation issue

Exit criteria:

- standalone implementation is as good as current implementation for Lumen
- rollback is a config change
- old code remains available until confidence is high

## Switch-Over Gates

Do not switch Lumen until all gates pass:

- Python graph extraction parity on fixtures
- retrieval parity on a curated query set
- persisted Neo4j retrieval works
- incremental indexing works or has an accepted fallback
- semantic and keyword retrieval pass curated query tests
- expanded MCP analysis tools pass smoke tests
- SQLite local backend works without Neo4j
- architecture/community summaries are available or explicitly deferred by an
  accepted issue
- source snippet safety tests pass
- no regression in current agent workflow
- rollback path tested
