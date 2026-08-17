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
``data.reason`` is the branch point (see :func:`err`). 4090 carries THREE
reasons, and conflating any two of them would be the whole bug, because each
has a different cure:

``stale_revision``
    The client's own prediction is behind. Refetch and rebase.
``sync_conflict``
    A realm-sync sidecar is unresolved. NO amount of refetch-and-retry clears
    it — it needs an operator running ``harness office actor-resolve``. A
    client that retried this one would spin forever.
``class_key_collision``
    The write is class-keyed and would undo the class→instance re-key
    migration (``office_class_key_guard``). Neither refetching nor retrying
    helps; the client must name WHICH instance it is placing.

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
is about.

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

from dataclasses import dataclass
from typing import Any, Callable

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
    had no way to name the connection it would later push to. This is that
    missing argument, and it is deliberately the ONLY new one.

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
    """

    connection_key: str | None = None
    transport: str = "stdio"
    emit: Callable[[dict], None] | None = None

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


def method(name: str):
    def dec(fn):
        _METHODS[name] = fn
        return fn

    return dec


def method_names() -> list[str]:
    return sorted(_METHODS)


def manifest() -> dict[str, Any]:
    """What this runtime's method lane offers, for the greeting frames.

    Rides ``ready`` / ``hello_ok`` / the ``version`` reply. A client reads it
    once and knows both which methods exist and whether it understands their
    shape; a runtime that predates the lane carries no ``rpc`` block at all,
    which reads as "argv only" rather than as an error.
    """

    return {"contract": RPC_CONTRACT_VERSION, "methods": method_names()}


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


@method("runtime.office.get")
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
    are excluded outright: ``list_actors`` without ``include_archived`` is the
    placement set the canvas draws.

    Items are flattened out of their actor files in ``(actor_key, file order)``
    — ``list_actors`` sorts by ``actor_key`` — so the same store state produces
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
    from agent_runtime.office_store import OfficeStore
    from agent_runtime.serde import to_jsonable
    from agent_runtime.snapshot import MAX_OFFICE_ACTORS_PROJECTED

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
        return None

    surface = store.get_surface(workspace_id)
    actors = store.list_actors(workspace_id)
    projected = actors[:MAX_OFFICE_ACTORS_PROJECTED]

    items = [
        {
            "item_id": item.item_id,
            "kind": item.kind,
            "persona_id": item.persona_id,
            # The actor's binding, repeated onto each of its items because the
            # wire shape is flat. Explicit ``None`` for a class-keyed actor —
            # NEVER an omitted key, the same rule desks already follow for
            # ``display_name`` / ``pet_slug``: a client decoding into a typed
            # struct must not have to special-case which keys exist.
            "persona_instance_id": actor.persona_instance_id,
            # The ACTOR's revision, likewise repeated onto each of its items:
            # the concurrency token ``runtime.office.upsert``'s
            # ``expect_revision`` is checked against. NOT the ``revision``
            # beside ``folders`` above — that one is the SURFACE's and does not
            # move when an actor moves.
            "revision": actor.revision,
            "folder": item.folder,
            "position": [float(item.position[0]), float(item.position[1])],
            "scale": float(item.scale),
            "display_name": item.display_name,
            "pet_slug": item.pet_slug,
        }
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
    }


@method("runtime.office.subscribe")
def _runtime_office_subscribe(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """The baseline AND the registration, in one call. The push leg's keystone.

    Params: ``workspace_id`` (required), ``fold_entities`` (optional list of
    strings — what THIS client can fold).

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
    """

    from agent_runtime.locks import office_lock
    from agent_runtime.parity import events_watermark
    from agent_runtime.serve_office_subscriptions import (
        NO_PUSH_LANE,
        OFFICE_FOLD_ENTITIES,
        OFFICE_SUBSCRIPTIONS,
        PUSH_LANE_DRAINING,
        normalize_office_fold_entities,
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
        try:
            baseline_offset = int(events_watermark().get("event_offset") or 0)
        except (TypeError, ValueError):
            baseline_offset = 0
        outcome = OFFICE_SUBSCRIPTIONS.subscribe(
            connection_key=context.connection_key,
            workspace_id=workspace_id,
            baseline_offset=baseline_offset,
            emit=context.emit,
            fold_entities=fold_entities,
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


@method("runtime.office.unsubscribe")
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


@method("runtime.office.upsert")
def _runtime_office_upsert(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE actor placement, written — and acked LIGHT so a drag can predict.

    Params: ``workspace_id`` (required), ``actor`` (required object — the same
    identity-triple-plus-items payload ``harness office actor-upsert`` takes on
    ``--actor-json``, deliberately not a second schema), ``expect_revision``
    (optional int) and ``updated_by`` (optional string, defaults to the argv
    lane's own ``operator``).

    Result: ``{"actor_key", "revision"}``. See the module docstring for why it
    is those two and not the actor.

    Why this method REFUSES an unknown workspace instead of authoring one
    ---------------------------------------------------------------------
    ``OfficeStore.upsert_actor`` calls ``ensure_surface`` and would happily
    lazily create the office, which is right for the CLI: a human typed
    ``--workspace`` and can see what they made. This lane's caller is the same
    program that just called ``runtime.office.get``, which REFUSES an unknown
    workspace so a typo cannot render as a blank canvas. A pair where the read
    refuses a typo and the write silently authors a whole new office for it is
    incoherent, and the write side is the worse half — a mis-rendered canvas is
    repainted on the next poll, a mis-authored one is on disk forever. So the
    existence check happens HERE, before the store's own lazy create, and the
    surface-authoring path stays where it already works: the argv lane.

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
    ``office_class_key_guard`` is called before the store, for the reason that
    module gives: ``upsert_actor`` reads an explicit upsert of an ARCHIVED key
    as operator intent to re-add and clears the resurrection ledger, so one
    surviving class-keyed write undoes the class→instance re-key and places the
    same agent twice. This method is the third writer through that hole and the
    only one reachable from the network.

    The CLI verb beside it takes ``--allow-class-key``. This one takes no
    equivalent, and that asymmetry is the point rather than an omission. The
    flag is consent: an operator read the refusal, typed the override, and owns
    the double placement. A wire PARAMETER is not consent — it is a constant in
    a client build, set once by whoever was debugging the day drags started
    failing, and thereafter sent by every install on every write with no human
    in any loop. And the wire client needs it least: the read projection hands
    it ``persona_instance_id`` on every item, so its remedy is to send back the
    binding it was already given. The genuine operator-intent paths are
    untouched — ``harness office actor-restore``, and the CLI's own override.
    """

    from agent_runtime.errors import StaleRevision, SyncConflict
    from agent_runtime.office_class_key_guard import class_key_collision
    from agent_runtime.office_store import OfficeStore

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

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
        return err(
            rid,
            ERR_NOT_FOUND,
            f"unknown workspace: {workspace_id}",
            {"reason": "workspace_not_found", "workspace_id": workspace_id},
        )

    collision = class_key_collision(store, workspace_id, actor_payload)
    if collision is not None:
        # Its own reason: the remedy is neither "refetch and rebase" nor "fix
        # the payload shape" — the payload is well-formed and the client is not
        # behind. It is "name WHICH instance you are placing". The guard's two
        # narrow reasons ride beside it as a list rather than as the branch
        # point, because they share that one remedy and because a client
        # decoder switches on a single stable string.
        #
        # ``refusal_message`` is deliberately NOT reused: it ends by offering
        # ``--allow-class-key``, which is a CLI flag that does not exist on this
        # lane. Advice a caller cannot follow is worse than none.
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

    try:
        actor = store.upsert_actor(
            workspace_id,
            actor_payload,
            updated_by=updated_by or "operator",
            expect_revision=expect_revision,
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

    # Light, and both fields are things the caller could not have computed.
    return ok(rid, {"actor_key": actor.actor_key, "revision": actor.revision})


# ── runtime.agent.create ─────────────────────────────────────────────────────


@method("runtime.agent.create")
def _runtime_agent_create(
    rid: Any, params: dict, context: RpcContext | None = None
) -> dict:
    """ONE call places an agent: roster row, chat root and placement together.

    Params: ``persona_id``, ``workspace_id``, ``position: [x, y]`` and
    ``idempotency_key`` (all required); ``display_name``, ``placement_id``,
    ``realm_id``, ``folder``, ``correlation_id`` (all optional).

    Result::

        {persona_instance_id, persona_id, placement_id, display_name,
         default_chat_session_id, actor_key, revision, workspace_id,
         phases: {instance_ms, placement_ms, total_ms}, idempotent_replay}

    ``actor_key``/``revision`` mirror ``runtime.office.upsert``'s light ack on
    purpose, so the launcher's existing prediction and ``expect_revision``
    bookkeeping keeps working with no new decoder.

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

    outcome = perform_agent_create(params)
    if outcome.refusal is not None:
        refusal = outcome.refusal
        return err(rid, refusal.code, refusal.message, refusal.data)
    return ok(rid, outcome.result)
