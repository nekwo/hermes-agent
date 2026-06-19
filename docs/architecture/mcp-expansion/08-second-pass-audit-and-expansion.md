# Pass 2 — Deep audit revisions + expansion

> Pass 1: [`00-deep-audit.md`](00-deep-audit.md). This doc revisits the gaps in §E and updates assumptions across [`02`](02-stage-1-launcher-mcp.md)–[`07`](07-cross-cutting.md).

This is the "look again, harder" pass requested in the original task. It is the source of truth where it conflicts with pass 1.

---

## R1 — Audit log location (Stage 2 / Stage 4 / Cross-cutting §4 correction)

**Pass 1 assumption.** Per-profile `~/.hermes/profiles/<p>/state.db` reuses the kanban `events` table for the audit log.

**Pass 2 finding.** Wrong on two points:

1. `state.db` is the **session FTS store** ([`hermes_state.py:34`](../../../hermes_state.py)), not the kanban store. Schema version 11, owns `sessions` + FTS5 over messages, not task events.
2. The kanban DB is **shared at hermes-home root**: `~/.hermes/kanban.db` for the `default` board, or `~/.hermes/kanban/boards/<slug>/kanban.db` for additional boards ([`hermes_cli/kanban_db.py:1-69`](../../../hermes_cli/kanban_db.py)). Profiles deliberately collapse onto a shared board — *"Profiles intentionally collapse onto a shared board: it IS the cross-profile coordination primitive."* The active board on this machine is `eternia-launcher` (per `~/.hermes/kanban/current`).
3. The events table is `task_events`, keyed on `task_id` ([`kanban_db.py:818, 1665, 1705`](../../../hermes_cli/kanban_db.py)). Non-task events (control-plane mutations, agentops spawns, brain mutations, release classifications) do not have a natural `task_id` — wedging them in with `task_id=NULL` would distort `list_events` and the gateway's kanban-notifier watcher.

**Revised approach.**

- **Stage 2 / 2.5 control-plane audit log** → new sibling table `control_events` in the **shared** kanban.db (per-board, picked up automatically by the existing board switch). Columns: `id`, `kind` (`control_mcp_read`/`control_mcp_mutation`), `tool`, `caller_profile`, `caller_session_id`, `args_json`, `result_class`, `dry_run`, `created_at`. Reads stay un-audited by default (`HERMES_CONTROL_MCP_AUDIT_READS=1` opts in, expensive). Writes always audit.
- **Stage 4 agentops audit log** → reuse the existing `processes.json` per profile (already present in `claude_launcher`, `alice`, `pm`, etc.). Stage 4 doc §"Reap" was correct; only the cross-cutting §4 needs updating.
- **Stage 3 brain mutation log** → unchanged from pass 1: `.brain-mutation-log.jsonl` at each vault root.
- **Stage 5 release classifications** → write to the **arcadia vault** as a closure note via Stage 3 + a row in `control_events`. No new store.

