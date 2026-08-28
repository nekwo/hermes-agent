"""Durable store for DETACHED agent-to-agent dispatches (``wait: false``).

A dispatch is one ``agent_chat_send(wait=false)``: the sender's turn returns
immediately with a handle, the target's turn runs on a background executor, and
the reply is delivered back into the SENDER's chat session later as a forged
turn when that session is idle (see :mod:`agent_runtime.dispatch_delivery`).

Why a durable store and not an in-memory record map
---------------------------------------------------
The whole value of the lane is that the sender does not have to sit still. That
means the completion can land while the sender is mid-turn, and it means the
process can die between "the target answered" and "the sender was told". An
in-memory handle loses the answer in both cases, silently — the sender simply
never hears back and has no way to tell that from "they are still working".

So this reuses the ``async_delegation`` durable protocol WHOLESALE rather than
inventing a second one:

* ``delivery_state`` ∈ ``pending | delivered | dropped`` — the row's own record
  of whether the sender has actually been told.
* An expiring **claim** (:data:`CLAIM_EXPIRY_SECONDS`) so competing consumers —
  and, after a crash, the same consumer on the next boot — cannot double-deliver
  a completion or strand one behind a dead claimant.
* An attempt cap (:data:`MAX_DELIVERY_ATTEMPTS`) so an undeliverable row
  converges to a terminal ``dropped`` instead of replaying on every restart
  forever.
* Owner **PID + process start time**, because a bare PID is not identity: the
  kernel recycles numbers, and this repo has already been bitten by a recycled
  one landing on an unrelated process.
* Restore-on-boot stamps ``restored=True`` and requires POSITIVE ownership proof
  before delivery (#64484): a row restored from a previous process names the
  chat root it must be delivered into, and the drain re-proves that root is a
  real, current chat session before forging anything into it. Absence of
  disproof is not proof.

Where the database lives — and why that is not ``get_hermes_home()``
--------------------------------------------------------------------
``persona_profile_context`` flips ``HERMES_HOME`` PROCESS-GLOBALLY for the
duration of a persona turn, and a dispatch is *made from inside* such a turn.
Resolving the database through the ambient home at write time would persist an
in-flight dispatch into the persona profile's database — which the serve drain,
the Activity projection and the operator never open. That is the exact failure
:func:`hermes_constants.get_hermes_background_work_home` was extracted to close,
so this module resolves through that ONE authority and never re-derives it.

Every mutation emits a registered EventLog event
-------------------------------------------------
``dispatch.recorded`` / ``dispatch.completed`` / ``dispatch.delivered`` /
``dispatch.dropped``. This is the standing store rule, not decoration: the
stream and read-model pipeline are watermark-gated on the EventLog, so a store
write with no event is invisible to every consumer until an unrelated event
happens to advance the offset. Payloads are bounded well inside the 4096-byte
cap — summaries, never transcripts.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "CLAIM_EXPIRY_SECONDS",
    "DELIVERY_DELIVERED",
    "DELIVERY_DROPPED",
    "DELIVERY_PENDING",
    "DROP_REASON_ATTEMPT_CAP",
    "DROP_REASON_FORGE_REJECTED",
    "MAX_DELIVERY_ATTEMPTS",
    "REMOTE_UNREACHABLE_REASON",
    "REARM_ALREADY_DELIVERED",
    "REARM_NOT_DROPPED",
    "REARM_NOT_FOUND",
    "REARM_REARMED",
    "STATE_ERROR",
    "STATE_RUNNING",
    "STATE_UNKNOWN",
    "TERMINAL_STATES",
    "claim_delivery",
    "dispatch_db_path",
    "list_dispatches",
    "mark_delivered",
    "mint_dispatch_id",
    "pending_deliveries",
    "rearm_delivery",
    "record_completion",
    "record_dispatch",
    "release_delivery_claim",
    "restore_undelivered_dispatches",
    "running_dispatches",
    "undeliverable_dispatches",
    "set_dispatch_owner",
]

_TABLE = "mission_chat_dispatches"
_DB_LOCK = threading.Lock()

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_DROPPED = "dropped"

STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_ERROR = "error"
STATE_UNKNOWN = "unknown"

#: The states a dispatch can END in. ``running`` is the only non-terminal one;
#: everything else is a completion the sender is owed an answer about, including
#: ``error`` and ``unknown`` — "it failed" and "nobody knows" are results a
#: waiting agent must receive, not rows to quietly bury.
TERMINAL_STATES = (STATE_COMPLETED, STATE_ERROR, STATE_UNKNOWN)

#: How long a delivery claim is honoured before another consumer may take it.
#: Same 300 s the delegation lane uses: long enough that a slow forge is not
#: raced, short enough that a claimant killed mid-delivery does not strand the
#: completion until the next reboot.
CLAIM_EXPIRY_SECONDS = 300.0
#: Terminal after this many failed delivery attempts. An unroutable row (its
#: chat root deleted, say) must converge instead of replaying forever.
MAX_DELIVERY_ATTEMPTS = 8

#: Why the attempt cap gave up, recorded on the ROW and not only on the event.
#: One constant because the two must never drift: the Activity projection reads
#: the row and the operator reads the projection, so a reason that lives only in
#: the event log renders as the useless "undelivered".
DROP_REASON_ATTEMPT_CAP = "attempt_cap"

#: Gateway Stage 7 (R8). Why a cross-install dispatch gave up: every attempt to
#: reach the paired install was a TRANSPORT failure, so the target never
#: answered and never refused.
#:
#: **This is the one place Stage 7 knowingly departs from R8's wording, and the
#: departure is what makes the ruling keep its own promise.** R8 says an offline
#: target "converges to ``dropped``". ``dropped`` is a DELIVERY state and it
#: means *the sender was never told* — which is exactly backwards for this case,
#: because "the other machine is not answering" is the single most useful thing
#: the sending agent could be told, and a ``dropped`` row is indistinguishable
#: in the Activity panel from a dispatch that evaporated. So the attempts CAP is
#: R8's, unchanged and spelled from the same
#: :data:`MAX_DELIVERY_ATTEMPTS` constant rather than a second number, and what
#: it converges to is a terminal ``error`` completion carrying this reason —
#: which then travels the ordinary delivery lane and lands in the sender's chat.
#: R8's naming half is honoured on the row, in the ``dispatch.completed`` event
#: and in the sentence the sender reads.
REMOTE_UNREACHABLE_REASON = "peer_unreachable"

#: Prefix for the OTHER terminal give-up: the forge refused this row for a
#: DETERMINISTIC reason (a guard verdict — foreign root, unknown persona,
#: retired instance). Spelled ``forge_rejected:<error_kind>`` on the row, so the
#: Activity panel renders the actual verdict instead of ``attempt_cap`` — which
#: is what an operator saw for 40 seconds' worth of eight identical refusals.
DROP_REASON_FORGE_REJECTED = "forge_rejected"

#: Outcomes of :func:`rearm_delivery`. A verb refusing a row must be able to say
#: WHICH refusal it was, so these are values rather than a bare ``False``.
REARM_REARMED = "rearmed"
REARM_NOT_FOUND = "not_found"
REARM_ALREADY_DELIVERED = "already_delivered"
REARM_NOT_DROPPED = "not_dropped"

#: The wire ``error_kind`` for each re-arm refusal, and for a store that could
#: not be read at all. Declared HERE, beside the outcomes they name, because
#: this module owns them — the CLI verb reads this table instead of re-spelling
#: the values, exactly as ``relay_policy`` and ``target_policy`` own theirs.
#: Registered for the record in
#: ``mission_chat_outcome.DELEGATED_ERROR_KIND_SOURCES``.
REARM_ERROR_KINDS = {
    REARM_NOT_FOUND: "dispatch_not_found",
    REARM_ALREADY_DELIVERED: "dispatch_already_delivered",
    REARM_NOT_DROPPED: "dispatch_not_dropped",
}
ERROR_KIND_DISPATCH_STORE_UNAVAILABLE = "dispatch_store_unavailable"

#: Bound on the stored ask/reply text. The reply bound matches the relay tool's
#: own ``_REPLY_LIMIT`` (``tools/agent_chat_tool.py``) and the delivery lane's
#: ``dispatch_delivery.REPLY_LIMIT``; nothing downstream ever needs more, and the
#: store is not a transcript (the thread itself is, and the row points at it).
#: The three spellings are FENCED, not merely documented:
#: ``tests/agent_runtime/test_mirrored_constant_fences.py`` asserts they are
#: equal, because three bounds that disagree truncate one answer twice.
ASK_LIMIT = 4000
REPLY_LIMIT = 8000

_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_RETAINED_TERMINAL = 200


def mint_dispatch_id() -> str:
    """A dispatch handle. Short, opaque, and stable across processes."""

    return f"dispatch-{uuid.uuid4().hex[:12]}"


def dispatch_db_path() -> Path:
    """The store's database — ONE authority, resolved through ONE resolver.

    Deliberately the same ``state.db`` the delegation lane writes: one
    background-work database per home, two tables. A second file would need its
    own fingerprint entry in the serve read-model cache and its own backup
    story, for no isolation this lane actually needs.
    """

    from hermes_constants import get_hermes_background_work_home

    return Path(get_hermes_background_work_home()) / "state.db"


def _connect() -> sqlite3.Connection:
    path = dispatch_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (mission_chat_dispatches)")
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
            dispatch_id TEXT PRIMARY KEY,
            sender_session_id TEXT NOT NULL DEFAULT '',
            sender_persona_id TEXT NOT NULL DEFAULT '',
            target_persona TEXT NOT NULL DEFAULT '',
            target_instance_id TEXT NOT NULL DEFAULT '',
            target_session_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            ask TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            notify_operator INTEGER NOT NULL DEFAULT 0,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            relay_chain_json TEXT,
            delivery_error TEXT,
            remote_install_id TEXT NOT NULL DEFAULT ''
        )"""
    )
    # CREATE TABLE IF NOT EXISTS does nothing to a store that already exists, so
    # a column added after the first release needs an explicit migration — or it
    # is absent on every machine that ever ran the older build, and the reads
    # below raise there while a fresh install stays green. That is the worst
    # possible place to discover a schema change.
    _add_missing_column(conn, "delivery_error", "TEXT")
    # Gateway Stage 7. A column and not a key inside ``result_json``, because
    # this fact is true from the moment the row is WRITTEN and the result blob
    # is not written until the turn ends: an operator watching Activity has to
    # be able to see that a dispatch left this machine while it is still
    # running, which is precisely the window in which they would otherwise
    # wonder why nothing is happening. Empty string means local, which is what
    # every row that predates this line already means.
    _add_missing_column(conn, "remote_install_id", "TEXT NOT NULL DEFAULT ''")


