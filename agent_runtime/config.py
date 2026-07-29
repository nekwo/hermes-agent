from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_config_path
from hermes_cli.profiles import profile_exists

from .dispatch_session_policy import normalize_dispatch_session_policy
from .personas import BUNDLED_PERSONA_IDS, BUNDLED_PERSONA_PROFILES, DEFAULT_PERSONA_IDS, PROFILE_ROLE_SENTINEL, coerce_agent_role, default_personas, seed_personas, validate_toolsets, AgentRole
from .profile_context import active_profile_name
from .redaction_mode import normalize_redaction_mode
from .runtime_config import ContinuousRoleSessionConfig, CoordinatorPermissionConfig, EnterpriseWorkerSessionsConfig, EventLogConfig, McpAdmissionConfig, MissionChatConfig, NormalWorkerFlowConfig, PersonaChatConfig, ReadModelConfig, RepoBundleRoutingConfig, RoleEnvelopeConfig, RuntimeConfig, SimplifiedAgentContractConfig, SupervisionConfig, SwarmConfig, TerminalEnvelopeConfig

logger = logging.getLogger(__name__)

#: Bounds for ``agent_runtime.mission_chat.default_max_seconds``. Below the
#: floor the graceful checkpoint reserve (``turn_budget``: ``max(60s, 15%)``,
#: capped so 30 s of work survives) consumes the whole window and the turn can
#: run no tool at all; above the ceiling one conversational turn outlives the
#: mission wall-clock deadline (``mission_wall_clock_deadline_seconds``, 86400).
MISSION_CHAT_MIN_MAX_SECONDS = 30.0
MISSION_CHAT_MAX_MAX_SECONDS = 86_400.0

#: Hard ceiling for ``agent_runtime.mcp_admission.max_tool_calls_per_run``.
#: A per-run MCP call budget only bounds a looping agent while it is actually
#: reachable, so "effectively unlimited" must not be spellable in config — a
#: mistyped ``1000000`` clamps here instead of silently retiring the bound. The
#: value is far above any honest QA drill (the 6-row Stage C acceptance matrix
#: costs ~60 admitted calls) and far below a loop worth paying for.
MCP_ADMISSION_MAX_TOOL_CALLS_CEILING = 1_000


@dataclass(slots=True)
class AgentRuntimeConfig(RuntimeConfig):
    store_root: str | None = None
    head_agent_profile: str | None = None
    personas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Provenance of the resolved runtime default: which YAML key actually
    # supplied ``default_model`` / ``default_provider``. The top-level ``model:``
    # block is the surface ``hermes model`` / ``hermes status`` own and the user
    # treats as truth; ``agent_runtime.default_*`` is an explicit harness-wide
    # override (and, when absent, silence — the runtime follows the top-level
    # default). These labels let the launcher and ``hermes harness config show``
    # report which authority won without re-deriving it.
    default_model_source: str = "unset"
    default_provider_source: str = "unset"


def _clean_config_str(value: Any) -> str | None:
    """Return a stripped non-empty string, else None (empty/whitespace == unset)."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _top_level_model_authority(top: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read the top-level ``model:`` block — the authority the user sets via
    ``hermes model`` and reads back via ``hermes status``.

    Tolerates ``model:`` as a mapping (``default``/``name`` + ``provider``) or as
    a bare string model id, mirroring ``hermes_cli/status.py``'s tolerance.
    """
    model_block = top.get("model")
    if isinstance(model_block, dict):
        model = _clean_config_str(model_block.get("default")) or _clean_config_str(model_block.get("name"))
        return (model, _clean_config_str(model_block.get("provider")))
    if isinstance(model_block, str):
        return (_clean_config_str(model_block), None)
    return (None, None)


def _resolve_default_authority(
    override_value: Any,
    top_value: str | None,
    override_source: str,
    top_source: str,
) -> tuple[str | None, str]:
    """Resolve a runtime default: an explicit ``agent_runtime.*`` override wins;
    otherwise the top-level ``model.*`` authority; otherwise unset."""
    override = _clean_config_str(override_value)
    if override is not None:
        return (override, override_source)
    if top_value is not None:
        return (top_value, top_source)
    return (None, "unset")


