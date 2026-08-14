# Pass 3 — Addendum: routines audit, messaging↔control reconciliation, operational concerns

> Predecessors: [`00-deep-audit.md`](00-deep-audit.md) (pass 1), [`08-second-pass-audit-and-expansion.md`](08-second-pass-audit-and-expansion.md) (pass 2).
> Addresses the remaining gaps called out internally after pass 2.

---

## A. [`hermes-already-has-routines.md`](../../../hermes-already-has-routines.md) — full read

Pass 1 missed this file at repo root. Reading it confirms:

| Trigger type | Hermes surface | Already shipped |
|---|---|---|
| Scheduled (cron) | `hermes cron create "<expr>" "<prompt>" [--script] [--skills] [--deliver] [--name] [--model] [--provider]` | yes |
| GitHub events | `hermes webhook subscribe <name> --events <list> --prompt "..." [--skills] [--deliver]` | yes |
| API triggers | `hermes webhook subscribe <name> --prompt "..." [HMAC auth, prompt template vars]` | yes |
| Script-before-agent injection | `hermes cron create ... --script ~/.hermes/scripts/foo.py` (stdout becomes context) | yes |
| Multi-skill chaining | `--skills "arxiv,obsidian"` | yes |
| Delivery fanout | `--deliver telegram` / `discord:...` / `slack:...` / `sms:...` / `github_comment` / `local` | yes |
| `[SILENT]` no-op return | "If NO_CHANGE, respond with `[SILENT]`" — the agent suppresses delivery on this sentinel | yes |
| Model-agnostic | any provider per-cron | yes |

### Implications for the stage docs

1. **Stage 0 verdict matrix update.** The "Cron" family verdict was already `covered` in pass 1; the addendum is that **`hermes webhook subscribe` is also covered** and was not listed in [`00-deep-audit.md`](00-deep-audit.md) §A.2. Stage 2 should add a `webhook` family alongside the cron family:

   | Tool | Wraps |
   |---|---|
   | `hermes_webhook_list` | `hermes_cli/webhook.py` list |
   | `hermes_webhook_show` | `hermes_cli/webhook.py` show |
   | `hermes_webhook_status` | aggregate |
   | `hermes_webhook_subscribe` (mutating, deferred to Stage 2.5) | `hermes_cli/webhook.py` subscribe |
   | `hermes_webhook_unsubscribe` (mutating, deferred) | … |

   `gateway/platforms/webhook.py` is the runtime; `hermes_cli/webhook.py` is the user-facing CLI surface.

2. **Stage 4 spawn entry points are not just cron.** A worker can be triggered by:
   - `hermes cron create ...` (scheduled — covered in Stage 4)
   - `hermes webhook subscribe ... --prompt ...` (event-driven — **not previously covered**)
   - kanban dispatcher (covered)
   - direct MCP `arcadia_agentops_spawn_*` (covered)

   Stage 4 should add a brief §"Webhook-triggered spawns" note: webhook-fired prompts run through the same gateway, so the agentops audit log captures them via the existing `processes.json` write — no new tool needed, but the doc should make this entry point explicit.

