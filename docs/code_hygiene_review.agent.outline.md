# Code Hygiene MCP: Architectural Review & Improvement Recommendations

## Executive Summary
### Key Findings
#### The planned architecture is sound in its adapter-based design and parity-driven migration strategy but has significant gaps in MCP tool surface area, semantic search, incremental sync, and parser strategy compared to state-of-the-art tools
#### Five priority improvements identified: expand MCP tools (5 to 14+), add semantic/vector search, implement incremental file watching, adopt Tree-sitter for TypeScript, and add community detection

## 1. Current Plan Assessment (~2500 words, 2 tables)
### 1.1 Architecture Strengths
#### 1.1.1 The adapter pattern for both language parsers and graph stores enables clean extensibility without core modification
#### 1.1.2 Parity-driven migration with 95% coverage gates and fixture-based testing is methodologically rigorous
#### 1.1.3 Graph schema richness with 12 node types and 15 relationship types covers Python-specific constructs (decorators, type edges, FastAPI routes) that generic tools miss
#### 1.1.4 Repo-agnostic core with Lumen integration as optional adapter prevents vendor lock-in
### 1.2 Identified Weaknesses
#### 1.2.1 MCP tool surface area (5 planned tools) is 2-5x smaller than competitors (Codebase-Memory: 14, code-graph-rag-mcp: 26)
#### 1.2.2 No semantic search or vector embedding capability limits natural language query support
#### 1.2.3 No incremental synchronization strategy specified for live coding workflows
#### 1.2.4 TypeScript parser choice (ts-morph) requires Node helper process, adding operational complexity
#### 1.2.5 No community detection or architecture summarization for macro-level codebase understanding
### 1.3 Comparison at a Glance
#### 1.3.1 Feature matrix comparing Code Hygiene MCP plan against FalkorDB Code-Graph, code-graph-rag-mcp, Graphiti, and Codebase-Memory across 15 capabilities (table)
#### 1.3.2 Architecture pattern comparison: storage backends, parsing strategies, query interfaces, deployment models (table)

## 2. Deep-Dive: Critical Gaps and Recommendations (~4000 words, 4 tables)
### 2.1 Gap: MCP Tool Surface Area
#### 2.1.1 The 5 planned tools cover basic CRUD but lack analysis capabilities: impact analysis, hotspot detection, clone detection, architecture summary, change detection
#### 2.1.2 Recommended expansion to 12-15 tools: add code_graph_impact_analysis, code_graph_detect_changes, code_graph_search_semantic, code_graph_get_architecture, code_graph_find_hotspots, code_graph_list_communities
#### 2.1.3 Tool design inspired by Codebase-Memory's 14-tool categorization (indexing, query, analysis, code) and code-graph-rag-mcp's 26-method approach
### 2.2 Gap: Semantic and Hybrid Search
#### 2.2.1 Current plan supports only deterministic symbol-based retrieval; no natural language code search
#### 2.2.2 Graphiti's hybrid retrieval (cosine similarity + BM25 + graph traversal) achieves P95 300ms latency without LLM calls at query time
#### 2.2.3 Recommendation: add vector embeddings for code entities, implement hybrid search combining semantic similarity with graph traversal; sqlite-vec for local, Neo4j vector indexes for production
### 2.3 Gap: Incremental Synchronization
#### 2.3.1 No strategy specified for keeping the graph current during active development
#### 2.3.2 Codebase-Memory's approach: XXH3 content hashing with adaptive polling file watcher, incremental re-indexing of changed files only
#### 2.3.3 Recommendation: implement content-hash-based incremental sync with configurable polling intervals; support both manual reindex triggers and automatic file watching
### 2.4 Gap: Parser Strategy and Language Coverage
#### 2.4.1 Python AST is appropriate for Python (superior semantic depth for Python-specific constructs like decorators, dataclasses, Pydantic models)
#### 2.4.2 For TypeScript: Tree-sitter should be preferred over ts-morph — offers error tolerance, consistent cross-language AST structure, and eliminates Node helper process dependency
#### 2.4.3 Long-term: Tree-sitter provides a path to 10+ additional languages without per-language parser development
### 2.5 Gap: Community Detection and Architecture Views
#### 2.5.1 No planned capability for macro-level codebase organization analysis
#### 2.5.2 Codebase-Memory uses Louvain community detection to identify module clusters and high-level architecture patterns
#### 2.5.3 Recommendation: integrate Louvain or label-propagation community detection for automatic architecture summarization and module boundary identification

