from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .plan_review import PlanReview
from .proof_rules import ProofType
from .states import PossessionState, RunState, StageStatus, TaskState, WorkerSessionState


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
    sync_manifest_ref: str | None = None
    archived: bool = False
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
    spawned_by: str | None = None
    returned_to: str | None = None
    current_chat_goal: str | None = None
    skill_overrides: list[str] | None = None
    # Instance-level model override tier: None = inherit the backing persona
    # live (cascade: chat-session override > instance > persona > cfg default).
    model: str | None = None
    provider: str | None = None
    api_mode: str | None = None
    # issued_at of the last applied model write; stale writes are superseded.
    model_override_issued_at: datetime | None = None
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
    child_events_offset: int = 0
    last_heartbeat_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1


def apply_instance_model_overrides(
    persona: AgentPersona, instance: PersonaInstance | None
) -> AgentPersona:
    """Overlay a persona instance's model override tier onto its backing persona.

    Pure: returns a copy, never mutates. ``None`` on the instance means inherit
    the persona value live. Both the chat lane and the run/tick lane must
    resolve model/provider/api_mode through this single overlay so two
    instances of one persona can run different models without drift between
    the lanes.
    """

    if instance is None:
        return persona
    if instance.model is None and instance.provider is None and instance.api_mode is None:
        return persona
    return replace(
        persona,
        model=instance.model if instance.model is not None else persona.model,
        provider=instance.provider if instance.provider is not None else persona.provider,
        api_mode=instance.api_mode if instance.api_mode is not None else persona.api_mode,
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
