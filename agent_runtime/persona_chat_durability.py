"""Chat-root durability: the SessionDB row exists BEFORE the pointer is bound.

A ``default_chat_session_id`` on a persona instance is a PROMISE that the
mission-chat lane can dereference it. Two facts make that promise absolute
rather than eventual:

* ``mission-chat message`` refuses any explicit root that names no SessionDB
  row (``ChatErrorKind.UNKNOWN_CHAT_SESSION``) — deliberately, because its job
  is refusing client-fabricated roots.
* ``resolve_default_chat_session_id_for_instance`` hands the STORED pointer
  back forever on shape alone; it never asks the transcript store whether the
  row exists, so a phantom cannot self-heal.

So a pointer bound onto an instance before its row is durable is not "not yet
consistent" — it is permanently undeliverable, and every send to that agent
fails identically until an operator repairs the row by hand.

Why this module exists at all
-----------------------------
All of this used to live inside ``hermes_cli/harness_parts/persona_commands.py``,
which made chat-root durability a CLI-LANE concern: every call site of
``_ensure_persona_chat_session`` was an argv handler and NONE were in
``agent_runtime``. The one-call create lane
(:func:`agent_runtime.agent_create.perform_agent_create` — the lane the
launcher's drag-drop reaches over RPC) therefore could not reach it, and the
mint fallbacks in :meth:`PersonaInstanceStore.add_instance` /
:meth:`PersonaInstanceStore.create_operator_chat` bound a
``persona_chat_session_id_for()`` string — a bare ``uuid4`` — with nothing
anywhere creating its row.

Observed live 2026-08-20: an operator dragged a QA agent into the office and
every send was refused with ``UNKNOWN EXPLICIT PERSONA CHAT ROOT:
persona_chat_personainst_qa_agent_03ba2049_67d5a1a6921f``. That root existed in
the instance row, in the create reservation and in the serve read-model, and in
no SessionDB on the machine.

Living in ``agent_runtime`` is the point: the mint sites themselves close the
invariant, so BOTH transports inherit it and so does any future caller of
``add_instance``. ``persona_commands.py`` imports these names under its old
private spellings, so its four pre-existing call sites are unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .persona_assignments import (
    chat_session_owner_instance_id,
    normalize_persona_or_template_id,
    safe_assignment_text,
    safe_assignment_token,
)
from .persona_chat_continuity import PERSONA_CHAT_SESSION_SOURCE


class PersonaChatPersistenceError(RuntimeError):
    """A required canonical persona-chat transcript operation failed.

    The underlying exception remains available through exception chaining for
    logs, while the public message intentionally exposes only the operation and
    exception type so CLI/stream failure frames cannot leak database details.
    """

    def __init__(self, operation: str, cause: BaseException | None = None):
        self.operation = safe_assignment_token(operation) or "unknown"
        self.cause_type = type(cause).__name__ if cause is not None else None
        detail = f" ({self.cause_type})" if self.cause_type else ""
        super().__init__(
            f"canonical persona chat transcript {self.operation.replace('_', ' ')} failed{detail}"
        )


def persona_chat_persistence_failed(
    operation: str,
    exc: BaseException | None,
    *,
    required: bool,
) -> bool:
    error = (
        exc
        if isinstance(exc, PersonaChatPersistenceError)
        else PersonaChatPersistenceError(operation, exc)
    )
    if required:
        if exc is None or error is exc:
            raise error
        raise error from exc
    return False


def default_persona_session_db():
    try:
        from hermes_constants import get_hermes_home_override

        from agent_runtime.chat_session_scope import (
            open_chat_session_db,
            resolve_process_chat_scope,
        )

        # The persona-chat SessionDB is the OPERATOR-visible transcript store —
        # the exact DB ``persona_chat_history`` (the snapshot projection) reads.
        # Under an in-process persona profile override it must bind to the
        # operator home, never the active profile DB, or one shared
        # persona-instance pointer splits across profile-local SessionDBs (the
        # 2026-07-27 cockpit read-lane gap: bindings in the shared runtime root
        # pointing at transcripts minted in ``profiles/alice/state.db``).
        #
        # Fail closed ONLY when NO authority resolved a head at all: with an
        # override active and an AMBIENT scope, the "head" degenerates to the
        # override itself and the operator home is unknown. An authoritative
        # head that happens to EQUAL the override is the legitimate same-DB
        # case — a persona bound to the head profile (e.g. Neko on the seeded
        # base profile) relaying in-process. The former path-equality check
        # conflated the two and killed every relay such a persona sent (live
        # 2026-07-23, chat_session_db_unavailable).
        # Default DB acquisition for the process: no session in hand. A
        # caller that HAS one passes its own scope to open_chat_session_db.
        scope = resolve_process_chat_scope()
        override = get_hermes_home_override()
        if override is not None and not scope.authoritative:
            raise PersonaChatPersistenceError("session_db_acquire")
        db = open_chat_session_db(scope)
        if db is None:
            raise PersonaChatPersistenceError("session_db_acquire")
        return db
    except PersonaChatPersistenceError:
        raise
    except Exception as exc:
        raise PersonaChatPersistenceError("session_db_acquire", exc) from exc


def ensure_persona_chat_session(
    *,
    session_db,
    session_id: str | None,
    persona_id: str | None,
    title: str | None = None,
    required: bool = False,
) -> bool:
    if session_db is None or not session_id:
        return persona_chat_persistence_failed(
            "session_create", None, required=required
        )
    try:
        normalized_persona = normalize_persona_or_template_id(persona_id or "persona")
    except Exception:
        normalized_persona = safe_assignment_token(persona_id) or "persona"
    owner_instance_id = chat_session_owner_instance_id(session_id)
    ownership = {
        "source": PERSONA_CHAT_SESSION_SOURCE,
        "persona_id": normalized_persona,
    }
    if owner_instance_id:
        ownership["persona_instance_id"] = owner_instance_id
    try:
        session_db.create_session(
            session_id=session_id,
            source=PERSONA_CHAT_SESSION_SOURCE,
            model=None,
            model_config=ownership,
            system_prompt=f"Mission Control persona chat for {normalized_persona}",
        )
    except Exception as exc:
        return persona_chat_persistence_failed(
            "session_create", exc, required=required
        )

    safe_title = safe_assignment_text(title, limit=120)
    if not safe_title:
        return True
    try:
        existing_title = session_db.get_session_title(session_id)
    except Exception:
        existing_title = None
    if existing_title:
        return True
    try:
        session_db.set_session_title(session_id, safe_title)
    except Exception:
        pass
    return True


@contextmanager
def _acquired_persona_session_db() -> Iterator[Any]:
    """Acquire the operator chat ``SessionDB`` and CLOSE it on the way out.

    The argv handlers pass a handle they already hold, so this scope exists for
    the STORE-side callers, which hold none. It closes because they are reached
    from a long-lived ``harness serve``: an unclosed SQLite connection per
    created agent is the leak MCF-27 already paid for once
    (``snapshot.persona_session_db_scope`` carries the full argument).
    """

    session_db = default_persona_session_db()
    try:
        yield session_db
    finally:
        close = getattr(session_db, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 — a create must not fail on release
                pass


def ensure_durable_persona_chat_root(
    session_id: str,
    *,
    persona_id: str,
    title: str | None = None,
) -> str:
    """Make *session_id* durable in the operator chat store, or RAISE.

    Returns the same id so a mint site can write
    ``session_id=ensure_durable_persona_chat_root(...)`` and be structurally
    unable to bind a root it did not first persist. Raises
    :class:`PersonaChatPersistenceError` — never a bool — because the whole
    defect this closes is a mint that treated "could not persist" as "carry
    on and bind anyway".
    """

    with _acquired_persona_session_db() as session_db:
        ensure_persona_chat_session(
            session_db=session_db,
            session_id=session_id,
            persona_id=persona_id,
            title=title,
            required=True,
        )
    return session_id


__all__ = [
    "PersonaChatPersistenceError",
    "default_persona_session_db",
    "ensure_durable_persona_chat_root",
    "ensure_persona_chat_session",
    "persona_chat_persistence_failed",
]
