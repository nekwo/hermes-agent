"""Recorded-progress idempotency for ``runtime.agent.create``.

``reserve_persona_chat_mint`` (``persona_chat_mints.py``) is the precedent and
the shape is copied from it deliberately — same digest-keyed atomic receipt,
same "durable BEFORE the work begins" ordering, same typed error class. What is
NOT copied is its state vocabulary, because the question is different.

The chat mint reserves ONE write. This reserves a SEQUENCE of two writes into
two stores with no common transaction:

1. the persona instance + its chat root (``PersonaInstanceStore.add_instance``),
2. the office placement (``OfficeStore.upsert_actor``).

A crash between them leaves the half-state the whole method exists to make
unrepresentable (R#37, fold plan §10.4). So the receipt records WHICH of the two
has happened, and a replay resumes from there instead of duplicating or
stranding:

``instance_minted``
    The roster row and chat root exist; the placement does not. A replay skips
    the mint and performs the placement.
``placed``
    The roster row, the chat root AND the office actor exist, and the create
    asked for skills that have not been assigned yet. A replay skips both
    writes and re-enters at the SKILLS phase alone. Added by plan S4.
``done``
    Both landed. A replay returns the recorded reply verbatim and writes
    nothing — the revision the actor carries is the witness that it wrote
    nothing.
``rolled_back``
    The placement was refused and the instance was compensated away. A replay
    returns the recorded refusal rather than re-attempting, which is decision
    **D-A3** and is forced rather than chosen: ``retire`` leaves an end-of-life
    tombstone and ``assert_bindable`` (``persona_assignments.py:1399-1403``)
    refuses to ever re-bind that instance id, so the placement id is BURNED. A
    resume would have to invent a different placement id, stranding whatever
    actor key the client predicted from the one it sent. The client's honest
    cure is a new gesture with a new key, which is what the launcher already
    does (it stamps micros into every key).

If the compensation ITSELF fails the state stays ``instance_minted`` with
``rollback_error`` recorded — the §6 worst case, now bounded and named on disk
instead of being an unlabelled orphan.

The ``placed`` migration, exactly (plan D4)
-------------------------------------------
``placed`` is an ADDED state, not a renamed one, and the three rules that make
it a migration rather than a rewrite are all here:

* **A ``done`` receipt with no ``skills`` field is PRE-PLAN and is never
  re-entered.** Every receipt written before S4 is exactly that, and a resume
  that treated it as "done, but did the skills run?" would install a second
  time and re-write ``skill_overrides`` for an agent an operator may have
  edited since. ``skills=None`` on a ``done`` record means "this key predates
  the phase", and :attr:`AgentCreateRecord.skills` is ``None`` for it because
  the key is absent from the file, not because the list was empty — an empty
  REQUEST is recorded as ``[]``, which is a different value on purpose.
* **``placed`` carries ``skills``** — the normalised request list, possibly
  empty — so the receipt says what the phase was asked for.
* **An unknown state stays ``reservation_corrupt``.** The ``done`` literal is
  deliberately not renamed: an OLD serve reading a NEW ``placed`` receipt fails
  the ``_VALID_STATES`` check and refuses loudly rather than re-minting, which
  is the safe direction for a downgrade.
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
from .locks import HarnessLockUnavailable, agent_create_lock

_SCHEMA_VERSION = 1

STATE_INSTANCE_MINTED = "instance_minted"
#: Both writes landed; the skills phase has not run yet (plan S4/D4).
STATE_PLACED = "placed"
STATE_DONE = "done"
STATE_ROLLED_BACK = "rolled_back"

_VALID_STATES = frozenset(
    {STATE_INSTANCE_MINTED, STATE_PLACED, STATE_DONE, STATE_ROLLED_BACK}
)


class AgentCreateReservationError(RuntimeError):
    """A typed, fail-closed reservation failure. ``code`` is the branch point."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AgentCreateRecord:
    key_digest: str
    persona_id: str
    workspace_id: str
    created_at: str
    updated_at: str
    state: str | None = None
    persona_instance_id: str | None = None
    placement_id: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] = field(default_factory=dict)
    rollback_error: str | None = None
    #: The normalised skills request, or ``None`` when the receipt carries NO
    #: ``skills`` key at all. The distinction is load-bearing: ``None`` on a
    #: ``done`` record identifies a PRE-PLAN receipt, which is never re-entered,
    #: while ``[]`` is a create that explicitly asked for no skills.
    skills: list[str] | None = None

    @property
    def is_new(self) -> bool:
        """True when this key has never been seen — nothing durable exists."""

        return self.state is None


