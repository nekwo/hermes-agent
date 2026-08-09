"""Operator-facing explanation of the terminal safety envelope for one persona.

``agent_runtime.terminal_envelope`` is the authority: it owns the command-class
taxonomy, the ROOT-config grant table and the per-command
decision. ``runtime_hud.render_capability_block`` already renders the
AGENT-facing half of that posture onto the chat turn's volatile tail.

This module is the operator-facing half, and it exists for the same reason the
agent-facing half does: the facts were computed and typed, and the person who
has to act on them could not see them. Before ``hermes harness persona tool-diff
--explain-envelope``, an operator debugging "why did Dev refuse to push?" had to
read ``terminal_envelope.py`` to learn the command taxonomy and then guess at
the config key.

Everything here is RENDERING. Every fact comes from the canonical authorities —
:func:`terminal_envelope.explain_terminal_envelope`,
:func:`terminal_envelope.hard_floor_command_classes`,
:func:`terminal_envelope.scope_for_persona` — and no taxonomy, floor, or grant
rule is re-derived. A second derivation of "which classes are grantable" is
precisely how an operator surface starts telling a different story than the
runtime.
"""

from __future__ import annotations

from typing import Any

from .terminal_envelope import (
    GOVERNED_LANES,
    LANE_MISSION_CHAT,
    explain_terminal_envelope,
    hard_floor_command_classes,
    scope_for_persona,
)

#: The lane binds a :class:`TerminalEnvelopeScope`, so every gated command
#: resolves through ``envelope_decision``: grants are ROOT-config only and a
#: refusal is typed, explained and final.
DISPOSITION_DETERMINISTIC = "deterministic"
#: The lane binds no scope, so ``envelope_decision`` returns ``None`` and the
#: legacy terminal-tool behavior owns the command — which is fail-CLOSED when
#: ``HERMES_AGENT_RUNTIME_ROOT`` happens to be exported in this process and
#: fail-OPEN when it does not. That coin flip is exactly what the governed lane
#: retired; naming it is how an operator knows which world they are in.
DISPOSITION_LEGACY_AMBIENT = "legacy_ambient"

_DISPOSITION_SUMMARY = {
    DISPOSITION_DETERMINISTIC: (
        "deterministic — this lane binds an envelope scope, so every gated command "
        "resolves through one decision point. Grants come from the ROOT config only "
        "(a profile cannot grant itself), and a refusal is typed and final."
    ),
    DISPOSITION_LEGACY_AMBIENT: (
        "legacy ambient — this lane binds no envelope scope, so the decision point "
        "has no opinion and the legacy terminal-tool behavior applies: fail-CLOSED "
        "when HERMES_AGENT_RUNTIME_ROOT is exported in the process, fail-OPEN when "
        "it is not. Not a policy; process state."
    ),
}


def _resolved_permission_mode(persona: Any, *, session_id: str | None = None) -> str:
    """This persona's effective chat permission mode, or ``""`` if unresolvable.

    Read through the ONE chokepoint. An operator preview that skipped it would
    describe the config grants table alone, which since the 2026-08-09 ruling is
    only half the answer: an unbounded run is granted the grantable classes by
    MODE, and a preview that showed them refused would send the operator writing
    a grant stanza for something already allowed.
    """

    try:
        from .tool_permissions import permission_options_for_chat

        return str(
            permission_options_for_chat(persona, session_id=session_id).permission_mode or ""
        )
    except Exception:  # pragma: no cover - a preview must never fail on policy
        return ""


