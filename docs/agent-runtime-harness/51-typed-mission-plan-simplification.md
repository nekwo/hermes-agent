# Stage 51 Typed Mission Plan Simplification

## Purpose

Stage 51 removes the fragile part of the current Harness: prose and risk-flag inference deciding whether a role should patch, request proof, release another specialist, or let QA close the goal.

The live Mission Control all-role terminal test exposed the failure mode:

- Backend Dev correctly selected `request_test_run` for `backend_contract_smoke`.
- The Harness rejected or hid that choice when prose mentioned downstream Launcher UI repair.
- After guard fixes, Backend proof passed, but Neko narrowed the whole mission into a Backend-only stage.
- QA approved that Backend-only stage as if it satisfied the original all-role Launcher UI goal.

That is not an agent-quality problem. It is an orchestration contract problem.

Stage 51 keeps rich internal observability, worker sessions, proof records, and archive safety, but simplifies the mission model so agents operate against explicit typed stages rather than text classifiers.

## Target Outcome

After Stage 51, the Harness should behave like one uninterrupted competent agent per role:

- Neko creates or repairs a typed mission plan.
- Dev agents work only their assigned typed stage.
- QA can only verify when the typed dependency graph says all required implementation/proof stages are complete.
- The HUD exposes a small action menu based on typed fields, not inferred prose.
- Prose remains useful context, never the source of truth for stage kind, owner, dependencies, or QA eligibility.

## Readiness Audit Verdict

The design is ready to implement after the clarifications in this document.

The critical rule is: add exactly one new orchestration authority, the typed mission plan. Do not create a parallel route beside the current stage system and let both make independent decisions. `TaskStage`, `Task.current_stage_id`, risk flags, and handoff packets can remain as compatibility projections, but when `mission_plan.enabled=true` the typed plan must own:

- current owner
- current stage
- stage kind
- proof recipe
- dependency readiness
- QA eligibility
- terminal completion eligibility

This avoids repeating the Stage 47-50 failure pattern where a stopgap heuristic fixes one live goal and creates another hidden route through old prose classifiers.

## Implementation Surface Map

Harness files expected to change:

- `agent_runtime/models.py`: add typed mission plan dataclasses and `Task.mission_plan`.
- `agent_runtime/serde.py`: prove additive optional typed fields load old v1 task JSON safely.
- `agent_runtime/runtime_config.py`: add `MissionPlanConfig`.
- `agent_runtime/config.py`: load `agent_runtime.mission_plan`, default off for standard Hermes.
- `agent_runtime/mission_plan.py`: new pure typed-plan helpers, validation, planner repair, routing queries.
- `agent_runtime/state_machine.py`: early typed-plan routing branch when enabled and a plan exists.
- `agent_runtime/planning.py`: update typed plan state from Neko, Dev, proof, and QA decisions before legacy fallback.
- `agent_runtime/worker_actions.py`: project the existing worker action menu from typed stage fields instead of `stage_intent` heuristics.
- `agent_runtime/context_builder.py`: render typed plan, typed current stage, allowed actions, forbidden reasons, and validation repair hints in the Mission HUD.
- `agent_runtime/decision_contract_registry.py`: add typed-plan payload keys and HUD shape metadata without adding new top-level `DecisionType` values unless unavoidable.
- `agent_runtime/decision_contracts.py`: validate optional typed mission plan payloads and reject unknown typed stage keys.
- `agent_runtime/proof_gates.py`: gate QA against typed blocking stages and typed proof requirements.
- `agent_runtime/ticker.py`: attach command/visual proof IDs to the typed stage and keep same-session continuation policy aligned with typed owner changes.
- `agent_runtime/snapshot.py` and `agent_runtime/observability.py`: expose typed plan readiness and role/stage event streams for Mission Control.

Launcher files expected to change only after the Harness snapshot contract exists:

- `lib/features/mission_control/data/mission_control_snapshot.dart`: parse typed plan, typed stages, and role streams.
- `lib/features/mission_control/mission_control_page.dart`: render role streams as DM-style event rows grouped by role and typed stage.
- `test/features/mission_control/mission_control_snapshot_test.dart`: parse fixture coverage.
- `test/features/mission_control/mission_control_page_test.dart`: visual/semantic widget coverage for Neko, Backend Dev, Launcher Dev, and QA streams.

