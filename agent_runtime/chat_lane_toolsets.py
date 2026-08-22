"""Chat-lane toolset cost policy — the single place that decides which toolsets
a conversational (operator-chat / mission-chat) lane is allowed to ship.

Why this exists
---------------
Tool schemas are re-billed on *every* API call: the model receives the full
tool-schema list each turn, and (after the system message) they are the single
largest fixed slice of a chat turn's prompt. A supervision / operator chat lane
does not drive a browser, analyze images, or execute code — those are worker
capabilities — yet a chat lane whose resolved toolsets happen to include them
(an unbounded grant, a persona configured with the QA/supervisor browser+vision
set, or a legacy default) pays the full browser/vision/execute schema on every
conversational turn. On the live mission-chat lane that was 10 ``browser_*``
tools + ``vision_analyze`` + ``execute_code`` riding a supervision chat that
never uses them.

What this is (and is NOT)
-------------------------
This module is a **pure filter** over an *already-resolved* toolset list. It
never GRANTS a toolset that a role / permission layer withheld — it only DROPS
the browser / vision / heavy-dev toolsets from a chat lane's enabled set. Role
gating, persona blocklists, and permission mode
all run first and upstream; this is the last, cost-motivated narrowing applied
only at the chat-lane chokepoint (``persona_runtime._enabled_toolsets_for_chat``).

Worker / dev task lanes never go through here — they resolve toolsets via
``effective_toolsets(persona)`` directly (``persona_runtime.run_persona``,
``root_node_engine``, ``node_tools``); only the two operator/mission chat call
sites apply this policy, so there is exactly one chat-lane authority.

Per-profile override
--------------------
A deployment that genuinely wants one of these toolsets — or the single
``skill_manage`` tool — back on a persona's chat lane restores it per-persona via
config. The one ``chat_lane_restore_toolsets`` list restores both toolsets and
the single tool cuts (T6a):

    agent_runtime:
      personas:
        neko_supervisor:
          # keep the file toolset, the terminal toolset, AND skill authoring on
          # Neko's chat lane
          chat_lane_restore_toolsets: [file, terminal, skill_manage]

Restore is un-exclusion, not a grant: a restored toolset is only kept if it was
already in the lane's resolved set — you cannot hand a chat persona the browser
toolset through this knob. A restored single tool (``skill_manage``) is likewise
only un-blocked, not injected; it reappears only because its toolset
(``skills``) is enabled.

**Why that is safe, stated exactly — do NOT relax un-exclusion believing a role
backstop exists.** This clause used to read "(role gating still applies)". It no
longer does: S61/S64 made SOUL/profile/persona declarations the sole capability
authority, and ``personas.validate_toolsets`` is now a pure dedupe with no role
ceiling of any kind. The ONLY thing standing between this knob and an arbitrary
grant is the un-exclusion semantics themselves — the intersection with the
already-resolved set. Widen restore into a grant and there is nothing behind it.

Typed drop accounting (G5)
--------------------------
The exclusions above are by DESIGN. Their INVISIBILITY was the defect: a
mission-chat agent asked to run a command finds no ``terminal`` tool, and both
it and the operator read the plain absence as a permission problem — the exact
failure class ``mcp_lane`` was written to retire for MCP, recurring here (see
``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/mission-chat-lane-gap-audit.md`` §6 / G5).

So every dropper below returns **what it removed**, not only what remains:
:func:`chat_lane_toolset_drops` and :func:`chat_lane_tool_drops` produce typed
:class:`ChatLaneDrop` values and :meth:`ChatLaneDrop.row` renders the SAME
``requirement_failures`` row shape ``mcp_lane`` /
``mcp_admission.McpAdmissionDenial.row()`` / ``machine_roots.PathTokenIssue.row()``
already emit (``{code, <subject>, entry_point_lane, summary, fix_hint}``), so
operator surfaces that already render typed issue rows need no new case.

Accounting only: nothing here grants, restores or registers anything, and the
list-returning policy functions are byte-for-byte unchanged. A RESTORED toolset
produces no row — it was not dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable


#: ``requirement_failures[].code`` for a whole toolset this policy removed from a
#: chat lane. Subject key: ``toolset``.
TOOLSET_DROPPED_BY_CHAT_LANE_POLICY = "toolset_dropped_by_chat_lane_policy"

#: ``requirement_failures[].code`` for a single TOOL this policy removed while
#: keeping the rest of its toolset. Subject key: ``tool``.
TOOL_DROPPED_BY_CHAT_LANE_POLICY = "tool_dropped_by_chat_lane_policy"

#: Drop kinds — which subject key a :class:`ChatLaneDrop` renders under.
DROP_KIND_TOOLSET = "toolset"
DROP_KIND_TOOL = "tool"


#: Toolsets a conversational chat lane never needs, dropped by default so their
#: schemas stop riding every turn. Ordered by savings: ``browser`` is 10
#: ``browser_*`` tools (+ ``browser_vision``), ``vision`` is ``vision_analyze``,
#: ``code_execution`` is ``execute_code``, ``debugging`` is the interactive
#: debugger tools. These are the "browser / vision / heavy-dev" toolsets.
#:
#: T6a (2026-07-18) adds the dev-toolkit toolsets ``file`` (read_file /
#: write_file / patch / search_files) and ``terminal`` (terminal / process): a
#: supervision / operator chat coordinates and reads out — it does not patch
#: files or drive a shell as its default posture, and those schemas are large.
#: A persona that genuinely needs them back on its *bounded* chat lane restores
#: them via ``chat_lane_restore_toolsets`` (un-exclusion, not a grant), and the
#: ``unbounded`` permission mode is always returned unfiltered. Worker / dev
#: task lanes never pass through this filter, so their file/terminal access is
#: untouched.
DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS: frozenset[str] = frozenset(
    {
        "browser",
        "vision",
        "code_execution",
        "debugging",
        "file",
        "terminal",
    }
)


#: Single TOOLS (not whole toolsets) the chat-lane cost policy drops, applied via
#: the blocked-tool-names lane rather than toolset exclusion. ``skill_manage``
#: (skill authoring — create / edit / delete / install) is a worker capability,
#: not a supervision-chat one, and carries a large schema; but the ``skills``
#: toolset can't be split at the toolset level, so cutting the single tool while
#: keeping ``skill_search`` / ``skill_view`` / ``skills_list`` (read-only skill
#: recall, which the chat lane does use) requires the tool-level lane. Restored
#: per-persona through the same ``chat_lane_restore_toolsets`` knob. (T6a)
DEFAULT_CHAT_LANE_EXCLUDED_TOOLS: frozenset[str] = frozenset({"skill_manage"})


def chat_lane_restore_config_key(persona_id: str | None) -> str:
    """The EXACT root-``config.yaml`` key that un-excludes a drop for a persona.

    One spelling authority for the fix hint: the reader
    (``config.chat_lane_restore_toolsets``), this module's docstring, and every
    emitted row must name the same key, or an operator follows a hint to a
    setting that does not exist."""

    persona = str(persona_id or "").strip() or "<persona-id>"
    return f"agent_runtime.personas.{persona}.chat_lane_restore_toolsets"


@dataclass(frozen=True, slots=True)
class ChatLaneDrop:
    """One capability the chat-lane cost policy removed from a resolved lane.

    ``subject`` is the toolset or tool name; ``kind`` picks the subject key the
    row renders under (:data:`DROP_KIND_TOOLSET` ⇒ ``toolset``,
    :data:`DROP_KIND_TOOL` ⇒ ``tool``). ``restorable_via`` is the exact root
    config key that un-excludes it — the whole point of the row is that the
    reader can act on it without reading source.

    ``role`` and ``entry_point_lane`` are stamped at :meth:`row` time rather
    than stored: the DROP is a property of the policy, while the role and the
    lane are properties of the resolution that observed it (the resolver already
    knows both). Keeping them out of the value object is what lets one drop be
    reported honestly from more than one surface.
    """

    subject: str
    kind: str
    code: str
    restorable_via: str
    detail: str = ""

    def row(self, *, role: str = "", entry_point_lane: str = "") -> dict[str, Any]:
        lane = str(entry_point_lane or "").strip() or "unknown"
        role_text = str(role or "").strip()
        role_clause = f" (role '{role_text}')" if role_text else ""
        is_toolset = self.kind == DROP_KIND_TOOLSET
        noun = "toolset" if is_toolset else "tool"
        outcome = (
            "it contributes no tools there"
            if is_toolset
            else "it is not callable there"
        )
        detail = f" {self.detail}" if self.detail else ""
        return {
            "code": self.code,
            self.kind: self.subject,
            "role": role_text,
            "entry_point_lane": lane,
            "restorable_via": self.restorable_via,
            "summary": (
                f"The '{self.subject}' {noun} is resolved for this persona{role_clause}, "
                f"but the chat-lane cost policy drops it from operator / mission-chat "
                f"turns on the '{lane}' lane — {outcome}.{detail}"
            ),
            "fix_hint": (
                "By design, not a permission problem: this is a per-turn schema-cost "
                "cut applied AFTER role and permission resolution, so chasing "
                "blocked-tool counts will not find it. Seeing this row at all means "
                "the session is running BELOW the runtime default — since the "
                "2026-08-09 ruling that default is `unbounded`, which bypasses this "
                "policy entirely — so an operator either restricted this session "
                "(`harness persona permission set --mode bounded|read_only`) or "
                "configured `agent_runtime.tool_permissions.default_mode` narrower. "
                "Lift that, restore it for this persona with "
                f"`{self.restorable_via}: [{self.subject}]` in the ROOT "
                "config.yaml (un-exclusion — the role must already allow it), or run "
                "the work on a worker lane, which never passes through this policy."
            ),
        }


def chat_lane_toolset_drops(
    toolsets: Iterable[str],
    *,
    restore: Iterable[str] | None = None,
    persona_id: str | None = None,
) -> tuple[ChatLaneDrop, ...]:
    """What :func:`scope_chat_lane_toolsets` REMOVES from ``toolsets``, typed.

    The exact inverse of the filter, over the same inputs and the same resolved
    exclusion set, so the kept list and the drop list can never disagree.
    Order-preserving over the input and deduped; a restored toolset, and a
    toolset that was never in the lane's resolved set, produce nothing."""

    excluded = resolve_chat_lane_excluded_toolsets(restore)
    key = chat_lane_restore_config_key(persona_id)
    drops: list[ChatLaneDrop] = []
    seen: set[str] = set()
    for name in toolsets or ():
        text = str(name)
        if text in excluded and text not in seen:
            seen.add(text)
            drops.append(
                ChatLaneDrop(
                    subject=text,
                    kind=DROP_KIND_TOOLSET,
                    code=TOOLSET_DROPPED_BY_CHAT_LANE_POLICY,
                    restorable_via=key,
                )
            )
    return tuple(drops)


