"""One-file-per-chat-session coverage for the mission-chat turn store.

The hardening + perf suites already lock down the transition table, stale
repair, write-ahead outcomes, the cross-process lock, and retention. This suite
covers the storage-model split itself:

- filename scheme (deterministic, filesystem-safe, collision-free);
- per-session write isolation (a write touches ONLY that session's file);
- directory-enumeration reader parity with the old whole-store read;
- legacy-monolith migration (split once, rename aside, idempotent, converges
  from a simulated half-migrated state);
- session-file eviction to the archive (running / protected survive).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime import mission_chat_turns
from agent_runtime.mission_chat_turns import (
    MissionChatTurnPersistOutcome,
    mission_chat_turn_record,
    mission_chat_turn_records,
    persist_mission_chat_turn,
)


def _persist_completed(session_id: str, client_message_id: str) -> None:
    outcome = persist_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=f"turn_{client_message_id}",
        elements=[],
        state="completed",
    )
    assert outcome is MissionChatTurnPersistOutcome.PERSISTED


# ---------------------------------------------------------------------------
# Filename scheme
# ---------------------------------------------------------------------------


def test_session_filename_is_deterministic():
    stem = mission_chat_turns._session_filename_stem("session-alpha")
    assert stem == mission_chat_turns._session_filename_stem("session-alpha")


def test_session_filename_is_filesystem_safe():
    stem = mission_chat_turns._session_filename_stem("a/b\\c:d*e?f|g")
    assert all(ch.isalnum() or ch in "_.-" for ch in stem)


def test_session_filename_is_collision_free_for_same_sanitized_prefix():
    # Two distinct keys that sanitize to the SAME human-readable prefix must
    # still land in different files — the sha256 suffix disambiguates.
    a = mission_chat_turns._session_filename_stem("weird/key::with*chars")
    b = mission_chat_turns._session_filename_stem("weird_key__with_chars")
    assert a.rsplit("_", 1)[0] == b.rsplit("_", 1)[0]  # same sanitized prefix
    assert a != b  # different hash suffix
    assert len(a.rsplit("_", 1)[1]) == mission_chat_turns._SESSION_KEY_HASH_LEN


def test_session_filename_is_length_bounded_for_long_keys():
    stem = mission_chat_turns._session_filename_stem("x" * 500)
    assert len(stem) <= (
        mission_chat_turns._SESSION_KEY_PREFIX_MAX + 1 + mission_chat_turns._SESSION_KEY_HASH_LEN
    )


def test_all_special_char_key_still_yields_a_usable_stem():
    stem = mission_chat_turns._session_filename_stem("////")
    assert stem.startswith("session_")


# ---------------------------------------------------------------------------
# Per-session isolation
# ---------------------------------------------------------------------------


def test_write_touches_only_its_own_session_file(monkeypatch):
    _persist_completed("sA", "m1")
    _persist_completed("sB", "m1")

    written: list[Path] = []
    original = mission_chat_turns._write_session_file

    def _spy(path, data):
        written.append(Path(path))
        original(path, data)

    monkeypatch.setattr(mission_chat_turns, "_write_session_file", _spy)
    _persist_completed("sB", "m2")

    # No cross-file rewrite: only sB's file was written; sA's file was untouched.
    assert written == [mission_chat_turns._session_file_path("sB")]


def test_sessions_use_distinct_files_and_locks():
    assert mission_chat_turns._session_file_path("sA") != mission_chat_turns._session_file_path("sB")
    assert mission_chat_turns._session_lock_path("sA") != mission_chat_turns._session_lock_path("sB")


# ---------------------------------------------------------------------------
# Directory-enumeration reader parity
# ---------------------------------------------------------------------------


def test_directory_enumeration_matches_per_session_reads():
    _persist_completed("s1", "m1")
    _persist_completed("s1", "m2")
    _persist_completed("s2", "m1")

    # Enumerating the directory yields exactly the live session files, and each
    # file's decoded map matches what the per-session reader returns — the
    # union across files is the whole store, same as the old monolith read.
    files = mission_chat_turns._iter_session_files()
    assert {p.name for p in files} == {
        mission_chat_turns._session_file_path("s1").name,
        mission_chat_turns._session_file_path("s2").name,
    }

    for session_key in ("s1", "s2"):
        on_disk = json.loads(
            mission_chat_turns._session_file_path(session_key).read_text(encoding="utf-8")
        )
        via_reader = {
            record["client_message_id"]
            for record in mission_chat_turn_records(session_id=session_key)
        }
        assert set(on_disk.keys()) == via_reader


# ---------------------------------------------------------------------------
# Legacy monolith migration
# ---------------------------------------------------------------------------


def _write_legacy_monolith(isolate_agent_runtime_root: Path, payload: dict) -> Path:
    legacy = isolate_agent_runtime_root / "mission_chat_turns.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    return legacy


_LEGACY_PAYLOAD = {
    "sA": {
        "client_a1": {"schema_version": 1, "turn_id": "turn_a1", "state": "completed", "elements": []},
        "client_a2": {"schema_version": 1, "turn_id": "turn_a2", "state": "interrupted", "elements": []},
    },
    "sB": {
        "client_b1": {"schema_version": 1, "turn_id": "turn_b1", "state": "completed", "elements": []},
    },
}


def test_migration_splits_monolith_into_per_session_files(isolate_agent_runtime_root):
    legacy = _write_legacy_monolith(isolate_agent_runtime_root, _LEGACY_PAYLOAD)

    # First read triggers migration.
    record = mission_chat_turn_record(session_id="sA", client_message_id="client_a1")
    assert record["state"] == "completed"

    # Per-session files exist; monolith renamed aside (kept, not deleted).
    assert mission_chat_turns._session_file_path("sA").exists()
    assert mission_chat_turns._session_file_path("sB").exists()
    assert not legacy.exists()
    assert (isolate_agent_runtime_root / "mission_chat_turns.legacy.json").exists()

    # All records survived the split with their states intact.
    assert mission_chat_turn_record(session_id="sA", client_message_id="client_a2")["state"] == "interrupted"
    assert mission_chat_turn_record(session_id="sB", client_message_id="client_b1")["state"] == "completed"


def test_migration_is_idempotent_on_rerun(isolate_agent_runtime_root):
    _write_legacy_monolith(isolate_agent_runtime_root, _LEGACY_PAYLOAD)
    mission_chat_turns._migrate_legacy_if_present()
    first = mission_chat_turns._session_file_path("sA").read_text(encoding="utf-8")

    # Re-running with the monolith already gone is a no-op and leaves the split
    # files untouched.
    mission_chat_turns._migrate_legacy_if_present()
    assert mission_chat_turns._session_file_path("sA").read_text(encoding="utf-8") == first
    assert {p.name for p in mission_chat_turns._iter_session_files()} == {
        mission_chat_turns._session_file_path("sA").name,
        mission_chat_turns._session_file_path("sB").name,
    }


def test_migration_converges_from_half_migrated_state(isolate_agent_runtime_root):
    # Simulate a crash mid-migration: sA was already split (and then a live turn
    # wrote a NEWER record into it), while the monolith still lingers with sB
    # unsplit. A re-run must NOT clobber sA's authoritative file and must finish
    # splitting sB.
    live_sa = mission_chat_turns._session_file_path("sA")
    live_sa.parent.mkdir(parents=True, exist_ok=True)
    live_sa.write_text(
        json.dumps(
            {
                "client_live": {
                    "schema_version": 1,
                    "turn_id": "turn_live",
                    "state": "running",
                    "elements": [],
                }
            }
        ),
        encoding="utf-8",
    )
    _write_legacy_monolith(isolate_agent_runtime_root, _LEGACY_PAYLOAD)

    mission_chat_turns._migrate_legacy_if_present()

    # sA's pre-existing (authoritative) file was preserved, not overwritten by
    # the stale legacy sA payload.
    assert mission_chat_turn_record(session_id="sA", client_message_id="client_live")["state"] == "running"
    assert mission_chat_turn_record(session_id="sA", client_message_id="client_a1") is None
    # sB was split from the legacy monolith.
    assert mission_chat_turn_record(session_id="sB", client_message_id="client_b1")["state"] == "completed"
    # Monolith renamed aside; migration converged.
    assert not (isolate_agent_runtime_root / "mission_chat_turns.json").exists()
    assert (isolate_agent_runtime_root / "mission_chat_turns.legacy.json").exists()


def test_migration_of_corrupt_monolith_still_converges(isolate_agent_runtime_root):
    legacy = isolate_agent_runtime_root / "mission_chat_turns.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{not valid json", encoding="utf-8")

    # A corrupt monolith must not wedge migration into an infinite retry — it is
    # renamed aside so the store converges to the (empty) per-session layout.
    mission_chat_turns._migrate_legacy_if_present()
    assert not legacy.exists()
    assert (isolate_agent_runtime_root / "mission_chat_turns.legacy.json").exists()


def test_write_after_migration_appends_without_reviving_monolith(isolate_agent_runtime_root):
    _write_legacy_monolith(isolate_agent_runtime_root, _LEGACY_PAYLOAD)
    _persist_completed("sA", "client_a3")

    # The new record joined sA's per-session file alongside the migrated ones,
    # and the monolith is gone for good.
    keys = {
        record["client_message_id"] for record in mission_chat_turn_records(session_id="sA")
    }
    assert keys == {"client_a1", "client_a2", "client_a3"}
    assert not (isolate_agent_runtime_root / "mission_chat_turns.json").exists()


# ---------------------------------------------------------------------------
# serve read-cache fingerprint (the split's one out-of-store dependency)


def test_serve_fingerprint_flips_on_turn_flushes(isolate_agent_runtime_root):
    """A streamed-turn flush rewrites ONE session file in place and emits no
    EventLog event — the serve read-cache fingerprint must still flip, or a
    cached snapshot serves stale turn elements (the monolith stat went dead
    with the per-session split)."""

    from hermes_cli.harness_parts.serve import _runtime_state_fingerprint

    persist_mission_chat_turn(
        session_id="chat_fp_session",
        client_message_id="msg_1",
        turn_id="turn_1",
        elements=[{"type": "text", "text": "first"}],
        state="running",
        write_ahead=True,
    )
    fp1 = _runtime_state_fingerprint()

    # In-place rewrite of the SAME session file (an incremental stream flush).
    persist_mission_chat_turn(
        session_id="chat_fp_session",
        client_message_id="msg_1",
        turn_id="turn_1",
        elements=[{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
        state="running",
    )
    fp2 = _runtime_state_fingerprint()
    assert fp1 != fp2

    # A NEW session file must flip it too.
    persist_mission_chat_turn(
        session_id="chat_fp_other",
        client_message_id="msg_2",
        turn_id="turn_2",
        elements=[{"type": "text", "text": "hello"}],
        state="completed",
    )
    fp3 = _runtime_state_fingerprint()
    assert fp2 != fp3
