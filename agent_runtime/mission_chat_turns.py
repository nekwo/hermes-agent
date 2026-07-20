from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from . import paths
from .persona_assignments import safe_assignment_text, safe_assignment_token

# ---------------------------------------------------------------------------
# Storage layout (one file per chat session)
# ---------------------------------------------------------------------------
# Each chat session owns exactly one file
#   ``mission_chat_turns/<safe_session_key>.json``
# holding that session's ``{client_message_id: record}`` map, plus a co-located
# per-session lock ``mission_chat_turns/<safe_session_key>.lock``. Concurrent
# turns in DIFFERENT chats never contend (they touch different files/locks);
# same-session concurrency keeps today's exclusive read-modify-write semantics.
# This retires the old single ``mission_chat_turns.json`` monolith, whose every
# incremental flush rewrote the WHOLE store under one global lock — the store
# now aligns with the per-actor "the store IS the checkpoint" model (operator
# ruling 2026-07-17: "one file per chat is a good method for storage").
_STORE_DIR_NAME = "mission_chat_turns"
_ARCHIVE_DIR_NAME = "mission_chat_turns_archive"
# Legacy monolith. Split ONCE into per-session files on first read/write that
# finds it, then renamed aside (kept, never deleted).
_LEGACY_STORE_NAME = "mission_chat_turns.json"
_LEGACY_MIGRATED_NAME = "mission_chat_turns.legacy.json"
_MIGRATE_LOCK_NAME = "mission_chat_turns.migrate.lock"
_GC_LOCK_NAME = "mission_chat_turns.gc.lock"
_SESSION_FILE_SUFFIX = ".json"
_SESSION_LOCK_SUFFIX = ".lock"
# Filename scheme: a sanitized, human-readable prefix of the session key plus a
# sha256 suffix so long/odd session ids stay filesystem-safe AND collision-free
# (two keys with the same sanitized prefix still get distinct files). 80 + 1 +
# 12 + 5 (".json") = 98 chars keeps us well under the Windows MAX_PATH budget
# for any sane runtime root.
_SESSION_KEY_PREFIX_MAX = 80
_SESSION_KEY_HASH_LEN = 12

_LOCK_TIMEOUT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.01
# Migration is a one-time, bounded split — wait comfortably for whichever
# process is doing it rather than racing a partial layout.
_MIGRATE_LOCK_TIMEOUT_SECONDS = 10.0
# The max-sessions GC is opportunistic (retention "can transiently exceed its
# bound rather than lose live state"), so both its own lock and the per-file
# probes it takes are short/non-blocking — a busy session is simply skipped and
# retried on the next new-session write.
_GC_LOCK_TIMEOUT_SECONDS = 1.0
_MAX_ELEMENTS = 80
_MAX_TEXT = 20000
# Retention bounds, applied on every write inside the per-session lock (turn
# cap) and after each new-session-file creation (session-file GC). The
# per-session bound must stay comfortably above the projection's displayable
# message tail (MAX_PERSONA_CHAT_MESSAGE_TAIL = 40 in persona_chat_history.py)
# so no displayable agent row loses turn_elements.
_RETENTION_MAX_TURNS_PER_SESSION = 100
_RETENTION_MAX_SESSIONS = 50
_SENSITIVE_FILE_MARKERS = ("private_token", "secret_token", "api_key", "apikey", "credential")
JOURNAL_TURN_STATES = {
    "pending",
    "executing",
    "outcome_unknown",
    "native_committed",
    "projected",
    "abandoned",
}
_LEGACY_TURN_STATES = {"running", "completed", "failed", "interrupted"}
_VALID_TURN_STATES = JOURNAL_TURN_STATES | _LEGACY_TURN_STATES
_JOURNAL_TRANSITIONS = {
    None: {"pending"},
    "pending": {"pending", "executing", "abandoned"},
    "executing": {"native_committed", "outcome_unknown"},
    "outcome_unknown": {"abandoned", "native_committed"},
    "native_committed": {"native_committed", "projected"},
    "projected": {"projected"},
    "abandoned": {"abandoned"},
}