def load_agent_runtime_config(config_path: Path | None = None) -> AgentRuntimeConfig:
    from .parse_cache import cached_yaml_file

    config_path = config_path or get_config_path()
    # mtime-cached parse: this is called many times per snapshot build (and across
    # the runtime) and was re-parsing the full config.yaml each time.
    loaded = cached_yaml_file(config_path, default=None)
    top = loaded if isinstance(loaded, dict) else {}
    raw = top.get("agent_runtime", {}) or {}
    # Single runtime-default authority: the harness follows the top-level
    # ``model.default`` the user sets, unless ``agent_runtime.default_*`` is
    # explicitly pinned as a harness-wide override.
    top_model, top_provider = _top_level_model_authority(top)
    resolved_model, default_model_source = _resolve_default_authority(
        raw.get("default_model"), top_model, "agent_runtime.default_model", "model.default"
    )
    resolved_provider, default_provider_source = _resolve_default_authority(
        raw.get("default_provider"), top_provider, "agent_runtime.default_provider", "model.provider"
    )
    enterprise_worker_sessions = _enterprise_worker_sessions_config(raw.get("enterprise_worker_sessions") or {})
    continuous_role_sessions = _continuous_role_sessions_config(raw.get("continuous_role_sessions") or {})
    normal_worker_flow = _normal_worker_flow_config(raw.get("normal_worker_flow") or {})
    role_envelope = _role_envelope_config(raw.get("role_envelope") or {})
    repo_bundle_routing = _repo_bundle_routing_config(raw.get("repo_bundle_routing") or {})
    simplified_agent_contract = _simplified_agent_contract_config(raw.get("simplified_agent_contract") or {})
    read_model = _read_model_config(raw.get("read_model") or {})
    persona_chat = _persona_chat_config(raw.get("persona_chat") or {})
    event_log = _event_log_config(raw.get("event_log") or {})
    swarm = _swarm_config(raw.get("swarm") or {})
    supervision = _supervision_config(raw.get("supervision") or {})
    coordinator_permissions = _coordinator_permission_config(raw.get("coordinator_permissions") or {})
    mission_chat = _mission_chat_config(raw.get("mission_chat") or {})
    mcp_admission = _mcp_admission_config(raw.get("mcp_admission") or {})
    terminal_envelope = _terminal_envelope_config(raw.get("terminal_envelope") or {})
    continuous_role_sessions = _apply_enterprise_role_session_compat(
        continuous_role_sessions,
        enterprise_worker_sessions=enterprise_worker_sessions,
        raw_continuous=raw.get("continuous_role_sessions"),
    )
    cfg = AgentRuntimeConfig(
        schema_version=int(raw.get("schema_version", 1)),
        store_root=raw.get("store_root"),
        head_agent_profile=raw.get("head_agent_profile") or raw.get("head_profile"),
        default_provider=resolved_provider,
        default_model=resolved_model,
        default_api_mode=raw.get("default_api_mode", "codex_responses"),
        redaction_mode=normalize_redaction_mode(raw.get("redaction_mode") or os.environ.get("HERMES_REDACTION_MODE", "strict")),
        open_incident_warning_threshold=_positive_int(raw.get("open_incident_warning_threshold"), 100),
        heartbeat_ttl_seconds=int(raw.get("heartbeat_ttl_seconds", 900)),
        max_actions_per_tick=int(raw.get("max_actions_per_tick", 1)),
        daemon_enabled=bool((raw.get("daemon") or {}).get("enabled", raw.get("daemon_enabled", False))),
        daemon_interval_seconds=int((raw.get("daemon") or {}).get("interval_seconds", raw.get("daemon_interval_seconds", 10))),
        daemon_idle_interval_seconds=int((raw.get("daemon") or {}).get("idle_interval_seconds", raw.get("daemon_idle_interval_seconds", 30))),
        daemon_heartbeat_seconds=int((raw.get("daemon") or {}).get("heartbeat_seconds", raw.get("daemon_heartbeat_seconds", 5))),
        task_create_auto_start_daemon=bool(raw.get("task_create_auto_start_daemon", False)),
        root_node_mode=bool(raw.get("root_node_mode", False)),
        preferred_goal_execution_mode=str(raw.get("preferred_goal_execution_mode", "in_process_controller") or "in_process_controller"),
        live_run_max_wall_seconds=float(raw.get("live_run_max_wall_seconds", 300.0)),
        live_run_max_api_calls=int(raw.get("live_run_max_api_calls", 20)),
        live_run_max_total_tokens=int(raw.get("live_run_max_total_tokens", 750_000)),
        live_run_iteration_budget=int(raw.get("live_run_iteration_budget", 60)),
        scope_wait_deadline_seconds=int(raw.get("scope_wait_deadline_seconds", 900)),
        run_lease_seconds=int(raw.get("run_lease_seconds", 600)),
        tool_wait_timeout_seconds=int(raw.get("tool_wait_timeout_seconds", 300)),
        liveness_enabled=bool(raw.get("liveness_enabled", True)),
        liveness_poll_seconds=_clamped_positive_int(raw.get("liveness_poll_seconds"), 60, minimum=30, maximum=120),
        liveness_quiet_strikes=_positive_int(raw.get("liveness_quiet_strikes"), 2),
        liveness_hung_seconds=_positive_int(raw.get("liveness_hung_seconds"), 300),
        child_progress_min_interval_seconds=_positive_int(raw.get("child_progress_min_interval_seconds"), 30),
        deploy_timeout_seconds=_positive_int(raw.get("deploy_timeout_seconds"), 120),
        lock_acquire_timeout_seconds=_positive_int(raw.get("lock_acquire_timeout_seconds"), 15),
        mission_max_total_tokens=int(raw.get("mission_max_total_tokens", 1_000_000)),
        mission_wall_clock_deadline_seconds=int(raw.get("mission_wall_clock_deadline_seconds", 86_400)),
        neko_recovery_attempt_cap=int(raw.get("neko_recovery_attempt_cap", 2)),
        neko_extension_cap=int(raw.get("neko_extension_cap", 2)),
        artifact_storage_low_watermark_mb=int(raw.get("artifact_storage_low_watermark_mb", 512)),
        artifact_storage_high_watermark_mb=int(raw.get("artifact_storage_high_watermark_mb", 1024)),
        artifact_storage_critical_watermark_mb=int(raw.get("artifact_storage_critical_watermark_mb", 2048)),
        continuous_role_sessions=continuous_role_sessions,
        enterprise_worker_sessions=enterprise_worker_sessions,
        normal_worker_flow=normal_worker_flow,
        role_envelope=role_envelope,
        repo_bundle_routing=repo_bundle_routing,
        simplified_agent_contract=simplified_agent_contract,
        read_model=read_model,
        persona_chat=persona_chat,
        event_log=event_log,
        swarm=swarm,
        supervision=supervision,
        coordinator_permissions=coordinator_permissions,
        mission_chat=mission_chat,
        mcp_admission=mcp_admission,
        terminal_envelope=terminal_envelope,
        personas=raw.get("personas", {}) or {},
        default_model_source=default_model_source,
        default_provider_source=default_provider_source,
    )
    return cfg


def _override_state(override: str | None, top_value: str | None) -> str:
    """Classify an ``agent_runtime.default_*`` value against the top-level authority.

    - ``absent``: no override (the healthy state — the runtime follows the user default).
    - ``override_only``: override set but no top-level authority to compare against.
    - ``redundant``: override equals the top-level default (an unmaintained duplicate;
      recommend removal so the single authority stays single).
    - ``shadowing``: override diverges from the top-level default (the stale-pin bug —
      agents silently run something other than what the user set).
    """
    if override is None:
        return "absent"
    if top_value is None:
        return "override_only"
    return "redundant" if override == top_value else "shadowing"


def describe_runtime_default_authority(config_path: Path | None = None) -> dict[str, Any]:
    """Pure, redaction-safe provenance report comparing the top-level ``model:``
    authority against any ``agent_runtime.*`` override and per-persona pins.

    Single source of truth for the migrations warning and the harness-doctor
    ``model_authority`` block, so the "shadowing vs redundant vs stale pin"
    classification is never re-derived divergently.
    """
    from .parse_cache import cached_yaml_file

    config_path = config_path or get_config_path()
    loaded = cached_yaml_file(config_path, default=None)
    top = loaded if isinstance(loaded, dict) else {}
    raw = top.get("agent_runtime", {}) or {}
    top_model, top_provider = _top_level_model_authority(top)
    override_model = _clean_config_str(raw.get("default_model"))
    override_provider = _clean_config_str(raw.get("default_provider"))
    resolved_model, model_source = _resolve_default_authority(
        raw.get("default_model"), top_model, "agent_runtime.default_model", "model.default"
    )
    resolved_provider, provider_source = _resolve_default_authority(
        raw.get("default_provider"), top_provider, "agent_runtime.default_provider", "model.provider"
    )

    persona_pins: list[dict[str, Any]] = []
    personas = raw.get("personas", {}) or {}
    if isinstance(personas, dict):
        for pid, overrides in personas.items():
            if not isinstance(overrides, dict):
                continue
            pin_model = _clean_config_str(overrides.get("model"))
            pin_provider = _clean_config_str(overrides.get("provider"))
            if pin_model is None and pin_provider is None:
                continue
            persona_pins.append({
                "persona_id": "neko_supervisor" if pid == "alice_supervisor" else pid,
                "model": pin_model,
                "provider": pin_provider,
                # None when the pin sets no model (provider-only pin); otherwise
                # whether the pin duplicates the resolved runtime default (redundant).
                "matches_runtime_default": (pin_model == resolved_model) if pin_model is not None else None,
                "provider_pinned_without_model": pin_provider is not None and pin_model is None,
            })

    return {
        "resolved": {
            "model": resolved_model,
            "provider": resolved_provider,
            "model_source": model_source,
            "provider_source": provider_source,
        },
        "top_level": {"model": top_model, "provider": top_provider},
        "harness_override": {
            "model": override_model,
            "provider": override_provider,
            "model_state": _override_state(override_model, top_model),
            "provider_state": _override_state(override_provider, top_provider),
        },
        "persona_pins": persona_pins,
    }


