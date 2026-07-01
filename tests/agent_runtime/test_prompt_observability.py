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


def test_accessible_skills_hash_check_uses_persona_profile_home(monkeypatch, tmp_path):
    # Regression: skill hash/missing checks in the HUD must run against the persona's
    # OWN profile home (mirroring profile_readiness), not the active HERMES_HOME.
    # Without the home an isolated persona (e.g. base) shows a false hash_mismatch in
    # Mission Control while `harness status` reports the skill clean.
    from agent_runtime import prompt_observability as po
    import agent_runtime.skill_install as skill_install
    import agent_runtime.profile_context as profile_context

    home = tmp_path / "base_home"
    captured = {}

    monkeypatch.setattr(
        profile_context,
        "resolve_persona_profile",
        lambda _persona: SimpleNamespace(profile_home=home, hermes_profile="base", readiness="ready"),
    )

    def fake_mismatches(_names, *, hermes_home=None):
        captured["hermes_home"] = hermes_home
        return []

    monkeypatch.setattr(skill_install, "harness_skill_hash_mismatches", fake_mismatches)

    po._accessible_skills_context(
        SimpleNamespace(id="base", hermes_profile="base", skills=["harness-runtime-model"]),
        "base",
    )

    assert captured["hermes_home"] == home