class AgentCreateReservation:
    """One reservation, held for the duration of its handler's work."""

    def __init__(self, record: AgentCreateRecord, *, replayed: bool):
        self.record = record
        #: True when the key was already on disk when the reservation opened.
        self.replayed = replayed

    @property
    def state(self) -> str | None:
        return self.record.state

    def mark_instance_minted(
        self, *, persona_instance_id: str, placement_id: str
    ) -> AgentCreateRecord:
        """Durable BEFORE the placement is attempted. That ordering is the whole
        design: a crash after this line is resumable, a crash before it wrote
        nothing at all."""

        self.record = replace(
            self.record,
            state=STATE_INSTANCE_MINTED,
            persona_instance_id=persona_instance_id,
            placement_id=placement_id,
            updated_at=_timestamp(),
        )
        _write(self.record)
        return self.record

    def mark_placed(
        self, result: dict[str, Any], *, skills: list[str]
    ) -> AgentCreateRecord:
        """Durable BEFORE the skills phase is attempted.

        Same ordering argument as :meth:`mark_instance_minted`, one phase down:
        a crash after this line resumes at the skills phase with both writes
        already accounted for, and a crash before it is answered by the
        ``instance_minted`` resume that has always existed.

        ``result`` is the full placement ack, recorded here rather than
        recomputed on resume — a resumed create must hand back the actor key,
        revision and position the FIRST attempt wrote, not a second read of a
        row anything could have moved since.
        """

        self.record = replace(
            self.record,
            state=STATE_PLACED,
            result=dict(result),
            skills=list(skills),
            updated_at=_timestamp(),
        )
        _write(self.record)
        return self.record

    def mark_done(
        self, result: dict[str, Any], *, skills: list[str] | None = None
    ) -> AgentCreateRecord:
        """``skills`` defaults to whatever the record already carries.

        A create that never entered the skills phase leaves it ``None``, which
        is what makes a ``done`` receipt written by THIS code indistinguishable
        from a pre-plan one — correctly, because neither has skills to re-run.
        """

        self.record = replace(
            self.record,
            state=STATE_DONE,
            result=dict(result),
            skills=(
                list(skills) if skills is not None else self.record.skills
            ),
            updated_at=_timestamp(),
        )
        _write(self.record)
        return self.record

    def mark_rolled_back(self, failure: dict[str, Any]) -> AgentCreateRecord:
        self.record = replace(
            self.record,
            state=STATE_ROLLED_BACK,
            failure=dict(failure),
            updated_at=_timestamp(),
        )
        _write(self.record)
        return self.record

    def mark_rollback_failed(
        self, failure: dict[str, Any], *, rollback_error: str
    ) -> AgentCreateRecord:
        """The placement was refused AND the compensation raised.

        The state deliberately STAYS ``instance_minted``: a roster row that
        could not be retired still exists, and a replay must resume the
        placement rather than treat the whole create as never-happened.
        """

        self.record = replace(
            self.record,
            state=STATE_INSTANCE_MINTED,
            failure=dict(failure),
            rollback_error=str(rollback_error)[:400],
            updated_at=_timestamp(),
        )
        _write(self.record)
        return self.record