def harness_root_config_path() -> Path:
    """The harness-global ``config.yaml`` under the Hermes ROOT home.

    Harness-wide operator policy must resolve against the ROOT config no
    matter which profile is sticky-active. The CLI bootstrap redirects a bare
    invocation into the active profile's home
    (``hermes_cli.main._apply_profile_override``), so ``get_config_path()`` —
    and therefore ``load_agent_runtime_config()`` with no argument — silently
    reads THAT profile's ``config.yaml``. Live proof 2026-07-23: with
    ``alice`` sticky-active, the mission-chat lane resolved
    ``chat_lane_restore_toolsets`` against ``profiles/alice/config.yaml``,
    so the operator's root-config ruling (Neko ``file`` restore, 2026-07-18)
    was dead on arrival. Policy readers that must be immune to that redirect
    load through this path instead of ``get_config_path()``.
    """

    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "config.yaml"


def load_root_runtime_config() -> AgentRuntimeConfig:
    """Load the harness-global runtime config from the ROOT ``config.yaml``.

    Convenience for harness-wide policy readers that must be immune to the CLI
    profile redirect. A bare invocation runs
    ``hermes_cli.main._apply_profile_override`` at import time, which reads
    ``<root>/active_profile`` and points ``HERMES_HOME`` at
    ``<root>/profiles/<name>``. From that point ``get_config_path()`` — and so
    ``load_agent_runtime_config()`` with no argument — silently resolves against
    THAT profile's ``config.yaml``, letting whichever profile is sticky-active
    shadow harness-global operator policy (live proof 2026-07-23: with ``alice``
    active, the mission-chat lane resolved ``chat_lane_restore_toolsets`` off
    ``profiles/alice/config.yaml``, so the root-config ruling was dead on
    arrival — see :func:`harness_root_config_path`).

    Policy that is a property of the harness as a whole — not of any one
    profile — loads through this instead of the bare
    ``load_agent_runtime_config()``. Per-profile facts (``personas``, the
    ``default_model`` / ``default_provider`` / ``default_api_mode`` resolution,
    skills) must NOT use this; they stay on ``load_agent_runtime_config()`` so
    the active profile's own overrides apply.
    """

    return load_agent_runtime_config(harness_root_config_path())


def chat_lane_restore_toolsets(persona_id: str, cfg: AgentRuntimeConfig | None = None) -> list[str]:
    """Per-persona operator override for the chat-lane toolset cost policy.

    Read from ``agent_runtime.personas.<id>.chat_lane_restore_toolsets`` in
    the ROOT ``config.yaml`` (see :func:`harness_root_config_path` — this is
    harness-global operator policy, and an active profile's own config must
    not shadow it): a list of toolsets to RESTORE onto that persona's
    operator / mission chat lane after the default policy
    (``chat_lane_toolsets.DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS``) would exclude
    them (browser / vision / heavy-dev). Restore is un-exclusion, not a grant —
    a restored toolset is only kept if the persona's role/permission layer
    already resolved it into the lane.

    Honors the legacy ``alice_supervisor`` ⇄ ``neko_supervisor`` alias so an
    older config keyed on either name is respected. Absent / malformed → ``[]``
    (the default policy applies unchanged)."""

    persona_id = str(persona_id or "").strip()
    if not persona_id:
        return []
    cfg = cfg or load_agent_runtime_config(harness_root_config_path())
    personas = cfg.personas if isinstance(getattr(cfg, "personas", None), dict) else {}
    keys = [persona_id]
    if persona_id == "neko_supervisor":
        keys.append("alice_supervisor")
    elif persona_id == "alice_supervisor":
        keys.append("neko_supervisor")
    for key in keys:
        raw = personas.get(key)
        if isinstance(raw, dict) and "chat_lane_restore_toolsets" in raw:
            return _string_list(raw.get("chat_lane_restore_toolsets"))
    return []


def _expand_machine_root_tokens(value, *, field: str):
    """Expand ``${roots.…}`` in a persona path field at config-load time.

    Unresolvable tokens are left LITERAL on purpose: substituting a guess would
    hand a persona a fabricated workdir, and blanking the field would look like
    "no repo scope configured". The literal token is the signal
    ``profile_readiness`` turns into a typed ``mcp_attention`` row with the
    exact `hermes harness roots set …` fix, and it can never be mistaken for a
    real path. Values with no token are returned unchanged.
    """

    from .machine_roots import MachineRootError, contains_path_tokens, expand_config_paths

    if not contains_path_tokens(value):
        return value
    try:
        return expand_config_paths(value, field=field)
    except MachineRootError as exc:
        logger.error("Persona path field %s is unresolved: %s | fix: %s", field, exc.summary, exc.fix_hint)
        return value


