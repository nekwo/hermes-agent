"""W-H4 (office plan §5.1): profile-aware realm-sync pull destinations.

Before 2026-07-17 `_destination_for_sync_path` collapsed every
``profiles/<name>/…`` artifact into the ACTIVE profile home — a degenerate
ternary whose two branches both returned ``get_hermes_home()`` — so a
multi-profile realm pull silently last-write-wins'd every profile's
config.yaml / MEMORY.md onto one home. Invisible to the B1 drill because both
ends were single-profile; these are the first tests that can see it. The
cross-profile clobber test is the sabotage guard: restoring the degenerate
mapping turns it red.
"""

from __future__ import annotations

from pathlib import Path

import agent_runtime.realm_sync as realm_sync
from agent_runtime.realm_sync import (
    RealmSyncArtifact,
    _destination_for_sync_path,
    _profile_home_for_token,
    _pulled_profile_tokens,
)


def _pin_profiles(monkeypatch, tmp_path, *, active: str = "alice") -> Path:
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(realm_sync, "active_profile_name", lambda: active)
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda name: profiles_root / profiles_mod.normalize_profile_name(name))
    return profiles_root


def test_active_profile_maps_to_active_home(monkeypatch, tmp_path):
    _pin_profiles(monkeypatch, tmp_path, active="alice")
    from hermes_constants import get_hermes_home

    dest = _destination_for_sync_path("profiles/alice/config.yaml")
    assert dest == get_hermes_home() / "config.yaml"


def test_named_profiles_map_to_their_own_homes_not_the_active_one(monkeypatch, tmp_path):
    """THE cross-profile clobber regression (plan §5.1). With the degenerate
    mapping, alice's and bob's config.yaml resolved to the SAME file."""

    profiles_root = _pin_profiles(monkeypatch, tmp_path, active="alice")
    bob_config = _destination_for_sync_path("profiles/bob/config.yaml")
    neko_config = _destination_for_sync_path("profiles/neko/config.yaml")
    alice_config = _destination_for_sync_path("profiles/alice/config.yaml")
    assert bob_config == profiles_root / "bob" / "config.yaml"
    assert neko_config == profiles_root / "neko" / "config.yaml"
    assert len({str(alice_config), str(bob_config), str(neko_config)}) == 3, (
        "profiles/<name> pull destinations collapsed — the W-H4 cross-profile "
        "clobber is back"
    )


def test_named_profile_memory_and_prompts_stay_profile_scoped(monkeypatch, tmp_path):
    profiles_root = _pin_profiles(monkeypatch, tmp_path, active="alice")
    memory = _destination_for_sync_path("profiles/bob/personas/dev/memories/MEMORY.md")
    prompt = _destination_for_sync_path("profiles/bob/personas/dev/system_prompt/dev.md")
    context = _destination_for_sync_path("profiles/bob/personas/dev/context/AGENTS.md")
    assert memory == profiles_root / "bob" / "memories" / "MEMORY.md"
    assert prompt == profiles_root / "bob" / "personas" / "dev.md"
    assert context == profiles_root / "bob" / "AGENTS.md"


def test_hostile_profile_tokens_are_refused(monkeypatch, tmp_path):
    _pin_profiles(monkeypatch, tmp_path, active="alice")
    for token in ("..", ".", "", "c:evil", "/abs", "\\\\share", "a/b"):
        assert _profile_home_for_token(token) is None, token
    assert _destination_for_sync_path("profiles/../config.yaml") is None


def test_pulled_profile_tokens_report_created_profiles(monkeypatch, tmp_path):
    _pin_profiles(monkeypatch, tmp_path, active="alice")
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: name == "bob")
    artifacts = [
        RealmSyncArtifact(kind="persona_config", source=tmp_path / "a", relative_path="profiles/alice/config.yaml", destination=tmp_path / "a"),
        RealmSyncArtifact(kind="persona_config", source=tmp_path / "b", relative_path="profiles/bob/config.yaml", destination=tmp_path / "b"),
        RealmSyncArtifact(kind="persona_config", source=tmp_path / "c", relative_path="profiles/neko/config.yaml", destination=tmp_path / "c"),
        RealmSyncArtifact(kind="skill", source=tmp_path / "s", relative_path="skills/x/SKILL.md", destination=tmp_path / "s"),
    ]
    tokens, created = _pulled_profile_tokens(artifacts)
    assert tokens == ["alice", "bob", "neko"]
    # alice = active (never "created"); bob exists; neko is materialized by this pull.
    assert created == ["neko"]


def test_no_profile_artifacts_reports_nothing(tmp_path):
    artifacts = [
        RealmSyncArtifact(kind="skill", source=tmp_path / "s", relative_path="skills/x/SKILL.md", destination=tmp_path / "s"),
    ]
    assert _pulled_profile_tokens(artifacts) == ([], [])
