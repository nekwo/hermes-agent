"""``hermes harness serve --ndjson`` — persistent stdio bridge (schema v1).

One warm process replaces the per-call CLI spawns the Launcher Mission
Control bridge pays ~3s import tax on today. Requests dispatch into the
EXISTING harness argparse tree and ``_cmd_*`` handlers, unchanged — argv
arrives verbatim as the bridge already builds it, so intent→argv mapping,
the capability registry, and the per-call CLI fallback stay byte-identical.

Design doc: ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/harness-serve-design.md``
(settled 2026-07-08). Explicit non-goals: no network listener, not the
mission daemon, no second chat pipeline. "No auth (a local stdio child IS
the security model)" held while the transport was an inherited pipe; the
durable runtime-root service replaces that pipe with one any local process
can reach, so a per-root token is now minted at boot (unwired — see
``agent_runtime/serve_auth.py``) rather than retrofitted after the socket
exists.

Protocol (NDJSON, one frame per line):

- boot:      ``{"event":"ready","pid":…,"schema_version":1,"runtime_root":…}``
             plus the durable-service foundations, all additive:
             ``"build"`` (which commit this runtime is on —
             ``agent_runtime/build_stamp.py``), ``"auth"``
             (``{"token_file":"present"|"minted"|"error:<reason>"}`` — the
             posture, NEVER the token itself), and ``"instance"`` (this
             serve's registry entry under ``<store_root>/serve_instances/``).
             It also carries the two CAPABILITY advertisements — ``"rpc"``
             (``serve_rpc.manifest()``) and ``"ops"``
             (:func:`ops_manifest`) — so a client learns the method set AND the
             op set from the greeting it already reads, instead of probing.
- request:   ``{"id":"req-7","argv":["harness","status","--json"]}``
- reply:     ``{"id":"req-7","event":"line","line":…}`` × N then
             ``{"id":"req-7","event":"exit","code":0}``
             (a status/snapshot poll replayed from the poll response cache
             adds ``"served_from_cache": true, "cache_age_ms": N`` to its exit
             frame — additive; see _PollResponseCache below)
- stderr:    ``{"id":<request id or null>,"event":"stderr","line":…}``
- progress:  ``{"id":"req-7","event":"request_progress","state":"queued"|
             "running","waited_ms":N,"running_ms":N,"pending":M,
             "pool_size":P}`` — UNSOLICITED, on the lane that asked, for a
             request that has produced nothing for
             ``_REQUEST_SILENCE_SECONDS``. A request's first frame is written
             by its HANDLER, so before the fix "queued behind a full pool",
             "slow handler" and "wedged" were one silence; ``state`` separates
             the first from the other two, and it is the field that says
             whether a retry is free. Additive and never emitted on the normal
             path — a client that does not know the event ignores it and
             still reads its ``line``/``exit`` frames unchanged.
- ping:      ``{"op":"ping"}`` → ``{"event":"busy","chat_turns":N,"pending":M}``
             (the Launcher supervisor must NEVER recycle serve while
             ``chat_turns`` > 0 — recording safety). The SAME frame is pushed
             unsolicited by the liveness pump while work is in flight, to
             stdout AND to every attached socket/gateway client.
- shutdown:  ``{"op":"shutdown"}`` → drain in-flight requests, exit 0
- version:   ``{"op":"version"}`` → ``{"event":"version","build":{…},
             "runtime_root":…,"boot_id":…,"transport":"stdio","auth":{…}}``
             — the SAME stamp the ready frame carried, re-askable at any
             time. A durable service outlives the install it was started
             from; this is how a client proves it is not talking to last
             week's code. Both advertisements (``"rpc"`` and ``"ops"``) are
             restated here, and ``"ops"`` is answered for the transport the
             ask arrived on.
- drain:     ``{"op":"drain"[,"deadline_seconds":30][,"force":true]}`` → stop
             accepting new requests (each is answered
             ``{"id":…,"event":"draining",…}`` and a terminal ``exit`` frame
             with code 75), let in-flight requests finish, then
             ``{"event":"drain_complete","requests_refused":N,
             "requests_completed":M,"drain_ms":X}`` and exit 0. If the
             deadline elapses first: ``{"event":"drain_timeout",…,
             "stuck_request_ids":[…],"held_by_chat_turns":N,
             "held_by_long_runs":N,"terminal":true}``
             and a NONZERO exit — a drain that can hang forever is not a drain.
             Progress is reported as ``{"event":"drain_progress",…}`` while the
             wait runs, so a draining service never looks dead to a watchdog.

             THE DEADLINE IS THE SERVER'S, and so is the kill. Two rules make
             it so, and both exist because `drain` on the socket is reachable
             by any local process holding the root's secret while `shutdown` is
             refused there on purpose:

             * ON THE SOCKET the effective deadline is ``max(client ask,
               server minimum)`` — a socket client could previously ask for
               0.05s, which turned the restart verb into a kill
               (`hard_exit(3)` is `os._exit`) over a live chat turn, straight
               through the never-recycle-during-turns contract this file opens
               with. Over STDIO the ask still stands as given: that asker is
               the parent that spawned this process and owns its stdin, so it
               can end the runtime with a signal regardless, and flooring it
               would change a contract this lane promised to leave untouched;
             * a deadline that expires WHILE A CHAT TURN OR A LONG RUN IS IN
               FLIGHT does not end the process. It emits a non-terminal
               ``{"event":"drain_timeout","terminal":false,
               "held_by_chat_turns":N,"held_by_long_runs":N,…}``, keeps serving,
               and re-arms. Only an expiry with neither in flight is terminal.
               Recording safety outranks restart latency: a killed turn is lost
               work, a late restart is a slow one.

               The long runs are the ``characters turnaround|rows|auto`` verbs
               (``_LONG_RUN_COMMANDS``), and they are here on the same argument
               a chat turn is: a launcher update or a Reap & Restart landing
               mid-generation used to end a ten-to-twenty-minute run without a
               word, and a ``turnaround`` has no partial result to resume from.
               Both counts are reported separately because the WAIT differs —
               a chat turn ends in seconds, a generation may be fifteen minutes
               from done — and ``held_by_chat_turns`` keeps its name and its
               meaning so a reader that only knows that key is never lied to.

             On the SOCKET lane `drain` additionally requires ``"force":true``.
             Same reasoning as the `shutdown` refusal — an attached client
             asking to replace a service other clients are using should have to
             say so explicitly — and one flag is a trivial cost for the
             operator verb (`harness serve connect --drain` sets it). The
             refusal is typed: ``{"event":"error","error":"drain_requires_force"}``.
- cancel:    ``{"op":"cancel","id":"req-7"}`` → a QUEUED request is dropped
             and answers ``{"id":"req-7","event":"exit","code":130,
             "cancelled":true}``; a request already RUNNING (or unknown)
             answers ``{"id":…,"event":"cancel_denied","state":
             "running"|"unknown"}`` — its side effects may still happen, so
             mutation verbs carry their own replay guard (``--issued-at``).
             A RUNNING read-only ``harness stream`` is cooperatively cancelled
             and releases its pool worker; it is the sole running exception.
- errors:    ``{"id":…,"event":"error","error":"invalid_request"|…,"detail":…}``
- method:    ``{"jsonrpc":"2.0","id":…,"method":"runtime.office.get"|…,
             "params":{…}}`` → ``{"jsonrpc":"2.0","id":…,"result":{…}}`` or
             ``{"jsonrpc":"2.0","id":…,"error":{"code":…,"message":…,"data":…}}``.

             The method name above is ONE example, deliberately not a list:
             the ``@method`` registry in ``agent_runtime/serve_rpc.py`` is the
             authority for the advertised set (count it there). This block
             used to name ``get`` | ``upsert`` and stayed at two while the
             registry grew — a docstring that copies a register starts lying
             the first time the register moves, and nothing reports it.

             The CALL half (``agent_runtime/serve_rpc.py``), mirroring
             ``tui_gateway``'s JSON-RPC 2.0 shape and its error codes rather
             than minting a third convention. It sits BESIDE the argv lane
             above, which is unchanged and remains the fallback: a frame is
             claimed by this lane only when it names ``jsonrpc`` or ``method``,
             neither of which an argv request has ever carried.

             HOW A CLIENT LEARNS THE SURFACE — the ``hello_contract``
             precedent, not a parallel scheme. ``{"contract":N,"methods":[…]}``
             rides the greeting each transport already reads (``ready`` on
             stdio, ``hello_ok`` on the socket) under ``"rpc"``, and is
             restated on the re-askable ``version`` reply because a durable
             service outlives the install it was started from. The manifest is
             a SET plus an integer: the integer moves when an existing
             method's shape changes incompatibly, the set grows when a method
             is added — so methods can be adopted one at a time, exactly as
             ``fold_entities`` does for patch entities. A runtime that
             predates the lane carries no ``rpc`` key, which reads as "argv
             only" rather than as a failure.

Per-request stdout: handlers ``print()`` directly and streaming turns emit
deltas live, so ``sys.stdout``/``sys.stderr`` are swapped once for
contextvar-dispatching proxies; each pool worker binds its request id and a
single write lock keeps frames atomic. Writes from threads a handler spawns
itself carry no request id and are forwarded with ``"id": null``.

The socket lane (slice 3)
-------------------------

``serve_loop`` is now transport-agnostic: ONE dispatcher answers ops arriving
on stdio and on a localhost socket alike. Everything above this line is
unchanged on stdio — every frame, reply, and exit code is byte-identical,
because the socket lane is injected and OFF unless ``_cmd_serve`` turns it on.

- ownership: one serve per root owns the socket, decided by an OS-held
  exclusive lock (``agent_runtime/serve_socket.py``). The loser runs
  stdio-only and says so on ``ready`` under ``"socket"``:
  ``{"outcome":"lock_held_by","pid":…}``. The winner's ``ready`` carries
  ``{"outcome":"listening","host":"127.0.0.1","port":…}`` and its registry
  entry records ``transport:"stdio+socket"`` plus the port.
- hello:     CHALLENGE-RESPONSE, and the SERVER speaks first
             (``hello_contract`` 3). On accept the service writes
             ``{"event":"server_hello","nonce":<64 hex>,"boot_id":…,
             "contract":1,"hello_contract":3,"algorithm":"hmac-sha256"}`` and
             the client answers
             ``{"op":"hello","client":…,"client_build":…,"proof":<hex>}`` where
             the proof is ``HMAC-SHA256(key=<per-root token>,
             msg="v3|<the port the client DIALLED>|<nonce>")`` —
             ``serve_socket.hello_proof`` is the authority, and this line said
             ``2`` and ``msg=<nonce>`` until 2026-08-27, which is wrong twice
             over: a client written against it is refused with ``bad_proof``,
             indistinguishable from holding the wrong credential.
             Success → ``{"event":"hello_ok","build":{…},"boot_id":…,
             "contract":1,"hello_contract":3,"build_mismatch":true|false|null}``;
             failure → ONE ``{"event":"hello_rejected","reason":…}`` and the
             connection is closed, with a rate limit against hammering. Before
             that proof is verified a connection can do NOTHING.

             THE TOKEN NEVER TRAVELS. It is the HMAC key, never a field, so it
             appears in no frame, log, error, or registry entry on either side,
             and a captured transcript is unreplayable (fresh nonce per
             connection). The first cut sent the raw token and paired that with
             a discovery fallback that could hand a client a target already
             classified ``stale_dead_pid`` — an impostor on a dead serve's port
             harvested the real token, live-proven. There is deliberately no
             compatibility shim for the old hello: it has no other clients yet,
             and a shim would keep the cleartext lane open forever.

             Rejection reasons are typed and mean different things:
             ``bad_proof`` / ``hello_required`` / ``hello_malformed`` are
             AUTHENTICATION failures and are the only ones that charge the rate
             limiter; ``too_many_connections`` / ``too_many_pending`` /
             ``draining`` / ``hello_timeout`` / ``rate_limited`` /
             ``handshake_throttled`` describe the SERVER's state and never do —
             charging them made a blocked window extend itself forever, so a
             client with the right credential could not recover.
- subscribe: ``{"op":"subscribe","lane":"stream"}`` pushes the SAME hydrate /
             delta / heartbeat frames ``harness stream`` produces, from ONE
             shared producer fanned out to every subscriber (a per-batch
             snapshot rebuild is why it is not one generator per client). A
             subscriber that outruns its bounded buffer gets
             ``{"event":"subscription_dropped","reason":"backpressure",…}`` and
             is unsubscribed — never silently stalled, and never able to wedge
             the producer or another subscriber. ``{"op":"unsubscribe"}`` ends
             it cleanly, and so does a disconnect.
             An optional ``"fold_entities":["persona_instance",…]`` declares
             which entity classes THIS client can fold in place; the producer
             promotes a coalesced batch to a small ``patch`` frame only for
             declared entities and demotes anything else to the full core it
             would have sent anyway. Omitting it means the historical
             ``{persona_instance, incident}`` — exactly today's wire, so an
             un-updated client is unaffected. The producer is SHARED, so the
             ACCEPTED set is the intersection over every attached subscriber and
             is echoed on the ``subscribed`` ack (and on the hydrate) rather than
             left for the client to assume. A malformed declaration is refused
             with ``{"event":"subscribe_denied","reason":"invalid_fold_entities"}``
             instead of being silently read as absent.
             The frames a subscriber receives are the argv stream's frames,
             not a second contract: byte parity against
             ``harness stream --fold-entities …`` over one seeded root and one
             scripted event sequence is a contract test
             (``tests/agent_runtime/test_serve_stream_lane_parity.py``).
             Whether THIS runtime carries the lane is answerable before
             subscribing — see ``"ops"`` / :func:`ops_manifest`.
- connections: ``{"op":"connections"}`` → ``{"event":"socket_connections",…}``
             (count, and per client: name, build, subscribed, connected_at,
             frames and bytes pushed). The same block rides the ``version``
             reply, so "who is attached to this runtime" is answerable from the
             handshake a client already performs.

Client disconnect unsubscribes and does NOTHING else: the backend state a
client was watching is the runtime's, not the client's, and surviving a client
is the entire point of the durable service.
"""

from __future__ import annotations

import argparse
import contextvars
import inspect
import io
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TextIO

SERVE_SCHEMA_VERSION = 1
DEFAULT_POOL_SIZE = 4

#: The NAMED boot instant at which this serve captures the core cache's
#: fingerprint home (HC-1). A constant rather than a literal at the call site
#: because it is a two-ended contract: it is what
#: ``core_cache.RECEIPT_FINGERPRINT_HOME_LAZY_CAPTURE`` prints as ``site=`` when
#: the capture did NOT happen here, and it is what the regression pin asserts the
#: capture instant to be. The spelling names the frame it follows, so a boot log
#: and the receipt can be read against each other without consulting this file.
FINGERPRINT_HOME_BOOT_SITE = "serve_loop:booting_frame_emitted"

# ── The OP lane's advertisement (TC-1/C-1) ───────────────────────────────────
#
# The METHOD lane has been discoverable since ``serve_rpc.manifest()`` started
# riding ``ready``/``hello_ok``/``version`` under ``"rpc"``: a client reads the
# greeting it already reads and learns which methods exist. The OP lane — the
# ``{"op":…}`` verbs this file dispatches, ``subscribe`` among them — was NOT
# discoverable at all, so a client could only learn whether this runtime carries
# the push lane by sending a subscribe and interpreting whatever came back. That
# is a probe, and a probe cannot distinguish "this runtime is too old" from "this
# runtime refused THIS subscribe" (`unsupported_lane`, `draining`,
# `already_subscribed` are all real answers a live lane gives).
#
# So the ops advertise themselves, under ``"ops"``, beside ``"rpc"``, following
# the method lane's discipline verbatim rather than minting a second scheme:
#
#   * a SET plus an integer. The set grows when an op is added — a client only
#     ever sends an op it FOUND — and the integer moves only when an existing
#     op's shape changes incompatibly. Adding ``subscribe`` to the advertisement
#     does not move ``SERVE_SCHEMA_VERSION``, ``RPC_CONTRACT_VERSION``, or this
#     module's own integer, exactly as the eighth RPC method did not move the
#     method lane's;
#   * a runtime that predates the advertisement carries no ``"ops"`` key, which
#     reads as "ops undiscoverable, probe if you must" rather than as a failure;
#   * PER TRANSPORT, because the answer genuinely differs. ``shutdown`` is the
#     stdio owner's verb and is refused on the socket
#     (``op_not_available_on_socket``), so advertising it to a socket client
#     would be a false all-clear of the exact kind the build stamp beside it
#     exists to retire. The block names the transport it describes so a client
#     that cached it cannot mis-apply it to the other lane.
#
# A THIRD block rides the same three frames, and it is not a manifest: ``install``
# (``{install_id, display_name, state}``, from
# ``agent_runtime.gateway_identity``) names WHICH runtime a client reached, where
# ``rpc``/``ops`` say what it can do. It is on the greeting for the same reason
# they are — a remote client (gateway plan Stage 2+) has no ``runtime_root`` path
# it can interpret and no second question it can afford to ask — but it is a pair
# of strings rather than a set plus an integer, so nothing negotiates on it and
# no contract integer moves when it appears. Absent from a runtime that predates
# it; ``state`` says ``error:<reason>`` rather than vanishing when this one could
# not mint. It NAMES; it never authorises (see the module's own docstring).
#
# ``hello`` is deliberately absent from both sets. It is the socket's FIRST line
# and is consumed by ``serve_socket`` before this dispatcher ever sees a frame;
# reaching the dispatcher means it is a SECOND hello, which is answered
# ``unexpected_hello``. Its contract is already advertised — ``hello_contract``
# on ``server_hello`` and restated on ``hello_ok``.
OPS_CONTRACT_VERSION = 1

#: Ops this dispatcher answers on EVERY transport.
OPS_EVERY_TRANSPORT: tuple[str, ...] = (
    "cancel",
    "connections",
    "drain",
    "ping",
    "stacks",
    "subscribe",
    "unsubscribe",
    "version",
)

#: Ops only the process that owns this runtime's stdin may use. See the
#: ``shutdown`` refusal in ``_handle_message``.
OPS_STDIO_ONLY: tuple[str, ...] = ("shutdown",)

#: The transport name the gateway listener tags its connections and frames with.
#: The same string ``call_authorization.TRANSPORT_GATEWAY`` keys its structural
#: guard on; spelled in both places rather than imported across the boundary,
#: for the reason that module gives (it must not import a transport to answer a
#: question about an object it was handed) and pinned equal by a test.
GATEWAY_TRANSPORT = "gateway"

#: Ops a paired DEVICE is refused, on top of the stdio-only set. One entry, and
#: it is the one that ends the process.
#:
#: ``drain`` is not a read and it is not a level mutation — it is the multi-client
#: lifecycle verb, and its whole effect is that this runtime stops and every
#: OTHER attached client is disconnected. A phone deciding that for the desktop
#: it is a guest on is the wrong default even at ``console`` tier, and "the
#: operator wanted to restart the runtime from their phone" is a verb somebody
#: can add deliberately later. The refusal mirrors ``shutdown``'s
#: (``op_not_available_on_socket``) rather than inventing a shape.
OPS_GATEWAY_DENIED: tuple[str, ...] = ("drain",)

#: The push lanes ``{"op":"subscribe","lane":…}`` accepts. ONE today, and the
#: value EG-4.2's launcher gate reads: the argv stream stays the backstop until
#: a runtime says this word, because the launcher must never unilaterally switch
#: onto a lane the runtime it is attached to does not carry.
SUBSCRIBE_LANES: tuple[str, ...] = ("stream",)


def ops_manifest(*, transport: str) -> dict[str, Any]:
    """What this runtime's OP lane offers *transport*, for the greeting frames.

    Rides ``ready`` (stdio), ``hello_ok`` (socket) and the re-askable ``version``
    reply — the same three frames ``serve_rpc.manifest()`` rides, for the same
    reason: a durable service outlives the install it was started from, so "does
    the thing I am attached to carry the push lane" must be answerable at any
    time and not only from a greeting a client read hours ago.
    """

    ops = set(OPS_EVERY_TRANSPORT)
    if transport == "stdio":
        ops |= set(OPS_STDIO_ONLY)
    if transport == GATEWAY_TRANSPORT:
        # The per-transport shape earning its keep a second time. A device
        # learns what it may ask by MEMBERSHIP — the set-plus-integer rule the
        # D12 rollout gate proved — rather than by trying `drain` and reading an
        # error, and the manifest cannot disagree with the dispatcher because
        # both read this tuple.
        ops -= set(OPS_GATEWAY_DENIED)
    return {
        "contract": OPS_CONTRACT_VERSION,
        "transport": transport,
        "ops": sorted(ops),
        "subscribe_lanes": sorted(SUBSCRIBE_LANES),
    }


def _pairing_block(connection: Any) -> dict[str, Any]:
    """Pop the one-shot credentials onto the greeting, or contribute nothing.

    A function rather than an inline expression because the CLEAR has to be
    unconditional and unmissable: a `getattr` that read the token without
    clearing it would leave a secret on a long-lived object for the life of the
    session, and the bug would be invisible until somebody logged a connection.

    Two slots, one function, and they are mutually exclusive by construction —
    a hello redeems a device code or a peer code, never both, because the
    authenticator refuses a frame that names two credentials. Rendered as two
    differently-named blocks (`paired` / `peered`) rather than one with a
    discriminator, so a client that only understands devices cannot mistake a
    peer secret for a device token by reading a field it already knows.
    """

    token = getattr(connection, "pairing_token", None)
    if token:
        connection.pairing_token = None
        return {
            "paired": {
                "device_id": connection.device_id,
                "tier": connection.device_tier,
                # Store it now — this is the only time it is ever sent, and the
                # install itself keeps only a digest of it.
                "device_token": token,
            }
        }
    secret = getattr(connection, "peer_secret", None)
    if secret:
        connection.peer_secret = None
        expires_at = getattr(connection, "peer_secret_expires_at", None)
        # Cleared with the secret and in the same breath: the two are one
        # one-shot fact, and a slot that outlived its secret would be a stale
        # expiry on a long-lived object for the rest of the session.
        connection.peer_secret_expires_at = None
        return {
            # Gateway Stage 6. The joining install writes its own half of the
            # edge from this block; the `install` block on the same frame is
            # what names WHICH install it just paired with, so nothing is
            # repeated here that the greeting already carries.
            "peered": {
                "peer_install_id": connection.peer_install_id,
                # The only time it is ever sent. BOTH installs keep only a
                # digest of it — see ``gateway_peers`` — so a client that drops
                # this frame has paired an edge it can never use.
                "peer_secret": secret,
                # S2 (R-IP15 as amended). ADDITIVE, and ``None`` on every edge
                # the manual ceremony mints, so a joining install that predates
                # this key reads what it always read. It has to travel: the
                # redeeming side computed the stamp and the joining side has no
                # other way to learn it, and an edge whose two ends expire on
                # different days is the divergence ``_row`` exists to prevent.
                "expires_at": expires_at,
            }
        }
    return {}


def _is_gateway(connection: Any) -> bool:
    """Did this frame arrive on the gateway lane — from a device or a peer?

    Keyed on the TRANSPORT and not on the credential stamp, deliberately. The
    stamp answers "which device" or "which install"; this answers "did this come
    through the door that is open to the network", and those are different
    questions whose answers must not be allowed to diverge. A gateway connection
    that somehow lacks a stamp is exactly the case where the narrower test would
    silently grant local authority — the same reasoning, and the same guard, as
    ``call_authorization.caller_for_connection``'s gateway arm.

    Named ``_is_device`` until gateway Stage 6, when the name became false: the
    refusals it guards (`argv_lane_unavailable`, `op_not_available_on_gateway`)
    were always about the DOOR and now genuinely have two kinds of caller behind
    them. The behaviour did not change and neither did the predicate; a peer
    inherits both refusals for free, which is the point of keying on the lane.
    """

    return (
        connection is not None
        and str(getattr(connection, "transport", "") or "") == GATEWAY_TRANSPORT
    )


