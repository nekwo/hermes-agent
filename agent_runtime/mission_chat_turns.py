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
from .mission_chat_phases import (
    TURN_PHASES_KEY,
    TURN_RECORD_SCHEMA_VERSION,
    safe_turn_phases,
)
from .run_budget import (
    ACCOUNTING_KEY as RUN_BUDGET_ACCOUNTING_KEY,
    safe_accounting_block as safe_run_budget_accounting,
)

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
# ═══ turn-lifecycle vocabulary — ONE table, and it decides everything ═══════
#
# This module OWNS the turn states. Every consumer — in this file, in the
# history projection, in the operator-conversation contract, in the CLI chat
# lane — asks this table instead of re-spelling a state literal.
#
# The rule exists because the same defect landed twice, ~700 lines apart. Both
# times a consumer wrote "which turn states are over" as its own string:
# ``state != "interrupted"`` in the marker synthesizer and again in the
# tool-call settler. When ``budget_exhausted`` was added (2026-07-26) neither
# spelling knew about it, so a turn that had been over for minutes still
# rendered a live spinner in the cockpit. Recurrence was the finding: the bug
# is not either literal, it is that a consumer was free to invent one. Adding
# a state must extend a TABLE here, never require finding every comparison.
#
# ── the states ──────────────────────────────────────────────────────────────
# Journal states — the exactly-once lane driven by
# ``transition_mission_chat_turn``.
TURN_STATE_PENDING = "pending"
TURN_STATE_EXECUTING = "executing"
TURN_STATE_OUTCOME_UNKNOWN = "outcome_unknown"
TURN_STATE_NATIVE_COMMITTED = "native_committed"
TURN_STATE_PROJECTED = "projected"
TURN_STATE_ABANDONED = "abandoned"
# Wall-budget terminal (2026-07-26). A turn that ran out of wall clock is NOT
# an ambiguous provider outcome — the harness knows exactly why it stopped — so
# it settles here instead of freezing at ``outcome_unknown`` and demanding an
# operator ``turn-resolve --action abandon``. Terminal, needs no resolution,
# and never blocks the next send.
TURN_STATE_BUDGET_EXHAUSTED = "budget_exhausted"
# Legacy streaming vocabulary. Written by the pre-journal persist lane and by
# the repair sweep; never produced by ``transition_mission_chat_turn``.
TURN_STATE_RUNNING = "running"
TURN_STATE_COMPLETED = "completed"
TURN_STATE_FAILED = "failed"
TURN_STATE_INTERRUPTED = "interrupted"

JOURNAL_TURN_STATES = frozenset(
    {
        TURN_STATE_PENDING,
        TURN_STATE_EXECUTING,
        TURN_STATE_OUTCOME_UNKNOWN,
        TURN_STATE_NATIVE_COMMITTED,
        TURN_STATE_PROJECTED,
        TURN_STATE_ABANDONED,
        TURN_STATE_BUDGET_EXHAUSTED,
    }
)
LEGACY_TURN_STATES = frozenset(
    {
        TURN_STATE_RUNNING,
        TURN_STATE_COMPLETED,
        TURN_STATE_FAILED,
        TURN_STATE_INTERRUPTED,
    }
)
# The known universe. A state outside it is not a turn state and is rejected at
# the store boundary (``_safe_turn_state``).
ALL_TURN_STATES = JOURNAL_TURN_STATES | LEGACY_TURN_STATES