def persona_records_from_config(cfg: AgentRuntimeConfig | None = None):
    cfg = cfg or load_agent_runtime_config()
    # Full resolvable catalog: the typed pipeline personas remain here (dormant, so the
    # mothballed pipeline can still resolve dev/qa), plus any profile-backed agents declared
    # in config. Only ``seed_personas()`` (base) is actually seeded into the running store.
    personas = {p.id: p for p in default_personas()}
    head_profile = _head_agent_profile(cfg)
    for p in personas.values():
        p.provider = p.provider or cfg.default_provider
        p.model = p.model or cfg.default_model
        p.api_mode = p.api_mode or cfg.default_api_mode
    for pid, overrides in cfg.personas.items():
        persona_id = "neko_supervisor" if pid == "alice_supervisor" else pid
        if persona_id not in personas:
            default_role = AgentRole.PM.value if persona_id == AgentRole.PM.value else AgentRole.DEV.value
            role = str(overrides.get("role", default_role))
            # Typed roles resolve as before (dormant catalog); profile-backed agents
            # (role: profile) may also be declared for the base-profile world.
            if role not in {item.value for item in AgentRole} and role != PROFILE_ROLE_SENTINEL:
                continue
            personas[persona_id] = _persona_from_overrides(persona_id, role, overrides, cfg)
        p = personas[persona_id]
        if "role" in overrides:
            p.role = str(overrides.get("role") or p.role)
        p.display_name = str(overrides.get("display_name", p.display_name))
        p.provider = overrides.get("provider", p.provider)
        p.model = overrides.get("model", p.model)
        p.api_mode = overrides.get("api_mode", p.api_mode)
        p.autonomy = str(overrides.get("autonomy", p.autonomy))
        p.hermes_profile = overrides.get("hermes_profile", p.hermes_profile)
        p.soul_overlay_path = overrides.get("soul_overlay_path", p.soul_overlay_path)
        p.include_profile_memory = bool(overrides.get("include_profile_memory", p.include_profile_memory))
        p.include_core_context_files = bool(overrides.get("include_core_context_files", p.include_core_context_files))
        p.repo_scope = _expand_machine_root_tokens(
            overrides.get("repo_scope", p.repo_scope),
            field=f"agent_runtime.personas.{persona_id}.repo_scope",
        )
        p.repo_scope_label = overrides.get("repo_scope_label", p.repo_scope_label)
        p.iteration_budget = _optional_int(overrides.get("iteration_budget", p.iteration_budget))
        p.max_wall_seconds = _optional_float(overrides.get("max_wall_seconds", p.max_wall_seconds))
        p.max_api_calls = _optional_int(overrides.get("max_api_calls", p.max_api_calls))
        p.max_total_tokens = _optional_int(overrides.get("max_total_tokens", p.max_total_tokens))
        if "skills" in overrides or "skills_remove" in overrides:
            additions = _string_list(overrides.get("skills", []))
            removals = set(_string_list(overrides.get("skills_remove", [])))
            # Persona defaults are required/recommended assignments. Config
            # extends that baseline by id; subtraction is explicit so adding
            # one skill never accidentally erases every default.
            merged = list(dict.fromkeys([*p.skills, *additions]))
            p.skills = [skill_id for skill_id in merged if skill_id not in removals]
        if "required_mcp_servers" in overrides:
            p.required_mcp_servers = _string_list(overrides["required_mcp_servers"])
        if "toolsets" in overrides:
            p.toolsets = validate_toolsets(coerce_agent_role(p.role), list(overrides["toolsets"]))
    supervisor = personas.get("neko_supervisor")
    if supervisor is not None:
        explicit_supervisor = _explicit_supervisor_profile_override(cfg)
        supervisor.hermes_profile = _effective_supervisor_profile(
            supervisor.hermes_profile,
            head_profile,
            fallback_legacy_alice=not explicit_supervisor or bool(str(getattr(cfg, "head_agent_profile", "") or "").strip()),
        )
    return list(personas.values())


def ensure_persisted_personas(cfg: AgentRuntimeConfig | None = None):
    """Materialize configured/default personas, then return persisted personas.

    Stage 10 retires runtime fallback paths that silently use config defaults when
    the store is empty. This helper is the bootstrap boundary: callers that need
    runnable personas persist the resolved persona records first, then operate on
    the store as the source of truth.
    """
    from .store import AgentStore

    cfg = cfg or load_agent_runtime_config()
    store = AgentStore()
    stored = {persona.id: persona for persona in store.list_all()}
    # Base-profile foundation: seed ONLY the base profile for a generic Hermes
    # installation. Launcher installations explicitly provision the bundled typed
    # team; once that team exists, later bootstrap calls must not add a fifth `base`
    # persona on top of it.
    has_bundled_team = bool(BUNDLED_PERSONA_IDS.intersection(stored))
    seed = {} if has_bundled_team else {persona.id: persona for persona in seed_personas()}
    changed = False
    for persona_id, persona in seed.items():
        if persona_id not in stored:
            stored[persona_id] = store.save(persona)
            changed = True
        elif persona_id in DEFAULT_PERSONA_IDS:
            current = stored[persona_id]
            if (
                getattr(current, "hermes_profile", None) != persona.hermes_profile
                or list(getattr(current, "toolsets", []) or []) != list(getattr(persona, "toolsets", []) or [])
            ):
                current.hermes_profile = persona.hermes_profile
                current.toolsets = list(getattr(persona, "toolsets", []) or [])
                stored[persona_id] = store.save(current)
                changed = True
    if changed:
        stored = {persona.id: persona for persona in store.list_all()}
    # Return the seeded store PLUS the dormant resolvable catalog: only base is
    # seeded/shown, but every persona resolver (get_persisted_persona, blueprint slot
    # binding, CLI persona lookup) must still find the typed pipeline personas so the
    # mothballed pipeline keeps functioning. Seeded (base) wins on id collision.
    catalog = {persona.id: persona for persona in persona_records_from_config(cfg)}
    merged = {**catalog, **stored}
    return list(merged.values())


def provision_bundled_personas(cfg: AgentRuntimeConfig | None = None):
    """Persist the Launcher's typed default team after a profile preflight.

    The generic Hermes bootstrap remains base-only. The Launcher explicitly
    opts into this team during installation, after creating every required
    profile. Preflighting all bindings before the first store write prevents
    Mission Control from advertising a half-runnable backend/frontend team.
    Existing custom bindings are preserved when they still point at a real
    profile; missing/unbacked legacy rows are repaired to the bundled binding.
    """
    from .store import AgentStore

    cfg = cfg or load_agent_runtime_config()
    resolved = {persona.id: persona for persona in persona_records_from_config(cfg)}
    missing_definitions = sorted(BUNDLED_PERSONA_IDS.difference(resolved))
    if missing_definitions:
        raise ValueError(
            "bundled persona definitions are missing: "
            + ", ".join(missing_definitions)
        )

    # This is the Launcher's explicit bundled-team contract. In particular,
    # generic Hermes may resolve Neko to a configured head profile, while the
    # Launcher installer always creates and binds the bundled Neko to `base`.
    for persona_id, profile in BUNDLED_PERSONA_PROFILES.items():
        resolved[persona_id].hermes_profile = profile

    missing_profiles = sorted(
        persona_id
        for persona_id in BUNDLED_PERSONA_IDS
        if not str(resolved[persona_id].hermes_profile or "").strip()
        or not profile_exists(str(resolved[persona_id].hermes_profile))
    )
    if missing_profiles:
        raise ValueError(
            "bundled persona profiles are missing: " + ", ".join(missing_profiles)
        )

    store = AgentStore()
    stored = {persona.id: persona for persona in store.list_all()}
    provisioned = []
    for persona_id in sorted(BUNDLED_PERSONA_IDS):
        desired = resolved[persona_id]
        current = stored.get(persona_id)
        if current is None:
            current = store.save(desired)
        else:
            current_profile = str(current.hermes_profile or "").strip()
            if not current_profile or not profile_exists(current_profile):
                current.hermes_profile = desired.hermes_profile
                current = store.save(current)
        provisioned.append(current)
    return provisioned


