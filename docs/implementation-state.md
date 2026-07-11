# Implementation state and known gaps

> This document records the historical/narrative implementation state of
> `ariadne-graph` — what has been built, wired, and what gaps remain. It was
> extracted from `AGENTS.md` to keep that file a lean operational reference.
> `AGENTS.md` retains the operational essentials (commands, tool list, module
> map, schema, backends, config, security); this file holds the detailed
> running log of the project's state.

## Current state and known gaps (as of 2026-06-29)

As of the latest changes:

- **Tests:** 292 tests pass, 13 skipped (Neo4j backend tests when Neo4j is not
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
