# Stage 41 — Archive-Ready Goal Support Live Run Gaps

## Context

Tony requested support for the currently unsupported operator action:

```text
archive ready goal is not supported by the live harness cli yet
```

Alice initially started auditing/implementing directly, but Tony clarified:

```text
make a goal don't do it yourself
```

So Alice created a live Harness goal for the agents to implement the feature, with Launcher Dev as implementer.

## Live Task

- Task ID: `task_archive_ready_goal_cli_dev_20260604`
- Intended implementer: `dev` / Launcher Dev / `gpt-launcher`
- Neko role: coordination only if required by Harness state machine
- QA role: proof review and verdict

## Confirmed Routing

The live run is being performed by Launcher Dev:

- Active persona: `dev`
- Hermes profile: `gpt-launcher`
- Model: `gpt-5.5`
- Provider: `openai-codex`
- Run ID: `run_a480781f36b5`
- Stage ID: `stage_cli_archive_ready_support`

This confirms it is not Neko doing the implementation.

## Pre-Audit Facts

Alice’s pre-audit found:

- `agent_runtime.paths.deleted_archive_dir()` already exists.
- `agent_runtime.snapshot` already reads archived task summaries from `deleted_archive`.
- `hermes_cli.harness` currently exposes task create/list/show/cancel but not a task archive/archive-ready command.
- `tests/hermes_cli/test_harness_cli.py` currently has parser and basic e2e coverage but no archive-ready coverage.

## Feature to Implement

Add live Harness CLI support for archiving ready/done goals.

Likely CLI surface:

```bash
python -m hermes_cli.main harness task archive-ready --json
```

Optionally also:

```bash
python -m hermes_cli.main harness task archive <task_id> --json
```

## Required Semantics

1. Archive terminal/ready-to-clear goals without deleting evidence.
2. Preserve task JSON under `deleted_archive`.
3. Preserve associated proof records and run records, or create a manifest that points to preserved evidence.
4. Refuse or skip active/open/non-terminal tasks safely.
5. Return JSON output with counts and archive path/batch identifier.
6. Archived tasks should no longer show as open tasks.
7. Archived tasks should remain visible through snapshot archived task summaries or equivalent operator evidence.
8. Tests must cover parser support, archive semantics, active-task safety, and snapshot visibility.

## Live Efficiency Concern to Watch

During the first live Dev run, observability showed repeated `search_files` calls and Harness emitted critical `runaway_warning` progress events:

- `phase`: `runaway_warning`
- `step`: `repeated_read_search_loop`
- `tool_name`: `search_files`
- `next_expected`: `bounded_verdict_proof_handoff_or_exact_blocker`

At the time this was observed:

- Active run count: `1`
- Running run: `run_a480781f36b5`
- Persona: `dev`
- No open incident yet
- Run was not stalled yet

## What We Need To Figure Out / Fix

### 1. CLI archive-ready support gap

Root question:

- What exact store/archive helper should own the evidence-preserving move into `deleted_archive`?

Fix likely belongs in:

- `agent_runtime/store.py`
- `hermes_cli/harness.py`
- tests in `tests/hermes_cli/test_harness_cli.py`
- possibly `agent_runtime/snapshot.py` if archive format needs adjustment

### 2. Archive data model and manifest format

Need decide:

- batch directory naming convention
- manifest fields
- whether task/proof/run JSON are copied or moved
- whether proof artifact logs are copied
- how rollback/recovery would find archived evidence

AAA requirement:

- No evidence loss.
- No hard-delete without durable archive.

### 3. Safe eligibility rules

Need define “ready goal” precisely:

Candidate eligible states:

- `done`
- perhaps `cancelled`
- perhaps old terminal/blocked goals only with explicit operator flag

Must not archive:

- `created`
- `dev_implementing`
- `dev_ready_for_qa`
- `qa_testing`
- active runs
- waiting approval runs
- tasks with open incidents unless explicit force flag exists

### 4. Live Dev efficiency / repeated search loop

Need determine whether `runaway_warning` should become an actionable incident or merely progress telemetry.

