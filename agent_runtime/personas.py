from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from pathlib import Path

from .models import AgentPersona


class AgentRole(StrEnum):
    PM = "pm"
    DEV = "dev"
    QA = "qa"
    ALICE_SUPERVISOR = "alice_supervisor"


class AutonomyLevel(StrEnum):
    PROPOSE_ONLY = "propose_only"
    APPLY_WITH_REVIEW = "apply_with_review"
    AUTONOMOUS = "autonomous"


# Roles/personas retired from the product flow. A persona instance persisted under one
# of these — historically the legacy ``pm`` slot (``shared_harness_overlay.md``: "Treat
# PM names ... as legacy compatibility only; do not present PM as the product flow") —
# must never render as a live product agent. The persona-instance reconciler prunes such
# rows (archive, never delete). Single-sourced here so liveness/roster checks resolve a
# mothballed role through this set instead of hand-rolling ``role == "pm"`` string tests.
MOTHBALLED_ROLES: frozenset[AgentRole] = frozenset({AgentRole.PM})
MOTHBALLED_ROLE_TOKENS: frozenset[str] = frozenset(role.value for role in MOTHBALLED_ROLES)
MOTHBALLED_PERSONA_IDS: frozenset[str] = frozenset({AgentRole.PM.value})


# Synthetic operator-channel personas built from a raw Hermes profile carry the
# "profile" role sentinel (see ``hermes_cli.harness._persona_by_id``). They are not
# a typed mission slot, so it is not a real ``AgentRole`` — passing it straight into
# ``AgentRole(...)`` raises ``'profile' is not a valid AgentRole`` and kills the whole
# operator chat turn. For capability/toolset resolution on that chat path a profile
# behaves as the supervisor class: the most permissive ceiling, so the profile's own
# configured toolsets pass the ``validate_toolsets`` intersection unchanged.
PROFILE_ROLE_SENTINEL = "profile"
def coerce_agent_role(role: AgentRole | str | None) -> AgentRole | str:
    """Resolve a persona role token to an ``AgentRole``.

    Known legacy values retain their enum spelling for compatibility. Unknown
    values remain data: they are never dropped or coerced into a hardcoded role.
    """

    if isinstance(role, AgentRole):
        return role
    text = str(role or "").strip()
    try:
        return AgentRole(text)
    except ValueError:
        return text

PROFILE_CHAT_FALLBACK_TOOLSETS = (
    "file",
    "search",
    "terminal",
    "code_execution",
    "session_search",
    "skills",
    "agent_chat",
    "board",
)


# Fork registry hygiene (T6c, 2026-07-18). Upstream toolsets the fork's effective
# registry must never resolve on ANY agent-runtime lane: the whole ``kanban``
# toolset (9 tools — superseded by the fork board/mission system) and the
# ``feishu_doc`` + ``feishu_drive`` toolsets (5 tools — an irrelevant Feishu/Lark
# integration). The upstream tool files stay untouched (fork-sync cleanliness);
# this fork-owned constant is the deregistration mechanism. It is enforced in TWO
# places so no lane escapes:
#   1. folded into ``PERSONA_BLOCKED_TOOLS`` below → the persona chat/run lanes and
#      the ``tool_visibility`` permission-preview surface, and
#   2. unioned at the ``profile_runner`` agent-construction chokepoint → every
#      lane, including the worker / root-node lanes that pass
#      ``blocked_tool_names=[]`` (node_tools.py / root_node_engine.py).
# ``delegate_task`` and ``memory`` are deliberately NOT here — operator ruling:
# keep them registered (both are parallel-authority surfaces; a future lane that
# enables either owns reconciling delegation-vs-harness-dispatch / upstream-memory-
# vs-profile-memory).
REGISTRY_HYGIENE_BLOCKED_TOOLS = frozenset(
    {
        # kanban toolset (9)
        "kanban_show",
        "kanban_list",
        "kanban_create",
        "kanban_complete",
        "kanban_block",
        "kanban_link",
        "kanban_comment",
        "kanban_unblock",
        "kanban_heartbeat",
        # feishu_doc toolset (1)
        "feishu_doc_read",
        # feishu_drive toolset (4)
        "feishu_drive_list_comments",
        "feishu_drive_list_comment_replies",
        "feishu_drive_reply_comment",
        "feishu_drive_add_comment",
    }
)


PERSONA_BLOCKED_TOOLS = frozenset(
    {
        "delegate_task",
        "clarify",
        "memory",
        "send_message",
        "cronjob",
    }
) | REGISTRY_HYGIENE_BLOCKED_TOOLS