@contextmanager
def reserve_agent_create(
    *, idempotency_key: str, persona_id: str, workspace_id: str
) -> Iterator[AgentCreateReservation]:
    """Open (or replay) the reservation for one create, under its own lock.

    The lock is keyed on the idempotency DIGEST and is taken by nothing else in
    the runtime, so although the office lock is acquired inside it (by
    ``upsert_actor``) no cycle exists: an agent-create lock is never waited on
    while an office lock is held. This is a correction to the plan's §5, which
    claims the handler holds its locks "sequentially, never nested".
    """

    key = str(idempotency_key or "").strip()
    if not key:
        raise AgentCreateReservationError(
            "idempotency_key_required", "idempotency_key is required"
        )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    try:
        with agent_create_lock(digest):
            path = paths.agent_create_reservation_path(digest)
            if path.exists():
                record = _read(path, digest=digest)
                _validate_scope(
                    record, persona_id=persona_id, workspace_id=workspace_id
                )
                yield AgentCreateReservation(record, replayed=True)
            else:
                timestamp = _timestamp()
                # NOT written yet. A brand-new key that fails validation or the
                # workspace check must leave no receipt behind, or a client
                # correcting a typo would be answered with its own stale error
                # forever. The first durable write is mark_instance_minted.
                yield AgentCreateReservation(
                    AgentCreateRecord(
                        key_digest=digest,
                        persona_id=str(persona_id),
                        workspace_id=str(workspace_id),
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                    replayed=False,
                )
    except HarnessLockUnavailable as exc:
        raise AgentCreateReservationError(
            "create_lock_unavailable",
            "another create with this idempotency key is still active; retry with the same key",
        ) from exc


def _validate_scope(
    record: AgentCreateRecord, *, persona_id: str, workspace_id: str
) -> None:
    """A key names ONE create. Re-using it for another persona or workspace is
    a client bug and is refused rather than answered with the wrong agent."""

    if record.persona_id == str(persona_id) and record.workspace_id == str(workspace_id):
        return
    raise AgentCreateReservationError(
        "idempotency_conflict",
        "idempotency_key was already used for a different persona or workspace",
    )


def _read(path, *, digest: str) -> AgentCreateRecord:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("schema_version") or 0) != _SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        state = str(raw.get("state") or "")
        if state not in _VALID_STATES:
            raise ValueError("invalid state")
        record = AgentCreateRecord(
            key_digest=str(raw["idempotency_key_sha256"]),
            persona_id=str(raw["persona_id"]),
            workspace_id=str(raw["workspace_id"]),
            state=state,
            persona_instance_id=raw.get("persona_instance_id") or None,
            placement_id=raw.get("placement_id") or None,
            result=dict(raw.get("result") or {}),
            failure=dict(raw.get("failure") or {}),
            rollback_error=raw.get("rollback_error") or None,
            # ABSENT stays ``None``; a present list is taken as written,
            # including an empty one. ``raw.get("skills") or None`` would
            # collapse ``[]`` to ``None`` and turn "asked for nothing" into
            # "predates the phase" on every reload.
            skills=(
                [str(item) for item in raw["skills"]]
                if isinstance(raw.get("skills"), list)
                else None
            ),
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
        )
        if record.key_digest != digest:
            raise ValueError("key digest does not match receipt path")
        if not all((record.persona_id, record.workspace_id, record.created_at)):
            raise ValueError("required field is blank")
        return record
    except AgentCreateReservationError:
        raise
    except Exception as exc:
        raise AgentCreateReservationError(
            "reservation_corrupt", f"agent-create reservation is unreadable: {exc}"
        ) from exc


def _write(record: AgentCreateRecord) -> None:
    atomic_json_write(
        paths.agent_create_reservation_path(record.key_digest),
        {
            "schema_version": _SCHEMA_VERSION,
            "idempotency_key_sha256": record.key_digest,
            "persona_id": record.persona_id,
            "workspace_id": record.workspace_id,
            "state": record.state,
            "persona_instance_id": record.persona_instance_id,
            "placement_id": record.placement_id,
            "result": record.result,
            "failure": record.failure,
            "rollback_error": record.rollback_error,
            # Omitted entirely when there is none, so a receipt this code writes
            # for a skill-less create is byte-identical in shape to a pre-plan
            # one and reads back the same way.
            **({"skills": list(record.skills)} if record.skills is not None else {}),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        },
        indent=2,
        sort_keys=True,
    )


def _timestamp() -> str:
    return now().isoformat().replace("+00:00", "Z")
