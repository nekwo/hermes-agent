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
gating (``ALLOWED_TOOLSETS_BY_ROLE``), persona blocklists, and permission mode
all run first and upstream; this is the last, cost-motivated narrowing applied
only at the chat-lane chokepoint (``persona_runtime._enabled_toolsets_for_chat``).

Worker / dev task lanes never go through here — they resolve toolsets via
``effective_toolsets(persona)`` directly (``persona_runtime.run_persona``,
``root_node_engine``, ``node_tools``); only the two operator/mission chat call
sites apply this policy, so there is exactly one chat-lane authority.

Per-profile override
--------------------
A deployment that genuinely wants one of these toolsets on a persona's chat lane
restores it per-persona via config:

    agent_runtime:
      personas:
        neko_supervisor:
          chat_lane_restore_toolsets: [browser]   # keep browser on Neko's chat

Restore is un-exclusion, not a grant: a restored toolset is only kept if it was
already in the lane's resolved set (role gating still applies) — you cannot hand
a PM chat the browser toolset through this knob.
"""

from __future__ import annotations

from collections.abc import Iterable


#: Toolsets a conversational chat lane never needs, dropped by default so their
#: schemas stop riding every turn. Ordered by savings: ``browser`` is 10
#: ``browser_*`` tools (+ ``browser_vision``), ``vision`` is ``vision_analyze``,
#: ``code_execution`` is ``execute_code``, ``debugging`` is the interactive
#: debugger tools. These are exactly the "browser / vision / heavy-dev" toolsets.
DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS: frozenset[str] = frozenset(
    {
        "browser",
        "vision",
        "code_execution",
        "debugging",
    }
)


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
