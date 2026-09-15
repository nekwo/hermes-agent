# Stage 2 — Hermes Control MCP (MVP, read-only)

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Layer 1 + §Recommended staged build → Stage 2.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.1, §A.2, §B.1.

## Goal

Publish Hermes' own control plane (kanban / cron / profiles / sessions / skills / tools / health) as a **standalone, discoverable MCP server** that any agent — Hermes-native, Claude Code, Codex app-server, Cursor — can attach to. Stage 2 is **read-only**. Mutating control-plane tools land in a separate follow-up after the permission/audit design has been exercised against the read-only surface.

This is the only stage in the roadmap that is intended to be upstream-worthy. Stage 2's tool surface must therefore stay free of Arcadia/Eternia-specific paths, profile names, and conventions. (Arcadia-specific orchestration moves to Stages 3–5.)

## Decision-1: new server vs extend `hermes_tools_mcp_server.py`

**Recommendation: new standalone server.** Rationale:

1. `hermes_tools_mcp_server.py` is documented as the **codex-runtime escape hatch** ("hermes-tools-as-MCP for codex_app_server runtime"). Its tool list is curated against what Codex already has natively — extending it would re-tangle Codex curation with general control-plane semantics.
2. Stage 2 must be runnable as `hermes mcp serve --surface control` (or a sibling `hermes mcp control serve`) so it can be added to any profile's `mcp_servers` block. The `hermes_tools_mcp_server` is spawned implicitly by the codex app-server runtime and not directly user-configurable today.
3. Permission scoping: a profile that should not see kanban/cron mutations should be able to drop the control MCP from its `mcp_servers` without losing the web/browser/vision tools.

Proposed module: `hermes/control_mcp_server.py` (peer of [`mcp_serve.py`](../../../mcp_serve.py)), CLI entry `hermes mcp control serve`.

## Tool surface (read-only MVP)

