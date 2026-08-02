from agent_runtime.models import AgentPersona
from agent_runtime.personas import (
    AgentRole,
    AutonomyLevel,
    PERSONA_BLOCKED_TOOLS,
    effective_toolsets,
    validate_toolsets,
)
from tests.agent_runtime.persona_samples import sample_personas


def persona(role, toolsets):
    return AgentPersona(
        id=role.value,
        display_name=role.value,
        role=role.value,
        model=None,
        provider=None,
        api_mode=None,
        toolsets=toolsets,
        system_prompt_path=f"personas/{role.value}/system.md",
    )


def test_role_tokens_do_not_filter_configured_toolsets():
    pm = persona(AgentRole.PM, ["file", "terminal", "code_execution", "todo"])

    assert effective_toolsets(pm) == ["file", "terminal", "code_execution", "todo"]


def test_blocked_tools_are_exposed_for_runtime_filtering():
    assert "delegate_task" in PERSONA_BLOCKED_TOOLS
    assert "memory" in PERSONA_BLOCKED_TOOLS
    assert "send_message" in PERSONA_BLOCKED_TOOLS


def test_validate_toolsets_preserves_unknown_and_deduplicates_values():
    assert validate_toolsets("qa", ["browser", "terminal", "code_execution", "made_up"]) == [
        "browser", "terminal", "code_execution", "made_up",
    ]


def test_explicit_persona_samples_are_valid():
    personas = sample_personas()

    assert {p.id for p in personas} == {"dev", "backend_dev", "qa", "neko_supervisor", "base"}
    assert personas[0].id == "neko_supervisor"
    dev = next(p for p in personas if p.id == "dev")
    assert dev.autonomy == AutonomyLevel.PROPOSE_ONLY.value
    assert dev.include_profile_memory is False
    assert all(p.api_mode is not None for p in personas)
    assert {p.autonomy for p in personas} <= {level.value for level in AutonomyLevel}
    assert effective_toolsets(dev)
    backend_dev = next(p for p in personas if p.id == "backend_dev")
    assert backend_dev.role == "dev"
    assert backend_dev.hermes_profile == "backend-dev"
    assert backend_dev.autonomy == AutonomyLevel.PROPOSE_ONLY.value
    assert effective_toolsets(backend_dev) == effective_toolsets(dev)


def test_pm_role_remains_available_for_explicit_legacy_configuration():
    pm = persona(AgentRole.PM, ["file", "terminal", "todo"])

    assert effective_toolsets(pm) == ["file", "terminal", "todo"]
