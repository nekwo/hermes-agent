# Stage 63 - First-Class Persona Instances in Mission Control

## Goal

Make persona instances first-class operator objects across Harness CLI, Harness snapshots, and Mission Control.

Tony must be able to:

- test one persona by itself without creating a real product mission;
- create or inspect free-floating persona instances for prompt/tool/profile testing;
- see task-bound workers as durable instances rather than reconstructing them from recent runs;
- message, pause, resume, interrupt, nudge, possess, release, close, and archive instances through supported runtime commands;
- distinguish diagnostic evidence from production proof;
- trust that Mission Control is showing runtime truth, not placeholder persona shells.

This stage does **not** invent a separate agent manager. It completes the instance path already started by Stage 55 and Stage 56:

> **2026-06-18 update:** Stage 64 (`64-aaa-persona-instance-chat-history-contract.md`) extends this stage with the current chat-history/open-chat contract. Current code now includes `persona.instance.open_chat` capability/CLI/test coverage and Launcher has started `persona.instance.create` integration, but Launcher still needs the `open_chat` bridge and a dedicated redaction-safe chat-history snapshot model before the product is AAA.

```text
Stage 55 persona diagnostics -> Stage 56 persona instances/assignments -> Stage 63 Mission Control first-class instance UX and lifecycle
```

## Non-Negotiable Product Stance

1. **Diagnostics are first-class, but not production proof.** A standalone `persona diagnose` run can prove persona behavior, tool readiness, prompt discipline, and routing. It must not be presented as QA approval or product acceptance unless a separate task-bound proof gate accepts it.
2. **Free-floating instances are valid for testing.** They are useful for testing a persona by itself, but they must carry `mode=free_floating` or `kind=diagnostic` so they cannot masquerade as mission workers.
3. **Task-bound workers stay bound to tasks/goals.** Real delivery still flows through task-bound assignments, proof, QA, archive, and burn-in.
4. **Mission Control consumes Harness contracts.** Launcher must not crawl raw files or infer runtime state from scattered logs. Harness is the source of truth through `snapshot --json` fields: `persona_instances`, `persona_assignments`, `worker_sessions`, runs, events, and proofs.
5. **Default-off remains mandatory.** Standard Hermes users must not get enterprise persona-instance behavior unless the feature flags are enabled.
6. **No hidden token loops.** Creating, listing, showing, or messaging an instance must not start autonomous model work unless the command explicitly says it runs a diagnostic/turn/tick.

## Current Deep Audit - 2026-06-13

### Harness docs already present

- `docs/agent-runtime-harness/55-persona-operations-diagnostics.md`
  - Documents `harness persona diagnose` as the existing per-persona test path.
  - Explicitly lists next substages: Mission Control diagnostic buttons, persona operation history, and live-token recipes.
- `docs/agent-runtime-harness/56-persona-instance-assignment-runtime.md`
  - Defines `PersonaInstance -> PersonaAssignment -> AgentRun -> Events/Proofs`.
  - Already includes a deep-audit closure and implementation decisions.
  - Needs this Stage 63 to pin down Mission Control UX, diagnostic/free-floating/task-bound modes, and rollout from the current implemented code.
- `docs/agent-runtime-harness/48-worker-session-kernel-normal-harness.md`
  - Defines `WorkerSession`, control intents, heartbeat/context receipt fields, and Mission Control worker roster requirements.

### Harness code already implemented

- `agent_runtime/models.py`
  - `PersonaInstance` exists with instance id, persona id, profile, runtime root, worker state, active assignment/task/worker/run ids, session id, context/compression receipts, prompt/skill hashes, token/tool counters, heartbeat, and timestamps.
  - `PersonaAssignment` exists with assignment id, persona instance id, persona id, kind, state, title, message, task/stage/operation ids, repo bundle/repo, affected paths, proof targets, acceptance/non-goals, allowed decisions/tools, run/proof/context receipts, and signal hash.
  - `WorkerSession` already has `current_assignment_id`, possession fields, heartbeat fields, context receipt fields, prompt/skill hashes, budget counters, and active run id.
  - `AgentRun` stores assignment identity through `progress` for Stage 63. Stage 63 explicitly keeps `AgentRun.task_id` required and does not add taskless run records.
- `agent_runtime/persona_assignments.py`
  - `PersonaInstanceStore.ensure_for_persona()`, `list_all()`, `get()`, `update_from_worker()`, and `derive_from_workers()` exist.
  - `PersonaAssignmentStore.create_or_resume()` exists and dedupes active assignments using a deterministic signal hash.
  - `persona_instance_summary()` and `persona_assignment_summary()` exist for status/snapshot/CLI JSON.
  - Feature helpers exist: `persona_instance_runtime_enabled(config)` and `persona_assignment_store_enabled(config)`.
