from __future__ import annotations

from dataclasses import replace
from enum import StrEnum

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
# of these — historically the legacy ``pm`` slot, treated as legacy compatibility only —
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
# operator chat turn. Unknown roles remain data and ``validate_toolsets`` applies
# no role-specific ceiling.
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

# Fork registry hygiene (T6c, 2026-07-18). Upstream toolsets the fork's effective
# registry must never resolve on ANY agent-runtime lane: the whole ``kanban``
# toolset (12 tools — superseded by the fork board/mission system) and the
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
        # kanban toolset (12)
        "kanban_show",
        "kanban_list",
        "kanban_create",
        "kanban_complete",
        "kanban_block",
        "kanban_link",
        "kanban_comment",
        "kanban_unblock",
        "kanban_heartbeat",
        # +3 from the 2026-07-31 upstream sync: the kanban card-attachment
        # verbs. Blocked for the same reason as the rest of the toolset —
        # upstream kanban itself is KEPT (it is not the fork board), it just
        # must not resolve on an agent-runtime lane.
        "kanban_attach",
        "kanban_attach_url",
        "kanban_attachments",
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

    Exact persona ids outrank profile ownership. A unique profile owner may
    supply the declared toolsets; an unowned or multiply-owned profile inherits
    nothing. Universal chat capabilities are added later by the chat runtime.
    """

    profile = str(profile_id or "").strip()
    declared = list(personas or [])
    exact = next(
        (
            persona
            for persona in declared
            if str(getattr(persona, "id", "") or "").strip()
            in {profile, f"profile:{profile}"}
        ),
        None,
    )
    profile_matches = [
        persona
        for persona in declared
        if str(getattr(persona, "hermes_profile", "") or "").strip() == profile
    ]
    matching = exact or (profile_matches[0] if len(profile_matches) == 1 else None)
    toolsets = list(getattr(matching, "toolsets", []) or []) if matching is not None else []
    return [toolset for toolset in toolsets if toolset]


def all_registered_toolsets() -> list[str]:
    from model_tools import get_available_toolsets

    return sorted(str(name) for name in get_available_toolsets().keys())


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

    A profile is only a *template* — it carries no persona record — so promotion
    persists that record and points ``hermes_profile`` at the profile. When a
    matching persisted persona exists its settings are cloned; otherwise the
    supplied role remains data and runtime defaults provide the chat settings.

    Stores are imported lazily: ``agent_runtime.config`` imports this module, so a
    top-level import would close a cycle.
    """

    from agent_runtime.store import AgentStore

    store = agent_store if agent_store is not None else AgentStore()
    known = dict(personas or {})
    explicit_single_template = (
        next(iter(personas.values())) if personas is not None and len(personas) == 1 else None
    )
    cfg = None
    if not known:
        try:
            for persona in store.list_all():
                known[persona.id] = persona
        except Exception:
            pass
        from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config

        cfg = load_agent_runtime_config()
        for persona in ensure_persisted_personas(cfg):
            known.setdefault(persona.id, persona)
    template = next(
        (
            persona
            for persona in known.values()
            if str(getattr(persona, "role", "") or "").strip() == str(slot_role or "").strip()
        ),
        None,
    ) or known.get(str(slot_role or "").strip()) or explicit_single_template
    new_id = profile_name if profile_name not in known else f"{profile_name}_{slot_role}"
    if template is not None:
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
    else:
        if cfg is None:
            from agent_runtime.config import load_agent_runtime_config

            cfg = load_agent_runtime_config()
        role = str(slot_role or "").strip() or PROFILE_ROLE_SENTINEL
        persona = AgentPersona(
            id=new_id,
            display_name=f"{profile_name} ({role})",
            role=role,
            model=cfg.default_model,
            provider=cfg.default_provider,
            api_mode=cfg.default_api_mode,
            toolsets=profile_chat_toolsets(profile_name),
            system_prompt_path="",
            autonomy=AutonomyLevel.PROPOSE_ONLY.value,
            hermes_profile=profile_name,
            include_profile_memory=True,
        )
    return store.save(persona)
