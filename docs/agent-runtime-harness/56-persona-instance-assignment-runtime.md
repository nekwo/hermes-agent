# Stage 56 - Persona Instance Assignment Runtime

## Goal

Make the Harness behave like it has one uninterrupted competent worker per role. Neko, Launcher Dev, Backend Dev, and QA should exist as durable persona instances that receive assignments, keep their context, expose their live logs, and complete bounded work without the operator babysitting each tick.

Stage 55 made individual persona diagnostics possible. Stage 56 changes the core shape from task-first to persona-instance-first:

```text
PersonaInstance -> PersonaAssignment -> AgentRun -> Events/Proofs
Task/Goal -> aggregates assignments, decisions, logs, and final QA outcome
```

The task remains the product work container. The persona instance becomes the stable worker. Assignments become the unit of work that can be created by the state machine, an operator, another persona, or a diagnostic command.

## Why This Stage Exists

The current diagnostic path works, but it still creates a temporary Harness task to force a persona turn. That keeps proving the old shape is awkward:

- A worker's live terminal is often tied to whichever task/stage last emitted events, so Mission Control can appear to lose Neko, Dev, Backend Dev, or QA logs.
- Persona context, compression receipts, HUD state, and skill/tool budgets are stored indirectly through task/run state instead of being inspectable as the worker's own timeline.
- Operator steering has to mutate or create tasks instead of assigning a bounded message to the already-running worker.
- Proof targets can drift when a task-level plan field and a persona-level instruction disagree.
- Future possession or 3D worker control needs a stable worker identity that can be leased, messaged, paused, resumed, and inspected even when no product task is active.

## Non-Goals

- Do not rewrite the full Harness scheduler in one patch.
- Do not delete existing task, run, proof, archive, or Mission Control APIs.
- Do not make every role unlimited. Keep bounded assignments, watchdogs, and deterministic state acceptance.
- Do not add another daemon process just to represent workers. This should be an in-process runtime model used by CLI, daemon, diagnostics, and Mission Control.
- Do not hide raw evidence. Redaction or presentation filtering can happen in a later UI layer, while runtime events stay complete enough for diagnosis.

## Target Model

### PersonaInstance

A durable runtime record for one role/persona.

Minimum fields:

- `persona_instance_id`
- `persona_id`
- `role`
- `display_name`
- `profile_id`
- `runtime_root`
- `status`: `idle`, `assigned`, `running`, `waiting`, `blocked`, `possessed`, `offline`
- `current_assignment_id`
- `current_task_id`
- `session_id`
- `context_log_path`
- `context_receipt_ids`
- `last_compression_at`
- `prompt_hash`
- `skill_hashes`
- `tool_budget_state`
- `last_heartbeat_at`
- `created_at`
- `updated_at`

Implementation preference: wrap or extend the existing worker session store first, then migrate storage once behavior is proven. The first implementation should expose persona instances without breaking current `TaskStore`, `RunStore`, `WorkerSessionStore`, snapshot, or archive behavior.

### PersonaAssignment

A bounded unit of work assigned to a persona instance.

Minimum fields:

- `assignment_id`
- `persona_instance_id`
- `persona_id`
- `assignment_kind`: `task_stage`, `diagnostic`, `operator_message`, `cross_role_message`, `qa_review`, `recovery`, `possession`
- `task_id`
- `goal_id`
- `stage_id`
- `operation_id`
- `title`
- `message`
- `repo`
- `affected_paths`
- `proof_targets`
- `acceptance`
- `non_goals`
- `allowed_decisions`
- `allowed_tools`
- `state`: `queued`, `assigned`, `running`, `waiting_on_tool`, `waiting_on_proof`, `needs_input`, `completed`, `blocked`, `cancelled`
- `run_ids`
- `proof_ids`
- `created_by`
- `created_at`
- `started_at`
- `completed_at`
- `last_error`

Assignment state is runtime state. Task state changes only after the deterministic Harness state machine accepts an assignment result.

### AgentRun

Runs should link to both task and assignment:

- `run_id`
- `task_id`
- `assignment_id`
- `persona_instance_id`
- `persona_id`
- `decision_type`
- `validation_status`
- `token_usage`
- `tool_turn_count`
- `context_receipts`

The same task can have many assignments. The same persona instance can work many assignments over time without losing its worker identity.

