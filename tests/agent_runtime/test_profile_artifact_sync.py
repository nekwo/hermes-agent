"""The per-profile FILE family: baseline merge, never a wholesale overwrite.

``c905569c1`` retired the wholesale-overwrite defect for ONE artifact kind
(``persona_config``). Four more still mapped to real destinations and were still
overwritten blind by the generic write-loop — ``profile_memory``,
``core_context``, ``system_prompt``, ``soul_overlay``. **A member's accumulated
``MEMORY.md`` was destroyed by a realm pull.**

The load-bearing test in this file is
:func:`test_diverged_member_memory_is_never_overwritten`: it is the guard against
this class ever coming back, and it was sabotage-verified (restore the wholesale
write and it goes red).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_runtime.realm_sync as realm_sync
from agent_runtime import paths
from agent_runtime.profile_artifact_sync import (
    CORE_CONTEXT_FILENAMES,
    MEMORY_DESTINATION,
    PROFILE_FILES_ROOT,
    ProfileArtifactResolveError,
    apply_profile_artifact_pull,
    classify_destination,
    content_hash,
    entity_key,
    read_profile_artifact_baseline,
    read_remote_profile_files,
    resolve_profile_artifact,
    write_profile_artifact_baseline,
)

REALM = "realm_test"


# ── harness ────────────────────────────────────────────────────────────────


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """Two profile homes (``alice`` active, ``bob`` named) + a subtree builder."""

    profiles_root = tmp_path / "profiles"
    active_home = profiles_root / "alice"
    active_home.mkdir(parents=True)
    monkeypatch.setattr(realm_sync, "active_profile_name", lambda: "alice")
    monkeypatch.setattr(realm_sync, "get_hermes_home", lambda: active_home)
    import hermes_cli.profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod, "get_profile_dir", lambda name: profiles_root / profiles_mod.normalize_profile_name(name)
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: (profiles_root / name).exists())
    return profiles_root


def _publish(subtree: Path, profile: str, dest_rel: str, body: str) -> None:
    path = subtree.joinpath(*PROFILE_FILES_ROOT.split("/"), profile, *dest_rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))


def _publish_legacy(subtree: Path, profile: str, persona: str, segment: str, name: str, body: str) -> None:
    path = subtree / "profiles" / profile / "personas" / persona / segment / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))


def _local(homes: Path, profile: str, dest_rel: str, body: str) -> Path:
    path = homes.joinpath(profile, *dest_rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))
    return path


# ── the destination allowlist ──────────────────────────────────────────────


def test_destination_allowlist_is_opt_in():
    assert classify_destination(MEMORY_DESTINATION) == "profile_memory"
    for name in CORE_CONTEXT_FILENAMES:
        assert classify_destination(name) == "core_context"
    assert classify_destination("personas/dev/prompt.md") == "persona_prompt"
    assert classify_destination("personas/prompt.md") == "persona_prompt"
    # A prompt/overlay is profile-relative to ANYWHERE in the home. Restricting
    # it to ``personas/**`` shipped a silent one-way loss: publish emitted a
    # profile-root ``soul.md`` and every member refused it (caught 2026-07-25).
    assert classify_destination("soul.md") == "persona_prompt"
    assert classify_destination("prompts/role/dev.txt") == "persona_prompt"
    # Everything dangerous in a profile home is a NON-document, so the suffix
    # rule — not a directory prefix — is what closes the door.
    for rel in (
        "config.yaml",
        ".env",
        "state.db",
        "plugins/thing/plugin.py",
        "skins/custom.yaml",
        ".hidden/prompt.md",
        "memories/other.md",  # the member-state dir is closed to prompt writes
        "personas",
        "",
        "a/b/c/d/e/f/g/h/i/j.md",
    ):
        assert classify_destination(rel) is None, rel


def test_publish_and_pull_agree_on_every_destination(homes, tmp_path, monkeypatch):
    """The asymmetry guard.

    ``realm_sync._persona_artifacts`` (publish) and ``classify_destination``
    (pull) are two sides of ONE contract. When they disagreed, a file published
    into the realm that every member then refused — silently, one-way, with no
    accounting. This pins that every destination publish EMITS is one the pull
    side ADMITS, and that anything inadmissible is withheld with a typed row.
    """

    from types import SimpleNamespace

    from agent_runtime.models import AgentPersona
    from agent_runtime.realm_sync import _persona_artifacts

    home = homes / "alice"
    for rel in ("soul.md", "memories/MEMORY.md", "AGENTS.md"):
        path = home.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        realm_sync,
        "resolve_persona_profile",
        lambda persona: SimpleNamespace(profile_home=home, hermes_profile="alice"),
    )

    def _make(**over):
        base = dict(
            id="dev",
            role="dev",
            display_name="Dev",
            model="",
            provider="",
            api_mode="",
            toolsets=[],
            system_prompt_path="",
            include_profile_memory=True,
            include_core_context_files=True,
        )
        base.update(over)
        return AgentPersona(**base)

    prefix = f"{PROFILE_FILES_ROOT}/alice/"
    artifacts, withheld = _persona_artifacts(_make(soul_overlay_path="soul.md"))
    published = sorted(item.relative_path for item in artifacts)
    assert published == [
        f"{prefix}AGENTS.md",
        f"{prefix}memories/MEMORY.md",
        f"{prefix}soul.md",
    ]
    assert withheld == []
    for rel in published:
        assert classify_destination(rel[len(prefix):]) is not None, rel

    # An inadmissible destination is withheld with a typed row, never emitted.
    (home / "notes.yaml").write_text("x\n", encoding="utf-8")
    artifacts, withheld = _persona_artifacts(_make(soul_overlay_path="notes.yaml"))
    assert all(not item.relative_path.endswith("notes.yaml") for item in artifacts)
    assert [(row["kind"], row["reason"]) for row in withheld] == [
        ("soul_overlay", "destination_not_publishable")
    ]


# ── decision table, per artifact kind ──────────────────────────────────────


@pytest.mark.parametrize(
    "dest_rel",
    [MEMORY_DESTINATION, "AGENTS.md", "CLAUDE.md", "GEMINI.md", "personas/dev/prompt.md"],
)
def test_adopts_when_the_member_has_nothing(homes, tmp_path, dest_rel):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", dest_rel, "realm content\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    key = entity_key("alice", dest_rel)
    assert summary.adopted == [key]
    assert summary.held == []
    assert homes.joinpath("alice", *dest_rel.split("/")).read_bytes() == b"realm content\n"
    assert read_profile_artifact_baseline(REALM)[key] == content_hash(b"realm content\n")


def test_converges_when_identical_without_rewriting(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "same\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "same\n")
    before = local.stat().st_mtime_ns

    summary = apply_profile_artifact_pull(REALM, subtree)

    key = entity_key("alice", MEMORY_DESTINATION)
    assert summary.converged == [key]
    assert summary.adopted == []
    assert local.stat().st_mtime_ns == before  # no churn


def test_crlf_local_and_lf_remote_converge(homes, tmp_path):
    """A Windows member's CRLF ``MEMORY.md`` against the publisher's LF artifact
    is the SAME content — hashing canonically is what keeps it from conflicting
    forever."""

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "line one\nline two\n")
    path = homes / "alice" / "memories" / "MEMORY.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"line one\r\nline two\r\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.converged == [entity_key("alice", MEMORY_DESTINATION)]
    assert path.read_bytes() == b"line one\r\nline two\r\n"  # untouched


def test_diverged_member_memory_is_never_overwritten(homes, tmp_path):
    """THE guard. Both sides moved → HOLD, and the member's file is byte-for-byte
    untouched.

    Sabotage-verified 2026-07-25: restoring the wholesale write (either the
    ``profiles/…`` mapping in ``_destination_for_sync_path`` or the
    ``_may_write`` invariant) turns this red.
    """

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm memories\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "the member's accumulated memories\n")
    key = entity_key("alice", MEMORY_DESTINATION)
    # A baseline that matches NEITHER side: both have moved since the last sync.
    write_profile_artifact_baseline(REALM, {key: content_hash(b"what the last sync saw\n")})

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.held == [key]
    assert summary.adopted == []
    assert local.read_bytes() == b"the member's accumulated memories\n"


def test_first_pull_over_a_pre_existing_memory_holds(homes, tmp_path):
    """No baseline at all (the very first pull after this shipped) with content on
    both sides is ``new_both`` — a hold, never a clobber."""

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm memories\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "mine\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.held == [entity_key("alice", MEMORY_DESTINATION)]
    assert local.read_bytes() == b"mine\n"


def test_member_edit_against_unchanged_remote_keeps_local(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm memories\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "mine\n")
    key = entity_key("alice", MEMORY_DESTINATION)
    write_profile_artifact_baseline(REALM, {key: content_hash(b"realm memories\n")})

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.kept_local == [key]
    assert local.read_bytes() == b"mine\n"


def test_untouched_member_copy_takes_the_realms_update(homes, tmp_path):
    """local == baseline: the member accumulated nothing since the last sync, so
    adopting loses nothing of theirs. This is what keeps the lane from degrading
    into a permanent hold storm."""

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", "AGENTS.md", "v2\n")
    local = _local(homes, "alice", "AGENTS.md", "v1\n")
    key = entity_key("alice", "AGENTS.md")
    write_profile_artifact_baseline(REALM, {key: content_hash(b"v1\n")})

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.adopted == [key]
    assert local.read_bytes() == b"v2\n"


def test_remote_removal_retains_the_member_file(homes, tmp_path):
    """The realm stopped publishing it. A member's memory/prompt is NEVER deleted
    by a sync — it is retained and reported."""

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", "AGENTS.md", "still published\n")  # keeps source != None
    local = _local(homes, "alice", MEMORY_DESTINATION, "mine\n")
    key = entity_key("alice", MEMORY_DESTINATION)
    write_profile_artifact_baseline(REALM, {key: content_hash(b"mine\n")})

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.retained == [key]
    assert local.exists()
    assert key not in read_profile_artifact_baseline(REALM)


def test_a_realm_with_no_profile_files_never_touches_the_baseline(homes, tmp_path):
    subtree = tmp_path / "subtree"
    subtree.mkdir()
    baseline = {entity_key("alice", MEMORY_DESTINATION): "deadbeef"}
    write_profile_artifact_baseline(REALM, baseline)

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.source is None
    assert summary.as_dict()["retained"] == []
    assert read_profile_artifact_baseline(REALM) == baseline


def test_dry_run_classifies_without_writing_anything(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm\n")
    before = paths.profile_artifact_baseline_path(REALM)

    summary = apply_profile_artifact_pull(REALM, subtree, dry_run=True)

    assert summary.adopted == [entity_key("alice", MEMORY_DESTINATION)]
    assert not (homes / "alice" / "memories" / "MEMORY.md").exists()
    assert not before.exists()


# ── (a) persona-keyed prompt destinations ──────────────────────────────────


def test_two_personas_same_prompt_filename_do_not_collide(homes, tmp_path):
    """The §5.1 collision, verbatim: two personas on ONE profile with same-named
    prompt files. The published tail is the publisher's profile-relative path, so
    both land where their own definition points."""

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", "personas/dev/prompt.md", "dev prompt\n")
    _publish(subtree, "alice", "personas/qa/prompt.md", "qa prompt\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.adopted == [
        entity_key("alice", "personas/dev/prompt.md"),
        entity_key("alice", "personas/qa/prompt.md"),
    ]
    assert (homes / "alice" / "personas" / "dev" / "prompt.md").read_bytes() == b"dev prompt\n"
    assert (homes / "alice" / "personas" / "qa" / "prompt.md").read_bytes() == b"qa prompt\n"


def test_named_profiles_stay_isolated(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "alice memory\n")
    _publish(subtree, "bob", MEMORY_DESTINATION, "bob memory\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert sorted(summary.profiles) == ["alice", "bob"]
    assert (homes / "alice" / "memories" / "MEMORY.md").read_bytes() == b"alice memory\n"
    assert (homes / "bob" / "memories" / "MEMORY.md").read_bytes() == b"bob memory\n"


def test_materialized_profile_homes_are_reported(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "a\n")
    _publish(subtree, "neko", MEMORY_DESTINATION, "n\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    # alice is the ACTIVE profile (never "created"); neko is materialized here.
    assert summary.created_profiles == ["neko"]


def test_two_publishers_claiming_one_destination_are_refused(homes, tmp_path):
    """Different content for one destination is a collision, not a last-writer
    -wins. Both are refused and neither is written."""

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", "personas/dev/prompt.md", "from the new layout\n")
    _publish_legacy(subtree, "alice", "dev", "system_prompt", "prompt.md", "from the legacy layout\n")
    # The legacy tail maps to ``personas/prompt.md`` — a DIFFERENT destination, so
    # force the collision explicitly on one key instead.
    _publish_legacy(subtree, "alice", "qa", "system_prompt", "prompt.md", "yet another\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    refused = {row["key"]: row["code"] for row in summary.refused}
    assert refused.get(entity_key("alice", "personas/prompt.md")) == "destination_collision"
    assert not (homes / "alice" / "personas" / "prompt.md").exists()


def test_publish_tail_is_the_destination_so_a_prompt_round_trips(homes, tmp_path, monkeypatch):
    """The orphan fix, end to end.

    A publisher whose ``soul_overlay_path`` is ``personas/neko/SOUL.md`` used to
    publish ``…/soul_overlay/SOUL.md``, which a member wrote to
    ``personas/SOUL.md`` — a path their own persona definition does NOT name, so
    the pulled file was dead on arrival. The published tail is now the
    profile-relative destination, so publish and pull agree by construction.
    """

    from types import SimpleNamespace

    from agent_runtime.models import AgentPersona
    from agent_runtime.realm_sync import _persona_artifacts

    publisher_home = homes / "alice"
    overlay = publisher_home / "personas" / "neko" / "SOUL.md"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("soul\n", encoding="utf-8")
    monkeypatch.setattr(
        realm_sync,
        "resolve_persona_profile",
        lambda persona: SimpleNamespace(profile_home=publisher_home, hermes_profile="alice"),
    )
    persona = AgentPersona(
        id="neko",
        role="supervisor",
        display_name="Neko",
        model="",
        provider="",
        api_mode="",
        toolsets=[],
        system_prompt_path="",
        soul_overlay_path="personas/neko/SOUL.md",
    )

    artifacts, withheld = _persona_artifacts(persona)

    assert [item.relative_path for item in artifacts] == [
        f"{PROFILE_FILES_ROOT}/alice/personas/neko/SOUL.md"
    ]
    assert artifacts[0].persona_id == "neko"
    assert withheld == []

    # And the pull side lands it back at exactly ``personas/neko/SOUL.md`` —
    # never the flat ``personas/SOUL.md`` the basename-keyed mapper produced.
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", "personas/neko/SOUL.md", "soul from the realm\n")
    overlay.unlink()  # a member who does not have it yet
    member = homes / "alice"
    summary = apply_profile_artifact_pull(REALM, subtree)
    assert summary.adopted == [entity_key("alice", "personas/neko/SOUL.md")]
    assert (homes / "alice" / "personas" / "neko" / "SOUL.md").read_bytes() == b"soul from the realm\n"
    assert not (member / "personas" / "SOUL.md").exists()


# ── (b) admission scanning ─────────────────────────────────────────────────


def test_secret_shaped_content_is_refused_per_entity(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, 'api_key = "sk-test-secret-value-123456"\n')
    _publish(subtree, "alice", "AGENTS.md", "clean\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert [row["code"] for row in summary.refused] == ["secret_shaped_value"]
    assert summary.adopted == [entity_key("alice", "AGENTS.md")]  # isolation
    assert not (homes / "alice" / "memories" / "MEMORY.md").exists()


def test_off_allowlist_destination_is_refused(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", "config.yaml", "agent_runtime: {}\n")
    _publish(subtree, "alice", "AGENTS.md", "clean\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert [row["code"] for row in summary.refused] == ["destination_not_allowed"]
    assert not (homes / "alice" / "config.yaml").exists()


def test_hostile_profile_token_is_refused(homes, tmp_path):
    """A profile token that ``_profile_home_for_token`` will not resolve refuses
    the whole entity rather than guessing a home. (``..`` cannot exist as a real
    directory NAME, so the realistic hostile shape is a token outside the safe
    character class.)"""

    subtree = tmp_path / "subtree"
    _publish(subtree, "bad name!", MEMORY_DESTINATION, "escape\n")
    _publish(subtree, "alice", "AGENTS.md", "clean\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert [row["code"] for row in summary.refused] == ["unsafe_profile_token"]
    assert summary.adopted == [entity_key("alice", "AGENTS.md")]


# ── version tolerance, both directions ─────────────────────────────────────


def test_new_member_pulling_an_old_publisher_merges_the_legacy_layout(homes, tmp_path):
    """OLD publisher → NEW member. The legacy per-persona paths are read and
    merged through the SAME decision table, landing at the SAME destination the
    old write-loop used — so nothing moves, and the member's copy is held rather
    than clobbered."""

    subtree = tmp_path / "subtree"
    _publish_legacy(subtree, "alice", "dev", "memories", "MEMORY.md", "realm memories\n")
    _publish_legacy(subtree, "alice", "dev", "context", "AGENTS.md", "realm agents\n")
    _publish_legacy(subtree, "alice", "dev", "system_prompt", "dev.md", "realm prompt\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "mine\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert summary.source == "legacy_profiles"
    assert summary.held == [entity_key("alice", MEMORY_DESTINATION)]
    assert local.read_bytes() == b"mine\n"
    assert (homes / "alice" / "AGENTS.md").read_bytes() == b"realm agents\n"
    assert (homes / "alice" / "personas" / "dev.md").read_bytes() == b"realm prompt\n"


def _pre_2026_07_25_destination(rel: str, profile_home: Path) -> Path | None:
    """The mapper an OLDER hermes runs, replicated verbatim from
    ``_destination_for_sync_path`` before this change.

    Reproduced here (rather than asserted about in prose) because "what does an
    old client do with our new paths" is the whole version-tolerance contract and
    the only way to test it is to run their rules.
    """

    parts = Path(rel).parts
    if parts and parts[0] == "skills":
        return None
    if len(parts) == 3 and parts[0] == "store" and parts[1] == "workspaces":
        return Path("workspaces") / parts[2]
    if len(parts) == 3 and parts[0] == "store" and parts[1] == "realms":
        return Path("realms") / parts[2]
    if parts and parts[0] == "store" and len(parts) == 2 and parts[1] == "personas.yaml":
        return None
    if parts and parts[0] == "profiles" and len(parts) > 1:
        if len(parts) == 3 and parts[2] == "config.yaml":
            return None
        if "memories" in parts and parts[-1] == "MEMORY.md":
            return profile_home / "memories" / "MEMORY.md"
        if "context" in parts:
            return profile_home / parts[-1]
        if "system_prompt" in parts or "soul_overlay" in parts:
            return profile_home / "personas" / parts[-1]
    return None


def test_old_member_pulling_a_new_publisher_skips_the_family(homes, tmp_path):
    """NEW publisher → OLD member, stated plainly.

    An older hermes still runs its own mapper. Under the LEGACY published path it
    maps ``…/memories/MEMORY.md`` onto the member's home and overwrites it
    wholesale — a defect no publisher-side fix can reach except by moving the
    path. Under the NEW published path its mapper returns ``None``, so the old
    member skips the whole family: they receive no profile files (degraded), and
    their accumulated ``MEMORY.md`` survives (the point).
    """

    from agent_runtime.profile_artifact_sync import published_relative_path

    home = homes / "alice"
    legacy = "profiles/alice/personas/dev/memories/MEMORY.md"
    assert _pre_2026_07_25_destination(legacy, home) == home / "memories" / "MEMORY.md"

    for dest in (MEMORY_DESTINATION, "AGENTS.md", "personas/dev/prompt.md"):
        rel = published_relative_path("alice", dest)
        assert _pre_2026_07_25_destination(rel, home) is None, rel
        # …and the CURRENT mapper agrees: the family belongs to this applier.
        assert realm_sync._destination_for_sync_path(rel) is None, rel


def test_new_layout_wins_over_a_legacy_twin_without_double_writing(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "new layout\n")
    _publish_legacy(subtree, "alice", "dev", "memories", "MEMORY.md", "legacy layout\n")

    entries, refusals, source, _profiles = read_remote_profile_files(subtree)

    key = entity_key("alice", MEMORY_DESTINATION)
    assert source == "profile_files"
    assert entries[key].data == b"new layout\n"
    assert refusals == []


# ── the superseded legacy flat prompt ──────────────────────────────────────


def test_legacy_flat_prompt_is_reported_not_deleted(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", "personas/dev/prompt.md", "scoped\n")
    flat = _local(homes, "alice", "personas/prompt.md", "left over from the old layout\n")

    summary = apply_profile_artifact_pull(REALM, subtree)

    assert [row["reason"] for row in summary.superseded] == ["legacy_flat_layout"]
    assert summary.superseded[0]["key"] == entity_key("alice", "personas/prompt.md")
    assert flat.exists()  # never deleted by a sync


# ── operator resolution ────────────────────────────────────────────────────


def test_resolve_take_local_stops_the_hold_without_touching_content(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm memories\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "mine\n")
    key = entity_key("alice", MEMORY_DESTINATION)
    assert apply_profile_artifact_pull(REALM, subtree).held == [key]

    row = resolve_profile_artifact(REALM, subtree, key, take="local")

    assert row["changed"] is False
    assert local.read_bytes() == b"mine\n"
    # The next pull sees local-changed vs unchanged-remote: kept_local, not a
    # conflict. The hold stops re-reporting; the content stays the member's.
    again = apply_profile_artifact_pull(REALM, subtree)
    assert again.held == []
    assert again.kept_local == [key]
    assert local.read_bytes() == b"mine\n"


def test_resolve_take_remote_adopts_and_then_noops(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm memories\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "mine\n")
    key = entity_key("alice", MEMORY_DESTINATION)
    assert apply_profile_artifact_pull(REALM, subtree).held == [key]

    row = resolve_profile_artifact(REALM, subtree, key, take="remote")

    assert row["changed"] is True
    assert local.read_bytes() == b"realm memories\n"
    again = apply_profile_artifact_pull(REALM, subtree)
    assert again.held == [] and again.adopted == []
    assert again.converged == [key]


def test_resolve_dry_run_leaves_the_store_byte_identical(homes, tmp_path):
    """``_add_stage42_global_args(mutation=True)`` auto-registers ``--dry-run``;
    a verb that does not READ it silently mutates on a preview. Recurred twice in
    this repo — pinned here at the store chokepoint."""

    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm memories\n")
    local = _local(homes, "alice", MEMORY_DESTINATION, "mine\n")
    key = entity_key("alice", MEMORY_DESTINATION)
    apply_profile_artifact_pull(REALM, subtree)
    baseline_path = paths.profile_artifact_baseline_path(REALM)
    before_file = local.read_bytes()
    before_baseline = baseline_path.read_bytes()

    for take in ("local", "remote"):
        row = resolve_profile_artifact(REALM, subtree, key, take=take, dry_run=True)
        assert row["key"] == key
        assert local.read_bytes() == before_file
        assert baseline_path.read_bytes() == before_baseline


def test_resolve_refuses_an_unknown_key(homes, tmp_path):
    subtree = tmp_path / "subtree"
    _publish(subtree, "alice", MEMORY_DESTINATION, "realm\n")
    with pytest.raises(ProfileArtifactResolveError) as excinfo:
        resolve_profile_artifact(REALM, subtree, "alice:AGENTS.md", take="remote")
    assert excinfo.value.code == "not_found"
    with pytest.raises(ProfileArtifactResolveError) as excinfo:
        resolve_profile_artifact(REALM, subtree, "not-a-key", take="remote")
    assert excinfo.value.code == "invalid_request"


# ── the baseline sidecar is un-syncable ────────────────────────────────────


def test_baseline_sidecars_can_never_be_published_or_pulled(homes, tmp_path):
    """A baseline that could itself travel would let one member's merge state
    overwrite another's — the sidecar must be structurally unreachable from both
    directions."""

    baseline_paths = [
        paths.profile_artifact_baseline_path(REALM),
        paths.persona_config_baseline_path(REALM),
        paths.board_baseline_path(REALM),
        paths.office_baseline_path(REALM),
    ]
    # 1. No pulled relative path resolves ONTO a baseline sidecar. The generic
    #    loop only maps store/workspaces + store/realms; nothing else has a
    #    destination at all.
    for rel in (
        "store/realm_sync/realm_test/profile_artifact_baseline.json",
        "store/workspaces/../realm_sync/realm_test/board_baseline.json",
        f"{PROFILE_FILES_ROOT}/alice/../../realm_sync/x.json",
        "profiles/alice/personas/dev/memories/MEMORY.md",
    ):
        destination = realm_sync._destination_for_sync_path(rel)
        assert destination is None or destination not in baseline_paths, rel

    # 2. The sidecars live outside every pull destination ROOT, so no published
    #    tail can name one.
    roots = [paths.workspaces_dir().resolve(), paths.realms_dir().resolve(), (homes / "alice").resolve()]
    for path in baseline_paths:
        for root in roots:
            assert root not in path.resolve().parents, f"{path} is inside {root}"

    # 3. A baseline sitting in the realm-sync tree is never an ingestable profile
    #    file either — its destination is off the allowlist.
    assert classify_destination("realm_sync/realm_test/profile_artifact_baseline.json") is None


def test_baseline_is_not_a_published_artifact(homes, tmp_path, isolate_agent_runtime_root):
    """End-to-end: write every baseline sidecar, then resolve what a publish would
    ship. None of them is in it."""

    write_profile_artifact_baseline(REALM, {entity_key("alice", MEMORY_DESTINATION): "abc"})
    from agent_runtime.board_sync import write_board_baseline
    from agent_runtime.office_sync import write_office_baseline
    from agent_runtime.persona_config_sync import write_persona_config_baseline

    write_board_baseline(REALM, {"b:board": "abc"})
    write_office_baseline(REALM, {"w:office": "abc"})
    write_persona_config_baseline(REALM, {"dev": "abc"})

    from agent_runtime.store import RealmStore

    realm = RealmStore().create(name="Test", server_id=None)
    published = {row["path"] for row in _publish_artifact_rows(realm.id)}
    assert not any("baseline" in path for path in published)


def _publish_artifact_rows(realm_id: str) -> list[dict]:
    return [artifact.row() for artifact in realm_sync.resolve_realm_sync_artifacts(realm_id)]