def _add_missing_column(conn: sqlite3.Connection, name: str, decl: str) -> None:
    try:
        existing = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({_TABLE})")
        }
    except Exception:  # pragma: no cover - defensive
        return
    if name in existing:
        return
    try:
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {decl}")
    except sqlite3.OperationalError:  # pragma: no cover - lost a concurrent race
        pass


class _transaction:
    """Open, commit/rollback, and ALWAYS close.

    ``sqlite3.Connection.__exit__`` commits the transaction but does NOT close
    the connection, so ``with _connect()`` leaks a handle — and its WAL/SHM file
    descriptors — on every write, deferring the close to the garbage collector.
    On a long-running serve process that eventually exhausts the fd limit; the
    delegation lane learned this the expensive way (#69567).
    """

    def __enter__(self) -> sqlite3.Connection:
        self._conn = _connect()
        self._conn.__enter__()
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._conn.__exit__(exc_type, exc, tb)
        finally:
            self._conn.close()


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[:limit]


def _supervised_here() -> set[str]:
    """Dispatch ids a live supervisor in THIS process is still answering for.

    Read through the tools lane's own registry rather than duplicated here, so
    "who owns this row right now" has exactly one answer. Imported lazily and
    fails open to an empty set: a store that cannot see the registry sweeps
    exactly as it did before, which is the previous behaviour rather than a new
    hazard.
    """

    try:
        from tools.agent_chat_dispatch import supervised_dispatch_ids

        return supervised_dispatch_ids()
    except Exception:  # pragma: no cover - defensive
        return set()


