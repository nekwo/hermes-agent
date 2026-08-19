from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import unlink_quietly

CAPABILITY_ID = "mission.chat.steer"
_POLL_SECONDS = 0.05
_ACK_TIMEOUT_SECONDS = 4.0


@dataclass(slots=True)
class MissionChatSteerHandle:
    runtime_root: Path
    session_id: str
    agent: Any
    stop_event: threading.Event = field(default_factory=threading.Event)
    processed_client_ids: set[str] = field(default_factory=set)
    thread: threading.Thread | None = None

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        _mark_active_complete(self.runtime_root, self.session_id)


def start_active_mission_chat_turn(
    *,
    runtime_root: Path,
    session_id: str,
    agent: Any,
    persona_id: str | None = None,
    persona_instance_id: str | None = None,
    client_message_id: str | None = None,
    poll_seconds: float = _POLL_SECONDS,
) -> MissionChatSteerHandle:
    """Register a live Mission Control chat turn and watch its steer inbox."""

    normalized_session_id = _require_text(session_id, "session_id")
    root = Path(runtime_root)
    turn_dir = _turn_dir(root, normalized_session_id)
    (turn_dir / "inbox").mkdir(parents=True, exist_ok=True)
    (turn_dir / "ack").mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        _active_path(root, normalized_session_id),
        {
            "ok": True,
            "capability_id": CAPABILITY_ID,
            "execution_state": "active",
            "session_id": normalized_session_id,
            "persona_id": persona_id,
            "persona_instance_id": persona_instance_id,
            "client_message_id": client_message_id,
            "pid": os.getpid(),
            "started_at": time.time(),
            "updated_at": time.time(),
        },
    )
    handle = MissionChatSteerHandle(
        runtime_root=root,
        session_id=normalized_session_id,
        agent=agent,
    )
    handle.thread = threading.Thread(
        target=_watch_inbox,
        name=f"mission-chat-steer-{_safe_token(normalized_session_id)}",
        args=(handle, poll_seconds),
        daemon=True,
    )
    handle.thread.start()
    return handle


