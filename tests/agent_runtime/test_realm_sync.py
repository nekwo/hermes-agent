import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_constants import get_hermes_home, get_shared_skills_dir

from agent_runtime import paths as runtime_paths
from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
from agent_runtime.events import EventLog
from agent_runtime.realm_sync import (
    RealmSyncError,
    _git,
    _git_clone,
    publish_realm_sync,
    pull_realm_sync,
    read_realm_sync_sidecar,
    realm_sync_sidecar_path,
    realm_sync_status,
    resolve_realm_sync_artifacts,
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
    skill = get_shared_skills_dir() / "demo-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo-skill\n---\n# Demo\n", encoding="utf-8")
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
    # ``dev``'s role prompt is REPOSITORY-bundled
    # (``agent_runtime/prompts/dev.md``): it already ships with every member's
    # hermes and no persona definition addresses a copy inside a profile home, so
    # publishing it only ever wrote a dead file there. It is now withheld — and
    # ACCOUNTED, never silently omitted. Nothing publishes under ``profiles/``
    # any more; the persona definitions travel as the projection.
    assert all(not path.startswith("profiles/") for path in paths)
    assert "store/personas.yaml" in paths
    withheld = {
        (row["persona_id"], row["kind"], row["reason"])
        for row in result["profile_files"]["withheld"]
    }
    # S11 removed the hardcoded dev persona declaration, so the sync layer has
    # no undeclared profile file to account for.
    assert withheld == set()
    assert all("blueprint" not in path.lower() for path in paths)
    assert all("state.db" not in path.lower() for path in paths)
    assert all("\\" not in path for path in paths)


def test_publish_secret_candidate_hard_fails(isolate_agent_runtime_root, tmp_path):
    home = get_hermes_home()
    skill = get_shared_skills_dir() / "leaky-skill" / "SKILL.md"
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
    installed = get_shared_skills_dir() / "harness-runtime-model" / "SKILL.md"
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


