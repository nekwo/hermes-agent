# Stage 43 — Mission Control independent investigation brief

## Purpose

Tony wants GPT Harness and Claude to independently investigate why Mission Control is not yet AAA-working end-to-end, then compare their findings against Alice's Stage 39–42 implementation/audit stages.

This brief records the problems encountered during the Stage 39–42 live Harness audit and gives a copy/paste prompt for independent investigators.

## Repositories / roots

- Harness / Hermes Agent: `X:\Eternia\hermes-agent`
- Agent runtime root: `X:\Eternia\.hermes\agent-runtime`
- Launcher frontend: `X:\Unreal Engine\Engine\Launcher\EterniaLauncher`
- Launcher brain: `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\Launcher_Brain`
- Shared Arcadia brain: `X:\Unreal Engine\Engine\ArcadiaLabs_Brain`

## Existing Stage docs to compare against

- `X:\Eternia\hermes-agent\docs\agent-runtime-harness\40-cross-stack-contract-smoke-neko-release-gap.md`
- `X:\Eternia\hermes-agent\docs\agent-runtime-harness\41-archive-ready-live-goal-gaps.md`
- `X:\Eternia\hermes-agent\docs\agent-runtime-harness\42-mission-control-archive-button-live-bridge.md`
- This file: `X:\Eternia\hermes-agent\docs\agent-runtime-harness\43-mission-control-independent-investigation-brief.md`

## Commits from Alice's implementation stages

Harness / Hermes Agent:

- `08654fdb3 docs(harness): record cross-stack smoke handoff gap`
- `e33bb3b92 feat(harness): archive ready goals and recovery routing`
- `a39cebe45 docs(harness): record archive button bridge closure`

Launcher:

- `26f38f9b fix(mission-control): wire archive button to harness CLI`

## Problem register from Stage 39–42

### P0/P1: Mission Control must be actually usable end-to-end

Mission Control should support real operator workflows, not just unit-tested fragments:

- View live Harness state from the correct runtime root/profile.
- Show active/done/ready/archive states truthfully.
- Let Tony create/run/observe/QA/archive goals without needing Alice to manually recover state.
- Give legible logs/proof/status in the Launcher UI.
- Support visual QA through the correct marionette/debug target.

### 1. Neko prematurely blocked cross-stack handoff

Evidence:

- Stage 40 cross-stack smoke: backend proof passed, but Neko blocked instead of releasing frontend stage.
- Operator recovery was needed to route to frontend contract probe.

Impact:

- Sequential specialist flows are not deterministic enough.
- Backend -> frontend release should not depend on another model tick if proof/state already satisfies the handoff.

Needed investigation:

- Inspect state machine / Neko coordination flow for sequential specialist stages.
- Determine whether deterministic transitions can replace model-mediated release for proof-backed handoffs.

### 2. Harness CLI archive support was missing

Evidence:

- `hermes_cli/harness.py` lacked archive handlers before Stage 41.
- Added `harness task archive-ready --json` and `harness task archive <task_id> --json`.

Impact:

- Mission Control archive UI could not be live-backed before CLI/runtime support.

Current status:

- Fixed in Harness commit `e33bb3b92`.

Independent audit:

- Verify command behavior, archive manifests, event logging, active-task refusal, and evidence preservation.

### 3. `task.archived` event type was missing

Evidence:

- Archive test failed with `ValueError: unknown event type: task.archived`.

Current status:

- Fixed in `agent_runtime/events.py` in commit `e33bb3b92`.

Independent audit:

- Verify sanitizer/redaction coverage for the new event type and archived paths/manifests.

### 4. Archive storage had to preserve evidence

Evidence:

- User requirement: clear tasks without hard-deleting proof.
- `ArchiveStore` added to move task/run/proof artifacts into `deleted_archive/` with manifest.

Current status:

- Fixed/tested in Stage 41.

Independent audit:

- Confirm archived tasks disappear from open listings but remain discoverable in archive listing/snapshot.
- Check exact file moves and manifest schema.
- Check behavior on partial failures, duplicate archives, active runs, malformed task IDs, missing proof files.

