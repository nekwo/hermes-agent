# Stage 64 - Callable Harness Capabilities

## Goal

Make every useful Mission Control operation callable through a typed, redaction-safe capability envelope while preserving the existing Agent Runtime Harness brainstem.

Tony must be able to call the full Harness logic from Mission Control and related clients:

- queue a task, persona message, or free-floating persona-instance message;
- run one tick, run until settled, or run one bounded persona turn;
- steer workers, runs, lanes, daemon lifecycle, and task recovery;
- close/archive assignments and tasks;
- refresh authoritative readback from `snapshot`, `observe`, `status`, `task show`, `run show`, `worker show`, `persona show`, and `proof list`;
- do all of that without Launcher or another UI rebuilding queueing, steering, scheduling, proof, or state-machine logic.

Stage 64 is an adapter/capability-projection stage. It **does not** create a new runtime.

```text
Mission Control UI
  -> typed capability call
  -> existing `hermes harness ...` CLI command
  -> existing Harness stores/state machine/ticker/daemon/proof gates
  -> authoritative Harness readback refresh
```

## Non-Negotiable Product Stance

1. **Do not rebuild Harness logic.** Launcher and future clients must not implement a second queue, second worker steering system, second scheduler, second daemon, second assignment store, or direct JSON-store mutation path.
2. **Harness CLI remains the write boundary.** Every Stage 64 write-capability maps to a currently audited `hermes harness ... --json` command. Stage 64 adds a generic call envelope, and that envelope dispatches only to the existing CLI/controller functions listed in this document.
3. **Harness read models remain authoritative.** After any call, Mission Control refreshes through official read APIs (`snapshot`, `observe`, `status`, `show`, `history`, `proof list`) rather than trusting the submit response as final state.
4. **Capabilities describe existing operations.** A capability descriptor is UI/schema metadata plus a command mapping. It is not a workflow engine.
5. **No arbitrary shell.** Generic capability calls are whitelisted by `capability_id`; no user-provided command names, executable names, or unvalidated flags may pass through.
6. **Queue and run are separate.** Queueing a persona/task message must not silently execute a model call. Running/ticking must be a separate explicit capability unless the user chooses a combined capability that is named as such.
7. **Task-bound proof remains task-bound.** Free-floating and diagnostic calls remain non-production proof unless a separate existing proof gate imports/accepts evidence for a real task.
8. **Feature flags remain honored.** Persona instance/assignment capabilities are hidden or disabled when `persona_instance_runtime` or `persona_assignment_store` are disabled.

## Current Deep Audit - 2026-06-14

### Existing Harness CLI surfaces

Audited file: `hermes_cli/harness.py`.

The current CLI already exposes the core operations Stage 64 needs:

- Task/goal flow:
  - `harness goal run`
  - `harness task create/list/show/history/cancel/unblock/archive-ready/archive`
- Execution:
  - `harness tick [--task <task_id>]`
  - `harness run-until-settled [--task <task_id>] --max-actions ... --max-seconds ...`
  - `harness daemon start/status/stop/foreground/run-once`
- Run controls:
  - `harness run show/cancel/approve`
- Worker controls:
  - `harness worker list/show`
  - `harness worker pause/resume/interrupt/nudge/possess/release`
- Persona and persona instance operations:
  - `harness persona list/show/assignments`
  - `harness persona message <persona_id> --task <task_id> --message ...`
  - `harness persona diagnose <persona_id> --title ... --message ...`
  - `harness persona instance create/message/run-once/close/archive`
- Lane controls:
  - `harness lane list/show/pause/park/resume/drain`
- Readback, proof, issue, incident, migration, config:
  - `harness status/health/observe/snapshot/config show/migrate --check`
  - `harness proof list <task_id>`
  - `harness issue list/show`
  - `harness incident list/show/close`

### Existing Harness implementations

Audited files:

- `hermes_cli/harness.py`
- `agent_runtime/persona_assignments.py`
- `agent_runtime/worker_sessions.py`
- `agent_runtime/snapshot.py`
- `agent_runtime/status.py`
- `agent_runtime/observability.py`

Findings:

- `PersonaAssignmentStore.create_or_resume()` already dedupes active assignments via `signal_hash` and owns assignment persistence.
- `harness persona message` validates task existence, derives persona instances from configured personas/workers, and queues `PersonaAssignment(kind="operator_message")` against the task/stage. It does not tick.
- `harness persona instance message` queues `PersonaAssignment(kind="free_floating_message")` with `task_id=null`, sets `PersonaInstance.mode="free_floating"`, and returns `next_expected` pointing to `persona instance run-once`.
- `harness persona instance run-once` uses `PersonaDiagnosticController` + `TickEngine` + `GPTPersonaRuntime` to run one bounded sandbox task/turn. It attaches resulting run ids to the seed assignment and completes the free-floating assignment. This is an existing execution path, not a separate Launcher runtime.
- `harness worker pause/resume/interrupt/nudge/possess/release` dispatches to `WorkerSessionStore` methods and returns `worker_session_summary()`.
- `harness run cancel` updates matching active worker sessions through `WorkerSessionStore.update_after_run()`.
- `harness run approve` closes `run_budget_exceeded` incidents and returns `next_expected="run harness tick to continue same session"`.
- `harness observe` builds its read model from `TaskStore`, `RunStore`, `IncidentStore`, `WorkerSessionStore`, and proof/event surfaces. Stage 64 should extend/read this rather than introduce a separate UI-only capability source.