Root question:

- Did Dev recover and produce proof, or did repeated search consume excessive budget without implementation?

Potential fixes depending on evidence:

- Improve Dev prompt/context for Harness CLI tasks with exact pre-audit facts.
- Add deterministic context bundle for obvious code paths when task includes `harness_cli_archive` risk flag.
- Tighten repeated search loop handling so it opens an incident or forces bounded blocker if no proof appears after warnings.
- Add tests for progress warning → intervention behavior if warnings are currently non-actionable.

### 5. Neko role boundary

Tony asked whether Neko is doing it. Confirmed no; Dev is doing implementation.

Need ensure future tasks make roles obvious in Mission Control:

- Active implementer should display `dev / gpt-launcher` clearly.
- Neko should not look like the implementer when it is only coordinating.

### 6. Proof requirements for final acceptance

QA should require proof for:

- parser exposes archive command
- archive command archives only eligible goals
- active/open tasks are skipped/refused
- archived task is no longer open
- archive can be discovered in snapshot archived_tasks
- targeted tests pass
- relevant non-integration Harness tests pass

### 7. Mission Control live update log ordering/pagination UX bug

Tony observed the live update log display for this run is confusing/broken:

- visible entries showed indices `0015` through `0021`;
- timestamps were newer at the top and older at the bottom;
- the list appeared to stop at `21`;
- updates appeared to overwrite from top to bottom instead of behaving like a clear append/scrolling log.

Example observed entries:

- `0015` / `run.progress` / `2026-06-04T18:36:47.315345Z`
- `0016` / `run.tool.finished` / `2026-06-04T18:36:47.300346Z`
- `0017` / `run.progress` / `2026-06-04T18:36:47.287345Z`
- `0018` / `run.tool.started` / `2026-06-04T18:36:46.752913Z`
- `0019` / `run.tool.started` / `2026-06-04T18:36:46.738911Z`
- `0020` / `run.tool.started` / `2026-06-04T18:36:46.724914Z`
- `0021` / `run.summary` / `2026-06-04T18:35:49.084279Z`

Root questions:

- Is Mission Control intentionally rendering newest-first but numbering oldest-first/newest-first inconsistently?
- Is the log viewport capped at 21 rows without a clear scroll/continuation affordance?
- Are incoming updates replacing rows instead of appending to a stable chronological list?
- Is the data source ordering different from the UI render ordering?

Required fix:

- Make ordering explicit and stable, preferably chronological append with newest at bottom for live logs, or newest-first with clear labels and stable numbering.
- Preserve scroll position predictably when new events arrive.
- Show whether the list is truncated and how many total events exist.
- Add frontend regression/widget test or MCP-visible state proof for ordering/truncation behavior.
- Capture visual proof after fix because this is a UI/operator-readability defect.

Additional Tony UX requirement — compact DM-bubble event pattern:

- The current log is noisy, but the data is valuable; do not remove the rich data.
- Default visible row should be compact and human-readable:
  - `tool <tool name> <started/finished>`
  - examples: `tool Read File started`, `tool Search Files finished`
- The rest of the structured data should be available on hover/expand/details, not shoved into the main row.
- Apply the same progressive-disclosure pattern across all event types, not only tools:
  - one-line summary/bubble in the feed;
  - full fields in hover/expanded/details surface.
- Use DM/message bubble visual language because Tony eventually wants this surfaced in the DM service.
- Keep enough data to diagnose efficiency gaps, loops, proof handoffs, stalls, and failures.
- Add tests/proof for both compact row rendering and expanded detail availability.

Severity: Medium-high for operator trust. It does not corrupt Harness state, but it makes live supervision harder and can hide whether agents are looping or making progress.

### 8. Budget approval / same-session continuation semantics gap

After Dev exceeded the live run token budget, Harness status reported:

- `active_runs=1`
- active run: `run_6905634dd77a`
- task: `task_archive_ready_goal_cli_dev_20260604`
- persona: `dev`
- state: `waiting_on_approval`
- progress summary: `Budget limit reached; waiting for operator approval to continue same session.`
- next action: `run_neko_supervisor`
- open incident: `inc_2090d858` / `run_budget_exceeded`

