# Stage 57 - Simplified State Machine And Agent Contract

Date: 2026-06-09
Owner: Codex independent Harness implementation
Status: implementation-in-progress; current-state audit required before more coding
Depends on: Stage 51 typed mission plan simplification, Stage 52 role envelopes, Stage 54 operational task lists, Stage 56 persona instance assignment runtime

## Goal

Make Mission Control behave like a normal competent harness:

```text
Plan stage -> run owner -> owner delivers or blocks -> QA verifies -> done or repair
```

Keep the rich internal state for audit, proof, context, sessions, incidents, archives, and Mission Control observability. Do not expose that full complexity as agent-facing decision space.

The agent-facing contract should become small enough that Neko, Dev, Backend Dev, and QA cannot accidentally invent workflow states or burn turns choosing between internal mechanisms.

## Current Implementation Audit

This stage is no longer only conceptual. The current Harness checkout already contains a partial Stage 57 implementation:

- `agent_runtime/repo_bundles.py` exists and stores repo bundle records under the runtime root.
- `agent_runtime/runtime_config.py` defines `repo_bundle_routing` and `simplified_agent_contract` feature flags.
- `agent_runtime/context_builder.py`, `agent_runtime/snapshot.py`, and `agent_runtime/status.py` project repo bundles into the HUD/snapshot/status surfaces.
- `agent_runtime/ticker.py` links repo bundles to persona assignments and marks bundles running/delivered in some decision paths.
- `agent_runtime/store.py` archives repo bundle evidence.
- `tests/agent_runtime/test_repo_bundles.py` covers bundle creation, assignment linking, snapshot projection, archive preservation, and dependency wake basics.
- Persona skills already mention the Stage 53/57 simplified HUD and repo bundle IDs.

Do not re-create those pieces from scratch. The remaining work is to finish the enforcement, routing, queueing, and live certification gaps below.

### Remaining Stage 57 Gaps

1. **External action collapse is not fully enforced.**
   - Current code still exposes compatibility decision shapes in some HUD paths.
   - Needed: role-specific HUD tests that prove Neko sees only `plan/route/clarify/accept`, Dev sees only `request_context/deliver/block`, and QA sees only `approve/reject/request_missing_proof` except explicit compatibility gates.

2. **Repo bundle routing exists, but full task ownership is not complete.**
   - Bundle records can be created and attached, but the Harness still needs a complete guarantee that normal repo-owned work uses one bundle assignment instead of stage-fragment assignments.
   - Needed: tests that reject normal stage-level Dev assignment when a repo bundle exists.

3. **Queueing is partially modeled, not fully operational.**
   - Dependency wake basics exist, but QA waiting and Dev waiting need stronger no-token guarantees.
   - Needed: tests proving queued Dev/QA assignments render in status/Mission Control without opening model runs.

4. **Same-session steering needs enforcement.**
   - Persona instances/assignments exist, but Stage 57 requires repair and QA rejection to resume the same assignment/session when healthy.
   - Needed: tests for QA rejection reopening the same bundle assignment, and Neko steering injecting a bounded update instead of creating duplicate workers.

5. **Harness-owned transition authority is not fully closed.**
   - Some old decision paths still let agent decisions imply stage/task transitions directly.
   - Needed: tests proving Dev cannot mark task `done`, QA approval is the normal completion path, and assignment state wins when task/stage/run/session disagree.

6. **Context budgets need hard stops.**
   - Context-loop observability exists, but Stage 57 requires the next HUD to force `deliver` or `block` after budget exhaustion.
   - Needed: tests for locator + exact-file budget, repeated same path rejection, terminal feedback, and no new model run on exhausted context.

7. **Proof flow needs product-edit default enforcement.**
   - Normal worker flow exists, but product-edit Dev should not normally see `request_test_run`.
   - Needed: tests that Dev delivery schedules final gate automatically, failed final gate routes same bundle, and QA missing-proof can happen once without looping.

8. **Mission Control projection still needs visual proof.**
   - Snapshot fields exist, but the Launcher UI must show simple phases, bundle queue state, QA waiting state, and logs for all personas.
   - Needed: fullscreen Stage C proof after implementation.

9. **Live-token certification is still open.**
   - Required: no-edit investigation, simple product-edit, and cross-stack product goal all complete or block truthfully without manual nudging.

### Implementation-Ready Work Items

Implement Stage 57 by closing these work items in order. Each item must be covered by a focused regression test before live-token certification.

