from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.dispatch_session_policy import DEFAULT_DISPATCH_SESSION_POLICY
from agent_runtime.permission_modes import SHIPPED_DEFAULT_PERMISSION_MODE


# S56 (2026-08-01) removed FIVE whole config blocks and pruned a sixth. Every
# one shipped on the ``runtime_config`` wire via ``asdict(cfg)`` and told an
# operator something the runtime does not do:
#
#   * ``ContinuousRoleSessionConfig`` / ``continuous_role_sessions`` — no reader
#     outside its own loader and range validators. Its
#     ``_apply_enterprise_role_session_compat`` mapper went too: one unread block
#     mapped onto another unread block.
#   * ``EnterpriseWorkerSessionsConfig`` / ``enterprise_worker_sessions`` — 15
#     fields. Twelve had no reader at all; the other three
#     (``enabled`` / ``persona_instance_runtime`` / ``persona_assignment_store``)
#     gated the persona-instance ROSTER, which S56 made unconditional. The block
#     is named for the worker-session lane deleted in the same commit.
#   * ``NormalWorkerFlowConfig`` / ``normal_worker_flow`` — no reader.
#   * ``RepoBundleRoutingConfig`` / ``repo_bundle_routing`` — consulted by
#     NOTHING, not even a validator arm that acted on it.
#   * ``SimplifiedAgentContractConfig`` / ``simplified_agent_contract`` — S27
#     left it report-only; the only thing that ever read it was the
#     ``production_envelope`` prose block, itself removed here.
#   * ``SwarmConfig`` / ``swarm`` — report-only. The enforcement the fields
#     describe (global/per-lane token + API ceilings) was never implemented.
#   * ``SupervisionConfig`` is PRUNED, not removed: ``child_events_enabled`` is
#     live (``continuity.py`` reads it to gate child.* emission). The other three
#     flags had no reader.
#
# An operator yaml that still sets any removed block LOADS AND IS IGNORED (S47
# precedent, pinned by test).


# S47 removed ``RoleEnvelopeConfig`` (11 fields) and ``RuntimeConfig.
# role_envelope``. S44 deleted the ``role_envelopes`` / ``role_checklists``
# store family the block governed; the config lane outlived it as a knob no
# code reads that still shipped on the snapshot wire — reading ``enabled:
# true`` on the live root. See
# ``tests/agent_runtime/test_s47_wire_constant_field_removal.py``.


#: What ``agent_runtime.read_model.delta_patches`` SHIPS as.
#:
#: True since 2026-08-14. The flag was born ``False`` because "the producer lane
#: is dark until the launcher fold exists" — that precondition expired: the fold
#: consumer shipped (``mission_read_model.dart``), the stream promotes coverable
#: batches to v2 ``patch`` frames, and the fold-entity handshake landed so a
#: batch naming an entity the client did not declare is demoted to the honest
#: full core rather than shipped as a patch nobody can apply.
#:
#: This is a SHIPPED DEFAULT and not a scaffolded file, deliberately. The flag is
#: ROOT-ONLY (``config.ROOT_ONLY_CONFIG_KEYS``): the only reader,
#: ``state_patches.delta_patches_enabled``, resolves
#: ``config.harness_root_config_path`` and never consults the active profile. A
#: value that must live in exactly one file is a value a WRITER can put in the
#: wrong file, and one already did — the Launcher installer's seed template
#: (``kMissionControlBaseSeedConfigYaml``) stamps ``delta_patches: true`` into
#: the fresh ``base`` PROFILE, where the reader never looks. So the lane was dark
#: for its whole life on every install: measured live 2026-08-13, ONE field
#: change on ONE persona instance shipped an 822,671-byte delta carrying an
#: 864,241-byte full snapshot core, where the patch frame the lane exists for is
#: 486 bytes. ``harness status`` reported ``delta_patches: true`` the entire time,
#: because status reads profile-aware.
#:
#: A dataclass default cannot be written to the wrong file, needs no init/upgrade
#: step to reach an install that already exists, and is applied by
#: ``config._read_model_config`` through ``raw.get(key, defaults.key)`` — so an
#: operator's explicit ``false`` still wins, and only silence resolves here.
SHIPPED_DELTA_PATCHES = True

