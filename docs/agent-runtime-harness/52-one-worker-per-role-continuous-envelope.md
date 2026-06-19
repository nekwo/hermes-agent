# Stage 52 One Worker Per Role Continuous Envelope

Date: 2026-06-07
Owner: Codex independent Harness investigation
Status: implementation plan ready
Depends on: Stage 50 normal worker flow, Stage 51 typed mission plan simplification

## Purpose

Stage 52 makes the Harness behave like one uninterrupted competent worker per role:

```text
Neko scopes and releases work.
Dev owns implementation from inspect through self-test, final gate repair, and delivery.
QA owns independent verification and final release verdict.
```

The goal is not to make everything unlimited. The goal is to stop chopping a coherent job
into too many tiny agent turns. Each role should keep its session, checklist, context,
proof batch, and recovery state for as long as there is real progress.

Core principle:

```text
Keep rich internal states. Simplify the agent contract. Give each role a durable task list.
```

The Harness may keep typed mission plans, proof stores, worker sessions, incidents,
watchdogs, dirty-state markers, archive manifests, and Mission Control event streams.
The agents should see a small worker surface:

- their role checklist
- the current checklist item
- allowed next actions
- the exact done criteria
- the exact blocker criteria
- the exact packet shape

## Why This Stage Exists

Stage 50 and Stage 51 simplify the agent-facing decision menu and make typed stages the
routing authority. Live-token testing showed the next bottleneck:

- Dev can still be interrupted before it has completed a natural inspect/edit/test loop.
- QA can re-enter too many times if the Harness does not close the QA envelope after a
  needs-fixes verdict.
- Neko can be asked to steer too often when the right answer is to let the current role
  finish its checklist.
- The HUD shows valid packet shapes, but it does not yet feel like the role's own task
  list with self-checkable completion.
- Mission Control can show process summaries, but it should also explain why the Harness
  continued, paused, rerouted, or asked for QA.

Stage 52 makes continuation a first-class contract.

## Non-Goals

- Do not expose hidden provider chain-of-thought. Show safe reasoning summaries,
  decisions, checklist updates, tool calls, diffs, command logs, proof summaries, and
  validation repairs.
- Do not let Dev self-approval replace QA approval.
- Do not let self-test evidence replace Harness final proof gates.
- Do not collapse Neko, Launcher Dev, Backend Dev, and QA into one persona.
- Do not make Stage 52 default-on for standard Hermes profiles.
- Do not add a large new public decision protocol if the existing decisions can carry the
  simplified worker actions.

## Product Decision: Role Checklist With Scoped Self-Approval

Each role gets a durable checklist in the HUD.

The checklist is not a generic todo note. It is typed runtime state attached to the
current mission plan stage and worker envelope.

Self-approval rules:

- Neko may approve its own planning checklist item when the typed mission plan is valid,
  preserves parent acceptance criteria, and has a next unblocked stage.
- Dev may approve its own implementation checklist items when it has recorded code edits,
  self-test evidence, and either requested or passed the required final gate.
- Backend Dev follows the same Dev rules, scoped to backend/proof-only stages.
- QA may approve the release checklist only after it has independently reviewed required
  command proof, visual proof when required, and typed stage coverage.
- Harness may auto-advance within the same role envelope after checklist self-approval,
  but only QA can approve the full goal.

This mirrors how strong single-agent harnesses feel: the worker tracks its own checklist,
marks work done, and keeps going. The Harness still enforces the final gates.

## Agent-Facing Options After Stage 52

Do not expand the menu. Keep the simplified Stage 51 actions and attach them to checklist
items.

Neko normal options:

- `set_or_repair_plan`
- `release_next_stage`
- `route_recovery`
- `block`

Dev and Backend Dev normal options:

- `deliver_patch`
- `request_gate`
- `request_context`
- `block`

QA normal options:

- `request_missing_proof`
- `report_verdict`
- `block`

Checklist state changes are payload fields on these actions, not new top-level decisions.
For example, Dev can emit `deliver_patch` with `checklist_updates` and
`self_approval_status=ready_for_gate`.

## Stage 52A. Role Envelope Model

Add a first-class role envelope persisted per task, role, typed stage, and worker session.

Suggested model:

```python
@dataclass(slots=True)
class RoleEnvelope:
    envelope_id: str
    task_id: str
    role_id: str
    worker_session_id: str | None
    mission_stage_id: str | None
    phase: str
    status: str
    started_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    checklist_id: str | None = None
    proof_batch_id: str | None = None
    last_decision_type: str | None = None
    last_progress_hash: str | None = None
    last_qa_finding_hash: str | None = None
    continuation_count: int = 0
    no_progress_count: int = 0
    repair_count: int = 0
    close_reason: str | None = None
```

Allowed `phase` values:

- `planning`
- `inspection`
- `implementation`
- `self_test`
- `final_gate`
- `qa_review`
- `fix_repair`
- `recovery`
- `blocked`
- `complete`

Allowed `status` values:

- `open`
- `continuing`
- `waiting_for_gate`
- `waiting_for_qa`
- `needs_fix`
- `blocked`
- `closed`

Rules:

- Only one open envelope per role and typed stage.
- A role envelope keeps the same worker session while there is progress.
- A new envelope is opened for a new typed stage, new owner, or QA needs-fixes handback.
- Closed envelopes remain visible in Mission Control and archived manifests.

Implementation-ready tests:

- opening a Dev envelope persists role, typed stage, and worker session IDs.
- resuming a stage reuses the open envelope instead of creating a duplicate.
- QA needs-fixes closes the QA envelope and opens a Dev fix envelope.
- archive-ready task manifests include role envelope records.

## Stage 52B. Durable Role Checklist

Add a first-class checklist model attached to the role envelope.

Suggested model:

```python
@dataclass(slots=True)
class RoleChecklistItem:
    item_id: str
    title: str
    owner_role: str
    required: bool = True
    status: str = "pending"
    done_criteria: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    self_approved_at: datetime | None = None
    self_approved_by_run_id: str | None = None
    blocked_reason: str | None = None

@dataclass(slots=True)
class RoleChecklist:
    checklist_id: str
    task_id: str
    mission_stage_id: str | None
    role_id: str
    items: list[RoleChecklistItem] = field(default_factory=list)
    status: str = "active"
    revision: int = 0
```

Checklist item statuses:

- `pending`
- `in_progress`
- `self_approved`
- `verified`
- `needs_fix`
- `blocked`
- `skipped_with_reason`

Default checklist templates:

Neko planning checklist:

- preserve parent mission intent
- create typed stages and dependencies
- identify proof/visual requirements
- release next unblocked owner

Dev implementation checklist:

- inspect target files/logs
- patch the product code
- run focused self-test
- attach self-test evidence
- request or satisfy final gate
- hand off to QA

Backend Dev proof-only checklist:

- inspect requested contract/runtime state
- request exact proof recipe
- verify proof result is attached to typed stage
- hand off to Neko or QA according to dependencies

QA checklist:

- verify all blocking typed stages are ready
- verify required command proof IDs
- verify required visual proof IDs
- review final UI/runtime behavior
- issue final verdict with cited proof coverage

Implementation-ready tests:

- checklist templates are generated from typed stage owner/kind.
- role-invalid checklist items are not rendered in the HUD.
- Dev self-approval cannot mark a QA item verified.
- QA approval requires evidence refs for every required proof item.
- skipped checklist items require a redaction-safe reason.

## Stage 52C. HUD Checklist Projection

The HUD should render a compact checklist section before the packet skeleton.

HUD shape:

```text
ROLE TASK LIST
Stage: launcher_terminal_ui_fix
Envelope: dev / implementation / continuing
Current item: run focused self-test

[done] inspect target files/logs
[done] patch product code
[in_progress] run focused self-test
[pending] attach self-test evidence
[pending] request or satisfy final gate
[pending] hand off to QA

Allowed next actions:
1. deliver_patch
2. request_gate
3. request_context
4. block

Done criteria for current item:
- focused command exits 0
- command output is captured as self-test evidence
- no unrelated files changed
```

HUD rules:

- Render only the current role's checklist by default.
- Render dependency summaries for other roles, not full foreign checklists, unless the
  current role is Neko or QA.
- Render exact enum choices for status updates.
- Render the smallest valid packet skeleton for the current action.
- If a packet is invalid, return the same checklist with exact repair instructions.
- Do not dump large logs into the HUD. Show artifact IDs and let the agent request
  context when needed.