def gateway_listen_config() -> tuple[str | None, int]:
    """``(host, port)`` from ``remote_gateway.*``; ``(None, …)`` means off.

    The FIRST reader of the keys Stage 0a declared — and the read that found
    they had never existed: Stage 0a put them under ``"gateway"``, which is
    already a top-level key in ``config_defaults``' one big dict literal, so
    Python kept the later entry and dropped this one at parse time. They are
    ``remote_gateway.*`` now, guarded by an AST test.

    ``listen`` is a HOST STRING when it is on, and a boolean ``True`` is
    deliberately refused rather than resolved to a default interface: an
    operator opening a port onto a LAN should have to say which one, and
    "guessed an interface for you" is not a sentence this runtime should be able
    to say about a listener that executes agents with tools. Anything unreadable
    is off, because the failure direction for a config that cannot be parsed is
    "do not bind".
    """

    try:
        from hermes_cli.config import load_config_readonly

        block = load_config_readonly().get("remote_gateway") or {}
    except Exception:
        return None, 0
    if not isinstance(block, dict):
        return None, 0
    listen = block.get("listen")
    if not isinstance(listen, str):
        return None, 0
    host = listen.strip()
    if not host or host.lower() in {"false", "off", "no", "true"}:
        return None, 0
    try:
        port = int(block.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return host, max(0, min(65535, port))


def start_gateway_listener(
    store_root: Any,
    *,
    boot_id: str,
    display_name: Any,
    dispatch_line: Any,
    hello_payload: Any,
    on_disconnect: Any,
    log: Any,
    frame_contract: int,
) -> tuple[Any, dict[str, Any]]:
    """Bind the second listener, or say precisely why there is none.

    Returns ``(server_or_None, block)`` where ``block`` is what rides the
    greeting frames. It follows the ``socket`` block's standing rule — a block
    states its own outcome rather than vanishing — because the failure this
    guards against is specific and quiet: an operator sets ``remote_gateway.listen``,
    restarts, and a phone cannot reach the install. Without a stated outcome
    that looks identical whether the port was taken, the certificate could not be
    minted, or the config was never read at all.

    Module-level rather than a closure inside ``serve_loop`` so it is testable
    without standing up a runtime, and so the credential wiring — the one part
    that must not be got wrong — is readable in one screen instead of inside a
    2000-line function.
    """

    host, port = gateway_listen_config()
    if host is None:
        return None, {"outcome": "disabled"}

    from agent_runtime.gateway_tls import ensure_certificate, server_ssl_context
    from agent_runtime.serve_socket import ServeSocketServer

    certificate = ensure_certificate(
        store_root, common_name=display_name if isinstance(display_name, str) else None
    )
    if not certificate.ok:
        # R1 ruled ENCRYPT, so a listener that cannot encrypt does not open.
        # Degrading to plaintext here would be the single worst thing this file
        # could do: the operator asked for a LAN door, would get one, and the
        # only thing missing would be the property they were promised.
        return None, {
            "outcome": f"error:{certificate.state}",
            "host": host,
            "port": port,
        }
    try:
        context = server_ssl_context(store_root)
    except Exception as exc:
        return None, {
            "outcome": f"error:{type(exc).__name__}",
            "host": host,
            "port": port,
        }

    server = ServeSocketServer(
        store_root,
        boot_id=boot_id,
        dispatch_line=dispatch_line,
        hello_payload=hello_payload,
        # The per-root token is NOT this lane's credential, and the provider is
        # wired to refuse rather than left absent: `token_provider` is a required
        # argument, and one that returned the root token while `authenticator`
        # happened to be set would be a live fallback waiting for a refactor to
        # find it. There is no path on this listener that consults the install's
        # own secret.
        token_provider=lambda: None,
        authenticator=_gateway_authenticator(store_root),
        ssl_context=context,
        host=host,
        port=port,
        transport_name=GATEWAY_TRANSPORT,
        frame_contract=frame_contract,
        on_disconnect=on_disconnect,
        log=log,
    )
    try:
        bound = server.bind()
    except Exception as exc:
        # A port already in use is the ordinary case here, not the exotic one:
        # this lane's port is usually FIXED (an operator wrote a firewall rule
        # for it), so a stale process holding it is a Tuesday. Typed, and never
        # fatal — the loopback lane and the stdio lane are unaffected.
        return None, {
            "outcome": f"error:{type(exc).__name__}",
            "host": host,
            "port": port,
        }
    return server, {
        "outcome": "listening",
        "host": host,
        "port": bound,
        "started_at": server.started_at,
        # The value a pairing payload carries and a client pins. Published on
        # the greeting because a client that has to ask a second question to
        # learn what it should have pinned has a window in which it is trusting
        # nothing — and this is the same argument the `build` block beside it
        # makes about code.
        "cert_fingerprint": certificate.fingerprint,
    }


def _gateway_authenticator(store_root: Any):
    """The gateway lane's credential check, as a ``ServeSocketServer`` seam.

    FOUR hellos reach this function and it is the only place that tells them
    apart: a device credential, a device pairing code, a peer credential, and a
    peer join code. The dispatch is on which FIELD the frame names, and the
    first thing it does is refuse a frame that names more than one — see
    ``_credential_kind`` below, which is where Stage 6's "device-tier and
    peer-tier credentials are never interchangeable" is actually enforced.

    A device names itself in the hello (``device_id``) and answers the challenge
    with an HMAC keyed by its own token's digest, bound to the port it dialled.
    A peer names itself with ``peer_install_id`` and answers with an HMAC keyed
    by the shared verifier, over a message with a different prefix, bound to the
    same port. Every failure of any kind — no id, unknown id, revoked row, wrong
    proof, wrong code, wrong ceremony — comes back as the SAME ``bad_proof``
    rejection, so a caller that has proven nothing cannot enumerate which device
    ids or which paired installs exist by watching the reason change, and cannot
    even learn which of the two ceremonies it just failed. The runtime's own log
    keeps the distinction; the wire does not.

    **The other arm is the pairing ceremony's second half**, and it is here
    rather than in a stage of its own because the alternative is shipping a
    device tier no device can ever enter. A hello carrying ``pairing_code``
    instead of ``device_id`` is a phone that has just been shown eight
    characters on the operator's terminal; the store redeems them under all of
    ``gateway/pairing.py``'s discipline (TTL, pending cap, lockout, constant-time
    compare) and the connection is admitted as the device it just created, with
    the minted token riding the ``hello_ok`` it was going to send anyway.

    Three properties make that safe enough to do in one round trip. The link is
    already TLS with a fingerprint the operator handed over out of band, so the
    token is not readable and not deliverable to an impostor. The code is
    one-shot — redeemed, it is deleted before the token is minted — so a replay
    finds nothing. And a failed redemption collapses into the same ``bad_proof``
    as every other credential failure and charges the same limiter, so the code
    space cannot be ground down any faster than the device-id space can.

    **The peer join (Stage 6) is those same three properties over the same
    machinery**, plus one the device ceremony has no need of: it is the only arm
    that WRITES facts the other side asserted — the joining install's name, its
    endpoints, its certificate fingerprint. They are bounded and cleaned by
    ``gateway_peers`` before they land, and what makes them safe to keep at all
    is R5's second operator: the code was minted seconds earlier by a human at
    THIS machine, which is a stronger provenance than anything the wire could
    supply.
    """

    from agent_runtime.gateway_peers import (
        PeerCredential,
        cache_peer_hello,
        note_peer_seen,
        note_peer_store_read,
        redeem_peer_code,
        verify_peer_proof,
    )
    from agent_runtime.serve_gateway_auth import (
        DeviceCredential,
        note_device_seen,
        redeem_pairing_code,
        verify_device_proof,
    )
    from agent_runtime.serve_socket import HelloAuthOutcome, REJECT_BAD_PROOF

    def _reject():
        return HelloAuthOutcome(ok=False, reject_reason=REJECT_BAD_PROOF)

    def _authenticate(message: dict[str, Any], nonce: str, port: int):
        kind = _credential_kind(message)
        if kind is None:
            # Zero credentials named, or more than one. A handshake with two
            # credentials in it is exactly where a downgrade lives, and the
            # server must not get to pick which one it liked.
            return _reject()

        if kind == "pairing_code":
            outcome = redeem_pairing_code(
                store_root,
                message.get("pairing_code"),
                device_name=message.get("client")
                if isinstance(message.get("client"), str)
                else None,
            )
            if not isinstance(outcome, DeviceCredential):
                return _reject()
            return HelloAuthOutcome(
                ok=True,
                device_id=outcome.device_id,
                device_tier=outcome.tier,
                issued_token=outcome.token,
            )

        if kind == "peer_code":
            # The joining install must NAME itself in the same frame: the edge
            # is symmetric, so a row keyed by nothing would be a peer this
            # install could never dial back and could never recognise again.
            outcome = redeem_peer_code(
                store_root,
                message.get("peer_code"),
                peer_install_id=str(message.get("peer_install_id") or ""),
                display_name=message.get("peer_display_name")
                or message.get("client"),
                endpoints=message.get("peer_endpoints"),
                cert_fingerprint=message.get("peer_cert_fingerprint"),
            )
            if not isinstance(outcome, PeerCredential):
                return _reject()
            return HelloAuthOutcome(
                ok=True,
                peer_install_id=outcome.peer_install_id,
                issued_peer_secret=outcome.secret,
                # S2: whatever the mint decided, carried straight through. The
                # store computed it at redemption; this function neither derives
                # nor defaults one, so the two ends of the edge hold one value.
                issued_peer_secret_expires_at=outcome.expires_at,
            )

        if kind == "peer_install_id":
            # S2c (R-S2-8). The revision read that makes an EXTERNAL write
            # visible, taken on a read this lane was making anyway. The serve is
            # the process that notices because it is the one that reads
            # repeatedly; a fresh CLI process seeds on its first read and emits
            # nothing, having no baseline to claim a change against.
            note_peer_store_read(store_root)
            peer = verify_peer_proof(
                store_root,
                message.get("peer_install_id"),
                message.get("proof"),
                nonce,
                port=port,
            )
            if not peer.ok or peer.record is None:
                return _reject()
            note_peer_seen(store_root, peer.record.peer_install_id)
            # …and the three OPTIONAL facts the hello may carry about itself,
            # after the proof and never before it: these are assertions by a
            # party that has now authenticated, and writing them for a caller
            # that had not would let an unpaired stranger grow this file.
            cache_peer_hello(
                store_root,
                peer.record.peer_install_id,
                display_name=message.get("peer_display_name"),
                endpoints=message.get("peer_endpoints"),
                cert_fingerprint=message.get("peer_cert_fingerprint"),
            )
            return HelloAuthOutcome(
                ok=True, peer_install_id=peer.record.peer_install_id
            )

        auth = verify_device_proof(
            store_root,
            message.get("device_id"),
            message.get("proof"),
            nonce,
            port=port,
        )
        if not auth.ok or auth.record is None:
            return _reject()
        note_device_seen(store_root, auth.record.device_id)
        return HelloAuthOutcome(
            ok=True,
            device_id=auth.record.device_id,
            device_tier=auth.record.tier,
        )

    return _authenticate


#: The four credential fields a gateway hello may name, in the order
#: :func:`_credential_kind` reports them. A TUPLE and not four ``if``s, because
#: the rule being enforced is "exactly one of these" and a rule about a set is
#: only checkable against a set — four independent branches is how a fifth field
#: eventually gets added to three of them.
_CREDENTIAL_FIELDS: tuple[str, ...] = (
    "pairing_code",
    "peer_code",
    "peer_install_id",
    "device_id",
)


def _credential_kind(message: dict[str, Any]) -> str | None:
    """Which ONE credential this hello names, or ``None`` for zero or many.

    The whole of "device-tier and peer-tier credentials are never
    interchangeable" at the FRAME level, and it is a counting rule rather than a
    precedence rule on purpose. A precedence — "a code beats an id", "a peer
    beats a device" — answers a malformed frame by picking a winner, and every
    such rule is one refactor away from picking the more privileged one.
    Counting cannot be got wrong in that direction: two credentials is a
    refusal, and the refusal looks exactly like every other credential failure
    on this lane.

    The ONE pair that is not two credentials is spelled out rather than hidden:
    a join frame carries ``peer_code`` AND ``peer_install_id``, where the code
    is the credential and the id is the name being claimed under it. Writing
    that as an explicit allowance keeps the counting rule intact for every other
    combination, including the one an attacker would actually try — a peer id
    beside a device id, or a device code beside a peer code.
    """

    named = [
        field
        for field in _CREDENTIAL_FIELDS
        if isinstance(message.get(field), str) and str(message.get(field)).strip()
    ]
    if named == ["peer_code", "peer_install_id"]:
        return "peer_code"
    return named[0] if len(named) == 1 else None


# ── Drain ────────────────────────────────────────────────────────────────────
#
# A durable service must be replaceable WITHOUT killing work: `drain` refuses
# new requests, lets the in-flight ones land, and exits. Every path emits its
# typed terminal frame BEFORE exiting — the frame is the observability, and a
# drain that exited without one would be indistinguishable from the crash it
# exists to avoid.
DEFAULT_DRAIN_DEADLINE_SECONDS = 30.0
#: The absolute sanity floor, both transports: a deadline of zero is not a
#: deadline. This is the long-standing stdio contract and is unchanged.
_DRAIN_DEADLINE_FLOOR_SECONDS = 0.05
#: The SOCKET lane's floor under a client-supplied deadline, and the
#: correction for a real defect: with only the sanity floor above, any local
#: process holding the root's secret could ask for a deadline that expires
#: instantly, and `drain` became `kill` — the timeout path calls `hard_exit`,
#: which is `os._exit`, over whatever was running.
#:
#: Why the SOCKET lane only. A deadline is a promise about how long in-flight
#: work is allowed to finish, and the question is who is entitled to shorten
#: it. Over stdio the asker is the PARENT that spawned this process and owns
#: its stdin; it can end this runtime with a signal whether or not the drain
#: cooperates, so a floor there buys no safety and would silently rewrite a
#: contract the socket slice promised to leave byte-identical. Over the socket
#: the asker is any local process that could read the token file, refereeing
#: work it cannot see. `max(ask, this)` on that lane, always.
_DRAIN_SOCKET_MINIMUM_DEADLINE_SECONDS = 30.0
#: An unbounded value would restore "can hang forever" through the front door.
_DRAIN_DEADLINE_MAX_SECONDS = 3600.0
_DRAIN_POLL_INTERVAL_SECONDS = 0.05
#: While draining, this replaces the `busy` liveness pump (which stops with the
#: delivery drain the moment draining starts). A watchdog keyed on "no frames
#: for N seconds" must not declare a healthily-draining runtime dead.
_DRAIN_PROGRESS_INTERVAL_SECONDS = 5.0
#: Refused-because-draining. 75 is EX_TEMPFAIL: "try again", which is exactly
#: what a client should do — against the replacement runtime. The refusal also
#: carries this terminal `exit` frame so a client that predates the typed
#: `draining` event still terminates its request instead of waiting forever.
DRAINING_EXIT_CODE = 75
#: In-flight work outlived the deadline. Nonzero on purpose: a supervisor must
#: be able to tell "drained" from "gave up with work still running".
DRAIN_TIMEOUT_EXIT_CODE = 3
#: ONE deadline for everything between "the drain has decided how it ended"
#: and "this process is gone": publishing the terminal frame, broadcasting it
#: to attached clients, tearing the socket lane down, unregistering, and the
#: reader unwinding. It is armed as the FIRST act of ``_finish_drain`` rather
#: than after the teardown, because the teardown is exactly what can hang —
#: hub joins were 2.0s EACH and a wedged reader can park a broadcast write for
#: IO_TIMEOUT, so with 32 subscribers the old arrangement could sum past a
#: minute with the watchdog not yet armed. Summed per-step budgets are not a
#: bound; this is.
_DRAIN_EXIT_DEADLINE_SECONDS = 15.0
#: The mirror image: how long the READER waits for the drain monitor to publish
#: its terminal frame when the transport closed first (a `shutdown` op or EOF
#: arriving mid-drain). The pool has already been joined by then, so the monitor
#: is normally one poll interval away; past this bound the drain is declared
#: abandoned IN A FRAME rather than exiting silently.
_DRAIN_ABANDON_GRACE_SECONDS = 5.0
#: How long ONE request may produce nothing before the loop describes it, on
#: the lane that asked, as ``{"id":…,"event":"request_progress","state":…}``.
#:
#: An argv request's first frame is written by its HANDLER, so until then a
#: request queued behind a full pool, one whose handler is merely slow, and one
#: whose handler has wedged are the same silence on the wire. Measured
#: 2026-08-27: a ``characters list --json`` on an authenticated socket read ZERO
#: frames for >120s while the same serve answered a later connection's identical
#: argv in ~6s — and the client could not tell which of the three it had.
#:
#: The budget is deliberately longer than any healthy read (``status`` and
#: ``snapshot`` are the launcher's cadence polls and a warm ``snapshot`` is
#: ~7s), so the normal path pays no extra frames at all and only a request that
#: has genuinely gone quiet is described.
#:
#: Read from the module at call time on purpose — a test lowers it, the same
#: seam ``_DRAIN_EXIT_DEADLINE_SECONDS`` uses.
_REQUEST_SILENCE_SECONDS = 15.0

# Chat turns must survive supervisor recycles (recording safety): these argv
# shapes mark a request as an in-flight chat turn for the busy/ping frame.
_CHAT_TURN_COMMANDS = (("mission-chat", "message"), ("mission-chat", "steer"))

# The verbs that legitimately run for MINUTES, and hold the drain deadline the
# way a chat turn does. Same reason, different work: a character generation is
# one provider call per direction or per row, hermes's own rate estimate is
# 1-2 min per generation (`harness.py::_characters_auto_write`), and a
# `turnaround` produces NOTHING until the whole strip lands — so a launcher
# update or a Reap & Restart landing mid-generation used to end the run with no
# word to anybody, and `turnaround` had no partial result to resume from.
#
# `rows` and `auto` do land per row, so what a kill costs them is the row in
# flight; `turnaround` loses everything. That is the asymmetry, and it does not
# change the answer: all three hold, because the frame the supervisor reads has
# to say a long run is in flight before the supervisor can be expected to wait.
#
# NOT a licence to hang. The hold is only bounded because the generation itself
# now is: `agent/charsheet/pipeline.py::PROVIDER_TIMEOUT_SECONDS` puts a
# deadline on every provider call, which is what stops one wedged backend from
# holding a drain open forever. The two changes are one change.
_LONG_RUN_COMMANDS = (
    ("characters", "turnaround"),
    ("characters", "rows"),
    ("characters", "auto"),
)

# ── Poll response cache (follow-up slice 1 of the serve design doc) ──────────
#
# NOT the serve core cache (``<store_root>/serve_read_model/``). This is a
# per-serve-loop replay cache for the stdout payload of the two read-only poll
# commands.
#
# The disambiguation used to name a THIRD thing, ``agent_runtime/read_model.py``,
# because "read model" meant three unrelated things in this repo. Stage 6
# (2026-08-22) deleted that module, so two remain — and the one that still
# collides is the DIRECTORY NAME below, which is the core cache's on-disk home
# and has nothing to do with either cache's contents. That naming trap is the
# reason this comment survives the module it used to warn about.
#
# The Launcher polls `harness status --json` / `harness snapshot --json` on a
# fixed cadence; each build recomputes the full projection (~1.7s status /
# ~7s snapshot warm) even when NOTHING changed. Serve is a warm process, so it
# caches the exact stdout payload of these read-only requests keyed by a
# runtime-state fingerprint (the sequence check) and replays it while the
# fingerprint holds.
#
# The fingerprint stats the cheap change signals: events.jsonl (every store
# mutation appends an event — the architecture's change feed), the turn store,
# scope pointers, the live store directories (record add/rename
# flips a directory's mtime), and the SessionDB files (chat writes; -wal /
# -journal included because a SQLite WAL commit does not touch the main db's
# mtime). Signals that live OUTSIDE the runtime root (git working trees for
# dirty state, provider health) cannot flip the fingerprint, so a TTL bounds
# their staleness: a cached payload older than _READ_CACHE_MAX_AGE_SECONDS is
# rebuilt even on a fingerprint match.
#
# Visibility: a replayed response stamps `served_from_cache` + `cache_age_ms`
# on its exit frame (additive), and the payload's own parity envelope keeps
# the honest original `generated_at`.

_CACHEABLE_ARGV: dict[tuple[str, ...], str] = {
    ("harness", "status", "--json"): "status",
    ("harness", "snapshot", "--json"): "snapshot",
}
_READ_CACHE_MAX_AGE_SECONDS = 20.0

_FINGERPRINT_ROOT_FILES = (
    "events.jsonl",
    "mission_chat_turns.json",
    "active_realm.json",
    "active_workspace.json",
)
_FINGERPRINT_STORE_DIRS = (
    "runs",
    "incidents",
    "agents",
    # S57 dropped "repo_bundles" here with the store: this list exists to
    # invalidate the poll response cache when a store directory changes, and no
    # code path can write that tree any more (S52 took the last writer, S57 the
    # module).
    # Stat'ing it every poll was cost against a directory that cannot move. Same
    # rule S56 applied to "worker_sessions".
    "runtime_instances",
    "persona_instances",
    "persona_assignments",
    "workspaces",
    "realms",
    # DELIBERATELY ABSENT: "serve_instances". Its entries appear and vanish at
    # every serve boot/exit, and the ``serve_auth_token`` file appears at first
    # boot — inside a fingerprint either one would cold the poll response cache
    # exactly when a fresh runtime is warming up, and make the stream emit
    # ``state.reconciled`` on every restart. Same standing precedent as
    # ``dispatch_delivery.DRAIN_STATE_FILENAME``; the rule is restated at both
    # ``agent_runtime/serve_registry.py`` and ``agent_runtime/serve_auth.py``.
)

# The ``running_work`` durable stores (``processes.json``, ``state.db``) are
# fingerprinted too, but they live under the HERMES **home** rather than the
# agent-runtime store root — on a profiled install those are genuinely
# different directories — so they cannot ride the two tuples above. Their one
# path authority is ``agent_runtime.running_work.running_work_store_paths``,
# called from ``_runtime_state_fingerprint`` below; duplicating the names here
# would stand up a second list free to drift from the projection's.


_FINGERPRINT_BOARD_CARD_CAP = 600  # bounded per-board card stat; remainder is rare + also evented


def _stat_board_tree(root: Any, _stat) -> None:
    """Bounded stat of the boards/ subtree: the root, each board's def + card
    files + conflict dir. Card files are stat'd individually so in-place edits
    (move/edit rewrite a file without touching the dir mtime) still flip the
    fingerprint. Capped per board to stay cheap on the hot poll path."""

    boards_root = root / "boards"
    _stat(boards_root)
    try:
        board_dirs = sorted(p for p in boards_root.iterdir() if p.is_dir())
    except OSError:
        return
    for board_dir in board_dirs:
        _stat(board_dir / "board.json")
        cards_dir = board_dir / "cards"
        _stat(cards_dir)
        _stat(board_dir / "conflicts")
        try:
            card_files = sorted(cards_dir.glob("*.json"))
        except OSError:
            continue
        for card_path in card_files[:_FINGERPRINT_BOARD_CARD_CAP]:
            _stat(card_path)


_FINGERPRINT_TURN_FILE_CAP = 200  # session cap is 50; defensive bound only


def _stat_turn_store_tree(root: Any, _stat) -> None:
    """Bounded stat of the per-session turn store (mission_chat_turns/<key>.json).

    The turn-store split (one file per chat session) made the legacy
    `mission_chat_turns.json` root-file stat a dead signal: after migration the
    monolith is renamed aside and every streamed-turn flush rewrites ONE
    session file in place — which does not reliably move the directory mtime.
    Stat each session file individually (the board-tree pattern) so a cached
    snapshot can never serve stale turn elements. The legacy root file stays in
    _FINGERPRINT_ROOT_FILES so the one-time migration rename also flips the
    fingerprint."""

    turns_root = root / "mission_chat_turns"
    _stat(turns_root)
    try:
        session_files = sorted(turns_root.glob("*.json"))
    except OSError:
        return
    for session_path in session_files[:_FINGERPRINT_TURN_FILE_CAP]:
        _stat(session_path)


def _runtime_state_fingerprint() -> tuple | None:
    """Cheap stat-based sequence check over the harness poll-payload inputs.

    Returns None when the runtime root cannot be resolved — callers must
    treat None as "never cache"."""
    try:
        from agent_runtime import paths as _paths

        root = _paths.store_root()
    except Exception:
        return None
    parts: list[tuple[str, int, int]] = []

    def _stat(path: Any) -> None:
        try:
            st = os.stat(path)
        except OSError:
            parts.append((str(path), -1, -1))
            return
        parts.append((str(path), st.st_mtime_ns, st.st_size))

    for name in _FINGERPRINT_ROOT_FILES:
        _stat(root / name)
    for name in _FINGERPRINT_STORE_DIRS:
        _stat(root / name)
    # Background-work stores hang off the HERMES home, not the store root, and
    # resolve through the same head authority their WRITERS use
    # (``get_hermes_background_work_home``): ``persona_profile_context`` flips
    # ambient HERMES_HOME process-globally while a persona turn runs in THIS
    # process, so an ambient read here would fingerprint whichever profile
    # happened to be mid-turn.
    #
    # An EMPTY tuple means the authority could not resolve a home — "I cannot
    # fingerprint these", not "there is nothing to watch". Both that case and a
    # raised exception get the same sentinel, because caching against a silently
    # missing signal is exactly how a stale HUD gets served.
    try:
        from agent_runtime.running_work import running_work_store_paths

        store_paths = running_work_store_paths()
        if not store_paths:
            parts.append(("running_work_stores", -1, -1))
        for path in store_paths:
            _stat(path)
    except Exception:
        parts.append(("running_work_stores", -1, -1))
    # Event-log rotation (C6a) moves appends off the static "events.jsonl" onto a
    # rotating live slice, so the _FINGERPRINT_ROOT_FILES entry above freezes once
    # the log rotates. Stat the manifest (flips on each rotation) AND the resolved
    # live slice (flips on every append) so a cached snapshot never serves stale
    # frames after rotation. Pre-rotation the live slice IS events.jsonl (a
    # harmless duplicate stat); the manifest is absent (a stable -1/-1 signal).
    try:
        from agent_runtime import event_rotation as _event_rotation

        _stat(_event_rotation.manifest_path())
        _stat(_event_rotation.live_path())
    except Exception:
        parts.append(("event_log_rotation", -1, -1))
    # Mission Board tree is nested two levels deep (boards/<id>/cards/<card>.json),
    # so a top-level dir stat alone misses card adds/moves/in-place edits and
    # pull-materialized cards. Every board mutation also advances events.jsonl
    # (already fingerprinted), but a bounded subtree walk here keeps cached
    # snapshots honest even for event-less file materialization (realm pull).
    _stat_board_tree(root, _stat)
    # Per-session turn store: streamed-turn flushes rewrite one session file in
    # place and emit NO EventLog event, so without these stats a cached snapshot
    # would serve stale turn elements.
    _stat_turn_store_tree(root, _stat)
    try:
        # Fingerprint the database the CHAT LANE actually writes, not the one
        # ambient HERMES_HOME resolution happens to hand this process. A bare
        # ``SessionDB()`` keyed the cache on ``HERMES_HOME/state.db`` while every
        # chat write goes to the resolved chat scope; whenever the two diverge a
        # cached snapshot could serve a frozen Chat History for the life of the
        # serve process (defect D1 in
        # ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/chat-session-presence-authority.md``,
        # the serve twin of the stream-lane fix 639242901). Resolving the PATH
        # also stops the poll loop from opening — and potentially creating — a
        # database just to read its own filename.
        from agent_runtime.chat_session_scope import chat_session_db_path
        from agent_runtime.core_cache import sqlite_fingerprint_triples

        # Keyed through the SHARED SQLite authority, not a raw stat of the three
        # siblings, since 2026-08-21. SQLite deletes the WAL on a clean
        # last-close and re-creates it EMPTY on the next open, so under the raw
        # stat this entry flipped between ``-1/-1`` and a fresh ``mtime_ns``
        # every time any process merely OPENED the chat database — keeping this
        # cache permanently cold for reasons that were never content. That is
        # the defect the 2026-08-09 analysis named; the mask that answers it has
        # existed in ``core_cache`` since MC-3b and had simply never been
        # propagated to the two poll lanes. A real commit still moves this
        # entry: uncheckpointed the WAL is non-empty and keyed in full,
        # checkpointed the frames are in ``state.db`` whose own triple is here.
        db_path = str(chat_session_db_path())
        for suffix, mtime_ns, size in sqlite_fingerprint_triples(db_path):
            parts.append((db_path + suffix, mtime_ns, size))
    except Exception:
        # Chat persistence unavailable → its absence is itself stable.
        parts.append(("session_db", -1, -1))
    return tuple(parts)


class _PollResponseCacheEntry:
    __slots__ = ("fingerprint", "lines", "code", "built_monotonic")

    def __init__(
        self, fingerprint: tuple, lines: list[str], code: int, built_monotonic: float
    ):
        self.fingerprint = fingerprint
        self.lines = lines
        self.code = code
        self.built_monotonic = built_monotonic


class _PollResponseCache:
    """Per-serve-loop stdout-payload replay cache for the read-only polls.

    Keyed by :func:`_runtime_state_fingerprint` and bounded by
    ``_READ_CACHE_MAX_AGE_SECONDS``. It caches RESPONSE BYTES — it is not the
    serve core cache (``<store_root>/serve_read_model/``), which is why it is not
    named for it. (It was not the retired ``agent_runtime/read_model.py`` either;
    that module went at Stage 6, 2026-08-22.)
    """

    def __init__(self, max_age_seconds: float = _READ_CACHE_MAX_AGE_SECONDS):
        self._entries: dict[str, _PollResponseCacheEntry] = {}
        self._lock = threading.Lock()
        self._max_age = max_age_seconds

    def get(
        self, key: str, fingerprint: tuple | None, now_monotonic: float
    ) -> _PollResponseCacheEntry | None:
        if fingerprint is None:
            return None
        with self._lock:
            entry = self._entries.get(key)
        if entry is None or entry.fingerprint != fingerprint:
            return None
        if now_monotonic - entry.built_monotonic > self._max_age:
            return None
        return entry

    def put(
        self,
        key: str,
        fingerprint: tuple | None,
        lines: list[str],
        code: int,
        now_monotonic: float,
    ) -> None:
        # Only successful builds are worth replaying; a failed build must
        # re-run so the error stays live, not fossilized.
        if fingerprint is None or code != 0:
            return
        with self._lock:
            self._entries[key] = _PollResponseCacheEntry(
                fingerprint, lines, code, now_monotonic
            )

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_serve_request_id", default=None
)

