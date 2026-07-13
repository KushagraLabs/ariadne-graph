# Recipe: brief before you edit (`code_graph_change_briefing`)

Ariadne serves implementation agents across consumer repos. Every other analysis
tool is request/response over raw graph rows the agent must re-interpret, which
reintroduces the wrong-assumption problem Ariadne exists to kill. The
`code_graph_change_briefing` tool closes that gap: an agent calls it **before**
editing a file or symbol and gets a *digested* briefing — callers grouped by file
and module, import-cycle membership, layering findings, public-surface status,
coupling rank, and an index-freshness stamp — as short markdown plus structured
fields, with names and paths only (never bare node ids).

This is the **push-lite** path. There is no webhook; the consumer repo instructs
its own agents to pull a briefing at the right moment. Wire it in one of two ways.

## Option A — CLAUDE.md snippet (drop into the consumer repo's CLAUDE.md)

```markdown
## Before non-trivial edits: pull a change briefing

Before editing any production file or symbol (not for pure test/doc/config
tweaks), call the Ariadne MCP tool `code_graph_change_briefing` with the repo
path and the target `symbol` OR `file_path`. Read the `summary` and:

- If `cycle` is set, the file is in an import ring — expect ripple across every
  file in `cycle.path`.
- If `layering` is non-empty, your edit sits on an existing architecture
  violation; note it before adding more coupling.
- If `freshness.stale` is true, the graph is behind the working tree
  (`dirty_file_count` files changed since the last index) — treat findings as
  approximate and consider a reindex.
- Use `callers_by_module` to know who you may break; `truncated.callers`, when
  present, tells you the list was capped.

The briefing states facts and risks only — it does not prescribe an action.
```

## Option B — SessionStart hook (settings.json in the consumer repo)

A hook cannot know which file the agent will touch, so use it to *remind* the
agent of the workflow at session start rather than to fetch a specific briefing:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo 'Reminder: before editing a production file/symbol, call code_graph_change_briefing (Ariadne MCP) on it and read the summary for callers, cycles, layering, and index freshness.'"
          }
        ]
      }
    ]
  }
}
```

## Tool contract

Input (exactly one of `symbol` / `file_path`):

| field         | notes                                                         |
| ------------- | ------------------------------------------------------------- |
| `repo_path`   | repository root (absolute or relative)                        |
| `symbol`      | resolved to its owning file                                   |
| `file_path`   | repo-relative or absolute                                     |
| `max_callers` | cap per direction (default 20); truncation is reported        |

Output highlights:

- `summary` — agent-facing markdown (facts + risk notes, no prescriptions).
- `callers_by_module` — `{module -> [{file, depth, direct}]}`, paths only.
- `cycle` — `{scc_size, path}` or `null`.
- `layering` — `[{rule, message, from, to}]` for deep_import / layer_violation /
  upward_import findings on this file.
- `public_surface` — `{is_surface, module, deep_import_consumers,
  via_surface_consumers}` (empty unless `.ariadne/architecture.yml` declares
  surfaces).
- `coupling_rank` — file-level `{rank, ranked_within, pool_capped, metric,
  score}` or `null` (null when the file is outside the ranked pool).
- `truncated` — explicit caps, e.g. `{callers: {shown, total}}`. No silent caps.
- `freshness` — `{last_indexed, dirty_file_count, stale, sync_enabled}`.

The briefing composes existing internals only (impact/trace, diagnostics,
public-surface audit, hotspots, freshness); it runs no new analysis and is safe
to call as often as you edit.