def test_publish_syncs_multi_file_skill_package(isolate_agent_runtime_root, tmp_path):
    # A multi-file skill (references/, scripts/) must sync WHOLE, while junk
    # (__pycache__) and dotfiles are pruned so they never ride the realm repo.
    pkg = get_shared_skills_dir() / "multi-skill"
    (pkg / "references").mkdir(parents=True)
    (pkg / "scripts").mkdir(parents=True)
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "SKILL.md").write_text("---\nname: multi-skill\n---\n# Multi\n", encoding="utf-8")
    (pkg / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (pkg / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    (pkg / "__pycache__" / "x.pyc").write_text("junk\n", encoding="utf-8")
    (pkg / ".scratch_note").write_text("local only\n", encoding="utf-8")

    realm, _repo = _realm_with_repo(tmp_path)
    result = publish_realm_sync(realm.id, dry_run=True)
    paths = [item["path"] for item in result["artifacts"]]

    assert "skills/multi-skill/SKILL.md" in paths
    assert "skills/multi-skill/references/guide.md" in paths
    assert "skills/multi-skill/scripts/run.py" in paths
    assert all("__pycache__" not in path for path in paths)
    assert all(".scratch_note" not in path for path in paths)


def test_publish_refuses_duplicate_profile_skill_authority(
    isolate_agent_runtime_root, tmp_path
):
    shared = get_shared_skills_dir() / "duplicate-skill" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("---\nname: duplicate-skill\n---\n# Shared\n", encoding="utf-8")
    profile = get_hermes_home() / "skills" / "duplicate-skill" / "SKILL.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("---\nname: duplicate-skill\n---\n# Profile\n", encoding="utf-8")
    realm, _repo = _realm_with_repo(tmp_path)

    with pytest.raises(RealmSyncError) as exc:
        publish_realm_sync(realm.id, dry_run=True)

    assert exc.value.code == "skill_authority_conflict"
    assert exc.value.safe_details["skill"] == "duplicate-skill"
    assert exc.value.safe_details["resolution_status"] == "collision"


def test_sync_destination_routes_skills_to_inbox_not_shared_root(isolate_agent_runtime_root, tmp_path):
    from agent_runtime.realm_sync import _destination_for_sync_path, _mirror_realm_skill_inbox
    from agent_runtime.skill_promotion import classify_promotion, realm_inbox_dir

    # C3: skills no longer pull straight into the canonical shared root through
    # the generic overwrite loop. Every skills/* path leaves the loop (returns
    # None) — apply_skill_inbox_pull mirrors them into the resolver-invisible
    # per-realm inbox and promotes through the one guarded door, so a realm pull
    # can never silently clobber a local canonical skill of the same id.
    assert _destination_for_sync_path("skills/foo/references/guide.md") is None
    assert _destination_for_sync_path("skills/foo/SKILL.md") is None

    # Real (non-vacuous) traversal proof: the INBOX MIRROR route confines every
    # write strictly UNDER the realm inbox — a hostile source tree can never
    # escape it — and the guarded promotion door refuses traversal / absolute /
    # drive-letter / multi-level slugs so none can become canonical. (The old
    # ``_destination_for_sync_path("skills/foo/../evil.md") is None`` assertion was
    # vacuous: EVERY skills/* path returns None regardless of traversal.)
    source_skills = tmp_path / "subtree" / "skills"
    (source_skills / "legit").mkdir(parents=True)
    (source_skills / "legit" / "SKILL.md").write_text(
        "---\nname: legit\n---\n# L\n", encoding="utf-8"
    )
    inbox = realm_inbox_dir("hostile-realm")
    removed, reserved = _mirror_realm_skill_inbox(source_skills, inbox)
    assert removed == [] and reserved == []
    written = [p for p in inbox.rglob("*") if p.is_file()]
    assert written, "mirror should have written the legit package"
    inbox_resolved = inbox.resolve()
    for path in written:
        # Every mirrored file is strictly contained under the inbox root — no
        # write escapes to a parent/sibling location.
        assert inbox_resolved in path.resolve().parents
    assert not (inbox.parent / "evil.md").exists()

    # The guarded promotion door refuses hostile slugs outright — never promoted,
    # so never written to the canonical root.
    for hostile in ("../x", "a/../../x", "/abs/x", "C:evil", "skills/../x"):
        plan = classify_promotion(hostile, source_skills / "legit")
        assert plan.action == "refuse_invalid", hostile


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
    skill = get_shared_skills_dir() / "sidecar-skill" / "SKILL.md"
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
    skill = get_shared_skills_dir() / "notify-skill" / "SKILL.md"
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
    skill = get_shared_skills_dir() / "counts-skill" / "SKILL.md"
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


# ── per-realm skill publish selection (REALM_SKILL_SELECTION_DESIGN §2–5, §9) ──


def _make_shared_skill(slug: str, *, body: str | None = None) -> Path:
    skill = get_shared_skills_dir() / slug / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(body or f"---\nname: {slug}\n---\n# {slug}\n", encoding="utf-8")
    return skill


def _resolved_skill_packages(realm_id: str) -> set[str]:
    packages: set[str] = set()
    for artifact in resolve_realm_sync_artifacts(realm_id):
        if artifact.kind == "skill":
            packages.add(Path(artifact.relative_path).parts[1])
    return packages


def _resolved_persona_packages(realm_id: str) -> set[str]:
    """Persona ids this realm publishes.

    Reads the persona-definition PROJECTION plus any profile FILE attributed to a
    persona. It used to scrape ``…/personas/<id>/…`` out of the published path;
    since 2026-07-25 the profile-file family publishes at destination-shaped
    paths where the persona id is not a path segment, and a repository-bundled
    prompt is deliberately withheld — so the path scrape would now report nothing
    and quietly turn these assertions vacuous.
    """

    from agent_runtime.realm_sync import _resolve_artifacts_with_projection

    resolved = _resolve_artifacts_with_projection(realm_id)
    return set(resolved.projection.personas) | {
        artifact.persona_id for artifact in resolved.artifacts if artifact.persona_id
    }


def test_skill_selection_defaults_to_publish_all(isolate_agent_runtime_root, tmp_path):
    # Back-compat lock: a realm with no selection publishes every shared skill.
    for slug in ("alpha", "beta"):
        _make_shared_skill(slug)
    realm, _repo = _realm_with_repo(tmp_path)

    assert realm.skill_publish_mode == "all"
    assert realm.skill_selection == []
    assert {"alpha", "beta"} <= _resolved_skill_packages(realm.id)

    status = realm_sync_status(realm.id)
    assert status["skill_publish_mode"] == "all"
    assert status["skill_selection"] == []
    assert status["skills_published"] >= 2


def test_selected_mode_filters_skill_artifacts(isolate_agent_runtime_root, tmp_path):
    for slug in ("alpha", "beta", "gamma"):
        _make_shared_skill(slug)
    realm, _repo = _realm_with_repo(tmp_path)

    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["gamma", "alpha"])

    assert _resolved_skill_packages(realm.id) == {"alpha", "gamma"}
    status = realm_sync_status(realm.id)
    assert status["skill_publish_mode"] == "selected"
    assert status["skill_selection"] == ["alpha", "gamma"]  # sorted + deduped
    assert status["skills_published"] == 2


def test_republish_prunes_deselected_skills_from_subtree(isolate_agent_runtime_root, tmp_path):
    for slug in ("alpha", "beta"):
        _make_shared_skill(slug)
    realm, repo = _realm_with_remote(tmp_path)
    subtree_skills = repo / "realms" / realm.id / "skills"

    publish_realm_sync(realm.id)
    assert (subtree_skills / "alpha" / "SKILL.md").exists()
    assert (subtree_skills / "beta" / "SKILL.md").exists()

    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["alpha"])
    publish_realm_sync(realm.id)

    assert (subtree_skills / "alpha" / "SKILL.md").exists()
    assert not (subtree_skills / "beta").exists()  # deselected → pruned on republish


