"""A chat turn publishes its own START and END on the stream lane.

Stage C1h-bis of ``remote-chat-parity.md``. C1h measured the hole this module
closes, on a real serve: a turn started over ``runtime.chat.message`` has its
``running_work`` ``chat_turn`` row in the projection the instant the write-ahead
journal record lands, but **nothing published a frame on the turn's own
account**. The stream hub is event-driven — ``stream.stream_frames`` tails the
EventLog and rebuilds a core when the offset moves — and a chat turn running a
model appends nothing of its own between the write-ahead and the projection
commit. So a SECOND console holding only the ``stream`` lane (the Mac's own
launcher, while Windows prompts it) learned of a running turn late, or not until
an unrelated write moved the log; C1h's own measurement test had to issue five
real ``runtime.agent.create`` calls mid-turn before it could sample a row.

The frame is the ``running_work`` DELTA, not a new frame kind
-------------------------------------------------------------
There is no new wire vocabulary here and there deliberately is none. The
projection already carries the row (``running_work._collect_chat_turns``, off
``mission_chat_turns.inflight_turn_rows``); what was missing was a reason for
the hub to look. An EventLog append IS that reason and is the only one — the
producer's other wake-ups are its heartbeat and the Stage-12 freshness
backstop, and the backstop exists precisely to name a write that appended no
event as a producer bug. So this module appends an event and the existing
pipeline does the rest, exactly as the DISPATCH lane of the same projection
already works: ``dispatch.recorded`` when a detached dispatch starts,
``dispatch.completed`` when it settles, and the row appears and disappears on
subscribers' screens because of them.

Both doors, one place
---------------------
``runtime.chat.message`` (the method a paired console calls) lowers to argv and
is dispatched through the same argparse tree ``harness mission-chat message``
takes — see ``agent_runtime/chat_turn.py``'s header. So the two entry points
share ONE core, ``persona_commands._cmd_mission_chat_message`` /
``_mission_chat_commit_turn``, and the publish belongs there rather than in the
method shim: a shim-side publish would leave every locally-typed turn (the lane
the local launcher still uses) unpublished.

Fail-safe, and never a turn's problem
-------------------------------------
A notification is not the turn. Every append here is wrapped: a full event log,
an unwritable store or a contract rejection returns ``False`` and the turn
proceeds untouched. The precedent is ``_publish_persona_chat_projection_event``
one lane over, whose comment states the same rule for the same reason — the
reply is already durable; a missed notification must not cost it.

END is published exactly once, and only if START was
----------------------------------------------------
:meth:`ChatTurnPresence.publish_ended` is a no-op unless :meth:`publish_started`
actually appended. That keeps the pairing honest on the paths that never reach
the write-ahead at all — a busy chat root, a refused send, a missing message —
where an unpaired "the turn ended" would announce a turn that never began. It
is also idempotent: the caller runs it from a ``finally``, and a second call
after a normal one publishes nothing.

The END event's ``state`` is READ from the journal at publish time rather than
passed in. The commit function has fourteen terminal transitions and a caller
that sees only an exit code; asking the store what state the record is actually
in is the one answer that cannot drift from what the projection will report,
which is the whole point of publishing at all. A record that has been garbage
collected answers ``"absent"`` — also a fact, and the one a consumer needs (the
row is gone).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "EVENT_TURN_ENDED",
    "EVENT_TURN_STARTED",
    "ChatTurnPresence",
]

#: Registered in :mod:`agent_runtime.decision_contract_registry` in the same
#: commit as this emitter, per S55.
EVENT_TURN_STARTED = "persona_chat.turn_started"
EVENT_TURN_ENDED = "persona_chat.turn_ended"

#: What :meth:`ChatTurnPresence.publish_ended` reports when the turn journal has
#: no record for the turn any more. Not ``"unknown"``: the store answered, and
#: "the record is gone" is a different fact from "the state could not be read".
STATE_ABSENT = "absent"


class ChatTurnPresence:
    """One turn's two stream publishes. Built per turn, used from two places.

    Constructed by the chat-turn core's caller so the END publish can live in
    its ``finally`` — the only place on that function that every exit passes
    through — while the START publish stays where it belongs, immediately after
    the write-ahead journal record that makes the row real.
    """

    __slots__ = ("_identity", "_started", "_ended")

    def __init__(self) -> None:
        self._identity: dict[str, Any] | None = None
        self._started = False
        self._ended = False

    @property
    def started(self) -> bool:
        """Whether a START frame was published for this turn."""

        return self._started

    def publish_started(
        self,
        *,
        session_id: str,
        client_message_id: str,
        turn_id: str,
        persona_id: str,
        persona_instance_id: str,
        active_session_id: str | None = None,
    ) -> bool:
        """Announce a turn that is now in the projection. Returns whether it landed.

        Call AFTER the write-ahead journal transition persisted, never before:
        the frame the hub builds from this event is a fresh projection, and a
        projection taken before the record exists carries no row — which is the
        "published too early" shape that reads exactly like "never published".
        """

        if self._started:
            return False
        identity = {
            "persona_instance_id": str(persona_instance_id or ""),
            "root_chat_session_id": str(session_id or ""),
            "client_message_id": str(client_message_id or ""),
            "turn_id": str(turn_id or client_message_id or ""),
            "active_session_id": str(active_session_id or session_id or ""),
        }
        self._identity = dict(identity, persona_id=str(persona_id or ""))
        if not self._append(EVENT_TURN_STARTED, identity):
            return False
        self._started = True
        return True

    def publish_ended(self) -> bool:
        """Announce that the turn left the in-flight set. Returns whether it landed.

        A no-op unless :meth:`publish_started` published, and a no-op on every
        call after the first.
        """

        if not self._started or self._ended or self._identity is None:
            return False
        self._ended = True
        identity = {
            key: value
            for key, value in self._identity.items()
            if key != "persona_id"
        }
        return self._append(
            EVENT_TURN_ENDED,
            dict(identity, state=self._journal_state()),
        )

    # -- internals ---------------------------------------------------------

    def _journal_state(self) -> str:
        """The journal's own word for where this turn ended up."""

        identity = self._identity or {}
        try:
            from .mission_chat_turns import mission_chat_turn_record

            record = mission_chat_turn_record(
                session_id=identity.get("root_chat_session_id", ""),
                client_message_id=identity.get("client_message_id", ""),
            )
        except Exception:
            return STATE_ABSENT
        if not isinstance(record, dict):
            return STATE_ABSENT
        return str(record.get("state") or "") or STATE_ABSENT

    def _append(self, event_type: str, payload: dict[str, Any]) -> bool:
        """One bounded, fail-safe EventLog append."""

        identity = self._identity or {}
        try:
            from hermes_time import now

            from .events import EventLog
            from .models import Event

            EventLog().append(
                Event(
                    type=event_type,
                    # Chat is the only lane (contract 45): a chat turn has no
                    # task binding to report, so these are constants rather
                    # than values that could only ever be None.
                    task_id=None,
                    run_id=None,
                    persona_id=str(identity.get("persona_id") or "") or None,
                    ts=now(),
                    payload=payload,
                    session_id=payload.get("root_chat_session_id") or None,
                    turn_id=payload.get("turn_id") or None,
                )
            )
        except Exception:
            # A notification is not the turn. See the module docstring.
            return False
        return True
