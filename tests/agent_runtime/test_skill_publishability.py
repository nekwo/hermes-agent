"""Skill publishability: can a resolvable skill reach a realm, and may it be
promoted into the shared root?

Pins three contracts that were previously invisible or unenforced:

1. **Typed publishability per resolver tier.** ``shared_core`` publishes;
   ``profile_local`` / ``external`` structurally cannot, and now SAY so with a
   typed reason instead of silently reporting an empty realm-sync list.
2. **Installer-owned suppression.** The bundled hermes catalog that
   ``tools.skills_sync`` materializes into every profile (tracked by content
   hash in ``<profile_home>/skills/.bundled_manifest``) is neither offered for
   promotion nor admitted by the guarded door — pristine and operator-edited
   copies are distinct typed classes, both refused.
3. **The refusal is a real guard, not an advisory.** ``execute_promotion``
   returns a typed ``refused`` result and leaves the shared root byte-identical,
   including on ``--dry-run``.

Fixture pattern mirrors ``test_skill_promotion.py``: ``tests/conftest.py`` points
HERMES_HOME at a per-test tempdir, so ``get_skills_dir()`` is
``<home>/skills`` (the ``profile_local`` root) and ``get_shared_skills_dir()`` is
``<home>/shared/skills``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent.skill_utils import (
    _content_hash_cache_clear,
    _external_dirs_cache_clear,
    get_skills_dir,
)
from agent_runtime import skill_publishability as sp
from agent_runtime.skill_promotion import classify_promotion, execute_promotion
from hermes_constants import get_shared_skills_dir


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    monkeypatch.delenv("HERMES_SHARED_SKILLS", raising=False)
    _content_hash_cache_clear()
    _external_dirs_cache_clear()
    sp.cache_clear()
    yield
    _content_hash_cache_clear()
    _external_dirs_cache_clear()
    sp.cache_clear()


# ── helpers ────────────────────────────────────────────────────────────────


def _write_package(base: Path, slug: str, *, body: str = "# Body\n", name: str | None = None) -> Path:
    pkg = base.joinpath(*slug.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_text(
        f"---\nname: {name or slug.split('/')[-1]}\n---\n{body}", encoding="utf-8"
    )
    return pkg


def _shared() -> Path:
    root = get_shared_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _profile_skills() -> Path:
    root = get_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_manifest(skills_root: Path, entries: dict[str, str]) -> Path:
    """Write a ``.bundled_manifest`` in the v2 ``name:origin_hash`` form."""
    path = skills_root / sp.BUNDLED_MANIFEST_FILENAME
    path.write_text(
        "\n".join(f"{name}:{origin}" for name, origin in sorted(entries.items())) + "\n",
        encoding="utf-8",
    )
    return path


def _seed_bundled(slug: str, *, body: str = "# Bundled\n", name: str | None = None) -> Path:
    """Materialize an installer-owned package: the package plus a manifest entry
    recording its CURRENT content hash (i.e. pristine, straight from the
    installer)."""
    from tools.skills_sync import _dir_hash

    root = _profile_skills()
    pkg = _write_package(root, slug, body=body, name=name)
    manifest_name = name or slug.split("/")[-1]
    existing = sp.read_bundled_manifest(root / sp.BUNDLED_MANIFEST_FILENAME)
    _write_manifest(root, {**existing, manifest_name: _dir_hash(pkg)})
    sp.cache_clear()
    return pkg


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


# ── 1. publishability per source_kind ──────────────────────────────────────


def test_shared_core_package_is_publishable():
    pkg = _write_package(_shared(), "canonical-skill")
    row = sp.classify_publishability("canonical-skill", pkg, get_shared_skills_dir())

    assert row.source_kind == "shared_core"
    assert row.publishable is True
    assert row.publishable_reason == sp.REASON_SHARED_ROOT
    assert row.root_label == "shared"
    # Already canonical is NOT "promotable" — there is nothing to promote.
    assert row.promotable is False
    assert row.promotion_block_reason == sp.BLOCK_ALREADY_CANONICAL


def test_profile_local_package_reports_that_it_cannot_travel():
    pkg = _write_package(_profile_skills(), "mine")
    row = sp.classify_publishability("mine", pkg, get_skills_dir())

    assert row.source_kind == "profile_local"
    assert row.publishable is False
    assert row.publishable_reason == sp.REASON_PROFILE_LOCAL_ONLY
    assert row.root_label == "profile:default"
    # Not installer-owned and no reserved slug — the honest answer is "promote it".
    assert row.promotable is True
    assert row.promotion_block_reason is None
    # The detail is an operator-facing sentence, never a bare bool for the UI.
    assert "cannot reach a realm" in row.publishable_detail


def test_external_dir_package_reports_that_it_cannot_travel(tmp_path, monkeypatch):
    external = tmp_path / "external-skills"
    external.mkdir()
    pkg = _write_package(external, "vendored")

    row = sp.classify_publishability("vendored", pkg, external, source_kind="external")
    assert row.publishable is False
    assert row.publishable_reason == sp.REASON_EXTERNAL_DIR_ONLY
    assert row.root_label == "external:external-skills"
    assert row.promotable is True


def test_sweep_names_every_tier_and_summarizes_offenders():
    _write_package(_shared(), "canonical-skill")
    _write_package(_profile_skills(), "mine")
    _seed_bundled("gaming")

    rows = sp.build_publishability_rows()
    by_slug = {row["skill"]: row for row in rows}

    assert by_slug["canonical-skill"]["publishable"] is True
    assert by_slug["mine"]["publishable"] is False
    assert by_slug["gaming"]["publishable"] is False
    # ALL offenders named in one pass with per-row typed codes.
    assert by_slug["gaming"]["promotion_block_reason"] == sp.BLOCK_INSTALLER_OWNED_PRISTINE
    assert by_slug["mine"]["promotion_block_reason"] is None
    # Publishable tier sorts first so the unpublishable set reads as a block.
    assert rows[0]["source_kind"] == "shared_core"


# ── 2. installer-owned suppression ─────────────────────────────────────────


def test_pristine_bundled_package_is_installer_owned():
    pkg = _seed_bundled("gaming")
    row = sp.classify_publishability("gaming", pkg, get_skills_dir())

    assert row.installer_owned is True
    assert row.installer_edited is False
    assert row.promotable is False
    assert row.promotion_block_reason == sp.BLOCK_INSTALLER_OWNED_PRISTINE
    assert "fork the installer's catalog" in row.promotion_block_detail


def test_edited_bundled_package_is_a_distinct_typed_class_with_an_escape_hatch():
    pkg = _seed_bundled("gaming")
    (pkg / "SKILL.md").write_text(
        "---\nname: gaming\n---\n# Bundled\nMy own additions, several lines long.\n",
        encoding="utf-8",
    )
    sp.cache_clear()
    _content_hash_cache_clear()

    row = sp.classify_publishability("gaming", pkg, get_skills_dir())
    assert row.installer_owned is True
    assert row.installer_edited is True
    assert row.promotable is False
    assert row.promotion_block_reason == sp.BLOCK_INSTALLER_OWNED_EDITED
    # The refusal must be actionable: an operator's real work is not simply
    # discarded, it is routed to a non-forking path.
    assert "distinct slug" in row.promotion_block_detail


def test_v1_manifest_entry_without_origin_hash_fails_closed():
    """A manifest line with no recorded origin hash cannot prove pristine OR
    edited — the honest verdict is 'unknown', and unknown must not be
    promotable (fail-closed), never silently 'fine'."""
    root = _profile_skills()
    pkg = _write_package(root, "gaming")
    (root / sp.BUNDLED_MANIFEST_FILENAME).write_text("gaming\n", encoding="utf-8")
    sp.cache_clear()

    row = sp.classify_publishability("gaming", pkg, get_skills_dir())
    assert row.installer_owned is True
    assert row.promotable is False
    assert row.promotion_block_reason == sp.BLOCK_INSTALLER_STATE_UNKNOWN


def test_untracked_profile_package_beside_bundled_ones_stays_promotable():
    _seed_bundled("gaming")
    mine = _write_package(_profile_skills(), "my-own-skill")

    row = sp.classify_publishability("my-own-skill", mine, get_skills_dir())
    assert row.installer_owned is False
    assert row.promotable is True


def test_manifest_is_keyed_on_frontmatter_name_not_directory_name():
    """``tools.skills_sync`` records the SKILL.md frontmatter ``name``; a
    categorized package (``skills/mlops/axolotl``) is tracked as ``axolotl``.
    Keying on the path instead would miss every categorized bundled package."""
    pkg = _seed_bundled("mlops/axolotl", name="axolotl")
    assert sp.package_manifest_name(pkg) == "axolotl"

    row = sp.classify_publishability("mlops/axolotl", pkg, get_skills_dir())
    assert row.installer_owned is True
    assert row.promotion_block_reason == sp.BLOCK_INSTALLER_OWNED_PRISTINE


# ── 2b. SABOTAGE: prove the suppression is driven by the manifest ──────────


def test_sabotage_removing_the_manifest_makes_the_same_package_promotable():
    """Sabotage-verify: the ONLY thing making the package unpromotable is the
    installer's manifest. Delete it and the identical bytes become promotable —
    so the guard cannot be passing for an unrelated reason."""
    pkg = _seed_bundled("gaming")
    assert sp.classify_publishability("gaming", pkg, get_skills_dir()).promotable is False

    (_profile_skills() / sp.BUNDLED_MANIFEST_FILENAME).unlink()
    sp.cache_clear()

    row = sp.classify_publishability("gaming", pkg, get_skills_dir())
    assert row.installer_owned is False
    assert row.promotable is True
    assert row.promotion_block_reason is None


def test_sabotage_manifest_naming_a_different_skill_does_not_suppress():
    """Sabotage-verify: a manifest that tracks some OTHER skill must not
    suppress this one — the check is per-package identity, not 'a manifest
    exists in this profile'."""
    root = _profile_skills()
    pkg = _write_package(root, "my-own-skill")
    _write_manifest(root, {"some-other-bundled-skill": "deadbeef"})
    sp.cache_clear()

    assert sp.classify_publishability("my-own-skill", pkg, get_skills_dir()).promotable is True


