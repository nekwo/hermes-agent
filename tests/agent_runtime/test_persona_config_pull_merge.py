"""Portable persona-config projection: pull side.

The generic realm-sync write loop used to overwrite ``<profile_home>/config.yaml``
wholesale with the publisher's raw file — no merge, no hold, no accounting, the
last blind last-write-wins lane after boards/office got a 3-way baseline merge
and skills got the guarded inbox door.

These tests pin the replacement decision table (SHARED
``sync_merge.classify_three_way_pull`` — no second merge engine), the key-wise
write that leaves the member's machine sections alone, and the bidirectional
version tolerance: an OLD publisher's raw ``profiles/<name>/config.yaml`` is
projected on ingest instead of being written verbatim.

Sabotage guards for the merge tests are called out inline: breaking the
classifier wiring, the hold branch, or the key-wise write must turn a named
test red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from agent_runtime.persona_config_sync import (
    PROJECTION_KIND,
    PROJECTION_RELATIVE_PATH,
    apply_persona_config_pull,
    persona_def_hash,
    read_persona_config_baseline,
    read_remote_persona_defs,
    write_persona_config_baseline,
)
from agent_runtime.realm_sync import pull_realm_sync
from agent_runtime.store import RealmStore, WorkspaceStore
from hermes_constants import get_config_path

REALM = "realm-under-test"


# ── helpers ────────────────────────────────────────────────────────────────


def _member_config(personas: dict | None = None, **extra) -> Path:
    """The member's own config: machine sections plus whatever personas they
    have. These machine sections are the thing a pull must never touch."""

    data = {
        "model": {"default": "member-model", "provider": "member-provider"},
        "mcp_servers": {
            "launcher_qa": {
                "command": "D:\\Member\\build\\stagec_qa_mcp_server.exe",
                "env": {"STAGEC_QA_REPO_ROOT": "D:\\Member\\Launcher"},
            }
        },
        "display": {"skin": "mono"},
        "agent_runtime": {"redaction_mode": "observe", "personas": personas or {}},
        **extra,
    }
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return path


def _read_member_config() -> dict:
    return yaml.safe_load(get_config_path().read_text(encoding="utf-8")) or {}


def _member_personas() -> dict:
    return _read_member_config().get("agent_runtime", {}).get("personas", {})


def _remote_projection(tmp_path: Path, personas: dict) -> Path:
    """A NEW publisher's realm subtree carrying ``store/personas.yaml``."""

    subtree = tmp_path / "subtree"
    target = subtree.joinpath(*PROJECTION_RELATIVE_PATH.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            {"kind": PROJECTION_KIND, "schema_version": 1, "personas": personas},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return subtree


def _legacy_subtree(tmp_path: Path, profile: str, config: dict) -> Path:
    """An OLD publisher's realm subtree: raw ``profiles/<name>/config.yaml``."""

    subtree = tmp_path / "legacy-subtree"
    target = subtree / "profiles" / profile / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    return subtree


DEV_V1 = {"display_name": "Dev", "hermes_profile": "launcher-dev", "iteration_budget": 12}
DEV_V2 = {"display_name": "Dev (realm)", "hermes_profile": "launcher-dev", "iteration_budget": 20}
DEV_LOCAL_EDIT = {"display_name": "Dev (mine)", "hermes_profile": "launcher-dev", "iteration_budget": 12}


# ── decision table ─────────────────────────────────────────────────────────


def test_adopts_a_definition_the_member_does_not_have(tmp_path):
    _member_config()
    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V1}))

    assert summary.adopted == ["dev"]
    assert summary.held == []
    assert summary.changed is True
    assert _member_personas()["dev"] == DEV_V1
    assert read_persona_config_baseline(REALM)["dev"] == persona_def_hash(DEV_V1)


def test_converges_when_local_already_matches_remote(tmp_path):
    _member_config({"dev": dict(DEV_V1)})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})

    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V1}))

    assert summary.converged == ["dev"]
    assert summary.adopted == []
    assert summary.changed is False


def test_converges_without_rewriting_when_both_sides_moved_to_the_same_content(tmp_path):
    """Both sides changed vs the baseline but landed on identical content: only
    the baseline catches up. Rewriting an identical definition would churn the
    member's config for nothing."""

    _member_config({"dev": dict(DEV_V2)})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})
    before = get_config_path().read_bytes()

    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V2}))

    assert summary.converged == ["dev"]
    assert summary.adopted == []
    assert get_config_path().read_bytes() == before
    assert read_persona_config_baseline(REALM)["dev"] == persona_def_hash(DEV_V2)


def test_takes_remote_when_only_the_realm_moved(tmp_path):
    _member_config({"dev": dict(DEV_V1)})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})

    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V2}))

    assert summary.adopted == ["dev"]
    assert _member_personas()["dev"]["iteration_budget"] == 20


