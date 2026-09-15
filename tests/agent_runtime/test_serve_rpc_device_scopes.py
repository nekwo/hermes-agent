"""Stage A5: a paired device's tier becomes a real refusal at the front door.

A3 landed the enforcement point with an empty policy and asserted, directly
rather than by inference from a green suite, that no existing caller's outcome
moved. This file is the other half of that bargain — the first caller the
policy actually refuses — and it carries A3's assertion forward: every test in
``test_serve_rpc_authorization.py`` must still pass unchanged, and the local
callers are re-asserted here too, because "we did not break the grandfather" is
a claim about THIS commit and not about the one that made the promise.

Nothing here touches a socket. The transport half — that a gateway connection is
the only thing that ever mints a device caller — is proven over a real listener
in ``test_serve_gateway_lane.py``; what is proven here is the PREDICATE, which
is the thing a wire test cannot isolate.
"""

from __future__ import annotations

import pytest

from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    CALLER_LOCAL_CONSOLE,
    CALLER_STDIO_OWNER,
    CALLER_UNKNOWN,
    CLI_CONSOLE,
    LOCAL_CONSOLE,
    REASON_SCOPE_DENIED,
    STDIO_OWNER,
    TIER_CONSOLE,
    TIER_READ,
    TRANSPORT_GATEWAY,
    UNKNOWN_CALLER,
    RpcCaller,
    authorize_call,
    caller_for_connection,
)


class _Connection:
    """The duck the transport hands the dispatcher. Deliberately not a
    ``SocketConnection``: ``caller_for_connection``'s contract is that it reads
    attributes off whatever it was given without importing the socket module, and
    a test that used the real class would not exercise that."""

    def __init__(self, **fields):
        self.key = fields.pop("key", "conn-1")
        self.transport = fields.pop("transport", "socket")
        self.authenticated = fields.pop("authenticated", True)
        for name, value in fields.items():
            setattr(self, name, value)


def device(tier: str = TIER_CONSOLE, *, device_id: str = "dev_abc") -> RpcCaller:
    return caller_for_connection(
        _Connection(
            transport=TRANSPORT_GATEWAY, device_id=device_id, device_tier=tier
        )
    )


# ── the caller the transport mints ──────────────────────────────────────────


def test_a_stamped_gateway_connection_becomes_a_device_caller_carrying_its_tier(
):
    caller = device(TIER_READ, device_id="dev_phone")

    assert caller.kind == CALLER_DEVICE
    assert caller.device_id == "dev_phone"
    assert caller.device_tier == TIER_READ
    assert caller.transport == TRANSPORT_GATEWAY
    # The refusal and the log line can name WHICH device was turned away — the
    # fact a per-connection key cannot supply, because it means nothing across
    # two connections.
    assert caller.describe()["device_id"] == "dev_phone"
    assert caller.describe()["device_tier"] == TIER_READ


def test_a_gateway_connection_with_no_device_stamp_is_unknown_never_local_console():
    """The structural guard. Without it the fall-through would hand a REMOTE peer
    the machine owner's authority the moment any future change let a gateway
    connection through with an empty stamp — and the grandfather clause is
    supposed to be about the machine owner, not about whoever reached a listener.
    """

    caller = caller_for_connection(_Connection(transport=TRANSPORT_GATEWAY))

    assert caller.kind == CALLER_UNKNOWN
    assert authorize_call(TIER_CONSOLE, caller).ok is False


def test_a_gateway_connection_whose_tier_this_build_cannot_read_is_unknown():
    """A stamp is only a stamp if both halves are usable. A device id with a
    junk tier is not a narrower device — it is an unplaceable caller."""

    caller = caller_for_connection(
        _Connection(
            transport=TRANSPORT_GATEWAY, device_id="dev_abc", device_tier="superuser"
        )
    )

    assert caller.kind == CALLER_UNKNOWN


def test_an_unauthenticated_gateway_connection_is_unknown_even_when_stamped():
    """``authenticated`` is read rather than assumed, and it is read FIRST: a
    stamp on a connection that never passed its proof is a claim, not a fact."""

    caller = caller_for_connection(
        _Connection(
            transport=TRANSPORT_GATEWAY,
            authenticated=False,
            device_id="dev_abc",
            device_tier=TIER_CONSOLE,
        )
    )

    assert caller.kind == CALLER_UNKNOWN