# ── 3. reserved installer slugs (target identity) ──────────────────────────


def test_promoting_any_source_onto_an_installer_managed_slug_is_refused(tmp_path):
    """A package authored anywhere may not claim a slug the installer manages:
    the shared-root copy would collide with the installer's copy at resolver
    index 0 on every profile."""
    _seed_bundled("gaming")
    outside = _write_package(tmp_path / "authored", "gaming")

    refusal = sp.promotion_refusal("gaming", outside)
    assert refusal is not None
    assert refusal.code == sp.BLOCK_INSTALLER_SLUG_RESERVED


def test_sabotage_a_distinct_slug_for_the_same_bytes_is_allowed(tmp_path):
    """Sabotage-verify the reserved-slug rule: the identical source package
    under a slug the installer does NOT manage is admitted, so the refusal is
    keyed on the slug and not on something incidental to the source."""
    _seed_bundled("gaming")
    outside = _write_package(tmp_path / "authored", "gaming")

    assert sp.promotion_refusal("my-gaming", outside) is None


def test_categorized_slug_is_refused_when_its_leaf_is_installer_managed(tmp_path):
    _seed_bundled("gaming")
    outside = _write_package(tmp_path / "authored", "gaming")

    refusal = sp.promotion_refusal("creative/gaming", outside)
    assert refusal is not None
    assert refusal.code == sp.BLOCK_INSTALLER_SLUG_RESERVED