#: Where a delta-patch config FAULT lands. Deliberately NOT the shipped default,
#: mirroring ``permission_modes.FALLBACK_DEFAULT_PERMISSION_MODE``: a config the
#: runtime could not parse has told us nothing, and "emit a new class of event
#: from every store chokepoint" is not what a runtime should infer from silence
#: it could not read. The fault path is off AND LOUD — ``delta_patches_enabled``
#: warns with the config path — because an unannounced fault-off is the exact
#: silent-dark failure the shipped default exists to retire.
FALLBACK_DELTA_PATCHES = False


@dataclass(slots=True)
class ReadModelConfig:
    enabled: bool = False
    serve_snapshot_from_db: bool = True
    db_filename: str = "read_model.db"
    # S6 producer kill-switch, now shipped ON (see ``SHIPPED_DELTA_PATCHES``).
    # When True, the steer / profile / task-transition / incident-close store
    # chokepoints append a ``state.patched`` EventLog entry carrying the
    # field-level patch, and ``stream.stream_frames`` promotes a fully-coverable
    # batch to a v2 ``patch`` frame. Set it ``false`` in the ROOT ``config.yaml``
    # to take the lane back to full-core deltas; a profile copy is inert.
    #
    # NOTE (S7-B RULING-0 COMPAT STRIP, 2026-07-16): the S2 ``history_in_frame``
    # and S3 ``inline_prompt_payloads`` kill-switches were removed here — the
    # evicted/hoisted read-model shape is the ONLY shape (operator ruling: "no
    # backward-shape support; makes things stale"). Rollback = ``git revert`` of
    # the landing, not a runtime flag flip. ``delta_patches`` stays: it gates the
    # S7-A producer lane, not a legacy-emission fallback.
    delta_patches: bool = SHIPPED_DELTA_PATCHES


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
class SupervisionConfig:
    child_events_enabled: bool = False


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
    ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/mission-chat-lane-gap-audit.md``). A deployment
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
            # Wall budget for a DETACHED dispatch (agent_chat_send wait=false).
            # Default 1800; clamped to [30, 86400]. Separate from
            # default_max_seconds on purpose: a detached dispatch exists to host
            # work that outlives a conversational window.
            dispatch_max_seconds: 1800
            # How many detached dispatches execute at once. Overflow queues.
            dispatch_max_concurrent: 3
            # Compact a chat root once its assembled context passes this many
            # tokens. A CAP, never a floor: a model whose own ratio-based
            # threshold is already lower keeps it. 0 disables the lane cap.
            compaction_threshold_tokens: 150000
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
    #: Wall budget for a DETACHED dispatch — an ``agent_chat_send(wait=false)``
    #: whose target turn runs on the background executor while the sender goes
    #: back to work. Deliberately its OWN knob rather than a share of
    #: ``default_max_seconds``: the whole point of a detached dispatch is work
    #: that outlives a conversational window ("run the suite and tell me"), and
    #: the 240 s conversational default would kill exactly the turns this lane
    #: exists to host. Default **1800 s** (30 min, operator decision 2026-08-03),
    #: clamped to the same ``[30, 86400]`` window ``default_max_seconds`` uses —
    #: below it the checkpoint reserve leaves no working window, above it one
    #: dispatch outlives the mission clock.
    dispatch_max_seconds: float = 1800.0
    #: How many detached dispatches may execute at once, mirroring
    #: ``delegation.max_concurrent_children``. The cap is what keeps a head agent
    #: that fires ten dispatches in one turn from putting ten model turns on the
    #: provider at once; the overflow QUEUES on the executor rather than being
    #: refused, because a dispatch is already asynchronous and a caller told
    #: "dispatched" must not silently lose the work.
    dispatch_max_concurrent: int = 3
    #: Token threshold at which a mission-chat ROOT compacts, expressed as an
    #: absolute CAP on the compressor's own ratio-based threshold.
    #:
    #: Why the lane needs its own number at all: Hermes derives the threshold
    #: from ``compaction_ratio × window``, which on this lane's 1,050,000-token
    #: model is 892,500 — a bound a chat root reaches roughly never. Measured
    #: 2026-08-09: the longest live root reached ~200 k in 19 turns, and one
    #: tool-heavy turn on it metered ~826 k prompt tokens across 4 provider
    #: calls where the same turn on a fresh thread meters ~50 k. The cost is
    #: subscription **limit burn**, not dollars (that lane bills at $0), and a
    #: threshold nothing can reach is not a bound.
    #:
    #: Why **150,000**: it is ~7× a measured fresh-thread turn-1 prefix
    #: (22.7 k), so a thread keeps 15–30 real turns of headroom and only a
    #: thread that outlived its task ever reaches it; it sits BELOW the 200 k
    #: root that produced the measured 16× burn, so it engages on exactly the
    #: state that motivated it; and it is 14% of the window, far enough away
    #: that one tool-heavy multi-call turn cannot cross the window mid-turn.
    #:
    #: It is applied through ``ContextCompressor.threshold_tokens_cap``, which
    #: takes the LOWER of the ratio-based threshold and the cap. So it can only
    #: make compaction fire EARLIER, never later: a 32 k-window model whose own
    #: threshold is ~27 k is untouched. ``0`` (or a negative / unparseable
    #: value) disables the lane cap and restores the model-derived threshold —
    #: that spelling IS the rollback. An explicit per-turn
    #: ``--compression-threshold-tokens`` always wins over it.
    compaction_threshold_tokens: int = 150_000


