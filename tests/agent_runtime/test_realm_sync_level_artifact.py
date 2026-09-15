"""The workspace LEVEL rides realm sync — the wiring, not the family's logic.

``test_level_sync.py`` holds the family (validate / hash / adopt-or-hold). This
file holds the four things that are only true because ``realm_sync`` was
changed, and each of them is the exact thing the launcher measured as missing at
`6d79b6d724` (EterniaLauncher
``docs/spatial/planned/one-engine-one-catalogue-levels-per-workspace.md``,
"R15's office half LANDED … and its realm half did NOT"):

1. a level is MINTED as an artifact, so publish carries it;
2. it SURVIVES the publisher's own next publish — ``publish_realm_sync``
   ``rmtree``s the realm subtree and rebuilds it from the artifact list, which
   is why a file the launcher planted by hand was deleted;
3. the generic pull loop does NOT write it (``_destination_for_sync_path`` →
   ``None``) and the named applier does;
4. the publisher records its own baseline, so publishing and then pulling is not
   a HOLD on the environment you just shipped.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import paths as runtime_paths
from agent_runtime.level_sync import (
    LevelStore,
    level_baseline_key,
    level_document_hash,
    read_level_baseline,
    write_level_baseline,
)
from agent_runtime.realm_sync import (
    _destination_for_sync_path,
    _kind_for_sync_path,
    publish_realm_sync,
    pull_realm_sync,
    realm_sync_status,
    resolve_realm_sync_artifacts,
)
from agent_runtime.store import RealmStore, WorkspaceStore


def _document(*, prop_x: float = 1.0, scene_id: str = "ws-x") -> bytes:
    return json.dumps(
        {
            "version": 6,
            "scene": {"id": scene_id, "props": [{"id": "a1b2c3d4", "x": prop_x, "y": 2.0}]},
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _realm_with_repo(tmp_path: Path, name: str = "Level Realm"):
    realm = RealmStore().create(name=name)
    repo = tmp_path / "realm-sync-repo"
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "realm-sync-repo"],
        check=True,
        capture_output=True,
        text=True,
    )
    realm.sync_manifest_ref = str(repo)
    return RealmStore().save(realm), repo


def _git_in(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _realm_with_remote(tmp_path: Path, name: str = "Level Realm"):
    """Server-less realm whose sync repo tracks a local bare upstream, so the
    publish's push and the pull's ``--ff-only`` both exercise the real paths.
    Lifted from ``test_realm_sync.py``'s helper of the same name — a publish
    against a repo with no remote raises ``sync_remote_unreachable``, so the
    remote is a precondition of these tests rather than extra fidelity."""

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True)
    realm, repo = _realm_with_repo(tmp_path, name=name)
    _git_in(repo, "config", "user.email", "level-sync-test@localhost")
    _git_in(repo, "config", "user.name", "Level Sync Test")
    _git_in(repo, "commit", "--allow-empty", "-m", "init")
    _git_in(repo, "remote", "add", "origin", str(bare))
    _git_in(repo, "push", "-u", "origin", "HEAD")
    return realm, repo


def _realm_with_workspace_and_level(tmp_path, raw: bytes = None, *, remote: bool = False):
    realm, repo = (_realm_with_remote(tmp_path) if remote else _realm_with_repo(tmp_path))
    workspace = WorkspaceStore().create(name="Launcher", realm_id=realm.id)
    LevelStore().write(workspace.id, raw if raw is not None else _document())
    return realm, repo, workspace


def _subtree(repo: Path, realm_id: str) -> Path:
    return repo / "realms" / runtime_paths.safe_path_token(realm_id) / "store" / "levels"


# ── (1) minted, and routed as its own kind ──────────────────────────────────


def test_a_workspaces_level_is_published_as_its_own_artifact_kind(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo, workspace = _realm_with_workspace_and_level(tmp_path)

    artifacts = resolve_realm_sync_artifacts(realm.id)
    rows = {item.relative_path: item.kind for item in artifacts}

    rel = f"store/levels/{runtime_paths.safe_path_token(workspace.id)}.json"
    assert rows.get(rel) == "level"
    assert _kind_for_sync_path(rel) == "level"


def test_a_level_belonging_to_another_realms_workspace_does_not_travel(
    isolate_agent_runtime_root, tmp_path
):
    """The publish is realm-scoped by workspace TOKEN — the address lives in the
    filename because hermes does not parse the document. A scan that walked the
    level directory without that filter would publish every workspace on the
    machine into whichever realm happened to be publishing."""

    realm, _repo, _ws = _realm_with_workspace_and_level(tmp_path)
    other_realm = RealmStore().create(name="Other")
    outsider = WorkspaceStore().create(name="Outsider", realm_id=other_realm.id)
    LevelStore().write(outsider.id, _document(prop_x=99.0))

    published = {item.relative_path for item in resolve_realm_sync_artifacts(realm.id)}

    assert f"store/levels/{runtime_paths.safe_path_token(outsider.id)}.json" not in published


def test_a_level_that_will_not_read_publishes_nothing_and_is_refused_typed(
    isolate_agent_runtime_root, tmp_path
):
    """Publish copies the file verbatim, so an undecodable document would travel
    and every peer would refuse it on arrival — leaving the one operator who can
    fix it the only one not told."""

    realm, _repo, workspace = _realm_with_workspace_and_level(tmp_path)
    runtime_paths.level_path(workspace.id).write_bytes(b"{half a document")

    result = publish_realm_sync(realm.id, dry_run=True)

    assert all(not item["path"].startswith("store/levels/") for item in result["artifacts"])
    assert [row["workspace_token"] for row in result["level_sync"]["refused"]] == [
        runtime_paths.safe_path_token(workspace.id)
    ]


# ── (2) it survives the publisher's own next publish ────────────────────────


def test_the_level_is_still_in_the_subtree_after_a_second_publish(
    isolate_agent_runtime_root, tmp_path
):
    """THE reason this change had to be made in hermes at all. ``publish_realm_sync``
    ``rmtree``s the realm subtree and rebuilds it from ``artifacts``, so a file
    the launcher dropped into the repo by hand is deleted the next time anything
    publishes. Minting it as an artifact is what makes it survive.

    The second publish changes something ELSE (a new workspace), so the rebuild
    is genuinely re-run rather than skipped by the content-change detector."""

    realm, repo, workspace = _realm_with_workspace_and_level(tmp_path, remote=True)
    token = runtime_paths.safe_path_token(workspace.id)
    raw = LevelStore().read(workspace.id)

    publish_realm_sync(realm.id)
    assert (_subtree(repo, realm.id) / f"{token}.json").read_bytes() == raw

    WorkspaceStore().create(name="Second", realm_id=realm.id)
    publish_realm_sync(realm.id)

    assert (_subtree(repo, realm.id) / f"{token}.json").read_bytes() == raw


# ── (3) the generic loop does not own it; the applier does ──────────────────


def test_the_generic_pull_loop_refuses_to_route_a_level_path():
    """A positive guarantee would be a lie here and a NEGATIVE one is the point:
    this is the branch that keeps a raw last-write-wins overwrite off somebody's
    authored environment. Over-approximating is the safe direction."""

    assert _destination_for_sync_path("store/levels/ws_launcher.json") is None


def test_a_pulled_level_lands_through_the_store_door(isolate_agent_runtime_root, tmp_path):
    """The pull LOOP reaches the applier. The subtree is written by hand rather
    than by a peer's push because what is under test is the call, not git: the
    family's own arms are exhaustive in ``test_level_sync.py``, and a second
    clone here would buy fidelity about a lane realm sync already pins.

    A no-remote realm on purpose — ``pull_realm_sync`` skips ``git pull`` when
    there is no remote, so the only thing between the subtree and the store is
    the applier this change added.
    """

    realm, repo, workspace = _realm_with_workspace_and_level(tmp_path)
    token = runtime_paths.safe_path_token(workspace.id)
    raw = LevelStore().read(workspace.id)
    write_level_baseline(realm.id, {level_baseline_key(token): level_document_hash(raw)})

    theirs = _document(prop_x=42.0)
    subtree = _subtree(repo, realm.id)
    subtree.mkdir(parents=True, exist_ok=True)
    (subtree / f"{token}.json").write_bytes(theirs)

    result = pull_realm_sync(realm.id)

    assert result["level_sync"]["adopted"] == [token]
    assert LevelStore().read(workspace.id) == theirs


def test_the_pull_ack_carries_the_level_key_even_when_no_level_travelled(
    isolate_agent_runtime_root, tmp_path
):
    """Emitted unconditionally: an omitted key cannot tell "this realm publishes
    no level" apart from "this ack came from a hermes with no level family", and
    the launcher's version-skew rule has to tell those two apart."""

    realm, _repo = _realm_with_remote(tmp_path)
    WorkspaceStore().create(name="Levelless", realm_id=realm.id)
    publish_realm_sync(realm.id)

    result = pull_realm_sync(realm.id)

    assert "level_sync" in result
    assert result["level_sync"]["source"] is None


