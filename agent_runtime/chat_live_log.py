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

Two line kinds, both bounded:

    {"ts": ..., "kind": "message", "role": "operator|agent|system",
     "text": ..., "turn_id"?: ..., "client_message_id"?: ...}
    {"ts": ..., "kind": "tool", "tool": ..., "status": ..., "turn_id"?: ...}

plus a single ``{"kind": "log_opened"}`` header written when the file is
created, which also carries the one-shot backfill count.

Contract
--------
* **Regenerable artifact, never authority.** SessionDB remains the transcript
  of record; every line here is rebuildable from it. Deleting the directory
  loses nothing but convenience — the next request backfills it again.
  KNOWN GAP, stated rather than hidden: the backfill runs ONCE, at file
  creation, so a message whose mirror append was lost (process killed between
  the durable write and the append, or a transient IO failure) stays missing
  from that file. The repair is to delete the file and let the next request
  rebuild it whole; nothing downstream treats the mirror as complete.
* **Redaction-safe by construction.** Every text this module writes goes
  through :data:`~agent_runtime.redaction.TEXT_SECRET_ASSIGNMENT_RE` — the ONE
  secret-assignment authority — with per-line masking, exactly like the read
  projection (``persona_chat_history._mask_secret_lines``). The live-append
  callers already hand over redacted text (the ``_redact_persona_chat_text``
  write boundary), and the backfill reads the already-redacting projection, so
  the pass here is idempotent belt-and-braces rather than the only guard.
* **Best effort, counted.** A mirror write must NEVER fail a chat turn. IO
  failures are swallowed but tallied (:func:`chat_live_log_failures`) and
  logged ONCE per process, so a silently broken mirror is still discoverable
  instead of merely invisible.
* **Bounded.** Per-line text cap :data:`LIVE_LOG_TEXT_LIMIT`; size-capped
  rotation at :data:`LIVE_LOG_ROTATE_BYTES` into a single ``.1`` sibling (one
  generation kept, the older one is dropped — this is a convenience mirror, not
  an archive).

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

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from hermes_time import now

from .redaction import TEXT_SECRET_ASSIGNMENT_RE

logger = logging.getLogger(__name__)

__all__ = [
    "CHAT_LIVE_LOG_DIRNAME",
    "LIVE_LOG_BACKFILL_MESSAGE_CAP",
    "LIVE_LOG_ROTATE_BYTES",
    "LIVE_LOG_TEXT_LIMIT",
    "capture_chat_live_log_root",
    "chat_live_log_failures",
    "chat_live_log_path",
    "chat_live_log_root",
    "chat_live_log_stats",
    "ensure_chat_live_log",
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
#: Upper bound on rows materialized by the one-shot backfill.
LIVE_LOG_BACKFILL_MESSAGE_CAP = 4000
#: Same masking vocabulary the read projection uses.
_REDACTED_LINE = "[redacted line — contained a secret]"
#: How much of an existing file the replay-dedupe index seeds from. Replays only
#: ever re-offer recent turns, so a bounded tail scan is enough and a 10MB file
#: never has to be read whole on a hot path.
_DEDUPE_TAIL_BYTES = 256 * 1024

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


# ── writes ──────────────────────────────────────────────────────────────────


def record_chat_message(
    *,
    session_id: Any,
    role: Any,
    text: Any,
    turn_id: Any = None,
    client_message_id: Any = None,
    session_db: Any = None,
) -> bool:
    """Append one persisted chat message to the live mirror.

    Called from the persona-chat persist seam, so the text handed in has already
    crossed the ``_redact_persona_chat_text`` write boundary. Idempotent for a
    given ``(role, client_message_id)`` pair, which is what makes the replay /
    resend lanes safe to route through here without doubling rows.
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
    client_key = _safe_token(client_message_id, limit=240)
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


# ── backfill ────────────────────────────────────────────────────────────────


def ensure_chat_live_log(session_id: Any, *, session_db: Any = None) -> Path | None:
    """Return the mirror path, materializing pre-feature history exactly once.

    A session that predates this feature (or one whose mirror was deleted) has
    no file. The first touch — a live append, or a head agent asking for the
    path — creates it from the SAME projection ``agent_chat_open`` reads
    (``persona_chat_session_messages``, which redacts at read), so the head does
    not get a file that starts mid-conversation. Live appends continue it from
    there; the backfill never runs again for that file.
    """

    path = chat_live_log_path(session_id, session_db=session_db)
    if path is None:
        return None
    try:
        if path.exists():
            return path
    except OSError:
        return None

    token = _safe_session_token(session_id) or ""
    rows = _backfill_rows(token, session_db=session_db)
    lines: list[dict[str, Any]] = [
        {
            "ts": _now_iso(),
            "kind": "log_opened",
            "session_id": token,
            "backfilled": len(rows),
        }
    ]
    lines.extend(rows)
    if _create_log(path, lines):
        return path
    # Lost the create race with another writer (or the directory is unwritable
    # and _create_log already counted the failure). If the file is there now it
    # is usable either way.
    try:
        return path if path.exists() else None
    except OSError:
        return None


def _backfill_rows(session_id: str, *, session_db: Any = None) -> list[dict[str, Any]]:
    if not session_id:
        return []
    try:
        from .persona_chat_history import (
            MAX_PERSONA_CHAT_MESSAGE_TAIL,
            persona_chat_session_messages,
        )
    except Exception:  # pragma: no cover - defensive
        return []

    pages: list[list[dict[str, Any]]] = []
    before: str | None = None
    total = 0
    while total < LIVE_LOG_BACKFILL_MESSAGE_CAP:
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
            client_token = _safe_token(message.get("client_message_id"), limit=240)
            if client_token:
                payload["client_message_id"] = client_token
            rows.append(payload)
    return rows


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
                stamp = row.get("ts")
                if isinstance(stamp, str) and stamp:
                    last_activity = stamp
    except OSError as exc:
        _note_failure("read", exc)
        return None
    rotated = path.with_name(path.name + ".1")
    try:
        has_rotated = rotated.exists()
    except OSError:  # pragma: no cover - defensive
        has_rotated = False
    return {
        "path": str(path),
        "bytes": size,
        "message_count": message_count,
        "tool_count": tool_count,
        "last_activity": last_activity,
        "rotated_path": str(rotated) if has_rotated else None,
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


def _create_log(path: Path, lines: list[dict[str, Any]]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            for payload in lines:
                handle.write(_encode(payload) + "\n")
        return True
    except FileExistsError:
        return False
    except (OSError, ValueError) as exc:
        _note_failure("create", exc)
        return False


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
        seeded = session_id in _seeded_sessions
        if seeded:
            return key in _seen_keys.get(session_id, set())
    seen = _seed_from_tail(path)
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
    for raw in blob.decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("kind") != "message":
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