- `agent_runtime/persona_diagnostics.py`
  - `PersonaDiagnosticController` creates a normal diagnostic task and creates `PersonaAssignment(kind=diagnostic)` only when the enterprise persona flags are enabled. Stage 63 keeps that feature-gated behavior and makes Mission Control surface the enabled-path records.
- `agent_runtime/runtime_config.py`
  - `EnterpriseWorkerSessionsConfig` already has `persona_instance_runtime: bool = False` and `persona_assignment_store: bool = False`.
- `agent_runtime/status.py`
  - `build_status()` already has code paths for `persona_instances` and `persona_assignments` when the feature flags are enabled.
- `agent_runtime/snapshot.py`
  - `build_snapshot()` already derives persona instances from configured personas plus worker sessions and can include active/recent persona assignments.
  - Task summaries and archived task summaries include persona assignment/stream information.
- `hermes_cli/harness.py`
  - `harness persona list --json` exists.
  - `harness persona show <persona_id_or_instance_id> --json` exists.
  - `harness persona assignments [--persona <persona>] [--task <task_id>] --json` exists.
  - `harness persona message <persona> --task <task_id> --message ... --json` exists and queues an operator-message assignment without ticking.
  - `harness persona diagnose <persona> --title ... --message ... --json` exists and runs one bounded diagnostic task/turn.
  - Worker controls exist separately under `harness worker ...` for task-bound worker sessions.

### Current live profile state

Live `hermes harness persona list --json` returns:

```json
{
  "assignment_store_enabled": false,
  "feature_enabled": false,
  "persona_instances": []
}
```

Live config currently has:

```json
"enterprise_worker_sessions": {
  "enabled": false,
  "persona_instance_runtime": false,
  "persona_assignment_store": false,
  "worker_session_store": true
}
```

Therefore Mission Control can only show fallback/persona-shell behavior unless Tony enables the enterprise persona instance flags or a dev fixture overrides them.

### Launcher Mission Control current state

- `lib/features/mission_control/data/mission_control_bridge.dart`
  - Maps runtime lifecycle, task, worker, run, and agent-message intents.
  - Does **not** map `persona diagnose`, `persona list/show/assignments`, or free-floating instance creation.
- `lib/features/mission_control/data/mission_control_actions.dart`
  - `MissionControlIntentType` has `sendAgentMessage`, worker controls, run controls, and runtime controls.
  - It has no `testPersona`, `queuePersonaMessage`, `createFreeFloatingInstance`, `runPersonaInstanceOnce`, `closePersonaInstance`, or `archivePersonaInstance` intent.
- `lib/features/mission_control/data/mission_agent_instance.dart`
  - `MissionAgentInstance` is derived from agent logs/personas and worker ids.
  - It does not yet parse Harness `persona_instances` / `persona_assignments` directly.
- `lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart`
  - Shows persona chips and an inline Agent Instance Menu.
  - Displays worker session when present.
  - Has no Test Persona, Create Free-Floating Instance, Run One Turn, Close, or Archive controls.
- `lib/features/mission_control/mission_control_page.dart`
  - Provides baseline idle fallback instances when no real agent instances are present.
  - These fallback instances are useful for debugging the chat surface, but they are not real Harness instances.

## Instance Vocabulary

Use these terms everywhere in code, docs, tests, and UI.

### Diagnostic assignment

A bounded one-persona test, currently backed by `harness persona diagnose`.

- `PersonaAssignment.kind = "diagnostic"`
- Always uses a diagnostic/sandbox task in Stage 63 because `AgentRun.task_id` remains required.
- Runs at most the requested bounded action/seconds limit.
- Appears in Mission Control as `Diagnostic`.
- Always sets `production_proof_eligible=false` in assignment/result summaries unless a later explicit task proof-import command creates a separate production proof record.

### Free-floating persona instance

A persistent or semi-persistent testing instance that is not bound to a product goal.

- Stage 63 adds `PersonaInstance.mode` with exactly these values: `configured`, `diagnostic`, `free_floating`, `task_bound`, `closed`.
- Stage 63 adds `PersonaAssignment.kind = "free_floating_message"` for free-floating chat/setup messages and `PersonaAssignment.kind = "free_floating_run_once"` for one-turn execution.
- Stage 63 stores free-floating setup/message assignments with `task_id = null`; `run-once` converts the active execution assignment to the sandbox-task path before creating an `AgentRun`.
- Stage 63 execution creates a Harness-owned sandbox task and links it through `PersonaAssignment.task_id`; `AgentRun.task_id` remains required.
- Appears in Mission Control as `Free-floating`.
- Valid for prompt/tool/profile/persona testing.

