# Stage 4.5 — Arcadia PM MCP

> Parent: [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md) §Layer 2 → `arcadia_pm_mcp`.
> Audit context: [`00-deep-audit.md`](00-deep-audit.md) §A.3 (pm profile), [`08-second-pass-audit-and-expansion.md`](08-second-pass-audit-and-expansion.md) §R8 (gap).

This stage was missing in the first pass. It sits between Stage 4 (agentops) and Stage 5 (release), because PM **consumes** agentops outputs and **routes into** release classification.

## Goal

Encode the PM workflow rules from [Agent QA & Release Doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#recovery-doctrine) as typed MCP tools so PM (the `pm` profile) can route `NEEDS_FIX`, reviewer failures, QA failures, and gate failures **as normal continuation states** instead of escalating every blocker to Tony.

The roadmap rule, made executable: *"PM asks Tony/Alice first only when scope, credentials, external access, backend/API semantics, or optional work changes."* Every other failure routes through tools in this server.

## Inventory (existing)

| Asset | Path | Role |
|---|---|---|
| `pm` profile | `~/.hermes/profiles/pm/` | PM operator (max_turns/model gated for PM personality) |
| Recovery doctrine | [`Agent QA & Release Doctrine.md` §Recovery doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#recovery-doctrine) | "same-repo recoverable failure → steer the active card", "different owner/repo or credential gap → one narrow recovery path", "avoid card sprawl", "PM must clear superseded blocked cards" |
| Reviewer doctrine | [same doc §Reviewer doctrine](../../../../../Unreal%20Engine/Engine/ArcadiaLabs_Brain/Agent%20QA%20%26%20Release%20Doctrine.md#reviewer-doctrine) | rejects placeholder screenshots, zero-file redaction, broad claims, local-only PASS, staging touching prod |
| Stage 4 worker summaries | `arcadia_agentops_summarize_worker_result` | PM's primary input |
| Stage 5 release classifications | `arcadia_release_classify` | PM's primary output target |
| Kanban event stream | `task_events` table in shared kanban.db | the substrate PM reads to detect gate failures |

## Tool surface

### Classification (read-only)

| Tool | Args | Returns |
|---|---|---|
| `arcadia_pm_classify_closure` | `worker_summary` (from `agentops_summarize_worker_result`), `gate_results[]` (from `release_collect_gate_results`) | One of: `recoverable_same_card`, `recoverable_new_card`, `escalate_tony`, `escalate_alice`, `superseded`, `clean_pass`. Pure function — no I/O. Mirrors the roadmap's "PASS / NEEDS_FIX / FAIL_NON_BLOCKING_TOOLING_PARITY / NOT_RUN_MISSING_CONTEXT" with PM-routing semantics added. |
| `arcadia_pm_explain_route` | `closure_classification` | Returns the rule-by-rule trace that produced the classification (which doctrine bullet matched). Critical for trust — PM cannot route silently. |

### Routing actions

| Tool | Args | Behavior |
|---|---|---|
| `arcadia_pm_route_qa_gate` | `target` (`launcher`/`backend`/`cross`), `commit`, `gate_results[]`, `dry_run=true` | If `clean_pass`: hands off to `arcadia_release_classify`. If `recoverable_same_card`: writes a `pm_steer` comment on the active running card via the kanban tool. If `recoverable_new_card`: calls `arcadia_pm_create_recovery_card`. If `escalate_*`: writes an escalation note (does not page). |
| `arcadia_pm_create_recovery_card` | `parent_card_id`, `repo`, `scope` (one of `same_repo`/`cross_repo`/`credential_gap`/`prod_approval_needed`), `summary`, `blocker_evidence_paths[]`, `dry_run=true` | Creates **one** narrow recovery card on the right board (matches roadmap rule "Avoid card sprawl"). Refuses to create a sibling if `parent_card_id` already has an open recovery descendant — returns the existing one. |
| `arcadia_pm_escalate_gap` | `kind` (`scope_change`/`credentials`/`external_access`/`backend_api_semantics`/`optional_work`), `summary`, `decision_needed_by?`, `dry_run=true` | Writes a structured escalation note to `ArcadiaLabs_Brain/escalations/<DATE>-<slug>.md` via [Stage 3 brain](04-stage-3-arcadia-brain-mcp.md). Does NOT spawn a card. Tony or Alice reads escalations. |
| `arcadia_pm_clear_superseded` | `card_id`, `superseded_by`, `reason` | Marks a blocked card as `archived` with a structured event row `kind: superseded`. Refuses without a `superseded_by` reference. |
| `arcadia_pm_fan_in_children` | `parent_card_id` | Reads every child card linked via kanban `task_links`, summarizes their results, and writes a single `pm_fan_in` comment on the parent. Encodes the doctrine rule "Parent/integration cards should fan in child results before one final verification gate." |

### Reviewer routing (read-only, advisory)

| Tool | Args | Returns |
|---|---|---|
| `arcadia_pm_check_reviewer_reject_signals` | `artifact_manifest` | Returns `[{signal, evidence}]` matching any of the rejection patterns from the doctrine: placeholder screenshot detected, redaction-scan zero files scanned, broad-claim language without commands, PASS on local-only rehearsal, staging→prod touch, env rewrite dropping unrelated keys, raw-secret-shaped strings in artifacts. |

This tool is advisory — it surfaces signals; reviewer decides. Stage 5 PASS classification *should not* depend on it (reviewer is a human role), but PM may use it to predict a reviewer rejection before creating a closure note.

## Classification rules (encoded from the doctrine)

`arcadia_pm_classify_closure` runs this rule cascade — top-to-bottom, first match wins:

```
1.  redaction.findings > 0                      → escalate_tony (always; redaction is sacred)
2.  AUTH_REQUIRED + scope==credentials          → escalate_tony  (only Tony rotates secrets)
3.  scope_change in worker_summary              → escalate_tony  (scope is Tony's call)
4.  external_access required                    → escalate_tony
5.  backend_api_semantics question              → escalate_alice (Alice owns API alignment)
6.  optional_work proposal                      → escalate_alice
7.  superseded by newer card                    → superseded
8.  same repo + hot artifacts + recoverable     → recoverable_same_card
9.  different repo OR credential gap OR exhausted same-card steering
                                                → recoverable_new_card
10. all gates green + reviewer signals clean    → clean_pass
11. fallthrough                                 → escalate_alice (PM can't classify; ask)
```

Each rule cites its doctrine bullet in `arcadia_pm_explain_route` output. New rules must reference a doctrine bullet — no implicit additions.

## Card-sprawl guard (load-bearing)

The doctrine bullet "Avoid card sprawl. Parent/integration cards should fan in child results before one final verification gate by default" is enforced by `create_recovery_card`:

- Walks `task_links` (parent → child) from `parent_card_id`.
- If any open (`todo`/`ready`/`running`/`blocked`) child exists with the same `scope` tag, the tool returns that child instead of creating a new one (`{action: "reused", card_id: ...}`).
- If a closed (`done`/`archived`) child exists, it's logged in the result but not blocking.
- A `force_new=true` arg overrides the guard, but writes `pm_card_sprawl_override` to `control_events` so the override is auditable.

## Permission gate

| Profile | `arcadia-pm` allowed? |
|---|---|
| `alice` | yes (full) |
| `pm` | yes (full) |
| `reviewer` | yes (read-only — `classify_closure`, `explain_route`, `check_reviewer_reject_signals`) |
| `brain-writer` | no |
| `claude_*`, `gpt-*`, `spark_*`, `launcher-qa*` | no (workers must not self-route) |

A worker that thinks it needs PM routing writes a kanban comment requesting it; the dispatcher / cron watcher escalates to PM. **Workers cannot call PM tools directly** — that's how card sprawl starts.

## Acceptance

Stage 4.5 is done when:

1. `hermes mcp arcadia pm serve` runs and exposes the 8 tools above.
2. `pm`'s `config.yaml` lists `arcadia-pm-mcp`; no worker profile does.
3. `classify_closure` returns the documented routing class for each of 11 scripted scenarios (one per rule). Doctrine-bullet citations in `explain_route` exact-match the bullet text.
4. `create_recovery_card` returns the existing sibling when one is open (test).
5. `clear_superseded` refuses without `superseded_by` (test).
6. `route_qa_gate` in `dry_run=true` produces the planned action set without writing.
7. Audit log: every routing action writes a `pm_route` event row to `control_events` (see [§R1](08-second-pass-audit-and-expansion.md#r1--audit-log-location-stage-2--stage-4--cross-cutting-4-correction)).

## Risks

- **PM auto-routing a credential failure.** Mitigation: rule 2 is explicit — `AUTH_REQUIRED + scope=credentials` always escalates. Test covers this.
- **Card sprawl via parallel PM invocations.** Mitigation: `create_recovery_card` acquires a SQLite advisory lock on `parent_card_id` for the read+write window. Race-tested.
- **PM masking a reviewer rejection.** Mitigation: `check_reviewer_reject_signals` is advisory only; Stage 5 PASS does not depend on it. The closure note records the signals as a separate section.
- **Doctrine drift.** Mitigation: every rule cites a doctrine bullet by exact text. A test loads the doctrine markdown, extracts bullets, and asserts every rule's cited string appears verbatim. If the doctrine changes, the test fails — forcing a deliberate update.

## Out of scope

- Auto-paging Tony. PM writes escalation notes; pagers are out-of-band.
- Auto-merging anything. PM never merges; reviewer/Tony do.
- Re-running gates. PM routes; agentops/release run.