def _owner_identity() -> tuple[int, int | None]:
    """``(pid, start_ticks)`` for THIS process.

    The start time is what turns a PID into an identity. A ``None`` here is an
    unreadable probe, not a claim of any kind, and every consumer treats it as
    "cannot prove" rather than "not ours".
    """

    try:
        from gateway.status import get_process_start_time

        return os.getpid(), get_process_start_time(os.getpid())
    except Exception:  # pragma: no cover - defensive
        return os.getpid(), None


def _emit(event_type: str, **payload: Any) -> None:
    """Append the store event for a mutation. Best effort, never silent.

    Mirrors ``agent_runtime.store._append_store_event``: the EventLog is the
    change feed every watermark-gated consumer reads, so a mutation without one
    is invisible; but a broken event log must not fail the durable write that
    already happened.
    """

    try:
        from hermes_time import now

        from .events import EventLog
        from .models import Event

        body = {key: value for key, value in payload.items() if value is not None}
        EventLog().append(Event(now(), event_type, None, None, None, body))
    except Exception:
        logger.warning("dispatch store event append failed: %s", event_type, exc_info=True)


def _row_to_dict(row: tuple) -> dict[str, Any]:
    (
        dispatch_id,
        sender_session_id,
        sender_persona_id,
        target_persona,
        target_instance_id,
        target_session_id,
        title,
        ask,
        state,
        notify_operator,
        dispatched_at,
        completed_at,
        updated_at,
        result_json,
        delivery_state,
        delivery_attempts,
        delivered_at,
        owner_pid,
        owner_started_at,
        relay_chain_json,
        delivery_error,
        remote_install_id,
    ) = row
    try:
        result = json.loads(result_json) if result_json else None
    except Exception:
        result = None
    try:
        relay_chain = json.loads(relay_chain_json) if relay_chain_json else []
    except Exception:
        relay_chain = []
    return {
        "dispatch_id": dispatch_id,
        "sender_session_id": sender_session_id or "",
        "sender_persona_id": sender_persona_id or "",
        "target_persona": target_persona or "",
        "target_instance_id": target_instance_id or "",
        "target_session_id": target_session_id or "",
        "title": title or "",
        "ask": ask or "",
        "state": state or "",
        "notify_operator": bool(notify_operator),
        "dispatched_at": float(dispatched_at or 0.0),
        "completed_at": float(completed_at) if completed_at else None,
        "updated_at": float(updated_at or 0.0),
        "result": result,
        "delivery_state": delivery_state or DELIVERY_PENDING,
        "delivery_attempts": int(delivery_attempts or 0),
        "delivered_at": float(delivered_at) if delivered_at else None,
        "owner_pid": int(owner_pid) if owner_pid else None,
        "owner_started_at": int(owner_started_at) if owner_started_at else None,
        "relay_chain": relay_chain if isinstance(relay_chain, list) else [],
        "delivery_error": delivery_error or "",
        # Empty string, never None: "this dispatch is local" is a fact every row
        # carries, and a consumer that had to tell absent from empty would be
        # deciding what a row written before Stage 7 meant.
        "remote_install_id": remote_install_id or "",
    }