def test_the_loopback_socket_still_mints_local_console_unchanged():
    """The byte-identical promise, at the predicate's own door. A device stamp is
    something the loopback listener never applies, so this connection is exactly
    the object A2 shipped."""

    caller = caller_for_connection(_Connection(transport="socket", key="conn-9"))

    assert caller.kind == CALLER_LOCAL_CONSOLE
    assert caller.device_id is None
    assert caller.device_tier is None
    assert caller.describe() == {
        "kind": CALLER_LOCAL_CONSOLE,
        "transport": "socket",
        "connection_key": "conn-9",
    }


def test_stdio_is_still_the_owner_and_carries_no_device_fields():
    caller = caller_for_connection(None)

    assert caller is STDIO_OWNER
    assert caller.kind == CALLER_STDIO_OWNER
    assert "device_id" not in caller.describe()


# ── the policy ──────────────────────────────────────────────────────────────


def test_a_console_tier_device_may_run_a_console_verb():
    decision = authorize_call(TIER_CONSOLE, device(TIER_CONSOLE))

    assert decision.ok is True
    assert decision.reason == "device_tier"
    assert decision.caller_kind == CALLER_DEVICE


def test_a_read_tier_device_is_refused_a_console_verb_with_the_typed_reason():
    """``data.reason`` is what the launcher's decoders branch on first and the
    numeric code second, so this string is as much a shape as the frame."""

    decision = authorize_call(TIER_CONSOLE, device(TIER_READ))

    assert decision.ok is False
    assert decision.reason == REASON_SCOPE_DENIED
    assert decision.refusal_data() == {
        "reason": REASON_SCOPE_DENIED,
        "tier": TIER_CONSOLE,
        "caller": CALLER_DEVICE,
    }


def test_a_read_tier_device_may_still_run_read_verbs():
    """The whole point of shipping ``read`` as a representable tier rather than
    as a field with one value: a viewer device is useful."""

    assert authorize_call(TIER_READ, device(TIER_READ)).ok is True
    assert authorize_call(TIER_READ, device(TIER_CONSOLE)).ok is True


def test_a_device_whose_tier_field_was_lost_is_refused_rather_than_defaulted():
    """Constructed directly, because the transport cannot produce this — which is
    exactly why the predicate must not assume it never will. Absence of a
    decision is never an allow."""

    caller = RpcCaller(
        kind=CALLER_DEVICE, transport=TRANSPORT_GATEWAY, device_id="dev_abc"
    )

    assert authorize_call(TIER_CONSOLE, caller).ok is False
    assert authorize_call(TIER_CONSOLE, caller).reason == REASON_SCOPE_DENIED


# ── A3's promise, re-asserted rather than assumed ───────────────────────────


@pytest.mark.parametrize("caller", [STDIO_OWNER, LOCAL_CONSOLE, CLI_CONSOLE, None])
def test_every_caller_a3_grandfathered_is_still_allowed_every_tier(caller):
    """A5's no-behaviour-change half. ``None`` is in the list because
    ``authorize_call`` resolves it to UNKNOWN, and read stays open to unknown —
    which is A3's deliberate line, kept rather than inherited by omission."""

    if caller is None:
        assert authorize_call(TIER_READ, None).ok is True
        assert authorize_call(TIER_CONSOLE, None).ok is False
        return
    assert authorize_call(TIER_READ, caller).ok is True
    assert authorize_call(TIER_CONSOLE, caller).ok is True
    assert authorize_call(TIER_CONSOLE, caller).reason == "console_grandfathered"


def test_the_unknown_caller_is_refused_console_and_allowed_read_exactly_as_before():
    assert authorize_call(TIER_CONSOLE, UNKNOWN_CALLER).ok is False
    assert authorize_call(TIER_CONSOLE, UNKNOWN_CALLER).reason == REASON_SCOPE_DENIED
    assert authorize_call(TIER_READ, UNKNOWN_CALLER).ok is True


def test_an_unknown_tier_still_refuses_every_caller_including_a_device():
    """A typo in a registration is exactly the case where a door is open and
    nobody meant it to be. The device arm must not become a second way past it."""

    assert authorize_call("admin", device(TIER_CONSOLE)).ok is False
    assert authorize_call("admin", device(TIER_CONSOLE)).reason == "unknown_tier"
    assert authorize_call("admin", LOCAL_CONSOLE).ok is False
