# TypeScript Adapter Plan

## Purpose

Add useful code graph analysis for TypeScript and TSX repositories, starting
with `/Users/amitkumarsingh/Documents/cosmic_lens`.

## Parser Choice

Use Tree-sitter TypeScript/TSX first.

Reasons:

- keeps the standalone MCP Python-native at runtime
- avoids a mandatory Node helper process
- parses incomplete code better during live editing
- gives the project a repeatable path for future language adapters
- supports declarative Tree-sitter query files for definitions, imports, calls,
  JSX components, hooks, and route-like exports

Deferred type-aware refinement option:

- **`scip-typescript`** (Sourcegraph SCIP indexer) — chosen as the refinement
  layer. See [`docs/scip-typescript-integration-plan.md`](./scip-typescript-integration-plan.md)
  for the full design. It runs the TypeScript compiler, produces a SCIP index,
  and gives compiler-accurate call, import, and type edges. It will be wired
  into the existing adapter as an optional, fallible, project-level pass while
  Tree-sitter continues to provide structural enrichment (complexity, React
  labels, snippets, decorators).

This should be added after syntactic Tree-sitter extraction gaps are measured
on fixtures and `cosmic_lens`. Avoid regex-based parsing except as a small
fallback for source text search.

## Capabilities

Initial useful scope:

- `.ts` and `.tsx` file discovery
- tsconfig loading
- path alias resolution
- import/export graph
- functions and async functions
- classes and methods
- interfaces and type aliases
- React components
- hooks
- route files and route-like exports
- syntactic call expressions where Tree-sitter can support practical extraction
- source snippets

## Node Types

Map TypeScript constructs into the common schema:

- `CodeFile`
- `CodeModule`
- `CodeFunction`
- `CodeMethod`
- `CodeClass`
- `CodeInterface`
- `CodeTypeAlias`
- `CodeVariable`
- `CodeImport`
- `CodeExport`
- `CodeReactComponent`
- `CodeHook`
- `CodeRoute`

## Relationships

Initial relationships:

- `CONTAINS`
- `DEFINES`
- `IMPORTS`
- `IMPORTS_SYMBOL`
- `EXPORTS`
- `CALLS`
- `IMPLEMENTS`
- `EXTENDS`
- `USES_TYPE`
- `RETURNS_TYPE`
- `ROUTES_TO`

## Cosmic Lens Validation

The adapter should be validated against:

```text
/Users/amitkumarsingh/Documents/cosmic_lens
```

Questions it should help answer:

- Where is a React screen or route implemented?
- What imports a specific hook or service?
- What components depend on a shared utility?
- What files are affected by changing a type?
- Where is a domain calculation surfaced in the UI?

## Implementation Notes

The TypeScript adapter should be implemented as:

- a Python adapter using `tree-sitter` and `tree-sitter-typescript`
- Tree-sitter query files for `.ts` and `.tsx`
- a shared JSON graph payload matching the common `LanguageAdapter` contract
- a tsconfig-aware resolver for path aliases and project roots

Optional TypeScript-native tooling can be added later as a refinement layer, not
as the initial implementation dependency. If introduced, it must keep a stable
JSON contract back to the Python core and must not become required for Python
analysis or local SQLite workflows.

## Exit Criteria

- indexes TS and TSX files
- honors tsconfig path aliases
- produces common graph payloads
- supports retrieval and snippets
- works without a mandatory Node runtime
- records measured gaps for type-aware call resolution and symbol binding
- adds enough semantic value to outperform plain text search
- has a concrete plan (and, when implemented, optional integration) for
  `scip-typescript` to close type-aware gaps without making Node a hard
  dependency
