# 15 — Legacy Orchestrator Retirement (executing the ledger remainder)

> **Why this doc exists.** [03 — Retirement Ledger](03-retirement-ledger.md) marks every
> item R1–R8 ✅ and its own header warns "re-verify before acting, the tree moves."
> A grep re-verification on **2026-07-29** found the ledger's two headline claims are
> **materially wrong**, while everything else it claims is genuinely done. This doc is the
> staged plan for the actual remainder — the keystone item the ledger believed was closed.
>
> **The wrong claims** (03, "Already retired"):
> - *"`MissionStateMachine.next_action` is already **unconditionally** graph-routed … The
>   'two orchestrators in `next_action`' the old docs described is gone."*
> - *"`mission_plan_routing_enabled` / `enforce_routing` config switch — gone from code."*
>
> **The tree as of 2026-07-29:** `_mission_plan_routing_enabled` exists at
> `agent_runtime/state_machine.py:319` and gates `ensure_default_mission_plan` at three
> call sites (`:90` `next_action`, `:108` `next_actions`, `:233`). `_legacy_next_action`
> exists at `:328` and is the live fallback whenever `_blueprint_next_action` returns
> `None` (`:99`) — a full `if state == TaskState.X` ladder with its own helpers
> (`_legacy_dev_slot_for_task` `:390`, `_legacy_backend_first_burn_in` `:416`). Both
> orchestrators still run. That is precisely the condition 03 names as *"the single
> largest risk … running both orchestrators at once."*
>
> **Verified genuinely complete** (zero live hits, 2026-07-29): `has_typed_plan`,
> `_synthesize_plan_from_handoff`, `mirror_legacy_stages_from_plan`, `configured_personas`,
> `_ensure_launcher_handoff_stage`, `_ensure_no_edit_proof_handoff_stage`,
> `_repair_bounded_visual_proof*`, `LAUNCHER_RELEASED_BY_NEKO_FLAG`,
> `QA_COORDINATION_RELEASED_FLAG`, `hud_shape_index_for_role`, `role_checklist_hud`,
> `_STAGE46_REQUIRED_SKILLS`, `retired_owner_allowlist`. **R1–R8 need no further work.**

---

## Scope

| Stage | What | Risk |
|---|---|---|
| 15.1 | Prove reachability of the legacy path — instrument before deleting | none (additive) |
| 15.2 | Close the un-planned window at every task-creation entry point | low |
| 15.3 | Make routing unconditional — delete `_mission_plan_routing_enabled` | medium |
| 15.4 | Delete `_legacy_next_action` and its helper block | **highest** |
| 15.5 | Rename the `stage46_*` burn-in vocabulary | cosmetic |

**Ordering is not negotiable.** 15.4 before 15.2 would strand any task that still reaches
the ladder. 03's own warning applies: *delete in order, keep tests green at every step.*

---

### 15.1 — Prove the legacy path is (or is not) reachable

The ledger assumed unreachability and was wrong once already. Measure, don't assume.

**Affected files**
- `agent_runtime/state_machine.py` (`next_action` `:88`, `next_actions` `:106`, `_legacy_next_action` `:328`)
- `agent_runtime/observability.py` (existing event emission — reuse it, do not invent a channel)

**Implementation actions**
1. Emit a typed observability event (`orchestrator.legacy_fallback`) carrying `task_id`,
   `state`, and whether `mission_plan`/`stages` were absent, at the `:99` fallback and at
   each `_mission_plan_routing_enabled` false-return.
2. Do **not** change behavior in this stage. It ships alone so a real run can be observed.
3. Run the existing burn-in (`agent_runtime/burn_in.py`) and a normal goal, then report
   hit counts per call site.

**Proof**
```bash
python -m pytest tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_observability.py -q
rg -n "orchestrator.legacy_fallback" agent_runtime tests
```

---

### 15.2 — Close the un-planned window at every creation entry point

`_mission_plan_routing_enabled` returns `False` only for a task with **no** `mission_plan`,
**no** `stages`, and `config.mission_plan.enabled` false. Remove that shape at the source.

**Affected files**
- `agent_runtime/burn_in.py:505`, `persona_diagnostics.py:251`, `smoke.py:70`, `scope_control.py:254`
- `hermes_cli/web_server.py:8810`, `hermes_cli/harness.py:729,775,2896`
- `agent_runtime/goal_runner.py:179` (already correct — the reference implementation)

**Implementation actions**
1. Every site that constructs a `Task` with `mission_plan=None` builds a default plan, the
   way `goal_runner._create_task` already does via `DEFAULT_GOAL_BLUEPRINT_ID`.
2. Where a site genuinely must create a plan-less task, say so in a comment **and** in the
   15.4 report — that is a blocker for 15.4, not a footnote.

**Proof**
```bash
rg -n "mission_plan=None|mission_plan = None" agent_runtime hermes_cli   # zero, or each hit justified in-line
python -m pytest tests/agent_runtime/test_burn_in.py tests/agent_runtime/test_smoke.py tests/agent_runtime/blueprints -q
```

---

### 15.3 — Make routing unconditional

**Affected files**
- `agent_runtime/state_machine.py:90,108,233` (call sites), `:319` (definition)
- `agent_runtime/config.py:1051` `_mission_plan_config` / `MissionPlanConfig.enabled`

**Implementation actions**
1. Replace the three guarded calls with unconditional `ensure_default_mission_plan(mission)`.
2. Delete `_mission_plan_routing_enabled`.
3. Decide `MissionPlanConfig.enabled` explicitly: if no other consumer remains, delete the
   field and treat a config that still names it as a **typed config error**, matching how
   `terminal_envelope` handles ungrantable classes — never a silently-ignored key.

