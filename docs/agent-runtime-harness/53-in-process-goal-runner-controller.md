# Stage 53 - In-Process Goal Runner Controller

## Problem

Mission Control goals still require too much babysitting because the current CLI flow splits one operator intent across multiple lifecycle commands:

1. create task;
2. start daemon or manually tick;
3. watch status;
4. repair if stuck;
5. tick again;
6. archive when terminal.

Stage 52 improved the worker envelope, and `harness task create` now starts the daemon as a bridge fix. That bridge is useful, but it is not the enterprise shape for CLI-launched live goals. Task creation should not own background process lifecycle. It should either enqueue work into an already-supervised service, or a single foreground command should own the mission controller from creation through terminal state.

The near-term enterprise path is an explicit in-process goal runner:

```powershell
python -m hermes_cli.main harness goal run --title "..." --description "..." --json
```

The command creates the goal, constructs a `MissionRuntimeController` in the same Python process, and runs bounded ticks until the goal reaches a clear stop boundary. No hidden background process is required for CLI-driven work.

## Existing Full-Tick Audit

This stage must not duplicate the scheduler. Hermes already has a full-tick/settler path:

- `agent_runtime.ticker.TickEngine.run_until_settled(...)`
- `hermes harness run-until-settled --task <task_id> --max-actions <n> --max-seconds <seconds> --json`
- `agent_runtime.daemon.MissionDaemon.run_foreground(...)`, which calls `TickEngine.run_until_settled(max_actions=10)` each loop
- burn-in helpers that already call `engine.run_until_settled(task_id=..., max_actions=...)`

Current `run_until_settled` already handles important boundaries:

- `task_terminal`
- `task_blocked`
- `incident_opened`
- `waiting_on_approval`
- `active_run`
- `action_failed`
- `max_actions`
- `max_seconds`
- `no_eligible_action`
- `tick_lock_unavailable`

It also already contains nontrivial recovery behavior:

- stale run marking before settling;
- open incident handling;
- recoverable blocked-task routing through the state machine;
- budget-approval incident continuation;
- waiting/active run boundaries;
- bounded action count enforcement;
- task-id targeting.

So Stage 53 is not "build a second full tick." The real gap is productization:

- goal creation and settling are separate commands;
- `run-until-settled` cannot create the goal it is settling;
- `run-until-settled` always exits `0`, even when the stop reason means blocked/waiting/failed;
- result JSON is a raw settler result, not a goal-level operator summary;
- no compact monitor stream exists for humans watching a live goal;
- no terminal archive policy is integrated;
- task creation currently has a bridge daemon auto-start, which is useful but hides lifecycle instead of making the foreground command own it.

Stage 53 should wrap and extend the existing settler, not replace it.

## Live-Run Problems To Preserve As Requirements

Recent real-token goals completed, but they were too slow and required operator recovery. Stage 53 must treat those symptoms as requirements, not anecdotes.

Observed problems:

- Too many operator-visible steps for small goals. A narrow Launcher UI change still required create, Neko, Dev, manual status checks, recovery, QA, complete, commit, and archive.
- Dev over-spent tokens before landing simple patches. One small posts-image sizing goal hit a large token budget before the useful patch was stable.
- Dev missed measurable product intent. "3x bigger" initially became a smaller partial change, so the controller summary must surface acceptance math/claim coverage instead of only "Dev patched."
- Neko sometimes scoped incompletely. Visual UI goals need `requires_visual_proof`, affected paths, and relevant proof expectations before Dev starts.
- Typed mission state and legacy task state can drift. In prior runs, implementation proof existed but routing still re-entered Dev until manual repair moved the task to QA.
- Visual proof can be weak or irrelevant. A Stage C Posts screenshot loaded the tab but the feed was empty, proving runtime navigation but not image-row sizing.
- QA can approve with mixed proof strength. The final verdict should distinguish command/widget proof, visual runtime proof, and visual acceptance proof with fixture coverage.
- Neko can still emit invalid packets/checklist updates. The controller should stop with a clear invalid-output summary and next action instead of letting the operator infer what happened.
- Full test suites can contain unrelated failures. Goal summaries must separate focused affected proof from unrelated discovered failures.
- Daemon/task lifecycle was hidden. A goal could exist while no foreground controller was obviously driving it, creating the feeling that the system froze.

Stage 53 must improve the operator experience around these problems:

- One command should create, settle, monitor, summarize, and return a truthful exit code.
- Monitor output should show which role is active, what proof is missing, why a boundary was reached, and whether progress is happening.
- The final summary must call out manual interventions, invalid packets, weak visual proof, unrelated test failures, and acceptance-coverage caveats.
- If a task blocks, the output must include actionable next steps derived from incidents/proofs/events, not just raw stop reason.
- If the controller needed no manual ticks, that should be visible in the result; if it did, the result should say where automation was insufficient.

## Goals

- Make CLI-created goals self-driving without an ad hoc daemon spawn.
- Keep one command responsible for lifecycle, monitoring, exit code, and final summary.
- Preserve the existing Harness state machine, proofs, worker sessions, role envelopes, event log, and Mission Control UI data.
- Keep the daemon/service path available for Launcher Mission Control and future always-on runtime service work.
- Treat this as a new Mission Control product surface, not a legacy CLI compatibility exercise.
- Remove legacy state/packet/checklist burden from agent-facing prompts and contracts.
- Reduce manual babysitting by turning common operator actions into controller policy:
  - create;
  - clean temp state;
  - run until settled;
  - monitor health;
  - stop on blocked/waiting/done;
  - optionally archive terminal smoke/test goals.

## Non-Goals

- Do not remove the Mission Daemon.
- Do not implement a Windows Service in this stage.
- Do not bypass QA, proof gates, typed mission plans, or role envelopes.
- Do not make all goals unlimited. The controller must remain bounded and observable.
- Do not hard-delete runtime evidence.
- Do not claim to expose hidden provider chain-of-thought that the model/API does not return. Capture every observable runtime output, tool call, tool result, model message, and safe reasoning summary; downstream presentation/redaction layers decide what to show.

## New Product Contract: Remove Legacy Agent Burden

Mission Control should stop exposing legacy Harness machinery to the agents. Keep rich internal state, but make the worker-facing product feel like one competent role doing a job.

Current agent-facing burden to remove or hide for all personas:

- legacy task state names;
- typed mission stage projection details;
- checklist mutation rules;
- packet kind menus that are not directly relevant to the role;
- proof attachment mechanics;
- archive mechanics;
- daemon/service lifecycle;
- state-machine transition responsibility;
- QA handoff ceremony;
- patch-by-patch approval expectations;
- legacy field compatibility requirements.

This cleanup applies equally to Neko, Dev, Backend Dev, and QA. If only Dev is simplified, Neko will still over-steer with legacy stage/status concepts and QA will still over-focus on packet/proof mechanics instead of final acceptance.

The Harness may still keep these internally for migration and observability, but agents should not have to think about them. Use adapters/projections:

- `MissionIntent` => Neko-facing scope packet;
- `WorkerAssignment` => Dev-facing work packet;
- `ProofRecipe` => Harness-owned command/visual capture plan;
- `QAVerificationRequest` => QA-facing final verification packet;
- legacy task/stage fields => internal persistence projection only.

### Minimal Role Contracts

All personas get the same simplified outer frame:

- current assignment;
- allowed actions;
- acceptance criteria;
- relevant evidence so far;
- missing evidence, if any;
- budget/watchdog limits;
- the exact valid response shape.

They do not get legacy state-machine menus, raw checklist mutation rules, or archive/daemon instructions unless the task explicitly requires Harness development.

Dev should only need these actions:

```json
{
  "allowed_actions": ["deliver", "report_blocker", "request_missing_input"],
  "objective": "...",
  "acceptance": ["..."],
  "repo": "EterniaLauncher",
  "likely_paths": ["..."],
  "required_proofs": ["focused_analyze", "focused_tests"],
  "visual_required": true,
  "budgets": {
    "read_search_limit": 8,
    "proof_command_limit": 2
  }
}
```

Dev returns:

```json
{
  "action": "deliver",
  "summary": "...",
  "changed_files": ["..."],
  "commands_run": ["..."],
  "proof_refs": ["..."],
  "remaining_risk": "..."
}
```

Neko should only need:

```json
{
  "allowed_actions": ["assign", "report_blocker", "request_missing_input"],
  "objective": "...",
  "acceptance": ["..."],
  "repo_candidates": ["EterniaLauncher", "EterniaBackend", "hermes-agent"],
  "proof_policy": "harness_owned",
  "routing_options": ["launcher_dev", "backend_dev", "qa_after_dev", "blocked"]
}
```

Neko should not manually set task/stage status, mutate checklists, or emit legacy release packets. Neko's job is to assign the right worker, preserve scope, detect missing context, and explain blockers.

Backend Dev uses the same contract as Launcher Dev, scoped to backend repo/proofs:

```json
{
  "allowed_actions": ["deliver", "report_blocker", "request_missing_input"],
  "objective": "...",
  "acceptance": ["..."],
  "repo": "EterniaBackend",
  "likely_paths": ["..."],
  "required_proofs": ["focused_backend_tests"],
  "visual_required": false,
  "budgets": {
    "read_search_limit": 8,
    "proof_command_limit": 2
  }
}
```

QA should only need:

```json
{
  "allowed_actions": ["approve", "reject", "request_missing_proof"],
  "objective": "...",
  "acceptance": ["..."],
  "changed_files": ["..."],
  "proof_refs": ["..."],
  "visual_required": true,
  "visual_fixture_expected": "..."
}
```

QA should not decide how to attach proofs, manually close stages, or reason about legacy state names. QA judges final outcome coverage:

- acceptance criteria met or not;
- focused command proof relevant or not;
- visual runtime proof relevant or not;
- visual acceptance proof relevant or not;
- unrelated failures separated from task blockers;
- final verdict with actionable missing evidence.

The Harness derives state transitions from these simple role decisions. Agents do not manually set stage status.

### Cross-Role Missing Input Routing

`request_missing_input` is not a generic pause button and does not mean "ask Tony" by default. It is a structured cross-role routing request. The Harness should first route the missing input to the best available persona that can answer it from repo context, proof context, or role expertise.

Allowed missing input types:

- `backend_contract`: Launcher Dev or QA needs backend response shape, API semantics, serializer behavior, or backend proof context.
- `frontend_usage`: Backend Dev or QA needs to know how Launcher consumes a field, endpoint, event, or visual state.
- `visual_verification`: Neko or Dev needs QA to judge whether visual proof covers the claim.
- `scope_decision`: Dev or QA sees ambiguous ownership/scope and needs Neko to decide.
- `proof_gap`: Dev or QA needs Harness/QA to identify the correct proof recipe or missing proof artifact.
- `environment_blocker`: a tool/runtime dependency is unavailable and needs Harness preflight/self-heal before another worker run.
- `user_decision`: only for product intent or external decisions no persona can infer safely.

Request shape:

```json
{
  "action": "request_missing_input",
  "missing_input_type": "backend_contract",
  "preferred_responder": "backend_dev",
  "why_blocking": "Launcher Dev needs the authoritative backend payload shape before changing the UI parser.",
  "attempted_self_service": ["searched frontend usage", "checked existing proof refs"],
  "minimum_needed": "Endpoint response field names and nullability for post image attachments."
}
```

Harness routing examples:

- Launcher Dev requests `backend_contract` => route to Backend Dev, then return to Launcher Dev.
- Backend Dev requests `frontend_usage` => route to Launcher Dev, then return to Backend Dev.
- Dev requests `visual_verification` => route to QA.
- QA requests `scope_decision` => route to Neko.
- Neko requests `user_decision` only when no worker can infer the answer safely.
- Any worker requests `environment_blocker` => route to Harness preflight/self-heal, not another model, unless the blocker requires code/tool repair.

Routing record:

```json
{
  "type": "missing_input.route",
  "task_id": "task_123",
  "from_persona": "dev",
  "to_persona": "backend_dev",
  "return_to_persona": "dev",
  "missing_input_type": "backend_contract",
  "status": "requested",
  "minimum_needed": "Endpoint response field names and nullability for post image attachments."
}
```

Responder shape:

```json
{
  "action": "answer_missing_input",
  "request_id": "missing_input_123",
  "answer_summary": "...",
  "evidence_refs": ["event_1", "proof_backend_1"],
  "confidence": "high",
  "remaining_uncertainty": "..."
}
```

The original worker receives the answer as a compact continuation note:

