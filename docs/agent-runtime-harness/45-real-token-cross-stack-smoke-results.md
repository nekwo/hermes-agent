# Stage 45 Real-Token Cross-Stack Smoke Results

Date: 2026-06-05
Owner: Codex independent Harness investigation

## Summary

Mission Control/Harness did not complete a full Backend Dev -> Launcher Dev -> QA live goal yet. The live runs proved several root-cause orchestration gaps and the Harness now handles those gaps more deterministically, but the final smoke stopped before Launcher Dev because Backend Dev hit proof/environment and persona-progress failures.

Final runtime cleanup is healthy:

- `open_tasks=0`
- `blocked_tasks=0`
- `open_incidents=0`
- `active_runs=0`
- daemon offline
- observability healthy

## Code Fixes Implemented

1. Backend-first cross-stack scope normalization
   - Neko can describe a sequential backend-first mission using live wording such as `cross_stack_sequential_handoff` or "Backend Dev before Launcher Dev before QA."
   - Harness now normalizes that accepted scope to an executable `EterniaBackend` first slice instead of repeatedly routing back to Neko.

2. Cross-stack close guard
   - Backend-only proof can no longer be mistaken for full cross-stack completion.
   - Terminal close and QA routing are blocked until the Launcher side is represented by a real Launcher stage or release.

3. Launcher-stage classifier hardening
   - Backend stages may mention future Launcher gates in acceptance criteria without being classified as Launcher-complete.
   - Stage identity now uses stage id/title/objective/scope/proof requirement rather than downstream acceptance wording.

4. Dev stage-plan sanitization
   - Dev-proposed plans can no longer enqueue Neko scope-freeze or QA verification gates as Dev implementation stages.
   - Harness keeps executable specialist stages and skips orchestration-only stages from the Dev stage queue.

5. Failed proof retry recovery
   - A blocked task with failed current-stage command proofs now routes back to Dev for retry once the external condition can be corrected.
   - This enabled retry after Docker Desktop was started.

## Live Smoke Evidence

### Failed/Archived Task `task_4bdfeb1e`

Intent: initial real-token cross-stack smoke.

Result: incorrect terminal success. Neko collapsed the goal into a backend-only slice; Backend Dev and QA ran, Launcher Dev did not.

Important evidence:

- archived batch: `20260604T214616354004Z_archive_ready`
- proofs: backend proof plus QA verdict
- problem: task reached `done` without Launcher proof
- root cause fixed by cross-stack close guard and Launcher-stage classifier

### Failed/Archived Task `task_85bbc4f2`

Intent: post-guard cross-stack smoke.

Result: Backend Dev ran and attached backend proof, but Neko released QA instead of Launcher because backend acceptance criteria mentioned Launcher and were misclassified as Launcher completion.

Important evidence:

- archived batch: `20260604T215920825980Z_archive_ready`
- proofs preserved:
  - `proof_qa_060ffb7a`
  - `test_task_85bbc4f2_backend_contract_smoke_run_4c86fef2793e_0_ae54924e`
  - `test_task_85bbc4f2_backend_contract_smoke_run_4c86fef2793e_1_33a2827c`
  - `test_task_85bbc4f2_backend_contract_smoke_run_4c86fef2793e_2_514677fe`
- root cause fixed by tightening `_stage_mentions_launcher`

### Failed/Archived Task `task_502472e5`

Intent: corrected cross-stack backend-launcher smoke.

Result: Neko looped on broad path-withheld backend+launcher scope, then Backend Dev created Neko/QA orchestration stages as Dev stages and blocked.

Important evidence:

- archived batch: `20260604T221414964295Z_archive_ready`
- run count preserved: 18
- proof count: 0
- root causes fixed by backend-first normalization and Dev stage-plan sanitization

### Failed/Archived Task `task_cfaf3932`

Intent: final clean cross-stack backend-launcher proof smoke.

