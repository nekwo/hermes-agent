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

from agent.skill_utils import (
    _content_hash_cache_clear,
    resolve_skills,
    skill_package_content_hash,
)
from agent_runtime.realm_sync import (
    publish_realm_sync,
    pull_realm_sync,
    read_realm_sync_sidecar,
    realm_sync_status,
)
from agent_runtime.skill_promotion import (
    realm_inbox_dir,
)
from agent_runtime.store import RealmStore
from hermes_constants import CANONICAL_SHARED_SKILL_IDS, get_shared_skills_dir


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


def _provenance(slug: str):
    """Read a promotion provenance record straight off disk.

    S54 deleted ``skill_promotion.promotion_provenance``, a read-back accessor
    with no production caller. These cases assert what the PRODUCTION WRITER
    put in the record, so the coverage is real and stays -- it just reads the
    file the writer names instead of going through a reader kept alive to be
    tested.
    """

    import json

    path = get_shared_skills_dir() / ".provenance" / f"{slug.replace('/', '__')}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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

    prov = _provenance("foo")
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
    assert _provenance("foo") is None


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


# ── occupancy refusals: pull COMPLETES, package refused, canonical untouched ──


def test_pull_refuses_bare_slug_over_existing_category_dir_and_completes(tmp_path):
    # F1 (regression, pull-wide DoS): canonical holds a CATEGORIZED skill, which
    # makes top-level ``software-development`` a category dir WITHOUT its own
    # SKILL.md. A realm publishing a BARE skill of that name must be REFUSED (an
    # os.replace onto the non-empty category dir would raise and abort the entire
    # pull); the pull must still COMPLETE and the healthy sibling must promote.
    realm, repo = _local_realm(tmp_path)
    _seed_canonical("software-development/hermes-agent", body="# Child\n")
    child_before = _snapshot(_canonical("software-development/hermes-agent"))
    _write_subtree_skill(repo, realm, "healthy", files={"SKILL.md": "---\nname: healthy\n---\n# H\n"})
    _write_subtree_skill(
        repo, realm, "software-development",
        files={"SKILL.md": "---\nname: software-development\n---\n# Bare\n"},
    )

    result = pull_realm_sync(realm.id)

    # Pull completed (never aborted) and the healthy package promoted.
    assert result["state"] == "pulled"
    assert "healthy" in result["skill_sync"]["adopted"]
    # The colliding bare package is refused — not adopted, not held.
    assert "software-development" in result["skill_sync"]["refused"]
    assert "software-development" not in result["skill_sync"]["adopted"]
    assert "software-development" not in result["skill_sync"]["held"]
    # Canonical category + child untouched; no bare SKILL.md injected.
    assert not (_canonical("software-development") / "SKILL.md").exists()
    assert _snapshot(_canonical("software-development/hermes-agent")) == child_before
    # Refused packages are deliberately NOT surfaced as drift.
    assert "software-development" not in read_realm_sync_sidecar(realm.id)["skills_drift"]


def test_pull_refuses_categorized_into_existing_bare_skill(tmp_path):
    # F2 (trust boundary): canonical ``foo`` is a BARE skill package. A realm
    # publishing categorized ``foo/bar`` must be REFUSED — writing bar inside foo
    # would change foo's content hash and inject a new resolvable skill with no
    # gate. Pull completes; foo's hash is unchanged; no new ``bar`` resolves.
    realm, repo = _local_realm(tmp_path)
    foo = _seed_canonical("foo", body="# Foo bare\n")
    _content_hash_cache_clear()
    foo_hash_before = skill_package_content_hash(foo, foo / "SKILL.md")
    foo_snapshot = _snapshot(_canonical("foo"))
    _write_subtree_skill(repo, realm, "healthy", files={"SKILL.md": "---\nname: healthy\n---\n# H\n"})
    _write_subtree_skill(repo, realm, "foo/bar", files={"SKILL.md": "---\nname: bar\n---\n# Bar\n"})

    result = pull_realm_sync(realm.id)

    assert result["state"] == "pulled"
    assert "healthy" in result["skill_sync"]["adopted"]
    assert "foo/bar" in result["skill_sync"]["refused"]
    assert "foo/bar" not in result["skill_sync"]["adopted"]
    # foo's package hash unchanged; no ``bar`` injected inside it.
    _content_hash_cache_clear()
    assert (
        skill_package_content_hash(_canonical("foo"), _canonical("foo") / "SKILL.md")
        == foo_hash_before
    )
    assert _snapshot(_canonical("foo")) == foo_snapshot
    assert not (_canonical("foo") / "bar").exists()
    # No new resolvable ``bar`` skill was injected.
    assert resolve_skills(["bar"], roots=[get_shared_skills_dir()])["bar"].status == "missing"