```json
{
  "missing_input_answered": true,
  "missing_input_type": "backend_contract",
  "answered_by": "backend_dev",
  "answer_summary": "...",
  "evidence_refs": ["event_1", "proof_backend_1"],
  "next_expected": "continue original assignment"
}
```

Guardrails:

- The Harness should reject vague missing-input requests such as "need more context."
- The worker must state why the missing input is blocking and what minimum answer is needed.
- The worker must list attempted self-service unless the missing input is obviously owned by another role.
- The Harness should dedupe repeated identical requests.
- Cross-role answers should be compact and evidence-linked, not full context dumps.
- If a role cannot answer, it returns `report_blocker` with why and the Harness escalates to Neko or Tony only when appropriate.

### Role Cleanup Requirements

Neko cleanup:

- remove legacy release/handoff packet burden;
- remove direct stage status mutation;
- give Neko a small routing menu and exact assign/block/request-missing-input shape;
- make missing visual proof, missing repo/path, and ambiguous scope first-class checklist items managed by Harness, not Neko-authored schema guesses;
- make invalid Neko output return a concise correction with the allowed action menu.

Launcher Dev and Backend Dev cleanup:

- remove proof attachment mechanics;
- remove QA handoff ceremony;
- allow patch/test/revise inside one session;
- require only final `deliver`, `report_blocker`, or `request_missing_input`;
- make Harness capture command proof automatically from tool results.

QA cleanup:

- remove packet/proof plumbing;
- remove stage mutation/checklist mutation;
- provide a final verification packet with changed files, proof refs, acceptance criteria, and expected visual fixture;
- require `approve`, `reject`, or `request_missing_proof`;
- make QA verdict distinguish product acceptance from proof artifact limitations.

## Raw Capture First, Presentation Later

Mission Control is a new product surface, so runtime capture should be raw-first and complete. The capture layer should persist every observable thing the runtime receives or executes:

- model input envelope ids and metadata;
- model output messages;
- provider-visible reasoning summaries when available;
- tool calls;
- tool arguments;
- tool outputs;
- command stdout/stderr summaries and raw artifacts;
- file edit summaries and diffs;
- proof capture records;
- screenshots and visual manifests;
- invalid packets and validator errors;
- state transitions;
- watchdog interventions;
- token/API/wall-clock counters.

Do not make the capture layer responsible for final user-facing redaction. Add a later presentation layer that filters, redacts, groups, and summarizes for the Launcher UI, audit exports, or public reports.

Important constraint: hidden provider chain-of-thought is not an observable runtime artifact if the provider does not return it. The product should not claim to capture unavailable hidden reasoning. It should capture the model's actual returned content, safe reasoning summaries, decisions, tool use, and all external work evidence.

## Persona Worklog Stream For Launcher Terminal

The Launcher terminal should show each persona's work the way a good coding agent feels in chat: concise, timestamped, human-readable progress notes interleaved with tool/proof events. The first screen should not be raw event JSON. Raw JSON belongs behind expandable details.

Target feel:

```text
Neko Mission Lead
09:08:01  I’m checking the task state and next Harness action first.
09:08:47  The goal is created and needs Launcher Dev. I’m scoping focused Flutter proof plus QA visual verification.
09:09:12  Scope is ready, but visual proof metadata is missing. I’m marking visual proof required before Dev starts.

Launcher Dev Agent
09:09:36  I’m inspecting the posts media layout path and the focused widget tests.
09:13:42  I found the image hero height cap. I’m changing the cap and updating native/federated image post expectations.
09:17:11  Focused analyze and two image-post widget tests passed. I’m delivering this for QA.

QA Agent
09:32:18  I’m checking command proof first, then the Stage C visual artifact.
09:33:29  Command proof covers sizing. Visual artifact proves the Posts tab loads, but the feed is empty, so visual acceptance is partial.
```

This is not hidden chain-of-thought. It is an observable agent worklog made from:

- assistant-visible progress messages;
- role decision summaries;
- safe reasoning summaries returned by the provider;
- tool start/finish events;
- proof attach/approve/reject events;
- state transitions;
- validator errors and recovery actions;
- explicit "next action" fields from the controller.

### Worklog Event Shape

Persist a normalized `persona.worklog` event in addition to the raw event/proof/run records:

```json
{
  "type": "persona.worklog",
  "task_id": "task_123",
  "run_id": "run_abc",
  "persona_id": "dev",
  "stage_id": "launcher_implementation",
  "timestamp": "2026-06-08T13:09:36Z",
  "message": "I’m inspecting the posts media layout path and the focused widget tests.",
  "source": "agent_progress",
  "visibility": "operator",
  "related_event_ids": ["event_1"],
  "related_proof_ids": [],
  "redaction_layer": "presentation_pending"
}
```

`source` values:

- `agent_progress`
- `safe_reasoning_summary`
- `tool_event`
- `proof_event`
- `state_transition`
- `validator_feedback`
- `controller_summary`

The Launcher terminal groups these by persona and renders them as compact DM-style bubbles. Each row can expand to show raw events, tool output, proof IDs, JSON payloads, and artifacts.

### Persona Tabs

Each persona should have the same worklog surface:

- Neko Mission Lead;
- Launcher Dev Agent;
- Backend Dev Agent;
- QA Agent.

Empty persona tabs should show an explicit state such as "No run assigned yet" or "Waiting for Neko release", not an empty terminal. This prevents the UI from looking frozen when only one persona has logs.

### Worklog Requirements

- Preserve raw logs and raw JSON separately.
- Do not redact at capture time except for mandatory secret safety boundaries.
- Presentation layer may redact, collapse, or hide fields later.
- Every visible worklog item should link back to raw evidence.
- Invalid packets should produce a clear worklog item explaining the allowed options and next recovery path.
- Budget/stall/watchdog events should show what the Harness is doing next.
- QA should distinguish command proof, visual runtime proof, and visual acceptance proof.

## Final Outcome Gate, Not Patch-By-Patch Scrutiny

Every patch does not need to be scrutinized by QA or Neko. The Harness should let Dev work inside one bounded session and only gate the final delivered outcome unless a watchdog sees evidence of a real problem.

Dev may patch, run tests, revise, and patch again inside the same stage without requiring Harness-level approval after every small edit. Harness intervention should happen on:

- invalid role decision;
- repeated no-progress loop;
- budget boundary;
- unsafe/destructive command;
- proof failure after bounded retry;
- stale/active run problem;
- environment blocker;
- requested human approval.

