from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.dispatch_session_policy import DEFAULT_DISPATCH_SESSION_POLICY


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
    hide_request_test_run_until_gate: bool = True
    self_test_evidence_capture: bool = True
    max_self_test_repeats_without_change: int = 1
    expose_worker_actions_in_contract_dump: bool = True


# S47 removed ``RoleEnvelopeConfig`` (11 fields) and ``RuntimeConfig.
# role_envelope``. S44 deleted the ``role_envelopes`` / ``role_checklists``
# store family the block governed; the config lane outlived it as a knob no
# code reads that still shipped on the snapshot wire — reading ``enabled:
# true`` on the live root. See
# ``tests/agent_runtime/test_s47_wire_constant_field_removal.py``.


@dataclass(slots=True)
class RepoBundleRoutingConfig:
    enabled: bool = False
    strict_repo_ownership: bool = True
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

    ``dispatch_session_policy`` decides which chat session an agent→agent
    dispatch lands in when the caller states no thread target. It defaults to
    ``new_per_dispatch``: each dispatched task starts a fresh, task-scoped
    thread (with typed lineage back to its predecessor) instead of accumulating
    in one sticky mega-thread per pair, which re-fed the whole transcript to the
    provider on every turn. ``sticky`` restores the previous behavior
    deployment-wide. The same precedence rule applies — an explicit
    ``session_id`` or ``new_session`` on the send always wins. The decision
    itself lives in :mod:`agent_runtime.dispatch_session_policy`.

    Root ``config.yaml`` shape::

        agent_runtime:
          mission_chat:
            # Wall budget for a mission-chat turn when --max-seconds is absent.
            # Default 240; clamped to [30, 86400]. An explicit --max-seconds
            # always wins. The last max(60s, 15%) of the window is reserved for
            # the turn's graceful checkpoint reply.
            default_max_seconds: 1800
            # Thread target for a dispatch that names none: new_per_dispatch
            # (default — one thread per task) or sticky (one durable thread
            # per pair). An explicit session_id / new_session always wins.
            dispatch_session_policy: new_per_dispatch
            # Bind a clarify ANSWER to the thread its QUESTION was asked in,
            # via an echoed clarify_token, instead of trusting the replier to
            # reproduce the session_id. Default true.
            clarify_token_binding: true
    """

    default_max_seconds: float = 240.0
    dispatch_session_policy: str = DEFAULT_DISPATCH_SESSION_POLICY
    #: Whether the mission-chat lane mints a ``clarify_token`` alongside a
    #: ``clarify_request`` and binds a reply that echoes one back to the thread
    #: the question was asked in. Defaults to **True**: the failure it retires
    #: (a clarify answer opening a THIRD thread, leaving the child reading a
    #: bare choice with no question attached) is silent and lossy, and both
    #: directions are additive — a caller that never echoes hits exactly
    #: today's precedence. Flipping it to ``false`` IS the rollback: no token is
    #: minted, none is resolved, ticket files go inert and the TTL sweep
    #: reclaims them. Loaded through ``load_root_runtime_config`` for the same
    #: reason ``dispatch_session_policy`` is — one profile's own config must not
    #: change how every other profile threads.
    clarify_token_binding: bool = True


@dataclass(slots=True)
class McpAdmissionConfig:
    """Which personas may have their declared MCP servers registered for a run.

    ``enabled`` is the single kill switch and defaults to **False**: admission is
    the first path on which an autonomous mission-chat agent can spawn a local
    executable, so it stays off until an operator turns it on deliberately.
    ``roles`` is deny-by-default with no wildcard — a role with no entry, or a
    lane with no entry under that role, admits nothing.

    ``max_tool_calls_per_run`` is the per-run MCP call budget (design §3's
    residual-risk mitigation, §7's ``mcp_admission_budget_exhausted`` row): once
    a run has spent it, further admitted MCP calls are refused with a typed row
    instead of dispatched. It bounds a LOOPING admitted agent, which single-flight
    (one admission at a time) and the wall/AS0 watchdogs (bound the turn's clock,
    not its call count) cannot. There is deliberately no "unlimited" spelling —
    a non-positive or unparseable value falls back to the default and the parser
    caps it, because an unbounded admitted MCP surface is the exact failure this
    budget exists to prevent.

    Root ``config.yaml`` shape (see
    ``docs/agent-runtime-harness/mission-chat-mcp-admission.md``)::

        agent_runtime:
          mcp_admission:
            enabled: true
            connect_timeout_seconds: 20
            max_tool_calls_per_run: 120
            roles:
              qa:
                mission_chat: [launcher_qa]
    """

    enabled: bool = False
    connect_timeout_seconds: float = 20.0
    max_tool_calls_per_run: int = 120
    roles: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(slots=True)
class TerminalEnvelopeConfig:
    """Which envelope-gated command classes a role may run on a governed lane.

    There is deliberately NO ``enabled`` kill switch: enforcement is
    unconditional, and the deny-by-default property comes from the grant table
    being EMPTY rather than from a flag. One authority, no double negative — an
    operator revokes a grant by deleting the class from the list.

    ``grants`` is deny-by-default with no wildcard and no inheritance: a role
    with no entry, or a lane with no entry under that role, grants nothing. The
    classes an operator may name at all are bounded by
    ``agent_runtime.terminal_envelope.GRANTABLE_COMMAND_CLASSES``; anything
    else is a typed config error rather than a silent grant.

    Root ``config.yaml`` shape (see
    ``docs/agent-runtime-harness/mission-chat-terminal-envelope-grants.md``)::

        agent_runtime:
          terminal_envelope:
            grants:
              dev:
                mission_chat: [git_push]
    """

    grants: dict[str, dict[str, list[str]]] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeConfig:
    schema_version: int = 1
    heartbeat_ttl_seconds: int = 900
    max_actions_per_tick: int = 1
    default_provider: str | None = None
    default_model: str | None = None
    default_api_mode: str = "codex_responses"
    redaction_mode: str = "strict"
    # B5 (2026-07-31): ``open_incident_warning_threshold`` stood here. Its sole
    # reader was a snapshot parity warning keyed on ``summary.open_incidents``,
    # a field the frame stopped emitting at contract 45 — so the knob governed
    # nothing while riding the ``runtime_config`` wire advertising a budget that
    # could not be exceeded. No profile config sets it; no Launcher code reads it.
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
    terminal_envelope: TerminalEnvelopeConfig = field(default_factory=TerminalEnvelopeConfig)
