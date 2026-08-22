"""Entry-point lane identity + typed accounting for MCP a lane never registers.

Why this module exists
----------------------
MCP tool registration (``tools.mcp_tool.discover_mcp_tools``) is wired to the
CLI entry points that host a model turn against the operator's full MCP
surface: bare ``hermes`` / ``chat`` / ``acp`` / ``rl``, plus ``cron run|tick``,
``gateway run`` and ``mcp serve``. See ``hermes_cli/main.py`` — the
``_AGENT_COMMANDS`` / ``_AGENT_SUBCOMMANDS`` gate in front of
``_prepare_agent_startup`` is the single registrar.

``hermes harness ...`` — the harness / mission-chat / serve lane — is
deliberately EXCLUDED from that gate, and must stay excluded. The harness lane
is a fast, deterministic control plane; it cannot pay an MCP connect budget on
every command.

The exclusion is by design. Its INVISIBILITY was the defect: a persona whose
profile declares MCP servers resolved tool visibility on the harness lane with
``requirement_failures: []`` — the capability drop was reported as *nothing at
all*. Operators and agents then misread the plain absence of MCP tools as a
permission problem and burned turns on permission-mode goose chases ("Blocked
21 tools" red herrings, 2026-07-25).

This module makes that drop TYPED. It registers nothing, unblocks nothing and
changes no tool wiring — observability only. It lets the resolver state, in a
structured row, "these declared MCP servers are not registered on THIS lane,
and here is why."
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable, Sequence

#: ``requirement_failures[].code`` for the drop this module accounts for.
MCP_NOT_REGISTERED_ON_LANE = "mcp_not_registered_on_lane"

HARNESS_LANE = "harness"
UNKNOWN_LANE = "unknown"

#: Top-level CLI commands whose startup path reaches ``discover_mcp_tools()``.
#:
#: MIRROR of ``hermes_cli/main.py::_AGENT_COMMANDS`` (``chat``/``acp``/``rl``
#: and the bare no-command chat launch) plus the ``_AGENT_SUBCOMMANDS`` owners
#: (``cron run|tick``, ``gateway run``, ``mcp serve``). We mirror rather than
#: import because ``hermes_cli.main`` is an upstream module and importing it
#: from the resolver would drag the whole CLI into every visibility resolve.
#:
#: Mirrored at top-level-command granularity on purpose: ``cron list`` does not
#: register, but treating the whole ``cron`` command as registering is the
#: CONSERVATIVE direction — it can only suppress a row, never invent one.
#: ``tests/agent_runtime/test_mcp_lane_visibility.py`` guards the mirror
#: against upstream drift.
MCP_REGISTERING_LANES = frozenset({"chat", "acp", "rl", "cron", "gateway", "mcp"})

#: Lane tokens we can recognize positionally in ``sys.argv``.
_RECOGNIZED_LANES = MCP_REGISTERING_LANES | {HARNESS_LANE}

#: Escape hatch for embedders (the Launcher's serve child, QA harnesses, test
#: rigs) that own their own argv and want to label the lane honestly.
ENTRY_POINT_LANE_ENV = "HERMES_ENTRY_POINT_LANE"

#: Toolset-name prefix ``tools/mcp_tool.py`` registers every MCP server under
#: (``toolset_name = f"mcp-{name}"``). Reading the registry is how we prove
#: registration actually happened in THIS process, instead of assuming it from
#: the lane label.
_MCP_TOOLSET_PREFIX = "mcp-"

_lane_override: str | None = None


def set_entry_point_lane(lane: str | None) -> None:
    """Pin this process's entry-point lane (highest-precedence signal).

    For entry points that know what they are and do not want to be inferred
    from argv. Passing ``None`` clears the pin.
    """

    global _lane_override
    text = str(lane or "").strip()
    _lane_override = text or None


def current_entry_point_lane(argv: Sequence[str] | None = None) -> str:
    """Resolve the lane this process is running on.

    Precedence: explicit pin > ``HERMES_ENTRY_POINT_LANE`` > argv inference >
    ``"unknown"``. Never raises — a lane label must not be able to break a
    visibility resolve.
    """

    if _lane_override:
        return _lane_override
    try:
        env = str(os.environ.get(ENTRY_POINT_LANE_ENV, "") or "").strip()
    except Exception:  # pragma: no cover - os.environ access is not expected to fail
        env = ""
    if env:
        return env
    return _lane_from_argv(sys.argv if argv is None else argv)


def _lane_from_argv(argv: Sequence[str] | None) -> str:
    """First recognized lane token in ``argv`` after the program name.

    Positional rather than parsed on purpose: ``hermes -p launcher-qa chat``
    puts a flag value before the command, so "first non-flag token" would read
    ``launcher-qa`` as the lane. Matching only against the known lane tokens
    keeps that honest, and anything unrecognized stays ``unknown`` instead of
    being guessed.
    """

    try:
        tokens = list(argv or [])[1:]
    except Exception:  # pragma: no cover - defensive
        return UNKNOWN_LANE
    for token in tokens:
        text = str(token or "").strip()
        if text in _RECOGNIZED_LANES:
            return text
    return UNKNOWN_LANE


def lane_registers_mcp(lane: str | None) -> bool:
    """Does this lane's startup path reach ``discover_mcp_tools()``?"""

    return str(lane or "").strip() in MCP_REGISTERING_LANES


