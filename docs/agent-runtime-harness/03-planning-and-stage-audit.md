# Stage 3 — Task Planning + Stage Audit Loop

## Goal

Implement Tony's desired work choreography inside **one durable harness task**:

```text
Task created
  -> PM fleshes it out
  -> Dev audits codebase/context
  -> Dev creates a staged plan
  -> Dev deep-audits each stage and corrects the plan
  -> Dev designs tests for each stage
  -> QA reviews the plan/test design before implementation
```

No implementation begins until QA has approved the plan/test design, or PM/Tony has recorded an explicit waiver.

This stage is the first stage where Stage 1 state and Stage 2 persona decisions are *applied* to task records. It still does **not** apply patches, run implementation tests, or capture proof artifacts; those begin in later stages.

## Deep audit findings from current repo

### Existing harness code to build on

Stage 1 and 2 already introduced the primitives this stage should use:

- `agent_runtime/models.py`
  - `Task` already has `acceptance_criteria`, `non_goals`, `affected_repos`, `suggested_roles`, `stages`, `current_stage_id`, `waiver`, `proof_ids`, and `open_incident_ids`.
  - `TaskStage` already has `affected_paths`, `acceptance_criteria`, `test_plan`, `audit_notes`, `corrections`, and `requires_visual_proof`.
- `agent_runtime/states.py`
  - The planning path already exists: `CREATED -> PM_TRIAGE -> PM_READY_FOR_DEV -> DEV_AUDIT -> DEV_STAGE_PLANNING -> DEV_TEST_DESIGN -> QA_REVIEW_PLAN -> DEV_IMPLEMENTING`.
- `agent_runtime/transitions.py`
  - The table currently allows `QA_REVIEW_PLAN -> DEV_IMPLEMENTING`; Stage 3 must gate that transition through plan approval.
- `agent_runtime/decision_schema.py`
  - PM/dev/QA decision types exist, but payloads are only shape-checked as generic objects. Stage 3 must add semantic validators for planning payloads.
- `agent_runtime/store.py`
  - `TaskStore.update()` appends transition events only when state changes. Stage 3 must add explicit event helpers for planning mutations that do not change `Task.state`.
- `agent_runtime/events.py`
  - `task.stage_added`, `task.stage_updated`, and `task.stage_corrected` already exist in `ALLOWED_EVENT_TYPES`. Stage 3 should use them rather than inventing noisy new event names.
- `agent_runtime/persona_runtime.py`
  - Personas already return typed `AgentDecision`s. Stage 3 should not call the model directly; it should consume already-parsed decisions.

### Existing Kanban code to avoid copying

Kanban has useful decomposition/specification ideas, but its output shape is wrong for Mission Control:

- `hermes_cli/kanban_decompose.py` and `hermes_cli/kanban_specify.py` decompose broad work into cards.
- `hermes_cli/kanban_db.py` has mature concurrency and event/log lessons, but the board/card schema is intentionally not reused.

Stage 3 must keep planning inside the `Task.stages` list and same task JSON. Findings and corrections should mutate stage records, not create new tasks/cards.

### Gaps in current Stage 2 decisions

Current `AgentDecision.payload` is a plain `dict[str, Any]`. Stage 3 must enforce payload contracts for:

- `PROPOSE_ACCEPTANCE`
- `REQUEST_FILE_READS`
- `PROPOSE_STAGE_PLAN`
- `CORRECT_STAGE`
- `REQUEST_TEST_RUN`
- `APPROVE`
- `REPORT_QA_VERDICT`
- `BLOCK`

The validator should be deterministic and side-effect-free so bad model output opens a model-output incident, not a product task failure.

## Package additions

```text
agent_runtime/
  planning.py              # applies PM/dev planning decisions to Task records
  plan_review.py           # PlanReview, Finding, verdict models + gates
  gates.py                 # can_enter_dev_implementing(), waiver checks
  decision_contracts.py    # semantic payload validators for Stage 3 decisions
```

Stage 3 should not add CLI commands, daemon loops, proof artifact capture, or patch application. Those belong to later stages.

## New / expanded models

### Finding

```python
class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(slots=True)
class Finding:
    id: str
    severity: FindingSeverity
    summary: str
    affected_stage_id: str | None = None
    affected_paths: list[str] = field(default_factory=list)
    recommendation: str = ""
    created_by: str = "qa"
    created_at: datetime = field(default_factory=now)
    resolved_at: datetime | None = None
    schema_version: int = 1
```

### PlanReview

```python
class PlanReviewVerdict(StrEnum):
    APPROVED = "approved"
    NEEDS_CORRECTIONS = "needs_corrections"
    BLOCKED = "blocked"


@dataclass(slots=True)
class PlanReview:
    id: str
    task_id: str
    reviewer_agent_id: str
    verdict: PlanReviewVerdict
    findings: list[Finding] = field(default_factory=list)
    reviewed_stage_ids: list[str] = field(default_factory=list)
    proof_requirements_confirmed: bool = False
    test_plan_confirmed: bool = False
    created_at: datetime = field(default_factory=now)
    schema_version: int = 1
```

