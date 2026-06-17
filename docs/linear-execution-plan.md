# Linear Execution Plan

## Project

Name:

```text
Reusable Code Hygiene MCP
```

Suggested team:

```text
Lumen
```

## Milestones

1. Architecture and Repo Scaffold
2. Python Parity
3. SQLite Graph Store and Incremental Sync
4. Retrieval, Semantic Search, and MCP Tool Expansion
5. Neo4j Production Backend
6. External Python Repo Validation
7. TypeScript Tree-sitter Adapter
8. Architecture Analysis and Community Detection
9. Lumen Compatibility and Switch-Over

## Initial Issues

1. Scaffold standalone `code_hygiene_mcp` repository
2. Define core graph schema and adapter contracts
3. Port Python AST fact extractor behind language adapter
4. Add fixture-based parity tests against current Lumen behavior
5. Implement `GraphStore` protocol and in-memory store
6. Implement first-class SQLite graph store
7. Add sqlite-vec or equivalent semantic-search storage support
8. Add content-hash manifests and incremental sync watcher
9. Port retrieval and prompt context generation
10. Expand standalone MCP server to indexing, query, analysis, and code tools
11. Add semantic search and keyword code search
12. Add impact analysis, change detection, and hotspot analysis
13. Implement direct Neo4j graph store
14. Add Neo4j vector-index and community-detection integration where available
15. Validate Python analyzer on `enterprise_tabular_ad`
16. Build Tree-sitter TypeScript adapter prototype for `cosmic_lens`
17. Add architecture summaries and community listing tools
18. Add optional Lumen compatibility adapter
19. Prepare Lumen switch-over and rollback plan
