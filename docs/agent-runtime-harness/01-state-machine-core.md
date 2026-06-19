# Stage 1 — State Machine Core

## Goal

Create the minimal reliable Python core for tasks, agents, states, events, and proof references. This stage deliberately avoids model calls. It must be deterministic, fully unit-testable, and ship before any persona / ticker / CLI work.

## Codebase anchors (verified)

| Concern | Existing file/symbol | What we keep / avoid |
|---|---|---|
| Profile-aware paths | `hermes_constants.py::get_hermes_home`, `display_hermes_home`, `set_hermes_home_override` | Reuse for `<hermes-root>/agent-runtime/`. Never hardcode `~/.hermes`. |
| Atomic JSON writes | `utils.py::atomic_json_write`, `utils.py::atomic_replace` | Reuse verbatim for `tasks/*.json` / `runs/*.json`. Symlink-safe — needed for managed deployments. |
| Wall-clock / TZ | `hermes_time.py::now` (UTC, TZ-aware) | All timestamps must go through this — direct `datetime.utcnow()` is forbidden. |
| Concurrency lessons (not schema) | `hermes_cli/kanban_db.py` — WAL + `BEGIN IMMEDIATE` + CAS on `tasks.status` & `tasks.claim_lock`, `DEFAULT_CLAIM_TTL_SECONDS = 15*60`, `HERMES_KANBAN_CLAIM_TTL_SECONDS` override | Apply the CAS-on-claim mental model. Do **not** import its schema. |
| Profile root vs shared root | `kanban_db.py` resolves boards under the *shared* `<root>/kanban/...` so profile workers converge on one DB | Mirror the rule: harness store lives at `<get_default_hermes_root()>/agent-runtime/`, not `<get_hermes_home()>/agent-runtime/`, so PM/dev/QA personas running under different profiles share one truth. |
| File locking, cross-platform | `cron/scheduler.py` lines 1799–1814 (`fcntl` on POSIX, `msvcrt` on Windows) | Lift the pattern into a tiny `agent_runtime.locks.tick_lock()` context manager. |
| Test isolation | `tests/conftest.py` redirects `HERMES_HOME` per test; `_CREDENTIAL_NAMES` strips API keys | Stage 1 tests must run hermetic under the wrapper without any provider env. |

`hermes_cli/kanban.py::build_parser` and the `kanban_command` dispatcher remain reference reading for *how to register a subcommand cleanly*, but the new package owns its own schema.

## New package layout

```text
agent_runtime/
  __init__.py                # version, public exports
  paths.py                   # store_root(), task_path(), proof_root(), events_path()
  models.py                  # Task, TaskStage, AgentPersona, AgentRun, Proof, Event, Incident
  states.py                  # TaskState, AgentState, RunState, StageStatus enums
  transitions.py             # TRANSITION_TABLE, apply_transition(), InvalidTransition
  events.py                  # EventType, append_event(), iter_events()
  proof_rules.py             # ProofType, ProofRequirement (Stage 4 owns the gates)
  store.py                   # TaskStore, AgentStore, RunStore, ProofStore
  locks.py                   # tick_lock() ctx mgr, atomic_task_update()
  errors.py                  # InvalidTransition, ProofMissing, StaleRun, StoreCorrupt
  serde.py                   # to_jsonable() / from_jsonable() for dataclass <-> JSON
```

Test layout (must mirror `tests/` convention, run under `scripts/run_tests.sh`):

```text
tests/agent_runtime/
  __init__.py
  conftest.py                # tmp HERMES_HOME, frozen clock fixture
  test_paths.py
  test_models_serde.py
  test_states.py
  test_transitions.py
  test_store.py
  test_events.py
  test_locks.py
  test_proof_rules.py        # Stage 4 expands this — Stage 1 only ships the enum + container
```

## Core enums (final)

`states.py`:

```python
from enum import StrEnum


class TaskState(StrEnum):
    CREATED = "created"
    PM_TRIAGE = "pm_triage"
    PM_READY_FOR_DEV = "pm_ready_for_dev"
    DEV_AUDIT = "dev_audit"
    DEV_STAGE_PLANNING = "dev_stage_planning"
    DEV_TEST_DESIGN = "dev_test_design"
    QA_REVIEW_PLAN = "qa_review_plan"
    DEV_IMPLEMENTING = "dev_implementing"
    DEV_READY_FOR_QA = "dev_ready_for_qa"
    QA_TESTING = "qa_testing"
    QA_NEEDS_FIXES = "qa_needs_fixes"
    QA_APPROVED = "qa_approved"
    PM_PROOF_REVIEW = "pm_proof_review"
    PM_READY_FOR_INTEGRATION = "pm_ready_for_integration"
    INTEGRATING = "integrating"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentState(StrEnum):
    IDLE = "idle"
    ASSIGNED = "assigned"
    READING_CONTEXT = "reading_context"
    AUDITING = "auditing"
    PLANNING = "planning"
    DESIGNING_TESTS = "designing_tests"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    TESTING = "testing"
    CAPTURING_PROOF = "capturing_proof"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_FIXES = "waiting_for_fixes"
    BLOCKED = "blocked"
    CRASHED = "crashed"
    COMPLETE = "complete"


class RunState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_ON_TOOL = "waiting_on_tool"
    WAITING_ON_APPROVAL = "waiting_on_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


class StageStatus(StrEnum):
    DRAFT = "draft"
    AUDITED = "audited"
    READY = "ready"
    IMPLEMENTING = "implementing"
    READY_FOR_QA = "ready_for_qa"
    PASSED = "passed"
    NEEDS_FIXES = "needs_fixes"
    BLOCKED = "blocked"
```

## Data models

`models.py` — plain dataclasses. No Pydantic / ORM / SQLAlchemy in v0. Reason: we can serialize with the existing `atomic_json_write` helper; adding Pydantic just to validate dataclasses we own is ceremony.

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Task:
    id: str
    title: str
    description: str
    state: TaskState
    created_at: datetime
    updated_at: datetime
    requested_by: str                    # "tony" / launcher user id / cron job id
    requires_visual_proof: bool = False  # PM may flip during triage
    acceptance_criteria: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    affected_repos: list[str] = field(default_factory=list)
    suggested_roles: list[str] = field(default_factory=list)
    stages: list["TaskStage"] = field(default_factory=list)
    current_stage_id: str | None = None
    assigned_persona_ids: dict[str, str] = field(default_factory=dict)
    proof_ids: list[str] = field(default_factory=list)
    open_incident_ids: list[str] = field(default_factory=list)
    waiver: dict[str, str] | None = None
    parent_task_id: str | None = None
    schema_version: int = 1


@dataclass(slots=True)
class TaskStage:
    id: str
    title: str
    objective: str
    status: StageStatus
    affected_paths: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)
    audit_notes: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    requires_visual_proof: bool | None = None  # None = inherit from Task
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class AgentPersona:
    id: str                  # "pm", "dev", "qa", "alice_supervisor"
    display_name: str
    role: str                # AgentRole literal — Stage 2 introduces enum
    model: str | None        # None => fall back to default_model config
    provider: str | None
    api_mode: str | None     # passes through to agent_init.py
    toolsets: list[str]      # constrained list — see Stage 2
    system_prompt_path: str  # relative to <root>/agent-runtime/personas/<id>/system.md
    autonomy: str = "review"
    schema_version: int = 1


@dataclass(slots=True)
class AgentRun:
    id: str
    persona_id: str
    task_id: str
    stage_id: str | None
    state: RunState
    started_at: datetime
    last_heartbeat_at: datetime
    finished_at: datetime | None = None
    iteration_budget: int = 90
    iterations_used: int = 0
    cost_usd: float = 0.0
    session_id: str | None = None         # threads to hermes_state.SessionDB
    final_decision: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    schema_version: int = 1


