# Dimension 4: Graphiti (Zep) Temporal Knowledge Graph Analysis

## Overview
Graphiti is an open-source temporal context graph engine for AI agents. It builds real-time knowledge graphs that track how facts change over time. It powers Zep's memory layer.

## Architecture
- **Core Engine**: Graphiti (Python, TypeScript, Go SDKs)
- **Graph Backends**: Neo4j, FalkorDB, or Kuzu (pluggable)
- **Vector Storage**: Via graph backend (Neo4j supports vector indexes)
- **LLM Integration**: Required for entity extraction, relationship inference, edge invalidation
- **MCP Server**: Available as experimental MCP server implementation

## Three-Layer Knowledge Graph
1. **Episodic Subgraph (Ge)**: Raw events/messages (ground truth, non-lossy)
2. **Semantic Entity Subgraph (Gs)**: Extracted entities and relationships
3. **Community Subgraph (Gc)**: Clusters of strongly connected entities with summaries

## Bi-Temporal Model
Every edge carries four timestamps:
- `valid_at` / `invalid_at`: When fact was true in the world (valid time)
- `created_at` / `expired_at`: When system knew about it (transaction time)

When contradicting facts arrive, old edges are invalidated (stamped with invalid_at) rather than deleted.

## Key Capabilities
1. **Temporal Fact Management**: Facts have validity windows; query current or historical state
2. **Episodes & Provenance**: Every entity/relationship traces back to source episodes
3. **Custom Ontology**: Define entity/edge types via Pydantic models (prescribed) or let emerge (learned)
4. **Incremental Construction**: New data integrates immediately without batch recompute
5. **Hybrid Retrieval**: Semantic embeddings + BM25 keyword + graph traversal (no LLM at query time)
6. **Edge Invalidation**: Automatic contradiction detection and temporal invalidation

## Performance
- P95 query latency: 300ms (Zep's implementation)
- Near-constant time access via vector + BM25 indexes
- Benchmarks: 94.8% DMR accuracy vs MemGPT 93.4%; +18.5% LongMemEval accuracy; -90% latency

## Graphiti vs. GraphRAG Comparison
| Aspect | GraphRAG | Graphiti |
|--------|----------|----------|
| Primary Use | Static document summarization | Dynamic, evolving context |
| Data Handling | Batch-oriented | Continuous, incremental |
| Knowledge Structure | Entity clusters & summaries | Temporal context graph with episodes |
| Retrieval | Sequential LLM calls | Hybrid (cosine + BM25 + BFS), no LLM |
| Temporal Handling | Basic timestamps | Rich bi-temporal tracking |
| Contradiction Handling | LLM judgment | Automatic edge invalidation |
| Query Latency | Seconds to tens of seconds | Sub-second |
| Custom Entity Types | No | Yes (Pydantic) |

## MCP Server Features
- Episode Management: add, retrieve, delete episodes
- Entity Management: search and manage nodes/relationships
- Search: semantic and hybrid search for facts and summaries
- Group Management: organize data with group_id filtering
- Graph Maintenance: clear and rebuild indices

## Relevance to Code Hygiene MCP
- Temporal model could track code evolution over git history
- Edge invalidation model could handle code refactoring
- Hybrid retrieval approach is superior to graph-only
- Custom ontology (Pydantic) aligns with planned schema
- MCP server design patterns are reference-quality
