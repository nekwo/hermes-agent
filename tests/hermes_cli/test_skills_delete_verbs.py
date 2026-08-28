"""S3 tests for ``hermes harness skills delete`` / ``restore`` and the additive
``tombstones`` array on ``realm skills show``.

The operator half of the realm skill-delete lane (canon:
``docs/agent-runtime-harness/01-system-architecture.md`` §Skills, third lane;
the plan file was retired at the 2026-08-28 canon fold). S1 owns the
ledger and its store chokepoints; S2 owns pull/publish enforcement; what is
asserted here is the VERBS: which realms a bare ``delete`` resolves (R-E), that
the local canonical package is archived rather than deleted (R-A), that the
inbox mirror stops offering the package, that refusals are typed and exit
non-zero, that ``--dry-run`` writes nothing, and that ``restore`` lifts an entry
without pretending to restore bytes (R-C).

Every case drives the REAL argparse tree and dispatches through ``args.func``,
never by poking a handler: a handler nothing routes to is a verb no operator can
run, and this program has been bitten by exactly that before (the precedent is
``test_agent_retire_verb.py``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent.skill_utils import _content_hash_cache_clear
from agent_runtime import paths
from agent_runtime.store import RealmStore
from hermes_constants import CANONICAL_SHARED_SKILL_IDS, get_shared_skills_dir


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed.

    These cases MOVE skill packages and write realm records. The shared skills
    root already follows the repo-wide ``HERMES_HOME`` sandbox
    (``get_shared_skills_dir`` resolves ``<root>/shared/skills``), so only the
    agent-runtime store needs its own pin — but a resolution regression here
    would archive the OPERATOR's skills, so the pin is asserted, not assumed.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    monkeypatch.delenv("HERMES_SHARED_SKILLS", raising=False)
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "write into a runtime root nobody in this repo controls."
    )
    shared = get_shared_skills_dir().resolve()
    assert tmp_path.resolve() in shared.parents, (
        f"get_shared_skills_dir() resolved to {shared}, OUTSIDE {tmp_path}: this "
        "test archives packages and must never reach a real shared root."
    )
    _content_hash_cache_clear()
    yield root
    _content_hash_cache_clear()


# ── driving the real tree ────────────────────────────────────────────────────


def _parse(argv: list[str]):
    from hermes_cli import harness

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers()
    harness.build_parser(subs)
    return parser.parse_args(argv)


def _run(capsys, argv: list[str]) -> tuple[int, dict]:
    args = _parse(argv)
    code = args.func(args)
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


# ── seeding ──────────────────────────────────────────────────────────────────


def _canonical(slug: str) -> Path:
    return get_shared_skills_dir().joinpath(*slug.split("/"))


def _seed_canonical(slug: str, *, body: str = "# Body\n") -> Path:
    pkg = _canonical(slug)
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_bytes(
        f"---\nname: {slug.split('/')[-1]}\n---\n{body}".encode("utf-8")
    )
    return pkg


def _seed_inbox(realm_id: str, slug: str) -> Path:
    from agent_runtime.skill_promotion import realm_inbox_dir

    pkg = realm_inbox_dir(realm_id).joinpath(*slug.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_bytes(b"---\nname: mirrored\n---\n# Mirror\n")
    return pkg


def _realm(name: str, *, mode: str = "all", selection: list[str] | None = None):
    realm = RealmStore().create(name=name)
    if mode != "all" or selection is not None:
        realm = RealmStore().set_skill_selection(
            realm.id, mode=mode, selection=selection or []
        )
    return realm


def _ledger(realm_id: str) -> list[str]:
    return [entry.slug for entry in RealmStore().get(realm_id).skill_tombstones]


# ── parser shape ─────────────────────────────────────────────────────────────


def test_delete_and_restore_are_reachable_through_the_real_parser():
    delete = _parse(
        ["harness", "skills", "delete", "doomed", "--realm", "r1", "--realm", "r2", "--json", "--dry-run"]
    )
    assert delete.func.__name__ == "_cmd_skills_delete"
    assert delete.skill == "doomed"
    # ``--realm`` is REPEATABLE on delete: R-E's narrowing is a set, not one id.
    assert delete.realms == ["r1", "r2"]
    assert delete.json is True and delete.dry_run is True

    restore = _parse(["harness", "skills", "restore", "doomed", "--realm", "r1", "--json"])
    assert restore.func.__name__ == "_cmd_skills_restore"
    assert restore.realm == "r1"
    assert restore.json is True


def test_delete_without_realm_parses_and_restore_without_realm_does_not():
    # The asymmetry is the design: a delete defaults to every publishing realm
    # (R-E), while a tombstone is per-realm truth so there is no all-realms
    # restore to default to.
    assert _parse(["harness", "skills", "delete", "doomed"]).realms is None
    with pytest.raises(SystemExit):
        _parse(["harness", "skills", "restore", "doomed"])


def test_restore_does_not_advertise_dry_run():
    # ``--dry-run`` is registered per-verb (``controls``); restore does not
    # implement one, and an accepted-but-ignored flag is a wrong answer believed.
    with pytest.raises(SystemExit):
        _parse(["harness", "skills", "restore", "doomed", "--realm", "r1", "--dry-run"])


# ── delete: the happy path ───────────────────────────────────────────────────


def test_delete_one_named_realm_archives_the_package_and_writes_the_ledger(capsys):
    realm = _realm("Named Realm")
    pkg = _seed_canonical("doomed")

    code, envelope = _run(
        capsys, ["harness", "skills", "delete", "doomed", "--realm", realm.id, "--json"]
    )

    assert code == 0
    assert envelope["schema_version"] == 1
    assert envelope["kind"] == "skill_delete"
    assert envelope["skill"] == "doomed"
    assert envelope["realms"] == [
        {
            "realm_id": realm.id,
            "tombstoned": True,
            "selection_pruned": [],
            "refreshed": False,
            "inbox_pruned": [],
        }
    ]
    assert envelope["next"] == (
        f"hermes harness realm sync publish {realm.id} to propagate"
    )
    # R-A: archived, never deleted — and the receipt names where.
    assert not pkg.exists()
    assert envelope["archived_to"].startswith(".archive/")
    archived = get_shared_skills_dir() / envelope["archived_to"]
    assert (archived / "SKILL.md").is_file()
    assert envelope["archived"] == [
        {
            "slug": "doomed",
            "archived_to": envelope["archived_to"],
            "deleted_hash": envelope["deleted_hash"],
        }
    ]
    assert envelope["deleted_hash"]
    assert _ledger(realm.id) == ["doomed"]
    # Root observability: a delete resolved against the WRONG shared root finds
    # nothing and reports a well-formed "already gone". The envelope says which
    # root answered (the gate in test_harness_json_root_observability.py).
    assert envelope["resolution"]


def test_delete_records_the_content_hash_of_what_it_deleted(capsys):
    from agent.skill_utils import skill_package_content_hash

    realm = _realm("Hash Realm")
    pkg = _seed_canonical("doomed")
    expected = skill_package_content_hash(pkg, pkg / "SKILL.md")

    _code, envelope = _run(
        capsys, ["harness", "skills", "delete", "doomed", "--realm", realm.id, "--json"]
    )

    # ``deleted_hash`` is evidence for the restore receipt ("the thing you are
    # restoring is/isn't the bytes you deleted"), so it must be the hash of the
    # package as it stood, taken BEFORE the archive move.
    assert envelope["deleted_hash"] == expected
    assert RealmStore().get(realm.id).skill_tombstones[0].deleted_hash == expected


def test_delete_with_no_realm_flag_hits_every_publishing_realm(capsys):
    # R-E: one canonical root serves all realms, so a copy deleted locally is
    # deleted for every realm that published it — leaving one un-tombstoned
    # resurrects the local copy on that realm's next pull.
    publishes_all = _realm("Mode All")
    selects_it = _realm("Selected With", mode="selected", selection=["doomed"])
    selects_other = _realm("Selected Without", mode="selected", selection=["kept"])
    _seed_canonical("doomed")
    _seed_canonical("kept")

    code, envelope = _run(capsys, ["harness", "skills", "delete", "doomed", "--json"])

    assert code == 0
    assert sorted(row["realm_id"] for row in envelope["realms"]) == sorted(
        [publishes_all.id, selects_it.id]
    )
    # The realm that never published it is untouched — and so is `kept`.
    assert _ledger(selects_other.id) == []
    assert (_canonical("kept") / "SKILL.md").is_file()
    # Many targets: the placeholder is the honest ``next``, since the publish
    # has to be run once per realm the receipt lists.
    assert envelope["next"] == "hermes harness realm sync publish <realm> to propagate"


def test_delete_prunes_the_selection_and_the_inbox_mirror(capsys):
    realm = _realm("Pruned", mode="selected", selection=["doomed", "kept"])
    _seed_canonical("doomed")
    _seed_canonical("kept")
    mirrored = _seed_inbox(realm.id, "doomed")
    innocent = _seed_inbox(realm.id, "kept")

    _code, envelope = _run(capsys, ["harness", "skills", "delete", "doomed", "--json"])

    row = envelope["realms"][0]
    # R-F: a selection naming a tombstoned slug is a standing contradiction.
    assert row["selection_pruned"] == ["doomed"]
    assert RealmStore().get(realm.id).skill_selection == ["kept"]
    # The mirror stops offering the package as promotable; its neighbour stays.
    assert row["inbox_pruned"] == ["doomed"]
    assert not mirrored.exists()
    assert (innocent / "SKILL.md").is_file()


def test_a_bare_name_delete_covers_the_categorized_package_it_names(capsys):
    # The ONE match rule: a bare entry blocks ``<cat>/<child>`` of the same child
    # name, so the archive half must cover exactly what the publish filter does.
    realm = _realm("Categorized")
    pkg = _seed_canonical("software-development/hermes-agent")
    sibling = _seed_canonical("software-development/other-skill")

    _code, envelope = _run(
        capsys, ["harness", "skills", "delete", "hermes-agent", "--json"]
    )

    assert [row["slug"] for row in envelope["archived"]] == [
        "software-development/hermes-agent"
    ]
    assert not pkg.exists()
    assert (sibling / "SKILL.md").is_file()
    assert _ledger(realm.id) == ["hermes-agent"]


def test_repeating_a_delete_refreshes_rather_than_appending(capsys):
    realm = _realm("Repeat")
    _seed_canonical("doomed")
    _run(capsys, ["harness", "skills", "delete", "doomed", "--realm", realm.id, "--json"])

    _code, envelope = _run(
        capsys, ["harness", "skills", "delete", "doomed", "--realm", realm.id, "--json"]
    )

    assert envelope["realms"][0]["refreshed"] is True
    assert _ledger(realm.id) == ["doomed"]
    # Second pass: the package was already archived, so there is nothing left to
    # archive and the receipt says so instead of inventing a path.
    assert envelope["archived"] == []
    assert envelope["archived_to"] is None


# ── delete: refusals and warnings ────────────────────────────────────────────


def test_delete_refuses_an_installer_owned_slug_before_writing_anything(capsys):
    # R-B: every pull reinstalls these from repo source, so a realm tombstone
    # would lose that argument on every pull. Read from the CONSTANT — the set
    # is four ids today and has been three sizes this month.
    slug = sorted(CANONICAL_SHARED_SKILL_IDS)[0]
    realm = _realm("Installer Owned")
    pkg = _seed_canonical(slug)

    code, envelope = _run(capsys, ["harness", "skills", "delete", slug, "--json"])

    assert code == 2
    assert envelope["kind"] == "error"
    assert envelope["error"]["code"] == "skill_installer_owned"
    assert envelope["error"]["safe_details"] == {"skill": slug}
    assert "CANONICAL_SHARED_SKILL_IDS" in envelope["error"]["message"]
    # Refused BEFORE any mutation: ledger empty, package intact.
    assert _ledger(realm.id) == []
    assert (pkg / "SKILL.md").is_file()


@pytest.mark.parametrize("slug", ["../escape", "a/b/c", ".hidden", "with:colon"])
def test_delete_refuses_an_invalid_slug(capsys, slug):
    realm = _realm("Invalid Slug")

    code, envelope = _run(capsys, ["harness", "skills", "delete", slug, "--json"])

    assert code == 2
    assert envelope["error"]["code"] == "skill_slug_invalid"
    assert _ledger(realm.id) == []


def test_delete_of_an_unknown_slug_warns_inside_a_successful_envelope(capsys):
    # §3: ``skill_unknown`` is a WARNING, not a refusal — a tombstone records
    # intent, and intent is valid even when no copy exists here. With nothing to
    # resolve, nothing is written and the receipt says why.
    _realm("Nothing Published", mode="selected", selection=[])

    code, envelope = _run(capsys, ["harness", "skills", "delete", "ghost", "--json"])

    assert code == 0
    assert envelope["kind"] == "skill_delete"
    assert envelope["realms"] == []
    assert envelope["archived"] == []
    assert [warning["code"] for warning in envelope["warnings"]] == ["skill_unknown"]


def test_naming_a_realm_records_intent_even_with_no_local_package(capsys):
    # The other half of the same ruling: the operator named the realm, so the
    # tombstone IS written — with a warning that nothing was archived, because
    # this machine never had the bytes.
    realm = _realm("Intent Only")

    code, envelope = _run(
        capsys, ["harness", "skills", "delete", "ghost", "--realm", realm.id, "--json"]
    )

    assert code == 0
    assert envelope["realms"][0]["tombstoned"] is True
    assert envelope["deleted_hash"] is None
    assert [warning["code"] for warning in envelope["warnings"]] == [
        "skill_no_local_package"
    ]
    assert _ledger(realm.id) == ["ghost"]


def test_delete_of_an_unknown_realm_is_a_typed_not_found(capsys):
    code, envelope = _run(
        capsys, ["harness", "skills", "delete", "doomed", "--realm", "realm_nope", "--json"]
    )

    assert code == 3
    assert envelope["error"]["code"] == "not_found"


# ── delete: --dry-run ────────────────────────────────────────────────────────


def test_delete_dry_run_writes_nothing(capsys):
    realm = _realm("Dry Run", mode="selected", selection=["doomed"])
    pkg = _seed_canonical("doomed")
    mirrored = _seed_inbox(realm.id, "doomed")

    code, envelope = _run(
        capsys, ["harness", "skills", "delete", "doomed", "--json", "--dry-run"]
    )

    assert code == 0
    assert envelope["dry_run"] is True
    assert envelope["realms"][0]["realm_id"] == realm.id
    # Everything the real run would touch is still exactly as it was.
    assert (pkg / "SKILL.md").is_file()
    assert (mirrored / "SKILL.md").is_file()
    assert _ledger(realm.id) == []
    assert RealmStore().get(realm.id).skill_selection == ["doomed"]
    # The preview still reports the hash and the package it WOULD archive; the
    # path is null because no move happened.
    assert envelope["archived"] == [
        {"slug": "doomed", "archived_to": None, "deleted_hash": envelope["deleted_hash"]}
    ]
    assert envelope["archived_to"] is None


# ── restore ──────────────────────────────────────────────────────────────────


def test_restore_lifts_a_present_entry_and_names_the_archived_bytes(capsys):
    realm = _realm("Restore Me")
    _seed_canonical("doomed")
    _code, deleted = _run(
        capsys, ["harness", "skills", "delete", "doomed", "--realm", realm.id, "--json"]
    )

    code, envelope = _run(
        capsys, ["harness", "skills", "restore", "doomed", "--realm", realm.id, "--json"]
    )

    assert code == 0
    assert envelope["kind"] == "skill_restore"
    assert envelope["restored"] is True
    assert envelope["realm_id"] == realm.id
    assert envelope["tombstones"] == []
    assert _ledger(realm.id) == []
    # R-C: the entry is lifted, the BYTES are the promotion lane's job — and the
    # hint makes that a command to run rather than a recovery hunt.
    hint = envelope["content_hint"]
    assert hint["archived"] is True
    assert hint["candidates"] == 1
    assert hint["path"] == f"shared/skills/{deleted['archived_to']}"
    assert hint["promote_command"] == (
        f"hermes harness skills promote doomed --from-path {hint['path']}"
    )
    assert "warnings" not in envelope
    assert envelope["resolution"]


def test_restore_of_an_absent_entry_is_an_idempotent_no_op(capsys):
    realm = _realm("Nothing To Lift")

    code, envelope = _run(
        capsys, ["harness", "skills", "restore", "ghost", "--realm", realm.id, "--json"]
    )

    assert code == 0
    # The store returns the realm, not a flag, so ``restored`` is taken from the
    # ledger BEFORE the write — a no-op must not claim a change it did not make.
    assert envelope["restored"] is False
    assert [warning["code"] for warning in envelope["warnings"]] == [
        "skill_not_tombstoned"
    ]
    assert envelope["content_hint"] == {
        "archived": False,
        "path": None,
        "candidates": 0,
        "promote_command": None,
    }


def test_restore_says_so_when_another_entry_still_covers_the_slug(capsys):
    # Lifting ``cat/child`` leaves a bare ``child`` entry standing, and that
    # entry still blocks the package. A receipt saying only "restored" would be
    # a lie of omission.
    realm = _realm("Still Blocked")
    RealmStore().tombstone_skill(realm.id, "hermes-agent")
    RealmStore().tombstone_skill(realm.id, "software-development/hermes-agent")

    _code, envelope = _run(
        capsys,
        [
            "harness",
            "skills",
            "restore",
            "software-development/hermes-agent",
            "--realm",
            realm.id,
            "--json",
        ],
    )

    assert envelope["restored"] is True
    warning = envelope["warnings"][0]
    assert warning["code"] == "skill_still_tombstoned"
    assert warning["blocking_slug"] == "hermes-agent"
    assert [row["slug"] for row in envelope["tombstones"]] == ["hermes-agent"]


def test_restore_does_not_re_add_the_selection_entry(capsys):
    # R-F's second half: selection is a separate, deliberate act
    # (``realm skills set``), so a restore must not quietly re-publish.
    realm = _realm("Selection Stays Pruned", mode="selected", selection=["doomed"])
    _seed_canonical("doomed")
    _run(capsys, ["harness", "skills", "delete", "doomed", "--json"])

    _run(capsys, ["harness", "skills", "restore", "doomed", "--realm", realm.id, "--json"])

    assert RealmStore().get(realm.id).skill_selection == []


def test_restore_of_an_unknown_realm_is_a_typed_not_found(capsys):
    code, envelope = _run(
        capsys, ["harness", "skills", "restore", "doomed", "--realm", "realm_nope", "--json"]
    )

    assert code == 3
    assert envelope["error"]["code"] == "not_found"


# ── realm skills show ────────────────────────────────────────────────────────


def test_realm_skills_show_carries_the_tombstones_array(capsys):
    realm = _realm("Shown")
    _seed_canonical("doomed")
    _code, deleted = _run(
        capsys, ["harness", "skills", "delete", "doomed", "--realm", realm.id, "--json"]
    )

    code, envelope = _run(
        capsys, ["harness", "realm", "skills", "show", realm.id, "--json"]
    )

    assert code == 0
    assert envelope["kind"] == "realm_skill_selection"
    assert [row["slug"] for row in envelope["tombstones"]] == ["doomed"]
    row = envelope["tombstones"][0]
    # The same row shape the sync status envelope and the sidecar carry.
    assert set(row) == {"slug", "deleted_at", "deleted_hash"}
    assert row["deleted_hash"] == deleted["deleted_hash"]


def test_realm_skills_show_reports_an_empty_array_when_nothing_is_deleted(capsys):
    realm = _realm("Untouched")

    _code, envelope = _run(
        capsys, ["harness", "realm", "skills", "show", realm.id, "--json"]
    )

    # Absent-tolerant consumers exist, but the key is always present so a
    # launcher sheet can render "nothing deleted" without guessing.
    assert envelope["tombstones"] == []
