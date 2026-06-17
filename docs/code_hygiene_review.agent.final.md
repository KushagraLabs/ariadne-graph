# Code Hygiene MCP: Architectural Review & Improvement Recommendations

**Date:** June 2026
**Subject:** Comparative architectural review of the planned Code Hygiene MCP server against FalkorDB Code-Graph, code-graph-rag-mcp, Graphiti, and Codebase-Memory

---

## Executive Summary

### Key Findings

The planned Code Hygiene MCP architecture demonstrates sound fundamental design through its dual-adapter pattern (language parsing and graph storage as independent adapters), parity-driven migration methodology with measurable 95% coverage gates, and a richly expressive graph schema capturing Python-specific constructs like decorators, type flows, and FastAPI routes. However, comparative analysis against four state-of-the-art tools reveals a cluster of critical gaps that would place the system at a significant functional disadvantage if unaddressed.

The five most impactful gaps are: **(1)** MCP tool surface area of 5 planned tools versus 14–26 in competing systems, constraining agent analytical capabilities; **(2)** complete absence of semantic search or vector embedding support, forcing agents to query by exact symbol names rather than natural language intent; **(3)** no incremental synchronization strategy for live coding workflows, creating a freshness-versus-overhead trade-off that competing systems have eliminated; **(4)** TypeScript parser choice (`ts-morph` via Node helper process) that introduces multi-runtime operational complexity when Tree-sitter offers a zero-dependency alternative; and **(5)** no community detection or architecture summarization, limiting macro-level codebase understanding.

Five priority improvements are recommended: **(a)** expand the MCP tool set from 5 to 14 tools across indexing, query, analysis, and code categories; **(b)** add a hybrid semantic search layer combining vector embeddings, BM25 keyword search, and graph traversal; **(c)** implement incremental file watching with XXH3 content-hash-based change detection; **(d)** adopt Tree-sitter for TypeScript parsing to eliminate the Node.js dependency; and **(e)** integrate Louvain community detection for automatic architecture summarization. Additionally, the storage strategy should be expanded from Neo4j-only to a dual-backend approach where SQLite with `sqlite-vec` serves as the default for development and Neo4j remains the production backend. Temporal tracking should be limited to lightweight git-commit-level timestamping rather than Graphiti's full bi-temporal model, which is disproportionately complex for code hygiene use cases.

The implementation roadmap resequences milestones to prioritize high-impact items: MCP tool expansion and semantic search are elevated ahead of the TypeScript adapter; SQLite backend support is inserted as a dedicated milestone; and incremental sync is classified as a core requirement rather than a post-production enhancement. The total estimated effort for all Must-Have improvements is 8–11 weeks, with Should-Have items adding 4–6 weeks. The adapter-based architecture already supports most of these changes without structural modification — the primary work is additive implementation behind existing protocol boundaries.

---

## 1. Current Plan Assessment

### 1.1 Architecture Strengths

#### 1.1.1 Adapter Pattern Enables Clean Extensibility

The planned architecture defines two primary abstraction boundaries — a `LanguageAdapter` Protocol and a `GraphStore` Protocol — that together decouple the system's core from both parser implementations and persistence backends. The `LanguageAdapter` contract requires each adapter to expose a `language` identifier, a `parser_version`, supported file `extensions`, and two methods: `discover_files` for file enumeration and `extract_file` for per-file graph delta extraction. This design mirrors the strategy pattern documented in the architecture specification, where the Python adapter uses the standard library `ast` module and the TypeScript adapter targets `ts-morph` or the TypeScript compiler API, each producing a common `CodeGraphDelta` payload. The `GraphStore` Protocol similarly abstracts over in-memory, JSON/SQLite, Neo4j, and optional Lumen KnowledgeGraphEngine backends, exposing only `delete_graph`, `add_nodes_batch`, `add_edges_batch`, and `query` methods.

This dual-adapter approach compares favorably to the tightly coupled architectures observed in competing tools. FalkorDB Code-Graph, for instance, binds its indexing and query logic directly to the FalkorDB Redis module with no intermediate abstraction, making migration to alternative graph stores impractical [^16^]. Codebase-Memory avoids this coupling by embedding Tree-sitter and SQLite directly into a single static C binary, but trades extensibility for deployment simplicity — new language support requires recompiling the binary rather than installing an adapter [^53^]. The planned adapter pattern preserves the option to add languages (e.g., Go, Rust) or swap graph backends without modifying core retrieval or MCP tool logic, a property that reduces long-term maintenance burden as the system scales beyond its initial Python and TypeScript targets.

#### 1.1.2 Parity-Driven Migration with Measurable Coverage Gates

The migration methodology treats the existing Lumen implementation as a golden reference and mandates that no switch-over occurs until explicit parity gates are satisfied. The defined metrics — 95% node label coverage, 95% relationship coverage, exact preservation of core node ID strategy, and top-5 retrieval overlap for curated queries — transform what is often an subjective "good enough" judgment into a measurable engineering decision. This approach is methodologically rigorous because it provides binary pass/fail criteria for each adapter before it is promoted to production use.

The fixture-based testing strategy reinforces this rigor. Three fixture classes — a minimal Python package, a FastAPI/Pydantic package, and a Lumen-derived fixture with facade edge cases — are used to compare payload determinism, node IDs, labels, edge relationships, and source snippets against the golden reference. Fixture-based parity testing is particularly valuable for graph extraction systems because small changes in parser behavior can propagate through the entire graph topology; deterministic fixtures provide a controlled baseline against which regressions are immediately detectable. The 95% threshold acknowledges that the standalone implementation may legitimately differ from Lumen on platform-specific constructs (DI container probes, facade contract checks) that do not belong in a repo-agnostic tool, while ensuring that the core extraction quality remains intact.

#### 1.1.3 Graph Schema Richness for Python-Specific Constructs

The planned graph schema defines 12 node types and 15 relationship types, a level of expressiveness that exceeds the generic function-class-module triplets typical of baseline code graph tools. The schema includes dedicated constructs for Python-specific patterns: `DECORATED_BY` edges capture decorator semantics (critical for frameworks like Flask and FastAPI), `USES_TYPE` and `RETURNS_TYPE` edges preserve type annotation information, `ROUTES_TO` edges model FastAPI route definitions, and `HAS_DIAGNOSTIC` nodes attach audit findings directly to the graph entities they describe. The node inventory also distinguishes `CodeMethod` from `CodeFunction`, `CodeVariable` from `CodeAttribute`, and includes `CodeDiagnostic` and `CodeRoute` types that competing systems often omit.

This schema richness directly impacts the quality of context that can be provided to an LLM agent. Consider a query about "which API endpoints use a particular authentication decorator" — the `DECORATED_BY` and `ROUTES_TO` relationships allow this to be answered via graph traversal rather than grep, reducing token consumption and eliminating false positives from comment or string matches. Generic tools such as FalkorDB Code-Graph model only `Module`, `Class`, and `Function` nodes with `CALLS`, `INHERITS_FROM`, and `DEPENDS_ON` edges [^16^], which cannot represent decorator chains or type flows. Codebase-Memory captures broader language coverage but uses a more generic edge vocabulary optimized for cross-language uniformity rather than language-specific semantic depth [^14^]. The planned schema thus occupies a differentiated position: narrower in language breadth than Codebase-Memory's 158 languages, but deeper in Python-specific semantic expressiveness.

#### 1.1.4 Repo-Agnostic Core with Optional Lumen Integration

The architecture explicitly prohibits the core package from importing Lumen application modules, DI containers, or FastAPI routers, enforcing this isolation as a design principle rather than a convention. The Lumen integration is implemented as a optional `LumenGraphStore` adapter that sits behind the `GraphStore` Protocol, making it reversible and non-blocking. This arrangement prevents vendor lock-in and ensures that the standalone tool remains deployable in any Python or TypeScript repository without requiring the Lumen platform runtime.

This design decision has operational implications. Neo4j is designated as the primary production backend for the standalone tool, with direct Cypher queries rather than Lumen's KnowledgeGraphEngine mediating all graph access. The graph store design document explicitly lists the concerns that copying Lumen KG would introduce — service containers, metadata services, versioned fact stores, platform auth assumptions — and rejects them in favor of a "compact persistence and query interface" [^16^]. The result is a system that can serve as a drop-in replacement for Lumen's code hygiene functionality without creating a dependency on Lumen's broader platform lifecycle.

### 1.2 Identified Weaknesses

#### 1.2.1 MCP Tool Surface Area is 2–5x Smaller than Competitors

The planned tool surface consists of five MCP methods: `code_graph_retrieve`, `code_graph_index`, `code_graph_status`, `code_graph_inspect_file`, and `code_graph_trace_dependencies`. This is substantially smaller than the competitive landscape. Codebase-Memory exposes 14 typed tools organized across indexing, query, analysis, and code categories, including `get_architecture` for community-level summaries, `detect_changes` for incremental awareness, and `search_code` for text-based retrieval [^53^]. The code-graph-rag-mcp project provides 26 methods covering semantic search, code similarity analysis, clone detection, impact analysis, hotspot analysis, AI refactoring suggestions, agent telemetry, and graph health diagnostics [^34^].

The gap is not merely numerical. The five planned tools support retrieval, indexing, status inspection, file-level viewing, and dependency tracing — all necessary functions, but collectively insufficient for the structural analysis workflows that modern coding agents require. For example, none of the planned tools expose community detection (to understand module clusters), code similarity (to identify duplication), impact prediction (to assess change risk), or hotspot analysis (to find complexity concentrations). The Codebase-Memory evaluation demonstrates that agents with access to 14 graph-native tools achieve 83% answer quality at 10x fewer tokens than file-based exploration, with the richest tool set producing the most efficient agent behavior [^14^]. A five-tool surface constrains the agent to basic retrieval operations, forcing it to fall back to file-reading for analysis tasks that competing tools handle via optimized graph queries.

#### 1.2.2 No Semantic Search or Vector Embedding Capability

The current plan specifies deterministic symbol-based retrieval only — queries traverse the graph by matching node labels, relationship types, and property values. There is no provision for semantic search, vector embeddings, or natural language query translation. This is a significant gap because the most advanced competing tools all incorporate semantic retrieval. Code-graph-rag-mcp implements provider-based embeddings with support for memory, transformers, Ollama, OpenAI, and CloudRU providers, enabling natural language queries such as "find authentication functions" to resolve to relevant code entities via vector similarity [^34^]. Graphiti's hybrid retrieval combines cosine similarity on embeddings, BM25 keyword search, and graph traversal into a single ranked result set, achieving a P95 query latency of 300 ms and 94.7% accuracy on the LOCOMO benchmark [^57^].

The absence of semantic search means that an agent using the Code Hygiene MCP must formulate queries in structural terms — knowing the exact function name, class name, or relationship type to traverse — rather than describing intent in natural language. This shifts cognitive load from the tool to the agent, increasing the probability of imprecise queries that return irrelevant subgraphs or miss related entities. Codebase-Memory's evaluation found that graph-native structural queries (hub detection, caller ranking) outperformed file-based exploration on 19 of 31 languages when the graph was accessible via rich tool interfaces [^14^]; adding semantic search would further extend this advantage to intent-based queries where the agent does not already know the exact symbol names.

#### 1.2.3 No Incremental Synchronization Strategy Specified