### Task-bound worker instance

A durable worker attached to a goal/task/stage.

- Backed by `WorkerSession` plus `PersonaInstance` view plus `PersonaAssignment(kind="task_stage" | "operator_message" | "qa_review" | "recovery")`.
- Appears in Mission Control as `Task-bound`.
- Can produce production proof only through task proof gates.

## Stage 63A - Enable and Certify Existing Persona Instance Runtime

### Goal

Turn the already-implemented Harness persona instance/assignment stores on in a controlled Tony profile path and prove they are accurate before Mission Control depends on them.

### Deep audit findings

- The stores, summaries, status/snapshot hooks, CLI list/show/assignments/message commands, and diagnostic assignment links already exist.
- The live config keeps `enterprise_worker_sessions.enabled`, `persona_instance_runtime`, and `persona_assignment_store` false.
- Because the flags are false, `harness persona list --json` returns no instances even though configured personas exist.

### Implementation tasks

1. Add a Tony-profile config patch or documented profile command that enables only:
   - `enterprise_worker_sessions.enabled=true`
   - `enterprise_worker_sessions.mode="observe_only"`
   - `enterprise_worker_sessions.persona_instance_runtime=true`
   - `enterprise_worker_sessions.persona_assignment_store=true`
2. Do not enable authoritative tick routing in this stage.
3. Add or update tests proving disabled configs omit or return disabled persona instance fields.
4. Add or update tests proving enabled observe-only configs list all configured personas as idle instances with no active task.
5. Add a runtime smoke command to the doc/runbook:

```bash
python -m hermes_cli.main harness persona list --json
python -m hermes_cli.main harness persona show dev --json
python -m hermes_cli.main harness snapshot --json
```

### Tests

- `tests/agent_runtime/test_status.py`
  - disabled config reports no enabled persona-instance runtime.
  - enabled config emits all configured persona instances.
- `tests/agent_runtime/test_snapshot.py`
  - enabled snapshot includes `persona_instances` and `persona_assignments` keys.
  - disabled snapshot remains backward-compatible.
- `tests/hermes_cli/test_harness_cli.py`
  - `harness persona list --json` disabled shape.
  - `harness persona list --json` enabled shape.

### Acceptance

- With flags enabled, `harness persona list --json` lists Neko, Launcher Dev, Backend Dev, PM, and QA as idle or current-state instances.
- With flags disabled, standard Hermes behavior is unchanged.
- No model call happens from list/show/snapshot/status.

## Stage 63B - Mission Control Reads Harness Persona Instances

### Goal

Stop Mission Control from relying on placeholder persona shells when Harness emits real `persona_instances` and `persona_assignments`.

### Deep audit findings

- Launcher currently derives `MissionAgentInstance` from logs/personas and worker session ids.
- Harness snapshot can now emit `persona_instances` and active/recent `persona_assignments`.
- Launcher bridge does not parse those fields yet.
- The current baseline idle fallback remains only for feature-disabled/no-data debugging. It must not render when Harness emits any `persona_instances` array, even an empty enabled array.

### Implementation tasks

1. Extend Launcher snapshot models to parse:

```json
"persona_instances": [...],
"persona_assignments": {"active": [...], "recent": [...]}
```

2. Extend `MissionAgentInstance` with fields:
   - `personaInstanceId`
   - `instanceMode`: `diagnostic | freeFloating | taskBound | configuredIdle | fallback`
   - `currentAssignmentId`
   - `assignmentKind`
   - `assignmentState`
   - `assignmentTitle`
   - `assignmentTaskId`
   - `activeWorkerSessionId`
   - `contextReceiptId`
   - `compressionReceiptId`
   - `lastHeartbeatAt`
3. Prefer Harness `persona_instances` over log-derived idle fallback.
4. Preserve worker/log-derived instances only as rollout fallback.
5. Add UI copy that marks `Configured Idle`, `Diagnostic`, `Free-floating`, and `Task-bound` explicitly.

### Tests

- `test/features/mission_control/mission_control_bridge_test.dart`
  - parses `persona_instances` and active/recent assignments.
- `test/features/mission_control/mission_agent_instance_test.dart`
  - derives MissionAgentInstance from Harness persona instance fields.
- `test/features/mission_control/mission_agent_instance_picker_test.dart`
  - shows Harness-backed instance IDs and assignment labels.
- `test/features/mission_control/mission_control_page_test.dart`
  - no-data fallback still works only when snapshot has no Harness instance data.

### Acceptance

