# Lumen Integration Plan

## Goal

Allow `lumen_ai` to use the standalone Code Hygiene MCP only after parity is
proven, while preserving an easy rollback to the current in-repo implementation.

## Current Golden Implementation

Current analyzer:

```text
/Users/amitkumarsingh/Documents/lumen_ai/scripts/analysis/code_hygiene
```

Current MCP integration:

```text
/Users/amitkumarsingh/Documents/lumen_ai/lumen_platform/applications/api/routers/mcp
```

## Integration Strategy

Keep the current implementation active. Add the standalone implementation
side-by-side behind configuration.

Possible environment flag:

```text
LUMEN_CODE_GRAPH_PROVIDER=current
LUMEN_CODE_GRAPH_PROVIDER=standalone
```

Default should remain `current` until the switch-over gates pass.

## Compatibility Layer

The standalone repo should optionally expose:

- `lumen_code_graph_retrieve` alias
- Lumen-style prompt context
- Lumen workspace root restrictions
- optional Lumen KG GraphStore adapter

The canonical standalone tool should still be generic:

```text
code_graph_retrieve
```

## Validation

Run both implementations against:

- curated fixtures
- selected `lumen_ai` paths
- runtime-error query examples
- known planning queries

Compare:

- top matches
- graph neighborhood
- source snippets
- diagnostics
- prompt context usefulness
- latency

## Rollback

Rollback should be a config change:

```text
LUMEN_CODE_GRAPH_PROVIDER=current
```

No destructive migration should be required.

## Switch-Over Criteria

Switch only when:

- Python adapter parity passes
- SQLite local retrieval and Neo4j persisted retrieval are stable
- incremental sync is correct or has an accepted fallback for Lumen workflows
- semantic and keyword search pass curated query tests
- expanded MCP analysis tools pass smoke tests
- architecture/community summaries are available or explicitly deferred by an
  accepted issue
- Lumen-specific diagnostics are either ported or intentionally scoped out
- agent workflow is not worse than current behavior
- fallback to current provider is tested
