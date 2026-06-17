# Cross-Verification: Confidence Tiers

## High Confidence (Verified across 3+ sources)
- Tree-sitter is the consensus parser choice for multi-language code graph tools (confirmed in Codebase-Memory paper, code-graph-rag-mcp, FalkorDB Code-Graph)
- MCP protocol is the standard interface for code intelligence tools (confirmed in Codebase-Memory, code-graph-rag-mcp, Graphiti MCP server, Sourcegraph MCP)
- SQLite + vector extensions is a viable alternative to dedicated graph databases for local-first tools (confirmed in code-graph-rag-mcp, Codebase-Memory, Cognee)
- Hybrid retrieval (semantic + keyword + graph) outperforms single-mode retrieval (confirmed in Graphiti benchmarks, Codebase-Memory evaluation)
- Incremental indexing with content hashing is table stakes (confirmed in Codebase-Memory, code-graph-rag-mcp)
- Sub-millisecond to sub-second query latency is achievable for code graph queries (confirmed: Codebase-Memory <1ms, Graphiti P95 300ms)
- 10x+ token reduction vs file-based exploration (confirmed in Codebase-Memory benchmarks)

## Medium Confidence (Verified across 2 sources)
- Louvain community detection adds value for architecture understanding (Codebase-Memory, FalkorDB Code-Graph)
- Hybrid LSP (type resolution beyond AST) significantly improves call graph accuracy (Codebase-Memory paper, code-graph-rag-mcp advanced language support)
- Temporal knowledge graphs are useful for code evolution tracking (Graphiti, FalkorDB Code-Graph git history)
- 14-26 MCP tools is the competitive range (code-graph-rag-mcp: 26, Codebase-Memory: 14)
- SHA256-based deterministic IDs are best practice for incremental updates (code-graph-rag-mcp, Graphiti)

## Lower Confidence / Emerging
- Bi-temporal model specifically for code graphs (primarily from Graphiti/Zep which targets agent memory, not code analysis specifically)
- Pydantic-based custom ontology for code entities (Graphiti feature, not widely adopted in code tools yet)
- Edge invalidation patterns for code refactoring (theoretical fit, not demonstrated in code analysis context)

## Conflict Zone
- **Neo4j vs SQLite**: The plan prefers Neo4j; code-graph-rag-mcp and Codebase-Memory use SQLite. Both are valid with different trade-offs: Neo4j offers superior graph traversal and Cypher; SQLite offers zero-config deployment. Recommendation: support both via the adapter pattern.
- **Custom parsers vs Tree-sitter**: The plan uses Python AST and ts-morph; state-of-the-art uses Tree-sitter. Python AST offers richer semantic analysis for Python; Tree-sitter offers cross-language consistency. Recommendation: use Python AST for Python (superior semantic depth) and Tree-sitter for TypeScript/other languages.