Alice inspected the CLI and found the intended operator control:

```bash
python -m hermes_cli.main harness run approve run_6905634dd77a --json
```

The approval command returned:

```json
{
  "approved_for_continuation": true,
  "closed_incidents": ["inc_2090d858"],
  "next_expected": "run harness tick to continue same session",
  "run_id": "run_6905634dd77a",
  "session_id": "20260604_143406_67fda1",
  "state": "failed"
}
```

This exposed an operator-semantics gap:

- the command successfully approves continuation and closes the budget incident;
- it says the next expected action is to continue the same session;
- but it also reports the approved run `state` as `failed`, which is confusing and likely wrong for Mission Control/operator UX;
- status previously suggested `run_neko_supervisor`, while the approval response suggests `run harness tick`, so the next-action language may be inconsistent across surfaces;
- this needs deterministic, test-covered semantics for `waiting_on_approval -> approved/continuing` so Neko/Dev/QA routing is obvious and recoverable.

Required later fix:

- define a clear post-approval run state or transition model, e.g. `approved_for_continuation`, `queued`, or `continuing`, rather than reporting `failed` while promising continuation;
- keep the closed incident linked to the approval decision for auditability;
- align `harness status --json`, `harness run approve --json`, and Mission Control UI next-action text;
- add CLI tests for approval response shape and status after approval;
- add Mission Control proof that the UI does not show an approved continuation as a failed run;
- ensure Neko's role is displayed as coordinator/releaser, not implementer, during continuation.

Severity: High for live-run recoverability and operator trust. This does not necessarily block continuation, but it makes it unclear whether the run is safe, failed, or actively approved.

### 9. Post-approval live state inconsistencies during Dev continuation

After approval, Alice started:

```bash
python -m hermes_cli.main harness run-until-settled --task task_archive_ready_goal_cli_dev_20260604 --max-actions 10 --max-seconds 1200 --json
```

Process: `proc_cf88cd94f147`.

While the settle process was still running, Harness status showed the continuation was active and healthy:

- `active_runs=1`
- `running_runs=1`
- `open_incidents=0`
- active run: `run_a44e0d49cc7d`
- persona: `dev`
- stage: `stage_cli_archive_ready_support`
- state: `running`
- progress: `Finished tool terminal: passed`
- health: `healthy`

Dev did make progress:

- `proof.attached` event appeared for proof `test_task_archive_ready_goal_cli_dev_20260604_stage_cli_archive_ready_support_run_a44e0d49cc7d_0_8f695ceb`
- proof event had `exit_code=0`, `status=passed`, `next_expected=request_qa_review`
- task stage update moved `stage_cli_archive_ready_support` to `ready_for_qa`

But the same status/task surfaces exposed several consistency gaps:

1. `harness status --json` said `next_actions=[{"action":"run_dev","reason":"needs dev implementation/fix pass"}]` while an active Dev run was still running and proof had already requested QA review.
2. `harness task show` still listed `open_incident_ids=["inc_2090d858"]` even though `harness status --json` showed `open_incidents=0` and approval had closed that incident.
3. The task `current_stage_id` advanced to `stage_cli_archive_ready_green`, while `stage_cli_archive_ready_support` was marked `ready_for_qa`; this may be intended staged decomposition, but Mission Control/operator UX needs to make clear whether QA should review the support stage before Dev continues green/regression work.
4. Recent events showed contradictory terminal tool outcomes for the same run window: `run.progress` reported `Finished tool terminal: failed`, followed by `run.tool.finished` reporting `Finished tool terminal: passed`. This can make live supervision unreliable unless failure/pass semantics are scoped to exact command/proof.
5. Repeated terminal warnings appeared (`runaway_warning` repeated tool start/finish) but no incident was opened because Dev subsequently attached proof. This is acceptable if warnings are recoverable telemetry, but the UI should clearly distinguish recovered warnings from active interventions.

Required later fix:

- synchronize closed incident IDs back onto task records or make task-level incident lists derive from current incident state;
- ensure `next_actions` suppresses `run_dev` while a Dev run is actively running, and changes to `run_qa` / `request_qa_review` when proof requests QA;
- clarify the stage transition model when one stage is `ready_for_qa` but `current_stage_id` points to a following implementation stage;
- include command identity/exit code in compact event rows so `terminal failed` vs `terminal passed` can be understood without ambiguity;
- add tests for status/task consistency after budget approval, proof attachment, and incident closure.

Severity: High for Mission Control trust. The agents are moving, but the operator surfaces disagree about whether the system needs Dev, QA, or incident cleanup.

### 10. Windows-safe test proof blocker + second Dev budget incident

The post-approval settle run `settle_c72f4ca4` exited with:

- `stop_reason=action_failed`
- `final_task_state=dev_implementing`
- `ticks=2`
- `open_incidents=1`
- task: `task_archive_ready_goal_cli_dev_20260604`

The settle output included a Neko/supervisor-style summary:

```text
Archive-ready CLI/store implementation appears present in the working tree, but green proof is blocked by the Windows pytest-timeout signal config; request deterministic Windows-safe test proof.
```

Then the next Dev action failed with another budget incident:

- incident: `inc_d9ce42ac`
- kind: `run_budget_exceeded`
- run: `run_269a83448bab`
- stage: `stage_cli_archive_ready_green`
- summary: `live run budget exceeded: total_tokens=600238/600000`
- status: `waiting_on_approval`
- next expected: approve budget continuation

Additional evidence from `harness status --json`:

- `active_runs=1`
- `waiting_runs=1`
- `running_runs=0`
- `open_incidents=1`
- health: `critical`
- next action: `run_neko_supervisor` / needs Neko approval to continue budget-limited Dev run
- active run persona: `dev`
- model/provider: `gpt-5.5` / `openai-codex`

Recent events showed another runaway pattern before budget exhaustion:

- repeated `read_file` calls;
- `runaway_warning` with `step=repeated_read_search_loop`;
- `next_expected=bounded_verdict_proof_handoff_or_exact_blocker`;
- then terminal tools ran and the budget limit triggered.

Required later fix:

- make the Harness/Dev Windows test command guidance deterministic and pre-baked for Windows/Git Bash, especially pytest-timeout options that are incompatible with signal mode on Windows;
- add a Windows-safe proof recipe to the task context or Dev persona for Harness tests, e.g. avoid signal timeout mode and use thread-safe timeout behavior where needed;
- reduce repeated read/search loops after Neko has already identified the exact blocker; Dev should run the deterministic proof or report the exact blocker, not re-explore enough to hit budget again;
- consider letting Neko inject a bounded correction into the next Dev continuation: `use Windows-safe pytest command; do not inspect broadly; attach proof or exact failure only`;
- add a budget-efficiency rule that opens an incident earlier when a continuation repeats read/search loops after a supervisor has already given the exact next proof request.

Severity: High. The implementation may be present, but the goal cannot complete until Dev produces deterministic green proof and QA can review it without repeated budget exhaustion.

### 11. Neko handoff routing bug after Dev proof completion

After approving `inc_d9ce42ac`, Alice resumed settle with process `proc_d96b72f6006d`.

Dev then successfully progressed the task to QA readiness:

- final task state from settle output: `dev_ready_for_qa`
- `stage_cli_archive_ready_green`: `ready_for_qa`
- `stage_cli_archive_ready_regression`: `ready_for_qa`
- proof IDs added:
  - `test_task_archive_ready_goal_cli_dev_20260604_stage_cli_archive_ready_green_run_d519f1188206_0_d73d3d89`
  - `test_task_archive_ready_goal_cli_dev_20260604_stage_cli_archive_ready_green_run_d519f1188206_1_0be835fb`
  - `test_task_archive_ready_goal_cli_dev_20260604_stage_cli_archive_ready_regression_run_f530486cc7d4_0_3cd0a53a`
  - `test_task_archive_ready_goal_cli_dev_20260604_stage_cli_archive_ready_regression_run_f530486cc7d4_1_03bde57b`
