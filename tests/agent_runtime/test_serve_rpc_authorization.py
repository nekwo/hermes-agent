"""Stage A3 — the front-door gate, with an empty policy.

Every frame on both transports now passes an authorization decision before a
handler runs. The decision allows every caller that exists, which is the point:
the stage lands the enforcement POINT, so R11's per-device scopes become an edit
to one predicate instead of an architecture change, and Stage 1 of the gateway
plan stops being blocked on "there is nowhere to hook".

The two halves this file has to prove are opposites, and both are load-bearing:

1. **Nothing observable moved.** Every caller that works today still works —
   the stdio owner, an authenticated socket peer, and the bare
   ``handle_request(req)`` the argv-lane probes and every unit test in this repo
   use. A gate that quietly refused one of those would be a regression the rest
   of the suite would report as a hundred unrelated failures, so it is asserted
   HERE, directly, in the terms the plan promised.
2. **The gate is live, not decorative.** A caller kind nothing yet mints is
   refused, with the typed reason the launcher's decoders branch on. Without
   this half, "allow everything" and "no gate at all" are the same tree.

Registry-driven, for the third time in this lane and the same reason: the
coverage test iterates ``_METHODS`` rather than naming the ten verbs, so a
method added tomorrow is covered today and cannot ship outside the gate.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_LOCAL_CONSOLE,
    CALLER_STDIO_OWNER,
    CALLER_UNKNOWN,
    LOCAL_CONSOLE,
    REASON_SCOPE_DENIED,
    REASON_UNKNOWN_TIER,
    STDIO_OWNER,
    TIER_CONSOLE,
    TIER_READ,
    UNKNOWN_CALLER,
    RpcCaller,
    authorize_call,
)


def _request(name: str, rid: str = "r1") -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": name, "params": {}}


# ── the predicate, on its own ───────────────────────────────────────────────


def test_every_caller_that_exists_today_may_run_a_console_verb():
    """The grandfather clause, stated as a value rather than left implicit in an
    absent check. Both kinds the transport can mint are allowed, and the reason
    says WHY they are allowed — so the day Stage A5 narrows it, the diff is one
    line and the receipts already name what changed."""

    for caller in (STDIO_OWNER, LOCAL_CONSOLE):
        decision = authorize_call(TIER_CONSOLE, caller)
        assert decision.ok, caller
        assert decision.reason == "console_grandfathered"


def test_a_caller_the_transport_could_not_place_is_refused_a_console_verb():
    """The ruling's own words: the default tier for an unrecognised or tierless
    credential is refuse-console-verbs, not allow."""

    decision = authorize_call(TIER_CONSOLE, UNKNOWN_CALLER)

    assert decision.ok is False
    assert decision.reason == REASON_SCOPE_DENIED
    assert decision.refusal_data() == {
        "reason": REASON_SCOPE_DENIED,
        "tier": TIER_CONSOLE,
        "caller": CALLER_UNKNOWN,
    }


def test_a_missing_caller_is_unknown_and_never_the_owner():
    """A ``None`` here means somebody built a context by a path that never asked
    the transport. Absence of a decision must not read as an allow."""

    assert authorize_call(TIER_CONSOLE, None).ok is False
    assert authorize_call(TIER_CONSOLE, None).caller_kind == CALLER_UNKNOWN


def test_reads_stay_open_to_a_caller_that_has_proved_nothing():
    """A read tier is what a caller who proved nothing may still do, and nothing
    on the read side mutates a level. Stage A5 gates reads — if it gates them at
    all — on a device record, not on the absence of one."""

    assert authorize_call(TIER_READ, UNKNOWN_CALLER).ok is True
    assert authorize_call(TIER_READ, None).ok is True


def test_an_unrecognised_tier_refuses_rather_than_waving_through():
    """Unreachable through ``method()``, which rejects one at import. Asserted
    because a typo in a registration is exactly the case where a door is open
    and nobody meant it to be."""

    decision = authorize_call("wizard", LOCAL_CONSOLE)

    assert decision.ok is False
    assert decision.reason == REASON_UNKNOWN_TIER


def test_a_blank_tier_reads_as_console_and_not_as_no_requirement():
    """The strongest tier is the safe answer to "nobody said"."""

    assert authorize_call("", UNKNOWN_CALLER).ok is False
    assert authorize_call("", UNKNOWN_CALLER).tier == TIER_CONSOLE


# ── the dispatcher, driven by the registry ──────────────────────────────────


def test_every_registered_method_dispatches_through_the_gate():
    """KILLING MUTATION: delete the ``authorize_call`` block in
    ``handle_request`` and this reds for every method at once.

    Iterates the real registry, so a verb added tomorrow is covered today. The
    assertion is that a caller nothing mints is refused BEFORE the handler runs
    — proven by the frame shape, since a handler that ran would answer with its
    own result or its own typed refusal, never with ``scope_denied``.
    """

    refused = serve_rpc.RpcContext(caller=UNKNOWN_CALLER)

    for name, tier in serve_rpc.method_tiers().items():
        frame = serve_rpc.handle_request(_request(name), refused)
        if tier == TIER_READ:
            # A read is not refused; it is answered (or it refuses on its own
            # terms, e.g. a missing workspace_id). What matters is that the
            # answer is never the GATE's.
            data = frame.get("error", {}).get("data", {})
            assert data.get("reason") != REASON_SCOPE_DENIED, name
            continue
        assert "result" not in frame, name
        assert frame["error"]["code"] == serve_rpc.ERR_HANDLER_FAILED, name
        assert frame["error"]["data"] == {
            "reason": REASON_SCOPE_DENIED,
            "tier": TIER_CONSOLE,
            "caller": CALLER_UNKNOWN,
        }, name


def test_a_refused_call_never_reaches_its_handler():
    """The refusal is a gate refusal, not a handler that ran and said no. Proven
    by pointing the registry at a handler that would fail the test if entered."""

    entered: list[str] = []

    def _tripwire(rid, params, context=None):
        entered.append("ran")
        return serve_rpc.ok(rid, {})

    original = serve_rpc._METHODS["runtime.agent.retire"]
    serve_rpc._METHODS["runtime.agent.retire"] = _tripwire
    try:
        frame = serve_rpc.handle_request(
            _request("runtime.agent.retire"),
            serve_rpc.RpcContext(caller=UNKNOWN_CALLER),
        )
    finally:
        serve_rpc._METHODS["runtime.agent.retire"] = original

    assert entered == []
    assert frame["error"]["data"]["reason"] == REASON_SCOPE_DENIED


def test_an_unknown_method_is_answered_before_the_gate_runs():
    """Ordering, asserted. A gate refusal that varied by whether the name exists
    would let a refused caller map the surface by watching the error code
    change; ``method_not_found`` tells them nothing a manifest would not."""

    frame = serve_rpc.handle_request(
        _request("runtime.nothing.here"), serve_rpc.RpcContext(caller=UNKNOWN_CALLER)
    )

    assert frame["error"]["code"] == serve_rpc.ERR_METHOD_NOT_FOUND
    assert frame["error"]["data"]["reason"] == "unknown_method"


def test_a_malformed_frame_is_still_a_normalizer_refusal_not_a_scope_one():
    """The gate sits AFTER normalization, so a caller who cannot spell a request
    is told that, rather than being told it lacks a tier it never asked for."""

    frame = serve_rpc.handle_request(
        {"jsonrpc": "1.0", "id": "r9", "method": "runtime.agent.retire"},
        serve_rpc.RpcContext(caller=UNKNOWN_CALLER),
    )

    assert frame["error"]["data"]["reason"] == "bad_jsonrpc_version"


# ── the promise: nothing observable moved ───────────────────────────────────


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(None, id="omitted-entirely"),
        pytest.param(serve_rpc.RpcContext(), id="bare-context"),
        pytest.param(
            serve_rpc.RpcContext(connection_key="c1", transport="socket"),
            id="context-built-by-hand-without-a-caller",
        ),
        pytest.param(
            serve_rpc.RpcContext(
                connection_key="c1",
                transport="socket",
                caller=RpcCaller(
                    kind=CALLER_LOCAL_CONSOLE,
                    connection_key="c1",
                    transport="socket",
                ),
            ),
            id="an-authenticated-socket-peer",
        ),
        pytest.param(
            serve_rpc.RpcContext(caller=RpcCaller(kind=CALLER_STDIO_OWNER)),
            id="the-stdio-owner",
        ),
    ],
)
def test_no_caller_that_works_today_is_refused_by_the_gate(context):
    """The A3 promise, in the terms the plan wrote it in: landing A1–A3 must not
    change any current caller's observable outcome.

    Every context shape that exists in this tree is here — including the two
    that carry no caller at all, which is what the argv lane's probes and every
    unit test in the repo pass. The assertion is deliberately NOT "the call
    succeeds": most of these have no seeded store and refuse on their own terms.
    It is that the refusal is never the GATE's, which is the only thing this
    stage could have changed.
    """

    for name in serve_rpc.method_names():
        frame = (
            serve_rpc.handle_request(_request(name))
            if context is None
            else serve_rpc.handle_request(_request(name), context)
        )
        data = frame.get("error", {}).get("data", {}) or {}
        assert data.get("reason") not in (
            REASON_SCOPE_DENIED,
            REASON_UNKNOWN_TIER,
        ), f"{name} refused a caller that works today"


def test_the_gate_is_what_separates_the_two_and_it_is_actually_running():
    """The pair, side by side, on the same verb. If the gate were removed both
    would answer the same way and the first assertion would red — which is what
    makes "allow everything" a different tree from "no gate at all"."""

    allowed = serve_rpc.handle_request(
        _request("runtime.agent.create"), serve_rpc.RpcContext(caller=LOCAL_CONSOLE)
    )
    refused = serve_rpc.handle_request(
        _request("runtime.agent.create"), serve_rpc.RpcContext(caller=UNKNOWN_CALLER)
    )

    assert refused["error"]["data"]["reason"] == REASON_SCOPE_DENIED
    # The allowed one reached the handler and refused on the HANDLER's terms
    # (no persona_id in an empty params dict), which is the shipped behaviour.
    assert allowed.get("error", {}).get("data", {}).get("reason") != (
        REASON_SCOPE_DENIED
    )
