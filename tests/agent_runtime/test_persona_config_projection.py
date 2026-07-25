"""Portable persona-config projection: publish side.

Retires the last blind last-write-wins lane in realm sync. Before 2026-07-25
``_persona_artifacts`` published the bound profile's RAW ``config.yaml`` as
``profiles/<profile>/config.yaml`` and the generic pull loop overwrote the
member's file wholesale. Two proven defects:

1. a persona bound to ``hermes_profile: base`` published ``profiles/base/…`` and
   a member's pull overwrote THEIR fork seed (Office layout realm-sync plan §5.1
   ruled the base seed must never sync; the old guard keyed on the base PERSONA
   id, so a persona merely bound to the base profile walked past it);
2. the published bytes carried machine-shaped ``mcp_servers`` commands/env and
   absolute Windows paths that resolve to nothing on another member's machine.

These tests pin the replacement: a synthesized, allowlisted, pruned,
deterministic projection plus the publish-time portability + base-seed guards.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from agent_runtime.persona_config_sync import (
    PERSONA_DEF_ALLOWED_KEYS,
    PROJECTION_KIND,
    PROJECTION_RELATIVE_PATH,
    find_nonportable_values,
    nonportable_reason,
    project_persona_definitions,
)
from agent_runtime.realm_sync import (
    RealmSyncArtifact,
    RealmSyncError,
    _assert_no_raw_profile_config,
    _assert_portable_artifacts,
    publish_realm_sync,
    resolve_realm_sync_artifacts,
)
from agent_runtime.store import RealmStore, WorkspaceStore
from hermes_constants import get_config_path


# ── fixtures ───────────────────────────────────────────────────────────────


def _local_realm(tmp_path: Path, name: str = "Projection Realm"):
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


def _realm_with_remote(tmp_path: Path, name: str = "Projection Realm"):
    """A realm whose sync repo tracks a local bare upstream, so a real (non
    dry-run) publish exercises commit + push (``test_realm_sync`` pattern)."""

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, text=True)
    realm, repo = _local_realm(tmp_path, name=name)
    for args in (
        ("config", "user.email", "realm-sync-test@localhost"),
        ("config", "user.name", "Realm Sync Test"),
        ("commit", "--allow-empty", "-m", "init"),
        ("remote", "add", "origin", str(bare)),
        ("push", "-u", "origin", "HEAD"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return realm, repo


#: The shape of the operator's real ``config.yaml``: portable persona
#: definitions sitting next to machine-shaped installation wiring.
_MACHINE_CONFIG = {
    "model": {"default": "gpt-5.6-luna", "provider": "openai-codex"},
    "mcp_servers": {
        "launcher_qa": {
            "command": "X:\\Unreal Engine\\Engine\\Launcher\\build\\stagec_qa_mcp_server.exe",
            "args": [],
            "env": {
                "STAGEC_QA_REPO_ROOT": "X:\\Unreal Engine\\Engine\\Launcher",
                "STAGEC_SCREENSHOT_HELPER": "X:\\Unreal Engine\\scripts\\Capture.ps1",
            },
        }
    },
    "agent_runtime": {
        "redaction_mode": "observe",
        "personas": {
            "dev": {
                "display_name": "Launcher Dev Agent",
                "autonomy": "autonomous",
                "hermes_profile": "launcher-dev",
                "include_profile_memory": True,
                "repo_scope": "X:/Unreal Engine/Engine/Launcher/EterniaLauncher",
                "repo_scope_label": "EterniaLauncher",
                "iteration_budget": 12,
                "skills": ["writing-plans"],
                "readiness": {"checked_at": "2026-07-24"},
            },
            "neko_supervisor": {
                "hermes_profile": "base",
                "display_name": "Neko Mission Lead",
                "soul_overlay_path": "SOUL.md",
                "toolsets": ["file", "terminal"],
            },
            "unwanted": {"display_name": "Not published by this realm"},
        },
    },
}


def _write_config(data: dict) -> Path:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    return path


# ── projection: allowlist + pruning ────────────────────────────────────────


def test_projection_excludes_machine_sections_entirely():
    projection = project_persona_definitions(["dev"], raw_config=_MACHINE_CONFIG)
    document = projection.document()

    assert set(document) == {"kind", "personas", "schema_version"}
    assert document["kind"] == PROJECTION_KIND
    # No machine/installation/account-shaped section survives the projection.
    text = projection.to_bytes().decode("utf-8")
    for leaked in ("mcp_servers", "STAGEC", "launcher_qa", "redaction_mode", "gpt-5.6-luna"):
        assert leaked not in text, leaked


def test_projection_keeps_portable_persona_keys_and_drops_machine_ones():
    projection = project_persona_definitions(["dev"], raw_config=_MACHINE_CONFIG)
    dev = projection.personas["dev"]

    assert dev["display_name"] == "Launcher Dev Agent"
    assert dev["hermes_profile"] == "launcher-dev"
    assert dev["repo_scope_label"] == "EterniaLauncher"
    assert dev["iteration_budget"] == 12
    assert dev["skills"] == ["writing-plans"]
    # repo_scope is an absolute checkout path that exists only on this machine;
    # readiness is derived runtime state. Both are dropped — and ACCOUNTED.
    assert "repo_scope" not in dev
    assert "readiness" not in dev
    assert "personas.dev.repo_scope" in projection.dropped_keys
    assert "personas.dev.readiness" in projection.dropped_keys


def test_projection_is_pruned_to_the_published_persona_set():
    projection = project_persona_definitions(["dev", "neko_supervisor"], raw_config=_MACHINE_CONFIG)

    assert sorted(projection.personas) == ["dev", "neko_supervisor"]
    assert "unwanted" not in projection.to_bytes().decode("utf-8")


def test_projection_allowlist_is_opt_in_for_unknown_keys():
    config = {"agent_runtime": {"personas": {"dev": {"display_name": "Dev", "future_key": "x"}}}}
    projection = project_persona_definitions(["dev"], raw_config=config)

    assert projection.personas["dev"] == {"display_name": "Dev"}
    assert "personas.dev.future_key" in projection.dropped_keys
    assert "future_key" not in PERSONA_DEF_ALLOWED_KEYS


def test_projection_is_deterministic_so_republish_is_a_noop():
    shuffled = {
        "agent_runtime": {
            "personas": {
                "neko_supervisor": dict(reversed(list(_MACHINE_CONFIG["agent_runtime"]["personas"]["neko_supervisor"].items()))),
                "dev": _MACHINE_CONFIG["agent_runtime"]["personas"]["dev"],
            }
        }
    }
    first = project_persona_definitions(["dev", "neko_supervisor"], raw_config=_MACHINE_CONFIG).to_bytes()
    second = project_persona_definitions(["neko_supervisor", "dev"], raw_config=shuffled).to_bytes()

    assert first == second
    assert b"\r" not in first


def test_projection_synthesizes_a_definition_for_a_store_only_persona():
    """A persona that exists in the store with no config override must still
    publish a definition — otherwise an Office placement references a persona no
    member can materialize (the reason we cannot simply stop publishing)."""

    class _Record:
        id = "storeonly"
        display_name = "Store Only"
        role = "dev"
        hermes_profile = "qa"
        repo_scope = "X:/only/here"
        readiness = {"state": "ready"}

    projection = project_persona_definitions(
        ["storeonly"], raw_config={"agent_runtime": {"personas": {}}}, records={"storeonly": _Record()}
    )

    assert projection.synthesized == ["storeonly"]
    assert projection.personas["storeonly"]["display_name"] == "Store Only"
    assert "repo_scope" not in projection.personas["storeonly"]


def test_projection_reports_wanted_personas_it_cannot_define():
    projection = project_persona_definitions(["ghost"], raw_config={"agent_runtime": {"personas": {}}})

    assert projection.personas == {}
    assert projection.missing == ["ghost"]


def test_projection_refuses_hostile_persona_ids():
    config = {"agent_runtime": {"personas": {"../evil": {"display_name": "x"}, "a.b": {"display_name": "y"}}}}
    projection = project_persona_definitions(["../evil", "a.b"], raw_config=config)

    assert projection.personas == {}
    assert "personas.../evil" in projection.dropped_keys
    assert "personas.a.b" in projection.dropped_keys


# ── portability validator ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,reason",
    [
        ("X:\\Unreal Engine\\repo", "drive_letter_path"),
        ("x:/unreal/repo", "drive_letter_path"),
        ("   X:\\padded\\path   ", "drive_letter_path"),
        ("run --root=C:/data/x", "drive_letter_path"),
        ("\\\\fileserver\\share\\bin", "unc_path"),
        ("/home/tony/repo", "posix_absolute_path"),
        ("  /opt/hermes/bin  ", "posix_absolute_path"),
    ],
)
def test_nonportable_values_are_detected_regardless_of_case_or_whitespace(value, reason):
    assert nonportable_reason(value) == reason


@pytest.mark.parametrize(
    "value",
    [
        "EterniaLauncher",
        "software-development/hermes-agent",
        "SOUL.md",
        "personas/dev.md",
        "https://example.invalid/a/b",
        "http://host/path/to/thing",
        "/help /clear",
        "Agent X: the second",
        "12:30",
        None,
        42,
        True,
    ],
)
def test_portable_values_are_not_flagged(value):
    """A refusal that bricks a publish over a display-name false positive is a
    known failure class in this file — the validator must not fire on prose,
    relative paths, URLs, or non-strings."""

    assert nonportable_reason(value) is None


def test_find_nonportable_values_names_every_offender_with_its_key():
    body = {
        "dev": {"system_prompt_path": "X:\\prompts\\dev.md", "display_name": "Dev"},
        "qa": {"skills": ["ok", "/srv/skills/qa"]},
    }
    offenders = find_nonportable_values(body, prefix="personas")

    assert [row["key"] for row in offenders] == [
        "personas.dev.system_prompt_path",
        "personas.qa.skills[1]",
    ]
    assert {row["reason"] for row in offenders} == {"drive_letter_path", "posix_absolute_path"}


def test_publish_refuses_a_nonportable_projection_naming_all_offenders(tmp_path):
    bad = yaml.safe_dump(
        {
            "kind": PROJECTION_KIND,
            "schema_version": 1,
            "personas": {
                "dev": {"system_prompt_path": "X:\\prompts\\dev.md"},
                "qa": {"soul_overlay_path": "/home/tony/SOUL.md"},
            },
        },
        sort_keys=True,
    ).encode("utf-8")
    artifact = RealmSyncArtifact(
        kind="persona_config",
        source=tmp_path / "config.yaml",
        relative_path=PROJECTION_RELATIVE_PATH,
        destination=tmp_path / "config.yaml",
        content=bad,
    )

    with pytest.raises(RealmSyncError) as exc:
        _assert_portable_artifacts([artifact])

    assert exc.value.code == "sync_nonportable_path"
    keys = {row["key"] for row in exc.value.safe_details["offenders"]}
    assert keys == {"personas.dev.system_prompt_path", "personas.qa.soul_overlay_path"}
    assert exc.value.safe_details["hint"]


def test_portability_gate_ignores_free_text_artifacts(tmp_path):
    """Scope is deliberate: a profile MEMORY.md / AGENTS.md / SKILL.md mentions
    absolute paths as PROSE. Refusing those would brick every publish on this
    machine without preventing a single piece of dead wiring."""

    memory = tmp_path / "MEMORY.md"
    memory.write_text("Live checkout at X:\\Eternia\\hermes-agent and /home/tony/x\n", encoding="utf-8")
    artifact = RealmSyncArtifact(
        kind="profile_memory",
        source=memory,
        relative_path="profiles/base/personas/dev/memories/MEMORY.md",
        destination=memory,
    )

    _assert_portable_artifacts([artifact])  # must not raise


# ── base-seed guard (Office plan §5.1) ─────────────────────────────────────


def test_raw_profile_config_can_never_be_an_artifact(tmp_path):
    """The §5.1 guard, made structural. The original guard keyed on the base
    PERSONA id, so a persona bound to ``hermes_profile: base`` published
    ``profiles/base/config.yaml`` anyway and a member's pull overwrote their
    fork seed."""

    artifact = RealmSyncArtifact(
        kind="persona_config",
        source=tmp_path / "config.yaml",
        relative_path="profiles/base/config.yaml",
        destination=tmp_path / "config.yaml",
    )

    with pytest.raises(RealmSyncError) as exc:
        _assert_no_raw_profile_config([artifact])

    assert exc.value.code == "sync_profile_config_excluded"
    assert exc.value.safe_details["paths"] == ["profiles/base/config.yaml"]
    assert exc.value.safe_details["base_profile"] == "base"


# ── end to end through the real publish lane ───────────────────────────────


def test_publish_ships_the_projection_and_never_a_raw_profile_config(tmp_path):
    _write_config(_MACHINE_CONFIG)
    realm, repo = _realm_with_remote(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])

    result = publish_realm_sync(realm.id)
    paths = {row["path"] for row in result["artifacts"]}

    assert PROJECTION_RELATIVE_PATH in paths
    assert not any(path.startswith("profiles/") and path.endswith("/config.yaml") for path in paths)

    published = (repo / "realms" / realm.id / Path(PROJECTION_RELATIVE_PATH)).read_text(encoding="utf-8")
    data = yaml.safe_load(published)
    assert data["kind"] == PROJECTION_KIND
    assert "dev" in data["personas"]
    for leaked in ("mcp_servers", "STAGEC", "X:\\", "X:/", "readiness"):
        assert leaked not in published, leaked


def test_publish_reports_base_seed_guard_and_dropped_keys(tmp_path):
    _write_config(_MACHINE_CONFIG)
    realm, _repo = _local_realm(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["neko_supervisor"])

    row = publish_realm_sync(realm.id, dry_run=True)["persona_projection"]

    # neko_supervisor binds hermes_profile: base — the profile whose RAW seed
    # settings §5.1 says must never travel. Reported, not silently skipped.
    assert row["base_seed_guarded"] is True
    assert "base" in row["profiles_withheld"]
    assert "neko_supervisor" in row["personas"]


def test_republishing_an_unchanged_projection_is_a_noop(tmp_path):
    _write_config(_MACHINE_CONFIG)
    realm, _repo = _realm_with_remote(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])

    assert publish_realm_sync(realm.id)["changed"] is True
    assert publish_realm_sync(realm.id)["changed"] is False


def test_resolved_artifacts_carry_synthesized_content_not_the_raw_file(tmp_path):
    _write_config(_MACHINE_CONFIG)
    realm, _repo = _local_realm(tmp_path)
    WorkspaceStore().create(name="Launcher", realm_id=realm.id, agent_ids=["dev"])

    artifacts = resolve_realm_sync_artifacts(realm.id)
    projection = [a for a in artifacts if a.relative_path == PROJECTION_RELATIVE_PATH]

    assert len(projection) == 1
    assert projection[0].content is not None
    assert projection[0].read_bytes() == projection[0].content
    assert b"mcp_servers" not in projection[0].read_bytes()
