from __future__ import annotations

from agent_runtime.models import AgentPersona
from agent_runtime.personas import AutonomyLevel, PROFILE_ROLE_SENTINEL


SAMPLE_PROFILE_TOOLSETS = [
    "file",
    "search",
    "terminal",
    "code_execution",
    "session_search",
    "skills",
    "agent_chat",
    "board",
]


def sample_persona(
    persona_id: str = "dev",
    *,
    role: str | None = None,
    toolsets: list[str] | None = None,
    hermes_profile: str | None = None,
    skills: list[str] | None = None,
) -> AgentPersona:
    roles = {
        "neko_supervisor": "alice_supervisor",
        "dev": "dev",
        "backend_dev": "dev",
        "qa": "qa",
        "base": PROFILE_ROLE_SENTINEL,
    }
    return AgentPersona(
        id=persona_id,
        display_name=persona_id.replace("_", " ").title(),
        role=role or roles.get(persona_id, "custom-reviewer"),
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=list(toolsets if toolsets is not None else SAMPLE_PROFILE_TOOLSETS),
        system_prompt_path="",
        autonomy=AutonomyLevel.PROPOSE_ONLY.value,
        hermes_profile=hermes_profile,
        skills=list(skills or []),
    )


def sample_personas() -> list[AgentPersona]:
    neko = sample_persona(
        "neko_supervisor",
        toolsets=["file", "search", "terminal", "session_search", "code_execution", "todo", "skills"],
        skills=["harness-continuity", "harness-runtime-model"],
    )
    neko.display_name = "Neko Mission Lead"
    dev = sample_persona(
        "dev",
        toolsets=["file", "search", "terminal", "session_search", "code_execution", "skills"],
        hermes_profile="gpt-launcher",
        skills=["harness-continuity", "harness-dev-delivery", "harness-qa-verdict", "launcher-stagec-mcp-screenshot"],
    )
    dev.display_name = "Launcher Dev Agent"
    dev.repo_scope_label = "EterniaLauncher"
    backend = sample_persona(
        "backend_dev",
        toolsets=["file", "search", "terminal", "session_search", "code_execution", "skills"],
        hermes_profile="backend-dev",
        skills=["harness-continuity", "harness-dev-delivery"],
    )
    backend.display_name = "Backend Dev Agent"
    backend.repo_scope = "X:/Unreal Engine/Engine/EterniaBackend/eternia-backend"
    backend.repo_scope_label = "EterniaBackend"
    qa = sample_persona(
        "qa",
        toolsets=["file", "search", "terminal", "browser", "vision", "session_search", "skills"],
        hermes_profile="qa",
        skills=["harness-qa-verdict", "launcher-stagec-mcp-screenshot"],
    )
    qa.display_name = "QA Agent"
    return [neko, dev, backend, qa, sample_persona("base", hermes_profile="base")]