def submit_mission_chat_steer(
    *,
    runtime_root: Path,
    session_id: str,
    message: str,
    client_message_id: str,
    persona_id: str | None = None,
    persona_instance_id: str | None = None,
    timeout_seconds: float = _ACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    normalized_session_id = (session_id or "").strip()
    normalized_client_id = (client_message_id or "").strip()
    normalized_message = (message or "").strip()
    if not normalized_session_id or not normalized_client_id or not normalized_message:
        return _rejected(
            "invalid_request",
            session_id=normalized_session_id,
            client_message_id=normalized_client_id,
            error="session_id, client_message_id, and non-empty message are required",
        )
    root = Path(runtime_root)
    if not _active_record_is_live(root, normalized_session_id):
        return _rejected(
            "no_active_turn",
            session_id=normalized_session_id,
            client_message_id=normalized_client_id,
            error="No active Mission Control chat turn is registered for this session.",
        )
    ack_path = _ack_path(root, normalized_session_id, normalized_client_id)
    existing = _read_json(ack_path)
    if existing is not None:
        return {**existing, "idempotent_replay": True}

    request_path = _request_path(root, normalized_session_id, normalized_client_id)
    if not request_path.exists():
        _write_json_atomic(
            request_path,
            {
                "capability_id": CAPABILITY_ID,
                "session_id": normalized_session_id,
                "client_message_id": normalized_client_id,
                "persona_id": persona_id,
                "persona_instance_id": persona_instance_id,
                "message": normalized_message,
                "created_at": time.time(),
            },
        )

    deadline = time.monotonic() + max(0.05, timeout_seconds)
    while time.monotonic() < deadline:
        ack = _read_json(ack_path)
        if ack is not None:
            return ack
        if not _active_record_is_live(root, normalized_session_id):
            return _rejected(
                "no_active_turn",
                session_id=normalized_session_id,
                client_message_id=normalized_client_id,
                error="The active Mission Control chat turn ended before steer was accepted.",
            )
        time.sleep(_POLL_SECONDS)
    return _rejected(
        "mission_chat_steer_timeout",
        session_id=normalized_session_id,
        client_message_id=normalized_client_id,
        error="Timed out waiting for the live Mission Control chat turn to acknowledge steer.",
    )


def _watch_inbox(handle: MissionChatSteerHandle, poll_seconds: float) -> None:
    inbox = _turn_dir(handle.runtime_root, handle.session_id) / "inbox"
    while not handle.stop_event.is_set():
        for path in sorted(inbox.glob("*.json"), key=lambda item: (item.stat().st_mtime, item.name)):
            _process_request(handle, path)
        handle.stop_event.wait(max(0.01, poll_seconds))


def _process_request(handle: MissionChatSteerHandle, path: Path) -> None:
    request = _read_json(path)
    if request is None:
        return
    client_message_id = _require_text(str(request.get("client_message_id") or path.stem), "client_message_id")
    ack_path = _ack_path(handle.runtime_root, handle.session_id, client_message_id)
    if client_message_id in handle.processed_client_ids:
        if not ack_path.exists():
            _write_json_atomic(
                ack_path,
                _accepted(session_id=handle.session_id, client_message_id=client_message_id, duplicate=True),
            )
        unlink_quietly(path)
        return
    message = str(request.get("message") or "").strip()
    if not message:
        _write_json_atomic(
            ack_path,
            _rejected(
                "invalid_request",
                session_id=handle.session_id,
                client_message_id=client_message_id,
                error="message is required",
            ),
        )
        unlink_quietly(path)
        return
    steer = getattr(handle.agent, "steer", None)
    if not callable(steer):
        _write_json_atomic(
            ack_path,
            _rejected(
                "steer_unavailable",
                session_id=handle.session_id,
                client_message_id=client_message_id,
                error="The active agent does not expose AIAgent.steer(text).",
            ),
        )
        unlink_quietly(path)
        return
    try:
        steer(message)
        handle.processed_client_ids.add(client_message_id)
        _mirror_steer_to_live_log(handle.session_id, message, client_message_id)
        _write_json_atomic(
            ack_path,
            _accepted(session_id=handle.session_id, client_message_id=client_message_id),
        )
    except Exception as exc:
        _write_json_atomic(
            ack_path,
            _rejected(
                "steer_failed",
                session_id=handle.session_id,
                client_message_id=client_message_id,
                error=type(exc).__name__,
            ),
        )
    finally:
        unlink_quietly(path)


def _mirror_steer_to_live_log(session_id: str, message: str, client_message_id: str) -> None:
    """Put an accepted mid-turn steer into the session's live chat log.

    ``handle.session_id`` is the ROOT chat session (``_agent_ready_for_steer``
    binds the turn with it), so this lands in the same file the turn's order and
    reply do. Without it a head agent tailing that file sees an agent visibly
    change course with nothing in the stream explaining why — and would have to
    wait for a rebuild to learn a steer had been sent at all.

    Best effort: a steer is accepted the moment ``steer()`` returns, and a
    mirror problem must never turn an accepted steer into a rejected one.
    """

    try:
        from .chat_live_log import record_chat_message

        record_chat_message(
            session_id=session_id,
            role="operator",
            text=message,
            client_message_id=client_message_id,
            steered=True,
        )
    except Exception:
        return None
    return None


def _accepted(*, session_id: str, client_message_id: str, duplicate: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "capability_id": CAPABILITY_ID,
        "execution_state": "accepted",
        "session_id": session_id,
        "client_message_id": client_message_id,
        "idempotent_replay": duplicate,
        "next_expected": "steer accepted into the active Mission Control chat turn",
    }


def _rejected(error_kind: str, *, session_id: str, client_message_id: str, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "capability_id": CAPABILITY_ID,
        "execution_state": "rejected",
        "session_id": session_id,
        "client_message_id": client_message_id,
        "error_kind": error_kind,
        "error": error,
        "next_expected": "fall back to mission.chat.message if operator text should start a new turn",
    }


def _active_record_is_live(runtime_root: Path, session_id: str) -> bool:
    record = _read_json(_active_path(runtime_root, session_id))
    if record is None or record.get("execution_state") != "active":
        return False
    pid = record.get("pid")
    if isinstance(pid, int) and pid > 0 and not _pid_is_alive(pid):
        return False
    return True


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _mark_active_complete(runtime_root: Path, session_id: str) -> None:
    path = _active_path(runtime_root, session_id)
    record = _read_json(path) or {}
    record.update(
        {
            "ok": True,
            "capability_id": CAPABILITY_ID,
            "execution_state": "completed",
            "session_id": session_id,
            "pid": os.getpid(),
            "updated_at": time.time(),
        }
    )
    _write_json_atomic(path, record)


def _turn_dir(runtime_root: Path, session_id: str) -> Path:
    return Path(runtime_root) / "mission_chat_steer" / _safe_token(session_id)


def _active_path(runtime_root: Path, session_id: str) -> Path:
    return _turn_dir(runtime_root, session_id) / "active.json"


def _request_path(runtime_root: Path, session_id: str, client_message_id: str) -> Path:
    return _turn_dir(runtime_root, session_id) / "inbox" / f"{_safe_token(client_message_id)}.json"


def _ack_path(runtime_root: Path, session_id: str, client_message_id: str) -> Path:
    return _turn_dir(runtime_root, session_id) / "ack" / f"{_safe_token(client_message_id)}.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        decoded = json.loads(path.read_text(encoding="utf-8"))
        return decoded if isinstance(decoded, dict) else None
    except Exception:
        return None


def _require_text(value: str | None, name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())[:160]
    return token or "default"