_SELECT = f"""SELECT dispatch_id, sender_session_id, sender_persona_id, target_persona,
                     target_instance_id, target_session_id, title, ask, state,
                     notify_operator, dispatched_at, completed_at, updated_at,
                     result_json, delivery_state, delivery_attempts, delivered_at,
                     owner_pid, owner_started_at, relay_chain_json, delivery_error,
                     remote_install_id
              FROM {_TABLE}"""


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def record_dispatch(
    *,
    dispatch_id: str,
    sender_session_id: str,
    sender_persona_id: str = "",
    target_persona: str,
    target_instance_id: str = "",
    title: str = "",
    ask: str = "",
    notify_operator: bool = False,
    relay_chain: Any = None,
    dispatched_at: float | None = None,
    remote_install_id: str = "",
) -> dict[str, Any]:
    """Persist a dispatch BEFORE its target turn starts.

    Order matters and is not negotiable: the row exists first, so a process that
    dies one instruction into the target's turn leaves a record the boot sweep
    can classify as ``unknown`` and still tell the sender about. A row written
    after the fact would lose exactly the dispatches most worth reporting.
    """

    now_epoch = float(dispatched_at if dispatched_at is not None else time.time())
    pid, started = _owner_identity()
    row = {
        "dispatch_id": str(dispatch_id),
        "sender_session_id": _text(sender_session_id, 240),
        "sender_persona_id": _text(sender_persona_id, 160),
        "target_persona": _text(target_persona, 160),
        "target_instance_id": _text(target_instance_id, 200),
        "title": _text(title, 200),
        "ask": _text(ask, ASK_LIMIT),
        "notify_operator": bool(notify_operator),
        "dispatched_at": now_epoch,
        "remote_install_id": _text(remote_install_id, 128),
    }
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            f"""INSERT OR REPLACE INTO {_TABLE}
                (dispatch_id, sender_session_id, sender_persona_id, target_persona,
                 target_instance_id, target_session_id, title, ask, state,
                 notify_operator, dispatched_at, updated_at, delivery_state,
                 delivery_attempts, owner_pid, owner_started_at, relay_chain_json,
                 remote_install_id)
                VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (
                row["dispatch_id"],
                row["sender_session_id"],
                row["sender_persona_id"],
                row["target_persona"],
                row["target_instance_id"],
                row["title"],
                row["ask"],
                STATE_RUNNING,
                1 if row["notify_operator"] else 0,
                now_epoch,
                now_epoch,
                DELIVERY_PENDING,
                pid,
                started,
                json.dumps(list(relay_chain or [])),
                row["remote_install_id"],
            ),
        )
    _emit(
        "dispatch.recorded",
        dispatch_id=row["dispatch_id"],
        target_persona=row["target_persona"],
        sender_session_id=row["sender_session_id"],
        notify_operator=row["notify_operator"],
        title=row["title"][:120] or None,
        # Optional on the contract and present only when true, so a local
        # dispatch's event stays byte-identical to what it has always been.
        remote_install_id=row["remote_install_id"] or None,
    )
    return row


def record_completion(
    dispatch_id: str,
    *,
    state: str,
    reply: str = "",
    error: str = "",
    target_session_id: str = "",
    total_tokens: Any = None,
    visibility: dict[str, Any] | None = None,
    remote: dict[str, Any] | None = None,
    only_if_running: bool = False,
) -> bool:
    """Record the target turn's outcome and arm the row for delivery.

    ``delivery_state`` is (re)set to ``pending`` here rather than at dispatch
    time so a row only becomes deliverable once there is something to deliver —
    EXCEPT on a row already marked ``delivered``, which is never re-armed. The
    sender has been told; re-arming would queue a second delivery of the same
    dispatch, and the only thing standing between that and a duplicate message
    is a replay cache that can rotate or be compressed away.

    ``only_if_running`` scopes the write to a row still in flight. The GUESSING
    writer — the orphan sweep, which infers ``unknown`` from a dead PID — passes
    it; the supervisor that actually watched the turn does not. That ordering is
    the point: the supervisor's observed outcome must always beat the sweep's
    inference, never the other way round.

    ``visibility`` is the target turn's typed ``TurnVisibility`` block
    (``agent_runtime.turn_visibility``), passed only by the writer that HELD the
    child's payload — nobody else can know it. It rides the result blob, so
    absent stays absent and there is no schema to migrate: a row written by an
    older process simply carries no block, and the delivery formatter falls back
    to the generic wording. Without it, "they answered with nothing" and "their
    answer never reached us" are the same empty string to the sender.

    ``remote`` (Stage 7) rides the same blob for the same reason, and carries
    ``{install_id, attempts, reason?}`` — how many dial attempts the
    cross-install leg spent and, when it gave up,
    :data:`REMOTE_UNREACHABLE_REASON`. It is on the RESULT rather than in a
    column because it is only knowable once the leg is finished, which is the
    line ``visibility`` already drew: the column beside it
    (``remote_install_id``) carries the half that is true at dispatch time.
    """

    settled = str(state or STATE_UNKNOWN)
    if settled not in TERMINAL_STATES:
        settled = STATE_UNKNOWN
    now_epoch = time.time()
    result = {
        "status": settled,
        "reply": _text(reply, REPLY_LIMIT),
        "error": _text(error, 600),
        "target_session_id": _text(target_session_id, 240),
        "total_tokens": total_tokens,
    }
    if isinstance(visibility, dict) and visibility:
        result["visibility"] = dict(visibility)
    if isinstance(remote, dict) and remote:
        result["remote"] = dict(remote)
    guard = " AND state=?" if only_if_running else ""
    params: list[Any] = [
        settled,
        now_epoch,
        now_epoch,
        json.dumps(result),
        result["target_session_id"],
        # CASE WHEN delivery_state=<delivered> THEN <delivered> ELSE <pending>
        DELIVERY_DELIVERED,
        DELIVERY_DELIVERED,
        DELIVERY_PENDING,
        # …and the two CASE guards that leave a delivered row's bookkeeping
        # untouched while resetting a re-armed one's.
        DELIVERY_DELIVERED,
        DELIVERY_DELIVERED,
        str(dispatch_id),
    ]
    if only_if_running:
        params.append(STATE_RUNNING)
    with _DB_LOCK, _transaction() as conn:
        # Read the row's PRIOR verdict inside the same transaction, purely to
        # notice a specific silence: a second writer landing a DIFFERENT outcome
        # on a row the sender was already told about. The re-arm guard makes
        # that harmless to the sender (no second delivery), which is exactly why
        # it would otherwise leave no trace at all — and it is the observable
        # symptom of the supervised-id registry being process-local, so it wants
        # a name rather than to be silently absorbed.
        prior = conn.execute(
            f"SELECT state, delivery_state FROM {_TABLE} WHERE dispatch_id=?",
            (str(dispatch_id),),
        ).fetchone()
        cur = conn.execute(
            f"""UPDATE {_TABLE} SET state=?, completed_at=?, updated_at=?, result_json=?,
                   target_session_id=?,
                   delivery_state=CASE WHEN delivery_state=? THEN ? ELSE ? END,
                   delivery_claim=NULL, delivery_claimed_at=NULL,
                   -- Re-arming clears the PREVIOUS give-up. A row dropped by the
                   -- attempt cap keeps its exhausted counter and its drop reason
                   -- otherwise, so the very next claim re-drops it instantly —
                   -- and the operator reads a stale "attempt_cap" against a
                   -- delivery that never got an attempt. Scoped to rows actually
                   -- being re-armed: a delivered row is left exactly as it is.
                   delivery_attempts=CASE WHEN delivery_state=? THEN delivery_attempts ELSE 0 END,
                   delivery_error=CASE WHEN delivery_state=? THEN delivery_error ELSE NULL END
                WHERE dispatch_id=?{guard}""",
            tuple(params),
        )
        updated = cur.rowcount == 1
    if updated and prior is not None:
        prior_state, prior_delivery = prior
        if prior_delivery == DELIVERY_DELIVERED and str(prior_state or "") != settled:
            _emit(
                "dispatch.outcome_superseded",
                dispatch_id=str(dispatch_id),
                previous=str(prior_state or ""),
                settled=settled,
            )
    if updated:
        _emit(
            "dispatch.completed",
            dispatch_id=str(dispatch_id),
            status=settled,
            reply_chars=len(result["reply"]),
            error=result["error"][:200] or None,
            target_session_id=result["target_session_id"] or None,
            # Both optional and both absent on a local dispatch, so its event
            # stays exactly the bytes it has always been.
            remote_install_id=(remote or {}).get("install_id") or None,
            remote_reason=(remote or {}).get("reason") or None,
        )
    _prune()
    return updated


def claim_delivery(dispatch_id: str, claim_id: str) -> bool:
    """Take exclusive, EXPIRING ownership of one pending delivery.

    Returns ``True`` only for the consumer that won the claim. A claim older
    than :data:`CLAIM_EXPIRY_SECONDS` is takeable — that is what stops a
    claimant killed mid-forge from stranding the completion forever.

    The attempt counter increments HERE, on the claim, not on success: a
    consumer that crashes between claiming and delivering must still burn an
    attempt, or an input that reliably kills the drain would be retried
    infinitely and never converge to ``dropped``.
    """

    now_epoch = time.time()
    claimed = False
    dropped = False
    attempts = 0
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            f"SELECT delivery_state, delivery_attempts FROM {_TABLE} WHERE dispatch_id=?",
            (str(dispatch_id),),
        ).fetchone()
        if row is None:
            return False
        state, attempts = row
        if state != DELIVERY_PENDING:
            return False
        if int(attempts or 0) >= MAX_DELIVERY_ATTEMPTS:
            conn.execute(
                f"""UPDATE {_TABLE} SET delivery_state=?, updated_at=?, delivery_claim=NULL,
                       delivery_claimed_at=NULL, delivery_error=?
                    WHERE dispatch_id=?""",
                (DELIVERY_DROPPED, now_epoch, DROP_REASON_ATTEMPT_CAP, str(dispatch_id)),
            )
            dropped = True
        else:
            cur = conn.execute(
                f"""UPDATE {_TABLE} SET delivery_claim=?, delivery_claimed_at=?,
                       delivery_attempts=delivery_attempts+1, updated_at=?
                    WHERE dispatch_id=? AND delivery_state=?
                      AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
                (
                    str(claim_id),
                    now_epoch,
                    now_epoch,
                    str(dispatch_id),
                    DELIVERY_PENDING,
                    now_epoch - CLAIM_EXPIRY_SECONDS,
                ),
            )
            claimed = cur.rowcount == 1
    if dropped:
        _emit(
            "dispatch.dropped",
            dispatch_id=str(dispatch_id),
            reason=DROP_REASON_ATTEMPT_CAP,
            attempts=int(attempts or 0),
        )
        return False
    return claimed