_T = TypeVar("_T")


class MissionChatTurnPersistOutcome(str, Enum):
    """Typed result of a turn-record persist. No write is ever lost silently:
    every skipped or rejected persist names its reason."""

    PERSISTED = "persisted"
    SKIPPED_NO_KEYS = "skipped_no_keys"
    SKIPPED_EMPTY_LEGACY = "skipped_empty_legacy"
    REJECTED_INVALID_STATE = "rejected_invalid_state"
    REJECTED_STALE_TRANSITION = "rejected_stale_transition"
    SKIPPED_LOCK_TIMEOUT = "skipped_lock_timeout"


def next_turn_state(
    current: str | None,
    requested: str | None,
    *,
    write_ahead: bool = False,
) -> str | None:
    """Single transition authority for turn-record states.

    Returns the state to store, or ``None`` when the write must be rejected.
    Rules:
    - ``requested=None`` (legacy elements-only call) preserves the current
      state; a brand-new record defaults to ``running``.
    - Explicit terminal states (``completed``/``failed``) and the repair state
      (``interrupted``) always win — a completed reply recorded after a
      repair flip must not be lost.
    - ``running`` with ``write_ahead=True`` is a fresh turn start (same-client
      retry after an interrupted/failed turn) and always wins.
    - ``running`` with ``write_ahead=False`` is an incremental on_update flush
      and must NOT resurrect a settled record: a late flush from a turn that
      another process already repaired to ``interrupted`` (or that completed)
      is stale and is rejected.
    """

    if current in JOURNAL_TURN_STATES and requested in _LEGACY_TURN_STATES:
        return None
    if requested is None:
        return current or "running"
    if requested not in _VALID_TURN_STATES:
        return None
    if requested != "running":
        return requested
    if write_ahead or current is None or current == "running":
        return "running"
    return None


def persist_mission_chat_turn(
    *,
    session_id: str | None,
    client_message_id: str | None,
    turn_id: str | None,
    elements: list[dict[str, Any]] | None,
    state: str | None = None,
    write_ahead: bool = False,
    metadata: dict[str, Any] | None = None,
) -> MissionChatTurnPersistOutcome:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key:
        return MissionChatTurnPersistOutcome.SKIPPED_NO_KEYS
    requested_state = _safe_turn_state(state) if state is not None else None
    if state is not None and requested_state is None:
        return MissionChatTurnPersistOutcome.REJECTED_INVALID_STATE
    if state is None and not elements:
        return MissionChatTurnPersistOutcome.SKIPPED_EMPTY_LEGACY
    safe_elements = _safe_elements(elements)
    if state is None and not safe_elements:
        return MissionChatTurnPersistOutcome.SKIPPED_EMPTY_LEGACY

    def _mutate(session: dict[str, Any]) -> tuple[bool, MissionChatTurnPersistOutcome]:
        existing = session.get(message_key)
        resolved_state = next_turn_state(
            _record_state(existing),
            requested_state,
            write_ahead=write_ahead,
        )
        if resolved_state is None:
            return False, MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        # C8 turn-start anchor: stamped ONCE at write-ahead (the moment the turn
        # begins) and carried unchanged through every later flush/terminal
        # persist, so replay can order turns by when they STARTED instead of
        # when they settled. Records that predate the anchor never get one
        # retro-stamped — they fall back to settle-time order (honest fallback,
        # no fabricated keys).
        started_at = (
            safe_assignment_text((existing or {}).get("started_at"), limit=80)
            if isinstance(existing, dict)
            else None
        )
        if write_ahead and not started_at:
            started_at = _utc_now_iso()
        prior = dict(existing) if isinstance(existing, dict) else {}
        session[message_key] = {
            **prior,
            "schema_version": 2,
            "turn_id": safe_assignment_token(turn_id) or safe_assignment_token(message_key),
            "state": resolved_state,
            "updated_at": _utc_now_iso(),
            **({"started_at": started_at} if started_at else {}),
            "elements": safe_elements,
            **_safe_journal_metadata(metadata),
        }
        return True, MissionChatTurnPersistOutcome.PERSISTED

    return _mutate_session(
        session_key,
        _mutate,
        timeout_result=MissionChatTurnPersistOutcome.SKIPPED_LOCK_TIMEOUT,
        protected_message=message_key,
    )