def get_persisted_persona(persona_id: str, cfg: AgentRuntimeConfig | None = None):
    persona_id = str(persona_id or "").strip()
    for persona in ensure_persisted_personas(cfg):
        if persona.id == persona_id:
            return persona
    raise StopIteration(persona_id)


def _head_agent_profile(cfg: AgentRuntimeConfig) -> str:
    configured = str(getattr(cfg, "head_agent_profile", None) or "").strip()
    return configured or active_profile_name()


def _explicit_supervisor_profile_override(cfg: AgentRuntimeConfig) -> bool:
    for key in ("neko_supervisor", "alice_supervisor"):
        raw = cfg.personas.get(key) if isinstance(getattr(cfg, "personas", None), dict) else None
        if isinstance(raw, dict) and "hermes_profile" in raw:
            return True
    return False


def _effective_supervisor_profile(configured_profile: str | None, head_profile: str, *, fallback_legacy_alice: bool = True) -> str:
    """Resolve Neko/head supervisor profile without hard-coding Alice.

    Existing Tony configs may still say ``alice`` from the Windows runtime.  If
    that profile exists, preserve it.  If it is absent on the current host, use
    the active/configured head-agent profile instead so Mission Control can be
    driven by alice-mac, pm, a future Neko profile, or any other head agent.
    """
    value = str(configured_profile or "").strip()
    if not value:
        return head_profile
    if fallback_legacy_alice and value == "alice" and not profile_exists("alice"):
        return head_profile
    return value


def _persona_from_overrides(persona_id: str, role: str, overrides: dict[str, Any], cfg: AgentRuntimeConfig):
    from .models import AgentPersona

    return AgentPersona(
        id=persona_id,
        display_name=str(overrides.get("display_name") or persona_id.replace("_", " ").title()),
        role=role,
        model=overrides.get("model") or cfg.default_model,
        provider=overrides.get("provider") or cfg.default_provider,
        api_mode=overrides.get("api_mode") or cfg.default_api_mode,
        toolsets=validate_toolsets(coerce_agent_role(role), list(overrides.get("toolsets") or [])),
        system_prompt_path=str(overrides.get("system_prompt_path") or f"agent_runtime/prompts/{role}.md"),
        include_core_context_files=bool(overrides.get("include_core_context_files", False)),
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                decoded = yaml.safe_load(text)
            except yaml.YAMLError:
                decoded = None
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        return [text]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _continuous_role_sessions_config(raw: dict[str, Any]) -> ContinuousRoleSessionConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = ContinuousRoleSessionConfig()
    return ContinuousRoleSessionConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        observe_only=bool(raw.get("observe_only", defaults.observe_only)),
        max_decisions_per_envelope=_positive_int(raw.get("max_decisions_per_envelope"), defaults.max_decisions_per_envelope),
        max_proofs_per_envelope=_positive_int(raw.get("max_proofs_per_envelope"), defaults.max_proofs_per_envelope),
        max_continuations_per_stage=_positive_int(raw.get("max_continuations_per_stage"), defaults.max_continuations_per_stage),
        continue_after_passing_proof=bool(raw.get("continue_after_passing_proof", defaults.continue_after_passing_proof)),
        continue_after_failed_proof=bool(raw.get("continue_after_failed_proof", defaults.continue_after_failed_proof)),
        close_on_state_owner_change=bool(raw.get("close_on_state_owner_change", defaults.close_on_state_owner_change)),
        close_on_open_incident=bool(raw.get("close_on_open_incident", defaults.close_on_open_incident)),
        close_on_invalid_output=bool(raw.get("close_on_invalid_output", defaults.close_on_invalid_output)),
        close_on_budget_warning=bool(raw.get("close_on_budget_warning", defaults.close_on_budget_warning)),
    )


def _enterprise_worker_sessions_config(raw: dict[str, Any]) -> EnterpriseWorkerSessionsConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = EnterpriseWorkerSessionsConfig()
    mode = str(raw.get("mode", defaults.mode) or defaults.mode).strip() or defaults.mode
    if mode not in {"observe_only", "enforce"}:
        mode = defaults.mode
    strategy = str(raw.get("static_prompt_strategy", defaults.static_prompt_strategy) or defaults.static_prompt_strategy).strip()
    if strategy not in {"capability_detect", "always_send", "receipt_only"}:
        strategy = defaults.static_prompt_strategy
    return EnterpriseWorkerSessionsConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        mode=mode,
        worker_session_store=bool(raw.get("worker_session_store", defaults.worker_session_store)),
        persona_instance_runtime=bool(raw.get("persona_instance_runtime", defaults.persona_instance_runtime)),
        persona_assignment_store=bool(raw.get("persona_assignment_store", defaults.persona_assignment_store)),
        same_session_continuation=bool(raw.get("same_session_continuation", defaults.same_session_continuation)),
        harness_owned_proof_recipes=bool(raw.get("harness_owned_proof_recipes", defaults.harness_owned_proof_recipes)),
        no_edit_certification_sandbox=bool(raw.get("no_edit_certification_sandbox", defaults.no_edit_certification_sandbox)),
        possession_controls=bool(raw.get("possession_controls", defaults.possession_controls)),
        static_prompt_strategy=strategy,
        worker_heartbeat_seconds=_positive_int(raw.get("worker_heartbeat_seconds"), defaults.worker_heartbeat_seconds),
        worker_stale_seconds=_positive_int(raw.get("worker_stale_seconds"), defaults.worker_stale_seconds),
        possession_lease_seconds=_positive_int(raw.get("possession_lease_seconds"), defaults.possession_lease_seconds),
        max_same_worker_repairs_per_stage=_positive_int(raw.get("max_same_worker_repairs_per_stage"), defaults.max_same_worker_repairs_per_stage),
        max_worker_context_compressions_per_goal=_positive_int(raw.get("max_worker_context_compressions_per_goal"), defaults.max_worker_context_compressions_per_goal),
    )