# ── lifecycle buckets: every known state belongs to EXACTLY one ─────────────
# A record here has an executor that has not settled it: the legacy streaming
# state plus every non-terminal journal state. Retention never evicts them, GC
# never archives their session file, and the stale-turn repairs (next-send +
# serve-boot orphan sweep) flip exactly this set. ``budget_exhausted`` is
# deliberately ABSENT: it is settled, so no repair may reopen it and no sweep
# may flip it to ``interrupted``.
INFLIGHT_TURN_STATES = frozenset(
    {
        TURN_STATE_RUNNING,
        TURN_STATE_PENDING,
        TURN_STATE_EXECUTING,
        TURN_STATE_OUTCOME_UNKNOWN,
    }
)
# The provider's reply is durable but the Mission Control projection has not
# committed yet. NEITHER in-flight (a repair flip here would destroy a recorded
# reply) NOR terminal (the projection walk still owes work). This bucket had no
# name before the consolidation — ``native_committed`` simply fell out of both
# sets, which is exactly the kind of silent gap the coverage guard below now
# makes impossible to reintroduce.
SETTLING_TURN_STATES = frozenset({TURN_STATE_NATIVE_COMMITTED})
# States that are settled and require NO operator resolution. ``turn-resolve``
# still accepts only ``outcome_unknown`` (the genuinely ambiguous case).
TERMINAL_TURN_STATES = frozenset(
    {
        TURN_STATE_PROJECTED,
        TURN_STATE_ABANDONED,
        TURN_STATE_BUDGET_EXHAUSTED,
        TURN_STATE_COMPLETED,
        TURN_STATE_FAILED,
        TURN_STATE_INTERRUPTED,
    }
)

# ── decision sets: what a consumer actually asks ────────────────────────────
# A resend of the SAME client_message_id that finds a durable reply already in
# SessionDB promotes the record to ``native_committed`` from these states — the
# reply is proven, so it must be projected rather than lost. Read by the CLI
# chat lane; mirrors the ``native_committed`` column of ``_JOURNAL_TRANSITIONS``
# (guarded below).
REPLY_RECOVERABLE_TURN_STATES = frozenset(
    {
        TURN_STATE_EXECUTING,
        TURN_STATE_OUTCOME_UNKNOWN,
        TURN_STATE_BUDGET_EXHAUSTED,
    }
)
# ...and with no such proof, a resend from these states is REFUSED: the prior
# provider outcome cannot be proven, so the operator must resolve the turn
# first. ``budget_exhausted`` is deliberately absent — it is settled, gets its
# own honest refusal, and never routes anyone to ``turn-resolve``.
RESEND_BLOCKING_TURN_STATES = frozenset(
    {TURN_STATE_EXECUTING, TURN_STATE_OUTCOME_UNKNOWN}
)
# The only states ``turn-resolve --action abandon`` accepts: the genuinely
# ambiguous provider outcome, and nothing else.
OPERATOR_RESOLVABLE_TURN_STATES = frozenset({TURN_STATE_OUTCOME_UNKNOWN})

# A legacy record entering the journal lane is read as its journal equivalent.
_LEGACY_TO_JOURNAL_STATE = {
    TURN_STATE_RUNNING: TURN_STATE_PENDING,
    TURN_STATE_COMPLETED: TURN_STATE_PROJECTED,
}
_JOURNAL_TRANSITIONS = {
    None: {TURN_STATE_PENDING},
    TURN_STATE_PENDING: {
        TURN_STATE_PENDING,
        TURN_STATE_EXECUTING,
        TURN_STATE_ABANDONED,
        TURN_STATE_BUDGET_EXHAUSTED,
    },
    TURN_STATE_EXECUTING: {
        TURN_STATE_NATIVE_COMMITTED,
        TURN_STATE_OUTCOME_UNKNOWN,
        TURN_STATE_BUDGET_EXHAUSTED,
    },
    TURN_STATE_OUTCOME_UNKNOWN: {
        TURN_STATE_ABANDONED,
        TURN_STATE_NATIVE_COMMITTED,
        TURN_STATE_BUDGET_EXHAUSTED,
    },
    # A durable reply proven AFTER the budget settled the turn still wins — the
    # same legacy-interrupted convention that lets ``outcome_unknown`` promote
    # to ``native_committed`` (a recorded reply must never be lost to a repair
    # flip). Nothing else may leave this state: it does not resurrect to
    # ``pending``, so a retry uses a NEW client_message_id like any other
    # settled turn.
    TURN_STATE_BUDGET_EXHAUSTED: {
        TURN_STATE_BUDGET_EXHAUSTED,
        TURN_STATE_NATIVE_COMMITTED,
    },
    TURN_STATE_NATIVE_COMMITTED: {TURN_STATE_NATIVE_COMMITTED, TURN_STATE_PROJECTED},
    TURN_STATE_PROJECTED: {TURN_STATE_PROJECTED},
    TURN_STATE_ABANDONED: {TURN_STATE_ABANDONED},
}


