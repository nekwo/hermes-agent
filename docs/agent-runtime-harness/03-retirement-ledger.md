# 03 — Retirement Ledger

> **Status: the single worklist for deleting legacy code.** The codebase was rewritten
> several times; this ledger is the one place that tracks what the locked architecture
> ([01](01-architecture.md)) and the blueprint engine ([02](02-execution-engine.md))
> **delete**. It folds the scattered retirement notes (blueprint Stage 10, Stage 76C/76D,
> Stage 77 "what this retires") into one grep-gated checklist.
>
> **A box is "done" only when the legacy branch is _deleted_, not merely bypassed.** The
> gate for each item is an `rg` returning zero hits outside tests/migrations, plus a
> green test run. The single largest risk is running both orchestrators at once — delete
> in order, keep tests green at every step.
>
> Status keys: ✅ done · 🔜 ready (dependencies met) · ⛓ blocked (waiting on a prior item).
> Status below was grep-verified against the working tree on 2026-06-25 — re-verify
> before acting, the tree moves.

---

## Already retired (verified gone from the execution path)

These were completed by earlier passes; the symbols return zero live hits. Keep the
data-migration compatibility noted — do **not** re-delete the legacy serialized *values*.

- ✅ **Role-named `TaskState` members.** `TaskState` is reduced to generic lifecycle
  (`created/running/done/blocked/failed/cancelled`, `states.py:4`). The old role values
  (`dev_implementing`, `qa_approved`, …) survive **only** as
  `_LEGACY_RUNNING_TASK_STATE_VALUES` (`states.py:64`) for deserialization via
  `_missing_`. *Keep that map* — it is load-safety, not live routing.
- ✅ **`AgentState` role-shaped enum** (`AUDITING/DESIGNING_TESTS/…`) — gone from
  `states.py`.
- ✅ **Role-specific action enum members** (`retired_*_action`) and **role-named
  proof-gate functions** (`*_proof_satisfied` for implementation/verification/
  integration) — collapsed into `HarnessActionType.RUN_SLOT` (`actions.py`) and
  `stage_proof_satisfied`. Grep clean across `agent_runtime`/`hermes_cli`/`tests`.
- ✅ **Per-call UUID instances.** `unique_operator_persona_instance_id` /
  `free_floating_persona_instance_id_for` are gone from code (only referenced in the
  superseded Stage 76 doc, now deleted). Instance creation is unified on the canonical
  placement id (Stage 76A). Guard: no new `personainst_operator_<uuid>` files appear
  under `.hermes/agent-runtime/persona_instances/`.

- ✅ **The routing fork in `next_action`.** `MissionStateMachine.next_action` is
  **unconditionally graph-routed**: it calls `ensure_default_mission_plan(mission)` then
  `_blueprint_next_action`, and `_blueprint_next_action` is the **only** action source.
  Its `None` case raises `LegacyOrchestratorRemoved` — a typed refusal — instead of
  falling through to a second orchestrator.
  > **Correction (2026-07-29).** This box was marked ✅ on 2026-06-25 and was **false**
  > for a month. A re-grep found `_legacy_next_action` alive at `state_machine.py:328`
  > with its own full `if state == TaskState.X` ladder, reached at `:99` whenever
  > `_blueprint_next_action` returned `None` — the exact "two orchestrators at once"
  > condition this ledger's own header calls the single largest risk. **Actually
  > retired by [15 — Legacy Orchestrator Retirement](15-legacy-orchestrator-retirement.md)
  > on 2026-07-29**, in four measured stages (instrument → close the plan-less creation
  > window → make routing unconditional → delete the ladder). Stage 15.1 instrumented
  > the branch with the typed `orchestrator.legacy_fallback` event before deleting it,
  > precisely because this box had already been wrong once: measured **zero** hits
  > across the deterministic end-to-end smoke mission, all nine burn-in case shapes, and
  > a `goal_runner` goal. Do not re-mark a routing box from reading `next_action` alone —
  > read what its fallback returns to.
- ✅ **`mission_plan_routing_enabled` / `enforce_routing` config switch** — `enforce_routing`
  never existed in code; `_mission_plan_routing_enabled` is now gone. No routing on/off
  switch survives.
  > **Correction (2026-07-29).** Also false when marked. `_mission_plan_routing_enabled`
  > lived at `state_machine.py:319` and gated `ensure_default_mission_plan` at three call
  > sites (`next_action`, `next_actions`, `apply_decision`), so a task with no plan, no
  > legacy `stages`, and `config.mission_plan.enabled` false (**the default**) skipped
  > typing entirely and fell to the ladder. Deleted in stage 15.3.
  > `MissionPlanConfig.enabled` itself is **kept**: it still has a live non-routing
  > consumer, `mission_plan_hud_enabled` → `worker_actions.py` (worker-action HUD shape).
  > That is a projection fork, not a routing fork — retire it with the R1 projection
  > family, not here.
- ✅ **`retired_owner_allowlist` global** — no such symbol exists in code (`rg` clean).
- ✅ **Goal-runner tasks are typed from birth.** `_create_task` (`goal_runner.py:179`)
  already defaults to `DEFAULT_GOAL_BLUEPRINT_ID` (`= "neko_two_dev_default"`), so
  goal-runner missions instantiate a blueprint plan at creation.

