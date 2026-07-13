from types import SimpleNamespace

from agent_runtime.prompt_observability import (
    MAX_WORKSPACE_AGENTS_BYTES,
    _backfill_derived_fields,
    load_workspace_agents_context,
    mission_chat_prompt_observability,
    snapshot_prompt_observability,
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


def test_prompt_observability_reports_persona_identity_layer_and_memory_flag():
    # The mission-chat lane injects a first-person identity block and (post-fix)
    # does NOT load the profile SOUL as identity; memory tracks
    # include_profile_memory. The observability report must reflect that honestly
    # so the launcher CONTEXT peek does not claim SOUL/memory that isn't loaded.
    no_memory = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="alice_supervisor",
            include_profile_memory=False,
        ),
        session_id="persona_chat_neko",
    )
    layers = {layer["kind"]: layer for layer in no_memory["prompt_layers"]}
    assert "persona_identity" in layers
    assert "Neko Mission Lead" in layers["persona_identity"]["summary"]
    assert layers["profile_context"]["status"] == "skipped"
    assert no_memory["prompt_flags"]["skip_memory"] is True
    assert no_memory["prompt_flags"]["load_soul_identity"] is False

    with_memory = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="alice_supervisor",
            include_profile_memory=True,
        ),
        session_id="persona_chat_neko",
    )
    memory_layer = {layer["kind"]: layer for layer in with_memory["prompt_layers"]}["profile_context"]
    assert memory_layer["status"] == "loaded"
    assert with_memory["prompt_flags"]["skip_memory"] is False


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


def test_mission_chat_prompt_observability_carries_mission_hud():
    hud = {"preview": True, "typed_current_stage": {"id": "dev"}}
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev"),
        persona_instance_id="personainst_dev",
        task_id="task_live",
        mission_hud=hud,
    )
    assert context["mission_hud"] == hud


def test_mission_chat_prompt_observability_defaults_mission_hud_to_empty():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev"),
    )
    # No task bound → no HUD; the key is always present so the launcher parser
    # has a stable shape rather than a sometimes-missing field.
    assert context["mission_hud"] == {}


def test_snapshot_previews_the_mission_hud_for_a_bound_task(monkeypatch):
    from agent_runtime import context_builder
    from agent_runtime import prompt_observability as po

    # Isolate from the real on-disk store and stub the single-authority builder;
    # this test covers the wiring (task lookup + pass-through), not HUD shape.
    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    sentinel = {"preview": True, "phase": "in_progress", "task_id": "task_live"}
    monkeypatch.setattr(
        context_builder,
        "mission_hud_preview",
        lambda task, *, proof_store=None: sentinel,
    )

    persona = SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev")
    instance = SimpleNamespace(
        id="personainst_dev",
        persona_id="dev",
        session_id="persona_chat_dev",
        current_task_id="task_live",
        goal_id="goal_1",
    )
    snapshot = snapshot_prompt_observability(
        personas=[persona],
        persona_instances=[instance],
        tasks=[SimpleNamespace(id="task_live")],
    )

    contexts = snapshot["chat_contexts"]
    assert len(contexts) == 1
    assert contexts[0]["mission_hud"] == sentinel


def test_snapshot_leaves_mission_hud_empty_for_unbound_instance(monkeypatch):
    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    persona = SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev")
    instance = SimpleNamespace(id="personainst_dev", persona_id="dev", session_id="s")
    snapshot = snapshot_prompt_observability(
        personas=[persona],
        persona_instances=[instance],
        tasks=[SimpleNamespace(id="some_other_task")],
    )
    assert snapshot["chat_contexts"][0]["mission_hud"] == {}


def test_snapshot_includes_situational_hud_for_instance(monkeypatch):
    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    persona = SimpleNamespace(
        id="neko_supervisor", hermes_profile="neko", display_name="Neko Mission Lead", role="supervisor"
    )
    instance = SimpleNamespace(
        id="personainst_neko",
        persona_id="neko_supervisor",
        session_id="s",
        current_task_id=None,
        goal_id=None,
        role="supervisor",
        display_name="Neko Mission Lead",
        state="idle",
        mode="configured",
    )
    snapshot = snapshot_prompt_observability(
        personas=[persona],
        persona_instances=[instance],
        tasks=[],
        daemon={"state": "starting", "loops": 0},
        realm="default",
        workspace="default",
    )
    situational = snapshot["chat_contexts"][0]["situational_hud"]
    assert situational["runtime"]["state"] == "starting"
    assert situational["scope"] == {"realm": "default", "workspace": "default"}
    assert situational["lane"]["persona_instance_id"] == "personainst_neko"


def test_snapshot_situational_hud_without_daemon_scope_still_carries_lane(monkeypatch):
    from agent_runtime import prompt_observability as po

    monkeypatch.setattr(po, "load_latest_prompt_observability_contexts", lambda: [])
    persona = SimpleNamespace(id="dev", hermes_profile="dev", display_name="Dev", role="dev")
    instance = SimpleNamespace(
        id="personainst_dev",
        persona_id="dev",
        session_id="s",
        current_task_id=None,
        goal_id=None,
        role="dev",
        display_name="Dev",
        state="idle",
        mode="configured",
    )
    snapshot = snapshot_prompt_observability(
        personas=[persona], persona_instances=[instance], tasks=[]
    )
    situational = snapshot["chat_contexts"][0]["situational_hud"]
    # No daemon/scope threaded → those sub-blocks are absent, but the lane
    # identity (and the always-present key) still hold a stable shape.
    assert "runtime" not in situational
    assert situational["lane"]["persona_instance_id"] == "personainst_dev"


def test_backfill_copies_situational_hud_onto_persisted_row():
    persisted = {"persona_instance_id": "x", "session_id": "s", "persona_id": "p"}
    built = {"situational_hud": {"preview": True, "lane": {"persona_instance_id": "x"}}}
    _backfill_derived_fields(persisted, built)
    assert persisted["situational_hud"] == {"preview": True, "lane": {"persona_instance_id": "x"}}


def test_backfill_copies_mission_hud_onto_persisted_row():
    persisted = {"persona_instance_id": "x", "session_id": "s", "persona_id": "p"}
    built = {"mission_hud": {"preview": True, "phase": "in_progress"}}
    _backfill_derived_fields(persisted, built)
    assert persisted["mission_hud"] == {"preview": True, "phase": "in_progress"}


def test_backfill_does_not_overwrite_an_existing_persisted_hud():
    persisted = {"mission_hud": {"real": True}}
    built = {"mission_hud": {"preview": True}}
    _backfill_derived_fields(persisted, built)
    assert persisted["mission_hud"] == {"real": True}
