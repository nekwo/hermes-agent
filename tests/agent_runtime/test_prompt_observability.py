from types import SimpleNamespace

from agent_runtime.prompt_observability import mission_chat_prompt_observability


def test_prompt_observability_preserves_profile_persona_identity():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="profile:alice",
            hermes_profile="alice",
            display_name="Alice Agent",
            role="profile",
        ),
        persona_instance_id="personainst_profile_alice",
        session_id="persona_chat_alice",
    )

    assert context["persona_id"] == "profile:alice"
    assert context["profile"] == "alice"