def registered_mcp_server_names() -> frozenset[str]:
    """MCP server names with tools registered in THIS process.

    Ground truth, read from the tool registry's ``mcp-<server>`` toolsets — not
    inferred from the lane. Reading the registry costs nothing extra (the
    resolver already imports it) and, unlike importing ``tools.mcp_tool``, does
    not drag in the MCP SDK.
    """

    try:
        from tools.registry import registry

        names = registry.get_registered_toolset_names()
    except Exception:  # pragma: no cover - registry is always importable in-process
        return frozenset()
    return frozenset(
        str(name)[len(_MCP_TOOLSET_PREFIX) :]
        for name in names or []
        if str(name).startswith(_MCP_TOOLSET_PREFIX)
    )


def mcp_lane_requirement_failures(
    *,
    declared_servers: Iterable[str],
    lane: str | None = None,
    registered_servers: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Typed ``requirement_failures`` rows for MCP this lane never registered.

    Pure policy over three facts, so the transition table is unit-testable
    without a live registry or a live CLI process:

    * ``declared_servers`` — what the persona/profile says it needs.
    * ``lane`` — which entry point resolved this visibility.
    * ``registered_servers`` — what actually registered in this process.

    Emits ONE row per dropped server, and only when the lane is not an MCP
    registrar AND that declared server is genuinely absent from the registry. A
    lane that DOES register but whose server failed to connect is a different
    failure class (a connect/config fault, already surfaced as
    ``mcp_attention`` profile readiness) and is deliberately not claimed here.

    Row shape is ``{code, server, summary, fix_hint}`` — deliberately the same
    shape as ``machine_roots.PathTokenIssue.row()``, per
    ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/mission-chat-mcp-admission.md`` §D, so operator
    surfaces that already render typed issue rows need no new case.
    ``entry_point_lane`` rides along because "not registered" is only actionable
    once you know WHERE it was not registered.
    """

    declared = _clean(declared_servers)
    if not declared:
        return []
    resolved_lane = str(lane or "").strip() or current_entry_point_lane()
    if lane_registers_mcp(resolved_lane):
        return []
    registered = (
        registered_mcp_server_names()
        if registered_servers is None
        else frozenset(_clean(registered_servers))
    )
    return [
        {
            "code": MCP_NOT_REGISTERED_ON_LANE,
            "server": name,
            "entry_point_lane": resolved_lane,
            "summary": (
                f"'{name}' is declared for this persona, but MCP tools are not "
                f"registered on the '{resolved_lane}' lane — it contributes no tools here."
            ),
            "fix_hint": (
                "By design, not a permission problem: no permission mode can expose a "
                "tool that was never registered, so do not chase permission modes or "
                "blocked-tool counts. Only the agent entry points (chat/acp/rl, "
                "cron run|tick, gateway run, mcp serve) run MCP discovery; the harness "
                "lane never calls discover_mcp_tools(). An operator can run the persona "
                "on an MCP-registering lane (e.g. `hermes -p <profile> chat`); there is "
                f"no harness-side fallback contract for {name} to take on this lane, so "
                "otherwise report the drop and finish the turn without it."
            ),
        }
        for name in declared
        if name not in registered
    ]


# ── the agent-visible half of the same fact (G5) ────────────────────────────
#
# The rows above are the OPERATOR's account. Until 2026-07-26 that was the only
# account: an agent whose declared server is dark on this lane saw a tool list
# with no ``mcp__<server>__*`` entries and NO explanation, and — being a language
# model — invented alternatives. That is how ``pwsh -File`` calls end up in agent
# output, and why the launcher repo needs a grep gate forbidding them. The
# admission design (§D3, W3) says it plainly: telling the agent the truth is
# cheaper than fencing every workaround it can invent.
#
# The admission module renders this line when admission is ENABLED. With the
# flag off — the default, and every deployment today — admission is inert by
# design and renders nothing, so the drop stayed operator-only. This renderer is
# the flag-off half, and it costs what the R0 row costs: no root-config load, no
# profile read, no MCP SDK import.

#: Bullet prefix, so the line sits in the same volatile-tail list the wall-budget
#: line renders into. Byte-identical to ``mcp_admission._ADMISSION_LINE_PREFIX``
#: on purpose — the agent must hear ONE voice about MCP whether the flag is on or
#: off. ``test_mcp_lane_agent_context_line.py`` pins the two against drift.
MCP_CONTEXT_LINE_PREFIX = "- MCP tools:"

#: Everything after the ``<server> (<code>)`` detail. Shared verbatim with
#: ``mcp_admission.render_mcp_admission_line``.
MCP_CONTEXT_LINE_TAIL = (
    " — declared for this persona but NOT available on this turn, so no "
    "mcp__<server>__* tools for it are in your tool list. This is a capability "
    "fact, not a permission problem: do not retry, do not hunt for a permission "
    "mode, and do not substitute a shell/PowerShell workaround or a second lane. "
    "There is no harness-side fallback contract to take instead — that lane no "
    "longer exists, so this route is closed for the turn. Say plainly in your "
    "reply that the tools were unavailable and what you could not verify without "
    "them, then finish the turn. Only an operator can lift this, by fixing the "
    "condition the code above names in the root or persona-profile config.yaml."
)


def render_mcp_lane_line(rows: Iterable[dict[str, Any]] | None) -> str:
    """One compact, agent-visible line for the drops ``rows`` accounts for.

    Takes the SAME rows the operator's ``requirement_failures`` carries — one
    producer, two surfaces, so the agent and the operator can never be told
    different things. Returns ``""`` when there is nothing to say: a turn that
    HAS its tools must not pay a line explaining a mechanism it never met.
    """

    ordered: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        server = str(row.get("server") or "").strip()
        code = str(row.get("code") or "").strip()
        if server and code:
            ordered.setdefault(server, code)
    if not ordered:
        return ""
    detail = ", ".join(f"{server} ({code})" for server, code in ordered.items())
    return f"{MCP_CONTEXT_LINE_PREFIX} {detail}{MCP_CONTEXT_LINE_TAIL}"


def mission_chat_mcp_lane_line(persona: Any, *, lane: str | None = None) -> str:
    """The agent-visible line for what THIS persona declared and this lane dropped.

    Deliberately narrower than the operator's rows, in both directions that
    matter:

    * **Declaration source.** Reads ``_effective_required_mcp_servers`` — the
      persona's own ``required_mcp_servers`` plus the existing role policy — and
      NOT ``declared_mcp_server_names``, which additionally parses the profile's
      ``config.yaml``. Two reasons, and the cheapness is the lesser one: the
      admission design fixes that a flag-off turn pays no profile read, and a
      server that is merely CONFIGURED in the ambient profile was never
      something this agent was going to reach for — telling it about that is
      noise, telling it about what it requires is the fix. The operator's
      ``requirement_failures`` still carries the wider union.
    * **One policy authority.** The role policy (``qa`` + visual proof ⇒
      ``launcher_qa``) is imported, never re-implemented — the design's standing
      "do not write a second copy" rule.

    Never raises: a context line must not be able to fail a turn.
    """

    try:
        # Local import: ``profile_readiness`` pulls in ``hermes_cli`` and this
        # module is imported by every visibility resolve. Private-but-canonical
        # is the deliberate trade — a second copy of the role policy is worse
        # than reaching across for the first one.
        from .profile_readiness import _effective_required_mcp_servers

        declared = _effective_required_mcp_servers(persona)
    except Exception:  # pragma: no cover - defensive; a declaration probe is best-effort
        return ""
    if not declared:
        return ""
    try:
        return render_mcp_lane_line(
            mcp_lane_requirement_failures(declared_servers=declared, lane=lane)
        )
    except Exception:  # pragma: no cover - defensive
        return ""


def _clean(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out