| Item | Files | Required implementation | Required tests |
| --- | --- | --- | --- |
| 57-1 Simplified HUD role menu | `agent_runtime/context_builder.py`, `agent_runtime/worker_actions.py` | Project a concise `agent_hud.contract.allowed_actions` per role and expose only valid visible worker actions in normal worker flow. Keep legacy `decision_menu` only as debug/compat detail. | `tests/agent_runtime/test_context_builder.py`: Neko, Dev, Backend Dev, QA allowed actions and no forbidden internal actions. |
| 57-2 Bundle active-owner authority | `agent_runtime/ticker.py`, `agent_runtime/persona_assignments.py`, `agent_runtime/repo_bundles.py` | When repo-bundle routing is enabled, normal Dev work must attach to one `PersonaAssignment.repo_bundle_id`; assignment state wins for active owner projection. | `tests/agent_runtime/test_repo_bundles.py`: normal Dev assignment includes bundle; stage-fragment duplicate is not created. |
| 57-3 No-token queueing | `agent_runtime/ticker.py`, `agent_runtime/state_machine.py`, `agent_runtime/repo_bundles.py` | Queued Dev bundles and QA waiting states appear in status/snapshot/Mission Control but do not launch model runs until wake conditions are satisfied. | `tests/agent_runtime/test_repo_bundles.py`: queued bundle/QA state creates no `AgentRun`. |
| 57-4 Same-assignment repair | `agent_runtime/ticker.py`, `agent_runtime/persona_assignments.py`, `agent_runtime/repo_bundles.py` | QA rejection and Neko scope repair reopen the same bundle assignment where healthy; no duplicate worker unless stale/corrupt. | `tests/agent_runtime/test_repo_bundles.py`: QA rejection reopens matching bundle and preserves assignment reference. |
| 57-5 Context-budget terminal feedback | `agent_runtime/worker_actions.py`, `agent_runtime/context_requests.py`, `agent_runtime/context_builder.py` | After bounded context budget is exhausted, next HUD must force deliver/block and terminal feedback must explain why. | Existing repeated-context tests plus explicit terminal-feedback assertion. |
| 57-6 Product-edit proof simplification | `agent_runtime/worker_actions.py`, `agent_runtime/ticker.py` | Product-edit Dev does in-session self-test and `deliver`; Harness auto-runs final gate; `request_test_run` hidden until final-gate repair/proof-only compat. | Existing normal-worker-flow tests plus assertion that visible product-edit actions exclude `request_test_run`. |
| 57-7 Snapshot/Mission Control projection | `agent_runtime/snapshot.py`, Launcher Mission Control renderer | Status/snapshot expose simple phase, repo bundles, bundle queue, QA waiting, assignment ID, active run ID, and terminal feedback. | Snapshot tests plus fullscreen Stage C visual proof after Launcher renderer work. |

Implementation is complete only after all 57-x tests pass and Stage 57K live-token certification is run.

### Implementation Pass - 2026-06-09

Closed in code:

- Repo bundle queueing now preserves `queued_waiting_dependency` when an assignment is attached.
- Queued Dev bundles do not launch model runs while waiting on dependency wake conditions.
- QA does not launch a model run while required repo bundles are still pending.
- Simplified role contracts are regression-tested for Neko, Launcher Dev, Backend Dev, and QA.
- Status/snapshot/HUD projection tests cover repo bundles, queue state, QA waiting, and role contracts.

Focused tests:

```powershell
python -m pytest -o addopts= tests/agent_runtime/test_repo_bundles.py tests/agent_runtime/test_context_builder.py -q
```

Remaining before Stage 57 can be called fully certified:

- Launcher Mission Control fullscreen visual proof for simplified phases, bundle queue, QA waiting, and all persona logs.
- Live-token Stage 57K certification goals.

## Maximum Simplification Target

The product should feel like one smart harness with specialist workers, not a collection of exposed runtime tables.

The simplest durable model is:

```text
Mission has stages.
Stage has one owner.
Owner has one assignment.
Assignment has one visible next action.
Harness decides every transition.
QA is the final verifier.
```

Everything else is internal evidence:

- runs are execution attempts;
- worker sessions preserve continuity;
- persona instances preserve identity;
- proofs preserve verification;
- context requests preserve bounded missing input;
- incidents preserve blockers;
- checklists preserve local progress;
- events preserve the terminal/log view.

Agents should never need to understand or mutate those record types to finish work.

The public workflow should reduce to:

```text
Neko creates the plan.
Dev executes the assignment.
Harness runs final gates.
QA verifies the result.
Harness closes or routes repair.
```

## Primary Routing Rule: Repo Bundles

This is the normal Harness shape Tony wants:

```text
Neko splits the mission by repository once.
Harness assigns one repo bundle to the matching Dev.
Each Dev completes all stages for that repo.
Harness runs final proof gates.
QA verifies the whole mission.
```

Routing algorithm:

```text
if affected_repos.count == 0:
    Neko resolves repo ownership or blocks for missing scope.
elif affected_repos.count == 1:
    create one RepoBundle for that repo
    assign the entire bundle to that repo's Dev
else:
    create one RepoBundle per affected repo
    assign each bundle to the matching repo Dev
```

