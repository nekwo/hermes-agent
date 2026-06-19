# Stage 12 — Unrelated Issue Forking and Scope Control

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after PM/Neko approves the stage order.

**Goal:** Let Dev/QA report unrelated discoveries without scope creep, let PM/Neko triage them deterministically, and create a separate child mission for approved unrelated work with clean proof and observability.

**Architecture:** Add a narrow issue-discovery contract to the existing Agent Runtime Harness rather than letting Dev mutate task scope directly. Dev/QA can emit a structured discovery, the harness records it as redaction-safe evidence on the current mission, PM decides whether it is blocking/current-scope/fork/defer/escalate, and only the harness creates a child mission when forking is approved. The current mission continues unless the issue is classified as blocking.

**Tech Stack:** `agent_runtime` dataclasses + schema-v1 JSON serde, `TaskStore`/`IncidentStore`/`EventLog`, `MissionStateMachine`, `TickEngine`, `hermes harness` CLI, existing pytest suite under `tests/agent_runtime/`.

---

## Product stance

This is a Mission Control reliability feature, not a Kanban board feature.

The operator-facing behavior should stay simple:

```text
Dev/QA notices issue -> Harness records discovery -> PM triages -> same mission, child mission, defer, or intervention -> QA proves separately
```

Non-negotiables:

- Dev agents may **report** unrelated findings but must not silently expand their mission.
- PM/Neko/harness owns triage and child-mission creation.
- Child missions must have explicit parent links and discovery provenance.
- Current mission only blocks when the PM classification says the finding blocks acceptance, launch safety, security, data integrity, or repo consistency.
- Launcher/Mission Control snapshots expose counts and handles, not raw secrets, file contents, or full model output.

---

## Deep audit findings from current repo

### Existing surfaces to build on

- `agent_runtime/models.py`
  - `Task.parent_task_id` already exists, so child mission lineage can be represented without a schema-version bump.
  - `Task.non_goals`, `acceptance_criteria`, `affected_repos`, `risk_flags`, and `open_incident_ids` are good inputs for scope classification.
  - There is no structured discovery/finding model yet.
- `agent_runtime/decision_schema.py`
  - Current allowed decision types cover planning, proof, block, and human request.
  - No decision type exists for “I found a separate issue.”
  - Role gating is already centralized in `ALLOWED_DECISIONS_BY_ROLE`; this is the correct place to permit Dev/QA reporting but not child creation.
- `agent_runtime/decision_contracts.py`
  - Payload validation exists for acceptance, stage planning, plan review, and context requests.
  - Add payload validation here; do not leave discovery payloads as arbitrary dicts.
- `agent_runtime/planning.py`
  - `apply_planning_decision()` mutates missions from structured decisions.
  - It currently has no branch for discovery reporting or PM triage decisions.
- `agent_runtime/store.py`
  - `TaskStore.create()` and `TaskStore.update()` already emit events and use atomic JSON writes.
  - `IncidentStore` can represent interventions but is too severe/noisy for every discovery. Use incidents only for blocked/escalated findings.
- `agent_runtime/state_machine.py`
  - `MissionStateMachine.next_action()` currently routes task states to PM/Dev/QA.
  - It can enforce “untriaged discovery pauses current mission only when configured/needed,” but the default should avoid blocking unrelated work.
- `agent_runtime/ticker.py`
  - Tick execution is single-action-per-task and already skips tasks with open incidents.
  - Child mission creation should happen through a deterministic harness service before/after applying PM triage, not inside model code.
- `agent_runtime/snapshot.py` and `agent_runtime/observability.py`
  - Redaction-safe snapshot and intervention envelopes already exist.
  - Add discovery counts/triage-needed handles without exposing full discovery details or file paths unless safe/summarized.
- `hermes_cli/harness.py`
  - Current task CLI supports create/list/show only.
  - Add explicit issue/discovery inspect/triage commands only after the model/store contract exists.
- `tests/agent_runtime/`
  - Existing tests cover model serde, decision schema, planning, ticker, snapshot, observability, and redaction.
  - New tests should extend these same files or add focused `test_issue_discovery.py` / `test_scope_triage.py` rather than building a parallel test harness.

### Gaps

