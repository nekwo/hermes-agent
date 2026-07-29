from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .plan_review import PlanReview
from .proof_rules import ProofType
from .states import PossessionState, RunState, StageStatus, TaskState, WorkerSessionState


# The structural prefix every persona-instance id carries. Defined at this low
# layer (no import back into persona_assignments, which would be a cycle) so the
# PersonaInstance backfill can recognize instance-shaped tokens. The id authority
# in ``persona_assignments`` re-exports this constant + predicate; do not fork a
# second copy.
PERSONA_INSTANCE_ID_PREFIX = "personainst_"


def looks_like_persona_instance_id(token: object) -> bool:
    """True when ``token`` is structurally a persona-instance id (``personainst_*``).

    A steering-parent SET (``steered_by``) and its denormalized mirror
    (``spawned_by``) may hold ONLY these. A non-instance principal — the
    operator, a bare persona/role token, any provenance string — is never a
    steering parent, so it must never be mirrored into a steering field from
    provenance. Read projections filter on this predicate so a legacy row that
    already carries such a value renders as an accounted anomaly, never as a
    phantom "steered by <principal>" edge.
    """
    return isinstance(token, str) and token.strip().startswith(PERSONA_INSTANCE_ID_PREFIX)


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
    owner_slot: str | None = None
    status: StageStatus = StageStatus.READY
    proof_recipe_id: str | None = None
    proof_gate: dict[str, Any] = field(default_factory=dict)
    output_type: str | None = None
    requires_product_edit: bool = False
    requires_visual_proof: bool = False
    depends_on: list[str] = field(default_factory=list)
    blocks_qa_until: bool = True
    proof_ids: list[str] = field(default_factory=list)
    packet_ids: list[str] = field(default_factory=list)
    blocker_ids: list[str] = field(default_factory=list)
    affected_paths: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)
    audit_notes: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
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
    blueprint_id: str | None = None
    blueprint_version: int | None = None
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)
    binding_sources: dict[str, str] = field(default_factory=dict)
    edges: list[dict[str, str]] = field(default_factory=list)
    agent_topology: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    stage_attempts: dict[str, int] = field(default_factory=dict)
    on_unhandled: str = "intervention"


@dataclass(slots=True)
class Task:
    id: str
    title: str
    description: str
    state: TaskState
    created_at: datetime
    updated_at: datetime
    requested_by: str
    requires_visual_proof: bool = False
    # Declared delivery directive (promote / preserve_diff / worktree). None
    # means the contract default; resolved via delivery_directive.task_delivery_directive.
    delivery_directive: dict[str, Any] | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    proof_expectations: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    affected_repos: list[str] = field(default_factory=list)
    suggested_roles: list[str] = field(default_factory=list)
    stages: list[TaskStage] = field(default_factory=list)
    current_stage_id: str | None = None
    assigned_persona_ids: dict[str, str] = field(default_factory=dict)
    proof_ids: list[str] = field(default_factory=list)
    open_incident_ids: list[str] = field(default_factory=list)
    waiver: dict[str, str] | None = None
    parent_task_id: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    # Neko/supervisor may narrow a goal into the current routing slice. Keep
    # that scope separate so operator-authored goal fields remain stable.
    routing_scope: dict[str, Any] = field(default_factory=dict)
    operator_notes: list[str] = field(default_factory=list)
    harness_self_heal: dict[str, Any] = field(default_factory=dict)
    context_requests: list[dict[str, Any]] = field(default_factory=list)
    issue_discoveries: list[dict[str, Any]] = field(default_factory=list)
    plan_review: PlanReview | None = None
    mission_plan: MissionPlan | None = None
    planning_locked: bool = False
    goal_id: str | None = None
    workspace_id: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.goal_id:
            self.goal_id = self.id


# Deprecated compatibility alias for one release while persisted files and
# legacy imports still use Task as the storage record name.
Goal = Task


@dataclass(slots=True)
class Workspace:
    id: str
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime
    agent_ids: list[str] = field(default_factory=list)
    default_blueprint_id: str | None = None
    isolation: str = "soft"
    max_concurrent_lanes: int | None = None
    realm_id: str | None = None
    archived: bool = False
    schema_version: int = 1