def transition_mission_chat_turn(
    *,
    session_id: str | None,
    client_message_id: str | None,
    turn_id: str | None,
    state: str,
    metadata: dict[str, Any] | None = None,
    elements: list[dict[str, Any]] | None = None,
) -> MissionChatTurnPersistOutcome:
    """Durably advance the exactly-once persona-chat journal.

    The record is keyed by stable root plus client id and also carries the turn
    id.  Invalid/backwards transitions fail closed; callers may safely repeat a
    transition to its current state after a crash.
    """

    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    requested = _safe_turn_state(state)
    if not session_key or not message_key:
        return MissionChatTurnPersistOutcome.SKIPPED_NO_KEYS
    if requested not in JOURNAL_TURN_STATES:
        return MissionChatTurnPersistOutcome.REJECTED_INVALID_STATE

    def _mutate(session: dict[str, Any]) -> tuple[bool, MissionChatTurnPersistOutcome]:
        existing = session.get(message_key)
        current = _record_state(existing) if isinstance(existing, dict) else None
        if current in _LEGACY_TURN_STATES:
            current = {"running": "pending", "completed": "projected"}.get(current)
        if requested not in _JOURNAL_TRANSITIONS.get(current, set()):
            return False, MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        now_iso = _utc_now_iso()
        record = dict(existing) if isinstance(existing, dict) else {}
        if not record.get("started_at"):
            record["started_at"] = now_iso
        record.update(
            {
                "schema_version": 2,
                "turn_id": safe_assignment_token(turn_id) or safe_assignment_token(message_key),
                "state": requested,
                "updated_at": now_iso,
            }
        )
        if elements is not None:
            record["elements"] = _safe_elements(elements)
        else:
            record.setdefault("elements", [])
        record.update(_safe_journal_metadata(metadata))
        session[message_key] = record
        return True, MissionChatTurnPersistOutcome.PERSISTED

    return _mutate_session(
        session_key,
        _mutate,
        timeout_result=MissionChatTurnPersistOutcome.SKIPPED_LOCK_TIMEOUT,
        protected_message=message_key,
    )


def abandon_mission_chat_turn(
    *, session_id: str | None, client_message_id: str | None, turn_id: str | None
) -> MissionChatTurnPersistOutcome:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    exact_turn = safe_assignment_token(turn_id)
    if not session_key or not message_key or not exact_turn:
        return MissionChatTurnPersistOutcome.SKIPPED_NO_KEYS

    def _mutate(session: dict[str, Any]) -> tuple[bool, MissionChatTurnPersistOutcome]:
        existing = session.get(message_key)
        if not isinstance(existing, dict):
            return False, MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        if (
            _record_state(existing) != "outcome_unknown"
            or safe_assignment_token(existing.get("turn_id")) != exact_turn
        ):
            return False, MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        record = dict(existing)
        record.update(
            {
                "state": "abandoned",
                "updated_at": _utc_now_iso(),
                "resolved_at": _utc_now_iso(),
                "resolution": "abandon",
            }
        )
        session[message_key] = record
        return True, MissionChatTurnPersistOutcome.PERSISTED

    return _mutate_session(
        session_key,
        _mutate,
        timeout_result=MissionChatTurnPersistOutcome.SKIPPED_LOCK_TIMEOUT,
        protected_message=message_key,
    )


def mission_chat_turn_elements(
    *,
    session_id: str | None,
    client_message_id: str | None,
) -> list[dict[str, Any]]:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key:
        return []
    record = _read_session(session_key).get(message_key)
    if not isinstance(record, dict):
        return []
    return _safe_elements(record.get("elements"))


