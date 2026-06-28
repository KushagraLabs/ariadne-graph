# AGENTS.md — Ariadne Graph

> This file is written for AI coding agents. It summarises the actual project
> layout, build/test commands, code conventions, and current implementation
> state as observed from the repository contents. When in doubt, trust the
> source files and `pyproject.toml` over this file, and update this file when
> you change any of the items described below.

## 1. Project overview

`ariadne-graph` is a standalone Python MCP (Model Context Protocol) server
for repository-level code intelligence. It parses source files into a code
knowledge graph and exposes 16 canonical MCP tools (plus a Lumen compatibility
alias) for indexing, querying, dependency tracing, impact analysis,
architecture summarisation, code inspection, and code-quality diagnostics.

Key goals from `README.md`:

- Provide a repo-agnostic MCP server for codebase navigation and impact analysis.
- Python-first with a full Python AST extractor; TypeScript/TSX extraction
  available as an optional extra.
- Dual persistence: SQLite for zero-config local use, Neo4j for production.
- Incremental sync via XXH3 content hashing.
- Semantic, keyword, and graph-neighbourhood retrieval.

**Important scope note:** The project is a working Python-first scaffold with
a real TypeScript/TSX Tree-sitter extractor available as an optional extra.
The CLI/server default to `SQLiteGraphStore`; `MemoryGraphStore` is only a
fallback when SQLite cannot be initialised.

## 2. Repository layout

The repository root contains documentation and research; the runnable package
lives under `project/`.

```text
/
├── README.md                    # High-level goals and non-goals
├── AGENTS.md                    # This file
├── docs/                        # Architecture, spec, plans (Markdown)
├── research/                    # Review and dimension analysis notes
└── project/                     # Runnable Python package
    ├── pyproject.toml
    ├── README.md
    ├── src/ariadne_graph/
    │   ├── __init__.py
    │   ├── cli.py                 # `ariadne` CLI
    │   ├── core/                  # Models, config, retrieval, search, sync, …
    │   ├── languages/             # Language adapters
    │   ├── graphstores/           # Storage backends
    │   └── mcp/                   # MCP server, tools, schemas
    └── tests/                     # Test suite
```

### Main modules

| Module | Purpose |
|--------|---------|
| `core/config.py` | `AnalyzerConfig` — repo root, ignore patterns, embedding settings, sync settings |
| `core/models.py` | Pydantic models: `CodeNode`, `CodeEdge`, `CodeGraphDelta`, search/impact outputs |
| `core/discovery.py` | `FileDiscovery` with ignore-pattern and file-size filtering |
| `core/incremental_sync.py` | `IncrementalSync` — XXH3 content-hash based incremental indexing |
| `core/retrieval.py` | `GraphRetriever` — node lookup, BFS dependency tracing, impact analysis |
| `core/search.py` | `HybridSearcher` — semantic, keyword, hybrid (RRF), and symbol search |
| `core/embeddings.py` | `EmbeddingProvider` protocol and `LocalEmbeddingProvider` (sentence-transformers) |
| `core/communities.py` | `CommunityAnalyzer` — Louvain community detection and hotspot detection |
| `core/diagnostics.py` | `DiagnosticsCollector` — unused import, missing type, complexity, long-param rules |
| `core/freshness.py` | `FreshnessTracker` — index freshness metadata and dirty-file detection |
| `core/auto_sync.py` | `AutoSyncManager` — background polling incremental sync |
| `core/snippets.py` | `SnippetExtractor` — read source snippets with line numbers and context |
| `languages/base.py` | `LanguageAdapter` protocol and `ExtractionContext` |
| `languages/python_ast/` | Python AST adapter (`PythonLanguageAdapter`, `PythonFactExtractor`) |
| `languages/typescript/` | TypeScript/TSX adapter with optional SCIP enhancement (`TypeScriptLanguageAdapter`, `TypeScriptFactExtractor`, `TsConfigResolver`, `ScipIndexParser`, `ScipTypeScriptIndexer`, `ScipGraphTranslator`, `TreeSitterEnricher`) |
| `graphstores/base.py` | `GraphStore` and `SearchableGraphStore` protocols |
| `graphstores/memory.py` | `MemoryGraphStore` — in-memory, for tests and smoke checks |
| `graphstores/sqlite.py` | `SQLiteGraphStore` — SQLite with FTS5 and optional sqlite-vec |
| `graphstores/neo4j.py` | `Neo4jGraphStore` — Neo4j 5.x with vector/full-text fallbacks |
| `graphstores/lumen.py` | `LumenGraphStore` — optional Lumen KG compatibility wrapper |
| `mcp/schemas.py` | Pydantic input/output schemas for all 16 canonical MCP tools plus Lumen alias |
| `mcp/tools.py` | `ToolRegistry` — handlers for all 16 tools |
| `mcp/fallbacks.py` | `GraphStoreFallbacks` — scan-based handler fallbacks for non-searchable backends |
| `mcp/server.py` | FastMCP server exposing the tools (stdio/sse/streamable-http) |

