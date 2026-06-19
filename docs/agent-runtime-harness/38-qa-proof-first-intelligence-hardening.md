# Stage 38 — QA Proof-First Intelligence Hardening

## Goal

Make the Harness QA persona behave with Alice/Neko-level proof discipline: review attached proof quickly, avoid broad repo rediscovery, produce a bounded verdict, and fail safe before burning real-token budget.

This stage was opened after a real-token Mission Control smoke on `task_6c96e020` where Neko scoped correctly and Dev attached valid command proof, but QA repeatedly searched/read files, hit `run_budget_exceeded`, resumed in the same session, then hit budget again without producing `report_qa_verdict`.

## Product stance

QA is a proof gatekeeper, not a second implementation investigator. QA must start from the task's `proof_ids`, inspect the proof artifacts and the exact claimed files/commands, then return one structured decision:

- `report_qa_verdict` with `verdict=approved` when proof satisfies the stage/task.
- `report_qa_verdict` with `verdict=needs_fixes` when Dev must patch or rerun proof.
- `report_qa_verdict` with `verdict=blocked` when required evidence is missing or unsafe.
- `request_test_run` only for a single clearly missing deterministic proof command.
- `request_screenshot` / `request_video` only when visual proof is required or code/test proof cannot validate the user-visible claim.

## Rejected alternatives

- Let QA continue broad search with a large token budget: rejected because it repeats the failure mode and delays release decisions.
- Ask Neko to approve QA budget continuations repeatedly: rejected because it normalizes a QA loop instead of fixing QA.
- Remove QA's ability to inspect files entirely: rejected because QA sometimes needs targeted verification of proof claims.

## Codebase audit evidence

- `agent_runtime/prompts/qa.md` says QA should review `proof_ids`, but does not impose a mandatory proof-first order, concrete tool caps, or verdict deadline.
- `agent_runtime/persona_runtime.py` passes `stop_on_repeated_read_search=role_from_persona(persona) == "dev"`, so QA receives warning-only repeated-tool events and can continue until budget gates.
- `agent_runtime/profile_runner.py` can already raise `RunBudgetExceeded` on repeated read/search loops when `stop_on_repeated_read_search=True`.
- `tests/agent_runtime/test_profile_runner.py` already covers the repeated read/search hard-stop adapter behavior.
- `agent_runtime/state_machine.py` correctly routes `DEV_READY_FOR_QA` through Neko coordination and then QA; the smoke showed `neko_qa_coordination_released` worked.
- Real proof artifact from `task_6c96e020` showed tests/analyze passed and the product answer was available from proof, so QA did not need broad rediscovery.

## Stage 38.1 — QA persona proof-first prompt

### Affected files

- `agent_runtime/prompts/qa.md`
- `tests/agent_runtime/test_persona_runtime.py` or equivalent prompt/regression tests if present/appropriate

### Implementation actions

- Add a mandatory QA review order:
  1. Read task state/acceptance criteria and current stage.
  2. Inspect attached `proof_ids` and proof artifact/logs first.
  3. Inspect only exact files/functions referenced by proof or task acceptance criteria.
  4. Stop after a small bounded number of read/search tool calls and return a verdict.
- Add explicit loop guard language: repeated `search_files`, `read_file`, `session_search`, or `browser_snapshot` means stop and return `report_qa_verdict` with findings.
- Add explicit no-broad-rediscovery rule.
- Add verdict output rules for code-only vs visual tasks.

### Tests/proof

- Targeted tests verifying the QA prompt contains proof-first and bounded-verdict guardrails, or snapshot-style system prompt tests if available.

## Stage 38.2 — Runtime hard-stop for QA repeated read/search loops

### Affected files

- `agent_runtime/persona_runtime.py`
- `tests/agent_runtime/test_profile_runner.py` and/or `tests/agent_runtime/test_persona_runtime.py`

### Implementation actions

- Enable `stop_on_repeated_read_search` for QA as well as Dev.
- Make repeated loop warning wording role-neutral or explicitly mention QA verdict/blocker, not only Dev/Neko slicing.
- Preserve Neko behavior; do not hard-stop Neko's supervisory skill/file reads unless separately staged.

### Tests/proof

- Unit test proving QA requests enable repeated read/search hard-stop.
- Existing adapter test remains passing.
- New/updated test confirms warning says produce proof/verdict/blocker rather than only patch/test.

## Stage 38.3 — Cleanup of live smoke tasks/incidents

### Affected runtime state

- Harness store under active runtime root.
- Live smoke tasks:
  - `task_6c96e020` — QA hardening smoke blocked by budget incident.
  - `task_30af6060` — older BMP retry blocked by model invalid output.

### Implementation actions

- Preserve evidence before cleanup through normal Harness records/proofs.
- Close or cancel only test/smoke tasks and their incidents, with explicit reason.
- Verify `hermes harness task list --json` no longer shows those smoke tasks as active.

### Tests/proof

- Targeted cleanup scope confirmed by inspecting `task_6c96e020` and `task_30af6060` only.
- Archived evidence batch: `X:\\Eternia\\.hermes\\agent-runtime\\deleted_archive\\20260604T072645Z_qa_hardening_smoke_cleanup`.
- Verification command showed `active_task_exists {'task_6c96e020': False, 'task_30af6060': False}` after cleanup.
- Related archived incidents: `inc_28797d9c`, `inc_381ccee5`, `inc_727f5bda`.