def mission_chat_turn_record(
    *,
    session_id: str | None,
    client_message_id: str | None,
) -> dict[str, Any] | None:
    session_key = safe_assignment_text(session_id, limit=240)
    message_key = safe_assignment_text(client_message_id, limit=240)
    if not session_key or not message_key:
        return None
    record = _read_session(session_key).get(message_key)
    if not isinstance(record, dict):
        return None
    return _safe_record(record, client_message_id=message_key)


def mission_chat_turn_records(
    *,
    session_id: str | None,
) -> list[dict[str, Any]]:
    session_key = safe_assignment_text(session_id, limit=240)
    if not session_key:
        return []
    raw_session = _read_session(session_key)
    if not isinstance(raw_session, dict):
        return []
    records: list[dict[str, Any]] = []
    for message_key, record in raw_session.items():
        if not isinstance(record, dict):
            continue
        safe_key = safe_assignment_text(message_key, limit=240)
        if not safe_key:
            continue
        safe_record = _safe_record(record, client_message_id=safe_key)
        if safe_record is not None:
            records.append(safe_record)
    # C8 replay order: turn START, not `updated_at` settle time — a long turn
    # that settles after a quick later one must not replay after it (F17 seam
    # d). `started_at` is stamped at write-ahead; records that predate the
    # anchor fall back to their settle time (pre-C8 behavior, honest fallback).
    return sorted(
        records,
        key=lambda item: (
            str(item.get("started_at") or item.get("updated_at") or ""),
            str(item.get("client_message_id") or ""),
        ),
    )


def mark_stale_running_turns_interrupted(
    *,
    session_id: str | None,
    active_client_message_id: str | None,
) -> list[str]:
    session_key = safe_assignment_text(session_id, limit=240)
    active_key = safe_assignment_text(active_client_message_id, limit=240)
    if not session_key:
        return []

    def _mutate(session: dict[str, Any]) -> tuple[bool, list[str]]:
        flipped: list[str] = []
        now_iso = _utc_now_iso()
        for message_key, record in session.items():
            safe_key = safe_assignment_text(message_key, limit=240)
            if not safe_key or safe_key == active_key or not isinstance(record, dict):
                continue
            if _record_state(record) != "running":
                continue
            record["state"] = "interrupted"
            record["updated_at"] = now_iso
            flipped.append(safe_key)
        return bool(flipped), flipped

    # Lock timeout returns [] — this repair is opportunistic by design
    # (repair-on-next-write); the next send in the session retries it.
    return _mutate_session(
        session_key,
        _mutate,
        timeout_result=[],
        protected_message=active_key,
    )


def _mutate_session(
    session_key: str,
    mutator: Callable[[dict[str, Any]], tuple[bool, _T]],
    *,
    timeout_result: _T,
    protected_message: str | None = None,
) -> _T:
    """Single write chokepoint for one chat session's turn file.

    Holds that session's exclusive cross-process file lock for the whole
    read-modify-write window so concurrent CLI turns in the SAME chat can never
    lose each other's records — while turns in DIFFERENT chats take different
    locks and never contend. On lock timeout the mutation is skipped and
    ``timeout_result`` is returned — a chat turn must never hang on a stuck
    lock; the skip is surfaced through the typed persist outcome.

    Every changed write applies the per-session turn cap (same lock, same
    atomic tmp-replace). When the write CREATES a new session file, a
    best-effort directory GC bounds the session count (see
    ``_gc_session_files``). Retention never evicts ``running`` records or the
    protected record being written, and it is invisible to the mutator result.
    """

    _migrate_legacy_if_present()
    path = _session_file_path(session_key)
    changed = False
    created = False
    with _file_lock(_session_lock_path(session_key)) as acquired:
        if not acquired:
            return timeout_result
        created = not path.exists()
        session = _read_session_map(path)
        changed, result = mutator(session)
        if changed:
            _apply_session_turn_cap(session, protected_message=protected_message)
            _write_session_file(path, session)
    # Bound the session-file count only when a new session file appeared — the
    # only moment the directory can grow. Runs OUTSIDE the session lock (it
    # takes the GC lock + non-blocking per-candidate locks; the just-written
    # session is protected), so the hot per-flush path (rewrites of an existing
    # session file) never pays for a directory scan.
    if changed and created:
        _gc_session_files(protected_session_key=session_key)
    return result