### Existing Launcher Mission Control surfaces

Audited files in `EterniaLauncher`:

- `lib/features/mission_control/data/mission_control_actions.dart`
- `lib/features/mission_control/data/mission_control_bridge.dart`
- `lib/features/mission_control/data/mission_control_snapshot.dart`
- `lib/features/mission_control/data/mission_agent_instance.dart`
- `lib/features/mission_control/agent_chat/mission_agent_chat_adapter.dart`
- `lib/features/mission_control/agent_chat/mission_agent_chat_panel.dart`
- `lib/features/mission_control/mission_control_page.dart`

Findings:

- Launcher currently uses a bespoke `MissionControlIntentType` enum for each supported action.
- `CliMissionControlActionRepository._argsForIntent()` maps each bespoke intent to a concrete `hermes harness ...` command.
- Current supported Launcher write mappings include:
  - `createGoal -> harness task create`
  - `bootRuntime/fullTickMode -> harness daemon start`
  - `manualTickMode/shutOffRuntime -> harness daemon stop`
  - `runNextTick -> harness tick --json`
  - `archiveReadyGoal -> harness task archive <goalId>`
  - `sendAgentMessage -> harness persona message <persona> --task <task>`
  - `runPersonaInstanceMessage -> harness persona instance run-once <instance>`
  - `testPersona -> harness persona diagnose <persona>`
  - worker controls -> `harness worker ...`
  - run controls -> `harness run cancel/approve`
- `pauseGoal` and `requestQa` still return unsupported/null in the live bridge.
- `mission_agent_chat_adapter.dart` currently decides send behavior procedurally:
  - task-bound instance -> queue task-backed persona message;
  - free-floating instance -> run one bounded persona instance turn;
  - fallback/idle instance -> diagnostic persona turn.
- `missionAgentActionSpecs()` hardcodes Test Persona, Nudge, Pause, Resume, Cancel Run, and Approve Run based on `workerSessionId`/`runId`/supported persona IDs.
- The current approach works but is not scalable: every new Harness operation requires a new enum case, factory, validation switch, bridge switch entry, UI action constant, and tests.

### Existing test surfaces to extend

Launcher tests already cover the current adapter bridge:

- `test/features/mission_control/mission_control_actions_test.dart`
- `test/features/mission_control/mission_control_bridge_test.dart`
- `test/features/mission_control/mission_agent_chat_adapter_test.dart`
- `test/features/mission_control/mission_agent_chat_panel_test.dart`
- `test/features/mission_control/mission_control_page_test.dart`
- `test/features/mission_control/mission_control_empty_state_test.dart`

Harness tests to extend should live near existing runtime/CLI tests:

- `tests/agent_runtime/test_snapshot.py`
- `tests/agent_runtime/test_status.py`
- `tests/agent_runtime/test_worker_sessions.py`
- `tests/agent_runtime/test_persona_assignments.py`
- `tests/hermes_cli/test_harness_cli.py` or the existing CLI test file matching current repo naming.

## Target Architecture

### Capability descriptor

A capability is a redaction-safe descriptor for an existing Harness operation.

Minimum schema v1:

```json
{
  "capability_id": "persona.instance.message",
  "target_kind": "persona_instance",
  "target_id": "personainst_dev",
  "label": "Queue Message",
  "group": "queue",
  "description": "Queue a message to a free-floating persona instance without ticking.",
  "enabled": true,
  "disabled_reason": null,
  "danger_level": "normal",
  "execution_semantics": "queues_only",
  "readback": ["snapshot", "persona.show", "persona.assignments"],
  "args_schema": {
    "type": "object",
    "required": ["message"],
    "properties": {
      "message": {"type": "string", "minLength": 1, "maxLength": 4000},
      "title": {"type": "string", "default": "Free-floating operator message"}
    },
    "additionalProperties": false
  }
}
```

Allowed `group` values for Stage 64:

- `queue`
- `run`
- `steer`
- `lifecycle`
- `archive`
- `observe`

Allowed `execution_semantics` values:

- `read_only`
- `queues_only`
- `bounded_execution`
- `daemon_lifecycle`
- `control_state_change`
- `archive_or_close`

Allowed `danger_level` values:

- `normal`
- `warning`
- `destructive`

### Capability call intent

Generic write envelope:

```json
{
  "intent": "call_harness_capability",
  "capability_id": "persona.instance.message",
  "target_kind": "persona_instance",
  "target_id": "personainst_dev",
  "args": {
    "message": "Please inspect this state.",
    "title": "Operator message"
  },
  "requested_by": "launcher",
  "idempotency_key": "agent-chat-message-..."
}
```

Implementation decision:

- Add this generic envelope alongside existing bespoke intents first.
- Existing bespoke intents remain as compatibility wrappers until the UI migrates.
- Generic calls must be validated against a static whitelist/mapping before dispatch.
- The generic envelope does not include a caller-controlled `readback_hint` in Stage 64. The dispatcher/provider owns final readback behavior from the capability registry.

### Capability command mapping

The mapping is static and checked in. It must never accept arbitrary command strings.

Example mapping entry:

```dart
const HarnessCapabilityCommand(
  capabilityId: 'persona.instance.message',
  targetKind: HarnessCapabilityTargetKind.personaInstance,
  commandTemplate: <String>[
    'harness', 'persona', 'instance', 'message', '{targetId}',
    '--message', '{args.message}',
    '--title', '{args.title}',
    '--requested-by', 'launcher',
    '--json',
  ],
);
```

The concrete implementation may use strongly typed Dart classes rather than string templates; the invariant is that every capability id has one explicit, tested command builder.

## Stage 64A - Capability Contract and Whitelist

### Goal

Introduce the typed capability/call contract without changing runtime behavior.

### Deep audit findings

- Current Launcher intent model is bespoke and grows one enum/factory/switch branch per Harness action.
- Current Harness CLI already has stable command groups; Stage 64A only models them.
- Harness does not yet emit `capabilities` in `snapshot`/`observe`; Stage 64 starts with a static local Launcher registry generated from current CLI command support, then Stage 64G adds Harness-emitted descriptors with the same ids.
- A static registry is safer than dynamic shell discovery because command-line help text is not a stable machine contract.

### Implementation-ready decisions

- Create the first registry in Launcher, not Harness, to avoid touching the runtime brainstem before the UI adapter proves useful.
- Use repo-local source files, not generated build_runner code.
- Keep compatibility with all existing `MissionControlIntentType` values.
- The first registry must cover only commands already supported by Launcher or audited current Harness commands.

### Files

Launcher:

- Create: `lib/features/mission_control/data/harness_capability.dart`
- Create: `lib/features/mission_control/data/harness_capability_registry.dart`
- Modify: `lib/features/mission_control/data/mission_control_actions.dart`
- Test: `test/features/mission_control/harness_capability_registry_test.dart`
- Test: `test/features/mission_control/mission_control_actions_test.dart`

Harness docs only in this stage:

- Modify: `docs/agent-runtime-harness/64-callable-harness-capabilities.md`

### Data contract

Add Dart types:

```dart
enum HarnessCapabilityGroup { queue, run, steer, lifecycle, archive, observe }
enum HarnessCapabilityDanger { normal, warning, destructive }
enum HarnessCapabilityExecutionSemantics {
  readOnly,
  queuesOnly,
  boundedExecution,
  daemonLifecycle,
  controlStateChange,
  archiveOrClose,
}

class HarnessCapabilitySpec {
  const HarnessCapabilitySpec({
    required this.id,
    required this.targetKind,
    required this.label,
    required this.group,
    required this.executionSemantics,
    this.description,
    this.danger = HarnessCapabilityDanger.normal,
    this.requiredArgs = const <String>[],
    this.defaultArgs = const <String, Object?>{},
  });

  final String id;
  final String targetKind;
  final String label;
  final HarnessCapabilityGroup group;
  final HarnessCapabilityExecutionSemantics executionSemantics;
  final String? description;
  final HarnessCapabilityDanger danger;
  final List<String> requiredArgs;
  final Map<String, Object?> defaultArgs;
}
```

Add generic intent fields to `MissionControlIntent`:

- `capabilityId`
- `targetKind`
- `targetId`
- `capabilityArgs`

Add factory:

```dart
factory MissionControlIntent.callHarnessCapability({
  required String capabilityId,
  required String targetKind,
  required String targetId,
  required Map<String, Object?> args,
  required String idempotencyKey,
})
```

Add enum value:

```dart
callHarnessCapability
```

### Initial static registry

Implement these capability ids first:

Queue:

- `task.create`
- `persona.message_task`
- `persona.instance.message`

Run:

- `task.tick`
- `task.run_until_settled`
- `persona.instance.run_once`
- `persona.diagnose`

Steer:

- `worker.nudge`
- `worker.pause`
- `worker.resume`
- `worker.interrupt`
- `worker.possess`
- `worker.release`
- `run.cancel`
- `run.approve`
- `task.unblock`
- `lane.pause`
- `lane.park`
- `lane.resume`
- `lane.drain`

Lifecycle:

- `daemon.start`
- `daemon.stop`
- `daemon.run_once`

Archive:

- `persona.instance.close`
- `persona.instance.archive`
- `task.archive`
- `task.archive_ready`

Observe/read descriptors may exist for UI grouping but must not be submitted through the write repository in Stage 64A.

### Tests

Add tests that prove:

- every registry id is unique;
- every id has non-empty target kind, label, group, and execution semantics;
- write capabilities define required args where needed;
- `callHarnessCapability` rejects blank `capabilityId`, `targetKind`, `targetId`, and `idempotencyKey`;
- `callHarnessCapability` rejects missing required args for the selected capability;
- `callHarnessCapability` rejects unknown args when a capability declares a closed schema.

