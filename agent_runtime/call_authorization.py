"""WHO may call WHAT — the front-door authorization vocabulary for the method lane.

Ruling A (2026-08-27,
``docs/agent-runtime-harness/planned/authorization-chokepoint.md``) put the check
at the RPC dispatch layer, evaluated against what the TRANSPORT proved, and
reframed the policy: **the tier is client security auth.** A credential that
traces to account-authed pairing may run console verbs; one that does not, may
not. hermes never sees an Eternia account directly, so the binding happens where
credentials are minted — the install's own serve token is the machine owner and
is ``local_console``; a device credential's tier is fixed at a pairing ceremony
only an account-authed operator surface can run (gateway Stage 1 / Stage A5).

Two rules follow, and they are the whole module:

* **The gate reads the tier off the CALLER, never off the request.** Everything
  a caller can type — ``--requested-by``, a ``params`` key, a hello field — is an
  assertion. The predicate takes a caller that only the transport builder mints,
  and there is no argument by which a request can name its own tier. This is the
  property the pre-existing coordinator machinery lacked (that one takes both the
  identity and the grant off argv, which is why Stage A4 renames it to what it
  is: an advisory self-declared budget).
* **A caller nobody recognised is REFUSED, not defaulted through.** Absence of a
  decision is never an allow.

Why a module and not four constants in ``serve_rpc``
---------------------------------------------------

``serve_rpc`` is the DISPATCHER. It should ask "may this caller run this tier?"
and render the refusal it gets back; it should not also be the place the policy
lives, because Stage A5 grows that policy a device-record lookup and the CLI
mirror (Stage A4) has to evaluate the same predicate with no dispatcher in the
picture. One import direction — ``serve_rpc`` → here, ``persona_commands`` →
here, and nothing back — keeps the two doors provably on one predicate.

What is deliberately NOT decided here
-------------------------------------

**The tier vocabulary itself is R11's, not this file's.** Two tiers exist because
two are what the shipped surface needs a word for: the docstrings on
``agent_retire.perform_agent_retire`` and ``serve_rpc._runtime_agent_retire``
already said ``console``, and everything that is not a level mutation is
``read``. Whether an ``admin`` tier carves out the skills INSTALL sub-phase is
the gateway plan's R11 question and is answered there, not by adding a constant
here on the way past.

Stage A1 landed the vocabulary and the declaration, A2 the caller model, A3 the
predicate with an EMPTY policy — every caller that existed was allowed, so
nothing observable moved, and the value of the stage was that a NEW caller would
arrive at a place where a decision is made instead of at three doors that never
asked.

**Stage A5 is that new caller, and the promise held: it is an edit to this file
and not an architecture change.** A paired device (gateway plan Stage 1,
``serve_gateway_auth.py``) arrives as :data:`CALLER_DEVICE` carrying the tier its
record holds, and :func:`authorize_call` compares that stored word against the
verb's declared tier. Everything A3 grandfathered is grandfathered still —
``_CONSOLE_KINDS`` was not touched — because a device's authority was added
BESIDE the machine owner's rather than folded into it. Two callers whose
authority comes from different kinds of fact should not share a membership test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "TIER_READ",
    "TIER_CONSOLE",
    "TIERS",
    "CALLER_STDIO_OWNER",
    "CALLER_LOCAL_CONSOLE",
    "CALLER_DEVICE",
    "CALLER_UNKNOWN",
    "TRANSPORT_GATEWAY",
    "RpcCaller",
    "LOCAL_CONSOLE",
    "STDIO_OWNER",
    "UNKNOWN_CALLER",
    "CLI_CONSOLE",
    "caller_for_connection",
    "REASON_SCOPE_DENIED",
    "REASON_UNKNOWN_TIER",
    "CallAuthorization",
    "authorize_call",
]

#: Anything that neither writes store state, emits an event, nor mints an id.
TIER_READ = "read"
#: A level MUTATION — the tier both agent methods' docstrings already named
#: (placement plan §A.11, owner decision D10-iv).
TIER_CONSOLE = "console"

#: Ordered least-privileged first. A tier outside this set is a programming
#: error, not a policy question: the registry refuses one at registration time
#: rather than letting a typo widen a door.
TIERS: tuple[str, ...] = (TIER_READ, TIER_CONSOLE)


# ── who is calling (Stage A2) ────────────────────────────────────────────────

#: The serve owner's own pipe. There is no connection, no key and no third
#: party: whoever holds this process's stdin already holds the process, so
#: refusing them would be refusing the operator their own machine.
CALLER_STDIO_OWNER = "stdio_owner"
#: A socket peer that presented THIS install's serve token.
#: ``verify_hello_proof`` fails CLOSED on a missing token (``serve_socket.py``),
#: so this is proven rather than asserted — but it proves exactly one thing, that
#: the peer holds the one install-wide credential. There is no device
#: granularity because there is one token; until Stage A5 mints per-device
#: credentials, holding the install token IS being the machine owner.
CALLER_LOCAL_CONSOLE = "local_console"
#: A peer on the GATEWAY listener that presented a per-device credential
#: (``gateway/devices.json``, minted by an operator-run pairing on this
#: install's own machine). Proven the same way ``local_console`` is — the
#: connection is only stamped after its HMAC proof verifies — but it proves
#: something narrower and something MORE: not "holds the one install-wide
#: secret" but "is this named device", which is why it is the first caller kind
#: that carries a tier of its own instead of inheriting the machine owner's.
CALLER_DEVICE = "device"
#: A caller the transport could not place. Minted by tests, by the defensive arm
#: of :func:`caller_for_connection`, and — the arm Stage A5 adds — by a gateway
#: connection that somehow reached the dispatcher without a device stamp.
CALLER_UNKNOWN = "unknown"

#: The transport name the gateway listener tags its connections with. Named here
#: rather than imported from ``serve_socket`` for the reason
#: :func:`caller_for_connection` is duck-typed: this module must not import the
#: transport to answer a question about an object it was handed.
TRANSPORT_GATEWAY = "gateway"


@dataclass(frozen=True, slots=True)
class RpcCaller:
    """What the TRANSPORT proved about who is asking. Never built from params.

    Deliberately separate from ``RpcContext.connection_key``, whose own
    docstring already commits that field to being the SUBSCRIPTION identity —
    the name a teardown sweep uses to find the registrations it must drop. An
    authorization fact riding it would silently redefine a key two other systems
    index on, so the key is REPEATED here (a refusal has to be able to name the
    connection it refused) rather than borrowed.

    The default is the stdio owner, and that is the honest value rather than a
    convenient one. An ``RpcCaller`` can only be constructed by code already
    inside this process; nothing on either wire reaches the constructor. So a
    context assembled with no arguments describes an in-process caller, which is
    what the sibling ``transport: str = "stdio"`` default has always said. The
    transport builder — :func:`caller_for_connection`, the ONE place a live
    connection becomes a caller — always passes the value explicitly, so the
    default is never what a remote peer gets.
    """

    kind: str = CALLER_STDIO_OWNER
    connection_key: str | None = None
    transport: str = "stdio"
    #: Set only for :data:`CALLER_DEVICE`. The device's own id, so a refusal and
    #: a log line can name WHICH paired device was turned away — the fact an
    #: operator auditing a revoked phone needs and the one a bare
    #: ``connection_key`` cannot supply, because that key is minted per
    #: connection and means nothing across two of them.
    device_id: str | None = None
    #: The tier this caller HOLDS, read off its device record by the transport.
    #: ``None`` for every non-device caller, whose authority is its kind. This
    #: is the field Ruling A's "the tier is client security auth" becomes:
    #: fixed at the pairing ceremony, read off the authenticated connection,
    #: never off anything the request carries.
    device_tier: str | None = None

    def describe(self) -> dict[str, Any]:
        """The caller as it appears on a log line or a refusal's ``data``.

        No secrets by construction: a kind, a transport, a connection key the
        server already echoes back to that same peer on ``hello_ok``, and — for
        a device — the id it named itself in the hello. The verifier that proved
        it has no field here and no field on ``DeviceRecord`` either.
        """

        payload = {
            "kind": self.kind,
            "transport": self.transport,
            "connection_key": self.connection_key,
        }
        if self.device_id is not None:
            payload["device_id"] = self.device_id
        if self.device_tier is not None:
            payload["device_tier"] = self.device_tier
        return payload


#: The machine owner at its own console, over the socket. Spelled as a value so
#: the grandfather clause is greppable rather than implicit in an absent check.
LOCAL_CONSOLE = RpcCaller(kind=CALLER_LOCAL_CONSOLE, transport="socket")
STDIO_OWNER = RpcCaller(kind=CALLER_STDIO_OWNER, transport="stdio")
UNKNOWN_CALLER = RpcCaller(kind=CALLER_UNKNOWN, transport="unknown")

#: The operator at the install's own shell (Stage A4's mirror). ``transport``
#: says ``cli`` because there is no wire: the argv reached this process from a
#: terminal the machine owner already controls, or from ``serve.py``'s argv lane
#: on a socket that already passed the HMAC. Either way the caller IS the
#: console, and this value is the greppable spelling of that grandfather clause.
#:
#: It is a CONSTANT and takes no arguments on purpose. Deriving the CLI's
#: identity from anything the invocation carries — ``--requested-by``, an env
#: var, a config key — would rebuild the self-declaration hole one door over.
CLI_CONSOLE = RpcCaller(kind=CALLER_LOCAL_CONSOLE, transport="cli")


def caller_for_connection(connection: Any) -> RpcCaller:
    """Turn a live serve connection into the one authorization fact it proves.

    ``None`` is stdio: ``serve.py``'s dispatcher passes the connection it is
    answering for, and there is no connection object on the owner's own pipe.

    ``authenticated`` is READ rather than assumed, even though
    ``ServeSocketServer`` only enters a connection into ``_connections`` after
    ``verify_hello_proof`` succeeds. The check costs one attribute read and makes
    the claim local: a future transport that hands the dispatcher a
    pre-handshake connection gets ``unknown`` instead of silently inheriting a
    guarantee it never made. Duck-typed on ``getattr`` for the same reason the
    two fields beside it are — this must not import the socket module to answer a
    question about an object it was handed.

    **Stage A5's arm, and the structural guard under it.** A gateway connection
    carries a ``device_id`` / ``device_tier`` pair the handshake stamped after
    the device's proof verified, and it becomes a :data:`CALLER_DEVICE` with the
    tier its record holds. The guard is the second half: a connection whose
    transport says ``gateway`` and which carries NO usable device stamp is
    ``unknown``, never ``local_console``. Without that line the default arm
    below would hand a remote peer the machine owner's authority the moment any
    future change let a gateway connection through with an empty stamp — and
    "the wrong default is one refactor away" is exactly the shape this lane
    keeps retiring. The grandfathered ``local_console`` therefore requires
    ``transport != "gateway"``, which is a property of the LISTENER the peer
    reached rather than of anything the peer said.
    """

    if connection is None:
        return STDIO_OWNER
    transport = str(getattr(connection, "transport", "stdio") or "stdio")
    key = getattr(connection, "key", None)
    key = key if isinstance(key, str) and key else None
    if not bool(getattr(connection, "authenticated", False)):
        return RpcCaller(kind=CALLER_UNKNOWN, connection_key=key, transport=transport)
    device_id = getattr(connection, "device_id", None)
    device_id = device_id if isinstance(device_id, str) and device_id else None
    device_tier = getattr(connection, "device_tier", None)
    device_tier = device_tier if device_tier in TIERS else None
    if device_id is not None and device_tier is not None:
        return RpcCaller(
            kind=CALLER_DEVICE,
            connection_key=key,
            transport=transport,
            device_id=device_id,
            device_tier=device_tier,
        )
    if transport == TRANSPORT_GATEWAY:
        return RpcCaller(kind=CALLER_UNKNOWN, connection_key=key, transport=transport)
    return RpcCaller(
        kind=CALLER_LOCAL_CONSOLE, connection_key=key, transport=transport
    )


# ── the decision (Stage A3) ──────────────────────────────────────────────────

#: The caller does not hold the tier this verb wants. CONTRACT, not prose: the
#: launcher's decoders branch on ``data.reason`` first and the numeric code
#: second, so this string is as much a shape as the frame around it.
REASON_SCOPE_DENIED = "scope_denied"
#: The verb declared a tier this build does not know. A programming error
#: surfaced as a refusal rather than as an allow — see :func:`authorize_call`.
REASON_UNKNOWN_TIER = "unknown_tier"

#: Kinds whose authority IS their kind: the machine owner, at its own pipe or
#: over its own loopback socket. A3 landed the enforcement point with this set
#: covering every caller that existed, so nothing observable moved. Stage A5
#: does not touch it — the local launcher, the CLI and every test are as
#: grandfathered as they were — it adds a kind BESIDE it whose authority is a
#: stored tier rather than a membership.
_CONSOLE_KINDS = frozenset({CALLER_STDIO_OWNER, CALLER_LOCAL_CONSOLE})


@dataclass(frozen=True, slots=True)
class CallAuthorization:
    """The answer, with the reason spelled for a machine."""

    ok: bool
    reason: str
    tier: str
    caller_kind: str

    def refusal_data(self) -> dict[str, Any]:
        """The ``data`` block of the typed refusal. ``reason`` leads because
        that is what a client branches on."""

        return {"reason": self.reason, "tier": self.tier, "caller": self.caller_kind}


def authorize_call(tier: str, caller: RpcCaller | None) -> CallAuthorization:
    """May *caller* run a verb declared at *tier*?

    ``caller is None`` is :data:`UNKNOWN_CALLER`, NOT the owner. Every production
    construction site fills the field; a ``None`` arriving here means somebody
    built a context by a path that never asked the transport, and the ruling's
    own words for "I do not know who this is" are refuse-console-verbs.

    An unrecognised TIER refuses too. The temptation is to wave it through as a
    typo — but a typo in a registration is exactly the case where a door is open
    and nobody meant it to be, and this arm is unreachable anyway because
    :func:`serve_rpc.method` rejects an unknown tier at import.

    Read verbs are open to everyone including ``unknown``, and Stage A5 KEPT
    that line rather than inheriting it by omission. The temptation once real
    devices exist is to gate reads on the device record too, and the reason not
    to is that a read tier is precisely what a caller who has proved nothing may
    still do: nothing on the read side mutates a level, and a caller that got as
    far as this function on the gateway lane has already passed an HMAC proof
    against a paired credential — the ``unknown`` arm there means "authenticated
    but unplaceable", which is a state the transport does not currently produce.
    An ``admin`` tier, if the skills install sub-phase ever needs one, is still
    R11's question and still not answered by adding a constant here on the way
    past.

    **The device arm is A5.** A device's authority is not its KIND but the
    ``device_tier`` the transport read off ``gateway/devices.json``, which was
    fixed at a pairing ceremony only an operator at this install's own machine
    can run. So the check is an equality against a stored word, and a device
    holding ``read`` is refused a console verb with the same typed
    ``scope_denied`` the launcher's decoders already branch on. A revoked or
    unpaired device never reaches this function as a device at all — the
    handshake refuses it — and if one ever did it would arrive as ``unknown``
    and be refused by the fall-through, which is the ruling's own words for "I
    do not know who this is".
    """

    resolved = caller if caller is not None else UNKNOWN_CALLER
    normalized = str(tier or "").strip() or TIER_CONSOLE
    if normalized not in TIERS:
        return CallAuthorization(
            ok=False,
            reason=REASON_UNKNOWN_TIER,
            tier=normalized,
            caller_kind=resolved.kind,
        )
    if normalized == TIER_READ:
        return CallAuthorization(
            ok=True, reason="read_tier", tier=normalized, caller_kind=resolved.kind
        )
    if resolved.kind == CALLER_DEVICE:
        # ``in TIERS`` as well as the equality, because a tier this build does
        # not know must refuse rather than compare-unequal-and-fall-through to
        # some later arm. There is no later arm today; there is no reason to
        # depend on that staying true.
        held = resolved.device_tier
        if held in TIERS and held == normalized:
            return CallAuthorization(
                ok=True,
                reason="device_tier",
                tier=normalized,
                caller_kind=resolved.kind,
            )
        return CallAuthorization(
            ok=False,
            reason=REASON_SCOPE_DENIED,
            tier=normalized,
            caller_kind=resolved.kind,
        )
    if resolved.kind in _CONSOLE_KINDS:
        return CallAuthorization(
            ok=True,
            reason="console_grandfathered",
            tier=normalized,
            caller_kind=resolved.kind,
        )
    return CallAuthorization(
        ok=False,
        reason=REASON_SCOPE_DENIED,
        tier=normalized,
        caller_kind=resolved.kind,
    )
