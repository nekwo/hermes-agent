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
predicate. The POLICY A3 ships is empty on purpose: every caller that exists
today is allowed, and the value of the stage is that a NEW caller — a paired
device — arrives at a place where a decision is made, instead of arriving at
three doors that never asked. Turning a device's scope into a refusal (Stage A5)
is then an edit to :func:`authorize_call`, not an architecture change.
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
    "CALLER_UNKNOWN",
    "RpcCaller",
    "LOCAL_CONSOLE",
    "STDIO_OWNER",
    "UNKNOWN_CALLER",
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
#: A caller the transport could not place. Minted today only by tests and by the
#: defensive arm of :func:`caller_for_connection`; from Stage A5 also by a device
#: credential that is revoked, expired, or absent from ``gateway/devices.json``.
CALLER_UNKNOWN = "unknown"


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

    def describe(self) -> dict[str, Any]:
        """The caller as it appears on a log line or a refusal's ``data``.

        No secrets by construction: a kind, a transport, and a connection key
        the server already echoes back to that same peer on ``hello_ok``.
        """

        return {
            "kind": self.kind,
            "transport": self.transport,
            "connection_key": self.connection_key,
        }


#: The machine owner at its own console, over the socket. Spelled as a value so
#: the grandfather clause is greppable rather than implicit in an absent check.
LOCAL_CONSOLE = RpcCaller(kind=CALLER_LOCAL_CONSOLE, transport="socket")
STDIO_OWNER = RpcCaller(kind=CALLER_STDIO_OWNER, transport="stdio")
UNKNOWN_CALLER = RpcCaller(kind=CALLER_UNKNOWN, transport="unknown")


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
    """

    if connection is None:
        return STDIO_OWNER
    transport = str(getattr(connection, "transport", "stdio") or "stdio")
    key = getattr(connection, "key", None)
    key = key if isinstance(key, str) and key else None
    if not bool(getattr(connection, "authenticated", False)):
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

#: Kinds that may run a ``console`` verb today. EVERY caller that exists is in
#: it, which is A3's whole point: the enforcement POINT lands with an empty
#: policy, so nothing observable moves for the local launcher, the CLI or the
#: tests, and Stage A5 turns a device's scope into a refusal by editing this
#: rather than by moving the check.
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

    Read verbs are open to everyone including ``unknown``, and that is a
    deliberate line rather than an oversight at this stage: a read tier is what a
    caller who has proved nothing may still do, and Stage A5 gates reads (if it
    gates them at all) on the device record, not on the absence of one. Nothing
    on the read side mutates a level.
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
