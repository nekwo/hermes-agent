"""Repo grounding for a mission-chat turn — which directory the turn runs in.

Why this module exists
----------------------
``GPTPersonaRuntime.mission_chat_reply`` built its ``AgentRunRequest`` with no
``workdir`` at all, so ``profile_runner._agent_workdir(None)`` yielded without
``os.chdir`` and without exporting ``TERMINAL_CWD``: a mission-chat turn ran in
whatever cwd the serve (or CLI) process happened to hold, and every relative
path the agent used resolved against *that*. The worker lane had its own repo
grounding and ``hermes chat`` runs in the operator's own cwd; only this lane had
none — see ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/mission-chat-lane-gap-audit.md`` G6.

That comparison has since gone one-sided, so read it as history: S5 removed the
worker lane and S29 removed the helper it grounded through
(``persona_runtime._repo_context_for_persona``, which this docstring used to
name as if it were live). THIS module is now the only repo grounding the runtime
has — ``mission_chat_reply`` resolves a workdir through the ladder below and
hands it to ``AgentRunRequest.workdir`` on every mission-chat turn.

This module is the resolution POLICY only. It reuses the existing seam rather
than inventing a parallel one: the answer is handed to
``AgentRunRequest.workdir``, which ``profile_runner`` already honors (chdir +
``TERMINAL_CWD`` under ``_WORKDIR_LOCK``), which is what puts the directory in
front of the ``terminal`` / ``file`` tools. Nothing here changes any tool.

The ladder (first source that resolves to a real directory wins)
---------------------------------------------------------------
1. ``agent_runtime.personas.<id>.workdir`` in the ROOT ``config.yaml`` — the
   operator's explicit per-persona grounding. Absolute; ``${roots.…}`` machine
   tokens are expanded by the config reader, so the stanza stays portable.
2. The **workspace pointer**: the directory holding the operator-selected
   workspace ``AGENTS.md`` (``mission-chat message --agents-file``) when that
   file loaded for this turn. If the operator pointed the turn at a workspace's
   doctrine, that workspace is where the turn belongs.
3. The persona's own ``repo_scope`` — the same declared repo the worker lane
   grounds in, when it names a real directory on this machine.
4. Nothing: the turn keeps today's behavior and runs in the process cwd.

Failing to safe cwd, loudly
---------------------------
A configured workdir that does not exist must NEVER fail a turn — an operator
typo in a config stanza is not a reason to lose a conversation. It degrades to
the next rung of the ladder and records a TYPED issue
(:data:`MISSION_CHAT_WORKDIR_UNRESOLVED`) in the same ``requirement_failures``
row shape ``mcp_lane`` and ``chat_lane_toolsets`` emit, so the degradation is
accounted for instead of silent (the G5 rule applied to this seam).

Only the explicitly configured ``workdir`` key produces an issue row. A
``repo_scope`` that does not resolve on this machine is already typed by
``profile_readiness`` / ``machine_roots``; re-reporting it here would mint a
second authority for one fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: ``requirement_failures[].code`` for a configured workdir this machine cannot
#: use. Subject key: ``workdir``.
MISSION_CHAT_WORKDIR_UNRESOLVED = "mission_chat_workdir_unresolved"

#: Where a resolved workdir came from (or why there is none).
WORKDIR_SOURCE_PERSONA_CONFIG = "persona_config"
WORKDIR_SOURCE_WORKSPACE_AGENTS = "workspace_agents_file"
WORKDIR_SOURCE_PERSONA_REPO_SCOPE = "persona_repo_scope"
WORKDIR_SOURCE_PROCESS_CWD = "process_cwd"

#: Why a requested path was refused. Typed rather than free text so the
#: transition table is assertable.
WORKDIR_REASON_NOT_ABSOLUTE = "not_absolute"
WORKDIR_REASON_MISSING = "missing"
WORKDIR_REASON_NOT_A_DIRECTORY = "not_a_directory"


def persona_workdir_config_key(persona_id: str | None) -> str:
    """The EXACT root-``config.yaml`` key that grounds one persona's chat turns.

    One spelling authority shared by the reader (``config.mission_chat_workdir``),
    this module's docstring and every emitted row."""

    persona = str(persona_id or "").strip() or "<persona-id>"
    return f"agent_runtime.personas.{persona}.workdir"


@dataclass(frozen=True, slots=True)
class MissionChatWorkdirIssue:
    """One typed reason a requested grounding path was not used."""

    requested: str
    source: str
    reason: str
    config_key: str
    fell_back_to: str = WORKDIR_SOURCE_PROCESS_CWD

    def row(self, *, entry_point_lane: str = "") -> dict[str, Any]:
        lane = str(entry_point_lane or "").strip() or "unknown"
        why = {
            WORKDIR_REASON_NOT_ABSOLUTE: "is not an absolute path",
            WORKDIR_REASON_MISSING: "does not exist on this machine",
            WORKDIR_REASON_NOT_A_DIRECTORY: "is not a directory",
        }.get(self.reason, "could not be used")
        fallback = (
            "the process working directory"
            if self.fell_back_to == WORKDIR_SOURCE_PROCESS_CWD
            else f"the {self.fell_back_to.replace('_', ' ')} grounding"
        )
        return {
            "code": MISSION_CHAT_WORKDIR_UNRESOLVED,
            "workdir": self.requested,
            "entry_point_lane": lane,
            "source": self.source,
            "reason": self.reason,
            "configured_via": self.config_key,
            "summary": (
                f"The configured mission-chat workdir '{self.requested}' {why}, so "
                f"turns on the '{lane}' lane fall back to {fallback} instead of that "
                "repo — relative paths and terminal commands resolve somewhere else."
            ),
            "fix_hint": (
                f"Point `{self.config_key}` in the ROOT config.yaml at an existing "
                "absolute directory (a `${roots.<name>}` token keeps the stanza "
                "portable — bind it with `hermes harness roots set <name> <path>`). "
                "The turn is NOT failed by this: grounding degrades, it never kills a "
                "conversation."
            ),
        }


