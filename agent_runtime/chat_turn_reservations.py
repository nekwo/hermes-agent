"""ACCEPT-side exactly-once for the REMOTE chat-turn methods (gateway Stage 3).

The shape is ``agent_create_reservations``' — digest-keyed atomic receipt under
its own cross-process lock, durable BEFORE the work begins, typed error class,
``idempotent_replay`` on the second presentation. What is deliberately NOT
copied is the CLAIM: that module is the authority on whether a create happened.
This one is not the authority on whether a chat turn happened, and saying so is
the whole point of the file.

The finding this module is built around
---------------------------------------
The gateway plan's Stage 3 (and its 2026-08-27 re-verification) records that
mission-chat send has no server-side dedupe — "no ``turn_request_id`` anywhere".
The grep was accurate and the conclusion was wrong. Mission-chat send has
carried exactly-once semantics for months under a different name:
``client_message_id`` plus the per-session TURN JOURNAL
(``persona_commands._mission_chat_busy_outcome``,
``mission_chat_turn_record``). Present the same ``client_message_id`` twice and
the second presentation is answered from the journal — ``idempotent_replay:
True`` with the committed reply once the turn settles,
``chat_turn_duplicate_in_flight`` while it is still running,
``chat_turn_outcome_unknown`` when the provider outcome cannot be proven. That
machinery was built by the 2026-08-24 incident and is richer than anything this
stage would have re-derived.

So ``turn_request_id`` is **not a second key.** The RPC door takes the gateway
plan's word for it and passes the bytes to ``--client-message-id`` unchanged —
no hash, no prefix, no re-mint — so the journal that already owns mission
chat's exactly-once keys on exactly what a remote device sent. One value, one
authority, two spellings across a lane boundary, and the reason for the second
spelling is only that the plan named it before the discovery.

What is genuinely missing, and is all this file adds
----------------------------------------------------
The journal's first write happens INSIDE the chat-root lease, i.e. after a
worker is already executing the turn. The RPC lane cannot execute inline —
``serve.py``'s method lane is answered on the reader loop and a chat turn runs
for seconds to minutes — so it must ACCEPT and hand off. Between the accept and
the journal's first write there is a window in which a duplicate accept sees an
empty journal and spawns a second worker. The second worker is not a second
turn (it loses the lease and is answered ``chat_turn_duplicate_in_flight``), so
correctness was never at risk — but a remote client that lost its ack and
retried would get a fresh ``accepted`` ack and no replay marker, which is the
one fact the outbox on the other end has to branch on.

This receipt closes exactly that window and nothing else. It records the ACCEPT
decision — "this runtime already handed this id to a worker, and here is the
ack it was given" — and replays that ack. It never records a reply, never
records a turn outcome, and never answers a question the journal can answer:

``accepted``
    Spawned. A replay returns the recorded ack with ``idempotent_replay: True``
    and spawns nothing. Whether the turn is still running, has committed, or
    died is the journal's question, and a client asks it by re-presenting the
    same id on the argv/stream lane exactly as a local client does.
``settled``
    The worker's terminal exit was observed and its code is recorded. A replay
    still returns the recorded ack with ``idempotent_replay: True`` — the ack
    is the same ack; the exit code is here so an operator reading the receipt
    directory can tell a turn that ended from one whose serve was killed
    mid-flight, which is the state the acceptance test deliberately produces.

An unknown state is ``reservation_corrupt`` and refuses loudly, on
``agent_create_reservations``' own reasoning: a downgrade must never re-spawn.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .locks import HarnessLockUnavailable, chat_turn_reservation_lock

_SCHEMA_VERSION = 1

#: Handed to a worker. The durable write that makes a retry a replay.
STATE_ACCEPTED = "accepted"
#: The worker's terminal exit was observed.
STATE_SETTLED = "settled"

_VALID_STATES = frozenset({STATE_ACCEPTED, STATE_SETTLED})

#: The cap ``client_message_id`` is already normalised to by
#: ``safe_assignment_text(..., limit=200)`` in the chat handler. Repeated here
#: rather than imported because this module must refuse an oversized key BEFORE
#: it becomes a filename, and the handler's normaliser would silently TRUNCATE
#: one — which would make two distinct remote ids share one receipt.
MAX_TURN_REQUEST_ID_LENGTH = 200


class ChatTurnReservationError(RuntimeError):
    """A typed, fail-closed reservation failure. ``code`` is the branch point."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChatTurnRecord:
    key_digest: str
    verb: str
    session_scope: str
    created_at: str
    updated_at: str
    state: str | None = None
    #: The serve request id the turn's frames carry. A replaying client needs
    #: it to re-attach to the stream lane it was disconnected from — it is the
    #: only field of the ack that a client cannot reconstruct from its own
    #: request.
    request_id: str | None = None
    ack: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None

    @property
    def is_new(self) -> bool:
        return self.state is None