### Task additions

Prefer adding only small, explicit fields to `Task`:

```python
plan_review: PlanReview | None = None
planning_locked: bool = False
context_requests: list[ContextRequest] = field(default_factory=list)
```

If adding nested dataclasses makes Stage 3 too broad, store plan-review objects under `planning/<task_id>/plan_review.json` and keep `Task.plan_review_id` instead. The deciding criterion is JSON readability; avoid a separate database.

## Decision payload contracts

### `PROPOSE_ACCEPTANCE` — PM fleshing

Allowed role: PM.

Required payload:

```json
{
  "objective": "One-sentence objective",
  "acceptance_criteria": ["Observable criterion"],
  "non_goals": ["Explicitly out of scope"],
  "affected_repos": ["optional repo/path hint"],
  "suggested_roles": ["dev", "qa"],
  "requires_visual_proof": true,
  "risk_flags": ["windows-paths", "launcher-ui"]
}
```

Application rules:

1. Update `Task.description` only if PM provides a clearer `objective`; do not erase Tony's raw request. Store the raw request in `Task.metadata.raw_request` if a metadata field is added later.
2. Replace `acceptance_criteria`, `non_goals`, `affected_repos`, and `suggested_roles` with normalized lists.
3. Set `requires_visual_proof` from PM classification.
4. Transition `CREATED -> PM_TRIAGE -> PM_READY_FOR_DEV`, or if the task is already in `PM_TRIAGE`, transition to `PM_READY_FOR_DEV`.
5. Append a planning event with safe summary only; do not inline the full prompt.

### `REQUEST_FILE_READS` — context request

Allowed roles: Dev, QA.

Required payload:

```json
{
  "paths": ["relative/or/absolute/path.py"],
  "reason": "Why these reads are needed",
  "stage_id": "optional-stage-id"
}
```

Application rules:

1. Record the request as structured context demand.
2. Do not transition task state by default.
3. Harness later satisfies this by reading files and feeding summaries into the next tick.
4. Paths must be normalized; reject traversal into secrets or unrelated user directories unless explicitly allowed by Tony.

### `PROPOSE_STAGE_PLAN` — dev plan creation / test design

Allowed role: Dev.

Required payload:

```json
{
  "stages": [
    {
      "id": "optional-stable-id",
      "title": "Short stage title",
      "objective": "One objective",
      "affected_paths": ["agent_runtime/planning.py"],
      "acceptance_criteria": ["Stage-specific criterion"],
      "test_plan": ["pytest tests/agent_runtime/test_planning.py -q"],
      "requires_visual_proof": false
    }
  ],
  "audit_notes": ["global plan note"]
}
```

Application rules:

1. If no `id` is supplied, generate stable `stage_<n>` IDs in list order.
2. Duplicate IDs in one payload are rejected.
3. Existing stage IDs are updated in place, not duplicated.
4. New stages are appended in payload order.
5. No stage may have empty `title`, `objective`, or `acceptance_criteria`.
6. If any stage modifies UI/game/visual/Launcher flows, either stage or task `requires_visual_proof` must be true.
7. Transition:
   - `DEV_AUDIT -> DEV_STAGE_PLANNING` when the first valid stage plan lands.
   - `DEV_STAGE_PLANNING -> DEV_TEST_DESIGN` only when every stage has at least one `test_plan` entry or a documented test waiver.

### `CORRECT_STAGE` — deep audit correction

Allowed roles: Dev, QA.

Required payload:

```json
{
  "stage_id": "stage_1",
  "corrections": ["Add Windows path test"],
  "audit_notes": ["Current plan misses profile root behavior"],
  "affected_paths": ["agent_runtime/paths.py"],
  "test_plan": ["pytest tests/agent_runtime/test_paths.py -q"]
}
```

Application rules:

1. `stage_id` must exist. Missing stage IDs are rejected; do not create a new stage from a correction.
2. Append corrections/audit notes with timestamp/persona metadata if model is expanded; otherwise append text with `created_by` prefix.
3. Merge `affected_paths` and `test_plan` without duplicates.
4. If QA emits correction during `QA_REVIEW_PLAN`, transition back to `DEV_STAGE_PLANNING` or `DEV_TEST_DESIGN` depending on missing test coverage.
5. Append `task.stage_corrected`.

### `REQUEST_TEST_RUN` — test design request

Allowed roles: Dev, QA.

Required payload:

```json
{
  "stage_id": "stage_1",
  "commands": ["pytest tests/agent_runtime/test_planning.py -q"],
  "reason": "Validate planning gate behavior"
}
```

Application rules:

1. Stage 3 records intended test commands; it does not execute them.
2. Commands must be stored as plan/test-design data, not proof. Stage 4/5 will execute and create `ProofType.TEST_RUN`.
3. Do not allow shell metacharacter-heavy commands unless later harness command policy approves them.

### `APPROVE` — QA plan approval

Allowed roles: PM, QA. In Stage 3, only QA approval opens implementation gate.

Required payload for QA plan review:

```json
{
  "review_scope": "plan",
  "reviewed_stage_ids": ["stage_1", "stage_2"],
  "findings": [],
  "test_plan_confirmed": true,
  "proof_requirements_confirmed": true
}
```

Application rules:

1. All current stages must be reviewed.
2. All stages must have acceptance criteria.
3. All stages must have test plan or a PM/Tony waiver.
4. Visual proof requirement must be confirmed when task/stage requires it.
5. Write `PlanReview(verdict=APPROVED)`.
6. Transition `QA_REVIEW_PLAN -> DEV_IMPLEMENTING` becomes allowed only after gate passes.

### `REPORT_QA_VERDICT` in Stage 3

Stage 3 should treat `REPORT_QA_VERDICT` as **plan verdict only** when task is in `QA_REVIEW_PLAN`. Implementation QA verdicts belong to Stage 4+.

Required payload:

```json
{
  "verdict": "approved | needs_fixes | blocked",
  "review_scope": "plan",
  "findings": [{"severity": "warning", "summary": "..."}],
  "proof_ids": []
}
```

## Planning transition gates

Add a gate function rather than encoding every condition into `TRANSITION_TABLE`:

```python
def can_enter_dev_implementing(task: Task) -> GateResult:
    if task.waiver and task.waiver.get("gate") == "qa_plan_review":
        return GateResult(True, [])
    if not task.stages:
        return GateResult(False, ["missing stage plan"])
    if not task.plan_review or task.plan_review.verdict != "approved":
        return GateResult(False, ["missing approved QA plan review"])
    ...
```

`apply_transition()` can stay table-only from Stage 1; Stage 3 orchestration code should call gates before requesting that transition.

## Event requirements

Use existing Stage 1 event types:

- `task.stage_added`
- `task.stage_updated`
- `task.stage_corrected`

Add if needed:

- `task.pm_fleshed`
- `plan.reviewed`
- `context.requested`

If adding event types, update `ALLOWED_EVENT_TYPES` and tests in the same commit.

Payload rules:

- Include IDs, counts, and short summaries.
- Do not inline full model rationales > 4 KB.
- Do not include file contents.
- Link large audit detail to artifacts only in later stages.

## Implementation tasks

1. Add failing tests for PM `PROPOSE_ACCEPTANCE` applying fields and transitioning to `PM_READY_FOR_DEV`.
2. Add failing tests for dev `PROPOSE_STAGE_PLAN` appending stages with stable IDs.
3. Add failing tests for duplicate stage IDs being rejected.
4. Add failing tests for `CORRECT_STAGE` updating existing stages without duplication.
5. Add failing tests for QA plan rejection routing back to dev planning/test-design.
6. Add failing tests for QA plan approval opening `DEV_IMPLEMENTING` gate.
7. Implement `decision_contracts.py` semantic validators.
8. Implement `planning.py` decision appliers.
9. Implement `plan_review.py` models and serde support.
10. Implement `gates.py` for plan-review gating.
11. Add events and event tests.
12. Run `python -m pytest tests/agent_runtime/ -q`.

## Tests

Required test files:

```text
tests/agent_runtime/test_decision_contracts.py
tests/agent_runtime/test_planning.py
tests/agent_runtime/test_plan_review.py
tests/agent_runtime/test_gates.py
```

Test matrix:

- PM fleshing applies objective, acceptance criteria, non-goals, repo hints, roles, and visual-proof flag.
- Dev stage plan appends stages and sets `current_stage_id` to first stage when unset.
- Existing stage IDs update in place.
- Duplicate stage IDs in one payload raise `DecisionPayloadInvalid`.
- Empty acceptance criteria rejects stage plan.
- Deep audit correction updates an existing stage only.
- QA correction writes finding and returns task to dev planning/test design.
- QA approval fails if any stage lacks test plan.
- QA approval fails if visual proof requirement is ambiguous.
- QA approval succeeds only with reviewed all stage IDs.
- `DEV_IMPLEMENTING` gate fails before approval and passes after approval.
- Waiver with actor `tony` or PM override can bypass QA plan gate but records reason.

## Acceptance criteria

- One task JSON can hold the complete multi-stage plan.
- No planning operation creates a new task/card.
- Every Stage 3 mutation is deterministic from `(task, decision, actor)`.
- The same decision applied twice is idempotent or explicitly rejected; it must not duplicate stages/corrections.
- A task cannot enter `DEV_IMPLEMENTING` before QA plan approval or explicit waiver.
- Bad model payloads become validation errors/incidents; they do not mutate task state.

## Risks / interventions

- **Planning noise becoming Kanban in disguise:** keep stage titles/objectives short; force concise fields and reject giant payloads.
- **Model overconfidence:** dev cannot skip audit/test design; QA review is mandatory unless waived.
- **Duplicate stage churn:** stage IDs must be stable and updated in place.
- **Visual proof drift:** PM sets initial visual-proof flag, QA can escalate, but only Stage 4 validates actual artifacts.
- **Overbroad stage scope:** reject stages that touch unrelated repos/features unless PM explicitly marks the task as cross-cutting.
