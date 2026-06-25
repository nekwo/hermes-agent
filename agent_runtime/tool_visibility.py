from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from model_tools import get_tool_definitions, get_toolset_for_tool

from .models import AgentPersona
from .personas import (
    ALLOWED_TOOLSETS_BY_ROLE,
    PER_ROLE_TOOL_DENIES,
    PERSONA_BLOCKED_TOOLS,
    blocked_tool_names,
    effective_toolsets,
    role_from_persona,
)
from .profile_readiness import profile_readiness_for_persona
from .tool_turn_history import load_tool_turn_history

TOOL_VISIBILITY_SCHEMA_VERSION = 1

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
    repo_scope: str | None = None
    workdir: str | Path | None = None
    session_id: str | None = None
    task_id: str | None = None
    goal_id: str | None = None
    runtime_root: str | Path | None = None
    blocked_tool_names: list[str] | None = None
    enabled_toolsets: list[str] | None = None
    expires_at: str | None = None
    turns_remaining: int | None = None


def resolve_tool_visibility(persona: AgentPersona, options: ToolVisibilityOptions | None = None) -> dict[str, Any]:
    opts = options or ToolVisibilityOptions()
    role = role_from_persona(persona)
    resolved_toolsets = list(opts.enabled_toolsets) if opts.enabled_toolsets is not None else effective_toolsets(persona)
    role_allowed_toolsets = list(resolved_toolsets) if _is_unbounded(persona, opts) else sorted(ALLOWED_TOOLSETS_BY_ROLE[role])
    persona_toolsets = list(getattr(persona, "toolsets", []) or [])
    persona_blocked = blocked_tool_names(persona)
    requested_blocked = frozenset(_clean_names(opts.blocked_tool_names or []))
    final_blocked = persona_blocked | requested_blocked
    candidate_tools = _tool_names_for_toolsets(resolved_toolsets, blocked_tool_names=[])
    final_tools = _tool_names_for_toolsets(resolved_toolsets, blocked_tool_names=sorted(final_blocked))
    blocked_entries = _blocked_tool_entries(
        sorted((set(candidate_tools) | set(_clean_names(final_blocked))) - set(final_tools)),
        role_denies=PER_ROLE_TOOL_DENIES[role],
        persona_denies=PERSONA_BLOCKED_TOOLS,
        requested_denies=requested_blocked,
    )
    readiness = profile_readiness_for_persona(persona)
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
        "session_id": opts.session_id,
        "task_id": opts.task_id,
        "goal_id": opts.goal_id,
        "repo_scope": opts.repo_scope or getattr(persona, "repo_scope_label", None),
        "workdir": str(opts.workdir) if opts.workdir is not None else None,
        "runtime_root": str(opts.runtime_root) if opts.runtime_root is not None else None,
        "profile_toolsets": resolved_toolsets,
        "persona_toolsets": persona_toolsets,
        "effective_toolsets": resolved_toolsets,
        "role_allowed_toolsets": role_allowed_toolsets,
        "persona_candidate_tools": candidate_tools,
        "profile_candidate_tools": candidate_tools,
        "final_model_tools": final_tools,
        "final_tool_count": len(final_tools),
        "blocked_tool_names": [entry["name"] for entry in blocked_entries],
        "blocked_tools": blocked_entries,
        "requirement_failures": [],
        "mutation_boundary": _mutation_boundary(final_tools),
        "expires_at": opts.expires_at,
        "turns_remaining": opts.turns_remaining,
    }


def turn_tool_context_for_persona(persona: AgentPersona, options: ToolVisibilityOptions | None = None) -> dict[str, Any]:
    visibility = resolve_tool_visibility(persona, options)
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
        "history": history["history"],
    }


def permission_state_for_persona(persona: AgentPersona, options: ToolVisibilityOptions | None = None) -> dict[str, Any]:
    visibility = resolve_tool_visibility(persona, options)
    return {
        "schema_version": TOOL_VISIBILITY_SCHEMA_VERSION,
        "persona_id": visibility["persona_id"],
        "session_id": visibility.get("session_id"),
        "mode": visibility.get("permission_mode") or "profile_default",
        "source": visibility.get("permission_source") or "persona_role_policy",
        "repo_scope": visibility.get("repo_scope"),
        "workdir": visibility.get("workdir"),
        "can_mutate_files": bool(visibility["mutation_boundary"]["can_mutate_files"]),
        "can_run_terminal": "terminal" in set(visibility["final_model_tools"]),
        "blocked_tools": visibility["blocked_tools"],
        "expires_at": visibility.get("expires_at"),
        "turns_remaining": visibility.get("turns_remaining"),
    }


def agent_hud_state_for_persona(persona: AgentPersona, options: ToolVisibilityOptions | None = None) -> dict[str, Any]:
    visibility = resolve_tool_visibility(persona, options)
    return {
        "schema_version": TOOL_VISIBILITY_SCHEMA_VERSION,
        "kind": "agent_hud_state",
        "persona_id": visibility["persona_id"],
        "display_name": visibility["display_name"],
        "role": visibility["role"],
        "permission_mode": visibility["permission_mode"],
        "repo_scope": visibility.get("repo_scope"),
        "workdir": visibility.get("workdir"),
        "session_id": visibility.get("session_id"),
        "task_id": visibility.get("task_id"),
        "goal_id": visibility.get("goal_id"),
        "tool_count": visibility["final_tool_count"],
        "toolsets": visibility["effective_toolsets"],
        "blocked_tools": visibility["blocked_tools"],
        "mutation_boundary": visibility["mutation_boundary"],
    }


def _tool_names_for_toolsets(toolsets: list[str], *, blocked_tool_names: list[str]) -> list[str]:
    return list(_cached_tool_names_for_toolsets(tuple(toolsets), tuple(blocked_tool_names)))


@lru_cache(maxsize=128)
def _cached_tool_names_for_toolsets(toolsets: tuple[str, ...], blocked_tool_names: tuple[str, ...]) -> tuple[str, ...]:
    tools = get_tool_definitions(
        enabled_toolsets=list(toolsets),
        blocked_tool_names=list(blocked_tool_names),
        quiet_mode=True,
    )
    return tuple(sorted(_tool_name(tool) for tool in tools if _tool_name(tool)))


def _tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    return str(name or "").strip()


def _blocked_tool_entries(
    names: list[str],
    *,
    role_denies: frozenset[str],
    persona_denies: frozenset[str],
    requested_denies: frozenset[str],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name in names:
        reason = "not_resolved_for_turn"
        if name in requested_denies:
            reason = "turn_runtime_block"
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


def _is_unbounded(persona: AgentPersona, options: ToolVisibilityOptions) -> bool:
    return (
        str(getattr(persona, "hermes_profile", "") or "").strip().lower() == "unbounded"
        or str(getattr(options, "permission_mode", "") or "").strip().lower() == "unbounded"
    )
