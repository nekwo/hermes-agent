# Stage 4 — AgentOps MCP

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Layer 2 → `arcadia_agentops_mcp` + §Recommended staged build → Stage 4.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.3, §D.

## Goal

Make worker spawn / supervision / reap a typed MCP surface so:

- Orchestrators (Alice, PM) can dispatch Claude / Codex / Spark / QA workers without leaning on bespoke PowerShell.
- Workers always run **durably** (off the foreground turn), with verifiable handles (kanban card id, log path, artifact manifest, commit hash).
- Stale processes are reapable from a single tool, not by hunting through `Get-Process flutter`.

This stage is **private**. It encodes the eight existing worker profiles and the doctrine "Long-running work should run durably, not inside fragile foreground chat turns" from [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#recovery-doctrine).

## Inventory (existing)

| Asset | Path | Role |
|---|---|---|
| Worker profiles | `~/.hermes/profiles/{claude_launcher,claude_launcher_qa,claude_backend,gpt-launcher,gpt_backend,spark_launcher,spark_backend,spark_docs,spark_logreader,spark_testwriter,brain-writer}/` | already split by role |
| Worker PID file | `~/.hermes/profiles/<p>/processes.json` | tracked per profile |
| Kanban dispatcher | `hermes_cli/kanban.py:dispatcher`, `hermes_cli/kanban_diagnostics.py` | current spawn path for kanban-driven workers |
| Cron-driven spawn | [`cron/jobs.py`](../../../cron/jobs.py), [`cron/scheduler.py`](../../../cron/scheduler.py) | scheduled spawn |
| Codex worker handoff tools | `kanban_complete`, `kanban_block`, `kanban_comment`, `kanban_heartbeat` exposed via [`agent/transports/hermes_tools_mcp_server.py`](../../../agent/transports/hermes_tools_mcp_server.py#L84) | already MCP-callable from inside a codex-runtime worker |
| QA reap script | `docs/stages/qa-reboot/scripts/Invoke-StageCProcessHygiene.ps1` | reaps stray Launcher PIDs |
| Spark pilot | [`docs/agent-handoffs/spark-pilot-2026-05-14.md`](../../agent-handoffs/spark-pilot-2026-05-14.md) | reference for spark worker pattern |

## Tool surface

### Spawn

| Tool | Args | Behavior |
|---|---|---|
| `arcadia_agentops_spawn_claude_worker` | `profile`, `card_id?`, `prompt`, `workdir?`, `timeout_s?`, `inherit_toolsets=true`, `dry_run=true` | Spawns a `claude_*` profile worker via the existing kanban dispatcher path. Returns `{worker_id, pid, log_path, kanban_card_id?, started_at}`. Profile must be one of the `claude_*` profiles in the allowlist. |
| `arcadia_agentops_spawn_codex_worker` | same shape, `profile` ∈ `gpt-*` | Spawns Codex worker (`openai_runtime=codex_app_server`). Returns same envelope. |
| `arcadia_agentops_spawn_spark_worker` | same shape, `profile` ∈ `spark_*`, `model?` override, `subtask_kind` (one of: `dev`, `docs`, `logreader`, `testwriter`) | Spawns Spark worker. Per [Pass 2 §R5](08-second-pass-audit-and-expansion.md#r5--spark--spark_logreader-profile-classes-stage-4): `subtask_kind=logreader` **ignores `inherit_toolsets`** and forces `toolsets: [logs, terminal-readonly]`. `subtask_kind=docs` forces vault-write-only toolsets. `subtask_kind=dev` and `testwriter` honor `inherit_toolsets`. The `processes.json` audit row records the *effective* toolset list. |
| `arcadia_agentops_spawn_qa_worker` | `profile="launcher-qa"`, `target` (`launcher`/`backend`), `gate` (`smoke`/`full_parity`/`full_gate`), `commit?`, `dry_run=true` | Spawns the QA harness — either the Stage 1 closure run on the launcher or `scripts/test.sh` on the backend. Returns the future artifact manifest path so the caller can poll. |

All spawn tools share:

- **`dry_run=true` default.** Returns the resolved command line, env, profile, workdir, and `kanban_card_id` (if any) without executing.
- **Allowlist gate.** `profile` must be in `agentops.allowed_profiles` config — refuse anything else, including newly-created profiles, until they are explicitly allowlisted.
- **Audit log row.** Single line into `~/.hermes/profiles/<caller>/processes.json` (`spawn` event) + a kanban event row when `card_id` is set.

### Monitor

| Tool | Args | Returns |
|---|---|---|
| `arcadia_agentops_list_workers` | `profile?`, `status?` (`running`/`stopped`/`stale`) | `[{worker_id, profile, pid, started_at, last_heartbeat, kanban_card_id?, status}]` |
| `arcadia_agentops_tail_worker_logs` | `worker_id`, `lines=200`, `stream=both/stdout/stderr` | Tail of the worker's log file. Pre-redacted via `agent/redact.py`. |
| `arcadia_agentops_summarize_worker_result` | `worker_id` | Parses the worker's kanban handoff (commands run, exit codes, artifact paths, classification) and returns a structured summary. **Never returns raw logs** — that's `tail_worker_logs` for. |

### Reap

| Tool | Args | Behavior |
|---|---|---|
| `arcadia_agentops_reap_stale_processes` | `profile?`, `older_than_s=3600`, `dry_run=true` | Lists or kills processes that: (1) are tracked in `processes.json`, (2) have no heartbeat for `older_than_s`. Live mode requires `dry_run=false`. Always writes a `processes.json` entry recording the reap. |
| `arcadia_agentops_kill_worker` | `worker_id`, `signal="TERM"`, `dry_run=true` | Targeted kill. Records to audit log. |

## Worker output contract

Every spawn tool returns a **future-tense manifest** the caller can poll without re-spawning:

```json
{
  "worker_id": "spark-2026-05-15-184211-7f3a",
  "profile": "spark_launcher",
  "pid": 28144,
  "started_at": "2026-05-15T18:42:11Z",
  "log_path": "X:/.../hermes/profiles/spark_launcher/logs/worker-7f3a.log",
  "kanban_card_id": "t_abc12345",
  "expected_artifact_root": "X:/.../qa-artifacts/spark_run_7f3a/",
  "dry_run": false
}
```

Per roadmap §`arcadia_agentops_mcp` rule: *"Worker outputs need verifiable handles: card IDs, log paths, artifact manifests, commit hashes."* The handoff written by the worker on completion must include all of these — the agentops MCP does not invent them retroactively.

## Worker doctrine encoded in tools

The roadmap §`arcadia_agentops_mcp` rules become test-enforced:

1. *"Long-running work should run durably."* — every spawn forks a detached process; the tool returns immediately with the handle. No tool waits for completion.
2. *"Worker outputs need verifiable handles."* — every spawn writes to a `card_id` (kanban) or creates one if unset. `summarize_worker_result` refuses to return a summary if no `card_id` is tracked.
3. *"Parent agents own spec/integration/checks."* — agentops MCP has **no** "auto-merge child result" tool. The parent reads `summarize_worker_result` and acts on it.

## Webhook-triggered spawns (Pass 3 §A.2)

Per [`hermes-already-has-routines.md`](../../../hermes-already-has-routines.md), Hermes already supports GitHub-event and API-trigger worker spawns via `hermes webhook subscribe ... --prompt "..."` ([`hermes_cli/webhook.py`](../../../hermes_cli/webhook.py), [`gateway/platforms/webhook.py`](../../../gateway/platforms/webhook.py)). These fire through the same gateway as cron jobs, so:

- A webhook-fired prompt that ends up spawning a worker is captured in `processes.json` the same way cron-fired and MCP-fired spawns are.
- Stage 4 does NOT add a separate "webhook spawn" tool — the existing `hermes_webhook_*` surface in Stage 2 covers subscription management, and the prompt's tool calls cover the actual spawn.
- Audit row in `control_events` includes `caller_session` = the webhook delivery id, so the trace is auditable across the boundary.

## Composition with cron

Cron jobs ([`00-deep-audit.md`](00-deep-audit.md#d-cron-job-context-relevant-to-stage-4-agentops)) already drive recurring watchers. Stage 4 must:

- Not double-notify: cron watchers that poll kanban state should continue using the existing scripts (`scripts/kanban_*.py`). Agentops adds new spawn paths, not parallel watchers.
- Provide `arcadia_agentops_spawn_qa_worker` so a cron job's prompt can call it instead of shelling `scripts/test.sh` directly — clean handoff into the Stage 5 release classification.

## Allowlist config

```yaml
agentops:
  allowed_profiles:
    claude_worker: ["claude_launcher", "claude_launcher_qa", "claude_backend"]
    codex_worker: ["gpt-launcher", "gpt_backend"]
    spark_worker:
      dev:        ["spark_launcher", "spark_backend"]
      docs:       ["spark_docs"]
      logreader:  ["spark_logreader"]
      testwriter: ["spark_testwriter"]
    qa_worker: ["launcher-qa", "launcher-qa-direct"]

  # Per Pass 2 §R5 — toolset overrides enforced regardless of inherit_toolsets.
  forced_toolsets:
    spark_logreader: ["logs", "terminal-readonly"]   # NO file write
    spark_docs:      ["file-vault-only", "logs"]     # vault paths only
    # spark_launcher / spark_backend / spark_testwriter honor inherit_toolsets

  default_timeout_s: 1800
  reap_threshold_s: 3600
  max_concurrent_per_profile: 2
```

The `processes.json` row written at spawn time records `effective_toolsets` so an after-the-fact audit can detect privilege creep.

## Acceptance

Stage 4 is done when:

1. `hermes mcp arcadia agentops serve` runs and exposes the 9 tools above.
2. Only Alice + PM profiles list `arcadia-agentops-mcp` in their `mcp_servers`.
3. Spawn tools refuse non-allowlisted profiles (test).
4. Every spawn produces a kanban card (existing or new) and a `processes.json` audit row.
5. `summarize_worker_result` returns a structured doctrine-shaped summary (commands run, exit codes, artifact root, classification) for both a passing and a failing reference worker run.
6. `reap_stale_processes` in `dry_run=true` lists the right PIDs; live-run kills only those PIDs and never PID 0 / current shell / unknown PIDs.
7. Audit-log replay test: walk `processes.json` for an artificial 1-hour run, assert every spawn has a matched `summarize_worker_result` or `reap_stale_processes` row.

## Risks

- **Race between spawn and kanban claim.** Existing dispatcher already handles this via `claim_ttl_seconds` (observed at 2700 on `launcher-qa` profile). Reuse — do not reinvent.
- **PID reuse on Windows.** Long-running PIDs can collide after OS reboot. Mitigation: `processes.json` records `(pid, started_at)` tuple; reap only acts if both match.
- **Detached process orphaning.** Windows `CREATE_NEW_PROCESS_GROUP` is the spawn flag of record. Tests must cover that the spawn tool returns even if the child crashes within 100ms.
- **Spawn loop.** A worker that spawns workers can deadlock the dispatcher. Mitigation: `max_spawn_depth` already exists in Alice's `config.yaml` (`delegation.max_spawn_depth: 1`). Agentops MCP must read this and refuse a spawn from a worker whose own depth ≥ limit.
- **Token leakage in logs.** Logs already pre-redacted via existing pipeline. `tail_worker_logs` must re-scan tail content with `agent/redact.py` before returning, in case redaction was bypassed by a child process.

## Out of scope

- Cross-machine spawn. Stage 4 is single-host. Distributed work is a separate roadmap.
- Auto-restart on worker failure. PM doctrine routes recovery via a new card, not a silent restart.
- Generic "spawn anything" tool. Each spawn tool is profile-class-typed on purpose.