def _apply_session_turn_cap(
    session: dict[str, Any],
    *,
    protected_message: str | None = None,
) -> None:
    """Per-session tail bound, applied in place inside the session lock.

    Keep the ``_RETENTION_MAX_TURNS_PER_SESSION`` most recent records by
    ``updated_at``. ``running`` records are never evicted (a live turn must not
    lose its write-ahead marker) and neither is the record being written, so a
    session can transiently exceed its bound rather than lose live state.
    """

    excess = len(session) - _RETENTION_MAX_TURNS_PER_SESSION
    if excess <= 0:
        return
    evictable = sorted(
        (
            str(record.get("updated_at") or "") if isinstance(record, dict) else "",
            str(message_key),
        )
        for message_key, record in session.items()
        if not (
            str(message_key) == str(protected_message)
            or _record_state(record) in {"running", "pending", "executing", "outcome_unknown"}
        )
    )
    for _, message_key in evictable[:excess]:
        session.pop(message_key, None)


def _gc_session_files(*, protected_session_key: str | None = None) -> None:
    """Bound the number of session FILES on disk (archive-never-delete).

    When ``mission_chat_turns/`` exceeds ``_RETENTION_MAX_SESSIONS`` live files,
    move the oldest-updated session files under ``mission_chat_turns_archive/``.
    Never archives the protected session (the one just written) nor any file
    holding a ``running`` record (a live concurrent turn). Best-effort by
    design: it runs under its own GC lock and probes each candidate's session
    lock non-blocking, so it can never deadlock against a live write and simply
    skips (and retries on the next new-session write) whatever it cannot safely
    claim. Any failure is swallowed — retention is invisible to the caller.
    """

    try:
        if len(_iter_session_files()) <= _RETENTION_MAX_SESSIONS:
            return
        protected_path = (
            _session_file_path(protected_session_key) if protected_session_key else None
        )
        with _file_lock(_gc_lock_path(), timeout_seconds=_GC_LOCK_TIMEOUT_SECONDS) as acquired:
            if not acquired:
                return
            files = _iter_session_files()
            excess = len(files) - _RETENTION_MAX_SESSIONS
            if excess <= 0:
                return
            dropped = 0
            for path in sorted(files, key=lambda item: (_session_file_recency(item), item.name)):
                if dropped >= excess:
                    break
                if protected_path is not None and path == protected_path:
                    continue
                with _file_lock(_lock_path_for_session_file(path), timeout_seconds=0.0) as got:
                    if not got:
                        continue
                    session = _read_session_map(path)
                    if any(
                        _record_state(record) in {"running", "pending", "executing", "outcome_unknown"}
                        for record in session.values()
                    ):
                        continue
                    if _archive_session_file(path):
                        dropped += 1
    except Exception:
        return