def chat_lane_tool_drops(
    *,
    restore: Iterable[str] | None = None,
    persona_id: str | None = None,
    enabled_toolsets: Iterable[str] | None = None,
    toolset_for_tool: Callable[[str], str | None] | None = None,
) -> tuple[ChatLaneDrop, ...]:
    """What :func:`chat_lane_blocked_tools` REMOVES, typed.

    A single-tool cut is only a real capability drop when the tool's toolset is
    actually enabled on the lane — blocking ``skill_manage`` on a persona with
    no ``skills`` toolset removes nothing, and claiming a drop there would be
    the same dishonesty as hiding one. Callers that can answer "which toolset
    owns this tool" pass ``toolset_for_tool`` (``model_tools.get_toolset_for_tool``)
    together with the lane's ``enabled_toolsets``; the mapping is never mirrored
    here, so it cannot drift from the registry. With either argument omitted the
    cut is reported unconditionally."""

    key = chat_lane_restore_config_key(persona_id)
    enabled = (
        None
        if enabled_toolsets is None or toolset_for_tool is None
        else {str(name) for name in enabled_toolsets}
    )
    drops: list[ChatLaneDrop] = []
    for name in chat_lane_blocked_tools(restore):
        owning = None
        if enabled is not None:
            try:
                owning = toolset_for_tool(name) if toolset_for_tool else None
            except Exception:  # pragma: no cover - a registry fault must not break accounting
                owning = None
            if owning is not None and str(owning) not in enabled:
                continue
        drops.append(
            ChatLaneDrop(
                subject=name,
                kind=DROP_KIND_TOOL,
                code=TOOL_DROPPED_BY_CHAT_LANE_POLICY,
                restorable_via=key,
                detail=(
                    f"The rest of the '{owning}' toolset is unaffected."
                    if owning
                    else ""
                ),
            )
        )
    return tuple(drops)