def release_delivery_claim(dispatch_id: str, *, refund_attempt: bool = False) -> None:
    """Hand a claimed-but-undelivered row back for a later attempt.

    Used when the sender is BUSY: the completion is fine, the moment is wrong,
    and holding the claim would block the next drain pass for the whole expiry
    window.

    ``refund_attempt`` un-counts the attempt the claim burned, and exists for one
    specific and entirely real class: the sender took its chat-root lease between
    the drain's idle probe and the forge, so the handler answered ``chat_busy``.
    NOTHING failed there — racing a live operator is the system working — but the
    attempt counter cannot tell, and eight unlucky races would have marched a
    perfectly deliverable completion to a terminal ``dropped`` with no failure
    anywhere in its history. Attempts count real failures only; the cap exists to
    converge genuinely undeliverable rows, not to time out busy ones.
    """

    with _DB_LOCK, _transaction() as conn:
        if refund_attempt:
            conn.execute(
                f"""UPDATE {_TABLE}
                    SET delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?,
                        delivery_attempts=MAX(delivery_attempts-1, 0)
                    WHERE dispatch_id=? AND delivery_state=?""",
                (time.time(), str(dispatch_id), DELIVERY_PENDING),
            )
        else:
            conn.execute(
                f"""UPDATE {_TABLE} SET delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                    WHERE dispatch_id=? AND delivery_state=?""",
                (time.time(), str(dispatch_id), DELIVERY_PENDING),
            )