Result: Neko normalized, Backend Dev ran, and command proofs were preserved. The mission still did not reach Launcher Dev.

Important evidence:

- archived batch: `20260604T223126992928Z_archive_ready`
- proof IDs:
  - `test_task_cfaf3932_stage_46_backend_observational_proof_run_286f9c80934e_0_d8947113` passed
  - `test_task_cfaf3932_stage_46_backend_observational_proof_run_286f9c80934e_1_3d6956c0` failed
  - `test_task_cfaf3932_stage_46_backend_observational_proof_run_286f9c80934e_2_949351e1` failed
  - `test_task_cfaf3932_stage_46_backend_observational_proof_run_286f9c80934e_3_9da1278a` passed
- failing proof cause: Docker Desktop/Linux engine was offline:
  - `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`
- Docker was started successfully after the blocker:
  - `docker info` returned server `29.4.2`
- retry route worked after the failed-proof recovery fix:
  - next action changed to `run_dev`
- final blocker:
  - `inc_859bf8e0`
  - kind: `model_invalid_output`
  - summary: `Dev early progress gate failed: high-call run produced no patch/test/proof progress and did not split or block`
- incident was closed after task cancellation/archive to leave runtime clean.

## Commands Run

Key commands:

- `python -m hermes_cli.main harness status --json`
- `python -m hermes_cli.main harness daemon run-once --json`
- `python -m hermes_cli.main harness task create ... --json`
- `python -m hermes_cli.main harness task cancel ... --json`
- `python -m hermes_cli.main harness task archive-ready --json`
- `python -m hermes_cli.main harness incident close inc_859bf8e0 ... --json`
- `docker info`
- `python -m pytest -o addopts='' -q tests/agent_runtime`

## Test Results

Focused regression suite:

- `39 passed`

Broad Harness agent-runtime suite:

- `376 passed in 21.10s`

## Remaining Gaps

Severity: High

1. Backend Dev proof-command efficiency is still not enterprise-grade.
   - Backend Dev chose heavyweight Docker/Postgres checks for a no-edit observational smoke.
   - After Docker was fixed, it spent another high-call run without a proof-oriented decision and tripped the early progress gate.

2. Persona prompt/tool-budget guidance needs tightening.
   - Backend Dev should use the existing proof IDs and either request a bounded retry command or block once with exact evidence.
   - It should not re-enter read/search loops after a proof-backed environment blocker.

3. Full cross-stack proof remains unproven.
   - No successful live run reached Launcher Dev.
   - No QA verdict exists over both backend and Launcher proof sets.

Severity: Medium

4. Environment preflight should run before expensive live goals.
   - Docker was installed but offline.
   - The Harness surfaced this only after spending real tokens and running backend proof commands.

5. Open incidents from archived tasks require cleanup policy.
   - Manual incident close was needed after archiving `task_cfaf3932`.
   - Archive-ready should either close/archive task-scoped incidents or report them as retained open blockers explicitly.

## Next Implementation Stage

Stage 46 should focus on proof-command and persona self-healing:

1. Add a deterministic preflight stage for required external dependencies such as Docker Desktop, Flutter, backend venv, and repo cleanliness before live specialist runs.
2. Teach Dev personas to reuse attached failed proof IDs and choose one bounded retry command after an environment fix.
3. Add a Harness rule that prevents repeated same-stage Dev retries after a proof-backed environment blocker unless the environment signal changed.
4. Add observability counters for repeated Neko scope updates and repeated Dev read/search after failed proof.
5. Run a final real-token smoke and require actual sequence:
   - Neko
   - Backend Dev
   - Neko join release
   - Launcher Dev
   - QA

## Current Verdict

Not fully working end-to-end yet. The Harness is substantially better guarded against false success and silent loops, and it now preserves actionable evidence when the goal cannot proceed. The remaining blocker is proof/persona efficiency and environment preflight, not the initial Mission Control archive/bridge code path.