### Graph schema

- **Node labels:** `CodeRepo`, `CodeFile`, `CodeModule`, `CodeClass`, `CodeFunction`, `CodeMethod`, `CodeVariable`, `CodeAttribute`, `CodeImport`, `CodeExport`, `CodeInterface`, `CodeTypeAlias`, `CodeRoute`, `CodeReactComponent`, `CodeHook`, `CodeDiagnostic`.
- **Relationship types:** `CONTAINS`, `DEFINES`, `IMPORTS`, `IMPORTS_SYMBOL`, `EXPORTS`, `CALLS`, `INHERITS`, `OVERRIDES`, `DECORATED_BY`, `USES_TYPE`, `RETURNS_TYPE`, `ROUTES_TO`, `HAS_DIAGNOSTIC`.

The Python and TypeScript extractors both add a generic `KnowledgeNode`
label to every node.

## 3. Technology stack

- **Language:** Python 3.11+ (the system default `python3` may be 3.9; use
  `python3.12` or `python3.13` on this machine).
- **Build backend:** `setuptools` with `pyproject.toml`.
- **CLI framework:** `argparse` (async handlers).
- **MCP framework:** `mcp.server.fastmcp` (`mcp>=1.0.0`).
- **Data validation:** Pydantic v2.
- **Async I/O:** `asyncio`, `aiofiles`, `aiosqlite` (SQLite backend).
- **Graph analytics:** `networkx`, `python-louvain`.
- **Hashing:** `xxhash` (XXH3).
- **Optional ML:** `sentence-transformers` + `torch` for local embeddings.
- **Optional graph DB:** `neo4j` driver for Neo4j backend.
- **Optional vector store:** `sqlite-vec` for SQLite vector search.
- **Optional language parsing:** `tree-sitter` + `tree-sitter-typescript` for
  TypeScript/TSX extraction; `protobuf` + `@sourcegraph/scip-typescript` for
  SCIP-enhanced TypeScript indexing.

## 4. Build, install, and run commands

All commands below assume you are in `project/`.

### Create a virtual environment and install

```bash
cd project
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Optional feature extras

```bash
pip install -e ".[semantic]"   # sentence-transformers + torch
pip install -e ".[neo4j]"      # Neo4j driver
pip install -e ".[vector]"     # sqlite-vec
pip install -e ".[typescript]" # tree-sitter + tree-sitter-typescript + protobuf
# additionally, for SCIP-enhanced TypeScript:
npm install -g @sourcegraph/scip-typescript
pip install -e ".[all]"        # everything above + dev tools
```

### Run tests

```bash
pytest tests/ -v
```

Current status: 292 tests pass, 13 skipped (Neo4j backend tests when Neo4j is not
configured): 46 Python extractor tests, 15 Python diagnostics tests in
`tests/test_python/test_diagnostics.py`, 32 TypeScript/TSX extractor tests,
10 deterministic SCIP parser/translator/indexer tests in
`tests/test_typescript/test_scip_bridge.py` and `tests/test_typescript/test_scip_indexer.py`,
14 wired handler/SQLite tests in `tests/test_mcp/test_wired_handlers.py`,
2 MCP server smoke tests in `tests/test_mcp/test_server.py`,
5 change-detection tests in `tests/test_core/test_incremental_sync.py`,
4 auto-sync tests in `tests/test_core/test_auto_sync.py`,
6 config tests in `tests/test_core/test_config.py`,
6 search tests in `tests/test_core/test_search.py`,
7 community-analysis tests in `tests/test_core/test_communities.py`,
39 graph-store unit tests in `tests/test_graphstores/` (including the Lumen
adapter tests, the legacy-FTS migration test, the sqlite-vec fallback deadlock test,
and factory backend-selection tests),
3 capability-report tests in `tests/test_core/test_capabilities.py`,
4 dependency-fallback acceptance tests in `tests/test_acceptance/test_dependency_fallbacks.py`,
9 discovery tests in `tests/test_core/test_discovery.py`,
14 freshness tests in `tests/test_core/test_freshness.py`,
12 retrieval tests in `tests/test_core/test_retrieval.py`, and
18 CLI tests in `tests/test_cli.py`. Neo4j graph-store tests are skipped unless
`ARIADNE_NEO4J_URI` is set.

### Run linting and type checking

```bash
ruff check src/
mypy src/
```

Current status: both `ruff check src/ tests/` and `mypy src/` pass. The generated
SCIP protobuf bindings (`src/ariadne_graph/languages/typescript/_scip_pb2.py`)
are excluded from both tools. Missing-stub warnings for optional dependencies
(`neo4j`, `sentence_transformers`) and untyped core dependencies (`aiofiles`,
`networkx`, `community`) are suppressed with targeted `# type: ignore` comments
so that `warn_unused_ignores = true` remains enabled.