The plan acknowledges "incremental indexing metadata" as a capability to preserve from the Lumen implementation, but does not specify a concrete synchronization strategy for live coding workflows. This omission is consequential because developers expect code intelligence tools to reflect changes within seconds of saving a file, not requiring a manual re-index after every edit. Codebase-Memory addresses this with a background file watcher that uses XXH3 content hashing to detect changes and re-indexes incrementally; the re-index is typically a sub-millisecond no-op when nothing has changed [^53^]. Code-graph-rag-mcp implements incremental parsing with a batched indexing system that supports resumable indexing with progress tracking [^34^].

Without an explicit incremental sync mechanism, the planned system would likely default to batch re-indexing, which introduces a choice between freshness (re-index frequently, paying the cost each time) and accuracy (re-index rarely, serving stale graph data). For a tool positioned as an MCP server that agents query during active development sessions, this trade-off is unacceptable. The architecture should specify a file watcher integration (using `watchdog` on Python or platform-native APIs), a content-based change detection strategy, and a policy for incremental graph delta application that preserves existing node IDs for unchanged entities.

#### 1.2.4 TypeScript Parser Choice Adds Operational Complexity

The TypeScript adapter plan specifies `ts-morph` or the TypeScript compiler API as the parser, implemented as a Node helper process invoked by the Python MCP core with a JSON contract. While this choice leverages the most mature TypeScript parsing infrastructure available, it introduces a runtime dependency on Node.js that increases operational complexity for what is otherwise a Python-native tool. The deployment surface expands from a single Python package to a Python-plus-Node environment, with version compatibility, process lifecycle management, and cross-platform behavior to coordinate.

