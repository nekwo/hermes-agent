from types import SimpleNamespace

from agent_runtime.prompt_observability import (
    MAX_WORKSPACE_AGENTS_BYTES,
    load_workspace_agents_context,
    mission_chat_prompt_observability,
)


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


def test_prompt_observability_names_live_task_bound_chat_without_session_row():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        persona_instance_id="personainst_dev",
        session_id="persona_chat_personainst_dev_live",
        task_id="task_live",
        session_db=None,
    )

    assert context["chat_id"] == "persona_chat_personainst_dev_live"
    assert context["chat_title"] == "Mission run"
    assert context["chat"]["source"] == "task_bound"


def test_workspace_agents_context_is_loaded_and_reported_from_selected_file(tmp_path):
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_text("# Workspace rules\nKeep this workspace isolated.\n", encoding="utf-8")

    workspace_agents = load_workspace_agents_context(str(agents_file))
    assert workspace_agents is not None
    assert workspace_agents.content.startswith("# Workspace rules")
    assert workspace_agents.receipt["included"] is True
    assert workspace_agents.receipt["status"] == "loaded"

    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        workspace_id="ws_launcher",
        workspace_name="Launcher",
        workspace_agents=workspace_agents,
    )

    receipt = next(item for item in context["context_files"] if item["name"] == "AGENTS.md")
    assert context["workspace_id"] == "ws_launcher"
    assert context["workspace_name"] == "Launcher"
    assert receipt["path"] == str(agents_file.resolve())
    assert receipt["sha256"]


def test_workspace_agents_context_refuses_oversized_file_without_blocking_receipt(tmp_path):
    agents_file = tmp_path / "AGENTS.md"
    agents_file.write_bytes(b"x" * (MAX_WORKSPACE_AGENTS_BYTES + 1))

    workspace_agents = load_workspace_agents_context(str(agents_file))

    assert workspace_agents is not None
    assert workspace_agents.content is None
    assert workspace_agents.receipt["included"] is False
    assert workspace_agents.receipt["status"] == "too_large"


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