@dataclass(slots=True)
class Realm:
    id: str
    slug: str
    name: str
    created_at: datetime
    updated_at: datetime
    server_id: str | None = None
    # Stable identity minted by the membership backend. This is a pointer,
    # never a copy of a member's local/stale default workspace contents.
    default_workspace_id: str | None = None
    default_workspace_name: str = "Default"
    default_workspace_version: int = 0
    workspace_ids: list[str] = field(default_factory=list)
    # Resurrection-guard ledger (ids only, bounded): workspaces DELETED from
    # this realm. Travels inside the realm JSON through realm sync so a member
    # that still holds a local copy neither republishes it nor re-adopts it on
    # pull (the Board.archived_card_ids / OfficeSurface.archived_actor_keys
    # idiom, lifted to workspace granularity).
    deleted_workspace_ids: list[str] = field(default_factory=list)
    # Which shared skills publish to this realm. Mode "all" (default,
    # back-compat) publishes every skill in the shared catalog including
    # future ones; "selected" publishes exactly skill_selection (empty list =
    # publish none). Travels realm-wide via realm sync (NOT an authority
    # field) — the selection is realm truth, converged last-publisher-wins.
    # Kept sorted + deduped at every write chokepoint
    # (RealmStore.set_skill_selection); slugs unknown to a member's local
    # catalog are preserved, never stripped, on an unrelated save.
    skill_publish_mode: str = "all"  # "all" | "selected"
    skill_selection: list[str] = field(default_factory=list)
    # Which persona definitions publish to this realm. ``workspace`` preserves
    # the pre-selection behavior: every persona required by a workspace roster
    # or Office placement travels. ``selected`` adds the explicit
    # ``agent_selection`` set while required references remain pinned so a
    # pulled workspace/office can never point at an absent persona definition.
    # The explicit list is preserved when switching back to workspace mode.
    agent_publish_mode: str = "workspace"  # "workspace" | "selected"
    agent_selection: list[str] = field(default_factory=list)
    sync_manifest_ref: str | None = None
    archived: bool = False
    schema_version: int = 1


@dataclass(slots=True)
class BoardColumn:
    """A value object living inside ``board.json`` (never its own file).

    Default columns use FIXED ids + deterministic content so two machines
    lazily creating the same default board converge on identical semantic
    content instead of conflicting on first realm sync. Behavior binds to
    ``kind`` (queued/active/review/done/custom), never to ``title``.
    """

    column_id: str
    title: str
    kind: str = "custom"
    wip_limit: int | None = None  # soft — surfaces a warning, never blocks


@dataclass(slots=True)
class BoardCard:
    """One planning card — one file each under ``boards/<board_id>/cards/``.

    A card is a PLANNING artifact only. ``created_by`` attribution is first-class
    so operator- and agent-authored cards render distinctly.
    """

    card_id: str
    board_id: str
    column_id: str
    title: str
    order_key: str
    description: str = ""
    priority: str = "p2"  # "p0".."p3"
    labels: list[str] = field(default_factory=list)
    assignee: str | None = None  # persona_id or "operator"
    checklist: list[dict[str, Any]] = field(default_factory=list)  # [{text, done}]
    state: str = "active"  # "active" | "archived"
    created_by: str = "operator"  # "operator" | persona_id
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class Board:
    """A workspace-scoped kanban board (def + ordered columns + card ledger).

    The default board id is deterministic (``board_default_<workspace_id>``) so
    two machines converge on it. ``archived_card_ids`` is the resurrection-guard
    ledger (ids only, bounded) that blocks a pulled remote copy from re-creating
    a locally archived card.
    """

    board_id: str
    workspace_id: str
    title: str
    columns: list[BoardColumn] = field(default_factory=list)
    archived_card_ids: list[str] = field(default_factory=list)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class OfficeItem:
    """One authored Mission Office scene item (agent character or its desk) —
    a value object living inside its actor's file, never its own file.

    Geometry is scene-space ``[x, y]``; ``scale`` is the operator-authored
    render scale, clamped to the launcher's authorable range at the store
    boundary. ``display_name`` is operator text and is validated against the
    secret-assignment scanner at WRITE time (plan §4.2) so one member's name
    can never fail another member's realm publish.
    """

    item_id: str
    persona_id: str
    kind: str = "agent"  # "agent" | "desk"
    position: list[float] = field(default_factory=lambda: [0.0, 0.0])
    folder: str = ""
    display_name: str | None = None
    pet_slug: str | None = None
    scale: float = 1.0