#: WHERE this request's frames go. Bound by ``_run`` alongside the request id,
#: for the same span and in the same pool-worker context.
#:
#: A durable service answers more than one transport, and a handler's ``print``
#: belongs to the client that asked — not to whoever owns stdout. Unset (the
#: default) means stdout, which is every stdio request and every thread a
#: handler spawns for itself, so the stdio lane is byte-identical to the
#: pre-socket loop: same proxy, same frames, same order.
_request_sink: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "harness_serve_request_sink", default=None
)


def current_serve_request_id() -> str | None:
    """The serve frame-protocol request id bound to THIS context, or None.

    ``_run`` binds it for exactly the span of one serve-dispatched request, in
    the pool-worker context that dispatches to the command handler — so a
    non-None answer is DIRECT provenance that the current work arrived as a
    serve frame request. Every other lane reads None: a one-shot CLI turn, the
    delivery drain's forged turns, background threads.

    This is the honest "did this turn arrive via serve" fact, and the only one.
    Two proxies have already been retired for impersonating it (both live
    2026-08-09 findings): ``persona_chat_runtime_registry() is not None`` is
    really "the hot-sessions CACHE is enabled" (default off, so every live
    serve read False), and ``delivery_drain_is_live()`` is really "a delivery
    consumer exists" — a serve whose drain died is still a serve. Provenance
    questions read THIS; capability questions read the drain.
    """

    return _request_id.get()


class _FrameWriter:
    """Sole owner of the real stdout; one lock keeps frames atomic."""

    def __init__(self, stream: TextIO):
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, frame: dict[str, Any]) -> None:
        payload = json.dumps(frame, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(payload + "\n")
            self._stream.flush()


class _LineFrameProxy(io.TextIOBase):
    """Stand-in for sys.stdout/sys.stderr that re-emits handler output as
    tagged line frames, buffered per request id until a newline."""

    def __init__(self, frames: _FrameWriter, event: str):
        super().__init__()
        self._frames = frames
        self._event = event
        self._buffers: dict[tuple[int | None, str | None], str] = {}
        self._captures: dict[tuple[int | None, str | None], list[str]] = {}
        self._lock = threading.Lock()

    def writable(self) -> bool:  # pragma: no cover - io protocol
        return True

    def isatty(self) -> bool:
        # Handlers key default output on isatty(); serve is a pipe.
        return False

    @staticmethod
    def _slot(rid: str | None) -> tuple[int | None, str | None]:
        """The partial-line buffer this write belongs to.

        Keyed by (destination, request id), not by request id alone. Request
        ids are chosen by CLIENTS, so once more than one transport is attached
        two connections may legitimately both be running ``req-1`` — and a
        buffer keyed on the id alone would splice one client's half-written
        line into the other's. Stdio's destination is None (stdout), which is
        what every pre-socket request already was.
        """

        sink = _request_sink.get()
        return (id(sink) if sink is not None else None, rid)

    def write(self, text: str) -> int:
        if not text:
            return 0
        rid = _request_id.get()
        slot = self._slot(rid)
        with self._lock:
            buffered = self._buffers.get(slot, "") + str(text)
            *lines, remainder = buffered.split("\n")
            self._buffers[slot] = remainder
            capture = self._captures.get(slot)
            if capture is not None:
                capture.extend(lines)
        sink = _request_sink.get() or self._frames
        for line in lines:
            sink.emit({"id": rid, "event": self._event, "line": line})
        return len(text)

    def flush(self) -> None:  # pragma: no cover - io protocol
        return None

    def begin_capture(self, rid: str | None) -> None:
        """Start mirroring [rid]'s emitted lines for the poll response cache."""
        with self._lock:
            self._captures[self._slot(rid)] = []

    def end_capture(self, rid: str | None) -> list[str]:
        """Stop mirroring and return everything captured for [rid]."""
        with self._lock:
            return self._captures.pop(self._slot(rid), [])

    def flush_request(self, rid: str | None) -> None:
        """Emit a request's unterminated tail (handler printed without a
        trailing newline) and drop its buffer."""
        slot = self._slot(rid)
        with self._lock:
            remainder = self._buffers.pop(slot, "")
            if remainder:
                capture = self._captures.get(slot)
                if capture is not None:
                    capture.append(remainder)
        if remainder:
            sink = _request_sink.get() or self._frames
            sink.emit({"id": rid, "event": self._event, "line": remainder})


class _SafeSink:
    """A frame sink that never raises — the socket lane's request path.

    A pool worker's ``finally`` MUST emit its terminal ``exit`` frame and clean
    up its inflight entry; a client that hung up mid-request would otherwise
    take that bookkeeping down with it, leaking the request id forever and
    stalling any drain waiting on it. Stdout is deliberately NOT wrapped: the
    stdio pipe failing is the process losing its transport, and that has always
    propagated.
    """

    __slots__ = ("_target", "write_failures")

    def __init__(self, target: Any):
        self._target = target
        self.write_failures = 0

    def emit(self, frame: dict[str, Any]) -> None:
        try:
            self._target.emit(frame)
        except Exception:
            self.write_failures += 1


def _emit_deferred_reply(build: Any, sink: Any) -> None:
    """Finish ONE deferred method reply on a pool worker and write it.

    ``build`` is already exception-proofed by ``serve_rpc.deferred_reply`` — it
    returns a typed ``-32000`` rather than raising, because a raise here would
    be a client waiting forever for a frame nobody writes. The belt below is for
    the write itself: the socket lane's sink swallows a dead client
    (:class:`_SafeSink`), and stdout's deliberately does not, so a stdio client
    that went away must not take a pool worker's thread down with it.
    """

    try:
        frame = build()
    except BaseException:  # pragma: no cover - deferred_reply already caught it
        return
    try:
        sink.emit(frame)
    except Exception:
        pass


class _ArgvRequest:
    __slots__ = (
        "rid",
        "argv",
        "is_chat_turn",
        "is_long_run",
        "is_runtime_stream",
        "cancel_event",
        "key",
        "owner",
        "sink",
        "turn_request_id",
        "submitted_monotonic",
        "started_monotonic",
        "progress_monotonic",
    )

    def __init__(
        self,
        rid: str,
        argv: list[str],
        *,
        owner: str = "stdio",
        sink: Any = None,
        turn_request_id: str | None = None,
    ):
        self.rid = rid
        self.argv = argv
        tail = argv[1:] if argv and argv[0] == "harness" else argv
        self.is_chat_turn = any(
            tuple(tail[: len(shape)]) == shape for shape in _CHAT_TURN_COMMANDS
        )
        #: A generate verb that runs for minutes. Derived from the same argv
        #: tail, by the same prefix match, so the two marks cannot disagree
        #: about where a command name starts — and a sibling flag rather than a
        #: `holds_drain` union because the drain frame reports the two
        #: separately: an operator asking why a restart is waiting needs to know
        #: WHICH kind of work is holding it, and the counts have different
        #: cures (a chat turn ends in seconds; a generation may be fifteen
        #: minutes from done).
        self.is_long_run = any(
            tuple(tail[: len(shape)]) == shape for shape in _LONG_RUN_COMMANDS
        )
        self.is_runtime_stream = bool(tail and tail[0] == "stream")
        self.cancel_event = threading.Event()
        #: Which connection asked. ``stdio`` for the inherited pipe.
        self.owner = owner
        #: The inflight-table key. Request ids are chosen by CLIENTS, so two
        #: connections may legitimately both use ``req-1``; the table is keyed
        #: per owner so neither can collide with — or cancel — the other's work.
        #: Stdio keeps the bare id, so its frames and its drain reports are
        #: byte-identical to the single-transport loop.
        self.key = rid if owner == "stdio" else f"{owner}:{rid}"
        #: Where this request's frames go. None means stdout.
        self.sink = sink
        #: Set only for a turn started by the METHOD lane (gateway Stage 3): the
        #: ``turn_request_id`` whose accept receipt this worker settles when it
        #: exits. ``None`` for every argv request, including the argv chat turns
        #: a local launcher sends — the receipt exists to close the RPC lane's
        #: accept window and a local send never opens one.
        self.turn_request_id = turn_request_id
        #: When the dispatcher took it. Set HERE rather than in ``_run``,
        #: because the gap between the two is the whole point: it is the time
        #: the request spent in the pool's queue, and that time is invisible to
        #: the worker that eventually runs it.
        self.submitted_monotonic = time.monotonic()
        #: When a pool worker entered ``_run``, or ``None`` while still queued.
        #: This single field is the difference between "the pool is full and
        #: your request has not started" and "your request is running and its
        #: side effects may already have landed" — which used to be the same
        #: silence on the wire.
        self.started_monotonic: float | None = None
        #: When the liveness pump last described this request, so a long turn
        #: is reported on a cadence rather than on every pump tick.
        self.progress_monotonic: float | None = None


class _DrainState:
    """One drain in progress, and everything its terminal frame must account for.

    The counters are the point. A `drain_complete` that only said "done" would
    be a frame with the right NAME and no evidence — it could not distinguish a
    drain that let three turns land from one that refused them all, which is
    the difference between a safe restart and lost work.
    """

    __slots__ = (
        "started_monotonic",
        "deadline_seconds",
        "refused",
        "completed",
        "deadline_holds",
        "lock",
    )

    def __init__(self, deadline_seconds: float):
        self.started_monotonic = time.monotonic()
        self.deadline_seconds = deadline_seconds
        self.refused = 0
        self.completed = 0
        #: How many times the deadline expired and was NOT allowed to end the
        #: process because a chat turn was still in flight. Counted because
        #: "this restart is taking a while" and "this restart has been held
        #: open by recording safety four times" are different operator facts.
        self.deadline_holds = 0
        self.lock = threading.Lock()

    def note_refused(self) -> int:
        with self.lock:
            self.refused += 1
            return self.refused

    def note_completed(self) -> None:
        with self.lock:
            self.completed += 1

    def note_deadline_held(self) -> int:
        with self.lock:
            self.deadline_holds += 1
            return self.deadline_holds

    def counters(self) -> dict[str, Any]:
        with self.lock:
            return {
                "requests_refused": self.refused,
                "requests_completed": self.completed,
                "deadline_holds": self.deadline_holds,
            }

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)


def _drain_deadline_seconds(
    raw: Any, default: float, *, minimum: float = _DRAIN_DEADLINE_FLOOR_SECONDS
) -> float:
    """The EFFECTIVE deadline: the client's ask, floored by the server's.

    A client may lengthen a drain (up to the hard ceiling) and may not shorten
    it below the floor the caller passes for its TRANSPORT: the sanity floor on
    stdio (unchanged — that asker owns the process), the socket minimum on the
    socket lane. The floor is a parameter rather than a constant read in here
    precisely so the two lanes can differ and so the loop's own tests can run a
    drain in milliseconds; it is a SERVER-side parameter either way, and no
    field a client sends can lower it.
    """

    floor = max(0.0, float(minimum))
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return max(floor, float(default))
    return max(floor, min(float(raw), _DRAIN_DEADLINE_MAX_SECONDS))


def _build_harness_parser() -> argparse.ArgumentParser:
    """A fresh top-level parser holding only the harness tree. Built per
    request: cheap next to any handler, and avoids sharing one parser
    across pool threads."""
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_parser(subparsers)
    return parser


def dispatch_argv(argv: list[str]) -> int:
    """Parse and run one request exactly as ``hermes <argv…>`` would,
    including the harness error-envelope contract."""
    from hermes_cli.harness import emit_harness_error

    parser = _build_harness_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.parse_args([*argv, "--help"])  # exits 0 after printing help
        return 0
    try:
        code = func(args)
    except SystemExit:
        raise
    except BaseException as exc:  # mirror hermes_cli.main harness dispatch
        return emit_harness_error(exc, args=args)
    return code if isinstance(code, int) else 0


def _prewarm_read_model_snapshot() -> None:
    """Build ONE read-model core in the background right after ``ready``.

    A fresh serve child's first snapshot build costs ~7.5s against ~2.2s warm
    (measured 2026-08-09, B4/B11): ~5s of that is per-process cache fill —
    YAML parse cache, tool-visibility memos, skill resolution, the event tail.
    Serve is long-lived, so paying it on a daemon thread the moment the child
    is ready takes it off whichever request would otherwise have been first.

    Since EG-3.1 this build is also the process's cache CONSULTATION and, when it
    demotes, its write-back: ``build_snapshot`` stats every build input and either
    loads the persisted core (~2 s, ``core_source=cache``) or rebuilds and
    persists what it built under ``<store_root>/serve_read_model/``. So the
    prewarm is no longer read-only — it is the fastest place in the boot to
    discover which of the two this child is paying for. It still writes no STORE
    state (it never called ``write_snapshot``, the ``snapshot.json`` boot-cache
    writer — and Stage 6 deleted that writer outright, so the bypass this line
    used to describe is now the only behaviour there is), and the cache write is
    best-effort by contract. Concurrency is
    handled by the builder's own coalescing — a real request arriving mid-build
    joins it (hydrate) or waits and shares the next one; it never double-builds.
    Best effort by contract: a failure here surfaces on the first real request
    exactly as it would have without the prewarm.

    ``build_info={"caller": "prewarm"}`` is what makes this build appear in the
    log at all. It is the most expensive build of a cold boot and, until the
    builder learned to emit its own receipt, it was the only one with no line
    anywhere: every ``snapshot_build`` line in the boot window belonged to a
    caller that RODE it, which is how one build came to look like three
    (plan EG-2.1). Naming the caller here costs a dict.
    """

    try:
        from agent_runtime.snapshot import build_snapshot

        build_snapshot(build_info={"caller": "prewarm"})
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "serve snapshot prewarm did not complete", exc_info=True
        )


def _prewarm_provider_runtime() -> None:
    """Best-effort warmup of the per-process one-time costs a chat turn pays.

    Runs on a daemon thread right after the ready frame. Each step is
    independent and failure-isolated: a broken CA bundle or missing provider
    dependency surfaces on the first real turn with its normal typed error,
    exactly as it would without prewarm.
    """
    try:
        from agent.process_bootstrap import _load_openai_cls, shared_ssl_context

        _load_openai_cls()
        shared_ssl_context()
    except Exception:
        pass
    try:
        from agent.ssl_guard import verify_ca_bundle

        verify_ca_bundle()
    except Exception:
        pass
    try:
        from model_tools import get_tool_definitions

        # The exact cache key varies per persona toolset; this call warms the
        # shared parts (tool module imports, registry build, config parse).
        get_tool_definitions(quiet_mode=True)
    except Exception:
        pass


def _prewarm_persona_chat_actors() -> None:
    """Best-effort background construction of the resident chat actors.

    THIRD on the one prewarm thread, behind the read-model build and the
    provider warmup, and the ordering is load-bearing in both directions: the
    launcher's canvas is waiting on the build, and an agent construction that
    runs after ``_load_openai_cls``/``shared_ssl_context`` does not pay the SDK
    import itself (which is the single largest item in a cold construct).

    Inert unless the root config turns hot sessions on — with no resident
    registry there is nowhere to put a pre-built actor, and that is the ONLY
    gate (a ``prewarm_on_boot`` knob was written and withdrawn: every
    ``PersonaChatConfig`` field rides the read-model wire, so a new key is a
    cross-stack golden change — see the note on that class). The pass QUEUES;
    the constructions run on
    ``persona_chat_actor_prewarm``'s own single daemon worker, which stands down
    for any real turn in flight, so a chat sent during the boot window is never
    behind a warm.
    """

    try:
        from agent_runtime.persona_chat_actor_prewarm import prewarm_chat_actors_on_boot

        prewarm_chat_actors_on_boot()
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "serve chat-actor prewarm did not complete", exc_info=True
        )


def install_harness_skills_at_boot() -> str:
    """Re-join the runtime's copy of every canonical skill to this repo's copy.

    THE GAP THIS CLOSES (operator ruling 2026-08-30, plan
    ``archive/skill-install-trigger-relocation.md``). A canonical shared skill
    has two copies and only one is ever executed: a chat turn loads
    ``<hermes root>/shared/skills/<id>/SKILL.md``, never the repo's
    ``docs/agent-runtime-harness/harness-skills/<id>/SKILL.md``, because
    ``agent.skill_utils`` refuses any candidate for a canonical id whose
    ``source_kind`` is not ``shared_core``. Until this ran, the join was made by
    exactly three triggers — an explicit CLI verb, a realm-sync pull, and a
    pre-push hook — and every one of them fires when the machine PUBLISHES.
    A machine that merely ``git pull``s and boots was repaired by nothing.

    So it runs at the moment a CONSUMER acquires the drift instead: boot. It is
    the strongest spot in the census because it is the only one with an
    unambiguous home — the pre-push hook's whole refuse-to-guess-``HERMES_HOME``
    contortion (``scripts/verify_harness_skill_install.py``, exit 2) exists
    because a hook inherits an arbitrary pushing shell, while this process was
    spawned with its home explicitly pinned and has already resolved it.

    EXACTLY what a realm-sync pull runs (``agent_runtime/realm_sync.py:509-511``),
    deliberately, rather than a second spelling of the same repair.

    FAILURE POSTURE: loud, never fatal. The push gate blocked, because a push is
    a one-shot event and an install that did not take had to stop it. A boot is
    not: the next boot retries for free, and a chat runtime that refuses to start
    because a skill package would not copy is a far worse outcome than one that
    starts carrying a stale package and says so. Every failed result is named on
    the caller's log lane.

    Returns the one-line summary; raising is not part of the contract.
    """

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.skill_install import (
        HARNESS_SKILLS,
        install_harness_skills,
        install_harness_skills_for_personas,
    )

    results = [
        *install_harness_skills(skills=sorted(HARNESS_SKILLS)),
        *install_harness_skills_for_personas(
            ensure_persisted_personas(load_agent_runtime_config())
        ),
    ]
    changed = [item.skill for item in results if item.changed]
    failed = [item for item in results if not item.ok]
    summary = (
        f"harness serve: skill install — {len(results)} package(s), "
        f"{len(changed)} refreshed, {len(failed)} failed"
    )
    if changed:
        summary += f" | refreshed: {', '.join(sorted(set(changed)))}"
    return summary + "".join(
        f"\n  FAILED {item.skill}: {item.source} -> {item.destination}"
        f" (repo {item.source_hash}, installed {item.installed_hash})"
        for item in failed
    )


def _annotate_import_tax(timeline: Any) -> None:
    """Decompose ``interpreter_ms`` into named segments on the boot block (BW-0).

    ``interpreter_ms`` is one number covering process creation → this command's
    first instruction, and on the 2026-08-17 cold boot it was 20,421 ms against a
    warm baseline of ~2,000 ms — 18.4 s of unattributed cold-boot cost, next to
    1,437 ms of post-``booting`` work attributed phase by phase. The anchors that
    split it can only be read where they were taken (``hermes_cli._boot_clock``,
    written by ``main.py``), so the derivation happens here and rides the frame
    the launcher already parses.

    Never raises and never fabricates: a segment whose endpoints were not both
    observed is simply absent, exactly as ``interpreter_ms`` itself is absent on
    a platform that will not report a process creation time. A boot that reached
    this loop without going through ``main()`` (every ``serve_loop`` unit test)
    annotates whatever subset its anchors support, which is additive and inert.
    """

    try:
        from hermes_cli import _boot_clock

        timeline.annotate(
            _boot_clock.import_tax_segments(
                process_start=timeline.process_start_monotonic,
                dispatch_reached=timeline.started_monotonic,
            )
        )
    except Exception:  # pragma: no cover - observability must never fail a boot
        pass


