from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

from model_tools import get_toolset_for_tool
from tools.registry import registry

from .chat_lane_toolsets import ChatLaneDrop, chat_lane_drop_rows
from .mcp_admission import (
    LANE_MISSION_CHAT,
    admission_enabled,
    admission_requirement_failures,
    resolve_mcp_admission,
)
from .mcp_lane import current_entry_point_lane, mcp_lane_requirement_failures
from .mission_chat_workdir import MissionChatWorkdir
from .models import AgentPersona
from .personas import (
    PERSONA_BLOCKED_TOOLS,
    REGISTRY_HYGIENE_BLOCKED_TOOLS,
    all_registered_toolsets,
    blocked_tool_names,
    effective_toolsets,
    role_from_persona,
)
from .profile_readiness import declared_mcp_server_names, profile_readiness_for_persona
from .tool_turn_history import load_tool_turn_history

TOOL_VISIBILITY_SCHEMA_VERSION = 2

_MUTATING_TOOLS = frozenset(
    {
        "apply_patch",
        "edit_file",
        "file.edit",
        "file.write",
        "patch",
        "terminal",
        "write_file",
    }
)


@dataclass(slots=True)
class ToolVisibilityOptions:
    permission_mode: str = "profile_default"
    permission_source: str = "persona_role_policy"
    permission_expired: bool = False
    repo_scope: str | None = None
    workdir: str | Path | None = None
    session_id: str | None = None
    task_id: str | None = None
    goal_id: str | None = None
    runtime_root: str | Path | None = None
    blocked_tool_names: list[str] | None = None
    enabled_toolsets: list[str] | None = None
    # Toolsets resolved from the persona's default configuration before a
    # session lane narrows them for cost/safety. When absent, the effective set
    # is also the configured set (worker/default-resolution compatibility).
    configured_toolsets: list[str] | None = None
    #: Authoritative final block set for a CHAT-lane preview (T9b). When set, it
    #: is used verbatim as ``final_blocked`` instead of the generic
    #: ``persona_blocked | requested_blocked`` union — because the chat-lane
    #: chokepoint has ALREADY resolved the true block (persona blocks + permission
    #: mode + chat-lane cost cuts + registry hygiene, minus the ``clarify``
    #: unblock the chat bridge grants). Threading it (with ``enabled_toolsets``)
    #: makes the operator-facing preview's ``final_model_tools`` byte-match the
    #: schema the chat lane actually ships. Callers that don't model a chat lane
    #: leave it ``None`` (unchanged behaviour).
    chat_lane_blocked_tool_names: list[str] | None = None
    #: Which CLI entry point is resolving this visibility ("harness", "chat",
    #: "acp", …). Only MCP-registering lanes hand a persona its declared MCP
    #: tools (see ``mcp_lane``), so the lane is what turns "no MCP tools here"
    #: from a silent absence into a typed ``requirement_failures`` row. Left
    #: ``None``, the lane is inferred from the running process.
    entry_point_lane: str | None = None
    #: Typed account of what the CHAT-lane cost policy removed for this persona
    #: (``chat_lane_toolsets.ChatLaneDrop``), threaded by the callers that model a
    #: chat lane — ``persona_runtime.apply_chat_lane_tool_scope`` and the
    #: ``persona tool-diff`` preview. Each drop becomes one typed
    #: ``requirement_failures`` row, because a list of what SURVIVED was never an
    #: account of what was removed and why (G5). Left ``None`` by callers that do
    #: not model a chat lane — a worker-lane resolve must not claim a chat-lane
    #: drop it never applied.
    chat_lane_capability_drops: tuple[ChatLaneDrop, ...] | None = None
    #: Resolved mission-chat repo grounding for this persona
    #: (``mission_chat_workdir.MissionChatWorkdir``). Contributes rows only when a
    #: CONFIGURED workdir could not be used — the fallback is safe, but silent
    #: fallback is exactly the defect this lane exists to retire (G6).
    mission_chat_workdir: MissionChatWorkdir | None = None
    expires_at: str | None = None
    turns_remaining: int | None = None