## Implementation Stages

### 56A - Inventory And Compatibility Design

Audit the existing runtime surfaces before changing behavior:

- `agent_runtime/persona_diagnostics.py`
- worker session store and worker session event emission
- task store, run store, proof store, archive manifests
- `TickEngine` action execution and run creation
- mission state machine action selection
- typed mission plan and handoff synthesis
- `harness status`, `harness snapshot`, and Mission Control bridge consumers

Deliverables:

- A short code map in this document or a follow-up appendix.
- A compatibility decision for whether `PersonaInstanceStore` wraps `WorkerSessionStore` or stores a sidecar file.
- Regression tests that assert current task-first diagnostics still work before assignment migration begins.

### 56B - Add PersonaInstance Store

Create a first-class instance API with no behavior change.

Required behavior:

- One stable persona instance exists for each configured role: Neko, Launcher Dev, Backend Dev, QA.
- Instances can be listed even when no active task exists.
- Each instance exposes current task, current assignment, last run, live status, context log path, compression receipts, and heartbeat.
- Existing worker session records remain readable and are linked to the corresponding persona instance.
- `harness status --json` and `harness snapshot --json` include a `persona_instances` section behind a Tony runtime feature flag.

Tests:

- Unit tests for instance creation and idempotent lookup.
- Snapshot/status tests for idle instances.
- Migration/wrapper tests for existing worker session records.

### 56C - Add PersonaAssignment Store

Add assignment persistence before routing ticks through it.

Required behavior:

- Create, list, get, update, and complete assignments.
- Enforce idempotency: the runtime must not create a duplicate assignment for the same persona/task/stage/kind while a non-terminal assignment already exists, unless a real environment or scope signal changed.
- Preserve assignment evidence when tasks are archived.
- Assignments remain inspectable after task completion and archive.

Tests:

- Store CRUD tests.
- Duplicate prevention tests.
- Archive manifest tests showing assignment ids, run ids, proof ids, and context receipts are preserved.

### 56D - Route Ticks Through Assignments

Change the scheduler handoff point so state-machine actions create or resume assignments.

Required behavior:

- The mission state machine selects the next required role and work intent.
- The assignment layer materializes the bounded work packet for that persona.
- `TickEngine` consumes a `PersonaAssignment`, starts an `AgentRun`, and records `assignment_id` plus `persona_instance_id`.
- Task state transitions happen only after assignment result validation.
- Run-until-settled monitors assignment progress, not just task status.
- Watchdogs intervene only when there is evidence of looping, stalling, invalid packets, repeated same proof failures, or unsafe behavior.

Tests:

- Neko scoping emits exactly one Neko assignment.
- Dev implementation emits exactly one Dev assignment for the selected stage.
- QA review emits exactly one QA assignment after proof is attached.
- Re-running tick while an assignment is running resumes/observes it instead of opening duplicate work.

### 56E - Convert Persona Diagnostics To Assignments

Migrate Stage 55 diagnostics to the new assignment layer.

Required behavior:

- `harness persona diagnose <persona>` creates `PersonaAssignment(kind=diagnostic)`.
- The result returns `persona_instance_id`, `assignment_id`, `task_id`, `run_id`, decision, validation, token usage, and elapsed time.
- Diagnostic assignments can optionally attach to an existing task with `--task <task_id>` without changing product scope.
- Focused proof targets named in the diagnostic message remain assignment-level truth and cannot be replaced by generic no-edit recipes.

Tests:

- Existing persona diagnostic tests updated to assert assignment ids.
- Live-token Neko diagnostic proving one bounded Neko turn with no contract repair.

### 56F - Add Persona Messaging And Cross-Role Steering

Add bounded worker communication without dumping giant context blocks.

Required behavior:

- `harness persona message <persona> --task <task_id> --message ...` creates `operator_message` assignment.
- A persona can request bounded help from another persona through a `cross_role_message` assignment.
- Missing input should be routed to the best available role when possible:
  - Launcher Dev can ask Backend Dev about API behavior.
  - Backend Dev can ask Launcher Dev about UI contract behavior.
  - QA can ask Neko for acceptance-scope clarification.
  - Neko can steer any role back to the mission plan.
- Messages carry only the relevant task, stage, proof target, and recent decision receipts. They do not dump full logs unless explicitly requested.

