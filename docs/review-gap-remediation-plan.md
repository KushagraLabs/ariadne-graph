# Review Gap Remediation Plan

This plan maps the June 2026 architecture review gaps to implementation changes
in the project roadmap.

## Priorities

Must-have before Lumen switch-over:

- expanded MCP tool surface across indexing, query, analysis, and code
  inspection
- hybrid retrieval with semantic vector search, keyword search, and graph
  traversal
- content-hash based incremental sync for live coding workflows
- first-class SQLite local backend with vector-search support where available

Should-have before broad production use:

- Tree-sitter TypeScript/TSX adapter without a mandatory Node helper process
- architecture summaries and community detection

Could-have after core parity:

- lightweight temporal metadata: `first_seen_at`, `last_modified_at`, and
  `source_commit`

Explicitly deferred:

- full bi-temporal graph history and Graphiti-style edge invalidation

## Gap Mapping

| Review gap | Planned response | Primary docs | Acceptance gate |
| --- | --- | --- | --- |
| MCP surface is too small | Expand from 5 tools to 14 tools covering indexing, query, analysis, and file inspection. | `architecture.md`, `migration-plan.md`, `linear-execution-plan.md` | Tool schemas and smoke tests exist for all planned tools. |
| No semantic search | Add provider-based embeddings, vector indexes, keyword search, and graph-neighborhood result expansion. | `architecture.md`, `graph-store-design.md`, `migration-plan.md` | Curated natural-language queries return ranked useful entities under the target latency budget. |
| No incremental sync strategy | Add per-file content hashes, dirty-file status, configurable polling, changed-file reparse, and deleted-file cleanup. | `architecture.md`, `graph-store-design.md`, `migration-plan.md` | File changes become visible to retrieval within the configured polling window without a full rebuild. |
| TypeScript parser adds Node runtime complexity | Use Tree-sitter TypeScript/TSX first; defer compiler or LSP refinement until measured gaps justify it. | `typescript-adapter-plan.md`, `architecture.md`, `migration-plan.md` | TS/TSX indexing works on `cosmic_lens` without a mandatory Node runtime. |
| No community detection or architecture view | Add Louvain or label-propagation community assignment plus architecture and community-listing MCP tools. | `architecture.md`, `migration-plan.md`, `linear-execution-plan.md` | Architecture summaries identify major modules, coupling, and representative files. |
| Neo4j-only production posture limits local adoption | Make SQLite the default local backend and keep Neo4j for shared or production deployments. | `graph-store-design.md`, `architecture.md`, `migration-plan.md` | SQLite indexes and retrieves a validation repo without a database service. |
| Temporal model is undefined | Add lightweight commit/timestamp metadata only; defer full bi-temporal history. | `architecture.md`, `graph-store-design.md` | Schema supports optional commit attribution without requiring LLM-based temporal extraction. |

## Roadmap Changes

The roadmap is resequenced so cross-cutting agent capabilities land before
language breadth:

1. Python parity remains the first functional milestone.
2. SQLite storage and incremental sync are promoted ahead of rich retrieval.
3. MCP tool expansion and semantic search are implemented before TypeScript.
4. TypeScript starts with Tree-sitter to avoid multi-runtime setup.
5. Community detection is a dedicated milestone before Lumen switch-over unless
   explicitly deferred by an accepted issue.

This keeps the migration compatible with the current Lumen golden reference
while closing the review gaps that would otherwise make the standalone MCP less
useful than competing code-intelligence systems.