def _normal_worker_flow_config(raw: dict[str, Any]) -> NormalWorkerFlowConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = NormalWorkerFlowConfig()
    return NormalWorkerFlowConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        dev_self_tests_in_session=bool(raw.get("dev_self_tests_in_session", defaults.dev_self_tests_in_session)),
        hide_request_test_run_until_gate=bool(raw.get("hide_request_test_run_until_gate", defaults.hide_request_test_run_until_gate)),
        self_test_evidence_capture=bool(raw.get("self_test_evidence_capture", defaults.self_test_evidence_capture)),
        max_self_test_repeats_without_change=_positive_int(raw.get("max_self_test_repeats_without_change"), defaults.max_self_test_repeats_without_change),
        expose_worker_actions_in_contract_dump=bool(raw.get("expose_worker_actions_in_contract_dump", defaults.expose_worker_actions_in_contract_dump)),
    )




def _role_envelope_config(raw: dict[str, Any]) -> RoleEnvelopeConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = RoleEnvelopeConfig()
    return RoleEnvelopeConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        prefer_same_session=bool(raw.get("prefer_same_session", defaults.prefer_same_session)),
        checklist_hud_enabled=bool(raw.get("checklist_hud_enabled", defaults.checklist_hud_enabled)),
        self_approval_enabled=bool(raw.get("self_approval_enabled", defaults.self_approval_enabled)),
        qa_final_approval_required=bool(raw.get("qa_final_approval_required", defaults.qa_final_approval_required)),
        max_same_session_continuations=_positive_int(raw.get("max_same_session_continuations"), defaults.max_same_session_continuations),
        max_no_progress_repeats=_positive_int(raw.get("max_no_progress_repeats"), defaults.max_no_progress_repeats),
        max_fix_envelopes_per_stage=_positive_int(raw.get("max_fix_envelopes_per_stage"), defaults.max_fix_envelopes_per_stage),
        max_checklist_items_rendered=_positive_int(raw.get("max_checklist_items_rendered"), defaults.max_checklist_items_rendered),
        max_foreign_checklist_summaries=_positive_int(raw.get("max_foreign_checklist_summaries"), defaults.max_foreign_checklist_summaries),
        enable_legacy_stage_projection=bool(raw.get("enable_legacy_stage_projection", defaults.enable_legacy_stage_projection)),
    )


def _repo_bundle_routing_config(raw: dict[str, Any]) -> RepoBundleRoutingConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = RepoBundleRoutingConfig()
    return RepoBundleRoutingConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        strict_repo_ownership=bool(raw.get("strict_repo_ownership", defaults.strict_repo_ownership)),
        queue_on_dependency_bundles=bool(raw.get("queue_on_dependency_bundles", defaults.queue_on_dependency_bundles)),
        expose_in_snapshot=bool(raw.get("expose_in_snapshot", defaults.expose_in_snapshot)),
    )


def _simplified_agent_contract_config(raw: dict[str, Any]) -> SimplifiedAgentContractConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = SimplifiedAgentContractConfig()
    return SimplifiedAgentContractConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        expose_only_simplified_actions=bool(raw.get("expose_only_simplified_actions", defaults.expose_only_simplified_actions)),
        keep_internal_state_machine=bool(raw.get("keep_internal_state_machine", defaults.keep_internal_state_machine)),
        terminal_feedback_enabled=bool(raw.get("terminal_feedback_enabled", defaults.terminal_feedback_enabled)),
    )


def _read_model_config(raw: dict[str, Any]) -> ReadModelConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = ReadModelConfig()
    filename = str(raw.get("db_filename", defaults.db_filename) or defaults.db_filename).strip()
    if not filename or "/" in filename or "\\" in filename:
        filename = defaults.db_filename
    return ReadModelConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        serve_snapshot_from_db=bool(raw.get("serve_snapshot_from_db", defaults.serve_snapshot_from_db)),
        db_filename=filename,
        delta_patches=bool(raw.get("delta_patches", defaults.delta_patches)),
    )


def _persona_chat_config(raw: dict[str, Any]) -> PersonaChatConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = PersonaChatConfig()
    return PersonaChatConfig(
        hot_sessions_enabled=bool(
            raw.get("hot_sessions_enabled", defaults.hot_sessions_enabled)
        ),
        max_hot_sessions=_clamped_positive_int(
            raw.get("max_hot_sessions"),
            defaults.max_hot_sessions,
            minimum=1,
            maximum=64,
        ),
        idle_ttl_seconds=_clamped_positive_int(
            raw.get("idle_ttl_seconds"),
            defaults.idle_ttl_seconds,
            minimum=30,
            maximum=86_400,
        ),
    )


def _mission_chat_config(raw: dict[str, Any]) -> MissionChatConfig:
    """Parse ``agent_runtime.mission_chat`` (see :class:`MissionChatConfig`).

    An absent / malformed value keeps the historical 240 s CLI default, and a
    present one is clamped to a window that can actually host a turn — the
    checkpoint reserve eats the whole budget below ~30 s, and above a day a
    single turn outlives the mission deadline. Clamping (rather than rejecting)
    keeps a fat-fingered stanza from failing every turn on the lane."""

    raw = raw if isinstance(raw, dict) else {}
    defaults = MissionChatConfig()
    return MissionChatConfig(
        default_max_seconds=_clamped_positive_float(
            raw.get("default_max_seconds"),
            defaults.default_max_seconds,
            minimum=MISSION_CHAT_MIN_MAX_SECONDS,
            maximum=MISSION_CHAT_MAX_MAX_SECONDS,
        ),
        dispatch_session_policy=normalize_dispatch_session_policy(
            raw.get("dispatch_session_policy"),
            defaults.dispatch_session_policy,
        ),
        clarify_token_binding=bool(
            raw.get("clarify_token_binding", defaults.clarify_token_binding)
        ),
    )


def mission_chat_clarify_token_binding(cfg: AgentRuntimeConfig | None = None) -> bool:
    """Whether a clarify answer is bound to its question's thread by token.

    Mirrors :func:`mission_chat_dispatch_session_policy` exactly, and for the
    same reason: this decides how EVERY profile's clarify round-trips thread, so
    it is harness-wide operator policy read from the ROOT config, and a config
    fault degrades to the built-in default (``True``) rather than failing the
    turn. The gate is checked at the two seams that matter — minting a ticket
    when a turn asks a question, and resolving an echoed token when a turn
    answers one — so flipping it off returns the lane to today's precedence with
    no migration and nothing to unwind."""

    if cfg is not None:
        return bool(
            getattr(cfg.mission_chat, "clarify_token_binding", MissionChatConfig().clarify_token_binding)
        )
    try:
        return bool(load_root_runtime_config().mission_chat.clarify_token_binding)
    except Exception:  # pragma: no cover - defensive; a config fault must not kill a turn
        logger.debug("mission_chat clarify-token gate load failed; using the built-in default", exc_info=True)
        return MissionChatConfig().clarify_token_binding