def explain_persona_terminal_envelope(
    persona: Any,
    *,
    lane: str = LANE_MISSION_CHAT,
    cfg: Any | None = None,
    session_id: str | None = None,
    permission_mode: str | None = None,
) -> dict[str, Any]:
    """The terminal-envelope posture of one persona's lane, as a JSON payload.

    Pure and side-effect free — like ``resolve_mcp_admission(...).explain()``,
    which the sibling ``--explain-mcp`` flag renders: an operator can read
    exactly what this persona WOULD get without running a turn or touching a
    tool.

    The refusal set retains its grantable/hard-floor split so any future
    code-owned floor remains representable. Ruling R-2 leaves the hard-floor
    side empty today.
    """

    mode = str(permission_mode or "").strip() or _resolved_permission_mode(
        persona, session_id=session_id
    )
    scope = scope_for_persona(persona, lane=lane, session_id=session_id, permission_mode=mode)
    explained = explain_terminal_envelope(
        role=scope.role, lane=scope.lane, cfg=cfg, permission_mode=mode
    )

    hard_floor = hard_floor_command_classes()
    refused = [str(name) for name in explained.get("refused") or ()]
    governed = bool(explained.get("governed"))
    disposition = (
        DISPOSITION_DETERMINISTIC if governed else DISPOSITION_LEGACY_AMBIENT
    )

    return {
        "lane": explained.get("lane"),
        "role": explained.get("role"),
        "persona_id": scope.persona_id,
        # ``governed`` IS "is a scope bound for this lane" — the same predicate
        # ``TerminalEnvelopeScope.governed`` answers at decision time.
        "governed": governed,
        "governed_lanes": sorted(GOVERNED_LANES),
        "disposition": disposition,
        "disposition_summary": _DISPOSITION_SUMMARY[disposition],
        "config_key": explained.get("config_key"),
        "command_classes": explained.get("command_classes"),
        "grantable_command_classes": explained.get("grantable_command_classes"),
        "hard_floor_command_classes": sorted(hard_floor),
        "permission_mode": explained.get("permission_mode"),
        "granted": explained.get("granted"),
        "granted_by_config": explained.get("granted_by_config"),
        "granted_by_permission_mode": explained.get("granted_by_permission_mode"),
        "refused": refused,
        "refused_grantable": [name for name in refused if name not in hard_floor],
        "refused_hard_floor": [name for name in refused if name in hard_floor],
        "grant_issues": explained.get("grant_issues"),
    }


def render_terminal_envelope_explanation(explained: dict[str, Any]) -> list[str]:
    """Human lines for the non-``--json`` output. One fact per line, no prose."""

    if not isinstance(explained, dict) or not explained:
        return []

    def _names(values: Any) -> str:
        items = [str(item) for item in (values or ())]
        return ", ".join(items) if items else "-"

    lines = [
        f"terminal envelope ({explained.get('lane')}, role={explained.get('role')}): "
        f"{'GOVERNED' if explained.get('governed') else 'NOT GOVERNED'}"
    ]
    lines.append(f"  disposition: {explained.get('disposition_summary')}")
    if explained.get("config_key"):
        lines.append(f"  grant config key: {explained['config_key']}")
    if explained.get("permission_mode"):
        lines.append(f"  permission mode: {explained['permission_mode']}")
    lines.append(f"  granted:  {_names(explained.get('granted'))}")
    if explained.get("granted_by_permission_mode"):
        lines.append(
            "    by permission mode: "
            f"{_names(explained.get('granted_by_permission_mode'))} (receipted per command)"
        )
    if explained.get("granted_by_config"):
        lines.append(f"    by config grant:    {_names(explained.get('granted_by_config'))}")
    lines.append(
        f"  refused (operator-grantable): {_names(explained.get('refused_grantable'))}"
    )
    lines.append(
        "  refused (HARD FLOOR, no config lifts): "
        f"{_names(explained.get('refused_hard_floor'))}"
    )
    for issue in explained.get("grant_issues") or ():
        if not isinstance(issue, dict):
            continue
        lines.append(f"  grant issue [{issue.get('code')}] {issue.get('summary')}")
        if issue.get("fix_hint"):
            lines.append(f"    fix: {issue['fix_hint']}")
    return lines


__all__ = [
    "DISPOSITION_DETERMINISTIC",
    "DISPOSITION_LEGACY_AMBIENT",
    "explain_persona_terminal_envelope",
    "render_terminal_envelope_explanation",
]
