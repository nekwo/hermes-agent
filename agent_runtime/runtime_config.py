from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ContinuousRoleSessionConfig:
    enabled: bool = False
    observe_only: bool = True
    max_decisions_per_envelope: int = 4
    max_proofs_per_envelope: int = 2
    max_continuations_per_stage: int = 3
    continue_after_passing_proof: bool = True
    continue_after_failed_proof: bool = False
    close_on_state_owner_change: bool = True
    close_on_open_incident: bool = True
    close_on_invalid_output: bool = True
    close_on_budget_warning: bool = True


@dataclass(slots=True)
class EnterpriseWorkerSessionsConfig:
    enabled: bool = False
    mode: str = "observe_only"
    worker_session_store: bool = True
    persona_instance_runtime: bool = False
    persona_assignment_store: bool = False
    same_session_continuation: bool = False
    harness_owned_proof_recipes: bool = False
    no_edit_certification_sandbox: bool = False
    possession_controls: bool = False
    static_prompt_strategy: str = "capability_detect"
    worker_heartbeat_seconds: int = 5
    worker_stale_seconds: int = 600
    possession_lease_seconds: int = 900
    max_same_worker_repairs_per_stage: int = 1
    max_worker_context_compressions_per_goal: int = 3


@dataclass(slots=True)
class NormalWorkerFlowConfig:
    enabled: bool = False
    dev_self_tests_in_session: bool = True
    auto_final_gate_after_delivery: bool = True
    hide_request_test_run_until_gate: bool = True
    self_test_evidence_capture: bool = True
    max_self_test_repeats_without_change: int = 1
    max_auto_final_gate_repairs_per_stage: int = 1
    expose_worker_actions_in_contract_dump: bool = True


@dataclass(slots=True)
class MissionPlanConfig:
    enabled: bool = False
    enforce_hud: bool = True
    version: int = 1


@dataclass(slots=True)
class RoleEnvelopeConfig:
    enabled: bool = False
    prefer_same_session: bool = True
    checklist_hud_enabled: bool = True
    self_approval_enabled: bool = True
    qa_final_approval_required: bool = True
    max_same_session_continuations: int = 8
    max_no_progress_repeats: int = 1
    max_fix_envelopes_per_stage: int = 2
    max_checklist_items_rendered: int = 8
    max_foreign_checklist_summaries: int = 3
    enable_legacy_stage_projection: bool = True


@dataclass(slots=True)
class RepoBundleRoutingConfig:
    enabled: bool = False
    strict_repo_ownership: bool = True
    auto_create_from_mission_plan: bool = True
    queue_on_dependency_bundles: bool = True
    expose_in_snapshot: bool = True


@dataclass(slots=True)
class SimplifiedAgentContractConfig:
    enabled: bool = False
    expose_only_simplified_actions: bool = True
    keep_internal_state_machine: bool = True
    terminal_feedback_enabled: bool = True


@dataclass(slots=True)
class ReadModelConfig:
    enabled: bool = False
    serve_snapshot_from_db: bool = True
    db_filename: str = "read_model.db"
    # S2 read-model kill-switch. When False (default), history sections
    # (``archived_tasks``, closed/ancient ``incidents``, persona-chat message
    # tails) are EVICTED from the steady-state frame and served via paged
    # on-demand queries; the frame carries typed pointer stubs in their place.
    # Flip True to restore the old full-in-frame shape for one release so a
    # surface that regressed can be caught against the previous shape.
    history_in_frame: bool = False
    # S6 producer kill-switch. When False (default), store chokepoints emit NO
    # ``state.patched`` field-patch entries — the producer lane is dark until the
    # launcher fold exists (the consumer + stream wire land after S4). Flip True
    # to have steer/profile/task-transition/incident-close chokepoints append a
    # ``state.patched`` EventLog entry carrying the field-level patch. Log-only:
    # no stream/wire change rides this flag in S6.
    delta_patches: bool = False
    # S3 read-model kill-switch. When False (default), the duplicated skills
    # catalogs (``available_skills`` / ``skills_catalog`` / ``accessible_skills``
    # / ``skills``) are HOISTED out of every ``chat_contexts`` row and stored
    # once, content-addressed, under ``prompt_observability.skills_catalogs``
    # (rows carry ``available_skills_ref`` / ``accessible_skills_ref`` hashes),
    # and the heavy per-turn ``final_model_input`` debug payload is EVICTED from
    # the frame to a typed stub (the full row stays on disk). Flip True to
    # restore the old inline payloads for one release for A/B parity.
    inline_prompt_payloads: bool = False


