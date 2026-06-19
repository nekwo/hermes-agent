# Stage 2.5 — Hermes Control MCP (mutating surface)

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Layer 1 (the mutating verbs the roadmap defers).
> Predecessor: [`03-stage-2-hermes-control-mcp.md`](03-stage-2-hermes-control-mcp.md) — read-only surface that this builds on.

Stage 2 deferred every mutating control-plane verb to "after the permission and audit design is proven." This is that follow-up.

## Scope

A **separate server** named `hermes-control-mutate`. Not a flag on the read-only server. Two server names so a profile can opt in to one but not the other purely by editing `mcp_servers`.

## Server boundary

```
hermes-control           → read-only (Stage 2)
hermes-control-mutate    → mutating  (Stage 2.5, this doc)
```

Both share the same in-process dispatcher (`model_tools.handle_function_call`) and the same `control_events` audit table — they differ only in which tools are exposed.

## Tool surface

### Kanban (mutating)

| Tool | Args | Behavior |
|---|---|---|
| `hermes_kanban_create_card` | `board?`, `title`, `body?`, `assignee?`, `priority?`, `tenant?`, `workspace_kind?`, `workspace_path?`, `skills?[]`, `parent_card_id?`, `dry_run=true` | Wraps `kanban_db.create_task`. If `parent_card_id` is set, also creates the `task_link` row. |
| `hermes_kanban_assign_card` | `board?`, `card_id`, `assignee`, `dry_run=true` | `kanban_db.assign_task`. |
| `hermes_kanban_reassign_card` | `board?`, `card_id`, `new_assignee`, `reason`, `dry_run=true` | Records previous assignee in event row. |
| `hermes_kanban_link_cards` | `board?`, `parent_card_id`, `child_card_id`, `dry_run=true` | `kanban_db.add_link`. |
| `hermes_kanban_unlink_cards` | `board?`, `parent_card_id`, `child_card_id`, `dry_run=true` | Reverse. |
| `hermes_kanban_comment` | `board?`, `card_id`, `body`, `dry_run=true` | `kanban_db.add_comment`. |
| `hermes_kanban_block` | `board?`, `card_id`, `reason`, `dry_run=true` | `kanban_db.set_status(blocked)` with `reason` in event payload. |
| `hermes_kanban_unblock` | `board?`, `card_id`, `dry_run=true` | Returns card to `ready`. |
| `hermes_kanban_dispatch` | `board?`, `dry_run=true` | One dispatcher tick. Returns `{claimed: [card_id...], skipped: [{card_id, reason}...]}`. |

`hermes_kanban_complete` is **not** in this surface — workers self-complete via the existing `hermes_tools_mcp_server.py` path (the `HERMES_KANBAN_TASK` env gate). Orchestrators do not retroactively close cards.

### Cron (mutating)

| Tool | Args | Behavior |
|---|---|---|
| `hermes_cron_run` | `job_id`, `dry_run=true` | Fires one off-schedule run; tracked via the existing cron-output store. |
| `hermes_cron_pause` | `job_id`, `reason`, `dry_run=true` | Sets `enabled=false`, `paused_at`, `paused_reason`. |
| `hermes_cron_resume` | `job_id`, `dry_run=true` | Reverses pause. |
| `hermes_cron_create` | `name`, `schedule`, `prompt?`, `script?`, `model?`, `provider?`, `skills?[]`, `deliver?`, `workdir?`, `enabled_toolsets?[]`, `dry_run=true` | Wraps `cronjob_tools.cronjob(action="create", ...)`. **`dry_run=true` by default — a misconfigured cron can spam Telegram in minutes.** |
| `hermes_cron_edit` | `job_id`, `patch`, `dry_run=true` | Partial update. Refuses `name` change if the new name is already taken. |
| `hermes_cron_delete` | `job_id`, `dry_run=true` | Hard remove; row archived via `control_events`. |

### Sessions

| Tool | Args | Behavior |
|---|---|---|
| `hermes_sessions_export` | `session_id`, `out_path` (must be inside the active artifact root), `format` (`jsonl`/`md`), `dry_run=true` | Exports a session via the existing `hermes_state` export path. Path-escape protected. |

### Skills

| Tool | Args | Behavior |
|---|---|---|
| `hermes_skills_sync_profile` | `profile`, `skills?[]` (default all), `dry_run=true` | Walks the global skill registry and the profile's skill dir, syncs deltas. Returns `{added, updated, removed}`. |

## `dry_run=true` is the default — load-bearing

Every tool in this surface defaults to `dry_run=true`. Live execution requires the caller to explicitly pass `dry_run=false`. Rationale:

- The most common orchestrator mistake is re-running a recovery routine with the same args without realizing it mutates state.
- `dry_run=true` returns the resolved action ("would set status=blocked on t_abc with reason='X'") which is enough for the model to verify intent before committing.
- Two-step confirmation maps cleanly onto Hermes' existing `approvals.mode: manual` config — manual mode users get an approval prompt on the live call, not on the dry-run.

A test enforces the default: every tool function signature must have `dry_run: bool = True` in position 1.

## Belt-and-suspenders env gate