# ── import-time contract guards ─────────────────────────────────────────────
#
# Raised, not asserted, so ``python -O`` cannot strip the contract (same
# convention as ``persona_chat_history.TERMINAL_TURN_MARKERS``). Each guard
# encodes a failure that has already cost real time: a state nobody classified
# (the wall-budget spinner), a decision set naming a state the store cannot
# hold (a typo that silently never matches), or a transition table drifting
# away from the decision set derived from it.
def _guard_turn_state_vocabulary() -> None:  # pragma: no cover - import contract
    buckets = {
        "INFLIGHT_TURN_STATES": INFLIGHT_TURN_STATES,
        "SETTLING_TURN_STATES": SETTLING_TURN_STATES,
        "TERMINAL_TURN_STATES": TERMINAL_TURN_STATES,
    }
    decisions = {
        "REPLY_RECOVERABLE_TURN_STATES": REPLY_RECOVERABLE_TURN_STATES,
        "RESEND_BLOCKING_TURN_STATES": RESEND_BLOCKING_TURN_STATES,
        "OPERATOR_RESOLVABLE_TURN_STATES": OPERATOR_RESOLVABLE_TURN_STATES,
    }
    for name, states in {**buckets, **decisions}.items():
        unknown = sorted(states - ALL_TURN_STATES)
        if unknown:
            raise RuntimeError(f"{name} names non-existent turn state(s): {unknown}")

    # Exactly one bucket per state: pairwise disjoint...
    names = sorted(buckets)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            overlap = sorted(buckets[left] & buckets[right])
            if overlap:
                raise RuntimeError(
                    f"turn state(s) in both {left} and {right}: {overlap}"
                )
    # ...and no state left behind. An unclassified state is the wall-budget
    # spinner waiting to happen again.
    unclassified = sorted(ALL_TURN_STATES - set().union(*buckets.values()))
    if unclassified:
        raise RuntimeError(
            "turn state(s) belong to no lifecycle bucket: " f"{unclassified}"
        )

    # The refusal ladder narrows: what an operator may resolve is a subset of
    # what blocks a resend, which is a subset of what a proven reply recovers.
    if not (
        OPERATOR_RESOLVABLE_TURN_STATES
        <= RESEND_BLOCKING_TURN_STATES
        <= REPLY_RECOVERABLE_TURN_STATES
    ):
        raise RuntimeError(
            "turn-state refusal ladder is not nested: "
            f"resolvable={sorted(OPERATOR_RESOLVABLE_TURN_STATES)} "
            f"blocking={sorted(RESEND_BLOCKING_TURN_STATES)} "
            f"recoverable={sorted(REPLY_RECOVERABLE_TURN_STATES)}"
        )
    # ``REPLY_RECOVERABLE`` is a VIEW of the transition table, not a second
    # opinion: it must be exactly the states from which the journal accepts a
    # promotion to ``native_committed``, minus that state itself (a record
    # already there has nothing to recover).
    derived = {
        state
        for state, allowed in _JOURNAL_TRANSITIONS.items()
        if state is not None
        and state != TURN_STATE_NATIVE_COMMITTED
        and TURN_STATE_NATIVE_COMMITTED in allowed
    }
    if derived != REPLY_RECOVERABLE_TURN_STATES:
        raise RuntimeError(
            "REPLY_RECOVERABLE_TURN_STATES disagrees with _JOURNAL_TRANSITIONS: "
            f"table={sorted(derived)} set={sorted(REPLY_RECOVERABLE_TURN_STATES)}"
        )

    # The transition table and the legacy alias map may only name real states.
    for state, allowed in _JOURNAL_TRANSITIONS.items():
        if state is not None and state not in JOURNAL_TURN_STATES:
            raise RuntimeError(f"_JOURNAL_TRANSITIONS keys a non-journal state: {state}")
        unknown = sorted(allowed - JOURNAL_TURN_STATES)
        if unknown:
            raise RuntimeError(
                f"_JOURNAL_TRANSITIONS[{state}] targets non-journal state(s): {unknown}"
            )
    for legacy, journal in _LEGACY_TO_JOURNAL_STATE.items():
        if legacy not in LEGACY_TURN_STATES or journal not in JOURNAL_TURN_STATES:
            raise RuntimeError(
                f"_LEGACY_TO_JOURNAL_STATE maps {legacy!r} -> {journal!r}, "
                "which is not legacy -> journal"
            )