class ChatTurnReservation:
    """One accept decision, held for the microseconds the spawn takes."""

    def __init__(self, record: ChatTurnRecord, *, replayed: bool):
        self.record = record
        #: True when the key was already on disk when the reservation opened.
        self.replayed = replayed

    @property
    def state(self) -> str | None:
        return self.record.state

    def mark_accepted(self, ack: dict[str, Any], *, request_id: str) -> ChatTurnRecord:
        """Durable BEFORE the worker is submitted.

        The ordering is the whole design and it is the pessimistic direction on
        purpose: a crash between this write and the submit leaves a receipt for
        a turn that never ran, and the retry is answered ``idempotent_replay``
        for work that did not happen. That is a HUNG turn, which the client can
        see and resolve (the journal has no record, so the id is re-presentable
        on the argv lane and ``turn-resolve`` exists for the unprovable case).
        The other ordering loses the opposite way — a crash after the submit and
        before the write lets a retry spawn a SECOND execution of an operator's
        message — and a duplicated agent turn is not something a client can
        undo. Between an ack that over-claims and an execution that duplicates,
        this lane takes the over-claim.
        """

        self.record = replace(
            self.record,
            state=STATE_ACCEPTED,
            request_id=str(request_id),
            ack=dict(ack),
            updated_at=_timestamp(),
        )
        _write(self.record)
        return self.record

    def replay_ack(self) -> dict[str, Any]:
        """The recorded ack, stamped as a replay.

        Stamped HERE rather than at the call site so the flag cannot be
        forgotten by a second caller, and copied so a handler that mutates its
        reply cannot edit the receipt in memory.
        """

        payload = dict(self.record.ack)
        payload["idempotent_replay"] = True
        if self.record.request_id:
            payload["request_id"] = self.record.request_id
        if self.record.state == STATE_SETTLED and self.record.exit_code is not None:
            payload["settled"] = True
            payload["exit_code"] = int(self.record.exit_code)
        else:
            payload["settled"] = False
        return payload


def turn_request_digest(turn_request_id: str) -> str:
    """The receipt/lock key. Digested for the same reason the create's is: the
    id is client-chosen text and a filename must not be."""

    return hashlib.sha256(str(turn_request_id).encode("utf-8")).hexdigest()


