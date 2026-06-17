# Static analysis limitations

The Code Hygiene MCP server builds its dependency graph and impact analysis
from **statically extracted** facts.  This means the results are fast,
deterministic, and do not require executing user code, but it also means some
dynamic patterns are inherently invisible to the analyzer.

## What is fully resolved

- Python `import` and `from ... import ...` statements.
- TypeScript/TSX `import` and `export` statements, including `tsconfig.json`
  path aliases (e.g. `@/components/Foo`).
- Decorator edges, inheritance (`INHERITS`/`IMPLEMENTS`), overrides
  (`OVERRIDES`), and explicit call sites (`CALLS`).
- FastAPI routes and React component/hook labels.

## Known blind spots

The following patterns can cause dependency tracing and impact analysis to be
incomplete:

| Pattern | Why it is hard |
|---------|----------------|
| Dynamic dispatch | Virtual methods, `getattr(obj, name)(...)`, and plugin hooks are resolved by runtime type, not syntax. |
| `__import__` / `importlib` | Runtime import strings cannot be evaluated without executing code. |
| Factory patterns | `Container.get("SomeService")` and similar lookups bind symbols dynamically. |
| String-based routing | Frameworks that route by string identifiers (e.g. Celery task names, some RPC systems) hide the call target. |
| Conditional imports inside functions | The extractor records the import, but branches guarded by runtime flags are invisible. |
| Eval / exec | Arbitrary code execution cannot be analyzed statically. |

## Impact on tools

- `code_graph_trace_dependencies` and `code_graph_impact_analysis` may miss
  callers or callees when they are connected through any of the patterns above.
- `code_graph_find_hotspots` ranks symbols by syntactic fan-in/fan-out, so
  hotspots created by dynamic coupling will be under-reported.

## Recommended mitigations

1. Treat impact analysis as a **hint**, not a guarantee.  Always review changes
   that touch extension points, plugin registries, or dynamic dispatch.
2. Combine the graph tools with semantic search (`code_graph_search_semantic`)
   to find symbols that may be related through naming conventions even when
   static edges are missing.
3. For codebases that rely heavily on dynamic patterns, add explicit
   registration/dependency edges where possible (e.g. explicit DI wiring).