def serve_loop(
    reader: TextIO,
    writer: TextIO,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    dispatch: Callable[[list[str]], int] = dispatch_argv,
    fingerprint: Callable[[], tuple | None] = _runtime_state_fingerprint,
    read_cache_max_age: float = _READ_CACHE_MAX_AGE_SECONDS,
    liveness_pump_interval_seconds: float = 5.0,
    boot_timeline: Any = None,
    snapshot_prewarm: Callable[[], None] | None = None,
    provider_prewarm: Callable[[], None] | None = None,
    actor_prewarm: Callable[[], None] | None = None,
    root_anchor: Callable[[], Any] | None = None,
    skill_install: Callable[[], str] | None = None,
    drain_deadline_seconds: float = DEFAULT_DRAIN_DEADLINE_SECONDS,
    drain_socket_minimum_deadline_seconds: float = (
        _DRAIN_SOCKET_MINIMUM_DEADLINE_SECONDS
    ),
    drain_poll_interval_seconds: float = _DRAIN_POLL_INTERVAL_SECONDS,
    drain_wakeup: Callable[[], None] | None = None,
    hard_exit: Callable[[int], None] | None = None,
    socket_lane: bool = False,
    stream_source_factory: Callable[[], Any] | None = None,
    stream_buffer_limit: int | None = None,
    stream_byte_limit: int | None = None,
) -> int:
    """Core dispatch loop over explicit streams. stdio is transport #1; the
    localhost socket is transport #2, and both feed THIS dispatcher.

    ``socket_lane`` is injected and OFF by default — the same contract as
    ``root_anchor`` and ``hard_exit`` — so every pre-socket test observes the
    byte-identical stdio loop, and a caller that wants the durable service says
    so explicitly. When it is on, this loop races for the per-root socket lock,
    binds an ephemeral loopback port before ``ready`` (so the ready frame and
    the registry entry can both carry it), and starts accepting only after the
    request pool exists.

    ``stream_source_factory`` is the shared subscription producer, likewise
    injectable: the default builds the real ``agent_runtime.stream``
    generator, and a test hands over a finite fake so a subscription test costs
    milliseconds instead of a projection build.

    ``drain_wakeup`` and ``hard_exit`` are the two process-level levers the
    drain needs and a unit test must not be given: the first unblocks a reader
    parked on an idle pipe once the drain has finished (the real entry point
    closes the protocol descriptor), the second takes the process down when
    in-flight work outlived the deadline. The timeout case CANNOT be a plain
    return: ``concurrent.futures`` registers an atexit hook that JOINS every
    worker thread, so an interpreter carrying a stuck worker hangs on the way
    out — which is the same "forever" the deadline exists to bound. Both are
    injected and OFF by default, the same contract as ``snapshot_prewarm`` and
    ``root_anchor``, so ``serve_loop``'s own tests observe the frames and the
    return code without ever exiting the test process.

    ``boot_timeline`` is the caller's already-running :class:`BootTimeline`
    (``_cmd_serve`` starts one at the process's first hermes instruction, so
    ``interpreter_ms`` covers the import tax); the loop starts its own when a
    caller supplies none. ``snapshot_prewarm`` is the post-``ready`` warmup
    policy — injected, and OFF unless the real entry point turns it on, so the
    loop's own unit tests never fire a multi-second projection build.

    ``provider_prewarm`` is the chat turn's one-time costs (lazy OpenAI SDK
    import, SSL context / CA verification, tool-definition registry) and takes
    the SAME injection contract for the same reason — it was an unconditional
    ``Thread.start()`` in this loop, which meant every unit test of the loop
    imported the OpenAI SDK. It runs on the snapshot prewarm's thread, after it
    (EG-3.2); see the comment at the thread start below for why the ordering is
    the whole stage.

    ``actor_prewarm`` is the persona-chat resident-actor pass and rides the same
    thread THIRD, on the same injection contract: it queues real agent
    constructions, so a loop unit test must never fire it by default.

    ``skill_install`` re-joins the runtime's installed canonical skill packages
    to this repo's copies (:func:`install_harness_skills_at_boot`) and takes the
    SAME injection contract as ``root_anchor``, for the same reason and with more
    at stake: it WRITES into the machine-global ``get_shared_skills_dir()``, so a
    loop unit test that fired it by default would edit the operator's live
    runtime. On, therefore, only where the real entry point turns it on. It runs
    SYNCHRONOUSLY and before the pool exists — the whole point is that no request
    is ever dispatched against a stale package — and it is bounded work (a hash
    per canonical package; ``install_harness_skill`` writes nothing when they
    match), unlike the prewarms it sits beside.
    """

    from agent_runtime.boot_timeline import BootTimeline

    timeline = boot_timeline if boot_timeline is not None else BootTimeline()
    _annotate_import_tax(timeline)
    frames = _FrameWriter(writer)
    # Emitted before ANY heavy boot work (the agent_runtime import, root
    # config load, registry init, and the pre-ready orphan sweep below): a
    # supervising launcher can tell a live cold boot from a wedged child by
    # this frame alone. A cold-cache boot can run past any short watchdog
    # before ``ready``; killing it mid-boot respawns into another cold boot
    # forever (2026-07-26 launcher kill-loop incident). Consumers that
    # predate this frame ignore unknown events, so it is purely additive.
    #
    # ``boot``: the self-attributing cold-boot stamp (T9). A >25s cold boot has
    # been recorded and is not reproducible on demand (the OS file cache is
    # warm on any machine that just ran the launcher), so the boot measures
    # itself instead: ``interpreter_ms`` here is the interpreter + hermes CLI
    # import tax the supervisor can see NO other way, and the ``ready`` frame
    # below carries the per-phase breakdown of everything after it.
    frames.emit(
        {
            "event": "booting",
            "pid": os.getpid(),
            "schema_version": SERVE_SCHEMA_VERSION,
            "boot": timeline.stamps(),
        }
    )
    # ── The fingerprint home, captured HERE and nowhere later (HC-1) ─────────
    #
    # WHY THIS INSTANT, named against what is on either side of it.
    #
    # BEFORE: process creation, the interpreter + hermes import tax, and
    # ``hermes_cli.main._apply_profile_override`` — which is where HERMES_HOME
    # is resolved from --profile / the active-profile marker, at main's MODULE
    # import, long before this command was dispatched. HERMES_HEAD_HOME is the
    # launcher's spawn env and is likewise fixed by now. So the two authorities
    # ``get_hermes_head_home`` reads are already final at this line; there is no
    # earlier point in this process where they are BOTH valid.
    #
    # AFTER: everything that could take a fingerprint, and everything that could
    # install a context-local home override. Specifically — the root runtime
    # config load, the chat-session registry, the chat-head publish, the root
    # anchor, the store-root resolve, the service foundations, the orphaned-turn
    # and dispatch sweeps, the ready frame, the prewarm thread (whose first act
    # is a full read-model build), and every dispatched request after it. A
    # persona scope can only exist inside one of those, so a capture here cannot
    # be taken under one.
    #
    # THE DEFECT THIS RETIRES. ``resolved_fingerprint_home`` is capture-once but
    # was LAZY, so the capture instant was whichever build or consult won the
    # race — and on a persona turn that is a thread with
    # ``persona_profile_context``'s override live. The install this runs on has
    # ONE profile and still demoted ``reason=home_mismatch`` on three callers in
    # a single boot, twice (2026-08-21 16:04:32, 2026-08-22 13:36), each time
    # followed by a cold 7.6s build.
    #
    # COST, measured rather than assumed: importing ``core_cache`` here is 93ms
    # cold, of which ~90ms is its dependency set (paths, dispatch_delivery,
    # parity, serve_auth, serve_registry, serve_socket) — every one of which the
    # boot below imports anyway, before ``ready``. The module itself is 2.5ms.
    # This moves the import earlier; it does not add it.
    #
    # The declaration is a SEPARATE call on purpose — see
    # ``core_cache.declare_fingerprint_home_boot_site``. It is what makes a boot
    # that stops capturing SAY so, instead of silently going back to lazy.
    from agent_runtime import core_cache as _core_cache

    _core_cache.declare_fingerprint_home_boot_site(FINGERPRINT_HOME_BOOT_SITE)
    _core_cache.capture_fingerprint_home()
    # THIS SERVE'S OWN HERMES HOME, captured at the same instant and for the
    # same reason — and then handed to every argv request below (see ``_run``).
    #
    # The fingerprint capture above protects the snapshot cache's closure from a
    # concurrent persona scope. This protects the REQUESTS. Same mechanism, same
    # boot instant, different victim, and it is worth stating why one capture
    # cannot serve both: ``capture_fingerprint_home`` resolves through
    # ``get_hermes_head_home()`` and reports whether that answer was
    # authoritative, because a cache key must know when its home is a guess. A
    # request does not want the head — under an operator-supplied
    # ``HERMES_HEAD_HOME`` (the launcher sets it so the Mission Control
    # transcript store stays put while ``HERMES_HOME`` selects a profile) the
    # head and the runtime home are DIFFERENT directories on purpose, and an
    # argv request belongs to the runtime one. So this reads ``get_hermes_home``
    # directly.
    #
    # WHAT IT FIXES (measured 2026-08-27, operator's screen). This process is
    # booted onto one home and then runs ``persona_chat_actor_prewarm`` over
    # every placed persona; each warm binds ``persona_profile_context`` with the
    # ``os.environ`` mirror ON — necessarily, because a spawned MCP server and a
    # raw-env in-process plugin have no other channel. On the incident boot that
    # was four instances on the profile ``launcher-qa``, 20:56:06-20:56:14, the
    # first bind held 11.25 s. For that whole span every pool worker without a
    # binding of its own resolved ``get_hermes_home()`` to ``launcher-qa``:
    # ``harness characters status --draft 20260827-150945-7ba0cb`` read
    # ``profiles\launcher-qa\characters\.drafts`` and reported a base-authored
    # draft as nonexistent.
    #
    # Captured, never re-read per request: at this instant no persona scope can
    # exist in the process (the anchor, the store-root resolve, the sweeps, the
    # ready frame and the prewarm thread all come after), whereas a read at
    # request entry would inherit a flip that was already live — which is half
    # the field cases.
    from hermes_constants import get_hermes_home as _get_hermes_home

    serve_request_home = _get_hermes_home()
    # The METHOD lane's registry + its manifest. Imported here rather than at
    # module scope for the same reason as everything else in this function —
    # nothing agent_runtime-shaped is paid for before ``booting`` is out — and
    # it is cheap: ``serve_rpc`` imports only stdlib, and each method reaches
    # for its stores function-locally when it is actually called.
    from agent_runtime import serve_rpc

    # The one function that turns a live connection into an authorization
    # identity (chokepoint plan A2). Beside ``serve_rpc`` because it is the same
    # lane and the same stdlib-only cost.
    from agent_runtime.call_authorization import caller_for_connection

    from agent_runtime.persona_chat_continuity import (
        initialize_persona_chat_runtime_registry,
    )
    from agent_runtime.config import load_root_runtime_config

    persona_chat_cfg = load_root_runtime_config().persona_chat
    initialize_persona_chat_runtime_registry(
        enabled=persona_chat_cfg.hot_sessions_enabled,
        max_entries=persona_chat_cfg.max_hot_sessions,
        ttl_seconds=persona_chat_cfg.idle_ttl_seconds,
    )
    timeline.mark("chat_registry_ms")
    # Publish this process's EXPLICIT chat head home into the shared runtime
    # store root — the ONE writer of that pointer. The Launcher always starts
    # serve with HERMES_HEAD_HOME; a plain CLI turn started later names no head
    # and, without the pointer, degrades to its own profile database, minting
    # the transcript where the cockpit never looks while writing the binding
    # into the shared store (the 2026-07-27 read-lane gap). No-op when this
    # process named no head of its own, and best effort by contract.
    from agent_runtime.chat_session_scope import publish_chat_head_home

    publish_chat_head_home()
    timeline.mark("head_publish_ms")
    # Publish the machine root anchor: `agent_runtime.store_root` into the
    # PLATFORM DEFAULT home's config.yaml, so a later ambient process (no
    # HERMES_HOME, no HERMES_AGENT_RUNTIME_ROOT) resolves this serve's real
    # runtime root — and therefore finds the chat-head pointer above — instead
    # of the %LOCALAPPDATA% shadow runtime (the 2026-08-12 ambient
    # chat-history incident: `ok: true, count: 0` from the wrong root).
    # Injected and OFF unless the real entry point turns it on — the same
    # contract as ``snapshot_prewarm`` — so the loop's unit tests can never
    # write the machine-global config. Best effort by contract, but ACCOUNTED:
    # the typed outcome is emitted as its own frame either way, because a
    # silent skip here is exactly the false-all-clear class the anchor
    # retires. Consumers that predate this frame ignore unknown events.
    #
    # Since 2026-08-13 the same call also DECLARES `agent_runtime.head_home`
    # when this serve was started with an explicit head, and the frame carries
    # that outcome additively under `head`. That is the runtime declaring its
    # own identity: the Launcher's `HERMES_HEAD_HOME` pin demotes from sole
    # authority to an override plus a consistency check, and the launcher
    # compares its pin against this frame (a disagreement is a durable
    # `root_declaration_mismatch` transport receipt, never a silent divergence).
    if root_anchor is not None:
        try:
            anchor_report = root_anchor()
            anchor_frame = {"event": "root_anchor", **anchor_report.payload()}
        except Exception as exc:  # must never take the boot down
            anchor_frame = {
                "event": "root_anchor",
                "outcome": "unwritable",
                "detail": type(exc).__name__,
            }
        frames.emit(anchor_frame)
    timeline.mark("root_anchor_ms")
    # ── The installed-skill join, at the moment a CONSUMER acquires drift ─────
    #
    # See :func:`install_harness_skills_at_boot` for what it repairs and why the
    # pre-push hook was the wrong trigger for it. Three things about the PLACE:
    #
    # * AFTER ``booting``, so the frame a supervising launcher uses to tell a
    #   live cold boot from a wedged child is already out. Nothing goes in front
    #   of that frame (2026-07-26 kill-loop incident, above);
    # * AFTER the root anchor, which is the call that declares this machine's
    #   ``agent_runtime.head_home``. The install destination derives from the
    #   resolved hermes home, so the declaration that names it is published
    #   first — the same ordering the verify script's resolution ladder assumes;
    # * BEFORE the stdout/stderr swap on the line below. ``sys.stderr`` is still
    #   the inherited descriptor here — the serve log lane — so the summary
    #   CANNOT reach the NDJSON stdout bridge even by accident. That is the
    #   stdout discipline made structural instead of careful.
    #
    # Loud, never fatal: a chat runtime that refuses to boot because a package
    # would not copy is worse than one that boots carrying a stale package and
    # says so, and the next boot retries for free.
    if skill_install is not None:
        try:
            print(skill_install(), file=sys.stderr, flush=True)
        except Exception as exc:
            print(
                f"harness serve: skill install FAILED — {type(exc).__name__}: {exc}"
                " (booting anyway; the installed packages may be stale)",
                file=sys.stderr,
                flush=True,
            )
    timeline.mark("skill_install_ms")
    stdout_proxy = _LineFrameProxy(frames, "line")
    stderr_proxy = _LineFrameProxy(frames, "stderr")
    read_cache = _PollResponseCache(read_cache_max_age)
    from agent_runtime.snapshot import SnapshotBuildContext

    read_build_context = SnapshotBuildContext()

    inflight: dict[str, _ArgvRequest] = {}
    # Futures by request id so ``{"op":"cancel"}`` can drop work that is
    # still queued behind the pool. A running request is uninterruptible —
    # cancel() then returns False and the client is told the side effect may
    # still land.
    inflight_futures: dict[str, Future] = {}
    inflight_lock = threading.Lock()

    # Drain state. ``None`` until a `drain` op arrives; from then on it is the
    # single answer to "are we still accepting work", read by the request path
    # and written once by the op.
    drain_state: _DrainState | None = None
    drain_exit_code = 0
    drain_finished = threading.Event()
    #: Latched the instant a drain DECIDES how it ended, before it publishes
    #: anything. A drain has exactly one terminal frame: without this latch a
    #: mid-drain EOF could publish ``drain_abandoned`` after a completed drain
    #: had already published ``drain_complete``, telling a supervisor that a
    #: successful restart gave up — and exiting 3 on it.
    drain_terminal_published = threading.Event()
    drain_terminal_lock = threading.Lock()
    reader_unwound = threading.Event()
    pool_shutdown_wait = True
    boot_id = uuid.uuid4().hex

    # ── socket lane state (all None unless ``socket_lane`` is on AND this
    # serve wins the per-root ownership lock) ────────────────────────────────
    socket_server: Any = None
    socket_lock: Any = None
    #: The SECOND listener (remote-gateway Stage 1). ``None`` unless
    #: ``remote_gateway.listen`` names an interface AND this serve owns the
    #: loopback lane — the gateway lane is not a separate ownership question, it
    #: is the same dispatcher answering on a second door, so a serve that lost
    #: the per-root lock must not open one either. It shares the loopback lane's
    #: lock, its pool, its stream hub and its drain; what it does not share is
    #: the credential (per device, not per root) and the encryption (TLS).
    gateway_server: Any = None
    #: The ONE stream producer, built on the first ``subscribe`` and stopped
    #: when the last subscriber leaves. Never per client: a delta batch rebuilds
    #: a full snapshot core, so N generators would cost N of them.
    stream_hub: Any = None
    #: ONE lock over all three lane handles above. They used to be swapped by
    #: bare ``nonlocal`` assignment from the drain path, the shutdown path, and
    #: the EOF path — three threads racing an unsynchronised read-modify-write
    #: on the objects whose whole job is to be released exactly once. Held for
    #: the SWAP only, never across a join: the point is that two closers cannot
    #: both take the same handle, not that teardown is serialised.
    lane_lock = threading.Lock()
    #: Per-subscriber patch-fold declarations: connection key → the entity
    #: classes that client said it can fold, or None when it said nothing (which
    #: is NOT the empty set — see ``patch_coverage.HISTORICAL_FOLD_ENTITIES``).
    #: Guarded by ``lane_lock`` because the producer thread reads it while a
    #: request thread is writing it. The producer is SHARED, so what it may
    #: promote is the INTERSECTION over this table, not any one client's answer.
    stream_fold_entities: dict[str, Any] = {}

    def _busy_frame() -> dict[str, Any]:
        with inflight_lock:
            pending = len(inflight)
            chat_turns = sum(1 for item in inflight.values() if item.is_chat_turn)
            long_runs = sum(1 for item in inflight.values() if item.is_long_run)
        # ``long_runs`` is ADDITIVE. ``chat_turns`` keeps its exact meaning and
        # its exact name because the launcher decodes it by that name
        # (`mission_control_serve_session_io.dart`, the `busy` case →
        # `MissionServeBusySignal.chatTurns`), and a supervisor that learned to
        # read a renamed key would be a supervisor that stopped reading the old
        # one mid-upgrade.
        return {
            "event": "busy",
            "chat_turns": chat_turns,
            "long_runs": long_runs,
            "pending": pending,
        }

    def _report_quiet_requests(pending: list[_ArgvRequest]) -> None:
        """Describe every request that has gone quiet, to the lane that asked.

        ``busy`` says the SERVICE is alive and carries a count. It cannot
        answer the question a waiting client actually has, which is about ONE
        request: has mine started, or is it behind the pool? This does, by id,
        on that request's own sink — so the answer arrives on the connection
        that is waiting for it rather than on stdout, where a socket client
        cannot see it.

        The budget is read from the module on every lap, so a test lowers it.

        ``harness stream`` is deliberately excluded: it is the infinite
        subscription, it is silent between events BY DESIGN, and it has the
        stream lane's own liveness. Everything else that produces nothing for
        this long is a fact an operator wants.
        """

        budget = max(0.0, float(_REQUEST_SILENCE_SECONDS))
        now = time.monotonic()
        depth = len(pending)
        for request in pending:
            if request.is_runtime_stream:
                continue
            waited = now - request.submitted_monotonic
            if waited < budget:
                continue
            last = request.progress_monotonic
            if last is not None and (now - last) < budget:
                continue
            request.progress_monotonic = now
            started = request.started_monotonic
            frame = {
                "id": request.rid,
                "event": "request_progress",
                # The one field that matters. "queued" means no handler code
                # has run, so nothing has been mutated and a retry is free;
                # "running" means side effects may already have landed.
                "state": "running" if started is not None else "queued",
                "waited_ms": int(waited * 1000),
                "running_ms": (
                    0 if started is None else int((now - started) * 1000)
                ),
                "pending": depth,
                "pool_size": pool_size,
            }
            target = request.sink if request.sink is not None else frames
            try:
                target.emit(frame)
            except Exception:
                # Same contract as the pump that calls this: telemetry must
                # never take down the loop it describes.
                pass

    def _service_log(payload: dict[str, Any]) -> None:
        """One structured line per transport event, on the serve's own stderr.

        Which means it arrives at the supervisor as an ordinary
        ``{"id":null,"event":"stderr","line":…}`` frame — the lane serve already
        uses for everything a handler writes to stderr. No new frame type, no
        new sink, and correlatable by ``boot_id`` against the ready frame.
        """

        try:
            sys.stderr.write(
                json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            )
        except Exception:
            pass

    def _run(request: _ArgvRequest) -> None:
        from agent_runtime.profile_context import process_home_scope
        from agent_runtime.request_control import request_cancel_scope

        # FIRST act of the worker, before any import or any handler code: from
        # here on the request is RUNNING, and the liveness pump says so instead
        # of reporting it as queued. A stamp taken later would describe a
        # request that is inside its handler as still waiting for a worker,
        # which is the exact confusion this field exists to end.
        request.started_monotonic = time.monotonic()
        token = _request_id.set(request.rid)
        # Answers go back to whoever asked. ``request.sink`` is None on stdio,
        # which leaves the contextvar unset and the proxy on stdout — the
        # pre-socket path, unchanged.
        sink_token = _request_sink.set(request.sink)
        sink: Any = request.sink if request.sink is not None else frames
        code = 1
        cache_key = _CACHEABLE_ARGV.get(tuple(request.argv))
        request_fingerprint: tuple | None = None
        served_from_cache = False
        cache_age_ms = 0
        capturing = False
        try:
            cached = None
            if cache_key is not None:
                request_fingerprint = fingerprint()
                cached = read_cache.get(
                    cache_key, request_fingerprint, time.monotonic()
                )
            if cached is not None:
                served_from_cache = True
                cache_age_ms = int(
                    (time.monotonic() - cached.built_monotonic) * 1000
                )
                code = cached.code
                for line in cached.lines:
                    sink.emit({"id": request.rid, "event": "line", "line": line})
                return
            if cache_key is not None and request_fingerprint is not None:
                stdout_proxy.begin_capture(request.rid)
                capturing = True
            try:
                # THE REQUEST'S OWN HOME, pinned for the width of the dispatch.
                #
                # A ContextVar, so it is per-worker and out-ranks
                # ``os.environ["HERMES_HOME"]`` in ``get_hermes_home()``'s
                # ladder — which is exactly the asymmetry the fix needs. A
                # persona lane that mirrors the env keeps the global channel it
                # genuinely requires (spawns, raw-env plugins), and this lane
                # stops being a passenger on it. See
                # ``profile_context.process_home_scope`` for the measured
                # incident and for what the scope deliberately does not cover.
                #
                # Placed OUTSIDE ``request_cancel_scope`` and around the whole
                # dispatch, not around a resolver: the bled reader was
                # ``agent.charsheet.draft.drafts_dir()``, four call frames deep
                # inside a ``_cmd_*`` handler, and there is no list of such
                # readers worth maintaining — every handler that resolves a home
                # is one. Binding at the seam covers all of them, including the
                # ones added tomorrow.
                #
                # Chat turns arrive here too, on both lanes (the RPC
                # ``spawn_chat_turn`` builds an ``_ArgvRequest`` and submits it
                # to this same ``_run``). This does not disturb them: a turn's
                # own ``persona_profile_context`` binds INSIDE this scope and
                # its ContextVar override nests over this one, so the persona
                # still gets its profile home. What changes is only the turn's
                # STARTING home, which is now this serve's rather than whatever
                # another lane last left in the environment.
                with process_home_scope(serve_request_home), request_cancel_scope(
                    request.cancel_event
                ):
                    if cache_key is not None:
                        from agent_runtime.snapshot import snapshot_build_context_scope

                        with snapshot_build_context_scope(read_build_context):
                            code = dispatch(list(request.argv))
                    else:
                        code = dispatch(list(request.argv))
            except SystemExit as exc:  # argparse usage errors land here
                raw = exc.code
                code = raw if isinstance(raw, int) else (0 if raw is None else 2)
                if code != 0:
                    sink.emit(
                        {
                            "id": request.rid,
                            "event": "error",
                            "error": "argv_parse_failed",
                            "detail": "argparse rejected the request argv; usage was forwarded as stderr frames",
                        }
                    )
            except BaseException as exc:  # dispatch() already enveloped harness errors
                sink.emit(
                    {
                        "id": request.rid,
                        "event": "error",
                        "error": "dispatch_failed",
                        "detail": f"{type(exc).__name__}",
                    }
                )
        finally:
            if request.turn_request_id:
                # Gateway Stage 3. The accept receipt learns its worker ended,
                # and the code goes on it.
                #
                # Placed FIRST in the finally, and that position was found by a
                # test rather than reasoned to. It has to be before the exit
                # frame, or a client that reads the exit and immediately retries
                # the same ``turn_request_id`` can observe a receipt still
                # saying ``accepted``. But putting it between the inflight POP
                # and the frame is worse than either: the drain monitor polls
                # the pending set, so a request that is out of ``inflight`` and
                # not yet emitted is a window in which the drain can complete
                # and close the lane UNDER the exit frame — reproduced, as a
                # lost exit, the first time this was written that way. Before
                # the pop, the monitor still counts this request and the window
                # does not exist.
                #
                # Best-effort by contract (``settle_chat_turn`` never raises):
                # the ack it settles is long since on the wire, the receipt's
                # REPLAY answer does not depend on the exit code, and a
                # bookkeeping failure must never take the place of a turn's own
                # exit frame.
                from agent_runtime.chat_turn_reservations import settle_chat_turn

                settle_chat_turn(
                    turn_request_id=request.turn_request_id, exit_code=code
                )
            stdout_proxy.flush_request(request.rid)
            stderr_proxy.flush_request(request.rid)
            if capturing:
                read_cache.put(
                    cache_key,
                    request_fingerprint,
                    stdout_proxy.end_capture(request.rid),
                    code,
                    time.monotonic(),
                )
            _request_id.reset(token)
            _request_sink.reset(sink_token)
            with inflight_lock:
                inflight.pop(request.key, None)
                inflight_futures.pop(request.key, None)
                # Accounted here rather than by the monitor's before/after
                # arithmetic: the monitor only ever sees the pending SET, so a
                # request that both started and finished during the drain would
                # be invisible to it.
                #
                # And accounted INSIDE the same critical section as the pop,
                # which it did not used to be. With the increment outside, a
                # request sat in a window where it was gone from ``inflight``
                # and not yet in ``completed`` — the monitor could observe an
                # empty pending set and publish ``drain_complete`` with a
                # completion count LOWER than the number of exits it had
                # actually let land (reproduced: 5 reported for 8 exits). The
                # counters are the drain's only evidence, so an under-count
                # reads to an operator as work the restart dropped.
                #
                # Lock order is inflight_lock → _DrainState.lock, and it is the
                # only nesting of the two: every other site takes them one after
                # the other, never one inside the other.
                if drain_state is not None:
                    drain_state.note_completed()
            exit_frame: dict[str, Any] = {
                "id": request.rid,
                "event": "exit",
                "code": code,
            }
            if served_from_cache:
                exit_frame["served_from_cache"] = True
                exit_frame["cache_age_ms"] = cache_age_ms
            sink.emit(exit_frame)

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_proxy, stderr_proxy
    try:
        store_root_path: Any = None
        try:
            from agent_runtime import paths as _paths

            store_root_path = _paths.store_root()
            runtime_root = str(store_root_path)
        except Exception:
            store_root_path = None
            runtime_root = None
        timeline.mark("store_root_ms")
        # ── Durable-service foundations (slice 2) ───────────────────────────
        #
        # These three run BEFORE ``ready`` because ``ready`` is the frame that
        # carries them: a client that has to ask a second question to learn
        # what code it just connected to has a window in which it does not
        # know, and windows like that are how a stale service serves a whole
        # session before anyone notices.
        #
        # 1. WHICH CODE. Today serve is a per-client child, so a launcher
        #    restart picks up landed fixes for free and nobody ever had to
        #    ask. A durable service silently pins last week's code instead —
        #    the shape of the dispatch dead-flag-proxy incident, which ran
        #    green for a week. Resolved once per process and cached.
        try:
            from agent_runtime.build_stamp import build_stamp

            build_block = build_stamp().frame_payload()
        except Exception as exc:  # an instrument must never take the boot down
            build_block = {
                "commit": None,
                "dirty": None,
                "source": "unknown",
                "resolved_at": None,
                "reason": f"stamp_failed:{type(exc).__name__}",
            }
        # 2. THE SECRET. Unwired to any transport (stdio needs none), minted
        #    now so the socket slice starts with a lock already on the door
        #    rather than shipping open. The frame carries the POSTURE only —
        #    the token value must never appear in a frame, a log, or an event.
        auth_block: dict[str, Any] = {"token_file": "error:root_unresolved"}
        if store_root_path is not None:
            try:
                from agent_runtime.serve_auth import ensure_token

                auth_block = ensure_token(store_root_path).payload()
            except Exception as exc:
                auth_block = {"token_file": f"error:{type(exc).__name__}"}
        # 2b. WHICH INSTALL. The secret above says a caller MAY talk to this
        #     runtime; this says WHICH runtime it reached. Two facts, two
        #     mechanisms, deliberately — an id that both names and authorises is
        #     how "I know your install id" becomes "I am you", and the gateway
        #     plan's device/peer tiers (Stage 1/6) hang their credentials off the
        #     auth block, never off this one. Nothing here is secret: the id and
        #     the operator-set name travel in the clear on every greeting.
        #
        #     Mint-iff-absent, per root, and NOT the monitoring/telemetry
        #     ``install_id``s — those are rotatable and home/db-scoped, and the
        #     argument is written out in ``agent_runtime/gateway_identity.py``.
        install_block: dict[str, Any] = {
            "install_id": None,
            "display_name": None,
            "state": "error:root_unresolved",
        }
        if store_root_path is not None:
            try:
                from agent_runtime.gateway_identity import ensure_install_identity

                install_block = ensure_install_identity(store_root_path).frame_payload()
            except Exception as exc:
                install_block = {
                    "install_id": None,
                    "display_name": None,
                    "state": f"error:{type(exc).__name__}",
                }
        # 3. THE TRANSPORT (slice 3). One serve per root owns the socket lane,
        #    decided by an OS-held exclusive lock rather than by who booted
        #    first: two serves against one root is a real, ordinary concurrency
        #    (a launcher restart overlaps its replacement), and "connect to the
        #    service for root X" must have exactly one answer. The loser keeps
        #    serving stdio and SAYS so on the ready frame — a socket that
        #    silently never came up is indistinguishable from one that is
        #    broken.
        #
        #    Bound here, BEFORE the registry entry and the ready frame, so both
        #    can carry the real port; accepting starts later, once the request
        #    pool exists (see ``start_accepting`` below). A client that connects
        #    in between waits in the listen backlog, which is what a backlog is
        #    for.
        socket_block: dict[str, Any] = {"outcome": "disabled"}
        socket_transport = "stdio"
        if socket_lane and store_root_path is not None:
            try:
                from agent_runtime.serve_socket import (
                    SOCKET_HOST,
                    ServeSocketServer,
                    SocketOwnerLock,
                )
                from agent_runtime.serve_auth import read_token as _read_serve_token

                socket_lock = SocketOwnerLock(store_root_path)
                lock_result = socket_lock.acquire()
                if lock_result.acquired:
                    socket_server = ServeSocketServer(
                        store_root_path,
                        boot_id=boot_id,
                        # Late-bound on purpose: these two closures are defined
                        # further down (they need the pool and the drain state),
                        # and a Python closure resolves its enclosing names at
                        # CALL time — which cannot happen before the accept loop
                        # starts, which is after both exist.
                        dispatch_line=lambda line, connection: _handle_socket_line(
                            line, connection
                        ),
                        hello_payload=lambda message, connection: _hello_ok_frame(
                            message, connection
                        ),
                        # THIS root's secret, read per handshake and used as an
                        # HMAC key over a per-connection nonce. It is the key
                        # and never the message, so nothing derived from it and
                        # put on the wire discloses it — which is the whole
                        # reason the hello stopped carrying the token at all.
                        token_provider=lambda: _read_serve_token(store_root_path),
                        frame_contract=SERVE_SCHEMA_VERSION,
                        on_disconnect=lambda connection: _on_connection_closed(
                            connection
                        ),
                        log=_service_log,
                    )
                    port = socket_server.bind()
                    socket_lock.publish_owner(
                        {
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            "host": SOCKET_HOST,
                            "port": port,
                            "started_at": socket_server.started_at,
                            "store_root": runtime_root,
                        }
                    )
                    socket_transport = "stdio+socket"
                    socket_block = {
                        "outcome": "listening",
                        "host": SOCKET_HOST,
                        "port": port,
                        "started_at": socket_server.started_at,
                    }
                else:
                    socket_block = lock_result.payload()
            except Exception as exc:
                # A transport that failed to come up must not take the runtime
                # with it: stdio still works, and the typed outcome is how an
                # operator learns the socket did not.
                try:
                    if socket_lock is not None:
                        socket_lock.release()
                except Exception:
                    pass
                socket_server = None
                socket_lock = None
                socket_block = {"outcome": f"error:{type(exc).__name__}"}
        # 3b. THE SECOND DOOR (remote-gateway Stage 1). Off unless an operator
        #     names an interface in `remote_gateway.listen`, and the block SAYS
        #     which of those it is either way — `disabled` is a different fact
        #     from `error:port_in_use`, and a listener that silently failed to
        #     come up while the config said it should is the false-all-clear
        #     shape the `socket` block beside it already exists to retire.
        #
        #     Three things differ from the lane above and nothing else does: the
        #     bind (an operator-chosen interface and usually a fixed port), the
        #     credential (per DEVICE — `serve_gateway_auth`, not the per-root
        #     token), and the link (TLS, R1). Same dispatcher, same ops, same
        #     stream hub, same drain.
        # R-IP16 / R-S2-1: the capability list rides EVERY outcome, including
        # ``disabled``. "Does this hermes know the verb" and "is the LAN door
        # open" are different questions, and S3's request loop asks the first
        # over loopback argv against a serve that may legitimately have the
        # second answered ``no``. Stamped in exactly two places — here, and on
        # the listener's own block below — so ``ready`` / ``hello_ok`` /
        # ``version`` are untouched and cannot disagree.
        from agent_runtime.gateway_capabilities import with_capabilities

        gateway_block: dict[str, Any] = with_capabilities({"outcome": "disabled"})
        if socket_server is not None and store_root_path is not None:
            gateway_server, gateway_block = start_gateway_listener(
                store_root_path,
                boot_id=boot_id,
                display_name=install_block.get("display_name"),
                dispatch_line=lambda line, connection: _handle_socket_line(
                    line, connection
                ),
                hello_payload=lambda message, connection: _hello_ok_frame(
                    message, connection
                ),
                on_disconnect=lambda connection: _on_connection_closed(connection),
                log=_service_log,
                frame_contract=SERVE_SCHEMA_VERSION,
            )
            gateway_block = with_capabilities(gateway_block)
            if gateway_server is not None and socket_lock is not None:
                # RE-PUBLISH the ownership sidecar, now that the second door has
                # a real port. The first publish happens before this block on
                # purpose (the loopback port must be advertised as early as
                # possible, and the gateway lane must not be able to delay it),
                # so the gateway endpoint can only arrive in a second write.
                #
                # It has to arrive somewhere: `harness gateway pair` runs in the
                # operator's shell, not in this process, and the pairing payload
                # it prints has to name a port a phone can dial. With an
                # ephemeral port that number exists nowhere else — the registry
                # entry carries the LOOPBACK port, and the `ready` frame goes to
                # a launcher rather than to a terminal.
                socket_lock.publish_owner(
                    {
                        "pid": os.getpid(),
                        "boot_id": boot_id,
                        "host": SOCKET_HOST,
                        "port": socket_server.port,
                        "started_at": socket_server.started_at,
                        "store_root": runtime_root,
                        # Additive: a reader that predates this lane finds the
                        # keys it knows, unchanged and in the same places.
                        "gateway": {
                            "host": gateway_block.get("host"),
                            "port": gateway_block.get("port"),
                            "cert_fingerprint": gateway_block.get("cert_fingerprint"),
                        },
                    }
                )
        # S2c. ONE announce per boot, on a background thread, telling every
        # usable peer where this install is now reachable and what certificate
        # it presents. It is the push that makes a machine which changed
        # networks findable again without an operator re-running a ceremony they
        # had no reason to suspect was needed — the far side's cache endpoints
        # are tried before its pairing-time ones (`dial_peer`), so the new
        # address wins on the next call.
        #
        # Once per boot and never on a timer: this is news, and news that
        # repeats is a poll wearing a push's clothes. A peer that was off simply
        # rests `unreachable` in our cache until its own next hello refreshes
        # both sides.
        if gateway_block.get("outcome") == "listening" and store_root_path is not None:
            try:
                from agent_runtime.gateway_announce import announce_in_background
                from hermes_cli.harness_parts.gateway_commands import (
                    _candidate_endpoints,
                )

                announce_in_background(
                    store_root_path,
                    {
                        "endpoints": _candidate_endpoints(store_root_path),
                        "cert_fingerprint": gateway_block.get("cert_fingerprint"),
                        "display_name": install_block.get("display_name"),
                    },
                )
            except Exception:  # noqa: BLE001 — courtesy channel, never the boot
                pass

        # 4. DISCOVERY. Multiple runtime roots legitimately coexist on this
        #    machine (QA lanes, isolated worktree roots), and until now
        #    "how many serves are running against this root, on what code"
        #    had no answer at all. The entry is removed on every clean exit
        #    (shutdown AND drain); a crash leaves it, which is why liveness is
        #    proven at READ time and never trusted from the file.
        #
        #    The socket fields ride the SAME entry (additive): a client
        #    discovering "the service for root X" reads the port from the
        #    instance whose liveness the registry has just classified, rather
        #    than from a second file with its own staleness story.
        instance_block: dict[str, Any] = {"outcome": "error:root_unresolved"}
        if store_root_path is not None:
            try:
                from agent_runtime.serve_registry import register_serve_instance

                # WHICH HOME this child resolved (D-3). store_root answers a
                # DIFFERENT question — one root is shared by serves on
                # different profile homes — so from outside the process
                # nothing could say which home a running serve was on.
                # Resolved HERE, not in the registry: that module stays free
                # of hermes_constants and unit-testable against a string.
                # A resolution failure degrades to None (written as null);
                # bookkeeping must never be the thing that fails a boot.
                try:
                    from hermes_constants import get_hermes_home

                    resolved_home: str | None = str(get_hermes_home())
                except Exception:
                    resolved_home = None
                instance_block = register_serve_instance(
                    store_root_path,
                    transport=socket_transport,
                    build=build_block,
                    boot_id=boot_id,
                    port=socket_server.port if socket_server is not None else None,
                    socket_started_at=(
                        socket_server.started_at if socket_server is not None else None
                    ),
                    hermes_home=resolved_home,
                ).payload()
            except Exception as exc:
                instance_block = {"outcome": f"error:{type(exc).__name__}"}
            # ...and, having just written, drop the records that are provably
            # wreckage. AFTER registration on purpose: this serve's own entry
            # then exists and classifies `live`, so the sweep can never be the
            # thing that removes it.
            #
            # WHY THIS DOES NOT CONTRADICT "listing never prunes"
            # ---------------------------------------------------
            # serve_registry's docstring argues, correctly, that a READ must not
            # destroy the evidence it is reporting: an operator asking "why do I
            # have four serves" must see the wreckage. A boot is not that
            # moment. It is a WRITE moment - the line above just created a file
            # in this directory - and, decisively, the evidence does not vanish:
            # prune_stale_serve_instances returns pid, boot_id, path,
            # classification and reason for every record, deleted and kept
            # alike, and that report goes onto the service log correlatable by
            # boot_id against this boot's ready frame. The wreckage moves from a
            # directory nobody reads into a log the operator already reads.
            #
            # It is needed because clean exit removes its own entry and the
            # crash path deliberately does not - and the launcher's boot hygiene
            # sweep taskkill /F's orphan serves, which is a crash by
            # construction: those serves are never given the chance to
            # unregister. Measured on the operator's runtime: 14 serve boots in
            # ~19 h left 2 records behind (13856, 35080), while a third (21440)
            # exited cleanly and removed its own.
            #
            # SCOPE, stated plainly: this is tidiness plus forensics, not a
            # correctness fix. The leftover records are already harmless -
            # resolve_socket_target returns only rows classified `live`, the
            # launcher never reads this directory, and it is excluded from every
            # freshness fingerprint (see the module docstring).
            #
            # What is pruned is NOT widened here: stale_dead_pid only, which is
            # the registry's own rule. stale_recycled_pid names a live process
            # this registry no longer understands, and `unknown` means a probe
            # could not answer - deleting on a failed probe is how a sweep
            # removes a RUNNING service's record, and this repo has already been
            # bitten once by a recycled pid killing an unrelated process.
            #
            # Silent when it found nothing to do: a line every boot saying it
            # deleted zero files is the kind of noise that trains an operator to
            # stop reading the channel this report needs to be seen on.
            try:
                from agent_runtime.serve_registry import (
                    prune_stale_serve_instances,
                )

                prune_report = prune_stale_serve_instances(store_root_path)
                if prune_report.get("deleted_count") or any(
                    "error" in row for row in prune_report.get("kept") or ()
                ):
                    _service_log(
                        {
                            "event": "serve_instances_pruned",
                            "boot_id": boot_id,
                            "pid": os.getpid(),
                            **prune_report,
                        }
                    )
            except Exception:
                # Bookkeeping must never take a boot with it.
                pass
        timeline.mark("service_foundations_ms")
        # Orphaned-turn sweep BEFORE the ready frame: serve boot is the moment
        # a launcher restart replaces a dead runtime, and the first hydrate is
        # only requested after ready — so records a dead executor left frozen
        # in-flight (lease provably free) already project as typed
        # ``turn_interrupted`` markers in that hydrate instead of a console
        # stuck "running" forever. Bounded (≤50 session files) and fail-open.
        orphaned_repaired: list[str] = []
        try:
            from agent_runtime.persona_chat_continuity import repair_orphaned_chat_turns

            orphaned_repaired = repair_orphaned_chat_turns()
        except Exception:
            orphaned_repaired = []
        timeline.mark("orphaned_turn_sweep_ms")
        # Same moment, same reason, for detached dispatches: a row still marked
        # ``running`` whose owning process is provably gone can never finish, and
        # the sender is owed that answer too. Reclassifying it here — BEFORE the
        # drain starts — turns "the agent I dispatched went silent forever" into
        # a delivered "the outcome is unknown, re-send if you still need it".
        # Identity-verified (a recycled PID is not the old owner) and fail-open.
        dispatches_restored = 0
        try:
            from agent_runtime.dispatch_store import restore_undelivered_dispatches

            dispatches_restored = int(
                (restore_undelivered_dispatches() or {}).get("restored") or 0
            )
        except Exception:
            dispatches_restored = 0
        timeline.mark("dispatch_restore_ms")
        ready_frame: dict[str, Any] = {
            "event": "ready",
            "pid": os.getpid(),
            "schema_version": SERVE_SCHEMA_VERSION,
            "runtime_root": runtime_root,
            # Additive, always present (never conditional on success): a
            # missing block would read as "old runtime", while a block whose
            # own fields say `unknown`/`error:…` reads as what it is — the
            # measurement was attempted and this is what it found.
            "boot_id": boot_id,
            "build": build_block,
            "auth": auth_block,
            # WHICH INSTALL this is, by the same "always present, states its own
            # outcome" rule as ``auth`` above — a picker with two installs in it
            # needs a stable id and a human name, and absence would be
            # indistinguishable from a runtime that predates the lane.
            "install": install_block,
            "instance": instance_block,
            # ``disabled`` (no socket lane asked for), ``listening`` with the
            # port, ``lock_held_by`` with the winner's pid, or ``error:<reason>``
            # — the outcome is stated either way, never inferred from absence.
            "socket": socket_block,
            # The SECOND door, by the same rule and for a sharper reason. An
            # operator who sets ``remote_gateway.listen`` and restarts has one
            # question — can my phone reach this install — and every way the
            # answer is no is quiet: the port was taken, the certificate could
            # not be minted, the config key was never read. ``disabled`` when
            # nobody asked, ``listening`` with host/port and the
            # ``cert_fingerprint`` a client pins, ``error:<reason>`` otherwise.
            # Never absent, never inferred.
            "gateway": gateway_block,
            # The METHOD lane's capability manifest — ``{"contract":N,
            # "methods":[…]}``. This is stdio's greeting, so this is where a
            # stdio client learns the method set; the socket's equivalent is
            # ``hello_ok``, and both are restated on the re-askable ``version``
            # reply. Same shape of promise as ``hello_contract``: the server
            # advertises, the client asserts, and a runtime that predates the
            # lane carries no ``rpc`` key at all — which reads as "argv only"
            # rather than as a failure.
            "rpc": serve_rpc.manifest(),
            # The OP lane's half of the same promise (TC-1/C-1). ``ready`` is
            # stdio's greeting, so this is where a stdio client learns that
            # ``{"op":"subscribe","lane":"stream"}`` is carried here rather than
            # having to send one and read the answer's tea leaves.
            "ops": ops_manifest(transport="stdio"),
        }
        if orphaned_repaired:
            ready_frame["orphaned_turns_repaired"] = len(orphaned_repaired)
        if dispatches_restored:
            ready_frame["dispatches_restored"] = dispatches_restored
        # Every phase this boot actually paid, on the frame the supervisor
        # already waits for — and the same line in agent.log, because the boot
        # worth attributing (the cold one) is the boot nobody is watching a
        # console for. Emission is defensive: a broken instrument must never be
        # the reason a runtime fails to come up.
        try:
            ready_frame["boot_timeline"] = timeline.stamps()
        except Exception:
            pass
        # Read-model warmup starts BEFORE ``ready`` is announced, unlike the
        # provider warmup below. The launcher's first request lands within
        # milliseconds of this frame, and only the build that STARTED FIRST can
        # be shared: if the request wins the race it leads its own build and
        # the warmup then queues a second, redundant one behind it. Starting a
        # daemon thread costs microseconds, so ``ready`` is not delayed.
        #
        # ONE thread, and the provider warmup runs on it AFTER the build (EG-3.2,
        # two independent investigations reaching the same fix: HY-H2 = HC-H3).
        # It used to be a second daemon thread started just after ``ready``, and
        # under the GIL its ~5-8s of CPU — OpenAI SDK import, SSL context, and
        # since BW-H3 the ``model_tools`` import plus the discovery/check_fn storm
        # — was subtracted from the build the launcher's canvas is waiting on.
        # Nothing it warms is consumable before that canvas is authoritative: its
        # purpose is the FIRST CHAT TURN's latency, which is after.
        #
        # The brief's one-line version of this fix — reorder the two
        # ``Thread.start()`` calls — was REFUSED as a no-op by both sources
        # independently: starts issued microseconds apart schedule nothing, and
        # the provider prewarm reached ``model_tools`` ~5s in either way.
        #
        # Named cost, carried rather than hidden: a chat turn sent inside the (now
        # shorter) boot window pays the cold SDK import inline, exactly as every
        # turn did before the prewarm existed — best effort by the prewarm's own
        # contract. If receipts show first-turn misses, the refinement is
        # "provider prewarm starts at first-request-enqueue OR build-completion,
        # whichever is first", not a revert.
        # Since 2026-08-23 a THIRD step rides the same thread, last: the
        # persona-chat actor prewarm (Stage 2 of `planned/chat-turn-prep-cost`).
        # Last on purpose — it queues agent constructions, and a construction
        # that runs after the provider warmup does not pay the SDK import it
        # would otherwise pay itself. It only QUEUES here; the constructions run
        # on that module's own worker, which stands down for any live turn.
        if (
            snapshot_prewarm is not None
            or provider_prewarm is not None
            or actor_prewarm is not None
        ):

            def _prewarm_worker() -> None:
                # Sequential, and each step isolated: a build that raised must
                # still leave the providers warm (HY-H2), and an injected fake
                # that raises must not silently cancel the step after it.
                for step in (snapshot_prewarm, provider_prewarm, actor_prewarm):
                    if step is None:
                        continue
                    try:
                        step()
                    except Exception:
                        try:
                            import logging as _logging

                            _logging.getLogger(__name__).debug(
                                "serve prewarm step did not complete", exc_info=True
                            )
                        except Exception:
                            pass

            threading.Thread(
                target=_prewarm_worker,
                name="harness-serve-prewarm",
                daemon=True,
            ).start()
        frames.emit(ready_frame)
        try:
            import logging as _logging

            _logging.getLogger(__name__).info(
                timeline.log_line("harness serve boot timeline:")
            )
        except Exception:
            pass
        # (Both warmups run on the single thread started just before the ready
        # frame above — the read-model build first, then the chat turn's one-time
        # costs. The invariant the two-thread arrangement was written to protect
        # is now provable rather than raced: the build cannot queue behind the
        # ~3s SDK import, because that import has not started yet.)
        # A busy serve must never look dead. The launcher's stream watchdog
        # keys on "no frames for N seconds", and when pool workers are deep in
        # chat-turn work the infinite `stream` request's generator can starve
        # past that budget — Mission Control then raised the loud "Runtime
        # offline" banner DURING healthy turns (live incident 2026-07-23,
        # two flaps inside one 4-minute Neko turn). This dedicated thread
        # emits the same typed `busy` frame the `ping` op returns whenever
        # requests are in flight: pure liveness telemetry on the shared
        # stdout, independent of every pool worker, so the launcher can
        # distinguish "busy running your turn" from "gone".
        liveness_stop = threading.Event()

        def _liveness_pump() -> None:
            while not liveness_stop.wait(liveness_pump_interval_seconds):
                with inflight_lock:
                    pending = list(inflight.values())
                if not pending:
                    continue
                try:
                    busy_frame = _busy_frame()
                    frames.emit(busy_frame)
                except Exception:
                    # Writer gone — the main loop is on its way down too.
                    return
                # And to every SOCKET client, for the identical reason the
                # comment above gives for stdout. This lane was left behind
                # when the drain path learned the same lesson: "the socket
                # client IS such a watchdog: it reads with a finite timeout and
                # reports `transport_failed` on silence" (see the drain's
                # `_broadcast_lanes(progress)`). Measured 2026-08-27: an
                # authenticated socket connection waiting on a `characters
                # list` read ZERO frames for >120s while this pump was emitting
                # `busy` the whole time — to stdout, where that client could
                # not see it.
                #
                # Best-effort and never fatal: `_broadcast_lanes` is a later
                # local of this loop, so an early tick can find it unbound, and
                # a broadcast failing must not stop the liveness the launcher
                # keys on.
                try:
                    _broadcast_lanes(busy_frame)
                except Exception:
                    pass
                # The per-request half: `busy` is a count, and a client waiting
                # on ONE id needs to know whether that id has started.
                _report_quiet_requests(pending)

        threading.Thread(
            target=_liveness_pump,
            name="harness-serve-liveness",
            daemon=True,
        ).start()
        # Detached-dispatch delivery. This is the half that makes
        # `agent_chat_send(wait=false)` honest: the target's turn ran in the
        # background, its answer is durable, and this thread forges it back into
        # the SENDER's thread once that thread is idle. It lives HERE, and only
        # here, because serve is the one long-lived process that hosts persona
        # turns — a one-shot CLI exits long before a 30-minute dispatch lands.
        #
        # Started after `ready` (so a cold boot is never delayed by a delivery)
        # and stopped with the liveness pump before `shutdown` (so it cannot
        # forge a turn into a process that is on its way down). Best effort by
        # contract: a runtime that cannot start the drain still serves, and the
        # completions stay pending for the next boot rather than being lost.
        try:
            from agent_runtime.dispatch_delivery import start_delivery_drain

            # Rehydrate durable delegation completions BEFORE the drain that
            # will deliver them starts — explicit at serve boot, never as an
            # import side effect (same #16856 class as module-scope MCP
            # discovery; see
            # docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/eager-tool-discovery-audit-2026-08-09.md).
            from tools.process_registry import process_registry

            process_registry.restore_durable_completions()
            start_delivery_drain(stop_event=liveness_stop)
        except Exception:
            # Function-local: parts files are exec'd into harness.py's globals,
            # which carry no module logger.
            #
            # WARNING, not debug: a drain that fails to start disables the
            # entire `agent_chat_send(wait=false)` lane for the life of this
            # serve — every dispatch is refused with
            # `async_delivery_unavailable` — and at debug level that
            # feature-killing fact was invisible in every live log
            # (2026-08-09 dispatch-lane investigation).
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "dispatch delivery drain did not start; "
                "agent_chat_send(wait=false) will be refused for this serve",
                exc_info=True,
            )

        def _unregister_instance() -> None:
            """Drop this serve's registry entry. Idempotent, never raises."""

            if store_root_path is None:
                return
            try:
                from agent_runtime.serve_registry import (
                    serve_instance_path,
                    unregister_serve_instance,
                )

                if not unregister_serve_instance(store_root_path):
                    # Reported, not swallowed. A clean exit that leaves its
                    # entry behind makes the registry claim a serve that is on
                    # its way out, and the next client's discovery would try to
                    # connect to it. The read-time classification eventually
                    # calls it dead — "eventually" is the part an operator has
                    # to be able to see coming.
                    if serve_instance_path(store_root_path, os.getpid()).exists():
                        _service_log(
                            {
                                "event": "serve_instance_unregister_failed",
                                "boot_id": boot_id,
                                "pid": os.getpid(),
                                "path": str(
                                    serve_instance_path(store_root_path, os.getpid())
                                ),
                            }
                        )
            except Exception:
                pass

        # ── socket lane plumbing ────────────────────────────────────────────
        #
        # Everything below is inert on a stdio-only serve: ``socket_server`` is
        # None, no connection ever exists, and the stdio path never reaches a
        # branch that touches it.

        connection_sinks: dict[str, _SafeSink] = {}
        connection_sinks_lock = threading.Lock()

        def _emit_safely(sink: Any, frame: dict[str, Any]) -> None:
            try:
                sink.emit(frame)
            except Exception:
                pass

        def _sink_for(connection: Any) -> Any:
            """The STABLE per-connection request sink.

            Stable matters twice: the partial-line buffers in
            ``_LineFrameProxy`` are keyed on the sink's identity, and a sink
            rebuilt per line would split one handler's output across two
            buffers mid-line.
            """

            if connection is None:
                return frames
            with connection_sinks_lock:
                sink = connection_sinks.get(connection.key)
                if sink is None:
                    sink = _SafeSink(connection)
                    connection_sinks[connection.key] = sink
                return sink

        def _owner_of(connection: Any) -> str:
            return "stdio" if connection is None else str(connection.key)

        def _accepted_fold_entities() -> Any:
            """What the SHARED producer may promote: the intersection of every
            attached subscriber's declaration.

            One producer feeds N subscribers (``serve_stream_hub``), so a patch
            frame promoted for a client that declared ``office_actor`` would ALSO
            be fanned out to whoever sits next to it, and a subscriber that
            cannot fold that entity answers with a full re-hydrate. Intersection
            is the only rule under which a promotion is safe for everyone in the
            room; a client that declared nothing contributes the historical set,
            so a room of only today's clients accepts exactly today's set.

            The room is BOTH LANES. ``stream_fold_entities`` holds the socket
            stream lane's declarations, but an RPC office subscriber
            (``serve_office_subscriptions``) registers against this same hub and
            is fanned exactly the same frames — it is an attached subscriber in
            every sense that matters here. Reading only the stream table is how
            ``office_actor`` was never once promoted in production: an
            office-only room resolved to the historical default, every office
            write demoted to a full core, and the push lane could emit nothing
            but resync. Both tables are read, so the intersection is taken over
            everyone actually attached.

            **Read LIVE, once per drain pass, not once per producer.** It used to
            be producer-build-time, and the note here said a LEAVE deliberately
            does not re-widen the running producer because re-widening would mean
            RESTARTING it — charging every remaining subscriber a fresh full core
            to buy back a promotion they were living without. That trade is gone:
            ``stream_frames`` takes this derivation as ``fold_room`` and re-reads
            it between drains, so a leave re-widens for free and a JOIN is
            noticed by a producer nobody restarted.

            The second half is what makes a restart-free join safe at all. A
            joiner that folds LESS than the frozen floor would otherwise be
            handed bare patches inside that floor and answer them with
            re-hydrates — the promotion regression this negotiation exists to
            prevent, arriving through the door built to avoid a restart. Read
            live, the next pass sees the narrowed floor and splits instead.

            The window that remains is one drain pass wide: a batch already
            GATED when a declaration lands can still go out bare. It costs the
            joiner one resync and cannot lose an event — the client's
            ``base_offset`` gate refuses a patch it cannot chain — and it is
            named here rather than left for a reader to find.
            """

            from agent_runtime.patch_coverage import accepted_fold_entities

            return accepted_fold_entities(_room_fold_declarations())

        def _room_fold_declarations() -> list[Any]:
            """Every attached subscriber's declaration, both lanes, once.

            Was inline in ``_accepted_fold_entities``; lifted out when a SECOND
            operator over the same room arrived (``_promoted_fold_entities``'s
            union). Two readers assembling the same list from the same two tables
            is how one of them quietly stops reading the office registry, which is
            the bug the intersection already shipped once.
            """

            from agent_runtime.serve_office_subscriptions import OFFICE_SUBSCRIPTIONS

            with lane_lock:
                declarations = list(stream_fold_entities.values())
            # Taken OUTSIDE ``lane_lock``: the registry holds a lock of its own,
            # and the two are never nested in the opposite order anywhere.
            declarations.extend(OFFICE_SUBSCRIPTIONS.declarations())
            return declarations

        def _promoted_fold_entities() -> Any:
            """What the shared producer may promote for SOMEBODY: the UNION.

            R10's assigned consequence, closed here. The intersection above is
            the only safe rule while a fan-out can deliver exactly one shape of a
            frame, and it has a cost the drop-latency tables did not price: a
            Stage 5 phone declaring a narrow chat-first fold DEMOTES the desktop
            beside it to a full ~1 MB core on every office write. Correct, and
            paid by the client that did nothing.

            Now a batch can go out as a ``fold_variants`` envelope — the promoted
            patch and the demoted core together — and each subscriber's pump
            resolves it against its own declaration
            (:func:`agent_runtime.stream.resolve_fold_variant`). So the producer
            promotes whenever ANYBODY can fold, and the demotion is per
            subscriber. The intersection is still derived and still shipped, as
            the ROOM'S FLOOR on the hydrate's echo — a value true for every
            recipient of a frame that is fanned to all of them.

            **With one subscriber these two functions return the same set**, the
            envelope is never built, and the wire does not move by a byte. That
            is not a convention: ``_batch_frames_with_liveness`` takes the
            promoted branch only when the floor REFUSED a batch the union
            accepts, which an equal pair cannot produce.
            """

            from agent_runtime.patch_coverage import union_fold_entities

            return union_fold_entities(_room_fold_declarations())

        def _room_wants_stale_first() -> bool:
            """Does anybody attached to the shared producer PAINT a whole core?

            Same room as ``_accepted_fold_entities`` above — both lanes, read at
            producer-build time — and deliberately the OPPOSITE operator, which
            is the sentence worth keeping. Intersection is right there because a
            PROMOTION must be safe for everyone fanned the frame: one subscriber
            that cannot fold ``office_actor`` makes the promotion wrong for the
            room. Union is right here because the stale-first hydrate is an EXTRA
            frame that a non-painting subscriber merely ignores: the office sink
            discards every row that is not an ``office_actor`` under its own
            workspace, so a stale core costs it a discard and costs the painting
            subscriber beside it the whole point of EG-3.1. One painter is enough;
            a room of office-only sinks answers False, and the boot's single
            stale core stays available for the argv lane the launcher is actually
            on (measured 2026-08-18: the office subscribe attaches 0.1–0.2s
            first, and under the old process-global one-shot it won two boots in
            three and threw the paint away).

            The predicate is membership in ``stream_fold_entities``, not its
            values: that table is the socket STREAM lane's, one entry per
            subscribed connection, and a stream subscriber is by construction a
            consumer of whole hydrate/delta frames. The office registry's
            subscribers contribute False — the union over an empty set of
            painters is False — which is why they are not read here at all.
            """

            with lane_lock:
                return bool(stream_fold_entities)

        #: Does an INJECTED source factory want the negotiated fold set? Answered
        #: once, by signature — never by calling it and catching ``TypeError``,
        #: which would swallow a TypeError raised INSIDE a zero-arg factory and
        #: retry it at a different arity (the reasoning ``serve_stream_hub``
        #: records for its own stop-event probe, one seam up).
        stream_factory_takes_fold_entities = False
        if stream_source_factory is not None:
            try:
                inspect.signature(stream_source_factory).bind(frozenset())
                stream_factory_takes_fold_entities = True
            except (TypeError, ValueError):
                stream_factory_takes_fold_entities = False

        def _stream_source(stop: Any = None) -> Any:
            """The shared subscription producer. One per serve, never per client.

            Takes the hub's per-GENERATION stop event (the hub probes for it by
            signature — ``serve_stream_hub._accepts_stop_argument``) and hands it
            to the runtime's own cancellation seam, which is what makes an
            abandoned generation stop before its next frame instead of after it.

            WHY THE SEAM AND NOT A CHECK BETWEEN FRAMES. Checking ``stop`` around
            the ``yield`` here buys NOTHING, and measuring it is the only way to
            know that: ``StreamHub._produce`` already tests ``_should_stop``
            immediately after every ``next()``, so a wrapper that tested the same
            flag at the same moment would be a second copy of a check that had
            already been made. Measured on the real producer at production
            cadences, both spellings left the producer thread alive for 3.08s past
            ``hub.stop(join_timeout=2.0)`` — identical to no fix at all. A fence
            that changes nothing while looking staffed is worse than an absent
            one.

            The park is INSIDE ``next()``: ``stream_frames`` polls its event tail
            every 250ms and only YIELDS on a frame, so a quiet lane surfaces once
            per 5s heartbeat and nothing outside can interrupt the gap. The one
            thing that can is ``request_control``, the seam that module exists for
            — "the read-only ``harness stream`` handler is infinite and must
            release its worker when its consumer disconnects", which is this
            situation exactly, one caller over. Bound to the stop event, every
            ``request_cancelled()`` probe inside the tail loop (its bounded sleep
            slices at 100ms, and the snapshot-build wait beside it) becomes a
            probe of THIS generation's liveness, and the generator returns
            cooperatively at its own next safe point. Same measurement,
            afterwards: ``hub.stop()`` returns with zero producers alive.

            The ``stop`` default keeps the factory callable with no argument (a
            direct caller, and the hub itself if the probe ever stops matching),
            in which case the scope is bound to an event nobody sets and the
            behaviour is exactly what it was.

            An INJECTED ``stream_source_factory`` is deliberately not handed the
            event: its arity contract is the fold-set one negotiated above, and a
            test fake owns its own lifecycle by construction.
            """

            fold_entities = _accepted_fold_entities()
            # The union, derived beside the floor and with the same lifetime, so
            # a join that widens the room re-derives both together. An INJECTED
            # factory is deliberately not handed it: its arity contract is the
            # one-set one negotiated above, and a test fake that wanted the split
            # lane would be testing the hub rather than itself.
            promote_entities = _promoted_fold_entities()
            # Derived HERE, beside the fold set, for the same reason and with the
            # same lifetime: ``StreamHub.subscribe`` restarts the producer, so
            # every join re-derives it. That restart is what makes the boot work
            # under the office-first ordering — the first generation is built for
            # an office-only room and takes nothing, and the painting subscriber's
            # own join builds the generation that does take it.
            wants_stale_first = _room_wants_stale_first()
            if stream_source_factory is not None:
                return (
                    stream_source_factory(fold_entities)
                    if stream_factory_takes_fold_entities
                    else stream_source_factory()
                )
            from agent_runtime.request_control import request_cancel_scope
            from agent_runtime.serde import to_jsonable
            from agent_runtime.stream import stream_frames

            # Never-set stand-in for the no-argument call, so the body below has
            # ONE shape rather than a scoped and an unscoped variant to keep in
            # step.
            generation_stop = stop if stop is not None else threading.Event()

            def _generate():
                # The scope is entered on the PRODUCER thread — this body runs
                # there, and a fresh thread starts with an empty context, so
                # nothing of serve's own dispatch is being overwritten and the
                # reset on close lands in the same context that set it.
                with request_cancel_scope(generation_stop):
                    # ``caller="hub"``: every build this producer pays for is
                    # attributed to the SHARED lane rather than to whichever
                    # subscriber happened to trigger the restart — the serve hub
                    # is one producer for N subscribers by construction, and a
                    # build line naming a subscriber would be a lie about who
                    # pays.
                    for frame in stream_frames(
                        fold_entities=fold_entities,
                        promote_fold_entities=promote_entities,
                        # The LIVE room, re-read by the producer once per drain
                        # pass. The two sets above still seed the hydrate's echo
                        # (resolved once, so a client's ack and its baseline
                        # cannot disagree); this is what a batch is promoted
                        # against, and it is what lets a restart-free join —
                        # the office lane's, and a watermark resume's — be
                        # noticed by a producer nobody restarted.
                        fold_room=lambda: (
                            _accepted_fold_entities(),
                            _promoted_fold_entities(),
                        ),
                        caller="hub",
                        wants_stale_first=wants_stale_first,
                    ):
                        # Byte-for-byte the frames ``harness stream`` writes: a
                        # subscriber folds the same hydrate/delta/patch/heartbeat
                        # shapes it already folds, so the socket lane introduces
                        # no second stream contract to keep in sync.
                        yield to_jsonable(frame)

            return _generate()

        def _ensure_stream_hub() -> Any:
            nonlocal stream_hub
            with lane_lock:
                if stream_hub is None:
                    from agent_runtime.serve_stream_hub import (
                        DEFAULT_BUFFER_LIMIT,
                        DEFAULT_BYTE_LIMIT,
                        StreamHub,
                    )

                    stream_hub = StreamHub(
                        _stream_source,
                        buffer_limit=int(stream_buffer_limit or DEFAULT_BUFFER_LIMIT),
                        byte_limit=int(stream_byte_limit or DEFAULT_BYTE_LIMIT),
                        log=_service_log,
                    )
                return stream_hub

        # The office push lane reaches the hub through a FACTORY, not a handle:
        # `_ensure_stream_hub` builds lazily and `_close_socket_lane` can swap
        # the hub out across a drain, so a captured instance would go stale at
        # exactly the moment a client reconnects. Binding the factory also means
        # an RPC subscriber COUNTS as a hub subscriber — load-bearing, because
        # the hub stops producing when its room empties, and the whole point of
        # the ruling is that the launcher stops joining the legacy stream.
        from agent_runtime.serve_office_subscriptions import OFFICE_SUBSCRIPTIONS

        # `log` is where a RE-BASELINE is billed. A second subscribe on one
        # connection replaces the first rather than being refused, which cures a
        # client stuck holding a baseline it refused — but `StreamHub.subscribe`
        # restarts the producer, so a re-baseline makes every OTHER subscriber
        # on this hub pay a fresh full core. The client sees `replaced` on its
        # own reply; without this line the operator would see a retry loop only
        # as an unexplained climb in the hub's generation counter.
        #
        # `_accepted_fold_entities` rides along because a restart-free rejoin is
        # only safe when the joiner is not NARROWING what the room may promote,
        # and the room is BOTH LANES — the stream lane's declaration table is
        # this closure's, unreachable from a process-global registry. Lending
        # the derivation keeps one authority for what the room accepts; reading
        # only one of the two tables is the mistake this lane already made once.
        OFFICE_SUBSCRIPTIONS.bind(
            _ensure_stream_hub,
            log=_service_log,
            accepted_fold_entities=_accepted_fold_entities,
        )

        def _release_subscription(connection: Any) -> None:
            """A client left. Unsubscribe it, and do NOTHING else.

            Not a cancellation, not a shutdown, not a state change: the runtime
            outliving its clients is the entire point of the durable service,
            and a disconnect that touched backend state would reintroduce the
            per-client lifecycle ownership this workstream exists to retire.
            """

            key = _owner_of(connection)
            with lane_lock:
                hub = stream_hub
                # A departed client's fold declaration must not keep narrowing
                # the lane for the clients that remain — the next subscribe
                # re-derives the accepted set from whoever is actually here.
                stream_fold_entities.pop(key, None)
            if hub is not None:
                try:
                    hub.unsubscribe(key)
                except Exception:
                    pass
            # The office lane's keys are NAMESPACED away from `key` (which the
            # stream lane owns), so the unsubscribe above cannot reach them and
            # a departing connection would otherwise leak a subscriber — which
            # would in turn keep a producer alive for nobody.
            from agent_runtime.serve_office_subscriptions import (
                OFFICE_SUBSCRIPTIONS as _office_subs,
            )

            _office_subs.release(key)
            # S2d's lane is namespaced away from both of the above for the same
            # reason the office lane is, so a departing connection would
            # otherwise leave a sink the fan-out keeps writing to — which is how
            # a push registry starts holding a dead socket open.
            from agent_runtime.serve_gateway_peers_rpc import (
                PEER_DIRECTORY_SUBSCRIPTIONS as _peer_dir_subs,
            )

            _peer_dir_subs.release(key)
            if connection is not None:
                connection.subscribed = False
                with connection_sinks_lock:
                    connection_sinks.pop(connection.key, None)

        def _reclaim_abandoned_streams(connection: Any) -> int:
            """Cancel the departed connection's infinite ``harness stream``.

            THE POOL IS FOUR WORKERS WIDE and ``harness stream`` is an argv
            request that never returns, so every abandoned one is a worker
            permanently gone. The ``cancel`` op's own comment says what that
            costs — "otherwise four watchdog cycles exhaust the entire serve
            pool with abandoned streams" — and until now the ONLY thing that
            set the event was that op, sent by a launcher that came BACK. A
            client that simply died, or a socket session that closed, left its
            stream running forever, and the next argv request queued behind a
            pool with no free worker and emitted nothing at all. That is the
            measured 2026-08-27 shape: >120s of zero frames for a ``characters
            list``, the identical argv answered in ~6s on a later connection.
            ``agent_runtime.request_control`` already states this as the
            contract — the stream handler "must release its worker when its
            consumer disconnects" — and nothing implemented the disconnect half.

            Deliberately narrower than ``_release_subscription``'s "do NOTHING
            else", and not a softening of it: that rule is about BACKEND state
            surviving its clients, which is the whole durable-service premise.
            This touches no backend state. It reclaims a worker that is
            producing frames for a socket nobody is reading, and it reclaims it
            for exactly the one request shape the cancel path already calls the
            sole safe cooperative exception — read-only, infinite, with a
            polled seam. Chat turns and every mutation are untouched: they stay
            uninterruptible, because a half-applied mutation is worse than a
            held worker and a killed turn is lost recording.
            """

            if connection is None:
                return 0
            owner = _owner_of(connection)
            with inflight_lock:
                abandoned = [
                    request
                    for request in inflight.values()
                    if request.is_runtime_stream and request.owner == owner
                ]
            for request in abandoned:
                request.cancel_event.set()
            if abandoned:
                _service_log(
                    {
                        "event": "serve_stream_worker_reclaimed",
                        "boot_id": boot_id,
                        "connection": owner,
                        "client": getattr(connection, "client", None),
                        "request_ids": sorted(item.rid for item in abandoned),
                    }
                )
            return len(abandoned)

        def _on_connection_closed(connection: Any) -> None:
            """The ONE disconnect path: unsubscribe, then reclaim the worker.

            Both doors call this rather than ``_release_subscription`` on its
            own, so a lane added later cannot get one half and not the other —
            the same reasoning ``_broadcast_lanes`` is written down with.
            """

            _release_subscription(connection)
            _reclaim_abandoned_streams(connection)

        def _broadcast_lanes(frame: dict[str, Any]) -> None:
            """Tell every attached client, on whichever door it came through.

            One call site per announcement rather than two, because the failure
            mode of two is silent and asymmetric: a drain that reached the
            loopback launcher and not the paired phone leaves the phone waiting
            on a runtime that has gone, and nothing anywhere says so. Every
            broadcast in this loop goes through here, so a lane added later is
            added once.
            """

            for server in (socket_server, gateway_server):
                if server is None:
                    continue
                try:
                    server.broadcast(frame)
                except Exception:
                    pass

        def _close_socket_lane(reason: str) -> None:
            """Stop the hub, close every connection, release the ownership lock.

            Idempotent and never raises: it runs on the drain path, the
            shutdown path, and the EOF path, and any of them may be second.
            """

            nonlocal socket_server, socket_lock, stream_hub, gateway_server
            # Unbound FIRST, so a subscribe racing the drain is refused with a
            # typed `push_lane_unavailable` instead of registering against a hub
            # that is about to be stopped. The registry is process-global and
            # outlives this loop, so leaving it bound would also hand the next
            # serve_loop in the same process a factory closed over a dead lane —
            # which is a test-suite failure mode, not only a production one.
            from agent_runtime.serve_office_subscriptions import (
                OFFICE_SUBSCRIPTIONS as _office_subs,
            )

            _office_subs.bind(None)
            # The three swaps happen together, under the lock, and NOTHING
            # slow happens while it is held: whoever takes a handle owns
            # closing it, and a second caller gets None and does nothing.
            with lane_lock:
                hub, stream_hub = stream_hub, None
                server, socket_server = socket_server, None
                # The gateway listener is swapped under the SAME lock and by the
                # same closer. It has no lock and no registry entry of its own —
                # it is the loopback lane's dispatcher answering on a second
                # door — so a teardown that closed one and not the other would
                # leave a runtime that has drained still accepting devices.
                gateway, gateway_server = gateway_server, None
                lock, socket_lock = socket_lock, None
                stream_fold_entities.clear()
            if gateway is not None:
                try:
                    gateway.close(reason=reason)
                except Exception:
                    pass
            if hub is not None:
                try:
                    # One TOTAL budget for the hub, not one per subscriber
                    # join: the drain's exit watchdog is already armed, and a
                    # teardown that can outlast it is how a drained runtime
                    # kept running.
                    hub.stop()
                except Exception:
                    pass
            if server is not None:
                try:
                    server.close(reason=reason)
                except Exception:
                    pass
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

        def _build_mismatch(client_build: Any) -> bool | None:
            """Does the client's build disagree with the code answering it?

            None means NOT COMPARABLE — the client named no build, or this
            runtime could not measure its own. A fabricated ``false`` there
            would answer "you are current" for a runtime that does not know,
            which is exactly the false-all-clear the build stamp exists to
            retire. Prefix comparison so a short hash and a full one agree.
            """

            serve_commit = build_block.get("commit")
            if not isinstance(serve_commit, str) or not serve_commit:
                return None
            if not isinstance(client_build, str) or len(client_build.strip()) < 7:
                return None
            claimed = client_build.strip().lower()
            actual = serve_commit.lower()
            return not (
                actual.startswith(claimed) or claimed.startswith(actual)
            )

        def _hello_ok_frame(message: dict[str, Any], connection: Any) -> dict[str, Any]:
            """The version handshake, enforced end to end at the door."""

            from agent_runtime.serve_socket import HELLO_CONTRACT_VERSION

            return {
                "event": "hello_ok",
                "pid": os.getpid(),
                "boot_id": boot_id,
                # The frame-protocol contract this service speaks. A client that
                # does not recognise it must not proceed on hope.
                "contract": SERVE_SCHEMA_VERSION,
                # Restated from ``server_hello`` so a client that reconnects and
                # reads only the reply still learns which handshake it just
                # completed.
                "hello_contract": HELLO_CONTRACT_VERSION,
                "schema_version": SERVE_SCHEMA_VERSION,
                # Which DOOR this client came through, read off the connection
                # rather than written as a constant. It was "socket" when there
                # was one listener; a device reading "socket" here would be told
                # it is on the local lane, and `ops` below would then advertise
                # a verb this connection is refused.
                "transport": connection.transport,
                "connection": connection.key,
                "runtime_root": runtime_root,
                "build": build_block,
                # The socket greeting's half of the install identity. A socket
                # client never reads ``ready``, and from Stage 1 a REMOTE client
                # reads nothing else — which install it just reached has to be
                # answerable from the handshake it already performs, not from a
                # ``runtime_root`` path that means nothing on another machine.
                "install": install_block,
                # Visible, never fatal: a client on other code still gets to
                # work, and now KNOWS it is talking to a different build.
                "build_mismatch": _build_mismatch(connection.client_build),
                "draining": drain_state is not None,
                # The socket's half of the method-lane advertisement. A socket
                # client never reads ``ready`` (that frame goes to the stdio
                # owner), so without this it could only learn the method set by
                # asking ``version`` — one extra round trip on every connect,
                # for something the handshake it already performs can carry.
                "rpc": serve_rpc.manifest(),
                # Same argument, the OP lane's half — and the place the
                # advertisements differ per door: ``shutdown`` is refused on
                # both sockets, and ``drain`` additionally on the gateway one.
                # A device learns what it may ask by MEMBERSHIP rather than by
                # trying and reading an error.
                "ops": ops_manifest(transport=connection.transport),
                # What the SECOND door is doing, on the greeting a client
                # already reads. For a device this is the lane it is standing
                # on; for the local launcher it is the answer to "is this
                # install reachable from my phone", which nothing else on this
                # frame can give it.
                "gateway": gateway_block,
                # The ONE frame in this lane that ever carries a secret, and it
                # carries it exactly once: the credential a pairing code was
                # just redeemed for. Read-and-CLEAR, so the value is gone from
                # the connection before this function returns and cannot reach
                # `payload()`, a log line, or a second reply. Absent on every
                # other handshake, which is every handshake after the first.
                **_pairing_block(connection),
            }

        def _connections_frame() -> dict[str, Any]:
            # S2c (R-S2-8). One stat on a read this frame was making anyway.
            # The serve is the process that NOTICES an external write because it
            # is the one that reads repeatedly; a fresh CLI process seeds on its
            # first read and emits nothing, having no baseline to claim a change
            # against.
            if store_root_path is not None:
                try:
                    from agent_runtime.gateway_peers import note_peer_store_read

                    note_peer_store_read(store_root_path)
                except Exception:
                    pass
            payload: dict[str, Any] = {"event": "socket_connections", "boot_id": boot_id}
            with lane_lock:
                server = socket_server
            if server is None:
                payload["enabled"] = False
                payload["socket"] = socket_block
                payload["count"] = 0
                payload["connections"] = []
            else:
                payload["enabled"] = True
                payload.update(server.connections_payload())
            with lane_lock:
                gateway = gateway_server
            # The gateway lane gets its OWN sub-block rather than having its
            # rows merged into the list above, and the reason is that the
            # top-level keys are per-listener facts: `port`, `host`, `count`,
            # `max_connections`, `rejected_by_reason`. Merged, every one of them
            # would answer for two listeners at once and none of them would say
            # which. Additive and absent-when-off, so every existing consumer of
            # this frame reads exactly the shape it was written against.
            if gateway is not None:
                payload["gateway"] = {
                    "enabled": True,
                    **gateway.connections_payload(),
                }
            else:
                payload["gateway"] = {"enabled": False, "outcome": gateway_block.get("outcome")}
            with lane_lock:
                hub = stream_hub
            payload["subscriptions"] = (
                hub.stats() if hub is not None else {"subscribers": 0}
            )
            return payload

        def _handle_socket_line(line: str, connection: Any) -> None:
            """Every authenticated socket line enters the SHARED dispatcher."""

            _handle_line(line, _sink_for(connection), connection=connection)

        def _finish_drain(code: int, frame: dict[str, Any]) -> None:
            """Emit the drain's terminal frame, then get the process out.

            Order is the contract: the frame is written and flushed BEFORE any
            exit path, because a drain that took the process down without
            accounting for what it refused and what it completed is
            indistinguishable from the crash the drain exists to replace.
            """

            nonlocal drain_exit_code, pool_shutdown_wait

            # A drain has ONE terminal frame. The latch is taken before
            # anything is published, so a mid-drain EOF racing a completing
            # drain cannot follow ``drain_complete`` with ``drain_abandoned``.
            with drain_terminal_lock:
                if drain_terminal_published.is_set():
                    return
                drain_terminal_published.set()
            drain_exit_code = code
            if code != 0:
                # Stuck workers: do NOT let the pool's context manager join
                # them (it would hang exactly as long as "forever"), and do not
                # trust a plain return either — concurrent.futures' atexit hook
                # joins worker threads on the way out of the interpreter.
                pool_shutdown_wait = False
            # THE WATCHDOG IS THE FIRST ACT, before the frame, the broadcast,
            # and the teardown — because every one of those can block. It used
            # to be armed after them, so the very steps most likely to hang ran
            # unwatched: broadcasting to a wedged reader parks a ``sendall``
            # for IO_TIMEOUT, and the hub's joins were a per-subscriber budget
            # that SUMMED. And the wakeup itself can block: observed live on
            # Windows (2026-08-13), closing the protocol descriptor a reader is
            # parked on does not return until that read does, and the child
            # outlived its own completed drain. From here to process exit
            # everything is inside one deadline.
            if hard_exit is not None:
                threading.Thread(
                    target=_force_exit_after_drain,
                    args=(code,),
                    name="harness-serve-drain-exit",
                    daemon=True,
                ).start()
            frames.emit(frame)
            # Socket clients are owed the SAME terminal frame: a client that
            # asked for the drain over the socket, and every client that was
            # merely attached, learns how it ended on the transport it is on.
            # Broadcast before teardown — after ``_close_socket_lane`` there is
            # nobody left to tell.
            _broadcast_lanes(frame)
            _close_socket_lane(reason="drain")
            _unregister_instance()
            drain_finished.set()
            if drain_wakeup is not None:
                try:
                    drain_wakeup()
                except Exception:
                    pass
            if hard_exit is None:
                # Unit-test path: the loop returns ``drain_exit_code`` and the
                # caller observes the frames. No process-level lever is pulled.
                return
            if code != 0:
                hard_exit(code)
                return
            # Clean drain: the reader gets its chance to unwind normally
            # (closed sockets, flushed writer, restored stdio) and the watchdog
            # above forces the exit if it does not. Nothing is waited on here.

        def _force_exit_after_drain(code: int) -> None:
            """Force the process down if the drain does not finish getting out.

            Armed at the START of ``_finish_drain``, so its deadline covers the
            WHOLE tail: publishing the terminal frame, broadcasting it, closing
            the socket lane (hub joins, connection closes, lock release),
            unregistering, waking the reader, and the reader unwinding. The
            normal case returns in milliseconds; anything else is a drained
            runtime that is still running, which is the state this exists to
            make impossible.

            Read from the module at call time on purpose — a test lowers it.
            """

            deadline = time.monotonic() + _DRAIN_EXIT_DEADLINE_SECONDS
            while time.monotonic() < deadline:
                if drain_finished.is_set() and reader_unwound.is_set():
                    return
                time.sleep(0.02)
            if hard_exit is not None:
                hard_exit(code)

        def _drain_monitor(state: _DrainState) -> None:
            deadline = state.started_monotonic + state.deadline_seconds
            last_progress = state.started_monotonic
            while True:
                with inflight_lock:
                    remaining = sorted(inflight)
                    # Read in the SAME critical section as the pending set: a
                    # timeout that decided "no chat turns" from a second,
                    # later read could kill the turn that started in between.
                    chat_turn_ids = sorted(
                        key for key, item in inflight.items() if item.is_chat_turn
                    )
                    # Read in the SAME critical section for the same reason,
                    # one line later: a generation that started between two
                    # reads would be killed by a timeout that had already
                    # decided nothing was holding.
                    long_run_ids = sorted(
                        key for key, item in inflight.items() if item.is_long_run
                    )
                if not remaining:
                    _finish_drain(
                        0,
                        {
                            "event": "drain_complete",
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            **state.counters(),
                            "drain_ms": state.elapsed_ms(),
                        },
                    )
                    return
                now = time.monotonic()
                if now >= deadline:
                    expiry = {
                        "event": "drain_timeout",
                        "pid": os.getpid(),
                        "boot_id": boot_id,
                        **state.counters(),
                        "drain_ms": state.elapsed_ms(),
                        "deadline_seconds": state.deadline_seconds,
                        # WHICH requests are stuck, by id — a timeout that
                        # only reported a count would leave the operator
                        # with nothing to correlate against the stack dump.
                        "stuck_request_ids": remaining,
                        # And WHY it is allowed to be stuck. A chat turn in
                        # flight is recording-safety work: this file's own
                        # contract says a supervisor must never recycle serve
                        # while ``chat_turns`` > 0, and a drain deadline firing
                        # `hard_exit` (which is `os._exit`) over one is that
                        # recycle by another name.
                        "held_by_chat_turns": len(chat_turn_ids),
                        "chat_turn_request_ids": chat_turn_ids,
                        # ADDITIVE, beside the two above rather than folded into
                        # them. `held_by_chat_turns` keeps its name and its
                        # meaning — a reader that only knows that key still
                        # reads a true number about chat turns, it just is not
                        # the whole reason the drain is being held any more.
                        # And the split is what the frame is FOR: "held by 1
                        # chat turn" and "held by 1 `characters rows`" are the
                        # same terminal:false with very different waits behind
                        # them, and an operator watching a restart deserves to
                        # know which.
                        "held_by_long_runs": len(long_run_ids),
                        "long_run_request_ids": long_run_ids,
                        "terminal": not (chat_turn_ids or long_run_ids),
                    }
                    if chat_turn_ids or long_run_ids:
                        # NOT terminal: say so, keep serving, re-arm. The frame
                        # is emitted every time the deadline lapses, so a
                        # supervisor watching a drain that is being held open
                        # sees each hold rather than silence.
                        state.note_deadline_held()
                        expiry.update(state.counters())
                        frames.emit(expiry)
                        _broadcast_lanes(expiry)
                        deadline = now + state.deadline_seconds
                        last_progress = now
                        time.sleep(max(0.0, drain_poll_interval_seconds))
                        continue
                    _finish_drain(DRAIN_TIMEOUT_EXIT_CODE, expiry)
                    return
                if now - last_progress >= _DRAIN_PROGRESS_INTERVAL_SECONDS:
                    progress = {
                        "event": "drain_progress",
                        "pending": len(remaining),
                        "request_ids": remaining,
                        "drain_ms": state.elapsed_ms(),
                    }
                    frames.emit(progress)
                    # The ONE drain frame that reached stdio and nothing else.
                    # Its entire purpose is that "a draining service never looks
                    # dead to a watchdog" — and the socket client IS such a
                    # watchdog: it reads with a finite timeout and reports
                    # `transport_failed` on silence. With the socket lane's
                    # minimum deadline, a drain holding a chat turn open puts
                    # the first socket-visible frame 30s out, so a healthy,
                    # completing drain reported a transport failure and exit 6.
                    _broadcast_lanes(progress)
                    last_progress = now
                time.sleep(max(0.0, min(drain_poll_interval_seconds, deadline - now)))

        # ── the shared dispatcher ───────────────────────────────────────────
        #
        # ONE op table, N transports. ``sink`` is where this message's answers
        # go (stdout for stdio, the originating connection for a socket client)
        # and ``connection`` is None on stdio. Every branch below was previously
        # inline in the stdio reader loop and is unchanged in behaviour: on
        # stdio, ``sink is frames`` and ``connection is None``, so the frames,
        # their order, and the exit codes are byte-identical.

        def _handle_line(line: str, sink: Any, *, connection: Any = None) -> str | None:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                sink.emit(
                    {
                        "id": None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": "request line is not valid JSON",
                    }
                )
                return None
            if not isinstance(message, dict):
                sink.emit(
                    {
                        "id": None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": "request must be a JSON object",
                    }
                )
                return None
            return _handle_message(message, sink, connection=connection)

        def _handle_message(
            message: dict[str, Any], sink: Any, *, connection: Any = None
        ) -> str | None:
            """Answer one op. Returns ``"shutdown"`` to stop the stdio reader."""

            nonlocal drain_state

            op = message.get("op")
            if op == "ping":
                sink.emit(_busy_frame())
                return None
            if op == "hello":
                # The socket lane authenticates BEFORE this dispatcher ever
                # sees a line, so a hello arriving here is a second one (or a
                # stdio client speaking the socket handshake at a pipe that
                # needs no handshake). Typed, and never a second auth path.
                sink.emit(
                    {
                        "event": "error",
                        "error": "unexpected_hello",
                        "detail": (
                            "this connection is already established; hello is the "
                            "first line of a SOCKET connection only"
                        ),
                    }
                )
                return None
            if op == "version":
                # Re-askable at any time, and deliberately NOT re-measured:
                # the answer is what code THIS interpreter loaded, which
                # cannot change while it lives. A client comparing against
                # its own install is how "the service is stale" becomes a
                # measurement instead of a theory.
                try:
                    from agent_runtime.build_stamp import build_stamp

                    version_build = build_stamp().payload()
                except Exception as exc:
                    version_build = {
                        "commit": None,
                        "dirty": None,
                        "source": "unknown",
                        "reason": f"stamp_failed:{type(exc).__name__}",
                    }
                sink.emit(
                    {
                        "event": "version",
                        "schema_version": SERVE_SCHEMA_VERSION,
                        "pid": os.getpid(),
                        "boot_id": boot_id,
                        # The transport THIS reply came over — honest per
                        # connection, and unchanged for every stdio consumer.
                        "transport": (
                            "stdio" if connection is None else connection.transport
                        ),
                        "runtime_root": runtime_root,
                        "build": version_build,
                        "auth": auth_block,
                        # Re-askable like the two blocks above it. Resolved ONCE
                        # at boot and echoed, not re-read: an operator rename
                        # (``harness gateway id --set-name``) writes the file,
                        # but the identity this SESSION greeted with is the one
                        # its clients correlate against, and re-reading here
                        # would let a frame disagree with the greeting that
                        # opened the connection.
                        "install": install_block,
                        "draining": drain_state is not None,
                        # Additive: what else is attached to this runtime, on
                        # the reply a client already asks for.
                        "socket": socket_block,
                        # Re-askable like the socket block above it: a client
                        # that reconnects after an operator turned the lane on
                        # (or after it failed to come up) must be able to learn
                        # that without a restart it cannot cause.
                        "gateway": gateway_block,
                        "connections": _connections_frame(),
                        # Re-askable, like the build stamp beside it and for the
                        # same reason: a durable service outlives the install it
                        # was started from, so "which methods does the thing I
                        # am attached to actually have" must be answerable at
                        # any time, not only at the greeting a client may have
                        # read hours ago.
                        "rpc": serve_rpc.manifest(),
                        # Re-askable for the same reason, and honest about the
                        # transport it just came over: ``shutdown`` is in the
                        # stdio answer and out of the socket one.
                        "ops": ops_manifest(
                            transport=(
                                "stdio" if connection is None else connection.transport
                            )
                        ),
                    }
                )
                return None
            if op == "connections":
                sink.emit(_connections_frame())
                return None
            if op == "subscribe":
                lane = message.get("lane", "stream")
                if lane != "stream":
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": lane,
                            "reason": "unsupported_lane",
                        }
                    )
                    return None
                if drain_state is not None:
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "draining",
                        }
                    )
                    return None
                # Optional patch-fold capability declaration. ABSENT means the
                # client said nothing — the historical {persona_instance,
                # incident} — which is what every client in the field sends and
                # is exactly today's wire. Present-but-malformed is REFUSED
                # rather than quietly read as absent: a client that meant to
                # narrow the set and was silently widened back to the historical
                # one would get patches it cannot fold, which is the precise
                # failure this negotiation exists to prevent.
                declared_raw = message.get("fold_entities")
                if declared_raw is None:
                    declared_fold_entities: Any = None
                elif isinstance(declared_raw, list) and all(
                    isinstance(name, str) and name.strip() for name in declared_raw
                ):
                    declared_fold_entities = frozenset(
                        name.strip() for name in declared_raw
                    )
                else:
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "invalid_fold_entities",
                        }
                    )
                    return None
                # Optional watermark RESUME. Same discipline as the declaration
                # above: absent means the client asked for nothing (and gets the
                # hydrate it always got, with no `resume` key anywhere on the
                # ack, so a subscribe that predates this parameter is answered
                # byte-identically); present-but-malformed is REFUSED rather
                # than read as absent, because a client that meant to resume and
                # was silently re-baselined would pay the megabyte it asked not
                # to and have nothing to grep for.
                resume_raw = message.get("resume")
                resume_requested = resume_raw is not None
                resume_offset: Any = None
                if resume_requested:
                    if isinstance(resume_raw, dict):
                        resume_offset = resume_raw.get("event_offset")
                    else:
                        sink.emit(
                            {
                                "event": "subscribe_denied",
                                "lane": "stream",
                                "reason": "invalid_resume",
                            }
                        )
                        return None
                key = _owner_of(connection)
                hub = _ensure_stream_hub()
                if hub.has(key):
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "already_subscribed",
                        }
                    )
                    return None
                # Recorded BEFORE ``hub.subscribe``: that call starts the new
                # producer generation, which reads this table to decide what it
                # may promote. Recorded after, this subscriber's declaration
                # would not reach the very producer its own subscribe created.
                with lane_lock:
                    stream_fold_entities[key] = declared_fold_entities
                # THIS subscriber's own answer, not the room's. Per-subscriber
                # promotion made the room's intersection the wrong thing to echo
                # on a PER-CONNECTION ack: what this client will actually be
                # handed a patch for is its own declaration, because the fan-out
                # resolves each envelope against it. The room's floor is still
                # echoed — on the hydrate, which is one frame fanned to everyone
                # and can only honestly carry a value true for all of them.
                #
                # For a single subscriber the two are the same set, which is why
                # the byte-pinned `subscribed.json` capture does not move.
                from agent_runtime.patch_coverage import normalize_fold_entities

                accepted_entities = sorted(
                    normalize_fold_entities(declared_fold_entities)
                )
                raw_sink = connection.emit if connection is not None else frames.emit

                # The resume decision, taken AFTER this connection's declaration
                # is recorded and BEFORE the hub join, which is the only window
                # where both facts are true: the span is judged against what this
                # client says it folds, and the producer that will feed it can
                # already see the narrowed room (`stream_frames` re-reads the
                # room per drain pass — see `_room` there, which is what makes a
                # restart-free join safe at all).
                resume = None
                if resume_requested:
                    from agent_runtime.stream_resume import resolve_stream_resume

                    try:
                        resume = resolve_stream_resume(
                            resume_offset, fold_entities=declared_fold_entities
                        )
                    except Exception as exc:  # noqa: BLE001 - never fatal
                        # A resume that cannot be COMPUTED must still leave the
                        # client subscribed. The hydrate is the answer to every
                        # question this path could have answered more cheaply, so
                        # a failure here costs bytes and never a lane.
                        from agent_runtime.stream_resume import StreamResume

                        resume = StreamResume(
                            honored=False,
                            reason=f"resume_failed:{type(exc).__name__}",
                        )

                def _on_drop(reason: str, stats: dict[str, Any]) -> None:
                    # Typed, never silent: an unsubscribed client that was told
                    # nothing would keep folding a stream that stopped arriving
                    # and believe itself current.
                    #
                    # The buffer is bounded TWICE — by frame count and by bytes
                    # — so the drop has to say WHICH bound tripped and carry
                    # both sets of numbers. The hub measures all of this and
                    # this frame used to throw it away, reporting a count
                    # against a `buffer_limit` read from the CONFIG rather than
                    # from the hub (None whenever it was left at the default).
                    # A client told only `backpressure` cannot tell one that
                    # fell 256 heartbeats behind from one that pinned 32 MiB,
                    # which is the difference between resubscribing and fixing
                    # its reader.
                    _emit_safely(
                        sink,
                        {
                            "event": "subscription_dropped",
                            "lane": "stream",
                            "reason": reason,
                            "bound": stats.get("drop_bound"),
                            "frames_delivered": stats.get("frames_delivered"),
                            "frames_discarded": stats.get("frames_discarded"),
                            "bytes_discarded": stats.get("bytes_discarded"),
                            "buffer_limit": stats.get("frame_limit"),
                            "byte_limit": stats.get("byte_limit"),
                        },
                    )
                    _release_subscription(connection)
                    _service_log(
                        {
                            "event": "serve_stream_subscription_dropped",
                            "boot_id": boot_id,
                            "connection": key,
                            "client": getattr(connection, "client", None),
                            "reason": reason,
                            "bound": stats.get("drop_bound"),
                            "frames_discarded": stats.get("frames_discarded"),
                            "bytes_discarded": stats.get("bytes_discarded"),
                        }
                    )

                # ONE line per attachment, in the serve child's OWN log. The
                # subscriber census of the 2026-08-17 boot had to be
                # reconstructed from timestamps and still left one rider
                # unidentified, because nothing on any attach path said so —
                # every line in the window described a BUILD, and the builds
                # were what the census was trying to explain (plan EG-2.1).
                from agent_runtime.stream import log_stream_attach

                log_stream_attach(
                    op="subscribe",
                    purpose="stream_lane",
                    connection=key,
                    client=getattr(connection, "client", None),
                    fold_entities=",".join(accepted_entities) or "-",
                )
                # The ACK precedes the subscription, deliberately. The producer
                # starts pushing the moment ``subscribe`` returns, so acking
                # afterwards would let the hydrate overtake the ack — and a
                # client reading "everything up to my ack is a reply to
                # something else" would discard its own baseline.
                if connection is not None:
                    connection.subscribed = True
                ack: dict[str, Any] = {
                    "event": "subscribed",
                    "lane": "stream",
                    "connection": key,
                    "buffer_limit": hub.stats().get("buffer_limit"),
                        # What the producer will actually promote FOR THIS
                        # CLIENT. It used to be able to come back narrower than
                        # asked because another subscriber folded less; per
                        # subscriber promotion retired that — a room that
                        # disagrees now ships both halves and each pump takes
                        # its own. So this is the client's own declaration,
                        # normalized (an absent one resolving to the historical
                    # set, which is the answer it always got).
                    "fold_entities": accepted_entities,
                }
                # Present ONLY when a resume was asked for, so the ack a client
                # that asked for nothing receives is byte-for-byte the one it
                # received before this lane existed — which is what keeps the
                # launcher's `subscribed.json` capture from moving.
                if resume is not None:
                    ack["resume"] = resume.payload()
                sink.emit(ack)

                # The catch-up span, on THIS connection's own sink, between the
                # ack and the join. Both edges matter: after the ack, because a
                # client reads everything before its ack as a reply to something
                # else; before the join, because the hub's first frame has to be
                # able to CHAIN onto the last of these.
                #
                # A honoured resume with zero frames is the whole feature working
                # — the client was already current and is sent nothing at all,
                # where it used to be sent the core.
                if resume is not None and resume.honored:
                    for frame in resume.frames:
                        _emit_safely(sink, frame)

                if not hub.subscribe(
                    key,
                    sink=raw_sink,
                    on_drop=_on_drop,
                    # A honoured resume attaches to the RUNNING producer instead
                    # of restarting it, and that is the half that actually saves
                    # the megabyte: a restart re-baselines the room, so a resume
                    # that restarted would hand this client the very hydrate it
                    # just proved it did not need — and charge every other
                    # subscriber a fresh full core for the privilege.
                    #
                    # Safe because `stream_frames` re-reads the room per drain
                    # pass: a producer that has not been restarted still NOTICES
                    # this subscriber's declaration and splits for it. The hub's
                    # own floor still starts a producer when none is running, so
                    # a resume into an empty room is not a subscription attached
                    # to nothing.
                    restart_producer=not (resume is not None and resume.honored),
                    # What this pump resolves a split frame against. Passing the
                    # RAW declaration rather than the normalized one keeps
                    # "said nothing" distinguishable all the way down, exactly
                    # as `parse_fold_entities_option` argues at the other end.
                    declared=declared_fold_entities,
                ):
                    # Lost a race with another subscribe for the same key. Say
                    # so rather than leave a client believing it is attached.
                    if connection is not None:
                        connection.subscribed = False
                    # The declaration is deliberately LEFT in place. This branch
                    # means another subscribe for the same key won the race, so
                    # that key IS attached — dropping its declaration here could
                    # only WIDEN the lane under a subscriber that never asked
                    # for the wider set, which is the failure direction. A stale
                    # entry can only ever narrow, and ``_release_subscription``
                    # (or the lane close) clears it.
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "already_subscribed",
                        }
                    )
                return None
            if op == "unsubscribe":
                key = _owner_of(connection)
                with lane_lock:
                    hub = stream_hub
                was_subscribed = hub is not None and hub.has(key)
                _release_subscription(connection)
                sink.emit(
                    {
                        "event": "unsubscribed",
                        "lane": "stream",
                        "connection": key,
                        "was_subscribed": was_subscribed,
                    }
                )
                return None
            if op == "drain":
                if _is_gateway(connection):
                    # A paired device does not get to end a runtime other
                    # clients are using — not even at `console` tier, because
                    # `drain` is not a level mutation the tier speaks about: it
                    # is the lifecycle verb, and its effect is that this process
                    # stops and every other attached client is disconnected. A
                    # phone deciding that for the desktop it is a guest on is
                    # the wrong default, and "restart the runtime from my phone"
                    # is a verb somebody can add on purpose later. The refusal
                    # mirrors `shutdown`'s rather than inventing a shape, and
                    # `ops_manifest(transport="gateway")` already said so, so a
                    # well-behaved client never reaches this line.
                    sink.emit(
                        {
                            "event": "error",
                            "error": "op_not_available_on_gateway",
                            "detail": (
                                "drain ends this runtime for every attached "
                                "client; it is the local console's verb"
                            ),
                        }
                    )
                    return None
                if connection is not None and message.get("force") is not True:
                    # The socket lane's second key. `shutdown` is refused there
                    # outright because a client does not get to kill a service
                    # other clients are using; `drain` is the safe replacement
                    # verb, but it still ENDS this process, and any local
                    # process holding the root's secret can ask. One explicit
                    # field is a trivial cost for an operator and a real
                    # barrier against an automated or accidental restart.
                    sink.emit(
                        {
                            "event": "error",
                            "error": "drain_requires_force",
                            "transport": "socket",
                            "detail": (
                                "drain over the socket ends the service for every "
                                'attached client; resend as {"op":"drain","force":true}'
                            ),
                        }
                    )
                    return None
                # The EFFECTIVE deadline is decided here, server-side, from the
                # client's ask floored by the minimum for the TRANSPORT it came
                # in on. Over stdio the asker owns this process outright and the
                # ask stands as given (the pre-socket contract, untouched); over
                # the socket it is floored, because that asker is any local
                # process holding the root's secret and it is shortening a
                # promise made to work it cannot see.
                effective_minimum = (
                    drain_socket_minimum_deadline_seconds
                    if connection is not None
                    else _DRAIN_DEADLINE_FLOOR_SECONDS
                )
                effective_deadline = _drain_deadline_seconds(
                    message.get("deadline_seconds"),
                    drain_deadline_seconds,
                    minimum=effective_minimum,
                )
                # ONE critical section for the whole transition. The guard and
                # the install used to be a bare read-modify-write on a closure
                # variable, which was harmless while the only caller was the
                # single stdio reader and became a genuine race the moment N
                # connection threads could ask: two of them could both observe
                # ``None``, both install a ``_DrainState``, and the process
                # would then run two monitors, publish two terminal frames, and
                # split its counters across two objects. The "already draining"
                # answer is decided INSIDE the section that would have
                # installed it, so it cannot be decided against a state a
                # sibling thread is mid-way through replacing.
                with inflight_lock:
                    existing = drain_state
                    if existing is None:
                        drain_state = _DrainState(effective_deadline)
                        started = drain_state
                        pending_at_start = sorted(inflight)
                if existing is not None:
                    sink.emit(
                        {
                            "event": "drain_in_progress",
                            "drain_ms": existing.elapsed_ms(),
                            **existing.counters(),
                        }
                    )
                    return None
                # Stop the delivery drain (and with it the busy pump) the
                # moment we stop accepting work: it forges completed
                # dispatches back into a sender's thread, and doing that to
                # a process on its way down is exactly what the shutdown
                # path already refuses to allow. `drain_progress` frames
                # take over the liveness duty for the rest of the wait.
                liveness_stop.set()
                draining_frame = {
                    "event": "draining",
                    "id": None,
                    "pid": os.getpid(),
                    "boot_id": boot_id,
                    "pending": len(pending_at_start),
                    "request_ids": pending_at_start,
                    "deadline_seconds": started.deadline_seconds,
                    # What was ASKED for, beside what was granted: a client
                    # that requested 0.05s and got 30 must be able to see that
                    # its ask was floored rather than honoured.
                    "requested_deadline_seconds": message.get("deadline_seconds"),
                    "minimum_deadline_seconds": effective_minimum,
                }
                frames.emit(draining_frame)
                # New connections are refused from here on BOTH doors (existing
                # ones stay up to be told how it ends), and every attached
                # client hears it at the same moment the stdio supervisor does.
                for _lane in (socket_server, gateway_server):
                    if _lane is None:
                        continue
                    try:
                        _lane.begin_drain()
                    except Exception:
                        pass
                _broadcast_lanes(draining_frame)
                threading.Thread(
                    target=_drain_monitor,
                    args=(started,),
                    name="harness-serve-drain",
                    daemon=True,
                ).start()
                return None
            if op == "stacks":
                # Operator diagnostic: dump every thread's stack as
                # stderr frames (hung-request forensics without py-spy).
                import traceback

                for thread_id, frame in sys._current_frames().items():
                    sink.emit(
                        {
                            "id": None,
                            "event": "stderr",
                            "line": f"--- thread {thread_id} ---",
                        }
                    )
                    for entry in traceback.format_stack(frame):
                        for line in entry.rstrip().splitlines():
                            sink.emit(
                                {"id": None, "event": "stderr", "line": line}
                            )
                sink.emit({"event": "stacks_dumped"})
                return None
            if op == "shutdown":
                if connection is not None:
                    # A socket client does NOT get to kill a service other
                    # clients are using. `drain` is the multi-client lifecycle
                    # verb — it refuses new work, lets in-flight work land, and
                    # accounts for both — and `shutdown` stays what it has
                    # always been: the verb of the process that owns the pipe.
                    sink.emit(
                        {
                            "event": "error",
                            "error": "op_not_available_on_socket",
                            "detail": (
                                "shutdown is the stdio owner's verb; use "
                                '{"op":"drain"} to replace the service safely'
                            ),
                        }
                    )
                    return None
                return "shutdown"
            if op == "cancel":
                cancel_id = message.get("id")
                cancel_id = cancel_id.strip() if isinstance(cancel_id, str) else ""
                if not cancel_id:
                    sink.emit(
                        {
                            "id": None,
                            "event": "error",
                            "error": "invalid_request",
                            "detail": 'cancel needs {"op": "cancel", "id": "<request id>"}',
                        }
                    )
                    return None
                # Scoped to the asker's OWN work: the inflight table is keyed
                # per owner, so one client can neither cancel nor even observe
                # another's request id.
                owner = _owner_of(connection)
                cancel_key = (
                    cancel_id if owner == "stdio" else f"{owner}:{cancel_id}"
                )
                with inflight_lock:
                    future = inflight_futures.get(cancel_key)
                    running_request = inflight.get(cancel_key)
                    known = running_request is not None
                if future is not None and future.cancel():
                    with inflight_lock:
                        inflight.pop(cancel_key, None)
                        inflight_futures.pop(cancel_key, None)
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "exit",
                            "code": 130,
                            "cancelled": True,
                        }
                    )
                elif running_request is not None and running_request.is_runtime_stream:
                    # The state stream is read-only and infinite. Unlike a
                    # mutation, it has a cooperative cancellation seam and
                    # MUST release its worker when the Launcher reconnects;
                    # otherwise four watchdog cycles exhaust the entire
                    # serve pool with abandoned streams.
                    running_request.cancel_event.set()
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "cancel_accepted",
                            "state": "running",
                        }
                    )
                else:
                    # Already running (uninterruptible) or unknown — the
                    # side effect may still land; mutation verbs' own
                    # --issued-at replay guard is what makes that safe.
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "cancel_denied",
                            "state": "running" if known else "unknown",
                        }
                    )
                return None
            # ── the METHOD lane ─────────────────────────────────────────────
            #
            # Named JSON-RPC 2.0 methods, BESIDE the argv lane rather than
            # instead of it (decision doc §3 / launcher `fa2226750`). The argv
            # lane below is unchanged and stays the fallback: it has never sent
            # `jsonrpc` or `method`, so nothing that used to reach it can be
            # captured here, and nothing about its frames or exit codes moves.
            #
            # Answered INLINE, like `ping` / `version` / `connections` and
            # unlike an argv request. The pool exists for handlers that block —
            # chat turns, streams — and these methods touch a handful of small
            # JSON files under the office lock and are done in microseconds.
            #
            # It is also why the lane is not refused while draining, and the
            # test that matters here is NOT "is it a read": `runtime.office.
            # upsert` mutates and is still answered. A drain refuses new WORK so
            # in-flight work can land, and the work it is protecting is the kind
            # that can be CUT OFF HALF-DONE — a chat turn whose frames stop
            # mid-stream when the process exits. An inline handler cannot be:
            # `OfficeStore` has written the actor file atomically and released
            # the lock before the ack is emitted, and the replacement runtime
            # reads that same file. Refusing it would fail an operator's drag
            # during a restart to protect against a loss that cannot occur.
            # `version` and `ping` are answered throughout for the same reason.
            # (Pinned by `test_a_write_during_a_drain_lands_because_it_cannot_be
            # _cut_off_half_done` in tests/agent_runtime/test_serve_rpc_office_
            # upsert.py — this is a decision, not an oversight.)
            #
            # The handler is told WHO asked, not just what. All of it comes
            # from this frame's own dispatch — ``sink`` is the stable
            # per-connection writer ``_sink_for`` hands out, and ``connection``
            # is None exactly on stdio. Nothing here is office-specific: it is
            # the argument a method needs before it can push to its caller
            # LATER, which request/response methods simply ignore.
            #
            # ``caller`` is the AUTHORIZATION half (chokepoint plan, Stage A2),
            # and this is the ONE place a live connection becomes one. It is
            # derived from the connection object the transport handed us — never
            # from ``message`` — so no field a client can type reaches the front
            # door's predicate. ``caller_for_connection`` reads the connection's
            # own ``authenticated`` flag, which is set only after
            # ``verify_hello_proof``, so the socket lane's identity is proven
            # here rather than assumed, and stdio's is the process owner's.
            #
            # ``spawn_chat_turn`` is the ONE exception to "answered inline", and
            # it proves the rule rather than breaking it: the chat methods do not
            # run their turn on this loop, they put it on the pool through this
            # seam and ack. Everything the argv lane does for a chat turn happens
            # here too — the same ``_ArgvRequest``, so ``is_chat_turn`` is
            # derived from the same ``_CHAT_TURN_COMMANDS`` shapes and the drain
            # ledger counts an RPC turn exactly as it counts a local one; the
            # same inflight table, so ``connections`` and cancel see it; the same
            # ``_run``, so the frames, the exit code and the completion
            # accounting are one implementation. A serve that recycled mid-turn
            # because the turn arrived on the other lane is the exact defect
            # ``held_by_chat_turns`` exists to prevent.
            def _spawn_chat_turn(
                request_id: str, argv: list[str], turn_request_id: str
            ) -> None:
                from agent_runtime.chat_turn import ChatTurnSpawnRefused

                if drain_state is not None:
                    # And ACCOUNTED, exactly as an argv refusal is: a drain that
                    # turned a remote turn away is a number on the terminal
                    # frame rather than an inference. The method lane keeps
                    # answering during a drain for handlers that cannot be cut
                    # off half-done; a chat turn is the work that CAN be, which
                    # is what the drain is for.
                    drain_state.note_refused()
                    raise ChatTurnSpawnRefused(
                        "draining",
                        "serve is draining and is not accepting new chat turns; "
                        "reconnect to the replacement runtime and retry with the "
                        "same turn_request_id",
                    )
                chat_request = _ArgvRequest(
                    request_id,
                    [str(item) for item in argv],
                    owner=_owner_of(connection),
                    sink=None if connection is None else sink,
                    turn_request_id=turn_request_id,
                )
                with inflight_lock:
                    # The id is server-minted and random, so a collision here is
                    # not a client behaviour — it is a bug, and it refuses
                    # rather than silently replacing a live request's entry.
                    if chat_request.key in inflight:
                        raise ChatTurnSpawnRefused(
                            "request_id_collision",
                            "a request with this server-minted id is already in flight",
                        )
                    inflight[chat_request.key] = chat_request
                chat_future = pool.submit(_run, chat_request)
                with inflight_lock:
                    if chat_request.key in inflight:
                        inflight_futures[chat_request.key] = chat_future

            # The SECOND seam onto the pool, and the general one. A chat turn is
            # a whole request handed over and acked; this is the TAIL of one
            # request handed over — a callable that returns the very frame the
            # handler would have returned — so the method lane keeps its
            # request/response shape and only the thread that finishes the work
            # moves. ``runtime.media.get``'s proxy arm is the first caller: it
            # dials another machine, and a machine that is switched off parked
            # this loop for the dial's whole timeout with every other request
            # from that client queued behind it.
            #
            # Refused while DRAINING, which puts the deferral on the same side
            # of the drain as the pool it uses: a drain is waiting for the pool
            # to empty, and handing it new work is the opposite of that. The
            # handler answers inline instead — the pre-existing behaviour, for
            # the seconds a drain lasts.
            def _spawn_reply(build: Any) -> bool:
                if drain_state is not None:
                    return False
                try:
                    pool.submit(_emit_deferred_reply, build, sink)
                except RuntimeError:
                    # The pool is already shutting down. False, so the handler
                    # answers on this thread rather than a client waiting for a
                    # frame no worker will ever write.
                    return False
                return True

            if serve_rpc.is_rpc_frame(message):
                rpc_frame = serve_rpc.handle_request(
                    message,
                    serve_rpc.RpcContext(
                        connection_key=getattr(connection, "key", None),
                        transport=getattr(connection, "transport", "stdio"),
                        emit=sink.emit,
                        caller=caller_for_connection(connection),
                        spawn_chat_turn=_spawn_chat_turn,
                        spawn_reply=_spawn_reply,
                    ),
                )
                # The ONE frame this lane does not write: the handler took the
                # deferral and the worker owns the reply now. Compared by
                # identity, so no result a handler builds can land here.
                if not serve_rpc.is_deferred(rpc_frame):
                    sink.emit(rpc_frame)
                return None
            if _is_gateway(connection):
                # THE ARGV LANE IS NOT REACHABLE FROM A DEVICE, and this is the
                # load-bearing refusal of the whole stage. The front-door tier
                # gate (`authorize_call`) sits on the METHOD lane; the argv lane
                # runs `harness <anything>` through the CLI dispatcher, where a
                # tier declaration does not exist and every verb is the local
                # operator's. Without this line a `read`-tier device refused
                # `runtime.agent.retire` on the method lane could simply send
                # `{"argv": ["harness", "agent", "retire", ...]}` and be obeyed —
                # the gate would be real and bypassable in one frame.
                #
                # This is a refusal rather than a second gate on purpose. Gating
                # argv would mean deciding a tier for every CLI verb this repo
                # has and keeping that map correct forever, which is the
                # duplicated-authority shape this stack keeps retiring. A device
                # has the method lane, whose tiers ride the manifest it already
                # reads.
                sink.emit(
                    {
                        "id": message.get("id") if isinstance(message.get("id"), str) else None,
                        "event": "error",
                        "error": "argv_lane_unavailable",
                        "detail": (
                            "the argv lane is the local console's; a paired "
                            "device calls JSON-RPC methods, whose tiers ride "
                            "the rpc manifest on hello_ok"
                        ),
                    }
                )
                return None
            rid = message.get("id")
            argv = message.get("argv")
            if (
                not isinstance(rid, str)
                or not rid.strip()
                or not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) for item in argv)
            ):
                sink.emit(
                    {
                        "id": rid if isinstance(rid, str) else None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": 'request needs {"id": "<non-empty>", "argv": ["harness", …]}',
                    }
                )
                return None
            if drain_state is not None:
                # Refused, and ACCOUNTED: the count lands on the terminal
                # drain frame, so "the restart dropped work" is a number an
                # operator can read rather than an inference.
                drain_state.note_refused()
                sink.emit(
                    {
                        "id": rid.strip(),
                        "event": "draining",
                        "detail": (
                            "serve is draining and is not accepting new requests; "
                            "reconnect to the replacement runtime"
                        ),
                        "drain_ms": drain_state.elapsed_ms(),
                    }
                )
                # Terminal frame too: a client that predates the `draining`
                # event is waiting for an `exit` and would otherwise hang
                # for the life of its request.
                sink.emit(
                    {
                        "id": rid.strip(),
                        "event": "exit",
                        "code": DRAINING_EXIT_CODE,
                        "draining": True,
                    }
                )
                return None
            request = _ArgvRequest(
                rid.strip(),
                [str(item) for item in argv],
                owner=_owner_of(connection),
                sink=None if connection is None else sink,
            )
            with inflight_lock:
                if request.key in inflight:
                    sink.emit(
                        {
                            "id": request.rid,
                            "event": "error",
                            "error": "duplicate_request_id",
                            "detail": "a request with this id is still in flight",
                        }
                    )
                    return None
                inflight[request.key] = request
            future = pool.submit(_run, request)
            with inflight_lock:
                # _run may already have finished and popped the request;
                # only track the future while the request is in flight so
                # the registry cannot leak completed entries.
                if request.key in inflight:
                    inflight_futures[request.key] = future
            return None

        pool = ThreadPoolExecutor(
            max_workers=max(1, pool_size), thread_name_prefix="harness-serve"
        )
        # The socket starts ACCEPTING only now: the listener has been bound
        # since before the ready frame (so the port could be published), but a
        # connection whose first request landed before this pool existed would
        # have nowhere to dispatch. The backlog holds them for the microseconds
        # in between.
        if socket_server is not None:
            try:
                socket_server.start_accepting()
            except Exception as exc:
                _service_log(
                    {
                        "event": "serve_socket_accept_start_failed",
                        "boot_id": boot_id,
                        "reason": type(exc).__name__,
                    }
                )
                _close_socket_lane(reason="accept_start_failed")
        if gateway_server is not None:
            # Same two-phase start, same reason: the port was bound before the
            # ready frame so it could be published, and accepting waits for the
            # pool. A gateway lane that cannot start accepting closes BOTH doors
            # through the shared closer, because a half-open runtime — loopback
            # serving, gateway bound but deaf — is a state nothing downstream
            # can describe.
            try:
                gateway_server.start_accepting()
            except Exception as exc:
                _service_log(
                    {
                        "event": "serve_gateway_accept_start_failed",
                        "boot_id": boot_id,
                        "reason": type(exc).__name__,
                    }
                )
                _close_socket_lane(reason="gateway_accept_start_failed")
        # Explicit construction + shutdown rather than ``with``: the drain's
        # timeout path must be able to stop waiting on work that has proven it
        # will not finish, and a context manager always joins.
        try:
            for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                if _handle_line(line, frames) == "shutdown":
                    break
        finally:
            # The reader is done; from here the process is unwinding normally,
            # which is what the drain monitor's grace window is waiting to see.
            reader_unwound.set()
            # ``wait`` is True everywhere except after a drain TIMEOUT, where
            # the whole point is that the remaining work has already outlived
            # its deadline and joining it would restore the hang.
            pool.shutdown(wait=pool_shutdown_wait)
        liveness_stop.set()
        if drain_state is not None:
            # The drain owns the terminal frame (``drain_complete`` /
            # ``drain_timeout``); emitting ``shutdown`` as well would tell a
            # consumer that a TIMED-OUT drain ended cleanly — the one thing it
            # must not conclude.
            #
            # But the reader can get here BEFORE the monitor has published
            # anything: a `shutdown` op, or the pipe reaching EOF, while a drain
            # is still in progress. That path used to fall straight to
            # ``return drain_exit_code`` — no terminal frame at all, the registry
            # entry left on disk, and code 0 even when the drain had TIMED OUT.
            # A drain that exits silently is exactly the crash it exists to
            # replace, so wait a bounded moment for the monitor (the pool is
            # already joined above, so it is normally one poll away) and, if it
            # never publishes, say so in a typed frame of its own.
            if not drain_finished.wait(_DRAIN_ABANDON_GRACE_SECONDS):
                if drain_terminal_published.is_set():
                    # A drain that already DECIDED how it ended owns the
                    # terminal frame; this path is only for a drain that never
                    # got one. Publishing ``drain_abandoned`` on top of a
                    # completed drain told a supervisor that a successful
                    # restart gave up, and exited 3 on it — the frame and the
                    # code both wrong, about work that had actually landed. The
                    # exit watchdog covers the case where the publisher is the
                    # thing that hung.
                    return drain_exit_code
                abandoned = {
                    "event": "drain_abandoned",
                    "pid": os.getpid(),
                    "boot_id": boot_id,
                    **drain_state.counters(),
                    "drain_ms": drain_state.elapsed_ms(),
                    "detail": (
                        "the transport closed while a drain was still in "
                        "progress; the drain published no terminal frame"
                    ),
                }
                frames.emit(abandoned)
                _broadcast_lanes(abandoned)
                _close_socket_lane(reason="drain_abandoned")
                _unregister_instance()
                # Nonzero on purpose, and the SAME code a timeout uses: a
                # supervisor must be able to tell "drained" from "gave up".
                return DRAIN_TIMEOUT_EXIT_CODE
            # ``_finish_drain`` published the frame, closed the socket lane, and
            # unregistered; ``drain_exit_code`` is its verdict, not this path's.
            return drain_exit_code
        shutdown_frame = {"event": "shutdown", "pid": os.getpid()}
        # Socket clients hear it BEFORE the transport closes under them: an
        # attached client whose socket simply died could not tell a clean
        # service shutdown from a crash, which is the distinction the durable
        # service exists to make legible.
        _broadcast_lanes(shutdown_frame)
        _close_socket_lane(reason="shutdown")
        _unregister_instance()
        frames.emit(shutdown_frame)
        return 0
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr


def _raw_fd_lines(fd: int):
    """Yield lines from [fd] as they arrive on an OPEN interactive pipe.

    Reads the descriptor directly (``os.read`` returns per pipe write, lines
    are assembled manually) instead of iterating a text wrapper, so no stdio
    layer can buffer a request until EOF."""
    buffer = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            if buffer:
                yield buffer.decode("utf-8", errors="replace")
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")


def _claim_protocol_pipes() -> tuple[int, int]:
    """Move the NDJSON protocol onto private descriptors and detach fd 0/1.

    Handlers spawn subprocesses (git for dirty state, proof runners, …) that
    inherit the standard descriptors. With serve's stdin pipe left on fd 0,
    any child that reads stdin blocks forever against the Launcher's open
    pipe — ``git status`` deadlocked the whole status handler (observed live
    2026-07-08; the piped smoke passed only because a closed pipe is
    instant EOF). A child writing raw output to an inherited fd 1 would
    likewise corrupt the frame stream. Serve therefore dups the protocol
    pipes to private fds and points fd 0 at the null device (children read
    EOF) and fd 1 at the null device (stray child writes vanish; every
    handler print already flows through the contextvar proxy)."""
    protocol_in = os.dup(0)
    protocol_out = os.dup(1)
    devnull_read = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_read, 0)
    os.close(devnull_read)
    devnull_write = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_write, 1)
    os.close(devnull_write)
    return protocol_in, protocol_out


