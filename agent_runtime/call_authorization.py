"""WHO may call WHAT — the front-door authorization vocabulary for the method lane.

Ruling A (2026-08-27,
``docs/agent-runtime-harness/archive/authorization-chokepoint.md``) put the check
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

**Gateway Stage 6 is the third caller, and it is not a third tier.** A paired
INSTALL (``gateway_peers.py``) arrives as :data:`CALLER_PEER`, and what it holds
is not a tier word at all but an explicit ALLOWLIST of method names
(:data:`PEER_METHOD_ALLOWLIST`). The device arm stayed an equality against a
stored tier because a device is a client of THIS install whose operator chose
how much of the surface to hand it; a peer is a different KIND of caller — an
autonomous runtime whose own agents drive it — and the question "how much of my
runtime may another runtime's agents reach" has a different answer shape. The
canon already committed to that answer: 06's remote-connector table says the
peer tier is "deliberately excluded: agents never mint or retire agents on
another install; a remote OPERATOR does". An allowlist is that sentence as code
— the exclusion holds BY CONSTRUCTION rather than by a tier comparison that
would silently include every future ``read`` verb the moment one was added.
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
    "CALLER_PEER",
    "CALLER_UNKNOWN",
    "PEER_METHOD_ALLOWLIST",
    "LOCAL_CONSOLE_METHODS",
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
    "SERVICE_DEFAULT_CALLER",
    "service_backstop",
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
#: A peer on the GATEWAY listener that presented a per-INSTALL credential
#: (``gateway/peers.json``, minted by a ceremony an operator ran at BOTH
#: installs — R5). Proven the way ``device`` is, and narrower in exactly one
#: respect that matters: what is on the other end is not a person holding a
#: screen but ANOTHER RUNTIME, whose agents drive it. So this kind carries no
#: tier and is answered from :data:`PEER_METHOD_ALLOWLIST` instead — see
#: :func:`authorize_call`.
CALLER_PEER = "peer"
#: A caller the transport could not place. Minted by tests, by the defensive arm
#: of :func:`caller_for_connection`, and — the arm Stage A5 adds — by a gateway
#: connection that somehow reached the dispatcher without a device stamp.
CALLER_UNKNOWN = "unknown"

#: **Exactly what a paired install may call on this one.** Two names, and the
#: shortness is still the design: Stage 6 proved an edge exists (``peer.ping``,
#: which answers "are you there" and touches nothing), Stage 7 lets that edge
#: carry ONE thing (``peer.agent_chat.execute``, a chat turn).
#:
#: **The second entry is a deliberate widening and this comment is its reason.**
#: Canon 06's remote-connector table draws the line in a sentence — *agents
#: never mint or retire agents on another install; a remote OPERATOR does* — and
#: a chat turn is on the permitted side of it: install B runs one of its own
#: agents, in its own chat store, under its own admission rules, and the
#: operator at B sees the turn like any other inbound agent message. What stays
#: excluded is the whole of the level: no create, no retire, no office write, no
#: subscribe. Widening this set is one line in a diff on purpose, and this is
#: what that line is supposed to cost — a stated reason and a citation.
#:
#: An ALLOWLIST and not a tier, and the difference is what it does when the
#: registry grows. A tier comparison admits every future verb that happens to
#: declare the same word — so the day somebody registers a new ``read`` method,
#: every paired install on the LAN can call it, and nobody decided that. A
#: membership test admits nothing it was not edited to admit, which makes
#: widening the peer surface a visible line in a diff with a reason attached.
#:
#: The canon's exclusion therefore holds BY CONSTRUCTION rather than by a rule
#: about two names: ``runtime.agent.create`` and ``runtime.agent.retire`` are
#: not absent from this set because someone remembered to leave them out, they
#: are absent because everything is absent unless it is here. 06's table says
#: agents never mint or retire agents on another install; a test iterates the
#: whole registry against this set rather than naming those two, because a rule
#: pinned by two literals stops being pinned the moment a third verb arrives.
PEER_METHOD_ALLOWLIST: frozenset[str] = frozenset(
    {"peer.ping", "peer.agent_chat.execute"}
)

#: **Verbs whose authority is a KIND and not a strength** — the machine owner at
#: this install's own console, and nobody else, however strong their credential
#: (plan WS4 / ruling R-W1, ``planned/instant-workspace-switching.md``).
#:
#: Why a set and not a tier. The two scope-pointer verbs
#: (``runtime.workspace.use`` / ``runtime.realm.use``) park THIS install's
#: active realm/workspace — the two scalars every local surface reads to decide
#: what it is looking at. R-B's sentence is that a remote cockpit holds its own
#: scope and cannot park the harness, and the tier vocabulary cannot say that:
#: there are two words, the verb is plainly a level-adjacent write so it declares
#: ``console``, and :func:`authorize_call`'s device arm is an EQUALITY — so a
#: device paired at ``console`` (which R11 explicitly contemplates: "a paired
#: console device may chat") would compare equal and be allowed to move the
#: desktop operator's scope out from under them mid-session. The tier is right
#: about STRENGTH and silent about KIND, and this is the kind.
#:
#: **The plan's own sentence was imprecise here and this comment is the
#: correction** (recorded 2026-09-01, WS4 field notes): the survey wrote that
#: the device arm's equality alone refuses a device caller. It refuses a ``read``
#: device; it does not refuse a ``console`` one. Enforcing R-B needed this set.
#:
#: A membership test for the same reason :data:`PEER_METHOD_ALLOWLIST` is one:
#: it admits — here, RESTRICTS — nothing it was not edited to name, so a future
#: ``console`` verb does not silently join, and widening or narrowing this door
#: is a visible line in a diff with a reason attached.
LOCAL_CONSOLE_METHODS: frozenset[str] = frozenset(
    {"runtime.workspace.use", "runtime.realm.use"}
)

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
    #: Set only for :data:`CALLER_PEER`. The OTHER install's id, as its row in
    #: ``gateway/peers.json`` names it — so a refusal can say which paired
    #: install was turned away, which is the fact an operator auditing a
    #: cross-install call needs and the one a per-connection key cannot supply.
    #: There is no ``peer_tier`` beside it, deliberately: a peer's authority is
    #: a membership in :data:`PEER_METHOD_ALLOWLIST`, and a tier field that
    #: nothing read would be a field that looked like a door.
    peer_install_id: str | None = None

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
        if self.peer_install_id is not None:
            payload["peer_install_id"] = self.peer_install_id
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

    **Stage 6's arm sits FIRST among the stamped ones, and a connection that
    carries both stamps is ``unknown``.** The gateway handshake writes exactly
    one of them — a hello naming a device credential and a hello naming a peer
    credential are different frames and the authenticator refuses a frame that
    is both — so a connection wearing two identities is a state the transport
    does not produce. It is answered anyway, and answered with the least
    authority, because the alternative is an ordering that decides which
    identity wins: a rule like "peer beats device" is a rule somebody can flip,
    and the flip would be invisible.
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
    peer_install_id = getattr(connection, "peer_install_id", None)
    peer_install_id = (
        peer_install_id if isinstance(peer_install_id, str) and peer_install_id else None
    )
    if peer_install_id is not None and device_id is not None:
        return RpcCaller(kind=CALLER_UNKNOWN, connection_key=key, transport=transport)
    if peer_install_id is not None:
        return RpcCaller(
            kind=CALLER_PEER,
            connection_key=key,
            transport=transport,
            peer_install_id=peer_install_id,
        )
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
    #: The operator-facing sentence, when the dispatcher's default one would be
    #: WRONG rather than merely terse. It is set by exactly one arm — the
    #: machine-owner restriction (WS4) — because that is the one refusal where
    #: "requires the console tier" is false: the caller may well HOLD console
    #: and be refused anyway. Prose only, and deliberately not on
    #: :meth:`refusal_data`: a message is for an operator's eyes and is free to
    #: change, while ``reason`` is the contract a client branches on.
    #:
    #: ``None`` everywhere else, so the dispatcher's own sentence stays the
    #: default and no existing refusal's wording moves.
    detail: str | None = None

    def refusal_data(self) -> dict[str, Any]:
        """The ``data`` block of the typed refusal. ``reason`` leads because
        that is what a client branches on."""

        return {"reason": self.reason, "tier": self.tier, "caller": self.caller_kind}


def authorize_call(
    tier: str, caller: RpcCaller | None, *, method: str | None = None
) -> CallAuthorization:
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

    **The peer arm is Stage 6, and its POSITION is the load-bearing part.** It
    runs before the read-tier arm, because the read arm is open to everyone —
    that is its whole point, and A5 kept it deliberately — so a peer evaluated
    after it would inherit the entire read surface of this runtime: the office
    core, the subscribe lane, every read verb that has not been written yet.
    That is not what an operator approves when they approve an edge between two
    installs, and it is not what 06's exclusion describes. So a peer is answered
    from :data:`PEER_METHOD_ALLOWLIST` and from nothing else, and the tier the
    verb declares does not enter into it.

    **The machine-owner arm is WS4, and it is a RESTRICTION rather than a
    grant** — the only arm here that can turn an allow into a refusal for a
    caller whose credential is strong enough. A verb in
    :data:`LOCAL_CONSOLE_METHODS` is answerable only to the machine owner's own
    kinds, so a paired device holding ``console`` — which every other console
    verb admits, deliberately — is refused these two with the same typed
    ``scope_denied`` the launcher's decoders already branch on. See that set's
    comment for why a tier could not have said this.

    ``method`` is therefore REQUIRED in practice for a peer and optional in the
    signature, and the asymmetry is intentional: every other caller kind is
    decided by tier alone, so forcing the argument on all of them would rewrite
    call sites that have no use for it (the A4 CLI mirror asks "may the console
    run a console verb", a question with no method in it). A peer arriving with
    no method name is REFUSED rather than defaulted — absence of a name is
    absence of a decision, and the module's second rule is that absence of a
    decision is never an allow.
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
    name = str(method or "").strip()
    # **The machine-owner arm (WS4), and its POSITION is load-bearing.** It runs
    # before every other arm — before the peer allowlist, before the read-tier
    # opening, before the device equality — because each of those answers a
    # question about STRENGTH or about one caller kind, and this one is a
    # restriction that must hold across all of them. Placed after the device arm
    # it would be unreachable for exactly the caller it exists to refuse (a
    # ``console`` device compares equal and returns), which is the bug the
    # comment on :data:`LOCAL_CONSOLE_METHODS` records.
    #
    # A caller with no method name is NOT restricted here, and that is not a
    # hole: the CLI mirror (Stage A4) asks "may the console run a console verb"
    # with no method in it, and it is already the machine owner. Every caller
    # this set exists to refuse arrives through :func:`serve_rpc.handle_request`,
    # which passes the name unconditionally.
    if name and name in LOCAL_CONSOLE_METHODS and resolved.kind not in _CONSOLE_KINDS:
        return CallAuthorization(
            ok=False,
            reason=REASON_SCOPE_DENIED,
            tier=normalized,
            caller_kind=resolved.kind,
            detail=(
                f"{name} is answerable only to this install's own console; "
                f"a {resolved.kind} caller is refused however it is paired"
            ),
        )
    if resolved.kind == CALLER_PEER:
        if name and name in PEER_METHOD_ALLOWLIST:
            return CallAuthorization(
                ok=True,
                reason="peer_allowlisted",
                tier=normalized,
                caller_kind=resolved.kind,
            )
        return CallAuthorization(
            ok=False,
            reason=REASON_SCOPE_DENIED,
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


# ── the backstop (Stage A6) ──────────────────────────────────────────────────

#: The grandfather clause for a service call that named no caller. Spelled as a
#: value, and as the SAME value the CLI doors already mint, so "this call was
#: not authorized by anything" is greppable rather than implicit in a default
#: argument. Every existing caller of the two service functions is exactly this:
#: an operator at the install's own shell, or a test standing in for one.
SERVICE_DEFAULT_CALLER = CLI_CONSOLE


def service_backstop(
    caller: RpcCaller | None, *, method: str, tier: str = TIER_CONSOLE
) -> CallAuthorization | None:
    """The SERVICE-boundary check: ``None`` to proceed, the refusal otherwise.

    Stage A6 of the authorization-chokepoint plan, built 2026-08-30 under the
    wave's ruling. Ruling A picked (b) — the gate goes at the RPC front door,
    where the transport's proof is — and this is NOT that gate and must not be
    mistaken for it. It is option (c)'s second half: **a narrow, non-bypassable
    assertion that a decision was made**, evaluated by the same predicate, at
    the boundary the two write verbs actually live at.

    WHAT IT BUYS, precisely, because a second check that buys nothing is worse
    than no second check. The front-door gate is a property of the DISPATCHER:
    it runs because ``handle_request`` calls :func:`authorize_call` before
    dispatch, and because ``@method`` recorded the right tier. Both of those are
    one edit away from being wrong in a way no service test would see — a method
    re-registered at ``read`` by a typo, a future transport that reaches a
    handler by another path, a refactor that moves dispatch. This check is a
    property of the FUNCTION: it runs whoever called it and however they got
    there, so the level-mutating verbs cannot be reached by a caller the policy
    refuses, even from a lane that forgot to ask.

    WHY IT IS NOT SELF-DECLARATION, which is the one way a second check could
    make things worse. ``caller`` is an :class:`RpcCaller`, and the services take
    their request as a ``params`` DICT. No JSON a client sends can become one:
    the handlers pass ``context.caller``, which only the transport builder mints,
    and the CLI doors pass the :data:`CLI_CONSOLE` constant, which takes no
    arguments precisely so nothing on the invocation can steer it. The gate's
    first rule — read the tier off the caller, never off the request — holds
    here by the same mechanism it holds there.

    ``caller is None`` is the GRANDFATHER, and it is the one place in this module
    where absence is not a refusal. That is deliberate and it is bounded: the
    services have ~60 existing call sites, every one of them an operator at this
    install's own shell or a test standing in for one, and Ruling A's own
    sentencing on option (c) named the alternative — rewriting all of them — as
    the reason (c) was not taken. So the default is :data:`SERVICE_DEFAULT_CALLER`
    and it is a VALUE, not an absent check: the answer to "what authority did
    this call run under" is always a caller kind that appears in the refusal
    data, never a shrug. Note the asymmetry with :func:`authorize_call`, where
    ``None`` is ``unknown`` and refused — there, ``None`` means a transport
    failed to place a caller it was answering for, which is a fact about a live
    connection; here it means no wire was involved at all.
    """

    decision = authorize_call(tier, caller or SERVICE_DEFAULT_CALLER, method=method)
    return None if decision.ok else decision
