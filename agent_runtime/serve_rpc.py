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
===========  ===========================================================

``4001`` is reused verbatim rather than minted: upstream already spends it on
exactly this meaning, and a fork that renumbers "not found" makes a future
union of the two dispatchers a translation layer instead of a merge.

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
"""

from __future__ import annotations

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

_METHODS: dict[str, Callable[[Any, dict], dict]] = {}


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


def handle_request(req: Any) -> dict:
    """Answer one JSON-RPC request. Always returns a frame — never raises.

    A handler that raises becomes ``-32000`` rather than escaping into the
    serve reader loop: this lane shares a thread with the transport dispatcher,
    and a read method is not permitted to take a durable service down.
    """

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
        return fn(rid, params)
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
def _runtime_office_get(rid: Any, params: dict) -> dict:
    """ONE workspace's office projection — canvas-shaped, and bounded.

    Carries per item: ``item_id``, ``kind``, ``persona_id`` (the character-class
    POINTER), ``persona_instance_id`` (the owning actor's IDENTITY binding, or
    ``null`` when the actor is class-keyed), ``folder``, ``position``, ``scale``,
    ``display_name``, ``pet_slug``; plus surface-level ``folders``, ``revision``,
    ``updated_at``.

    ``persona_instance_id`` is the actor's, not the item's — an actor file is
    the binding unit (all of one agent's placements plus its coupled desk live
    in it), so every item flattened out of one actor carries the same value.
    It is NOT derivable client-side: an ``item_id`` such as
    ``personainst_qa_agent_9c8a382f`` is instance-SHAPED but is an item id, and
    reading it as a binding would invent an instance for a class-keyed actor.
    Every live actor today is class-keyed, so this is ``null`` across the board;
    the field exists so that stops being invisible.

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

    from agent_runtime import paths  # noqa: F401 - store_root side of the read
    from agent_runtime.office_store import OfficeStore
    from agent_runtime.serde import to_jsonable
    from agent_runtime.snapshot import MAX_OFFICE_ACTORS_PROJECTED

    workspace_id = _workspace_id_param(params)
    if workspace_id is None:
        return err(
            rid,
            ERR_INVALID_PARAMS,
            "invalid params: workspace_id must be a non-empty string",
            {"reason": "workspace_id_required"},
        )

    store = OfficeStore()
    if not store.surface_exists(workspace_id):
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
            "folder": item.folder,
            "position": [float(item.position[0]), float(item.position[1])],
            "scale": float(item.scale),
            "display_name": item.display_name,
            "pet_slug": item.pet_slug,
        }
        for actor in projected
        for item in actor.items
    ]

    return ok(
        rid,
        {
            "workspace_id": surface.workspace_id,
            "folders": list(surface.folders),
            "revision": surface.revision,
            "updated_at": to_jsonable(surface.updated_at),
            "items": items,
            # Accounted, never silent. Zero on every real workspace today; a
            # cut that read as a smaller office would be indistinguishable
            # from actors having been removed.
            "actors_truncated": max(0, len(actors) - len(projected)),
        },
    )