@dataclass(slots=True)
class Proof:
    id: str
    task_id: str
    stage_id: str | None
    type: "ProofType"                     # defined in proof_rules.py
    title: str
    path_or_value: str                    # relative path under <root>/agent-runtime/proofs/<task_id>/...
    created_by: str                       # persona_id or "harness"
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    redaction_status: str = "needs_scan"  # "safe" | "needs_scan" | "unsafe"
    schema_version: int = 1


@dataclass(slots=True)
class Event:
    ts: datetime
    type: str                             # "task.transition" / "run.heartbeat" / ...
    task_id: str | None
    run_id: str | None
    persona_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Incident:
    id: str
    task_id: str | None
    run_id: str | None
    kind: str                             # "model_invalid_output" / "stale_run" / "tool_failure" / ...
    summary: str
    detail_path: str | None               # large blobs land in incidents/<id>.txt
    opened_at: datetime
    closed_at: datetime | None = None
    schema_version: int = 1
```

## Local storage

`paths.py`:

```python
from hermes_constants import get_default_hermes_root, get_hermes_home_override


def store_root() -> Path:
    # Cross-profile coordination, mirroring kanban_db.py's shared-root rule.
    override = os.getenv("HERMES_AGENT_RUNTIME_ROOT")
    if override:
        return Path(override)
    return get_default_hermes_root() / "agent-runtime"


def tasks_dir() -> Path:    return store_root() / "tasks"
def runs_dir() -> Path:     return store_root() / "runs"
def agents_dir() -> Path:   return store_root() / "agents"
def proofs_dir() -> Path:   return store_root() / "proofs"
def incidents_dir() -> Path: return store_root() / "incidents"
def events_path() -> Path:  return store_root() / "events.jsonl"
def lock_dir() -> Path:     return store_root() / "locks"
def snapshot_path() -> Path: return store_root() / "snapshot.json"   # Stage 7 fills this
```

On-disk shape:

```text
<root>/agent-runtime/
  tasks/<task_id>.json
  agents/<persona_id>.json
  runs/<run_id>.json
  proofs/<task_id>/
    screenshots/
    videos/
    logs/
    test-runs/
    proof_<id>.json          # one file per Proof record, points at large artifact
  incidents/<incident_id>.json
  incidents/<incident_id>.txt  # large detail blob, redaction_status applies
  events.jsonl
  locks/
    tick.lock
    task_<task_id>.lock
  snapshot.json              # Launcher/Unreal observability output (Stage 7)
