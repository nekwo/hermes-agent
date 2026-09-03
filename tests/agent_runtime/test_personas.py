from agent_runtime.models import AgentPersona
from agent_runtime.personas import (
    AgentRole,
    AutonomyLevel,
    PERSONA_BLOCKED_TOOLS,
    declared_lane_toolsets,
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
    """There is NO role ceiling — and since S0a there is no per-persona list either.

    ``validate_toolsets`` is the normalizer this test was always really about: a
    role token does not filter the list handed to it. What moved on 2026-09-03 is
    WHOSE list the lane reads — ``effective_toolsets`` answers the bound profile's
    declaration (``declared_lane_toolsets``), not ``persona.toolsets`` — so the
    field-shaped half of this assertion is made against the normalizer and the
    lane half is asserted for what it now is.
    """

    pm = persona(AgentRole.PM, ["file", "terminal", "code_execution", "todo"])

    assert validate_toolsets(pm.toolsets) == ["file", "terminal", "code_execution", "todo"]
    # The persona field admits nothing; the profile declaration does.
    assert effective_toolsets(pm) == list(declared_lane_toolsets(pm).toolsets)
    assert "harness_core" not in effective_toolsets(pm)  # expanded to members
    assert "terminal" in effective_toolsets(pm)


def test_blocked_tools_are_exposed_for_runtime_filtering():
    assert "delegate_task" in PERSONA_BLOCKED_TOOLS
    assert "memory" in PERSONA_BLOCKED_TOOLS
    assert "send_message" in PERSONA_BLOCKED_TOOLS


def test_validate_toolsets_preserves_unknown_and_deduplicates_values():
    assert validate_toolsets(["browser", "terminal", "code_execution", "made_up"]) == [
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

    # The legacy role still resolves as data (this is the subject); its declared
    # capability comes from the profile lane default since S0a, so the persona
    # field is asserted through the normalizer that still reads it.
    assert validate_toolsets(pm.toolsets) == ["file", "terminal", "todo"]
    assert declared_lane_toolsets(pm).source == "profile_unresolved"