Repo ownership:

- `EterniaBackend` -> Backend Dev
- `EterniaLauncher` -> Launcher Dev
- `hermes-agent` -> Harness/Backend Dev unless explicitly assigned otherwise

Neko should not split work into tiny proof/context/handoff stages unless there is a real cross-repo dependency. The bundle is the unit Dev works.

Dev receives:

```text
Here is your repo bundle.
Here are the stages for your repo.
Here is the acceptance.
Do the work.
Return deliver or block.
```

QA receives:

```text
Here are the delivered repo bundles.
Here are the proof IDs and required visual artifacts.
Approve or reject.
```

Harness owns all coordination between bundles.

Cross-repo dependency rule:

- Backend can deliver a contract packet inside its repo bundle.
- Launcher consumes the latest backend contract packet inside its repo bundle.
- Harness waits for all required repo bundles before final QA.
- Neko only re-enters if a Dev blocks on missing scope, contract ambiguity, human decision, or true cross-repo sequencing uncertainty.

Task queueing rule:

- Repo bundles may have dependencies on other repo bundles.
- A blocked dependency does not create a new stage by default; it queues the dependent bundle.
- Waiting bundles stay assigned to their Dev but enter `queued_waiting_dependency`.
- When the dependency delivers, Harness wakes the dependent bundle and resumes the same persona session when possible.
- The waiting Dev does not burn tokens polling for dependency completion.
- Mission Control should show the queue reason, dependency bundle ID, owner, and wake condition.

QA queueing rule:

- QA can be queued before all Dev bundles are ready.
- QA assignment state should be `queued_waiting_bundles` while any required repo bundle is not delivered.
- QA does not start a model run while waiting for Dev bundles or final gate proof.
- When all required bundles are delivered and final gates are attached, Harness wakes QA.
- If one Dev bundle is blocked, QA stays queued unless Neko/Harness explicitly asks QA for partial-risk review.
- Mission Control should show QA as waiting, not missing logs or frozen.

Handoff-and-wait rule:

- When a Dev delivers its repo bundle, that Dev is done for now.
- The Dev persona instance remains available with session/context references preserved.
- The Dev does not wait actively for QA or another Dev.
- If QA later rejects that repo bundle, Harness reopens the same bundle assignment and resumes the same Dev persona instance when healthy.
- If Neko needs scope repair, Neko steers the same Dev assignment instead of creating a duplicate worker.
- A delivered Dev bundle can be `waiting_for_qa` without an active run.
- Mission Control should show the Dev as `delivered_waiting_for_qa`, not running, frozen, or missing.

Steering rule:

- Devs should stay in the same persona session for the repo bundle as long as they are making progress.
- Neko steers only when scope, ownership, repo split, contract sequencing, or blocker routing needs a decision.
- QA steers only from evidence: missing proof, failed proof, visual mismatch, acceptance gap, or regression risk.
- Steering should usually be an injected same-session repair/update, not a new worker session.
- Starting a new session is a recovery action, not the normal path.

## External Stage Kinds

Expose only four stage kinds to agents and Mission Control:

- `investigate`: gather bounded context and produce a report/plan.
- `implement`: change product or Harness code and self-test.
- `verify`: QA review of evidence and behavior.
- `release`: final acceptance/archive/summary.

Internal stage flavors such as `context`, `proof_only`, `qa_verdict`, `contract_join`, smoke, certification, and recovery may remain as implementation details. They should map into one of the four external kinds before reaching an agent HUD.

## One Active Authority

Only `PersonaAssignment` should answer "who is active and what are they doing?"

Authority rules:

- `Task` is the product goal container.
- `MissionPlanStage` is the product workflow step.
- `PersonaInstance` is durable worker identity.
- `PersonaAssignment` is current active work and next allowed action.
- `AgentRun` is one execution attempt against an assignment.

If these records disagree, the assignment wins for runtime routing, and observability reports the inconsistency.

## Unified Blocker Envelope

All blocked paths should normalize to one envelope:

```json
{
  "blocker_type": "environment | missing_context | invalid_output | budget | scope | proof | runtime",
  "owner": "harness | neko | dev | qa | human",
  "minimum_next_signal": "The smallest concrete signal required to retry.",
  "retry_allowed": true
}
```

Agents return a simplified `block`. Harness converts it into incidents, Neko routing, retry locks, or human-visible blockers.

## Simplified Proof Flow

Proof should not be a normal agent decision maze.

Target proof behavior:

```text
Dev runs focused self-tests in-session when possible.
Dev delivers.
Harness automatically runs final gate proof.
QA approves or rejects from final gate and required visual proof.
QA requests exactly one missing proof only when Harness proof is absent or stale.
```

