# Cross-Dimension Insights: Code Hygiene MCP Architecture Review

## Critical Gap: MCP Tool Surface Area
The planned 5 MCP tools (code_graph_retrieve, code_graph_index, code_graph_status, code_graph_inspect_file, code_graph_trace_dependencies) are significantly fewer than competitors: Codebase-Memory offers 14, code-graph-rag-mcp offers 26. The minimal tool set limits agent capabilities for structural analysis, impact prediction, and code quality insights.

## Critical Gap: Semantic Search
The current plan has no semantic search capability. Both code-graph-rag-mcp and Codebase-Memory offer semantic/vector search for natural language code queries. Graphiti's hybrid retrieval (semantic + BM25 + graph traversal) achieves sub-second P95 latency. The planned graph schema supports only deterministic symbol-based retrieval.

## Critical Gap: Incremental Synchronization
No file watcher or incremental re-indexing strategy is specified. Codebase-Memory uses XXH3 content hashing with adaptive polling. code-graph-rag-mcp supports incremental parsing. For a development tool used in live coding workflows, incremental sync is essential.

## Important Gap: Parser Strategy
The plan uses Python AST for Python and ts-morph/TypeScript compiler API for TypeScript. State-of-the-art tools (Codebase-Memory, code-graph-rag-mcp, FalkorDB Code-Graph) have converged on Tree-sitter, which supports 66-158 languages with a single parser framework. Tree-sitter is error-tolerant (handles incomplete code) and produces consistent ASTs across languages.

## Important Gap: Storage Backend Choice
Neo4j is designated as the primary production backend. However, code-graph-rag-mcp's choice of SQLite + sqlite-vec offers compelling advantages: zero configuration, single-file deployment, no separate service, vector search capability. For a standalone developer tool, this significantly reduces operational complexity. Neo4j remains appropriate for enterprise/multi-user deployments.

## Important Gap: Community Detection
No community detection or architecture summarization is planned. Codebase-Memory uses Louvain community detection to identify module clusters. This enables high-level architecture views and helps agents understand codebase organization at macro scale.

## Moderate Gap: Call Graph Accuracy
The plan's Python adapter extracts "call edges where currently available" — acknowledging limitations. Codebase-Memory's Hybrid LSP approach (type-aware resolution beyond AST) significantly improves cross-module call graph accuracy. This is critical for reliable impact analysis.

## Moderate Gap: Temporal Model
No temporal tracking of code evolution is planned. Graphiti's bi-temporal model (valid time + transaction time) enables querying code state at any point in time. While less critical for a hygiene checker than for agent memory, git-history-aware temporal queries could support understanding when issues were introduced.

## Moderate Gap: Deterministic Graph IDs
The plan mentions "exact preservation of core node ID strategy" but doesn't specify a deterministic content-based ID approach. code-graph-rag-mcp uses SHA256-based stable IDs. Codebase-Memory uses qualified-name-based IDs. Deterministic IDs enable reliable incremental updates and parity testing.

## Strength: Adapter Architecture
The language adapter protocol and graph store protocol are well-designed abstractions that enable extensibility. This is architecturally sound and matches best practices.

## Strength: Parity-Driven Migration
The Lumen parity strategy with fixture-based testing and measurable coverage gates is methodologically rigorous. The 95% coverage threshold is appropriate.

## Strength: Graph Schema Richness
The planned node labels (12 types) and relationships (15 types) are comprehensive, covering Python-specific constructs (decorators, type edges, route edges) that generic code graph tools often miss.

## Emerging Pattern: TypeScript-as-Helper
The plan's TypeScript adapter as a Node helper process with JSON contract is pragmatic. This mirrors how Codebase-Memory embeds multiple language parsers — native tooling for each language, unified graph output.
