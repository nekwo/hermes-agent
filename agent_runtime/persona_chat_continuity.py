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
        — into the session meta as ``_dispatched_from``. It is honoured by the
        mint that CREATES the thread; a replay of the same idempotency key
        ignores it and carries the stored block forward unchanged. It rides the
        SAME meta write as ``mission_chat_root_id`` (one write, one authority)
        rather than a second update, and deliberately does NOT touch
        ``parent_session_id``:
        on the persona-chat lane that column is claimed by native-compression
        lineage (``native_lineage_summary`` raises on a foreign parent, and
        usage aggregation blanks), so borrowing it for relay provenance would
        corrupt both. Marker-key precedent: ``_delegate_from`` / ``_branched_from``.

        Order of operations is load-bearing, not incidental: assert bindable →
        reserve the receipt → BIND → create the session → meta → title. The bind
        used to be last, which is why a ``retire`` landing mid-lane still left a
        titled thread behind for a placement that no longer existed. See the
        early-bind comment below.
        """

        instance_id = safe_assignment_token(persona_instance_id)
        key = safe_assignment_text(idempotency_key, limit=240)
        if not instance_id or not key:
            raise ValueError("persona_instance_id and idempotency_key are required")
        # PRECONDITION, asserted before this lane's FIRST durable write, through
        # the SAME seam the bind itself uses (``assert_bindable`` → one
        # derivation of the target id, one retirement rule, one refusal). It
        # reports the id the seam resolved, not the caller's raw token: a refusal
        # reachable from three sites must not identify its target three ways.
        #
        # It closes the refusal that is TRUE AT ENTRY — the target was already
        # retired when the mint arrived, which is every dispatch at a deleted
        # placement. The narrower race (a ``retire`` landing WHILE this lane
        # runs) is closed by the early bind below, not here.
        #
        # The local ``instance_id`` deliberately stays the CALLER's token: it
        # keys the idempotency receipt path and derives the root session id, and
        # canonicalizing it here would re-key every in-flight receipt — a replay
        # would miss its own reservation and mint a SECOND thread for a task
        # that already has one.
        instance_store.assert_bindable(
            persona_id=persona_id, persona_instance_id=instance_id
        )
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
            # THE replay signal: a receipt that already names a root is one this
            # key established on an earlier pass. Anything a retry must NOT
            # re-derive branches on this fact, never on the shape of the values
            # the retry happened to compute.
            replayed = bool(root)
            if not replayed:
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
            # EARLY BIND. This used to be the LAST step of the lane, and that
            # position was the whole remaining defect: ``retire`` refuses only on
            # a live run/worker binding or an active assignment, and a chat mint
            # held neither until it bound — so a ``retire`` landing after the
            # precondition above still let the lane create the session, write its
            # meta and TITLE it before the bind refused, leaving a titled thread
            # in Mission Control for a placement that no longer exists.
            #
            # Binding first inverts that. The bind is the step that proves the
            # target is still live, so it runs before the first SESSION-visible
            # write; from here on every write belongs to a target this lane has
            # already proven bindable. A ``retire`` that lands after this point
            # archives a row that legitimately owned the thread — its tombstone
            # carries the pointer, and preserved chat history is exactly what
            # ``retire`` promises. No orphan either way.
            #
            # Ordered AFTER the receipt reservation on purpose: the receipt is
            # what makes the whole lane idempotent, it is internal (never a
            # Mission Control row), and reserving first means a crash between the
            # two is repaired by a retry that resolves the same root instead of
            # minting a second one.
            instance = instance_store.open_chat(
                persona_id=persona_id,
                persona_instance_id=instance_id,
                session_id=root,
            )
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
            if replayed:
                # REPLAY: lineage is established ONCE, by the mint that created
                # the thread, and is never recomputed. A retry cannot re-derive
                # it, because both inputs have gone stale in ways that lie:
                #   * the caller's `predecessor` was read from the instance's
                #     default-thread pointer, which this very session has since
                #     BECOME — so on a same-key retry it is a self-reference
                #     (dropped), and after an INTERLEAVED later dispatch it is
                #     that later thread, which this session never superseded.
                #     Recording it would invert the arrow (A claiming it retired
                #     B when A came first).
                #   * `requested_by_session` belongs to whoever asked for the
                #     ORIGINAL mint; the retry's sender is not that fact.
                # And this meta write replaces the stored value wholesale, so
                # anything not carried forward is ERASED. Carry it forward
                # unconditionally: the stored block is the whole truth, and an
                # empty one means the thread was born without lineage. (A mint
                # that died between reserving the receipt and writing this meta
                # therefore keeps no lineage — silence, never a fabricated arrow.)
                lineage = _stored_dispatch_lineage(session_db, root)
            else:
                lineage = _dispatch_lineage_meta(dispatched_from, root=root)
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


#: How long a clarify ticket file is kept before the sweep may prune it. TTL
#: governs GARBAGE COLLECTION ONLY — an expired-but-present ticket still binds
#: (see :class:`PersonaChatClarifyTicketStore`). One rule, no cliff.
CLARIFY_TICKET_TTL_SECONDS = 604_800  # 7 days

#: ``clarify-<12 hex>`` — mirrors the relay id precedent
#: (``agent-relay-<12 hex>``, ``tools/agent_chat_tool.py``). 48 bits is ample:
#: the token is a LOOKUP KEY validated against a stored record, never a
#: capability secret. The session it resolves to is independently re-validated
#: by the handler's existing ``unknown_chat_session`` / ``foreign_chat_session``
#: guards, so guessing a token buys nothing a caller could not already name.
CLARIFY_TOKEN_PREFIX = "clarify-"

#: Ticket lifecycle states. ``open`` → the question is unanswered; ``answered``
#: → some turn landed in its session (with or without an echoed token);
#: ``rebound`` → a later, DIFFERENT turn bound through the same token.
CLARIFY_TICKET_OPEN = "open"
CLARIFY_TICKET_ANSWERED = "answered"
CLARIFY_TICKET_REBOUND = "rebound"


class PersonaChatClarifyTicketStore:
    """Binds a clarify ANSWER to the thread its QUESTION was asked in.

    A child that calls ``clarify`` on the mission-chat lane has its question
    threaded back to the asker as ``clarify_request``. Until this store existed,
    the ONLY thing linking that question to its answer was the enclosing
    ``session_id``, and passing it back was enforced by prompt text alone — so a
    parent that omitted it fell through to ``policy_new_per_dispatch`` and the
    child read a bare choice with no question attached. Continuity that depends
    on a model reproducing an opaque identifier is not continuity; the runtime
    owns the binding now.

    A sidecar keyed store, deliberately NOT session meta:
    :meth:`PersonaChatMintReceiptStore.mint` writes meta WHOLESALE
    (``update_session_meta(root, json.dumps(meta))``), which is exactly why
    ``640131e8c`` had to add carry-forward logic for ``_dispatched_from``. A
    ticket parked in meta would be erased by the next mint against that root.

    **The filename is the digest of the token, never the token itself.** The
    token arrives as a caller-supplied string from a model; interpolating it
    into a path is traversal. Same precedent as the mint receipt store above.
    """

    def _path(self, token: str) -> Path:
        digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        return paths.store_root() / "persona_chat_clarify_tickets" / f"{digest}.json"

    @staticmethod
    def new_token() -> str:
        import uuid

        return f"{CLARIFY_TOKEN_PREFIX}{uuid.uuid4().hex[:12]}"

    def mint(
        self,
        *,
        chat_session_id: str,
        persona_instance_id: str | None = None,
        persona_id: str | None = None,
        asked_by_client_message_id: str | None = None,
        asked_turn_id: str | None = None,
        requested_by_session: str | None = None,
    ) -> str | None:
        """Record a clarify ticket for the question this turn is asking.

        Returns the token to ship down inside ``clarify_request``, or ``None``
        when there is no session to bind to or the write failed. Best-effort by
        construction: a ticket that cannot be written must not fail the turn
        that produced a real reply — the caller degrades to today's precedence,
        which is exactly the pre-token behavior."""

        root = safe_assignment_text(chat_session_id, limit=240)
        if not root:
            return None
        token = self.new_token()
        record = {
            "schema_version": 1,
            "clarify_token": token,
            "chat_session_id": root,
            "persona_instance_id": safe_assignment_token(persona_instance_id) or None,
            "persona_id": safe_assignment_token(persona_id) or None,
            "asked_by_client_message_id": safe_assignment_text(
                asked_by_client_message_id, limit=240
            )
            or None,
            "asked_turn_id": safe_assignment_text(asked_turn_id, limit=240) or None,
            "requested_by_session": safe_assignment_text(requested_by_session, limit=240)
            or None,
            "state": CLARIFY_TICKET_OPEN,
            "created_at": time.time(),
            "answered_at": None,
            "answered_by_client_message_id": None,
            "bound_via": None,
        }
        path = self._path(token)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(path, record)
        except OSError:
            return None
        return token

    def resolve(self, token: str | None) -> dict[str, Any] | None:
        """The stored ticket for *token*, or ``None`` for unknown/unreadable.

        NEVER RAISES. An unknown or GC'd token must DEGRADE (the caller falls
        through to normal precedence and reports ``unknown_token``), never
        refuse: turning a pruned ticket into a hard failure would punish a
        parent that did exactly the right thing."""

        candidate = safe_assignment_text(token, limit=240)
        if not candidate:
            return None
        try:
            record = json.loads(self._path(candidate).read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(record, dict):
            return None
        # A record whose stored token does not match is not this token's ticket:
        # a digest collision or a hand-edited file must never bind a turn onto
        # somebody else's thread.
        if str(record.get("clarify_token") or "") != candidate:
            return None
        return record

    def settle(
        self,
        token: str | None,
        *,
        client_message_id: str | None = None,
        bound_via: str = "clarify_token",
    ) -> dict[str, Any] | None:
        """Mark the ticket answered, idempotently, and report its new state.

        Re-presentation with the SAME ``client_message_id`` (a lease re-entry, a
        relay retry) settles identically — the same replay-signal discipline the
        mint receipt uses. A settle from a DIFFERENT message is a genuine second
        answer and is recorded as ``rebound``: visible, never an error, because
        binding a token to its live thread is always the right outcome. The
        returned dict is the record as it now stands (or ``None`` when the token
        does not resolve)."""

        record = self.resolve(token)
        if record is None:
            return None
        message_id = safe_assignment_text(client_message_id, limit=240) or None
        already = record.get("answered_by_client_message_id")
        if record.get("state") == CLARIFY_TICKET_OPEN:
            record["state"] = CLARIFY_TICKET_ANSWERED
        elif already and message_id and already != message_id:
            record["state"] = CLARIFY_TICKET_REBOUND
        if not already or (message_id and already == message_id):
            record["answered_by_client_message_id"] = message_id or already
            record["answered_at"] = record.get("answered_at") or time.time()
            record["bound_via"] = record.get("bound_via") or str(bound_via)
        try:
            _atomic_json(self._path(str(record.get("clarify_token"))), record)
        except OSError:
            pass
        return record

    def open_ticket_for_session(self, chat_session_id: str | None) -> dict[str, Any] | None:
        """The newest OPEN ticket bound to *chat_session_id*, if any.

        Settlement without a token: any turn landing in a session with an open
        ticket settles it. Without this, every prompt-compliant-via-``session_id``
        parent would leave a permanently-open ticket and the adoption metric
        would lie in the pessimistic direction."""

        root = safe_assignment_text(chat_session_id, limit=240)
        if not root:
            return None
        newest: dict[str, Any] | None = None
        for record in self._iter_records():
            if record.get("state") != CLARIFY_TICKET_OPEN:
                continue
            if str(record.get("chat_session_id") or "") != root:
                continue
            if newest is None or float(record.get("created_at") or 0.0) > float(
                newest.get("created_at") or 0.0
            ):
                newest = record
        return newest

    def sweep(self, *, ttl_seconds: float = CLARIFY_TICKET_TTL_SECONDS) -> int:
        """Prune ticket files older than *ttl_seconds*. Returns the count.

        TTL governs GC only — nothing consults it when deciding whether a token
        binds, so a ticket that survives past its TTL keeps working right up
        until a sweep removes the file. One rule, no cliff."""

        cutoff = time.time() - max(float(ttl_seconds), 0.0)
        pruned = 0
        for record in self._iter_records():
            if float(record.get("created_at") or 0.0) > cutoff:
                continue
            try:
                self._path(str(record.get("clarify_token"))).unlink(missing_ok=True)
            except OSError:
                continue
            pruned += 1
        return pruned

    def _iter_records(self) -> Iterator[dict[str, Any]]:
        root = paths.store_root() / "persona_chat_clarify_tickets"
        try:
            entries = sorted(root.glob("*.json"))
        except OSError:
            return
        for entry in entries:
            try:
                record = json.loads(entry.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(record, dict) and record.get("clarify_token"):
                yield record


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

    FRESH MINTS ONLY — a replay carries the stored block forward instead of
    re-projecting caller arguments that have gone stale (see :meth:`mint`).
    *root* is the session this meta belongs to; a predecessor equal to it is
    dropped through the shared :func:`superseded_session_id` rule, which a
    fresh mint's brand-new id cannot trip but which keeps the one lineage
    projection honest for any caller that hands in the thread itself."""

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