## Compatibility Strategy

Use existing `AgentDecision` values for Stage 51. The simplified agent menu is a HUD/action projection layer, not a new public decision protocol.

Recommended mapping:

- `set_or_repair_plan` -> `propose_acceptance` with an optional typed `mission_plan` or `mission_plan_patch` payload.
- `release_next_stage` -> `propose_acceptance` with typed `release_stage_id`.
- `route_recovery` -> `resolve_incident`, `triage_issue_discovery`, or `propose_acceptance` depending on the existing blocker.
- `deliver_patch` -> `propose_patch`.
- `request_gate` -> `request_test_run`, `request_screenshot`, or `request_video`.
- `request_context` -> `request_file_reads`.
- `block` -> `block`.
- `request_missing_proof` -> `request_test_run`, `request_screenshot`, or `request_video`.
- `report_verdict` -> `report_qa_verdict`.

Do not add new top-level `DecisionType` values unless an implementation blocker appears. New decision types would force a larger schema/persona migration and are unnecessary for the current simplification.

## Do Not Double Implement

Stage 51 must avoid split authority:

- `mission_plan` is authoritative when enabled.
- `TaskStage` remains a legacy compatibility projection.
- `risk_flags` remain observability/advisory only.
- `handoff_packet` remains an evidence packet, not a router.
- `stage_intent.py` remains legacy fallback only.
- `_stage_mentions_launcher`, `_is_cross_stack_backend_first`, `_needs_cross_stack_launcher_completion`, `_ensure_pending_dev_handoff_stage`, and similar helpers must not run the typed-plan route.

The implementation should include regression tests proving old prose helpers cannot override a typed plan.

## Core Simplification

Add a typed mission plan layer:

```json
{
  "mission_intent": {
    "title": "Fix Mission Control all-role live terminals",
    "objective": "Original user goal, immutable except explicit operator edit",
    "acceptance_criteria": ["Full-goal criteria"]
  },
  "stages": [
    {
      "id": "backend_stream_seed",
      "owner": "backend_dev",
      "repo": "EterniaBackend",
      "kind": "proof_only",
      "proof_recipe_id": "backend_contract_smoke",
      "requires_product_edit": false,
      "requires_visual_proof": false,
      "depends_on": [],
      "blocks_qa_until": true
    },
    {
      "id": "launcher_terminal_ui_fix",
      "owner": "dev",
      "repo": "EterniaLauncher",
      "kind": "implementation",
      "proof_recipe_id": null,
      "requires_product_edit": true,
      "requires_visual_proof": true,
      "depends_on": ["backend_stream_seed"],
      "blocks_qa_until": true
    },
    {
      "id": "qa_release",
      "owner": "qa",
      "repo": "EterniaLauncher",
      "kind": "qa_verdict",
      "depends_on": ["backend_stream_seed", "launcher_terminal_ui_fix"]
    }
  ]
}
```

The existing `TaskStage` can remain for compatibility, but Stage 51 should add a parallel `mission_plan` object as the single authority. Legacy stage fields are projections from that plan when the typed route is enabled.

## Agent-Facing Options

Neko should have only these normal choices:

- `set_or_repair_plan`: create or repair typed stages and dependencies.
- `release_next_stage`: release the next unblocked stage owner.
- `route_recovery`: route a failed/stalled stage to the same owner, a different owner, or block with evidence.
- `block`: only for real environment/human/safety blockers.

Dev should have only these normal choices:

- `deliver_patch`: for `kind=implementation`.
- `request_gate`: for `kind=proof_only`, final gates after delivery, or failed-gate repair.
- `request_context`: one bounded missing file/log/context item.
- `block`: exact blocker evidence.

QA should have only these normal choices:

- `request_missing_proof`: command or visual proof required by the typed plan.
- `report_verdict`: approve/block based on all dependency proof.
- `block`: exact verification blocker.

## Non-Goals

- Do not delete the rich event model.
- Do not delete worker sessions, context receipts, proof artifacts, or archive manifests.
- Do not make everything unlimited.
- Do not keep expanding word lists to decide stage ownership or QA eligibility.
- Do not let Neko replace the parent goal with a substage description.

## Stage 51A. Schema and Compatibility

Add typed mission-plan support behind a config gate, default off for standard Hermes:

- `mission_plan.enabled`
- `mission_plan.enforce_routing`
- `mission_plan.enforce_hud`
- `mission_plan.version`
- `mission_intent`
- `stages[].owner`
- `stages[].repo`
- `stages[].kind`
- `stages[].proof_recipe_id`
- `stages[].requires_product_edit`
- `stages[].requires_visual_proof`
- `stages[].depends_on`
- `stages[].blocks_qa_until`

Runtime enablement:

- Add defaults in `RuntimeConfig` with `enabled=false`.
- Load `agent_runtime.mission_plan` from `config.py`.
- After unit tests prove legacy fallback, enable the typed route only in Tony/Alice runtime config: `X:\Eternia\.hermes\profiles\alice\config.yaml`.
- Do not enable globally for every Hermes profile in this stage.

Concrete model shape:

```python
@dataclass(slots=True)
class MissionIntent:
    title: str
    objective: str
    acceptance_criteria: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    source_task_id: str | None = None
    locked: bool = True


@dataclass(slots=True)
class MissionPlanStage:
    id: str
    title: str
    objective: str
    owner: str
    repo: str
    kind: str
    status: StageStatus = StageStatus.READY
    proof_recipe_id: str | None = None
    requires_product_edit: bool = False
    requires_visual_proof: bool = False
    depends_on: list[str] = field(default_factory=list)
    blocks_qa_until: bool = True
    proof_ids: list[str] = field(default_factory=list)
    packet_ids: list[str] = field(default_factory=list)
    blocker_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class MissionPlan:
    version: int = 1
    enabled: bool = True
    mission_intent: MissionIntent | None = None
    stages: list[MissionPlanStage] = field(default_factory=list)
    current_stage_id: str | None = None
    revision: int = 0
```

Allowed values:

- `owner`: `neko_supervisor`, `dev`, `backend_dev`, `qa`, `harness`, `human`.
- `repo`: `EterniaLauncher`, `EterniaBackend`, `hermes-agent`, `none`.
- `kind`: `planning`, `proof_only`, `implementation`, `qa_verdict`, `recovery`, `context`.
- `status`: reuse `StageStatus` values first. Do not add a second status enum unless reuse proves impossible.

Persistence rule:

- Add `Task.mission_plan: MissionPlan | None = None`.
- Keep `Task.schema_version=1` unless a non-additive migration is required.
- Old task JSON must load with `mission_plan=None`.
- New typed tasks must still write legacy `stages` and `current_stage_id` projections for Mission Control and archive compatibility.

Compatibility rule:

- If typed fields exist, routing and HUD use typed fields.
- If typed fields do not exist, fall back to current behavior.
- During migration, write both typed fields and legacy `TaskStage` fields.

Implementation-ready tests:

- `test_models_serde.py`: old v1 task JSON without `mission_plan` loads.
- `test_models_serde.py`: typed task round-trips through `to_jsonable/from_jsonable`.
- `test_config.py`: `mission_plan` config defaults off and loads enabled/enforce settings.
- `test_migrations.py`: migration status reports typed-plan-safe and does not rewrite archives.

## Stage 51B. Deterministic Planner

Add `agent_runtime/mission_plan.py` with pure helpers that convert Neko payloads into typed stages and validate typed plans.

Rules:

- Parent mission intent is copied from original task title/description/acceptance and not overwritten by stage scope.
- Neko can add, reorder, or repair stages, but cannot silently shrink the mission acceptance criteria.
- If Neko emits a backend-only stage while the parent goal mentions Launcher/UI/QA follow-up, the planner must preserve a pending Launcher stage.
- If a stage has `proof_recipe_id`, its `kind` defaults to `proof_only` unless Neko explicitly marks it as implementation and the recipe is compatible.
- If a stage has `owner=qa`, it must depend on all blocking stages.

Required helpers:

- `mission_plan_enabled(config) -> bool`
- `has_typed_plan(task) -> bool`
- `ensure_mission_plan(task, payload=None, actor=None) -> MissionPlan`
- `validate_mission_plan(plan) -> list[str]`
- `mirror_legacy_stages_from_plan(task) -> None`
- `current_plan_stage(task) -> MissionPlanStage | None`
- `next_unblocked_stage(task) -> MissionPlanStage | None`
- `blocking_stages_ready_for_qa(task, proof_store=None) -> GateResult`
- `attach_proofs_to_plan_stage(task, stage_id, proof_ids, proof_store=None) -> None`
- `mark_plan_stage_from_decision(task, decision, actor, proof_store=None) -> None`

