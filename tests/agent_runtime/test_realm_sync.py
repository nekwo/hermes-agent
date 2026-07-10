import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home

from agent_runtime import paths as runtime_paths
from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
from agent_runtime.realm_sync import (
    RealmSyncError,
    _git,
    _git_clone,
    publish_realm_sync,
    pull_realm_sync,
    read_realm_sync_sidecar,
    realm_sync_sidecar_path,
    realm_sync_status,
)
from agent_runtime.skill_install import HARNESS_SKILLS, harness_skill_hash_mismatches, install_harness_skills
from agent_runtime.store import RealmStore, WorkspaceStore


def _run_harness(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _realm_with_repo(tmp_path: Path, name: str = "Sync Realm"):
    # Server-less (server_id=None) — Stage 43 keeps local realms on the allow
    # stub; server-bound realms now fail closed without a brokered credential.
    realm = RealmStore().create(name=name)
    repo = tmp_path / "realm-sync-repo"
    subprocess.run(["git", "-C", str(tmp_path), "init", "realm-sync-repo"], check=True, capture_output=True, text=True)
    realm.sync_manifest_ref = str(repo)
    realm = RealmStore().save(realm)
    return realm, repo


def _git_in(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _realm_with_remote(tmp_path: Path, name: str = "Remote Realm"):
    """Server-less realm whose sync repo tracks a local bare upstream, so pull
    --ff-only and push both exercise the real remote paths."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True)
    realm, repo = _realm_with_repo(tmp_path, name=name)
    _git_in(repo, "config", "user.email", "realm-sync-test@localhost")
    _git_in(repo, "config", "user.name", "Realm Sync Test")
    _git_in(repo, "commit", "--allow-empty", "-m", "init")
    _git_in(repo, "remote", "add", "origin", str(bare))
    _git_in(repo, "push", "-u", "origin", "HEAD")
    return realm, repo


def _test_credential(realm_id: str = "realm_test", *, expires_at: str = "2999-01-01T00:00:00Z"):
    from agent_runtime.realm_membership import RealmSyncCredential

    return RealmSyncCredential.parse(
        {
            "schema_version": 1,
            "realm_id": realm_id,
            "api_base": "https://api.test.invalid/api",
            "api_token": "etk_test_api_token_000000",
            "git_url": "https://git.test.invalid/realm-sync/test.git",
            "git_authorization": "Bearer forgejo_sekret_value_1234567890",
            "expires_at": expires_at,
        }
    )


def test_harness_runtime_model_is_hash_tracked():
    assert "harness-runtime-model" in HARNESS_SKILLS
    personas = {persona.id: persona for persona in ensure_persisted_personas(load_agent_runtime_config())}
    assert "harness-runtime-model" in personas["neko_supervisor"].skills


def test_publish_dry_run_is_allowlisted_and_excludes_state(isolate_agent_runtime_root, tmp_path):
    home = get_hermes_home()
    skill = home / "skills" / "demo-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo-skill\n---\n# Demo\n", encoding="utf-8")
    system_prompt = home / "personas" / "dev" / "system.md"
    system_prompt.parent.mkdir(parents=True)
    system_prompt.write_text("# Dev system\n", encoding="utf-8")
    (isolate_agent_runtime_root / "blueprints").mkdir(parents=True)
    (isolate_agent_runtime_root / "blueprints" / "already-git-tracked.yaml").write_text("id: bp\n", encoding="utf-8")
    (isolate_agent_runtime_root / "state.db").write_text("do not sync\n", encoding="utf-8")

    realm, _repo = _realm_with_repo(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])

    result = publish_realm_sync(realm.id, dry_run=True)
    paths = [item["path"] for item in result["artifacts"]]

    assert result["state"] == "dry_run"
    assert result["secrets_excluded"] == []
    assert f"skills/demo-skill/SKILL.md" in paths
    assert any(path.startswith("store/workspaces/") for path in paths)
    assert f"store/realms/{realm.id}.json" in paths
    assert "profiles/default/personas/dev/system_prompt/system.md" in paths
    assert all("blueprint" not in path.lower() for path in paths)
    assert all("state.db" not in path.lower() for path in paths)
    assert all("\\" not in path for path in paths)


def test_publish_secret_candidate_hard_fails(isolate_agent_runtime_root, tmp_path):
    home = get_hermes_home()
    skill = home / "skills" / "leaky-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: leaky-skill\n---\napi_key = \"sk-test-secret-value-123456\"\n", encoding="utf-8")
    realm, _repo = _realm_with_repo(tmp_path)

    proc = _run_harness("realm", "sync", "publish", realm.id, "--dry-run", "--json")
    payload = json.loads(proc.stdout)

    assert proc.returncode == 4
    assert payload["kind"] == "error"
    assert payload["error"]["code"] == "sync_secret_excluded"
    assert payload["error"]["safe_details"]["paths"] == ["skills/leaky-skill/SKILL.md"]


def test_pull_reconciles_harness_skill_hash(isolate_agent_runtime_root, tmp_path):
    home = get_hermes_home()
    install_harness_skills(hermes_home=home, skills=["harness-runtime-model"])
    installed = home / "skills" / "harness-runtime-model" / "SKILL.md"
    installed.write_text("# stale local copy\n", encoding="utf-8")
    assert harness_skill_hash_mismatches(["harness-runtime-model"], hermes_home=home) == ["harness-runtime-model"]

    realm, repo = _realm_with_repo(tmp_path)
    synced = repo / "realms" / realm.id / "skills" / "harness-runtime-model" / "SKILL.md"
    synced.parent.mkdir(parents=True)
    synced.write_text("# synced copy\n", encoding="utf-8")

    result = pull_realm_sync(realm.id)

    assert result["state"] == "pulled"
    assert result["skill_reconcile"]["ok"] is True
    assert harness_skill_hash_mismatches(["harness-runtime-model"], hermes_home=home) == []


def test_realm_sync_status_cli_uses_stage42_envelope(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)

    proc = _run_harness("realm", "sync", "status", realm.id, "--json")
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0
    assert payload["schema_version"] == 1
    assert payload["kind"] == "realm_sync"
    assert payload["id"] == realm.id
    assert payload["state"] in {"in_sync", "ahead", "behind", "conflict"}
    assert "sync_repo" in payload


def test_local_workspace_status_is_local(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    workspace = WorkspaceStore().create(name="Local Office", realm_id=realm.id)

    result = realm_sync_status(realm.id)

    assert result["workspace_statuses"] == [
        {"workspace_id": workspace.id, "state": "local"},
    ]


def test_server_workspace_moves_unpublished_to_published_and_back(
    isolate_agent_runtime_root, tmp_path,
):
    realm, _repo = _realm_with_remote(tmp_path)
    realm = RealmStore().bind_server(realm.id, "srv_workspace_status")
    workspace = WorkspaceStore().create(name="Shared Office", realm_id=realm.id)

    before = realm_sync_status(realm.id)
    assert before["workspace_statuses"] == [
        {"workspace_id": workspace.id, "state": "unpublished"},
    ]

    published = publish_realm_sync(realm.id)
    assert published["workspace_statuses"] == [
        {"workspace_id": workspace.id, "state": "published"},
    ]

    workspace.name = "Shared Office Updated"
    WorkspaceStore().save(workspace)
    changed = realm_sync_status(realm.id)
    assert changed["workspace_statuses"] == [
        {"workspace_id": workspace.id, "state": "unpublished"},
    ]


def test_server_pull_preserves_backend_owned_default_pointer(
    isolate_agent_runtime_root, tmp_path,
):
    realm, repo = _realm_with_remote(tmp_path)
    realm = RealmStore().bind_server(realm.id, "srv_authority")
    realm.default_workspace_id = "ws_backend_default"
    realm.default_workspace_name = "Backend Office"
    realm.default_workspace_version = 8
    realm = RealmStore().save(realm)

    stale = {
        **json.loads(runtime_paths.realm_path(realm.id).read_text(encoding="utf-8")),
        "default_workspace_id": "ws_stale",
        "default_workspace_name": "Stale Office",
        "default_workspace_version": 2,
    }
    remote_realm = repo / "realms" / realm.id / "store" / "realms" / f"{realm.id}.json"
    remote_realm.parent.mkdir(parents=True, exist_ok=True)
    remote_realm.write_text(json.dumps(stale), encoding="utf-8")

    pull_realm_sync(realm.id)

    pulled = RealmStore().get(realm.id)
    assert pulled.default_workspace_id == "ws_backend_default"
    assert pulled.default_workspace_name == "Backend Office"
    assert pulled.default_workspace_version == 8


def _server_bound_remote_realm(name: str = "Bound Realm"):
    realm = RealmStore().create(name=name, server_id="srv_9")
    realm.sync_manifest_ref = "https://git.test.invalid/realm-sync/test.git"
    return RealmStore().save(realm)


def test_server_bound_remote_realm_without_credential_fails_closed(isolate_agent_runtime_root):
    realm = _server_bound_remote_realm()

    with pytest.raises(RealmSyncError) as excinfo:
        publish_realm_sync(realm.id, dry_run=True)

    assert excinfo.value.code == "sync_auth_failed"


def test_server_bound_local_path_realm_keeps_local_stub(isolate_agent_runtime_root, tmp_path):
    # Deliberate carve-out: a server-bound realm whose sync repo is a local
    # path (dev/test setups, no remote to protect) stays on the allow stub.
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().bind_server(realm.id, "srv_9")

    result = publish_realm_sync(realm.id, dry_run=True)
    assert result["state"] == "dry_run"


def test_server_bound_realm_cli_denies_without_credential(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.delenv("HERMES_REALM_SYNC_CREDENTIAL", raising=False)
    realm = _server_bound_remote_realm()

    proc = _run_harness("realm", "sync", "status", realm.id, "--json")
    payload = json.loads(proc.stdout)

    assert proc.returncode == 5
    assert payload["kind"] == "error"
    assert payload["error"]["code"] == "sync_auth_failed"


def test_server_bound_realm_with_credential_maps_backend_denial(isolate_agent_runtime_root, monkeypatch):
    import agent_runtime.realm_membership as realm_membership_module

    realm = _server_bound_remote_realm()
    credential = _test_credential(realm.id)

    def _deny(method, url, **kwargs):
        assert method == "GET"
        assert f"/realms/{realm.id}/sync/permission?action=publish" in url
        return 200, {"allowed": False, "code": "role_insufficient", "message": "publisher role required"}

    monkeypatch.setattr(realm_membership_module, "_request_json", _deny)

    with pytest.raises(RealmSyncError) as excinfo:
        publish_realm_sync(realm.id, dry_run=True, credential=credential)

    assert excinfo.value.code == "role_insufficient"


def test_cli_missing_credential_file_fails_closed(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)

    proc = _run_harness("realm", "sync", "status", realm.id, "--credential-file", str(tmp_path / "missing-credential.json"), "--json")
    payload = json.loads(proc.stdout)

    assert proc.returncode == 5
    assert payload["error"]["code"] == "sync_auth_failed"


def test_cli_env_credential_fallback(isolate_agent_runtime_root, tmp_path, monkeypatch):
    realm, _repo = _realm_with_repo(tmp_path)
    cred_path = tmp_path / "realm-sync-credential.json"
    cred_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "realm_id": realm.id,
                "api_base": "https://api.test.invalid/api",
                "api_token": "etk_test_api_token_000000",
                "git_url": "https://git.test.invalid/realm-sync/test.git",
                "git_authorization": "Bearer forgejo_sekret_value_1234567890",
                "expires_at": "2999-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_REALM_SYNC_CREDENTIAL", str(cred_path))

    proc = _run_harness("realm", "sync", "status", realm.id, "--json")
    assert proc.returncode == 0

    expired = json.loads(cred_path.read_text(encoding="utf-8"))
    expired["expires_at"] = "2020-01-01T00:00:00Z"
    cred_path.write_text(json.dumps(expired), encoding="utf-8")

    proc = _run_harness("realm", "sync", "status", realm.id, "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == 5
    assert payload["error"]["code"] == "sync_auth_failed"


def test_git_extra_config_threads_and_never_leaks(isolate_agent_runtime_root, tmp_path, monkeypatch):
    import agent_runtime.realm_sync as realm_sync_module

    repo = tmp_path / "unit-repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    calls: list[list[str]] = []
    real_run = realm_sync_module.subprocess.run

    def _recording_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(realm_sync_module.subprocess, "run", _recording_run)
    header = "http.extraHeader=Authorization: Bearer sekret_value_1234567890"

    _git(repo, "status", "--porcelain", extra_config=[header])
    assert calls[-1][:3] == ["git", "-c", header]

    _git(repo, "status", "--porcelain")
    assert "-c" not in calls[-1]

    with pytest.raises(RealmSyncError) as excinfo:
        _git(repo, "definitely-not-a-git-subcommand", extra_config=[header])
    leaked = json.dumps(excinfo.value.safe_details)
    assert "sekret_value_1234567890" not in leaked
    assert "extraHeader" not in leaked
    assert excinfo.value.safe_details["git_args"] == ["definitely-not-a-git-subcommand"]


def test_git_clone_renders_extra_config(tmp_path, monkeypatch):
    import agent_runtime.realm_sync as realm_sync_module

    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _stub_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(realm_sync_module.subprocess, "run", _stub_run)
    header = "http.extraHeader=Authorization: Bearer sekret_value_1234567890"

    _git_clone("https://git.test.invalid/x.git", tmp_path / "clone-target", extra_config=[header])
    assert calls[-1][:3] == ["git", "-c", header]
    assert "clone" in calls[-1]

    _git_clone("https://git.test.invalid/x.git", tmp_path / "clone-target")
    assert "-c" not in calls[-1]


def test_pull_threads_credential_header_per_invocation_only(isolate_agent_runtime_root, tmp_path, monkeypatch):
    import agent_runtime.realm_sync as realm_sync_module

    realm, repo = _realm_with_remote(tmp_path)
    credential = _test_credential(realm.id)
    calls: list[list[str]] = []
    real_run = realm_sync_module.subprocess.run

    def _recording_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(realm_sync_module.subprocess, "run", _recording_run)
    pull_realm_sync(realm.id, credential=credential)

    header = credential.git_extra_config()[0]
    pull_cmd = next(cmd for cmd in calls if "pull" in cmd)
    assert "-c" in pull_cmd
    assert header in pull_cmd
    # Per-invocation only — the header must never be persisted to .git/config.
    config_text = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "extraheader" not in config_text.lower()
    assert "sekret" not in config_text.lower()


def test_sidecar_written_by_each_verb(isolate_agent_runtime_root, tmp_path):
    home = get_hermes_home()
    skill = home / "skills" / "sidecar-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: sidecar-skill\n---\n# Sidecar\n", encoding="utf-8")
    realm, _repo = _realm_with_remote(tmp_path)
    sidecar = realm_sync_sidecar_path(realm.id)

    realm_sync_status(realm.id)
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["realm_id"] == realm.id
    assert payload["state"] in {"in_sync", "ahead", "behind", "conflict"}
    assert payload["checked_at"]
    assert payload["workspace_statuses"] == []

    sidecar.unlink()
    pull_realm_sync(realm.id)
    assert json.loads(sidecar.read_text(encoding="utf-8"))["last_pull"]

    sidecar.unlink()
    result = publish_realm_sync(realm.id)
    assert result["state"] == "published"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["last_publish"]


def test_snapshot_reads_sidecar_and_never_calls_git(isolate_agent_runtime_root, tmp_path, monkeypatch):
    import agent_runtime.realm_sync as realm_sync_module
    from agent_runtime.snapshot import build_snapshot

    realm, _repo = _realm_with_remote(tmp_path)
    realm_sync_status(realm.id)  # writes the sidecar
    no_sidecar_realm = RealmStore().create(name="No Sidecar Realm")

    def _forbidden(*args, **kwargs):
        raise AssertionError("build_snapshot must not touch realm sync git/artifact paths (Decision 7)")

    monkeypatch.setattr(realm_sync_module, "_git", _forbidden)
    monkeypatch.setattr(realm_sync_module, "_git_clone", _forbidden)
    monkeypatch.setattr(realm_sync_module, "_git_state", _forbidden)
    monkeypatch.setattr(realm_sync_module, "resolve_realm_sync_artifacts", _forbidden)

    snap = build_snapshot()

    synced_row = next(item for item in snap["realms"] if item["id"] == realm.id)
    assert synced_row["sync"]["state"] in {"in_sync", "ahead", "behind", "conflict"}
    assert synced_row["sync"]["checked_at"]
    unsynced_row = next(item for item in snap["realms"] if item["id"] == no_sidecar_realm.id)
    assert unsynced_row["sync"] is None


def test_publish_notify_failure_is_warning_not_error(isolate_agent_runtime_root, tmp_path, monkeypatch):
    import agent_runtime.realm_membership as realm_membership_module

    home = get_hermes_home()
    skill = home / "skills" / "notify-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: notify-skill\n---\n# Notify\n", encoding="utf-8")
    realm, _repo = _realm_with_remote(tmp_path)
    credential = _test_credential(realm.id)

    def _failing_notify(*args, **kwargs):
        raise RealmSyncError("sync_remote_unreachable", "notify endpoint down", retryable=True)

    monkeypatch.setattr(realm_membership_module, "notify_realm_published", _failing_notify)
    result = publish_realm_sync(realm.id, credential=credential)

    assert result["state"] == "published"
    assert result["changed"] is True
    assert result["warnings"][0]["code"] == "sync_notify_failed"


def test_publish_notify_posts_counts_only(isolate_agent_runtime_root, tmp_path, monkeypatch):
    import re

    import agent_runtime.realm_membership as realm_membership_module

    home = get_hermes_home()
    skill = home / "skills" / "counts-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: counts-skill\n---\n# Counts\n", encoding="utf-8")
    realm, _repo = _realm_with_remote(tmp_path)
    credential = _test_credential(realm.id)
    captured: dict = {}

    def _capture_notify(cred, realm_id, *, commit, artifact_counts):
        captured.update({"realm_id": realm_id, "commit": commit, "artifact_counts": artifact_counts})

    monkeypatch.setattr(realm_membership_module, "notify_realm_published", _capture_notify)
    result = publish_realm_sync(realm.id, credential=credential)

    assert result["state"] == "published"
    assert "warnings" not in result
    assert captured["realm_id"] == realm.id
    assert re.fullmatch(r"[0-9a-f]{40}", captured["commit"])
    assert captured["artifact_counts"]["skill"] >= 1
    assert all(isinstance(count, int) for count in captured["artifact_counts"].values())
    assert set(captured) == {"realm_id", "commit", "artifact_counts"}