- Dev regression run `run_f530486cc7d4` closed valid:
  - `decision_type=request_test_run`
  - `state=completed`
  - `total_tokens=78037`
  - `validation_status=valid`
- transition event said:

```text
Passing command proof attached; routed to QA without another Dev model tick.
```

However, the settle loop then attempted a Neko action:

```json
{
  "type": "run_neko_supervisor",
  "reason": "needs Neko Mission Lead to coordinate multi-Dev QA handoff"
}
```

That failed with incident:

- incident: `inc_141d0b0d`
- kind: `provider_failure`
- run: `run_9fe014e5649e`
- persona: `neko_supervisor`
- summary from settle output: `run is not waiting on approval`
- settle `stop_reason=action_failed`
- settle `open_incidents=1`

Harness status after this showed:

- `active_runs=0`
- `open_incidents=1`
- `next_actions=[blocked_by_incident]`
- task state: `dev_ready_for_qa`
- all stages: `ready_for_qa`

This is a routing/state-machine gap:

- Dev had already attached deterministic proof and routed to QA;
- the system then chose Neko instead of QA;
- Neko failed because the run was not waiting on approval;
- an implementation-complete QA-ready task became blocked by a provider_failure incident caused by an invalid supervisor action.

Required later fix:

- when task state is `dev_ready_for_qa` and proof handoff says `next_expected=qa_verification`, next action must be `run_qa`, not `run_neko_supervisor`;
- reserve Neko budget-approval path for actual `waiting_on_approval` runs only;
- do not label deterministic state mismatch (`run is not waiting on approval`) as provider_failure; use `routing_error`, `state_transition_error`, or another precise incident kind;
- add a regression test for `dev_ready_for_qa + proof_ids + no waiting run -> run_qa`;
- keep Mission Control UI from showing this as a model/provider outage.

Severity: Critical for agent orchestration. This directly blocks completion after Dev did the requested proof work.

Reproduction after operator closed `inc_141d0b0d`:

- Alice closed the incident with an explicit operator reason to unblock QA routing.
- Alice reran `run-until-settled`.
- Settle `settle_48c7711a` immediately selected `run_neko_supervisor` again.
- It failed the same way: `run is not waiting on approval`.
- New incident opened: `inc_3759d5a9`.
- Final state remained `dev_ready_for_qa`.

This confirms the bug is deterministic/reproducible, not a one-off stale incident artifact.

Temporary live unblock applied during the live audit:

- Alice briefly patched `agent_runtime/state_machine.py` so `DEV_READY_FOR_QA` with all Dev stages complete and proof IDs routed directly to `RUN_QA`.
- This let the live run continue far enough to expose the stale incident-ID QA blocker below.
- Follow-up regression later showed the bypass was too broad for intended multi-agent choreography because sequential-specialist flows must still pass through Neko coordination.
- Alice reverted the broad direct-QA behavior and kept the narrower resolved-incident-only QA retry fix documented later in this file.

This confirmed the Neko handoff bug and enabled further live QA discovery, but it is not the final product behavior.

### 12. QA blocked on stale task incident IDs despite zero open incidents

After the state-machine patch, Harness status correctly reported:

- `next_actions=[run_qa]`
- `open_incidents=0`
- health: `healthy`

Alice resumed the settle loop and QA ran:

- settle: `settle_3d6fc984`
- QA run: `run_1fe7c6473987`
- action: `run_qa`
- action result: `ok=true`
- decision: `report_qa_verdict`

QA verdict summary:

```text
QA blocked: implementation evidence and targeted regressions look good, but the task still has open run-budget incidents, violating the no-open-incidents acceptance gate.
```

Settle result:

- `final_task_state=blocked`
- `stop_reason=task_blocked`
- `open_incidents=0`
- `ticks=1`

This confirms another state consistency bug:

- global incident store/status says no open incidents;
- QA sees task-level stale `open_incident_ids` and blocks;
- previously observed task snapshot still carried closed IDs `inc_2090d858` and `inc_d9ce42ac`;
- incident closure is not synchronizing task-level incident references, and QA relies on the task field.