def role_from_persona(persona: AgentPersona) -> AgentRole | str:
    return coerce_agent_role(persona.role)


def validate_toolsets(role: AgentRole | str, configured: list[str]) -> list[str]:
    return list(dict.fromkeys(str(toolset).strip() for toolset in configured if str(toolset).strip()))


def blocked_tool_names(persona: AgentPersona) -> frozenset[str]:
    return PERSONA_BLOCKED_TOOLS


def effective_toolsets(persona: AgentPersona) -> list[str]:
    return validate_toolsets(role_from_persona(persona), persona.toolsets)


def profile_chat_toolsets(profile_id: str, personas: list[AgentPersona] | tuple[AgentPersona, ...] | None = None) -> list[str]:
    """Resolve the toolsets for a raw profile-backed operator chat persona.

    A ``profile:<name>`` chat is not a typed blueprint slot, but when it backs a
    known mission persona (Alice/Neko is the common case) it should inherit that
    persona's production tool surface instead of a reduced legacy profile-chat
    subset. If no typed persona owns the profile, fall back to the supervisor
    chat ceiling so the operator channel remains command-capable.
    """

    profile = str(profile_id or "").strip()
    matching = next(
        (
            persona
            for persona in personas or []
            if str(getattr(persona, "hermes_profile", "") or "").strip() == profile
        ),
        None,
    )
    toolsets = list(getattr(matching, "toolsets", []) or []) if matching is not None else list(PROFILE_CHAT_FALLBACK_TOOLSETS)
    for toolset in PROFILE_CHAT_FALLBACK_TOOLSETS:
        if toolset == "agent_chat" and toolset not in toolsets:
            toolsets.append(toolset)
    return [toolset for toolset in toolsets if toolset]


def all_registered_toolsets() -> list[str]:
    from model_tools import get_available_toolsets

    return sorted(str(name) for name in get_available_toolsets().keys())


def load_bundled_prompt(role: AgentRole | str) -> str:
    token = role.value if isinstance(role, AgentRole) else str(role or "").strip()
    path = Path(__file__).with_name("prompts") / f"{token}.md"
    return path.read_text(encoding="utf-8")


# ── profile → persona promotion ───────────────────────────────────────────────
# Re-homed here from ``agent_runtime/blueprints/resolve.py`` (mission-lane removal,
# S1). It only lived under ``blueprints/`` by accident of filing: promoting a raw
# Hermes profile into a persisted persona is a persona-lifecycle operation, not
# stage routing, and its live callers are the blueprint slot resolver *and* the
# upstream ``POST /api/profiles/{name}/promote`` endpoint, which has nothing to do
# with stage graphs. It has to outlive the blueprint package.
#
def promote_profile_to_persona(
    profile_name: str,
    *,
    slot_role: str,
    personas: dict[str, AgentPersona] | None = None,
    agent_store=None,
) -> AgentPersona:
    """Mint and persist a persona that wraps the raw Hermes profile ``profile_name``.

    A profile is only a *template* — it carries no orchestration contract — so it
    cannot act as an agent on its own. Promotion clones the ``slot_role`` template
    persona (so model / provider / toolsets / system prompt are valid) and points
    ``hermes_profile`` at the profile.

    Stores are imported lazily: ``agent_runtime.config`` imports this module, so a
    top-level import would close a cycle.
    """

    from agent_runtime.store import AgentStore

    store = agent_store if agent_store is not None else AgentStore()
    known = dict(personas or {})
    if not known:
        try:
            for persona in store.list_all():
                known[persona.id] = persona
        except Exception:
            pass
        from agent_runtime.config import ensure_persisted_personas

        for persona in ensure_persisted_personas():
            known.setdefault(persona.id, persona)
    template = next(
        (
            persona
            for persona in known.values()
            if str(getattr(persona, "role", "") or "").strip() == str(slot_role or "").strip()
        ),
        None,
    ) or known.get(str(slot_role or "").strip()) or next(iter(known.values()), None)
    if template is None:
        raise ValueError(
            f"cannot promote profile {profile_name!r}: no template persona for role {slot_role!r}"
        )
    new_id = profile_name if profile_name not in known else f"{profile_name}_{slot_role}"
    persona = replace(
        template,
        id=new_id,
        display_name=f"{profile_name} ({slot_role})",
        hermes_profile=profile_name,
        skills=list(template.skills),
        toolsets=list(template.toolsets),
        required_mcp_servers=list(template.required_mcp_servers),
        readiness={},
    )
    return store.save(persona)
