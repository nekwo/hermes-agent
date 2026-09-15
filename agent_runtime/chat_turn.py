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
``archive/runtime-rpc-call-half.md`` already had. Scope is deliberately those
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
#: Gateway Stage 7. The THIRD chat verb, and the only one whose provenance the
#: server derives from the connection rather than reading off params — see
#: :func:`normalize_peer_chat_execute`.
PEER_CHAT_EXECUTE_METHOD = "peer.agent_chat.execute"
#: THE chat-turn vocabulary: every method whose handler ends in
#: :func:`perform_chat_turn`. Minted with the peer verb at gateway Stage 7 and
#: readerless until 2026-09-01, when it was given the reader the three constants
#: above already claim to serve ("spelled once so the manifest test, the serve
#: wiring and the handlers cannot drift").
#:
#: That claim was only half true. The manifest and tier assertions were written
#: PER VERB and in two different files — ``test_serve_rpc_chat_turn`` names the
#: first two, ``test_peer_chat_execute`` names the third — so a fourth chat verb
#: would have joined the lane with no manifest assertion at all and nothing
#: would have said so. ``test_serve_rpc_chat_turn`` now enumerates FROM this
#: tuple, against a live ``serve_rpc.manifest()``, so the day a verb is added
#: here it is advertised and tiered or the suite reds.
#:
#: Add a verb HERE when you add its ``@method`` registration, not after.
CHAT_TURN_METHODS: tuple[str, ...] = (
    CHAT_MESSAGE_METHOD,
    CHAT_STEER_METHOD,
    PEER_CHAT_EXECUTE_METHOD,
)

#: Ceiling on one remote message body. The chat handler has its own caps
#: further in; this one exists at the BOUNDARY so an oversized frame is refused
#: before it is spawned onto a worker, and it is generous rather than tuned —
#: the purpose is to make "a device wedged the pool with a 40 MB paste"
#: unreachable, not to have an opinion about how long an operator writes.
MAX_MESSAGE_LENGTH = 64_000

#: Mirrors the ``client_message_id`` normaliser's cap in the chat handler.
MAX_TURN_REQUEST_ID_LENGTH = 200

#: Cap on ``workspace_name``, matching ``safe_assignment_text(..., limit=120)``
#: in the argv handler that reads it. A boundary that accepted more would hand
#: the handler a string it silently truncates, and the operator would never
#: learn which half arrived.
MAX_WORKSPACE_NAME_LENGTH = 120
#: Cap on ``clarify_token``, matching the handler's own
#: ``safe_assignment_text(..., limit=240)``.
MAX_CLARIFY_TOKEN_LENGTH = 240
#: Cap on ``surface_prompt``, matching ``safe_assignment_text(..., limit=4000)``.
MAX_SURFACE_PROMPT_LENGTH = 4000
#: Cap on ``intent_hint``: the handler reads it through ``safe_assignment_token``,
#: which truncates at 120.
MAX_INTENT_HINT_LENGTH = 120
#: Cap on ``provider`` / ``model``, matching ``_CHAT_PROVIDER_MODEL_RE``'s own
#: ``{1,200}`` in ``persona_commands``. The CHARSET stays the handler's to
#: enforce — it refuses out loud with a sentence naming the allowed characters,
#: and duplicating the regex here would be a second place to keep correct.
MAX_MODEL_OVERRIDE_LENGTH = 200

#: The parser defaults for the two keys that are only lowered when the operator
#: moved them off the default. ``harness mission-chat message`` declares
#: ``--surface-prompt`` default ``""`` and ``--intent-hint`` default ``"chat"``,
#: and the launcher's console decorator puts the default word on EVERY ordinary
#: send — so lowering them unconditionally would put two flags on every remote
#: turn that a local turn does not carry, which is the divergence this whole
#: lane exists to prevent.
SURFACE_PROMPT_DEFAULT = ""
INTENT_HINT_DEFAULT = "chat"

#: R-C8. Every param key :func:`normalize_chat_message` reads, sorted, spelled
#: ONCE — this tuple is what :func:`agent_runtime.serve_rpc.manifest` advertises
#: under ``params``, so the advertisement cannot drift from the code that
#: honours it. A test drives the normaliser through a recording mapping and
#: asserts the two are the same set, which is the only way to keep them in step
#: without a second hand-maintained list.
#:
#: The block exists because of what the C5 field run measured on 2026-09-06:
#: the launcher's lowering could see WHICH methods a runtime has and not WHAT
#: any of them carries, so a console send decorated with ``workspace_name`` fell
#: to the argv arm — and argv to a remote install is a designed wall, not a
#: fallback. Unknown params are still ignored rather than refused (a client
#: cannot be refused for a key this runtime has never heard of), and this list
#: is what lets a client tell "ignored" from "honoured" BEFORE it sends.
CHAT_MESSAGE_PARAMS: tuple[str, ...] = (
    "clarify_token",
    "correlation_id",
    "intent_hint",
    "max_seconds",
    "message",
    "model",
    "new_session",
    "persona_id",
    "persona_instance_id",
    "provider",
    "session_id",
    "stream",
    "surface_prompt",
    "title",
    "turn_request_id",
    "use_agent_default",
    "workspace_id",
    "workspace_name",
)

