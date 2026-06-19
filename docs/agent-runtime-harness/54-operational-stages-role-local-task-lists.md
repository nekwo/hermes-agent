# Stage 54 - Operational Stages With Role-Local Task Lists

Date: 2026-06-08
Owner: Codex independent Harness implementation
Status: implemented
Depends on: Stage 51 typed mission plan simplification, Stage 52 continuous role envelopes, Stage 53 in-process goal runner

## Purpose

Make Mission Control flow like a competent normal harness:

```text
Neko creates or repairs operational mission stages.
Harness routes one active role to one operational stage.
The active role receives a small local task list.
The role checks off local work as it progresses.
Harness validates proof and QA before global completion.
```

Core rule:

```text
Operational stages are global Harness routing state.
Role task lists are local worker state.
Dev and QA do not create global stages unless Neko/Harness promotes a real cross-role dependency.
```

This keeps the system simple to the agents without deleting the rich internal state needed for audit, recovery, archives, and Mission Control observability.

## Deep Audit

Existing Stage 52 already implemented substantial machinery:

- persisted `RoleChecklist` and `RoleChecklistItem` records;
- role envelopes with checklist IDs;
- HUD injection through `role_task_list`;
- validation for invented checklist item IDs;
- archive preservation for role envelopes/checklists/proof batches;
- Mission Control snapshot surfacing.

The remaining gap was not a missing checklist store. The gap was authority and projection:

- `checklist_for_task_stage(...)` selected templates from `current_plan_stage(task)` even when a specific `mission_stage_id` was requested. This could project the wrong role checklist when QA, Dev, or a future role stream asked for a non-current stage.
- Checklist updates accepted unknown keys inside update objects, so an agent could invent fields like `create_global_stage` without a clear invalid-packet repair.
- The HUD did not explicitly tell workers that their checklist is local state and that global stage promotion belongs to Neko/Harness.

## Product Contract

### Neko

Neko owns global operational stage creation and repair.

Allowed global responsibilities:

- preserve parent mission intent;
- choose owner/repo/proof policy;
- create or repair typed mission stages;
- release the next unblocked owner;
- promote a discovered cross-role dependency into a real mission stage;
- route recovery when a worker cannot proceed.

Neko should not micromanage Dev or QA local subtasks once the next owner is clear.

### Dev and Backend Dev

Dev roles own role-local implementation task lists inside the assigned mission stage.

Typical local checklist:

- inspect target files/logs narrowly;
- patch relevant code;
- run focused self-test;
- attach or cite self-test evidence;
- request or satisfy the final gate;
- hand off to QA.

Dev does not create global mission stages from checklist updates. If Dev discovers a backend contract dependency, visual uncertainty, scope ambiguity, or environment blocker, it uses `request_missing_input` or `report_blocker`. Harness/Neko decides whether that becomes a global stage.

### QA

QA owns role-local verification task lists inside the QA stage.

Typical local checklist:

- verify blocking stages are ready;
- verify required command proof IDs;
- verify required visual proof IDs when required;
- review final runtime/product behavior;
- issue an evidence-backed verdict.

QA does not create global stages. QA rejects or requests missing proof with the smallest actionable fix; Harness/Neko routes repair.

## Implementation

Implemented in:

- `agent_runtime/role_checklists.py`
- `tests/agent_runtime/test_stage52_role_envelopes.py`

Runtime changes:

- Checklist template selection now uses the requested `mission_stage_id` when present, falling back to the current plan stage only when no requested stage exists.
- `RoleChecklist` now exposes:
  - `operational_scope`;
  - `can_promote_global_stage`;
  - `promotion_rule`.
- Neko checklists are marked as the global mission-stage promotion surface.
- Dev, Backend Dev, and QA checklists are marked as role-local subtasks.
- Checklist update objects now reject unsupported keys and return a repair payload with `allowed_update_keys`.
- HUD `role_task_list` now carries the local/global authority rule so agents do not have to infer it.

## Tests

Focused coverage:

- Dev HUD exposes local-only task-list policy and valid choices.
- Checklist validation rejects invented global-stage promotion fields.
- QA checklist templates are generated from the requested typed stage, not whatever stage is currently active.
- Neko checklist is the only normal checklist surface marked as able to promote global mission stages.
- Existing Stage 52 archive, snapshot, envelope, proof-batch, and checklist-update tests still pass.

Command:

```powershell
& 'X:\Eternia\.hermes\venvs\hermes-agent\Scripts\python.exe' -m pytest tests/agent_runtime/test_stage52_role_envelopes.py -q
```

Result:

```text
14 passed
```

## Remaining Follow-Up

This stage hardens the task-list authority boundary. The next efficiency follow-up is to teach the in-process goal runner to show the current role checklist and checklist progress in its compact monitor stream, so an operator can see the same “simple list getting checked off” flow without opening the full snapshot.