def set_dispatch_owner(
    dispatch_id: str, *, owner_pid: int | None, owner_started_at: int | None
) -> bool:
    """Re-point a running row's owner identity at the process actually doing the work.

    A dispatch is RECORDED by the tool (in the sender's process) and then
    EXECUTED by a child process. The row's owner has to become the child, because
    "is this dispatch still running?" is a question about the process running the
    turn — not about the supervisor thread waiting on it, and not about the
    sender that asked. Stamping the child here is what keeps the orphan sweep
    coherent when the supervisor itself dies: the row settles when the CHILD is
    gone, which is exactly when the work is gone.

    Only ``running`` rows move. A completion that arrived first wins — this is
    bookkeeping, and it must never resurrect a settled row.
    """

    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            f"""UPDATE {_TABLE} SET owner_pid=?, owner_started_at=?, updated_at=?
                WHERE dispatch_id=? AND state=?""",
            (
                int(owner_pid) if owner_pid else None,
                int(owner_started_at) if owner_started_at else None,
                time.time(),
                str(dispatch_id),
                STATE_RUNNING,
            ),
        )
        return cur.rowcount == 1


def mark_delivered(dispatch_id: str) -> bool:
    """Atomically acknowledge that the sender was actually told."""

    now_epoch = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            f"""UPDATE {_TABLE} SET delivery_state=?, delivered_at=?, updated_at=?,
                   delivery_claim=NULL, delivery_claimed_at=NULL
                WHERE dispatch_id=? AND delivery_state!=?""",
            (
                DELIVERY_DELIVERED,
                now_epoch,
                now_epoch,
                str(dispatch_id),
                DELIVERY_DELIVERED,
            ),
        )
        delivered = cur.rowcount == 1
    if delivered:
        _emit("dispatch.delivered", dispatch_id=str(dispatch_id))
    return delivered


def drop_delivery(dispatch_id: str, *, reason: str) -> bool:
    """Terminally give up on delivering a row, with the reason recorded."""

    now_epoch = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            f"""UPDATE {_TABLE} SET delivery_state=?, updated_at=?, delivery_claim=NULL,
                   delivery_claimed_at=NULL, delivery_error=?
                WHERE dispatch_id=? AND delivery_state=?""",
            (
                DELIVERY_DROPPED,
                now_epoch,
                _text(reason, 200),
                str(dispatch_id),
                DELIVERY_PENDING,
            ),
        )
        dropped = cur.rowcount == 1
    if dropped:
        _emit("dispatch.dropped", dispatch_id=str(dispatch_id), reason=str(reason)[:120])
    return dropped


def rearm_delivery(dispatch_id: str) -> tuple[str, dict[str, Any] | None]:
    """Put a DROPPED delivery back in the queue. Returns ``(outcome, row)``.

    The operator's way back from a terminal give-up. A dropped row is not a lost
    answer — the reply is still durable on the row and the sender still has not
    been told — so once the reason it was refused is FIXED (a deployed guard fix,
    a re-opened chat root), the delivery deserves another pass rather than a
    hand-written re-send.

    The re-arm is byte-for-byte the one ``record_completion`` already performs
    when a second outcome lands on a dropped row (see the ``delivery_attempts`` /
    ``delivery_error`` CASE arms there): back to ``pending``, counter to zero,
    previous give-up cleared, claim released. Both spellings must stay identical
    — a re-arm that kept the exhausted counter is re-dropped by the very next
    claim, and the operator then reads a stale reason against a delivery that
    never got an attempt.

    Refuses by NAME rather than silently: ``not_found``, ``already_delivered``
    (the sender was told; re-arming would deliver a second copy) and
    ``not_dropped`` (a pending row is already queued and needs nothing).

    Deliberately emits no EventLog row. ``dispatch.delivery_rearmed`` would be a
    new registered contract, and the registry's hash is stamped on every live
    persona instance — a migration this repair verb has no business forcing. The
    mutation is not silent regardless: the operator ran the verb and reads its
    envelope, and the next drain pass emits the real ``dispatch.delivered`` or
    ``dispatch.dropped`` for the same row.
    """

    now_epoch = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            f"SELECT delivery_state FROM {_TABLE} WHERE dispatch_id=?",
            (str(dispatch_id),),
        ).fetchone()
        if row is None:
            return REARM_NOT_FOUND, None
        state = str(row[0] or DELIVERY_PENDING)
        if state == DELIVERY_DELIVERED:
            outcome = REARM_ALREADY_DELIVERED
        elif state != DELIVERY_DROPPED:
            outcome = REARM_NOT_DROPPED
        else:
            conn.execute(
                f"""UPDATE {_TABLE} SET delivery_state=?, delivery_attempts=0,
                       delivery_error=NULL, delivery_claim=NULL,
                       delivery_claimed_at=NULL, updated_at=?
                    WHERE dispatch_id=? AND delivery_state=?""",
                (DELIVERY_PENDING, now_epoch, str(dispatch_id), DELIVERY_DROPPED),
            )
            outcome = REARM_REARMED
    return outcome, get_dispatch(dispatch_id)