def test_skills_drift_only_covers_selected(isolate_agent_runtime_root, tmp_path):
    # Drift is computed over the RESOLVED (already-filtered) artifacts, so a
    # deselected skill can never surface as drift.
    for slug in ("alpha", "beta"):
        _make_shared_skill(slug)
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["alpha"])

    status = realm_sync_status(realm.id)
    assert "beta" not in status["skills_drift"]
    assert _resolved_skill_packages(realm.id) == {"alpha"}


def test_selection_travels_on_pull_while_authority_preserved(isolate_agent_runtime_root, tmp_path):
    _make_shared_skill("alpha")
    realm, repo = _realm_with_remote(tmp_path)
    realm = RealmStore().bind_server(realm.id, "srv_selection")
    realm.default_workspace_id = "ws_backend_default"
    realm.default_workspace_name = "Backend Office"
    realm.default_workspace_version = 8
    realm = RealmStore().save(realm)
    # Local starts in the default "all" mode with no selection.
    assert realm.skill_publish_mode == "all"

    incoming = {
        **json.loads(runtime_paths.realm_path(realm.id).read_text(encoding="utf-8")),
        # Realm-wide truth authored by another member (travels on pull).
        "skill_publish_mode": "selected",
        "skill_selection": ["alpha"],
        "agent_publish_mode": "selected",
        "agent_selection": ["qa"],
        # Stale authority fields must NOT roll our backend-owned pointer back.
        "default_workspace_id": "ws_stale",
        "default_workspace_name": "Stale Office",
        "default_workspace_version": 2,
    }
    remote_realm = repo / "realms" / realm.id / "store" / "realms" / f"{realm.id}.json"
    remote_realm.parent.mkdir(parents=True, exist_ok=True)
    remote_realm.write_text(json.dumps(incoming), encoding="utf-8")

    pull_realm_sync(realm.id)

    pulled = RealmStore().get(realm.id)
    # Selection is NOT an authority field → it adopts the incoming realm truth.
    assert pulled.skill_publish_mode == "selected"
    assert pulled.skill_selection == ["alpha"]
    assert pulled.agent_publish_mode == "selected"
    assert pulled.agent_selection == ["qa"]
    # Authority fields are still preserved against the stale incoming copy.
    assert pulled.default_workspace_id == "ws_backend_default"
    assert pulled.default_workspace_name == "Backend Office"
    assert pulled.default_workspace_version == 8