- No way for Dev/QA to record an unrelated issue without abusing `BLOCK`, `REQUEST_HUMAN`, `risk_flags`, or freeform audit notes.
- No structured PM decision for “same scope vs child mission vs defer vs intervention.”
- No deterministic child mission creation service with lineage/provenance.
- No proof separation rules that prevent child mission proof from accidentally satisfying parent acceptance criteria.
- No snapshot/observability summary for untriaged discoveries or forked child missions.
- No CLI/operator path for listing/triaging discoveries.
- No explicit prompt/context instructions that tell Dev/QA not to fix unrelated findings inline.

### Key design decision

Use schema-v1-compatible additive fields and dict payloads first.

Do **not** bump `schema_version` yet. `agent_runtime/serde.py` has historically been strict about schema versions, and additive dataclass defaults are safer for persisted stores. If implementation discovers serde rejects extra fields in existing JSON, keep discovery records in a sidecar store under the runtime root instead of bumping `Task.schema_version` in this stage.

---

## Fixed contracts

### Discovery classification terms

Use these exact strings in model decisions and persisted records:

- `blocks_current`: issue must be fixed before current mission can satisfy acceptance criteria or launch safety.
- `same_scope`: issue belongs in the current mission; no child mission.
- `fork_child`: create a separate child mission and let current mission continue.
- `defer`: record for later but do not create a mission now.
- `escalate`: open an intervention incident for Tony/Neko/PM.

### Proposed persisted model

Preferred file: `agent_runtime/scope_control.py`

```python
@dataclass(slots=True)
class IssueDiscovery:
    id: str
    parent_task_id: str
    reported_by: str
    reported_run_id: str | None
    title: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    affected_paths: list[str] = field(default_factory=list)
    severity: str = "medium"  # low|medium|high|critical
    relationship_hint: str = "unknown"  # blocks_current|same_scope|fork_child|defer|escalate|unknown
    triage_status: str = "untriaged"  # untriaged|triaged|forked|deferred|escalated|rejected
    triage_decision: str | None = None
    child_task_id: str | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1
```

Preferred storage options, in order:

1. Add `Task.issue_discoveries: list[dict[str, Any]] = field(default_factory=list)` for schema-v1-compatible embedding.
2. If embedding creates noisy task JSON or serde compatibility issues, use sidecar files: `<runtime_root>/issue_discoveries/discovery_<id>.json` with `IssueDiscoveryStore`.

The implementation handoff below assumes option 1 for the first slice and allows a sidecar fallback only if tests prove embedding is unsafe.

### New decision types

Add to `DecisionType`:

- `REPORT_ISSUE_DISCOVERY`
- `TRIAGE_ISSUE_DISCOVERY`

Role permissions:

- Dev: may emit `REPORT_ISSUE_DISCOVERY`.
- QA: may emit `REPORT_ISSUE_DISCOVERY`.
- PM: may emit `TRIAGE_ISSUE_DISCOVERY`.
- Neko supervisor: may emit `TRIAGE_ISSUE_DISCOVERY`, `REQUEST_HUMAN`, or `BLOCK` if intervention is required.

### `REPORT_ISSUE_DISCOVERY` payload

```json
{
  "title": "Short human-readable issue title",
  "summary": "What was found and why it matters",
  "evidence": ["bounded redaction-safe evidence strings or proof IDs"],
  "affected_paths": ["relative/path/or/module"],
  "severity": "low|medium|high|critical",
  "relationship_hint": "blocks_current|same_scope|fork_child|defer|escalate|unknown",
  "suggested_child_title": "Optional child mission title",
  "suggested_child_description": "Optional child mission description",
  "suggested_acceptance_criteria": ["Optional acceptance criteria for child mission"]
}
```

Validation:

- `title` and `summary` required, non-empty.
- `severity` must be one of `low`, `medium`, `high`, `critical`.
- `relationship_hint` must be one of the fixed classification terms plus `unknown`.
- `affected_paths`, `evidence`, and `suggested_acceptance_criteria` must be lists of strings.
- Payload must not include raw secrets or absolute local secret paths in snapshot summaries.

### `TRIAGE_ISSUE_DISCOVERY` payload

```json
{
  "discovery_id": "disc_abc123",
  "decision": "blocks_current|same_scope|fork_child|defer|escalate",
  "rationale": "Why this classification is correct",
  "child_title": "Required when decision=fork_child",
  "child_description": "Required when decision=fork_child",
  "child_acceptance_criteria": ["Required when decision=fork_child"],
  "priority": "low|medium|high|critical"
}
```