- Mission Control selector uses Harness `persona_instance_id` as primary identity.
- Baseline idle shells do not appear when real Harness instances exist.
- Selecting Neko/Dev/Backend/QA shows current assignment, task binding, heartbeat, and receipt data when available.

## Stage 63C - First-Class Test Persona Action in Mission Control

### Goal

Expose the current per-persona test path directly in Mission Control.

### Deep audit findings

- `harness persona diagnose` already exists and is the correct current path for testing one persona by itself.
- Launcher `MissionControlIntentType` has no `testPersona` intent.
- `CliMissionControlActionRepository._argsForIntent()` has no persona-diagnose mapping.
- The inline Agent Instance Menu has no Test Persona affordance.

### Implementation tasks

1. Add `MissionControlIntentType.testPersona`.
2. Add factory:

```dart
MissionControlIntent.testPersona({
  required String personaId,
  required String title,
  required String message,
  int maxActions = 1,
  int maxSeconds = 240,
  required String idempotencyKey,
})
```

3. Validate nonblank `personaId`, `title`, and `message`; validate max values are positive and bounded.
4. Map intent to:

```bash
hermes harness persona diagnose <persona_id> \
  --title <title> \
  --message <message> \
  --max-actions <n> \
  --max-seconds <seconds> \
  --requested-by launcher \
  --json
```

5. Add `Test Persona` button to the inline Agent Instance Menu.
6. Use a dialog with title/message/max-actions/max-seconds fields.
7. Show pending feedback while the diagnostic runs.
8. Refresh snapshot after accepted/failed result.

### Tests

- `mission_control_actions_test.dart`
  - intent JSON includes `test_persona`, `persona_id`, `title`, `message`, `max_actions`, `max_seconds`.
  - validation rejects blanks and invalid limits.
- `mission_control_bridge_test.dart`
  - `testPersona` maps to exact `hermes harness persona diagnose` args.
- `mission_agent_instance_picker_test.dart`
  - clicking Test Persona submits callback with persona id.
- `mission_control_page_test.dart`
  - delayed action repository shows immediate pending state and disables duplicate submits.

### Acceptance

- Tony can click a persona and run a bounded diagnostic from Mission Control.
- UI copy says `Diagnostic` / `Test Persona`, not `production proof`.
- The diagnostic result appears after snapshot refresh when Stage 63A/63B are complete; before that completion the action banner still displays the CLI result and the UI labels it as a diagnostic command result.

## Stage 63D - Persona Operation History Panel

### Goal

Make diagnostic and operator-message assignments inspectable without opening raw task files.

### Deep audit findings

- `PersonaAssignmentStore` preserves assignments and summaries.
- `persona diagnose` returns `assignment_id` and `persona_instance_id` when flags are enabled.
- Mission Control has Agent Chat and logs, but no assignment history panel grouped by persona instance.

### Implementation tasks

1. In Harness snapshot, ensure recent assignments include enough fields for UI history:
   - assignment id, kind, state, title, task id, stage id, operation id, run ids, proof ids, context receipt ids, timestamps, last error.
2. In Launcher, add an `Assignment History` section under the selected Agent Instance Menu.
3. Group rows by assignment kind and state.
4. Add filters: `All`, `Diagnostics`, `Operator messages`, `Task-bound`, `Proof`, `Blocked`.
5. Add copy-safe display labels and IDs.

### Tests

- Harness snapshot test with active and completed diagnostic assignments.
- Launcher bridge test mapping recent assignments.
- Widget test for selected persona showing diagnostic and operator-message history.

### Acceptance

- Tony can see what persona tests have been run.
- Each assignment row links to run ids/proof ids/context receipts when present.
- Diagnostic history is clearly separate from production task-bound work.

## Stage 63E - Free-Floating Persona Instance Mode

### Goal

Support persona testing that does not require fake product tasks.

### Deep audit findings

- Stage 56 target says persona can exist and receive work without creating a fake product task.
- Current `persona diagnose` still creates a diagnostic task before running the turn.
- `PersonaAssignment.task_id` is optional, so the storage model already supports unattached assignments.
- `persona message` currently requires `--task`, so operator messages cannot yet target a free-floating instance.

### Implementation tasks

1. Add CLI command:

```bash
python -m hermes_cli.main harness persona instance create \
  --persona dev \
  --title "Launcher Dev sandbox" \
  --message "Test this behavior." \
  --json
```

