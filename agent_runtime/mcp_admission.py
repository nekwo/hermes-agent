"""Selective, declared, per-run MCP admission for harness-lane persona runs.

Why this module exists
----------------------
``mcp_lane`` (R0) made one thing honest: the harness / mission-chat lane never
runs ``discover_mcp_tools()``, so a persona whose profile DECLARES MCP servers
gets none of their tools there, and now says so in a typed
``mcp_not_registered_on_lane`` row instead of reporting ``[]``.

This module is R1 of the follow-on: turning that honest refusal into an honest
ADMISSION for a narrow, declared, config-flagged set of personas — so a QA
persona that already declares ``launcher_qa`` in three places can actually run
``mcp__launcher_qa__*`` tools AS ITSELF on the mission-chat lane, with its
chats, trace and roster presence all native.

Design (canonical): ``docs/agent-runtime-harness/mission-chat-mcp-admission.md``.
Read that before changing anything here.

The invariants this module exists to hold
-----------------------------------------
1. **The lane blanket never flips.** ``hermes_cli/main.py::_AGENT_COMMANDS`` is
   untouched and ``discover_mcp_tools()`` is never called from here. Admission
   is per-RUN and per-PERSONA, driven by ``register_mcp_servers({name: cfg})``
   for an explicitly resolved subset — the same per-session mechanism
   ``acp_adapter/server.py::_register_session_mcp_servers`` already uses.
2. **Deny by default, and every step can only NARROW.** requested (what the
   persona declares) → role/lane allowlist (root config, no wildcard) → R1 stage
   floor → resolvable on this machine (``machine_roots``) → permission-mode tool
   filter. A persona that declares nothing admits nothing; a role the config
   does not name admits nothing; an unknown lane admits nothing.
3. **``unbounded`` must never widen the admitted set.** The chat lane's
   ``unbounded`` mode resolves ``all_registered_toolsets()`` — which, in a
   long-lived multi-persona harness process, would include another persona's
   admitted MCP toolsets. ``scope_toolsets_to_admission`` is applied AFTER
   permission-mode resolution and strips every ``mcp-*`` toolset (and alias)
   this run was not admitted. That is the cross-persona isolation boundary in
   R1 — see "R2 consequence" below.
4. **Registration is single-flight and bounded.** ``tools.registry`` and
   ``tools/mcp_tool._servers`` are process-global and a serve process is
   multi-persona (``ThreadPoolExecutor(4)``), so two interleaved admissions
   against one global registry are refused (``mcp_admission_lane_busy``) rather
   than raced. A registration that outruns its budget degrades to
   ``mcp_admission_timeout`` and the turn continues without those tools.

R2 consequence (recorded deliberately, not an oversight)
--------------------------------------------------------
R1 does NOT tear anything down. Once a server is admitted in a warm serve
process its tools stay in the process registry until the process recycles, and
isolation rests entirely on invariant 3 (the per-run toolset scope) plus
single-flight. Open question 2 of the design asked whether ``tools/registry.py``
supports scoped removal: it does — ``registry.deregister`` exempts ``mcp-*``
toolsets from the plugin-ownership gate and drops the toolset check + aliases
once the last tool of a toolset is removed — so R2 can keep the connection warm
and tear down only the registry scope, which is the design's preferred shape.
Until R2 lands, one consequence is load-bearing and stated plainly: a
``read_only`` admission that follows a ``profile_default`` admission of the same
server in the SAME process re-uses the already-registered full tool surface
(``register_mcp_servers`` short-circuits on connected servers), so the
registration-time ``tools.exclude`` filter cannot subtract. That is why
``McpAdmission`` also carries ``blocked_tool_names`` — the mutating tools are
removed from the model's tool list at ``get_tool_definitions`` regardless of
what is registered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── typed denial codes ──────────────────────────────────────────────────────
#
# Same vocabulary as the design's §A. ``mcp_not_registered_on_lane`` is reused
# from R0 rather than re-spelled: "admitted, but it did not actually register"
# is exactly what that code already means.

from .mcp_lane import MCP_NOT_REGISTERED_ON_LANE  # noqa: E402  (re-exported below)

MCP_NOT_ADMITTED_FOR_ROLE = "mcp_not_admitted_for_role"
MCP_ADMISSION_DISABLED = "mcp_admission_disabled"
MCP_ADMISSION_LANE_BUSY = "mcp_admission_lane_busy"
MCP_ADMISSION_TIMEOUT = "mcp_admission_timeout"
#: Declared + role-admitted, but the persona's profile has no ``mcp_servers``
#: entry to spawn. The readiness taxonomy already calls this ``mcp_attention``;
#: this is its admission-side spelling.
MCP_SERVER_NOT_CONFIGURED = "mcp_server_not_configured"
#: ``read_only`` can only admit a server whose mutating tools we can name. An
#: unknown server has no reviewer-shaped subset, so read_only admits nothing
#: rather than admitting a surface it cannot subtract.
MCP_READ_ONLY_SUBSET_UNKNOWN = "mcp_read_only_subset_unknown"

#: The admission LANE — the runtime surface a persona turn runs on. Distinct
#: from ``mcp_lane``'s entry-point lane (``harness`` / ``chat`` / …), which
#: answers "did this process run MCP discovery". Both are needed: admission is
#: what puts tools on the harness entry point in the first place.
LANE_MISSION_CHAT = "mission_chat"

#: R1 stage floor. Admission is config-driven (design §A step 2, R4 widens by
#: config alone), but R1 additionally refuses any role outside this set even
#: when the config names it — the first autonomous lane that can spawn a local
#: GUI-driving executable ships to ONE role, deliberately. Retired by R4.
R1_ADMISSIBLE_ROLES = frozenset({"qa"})

#: Toolset-name prefix ``tools/mcp_tool.py`` registers every MCP server under.
_MCP_TOOLSET_PREFIX = "mcp-"

#: Raw (server-advertised) tool names that MUTATE the target a server drives.
#: Sourced from the launcher's own per-profile allowlist —
#: ``EterniaLauncher docs/stages/qa-reboot/launcher_qa_profile_allowlists.yaml``,
#: the ``denied`` rows of its reviewer / pm / alice profiles. R3 owes the
#: compiled positive ``tools.include`` plus a fixture parity test against that
#: file; R1 only needs the SUBTRACTION to be real, which is what ``read_only``
#: promises.
READ_ONLY_EXCLUDED_TOOLS: Mapping[str, tuple[str, ...]] = {
    "launcher_qa": (
        "mcp_launcher_qa_begin_pkce_login",
        "mcp_launcher_qa_click_button",
        "mcp_launcher_qa_dismiss_hashtag_onboarding",
        "mcp_launcher_qa_kill_launcher",
        "mcp_launcher_qa_launch_or_attach",
        "mcp_launcher_qa_open_app_tab",
        "mcp_launcher_qa_scroll",
        "mcp_launcher_qa_scroll_to",
        "mcp_launcher_qa_scroll_to_fixture",
        "mcp_launcher_qa_set_tab",
    ),
}

_DEFAULT_CONNECT_TIMEOUT_SECONDS = 20.0

#: Process-wide admission mutex. Held for the FULL duration of a registration
#: attempt — including past a caller's timeout — because the worker thread, not
#: the caller, releases it. A second admission that arrives while one is in
#: flight is refused, never interleaved.
_ADMISSION_LOCK = threading.Lock()


# ── typed results ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class McpAdmissionDenial:
    """One typed reason a declared MCP server is not available for this run.

    ``row()`` is deliberately the same shape as ``machine_roots.PathTokenIssue.row()``
    and the R0 ``mcp_not_registered_on_lane`` row, so operator surfaces that
    already render typed issue rows need no new case.
    """

    server: str
    code: str
    summary: str
    fix_hint: str = ""

    def row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "server": self.server,
            "summary": self.summary,
            "fix_hint": self.fix_hint,
        }


@dataclass(frozen=True, slots=True)
class McpAdmission:
    """The resolved, side-effect-free answer to "what may this run register?"

    Resolution NEVER spawns, connects or registers anything — it is pure policy
    over config + the persona's declaration, so an operator can inspect it
    (``hermes harness persona tool-diff <id> --explain-mcp``) before the flag is
    ever flipped, and so the whole transition table is unit-testable.
    """

    lane: str
    role: str
    permission_mode: str
    enabled: bool
    requested: tuple[str, ...] = ()
    server_names: tuple[str, ...] = ()
    denied: tuple[McpAdmissionDenial, ...] = ()
    server_configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Prefixed registry names removed from the model's tool list for this run
    #: (the warm-process backstop for the ``read_only`` subtraction; see the
    #: module docstring's R2 consequence).
    blocked_tool_names: tuple[str, ...] = ()
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS

    @property
    def is_empty(self) -> bool:
        return not self.server_names

    def denial_rows(self) -> list[dict[str, Any]]:
        return [denial.row() for denial in self.denied]

    def explain(self) -> dict[str, Any]:
        """Stable, machine-readable operator view. No side effects."""

        return {
            "lane": self.lane,
            "role": self.role,
            "permission_mode": self.permission_mode,
            "enabled": self.enabled,
            "requested": list(self.requested),
            "admitted": list(self.server_names),
            "denied": self.denial_rows(),
            "blocked_tool_names": list(self.blocked_tool_names),
            "connect_timeout_seconds": self.connect_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class McpAdmissionOutcome:
    """What actually happened when an admission was executed."""

    admitted: tuple[str, ...] = ()
    denied: tuple[McpAdmissionDenial, ...] = ()
    registered_tool_names: tuple[str, ...] = ()
    duration_ms: int = 0
    attempted: bool = False

    def denial_rows(self) -> list[dict[str, Any]]:
        return [denial.row() for denial in self.denied]


# ── config ──────────────────────────────────────────────────────────────────


def admission_config(cfg: Any | None = None):
    """The ``agent_runtime.mcp_admission`` block from the ROOT runtime config.

    Harness-wide operator policy, so it loads through
    ``config.load_root_runtime_config`` — a sticky-active profile's own
    ``config.yaml`` must not be able to grant itself MCP admission.
    """

    from .runtime_config import McpAdmissionConfig

    if cfg is not None:
        resolved = getattr(cfg, "mcp_admission", None)
        return resolved if resolved is not None else McpAdmissionConfig()
    try:
        from .config import load_root_runtime_config

        return load_root_runtime_config().mcp_admission
    except Exception:  # pragma: no cover - defensive; a config fault must not open the gate
        logger.debug("MCP admission config load failed; treating admission as disabled", exc_info=True)
        return McpAdmissionConfig()


def admission_enabled(cfg: Any | None = None) -> bool:
    """The single kill switch: ``agent_runtime.mcp_admission.enabled``."""

    return bool(getattr(admission_config(cfg), "enabled", False))


# ── resolution (pure) ───────────────────────────────────────────────────────


def resolve_mcp_admission(
    persona,
    *,
    lane: str = LANE_MISSION_CHAT,
    permission_mode: str = "profile_default",
    task: Any = None,
    stage: Any = None,
    cfg: Any = None,
) -> McpAdmission:
    """Resolve which declared MCP servers this persona run may register.

    Pure: reads config and the persona's profile declaration, and returns the
    compiled registration inputs. It performs **zero spawns** — a patched
    ``register_mcp_servers`` that fails the test if called is part of the suite.
    """

    from .personas import role_from_persona

    config = admission_config(cfg)
    lane = str(lane or "").strip()
    permission_mode = str(permission_mode or "profile_default").strip() or "profile_default"
    try:
        role = str(role_from_persona(persona).value)
    except Exception:  # pragma: no cover - defensive
        role = str(getattr(persona, "role", "") or "")
    requested = tuple(_requested_servers(persona, task=task, stage=stage))
    timeout = _positive_float(getattr(config, "connect_timeout_seconds", None)) or _DEFAULT_CONNECT_TIMEOUT_SECONDS

    def _empty(denials: Sequence[McpAdmissionDenial]) -> McpAdmission:
        return McpAdmission(
            lane=lane,
            role=role,
            permission_mode=permission_mode,
            enabled=bool(getattr(config, "enabled", False)),
            requested=requested,
            denied=tuple(denials),
            connect_timeout_seconds=timeout,
        )

    if not getattr(config, "enabled", False):
        return _empty(
            [
                McpAdmissionDenial(
                    server=name,
                    code=MCP_ADMISSION_DISABLED,
                    summary=(
                        f"MCP admission is disabled, so '{name}' is not registered for this run."
                    ),
                    fix_hint=(
                        "Set agent_runtime.mcp_admission.enabled: true in the ROOT "
                        "config.yaml (and name the role/lane under "
                        "agent_runtime.mcp_admission.roles) to admit declared MCP servers."
                    ),
                )
                for name in requested
            ]
        )

    if not requested:
        return _empty([])

    allowed = _allowed_servers(config, role=role, lane=lane)
    if role not in R1_ADMISSIBLE_ROLES:
        return _empty(
            [
                McpAdmissionDenial(
                    server=name,
                    code=MCP_NOT_ADMITTED_FOR_ROLE,
                    summary=(
                        f"Role '{role}' is outside the MCP admission stage floor "
                        f"({', '.join(sorted(R1_ADMISSIBLE_ROLES))}), so '{name}' is not registered."
                    ),
                    fix_hint=(
                        "Admission ships one role at a time. Widening it is a deliberate "
                        "product decision (design R4), not a config edit alone."
                    ),
                )
                for name in requested
            ]
        )

    denials: list[McpAdmissionDenial] = []
    candidates: list[str] = []
    for name in requested:
        if name in allowed:
            candidates.append(name)
            continue
        denials.append(
            McpAdmissionDenial(
                server=name,
                code=MCP_NOT_ADMITTED_FOR_ROLE,
                summary=(
                    f"'{name}' is declared for this persona, but role '{role}' is not "
                    f"allowed to admit it on the '{lane}' lane."
                ),
                fix_hint=(
                    "Add it under agent_runtime.mcp_admission.roles."
                    f"{role}.{lane} in the ROOT config.yaml, with a written security note."
                ),
            )
        )

    if not candidates:
        return _empty(denials)

    configured = _configured_servers_for(persona)
    resolvable: dict[str, Any] = {}
    for name in candidates:
        raw = configured.get(name)
        if not isinstance(raw, Mapping):
            denials.append(
                McpAdmissionDenial(
                    server=name,
                    code=MCP_SERVER_NOT_CONFIGURED,
                    summary=(
                        f"'{name}' is admitted for role '{role}', but the persona's profile "
                        "declares no mcp_servers entry to spawn."
                    ),
                    fix_hint=(
                        f"Add an mcp_servers.{name} block to the persona profile's config.yaml "
                        "(portable form: agent_runtime/docs/machine_roots_path_portability.md)."
                    ),
                )
            )
            continue
        resolvable[name] = raw

    resolved: dict[str, Any] = {}
    if resolvable:
        from .machine_roots import resolve_mcp_servers

        issues: list[tuple[str, Any]] = []
        resolved = resolve_mcp_servers(
            resolvable, on_issue=lambda name, issue: issues.append((name, issue))
        )
        for name, issue in issues:
            # Reuse the EXISTING machine_roots taxonomy verbatim (unbound_root,
            # root_target_missing, platform_unsupported, …) rather than minting a
            # parallel one — proving reuse is part of the R1 test plan.
            denials.append(
                McpAdmissionDenial(
                    server=str(name),
                    code=issue.code,
                    summary=issue.summary,
                    fix_hint=issue.fix_hint,
                )
            )

    admitted: list[str] = []
    compiled: dict[str, dict[str, Any]] = {}
    blocked: list[str] = []
    for name in candidates:
        entry = resolved.get(name)
        if not isinstance(entry, Mapping):
            continue
        filtered, excluded, denial = _apply_permission_mode(
            name, dict(entry), permission_mode=permission_mode
        )
        if denial is not None:
            denials.append(denial)
            continue
        filtered["connect_timeout"] = _bounded_connect_timeout(filtered.get("connect_timeout"), timeout)
        admitted.append(name)
        compiled[name] = filtered
        blocked.extend(_prefixed_tool_names(name, excluded))

    return McpAdmission(
        lane=lane,
        role=role,
        permission_mode=permission_mode,
        enabled=True,
        requested=requested,
        server_names=tuple(admitted),
        denied=tuple(denials),
        server_configs=compiled,
        blocked_tool_names=tuple(sorted(set(blocked))),
        connect_timeout_seconds=timeout,
    )


def _requested_servers(persona, *, task=None, stage=None) -> list[str]:
    """What the persona DECLARES — required ∪ profile-configured.

    Reuses the two existing declaration surfaces rather than writing a third:
    ``profile_readiness.declared_mcp_server_names`` (required ∪ the profile's
    ``mcp_servers`` block, and deliberately NOT the ambient operator config for
    an unbound persona) unioned with ``_effective_required_mcp_servers``, which
    already carries the role policy (``role == "qa"`` + visual proof ⇒
    ``launcher_qa``) written in ``profile_readiness.py``.
    """

    from .profile_readiness import _effective_required_mcp_servers, declared_mcp_server_names

    names: list[str] = []
    for source in (
        declared_mcp_server_names(persona),
        _effective_required_mcp_servers(persona, task=task, stage=stage),
    ):
        for value in source or []:
            text = str(value or "").strip()
            if text and text not in names:
                names.append(text)
    return sorted(names)


def _configured_servers_for(persona) -> dict[str, Any]:
    """The persona profile's own ``mcp_servers`` map (never the ambient one)."""

    from .parse_cache import cached_yaml_file
    from .profile_context import resolve_persona_profile
    from .profile_readiness import _configured_mcp_servers

    try:
        binding = resolve_persona_profile(persona)
        if binding.profile_home is None:
            return {}
        raw = cached_yaml_file(binding.profile_home / "config.yaml", default={}) or {}
        return dict(_configured_mcp_servers(raw))
    except Exception:  # pragma: no cover - defensive; a config fault must not open the gate
        logger.debug("MCP admission could not read the persona profile config", exc_info=True)
        return {}