Validation:

- `discovery_id`, `decision`, and `rationale` required.
- Child fields required only for `fork_child`.
- `child_acceptance_criteria` must be non-empty for `fork_child`.
- `priority` defaults to discovery severity if omitted.

### Child mission creation contract

When PM triages `fork_child`, the harness creates a new `Task`:

- `parent_task_id`: parent mission id.
- `title`: `child_title`.
- `description`: `child_description`.
- `state`: `created` or `pm_ready_for_dev` depending on whether acceptance criteria were supplied.
- `acceptance_criteria`: `child_acceptance_criteria`.
- `non_goals`: include at least `"Do not modify parent mission scope unless PM re-triages."`.
- `risk_flags`: include `"forked_from_issue_discovery"` and severity/priority.
- `requested_by`: `"harness:issue_discovery:<discovery_id>"`.

The parent mission:

- keeps its current state for `fork_child` and `defer` unless an open incident already blocks it;
- moves to `blocked` only for `blocks_current` or `escalate` when an incident is opened;
- records `child_task_id` on the discovery.

---

## Stage breakdown

### Stage 12.1 — Discovery data model and schema validation

**Objective:** Add a structured way for Dev/QA to report unrelated issues without changing task scope.

**Files:**

- Modify: `agent_runtime/models.py`
- Modify: `agent_runtime/serde.py` only if needed by tests
- Modify: `agent_runtime/decision_schema.py`
- Modify: `agent_runtime/decision_contracts.py`
- Test: `tests/agent_runtime/test_models_serde.py`
- Test: `tests/agent_runtime/test_decision_schema.py`
- Test: `tests/agent_runtime/test_planning.py` or new `tests/agent_runtime/test_issue_discovery.py`

**Implementation tasks:**

1. Add `issue_discoveries: list[dict[str, Any]] = field(default_factory=list)` to `Task`.
2. Add `REPORT_ISSUE_DISCOVERY` to `DecisionType`.
3. Permit Dev and QA to emit `REPORT_ISSUE_DISCOVERY`.
4. Add payload validation to `decision_contracts.py`.
5. Add tests proving:
   - old task JSON without `issue_discoveries` still deserializes;
   - new task JSON round-trips with discovery records;
   - Dev/QA can emit discovery decisions;
   - PM cannot emit `REPORT_ISSUE_DISCOVERY` unless intentionally allowed later;
   - malformed discovery payloads are rejected.

**Acceptance criteria:**

- Dev/QA discovery decisions validate with strict payload shape.
- Existing mission records remain schema-v1-compatible.
- No child task is created in this stage.

---

### Stage 12.2 — Apply discovery decisions without scope mutation

**Objective:** Persist discovery records and events when Dev/QA report findings, while keeping the current mission moving by default.

**Files:**

- Create: `agent_runtime/scope_control.py`
- Modify: `agent_runtime/planning.py`
- Modify: `agent_runtime/events.py` only if helper changes are needed
- Test: `tests/agent_runtime/test_issue_discovery.py`
- Test: `tests/agent_runtime/test_state_machine.py`

**Implementation tasks:**

1. Implement `record_issue_discovery(task, decision, actor, run_id=None) -> dict`.
2. Generate stable ids like `disc_<8 hex>`.
3. Append a bounded dict record to `task.issue_discoveries`.
4. Emit event `issue.discovery_reported` with only safe summary fields:
   - `discovery_id`
   - `severity`
   - `relationship_hint`
   - `reported_by`
5. Add `apply_planning_decision()` branch for `REPORT_ISSUE_DISCOVERY`.
6. Ensure `task.state` does not change for `fork_child`, `same_scope`, `defer`, or `unknown` hints.
7. If `relationship_hint=blocks_current` or `severity=critical`, do **not** automatically block yet unless Stage 12.3 triage confirms. The only automatic action is recording and surfacing.

**Acceptance criteria:**

- A Dev tick can report discovery and close successfully.
- Parent mission state remains unchanged after discovery reporting.
- Discovery is visible in task JSON and event log.
- No incident opens until triage/escalation logic exists.

---

### Stage 12.3 — PM triage decision and deterministic child mission creation

**Objective:** Let PM classify discoveries and let the harness create child missions or incidents deterministically.

**Files:**