### 5. Active/non-terminal tasks needed archive refusal

Evidence:

- Archive command must refuse/skip active or unsafe tasks.

Current status:

- Tested in Stage 41.

Independent audit:

- Confirm active/running/blocked/waiting tasks are not archived incorrectly.
- Confirm refusal messages are safe and actionable.

### 6. Dev agent burned excessive budget

Evidence:

- Stage 41 Dev exceeded token budget.
- Stage 42 Dev used roughly 467k tokens for a narrow bridge fix.

Impact:

- Live-agent workflow is too expensive/noisy for small fixes.
- Neko/Alice need earlier steering or deterministic narrow instructions.

Needed investigation:

- Inspect recent run events for repeated read/search loops.
- Determine whether Harness needs stricter per-stage tool budgets, proof deadlines, or automatic handoff/intervention earlier.

### 7. Approval/continuation semantics are confusing

Evidence:

- `harness run approve` closed incidents but returned a shape that looked like `state: failed` with a separate `next_expected: run harness tick to continue same session`.

Impact:

- Operators cannot tell whether approval succeeded or how to continue.

Needed investigation:

- Redesign approval response schema/status wording.
- Decide whether approval should auto-continue, enqueue continuation, or emit a clear `approved_pending_tick` state.

### 8. Stale incident IDs remained attached to tasks

Evidence:

- Incident store had no open incidents, but task JSON still contained stale `open_incident_ids`.
- QA saw blockers that no longer existed.

Current status:

- `IncidentStore.close` now removes closed incident IDs from linked task.

Independent audit:

- Verify all incident close paths use the cleanup code.
- Check bulk close, operator close, approve close, and direct store close.
- Add any missing regression tests.

### 9. Blocked-task recovery routing was wrong

Evidence:

- Resolved non-code blockers routed toward Dev instead of letting QA retry.

Current status:

- Narrow resolved-incident-only QA retry route added.

Independent audit:

- Confirm the narrow route does not bypass Neko for genuine sequential-specialist choreography.
- Confirm no broad direct-QA bypass remains.

### 10. `harness status` disagreed with execution routing

Evidence:

- Status displayed a different next action from what `tick_once` would actually execute.

Current status:

- `build_status` passes `proof_store` into `MissionStateMachine`.

Independent audit:

- Compare `harness status --json`, `harness snapshot --json`, and actual `tick_once` routing across ready/blocked/done/incident-only cases.

### 11. `run-until-settled` stopped too early on blocked tasks

Evidence:

- It treated `TaskState.BLOCKED` as terminal even when the state machine had a valid recovery action.

Current status:

- `_settled_boundary` now consults state machine.

Independent audit:

- Verify no infinite loops were introduced.
- Confirm terminal blocked tasks still stop when no recovery action exists.

### 12. Broad direct-QA bypass was almost introduced, then reverted

Evidence:

- A broad route from `DEV_READY_FOR_QA` straight to QA would have broken Neko sequential choreography.

Current status:

- Reverted; narrow resolved-incident-only retry retained.

Independent audit:

- Verify current code preserves Neko coordination where intended.
- Identify whether deterministic proof-backed handoffs should be encoded differently.

### 13. Launcher archive button existed but was not live-wired

Evidence:

- `MissionControlIntent.archiveReadyGoal` existed.
- `CliMissionControlActionRepository._argsForIntent` mapped archiveReadyGoal to `null` before Stage 42.
- This caused: `Archive Ready Goal is not supported by the live harness CLI yet.`

Current status:

- Fixed in Launcher commit `26f38f9b`:

```dart
MissionControlIntentType.archiveReadyGoal => <String>[
  'harness',
  'task',
  'archive',
  intent.goalId!,
  '--json',
],
```

Independent audit:

- Verify button intent wiring, repository args, failure handling, success feedback, state refresh after archive, and archived-goal rendering.
- Determine if UI should use `archive-ready` bulk command or selected-goal `archive` command.