Agent-facing Dev should not normally see `request_test_run`.

Compatibility:

- `request_test_run` may remain internally for proof recipes and old snapshots.
- The HUD should expose it only for explicit proof-only/certification stages during migration.
- Product-edit Dev should almost always see `deliver` or `block`, not proof routing choices.

## Simplified Context Flow

Context should be typed, not arbitrary repeated path guessing.

Expose four context modes:

- `locator_context`: bounded directory/index listing for finding likely files.
- `exact_file_context`: bounded file excerpts from known repo-relative files.
- `proof_context`: bounded proof/log excerpts.
- `blocker_context`: bounded environment/runtime diagnostic context.

Budgets:

- `investigate`: one `locator_context`, one `exact_file_context`, then `deliver` or `block`.
- `implement`: local repo tools are allowed; Harness context is optional and should be rare.
- `verify`: one `proof_context` or `request_missing_proof`.
- `release`: no broad context; cite existing proof and QA verdict.

After the budget is exhausted, Harness should reject more context requests and return a repair HUD whose only valid choices are `deliver` and `block`.

## Evidence Driving This Stage

The NSFW investigation live goal exposed the core issue:

- Backend Dev stopped using forbidden `search_files` after no-edit context stages blocked repo search tools.
- Context request plumbing improved: directory listings and partial bundles worked.
- The task still looped because Backend Dev kept issuing more context requests instead of delivering once enough context existed.
- Observability correctly reported `context_request_loop`, but the state machine still allowed another Dev continuation.
- Several internal states existed at once: task stage, mission stage, assignment, worker session, context request, run, incident, proof targets, checklist state, daemon state.

Conclusion:

```text
The runtime needs rich internal states.
The agents need fewer choices.
The Harness, not the agents, should own state transitions.
```

## Target Agent Contract

### Neko

Neko should only see high-level mission control choices:

- `plan`: create or repair the typed mission plan.
- `route`: choose the next owner for a blocked or ready stage.
- `clarify`: request human input only for true missing human intent, safety, credentials, or product decision.
- `accept`: accept final QA-approved mission outcome and summarize alternatives after completion.

Neko should not choose low-level proof recipes, context paths, role checklist item state, or assignment lifecycle transitions unless a recovery requires it.

### Dev And Backend Dev

Dev roles should only see:

- `request_context`: ask Harness for bounded missing repo/log/proof context.
- `deliver`: report completed work or completed investigation with evidence and remaining risk.
- `block`: report an exact blocker with attempted self-service and minimum next signal.

For product-edit stages, Dev may still use local repo tools to inspect, patch, and run focused self-tests in-session. The returned Harness decision is still only `deliver` or `block` unless the HUD explicitly exposes `request_context`.

For no-product-edit investigation stages:

- Dev gets at most one broad locator context request.
- Dev gets at most one follow-up exact-file context request.
- After that, the next valid decision is `deliver` or `block`.
- Proof recipe, screenshot, video, patch, QA handoff, and cross-role state choices are hidden.

### QA

QA should only see:

- `approve`: approve with cited proof/context IDs.
- `reject`: reject with exact missing evidence or behavioral failure.
- `request_missing_proof`: request exactly one missing deterministic proof or visual proof lane.

QA does not create stages, route owners, mutate Dev task lists, or choose global state transitions.

## Simplified Runtime Flow

The external state machine should reduce to:

```text
created -> planning -> working -> verifying -> done
                         |           |
                         v           v
                      blocked      repair
```

Internal records may have richer states, but Mission Control and agent HUDs should project them into these six phases only:

- `created`
- `planning`
- `working`
- `verifying`
- `repair`
- `blocked`
- `done`

Existing detailed records remain, but they become projections:

- persona assignments describe who is working;
- worker sessions describe continuity;
- context requests describe bounded context expansion;
- proofs describe command/visual evidence;
- role checklists describe local progress;
- incidents describe intervention requirements;
- Mission Control logs render all of the above.

Agents should never be asked to pick from those internal runtime record types.

## Implementation Stages

### 57A - Contract Inventory And Decision Collapse

Audit the current agent-facing choices and map them to the simplified contract.

Files to inspect:

- `agent_runtime/decision_schema.py`
- `agent_runtime/decision_contract_registry.py`
- `agent_runtime/worker_actions.py`
- `agent_runtime/context_builder.py`
- `agent_runtime/ticker.py`
- `agent_runtime/state_machine.py`
- `agent_runtime/persona_assignments.py`
- `agent_runtime/role_checklists.py`
- persona skills under `docs/agent-runtime-harness/stage46-skills/`

Deliverables:

- A table mapping old decision types to simplified actions.
- A table mapping old stage kinds to `investigate`, `implement`, `verify`, or `release`.
- A list of internal-only decision/state fields that should be hidden from agents and Mission Control's primary workflow.
- Tests that snapshot the simplified HUD menu for Neko, Dev, Backend Dev, and QA.

