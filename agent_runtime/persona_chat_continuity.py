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
from datetime import datetime, timezone

from . import paths
from .dispatch_session_policy import superseded_session_id
from .persona_assignments import (
    PersonaInstanceStore,
    persona_chat_session_id_for,
    safe_assignment_text,
    safe_assignment_token,
)
from .redaction import TEXT_SECRET_ASSIGNMENT_RE


PERSONA_CHAT_SESSION_SOURCE = "agent_runtime_persona_chat"
_TOOL_EXECUTION_SCOPE: ContextVar[str | None] = ContextVar(
    "persona_chat_tool_execution_scope", default=None
)
# Single-homed in ``agent_runtime.redaction`` — see the header there for the
# JSON blind spot every local spelling shared. group(1) is still the key, so
# the ``\1: [redacted]`` rebuild below is unchanged; the value shape widens
# from ``[^\s,;]+`` to ``\S+``, which only removes MORE of the offending run.
_SECRET_RE = TEXT_SECRET_ASSIGNMENT_RE
_MAX_CONTENT = 20_000
_MAX_ARGUMENTS = 4_000


def _safe_text(value: Any, *, limit: int = _MAX_CONTENT) -> str:
    text = str(value or "").replace("\x00", " ")
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:
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
    normalized = [
        safe_native_message(raw)
        for raw in messages
        if isinstance(raw, dict)
        and str(raw.get("client_message_id") or "") not in abandoned
    ]
    # Compression children contain the protected current turn as well as the
    # preserved parent transcript. Folding a multi-pass lineage can therefore
    # surface byte-identical copies of one logical user/reply row. Collapse
    # only rows that share the stable client/role/payload identity; distinct
    # compaction summaries and tool-call messages remain intact.
    deduped: list[dict[str, Any]] = []
    seen_logical_rows: set[tuple[str, str, str]] = set()
    for item in normalized:
        client_message_id = str(item.get("client_message_id") or "")
        if client_message_id:
            logical_client_id = re.sub(r":assistant:\d+$", "", client_message_id)
            payload = json.dumps(
                {
                    "content": item.get("content"),
                    "tool_calls": item.get("tool_calls"),
                    "tool_call_id": item.get("tool_call_id"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            key = (item["role"], logical_client_id, payload)
            if key in seen_logical_rows:
                continue
            seen_logical_rows.add(key)
        deduped.append(item)
    normalized = deduped
    available_results = {
        str(item.get("tool_call_id"))
        for item in normalized
        if item.get("role") == "tool" and item.get("tool_call_id")
    }
    safe: list[dict[str, Any]] = []
    live_tool_ids: set[str] = set()
    for item in normalized:
        if item["role"] == "assistant":
            paired_calls = [
                call
                for call in item.get("tool_calls", [])
                if str(call.get("id") or "") in available_results
            ]
            if paired_calls:
                item["tool_calls"] = paired_calls
                live_tool_ids.update(str(call["id"]) for call in paired_calls)
            else:
                item.pop("tool_calls", None)
        if item["role"] == "tool" and item.get("tool_call_id") not in live_tool_ids:
            continue
        safe.append(item)
    return safe


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def native_lineage_summary(session_db: Any, root_session_id: str) -> dict[str, Any]:
    """Resolve a root's current native tip and validated compression depth."""

    root = safe_assignment_text(root_session_id, limit=240)
    tip = session_db.resolve_resume_session_id(root)
    current = tip
    depth = 0
    seen: set[str] = set()
    while current and current != root:
        if current in seen:
            raise ValueError(f"cyclic persona chat lineage for {root}")
        seen.add(current)
        row = session_db.get_session(current)
        if not isinstance(row, dict):
            raise ValueError(f"missing persona chat lineage node {current}")
        parent = safe_assignment_text(row.get("parent_session_id"), limit=240)
        if not parent:
            raise ValueError(f"persona chat tip {tip} does not descend from {root}")
        current = parent
        depth += 1
    return {"active_session_id": tip, "continuation_depth": depth}


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


def repair_orphaned_chat_turns() -> list[str]:
    """Settle in-flight turn records whose executor process died (boot sweep).

    A native turn holds the OS-backed root lease for its ENTIRE execution and
    the kernel releases the lock when the holding process dies, so "in-flight
    record AND acquirable lease" is proof the turn can no longer settle itself
    (live incident 2026-07-25: the Stage C MCP flow reaped the Launcher, which
    took the serve child executing a QA relay turn with it; the record froze
    at ``executing`` and the console showed a running turn forever). A session
    whose lease is HELD is a live turn in another process and is skipped.

    Runs at ``harness serve`` boot — the moment a launcher restart replaces a
    dead runtime — before the first hydrate is served, so the repaired records
    project as typed ``turn_interrupted`` markers instead of frozen output.
    When anything flips, a ``state.reconciled`` event is appended so any
    already-connected watermark-gated consumer converges too (turn files are
    not patch-covered). Best-effort per session; the next boot retries.
    """

    from .mission_chat_turns import (
        inflight_chat_session_roots,
        mark_stale_inflight_turns_interrupted,
    )

    repaired: list[str] = []
    for root in inflight_chat_session_roots():
        try:
            with persona_chat_root_lease(root, observer_kind="orphan_sweep"):
                flipped = mark_stale_inflight_turns_interrupted(
                    session_id=root,
                    active_client_message_id=None,
                )
        except PersonaChatBusyError:
            continue
        except Exception:  # noqa: BLE001 — sweep must never block serve boot
            continue
        if flipped:
            repaired.append(root)
    if repaired:
        try:
            from hermes_time import now

            from .events import EventLog
            from .models import Event

            digest = hashlib.sha1("|".join(sorted(repaired)).encode("utf-8")).hexdigest()[:16]
            EventLog().append(
                Event(
                    now(),
                    "state.reconciled",
                    None,
                    None,
                    None,
                    {"fingerprint": digest, "source": "chat_orphan_sweep"},
                )
            )
        except Exception:  # noqa: BLE001 — the repair itself already landed
            pass
    return repaired


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
        dispatched_from: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reserve-then-create the instance's canonical chat root.

        ``dispatched_from`` records task-scoped dispatch lineage — the thread
        this fresh session superseded, and the sender session that asked for it
        — into the session meta as ``_dispatched_from``. It rides the SAME meta
        write as ``mission_chat_root_id`` (one write, one authority) rather than
        a second update, and deliberately does NOT touch ``parent_session_id``:
        on the persona-chat lane that column is claimed by native-compression
        lineage (``native_lineage_summary`` raises on a foreign parent, and
        usage aggregation blanks), so borrowing it for relay provenance would
        corrupt both. Marker-key precedent: ``_delegate_from`` / ``_branched_from``.
        """

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
            lineage = _dispatch_lineage_meta(dispatched_from, root=root)
            if not lineage:
                # REPLAY: the same idempotency key resolves the root this call
                # already established, so the caller's "predecessor" is now this
                # very session and drops out as a self-reference. The lineage it
                # was BORN with is still true — carry it forward, because this
                # meta write replaces the stored value wholesale and would
                # otherwise erase the provenance on a retry.
                lineage = _stored_dispatch_lineage(session_db, root)
            if lineage:
                meta["_dispatched_from"] = lineage
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
            return {
                **receipt,
                "default_chat_session_id": instance.default_chat_session_id,
                # The lineage this mint actually RECORDED (not the lineage it was
                # asked for), so the reply envelope reports the same predecessor
                # the session meta holds — on a first mint and on a replay alike.
                # Not persisted into the receipt file: the meta is its home.
                "dispatched_from": dict(lineage),
            }
        finally:
            try:
                _unlock(fd)
            except OSError:
                pass
            os.close(fd)


#: Keys accepted inside ``_dispatched_from``. An allow-list rather than a
#: pass-through: session meta is read back by projections and shipped to the
#: launcher, so an unbounded caller-supplied dict is a payload-growth and
#: leak surface, not provenance.
_DISPATCH_LINEAGE_KEYS = ("predecessor_chat_session_id", "requested_by_session")


def _stored_dispatch_lineage(session_db: Any, root: str) -> dict[str, str]:
    """The ``_dispatched_from`` already recorded for *root*, if any."""

    try:
        row = session_db.get_session(root)
    except Exception:
        return {}
    raw = (row if isinstance(row, dict) else {}).get("model_config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except Exception:
            return {}
    stored = raw.get("_dispatched_from") if isinstance(raw, dict) else None
    if not isinstance(stored, dict):
        return {}
    return {
        str(key): text
        for key in _DISPATCH_LINEAGE_KEYS
        if (text := safe_assignment_text(stored.get(key), limit=240))
    }


def _dispatch_lineage_meta(
    dispatched_from: dict[str, Any] | None, *, root: str
) -> dict[str, str]:
    """Bounded, string-only projection of the dispatch lineage.

    *root* is the session this meta belongs to: a replayed mint resolves the
    same receipt and re-writes this meta with a predecessor that has since
    BECOME this session, so the self-reference is dropped through the shared
    :func:`superseded_session_id` rule the reply envelope uses."""

    if not isinstance(dispatched_from, dict):
        return {}
    lineage: dict[str, str] = {}
    for key in _DISPATCH_LINEAGE_KEYS:
        value = safe_assignment_text(dispatched_from.get(key), limit=240)
        if key == "predecessor_chat_session_id":
            value = superseded_session_id(value, established=root) or ""
        if value:
            lineage[key] = value
    return lineage


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
    last_resumed_at: str
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
                self._close_entry(entry)
                entry = None
            elif entry is not None and (entry.revision != revision or entry.active_session_id != active_session_id):
                rebuild_reason = "disk_revision_changed"
                self._close_entry(entry)
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
                    last_resumed_at=_utc_now_iso(),
                )
                self._record_transition(
                    root_session_id,
                    "cold",
                    "rebuilt" if rebuild_reason else "rehydrated",
                )
            entry.last_used_at = now
            self._entries[root_session_id] = entry
            while len(self._entries) > self.max_entries:
                evicted_root, evicted = self._entries.popitem(last=False)
                self._close_entry(evicted)
                self._record_transition(evicted_root, "cold", "evicted")
            return entry, reused, rebuild_reason

    def finish(self, root_session_id: str, *, active_session_id: str, revision: str) -> None:
        with self._lock:
            entry = self._entries.get(root_session_id)
            if entry is None:
                return
            tip_advanced = entry.active_session_id != active_session_id
            entry.active_session_id = active_session_id
            entry.revision = revision
            entry.last_used_at = time.monotonic()
            entry.turn_count += 1
            self._entries.move_to_end(root_session_id)
            self._record_transition(
                root_session_id,
                "hot",
                "tip_advanced" if tip_advanced else None,
            )

    def transition(self, root_session_id: str, state: str) -> None:
        """Record process-local lifecycle truth for an owning serve observer."""

        if state not in {"cold", "busy", "hot", "failed"}:
            raise ValueError(f"invalid persona chat runtime state: {state}")
        with self._lock:
            self._record_transition(
                root_session_id,
                state,
                "failed" if state == "failed" else None,
            )

    def evict(self, root_session_id: str) -> bool:
        with self._lock:
            entry = self._entries.pop(root_session_id, None)
            removed = entry is not None
            if entry is not None:
                self._close_entry(entry)
            self._record_transition(root_session_id, "cold", "evicted")
            return removed

    def observation(self, root_session_id: str, *, owning_process: bool) -> dict[str, Any]:
        if not owning_process:
            return {
                "runtime_state": "unknown",
                "runtime_observer_id": "external_cli",
                "runtime_observed_at": _utc_now_iso(),
            }
        with self._lock:
            entry = self._entries.get(root_session_id)
            transition = self._transitions.get(root_session_id) or {}
            state = transition.get("state") or ("hot" if entry else "cold")
            return {
                "runtime_state": state,
                "last_runtime_transition": transition.get("transition"),
                "runtime_observer_id": f"serve:{os.getpid()}",
                "runtime_observed_at": _utc_now_iso(),
                "active_session_id": entry.active_session_id if entry else None,
                "last_resumed_at": entry.last_resumed_at if entry else None,
            }

    def _record_transition(
        self, root_session_id: str, state: str, transition: str | None = None
    ) -> None:
        previous = self._transitions.get(root_session_id) or {}
        self._transitions[root_session_id] = {
            "state": state,
            "transition": transition or previous.get("transition"),
        }

    @staticmethod
    def _close_entry(entry: ResidentPersonaChatRuntime) -> None:
        close = getattr(entry.agent, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _evict_expired(self, now: float) -> None:
        expired = [key for key, value in self._entries.items() if now - value.last_used_at > self.ttl_seconds]
        for key in expired:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._close_entry(entry)
            self._record_transition(key, "cold", "evicted")


_REGISTRY: PersonaChatRuntimeRegistry | None = None


def initialize_persona_chat_runtime_registry(
    *, enabled: bool = True, max_entries: int = 8, ttl_seconds: float = 1800.0
) -> PersonaChatRuntimeRegistry | None:
    global _REGISTRY
    _REGISTRY = (
        PersonaChatRuntimeRegistry(max_entries=max_entries, ttl_seconds=ttl_seconds)
        if enabled
        else None
    )
    return _REGISTRY


def persona_chat_runtime_registry() -> PersonaChatRuntimeRegistry | None:
    return _REGISTRY
