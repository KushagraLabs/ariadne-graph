# Dimension 5: Codebase-Memory and Other State-of-the-Art Tools

## Codebase-Memory (arXiv:2603.27277, GitHub: DeusData/codebase-memory-mcp)

### Architecture
- **Implementation**: Single statically linked C binary, zero runtime dependencies
- **Parsing**: Tree-sitter across 158 languages (vendored grammars compiled into binary)
- **Storage**: SQLite (single file)
- **Query**: Custom Cypher-like query engine
- **MCP Tools**: 14 typed tools
- **Communities**: Louvain community detection
- **Incremental**: File watcher with XXH3 content hashing

### Three-Stage Pipeline
1. **Parse**: Walk Tree-Sitter ASTs, extract definitions, call sites, imports, references
2. **Build**: Multi-phase pipeline with parallel worker pools, in-memory graph buffers
3. **Serve**: MCP server with 14 tools

### 14 MCP Tools (Grouped)
| Category | Tools |
|----------|-------|
| Indexing | index_repository, index_status, list_projects, delete_project |
| Query | search_graph, trace_call_path, query_graph, ingest_traces |
| Analysis | detect_changes, get_graph_schema, get_architecture |
| Code | get_code_snippet, search_code, manage_adr |

### Hybrid LSP Architecture
Two-layer approach:
1. **Tree-sitter pass**: Fast syntactic extraction for all 158 languages
2. **Hybrid LSP pass**: Type-aware refinement for Python, TS/JS/JSX/TSX, PHP, C#, Go, C, C++

Clean-room reimplementation of type-resolution algorithms from tsserver, pyright, gopls, etc.

### Performance Benchmarks (31 repos, 12 question categories)
| Metric | MCP Agent | Explorer Agent |
|--------|-----------|----------------|
| Quality Score | 0.83 | 0.92 |
| Tool Calls/Question | 2.3 | 4.8 |
| Tokens/Question | ~1,000 | ~10,000 |
| Query Latency | <1ms | 10-30s |

### Key Differentiators
- **158 languages** (most comprehensive)
- **Sub-millisecond queries** (fastest)
- **10x fewer tokens** than file-based exploration
- **C binary with zero dependencies** (simplest deployment)
- **Hybrid LSP** for semantic type resolution
- **Cross-service HTTP linking** (REST endpoints as graph entities)
- **Infrastructure-as-code indexing** (Dockerfiles, K8s manifests)

## RepoGraph (ICLR 2025)
- Line-level graph representation (each node = one line of code)
- Ego-graph retrieval for contextual code understanding
- 32.8% relative improvement on SWE-bench
- Integrates with both procedural and agent frameworks
- Sub-graph retrieval algorithms extract relationships around keywords

## Sourcegraph MCP Server
- SCIP-based precise code intelligence (open standard)
- Cross-repository code navigation
- Multi-layer context: local file, local repo, remote repo
- Keyword search, semantic search, go-to-definition, find-references
- Deep Search: multi-step investigation across repos
- History search: commits, diffs, blame

## Key Insights for Code Hygiene MCP
1. **Tree-sitter is the consensus parser choice** across all modern tools
2. **SQLite is increasingly preferred** over Neo4j for local-first tools
3. **MCP tool richness matters**: 5 tools (planned) vs 14-26 (competitors)
4. **Hybrid retrieval** (semantic + keyword + graph) outperforms graph-only
5. **Incremental sync** with file watching is table stakes
6. **Community detection** (Louvain) adds value for architecture understanding
7. **Type resolution beyond AST** (Hybrid LSP) significantly improves call graph accuracy
8. **Deterministic IDs** enable parity testing and incremental updates