### Run the CLI

```bash
# Index a repository
ariadne index /path/to/repo

# Check status
ariadne status /path/to/repo

# Keyword search (default)
ariadne search /path/to/repo "def parse"

# Semantic search filtered to functions
ariadne search /path/to/repo "authentication logic" --semantic --types CodeFunction

# Keyword search filtered by language
ariadne search /path/to/repo "helper" --language python

# Architecture summary
ariadne architecture /path/to/repo

# Watch a repository and re-index on a schedule
ariadne watch /path/to/repo

# Start MCP server over stdio
ariadne mcp --transport stdio
```

### Run the MCP server directly

```bash
python -m ariadne_graph.mcp.server
```

## 5. Testing instructions

> **Definition of done (READ THIS — overrides "it works" and "tests pass"):**
> No feature is done until a test FAILS on the wrong/cheap implementation and
> PASSES on the real one — shown going red, then green, on the *hard case*
> (the input a fuzzy/heuristic version gets wrong). Do NOT weaken or delete a
> test to make it pass. Do NOT satisfy a resolution test with bare-name
> fallback. If you cannot make it real, STOP and say so. A test that passes
> immediately often tests nothing. Full rationale and the per-feature
> template: see [`VERIFICATION.md`](VERIFICATION.md) at the repo root.
> Worked example: `project/tests/test_python/test_hardcase_call_resolution.py`
> (two files defining `save()` — the call must resolve to the *local* one).

- The test runner is **pytest** with `asyncio_mode = "auto"` configured in
  `pyproject.toml`.
- Tests live under `tests/`. Active suites cover the Python AST extractor,
  the TypeScript/TSX Tree-sitter extractor, wired MCP handlers, and
  change-detection logic.
- Fixtures use plain strings and `tmp_path`; there is no separate fixture
  directory for the active tests.
- To add a test, create or extend a file under the appropriate `tests/`
  sub-package. Existing empty `__init__.py` files mark the packages.
- Run a single test file:

  ```bash
  pytest tests/test_python/test_extractor.py -v
  pytest tests/test_typescript/test_extractor.py -v
  pytest tests/test_core/test_incremental_sync.py -v
  ```

## 6. Code style guidelines

The project uses `ruff` and `mypy` as configured in `pyproject.toml`:

- **Line length:** 100 characters (`line-length = 100`).
- **Target Python version:** 3.11 (`target-version = "py311"`).
- **Ruff lint rules:** `E`, `F`, `W`, `I`, `N`, `UP`, `B`, `C4`, `SIM`.
- **Ruff ignores:** `E501` (line length is informational only).
- **MyPy:** `disallow_untyped_defs = true`, `warn_return_any = true`,
  `warn_unused_ignores = true`.

Conventions observed in the code:

- Start every module with `from __future__ import annotations`.
- Use absolute imports; `src/` is the package root.
- Prefer `Path` from `pathlib` over string paths.
- Type-hint public functions and methods.
- Use Pydantic `BaseModel` for configuration, input, and output schemas.
- Use `from collections.abc import Sequence` rather than `typing.Sequence`
  (the current code has some legacy imports that ruff flags).