### 14. Mission Control visual/MCP proof is blocked by stale debug EXE target

Evidence:

- MCP launch failed with `launch_wrong_debug_target_missing_marionette`.
- Error said debug EXE was built against `lib/main.dart`, not `lib/main_marionette.dart`.

Needed command:

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Set-Location 'X:\Unreal Engine\Engine\Launcher\EterniaLauncher'; flutter build windows --debug --target lib/main_marionette.dart"
```

Impact:

- Cannot visually prove Mission Control through semantic MCP until the correct target is built/launched.

Independent audit:

- Rebuild correct target.
- Use Launcher MCP semantic controls/state and screenshots to prove Mission Control workflow.
- Verify runtime root/profile points at `X:\Eternia\.hermes\agent-runtime` and Hermes profile `alice`/expected QA smoke profile.

### 15. Mission Control live log UX is noisy/confusing

Evidence:

- Tony reported logs newest-at-top, overwriting top-to-bottom, stopping around a visible limit like `0015`–`0021`.
- Current logs show noisy `run.progress`, `run.tool.started`, `run.tool.finished`, proof events, and runaway warnings without operator-friendly grouping.

Desired UX:

- Compact DM-bubble style event rows, e.g.:
  - `tool read_file started`
  - `tool read_file finished`
  - `QA verdict approved`
  - `proof attached proof_...`
- Details hidden/expandable on hover/click.
- Stable chronological ordering or an explicit newest-first setting.
- Clear truncation/window indicator.
- No apparent overwriting.

Independent audit:

- Inspect Launcher Mission Control log mapping/rendering.
- Inspect Harness event schema and redaction-safe fields.
- Propose/implement log grouping and UI tests.

### 16. QA also showed repeated read/search loop warnings

Evidence:

- Stage 42 QA run had `runaway_warning`, `repeated_read_search_loop` events while still approving.

Impact:

- QA proof succeeded, but verification discipline is inefficient and noisy.

Needed investigation:

- Determine whether QA prompts/tool budgets need tighter proof-first constraints.
- Ensure warnings surface to Tony without making Mission Control look broken.

## Current observed closure state after Stage 42

Harness status after Stage 42:

```text
open_tasks=0
active_runs=0
waiting_runs=0
open_incidents=0
next_actions=[]
runtime_health.ok=true
```

Launcher targeted tests after Stage 42:

```bash
flutter test test/features/mission_control/mission_control_bridge_test.dart test/features/mission_control/mission_control_page_test.dart
```

Result:

```text
All tests passed!
```

## Copy/paste prompt for GPT Harness / Claude

```text
You are doing an independent AAA investigation of Tony's Mission Control / Agent Runtime Harness integration. Do not trust Alice's implementation summary as proof; use it only as a comparison target after your own investigation.

Goal: make Mission Control actually work end-to-end for Tony. Investigate Harness runtime behavior, Launcher Mission Control UI/bridge behavior, agent orchestration, logs/proofs, and visual/MCP proof readiness. Produce a gap report and implementation plan, and if authorized, implement/fix root-cause blockers with tests and commits.

Repos / roots:
- Harness / Hermes Agent: X:\Eternia\hermes-agent
- Agent runtime root: X:\Eternia\.hermes\agent-runtime
- Launcher frontend: X:\Unreal Engine\Engine\Launcher\EterniaLauncher
- Launcher brain: X:\Unreal Engine\Engine\Launcher\EterniaLauncher\Launcher_Brain
- Shared Arcadia brain: X:\Unreal Engine\Engine\ArcadiaLabs_Brain

Read first:
- X:\Eternia\hermes-agent\docs\agent-runtime-harness\40-cross-stack-contract-smoke-neko-release-gap.md
- X:\Eternia\hermes-agent\docs\agent-runtime-harness\41-archive-ready-live-goal-gaps.md
- X:\Eternia\hermes-agent\docs\agent-runtime-harness\42-mission-control-archive-button-live-bridge.md
- X:\Eternia\hermes-agent\docs\agent-runtime-harness\43-mission-control-independent-investigation-brief.md
- X:\Unreal Engine\Engine\Launcher\EterniaLauncher\CLAUDE.md
- X:\Unreal Engine\Engine\Launcher\EterniaLauncher\Launcher_Brain\Brain Index.md