Final gate should evaluate:

- acceptance criteria coverage;
- changed files;
- focused proof results;
- visual proof relevance when required;
- known risks;
- unrelated failures separately classified;
- QA verdict.

This should reduce churn and make the Dev stage feel like a normal coding agent run: inspect, patch, test, revise, deliver.

## Proposed User Flow

### Live Product Goal

```powershell
python -m hermes_cli.main harness goal run `
  --title "Make post images 3x bigger" `
  --description "Launcher posts UI goal..." `
  --monitor `
  --json
```

Expected behavior:

- creates a task;
- runs Neko planning;
- runs Dev;
- runs QA when proof exists;
- completes task when QA passes;
- exits with code `0` when terminal `done`;
- exits nonzero when blocked, waiting on approval, runtime unhealthy, or budget exhausted;
- emits a redaction-safe summary with task id, run ids, proof ids, incidents, final state, elapsed time, and stop reason.

### Manual Debug Goal

```powershell
python -m hermes_cli.main harness goal run `
  --title "Debug mission control logs" `
  --description "..." `
  --max-actions 8 `
  --no-archive `
  --json
```

Expected behavior:

- foreground controller owns all ticks;
- no background daemon start;
- no hidden lifecycle;
- stopped state is explicit.

### Create-Only Compatibility

```powershell
python -m hermes_cli.main harness task create --title "..." --description "..." --no-start-daemon --json
```

This remains available for tests, manual state-machine debugging, and Launcher/UI flows that should enqueue into a supervised service later.

## Architecture

### Goal Runner Controller

Add `agent_runtime.goal_runner.MissionRuntimeController`.

Responsibilities:

- prepare new-goal runtime hygiene;
- create the task;
- optionally preflight runtime health;
- call the existing `TickEngine.run_until_settled(task_id=...)` API;
- poll/stream redaction-safe status between controller cycles;
- detect stop boundaries;
- perform optional terminal archive;
- return a stable `GoalRunResult`.

The controller must call existing runtime APIs instead of duplicating state-machine logic. Any missing stop boundary must be fixed in `TickEngine.run_until_settled` or its helper methods, not in a parallel controller scheduler.

### Stop Boundaries

The controller stops when any of these are true:

- task state is `done`;
- task state is `blocked`;
- open incident exists for the task;
- active run is `waiting_on_approval`;
- run budget or wall-clock budget reached;
- runtime health becomes critical;
- no progress repeats exceed policy;
- operator interrupt occurs.

The controller should preserve raw settler stop reasons and add a normalized goal-level reason. Examples:

- raw `task_terminal` + final task state `done` => goal `task_done`
- raw `task_terminal` + final task state `cancelled` => goal `task_cancelled`
- raw `task_blocked` => goal `task_blocked`
- raw `incident_opened` => goal `open_incident`
- raw `waiting_on_approval` => goal `waiting_on_approval`
- raw `active_run` => goal `active_run_boundary`
- raw `action_failed` => goal `action_failed`
- raw `max_actions` => goal `max_actions`
- raw `max_seconds` => goal `max_seconds`
- raw `no_eligible_action` => goal `no_eligible_action`
- raw `tick_lock_unavailable` => goal `tick_lock_unavailable`
- interrupt => goal `operator_interrupt`
- unhandled exception => goal `error`

### Exit Codes

- `0`: task reached `done`.
- `1`: task blocked, incident opened, waiting on approval, or QA rejected.
- `2`: controller/runtime error.
- `3`: action/time budget boundary reached before terminal state.
- `130`: operator interrupt.

### Relationship To Daemon

The daemon remains useful for an always-on Mission Control service. Stage 53 should stop treating daemon spawn as the normal CLI live-goal path.

After Stage 53:

- `harness goal run` is the preferred CLI live-goal command.
- `harness task create --no-start-daemon` is the explicit create-only primitive.
- `harness daemon start` remains a service-mode/manual command.
- The temporary `task create` auto-start bridge should either:
  - become opt-in only, or
  - remain default only behind a config flag until Launcher has a service-backed bridge.

Recommended final policy:

```yaml
task_create_auto_start_daemon: false
goal_run_uses_in_process_controller: true
launcher_mission_control_requires_runtime_service: future
```

## CLI Shape

Add a new command group:

```powershell
python -m hermes_cli.main harness goal run --title ... --description ... [options]
```

Options:

- `--title`
- `--description`
- `--requested-by`
- `--max-actions`
- `--max-seconds`
- `--monitor`
- `--json`
- `--archive-on-done`
- `--archive-on-cancelled-test-goal`
- `--no-archive`
- `--requires-visual-proof`
- `--affected-repo`
- `--acceptance`
- `--non-goal`

Do not overload `task create` with too many orchestration flags. `task` commands manage persisted tasks. `goal run` owns live execution.

## Result Shape

Redaction-safe JSON:

```json
{
  "ok": true,
  "task_id": "task_123",
  "title": "Make post images 3x bigger",
  "final_state": "done",
  "stop_reason": "task_done",
  "settler_stop_reason": "task_terminal",
  "elapsed_seconds": 312.4,
  "actions_taken": 6,
  "ticks": 4,
  "run_ids": ["run_neko", "run_dev", "run_qa"],
  "proof_ids": ["proof_1", "proof_2"],
  "incident_ids": [],
  "archive_batch": null,
  "repo_dirty_summary": "EterniaLauncher dirty",
  "next_actions": []
}
```

For blocked runs:

```json
{
  "ok": false,
  "task_id": "task_123",
  "final_state": "blocked",
  "stop_reason": "open_incident",
  "settler_stop_reason": "incident_opened",
  "incident_ids": ["inc_123"],
  "blocked_summary": "QA blocked: visual proof did not cover image rows",
  "next_actions": [
    "attach deterministic posts fixture screenshot",
    "rerun qa_release"
  ]
}
```

## Monitoring UX

With `--monitor`, print compact redaction-safe progress lines:

```text
[00:00] task created task_123
[00:04] neko planning run_aaa started
[01:12] neko released launcher_implementation
[01:13] dev started run_bbb
[06:40] dev proof attached proof_test_1
[06:42] qa started run_ccc
[07:55] qa approved
[07:56] task done
```

Never print hidden provider chain-of-thought. Print only Harness event summaries, tool events, safe reasoning summaries, and proof metadata.

## Implementation Readiness Audit

Existing implementation pieces to reuse:

- `agent_runtime.ticker.TickEngine.run_until_settled(...)` is the scheduler primitive.
- `hermes_cli.harness._cmd_run_until_settled(...)` already proves the CLI can call the settler with live `GPTPersonaRuntime`.
- `agent_runtime.daemon.MissionDaemon` already wraps the settler for background loops.
- `agent_runtime.burn_in.run_burn_in_case(...)` already uses the settler with fake/live engines.
- `agent_runtime.goal_hygiene.prepare_new_goal_runtime(...)` already owns new-goal cleanup.
- `agent_runtime.status.build_status(...)` and `agent_runtime.snapshot.build_snapshot(...)` already produce redaction-safe runtime state.
- `agent_runtime.events.EventLog` already persists raw event records.
- `agent_runtime.store.TaskStore.archive(...)` and `archive_ready(...)` already preserve evidence under `deleted_archive/`.

Implementation risks to avoid:

- Do not add a second scheduler loop that duplicates `TickEngine`.
- Do not make agents responsible for raw legacy state transitions.
- Do not make `goal run` spawn a daemon.
- Do not make `task create` collect goal-run options such as archive policy, monitor UX, or exit-code semantics.
- Do not hide blocked/waiting/action-failed outcomes behind exit code `0`.
- Do not make Launcher terminal depend on parsing raw JSON blobs when a normalized worklog event can be produced.

## Implementation Stages

### 53A - Goal Runner Core

Create `agent_runtime/goal_runner.py`.

Dataclasses:

```python
@dataclass(slots=True)
class GoalRunOptions:
    title: str
    description: str
    requested_by: str = "cli"
    max_actions: int = 12
    max_seconds: float | None = None
    monitor: bool = False
    archive_on_done: bool = False
    no_archive: bool = False
    requires_visual_proof: bool = False
    affected_repos: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