- Modify: `agent_runtime/decision_schema.py`
- Modify: `agent_runtime/decision_contracts.py`
- Modify: `agent_runtime/scope_control.py`
- Modify: `agent_runtime/planning.py`
- Modify: `agent_runtime/store.py` only if a helper is needed; prefer using existing `TaskStore.create()`
- Test: `tests/agent_runtime/test_scope_triage.py`
- Test: `tests/agent_runtime/test_ticker.py`

**Implementation tasks:**

1. Add `TRIAGE_ISSUE_DISCOVERY` to `DecisionType` and permit PM/Neko supervisor only.
2. Implement `apply_issue_triage(parent_task, decision, *, actor, task_store=None, incident_store=None)`.
3. For `fork_child`, create child task with parent link and provenance fields.
4. For `same_scope`, append a correction/audit note or risk flag but do not create child.
5. For `blocks_current`, set parent state to `blocked` and open an incident kind `scope_blocker`.
6. For `escalate`, open an incident kind `scope_intervention` and leave/mark parent blocked depending on severity.
7. For `defer`, mark discovery deferred and leave parent state unchanged.
8. Emit events:
   - `issue.discovery_triaged`
   - `issue.child_mission_created` when applicable
   - `incident.opened` via `IncidentStore` for blocker/escalation
9. Ensure duplicate triage of the same discovery is idempotent: either reject with `DecisionPayloadInvalid` or return existing `child_task_id` without creating another child.

**Acceptance criteria:**

- `fork_child` creates exactly one child task with `parent_task_id` set.
- Parent mission keeps moving for `fork_child` and `defer`.
- Parent mission blocks and opens an incident for `blocks_current`.
- Duplicate PM triage cannot create duplicate child missions.

---

### Stage 12.4 — State machine and prompt policy integration

**Objective:** Make the agent loop ask PM to triage untriaged discoveries without letting Dev self-assign extra work.

**Files:**

- Modify: `agent_runtime/state_machine.py`
- Modify: `agent_runtime/context_builder.py`
- Modify: persona prompt files under the harness prompt/profile area discovered during implementation
- Test: `tests/agent_runtime/test_state_machine.py`
- Test: `tests/agent_runtime/test_context_builder.py`
- Test: `tests/agent_runtime/test_persona_prompts.py`

**Implementation tasks:**

1. Add helper `has_untriaged_issue_discovery(task)`.
2. Decide default scheduling behavior:
   - If task has untriaged discovery with `relationship_hint in {blocks_current, escalate}` or `severity in {high, critical}`, route next action to PM triage.
   - If hint is `fork_child`, `defer`, `same_scope`, or `unknown`, let current state continue but surface in context/snapshot.
3. Render current mission non-goals and untriaged discovery handles in `context_builder.render_context()`.
4. Update Dev prompt policy:
   - report unrelated issues with `REPORT_ISSUE_DISCOVERY`;
   - do not implement unrelated fixes inline;
   - continue original mission unless blocked by acceptance criteria.
5. Update PM prompt policy:
   - classify reported discoveries using the fixed terms;
   - create child missions through `TRIAGE_ISSUE_DISCOVERY`, not by editing parent scope freeform.
6. Update QA prompt policy:
   - QA may report discoveries but cannot approve child mission proof as parent proof.

**Acceptance criteria:**

- Severe/blocking discoveries route PM before another Dev action.
- Non-blocking discoveries do not starve current mission.
- Persona context gives enough information to triage without raw unsafe content.

---

### Stage 12.5 — Snapshot, observability, and Launcher contract

**Objective:** Make Mission Control show discovery/triage state and child mission lineage without leaking sensitive detail.

**Files:**

- Modify: `agent_runtime/snapshot.py`
- Modify: `agent_runtime/observability.py`
- Modify: `docs/agent-runtime-harness/07-launcher-unreal-observability.md` if Launcher contract fields change
- Test: `tests/agent_runtime/test_snapshot.py`
- Test: `tests/agent_runtime/test_observability.py`
- Test: `tests/agent_runtime/test_snapshot_redaction.py`

**Snapshot additions:**

Task summary should add redaction-safe fields:

```json
{
  "parent_task_id": "task_parent_or_null",
  "child_task_count": 1,
  "issue_discovery_counts": {
    "untriaged": 1,
    "forked": 1,
    "deferred": 0,
    "escalated": 0
  },
  "untriaged_issue_severities": ["high"]
}
```

Observability additions:

- signal: `untriaged_issue_discoveries`
- intervention kind: `issue_discovery_triage_needed`
- intervention severity mapping:
  - critical/high discovery -> high
  - medium/low discovery -> medium

**Acceptance criteria:**

- Launcher/Mission Control can show “needs PM triage” and parent/child relationship.
- Snapshot does not expose raw evidence strings, absolute paths, model output, or secrets.
- Redaction tests cover discovery evidence and affected paths.

---

### Stage 12.6 — CLI/operator controls

**Objective:** Give Tony/Neko/PM an explicit CLI path to inspect and triage discoveries without editing JSON by hand.

**Files:**

- Modify: `hermes_cli/harness.py`
- Modify: `agent_runtime/cli_format.py`
- Test: CLI tests under `tests/agent_runtime/` or `tests/cli/` depending on existing patterns

**Commands:**

```bash
hermes harness issue list --task-id task_123 --json
hermes harness issue show disc_123 --json
hermes harness issue triage disc_123 \
  --decision fork_child \
  --child-title "Fix unrelated import crash" \
  --child-description "..." \
  --acceptance "Focused test passes" \
  --json
```

Keep `issue triage` deterministic: it should call the same `scope_control.apply_issue_triage()` helper used by PM persona decisions.

**Acceptance criteria:**

- CLI list/show/triage works on a temporary runtime root.
- CLI triage creates the same child mission shape as PM triage.
- Human-readable output is terse; `--json` is complete and redaction-safe.

---

### Stage 12.7 — End-to-end tick smoke and proof separation

**Objective:** Prove the full flow with fake persona runtime before live LLM smoke.

**Files:**

- Modify/add tests: `tests/agent_runtime/test_ticker.py`
- Modify/add tests: `tests/agent_runtime/test_proof_gates.py`
- Modify/add docs if live smoke notes belong in Stage 10/11 docs

**Test scenario:**

1. Create parent mission with acceptance criteria.
2. Fake Dev emits `REPORT_ISSUE_DISCOVERY` with `relationship_hint=fork_child`.
3. Parent remains open and not blocked.
4. Fake PM emits `TRIAGE_ISSUE_DISCOVERY` with `decision=fork_child`.
5. Harness creates child mission exactly once.
6. Fake Dev/QA work on child mission separately.
7. Parent proof gate does not count child proof unless PM explicitly links it in a later supported contract.

**Acceptance criteria:**

- Full fake runtime test passes deterministically.
- `git diff --check` passes.
- Focused test command passes:

```bash
bash scripts/run_tests.sh tests/agent_runtime/test_issue_discovery.py tests/agent_runtime/test_scope_triage.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_snapshot.py tests/agent_runtime/test_observability.py
```

---

## Risks and interventions

- **Schema compatibility risk:** If adding `Task.issue_discoveries` breaks older JSON reads, stop and switch to sidecar `IssueDiscoveryStore` rather than bumping schema blindly.
- **Prompt compliance risk:** Dev may still try to fix unrelated work inline. Mitigate with decision schema, prompt text, and QA findings for unexpected file changes outside stage affected paths.
- **Duplicate child mission risk:** PM retry/tick replay can create duplicates unless triage is idempotent. This is a high-severity AAA gap; tests must prove exactly-once behavior.
- **Snapshot leakage risk:** Discovery evidence may include paths/secrets. Snapshot must expose counts, ids, severity, and status only.
- **Workflow complexity risk:** Do not build a mini issue tracker. Keep this as mission lineage + triage only.

---

## No-guesswork implementation order

1. Add model field + decision enum + validation tests.
2. Add discovery recording helper and planning branch.
3. Add PM triage decision + deterministic child creation helper.
4. Add state-machine scheduling rules for severe/untriaged discoveries.
5. Add prompt/context instructions.
6. Add snapshot/observability redaction-safe fields.
7. Add CLI issue list/show/triage.
8. Add full fake-runtime E2E test.
9. Run focused tests and `git diff --check`.
10. Only after fake runtime is green, run a live temporary-root smoke with profile-bound PM/Dev/QA.

---

## Out of scope for this stage

- Kanban card creation or board sync.
- Automatic GitHub issue creation.
- Launcher write-actions beyond displaying triage state and invoking existing CLI/action bridge later.
- Letting Dev agents create child missions directly.
- Treating child mission proof as parent proof by default.