Before committing, aim to make `ruff check src/` and `mypy src/` pass. If you
add an optional dependency, add the appropriate `type: ignore` or stub package
rather than weakening global mypy settings.

## 7. Runtime architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                        MCP client / CLI                      │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────────┐
│              FastMCP server / ToolRegistry                   │
│   (16 canonical tools + 1 Lumen alias)                       │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────────┐
│          Core services (search, retrieval, sync, …)          │
└───────────────────┬─────────────────────────────────────────┘
                    │
┌───────────────────▼─────────────────────────────────────────┐
│   GraphStore backend (Memory / SQLite / Neo4j / Lumen wrap)  │
└─────────────────────────────────────────────────────────────┘
                    ▲
┌───────────────────┴─────────────────────────────────────────┐
│       Language adapters (Python AST, TypeScript/TSX)         │
└─────────────────────────────────────────────────────────────┘
```

- The CLI and the MCP server both create a `ToolRegistry` and call the same
  handlers in `mcp/tools.py`.
- By default both use `graphstores.factory.create_graph_store()`, which
  selects SQLite when `aiosqlite` is available (`.ariadne/graph.db` by
  default), Neo4j when `ARIADNE_NEO4J_URI` is set, and falls back to
  `MemoryGraphStore` only when the preferred backends cannot be initialised.
  The registry accepts any `GraphStore`/`SearchableGraphStore` implementation.
- Language adapters are loaded dynamically with try/except; missing adapters
  are skipped rather than crashing the server.
- `graph_id` is derived deterministically from the resolved repo path using
  SHA-256 (first 16 hex chars).
- `FreshnessTracker` is fully wired: `handle_index` records freshness metadata
  after indexing and `handle_index_status` uses it for last-indexed, file count,
  and dirty-file detection.
- **Implementation-vs-design gap:** Most `core/` services
  (`IncrementalSync`, `GraphRetriever`, `HybridSearcher`, `CommunityAnalyzer`)
  are now invoked by the MCP/CLI tool handlers when the active backend is a
  `SearchableGraphStore`. `SnippetExtractor` is used by retriever/search for
  context. `DiagnosticsCollector` is wired into the Python extractor and emits
  `CodeDiagnostic` nodes for unused imports, missing type annotations, complex
  functions, and long parameter lists. `code_graph_list_diagnostics` queries
  these nodes. Several handlers still keep simple in-memory fallbacks for
  non-searchable backends.

## 8. Configuration

`AnalyzerConfig` (`core/config.py`) controls behaviour. Key fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `repo_root` | required | Root path of the repo to analyse |
| `ignore_patterns` | `[".git", "__pycache__", "*.pyc", "node_modules", ".venv", …]` | File discovery ignore rules |
| `max_file_size` | 1_000_000 bytes | Skip files larger than this |
| `embedding_provider` | `"local"` | Provider name |
| `embedding_model` | `"all-MiniLM-L6-v2"` | Local sentence-transformers model |
| `embedding_dimensions` | 384 | Expected vector dimension |
| `incremental_sync_interval` | 30 seconds | Poll interval when auto-sync is on |
| `auto_sync` | False | Enable background sync |
| `scip_typescript_enabled` | `None` (auto-detect) | Enable/disable SCIP-TypeScript indexing |
| `scip_typescript_path` | `None` | Path to `scip-typescript`, or `npx` |
| `scip_typescript_args` | `[]` | Extra args passed to `scip-typescript index` |
| `scip_typescript_infer_tsconfig` | `None` (auto) | Use `--infer-tsconfig` for JS-only projects |

Environment variables read by `AnalyzerConfig` and honoured by the CLI and
server startup via `graphstores.factory.create_graph_store()`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARIADNE_DB` | `.ariadne/graph.db` | SQLite database path |
| `ARIADNE_NEO4J_URI` | `bolt://localhost:7687` | Neo4j URI |
| `ARIADNE_NEO4J_USER` | `neo4j` | Neo4j username |
| `ARIADNE_NEO4J_PASSWORD` | `password` | Neo4j password |
| `ARIADNE_EMBEDDING_PROVIDER` | `local` | Embedding provider |
| `ARIADNE_SCIP_TYPESCRIPT_ENABLED` | unset | `true`/`false` to force SCIP indexing on/off |
| `ARIADNE_SCIP_TYPESCRIPT_PATH` | unset | Path to `scip-typescript` binary or `npx` |
| `ARIADNE_SCIP_TYPESCRIPT_ARGS` | unset | Comma-separated extra args for `scip-typescript index` |
| `ARIADNE_SCIP_TYPESCRIPT_INFER_TSCONFIG` | unset | `true`/`false` to force `--infer-tsconfig` |
| `LUMEN_CODE_GRAPH_PROVIDER` | unset | Set to `standalone` to enable the Lumen compatibility adapter |
| `LUMEN_WORKSPACE_ID` | unset | Lumen workspace identifier stored in project metadata |