```

```python
@dataclass(slots=True)
class GoalRunResult:
    ok: bool
    task_id: str
    title: str
    final_state: str | None
    stop_reason: str
    settler_stop_reason: str
    exit_code: int
    elapsed_seconds: float
    actions_taken: int
    ticks: int
    run_ids: list[str]
    proof_ids: list[str]
    incident_ids: list[str]
    archive_batch: str | None
    dirty_summary: str
    blocked_summary: str | None
    next_actions: list[str]
```

`MissionRuntimeController.run_goal(options)` must:

1. load config with `load_agent_runtime_config()`;
2. call `prepare_new_goal_runtime(...)` once;
3. create a `Task` through `TaskStore.create(...)`;
4. apply simple metadata from options without exposing legacy state to agents:
   - `requires_visual_proof`;
   - `affected_repos`;
   - `acceptance_criteria`;
   - `non_goals`;
5. emit a `persona.worklog` controller event for goal creation;
6. construct or receive a `TickEngine`;
7. call `TickEngine.run_until_settled(task_id=task.id, max_actions=..., max_seconds=...)`;
8. normalize raw settler stop reason into goal-level stop reason;
9. build a summary from `TaskStore`, `RunStore`, `ProofStore`, `IncidentStore`, `build_status`, and recent `EventLog` items;
10. optionally archive terminal tasks;
11. return `GoalRunResult`.

Anti-duplication assertion:

- `MissionRuntimeController` may call `run_until_settled`.
- It must not call `MissionStateMachine.next_action`.
- It must not call `TickEngine.tick_once` directly except through injected test doubles that expose only `run_until_settled`.

Tests:

- `tests/agent_runtime/test_goal_runner.py::test_goal_runner_creates_task_and_calls_hygiene_once`
- `test_goal_runner_delegates_to_run_until_settled`
- `test_goal_runner_does_not_spawn_daemon`
- `test_goal_runner_normalizes_task_terminal_done`
- `test_goal_runner_normalizes_incident_opened`
- `test_goal_runner_normalizes_waiting_on_approval`
- `test_goal_runner_collects_runs_proofs_incidents`
- `test_goal_runner_archive_on_done_preserves_evidence`
- `test_goal_runner_no_archive_leaves_done_task_open`

Acceptance:

- fake-engine tests prove the controller can drive a task to `done` with no manual tick command;
- blocked/waiting/action-failed results have `ok=false` and nonzero `exit_code`;
- result contains both `settler_stop_reason` and normalized `stop_reason`.

### 53B - CLI Product Command

Patch `hermes_cli/harness.py`.

Parser shape:

```powershell
harness goal run --title ... --description ... --requested-by ... --max-actions 12 --max-seconds 900 --monitor --json
```

Implementation:

- add `goal = subs.add_parser("goal", ...)`;
- add `goal_run = goal_subs.add_parser("run", ...)`;
- keep `harness run-until-settled` unchanged as the low-level diagnostic command;
- call `MissionRuntimeController.run_goal(...)`;
- print `GoalRunResult` as JSON or human summary;
- return `result.exit_code`.

Do not call:

- `start_daemon`;
- `MissionDaemon`;
- `TickEngine.tick_once` directly;
- `harness task create` subprocess.

Tests in `tests/hermes_cli/test_harness_cli.py`:

- parser exposes `harness goal run`;
- CLI maps args to `GoalRunOptions`;
- CLI returns `0` for done result;
- CLI returns `1` for blocked/waiting result;
- CLI returns `3` for max-actions/max-seconds result;
- CLI does not call `start_daemon`;
- JSON output includes `task_id`, `stop_reason`, `settler_stop_reason`, `exit_code`.

Acceptance:

- `python -m hermes_cli.main harness goal run --help` documents the new product command;
- `harness task create --help` remains focused on task persistence;
- existing `run-until-settled` tests still pass.

### 53C - Config And Transition From Daemon Auto-Start Bridge

Patch config model in `agent_runtime/runtime_config.py` or the existing config source that owns daemon options.

Add:

```yaml
task_create_auto_start_daemon: false
preferred_live_goal_mode: in_process_controller
```

Migration behavior:

- default new config to `task_create_auto_start_daemon=false`;
- keep `--start-daemon` and `--no-start-daemon` flags for compatibility;
- make `task create` default follow config rather than hard-coded daemon start;
- `harness goal run` ignores daemon auto-start config and always runs in-process.

Tests:

- task create does not start daemon when config disables bridge;
- task create starts daemon when flag/config explicitly enables bridge;
- goal run does not start daemon even if bridge config is enabled;
- status remains clean after create-only no-start smoke cleanup.

Acceptance:

- the temporary Stage 52 bridge becomes compatibility, not the preferred path;
- no existing Mission Control UI path breaks before Stage 54 service work.

### 53D - Worklog Capture Layer

Add a small module such as `agent_runtime/worklog.py`.

Responsibilities:

- create normalized `persona.worklog` events;
- derive worklog messages from existing raw event payloads;
- preserve raw events unchanged;
- provide helper `append_worklog(...)`;
- provide helper `worklog_for_task(task_id, persona_id=None, since=None, limit=...)`.

Event sources:

- `agent_progress`;
- `safe_reasoning_summary`;
- `tool_event`;
- `proof_event`;
- `state_transition`;
- `validator_feedback`;
- `controller_summary`.

Integration points:

- controller emits `controller_summary` worklog on create/start/stop;
- `TickEngine` emits/derives worklog for run start/close, proof attach, invalid packet, blocker, and QA verdict;
- do not alter raw `run.progress`, `packet.recorded`, `proof.attached`, or `task.transition` events.

Tests:

- appending worklog preserves raw event references;
- worklog event has required fields;
- worklog text is stored separately from raw JSON;
- invalid packet produces allowed-action guidance;
- budget/waiting/blocker event produces next-action guidance;
- worklog query can filter by persona.

Acceptance:

- every persona can have a useful terminal even when no raw run JSON exists yet;
- empty persona state is explicit, not visually blank.

### 53E - Simplified Role Contract Projection

Add projection helpers rather than rewriting persona runtime immediately:

- `agent_runtime/role_contracts.py`
- `build_neko_assignment_contract(task, status, evidence)`
- `build_dev_assignment_contract(task, stage, proof_recipe)`
- `build_qa_verification_contract(task, proofs, visual_recipe)`

Purpose:

- hide legacy task states and stage mutation rules;
- expose only `allowed_actions`;
- include exact valid response shape;
- include acceptance, likely paths, missing evidence, and budgets;
- keep role envelope and typed mission internals private.

Initial implementation may wrap existing decision schemas:

- map `assign` to current Neko planning/assignment decision internally;
- map Dev `deliver` to existing valid implementation delivery/propose-patch path;
- map QA `approve/reject/request_missing_proof` to current QA verdict path.

Do not require a complete schema replacement in the first commit. The enterprise rule is that the prompt/HUD seen by agents becomes simple, while adapters preserve current state-machine compatibility.

Tests:

- Neko projection does not contain legacy state names or checklist mutation fields;
- Dev projection contains only `deliver/report_blocker/request_missing_input`;
- Backend Dev projection matches Dev shape but backend repo/proof policy;
- QA projection contains only `approve/reject/request_missing_proof`;
- projection includes missing visual proof when visual proof is required;
- projection includes unrelated-failure classification input when present.
- projection includes cross-role missing-input routing choices when another persona can answer.

Acceptance:

- agents see one small action menu per role;
- legacy fields remain internal only.

### 53F - Cross-Role Missing Input Router

Add `agent_runtime/missing_input.py`.

Responsibilities:

- validate `request_missing_input` payloads;
- classify missing input type;
- select responder persona;
- persist request/route/answer records;
- return compact answers to the original worker;
- prevent vague or repeated context stalls;
- escalate to Tony only when no persona/Harness preflight can answer safely.

Dataclasses:

```python
@dataclass(slots=True)
class MissingInputRequest:
    id: str
    task_id: str
    from_persona: str
    missing_input_type: str
    preferred_responder: str | None
    why_blocking: str
    minimum_needed: str
    attempted_self_service: list[str]
    created_at: datetime
    status: str