def test_keeps_a_local_edit_the_realm_has_not_published(tmp_path):
    _member_config({"dev": dict(DEV_LOCAL_EDIT)})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})

    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V1}))

    assert summary.kept_local == ["dev"]
    assert summary.adopted == []
    assert _member_personas()["dev"] == DEV_LOCAL_EDIT


def test_holds_a_divergent_definition_instead_of_clobbering_it(tmp_path):
    """SABOTAGE GUARD: route CONFLICT to a write (or drop the baseline compare)
    and this must go red — a member's edited definition being silently replaced
    is the exact defect this lane exists to retire."""

    _member_config({"dev": dict(DEV_LOCAL_EDIT)})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})

    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V2}))

    assert summary.held == ["dev"]
    assert summary.adopted == []
    assert summary.changed is False
    assert _member_personas()["dev"] == DEV_LOCAL_EDIT


def test_retains_a_definition_the_realm_stopped_publishing(tmp_path):
    """Unlike a board card, a persona definition is referenced by Office
    placements, persona instances and running assignments — a sync never removes
    it from the member's config."""

    _member_config({"dev": dict(DEV_V1), "qa": {"display_name": "QA"}})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1), "qa": persona_def_hash({"display_name": "QA"})})

    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V1}))

    assert summary.retained == ["qa"]
    assert "qa" in _member_personas()
    assert "qa" not in read_persona_config_baseline(REALM)


# ── the member's own machine content is never touched ──────────────────────


def test_merge_leaves_every_machine_section_alone(tmp_path):
    """SABOTAGE GUARD: replace the key-wise write with a whole-file write and
    this goes red — it is the original defect, byte for byte."""

    _member_config({"dev": dict(DEV_V1)})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})
    before = _read_member_config()

    apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V2}))
    after = _read_member_config()

    assert after["mcp_servers"] == before["mcp_servers"]
    assert after["model"] == before["model"]
    assert after["display"] == before["display"]
    assert after["agent_runtime"]["redaction_mode"] == "observe"
    assert after["agent_runtime"]["personas"]["dev"]["display_name"] == "Dev (realm)"


def test_adopting_preserves_the_members_own_persona_keys(tmp_path):
    """The realm owns the SHARED surface; the member keeps their machine-shaped
    keys. Their ``repo_scope`` points at THEIR checkout and must survive."""

    local = {**DEV_V1, "repo_scope": "D:/Member/Launcher", "readiness": {"state": "ready"}}
    _member_config({"dev": local})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})

    apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V2}))
    dev = _member_personas()["dev"]

    assert dev["repo_scope"] == "D:/Member/Launcher"
    assert dev["readiness"] == {"state": "ready"}
    assert dev["display_name"] == "Dev (realm)"


# ── refusals: per-entity isolation, one bad definition never aborts a pull ──


def test_refuses_a_machine_shaped_incoming_definition_without_blocking_others(tmp_path):
    _member_config()
    subtree = _remote_projection(
        tmp_path,
        {
            "dev": DEV_V1,
            "bad": {"display_name": "Bad", "system_prompt_path": "X:\\prompts\\bad.md"},
        },
    )

    summary = apply_persona_config_pull(REALM, subtree)

    assert summary.adopted == ["dev"]
    assert [row["persona_id"] for row in summary.refused] == ["bad"]
    assert summary.refused[0]["code"] == "nonportable_path"
    assert "bad" not in _member_personas()


def test_refuses_a_secret_shaped_incoming_definition(tmp_path):
    _member_config()
    subtree = _remote_projection(
        tmp_path,
        {"leaky": {"display_name": "api_key: sk-abcdefghijklmnopqrstuvwxyz"}},
    )

    summary = apply_persona_config_pull(REALM, subtree)

    assert [row["code"] for row in summary.refused] == ["secret_shaped_value"]
    assert _member_personas() == {}


def test_refuses_a_hostile_persona_id_from_the_wire(tmp_path):
    _member_config()
    subtree = _remote_projection(tmp_path, {"../../evil": {"display_name": "x"}, "dev": DEV_V1})

    summary = apply_persona_config_pull(REALM, subtree)

    assert summary.adopted == ["dev"]
    assert [row["code"] for row in summary.refused] == ["invalid_persona_id"]


def test_incoming_definitions_are_reprojected_not_trusted(tmp_path):
    """A publisher is not trusted to have filtered correctly: a non-allowlisted
    key in the wire document is dropped at the door and accounted."""

    _member_config()
    subtree = _remote_projection(
        tmp_path, {"dev": {**DEV_V1, "repo_scope": "Z:/publisher/checkout", "mystery": 1}}
    )

    summary = apply_persona_config_pull(REALM, subtree)

    assert summary.adopted == ["dev"]
    assert _member_personas()["dev"] == DEV_V1
    assert "personas.dev.repo_scope" in summary.dropped_keys
    assert "personas.dev.mystery" in summary.dropped_keys


# ── version tolerance ──────────────────────────────────────────────────────


