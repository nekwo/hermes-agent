# Stage 15 — Persona Agency and Proof Handoff Hardening

## Goal

Make Mission Control personas behave less like stateless JSON robots and more like accountable teammates while preserving deterministic Harness safety. A persona must understand its current stage, what “done” means, who receives the next handoff, and which machine-visible proof IDs make that handoff valid.

## Product stance

Humanlike agency is not free-form autonomy. It is explicit responsibility inside a bounded role:

```text
PM scopes and hands off to Dev.
Dev implements, verifies, attaches proof IDs, and hands off to QA.
QA independently verifies proof, attaches a QA verdict proof, and hands off to PM.
Neko reconciles inconsistent handoffs without inventing proof.
```

The Harness remains the source of truth for state transitions, proof records, and retries. Agents may learn through skills only after verified runtime lessons; they do not write memory/skills from inside ordinary ticks.

## Deep-audit evidence

- `agent_runtime/persona_runtime.py` builds the system prompt and already includes a shared overlay plus role prompt. It can be extended with explicit agency/handoff rules without replacing `AIAgent`.
- `agent_runtime/context_builder.py` renders task/stage/proof context. Stage 14/early live testing proved missing `affected_repos` caused Dev to block even though the task record had the right repo.
- `agent_runtime/planning.py` applies structured decisions. It currently handles plan-scope QA review and Dev handoff, but implementation-scope QA verdicts/proof attachment are under-wired.
- `agent_runtime/status.py::_next_action()` maintains a stale partial mapping instead of using `MissionStateMachine.next_action()`, causing `dev_ready_for_qa` to appear as `noop` while the real state machine can route QA.
- `agent_runtime/qa_verdict.py` already has a helper to create QA verdict proof records, but `apply_planning_decision()` does not use it for implementation review.
- `agent_runtime/proof_gates.py` already requires safe diff/change proof, passed test proof, and approved QA verdict proof.
- Live goal test showed Dev could claim a proof package was ready while task `proof_ids` remained empty; QA correctly refused approval.

## Implementation stages

### 15.1 — Status read-model alignment

- Replace the private partial mapping in `agent_runtime/status.py` with `MissionStateMachine.next_action()`.
- Preserve the open-incident override.
- Test `dev_ready_for_qa` returns `run_qa` in status.

### 15.2 — Stage-aware tick context

- Ensure rendered context includes affected repositories, current proof IDs, stage owner/handoff expectations, and role-specific “done” criteria.
- Test that affected repos and proof IDs are visible to personas.

### 15.3 — Dev proof handoff contract

- Make `request_qa_review` require `stage_id`, `proof_ids`, and `handoff` metadata when used as a Dev implementation handoff.
- When a proof store is available, merge only existing referenced proof IDs into `task.proof_ids`; reject missing proof IDs instead of silently advancing.
- If no proof store is supplied in old unit tests, preserve behavior for pure planning/unit use.

### 15.4 — QA implementation verdict proof

- Extend `report_qa_verdict` / `approve` to support `review_scope: "implementation"`.
- Require non-empty `proof_ids` for implementation approval.
- Attach a `qa_verdict` proof via `record_qa_verdict()` and append it to `task.proof_ids`.
- Approved implementation review moves to `qa_approved`; non-approved implementation review moves to `qa_needs_fixes` or `blocked` with exact findings.

### 15.5 — Persona self-awareness overlays

- Update shared/role prompts so each persona states ownership and handoff behavior:
  - PM: next-owner scoped mission.
  - Dev: done means proof IDs attached and QA handoff complete.
  - QA: done means independent verdict proof attached or exact fix request.
  - Neko: reconcile handoff mismatch, do not invent proof.
- Keep JSON-only requirement and no Kanban/no messaging/no memory writes.

### 15.6 — Verification and gap review

- Run targeted runtime tests for status, context, planning, ticker, proof gates, and persona prompt construction.
- Run compile/diff hygiene.
- Use temp-root smoke where possible.

## Acceptance criteria

- Status and ticker agree on next action for every active stage tested.
- Dev cannot advance implementation to QA with invented/missing proof IDs when a proof store is available.
- QA implementation approval creates an approved QA verdict proof and moves the task forward.
- Tick context gives personas the repo/proof/stage ownership information needed to make self-conscious handoffs.
- Prompt overlays explicitly instruct agents to own their stage, verify completion, and hand off with proof.
- Remaining gaps are documented, not normalized.

## Implementation completion log

### 15.1 — Status read-model alignment

Completed. `agent_runtime/status.py` now uses `MissionStateMachine.next_action()` for next-action reporting while preserving the open-incident override. Regression: `test_status_next_action_uses_mission_state_machine_for_dev_ready_for_qa`.

### 15.2 — Stage-aware tick context

Completed. `build_context()` now defaults to persisted `task.proof_ids`, so personas see existing machine-verifiable proof IDs even when the caller does not pass a separate proof list. Affected repositories remain rendered in the tick context. Regression: `test_build_context_defaults_to_task_proof_ids_for_handoff_awareness`.

### 15.3 — Dev proof handoff contract

Completed. `request_qa_review` now requires `stage_id`, non-empty `proof_ids`, and a `handoff` object with `to: qa` and `stage_complete: true`. When `ProofStore` is supplied, referenced proof IDs are verified against the current task before they are merged into `task.proof_ids`; unknown/invented IDs are rejected. `propose_patch` also refuses to advance implementation to QA under a supplied proof store unless it references existing proof IDs. Regressions cover accepted, missing, and prose-only proof handoffs.

### 15.4 — QA implementation verdict proof

Completed. `report_qa_verdict` / `approve` now support `review_scope: implementation`. QA implementation review requires proof IDs, records a safe QA verdict proof through `record_qa_verdict()`, attaches that proof ID to the task, and routes approved/needs-fixes/blocked verdicts to `qa_approved` / `qa_needs_fixes` / `blocked`.

### 15.5 — Persona self-awareness overlays

Completed. The shared system prompt now contains Stage Ownership and Handoff rules. PM/Dev/QA/Neko prompts now describe each persona's accountable stage ownership, completion standard, next-owner handoff, and proof responsibilities while preserving JSON-only/no-Kanban/no-side-effect constraints.

### 15.6 — Verification

Completed targeted verification:

```text
PYTHONPATH=. venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime -q
# 166 passed, 1 warning

PYTHONPATH=. venv/Scripts/python.exe -m pytest --timeout-method=thread tests/hermes_cli/test_harness_cli.py -q
# 7 passed

PYTHONPATH=. venv/Scripts/python.exe -m compileall agent_runtime hermes_cli -q
# PASS

PYTHONPATH=. venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model
# ok=true, final_state=done

git diff --check
# PASS
```

Remaining non-blocking gaps:

- This stage improves deterministic handoff/proof semantics, but it does not yet run live model smoke again against a real product repo after the new prompt/contracts. That should be the next operator validation slice.
- The Harness still needs deterministic command/test proof collection if Dev requests tests rather than providing existing proof IDs. This stage prevents invented proof and makes QA verdict proof machine-visible; it does not build an autonomous test-runner pipeline.