## 9. Graph store backends

| Backend | Class | Persistence | Search | Notes |
|---------|-------|-------------|--------|-------|
| Memory | `MemoryGraphStore` | No | No | Fallback for tests and smoke checks |
| SQLite | `SQLiteGraphStore` | Yes | FTS5 + optional sqlite-vec | Default local backend selected by the factory |
| Neo4j | `Neo4jGraphStore` | Yes | Native vector + full-text | Requires `neo4j` package and running server; selected when `ARIADNE_NEO4J_URI` is set |
| Lumen | `LumenGraphStore` | Depends on delegate | Via delegate | Optional wrapper that adds Lumen KG query aliases, workspace-root restrictions, and `lumen_code_graph_retrieve`; enabled by `LUMEN_CODE_GRAPH_PROVIDER=standalone` |

`GraphStore.query()` accepts named queries. The common names implemented by
`MemoryGraphStore`, `SQLiteGraphStore`, and `Neo4jGraphStore` are:

- `nodes`, `edges`
- `node_by_id`, `node_by_name`, `node_name_fuzzy`
- `neighbors`, `node_neighbors`, `node_edges`, `node_outgoing_edges`,
  `node_incoming_edges`, `node_all_edges`
- `incoming`, `outgoing`
- `nodes_by_label`, `nodes_by_file`, `stored_file_paths`
- `index_metadata`, `set_index_metadata`, `count_files`, `file_hashes`,
  `dirty_files`

`SearchableGraphStore` adds `upsert_embeddings`, `search_vector`,
`search_keyword`, `set_communities`, and `get_communities`.

## 10. Language adapters

- **Python (`languages/python_ast/`):** Fully implemented. Uses the standard
  library `ast` module. Extracts modules, classes, functions, methods,
  imports, variables, attributes, type annotations, decorators, inheritance,
  overrides, call edges, FastAPI routes, dataclass/enum/Pydantic flags,
  snippets, complexity, and code-quality diagnostics (unused imports, missing
  type annotations, complex functions, and long parameter lists).
- **TypeScript (`languages/typescript/`):** Implemented using Tree-sitter with
  an optional SCIP enhancement layer. When `@sourcegraph/scip-typescript` is
  installed, `TypeScriptLanguageAdapter.prepare_project` runs
  `scip-typescript index` once per sync, parses the resulting `index.scip`
  using vendored protobuf bindings, and emits compiler-accurate symbol nodes
  and edges. Tree-sitter is then used to enrich SCIP nodes with snippets,
  complexity, React labels, decorators, and call positions. When SCIP is
  unavailable, the adapter falls back to the Tree-sitter extractor. Requires
  the `[typescript]` extra (which now includes `protobuf`) plus the
  `scip-typescript` npm package for the enhanced path.

To add a new language, implement the `LanguageAdapter` protocol in a new
sub-package under `languages/` and load it in `mcp/server.py` and `cli.py`.

## 11. MCP tools (16 canonical + 1 Lumen alias)

### Indexing
- `code_graph_index(repo_path, force_rebuild=False)`
- `code_graph_index_status(repo_path)`
- `code_graph_list_projects()`
- `code_graph_delete_project(repo_path)`

### Capabilities
- `code_graph_capabilities()` — runtime availability report for optional extras (`typescript`, `semantic`, `vector`, `neo4j`, `scip_typescript_indexer`).

### Query
- `code_graph_retrieve(query, graph_id=None)`
- `code_graph_search_semantic(query_text, repo_path=None, limit=10, types=[])`
- `code_graph_search_code(pattern, repo_path=None, language=None, limit=10)`
- `code_graph_trace_dependencies(symbol, direction="both", max_depth=3)`

