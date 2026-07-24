"""C6 tests for the realm skill inbox pull lane (C3) + categorized publish (C5).

Proves the one guarded door end to end through ``pull_realm_sync`` /
``publish_realm_sync``:

- a pull MIRRORS ``subtree/skills/**`` into the resolver-invisible per-realm
  inbox and PRUNES packages/files the realm no longer publishes;
- a package with NO canonical copy AUTO-PROMOTES (canonical + realm provenance +
  ``skill_sync.adopted``);
- an identical package CONVERGES with no rewrite — including an EOL-only
  difference (the inbox mirror is LF-canonical, matching an LF canonical);
- a DIVERGENT package is HELD: canonical bytes untouched, listed in both
  ``skill_sync.held`` and the ``skills_drift`` sidecar/status key;
- the resolver sees exactly ONE candidate for a skill that exists canonical +
  inbox (C1 invisibility, exercised via the real pull);
- publish EXCLUDES ``.realm_inbox`` / ``.provenance`` and PUBLISHES a categorized
  ``<cat>/<name>`` package, with ``selected``-mode matching the bare child name
  AND the categorized id;
- ``pull_realm_sync(dry_run=True)`` leaves the filesystem untouched.

Fixture pattern mirrors ``test_realm_sync.py``: the autouse
``isolate_agent_runtime_root`` (conftest) + the global ``_hermetic_environment``
point HERMES_HOME at a per-test tempdir, so ``get_shared_skills_dir()`` resolves
to an isolated ``<home>/shared/skills`` and a local git repo stands in for the
realm sync repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent.skill_utils import _content_hash_cache_clear, resolve_skills
from agent_runtime.realm_sync import (
    publish_realm_sync,
    pull_realm_sync,
    read_realm_sync_sidecar,
    realm_sync_status,
)
from agent_runtime.skill_promotion import (
    promotion_provenance,
    realm_inbox_dir,
)
from agent_runtime.store import RealmStore
from hermes_constants import get_shared_skills_dir


@pytest.fixture(autouse=True)
def _clean_hash_cache(monkeypatch):
    # A stray HERMES_SHARED_SKILLS from the ambient env would break isolation;
    # a warm package content-hash cache would let one test see another's bytes.
    monkeypatch.delenv("HERMES_SHARED_SKILLS", raising=False)
    _content_hash_cache_clear()
    yield
    _content_hash_cache_clear()


# ── helpers ────────────────────────────────────────────────────────────────


def _local_realm(tmp_path: Path, name: str = "Inbox Realm"):
    """A server-less realm whose sync repo is a local git repo (no remote), so a
    pull reads the on-disk subtree directly — the ``test_realm_sync`` pattern."""

    realm = RealmStore().create(name=name)
    repo = tmp_path / "realm-sync-repo"
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "realm-sync-repo"],
        check=True,
        capture_output=True,
        text=True,
    )
    realm.sync_manifest_ref = str(repo)
    realm = RealmStore().save(realm)
    return realm, repo


def _subtree_skills(repo: Path, realm) -> Path:
    return repo / "realms" / realm.id / "skills"


def _write_subtree_skill(repo: Path, realm, slug: str, files: dict[str, str]) -> Path:
    """Write a skill package into the pulled realm subtree (LF bytes, as the
    canonical publisher would). ``files`` maps package-relative paths → text."""

    pkg = _subtree_skills(repo, realm).joinpath(*slug.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = pkg / Path(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return pkg


def _write_subtree_skill_bytes(repo: Path, realm, slug: str, rel: str, data: bytes) -> Path:
    pkg = _subtree_skills(repo, realm).joinpath(*slug.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    target = pkg / Path(*rel.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return pkg


def _canonical(slug: str) -> Path:
    return get_shared_skills_dir().joinpath(*slug.split("/"))


def _seed_canonical(slug: str, *, body: str) -> Path:
    """Write a canonical shared-root skill package directly (LF bytes)."""

    pkg = _canonical(slug)
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_bytes(
        f"---\nname: {slug.split('/')[-1]}\n---\n{body}".encode("utf-8")
    )
    return pkg


def _write_package(base: Path, slug: str, *, body: str = "# Body\n") -> Path:
    pkg = base.joinpath(*slug.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_bytes(
        f"---\nname: {slug.split('/')[-1]}\n---\n{body}".encode("utf-8")
    )
    return pkg


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


# ── pull: mirror + prune ─────────────────────────────────────────────────────


def test_pull_mirrors_subtree_skills_into_inbox_and_prunes_removed(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(
        repo,
        realm,
        "foo",
        files={"SKILL.md": "---\nname: foo\n---\n# Foo\n", "notes/n.md": "# N\n"},
    )
    _write_subtree_skill(repo, realm, "bar", files={"SKILL.md": "---\nname: bar\n---\n# Bar\n"})

    pull_realm_sync(realm.id)

    inbox = realm_inbox_dir(realm.id)
    assert (inbox / "foo" / "SKILL.md").is_file()
    assert (inbox / "foo" / "notes" / "n.md").is_file()
    assert (inbox / "bar" / "SKILL.md").is_file()

    # The realm stops publishing bar and drops a file from foo — the mirror
    # reflects both deletions (true mirror), reporting the pruned package.
    shutil.rmtree(_subtree_skills(repo, realm) / "bar")
    (_subtree_skills(repo, realm) / "foo" / "notes" / "n.md").unlink()

    result = pull_realm_sync(realm.id)

    assert not (inbox / "bar").exists()
    assert not (inbox / "foo" / "notes").exists()
    assert (inbox / "foo" / "SKILL.md").is_file()
    assert "bar" in result["skill_sync"]["removed"]


# ── pull: auto-promote new ───────────────────────────────────────────────────


def test_pull_auto_promotes_new_package_with_realm_provenance(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(repo, realm, "foo", files={"SKILL.md": "---\nname: foo\n---\n# Foo\n"})
    assert not _canonical("foo").exists()

    result = pull_realm_sync(realm.id)

    assert (_canonical("foo") / "SKILL.md").is_file()
    assert result["skill_sync"]["adopted"] == ["foo"]
    assert result["skill_sync"]["held"] == []
    assert result["changed"] is True

    prov = promotion_provenance("foo")
    assert prov is not None
    assert prov["source"] == {"kind": "realm", "realm_id": realm.id}
    assert "promoted_at" in prov
    # The provenance sidecar lives outside the package (never changes its hash).
    assert not (_canonical("foo") / ".provenance").exists()


# ── pull: converge identical (incl. EOL-only) ────────────────────────────────


def test_pull_converges_identical_package_without_rewrite(tmp_path):
    realm, repo = _local_realm(tmp_path)
    body = "# Foo\n"
    _seed_canonical("foo", body=body)
    before = _snapshot(_canonical("foo"))
    _write_subtree_skill(repo, realm, "foo", files={"SKILL.md": f"---\nname: foo\n---\n{body}"})

    result = pull_realm_sync(realm.id)

    assert result["skill_sync"]["converged"] == ["foo"]
    assert result["skill_sync"]["adopted"] == []
    assert result["skill_sync"]["held"] == []
    # Canonical bytes untouched, and a converge never writes provenance.
    assert _snapshot(_canonical("foo")) == before
    assert promotion_provenance("foo") is None


def test_pull_converges_eol_only_difference(tmp_path):
    realm, repo = _local_realm(tmp_path)
    # Canonical is LF; the realm publishes byte-identical CONTENT but CRLF.
    _seed_canonical("foo", body="# Foo\n")
    before = _snapshot(_canonical("foo"))
    _write_subtree_skill_bytes(
        repo, realm, "foo", "SKILL.md", b"---\r\nname: foo\r\n---\r\n# Foo\r\n"
    )

    result = pull_realm_sync(realm.id)

    # The inbox mirror is LF-canonical at the write chokepoint, so it matches the
    # LF canonical and converges — an EOL-only difference is never spurious drift.
    assert result["skill_sync"]["converged"] == ["foo"]
    assert result["skill_sync"]["held"] == []
    assert _snapshot(_canonical("foo")) == before
    assert (
        (realm_inbox_dir(realm.id) / "foo" / "SKILL.md").read_bytes()
        == b"---\nname: foo\n---\n# Foo\n"
    )
    assert read_realm_sync_sidecar(realm.id)["skills_drift"] == []


# ── pull: hold divergent ─────────────────────────────────────────────────────


def test_pull_holds_divergent_package_and_lists_drift(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _seed_canonical("foo", body="# Canonical v1\n")
    before = _snapshot(_canonical("foo"))
    _write_subtree_skill(repo, realm, "foo", files={"SKILL.md": "---\nname: foo\n---\n# Realm v2\n"})

    result = pull_realm_sync(realm.id)

    assert result["skill_sync"]["held"] == ["foo"]
    assert result["skill_sync"]["adopted"] == []
    assert result["skill_sync"]["converged"] == []
    # Canonical is NOT written — the operator resolves the divergence explicitly.
    assert _snapshot(_canonical("foo")) == before
    # Drift surfaces the held package in both the sidecar and a fresh status.
    assert "foo" in read_realm_sync_sidecar(realm.id)["skills_drift"]
    assert "foo" in realm_sync_status(realm.id)["skills_drift"]


# ── resolver invisibility exercised through a real pull (C1) ─────────────────


def test_resolver_sees_single_candidate_after_divergent_pull(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _seed_canonical("foo", body="# Canonical\n")
    _write_subtree_skill(repo, realm, "foo", files={"SKILL.md": "---\nname: foo\n---\n# Realm differs\n"})

    pull_realm_sync(realm.id)

    # foo now exists canonical + inbox; the resolver must pick exactly one.
    resolution = resolve_skills(["foo"], roots=[get_shared_skills_dir()])["foo"]
    assert resolution.status == "resolved"
    assert len(resolution.candidates) == 1
    assert resolution.candidate.skill_dir == _canonical("foo")


# ── publish: excludes quarantine/provenance; categorized + selection (C5) ─────


def test_publish_excludes_inbox_and_provenance(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _seed_canonical("keeper", body="# Keeper\n")
    # Quarantine + provenance debris under the shared root must be publish-invisible.
    _write_package(realm_inbox_dir("other_realm"), "ghost", body="# Ghost\n")
    provenance = get_shared_skills_dir() / ".provenance"
    provenance.mkdir(parents=True, exist_ok=True)
    (provenance / "keeper.json").write_text("{}", encoding="utf-8")

    result = publish_realm_sync(realm.id, dry_run=True)
    paths = [item["path"] for item in result["artifacts"]]

    assert "skills/keeper/SKILL.md" in paths
    assert all(".realm_inbox" not in path for path in paths)
    assert all(".provenance" not in path for path in paths)
    assert all("ghost" not in path for path in paths)


def test_publish_categorized_package_and_selected_matching(tmp_path):
    realm, repo = _local_realm(tmp_path)
    root = get_shared_skills_dir()
    # A categorized package: the category dir has NO SKILL.md; the child does.
    _write_package(root, "software-development/hermes-agent", body="# Agent\n")
    _write_package(root, "plain", body="# Plain\n")

    # Default "all" mode publishes both the categorized slug and the bare one.
    all_paths = [
        item["path"] for item in publish_realm_sync(realm.id, dry_run=True)["artifacts"]
    ]
    assert "skills/software-development/hermes-agent/SKILL.md" in all_paths
    assert "skills/plain/SKILL.md" in all_paths

    # selected-mode matches the BARE child name (what the Launcher picker offers).
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["hermes-agent"])
    by_child = [
        item["path"] for item in publish_realm_sync(realm.id, dry_run=True)["artifacts"]
    ]
    assert "skills/software-development/hermes-agent/SKILL.md" in by_child
    assert "skills/plain/SKILL.md" not in by_child

    # ...and the categorized id itself matches. The CLI validator rejects a '/'
    # slug (a documented follow-up), so persist it directly through the store.
    stored = RealmStore().get(realm.id)
    stored.skill_publish_mode = "selected"
    stored.skill_selection = ["software-development/hermes-agent"]
    RealmStore().save(stored)
    by_id = [
        item["path"] for item in publish_realm_sync(realm.id, dry_run=True)["artifacts"]
    ]
    assert "skills/software-development/hermes-agent/SKILL.md" in by_id
    assert "skills/plain/SKILL.md" not in by_id


# ── dry-run pull writes nothing ──────────────────────────────────────────────


def test_dry_run_pull_writes_nothing(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(repo, realm, "foo", files={"SKILL.md": "---\nname: foo\n---\n# Foo\n"})
    shared = get_shared_skills_dir()
    before = _snapshot(shared)

    result = pull_realm_sync(realm.id, dry_run=True)

    assert result["state"] == "dry_run"
    # No inbox mirror, no canonical adoption — the mirror is a mutation and a
    # dry-run pull returns before the inbox applier runs.
    assert not realm_inbox_dir(realm.id).exists()
    assert not _canonical("foo").exists()
    assert _snapshot(shared) == before