2. Implement this command by creating a `PersonaInstance(mode="free_floating")` with id `personainst_<persona>_<12hex>` and a `PersonaAssignment(kind="free_floating_message", task_id=null, state="queued")` linked to that instance.
3. Add `harness persona instance message <persona_instance_id> --message ... --json` with no required task; it creates another `free_floating_message` assignment and does not tick.
4. Add `harness persona instance run-once <persona_instance_id> --json`; it creates a Harness-owned sandbox task, updates the active assignment to `kind="free_floating_run_once"`, runs one bounded turn, and writes `production_proof_eligible=false`.
5. Add `harness persona instance close <persona_instance_id> --reason ... --json` for `mode="free_floating"` instances only.
6. Reject close/archive on `mode="configured"` and `mode="task_bound"` instances with `error="unsupported_instance_mode"`.

### Tests

- CLI parser tests for `persona instance create/message/close`.
- Store tests for `task_id=null` assignment creation and listing by persona.
- Safety tests proving free-floating message/create does not tick by default.
- Runtime test for run-once using the sandbox-task compatibility path.

### Acceptance

- Tony can create a free-floating persona test target without creating a fake product goal.
- It appears in Mission Control as `Free-floating`.
- It can receive chat/messages without mutating mission scope.
- It cannot be accidentally archived as a production task or counted as QA proof.

## Stage 63F - Assignment-Backed One-Turn Execution

### Goal

Execute one bounded turn for diagnostic/free-floating assignments without requiring a task-first workaround.

### Deep audit findings

- `TickEngine.run_until_settled()` is currently task-id centered.
- `PersonaDiagnosticController` still creates a task and calls the task-centered engine.
- `PersonaAssignmentStore.attach_run()` and `complete()` already exist.
- `AgentRun` requires `task_id`; Stage 63 uses the sandbox-task compatibility decision below.

### Implementation decision

Do **not** make `AgentRun.task_id` optional in Stage 63. Implement one-turn execution through this fixed compatibility path:

1. Keep `persona diagnose` task-backed.
2. For free-floating run-once, create a Harness-owned sandbox task with explicit risk flags:
   - `persona_operation`
   - `free_floating_persona_instance`
   - `not_production_proof`
3. Set sandbox task fields deterministically:
   - `requested_by="persona_instance"`
   - `requires_visual_proof=false`
   - `affected_repos=[]` unless the command passes explicit redaction-safe repo labels
   - `acceptance_criteria=["Return one bounded diagnostic response for the free-floating persona instance."]` unless the command passes explicit acceptance text
4. Attach `assignment_id`, `persona_instance_id`, `instance_mode="free_floating"`, and `production_proof_eligible=false` to run progress, assignment summary, and command result.
5. Treat optional taskless `AgentRun` support as out of scope for Stage 63. It requires a separate post-Stage-63 schema migration stage, not an implementation choice inside this stage.

### Tests

- Run-once creates exactly one run.
- Run progress contains `assignment_id` and `persona_instance_id`.
- Result JSON says `production_proof=false`.
- Re-running run-once while an active same-signal assignment exists resumes or rejects instead of duplicating.

### Acceptance

- One-turn execution is bounded, visible, and honest.
- No hidden daemon loop starts.
- Assignment identity is preserved across task, run, events, and Mission Control.

## Stage 63G - Task-Bound Worker Instance Promotion

### Goal

Make normal goals show real task-bound persona instances in Mission Control.

### Deep audit findings

- Stage 48/56 define WorkerSession and PersonaAssignment linkage.
- Existing worker CLI controls operate on worker sessions, not persona instance ids.
- Launcher already maps worker controls when `workerSessionId` is present.
- The missing bridge is Mission Control using `persona_instances` and assignments as the primary roster.

### Implementation tasks

1. Ensure `TickEngine` observe-only assignment path writes:
   - `WorkerSession.current_assignment_id`
   - `AgentRun.progress.assignment_id`
   - `AgentRun.progress.persona_instance_id`
2. Ensure `PersonaInstanceStore.update_from_worker()` is called from status/snapshot after worker changes.
3. Expose task-bound assignment state in snapshot task summaries.
4. In Launcher, wire task-bound rows to existing worker controls when `active_worker_session_id` exists.
5. Disable controls with explicit copy when only configured idle/no worker session exists.

### Tests

- Harness integration test: normal task opens/updates one assignment for the selected persona in observe-only mode.
- Worker session test: current assignment id survives status/snapshot.
- Launcher widget test: task-bound instance shows worker controls; idle configured instance does not show invalid worker controls.

### Acceptance

- Real goals show Neko/Dev/Backend/QA as task-bound instances with assignment/run/heartbeat truth.
- Worker controls remain backed by worker-session CLI commands.
- No duplicate assignments are created by repeated status/snapshot reads.

## Stage 63H - Mission Control Unified Instance Controls

### Goal

Give Tony one cockpit for diagnostic, free-floating, and task-bound instance actions.

### Deep audit findings