### Analysis
- `code_graph_impact_analysis(symbol)`
- `code_graph_detect_changes(repo_path, since_ref=None)`
- `code_graph_find_hotspots(repo_path, top_n=10, metric="complexity")`
- `code_graph_get_architecture(repo_path)`
- `code_graph_list_communities(repo_path, community_id=None)`

### Code
- `code_graph_inspect_file(file_path, graph_id=None)`

### Diagnostics
- `code_graph_list_diagnostics(repo_path, level=None, rule=None, file_path=None, limit=100)`

### Lumen compatibility alias
- `lumen_code_graph_retrieve(query, graph_id=None, repo_path=None)` — delegates to `code_graph_retrieve` and augments the response with a Lumen-style context block.

## 12. Security considerations

- The server reads arbitrary files from the configured `repo_root`. Do not
  point it at untrusted repositories without review, and do not expose the
  MCP server over a network unless you understand the file-system access it
  grants.
- Neo4j credentials are currently plain strings in environment variables
  (or defaults). In production, inject them through a secrets manager and
  avoid the default password.
- Indexing uses `ast.parse` on Python files and Tree-sitter on TypeScript/TSX
  files; both parse but do **not** execute source code.
- The CLI resolves paths with `Path(...).resolve()`. Ensure the process runs
  with the least privileges necessary for the target repository.
- There is no authentication/authorisation layer in the current MCP server.

## 13. Current state and known gaps

As of the latest changes:

- **Tests:** 292 tests pass, 13 skipped (Neo4j backend tests when Neo4j is not
  configured): 46 Python extractor tests, 15 Python diagnostics tests in
`tests/test_python/test_diagnostics.py`, 32 TypeScript/TSX extractor tests,
  14 wired handler/SQLite tests in `tests/test_mcp/test_wired_handlers.py`,
  2 MCP server smoke tests in `tests/test_mcp/test_server.py`,
  5 change-detection tests in `tests/test_core/test_incremental_sync.py`,
  4 auto-sync tests in `tests/test_core/test_auto_sync.py`,
  6 config tests in `tests/test_core/test_config.py`,
  6 search tests in `tests/test_core/test_search.py`,
  7 community-analysis tests in `tests/test_core/test_communities.py`,
  39 graph-store unit tests in `tests/test_graphstores/`,
  3 capability-report tests in `tests/test_core/test_capabilities.py`,
  4 dependency-fallback acceptance tests in
  `tests/test_acceptance/test_dependency_fallbacks.py`,
  9 discovery tests in `tests/test_core/test_discovery.py`,
  14 freshness tests in `tests/test_core/test_freshness.py`,
  12 retrieval tests in `tests/test_core/test_retrieval.py`, and
  18 CLI tests in `tests/test_cli.py`. Neo4j graph-store tests are skipped unless
  `ARIADNE_NEO4J_URI` is set.
- **Lint/type:** both `ruff check src/ tests/` and `mypy src/` pass.
  Missing-stub warnings for optional dependencies (`neo4j`,
  `sentence_transformers`) and untyped core dependencies (`aiofiles`,
  `networkx`, `community`) are suppressed with targeted `# type: ignore`
  comments so that `warn_unused_ignores = true` remains enabled.
- **Optional-dependency degradation:** `code_graph_capabilities` and the
  `ariadne capabilities` CLI command report runtime availability of the
  `typescript`, `semantic`, `vector`, and `neo4j` extras. `code_graph_index_status`
  includes the same capability report. When tree-sitter is missing, TypeScript
  files are indexed as stub `CodeFile` nodes with a `CodeDiagnostic` warning
  (`rule="missing_dependency"`). CI runs the test suite against both the base
  (`[dev]`) and full (`[all]`) installs so fallback paths are exercised.
- **Default backend:** CLI and server use `graphstores.factory` to select
  `SQLiteGraphStore` by default (`.ariadne/graph.db`),
  `Neo4jGraphStore` when `ARIADNE_NEO4J_URI` is set, and
  `MemoryGraphStore` only as a fallback. The environment variables
  `ARIADNE_DB` / `ARIADNE_NEO4J_*` are wired through
  `AnalyzerConfig`.
- **Semantic/keyword search:** `handle_search_semantic` and
  `handle_search_code` are wired to `HybridSearcher` when a searchable backend
  and embedding provider are available. Both tools now accept an optional
  `repo_path` parameter to restrict results to a single graph; the CLI `search`
  command passes its repo argument through. If `repo_path` is omitted, the
  tools still search all known graphs for backward compatibility.