@dataclass(slots=True)
class McpAdmissionConfig:
    """How profile-declared MCP servers are registered for a run.

    ``enabled`` is the single kill switch and defaults to **False**: admission is
    the first path on which an autonomous mission-chat agent can spawn a local
    executable, so it stays off until an operator turns it on deliberately.
    The persona profile is the admission authority: only servers declared by
    that profile are considered.  Role and lane are retained on admission rows
    for observability, not as a second policy language.

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
    ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/mission-chat-mcp-admission.md``)::

        agent_runtime:
          mcp_admission:
            enabled: true
            connect_timeout_seconds: 20
            max_tool_calls_per_run: 120
    """

    enabled: bool = False
    connect_timeout_seconds: float = 20.0
    max_tool_calls_per_run: int = 120


@dataclass(slots=True)
class ToolPermissionConfig:
    """The runtime-wide DEFAULT chat tool-permission mode.

    Operator ruling 2026-08-09 (``docs/agent-runtime-harness/
    archive/2026-08-22-pre-consolidation/UNBOUNDED_DEFAULT_PLAN_2026-08-09.md``): every persona/agent in this runtime
    gets full tool access by default. Before it, the default was hardcoded twice
    in ``tool_permissions.py`` and liftable only by a per-session operator
    ritual; this block is the ONE knob that answers it, resolved at the ONE
    chokepoint (``tool_permissions.permission_options_for_chat``), which is why
    no persona or profile file needs migrating.

    ``default_mode`` ships as ``unbounded``. A deployment that wants the former
    posture writes ``profile_default`` (or ``read_only``); an unknown value is a
    typed :attr:`issues` row and falls back to ``profile_default`` — a config
    fault must never resolve to MORE capability than the operator wrote, even
    though the shipped default is the wider mode.

    Root ``config.yaml`` shape::

        agent_runtime:
          tool_permissions:
            default_mode: unbounded   # or profile_default / read_only

    Wire note: this block rides the ``runtime_config`` frame section via
    ``asdict(cfg)``. It is an ADDITION to a map a consumer already parses
    key-by-key, so it is "merely unread" rather than "invisible" by the contract
    ledger's own rule (snapshot.py, the 52-KEPT entries) — no contract bump.
    """

    default_mode: str = SHIPPED_DEFAULT_PERMISSION_MODE
    #: Typed config faults, in the ``{code, subject, summary, fix_hint}`` row
    #: shape every other harness policy surface emits. Populated by the parser;
    #: a fault NARROWS (see ``default_mode``) and is never silent.
    issues: tuple[dict[str, str], ...] = ()


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
    ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/mission-chat-terminal-envelope-grants.md``)::

        agent_runtime:
          terminal_envelope:
            grants:
              dev:
                mission_chat: [git_push]
    """

    grants: dict[str, dict[str, list[str]]] = field(default_factory=dict)


# S57 (2026-08-01) emptied the S56 reader-gate's ``UNRULED_DEBT`` bucket: the
# TWENTY-NINE scalar knobs it measured as reader-less are removed here, dataclass
# row + ``config.py`` load line + ``migrations`` range validator, in one pass.
#
#   * the ``daemon_*`` family (``daemon_enabled`` / ``_interval_seconds`` /
#     ``_idle_interval_seconds`` / ``_heartbeat_seconds``) and
#     ``task_create_auto_start_daemon`` — the Mission Daemon is retired; ``status``
#     hardcodes ``execution_mode="manual"``.
#   * ``heartbeat_ttl_seconds`` / ``max_actions_per_tick`` — the run-heartbeat TTL
#     and the per-tick action budget went with the ticker.
#   * the four ``live_run_*`` budgets and ``run_lease_seconds`` /
#     ``scope_wait_deadline_seconds`` / ``tool_wait_timeout_seconds`` — the run
#     opener that enforced them is retired; ``run_lease_seconds`` lost its last
#     non-validator reader at S56 with ``production_envelope``.
#   * the four ``liveness_*`` knobs — the watchdog reads its own constants.
#   * ``mission_max_total_tokens`` / ``mission_wall_clock_deadline_seconds`` — no
#     enforcer consults either; the soft/hard cross-check that "read" the first
#     was a validator arm on two dead fields.
#   * ``neko_recovery_attempt_cap`` / ``neko_extension_cap`` — the
#     bounded-continuation lane reads its own constants.
#   * the three ``artifact_storage_*_watermark_mb`` — no sweeper reads them.
#   * ``child_progress_min_interval_seconds`` (``continuity.py`` does not read it),
#     ``deploy_timeout_seconds`` (the lane was never wired),
#     ``preferred_goal_execution_mode`` (went with the mission/dispatch lane) and
#     ``root_node_mode`` (every skill-gate call site passes the flag literally;
#     the CONFIG field was never the source).
#
# ``lock_acquire_timeout_seconds`` STAYS: ``locks.py`` reads it. It is the only
# scalar in this neighbourhood that survived the gate, which is exactly why the
# gate is AST-based rather than prefix-based.
#
# Every one shipped on the ``runtime_config`` wire via ``asdict(cfg)`` and told an
# operator it governed a budget, a deadline or a mode. Removing them EDITS the
# emitted frame (verified against the live root: all 29 were present in
# ``snapshot --json`` at contract 47), so this is a contract move — 47 -> 48 with
# a Launcher lockstep pin. An operator yaml that still sets any of them LOADS AND
# IS IGNORED (S47 precedent, pinned by test).


@dataclass(slots=True)
class RuntimeConfig:
    schema_version: int = 1
    default_provider: str | None = None
    default_model: str | None = None
    default_api_mode: str = "codex_responses"
    redaction_mode: str = "strict"
    # B5 (2026-07-31): ``open_incident_warning_threshold`` stood here. Its sole
    # reader was a snapshot parity warning keyed on ``summary.open_incidents``,
    # a field the frame stopped emitting at contract 45 — so the knob governed
    # nothing while riding the ``runtime_config`` wire advertising a budget that
    # could not be exceeded. No profile config sets it; no Launcher code reads it.
    lock_acquire_timeout_seconds: int = 15
    read_model: ReadModelConfig = field(default_factory=ReadModelConfig)
    persona_chat: PersonaChatConfig = field(default_factory=PersonaChatConfig)
    event_log: EventLogConfig = field(default_factory=EventLogConfig)
    supervision: SupervisionConfig = field(default_factory=SupervisionConfig)
    coordinator_permissions: CoordinatorPermissionConfig = field(default_factory=CoordinatorPermissionConfig)
    mission_chat: MissionChatConfig = field(default_factory=MissionChatConfig)
    mcp_admission: McpAdmissionConfig = field(default_factory=McpAdmissionConfig)
    terminal_envelope: TerminalEnvelopeConfig = field(default_factory=TerminalEnvelopeConfig)
    tool_permissions: ToolPermissionConfig = field(default_factory=ToolPermissionConfig)
