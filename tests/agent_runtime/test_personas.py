from agent_runtime.models import AgentPersona
from agent_runtime.personas import (
    AgentRole,
    AutonomyLevel,
    PERSONA_BLOCKED_TOOLS,
    effective_toolsets,
    load_bundled_prompt,
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
    assert validate_toolsets(AgentRole.QA, ["browser", "terminal", "code_execution", "made_up"]) == [
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
    assert backend_dev.role == AgentRole.DEV.value
    assert backend_dev.hermes_profile == "backend-dev"
    assert backend_dev.autonomy == AutonomyLevel.PROPOSE_ONLY.value
    assert effective_toolsets(backend_dev) == effective_toolsets(dev)


def test_bundled_prompts_exist_for_each_role():
    for role in AgentRole:
        prompt = load_bundled_prompt(role)
        assert role.value in prompt
        assert "AgentDecision" in prompt


def test_qa_prompt_documents_visual_proof_packet_contract():
    prompt = load_bundled_prompt(AgentRole.QA)

    assert "`request_screenshot` and `request_video`" in prompt
    assert '`mcp_server: "launcher_qa"`' in prompt
    assert "`required_launch_pins`" in prompt
    assert '`target: "mission_control"`' in prompt
    assert "never put absolute runtime paths" in prompt


def test_pm_role_remains_available_for_explicit_legacy_configuration():
    pm = persona(AgentRole.PM, ["file", "terminal", "todo"])

    assert effective_toolsets(pm) == ["file", "terminal", "todo"]


def test_dev_prompt_requires_no_guesswork_implementation_handoffs():
    prompt = load_bundled_prompt(AgentRole.DEV)

    assert "no-guesswork implementation handoff" in prompt
    assert "exact files/modules" in prompt
    assert "schema/version handling" in prompt
    assert "verification commands" in prompt
    assert "patch/test/proof loop" in prompt
    assert "Do not wait for Harness permission to run obvious focused tests" in prompt
    assert "For no-edit verification stages" in prompt
    assert "Return `request_test_run` immediately with exactly those commands" in prompt
    assert "at most six model/tool turns" in prompt
    assert "one targeted search/read pass, one patch, and one focused test command" in prompt


def test_neko_prompt_slices_broad_specialist_work_before_dev():
    prompt = load_bundled_prompt("alice_supervisor")

    assert "broad specialist mission" in prompt
    assert "slice it for the rightful graph-selected specialist" in prompt
    assert "keep the visible path faithful to the active blueprint" in prompt