def _migrate_legacy_if_present() -> None:
    """Split the legacy monolith into per-session files ONCE, crash-safely.

    A single stat fast-paths the common case (no monolith). When the monolith
    exists, the split runs under a global migration lock so two processes never
    race it. For each legacy session, a per-session file is written ONLY when it
    does not already exist — so a per-session file written by a live turn (or by
    a partially-completed prior migration) is authoritative and never clobbered.
    The monolith is then renamed aside (kept, not deleted). This is idempotent
    and converges from a half-migrated state: a re-run skips the files already
    split, writes the rest, and completes the rename.
    """

    legacy = _legacy_store_path()
    try:
        if not legacy.exists():
            return
    except OSError:
        return
    with _file_lock(_migrate_lock_path(), timeout_seconds=_MIGRATE_LOCK_TIMEOUT_SECONDS) as acquired:
        if not acquired:
            return
        try:
            if not legacy.exists():
                return
        except OSError:
            return
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            _store_dir().mkdir(parents=True, exist_ok=True)
            for raw_session_key, session_map in data.items():
                if not isinstance(session_map, dict):
                    continue
                session_key = safe_assignment_text(raw_session_key, limit=240)
                if not session_key:
                    continue
                target = _session_file_path(session_key)
                if target.exists():
                    # Authoritative per-session file already present (live write
                    # or a prior partial migration) — never clobber it.
                    continue
                _write_session_file(target, session_map)
        # Rename the monolith aside even if it was unreadable, so migration
        # always converges instead of re-attempting a corrupt file forever.
        try:
            os.replace(str(legacy), str(_migrated_legacy_path()))
        except OSError:
            pass
    # A legacy store could carry more sessions than the current bound; enforce
    # it once after the split (best-effort, nothing is live under the migration
    # lock we just released).
    _gc_session_files(protected_session_key=None)


# ---------------------------------------------------------------------------
# Cross-process file lock (per session, plus migrate/GC coordination locks)
# ---------------------------------------------------------------------------