def mission_chat_dispatch_session_policy(cfg: AgentRuntimeConfig | None = None) -> str:
    """Which thread a dispatch lands in when the caller names none.

    Harness-wide operator policy, so it loads through
    :func:`load_root_runtime_config` for the same reason the budget default
    does — a sticky-active profile's own ``config.yaml`` must not be able to
    change how every OTHER profile's dispatches thread. A config fault degrades
    to the built-in default rather than failing the turn.

    The precedence rule (explicit ``session_id`` / ``new_session`` always wins
    over this) is decided in one place:
    :func:`agent_runtime.dispatch_session_policy.resolve_dispatch_session_decision`."""

    if cfg is not None:
        return normalize_dispatch_session_policy(
            getattr(cfg.mission_chat, "dispatch_session_policy", None),
            MissionChatConfig().dispatch_session_policy,
        )
    try:
        return normalize_dispatch_session_policy(
            load_root_runtime_config().mission_chat.dispatch_session_policy,
            MissionChatConfig().dispatch_session_policy,
        )
    except Exception:  # pragma: no cover - defensive; a config fault must not kill a turn
        logger.debug("mission_chat dispatch policy load failed; using the built-in default", exc_info=True)
        return MissionChatConfig().dispatch_session_policy


def mission_chat_default_max_seconds(cfg: AgentRuntimeConfig | None = None) -> float:
    """The wall budget a mission-chat turn gets when ``--max-seconds`` is absent.

    Harness-wide operator policy, so it loads through
    :func:`load_root_runtime_config` — a sticky-active profile's own
    ``config.yaml`` must not be able to shorten (or extend) every other
    profile's turns (the same shadowing bug :func:`harness_root_config_path`
    documents for ``chat_lane_restore_toolsets``). A config fault degrades to
    the historical default rather than failing the turn."""

    if cfg is not None:
        return float(getattr(cfg.mission_chat, "default_max_seconds", MissionChatConfig().default_max_seconds))
    try:
        return float(load_root_runtime_config().mission_chat.default_max_seconds)
    except Exception:  # pragma: no cover - defensive; a config fault must not kill a turn
        logger.debug("mission_chat default budget load failed; using the built-in default", exc_info=True)
        return MissionChatConfig().default_max_seconds


def resolve_mission_chat_max_seconds(
    requested: float | None, cfg: AgentRuntimeConfig | None = None
) -> float:
    """The wall budget one mission-chat turn gets — the precedence chokepoint.

    An explicit request (the CLI's ``--max-seconds``, a relay hop's chosen
    window) ALWAYS wins, including a value outside the config clamp: the clamp
    guards a deployment-wide default from a fat-fingered stanza, it is not a cap
    on a caller who states a number. Only ``None`` — "no opinion" — falls through
    to :func:`mission_chat_default_max_seconds`.

    One function so the precedence is decided in one place; the parser default is
    ``None`` precisely so "absent" and "explicitly 240" remain distinguishable.
    """

    if requested is None:
        return mission_chat_default_max_seconds(cfg)
    try:
        value = float(requested)
    except (TypeError, ValueError):
        return mission_chat_default_max_seconds(cfg)
    return value if value > 0 else mission_chat_default_max_seconds(cfg)


def mission_chat_workdir(persona_id: str, cfg: AgentRuntimeConfig | None = None) -> str | None:
    """Per-persona repo grounding for the mission-chat lane.

    Read from ``agent_runtime.personas.<id>.workdir`` in the ROOT
    ``config.yaml`` (see :func:`harness_root_config_path`): an absolute
    directory the persona's chat turns run in, so its ``terminal`` / ``file``
    tools resolve relative paths against a real repo instead of whatever cwd the
    serve process happens to hold (G6). ``${roots.…}`` machine tokens are
    expanded here, exactly like ``repo_scope``, so the stanza stays portable
    across machines; an unresolvable token is left literal and surfaces as a
    typed ``mission_chat_workdir_unresolved`` row rather than a fabricated path.

    Honors the legacy ``alice_supervisor`` ⇄ ``neko_supervisor`` alias.
    Absent / blank → ``None`` (the lane keeps the process cwd, unchanged).
    Resolution of the whole ladder (config → workspace pointer → repo_scope →
    cwd) lives in :mod:`agent_runtime.mission_chat_workdir`; this only reads the
    key."""

    persona_id = str(persona_id or "").strip()
    if not persona_id:
        return None
    cfg = cfg or load_agent_runtime_config(harness_root_config_path())
    personas = cfg.personas if isinstance(getattr(cfg, "personas", None), dict) else {}
    keys = [persona_id]
    if persona_id == "neko_supervisor":
        keys.append("alice_supervisor")
    elif persona_id == "alice_supervisor":
        keys.append("neko_supervisor")
    for key in keys:
        raw = personas.get(key)
        if isinstance(raw, dict) and "workdir" in raw:
            value = _clean_config_str(raw.get("workdir"))
            if value is None:
                return None
            return _expand_machine_root_tokens(
                value, field=f"agent_runtime.personas.{key}.workdir"
            )
    return None


def _event_log_config(raw: dict[str, Any]) -> EventLogConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = EventLogConfig()
    cap = raw.get("rotation_cap_bytes", defaults.rotation_cap_bytes)
    try:
        cap_int = int(cap)
    except (TypeError, ValueError):
        cap_int = defaults.rotation_cap_bytes
    # Negative is meaningless; clamp to 0 (rotation disabled). 0 is a valid
    # explicit "never rotate" (legacy unbounded live file).
    if cap_int < 0:
        cap_int = 0
    return EventLogConfig(rotation_cap_bytes=cap_int)