_guard_turn_state_vocabulary()

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

    if current in JOURNAL_TURN_STATES and requested in LEGACY_TURN_STATES:
        return None
    if requested is None:
        return current or TURN_STATE_RUNNING
    if requested not in ALL_TURN_STATES:
        return None
    if requested != TURN_STATE_RUNNING:
        return requested
    if write_ahead or current is None or current == TURN_STATE_RUNNING:
        return TURN_STATE_RUNNING
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
            "schema_version": TURN_RECORD_SCHEMA_VERSION,
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
        if current in LEGACY_TURN_STATES:
            current = _LEGACY_TO_JOURNAL_STATE.get(current)
        if requested not in _JOURNAL_TRANSITIONS.get(current, set()):
            return False, MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        now_iso = _utc_now_iso()
        record = dict(existing) if isinstance(existing, dict) else {}
        if not record.get("started_at"):
            record["started_at"] = now_iso
        record.update(
            {
                "schema_version": TURN_RECORD_SCHEMA_VERSION,
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
    *,
    session_id: str | None,
    client_message_id: str | None,
    turn_id: str | None,
    resolution_actor: str | None = None,
    resolution_reason: str | None = None,
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
            _record_state(existing) not in OPERATOR_RESOLVABLE_TURN_STATES
            or safe_assignment_token(existing.get("turn_id")) != exact_turn
        ):
            return False, MissionChatTurnPersistOutcome.REJECTED_STALE_TRANSITION
        record = dict(existing)
        record.update(
            {
                "state": TURN_STATE_ABANDONED,
                "updated_at": _utc_now_iso(),
                "resolved_at": _utc_now_iso(),
                "resolution": "abandon",
                "resolution_actor": safe_assignment_text(
                    resolution_actor, limit=160
                )
                or "operator",
                "resolution_reason": safe_assignment_text(
                    resolution_reason, limit=320
                )
                or "explicit abandon",
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


def mark_stale_inflight_turns_interrupted(
    *,
    session_id: str | None,
    active_client_message_id: str | None,
) -> list[str]:
    """Flip a session's dead in-flight turn records to ``interrupted``.

    Callers MUST guarantee no live executor on the session: hold its root
    lease (``persona_chat_root_lease`` — held for a native turn's entire
    execution and released by the kernel when the executor dies), or run from
    a lane that already serializes sends per session. Under that guarantee
    every OTHER in-flight record —
    journal ``pending``/``executing``/``outcome_unknown`` as much as legacy
    ``running`` — is a corpse that can no longer settle itself (live incident
    2026-07-25: a reaped Launcher took its serve child down mid-turn and the
    QA relay record froze at ``executing`` forever, a permanently "running"
    console). ``interrupted`` is the one repair state the history projection
    renders as a typed ``turn_interrupted`` marker row.
    """

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
            if _record_state(record) not in INFLIGHT_TURN_STATES:
                continue
            record["state"] = TURN_STATE_INTERRUPTED
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


def inflight_chat_session_roots() -> list[str]:
    """Root chat session ids of sessions holding at least one in-flight record.

    Read-only scan feeding the serve-boot orphan sweep. The root id comes from
    record metadata (``root_chat_session_id``, then ``active_session_id``) —
    the session file stem is a one-way digest and cannot be reversed, so a
    record that predates the metadata stays invisible here and keeps relying
    on the next-send repair. Torn/unreadable files are skipped; the sweep is
    best-effort and retries on the next boot.
    """

    _migrate_legacy_if_present()
    roots: list[str] = []
    seen: set[str] = set()
    for path in _iter_session_files():
        for record in _read_session_map(path).values():
            if not isinstance(record, dict):
                continue
            if _record_state(record) not in INFLIGHT_TURN_STATES:
                continue
            root = safe_assignment_text(
                record.get("root_chat_session_id") or record.get("active_session_id"),
                limit=240,
            )
            if root and root not in seen:
                seen.add(root)
                roots.append(root)
    return roots


def inflight_turn_rows() -> list[dict[str, Any]]:
    """Every in-flight turn RECORD across every session, bounded and safe.

    ``inflight_chat_session_roots`` answers "which sessions owe a sweep?" — a
    set of ids. The ``running_work`` projection asks a different question:
    "which turns are in flight right now, and since when?", which needs the
    records themselves. Deriving that from the roots is not possible: the
    session FILE stem is a one-way digest of the session KEY, while the root id
    lives in record metadata and the two are not interchangeable, so a caller
    holding only a root cannot get back to the records it came from.

    Same scan discipline as the roots walk: read-only, torn/unreadable files
    skipped, every record passed through ``_safe_record`` so callers see the
    bounded, redaction-safe projection rather than raw journal contents. The
    ``session_id`` carried on each row is the record's own root/active id — the
    same field the roots walk reads — so a record predating that metadata
    reports an empty session rather than a fabricated one.

    Ordered by turn start (``started_at``, falling back to ``updated_at``) so
    the oldest in-flight work sorts first, matching the C8 replay ordering
    ``mission_chat_turn_records`` already uses.
    """

    _migrate_legacy_if_present()
    rows: list[dict[str, Any]] = []
    for path in _iter_session_files():
        for message_key, record in _read_session_map(path).items():
            if not isinstance(record, dict):
                continue
            if _record_state(record) not in INFLIGHT_TURN_STATES:
                continue
            safe_key = safe_assignment_text(message_key, limit=240)
            if not safe_key:
                continue
            safe_record = _safe_record(record, client_message_id=safe_key)
            if safe_record is None:
                continue
            # Elements are the turn's projected message content; the projection
            # only needs identity + timing, and carrying them would put chat
            # text on a HUD wire that has no reader for it.
            safe_record.pop("elements", None)
            safe_record["session_id"] = safe_assignment_text(
                record.get("root_chat_session_id") or record.get("active_session_id"),
                limit=240,
            )
            rows.append(safe_record)
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("started_at") or item.get("updated_at") or ""),
            str(item.get("client_message_id") or ""),
        ),
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
            or _record_state(record) in INFLIGHT_TURN_STATES
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
                        _record_state(record) in INFLIGHT_TURN_STATES
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
        "state": _record_state(record) or TURN_STATE_COMPLETED,
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
    "resolution_actor": 160,
    "resolution_reason": 320,
    "resolved_at": 80,
    "pending_user_message": 12000,
    # Wall-budget provenance. ``budget_trigger`` is the typed reason the
    # graceful checkpoint (or the last-resort hard wall) ended the turn;
    # ``budget_summary`` is the one-line window description an operator reads
    # without re-deriving the arithmetic.
    "budget_trigger": 80,
    "budget_summary": 400,
}


