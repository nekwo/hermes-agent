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


def test_profile_config_is_never_a_generic_pull_destination(monkeypatch, tmp_path):
    """SUPERSEDED W-H4 detail (2026-07-25): a profile ``config.yaml`` no longer
    has a generic pull destination AT ALL.

    W-H4 made the mapping profile-aware so a multi-profile pull stopped
    collapsing every config onto the active home. That fixed WHERE the file
    landed but kept the blind wholesale overwrite — a raw publisher config
    (machine paths, ``mcp_servers``, and the base fork seed) replacing a
    member's file. The lane now belongs to
    ``persona_config_sync.apply_persona_config_pull``, which merges only
    allowlisted persona definitions key-wise. Same exclusion precedent as
    store/boards/*, store/office/*, skills/* → ``None``.
    """

    _pin_profiles(monkeypatch, tmp_path, active="alice")
    for token in ("alice", "bob", "neko", "base"):
        assert _destination_for_sync_path(f"profiles/{token}/config.yaml") is None, token
    # The NEW projection path is owned by the same applier, never the loop.
    assert _destination_for_sync_path("store/personas.yaml") is None


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