Gate (must stay green):
```bash
rg "unique_operator_persona_instance_id|free_floating_persona_instance_id_for" agent_runtime hermes_cli
rg "DEV_IMPLEMENTING|QA_APPROVED|class AgentState|_proof_satisfied\b" agent_runtime/states.py agent_runtime/proof_gates.py
rg "mission_plan_routing_enabled|enforce_routing|retired_owner_allowlist" agent_runtime hermes_cli   # code-only: zero hits
rg "_legacy_next_action|_legacy_dev_slot_for_task|_legacy_backend_first_burn_in" agent_runtime hermes_cli tests   # zero hits
```

> **Standing rule learned from the two corrections above.** A routing box is "done"
> only when the grep is run against the tree *at the moment of marking* AND the
> `None`/else branch of the surviving router is read. "The typed path exists" is not
> evidence that the untyped path is gone.

---

## Remaining retirement (the real work)

Grouped by subsystem. Each item: the symbols, the files, the gate, and the owning stage.

### R1 — The residual `has_typed_plan` projection fork  ✅

**Re-scoped after a 2026-06-25 audit.** The dangerous part of R1 (the routing fork, the
config switches, the owner allowlist) is **already done** — see "Already retired" above.
What remains is the **projection-layer** `has_typed_plan` fork: ~30 call sites that
branch between the **graph projection** and a **legacy projection** for tasks that don't
yet have a typed plan, e.g. `legacy_projection=not has_typed_plan(task)`
(`role_checklists.py:232,324,369,422`, `ticker.py:570`), and the `current_plan_stage(...)
if has_typed_plan(task) else _current_stage(task)` fallbacks (`ticker.py:1377,1716`,
`context_builder.py:682`, `proof_gates.py:130`, `worker_actions.py:60`).

These branches are **only reachable while a task is un-typed** — the window between
creation and its first tick (`next_action` types every task via
`ensure_default_mission_plan`). Goal-runner tasks are already typed at birth; the un-typed
window survives only for the **other task-creation entry points** that pass
`mission_plan=None`:

- `agent_runtime/burn_in.py:505`, `persona_diagnostics.py:251`, `smoke.py:70`,
  `scope_control.py:254` (child tasks); `hermes_cli/web_server.py:8810`,
  `harness.py:729,775,2896`.

**Safe removal condition (do this first, then delete the fork):**
- [x] **Make every task-creation site build a default plan** (call
  `build_default_mission_plan` / set a `DEFAULT_TASK_BLUEPRINT_ID` plan), so
  `has_typed_plan` is provably true everywhere the projection code runs.
- [x] **Then collapse the projection branches** to the typed path unconditionally and
  delete the `legacy_projection=True` code + `_current_stage` legacy fallback. Each
  `has_typed_plan(task)` guard above becomes unconditional.
- [x] **Then delete `has_typed_plan`** and the legacy `Task.stages` reads it protected
  (folds into R2).

> ⚠️ Do **not** delete the projection branches before the first box is proven — an
> un-typed task hitting a removed fallback would crash the HUD/checklist render. This is
> the careful part the old "delete the ladder" framing hid.

Gate:
```bash
rg "has_typed_plan" agent_runtime hermes_cli   # target: only the definition + R2 removal
python -m pytest tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_context_builder.py tests/agent_runtime/blueprints -q
```

### R2 — Plan/stage duplication  ✅

- [x] **Per-task plan synthesis** `_synthesize_plan_from_handoff` (`mission_plan.py`)
  replaced by blueprint instantiation.
- [x] **Legacy `Task.stages` mirror** — delete `mirror_legacy_stages_from_plan`
  (`mission_plan.py`, 3 hits) once Launcher/snapshot read `mission_plan.stages` directly.

Gate:
```bash
rg "_synthesize_plan_from_handoff|mirror_legacy_stages_from_plan" agent_runtime hermes_cli tests
```

### R3 — Launcher / cross-stack special cases baked into the engine  ✅

Express these as ordinary blueprint stages/edges, then delete from `planning.py`:

- [x] `_ensure_launcher_handoff_stage` (`planning.py:1958`, called :209,:316,:663)
- [x] `_ensure_no_edit_proof_handoff_stage` (`planning.py:1714`, called :330)
- [x] `_repair_bounded_visual_proof_stage_from_neko_handoff` (`planning.py:2000`, called :317)
- [x] `LAUNCHER_RELEASED_BY_NEKO_FLAG`, `QA_COORDINATION_RELEASED_FLAG`, and the
  cross-stack release branches.

Gate:
```bash
rg "_ensure_launcher_handoff_stage|_ensure_no_edit_proof_handoff_stage|_repair_bounded_visual_proof|LAUNCHER_RELEASED_BY_NEKO_FLAG|QA_COORDINATION_RELEASED_FLAG" agent_runtime hermes_cli tests
```

### R4 — Role-shaped HUD → slot/stage-shaped  ✅