```

Rationale for JSON-on-disk instead of SQLite at this stage:

1. The harness state is **small** (10s–100s of tasks) and **append-mostly**. JSON files round-trip through `atomic_json_write` cleanly; SQLite buys atomicity we already get from atomic-replace.
2. Tasks are the natural shard. Reads almost never need cross-task joins.
3. We want diff-friendly state — `git diff` on `tasks/*.json` is the cheapest debugging tool we can ship.
4. Stage 8 documents the SQLite migration trigger: if any one of (task count > 5000) / (events.jsonl > 100MB) / (parallel ticks become required) hits, lift to a SQLite store using the kanban schema as a template, **not** before.

## Transition design

`transitions.py`:

```python
from typing import Iterable
from .errors import InvalidTransition
from .states import TaskState as T

# Adjacency table. Reading: from-state → set of allowed to-states.
TRANSITION_TABLE: dict[T, frozenset[T]] = {
    T.CREATED: frozenset({T.PM_TRIAGE, T.CANCELLED}),
    T.PM_TRIAGE: frozenset({T.PM_READY_FOR_DEV, T.BLOCKED, T.CANCELLED}),
    T.PM_READY_FOR_DEV: frozenset({T.DEV_AUDIT, T.BLOCKED, T.CANCELLED}),
    T.DEV_AUDIT: frozenset({T.DEV_STAGE_PLANNING, T.BLOCKED}),
    T.DEV_STAGE_PLANNING: frozenset({T.DEV_TEST_DESIGN, T.DEV_AUDIT, T.BLOCKED}),
    T.DEV_TEST_DESIGN: frozenset({T.QA_REVIEW_PLAN, T.DEV_STAGE_PLANNING, T.BLOCKED}),
    T.QA_REVIEW_PLAN: frozenset({T.DEV_IMPLEMENTING, T.DEV_STAGE_PLANNING, T.BLOCKED}),
    T.DEV_IMPLEMENTING: frozenset({T.DEV_READY_FOR_QA, T.BLOCKED, T.FAILED}),
    T.DEV_READY_FOR_QA: frozenset({T.QA_TESTING, T.DEV_IMPLEMENTING, T.BLOCKED}),
    T.QA_TESTING: frozenset({T.QA_APPROVED, T.QA_NEEDS_FIXES, T.BLOCKED, T.FAILED}),
    T.QA_NEEDS_FIXES: frozenset({T.DEV_IMPLEMENTING, T.DEV_STAGE_PLANNING, T.BLOCKED, T.CANCELLED}),
    T.QA_APPROVED: frozenset({T.PM_PROOF_REVIEW, T.BLOCKED}),
    T.PM_PROOF_REVIEW: frozenset({T.PM_READY_FOR_INTEGRATION, T.QA_NEEDS_FIXES, T.BLOCKED}),
    T.PM_READY_FOR_INTEGRATION: frozenset({T.INTEGRATING, T.BLOCKED}),
    T.INTEGRATING: frozenset({T.DONE, T.FAILED, T.BLOCKED}),
    T.DONE: frozenset(),
    T.FAILED: frozenset({T.PM_TRIAGE}),       # only PM can reopen
    T.CANCELLED: frozenset(),
    T.BLOCKED: frozenset(set(T) - {T.DONE}),  # unblock to any prior workflow state
}


def apply_transition(task, to_state, *, actor: str, reason: str = "") -> None:
    """In-memory transition. Caller owns persistence + event append."""
    if to_state not in TRANSITION_TABLE[task.state]:
        raise InvalidTransition(
            f"{task.id}: cannot transition {task.state} -> {to_state} "
            f"(actor={actor}, reason={reason!r})"
        )
    task.state = to_state
    task.updated_at = _now_utc()
```

Stage 1 test matrix:

- `created -> pm_triage` allowed; `created -> dev_implementing` rejected.
- `pm_ready_for_dev -> dev_audit` allowed; `pm_ready_for_dev -> dev_implementing` rejected.
- `qa_needs_fixes -> dev_implementing` allowed (no other path).
- `qa_approved -> done` rejected (must go through PM proof review + integration).
- `done` is terminal; `cancelled` is terminal.
- `failed -> pm_triage` is the only resurrection path; `failed -> dev_implementing` rejected.
- `blocked -> *` (any prior workflow state) allowed; `blocked -> done` rejected.

## Store API

`store.py`:

```python
class TaskStore:
    def create(self, task: Task) -> Task: ...
    def get(self, task_id: str) -> Task: ...
    def update(self, task: Task) -> None:
        """Atomic-write under per-task lock. Caller is expected to have
        called apply_transition() before invoking this if state changed."""
    def list_open(self) -> list[Task]: ...
    def list_by_state(self, *states: TaskState) -> list[Task]: ...
    def list_all(self) -> list[Task]: ...

class RunStore:
    def open_run(self, persona_id, task_id, stage_id=None, *, iteration_budget=90) -> AgentRun: ...
    def heartbeat(self, run_id: str) -> None: ...
    def close_run(self, run_id: str, *, state: RunState, final_decision=None, error=None) -> AgentRun: ...
    def find_stale(self, *, heartbeat_ttl_seconds: int) -> list[AgentRun]: ...
    def list_for_task(self, task_id: str) -> list[AgentRun]: ...

class ProofStore:
    def attach(self, proof: Proof) -> Proof: ...
    def get(self, proof_id: str) -> Proof: ...
    def list_for_task(self, task_id: str) -> list[Proof]: ...
    def list_for_stage(self, stage_id: str) -> list[Proof]: ...

class EventLog:
    def append(self, evt: Event) -> None: ...
    def tail(self, n: int) -> list[Event]: ...
    def iter_since(self, ts: datetime): ...  # streaming reader for Stage 7
```

Mutation hygiene:

- All `update()` paths take the per-task lock (`locks/task_<id>.lock`), write to a temp file, and call `atomic_replace`.
- Every successful `update()` appends one `Event` via `EventLog.append()`. Failure to append the event aborts the lock transaction so state-without-event is impossible.
- `EventLog.append()` uses `open(events_path(), "a")` with a short fcntl/msvcrt lock for the write — append is naturally atomic for ≤PIPE_BUF on POSIX, but the explicit lock keeps Windows + line-ending consistent.

## Event log requirements

`events.jsonl` contains one JSON object per line. Schema (must stay strict; Launcher/Unreal in Stage 7 parse it):

```json
{"ts":"2026-05-20T22:15:01.412Z","type":"task.transition","task_id":"task_abc","run_id":null,"persona_id":null,"payload":{"from":"created","to":"pm_triage","actor":"harness","reason":"created"}}
```

Allowed `type` values (Stage 1 set — later stages extend):

- `task.created`, `task.transition`, `task.cancelled`, `task.blocked`, `task.unblocked`
- `task.stage_added`, `task.stage_updated`, `task.stage_corrected`
- `run.opened`, `run.heartbeat`, `run.closed`
- `proof.attached`, `proof.scanned`
- `incident.opened`, `incident.closed`

Redaction rules:

- No raw model prompts in payload.
- No tool stdout/stderr in payload — link to `proofs/<task_id>/logs/<run_id>.log`.
- No API keys, tokens, file contents.
- The `payload` size budget is 4 KB; oversize payloads are rejected at append time and the caller gets an `EventPayloadTooLarge` error.

## Acceptance criteria

1. `scripts/run_tests.sh tests/agent_runtime/ -q` passes under the hermetic wrapper.
2. `TaskStore.create()` + `apply_transition()` + `update()` round-trips a task through CREATED → PM_TRIAGE → PM_READY_FOR_DEV and the `events.jsonl` shows exactly three transition events.
3. Invalid transitions raise `InvalidTransition` and leave the on-disk task JSON byte-identical to its pre-call form.
4. Concurrent `update()` calls against the same task ID from two threads serialize via the per-task lock; no partial JSON ever lands on disk.
5. Stage 1 ships **zero** model calls, zero network, zero CLI surface — pure library.
6. `tests/agent_runtime/conftest.py` reuses the `_isolate_hermes_home` pattern; nothing escapes the tempdir.

## Risks / interventions

- **Atomic write on Windows**: `os.replace` is documented atomic on NTFS but the file must be on the same volume. `paths.store_root()` rooting under `get_default_hermes_root()` keeps temp + target on the same drive. Tests must exercise this on the Windows runner before Stage 2 lands.
- **Schema drift**: `schema_version` is on every dataclass. A `serde.upgrade(record)` shim runs on every read. Stage 1 only ships v1, but the shim hook is mandatory now — adding it later means writing a one-shot migrator for every shipped task.
- **JSON store ceiling**: if `tasks/` ever crosses ~5000 entries or `events.jsonl` crosses 100 MB, **stop** and migrate to SQLite (Stage 8 has the playbook). Do not bolt indices onto the JSON store.
- **Don't reuse the Kanban transition table mindset**: `ready/running/blocked/review/done` are flat. The harness states are workflow-positional. Mixing them is the failure mode this stage exists to prevent.
- **Time source**: never call `datetime.utcnow()`. `hermes_time.now()` is mocked in CI's `tests/conftest.py` so frozen-clock tests work. New callers that bypass it will fail flake-checks.
