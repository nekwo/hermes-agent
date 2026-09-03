from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_config_path
from .dispatch_session_policy import normalize_dispatch_session_policy
from .personas import PROFILE_ROLE_SENTINEL, validate_toolsets
from .redaction_mode import normalize_redaction_mode
from .permission_modes import (
    FALLBACK_DEFAULT_PERMISSION_MODE,
    SHIPPED_DEFAULT_PERMISSION_MODE,
    SUPPORTED_PERMISSION_MODES,
    normalize_permission_mode,
)
from .runtime_config import CoordinatorPermissionConfig, EventLogConfig, McpAdmissionConfig, MissionChatConfig, PersonaChatConfig, ReadModelConfig, RuntimeConfig, SupervisionConfig, TerminalEnvelopeConfig, ToolPermissionConfig

logger = logging.getLogger(__name__)

#: Bounds for ``agent_runtime.mission_chat.default_max_seconds``. Below the
#: floor the graceful checkpoint reserve (``turn_budget``: ``max(60s, 15%)``,
#: capped so 30 s of work survives) consumes the whole window and the turn can
#: run no tool at all; above the ceiling one conversational turn outlives a day.
#: (The ceiling was originally derived from ``mission_wall_clock_deadline_seconds``,
#: itself 86400; S57 removed that field as reader-less, so 86400 is now this
#: constant's own value rather than a reference to another knob.)
MISSION_CHAT_MIN_MAX_SECONDS = 30.0
MISSION_CHAT_MAX_MAX_SECONDS = 86_400.0