def test_pull_refuses_reserved_name_package_and_completes(tmp_path, monkeypatch):
    # F3: a realm publishing a package whose path carries a Windows reserved
    # device name must be skipped by the mirror (no write) and reported refused,
    # while the pull COMPLETES and healthy packages promote. A real reserved
    # dir/file can't be materialized on Windows (the OS forbids it / redirects to
    # the device), so a benign marker stands in for the device name here; the
    # reserved-name recognition itself is proven in test_skill_promotion.py.
    from agent_runtime import skill_promotion

    monkeypatch.setattr(
        skill_promotion,
        "is_windows_reserved_component",
        lambda c: str(c or "").split(".", 1)[0].strip().lower() == "blocked",
    )

    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(repo, realm, "healthy", files={"SKILL.md": "---\nname: healthy\n---\n# H\n"})
    _write_subtree_skill(repo, realm, "blocked", files={"SKILL.md": "---\nname: blocked\n---\n# B\n"})

    result = pull_realm_sync(realm.id)

    assert result["state"] == "pulled"
    assert "healthy" in result["skill_sync"]["adopted"]
    assert "blocked" in result["skill_sync"]["refused"]
    assert "blocked" not in result["skill_sync"]["adopted"]
    # Nothing written for the reserved package — neither inbox nor canonical.
    inbox = realm_inbox_dir(realm.id)
    assert not (inbox / "blocked").exists()
    assert not _canonical("blocked").exists()
    # The healthy package IS mirrored + promoted.
    assert (inbox / "healthy" / "SKILL.md").is_file()
    assert (_canonical("healthy") / "SKILL.md").is_file()


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


# ── S2: the skill-delete ledger enforced on pull and publish ─────────────────
#
# Every case below runs against ``_local_realm`` — ``server_id`` is None, the
# asymmetry §5 of the plan names: ``_pulled_artifact_bytes``'s authority-field
# merge only runs for a SERVER-bound realm, so a local realm's pulled realm JSON
# (ledger included) overwrites wholesale, one fewer guard, same LWW posture.
# ``test_pull_adopts_an_arriving_ledger_on_a_local_realm`` pins that directly.


def _archived_package_names() -> list[str]:
    """Slug-flattened package dirs sitting under ``shared/skills/.archive/<ts>``."""

    root = get_shared_skills_dir() / ".archive"
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for stamp in root.iterdir()
        if stamp.is_dir()
        for child in stamp.iterdir()
        if child.is_dir()
    )


def _publish_subtree_realm_record(repo: Path, realm) -> None:
    """Copy this member's realm JSON into the subtree, as a publish would.

    Lets one HERMES_HOME stand in for two members: the ledger written here is
    the ledger that ARRIVES on the next pull through the generic overwrite loop
    (``store/realms/<token>.json`` → the local realm record).
    """

    from agent_runtime import paths as runtime_paths

    token = runtime_paths.safe_path_token(realm.id)
    dest = repo / "realms" / realm.id / "store" / "realms" / f"{token}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(runtime_paths.realm_path(realm.id).read_bytes())


# (a) a tombstoned slug never reaches the promotion door.