This approach contrasts with the consensus parser choice in the broader code intelligence ecosystem. Codebase-Memory, code-graph-rag-mcp, and FalkorDB Code-Graph have all converged on Tree-sitter as their parser framework [^14^][^34^]. Tree-sitter supports 158 languages (in Codebase-Memory's vendored distribution) through a unified grammar interface, produces consistent abstract syntax trees across languages, and is error-tolerant — it can parse incomplete or syntactically invalid code, which is common during live editing [^53^]. The TypeScript Tree-sitter grammar supports `.ts`, `.tsx`, and JSX constructs natively, potentially covering the planned adapter's scope without a separate Node process. The plan's choice of `ts-morph` offers deeper semantic analysis (type resolution, symbol navigation) than Tree-sitter's syntactic AST, but at the cost of operational complexity that should be explicitly justified and mitigated.

#### 1.2.5 No Community Detection or Architecture Summarization

The planned tool set does not include any capability for macro-level codebase understanding — detecting module clusters, identifying architectural layers, or summarizing the overall structure of a repository. Codebase-Memory addresses this via Louvain community detection applied to the code graph, producing module clusterings that help agents understand codebase organization at scale [^14^]. The `get_architecture` MCP tool in Codebase-Memory exposes these communities as structured output, enabling an agent to answer questions like "what are the main subsystems?" or "which modules are most tightly coupled?" without traversing thousands of individual nodes.

This gap limits the planned system's utility for large repositories. While `code_graph_trace_dependencies` supports dependency chain traversal, it requires the agent to already know which entities to trace. Community detection provides an entry point: the agent can query the architecture summary first, identify relevant clusters, and then drill down into specific dependency chains. FalkorDB Code-Graph similarly provides repository-level analysis and commit-level history views [^16^], and Graphiti's community subgraph layer ($G_c$) produces summaries of strongly connected entity clusters [^57^]. The absence of this capability in the Code Hygiene MCP plan means agents lose a high-level orientation mechanism that competing tools provide.

### 1.3 Comparison at a Glance

#### 1.3.1 Feature Matrix

Table 1 compares the planned Code Hygiene MCP server against four state-of-the-art tools across 15 capabilities. Each capability is rated on a three-point scale: **Full** (native implementation), **Partial** (limited or planned support), or **None** (no support identified).

<table>
<thead style="background-color:#f0f0f0">
<tr><th>Capability</th><th>Code Hygiene MCP (planned)</th><th>FalkorDB Code-Graph</th><th>code-graph-rag-mcp</th><th>Graphiti</th><th>Codebase-Memory</th></tr>
</thead>
<tbody>
<tr><td>Semantic search</td><td>None</td><td>Full (LLM-to-Cypher)</td><td>Full</td><td>Full (hybrid)</td><td>Full</td></tr>
<tr><td>Vector embeddings</td><td>None</td><td>None</td><td>Full (multi-provider)</td><td>Full</td><td>Full</td></tr>
<tr><td>Incremental sync</td><td>Partial (metadata only)</td><td>None</td><td>Full (batched)</td><td>Full</td><td>Full (XXH3 watcher)</td></tr>
<tr><td>Multi-language parsing</td><td>Partial (2 languages)</td><td>Partial (3 languages)</td><td>Full (8+ languages)</td><td>N/A</td><td>Full (158 languages)</td></tr>
<tr><td>MCP tool count</td><td>5</td><td>0 (REST only)</td><td>26</td><td>~8 (experimental)</td><td>14</td></tr>
<tr><td>Community detection</td><td>None</td><td>None</td><td>None</td><td>Full (community subgraph)</td><td>Full (Louvain)</td></tr>
<tr><td>Temporal model</td><td>None</td><td>Partial (git history)</td><td>None</td><td>Full (bi-temporal)</td><td>None</td></tr>
<tr><td>Code similarity / clone detection</td><td>None</td><td>None</td><td>Full (JSCPD + semantic)</td><td>None</td><td>None</td></tr>
<tr><td>Impact analysis</td><td>Partial (dependency trace)</td><td>None</td><td>Full</td><td>None</td><td>Full</td></tr>
<tr><td>Hotspot analysis</td><td>None</td><td>None</td><td>Full</td><td>None</td><td>None</td></tr>
<tr><td>Type-aware call resolution</td><td>Partial (Python AST only)</td><td>None</td><td>Partial (Tree-sitter)</td><td>None</td><td>Full (Hybrid LSP)</td></tr>
<tr><td>Graph schema richness</td><td>Full (12 nodes, 15 edges)</td><td>Partial (3 nodes, 3 edges)</td><td>Partial (generic entities)</td><td>Full (custom ontology)</td><td>Full (typed nodes)</td></tr>
<tr><td>Graph visualization</td><td>None</td><td>Full (React UI)</td><td>None</td><td>None</td><td>Full (3D UI)</td></tr>
<tr><td>Adapter-based extensibility</td><td>Full (dual protocol)</td><td>None</td><td>Partial (embeddings)</td><td>Full (pluggable backends)</td><td>None (monolithic)</td></tr>
<tr><td>Zero-config deployment</td><td>Partial (SQLite optional)</td><td>None (FalkorDB required)</td><td>Full (single-file SQLite)</td><td>None (Neo4j required)</td><td>Full (single binary)</td></tr>
</tbody>
</table>

The feature matrix reveals several structural patterns. The planned Code Hygiene MCP has the most restrictive language coverage (2 languages) and the smallest MCP tool surface (5 tools), placing it at the lower end of agent-facing functionality. Its strengths are concentrated in schema richness and architectural extensibility: the 12 node types and 15 relationship types exceed FalkorDB Code-Graph's minimal schema, and the dual-adapter protocol is unique among the evaluated tools in decoupling both language parsing and graph storage simultaneously. The absence of semantic search, vector embeddings, incremental sync, community detection, and hotspot analysis creates a cluster of gaps in the upper-right quadrant of the table — capabilities that competing tools have converged on as baseline expectations.

Code-graph-rag-mcp offers the broadest functional coverage, with 26 MCP methods spanning semantic search, clone detection, impact analysis, and hotspot metrics, though its graph schema is generic (entities and relationships stored as adjacency lists rather than typed constructs) [^34^]. Codebase-Memory balances language breadth (158 languages via Tree-sitter) with deployment simplicity (single static C binary), trading adapter extensibility for operational zero-config properties [^14^]. Graphiti differentiates through its bi-temporal model and hybrid retrieval, targeting agent memory use cases rather than code analysis specifically, but its architecture patterns remain relevant for code evolution tracking [^57^]. FalkorDB Code-Graph occupies a middle ground with a React-based visualization UI and LLM-to-Cypher translation, but lacks MCP integration entirely, limiting its accessibility to agent workflows [^16^].

#### 1.3.2 Architecture Pattern Comparison

Table 2 compares the five tools across four architectural dimensions: storage backend, parsing strategy, query interface, and deployment model.

<table>
<thead style="background-color:#f0f0f0">
<tr><th>Dimension</th><th>Code Hygiene MCP (planned)</th><th>FalkorDB Code-Graph</th><th>code-graph-rag-mcp</th><th>Graphiti</th><th>Codebase-Memory</th></tr>
</thead>
<tbody>
<tr><td>Storage backend</td><td>Neo4j (primary) + SQLite + in-memory via adapter</td><td>FalkorDB (Redis module, GraphBLAS)</td><td>SQLite + sqlite-vec extension</td><td>Neo4j / FalkorDB / Kuzu (pluggable)</td><td>SQLite (single file)</td></tr>
<tr><td>Parsing strategy</td><td>Python AST + ts-morph / TS compiler API</td><td>Language-specific analyzers</td><td>Tree-sitter (WebAssembly)</td><td>LLM-based extraction</td><td>Tree-sitter + Hybrid LSP</td></tr>
<tr><td>Query interface</td><td>MCP (5 tools) + Cypher via Neo4j</td><td>REST API + LLM-to-Cypher chat</td><td>MCP (26 methods) + SQL</td><td>MCP (experimental) + SDK APIs</td><td>MCP (14 tools) + Cypher-like engine</td></tr>
<tr><td>Deployment model</td><td>Python package + optional Node for TS</td><td>Docker (FalkorDB + Redis + Flask)</td><td>npm package (Node.js)</td><td>Python/TypeScript SDK + graph DB</td><td>Single static C binary</td></tr>
</tbody>
</table>

The storage backend choices reveal a clear split in design philosophy. The planned Code Hygiene MCP designates Neo4j as its primary production backend, a choice that provides superior graph traversal performance and native Cypher support but requires a separate database service. Code-graph-rag-mcp and Codebase-Memory both use SQLite — the former with the `sqlite-vec` extension for vector search, the latter as a pure single-file deployment — achieving zero-configuration setups at the cost of some graph-traversal performance relative to Neo4j [^34^][^53^]. Graphiti's pluggable backend design (supporting Neo4j, FalkorDB, and Kuzu) offers the most flexibility, though at the cost of requiring configuration decisions from the deployer [^57^]. The planned adapter pattern could theoretically support SQLite as a first-class backend, but the architecture documents consistently position it as a "local cache" rather than a production-equivalent alternative to Neo4j.

The parsing strategies diverge along a semantic depth versus operational complexity axis. The planned Python AST + ts-morph combination offers the deepest semantic analysis for its two target languages — Python's `ast` module captures constructs that Tree-sitter's generic grammar cannot, and `ts-morph` provides type-resolution capabilities comparable to a language server. However, both are language-specific implementations, meaning each new language requires a custom adapter. Tree-sitter, used by code-graph-rag-mcp and Codebase-Memory, provides a unified parsing framework across 66–158 languages with error-tolerant behavior that handles incomplete code during live editing [^14^][^34^]. Codebase-Memory further augments Tree-sitter with a Hybrid LSP pass — a clean-room reimplementation of type-resolution algorithms from `tsserver`, `pyright`, and `gopls` — that refines call graph accuracy without requiring a running language server process [^53^]. The planned parsing strategy thus optimizes for depth on two languages at the expense of breadth, a defensible choice if the target use cases are predominantly Python and TypeScript repositories, but one that limits extensibility.

The query interface dimension illuminates the MCP tool surface gap most starkly. Code-graph-rag-mcp's 26 MCP methods expose the richest agent interface, followed by Codebase-Memory's 14 typed tools. The planned five tools are fewer even than Graphiti's experimental MCP server, which supports episode management, entity search, hybrid search, and graph maintenance operations [^57^]. The deployment model further compounds this difference: Codebase-Memory's single static C binary with zero runtime dependencies represents the simplest deployment path, while the planned Python-plus-Node architecture introduces multi-runtime coordination that the other tools avoid.

The architecture comparison suggests that the Code Hygiene MCP plan occupies a distinct but constrained niche. Its dual-adapter pattern and schema richness are genuine architectural strengths that competing tools do not replicate. However, the combination of a minimal MCP tool surface, absence of semantic search, lack of incremental sync, and multi-runtime deployment creates a cumulative functional deficit relative to the state-of-the-art. The following chapters address each of these gaps with specific, actionable recommendations.


---

## 2. Deep-Dive: Critical Gaps and Recommendations

The preceding chapter identified five areas where the planned Code Hygiene MCP architecture diverges from state-of-the-art implementations. This section examines each gap in depth, quantifies the divergence against demonstrated competitive benchmarks, and provides actionable recommendations with implementation priorities. The analysis is grounded in published performance data from three reference systems: Codebase-Memory (arXiv:2603.27277) with 14 MCP tools and sub-millisecond query latency [^32^], code-graph-rag-mcp with 26 methods and semantic search [^34^], and Graphiti with hybrid retrieval achieving P95 300 ms latency [^13^].

### 2.1 Gap: MCP Tool Surface Area

#### 2.1.1 Current State and Competitive Landscape

The architecture plan specifies five MCP tools: `code_graph_retrieve`, `code_graph_index`, `code_graph_status`, `code_graph_inspect_file`, and `code_graph_trace_dependencies`. These cover basic create-read-update-delete (CRUD) operations for the code graph and rudimentary dependency traversal. However, they omit analytical capabilities that have become standard in competing systems.

Codebase-Memory exposes 14 typed tools grouped into four functional categories — indexing, query, analysis, and code — including `detect_changes` for git-diff impact prediction, `get_architecture` for macro-level codebase summarization, and `trace_call_path` for directional call-chain traversal with configurable depth [^14^]. Codebase-Memory's evaluation across 31 real-world repositories demonstrated 83% answer quality while consuming 10x fewer tokens and 2.1x fewer tool calls per question versus file-based exploration [^53^]. The tool count alone does not determine quality, but the absence of entire functional categories — particularly analysis and semantic query — constrains the agent's ability to answer structural questions without falling back to expensive file-by-file search.

Code-graph-rag-mcp extends this surface further to 26 methods, adding semantic search, code similarity detection, clone detection via JSCPD integration, hotspot analysis for complexity and coupling metrics, impact analysis for change risk prediction, and cross-language relationship tracking [^34^]. Performance benchmarks for this system report 100+ files/second parsing throughput and sub-100 ms query response times against a SQLite-backed vector store [^5^].

The following table inventories the tool coverage across all three systems.

| Functional Category | Planned (5 tools) | Codebase-Memory (14 tools) | code-graph-rag-mcp (26 methods) |
|:---|:---|:---|:---|
| **Indexing** | `code_graph_index` | `index_repository`, `index_status`, `list_projects`, `delete_project` [^14^] | `index`, `batch_index`, `reset_graph`, `clean_index` [^34^] |
| **Basic Retrieval** | `code_graph_retrieve`, `code_graph_inspect_file` | `get_code_snippet`, `search_code` [^14^] | `get_entities`, `get_relationships`, `get_code_snippet` [^34^] |
| **Dependency Traversal** | `code_graph_trace_dependencies` | `trace_call_path`, `query_graph` (Cypher) [^14^] | `impact_analysis`, `trace_dependencies` [^34^] |
| **Semantic Search** | — | `search_graph` [^14^] | `semantic_search`, `code_similarity` [^34^] |
| **Change Detection** | — | `detect_changes` [^14^] | `detect_changes`, `get_graph_health` [^34^] |
| **Architecture Analysis** | — | `get_architecture`, `get_graph_schema` [^14^] | `hotspot_analysis`, `get_architecture` [^34^] |
| **Clone Detection** | — | — | `jscpd_clone_scan`, `code_similarity` [^34^] |
| **Diagnostics/Telemetry** | `code_graph_status` | — | `get_agent_metrics`, `get_bus_stats`, `get_version` [^34^] |

Table 1: MCP tool inventory across the planned architecture and two reference implementations. The planned five tools provide basic graph CRUD and dependency tracing but lack semantic search, change detection, architecture summarization, clone detection, and diagnostic telemetry.

#### 2.1.2 Recommended Expansion

The analysis recommends expanding the planned tool surface from 5 to 12–15 tools, organized into the same four categories used by Codebase-Memory. The additions should include:

- **`code_graph_impact_analysis`** — Given a symbol or file, return the transitive closure of dependent entities ranked by coupling strength. Code-graph-rag-mcp demonstrates that impact analysis significantly reduces risk in refactoring workflows by surfacing all dependent services, API endpoints, and potential breaking points before any code modification [^39^].
- **`code_graph_detect_changes`** — Compare the current graph against a stored snapshot (or git HEAD) to identify modified, added, and deleted entities. This tool directly addresses the gap identified in Codebase-Memory's evaluation, where detecting git-diff impact required specialized graph-diff logic [^14^].
- **`code_graph_search_semantic`** — Accept natural language queries ("find authentication functions") and return ranked code entities via vector similarity. Both competing systems provide this capability; its absence means agents must translate intent into exact symbol names.
- **`code_graph_get_architecture`** — Return a high-level codebase summary including module boundaries, detected communities, and inter-module coupling. This capability leverages community detection (Section 2.5) and provides the macro-level view that Codebase-Memory's `get_architecture` tool offers [^53^].
- **`code_graph_find_hotspots`** — Identify high-complexity, high-coupling regions using metrics such as cyclomatic complexity, fan-in, and fan-out. Code-graph-rag-mcp includes this as a core analytical tool for prioritizing refactoring targets [^34^].
- **`code_graph_list_communities`** — Enumerate the modules or communities detected in the codebase graph, with entity counts and inter-community edge densities.

#### 2.1.3 Design Inspiration from Competitors

Codebase-Memory's tool categorization (indexing, query, analysis, code) provides a proven organizational schema that maps cleanly to typical agent workflows: first index the repository, then query or analyze it, then retrieve specific code snippets for implementation [^14^]. Code-graph-rag-mcp's broader 26-method surface demonstrates that richer telemetry and diagnostics tools (`get_agent_metrics`, `get_bus_stats`) enable better operational visibility in production deployments [^34^]. The recommended 12–15 tool target represents a pragmatic midpoint — sufficient functional coverage without the operational complexity of a 26-method interface.

### 2.2 Gap: Semantic and Hybrid Search

#### 2.2.1 Current Plan: Deterministic Symbol-Based Retrieval Only

The planned architecture supports only deterministic retrieval: agents must specify exact symbol names, file paths, or relationship types to traverse the graph. This approach is precise when the agent already knows what it is looking for, but fails for exploratory queries where intent is expressed in natural language ("find functions that handle user authentication" or "where are database migrations defined?"). In practice, this limitation forces agents into expensive multi-step search patterns: first guessing potential file names or symbol names via regex, then reading files to verify relevance, then repeating the cycle — exactly the pattern that Codebase-Memory's benchmarks showed consuming approximately 412,000 tokens for five structural questions [^53^].

Both competing systems address this through vector embeddings. Codebase-Memory supports full-text code search via `search_code` and graph search via `search_graph`, with the evaluation demonstrating that graph-based search alone reduces tokens per question from roughly 10,000 (file-by-file exploration) to approximately 1,000 (graph queries) [^53^]. Code-graph-rag-mcp implements provider-based embeddings (supporting memory, transformers, ollama, openai, and cloudru providers) that enable natural language code search across all indexed entities [^34^]. The 5.5x speed improvement over native Claude tools reported by code-graph-rag-mcp is attributed in part to the elimination of iterative file search through direct semantic retrieval [^5^].

#### 2.2.2 Benchmark Evidence for Hybrid Retrieval

Graphiti's hybrid retrieval architecture — combining cosine similarity for semantic matching, BM25 for keyword relevance, and graph traversal for relationship-aware context — achieves P95 query latency of 300 ms without any LLM calls at query time [^13^]. This is enabled by near-constant-time access via pre-built vector and BM25 indexes on the graph backend. The benchmark comparison against MemGPT reports 94.8% DMR (Dialogue Memory Retrieval) accuracy versus MemGPT's 93.4%, an 18.5% improvement on LongMemEval, and approximately 90% latency reduction [^57^]. Thoughtworks' Technology Radar moved Graphiti to "Trial" status in April 2026, citing peer-reviewed benchmarks and the release of a first-class MCP server as evidence of production viability [^56^].

Codebase-Memory reports similarly compelling token-efficiency metrics: across five structural queries, knowledge-graph-based exploration consumed approximately 3,400 tokens versus 412,000 tokens for file-by-file search — roughly a 120x reduction [^53^]. A separate evaluation across 31 repositories confirmed 83% answer quality with 10x fewer tokens and 2.1x fewer tool calls compared to file-based exploration [^32^]. These results establish that hybrid retrieval is not merely a convenience feature but a transformative efficiency gain for agent-driven code exploration.

| Aspect | Planned (Graph Only) | Codebase-Memory | code-graph-rag-mcp | Graphiti (Reference) |
|:---|:---|:---|:---|:---|
| **Semantic Search** | Not planned | Full-text + graph search [^14^] | Provider-based embeddings [^34^] | Cosine similarity + BM25 + graph traversal [^13^] |
| **Query Latency** | Sub-100 ms (Neo4j) | < 1 ms (SQLite) [^53^] | < 100 ms (SQLite) [^34^] | P95 300 ms [^13^] |
| **LLM at Query Time** | No | No | Optional | No [^6^] |
| **Token Efficiency** | N/A (no semantic) | ~120x vs file search [^53^] | 5.5x faster vs native [^5^] | -90% latency vs GraphRAG [^57^] |
| **Storage Backend** | Neo4j | SQLite | SQLite + sqlite-vec | Neo4j / FalkorDB / Kuzu [^6^] |
| **Embedding Providers** | None | N/A | memory, transformers, ollama, openai [^34^] | OpenAI, Azure, Gemini, Anthropic [^57^] |

Table 2: Retrieval approach comparison across four systems. The planned architecture lacks semantic search entirely, while all reference implementations demonstrate that hybrid retrieval (semantic + keyword + graph) achieves sub-second latency without LLM calls at query time.

#### 2.2.3 Recommendation: Hybrid Search Implementation

The recommendation is to add vector embeddings for code entities and implement hybrid search that combines semantic similarity with graph traversal. The specific implementation path depends on the storage backend:

For local or development deployments, **sqlite-vec** provides a zero-configuration vector search extension for SQLite. Benchmarks on an M1-class machine demonstrate query times of 17 ms for 100,000 vectors with 8-bit quantization and 3.97 ms with preloading, achieving perfect recall at 20 results [^44^]. The sqlite-vec extension itself reports 17 ms query times for 1 million 128-dimension vectors (sift1m dataset) in static mode [^47^]. For a code graph with typically tens to hundreds of thousands of entities, this performance profile is more than adequate.

For production deployments using Neo4j, vector indexes are available as a native feature from version 5.11 onward. Neo4j supports both cosine similarity and Euclidean distance for vector search, and vector indexes can be combined with graph traversal in a single Cypher query — enabling the same hybrid retrieval pattern that Graphiti achieves [^13^]. The architectural decision is therefore: use sqlite-vec when the graph store adapter targets SQLite, and Neo4j native vector indexes when targeting Neo4j.

The embedding strategy should be provider-based (following code-graph-rag-mcp's design [^34^]) to allow flexibility between local models (for privacy-sensitive deployments), API-based models (for maximum accuracy), and quantized binary embeddings (for resource-constrained environments). For code-specific embeddings, models fine-tuned on code corpora (such as CodeBERT or code-specific sentence transformers) typically outperform general-purpose embedding models for semantic code search tasks.

### 2.3 Gap: Incremental Synchronization

#### 2.3.1 No Strategy Specified for Keeping the Graph Current

The architecture plan does not specify a strategy for keeping the code graph synchronized with the file system during active development. This is a critical operational gap: in a live coding workflow, files change frequently, and an out-of-date graph produces incorrect dependency traces, stale architecture views, and misleading impact analysis results.

#### 2.3.2 Codebase-Memory's Approach: XXH3 Content Hashing with Adaptive Polling

Codebase-Memory implements a background file watcher that monitors the repository using adaptive polling. On each file-system event, the system computes an XXH3 hash of the modified file and compares it against the stored hash. XXH3 is a non-cryptographic 64-bit hash achieving approximately 30 GB/s throughput, chosen over cryptographic alternatives for its speed in content-addressed indexing where collision resistance is a security-non-critical requirement [^14^]. If the hash differs, the file's existing nodes and edges are deleted and re-parsed with Tree-sitter; the hash is updated and affected Louvain community assignments are re-computed incrementally.

This approach offers several architectural advantages. First, content hashing is more reliable than timestamp-based change detection because version control operations (git checkout, branch switches) can revert files to earlier timestamps. Second, XXH3's throughput means the hashing overhead is negligible even for large codebases. Third, the adaptive polling strategy avoids the portability issues of OS-specific file system event APIs (FSEvents on macOS, inotify on Linux, ReadDirectoryChanges on Windows) while still achieving responsive synchronization.

Code-graph-rag-mcp similarly supports incremental parsing, with its batched indexing system providing resumable indexing and progress tracking for large repositories [^34^]. This pattern — content-hash-based change detection combined with selective re-parsing — is also the approach used by FalkorDB Code-Graph and other tools in the code intelligence ecosystem, confirming that incremental synchronization is considered table stakes among state-of-the-art implementations. For a development tool used in live coding workflows where files change on every save, the absence of incremental sync means the graph is either stale (leading to incorrect analysis results) or continuously rebuilt (leading to excessive CPU and I/O consumption). Neither outcome is acceptable for a tool intended to operate as a persistent structural analysis backend.

| Aspect | Planned | Codebase-Memory | code-graph-rag-mcp |
|:---|:---|:---|:---|
| **Change Detection** | Not specified | XXH3 content hash comparison [^14^] | Incremental parsing with file change detection [^34^] |
| **Watcher Strategy** | Not specified | Adaptive polling (portable, OS-agnostic) [^14^] | File-system event monitoring |
| **Granularity** | Not specified | File-level (re-parse changed files only) [^14^] | File-level with batching |
| **Community Re-computation** | Not specified | Incremental Louvain re-computation [^14^] | Not specified |
| **Trigger Modes** | Manual only | Automatic (watcher) + manual | Manual + automatic |
| **Resume Capability** | Not specified | Not applicable (fast enough) | `batch_index` with progress tracking [^34^] |

Table 3: Incremental synchronization strategy comparison. The planned architecture specifies no file watching or incremental re-indexing mechanism, while both reference systems implement content-hash-based change detection with automatic re-parsing.

#### 2.3.3 Recommendation: Content-Hash-Based Incremental Sync

The recommendation is to implement content-hash-based incremental synchronization with configurable polling intervals. The design should support both manual reindex triggers (for CI/CD integration and deterministic rebuilds) and automatic file watching (for interactive development workflows).

The implementation contract should extend the existing `GraphStore` adapter protocol with two new operations: `get_stored_hash(file_path)` and `update_hash(file_path, new_hash)`. The watcher loop runs in a background thread (or asyncio task) and, on detecting a content change, invokes the language adapter's `extract_file` method for that file only, then applies the resulting delta to the graph store. For Neo4j backends, this translates to deleting nodes and edges associated with the changed file and inserting the new ones within a single transaction. For SQLite backends, the same pattern applies using SQLite's transactional semantics.

The polling interval should be configurable (default 2 seconds for interactive use, longer for large repositories) with an adaptive mode that increases the interval when no changes are detected and decreases it when changes are frequent. This matches Codebase-Memory's approach and balances responsiveness against CPU overhead [^14^].

### 2.4 Gap: Parser Strategy and Language Coverage

#### 2.4.1 Python AST: Appropriate for Python with Superior Semantic Depth

The architecture plan correctly identifies Python's built-in `ast` module as the parser for Python code. This choice offers significant semantic depth that Tree-sitter cannot match for Python-specific constructs. The Python AST module provides full access to decorator chains, dataclass transformations, type annotation syntax, and Pydantic model definitions — constructs that are central to modern Python codebases but require language-specific knowledge to interpret correctly.

The planned graph schema demonstrates this depth through relationships such as `DECORATED_BY` (for Python decorators), `USES_TYPE` and `RETURNS_TYPE` (for type annotations), and `ROUTES_TO` (for web framework route definitions) — all of which are extracted more reliably from Python's native AST than from a generic Tree-sitter parse tree. This semantic richness is validated by the cross-verification analysis which confirmed Python AST offers deeper analysis for Python-specific constructs, while Tree-sitter offers superior cross-language consistency [^48^].

The recommendation is to **retain Python AST for Python parsing**.

#### 2.4.2 TypeScript: Tree-sitter Should Be Preferred Over ts-morph

The TypeScript adapter plan specifies `ts-morph` or the TypeScript compiler API as the parser for TypeScript and TSX files, with the adapter implemented as a Node helper process invoked by the Python MCP core. While this approach provides access to TypeScript's full type system, it introduces a significant architectural dependency: a Node.js runtime process that must be managed, monitored, and debugged separately from the main Python application.

Tree-sitter offers a compelling alternative. It provides error-tolerant parsing that handles incomplete code (a common state during active development), produces a consistent AST structure across all supported languages, and eliminates the Node helper process dependency entirely [^52^]. Tree-sitter's standardized query API (similar to CSS selectors) enables rapid extraction of definitions, call sites, imports, and references without requiring language-server-level type resolution [^50^].

The Hacker News discussion on Tree-sitter versus language servers (which includes commentary from a C# Language Designer involved in the Roslyn project) clarifies the trade-off: Tree-sitter is fundamentally a syntactic tool, not a semantic one [^48^]. It produces parse trees, not type-resolved symbol tables. For code graph construction at the granularity of the planned schema (functions, classes, methods, imports, calls, inheritance), syntactic extraction is sufficient for 80–90% of edges. The remaining accuracy gap — primarily cross-module call resolution — can be addressed through a future "Hybrid LSP" pass following Codebase-Memory's two-layer approach [^53^].

| Criterion | Python AST (Python) | ts-morph (TypeScript) | Tree-sitter (TypeScript) |
|:---|:---|:---|:---|
| **Error Tolerance** | Moderate | Good | Excellent (incremental recovery) [^52^] |
| **Semantic Depth** | High (decorators, types, dataclasses) | Very high (full type system) | Moderate (syntactic only) [^48^] |
| **Runtime Dependency** | None (stdlib) | Node.js helper process | None (Python bindings: `tree-sitter`) |
| **Cross-Language Consistency** | N/A (Python only) | N/A (TypeScript only) | High (same AST structure for 100+ langs) [^59^] |
| **Query API** | `ast.NodeVisitor` | ts-morph type API | Tree-sitter query language [^50^] |
| **Incremental Parsing** | No | No | Yes (tree reuse on edits) [^52^] |
| **Path to New Languages** | Per-language parser | Per-language parser | Single grammar file per language |
| **Performance** | Fast | Moderate (Node startup) | 100+ files/second [^34^] |

Table 4: Parser strategy comparison matrix. Python AST is retained for Python due to superior semantic depth. For TypeScript, Tree-sitter eliminates the Node helper process dependency while providing error tolerance and a path to multi-language support.

#### 2.4.3 Long-Term: Tree-sitter as the Universal Parser Foundation

The Tree-sitter project maintains grammars for hundreds of programming languages, with the official parser list documenting well over 200 individual language grammars at various maturity levels [^59^]. Codebase-Memory vendors 158 Tree-sitter grammars compiled directly into its static C binary [^53^]; code-graph-rag-mcp supports 10 languages with "complete" or "advanced" coverage, including TypeScript/JavaScript (complete), Python (95%), C/C++ (90%), C# (90%), Rust (90%), Go (90%), and Java (90%) [^34^]. The maturity of individual grammars varies — core languages like Python, JavaScript, TypeScript, C, and Rust have mature, actively maintained grammars, while niche languages may have less complete coverage. For a code hygiene tool targeting primarily Python and TypeScript initially, with potential expansion to Go, Rust, and Java, this coverage is more than sufficient.

Adopting Tree-sitter for TypeScript creates a structural precedent: adding support for Go, Rust, Java, C#, C, or C++ requires only a grammar file and a query definition, not a new language adapter with its own runtime dependencies. This path aligns with the architecture's stated principle that language parsing should be treated as an adapter, not the core identity. The graph schema's design — with common node labels (`CodeFunction`, `CodeClass`, `CodeMethod`) and language-specific properties — already accommodates this multi-parser architecture.

The specific recommendation for TypeScript is to use the `tree-sitter-typescript` grammar (which handles both TypeScript and TSX) with Tree-sitter query files that extract the node types defined in the TypeScript adapter plan: functions, async functions, classes, methods, interfaces, type aliases, React components, hooks, and route files. Tree-sitter's query language — which uses CSS-selector-like patterns to match nodes in the AST — provides a declarative mechanism for extracting these constructs without writing imperative traversal code [^50^]. For example, a query to capture all function declarations in TypeScript is a concise pattern that the Tree-sitter engine matches efficiently against the parse tree. The Python core would invoke the Tree-sitter parser directly through Python bindings (`tree-sitter` and `tree-sitter-typescript` packages on PyPI), eliminating the Node helper process and its associated operational complexity while maintaining a clean adapter contract.

### 2.5 Gap: Community Detection and Architecture Views

#### 2.5.1 No Planned Capability for Macro-Level Codebase Organization

The architecture plan does not include any mechanism for analyzing macro-level codebase organization — module boundaries, clustering patterns, or high-level architecture views. This is a significant gap because agents exploring large codebases (thousands of files, hundreds of modules) benefit enormously from understanding the structural "landscape" before diving into individual files.

Codebase-Memory addresses this through the `get_architecture` MCP tool, which returns an architecture summary derived from community detection on the code graph [^14^]. The tool enables agents to answer questions such as "what are the main modules in this codebase?" and "how are they related?" without requiring file-by-file exploration. Codebase-Memory's evaluation demonstrated that answering architecture overview questions from the graph consumed approximately 1,500 tokens versus 100,000 tokens via file-by-file search — a 67x reduction, the smallest savings among the five query types tested but still substantial for large repositories [^53^].

#### 2.5.2 Louvain Community Detection for Code Graphs

Codebase-Memory applies the Louvain community detection algorithm to the code graph to identify module clusters [^14^]. Louvain, introduced by Blondel et al. in 2008, is a greedy optimization algorithm that maximizes modularity — a metric measuring the density of edges within communities relative to edges between communities [^45^]. The algorithm proceeds in two iterative phases: first, each node is assigned to its own community and local moves that improve modularity are applied; second, communities are collapsed into super-nodes and the process repeats on the compressed graph [^46^].

For code graphs, communities detected by Louvain typically correspond to functional modules: a set of files with dense internal dependencies (many imports, calls, and inheritance relationships within the group) and sparse external dependencies (fewer connections to other groups). The Louvain method is particularly suitable for this application because it scales efficiently to graphs with millions of nodes and edges, runs in approximately $O(n \cdot \log n)$ time, and produces a hierarchical community structure that captures nested module relationships [^46^].

Louvain is not the only viable algorithm. Label propagation is faster but produces unstable results across runs. Spectral clustering provides mathematically rigorous partitions but requires eigenvector computations that scale poorly. Girvan-Newman is conceptually elegant but computationally expensive for large graphs [^45^]. For code graph applications, Louvain strikes the best balance between scalability and partition quality.

#### 2.5.3 Recommendation: Integrate Louvain or Label-Propagation Community Detection

The recommendation is to integrate community detection into the graph build pipeline, running Louvain (or label propagation as a faster alternative) after the initial graph construction completes. The detected communities should be stored as community annotations on nodes (e.g., a `community_id` property) and exposed through two new MCP tools: `code_graph_get_architecture` (returning a summary of communities, their sizes, and inter-community coupling) and `code_graph_list_communities` (returning detailed community membership and internal structure).

For Neo4j backends, the Neo4j Graph Data Science library provides a built-in `gds.louvain` procedure that can be executed directly within the database. For SQLite backends, the `python-louvain` or `igraph` Python libraries provide equivalent functionality that operates on the graph extracted from the database. The incremental synchronization mechanism (Section 2.3) should trigger selective re-computation of community assignments for affected regions when files change, rather than re-running the full algorithm — matching Codebase-Memory's approach of incremental Louvain re-computation [^14^].

The practical impact of this capability is substantial. Architecture summaries help agents navigate unfamiliar codebases efficiently, reducing onboarding time for new team members from hours of file exploration to minutes of structured graph queries. Community boundaries identify natural module boundaries for refactoring discussions, and inter-community edge density highlights coupling hotspots that may indicate architectural drift — where two modules that should be independent have developed excessive dependencies. When combined with the hybrid search capability (Section 2.2) and the expanded tool surface (Section 2.1), community detection transforms the code graph from a static dependency map into a dynamic, navigable, and analytically rich knowledge base for AI-assisted software engineering. The three capabilities together — expanded tools, hybrid search, and community detection — address the most significant functional gaps relative to state-of-the-art code intelligence systems identified in this analysis.


---

## 3. Storage Backend Reconsideration

The architecture plan designates Neo4j as the primary production backend for code graph storage. This section evaluates that choice against SQLite augmented with vector search extensions—an approach that has gained traction among state-of-the-art code intelligence tools. The analysis proceeds from Neo4j's capabilities and constraints, through the SQLite-based alternative demonstrated by reference implementations, to a recommendation for a dual-backend strategy.

### 3.1 Neo4j as Planned Primary Backend

#### 3.1.1 Advantages: Native Graph Traversal and Production-Grade Capabilities

Neo4j remains the most mature property graph database management system available, and the planned architecture's choice to build on it carries several structural advantages that are difficult to replicate with embedded alternatives.

**Native graph traversal and Cypher query language.** Neo4j's storage engine is purpose-built for graph-structured data, using index-free adjacency to achieve constant-time traversal regardless of graph depth. The Cypher query language provides a declarative, pattern-matching syntax that expresses multi-hop graph traversals with considerably less complexity than the recursive Common Table Expressions (CTEs) required in relational databases. For code analysis workloads—where understanding call chains, inheritance hierarchies, and import dependency paths demands deep traversal—this native graph capability is a genuine architectural fit. Neo4j 5.x further optimized deep traversal with faster k-hop queries delivering up to 1000× speed improvement for multi-hop patterns [^78^].

**Vector index support (version 5.13+).** Neo4j introduced native vector indexes in version 5.13 (October 2023), using Lucene's Hierarchical Navigable Small World (HNSW) implementation for approximate nearest neighbor (ANN) search [^78^] [^80^]. This enables semantic search over code embeddings directly within the graph database, eliminating the need for a separate vector store. The vector indexes support pre-filtering and post-filtering, with metadata filtering capabilities under active development [^80^]. For the planned architecture, which currently lacks any semantic search capability, this feature offers a path toward hybrid retrieval (deterministic graph traversal + semantic similarity) without introducing additional infrastructure components.

**Mature ecosystem and operational tooling.** Neo4j's Graph Data Science (GDS) library provides production-grade implementations of community detection algorithms including Louvain, Label Propagation, and Weakly Connected Components [^92^] [^93^]. These algorithms enable macro-scale architectural analysis—identifying module clusters and measuring centrality—that complements the micro-scale code facts extracted by language adapters. The GDS library operates on in-memory graph projections, processing graphs with hundreds of billions of nodes through highly parallelized implementations [^92^]. Drivers for Java, JavaScript, .NET, Go, and Python; monitoring integrations; and backup utilities reduce operational risk for production deployments.

**Production-grade clustering and high availability.** Neo4j Enterprise Edition supports causal clustering with multi-zone deployment, automatic failover, and read replica scaling [^84^]. The managed cloud offering, AuraDB Business Critical, provides a 99.95% service level agreement (SLA), role-based access control (RBAC), single sign-on (SSO) integration, and point-in-time recovery [^73^]. For multi-user or enterprise deployments where concurrent access, data durability, and administrative control are non-negotiable, these capabilities establish a clear differentiation from embedded alternatives.

#### 3.1.2 Disadvantages: Deployment Complexity and Licensing Constraints

Despite its technical strengths, Neo4j imposes operational and financial costs that must be weighed against the deployment context of the Code Hygiene MCP server.

**Separate service deployment.** Neo4j requires a dedicated database process, whether self-managed or consumed as a managed cloud service. This introduces network boundaries, connection management, authentication configuration, and version maintenance that an embedded database avoids. For a developer tool intended to run locally alongside an IDE or editor, requiring a separate Neo4j instance—potentially in a container or as a system service—represents a meaningful increase in setup complexity. The architecture plan acknowledges this tension by already including `MemoryGraphStore` and `JsonGraphStore`/`SQLiteGraphStore` for local development workflows, with Neo4j positioned as the production backend.

**Operational complexity.** Self-managed Neo4j deployments require capacity planning, JVM tuning, backup strategy implementation, and upgrade coordination. The Block Format storage engine introduced in Neo4j 5.17 improves performance for large databases but requires offline conversion that can take 15 minutes for a 180 GB database on high-end hardware [^79^]. Monitoring, log forwarding, and security hardening add further operational surface area. For teams without existing graph database operational expertise, these costs are non-trivial.

**Licensing considerations.** Neo4j's Community Edition is free and fully functional for single-instance deployments, supporting ACID transactions, Cypher, vector indexes, and the full property graph model [^75^]. However, critical production features are restricted to Enterprise Edition: autonomous clustering, hot backups, multi-database support, RBAC, LDAP/Active Directory integration, and the Advanced On-Disk Format [^75^] [^84^]. Community Edition also limits Graph Data Science parallelization to 4 CPUs and the model catalog to 3 models [^75^].

The financial implications are material. AuraDB Professional starts at $65 per GB per month, while Business Critical—required for RBAC, SSO, and the 99.95% SLA—starts at $146 per GB per month with a 2 GB minimum [^73^] [^75^]. Self-managed Enterprise Edition licensing ranges from $3,000 to $6,000 per core annually, with typical 16-core deployments costing $80,000–$200,000+ per year [^74^]. For a developer tool running on a workstation, these costs are often prohibitive.

### 3.2 SQLite + Vector as Alternative

Two reference implementations demonstrate that SQLite with vector search can serve as a viable backend for code graph storage, particularly for local-first workflows.

#### 3.2.1 code-graph-rag-mcp's SQLite + sqlite-vec Approach

code-graph-rag-mcp (Dimension 3 analysis) uses SQLite with the sqlite-vec extension as its sole database layer. This choice yields several practical advantages relevant to the Code Hygiene MCP architecture. The deployment model requires zero configuration: the database is a single file that requires no separate service, no network port allocation, and no authentication setup. Query latency remains below 100 ms for combined graph and vector operations, with a memory footprint of approximately 65 MB (reported in Dimension 3 analysis). The sqlite-vec extension supports brute-force k-nearest neighbor (KNN) search with quantization techniques (int8 reducing storage 4×, binary reducing 32×) that extend viable scale to hundreds of thousands of vectors [^47^].

The adjacency-list model used by code-graph-rag-mcp—storing entities and relationships in separate SQL tables with foreign key references—demonstrates that graph-structured data can be queried effectively within a relational model for the scale of individual codebases. The 26 MCP methods exposed by this tool (versus the 5 currently planned) operate entirely over this SQLite backend without apparent performance limitations.

However, sqlite-vec currently implements only brute-force vector search; ANN indexes such as HNSW or IVF are not yet available, though they are on the project's roadmap [^47^] [^85^]. For codebases with hundreds of thousands of embedded code snippets, this limits semantic search performance to linear scan behavior. Extensions such as vectorlite, which provides HNSW-backed ANN search via hnswlib integration, offer a potential upgrade path for SQLite deployments requiring faster approximate search [^91^].

#### 3.2.2 Codebase-Memory's SQLite Approach

Codebase-Memory (arXiv:2603.27277) takes the SQLite approach even further, achieving sub-millisecond query latency across 158 programming languages with a single statically linked C binary and zero runtime dependencies. Its custom Cypher-like query engine operates over an SQLite database, demonstrating that the performance ceiling for local-first code graph storage is substantially higher than naive assumptions might suggest. The tool uses Tree-sitter for parsing, Louvain community detection for architecture summarization, and XXH3 content hashing for incremental synchronization—all within the same SQLite-backed deployment.

The performance benchmarks are instructive: the MCP Agent variant achieves a quality score of 0.83 with approximately 1,000 tokens per question and 2.3 tool calls per question, compared to 10,000 tokens for file-based exploration. This confirms that SQLite-backed code graphs can deliver both low latency and high retrieval quality at the scale of individual developer workflows.

#### 3.2.3 Trade-off Analysis

Table 1 compares Neo4j and SQLite (+ vector extension) across dimensions relevant to the Code Hygiene MCP deployment context. The scoring reflects the specific requirements of a code intelligence tool rather than generic database workloads.

| Dimension | Neo4j (Community/Enterprise) | SQLite + sqlite-vec | Assessment |
|:---|:---|:---|:---|
| **Deployment complexity** | Requires separate service; container or system install | Single file; zero configuration; application-embedded | SQLite advantage decisive for local workflows |
| **Graph traversal depth** | Native index-free adjacency; O(1) per hop | Adjacency-list SQL queries; recursion via CTEs | Neo4j advantage grows with path depth [^78^] |
| **Vector search** | HNSW ANN index (Lucene-based); ≤4K dimensions [^80^] | Brute-force KNN; quantization supported; ANN on roadmap [^47^] | Neo4j advantage for >100K vectors; comparable below |
| **Query latency (local)** | Sub-10 ms for optimized traversals | <100 ms (code-graph-rag-mcp); <1 ms (Codebase-Memory) | Both sufficient for interactive use |
| **Memory footprint** | 512 MB–2 GB+ JVM heap typical | ~65 MB (code-graph-rag-mcp) | SQLite advantage for resource-constrained environments |
| **Community detection** | GDS library: Louvain, PageRank, WCC, etc. [^92^] | Requires external implementation | Neo4j advantage for architecture analysis |
| **Clustering/HA** | Causal clustering (Enterprise only) [^84^] | WAL mode enables concurrent reads; single writer [^97^] | Neo4j decisive for multi-user production |
| **Licensing cost (production)** | $65–$146/GB/month (AuraDB) or $3,000–$6,000/core/year (self-managed) [^73^] [^74^] | Public domain (SQLite) + MIT (sqlite-vec) | SQLite advantage decisive for cost-sensitive deployments |
| **Scale ceiling** | Billions of nodes/relationships (34B in Community) [^88^] | Millions of nodes practical; file-size limited | Neo4j advantage for very large monorepos |
| **Operational expertise required** | High (JVM tuning, clustering, backup strategy) | Minimal (file permissions, WAL checkpointing) | SQLite advantage for developer-self-serve |

The analysis reveals a clear pattern: Neo4j's advantages concentrate in enterprise-grade capabilities—clustering, deep analytics, and massive scale—while SQLite's advantages center on deployment simplicity, cost efficiency, and low operational overhead. For a tool targeting individual developers and small teams, where the graph typically contains tens of thousands to low millions of nodes, SQLite provides sufficient query capability at a fraction of the deployment cost. For enterprise deployments with multiple concurrent users, large monorepos, or requirements for high availability and centralized administration, Neo4j remains the appropriate choice.

### 3.3 Recommendation: Dual-Backend Strategy

The evaluation supports a dual-backend strategy that preserves Neo4j as the production-grade option while elevating SQLite to a first-class backend for development, testing, and local-first deployments. This approach aligns with the architecture plan's existing adapter-based persistence design and requires no structural changes to the core abstraction layer.

#### 3.3.1 Leverage the Existing GraphStore Adapter Protocol

The architecture plan defines a `GraphStore` protocol with four operations—`delete_graph`, `add_nodes_batch`, `add_edges_batch`, and `query`—that is deliberately backend-agnostic. Neo4j implements this interface via Cypher over the Bolt protocol, while an SQLite implementation would translate the same operations to SQL against adjacency-list tables. The plan already envisions multiple store implementations including `MemoryGraphStore`, `JsonGraphStore` or `SQLiteGraphStore`, `Neo4jGraphStore`, and `LumenGraphStore`. The recommended path is to fully realize the `SQLiteGraphStore` implementation rather than treating it as a secondary option.

The protocol's simplicity is a strength here. The `query` method accepts a query string and parameters, allowing each backend to expose its native query dialect—Cypher for Neo4j, SQL for SQLite—while the retrieval layer presents a uniform interface to the MCP tool layer. This pattern translates naturally to SQLite with equivalent table structures.

#### 3.3.2 SQLite as Default for Development and Testing; Neo4j for Production

The recommended deployment model assigns each backend to the context where its strengths are maximized:

**SQLite as default.** For individual developers running the MCP server locally, SQLite should be the default backend. The zero-configuration setup eliminates the need for Docker, system services, or cloud accounts. The database file lives alongside the project or in a configurable local directory, enabling effortless project isolation. Testing benefits from in-memory SQLite databases that initialize instantly and leave no artifacts. Cross-verification across code-graph-rag-mcp, Codebase-Memory, and Cognee confirms that SQLite with vector extensions is a viable alternative to dedicated graph databases for local-first tools.

**Neo4j for production persistent retrieval.** For team-shared deployments, continuous integration pipelines requiring durable graph state, or large monorepos where deep traversal performance is critical, Neo4j remains the recommended production backend. The clustering, backup, and administrative capabilities of Enterprise Edition (or AuraDB Business Critical for managed deployments) justify the operational investment in these contexts [^73^] [^84^].

Table 2 maps recommended backend choices to deployment scenarios.

| Deployment Scenario | Recommended Backend | Rationale |
|:---|:---|:---|
| Individual developer, local IDE | SQLite (default) | Zero setup; single-file project isolation; sufficient performance for typical codebases |
| Automated testing / CI | SQLite (in-memory or file) | Fast initialization; no external dependencies; reproducible state |
| Small team, shared graph (< 500K nodes) | SQLite (network-accessible file or litefs) | Low operational overhead; adequate performance; zero licensing cost |
| Large monorepo, deep traversal workloads | Neo4j (Community or Enterprise) | Superior multi-hop traversal; GDS analytics; scale headroom |
| Multi-user production, high availability | Neo4j Enterprise / AuraDB Business Critical | Clustering; RBAC; backup/restore; 99.95% SLA [^73^] |
| Enterprise, compliance requirements | Neo4j AuraDB Virtual Dedicated Cloud | VPC isolation; private endpoints; customer-managed encryption [^74^] |

#### 3.3.3 Implement SQLiteGraphStore with sqlite-vec for Semantic Search

The concrete implementation path involves extending the existing `graphstores/` module with a production-quality `SQLiteGraphStore` that includes vector search via sqlite-vec. The schema design follows the adjacency-list pattern demonstrated by code-graph-rag-mcp: an `entities` table storing node properties with an embedding blob, a `relationships` table storing typed edges, and a vec0 virtual table for vector search operations [^47^]. Node and relationship types from the planned schema map directly to discriminating columns with foreign key constraints.

Semantic search capability addresses a critical gap in the planned architecture: neither code-graph-rag-mcp nor Codebase-Memory rely on a separate vector store, yet both offer natural language code queries. Integrating sqlite-vec into SQLiteGraphStore makes hybrid retrieval available across both backends. The embedding provider architecture—supporting local models via Ollama or transformers, cloud providers via OpenAI, or memory-based fallbacks—can be shared between Neo4j and SQLite implementations, with only the storage and indexing layer varying.

For graph traversal, the SQLite implementation uses recursive CTEs to follow relationship chains. While asymptotically slower than Neo4j's index-free adjacency for deep traversals, empirical data from both reference tools indicates latency remains acceptable for shallow-to-medium depth queries typical of code analysis, where call depth rarely exceeds 10–20 hops. The 100 ms query ceiling reported by code-graph-rag-mcp and the sub-millisecond performance of Codebase-Memory confirm that SQLite is not a bottleneck for this workload class.

The dual-backend strategy preserves the architecture's extensibility while aligning deployment complexity with operational context. It requires no changes to existing adapter contracts; rather, it elevates an already-envisioned store implementation to parity with the Neo4j backend, giving operators a genuine choice based on scale, team size, and infrastructure constraints.


---

## 4. Temporal Model and Code Evolution

### 4.1 Graphiti's Temporal Approach

Graphiti, the open-source temporal knowledge graph engine that powers Zep's memory layer, implements one of the most sophisticated temporal data models available in production knowledge graph systems [^6^]. Its approach rests on three pillars: bi-temporal timestamping, non-destructive edge invalidation, and episode-based provenance tracking. Understanding these mechanisms is a prerequisite for evaluating whether they merit adaptation to a code hygiene context.

**Bi-temporal model.** Every edge in Graphiti carries four timestamps drawn from two orthogonal timelines [^35^]. The *valid time* timeline $T$ tracks when a fact held true in the real world, expressed through `valid_at` and `invalid_at` fields. The *transaction time* timeline $T'$ tracks when the system learned of the fact, expressed through `created_at` and `expired_at` fields. This separation enables Graphiti to distinguish between "the fact became false" and "we discovered the fact was false"—a distinction that matters when data arrives out of chronological order or when retroactive corrections are applied [^60^]. In the Zep paper, the authors characterize this bi-temporal approach as "a novel advancement in LLM-based knowledge graph construction" that underpins the system's capacity for precise temporal reasoning [^35^].

**Edge invalidation.** When a newly ingested fact contradicts an existing edge, Graphiti does not delete the old edge. Instead, it stamps the edge with an `invalid_at` timestamp equal to the `valid_at` of the superseding edge, and sets `expired_at` to the current system time [^37^]. The invalidated edge remains in the graph and remains queryable for historical state reconstruction. Graphiti automates this process by employing an LLM to compare new edges against semantically related existing edges, identifying temporally overlapping contradictions and resolving them according to a transactional-priority rule that favors more recently ingested information [^35^]. This preservation of historical edges is central to Graphiti's value proposition: agents can query "what is true now" or "what was true at time $t$" against the same graph structure [^13^].

**Episode-based provenance.** Every derived fact in Graphiti traces back to one or more *episodes*—raw data units such as messages, text documents, or structured JSON events [^35^]. The episodic subgraph $\mathcal{G}_e$ serves as ground truth, maintained non-lossily, while the semantic entity subgraph $\mathcal{G}_s$ contains extracted entities and relationships. Bidirectional indices connect semantic artifacts to their source episodes, enabling both forward traversal (from episode to extracted facts) and backward traversal (from fact to originating data) [^35^]. This provenance chain ensures that any derived conclusion can be audited against its source.

The performance claims for this model are substantial. Zep reports a P95 query latency of 300ms, a 18.5% accuracy improvement on LongMemEval temporal reasoning tasks, and a 90% latency reduction compared to MemGPT baseline implementations [^36^][^59^]. However, these benchmarks measure *conversational agent memory*, not code analysis workloads—a distinction that becomes critical in Section 4.2.

### 4.2 Applicability to Code Hygiene

The mapping from Graphiti's temporal model to code hygiene tracking is conceptually appealing but operationally problematic. Three specific application areas merit examination.

**Git-history-aware temporal queries.** A bi-temporal code graph could, in principle, answer questions such as "when was this circular import introduced" or "which commit created this inheritance chain." Git itself already stores this information, but it is file-oriented and line-oriented, not structure-oriented. FalkorDB Code-Graph demonstrates a related capability through its `list_commits` and `switch_commit` endpoints, which enable analysis of how code evolves across commits [^15^]. However, FalkorDB's approach operates at commit granularity—it does not attempt to track the valid-time semantics of individual relationships between commits. The gap between "this file changed in commit X" and "the CALLS edge between function A and function B was valid from commit X to commit Y" is significant, and bridging it requires parsing every commit in repository history.

**Edge invalidation for refactoring.** The edge invalidation pattern has a natural analog in code evolution. When an import statement is removed, the corresponding IMPORTS edge should be invalidated. When a function is renamed, old CALLS edges should be stamped invalid and new ones created. Graphiti's non-destructive invalidation would preserve the historical graph state, enabling queries like "which functions called this method before the refactoring." This capability is theoretically valuable for impact analysis and architectural archaeology. Independent technical analysis of Graphiti notes that its design targets agent memory workloads rather than code analysis, and its full temporal invalidation pipeline has not been demonstrated in production code intelligence tools [^37^].

**Cost-benefit analysis.** The bi-temporal model extracts a substantial operational toll. Graphiti's ingestion pipeline fires multiple LLM calls per episode: node extraction, entity resolution (with a MinHash and Locality-Sensitive Hashing fast path plus an LLM fallback), edge extraction, per-edge deduplication, temporal extraction, and contradiction resolution [^37^]. As one independent analysis noted, "the cost is high... this is the expensive end of the spectrum" [^37^]. A code repository generates far more "facts" per unit of data than a conversation log—every function definition, call site, import statement, and type annotation constitutes a graph edge. Applying Graphiti's full ingestion pipeline to code commits would multiply LLM invocation costs by orders of magnitude relative to deterministic AST parsing.

| Dimension | Full Bi-Temporal Model (Graphiti-style) | Lightweight Timestamp Properties | Git-Native Commit Tracking |
|---|---|---|---|
| **Storage overhead** | 4× timestamp fields + episode nodes + provenance indices | 3× timestamp fields per node/edge | Zero additional graph fields; relies on git |
| **Ingestion cost** | Multiple LLM calls per commit for extraction, resolution, invalidation | Deterministic property assignment during AST parsing | Git diff only; no graph modifications |
| **Query capability** | Point-in-time graph state reconstruction; temporal reasoning | First/last seen queries; commit attribution | File-level history; no structural edge history |
| **Implementation complexity** | High: requires edge invalidation logic, contradiction detection, episode management | Low: property additions to existing schema | Minimal: shell out to git |
| **LLM dependency** | Required for temporal extraction and invalidation | None | None |
| **Provenance depth** | Full episode-to-fact lineage | Commit SHA attribution only | Git native blame/annotate |
| **Benchmark precedent** | 300ms P95 query latency; validated on conversational memory [^36^] | Common pattern in property graphs; no benchmarks | Universal; git blame is standard tooling [^64^] |

The table above frames the decision as a three-way trade-off. The full bi-temporal model offers the richest query semantics at the highest implementation and runtime cost. Lightweight timestamp properties provide commit attribution without the computational overhead of invalidation logic. Git-native tracking delegates all historical queries to the version control system, sacrificing structural graph history for zero additional complexity.

For the Code Hygiene MCP's core use cases—hygiene checking, dependency tracing, and context generation—the evidence does not support the full bi-temporal model as a priority. Analysis of Graphiti's architecture highlights significant computational overhead from LLM-based entity extraction and relationship inference, making it costly at the scale of code repository facts [^29^]. Surveying the available tool landscape, FalkorDB Code-Graph provides commit-level history through git endpoints but no temporal modeling of code changes [^15^]; other tools in this space achieve competitive benchmarks for retrieval and dependency analysis without bi-temporal edge invalidation [^113^]. The principal reason is that git already provides authoritative temporal data at commit granularity; duplicating that history inside the graph with semantic-level invalidation constitutes overlapping functionality with marginal incremental value for hygiene-oriented queries.

### 4.3 Recommendation: Lightweight Temporal Tracking

The recommended approach is to add three timestamp properties to nodes and edges: `first_seen_at`, `last_modified_at`, and `source_commit`. These properties are populated deterministically during AST extraction by cross-referencing parsed entities with git blame data, requiring no LLM involvement and no edge invalidation logic.

**Schema extension.** Each node and edge in the graph schema (Section 3) receives three optional properties. The `first_seen_at` property records the commit timestamp when the entity or relationship was first extracted. The `last_modified_at` property records the most recent commit that touched the associated source code. The `source_commit` property stores the full commit SHA for traceability. These properties enable queries such as "find all functions introduced more than 180 days ago that have never been modified" or "identify IMPORTS edges added since the last release commit."

**Git blame integration.** During file extraction, the language adapter runs `git blame --porcelain` on the parsed file to obtain per-line commit metadata, including the commit SHA, author timestamp, and committer timestamp [^68^]. This metadata is mapped to AST node line ranges to assign `source_commit` and `first_seen_at` values. For edges, the `source_commit` is set to the commit in which the edge was first observed (typically the more recent of the two endpoint nodes). The incremental indexing strategy from Section 3 only re-parses changed files, so timestamp updates are naturally scoped to modified content. The `git blame` command is standard tooling available in all git installations, and its porcelain output format is stable and machine-parseable [^64^].

**Deferred invalidation.** Full temporal edge invalidation—modeled on Graphiti's contradiction detection and stamping pattern—is explicitly deferred to a future version. This deferral is not a permanent rejection; it is a sequencing decision based on the principle that core use cases should drive architectural complexity. If post-deployment usage demonstrates that users need to query structural graph state at historical commits (e.g., "show me the call graph as it existed before the v2.3 refactoring"), then edge invalidation can be introduced as a non-breaking schema extension. Until that need is validated, the cost of implementing and maintaining invalidation logic—LLM-based contradiction detection, temporal extraction prompts, and episode management infrastructure—outweighs the benefit for a hygiene-checking tool.

This lightweight approach preserves the ability to reason about code age, modification recency, and commit attribution, which addresses the most common temporal queries in code maintenance workflows. It does so without introducing the computational overhead, LLM dependency, and implementation complexity that characterize Graphiti's full bi-temporal model.


---

## 5. Refined Architecture and Implementation Priority

The preceding chapters identified five critical gaps — MCP tool surface area, semantic search, incremental synchronization, storage backend flexibility, and temporal tracking — and evaluated each against demonstrated competitive benchmarks. This chapter translates those findings into a concrete refined architecture and a sequenced implementation roadmap. Every recommendation specifies what to build, in what order, and the quantitative basis for that priority.

### 5.1 Recommended Architecture Changes

#### 5.1.1 Updated MCP Tool Set

The planned five MCP tools cover basic graph CRUD and dependency tracing but omit entire functional categories that competing systems expose. Codebase-Memory's 14 typed tools, organized across indexing, query, analysis, and code categories, demonstrated 83% answer quality at 10x fewer tokens than file-based exploration [^53^]. Code-graph-rag-mcp extends this to 26 methods with semantic search, clone detection, and hotspot analysis [^34^]. The refined architecture expands the tool surface to 14 tools, distributed across the same four categories. This represents the minimum set required to close the functional gap without incurring the interface complexity of a 26-method surface.

Table 1 inventories the complete recommended tool set, specifying each tool's name, functional category, input/output contract, and the competitive precedent validating its inclusion.

| # | Tool Name | Category | Input | Output | Competitive Precedent |
|:---|:---|:---|:---|:---|:---|
| 1 | `code_graph_index` | Indexing | `repo_path`, `force_rebuild` | `status`, `files_indexed` | Codebase-Memory [^14^], code-graph-rag-mcp [^34^] |
| 2 | `code_graph_index_status` | Indexing | `repo_path` | `last_indexed`, `file_count`, `dirty_files` | Codebase-Memory [^14^] |
| 3 | `code_graph_list_projects` | Indexing | — | `project_ids[]`, `repo_paths[]` | Codebase-Memory [^14^] |
| 4 | `code_graph_delete_project` | Indexing | `project_id` | `deleted` | Codebase-Memory [^14^] |
| 5 | `code_graph_retrieve` | Query | `node_id` or `symbol_name` | Node + edges + snippet | Planned (retained) |
| 6 | `code_graph_search_semantic` | Query | `query_text`, `limit`, `types[]` | Ranked entity list | code-graph-rag-mcp [^34^], Graphiti [^13^] |
| 7 | `code_graph_search_code` | Query | `pattern`, `language`, `limit` | Matching code snippets | Codebase-Memory [^14^] |
| 8 | `code_graph_trace_dependencies` | Query | `symbol`, `direction`, `max_depth` | Path list with edge types | Planned (retained) |
| 9 | `code_graph_impact_analysis` | Analysis | `symbol` or `file_path` | Transitive closure ranked by coupling | code-graph-rag-mcp [^34^] |
| 10 | `code_graph_detect_changes` | Analysis | `since_ref` (git ref or timestamp) | Added, modified, deleted entities | Codebase-Memory [^14^] |
| 11 | `code_graph_find_hotspots` | Analysis | `top_n`, `metric` (complexity/coupling) | Ranked hotspot list with scores | code-graph-rag-mcp [^34^] |
| 12 | `code_graph_get_architecture` | Analysis | — | Community summary: modules, sizes, coupling | Codebase-Memory [^14^] |
| 13 | `code_graph_list_communities` | Analysis | `community_id` (optional) | Community members + internal structure | Codebase-Memory [^14^] |
| 14 | `code_graph_inspect_file` | Code | `file_path` | Full graph subgraph for file | Planned (retained) |

The indexing category expands from one tool to four, adding project lifecycle management that competing systems treat as essential for multi-repository workflows. The query category gains `code_graph_search_semantic` and `code_graph_search_code`, addressing the most significant functional gap identified in Chapter 2 — the absence of any non-deterministic retrieval mechanism. The analysis category is entirely new: six tools that expose structural intelligence (impact analysis, change detection, hotspot identification, architecture summarization, and community enumeration) that agents require for non-trivial code maintenance tasks. The code category retains the existing file inspection tool without change. Each tool maps to a specific query pattern validated in competitor benchmarks, ensuring that the expansion is need-driven rather than speculative.

#### 5.1.2 Hybrid Search Layer

The refined architecture adds a hybrid search layer that combines three retrieval modalities: vector embeddings for semantic similarity, BM25 keyword indexing for exact and partial text matches, and graph traversal for relationship-aware context. This three-mode design follows Graphiti's proven architecture, which achieves P95 query latency of 300 ms and 94.7% accuracy on the LOCOMO benchmark by operating all three modes in parallel and merging results through a weighted ranking function [^13^][^57^].

The vector embedding component uses a provider-based architecture supporting local models (via Ollama or HuggingFace transformers), API-based models (OpenAI, Azure, Gemini), and a memory-based fallback for offline operation. This provider pattern, demonstrated in code-graph-rag-mcp's embedding architecture [^34^], decouples embedding generation from storage and enables deployment-specific model selection without code changes. For code-specific retrieval, models fine-tuned on code corpora (such as CodeBERT-derived sentence transformers) should be preferred over general-purpose embedding models, as they capture programming-language semantics more accurately.

The storage backend determines the vector index implementation. For SQLite deployments, sqlite-vec provides brute-force k-nearest neighbor search with int8 quantization (4x storage reduction) and binary quantization (32x reduction), achieving query times of 17 ms for 100,000 vectors on M1-class hardware and 3.97 ms with preloading [^44^][^47^]. For Neo4j deployments, native HNSW vector indexes (available from version 5.13) provide approximate nearest neighbor search with sub-10 ms latency and support for up to 4,096-dimensional vectors [^78^][^80^]. Both implementations share the same embedding provider interface; only the index storage and query execution differ.

The BM25 keyword index serves as a fallback for queries where exact terminology matters — searching for a specific function name, API endpoint, or error message that may not align with the semantic vector of the query. Graph traversal provides relationship context: when a semantic search identifies a relevant function, graph traversal expands the result to include its callers, callees, and type dependencies. The merged result set presents the agent with both the directly matched entity and its structural neighborhood, reducing the need for follow-up queries.

#### 5.1.3 Incremental Sync Module

The refined architecture elevates incremental synchronization from an unspecified future capability to a core module with defined contracts and implementation requirements. The design follows Codebase-Memory's validated approach: XXH3 content hashing for change detection, adaptive polling for file system monitoring, and file-level re-parsing with delta application for graph updates [^14^].

The incremental sync module extends the `GraphStore` adapter protocol with two operations: `get_stored_hash(file_path) -> str | None` and `update_hash(file_path, hash) -> None`. The watcher loop runs as a background asyncio task, polling the repository at a configurable interval (default 2 seconds). When a file's XXH3 hash differs from the stored value, the module invokes the language adapter's `extract_file` method for that file only, deletes nodes and edges previously associated with that file from the graph store, and inserts the new delta within a single transaction. XXH3 achieves approximately 30 GB/s throughput, making the hashing overhead negligible even for large codebases [^14^].

The adaptive polling strategy adjusts the interval dynamically: increasing it when no changes are detected for a period (reducing CPU usage during idle editing) and decreasing it when changes are frequent (improving responsiveness during active development). This approach avoids the portability issues of OS-specific file system event APIs — FSEvents on macOS, inotify on Linux, ReadDirectoryChanges on Windows — while delivering equivalent responsiveness [^14^]. A manual re-index trigger is also exposed through the `code_graph_index` tool for CI/CD integration and deterministic rebuilds.

The incremental sync module is classified as a core requirement rather than a future enhancement because an out-of-date graph produces incorrect dependency traces, stale architecture views, and misleading impact analysis results. Without incremental synchronization, the system forces a choice between graph freshness (continuous re-indexing, excessive CPU and I/O) and accuracy (infrequent re-indexing, stale data). Neither outcome is acceptable for a tool operating as a persistent structural analysis backend in live coding workflows.

#### 5.1.4 Community Detection Module

The refined architecture adds a community detection module that applies the Louvain algorithm to the code graph to identify module clusters. Louvain maximizes modularity — the density of edges within communities relative to edges between communities — through a greedy optimization that scales to millions of nodes in $O(n \cdot \log n)$ time [^45^][^46^]. For code graphs, detected communities typically correspond to functional modules: files with dense internal dependencies (many imports, calls, and inheritance relationships within the group) and sparse external dependencies.

Two MCP tools expose community detection results: `code_graph_get_architecture` returns a high-level summary of communities, their entity counts, and inter-community coupling densities; `code_graph_list_communities` returns detailed membership and internal structure for a specified community. Codebase-Memory's evaluation demonstrated that answering architecture overview questions from community-annotated graphs consumed approximately 1,500 tokens versus 100,000 tokens via file-by-file search — a 67x reduction [^53^].

For Neo4j backends, the module leverages the Neo4j Graph Data Science (GDS) library's built-in `gds.louvain` procedure, executing community detection directly within the database on in-memory graph projections [^92^]. For SQLite backends, the `python-louvain` or `igraph` libraries provide equivalent functionality operating on graph data extracted from the database. The incremental sync module triggers selective re-computation of community assignments for regions affected by changed files, rather than re-running the full algorithm on every edit — matching Codebase-Memory's incremental Louvain approach [^14^].

### 5.2 Updated Milestone Sequence

#### 5.2.1 Prioritize MCP Tool Expansion and Semantic Search Before TypeScript Adapter

The original milestone sequence positioned the TypeScript adapter (Milestone 6) before retrieval and MCP parity (Milestone 4). The refined sequence inverts this priority: MCP tool expansion and semantic search are elevated to Milestone 3, while the TypeScript adapter is deferred to Milestone 5. This reordering reflects the finding that the functional gap — 5 tools versus 14–26 in competing systems, and zero semantic search capability — has a larger impact on agent effectiveness than language coverage breadth.

Codebase-Memory's evaluation across 31 repositories demonstrated that 14 graph-native tools achieved 83% answer quality for Python repositories alone [^32^]. The TypeScript adapter expands language coverage but does not address the structural limitation that constrains agent capability regardless of language. Semantic search, in particular, is a cross-cutting capability: once implemented, it serves all language adapters without per-language modification. The embedding provider architecture is language-agnostic by design, and vector indexes operate on code text irrespective of source language [^34^]. Prioritizing semantic search before the TypeScript adapter thus maximizes impact per engineering hour.

#### 5.2.2 Add SQLite Backend Milestone Before Neo4j Production Hardening

The refined sequence introduces a new milestone — SQLite backend with vector search — positioned before Neo4j production hardening. This insertion reflects the dual-backend strategy validated in Chapter 3: SQLite serves as the default backend for development and testing, while Neo4j remains the production-grade option for team-shared deployments [^73^][^84^].

The rationale for this sequencing is developer adoption velocity. SQLite requires zero configuration: a single file, no separate service, no network port allocation, no authentication setup [^47^]. Code-graph-rag-mcp and Codebase-Memory both demonstrate that SQLite-backed code graphs deliver sub-100 ms query latency with memory footprints of approximately 65 MB — performance profiles that are more than adequate for individual developer workflows [^34^][^53^]. Building the SQLiteGraphStore implementation first provides immediate value to the primary user demographic (individual developers running the MCP server locally) while the Neo4j hardening milestone addresses enterprise requirements. The existing `GraphStore` adapter protocol already supports this insertion without structural changes; the SQLite implementation is a new adapter, not a protocol modification.

#### 5.2.3 Incremental Sync as Core Requirement

The refined sequence positions incremental synchronization as a dependency of the indexing milestone rather than a post-production enhancement. The incremental sync module must be operational before the system is deployed in interactive development workflows because graph freshness is not a performance optimization but a correctness requirement. Impact analysis, architecture views, and dependency traces are all invalid if the underlying graph does not reflect the current file system state.

The implementation sequence within the incremental sync milestone proceeds as follows: (1) extend the `GraphStore` protocol with hash storage operations, (2) implement the XXH3-based change detector, (3) build the adaptive polling watcher loop, (4) wire file-level re-parsing into the delta application pipeline, and (5) integrate incremental community re-computation. Each step has a defined completion criterion: step 1 is complete when all graph store adapters (memory, JSON, SQLite, Neo4j) pass hash read/write tests; step 5 is complete when community assignments update within 5 seconds of a file change in a 1,000-file repository.

### 5.3 Implementation Priority Matrix

Table 2 ranks all architectural recommendations by a composite score derived from two dimensions: user impact (the quantitative improvement in agent capability or developer experience) and implementation effort (engineering weeks estimated from competitive precedent and architectural complexity). The resulting four quadrants map to MoSCoW prioritization: Must-Have, Should-Have, Could-Have, and Won't-Have.

| Priority | Recommendation | Impact | Effort | Weeks | Key Metric |
|:---|:---|:---|:---|:---|:---|
| **Must-Have** | MCP tool expansion (5 → 14 tools) | High: closes 2–5x tool gap vs competitors [^34^][^14^] | Medium: tool handlers reuse existing query logic | 3–4 | 14 tools across 4 categories |
| **Must-Have** | Hybrid semantic search (vector + BM25 + graph) | High: 120x token reduction vs file search [^53^]; P95 300 ms latency [^13^] | Medium: provider-based embeddings; backend-specific indexes | 3–4 | <500 ms query latency |
| **Must-Have** | Incremental sync (XXH3 + adaptive polling) | High: correctness requirement for live workflows; 30 GB/s hash throughput [^14^] | Medium: protocol extensions + watcher loop | 2–3 | <5 s sync latency at 1K files |
| **Should-Have** | Tree-sitter for TypeScript (replace ts-morph) | High: eliminates Node dependency; error-tolerant parsing [^52^]; path to 100+ languages | Medium: grammar + query files; parity re-validation | 3–4 | TypeScript adapter passes 95% parity |
| **Should-Have** | Community detection (Louvain algorithm) | Medium: 67x token reduction for architecture queries [^53^]; module clustering | Low: GDS builtin (Neo4j); python-louvain (SQLite) | 1–2 | Community assignments in <2 s |
| **Could-Have** | Lightweight temporal tracking (`first_seen_at`, `last_modified_at`, `source_commit`) | Medium: enables code age queries; commit attribution [^64^] | Low: schema property additions + git blame integration | 1–2 | 3 properties on all nodes/edges |
| **Could-Have** | SQLite backend with sqlite-vec (elevated to first-class) | High for adoption: zero-config deployment [^47^]; 65 MB footprint [^34^] | Medium: adjacency-list schema + vec0 virtual table | 2–3 | <100 ms query latency |
| **Won't-Have** | Full bi-temporal model (Graphiti-style edge invalidation) | Low for hygiene: git provides equivalent commit-level history | Very high: LLM-based contradiction detection; episode management [^37^] | 8–12 | Not targeted |

The Must-Have tier contains the three recommendations whose absence would prevent the system from achieving functional parity with state-of-the-art code intelligence tools. The MCP tool expansion closes the 2–5x surface gap that constrains agent workflows to basic CRUD operations. Hybrid semantic search addresses the most significant retrieval limitation — the inability to answer natural language queries without exact symbol names — with demonstrated 120x token efficiency gains [^53^]. Incremental synchronization is a correctness prerequisite: without it, the graph is either stale or continuously rebuilt, and neither state is acceptable for live coding workflows. Together, these three items represent 8–11 engineering weeks and should be sequenced first.

The Should-Have tier contains two recommendations with high impact but lower urgency. Tree-sitter for TypeScript eliminates the Node.js helper process dependency, enables error-tolerant parsing of incomplete code during live editing, and creates a structural precedent for adding Go, Rust, Java, and other languages through grammar files rather than custom adapters [^52^][^59^]. Community detection adds architecture summarization capabilities that reduce token consumption for macro-level codebase queries by 67x [^53^], with low implementation complexity due to production-ready Louvain implementations in both Neo4j GDS [^92^] and Python libraries [^45^]. These items represent 4–6 additional weeks and should follow the Must-Have tier.

The Could-Have tier contains two recommendations that provide meaningful value but can be deferred without blocking core functionality. The SQLite backend with sqlite-vec significantly accelerates developer adoption through zero-configuration deployment; its inclusion here rather than in Must-Have reflects the fact that the existing plan already contemplates SQLite as a secondary option, and the work elevates it to first-class status rather than introducing it from scratch [^47^]. Lightweight temporal tracking adds `first_seen_at`, `last_modified_at`, and `source_commit` properties to nodes and edges, enabling code age and commit attribution queries without the computational overhead of full bi-temporal invalidation [^64^]. These items represent 3–5 weeks.

The Won't-Have tier contains a single recommendation: the full bi-temporal model with Graphiti-style edge invalidation. While conceptually elegant for code evolution tracking, the implementation cost — LLM-based contradiction detection, episode management infrastructure, and temporal extraction pipelines — is disproportionate to the benefit for a hygiene-checking tool [^37^]. Git already provides authoritative temporal data at commit granularity, and the lightweight timestamp properties in the Could-Have tier address the most common temporal queries without replicating Graphiti's full ingestion pipeline. This item is explicitly deferred to a future major version, contingent on post-deployment usage demonstrating demand for point-in-time graph state reconstruction.

The total estimated effort for all tiers except Won't-Have is 15–22 engineering weeks. Sequenced as two development phases — Must-Have first (8–11 weeks), followed by Should-Have and Could-Have in parallel (7–11 weeks) — the refined architecture can be delivered in a single quarter of focused engineering. The sequencing within each tier matters as much as the tier assignment: semantic search should follow MCP tool expansion (the tools consume the search layer), and incremental sync should precede both (freshness is a prerequisite for correct retrieval and analysis results).


---

