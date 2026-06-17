# Python Adapter Parity Plan

## Golden Reference

Current implementation:

```text
/Users/amitkumarsingh/Documents/lumen_ai/scripts/analysis/code_hygiene
```

The standalone Python adapter should preserve useful behavior before adding new
features.

## Current Capabilities To Preserve

- deterministic file discovery
- Python AST parsing
- module and package structure
- import and import-from relationships
- class, function, async function extraction
- method extraction
- dataclass detection
- enum detection
- Pydantic model semantics
- FastAPI route detection
- variables, attributes, and type aliases
- `__all__` exports
- decorators and decorator semantics
- inheritance and override edges
- return type and used type edges
- call edges where currently available
- raised exception detection
- audit/diagnostic nodes where portable
- visualization enrichment if still useful
- source hash and parser version properties
- incremental indexing metadata

## Known Lumen-Specific Items To Isolate

- Lumen module path fallback rules
- `lumen_platform` path conventions
- facade contract checks
- DI-specific runtime probes
- service accessor probes
- graph visualization API coupling
- Lumen MCP tool names

These can become optional diagnostics or compatibility plugins.

## Fixture Strategy

Use three fixture classes:

- minimal Python package
- FastAPI/Pydantic package
- Lumen-derived fixture with facade/import edge cases

Compare:

- payload determinism
- node IDs
- node labels
- edge relationships
- required properties
- source snippets
- prompt context shape

## Parity Metrics

Acceptable initial parity:

- 95 percent or better node label coverage on curated fixtures
- 95 percent or better relationship coverage on curated fixtures
- exact preservation of core node ID strategy or documented migration map
- no missing `CodeFile`, `CodeModule`, `CodeFunction`, `CodeClass`, `CodeImport`,
  or `CodeExport` facts
- retrieval top-5 overlap for curated queries where possible

## Curated Query Set

Initial queries:

- file path query
- module name query
- class name query
- function name query
- FastAPI route path query
- missing import symbol stack trace
- circular import symptom
- facade export mismatch
- guarded import fallback
- sync factory returning async/coroutine-like value

## Exit Criteria

The Python adapter is ready when:

- it indexes `lumen_ai` fixtures deterministically
- it indexes `enterprise_tabular_ad`
- it returns useful context without importing Lumen runtime modules
- Neo4j persisted retrieval works
- current Lumen implementation can remain untouched