Tests:

- CLI message creates assignment and does not mutate mission scope.
- Cross-role message can be emitted and rendered.
- Context payload size stays bounded.

### 56G - Mission Control HUD And Live Terminal

Move the Agent HUD and terminal to persona-instance identity.

Required behavior:

- The left agent selector maps to `persona_instance_id`, not whichever task emitted last.
- Every persona shows logs even when idle, archived, or between assignments.
- The HUD displays current assignment, task/stage, proof targets, allowed decisions, allowed tools, task checklist, context log path, compression timestamps, and last heartbeat.
- Terminal rows are grouped as DM-style event bubbles with expandable raw event payloads.
- Assignment filters show `All`, `Current assignment`, `Tools`, `Proof`, `Decision`, and `Context`.
- Archived tasks still render each persona's history through assignment/run links.

Tests:

- Snapshot fixture with Neko, Launcher Dev, Backend Dev, and QA events renders all four personas.
- Switching personas does not lose terminal events.
- Archived task fixture preserves persona logs.
- Fullscreen visual proof for Mission Control after debug build.

### 56H - Possession And Operator Lease

Prepare for future 3D possession without destabilizing normal runs.

Required behavior:

- An operator can lease a persona instance for manual steering.
- While possessed, the scheduler cannot start a duplicate model run for that persona.
- Operator messages and approvals become assignments or assignment events.
- Lease expiry and heartbeat recovery return the persona to normal scheduling safely.

Tests:

- Possession blocks duplicate assignment execution.
- Lease expiry releases the persona.
- Possession events are visible in snapshot and Mission Control.

### 56I - Observability, Self-Healing, And Performance Gates

Add worker-level counters so the Harness can self-heal without micromanagement.

Required counters:

- assignments completed per persona
- invalid decision packets per assignment
- contract repair attempts
- same assignment retries
- repeated read/search loops
- repeated proof target failures
- tool turns
- token usage
- idle time
- heartbeat age
- context compression count
- skill fanout count

Required behavior:

- The HUD tells the agent when its packet is invalid and lists the allowed choices.
- The runtime repairs one invalid packet when deterministic repair is safe.
- The runtime blocks or reroutes when the same assignment repeats without new evidence.
- Dev and QA should run proof commands inside monitored/background execution when commands can hang.
- Neko should resume from context receipts instead of re-reading broad history.

Tests:

- Invalid packet feedback test.
- Same-assignment retry blocker test.
- Token/tool budget warning test.
- Context receipt resume test.

### 56J - Rollout And Legacy Removal

Roll out behind a feature flag, then remove legacy task-first assumptions only after proof.

Feature flag:

- Default off for standard Hermes.
- Enabled for Tony Mission Control profile.
- Suggested key: `persona_instance_runtime.enabled`.

Rollout gates:

1. Instance store visible with no behavior change.
2. Assignment store visible with diagnostics only.
3. Neko diagnostics run assignment-backed.
4. All persona diagnostics run assignment-backed.
5. Normal goals route through assignments.
6. Mission Control reads persona instance logs.
7. Legacy task-first terminal grouping is removed.

Removal candidates after gates pass:

- Task-only live terminal grouping.
- Diagnostic task metadata used as the only persona operation identity.
- Plan fields that override assignment-level proof targets.
- Repeated Harness proof-request loops when the agent can run the command directly.

## Enterprise Test Matrix

Required before calling Stage 56 complete:

- Full `tests/agent_runtime` pass.
- Focused CLI tests for `harness persona diagnose` and `harness persona message`.
- Snapshot/status JSON contract tests with feature flag off and on.
- Archive manifest tests proving assignments and persona logs are preserved.
- Mission Control fixture tests for all four persona terminals.
- Fullscreen Mission Control screenshot after debug build.
- Live-token Neko-only diagnostic under two minutes with correct focused proof target.
- Live-token Dev-only diagnostic with one monitored proof command.
- Live-token QA-only diagnostic with one verdict and no invented fields.
- Full live goal smoke: Neko -> Dev -> QA with all events visible per persona.

## Success Criteria

Stage 56 is complete when:

- A persona can exist, be inspected, and receive work without creating a fake product task.
- A task can aggregate multiple persona assignments without owning the worker identity.
- Neko, Launcher Dev, Backend Dev, and QA each have persistent HUD state and terminal logs.
- Assignment proof targets remain authoritative and do not drift into generic recipes.
- Re-running tick resumes or observes active assignments instead of duplicating work.
- Operator steering and cross-role communication are bounded assignments.
- Archive preserves task evidence plus persona assignment history.
- The Harness can explain why it cannot continue when blocked, including the exact assignment, persona, proof, and watchdog signal.

## Alternatives Considered

### Keep Task-First Workers

Rejected for the long-term runtime. It is simpler in storage, but it keeps causing UI log loss, fake diagnostic tasks, awkward operator messages, and poor support for possession.

### Add A Separate Queue Service Now

Deferred. A queue may be useful later, but the first implementation should stay in-process and deterministic so CLI, daemon, diagnostics, and tests share one model.

### Make Assignments Unlimited

Rejected. The desired behavior is not unlimited loops. The target is one competent uninterrupted worker per role, with watchdogs that intervene only on evidence of looping, stalling, invalid packets, or unsafe behavior.

## Recommended First Patch

Start with 56B and 56C only:

1. Add read/write stores for `PersonaInstance` and `PersonaAssignment`.
2. Expose them in `harness status --json` and `harness snapshot --json` behind `persona_instance_runtime.enabled`.
3. Add tests for idle instances, assignment CRUD, duplicate prevention, and archive preservation.
4. Do not route normal goals through assignments until the compatibility layer is proven.

This keeps the patch narrow while establishing the right foundation for diagnostics, HUD logs, cross-role messaging, and possession.

## Deep Audit Closure

This section turns Stage 56 from architecture into implementation instructions based on the current codebase.

### Existing Code Seams

Current runtime modules already contain most of the foundation:

- `agent_runtime/models.py`
  - `WorkerSession` already has `current_assignment_id`, possession fields, context receipt fields, budget counters, prompt hash, skill hash, and heartbeat fields.
  - `AgentRun` does not yet have `assignment_id` or `persona_instance_id`.
  - `Event` does not yet have top-level assignment identity, so assignment identity must be present in event payloads until the event schema is migrated.
- `agent_runtime/worker_sessions.py`
  - `WorkerSessionStore.open_or_resume()` is the right compatibility seam for Stage 56.
  - Possession, pause, resume, interrupt, nudge, release, heartbeats, context receipts, token budgets, tool budgets, and prompt receipts already exist here.
  - The first implementation should wrap this store instead of creating a second worker truth.
- `agent_runtime/persona_diagnostics.py`
  - `PersonaDiagnosticController` currently creates a normal task with `persona_operation_*` risk flags, then calls `TickEngine.run_until_settled()`.
  - This is the first consumer to migrate to assignments after stores exist.
- `agent_runtime/ticker.py`
  - `TickEngine.run_until_settled()` and the run-opening path are the routing point that must attach assignment identity.
  - Do not make broad scheduler changes until diagnostics prove assignment persistence and idempotency.
- `agent_runtime/state_machine.py`
  - `MissionStateMachine.next_action()` remains the source of task-level intent.
  - Stage 56 should add an assignment materialization layer after this decision, not replace state-machine decisions immediately.
- `agent_runtime/status.py`
  - `build_status()` already loads agents, tasks, runs, workers, observability, dirty state, and next actions.
  - Feature-gated `persona_instances` and assignment summaries should be added here.
- `agent_runtime/snapshot.py`
  - `build_snapshot()` already emits `worker_sessions`, tasks, archived tasks, role streams, role envelopes, role checklists, proof batches, and events.
  - Feature-gated `persona_instances`, `persona_assignments`, and per-persona terminal grouping should be added here before Launcher changes.
- `agent_runtime/paths.py`
  - Add path helpers here, not ad hoc paths in stores.
- `hermes_cli/harness.py`
  - The CLI already has `harness persona diagnose`, `harness worker ...`, `harness status`, `harness snapshot`, `harness run-until-settled`, and archive commands.
  - Add `harness persona list`, `harness persona show`, `harness persona assignments`, and `harness persona message` here.
- Tests should live under `tests/agent_runtime/`, with focused tests near existing `test_persona_diagnostics.py`, `test_snapshot.py`, `test_status.py`, `test_archive*.py`, and ticker/state-machine tests.