- **Diagnostics query:** `code_graph_list_diagnostics` is exposed as an MCP
  tool and as the CLI `diagnostics` subcommand. It queries `CodeDiagnostic`
  nodes and supports filters by `level`, `rule`, and `file_path`. The Python
  extractor emits diagnostics with `level`/`rule` properties aligned with
  `DiagnosticsCollector`; the TypeScript adapter emits `missing_dependency`
  and SCIP-related diagnostics.
- **SQLite vector dimensions:** `SQLiteGraphStore` now accepts
  `embedding_dimensions` from `AnalyzerConfig` and uses it when creating the
  sqlite-vec `vec0` virtual table. A dimension mismatch with an existing table
  causes sqlite-vec search to be disabled for that session and falls back to
  brute-force cosine similarity.
- **SQLite FTS5 rowid-keyed external content:** `SQLiteGraphStore` uses a
  regular `fts_node_content` table plus an external-content `node_fts` virtual
  table keyed by `rowid`.  Per-file deletes now resolve rowids from the content
  table and delete from the FTS index by rowid, which is O(1) per row and
  eliminates the super-linear degradation seen on repos above ~100k nodes.
  Existing databases using the legacy schema are automatically migrated.
- **SQLite persistent connection:** `SQLiteGraphStore` now keeps one persistent
  aiosqlite connection per instance and serializes access with an asyncio lock.
  This removes the previous per-call ``_connect()`` / ``dlopen()`` of the
  sqlite-vec extension, which dominated wall-clock on large indexing runs due
  to dyld's global loader write-lock.  All graph store implementations now
  implement ``close()`` (including the ``MemoryGraphStore`` no-op and the
  ``LumenGraphStore`` delegate), and the CLI ensures ``await registry.close()``
  is called before the event loop exits, preventing the
  ``RuntimeError: Event loop is closed`` shutdown traceback.
- **SQLite indexed file_path column:** `nodes` now stores ``file_path`` as a
  real indexed column (with automatic migration and backfill for existing
  databases).  ``delete_file_facts`` and ``nodes_by_file`` use the index instead
  of ``json_extract(properties, '$.file_path')``, eliminating the O(N²) per-file
  JSON scan that dominated incremental sync on repositories above ~100k nodes.
- **Handler fallbacks consolidated:** Manual graph-scan fallback logic for
  retrieve, trace, impact, hotspots, and architecture has moved into
  `mcp/fallbacks.py` (`GraphStoreFallbacks`). The handlers now delegate to
  these helpers when a searchable backend is unavailable.
- **Index file count:** `handle_index` now reports the authoritative total
  number of indexed files via the `count_files` query, rather than only the
  files that changed in the current run.
- **Sync-enabled persistence:** `sync_enabled` is stored in the graph metadata
  (`graphs` table / `CodeProject` node / in-memory project catalog) and
  surfaced by `code_graph_index_status`. It reflects the config value at index
  time and is overridden when `AutoSyncManager` is actively running.
- **Core services wiring:** `IncrementalSync`, `GraphRetriever`,
  `HybridSearcher`, `CommunityAnalyzer`, and `FreshnessTracker` are fully
  wired through the MCP/CLI handlers when the backend supports the required
  queries. `SnippetExtractor` is used internally by retrieval/search.
- **Backend abstraction:** Removed-file cleanup lives in the backends;
  `GraphStore.delete_file_facts` also removes the stored content hash in all
  backends.
- **Community/architecture analysis:** `code_graph_get_architecture` and
  `code_graph_list_communities` call `CommunityAnalyzer` when the backend is
  searchable; a connected-components fallback remains for non-searchable
  backends.
- **Project listing:** `code_graph_list_projects` reads from a persistent
  project catalog stored in the active graph store backend, so projects
  survive server restart.
- **Change detection:** `code_graph_detect_changes` honours `since_ref` when
  provided, comparing the working tree against the resolved git ref. It falls
  back to stored-hash comparison for non-git or untracked files.
- **TypeScript support:** Full Tree-sitter extraction for `.ts` and `.tsx`
  files is implemented behind the `[typescript]` extra.