def _allowed_servers(config: Any, *, role: str, lane: str) -> frozenset[str]:
    """``roles.<role>.<lane>`` — deny-by-default, no wildcard, no inheritance."""

    roles = getattr(config, "roles", None) or {}
    lanes = roles.get(role) if isinstance(roles, Mapping) else None
    if not isinstance(lanes, Mapping):
        return frozenset()
    servers = lanes.get(lane)
    if not isinstance(servers, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(name).strip() for name in servers if str(name or "").strip())


def _apply_permission_mode(
    server: str, config: dict[str, Any], *, permission_mode: str
) -> tuple[dict[str, Any], tuple[str, ...], McpAdmissionDenial | None]:
    """Compose the permission mode onto the EXISTING per-server tool filter.

    ``read_only`` subtracts the mutating tools through
    ``mcp_servers.<name>.tools.exclude`` — the allowlist mechanism
    ``tools/mcp_tool.py`` already implements — so a denied tool is never
    registered, rather than registered and then blocked. No new filtering code
    path, per the design's §A step 4.
    """

    from .tool_permissions import PERMISSION_MODE_READ_ONLY

    if permission_mode != PERMISSION_MODE_READ_ONLY:
        return config, (), None

    excluded = READ_ONLY_EXCLUDED_TOOLS.get(server)
    if excluded is None:
        return (
            config,
            (),
            McpAdmissionDenial(
                server=server,
                code=MCP_READ_ONLY_SUBSET_UNKNOWN,
                summary=(
                    f"read_only admission has no reviewer-shaped subset for '{server}', "
                    "so nothing is admitted rather than admitting a surface it cannot subtract."
                ),
                fix_hint=(
                    f"Add '{server}' to agent_runtime.mcp_admission.READ_ONLY_EXCLUDED_TOOLS "
                    "with a written security note, or run this persona in profile_default."
                ),
            ),
        )

    tools_filter = dict(config.get("tools") or {})
    include = _name_list(tools_filter.get("include"))
    if include:
        # A profile-authored include list is already narrower than the server's
        # full surface; read_only can only narrow it further.
        tools_filter["include"] = [name for name in include if name not in set(excluded)]
    else:
        tools_filter["exclude"] = sorted(set(_name_list(tools_filter.get("exclude"))) | set(excluded))
    config["tools"] = tools_filter
    return config, tuple(excluded), None