#: The ONE structured entry on a turn record, carried verbatim from
#: ``run_budget.RunBudgetLedger.accounting()`` — the whole "what bounded this
#: turn?" block (``bounded_by`` / ``trip_reason`` / ``enforcement`` /
#: ``tripped`` / ``budgets``), documented in
#: ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/run-budget-accounting.md`` §3.
#:
#: A pure chat turn produces NO run record (``runs/`` is the goal/task lane), so
#: before this the ledger reached the live envelope and then evaporated: the
#: cockpit had nowhere to read "this reply is short because the wall closed"
#: after the turn settled. The turn journal IS the chat lane's run record, so
#: the block lives here, under the same key and in the same shape every other
#: carrier uses.
#:
#: ABSENT STAYS ABSENT. Older records and turns that declared no budget get no
#: key — never an empty dict, because "nothing bounded this turn" and "nobody
#: accounted this turn" are different facts and a reader must be able to tell
#: them apart.
_JOURNAL_RUN_BUDGET_FIELD = RUN_BUDGET_ACCOUNTING_KEY

#: Text fields whose EMPTY value is a recorded fact rather than an absence.
#:
#: Everything else above is dropped when empty, which is right for an id or a
#: fingerprint — there is no such thing as "the fingerprint was, meaningfully,
#: nothing". ``stored_reply`` is the exception: "the turn replied with nothing"
#: and "nobody recorded a reply here" are different facts about the turn, and
#: collapsing them had a live consequence. The replay branch admits a settled
#: turn on ``stored_reply is not None``, so a SILENT turn — model produced no
#: content, `ok` true, empty reply — failed that guard, fell through to the
#: live provider path and died there as ``chat_turn_not_submitted /
#: rejected_stale_transition``. The delivery drain derives its
#: ``client_message_id`` from the dispatch id precisely so a retry converges on
#: one turn; for silent turns that convergence was broken.
#:
#: Same principle the run-budget block states directly above: absent stays
#: absent, and recorded-empty stays recorded.
_JOURNAL_EMPTY_PRESERVING_FIELDS = frozenset({"stored_reply"})