def test_realm_inbox_source_is_never_installer_owned():
    """The inbox lives under the SHARED root, which is never installer-managed —
    a realm mirror must not be mistaken for a bundled package."""
    from agent_runtime.skill_promotion import realm_inbox_dir

    inbox_pkg = _write_package(realm_inbox_dir("realm_x"), "from-realm")
    assert sp.owning_profile_skills_root(inbox_pkg) is None
    assert sp.classify_installer_ownership(inbox_pkg) is None


def test_shared_root_is_excluded_even_if_it_carries_a_manifest():
    """Defense in depth: a stray ``.bundled_manifest`` in the shared root must
    not turn canonical packages into 'installer-owned'."""
    shared = _shared()
    pkg = _write_package(shared, "canonical-skill")
    _write_manifest(shared, {"canonical-skill": "deadbeef"})
    sp.cache_clear()

    assert sp.owning_profile_skills_root(pkg) is None
    assert sp.classify_installer_ownership(pkg) is None


# ── 4. the guarded door actually refuses, and writes nothing ───────────────


def test_execute_promotion_refuses_a_bundled_source_and_writes_nothing():
    pkg = _seed_bundled("gaming")
    shared = _shared()
    before = _snapshot(shared)

    plan = classify_promotion("gaming", pkg)
    assert plan.action == "promote_new"  # the pure comparison still says "new"

    result = execute_promotion(plan, source={"kind": "profile", "profile": "default"})
    assert result.action == "refused"
    assert result.reason_code == sp.BLOCK_INSTALLER_OWNED_PRISTINE
    assert result.provenance_path is None
    assert result.archived_previous_to is None
    # Canonical root byte-identical, and the source was NOT archived away.
    assert _snapshot(shared) == before
    assert (pkg / "SKILL.md").is_file()


