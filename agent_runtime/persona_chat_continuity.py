"""Native persona-chat continuity primitives.

This module intentionally contains no CLI presentation logic.  Direct CLI
commands and long-lived ``harness serve`` processes share these exact lease,
mint-receipt, safe-history, and resident-actor primitives.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterator

from . import paths
from .persona_assignments import (
    PersonaInstanceStore,
    persona_chat_session_id_for,
    safe_assignment_text,
    safe_assignment_token,
)


PERSONA_CHAT_SESSION_SOURCE = "agent_runtime_persona_chat"
_TOOL_EXECUTION_SCOPE: ContextVar[str | None] = ContextVar(
    "persona_chat_tool_execution_scope", default=None
)
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]\s*([^\s,;]+)"
)
_MAX_CONTENT = 20_000
_MAX_ARGUMENTS = 4_000


def _safe_text(value: Any, *, limit: int = _MAX_CONTENT) -> str:
    text = str(value or "").replace("\x00", " ")
    text = _SECRET_RE.sub(r"\1: [redacted]", text)
    if len(text) > limit:
        return text[:limit].rstrip() + " … [truncated]"
    return text


def safe_native_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return the one retained-and-persisted persona-chat message shape.

    Tool structure and ordering identifiers survive, while raw/unbounded
    payloads and provider-specific residue do not.  Applying this function more
    than once is stable, which lets warm memory and cold persistence share the
    same boundary without representation drift.
    """

    role = str(message.get("role") or "").strip().lower()
    if role not in {"system", "user", "assistant", "tool"}:
        role = "assistant"
    result: dict[str, Any] = {"role": role, "content": _safe_text(message.get("content"))}
    for key in (
        "tool_call_id",
        "tool_name",
        "finish_reason",
        "platform_message_id",
        "client_message_id",
        "turn_id",
        "root_chat_session_id",
    ):
        raw_value = message.get(key)
        if key == "platform_message_id" and raw_value is None:
            raw_value = message.get("message_id")
        value = safe_assignment_text(raw_value, limit=240)
        if value:
            result[key] = value
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        safe_calls: list[dict[str, Any]] = []
        for raw in calls[:64]:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            call_id = safe_assignment_text(raw.get("id"), limit=240)
            name = safe_assignment_text(function.get("name") or raw.get("name"), limit=240)
            if not call_id or not name:
                continue
            safe_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": _safe_text(function.get("arguments"), limit=_MAX_ARGUMENTS),
                    },
                }
            )
        if safe_calls:
            result["tool_calls"] = safe_calls
    return result


def safe_native_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    abandoned = {
        str(item.get("client_message_id"))
        for item in messages
        if isinstance(item, dict) and item.get("abandoned")
    }
    safe: list[dict[str, Any]] = []
    live_tool_ids: set[str] = set()
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("client_message_id") or "") in abandoned:
            continue
        item = safe_native_message(raw)
        if item["role"] == "assistant":
            live_tool_ids.update(str(call["id"]) for call in item.get("tool_calls", []))
        if item["role"] == "tool" and item.get("tool_call_id") not in live_tool_ids:
            continue
        safe.append(item)
    return safe


def native_history_revision(session_db: Any, root_session_id: str) -> str:
    tip = session_db.resolve_resume_session_id(root_session_id)
    history = session_db.get_messages_as_conversation(tip, include_ancestors=True)
    payload = json.dumps(safe_native_history(history or []), sort_keys=True, separators=(",", ":"))
    return f"{tip}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


@contextmanager
def tool_execution_scope(scope_id: str | None) -> Iterator[None]:
    token = _TOOL_EXECUTION_SCOPE.set(safe_assignment_text(scope_id, limit=240))
    try:
        yield
    finally:
        _TOOL_EXECUTION_SCOPE.reset(token)


def current_tool_execution_scope() -> str | None:
    return _TOOL_EXECUTION_SCOPE.get()


class PersonaChatBusyError(RuntimeError):
    error_kind = "chat_busy"

    def __init__(self, root_session_id: str, owner: dict[str, Any] | None = None):
        super().__init__(f"persona chat root is busy: {root_session_id}")
        self.root_session_id = root_session_id
        self.owner = dict(owner or {})