def resolve_tool_visibility(
    persona: AgentPersona,
    options: ToolVisibilityOptions | None = None,
    *,
    profile_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    opts = options or ToolVisibilityOptions()
    role = role_from_persona(persona)
    unbounded = _is_unbounded(opts)
    resolved_toolsets = _resolved_toolsets(persona, opts, unbounded=unbounded)
    configured_toolsets = list(opts.configured_toolsets or resolved_toolsets)
    role_allowed_toolsets = list(resolved_toolsets)
    persona_toolsets = list(getattr(persona, "toolsets", []) or [])
    persona_blocked = frozenset() if unbounded else blocked_tool_names(persona)
    requested_blocked = frozenset(_clean_names(opts.blocked_tool_names or []))
    if opts.chat_lane_blocked_tool_names is not None:
        # T9b chat-lane preview parity: use the chat-lane chokepoint's already
        # resolved block verbatim (see ToolVisibilityOptions). The generic
        # ``persona_blocked | requested_blocked`` union would re-add ``clarify``
        # (a PERSONA_BLOCKED_TOOLS member the chat lane deliberately unblocks) and
        # would miss the chat-lane cost cuts, so the preview would lie.
        final_blocked = frozenset(_clean_names(opts.chat_lane_blocked_tool_names))
    else:
        final_blocked = persona_blocked | requested_blocked
    candidate_tools = _tool_names_for_toolsets(resolved_toolsets, blocked_tool_names=[])
    final_tools = _tool_names_for_toolsets(resolved_toolsets, blocked_tool_names=sorted(final_blocked))
    blocked_entries = _blocked_tool_entries(
        sorted((set(candidate_tools) | set(_clean_names(final_blocked))) - set(final_tools)),
        role_denies=frozenset(),
        persona_denies=PERSONA_BLOCKED_TOOLS,
        requested_denies=requested_blocked,
        registry_hygiene_denies=REGISTRY_HYGIENE_BLOCKED_TOOLS,
    )
    candidate_set = set(candidate_tools)
    withheld_tools = [entry for entry in blocked_entries if entry["name"] in candidate_set]
    policy_tools = [entry for entry in blocked_entries if entry["name"] not in candidate_set]
    excluded_toolsets = [
        {
            "name": name,
            "reason": "session_toolset_policy",
            "tools": _tool_names_for_toolsets([name], blocked_tool_names=[]),
        }
        for name in configured_toolsets
        if name not in set(resolved_toolsets)
    ]
    readiness = profile_readiness or _profile_readiness_for_visibility(persona)
    entry_point_lane = str(opts.entry_point_lane or "").strip() or current_entry_point_lane()
    requirement_failures = _requirement_failures(
        persona,
        opts,
        lane=entry_point_lane,
        role=str(role.value if hasattr(role, "value") else role),
    )
    resolved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    resolution_id = _tool_resolution_id(
        persona_id=persona.id,
        permission_mode=opts.permission_mode,
        configured_toolsets=configured_toolsets,
        effective_toolsets=resolved_toolsets,
        final_tools=final_tools,
        blocked_entries=blocked_entries,
    )
    token_estimate = _estimate_model_tool_tokens(final_tools)
    return {
        "schema_version": TOOL_VISIBILITY_SCHEMA_VERSION,
        "persona_id": persona.id,
        "display_name": persona.display_name,
        "role": str(role.value if hasattr(role, "value") else role),
        "hermes_profile": getattr(persona, "hermes_profile", None),
        "profile_readiness": readiness.get("readiness"),
        "profile_readiness_summary": readiness.get("summary"),
        "permission_mode": opts.permission_mode,
        "permission_source": opts.permission_source,
        "permission_expired": bool(opts.permission_expired),
        "session_id": opts.session_id,
        "task_id": opts.task_id,
        "goal_id": opts.goal_id,
        "repo_scope": opts.repo_scope or getattr(persona, "repo_scope_label", None),
        "workdir": str(opts.workdir) if opts.workdir is not None else None,
        "runtime_root": str(opts.runtime_root) if opts.runtime_root is not None else None,
        "profile_toolsets": resolved_toolsets,
        "persona_toolsets": persona_toolsets,
        "configured_toolsets": configured_toolsets,
        "effective_toolsets": resolved_toolsets,
        "excluded_toolsets": excluded_toolsets,
        "role_allowed_toolsets": role_allowed_toolsets,
        "persona_candidate_tools": candidate_tools,
        "profile_candidate_tools": candidate_tools,
        "final_model_tools": final_tools,
        "callable_tools": [_tool_entry(name) for name in final_tools],
        "final_tool_count": len(final_tools),
        # Backwards-compatible scalar. New consumers use the typed estimate so
        # the UI never presents this name-length heuristic as an exact bill.
        "model_tool_tokens": token_estimate,
        "model_tool_token_estimate": {
            "value": token_estimate,
            "exact": False,
            "method": "tool_name_envelope_v1",
        },
        "blocked_tool_names": [entry["name"] for entry in blocked_entries],
        "blocked_tools": blocked_entries,
        "withheld_tools": withheld_tools,
        "policy_tools": policy_tools,
        "availability_counts": {
            "configured_toolsets": len(configured_toolsets),
            "effective_toolsets": len(resolved_toolsets),
            "callable": len(final_tools),
            "withheld": len(withheld_tools),
            "policy": len(policy_tools),
            "excluded_toolsets": len(excluded_toolsets),
        },
        "entry_point_lane": entry_point_lane,
        # Typed capability accounting. Historically hardcoded ``[]``, which made
        # the by-design "MCP never registers on the harness lane" drop read as
        # "nothing is wrong" — see ``mcp_lane``.
        "requirement_failures": requirement_failures,
        "mutation_boundary": _mutation_boundary(final_tools),
        "expires_at": opts.expires_at,
        "turns_remaining": opts.turns_remaining,
        "resolved_at": resolved_at,
        "resolution_id": resolution_id,
    }


def _requirement_failures(
    persona: AgentPersona, opts: ToolVisibilityOptions, *, lane: str, role: str = ""
) -> list[dict[str, Any]]:
    """Typed capability accounting for this persona on this entry-point lane.

    One list, one row shape, every accounted drop — the generalization the MCP
    row proved out (see ``chat_lane_toolsets`` and the audit's G5). Composition,
    in order:

    * **MCP** — with admission disabled (the default, and every deployment until
      an operator flips the flag) exactly the R0 answer: one
      ``mcp_not_registered_on_lane`` row per declared server the lane never
      registers. With admission enabled it is truthful in BOTH directions: a
      server this persona was admitted and that actually registered stops
      producing a drop row, and a server denied for a typed reason reports THAT
      reason instead of the generic lane row. The admission resolve is skipped
      entirely when the flag is off, so the default path costs nothing beyond
      the R0 read it already performed.
    * **Chat-lane cost policy** — one row per toolset / tool the policy removed,
      but only for callers that actually modeled a chat lane and threaded the
      typed drops. A worker-lane resolve threads none and claims none.
    * **Mission-chat workdir** — a row only when a CONFIGURED grounding path
      could not be used (the turn still runs, in the safe cwd).

    Rows are appended in that order so the MCP payload of an
    MCP-declaring persona stays byte-identical to what R0/R1 emitted.
    """

    declared = declared_mcp_server_names(persona)
    if not declared or not admission_enabled():
        rows = mcp_lane_requirement_failures(declared_servers=declared, lane=lane)
    else:
        admission = resolve_mcp_admission(
            persona, lane=LANE_MISSION_CHAT, permission_mode=opts.permission_mode
        )
        rows = admission_requirement_failures(
            admission, declared_servers=declared, lane=lane
        )
    rows.extend(
        chat_lane_drop_rows(
            opts.chat_lane_capability_drops, role=role, entry_point_lane=lane
        )
    )
    if opts.mission_chat_workdir is not None:
        rows.extend(opts.mission_chat_workdir.rows(entry_point_lane=lane))
    return rows


def turn_tool_context_for_persona(
    persona: AgentPersona,
    options: ToolVisibilityOptions | None = None,
    *,
    visibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visibility = visibility or resolve_tool_visibility(persona, options)
    history = load_tool_turn_history(
        persona_id=visibility["persona_id"],
        session_id=visibility.get("session_id"),
        limit=10,
    )
    return {
        "schema_version": TOOL_VISIBILITY_SCHEMA_VERSION,
        "kind": "turn_tool_context",
        "persona_id": visibility["persona_id"],
        "session_id": visibility.get("session_id"),
        "task_id": visibility.get("task_id"),
        "goal_id": visibility.get("goal_id"),
        "preview": visibility,
        "last_actual": history["last_actual"],
        "latest_persona_actual": history["latest_persona_actual"],
        "last_actual_session_match": history["last_actual_session_match"],
        "history": history["history"],
    }


def permission_state_for_persona(
    persona: AgentPersona,
    options: ToolVisibilityOptions | None = None,
    *,
    visibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    visibility = visibility or resolve_tool_visibility(persona, options)
    return {
        "schema_version": TOOL_VISIBILITY_SCHEMA_VERSION,
        "persona_id": visibility["persona_id"],
        "session_id": visibility.get("session_id"),
        "mode": visibility.get("permission_mode") or "profile_default",
        "source": visibility.get("permission_source") or "persona_role_policy",
        "expired": bool(visibility.get("permission_expired")),
        "repo_scope": visibility.get("repo_scope"),
        "workdir": visibility.get("workdir"),
        "can_mutate_files": bool(visibility["mutation_boundary"]["can_mutate_files"]),
        "can_run_terminal": "terminal" in set(visibility["final_model_tools"]),
        "blocked_tools": visibility["blocked_tools"],
        "expires_at": visibility.get("expires_at"),
        "turns_remaining": visibility.get("turns_remaining"),
    }


# ``agent_hud_state_for_persona`` was RETIRED in the snapshot residue-slim R2
# (2026-07-17). The legacy per-persona "Agent HUD" readout is superseded by the
# runtime situational-HUD lane (``runtime_hud.py``) — the single HUD authority
# the operator's Mission Control HUD strip and the agent's chat turn both render.
# The wire summary no longer carries ``agent_hud_state``; the launcher's HUD
# dialog kind is removed. Do not reintroduce this — extend the situational HUD.


def _tool_names_for_toolsets(toolsets: list[str], *, blocked_tool_names: list[str]) -> list[str]:
    return list(_cached_tool_names_for_toolsets(tuple(toolsets), tuple(blocked_tool_names)))


def _estimate_model_tool_tokens(tool_names: list[str]) -> int:
    # Cheap HUD estimate: each name implies a schema envelope even when we do
    # not materialize the full provider payload on this path.
    return sum(max(8, (len(name) + 96) // 4) for name in tool_names)


def _profile_readiness_for_visibility(persona: AgentPersona) -> dict[str, Any]:
    return dict(
        _cached_profile_readiness_for_visibility(
            str(getattr(persona, "id", "") or ""),
            str(getattr(persona, "hermes_profile", "") or ""),
            tuple(getattr(persona, "skills", []) or []),
            tuple(getattr(persona, "required_mcp_servers", []) or []),
            str(getattr(persona, "provider", "") or ""),
            str(getattr(persona, "model", "") or ""),
            str(getattr(persona, "api_mode", "") or ""),
        )
    )


# Readiness is assembled from on-disk profile/skill/config state and is consumed
# BOTH by resolve_tool_visibility (below) and by the agents-drawer summary
# (snapshot._agent_summary) — computing it twice per agent per build was pure
# waste. A short-TTL memo (mirroring _installed_skill_catalog /
# _profile_template_memo, 15s) shares one computation across both callers within
# a build. It is NOT a process-lifetime lru_cache: the leaf skill/config reads
# under profile_readiness_for_persona are themselves mtime-invalidated, and this
# memo lapses every 15s, so a genuine on-disk change surfaces on the first build
# after the TTL — never process-lifetime stale. Keyed on the readiness inputs AND
# the resolver's identity, so a monkeypatched profile_readiness_for_persona
# invalidates the entry immediately.
_PROFILE_READINESS_TTL_SECONDS = 15.0
_PROFILE_READINESS_MEMO_MAX = 512
_profile_readiness_memo: dict[tuple[Any, ...], dict[str, Any]] = {}


def _profile_readiness_cache_clear() -> None:
    """Test hook — drop the readiness TTL memo."""
    _profile_readiness_memo.clear()


def _cached_profile_readiness_for_visibility(
    persona_id: str,
    hermes_profile: str,
    skills: tuple[str, ...],
    required_mcp_servers: tuple[str, ...],
    provider: str,
    model: str,
    api_mode: str,
) -> tuple[tuple[str, Any], ...]:
    import time

    key = (
        persona_id,
        hermes_profile,
        skills,
        required_mcp_servers,
        provider,
        model,
        api_mode,
    )
    fn = profile_readiness_for_persona
    now = time.monotonic()
    entry = _profile_readiness_memo.get(key)
    if (
        entry is not None
        and entry["fn"] is fn
        and now - entry["at"] < _PROFILE_READINESS_TTL_SECONDS
    ):
        return entry["value"]
    persona = AgentPersona(
        id=persona_id,
        display_name=persona_id,
        role=persona_id,
        provider=provider or None,
        model=model or None,
        api_mode=api_mode,
        toolsets=[],
        system_prompt_path="",
        hermes_profile=hermes_profile or None,
        skills=list(skills),
        required_mcp_servers=list(required_mcp_servers),
    )
    value = tuple(fn(persona).items())
    if len(_profile_readiness_memo) >= _PROFILE_READINESS_MEMO_MAX:
        _profile_readiness_memo.clear()
    _profile_readiness_memo[key] = {"at": now, "fn": fn, "value": value}
    return value


@lru_cache(maxsize=128)
def _cached_tool_names_for_toolsets(toolsets: tuple[str, ...], blocked_tool_names: tuple[str, ...]) -> tuple[str, ...]:
    blocked = set(blocked_tool_names)
    names = [
        name
        for name in registry.get_all_tool_names()
        if name not in blocked and str(get_toolset_for_tool(name) or "") in set(toolsets)
    ]
    return tuple(sorted(names))


def _blocked_tool_entries(
    names: list[str],
    *,
    role_denies: frozenset[str],
    persona_denies: frozenset[str],
    requested_denies: frozenset[str],
    registry_hygiene_denies: frozenset[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name in names:
        reason = "session_tool_policy"
        if name in requested_denies:
            reason = "turn_runtime_block"
        elif name in registry_hygiene_denies:
            reason = "registry_hygiene"
        elif name in role_denies:
            reason = "role_policy"
        elif name in persona_denies:
            reason = "persona_safety_policy"
        entries.append(
            {
                "name": name,
                "toolset": get_toolset_for_tool(name),
                "reason": reason,
                "mutating": name in _MUTATING_TOOLS,
            }
        )
    return entries


def _tool_entry(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "toolset": get_toolset_for_tool(name),
        "mutating": name in _MUTATING_TOOLS,
    }


def _tool_resolution_id(
    *,
    persona_id: str,
    permission_mode: str,
    configured_toolsets: list[str],
    effective_toolsets: list[str],
    final_tools: list[str],
    blocked_entries: list[dict[str, Any]],
) -> str:
    material = {
        "persona_id": persona_id,
        "permission_mode": permission_mode,
        "configured_toolsets": configured_toolsets,
        "effective_toolsets": effective_toolsets,
        "final_tools": final_tools,
        "blocked": [
            (entry.get("name"), entry.get("reason"), entry.get("toolset"))
            for entry in blocked_entries
        ],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"toolres_{hashlib.sha256(encoded).hexdigest()[:16]}"


def _mutation_boundary(tool_names: list[str]) -> dict[str, Any]:
    names = set(tool_names)
    mutating = sorted(names & _MUTATING_TOOLS)
    return {
        "can_mutate_files": bool(names & {"apply_patch", "edit_file", "file.edit", "file.write", "patch", "write_file"}),
        "can_run_terminal": "terminal" in names,
        "mutating_tools": mutating,
    }


def _clean_names(values) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _resolved_toolsets(persona: AgentPersona, options: ToolVisibilityOptions, *, unbounded: bool) -> list[str]:
    if options.enabled_toolsets is not None:
        return list(options.enabled_toolsets)
    if unbounded:
        return all_registered_toolsets()
    return effective_toolsets(persona)


def _is_unbounded(options: ToolVisibilityOptions) -> bool:
    return str(getattr(options, "permission_mode", "") or "").strip().lower() == "unbounded"