def test_set_skill_selection_preserves_unknown_slug(isolate_agent_runtime_root, tmp_path):
    # Only alpha exists locally; "ghost" is owned by another member → preserved,
    # never stripped on save (dropping it would corrupt realm truth).
    _make_shared_skill("alpha")
    realm, _repo = _realm_with_repo(tmp_path)

    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["ghost", "alpha", "alpha"])

    stored = RealmStore().get(realm.id)
    assert stored.skill_selection == ["alpha", "ghost"]  # deduped + sorted, unknown kept


def test_set_skill_selection_rejects_malformed_slug(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    for bad in ("bad/slug", "bad\\slug", ".hidden", ""):
        with pytest.raises(ValueError):
            RealmStore().set_skill_selection(realm.id, mode="selected", selection=[bad])
    # A rejected write never mutates the realm.
    assert RealmStore().get(realm.id).skill_publish_mode == "all"


def test_set_skill_selection_names_every_malformed_slug(isolate_agent_runtime_root, tmp_path):
    # A batch save reports ALL offenders in one typed error, not just the first
    # — the launcher sends the whole checkbox set in one --skills batch.
    realm, _repo = _realm_with_repo(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        RealmStore().set_skill_selection(
            realm.id, mode="selected", selection=["bad/slug", ".hidden", "fine"]
        )
    message = str(excinfo.value)
    assert "bad/slug" in message
    assert ".hidden" in message
    assert "fine" not in message
    assert RealmStore().get(realm.id).skill_publish_mode == "all"  # untouched


def test_set_skill_selection_dry_run_does_not_mutate(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["alpha"])

    would_be = RealmStore().set_skill_selection(
        realm.id, mode="selected", selection=[], dry_run=True
    )
    assert would_be.skill_selection == []  # the preview reflects the request

    stored = RealmStore().get(realm.id)
    assert stored.skill_selection == ["alpha"]  # disk untouched
    events = [
        event
        for event in EventLog().tail(50)
        if event.type == "realm.updated" and event.payload.get("change") == "skill_selection"
    ]
    assert len(events) == 1  # only the seeding write emitted


def test_set_skill_selection_emits_store_event(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["a", "b"])

    events = [event for event in EventLog().tail(20) if event.type == "realm.updated"]
    match = next(event for event in events if event.payload.get("change") == "skill_selection")
    assert match.payload["mode"] == "selected"
    assert match.payload["selection_count"] == 2


def test_all_mode_keeps_selection_list(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["alpha", "beta"])
    RealmStore().set_skill_selection(realm.id, mode="all", selection=[])

    stored = RealmStore().get(realm.id)
    assert stored.skill_publish_mode == "all"
    assert stored.skill_selection == ["alpha", "beta"]  # intact — restores on switch back


def test_status_and_sidecar_carry_selection_fields(isolate_agent_runtime_root, tmp_path):
    _make_shared_skill("alpha")
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["alpha"])

    status = realm_sync_status(realm.id)  # writes the sidecar
    assert status["skill_publish_mode"] == "selected"
    assert status["skill_selection"] == ["alpha"]
    assert status["skills_published"] == 1

    sidecar = read_realm_sync_sidecar(realm.id)
    assert sidecar["skill_publish_mode"] == "selected"
    assert sidecar["skill_selection"] == ["alpha"]
    assert sidecar["skills_published"] == 1


def test_sidecar_read_defaults_when_fields_absent(isolate_agent_runtime_root, tmp_path):
    # Tolerant read: a sidecar written by an older hermes lacks the new keys.
    realm, _repo = _realm_with_repo(tmp_path)
    legacy = {"schema_version": 2, "realm_id": realm.id, "state": "in_sync"}
    sidecar_path = realm_sync_sidecar_path(realm.id)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(legacy), encoding="utf-8")

    sidecar = read_realm_sync_sidecar(realm.id)
    assert sidecar["skill_publish_mode"] == "all"
    assert sidecar["skill_selection"] == []
    assert sidecar["skills_published"] == 0


# ── CLI: `realm skills show|set` (design §5) ────────────────────────────────


