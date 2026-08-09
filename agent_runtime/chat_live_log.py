"""Live, greppable mirror of a persona-chat transcript.

Why this exists (operator ask, 2026-08-03)
------------------------------------------
A head agent (Neko Mission Lead) could not see a teammate thread's full
history: ``agent_chat_open`` caps at a 40-message tail and the transcript lives
in SessionDB, which no agent tool can grep or glob. The ask was explicitly for a
**live** artifact, not a point-in-time export — a path the head can ``grep`` /
``tail`` *while the teammate is mid-task* and watch it grow.

So this module maintains one append-only JSONL file per chat session:

    <head-home>/chat_live_logs/<session_id>.jsonl

Line kinds, all bounded:

    {"ts": ..., "kind": "message", "role": "operator|agent|system",
     "text": ..., "turn_id"?: ..., "client_message_id"?: ...,
     "relay_sender_persona_id"?: ..., "relay_sender_instance_id"?: ...}
    {"ts": ..., "kind": "tool", "tool": ..., "status": ..., "turn_id"?: ...}

plus a ``{"kind": "log_opened"}`` header written when the file is created,
carrying the backfill state (see below).

WHAT THE LIVE STREAM ACTUALLY CARRIES (honesty boundary — do not overstate)
--------------------------------------------------------------------------
Mirrored **as they happen**:

* the incoming operator/relay message of each mission-chat turn (write-ahead),
* text steered into a turn that is already running
  (``mission_chat_steer``, stamped ``steered: true``),
* the recorded final reply of each mission-chat turn (native commit),
* every row the explicit persona-chat append seam writes — the relay lane
  and the child return-summary lane,
* tool start/finish lines from :class:`~agent_runtime.progress.ChatProgressSink`.

NOT mirrored as they happen: the runtime's NON-FINAL rows — the intermediate
assistant messages between tool calls that the native session flush persists
(``run_agent`` writes those straight to SessionDB, outside every seam this
module hooks). They are in SessionDB and therefore appear when a file is built
or completed from the projection, so never claim the live tail is a complete
transcript. ``agent_chat_log_path``'s schema states the same boundary to the
model.

Contract
--------
* **Regenerable artifact, never authority.** SessionDB remains the transcript
  of record; every line here is rebuildable from it. Deleting a mirror file
  loses nothing but convenience — the next ``agent_chat_log_path`` call
  materializes it again from the projection.
* **Backfill happens on the TOOL lane, never the chat hot path.** The one-shot
  materialization re-runs the curated projection (which re-parses the turn
  journal per row), so doing it synchronously inside a chat persist seam would
  stall a live turn for seconds on a long session. The persist / progress lanes
  therefore only ever create a header (``backfill_pending``) and append; the
  deliberate ``agent_chat_log_path`` request fills the history in.
* **Publication is atomic.** Materialization writes a claim/temp file and
  ``os.replace()``s it into position, so a concurrent reader or appender sees
  either no file or a complete one — and an appended line is never overwritten
  by the creator's next buffered flush.
* **Redaction-safe by construction.** Every text this module writes goes
  through :data:`~agent_runtime.redaction.TEXT_SECRET_ASSIGNMENT_RE` — the ONE
  secret-assignment authority — with per-line masking, exactly like the read
  projection. Callers already hand over redacted text; the pass here is
  belt-and-braces, because "the caller already did it" is how a redaction
  boundary rots.
* **Best effort, counted.** A mirror write must NEVER fail a chat turn. IO
  failures are swallowed but tallied (:func:`chat_live_log_failures`) and
  logged ONCE per process, so a silently broken mirror is still discoverable.
* **Bounded.** Per-line text cap :data:`LIVE_LOG_TEXT_LIMIT`; size-capped
  rotation at :data:`LIVE_LOG_ROTATE_BYTES` into a single ``.1`` sibling (one
  generation kept — this is a convenience mirror, not an archive).

THE HERMES_HOME TRAP (read before touching the path resolution)
---------------------------------------------------------------
``agent_runtime.profile_context.persona_profile_context`` flips
``os.environ["HERMES_HOME"]`` **process-globally** to ``profiles/<persona>``
for the duration of a persona turn — which is precisely when these writes
happen. Resolving the directory from the ambient home at write time would
scatter one conversation's mirror across per-persona profile directories, away
from the SessionDB that actually holds the transcript.

So the root is **captured once** per process
(:func:`capture_chat_live_log_root`) and reused for every later write:

1. the directory of the chat ``SessionDB`` handed to the persist seam
   (``session_db.db_path.parent``) — the strongest answer, because it is
   literally where the transcript this mirrors landed;
2. otherwise ``chat_session_scope.resolve_chat_session_scope().head_home`` —
   the same head-home ladder the SessionDB acquisition itself uses.

A capture from a real ``session_db`` upgrades an earlier scope-derived capture
exactly once; nothing else re-resolves. ``reset_chat_live_log_state()`` exists
for tests only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from hermes_time import now

from .redaction import TEXT_SECRET_ASSIGNMENT_RE

logger = logging.getLogger(__name__)

__all__ = [
    "CHAT_LIVE_LOG_DIRNAME",
    "LIVE_LOG_BACKFILL_MESSAGE_CAP",
    "LIVE_LOG_BACKFILL_WALL_SECONDS",
    "LIVE_LOG_ROTATE_BYTES",
    "LIVE_LOG_TEXT_LIMIT",
    "capture_chat_live_log_root",
    "chat_live_log_failures",
    "chat_live_log_path",
    "chat_live_log_root",
    "chat_live_log_stats",
    "ensure_chat_live_log",
    "mirrored_persona_chat_append",
    "record_chat_message",
    "record_chat_tool",
    "reset_chat_live_log_state",
]

#: Directory name under the resolved head home.
CHAT_LIVE_LOG_DIRNAME = "chat_live_logs"
_LOG_SUFFIX = ".jsonl"
#: Per-line text cap. Matches the ``agent_chat_send`` reply bound so one mirror
#: line can never be larger than the largest reply the lane will hand back.
LIVE_LOG_TEXT_LIMIT = 8000
#: Rotate at ~10MB into a single ``.1`` sibling.
LIVE_LOG_ROTATE_BYTES = 10 * 1024 * 1024
#: Upper bound on rows materialized by the backfill (tool lane only).
LIVE_LOG_BACKFILL_MESSAGE_CAP = 4000
#: Wall budget for that materialization. The tool lane is a deliberate agent
#: request so it can afford seconds — but never unbounded: a pathological
#: session must not hang the caller's tool call.
LIVE_LOG_BACKFILL_WALL_SECONDS = 15.0
#: Same masking vocabulary the read projection uses.
_REDACTED_LINE = "[redacted line — contained a secret]"
#: How much of an existing file the replay-dedupe index seeds from. Replays only
#: ever re-offer recent turns, so a bounded tail scan is enough and a 10MB file
#: never has to be read whole on a hot path.
_DEDUPE_TAIL_BYTES = 256 * 1024
#: Suffix of the claim/temp file a materialization publishes from.
_CLAIM_SUFFIX = ".materializing"
#: How long an appender waits for someone else's materialization to publish.
_CLAIM_WAIT_SECONDS = 1.0
#: A claim older than this belonged to a process that died mid-write.
_CLAIM_STALE_SECONDS = 120.0

_state_lock = threading.RLock()
_captured_root: Path | None = None
_captured_source: str | None = None
_seen_keys: dict[str, set[tuple[str, str]]] = {}
_seeded_sessions: set[str] = set()
_failures = 0
_failure_logged = False


# ── root capture ────────────────────────────────────────────────────────────


def capture_chat_live_log_root(*, session_db: Any = None, head_home: Any = None) -> Path | None:
    """Capture (once) the directory this process mirrors into.

    Safe to call repeatedly and from any lane; the first successful capture
    wins, except that a ``session_db``-derived root upgrades a scope-derived one
    (the database's own directory is the stronger answer). Never raises.
    """

    global _captured_root, _captured_source

    candidate: Path | None = None
    source = ""
    if head_home is not None:
        candidate = _coerce_dir(head_home)
        source = "explicit"
    if candidate is None and session_db is not None:
        candidate = _root_from_session_db(session_db)
        source = "session_db"
    if candidate is None:
        with _state_lock:
            if _captured_root is not None:
                return _captured_root
        candidate = _root_from_scope()
        source = "chat_session_scope"
    if candidate is None:
        return None

    with _state_lock:
        if _captured_root is None or (
            source in {"explicit", "session_db"} and _captured_source == "chat_session_scope"
        ):
            _captured_root = candidate / CHAT_LIVE_LOG_DIRNAME
            _captured_source = source
        return _captured_root


def chat_live_log_root() -> Path | None:
    """The captured mirror directory, capturing it now if nothing has yet."""

    with _state_lock:
        if _captured_root is not None:
            return _captured_root
    return capture_chat_live_log_root()


def chat_live_log_path(session_id: Any, *, session_db: Any = None) -> Path | None:
    """Where *session_id*'s mirror lives. Does not create anything."""

    token = _safe_session_token(session_id)
    if not token:
        return None
    root = capture_chat_live_log_root(session_db=session_db)
    if root is None:
        return None
    return root / f"{token}{_LOG_SUFFIX}"


# ── writes (hot lane) ───────────────────────────────────────────────────────


@contextlib.contextmanager
def mirrored_persona_chat_append(
    *,
    session_db: Any = None,
    session_id: Any,
    role: Any,
    text: Any,
    client_message_id: Any = None,
    turn_id: Any = None,
    relay_marker: Any = None,
) -> Iterator[None]:
    """Wrap a durable persona-chat append so its mirror line rides the write.

    THE seam for "a row was explicitly appended to a persona-chat session".
    Every such site wraps its ``session_db.append_message`` in this, so a new
    append site cannot land a row that is invisible in the live log — which is
    exactly what happened to the child return-summary lane
    (``agent_runtime.continuity``) while the mirror was hooked by call-site
    convention instead of by a seam.

    Records only on a clean exit: a failed durable write leaves the mirror
    silent, because the mirror must never claim a message the transcript of
    record rejected.
    """

    yield
    record_chat_message(
        session_id=session_id,
        role=role,
        text=text,
        turn_id=turn_id,
        client_message_id=client_message_id,
        relay_marker=relay_marker,
        session_db=session_db,
    )


def record_chat_message(
    *,
    session_id: Any,
    role: Any,
    text: Any,
    turn_id: Any = None,
    client_message_id: Any = None,
    relay_marker: Any = None,
    steered: bool = False,
    session_db: Any = None,
) -> bool:
    """Append one persisted chat message to the live mirror.

    Idempotent for a given ``(role, logical client_message_id)`` pair, which is
    what makes the replay / resend lanes safe to route through here without
    doubling rows. The key is the LOGICAL turn id: the runtime's native flush
    stamps assistant rows ``<client_message_id>:assistant:<n>`` while the live
    hooks carry the bare id, so a raw comparison would let a materialized file
    and a live append both claim the same reply.
    """

    token = _safe_session_token(session_id)
    if not token:
        return False
    safe_text = _mirror_text(text)
    if not safe_text:
        return False
    path = ensure_chat_live_log(token, session_db=session_db)
    if path is None:
        return False

    normalized_role = _normalized_role(role)
    client_key = _logical_client_key(client_message_id)
    if client_key and _already_recorded(token, path, (normalized_role, client_key)):
        return True

    payload: dict[str, Any] = {
        "ts": _now_iso(),
        "kind": "message",
        "role": normalized_role,
        "text": safe_text,
    }
    turn_token = _safe_token(turn_id, limit=240)
    if turn_token:
        payload["turn_id"] = turn_token
    if client_key:
        payload["client_message_id"] = client_key
    if steered:
        # Typed, not inferred from position: a steer is text injected into a
        # turn ALREADY RUNNING, so a reader must be able to tell it from the
        # order that opened the turn.
        payload["steered"] = True
    payload.update(_relay_sender_fields(relay_marker))
    if not _append_line(path, payload):
        return False
    if client_key:
        _mark_recorded(token, (normalized_role, client_key))
    return True


def record_chat_tool(
    *,
    session_id: Any,
    tool: Any,
    status: Any,
    turn_id: Any = None,
    session_db: Any = None,
) -> bool:
    """Append one compact tool-activity line.

    This is what makes the mirror answer "what is it doing RIGHT NOW" instead of
    only "what did it say" — a head agent tailing the file during a long
    teammate turn sees tool starts/finishes land as they happen.
    """

    token = _safe_session_token(session_id)
    if not token:
        return False
    tool_name = _safe_token(tool, limit=120)
    if not tool_name:
        return False
    path = ensure_chat_live_log(token, session_db=session_db)
    if path is None:
        return False
    payload: dict[str, Any] = {
        "ts": _now_iso(),
        "kind": "tool",
        "tool": tool_name,
        "status": _safe_token(status, limit=60) or "unknown",
    }
    turn_token = _safe_token(turn_id, limit=240)
    if turn_token:
        payload["turn_id"] = turn_token
    return _append_line(path, payload)


# ── file lifecycle: create cheap, materialize deliberately ──────────────────


def ensure_chat_live_log(
    session_id: Any, *, session_db: Any = None, materialize: bool = False
) -> Path | None:
    """Return the mirror path, creating (and optionally materializing) it.

    ``materialize=False`` is the CHAT HOT PATH: a missing file is created with
    nothing but a ``backfill_pending`` header. O(1) — no projection read, no
    turn-journal parsing — so a chat turn is never taxed by the size of the
    thread it belongs to.

    ``materialize=True`` is the TOOL lane (``agent_chat_log_path``): a missing
    file is built from the SAME projection ``agent_chat_open`` reads
    (``persona_chat_session_messages``, which redacts at read), and a file whose
    header still says ``backfill_pending`` gets its history filled in — once —
    ahead of the live lines it already holds.

    Both paths publish through a claim file + ``os.replace``, so a concurrent
    appender sees either no file or a complete one and never has its own line
    overwritten by a creator's later buffered flush.
    """

    path = chat_live_log_path(session_id, session_db=session_db)
    if path is None:
        return None
    token = _safe_session_token(session_id) or ""

    if _exists(path):
        if not materialize or not _backfill_pending(path):
            return path
        completed = _with_claim(
            path,
            lambda claim: _complete_backfill(
                path, claim, session_id=token, session_db=session_db
            ),
        )
        return completed or path

    published = _with_claim(
        path,
        lambda claim: _create_log(
            path, claim, session_id=token, session_db=session_db, materialize=materialize
        ),
    )
    if published is not None:
        return published
    return path if _exists(path) else None


def _with_claim(path: Path, work) -> Path | None:
    """Run *work* under an exclusive, cross-process claim on *path*.

    The claim file IS the temp file the work publishes from, so "a
    materialization is in flight" and "where the half-built content lives" are
    ONE fact rather than two that can disagree. ``O_EXCL`` is the cross-process
    gate; a claim older than :data:`_CLAIM_STALE_SECONDS` belonged to a process
    that died mid-write and is reclaimed rather than blocking this session's
    mirror forever.
    """

    claim = path.with_name(path.name + _CLAIM_SUFFIX)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _note_failure("mkdir", exc)
        return None
    handle: int | None = None
    for attempt in (0, 1):
        try:
            handle = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # A live claim means somebody else is publishing: wait for it. A
            # STALE claim means its owner died mid-write — clear it and take
            # the work over, exactly once, so one crash cannot strand this
            # session's mirror permanently.
            if attempt == 0 and _claim_is_stale(claim):
                _unlink_quietly(claim)
                continue
            return _wait_for_publication(path, claim)
        except OSError as exc:
            _note_failure("claim", exc)
            return None
    if handle is None:  # pragma: no cover - defensive
        return _wait_for_publication(path, claim)
    os.close(handle)
    try:
        return work(claim)
    finally:
        _unlink_quietly(claim)


def _create_log(
    path: Path,
    claim: Path,
    *,
    session_id: str,
    session_db: Any,
    materialize: bool,
) -> Path | None:
    if _exists(path):  # someone published while we were claiming
        return path
    rows: list[dict[str, Any]] = []
    truncated = False
    if materialize:
        rows, truncated = _backfill_rows(session_id, session_db=session_db)
    header = {
        "ts": _now_iso(),
        "kind": "log_opened",
        "session_id": session_id,
        "backfilled": len(rows),
        # The honesty flag the tool lane keys on: history has NOT been
        # materialized, so this file starts mid-conversation.
        "backfill_pending": not materialize,
    }
    if truncated:
        header["backfill_truncated"] = True
    return _publish(path, claim, [header] + rows)


def _complete_backfill(
    path: Path, claim: Path, *, session_id: str, session_db: Any
) -> Path | None:
    """Fill history in ahead of the live lines a hot-path-created file holds."""

    try:
        with open(path, "rb") as handle:
            existing = handle.read()
    except OSError as exc:
        _note_failure("read_existing", exc)
        return None
    carried = [row for row in _decode_lines(existing) if row.get("kind") != "log_opened"]
    rows, truncated = _backfill_rows(session_id, session_db=session_db)
    # A live line and a materialized row can name the same message (the row was
    # persisted, mirrored live, and is now also in the projection). The live
    # line wins — it is the one already handed to whoever is tailing the file.
    live_keys = {
        (_normalized_role(row.get("role")), str(row.get("client_message_id") or ""))
        for row in carried
        if row.get("kind") == "message" and row.get("client_message_id")
    }
    rows = [
        row
        for row in rows
        if not row.get("client_message_id")
        or (_normalized_role(row.get("role")), str(row["client_message_id"])) not in live_keys
    ]
    header = {
        "ts": _now_iso(),
        "kind": "log_opened",
        "session_id": session_id,
        "backfilled": len(rows),
        "backfill_pending": False,
    }
    if truncated:
        header["backfill_truncated"] = True
    # Second pass over anything appended while the projection was being read.
    # This shrinks the lost-append window from "however long the projection
    # takes" to "however long one seek+read takes"; it does not eliminate it,
    # and the file is regenerable either way.
    try:
        with open(path, "rb") as handle:
            handle.seek(len(existing))
            late = handle.read()
    except OSError:
        late = b""
    return _publish(path, claim, [header] + rows + carried + _decode_lines(late))


def _publish(path: Path, claim: Path, lines: list[dict[str, Any]]) -> Path | None:
    """Write *lines* to the claim file and atomically move it into position."""

    try:
        with open(claim, "w", encoding="utf-8", newline="\n") as handle:
            for payload in lines:
                handle.write(_encode(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, ValueError) as exc:
        _note_failure("materialize", exc)
        return None
    try:
        os.replace(claim, path)
    except OSError as exc:
        _note_failure("publish", exc)
        return None
    with _state_lock:
        # The file's identity changed underneath any cached dedupe index.
        _seeded_sessions.discard(path.stem)
        _seen_keys.pop(path.stem, None)
    return path


def _wait_for_publication(path: Path, claim: Path) -> Path | None:
    """Bounded wait for another process's materialization to land.

    An appender must not write into a file that is about to be replaced, and it
    must not give up so eagerly that a first-touch message is dropped. One
    second covers a hot-path create (which reads nothing); a long tool-lane
    materialization falls through and the caller simply skips this line —
    without marking it recorded, so the next attempt still writes it.
    """

    deadline = time.monotonic() + _CLAIM_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _exists(path):
            return path
        if not _exists(claim):
            break
        time.sleep(0.02)
    return path if _exists(path) else None


def _backfill_pending(path: Path) -> bool:
    """Does this file's header still say history was never materialized?"""

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return False
    try:
        row = json.loads(first)
    except ValueError:
        return False
    return bool(isinstance(row, dict) and row.get("backfill_pending"))


def _backfill_rows(
    session_id: str, *, session_db: Any = None
) -> tuple[list[dict[str, Any]], bool]:
    """Materialize history from the projection. Returns ``(rows, truncated)``.

    Bounded on BOTH axes. Each page re-runs the full curated projection (which
    re-parses the turn journal per row), so an unbounded walk over a long
    session is measured in seconds — affordable on a deliberate tool call, never
    on a chat turn, which is why only the tool lane reaches here.
    """

    if not session_id:
        return [], False
    try:
        from .persona_chat_history import (
            MAX_PERSONA_CHAT_MESSAGE_TAIL,
            persona_chat_session_messages,
        )
    except Exception:  # pragma: no cover - defensive
        return [], False

    deadline = time.monotonic() + LIVE_LOG_BACKFILL_WALL_SECONDS
    pages: list[list[dict[str, Any]]] = []
    before: str | None = None
    total = 0
    truncated = False
    while True:
        if total >= LIVE_LOG_BACKFILL_MESSAGE_CAP or time.monotonic() > deadline:
            truncated = True
            break
        try:
            data = persona_chat_session_messages(
                session_id=session_id,
                limit=MAX_PERSONA_CHAT_MESSAGE_TAIL,
                before=before,
                session_db=session_db,
            )
        except Exception:
            break
        if not isinstance(data, dict) or not data.get("ok"):
            break
        page = [row for row in (data.get("messages") or []) if isinstance(row, dict)]
        if page:
            pages.append(page)
            total += len(page)
        before = data.get("next_before")
        if not data.get("has_more") or not before:
            break

    rows: list[dict[str, Any]] = []
    for page in reversed(pages):
        for message in page:
            text = _mirror_text(message.get("text"))
            if not text:
                continue
            payload: dict[str, Any] = {
                "ts": _iso_or_now(message.get("timestamp")),
                "kind": "message",
                "role": _normalized_role(message.get("role")),
                "text": text,
                "backfilled": True,
            }
            turn_token = _safe_token(message.get("turn_id"), limit=240)
            if turn_token:
                payload["turn_id"] = turn_token
            client_token = _logical_client_key(message.get("client_message_id"))
            if client_token:
                payload["client_message_id"] = client_token
            for key in ("relay_sender_persona_id", "relay_sender_instance_id"):
                value = _safe_token(message.get(key), limit=160)
                if value:
                    payload[key] = value
            # A backfilled delivery must be indistinguishable from a live one to
            # a consumer — that is the only thing that makes mixing the two
            # safe. The read projection names these facts `delivery_*` (it is
            # feeding the conversation contract); the mirror names them
            # `origin`/`dispatch_*`. TRANSLATE rather than copy: a blind
            # key-for-key copy would silently write nothing, because the two
            # vocabularies do not share a single key name.
            payload.update(_backfilled_delivery_fields(message))
            rows.append(payload)
    return rows, truncated


# ── reads ───────────────────────────────────────────────────────────────────


def chat_live_log_stats(session_id: Any, *, session_db: Any = None) -> dict[str, Any] | None:
    """Size / message-count / last-activity for one mirror, or ``None``.

    Counts only ``kind == "message"`` lines so the number is comparable to what
    ``agent_chat_open`` would report; tool lines are counted separately.
    """

    path = chat_live_log_path(session_id, session_db=session_db)
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    message_count = 0
    tool_count = 0
    last_activity: str | None = None
    backfill_pending = False
    backfill_truncated = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                kind = row.get("kind")
                if kind == "message":
                    message_count += 1
                elif kind == "tool":
                    tool_count += 1
                elif kind == "log_opened":
                    backfill_pending = bool(row.get("backfill_pending"))
                    backfill_truncated = bool(row.get("backfill_truncated"))
                stamp = row.get("ts")
                if isinstance(stamp, str) and stamp:
                    last_activity = stamp
    except OSError as exc:
        _note_failure("read", exc)
        return None
    rotated = path.with_name(path.name + ".1")
    return {
        "path": str(path),
        "bytes": size,
        "message_count": message_count,
        "tool_count": tool_count,
        "last_activity": last_activity,
        "backfill_pending": backfill_pending,
        # Two distinct ways history can be incomplete, kept distinct: never
        # materialized at all, versus materialized up to a declared bound.
        "backfill_truncated": backfill_truncated,
        "rotated_path": str(rotated) if _exists(rotated) else None,
    }


def chat_live_log_failures() -> int:
    """How many mirror writes failed in this process (0 when healthy)."""

    with _state_lock:
        return _failures


def reset_chat_live_log_state() -> None:
    """Drop the captured root and dedupe caches. TESTS ONLY."""

    global _captured_root, _captured_source, _failures, _failure_logged
    with _state_lock:
        _captured_root = None
        _captured_source = None
        _seen_keys.clear()
        _seeded_sessions.clear()
        _failures = 0
        _failure_logged = False


# ── internals ───────────────────────────────────────────────────────────────


def _root_from_session_db(session_db: Any) -> Path | None:
    raw = getattr(session_db, "db_path", None)
    if raw is None:
        return None
    try:
        parent = Path(str(raw)).expanduser().parent
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return parent if str(parent) not in {"", "."} else None


def _root_from_scope() -> Path | None:
    try:
        from .chat_session_scope import resolve_chat_session_scope

        return Path(resolve_chat_session_scope().head_home)
    except Exception:  # pragma: no cover - defensive
        return None


def _coerce_dir(value: Any) -> Path | None:
    try:
        candidate = Path(str(value)).expanduser()
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return candidate if str(candidate) not in {"", "."} else None


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:  # pragma: no cover - defensive
        return False


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _claim_is_stale(claim: Path) -> bool:
    try:
        age = time.time() - claim.stat().st_mtime
    except OSError:
        return False
    return age > _CLAIM_STALE_SECONDS


def _decode_lines(blob: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in blob.decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _now_iso() -> str:
    try:
        return now().isoformat()
    except Exception:  # pragma: no cover - a timestamp is not load-bearing
        return ""


def _iso_or_now(value: Any) -> str:
    text = str(value or "").strip()
    return text or _now_iso()


def _normalized_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in {"user", "operator"}:
        return "operator"
    if role in {"assistant", "agent"}:
        return "agent"
    if role == "system":
        return "system"
    return role or "unknown"


def _safe_token(value: Any, *, limit: int) -> str:
    text = str(value or "").strip().replace("\x00", "")
    return text[:limit]


def _logical_client_key(value: Any) -> str:
    """The durable operator-turn identity behind a persisted row's id.

    The runtime's native flush stamps non-user rows ``<client_message_id>:
    <role>:<index>``; the live hooks carry the bare id. Deduping on the raw
    value would let a materialized file and a live append both claim the same
    reply. One spelling, borrowed from the projection that already owns this
    normalization.
    """

    token = _safe_token(value, limit=240)
    if not token:
        return ""
    try:
        from .persona_chat_history import logical_persona_chat_client_message_id

        return logical_persona_chat_client_message_id(token) or token
    except Exception:  # pragma: no cover - defensive
        return token


def _backfilled_delivery_fields(message: dict[str, Any]) -> dict[str, str]:
    """Mirror-vocabulary origin fields for a row read back from the projection.

    The live path decodes the finish_reason marker directly
    (:func:`_relay_sender_fields`); backfill only ever sees the already-typed
    projection row, so this is the second and last place the two vocabularies
    meet. Keyed on the typed kind, never on prose.
    """

    from .persona_chat_history import PERSONA_HARNESS_DELIVERY_KIND

    if _safe_token(message.get("kind"), limit=64) != PERSONA_HARNESS_DELIVERY_KIND:
        return {}
    fields: dict[str, str] = {"origin": "harness_delivery"}
    dispatch_id = _safe_token(message.get("delivery_dispatch_id"), limit=200)
    if dispatch_id:
        fields["dispatch_id"] = dispatch_id
    fields["dispatch_state"] = (
        _safe_token(message.get("delivery_state"), limit=40) or "unknown"
    )
    if message.get("delivery_notify_operator"):
        fields["notify_operator"] = "1"
    return fields


def _relay_sender_fields(relay_marker: Any) -> dict[str, str]:
    """Origin fields for an incoming row, decoded from its finish_reason marker.

    Two non-operator origins ride that one column, and BOTH are invisible in the
    mirror without this. A relayed teammate message reads as the operator — the
    attribution defect the conversation projection carries ``relay_sender_*`` to
    avoid — and a forged dispatch DELIVERY reads as the operator too, which is
    worse here than in the UI: the mirror's whole purpose is machine
    consumption, and a head agent grepping a teammate's thread would attribute
    the runtime's own delivery to the human, with nothing in the line to say
    otherwise.

    Kept as ONE helper over one column rather than two: the marker vocabularies
    are mutually exclusive by construction (``relay_policy`` owns both), so a
    second call site would only create a chance for them to disagree.
    """

    if not relay_marker:
        return {}
    try:
        from .relay_policy import (
            parse_harness_delivery_marker,
            parse_relay_sender_marker,
        )

        delivery = parse_harness_delivery_marker(relay_marker)
        sender = None if delivery is not None else parse_relay_sender_marker(relay_marker)
    except Exception:  # pragma: no cover - defensive
        return {}
    if delivery is not None:
        fields: dict[str, str] = {"origin": "harness_delivery"}
        if delivery.dispatch_id:
            fields["dispatch_id"] = _safe_token(delivery.dispatch_id, limit=200)
        # `or "unknown"` to match the backfill path exactly. The two
        # vocabularies are deliberately identical — that is what makes a
        # backfilled line and a live one indistinguishable to a consumer — so a
        # fallback on one side and not the other is a real divergence, however
        # unreachable it looks today.
        fields["dispatch_state"] = _safe_token(delivery.state, limit=40) or "unknown"
        if delivery.notify_operator:
            fields["notify_operator"] = "1"
        return fields
    if sender is None:
        return {}
    fields = {}
    if sender.persona_id:
        fields["relay_sender_persona_id"] = _safe_token(sender.persona_id, limit=160)
    if sender.instance_id:
        fields["relay_sender_instance_id"] = _safe_token(sender.instance_id, limit=160)
    return fields


def _safe_session_token(value: Any) -> str:
    token = _safe_token(value, limit=240)
    # A session id becomes a FILENAME here. Anything path-shaped is refused
    # rather than sanitized: the ids this lane mints are
    # ``persona_chat_<handle>_<hex>``, so a value carrying a separator is not a
    # session id we should be mirroring in the first place.
    if not token or any(ch in token for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|')):
        return ""
    if token in {".", ".."}:
        return ""
    return token


def _mirror_text(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        _REDACTED_LINE if TEXT_SECRET_ASSIGNMENT_RE.search(line) else line.rstrip()
        for line in text.split("\n")
    ]
    normalized = "\n".join(lines).strip()
    if len(normalized) > LIVE_LOG_TEXT_LIMIT:
        # Truncation must be visible, never silent — same posture as the
        # persisted-transcript cap.
        normalized = normalized[:LIVE_LOG_TEXT_LIMIT].rstrip() + " … [truncated]"
    return normalized


def _append_line(path: Path, payload: dict[str, Any]) -> bool:
    try:
        blob = _encode(payload)
    except ValueError as exc:  # pragma: no cover - defensive
        _note_failure("encode", exc)
        return False
    try:
        _rotate_if_needed(path, len(blob.encode("utf-8")) + 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(blob + "\n")
        return True
    except OSError as exc:
        _note_failure("append", exc)
        return False


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _rotate_if_needed(path: Path, incoming_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size + incoming_bytes <= LIVE_LOG_ROTATE_BYTES:
        return
    try:
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError as exc:  # pragma: no cover - defensive
        _note_failure("rotate", exc)


def _already_recorded(session_id: str, path: Path, key: tuple[str, str]) -> bool:
    with _state_lock:
        if session_id in _seeded_sessions:
            return key in _seen_keys.get(session_id, set())
    seen = _seed_from_tail(path)
    # A rotation moves recent turns into the ``.1`` sibling, leaving the live
    # file's tail empty; a fresh process would then happily re-append a resend
    # it had already recorded. Seed across the generation boundary.
    rotated = path.with_name(path.name + ".1")
    if _exists(rotated):
        seen |= _seed_from_tail(rotated)
    with _state_lock:
        _seen_keys.setdefault(session_id, set()).update(seen)
        _seeded_sessions.add(session_id)
        return key in _seen_keys[session_id]


def _mark_recorded(session_id: str, key: tuple[str, str]) -> None:
    with _state_lock:
        _seen_keys.setdefault(session_id, set()).add(key)
        _seeded_sessions.add(session_id)


def _seed_from_tail(path: Path) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > _DEDUPE_TAIL_BYTES:
                handle.seek(size - _DEDUPE_TAIL_BYTES)
                handle.readline()  # drop the partial line
            blob = handle.read()
    except OSError as exc:
        _note_failure("seed", exc)
        return seen
    for row in _decode_lines(blob):
        if row.get("kind") != "message":
            continue
        client = str(row.get("client_message_id") or "")
        if client:
            seen.add((_normalized_role(row.get("role")), client))
    return seen


def _note_failure(step: str, exc: Exception | None = None) -> bool:
    """Count a mirror failure; log the FIRST one only.

    Silence would make a broken mirror indistinguishable from an idle one; a log
    line per failed append would flood a chat turn's log. One line plus a
    running count is the honest middle.
    """

    global _failures, _failure_logged
    with _state_lock:
        _failures += 1
        should_log = not _failure_logged
        _failure_logged = True
    if should_log:
        logger.warning(
            "chat live-log mirror write failed (%s): %s — the transcript itself is "
            "unaffected; the mirror is a regenerable artifact",
            step,
            exc,
        )
    return False