def test_execute_promotion_refuses_an_adopt_over_a_reserved_slug(tmp_path):
    """The adopt path is the OTHER write path — it must be gated too, or the
    guard would only cover half the door."""
    _seed_bundled("gaming")
    shared = _shared()
    _write_package(shared, "gaming", body="# Canonical\n")
    source = _write_package(tmp_path / "authored", "gaming", body="# Diverged\n")
    before = _snapshot(shared)

    plan = classify_promotion("gaming", source)
    assert plan.action == "hold_divergent"

    result = execute_promotion(
        plan, source={"kind": "path", "path": str(source)}, adopt_divergent=True
    )
    assert result.action == "refused"
    assert result.reason_code == sp.BLOCK_INSTALLER_SLUG_RESERVED
    assert _snapshot(shared) == before


def test_dry_run_reports_the_refusal_not_a_promise_to_promote():
    """The policy gate runs BEFORE the dry-run short-circuit: a preview that
    said 'would promote' for something the real run refuses is a lie."""
    pkg = _seed_bundled("gaming")
    shared = _shared()
    before = _snapshot(shared)

    plan = classify_promotion("gaming", pkg)
    result = execute_promotion(
        plan, source={"kind": "profile", "profile": "default"}, dry_run=True
    )
    assert result.action == "refused"
    assert result.reason_code == sp.BLOCK_INSTALLER_OWNED_PRISTINE
    assert _snapshot(shared) == before


def test_an_unowned_profile_package_still_promotes_through_the_same_door():
    """The guard must not become a wall: an operator's own package promotes
    exactly as before, with provenance recorded."""
    pkg = _write_package(_profile_skills(), "my-own-skill")
    plan = classify_promotion("my-own-skill", pkg)
    result = execute_promotion(plan, source={"kind": "profile", "profile": "default"})

    assert result.action == "promoted"
    assert result.reason_code is None
    assert (get_shared_skills_dir() / "my-own-skill" / "SKILL.md").is_file()
    # S54 deleted ``promotion_provenance`` (a callerless read-back accessor).
    # What matters here is what the PRODUCTION WRITER recorded, so the record is
    # read off disk directly.
    import json

    record = get_shared_skills_dir() / ".provenance" / "my-own-skill.json"
    provenance = json.loads(record.read_text(encoding="utf-8"))
    assert provenance["source"]["kind"] == "profile"


# ── 5. CLI: --dry-run leaves the store byte-identical ──────────────────────


def _parser():
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    build_parser(subs)
    return parser