#: Bounds for ``agent_runtime.mission_chat.compaction_threshold_tokens`` when it
#: is enabled. The floor exists because a cap under ~16 k would compact a chat
#: root before its own turn-1 prefix fits (measured: 22.7 k for a qa turn on
#: 2026-08-09, of which ~9.3 k is tool schema that no compaction can remove), so
#: the lane would summarize on every turn and never converge. The ceiling is the
#: largest window this lane has seen; a cap above it can only be a no-op anyway,
#: because ``_apply_threshold_tokens_cap`` already clamps the cap to the model's
#: context length. Zero is NOT clamped into this window — it is the documented
#: "no lane cap" spelling and is honoured verbatim.
MISSION_CHAT_MIN_COMPACTION_TOKENS = 16_000
MISSION_CHAT_MAX_COMPACTION_TOKENS = 2_000_000

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
    read_model = _read_model_config(raw.get("read_model") or {})
    persona_chat = _persona_chat_config(raw.get("persona_chat") or {})
    event_log = _event_log_config(raw.get("event_log") or {})
    supervision = _supervision_config(raw.get("supervision") or {})
    coordinator_permissions = _coordinator_permission_config(raw.get("coordinator_permissions") or {})
    mission_chat = _mission_chat_config(raw.get("mission_chat") or {})
    mcp_admission = _mcp_admission_config(raw.get("mcp_admission") or {})
    terminal_envelope = _terminal_envelope_config(raw.get("terminal_envelope") or {})
    tool_permissions = _tool_permission_config(raw.get("tool_permissions") or {})
    cfg = AgentRuntimeConfig(
        schema_version=int(raw.get("schema_version", 1)),
        store_root=raw.get("store_root"),
        head_agent_profile=raw.get("head_agent_profile") or raw.get("head_profile"),
        default_provider=resolved_provider,
        default_model=resolved_model,
        default_api_mode=raw.get("default_api_mode", "codex_responses"),
        redaction_mode=normalize_redaction_mode(raw.get("redaction_mode") or os.environ.get("HERMES_REDACTION_MODE", "strict")),
        # S57 removed 29 load lines here — the whole ``daemon_*`` family, the four
        # ``live_run_*`` budgets, the four ``liveness_*`` knobs, the three
        # ``artifact_storage_*`` watermarks, the two mission ceilings, the two
        # neko caps, ``heartbeat_ttl_seconds``, ``max_actions_per_tick``,
        # ``root_node_mode``, ``preferred_goal_execution_mode``,
        # ``scope_wait_deadline_seconds``, ``run_lease_seconds``,
        # ``tool_wait_timeout_seconds``, ``child_progress_min_interval_seconds``
        # and ``deploy_timeout_seconds``. None had a production reader (S56's gate
        # measured it; S57 re-verified each by hand, AST + string form). A yaml
        # that still sets any of them now loads and is IGNORED — ``raw`` is read
        # by ``.get`` per key, so an unknown key is simply never consulted.
        lock_acquire_timeout_seconds=_positive_int(raw.get("lock_acquire_timeout_seconds"), 15),
        read_model=read_model,
        persona_chat=persona_chat,
        event_log=event_log,
        supervision=supervision,
        coordinator_permissions=coordinator_permissions,
        mission_chat=mission_chat,
        mcp_admission=mcp_admission,
        terminal_envelope=terminal_envelope,
        tool_permissions=tool_permissions,
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


#: Historical spellings a persisted persona key may also be known by. Reported
#: ALONGSIDE the persisted key (``persona_id_alias``), never substituted for it:
#: a provenance report that renames what it found is not provenance.
_RUNTIME_DEFAULT_PERSONA_ALIASES: dict[str, str] = {
    "alice_supervisor": "neko_supervisor",
    "neko_supervisor": "alice_supervisor",
}


def describe_runtime_default_authority(config_path: Path | None = None) -> dict[str, Any]:
    """Pure, redaction-safe provenance report comparing the top-level ``model:``
    authority against any ``agent_runtime.*`` override and per-persona pins.

    Single source of truth for the migrations warning and the harness-doctor
    ``model_authority`` block, so the "shadowing vs redundant vs stale pin"
    classification is never re-derived divergently.

    ``persona_pins[].persona_id`` is the key AS PERSISTED. Where that key has a
    historical spelling, ``persona_id_alias`` carries it (``None`` otherwise).
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
            # S66: report what is ACTUALLY IN THE CONFIG. This used to rewrite a
            # pin persisted under ``alice_supervisor`` to ``neko_supervisor``,
            # so an operator reading a PROVENANCE report was told a key their
            # file does not contain — and could not find it by searching for the
            # name the report gave them. The alias is still surfaced, but as a
            # separate, clearly-labelled field rather than by falsifying the
            # first one.
            alias = _RUNTIME_DEFAULT_PERSONA_ALIASES.get(pid)
            persona_pins.append({
                "persona_id": pid,
                "persona_id_alias": alias,
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


#: Keys under ``agent_runtime`` that are ONLY ever read through the ROOT config,
#: paired with the reader that consumes each. ``"*"`` matches any persona id.
#:
#: Setting one of these in a PROFILE ``config.yaml`` is silently inert: the
#: reader resolves :func:`harness_root_config_path` and never looks at the
#: profile, so the operator's value is accepted by YAML, reported back by any
#: profile-aware surface, and ignored by the only code that acts on it.
#:
#: This is a REPEATED defect class, twice in three weeks, in both directions:
#:
#: * 2026-07-23 — the ruling lived in the ROOT and the reader used the profile,
#:   so ``chat_lane_restore_toolsets`` resolved off ``profiles/alice`` and the
#:   operator's root-config ruling was dead on arrival. Fixed by moving the
#:   READER to the root (that is why :func:`harness_root_config_path` exists).
#: * 2026-08-13 — the mirror image. ``read_model.delta_patches: true`` was
#:   written into ``profiles/base`` and ``profiles/alice`` while the reader
#:   correctly used the root, which carried no ``read_model`` block at all. The
#:   S7-A patch producer therefore stayed dark for its whole life: measured
#:   live, ONE field change on ONE persona instance shipped an 822,671-byte
#:   delta carrying an 864,241-byte full snapshot core, where the patch frame
#:   the lane was built for is 486 bytes. ``harness status`` reported
#:   ``delta_patches: true`` throughout, because status reads profile-aware.
#:
#:   2026-08-14 follow-up: a doctor row only helps an operator who RUNS the
#:   doctor, and the misplaced value had a WRITER — the Launcher installer's
#:   ``kMissionControlBaseSeedConfigYaml`` seeds ``delta_patches: true`` into the
#:   fresh ``base`` PROFILE — so every fresh install reproduced it. The lane's
#:   durability therefore does not depend on any file at all any more:
#:   ``read_model.delta_patches`` SHIPS on
#:   (``runtime_config.SHIPPED_DELTA_PATCHES``) and silence resolves to LIVE. A
#:   profile copy is still reported here, because it is still inert and still
#:   worth deleting — it just no longer decides whether the lane runs.
#:
#: Note ``read_model`` WAS split across both loaders and only the leaf was
#: root-only: ``read_model.enabled`` was read profile-aware (``snapshot.py``
#: consulted the passed cfg), while ``read_model.delta_patches`` is root-only.
#: So this list keys on LEAVES, never blocks — a block-level rule would have
#: raised a false positive on every profile that legitimately set ``enabled``.
#:
#: STAGE 6 (2026-08-22) removed the profile-aware half: ``read_model.enabled``
#: has no reader at all now, because the lane it gated is retired. The
#: leaf-keyed shape STAYS — not because ``read_model`` still needs it, but
#: because it is the correct shape for any block whose leaves resolve at
#: different scopes, and re-deriving that after the next such block appears is
#: how the original defect got in. ``delta_patches`` remains the one live leaf
#: and the one root-only row.
ROOT_ONLY_CONFIG_KEYS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("read_model", "delta_patches"), "agent_runtime.state_patches.delta_patches_enabled"),
    (("mcp_admission",), "agent_runtime.mcp_admission.admission_config"),
    (("personas", "*", "chat_lane_restore_toolsets"), "agent_runtime.config.chat_lane_restore_toolsets"),
    (("personas", "*", "workdir"), "agent_runtime.config.mission_chat_workdir"),
)


def _profile_config_paths() -> list[Path]:
    """Every ``profiles/<name>/config.yaml`` under the Hermes root.

    Returns ``[]`` when the profiles directory cannot be listed — the caller
    must treat that as "could not examine", never as "none found".
    """

    from hermes_constants import get_default_hermes_root

    try:
        profiles = get_default_hermes_root() / "profiles"
        return sorted(
            entry / "config.yaml"
            for entry in profiles.iterdir()
            if entry.is_dir() and (entry / "config.yaml").is_file()
        )
    except OSError:
        return []


def _key_present(raw: dict[str, Any], path: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Concrete key paths PRESENT in ``raw`` matching ``path`` (``*`` = any key).

    Presence, not truthiness: a profile that omits ``delta_patches`` parses
    identically to one that sets it ``false``, so a value check could not tell
    an operator's inert instruction from an absent one.
    """

    if not path:
        return [()]
    head, rest = path[0], path[1:]
    if not isinstance(raw, dict):
        return []
    if head == "*":
        found: list[tuple[str, ...]] = []
        for key, value in raw.items():
            for tail in _key_present(value, rest):
                found.append((str(key),) + tail)
        return found
    if head not in raw:
        return []
    return [(head,) + tail for tail in _key_present(raw[head], rest)]


def find_misplaced_root_only_keys() -> list[dict[str, Any]]:
    """Root-only keys that an operator has set in a PROFILE config, where they
    are inert.

    Each row names the profile, the full dotted key, and the reader that will
    never see it — enough to fix without re-deriving the analysis. An empty
    list means "examined and none found"; the caller distinguishes "could not
    examine" by catching the exception this may raise.

    ``set_in_root`` separates the two cases, which are NOT the same defect and
    must not be reported at one severity:

    * ``False`` — the key is set ONLY in a profile, so the operator's value is
      being IGNORED. This is the 2026-08-13 shape and it is actionable.
    * ``True`` — the root also carries it, so the live value is correct and the
      profile copy is a redundant leftover. Reporting this at defect severity
      would leave ``harness doctor`` permanently red for a cosmetic duplicate,
      which is the "a gate that is always red is not a gate" failure.
    """

    from .parse_cache import cached_yaml_file

    root_path = harness_root_config_path()
    root_loaded = cached_yaml_file(root_path, default=None)
    root_raw = (
        (root_loaded or {}).get("agent_runtime") or {} if isinstance(root_loaded, dict) else {}
    )

    rows: list[dict[str, Any]] = []
    for config_path in _profile_config_paths():
        loaded = cached_yaml_file(config_path, default=None)
        raw = (loaded or {}).get("agent_runtime") or {} if isinstance(loaded, dict) else {}
        if not isinstance(raw, dict):
            continue
        for key_path, reader in ROOT_ONLY_CONFIG_KEYS:
            for concrete in _key_present(raw, key_path):
                # Presence of the SAME concrete path in the root, not merely of
                # the pattern: a root that pins ``personas.neko.workdir`` does
                # not make a profile's ``personas.qa.workdir`` effective.
                rows.append(
                    {
                        "profile": config_path.parent.name,
                        "config_path": str(config_path),
                        "key": "agent_runtime." + ".".join(concrete),
                        "read_only_by": reader,
                        "root_config_path": str(root_path),
                        "set_in_root": bool(_key_present(root_raw, concrete)),
                    }
                )
    return rows


def chat_lane_restore_toolsets(persona_id: str, cfg: AgentRuntimeConfig | None = None) -> list[str]:
    """Per-persona operator override for the chat-lane toolset cost policy.

    Read from ``agent_runtime.personas.<id>.chat_lane_restore_toolsets`` in
    the ROOT ``config.yaml`` (see :func:`harness_root_config_path` — this is
    harness-global operator policy, and an active profile's own config must
    not shadow it): a list of toolsets to RESTORE onto that persona's
    operator / mission chat lane after the default policy
    (``chat_lane_toolsets.DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS``) would exclude
    them (browser / vision / heavy-dev). Restore is un-exclusion, not a grant —
    a restored toolset is only kept if the persona's own DECLARED toolsets
    already resolved it into the lane. There is no role backstop behind that:
    S61/S64 made profile/persona declarations the sole capability authority and
    ``personas.validate_toolsets`` carries no role ceiling, so the intersection
    IS the whole guarantee. See ``chat_lane_toolsets``' module docstring.

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
    personas = {}
    for pid, overrides in cfg.personas.items():
        persona_id = str(pid or "").strip()
        if not persona_id or not isinstance(overrides, dict):
            continue
        if persona_id not in personas:
            role = str(overrides.get("role") or PROFILE_ROLE_SENTINEL)
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
            # STILL READ, deliberately (S0a A2): deleting the reader would make a
            # config that carries the key silently identical to one that never
            # did, and the realm-sync body would keep shipping a list nothing in
            # the runtime could even show. What changed is that the field admits
            # nothing — the harness lane reads the PROFILE's declaration
            # (``personas.declared_lane_toolsets``) — so a non-empty list is
            # announced once per load and reported in every projection as
            # ``toolset_declaration.persona_list``, never obeyed.
            p.toolsets = validate_toolsets(list(overrides["toolsets"]))
            if p.toolsets:
                logger.info(
                    "agent_runtime.personas.%s.toolsets is legacy and admits nothing "
                    "(S0a atlas cleanup): %s. The harness lane reads the bound "
                    "profile's top-level toolsets: key; delete this list.",
                    persona_id,
                    ", ".join(p.toolsets),
                )
    return list(personas.values())


def ensure_persisted_personas(cfg: AgentRuntimeConfig | None = None):
    """Return the persisted persona store plus data-declared config records."""
    from .store import AgentStore

    cfg = cfg or load_agent_runtime_config()
    store = AgentStore()
    stored = {persona.id: persona for persona in store.list_all()}
    catalog = {persona.id: persona for persona in persona_records_from_config(cfg)}
    merged = {**catalog, **stored}
    return list(merged.values())


def _persona_from_overrides(persona_id: str, role: str, overrides: dict[str, Any], cfg: AgentRuntimeConfig):
    from .models import AgentPersona

    return AgentPersona(
        id=persona_id,
        display_name=str(overrides.get("display_name") or persona_id.replace("_", " ").title()),
        role=role,
        model=overrides.get("model") or cfg.default_model,
        provider=overrides.get("provider") or cfg.default_provider,
        api_mode=overrides.get("api_mode") or cfg.default_api_mode,
        toolsets=validate_toolsets(list(overrides.get("toolsets") or [])),
        system_prompt_path=str(overrides.get("system_prompt_path") or ""),
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


def _read_model_config(raw: dict[str, Any]) -> ReadModelConfig:
    # UNKNOWN KEYS ARE IGNORED, not rejected — every field is read through
    # ``raw.get(key, default)``, the same property the top-level loader states at
    # ``load_agent_runtime_config``. That is load-bearing for Stage 6: the live
    # operator root still carries ``read_model.enabled: true``, and a config that
    # sets a key the runtime no longer implements must load and be ignored rather
    # than fault the whole runtime out of a boot.
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
        # Same clamp window as ``default_max_seconds``, and for the same reasons
        # (below 30 s the checkpoint reserve leaves no working window; above a
        # day one turn outlives the mission clock) — a detached dispatch is a
        # LONGER turn, not a differently-bounded one.
        dispatch_max_seconds=_clamped_positive_float(
            raw.get("dispatch_max_seconds"),
            defaults.dispatch_max_seconds,
            minimum=MISSION_CHAT_MIN_MAX_SECONDS,
            maximum=MISSION_CHAT_MAX_MAX_SECONDS,
        ),
        dispatch_max_concurrent=_clamped_positive_int(
            raw.get("dispatch_max_concurrent"),
            defaults.dispatch_max_concurrent,
            minimum=1,
            maximum=32,
        ),
        compaction_threshold_tokens=_compaction_threshold_tokens(
            raw.get("compaction_threshold_tokens"), defaults.compaction_threshold_tokens
        ),
    )


def _compaction_threshold_tokens(value: Any, default: int) -> int:
    """Coerce the chat-lane compaction cap. ``0`` means "no lane cap".

    Deliberately NOT ``_clamped_positive_int``: that helper maps every
    non-positive value onto the default, which would make the documented
    rollback spelling (``compaction_threshold_tokens: 0``) silently re-enable
    the very cap it asks to remove. An operator who writes a disable must get a
    disable. Absent / unparseable still falls back to the shipped default —
    that is a missing opinion, not a stated one.
    """

    if value is None or isinstance(value, bool):
        return int(default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    if number <= 0:
        return 0
    return max(
        MISSION_CHAT_MIN_COMPACTION_TOKENS,
        min(MISSION_CHAT_MAX_COMPACTION_TOKENS, number),
    )


def mission_chat_compaction_threshold_tokens(cfg: AgentRuntimeConfig | None = None) -> int:
    """The chat-lane compaction cap in tokens; ``0`` ⇒ no lane cap.

    Harness-wide operator policy, so it loads through
    :func:`load_root_runtime_config` for exactly the reason
    :func:`mission_chat_default_max_seconds` documents — one sticky-active
    profile's own ``config.yaml`` must not decide when every OTHER profile's
    threads compact. A config fault degrades to the shipped default rather than
    failing the turn.
    """

    if cfg is not None:
        return _compaction_threshold_tokens(
            getattr(cfg.mission_chat, "compaction_threshold_tokens", None),
            MissionChatConfig().compaction_threshold_tokens,
        )
    try:
        return int(load_root_runtime_config().mission_chat.compaction_threshold_tokens)
    except Exception:  # pragma: no cover - defensive; a config fault must not kill a turn
        logger.debug(
            "mission_chat compaction threshold load failed; using the built-in default",
            exc_info=True,
        )
        return MissionChatConfig().compaction_threshold_tokens


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


def mission_chat_dispatch_max_seconds(cfg: AgentRuntimeConfig | None = None) -> float:
    """The wall budget a DETACHED dispatch's target turn gets.

    Loads through :func:`load_root_runtime_config` for exactly the reason
    :func:`mission_chat_default_max_seconds` documents — a sticky-active
    profile's own ``config.yaml`` must not be able to change how every other
    profile's background work is budgeted — and degrades to the built-in
    default rather than failing the dispatch."""

    if cfg is not None:
        return float(
            getattr(cfg.mission_chat, "dispatch_max_seconds", MissionChatConfig().dispatch_max_seconds)
        )
    try:
        return float(load_root_runtime_config().mission_chat.dispatch_max_seconds)
    except Exception:  # pragma: no cover - defensive; a config fault must not kill a dispatch
        logger.debug("mission_chat dispatch budget load failed; using the built-in default", exc_info=True)
        return MissionChatConfig().dispatch_max_seconds


def mission_chat_dispatch_max_concurrent(cfg: AgentRuntimeConfig | None = None) -> int:
    """How many detached dispatches may run at once (executor width)."""

    if cfg is not None:
        return int(
            getattr(
                cfg.mission_chat,
                "dispatch_max_concurrent",
                MissionChatConfig().dispatch_max_concurrent,
            )
        )
    try:
        return int(load_root_runtime_config().mission_chat.dispatch_max_concurrent)
    except Exception:  # pragma: no cover - defensive
        logger.debug("mission_chat dispatch concurrency load failed; using the built-in default", exc_info=True)
        return MissionChatConfig().dispatch_max_concurrent


def resolve_mission_chat_dispatch_max_seconds(
    requested: float | None, cfg: AgentRuntimeConfig | None = None
) -> float:
    """The DETACHED-dispatch budget precedence chokepoint.

    Mirrors :func:`resolve_mission_chat_max_seconds` exactly — an explicit
    caller-stated number always wins, ``None`` falls through to config — so the
    two lanes cannot drift into two different precedence rules. It is a separate
    function rather than a flag on the first because they resolve DIFFERENT
    config keys, and collapsing them behind a boolean is precisely the
    fragile-flag shape this repo's rulings forbid."""

    if requested is None:
        return mission_chat_dispatch_max_seconds(cfg)
    try:
        value = float(requested)
    except (TypeError, ValueError):
        return mission_chat_dispatch_max_seconds(cfg)
    return value if value > 0 else mission_chat_dispatch_max_seconds(cfg)


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


def _supervision_config(raw: dict[str, Any]) -> SupervisionConfig:
    raw = raw if isinstance(raw, dict) else {}
    defaults = SupervisionConfig()
    return SupervisionConfig(
        child_events_enabled=bool(raw.get("child_events_enabled", defaults.child_events_enabled)),
    )


def _coordinator_permission_config(raw: dict[str, Any]) -> CoordinatorPermissionConfig:
    return CoordinatorPermissionConfig(
        max_spawns=max(0, int(raw.get("max_spawns", 0))),
        may_kill_own=bool(raw.get("may_kill_own", True)),
        may_kill_others=bool(raw.get("may_kill_others", False)),
    )


def _mcp_admission_config(raw: dict[str, Any]) -> McpAdmissionConfig:
    """Parse the root MCP-admission controls.

    Server authority lives in each persona profile.  Unknown keys, including
    the retired ``roles`` policy table, are ignored like other removed config
    fields.  The connect budget is clamped so a config typo cannot park a chat
    turn behind a capability probe (or make the probe useless by rounding to
    zero).

    ``max_tool_calls_per_run`` is clamped the same way and for the same reason,
    with one extra property: there is no way to spell "unlimited". A missing,
    zero, negative or unparseable value falls back to the default, and the upper
    clamp refuses a fat-fingered ``1000000`` — an admitted MCP surface with no
    call bound is precisely the failure the budget exists to prevent, so it must
    not be reachable by a config typo either.
    """

    raw = raw if isinstance(raw, dict) else {}
    defaults = McpAdmissionConfig()
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
    )


