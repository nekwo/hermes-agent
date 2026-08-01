from agent_runtime.config import (
    describe_runtime_default_authority,
    load_agent_runtime_config,
    persona_records_from_config,
)


def test_config_merges_persona_overrides(tmp_path):
    p=tmp_path/"config.yaml"; p.write_text("agent_runtime:\n  default_model: gpt-x\n  personas:\n    pm:\n      toolsets: [file, terminal, todo]\n", encoding="utf-8")
    cfg=load_agent_runtime_config(p)
    pm=next(p for p in persona_records_from_config(cfg) if p.id=="pm")
    assert pm.model == "gpt-x"
    assert pm.toolsets == ["file", "terminal", "todo"]


def test_config_merges_profile_skills_and_readiness_fields(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    qa:\n"
        "      display_name: Visual QA Agent\n"
        "      autonomy: autonomous\n"
        "      hermes_profile: launcher-qa\n"
        "      skills: [agent-runtime-harness, launcher-stagec-mcp-screenshot]\n"
        "      soul_overlay_path: prompts/qa_harness.md\n"
        "      required_mcp_servers: [launcher_qa]\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)
    qa = next(persona for persona in persona_records_from_config(cfg) if persona.id == "qa")

    assert qa.display_name == "Visual QA Agent"
    assert qa.autonomy == "autonomous"
    assert qa.hermes_profile == "launcher-qa"
    assert "agent-runtime-harness" in qa.skills
    assert "launcher-stagec-mcp-screenshot" in qa.skills
    assert qa.soul_overlay_path == "prompts/qa_harness.md"
    assert qa.required_mcp_servers == ["launcher_qa"]


def test_config_allows_dev_launcher_to_opt_into_profile_memory(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    dev:\n"
        "      display_name: Launcher Dev Agent\n"
        "      autonomy: autonomous\n"
        "      include_profile_memory: true\n"
        "      max_total_tokens: 250000\n"
        "      max_api_calls: 6\n"
        "      max_wall_seconds: 180\n"
        "      iteration_budget: 6\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)
    dev = next(persona for persona in persona_records_from_config(cfg) if persona.id == "dev")

    assert dev.display_name == "Launcher Dev Agent"
    assert dev.autonomy == "autonomous"
    assert dev.include_profile_memory is True
    assert dev.max_total_tokens == 250000
    assert dev.max_api_calls == 6
    assert dev.max_wall_seconds == 180
    assert dev.iteration_budget == 6


def test_config_allows_explicit_core_context_file_opt_in(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    backend_dev:\n"
        "      include_core_context_files: true\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)
    backend_dev = next(persona for persona in persona_records_from_config(cfg) if persona.id == "backend_dev")

    assert backend_dev.include_core_context_files is True


def test_config_merges_specialist_dev_repo_scope_fields(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    backend_dev:\n"
        "      display_name: Backend Dev Agent\n"
        "      role: dev\n"
        "      hermes_profile: backend-dev\n"
        "      repo_scope: X:/Unreal Engine/Engine/EterniaBackend/eternia-backend\n"
        "      repo_scope_label: EterniaBackend\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)
    backend_dev = next(persona for persona in persona_records_from_config(cfg) if persona.id == "backend_dev")

    assert backend_dev.role == "dev"
    assert backend_dev.display_name == "Backend Dev Agent"
    assert backend_dev.hermes_profile == "backend-dev"
    assert backend_dev.repo_scope == "X:/Unreal Engine/Engine/EterniaBackend/eternia-backend"
    assert backend_dev.repo_scope_label == "EterniaBackend"


def test_config_loads_live_run_budget_fields(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  live_run_max_wall_seconds: 12.5\n"
        "  live_run_max_api_calls: 7\n"
        "  live_run_max_total_tokens: 12345\n"
        "  live_run_iteration_budget: 9\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    assert cfg.live_run_max_wall_seconds == 12.5
    assert cfg.live_run_max_api_calls == 7
    assert cfg.live_run_max_total_tokens == 12345
    assert cfg.live_run_iteration_budget == 9


def test_persona_chat_hot_runtime_defaults_dark_and_is_bounded(tmp_path):
    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")
    assert default_config.persona_chat.hot_sessions_enabled is False
    assert default_config.persona_chat.max_hot_sessions == 8
    assert default_config.persona_chat.idle_ttl_seconds == 1800

    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  persona_chat:\n"
        "    hot_sessions_enabled: true\n"
        "    max_hot_sessions: 3\n"
        "    idle_ttl_seconds: 45\n",
        encoding="utf-8",
    )
    configured = load_agent_runtime_config(p)
    assert configured.persona_chat.hot_sessions_enabled is True
    assert configured.persona_chat.max_hot_sessions == 3
    assert configured.persona_chat.idle_ttl_seconds == 45


def test_config_loads_read_model_flag_defaults_and_filename_guard(tmp_path):
    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")
    assert default_config.read_model.enabled is False
    assert default_config.read_model.serve_snapshot_from_db is True
    assert default_config.read_model.db_filename == "read_model.db"

    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  read_model:\n"
        "    enabled: true\n"
        "    serve_snapshot_from_db: false\n"
        "    db_filename: custom-runtime.db\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)
    assert cfg.read_model.enabled is True
    assert cfg.read_model.serve_snapshot_from_db is False
    assert cfg.read_model.db_filename == "custom-runtime.db"

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "agent_runtime:\n"
        "  read_model:\n"
        "    enabled: true\n"
        "    db_filename: ../outside.db\n",
        encoding="utf-8",
    )

    assert load_agent_runtime_config(bad).read_model.db_filename == "read_model.db"


def test_normal_worker_flow_defaults_off_and_can_be_enabled(tmp_path):
    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")

    assert default_config.normal_worker_flow.enabled is False
    assert not hasattr(default_config.normal_worker_flow, "auto_final_gate_after_delivery")

    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  normal_worker_flow:\n"
        "    enabled: true\n"
        "    max_self_test_repeats_without_change: 2\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    assert cfg.normal_worker_flow.enabled is True
    assert cfg.normal_worker_flow.max_self_test_repeats_without_change == 2


def test_legacy_mission_plan_config_is_ignored_after_stage_graph_removal(tmp_path):
    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")

    assert not hasattr(default_config, "mission_plan")

    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  mission_plan:\n"
        "    enabled: true\n"
        "    version: 1\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    assert not hasattr(cfg, "mission_plan")
    assert cfg.normal_worker_flow.enabled is False


def test_role_envelope_config_block_is_ignored_after_s47(tmp_path):
    """This test used to prove the ``role_envelope`` block loaded and could be
    turned on. S44 deleted every reader of those knobs and S47 deleted the block
    itself, so the contract inverted: an operator yaml that still sets it must
    load cleanly and produce NO such attribute. Retargeted, not weakened — the
    same yaml is still exercised end to end."""

    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")
    assert not hasattr(default_config, "role_envelope")

    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  role_envelope:\n"
        "    enabled: true\n"
        "    max_same_session_continuations: 6\n"
        "    max_no_progress_repeats: 2\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    assert not hasattr(cfg, "role_envelope")
    # A stale block must not poison the sibling blocks that DO still load.
    assert cfg.normal_worker_flow.enabled is False


def test_config_persona_skills_merge_defaults_with_explicit_removals(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      skills: [agent-runtime-harness]\n"
        "    dev:\n"
        "      skills: [launcher-stagec-mcp-screenshot]\n"
        "    backend_dev:\n"
        "      role: dev\n"
        "      skills: [backend-docker]\n"
        "    qa:\n"
        "      skills: [visual-qa]\n",
        encoding="utf-8",
    )

    personas = {persona.id: persona for persona in persona_records_from_config(load_agent_runtime_config(p))}

    assert "agent-runtime-harness" in personas["neko_supervisor"].skills
    assert "launcher-stagec-mcp-screenshot" in personas["dev"].skills
    assert "backend-docker" in personas["backend_dev"].skills
    assert "visual-qa" in personas["qa"].skills

    assert personas["neko_supervisor"].skills == ["agent-runtime-harness"]
    assert personas["dev"].skills == ["launcher-stagec-mcp-screenshot"]


def test_config_persona_skills_remove_is_explicit(tmp_path):
    removed = "visual-qa"
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    qa:\n"
        "      skills: [visual-qa, retained-skill]\n"
        f"      skills_remove: [{removed}]\n",
        encoding="utf-8",
    )

    personas = {persona.id: persona for persona in persona_records_from_config(load_agent_runtime_config(p))}
    assert personas["qa"].skills == ["retained-skill"]


def test_neko_supervisor_uses_configured_head_agent_profile_when_not_explicit(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  head_agent_profile: captain\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      skills: [agent-runtime-harness]\n",
        encoding="utf-8",
    )

    personas = {persona.id: persona for persona in persona_records_from_config(load_agent_runtime_config(p))}

    assert personas["neko_supervisor"].hermes_profile is None
    assert "agent-runtime-harness" in personas["neko_supervisor"].skills


def test_neko_supervisor_profile_data_does_not_fall_back_to_head_agent(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  head_agent_profile: alice-mac\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      hermes_profile: alice\n"
        "      skills: [agent-runtime-harness]\n",
        encoding="utf-8",
    )

    personas = {persona.id: persona for persona in persona_records_from_config(load_agent_runtime_config(p))}

    assert personas["neko_supervisor"].hermes_profile == "alice"


def test_neko_supervisor_preserves_explicit_profile_data(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  head_agent_profile: other-head\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      hermes_profile: alice\n",
        encoding="utf-8",
    )

    personas = {persona.id: persona for persona in persona_records_from_config(load_agent_runtime_config(p))}

    assert personas["neko_supervisor"].hermes_profile == "alice"


def test_config_accepts_json_encoded_skill_list_from_cli_config_set(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      skills: '[\"agent-runtime-harness\", \"harness-mission-lead\"]'\n",
        encoding="utf-8",
    )

    personas = {persona.id: persona for persona in persona_records_from_config(load_agent_runtime_config(p))}

    assert "agent-runtime-harness" in personas["neko_supervisor"].skills
    assert "harness-mission-lead" in personas["neko_supervisor"].skills


# ---------------------------------------------------------------------------
# Single runtime-default authority (top-level model.default vs agent_runtime.*)
# ---------------------------------------------------------------------------


def test_runtime_default_follows_top_level_model_when_no_override(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "  provider: openai-codex\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    assert cfg.default_model == "gpt-5.6-luna"
    assert cfg.default_model_source == "model.default"
    assert cfg.default_provider == "openai-codex"
    assert cfg.default_provider_source == "model.provider"


def test_runtime_default_override_wins_with_provenance(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "  provider: openai-codex\n"
        "agent_runtime:\n"
        "  default_model: gpt-5.5\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    # An explicit agent_runtime override still wins (backward compatible), but is
    # now provenance-stamped so surfaces can flag it as a shadow.
    assert cfg.default_model == "gpt-5.5"
    assert cfg.default_model_source == "agent_runtime.default_model"
    # Provider falls through to the top-level authority independently of model.
    assert cfg.default_provider == "openai-codex"
    assert cfg.default_provider_source == "model.provider"


def test_runtime_default_unset_when_neither_present(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("agent_runtime:\n  daemon_enabled: false\n", encoding="utf-8")

    cfg = load_agent_runtime_config(p)

    assert cfg.default_model is None
    assert cfg.default_model_source == "unset"
    assert cfg.default_provider is None
    assert cfg.default_provider_source == "unset"


def test_runtime_default_accepts_plain_string_model_block(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("model: gpt-x\n", encoding="utf-8")

    cfg = load_agent_runtime_config(p)

    assert cfg.default_model == "gpt-x"
    assert cfg.default_model_source == "model.default"


def test_runtime_default_empty_override_treated_as_unset(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "agent_runtime:\n"
        "  default_model: '   '\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    assert cfg.default_model == "gpt-5.6-luna"
    assert cfg.default_model_source == "model.default"


def test_unpinned_persona_follows_resolved_runtime_default(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "  provider: openai-codex\n"
        "agent_runtime:\n"
        "  personas:\n"
        "    dev:\n"
        "      display_name: Dev\n",
        encoding="utf-8",
    )

    dev = next(p for p in persona_records_from_config(load_agent_runtime_config(p)) if p.id == "dev")

    assert dev.model == "gpt-5.6-luna"
    assert dev.provider == "openai-codex"


def test_explicit_persona_pin_survives_resolved_default(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "agent_runtime:\n"
        "  personas:\n"
        "    pm:\n"
        "      model: gpt-5.3-codex-spark\n",
        encoding="utf-8",
    )

    pm = next(p for p in persona_records_from_config(load_agent_runtime_config(p)) if p.id == "pm")

    # A deliberate pin (e.g. pm's spark) is never overwritten by the default.
    assert pm.model == "gpt-5.3-codex-spark"


def test_authority_report_flags_shadowing_override(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "  provider: openai-codex\n"
        "agent_runtime:\n"
        "  default_model: gpt-5.5\n"
        "  default_provider: openai-codex\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      model: gpt-5.5\n"
        "      provider: openai-codex\n"
        "    pm:\n"
        "      model: gpt-5.3-codex-spark\n",
        encoding="utf-8",
    )

    report = describe_runtime_default_authority(p)

    assert report["harness_override"]["model_state"] == "shadowing"
    assert report["harness_override"]["provider_state"] == "redundant"
    assert report["top_level"]["model"] == "gpt-5.6-luna"
    pins = {pin["persona_id"]: pin for pin in report["persona_pins"]}
    # neko pin duplicates the (overridden) resolved default → redundant/stale.
    assert pins["neko_supervisor"]["matches_runtime_default"] is True
    # pm's deliberate spark pin diverges → operator judgment, not stale.
    assert pins["pm"]["matches_runtime_default"] is False


def test_authority_report_clean_when_only_top_level_default(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "  provider: openai-codex\n",
        encoding="utf-8",
    )

    report = describe_runtime_default_authority(p)

    assert report["harness_override"]["model_state"] == "absent"
    assert report["resolved"]["model"] == "gpt-5.6-luna"
    assert report["resolved"]["model_source"] == "model.default"
    assert report["persona_pins"] == []


def test_authority_report_flags_provider_only_pin(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "agent_runtime:\n"
        "  personas:\n"
        "    dev:\n"
        "      provider: openai-codex\n",
        encoding="utf-8",
    )

    report = describe_runtime_default_authority(p)
    pins = {pin["persona_id"]: pin for pin in report["persona_pins"]}

    assert pins["dev"]["provider_pinned_without_model"] is True
    assert pins["dev"]["matches_runtime_default"] is None