**Proof**
```bash
rg -n "mission_plan_routing_enabled|enforce_routing" agent_runtime hermes_cli tests   # zero
python -m pytest tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_config.py tests/agent_runtime/blueprints -q
```

---

### 15.4 — Delete the legacy ladder

Only after 15.1 shows zero live fallbacks and 15.2 leaves no plan-less creation path.

**Affected files**
- `agent_runtime/state_machine.py:99` (call), `:328–~430` (`_legacy_next_action`,
  `_legacy_dev_slot_for_task` `:390`, `_legacy_backend_first_burn_in` `:416`)
- Retain and re-home if still used by the typed path: `_has_blocked_qa_verdict` `:520`,
  `_has_resolved_incident_only_qa_block` `:536`, `_has_resolved_qa_output_incident` `:555`
  — **audit each; do not delete a helper the blueprint path calls.**

**Implementation actions**
1. `_blueprint_next_action` returning `None` becomes a typed, loud failure — not a silent
   fall-through to a second orchestrator. This is the same discipline today's fixes applied
   to `_selectedRoomInstance` and `PidPresence`: **refuse rather than guess.**
2. Delete the ladder and any helper that becomes unreferenced.
3. Keep `_LEGACY_RUNNING_TASK_STATE_VALUES` (`states.py:64`) — 03 is right that it is
   load-safety for deserialization, not routing. Do not touch it.

**Proof**
```bash
rg -n "_legacy_next_action|_legacy_dev_slot_for_task|_legacy_backend_first_burn_in" agent_runtime hermes_cli tests
python -m pytest tests/agent_runtime -q     # full suite; baseline 3711 passed / 0 failed
```

---

### 15.5 — Rename the `stage46_*` burn-in vocabulary

03's R5 gate greps bare `stage46` and still hits. The **skill map** it targeted is gone;
what remains is naming: blueprint ids `stage46_custom_{backend,launcher,cross_stack}_proof`,
the `stage46_custom_blueprint` risk flag, and `_apply_stage46_custom_blueprint`.

**Affected files**
- `agent_runtime/burn_in.py:105,109,117,121,133,137,553,564`
- `tests/agent_runtime/test_burn_in.py:305–316`, `tests/scripts/test_cert_streak.py:38`

**Implementation actions**
1. Rename to intent-named ids (e.g. `custom_launcher_proof`) — the stage number carries no
   meaning now that the stage is retired.
2. **Serialized-value check first:** if any persisted burn-in row or certification record
   stores these strings, add a read-compat map exactly like
   `_LEGACY_RUNNING_TASK_STATE_VALUES` rather than breaking old records.
3. Narrow 03's R5 gate to `_STAGE46_REQUIRED_SKILLS|stage46_` so it stops flagging prose.

**Proof**
```bash
rg -n "stage46" agent_runtime hermes_cli tests   # zero outside a documented read-compat map
python -m pytest tests/agent_runtime/test_burn_in.py tests/scripts/test_cert_streak.py -q
```

---

## Test obligations

Named specifically, audited against the tree — not "update tests".

**Existing tests that will break and must be updated, not deleted:**
- `tests/agent_runtime/test_state_machine.py` — `test_legacy_qa_stage_does_not_count_as_remaining_dev_work` (`:232`) and
  `test_typed_no_edit_investigation_repeated_legacy_context_routes_to_dev_delivery` (`:445`).
  Both name "legacy"; determine per test whether it pins ladder behavior (retarget to the
  typed path) or merely uses legacy fixture data (leave alone).
- `tests/agent_runtime/test_burn_in.py:305–316`, `tests/scripts/test_cert_streak.py:38` — 15.5 renames.

**New tests each stage must add:**
- 15.1 — the fallback event fires with the right payload on a deliberately plan-less task.
- 15.2 — each touched creation site yields `mission_plan is not None`, asserted per site.
- 15.3 — a task with no plan and `config.mission_plan.enabled` **false** still gets a plan.
- 15.4 — `_blueprint_next_action` returning `None` raises/returns the typed failure rather
  than silently routing; plus a regression pin that no second orchestrator exists.

## Skill obligations

Skills are the agents' operating manual — a skill describing retired behavior makes agents
act wrong even with correct code. All five name `blocked`/routing semantics and must be
audited in the same change as 15.4, then re-installed with
`hermes harness install-harness-skills` (the shared root is overwritten from
`docs/agent-runtime-harness/harness-skills/`, so edit the **repo source**, never
`.hermes/shared/skills`):

- `harness-runtime-model` — describes stage-graph routing; must not imply a legacy fallback.
- `harness-mission-lead` · `harness-qa-verdict` · `harness-dev-delivery` · `launcher-analyze-proof`
  — audit each `blocked` mention against 03's R7 ruling (blocked = escalation, not dead-end).

## Acceptance criteria

1. `rg "has_typed_plan|mission_plan_routing_enabled|enforce_routing|retired_owner_allowlist|_legacy_next_action|stage46"`
   over `agent_runtime hermes_cli tests` returns zero hits outside a documented read-compat map.
2. `python -m pytest tests/agent_runtime -q` ≥ 3711 passed, **0 failed**.
3. Exactly one orchestrator: `_blueprint_next_action` is the only action source, and its
   `None` case is a typed failure, never a fallback.
4. 03's "Already retired" section corrected — the two wrong ✅ claims replaced with what was
   actually found, so the next reader does not re-inherit them.
5. All five skills re-installed; repo source and shared root hash-identical.