def _cmd_serve(args) -> int:
    # Started before anything else this command does: everything from process
    # creation up to here is the interpreter + hermes import tax, and it is the
    # single largest term in a cold boot.
    from agent_runtime.boot_timeline import BootTimeline

    timeline = BootTimeline()
    if not getattr(args, "ndjson", False):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "unsupported_transport",
                    "detail": "hermes harness serve currently requires --ndjson (schema v1)",
                }
            )
        )
        return 2
    protocol_in, protocol_out = _claim_protocol_pipes()
    writer = os.fdopen(protocol_out, "w", encoding="utf-8", newline="\n")
    # Function-local on purpose: this file is exec'd into harness.py's globals.
    from agent_runtime.root_anchor import publish_store_root_anchor

    def _wake_reader() -> None:
        """Unblock a reader parked on an idle protocol pipe after a drain.

        Closing the descriptor makes the in-progress (or next) ``os.read``
        fail, which ``_raw_fd_lines`` already treats as end-of-stream. The
        second close in the ``finally`` below is a harmless no-op.
        """

        try:
            os.close(protocol_in)
        except OSError:
            pass

    try:
        return serve_loop(
            _raw_fd_lines(protocol_in),
            writer,
            pool_size=getattr(args, "pool_size", DEFAULT_POOL_SIZE)
            or DEFAULT_POOL_SIZE,
            boot_timeline=timeline,
            snapshot_prewarm=_prewarm_read_model_snapshot,
            # The production wiring for both warmups. The provider one is
            # injected here rather than hardcoded in the loop (EG-3.2): it is
            # policy, it now runs BEHIND the read-model build on one thread, and
            # a loop unit test must not import the OpenAI SDK to observe a ready
            # frame.
            provider_prewarm=_prewarm_provider_runtime,
            # Third and last on that one thread. Injected here for the same
            # reason the provider warmup is: it is policy, and a loop unit test
            # must not construct a persona agent to observe a ready frame.
            actor_prewarm=_prewarm_persona_chat_actors,
            root_anchor=publish_store_root_anchor,
            # The installed-skill join. ON here and nowhere else, the same
            # contract as ``root_anchor`` beside it and for a sharper version of
            # the same reason: this one WRITES into the machine's shared skills
            # root, so a ``serve_loop`` unit test must never be able to fire it.
            skill_install=install_harness_skills_at_boot,
            drain_wakeup=_wake_reader,
            # ``os._exit``, not ``sys.exit``: after a drain TIMEOUT the
            # interpreter cannot be trusted to come down at all — the
            # concurrent.futures atexit hook joins worker threads, and the
            # stuck ones are precisely why the deadline fired. Every frame is
            # flushed at emit, so nothing observable is lost.
            hard_exit=os._exit,
            # The durable service's transport. ON here and nowhere else: every
            # ``serve_loop`` unit test observes the byte-identical stdio loop
            # unless it asks for the socket by name.
            socket_lane=not getattr(args, "no_socket", False),
        )
    finally:
        try:
            writer.flush()
        except Exception:
            pass
        try:
            os.close(protocol_in)
        except OSError:
            pass


