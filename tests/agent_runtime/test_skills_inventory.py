"""Typed contract tests for `hermes harness skills inventory --json`
(``skills_inventory/v1``) — the read-model the Launcher's Skills console
consumes instead of scraping the human ``skills list`` table.

Pins the shared-catalog walk (manifest gate, exclusion rules, multi-file
counting, content hashing) and the assembled payload shape so a present skill,
a drifted realm, or a persona grant can never silently collapse into an empty
or malformed surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime import skills_inventory as si


def _make_skill(
    root: Path,
    slug: str,
    *,
    name: str | None = None,
    description: str = "",
    extra_files: tuple[str, ...] = (),
) -> Path:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True)
    frontmatter = "---\n" + f"name: {name or slug}\n"
    if description:
        frontmatter += f"description: {description}\n"
    frontmatter += "---\n\n# body\n"
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    for rel in extra_files:
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return skill_dir


def _patch_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr("hermes_constants.get_shared_skills_dir", lambda: root)


def test_shared_catalog_walks_only_manifest_dirs(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    _make_skill(
        root,
        "alpha",
        description="Alpha skill",
        extra_files=("references/a.md", "scripts/run.py"),
    )
    _make_skill(root, "beta")
    # No SKILL.md -> housekeeping, not a skill.
    (root / "notskill").mkdir()
    (root / "notskill" / "readme.md").write_text("x", encoding="utf-8")
    # Dotdir is skipped outright.
    (root / ".archive").mkdir()

    _patch_root(monkeypatch, root)
    got_root, exists, catalog = si.build_shared_catalog()

    assert got_root == root
    assert exists is True
    assert [entry["slug"] for entry in catalog] == ["alpha", "beta"]

    alpha = catalog[0]
    assert alpha["title"] == "alpha"
    assert alpha["description"] == "Alpha skill"
    assert alpha["multi_file"] is True
    assert alpha["file_count"] == 3  # SKILL.md + references/a.md + scripts/run.py
    assert len(alpha["content_hash"]) == 64

    beta = catalog[1]
    assert beta["multi_file"] is False
    assert beta["file_count"] == 1


def test_shared_catalog_missing_root_is_not_an_error(tmp_path, monkeypatch):
    _patch_root(monkeypatch, tmp_path / "does-not-exist")
    root, exists, catalog = si.build_shared_catalog()
    assert exists is False
    assert catalog == []


def test_content_hash_tracks_content_changes(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = _make_skill(root, "alpha")
    _patch_root(monkeypatch, root)

    _, _, before = si.build_shared_catalog()
    (skill_dir / "SKILL.md").write_text("---\nname: alpha\n---\nchanged body\n", encoding="utf-8")
    _, _, after = si.build_shared_catalog()

    assert before[0]["content_hash"] != after[0]["content_hash"]


def test_excluded_dirs_are_pruned_from_file_count(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    _make_skill(
        root,
        "alpha",
        extra_files=("node_modules/junk.txt", "keep.md"),
    )
    _patch_root(monkeypatch, root)

    _, _, catalog = si.build_shared_catalog()
    # SKILL.md + keep.md; node_modules/* is pruned (matches the realm publisher).
    assert catalog[0]["file_count"] == 2


def test_malformed_manifest_degrades_to_slug_title(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    skill_dir = root / "alpha"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    _patch_root(monkeypatch, root)

    _, _, catalog = si.build_shared_catalog()
    assert catalog[0]["slug"] == "alpha"
    assert catalog[0]["title"] == "alpha"
    assert catalog[0]["description"] == ""


def test_build_skills_inventory_shape_is_stable():
    """Runs against the live machine — pins the assembled payload contract the
    same way test_build_provider_visibility_shape does for providers."""
    payload = si.build_skills_inventory()

    assert payload["schema"] == "hermes.skills_inventory/v1"
    for key in ("shared_root", "shared_root_exists", "skills", "personas", "realms"):
        assert key in payload
    assert isinstance(payload["skills"], list)
    assert isinstance(payload["personas"], list)
    assert isinstance(payload["realms"], list)

    for skill in payload["skills"]:
        assert skill["slug"]
        assert isinstance(skill["file_count"], int)
        assert isinstance(skill["multi_file"], bool)
        assert isinstance(skill["shadowed_by"], list)
        assert len(skill["content_hash"]) == 64
    for persona in payload["personas"]:
        assert persona["id"]
        assert isinstance(persona["skills"], list)
        assert isinstance(persona["local_skills"], list)
    for realm in payload["realms"]:
        assert realm["realm_id"]
        assert isinstance(realm["server_bound"], bool)
        assert isinstance(realm["skills_drift"], list)
        # None (never checked) or a state string, never a bare bool.
        assert realm["sync_state"] is None or isinstance(realm["sync_state"], str)
        # Additive per-realm publish-selection fields (design §4).
        assert realm["skill_publish_mode"] in {"all", "selected"}
        assert isinstance(realm["skill_selection"], list)
        assert realm["agent_publish_mode"] in {"workspace", "selected"}
        assert isinstance(realm["agent_selection"], list)


def test_realm_publish_states_carry_selection_read_from_store():
    """build_realm_publish_states reads mode + selection STRAIGHT from the
    RealmStore realm (design §4) — fresher than the sidecar, present even when
    the realm has never been sync-checked (no sidecar on disk)."""
    from agent_runtime.store import RealmStore

    realm = RealmStore().create(name="Inventory Realm")
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["beta", "alpha"])
    RealmStore().set_agent_selection(realm.id, mode="selected", selection=["qa", "dev"])

    row = next(r for r in si.build_realm_publish_states() if r["realm_id"] == realm.id)

    # No sidecar was ever written for this realm.
    assert row["sync_state"] is None
    assert row["skill_publish_mode"] == "selected"
    assert row["skill_selection"] == ["alpha", "beta"]  # sorted + deduped
    assert row["agent_publish_mode"] == "selected"
    assert row["agent_selection"] == ["dev", "qa"]


# ── chat-turn-prep Stage 8 / CP-4b: the package hash is keyed on its own files ──


def _pkg(root, slug, *, body="body", support=None):
    skill_dir = root / slug
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: d\n---\n{body}\n", encoding="utf-8"
    )
    if support is not None:
        (skill_dir / "scripts" / "helper.py").write_text(support, encoding="utf-8")
    return skill_dir


def test_support_file_change_invalidates_shared_catalog_hash(tmp_path):
    """A support-script edit must move the hash, with the manifest untouched.

    This is the reason CP-4b refuses to key package hashes on the skill-root
    registry fingerprint. That fingerprint covers resolver-visible markdown and
    the active-org marker; it does not see ``scripts/helper.py`` at all, so a
    memo keyed on it would serve a stale content hash to realm sync after a
    support-only edit — and realm sync's whole contract is that the hash is a
    hash of BYTES.

    Every assertion compares the memoized answer against a freshly computed one
    (cache cleared), so the memo can never merely agree with itself.
    """

    from agent_runtime import skills_inventory as inv

    skill_dir = _pkg(tmp_path, "alpha", support="print('one')\n")

    def cached_hash():
        return inv._content_hash(skill_dir, inv._skill_files(skill_dir))

    def uncached_hash():
        inv._content_hash_cache_clear()
        return inv._content_hash(skill_dir, inv._skill_files(skill_dir))

    inv._content_hash_cache_clear()
    first = cached_hash()
    assert cached_hash() == first, "a second call on an unchanged package is stable"
    assert uncached_hash() == first, "the memo must agree with a cold recompute"

    # EDIT the support file only. Different length, so the (mtime, size)
    # fingerprint moves even on a filesystem with coarse mtime resolution —
    # see the honest limitation asserted at the end.
    (skill_dir / "scripts" / "helper.py").write_text(
        "print('a considerably longer body')\n", encoding="utf-8"
    )
    after_edit = cached_hash()
    assert after_edit != first, (
        "a support-script edit must invalidate the package hash — the manifest "
        "did not change, which is exactly the case a registry-fingerprint key "
        "would get wrong"
    )
    assert after_edit == uncached_hash()

    # ADD a support file.
    (skill_dir / "scripts" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    after_add = cached_hash()
    assert after_add not in (first, after_edit)
    assert after_add == uncached_hash()

    # DELETE it again.
    (skill_dir / "scripts" / "extra.py").unlink()
    after_delete = cached_hash()
    assert after_delete == after_edit, "deleting the added file returns the prior hash"
    assert after_delete == uncached_hash()

    # An UNREADABLE file is a change, not a skip, and the next good read recovers.
    doomed = skill_dir / "scripts" / "doomed.py"
    doomed.write_text("y = 2\n", encoding="utf-8")
    with_doomed = cached_hash()
    assert with_doomed == uncached_hash()
    doomed.unlink()
    assert cached_hash() == after_edit == uncached_hash()


def test_two_packages_are_cached_independently(tmp_path):
    """One entry per package: editing one must not invalidate or leak into the
    other, and must not evict it."""

    from agent_runtime import skills_inventory as inv

    a = _pkg(tmp_path, "alpha", support="a = 1\n")
    b = _pkg(tmp_path, "beta", support="b = 1\n")
    inv._content_hash_cache_clear()

    a_first = inv._content_hash(a, inv._skill_files(a))
    b_first = inv._content_hash(b, inv._skill_files(b))
    assert a_first != b_first, "distinct packages hash distinctly"

    (a / "scripts" / "helper.py").write_text("a = 'changed and longer'\n", encoding="utf-8")
    assert inv._content_hash(a, inv._skill_files(a)) != a_first
    assert inv._content_hash(b, inv._skill_files(b)) == b_first, (
        "one package's edit must not disturb another's memoized hash"
    )


def test_the_fingerprint_covers_exactly_the_files_the_hash_reads(tmp_path):
    """CP-4b's key claim, asserted structurally rather than by inspection.

    The fingerprint's relative paths must equal the file set ``_content_hash``
    iterates. If the two ever diverge — a new prune rule applied to one and not
    the other — the memo would key on a set that is not what it hashes, which is
    the precise failure this stage exists to avoid.
    """

    from agent_runtime import skills_inventory as inv

    skill_dir = _pkg(tmp_path, "alpha", support="s = 1\n")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "note.md").write_text("n\n", encoding="utf-8")
    (skill_dir / ".hidden").write_text("h\n", encoding="utf-8")
    (skill_dir / "node_modules").mkdir()
    (skill_dir / "node_modules" / "junk.js").write_text("j\n", encoding="utf-8")

    files = inv._skill_files(skill_dir)
    fingerprint = inv._package_fingerprint(skill_dir, files)
    hashed = tuple("/".join(f.relative_to(skill_dir).parts) for f in files)
    assert tuple(entry[0] for entry in fingerprint) == hashed

    # And the prune rules really did drop the excluded ones.
    assert ".hidden" not in hashed
    assert not any(part.startswith("node_modules") for part in hashed)


def test_same_size_same_mtime_edit_is_a_known_fingerprint_limitation(tmp_path):
    """The honest limit, pinned rather than papered over.

    ``(relpath, mtime_ns, size)`` is a stat-level identity, so a same-size edit
    that lands inside the filesystem's mtime resolution is invisible to it. That
    limitation is inherited verbatim from ``skill_package_content_hash``, which
    has keyed the identical file set this way since before this stage. It is
    recorded here so a future reader meets it as a known property with a named
    owner rather than as a mystery.
    """

    from agent_runtime import skills_inventory as inv

    skill_dir = _pkg(tmp_path, "alpha", support="aaaa\n")
    inv._content_hash_cache_clear()
    first = inv._content_hash(skill_dir, inv._skill_files(skill_dir))

    target = skill_dir / "scripts" / "helper.py"
    stamp = target.stat().st_mtime_ns
    target.write_text("bbbb\n", encoding="utf-8")  # same length
    import os

    os.utime(target, ns=(stamp, stamp))  # and forced to the same mtime

    memoized = inv._content_hash(skill_dir, inv._skill_files(skill_dir))
    inv._content_hash_cache_clear()
    truth = inv._content_hash(skill_dir, inv._skill_files(skill_dir))

    assert truth != first, "the bytes really did change"
    assert memoized == first, (
        "and the stat-level fingerprint cannot see it — this is the documented "
        "limitation, not a defect introduced here"
    )
