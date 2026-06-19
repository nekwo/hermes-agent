from agent_runtime.config import load_agent_runtime_config, configured_personas


def test_config_merges_persona_overrides(tmp_path):
    p=tmp_path/"config.yaml"; p.write_text("agent_runtime:\n  default_model: gpt-x\n  personas:\n    pm:\n      toolsets: [file, terminal, todo]\n", encoding="utf-8")
    cfg=load_agent_runtime_config(p)
    pm=next(p for p in configured_personas(cfg) if p.id=="pm")
    assert pm.model == "gpt-x"
    assert pm.toolsets == ["file", "todo"]


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
    qa = next(persona for persona in configured_personas(cfg) if persona.id == "qa")

    assert qa.display_name == "Visual QA Agent"
    assert qa.autonomy == "autonomous"
    assert qa.hermes_profile == "launcher-qa"
    assert qa.skills == ["agent-runtime-harness", "launcher-stagec-mcp-screenshot", "harness-qa-verdict"]
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
    dev = next(persona for persona in configured_personas(cfg) if persona.id == "dev")

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
    backend_dev = next(persona for persona in configured_personas(cfg) if persona.id == "backend_dev")

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
    backend_dev = next(persona for persona in configured_personas(cfg) if persona.id == "backend_dev")

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


def test_normal_worker_flow_defaults_off_and_can_be_enabled(tmp_path):
    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")

    assert default_config.normal_worker_flow.enabled is False
    assert default_config.normal_worker_flow.auto_final_gate_after_delivery is True

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


def test_mission_plan_config_defaults_off_and_can_be_enabled(tmp_path):
    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")

    assert default_config.mission_plan.enabled is False
    assert default_config.mission_plan.enforce_routing is True
    assert default_config.mission_plan.enforce_hud is True

    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  mission_plan:\n"
        "    enabled: true\n"
        "    enforce_routing: false\n"
        "    version: 1\n",
        encoding="utf-8",
    )

    cfg = load_agent_runtime_config(p)

    assert cfg.mission_plan.enabled is True
    assert cfg.mission_plan.enforce_routing is False
    assert cfg.mission_plan.enforce_hud is True
    assert cfg.mission_plan.version == 1


def test_role_envelope_config_defaults_off_and_can_be_enabled(tmp_path):
    default_config = load_agent_runtime_config(tmp_path / "missing.yaml")

    assert default_config.role_envelope.enabled is False
    assert default_config.role_envelope.checklist_hud_enabled is True

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

    assert cfg.role_envelope.enabled is True
    assert cfg.role_envelope.max_same_session_continuations == 6
    assert cfg.role_envelope.max_no_progress_repeats == 2


def test_config_preserves_stage46_required_skills_when_overrides_replace_skills(tmp_path):
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

    personas = {persona.id: persona for persona in configured_personas(load_agent_runtime_config(p))}

    assert "harness-mission-lead" in personas["neko_supervisor"].skills
    assert "harness-dev-delivery" in personas["dev"].skills
    assert "launcher-analyze-proof" in personas["dev"].skills
    assert "harness-dev-delivery" in personas["backend_dev"].skills
    assert "launcher-analyze-proof" not in personas["backend_dev"].skills
    assert "harness-qa-verdict" in personas["qa"].skills


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

    personas = {persona.id: persona for persona in configured_personas(load_agent_runtime_config(p))}

    assert personas["neko_supervisor"].hermes_profile == "captain"
    assert "harness-mission-lead" in personas["neko_supervisor"].skills


def test_neko_supervisor_legacy_alice_profile_falls_back_to_head_agent_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_runtime.config.profile_exists", lambda name: False)
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

    personas = {persona.id: persona for persona in configured_personas(load_agent_runtime_config(p))}

    assert personas["neko_supervisor"].hermes_profile == "alice-mac"


def test_neko_supervisor_preserves_existing_explicit_alice_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_runtime.config.profile_exists", lambda name: name == "alice")
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  head_agent_profile: other-head\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      hermes_profile: alice\n",
        encoding="utf-8",
    )

    personas = {persona.id: persona for persona in configured_personas(load_agent_runtime_config(p))}

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

    personas = {persona.id: persona for persona in configured_personas(load_agent_runtime_config(p))}

    assert personas["neko_supervisor"].skills == ["agent-runtime-harness", "harness-mission-lead"]