#: How often the backlog report may repeat while the condition persists.
_BACKLOG_REPORT_INTERVAL_SECONDS = 300.0

_backlog_report_state: dict[str, float | int] = {"at": 0.0, "high": 0}


def _backlog_report_due(pending: int, *, now: float | None = None) -> bool:
    """True when the backlog is worth reporting AGAIN.

    ``_prune`` runs on every ``record_completion``, so an unthrottled report
    would emit an EventLog row and a warning per completion for as long as the
    backlog lasts — a storm produced by the very alarm meant to describe a quiet
    failure, in exactly the pathological state where the log is most needed for
    something else.

    Reported when the backlog reaches a NEW HIGH (that is new information) or
    when the interval has elapsed (so a stuck backlog never goes permanently
    silent) — and never merely because another completion happened to run the
    collector.

    A high-water mark rather than "the number changed": the two lanes interleave,
    so the pending count oscillates as rows drain and new completions land.
    Reporting on any change turns 3 -> 4 -> 3 -> 4 back into the storm this
    exists to prevent, which is exactly what the first version of this guard did
    and what its test caught.
    """

    moment = time.time() if now is None else now
    elapsed = moment - float(_backlog_report_state["at"])
    if (
        pending <= int(_backlog_report_state["high"])
        and elapsed < _BACKLOG_REPORT_INTERVAL_SECONDS
    ):
        return False
    _backlog_report_state["high"] = max(pending, int(_backlog_report_state["high"]))
    _backlog_report_state["at"] = moment
    return True


def _prune() -> None:
    """Bound terminal history — but NEVER at the cost of an undelivered answer.

    Housekeeping deletes only rows whose DELIVERY has settled (``delivered`` or
    ``dropped``). A ``pending`` row is an answer the sender has not been told
    about, and the previous ordering-only preference ("delivered first, then
    whatever is oldest") fell straight through to those rows once the terminal
    count passed the cap: a permanently-busy sender never drains — the idle
    probe requeues without burning an attempt, so the row never converges to
    ``dropped`` either — and the reply it was holding got deleted with no event,
    no log line, and nothing left to notice it by. That is precisely the silence
    this lane exists to retire, manufactured by the lane's own collector.

    An undeliverable row is not thereby immortal: the attempt cap converges it
    to ``dropped``, which is deletable and evented. Pruning is simply not the
    mechanism that decides an answer was worthless.
    """

    cutoff = time.time() - _RETENTION_SECONDS
    try:
        with _DB_LOCK, _transaction() as conn:
            conn.execute(
                f"DELETE FROM {_TABLE} WHERE delivery_state=? AND updated_at < ?",
                (DELIVERY_DELIVERED, cutoff),
            )
            settled = conn.execute(
                f"""SELECT COUNT(*) FROM {_TABLE}
                    WHERE state != ? AND delivery_state != ?""",
                (STATE_RUNNING, DELIVERY_PENDING),
            ).fetchone()[0]
            excess = max(0, int(settled) - _MAX_RETAINED_TERMINAL)
            if excess:
                conn.execute(
                    f"""DELETE FROM {_TABLE} WHERE dispatch_id IN (
                          SELECT dispatch_id FROM {_TABLE}
                          WHERE state != ? AND delivery_state != ?
                          ORDER BY CASE delivery_state WHEN ? THEN 0 ELSE 1 END,
                                   updated_at ASC LIMIT ?
                        )""",
                    (STATE_RUNNING, DELIVERY_PENDING, DELIVERY_DELIVERED, excess),
                )
            # Exempting pending rows above would trade one silent failure for
            # another if nobody ever looked: a store growing without bound while
            # senders quietly fail to drain. So count them and SAY SO — loudly,
            # and in the event log — but never delete.
            stranded = conn.execute(
                f"""SELECT COUNT(*) FROM {_TABLE}
                    WHERE state != ? AND delivery_state = ?""",
                (STATE_RUNNING, DELIVERY_PENDING),
            ).fetchone()[0]
        if int(stranded) > _MAX_RETAINED_TERMINAL and _backlog_report_due(int(stranded)):
            logger.warning(
                "dispatch store holds %s undelivered completions (cap %s) — "
                "senders are not draining; nothing was deleted",
                stranded,
                _MAX_RETAINED_TERMINAL,
            )
            _emit(
                "dispatch.delivery_backlog",
                pending=int(stranded),
                cap=_MAX_RETAINED_TERMINAL,
            )
    except Exception:  # pragma: no cover - housekeeping must never fail a write
        logger.debug("dispatch store prune failed", exc_info=True)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def _query(where: str, params: tuple) -> list[dict[str, Any]]:
    path = dispatch_db_path()
    if not path.exists():
        # Read-only by contract: a projection asking "what is running" must
        # never CREATE the background-work database as a side effect.
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except Exception:
        return []
    try:
        rows = conn.execute(f"{_SELECT} {where}", params).fetchall()
    except sqlite3.OperationalError as exc:
        # A state.db that predates this table is a store with no dispatches, not
        # an unreadable store.
        if "no such table" not in str(exc).lower():
            raise
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [_row_to_dict(row) for row in rows]


def running_dispatches(limit: int = 200) -> list[dict[str, Any]]:
    """In-flight dispatches — the ``running_work`` kind=dispatch lane."""

    return _query(
        "WHERE state=? ORDER BY dispatched_at LIMIT ?", (STATE_RUNNING, int(limit))
    )