Planner normalization:

- `proof_recipe_id` must resolve through `resolve_proof_recipe`.
- `proof_only` stages must default `requires_product_edit=false`.
- `implementation` stages must default `requires_product_edit=true`.
- `qa_verdict` stages must default `blocks_qa_until=false`.
- unknown owners, repos, kinds, duplicate stage IDs, and dependency cycles are validation errors.
- a parent goal mentioning Launcher/UI/Mission Control must retain a Launcher-owned implementation or visual-proof stage unless the original parent acceptance criteria explicitly says backend-only.

Implementation-ready tests:

- Neko cannot shrink parent mission intent into a backend-only stage.
- Planner preserves existing parent acceptance criteria even if Neko emits narrower objective text.
- Planner rejects dependency cycles.
- Planner rejects unknown owner/repo/kind.
- Planner resolves known proof recipes and rejects unknown recipe IDs.
- Planner mirrors typed stages into legacy `TaskStage` without making legacy stages authoritative.

## Stage 51C. Decision Contract and HUD Shape Bridge

The current HUD menu already exists in `worker_actions.py` and `context_builder.py`. Stage 51 should replace its inputs, not create a second menu.

Rules:

- `worker_actions_for_role` should branch to typed-plan projection when `mission_plan.enabled=true` and `task.mission_plan` exists.
- Normal worker flow stays the visible contract mode.
- `decision_menu` entries must include `allowed_payload_keys`, `recommended_payload`, `enum_choices`, and forbidden reasons for hidden actions.
- Invalid output repair must point the agent back to one of the visible `decision_menu` entries.
- The model should never need to infer the valid shape from prose.

Typed action projection:

- Neko current `kind=planning` -> `set_or_repair_plan`.
- Neko dependency join complete -> `release_next_stage`.
- Neko blocked/recovery stage -> `route_recovery`.
- Dev `kind=proof_only` -> `request_gate`.
- Dev `kind=implementation` before delivery -> `deliver_patch`.
- Dev `kind=implementation` after delivery but missing proof -> `request_gate`.
- Dev no executable typed stage -> `request_context` or `block`.
- QA missing command proof -> `request_missing_proof`.
- QA missing visual proof -> `request_missing_proof`.
- QA all required proof present -> `report_verdict`.

Implementation-ready tests:

- Backend proof-only stage with downstream Launcher text exposes `request_gate`, not `deliver_patch`.
- Launcher implementation stage exposes `deliver_patch` before delivery and proof request after accepted delivery.
- QA visual-required stage exposes screenshot/video proof request before verdict.
- Validation repair HUD includes the current typed stage and exact allowed payload keys.
- Hidden actions include exact `not_allowed_reason`.

## Stage 51D. Routing Authority

Replace cross-stack prose/risk-flag routing with dependency routing.

Routing rules:

- Find the first stage with unmet dependencies and incomplete status.
- If no non-QA stages are incomplete, route QA.
- If QA passes and all blocking stages are passed, complete the task.
- If a stage fails proof and environment changed, route same owner once.
- If a stage fails proof and environment did not change, route Neko recovery.
- Never complete a parent goal because one substage passed.

This should make `cross_stack_contract_handoff`, `backend_contract_first`, `neko_qa_coordination_released`, and similar flags advisory observability only, not routing authority.

Implementation notes:

- Add optional `config` to `MissionStateMachine` and default to legacy behavior when config is absent.
- In `next_action`, check typed-plan authority before legacy state/prose helpers.
- `status.py`, `snapshot.py`, `ticker.py`, and tests that instantiate `MissionStateMachine()` must either pass config or preserve legacy fallback when config is absent.
- For typed plans, `_advance_to_next_dev_stage` must use dependency readiness, not list order alone.
- `COMPLETE_TASK` is legal only after typed QA verdict and all blocking typed stages are passed.

Implementation-ready tests:

- Backend proof completion routes Neko, not QA, when Launcher stage is pending.
- Neko release activates Launcher stage after backend proof.
- QA is blocked until Backend and Launcher blocking stages pass.
- A single passed substage cannot complete the parent goal.
- Typed plan routing ignores `risk_flags` that would otherwise imply QA release.
- Legacy tasks with no typed plan keep existing fallback behavior.