Alice's stage commits to compare against:
Harness:
- 08654fdb3 docs(harness): record cross-stack smoke handoff gap
- e33bb3b92 feat(harness): archive ready goals and recovery routing
- a39cebe45 docs(harness): record archive button bridge closure
Launcher:
- 26f38f9b fix(mission-control): wire archive button to harness CLI

Independent investigation requirements:
1. Verify current git status in both repos. Do not overwrite unrelated local changes.
2. Verify Harness CLI commands actually work:
   - python -m hermes_cli.main harness status --json
   - python -m hermes_cli.main harness snapshot --json
   - python -m hermes_cli.main harness task archive --help
   - python -m hermes_cli.main harness task archive-ready --help
3. Audit archive behavior:
   - evidence preservation in deleted_archive/ manifests
   - active/non-terminal task refusal
   - archived task visibility in snapshot/history
   - task.archived event safety/redaction
4. Audit state-machine/recovery behavior:
   - stale incident cleanup
   - resolved incident-only QA retry
   - status vs tick routing consistency
   - run-until-settled blocked boundary
   - Neko backend->frontend handoff behavior
5. Audit Launcher Mission Control bridge/UI:
   - archive button emits MissionControlIntent.archiveReadyGoal
   - CliMissionControlActionRepository maps it to `hermes harness task archive <goal_id> --json`
   - success/failure UI feedback is truthful
   - archive action refreshes state and archived goals render correctly
   - non-ready/active refusal remains Harness-side and is understandable
6. Fix visual/MCP proof blocker if possible:
   - rebuild Launcher debug target with:
     powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Set-Location 'X:\Unreal Engine\Engine\Launcher\EterniaLauncher'; flutter build windows --debug --target lib/main_marionette.dart"
   - launch Mission Control through Stage C MCP / semantic controls
   - capture screenshot/state proof of the Mission Control workflow
7. Audit Mission Control log UX:
   - identify why logs appear newest-at-top/overwriting/truncated around visible IDs
   - propose or implement compact DM-bubble event rows with expandable details
   - keep redaction-safe fields and preserve raw logs
8. Audit Dev/QA/Neko efficiency:
   - inspect recent run events for repeated read/search loops and high token use
   - propose deterministic routing/tool-budget improvements where appropriate
9. Compare your findings to Alice's Stage 40/41/42 implementation docs. For each problem, classify:
   - Confirmed fixed
   - Partially fixed
   - Not fixed
   - New gap found
   - Alice implementation regression risk
10. Produce final report with exact evidence:
   - commands run and exit codes
   - files inspected/changed
   - tests run and results
   - screenshots/artifacts if visual proof works
   - commits created, if any
   - remaining gaps with severity and next implementation stages

Quality bar:
- Enterprise/AAA: no false “done.”
- Do not claim button works from code inspection alone; use tests and, if possible, visual/MCP proof.
- Do not claim Mission Control works if runtime root/profile is wrong.
- Prefer root-cause fixes over symptoms.
- Keep patches narrow and aligned with existing architecture.
- Preserve evidence; do not hard-delete Harness task/run/proof artifacts.
- If visual proof is blocked, record the exact blocker and still complete code/test proof.
```

## Desired final output from each independent investigator

```text
Independent Mission Control Investigation — <agent name>

Summary verdict:
- Working:
- Not working:
- Highest severity gap:

Comparison to Alice stages:
- Stage 40:
- Stage 41:
- Stage 42:

Evidence:
- Commands:
- Tests:
- Screenshots/artifacts:
- Commits:

Implementation plan / fixes:
- P0:
- P1:
- P2:

Final recommendation:
- Ready for Tony use / blocked / needs follow-up implementation
```