@dataclass(slots=True)
class OfficeActor:
    """One Mission Office actor placement — one file each under
    ``office/<workspace>/actors/`` (the realm-sync merge unit).

    ``actor_key`` is the canonical sync key minted ONLY by ``OfficeStore``
    (``canonical_persona_instance_id`` for instance-bound actors, else the
    persona id). The identity triple (persona/instance/profile) is the
    payload truth — the filename is routing only. All scene items bound to
    one actor (agent placements + coupled desks) live in this one file, so
    actor granularity — not item granularity — is the merge unit.
    """

    actor_key: str
    workspace_id: str
    persona_id: str
    persona_instance_id: str | None = None
    backing_profile: str | None = None
    items: list[OfficeItem] = field(default_factory=list)
    state: str = "active"  # "active" | "archived"
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class OfficeSurface:
    """The per-workspace Mission Office surface definition — shared taxonomy
    only (folders) + the resurrection-guard ledger. Personal view state
    (viewport, collapsed docks, hidden ids) never enters this model — it stays
    launcher-local by design (plan §4.4).
    """

    workspace_id: str
    folders: list[str] = field(default_factory=list)
    archived_actor_keys: list[str] = field(default_factory=list)
    revision: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = "operator"
    schema_version: int = 1


@dataclass(slots=True)
class GoalRuntimeInstance:
    id: str
    task_id: str
    lane: str
    state: str
    created_at: datetime
    updated_at: datetime
    started_by: str
    run_generation: int = 1
    active_run_ids: list[str] = field(default_factory=list)
    parked_reason: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lane_kind: str = "production"
    priority: int = 5
    state_reason: str | None = None
    current_stage_id: str | None = None
    current_owner: str | None = None
    persona_instance_ids: list[str] = field(default_factory=list)
    repo_bundle_locks: list[dict[str, Any]] = field(default_factory=list)
    daemon_lease_id: str | None = None
    budget_counters: dict[str, int] = field(default_factory=dict)
    last_decision_type: str | None = None
    last_progress_at: datetime | None = None
    open_incident_ids: list[str] = field(default_factory=list)
    latest_proof_ids: list[str] = field(default_factory=list)
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
    requires_visual_proof: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class AgentPersona:
    id: str
    display_name: str
    role: str
    model: str | None
    provider: str | None
    api_mode: str | None
    toolsets: list[str]
    system_prompt_path: str
    autonomy: str = "review"
    hermes_profile: str | None = None
    skills: list[str] = field(default_factory=list)
    soul_overlay_path: str | None = None
    required_mcp_servers: list[str] = field(default_factory=list)
    include_profile_memory: bool = False
    include_core_context_files: bool = False
    repo_scope: str | None = None
    repo_scope_label: str | None = None
    iteration_budget: int | None = None
    max_wall_seconds: float | None = None
    max_api_calls: int | None = None
    max_total_tokens: int | None = None
    readiness: dict[str, Any] = field(default_factory=dict)
    # issued_at of the last applied model-default write; stale writes are
    # superseded (same guard as PersonaInstance.model_override_issued_at).
    model_override_issued_at: datetime | None = None
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
    max_wall_seconds: float | None = None
    max_api_calls: int | None = None
    max_total_tokens: int | None = None
    cost_usd: float = 0.0
    session_id: str | None = None
    llm: dict[str, Any] | None = None
    final_decision: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    schema_version: int = 1


@dataclass(slots=True)
class WorkerSession:
    id: str
    task_id: str
    persona_id: str
    role: str
    display_name: str
    state: WorkerSessionState
    opened_at: datetime
    last_heartbeat_at: datetime
    goal_epoch: str | None = None
    current_stage_id: str | None = None
    current_assignment_id: str | None = None
    active_run_id: str | None = None
    session_id: str | None = None
    last_context_receipt_at: datetime | None = None
    closed_at: datetime | None = None
    context_receipt_id: str | None = None
    compression_receipt_id: str | None = None
    skill_manifest_hash: str | None = None
    prompt_contract_hash: str | None = None
    model: str | None = None
    provider: str | None = None
    api_mode: str | None = None
    decision_count: int = 0
    proof_count: int = 0
    repair_count: int = 0
    handoff_count: int = 0
    tool_budget_used: int = 0
    read_search_budget_used: int = 0
    token_budget_used: int = 0
    watchdog_warning_count: int = 0
    last_environment_fingerprint: str | None = None
    last_failed_proof_id: str | None = None
    last_repair_signal_hash: str | None = None
    possession_state: PossessionState = PossessionState.AVAILABLE
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    close_reason: str | None = None
    schema_version: int = 1


