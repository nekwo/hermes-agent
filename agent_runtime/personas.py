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


# Base-profile foundation (2026-07): every agent is a free Hermes profile. The typed
# pipeline personas (neko/dev/backend_dev/qa) are mothballed as dormant templates in
# ``default_personas()`` and are no longer seeded. The running store seeds ONE base
# profile; other on-disk profiles surface as available personas and are chattable on demand.
BASE_PERSONA_ID = "base"
DEFAULT_PERSONA_IDS = frozenset({BASE_PERSONA_ID})


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
PROFILE_CHAT_ROLE = AgentRole.ALICE_SUPERVISOR


def coerce_agent_role(role: AgentRole | str | None) -> AgentRole:
    """Resolve a persona role token to an ``AgentRole``.

    Tolerates the synthetic ``"profile"`` sentinel (mapping it to the supervisor
    class); every other value still resolves strictly so a genuinely-misconfigured
    typed persona surfaces instead of being silently coerced.
    """

    if isinstance(role, AgentRole):
        return role
    text = str(role or "").strip()
    if text.lower() == PROFILE_ROLE_SENTINEL:
        return PROFILE_CHAT_ROLE
    return AgentRole(text)


# NOTE: the ``board`` toolset (advisory Mission Board card tools) is allowed for
# every mission role — the board is nudged-never-forced, so any agent may track
# follow-up work as a card. It never mutates goal state.
ALLOWED_TOOLSETS_BY_ROLE: dict[AgentRole, frozenset[str]] = {
    AgentRole.PM: frozenset({"file", "session_search", "todo", "skills", "agent_chat", "board"}),
    AgentRole.DEV: frozenset({"file", "search", "terminal", "session_search", "todo", "code_execution", "skills", "agent_chat", "board"}),
    AgentRole.QA: frozenset({"file", "search", "terminal", "browser", "vision", "session_search", "skills", "agent_chat", "board"}),
    AgentRole.ALICE_SUPERVISOR: frozenset(
        {
            "file",
            "search",
            "terminal",
            "code_execution",
            "browser",
            "vision",
            "web",
            "session_search",
            "todo",
            "skills",
            "agent_chat",
            "board",
        }
    ),
}

DEFAULT_SUPERVISOR_PERSONA_ID = "neko_supervisor"

# The Launcher-owned install path deliberately opts back into the typed Mission
# Control team. Every advertised agent must have a real Hermes profile behind
# it; these bindings are the cross-stack install contract consumed by
# ``harness init --with-bundled-personas`` and the Launcher's verifier.
BUNDLED_PERSONA_PROFILES: dict[str, str] = {
    DEFAULT_SUPERVISOR_PERSONA_ID: BASE_PERSONA_ID,
    "dev": "launcher-dev",
    "backend_dev": "backend-dev",
    "qa": "qa",
}
BUNDLED_PERSONA_IDS = frozenset(BUNDLED_PERSONA_PROFILES)

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

PER_ROLE_TOOL_DENIES: dict[AgentRole, frozenset[str]] = {
    AgentRole.PM: frozenset({"write_file", "patch", "terminal"}),
    AgentRole.DEV: frozenset({"send_message"}),
    AgentRole.QA: frozenset({"write_file", "patch"}),
    AgentRole.ALICE_SUPERVISOR: frozenset({"send_message"}),
}


def role_from_persona(persona: AgentPersona) -> AgentRole:
    return coerce_agent_role(persona.role)


def validate_toolsets(role: AgentRole | str, configured: list[str]) -> list[str]:
    resolved_role = role if isinstance(role, AgentRole) else AgentRole(role)
    allowed = ALLOWED_TOOLSETS_BY_ROLE[resolved_role]
    return [toolset for toolset in configured if toolset in allowed]


def blocked_tool_names(persona: AgentPersona) -> frozenset[str]:
    role = role_from_persona(persona)
    return PERSONA_BLOCKED_TOOLS | PER_ROLE_TOOL_DENIES[role]


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