def _name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return []


def _prefixed_tool_names(server: str, tool_names: Iterable[str]) -> list[str]:
    """Registry/wire names for raw MCP tool names, via the upstream builder.

    Imported lazily and only on an admitted read_only run: importing
    ``tools.mcp_tool`` pulls the whole MCP SDK (~200ms), which no preview or
    disabled-flag path should ever pay. Mirroring the ``mcp__<server>__<tool>``
    convention locally was rejected — a silently drifting mirror is exactly the
    class of bug ``mcp_lane`` needed a guard test for.
    """

    names = [str(name).strip() for name in tool_names or [] if str(name or "").strip()]
    if not names:
        return []
    try:
        from tools.mcp_tool import mcp_prefixed_tool_name
    except Exception:  # pragma: no cover - MCP SDK absent; the tools.exclude filter still applies
        logger.debug("MCP admission could not resolve prefixed tool names", exc_info=True)
        return []
    return [mcp_prefixed_tool_name(server, name) for name in names]


def _bounded_connect_timeout(configured: Any, budget: float) -> float:
    """Never let a server's own connect timeout outrun the admission budget.

    ``launcher_qa`` declares ``connect_timeout: 60``; a mission-chat turn cannot
    spend that on a capability probe. Clamping here means the spawn attempt
    self-terminates inside the budget instead of leaving a thread wedged behind
    the caller's deadline.
    """

    value = _positive_float(configured)
    if value is None:
        return budget
    return min(value, budget)


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# ── toolset scoping (pure) ──────────────────────────────────────────────────