- [x] Replace `hud_shape_index_for_role`, `role_checklist_hud`, `_hud_role`
  (`context_builder.py`, 4 hits + `role_checklists.py`) with stage-derived HUD keyed on
  `owner_slot` + `objective` + `proof_gate` + outgoing edges. (02 §HUD)

Gate:
```bash
rg "hud_shape_index_for_role|role_checklist_hud|_hud_role" agent_runtime tests
python -m pytest tests/agent_runtime/test_context_builder.py -q
```

### R5 — Hardcoded skill map → persona + per-stage skills  ✅

- [x] Drop `_STAGE46_REQUIRED_SKILLS` (`config.py`) and the `stage46_*` install/readiness
  naming; source skills from `AgentPersona.skills` + blueprint stage `required_skills`.
  (02 §Skills) **Note:** `config.py` is currently modified in the working tree by the
  tool-visibility branch — coordinate to avoid a collision.

Gate:
```bash
rg "_STAGE46_REQUIRED_SKILLS|stage46_" agent_runtime hermes_cli tests
# Green = zero hits OUTSIDE the documented read-compat map: 15.5 renamed the
# stage46_* blueprint ids and kept `burn_in._LEGACY_CUSTOM_BLUEPRINT_IDS`
# (plus its pin in tests/agent_runtime/test_burn_in.py) so persisted legacy
# ids still resolve — those hits are the compat map working, not R5 regressing.
```

### R6 — Persona/profile config fallback  ✅

- [x] `configured_personas` config-default fallback (incl. `alice_supervisor →
  neko_supervisor` aliasing in `config.py`) retired in favour of persisted personas +
  on-demand profile→persona promotion.
- [x] Snapshot `agents = agent_store.list_all() or configured_personas(cfg)` fallback
  removed once personas are first-class records.

Gate:
```bash
rg "configured_personas" agent_runtime hermes_cli tests
```

### R7 — Soften the task layer to a HUD (Stage 76C)  ✅

Not a symbol deletion but a gating-behavior retirement. Phase it:

- [x] **Phase 1 — `BLOCKED` non-terminal.** Each `BLOCKED` setter
  (`planning.py`, `blueprints/routing.py`, `no_freeze_monitor.py`, `preflight.py`) and
  consumer (`goal_runner.py`) emits an escalation to the goal-owner chat and keeps the
  run loop alive. Prefer softening the `blueprints/routing.py` path first.
- [x] **Phase 2 — proof becomes advisory.** Where `GateResult.allowed` hard-blocks a
  transition (`planning.py`, `blueprints/routing.py`), surface `GateResult.missing` as
  HUD evidence on the goal-owner chat and let the conductor adjudicate.
  **`proof_gates.py` computation is untouched** — only its consumers change.
- [x] **Phase 3 — skills reflect it.** `harness-mission-lead`, `harness-qa-verdict`,
  `harness-dev-delivery`, `launcher-analyze-proof`: proof = evidence the goal owner
  adjudicates; blocked = an escalation to handle, not a dead-end. Re-install
  (`hermes harness install-stage46-skills`).

Gate:
```bash
python -m pytest tests/agent_runtime/test_proof_gates.py tests/agent_runtime/test_planning.py tests/agent_runtime/test_state_machine.py -q
```
(`test_proof_gates.py` must stay green — the computation is unchanged.)

### R8 — Retire the standalone persona pipeline (Stage 77)  ✅

- [x] Remove the separate `Goal → Neko → Dev → QA → Proof` strip; the agents are the
  graph nodes (one node = one agent). The agent-vs-stage split collapses — a node renders
  the blueprint stage **with its bound agent on it**.
  - Launcher `blueprint_editor/blueprint_graph_editor_page.dart`,
    `office/mission_office_host.dart`. Gate: `flutter analyze <changed paths>`.

---

## Dependency order (delete in this sequence)

```
R4, R5  ─── independent, start now (role-shaped HUD + skill map)
   │
R1 (keystone: collapse entry points, kill has_typed_plan fork)
   ├── R2 (plan/stage duplication)
   ├── R3 (launcher cross-stack special cases)
   └── R6 (persona config fallback)
R7 (soften task layer) ── can phase in parallel; Phase 2 touches R1/R3 files, sequence after R1
R8 (Launcher persona pipeline) ── after the graph UI lands
```

## Master deletion gate (Stage 10 "done" check)

```bash
python -m pytest tests/agent_runtime/blueprints tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_proof_gates.py -q
rg "has_typed_plan|mission_plan_routing_enabled|enforce_routing|retired_owner_allowlist" agent_runtime hermes_cli tests
rg "hud_shape_index_for_role|role_checklist_hud|_STAGE46_REQUIRED_SKILLS|stage46_" agent_runtime hermes_cli tests
rg "_synthesize_plan_from_handoff|mirror_legacy_stages_from_plan|configured_personas" agent_runtime hermes_cli tests
rg "_ensure_launcher_handoff_stage|_ensure_no_edit_proof_handoff_stage|_repair_bounded_visual_proof" agent_runtime hermes_cli tests
```

All greps return zero hits **outside** tests/migrations that assert legacy serialized
data upgrades, and the pytest run is green → the retirement ledger is complete.
