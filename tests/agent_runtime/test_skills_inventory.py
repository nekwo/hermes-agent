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


def test_realm_publish_states_carry_selection_read_from_store():
    """build_realm_publish_states reads mode + selection STRAIGHT from the
    RealmStore realm (design §4) — fresher than the sidecar, present even when
    the realm has never been sync-checked (no sidecar on disk)."""
    from agent_runtime.store import RealmStore

    realm = RealmStore().create(name="Inventory Realm")
    RealmStore().set_skill_selection(realm.id, mode="selected", selection=["beta", "alpha"])

    row = next(r for r in si.build_realm_publish_states() if r["realm_id"] == realm.id)

    # No sidecar was ever written for this realm.
    assert row["sync_state"] is None
    assert row["skill_publish_mode"] == "selected"
    assert row["skill_selection"] == ["alpha", "beta"]  # sorted + deduped
