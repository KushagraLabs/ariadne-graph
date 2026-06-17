# Implementation Plan: Code Hygiene MCP

## Phase 1: Project Scaffold + Core Infrastructure
- Initialize git repo, pyproject.toml, directory structure
- Core data models (CodeNode, CodeEdge, CodeGraphDelta)
- Config and discovery modules
- GraphStore base protocol + MemoryGraphStore

## Phase 2: Python AST Adapter
- LanguageAdapter protocol
- Python AST fact extractor (all node types, all relationships)
- Fixture-based parity tests

## Phase 3: SQLite Backend
- SQLiteGraphStore (tables: graphs, nodes, edges, file_hashes, snippets)
- SearchableGraphStore with sqlite-vec or fallback
- FTS keyword search
- Incremental sync (XXH3 hashing, dirty-file tracking)

## Phase 4: Retrieval + Search + Analysis
- Hybrid retrieval (graph traversal + semantic + keyword)
- Embedding provider abstraction
- Community detection (Louvain)
- Impact analysis, hotspot detection

## Phase 5: MCP Server + CLI
- MCP server with all 14 tools
- CLI commands
- Integration tests

## Parallelization Strategy
Phase 1 and Phase 2 can be done in parallel (different modules).
Phase 3 depends on Phase 1.
Phase 4 depends on Phase 3.
Phase 5 depends on Phase 4.
