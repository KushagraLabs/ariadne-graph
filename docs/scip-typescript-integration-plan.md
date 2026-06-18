# SCIP-TypeScript Integration Plan

> **Status:** design + plan effort — not yet implemented.  
> **Owner:** ariadne-graph maintainers  
> **Target validation repo:** `/Users/amitkumarsingh/Documents/cosmic_lens`

## 1. Goal

Upgrade the TypeScript/TSX language substrate so that **call, import, and type edges are compiler-accurate from the first index of `cosmic_lens`**. The current Tree-sitter extractor is fast and Python-native, but it is syntactic: it misses renamed imports, barrel-file re-exports, dynamic dispatch, and cross-file symbol resolution. [`scip-typescript`](https://github.com/sourcegraph/scip-typescript) (Sourcegraph's TypeScript SCIP indexer) uses the real TypeScript compiler, so it resolves symbols exactly as `tsc` does.

This document describes how to wire `scip-typescript` into `ariadne-graph` as an optional, fallible, project-level refinement layer while keeping the existing Tree-sitter pass for structural enrichment.

## 2. Scope

### In scope
- Detect `scip-typescript` (global binary or `npx`) and a runnable TS/JS project (`tsconfig.json` or `package.json`).
- Run `scip-typescript index` as a subprocess, producing `index.scip`.
- Parse `index.scip` (protobuf) into Python objects.
- Translate SCIP symbols, occurrences, and relationships into `CodeNode` / `CodeEdge` / `CodeGraphDelta`.
- Feed those deltas through the existing `IncrementalSync` pipeline.
- Keep Tree-sitter extraction as a fallback and as a source of extra structural properties (complexity, React labels, snippets, decorators).
- Make the integration opt-in / auto-detect and degrade gracefully when Node, `scip-typescript`, or project dependencies are missing.

### Out of scope (for this upgrade)
- Replacing the Python AST extractor with a SCIP-based one.
- Adding new MCP tools; existing `trace_dependencies`, `impact_analysis`, `search_*`, etc. will consume the richer edges automatically.
- Real-time incremental SCIP indexing at file granularity (SCIP is project-wide; we will cache and fingerprint whole-project runs).

## 3. Current state & gap

The current TypeScript adapter lives in:

```
project/src/ariadne_graph/languages/typescript/
  adapter.py    # TypeScriptLanguageAdapter
  extractor.py  # TypeScriptFactExtractor (Tree-sitter)
  tsconfig.py   # TsConfigResolver
```

It extracts:
- modules, classes, interfaces, type aliases, functions, methods, variables, imports, exports
- syntactic `CALLS` edges from `call_expression` / `new_expression`
- `INHERITS`, `IMPLEMENTS`, `USES_TYPE`, `RETURNS_TYPE` edges
- React component / hook labels via naming heuristics
- cyclomatic complexity, decorators, unused-import diagnostics

The gaps that `scip-typescript` closes:
| Gap | Example | SCIP fix |
|---|---|---|
| Renamed imports | `import { foo as bar } from './a'`; `bar()` → callee reported as `foo` | SCIP resolves to the original symbol |
| Barrel re-exports | `export { foo } from './a'`; consumer imports from `./barrel` | SCIP follows the export chain |
| Path aliases | `import { x } from '@app/utils'` | SCIP resolves via `tsconfig.json` |
| Method call resolution | `obj.method()` vs `method` reference | SCIP knows the declared type of `obj` |
| External dependencies | `import _ from 'lodash'` | SCIP emits package-qualified symbols |
| Type-only imports | `import type { T } from './a'` | SCIP marks `Import` role distinctly |

## 4. SCIP primer

SCIP = Sourcegraph Code Intelligence Protocol. It is a language-agnostic protobuf format emitted by compiler-backed indexers.

### Relevant messages
```protobuf
message Index {
  Metadata metadata;
  repeated Document documents;
  repeated SymbolInformation external_symbols;
}

message Document {
  string relative_path;
  repeated Occurrence occurrences;
  repeated SymbolInformation symbols;
  string language;
}

message SymbolInformation {
  string symbol;              // fully-qualified symbol URI
  repeated string documentation;
  repeated Relationship relationships;
  Kind kind;
  string display_name;
  Signature signature_documentation;
  string enclosing_symbol;
}

message Occurrence {
  oneof typed_range { SingleLineRange single_line_range; MultiLineRange multi_line_range; }
  string symbol;
  int32 symbol_roles;         // bitset: Definition=0x1, Import=0x2, WriteAccess=0x4, ReadAccess=0x8, ...
  SyntaxKind syntax_kind;
  oneof typed_enclosing_range { ... }
}

message Relationship {
  string symbol;
  bool is_reference;
  bool is_implementation;
  bool is_type_definition;
  bool is_definition;
}
```

A `Symbol` string looks like:
```
scip-typescript npm cosmic_lens 1.0.0 src/services/lens.ts/LensService#process().
```
The trailing descriptors encode the qualified name:
- `src`, `services`, `lens.ts` — namespace/file descriptors (`/`)
- `LensService` — type descriptor (`#`)
- `process` — method descriptor (`().`)

`scip-typescript` emits these strings deterministically from the TypeScript compiler's symbol table.

### Tooling
- **Package name (verified):** the npm package is **`@sourcegraph/scip-typescript`** (latest `0.4.0`).
  The unscoped name `scip-typescript` is **not** published and 404s on the registry — never install or `npx` the bare name.
- **CLI bin name:** the package's bin *is* `scip-typescript` (`bin: { "scip-typescript": "dist/src/main.js" }`),
  so after install the command is `scip-typescript index`; without a global install use `npx @sourcegraph/scip-typescript index`.
- Install (global): `npm install -g @sourcegraph/scip-typescript`
- Install-free run: `npx @sourcegraph/scip-typescript index`
- Index a TS project: `scip-typescript index`
- Index a JS project: `scip-typescript index --infer-tsconfig`
- Output: `index.scip` (protobuf) in the working directory.
- Debug: `scip print index.scip` or `protoc --decode=scip.Index`.

### Prerequisites verified against `cosmic_lens` (2026-06-17)
- `node v22.21.1`, `npx 10.9.4` present.
- `cosmic_lens/tsconfig.json` present → use plain `scip-typescript index` (no `--infer-tsconfig` needed).
- `cosmic_lens/node_modules` resolved → dependency types available for accurate cross-file resolution.
- Conclusion: the acceptance target meets all `scip-typescript` prerequisites; the design is implementable against it as-is.

## 5. Design principles

1. **Optional, not required.** The server must start and index TS repos even when Node/`scip-typescript` is missing. If the indexer fails, fall back to Tree-sitter and emit a `CodeDiagnostic`.
2. **Minimal intrusion.** Re-use `IncrementalSync`, `CodeGraphDelta`, `GraphStore`, and existing MCP handlers. Add one optional hook to `LanguageAdapter` rather than rewriting the pipeline.
3. **Project-level SCIP, file-level deltas.** `scip-typescript` is whole-project, but our pipeline wants per-file deltas. Build a per-file delta cache from one SCIP index and serve it through `extract_file`.
4. **Stable, deterministic IDs.** Use the SCIP symbol string as the canonical node ID when SCIP is active. Downstream tools resolve by `name` / `qualname`, so ID format is an internal detail.
5. **Fusion, not duplication.** Tree-sitter should enrich SCIP-derived nodes with properties SCIP does not provide (complexity, React labels, snippets, decorators). Avoid creating duplicate nodes for the same entity.
6. **Cache smartly.** Only re-run `scip-typescript` when the project's TypeScript fingerprint changes.

## 6. Architecture overview

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                         IncrementalSync.full_sync                        │
│   1. discover_files()                                                    │
│   2. get_changed_files()                                                 │
│   3. adapter.prepare_project(ctx, all_files, changed_files)  ← new hook  │
│   4. for f in changed: adapter.extract_file(f, ctx)                      │
│   5. delete old facts + add new nodes/edges                              │
└─────────────────────────────┬───────────────────────────────────────────┘
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                           ▼
┌───────────────────────┐             ┌─────────────────────────┐
│ ScipTypeScriptIndexer │             │  TreeSitterEnricher     │
│  - detect / run npm   │             │  - parse file AST       │
│  - write index.scip   │             │  - complexity, React,   │
└───────────┬───────────┘             │    decorators, snippets │
            │                         └────────────┬────────────┘
            ▼                                      │
┌───────────────────────┐                         │
│ ScipIndexParser       │                         │
│  - protobuf → Python  │                         │
└───────────┬───────────┘                         │
            │                                      │
            ▼                                      ▼
┌───────────────────────┐             ┌─────────────────────────┐
│ ScipGraphTranslator   │◄────────────│  match by (name, range) │
│  - symbols → nodes    │             │                         │
│  - occurrences → edges│             │                         │
└───────────┬───────────┘             └─────────────────────────┘
            │
            ▼
┌───────────────────────┐
│ per-file delta cache  │   keyed by Path → CodeGraphDelta
└───────────────────────┘
```

## 7. Component design

### 7.1 `ExtractionContext` extension

Add optional project-wide fields so the adapter can decide whether to invoke SCIP:

```python
class ExtractionContext(BaseModel):
    graph_id: str
    repo_root: Path
    source_commit: str | None = None
    all_files: list[Path] = Field(default_factory=list)       # ← new
    changed_files: list[Path] = Field(default_factory=list)   # ← new
```

### 7.2 Optional `LanguageAdapter.prepare_project` hook

```python
@runtime_checkable
class LanguageAdapter(Protocol):
    ...
    def prepare_project(
        self,
        context: ExtractionContext,
        all_files: list[Path],
        changed_files: list[Path],
    ) -> None:
        """Optional project-wide setup before per-file extraction."""
        ...
```

`IncrementalSync.full_sync` will call it once before the per-file loop. The default implementation is a no-op. `TypeScriptLanguageAdapter` will use it to run `scip-typescript` once per project change.

### 7.3 `ScipTypeScriptIndexer`

Responsibilities:
- Detect whether `scip-typescript` is runnable:
  - `shutil.which("scip-typescript")`
  - or `shutil.which("npx")` + `@sourcegraph/scip-typescript`
- Detect project type:
  - `tsconfig.json` exists → `scip-typescript index`
  - only `package.json` → `scip-typescript index --infer-tsconfig`
- Compute a project fingerprint (sorted file paths + current XXH3 hashes of all TS/TSX files) and compare with the last stored fingerprint in graph metadata (`set_index_metadata` / `index_metadata`).
- If fingerprint changed (or cold start), run the indexer in `repo_root` with a controlled output path (e.g., `.ariadne/scip/index.scip`).
- Capture stdout/stderr; on non-zero exit emit a `CodeDiagnostic` and fall back to Tree-sitter.
- Expose the path to `index.scip` and the command-line arguments used.

```python
class ScipTypeScriptIndexer:
    def __init__(self, repo_root: Path, config: AnalyzerConfig) -> None: ...

    async def ensure_index(self, context: ExtractionContext) -> Path | None:
        """Return path to index.scip, or None if SCIP indexing was skipped/failed."""
```

### 7.4 `ScipIndexParser`

Parse `index.scip` into plain Python dataclasses. We will vendor generated protobuf bindings (`_scip_pb2.py`) from the upstream `scip.proto` (Apache-2.0) and add `protobuf>=4.25` as an optional dependency.

```python
from ariadne_graph.languages.typescript._scip_pb2 import Index

class ScipIndexParser:
    def parse(self, index_path: Path) -> ScipIndex:
        """Return a Python-friendly representation of the SCIP index."""
```

The returned `ScipIndex` contains:
- `metadata: ScipMetadata`
- `documents: dict[Path, ScipDocument]`
- `external_symbols: dict[str, ScipSymbolInfo]`

### 7.5 `ScipGraphTranslator`

Convert one `ScipDocument` into a `CodeGraphDelta`.

Key decisions:
- **Node ID:** the full SCIP symbol string. This guarantees uniqueness and stability across re-indexes.
- **Properties:**
  - `name` from `display_name` or parsed descriptor
  - `qualname` from parsed descriptors
  - `scip_symbol` = full symbol string (for debugging)
  - `scip_kind` = `SymbolInformation.Kind` enum name
  - `file_path`, `line_start`, `line_end` from occurrence range
  - `documentation` joined from `SymbolInformation.documentation`
  - `signature` from `signature_documentation.text`
  - `is_external` for symbols whose package is not the local project
- **Labels:** map `Kind` → existing labels:
  - `Class` → `CodeClass`
  - `Interface` → `CodeInterface`
  - `Method` → `CodeMethod`
  - `Function` → `CodeFunction`
  - `TypeAlias` → `CodeTypeAlias`
  - `Variable`, `Constant` → `CodeVariable`
  - `Field`, `Property` → `CodeAttribute`
  - `Module`, `Namespace` → `CodeModule`
  - all get `KnowledgeNode`
- **Edges:**
  - `CONTAINS` / `DEFINES`: from enclosing module/class to the symbol.
  - `IMPORTS_SYMBOL`: for occurrences with `SymbolRole.Import`.
  - `CALLS`: for reference occurrences inside a call expression.
  - `REFERENCES`: for other reference occurrences.
  - `USES_TYPE`: for `Relationship.is_type_definition` or type-only references.
  - `IMPLEMENTS` / `INHERITS`: for `Relationship.is_implementation`.
  - `EXPORTS`: inferred from `export` declaration occurrences.

Call-expression detection:
- During Tree-sitter enrichment, record a list of `(start_line, start_col, end_line, end_col)` ranges where the expression is in call position.
- For each SCIP reference occurrence, if its range lies within one of those call ranges, emit `CALLS`; otherwise `REFERENCES`.

### 7.6 `TreeSitterEnricher`

A lightweight wrapper around the existing `TypeScriptFactExtractor` that returns **property updates** instead of new nodes when SCIP is active.

```python
class TreeSitterEnricher:
    def enrich(self, file_path: Path, scip_delta: CodeGraphDelta) -> CodeGraphDelta:
        """Return a new delta where SCIP nodes are enriched with Tree-sitter-only props."""
```

For each SCIP node in `scip_delta`, match it to a Tree-sitter node by `(name, line_start, line_end)` overlap, then copy:
- `complexity`
- `is_react_component`, `is_hook` labels
- `decorators`
- `snippet` (if SCIP did not include `Document.text`)
- `arg_types`, `return_type` if SCIP signature is missing

If no match is found, the SCIP node remains unchanged. Tree-sitter nodes that have no SCIP counterpart (e.g., synthetic route objects) are added as new nodes.

## 8. ID mapping strategy

When SCIP is active, the SCIP symbol string is the canonical node ID.

Example mapping for `src/services/lens.ts`:

| Entity | SCIP symbol | CodeNode id |
|---|---|---|
| module | `...src/services/lens.ts` | same as symbol |
| class `LensService` | `...src/services/lens.ts/LensService#` | same as symbol |
| method `process` | `...src/services/lens.ts/LensService#process().` | same as symbol |
| imported `axios` | `...node_modules/axios/index.d.ts/...` | same as symbol (marked external) |

Why use the SCIP symbol directly?
- It is already globally unique and deterministic.
- It avoids writing and maintaining a descriptor parser that maps to our legacy module-based IDs.
- Downstream retrieval resolves by `name` / `qualname`, so user-facing behavior is unchanged.
- Mixed graphs (some files indexed with SCIP, some with Tree-sitter fallback) are acceptable because queries do not rely on a single ID scheme.

## 9. Node & edge mapping reference

### 9.1 From `SymbolInformation` (definitions)

| SCIP Kind | Our label(s) | Parent edge | Notes |
|---|---|---|---|
| `Class` | `CodeClass` | module/class `CONTAINS` + `DEFINES` | relationships → `INHERITS` |
| `Interface` | `CodeInterface` | module `CONTAINS` + `DEFINES` | relationships → `INHERITS` |
| `Method` | `CodeMethod` | class `CONTAINS`; module `DEFINES` | relationships → `OVERRIDES` / `IMPLEMENTS` |
| `Function` | `CodeFunction` | module `CONTAINS` + `DEFINES` | |
| `TypeAlias` | `CodeTypeAlias` | module `CONTAINS` + `DEFINES` | |
| `Variable`, `Constant` | `CodeVariable` | module `CONTAINS` + `DEFINES` | |
| `Field`, `Property` | `CodeAttribute` | class `CONTAINS` | |
| `Module`, `Namespace` | `CodeModule` | file `CONTAINS` | |
| Constructor | `CodeMethod` + `is_constructor` | class `CONTAINS` | |
| Enum / EnumMember | `CodeClass` / `CodeAttribute` | | optional |

### 9.2 From `Occurrence` roles

| Role bit | Action |
|---|---|
| `Definition` | Create/update the definition node (or use the matching `SymbolInformation`). |
| `Import` | Create `CodeImport` node + `IMPORTS_SYMBOL` edge from module to imported symbol. |
| `Reference` | Create `REFERENCES` or `CALLS` edge from enclosing definition to target symbol. |
| `WriteAccess` | Same as reference, property `access="write"`. |
| `ReadAccess` | Same as reference, property `access="read"`. |

### 9.3 From `Relationship`

| Field | Edge |
|---|---|
| `is_implementation` | `IMPLEMENTS` |
| `is_type_definition` | `USES_TYPE` |
| `is_reference` + `is_definition` | alias / re-export; emit `EXPORTS` or `ALIAS` |

## 10. Incremental sync strategy

SCIP is project-wide, but `IncrementalSync` is per-file. Reconcile them as follows:

1. `full_sync` calls `adapter.prepare_project(ctx, all_files, changed_files)`.
2. `TypeScriptLanguageAdapter.prepare_project`:
   - Computes `fingerprint = hash(sorted(file_path + hash for every .ts/.tsx file))`.
   - Reads stored fingerprint from graph metadata via `index_metadata` query.
   - If fingerprint unchanged and a parsed SCIP cache exists, do nothing.
   - Else, run `ScipTypeScriptIndexer.ensure_index()`.
   - If successful, parse `index.scip`, translate every document to a `CodeGraphDelta`, and store in `self._scip_cache: dict[Path, CodeGraphDelta]`.
3. `TypeScriptLanguageAdapter.extract_file(path, ctx)`:
   - If `path` in `_scip_cache`, enrich with Tree-sitter and return.
   - Else, fall back to pure Tree-sitter extraction (current behavior).
4. `IncrementalSync` continues normally: deletes old file facts, inserts new delta, updates per-file content hash.

This means:
- When one TS file changes, we re-run `scip-typescript` on the whole project (compiler requirement) but only sync the changed files.
- Unchanged files retain their previous graph facts, which is correct because their content hashes are unchanged.
- Deleted files are still cleaned up by `IncrementalSync`.
- The fingerprint is stored under the graph metadata key `scip_typescript_fingerprint`.

Future optimization (Phase 3): cache `index.scip` on disk and use `scip-typescript`'s own incremental compilation support if/when it becomes available.

## 11. Configuration

Add to `AnalyzerConfig`:

```python
scip_typescript_enabled: bool | None = Field(
    default=None,
    description="Enable scip-typescript. None = auto-detect.",
)
scip_typescript_path: str | None = Field(
    default=None,
    description="Path to scip-typescript binary, 'npx', or None to search PATH.",
)
scip_typescript_args: list[str] = Field(
    default_factory=list,
    description="Extra CLI args passed to scip-typescript.",
)
scip_typescript_infer_tsconfig: bool | None = Field(
    default=None,
    description="Use --infer-tsconfig for JS-only projects. None = auto.",
)
```

Environment-variable overrides:
- `ARIADNE_SCIP_TYPESCRIPT_ENABLED`
- `ARIADNE_SCIP_TYPESCRIPT_PATH`
- `ARIADNE_SCIP_TYPESCRIPT_ARGS`
- `ARIADNE_SCIP_TYPESCRIPT_INFER_TSCONFIG`

Default behavior:
- If `scip_typescript_enabled=False`, never use SCIP.
- If `None`, use SCIP only when `scip-typescript` (or `npx`) and a `tsconfig.json`/`package.json` are present.

## 12. Capabilities & fallback

Extend `RuntimeCapabilities`:

```python
@dataclass(frozen=True)
class RuntimeCapabilities:
    typescript_extraction: bool
    semantic_embeddings: bool
    sqlite_vector_search: bool
    neo4j_backend: bool
    scip_typescript_indexer: bool   # ← new
```

Detection logic:
- `protobuf` importable.
- `scip-typescript` binary on PATH or `npx` available.

Report in `code_graph_capabilities` and `code_graph_index_status`:
- `extra`: `"typescript"` or a new `"scip"` extra depending on dependency split.
- `install`: `npm install -g @sourcegraph/scip-typescript` and `pip install -e ".[typescript]"`.

Fallback behavior:
| Condition | Behavior |
|---|---|
| `protobuf` missing | SCIP disabled; Tree-sitter only. Diagnostic: `missing_dependency`. |
| `scip-typescript` missing | SCIP disabled; Tree-sitter only. |
| No `tsconfig.json` / `package.json` | SCIP disabled; Tree-sitter only. |
| `node_modules` missing | Emit diagnostic; try `scip-typescript index --infer-tsconfig`; if fails, Tree-sitter. |
| `scip-typescript` exits non-zero | Emit diagnostic with stderr; Tree-sitter for all TS files this run. |
| SCIP parse fails | Emit diagnostic; Tree-sitter fallback. |

## 13. Testing plan

### Unit tests (`project/tests/test_typescript/test_scip_bridge.py`)
- Parse a minimal hand-built `index.scip` (created offline with `scip-typescript`) and assert expected node/edge counts.
- Test SCIP symbol → label mapping for each `Kind`.
- Test call-position detection with a fixture TS file.
- Test project-fingerprint computation.

### Mock subprocess tests
- Mock `asyncio.create_subprocess_exec` to simulate `scip-typescript` success/failure and assert fallback to Tree-sitter.

### Integration fixture
Create a tiny TS repo under `project/tests/fixtures/ts_scip_project/`:
```
package.json
tsconfig.json
src/
  utils.ts
  main.ts
```
- Run a full index through `ToolRegistry.handle_index`.
- Assert that a renamed import (`import { foo as bar }`) resolves to the original `foo` symbol.
- Assert barrel re-export chain is resolved.
- Assert `CALLS` edges exist for method calls.

### Mixed-ID acceptance test (intra-TypeScript)
The highest-risk case is **within TypeScript**, not across languages: when SCIP
indexes some TS files (canonical IDs = SCIP symbol strings) but others fall
back to Tree-sitter (legacy module-based IDs), an edge that crosses the two ID
schemes must still resolve to a real target node. This is where ID mapping
breaks silently.

> **Note — not a cross-*language* test.** Python and TypeScript do not import
> each other's symbols, and nothing in the pipeline emits Python↔TS edges
> (verified: edges resolve by `target` ID in `retrieval.py`, and no
> cross-language linking mechanism exists). A Python-caller → TS-callee edge
> would never be produced, so do **not** assert one. The risk is mixed-ID
> resolution *inside* the TS graph.

Construct the fixture so the two TS files land on different ID schemes — e.g.
force `utils.ts` onto the Tree-sitter path (exclude it from `tsconfig` includes,
or stub the SCIP cache to omit it) while `main.ts` is SCIP-indexed:

```text
repo/
  tsconfig.json
  src/
    main.ts         # SCIP-indexed  → canonical ID = SCIP symbol string
    utils.ts        # Tree-sitter fallback → legacy module-based ID
```
where `main.ts` calls a function exported from `utils.ts`.

Assertions:
- `code_graph_trace_dependencies` from `main.ts`'s function reaches the
  `utils.ts` function across the SCIP-ID → legacy-ID boundary via the
  `CALLS` / `IMPORTS_SYMBOL` edge.
- `code_graph_impact_analysis` on the `utils.ts` function returns the
  `main.ts` caller in the transitive affected set.
- `code_graph_search_code("utils")` returns the Tree-sitter node by `name`,
  and `search_code` for the `main.ts` symbol returns it by `name` even though
  its ID is a SCIP symbol string.

Run with and without `scip-typescript` installed: with it, the boundary is
SCIP-ID↔legacy-ID; without it, both files are Tree-sitter (legacy↔legacy) and
the same assertions must still hold — proving graceful degradation.

### cosmic_lens acceptance
- Manual run: `ariadne index /Users/amitkumarsingh/Documents/cosmic_lens`.
- Validate queries:
  - `code_graph_trace_dependencies` for a key service.
  - `code_graph_impact_analysis` for a shared hook.
  - `code_graph_search_code` still returns TS results.

All SCIP-specific tests must be skipped when `scip-typescript` is not installed (use `pytest.mark.skipif`).

## 14. Implementation phases

### Phase 1 — Foundation (mergeable, no behavior change when disabled)
1. Add `protobuf` to `[typescript]` extra (or new `[scip]` extra).
2. Vendor generated `_scip_pb2.py` from `scip.proto` and document regeneration script.
3. Add `ScipIndexParser` with plain-Python dataclass output.
4. Add `prepare_project` hook to `LanguageAdapter` protocol; wire into `IncrementalSync.full_sync`.
5. Extend `ExtractionContext` with `all_files` / `changed_files`.
6. Add `RuntimeCapabilities.scip_typescript_indexer` and update reports.

### Phase 2 — SCIP translation
1. Implement `ScipTypeScriptIndexer` (subprocess runner + fingerprinting).
2. Implement `ScipGraphTranslator`:
   - symbol → node + labels
   - occurrences → edges
   - relationships → edges
3. Implement `TreeSitterEnricher` to merge SCIP and Tree-sitter deltas.
4. Update `TypeScriptLanguageAdapter` to use SCIP when available and fall back to Tree-sitter.

### Phase 3 — Validation & hardening
1. Add fixture-based SCIP tests.
2. Add mock subprocess tests.
3. Run against `cosmic_lens` and fix gaps.
4. Add diagnostics for SCIP failures.
5. Optimize fingerprinting and caching.

### Phase 4 — Documentation
1. Update `docs/typescript-adapter-plan.md` to mark SCIP as the chosen refinement layer.
2. Update `AGENTS.md` with the new capability and optional dependencies.
3. Update `README.md` with install instructions for `scip-typescript`.

## 15. Risks & open questions

| Risk | Mitigation |
|---|---|
| `scip-typescript` requires `node_modules` to resolve imports. | Detect missing `node_modules`, emit diagnostic, fall back to Tree-sitter. |
| Large projects may have slow SCIP runs. | Fingerprint whole project; skip run when unchanged. Document `NODE_OPTIONS=--max-old-space-size=...`. |
| SCIP symbol IDs are long and may affect FTS/search ranking. | Store human-readable `name` / `qualname`; search by those fields. |
| Mixed SCIP/Tree-sitter IDs in the same graph could break **intra-TypeScript** queries when an edge crosses a SCIP-ID node and a Tree-sitter-ID node (e.g. SCIP-indexed `main.ts` → Tree-sitter-fallback `utils.ts`). *(Not a cross-language risk — no Python↔TS edges are emitted.)* | Make retrieval resolve by name/qualname (already does). Add the explicit mixed-ID (intra-TS) acceptance test described in §13. |
| Generated `_scip_pb2.py` may conflict with installed `protobuf` version. | Pin `protobuf>=4.25,<6`; regenerate bindings when protobuf major changes. |
| `scip-typescript` may not emit `Document.text`, leaving snippets empty. | Always enrich snippets from Tree-sitter. |

### Open questions to resolve during implementation
1. Should SCIP be a separate `[scip]` extra or bundled into `[typescript]`? **Recommendation:** bundle into `[typescript]` because it is part of the enhanced TS adapter; users who want only syntactic TS can still install `[typescript]` and skip installing `@sourcegraph/scip-typescript`.
2. Should we map SCIP symbols to our legacy module-based IDs instead of using SCIP symbol strings? **Recommendation:** use SCIP strings to avoid a complex descriptor parser; revisit only if retrieval UX suffers.
3. How should we handle `scip-typescript` workspace flags (`--yarn-workspaces`, `--pnpm-workspaces`) for monorepos? **Recommendation:** expose via `scip_typescript_args` initially; auto-detect later if needed.
4. Should we keep a separate `index.scip` artifact in `.ariadne/` for debugging? **Recommendation:** yes, write to `.ariadne/scip/<graph_id>/index.scip` and include the `scip-typescript` version in metadata.

## 16. Appendix: example SCIP symbols

From the `scip-typescript` snapshot output format:

```ts
// src/animal.ts
export interface Animal {
  sound(): string
}

// src/dog.ts
import { Animal } from './animal'
export class Dog implements Animal {
  public sound(): string { return 'woof' }
}

// src/main.ts
import { Dog } from './dog'
const d = new Dog()
d.sound()
```

SCIP symbols:
```
scip-typescript npm cosmic_lens 1.0.0 src/animal.ts/Animal#
scip-typescript npm cosmic_lens 1.0.0 src/animal.ts/Animal#sound().
scip-typescript npm cosmic_lens 1.0.0 src/dog.ts/Dog#
scip-typescript npm cosmic_lens 1.0.0 src/dog.ts/Dog#sound().
```

Expected edges after translation:
- `src/dog.ts` `IMPORTS_SYMBOL` `src/animal.ts/Animal#`
- `src/dog.ts/Dog#` `IMPLEMENTS` `src/animal.ts/Animal#`
- `src/dog.ts/Dog#sound().` `OVERRIDES` `src/animal.ts/Animal#sound().`
- `src/main.ts` `IMPORTS_SYMBOL` `src/dog.ts/Dog#`
- `src/main.ts/<main>` `CALLS` `src/dog.ts/Dog#` (the `new Dog()` constructor)
- `src/main.ts/<main>` `CALLS` `src/dog.ts/Dog#sound().`

This is the level of precision the integration should deliver for `cosmic_lens`.
