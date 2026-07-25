"""Profile-scoped realm-sync pull destinations — W-H4 (office plan §5.1) and its
2026-07-25 supersession.

Before 2026-07-17 ``_destination_for_sync_path`` collapsed every
``profiles/<name>/…`` artifact into the ACTIVE profile home — a degenerate
ternary whose two branches both returned ``get_hermes_home()`` — so a
multi-profile realm pull silently last-write-wins'd every profile's
config.yaml / MEMORY.md onto one home. W-H4 made it profile-aware.

**Superseded 2026-07-25:** profile-scoping fixed WHERE a pulled file landed but
kept the blind wholesale overwrite AND kept prompt destinations keyed by the file
basename. ``profiles/…`` now has NO generic pull destination at all — the whole
family belongs to ``profile_artifact_sync`` (baseline merge, hold on divergence)
and ``persona_config_sync`` (allowlisted key-wise merge). These tests pin that
exclusion; the merge behaviour itself lives in ``test_profile_artifact_sync.py``.

``_profile_home_for_token`` survives as the shared, hostile-token-refusing
profile resolver both appliers call.
"""

from __future__ import annotations

from pathlib import Path

import agent_runtime.realm_sync as realm_sync
from agent_runtime.realm_sync import _destination_for_sync_path, _profile_home_for_token


def _pin_profiles(monkeypatch, tmp_path, *, active: str = "alice") -> Path:
    profiles_root = tmp_path / "profiles"
    monkeypatch.setattr(realm_sync, "active_profile_name", lambda: active)
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda name: profiles_root / profiles_mod.normalize_profile_name(name))
    return profiles_root


def test_profile_config_is_never_a_generic_pull_destination(monkeypatch, tmp_path):
    """A profile ``config.yaml`` has no generic pull destination: the lane belongs
    to ``persona_config_sync.apply_persona_config_pull``, which merges only
    allowlisted persona definitions key-wise."""

    _pin_profiles(monkeypatch, tmp_path, active="alice")
    for token in ("alice", "bob", "neko", "base"):
        assert _destination_for_sync_path(f"profiles/{token}/config.yaml") is None, token
    # The NEW projection path is owned by the same applier, never the loop.
    assert _destination_for_sync_path("store/personas.yaml") is None


def test_profile_files_are_never_a_generic_pull_destination(monkeypatch, tmp_path):
    """THE data-loss guard (2026-07-25).

    A member's ``MEMORY.md``, core-context files and persona prompts were
    overwritten wholesale by the generic write-loop. Restoring ANY of these
    mappings hands the family back to that loop and re-arms the defect, so this
    assertion is the structural half of the sabotage guard in
    ``test_profile_artifact_sync.py``.
    """

    _pin_profiles(monkeypatch, tmp_path, active="alice")
    for rel in (
        "profiles/alice/personas/dev/memories/MEMORY.md",
        "profiles/bob/personas/dev/memories/MEMORY.md",
        "profiles/bob/personas/dev/context/AGENTS.md",
        "profiles/bob/personas/dev/context/CLAUDE.md",
        "profiles/bob/personas/dev/system_prompt/dev.md",
        "profiles/bob/personas/dev/soul_overlay/SOUL.md",
        "store/profile_files/bob/memories/MEMORY.md",
        "store/profile_files/bob/AGENTS.md",
        "store/profile_files/bob/personas/dev/prompt.md",
    ):
        assert _destination_for_sync_path(rel) is None, rel


def test_generic_loop_still_owns_workspaces_and_realms(monkeypatch, tmp_path):
    """The exclusions above must not have swallowed the two families the generic
    loop legitimately owns."""

    _pin_profiles(monkeypatch, tmp_path, active="alice")
    assert _destination_for_sync_path("store/workspaces/ws_1.json") is not None
    assert _destination_for_sync_path("store/realms/realm_1.json") is not None


def test_named_profile_homes_stay_profile_scoped(monkeypatch, tmp_path):
    profiles_root = _pin_profiles(monkeypatch, tmp_path, active="alice")
    assert _profile_home_for_token("bob") == profiles_root / "bob"
    assert _profile_home_for_token("alice") == realm_sync.get_hermes_home()


def test_hostile_profile_tokens_are_refused(monkeypatch, tmp_path):
    _pin_profiles(monkeypatch, tmp_path, active="alice")
    for token in ("..", ".", "", "c:evil", "/abs", "\\\\share", "a/b"):
        assert _profile_home_for_token(token) is None, token
    assert _destination_for_sync_path("profiles/../config.yaml") is None