Implementation-ready tests:

- HUD includes current checklist item and done criteria.
- HUD lists only role-allowed actions.
- invalid packet repair includes allowed checklist status enum choices.
- prompt rendering caps foreign checklist summaries.
- context size remains bounded when many checklist items exist.

## Stage 52D. Same-Session Continuation Policy

Continue the same role envelope when any progress signal changed:

- code diff changed
- self-test evidence changed
- proof batch status changed
- checklist item status changed
- QA finding hash changed
- environment blocker signal changed
- valid decision advanced the typed stage

Interrupt or reroute only when there is evidence:

- same invalid packet shape repeats after repair guidance
- same checklist item repeats with no evidence change
- same proof failure repeats with no environment change
- dirty-state marker changes unexpectedly
- unsafe command/tool request occurs
- role attempts to approve another role's required item
- envelope exceeds configured continuation cap with no progress

Recommended config:

```python
@dataclass(slots=True)
class RoleEnvelopeConfig:
    enabled: bool = False
    prefer_same_session: bool = True
    max_same_session_continuations: int = 8
    max_no_progress_repeats: int = 1
    max_fix_envelopes_per_stage: int = 2
    checklist_hud_enabled: bool = True
    self_approval_enabled: bool = True
    qa_final_approval_required: bool = True
```

Tony runtime may enable this. Standard Hermes remains default off.

Implementation-ready tests:

- Dev inspect/edit/test/deliver continues in one worker session when progress changes.
- same invalid packet repeats once, then routes recovery with exact reason.
- same failed proof without environment change does not spawn infinite Dev retries.
- dirty-state mismatch pauses continuation and records a blocker.

## Stage 52E. Proof Batch and Self-Approval Semantics

Use proof batches to avoid stale proof confusion.

Rules:

- A final gate run creates or updates the current stage proof batch.
- A passed newer proof batch supersedes older failed proof batches for the same typed
  stage and recipe.
- QA must cite the active proof batch or explain why it is missing.
- Dev self-approval can mark `ready_for_gate`, not `qa_approved`.
- QA can mark checklist items `verified` only after checking active proof batches.

Implementation-ready tests:

- newer passed proof batch supersedes older failed proof for the same stage.
- older failed proof IDs are still archived but not rendered as current blockers.
- QA cannot approve from stale proof IDs when a newer failed batch exists.
- Dev self-approval does not satisfy QA release gates.

## Stage 52F. Mission Control UX

Mission Control should show each role as a worker with a checklist and timeline.

For each persona tab:

- role name and current typed stage
- envelope status and phase
- checklist progress count
- current item
- safe process summary rows
- tool/proof/decision event bubbles
- expandable raw redaction-safe payload
- why the Harness continued, paused, or rerouted

DM-bubble event rows should remain compact. Checklist rows should not become giant cards.

Snapshot additions:

```json
{
  "role_envelopes": [
    {
      "role_id": "dev",
      "mission_stage_id": "launcher_terminal_ui_fix",
      "phase": "implementation",
      "status": "continuing",
      "checklist": {
        "current_item_id": "self_test",
        "done_count": 2,
        "total_count": 6,
        "items": []
      },
      "continuation_reason": "self_test evidence changed"
    }
  ]
}
```

Implementation-ready tests:

- snapshot includes role envelopes for Neko, Backend Dev, Launcher Dev, and QA.
- inactive roles still show their last closed checklist for the task.
- Launcher parser accepts role envelope/checklist fields additively.
- Launcher widget renders checklist progress and DM bubbles for all personas.
- fullscreen screenshot proof is captured for Mission Control UI changes.

## Stage 52G. Routing Integration

Typed mission plan remains the authority. Role envelopes decide whether to continue the
current owner or hand control back to typed routing.

Routing order when `role_envelope.enabled=true`:

1. Clean stale/dirty temporary state for new goals and record dirty-state indicator.
2. Load typed mission plan.
3. Load or open role envelope for the current typed stage owner.
4. If the envelope can continue, resume same worker session.
5. If the envelope closed cleanly, ask typed mission plan for next unblocked owner.
6. If QA needs fixes, open Dev fix envelope for the exact typed stage.
7. If no role can progress and blocker evidence is unchanged, block with observability.

Implementation-ready tests:

- Neko does not re-enter between Dev self-test and final gate when Dev envelope can continue.
- QA needs-fixes routes to Dev fix envelope, not repeated QA verdicts.
- completed Dev envelope routes QA only when typed dependencies are ready.
- completed proof-only Backend envelope routes Neko/next dependency, not parent goal completion.

## Stage 52H. Observability and Anti-Freeze Metrics

Add counters and events that explain performance.

Events:

- `role_envelope.opened`
- `role_envelope.continued`
- `role_envelope.paused`
- `role_envelope.closed`
- `role_checklist.created`
- `role_checklist.item_updated`
- `role_checklist.self_approved`
- `role_checklist.verified`
- `proof_batch.superseded`

Metrics:

- same-session continuations per role
- checklist items self-approved per role
- invalid packet repair count
- repeated same-item count
- repeated same-proof-failure count
- token use per envelope
- wall-clock duration per envelope
- tool calls per checklist item
- watchdog interventions per task

Acceptance target for smooth goals:

- simple proof-only goal completes with Neko, one Dev/Backend envelope, and QA.
- small Launcher UI fix completes with Neko, one Dev envelope, one QA envelope, and at
  most one Dev fix envelope.
- no repeated identical Dev delivery.
- no repeated identical QA verdict.
- every pause/reroute has a visible reason in Mission Control.

## Stage 52I. Tests

Unit tests:

- role envelope model serde and archive preservation
- checklist template generation from typed stages
- checklist self-approval validation by role
- HUD projection includes current item, done criteria, and allowed actions
- same-session continuation when progress hash changes
- no-progress reroute when progress hash repeats
- proof batch supersession
- QA final approval cannot be replaced by Dev self-approval

Integration tests:

- Neko planning checklist creates typed plan and releases Backend Dev.
- Backend proof-only envelope requests gate and self-approves handoff.
- Launcher Dev implementation envelope inspects, records self-test evidence, requests
  final gate, and hands off to QA.
- QA needs-fixes opens Dev fix envelope with exact checklist item and finding hash.
- QA approved closes task only after all required checklist/proof items are verified.
- Mission Control snapshot contains all persona envelopes and checklist progress.

Live-token certification:

1. Simple no-product-edit Harness proof goal.
2. Small Launcher UI bug fix with QA screenshot proof enabled.
3. Cross-stack Backend + Launcher + QA goal.
4. Mission Control persona-log/checklist visual proof in fullscreen.

Each live run must record:

- task ID
- run IDs
- role envelope IDs
- checklist progress summary
- proof batch IDs
- total wall-clock time
- token/tool counts by envelope
- repeated invalid/repair counters
- final archive manifest path

## Stage 52J. Implementation Order

1. Add `RoleEnvelopeConfig` default off and enable only for Tony runtime profile.
2. Add `role_envelopes.py` model/store with serde and archive preservation.
3. Add `role_checklists.py` model/store and typed-stage template generation.
4. Add checklist update fields to existing decision payload validation.
5. Add HUD checklist projection and invalid packet repair hints.
6. Add same-session continuation policy to ticker/state routing.
7. Add proof batch active/superseded semantics to typed stage proof handling.
8. Add Mission Control snapshot fields for role envelopes and checklists.
9. Add Launcher parser/widget support after Harness snapshot contract is stable.
10. Add observability events and anti-freeze counters.
11. Run full Harness tests.
12. Run focused Launcher Mission Control tests.
13. Run simple live-token goal.
14. Run complex live-token goal with fullscreen visual proof.
15. Archive test goals and confirm runtime status is clean.

## Stage 52K. Risk Controls

Avoid these risky changes:

- adding new top-level decision types for checklist updates before payload fields are
  proven insufficient
- letting agents edit arbitrary checklist schema keys
- letting Dev mark QA items verified
- letting self-test evidence satisfy final release proof
- increasing continuation caps without no-progress watchdogs
- hiding interruption reasons from Mission Control
- enabling role envelopes globally for standard Hermes
- storing raw secret-bearing logs in checklist summaries


## Stage 52L. Concrete Implementation Contracts

This section closes the implementation-readiness gaps. Treat it as the executable contract
for the first Stage 52 patch series.

### Code Touch Map

Harness files expected to change:

- `agent_runtime/runtime_config.py`: add `RoleEnvelopeConfig` under runtime config,
  default off.
- `agent_runtime/config.py`: load `agent_runtime.role_envelope` settings and preserve
  legacy defaults.
- `agent_runtime/models.py`: add additive `Task.role_envelopes` and
  `Task.role_checklists` summaries only if the existing store projection is not enough.
  Prefer external store records first to avoid bloating task JSON.
- `agent_runtime/serde.py`: round-trip additive envelope/checklist fields and prove old
  task JSON loads unchanged.
- `agent_runtime/paths.py`: add runtime paths for role envelope/checklist records.
- `agent_runtime/role_envelopes.py`: new pure model/store/routing helper module.
- `agent_runtime/role_checklists.py`: new pure checklist model/template/validation
  module.
- `agent_runtime/decision_payload_contracts.py`: validate `checklist_updates`,
  `self_approval_status`, and `active_checklist_item_id` as strict payload extensions.
- `agent_runtime/decision_contract_registry.py`: expose checklist payload keys and enum
  choices in HUD shape metadata.
- `agent_runtime/worker_actions.py`: project current checklist item into the existing
  simplified worker action menu.
- `agent_runtime/context_builder.py`: render compact role checklist, current item, done
  criteria, allowed actions, and invalid-packet repair hints.
- `agent_runtime/ticker.py`: open/resume/close envelopes around worker execution and
  apply continuation policy before falling back to typed routing.
- `agent_runtime/state_machine.py`: keep typed mission plan authoritative; call envelope
  continuation as a pre-routing continuation check, not a second router.
- `agent_runtime/planning.py`: apply checklist updates from accepted Neko/Dev/QA packets
  and mirror terminal checklist state into typed stage state.
- `agent_runtime/final_gate.py`: create/update active proof batch IDs for final gate
  proofs and expose supersession status.
- `agent_runtime/proof_gates.py`: require active proof batch coverage for QA approval;
  self-test evidence remains non-release proof.
- `agent_runtime/proof_capture.py` and `agent_runtime/proof_runner.py`: attach proof
  batch IDs to new proof records when invoked from an envelope final gate.
- `agent_runtime/self_test_evidence.py`: let checklist items cite existing self-test
  evidence IDs without changing release proof semantics.
- `agent_runtime/dirty_state.py` and `agent_runtime/goal_hygiene.py`: clear stale temp
  envelope/checklist state at new goal start and record dirty-state indicator events.
- `agent_runtime/events.py`: add role envelope/checklist/proof batch event types and
  redaction-safe display metadata.
- `agent_runtime/observability.py`, `agent_runtime/snapshot.py`, and
  `agent_runtime/status.py`: expose role envelopes, checklist progress, continuation
  reason, pause reason, and active proof batch summaries.
- `agent_runtime/store.py`: archive envelope/checklist records and artifacts alongside
  runs, proofs, packets, self-tests, and manifests.
- `hermes_cli/main.py` or current Harness CLI module: include envelope/checklist fields
  in `harness status --json` and `harness snapshot --json` only when present.

Harness tests expected to change or be added:

- `tests/agent_runtime/test_config.py`
- `tests/agent_runtime/test_models_serde.py`
- `tests/agent_runtime/test_paths.py`
- `tests/agent_runtime/test_role_envelopes.py`
- `tests/agent_runtime/test_role_checklists.py`
- `tests/agent_runtime/test_decision_payload_contracts.py`
- `tests/agent_runtime/test_decision_contract_registry.py`
- `tests/agent_runtime/test_worker_sessions.py`
- `tests/agent_runtime/test_context_builder.py`
- `tests/agent_runtime/test_ticker.py`
- `tests/agent_runtime/test_state_machine.py`
- `tests/agent_runtime/test_mission_plan.py`
- `tests/agent_runtime/test_final_gate.py`
- `tests/agent_runtime/test_proof_gates.py`
- `tests/agent_runtime/test_snapshot.py`
- `tests/agent_runtime/test_status.py`
- `tests/agent_runtime/test_store.py`

Launcher files expected to change after Harness snapshot fields are stable:

- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\lib\features\mission_control\data\mission_control_snapshot.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\lib\features\mission_control\mission_control_page.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\test\features\mission_control\mission_control_snapshot_test.dart`
- `X:\Unreal Engine\Engine\Launcher\EterniaLauncher\test\features\mission_control\mission_control_page_test.dart`

### Runtime Config And Profile Enablement

Add this config block:

```yaml
agent_runtime:
  role_envelope:
    enabled: false
    prefer_same_session: true
    checklist_hud_enabled: true
    self_approval_enabled: true
    qa_final_approval_required: true
    max_same_session_continuations: 8
    max_no_progress_repeats: 1
    max_fix_envelopes_per_stage: 2
    max_checklist_items_rendered: 8
    max_foreign_checklist_summaries: 3
    enable_legacy_stage_projection: true
```

Default behavior:

- standard Hermes profiles: `enabled=false`
- Tony/Alice Mission Control runtime profile:
  `X:\Eternia\.hermes\profiles\alice\config.yaml` may enable it after unit tests pass
- live goal certification must report which profile and config values were active

### Persistence Paths

Use append-only JSON records, not a monolithic mutable log, so archive preservation and
crash recovery are simple.

```text
<runtime_root>/role_envelopes/<task_id>/<envelope_id>.json
<runtime_root>/role_checklists/<task_id>/<checklist_id>.json
<runtime_root>/role_checklists/<task_id>/events/<event_id>.json
<runtime_root>/proof_batches/<task_id>/<proof_batch_id>.json
```

Archive-ready behavior:

- copy all role envelope records for the task
- copy all role checklist records and checklist events for the task
- copy active and superseded proof batch records
- include these manifest arrays:
  - `role_envelopes`
  - `role_checklists`
  - `role_checklist_events`
  - `proof_batches`
- never delete self-test, proof, packet, run, or checklist artifacts outside the existing
  deleted-archive preservation flow

### Strict Payload Contract

Checklist updates are allowed only as payload extensions on existing decisions.

Allowed payload fragment:

```json
{
  "active_checklist_item_id": "self_test",
  "checklist_updates": [
    {
      "item_id": "self_test",
      "status": "self_approved",
      "evidence_refs": ["selftest_task_123_flutter_test_abc"],
      "summary": "Focused Mission Control widget test passed."
    }
  ],
  "self_approval_status": "ready_for_gate"
}
```

Allowed `checklist_updates[].status` values:

- `pending`
- `in_progress`
- `self_approved`
- `verified`
- `needs_fix`
- `blocked`
- `skipped_with_reason`

Allowed `self_approval_status` values:

- `none`
- `working`
- `ready_for_gate`
- `ready_for_handoff`
- `ready_for_qa`
- `blocked`

Validation rules:

- unknown checklist item IDs are rejected with a repair HUD showing valid item IDs
- unknown statuses are rejected with enum choices
- Dev and Backend Dev cannot emit `verified` for QA-owned items
- QA cannot mark Dev implementation items `self_approved`; QA may mark them `verified`
  only with proof evidence refs
- `skipped_with_reason` requires a non-empty redaction-safe `summary`
- `blocked` requires `summary` plus at least one blocker/proof/environment reference when
  available
- evidence refs must resolve to known proof, self-test, packet, event, or artifact IDs for
  the same task
- payload extensions must be ignored or rejected safely when `role_envelope.enabled=false`
  according to existing strictness mode

Invalid packet repair shape:

```json
{
  "validation_status": "invalid",
  "repair_kind": "checklist_payload",
  "current_role": "dev",
  "current_stage_id": "launcher_terminal_ui_fix",
  "valid_item_ids": ["inspect", "patch", "self_test", "final_gate", "handoff"],
  "valid_statuses": ["pending", "in_progress", "self_approved", "blocked"],
  "valid_self_approval_statuses": ["working", "ready_for_gate", "blocked"],
  "next_expected": "Update the current checklist item or choose one visible worker action."
}
```

### Legacy Fallback Contract

Stage 52 prefers Stage 51 typed mission plans. It must still avoid crashing if Stage 51 is
not enabled for a legacy task.

Fallback rules:

- if `mission_plan.enabled=true` and `task.mission_plan` exists, checklist templates come
  from typed stage owner/kind/proof requirements
- if `role_envelope.enabled=true` but no typed plan exists, generate a legacy checklist
  from `Task.current_stage_id`, `TaskStage.owner`, existing role/session state, and
  `stage_intent.py` as a compatibility projection
- fallback checklists are advisory and cannot complete a parent goal without existing
  legacy QA/proof gates
- Mission Control must label fallback checklists as `legacy_projection=true`
- typed and legacy routing must not both advance the same task in one tick

Implementation tests:

- legacy task with no typed plan gets a bounded advisory checklist and does not crash
- typed task uses typed checklist even when legacy `TaskStage` text disagrees
- typed route wins over legacy prose helpers
- fallback checklist cannot bypass QA/release gates

### Final Gate And Proof Batch Contract

Proof batch record:

```json
{
  "proof_batch_id": "proofbatch_task_123_launcher_final_gate_001",
  "task_id": "task_123",
  "mission_stage_id": "launcher_terminal_ui_fix",
  "role_envelope_id": "envelope_dev_001",
  "recipe_id": "launcher_mission_control_widget",
  "status": "passed",
  "proof_ids": ["proof_abc"],
  "supersedes": ["proofbatch_task_123_launcher_final_gate_000"],
  "created_at": "2026-06-07T00:00:00Z",
  "updated_at": "2026-06-07T00:00:10Z"
}
```

Allowed proof batch statuses:

- `pending`
- `running`
- `passed`
- `failed`
- `superseded`
- `blocked`

Rules:

- active proof batch is the newest non-superseded batch for the same task, typed stage,
  and recipe
- passed active batch satisfies final gate only if all required proof IDs are passed and
  redaction-safe
- failed active batch blocks QA unless a newer passed batch supersedes it
- superseded failed batches remain visible in archives but should not be rendered as
  current blockers

### Live Certification Pass/Fail Thresholds

A Stage 52 live run is a pass only if all of these are true:

- runtime profile and config are printed at start
- no open incidents remain at finish
- no active worker sessions remain at finish
- no dirty temp state remains at finish
- every role that participated has at least one visible envelope and checklist record
- every role tab in Mission Control renders either events or a truthful `no events yet`
  state
- no identical invalid packet repair appears more than once per role envelope
- no identical Dev delivery appears more than once per typed stage unless QA finding hash
  changed
- no identical QA verdict appears more than once per task unless proof batch changed
- simple proof-only goal uses no more than one Neko planning envelope, one Dev/Backend
  envelope, and one QA envelope
- small UI goal uses no more than one Dev fix envelope unless QA finding hash changes
- fullscreen screenshot proof is captured for UI-affecting goals
- final archive-ready manifest contains role envelopes, checklists, proof batches, runs,
  packets, proofs, and self-test evidence

A live run is a known-blocked result, not a pass, if an external dependency is missing and
observability records the exact dependency, proof command, profile, and next safe action.

### Implementation Commit Plan

Recommended patch order:

1. Harness config, paths, models, stores, and archive preservation.
2. Checklist templates and strict payload validation.
3. HUD projection and invalid packet repair.
4. Envelope continuation policy and typed/legacy routing integration.
5. Proof batch active/superseded semantics.
6. Snapshot/status/observability fields.
7. Launcher parser and checklist UI.
8. Full Harness tests.
9. Focused Launcher tests.
10. Live-token simple and complex certification.

Each patch should be independently testable and should avoid changing Launcher UI before
Harness snapshot fixtures exist.
## Exit Criteria

Stage 52 is complete only when:

- each persona has a visible role checklist in the HUD and Mission Control.
- Dev and Backend Dev can self-approve their own stage readiness without approving the
  full goal.
- QA remains the only final release approver.
- a normal implementation stage stays in the same worker session across inspect, patch,
  self-test, final gate, and delivery while progress changes.
- repeated no-progress loops are interrupted with exact evidence.
- typed mission plan routing remains authoritative.
- Mission Control can explain every continue/pause/reroute decision.
- unit, integration, Launcher, and live-token certification tests pass.

## Relationship To Stage 50 And Stage 51

Stage 50 simplified the worker flow and made self-test plus final gate the normal path.
Stage 51 made typed mission plans the routing authority.

Stage 52 does not replace either. It makes the worker experience continuous:

```text
Stage 50: what a normal worker should do.
Stage 51: which typed stage owns the work.
Stage 52: how the same role keeps working until its checklist is genuinely done.
```