## Stage 51E. Decision Application and Stage Updates

Typed routing must be matched by typed side effects.

Decision side-effect rules:

- Neko `set_or_repair_plan` updates `Task.mission_plan.revision`, preserves `mission_intent`, and mirrors legacy stages.
- Neko `release_next_stage` sets `mission_plan.current_stage_id` to the next unblocked stage and mirrors `Task.current_stage_id`.
- Dev `deliver_patch` marks current typed implementation stage as `IMPLEMENTING` with delivery packet ID; it does not make the stage QA-ready without proof.
- Dev/QA `request_gate` attaches command proof IDs to the current typed stage.
- Passing proof marks proof-only stages `READY_FOR_QA` and implementation stages `READY_FOR_QA` only when required proof is complete.
- Failed proof leaves the stage incomplete and records self-heal metadata on the typed stage.
- QA `report_verdict approved` marks all cited ready blocking stages `PASSED` only when proof coverage matches the typed plan.
- QA `blocked` or `needs_fixes` routes back to the typed owner that owns the failed/missing stage.

Implementation locations:

- `planning.py`: call typed plan update helpers at the start/end of relevant decision branches.
- `ticker.py`: after `_collect_command_proof` and `_collect_visual_proof`, call `attach_proofs_to_plan_stage`.
- `qa_verdict.py`: include typed stage coverage in QA proof metadata.
- `packets.py`: keep packet IDs linked from typed stages when packets are recorded.

Implementation-ready tests:

- `request_test_run` proof ID is recorded on both `task.proof_ids` and current typed stage `proof_ids`.
- failed proof does not mark typed stage ready.
- passed no-edit proof marks proof-only typed stage ready without product edits.
- Dev delivery without final gate does not unlock QA.
- QA blocked verdict routes to the exact typed owner with missing proof.

## Stage 51F. HUD Projection

Project the HUD from typed stage fields:

- `kind=proof_only` plus `proof_recipe_id` -> primary `request_gate`.
- `kind=implementation` and no accepted delivery -> primary `deliver_patch`.
- `kind=implementation` after delivery -> primary final gate.
- `owner=qa` and missing visual proof -> primary `request_missing_proof`.
- `owner=qa` and all required proof present -> primary `report_verdict`.

The HUD should include:

- current typed stage id
- owner
- repo
- kind
- dependencies
- allowed actions
- forbidden actions with exact reason
- completion blockers

It should not ask agents to infer allowed actions from prose.

## Stage 51G. Proof and QA Gates

Proof collection should attach to typed stages.

QA eligibility:

- Every stage with `blocks_qa_until=true` must be `ready_for_qa` or `passed`.
- Required proof IDs must exist and be passed.
- Required visual proof must exist if any completed implementation stage requires it.
- QA verdict must cite typed stage coverage.

Completion eligibility:

- QA approved.
- All blocking stages passed.
- No open incidents.
- No stale active worker sessions.
- Dirty state is clean or explicitly waived.

Implementation-ready tests:

- `can_qa_approve` reports each missing typed blocking stage by stage ID.
- visual proof is required only for typed stages that require it or task-level visual requirements.
- QA verdict proof must cite all required typed stage proof IDs.
- stale/wrong-stage proof IDs cannot satisfy the current typed stage.
- archived typed tasks preserve typed plan and proof references.

## Stage 51H. Mission Control UI Contract

Mission Control should render agent streams from runtime facts, not only task terminal status.

For each role:

- show active and recent `worker_session.*`
- show active and recent `run.*`
- show `run.tool.*`
- show `proof.attached`
- show `packet.recorded`
- show validation repair events

The UI should group by role and typed stage, so Neko, Backend Dev, Launcher Dev, and QA all have visible event streams even if only one role is currently active.

Harness snapshot contract:

```json
{
  "mission_plan": {
    "enabled": true,
    "current_stage_id": "launcher_terminal_ui_fix",
    "stages": []
  },
  "role_streams": [
    {
      "persona_id": "backend_dev",
      "display_name": "Backend Dev Agent",
      "current_stage_id": "backend_stream_seed",
      "events": []
    }
  ],
  "stage_streams": [
    {
      "stage_id": "launcher_terminal_ui_fix",
      "owner": "dev",
      "events": []
    }
  ]
}
```

