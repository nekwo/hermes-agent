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

Gate (must stay green):
```bash
rg "unique_operator_persona_instance_id|free_floating_persona_instance_id_for" agent_runtime hermes_cli
rg "DEV_IMPLEMENTING|QA_APPROVED|class AgentState|_proof_satisfied\b" agent_runtime/states.py agent_runtime/proof_gates.py
```

---

## Remaining retirement (the real work)

Grouped by subsystem. Each item: the symbols, the files, the gate, and the owning stage.

### R1 — The dual-orchestrator fork  ⛓ (the keystone; everything else rides on it)

The legacy ladder and the `has_typed_plan` fork are **still live** — 43 hits across 11
files. This is the keystone: until new goals are graph-routed from birth, the legacy
ladder cannot be deleted.

- [ ] **Collapse entry points.** `MissionRuntimeController._create_task`
  (`goal_runner.py`) instantiates the default blueprint onto `task.mission_plan` so
  `has_typed_plan` is true from birth; `goal run` thread `--blueprint`/`--bind`. (02 §1)
- [ ] **Delete the `has_typed_plan` / `mission_plan_routing_enabled` / `enforce_routing`
  fork.** `next_action` delegates to graph routing unconditionally.
  - Files (grep-verified): `state_machine.py`, `ticker.py` (6), `planning.py` (7),
    `context_builder.py` (4), `role_checklists.py` (6), `proof_gates.py` (2),
    `worker_actions.py` (2), `store.py` (2), `default_plan.py` (2), `mission_plan.py`.
- [ ] **`retired_owner_allowlist` global removed** (`planning.py`); owners validated
  per-blueprint against `plan.slots`.

Gate:
```bash
rg "has_typed_plan|mission_plan_routing_enabled|enforce_routing|retired_owner_allowlist" agent_runtime hermes_cli tests
python -m pytest tests/agent_runtime/test_state_machine.py tests/agent_runtime/blueprints -q
```

### R2 — Plan/stage duplication  ⛓ (depends on R1)

- [ ] **Per-task plan synthesis** `_synthesize_plan_from_handoff` (`mission_plan.py`)
  replaced by blueprint instantiation.
- [ ] **Legacy `Task.stages` mirror** — delete `mirror_legacy_stages_from_plan`
  (`mission_plan.py`, 3 hits) once Launcher/snapshot read `mission_plan.stages` directly.

Gate:
```bash
rg "_synthesize_plan_from_handoff|mirror_legacy_stages_from_plan" agent_runtime hermes_cli tests
```

### R3 — Launcher / cross-stack special cases baked into the engine  ⛓ (depends on R1)

Express these as ordinary blueprint stages/edges, then delete from `planning.py`:

- [ ] `_ensure_launcher_handoff_stage` (`planning.py:1958`, called :209,:316,:663)
- [ ] `_ensure_no_edit_proof_handoff_stage` (`planning.py:1714`, called :330)
- [ ] `_repair_bounded_visual_proof_stage_from_neko_handoff` (`planning.py:2000`, called :317)
- [ ] `LAUNCHER_RELEASED_BY_NEKO_FLAG`, `QA_COORDINATION_RELEASED_FLAG`, and the
  cross-stack release branches.

Gate:
```bash
rg "_ensure_launcher_handoff_stage|_ensure_no_edit_proof_handoff_stage|_repair_bounded_visual_proof|LAUNCHER_RELEASED_BY_NEKO_FLAG|QA_COORDINATION_RELEASED_FLAG" agent_runtime hermes_cli tests
```

### R4 — Role-shaped HUD → slot/stage-shaped  🔜 (independent of R1; can start now)

- [ ] Replace `hud_shape_index_for_role`, `role_checklist_hud`, `_hud_role`
  (`context_builder.py`, 4 hits + `role_checklists.py`) with stage-derived HUD keyed on
  `owner_slot` + `objective` + `proof_gate` + outgoing edges. (02 §HUD)

Gate:
```bash
rg "hud_shape_index_for_role|role_checklist_hud|_hud_role" agent_runtime tests
python -m pytest tests/agent_runtime/test_context_builder.py -q
```

### R5 — Hardcoded skill map → persona + per-stage skills  🔜 (independent of R1)

- [ ] Drop `_STAGE46_REQUIRED_SKILLS` (`config.py`) and the `stage46_*` install/readiness
  naming; source skills from `AgentPersona.skills` + blueprint stage `required_skills`.
  (02 §Skills) **Note:** `config.py` is currently modified in the working tree by the
  tool-visibility branch — coordinate to avoid a collision.

Gate:
```bash
rg "_STAGE46_REQUIRED_SKILLS|stage46" agent_runtime hermes_cli tests
```

### R6 — Persona/profile config fallback  ⛓ (depends on personas being first-class records)

- [ ] `configured_personas` config-default fallback (incl. `alice_supervisor →
  neko_supervisor` aliasing in `config.py`) retired in favour of persisted personas +
  on-demand profile→persona promotion.
- [ ] Snapshot `agents = agent_store.list_all() or configured_personas(cfg)` fallback
  removed once personas are first-class records.

Gate:
```bash
rg "configured_personas" agent_runtime hermes_cli tests
```

### R7 — Soften the task layer to a HUD (Stage 76C)  🔜 (largest behavior change; phase it)

Not a symbol deletion but a gating-behavior retirement. Phase it:

- [ ] **Phase 1 — `BLOCKED` non-terminal.** Each `BLOCKED` setter
  (`planning.py`, `blueprints/routing.py`, `no_freeze_monitor.py`, `preflight.py`) and
  consumer (`goal_runner.py`) emits an escalation to the goal-owner chat and keeps the
  run loop alive. Prefer softening the `blueprints/routing.py` path first.
- [ ] **Phase 2 — proof becomes advisory.** Where `GateResult.allowed` hard-blocks a
  transition (`planning.py`, `blueprints/routing.py`), surface `GateResult.missing` as
  HUD evidence on the goal-owner chat and let the conductor adjudicate.
  **`proof_gates.py` computation is untouched** — only its consumers change.
- [ ] **Phase 3 — skills reflect it.** `harness-mission-lead`, `harness-qa-verdict`,
  `harness-dev-delivery`, `launcher-analyze-proof`: proof = evidence the goal owner
  adjudicates; blocked = an escalation to handle, not a dead-end. Re-install
  (`hermes harness install-stage46-skills`).

Gate:
```bash
python -m pytest tests/agent_runtime/test_proof_gates.py tests/agent_runtime/test_planning.py tests/agent_runtime/test_state_machine.py -q
```
(`test_proof_gates.py` must stay green — the computation is unchanged.)

### R8 — Retire the standalone persona pipeline (Stage 77)  ⛓ (Launcher; depends on the graph UI)

- [ ] Remove the separate `Goal → Neko → Dev → QA → Proof` strip; the agents are the
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
rg "hud_shape_index_for_role|role_checklist_hud|_STAGE46_REQUIRED_SKILLS|stage46" agent_runtime hermes_cli tests
rg "_synthesize_plan_from_handoff|mirror_legacy_stages_from_plan|configured_personas" agent_runtime hermes_cli tests
rg "_ensure_launcher_handoff_stage|_ensure_no_edit_proof_handoff_stage|_repair_bounded_visual_proof" agent_runtime hermes_cli tests
```

All greps return zero hits **outside** tests/migrations that assert legacy serialized
data upgrades, and the pytest run is green → the retirement ledger is complete.