Required later fix:

- incident close should remove the incident ID from the related task's `open_incident_ids`, or task reads should derive open incidents from the incident store instead of stale embedded IDs;
- QA acceptance gate should check actual open incidents, not closed historical incident IDs;
- add regression coverage: close incident -> task.open_incident_ids no longer includes it -> QA no-open-incidents gate passes.

Severity: Critical for completion. It blocks a proof-good implementation because closed incidents remain attached to the task record.

Operator root-cause intervention applied:

- Patched `IncidentStore.close` in `agent_runtime/store.py` to remove the closed incident ID from the linked task's `open_incident_ids`.
- Added regression test `test_incident_store_close_removes_task_open_incident_reference` in `tests/agent_runtime/test_store.py`.
- Verification passed:

```text
python -m pytest -o addopts='' tests/agent_runtime/test_store.py::test_incident_store_close_removes_task_open_incident_reference -q
1 passed
```

Operator then re-closed historical closed budget incidents through the CLI to trigger cleanup:

- `inc_2090d858`
- `inc_d9ce42ac`

Task snapshot after cleanup:

- `state=blocked`
- `open_incident_ids=[]`
- `proof_count=8`
- `current_stage_id=stage_cli_archive_ready_regression`

Remaining recovery gap:

- Even after incident hygiene is fixed, the previous QA blocked verdict leaves task state as `blocked`.
- With no open incidents, `harness status` now routes to `run_dev` with reason `needs dev recovery from QA blocked verdict`.
- In this specific case the QA blocker was state hygiene, not implementation failure, so the ideal recovery may be to re-run QA after the stale incident IDs are cleaned rather than sending Dev back through another implementation pass.

Required later fix:

- represent QA blocked reasons as structured findings so resolved environment/state-hygiene blockers can route back to QA without unnecessary Dev work;
- add a safe operator command for `retry QA after resolved non-code blocker` or deterministic state transition from `blocked` to `dev_ready_for_qa` when all code proof remains valid and no incidents are open.

Additional settle-loop inconsistency after cleanup:

- `harness status --json` reported next action `run_dev` with reason `needs dev recovery from QA blocked verdict`.
- But `harness run-until-settled --task ...` immediately stopped with:
  - `actions_taken=[]`
  - `final_task_state=blocked`
  - `open_incidents=0`
  - `stop_reason=task_blocked`
  - `ticks=0`

This means status and settle disagree: status advertises a recovery action, while settle refuses to execute anything for the same blocked/no-incident task.

Required later fix:

- align `run-until-settled` stop conditions with `status.next_actions`;
- if blocked/no-incident has a valid recovery action from the state machine, settle should execute it or clearly report why it refuses;
- otherwise status should not advertise `run_dev`.

Explicit tick after this gap:

- Alice ran `python -m hermes_cli.main harness tick --task task_archive_ready_goal_cli_dev_20260604 --json`.
- Tick followed the advertised `run_dev` path.
- Dev run `run_f6e6297b94ae` failed after `total_tokens=581930` with validation invalid.
- New incident: `inc_3b279539`.
- Incident kind: `model_invalid_output`.
- Summary: `Dev budget pressure gate failed: run is approaching budget without a proof-oriented handoff; stop exploration and request_test_run, request_qa_review with proof_ids, or block with exact evidence so Neko can steer.`

This proves routing a resolved non-code QA blocker back to Dev is expensive and wrong for this class of issue. The task had proof and the stale incident IDs were already cleaned; the needed action was QA retry, not Dev recovery.

Operator root-cause intervention applied:

- Patched `MissionStateMachine.next_action` so `BLOCKED` tasks with no open incident IDs and a QA blocked verdict whose blocking findings are only `open_incidents` route to `RUN_QA` with reason `retry QA after resolved incident-only blocker`.
- Added helper `_has_resolved_incident_only_qa_block` in `agent_runtime/state_machine.py`.
- Patched `agent_runtime/status.py` so `status.next_actions` constructs `MissionStateMachine(proof_store=proof_store)` and matches execution routing.
- Added regression tests:
  - `test_state_machine_retries_qa_after_resolved_incident_only_blocker`
  - `test_status_uses_proof_store_for_resolved_incident_only_qa_blocker`