### Implementation Decision

Use this exact migration strategy:

1. `PersonaInstance` is a derived compatibility view over configured personas plus latest `WorkerSession` records.
2. `PersonaAssignment` is a new persisted record.
3. `WorkerSession.current_assignment_id` links the active assignment to the existing worker session.
4. `AgentRun.progress["assignment_id"]` and `AgentRun.progress["persona_instance_id"]` are used as the first compatibility link.
5. Only after tests pass should `AgentRun` grow first-class optional fields.

Reason: this avoids a schema-heavy rewrite and uses the worker-session code that already solved possession, context receipts, budgets, and heartbeats.

### Storage Contract

Add path helpers in `agent_runtime/paths.py`:

```python
def persona_instances_dir() -> Path:
    return store_root() / "persona_instances"

def persona_instance_path(persona_instance_id: str) -> Path:
    return persona_instances_dir() / f"{_safe_path_token(persona_instance_id)}.json"

def persona_assignments_dir() -> Path:
    return store_root() / "persona_assignments"

def persona_assignment_path(assignment_id: str) -> Path:
    return persona_assignments_dir() / f"{_safe_path_token(assignment_id)}.json"
```

Recommended ids:

- `persona_instance_id`: `personainst_<persona_id>` for the singleton role worker, for example `personainst_neko_supervisor`.
- `assignment_id`: `assign_<12 hex>`.

Do not put assignments under task directories. Assignments must remain listable by persona even when they are unattached diagnostics or future possession messages.

### New Models

Add new dataclasses in `agent_runtime/models.py`:

```python
@dataclass(slots=True)
class PersonaInstance:
    id: str
    persona_id: str
    role: str
    display_name: str
    profile_id: str | None
    runtime_root: str
    state: WorkerSessionState
    current_assignment_id: str | None = None
    current_task_id: str | None = None
    active_worker_session_id: str | None = None
    active_run_id: str | None = None
    session_id: str | None = None
    context_receipt_id: str | None = None
    compression_receipt_id: str | None = None
    prompt_contract_hash: str | None = None
    skill_manifest_hash: str | None = None
    token_budget_used: int = 0
    tool_budget_used: int = 0
    watchdog_warning_count: int = 0
    last_heartbeat_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1
```

```python
@dataclass(slots=True)
class PersonaAssignment:
    id: str
    persona_instance_id: str
    persona_id: str
    kind: str
    state: str
    title: str
    message: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    task_id: str | None = None
    goal_id: str | None = None
    stage_id: str | None = None
    operation_id: str | None = None
    repo: str | None = None
    affected_paths: list[str] = field(default_factory=list)
    proof_targets: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    allowed_decisions: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    proof_ids: list[str] = field(default_factory=list)
    context_receipt_ids: list[str] = field(default_factory=list)
    last_error: str | None = None
    completed_at: datetime | None = None
    signal_hash: str | None = None
    schema_version: int = 1
```

Use strings for `kind` and `state` in the first patch to reduce enum migration churn. Add enums only after the contract stabilizes.

### New Module

Add `agent_runtime/persona_assignments.py`.

Required public surface:

```python
TERMINAL_ASSIGNMENT_STATES = {"completed", "blocked", "cancelled"}
ACTIVE_ASSIGNMENT_STATES = {"queued", "assigned", "running", "waiting_on_tool", "waiting_on_proof", "needs_input"}

class PersonaInstanceStore:
    def ensure_for_persona(self, persona: AgentPersona) -> PersonaInstance: ...
    def list_all(self) -> list[PersonaInstance]: ...
    def get(self, persona_instance_id: str) -> PersonaInstance: ...
    def update_from_worker(self, worker: WorkerSession) -> PersonaInstance: ...
    def derive_from_workers(self, personas: list[AgentPersona], workers: list[WorkerSession]) -> list[PersonaInstance]: ...

class PersonaAssignmentStore:
    def create(self, assignment: PersonaAssignment) -> PersonaAssignment: ...
    def get(self, assignment_id: str) -> PersonaAssignment: ...
    def update(self, assignment: PersonaAssignment) -> PersonaAssignment: ...
    def list_all(self) -> list[PersonaAssignment]: ...
    def list_for_persona(self, persona_id: str) -> list[PersonaAssignment]: ...
    def list_for_task(self, task_id: str) -> list[PersonaAssignment]: ...
    def find_active(self, *, persona_id: str | None = None, task_id: str | None = None, stage_id: str | None = None, kind: str | None = None) -> list[PersonaAssignment]: ...
    def create_or_resume(self, spec: PersonaAssignmentSpec) -> PersonaAssignment: ...
    def attach_run(self, assignment_id: str, run_id: str) -> PersonaAssignment: ...
    def attach_proof(self, assignment_id: str, proof_id: str) -> PersonaAssignment: ...
    def complete(self, assignment_id: str, *, state: str = "completed", error: str | None = None) -> PersonaAssignment: ...
```