```

```python
@dataclass(slots=True)
class MissingInputRoute:
    request_id: str
    to_persona: str
    return_to_persona: str
    reason: str
```

```python
@dataclass(slots=True)
class MissingInputAnswer:
    request_id: str
    answered_by: str
    answer_summary: str
    evidence_refs: list[str]
    confidence: str
    remaining_uncertainty: str
```

Routing table:

| Missing input type | Preferred route | Fallback route | Human escalation |
| --- | --- | --- | --- |
| `backend_contract` | Backend Dev | Neko | Only if backend intent is product-ambiguous |
| `frontend_usage` | Launcher Dev | Neko | Only if frontend intent is product-ambiguous |
| `visual_verification` | QA | Neko | Only if visual acceptance is a product decision |
| `scope_decision` | Neko | none | Yes, if Neko cannot infer safely |
| `proof_gap` | QA or Harness proof registry | Neko | Rare |
| `environment_blocker` | Harness preflight/self-heal | Dev if code/tool repair needed | If external setup is required |
| `user_decision` | Neko first | none | Yes |

Validation rules:

- `missing_input_type` must be one of the allowed enum values.
- `why_blocking` and `minimum_needed` are required and non-empty.
- Vague text such as "need more context" without a concrete minimum is invalid.
- `attempted_self_service` is required unless type is `scope_decision`, `user_decision`, or the responder is obviously another role.
- Duplicate open requests from the same persona/type/minimum hash are deduped and linked to the original request.
- A responder cannot route the same request back to the requester without adding an answer or blocker.

State-machine integration:

- `request_missing_input` should not set the task to terminal blocked by default.
- The current worker session pauses with `waiting_on_missing_input`.
- The responder role receives a bounded assignment.
- After `answer_missing_input`, the original worker resumes with the answer in its `agent_hud`.
- If the responder reports blocker, route to Neko.
- If Neko reports `user_decision`, stop the goal runner with `stop_reason=user_decision_required`.

Worklog integration:

- create `persona.worklog` entry when request is opened;
- create worklog entry when routed;
- create worklog entry when answered;
- create worklog entry when escalated.

Tests:

- Launcher Dev `backend_contract` routes to Backend Dev and returns to Launcher Dev.
- Backend Dev `frontend_usage` routes to Launcher Dev and returns to Backend Dev.
- Dev `visual_verification` routes to QA.
- QA `scope_decision` routes to Neko.
- `environment_blocker` routes to Harness preflight/self-heal before model work.
- vague missing input is rejected with allowed examples.
- duplicate missing input request is deduped.
- responder answer is injected into original worker HUD.
- unresolved Neko `user_decision` stops goal runner with nonzero exit.

Acceptance:

- missing input becomes autonomous cross-role steering, not a default user escalation;
- no worker is allowed to stall with vague context requests;
- Launcher terminal clearly shows who asked, who answered, and what evidence supports the answer.

### 53G - HUD And Skill Injection Cleanup

Current HUD/skill seams to audit and patch:

- `agent_runtime/context_builder.py` builds `mission_hud`, including `decision_menu`, `decision_shape_index`, `next_required_move`, repair HUD fields, and normal worker flow hints.
- `agent_runtime/autonomy.py` records autonomy packets, selected skills, rejected skills, and prompt contracts.
- `agent_runtime/personas.py` declares persona skill lists.
- `agent_runtime/skill_context.py` loads persona skill context under the per-persona char cap.
- `agent_runtime/decision_contract_registry.py` and `decision_contracts.py` still expose canonical shape menus.
- `tests/agent_runtime/test_context_builder.py` contains the current HUD expectations for Dev, Neko, and QA.
- `tests/agent_runtime/test_autonomy.py` contains skill-selection and prompt-contract expectations.

Stage 53 must not simply remove the HUD. It must replace the agent-facing HUD with a smaller product HUD while preserving raw/internal contract data for debugging.

New HUD layers:

1. `operator_hud`: Launcher/Mission Control display model.
2. `agent_hud`: tiny role-specific assignment and action menu injected into prompts.
3. `debug_hud`: expandable internal contract/state/proof details for developers.

Agent HUD requirements:

- show only the current assignment;
- show only role-valid actions;
- show exact response shape;
- show acceptance criteria;
- show relevant evidence and missing evidence;
- show tool/proof budget;
- show next expected outcome;
- hide legacy task state names;
- hide typed/legacy projection internals;
- hide raw checklist mutation fields;
- hide archive/daemon lifecycle fields;
- hide packet shape menus unless the task is explicitly Harness-contract development.

Role action menus:

- Neko: `assign`, `report_blocker`, `request_missing_input`.
- Launcher Dev: `deliver`, `report_blocker`, `request_missing_input`.
- Backend Dev: `deliver`, `report_blocker`, `request_missing_input`.
- QA: `approve`, `reject`, `request_missing_proof`.

Invalid-output repair:

- Keep validation repair, but make it product-shaped.
- Instead of returning a large closed payload contract, return:
  - invalid field;
  - why it was invalid;
  - allowed actions;
  - one corrected minimal example;
  - whether the Harness can self-heal or needs the same persona to retry.
- Invalid output must produce a `persona.worklog` item so the Launcher terminal shows what happened.

Skill injection requirements:

- Skills should be selected by assignment context, not dumped from persona defaults.
- Default profile skills remain installed, but the autonomy packet should recommend a tiny set:
  - one always-on Harness operating skill for the role;
  - zero to two task-specific skills;
  - no broad skill fanout unless the task explicitly asks for research/planning.
- Neko gets only mission-lead/routing skill context by default.
- Dev gets only delivery + repo-specific implementation skill context by default.
- Backend Dev gets backend delivery/proof skill context, not Launcher visual skills.
- QA gets QA verdict + visual proof skill context when visual proof is required.
- Skill search remains available, but the agent should not browse skills before attempting a narrow obvious task.

Skill source-of-truth:

- Existing Stage 46 repo skill packages under `docs/agent-runtime-harness/stage46-skills/` remain the versioned source of truth.
- Stage 53 should either update those skills or add Stage 53 skill versions under a new `docs/agent-runtime-harness/stage53-skills/` directory.
- Runtime installed profile skills are deployment targets, not source of truth.
- Add or extend an installer/hash verifier so persona readiness can detect stale installed skills.

Specific skill updates:

- `harness-mission-lead`: remove legacy packet/stage mutation instructions; teach Neko the minimal `assign/report_blocker/request_context` contract and scope-quality checklist.
- `harness-dev-delivery`: remove QA handoff ceremony and proof attachment mechanics; teach `deliver/report_blocker/request_context`, final outcome delivery, and bounded patch/test/revise.
- `launcher-analyze-proof`: keep as a targeted proof helper, but ensure it is injected only when relevant to Launcher analyze/test proof.
- `harness-qa-verdict`: remove proof plumbing/stage mutation; teach `approve/reject/request_missing_proof` and proof-strength classification.
- `launcher-stagec-mcp-screenshot`: inject only when visual proof is required or QA requests missing visual proof.
- missing-input routing helper skill/instructions should be in the always-on Harness operating skill, not a broad extra skill.

Tests:

- Neko `agent_hud` exposes only `assign/report_blocker/request_missing_input`.
- Dev `agent_hud` exposes only `deliver/report_blocker/request_missing_input`.
- Backend Dev `agent_hud` does not include Launcher visual proof skill unless task requires cross-stack visual proof.
- QA `agent_hud` exposes only `approve/reject/request_missing_proof`.
- missing-input answer appears in original worker `agent_hud` as a compact continuation note.
- Legacy `decision_menu` remains available in `debug_hud`, not the main agent HUD.
- Skill selection caps default injected skills to role operating skill plus at most two task-specific skills.
- Invalid-output repair HUD returns a minimal corrected example, not a full legacy shape dump.
- Autonomy packet records selected/rejected skills and why rejected skills were withheld.

Acceptance:

- agents can read their prompt and know exactly what to do without knowing Harness legacy internals;
- Launcher terminal can render a clean operator HUD while raw/debug HUD remains expandable;
- token use drops because persona prompts no longer include broad legacy contract menus by default.

### 53H - Final Outcome Gate And Proof Summary

Add goal-level proof/acceptance summarization, likely in `agent_runtime/goal_runner.py` or a helper `agent_runtime/goal_summary.py`.

Summary must classify:

- focused command proof passed/failed/missing;
- visual runtime proof passed/failed/missing;
- visual acceptance proof passed/failed/missing;
- unrelated failures;
- invalid packets;
- missing input requests/routes/answers;
- manual interventions;
- acceptance math/claim coverage, when applicable;
- final QA verdict.

This stage changes product behavior:

- Dev can patch/test/revise inside one run;
- QA/Neko do not inspect every patch;
- final gate evaluates delivered outcome and proof coverage.

Tests:

- weak visual proof produces caveat, not full visual acceptance;
- unrelated test failure is reported separately from task blocker;
- invalid packet appears in final summary with next action;
- cross-role missing input appears in final summary with requester/responder/answer evidence;
- manual intervention count is surfaced;
- final done result with weak proof can still include caveat if QA approved command proof.

Acceptance:

- result summary is truthful enough that Tony can understand what happened without opening raw JSON;
- no "done" claim hides proof caveats.

### 53I - Launcher Mission Control UI Bridge Prep

This stage does not have to implement the full Launcher UI, but it must produce data the UI can render.

Harness-side API/CLI requirements:

- `harness task show <task_id> --events ... --json` or snapshot/status includes `persona.worklog` events;
- all personas can be listed with state:
  - `not_assigned`;
  - `waiting`;
  - `running`;
  - `blocked`;
  - `done`;
- worklog entries include expandable raw refs.
- missing input routes are visible as worklog bubbles with requester/responder/return-to state.

Launcher implementation notes for later:

- render per-persona DM bubbles;
- default selected persona should not hide others' logs;
- empty persona state must say "No run assigned yet" or "Waiting for Neko release";
- raw JSON appears behind disclosure controls;
- screenshots/proofs link from worklog rows.

Tests:

- snapshot/status includes worklog or a query path for all personas;
- persona with no logs still has explicit display state;
- QA worklog can display proof caveat.
- missing input worklog shows cross-role answer and evidence refs.

Acceptance:

- Stage 53 backend data is enough for Launcher to stop showing one persona only while others look empty/frozen.

### 53J - Live Certification

Run live certification only after 53A-53G automated tests pass.

Live goal 1: no-product-edit Harness certification.

- Use `harness goal run`.
- Must require no manual tick after command starts.
- Must end `done` or blocked with actionable incident.
- Must produce worklog entries for Neko, Dev or QA as applicable.

Live goal 2: small Launcher UI goal with focused proof.

- Use `harness goal run --monitor`.
- Must not rely on daemon.
- Must produce per-persona worklog.
- Must distinguish command proof from visual proof.
- If visual fixture is empty/weak, final summary must say so.

Evidence to record:

- command used;
- exit code;
- elapsed time;
- final `GoalRunResult`;
- task id;
- run ids;
- proof ids;
- incident ids;
- archive batch if used;
- final `harness status --json` dirty summary;
- manual intervention count.

Pass criteria:

- no manual ticks after goal runner starts;
- no hidden daemon process required;
- terminal state reached or blocked with actionable incident;
- final summary explains why it cannot proceed if blocked;
- Launcher UI has enough worklog data to render all personas.

## Tests

Required automated coverage:

- parser exposes `harness goal run`;
- goal runner creates task and calls hygiene once;
- goal runner uses `TickEngine.run_until_settled`;
- goal runner does not implement its own state-machine routing;
- goal runner preserves raw `settler_stop_reason`;
- goal runner stops on `done`;
- goal runner stops on open incident;
- goal runner stops on waiting run;
- goal runner stops on active run boundary with non-success exit unless explicitly told to wait;
- goal runner stops on max actions;
- goal runner maps exit codes correctly;
- `--monitor` streams each event once;
- redaction test for monitor output;
- archive-on-done preserves evidence;
- task-create create-only mode does not start daemon when config disables bridge;
- no duplicate daemon spawned by `goal run`.

## Risks

- Running in-process means the command owns the foreground. That is good for CLI but not enough for GUI-only operation.
- Long live-token goals can still exceed terminal patience. The monitor output must make progress visible.
- Existing daemon auto-start bridge may conflict conceptually. Keep compatibility during transition, then make auto-start opt-in.
- If `TickEngine.run_until_settled` stop reasons are too coarse, Stage 53 must extend them rather than inventing a parallel scheduler.
- The current `run-until-settled` command exits `0` for blocked/waiting/failure boundaries. `goal run` must not inherit that operator ambiguity.

## Open Decisions

1. Should `harness task create` default daemon auto-start be reverted immediately after `goal run` lands, or remain behind a compatibility config flag for Mission Control UI?
2. Should `harness goal run` auto-archive successful smoke/test goals by default when `requested_by` starts with `stage` or `codex_*_smoke`?
3. Should Launcher Mission Control call `harness goal run` for foreground jobs, or should it wait for the future supervised service API?

## Recommended Decision

Implement Stage 53 before adding a Windows Service. It gives Tony the practical behavior he wants now:

- one command;
- one controller;
- same process;
- clear monitor output;
- no hidden daemon babysitting;
- bounded self-driving execution.

Then reserve Stage 54 for the true enterprise always-on service:

- Windows Service or Hermes service supervisor;
- runtime API;
- Launcher Mission Control service health and queue UX;
- task creation as enqueue-only.

## Implementation Ledger

Implemented in this stage:

- `agent_runtime.goal_runner.MissionRuntimeController` creates a goal, runs new-goal hygiene, calls the existing `TickEngine.run_until_settled`, maps stop boundaries to truthful exit codes, records controller worklog events, and returns a goal-level receipt.
- `hermes harness goal run` is the product command for foreground in-process execution.
- `harness task create` is create-only by default again; daemon start is explicit with `--start-daemon` or `task_create_auto_start_daemon: true`.
- `persona.worklog` and `missing_input.requested` are registered event contracts.
- `agent_runtime.worklog` provides task/persona-filterable DM-style worklog events.
- `agent_runtime.missing_input` validates and routes cross-role missing input before escalating to the operator.
- `agent_runtime.role_contracts` defines the simplified Stage 53 agent-facing contracts.
- `context_builder` now includes `mission_hud.agent_hud` as the simplified worker-facing projection while keeping legacy `decision_menu` in the same HUD for debug/compatibility.

Automated coverage added or updated:

- goal runner done and max-action boundary receipts;
- parser and CLI exit-code propagation for `harness goal run`;
- task-create no longer starts the daemon by default and still supports explicit daemon start;
- simplified role contracts for Neko, Launcher Dev, Backend Dev, and QA;
- missing-input routing and validation;
- persona worklog append/filter behavior;
- context-builder simplified `agent_hud` projection.

Remaining follow-on work for Stage 54:

- true live streaming monitor output during long model/tool calls;
- Launcher UI rendering of `persona.worklog` as per-persona DM bubbles;
- richer final proof-strength summary for command proof vs visual runtime proof vs visual acceptance proof;
- optional supervised service/Windows Service for GUI-only queue ownership.