@dataclass(slots=True)
class SwarmConfig:
    enabled: bool = False
    requires_certification: bool = True
    allow_uncertified_dev_swarm: bool = False
    max_active_lanes: int = 2
    global_token_soft_limit: int = 2_000_000
    global_token_hard_limit: int = 3_000_000
    global_api_call_soft_limit: int = 200
    global_api_call_hard_limit: int = 300
    per_lane_token_limit: int = 1_000_000
    per_lane_api_call_limit: int = 100


@dataclass(slots=True)
class SupervisionConfig:
    child_events_enabled: bool = False
    recursive_enabled: bool = False
    hierarchical_budget_enabled: bool = False
    deploy_verification_enabled: bool = False


@dataclass(slots=True)
class CoordinatorPermissionConfig:
    max_spawns: int = 0
    may_kill_own: bool = True
    may_kill_others: bool = False


@dataclass(slots=True)
class RuntimeConfig:
    schema_version: int = 1
    heartbeat_ttl_seconds: int = 900
    max_actions_per_tick: int = 1
    default_provider: str | None = None
    default_model: str | None = None
    default_api_mode: str = "codex_responses"
    redaction_mode: str = "strict"
    open_incident_warning_threshold: int = 100
    daemon_enabled: bool = False
    daemon_interval_seconds: int = 10
    daemon_idle_interval_seconds: int = 30
    daemon_heartbeat_seconds: int = 5
    task_create_auto_start_daemon: bool = False
    root_node_mode: bool = False
    preferred_goal_execution_mode: str = "in_process_controller"
    live_run_max_wall_seconds: float = 300.0
    live_run_max_api_calls: int = 20
    live_run_max_total_tokens: int = 750_000
    live_run_iteration_budget: int = 60
    scope_wait_deadline_seconds: int = 900
    run_lease_seconds: int = 600
    tool_wait_timeout_seconds: int = 300
    liveness_enabled: bool = True
    liveness_poll_seconds: int = 60
    liveness_quiet_strikes: int = 2
    liveness_hung_seconds: int = 300
    child_progress_min_interval_seconds: int = 30
    deploy_timeout_seconds: int = 120
    lock_acquire_timeout_seconds: int = 15
    mission_max_total_tokens: int = 1_000_000
    mission_wall_clock_deadline_seconds: int = 86_400
    neko_recovery_attempt_cap: int = 2
    neko_extension_cap: int = 2
    artifact_storage_low_watermark_mb: int = 512
    artifact_storage_high_watermark_mb: int = 1024
    artifact_storage_critical_watermark_mb: int = 2048
    continuous_role_sessions: ContinuousRoleSessionConfig = field(default_factory=ContinuousRoleSessionConfig)
    enterprise_worker_sessions: EnterpriseWorkerSessionsConfig = field(default_factory=EnterpriseWorkerSessionsConfig)
    normal_worker_flow: NormalWorkerFlowConfig = field(default_factory=NormalWorkerFlowConfig)
    mission_plan: MissionPlanConfig = field(default_factory=MissionPlanConfig)
    role_envelope: RoleEnvelopeConfig = field(default_factory=RoleEnvelopeConfig)
    repo_bundle_routing: RepoBundleRoutingConfig = field(default_factory=RepoBundleRoutingConfig)
    simplified_agent_contract: SimplifiedAgentContractConfig = field(default_factory=SimplifiedAgentContractConfig)
    read_model: ReadModelConfig = field(default_factory=ReadModelConfig)
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    supervision: SupervisionConfig = field(default_factory=SupervisionConfig)
    coordinator_permissions: CoordinatorPermissionConfig = field(default_factory=CoordinatorPermissionConfig)