# ── the first real client ────────────────────────────────────────────────────
#
# ``harness serve connect`` is the operator/agent lane onto the socket: it
# resolves the root's live service from the registry, performs the mandatory
# hello, and prints what it got as JSON. It exists for three reasons, in order
# of importance:
#
# 1. A transport with no client is a transport nobody has proven. This performs
#    the REAL handshake against the REAL auth token over the REAL socket.
# 2. ``--drain`` gives the durable service its restart verb from the outside —
#    the thing slice 2 could describe but could not exercise, because a drain
#    over stdio ends the only connection that could observe it.
# 3. It is the shape the Launcher's client will mirror when it migrates.

#: Nothing LIVE to connect to for this root — either no registered socket
#: service at all, or one the registry classified as anything other than
#: ``live``. Both are "do not connect", and the second is the more important
#: one: a serve's port outlives the serve, so a dead entry names an address
#: some other local process may now be answering on.
SERVE_CONNECT_NO_SERVICE_EXIT_CODE = 4
#: The service is there; this client could not authenticate to it.
SERVE_CONNECT_REJECTED_EXIT_CODE = 5
#: The connection itself failed (refused, timed out, died mid-handshake).
SERVE_CONNECT_TRANSPORT_EXIT_CODE = 6


def _cmd_serve_connect(args) -> int:
    from agent_runtime import paths
    from agent_runtime.build_stamp import build_stamp
    from agent_runtime.serve_auth import read_token
    from agent_runtime.serve_socket import (
        HELLO_CONTRACT_VERSION,
        ServeHelloProtocolError,
        ServeSocketClient,
        resolve_socket_target,
    )

    def _emit(payload: dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))

    store_root = paths.store_root()
    # ``allow_stale`` is asked for so the REFUSAL can name what it refused —
    # not so a non-live target can be used. Discovery itself returns live rows
    # only; this call is the diagnostic form, and the check below is the gate.
    target = resolve_socket_target(store_root, allow_stale=True)
    if target is None:
        _emit(
            {
                "ok": False,
                "error": "no_socket_service",
                "detail": (
                    "no live serve with a socket transport is registered for this "
                    "runtime root"
                ),
                "runtime_root": str(store_root),
            }
        )
        return SERVE_CONNECT_NO_SERVICE_EXIT_CODE
    if not target.live:
        # The half of the credential-disclosure defect that lives on the client.
        # A registry row classified ``stale_dead_pid`` names a port whose owner
        # is gone, and a local port is reusable the moment its owner dies — so
        # connecting here means handshaking with whatever took it over. That is
        # not a theory: with the old raw-token hello, an impostor listening on a
        # dead serve's port harvested the real token. The token no longer
        # travels, and this connect still refuses, by name.
        _emit(
            {
                "ok": False,
                "error": "socket_service_not_live",
                "classification": target.classification,
                "detail": (
                    "the only socket service registered for this runtime root is "
                    f"classified {target.classification!r}, not 'live'; its port may "
                    "now belong to another process, so this client will not "
                    "handshake with it"
                ),
                "runtime_root": str(store_root),
                "target": target.payload(),
            }
        )
        return SERVE_CONNECT_NO_SERVICE_EXIT_CODE
    token = read_token(store_root)
    if not token:
        # Fails CLOSED, and says which side is missing: a client with no token
        # cannot authenticate, and pretending otherwise would send a hello that
        # can only ever be rejected.
        _emit(
            {
                "ok": False,
                "error": "no_auth_token",
                "detail": "this runtime root has no serve auth token to present",
                "runtime_root": str(store_root),
                "target": target.payload(),
            }
        )
        return SERVE_CONNECT_REJECTED_EXIT_CODE
    client_build = build_stamp().commit
    report: dict[str, Any] = {
        "ok": False,
        "runtime_root": str(store_root),
        "target": target.payload(),
        "client": getattr(args, "client", None) or "harness-serve-connect",
        "client_build": client_build,
        # Which handshake this client speaks. Stated in the report because the
        # next client of this lane is the Launcher, and "which contract did the
        # thing that worked use" must not be archaeology.
        "hello_contract": HELLO_CONTRACT_VERSION,
    }
    timeout = float(getattr(args, "timeout", 10.0) or 10.0)
    connection = ServeSocketClient(target.host, target.port, timeout_seconds=timeout)
    try:
        connection.connect()
    except OSError as exc:
        report["error"] = "connect_failed"
        report["detail"] = type(exc).__name__
        _emit(report)
        return SERVE_CONNECT_TRANSPORT_EXIT_CODE
    try:
        try:
            hello = connection.hello(
                token=token, client=report["client"], client_build=client_build
            )
        except ServeHelloProtocolError as exc:
            # Either the peer refused us before the challenge (its typed reason
            # is the answer) or what is on this port does not speak this
            # contract. Neither is a case for sending a credential anyway.
            report["error"] = (
                "hello_rejected" if exc.reason else "hello_contract_mismatch"
            )
            report["detail"] = exc.detail
            report["reason"] = exc.reason
            report["hello"] = exc.frame
            _emit(report)
            return SERVE_CONNECT_REJECTED_EXIT_CODE
        report["server_hello"] = connection.server_hello
        report["hello"] = hello
        if not isinstance(hello, dict) or hello.get("event") != "hello_ok":
            report["error"] = (
                "hello_rejected" if isinstance(hello, dict) else "no_hello_reply"
            )
            _emit(report)
            return SERVE_CONNECT_REJECTED_EXIT_CODE
        if getattr(args, "probe", False):
            connection.send({"op": "version"})
            report["version"] = connection.read_frame()
        if getattr(args, "drain", False):
            deadline = getattr(args, "deadline_seconds", None)
            # ``force`` is mandatory on the socket lane — this verb IS the
            # deliberate operator restart, so it says so rather than being
            # refused by the service it is trying to replace.
            request: dict[str, Any] = {"op": "drain", "force": True}
            if deadline is not None:
                request["deadline_seconds"] = float(deadline)
            connection.send(request)
            # Read to the TERMINAL frame, not to the first one: the drain's
            # evidence (what it refused, what it completed) is on the terminal
            # frame, and a client that stopped at ``draining`` would report a
            # restart it never watched finish.
            observed: list[dict[str, Any]] = []
            terminal = {
                "drain_complete",
                "drain_timeout",
                "drain_abandoned",
                "drain_in_progress",
            }
            while True:
                frame = connection.read_frame()
                if frame is None:
                    break
                observed.append(frame)
                if frame.get("event") not in terminal:
                    continue
                if (
                    frame.get("event") == "drain_timeout"
                    and frame.get("terminal") is False
                ):
                    # A deadline lapse HELD OPEN by a chat turn in flight: the
                    # service is still serving and will re-arm, so this is
                    # progress, not an ending. Reading it as terminal would
                    # report a restart that has not happened.
                    continue
                break
            report["drain"] = observed
            report["drain_outcome"] = (
                observed[-1].get("event") if observed else "no_frames"
            )
            report["drain_deadline_holds"] = len(
                [
                    frame
                    for frame in observed
                    if frame.get("event") == "drain_timeout"
                    and frame.get("terminal") is False
                ]
            )
        report["ok"] = True
        _emit(report)
        return 0
    except OSError as exc:
        report["error"] = "transport_failed"
        report["detail"] = type(exc).__name__
        _emit(report)
        return SERVE_CONNECT_TRANSPORT_EXIT_CODE
    except ServeHelloProtocolError as exc:  # pragma: no cover - defensive
        report["error"] = "hello_contract_mismatch"
        report["detail"] = exc.detail
        _emit(report)
        return SERVE_CONNECT_REJECTED_EXIT_CODE
    finally:
        connection.close()