# ── (4) the publisher's own baseline ────────────────────────────────────────


def test_publishing_then_pulling_is_not_a_hold_on_my_own_environment(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo, workspace = _realm_with_workspace_and_level(tmp_path, remote=True)
    token = runtime_paths.safe_path_token(workspace.id)
    raw = LevelStore().read(workspace.id)

    publish_realm_sync(realm.id)

    assert read_level_baseline(realm.id)[level_baseline_key(token)] == level_document_hash(raw)

    result = pull_realm_sync(realm.id)

    assert result["level_sync"]["held"] == []
    assert result["level_sync"]["converged"] == [token]


def test_status_reports_an_unpublished_level_as_unpublished(
    isolate_agent_runtime_root, tmp_path
):
    realm, _repo, workspace = _realm_with_workspace_and_level(tmp_path)
    token = runtime_paths.safe_path_token(workspace.id)

    fresh = realm_sync_status(realm.id)
    assert fresh["levels"] == {
        "publishable": 1,
        "unpublished": 1,
        "held": [],
        "refused": [],
    }

    write_level_baseline(
        realm.id,
        {level_baseline_key(token): level_document_hash(LevelStore().read(workspace.id))},
    )

    assert realm_sync_status(realm.id)["levels"]["unpublished"] == 0


def test_the_level_family_is_not_a_store_drift_family(isolate_agent_runtime_root, tmp_path):
    """Deliberate, and pinned so a later lane has to make the decision rather
    than inherit it: ``realm_revert`` subscripts ``_PROCESS_ORDER[row.family]``
    directly and dispatches on family for the store door, so a drift row with no
    revert arm hands ``revert --all`` a ``KeyError`` and offers the operator an
    exit that does not exist. The level family's revert arm is a filed
    follow-up; when it lands, this test changes with it."""

    realm, _repo, _ws = _realm_with_workspace_and_level(tmp_path)

    families = {row["family"] for row in realm_sync_status(realm.id)["store_drift"]["items"]}

    assert "level" not in families