Naming convention: `hermes_<family>_<verb>`. All tools take JSON-typed args, return JSON-encoded strings (FastMCP convention used by `mcp_serve.py`), and document `error_class` per [error classes](../mcp-expansion-roadmap.md#error-classes).

**Dispatch contract** (per [Pass 2 §R6](08-second-pass-audit-and-expansion.md#r6--model_toolshandle_function_call-dispatch-contract-stage-2-wrap-layer)): every tool dispatches via `model_tools.handle_function_call(name, args_dict)`, never direct module imports. This preserves argument validation, redaction, loop-guardrail counters, and gateway notifications. A test asserts every tool resolves through this path. The catalog shape is pinned in [`schemas/tool_catalog.example.jsonc`](schemas/tool_catalog.example.jsonc).

### Kanban (read-only)

Per [Pass 2 §R3](08-second-pass-audit-and-expansion.md#r3--kanban-boards-model-is-load-bearing-for-stages-2-and-5), every kanban tool takes an optional `board: str | None = None`. `None` resolves to the active board per `~/.hermes/kanban/current` + `HERMES_KANBAN_BOARD` env. Tools refuse board names not present in `~/.hermes/kanban/boards/` rather than silently falling back to `default`.

| Tool | Wraps | Notes |
|---|---|---|
| `hermes_kanban_list_boards` | walks `~/.hermes/kanban/boards/` + reads `current` | Returns `[{name, slug, active, task_counts: {todo,ready,running,blocked,done,archived}}]`. |
| `hermes_kanban_switch_board` | writes `~/.hermes/kanban/current` | Read tool that mutates only the active-board pointer file (not workers). Returns `{previous, current}`. Live workers keep their `HERMES_KANBAN_BOARD` env — switching the pointer doesn't migrate in-flight cards. |
| `hermes_kanban_list_cards` | `kanban_db.list_tasks` | Args: `board?`, `status` (optional list), `assignee?`, `limit` (default 50), `offset` (default 0). Returns `{rows, _truncated, _total, _next_offset}` per [Pass 3 §C](12-third-pass-addendum.md#c-token--payload-budgets). |
| `hermes_kanban_show_card` | `kanban_db.get_task` + comments + events | Args: `board?`, `card_id`. Returns `{task, comments, events}`. |
| `hermes_kanban_status` | aggregate over `list_tasks` | Args: `board?`. One-shot board health summary. |
| `hermes_kanban_search` | grep over title+body in `kanban_db` | Args: `query`, `board?`, `status?`, `limit` (default 20), `offset` (default 0). |

Deferred to Stage 2.5 (mutating): `create_card`, `assign_card`, `link_cards`, `comment`, `dispatch`. They are already implemented in `hermes_cli/kanban.py` — adding them is just exposing the existing functions, but per roadmap §Stage 2 *"no mutating tools until permission and audit design is proven."*

### Profiles + workers

| Tool | Wraps |
|---|---|
| `hermes_profiles_list` | walks `~/.hermes/profiles/` |
| `hermes_profiles_show` | reads `config.yaml` + summarizes (model, toolsets, mcp_servers, max_turns) **with redaction of any `api_key` / `secret` fields** |
| `hermes_profiles_check_toolsets` | diff config.yaml `toolsets:` against [`toolsets.py`](../../../toolsets.py) registry; report missing/orphan |
| `hermes_profiles_check_skills` | diff `~/.hermes/profiles/<p>/skills/` against `agent/skill_commands.py` registry |
| `hermes_workers_list` | reads `~/.hermes/profiles/<p>/processes.json`, filters live PIDs |
| `hermes_workers_tail_log` | tails a worker log; takes `profile`, `worker_id`, `lines` (default 200) |

### Cron

| Tool | Wraps |
|---|---|
| `hermes_cron_list` | `tools/cronjob_tools.cronjob(action="list")` → already JSON |
| `hermes_cron_show` | `cronjob(action="show", job_id=...)` |
| `hermes_cron_status` | one-shot: enabled count, scheduled count, paused count, last-error count |
| `hermes_cron_last_output` | reads `~/.hermes/profiles/<p>/cron/output/<job_id>.log` tail |

Deferred mutating: `cron_run`, `cron_pause`, `cron_resume`. Same rationale as kanban.

### Sessions + memory

| Tool | Wraps |
|---|---|
| `hermes_sessions_list` | reads session DB index, returns `[{session_id, started_at, last_activity, title, platform}]` |
| `hermes_sessions_search` | full-text over titles + first user message; deferred backend: SQLite FTS on existing `state.db` |
| `hermes_memory_status` | per [`agent/memory_manager.py`](../../../agent/memory_manager.py) — counts, last-flush timestamps |

Mutating `sessions_export` is deferred (writes to disk, blast radius).

### Skills

| Tool | Wraps |
|---|---|
| `hermes_skills_list` | walks `~/.hermes/profiles/<p>/skills/` + global registry |
| `hermes_skills_view` | reads skill markdown |
| `hermes_skills_check` | validates frontmatter, links, executable bits |

Mutating `skills_sync_profile` deferred.

### Tools + health

| Tool | Wraps |
|---|---|
| `hermes_tools_list` | dumps `toolsets.py` + active toolsets per profile |
| `hermes_toolsets_list` | `toolset_distributions.py` |
| `hermes_status` | profile + auth + gateway state — minimum-shape replacement for `~/.hermes/state.json` curl |
| `hermes_doctor` | shells `hermes doctor`, captures JSON summary; **no auto-fix** |
| `hermes_logs_tail` | tails `~/.hermes/profiles/<p>/logs/<name>.log` |

## Generic requirements (per roadmap §Layer 1, made concrete)

1. **Typed JSON schemas for every tool.** FastMCP infers from Python type hints — but every tool MUST also pin schemas in a module-level catalog (`hermes/control_mcp_tools.py`) so the catalog can be regression-tested without spinning the server. (Pattern lifted from `tool/stagec_qa_mcp_server/lib/tools.dart` — proven.)
2. **Read-only and mutating tools separated.** Stage 2 = read-only only. The mutating-tools follow-up gets its own server name (`hermes-control-mutate`) so a profile can opt in to one but not the other.
3. **Profile/role-scoped permissions.** Implemented at the `mcp_servers` config level — not inside the server. A worker profile that should not see kanban omits the entry. The server itself enforces a *secondary* check: if `HERMES_CONTROL_MCP_PROFILE_SCOPE` is set, every tool result is filtered to only return rows for that profile.
4. **Explicit `dry_run` for side effects.** Read-only tools don't need this; Stage 2.5 mutating tools MUST take `dry_run: bool = True` defaulting to true.
5. **Audit log for every mutation.** Stage 2.5 only — but the table schema is locked in Stage 2: reuse the existing kanban `events` table in per-profile `state.db` with a new event kind `control_mcp_mutation` (`task_id` nullable, payload includes `{tool, args, caller_session, dry_run, result_class}`).
6. **Redaction before any tool result reaches the model.** All output passes through `agent/redact.py` with the existing `redact_secrets: true` flag. Profile config dumps strip `api_key`, `*_secret`, `token`. Logs are tail-only and pre-scanned for `Bearer`, `pk_live_`, `sk_live_`, JWT shape (3 base64 segments). Reuse [`docs/stages/qa-reboot/scripts/Invoke-RedactionScan.ps1`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/docs/stages/qa-reboot/scripts/Invoke-RedactionScan.ps1) patterns.
7. **Deterministic error classes.** Per [error classes](../mcp-expansion-roadmap.md#error-classes). Every tool returns either a success envelope or `{"error_class": "MISSING_CONTEXT" | "AUTH_REQUIRED" | ...}` — never raw stack traces.
8. **Self-test command.** `hermes mcp control selftest` → exit 0 PASS, exits 2/3/4 mapped to error classes. Same shape as Stage 1's self-test.
9. **Unit tests + live discovery/tool-call tests.** Three layers: (a) catalog schema validation, (b) tool impl unit tests with mocked `kanban_db`/`cronjob_tools`, (c) live discovery test in CI that runs `hermes mcp test hermes-control` from a temp profile.

## Configuration

Reference `mcpServers` JSON to commit at `docs/architecture/mcp-expansion/examples/hermes-control.mcpServers.example.json`:

```jsonc
{
  "mcpServers": {
    "hermes-control": {
      "command": "hermes",
      "args": ["mcp", "control", "serve"],
      "env": {
        "HERMES_CONTROL_MCP_PROFILE_SCOPE": "${HERMES_ACTIVE_PROFILE}"
      },
      "timeout": 60,
      "connect_timeout": 10
    }
  }
}
```

Per-profile recommendations:

| Profile class | Add `hermes-control`? | Add (future) `hermes-control-mutate`? |
|---|---|---|
| `alice` | yes | yes |
| `pm` | yes | partial (kanban + cron status only) |
| `reviewer` | yes | no |
| `launcher-qa`, `claude_*_qa` | yes | no |
| `claude_launcher`, `gpt_*`, `spark_*` (dev workers) | yes (kanban-only) | no |
| `spark_logreader` | logs-only | no |

## Acceptance

Stage 2 is done when:

1. `hermes mcp control serve` runs from any profile and exposes the read-only tool surface above.
2. `hermes mcp list` from any profile that has the server configured shows it.
3. `hermes mcp test hermes-control` returns a green discovery dump.
4. A fresh Hermes session in `alice` calls each tool through the model and gets a redacted, schema-valid response.
5. The catalog regression test passes (schemas + `additionalProperties: false` on every input).
6. Redaction scan over a recorded request/response trace returns 0 findings.
7. No mutating tool exists in this surface — verified by a test asserting `tools/list` returns exactly the read-only set.

## Risks

- **Schema drift between FastMCP and the catalog.** Mitigation: a single test imports the catalog, walks `mcp.list_tools()`, and asserts equality.
- **Profile-scoped filter bypassed.** Mitigation: filter is applied by a single decorator wrapping every tool function; unit test asserts the decorator is present.
- **Redaction misses a new secret shape.** Mitigation: redaction patterns live in `agent/redact.py` as the single source of truth — Stage 2 must not maintain a parallel list.
- **Logs containing VM-service tokens.** Already a real failure mode (Stage C learning). `hermes_workers_tail_log` and `hermes_logs_tail` MUST redact before returning.

## Stage 2.5 — Mutating control-plane (sketch, not in scope)

After Stage 2 is green:

- `hermes_kanban_create_card`, `_assign_card`, `_link_cards`, `_comment`, `_dispatch`
- `hermes_cron_run`, `_pause`, `_resume`
- `hermes_sessions_export`
- `hermes_skills_sync_profile`

Gating:

- `dry_run: bool = True` default on every mutator.
- Audit row written to per-profile `state.db.events` (schema decided in Stage 2 — `control_mcp_mutation` kind).
- Tool not exposed unless `HERMES_CONTROL_MCP_ALLOW_MUTATIONS=1` in env (belt-and-suspenders alongside the `mcp_servers` config gate).
