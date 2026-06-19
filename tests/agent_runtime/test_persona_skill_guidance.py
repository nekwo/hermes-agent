from agent_runtime.persona_runtime import _recommended_skill_guidance


def test_recommended_skill_guidance_does_not_encourage_loading_entire_manifest():
    guidance = _recommended_skill_guidance(["agent-runtime-harness", "test-driven-development"])

    assert "Recommended skills:" in guidance
    assert 'skill_view(name="agent-runtime-harness")' not in guidance
    assert "call skill_view for a recommended skill only when" not in guidance
    assert "stop after loading the single most relevant skill" in guidance.lower()
    assert "two loaded skills is the normal maximum" in guidance.lower()
    assert "more than two is allowed only when each additional skill has an explicit" in guidance.lower()
    assert "explicit, current-stage purpose" in guidance.lower()