Snapshot rules:

- role streams include inactive roles if they have recent events for the task.
- events must be redaction-safe display projections, not raw prompt text.
- hidden provider chain-of-thought stays hidden; show safe reasoning summary placeholders only.
- raw logs/proofs remain preserved in Harness storage and archives.
- Launcher should render “no events yet” truthfully instead of appearing frozen.

Implementation-ready tests:

- `snapshot.py` exposes role streams for Neko, Backend Dev, Launcher Dev, and QA on a cross-stack typed task.
- Launcher snapshot parser accepts typed mission plan and role streams.
- Launcher page test renders all four role tabs/streams from a fixture.
- Launcher widget test verifies DM-style rows with expandable details and no giant terminal block.

## Stage 51I. Tests

Required unit tests:

- Neko cannot shrink parent mission intent into a backend-only stage.
- Backend proof-only stage with downstream Launcher UI text exposes only `request_gate`.
- Backend proof completion routes Neko, not QA, when Launcher stage is pending.
- Neko release creates or activates Launcher stage after backend proof.
- QA is blocked until Backend and Launcher blocking stages pass.
- Task cannot complete from a single passed substage.
- Existing legacy tasks still route through fallback behavior when typed plan is absent.

Required integration tests:

- Full sequence: Neko -> Backend Dev proof-only -> Neko -> Launcher Dev implementation -> QA.
- Failed Backend proof routes same Backend Dev once if environment changed.
- Repeated same proof failure routes Neko recovery.
- Launcher UI stage requires visual proof before QA approval.
- Mission Control snapshot exposes role streams for Neko, Backend Dev, Launcher Dev, and QA.

Required live-token certification:

- Simple proof-only goal.
- Complex cross-stack goal with Backend + Launcher + QA.
- Mission Control all-role terminal bug goal.
- Fullscreen visual proof when UI is changed.

## Stage 51J. Implementation Order

1. Add typed plan dataclasses/serde and config gate.
2. Add `mission_plan.py` pure helper module and validation tests.
3. Add deterministic planner from existing Neko payloads to typed stages.
4. Add typed mission plan to context/HUD while leaving routing legacy.
5. Project worker actions from typed stage fields.
6. Add typed routing branch to `MissionStateMachine`.
7. Attach proofs to typed stages and update QA gate.
8. Update snapshot/observability role streams.
9. Add Launcher parser/page tests for typed role streams.
10. Preserve legacy fallback and migration.
11. Run full Harness unit suite.
12. Run focused Launcher Mission Control tests.
13. Run live-token simple proof goal.
14. Run live-token Mission Control all-role terminal goal with fullscreen visual proof.

## Stage 51K. Unsafe Or Risky Changes To Avoid

Avoid these in Stage 51 unless a test proves they are required:

- adding many new top-level `DecisionType` values
- deleting legacy routing in the same patch
- making Stage 51 default-on for every Hermes profile
- accepting unknown typed plan keys from agents
- letting Neko overwrite parent mission intent
- letting QA approve based on task-level proof IDs without typed stage coverage
- running live-token certification before unit tests pass
- changing Launcher visuals before the Harness snapshot contract is stable

## Exit Criteria

Stage 51 is complete only when:

- No live-token goal depends on prose classification for proof-only vs implementation.
- Neko cannot accidentally replace the whole goal with one substage.
- Backend-only proof cannot close a Launcher UI goal.
- QA cannot run before all typed blocking stages are complete.
- Mission Control shows Neko, Backend Dev, Launcher Dev, and QA event streams.
- Full agent-runtime tests pass.
- Live-token complex goal completes or blocks with exact observable reason.

## Current Stopgap Evidence

Stopgap commits before Stage 51:

- `e02d2b475 fix(harness): infer no-edit proof recipes from stage text`
- `8eeb04c1f fix(harness): preserve no-edit proof stages after corrections`
- `c7b492d46 fix(harness): avoid ui text forcing no-edit stages to patch`
- `a591fd62a fix(harness): prevent backend-only proof from closing launcher goals`

These are useful guardrails, but they are not the final architecture. Stage 51 should replace most of this inference with typed mission-plan routing.
