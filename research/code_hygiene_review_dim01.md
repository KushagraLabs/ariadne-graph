# Dimension 1: Existing Plan Architecture Summary

## Code Hygiene MCP Plan (User-Provided)

### Purpose
Build a reusable Code Hygiene MCP server that can be plugged into Python and TypeScript repositories without requiring the Lumen platform runtime. The current Lumen implementation remains the golden reference until parity is achieved.

### High-Level Architecture
```
code_hygiene_mcp/
  src/code_hygiene_mcp/
    core/          # config, discovery, facts, retrieval, snippets, freshness, diagnostics
    languages/     # python_ast, typescript (adapter-based)
    graphstores/   # base, memory, json_store, neo4j, lumen (adapter-based)
    mcp/           # server, tools, schemas
    cli.py
```

### Core Design Principles
1. Repo-agnostic analyzer
2. Language parsing as adapter, not core identity
3. Graph persistence as backend adapter
4. Preserve deterministic graph facts for testable parity
5. Direct Neo4j integration preferred over copying Lumen's KG runtime
6. Lumen integration optional and reversible

### Graph Schema (Planned)
**Node labels**: CodeRepo, CodeFile, CodeModule, CodeClass, CodeFunction, CodeMethod, CodeVariable, CodeAttribute, CodeImport, CodeExport, CodeRoute, CodeDiagnostic

**Relationships**: CONTAINS, DEFINES, IMPORTS, IMPORTS_SYMBOL, EXPORTS, CALLS, INHERITS, OVERRIDES, DECORATED_BY, USES_TYPE, RETURNS_TYPE, ROUTES_TO, HAS_DIAGNOSTIC

### MCP Tools (Planned, 5 initial)
- code_graph_retrieve
- code_graph_index
- code_graph_status
- code_graph_inspect_file
- code_graph_trace_dependencies

### Persistence Strategy
- In-memory graph for tests
- JSON/SQLite local graph for dev workflows
- Direct Neo4j for persistent retrieval (primary production backend)
- Optional Lumen KG adapter

### Language Adapter Contract
```python
class LanguageAdapter(Protocol):
    language: str
    parser_version: str
    extensions: tuple[str, ...]
    def discover_files(self, root: Path, config: AnalyzerConfig) -> list[Path]: ...
    def extract_file(self, path: Path, context: ExtractionContext) -> CodeGraphDelta: ...
```

### TypeScript Adapter Plan
- Uses ts-morph or TypeScript compiler API (Node helper process invoked by Python MCP)
- Supports: .ts/.tsx, tsconfig loading, path alias resolution, import/export graph, React components, hooks, routes

### Execution Milestones
1. Architecture and Repo Scaffold
2. Python Parity
3. Graph Store and Neo4j
4. Retrieval and MCP Parity
5. External Repo Validation
6. TypeScript Adapter
7. Lumen Compatibility and Switch-Over

### Parity Metrics Target
- 95%+ node label coverage on curated fixtures
- 95%+ relationship coverage on curated fixtures
- Exact preservation of core node ID strategy
- No missing core facts (CodeFile, CodeModule, CodeFunction, CodeClass, CodeImport, CodeExport)
- Retrieval top-5 overlap for curated queries