@contextmanager
def _file_lock(
    lock_path: Path,
    timeout_seconds: float | None = None,
) -> Iterator[bool]:
    if timeout_seconds is None:
        timeout_seconds = _LOCK_TIMEOUT_SECONDS
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    acquired = False
    try:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            try:
                _lock_fd_exclusive_nonblocking(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        yield acquired
    finally:
        if acquired:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
        os.close(fd)


def _lock_fd_exclusive_nonblocking(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Paths + per-file I/O
# ---------------------------------------------------------------------------


def _store_dir() -> Path:
    return paths.store_root() / _STORE_DIR_NAME


def _archive_dir() -> Path:
    return paths.store_root() / _ARCHIVE_DIR_NAME


def _legacy_store_path() -> Path:
    return paths.store_root() / _LEGACY_STORE_NAME


def _migrated_legacy_path() -> Path:
    return paths.store_root() / _LEGACY_MIGRATED_NAME


def _migrate_lock_path() -> Path:
    return paths.store_root() / _MIGRATE_LOCK_NAME


def _gc_lock_path() -> Path:
    return paths.store_root() / _GC_LOCK_NAME


def _session_filename_stem(session_key: str) -> str:
    """Deterministic, filesystem-safe, collision-free file stem for a session.

    A sanitized prefix keeps the file human-recognizable; a sha256 suffix over
    the exact (already length-bounded) session key guarantees two keys that
    sanitize to the same prefix still land in distinct files.
    """

    sanitized = "".join(
        ch if (ch.isalnum() or ch in "_.-") else "_" for ch in session_key
    ).strip("._-")[:_SESSION_KEY_PREFIX_MAX]
    digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:_SESSION_KEY_HASH_LEN]
    return f"{sanitized or 'session'}_{digest}"


def _session_file_path(session_key: str) -> Path:
    return _store_dir() / f"{_session_filename_stem(session_key)}{_SESSION_FILE_SUFFIX}"


def _session_lock_path(session_key: str) -> Path:
    return _store_dir() / f"{_session_filename_stem(session_key)}{_SESSION_LOCK_SUFFIX}"


def _lock_path_for_session_file(path: Path) -> Path:
    return path.with_name(path.name[: -len(_SESSION_FILE_SUFFIX)] + _SESSION_LOCK_SUFFIX)


def _iter_session_files() -> list[Path]:
    store_dir = _store_dir()
    try:
        return sorted(p for p in store_dir.glob(f"*{_SESSION_FILE_SUFFIX}") if p.is_file())
    except OSError:
        return []


def _read_session(session_key: str) -> dict[str, Any]:
    """Read one session's ``{client_message_id: record}`` map (migrate first)."""
    _migrate_legacy_if_present()
    return _read_session_map(_session_file_path(session_key))


def _read_session_map(path: Path) -> dict[str, Any]:
    """Pure single-file read — never triggers migration (used by GC/migration)."""
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_session_file(path: Path, session: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(session, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _session_file_recency(path: Path) -> tuple[str, float]:
    """Rank key for eviction (oldest first): newest ``updated_at`` in the file,
    falling back to the file mtime when the file carries no timestamps."""
    session = _read_session_map(path)
    recency = max(
        (
            str(record.get("updated_at") or "")
            for record in session.values()
            if isinstance(record, dict)
        ),
        default="",
    )
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (recency, mtime)


def _archive_session_file(path: Path) -> bool:
    """Move a session file under the archive dir (archive-never-delete).

    Returns True on success. A name collision (a session evicted, recreated,
    evicted again) is preserved with a nanosecond suffix rather than clobbered.
    A concurrent GC that already moved the file yields FileNotFoundError → False.
    """
    try:
        archive_dir = _archive_dir()
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / path.name
        if target.exists():
            target = archive_dir / f"{path.stem}.{time.time_ns()}{path.suffix}"
        os.replace(str(path), str(target))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Record / element normalization (unchanged from the monolith design)
# ---------------------------------------------------------------------------


def _safe_record(
    record: dict[str, Any],
    *,
    client_message_id: str,
) -> dict[str, Any] | None:
    turn_id = safe_assignment_token(record.get("turn_id")) or safe_assignment_token(client_message_id)
    if not turn_id:
        return None
    started_at = safe_assignment_text(record.get("started_at"), limit=80)
    safe = {
        "client_message_id": client_message_id,
        "turn_id": turn_id,
        "state": _record_state(record) or "completed",
        "updated_at": safe_assignment_text(record.get("updated_at"), limit=80),
        # C8 turn-start anchor (write-ahead stamp). Absent on pre-C8 records —
        # consumers fall back to `updated_at`, never fabricate a start.
        **({"started_at": started_at} if started_at else {}),
        "elements": _safe_elements(record.get("elements")),
    }
    safe.update(_safe_journal_metadata(record))
    return safe


_JOURNAL_TEXT_FIELDS = {
    "root_chat_session_id": 240,
    "active_session_id": 240,
    "persona_instance_id": 200,
    "provider_request_id": 240,
    "provider_request_fingerprint": 128,
    "native_revision": 160,
    "native_assistant_message_id": 240,
    "stored_reply": _MAX_TEXT,
    "projection_revision": 160,
    "resolution": 80,
    "resolved_at": 80,
    "pending_user_message": 12000,
}


def _safe_journal_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, limit in _JOURNAL_TEXT_FIELDS.items():
        text = safe_assignment_text(value.get(key), limit=limit)
        if text:
            result[key] = text
    for key in ("provider_submitted", "native_committed", "projection_committed"):
        if key in value:
            result[key] = bool(value.get(key))
    return result


def _safe_turn_state(value: Any) -> str | None:
    state = safe_assignment_token(value)
    return state if state in _VALID_TURN_STATES else None


def _record_state(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    state = _safe_turn_state(record.get("state"))
    return state or "completed"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_elements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    elements: list[dict[str, Any]] = []
    for raw in value[:_MAX_ELEMENTS]:
        if not isinstance(raw, dict):
            continue
        kind = safe_assignment_token(raw.get("kind"))
        element_id = safe_assignment_text(raw.get("id"), limit=240)
        turn_id = safe_assignment_token(raw.get("turn_id"))
        try:
            seq = int(raw.get("seq"))
        except Exception:
            continue
        if kind not in {"segment", "tool"} or not element_id or not turn_id:
            continue
        base: dict[str, Any] = {
            "kind": kind,
            "id": element_id,
            "turn_id": turn_id,
            "seq": seq,
            "state": safe_assignment_token(raw.get("state")) or "settled",
        }
        if kind == "segment":
            base.update(
                {
                    "seg_type": safe_assignment_token(raw.get("seg_type")) or "answer",
                    "text": safe_assignment_text(raw.get("text"), limit=_MAX_TEXT) or "",
                    "ttft_ms": _safe_int(raw.get("ttft_ms")),
                    "duration_ms": _safe_int(raw.get("duration_ms")),
                    "redacted": bool(raw.get("redacted")),
                }
            )
        else:
            files = raw.get("files")
            safe_files = [_safe_file_label(item) for item in files[:20]] if isinstance(files, list) else []
            base.update(
                {
                    "name": safe_assignment_token(raw.get("name")) or "tool",
                    "args": safe_assignment_text(raw.get("args"), limit=800),
                    "command": safe_assignment_text(raw.get("command"), limit=1000),
                    "status": safe_assignment_token(raw.get("status")) or None,
                    "summary": safe_assignment_text(raw.get("summary"), limit=1200),
                    "detail": safe_assignment_text(raw.get("detail"), limit=1200),
                    "output": safe_assignment_text(raw.get("output"), limit=_MAX_TEXT),
                    "exit_code": _safe_exit_code(raw.get("exit_code")),
                    "duration_ms": _safe_int(raw.get("duration_ms")),
                    "files": [item for item in safe_files if item],
                    "redacted": bool(raw.get("redacted")),
                }
            )
            # T7: preserve the todo tool's structured checklist (id/content/status)
            # so the operator console can render it after the turn persists. Bounded
            # again here (defence in depth over the producer cap).
            # T9d: keep an explicit EMPTY list too (`is not None`, not truthiness) —
            # a cleared checklist persists as `todo_state: []` so a reloaded turn
            # clears the panel exactly like the live lane. `_safe_todo_state`
            # returns None only for a truly absent/non-list value, so non-todo
            # elements still gain no key.
            todo_state = _safe_todo_state(raw.get("todo_state"))
            if todo_state is not None:
                base["todo_state"] = todo_state
        elements.append(base)
    return sorted(elements, key=lambda item: (int(item.get("seq") or 0), str(item.get("id") or "")))


# Turn-store caps for the T7 todo checklist. Compact by design — a checklist row
# is a short line and the elements ride the snapshot frame; the producer already
# bounds this, and this is the defence-in-depth boundary for foreign/legacy rows.
_TODO_STATE_MAX_ITEMS = 64
_TODO_STATE_MAX_CONTENT = 240
_TODO_STATE_VALID_STATUS = {"pending", "in_progress", "completed", "cancelled"}


def _safe_todo_state(value: Any) -> list[dict[str, str]] | None:
    """Bounded, validated todo checklist for persistence.

    Keeps only ``{id, content, status}`` per item, caps item count and content
    length, and normalises unknown statuses to ``pending``. Returns ``None`` only
    when ``value`` is not a list (absent / non-todo element), so a non-todo
    element never gains the key.

    T9d: a list ``value`` — including an explicit empty one — returns a list
    (possibly ``[]``). The empty list is the cleared-checklist signal and must
    survive persistence so a reloaded turn clears the panel exactly like the live
    lane; ``items or None`` would have collapsed it back to absence."""

    if not isinstance(value, list):
        return None
    items: list[dict[str, str]] = []
    for raw in value[:_TODO_STATE_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        item_id = safe_assignment_text(raw.get("id"), limit=120) or "?"
        content = safe_assignment_text(raw.get("content"), limit=_TODO_STATE_MAX_CONTENT) or "(no description)"
        status = str(raw.get("status") or "").strip().lower()
        if status not in _TODO_STATE_VALID_STATUS:
            status = "pending"
        items.append({"id": item_id, "content": content, "status": status})
    return items


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed >= 0 else None


def _safe_exit_code(value: Any) -> int | None:
    # Exit codes can be negative (signal terminations), so unlike _safe_int we
    # keep the sign; just bound it to a sane range.
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if -256 <= parsed <= 256 else None


def _safe_file_label(value: Any) -> str | None:
    text = safe_assignment_text(value, limit=240)
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_FILE_MARKERS):
        return None
    return text