## Stage 38.4 — Real-token test 1, audit, gap closure, real-token test 2

### Test goal shape

A bounded QA-focused smoke where Dev attaches deterministic command proof and QA should approve from proof without broad rediscovery.

### Acceptance criteria

- Neko scopes the task.
- Dev produces proof or clear blocker.
- QA produces `report_qa_verdict` before budget approval gate.
- No repeated read/search loop warning for QA.
- If a gap is found, record severity/evidence, patch or explicitly defer, then rerun.

### Real-token test 1 result

- Task: `task_5a2b13b4` — `QA hardening live-token smoke 1`.
- Final state: `done` after 4 ticks.
- Dev action: `request_test_run` with summary `Requesting the exact deterministic targeted pytest proof for the tiny QA hardening live-token smoke scope.`
- Command proof: `test_task_5a2b13b4_stage_qa_hardening_live_smoke_run_d4bb690d36b9_0_2596f351`.
- Command output: `5 passed in 0.62s`.
- Neko action: `propose_acceptance`, releasing QA coordination with passing proof.
- QA action: `report_qa_verdict`, summary `Implementation QA approved from supplied deterministic command proof.`
- QA verdict proof: `proof_qa_16785cbe`, verdict `approved`.
- Event audit: QA emitted reasoning/proof/verdict events only; no `repeated_read_search_loop` warning and no budget approval incident.

### Gap audit after real-token test 1

- Severity: medium environment gap.
- Evidence: first token smoke invocation used global `C:\\Python312\\python.exe` and failed to initialize OpenAI because `openai` was missing; second used the shared Hermes venv and failed because `jiter` was a broken namespace install without `from_json`.
- Closure: installed `openai 2.41.0` into global Python 3.12, verified global `jiter.from_json=True`, closed the two environment incidents, and reran token test 1 successfully from global Python.
- Remaining intervention: shared active Hermes venv still has locked/broken `jiter` and should be repaired after the owning process releases the `.pyd`; not launch-blocking for this staged hardening because the verified live-token runner path is global Python 3.12.

### Real-token test 2 result

- Task: `task_db321a8b` — `QA hardening live-token smoke 2`.
- Final state: `done` after 4 ticks.
- Dev action: `request_test_run`, summary `Request the single targeted pytest proof for QA proof-first hardening smoke 2.`
- Command proof: `test_task_db321a8b_stage_qa_hardening_live_smoke_2_run_10f3edccaeb7_0_c66d1690`.
- Command output: `4 passed in 0.42s` for `tests/agent_runtime/test_qa_intelligence_hardening.py`.
- Neko action: `propose_acceptance`, releasing QA for proof-backed review.
- QA action: `report_qa_verdict`, summary `QA approved the implementation handoff from the supplied passing targeted pytest proof.`
- QA verdict proof: `proof_qa_a1a78ef5`, verdict `approved`.
- Event audit: `flag_count 0` for `repeated_read_search_loop` and budget-approval warning markers; QA produced a bounded verdict.

## Finalization delivery loop

### Four remaining delivery items

1. Broader Harness regression gate: run a wider `tests/agent_runtime` subset after the targeted QA hardening proof.
2. Commit QA hardening changes: commit only after tests and final diff review pass.
3. Runtime environment hygiene: repair or explicitly defer the shared Hermes venv `jiter` issue with evidence; do not block QA hardening if the verified runner path remains healthy.
4. Whole Mission Control gap audit: distinguish this QA hardening closure from broader Mission Control finalization and record the next staged system gap instead of overclaiming.

### Finalization audit findings

- Broader gate initially found 9 failures in `tests/agent_runtime`. Audit showed a mix of one real edge-case bug and stale expectations from the current Mission Control architecture.
- Real bug fixed: explicit `pm` persona overrides without `role` were created as `dev`; `configured_personas()` now defaults `persona_id == "pm"` to role `pm`, preserving PM tool restrictions for legacy/explicit configuration.
- Stale test contracts updated: default persona collection is Neko/Dev/BackendDev/QA, not PM-default; Dev-ready-for-QA next action is Neko Mission Lead coordination before QA; recommended skills are listed as compact skill names rather than pre-expanded `skill_view(...)` calls.
- Focused regression after fixes: 9 previously failing tests passed.
- Broader Harness regression after fixes: `python -m pytest -o addopts="-m 'not integration'" tests/agent_runtime -q` passed with `347 passed in 20.12s`.
- Shared Hermes venv evidence after attempted repair: `jiter.from_json=True` and `openai 2.24.0` import successfully from `X:\\Eternia\\.hermes\\venvs\\hermes-agent\\Scripts\\python.exe`. Pip still reported a transient file-lock during overwrite, but runtime import health is restored.

## AAA gap checklist

- [x] QA prompt is proof-first and verdict-bounded.
- [x] QA runtime hard-stops repeated read/search loops before runaway budget.
- [x] Tests cover prompt/runtime behavior (`5 passed in 0.77s` targeted pytest).
- [x] Live smoke tasks/incidents are cleaned safely after evidence capture.
- [x] Real-token test 1 passes or produces actionable gap.
- [x] Gap audit is recorded and closed/deferred.
- [x] Real-token test 2 verifies the closed loop.