#: Typed issue code for a ``tool_permissions.default_mode`` the runtime cannot
#: honor. Same ``{code, subject, summary, fix_hint}`` row shape the envelope
#: grant issues and the MCP admission denials already emit.
TOOL_PERMISSION_DEFAULT_MODE_UNKNOWN = "tool_permission_default_mode_unknown"


def _tool_permission_config(raw: dict[str, Any]) -> ToolPermissionConfig:
    """Parse ``agent_runtime.tool_permissions`` — a fault can only NARROW.

    Absent / blank ⇒ the shipped default (``unbounded``, operator ruling
    2026-08-09). A value the runtime does not recognize is NOT silently ignored
    and is NOT read as the shipped default: it produces a typed issue row and
    falls back to ``profile_default``, because a config the runtime could not
    parse must never resolve to more capability than the operator wrote. That
    asymmetry (wide shipped default, narrow fault fallback) is deliberate and is
    the one place this block behaves like the deny-by-default policies beside it.
    """

    raw = raw if isinstance(raw, dict) else {}
    if "default_mode" not in raw:
        return ToolPermissionConfig()
    text = normalize_permission_mode(raw.get("default_mode"))
    if not text:
        return ToolPermissionConfig()
    if text not in SUPPORTED_PERMISSION_MODES:
        issue = {
            "code": TOOL_PERMISSION_DEFAULT_MODE_UNKNOWN,
            "subject": "agent_runtime.tool_permissions.default_mode",
            "summary": (
                f"'{raw.get('default_mode')}' is not a permission mode; the runtime default "
                f"falls back to '{FALLBACK_DEFAULT_PERMISSION_MODE}' (never to "
                f"'{SHIPPED_DEFAULT_PERMISSION_MODE}') so a config fault cannot widen access."
            ),
            "fix_hint": (
                "Valid modes: " + ", ".join(sorted(SUPPORTED_PERMISSION_MODES)) + "."
            ),
        }
        return ToolPermissionConfig(
            default_mode=FALLBACK_DEFAULT_PERMISSION_MODE, issues=(issue,)
        )
    return ToolPermissionConfig(default_mode=text)


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