- Current Mission Control controls are split across runtime header, task actions, worker controls, and chat.
- The picker/menu is the right local UI surface because it already opens per persona and shows association.
- Current chat send requires task-bound context. Stage 63H adds a separate free-floating composer path that maps to `harness persona instance message`, while the existing task-bound chat path continues mapping to `harness persona message --task`.

### Implementation tasks

1. In the Agent Instance Menu, render controls based on mode:
   - Configured idle: `Test Persona`, `Create Free-Floating Instance`.
   - Diagnostic/free-floating queued: `Run One Turn`, `Close`.
   - Free-floating running: `Interrupt`, `Close after turn`.
   - Task-bound active worker: existing `Nudge`, `Pause`, `Resume`, `Interrupt`, `Possess`, `Release`.
   - Completed/closed: `Archive` or `Open evidence`.
2. Add pending state per instance action, not just runtime lifecycle header actions.
3. Add safe empty-state copy when no valid action exists.
4. Keep all text under the existing Mission Control `SelectionArea`.

### Tests

- Widget matrix for each instance mode and state.
- Pending-state test with delayed action repository.
- Invalid-control tests: no worker pause button for a free-floating non-worker instance.
- Copy/selectability regression remains green.

### Acceptance

- Tony can tell which actions are valid for each instance type.
- Buttons do not appear inert during slow actions.
- Mission Control never offers worker-session controls for non-worker instances.

## Stage 63I - Evidence, Archive, and Proof Boundaries

### Goal

Keep diagnostic/free-floating evidence useful without polluting production proof.

### Deep audit findings

- `PersonaAssignment` already carries proof ids, run ids, context receipt ids, and operation id.
- `snapshot.py` has archived persona assignment summaries and persona streams for archived tasks.
- Diagnostic tasks can currently be archived through normal task archive paths.
- Free-floating unattached assignments need their own evidence retention semantics.

### Implementation tasks

1. Add evidence metadata to assignment summaries:
   - `evidence_kind`: `diagnostic | free_floating | task_bound`
   - `production_proof_eligible: bool`
   - `archive_scope`: `task | instance | assignment`
2. Archive task-bound assignment evidence with the task archive.
3. Archive diagnostic and free-floating run-once assignments with their sandbox task in Stage 63.
4. For unattached free-floating assignments, add `harness persona instance archive <assignment_or_instance_id> --json`.
5. Mission Control labels diagnostic/free-floating evidence as `Not production proof` unless a task proof gate explicitly imports it.

### Tests

- Archive test for diagnostic assignment evidence.
- Archive test for task-bound assignment evidence.
- Archive test for unattached free-floating assignment evidence.
- Launcher widget test shows `Not production proof` on diagnostics/free-floating evidence.

### Acceptance

- No evidence is lost.
- Diagnostics remain inspectable.
- Production proof boundaries are explicit and machine-readable.

## Stage 63J - Burn-In and Rollout Gates

### Goal

Prove first-class instances are enterprise-grade before making them the default Mission Control surface.

### Deep audit findings

- The Harness repo already has burn-in, swarm, lane, replay-scenario, status, snapshot, persona diagnostic, and persona assignment test surfaces that Stage 63 can reuse.
- The live profile currently has persona instance flags disabled, so rollout must include both disabled and enabled proof paths.
- Launcher Mission Control changes require widget/analyzer/macOS build proof, not only Harness unit tests, because the operator-facing failure mode is misleading UI state.
- Free-floating and diagnostic evidence require archive/proof-boundary checks before Stage 63 can be considered production-ready.

### Burn-in sequence

1. Flags disabled: standard status/snapshot/Launcher fallback remains stable.
2. Flags enabled observe-only: `persona list` shows configured idle instances with no model calls.
3. Run Neko diagnostic from CLI; verify assignment id, persona instance id, run id, and non-production proof boundary.
4. Run Launcher Dev diagnostic from CLI.
5. Run QA diagnostic from CLI.
6. Queue `persona message dev --task <task_id>` and verify it creates assignment only, no tick.
7. Enable Mission Control parsing and confirm UI uses Harness instances.
8. Use Mission Control `Test Persona` and verify diagnostic appears in assignment history.
9. Create free-floating persona instance/message after Stage 63E lands.
10. Run one normal goal and verify task-bound workers appear with correct worker controls.
11. Archive diagnostic and task-bound evidence and verify retrieval from archived task/instance history.

### Required verification commands

Harness:

```bash
python -m pytest \
  tests/agent_runtime/test_persona_assignments.py \
  tests/agent_runtime/test_persona_diagnostics.py \
  tests/agent_runtime/test_status.py \
  tests/agent_runtime/test_snapshot.py \
  tests/hermes_cli/test_harness_cli.py -q

python -m hermes_cli.main harness persona list --json
python -m hermes_cli.main harness persona diagnose dev \
  --title "Launcher Dev diagnostic" \
  --message "Return a bounded diagnostic response." \
  --max-actions 1 \
  --max-seconds 240 \
  --json
```

Launcher:

```bash
flutter test \
  test/features/mission_control/mission_control_bridge_test.dart \
  test/features/mission_control/mission_control_actions_test.dart \
  test/features/mission_control/mission_agent_instance_test.dart \
  test/features/mission_control/mission_agent_instance_picker_test.dart \
  test/features/mission_control/mission_control_page_test.dart \
  --reporter=compact

flutter analyze \
  lib/features/mission_control/data/mission_control_bridge.dart \
  lib/features/mission_control/data/mission_control_actions.dart \
  lib/features/mission_control/data/mission_agent_instance.dart \
  lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart \
  lib/features/mission_control/mission_control_page.dart
```

macOS product proof after Launcher UI changes:

```bash
flutter build macos --debug
```

### Acceptance

- Burn-in ends with no active runs, no unexplained open incidents, no duplicate active assignments, and expected repo dirty state.
- Mission Control shows every persona with correct instance mode and action affordances.
- Diagnostics/free-floating runs are never mislabeled as production proof.

## Closed-Gap Decision Matrix

Every known Stage 63 ambiguity is closed by the decisions below. Implementers must not re-open these choices inside Stage 63.

### Feature flags

- Decision: Stage 63 runs behind the existing `enterprise_worker_sessions` config tree.
- Required enabled Tony/dev-fixture values:
  - `enterprise_worker_sessions.enabled=true`
  - `enterprise_worker_sessions.mode="observe_only"`
  - `enterprise_worker_sessions.persona_instance_runtime=true`
  - `enterprise_worker_sessions.persona_assignment_store=true`
- Required disabled default values:
  - `enterprise_worker_sessions.enabled=false`
  - `enterprise_worker_sessions.persona_instance_runtime=false`
  - `enterprise_worker_sessions.persona_assignment_store=false`
- Implementation gate: disabled-config tests and enabled-config tests must both pass before Launcher consumes these fields.

### Launcher snapshot source of truth

- Decision: Launcher reads `snapshot.persona_instances` and `snapshot.persona_assignments` first.
- Decision: log-derived/baseline fallback instances are used only when `persona_instances` is absent because the feature is disabled or an older Harness produced the snapshot.
- Decision: Harness emitting `persona_instances: []` while feature-enabled makes Mission Control show a Harness-enabled empty state and forbids baseline fallback shells.
- Implementation gate: bridge fixture tests cover absent, empty-enabled, and populated-enabled snapshots.

### Test Persona action

- Decision: Mission Control `Test Persona` maps only to `hermes harness persona diagnose` in Stage 63.
- Decision: it always displays `Diagnostic` and `Not production proof` copy.
- Decision: it never calls daemon start, run-until-settled for unrelated tasks, or worker create.
- Implementation gate: action repository arg test proves exact command mapping; widget pending-state test proves duplicate clicks are disabled.

### Free-floating create/message semantics

- Decision: `harness persona instance create` creates `PersonaInstance(mode="free_floating")` with id `personainst_<persona>_<12hex>` plus a queued `PersonaAssignment(kind="free_floating_message", task_id=null)`.
- Decision: `harness persona instance message` creates another queued `free_floating_message` assignment and does not tick.
- Decision: create/message commands return `started_run=false` and must not create `AgentRun` records.
- Implementation gate: store/CLI tests assert `task_id is None`, `started_run=false`, and no new run ids.

### Free-floating run-once semantics

- Decision: `harness persona instance run-once` uses the sandbox-task compatibility path in Stage 63.
- Decision: it creates exactly one sandbox task, exactly one bounded run, and links `assignment_id` plus `persona_instance_id` through assignment summary, run progress, events, and command result.
- Decision: Stage 63 keeps `AgentRun.task_id` required. Taskless `AgentRun` is explicitly out of scope.
- Implementation gate: run-once tests assert one run, one sandbox task, no duplicate active same-signal assignments, and `production_proof_eligible=false`.

### Worker controls

- Decision: task-bound persona instances use `active_worker_session_id` for existing `harness worker` controls.
- Decision: configured idle, diagnostic, and free-floating instances cannot call worker pause/resume/possess/release unless they also expose an active worker session id.
- Decision: invalid controls are hidden by default and can appear disabled only when the UI needs explanatory copy.
- Implementation gate: widget tests cover each mode/state action matrix.

### Evidence and proof boundaries