#: The same, for :func:`normalize_chat_steer` — which R-C8 found already whole,
#: so this tuple advertises a surface rather than growing one. ``title`` is
#: absent on purpose: the steer verb has no such flag and both lanes drop a
#: defaulted one.
CHAT_STEER_PARAMS: tuple[str, ...] = (
    "correlation_id",
    "message",
    "persona_id",
    "persona_instance_id",
    "session_id",
    "turn_request_id",
)

#: The manifest's ``params`` block, before it is turned into JSON lists.
#: ``peer.agent_chat.execute`` is deliberately absent: a device reads the
#: greeting, a peer is admitted by the pairing ceremony, and the two surfaces
#: are declared in different places on purpose.
CHAT_TURN_METHOD_PARAMS: dict[str, tuple[str, ...]] = {
    CHAT_MESSAGE_METHOD: CHAT_MESSAGE_PARAMS,
    CHAT_STEER_METHOD: CHAT_STEER_PARAMS,
}


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
    # R-C8, the rest of the operator's surface. Read here — before the conflict
    # rule and before the argv is built — so that the set of keys this function
    # ASKS FOR is exactly ``CHAT_MESSAGE_PARAMS``, which is what the manifest
    # advertises and what the drift test walks.
    workspace_name = _text(params, "workspace_name", limit=MAX_WORKSPACE_NAME_LENGTH)
    provider = _text(params, "provider", limit=MAX_MODEL_OVERRIDE_LENGTH)
    model = _text(params, "model", limit=MAX_MODEL_OVERRIDE_LENGTH)
    use_agent_default = _flag(params, "use_agent_default")
    clarify_token = _text(params, "clarify_token", limit=MAX_CLARIFY_TOKEN_LENGTH)
    surface_prompt = _text(params, "surface_prompt", limit=MAX_SURFACE_PROMPT_LENGTH)
    intent_hint = _text(params, "intent_hint", limit=MAX_INTENT_HINT_LENGTH)
    new_session = _flag(params, "new_session")
    stream = _flag(params, "stream")
    correlation_id = _correlation_id(params)

    if use_agent_default and (provider or model):
        # The argv handler's own rule (``_requested_chat_model_override`` raises
        # ``ValueError`` for it), moved to the door. Over argv that ValueError
        # surfaces inside the turn, which on this lane would mean an ACCEPTED
        # turn that dies as a handler failure — a client that already holds an
        # ack, a receipt keyed on its ``turn_request_id``, and no answer. The
        # boundary is where a request that cannot be honoured should be refused,
        # and ``ERR_INVALID_PARAMS`` with a reason is what the client branches
        # on.
        raise ChatTurnInvalid(
            "model_override_conflict",
            "invalid params: use_agent_default cannot be combined with provider "
            "or model",
        )

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
    # R-C8's seven, appended after the original set so an existing pin on a
    # send that carries none of them still reads the same argv. Each one is
    # lowered only when the operator actually expressed it: an absent optional
    # is the absence of a flag, not a flag carrying the parser's own default,
    # because the CLI tells "not given" from "given as the default" for several
    # of these and a remote turn must be the same execution as a local one.
    if workspace_name:
        argv += ["--workspace-name", workspace_name]
    if provider:
        argv += ["--provider", provider]
    if model:
        argv += ["--model", model]
    if use_agent_default:
        argv.append("--use-agent-default")
    if clarify_token:
        argv += ["--clarify-token", clarify_token]
    if surface_prompt and surface_prompt != SURFACE_PROMPT_DEFAULT:
        argv += ["--surface-prompt", surface_prompt]
    if intent_hint and intent_hint != INTENT_HINT_DEFAULT:
        argv += ["--intent-hint", intent_hint]

    return ChatTurnRequest(
        verb=CHAT_MESSAGE_METHOD,
        turn_request_id=turn_request_id,
        session_scope=session_id or persona_instance_id or f"persona:{persona_id}",
        argv=argv,
        correlation_id=correlation_id,
    )


