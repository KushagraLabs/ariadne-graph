# Dimension 3: code-graph-rag-mcp (er77) Analysis

## Overview
A high-performance TypeScript-based MCP server for code graph analysis with SQLite + vector search, Tree-sitter parsing, and 26 MCP methods.

## Architecture
- **Language**: TypeScript (85.2%), JavaScript (12.1%), Python (2.0%)
- **Database**: SQLite + sqlite-vec extension (zero-config, single-file)
- **Parsing**: Tree-sitter (WebAssembly-based, error-tolerant)
- **MCP Protocol**: Full MCP server with 26 methods
- **Multi-agent system**: CollectorAgent, AnalysisAgent, SemanticAgent, ParserAgent
- **Embeddings**: Provider-based (memory/transformers/ollama/openai/cloudru)

## Key Features (26 MCP Methods)
1. **Semantic Search**: Natural language code search
2. **Code Similarity**: Duplicate & clone detection
3. **JSCPD Clone Scan**: Copy/paste detection without embeddings
4. **Impact Analysis**: Change impact prediction
5. **AI Refactoring**: Intelligent code suggestions
6. **Hotspot Analysis**: Complexity & coupling metrics
7. **Cross-Language**: Multi-language relationships
8. **Graph Health**: Database diagnostics
9. **Safe Reset**: Clean reindexing (reset_graph, clean_index)
10. **Agent Telemetry**: Runtime metrics (get_agent_metrics)
11. **Bus Diagnostics**: Knowledge bus topics (get_bus_stats, clear_bus_topic)
12. **Batched Indexing**: Resumable indexing with progress
13. **Semantic Warmup**: Configurable cache priming

## Database Schema (Adjacency List Model)
- `entities` table: id, name, type, filePath, code
- `relationships` table: sourceId, targetId, type (calls, extends, imports)
- `doc_embeddings` / `vec_doc_embeddings`: Vector search tables
- Dual-schema support for sqlite-vec and fallback modes

## Performance Claims
- Parsing: 100+ files/second (Tree-sitter)
- Query Response: <100ms (SQLite + vector search)
- Memory Usage: 65MB
- 5.5x faster than native Claude tools

## Language Support
| Language | Support Level |
|----------|--------------|
| TypeScript/JavaScript | Complete (100%) |
| Python | Advanced (95%) |
| C/C++ | Advanced (90%) |
| C# | Advanced (90%) |
| Rust | Advanced (90%) |
| Go | Advanced (90%) |
| Java | Advanced (90%) |
| VBA | Regex-based (80%) |

## Architecture Trade-offs
| Component | Choice | Alternative | Rationale |
|-----------|--------|-------------|-----------|
| API Protocol | MCP (stdio) | REST API | Stateful, persistent connection |
| Database | SQLite + sqlite-vec | Neo4j/Qdrant | Zero-config, single-file, private |
| Code Parsing | Tree-sitter | Regex/Language-specific | Universal, fast, error-tolerant |
| Graph IDs | SHA256-based | Auto-increment | Deterministic, stable |

## Key Innovations Relative to Code Hygiene MCP Plan
- SQLite + vector search instead of Neo4j (much simpler deployment)
- 26 MCP methods vs planned 5 (much richer agent interface)
- Semantic search with embeddings
- Clone detection (JSCPD)
- Hotspot analysis (complexity/coupling metrics)
- Multi-agent coordination system
- Agent telemetry and diagnostics
- Provider-based embedding architecture
- Incremental parsing with richer metadata