Beyond the `mcp_servers` allowlist, the mutating server refuses every call unless `HERMES_CONTROL_MCP_ALLOW_MUTATIONS=1` is set in its env. Rationale: a stray `hermes-control-mutate` config in a worker profile (via copy/paste or a botched migration) still fails closed.

The env gate is set in the profile's `mcp_servers` block:

```yaml
mcp_servers:
  hermes-control-mutate:
    command: hermes
    args: ["mcp", "control-mutate", "serve"]
    env:
      HERMES_CONTROL_MCP_ALLOW_MUTATIONS: "1"
      HERMES_CONTROL_MCP_PROFILE_SCOPE: "${HERMES_ACTIVE_PROFILE}"
```

Removing the env var disables every mutating tool without removing the server entry.

## Audit log — every call (success or failure)

`control_events` row shape ([schema](schemas/control_events.sql)):

```jsonc
{
  "id": <auto>,
  "kind": "control_mcp_mutation",
  "tool": "hermes_kanban_block",
  "caller_profile": "pm",
  "caller_session_id": "s_2026-05-15-...",
  "args_json": "{\"board\":\"eternia-launcher\",\"card_id\":\"t_abc\",\"reason\":\"...\"}",
  "result_class": "ok" | "MISSING_CONTEXT" | "AUTH_REQUIRED" | ...,
  "dry_run": false,
  "created_at": <unixepoch>
}
```

- Every call writes one row, dry-run included. (Dry-run rows are how we audit "what almost happened.")
- Args are stored verbatim **after redaction** — `cron_create` prompts are scanned the same way log output is, and any matched pattern is replaced before the row is written.
- `control_events` retention follows the profile's `curator.archive_after_days` config (90d default).

## Approval routing

The profile's `approvals.mode` setting controls whether live calls require operator confirmation:

| `approvals.mode` | dry_run=false behavior |
|---|---|
| `auto` | runs without prompting; relies on `dry_run` discipline + audit log |
| `manual` | shells out to the existing Hermes approval UI; same path as a live shell command approval |
| `read-only` | refuses; `AUTH_REQUIRED` |

This reuses [`tools/approval.py`](../../../tools/approval.py) — no new approval surface.

## Permission gate (delta vs Stage 2)

Adds *to* Stage 2's read-only matrix:

| Profile | Stage 2 (R) | Stage 2.5 (R+W) |
|---|---|---|
| `alice` | yes | yes (all) |
| `pm` | yes | yes (kanban + cron only — sessions_export deferred) |
| `reviewer` | yes | **no** — reviewer is read-only by doctrine |
| `brain-writer` | yes | **no** — brain mutations go through Stage 3 |
| dev workers (`claude_*`, `gpt-*`, `spark_*` non-logreader) | partial (kanban-only) | **no** — workers do not mutate the control plane |
| `spark_logreader` | logs-only | **no** |

## Acceptance

Stage 2.5 is done when:

1. `hermes mcp control-mutate serve` runs from `alice` or `pm` and exposes the 18 tools.
2. A fresh Hermes session in `reviewer` does NOT list any mutating tool — verified by a test asserting `tools/list` exact-equals the Stage 2 read-only set.
3. Every mutating tool defaults to `dry_run=true` — assertion via reflection over the catalog.
4. Live calls without `HERMES_CONTROL_MCP_ALLOW_MUTATIONS=1` return `AUTH_REQUIRED`.
5. Every call writes a `control_events` row (test compares row count delta on a fixture call).
6. Redaction over a sample `cron_create` prompt containing a fake Bearer token strips it before the row is written.
7. Path-escape tests on `sessions_export` cover absolute paths, `..`, symlinks.

## Risks

- **Worker accidentally added to mutating surface.** Mitigation: triple gate — profile `mcp_servers` allowlist, `HERMES_CONTROL_MCP_ALLOW_MUTATIONS` env, `approvals.mode` resolution.
- **`dry_run=true` silently doing partial work.** Mitigation: every tool function takes `dry_run` and routes through a single decorator that asserts the underlying mutating function was *not* called when `dry_run=true`. Decorator is unit-tested.
- **`cron_create` storming a delivery channel.** Mitigation: dry-run default + `validate_schedule` step that rejects schedules with `every` < 1 minute unless an explicit `force_high_frequency=true` flag is set.
- **`sessions_export` exfil.** Mitigation: `out_path` must resolve inside the active artifact root (resolved via Stage 5's artifact-root config). Refused otherwise.
- **`skills_sync_profile` clobbering local edits.** Mitigation: detects modified-since-clone, returns a diff in `dry_run`; live call refuses if any tracked file has local mtime > global mtime, unless `force_overwrite=true`.

## Out of scope

- Mutating brain. Stage 3 owns that with its own append-only semantics.
- Mutating agentops. Stage 4 owns spawn/reap with its own audit shape.
- Mutating release classification. Stage 5 is pure; PM (Stage 4.5) does the routing.
- Cross-server transactions. A `pm_route_qa_gate` call that internally calls `kanban_block` + `cron_pause` is acceptable; an MCP-level transaction wrapping multiple servers is out of scope.