`create_or_resume()` must compute a deterministic `signal_hash` from persona, task, stage, kind, repo, affected paths, proof targets, and message. If a non-terminal assignment with the same key and signal hash exists, return it instead of creating a duplicate.

### Feature Flag Contract

Add config under `EnterpriseWorkerSessionsConfig` first:

```python
persona_instance_runtime: bool = False
persona_assignment_store: bool = False
```

Do not add a second config tree unless this grows beyond worker-session ownership. The Stage 56 runtime is part of enterprise worker sessions.

Default behavior:

- Standard Hermes: both flags false.
- Tony Mission Control profile: both true during staged rollout.
- Tests: explicit config objects, never dependent on the operator's live config.

### CLI Contract

Add these commands in `hermes_cli/harness.py`:

```powershell
python -m hermes_cli.main harness persona list --json
python -m hermes_cli.main harness persona show <persona_id_or_instance_id> --json
python -m hermes_cli.main harness persona assignments [--persona <persona>] [--task <task_id>] --json
python -m hermes_cli.main harness persona message <persona> --task <task_id> --message "..." --json
```

Expected JSON shape for `persona list`:

```json
{
  "feature_enabled": true,
  "persona_instances": [
    {
      "persona_instance_id": "personainst_neko_supervisor",
      "persona_id": "neko_supervisor",
      "display_name": "Neko Mission Lead",
      "state": "idle",
      "current_assignment_id": null,
      "current_task_id": null,
      "active_worker_session_id": null,
      "active_run_id": null,
      "last_heartbeat_at": null
    }
  ]
}
```

Expected JSON shape for `persona message`:

```json
{
  "ok": true,
  "assignment_id": "assign_...",
  "persona_instance_id": "personainst_dev",
  "persona_id": "dev",
  "task_id": "task_...",
  "state": "queued",
  "kind": "operator_message"
}
```

`persona message` should create an assignment only. It must not tick, mutate task scope, or change mission state in the first patch.

### Status And Snapshot Contract

When `persona_instance_runtime` is enabled, add to `build_status()`:

```json
"persona_instances": [...],
"persona_assignments": {
  "active": [...],
  "recent": [...]
}
```

When disabled, omit the fields or emit:

```json
"persona_instance_runtime": {"enabled": false}
```

Use the same shape in `build_snapshot()`, plus task summaries should include:

```json
"persona_assignment_ids": ["assign_..."],
"persona_streams": {
  "neko_supervisor": {"assignment_ids": [...], "run_ids": [...], "event_count": 0}
}
```

Do not remove `worker_sessions` from status or snapshot.

### Archive Contract

Archive must preserve:

- `persona_assignments/*.json`
- assignment ids in manifest summaries
- worker sessions with `current_assignment_id`
- runs whose `progress.assignment_id` matches archived assignment ids
- events whose payload includes `assignment_id`

Implementation detail:

- Add assignment copies to the same `deleted_archive/<batch>/persona_assignments/` folder.
- For task archive, include assignments where `assignment.task_id == task.id`.
- For archive-ready batch, include assignments for every archived task.
- Do not archive unattached diagnostic assignments unless the diagnostic task itself is archived.

### Tick Integration Contract

Stage 56D must be implemented in two passes.

Pass 1, observe-only:

- State machine still returns the same action.
- Assignment layer creates or resumes a `task_stage` assignment for the selected persona/action.
- `WorkerSessionStore.open_or_resume()` receives the assignment id and writes `current_assignment_id`.
- `AgentRun.progress` receives `assignment_id` and `persona_instance_id`.
- Assignment state follows run state, but task state remains unchanged.

