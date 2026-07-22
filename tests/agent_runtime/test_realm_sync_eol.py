"""Realm-sync publish line-ending canonicalization + honest change detection
(W3). Proves:

- a publish with no store changes makes no second commit and reports
  ``changed=false`` (the volatile manifest ``generated_at`` no longer forges a
  change, and EOL churn no longer forges one either);
- a CRLF-authored store file lands in the repo tree as LF bytes;
- the repo-root ``.gitattributes`` is written + committed with ``eol=lf``;
- pull of LF artifacts writes correct destinations (LF, valid JSON) on Windows;
- a just-published workspace reads "published", not "unpublished", even when the
  local store file is CRLF while the published artifact is LF (the byte-compare
  would otherwise lie).

Autouse conftest fixtures isolate the runtime root to a tmp dir — these never
touch the live home.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_runtime import paths as runtime_paths
from agent_runtime.realm_sync import (
    publish_realm_sync,
    pull_realm_sync,
    realm_sync_status,
)
from agent_runtime.store import RealmStore, WorkspaceStore


def _git_in(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _realm_with_repo(tmp_path: Path, name: str = "EOL Realm"):
    realm = RealmStore().create(name=name)
    repo = tmp_path / "realm-sync-repo"
    subprocess.run(["git", "-C", str(tmp_path), "init", "realm-sync-repo"], check=True, capture_output=True, text=True)
    realm.sync_manifest_ref = str(repo)
    realm = RealmStore().save(realm)
    return realm, repo


def _realm_with_remote(tmp_path: Path, name: str = "EOL Remote Realm"):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True)
    realm, repo = _realm_with_repo(tmp_path, name=name)
    _git_in(repo, "config", "user.email", "realm-sync-test@localhost")
    _git_in(repo, "config", "user.name", "Realm Sync Test")
    _git_in(repo, "commit", "--allow-empty", "-m", "init")
    _git_in(repo, "remote", "add", "origin", str(bare))
    _git_in(repo, "push", "-u", "origin", "HEAD")
    return realm, repo


def _rev_count(repo: Path) -> int:
    return int(_git_in(repo, "rev-list", "--count", "HEAD").stdout.strip())


def _as_crlf(path: Path) -> None:
    """Rewrite a file with CRLF endings deterministically (idempotent regardless
    of the host OS's default text-mode translation)."""
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    path.write_bytes(raw)


def test_publish_twice_no_store_change_produces_no_second_commit(isolate_agent_runtime_root, tmp_path):
    realm, repo = _realm_with_remote(tmp_path)
    WorkspaceStore().create(name="Office", realm_id=realm.id)

    first = publish_realm_sync(realm.id)
    assert first["changed"] is True
    commits_after_first = _rev_count(repo)

    second = publish_realm_sync(realm.id)
    assert second["changed"] is False
    assert _rev_count(repo) == commits_after_first  # no new commit for a no-op


def test_crlf_authored_store_file_publishes_as_lf(isolate_agent_runtime_root, tmp_path):
    realm, repo = _realm_with_remote(tmp_path)
    ws = WorkspaceStore().create(name="Office", realm_id=realm.id)
    # Force CRLF store files (the Windows text-mode default; made deterministic
    # so this asserts the same thing on any host OS).
    _as_crlf(runtime_paths.workspace_path(ws.id))
    _as_crlf(runtime_paths.realm_path(realm.id))

    publish_realm_sync(realm.id)

    published = [
        repo / "realms" / realm.id / "store" / "workspaces" / f"{ws.id}.json",
        repo / "realms" / realm.id / "store" / "realms" / f"{realm.id}.json",
        repo / "realms" / realm.id / "manifest.json",
    ]
    for artifact in published:
        raw = artifact.read_bytes()
        assert artifact.exists()
        assert b"\r\n" not in raw, f"{artifact.name} still carries CRLF"
        assert json.loads(raw.decode("utf-8"))  # canonicalization kept valid JSON


def test_gitattributes_is_committed_with_eol_lf(isolate_agent_runtime_root, tmp_path):
    realm, repo = _realm_with_remote(tmp_path)
    WorkspaceStore().create(name="Office", realm_id=realm.id)

    publish_realm_sync(realm.id)

    tracked = _git_in(repo, "ls-files", ".gitattributes").stdout.strip()
    assert tracked == ".gitattributes"  # rode the publish lane and is committed
    committed = _git_in(repo, "show", "HEAD:.gitattributes").stdout
    assert "* text=auto eol=lf" in committed


def test_pull_writes_lf_artifact_destinations(isolate_agent_runtime_root, tmp_path):
    realm, repo = _realm_with_repo(tmp_path)  # local repo → pull reads the subtree
    ws = WorkspaceStore().create(name="Office", realm_id=realm.id)
    remote_ws = repo / "realms" / realm.id / "store" / "workspaces" / f"{ws.id}.json"
    remote_ws.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(runtime_paths.workspace_path(ws.id).read_text(encoding="utf-8"))
    payload["name"] = "Renamed By Remote"
    # LF-authored published artifact (as the canonical publisher would write it).
    remote_ws.write_bytes((json.dumps(payload, indent=2) + "\n").encode("utf-8"))

    result = pull_realm_sync(realm.id)

    dest = runtime_paths.workspace_path(ws.id)
    assert dest.exists()
    written = dest.read_bytes()
    assert b"\r\n" not in written
    assert json.loads(written.decode("utf-8"))["name"] == "Renamed By Remote"
    assert result["changed"] is True


def test_workspace_status_published_after_publish_despite_crlf_store(isolate_agent_runtime_root, tmp_path):
    realm, repo = _realm_with_remote(tmp_path)
    realm = RealmStore().bind_server(realm.id, "srv_eol")
    ws = WorkspaceStore().create(name="Office", realm_id=realm.id)

    published = publish_realm_sync(realm.id)
    assert published["workspace_statuses"] == [{"workspace_id": ws.id, "state": "published"}]

    # The published artifact is LF; force the local store back to CRLF. A raw
    # byte compare would now read "unpublished" — the canonical compare must not.
    _as_crlf(runtime_paths.workspace_path(ws.id))

    status = realm_sync_status(realm.id)
    assert status["workspace_statuses"] == [{"workspace_id": ws.id, "state": "published"}]