Acceptance:

- Dev no-edit context stage HUD exposes only `request_context`, `deliver`, `block`.
- QA HUD exposes only `approve`, `reject`, `request_missing_proof`.
- Neko HUD exposes only `plan`, `route`, `clarify`, `accept`.
- Mission Control primary phase is one of `created`, `planning`, `working`, `verifying`, `repair`, `blocked`, `done`.

### 57A.5 - Implementation Interface Lock

Lock these interfaces before coding behavior.

Storage:

- Add `agent_runtime/repo_bundles.py`.
- Store bundle records under `X:\Eternia\.hermes\agent-runtime\repo_bundles\`.
- Do not embed mutable bundle state directly in `Task`.
- `Task` may keep bundle IDs for summary/projection only.

Feature flags:

```text
runtime_config.repo_bundle_routing.enabled
runtime_config.simplified_agent_contract.enabled
```

Default:

- enabled for Tony Agent Runtime Harness profile;
- disabled or compatibility mode for standard Hermes unless explicitly enabled.

Locked `RepoBundleState` enum:

```text
planned
queued_waiting_dependency
assigned
running
delivered_waiting_for_qa
delivered
blocked
verified
rejected
cancelled
archived
```

Locked external phase enum:

```text
created
planning
working
verifying
repair
blocked
done
```

Locked wake conditions:

```text
dependency_bundle_delivered
contract_packet_available
all_required_bundles_delivered
final_gate_passed
visual_proof_attached
qa_rejected_bundle
neko_scope_repaired
operator_resumed
```

Simplified action adapter:

```text
Dev request_context -> request_file_reads
Dev deliver -> propose_patch for product-edit, report_issue_discovery or handoff for no-edit investigation
Dev block -> block

QA approve -> report_qa_verdict verdict=approved
QA reject -> report_qa_verdict verdict=rejected
QA request_missing_proof -> request_test_run, request_screenshot, or request_video internally

Neko plan -> propose_acceptance / propose_stage_plan
Neko route -> handoff_to_dev / propose_acceptance
Neko clarify -> request_human / needs_context
Neko accept -> propose_acceptance final
```

Mission Control snapshot fields:

```json
{
  "simplified_phase": "working",
  "active_assignment_id": "assign_...",
  "repo_bundles": [
    {
      "repo_bundle_id": "bundle_...",
      "repo": "EterniaBackend",
      "owner_persona_id": "backend_dev",
      "state": "running",
      "stage_ids": ["backend_api"],
      "assignment_id": "assign_...",
      "active_run_id": "run_...",
      "dependency_bundle_ids": [],
      "queue_reason": null,
      "wake_condition": null,
      "proof_ids": [],
      "contract_packet_ids": [],
      "last_terminal_feedback": {}
    }
  ],
  "bundle_queue": [
    {
      "repo_bundle_id": "bundle_...",
      "state": "queued_waiting_dependency",
      "waiting_on_bundle_ids": ["bundle_backend"],
      "wake_condition": "contract_packet_available"
    }
  ],
  "qa_waiting_on": [
    {
      "kind": "repo_bundle",
      "repo_bundle_id": "bundle_launcher",
      "owner_persona_id": "dev",
      "reason": "bundle_not_delivered"
    }
  ]
}
```

Required first tests:

```text
tests/agent_runtime/test_repo_bundles.py
tests/agent_runtime/test_simplified_contract.py
tests/agent_runtime/test_bundle_queueing.py
tests/agent_runtime/test_qa_bundle_aggregation.py
tests/agent_runtime/test_mission_control_snapshot_bundles.py
```

Acceptance:

- Interfaces above exist and are covered by tests before live-token behavior changes.
- Old task/stage/run/proof/archive readers remain compatible.
- Feature flags can turn repo-bundle routing off without corrupting existing runtime state.

### 57B - Repo Bundle Planner

Add repo bundles as the normal unit of Dev work.

New model:

```text
RepoBundle
  id
  task_id
  repo
  owner_persona_id
  stage_ids
  acceptance
  dependencies
  contract_inputs
  contract_outputs
  dependency_bundle_ids
  queue_reason
  wake_condition
  proof_requirements
  visual_requirements
  state: planned | queued_waiting_dependency | assigned | running | delivered_waiting_for_qa | delivered | blocked | verified | rejected