def _safe_journal_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, limit in _JOURNAL_TEXT_FIELDS.items():
        text = safe_assignment_text(value.get(key), limit=limit)
        if text:
            result[key] = text
        elif key in _JOURNAL_EMPTY_PRESERVING_FIELDS and value.get(key) is not None:
            # An empty value that was WRITTEN is a fact, not an absence — the
            # same distinction the run-budget block above is careful about.
            result[key] = ""
    run_budget = safe_run_budget_accounting(value.get(_JOURNAL_RUN_BUDGET_FIELD))
    if run_budget is not None:
        result[_JOURNAL_RUN_BUDGET_FIELD] = run_budget
    # Turn-latency phase spans (schema v3). Same absent-stays-absent rule the
    # run-budget block states above, applied one level deeper: the block itself
    # is absent on a v2 record, and INSIDE the block a phase the turn never
    # reached has no key. ``safe_turn_phases`` drops what it cannot read and
    # supplies nothing, so a reader can never mistake "did not happen" for
    # "happened at millisecond zero".
    phases = safe_turn_phases(value.get(TURN_PHASES_KEY))
    if phases is not None:
        result[TURN_PHASES_KEY] = phases
    for key in (
        "provider_submitted",
        "native_committed",
        "projection_committed",
        "projection_event_emitted",
        # True on any turn the wall budget ended — including the graceful case
        # that still projected a real reply, so "why is this reply short?" is
        # answerable from the record instead of from the operator's memory.
        "budget_exhausted",
    ):
        if key in value:
            result[key] = bool(value.get(key))
    return result


def _safe_turn_state(value: Any) -> str | None:
    state = safe_assignment_token(value)
    return state if state in ALL_TURN_STATES else None


def _record_state(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    state = _safe_turn_state(record.get("state"))
    # A record whose state is missing/unrecognized predates the vocabulary (or
    # was written by something that is not this store). Read it as settled —
    # never as in-flight, which would hand it to the repair sweep.
    return state or TURN_STATE_COMPLETED


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
                    # Generic tool input/result record — block-preserving bound
                    # (safe_assignment_text would fold the key-per-line contract
                    # the console dropdown renders into one line). Scrubbed and
                    # bounded upstream at the progress sink.
                    "tool_input": _safe_block_text(raw.get("tool_input"), limit=1200),
                    "tool_result": _safe_block_text(raw.get("tool_result"), limit=1800),
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


def _safe_block_text(value: Any, *, limit: int) -> str | None:
    """Newline-preserving bounded text for the tool input/result record (the
    whitespace-collapsing ``safe_assignment_text`` would destroy the
    key-per-line structure the console dropdown renders)."""

    text = str(value or "").replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    if len(text) > limit:
        text = f"{text[:limit]}\n…(rest truncated)…"
    return text


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