def _root_stem(root: str) -> str:
    prefix = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in root)[:80]
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
    return f"{prefix or 'chat'}_{digest}"


def _lease_paths(root: str) -> tuple[Path, Path]:
    base = paths.store_root() / "persona_chat_leases"
    stem = _root_stem(root)
    return base / f"{stem}.lock", base / f"{stem}.owner.json"


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def persona_chat_root_lease(
    root_session_id: str,
    *,
    owner_id: str | None = None,
    observer_kind: str = "cli",
    timeout_seconds: float = 0.0,
) -> Iterator[dict[str, Any]]:
    """Hold the OS-backed root lease for an entire native turn."""

    root = safe_assignment_text(root_session_id, limit=240)
    if not root:
        raise ValueError("root_session_id is required")
    lock_path, owner_path = _lease_paths(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    acquired = False
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    try:
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    owner = None
                    try:
                        owner = json.loads(owner_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                    raise PersonaChatBusyError(root, owner)
                time.sleep(0.01)
        owner = {
            "root_chat_session_id": root,
            "owner_id": safe_assignment_token(owner_id) or f"pid-{os.getpid()}",
            "observer_kind": safe_assignment_token(observer_kind) or "cli",
            "pid": os.getpid(),
            "acquired_at": time.time(),
        }
        tmp = owner_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(owner, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(owner_path))
        yield owner
    finally:
        if acquired:
            try:
                owner_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                _unlock(fd)
            except OSError:
                pass
        os.close(fd)


class PersonaChatMintReceiptStore:
    def _path(self, instance_id: str, key: str) -> Path:
        digest = hashlib.sha256(f"{instance_id}\0{key}".encode("utf-8")).hexdigest()
        return paths.store_root() / "persona_chat_mint_receipts" / f"{digest}.json"

    def mint(
        self,
        *,
        instance_store: PersonaInstanceStore,
        session_db: Any,
        persona_id: str,
        persona_instance_id: str,
        idempotency_key: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        instance_id = safe_assignment_token(persona_instance_id)
        key = safe_assignment_text(idempotency_key, limit=240)
        if not instance_id or not key:
            raise ValueError("persona_instance_id and idempotency_key are required")
        path = self._path(instance_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:
                    time.sleep(0.01)
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                receipt = {}
            root = safe_assignment_text(receipt.get("root_chat_session_id"), limit=240)
            if not root:
                root = persona_chat_session_id_for(instance_id)
                receipt = {
                    "schema_version": 1,
                    "persona_instance_id": instance_id,
                    "idempotency_key": key,
                    "root_chat_session_id": root,
                    "state": "reserved",
                    "created_at": time.time(),
                }
                _atomic_json(path, receipt)
            session_db.create_session(
                session_id=root,
                source=PERSONA_CHAT_SESSION_SOURCE,
                model=None,
                system_prompt=f"Mission Control persona chat for {persona_id}",
            )
            meta = {
                "mission_chat_root_id": root,
                "persona_instance_id": instance_id,
                "source": PERSONA_CHAT_SESSION_SOURCE,
            }
            session_db.update_session_meta(root, json.dumps(meta, sort_keys=True))
            if title and not session_db.get_session_title(root):
                try:
                    session_db.set_session_title(root, title)
                except Exception as exc:
                    if "already in use" not in str(exc).lower():
                        raise
                    session_db.set_session_title(root, f"{title} · {root[-8:]}")
            instance = instance_store.open_chat(
                persona_id=persona_id,
                persona_instance_id=instance_id,
                session_id=root,
            )
            receipt.update({"state": "completed", "completed_at": time.time()})
            _atomic_json(path, receipt)
            return {**receipt, "default_chat_session_id": instance.default_chat_session_id}
        finally:
            try:
                _unlock(fd)
            except OSError:
                pass
            os.close(fd)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(str(tmp), str(path))


@dataclass
class ResidentPersonaChatRuntime:
    root_session_id: str
    active_session_id: str
    signature: str
    revision: str
    agent: Any
    last_used_at: float
    created_at: float
    turn_count: int = 0


class PersonaChatRuntimeRegistry:
    """Bounded process-scoped one-resident-agent-per-root registry."""

    def __init__(self, *, max_entries: int = 8, ttl_seconds: float = 900.0):
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._entries: OrderedDict[str, ResidentPersonaChatRuntime] = OrderedDict()
        self._transitions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def acquire(
        self,
        *,
        root_session_id: str,
        active_session_id: str,
        signature: str,
        revision: str,
        factory: Callable[[], Any],
    ) -> tuple[ResidentPersonaChatRuntime, bool, str | None]:
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            entry = self._entries.pop(root_session_id, None)
            rebuild_reason = None
            if entry is not None and entry.signature != signature:
                rebuild_reason = "runtime_signature_changed"
                entry = None
            elif entry is not None and (entry.revision != revision or entry.active_session_id != active_session_id):
                rebuild_reason = "disk_revision_changed"
                entry = None
            reused = entry is not None
            if entry is None:
                entry = ResidentPersonaChatRuntime(
                    root_session_id=root_session_id,
                    active_session_id=active_session_id,
                    signature=signature,
                    revision=revision,
                    agent=factory(),
                    created_at=now,
                    last_used_at=now,
                )
            entry.last_used_at = now
            self._entries[root_session_id] = entry
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return entry, reused, rebuild_reason

    def finish(self, root_session_id: str, *, active_session_id: str, revision: str) -> None:
        with self._lock:
            entry = self._entries.get(root_session_id)
            if entry is None:
                return
            entry.active_session_id = active_session_id
            entry.revision = revision
            entry.last_used_at = time.monotonic()
            entry.turn_count += 1
            self._entries.move_to_end(root_session_id)
            self._record_transition(root_session_id, "hot")

    def transition(self, root_session_id: str, state: str) -> None:
        """Record process-local lifecycle truth for an owning serve observer."""

        if state not in {"cold", "busy", "hot", "failed"}:
            raise ValueError(f"invalid persona chat runtime state: {state}")
        with self._lock:
            self._record_transition(root_session_id, state)

    def evict(self, root_session_id: str) -> bool:
        with self._lock:
            removed = self._entries.pop(root_session_id, None) is not None
            self._record_transition(root_session_id, "cold")
            return removed

    def observation(self, root_session_id: str, *, owning_process: bool) -> dict[str, Any]:
        if not owning_process:
            return {"runtime_state": "unknown", "observer_identity": "external_cli"}
        with self._lock:
            entry = self._entries.get(root_session_id)
            transition = self._transitions.get(root_session_id) or {}
            state = transition.get("state") or ("hot" if entry else "cold")
            return {
                "runtime_state": state,
                "last_runtime_transition": transition.get("transition"),
                "observer_identity": f"serve:{os.getpid()}",
                "observer_time": time.time(),
                "active_session_id": entry.active_session_id if entry else None,
                "continuation_depth": entry.turn_count if entry else 0,
                "last_resumed_at": entry.last_used_at if entry else None,
            }

    def _record_transition(self, root_session_id: str, state: str) -> None:
        self._transitions[root_session_id] = {
            "state": state,
            "transition": f"{state}@{time.time():.6f}",
        }

    def _evict_expired(self, now: float) -> None:
        expired = [key for key, value in self._entries.items() if now - value.last_used_at > self.ttl_seconds]
        for key in expired:
            self._entries.pop(key, None)


_REGISTRY: PersonaChatRuntimeRegistry | None = None


def initialize_persona_chat_runtime_registry(*, max_entries: int = 8, ttl_seconds: float = 900.0) -> PersonaChatRuntimeRegistry:
    global _REGISTRY
    _REGISTRY = PersonaChatRuntimeRegistry(max_entries=max_entries, ttl_seconds=ttl_seconds)
    return _REGISTRY


def persona_chat_runtime_registry() -> PersonaChatRuntimeRegistry | None:
    return _REGISTRY
