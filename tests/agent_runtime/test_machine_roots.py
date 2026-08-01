"""Contract tests for the machine-root path-token chokepoint.

The bug class this feature exists to prevent is a SILENT dead path: a config
that names a location which does not exist on this machine, resolved into
something plausible-looking, and handed to a spawn/workdir where it fails much
later with an unrelated-sounding error. Every "loud" assertion below is
therefore a contract, not a nicety — sabotage-verified by deleting the guard
and confirming the test goes red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime import machine_roots
from agent_runtime.machine_roots import (
    ISSUE_INVALID_ROOT_TOKEN,
    ISSUE_PLATFORM_UNSUPPORTED,
    ISSUE_ROOT_TARGET_MISSING,
    ISSUE_UNBOUND_ROOT,
    MACHINE_ROOTS_FILENAME,
    MachineRootError,
    MachineRoots,
    contains_path_tokens,
    expand_config_paths,
    load_machine_roots,
    machine_roots_cache_clear,
    mcp_server_issues,
    path_token_issues,
    platform_supported,
    resolve_mcp_servers,
    write_machine_roots,
)


@pytest.fixture(autouse=True)
def _clear_roots_cache():
    machine_roots_cache_clear()
    yield
    machine_roots_cache_clear()


def _roots(tmp_path: Path, name: str = "eternia_launcher") -> tuple[MachineRoots, Path]:
    repo = tmp_path / "EterniaLauncher"
    (repo / "tool" / "build").mkdir(parents=True)
    return MachineRoots(roots={name: str(repo)}), repo


# ── Expansion: both separator styles land on the same native path ───────────


def test_token_expansion_is_separator_agnostic_and_uses_native_joins(tmp_path):
    roots, repo = _roots(tmp_path)
    backslash = expand_config_paths(
        r"${roots.eternia_launcher}\tool\build\server", roots=roots
    )
    forward = expand_config_paths(
        "${roots.eternia_launcher}/tool/build/server", roots=roots
    )
    mixed = expand_config_paths(
        r"${roots.eternia_launcher}/tool\build/server", roots=roots
    )

    expected = str(repo / "tool" / "build" / "server")
    assert backslash == expected
    assert forward == expected
    assert mixed == expected
    # The join used pathlib, so the emitted separator is this OS's separator —
    # never a hardcoded one baked into the config.
    assert backslash == str(Path(backslash))


def test_bare_root_token_expands_to_the_root_itself(tmp_path):
    roots, repo = _roots(tmp_path)
    assert expand_config_paths("${roots.eternia_launcher}", roots=roots) == str(repo)


def test_token_inside_a_larger_command_string_only_normalizes_the_path_part(tmp_path):
    roots, repo = _roots(tmp_path)
    value = "Set-Location '${roots.eternia_launcher}'; dart mcp-server --force-roots-fallback"
    assert expand_config_paths(value, roots=roots) == (
        f"Set-Location '{repo}'; dart mcp-server --force-roots-fallback"
    )


def test_nested_structures_expand_through_the_same_walker(tmp_path):
    roots, repo = _roots(tmp_path)
    value = {
        "command": "${roots.eternia_launcher}/bin/x",
        "args": ["--root", "${roots.eternia_launcher}"],
        "env": {"REPO": "${roots.eternia_launcher}"},
        "timeout": 260,
    }
    expanded = expand_config_paths(value, roots=roots)
    assert expanded["command"] == str(repo / "bin" / "x")
    assert expanded["args"] == ["--root", str(repo)]
    assert expanded["env"]["REPO"] == str(repo)
    assert expanded["timeout"] == 260


# ── Backward compatibility: plain absolute paths are untouched ──────────────


@pytest.mark.parametrize(
    "value",
    [
        r"X:\Unreal Engine\Engine\Launcher\EterniaLauncher\tool\server.exe",
        "/opt/eternia/launcher/tool/server",
        "powershell.exe",
        "",
    ],
)
def test_values_without_tokens_are_returned_unchanged(tmp_path, value):
    roots, _repo = _roots(tmp_path)
    assert expand_config_paths(value, roots=roots) == value
    assert path_token_issues(value, roots=roots) == []
    assert contains_path_tokens(value) is False


def test_existing_mcp_config_without_tokens_survives_resolution_byte_identical(tmp_path):
    roots, _repo = _roots(tmp_path)
    servers = {
        "launcher_qa": {
            "command": r"X:\Unreal Engine\Engine\Launcher\EterniaLauncher\tool\server.exe",
            "args": [],
            "env": {"STAGEC_QA_REPO_ROOT": r"X:\Unreal Engine\Engine\Launcher\EterniaLauncher"},
            "timeout": 260,
        }
    }
    assert resolve_mcp_servers(servers, roots=roots) == servers


def test_env_var_placeholders_are_left_for_the_env_interpolator(tmp_path):
    roots, _repo = _roots(tmp_path)
    value = "Bearer ${MCP_LAUNCHER_QA_API_KEY}"
    assert expand_config_paths(value, roots=roots) == value


# ── Loud failure: unbound root ──────────────────────────────────────────────


def test_unbound_root_raises_a_typed_error_and_never_fabricates_a_path(tmp_path):
    roots, _repo = _roots(tmp_path)
    with pytest.raises(MachineRootError) as excinfo:
        expand_config_paths(
            "${roots.eternia_backend}/manage.py", roots=roots, field="mcp_servers.x.command"
        )
    error = excinfo.value
    assert error.code == ISSUE_UNBOUND_ROOT
    assert "eternia_backend" in error.summary
    assert "hermes harness roots set eternia_backend" in error.fix_hint
    row = error.rows()[0]
    assert row["field"] == "mcp_servers.x.command"
    assert row["root_name"] == "eternia_backend"


def test_unbound_root_non_raising_form_reports_and_keeps_the_token_literal(tmp_path):
    roots, _repo = _roots(tmp_path)
    issues = path_token_issues("${roots.eternia_backend}/manage.py", roots=roots)
    assert [issue.code for issue in issues] == [ISSUE_UNBOUND_ROOT]


def test_malformed_root_token_is_typed_not_ignored(tmp_path):
    roots, _repo = _roots(tmp_path)
    issues = path_token_issues("${roots.not a name}/x", roots=roots)
    assert [issue.code for issue in issues] == [ISSUE_INVALID_ROOT_TOKEN]


# ── Loud failure: bound root whose target is gone ───────────────────────────


def test_bound_root_with_missing_target_is_a_typed_failure(tmp_path):
    roots = MachineRoots(roots={"eternia_launcher": str(tmp_path / "gone")})
    with pytest.raises(MachineRootError) as excinfo:
        expand_config_paths("${roots.eternia_launcher}/bin/x", roots=roots)
    assert excinfo.value.code == ISSUE_ROOT_TARGET_MISSING
    assert "does not exist" in excinfo.value.summary


def test_missing_target_check_can_be_waived_for_pure_text_verification(tmp_path):
    roots = MachineRoots(roots={"eternia_launcher": str(tmp_path / "gone")})
    assert expand_config_paths(
        "${roots.eternia_launcher}", roots=roots, check_target_exists=False
    ) == str(tmp_path / "gone")


# ── Executable suffix per platform ──────────────────────────────────────────


def test_exe_suffix_is_dot_exe_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_roots, "current_platform_key", lambda: "windows")
    roots, repo = _roots(tmp_path)
    assert expand_config_paths(
        "${roots.eternia_launcher}/build/server${exe_suffix}", roots=roots
    ) == str(repo / "build" / "server.exe")


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_exe_suffix_is_bare_off_windows(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(machine_roots, "current_platform_key", lambda: platform)
    roots, repo = _roots(tmp_path)
    assert expand_config_paths(
        "${roots.eternia_launcher}/build/server${exe_suffix}", roots=roots
    ) == str(repo / "build" / "server")


# ── Platform gating ─────────────────────────────────────────────────────────


def test_undeclared_platforms_means_supported_everywhere(monkeypatch):
    for platform in ("windows", "macos", "linux"):
        monkeypatch.setattr(machine_roots, "current_platform_key", lambda p=platform: p)
        assert platform_supported(None) is True
        assert platform_supported([]) is True


def test_windows_only_server_is_dropped_with_a_typed_reason_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_roots, "current_platform_key", lambda: "linux")
    roots, _repo = _roots(tmp_path)
    servers = {
        "launcher_qa": {
            "command": "${roots.eternia_launcher}/build/server${exe_suffix}",
            "platforms": ["windows"],
        },
        "portable": {"command": "node", "args": ["server.js"]},
    }
    reported: list[tuple[str, str]] = []
    resolved = resolve_mcp_servers(
        servers, roots=roots, on_issue=lambda name, issue: reported.append((name, issue.code))
    )
    # No fake availability: the Windows-only capability simply is not there.
    assert set(resolved) == {"portable"}
    assert reported == [("launcher_qa", ISSUE_PLATFORM_UNSUPPORTED)]


def test_windows_only_server_survives_on_windows_and_drops_the_gate_key(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_roots, "current_platform_key", lambda: "windows")
    roots, repo = _roots(tmp_path)
    servers = {
        "launcher_qa": {
            "command": "${roots.eternia_launcher}/build/server${exe_suffix}",
            "platforms": ["windows"],
        }
    }
    resolved = resolve_mcp_servers(servers, roots=roots)
    assert resolved["launcher_qa"]["command"] == str(repo / "build" / "server.exe")
    # ``platforms`` is a harness-side gate, not part of the MCP client's schema.
    assert "platforms" not in resolved["launcher_qa"]


def test_platform_aliases_normalize(monkeypatch):
    monkeypatch.setattr(machine_roots, "current_platform_key", lambda: "macos")
    assert platform_supported(["darwin"]) is True
    assert platform_supported("Windows") is False


# ── Unresolvable servers are dropped, never spawned against a dead path ─────


def test_unresolvable_server_is_dropped_rather_than_handed_a_literal_token(tmp_path):
    roots, _repo = _roots(tmp_path)
    servers = {"backend_mcp": {"command": "${roots.eternia_backend}/manage.py"}}
    reported: list[str] = []
    resolved = resolve_mcp_servers(
        servers, roots=roots, on_issue=lambda _name, issue: reported.append(issue.code)
    )
    assert resolved == {}
    assert reported == [ISSUE_UNBOUND_ROOT]


def test_mcp_server_issues_filters_to_the_requested_capability(tmp_path):
    roots, _repo = _roots(tmp_path)
    # Both names are template-free on purpose — the filter is what is under test,
    # and a ``launcher_qa`` stub would now also report blocking template drift.
    servers = {
        "stagec_probe": {"command": "${roots.eternia_launcher}/build/x"},
        "backend_mcp": {"command": "${roots.eternia_backend}/manage.py"},
    }
    assert mcp_server_issues(servers, only=["stagec_probe"], roots=roots) == []
    codes = [issue.code for issue in mcp_server_issues(servers, only=["backend_mcp"], roots=roots)]
    assert codes == [ISSUE_UNBOUND_ROOT]


# ── Registry ────────────────────────────────────────────────────────────────


def test_registry_round_trips_through_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    result = write_machine_roots({"eternia_launcher": str(tmp_path)}, dry_run=False)
    assert result["written"] is True
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["roots"] == {"eternia_launcher": str(tmp_path)}

    machine_roots_cache_clear()
    assert load_machine_roots(refresh=True).roots["eternia_launcher"] == str(tmp_path)


def test_registry_write_honours_dry_run_at_the_store_chokepoint(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    result = write_machine_roots({"eternia_launcher": str(tmp_path)}, dry_run=True)
    assert result["dry_run"] is True
    assert result["changed"] is True
    assert result["written"] is False
    assert not Path(result["path"]).exists()


def test_profile_registry_overrides_machine_registry_per_key(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "qa"
    profile.mkdir(parents=True)
    (root / MACHINE_ROOTS_FILENAME).write_text(
        json.dumps({"roots": {"a": "/machine/a", "b": "/machine/b"}}), encoding="utf-8"
    )
    (profile / MACHINE_ROOTS_FILENAME).write_text(
        json.dumps({"roots": {"b": "/profile/b"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))
    machine_roots_cache_clear()

    roots = load_machine_roots(refresh=True)
    assert roots.roots == {"a": "/machine/a", "b": "/profile/b"}


def test_unreadable_registry_is_reported_not_swallowed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / MACHINE_ROOTS_FILENAME).write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    machine_roots_cache_clear()

    roots = load_machine_roots(refresh=True)
    assert roots.roots == {}
    assert [issue.code for issue in roots.issues] == ["invalid_registry"]


# ── Realm sync must never carry the registry ────────────────────────────────


def test_machine_roots_registry_is_hard_excluded_from_realm_sync():
    from agent_runtime import realm_sync

    assert MACHINE_ROOTS_FILENAME in realm_sync.HARD_EXCLUDED_PATH_PARTS
    assert realm_sync._is_hard_excluded_path(f"profiles/qa/{MACHINE_ROOTS_FILENAME}") is True
    assert realm_sync._is_hard_excluded_path(MACHINE_ROOTS_FILENAME) is True
    assert realm_sync._is_hard_excluded_path("profiles/qa/config.yaml") is False


def test_realm_sync_refuses_a_publish_that_would_include_the_registry(tmp_path):
    from agent_runtime import realm_sync

    source = tmp_path / MACHINE_ROOTS_FILENAME
    source.write_text(json.dumps({"roots": {}}), encoding="utf-8")
    artifact = realm_sync.RealmSyncArtifact(
        kind="profile",
        source=source,
        relative_path=f"profiles/qa/{MACHINE_ROOTS_FILENAME}",
        destination=None,
    )
    with pytest.raises(realm_sync.RealmSyncError) as excinfo:
        realm_sync._assert_no_secret_artifacts([artifact])
    assert excinfo.value.code == "sync_secret_excluded"