def test_realm_skills_show_cli_envelope(isolate_agent_runtime_root, tmp_path):
    _make_shared_skill("alpha")
    _make_shared_skill("beta")
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["alpha", "ghost"])

    proc = _run_harness("realm", "skills", "show", realm.id, "--json")
    payload = json.loads(proc.stdout)

    assert proc.returncode == 0
    assert payload["schema_version"] == 1
    assert payload["kind"] == "realm_skill_selection"
    assert payload["id"] == realm.id
    assert payload["mode"] == "selected"
    assert payload["selection"] == ["alpha", "ghost"]
    assert "alpha" in payload["catalog"] and "beta" in payload["catalog"]
    assert "ghost" not in payload["catalog"]
    assert payload["missing"] == ["ghost"]  # honest accounting: selection − catalog


def test_realm_skills_set_cli_selected_and_all(isolate_agent_runtime_root, tmp_path):
    _make_shared_skill("alpha")
    _make_shared_skill("beta")
    realm, _repo = _realm_with_repo(tmp_path)

    proc = _run_harness("realm", "skills", "set", realm.id, "--skills", "beta,alpha,beta", "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["mode"] == "selected"
    assert payload["selection"] == ["alpha", "beta"]

    proc = _run_harness("realm", "skills", "set", realm.id, "--all", "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["mode"] == "all"
    assert payload["selection"] == ["alpha", "beta"]  # --all keeps the list intact


def test_realm_skills_set_cli_none(isolate_agent_runtime_root, tmp_path):
    _make_shared_skill("alpha")
    realm, _repo = _realm_with_repo(tmp_path)
    proc = _run_harness("realm", "skills", "set", realm.id, "--none", "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["mode"] == "selected"
    assert payload["selection"] == []


def test_realm_skills_set_cli_dry_run_previews_without_mutating(isolate_agent_runtime_root, tmp_path):
    _make_shared_skill("alpha")
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["alpha"])

    proc = _run_harness("realm", "skills", "set", realm.id, "--none", "--dry-run", "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["dry_run"] is True
    assert payload["mode"] == "selected"
    assert payload["selection"] == []  # the previewed would-be state

    stored = RealmStore().get(realm.id)
    assert stored.skill_selection == ["alpha"]  # nothing was written


def test_realm_skills_set_cli_malformed_slug_is_typed_error(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    proc = _run_harness("realm", "skills", "set", realm.id, "--skills", "bad/slug", "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == 2
    assert payload["kind"] == "error"
    assert payload["error"]["code"] == "invalid_request"


def test_realm_skills_set_cli_requires_exactly_one_flag(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    both = _run_harness("realm", "skills", "set", realm.id, "--all", "--none", "--json")
    assert both.returncode == 2
    assert json.loads(both.stdout)["error"]["code"] == "invalid_request"
    neither = _run_harness("realm", "skills", "set", realm.id, "--json")
    assert neither.returncode == 2
    assert json.loads(neither.stdout)["error"]["code"] == "invalid_request"


# ── per-realm persona-definition selection (Agent Sync) ────────────────────


def test_agent_selection_defaults_to_required_workspace_personas(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo = _realm_with_repo(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])

    stored = RealmStore().get(realm.id)
    assert stored.agent_publish_mode == "workspace"
    assert stored.agent_selection == []
    packages = _resolved_persona_packages(realm.id)
    assert "dev" in packages
    assert "qa" not in packages


def test_selected_agents_add_to_required_references_and_prune_when_removed(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo = _realm_with_repo(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])

    RealmStore().set_agent_selection(
        realm.id, mode="selected", selection=["qa", "qa"]
    )
    packages = _resolved_persona_packages(realm.id)
    assert {"dev", "qa"} <= packages  # dev is pinned by the workspace

    RealmStore().set_agent_selection(realm.id, mode="selected", selection=[])
    packages = _resolved_persona_packages(realm.id)
    assert "dev" in packages
    assert "qa" not in packages


def test_agent_selection_preserves_unknown_ids_and_emits_event(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo = _realm_with_repo(tmp_path)
    RealmStore().set_agent_selection(
        realm.id,
        mode="selected",
        selection=["profile:remote", "qa", "qa"],
    )

    stored = RealmStore().get(realm.id)
    assert stored.agent_selection == ["profile:remote", "qa"]
    events = [event for event in EventLog().tail(20) if event.type == "realm.updated"]
    match = next(
        event for event in events if event.payload.get("change") == "agent_selection"
    )
    assert match.payload["mode"] == "selected"
    assert match.payload["selection_count"] == 2


def test_agent_selection_rejects_malformed_ids_without_partial_write(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo = _realm_with_repo(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        RealmStore().set_agent_selection(
            realm.id,
            mode="selected",
            selection=["qa", "bad/id", "bad id"],
        )
    assert "bad/id" in str(excinfo.value)
    assert "bad id" in str(excinfo.value)
    assert RealmStore().get(realm.id).agent_publish_mode == "workspace"


def test_realm_agents_cli_show_and_set(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_repo(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])

    proc = _run_harness(
        "realm", "agents", "set", realm.id, "--agents", "qa,profile:remote,qa", "--json"
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["kind"] == "realm_agent_selection"
    assert payload["mode"] == "selected"
    assert payload["selection"] == ["profile:remote", "qa"]
    assert "dev" in payload["required"]
    assert {"dev", "qa"} <= set(payload["published"])
    assert payload["missing"] == ["profile:remote"]

    proc = _run_harness("realm", "agents", "show", realm.id, "--json")
    shown = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert shown["selection"] == ["profile:remote", "qa"]


def test_realm_agents_cli_none_and_workspace_preserve_selection(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo = _realm_with_repo(tmp_path)
    _run_harness("realm", "agents", "set", realm.id, "--agents", "qa", "--json")

    none = _run_harness("realm", "agents", "set", realm.id, "--none", "--json")
    assert none.returncode == 0
    assert json.loads(none.stdout)["selection"] == []

    _run_harness("realm", "agents", "set", realm.id, "--agents", "qa", "--json")
    workspace = _run_harness(
        "realm", "agents", "set", realm.id, "--workspace", "--json"
    )
    payload = json.loads(workspace.stdout)
    assert workspace.returncode == 0
    assert payload["mode"] == "workspace"
    assert payload["selection"] == ["qa"]  # retained for switching back


def test_realm_agents_cli_dry_run_and_flag_validation(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo = _realm_with_repo(tmp_path)
    preview = _run_harness(
        "realm", "agents", "set", realm.id, "--agents", "qa", "--dry-run", "--json"
    )
    payload = json.loads(preview.stdout)
    assert preview.returncode == 0
    assert payload["dry_run"] is True
    assert payload["mode"] == "selected"
    assert payload["selection"] == ["qa"]
    assert RealmStore().get(realm.id).agent_publish_mode == "workspace"

    both = _run_harness(
        "realm", "agents", "set", realm.id, "--workspace", "--none", "--json"
    )
    assert both.returncode == 2
    assert json.loads(both.stdout)["error"]["code"] == "invalid_request"


def test_status_and_sidecar_carry_agent_selection_fields(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo = _realm_with_repo(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])
    RealmStore().set_agent_selection(realm.id, mode="selected", selection=["qa"])

    status = realm_sync_status(realm.id)
    assert status["agent_publish_mode"] == "selected"
    assert status["agent_selection"] == ["qa"]
    assert status["agents_published"] >= 2

    sidecar = read_realm_sync_sidecar(realm.id)
    assert sidecar["agent_publish_mode"] == "selected"
    assert sidecar["agent_selection"] == ["qa"]
    assert sidecar["agents_published"] >= 2


# ── H1: store-drift honesty surfaced in `realm sync status` ────────────────


def test_status_surfaces_store_drift_and_unpublished_changes(
    isolate_agent_runtime_root, tmp_path
):
    from agent_runtime import board_models
    from agent_runtime.board_store import BoardStore
    from agent_runtime.board_sync import update_board_baseline_after_sync

    realm, _repo = _realm_with_repo(tmp_path)
    ws = WorkspaceStore().create(name="Board WS", realm_id=realm.id)
    BoardStore().add_card(workspace_id=ws.id, title="Unpublished card")

    status = realm_sync_status(realm.id)
    boards = status["store_drift"]["boards"]
    assert set(boards) == {"boards_changed", "cards_changed", "cards_added", "cards_removed"}
    # A fresh local board add never touched the repo, so git state is otherwise
    # in_sync — but the store drift is honest about the unpublished changes.
    assert boards["cards_added"] >= 1
    assert boards["boards_changed"] >= 1
    assert status["unpublished_changes"] is True
    # The additive fields never disturb the git-derived state vocabulary.
    assert status["state"] in {"in_sync", "ahead", "behind", "conflict"}

    # After recording the baseline (as a publish/pull would), drift clears.
    update_board_baseline_after_sync(realm.id, [board_models.default_board_id(ws.id)])
    cleared = realm_sync_status(realm.id)
    assert cleared["unpublished_changes"] is False
    assert cleared["store_drift"]["boards"] == {
        "boards_changed": 0,
        "cards_changed": 0,
        "cards_added": 0,
        "cards_removed": 0,
    }


# ── H3: publish no-diff no-op is graceful (launcher auto-publishes) ────────


def test_publish_no_diff_second_run_is_graceful_noop(isolate_agent_runtime_root, tmp_path):
    realm, _repo = _realm_with_remote(tmp_path)
    WorkspaceStore().create(name="WS", realm_id=realm.id, agent_ids=["dev"])

    first = publish_realm_sync(realm.id)
    assert first["changed"] is True
    assert first["state"] == "published"

    # Second publish with nothing changed: no crash, no error, no empty
    # commit/push loop. changed=False proves the commit/push block was skipped.
    second = publish_realm_sync(realm.id)
    assert second["changed"] is False
    assert second["state"] == "published"
    assert second["conflicts"] == []
    assert second["ahead"] == 0


# ── held profile files: the operator resolution path ───────────────────────


def _publish_profile_file(repo: Path, realm_id: str, profile: str, dest_rel: str, body: str) -> None:
    from agent_runtime.profile_artifact_sync import PROFILE_FILES_ROOT
    from agent_runtime.realm_sync import _realm_subtree

    path = _realm_subtree(repo, realm_id).joinpath(*PROFILE_FILES_ROOT.split("/"), profile, *dest_rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))


def test_held_profile_file_is_listed_and_resolvable_and_honors_dry_run(
    isolate_agent_runtime_root, tmp_path
):
    """A held ``MEMORY.md`` must be visible AND actionable, and the mutation verb
    must honor ``--dry-run``.

    ``_add_stage42_global_args(mutation=True)`` auto-registers ``--dry-run``; a
    verb that does not READ ``args.dry_run`` silently mutates on a preview — a
    defect that has recurred twice in this repo. Pinned here at the CLI boundary
    (the store-level chokepoint is pinned in ``test_profile_artifact_sync.py``).
    """

    from agent_runtime.profile_artifact_sync import entity_key
    from agent_runtime.paths import safe_path_token
    from agent_runtime.realm_sync import active_profile_name

    realm, repo = _realm_with_repo(tmp_path)
    profile = safe_path_token(active_profile_name())
    _publish_profile_file(repo, realm.id, profile, "memories/MEMORY.md", "realm memories\n")
    local = get_hermes_home() / "memories" / "MEMORY.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"the member's accumulated memories\n")

    result = pull_realm_sync(realm.id)
    key = entity_key(profile, "memories/MEMORY.md")
    assert result["profile_artifact_sync"]["held"] == [key]
    assert local.read_bytes() == b"the member's accumulated memories\n"
    assert realm_sync_status(realm.id)["profile_artifacts_held"] == [key]

    listed = _run_harness("realm", "sync", "held", realm.id, "--json")
    assert listed.returncode == 0
    assert [row["id"] for row in json.loads(listed.stdout)["items"]] == [key]

    preview = _run_harness("realm", "sync", "resolve", realm.id, "--key", key, "--take", "remote", "--dry-run", "--json")
    assert preview.returncode == 0
    assert json.loads(preview.stdout)["dry_run"] is True
    assert local.read_bytes() == b"the member's accumulated memories\n"  # untouched

    applied = _run_harness("realm", "sync", "resolve", realm.id, "--key", key, "--take", "remote", "--yes", "--json")
    assert applied.returncode == 0
    assert local.read_bytes() == b"realm memories\n"
    assert realm_sync_status(realm.id)["profile_artifacts_held"] == []