@dataclass(slots=True)
class PersonaInstance:
    id: str
    persona_id: str
    role: str
    display_name: str
    profile_id: str | None
    runtime_root: str
    state: WorkerSessionState
    mode: str = "configured"
    goal_id: str | None = None
    # Scope-provenance pointers: the Mission Control realm/workspace this
    # instance belongs to, stamped at placement creation from the operator
    # client's active scope (a deliberate placement is minted INSIDE one
    # workspace's scene). None = runtime-global — canonical seeded rows and
    # pre-pointer records. These are the instance's own "belongs to" claim;
    # read-side consumers resolve the ids against the live realm/workspace
    # stores and fall back to roster/goal joins, so a stale pointer degrades
    # honestly instead of inventing scope.
    realm_id: str | None = None
    workspace_id: str | None = None
    # Legacy scalar parent. Retained as a denormalized back-compat MIRROR of the
    # primary steer parent (``steered_by[0]``); the PersonaInstanceStore is the
    # single writer that keeps it in sync. New code reads ``steered_by``.
    spawned_by: str | None = None
    # Authoritative living-graph parent SET (Stage 77 multi-parent fan-in): the
    # persona-instance ids that steer this child. Empty = standalone owner.
    # Back-filled from ``spawned_by`` for legacy v1 records in ``__post_init__``.
    steered_by: list[str] = field(default_factory=list)
    returned_to: str | None = None
    current_chat_goal: str | None = None
    skill_overrides: list[str] | None = None
    # Instance-level model override tier: None = inherit the backing persona
    # live (cascade: chat-session override > instance > persona > cfg default).
    model: str | None = None
    provider: str | None = None
    api_mode: str | None = None
    # Per-instance reasoning-effort override (None = inherit the runtime default;
    # applies only for models that support reasoning effort). One of
    # hermes_constants.VALID_REASONING_EFFORTS or "none". Rides the same
    # model-override lane as model/provider/api_mode so a set-model write can
    # move all four together and use_profile_default clears them together.
    reasoning_effort: str | None = None
    # issued_at of the last applied model write; stale writes are superseded.
    model_override_issued_at: datetime | None = None
    current_assignment_id: str | None = None
    current_task_id: str | None = None
    active_worker_session_id: str | None = None
    active_run_id: str | None = None
    # Durable pointer to the operator-owned Mission Control chat root.  This is
    # deliberately independent from worker/run sessions: a task bind may come
    # and go without changing which operator conversation opens by default.
    default_chat_session_id: str | None = None
    # Legacy dual-purpose pointer.  Read only for v1 migration; new writers do
    # not use it for either chat or worker ownership.
    session_id: str | None = None
    context_receipt_id: str | None = None
    compression_receipt_id: str | None = None
    prompt_contract_hash: str | None = None
    skill_manifest_hash: str | None = None
    token_budget_used: int = 0
    tool_budget_used: int = 0
    watchdog_warning_count: int = 0
    child_events_offset: int = 0
    last_heartbeat_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        # Back-compat: a legacy record (or any writer) that only set the scalar
        # ``spawned_by`` seeds the authoritative ``steered_by`` set, so every
        # reader sees a populated parent set. Idempotent — a writer that already
        # set ``steered_by`` (mirroring ``spawned_by`` = steered_by[0]) is a
        # no-op here. Kept out of ``upgrade()`` on purpose: schema_version stays
        # 1 (serde's shared upgrade hook hard-rejects any other version).
        #
        # Guarded on instance-shape: ``spawned_by`` doubles as a provenance
        # scalar and can legitimately hold a NON-instance principal (the
        # operator). Mirroring that into ``steered_by`` is exactly the defect
        # that made the HUD render "steered by operator" — a principal is not a
        # steering parent, so only an instance-shaped scalar seeds the set.
        if not self.steered_by and looks_like_persona_instance_id(self.spawned_by):
            self.steered_by = [self.spawned_by]
        if (
            not self.default_chat_session_id
            and isinstance(self.session_id, str)
            and self.session_id.startswith("persona_chat_")
        ):
            self.default_chat_session_id = self.session_id