## 3. Storage Backend Reconsideration (~2000 words, 2 tables)
### 3.1 Neo4j as Planned Primary Backend
#### 3.1.1 Neo4j advantages: native graph traversal, Cypher query language, mature ecosystem, production-grade clustering, vector index support (v5+)
#### 3.1.1 Neo4j disadvantages: requires separate service deployment, operational complexity, licensing considerations for enterprise features
### 3.2 SQLite + Vector as Alternative
#### 3.2.1 code-graph-rag-mcp's SQLite + sqlite-vec approach: zero configuration, single file, <100ms queries, 65MB memory footprint
#### 3.2.2 Codebase-Memory's SQLite approach: sub-millisecond queries for 158 languages, single C binary with zero dependencies
#### 3.2.3 Trade-off analysis: Neo4j for enterprise/multi-user deployments, SQLite for individual developer local-first workflows (table)
### 3.3 Recommendation: Dual-Backend Strategy
#### 3.3.1 Leverage existing GraphStore adapter protocol to support both backends with same interface
#### 3.3.2 SQLite as default for development and testing; Neo4j for production persistent retrieval
#### 3.3.3 Add SQLiteGraphStore implementation with sqlite-vec for semantic search capability

## 4. Temporal Model and Code Evolution (~1500 words, 1 table)
### 4.1 Graphiti's Temporal Approach
#### 4.1.1 Bi-temporal model: valid time (when fact was true) and transaction time (when system knew it)
#### 4.1.2 Edge invalidation: contradictions stamp old edges invalid rather than deleting — preserves history
#### 4.1.3 Episode-based provenance: every derived fact traces back to source data
### 4.2 Applicability to Code Hygiene
#### 4.2.1 Git-history-aware temporal queries could identify when code smells were introduced
#### 4.2.2 Edge invalidation pattern naturally models refactoring: old IMPORTS edge invalidated when import is removed
#### 4.2.3 Cost-benefit analysis: temporal adds complexity; recommend git-commit-level versioning rather than full bi-temporal model
### 4.3 Recommendation: Lightweight Temporal Tracking
#### 4.3.1 Add `first_seen_at`, `last_modified_at`, `source_commit` properties to nodes/edges
#### 4.3.2 Use git blame integration to timestamp code facts with commit metadata
#### 4.3.3 Defer full temporal invalidation to future version unless proven necessary for core use cases

## 5. Refined Architecture and Implementation Priority (~2000 words, 2 tables)
### 5.1 Recommended Architecture Changes
#### 5.1.1 Updated MCP tool set: 12-15 tools across 4 categories (indexing, query, analysis, code) with specific tool specifications
#### 5.1.2 Hybrid search layer: vector embeddings + graph traversal + keyword filtering
#### 5.1.3 Incremental sync module: content hashing + file watcher + delta indexing
#### 5.1.4 Community detection module: Louvain algorithm for architecture clustering
### 5.2 Updated Milestone Sequence
#### 5.2.1 Prioritize MCP tool expansion and semantic search before TypeScript adapter (higher impact for Python users)
#### 5.2.2 Add SQLite backend milestone before Neo4j production hardening (faster developer adoption)
#### 5.2.3 Incremental sync as core requirement, not future enhancement
### 5.3 Implementation Priority Matrix
#### 5.3.1 Priority ranking of all recommendations by impact vs effort (table): Must-Have (MCP tools, semantic search, incremental sync), Should-Have (Tree-sitter, community detection), Could-Have (temporal tracking, SQLite backend), Won't-Have (full bi-temporal model)

# References
## code_hygiene_review.agent.outline.md
- **Type**: Report outline
- **Description**: This outline file
- **Path**: /mnt/agents/output/code_hygiene_review.agent.outline.md

## Research Dimension Files
- **Type**: Research artifacts
- **Description**: Five dimension reports covering plan summary, FalkorDB, code-graph-rag-mcp, Graphiti, and state-of-the-art tools
- **Path**: /mnt/agents/output/research/code_hygiene_review_dim01.md through dim05.md

## Uploaded Plan Documents
- **Type**: Source documents
- **Description**: Original architecture and planning documents provided by user
- **Path**: /mnt/agents/upload/architecture.md, graph-store-design.md, linear-execution-plan.md, python-adapter-parity.md, typescript-adapter-plan.md, README.md