### Acceptance criteria

- No existing Launcher action behavior changes.
- Static registry compiles and tests pass.
- Unknown capabilities are impossible to submit successfully.
- There is no arbitrary command string in the call envelope.

## Stage 64B - Generic Capability Dispatcher Over Existing CLI

### Goal

Map generic capability calls to existing `hermes harness ...` CLI commands in `CliMissionControlActionRepository`.

### Deep audit findings

- `CliMissionControlActionRepository._argsForIntent()` already centralizes command mapping.
- The current mapping is a `switch` over bespoke `MissionControlIntentType` values.
- Command execution already goes through `MissionControlCommandRunner`, so tests can capture arguments without running real processes.
- Result handling currently returns a generic accepted/failed message and does not parse success JSON for readback hints.

### Implementation-ready decisions

- Add `_argsForCapabilityCall(intent)` and call it from `_argsForIntent` when `intent.intent == callHarnessCapability`.
- Use explicit command builders per capability id, not string interpolation templates that accept arbitrary names.
- Keep `executable = 'hermes'` unchanged.
- Do not parse arbitrary stdout in Stage 64B except to clip safe error details as current code does.
- Add JSON parsing/readback hints later in Stage 64F.

### Files

Launcher:

- Modify: `lib/features/mission_control/data/mission_control_bridge.dart`
- Modify: `lib/features/mission_control/data/harness_capability_registry.dart`
- Test: `test/features/mission_control/mission_control_bridge_test.dart`
- Test: `test/features/mission_control/harness_capability_registry_test.dart`

### Command mapping matrix

Queue:

- `task.create`
  - `harness task create --title <title> --description <description> --requested-by launcher --json`
- `persona.message_task`
  - `harness persona message <persona_id> --task <task_id> --message <message> --title <title> --requested-by launcher --json`
- `persona.instance.message`
  - `harness persona instance message <persona_instance_id> --message <message> --title <title> --requested-by launcher --json`

Run:

- `task.tick`
  - `harness tick --task <task_id> --json` when target kind is `task`, otherwise `harness tick --json`
- `task.run_until_settled`
  - `harness run-until-settled --task <task_id> --max-actions <n> --max-seconds <seconds> --json`
- `persona.instance.run_once`
  - `harness persona instance run-once <persona_instance_id> --title <title> --message <message> --max-actions <n> --max-seconds <seconds> --requested-by launcher --json`
- `persona.diagnose`
  - `harness persona diagnose <persona_id> --title <title> --message <message> --max-actions <n> --max-seconds <seconds> --requested-by launcher --json`

Steer:

- `worker.nudge`
  - `harness worker nudge <worker_session_id> --note <note> --actor launcher --json`
- `worker.pause|resume|interrupt|release`
  - `harness worker <verb> <worker_session_id> --reason <reason> --actor launcher --json`
- `worker.possess`
  - `harness worker possess <worker_session_id> --actor launcher --lease-seconds <seconds> --json`
- `run.cancel`
  - `harness run cancel <run_id> --reason <reason> --json`
- `run.approve`
  - `harness run approve <run_id> --json`
- `task.unblock`
  - `harness task unblock <task_id> --reason <reason> --state <state> [--rescope] [--foreground] --json`
- `lane.pause|park|resume|drain`
  - `harness lane <verb> <lane_id> --reason <reason> --json`

Lifecycle:

- `daemon.start`
  - `harness daemon start --json`
- `daemon.stop`
  - `harness daemon stop --json`
- `daemon.run_once`
  - `harness daemon run-once --json`

Archive:

- `persona.instance.close`
  - `harness persona instance close <persona_instance_id> --reason <reason> --requested-by launcher --json`
- `persona.instance.archive`
  - `harness persona instance archive <persona_instance_id> --reason <reason> --requested-by launcher --json`
- `task.archive`
  - `harness task archive <task_id> --json`
- `task.archive_ready`
  - `harness task archive-ready --json`

### Tests

For each capability group, add at least one focused bridge test that captures the exact argument vector.

Required exact-command tests:

- `persona.instance.message` maps to `harness persona instance message`, not `run-once`.
- `persona.instance.run_once` maps to `harness persona instance run-once`.
- `worker.nudge` maps to `harness worker nudge` with `--note` and `--actor launcher`.
- `task.unblock` maps state, `--rescope`, and `--foreground` only when requested.
- `daemon.start/stop/run_once` map to existing daemon subcommands.
- unknown capability returns rejected unsupported result and does not call runner.

### Acceptance criteria

- Existing bespoke intent tests still pass.
- Generic capability bridge tests pass.
- Every generic capability dispatch maps to an existing Harness CLI path.
- No capability dispatch mutates Launcher state directly.

## Stage 64C - Capability Projection on Mission Control Targets

### Goal

Attach available capability descriptors to the target objects Mission Control already renders: persona instances, worker sessions, runs, tasks/goals, lanes, and daemon/runtime state.

### Deep audit findings