Action: amend [`07-cross-cutting.md`](07-cross-cutting.md) §4 in a follow-up edit so audit-log targeting matches this revision (see [§R7](#r7--diffs-to-apply-to-pass-1-docs) below).

---

## R2 — Stage 0 Q3: codex-app-server already validates the standalone-server choice

**Pass 1 assumption.** Stage 2 should be a new standalone server (vs. extending `hermes_tools_mcp_server.py`).

**Pass 2 finding.** Confirmed and reinforced. The codex-app-server runtime ([`agent/transports/codex_app_server.py`](../../../agent/transports/codex_app_server.py), [`codex_app_server_session.py`](../../../agent/transports/codex_app_server_session.py), and [`website/docs/user-guide/features/codex-app-server-runtime.md`](../../../website/docs/user-guide/features/codex-app-server-runtime.md)) is **opt-in**, gated on `model.openai_runtime == "codex_app_server"`. When active, Codex owns the tool loop and **calls back into Hermes** via the `hermes-tools` MCP server registered in `~/.codex/config.toml`.

Three implications for the roadmap:

1. **`hermes_tools_mcp_server.py` is locked to the callback shape.** Its `EXPOSED_TOOLS` list is curated against "what Codex doesn't have natively." Adding kanban *read* tools here is fine (the kanban write tools are already exposed for Codex workers). Adding cron/profile/skills/health control tools would mis-shape it.
2. **A standalone `hermes-control` server is reachable by both Hermes-native sessions *and* a Codex-runtime session** — the Codex session just needs `hermes-control` added to `~/.codex/config.toml mcp_servers` (the Hermes-side profile config is mirrored into Codex via [`hermes_cli/codex_runtime_plugin_migration.py`](../../../hermes_cli/codex_runtime_plugin_migration.py)). This means the **Stage 2 standalone server is the right choice for both runtimes** — we get one server, configured once per profile.
3. **The `mcp_servers.codex` preset in [`hermes_cli/mcp_config.py:35`](../../../hermes_cli/mcp_config.py) confirms the inverse direction**: a Hermes-native session can also attach `codex mcp-server` as an MCP peer. Useful for delegating to Codex from inside a Hermes turn — not part of this roadmap, but worth noting for the Stage 0 audit doc.

Action: keep Stage 2 design as-is; expand Stage 0 Q3 verdict matrix to explicitly call out the bidirectional callback shape so the audit doesn't leave it implicit.

---

## R3 — Kanban "boards" model is load-bearing for Stages 2 and 5

**Pass 1 assumption.** "Kanban" is one global thing.

**Pass 2 finding.** Kanban supports **multiple boards** (per-project isolation), and the dispatcher injects `HERMES_KANBAN_BOARD` into worker subprocess env so workers pin to one board ([`kanban_db.py:25-53`](../../../hermes_cli/kanban_db.py)). The active board on this machine is `eternia-launcher` (verified at audit time).

Stage 2 must therefore:

- Take an optional `board: str | None = None` arg on every kanban tool; `None` means "active board per `~/.hermes/kanban/current` + `HERMES_KANBAN_BOARD` env."
- Expose `hermes_kanban_list_boards`, `hermes_kanban_switch_board` (read tool — returns the new active slug; mutates only the `current` file, not workers).
- Reject board names not present in `~/.hermes/kanban/boards/` to avoid silent-default fallback to `default`.

Stage 5 release-classification must:

- Pin the `board` it reads from (Stage 1 closure ⇒ `board=eternia-launcher`; cross-product closure ⇒ board=`default` or whatever covers the rollup).
- Refuse PASS if the artifact manifest cites a `board` different from the one the classifier was asked to assess.

Action: amend Stage 2 doc tool surface to include `board` arg (defaulting to active) on every kanban tool, and add `hermes_kanban_list_boards` (it was already in the read-only list but without the multi-board nuance).

---

## R4 — Stage C smoke credential contract (Stage 1 + Stage 5)

**Pass 1 assumption.** Stage 1 closure references the credential recovery doctrine but doesn't enumerate it.

**Pass 2 finding.** Per [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#stage-c--launcher-doctrine-summary), the hosted Stage C credential contract is **strictly typed**:

- Keycloak client: `qa-stagec-smoke`
- Realm: `EterniaStaging`
- k8s secret: `eternia-staging/stagec-smoke-credentials`
- Callback: `localhost:8890`

Recovery routine when the helper resolves the credential but Keycloak stays on `/login`:

1. Classify `auth_secret_invalid_or_stale`.
2. Resync Keycloak + k8s secret without printing secrets.
3. Remove secret-data `last-applied-configuration` annotations.
4. Prove browser PKCE.
5. Prove the full five-label matrix.

Implications:

- **Stage 1 closure manifest must include** a `credential_contract` field with `{keycloak_client, realm, k8s_secret_path, callback_url}` — Stage 5 verifies these match the configured contract before classifying PASS. A drift here is `AUTH_REQUIRED`, not PASS.
- **Stage 5 release MCP must surface** a tool `arcadia_release_verify_credential_contract` (read-only) that checks the live k8s secret name exists and the Keycloak client is reachable without exfiltrating secret values. This stays in Stage 5 §Collection.

Action: extend Stage 1 §"Closure artifact manifest" with the `credential_contract` block and add the Stage 5 tool to the §Collection table. (Updates noted in [§R7](#r7--diffs-to-apply-to-pass-1-docs).)

---

## R5 — Spark / spark_logreader profile classes (Stage 4)

**Pass 1 assumption.** Five Spark profiles handled by one `arcadia_agentops_spawn_spark_worker(subtask_kind=...)`.

**Pass 2 finding.** The Spark profiles are heterogeneous in **toolset shape**, not just name:

- `spark_logreader` — read-only; per roadmap §Permission model "read-only logs/artifacts unless explicitly promoted."
- `spark_docs` — write access scoped to vault paths (markdown/docs only).
- `spark_launcher`, `spark_backend` — repo-write workers (dev pair to `claude_*`).
- `spark_testwriter` — repo-write but scoped to `test/` and `*_test.dart` / `tests/`.

A single spawn tool that takes `subtask_kind` is correct, but the **per-class allowlist** must restrict not only the **profile name** (already in [`05-stage-4-agentops-mcp.md`](05-stage-4-agentops-mcp.md#allowlist-config)) but also the **toolsets** the worker inherits. Otherwise `spark_logreader` accidentally gets file-write via `inherit_toolsets=true`.

Revised rule:

- `arcadia_agentops_spawn_spark_worker(subtask_kind="logreader", ...)` ignores `inherit_toolsets` and forces the worker to start with an explicit `toolsets: [logs, terminal-readonly]` override.
- `subtask_kind="dev"` honors `inherit_toolsets=true` for the worker's own profile, never the caller's.

The Stage 4 `processes.json` audit row must include the **effective toolsets** the worker actually started with, so an after-the-fact audit can detect privilege creep.

Action: expand Stage 4 §"Spawn" and §"Allowlist config" with the per-class toolset override rule.

---

## R6 — `model_tools.handle_function_call` dispatch contract (Stage 2 wrap layer)

**Pass 1 assumption.** Stage 2 wraps `tools/cronjob_tools.cronjob` directly.

**Pass 2 finding.** The dispatch contract used by `hermes_tools_mcp_server.py` is `model_tools.handle_function_call(name, args_dict) -> result_json` ([`agent/transports/hermes_tools_mcp_server.py:112-115`](../../../agent/transports/hermes_tools_mcp_server.py)). This is the canonical Hermes tool dispatch path — it already routes through:

- argument validation (per the tool's registered schema),
- redaction of the result,
- the loop-guardrail counters (`tool_loop_guardrails` in profile config),
- the gateway notification path.

**Stage 2 must dispatch via `handle_function_call`, not call the underlying functions directly.** Direct calls would bypass guardrails and redaction. This was implicit in [`03-stage-2-hermes-control-mcp.md`](03-stage-2-hermes-control-mcp.md) — make it explicit.

Caveat: `_AGENT_LOOP_TOOLS` (memory / session_search / delegate_task / todo per `model_tools.py:493`) require AIAgent context and **cannot** be dispatched from a stateless MCP callback ([`hermes_tools_mcp_server.py:53-59`](../../../agent/transports/hermes_tools_mcp_server.py)). Stage 2 read-only kanban + cron + profile + skills tools are **not** loop tools — they go through `handle_function_call` cleanly.

Action: amend Stage 2 doc §"Tool surface" preamble to state the dispatch contract explicitly. Add a test that asserts every Stage 2 tool resolves through `model_tools.handle_function_call`.

---

## R7 — Diffs to apply to pass-1 docs

These are surgical amendments, not rewrites. Each row points at the file + section + nature of change.

| File | Section | Change |
|---|---|---|
| [`02-stage-1-launcher-mcp.md`](02-stage-1-launcher-mcp.md) | §"Closure artifact manifest" | Add `credential_contract: {keycloak_client, realm, k8s_secret_path, callback_url}` block per [§R4](#r4--stage-c-smoke-credential-contract-stage-1--stage-5). |
| [`03-stage-2-hermes-control-mcp.md`](03-stage-2-hermes-control-mcp.md) | §"Tool surface" preamble | "Every tool dispatches via `model_tools.handle_function_call(name, args)`, not direct module imports — preserves redaction + guardrails." (per [§R6](#r6--model_toolshandle_function_call-dispatch-contract-stage-2-wrap-layer)). |
| [`03-stage-2-hermes-control-mcp.md`](03-stage-2-hermes-control-mcp.md) | §"Kanban (read-only)" | Add `board: str | None = None` arg to every kanban tool, plus `hermes_kanban_switch_board` (read tool that mutates only `~/.hermes/kanban/current`). Per [§R3](#r3--kanban-boards-model-is-load-bearing-for-stages-2-and-5). |
| [`05-stage-4-agentops-mcp.md`](05-stage-4-agentops-mcp.md) | §"Spawn" + §"Allowlist config" | Per-class toolset override for Spark sub-kinds (`logreader` forces read-only toolsets). `processes.json` row records effective toolsets. Per [§R5](#r5--spark--spark_logreader-profile-classes-stage-4). |
| [`06-stage-5-release-mcp.md`](06-stage-5-release-mcp.md) | §"Collection" tool table | Add `arcadia_release_verify_credential_contract` tool, read-only. Per [§R4](#r4--stage-c-smoke-credential-contract-stage-1--stage-5). |
| [`07-cross-cutting.md`](07-cross-cutting.md) | §4 "Audit log — one store" | Replace "kanban events table in per-profile state.db" with the corrected matrix: `control_events` in shared kanban.db for control-plane; `processes.json` per profile for agentops; `.brain-mutation-log.jsonl` per vault for brain. Per [§R1](#r1--audit-log-location-stage-2--stage-4--cross-cutting-4-correction). |

Rather than edit the pass-1 docs in place, the diff table above is **canonical** — pass-1 docs link back here and readers reconcile. This keeps the "audit pass 1 → audit pass 2" reasoning visible instead of erasing it.

---

## R8 — New risks surfaced by pass 2

1. **Multi-board confusion.** A worker spawned without `HERMES_KANBAN_BOARD` set ends up on `default` even if the operator meant `eternia-launcher`. Mitigation: Stage 4 spawn tools refuse to spawn without an explicit `board` arg.
2. **Codex-runtime callback misroute.** If `hermes-control` is added to a profile but the profile is running under codex-app-server, the model could reach `hermes-control` through *two* paths (the Codex MCP callback + the Hermes-native MCP). Mitigation: Codex-side `~/.codex/config.toml` should list `hermes-control` once; the existing migration code in [`hermes_cli/codex_runtime_plugin_migration.py`](../../../hermes_cli/codex_runtime_plugin_migration.py) handles this for `hermes-tools` and the same pattern extends.
3. **`task_events` vs `control_events` audit confusion.** A reviewer auditing the kanban gateway watcher could miss control-plane mutations because they live in a sibling table, not `task_events`. Mitigation: Stage 2 §"Audit log" docs the sibling table explicitly; the gateway watcher only tails `task_events` on purpose — control-plane mutations are operator-visible via a separate `hermes mcp control audit` CLI tool (Stage 2.5 deliverable).
4. **`spark_logreader` privilege creep through `inherit_toolsets`.** Already mitigated in [§R5](#r5--spark--spark_logreader-profile-classes-stage-4), but worth flagging: the most likely real-world failure isn't a malicious actor, it's an orchestrator that calls `spawn_spark_worker(inherit_toolsets=true)` because that's the default everywhere else.
5. **Credential drift after secret rotation.** Stage 5's `verify_credential_contract` must not fetch the secret value — only check the secret resource exists and Keycloak's `/realms/EterniaStaging/.well-known/openid-configuration` is reachable. Pulling secret material into an MCP response is a hard `REDACTION_FAIL` per [§3 redaction](07-cross-cutting.md#3-redaction--single-pipeline).

---

## R9 — What pass 2 did NOT change

- Stage 0 deliverable shape — still a verdict-matrix audit doc.
- Stage 1 closure deliverables — still self-test, discovery proof, parity, screenshot/redaction, manifest.
- Stage 3 vault allowlist model — confirmed correct after re-reading the `.local.md` pattern.
- Stage 5 rubric — confirmed against [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md). Add `verify_credential_contract` per [§R4](#r4--stage-c-smoke-credential-contract-stage-1--stage-5); everything else stands.
- Cross-cutting permission matrix — confirmed against profile inventory.

---

## R10 — Next-pass follow-ups (not blockers)

If a pass 3 is needed:

- Index the actual `task_events` payload schemas to confirm `control_events` mirrors them cleanly.
- Trace `hermes_cli/profile_distribution.py` to confirm Stage 4 spawn re-uses its profile-clone path (don't reinvent profile clone).
- Audit `agent/transports/codex_event_projector.py` to confirm Stage 2 read tools don't accidentally surface raw codex events.
- Walk `tests/agent/transports/test_hermes_tools_mcp_server.py` to confirm Stage 2's test scaffolding can lift its fixtures.

None of these block the current stage docs — they sharpen the implementation when we get there.