@dataclass(frozen=True, slots=True)
class MissionChatWorkdir:
    """The resolved grounding for one mission-chat turn.

    ``path`` is ``None`` when no source resolved — the deliberate "run where the
    process runs" degrade, byte-identical to the behavior before this module.
    """

    path: str | None = None
    source: str = WORKDIR_SOURCE_PROCESS_CWD
    issues: tuple[MissionChatWorkdirIssue, ...] = ()

    @property
    def grounded(self) -> bool:
        return self.path is not None

    def rows(self, *, entry_point_lane: str = "") -> list[dict[str, Any]]:
        return [issue.row(entry_point_lane=entry_point_lane) for issue in self.issues]

    def receipt(self) -> dict[str, Any]:
        """Stable, machine-readable account of the resolution. No side effects."""

        return {
            "workdir": self.path,
            "source": self.source,
            "grounded": self.grounded,
            "issues": [issue.row() for issue in self.issues],
        }


def _refusal(value: str) -> str | None:
    """Typed reason ``value`` cannot be a workdir, or ``None`` when it can.

    Filesystem faults (a disconnected network root, a permission error on the
    stat) are reported as ``missing`` rather than raised: this resolver runs on
    the path of every turn and must never be the thing that fails one.
    """

    try:
        path = Path(value).expanduser()
    except Exception:
        return WORKDIR_REASON_MISSING
    if not path.is_absolute():
        return WORKDIR_REASON_NOT_ABSOLUTE
    try:
        if path.is_dir():
            return None
        return WORKDIR_REASON_NOT_A_DIRECTORY if path.exists() else WORKDIR_REASON_MISSING
    except OSError:
        return WORKDIR_REASON_MISSING


def _resolved(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:  # pragma: no cover - resolve() on an existing dir
        return str(Path(value).expanduser())


def _workspace_root(agents_path: str | None) -> str | None:
    """The directory holding an operator-selected workspace ``AGENTS.md``."""

    text = str(agents_path or "").strip()
    if not text:
        return None
    try:
        parent = Path(text).expanduser().parent
    except Exception:  # pragma: no cover - defensive
        return None
    return str(parent) if str(parent) else None


def resolve_mission_chat_workdir(
    *,
    persona_id: str | None = None,
    configured: str | None = None,
    workspace_agents_path: str | None = None,
    repo_scope: str | None = None,
) -> MissionChatWorkdir:
    """Walk the grounding ladder; return the first real directory, typed.

    Pure policy over three inputs plus the filesystem's answer to "is this a
    directory", so the whole transition table is unit-testable with ``tmp_path``
    and no harness, config, or agent in the loop. Never raises.
    """

    issues: list[MissionChatWorkdirIssue] = []
    config_key = persona_workdir_config_key(persona_id)

    requested = str(configured or "").strip()
    if requested:
        reason = _refusal(requested)
        if reason is None:
            return MissionChatWorkdir(
                path=_resolved(requested), source=WORKDIR_SOURCE_PERSONA_CONFIG
            )
        issues.append(
            MissionChatWorkdirIssue(
                requested=requested,
                source=WORKDIR_SOURCE_PERSONA_CONFIG,
                reason=reason,
                config_key=config_key,
            )
        )

    for source, candidate in (
        (WORKDIR_SOURCE_WORKSPACE_AGENTS, _workspace_root(workspace_agents_path)),
        (WORKDIR_SOURCE_PERSONA_REPO_SCOPE, str(repo_scope or "").strip() or None),
    ):
        # A pointer that does not resolve is NOT re-typed here (see module
        # docstring): the operator's explicit `workdir` key owns the issue lane,
        # machine_roots / profile_readiness own repo_scope's.
        if candidate and _refusal(candidate) is None:
            return MissionChatWorkdir(
                path=_resolved(candidate),
                source=source,
                issues=_with_fallback(issues, source),
            )

    return MissionChatWorkdir(
        path=None,
        source=WORKDIR_SOURCE_PROCESS_CWD,
        issues=tuple(issues),
    )


def _with_fallback(
    issues: list[MissionChatWorkdirIssue], fell_back_to: str
) -> tuple[MissionChatWorkdirIssue, ...]:
    """Stamp what a refused request actually degraded to, so the row is honest."""

    return tuple(
        MissionChatWorkdirIssue(
            requested=issue.requested,
            source=issue.source,
            reason=issue.reason,
            config_key=issue.config_key,
            fell_back_to=fell_back_to,
        )
        for issue in issues
    )


def mission_chat_workdir_for_persona(
    persona: Any, *, workspace_agents_path: str | None = None
) -> MissionChatWorkdir:
    """:func:`resolve_mission_chat_workdir` for a live persona.

    The thin config-reading wrapper — the ONE place the mission-chat lane asks
    "where does this persona work?". Config faults degrade to "no configured
    workdir" rather than propagating: a broken config must not fail a turn.
    """

    persona_id = str(getattr(persona, "id", "") or "")
    try:
        from .config import mission_chat_workdir

        configured = mission_chat_workdir(persona_id)
    except Exception:  # pragma: no cover - defensive; a config fault never fails a turn
        configured = None
    return resolve_mission_chat_workdir(
        persona_id=persona_id,
        configured=configured,
        workspace_agents_path=workspace_agents_path,
        repo_scope=getattr(persona, "repo_scope", None),
    )
