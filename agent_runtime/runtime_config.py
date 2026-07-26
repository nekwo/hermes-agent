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
    # S6 producer kill-switch. When False (default), store chokepoints emit NO
    # ``state.patched`` field-patch entries — the producer lane is dark until the
    # launcher fold exists (the consumer + stream wire land after S4). Flip True
    # to have steer/profile/task-transition/incident-close chokepoints append a
    # ``state.patched`` EventLog entry carrying the field-level patch. Log-only:
    # no stream/wire change rides this flag in S6.
    #
    # NOTE (S7-B RULING-0 COMPAT STRIP, 2026-07-16): the S2 ``history_in_frame``
    # and S3 ``inline_prompt_payloads`` kill-switches were removed here — the
    # evicted/hoisted read-model shape is the ONLY shape (operator ruling: "no
    # backward-shape support; makes things stale"). Rollback = ``git revert`` of
    # the landing, not a runtime flag flip. ``delta_patches`` stays: it gates the
    # in-progress S6 producer lane, not a legacy-emission fallback.
    delta_patches: bool = False


@dataclass(slots=True)
class PersonaChatConfig:
    """Process-resident persona-chat cache policy.

    Native SessionDB history, the root lease, and the turn journal remain
    authoritative when this optimization is disabled.
    """

    hot_sessions_enabled: bool = False
    max_hot_sessions: int = 8
    idle_ttl_seconds: int = 1800


@dataclass(slots=True)
class EventLogConfig:
    """Durable-log hygiene (C6a): size-gated, offset-safe rotation of the
    append-only ``events.jsonl``.

    ``rotation_cap_bytes`` is the size at/above which the live slice is sealed
    into an archive slice (``events_archive/events.<start_offset>.jsonl``) and a
    fresh live slice is opened. Logical (cross-slice, monotonic) offsets are
    preserved via the slice manifest, so byte-offset tailers and the checkpoint
    ``event_offset`` watermark keep resolving unchanged. ``0`` disables rotation
    (the live file grows unbounded, i.e. legacy behavior). The env override
    ``HERMES_EVENT_LOG_ROTATION_CAP_BYTES`` wins over this field when set
    (operator/test knob); no config is required for the 16 MiB default.
    """

    rotation_cap_bytes: int = 16 * 1024 * 1024


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
class MissionChatConfig:
    """Harness-wide defaults for the canonical Mission Control chat lane.

    ``default_max_seconds`` is the wall budget a mission-chat turn gets when the
    caller passes no ``--max-seconds``. It defaults to **240 s** — the value the
    CLI parser hardcoded before this block existed, so an absent stanza keeps
    today's behavior exactly.

    Why it is configurable: the mission-chat lane is the primary home for agent
    work, but 240 s is a *conversation*-shaped window (the last
    ``max(60s, 15%)`` is reserved for the graceful checkpoint, so a default turn
    has ~180 s of tool-using time — see ``turn_budget`` and G10 of
    ``docs/agent-runtime-harness/mission-chat-lane-gap-audit.md``). A deployment
    that runs real work here raises the floor once, in one place, instead of
    teaching every caller to pass a flag.

    An explicit ``--max-seconds`` on a turn ALWAYS wins over this default — the
    config sets the budget for callers that express no opinion, it caps nobody.
    The configured value itself is clamped to ``[30 s, 86400 s]``: below the
    clamp the checkpoint reserve leaves no working window at all, and above it
    one conversational turn outlives the mission wall-clock deadline.

    Root ``config.yaml`` shape::

        agent_runtime:
          mission_chat:
            # Wall budget for a mission-chat turn when --max-seconds is absent.
            # Default 240; clamped to [30, 86400]. An explicit --max-seconds
            # always wins. The last max(60s, 15%) of the window is reserved for
            # the turn's graceful checkpoint reply.
            default_max_seconds: 1800
    """

    default_max_seconds: float = 240.0


@dataclass(slots=True)
class McpAdmissionConfig:
    """Which personas may have their declared MCP servers registered for a run.

    ``enabled`` is the single kill switch and defaults to **False**: admission is
    the first path on which an autonomous mission-chat agent can spawn a local
    executable, so it stays off until an operator turns it on deliberately.
    ``roles`` is deny-by-default with no wildcard — a role with no entry, or a
    lane with no entry under that role, admits nothing.

    Root ``config.yaml`` shape (see
    ``docs/agent-runtime-harness/mission-chat-mcp-admission.md``)::

        agent_runtime:
          mcp_admission:
            enabled: true
            connect_timeout_seconds: 20
            roles:
              qa:
                mission_chat: [launcher_qa]
    """

    enabled: bool = False
    connect_timeout_seconds: float = 20.0
    roles: dict[str, dict[str, list[str]]] = field(default_factory=dict)


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
    persona_chat: PersonaChatConfig = field(default_factory=PersonaChatConfig)
    event_log: EventLogConfig = field(default_factory=EventLogConfig)
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    supervision: SupervisionConfig = field(default_factory=SupervisionConfig)
    coordinator_permissions: CoordinatorPermissionConfig = field(default_factory=CoordinatorPermissionConfig)
    mission_chat: MissionChatConfig = field(default_factory=MissionChatConfig)
    mcp_admission: McpAdmissionConfig = field(default_factory=McpAdmissionConfig)