def test_cli_promote_dry_run_leaves_the_shared_root_byte_identical(capsys, tmp_path):
    """stage42 gate: ``_add_stage42_global_args(mutation=True)`` auto-registers
    ``--dry-run``; a verb that does not READ ``args.dry_run`` silently mutates on
    a preview. Assert the store is byte-identical AND the envelope says so."""
    source = _write_package(tmp_path / "authored", "my-own-skill")
    shared = _shared()
    before = _snapshot(shared)

    args = _parser().parse_args(
        [
            "harness", "skills", "promote", "my-own-skill",
            "--from-path", str(source), "--dry-run", "--json",
        ]
    )
    assert args.dry_run is True
    assert args.func(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["action"] == "dry_run"
    assert payload["classification"] == "promote_new"
    assert _snapshot(shared) == before
    assert not (shared / "my-own-skill").exists()

    # And the non-dry run through the SAME verb does write — proving the
    # byte-identity above is the dry-run flag doing its job, not a broken verb.
    args = _parser().parse_args(
        [
            "harness", "skills", "promote", "my-own-skill",
            "--from-path", str(source), "--json",
        ]
    )
    assert args.func(args) == 0
    assert (shared / "my-own-skill" / "SKILL.md").is_file()


def test_cli_promote_refuses_an_installer_owned_source_with_a_typed_code(capsys):
    _seed_bundled("gaming")
    shared = _shared()
    before = _snapshot(shared)

    args = _parser().parse_args(
        ["harness", "skills", "promote", "gaming", "--from-profile", "default", "--json"]
    )
    assert args.func(args) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "error"
    assert payload["error"]["safe_details"]["reason_code"] == sp.BLOCK_INSTALLER_OWNED_PRISTINE
    assert _snapshot(shared) == before


def test_cli_publishable_lists_every_tier_with_typed_reasons(capsys):
    _write_package(_shared(), "canonical-skill")
    _write_package(_profile_skills(), "mine")
    _seed_bundled("gaming")

    args = _parser().parse_args(["harness", "skills", "publishable", "--json"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["item_kind"] == "skill_publishability"
    by_slug = {row["skill"]: row for row in payload["items"]}
    assert by_slug["canonical-skill"]["publishable"] is True
    assert by_slug["mine"]["publishable"] is False
    assert by_slug["gaming"]["promotion_block_reason"] == sp.BLOCK_INSTALLER_OWNED_PRISTINE

    args = _parser().parse_args(
        ["harness", "skills", "publishable", "--unpublishable-only", "--json"]
    )
    assert args.func(args) == 0
    filtered = json.loads(capsys.readouterr().out)
    assert {row["skill"] for row in filtered["items"]} == {"mine", "gaming"}


# ── 6. inventory contract ──────────────────────────────────────────────────


def test_inventory_carries_publishability_on_shared_rows_and_a_full_sweep():
    from agent_runtime.skills_inventory import build_skills_inventory

    _write_package(_shared(), "canonical-skill")
    _write_package(_profile_skills(), "mine")
    _seed_bundled("gaming")

    payload = build_skills_inventory()

    shared_row = next(row for row in payload["skills"] if row["slug"] == "canonical-skill")
    assert shared_row["source_kind"] == "shared_core"
    assert shared_row["publishable"] is True
    assert shared_row["publishable_reason"] == sp.REASON_SHARED_ROOT

    sweep = {row["skill"]: row for row in payload["resolvable_skills"]}
    assert sweep["mine"]["publishable"] is False
    assert sweep["gaming"]["promotion_block_reason"] == sp.BLOCK_INSTALLER_OWNED_PRISTINE

    summary = payload["publishability_summary"]
    assert summary["total"] == len(payload["resolvable_skills"])
    assert summary["publishable"] == 1
    assert summary["unpublishable"] == 2
    assert summary["promotable"] == 1
    assert summary["blocked_by_reason"][sp.BLOCK_INSTALLER_OWNED_PRISTINE] == 1


def test_prompt_observability_publishability_agrees_with_the_inventory_vocabulary():
    """Both surfaces MUST use one vocabulary or the Skills sheet and the
    inventory would disagree about what 'publishable' means."""
    from agent_runtime.prompt_observability import _skill_publishability

    assert _skill_publishability("shared_core") == (True, sp.REASON_SHARED_ROOT)
    assert _skill_publishability("profile_local") == (False, sp.REASON_PROFILE_LOCAL_ONLY)
    assert _skill_publishability("external") == (False, sp.REASON_EXTERNAL_DIR_ONLY)
    # An unresolved skill is explicitly unresolved, never silently defaulted.
    assert _skill_publishability(None) == (False, "unresolved")


# ── 7. inbox rows carry the door's verdict ────────────────────────────────


def test_inbox_rows_do_not_advertise_a_promotion_the_door_would_refuse():
    from agent_runtime.skill_promotion import list_inbox_packages, realm_inbox_dir

    _seed_bundled("gaming")
    _write_package(realm_inbox_dir("realm_x"), "gaming", body="# From a realm\n")

    row = next(r for r in list_inbox_packages() if r["skill"] == "gaming")
    assert row["action"] == "promote_new"
    assert row["promotion_block_reason"] == sp.BLOCK_INSTALLER_SLUG_RESERVED
    assert row["promotion_block_detail"]