@contextmanager
def reserve_chat_turn(
    *, turn_request_id: str, verb: str, session_scope: str
) -> Iterator[ChatTurnReservation]:
    """Open (or replay) the accept receipt for one remote chat turn.

    ``session_scope`` is the chat root (or the persona reference a send names
    when it has no session yet). It is validated on replay for
    ``agent_create``'s reason — a key names ONE turn, and re-using it against a
    different root is a client bug that must be refused rather than answered
    with somebody else's ack.
    """

    key = str(turn_request_id or "").strip()
    if not key:
        raise ChatTurnReservationError(
            "turn_request_id_required", "turn_request_id is required"
        )
    if len(key) > MAX_TURN_REQUEST_ID_LENGTH:
        raise ChatTurnReservationError(
            "turn_request_id_invalid",
            "turn_request_id must be "
            f"{MAX_TURN_REQUEST_ID_LENGTH} characters or fewer",
        )
    digest = turn_request_digest(key)
    try:
        with chat_turn_reservation_lock(digest):
            path = paths.chat_turn_reservation_path(digest)
            if path.exists():
                record = _read(path, digest=digest)
                _validate_scope(record, verb=verb, session_scope=session_scope)
                yield ChatTurnReservation(record, replayed=True)
            else:
                timestamp = _timestamp()
                # NOT written yet, exactly as the create's is not: a brand-new
                # key whose params turn out to be invalid must leave no receipt,
                # or a client fixing a typo would be answered with its own stale
                # ack forever. The first durable write is mark_accepted.
                yield ChatTurnReservation(
                    ChatTurnRecord(
                        key_digest=digest,
                        verb=str(verb),
                        session_scope=str(session_scope),
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                    replayed=False,
                )
    except HarnessLockUnavailable as exc:
        raise ChatTurnReservationError(
            "chat_turn_lock_unavailable",
            "another accept for this turn_request_id is still in progress; retry with the same id",
        ) from exc


def settle_chat_turn(*, turn_request_id: str, exit_code: int) -> bool:
    """Record a worker's terminal exit against its accept receipt.

    Best-effort by contract and NEVER raises: this runs in the serve worker's
    ``finally``, where an exception would replace a turn's real exit frame with
    a bookkeeping failure. A receipt that never got its exit code reads as
    ``accepted`` forever, which is the same thing a killed serve leaves behind
    and is therefore already a state every reader handles.
    """

    try:
        digest = turn_request_digest(str(turn_request_id or "").strip())
        path = paths.chat_turn_reservation_path(digest)
        if not path.exists():
            return False
        with chat_turn_reservation_lock(digest):
            if not path.exists():
                return False
            record = _read(path, digest=digest)
            _write(
                replace(
                    record,
                    state=STATE_SETTLED,
                    exit_code=int(exit_code),
                    updated_at=_timestamp(),
                )
            )
        return True
    except Exception:
        return False


def forget_chat_turn(turn_request_id: str) -> bool:
    """Delete an accept receipt for a turn that was NOT spawned after all.

    Called on exactly one path: the transport refused the spawn (a drain), which
    is a decision this process made after the durable write and can therefore
    still undo. It is not a general un-accept — a receipt whose worker actually
    started must never be removed, or a retry becomes a second execution of an
    operator's message.

    Never raises, for :func:`settle_chat_turn`'s reason: the caller is already
    rendering a refusal and must not have it replaced by a bookkeeping failure.
    A receipt that could not be removed reads as ``accepted`` for a turn that
    never ran, which is the same recoverable over-claim a crash between the
    write and the submit leaves — see :meth:`ChatTurnReservation.mark_accepted`.
    """

    try:
        digest = turn_request_digest(str(turn_request_id or "").strip())
        paths.chat_turn_reservation_path(digest).unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _validate_scope(
    record: ChatTurnRecord, *, verb: str, session_scope: str
) -> None:
    if record.verb == str(verb) and record.session_scope == str(session_scope):
        return
    raise ChatTurnReservationError(
        "turn_request_conflict",
        "turn_request_id was already used for a different chat verb or chat root",
    )


def _read(path, *, digest: str) -> ChatTurnRecord:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("schema_version") or 0) != _SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        state = str(raw.get("state") or "")
        if state not in _VALID_STATES:
            raise ValueError("invalid state")
        exit_raw = raw.get("exit_code")
        record = ChatTurnRecord(
            key_digest=str(raw["turn_request_id_sha256"]),
            verb=str(raw["verb"]),
            session_scope=str(raw["session_scope"]),
            state=state,
            request_id=raw.get("request_id") or None,
            ack=dict(raw.get("ack") or {}),
            exit_code=int(exit_raw) if isinstance(exit_raw, int) else None,
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
        )
        if record.key_digest != digest:
            raise ValueError("key digest does not match receipt path")
        if not all((record.verb, record.session_scope, record.created_at)):
            raise ValueError("required field is blank")
        return record
    except ChatTurnReservationError:
        raise
    except Exception as exc:
        raise ChatTurnReservationError(
            "reservation_corrupt", f"chat-turn reservation is unreadable: {exc}"
        ) from exc


def _write(record: ChatTurnRecord) -> None:
    atomic_json_write(
        paths.chat_turn_reservation_path(record.key_digest),
        {
            "schema_version": _SCHEMA_VERSION,
            # The KEY is a digest — the filename and this field both — because
            # a client-chosen string must never become a path component. It is
            # NOT a claim that the id is absent from the file: ``ack`` is
            # recorded verbatim so the replay is byte-identical to the original
            # accept, and that ack echoes the ``turn_request_id`` the client
            # itself sent and is waiting to see. Digesting the key and echoing
            # the ack are answers to two different questions, and conflating
            # them would mean either an unsafe filename or a replay that
            # returned something the client did not send.
            "turn_request_id_sha256": record.key_digest,
            "verb": record.verb,
            "session_scope": record.session_scope,
            "state": record.state,
            "request_id": record.request_id,
            "ack": record.ack,
            "exit_code": record.exit_code,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        },
        indent=2,
        sort_keys=True,
    )


def _timestamp() -> str:
    return now().isoformat().replace("+00:00", "Z")