Pass 2, authoritative:

- `run_until_settled()` checks active assignment state before opening another run.
- Duplicate assignment prevention becomes the guard against repeated same-stage Dev/QA loops.
- Task state transitions require a terminal assignment result plus valid decision contract.

### Mission Control Contract

Launcher work should not begin until `snapshot --json` emits all needed data.

Mission Control should use:

- `snapshot.persona_instances` for the agent selector.
- `snapshot.persona_assignments` for current assignment and assignment history.
- `task.persona_streams` for archived and task-specific grouping.
- Existing `worker_sessions`, `runs`, `proofs`, and events as fallback while rollout is feature-gated.

Visual acceptance:

- Fullscreen Mission Control screenshot.
- Select Neko, Launcher Dev, Backend Dev, and QA.
- Each role shows an idle or active HUD, context path/receipt state, and terminal history.
- Switching archived tasks does not collapse every role into only the last persona that emitted events.

### Implementation Order With Commit Boundaries

Use small commit boundaries:

1. `feat(harness): add persona assignment stores`
   - Models, path helpers, stores, config flags, unit tests.
2. `feat(harness): expose persona instances in status`
   - Status/snapshot fields and CLI list/show/assignments.
3. `feat(harness): add persona message assignments`
   - CLI message command and bounded context payload tests.
4. `feat(harness): link diagnostics to assignments`
   - `persona diagnose` creates assignment, writes assignment ids in result and run progress.
5. `feat(harness): route ticks through observe-only assignments`
   - Tick/session/run links without authoritative task-state changes.
6. `fix(launcher): render persona instance streams`
   - Mission Control consumes new snapshot fields.
7. `feat(harness): enforce assignment idempotency in normal flow`
   - Authoritative duplicate prevention after diagnostics and UI prove stable.

### Tests To Add Per Commit

Commit 1:

- `tests/agent_runtime/test_persona_assignments.py`
  - creates singleton persona instances
  - derives instances from worker sessions
  - creates assignments
  - resumes same signal hash
  - creates new assignment after signal changes

Commit 2:

- `tests/agent_runtime/test_status.py`
  - status includes instances only when enabled
  - status remains unchanged when disabled
- `tests/agent_runtime/test_snapshot.py`
  - snapshot includes instances and assignments only when enabled
  - all four configured personas appear even with no active task

Commit 3:

- CLI parser/help tests for `persona message`.
- Message command creates assignment and does not tick.
- Message payload is bounded and task scope is unchanged.

Commit 4:

- Update `tests/agent_runtime/test_persona_diagnostics.py`.
- Diagnostic result includes `assignment_id` and `persona_instance_id`.
- Focused proof target remains in assignment and diagnostic task stage.

Commit 5:

- Tick observe-only test proving run progress contains `assignment_id`.
- Worker session test proving `current_assignment_id` is set.
- Repeated tick test proving active assignment is resumed or observed instead of duplicated.

Commit 6:

- Launcher fixture test with all four persona streams.
- Archived task fixture test.
- Fullscreen screenshot proof.

Commit 7:

- Normal goal duplicate prevention test.
- Same-stage retry after unchanged blocker test.
- Watchdog observability counter test.

### Risks Still Open

- `AgentRun` currently lacks first-class assignment fields. Use `progress` metadata first, then schema fields later.
- `Event` currently lacks first-class assignment fields. Use event payloads first.
- Archive code must be audited during implementation because archive copy logic is outside this document's inspected excerpts.
- Launcher exact file paths are not listed here because this doc is Harness-side. The Launcher patch must start from the bridge/snapshot consumers and Mission Control terminal renderer.
- Live-token proof is required after diagnostics migration. Unit tests alone cannot prove persona behavior.

### Ready-To-Implement Checklist

Before coding Stage 56, verify:

- `git status --short` is clean or unrelated changes are identified.
- `python -m hermes_cli.main harness status --json` works on the intended runtime root.
- `python -m hermes_cli.main harness snapshot --json` works on the intended runtime root.
- `python -m pytest tests/agent_runtime/test_persona_diagnostics.py tests/agent_runtime/test_worker_sessions.py -q` passes before edits.

Stage 56 is implementation-ready when the first patch begins with stores, status, snapshot, and tests. It should not start by changing `TickEngine` behavior.