def _swarm_config(raw: dict[str, Any]) -> SwarmConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = SwarmConfig()
    return SwarmConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        requires_certification=bool(raw.get("requires_certification", defaults.requires_certification)),
        allow_uncertified_dev_swarm=bool(raw.get("allow_uncertified_dev_swarm", defaults.allow_uncertified_dev_swarm)),
        max_active_lanes=_positive_int(raw.get("max_active_lanes"), defaults.max_active_lanes),
        global_token_soft_limit=_positive_int(raw.get("global_token_soft_limit"), defaults.global_token_soft_limit),
        global_token_hard_limit=_positive_int(raw.get("global_token_hard_limit"), defaults.global_token_hard_limit),
        global_api_call_soft_limit=_positive_int(raw.get("global_api_call_soft_limit"), defaults.global_api_call_soft_limit),
        global_api_call_hard_limit=_positive_int(raw.get("global_api_call_hard_limit"), defaults.global_api_call_hard_limit),
        per_lane_token_limit=_positive_int(raw.get("per_lane_token_limit"), defaults.per_lane_token_limit),
        per_lane_api_call_limit=_positive_int(raw.get("per_lane_api_call_limit"), defaults.per_lane_api_call_limit),
    )


def _supervision_config(raw: dict[str, Any]) -> SupervisionConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = SupervisionConfig()
    return SupervisionConfig(
        child_events_enabled=bool(raw.get("child_events_enabled", defaults.child_events_enabled)),
        recursive_enabled=bool(raw.get("recursive_enabled", defaults.recursive_enabled)),
        hierarchical_budget_enabled=bool(raw.get("hierarchical_budget_enabled", defaults.hierarchical_budget_enabled)),
        deploy_verification_enabled=bool(raw.get("deploy_verification_enabled", defaults.deploy_verification_enabled)),
    )


def _coordinator_permission_config(raw: dict[str, Any]) -> CoordinatorPermissionConfig:
    return CoordinatorPermissionConfig(
        max_spawns=max(0, int(raw.get("max_spawns", 0))),
        may_kill_own=bool(raw.get("may_kill_own", True)),
        may_kill_others=bool(raw.get("may_kill_others", False)),
    )


def _mcp_admission_config(raw: dict[str, Any]) -> McpAdmissionConfig:
    """Parse ``agent_runtime.mcp_admission`` — deny-by-default at every step.

    A malformed block must never read as "allow": a non-mapping ``roles``, a
    non-mapping lane map, or a non-list server list all collapse to the empty
    allowlist, and any parse fault leaves ``enabled`` False. The connect budget
    is clamped so a config typo cannot park a chat turn behind a capability
    probe (or make the probe useless by rounding to zero).

    ``max_tool_calls_per_run`` is clamped the same way and for the same reason,
    with one extra property: there is no way to spell "unlimited". A missing,
    zero, negative or unparseable value falls back to the default, and the upper
    clamp refuses a fat-fingered ``1000000`` — an admitted MCP surface with no
    call bound is precisely the failure the budget exists to prevent, so it must
    not be reachable by a config typo either.
    """

    raw = raw if isinstance(raw, dict) else {}
    defaults = McpAdmissionConfig()
    roles: dict[str, dict[str, list[str]]] = {}
    raw_roles = raw.get("roles")
    if isinstance(raw_roles, dict):
        for role, lanes in raw_roles.items():
            if not isinstance(lanes, dict):
                continue
            parsed_lanes = {
                str(lane): _string_list(servers)
                for lane, servers in lanes.items()
                if _string_list(servers)
            }
            if parsed_lanes:
                roles[str(role)] = parsed_lanes
    timeout = _optional_float(raw.get("connect_timeout_seconds"))
    if timeout is None or timeout <= 0:
        timeout = defaults.connect_timeout_seconds
    return McpAdmissionConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        connect_timeout_seconds=min(120.0, max(1.0, float(timeout))),
        max_tool_calls_per_run=_clamped_positive_int(
            raw.get("max_tool_calls_per_run"),
            defaults.max_tool_calls_per_run,
            minimum=1,
            maximum=MCP_ADMISSION_MAX_TOOL_CALLS_CEILING,
        ),
        roles=roles,
    )


def _terminal_envelope_config(raw: dict[str, Any]) -> TerminalEnvelopeConfig:
    """Parse ``agent_runtime.terminal_envelope`` — deny-by-default at every step.

    Structural parsing only: this keeps well-shaped ``<role>.<lane>: [classes]``
    entries and drops anything else. Whether a *named* class is a real class,
    and whether it is grantable at all, is decided by
    ``terminal_envelope.resolve_terminal_envelope_grants`` so an unknown or
    non-grantable class produces a TYPED config issue at decision time instead
    of vanishing silently here. A malformed block must never read as "allow": a
    non-mapping ``grants`` or a non-mapping lane map collapses to the empty
    grant table, and a non-list class list is carried through verbatim so the
    resolver can report the shape fault rather than leave the operator staring
    at a stanza that appears to be in force.
    """

    raw = raw if isinstance(raw, dict) else {}
    grants: dict[str, dict[str, Any]] = {}
    raw_grants = raw.get("grants")
    if isinstance(raw_grants, dict):
        for role, lanes in raw_grants.items():
            if not isinstance(lanes, dict):
                continue
            parsed_lanes: dict[str, Any] = {}
            for lane, classes in lanes.items():
                if not isinstance(classes, (list, tuple, set, frozenset)):
                    parsed_lanes[str(lane)] = classes
                    continue
                names = _string_list(classes)
                if names:
                    parsed_lanes[str(lane)] = names
            if parsed_lanes:
                grants[str(role)] = parsed_lanes
    return TerminalEnvelopeConfig(grants=grants)


def _apply_enterprise_role_session_compat(
    config: ContinuousRoleSessionConfig,
    *,
    enterprise_worker_sessions: EnterpriseWorkerSessionsConfig,
    raw_continuous: Any,
) -> ContinuousRoleSessionConfig:
    if not enterprise_worker_sessions.enabled or not enterprise_worker_sessions.same_session_continuation:
        return config
    if isinstance(raw_continuous, dict) and ("enabled" in raw_continuous or "observe_only" in raw_continuous):
        return config
    return ContinuousRoleSessionConfig(
        enabled=enterprise_worker_sessions.mode == "enforce",
        observe_only=enterprise_worker_sessions.mode != "enforce",
        max_decisions_per_envelope=config.max_decisions_per_envelope,
        max_proofs_per_envelope=config.max_proofs_per_envelope,
        max_continuations_per_stage=config.max_continuations_per_stage,
        continue_after_passing_proof=config.continue_after_passing_proof,
        continue_after_failed_proof=config.continue_after_failed_proof,
        close_on_state_owner_change=config.close_on_state_owner_change,
        close_on_open_incident=config.close_on_open_incident,
        close_on_invalid_output=config.close_on_invalid_output,
        close_on_budget_warning=config.close_on_budget_warning,
    )


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _clamped_positive_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    number = _positive_int(value, default)
    return max(minimum, min(maximum, number))


def _clamped_positive_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    number = _optional_float(value)
    if number is None:
        number = default
    return max(minimum, min(maximum, float(number)))




def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