def test_pull_drops_a_tombstoned_package_from_the_mirror(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(repo, realm, "doomed", files={"SKILL.md": "---\nname: doomed\n---\n# D\n"})
    _write_subtree_skill(repo, realm, "healthy", files={"SKILL.md": "---\nname: healthy\n---\n# H\n"})
    RealmStore().tombstone_skill(realm.id, "doomed")

    result = pull_realm_sync(realm.id)

    assert result["state"] == "pulled"
    assert result["skill_sync"]["tombstoned"] == ["doomed"]
    # Not mirrored, so the auto-adopt door never sees it; the sibling promotes.
    assert result["skill_sync"]["adopted"] == ["healthy"]
    assert not (realm_inbox_dir(realm.id) / "doomed").exists()
    assert not _canonical("doomed").exists()
    assert (_canonical("healthy") / "SKILL.md").is_file()


# (b) an existing local canonical copy is ARCHIVED, never deleted.


def test_pull_archives_the_local_canonical_copy_of_a_tombstoned_skill(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(repo, realm, "doomed", files={"SKILL.md": "---\nname: doomed\n---\n# D\n"})
    first = pull_realm_sync(realm.id)
    assert first["skill_sync"]["adopted"] == ["doomed"]
    assert (_canonical("doomed") / "SKILL.md").is_file()

    RealmStore().tombstone_skill(realm.id, "doomed")
    result = pull_realm_sync(realm.id)

    assert result["skill_tombstones"]["archived"] == ["doomed"]
    assert result["skill_tombstones"]["warnings"] == []
    assert result["changed"] is True
    # Archived (the skills lane never deletes), gone from the live namespace,
    # and gone from the mirror too — reported as tombstoned, not as removed.
    assert "doomed" in _archived_package_names()
    assert not _canonical("doomed").exists()
    _content_hash_cache_clear()
    assert resolve_skills(["doomed"], roots=[get_shared_skills_dir()])["doomed"].status == "missing"
    assert result["skill_sync"]["tombstoned"] == ["doomed"]
    assert result["skill_sync"]["removed"] == []
    assert not (realm_inbox_dir(realm.id) / "doomed").exists()


def test_pull_adopts_an_arriving_ledger_on_a_local_realm(tmp_path):
    # The propagation shape in one home: member A's delete travels in the realm
    # JSON, and this member — who still holds a live canonical copy and an empty
    # ledger of their own — has it applied on the pull that delivers it.
    realm, repo = _local_realm(tmp_path)
    assert RealmStore().get(realm.id).server_id is None
    _seed_canonical("doomed", body="# Still live here\n")

    RealmStore().tombstone_skill(realm.id, "doomed", deleted_hash="sha256:ab")
    _publish_subtree_realm_record(repo, realm)
    RealmStore().restore_skill(realm.id, "doomed")
    assert RealmStore().get(realm.id).skill_tombstones == []

    result = pull_realm_sync(realm.id)

    # Wholesale overwrite (no server_id → no authority-field merge): the ledger
    # lands, and the same pull enforces it.
    assert [entry.slug for entry in RealmStore().get(realm.id).skill_tombstones] == ["doomed"]
    assert result["skill_tombstones"]["archived"] == ["doomed"]
    assert not _canonical("doomed").exists()
    assert "doomed" in _archived_package_names()
    assert [row["slug"] for row in realm_sync_status(realm.id)["skill_tombstones"]] == ["doomed"]
    assert [row["slug"] for row in read_realm_sync_sidecar(realm.id)["skill_tombstones"]] == [
        "doomed"
    ]


# (c) publish never re-exports a tombstoned slug.


def test_publish_excludes_a_tombstoned_skill(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _seed_canonical("doomed", body="# Doomed\n")
    _seed_canonical("keeper", body="# Keeper\n")

    before = [item["path"] for item in publish_realm_sync(realm.id, dry_run=True)["artifacts"]]
    assert "skills/doomed/SKILL.md" in before

    RealmStore().tombstone_skill(realm.id, "doomed")
    after = [item["path"] for item in publish_realm_sync(realm.id, dry_run=True)["artifacts"]]

    # Defense-in-depth: the canonical copy is still on disk here (this member
    # re-materialized it out of band), and it still may not travel.
    assert (_canonical("doomed") / "SKILL.md").is_file()
    assert "skills/doomed/SKILL.md" not in after
    assert "skills/keeper/SKILL.md" in after


# (d) an installer-owned slug in a ledger is SKIPPED, with a warning.


def test_pull_skips_an_installer_owned_tombstone_with_a_warning(tmp_path):
    # The store chokepoint refuses to mint this (R-B), so it can only arrive
    # from a peer that did not enforce it — written straight to the record here.
    from agent_runtime.models import SkillTombstone
    from hermes_time import now

    slug = sorted(CANONICAL_SHARED_SKILL_IDS)[0]
    realm, repo = _local_realm(tmp_path)
    _seed_canonical(slug, body="# Installer owned\n")
    item = RealmStore().get(realm.id)
    item.skill_tombstones = [SkillTombstone(slug=slug, deleted_at=now())]
    RealmStore().save(item)

    result = pull_realm_sync(realm.id)

    assert result["skill_tombstones"]["skipped_installer_owned"] == [slug]
    assert result["skill_tombstones"]["archived"] == []
    assert [row["code"] for row in result["skill_tombstones"]["warnings"]] == [
        "skill_tombstone_installer_owned"
    ]
    # The package is left standing — the installer re-copies it from repo source
    # on this very pull, so archiving it would be undone inside the same verb.
    assert (_canonical(slug) / "SKILL.md").is_file()
    assert slug not in _archived_package_names()


# (e) restore re-opens the normal promote_new door.


def test_restore_lets_a_republished_package_re_adopt_on_the_next_pull(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(repo, realm, "doomed", files={"SKILL.md": "---\nname: doomed\n---\n# D\n"})
    RealmStore().tombstone_skill(realm.id, "doomed")
    blocked = pull_realm_sync(realm.id)
    assert blocked["skill_sync"]["tombstoned"] == ["doomed"]
    assert not _canonical("doomed").exists()

    RealmStore().restore_skill(realm.id, "doomed")
    result = pull_realm_sync(realm.id)

    # No new machinery: the package walks back in through the same auto-adopt
    # door it was always going to use.
    assert result["skill_sync"]["adopted"] == ["doomed"]
    assert result["skill_sync"]["tombstoned"] == []
    assert (_canonical("doomed") / "SKILL.md").is_file()
    assert _provenance("doomed")["source"] == {"kind": "realm", "realm_id": realm.id}


# (f) the categorized-child match rule, shared with the selection.


def test_tombstone_matches_a_categorized_package_by_its_child_name(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(
        repo, realm, "software-development/hermes-agent",
        files={"SKILL.md": "---\nname: hermes-agent\n---\n# Agent\n"},
    )
    _write_subtree_skill(
        repo, realm, "software-development/sibling",
        files={"SKILL.md": "---\nname: sibling\n---\n# Sibling\n"},
    )
    # The bare child name is what the launcher picker offers, so it is what a
    # delete names — exactly as ``selected`` mode matches it.
    RealmStore().tombstone_skill(realm.id, "hermes-agent")

    result = pull_realm_sync(realm.id)

    assert result["skill_sync"]["tombstoned"] == ["software-development/hermes-agent"]
    assert result["skill_sync"]["adopted"] == ["software-development/sibling"]
    # The innocent sibling under the same category dir is untouched.
    inbox = realm_inbox_dir(realm.id)
    assert not (inbox / "software-development" / "hermes-agent").exists()
    assert (inbox / "software-development" / "sibling" / "SKILL.md").is_file()
    assert not _canonical("software-development/hermes-agent").exists()
    assert (_canonical("software-development/sibling") / "SKILL.md").is_file()

    # ...and the publish filter answers the same way about the same package.
    published = [
        item["path"] for item in publish_realm_sync(realm.id, dry_run=True)["artifacts"]
    ]
    assert "skills/software-development/sibling/SKILL.md" in published
    assert all("hermes-agent" not in path for path in published)


# (g) dry-run stays a read, ledger or not.


def test_dry_run_pull_writes_nothing_with_a_ledger(tmp_path):
    realm, repo = _local_realm(tmp_path)
    _write_subtree_skill(repo, realm, "doomed", files={"SKILL.md": "---\nname: doomed\n---\n# D\n"})
    _seed_canonical("doomed", body="# Local copy\n")
    RealmStore().tombstone_skill(realm.id, "doomed")
    shared = get_shared_skills_dir()
    before = _snapshot(shared)

    result = pull_realm_sync(realm.id, dry_run=True)

    assert result["state"] == "dry_run"
    assert "skill_tombstones" not in result
    # Nothing archived, nothing mirrored — a dry run returns before every
    # applier, and the tombstone lane is an applier like the rest.
    assert _snapshot(shared) == before
    assert _archived_package_names() == []
    assert not realm_inbox_dir(realm.id).exists()
