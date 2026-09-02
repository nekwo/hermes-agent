"""The METHOD lane: named JSON-RPC 2.0 methods on the serve transports.

Increment 1 of the "CALL half" ruled in the launcher's
``docs/mission_control/DECISION_push_and_rpc_2026-08-13.md`` §3, and framed by
launcher commit ``fa2226750``: **mirror ``tui_gateway``'s JSON-RPC 2.0 shape;
do not invent a third convention.** Everything structural here is copied from
``tui_gateway/server.py`` rather than re-decided — the frames (``:1656``/
``:1660``), the ``@method`` registry (``:1664``), the request normalizer
(``:1672``) and the error-code vocabulary:

===========  ===========================================================
``-32600``   invalid request (not an object / no method / bad jsonrpc)
``-32601``   unknown method
``-32602``   invalid params (missing, wrong type, malformed)
``-32000``   the handler raised — mirrors ``server.py:1733``
``4001``     the named entity does not exist (upstream: "session not found")
``4090``     the write lost a race with the store (fork-minted — see below)
===========  ===========================================================

``4001`` is reused verbatim rather than minted: upstream already spends it on
exactly this meaning, and a fork that renumbers "not found" makes a future
union of the two dispatchers a translation layer instead of a merge.

``4090`` had to be minted — upstream has no concurrency code, because its
domain (one TUI session, one writer) never contends. The NUMBER is chosen to
survive that future merge: upstream allocates its 4xxx band sequentially from
4001 and has reached 4020, and ``4002`` — the number a first guess reaches for —
is already spent there on "invalid value", so taking it would have collided on
day one. 4090 sits far above the live allocation and reads as HTTP 409, which
is the one number every client developer already associates with this meaning.

Codes are the coarse family — "a guard refused this write" — and
``data.reason`` is the branch point (see :func:`err`). 4090 carries FOUR
reasons, and conflating any two of them would be the whole bug, because each
has a different cure:

``stale_revision``
    The client's own prediction is behind. Refetch and rebase.
``sync_conflict``
    A realm-sync sidecar is unresolved. NO amount of refetch-and-retry clears
    it — it needs an operator running ``harness office resolve-conflict``. A
    client that retried this one would spin forever.
``class_key_collision``
    The write is class-keyed and would undo the class→instance re-key
    migration (``office_class_key_guard``). Neither refetching nor retrying
    helps; the client must name WHICH instance it is placing.
``actor_archived``
    The key was DELETED on this server (D1). Terminal: the client drops its
    local row. Refetching confirms the absence and retrying re-opens the wedge
    this fence closed — re-placing the agent is a new create with a new id,
    never a re-add of this key.

Why this is a lane and not a replacement
----------------------------------------
``hermes_cli/harness_parts/serve.py`` dispatches ``{"id","argv"}`` frames into
the harness argparse tree. That lane is byte-identity tested and stays the
fallback; this one sits BESIDE it. A frame is routed here when it carries
``jsonrpc`` or ``method`` — neither of which the argv lane has ever sent — so
the discrimination costs the argv lane nothing and cannot be ambiguous.

Both serve transports get it for free: ``serve.py``'s dispatcher is already
transport-agnostic, so one call site serves stdio and the socket alike.

How a client learns the method set
----------------------------------
``RPC_CONTRACT_VERSION`` + the method names ride the frames a client ALREADY
reads to learn what it is talking to — ``ready`` on stdio, ``hello_ok`` on the
socket, and the re-askable ``{"op":"version"}`` reply on both. That is the
``hello_contract`` precedent (``agent_runtime/serve_socket.py:217``) followed
rather than a parallel discovery scheme: the server advertises, the client
asserts. See :func:`manifest`.

The version is a SET plus an integer, not an integer alone. The integer moves
when the SHAPE of an existing method changes (a client that folds v1 must
refuse v2); the set grows when a method is added, which needs no version bump
because a client only ever calls methods it found in the set. This is the same
reasoning as Stage 2d's ``fold_entities``: naming the capabilities lets them be
adopted one at a time, where a bare number couples them.

Projection rule (decision doc, Stage 2b)
----------------------------------------
The runtime owns who exists, where it sits, what state it is in, and a POINTER
to the character class. The launcher owns what that class looks like. So
``persona_id`` crosses the wire and nothing cosmetic does — and so does
``persona_instance_id``, which is identity (WHICH one of a class is placed
here), not cosmetics, and which no client can derive from the ids it already
has. The bound is the snapshot's own ``MAX_OFFICE_ACTORS_PROJECTED``, reused
rather than re-declared, and a truncation is ACCOUNTED (``actors_truncated``) —
a silent cut that reads as an empty office is the failure this whole document
is about. So is the OTHER way the list can be short: an actor file that exists
and will not decode is counted too (``actors_unreadable``), because the store's
skip-and-continue used to hand this projection a shortened list that then
computed its own truncation from the shortened length and arrived at zero.

Prediction and reconciliation (decision doc, Stage 2c)
-----------------------------------------------------
The write leg is game netcode, not request/response: the launcher renders a
drag immediately as a PREDICTION, sends it, and reconciles against the server's
answer. Two consequences shape ``runtime.office.upsert``.

The ack is LIGHT — ``{actor_key, revision}`` and nothing else. Returning the
re-projected actor would be returning the client its own input plus a number,
on the hot path of a drag, and would tempt a client to adopt the echo as truth
instead of keeping the prediction it already drew. The two fields are the two
facts the client provably cannot compute: ``actor_key`` because the store
canonicalizes the identity triple at its own boundary (drift aliases such as
``persona_personainst_dev_agent_*`` collapse), and ``revision`` because it is
the token the NEXT write must present.

Which is why ``revision`` also rides every item of the READ projection. It is
the actor's, repeated onto its items exactly as ``persona_instance_id`` is,
because ``expect_revision`` guards the ACTOR row while the surface-level
``revision`` beside it is the SURFACE's — and the surface's does not move when
an actor moves (``OfficeStore.upsert_actor`` rewrites the actor file and leaves
``office.json`` alone). Without it a client had no honest first value for
``expect_revision`` and could only write unguarded, which is precisely the
lost-update the guard exists to prevent. Additive, so the integer holds: the
launcher's item decoder gates on required-key PRESENCE
(``mission_office_rpc.dart:260``) and never on a key count.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .call_authorization import (
    STDIO_OWNER,
    TIER_CONSOLE,
    TIER_READ,
    TIERS,
    RpcCaller,
    authorize_call,
    caller_for_connection,
)
from .store_file_io import iso_stamp as _now_iso

logger = logging.getLogger(__name__)

# The method-surface contract. Bump ONLY when an existing method's request or
# result shape changes incompatibly; adding a method does not move it.
RPC_CONTRACT_VERSION = 1

JSONRPC_VERSION = "2.0"

# Mirrored from tui_gateway/server.py — see the module docstring.
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_HANDLER_FAILED = -32000
ERR_NOT_FOUND = 4001
# Fork-minted; see the module docstring for why the number is 4090 and not the
# 4002 a first guess reaches for.
ERR_CONFLICT = 4090

_METHODS: dict[str, Callable[[Any, dict, "RpcContext"], dict]] = {}

#: name → the tier a caller must hold to run it. A PARALLEL registry rather than
#: a field on the handler, for the same reason ``_METHODS`` is a dict and not an
#: attribute sweep: the manifest and the gate both want the whole mapping, and a
#: thing you can iterate is a thing a test can assert is complete. Every entry is
#: written by :func:`method`, which has no default — see there.
#:
#: The classification rule is one line: **a level MUTATION is ``console``,
#: everything else is ``read``.** So the four ``runtime.office`` writes and both
#: ``runtime.agent`` verbs are ``console`` (the word their own docstrings and
#: canon 06 already used), while ``get`` / ``subscribe`` / ``unsubscribe`` are
#: ``read``.
#:
#: ``runtime.persona.prewarm`` is the one row worth arguing, and it is ``read``:
#: its contract is that it "writes no store state, emits no event and mints no
#: id", which is the same sentence that makes ``runtime.office.get`` a read. It
#: spends CPU, but spending CPU is a rate-limiting question and rate limiting is
#: not a tier — a viewer device that may not place an agent may certainly warm
#: the cache that makes its own reads fast.
#:
#: The gateway's two chat verbs (Stage 3) are the row that stretches the one-line
#: rule, and they are ``console``: a chat turn is not itself a level mutation,
#: but it RUNS AN AGENT WITH TOOLS, which can place, retire, write and dispatch.
#: A tier below ``console`` for them would be a door around ``console``. The full
#: argument, including why a new ``chat`` word would have refused every
#: already-paired console device, is on ``_runtime_chat_message``.
_METHOD_TIERS: dict[str, str] = {}


def ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": rid, "result": result}


def err(rid: Any, code: int, message: str, data: dict | None = None) -> dict:
    """A JSON-RPC error frame.

    ``code``/``message`` are upstream's ``_err`` exactly. ``data`` is the
    JSON-RPC 2.0 spec's own optional member, not a fork extension, and it is
    populated so a client can branch on a machine-readable ``reason`` instead
    of pattern-matching a human sentence — a message is for an operator's eyes
    and is free to change.
    """

    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": rid, "error": error}


def notification(method_name: str, params: dict) -> dict:
    """A JSON-RPC 2.0 NOTIFICATION — a frame the runtime sends UNPROMPTED.

    The PUSH half of the union ruling, in its smallest form. Structurally it is
    a request object minus ``id`` (spec §4.1), and the ``id`` key is ABSENT
    rather than ``null``: ``null`` is a legal request id, so a strict client
    that correlates on key presence would file a null-id push into its
    pending-call table and leak the entry forever. Our own launcher happens to
    be tolerant here — it treats ``"id": null`` as a notification on purpose
    (``mission_control_serve_session_io.dart:691``) — so this is a contract
    choice for the next client, not a fix for the current one.

    There is NO reply, and therefore no error channel: a notification the
    transport cannot deliver is dropped. Whoever owns the fan-out has to
    ACCOUNT for that drop, because the client's only other way to learn it
    missed one is the sequence gap. That is why the patch lane carries ``seq``
    and why a dropped subscriber is closed rather than quietly skipped.
    """

    return {"jsonrpc": JSONRPC_VERSION, "method": method_name, "params": params}


@dataclass(frozen=True)
class RpcContext:
    """WHO is calling — the half of a request that is not in its ``params``.

    Handlers took ``(rid, params)`` for the whole life of the CALL half, which
    was right while every method was request/response: an answer goes back the
    way the question came, and the transport already knew how. It stops being
    right the moment a method's job is to keep talking AFTERWARDS.
    ``runtime.office.subscribe`` cannot be written against ``(rid, params)`` at
    all — not because the emitter is missing (``SocketConnection.emit`` and
    ``ServeSocketServer.broadcast`` have always existed) but because a handler
    had no way to name the connection it would later push to. That was the
    first added argument, and for the whole life of the notification lane it
    was the only one.

    ``emit`` is the caller's own frame sink, already per-connection and stable
    across a connection's lifetime (``serve.py``'s ``_sink_for``). ``None``
    means the caller has no push channel, which is the honest state for a
    context built by a test or by a future non-duplex transport — a method that
    needs one must refuse rather than assume.

    ``connection_key`` is the SUBSCRIPTION identity: it is what the teardown
    path (``connection_sinks.pop`` on drop) can name, so a registry keyed on it
    can be swept when the socket dies. On stdio there is one implicit caller
    and no key, which is why a stdio subscribe is a different question from a
    socket one rather than the same code with a null.

    ``caller`` (Stage A2) is the second, and the only one that is not about
    talking back. It is what the transport PROVED about who is asking — the
    argument the front-door gate needs and the one no field here could supply,
    because ``connection_key`` is a subscription name and ``transport`` is a
    lane, and neither is an identity. It is built in one place
    (``call_authorization.caller_for_connection``, called from ``serve.py``'s
    dispatcher) and is never assembled from ``params``: a request that could
    name its own caller would be a request that authorizes itself.

    ``spawn_chat_turn`` (gateway Stage 3) is the third, and the first that is
    about work rather than about who or where. Every method before it finished
    on this thread; a chat turn runs for seconds to minutes, and the method lane
    is answered INLINE on the reader loop (see ``serve.py``'s method-lane
    comment, which names chat turns as the reason the pool exists). So the chat
    methods do not run their turn — they hand it to the transport's worker lane
    through this seam and ack. ``None`` means the caller has no worker lane,
    which is the honest state for a test-built context, and the chat methods
    REFUSE on it rather than falling back to running the turn inline: an inline
    chat turn would stall every other client on this serve for its whole length.
    """

    connection_key: str | None = None
    transport: str = "stdio"
    emit: Callable[[dict], None] | None = None
    caller: RpcCaller = STDIO_OWNER
    spawn_chat_turn: Callable[[str, list[str], str], None] | None = None

    def push(self, method_name: str, params: dict) -> bool:
        """Send one notification to THIS caller. False when there is no channel.

        Not exception-swallowing on purpose. A push raised from inside a
        handler is still inside :func:`handle_request`'s boundary, so it
        becomes a typed ``-32000`` on the very call that tried it — which is
        the one moment a dead channel is reportable at all. Fan-out to OTHER
        subscribers is a different path with a different answer (drop, account,
        close), and it must not borrow this one.
        """

        if self.emit is None:
            return False
        self.emit(notification(method_name, params))
        return True


def method(name: str, tier: str):
    """Register a handler AND declare the tier a caller must hold to run it.

    ``tier`` is REQUIRED and has no default. A default is what turns a
    registration into a hole — either it defaults open, and a new verb ships
    unguarded the day someone forgets, or it defaults closed and a forgotten
    read verb breaks a client that was working. Requiring the word makes
    "which tier is this?" a question the author answers at the moment they know
    the answer, and makes a tierless method unrepresentable rather than merely
    untested.

    An unknown tier raises HERE, at import, rather than at the first call: the
    registry is built when the module loads, so a typo is a boot failure with a
    name in it instead of a verb that mysteriously refuses in the field.
    """

    if tier not in TIERS:
        raise ValueError(
            f"unknown tier {tier!r} for method {name!r}; expected one of {TIERS}"
        )

    def dec(fn):
        _METHODS[name] = fn
        _METHOD_TIERS[name] = tier
        return fn

    return dec


def method_names() -> list[str]:
    return sorted(_METHODS)


def method_tier(name: str) -> str:
    """The declared tier, or ``console`` for a name that has none.

    Unreachable through :func:`method`, which requires the word. It is the
    fallback for a registry mutated by some other path (a test monkeypatching
    ``_METHODS``, a future dynamic registration), and it fails CLOSED because the
    only safe answer to "nobody declared what this needs" is the strongest tier.
    """

    return _METHOD_TIERS.get(name, TIER_CONSOLE)


def method_tiers() -> dict[str, str]:
    """The whole mapping, sorted, for the manifest and for tests."""

    return {name: method_tier(name) for name in method_names()}


def manifest() -> dict[str, Any]:
    """What this runtime's method lane offers, for the greeting frames.

    Rides ``ready`` / ``hello_ok`` / the ``version`` reply. A client reads it
    once and knows both which methods exist and whether it understands their
    shape; a runtime that predates the lane carries no ``rpc`` block at all,
    which reads as "argv only" rather than as an error.

    ``tiers`` (Stage A1) says WHAT CREDENTIAL each method wants, so a connector
    can know before it tries. Additive by the set-plus-integer rule this
    module's header states and the D12 rollout already proved: it adds a key
    beside ``methods`` and changes no existing method's request or result shape,
    so ``RPC_CONTRACT_VERSION`` does not move. A client that ignores it is a
    client that keeps working — which is the point of shipping the declaration
    one stage ahead of the enforcement.

    It is deliberately a MAP and not a per-method sub-object. A tier is one
    string, and ``{"runtime.office.get": "read"}`` is the shape a client can
    index; wrapping each value in ``{"tier": …}`` would buy room for fields that
    do not exist and would make the addition of one look like a shape change.
    """

    return {
        "contract": RPC_CONTRACT_VERSION,
        "methods": method_names(),
        "tiers": method_tiers(),
    }


def is_rpc_frame(message: Any) -> bool:
    """Does this frame belong to the METHOD lane rather than the argv lane?

    Deliberately generous on the way IN and strict once inside: a frame naming
    ``jsonrpc`` or ``method`` is claimed here even when malformed, so the
    caller gets a typed JSON-RPC error instead of the argv lane's
    ``invalid_request`` complaining about a missing ``argv``. The argv lane has
    never sent either key, so nothing is taken from it.
    """

    return isinstance(message, dict) and ("jsonrpc" in message or "method" in message)


def _normalize_request(req: Any) -> tuple[Any, str, dict] | dict:
    """Validate a JSON-RPC request enough for safe local dispatch.

    Copied from ``tui_gateway/server.py:1672`` with one addition: the
    ``jsonrpc`` member is CHECKED. Upstream can skip it because its transport
    carries nothing else; here the same line could have been an argv request,
    so the version member is what makes the routing decision auditable rather
    than assumed.
    """

    if not isinstance(req, dict):
        return err(None, ERR_INVALID_REQUEST, "invalid request: expected an object")

    rid = req.get("id")
    version = req.get("jsonrpc")
    if version != JSONRPC_VERSION:
        return err(
            rid,
            ERR_INVALID_REQUEST,
            f'invalid request: jsonrpc must be "{JSONRPC_VERSION}"',
            {"reason": "bad_jsonrpc_version", "jsonrpc": version},
        )

    name = req.get("method")
    if not isinstance(name, str) or not name:
        return err(
            rid,
            ERR_INVALID_REQUEST,
            "invalid request: method must be a non-empty string",
            {"reason": "bad_method"},
        )

    params = req.get("params", {})
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: expected an object",
            {"reason": "params_not_an_object"},
        )

    return rid, name, params


def handle_request(req: Any, context: RpcContext | None = None) -> dict:
    """Answer one JSON-RPC request. Always returns a frame — never raises.

    A handler that raises becomes ``-32000`` rather than escaping into the
    serve reader loop: this lane shares a thread with the transport dispatcher,
    and a read method is not permitted to take a durable service down.

    ``context`` is optional and defaults to an EMPTY one rather than to
    ``None``, so a handler can always ask ``context.emit is None`` instead of
    guarding the argument itself. A caller that omits it (every test, and the
    argv lane's own probes) gets a caller with no push channel, which is the
    truth about it.

    **The authorization gate is here, and it is here rather than in
    :func:`method`'s wrapper on purpose** (Stage A3). This is the single point
    every frame on both transports passes through, so a method cannot be
    registered around it: the tier declaration rides the decorator, the decision
    runs here, and there is no per-method opt-out to forget. It also leaves the
    handlers callable directly — which every unit test in this repo does — so
    landing the gate did not rewrite the suites that exercise the functions
    below.

    Ordering matters and is deliberate: an UNKNOWN method is answered
    ``-32601`` before the gate runs. A refusal that told a caller which names
    exist would be leaking the surface to someone who may not use it; a
    ``method_not_found`` tells them nothing they could not learn from a manifest
    they were already sent. And a caller refused for scope must not be able to
    probe the registry by watching which name changes the error code — which it
    cannot, because the gate's refusal names only the tier it wanted.
    """

    context = context or RpcContext()
    normalized = _normalize_request(req)
    if isinstance(normalized, dict):
        return normalized

    rid, name, params = normalized
    fn = _METHODS.get(name)
    if fn is None:
        return err(
            rid,
            ERR_METHOD_NOT_FOUND,
            f"unknown method: {name}",
            {"reason": "unknown_method", "methods": method_names()},
        )
    # The NAME travels beside the tier because gateway Stage 6's caller kind is
    # answered by an allowlist rather than by a tier word (``PEER_METHOD_ALLOWLIST``).
    # Passed unconditionally rather than only for peers: a gate that received
    # the method sometimes would be a gate whose answer depended on which arm
    # the caller happened to reach, which is the shape this module exists to
    # retire.
    decision = authorize_call(method_tier(name), context.caller, method=name)
    if not decision.ok:
        # ``detail`` when the policy supplied one, the tier sentence otherwise.
        # The dispatcher still does not KNOW any policy — it renders whichever
        # sentence the decision carried — and the fallback is unchanged, so no
        # existing refusal's wording moves. WS4 is the one arm that sets it,
        # because "requires the console tier" is false for a caller that holds
        # console and is refused on its KIND.
        return err(
            rid,
            ERR_HANDLER_FAILED,
            decision.detail or f"{name} requires the {decision.tier} tier",
            decision.refusal_data(),
        )
    try:
        return fn(rid, params, context)
    except Exception as exc:  # noqa: BLE001 - the boundary is the point
        return err(
            rid,
            ERR_HANDLER_FAILED,
            f"handler error: {exc}",
            {"reason": "handler_failed", "method": name},
        )


# ── methods ──────────────────────────────────────────────────────────────────


def _workspace_id_param(params: dict) -> str | None:
    raw = params.get("workspace_id")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


#: The one ``data.reason`` every write verb spends for a malformed gesture token,
#: so a client decoder branches on a single stable string across all four.
CORRELATION_ID_INVALID_REASON = "correlation_id_invalid"


class _CorrelationIdRefused(Exception):
    """A gesture token that failed boundary validation, carrying its message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _correlation_id_param(params: dict) -> str | None:
    """The gesture's correlation token off ``params``, or ``None`` when absent.

    THE boundary for EG-2.3's id (Plan D §V1/§V2). Absent is the normal case and
    means "no gesture behind this write" — every downstream payload then stays
    byte-identical to what it was before this key existed, which is what makes the
    whole change additive.

    A PRESENT-but-illegal token is REFUSED rather than dropped, and that asymmetry
    against :func:`state_patches.normalize_correlation_id`'s silent drop is
    deliberate: this lane has a reply channel, so a client that sent free text
    where a generated token belongs gets told so instead of quietly diagnosing
    with an id the server discarded. Nothing is sanitized — a repaired id would
    print a value neither side used, which is worse than no id at all.

    Raises :class:`_CorrelationIdRefused`; every caller translates it to the same
    ``-32602`` / :data:`CORRELATION_ID_INVALID_REASON` pair.
    """

    from agent_runtime.state_patches import (
        CORRELATION_ID_MAX_LEN,
        normalize_correlation_id,
    )

    raw = params.get("correlation_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _CorrelationIdRefused(
            "invalid params: correlation_id must be a string or omitted"
        )
    token = normalize_correlation_id(raw)
    if token is None:
        raise _CorrelationIdRefused(
            "invalid params: correlation_id must be a generated token of at most "
            f"{CORRELATION_ID_MAX_LEN} characters from [A-Za-z0-9_.:-]"
        )
    return token


def log_office_write(
    *, op: str, correlation_id: str | None, **fields: Any
) -> None:
    """ONE line per office WRITE, in the serve child's own log (EG-2.3 / CI-2).

    The hermes half of the two-log join. Before this line existed the serve
    child's log named no office write at all — plan §8 item 5 measured 12 MB with
    zero ``office`` lines — so "which RPC produced this launcher update" could
    only be answered by anchoring on the launcher's flush receipt, and that
    anchoring is exactly what produced the confidently wrong "deletes take 3.8 s"
    diagnosis (Plan D's opening).

    Shaped like ``stream.log_stream_attach`` on purpose — ``key=value`` after a
    leading event word, ``-`` for an absent value — because an operator greps the
    two together and a second format would make the join a parse instead of a
    grep. ``corr=-`` rather than an omitted key: a write with no gesture behind
    it is a FACT worth reading, and an omitted key is indistinguishable from an
    old build.

    Never raises: an instrument must not be the reason a write fails.
    """

    try:
        extras = " ".join(
            f"{key}={'-' if value is None else value}" for key, value in fields.items()
        )
        logger.info(
            "office_write op=%s corr=%s%s",
            op,
            correlation_id or "-",
            f" {extras}" if extras else "",
        )
    except Exception:  # pragma: no cover - observability must never fail a lane
        pass


@method("runtime.office.get", tier=TIER_READ)
def _runtime_office_get(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE workspace's office projection — canvas-shaped, and bounded.

    Carries per item: ``item_id``, ``kind``, ``persona_id`` (the character-class
    POINTER), ``persona_instance_id`` (the owning actor's IDENTITY binding, or
    ``null`` when the actor is class-keyed), ``revision`` (the owning actor's,
    and the token ``runtime.office.upsert`` guards on), ``folder``, ``position``,
    ``scale``, ``display_name``, ``pet_slug``; plus surface-level ``folders``,
    ``revision``, ``updated_at``.

    ``persona_instance_id`` is the actor's, not the item's — an actor file is
    the binding unit (all of one agent's placements plus its coupled desk live
    in it), so every item flattened out of one actor carries the same value.
    It is NOT derivable client-side: an ``item_id`` such as
    ``personainst_qa_agent_9c8a382f`` is instance-SHAPED but is an item id, and
    reading it as a binding would invent an instance for a class-keyed actor.
    When this field was added every live actor was class-keyed, so it was
    ``null`` across the board and existed precisely so that stopped being
    invisible. The re-key migration has since landed: all eight live actors
    across both workspaces are instance-keyed and carry a populated value. The
    field is now the normal case, not the empty warning it was written as.

    Deliberately NOT carried, though the store holds them: the surface's
    ``archived_actor_keys`` (an append-only ledger capped at 5000 — the one
    genuinely unbounded field in this domain), and the actors' ``updated_by`` /
    ``created_at`` / ``state`` / ``backing_profile``. None of it is renderable
    and the first three are provenance for a different surface. Archived actors
    are excluded outright: ``scan_actors`` without ``include_archived`` is the
    placement set the canvas draws.

    Items are flattened out of their actor files in ``(actor_key, file order)``
    — ``scan_actors`` sorts by ``actor_key`` — so the same store state produces
    the same bytes on every call, which is what makes a caching client able to
    compare them.
    """

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )
    projection = _office_projection(workspace_id)
    if projection is None:
        # NOT an empty projection. `office show` answers an unauthored office
        # with an honest empty because a CLI reader is a human who can see the
        # zero; a program told `folders: [], items: []` cannot tell "this
        # workspace has no office yet" from "you asked for a workspace that
        # does not exist", and would render a blank canvas for a typo.
        return err(
            rid,
            ERR_NOT_FOUND,
            f"unknown workspace: {workspace_id}",
            {"reason": "workspace_not_found", "workspace_id": workspace_id},
        )
    return ok(rid, projection)


def _office_projection(workspace_id: str) -> dict | None:
    """The canvas projection for one workspace, or None when it does not exist.

    Extracted so ``runtime.office.subscribe``'s BASELINE is not a second
    derivation of the same thing. A subscribe whose baseline could disagree
    with a ``get`` would put the client back in the state this whole lane
    exists to end — two readers of one truth, differing silently.
    """

    from agent_runtime import paths  # noqa: F401 - store_root side of the read
    from agent_runtime.office_models import office_item_wire_row
    from agent_runtime.office_store import OfficeStore
    from agent_runtime.serde import to_jsonable
    from agent_runtime.snapshot import MAX_OFFICE_ACTORS_PROJECTED

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
        return None

    surface = store.get_surface(workspace_id)
    # The cut below is measured against the scan's OWN length, and
    # ``scan.unreadable`` rides the projection beside it: a list that had already
    # dropped its unreadable files made that subtraction answer 0 — a projection
    # shortened by the platform, describing itself as complete.
    scan = store.scan_actors(workspace_id)
    actors = scan.actors
    projected = actors[:MAX_OFFICE_ACTORS_PROJECTED]

    # The item shape lives in ``office_models`` and NOT here (plan S2). It is the
    # shape ``runtime.agent.create``'s ack now returns inside ``result.actor``,
    # and ``agent_create`` cannot import this module — that dependency is
    # inverted on purpose. Two copies of these ten keys is the
    # silent-disagreement shape ``_office_projection`` itself was extracted to
    # end, one level down.
    items = [
        office_item_wire_row(actor, item)
        for actor in projected
        for item in actor.items
    ]

    return {
        "workspace_id": surface.workspace_id,
        "folders": list(surface.folders),
        "revision": surface.revision,
        "updated_at": to_jsonable(surface.updated_at),
        "items": items,
        # Accounted, never silent. Zero on every real workspace today; a cut
        # that read as a smaller office would be indistinguishable from actors
        # having been removed.
        "actors_truncated": max(0, len(actors) - len(projected)),
        # The OTHER way this projection can be short, and the one it used to
        # hide completely: files that exist and would not decode. Its sibling
        # above counts a cut WE chose; this one counts rows the platform took.
        # Additive — an old launcher ignores the key — and it rides the shared
        # ``_office_projection``, so the subscribe baseline and ``get`` cannot
        # disagree about how complete the office they just handed over was.
        "actors_unreadable": scan.unreadable,
    }


@method("runtime.office.subscribe", tier=TIER_READ)
def _runtime_office_subscribe(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """The baseline AND the registration, in one call. The push leg's keystone.

    Params: ``workspace_id`` (required), ``fold_entities`` (optional list of
    strings — what THIS client can fold), ``reason`` (optional string — WHY this
    client is subscribing).

    Result: the SAME body ``runtime.office.get`` returns, plus ``watermark``
    ``{"event_offset": N}`` — the event-log offset the baseline was read at —
    ``fold_entities``, the accepted declaration echoed back, and ``replaced``,
    the re-baselining receipt described below. Subsequent
    ``runtime.office.patch`` notifications carry ``base_offset`` /
    ``watermark`` from the same counter, so the client's existing ``>``-only
    sequence gate applies unchanged and a gap is a gap on either lane.

    Why one call and not two
    ------------------------
    A ``get`` followed by a separate join is two reads of one truth with a
    window between them, and nothing tells the client whether anything moved
    inside that window. Taking the projection and the offset together — and
    registering with the same value — is what makes "I have the office as of N,
    push me everything after N" a statement the runtime can honour rather than
    a hope. The office lock is held across the pair so a write cannot land
    between them.

    The ordering seam, stated rather than papered over
    --------------------------------------------------
    The dispatcher emits this reply AFTER the handler returns, so a patch
    published in between reaches the client BEFORE the baseline it rebases on.
    That is not fixed by a server-side buffer; it is fixed by the sink dropping
    any frame at or below ``event_offset``, which is the same rule that absorbs
    the hub's mandatory re-hydrate. See ``serve_office_subscriptions``.

    A second subscribe RE-BASELINES, and says so
    --------------------------------------------
    Registering and answering together has a consequence the first cut did not
    follow through on: the subscription exists before the client has finished
    reading the reply. A client that finds the baseline unusable is right to
    refuse it — folding a knowingly-partial office would render it as
    authoritative — but the old ``already_subscribed`` refusal then left a live
    subscription the client would never fold against, reclaimable only by
    dropping the connection. There is no method that could have released it.

    So a repeat subscribe for this ``(connection, workspace)`` replaces the
    registration with a fresh baseline and watermark, and the stuck state stops
    existing by construction. The refusal it retires was only ever there to stop
    a subscriber leaking per retry; one key still means one subscription, so
    nothing leaks either way, and replacement is the answer that gets a confused
    client out of the hole rather than deeper into it.

    ``replaced`` on the result is the bill. ``StreamHub.subscribe`` restarts the
    producer, so a re-baseline costs every OTHER subscriber on that hub a fresh
    full core — a cost the old refusal did not incur, because a duplicate key
    was declined before a generation was ever bumped. A client that sees
    ``replaced: true`` on a call it thought was its first has learned something
    true about its own state, and the same event is written to the service log
    for the operator (``serve_office_subscription_rebaselined``). Silent
    re-baselining would let a retry loop tax the whole room invisibly.

    Refusals are typed because their cures differ:

    ``push_channel_unavailable``
        This caller has no push channel — a stdio probe, a test double. The
        method refuses rather than registering into a void, which is the whole
        reason ``RpcContext.emit`` is allowed to be ``None``.
    ``push_lane_unavailable``
        The runtime has no stream hub bound — no socket lane, or a serve loop
        that has not reached its bind yet. Nothing the client can do about the
        first; the second is a startup window ``serve.py`` announces ``ready``
        several hundred lines before closing, and it is a separate bug.
    ``push_lane_draining``
        A hub IS bound and it refused: it is stopping, so this call raced
        ``_close_socket_lane``. Transient, and the cure is to reconnect — which
        is precisely why it must not share a name with the case above. When the
        caller held a subscription, ``data.prior_subscription_released`` says
        so: the re-baseline's teardown already ran, so the old lane is gone too.
    ``baseline_unavailable``
        The event log's tail could not be read, so there is no offset to
        baseline at. Transient like the case above, and the reason this method
        no longer answers an unreadable log with ``0`` — see the refusal at the
        watermark read for what a fabricated baseline costs the whole room.

    ``already_subscribed`` is GONE. It was the only ``ERR_CONFLICT`` this method
    raised, and its disappearance also retires a mislabel that shipped with it:
    the old branch chose its reason by asking ``bound()``, which answers True
    for a bound-but-draining hub as readily as for a live one. A client racing
    the drain was therefore told "already subscribed to this workspace" while
    holding no subscription at all — sent to the one cure (stop retrying) that
    could not work. Splitting the two reasons is what keeps replacement from
    quietly inheriting that lie under a new name.

    ``fold_entities``: what THIS client can fold, not what an office subscriber
    can fold in general
    ---------------------------------------------------------------------------
    The declaration used to be a SERVER-side constant
    (``OFFICE_FOLD_ENTITIES``), which is a shape that can only ever report a
    fact about the runtime — and that is the hole the 2026-08-16 capability
    token exposed (plan §V4). Promotion is negotiated over the room, so a
    launcher whose fold had been widened could never have its widened rows
    promoted on this lane: the intersection cannot contain a token nobody told
    the server about.

    So the param is optional and FAIL-OPEN. Absent → the legacy constant, i.e.
    today's wire for every client in the field, byte-identical. Present → this
    subscription declares exactly what it says, unknown members included (the
    channel has never interpreted its strings, and a server that filtered to a
    known vocabulary would drop the NEXT token the same way). An explicitly
    EMPTY list is honoured as empty — "I fold nothing, send me full cores" is a
    thing a client is allowed to say and must stay distinguishable from silence.
    A non-list is refused rather than guessed: a client sending the wrong shape
    should learn it, not be quietly filed as legacy.

    The accepted set is ECHOED on the reply, under the same always-present rule
    the other keys follow. Without it a client cannot tell a declaration that
    was honoured from one the runtime is too old to have read — and this whole
    method exists because a push that arrives and is silently dropped is the
    failure this lane keeps paying for.

    ``reason``: the client's own resubscribe cause, so the server log can join
    the ladder
    ---------------------------------------------------------------------------
    Every re-subscribe in the launcher flows through ONE door and already
    carries an exact cause string (``start``, ``fold:fenced``,
    ``push:full_core``, ``reconnect``, ``deferred:*``, ``fold_threw``) — and
    that string used to die in the launcher's log. The server saw a re-baseline
    with no way to tell a fold-fence storm from a demote storm, so separating
    the two classes meant joining two logs on timestamps: inference, on the same
    shape that has already misattributed this lane once.

    So the param is optional, additive and INERT. It decides nothing: it is
    stamped verbatim on the ``serve_office_subscription_rebaselined`` receipt
    and read nowhere else. A cause the client chose is evidence, never
    authority — a server that branched on it would be taking dispatch orders
    from an untrusted string.

    Boundary-validated rather than echoed raw, because it is written to an
    operator's log: ≤64 chars over ``[a-z0-9_:.-]``, refused ``-32602`` with
    ``{"reason": "reason_invalid"}`` otherwise, and refused BEFORE any store or
    hub call so a bad param cannot cost a projection, a lock, or a producer
    restart. See ``normalize_office_subscribe_reason`` for why a blank is a
    refusal rather than an absence.

    Absent — every client in the field today — prints
    ``SUBSCRIBE_REASON_ABSENT`` (``-``) on the receipt, so silence is visible as
    a value rather than as a missing key.
    """

    from agent_runtime.locks import office_lock
    from agent_runtime.parity import events_watermark
    from agent_runtime.serve_office_subscriptions import (
        BASELINE_UNAVAILABLE,
        NO_PUSH_LANE,
        OFFICE_FOLD_ENTITIES,
        OFFICE_SUBSCRIPTIONS,
        PUSH_LANE_DRAINING,
        event_offset_of,
        normalize_office_fold_entities,
        normalize_office_subscribe_reason,
    )

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )
    raw_fold_entities = params.get("fold_entities")
    fold_entities = (
        None if raw_fold_entities is None else normalize_office_fold_entities(raw_fold_entities)
    )
    if raw_fold_entities is not None and fold_entities is None:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: fold_entities must be a list of strings",
            {"reason": "fold_entities_invalid", "workspace_id": workspace_id},
        )
    # BEFORE the office lock, the projection and the hub — a param this method
    # only ever writes to a log must not be able to cost a producer restart on
    # its way to being refused.
    raw_reason = params.get("reason")
    subscribe_reason = (
        None if raw_reason is None else normalize_office_subscribe_reason(raw_reason)
    )
    if raw_reason is not None and subscribe_reason is None:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: reason must be <=64 chars of [a-z0-9_:.-]",
            {"reason": "reason_invalid", "workspace_id": workspace_id},
        )
    context = context or RpcContext()
    if context.emit is None:
        return err(
            rid,
            ERR_INVALID_REQUEST,
            "this transport cannot carry pushes; use runtime.office.get",
            {"reason": "push_channel_unavailable", "transport": context.transport},
        )

    with office_lock(workspace_id):
        projection = _office_projection(workspace_id)
        if projection is None:
            return err(
                rid,
                ERR_NOT_FOUND,
                f"unknown workspace: {workspace_id}",
                {"reason": "workspace_not_found", "workspace_id": workspace_id},
            )
        # ONE reader of the watermark, asked ONE question, through the same
        # helper the sink's baseline gate uses. ``int(… or 0)`` used to sit here
        # and answered a DIFFERENT question: it folded an unreadable log —
        # ``{"event_offset": None, "event_offset_error": ...}``, which
        # ``events_watermark`` documents as a routine outcome on this platform
        # under AV scanning — into offset 0, with no exception for the typed
        # ``except`` to catch and the error string discarded. A subscription
        # baselined at 0 has no gate at all, so the hub's mandatory hydrate came
        # back as a resync and the client re-subscribed, restarting the producer
        # for the whole room, forever.
        #
        # Cannot-read is now its own answer. No registration happens on this
        # path: the reply is a transient refusal beside the other two, and the
        # client's existing degrade ladder already holds this shape without
        # spinning.
        watermark = events_watermark()
        baseline_offset = event_offset_of(watermark)
        if baseline_offset is None:
            # The discarded half, kept: the reply cannot carry a platform error
            # string (a client has no use for one and it is not part of the
            # vocabulary), but an operator watching subscribes fail needs to
            # know WHY the log could not be read. Class only — the same
            # disclosure rule the rest of this runtime's receipts follow — and
            # the ``-`` sentinel rather than an absent key, because a field that
            # appears only sometimes is one a log reader stops looking for.
            raw_error = watermark.get("event_offset_error")
            error_class = (
                str(raw_error).split(":", 1)[0].strip() or "-"
                if isinstance(raw_error, str) and raw_error.strip()
                else "-"
            )
            log = OFFICE_SUBSCRIPTIONS.service_log()
            if log is not None:
                log(
                    {
                        "event": "serve_office_subscribe_refused",
                        "reason": BASELINE_UNAVAILABLE,
                        "workspace_id": workspace_id,
                        "error": error_class,
                    }
                )
            return err(
                rid,
                ERR_INVALID_REQUEST,
                "this runtime cannot read its event log's tail; subscribe again",
                {
                    "reason": BASELINE_UNAVAILABLE,
                    "workspace_id": workspace_id,
                    # Shape parity with the registry's own refusals below: this
                    # lane's clients branch on ``data.reason`` and decode the
                    # rest, so a key that exists on two of three transient
                    # refusals would have to be special-cased. Always False
                    # here, and honestly so — the registry was never called, so
                    # no prior subscription was displaced.
                    "prior_subscription_released": False,
                },
            )
        outcome = OFFICE_SUBSCRIPTIONS.subscribe(
            connection_key=context.connection_key,
            workspace_id=workspace_id,
            baseline_offset=baseline_offset,
            emit=context.emit,
            fold_entities=fold_entities,
            reason=subscribe_reason,
        )

    if not outcome.registered:
        # The reason comes from the REGISTRY, not from a second guess here. The
        # old branch re-derived it by asking ``bound()``, which cannot separate
        # "no hub" from "a bound hub that is draining" — see the docstring.
        # Both share ``-32600`` on purpose: this lane's clients branch on
        # ``data.reason``, which is the whole reason ``data`` is populated at
        # all, and minting a code per transient state would make the numbers
        # the contract instead of the names.
        return err(
            rid,
            ERR_INVALID_REQUEST,
            "this runtime's push lane is draining; reconnect and subscribe again"
            if outcome.reason == PUSH_LANE_DRAINING
            else "this runtime has no push lane",
            {
                "reason": outcome.reason or NO_PUSH_LANE,
                "workspace_id": workspace_id,
                # Honest even on the failure path: a re-baseline that raced the
                # drain destroyed the caller's previous subscription before it
                # learned the new one would not be granted. Silence here would
                # leave the client believing its old lane survived.
                "prior_subscription_released": bool(outcome.replaced),
            },
        )

    return ok(
        rid,
        {
            **projection,
            "watermark": {"event_offset": baseline_offset},
            # The declaration this subscription was actually registered with —
            # sorted so the answer is a value the client can compare, not an
            # iteration order. A client that declared nothing sees the legacy
            # constant here, which is how it learns what it is being held to
            # without having to know the server's defaults.
            "fold_entities": sorted(
                fold_entities if fold_entities is not None else OFFICE_FOLD_ENTITIES
            ),
            # Always present, never omitted on the False case — the same rule
            # the projection's own nullable keys follow. A client decoding into
            # a typed struct must not have to special-case which keys exist,
            # and a receipt that appears only sometimes is one a client learns
            # to stop reading.
            "replaced": bool(outcome.replaced),
        },
    )


@method("runtime.office.unsubscribe", tier=TIER_READ)
def _runtime_office_unsubscribe(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Hand ONE workspace's push subscription back, keeping the connection.

    Params: ``workspace_id`` (required).
    Result: ``{"workspace_id": ..., "released": bool}``.

    Why the method exists at all
    ----------------------------
    ``runtime.office.subscribe`` registers and answers together, so a
    subscription outlives the client's decision about the reply that created it.
    Re-baselining covers the client that wants a BETTER baseline; this covers
    the one that wants none — it navigated away from the workspace, or it gave
    up. Before this, the only release was closing the socket, which also took
    every unrelated call riding it. A subscriber the runtime could not reclaim
    keeps a producer rebuilding projections for nobody.

    Why an unknown subscription is an ANSWER and not an error
    ---------------------------------------------------------
    ``released: false`` is the honest report that nothing was live under this
    key, and it is deliberately a result rather than a 4001. Releasing something
    already released is exactly what a recovering client does: it lost track of
    whether its subscribe landed, and asking is how it finds out. An error there
    would make ordinary recovery indistinguishable from a fault, and a client
    that cannot tell those apart either logs noise forever or stops looking.

    Deliberately NOT here
    ---------------------
    No ``office_lock``, and no existence check on the workspace. There is no
    baseline to pair a read with, so the lock would buy nothing and could only
    make a release wait behind a write. And a client must still be able to
    release a workspace that has since been deleted — refusing there would
    strand the subscription for good, which is the very failure being closed.

    No ``push_channel_unavailable`` either. Subscribe needs an emitter because
    it registers one; this only names a key, and a caller with no push channel
    has no subscription to release — which ``released: false`` already says.
    """

    from agent_runtime.serve_office_subscriptions import OFFICE_SUBSCRIPTIONS

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        # A caller BUG, unlike everything else this method tolerates: there is
        # no key to name, so there is nothing to answer False about.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )
    context = context or RpcContext()

    released = OFFICE_SUBSCRIPTIONS.release_one(
        context.connection_key, workspace_id
    )
    return ok(rid, {"workspace_id": workspace_id, "released": bool(released)})


@method("runtime.office.upsert", tier=TIER_CONSOLE)
def _runtime_office_upsert(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE actor placement, written — and acked LIGHT so a drag can predict.

    Params: ``workspace_id`` (required), ``actor`` (required object — the same
    identity-triple-plus-items payload ``harness office actor-upsert`` takes on
    ``--actor-json``, deliberately not a second schema), ``expect_revision``
    (optional int) and ``updated_by`` (optional string, defaults to the argv
    lane's own ``operator``).

    Result: ``{"actor_key", "revision"}``, plus ``correlation_id`` echoed back
    when the caller sent one. See the module docstring for why the ack is those
    two facts and not the actor.

    ``correlation_id`` (optional string, EG-2.3) is the CALLER's gesture token:
    a generated ``[A-Za-z0-9_.:-]`` id of at most 64 characters, minted once per
    operator gesture and threaded into every event this write appends, so the
    launcher receipt and the serve-child ``office_write`` line join on one grep
    instead of on timestamps. Absent is normal and keeps every payload
    byte-identical; present-but-illegal is REFUSED
    (``data.reason: correlation_id_invalid``) rather than sanitized. It is NOT
    the idempotency key (a replay reuses that by design) and NOT ``issued_at``
    (an ordering basis); a retry of the same gesture carries the SAME token,
    which is the truth and is what makes a retry visible as two receipts under
    one id.

    Why this method REFUSES an unknown workspace instead of authoring one
    ---------------------------------------------------------------------
    ``OfficeStore.upsert_actor`` calls ``ensure_surface``, which used to lazily
    create the office for ANY id. This lane's caller is the same program that
    just called ``runtime.office.get``, which REFUSES an unknown workspace so a
    typo cannot render as a blank canvas. A pair where the read refuses a typo
    and the write silently authors a whole new office for it is incoherent, and
    the write side is the worse half — a mis-rendered canvas is repainted on the
    next poll, a mis-authored one is on disk forever. So the existence check
    happens HERE, before the store's own create.

    **Amended MC-8 / P10.** This paragraph used to continue "…which is right for
    the CLI: a human typed ``--workspace`` and can see what they made", leaving
    the surface-authoring path on the argv lane. That reasoning is retired: the
    lazy create is how a leaked test context minted a LIVE office for a workspace
    that never existed, so ``ensure_surface`` now refuses typed
    (``WorkspaceUnresolved``) on every lane when no workspace RECORD resolves the
    id. The two guards ask different questions and both still earn their place —
    this one asks whether a SURFACE exists, so an unknown workspace reads as
    ``workspace_not_found`` rather than as an empty office; the store's asks
    whether a workspace RECORD does. Because this check runs first and refuses
    whenever the surface is absent, ``WorkspaceUnresolved`` is NOT REACHABLE
    through this arm. A handler for it here would be a catch that can never fire,
    so there deliberately is none.

    The desk fence, and why this lane has no override for it either
    ---------------------------------------------------------------
    ``OfficeStore._guard_duplicate_desk`` (D6) refuses a payload that would
    leave one persona holding a second LIVE desk. Same division of labour as the
    class-key fence: the predicate and the decision are the store's, and what
    this handler keeps is the TRANSLATION — a 4090 whose ``data.reason`` is
    ``duplicate_desk`` and whose ``data`` names the holding actor and item read
    off ``safe_details``, never recomputed here. Unlike the class-key fence it
    has no override on ANY lane, because the thing it refuses is not a shape an
    operator can mean: the render layer draws the implicit desk only while a
    persona has no authored one, so the second authored desk is a placement that
    can never be reached.

    Concurrency is the store's, not a second scheme
    -----------------------------------------------
    ``expect_revision`` and the realm-sync conflict guard are passed straight
    through to ``upsert_actor``; both refusals arrive as typed exceptions and
    are translated, never re-implemented. Note what ``expect_revision`` can and
    cannot express: ``_check_revision`` compares against ``None`` for an actor
    that does not exist yet, so EVERY value — including ``0`` — refuses a
    create. A create is therefore necessarily unguarded, and a client must send
    ``expect_revision`` only for a placement it has already seen.

    The class-key fence, and why it has NO override here
    ----------------------------------------------------
    The fence is the STORE's (``OfficeStore._guard_class_keyed_write``, hoisted
    there by EG-6.6) for the reason ``office_class_key_guard`` gives:
    ``upsert_actor`` reads an explicit upsert of an ARCHIVED key as operator
    intent to re-add and clears the resurrection ledger, so one surviving
    class-keyed write undoes the class→instance re-key and places the same agent
    twice. What this handler keeps is the TRANSLATION — a 4090 whose
    ``data.reason`` is ``class_key_collision`` and whose message names the exit
    this lane actually has. It holds no copy of the predicate; deleting the
    store's fence does not leave this lane guarded.

    The CLI verb beside it passes ``allow_class_key=True`` to the store on
    ``--allow-class-key``. This one never passes it, and that asymmetry is the
    point rather than an omission. The
    flag is consent: an operator read the refusal, typed the override, and owns
    the double placement. A wire PARAMETER is not consent — it is a constant in
    a client build, set once by whoever was debugging the day drags started
    failing, and thereafter sent by every install on every write with no human
    in any loop. And the wire client needs it least: the read projection hands
    it ``persona_instance_id`` on every item, so its remedy is to send back the
    binding it was already given. The genuine operator-intent paths are
    untouched — ``harness office actor-restore``, and the CLI's own override.
    """

    from agent_runtime.errors import (
        ActorArchived,
        ArchiveUnreadable,
        StaleRevision,
        SyncConflict,
    )
    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused
    from agent_runtime.office_store import DuplicateDeskRefused, OfficeStore

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        # The same reason string the read leg spends, on purpose: one client
        # branch covers "the launcher forgot the workspace" on either lane.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )

    actor_payload = params.get("actor")
    if not isinstance(actor_payload, dict):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: actor must be an object",
            {"reason": "actor_required"},
        )

    expect_revision = params.get("expect_revision")
    # ``bool`` is an ``int`` in Python and ``True`` would silently mean revision
    # 1 — a wrong guard is worse than no guard, so the type check is explicit.
    if expect_revision is not None and (
        isinstance(expect_revision, bool) or not isinstance(expect_revision, int)
    ):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: expect_revision must be an integer or omitted",
            {"reason": "expect_revision_invalid"},
        )

    updated_by = params.get("updated_by")
    if updated_by is not None and not isinstance(updated_by, str):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: updated_by must be a string or omitted",
            {"reason": "updated_by_invalid"},
        )

    try:
        correlation_id = _correlation_id_param(params)
    except _CorrelationIdRefused as refused:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            refused.message,
            {"reason": CORRELATION_ID_INVALID_REASON, "workspace_id": workspace_id},
        )

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
        return err(
            rid,
            ERR_NOT_FOUND,
            f"unknown workspace: {workspace_id}",
            {"reason": "workspace_not_found", "workspace_id": workspace_id},
        )

    try:
        actor = store.upsert_actor(
            workspace_id,
            actor_payload,
            updated_by=updated_by or "operator",
            expect_revision=expect_revision,
            correlation_id=correlation_id,
        )
    except ClassKeyedPlacementRefused as exc:
        # The store's fence, translated. Its own reason: the remedy is neither
        # "refetch and rebase" nor "fix the payload shape" — the payload is
        # well-formed and the client is not behind. It is "name WHICH instance
        # you are placing". The guard's two narrow reasons ride beside it as a
        # list rather than as the branch point, because they share that one
        # remedy and because a client decoder switches on a single stable string.
        #
        # ``str(exc)`` — the shared ``refusal_message`` — is deliberately NOT
        # reused: it ends by offering ``--allow-class-key``, which is a CLI flag
        # that does not exist on this lane. Advice a caller cannot follow is
        # worse than none. So the SENTENCE is this lane's and the FACTS are the
        # store's, read off ``safe_details`` (the collision dict verbatim) rather
        # than recomputed — recomputing them here is the second copy EG-6.6
        # removed.
        collision = exc.safe_details
        return err(
            rid,
            ERR_CONFLICT,
            (
                f"class-keyed write for persona {collision['persona_id']!r} refused: "
                f"{', '.join(collision['reasons'])}"
                # Named when there is one. The two reasons fire on different
                # evidence — a ledger entry vs. a live sibling — and only the
                # second has another actor to point at.
                + (
                    f" (conflicts with {', '.join(collision['conflicting_actor_keys'])})"
                    if collision["conflicting_actor_keys"]
                    else ""
                )
                + ". The class→instance re-key archived this class key; writing it "
                "back undoes that migration. Send persona_instance_id to place a "
                "specific instance."
            ),
            {
                "reason": "class_key_collision",
                "workspace_id": workspace_id,
                "persona_id": collision["persona_id"],
                "class_actor_key": collision["class_actor_key"],
                "reasons": collision["reasons"],
                "conflicting_actor_keys": collision["conflicting_actor_keys"],
            },
        )
    except DuplicateDeskRefused as exc:
        # The desk fence, translated. A 4090 because something is already placed
        # and the write lost to it — the same family as the class-key collision
        # and ``stale_revision``, and a different ``reason`` because the cure is
        # a third thing again: not "send the binding" and not "refetch and
        # rebase", but "there is already a desk for this persona; move that one
        # or remove it".
        #
        # ``str(exc)`` is deliberately NOT reused: it ends by naming `harness
        # office actor-remove`, a CLI verb this lane does not have. Advice a
        # caller cannot follow is worse than none — so the SENTENCE is this
        # lane's and the FACTS are the store's, read off ``safe_details``.
        # ``data`` names the holder because a refusal a client cannot act on is
        # a refusal that becomes a retry loop.
        collision = exc.safe_details
        return err(
            rid,
            ERR_CONFLICT,
            (
                f"desk write for persona {collision['persona_id']!r} refused: "
                f"{collision['holding_actor_key']!r} already holds desk "
                f"{collision['holding_item_id']!r}. A persona has one desk on a "
                "level; move that desk, or remove it with runtime.office.remove "
                "before placing another."
            ),
            {
                "reason": "duplicate_desk",
                "workspace_id": workspace_id,
                "persona_id": collision["persona_id"],
                "holding_actor_key": collision["holding_actor_key"],
                "holding_item_id": collision["holding_item_id"],
            },
        )
    except ActorArchived as exc:
        # The tombstone fence (D1), translated. A 4090 because a guard refused
        # this write, and a FOURTH reason on that code because the cure is a
        # fourth thing again: not "refetch and rebase", not "name the instance",
        # not "an operator must resolve a sidecar" — it is DROP THE LOCAL ROW.
        # This is the live-incident lane. The launcher that re-pushed archived
        # actors nineteen seconds after boot was not sending a malformed or
        # stale payload; it was sending a well-formed write for a row that no
        # longer exists on the authority, and every retryable reason would have
        # spun it. A client must not re-place this key: re-placing is a NEW
        # create with a freshly minted id.
        #
        # No ``resurrect`` parameter on this lane, for the reason spelled out
        # above about ``allow_class_key``: a wire parameter is not consent. The
        # deliberate re-add doors stay where they are — ``harness office
        # actor-restore`` and the CLI's own ``--resurrect``.
        details = exc.safe_details
        return err(
            rid,
            ERR_CONFLICT,
            (
                f"actor {details['actor_key']!r} was deleted on this server. "
                "Drop the local row; re-placing this agent is a new create with "
                "a new id, not a re-add of this key."
            ),
            {
                "reason": "actor_archived",
                "workspace_id": workspace_id,
                "actor_key": details["actor_key"],
                "persona_instance_id": details["persona_instance_id"],
            },
        )
    except StaleRevision as exc:
        # The prediction is behind. ``data`` deliberately does NOT carry the
        # current revision: the prescribed cure is to refetch and rebase onto
        # server truth, and handing back a bare integer invites a retry with it
        # — which is the lost update this guard exists to refuse. The number is
        # in ``message``, where an operator can read it and a client should not.
        return err(
            rid,
            ERR_CONFLICT,
            str(exc),
            {
                "reason": "stale_revision",
                "workspace_id": workspace_id,
                "expect_revision": expect_revision,
            },
        )
    except SyncConflict as exc:
        # A different refusal with a different cure — an unresolved realm-sync
        # sidecar. Retrying never clears it; an operator resolving it does.
        return err(
            rid,
            ERR_CONFLICT,
            str(exc),
            {"reason": "sync_conflict", "workspace_id": workspace_id},
        )
    except ArchiveUnreadable as exc:
        # The archived copy of this key exists and would not decode, so the
        # revision this write must bump is unknown. NOT a 4090: no guard refused
        # anything and there is no prediction to rebase — the store declined to
        # invent a base. ``-32600`` is the band this runtime already spends on
        # "cannot serve this right now" with ``data.reason`` as the branch
        # (``baseline_unavailable`` on the subscribe lane), and the cure is the
        # same shape: ask again once the file is readable, or have an operator
        # repair/remove the archive copy. Retrying is safe and may well work —
        # an AV hold is transient — but the client must not paper over it by
        # writing UNGUARDED, which is what a revision of 1 would have invited.
        #
        # ``exc.code``, not the class constant: ``ActorsUnreadable`` subclasses
        # this so it inherits the band and the cure SHAPE (EG-6.6 — the class-key
        # fence refusing rather than answering "no conflict" from a directory it
        # could only partly read), and its own code names the different FILE the
        # operator has to repair. A hard-coded constant here would have told them
        # to go fix the archive copy.
        return err(
            rid,
            ERR_INVALID_REQUEST,
            str(exc),
            {
                "reason": exc.code,
                "workspace_id": workspace_id,
            },
        )
    except ValueError as exc:
        # Every ``invalid_request: …`` the store raises while normalizing the
        # payload — a missing persona_id, an unparseable position, a
        # secret-shaped display name. One reason, because the client's response
        # to all of them is identical: fix the payload, it is a launcher bug.
        # The store's own sentence rides ``message`` so the dev knows which.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            str(exc),
            {"reason": "actor_invalid", "workspace_id": workspace_id},
        )

    log_office_write(
        op="runtime.office.upsert",
        correlation_id=correlation_id,
        workspace=workspace_id,
        actor_key=actor.actor_key,
        revision=actor.revision,
    )
    # Light, and both fields are things the caller could not have computed.
    # ``correlation_id`` rides only when the caller sent one — the ECHO is what
    # lets the launcher's reply receipt name the token without trusting its own
    # memory of what it sent, and its absence keeps every pre-EG-2.3 reply
    # byte-identical.
    result: dict[str, Any] = {"actor_key": actor.actor_key, "revision": actor.revision}
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return ok(rid, result)


@method("runtime.office.remove", tier=TIER_CONSOLE)
def _runtime_office_remove(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE actor placement, ARCHIVED — the delete gesture's write leg.

    Params: ``workspace_id`` (required), ``actor_key`` (required string),
    ``expect_revision`` (optional int), ``updated_by`` and ``reason`` (optional
    strings, both defaulting to the argv lane's own ``operator``), and
    ``correlation_id`` (optional gesture token — see ``runtime.office.upsert``).

    Result: ``{"actor_key", "revision", "state"}`` — the store's POST-archive
    revision, not the one the caller was holding. ``_archive_actor_locked``
    bumps the number on its way out and an archived key carries it forward
    through a restore, so the +1 is the token a later guarded write on this key
    must present. Returning the pre-archive number would hand the client a
    guard token that is already one behind.

    ``state`` is on the ack even though it is a constant. The alternative reads
    ``{actor_key, revision}`` exactly like the upsert's ack and means the
    opposite thing, and a client decoder that crossed the two would settle a
    deletion with a placement's ack. One word makes the two frames
    self-describing.

    Why this REFUSES an unknown workspace before the store
    ------------------------------------------------------
    Same ruling as ``runtime.office.upsert``'s, reached from the other side.
    ``remove_actor`` on an unauthored workspace raises ``NotFound`` about the
    ACTOR — which is true but useless, because it names the wrong thing: the
    client's cure for "this workspace has no office" is not "resend a different
    key". Checking ``surface_exists`` first means the read leg and both write
    legs spend one reason string on one condition, and a typo answers the same
    way whichever verb hit it.

    Why an already-archived key is an OK and not a 4001
    ---------------------------------------------------
    ``OfficeStore.remove_actor`` is idempotent on purpose: an already-archived
    key returns the archived copy and writes nothing. That is the honest answer
    for this lane too. The launcher's flush re-names a key it has already
    deleted whenever a later save recomputes the same vacated set, and turning
    the second attempt into an error would make an operator's single deletion
    report a failure it did not have — with a rollback behind it that puts the
    actor back on the canvas. Nothing was written either way; the state the
    caller asked for is the state the store is in.

    NO class-key fence, and that is not an omission
    -----------------------------------------------
    ``office_class_key_guard`` exists because an upsert of an archived key is
    read as intent to RE-ADD and clears the resurrection ledger. An archive
    moves in the other direction — it cannot resurrect anything, and a
    class-keyed archive is the class→instance migration's own mechanism rather
    than a write that undoes it.
    """

    from agent_runtime.errors import ArchiveUnreadable, NotFound, StaleRevision
    from agent_runtime.office_store import OfficeStore

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        # The same reason string every office method spends on this, on
        # purpose: one client branch covers it whatever the verb was.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )

    raw_key = params.get("actor_key")
    actor_key = raw_key.strip() if isinstance(raw_key, str) else ""
    if not actor_key:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: actor_key must be a non-empty string",
            {"reason": "actor_key_required"},
        )

    expect_revision = params.get("expect_revision")
    # ``bool`` is an ``int`` in Python and ``True`` would silently mean revision
    # 1 — a wrong guard is worse than no guard, so the type check is explicit.
    if expect_revision is not None and (
        isinstance(expect_revision, bool) or not isinstance(expect_revision, int)
    ):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: expect_revision must be an integer or omitted",
            {"reason": "expect_revision_invalid"},
        )

    updated_by = params.get("updated_by")
    if updated_by is not None and not isinstance(updated_by, str):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: updated_by must be a string or omitted",
            {"reason": "updated_by_invalid"},
        )

    reason = params.get("reason")
    if reason is not None and not isinstance(reason, str):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: reason must be a string or omitted",
            {"reason": "reason_invalid"},
        )

    try:
        correlation_id = _correlation_id_param(params)
    except _CorrelationIdRefused as refused:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            refused.message,
            {"reason": CORRELATION_ID_INVALID_REASON, "workspace_id": workspace_id},
        )

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
        return err(
            rid,
            ERR_NOT_FOUND,
            f"unknown workspace: {workspace_id}",
            {"reason": "workspace_not_found", "workspace_id": workspace_id},
        )

    try:
        actor = store.remove_actor(
            workspace_id,
            actor_key,
            reason=reason or "operator",
            updated_by=updated_by or "operator",
            expect_revision=expect_revision,
            correlation_id=correlation_id,
        )
    except NotFound:
        # Its OWN reason, distinct from ``workspace_not_found``: the office is
        # there and this key is not in it, which is a different client story
        # (a stale key, or a key the store canonicalized differently) with a
        # different cure (refetch the projection, not re-author the office).
        return err(
            rid,
            ERR_NOT_FOUND,
            f"unknown actor: {actor_key}",
            {
                "reason": "actor_not_found",
                "workspace_id": workspace_id,
                "actor_key": actor_key,
            },
        )
    except StaleRevision as exc:
        # ``data`` deliberately carries NO current revision — the same rule the
        # upsert follows, and for the same reason: handing back the number
        # invites a retry with it, which is the lost update the guard refused.
        return err(
            rid,
            ERR_CONFLICT,
            str(exc),
            {
                "reason": "stale_revision",
                "workspace_id": workspace_id,
                "actor_key": actor_key,
                "expect_revision": expect_revision,
            },
        )
    except ArchiveUnreadable as exc:
        # The already-archived (idempotent) branch: this key's archive copy is
        # the only place its post-archive revision lives, and this ack CARRIES
        # that revision as the token a later guarded write must present. A decode
        # failure there is the token going missing, so the refusal is typed with
        # the same reason the upsert leg spends — one string per condition, not
        # one per verb.
        #
        # What it replaced was worse than an untyped crash: ``JSONDecodeError``
        # is a ``ValueError``, so the bare read fell into the ``actor_invalid``
        # arm below and told the client to FIX ITS PAYLOAD for a corrupt file on
        # the server. The mutation test for this arm still shows ``-32602``.
        return err(
            rid,
            ERR_INVALID_REQUEST,
            str(exc),
            {
                "reason": ArchiveUnreadable.code,
                "workspace_id": workspace_id,
                "actor_key": actor_key,
            },
        )
    except ValueError as exc:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            str(exc),
            {"reason": "actor_invalid", "workspace_id": workspace_id},
        )

    log_office_write(
        op="runtime.office.remove",
        correlation_id=correlation_id,
        workspace=workspace_id,
        actor_key=actor.actor_key,
        revision=actor.revision,
        state=actor.state,
    )
    result: dict[str, Any] = {
        "actor_key": actor.actor_key,
        "revision": actor.revision,
        "state": actor.state,
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return ok(rid, result)


@method("runtime.office.surface.update", tier=TIER_CONSOLE)
def _runtime_office_surface_update(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """The FOLDER TAXONOMY of one office surface, rewritten.

    Params: ``workspace_id`` (required), ``folders`` (required list of strings),
    ``expect_revision`` (optional int), ``updated_by`` (optional string,
    defaulting to the argv lane's own ``operator``), and ``correlation_id``
    (optional gesture token — see ``runtime.office.upsert``).

    Result: ``{"workspace_id", "folders", "revision"}`` — the folder list AS THE
    STORE NORMALIZED IT, and the post-write surface revision.

    ``folders`` is a LIST on this lane, deliberately
    -----------------------------------------------
    The capability lane joins the folders with commas onto one argv string
    because argv has no other shape. That encoding is an ARGV ARTIFACT and it is
    lossy in a way nobody has tripped over yet only because folder names happen
    not to contain commas — ``_safe_folder`` collapses whitespace and truncates
    at 80 chars but keeps every comma it is given, so ``"Design, Ops"`` splits
    into two folders on the way through. A typed lane must not copy an
    encoding's accidents, so the list stays a list and
    ``OfficeStore.update_surface`` receives exactly what the operator arranged.

    Why the ECHO is the load-bearing half of the reply
    --------------------------------------------------
    ``_normalize_folders`` is not identity: it always prepends
    ``DEFAULT_FOLDERS``, drops duplicates and blanks, and stops at
    ``MAX_FOLDERS``. The launcher's flush has, until this method existed, copied
    its OWN desired list into ``serverFolders`` on accept — so any normalization
    difference left the two permanently disagreeing and the folder branch
    re-firing on every subsequent flush, one write per flush forever. Echoing
    the store's canonical list is what closes that loop, which is why the reply
    carries the whole list rather than the light ``{revision}`` ack the actor
    verbs answer with. It is small by construction (≤64 names ≤80 chars).

    Why this REFUSES an unknown workspace instead of authoring one
    --------------------------------------------------------------
    Same ruling as ``runtime.office.upsert``'s, and it bites harder here.
    ``update_surface`` calls ``ensure_surface`` unconditionally on its non-dry
    path, so on this lane a typo'd ``workspace_id`` would not merely write to
    the wrong place — it would AUTHOR a whole office, emit
    ``office.surface.created``, and leave it on disk forever, while the read leg
    (``runtime.office.get``) answers the same typo with ``workspace_not_found``.
    A pair where the read refuses what the write invents is incoherent, and the
    write is the worse half. The lazy-create path stays where a human can see
    what they made: the argv lane.

    No class-key fence and no reservation, for the same reasons the archive
    records: this moves no actor rows, and it is one store call under one
    ``office_lock`` guarded by ``expect_revision``, so a transport retry
    converges.
    """

    from agent_runtime.errors import StaleRevision
    from agent_runtime.office_store import OfficeStore

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        # The same reason string every office method spends on this, on
        # purpose: one client branch covers it whatever the verb was.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )

    folders = params.get("folders")
    # Checked HERE rather than left to the store, because the store does not
    # refuse: ``_normalize_folders`` answers a non-list with the DEFAULT list,
    # so a client that sent a string would silently have its taxonomy reset to
    # ``("Agents", "Desks")`` and be acked. A per-element string check rides the
    # same reason: ``_safe_folder`` stringifies whatever it is handed, so a
    # number would be written as ``"3"`` and echoed back as a folder the
    # operator never named.
    if not isinstance(folders, list) or not all(
        isinstance(name, str) for name in folders
    ):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: folders must be a list of strings",
            {"reason": "folders_invalid", "workspace_id": workspace_id},
        )

    expect_revision = params.get("expect_revision")
    # ``bool`` is an ``int`` in Python and ``True`` would silently mean revision
    # 1 — a wrong guard is worse than no guard, so the type check is explicit.
    if expect_revision is not None and (
        isinstance(expect_revision, bool) or not isinstance(expect_revision, int)
    ):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: expect_revision must be an integer or omitted",
            {"reason": "expect_revision_invalid"},
        )

    updated_by = params.get("updated_by")
    if updated_by is not None and not isinstance(updated_by, str):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: updated_by must be a string or omitted",
            {"reason": "updated_by_invalid"},
        )

    try:
        correlation_id = _correlation_id_param(params)
    except _CorrelationIdRefused as refused:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            refused.message,
            {"reason": CORRELATION_ID_INVALID_REASON, "workspace_id": workspace_id},
        )

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
        return err(
            rid,
            ERR_NOT_FOUND,
            f"unknown workspace: {workspace_id}",
            {"reason": "workspace_not_found", "workspace_id": workspace_id},
        )

    try:
        surface = store.update_surface(
            workspace_id,
            folders=folders,
            updated_by=updated_by or "operator",
            expect_revision=expect_revision,
            correlation_id=correlation_id,
        )
    except StaleRevision as exc:
        # ``data`` deliberately carries NO current revision — the same rule both
        # actor verbs follow, and for the same reason: handing back the number
        # invites a retry with it, which is the lost update the guard refused.
        return err(
            rid,
            ERR_CONFLICT,
            str(exc),
            {
                "reason": "stale_revision",
                "workspace_id": workspace_id,
                "expect_revision": expect_revision,
            },
        )
    except ValueError as exc:
        # The store's own ``invalid_request`` sentence rides ``message``; one
        # reason, because the client's response to every one of them is the
        # same — fix the payload, it is a launcher bug.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            str(exc),
            {"reason": "folders_invalid", "workspace_id": workspace_id},
        )

    log_office_write(
        op="runtime.office.surface.update",
        correlation_id=correlation_id,
        workspace=workspace_id,
        folders=len(surface.folders),
        revision=surface.revision,
    )
    result: dict[str, Any] = {
        "workspace_id": surface.workspace_id,
        "folders": list(surface.folders),
        "revision": surface.revision,
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return ok(rid, result)


@method("runtime.office.resolve_conflict", tier=TIER_CONSOLE)
def _runtime_office_resolve_conflict(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE realm-sync conflict, ADOPTED — the sync strip's resolve button.

    Params: ``workspace_id`` and ``actor_key`` (required strings), ``take``
    (required, ``"local"`` or ``"remote"``), ``updated_by`` (optional string,
    defaulting to the argv lane's own ``operator``), and ``correlation_id``
    (optional gesture token — see ``runtime.office.upsert``).

    Result: ``{"actor_key", "take", "state", "revision"?}``. ``revision`` is
    present iff the resolution left an ACTOR behind; its absence is the
    edit-vs-remove tombstone, which is a real outcome of this verb and not an
    error (see below). ``actor_key`` is the STORE's, not the caller's, for the
    reason the upsert's ack records: ``resolve_conflict(take="remote")`` writes
    the key the peer's RECORD carries, and a sidecar's filename and its record
    are allowed to disagree ("payload is truth; the filename is routing only" —
    ``office_sync._read_remote_office``). Echoing the requested key would name a
    row the store did not write. ``take`` is echoed NORMALIZED (stripped,
    lowercased), so a client reading the ack learns which side actually won
    rather than which spelling it happened to send.

    The last office write verb to reach the wire, and the UNFINISHED MAIN PATH
    rather than a fallback deletion: ``harness office resolve-conflict`` stays
    the operator's lane and keeps its flags; this method wraps the same
    ``OfficeStore.resolve_conflict`` the CLI verb calls, adding envelope work
    and nothing else.

    Why ``take`` is validated HERE and not left to the store
    -------------------------------------------------------
    ``OfficeStore.resolve_conflict`` answers an unrecognized ``take`` with
    ``ValueError("invalid_request")`` — a bare string that carries no field
    name, and which this handler would then have to spend its ``actor_invalid``
    reason on, telling the client to inspect a payload whose only fault is one
    enum value. The typed reason is cheaper client-side and the check runs
    BEFORE the store is touched at all, so a nonsense ``take`` cannot even open
    the workspace it named.

    Why an already-resolved conflict is a 4001 and NOT the remove's idempotent
    ok
    -------------------------------------------------------------------------
    The two verbs look like they should agree here and must not. A repeat
    ARCHIVE describes the state the store is already in, so answering ok costs
    nothing and writes nothing. A repeat RESOLVE has no conflict to adopt: the
    sidecar is gone, the store has no remote copy to read, and the only way to
    answer ok would be to invent one. ``resolve_conflict`` refuses that with
    ``SyncConflict("no_conflict:…")``, and this lane translates it to
    ``4001 {reason: "conflict_not_found"}`` — a 4001 rather than 4090 because
    nothing raced: the named conflict does not exist, which is the same shape of
    answer the read leg gives an unknown workspace. Note that ``SyncConflict``
    means the OPPOSITE thing on ``runtime.office.upsert`` (there a sidecar
    EXISTS and blocks the write, so it is a 4090 ``sync_conflict``); copying
    that arm here would tell a client to fetch an operator for a conflict that
    has already been resolved.

    ``NotFound`` lands on the same reason on purpose. It is reachable only as a
    race — the live actor file disappearing between ``actor_exists`` and
    ``get_actor`` inside the store — and the client's cure is identical: refetch
    the projection, there is nothing here to resolve.

    NO ``allow_class_key``, and that asymmetry is the contract
    ---------------------------------------------------------
    ``take="remote"`` writes a PEER's actor with ``_write_actor``, past
    ``upsert_actor`` and every guard its callers hold, so its class-key fence
    lives inside the store (``OfficeStore._guard_class_keyed_adoption``) where a
    second caller inherits it instead of having to remember it. This method is
    that second caller, and it arrives fenced by construction because it calls
    the same store method the CLI does.

    The override stays where consent lives. ``harness office resolve-conflict
    --allow-class-key`` is an operator who read the refusal and typed it; a wire
    PARAMETER is not consent — it is a constant in a client build, set once by
    whoever was debugging the day resolves started failing, and thereafter sent
    by every install on every resolution with no human in any loop. So this
    handler takes no such param, forwards no such param, and an unknown
    ``allow_class_key`` key in ``params`` is inert. The sanctioned override arms
    are untouched: the CLI flag, and ``harness office actor-restore``.

    Which is also why the refusal message is BUILT here rather than passed
    through. The store's sentence ends by offering ``--allow-class-key`` and
    ``--take local``; the first is advice this caller cannot follow, and the
    upsert's arm already ruled that advice a caller cannot follow is worse than
    none. The wire message names the one exit this lane really has —
    ``take: "local"`` — and the machine-readable evidence rides ``data`` with
    the same keys the upsert's collision spends, so one client branch covers
    ``class_key_collision`` whichever verb hit it.
    """

    from agent_runtime.errors import ArchiveUnreadable, NotFound, SyncConflict
    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused
    from agent_runtime.office_store import OfficeStore

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        # The same reason string every office method spends on this, on
        # purpose: one client branch covers it whatever the verb was.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )

    raw_key = params.get("actor_key")
    actor_key = raw_key.strip() if isinstance(raw_key, str) else ""
    if not actor_key:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: actor_key must be a non-empty string",
            {"reason": "actor_key_required"},
        )

    raw_take = params.get("take")
    # Normalized the way the store normalizes it, so the handler and the store
    # cannot disagree about which spellings are legal — and echoed from this
    # value, so the ack names the side that won.
    take = raw_take.strip().lower() if isinstance(raw_take, str) else ""
    if take not in {"local", "remote"}:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            'invalid params: take must be "local" or "remote"',
            {"reason": "take_invalid", "workspace_id": workspace_id},
        )

    updated_by = params.get("updated_by")
    if updated_by is not None and not isinstance(updated_by, str):
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: updated_by must be a string or omitted",
            {"reason": "updated_by_invalid"},
        )

    try:
        correlation_id = _correlation_id_param(params)
    except _CorrelationIdRefused as refused:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            refused.message,
            {"reason": CORRELATION_ID_INVALID_REASON, "workspace_id": workspace_id},
        )

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
        return err(
            rid,
            ERR_NOT_FOUND,
            f"unknown workspace: {workspace_id}",
            {"reason": "workspace_not_found", "workspace_id": workspace_id},
        )

    try:
        actor = store.resolve_conflict(
            workspace_id,
            actor_key,
            take=take,
            updated_by=updated_by or "operator",
            correlation_id=correlation_id,
        )
    except ClassKeyedPlacementRefused as exc:
        details = dict(getattr(exc, "safe_details", None) or {})
        reasons = list(details.get("reasons") or [])
        conflicting = list(details.get("conflicting_actor_keys") or [])
        return err(
            rid,
            ERR_CONFLICT,
            (
                f"class-keyed adoption for persona {details.get('persona_id')!r} "
                f"refused: {', '.join(reasons)}"
                # Named when there is one — the two reasons fire on different
                # evidence (a ledger entry vs. a live sibling) and only the
                # second has another actor to point at.
                + (
                    f" (conflicts with {', '.join(conflicting)})"
                    if conflicting
                    else ""
                )
                + ". The class→instance re-key archived this class key; adopting "
                'the peer\'s copy undoes that migration. Resolve with take: "local" '
                "to keep the migrated state."
            ),
            {
                "reason": "class_key_collision",
                "workspace_id": workspace_id,
                "actor_key": actor_key,
                "persona_id": details.get("persona_id"),
                "class_actor_key": details.get("class_actor_key"),
                "reasons": reasons,
                "conflicting_actor_keys": conflicting,
                "take": take,
            },
        )
    except SyncConflict as exc:
        # ``no_conflict:<key>`` — see the docstring for why this is a 4001 and
        # not the 4090 ``sync_conflict`` the upsert spends on the same class.
        return err(
            rid,
            ERR_NOT_FOUND,
            str(exc),
            {
                "reason": "conflict_not_found",
                "workspace_id": workspace_id,
                "actor_key": actor_key,
            },
        )
    except NotFound as exc:
        # The race arm, same reason and same cure: refetch the projection.
        return err(
            rid,
            ERR_NOT_FOUND,
            str(exc),
            {
                "reason": "conflict_not_found",
                "workspace_id": workspace_id,
                "actor_key": actor_key,
            },
        )
    except ArchiveUnreadable as exc:
        # EG-1.5's typed refusal, surfaced rather than swallowed. No store path
        # inside ``resolve_conflict`` raises this TODAY — the arm is here because
        # ``ArchiveUnreadable`` is an ``AgentRuntimeError`` and not a
        # ``ValueError``, so without it the condition would arrive as
        # ``handle_request``'s catch-all ``-32000 handler_failed``: a corrupt
        # file on the server reported to the client as "the handler crashed",
        # with no reason to branch on and nothing an operator could act on. One
        # condition gets one name across all three write verbs, so a client that
        # learned ``archive_unreadable`` from the archive leg reads it here too.
        return err(
            rid,
            ERR_INVALID_REQUEST,
            str(exc),
            {
                "reason": ArchiveUnreadable.code,
                "workspace_id": workspace_id,
                "actor_key": actor_key,
            },
        )
    except ValueError as exc:
        # Whatever the store rejected while normalizing ids. ``take`` cannot
        # arrive here — it was validated above — so every remaining case is a
        # malformed identifier, and the client's response to all of them is the
        # same: fix the payload, it is a launcher bug.
        return err(
            rid,
            ERR_INVALID_PARAMS,
            str(exc),
            {"reason": "actor_invalid", "workspace_id": workspace_id},
        )

    if actor is None:
        # The edit-vs-remove tombstone: the peer removed what this side edited,
        # so the resolution ARCHIVED the local row and there is no revision to
        # present. Reported as a success with no ``revision`` key rather than as
        # a refusal, because the operator's conflict really is resolved — and
        # the missing key is what tells the client not to keep guarding a row
        # that no longer exists.
        log_office_write(
            op="runtime.office.resolve_conflict",
            correlation_id=correlation_id,
            workspace=workspace_id,
            actor_key=actor_key,
            take=take,
            state="archived",
        )
        tombstone: dict[str, Any] = {
            "actor_key": actor_key,
            "take": take,
            "state": "archived",
        }
        if correlation_id is not None:
            tombstone["correlation_id"] = correlation_id
        return ok(rid, tombstone)
    log_office_write(
        op="runtime.office.resolve_conflict",
        correlation_id=correlation_id,
        workspace=workspace_id,
        actor_key=actor.actor_key,
        take=take,
        state=actor.state,
        revision=actor.revision,
    )
    result: dict[str, Any] = {
        "actor_key": actor.actor_key,
        "take": take,
        "state": actor.state,
        "revision": actor.revision,
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return ok(rid, result)


# ── runtime.agent.create ─────────────────────────────────────────────────────


@method("runtime.agent.create", tier=TIER_CONSOLE)
def _runtime_agent_create(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE call places an agent: roster row, chat root and placement together.

    Params: ``persona_id``, ``workspace_id`` and ``idempotency_key`` (required);
    ``position: [x, y]``, ``skills: [id, ...]``, ``display_name``,
    ``placement_id``, ``realm_id``, ``folder``, ``correlation_id`` (all
    optional).

    ``position`` ABSENT (omitted or ``null``) means the client did not aim, and
    the service resolves the slot through ``agent_runtime.office_layout_policy``
    — the same lattice the launcher predicts with, pinned across the two repos
    by ``tests/fixtures/office_layout/cases.json`` (plan D2). Present, it is
    taken verbatim, exactly as before.

    ``skills`` ABSENT leaves the new instance inheriting its persona's skills; a
    list assigns ``skill_overrides`` at the instance tier after the placement,
    installing and hash-verifying every canonical id first (plan D5). It rides
    the RPC params rather than a CLI-only flag precisely so a remote connector,
    which can never run the install's CLI, gets the whole verb over ``call``.
    Two refusals are its own — ``skill_unresolved`` (-32602, with ``data.skill``
    and ``data.status``) and ``skill_install_diverged`` (-32000, with both
    hashes) — and both carry ``phase: "skills"`` with ``rolled_back: false``:
    the agent is PLACED and kept, and the same ``idempotency_key`` resumes the
    phase.

    Result::

        {persona_instance_id, persona_id, placement_id, display_name,
         default_chat_session_id, actor_key, revision, workspace_id,
         position: [x, y], actor: {...},
         skills: {assigned: [...], installed: [{skill, changed, installed_hash}]},
         actor_fresh: bool,
         phases: {instance_ms, placement_ms, skills_ms, total_ms},
         idempotent_replay}

    On an ``idempotent_replay`` the ``actor``/``revision``/``position`` are
    RE-READ off the live row rather than echoed from the receipt — the recorded
    ones can be arbitrarily old, and a client that adopts them would adopt a
    stale ``revision`` into its ``expect_revision`` bookkeeping. ``actor_fresh``
    is ``false`` exactly when that re-read could not be made (the actor was
    archived, the surface is gone), and the recorded row is then returned
    unchanged rather than invented.

    ``actor_key``/``revision`` mirror ``runtime.office.upsert``'s light ack on
    purpose, so the launcher's existing prediction and ``expect_revision``
    bookkeeping keeps working with no new decoder.

    ``position`` is what was WRITTEN — policy or verbatim — and ``actor`` is the
    row as STORED, in the SAME item shape ``runtime.office.get`` renders
    (``office_models.office_actor_wire_row``, which that method's projection now
    also flattens through). Both keys are ADDITIVE: an old client ignores them,
    ``RPC_CONTRACT_VERSION`` does not move, and no name joins the manifest's
    ``methods`` list.

    UC-H1 — this is a TRANSLATION SHIM and nothing else
    ---------------------------------------------------
    The sequence (reserve → mint → place → compensate/resume) lives in
    ``agent_create.perform_agent_create``, which is the same function
    ``harness agent create`` calls with no serve in the picture. Everything
    below is JSON-RPC envelope work: the reply dict and every ``data.reason``
    string come out of the service unchanged, because the launcher's
    ``missionAgentCreateReasonFrom`` decoder is the fielded consumer that pins
    them. If a refusal string ever needs to change, it changes in the service —
    a second spelling here would be the copy the hoist exists to abolish.

    Note the deliberate absence of a ``try``: ``KeyboardInterrupt`` and every
    other ``BaseException`` must keep propagating exactly as they did inline,
    since the crash-between-the-two-writes property is asserted by letting one
    escape.
    """

    from agent_runtime.agent_create import perform_agent_create

    # ``context.caller`` travels INTO the service (Stage A6): the front-door
    # gate above has already refused a caller without the console tier, and the
    # service checks the same predicate again with the same caller. That is the
    # point of a backstop — the guarantee stops being a property of this
    # dispatcher and becomes a property of the verb. Passing the caller rather
    # than a "the gate already ran" flag is what keeps it non-bypassable: a flag
    # is something a caller could eventually set.
    outcome = perform_agent_create(
        params, caller=context.caller if context is not None else None
    )
    if outcome.refusal is not None:
        refusal = outcome.refusal
        return err(rid, refusal.code, refusal.message, refusal.data)
    return ok(rid, outcome.result)


# ── runtime.agent.retire ─────────────────────────────────────────────────────


@method("runtime.agent.retire", tier=TIER_CONSOLE)
def _runtime_agent_retire(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE call retires an agent: the roster row AND every actor bound to it.

    Params: ``persona_instance_id`` (required); ``reason``, ``requested_by``,
    ``correlation_id`` (optional).

    Result::

        {persona_instance_id, persona_id, display_name, mode, reason,
         requested_by, archive_path, archive_dir, archived_actor_keys: [...],
         office_archive_failures: [{actor_key, workspace_id, error}],
         already_retired, correlation_id?}

    ``correlation_id`` (S8b) rides onto the ``office.actor.removed`` event and
    the ``state.patched`` remove row this call produces, and is echoed on the
    ack when sent. Until it existed this was the ONLY level-mutating verb with
    no gesture token, so a launcher's create half and delete half could not be
    joined by one grep — see ``agent_retire.perform_agent_retire``.

    The INVERSE of ``runtime.agent.create``, and the join its absence left
    unmade: the launcher removed a deliberate placement through two unjoined
    lanes (a ``persona.instance.retire`` argv capability AND
    ``runtime.office.remove``), so a half-state — actor archived with the row
    still live, or the reverse — was representable and nothing detected it. One
    call now archives both halves, and ``archived_actor_keys`` /
    ``office_archive_failures`` make the office half — best-effort inside the
    store, and until plan D7 also SILENT — visible on the ack.

    Idempotent under retry: a second retire of an archived id answers the same
    ack with ``already_retired: true`` rather than ``not_found``, because a
    remote client that lost the first ack must be able to ask again.

    Refusals are ``PersonaInstanceRetireError``'s codes one-to-one:
    ``not_found`` → ``ERR_NOT_FOUND``; ``canonical_persona_channel`` /
    ``instance_active`` → ``ERR_CONFLICT`` with ``data.reason`` carrying the
    code verbatim. (The two assignment refusals this list carried until AX2 left
    with the store guards that raised them, 2026-08-31.)

    **Authorization scope (placement plan §A.11, owner decision D10-iv):
    ``console``.** It mutates the level exactly as ``runtime.office.*`` does, and
    it is deliberately NOT on any peer-tier allowlist — an agent on install A
    never retires an agent on install B; a remote OPERATOR (device tier) does.

    A TRANSLATION SHIM and nothing else, exactly like ``_runtime_agent_create``:
    the sequence lives in ``agent_retire.perform_agent_retire``, which is the
    same function ``harness agent retire`` and ``harness persona instance
    retire`` call with no serve in the picture. Adding this name to the manifest
    GROWS the set without moving ``RPC_CONTRACT_VERSION`` — a manifest is a set
    plus an integer, and the integer moves only when an existing method's shape
    changes incompatibly.
    """

    from agent_runtime.agent_retire import perform_agent_retire

    # Stage A6's backstop, exactly as ``_runtime_agent_create`` threads it.
    outcome = perform_agent_retire(
        params, caller=context.caller if context is not None else None
    )
    if outcome.refusal is not None:
        refusal = outcome.refusal
        return err(rid, refusal.code, refusal.message, refusal.data)
    return ok(rid, outcome.result)


# ── runtime.persona.prewarm ──────────────────────────────────────────────────


@method("runtime.persona.prewarm", tier=TIER_READ)
def _runtime_persona_prewarm(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Fill this persona type's visibility memos BEFORE a create needs them.

    Params: ``persona_id`` (required); ``correlation_id`` (optional, echoed).

    Result::

        {persona_id, accepted: true, state: "started" | "already_running",
         correlation_id?}

    Fire-and-forget by contract. The reply says a warm was ACCEPTED, never that
    it finished — the whole point is that the caller (the launcher, on palette
    open) is doing something else while it runs, and a caller that awaited the
    warm would have moved the cold cost rather than removed it. So there is no
    completion field to wait on and none is coming: the observable effect is the
    NEXT ``runtime.agent.create``'s ``phases.instance_ms``, which the drop log
    already prints.

    Additive in the strongest sense. It registers a new method rather than
    changing one, it writes no store state, emits no event and mints no id, and
    a runtime nobody ever calls it on behaves exactly as it does today. A client
    that does not find ``runtime.persona.prewarm`` in the ``rpc`` manifest block
    simply keeps paying the cold create — which is why the contract integer does
    not move for it (see :func:`manifest`).

    Why the refusals are the CREATE's refusals. ``persona_not_found`` /
    ``persona_roster_unavailable`` come out of ``agent_create``'s own spellings,
    codes included, so a launcher that prewarms an id and then creates it can
    never be told two different stories about that id. The one reason this verb
    owns alone is ``profile_persona_not_prewarmable`` — the D-U1 carve-out the
    create accepts and a prewarm provably cannot serve; the service docstring
    says why.

    A failure INSIDE the warm never reaches here. It happens on the worker,
    after this frame is already on the wire, and is swallowed-and-logged there:
    a cache that did not fill costs latency, never correctness.

    On the inline-dispatch budget. ``serve.py`` answers this lane on the reader
    loop itself, on the stated grounds that a method touches a handful of small
    JSON files and is done before the loop misses anything. That still holds:
    the only synchronous work here is the roster read that decides accept-vs-
    refuse, which is the same read ``runtime.agent.create`` already performs
    inline on the same loop. The seconds — registry populate, toolset sweep,
    readiness — are exactly what moves to the worker, which is the point.
    """

    from agent_runtime.persona_prewarm import request_persona_prewarm

    outcome = request_persona_prewarm(params)
    if outcome.refusal is not None:
        refusal = outcome.refusal
        return err(rid, refusal.code, refusal.message, refusal.data)
    return ok(rid, outcome.result)


# ── runtime.chat.message / runtime.chat.steer ────────────────────────────────
#
# Gateway Stage 3. The plan's Stage 3 sketch said "RPC where methods exist,
# op/argv lane otherwise — same union", and that union has a hole a device falls
# through: ``mission.chat.*`` has no methods, it lowers to argv, and Stage 1
# REFUSES the argv lane to devices outright (``serve.py``'s ``_is_gateway``
# branch — the refusal that stops a ``read`` device from sending as argv what it
# was refused on the method lane). A remote device therefore could not send a
# chat turn at all, and chat is what this gateway is for.
#
# So the two chat-turn verbs are ported to the method lane, which is the
# direction ``planned/runtime-rpc-call-half.md`` already had, and the scope is
# deliberately those two and not the argv surface. The local stdio lane keeps
# its argv path byte-for-byte: nothing here changes how the launcher's local
# session sends a turn today.


@method("runtime.chat.message", tier=TIER_CONSOLE)
def _runtime_chat_message(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Send ONE Mission Control chat turn. Accepts and hands off; never blocks.

    Params: ``turn_request_id``, ``persona_id``, ``message`` (required);
    ``session_id``, ``persona_instance_id``, ``workspace_id``, ``title``,
    ``new_session``, ``stream``, ``max_seconds``, ``correlation_id`` (optional).

    Result::

        {turn_request_id, request_id, accepted: true, state: "accepted",
         verb, idempotent_replay, settled, exit_code?, correlation_id?}

    **The ack is an ACCEPT, not a reply**, and that is forced by the lane rather
    than chosen: this dispatcher answers INLINE on the reader loop (see the
    method-lane comment in ``serve.py``, which names chat turns as the reason
    the worker pool exists), so a method that ran a turn would stall every other
    client attached to this serve for its whole length. The turn's frames —
    deltas when ``stream`` is asked for, the final ``--json`` payload always —
    ride the existing per-request frame lane under the returned ``request_id``,
    which is the same lane the local launcher already reads. No second streaming
    transport is invented; the socket lane has carried per-request frames since
    it existed.

    **Tier: ``console``, and honestly rather than conveniently.** The tempting
    read is that a chat turn is not a level mutation, so it should be something
    softer — and R11's own sentence ("a paired console device may chat") can be
    satisfied by a new ``chat`` word. It should not be, for two reasons. The
    first is what the verb DOES: a chat turn runs an agent with tools. It can
    write files, spawn dispatches, install skills and place agents, so a tier
    below ``console`` would be a door around ``console`` — the ``read`` device
    refused ``runtime.agent.retire`` could ask an agent to retire one. The
    second is mechanical and would have bitten immediately:
    ``call_authorization.authorize_call``'s device arm is an EQUALITY against
    the stored word, not an ordering, so a new ``chat`` tier would have refused
    every already-paired ``console`` device the very thing R11 says it may do.
    Declaring chat at ``console`` satisfies R11 exactly, keeps the vocabulary at
    two words, and changes no predicate. If an ``admin``/``chat`` vocabulary is
    ever wanted it is still R11's question, and the honest first move there is
    to make the device arm an ordering — which is a decision, not a constant.

    **Exactly-once, and the correction it rests on.** The plan records that
    mission-chat send has no server-side dedupe ("no ``turn_request_id``
    anywhere", re-verified 2026-08-27). The grep was right and the conclusion
    was wrong: mission chat has carried exactly-once under the name
    ``client_message_id`` plus the per-session turn journal since the 2026-08-24
    incident, replying ``idempotent_replay: True`` with the committed reply,
    ``chat_turn_duplicate_in_flight`` while the turn runs, and
    ``chat_turn_outcome_unknown`` when the provider outcome cannot be proven.
    ``turn_request_id`` is therefore not a second key — it is passed to
    ``--client-message-id`` unchanged, so the journal that already owns this
    keys on exactly what the device sent. What the reservation
    (``chat_turn_reservations``) adds is only the ACCEPT window the journal
    cannot cover, because the journal's first write happens inside the chat-root
    lease, after a worker is already running. See that module's docstring.

    A TRANSLATION SHIM, exactly like ``_runtime_agent_create`` — with the shim
    landing one step lower. Mission chat's service is an argparse handler, not a
    ``perform_*`` function, and its one existing second door
    (``dispatch_delivery.deliver_via_mission_chat``) reaches it by building a
    namespace. This door builds ARGV, which the worker dispatches through the
    same argparse tree a local send uses, so a remote turn and a local turn are
    the same execution rather than two implementations that agree today.
    """

    from agent_runtime.chat_turn import CHAT_MESSAGE_METHOD, perform_chat_turn

    outcome = perform_chat_turn(
        params,
        verb=CHAT_MESSAGE_METHOD,
        spawn=None if context is None else context.spawn_chat_turn,
    )
    if outcome.refusal is not None:
        refusal = outcome.refusal
        return err(rid, refusal.code, refusal.message, refusal.data)
    return ok(rid, outcome.result)


@method("runtime.chat.steer", tier=TIER_CONSOLE)
def _runtime_chat_steer(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Steer the chat turn currently running on a root. Accepts and hands off.

    Params: ``turn_request_id``, ``session_id``, ``message`` (required);
    ``persona_id``, ``persona_instance_id``, ``correlation_id`` (optional).

    Result: ``runtime.chat.message``'s, with ``verb`` naming this method.

    Everything in that method's docstring applies here — the accept-not-reply
    contract, the ``console`` tier, and the ``turn_request_id`` →
    ``client_message_id`` identity — and one thing is specific to steer:
    ``harness mission-chat steer`` already REQUIRES ``--client-message-id``,
    where the send merely accepts it. So the remote steer is the verb whose
    exactly-once key was never optional, and the reservation over it is the
    accept-window cover rather than the key itself.

    It rides the worker lane rather than answering inline even though a steer is
    cheap, and that is a deliberate uniformity: ``_CHAT_TURN_COMMANDS`` in
    ``serve.py`` counts BOTH ``mission-chat message`` and ``mission-chat steer``
    as in-flight chat turns for the drain ledger, and a steer that skipped the
    worker would be a chat turn the recycle protection could not see.
    """

    from agent_runtime.chat_turn import CHAT_STEER_METHOD, perform_chat_turn

    outcome = perform_chat_turn(
        params,
        verb=CHAT_STEER_METHOD,
        spawn=None if context is None else context.spawn_chat_turn,
    )
    if outcome.refusal is not None:
        refusal = outcome.refusal
        return err(rid, refusal.code, refusal.message, refusal.data)
    return ok(rid, outcome.result)


# ── runtime.workspace.use / runtime.realm.use ────────────────────────────────
#
# Plan WS4 (``planned/instant-workspace-switching.md``), ruling R-W1. TWO things
# at once, and the second is the reason the stage exists rather than a latency
# footnote:
#
#  1. the local switch's ACCEPT stops being a process spawn and becomes a socket
#     round trip on a lane the launcher already holds open;
#  2. the pointer gets a SERVER-SIDE enforcement point. `local_console` is the
#     tier, and the device arm of ``call_authorization.authorize_call`` is an
#     EQUALITY against the word a device's own pairing record holds — so a
#     device paired at ``read`` is refused, a device paired at ``console`` is
#     refused too (its word is ``console`` and this verb wants the machine
#     owner's kind, not a tier a remote credential can hold), and a paired
#     INSTALL is refused because ``PEER_METHOD_ALLOWLIST`` admits nothing it was
#     not edited to admit. That is RS4's R-B, built here instead of asked for
#     politely at the client.
#
# **WHERE the enforcement is, and why not here.** These handlers contain no
# authorization code at all: the restriction is one membership set at the
# chokepoint (``call_authorization.LOCAL_CONSOLE_METHODS``), evaluated by
# ``authorize_call`` before dispatch, which is where Ruling A put the decision
# and where every other caller-kind rule already lives. A handler-local check
# would be a second policy in the dispatcher's own file, and the module's
# opening argument — ``serve_rpc`` renders refusals, it does not author them —
# would stop being true the day someone edited one of the two and not the other.
#
# **The correction the set carries** (WS4 field notes, 2026-09-01): the plan said
# the device arm's tier EQUALITY alone refuses a device caller. It refuses a
# ``read`` device and not a ``console`` one — R11 explicitly contemplates paired
# console devices — so R-B needed a kind test beside the strength test. That
# argument is written out at :data:`call_authorization.LOCAL_CONSOLE_METHODS`.
#
# NOT on ``PEER_METHOD_ALLOWLIST``, and deliberately with no edit: that set
# admits nothing it does not name, and ``test_peer_authorization`` iterates the
# whole registry against it, so these two names arrive already covered by a test
# nobody had to touch. Canon 06's sentence — a remote OPERATOR acts on an
# install, another install's agents do not — reads the same for a scope pointer
# as it does for an agent retire.
#
# The argv verbs STAY (CLI parity, scripts, and an older launcher), and both
# doors call ``agent_runtime.scope_activation`` so there is one decision and one
# row. The launcher's argv lowering for these two is marked for delete in the
# plan's retirement ledger, gated on manifest membership being universal.


@method("runtime.workspace.use", tier=TIER_CONSOLE)
def _runtime_workspace_use(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Park this install's active-workspace pointer.

    Params: ``workspace_id`` (required); ``issued_at`` (optional, the supersede
    basis the argv verb takes as ``--issued-at``); ``correlation_id`` (optional,
    echoed).

    Result: the argv verb's row, verbatim — ``{id, name, realm_id, agents, …,
    applied}`` when the write took, and the DECLINED row
    (``applied: false`` plus ``reason`` / ``superseded`` /
    ``requested_workspace_id``) when a strictly newer intent already owns the
    pointer or this exact intent already applied.

    **A declined activation is a RESULT, not an error**, exactly as it is on the
    argv lane where both arms exit 0. Rendering ``superseded`` as a JSON-RPC
    error would make the launcher's accept path treat a correctly-ordered switch
    as a failure and raise the R-A parked-elsewhere surface for something that
    worked.

    Answered INLINE on the reader loop, and it belongs there: the whole write is
    one small JSON file plus one event append — the same budget
    ``runtime.office.upsert`` already spends on this lane, and orders of
    magnitude under the chat turn that made the worker pool necessary.
    """

    from agent_runtime.scope_activation import (
        WORKSPACE_USE_METHOD,
        perform_scope_activation,
    )

    outcome = perform_scope_activation(params, verb=WORKSPACE_USE_METHOD)
    if outcome.refusal is not None:
        denial = outcome.refusal
        return err(rid, denial.code, denial.message, denial.data)
    return ok(rid, outcome.result)


@method("runtime.realm.use", tier=TIER_CONSOLE)
def _runtime_realm_use(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Park this install's active-realm pointer, reconciling the workspace.

    Params: ``realm_id`` (required); ``issued_at``, ``correlation_id``
    (optional).

    Result: ``harness realm use``'s row, verbatim — everything on
    :func:`_runtime_workspace_use` applies, with one addition it inherits from
    the shared implementation rather than re-states: an applied realm switch
    also moves the ACTIVE WORKSPACE when the current one belongs to the realm
    just left (``scope_activation.reconcile_active_workspace_to_realm``). So one
    call can emit two events, and a client that folds them must expect both.
    """

    from agent_runtime.scope_activation import REALM_USE_METHOD, perform_scope_activation

    outcome = perform_scope_activation(params, verb=REALM_USE_METHOD)
    if outcome.refusal is not None:
        denial = outcome.refusal
        return err(rid, denial.code, denial.message, denial.data)
    return ok(rid, outcome.result)


# ── runtime.media.index / runtime.media.get ──────────────────────────────────
#
# Gateway Stage 8, the ``fetch`` family §3.3 named. TWO verbs and not one, and
# the second is not a convenience: a chat image reaches a client as a
# ``MEDIA:<absolute path>`` line inside a message body, so the only pointer the
# client holds is a PATH — and a path is the one thing this lane will not
# accept. Something has to carry the client from what it holds to a handle it
# may spend, and the two candidates are (a) rewrite every stream frame's text
# server-side, which moves the stream contract for every client on every lane to
# solve a problem only a remote one has, or (b) let a client ask, once, what is
# in scope and join on the reference it already has. ``index`` is (b).
#
# The reference travels OUT and never IN. That asymmetry is the entire security
# story of this family: what comes back is a string the caller already rendered
# from a message it was already allowed to read, so it discloses nothing new;
# what goes in is `sha256:<64 hex>` and is refused by a regex before this
# process constructs a ``Path``. See ``agent_runtime/media_handles.py``.


#: The media lane's own shape number, beside ``PEER_PING_CONTRACT`` and for its
#: reason: it describes these two RESULTS and nothing else, so a later stage
#: that grows the family can move it without telling every ``runtime.office.*``
#: client that something changed.
MEDIA_CONTRACT = 1


@method("runtime.media.index", tier=TIER_CONSOLE)
def _runtime_media_index(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """What media this install can hand over, and under what name.

    Params: ``correlation_id`` (optional, echoed). Deliberately NOTHING else —
    no path, no directory, no filter, no chat id. A verb whose scope a caller
    could narrow is a verb whose scope a caller could WIDEN if the narrowing
    argument were ever mis-parsed, and there is no measured need: the whole live
    corpus on this machine is 46 mirrors and 17 declarations.

    Result::

        {contract: 1, cap_bytes, truncated, artifacts: [
            {handle, reference, media_type, size_bytes, fetchable}, …],
         scanned: {logs, declarations}, correlation_id?}

    ``fetchable`` is stated per artifact so a client never spends a round trip
    to be told the cap; :func:`_runtime_media_get` refuses the same artifact
    with the cap named anyway, because a client is free to ignore an index it
    did not read.

    **``truncated`` is not decoration.** It is the difference between "there is
    no handle for that picture" and "the scan stopped before it reached it", and
    a client that could not tell those apart would retry forever on one and never
    on the other.

    On the inline-dispatch budget, which this verb is the first to actually
    spend. The lane is answered on the reader loop; the derivation reads the
    live-log tails and hashes the images they declare, which on the measured
    corpus is 138,622 bytes of JSONL and 2.5 MB of PNG, and the per-file digest
    is memoized on ``(path, size, mtime_ns)`` so a second call in the same
    process re-hashes nothing. The bounds that keep the worst case bounded —
    logs scanned, artifacts minted, bytes read per mirror — are constants in
    ``media_handles`` with the rotation size they are derived from.
    """

    from agent_runtime import media_handles

    try:
        correlation_id = _correlation_id_param(params)
    except _CorrelationIdRefused as refused:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            refused.message,
            {"reason": CORRELATION_ID_INVALID_REASON},
        )

    scope = media_handles.build_media_scope()
    result: dict[str, Any] = {
        "contract": MEDIA_CONTRACT,
        "cap_bytes": media_handles.MAX_FETCH_BYTES,
        "truncated": scope.truncated,
        # Both halves, one ordering. A remote row (Stage P4) describes itself as
        # ``remote: true`` with the peer that holds it and NO path — because
        # there is no file here to name, and a row that claimed one would send
        # a client to a `File(path)` that cannot exist.
        "artifacts": [artifact.describe() for artifact in scope.rows()],
        "scanned": {
            "logs": scope.logs_scanned,
            "declarations": scope.declarations_seen,
            # Stage P4's second source, counted beside the first for the same
            # reason ``truncated`` exists: a client that sees zero completions
            # scanned knows the remote half was never derived, rather than
            # inferring it from an absence of remote rows.
            "completions": scope.completions_scanned,
        },
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return ok(rid, result)


@method("runtime.media.get", tier=TIER_CONSOLE)
def _runtime_media_get(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Hand over ONE artifact's bytes, named by handle and by nothing else.

    Params: ``handle`` (required, ``sha256:<64 hex>``); ``correlation_id``
    (optional, echoed).

    Result::

        {contract: 1, handle, media_type, size_bytes, encoding: "base64",
         data, correlation_id?}

    Refusals, all ``-32000`` with ``data.reason`` as the branch point:
    ``handle_invalid`` (not the grammar — where a path-shaped argument lands),
    ``unknown_handle`` (well-formed, in no scope), ``artifact_too_large``
    (carrying ``cap_bytes`` and ``size_bytes``), ``artifact_unreadable``.

    **``base64`` and the key that says so.** JSON carries no bytes, so there is
    exactly one honest choice and the ``encoding`` key states it rather than
    leaving a client to assume — a reply that silently changed encoding would be
    an image that decoded to noise. The cost is stated too: 5 MiB of artifact is
    ~6.99 MB on one NDJSON line, which is a size this lane can carry in ONE
    frame because the direction matters. ``serve_socket.MAX_LINE_BYTES`` (1 MiB)
    bounds what a CLIENT sends; this request is ~200 bytes. Nothing about this
    reply touches that bound, and the launcher's reader splits lines without one.
    No ranging is built, and the reason is measured rather than assumed — see
    ``media_handles``' cap argument.

    **The tier is ``console``, and this is the row where the one-line rule
    ("a level MUTATION is console, everything else is read") does not decide
    it.** Handing a caller the raw BYTES of a file on this machine is not a read
    of the level, it is an egress; and the read tier is deliberately open to
    ``unknown`` — a caller the transport authenticated but could not place —
    which is precisely the caller who must not be able to pull files off the
    disk. ``console`` is also what a ``read``-tier device gets refused with:
    "viewer" is an operator saying *look at my level*, not *stream me every
    proof screenshot on the machine*.

    **A peer is still refused THIS verb, and Stage P4 did not change that.**
    ``PEER_METHOD_ALLOWLIST`` admits nothing it was not edited to admit, and
    what P4 edited it to admit is :func:`_peer_media_get` — a narrower verb that
    resolves LOCAL rows only. A peer calling ``runtime.media.get`` would be
    asking this install to proxy on its behalf, i.e. to spend a third install's
    edge for a caller that never approved it; the arm below is for the DEVICE
    lane, whose caller is an operator holding a screen on the install that owns
    the peer edge.

    **The proxy arm (Stage P4, ruling R-P3).** A handle that resolves to a
    :class:`~agent_runtime.media_handles.RemoteMediaArtifact` — an artifact a
    paired install minted for a cross-install reply forged into this install's
    chat — is fetched through ``agent_runtime.media_proxy``: one dial, the bytes
    verified against the handle, cached by content address, served in a reply
    shaped exactly like a local one. The client cannot tell, and has nothing it
    would do differently if it could.

    **The honest cost of that arm, stated where the budget note already is.**
    This lane answers inline on the reader loop, so a remote handle whose peer is
    switched off stalls the loop for the proxy's dial timeout
    (``media_proxy.PEER_DIAL_TIMEOUT_SECONDS``, deliberately a short 5 s for this
    reason) before answering ``peer_unreachable``. A LOCAL handle is unaffected,
    the cache means a picture is proxied at most once ever, and moving the media
    family off the reader loop is a filed follow-up rather than something this
    stage half-did.
    """

    from agent_runtime import media_handles

    try:
        correlation_id = _correlation_id_param(params)
    except _CorrelationIdRefused as refused:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            refused.message,
            {"reason": CORRELATION_ID_INVALID_REASON},
        )

    raw = params.get("handle")
    if raw is None:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: handle is required",
            {"reason": media_handles.REASON_HANDLE_INVALID},
        )

    # The grammar runs against a scope that is only derived once the argument
    # has passed it, so a malformed handle costs no scan at all. That ordering
    # is also what keeps a caller from using this verb as a way to make the
    # server hash its disk.
    if not isinstance(raw, str) or not media_handles.HANDLE_RE.match(raw.strip()):
        return err(
            rid,
            ERR_HANDLER_FAILED,
            "runtime.media.get names an artifact by handle "
            "(sha256:<64 hex>); it does not accept a path",
            {"reason": media_handles.REASON_HANDLE_INVALID},
        )

    scope = media_handles.build_media_scope()
    resolved = media_handles.resolve_handle(raw, scope)
    if isinstance(resolved, media_handles.MediaRefusal):
        return err(
            rid,
            ERR_HANDLER_FAILED,
            f"runtime.media.get refused: {resolved.reason}",
            resolved.refusal_data(),
        )

    if isinstance(resolved, media_handles.RemoteMediaArtifact):
        # Stage P4's proxy arm. The client asked THIS install and this install
        # answers — it simply has to spend a peer edge to do it. Nothing about
        # the reply's shape changes, which is the point: a device cannot tell a
        # proxied artifact from a local one and has nothing to do differently
        # if it could.
        from agent_runtime import media_proxy

        data = media_proxy.fetch_remote_artifact(resolved)
    else:
        data = media_handles.read_artifact_bytes(resolved)
    if isinstance(data, media_handles.MediaRefusal):
        return err(
            rid,
            ERR_HANDLER_FAILED,
            f"runtime.media.get refused: {data.reason}",
            data.refusal_data(),
        )

    result: dict[str, Any] = {
        "contract": MEDIA_CONTRACT,
        "handle": resolved.handle,
        "media_type": resolved.media_type,
        "size_bytes": len(data),
        "encoding": "base64",
        "data": base64.b64encode(data).decode("ascii"),
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return ok(rid, result)


# ── peer.ping ────────────────────────────────────────────────────────────────
#
# Gateway Stage 6. The FIRST method outside the ``runtime.*`` family, and the
# prefix is the declaration: ``runtime.*`` verbs act on this install's level —
# they read it, mutate it, or run an agent on it — while ``peer.*`` verbs are
# about the EDGE between two installs and touch no level at all. A client
# reading the manifest can therefore tell the two apart without a table, which
# matters more here than usual because the peer surface is the one an operator
# on another machine is being asked to trust.


#: The peer lane's own shape number, and a THIRD beside ``RPC_CONTRACT_VERSION``
#: (this manifest's) and the two handshake ones. It describes the ``peer.ping``
#: RESULT and nothing else, so a Stage 7 that grows the peer surface can move it
#: without telling every ``runtime.*`` client that something changed.
PEER_PING_CONTRACT = 1


@method("peer.ping", tier=TIER_READ)
def _peer_ping(rid: Any, params: dict, context: RpcContext | None = None) -> dict:
    """Is the edge alive? Answers, and touches nothing.

    Params: ``echo`` (optional, a short opaque string returned verbatim).

    Result::

        {pong: true, contract: 1, peer: <caller's install id | null>,
         at: <iso8601>, echo?: <the string, bounded>}

    **Why the declared tier is ``read`` and why that is not a lie to device or
    console readers.** The ``tiers`` map answers one question — *what strength
    of credential does this verb require* — and the honest answer for a liveness
    ping that reads no store, writes nothing and mints no id is the same answer
    ``runtime.office.get`` gets. A ``console`` declaration would say a level
    mutation's credential is needed, which is false, and would make the map
    lie to exactly the readers it is for: a launcher rendering "what can this
    connection do" would grey out a ping any read-tier device may in fact call.

    What the tier map deliberately does NOT say is who may call this BESIDES
    a credential of that strength — and that asymmetry is already in the
    contract, not invented here. A manifest says what a call WANTS, never what a
    connection HOLDS (canon 03 §2, and the launcher's ``MissionRuntimeRpcManifest``
    branches on nothing). A PEER holds no tier at all: it is answered from
    ``call_authorization.PEER_METHOD_ALLOWLIST``, which contains this name and
    no other, so the peer lane is NARROWED by the allowlist rather than widened
    by this row. Nothing in the map would be more true if this said ``peer`` —
    there is no such tier, ``TIERS`` has two members, and inventing a third word
    that only one caller kind can hold would put a value in the map that every
    existing reader must be taught to ignore.

    So the row reads exactly as it should: any read-tier credential may ping,
    and a peer may ping AND NOTHING ELSE. The second half is the allowlist's to
    state, and it is stated where it is enforced.

    **No store read, and that is a property worth keeping.** The obvious
    temptation is to answer with this install's ``install_id`` — but the
    ``hello_ok`` the caller has already read carries the ``install`` block, so
    repeating it here would be a second authority for one fact, and it would
    make the cheapest verb on the wire open a file. What the result DOES name is
    the caller: ``peer`` is the install id the TRANSPORT proved, echoed back so
    the dialer can confirm it was recognised as the install it meant to be —
    which is a real answer to "is my credential still the one you know me by",
    and one no client-side check can give.
    """

    caller = None if context is None else context.caller
    echo = params.get("echo")
    result: dict[str, Any] = {
        "pong": True,
        "contract": PEER_PING_CONTRACT,
        # ``None`` for a non-peer caller (a console client or a device may call
        # this too — see the tier note above), and the key is present either way
        # so a client never has to branch on absence to read it.
        "peer": None if caller is None else caller.peer_install_id,
        "at": _now_iso(None),
    }
    if isinstance(echo, str) and echo.strip():
        # Bounded, because it comes off the wire and goes straight back onto it.
        # An echo is a correlation aid, never a channel: 128 characters is more
        # than any token this repo mints and less than anything worth smuggling.
        result["echo"] = echo.strip()[:128]
    return ok(rid, result)


# ── peer.agent_chat.execute ──────────────────────────────────────────────────
#
# Gateway Stage 7. The second verb on the peer surface, and the first one that
# DOES something: an agent on a paired install asks an agent on this one to take
# a turn. The row that remembers the ask lives on the SENDER's install; what
# lands here is one turn, executed and recorded in this install's own chat store
# exactly as any inbound agent message is, so this operator sees it too.


#: This install's ``data.reason`` for "you are not a peer". Its own value rather
#: than ``scope_denied``, because the two are different facts: ``scope_denied``
#: is the chokepoint saying a caller may not run a verb, this is the verb saying
#: it has no provenance to run under. A console client that calls this by
#: mistake should read the second, not the first.
PEER_CHAT_NOT_A_PEER_REASON = "peer_identity_required"


@method("peer.agent_chat.execute", tier=TIER_CONSOLE)
def _peer_agent_chat_execute(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Run ONE chat turn on this install, asked for by a paired install.

    Params: ``turn_request_id``, ``target``, ``message`` (required);
    ``session_id``, ``title``, ``new_session``, ``max_seconds``,
    ``correlation_id`` (optional).

    Result: :func:`runtime.chat.message`'s ack, plus ``peer`` — the install id
    THIS server proved about the caller, echoed for the same reason
    ``peer.ping`` echoes it.

    **Attribution comes off the CONNECTION, and this handler is where that is
    enforced.** ``context.caller.peer_install_id`` is set by
    ``call_authorization.caller_for_connection`` only for a connection whose
    peer HMAC verified against a row in ``gateway/peers.json``; it is not
    readable from, or writable by, anything in ``params``. A caller that reaches
    here without one is REFUSED rather than defaulted — including a perfectly
    legitimate local console client, which is the case that makes the refusal
    worth spelling: a turn run under "some console asked" would be a turn whose
    provenance nobody can audit, and the local console already has
    ``runtime.chat.message`` for turns of its own.

    **The tier says ``console`` and the tier is not what admits a peer.** It is
    the honest answer to what the map asks — *what strength of credential does
    this verb want* — and it is the same answer ``runtime.chat.message`` gives
    for the same reason: a chat turn runs an agent with tools, so anything
    softer would be a door around ``console``. What admits a peer is
    ``call_authorization.PEER_METHOD_ALLOWLIST``, which now names two verbs and
    still names neither ``runtime.agent.create`` nor ``runtime.agent.retire`` —
    canon 06's exclusion, holding by construction rather than by anybody
    remembering it.

    **The ack is an ACCEPT.** Stage 3's constraint, unchanged and inherited: this
    dispatcher answers inline on the reader loop, so the turn goes to the worker
    pool and its frames ride the per-request lane under ``request_id``. The
    dialling install reads those frames exactly as the local launcher does. That
    is what makes a remote turn and a local turn one execution rather than two
    implementations that agree today.
    """

    caller = None if context is None else context.caller
    peer_install_id = None if caller is None else caller.peer_install_id
    if not peer_install_id:
        return err(
            rid,
            ERR_HANDLER_FAILED,
            "peer.agent_chat.execute runs a turn on behalf of a PAIRED INSTALL, "
            "and this connection proved none; a local client sends chat turns "
            "with runtime.chat.message",
            {"reason": PEER_CHAT_NOT_A_PEER_REASON},
        )

    from agent_runtime.chat_turn import PEER_CHAT_EXECUTE_METHOD, perform_chat_turn

    outcome = perform_chat_turn(
        params,
        verb=PEER_CHAT_EXECUTE_METHOD,
        spawn=None if context is None else context.spawn_chat_turn,
        peer_install_id=peer_install_id,
    )
    if outcome.refusal is not None:
        refusal = outcome.refusal
        return err(rid, refusal.code, refusal.message, refusal.data)
    result = dict(outcome.result or {})
    result["peer"] = peer_install_id
    return ok(rid, result)


# ── peer.media.get ───────────────────────────────────────────────────────────
#
# Stage P4 (ruling R-P3). The third verb on the peer surface and the second one
# that hands anything over. It exists because of an asymmetry Stage 8 built and
# Stage 7 then made visible: install B runs a turn, B's reply declares an image
# on B's disk, and the reply is forged into A's chat — where the picture is a
# path to a machine A cannot read. B minted the handle at reply time (it holds
# the bytes; nobody else can hash them) and the map rode the completion home.
# This verb is the other end: A spends that handle, B answers with the bytes.
#
# It is deliberately the ONLY new door, and it is a keyhole rather than a door:
# no index, no reference, no path, no enumeration. A peer can spend a name it
# was given and can learn nothing else — which is the reference-out/handle-in
# asymmetry of the whole family, applied across an install boundary.


@method("peer.media.get", tier=TIER_CONSOLE)
def _peer_media_get(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """Hand ONE local artifact's bytes to a PAIRED INSTALL, named by handle.

    Params: ``handle`` (required, ``sha256:<64 hex>``); ``correlation_id``
    (optional, echoed).

    Result: :func:`_runtime_media_get`'s, plus ``peer`` — the install id this
    server proved about the caller, echoed for the reason ``peer.ping`` echoes
    it. Refusals are the same family and the same words, because a client that
    had to learn a second refusal vocabulary for the same question would be
    branching on which hop answered.

    **It resolves the LOCAL half of the scope and nothing else, and that is what
    makes the lane acyclic.** A handle this install holds only as a REMOTE row —
    one IT learned from a third install — is ``unknown_handle`` here, not a
    second proxy hop. So there is no chain to bound, no A→B→C fan-out to
    reason about, and no way for two paired installs to bounce a fetch between
    them. Where the bytes are is where the answer comes from.

    **The tier says ``console`` and the tier is not what admits a peer** —
    ``peer.agent_chat.execute``'s note, unchanged. What admits a peer is
    ``call_authorization.PEER_METHOD_ALLOWLIST``, which Stage P4 widened by this
    one name with its reason attached. What the ``console`` word says is that
    handing over raw file bytes wants a level-mutation-strength credential:
    ``read`` is deliberately open to a caller the transport could not place, and
    that is precisely the caller who must not pull files off a disk.

    **A non-peer is REFUSED rather than defaulted**, the same way
    ``peer.agent_chat.execute`` refuses one — including a legitimate local
    console client, which already has ``runtime.media.get`` and gets a strictly
    larger scope from it.
    """

    from agent_runtime import media_handles

    caller = None if context is None else context.caller
    peer_install_id = None if caller is None else caller.peer_install_id
    if not peer_install_id:
        return err(
            rid,
            ERR_HANDLER_FAILED,
            "peer.media.get answers a PAIRED INSTALL, and this connection "
            "proved none; a local client fetches artifacts with "
            "runtime.media.get",
            {"reason": PEER_CHAT_NOT_A_PEER_REASON},
        )

    try:
        correlation_id = _correlation_id_param(params)
    except _CorrelationIdRefused as refused:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            refused.message,
            {"reason": CORRELATION_ID_INVALID_REASON},
        )

    raw = params.get("handle")
    if raw is None:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: handle is required",
            {"reason": media_handles.REASON_HANDLE_INVALID},
        )
    # The grammar first, on the RAW argument, before a scope is derived — the
    # ordering ``runtime.media.get`` states its reason for, and the reason it
    # matters MORE here: this caller is on another machine, so "make the server
    # hash its disk" would be a remote-triggered cost.
    if not isinstance(raw, str) or not media_handles.HANDLE_RE.match(raw.strip()):
        return err(
            rid,
            ERR_HANDLER_FAILED,
            "peer.media.get names an artifact by handle "
            "(sha256:<64 hex>); it does not accept a path",
            {"reason": media_handles.REASON_HANDLE_INVALID},
        )

    # ``remote_completions=()`` is the acyclicity, spelled as an argument rather
    # than trusted to a later check: the scope this verb resolves against simply
    # does not contain the remote half.
    scope = media_handles.build_media_scope(remote_completions=())
    resolved = media_handles.resolve_handle(raw, scope)
    if isinstance(resolved, media_handles.MediaRefusal):
        return err(
            rid,
            ERR_HANDLER_FAILED,
            f"peer.media.get refused: {resolved.reason}",
            resolved.refusal_data(),
        )

    data = media_handles.read_artifact_bytes(resolved)
    if isinstance(data, media_handles.MediaRefusal):
        return err(
            rid,
            ERR_HANDLER_FAILED,
            f"peer.media.get refused: {data.reason}",
            data.refusal_data(),
        )

    result: dict[str, Any] = {
        "contract": MEDIA_CONTRACT,
        "handle": resolved.handle,
        "media_type": resolved.media_type,
        "size_bytes": len(data),
        "encoding": "base64",
        "data": base64.b64encode(data).decode("ascii"),
        "peer": peer_install_id,
    }
    if correlation_id is not None:
        result["correlation_id"] = correlation_id
    return ok(rid, result)