- Decision: all diagnostic and free-floating assignments emit `production_proof_eligible=false`.
- Decision: task-bound assignments emit `production_proof_eligible=true` only when the Harness proof gate attaches an accepted proof id.
- Decision: Mission Control always displays `Not production proof` for diagnostic/free-floating evidence.
- Decision: importing diagnostic/free-floating evidence into a production task requires a separate post-Stage-63 proof-import command; Stage 63 does not implement proof import.
- Implementation gate: archive/proof-boundary tests assert metadata and UI labels.

### Archive semantics

- Decision: task-bound assignments archive with their task.
- Decision: diagnostic assignments archive with their diagnostic/sandbox task.
- Decision: free-floating message-only assignments archive through `harness persona instance archive <assignment_or_instance_id> --json`.
- Decision: configured singleton instances cannot be archived or closed.
- Implementation gate: archive tests cover task-bound, diagnostic/sandbox, free-floating message-only, and configured-instance rejection paths.

## Implementation-Ready First Slice

The required first implementation slice is **63A + 63B only**.

### Slice 1 objective

Enable Harness persona instance data in an observe-only test/config path and make Mission Control able to parse/display it without adding new mutating controls.

### Files to modify

Harness:

- `agent_runtime/runtime_config.py`
- `agent_runtime/status.py`
- `agent_runtime/snapshot.py`
- `tests/agent_runtime/test_status.py`
- `tests/agent_runtime/test_snapshot.py`
- `tests/hermes_cli/test_harness_cli.py`

Launcher:

- `lib/features/mission_control/data/mission_control_snapshot.dart`
- `lib/features/mission_control/data/mission_control_bridge.dart`
- `lib/features/mission_control/data/mission_agent_instance.dart`
- `lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart`
- `test/features/mission_control/mission_control_bridge_test.dart`
- `test/features/mission_control/mission_agent_instance_test.dart`
- `test/features/mission_control/mission_agent_instance_picker_test.dart`

### TDD order

1. Add disabled/enabled Harness status tests.
2. Make Harness status/snapshot shape pass without changing runtime behavior.
3. Add Launcher bridge fixture with `persona_instances` and `persona_assignments`.
4. Make Launcher snapshot parser pass.
5. Add MissionAgentInstance derivation test from Harness persona instance data.
6. Make picker render Harness instance mode/current assignment.
7. Run focused Harness and Launcher test commands.
8. Commit Harness changes and Launcher changes separately unless Tony requests one combined commit.

### Do not start with

- TickEngine authoritative assignment routing.
- Free-floating taskless execution.
- AgentRun schema migration.
- Mission Control mutating controls.
- 3D possession UI.

Stages 63C-63J are blocked until the first read-only visibility slice passes.

## Implementation-Ready Second Slice

Start **63C** after the read-only slice passes.

### Slice 2 objective

Expose current `persona diagnose` as `Test Persona` in Mission Control.

### Files to modify

Launcher only:

- `lib/features/mission_control/data/mission_control_actions.dart`
- `lib/features/mission_control/data/mission_control_bridge.dart`
- `lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart`
- `lib/features/mission_control/mission_control_page.dart`
- related tests under `test/features/mission_control/`

### TDD order

1. Add failing intent validation test for `MissionControlIntent.testPersona`.
2. Implement intent and serialization.
3. Add failing CLI mapping test.
4. Implement bridge args for `hermes harness persona diagnose`.
5. Add failing picker/page widget test for Test Persona action and pending state.
6. Implement UI control/dialog/callback.
7. Run focused tests, analyzer, and macOS debug build.

### Acceptance

- Tony can test one persona from Mission Control using the already-supported Harness diagnostic command.
- The action is visibly bounded and non-production.
- Snapshot refresh surfaces the resulting diagnostic assignment when flags are enabled.

## Final Definition of Done

Stage 63 is complete when:

- [ ] Harness persona instances and assignments are visible in status/snapshot with flags enabled and harmless with flags disabled.
- [ ] Mission Control uses Harness persona instance identity before fallback persona shells.
- [ ] Mission Control exposes Test Persona backed by `harness persona diagnose`.
- [ ] Mission Control shows diagnostic/operator-message/task-bound assignment history per persona.
- [ ] Free-floating persona instance create/message/close exists and does not tick by default.
- [ ] One-turn free-floating execution is bounded, evented, and marked non-production.
- [ ] Task-bound worker instances map to existing worker session controls.
- [ ] Evidence/archive/proof boundaries prevent diagnostic/free-floating output from being mistaken for production proof.
- [ ] Burn-in covers CLI diagnostics, Mission Control Test Persona, free-floating instance lifecycle, task-bound worker controls, and archive retrieval.