def undeliverable_dispatches(
    limit: int = 50, *, since: float | None = None
) -> list[dict[str, Any]]:
    """Completions the sender will NEVER be told about, newest first.

    Three paths reach here and none of them forges a delivery turn: no sender
    session, an unresolvable sender, and the attempt cap. Each is an answer that
    was produced and then abandoned — the single worst outcome this lane can
    have, and until now its only trace was an EventLog row nothing reads.

    Surfaced so the Activity projection can show it. ``since`` bounds the window
    (the operator cares that work died, not that it died last week); the drop
    reason travels on the row rather than only in the event, so what is rendered
    can say WHY.
    """

    cutoff = float(since) if since is not None else 0.0
    return _query(
        "WHERE delivery_state = ? AND updated_at >= ? ORDER BY updated_at DESC LIMIT ?",
        (DELIVERY_DROPPED, cutoff, int(limit)),
    )


def pending_deliveries(limit: int = 50) -> list[dict[str, Any]]:
    """Completed dispatches the sender has NOT yet been told about."""

    return _query(
        "WHERE state != ? AND delivery_state = ? ORDER BY completed_at, dispatch_id LIMIT ?",
        (STATE_RUNNING, DELIVERY_PENDING, int(limit)),
    )


def get_dispatch(dispatch_id: str) -> dict[str, Any] | None:
    rows = _query("WHERE dispatch_id=?", (str(dispatch_id),))
    return rows[0] if rows else None


def list_dispatches(
    *, sender_session_id: str = "", limit: int = 25
) -> list[dict[str, Any]]:
    """The CALLER's dispatches, newest first.

    Scoped by the sender's chat-root session id — the same identity the delivery
    lane routes on — so ``agent_chat_dispatches`` can only ever list work the
    caller actually dispatched. An empty scope lists nothing rather than
    everything: a missing caller identity is a reason to show less, never more.
    """

    scope = _text(sender_session_id, 240)
    if not scope:
        return []
    bounded = max(1, min(int(limit or 25), 100))
    return _query(
        "WHERE sender_session_id=? ORDER BY dispatched_at DESC LIMIT ?",
        (scope, bounded),
    )


# --------------------------------------------------------------------------
# boot recovery
# --------------------------------------------------------------------------


def restore_undelivered_dispatches() -> dict[str, int]:
    """Reclassify dispatches orphaned by a dead process, at boot.

    A row still marked ``running`` whose owner process is provably gone can
    never complete: nothing is left to run its turn or write its result. It is
    reclassified ``unknown`` — an outcome the sender is still owed, phrased
    honestly — and armed for delivery.

    Identity, not liveness: a PID that exists but whose start time differs from
    the recorded baseline is a DIFFERENT process wearing a recycled number, and
    counts as gone. A start time that cannot be READ is neither proof nor
    disproof, so the row is left alone rather than being resurrected or buried
    on a psutil hiccup.

    Restored rows carry ``restored: True`` on the completion they produce (in
    memory only — the flag is a property of THIS boot, not of the record), and
    the drain must positively prove it owns the target chat root before
    delivering one (#64484).
    """

    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:  # pragma: no cover - defensive
        return {"restored": 0, "checked": 0}

    supervised = _supervised_here()
    rows = _query("WHERE state=?", (STATE_RUNNING,))
    restored = 0
    for row in rows:
        # NEVER answer for a dispatch this process is still supervising. Since
        # the sweep became periodic it runs beside live supervisors, and there
        # is a real window — from the child exiting to the supervisor's
        # `record_completion`, across `proc.wait()` and two pump joins — where a
        # dead PID on a running row means "about to be recorded", not "orphan".
        # Guessing there delivers "the outcome is unknown" for a dispatch that
        # COMPLETED, and the real answer landing moments later is then swallowed
        # by the delivery-turn replay dedup.
        if row.get("dispatch_id") in supervised:
            continue
        pid = row.get("owner_pid")
        if not pid:
            continue
        try:
            alive = bool(_pid_exists(int(pid)))
        except Exception:
            continue
        if alive:
            baseline = row.get("owner_started_at")
            if baseline is None:
                # No identity baseline recorded: cannot disprove ownership.
                continue
            try:
                observed = get_process_start_time(int(pid))
            except Exception:
                continue
            if observed is None or int(observed) == int(baseline):
                # Unreadable probe, or a genuine match — either way not disproof.
                continue
        # The dominant case after the subprocess move is a serve recycled while
        # a HEALTHY child ran to completion: the reply exists, it is in the
        # target's own thread, and only this bookkeeping row was orphaned. So
        # the copy points at that thread rather than inviting the sender to
        # duplicate work that has very likely already been done.
        thread = row.get("target_session_id") or ""
        where = (
            f" Their reply, if they finished, is in thread {thread} "
            "(agent_chat_open with that session_id)."
            if thread
            else (
                " Check your thread with them (agent_chat_open / agent_chat_log_path)"
                " before re-sending — they may well have finished."
            )
        )
        if record_completion(
            row["dispatch_id"],
            state=STATE_UNKNOWN,
            error=(
                "The process running this dispatch exited before recording a result, so "
                "the outcome is unknown." + where
            ),
            target_session_id=thread,
            # The sweep INFERS; it must never overwrite an outcome a supervisor
            # actually observed and recorded between the read above and here.
            only_if_running=True,
        ):
            restored += 1
    return {"restored": restored, "checked": len(rows)}
