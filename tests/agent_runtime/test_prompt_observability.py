from types import SimpleNamespace

from agent_runtime.prompt_observability import (
    MAX_WORKSPACE_AGENTS_BYTES,
    _backfill_derived_fields,
    _context_file_summary,
    _layer_text_size,
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
    # Per-item token attribution: the workspace receipt carries bytes // 4.
    assert receipt["token_estimate"] == receipt["bytes"] // 4


def test_context_file_summary_carries_bytes_over_four_token_estimate(tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_bytes(b"x" * 2867)  # today's live SOUL.md size

    summary = _context_file_summary(soul, included=True)

    assert summary["bytes"] == 2867
    # bytes // 4 — an integer-floor estimate, presented as ~N by the launcher.
    assert summary["token_estimate"] == 2867 // 4 == 716
    assert summary["token_estimate"] == summary["bytes"] // 4


def test_context_file_summary_absent_file_has_no_token_estimate(tmp_path):
    missing = tmp_path / "USER.md"  # never created

    summary = _context_file_summary(missing, included=False)

    # Absent file → no bytes, no fabricated token estimate (never a crash).
    assert summary["included"] is False
    assert "bytes" not in summary
    assert "token_estimate" not in summary


def test_layer_text_size_helper_relationship():
    # A present layer text → chars + chars // 4; an empty (blank) layer is a real
    # 0, distinct from an absent layer (no text seam) which degrades to {}.
    assert _layer_text_size("abcdefgh") == {"chars": 8, "token_estimate": 2}
    assert _layer_text_size("") == {"chars": 0, "token_estimate": 0}
    assert _layer_text_size(None) == {}


def test_prompt_layers_surface_carries_chars_and_token_estimate():
    surface_text = "Keep answers terse and cite the runbook."
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        surface_prompt=surface_text,
    )
    layers = {layer["kind"]: layer for layer in context["prompt_layers"]}
    surface = layers["surface"]
    assert surface["chars"] == len(surface_text)
    assert surface["token_estimate"] == len(surface_text) // 4


def test_prompt_layers_blank_surface_is_zero_not_absent():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
        surface_prompt="",
    )
    surface = {layer["kind"]: layer for layer in context["prompt_layers"]}["surface"]
    # Blank surface is present-but-empty: an explicit 0, never a missing key.
    assert surface["chars"] == 0
    assert surface["token_estimate"] == 0


def test_prompt_layers_without_available_text_degrade_to_no_estimate():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="dev",
            hermes_profile="dev",
            display_name="Launcher Dev",
            role="dev",
        ),
    )
    layers = {layer["kind"]: layer for layer in context["prompt_layers"]}
    # persona_identity / system_core / profile_context text is assembled later in
    # the turn (or attributed via context files) — no fabricated per-layer number.
    for kind in ("persona_identity", "system_core", "profile_context"):
        assert kind in layers
        assert "token_estimate" not in layers[kind]
        assert "chars" not in layers[kind]


def test_persona_identity_summary_reflects_own_soul_overlay():
    context = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="alice_supervisor",
        ),
    )
    identity = {layer["kind"]: layer for layer in context["prompt_layers"]}[
        "persona_identity"
    ]
    summary = identity["summary"]
    # Post-deafb825e: a persona's OWN configured soul overlay IS loaded; only the
    # OPERATOR profile's SOUL is not. The stale "does not load the profile SOUL"
    # absolute must be gone.
    assert "does not load the profile SOUL" not in summary
    assert "soul_overlay_path" in summary
    assert "OPERATOR profile's SOUL is" in summary


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
        daemon=None,
        realm="default",
        workspace="default",
    )
    situational = snapshot["chat_contexts"][0]["situational_hud"]
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


def test_chat_time_row_carries_situational_hud_verbatim():
    # Record-at-injection: the chat turn passes the very dict it rendered into
    # the fed block, so the persisted row IS what the model received — the
    # CONTEXT peek must never depend on the snapshot backfill's key match
    # (persona_instance_id, session_id, persona_id), which misses whenever the
    # console chat session differs from the instance's session field.
    injected = {
        "preview": True,
        "lane": {"persona_instance_id": "personainst_neko"},
        "steering": {"steered_by": [], "steers": [{"persona_instance_id": "personainst_dev", "display_name": "Dev"}]},
    }
    row = mission_chat_prompt_observability(
        persona=SimpleNamespace(
            id="neko_supervisor",
            hermes_profile="neko",
            display_name="Neko Mission Lead",
            role="supervisor",
        ),
        persona_instance_id="personainst_neko",
        session_id="console_chat_session",
        situational_hud=injected,
    )
    assert row["situational_hud"] == injected


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