def apply_instance_model_overrides(
    persona: AgentPersona, instance: PersonaInstance | None
) -> AgentPersona:
    """Overlay an instance's runtime overrides onto its backing persona.

    Pure: returns a copy, never mutates. ``None`` on the instance means inherit
    the persona value live. Both the chat lane and the run/tick lane must
    resolve model/provider/api_mode through this single overlay so two
    instances of one persona can run different models or assigned skill sets
    without drift between prompt observability and execution.
    """

    if instance is None:
        return persona
    instance_model = getattr(instance, "model", None)
    instance_provider = getattr(instance, "provider", None)
    instance_api_mode = getattr(instance, "api_mode", None)
    instance_skills = getattr(instance, "skill_overrides", None)
    if (
        instance_model is None
        and instance_provider is None
        and instance_api_mode is None
        and instance_skills is None
    ):
        return persona
    return replace(
        persona,
        model=instance_model if instance_model is not None else persona.model,
        provider=instance_provider if instance_provider is not None else persona.provider,
        api_mode=instance_api_mode if instance_api_mode is not None else persona.api_mode,
        skills=(
            list(instance_skills)
            if instance_skills is not None
            else list(persona.skills)
        ),
    )


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
    repo_bundle_id: str | None = None
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
    evidence_kind: str = "task_bound"
    production_proof_eligible: bool = True
    archive_scope: str = "task"
    client_message_id: str | None = None
    last_error: str | None = None
    completed_at: datetime | None = None
    signal_hash: str | None = None
    schema_version: int = 1


@dataclass(slots=True)
class RepoBundle:
    id: str
    task_id: str
    repo: str
    owner_persona_id: str | None
    state: str
    title: str
    objective: str
    stage_ids: list[str] = field(default_factory=list)
    affected_paths: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    proof_targets: list[str] = field(default_factory=list)
    proof_requirements: list[str] = field(default_factory=list)
    visual_requirements: list[str] = field(default_factory=list)
    dependency_bundle_ids: list[str] = field(default_factory=list)
    contract_input_ids: list[str] = field(default_factory=list)
    contract_output_ids: list[str] = field(default_factory=list)
    assignment_id: str | None = None
    active_run_id: str | None = None
    proof_ids: list[str] = field(default_factory=list)
    queue_reason: str | None = None
    wake_condition: str | None = None
    delivered_at: datetime | None = None
    verified_at: datetime | None = None
    rejected_at: datetime | None = None
    last_terminal_feedback: dict[str, Any] = field(default_factory=dict)
    # Delivery-time capture facts (delivering run id, patch name, changed
    # files) recorded by the delivery directive so the diff and worktree stay
    # recoverable after ``active_run_id`` is cleared.
    delivery_capture: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1


@dataclass(slots=True)
class Proof:
    id: str
    task_id: str
    stage_id: str | None
    type: ProofType
    title: str
    path_or_value: str
    created_by: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    redaction_status: str = "needs_scan"
    schema_version: int = 1


@dataclass(slots=True)
class Event:
    ts: datetime
    type: str
    task_id: str | None
    run_id: str | None
    persona_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    # Session lineage for events that belong to a conversational (non-task)
    # persona chat turn. Task-run events leave this ``None`` and remain keyed on
    # ``task_id``; chat-turn tool/progress events set this so the snapshot trace
    # projection can surface them per chat session. Optional + trailing keeps the
    # JSONL envelope backward compatible: older event rows decode with ``None``.
    session_id: str | None = None
    # Canonical chat-turn identity: the turn key derived from the operator's
    # ``client_message_id``. This is THE reconciliation key clients use to match
    # a projected trace/conversation row to their locally streamed copy of the
    # same turn — it is minted once at the send boundary and never re-derived.
    # Task-run events leave this ``None`` (their identity is ``run_id``).
    turn_id: str | None = None


@dataclass(slots=True)
class Incident:
    id: str
    task_id: str | None
    run_id: str | None
    kind: str
    summary: str
    detail_path: str | None
    opened_at: datetime
    closed_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