3. **`[SILENT]` return pattern.** Stage 4 `summarize_worker_result` should recognize `[SILENT]` as a valid no-op outcome (not a failure) — encode that in the worker handoff schema (see [§F](#f-worker-handoff-json-schema)).

4. **Script-injection is the cleanest Stage 4 pattern.** Per the routines doc, "The script handles mechanical work (fetching, diffing, computing); the agent handles reasoning." This maps directly onto the QA / log-reader split — Stage 4's `spawn_qa_worker` should support `--script` so the gate runner script (`Test-StageCHermesMcpFullParity.ps1`, `scripts/test.sh`) is the *script*, and the worker's prompt is purely "interpret the script's stdout."

### Stage 0 verdict matrix — corrected

Verdicts the Stage 0 audit doc must report (verdicts upgraded from pass 1 in **bold**):

| Family | CLI exists | In-process tool | Already published as MCP |
|---|---|---|---|
| Kanban | ✅ `hermes_cli/kanban.py` | ✅ `kanban_db.*` + `handle_function_call` | partial — verbs in `hermes_tools_mcp_server` codex-runtime only |
| Profiles + workers | ✅ `hermes_cli/profiles.py` | partial (no single dispatch fn) | no |
| **Cron** | ✅ `hermes_cli/cron.py` | ✅ `tools/cronjob_tools.cronjob` | no |
| **Webhooks** (new in pass 3) | ✅ `hermes_cli/webhook.py` + `gateway/platforms/webhook.py` | ✅ `cronjob_tools`-style API | no |
| Sessions + memory | ✅ scattered | partial (`hermes_state`) | no |
| Skills | ✅ `hermes_cli/skills_hub.py` | ✅ `agent/skill_commands.py` | partial — `skill_view`/`skills_list` in `hermes_tools_mcp_server` |
| Tools + health | ✅ `hermes_cli/doctor.py`, `status.py`, `logs.py` | ✅ | no |
| **Messaging bridge** (new in pass 3) | n/a | n/a | ✅ — `mcp_serve.py` already published |

---

## B. `mcp_serve.py` (messaging) ↔ `hermes-control` (Stage 2) reconciliation

Pass 1 was silent on whether they should federate. Decision:

**They stay independent servers.** Rationale:

1. **Different threading models.** [`mcp_serve.py`](../../../mcp_serve.py) runs an `EventBridge` background thread that polls platforms for incoming messages (`bridge.start()` on every server start, see [`mcp_serve.py` `run_mcp_server()`](../../../mcp_serve.py)). The control-plane server is pure request/response. Federating would pay the event-bridge startup cost on every control-plane spawn, including read-only sessions.
2. **Different blast radius.** Messaging tools can *send messages* — a leaked credential in a messaging tool config compromises Tony's Telegram/Discord. Control tools can mutate kanban/cron — leaks compromise project state. Separating means a worker that needs kanban reads does not also have a path to send a Slack message.
3. **Different upstream profile.** Messaging is platform-bridge specific (Telegram/Discord/Slack adapters in `gateway/platforms/`). Control is generic. The roadmap §Layer 1 says control is upstream-worthy; messaging is closer to a Hermes-specific addon.

**Naming convention to make this clear.** Add a "surface" suffix on the server CLI to avoid future operator confusion:

```
hermes mcp serve                 # legacy → maps to "messaging" surface, kept for back-compat
hermes mcp messaging serve       # explicit messaging
hermes mcp control serve         # Stage 2
hermes mcp control-mutate serve  # Stage 2.5
```

The Stage 0 audit doc must explicitly call out both servers and document that they are independent on purpose — not a TODO to merge.

### Optional bridge tool (not in scope)

If an orchestrator needs to atomically "complete card AND notify Telegram" it can call both servers in sequence. A cross-server transaction tool would be MCP-protocol-overloading; we don't ship one. The pattern is documented in `examples/orchestrator-recipes/notify-on-card-complete.md` (not committed in this batch).

---

## C. Token / payload budgets

Per [Pass 1 §C.3](00-deep-audit.md#c-cross-cutting-findings-that-affect-every-stage), redaction is mandatory. Pass 3 adds size budgets — every tool must respect:

| Limit (from profile `tool_output`) | Value (default) | Applies to |
|---|---|---|
| `tool_output.max_bytes` | 50000 | total payload bytes |
| `tool_output.max_lines` | 2000 | log-tail and search-result lists |
| `tool_output.max_line_length` | 2000 | per-line for log tails |

Rules:

1. Every tool that can return a list (kanban list, cron list, sessions list, brain search, agentops list_workers, etc.) takes a `limit: int` and `offset: int = 0` arg. Default `limit=50` (cron list), `limit=20` (search), `limit=100` (kanban list).
2. Every log-tail tool takes a `lines: int` arg with the existing profile default (200).
3. **Truncation is signaled, never silent.** A truncated payload returns `{...rows, _truncated: true, _total: <int>, _next_offset: <int>}`.
4. **No raw artifact-file return.** Tools return paths to artifact files, not the bytes. If the bytes are needed, the caller reads the path. This keeps token cost predictable and is the same pattern the launcher MCP uses for screenshots.

A test asserts every tool returning a list type has `limit`/`offset` args with documented defaults.

---

## D. Caching guidance

For Hermes' prompt-cache to amortize over multi-turn orchestration, control-plane tools should produce **stable, sorted output** so repeated calls don't bust cache prefixes.

| Rule | Why |
|---|---|
| Sort by stable key (created_at ASC, then id ASC) on every list response | Map ordering bias breaks cache hits |
| Round all timestamps to second precision in the rendered response | Sub-second jitter busts cache |
| Strip query-time fields (`now`, `request_id`, `duration_ms`) from the cached portion of the response — surface them in a sibling `_meta` block the caller can ignore | Same |
| Default time-window queries to a snapping window (e.g. "last 7 days" = trailing 7×86400 from UTC midnight, not from `now`) | Drifting windows break cache |

These don't change the tool surface but they do change implementation. Captured in the catalog test: a tool called twice in the same UTC second with the same args must return byte-identical payloads (modulo `_meta`).

---

## E. Concurrency rules per server

Pass 1 was implicit. Explicit table:

| Server | Tool class | Concurrency |
|---|---|---|
| `hermes-control` (R) | all | reentrant; readers don't lock |
| `hermes-control-mutate` | kanban mutating | serialized by kanban WAL — natural |
| `hermes-control-mutate` | `cron_create`/`cron_edit`/`cron_delete` | one in flight per `~/.hermes/cron.lock` |
| `arcadia-brain` | reads | reentrant |
| `arcadia-brain` | mutations | per-vault flock on `.brain-mutation-log.jsonl` |
| `arcadia-agentops` | spawn | per-profile lock on `processes.json` |
| `arcadia-agentops` | reap | global flock on `~/.hermes/locks/agentops-reap.lock` |
| `arcadia-pm` | all | per-`parent_card_id` advisory lock (sprawl guard) |
| `arcadia-release` | classify | pure — no lock |
| `eternia-launcher-qa` | fixture/media + screenshot | already serialized by [`fixture_mutex.dart`](../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server/lib/fixture_mutex.dart) |
| `eternia-backend` | full gate | global flock on `~/.hermes/locks/eternia-backend-gate.lock` |

---

## F. Worker handoff JSON schema

Stage 4's `summarize_worker_result` promised a "structured doctrine-shaped summary" without pinning it. Pin:

See [`schemas/worker_handoff.schema.json`](schemas/worker_handoff.schema.json) (committed in this batch).

Required fields, all sourced from [Agent QA & Release Doctrine §Evidence beats claims](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#evidence-beats-claims):

- `worker_id`, `profile`, `card_id`, `started_at`, `ended_at`
- `workspace`: `{cwd, commit, branch, dirty}`
- `commands`: `[{cmd, exit_code, duration_s, stdout_artifact_path?, stderr_artifact_path?}]`
- `artifacts`: `[{kind, path, sha256, size_bytes}]`
- `screenshots`: `[{label, path, blank_pixel_ratio}]` (launcher-QA only)
- `redaction_scan`: `{files_scanned, findings, patterns_checked}`
- `not_tested`: `[string]` — scope explicitly not covered
- `silent_outcome`: `bool` — true if the worker returned `[SILENT]` (no-op)
- `failure_class?`: one of the [error classes](07-cross-cutting.md#2-error-classes--one-taxonomy-every-server)
- `owner_recommendation?`: `tony` / `alice` / `pm` / `reviewer` / `same_card`

Every Stage 4 spawn writes one of these to `qa-artifacts/<run_dir>/worker_handoff.safe.json`. `summarize_worker_result` reads and returns it verbatim (after redaction re-scan).

---

## G. Migration path: codex-runtime kanban tools → Stage 2.5

Once Stage 2.5 ships, the same `kanban_create` operation is callable through two MCP surfaces in a Codex-runtime session:

1. `hermes_tools_mcp_server.py` (the existing callback) — exposes `kanban_create` for workers spawned with `openai_runtime=codex_app_server`.
2. `hermes-control-mutate` — exposes `hermes_kanban_create_card` for any session.

**They are not redundant.** Routing:

| Caller | Use |
|---|---|
| Codex-runtime *worker* with `HERMES_KANBAN_TASK` env | existing `kanban_*` via `hermes_tools_mcp_server` callback (the env gate is what makes `kanban_complete` safe — the worker can only complete its own task) |
| Codex-runtime *orchestrator* (no `HERMES_KANBAN_TASK`) | new `hermes_kanban_*` via `hermes-control-mutate` |
| Hermes-native orchestrator | new `hermes_kanban_*` via `hermes-control-mutate` |
| Hermes-native worker | new `hermes_kanban_*` (read-only Stage 2) |

The tool name prefix difference (`kanban_*` vs `hermes_kanban_*`) is deliberate so an audit log entry tells you which path was taken.

Deprecation: none. `hermes_tools_mcp_server.py` keeps its kanban verbs indefinitely because the codex-runtime callback shape is locked.

---

## H. Test fixture conventions

Every stage doc says "test asserts X." Where fixtures live:

```
tests/architecture/mcp-expansion/
  catalogs/                          # static catalog dumps for snapshot diff
    hermes_control_read.json
    hermes_control_mutate.json
    arcadia_brain.json
    arcadia_agentops.json
    arcadia_pm.json
    arcadia_release.json
    eternia_backend.json
  fixtures/
    sample_worker_handoff.safe.json
    sample_closure_manifest.safe.json
    sample_redaction_finding_log.txt
    sample_kanban_db.sqlite         # checked-in seeded DB for read tests
    sample_profile/                  # a complete fake profile dir for spawn tests
      config.yaml
      cron/jobs.json
      processes.json
      state.db
  doctrine/
    rule_table.csv                   # extracted bullet text → rule id mapping
                                     # PM rule test re-parses the doctrine doc and diffs against this
  ci/
    test_catalog_no_drift.py
    test_redaction_zero_findings.py
    test_doctrine_rules_match.py
    test_fresh_session_discovery.py
```

Test responsibility split:

| Test | Scope | Runs in |
|---|---|---|
| Catalog snapshot | every server | unit |
| Redaction over fixture log | every server | unit |
| Doctrine bullets ↔ rules | PM, release | unit |
| Fresh-session discovery + real tool call | every server | integration (CI) |
| Per-tool live call against fake profile | every server | integration |
| Path-escape suite | brain MCP, sessions_export | unit |
| Concurrency / lock contention | mutating tools | integration |

The Stage 0 audit doc closes with this fixture skeleton committed (empty placeholders ok).

---

## I. Production touchpoints

Listed in one place so reviewer can grep:

- `arcadia_release_verify_credential_contract` — reaches k8s + Keycloak well-known endpoint (no secret values fetched).
- `eternia_backend_verify_secret_contract` — reaches k8s read-only (secret presence check).
- `eternia_backend_deploy_staging_image` — wraps `scripts/deploy-stagec-staging-image.ps1` (staging, not production).
- `arcadia_pm_escalate_gap` with `kind=prod_approval_needed` — writes a note; **does not page**. Tony reads escalations.

No tool calls production deploy. Per doctrine, that is Tony only.

---

## J. Pass-3 self-criticism (gaps remaining after this addendum)

1. **Multi-tenant kanban.** Roadmap mentions `tenant` in task shape ([`hermes_cli/kanban.py:_task_to_dict`](../../../hermes_cli/kanban.py)). Pass 3 didn't audit tenant semantics. Open question for pass 4: does `hermes_kanban_list_cards` filter by tenant automatically based on caller profile?
2. **Streaming MCP notifications.** `eternia_backend_start_local_infra` says "Streams progress as MCP notification events." FastMCP supports notifications; the docs don't pin the notification shape. Open for pass 4.
3. **Internationalization.** The Hermes locale system ([`locales/`](../../../locales/)) is not addressed — every doc assumes English error messages. If MCP tool errors should localize, that's a cross-cutting addition.
4. **MCP protocol version pinning.** All docs assume FastMCP current; none pin a minimum protocol version. Pass 4 should add a `min_mcp_protocol_version` field to each server's selftest output and assert it during discovery.
5. **`agent/transports/hermes_tools_mcp_server.py` kanban tool **`kanban_create`** is orchestrator-only** (gated on `HERMES_KANBAN_TASK` being unset). Pass 3 §G notes this but the test rule should be: assert no worker profile environment ever exposes both `HERMES_KANBAN_TASK=<id>` AND `kanban_create` simultaneously. Tighten in pass 4.

These do not block stages 0–6 + 2.5 + 4.5.
