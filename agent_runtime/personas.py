from __future__ import annotations

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


DEFAULT_PERSONA_IDS = frozenset({"neko_supervisor", "dev", "backend_dev", "qa"})


ALLOWED_TOOLSETS_BY_ROLE: dict[AgentRole, frozenset[str]] = {
    AgentRole.PM: frozenset({"file", "session_search", "todo", "skills"}),
    AgentRole.DEV: frozenset({"file", "search", "terminal", "session_search", "todo", "code_execution", "skills"}),
    AgentRole.QA: frozenset({"file", "search", "terminal", "browser", "vision", "session_search", "skills"}),
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
            "mission_goal",
        }
    ),
}

DEFAULT_SUPERVISOR_PERSONA_ID = "neko_supervisor"


PERSONA_BLOCKED_TOOLS = frozenset(
    {
        "delegate_task",
        "clarify",
        "memory",
        "send_message",
        "cronjob",
        "kanban_show",
        "kanban_list",
        "kanban_create",
        "kanban_complete",
        "kanban_block",
        "kanban_link",
        "kanban_comment",
        "kanban_unblock",
        "kanban_heartbeat",
    }
)

PER_ROLE_TOOL_DENIES: dict[AgentRole, frozenset[str]] = {
    AgentRole.PM: frozenset({"write_file", "patch", "terminal"}),
    AgentRole.DEV: frozenset({"send_message"}),
    AgentRole.QA: frozenset({"write_file", "patch"}),
    AgentRole.ALICE_SUPERVISOR: frozenset({"send_message"}),
}


def role_from_persona(persona: AgentPersona) -> AgentRole:
    return persona.role if isinstance(persona.role, AgentRole) else AgentRole(persona.role)


def validate_toolsets(role: AgentRole | str, configured: list[str]) -> list[str]:
    resolved_role = role if isinstance(role, AgentRole) else AgentRole(role)
    allowed = ALLOWED_TOOLSETS_BY_ROLE[resolved_role]
    return [toolset for toolset in configured if toolset in allowed]


def blocked_tool_names(persona: AgentPersona) -> frozenset[str]:
    role = role_from_persona(persona)
    return PERSONA_BLOCKED_TOOLS | PER_ROLE_TOOL_DENIES[role]


def effective_toolsets(persona: AgentPersona) -> list[str]:
    return validate_toolsets(role_from_persona(persona), persona.toolsets)


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
            toolsets=["file", "search", "terminal", "session_search", "code_execution", "todo", "skills", "mission_goal"],
            system_prompt_path="personas/neko_supervisor/system.md",
            autonomy=AutonomyLevel.PROPOSE_ONLY.value,
            skills=["harness-mission-lead", "harness-runtime-model"],
        ),
        AgentPersona(
            id="dev",
            display_name="Launcher Dev Agent",
            role=AgentRole.DEV.value,
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=["file", "search", "terminal", "session_search", "code_execution", "skills"],
            system_prompt_path="personas/dev/system.md",
            autonomy=AutonomyLevel.AUTONOMOUS.value,
            skills=[
                "agent-runtime-harness",
                "staged-deep-audit-delivery",
                "aaa-feature-delivery",
                "test-driven-development",
                "systematic-debugging",
                "flutter-ui-development",
                "eternia-launcher-workflow",
                "frontend-backend-contract-handoff",
                "launcher-stagec-mcp-screenshot",
                "harness-handoff-recovery",
                "harness-dev-delivery",
                "launcher-analyze-proof",
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
            system_prompt_path="personas/dev/system.md",
            autonomy=AutonomyLevel.AUTONOMOUS.value,
            hermes_profile="backend-dev",
            skills=[
                "agent-runtime-harness",
                "staged-deep-audit-delivery",
                "aaa-feature-delivery",
                "test-driven-development",
                "systematic-debugging",
                "eternia-local-gates",
                "eternia-backend-tests",
                "frontend-backend-contract-handoff",
                "harness-handoff-recovery",
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
            system_prompt_path="personas/qa/system.md",
            autonomy=AutonomyLevel.AUTONOMOUS.value,
            skills=["harness-qa-verdict"],
        ),
    ]


def load_bundled_prompt(role: AgentRole | str) -> str:
    resolved_role = role if isinstance(role, AgentRole) else AgentRole(role)
    path = Path(__file__).with_name("prompts") / f"{resolved_role.value}.md"
    return path.read_text(encoding="utf-8")