```

Required behavior:

- Neko creates a typed mission plan with external stage kinds only.
- Harness groups implementation/investigation stages by repo into `RepoBundle` records.
- Single-repo missions create exactly one bundle.
- Multi-repo missions create exactly one bundle per repo unless Neko explicitly marks a stage as shared.
- Repo bundle ownership is deterministic from repo label.
- A bundle can contain multiple local stages, but Dev receives them as one assignment.
- Bundle records are archived with tasks, proofs, context receipts, assignments, and runs.
- Dependent bundles can be queued without starting a run until required upstream bundle outputs exist.
- Bundle queueing is deterministic from `dependency_bundle_ids` and `wake_condition`.

Tests:

- One affected repo creates one bundle assigned to the matching Dev.
- Backend + Launcher mission creates two bundles, one per repo.
- Unknown repo blocks in planning with a unified `scope` blocker.
- Neko cannot create duplicate bundles for the same repo/task unless repo signal changed.
- Archive manifest preserves repo bundle IDs and linked proof/context/run IDs.
- Launcher bundle depending on Backend contract stays queued until Backend bundle delivers the contract packet.
- Queued bundle does not start a Dev run or consume tokens while dependency is unresolved.

### 57C - Bundle Assignment And Dev Execution

Assign repo bundles, not tiny internal stage fragments.

Required behavior:

- Harness creates one `PersonaAssignment` per repo bundle.
- Assignment message contains:
  - repo;
  - ordered stages in the bundle;
  - acceptance criteria;
  - known dependencies;
  - proof expectations;
  - visual requirements;
  - allowed actions: `request_context`, `deliver`, `block`.
- Dev sees all stages for the repo and can complete them in one session when possible.
- Dev `deliver` applies to the whole repo bundle.
- Dev `block` blocks the whole repo bundle with a unified blocker envelope.
- Dev cannot route QA, create global stages, or assign another role.
- After Dev `deliver`, the run closes and the persona instance returns to available/idle with preserved assignment/session references.
- Delivered Dev bundles do not keep an active run while QA or other Dev bundles finish.
- Harness prefers same-session continuation for bundle progress, repair, and focused follow-up context.
- Same-session continuation is allowed while the worker has made progress or received new steering/proof/context.
- Same-session continuation is stopped when the same action repeats without new evidence, context budget is exhausted, or the blocker envelope says human/Neko/QA input is required.
- If a bundle is `queued_waiting_dependency`, Harness creates or updates the assignment as waiting but does not launch the Dev run.
- When the dependency wakes, Harness resumes the same assignment and same persona session if the session is healthy.

Tests:

- Bundle assignment includes every stage for that repo.
- Dev delivery records `repo_bundle_id`, changed files, self-test evidence, contract packet if present, and remaining risk.
- Dev blocker records `repo_bundle_id`, blocker type, attempted self-service, and minimum next signal.
- Assignment is the only active-owner authority.
- Stage-level assignment creation is rejected for normal repo-owned work.
- Same-session Dev continuation resumes the existing persona session and assignment.
- Repeated same-session no-progress actions trigger Neko/QA steering or a blocker, not a new blind run.
- Waiting dependency assignment is visible in status/snapshot/Mission Control without active run.
- Dependency wake resumes the waiting assignment rather than creating a duplicate assignment.
- Delivered bundle enters `delivered_waiting_for_qa` without active run while other bundles or QA finish.
- QA rejection reopens the same bundle assignment and resumes the same Dev persona instance when healthy.

### 57D - Harness-Owned State Transitions

Move transition authority fully into the Harness.

Required behavior:

- Dev `deliver` marks the repo bundle delivered.
- Delivered bundles with pending QA/final proof are represented as `delivered_waiting_for_qa`.
- Harness waits until all required repo bundles are delivered or blocked.
- Harness automatically wakes queued dependent bundles when required upstream contract/proof outputs are present.
- When all required bundles are delivered, Harness runs final gate proof.
- Harness routes QA after final gates are attached or records a proof blocker.
- Dev `block` automatically routes Neko or human depending on blocker class.
- QA `approve` marks bundles verified and advances the mission.
- QA `reject` routes the relevant repo bundle back to the same Dev with exact findings.
- Neko `route` creates or resumes the next persona assignment.
- Neko steering resumes the current assignment/session when possible and injects a bounded scope/routing update.
- QA rejection resumes the same Dev bundle/session when possible and injects the exact failed proof or acceptance gap.
- Agents cannot set task state, stage status, assignment state, worker state, or incident state directly.
- Assignment is the only routing authority for "active owner".
- If task/stage/run/worker session disagree with assignment, the Harness records an observability finding and routes by assignment.

Tests:

- Dev delivery cannot directly mark task `done`.
- QA approval is the only normal path to release completion.
- QA rejection routes same Dev owner with cited findings.
- Dev blocker with `environment_blocker` creates an incident; Dev blocker with `scope_ambiguity` routes Neko.
- Conflicting active run/worker/task state does not confuse routing when assignment is clear.
- Multi-repo QA does not run until all required repo bundles are delivered or truthfully blocked.
- Neko and QA steering preserve persona session continuity unless the prior session is stale, corrupt, over budget, or explicitly closed.
- A queued dependent bundle does not count as blocked; it counts as waiting with a concrete wake condition.
- A delivered bundle waiting for QA does not count as active; it counts as complete unless QA rejects it.

### 57E - Context Request Budget And Terminal Feedback Policy

Make context expansion deterministic.

Required behavior:

- Every stage kind has a context budget:
  - `investigate`: one `locator_context`, one `exact_file_context`;
  - `implement`: local tools allowed, Harness context optional and rare;
  - `verify`: one `proof_context` or one `request_missing_proof`;
  - `release`: no broad context.
- Fulfilled context requests count as progress only once.
- Repeating `request_context` after the budget is exhausted is rejected with a repair prompt that allows only `deliver` or `block`.
- Terminal feedback says what changed, what was fulfilled, what failed, and what next actions are valid.
- Context request payloads use `context_mode` plus bounded `paths`; arbitrary repeated path batches are rejected.

Tests:

- A no-edit investigation can request `.` or top-level directories once and receive affected-repo directory listings.
- A second exact-file request is allowed after the locator bundle.
- A third context request is invalid and returns a repair HUD forcing `deliver` or `block`.
- A fulfilled context request from the wrong root cannot happen; affected repo wins over Harness cwd.
- Repeating the same context mode/path without new evidence returns terminal feedback, not a new run.

### 57F - Simplified Proof And Gate Flow

Remove proof routing from normal Dev work.

Required behavior:

- Product-edit Dev runs focused self-tests in-session when possible, then returns `deliver`.
- Harness automatically runs final gate proof after Dev `deliver`.
- QA verifies final gate proof and required visual proof.
- QA can request exactly one missing proof if Harness proof is absent, stale, or wrong.
- Dev does not see `request_test_run` in product-edit HUDs.
- No-edit proof-only/certification compatibility remains behind an explicit migration flag.

Tests:

- Product-edit Dev HUD does not expose `request_test_run`.
- Dev `deliver` with changed files schedules the final gate automatically.
- Failed final gate routes same Dev assignment with proof ID and repair summary.
- QA missing proof request creates one proof assignment and cannot loop without a new proof result.

### 57G - QA Aggregation And Release

QA verifies the mission outcome, not individual internal stage mechanics.

Required behavior:

- Harness may create the QA assignment early, but it remains `queued_waiting_bundles` until wake conditions are satisfied.
- QA wake condition is all required repo bundles delivered plus required final gate/visual proof attached.
- QA receives one review packet containing:
  - delivered repo bundles;
  - changed files by repo;
  - self-test evidence by repo;
  - Harness final gate proof IDs;
  - visual proof IDs when required;
  - contract packets and consumed proof IDs for cross-repo work;
  - known risks/blockers.
- QA `approve` approves the whole mission or the explicitly scoped release slice.
- QA `reject` must identify which repo bundle or proof lane failed.
- Harness routes rejected findings to the matching bundle owner.
- QA rejection reactivates only the rejected bundle owner; other delivered Devs remain idle.
- QA cannot create new repo bundles or reassign owners.
- QA does not poll or spend tokens while waiting for a Dev bundle, final gate, or visual proof.

Tests:

- QA assignment can exist in `queued_waiting_bundles` with no active run.
- QA queue state shows which repo bundle/proof lane it is waiting for.
- QA wakes automatically when the last required Dev bundle delivers and final proof is attached.
- QA does not run while one required Dev bundle is still running or blocked.
- QA packet includes all delivered bundles before approval.
- QA rejection against Backend routes Backend bundle repair only.
- QA rejection against Launcher routes Launcher bundle repair only.
- QA rejection does not wake unrelated delivered Dev bundles.
- Cross-repo contract packet is visible to QA without requiring another context request.
- Mission reaches `done` only after QA approval and required archives/proofs are present.

### 57H - Simplified HUD Projection

Render the simple contract in the Mission HUD while preserving raw logs internally.

Required behavior:

- HUD shows one current phase:
  - `created`;
  - `planning`;
  - `working`;
  - `verifying`;
  - `repair`;
  - `blocked`;
  - `done`;
- HUD shows the role's three or four valid actions only.
- HUD shows local task list progress, but not global state mutation controls.
- HUD shows terminal feedback from the previous action.
- HUD shows the unified blocker envelope when blocked.
- Mission Control event feed can still display raw events/proofs/context as expandable details.

Tests:

- HUD never exposes internal state mutation fields to Dev or QA.
- HUD for repeated context loop switches to `working` with `deliver`/`block` only and terminal feedback explaining context budget exhaustion.
- Launcher snapshot renders persona logs for Neko, Launcher Dev, Backend Dev, and QA using the same simplified phase labels.
- The primary UI never shows raw internal state enums as workflow labels.

### 57I - Skill And Prompt Simplification

Update persona skills so they match the small contract.

Required skill changes:

- `harness-mission-lead`: Neko plans/routes/clarifies/accepts only.
- `harness-dev-delivery`: Dev requests bounded context, delivers, or blocks. No more broad proof-routing language for no-edit investigations.
- `harness-qa-verdict`: QA approves/rejects/requests one missing proof.
- Skills refer to external stage kinds only: `investigate`, `implement`, `verify`, `release`.

Required prompt changes:

- The core Harness overlay states that agents do not own state transitions.
- For no-edit investigation stages, the prompt explicitly says: after allowed context is fulfilled, deliver the best grounded report or block.
- Validation repair must include the allowed action list and the reason the prior action was rejected.
- Proof requests are Harness-owned except explicit proof-only compatibility stages.
- Blockers must use the unified blocker envelope.

Tests:

- Prompt/HUD regression tests assert the simplified action names appear.
- Prompt/HUD regression tests assert hidden internal actions do not appear for the wrong role.
- Malformed payload repair shows closed choices and does not crash the state machine.

### 57J - Observability, Daemon Semantics, And Loop Prevention

Make loops visible and self-healing.

Required behavior:

- Counters:
  - repeated context requests per stage;
  - repeated no-progress Dev deliveries;
  - repeated QA missing-proof requests;
  - repeated Neko replan attempts;
  - same-session continuation count;
  - provider latency and token totals per persona.
- Watchdogs:
  - stop Dev after context budget exhaustion and force deliver/block;
  - stop QA after one missing-proof request unless new proof arrived;
  - stop Neko after one replan unless objective or blocker changed;
  - close or cancel stale active runs when daemon stop is requested.
- Daemon contract:
  - `daemon stop` means no daemon process remains;
  - no active run remains unless `--keep-runs` is explicitly supplied;
  - stopped runs receive a clear `runtime` blocker or cancellation reason.
- Mission Control shows why the Harness intervened.

Tests:

- Context-loop watchdog transitions the assignment to repair/blocked without opening a runaway budget incident.
- Daemon stop leaves no active run process/session behind.
- Status/snapshot show loop counters and next allowed action.

### 57K - Live Token Certification

Run live tests after normal tests pass.

Required live tests:

1. No-edit investigation goal:
   - Neko scopes;
   - Backend Dev requests bounded context at most twice;
   - Backend Dev delivers investigation report or exact blocker;
   - QA approves/rejects from evidence;
   - task reaches done or a truthful terminal blocker.

2. Simple product-edit Launcher goal:
   - Neko scopes;
   - Launcher Dev patches and self-tests;
   - Harness runs final gate;
   - QA verifies;
   - task reaches done.

3. Cross-stack product goal:
   - Neko routes Backend Dev then Launcher Dev;
   - contract handoff is explicit;
   - QA verifies final proof;
   - no role repeats broad discovery after proof/context is present.

Certification metrics:

- No manual nudge after creation except stopping a confirmed runtime bug.
- No repeated same action without new evidence.
- No agent sees internal-only state mutation choices.
- No daemon stop leaves a live Python daemon or active run behind.
- Mission Control shows every persona's logs and final phase.

## Risk Controls

Do not delete the rich internal records during this stage. The simplification is a projection and authority change, not a storage rewrite.

Keep existing decision types available behind compatibility adapters until live certification passes:

- `request_test_run` can remain internal/Harness-owned for proof gates.
- `propose_patch` can remain an internal normalized form of Dev `deliver`.
- `report_qa_verdict` can remain an internal normalized form of QA `approve`/`reject`.
- existing archive manifests must continue preserving old and new records.

Avoid making all roles unlimited. The goal is not fewer safeguards; the goal is fewer agent-facing choices and stronger Harness-owned intervention.

## Definition Of Done

Stage 57 is complete only when:

- simplified HUD menus are enforced by tests for every role;
- only four external stage kinds reach agent HUDs: `investigate`, `implement`, `verify`, `release`;
- no-edit investigation context budget is enforced by tests;
- context requests use named modes and reject repeated arbitrary path batches;
- product-edit Dev does not see `request_test_run` in the normal HUD;
- Harness auto-runs final gates after Dev delivery;
- QA can request only one missing proof unless new proof evidence arrives;
- Harness, not agents, owns all task/stage/assignment/run transitions;
- `PersonaAssignment` is the active-owner authority when task/stage/run/session state disagree;
- blockers normalize to the unified blocker envelope;
- runtime stop/cancel leaves no active runs or daemon process;
- live token no-edit investigation completes or blocks truthfully without babysitting;
- live token product-edit goal completes with QA;
- Mission Control displays simple phase/action state while preserving raw event details.
