import json
import subprocess
import sys
from pathlib import Path

from hermes_constants import get_hermes_home

from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
from agent_runtime.realm_sync import pull_realm_sync, publish_realm_sync
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
    realm = RealmStore().create(name=name, server_id="srv_sync")
    repo = tmp_path / "realm-sync-repo"
    subprocess.run(["git", "-C", str(tmp_path), "init", "realm-sync-repo"], check=True, capture_output=True, text=True)
    realm.sync_manifest_ref = str(repo)
    realm = RealmStore().save(realm)
    return realm, repo


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