def scope_toolsets_to_admission(
    toolsets: Iterable[str] | None, *, admitted_servers: Iterable[str] | None
) -> list[str]:
    """Keep only the MCP toolsets THIS run was admitted; add the admitted ones.

    Applied after permission-mode resolution, which is what makes the
    ``unbounded`` rule hold: ``unbounded`` resolves ``all_registered_toolsets()``,
    and in a warm multi-persona process that set can contain another persona's
    admitted ``mcp-*`` toolsets. Stripping them here means no permission mode can
    widen the admitted set — the security acceptance property.

    Non-MCP toolsets pass through untouched and in order.
    """

    admitted = [str(name).strip() for name in admitted_servers or [] if str(name or "").strip()]
    keep = {f"{_MCP_TOOLSET_PREFIX}{name}" for name in admitted} | set(admitted)
    # ONE registry read per call, not one per toolset name. On every lane that
    # registers no MCP at all — which is every lane until an operator flips the
    # flag — the alias map is empty and the prefix test is the whole check.
    aliases = _mcp_toolset_aliases()
    scoped = [
        name
        for name in (toolsets or [])
        if not _is_mcp_toolset(name, aliases) or str(name) in keep
    ]
    for name in admitted:
        toolset = f"{_MCP_TOOLSET_PREFIX}{name}"
        if toolset not in scoped:
            scoped.append(toolset)
    return scoped