- Verification passed:

```text
python -m pytest -o addopts='' tests/agent_runtime/test_state_machine.py::test_state_machine_retries_qa_after_resolved_incident_only_blocker tests/agent_runtime/test_status.py::test_status_uses_proof_store_for_resolved_incident_only_qa_blocker tests/agent_runtime/test_store.py::test_incident_store_close_removes_task_open_incident_reference -q
3 passed in 0.59s
```

After this, `harness status --json` correctly reported:

- `open_incidents=0`
- `next_actions=[run_qa]`
- reason: `retry QA after resolved incident-only blocker`
- health: `healthy`

Additional settle-loop root-cause intervention:

- `run-until-settled` still stopped immediately on `TaskState.BLOCKED` before consulting the proof-aware state machine.
- Patched `TickEngine._settled_boundary` so a blocked task is only considered settled when `MissionStateMachine.next_action(task)` returns `NOOP`; if a valid recovery action exists, settle continues.
- Added regression test `test_run_until_settled_does_not_treat_blocked_task_as_boundary_when_recovery_action_exists`.
- Verification passed:

```text
python -m pytest -o addopts='' tests/agent_runtime/test_ticker.py::test_run_until_settled_does_not_treat_blocked_task_as_boundary_when_recovery_action_exists tests/agent_runtime/test_status.py::test_status_uses_proof_store_for_resolved_incident_only_qa_blocker tests/agent_runtime/test_state_machine.py::test_state_machine_retries_qa_after_resolved_incident_only_blocker -q
3 passed in 0.71s
```

### 13. Commit and cleanliness

Final acceptance should include:

- implementation commit by Dev if code changes are made
- clean git tree
- `harness status --json` with:
  - `open_tasks=0` or only intentional active task while running
  - `active_runs=0`
  - `open_incidents=0`
  - `runtime_health.ok=true`

## Monitoring Rule

Alice should baby/monitor the live run but avoid intervention unless:

- wrong persona routes the task;
- active run stalls;
- open incident appears;
- repeated search loop consumes budget without proof;
- Dev produces unsafe archive semantics;
- tests fail and Dev cannot recover;
- task blocks without actionable context request.

## Mission Control availability check after Tony report

Tony reported Mission Control went unavailable during the live archive-ready run.

Alice checked two layers:

1. Harness CLI availability:
   - `python -m hermes_cli.main harness status --json` initially failed after Dev's incomplete patch because parser construction only exposed `{init, task}`.
   - Root cause: Dev added `archive-ready` parser entries referencing missing `_cmd_task_archive_ready` / `_cmd_task_archive` handlers, leaving top-level Harness command registration broken.
   - Alice patched the missing handlers as a root-cause intervention because Mission Control availability was broken.
   - Focused archive tests then passed: `3 passed in 0.75s`.

2. Launcher Stage C Mission Control UI availability:
   - `mcp_launcher_qa_open_app_tab(tab=missionControl, hermes_profile=alice, harness_runtime_root=X:/Eternia/.hermes/agent-runtime, hermes_home=X:/Eternia/.hermes/profiles/alice, profile=stagec-smoke)` failed.
   - Failure class: `launch_wrong_debug_target_missing_marionette`.
   - Safe message: Debug EXE built against `lib/main.dart`, not `lib/main_marionette.dart`.
   - Suggested recovery: rebuild Launcher debug EXE with:

```bash
flutter build windows --debug --target lib/main_marionette.dart
```

This is a separate Launcher QA freshness/build-target blocker, not proof that Harness runtime is unavailable. It should be fixed/rebuilt before expecting MCP Mission Control screenshots/control to work.

## Current Status At Note Creation

- Goal created.
- Launcher Dev confirmed active.
- No implementation proof yet recorded in this note.
- Efficiency warning observed and needs follow-up after the live run completes or hits a boundary.
