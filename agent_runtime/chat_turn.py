"""The chat-turn SERVICE behind ``runtime.chat.message`` / ``runtime.chat.steer``.

Gateway Stage 3. The remote write path's whole point: a paired device must be
able to send a chat turn, and chat is what the gateway is FOR (the chat-first
ruling). Stage 1 made that impossible by accident — it refused the entire argv
lane to devices, correctly, and mission-chat had no method lane to fall back to.
This module is that lane.

Why a method lane at all, when the plan's Stage 3 sketch said "RPC where methods
exist, argv otherwise — same union"
-----------------------------------------------------------------------------
Because the "otherwise" arm is closed. ``serve.py``'s device branch refuses
``{"argv": [...]}`` outright, and that refusal is load-bearing rather than
incidental: the tier gate lives on the method lane, so an argv lane open to
devices would make every tier declaration bypassable in one frame. Gating argv
instead would mean assigning a tier to every CLI verb in this repo and keeping
that map correct forever. So the union is not "RPC or argv" for a device; it is
RPC, and porting the two chat-turn verbs onto it is the direction
``planned/runtime-rpc-call-half.md`` already had. Scope is deliberately those
two verbs and not the argv surface.

One service, two doors — and the door is LOWER than usual
---------------------------------------------------------
``runtime.agent.retire``'s door calls ``perform_agent_retire``, the same
function the CLI calls. Mission-chat has no such function: its service IS
``persona_commands._cmd_mission_chat_message``, a handler that takes an
argparse namespace, and the one existing second door onto it
(``dispatch_delivery.deliver_via_mission_chat``) reaches it by BUILDING a
namespace. So this door lowers one step further and builds ARGV, which
``serve.py``'s worker then dispatches through the same argparse tree a local
send goes through.

That is a stronger no-divergence guarantee than a parallel Python call site,
not a weaker one: the RPC door and the argv door are not two doors onto one
service, they are one door with a typed, authorized, deduped front. A remote
turn and a local turn are the SAME execution — same handler, same lease, same
journal, same frames — and no future edit to the chat handler can move one
without moving the other. The argv is built as a LIST from validated typed
params; nothing a client sends can become a flag, because flags are literals
here and values are always the element after them.

The local stdio lane is untouched. The launcher's local session keeps lowering
argv exactly as it does today; nothing in this stage switches it.

Streaming, v1
-------------
The ack is NON-STREAMING and says only that the turn was accepted. The turn's
frames — deltas when ``stream`` is asked for, the final ``--json`` payload
always — ride the EXISTING per-request frame lane under the ``request_id`` this
ack returns, which is the same lane the local launcher already reads and the
same lane a remote read subscriber is already attached to. No second streaming
transport is invented, and none is needed: the socket lane has carried
per-request frames since it existed.

The ack cannot be the turn's reply, and that is forced rather than chosen.
``serve.py`` answers the method lane INLINE on the reader loop — its own comment
says the pool exists "for handlers that block — chat turns, streams" — so a
method that ran a turn would stall every other client on this serve for the
length of it. Accept-and-hand-off is the only shape the lane admits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .chat_turn_reservations import (
    STATE_ACCEPTED,
    ChatTurnReservationError,
    forget_chat_turn as _forget,
    reserve_chat_turn,
)

#: The two method names, spelled once so the manifest test, the serve wiring and
#: the handlers cannot drift.
CHAT_MESSAGE_METHOD = "runtime.chat.message"
CHAT_STEER_METHOD = "runtime.chat.steer"
CHAT_TURN_METHODS: tuple[str, ...] = (CHAT_MESSAGE_METHOD, CHAT_STEER_METHOD)

#: Ceiling on one remote message body. The chat handler has its own caps
#: further in; this one exists at the BOUNDARY so an oversized frame is refused
#: before it is spawned onto a worker, and it is generous rather than tuned —
#: the purpose is to make "a device wedged the pool with a 40 MB paste"
#: unreachable, not to have an opinion about how long an operator writes.
MAX_MESSAGE_LENGTH = 64_000

#: Mirrors the ``client_message_id`` normaliser's cap in the chat handler.
MAX_TURN_REQUEST_ID_LENGTH = 200


class ChatTurnInvalid(Exception):
    """A refusable request. ``reason`` is what the client branches on."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class ChatTurnSpawnRefused(Exception):
    """The transport will not start a turn right now. Raised BY the seam.

    The drain is the case that forced this to exist. ``serve.py`` deliberately
    keeps answering the method lane while draining, on the argued grounds that
    an inline handler "cannot be cut off half-done" — the office writes land
    atomically and the replacement runtime reads the same file. A chat turn is
    the counter-example that argument itself names: it is precisely the work the
    drain exists to protect, and accepting one onto a pool that is being joined
    would be the recycle-mid-turn defect with an ack on it. So the seam refuses,
    and it refuses HERE rather than by returning ``None`` for its whole self,
    because "this transport has no worker lane" and "this transport is shutting
    down" are different facts and a client retries only one of them.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ChatTurnRefusal:
    code: int
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ChatTurnOutcome:
    result: dict[str, Any] | None = None
    refusal: ChatTurnRefusal | None = None


@dataclass(frozen=True)
class ChatTurnRequest:
    """One validated remote chat turn, ready to be lowered to argv."""

    verb: str
    turn_request_id: str
    #: The scope a replayed ``turn_request_id`` is checked against. The chat
    #: root when the client named one, otherwise the persona reference the send
    #: is aimed at — because a send that mints its own thread has no root yet
    #: and "no scope" would let one key answer for two different targets.
    session_scope: str
    argv: list[str]
    correlation_id: str | None = None


# ── param normalisation ──────────────────────────────────────────────────────


def _text(params: dict, key: str, *, limit: int) -> str:
    raw = params.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ChatTurnInvalid(
            f"{key}_invalid", f"invalid params: {key} must be a string when sent"
        )
    value = raw.strip()
    if len(value) > limit:
        raise ChatTurnInvalid(
            f"{key}_invalid",
            f"invalid params: {key} must be {limit} characters or fewer",
        )
    return value


def _required_text(params: dict, key: str, *, limit: int) -> str:
    value = _text(params, key, limit=limit)
    if not value:
        raise ChatTurnInvalid(
            f"{key}_required", f"invalid params: {key} must be a non-empty string"
        )
    return value


def _flag(params: dict, key: str) -> bool:
    raw = params.get(key)
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ChatTurnInvalid(
            f"{key}_invalid", f"invalid params: {key} must be a boolean when sent"
        )
    return raw


def _correlation_id(params: dict) -> str | None:
    """Validated at the boundary, echoed on the ack, and — today — carried no
    further.

    ``harness mission-chat message`` has no ``--correlation-id`` flag, so unlike
    the six office/agent write verbs there is nowhere for the token to ride onto
    the turn's events. The honest options were to drop it silently, to refuse it,
    or to accept-and-echo. Dropping is the shape ``_correlation_id_param``'s own
    docstring already argues against (a client would diagnose with an id the
    server discarded); refusing would make the launcher's correlation discipline
    unusable on the one lane the gateway is built for. So it is accepted, fenced
    by the SAME charset/cap normaliser every other verb uses — a token that fails
    it is refused out loud, never repaired — and echoed so a client can join its
    own send to its own ack.

    What it does NOT yet do is join the turn's events, which is a named gap owned
    by ``planned/correlation-id-coverage.md`` and not by this stage: closing it
    means an argv flag on the chat verb, i.e. a change to the local lane, and
    this stage's contract is that the local lane does not move.
    """

    from agent_runtime.state_patches import (
        CORRELATION_ID_MAX_LEN,
        normalize_correlation_id,
    )

    raw = params.get("correlation_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ChatTurnInvalid(
            "correlation_id_invalid",
            "invalid params: correlation_id must be a string or omitted",
        )
    token = normalize_correlation_id(raw)
    if token is None:
        raise ChatTurnInvalid(
            "correlation_id_invalid",
            "invalid params: correlation_id must be a generated token of at most "
            f"{CORRELATION_ID_MAX_LEN} characters from [A-Za-z0-9_.:-]",
        )
    return token


def normalize_chat_message(params: dict) -> ChatTurnRequest:
    """``runtime.chat.message`` params → the argv a local send would have used."""

    turn_request_id = _required_text(
        params, "turn_request_id", limit=MAX_TURN_REQUEST_ID_LENGTH
    )
    persona_id = _required_text(params, "persona_id", limit=200)
    message = _required_text(params, "message", limit=MAX_MESSAGE_LENGTH)
    session_id = _text(params, "session_id", limit=200)
    persona_instance_id = _text(params, "persona_instance_id", limit=200)
    workspace_id = _text(params, "workspace_id", limit=200)
    title = _text(params, "title", limit=200)
    new_session = _flag(params, "new_session")
    stream = _flag(params, "stream")
    correlation_id = _correlation_id(params)

    max_seconds_raw = params.get("max_seconds")
    max_seconds: float | None = None
    if max_seconds_raw is not None:
        if isinstance(max_seconds_raw, bool) or not isinstance(
            max_seconds_raw, (int, float)
        ):
            raise ChatTurnInvalid(
                "max_seconds_invalid",
                "invalid params: max_seconds must be a number when sent",
            )
        max_seconds = float(max_seconds_raw)
        if not (max_seconds > 0):
            raise ChatTurnInvalid(
                "max_seconds_invalid",
                "invalid params: max_seconds must be greater than zero",
            )

    argv: list[str] = [
        "harness",
        "mission-chat",
        "message",
        "--persona",
        persona_id,
        "--message",
        message,
        # THE key. The gateway plan calls it ``turn_request_id``; mission chat
        # has called it ``client_message_id`` since long before this stage, and
        # the turn journal keyed on that name is what already makes a repeated
        # send an ``idempotent_replay`` rather than a second turn. The bytes are
        # passed through unchanged so the two names are provably one value.
        "--client-message-id",
        turn_request_id,
        # A device is not the local CLI, and the provenance says so. This is the
        # one field of the send that the SERVER decides: it is not a param,
        # cannot be a param, and a client that could type it would be naming its
        # own provenance.
        "--requested-by",
        "gateway_device",
        "--json",
    ]
    if session_id:
        argv += ["--session-id", session_id]
    if persona_instance_id:
        argv += ["--persona-instance-id", persona_instance_id]
    if workspace_id:
        argv += ["--workspace-id", workspace_id]
    if title:
        argv += ["--title", title]
    if new_session:
        argv.append("--new-session")
    if stream:
        argv.append("--stream")
    if max_seconds is not None:
        argv += ["--max-seconds", repr(max_seconds)]

    return ChatTurnRequest(
        verb=CHAT_MESSAGE_METHOD,
        turn_request_id=turn_request_id,
        session_scope=session_id or persona_instance_id or f"persona:{persona_id}",
        argv=argv,
        correlation_id=correlation_id,
    )


def normalize_chat_steer(params: dict) -> ChatTurnRequest:
    """``runtime.chat.steer`` params → ``harness mission-chat steer`` argv."""

    turn_request_id = _required_text(
        params, "turn_request_id", limit=MAX_TURN_REQUEST_ID_LENGTH
    )
    session_id = _required_text(params, "session_id", limit=200)
    message = _required_text(params, "message", limit=MAX_MESSAGE_LENGTH)
    persona_id = _text(params, "persona_id", limit=200)
    persona_instance_id = _text(params, "persona_instance_id", limit=200)
    correlation_id = _correlation_id(params)

    argv: list[str] = [
        "harness",
        "mission-chat",
        "steer",
        "--session-id",
        session_id,
        "--message",
        message,
        "--client-message-id",
        turn_request_id,
        "--json",
    ]
    if persona_id:
        argv += ["--persona", persona_id]
    if persona_instance_id:
        argv += ["--persona-instance-id", persona_instance_id]

    return ChatTurnRequest(
        verb=CHAT_STEER_METHOD,
        turn_request_id=turn_request_id,
        session_scope=session_id,
        argv=argv,
        correlation_id=correlation_id,
    )


# ── the accept ───────────────────────────────────────────────────────────────

#: Refusal codes, spelled where both the service and its tests can name them.
#: The numbers are ``serve_rpc``'s and are imported at the door rather than here
#: so this module stays free of the dispatcher (the ``call_authorization``
#: import direction, applied to a service).
REASON_LANE_UNAVAILABLE = "chat_turn_lane_unavailable"

#: Prefix on every server-minted chat-turn request id. Serve request ids are
#: normally CLIENT-chosen, and the inflight table is keyed per owner precisely
#: because two connections may legitimately pick the same one. A server-minted
#: id shares that namespace, so it is prefixed AND random: the prefix makes it
#: greppable in a frame log as "the runtime chose this", and the uuid makes a
#: collision with a client's own id a non-question.
CHAT_TURN_REQUEST_ID_PREFIX = "chat-"


def mint_chat_turn_request_id() -> str:
    import uuid

    return f"{CHAT_TURN_REQUEST_ID_PREFIX}{uuid.uuid4().hex[:16]}"


def perform_chat_turn(
    params: dict,
    *,
    verb: str,
    spawn: Callable[[str, list[str], str], None] | None,
) -> ChatTurnOutcome:
    """Validate, dedupe, and hand ONE chat turn to a worker.

    ``spawn`` takes the minted request id, the argv, and the ``turn_request_id``
    the receipt is keyed on, and starts the turn on a worker. It may raise
    :class:`ChatTurnSpawnRefused` to decline (a drain). It is the transport's, injected rather than imported, for the reason
    every seam in this lane is injected: a service that reached into
    ``serve.py`` for a thread pool could not be tested without one, and a
    context built by a test or by a future non-duplex transport must be able to
    say honestly that it has no worker lane.

    The ORDER is reserve → record → spawn, and never reserve → spawn → record.
    See ``ChatTurnReservation.mark_accepted`` for which way this lane chooses to
    lose.
    """

    from .serve_rpc import ERR_CONFLICT, ERR_HANDLER_FAILED, ERR_INVALID_PARAMS

    try:
        if verb == CHAT_MESSAGE_METHOD:
            request = normalize_chat_message(params)
        elif verb == CHAT_STEER_METHOD:
            request = normalize_chat_steer(params)
        else:  # pragma: no cover - the registry is the only caller
            raise ChatTurnInvalid("unknown_chat_verb", f"unknown chat verb: {verb}")
    except ChatTurnInvalid as exc:
        return ChatTurnOutcome(
            refusal=ChatTurnRefusal(
                code=ERR_INVALID_PARAMS,
                message=exc.message,
                data={"reason": exc.reason},
            )
        )

    if spawn is None:
        # Not an error and not a fallback: a transport with no worker lane
        # cannot run a chat turn, and pretending otherwise would mean running it
        # inline on whatever loop asked. Refused with its own reason so a client
        # can tell "this runtime will not" from "this runtime cannot here".
        return ChatTurnOutcome(
            refusal=ChatTurnRefusal(
                code=ERR_HANDLER_FAILED,
                message=(
                    "this transport has no chat-turn worker lane; chat turns are "
                    "accepted on a serve loop"
                ),
                data={"reason": REASON_LANE_UNAVAILABLE},
            )
        )

    try:
        with reserve_chat_turn(
            turn_request_id=request.turn_request_id,
            verb=request.verb,
            session_scope=request.session_scope,
        ) as reservation:
            if reservation.replayed and reservation.state is not None:
                return ChatTurnOutcome(result=reservation.replay_ack())
            ack: dict[str, Any] = {
                "turn_request_id": request.turn_request_id,
                "accepted": True,
                "state": STATE_ACCEPTED,
                "verb": request.verb,
            }
            if request.correlation_id is not None:
                ack["correlation_id"] = request.correlation_id
            # The request id is minted HERE, before the durable write, which is
            # what lets the receipt record the very stream a reconnecting client
            # must re-attach to. A seam that minted it inside the submit and
            # returned it afterwards would force record-after-spawn, i.e. the
            # ordering ``mark_accepted`` exists to refuse.
            request_id = mint_chat_turn_request_id()
            ack["request_id"] = request_id
            reservation.mark_accepted(ack, request_id=request_id)
            try:
                spawn(request_id, request.argv, request.turn_request_id)
            except ChatTurnSpawnRefused as exc:
                # The receipt is REMOVED, not left behind. A refusal is not an
                # accept, and a receipt that outlived one would answer the
                # client's honest retry with ``idempotent_replay`` for a turn
                # that never ran — the over-claim ``mark_accepted`` accepts for
                # a CRASH is not one to keep for a decision this process made
                # deliberately and can still undo.
                _forget(request.turn_request_id)
                return ChatTurnOutcome(
                    refusal=ChatTurnRefusal(
                        code=ERR_CONFLICT,
                        message=exc.message,
                        data={"reason": exc.reason},
                    )
                )
            ack["idempotent_replay"] = False
            ack["settled"] = False
            return ChatTurnOutcome(result=ack)
    except ChatTurnReservationError as exc:
        return ChatTurnOutcome(
            refusal=ChatTurnRefusal(
                code=ERR_CONFLICT,
                message=str(exc),
                data={"reason": exc.code},
            )
        )