def test_legacy_raw_publisher_config_is_projected_never_written_wholesale(tmp_path):
    """NEW hermes ← OLD publisher. The old artifact is
    ``profiles/<name>/config.yaml`` carrying the publisher's whole machine. Its
    persona definitions still travel; nothing else does, and the member's config
    is never replaced."""

    _member_config()
    subtree = _legacy_subtree(
        tmp_path,
        "base",
        {
            "model": {"default": "publisher-model"},
            "mcp_servers": {"launcher_qa": {"command": "X:\\Publisher\\qa.exe"}},
            "agent_runtime": {
                "redaction_mode": "enforce",
                "personas": {"dev": {**DEV_V1, "repo_scope": "X:/Publisher/Launcher"}},
            },
        },
    )

    summary = apply_persona_config_pull(REALM, subtree)
    after = _read_member_config()

    assert summary.source == "legacy_config"
    assert summary.adopted == ["dev"]
    assert after["agent_runtime"]["personas"]["dev"] == DEV_V1
    # The publisher's machine/account content never lands.
    assert after["mcp_servers"]["launcher_qa"]["command"].startswith("D:")
    assert after["model"]["default"] == "member-model"
    assert after["agent_runtime"]["redaction_mode"] == "observe"


def test_new_projection_wins_over_a_legacy_config_in_the_same_subtree(tmp_path):
    _member_config()
    subtree = _remote_projection(tmp_path, {"dev": DEV_V2})
    legacy = subtree / "profiles" / "base" / "config.yaml"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        yaml.safe_dump({"agent_runtime": {"personas": {"dev": DEV_V1}}}, sort_keys=True), encoding="utf-8"
    )

    defs, _dropped, source = read_remote_persona_defs(subtree)

    assert source == "projection"
    assert defs["dev"] == DEV_V2


def test_a_realm_carrying_no_persona_definitions_touches_nothing(tmp_path):
    """Absence is 'not published', never 'removed' — an older or unrelated realm
    must not retire a member's definitions or their baseline."""

    _member_config({"dev": dict(DEV_V1)})
    write_persona_config_baseline(REALM, {"dev": persona_def_hash(DEV_V1)})
    empty = tmp_path / "empty-subtree"
    empty.mkdir()

    summary = apply_persona_config_pull(REALM, empty)

    assert summary.source is None
    assert summary.as_dict()["adopted"] == []
    assert summary.retained == []
    assert read_persona_config_baseline(REALM) == {"dev": persona_def_hash(DEV_V1)}
    assert _member_personas()["dev"] == DEV_V1


def test_dry_run_writes_nothing(tmp_path):
    _member_config()
    before = get_config_path().read_bytes()

    summary = apply_persona_config_pull(REALM, _remote_projection(tmp_path, {"dev": DEV_V1}), dry_run=True)

    assert summary.adopted == ["dev"]
    assert get_config_path().read_bytes() == before
    assert read_persona_config_baseline(REALM) == {}


# ── wired into the real pull lane ──────────────────────────────────────────


def _realm_with_repo(tmp_path: Path, name: str = "Merge Realm"):
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


def test_pull_realm_sync_merges_persona_definitions_and_reports_them(tmp_path):
    _member_config()
    realm, repo = _realm_with_repo(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])
    subtree = repo / "realms" / realm.id
    target = subtree.joinpath(*PROJECTION_RELATIVE_PATH.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            {"kind": PROJECTION_KIND, "schema_version": 1, "personas": {"dev": DEV_V2}}, sort_keys=True
        ),
        encoding="utf-8",
    )

    result = pull_realm_sync(realm.id)

    assert result["profile_sync"]["personas"]["adopted"] == ["dev"]
    assert result["profile_sync"]["personas"]["source"] == "projection"
    assert result["changed"] is True
    assert _member_personas()["dev"] == DEV_V2


def test_pull_realm_sync_never_writes_a_raw_legacy_config_over_the_member(tmp_path):
    """SABOTAGE GUARD: restore ``profiles/<name>/config.yaml`` as a generic pull
    destination and this goes red — it is the base-seed clobber."""

    from agent_runtime.profile_context import active_profile_name

    _member_config()
    realm, repo = _realm_with_repo(tmp_path)
    subtree = repo / "realms" / realm.id
    # Published under the ACTIVE profile name on purpose: that is the token the
    # old mapper resolved to ``get_config_path()`` — the member's live config.
    legacy = subtree / "profiles" / str(active_profile_name() or "default") / "config.yaml"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {"launcher_qa": {"command": "X:\\Publisher\\qa.exe"}},
                "agent_runtime": {"personas": {"dev": DEV_V1}},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    pull_realm_sync(realm.id)
    after = _read_member_config()

    assert after["mcp_servers"]["launcher_qa"]["command"].startswith("D:")
    assert after["display"] == {"skin": "mono"}
    assert after["agent_runtime"]["personas"]["dev"] == DEV_V1