def default_personas() -> list[AgentPersona]:
    return [
        AgentPersona(
            id=DEFAULT_SUPERVISOR_PERSONA_ID,
            display_name="Neko Mission Lead",
            role=AgentRole.ALICE_SUPERVISOR.value,
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=["file", "search", "terminal", "session_search", "code_execution", "todo", "skills"],
            system_prompt_path="agent_runtime/prompts/alice_supervisor.md",
            autonomy=AutonomyLevel.PROPOSE_ONLY.value,
            hermes_profile=None,
            skills=["harness-mission-lead", "harness-continuity", "harness-runtime-model"],
        ),
        AgentPersona(
            id="dev",
            display_name="Launcher Dev Agent",
            role=AgentRole.DEV.value,
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=["file", "search", "terminal", "session_search", "code_execution", "skills"],
            system_prompt_path="agent_runtime/prompts/dev.md",
            autonomy=AutonomyLevel.AUTONOMOUS.value,
            hermes_profile=BUNDLED_PERSONA_PROFILES["dev"],
            skills=[
                "harness-continuity",
                "harness-dev-delivery",
                "launcher-analyze-proof",
                # Same pairing as the seeded `qa` row: `dev` joined the MCP
                # admission floor on 2026-07-29, so it is admitted `launcher_qa`
                # and must be granted that surface's operating manual for
                # `MCP_OPERATING_SKILLS` to preload it.
                "launcher-stagec-mcp-screenshot",
            ],
            repo_scope_label="EterniaLauncher",
        ),
        AgentPersona(
            id="backend_dev",
            display_name="Backend Dev Agent",
            role=AgentRole.DEV.value,
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=["file", "search", "terminal", "session_search", "code_execution", "skills"],
            system_prompt_path="agent_runtime/prompts/dev.md",
            autonomy=AutonomyLevel.AUTONOMOUS.value,
            hermes_profile=BUNDLED_PERSONA_PROFILES["backend_dev"],
            skills=[
                "harness-continuity",
                "harness-dev-delivery",
            ],
            repo_scope="X:/Unreal Engine/Engine/EterniaBackend/eternia-backend",
            repo_scope_label="EterniaBackend",
        ),
        AgentPersona(
            id="qa",
            display_name="QA Agent",
            role=AgentRole.QA.value,
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=["file", "search", "terminal", "browser", "vision", "session_search", "skills"],
            system_prompt_path="agent_runtime/prompts/qa.md",
            autonomy=AutonomyLevel.AUTONOMOUS.value,
            hermes_profile=BUNDLED_PERSONA_PROFILES["qa"],
            # `launcher-stagec-mcp-screenshot` is the operating manual for the
            # `launcher_qa` MCP surface that `mcp_admission.MCP_OPERATING_SKILLS`
            # auto-preloads for admitted QA turns. That resolver requires the
            # skill to be GRANTED as well as admitted, so a deployment seeded
            # without this grant admits the tools and ships no manual — the exact
            # gap that made a QA turn rediscover the sparse-feed screenshot
            # refusal from scratch (2026-07-29).
            skills=["harness-qa-verdict", "launcher-stagec-mcp-screenshot"],
        ),
    ]


def seed_personas() -> list[AgentPersona]:
    """The personas actually materialized into the running store.

    Base-profile foundation: seed exactly ONE free base profile as the default
    agent. Other on-disk Hermes profiles surface as available personas and are
    chattable on demand; they are not seeded here. ``default_personas()`` is
    retained as dormant typed-pipeline templates for the eventual pipeline
    rebuild and is intentionally unused by the seed.

    The base profile receives the chat fallback toolsets directly.
    """

    base_toolsets = list(PROFILE_CHAT_FALLBACK_TOOLSETS)
    return [
        AgentPersona(
            id=BASE_PERSONA_ID,
            display_name="Base Agent",
            role=PROFILE_ROLE_SENTINEL,
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=base_toolsets,
            system_prompt_path="",
            autonomy=AutonomyLevel.PROPOSE_ONLY.value,
            hermes_profile=BASE_PERSONA_ID,
            # Base is the operator's default agent, so it carries the Mission Control
            # runtime-model skill (view/operate goals + graphs, incl. agent_topology),
            # installed into the base profile so its chat can load it on demand. It is NOT
            # a typed pipeline worker, so it does not carry harness-dev/qa delivery skills.
            skills=["harness-runtime-model"],
            include_profile_memory=True,
        )
    ]


def load_bundled_prompt(role: AgentRole | str) -> str:
    resolved_role = role if isinstance(role, AgentRole) else AgentRole(role)
    path = Path(__file__).with_name("prompts") / f"{resolved_role.value}.md"
    return path.read_text(encoding="utf-8")


# ── profile → persona promotion ───────────────────────────────────────────────
# Re-homed here from ``agent_runtime/blueprints/resolve.py`` (mission-lane removal,
# S1). It only lived under ``blueprints/`` by accident of filing: promoting a raw
# Hermes profile into a persisted persona is a persona-lifecycle operation, not
# stage routing, and its live callers are the blueprint slot resolver *and* the
# upstream ``POST /api/profiles/{name}/promote`` endpoint, which has nothing to do
# with stage graphs. It has to outlive the blueprint package.
#
# Role → the template persona id whose model/provider/toolsets/system prompt the
# promoted persona is cloned from. Unknown roles fall back to ``dev``.
_ROLE_TEMPLATE = {
    "builder": "dev",
    "dev": "dev",
    "backend_dev": "backend_dev",
    "verifier": "qa",
    "reviewer": "qa",
    "qa": "qa",
    "lead": "neko_supervisor",
    "neko": "neko_supervisor",
    "pm": "neko_supervisor",
    "specialist": "dev",
    "harness": "dev",
    "human": "dev",
}


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
    template_id = _ROLE_TEMPLATE.get(slot_role, "dev")
    template = known.get(template_id) or known.get("dev") or next(iter(known.values()), None)
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
