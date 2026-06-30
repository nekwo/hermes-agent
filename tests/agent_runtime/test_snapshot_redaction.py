from agent_runtime.models import AgentPersona
from agent_runtime.snapshot import _agent_summary


def test_agent_summary_contains_profile_readiness_without_secret_values():
    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "terminal"],
        system_prompt_path="personas/qa/system.md",
        hermes_profile="definitely-missing-stage9-profile",
        skills=["definitely-missing-stage9-skill"],
        required_mcp_servers=["launcher_qa"],
    )

    summary = _agent_summary(persona)

    assert summary["hermes_profile"] == "definitely-missing-stage9-profile"
    assert summary["profile_readiness"] == "missing_profile"
    assert summary["skills"] == ["definitely-missing-stage9-skill"]
    assert summary["required_mcp_servers"] == ["launcher_qa"]
    assert "token" not in str(summary).lower()
    assert "api_key" not in str(summary).lower()
