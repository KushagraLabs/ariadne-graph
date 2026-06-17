# Project Origin & Early Goals

> Historical note. Ariadne Graph began as a standalone replacement for an
> in-repo code-intelligence MCP. This file preserves the original framing
> goals, non-goals, and reference repositories that guided the early build.
> It is no longer the public README; see the repository root `README.md`.

## Goals

- Provide a repo-agnostic MCP server for codebase navigation, impact analysis,
  and implementation planning.
- Support Python first with parity against the original in-repo analyzer.
- Add TypeScript support for frontend and full-stack repositories.
- Support local SQLite persistence and production Neo4j persistence without
  depending on a hosted KnowledgeGraphEngine.
- Add semantic search, keyword search, incremental sync, and architecture
  summaries as core agent workflows.
- Keep optional adapters for Lumen integration after parity was proven.

## Non-Goals

- Do not copy a full external KG platform into this repo.
- Do not make TypeScript support a blocking requirement for Python parity.
- Do not depend on a hosted service for local analysis.
- Do not require Node.js for the first TypeScript adapter implementation.
- Do not implement full bi-temporal graph history until a concrete workflow
  needs historical graph-state reconstruction.

## Planned Docs

- [Architecture](architecture.md)
- [Migration Plan](migration-plan.md)
- [Graph Store Design](graph-store-design.md)
- [Python Adapter Parity](python-adapter-parity.md)
- [TypeScript Adapter Plan](typescript-adapter-plan.md)
- [Lumen Integration Plan](lumen-integration-plan.md)
- [Review Gap Remediation Plan](review-gap-remediation-plan.md)
