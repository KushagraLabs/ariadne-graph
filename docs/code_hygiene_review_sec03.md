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
