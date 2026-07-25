"""The machine-root chokepoint must be LOUD at every seam that consumes it.

These are the anti-silent-failure contracts: readiness, Stage C config
resolution, preflight, persona repo scope, and the runtime MCP loader each have
to report the typed reason rather than degrade into "not configured" or hand a
literal ``${roots.…}`` token to something that will spawn it.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import machine_roots
from agent_runtime.machine_roots import MACHINE_ROOTS_FILENAME, machine_roots_cache_clear
from agent_runtime.models import AgentPersona
from agent_runtime.profile_readiness import profile_readiness_for_persona


@pytest.fixture(autouse=True)
def _clear_roots_cache():
    machine_roots_cache_clear()
    yield
    machine_roots_cache_clear()


def _qa_persona(**overrides) -> AgentPersona:
    payload = dict(
        id="qa",
        display_name="QA",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/qa/system.md",
        hermes_profile="qa",
        skills=[],
        required_mcp_servers=["launcher_qa"],
    )
    payload.update(overrides)
    return AgentPersona(**payload)


def _bind_profile(tmp_path, monkeypatch, config_text: str, *, registry: dict | None = None):
    from agent_runtime import profile_context

    profile_home = tmp_path / "profiles" / "qa"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(config_text, encoding="utf-8")
    if registry is not None:
        (profile_home / MACHINE_ROOTS_FILENAME).write_text(
            json.dumps({"schema_version": 1, "roots": registry}), encoding="utf-8"
        )
    monkeypatch.setattr(profile_context, "profile_exists", lambda name: name == "qa")
    monkeypatch.setattr(profile_context, "get_profile_dir", lambda name: profile_home)
    return profile_home


# ── Readiness ───────────────────────────────────────────────────────────────


def test_readiness_reports_an_unbound_root_with_the_exact_fix(tmp_path, monkeypatch):
    _bind_profile(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n"
        "  launcher_qa:\n"
        "    command: ${roots.eternia_launcher}/tool/server${exe_suffix}\n",
    )

    readiness = profile_readiness_for_persona(_qa_persona())

    assert readiness["readiness"] == "mcp_attention"
    assert [row["code"] for row in readiness["machine_root_issues"]] == ["unbound_root"]
    assert "hermes harness roots set eternia_launcher" in readiness["summary"]
    # The server IS present in the config — the miss is the machine binding, and
    # the two must not be conflated.
    assert readiness["missing_mcp_servers"] == []


def test_readiness_is_clean_once_the_root_is_bound(tmp_path, monkeypatch):
    repo = tmp_path / "EterniaLauncher"
    (repo / "tool").mkdir(parents=True)
    _bind_profile(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n"
        "  launcher_qa:\n"
        "    command: ${roots.eternia_launcher}/tool/server${exe_suffix}\n",
        registry={"eternia_launcher": str(repo)},
    )

    readiness = profile_readiness_for_persona(_qa_persona())

    assert readiness["machine_root_issues"] == []
    assert readiness["readiness"] == "ready"


def test_readiness_reports_a_root_bound_to_a_vanished_checkout(tmp_path, monkeypatch):
    _bind_profile(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n  launcher_qa:\n    command: ${roots.eternia_launcher}/tool/server\n",
        registry={"eternia_launcher": str(tmp_path / "gone")},
    )

    readiness = profile_readiness_for_persona(_qa_persona())

    assert [row["code"] for row in readiness["machine_root_issues"]] == ["root_target_missing"]
    assert readiness["readiness"] == "mcp_attention"


def test_readiness_reports_a_windows_only_capability_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_roots, "current_platform_key", lambda: "linux")
    _bind_profile(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n"
        "  launcher_qa:\n"
        "    command: powershell.exe\n"
        "    platforms:\n"
        "      - windows\n",
    )

    readiness = profile_readiness_for_persona(_qa_persona())

    assert [row["code"] for row in readiness["machine_root_issues"]] == ["platform_unsupported"]
    assert "linux" in readiness["summary"]


def test_readiness_ignores_tokenless_configs_entirely(tmp_path, monkeypatch):
    _bind_profile(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n  launcher_qa:\n    command: launcher-qa\n",
    )

    readiness = profile_readiness_for_persona(_qa_persona())

    assert readiness["machine_root_issues"] == []
    assert readiness["readiness"] == "ready"


def test_readiness_surfaces_an_unexpanded_persona_repo_scope(tmp_path, monkeypatch):
    _bind_profile(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n  launcher_qa:\n    command: launcher-qa\n",
    )

    readiness = profile_readiness_for_persona(
        _qa_persona(repo_scope="${roots.eternia_backend}")
    )

    assert [row["code"] for row in readiness["machine_root_issues"]] == ["unbound_root"]
    assert readiness["machine_root_issues"][0]["field"].endswith("repo_scope")


# ── Persona config load ─────────────────────────────────────────────────────


def test_persona_repo_scope_expands_at_config_load(tmp_path, monkeypatch):
    from agent_runtime import config as runtime_config

    repo = tmp_path / "EterniaLauncher"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / MACHINE_ROOTS_FILENAME).write_text(
        json.dumps({"roots": {"eternia_launcher": str(repo)}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    assert (
        runtime_config._expand_machine_root_tokens("${roots.eternia_launcher}", field="x")
        == str(repo)
    )


def test_persona_repo_scope_keeps_the_literal_token_when_unresolvable(tmp_path, monkeypatch):
    from agent_runtime import config as runtime_config

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    # Not blanked (that would read as "no scope"), not guessed (that would be a
    # fabricated workdir) — left literal so readiness can name the real fix.
    assert (
        runtime_config._expand_machine_root_tokens("${roots.eternia_launcher}", field="x")
        == "${roots.eternia_launcher}"
    )


def test_persona_repo_scope_without_tokens_is_untouched(tmp_path, monkeypatch):
    from agent_runtime import config as runtime_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    machine_roots_cache_clear()
    plain = r"X:\Unreal Engine\Engine\Launcher\EterniaLauncher"
    assert runtime_config._expand_machine_root_tokens(plain, field="x") == plain
    assert runtime_config._expand_machine_root_tokens(None, field="x") is None


# ── Stage C launcher_qa resolution ──────────────────────────────────────────


def test_stagec_resolution_distinguishes_unbound_root_from_not_configured(tmp_path, monkeypatch):
    from agent_runtime import stagec_mcp_visual_provider as provider

    home = tmp_path / "home"
    (home / "profiles" / "qa").mkdir(parents=True)
    (home / "profiles" / "qa" / "config.yaml").write_text(
        "mcp_servers:\n  launcher_qa:\n    command: ${roots.eternia_launcher}/tool/server\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "qa"))
    machine_roots_cache_clear()

    resolution = provider.resolve_launcher_qa_mcp_config(profile_name="qa")

    assert resolution.config is None
    assert resolution.code == "unbound_root"
    assert "hermes harness roots set eternia_launcher" in resolution.fix_hint
    assert provider.load_launcher_qa_mcp_config(profile_name="qa") is None


def test_stagec_resolution_reports_platform_gate(tmp_path, monkeypatch):
    from agent_runtime import stagec_mcp_visual_provider as provider

    monkeypatch.setattr(machine_roots, "current_platform_key", lambda: "linux")
    home = tmp_path / "home"
    (home / "profiles" / "qa").mkdir(parents=True)
    (home / "profiles" / "qa" / "config.yaml").write_text(
        "mcp_servers:\n"
        "  launcher_qa:\n"
        "    command: powershell.exe\n"
        "    platforms:\n"
        "      - windows\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "qa"))
    machine_roots_cache_clear()

    resolution = provider.resolve_launcher_qa_mcp_config(profile_name="qa")

    assert resolution.config is None
    assert resolution.code == "platform_unsupported"
    assert "linux" in resolution.summary


def test_stagec_resolution_expands_a_bound_root(tmp_path, monkeypatch):
    from agent_runtime import stagec_mcp_visual_provider as provider

    repo = tmp_path / "EterniaLauncher"
    (repo / "tool").mkdir(parents=True)
    home = tmp_path / "home"
    profile = home / "profiles" / "qa"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "mcp_servers:\n"
        "  launcher_qa:\n"
        "    command: ${roots.eternia_launcher}/tool/server${exe_suffix}\n"
        "    env:\n"
        "      STAGEC_QA_REPO_ROOT: ${roots.eternia_launcher}\n",
        encoding="utf-8",
    )
    profile.joinpath(MACHINE_ROOTS_FILENAME).write_text(
        json.dumps({"roots": {"eternia_launcher": str(repo)}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))
    machine_roots_cache_clear()

    resolution = provider.resolve_launcher_qa_mcp_config(profile_name="qa")

    assert resolution.code == "ready"
    assert resolution.config is not None
    assert resolution.config.command == str(repo / "tool" / f"server{machine_roots.exe_suffix()}")
    assert resolution.config.env["STAGEC_QA_REPO_ROOT"] == str(repo)


# ── Preflight ───────────────────────────────────────────────────────────────


def test_preflight_surfaces_the_typed_root_code_not_a_flat_missing(monkeypatch):
    from agent_runtime import preflight
    from agent_runtime.stagec_mcp_visual_provider import StageCMcpConfigResolution

    monkeypatch.setattr(preflight, "load_launcher_qa_mcp_config", lambda persona_target: None)
    monkeypatch.setattr(
        preflight,
        "resolve_launcher_qa_mcp_config",
        lambda persona_target: StageCMcpConfigResolution(
            None,
            "unbound_root",
            "Machine root 'eternia_launcher' is not bound on this machine",
            "hermes harness roots set eternia_launcher <absolute-local-path> --yes",
        ),
    )

    check = preflight._mcp_exposure_check()

    assert check.ok is False
    assert check.token == "launcher_qa_mcp=unbound_root"
    assert "eternia_launcher" in check.detail
    assert "hermes harness roots set" in check.actionable_fix


# ── Runtime MCP loader ──────────────────────────────────────────────────────


def test_runtime_mcp_loader_drops_an_unresolvable_server(tmp_path, monkeypatch):
    import tools.mcp_tool as mcp_tool

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    resolved = mcp_tool._resolve_machine_root_tokens(
        {
            "launcher_qa": {"command": "${roots.eternia_launcher}/tool/server"},
            "portable": {"command": "node"},
        }
    )

    assert set(resolved) == {"portable"}


def test_runtime_mcp_loader_is_a_no_op_for_tokenless_configs(tmp_path, monkeypatch):
    import tools.mcp_tool as mcp_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    machine_roots_cache_clear()
    servers = {"launcher_qa": {"command": r"X:\repo\tool\server.exe", "args": []}}
    assert mcp_tool._resolve_machine_root_tokens(servers) == servers


def test_cli_probe_path_refuses_to_spawn_an_unresolved_token(tmp_path, monkeypatch):
    from hermes_cli import mcp_config
    from agent_runtime.machine_roots import MachineRootError

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    machine_roots_cache_clear()

    with pytest.raises(MachineRootError):
        mcp_config._resolve_mcp_server_config({"command": "${roots.eternia_launcher}/tool/x"})


def test_cli_probe_path_is_unchanged_for_tokenless_entries(tmp_path, monkeypatch):
    from hermes_cli import mcp_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    machine_roots_cache_clear()

    entry = {"command": r"X:\repo\tool\server.exe", "args": ["--flag"]}
    assert mcp_config._resolve_mcp_server_config(entry) == entry