def chat_lane_drop_rows(
    drops: Iterable[ChatLaneDrop] | None,
    *,
    role: str = "",
    entry_point_lane: str = "",
) -> list[dict[str, Any]]:
    """Render typed drops as ``requirement_failures`` rows (see module docstring)."""

    return [
        drop.row(role=role, entry_point_lane=entry_point_lane) for drop in drops or ()
    ]


def resolve_chat_lane_excluded_toolsets(
    restore: Iterable[str] | None = None,
) -> frozenset[str]:
    """The effective excluded set: the default minus any operator-restored names.

    ``restore`` is the per-profile override (see module docstring); each entry
    that names a default-excluded toolset un-excludes it. Unknown / empty names
    are ignored (they exclude nothing anyway)."""

    restored = {str(name).strip() for name in (restore or []) if str(name).strip()}
    return DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS - restored


def scope_chat_lane_toolsets(
    toolsets: Iterable[str],
    *,
    restore: Iterable[str] | None = None,
) -> list[str]:
    """Return ``toolsets`` with the chat-lane-excluded toolsets removed.

    Order- and duplicate-preserving (the caller owns dedup); this only drops the
    excluded names. A pure function of its inputs — the single, testable policy
    seam every chat lane funnels through."""

    excluded = resolve_chat_lane_excluded_toolsets(restore)
    return [name for name in toolsets if str(name) not in excluded]


def resolve_chat_lane_excluded_tools(
    restore: Iterable[str] | None = None,
) -> frozenset[str]:
    """The effective single-tool excluded set: the default minus any restored names.

    The ``restore`` list is shared with the toolset un-exclusion path
    (``resolve_chat_lane_excluded_toolsets``): naming ``skill_manage`` there
    un-blocks the tool, while naming a toolset (``file`` / ``terminal``) is a
    no-op here (and vice versa). Unknown / empty names are ignored."""

    restored = {str(name).strip() for name in (restore or []) if str(name).strip()}
    return DEFAULT_CHAT_LANE_EXCLUDED_TOOLS - restored


def chat_lane_blocked_tools(
    restore: Iterable[str] | None = None,
) -> list[str]:
    """The single tools the chat-lane cost policy blocks, minus restored names.

    Feeds the blocked-tool-names lane (``persona_runtime._blocked_tool_names_for_chat``)
    so a bounded chat turn's schema drops these individual tools while keeping
    the rest of their toolset. Sorted for a stable, testable block list. A pure
    function of its input; the ``unbounded`` escape hatch is enforced upstream
    (that lane returns no blocks at all)."""

    return sorted(resolve_chat_lane_excluded_tools(restore))