- `MissionAgentInstance` already has enough identity fields to infer many capabilities: `instanceId`, `personaId`, `taskId`, `workerSessionId`, `runId`, `instanceMode`, `currentAssignmentId`.
- `missionAgentActionSpecs()` currently hardcodes action buttons from those fields.
- `mission_control_snapshot.dart` already carries runtime, daemon, goals, bridge, agent logs, agent instances, counts, and observed events; capability lists are not first-class yet.
- Harness snapshot currently emits command-ready identifiers but not a formal capability array.

### Implementation-ready decisions

- Stage 64C computes capabilities in Launcher from snapshot fields and the static registry.
- Do not require Harness to emit a new capabilities field yet.
- Add concrete `capabilities` fields to Launcher models for persona instances, selected task/goal action view-models, daemon/runtime action view-models, and capability action rendering.
- Keep readback-only capabilities internal to repository/provider code, not rendered as user buttons unless useful.

### Files

Launcher:

- Modify: `lib/features/mission_control/data/mission_agent_instance.dart`
- Modify: `lib/features/mission_control/data/mission_control_snapshot.dart`
- Modify: `lib/features/mission_control/agent_chat/mission_agent_chat_adapter.dart`
- Modify: `lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart` if instance menu actions need descriptor-driven rendering.
- Test: `test/features/mission_control/mission_agent_chat_adapter_test.dart`
- Test: `test/features/mission_control/mission_agent_instance_picker_test.dart`

### Projection rules

Persona instance:

- Always eligible when supported persona id and feature data is present:
  - `persona.diagnose`
- If `instanceMode == freeFloating` and no `taskId`:
  - `persona.instance.message`
  - `persona.instance.run_once`
  - `persona.instance.close` if `currentAssignmentId` exists
  - `persona.instance.archive` if `currentAssignmentId` exists
- If task-bound and `taskId` exists:
  - `persona.message_task`
  - `task.tick`
  - `task.run_until_settled`
- If `workerSessionId` exists:
  - `worker.nudge`
  - `worker.pause`
  - `worker.resume`
  - `worker.interrupt`
  - `worker.possess`
  - `worker.release`
- If `runId` exists:
  - `run.cancel`
  - `run.approve`

Task/goal:

- active/nonterminal task:
  - `task.tick`
  - `task.run_until_settled`
  - `task.unblock` only for blocked or intervention states
- ready/done terminal task:
  - `task.archive`
- global/ready list:
  - `task.archive_ready`

Daemon/runtime:

- offline/manual:
  - `daemon.start`
  - `daemon.run_once`
- running/full tick:
  - `daemon.stop`
  - `daemon.run_once` is disabled while the daemon is already running and enabled while offline/manual, with disabled reason `Stop the running daemon before running one bounded daemon loop.`

Lane:

- lane present:
  - `lane.pause`, `lane.park`, `lane.resume`, `lane.drain` based on current lane state.

### Tests

Add projection tests proving:

- task-bound persona shows queue/tick/settle/worker/run capabilities as applicable;
- free-floating persona shows queue message and run one turn separately;
- unsupported persona id hides message/diagnose capabilities;
- missing worker id disables worker controls with a reason instead of creating invalid intents;
- run id controls appear only when a run id exists;
- daemon controls reflect runtime state.

### Acceptance criteria

- UI action availability is derived from target capabilities, not duplicated `if` ladders in multiple widgets.
- Queue and run remain visibly separate actions.
- The user can see why a capability is unavailable.

## Stage 64D - Agent Chat Uses Capabilities Instead of Procedural Send Routing

### Goal

Replace agent-chat send/action branching with capability-driven routing while preserving current behavior.

### Deep audit findings

- `missionAgentSendIntent()` currently chooses between `sendAgentMessage`, `runPersonaInstanceMessage`, and `testPersona` directly.
- The current behavior fixed the immediate no-goal persona problem, but it still bakes routing policy into chat adapter code.
- Tony wants instances callable through function properties, not a UI wrapper around `run-once`.

### Implementation-ready decisions

- Agent chat composer should select a default send capability from the instance capabilities.
- Default send priority:
  1. task-bound instance: `persona.message_task` (queue only)
  2. free-floating instance: `persona.instance.message` (queue only) plus show a secondary `Run One Turn` action
  3. fallback/configured idle supported persona: `persona.diagnose`
- Do **not** default free-floating composer send directly to `run-once` after this stage. That was an emergency bridge, not the desired callable-function model.
- Add an explicit `Run One Turn` button/action for immediate execution.

### Files

Launcher:

- Modify: `lib/features/mission_control/agent_chat/mission_agent_chat_adapter.dart`
- Modify: `lib/features/mission_control/agent_chat/mission_agent_chat_panel.dart`
- Modify: shared chat action model only if current `AgentChatActionSpec` cannot carry capability ids/arg prompts.
- Test: `test/features/mission_control/mission_agent_chat_adapter_test.dart`
- Test: `test/features/mission_control/mission_agent_chat_panel_test.dart`

### UX copy

Task-bound send accepted:

```text
Message queued for the task-backed Harness worker. It will reply after the next Harness tick/run.
```

Free-floating queue accepted:

```text
Message queued for this free-floating persona instance. Use Run One Turn to execute it now.
```

Run one turn accepted:

```text
Persona sandbox turn started. Refresh Mission Control to view the reply/proof event.
```

Diagnostic accepted:

```text
Persona diagnostic turn started. Refresh Mission Control to view the reply/proof event.
```

### Tests

- task-bound composer submit creates `callHarnessCapability(persona.message_task)`;
- free-floating composer submit creates `callHarnessCapability(persona.instance.message)`, not `persona.instance.run_once`;
- free-floating `Run One Turn` action creates `callHarnessCapability(persona.instance.run_once)`;
- fallback idle supported persona creates `callHarnessCapability(persona.diagnose)`;
- unsupported persona disables composer with truthful copy;
- accepted messages match queue/run semantics.

### Acceptance criteria

- Chat send no longer hides run semantics inside the composer.
- Free-floating instances are callable through function/capability properties.
- Current task-bound queue behavior is preserved.

## Stage 64E - Full Queue / Run / Steer UI Surfaces

### Goal

Render the full useful Harness operation set in Mission Control without turning the UI into a generic workflow designer.

### Deep audit findings

- Mission Control already has runtime controls, goal cards/details, agent instance picker, agent chat panel, live terminal/run inspector, and Mission Office.
- Tony wants Mission Control to stay a Harness cockpit, not an over-abstract generic runtime adapter.
- Current controls are scattered: top runtime controls, goal actions, chat actions, and bridge actions.

### Implementation-ready decisions

- Add a small capability action group component, not a large new dashboard.
- Keep groups contextual to selected target:
  - selected mission: Queue/Run/Archive/Recovery;
  - selected persona instance: Queue/Run/Steer/Close;
  - selected worker/run: Steer/Approve/Cancel;
  - runtime strip: Daemon lifecycle.
- Do not introduce drag/drop, columns, workflow builder, or generic command palette.

### Files

Launcher:

- Create: `lib/features/mission_control/capabilities/mission_capability_action_group.dart`
- Create: `lib/features/mission_control/capabilities/mission_capability_form.dart` if arguments need forms beyond one message/reason.
- Modify: `lib/features/mission_control/mission_control_page.dart`
- Modify: `lib/features/mission_control/agent_chat/mission_agent_chat_panel.dart`
- Test: `test/features/mission_control/mission_capability_action_group_test.dart`
- Test: `test/features/mission_control/mission_control_page_test.dart`

### UI rules

- Button labels come from `HarnessCapabilitySpec.label`.
- Group headings use `Queue`, `Run`, `Steer`, `Lifecycle`, `Archive`.
- Dangerous actions use destructive styling.
- Disabled actions show exact disabled reason.
- Any action requiring free-form args opens a compact form/dialog.
- Long-running lifecycle actions show immediate in-progress state.

### Tests

- selected free-floating persona shows Queue Message, Run One Turn, Close/Archive when assignment exists;
- selected task-bound worker shows Queue Message, Nudge, Pause, Resume, Interrupt, Possess, Release, Cancel Run/Approve Run when ids exist;
- selected active mission shows Run Tick and Run Until Settled;
- runtime strip shows Start/Stop/Run Once with progress feedback;
- unsupported or missing-id actions are disabled, not hidden silently when the missing id is relevant to user understanding.

### Acceptance criteria

- Tony can reach queue, run, and steer operations from the selected object without opening a terminal.
- The UI remains Mission Control-specific and compact.
- Every rendered action maps to one tested capability id.

## Stage 64F - Authoritative Readback After Calls

### Goal

After any capability call, refresh the right Harness read models and display evidence of what changed.

### Deep audit findings

- Current `CliMissionControlActionRepository.submitIntent()` returns accepted/failed based only on process exit code and a generic safe message.
- `MissionControlPage` already refreshes the snapshot after action acceptance in several paths.
- Harness CLI success JSON often includes useful ids: `assignment_id`, `persona_instance_id`, `task_id`, `run_id`, `closed_assignment_ids`, `next_expected`, worker summary state, run state.

### Implementation-ready decisions

- Parse stdout JSON only for known/safe fields.
- Keep raw stdout out of UI unless explicitly redaction-safe and clipped.
- Add `MissionControlActionResult.metadata` only with whitelisted scalar/list fields.
- Repository/provider owns refresh; widgets should not manually call CLI read commands.

### Files

Launcher:

- Modify: `lib/features/mission_control/data/mission_control_actions.dart`
- Modify: `lib/features/mission_control/data/mission_control_bridge.dart`
- Modify: `lib/features/mission_control/state/mission_control_provider.dart`
- Modify: `lib/features/mission_control/state/mission_run_detail_provider.dart` if run detail refresh is needed.
- Test: `test/features/mission_control/mission_control_bridge_test.dart`
- Test: `test/features/mission_control/mission_control_provider_test.dart`

### Safe metadata allowlist

Allow these keys from submit JSON:

- `ok`
- `task_id`
- `run_id`
- `run_ids`
- `assignment_id`
- `closed_assignment_ids`
- `persona_id`
- `persona_instance_id`
- `worker_session_id`
- `lane_id`
- `state`
- `kind`
- `stop_reason`
- `next_expected`
- `production_proof_eligible`
- `evidence_kind`
- `archive_scope`
- `closed_incidents`

Reject or ignore everything else for display.

### Readback matrix

- `task.create` -> refresh `snapshot` and select the returned `task_id` when the CLI returns it; otherwise keep the current selection and show accepted metadata.
- `persona.message_task` -> refresh `snapshot`, `task show --events`, `persona assignments`.
- `persona.instance.message` -> refresh `snapshot`, `persona show`, `persona assignments`.
- `persona.instance.run_once` -> refresh `snapshot`, `run show` for returned run ids, `persona assignments`.
- `worker.*` -> refresh `snapshot`, `worker show` if still present.
- `run.cancel|approve` -> refresh `snapshot`, `run show`.
- `daemon.*` -> refresh `daemon status` and `snapshot`.
- `task.archive|archive_ready` -> refresh `snapshot`.

### Tests

- accepted result preserves safe metadata and drops unknown keys;
- failed result clips stdout/stderr and does not expose raw large output;
- provider refreshes snapshot after any accepted capability call;
- run-related result triggers run detail refresh for returned `run_id` or the first returned `run_ids` entry;
- persona assignment result surfaces assignment id in safe message or metadata.

### Acceptance criteria

- UI state after a call is grounded in Harness readback.
- Operator sees the id/evidence needed to understand the result.
- No secrets/raw control data are surfaced.

## Stage 64G - Harness-Emitted Capabilities in Snapshot/Observe

### Goal

Move capability availability from Launcher inference to Harness-emitted descriptors after Stages 64A-F are green. Stage 64G is required for the full Stage 64 implementation, not optional.

### Deep audit findings

- Harness already knows feature flags, configured personas, worker sessions, runs, tasks, lanes, daemon state, incidents, and assignments.
- Harness can compute more accurate availability than Launcher for feature-gated or state-sensitive operations.
- Emitting descriptors from Harness risks freezing UI-specific concepts into runtime unless the schema is carefully neutral.

### Implementation-ready decisions

- Add a neutral `capabilities` section to both `snapshot --json` and `observe --json` after Stage 64A-F tests pass.
- Harness-emitted descriptors must use the same `capability_id` strings as the Launcher registry.
- Launcher must treat Harness descriptors as authoritative for enabled/disabled/disabled_reason while still using its static command builders for dispatch.
- Launcher accepts legacy Harness snapshots without descriptors by using the Stage 64C local projection; when Harness descriptors are present, descriptor enabled/disabled state is authoritative.

### Files

Hermes Harness:

- Create: `agent_runtime/capabilities.py`
- Modify: `agent_runtime/snapshot.py`
- Modify: `agent_runtime/observability.py`
- Test: `tests/agent_runtime/test_capabilities.py`
- Test: `tests/agent_runtime/test_snapshot.py`
- Test: `tests/agent_runtime/test_observability.py`

Launcher:

- Modify: `lib/features/mission_control/data/mission_control_snapshot.dart`
- Modify: `lib/features/mission_control/data/harness_capability_registry.dart`
- Test: `test/features/mission_control/mission_control_bridge_test.dart`
- Test: `test/features/mission_control/mission_control_page_test.dart`

### Harness descriptor scope

Harness should emit descriptors for these target objects only:

- daemon/runtime summary;
- each active/open task;
- each worker session;
- each run where controls are possible;
- each persona instance;
- each lane.

Descriptor should not include command args or shell details. It should include:

- `capability_id`
- `target_kind`
- `target_id`
- `enabled`
- `disabled_reason`
- `feature_flag` if disabled by feature gate
- `danger_level`
- `execution_semantics`

### Tests

- disabled persona-instance flags emit no enabled persona-instance write capabilities;
- enabled free-floating persona emits queue/run/close/archive capabilities according to assignment state;
- task-bound worker emits worker controls only when `worker_session_id` exists;
- run approve appears only for runs waiting on approval/continuation;
- daemon start/stop reflects daemon state;
- snapshot remains backward-compatible when `capabilities` is absent or empty.

### Acceptance criteria

- Harness can explain why an operation is unavailable.
- Launcher no longer has to infer all state-sensitive capability availability.
- CLI write path remains unchanged.

## Stage 64H - Compatibility Cleanup and Deprecation

### Goal

Retire duplicated bespoke action plumbing only after capability-based behavior has parity.

### Deep audit findings

- Existing bespoke intents are covered by tests and should not be deleted early.
- Several UI tests expect current accepted messages and exact command arguments.
- A premature deletion would risk breaking working Mission Control controls.

### Implementation-ready decisions

- Keep bespoke factories as wrappers for at least one release/stage.
- Migrate UI call sites first.
- Then migrate tests to assert capability ids plus exact existing CLI command vectors.
- Delete only unreachable duplicate action constants after `git grep` proves no usages.

### Files

Launcher:

- Modify: `lib/features/mission_control/data/mission_control_actions.dart`
- Modify: `lib/features/mission_control/data/mission_control_bridge.dart`
- Modify: `lib/features/mission_control/agent_chat/mission_agent_chat_adapter.dart`
- Modify tests under `test/features/mission_control/`.

### Tests

- all old bespoke intent factories produce either the same command args or a capability wrapper with the same command args;
- no UI test loses coverage for queue, run, steer, lifecycle, archive;
- `git grep` for removed action constants returns no production usages;
- full Mission Control test suite remains green.

### Acceptance criteria

- Mission Control behavior is unchanged or clearer to the operator.
- Code has fewer one-off switch cases.
- All Harness command paths remain existing CLI calls.

## Implementation Sequence

Use this exact order:

1. Stage 64A: data model + static registry + validation tests.
2. Stage 64B: generic dispatcher + exact command-vector tests.
3. Stage 64C: target capability projection + model tests.
4. Stage 64D: agent chat migration; free-floating send queues by default, explicit Run One Turn executes.
5. Stage 64E: contextual UI action groups.
6. Stage 64F: safe result metadata + readback refresh.
7. Stage 64G: Harness-emitted descriptors in `snapshot` and `observe`.
8. Stage 64H: compatibility cleanup.

Do not start Stage 64G before Stages 64A-F pass. Harness-emitted descriptors are required before Stage 64 is considered fully implemented, but Stages 64A-F provide the safe migration path and preserve legacy snapshot compatibility.

## Verification Matrix

Launcher focused gates:

```bash
flutter analyze lib/features/mission_control test/features/mission_control
flutter test test/features/mission_control/mission_control_actions_test.dart --reporter=compact
flutter test test/features/mission_control/mission_control_bridge_test.dart --reporter=compact
flutter test test/features/mission_control/mission_agent_chat_adapter_test.dart --reporter=compact
flutter test test/features/mission_control/mission_agent_chat_panel_test.dart --reporter=compact
flutter test test/features/mission_control --reporter=compact
flutter build macos --debug
```

Hermes focused gates for Stage 64G or any Harness-side changes:

```bash
python -m compileall agent_runtime hermes_cli tests/agent_runtime
python -m pytest -q tests/agent_runtime/test_snapshot.py tests/agent_runtime/test_status.py
python -m pytest -q tests/agent_runtime/test_capabilities.py
python -m pytest -q tests/hermes_cli/test_harness_cli.py
python -m hermes_cli.main harness snapshot --json
python -m hermes_cli.main harness observe --json
```

Repository hygiene:

```bash
git diff --check
git status --short --branch
```

## Capability ID Reference

### Queue

- `task.create`
- `persona.message_task`
- `persona.instance.message`

### Run

- `task.tick`
- `task.run_until_settled`
- `persona.instance.run_once`
- `persona.diagnose`
- `daemon.run_once`

### Steer

- `worker.nudge`
- `worker.pause`
- `worker.resume`
- `worker.interrupt`
- `worker.possess`
- `worker.release`
- `run.cancel`
- `run.approve`
- `task.unblock`
- `lane.pause`
- `lane.park`
- `lane.resume`
- `lane.drain`

### Lifecycle

- `daemon.start`
- `daemon.stop`

### Archive / close

- `persona.instance.close`
- `persona.instance.archive`
- `task.archive`
- `task.archive_ready`

### Observe / readback

These are read APIs used by repositories/providers. They do not need write-capability submission in Stage 64:

- `snapshot.read`
- `observe.read`
- `status.read`
- `task.show`
- `task.history`
- `run.show`
- `worker.show`
- `persona.show`
- `persona.assignments`
- `proof.list`

## Risks and Interventions

### Risk: UI becomes generic workflow-console bloat

Severity: Medium.

Mitigation: keep capability groups contextual and Mission Control-specific. Do not add arbitrary command palettes, workflow builders, columns, drag/drop, or generic runtime adapters.

### Risk: Generic call envelope becomes arbitrary shell

Severity: Critical.

Mitigation: whitelist capability ids and static command builders only. No user-controlled executable, subcommand, or flag names.

### Risk: Free-floating send semantics confuse queue vs run

Severity: High.

Mitigation: queue-only composer default for free-floating instances plus explicit `Run One Turn` action and truthful accepted messages.

### Risk: Launcher inference diverges from Harness feature gates

Severity: Medium.

Mitigation: Stage 64G moves availability to Harness-emitted descriptors after local registry proves stable.

### Risk: Existing bespoke action tests mask missing generic coverage

Severity: Medium.

Mitigation: every capability id needs at least one validation or exact command-vector test before UI can render it.

## Final Handoff Contract

An implementation agent must not make architecture choices beyond this document. The fixed decisions are:

- no Harness logic rebuild;
- CLI remains write boundary;
- static whitelist first;
- generic intent added alongside existing bespoke intents;
- free-floating composer send queues by default after Stage 64D;
- immediate execution is an explicit run capability;
- Harness-emitted descriptors are required in Stage 64G for full completion, but not a prerequisite for Stages 64A-F;
- every capability maps to an existing `hermes harness ... --json` command;
- every action refreshes official Harness readback before claiming final state.