def _mcp_toolset_aliases() -> frozenset[str]:
    """Alias names that point at an ``mcp-*`` toolset in this process."""

    try:
        from tools.registry import registry

        aliases = registry.get_registered_toolset_aliases()
    except Exception:  # pragma: no cover - registry is always importable in-process
        return frozenset()
    return frozenset(
        str(alias)
        for alias, target in (aliases or {}).items()
        if str(target).startswith(_MCP_TOOLSET_PREFIX)
    )


def _is_mcp_toolset(name: Any, aliases: frozenset[str]) -> bool:
    """Is this toolset name an MCP toolset — canonical ``mcp-x`` or an alias?

    The registry registers ``mcp-<server>`` and an alias ``<server>`` pointing at
    it, so a bare server name in ``enabled_toolsets`` resolves to the same tools.
    Both spellings have to be scoped or the strip leaks through the alias.
    """

    text = str(name or "").strip()
    if not text:
        return False
    return text.startswith(_MCP_TOOLSET_PREFIX) or text in aliases


# ── requirement-failure composition ─────────────────────────────────────────


def admission_requirement_failures(
    admission: McpAdmission | None,
    *,
    declared_servers: Iterable[str],
    lane: str | None = None,
    registered_servers: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Typed ``requirement_failures`` for a persona, admission-aware.

    One row per declared server, at most, resolved by precedence:

    1. Admitted AND actually registered in this process ⇒ **no row**. This is
       the "visibility is truthful both ways" half — once admission works, the
       R0 drop row must stop being emitted for that server.
    2. Admission produced a typed denial ⇒ that denial's row, which is strictly
       more actionable than the generic lane row.
    3. Otherwise ⇒ the existing R0 ``mcp_not_registered_on_lane`` row.

    With admission disabled (``admission is None`` or ``enabled=False``) this is
    byte-identical to calling ``mcp_lane_requirement_failures`` directly — the
    flag-off path must not change what R0 reports.
    """

    from .mcp_lane import mcp_lane_requirement_failures, registered_mcp_server_names

    declared = [str(name).strip() for name in declared_servers or [] if str(name or "").strip()]
    if admission is None or not admission.enabled:
        return mcp_lane_requirement_failures(
            declared_servers=declared, lane=lane, registered_servers=registered_servers
        )

    registered = (
        registered_mcp_server_names()
        if registered_servers is None
        else frozenset(str(name).strip() for name in registered_servers or [])
    )
    # Admitted for THIS persona and registered in this process. Intersecting is
    # what stops one persona's live admission from silencing another persona's
    # honest drop row.
    effective = frozenset(admission.server_names) & registered
    denials = {denial.server: denial for denial in admission.denied}

    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for name in declared:
        if name in effective:
            continue
        denial = denials.get(name)
        if denial is not None and denial.code != MCP_ADMISSION_DISABLED:
            rows.append(denial.row())
            continue
        unresolved.append(name)
    rows.extend(
        mcp_lane_requirement_failures(
            declared_servers=unresolved, lane=lane, registered_servers=effective
        )
    )
    return rows


# ── execution (the only side-effecting entry point) ─────────────────────────


def admit_mcp_servers(
    admission: McpAdmission | None,
    *,
    register: Callable[[Mapping[str, Mapping[str, Any]]], Any] | None = None,
    timeout_seconds: float | None = None,
) -> McpAdmissionOutcome:
    """Register the admitted servers' tools for this run. Bounded, single-flight.

    Returns an outcome rather than raising: a capability probe must never be able
    to fail a turn. Every degradation is typed —
    ``mcp_admission_lane_busy`` (another admission in flight),
    ``mcp_admission_timeout`` (budget exhausted; the registrar keeps running in
    the background and may land for a LATER turn, which is why the timeout row
    says "not available for this turn" rather than "failed"), and
    ``mcp_not_registered_on_lane`` (the registrar returned but the server is not
    in the registry) — so the turn can state the truth and take the
    ``qa.request_screenshot`` fallback.
    """

    if admission is None or admission.is_empty:
        return McpAdmissionOutcome(denied=tuple(admission.denied) if admission else ())

    budget = _positive_float(timeout_seconds) or admission.connect_timeout_seconds
    servers = dict(admission.server_configs)
    started = time.perf_counter()

    if not _ADMISSION_LOCK.acquire(blocking=False):
        return McpAdmissionOutcome(
            attempted=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            denied=tuple(admission.denied)
            + tuple(
                McpAdmissionDenial(
                    server=name,
                    code=MCP_ADMISSION_LANE_BUSY,
                    summary=(
                        f"Another MCP admission is in flight in this process, so '{name}' "
                        "was not registered for this turn."
                    ),
                    fix_hint=(
                        "MCP registration is process-global; admissions are serialized on "
                        "purpose. Retry the turn, or use the server's harness-side contract "
                        "(e.g. qa.request_screenshot) for this one."
                    ),
                )
                for name in admission.server_names
            ),
        )

    done = threading.Event()
    box: dict[str, Any] = {}

    def _work() -> None:
        try:
            registrar = register or _default_registrar
            box["tools"] = registrar(servers)
        except Exception as exc:  # pragma: no cover - defensive; surfaced as a typed denial
            box["error"] = exc
            logger.warning("MCP admission registration failed: %s", exc, exc_info=True)
        finally:
            # The WORKER releases the mutex, never the caller: on a timeout the
            # caller returns while this registration is still connecting, and
            # releasing here is what keeps a second admission from interleaving
            # against the global registry mid-spawn.
            _ADMISSION_LOCK.release()
            done.set()

    worker = threading.Thread(target=_work, name="mcp-admission", daemon=True)
    try:
        worker.start()
    except Exception:  # pragma: no cover - thread exhaustion; never strand the mutex
        _ADMISSION_LOCK.release()
        logger.warning("MCP admission could not start its registration thread", exc_info=True)
        return McpAdmissionOutcome(attempted=True, denied=tuple(admission.denied))
    finished = done.wait(budget)
    duration_ms = int((time.perf_counter() - started) * 1000)

    if not finished:
        return McpAdmissionOutcome(
            attempted=True,
            duration_ms=duration_ms,
            denied=tuple(admission.denied)
            + tuple(
                McpAdmissionDenial(
                    server=name,
                    code=MCP_ADMISSION_TIMEOUT,
                    summary=(
                        f"'{name}' did not finish registering within {budget:.0f}s, so it is "
                        "not available for this turn."
                    ),
                    fix_hint=(
                        "The turn continues without it — report that plainly and use the "
                        "server's harness-side contract (e.g. qa.request_screenshot). If the "
                        "server is slow to start, start it before the turn rather than raising "
                        "agent_runtime.mcp_admission.connect_timeout_seconds into the turn budget."
                    ),
                )
                for name in admission.server_names
            ),
        )

    from .mcp_lane import registered_mcp_server_names

    registered = registered_mcp_server_names()
    admitted = tuple(name for name in admission.server_names if name in registered)
    missed = tuple(name for name in admission.server_names if name not in registered)
    tool_names = tuple(str(name) for name in (box.get("tools") or []))
    return McpAdmissionOutcome(
        attempted=True,
        admitted=admitted,
        registered_tool_names=tool_names,
        duration_ms=duration_ms,
        denied=tuple(admission.denied)
        + tuple(
            McpAdmissionDenial(
                server=name,
                code=MCP_NOT_REGISTERED_ON_LANE,
                summary=(
                    f"'{name}' was admitted for this run but did not register — the server "
                    "did not connect or advertised no tools."
                ),
                fix_hint=(
                    "Check the server is running and its command resolves on this machine "
                    "(hermes harness persona tool-diff <persona> --explain-mcp), then retry."
                ),
            )
            for name in missed
        ),
    )


def _default_registrar(servers: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """``register_mcp_servers`` for the admitted subset ONLY.

    ``discover_mcp_tools()`` is never called: it would register everything in the
    profile's config, which is precisely the blast radius admission refuses.
    """

    from tools.mcp_tool import register_mcp_servers

    return list(register_mcp_servers({name: dict(cfg) for name, cfg in servers.items()}) or [])