- **SCIP-TypeScript integration:** `languages/typescript/` now includes a
  SCIP enhancement layer. `ScipIndexParser` (using vendored protobuf bindings
  pinned to commit `3b30443d39f2ad9a9f1b3c2dd770893022e81172`),
  `ScipTypeScriptIndexer`, `ScipGraphTranslator`, and `TreeSitterEnricher`
  work together to use `scip-typescript` as a compiler-accurate refinement
  layer, falling back to Tree-sitter when SCIP is unavailable. A committed
  binary fixture (`tests/fixtures/ts_scip_project/index.scip`) makes the
  parser/translator unit tests deterministic in CI.
- **TypeScript extractor traversal:** `_visit_expression` in
  `languages/typescript/extractor.py` now makes dispatch and recursion mutually
  exclusive, eliminating the exponential 2^n re-traversal bug that emitted the
  same node many times for nested functions and calls.
- **Unique node IDs:** Both the Python and TypeScript extractors share a
  ``UniqueIdMixin`` in ``languages/base.py`` that disambiguates node IDs for
  repeated imports, reassigned variables, overloaded methods/properties,
  duplicate class attributes, anonymous functions, and duplicate type aliases
  by appending a source-position suffix (``@L<line>C<col>``) when a name-based
  ID would otherwise collide. This prevents silent node overwrites in the
  graph and the corresponding FTS-row bloat in SQLite.
- **Ignore patterns:** The default ``AnalyzerConfig.ignore_patterns`` now
  includes ``.claude`` so Claude worktree copies (e.g.
  ``.claude/worktrees/agent-*/``) are skipped during file discovery.
- **Auto-sync:** `auto_sync` and `incremental_sync_interval` are wired. When
  `auto_sync=True`, the MCP server starts an `AutoSyncManager` background task
  that polls registered projects and re-indexes them. The CLI exposes a
  `watch` command that indexes once and then polls for changes.
- **Deployment:** A `project/Dockerfile` (installing the `[all]` extras and
  defaulting to `ariadne mcp`), a `.github/workflows/ci.yml` GitHub
  Actions workflow (running pytest, ruff, and mypy on Python 3.11 and 3.12),
  pinned `requirements.txt`/`requirements-all.txt` files, and a `uv.lock`
  file are now included. Distribution remains via `pip install -e ".[all]"`
  from source; the lock files support reproducible installs with `uv`.
- **Retrieve by repo path:** `code_graph_retrieve` now accepts an optional
  `repo_path` parameter and derives the graph ID from it when `graph_id` is
  omitted.  The Lumen alias `lumen_code_graph_retrieve` falls back to
  `AnalyzerConfig.lumen_workspace_root` when `repo_path` is not supplied, so
  workspace-specific MCP endpoints resolve the correct graph automatically.
- **Symbol resolution quality:** `GraphRetriever._resolve_symbol` now scores
  candidate matches and prefers definition labels (`CodeClass`,
  `CodeFunction`, `CodeMethod`, etc.) over `CodeImport` and `CodeDiagnostic`
  nodes, so retrieving a symbol name such as ``Trainer`` returns the class
  definition rather than an import stub.
- **Lumen compatibility:** An optional `LumenGraphStore` adapter
  (`graphstores/lumen.py`) wraps any concrete backend and adds Lumen KG query
  aliases, workspace-root restrictions, and Lumen-style project metadata.  The
  MCP server exposes `lumen_code_graph_retrieve` as an alias when
  `lumen_compat_aliases` is enabled.  Enable the adapter with
  `LUMEN_CODE_GRAPH_PROVIDER=standalone`.
- **Static analysis limitations:** Documented in
  `docs/static-analysis-limitations.md`.  Dependency tracing and impact
  analysis are syntactic and therefore incomplete for dynamic dispatch,
  runtime imports (`__import__`, `importlib`), factory patterns, and
  string-based routing.

## 14. Useful references

- `project/pyproject.toml` — dependencies, extras, scripts, tool configs.
- `project/README.md` — quick-start and feature summary.
- `docs/SPEC.md` — detailed spec of models, protocols, and tools.
- `docs/architecture.md` — design principles and high-level shape.
- `docs/graph-store-design.md` — backend conventions and persistence strategy.
- `docs/implementation-plan.md` — phased implementation plan.
- `docs/static-analysis-limitations.md` — documented gaps in static dependency
  tracing and impact analysis.
