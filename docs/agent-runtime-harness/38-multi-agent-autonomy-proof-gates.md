# Stage 38 — Multi-Agent Autonomy Proof Gates

> **For Hermes:** Use `agent-runtime-harness`, `staged-deep-audit-delivery`, `test-driven-development`, and `requesting-code-review` when implementing or auditing this stage.

## Goal

Make the Agent Runtime Harness prove the basic multi-agent autonomy loop without Alice babysitting: Neko scopes/steers, specialist Devs implement or gather proof, Neko coordinates QA release, QA verifies independently, and the task ends `done` or cleanly `blocked` with proof/intervention evidence.

## Acceptance Contract

1. Harness automatically routes budget-pressure or bad Dev proof handoffs to Neko before hard cap when possible.
2. Neko gathers/organizes proof and steers Dev/Backend Dev without Alice manually babysitting routine incidents.
3. Proof IDs are attached to the task, not only present in chat/tool output.
4. Neko releases Launcher/QA only after backend contract proof is complete.
5. Launcher Dev can run after the backend/Neko join gate and attach its own proof.
6. QA independently verifies backend + frontend proof, including visual proof when required.
7. The task reaches `done` or a clean `blocked` intervention with proof/incident evidence.

## Current Deep-Audit Findings

### Existing primitives to reuse

- `agent_runtime/profile_runner.py`
  - Emits `budget_pressure` warning at 80% of run token cap.
  - Emits `skill_loading_fanout` warning for repeated `skill_view` calls.
- `agent_runtime/dev_discipline.py`
  - Blocks near-budget non-proof Dev decisions and requires `request_test_run`, `request_qa_review` with `proof_ids`, or `block`.
- `agent_runtime/state_machine.py`
  - Routes `CREATED` and legacy `PM_TRIAGE` to Neko.
  - Routes `DEV_READY_FOR_QA` to Neko until `neko_qa_coordination_released` is present.
  - Routes QA-approved proof-backed tasks to deterministic `COMPLETE_TASK`.
- `agent_runtime/planning.py`
  - `REQUEST_TEST_RUN` materializes stages and lets `ticker` collect command proof.
  - `REQUEST_QA_REVIEW` merges existing proof IDs and moves to `DEV_READY_FOR_QA` when all dev stages are complete.
  - `REPORT_QA_VERDICT` records QA verdict proof and marks stages passed or blocked.
- `agent_runtime/ticker.py`
  - Collects command proof records through `CommandProofRunner` and attaches proof IDs to the task.
  - Routes `run_budget_exceeded` incidents to Neko approval/recovery.

### Gaps found

- No single regression proves the whole autonomous sequence: Backend Dev proof → Neko release → Launcher Dev proof → Neko release → QA verdict proof → deterministic done.
- Manual Alice proof from an external terminal is not automatically attached to the live task; this is acceptable only as manual recovery, not autonomous success.
- `run_budget_exceeded` hard-stop recovery exists, but budget-pressure pre-hard-cap recovery must be proven through the Dev progress gate and Neko routing tests.
- The live Posts admin-controls task is still `dev_implementing` with open `inc_aa276e41`; it is not completed by this stage until proof is attached and the remaining Launcher/QA stages run.

## Stage Plan

### Stage 38.1 — Budget-pressure and skill-loading guards

**Files:**
- `agent_runtime/profile_runner.py`
- `agent_runtime/dev_discipline.py`
- `agent_runtime/persona_runtime.py`
- `tests/agent_runtime/test_profile_runner.py`
- `tests/agent_runtime/test_dev_discipline.py`
- `tests/agent_runtime/test_persona_skill_guidance.py`

**Required proof:**
- `budget_pressure` emitted before hard token cap.
- Repeated `skill_view` emits `skill_loading_fanout`.
- Persona skill guidance allows more than two skills only with explicit current-stage purpose.
- Dev near budget cannot produce non-proof handoff.

### Stage 38.2 — End-to-end multi-agent autonomy regression

**Files:**
- `tests/agent_runtime/test_ticker.py`
- Existing runtime code if regression fails.

**Scenario:**
1. Backend Dev requests deterministic backend command proof.
2. Harness collects backend proof and attaches proof ID to task.
3. Backend Dev requests QA review with attached proof.
4. Harness routes `DEV_READY_FOR_QA` to Neko, not QA.
5. Neko records `neko_qa_coordination_released` after verifying backend proof and adding a Launcher stage.
6. Launcher Dev requests deterministic frontend/visual command proof.
7. Harness collects frontend proof and attaches proof ID to task.
8. Launcher Dev requests QA review with both proof IDs.
9. Harness routes through Neko release again if needed, then QA.
10. QA reports implementation verdict with proof IDs.
11. Harness records QA verdict proof and deterministic close reaches `done`.

**Required proof:**
- Targeted test passes and asserts persona order: backend_dev → backend_dev → neko_supervisor → dev → dev → neko_supervisor/qa as appropriate → qa → complete.
- Task has backend proof, frontend proof, and QA verdict proof IDs.
- No open incidents.

### Stage 38.3 — Live task recovery contract

**Files:**
- Runtime store/proof tooling if needed.
- Current live task `task_d9ccc54c` only after deterministic tests pass.

**Required proof:**
- Either attach manually collected backend proof to `task_d9ccc54c` and route forward, or leave it cleanly blocked with exact intervention.
- Do not approve another expensive Backend Dev continuation without proof-oriented Neko steering.

## Verification Matrix

- `python -m pytest -o addopts='' tests/agent_runtime/test_profile_runner.py tests/agent_runtime/test_dev_discipline.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_persona_skill_guidance.py -q`
- `python -m compileall agent_runtime/profile_runner.py agent_runtime/dev_discipline.py agent_runtime/persona_runtime.py agent_runtime/ticker.py agent_runtime/planning.py agent_runtime/state_machine.py tests/agent_runtime/test_profile_runner.py tests/agent_runtime/test_dev_discipline.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_persona_skill_guidance.py`
- `git diff --check`
- Live check after implementation: `hermes harness task show task_d9ccc54c --json` and `hermes harness status --json`.

## Final Status Fields

When reporting this stage, distinguish:

- **Harness autonomy regression proof:** targeted tests pass.
- **Live Posts admin-controls goal status:** task state/proof/incidents from real Harness store.
- **Remaining intervention:** any missing frontend implementation, visual proof, QA verdict, or open incident.