#: The ``--requested-by`` prefix a peer-executed turn carries. Beside
#: ``gateway_device`` (a device's, hardcoded in :func:`normalize_chat_message`)
#: and ``agent:<session>`` (a local relay's, ``tools/agent_chat_tool``). The
#: install id after the colon is the ONE variable part, and it is the reason
#: this is a prefix rather than a constant: an operator on B reading their own
#: chat has to be able to see WHICH paired install asked, and "a peer" would not
#: tell them.
PEER_REQUESTED_BY_PREFIX = "peer:"


def normalize_peer_chat_execute(
    params: dict, *, peer_install_id: str
) -> ChatTurnRequest:
    """``peer.agent_chat.execute`` params → the argv a local send would use.

    Gateway Stage 7. The sibling of :func:`normalize_chat_message` and the
    differences are all one difference: **who is asking is not in the params.**

    ``peer_install_id`` is a keyword-only ARGUMENT rather than a param key, and
    that is the whole security posture of this verb expressed as a signature.
    The caller of this function is the RPC handler, which reads the id off
    ``context.caller`` — a value ``call_authorization.caller_for_connection``
    minted from an authenticated connection whose HMAC verified against a row in
    ``gateway/peers.json``. There is no params key by which a peer can name a
    different install, because the field a peer could type does not exist. A
    ``correlation_id``, by contrast, is exactly the thing a caller MAY choose,
    and it is carried here for the reason the plan's own drift addendum names:
    *a token is correlation, NEVER identity.* The two facts arrive on this
    function by two different routes precisely so they cannot be confused.

    **One ``target``, split here rather than by the dialler.** A local send
    splits a ``personainst_*`` handle out of the persona slot before it calls
    the handler; this door does the same with the same rule, because the
    conventions for naming an instance are B's own and A must not have to know
    them. What A sends is the string its agent wrote after the ``/``.

    **No sender field, and the omission is deliberate.** The obvious courtesy is
    a ``sender_persona`` param so B's operator sees which agent on A asked. It
    is not here, because B cannot verify it: it would render, in B's chat, an
    unverified claim about an agent on another machine, styled exactly like a
    verified one. Who asked belongs in the message body, where it reads as what
    it is — something the sender wrote.
    """

    peer_install_id = str(peer_install_id or "").strip()
    if not peer_install_id:
        # Unreachable through the RPC door (its handler refuses a non-peer
        # caller before it gets here) and refused anyway, because a normaliser
        # that silently produced a turn with no provenance would be one edit
        # away from being the door.
        raise ChatTurnInvalid(
            "peer_install_unknown",
            "invalid params: a peer-executed chat turn needs an authenticated "
            "peer install, and this connection proved none",
        )

    turn_request_id = _required_text(
        params, "turn_request_id", limit=MAX_TURN_REQUEST_ID_LENGTH
    )
    target = _required_text(params, "target", limit=200)
    message = _required_text(params, "message", limit=MAX_MESSAGE_LENGTH)
    session_id = _text(params, "session_id", limit=200)
    title = _text(params, "title", limit=200)
    new_session = _flag(params, "new_session")
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

    from .persona_assignments import safe_assignment_token

    instance_id = target if safe_assignment_token(target).startswith("personainst_") else ""

    argv: list[str] = [
        "harness",
        "mission-chat",
        "message",
        "--persona",
        target,
        "--message",
        message,
        "--client-message-id",
        turn_request_id,
        # The one field of the send this server decides. See the docstring.
        "--requested-by",
        f"{PEER_REQUESTED_BY_PREFIX}{peer_install_id}",
        "--json",
    ]
    if session_id:
        argv += ["--session-id", session_id]
    if instance_id:
        argv += ["--persona-instance-id", instance_id]
    if title:
        argv += ["--title", title]
    if new_session:
        argv.append("--new-session")
    if max_seconds is not None:
        argv += ["--max-seconds", repr(max_seconds)]

    return ChatTurnRequest(
        verb=PEER_CHAT_EXECUTE_METHOD,
        turn_request_id=turn_request_id,
        # The peer install rides the replay SCOPE, which the two local verbs
        # have no equivalent of and this one needs: ``turn_request_id`` is
        # minted on the OTHER install, so two paired installs could legitimately
        # present the same one, and a replay answered out of the wrong install's
        # receipt would hand install C the ack for install B's turn. Scoping by
        # the proven id makes that unrepresentable rather than unlikely.
        session_scope=f"{PEER_REQUESTED_BY_PREFIX}{peer_install_id}/"
        + (session_id or instance_id or f"persona:{target}"),
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
    peer_install_id: str | None = None,
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
        elif verb == PEER_CHAT_EXECUTE_METHOD:
            # ``peer_install_id or ""`` rather than a guard here: the normaliser
            # refuses an empty one with its own typed reason, so there is one
            # place that decides what "no proven peer" means.
            request = normalize_peer_chat_execute(
                params, peer_install_id=peer_install_id or ""
            )
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
