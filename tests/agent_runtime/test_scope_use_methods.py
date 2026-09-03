"""WS4: the scope pointer's method lane, and the door that is closed to devices.

Plan `EterniaLauncher/docs/mission_control/planned/instant-workspace-switching.md`
§1.4, ruling R-W1. Three claims are under test and they are different kinds of
claim, which is why they are in one file:

1. **One implementation, two doors.** ``harness workspace use`` and
   ``runtime.workspace.use`` are the same operation, so the strong form of the
   test is not "the method returns something reasonable" but *the method's
   result is the argv envelope's row, key for key*. A fork would have to change
   one of them to pass.
2. **The declined arms are RESULTS.** ``superseded`` and ``duplicate`` exit 0 on
   the argv lane; a method that rendered them as JSON-RPC errors would make the
   launcher's accept path raise the R-A parked-elsewhere surface for a switch
   that worked exactly as designed.
3. **R-B's enforcement point.** A device credential is refused — INCLUDING a
   device paired at ``console``, which is the case the plan's own sentence
   missed and the reason ``LOCAL_CONSOLE_METHODS`` exists at all. The peer half
   needs no test here: ``test_peer_authorization`` iterates the whole registry
   against ``PEER_METHOD_ALLOWLIST``, so these two names arrived covered.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    CALLER_LOCAL_CONSOLE,
    CALLER_PEER,
    LOCAL_CONSOLE,
    LOCAL_CONSOLE_METHODS,
    PEER_METHOD_ALLOWLIST,
    REASON_SCOPE_DENIED,
    STDIO_OWNER,
    TIER_CONSOLE,
    TIER_READ,
    TRANSPORT_GATEWAY,
    RpcCaller,
    authorize_call,
    caller_for_connection,
)
from agent_runtime.scope_activation import (
    REALM_USE_METHOD,
    WORKSPACE_USE_METHOD,
    activate_realm,
    activate_workspace,
    perform_scope_activation,
)
from agent_runtime.serve_rpc import RpcContext
from agent_runtime.store import RealmStore, WorkspaceStore


class _Connection:
    """The duck ``caller_for_connection`` reads through ``getattr`` — the same
    stand-in ``test_serve_rpc_device_scopes`` uses, and for its reason: the
    contract is that the predicate never imports the socket module."""

    def __init__(self, **fields):
        self.key = fields.pop("key", "conn-1")
        self.transport = fields.pop("transport", TRANSPORT_GATEWAY)
        self.authenticated = fields.pop("authenticated", True)
        self.device_id = None
        self.device_tier = None
        self.peer_install_id = None
        for name, value in fields.items():
            setattr(self, name, value)


def _device(tier: str) -> RpcCaller:
    return caller_for_connection(
        _Connection(device_id="dev_phone", device_tier=tier)
    )


_PEER = RpcCaller(
    kind=CALLER_PEER, transport=TRANSPORT_GATEWAY, peer_install_id="inst_far_away"
)


@pytest.fixture()
def scope(isolate_agent_runtime_root):
    """Two realms, two workspaces, nothing active — the smallest world in which
    a switch and a reconcile are both observable."""

    realms = RealmStore()
    workspaces = WorkspaceStore()
    realm_a = realms.create(name="Realm A")
    realm_b = realms.create(name="Realm B")
    ws_a = workspaces.create(name="WS A", realm_id=realm_a.id)
    ws_b = workspaces.create(name="WS B", realm_id=realm_b.id)
    return {
        "realm_a": realm_a,
        "realm_b": realm_b,
        "ws_a": ws_a,
        "ws_b": ws_b,
    }


def _call(method: str, params: dict, *, caller: RpcCaller | None = None) -> dict:
    """One request through ``handle_request`` — the DISPATCHER, not the handler,
    because the authorization is at the dispatcher and a test that called the
    handler directly would prove nothing about the gate."""

    return serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "r-1", "method": method, "params": params},
        context=RpcContext(caller=caller if caller is not None else LOCAL_CONSOLE),
    )


# ── registration ─────────────────────────────────────────────────────────────


def test_both_methods_are_registered_at_the_console_tier():
    names = serve_rpc.method_names()

    assert WORKSPACE_USE_METHOD in names
    assert REALM_USE_METHOD in names
    assert serve_rpc.method_tier(WORKSPACE_USE_METHOD) == TIER_CONSOLE
    assert serve_rpc.method_tier(REALM_USE_METHOD) == TIER_CONSOLE


def test_the_manifest_carries_both_names_and_the_contract_integer_does_not_move():
    """Adding a method grows the SET; the integer means an incompatible SHAPE
    change (``serve_rpc``'s own header). The launcher's lowering is a membership
    test against exactly this set — the D12 pattern — so this is the wire fact
    the launcher half depends on."""

    manifest = serve_rpc.manifest()

    assert WORKSPACE_USE_METHOD in manifest["methods"]
    assert REALM_USE_METHOD in manifest["methods"]
    assert manifest["contract"] == serve_rpc.RPC_CONTRACT_VERSION == 1
    assert manifest["tiers"][WORKSPACE_USE_METHOD] == TIER_CONSOLE
    assert manifest["tiers"][REALM_USE_METHOD] == TIER_CONSOLE


def test_the_restricted_set_names_the_two_registered_scope_methods():
    """``LOCAL_CONSOLE_METHODS`` holds LITERALS, not imports — deliberately, for
    the reason ``PEER_METHOD_ALLOWLIST`` does: ``call_authorization`` is the
    policy module and must not import a service to answer a question about a
    name. The cost of literals is a typo that silently opens the door, so the
    join is pinned here instead.

    Both directions matter. A name in the set that no method registers is a
    dead restriction; a scope method NOT in the set is an open door. The
    registry is walked rather than the two names re-typed, so a third scope verb
    added later fails this instead of shipping ungated.
    """

    # S2d put THREE peer-directory verbs in the same set, and they are a
    # different KIND of fact from the scope pointers: the directory is the
    # operator's own map of their network (which machines they paired, what
    # those are called, the addresses they answer at), and one of the three
    # DIALS on the caller's behalf. Same gate, same reason — the tier vocabulary
    # has two words, both about strength, and this is about kind — so this test
    # asserts the SCOPE half exactly and leaves the peer half to
    # ``test_serve_gateway_peers_rpc.py``, which owns its argument.
    assert {WORKSPACE_USE_METHOD, REALM_USE_METHOD} <= LOCAL_CONSOLE_METHODS
    registry = set(serve_rpc.method_names())
    assert LOCAL_CONSOLE_METHODS <= registry, "a restriction on a method that does not exist"
    scope_methods = {name for name in registry if name.endswith((".workspace.use", ".realm.use"))}
    assert scope_methods == {WORKSPACE_USE_METHOD, REALM_USE_METHOD}, (
        "a scope verb reached the registry ungated"
    )
    assert scope_methods <= LOCAL_CONSOLE_METHODS


def test_neither_method_joins_the_peer_allowlist():
    """The exclusion holds by CONSTRUCTION — everything is absent unless it is
    named — and ``test_peer_authorization``'s registry walk is what enforces it
    without an edit. This assertion is the readable restatement, never the thing
    doing the work."""

    assert not (LOCAL_CONSOLE_METHODS & PEER_METHOD_ALLOWLIST)
    for name in (WORKSPACE_USE_METHOD, REALM_USE_METHOD):
        assert not authorize_call(TIER_CONSOLE, _PEER, method=name).ok


# ── the gate (R-B's enforcement point) ───────────────────────────────────────


@pytest.mark.parametrize("method", sorted(LOCAL_CONSOLE_METHODS))
@pytest.mark.parametrize("caller", [STDIO_OWNER, LOCAL_CONSOLE])
def test_the_machine_owner_may_park_the_pointer(method, caller):
    decision = authorize_call(TIER_CONSOLE, caller, method=method)

    assert decision.ok
    assert decision.caller_kind in {caller.kind}


@pytest.mark.parametrize("method", sorted(LOCAL_CONSOLE_METHODS))
@pytest.mark.parametrize("tier", [TIER_READ, TIER_CONSOLE])
def test_a_device_is_refused_at_every_tier_it_can_hold(method, tier):
    """**The test this stage exists for.** The ``read`` half would pass on the
    tier equality alone; the ``console`` half is the one the plan's sentence
    missed, and it is the one R-B is about — a paired console device is exactly
    what R11 contemplates for chat, and it must still not be able to park the
    desktop operator's scope."""

    decision = authorize_call(TIER_CONSOLE, _device(tier), method=method)

    assert not decision.ok
    assert decision.reason == REASON_SCOPE_DENIED
    assert decision.caller_kind == CALLER_DEVICE
    assert decision.refusal_data() == {
        "reason": REASON_SCOPE_DENIED,
        "tier": TIER_CONSOLE,
        "caller": CALLER_DEVICE,
    }


def test_the_restriction_does_not_leak_onto_other_console_verbs():
    """A console device keeps everything it had. The set restricts what it
    names and nothing else — the property a tier comparison could not have,
    because a tier admits every verb that happens to declare the same word."""

    console_device = _device(TIER_CONSOLE)
    for name in serve_rpc.method_names():
        if name in LOCAL_CONSOLE_METHODS or name in PEER_METHOD_ALLOWLIST:
            continue
        assert authorize_call(
            serve_rpc.method_tier(name), console_device, method=name
        ).ok, name


def test_the_dispatcher_renders_the_refusal_and_never_reaches_the_handler(scope):
    """End to end through ``handle_request``: a device asking gets the typed
    frame, and — the half that matters — the pointer does NOT move."""

    before = WorkspaceStore().active_id()
    reply = _call(
        WORKSPACE_USE_METHOD,
        {"workspace_id": scope["ws_a"].id},
        caller=_device(TIER_CONSOLE),
    )

    assert "result" not in reply
    assert reply["error"]["data"]["reason"] == REASON_SCOPE_DENIED
    assert reply["error"]["data"]["caller"] == CALLER_DEVICE
    assert WorkspaceStore().active_id() == before


# ── the accept, and its identity with the argv verb ──────────────────────────


def test_the_method_result_is_the_argv_verbs_row_key_for_key(scope, capsys):
    """ONE implementation, proven by comparing the two doors' answers rather
    than by reading the source. The argv row is taken through the real CLI
    handler so the Stage-42 envelope's serialization is in the comparison."""

    from hermes_cli import harness

    reply = _call(WORKSPACE_USE_METHOD, {"workspace_id": scope["ws_a"].id})
    method_row = reply["result"]

    # Reset the pointer so the argv door performs the same write, not a
    # duplicate — the arms are deliberately different and this test is about
    # the APPLIED one.
    WorkspaceStore().set_active(None)
    root = argparse.ArgumentParser()
    harness.build_parser(root.add_subparsers(dest="cmd"))
    args = root.parse_args(["harness", "workspace", "use", scope["ws_a"].id, "--json"])
    assert args.func(args) == 0
    # The Stage-42 envelope is FLAT: `{schema_version, kind, **row}`. Strip the
    # two envelope keys and what is left is the row — which is what the method
    # lane answers, because the shape is not an RPC invention.
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["kind"] == "workspace"
    argv_row = {
        key: value
        for key, value in envelope.items()
        if key not in {"schema_version", "kind"}
    }

    assert method_row == argv_row
    assert method_row["applied"] is True
    assert method_row["id"] == scope["ws_a"].id


def test_an_applied_workspace_switch_moves_the_pointer(scope):
    reply = _call(WORKSPACE_USE_METHOD, {"workspace_id": scope["ws_b"].id})

    assert reply["result"]["applied"] is True
    assert WorkspaceStore().active_id() == scope["ws_b"].id


def test_a_realm_switch_reconciles_the_workspace_under_it(scope):
    """The reconcile is inside the shared implementation, so it happens on the
    method lane by construction rather than because the handler remembered."""

    WorkspaceStore().set_active(scope["ws_a"].id)

    reply = _call(REALM_USE_METHOD, {"realm_id": scope["realm_b"].id})

    assert reply["result"]["applied"] is True
    assert RealmStore().active_id() == scope["realm_b"].id
    assert WorkspaceStore().active_id() == scope["ws_b"].id


def test_a_duplicate_send_is_an_idempotent_RESULT_not_an_error(scope):
    """The launcher retries a scope switch under the same stamp on a transport
    replay. Both sends must read as success; an error frame here would raise the
    R-A parked-elsewhere surface for a switch that already landed."""

    stamp = "2026-09-01T12:00:00+00:00"
    params = {"workspace_id": scope["ws_a"].id, "issued_at": stamp}

    first = _call(WORKSPACE_USE_METHOD, params)
    second = _call(WORKSPACE_USE_METHOD, params)

    assert first["result"]["applied"] is True
    assert "error" not in second
    assert second["result"]["applied"] is False
    assert second["result"]["reason"] == "duplicate"
    assert second["result"]["superseded"] is False
    assert second["result"]["requested_workspace_id"] == scope["ws_a"].id
    assert WorkspaceStore().active_id() == scope["ws_a"].id


def test_a_stale_intent_is_superseded_and_the_newer_pointer_stands(scope):
    """The supersede basis is the argv verb's ``--issued-at``, threaded through
    unchanged — a late-delivered switch presents the ORIGINAL instant and loses,
    rather than clobbering the newer one (Stage 13 write-path integrity)."""

    _call(
        WORKSPACE_USE_METHOD,
        {"workspace_id": scope["ws_b"].id, "issued_at": "2026-09-01T12:00:05+00:00"},
    )

    late = _call(
        WORKSPACE_USE_METHOD,
        {"workspace_id": scope["ws_a"].id, "issued_at": "2026-09-01T12:00:00+00:00"},
    )

    assert "error" not in late
    assert late["result"]["applied"] is False
    assert late["result"]["superseded"] is True
    assert late["result"]["id"] == scope["ws_b"].id
    assert late["result"]["requested_workspace_id"] == scope["ws_a"].id
    assert WorkspaceStore().active_id() == scope["ws_b"].id


def test_the_result_is_json_serializable(scope):
    """The row carries a ``datetime``. The argv lane serializes through
    Stage-42's printer; the method lane calls the same ``to_jsonable``, and this
    is what proves it — a frame ``json.dumps`` refuses never reaches a client."""

    reply = _call(REALM_USE_METHOD, {"realm_id": scope["realm_a"].id})

    assert json.loads(json.dumps(reply))["result"]["applied"] is True


def test_a_correlation_id_is_echoed_and_a_blank_one_is_not(scope):
    with_id = _call(
        WORKSPACE_USE_METHOD,
        {"workspace_id": scope["ws_a"].id, "correlation_id": " g-scope-7 "},
    )
    assert with_id["result"]["correlation_id"] == "g-scope-7"

    WorkspaceStore().set_active(None)
    without = _call(
        WORKSPACE_USE_METHOD,
        {"workspace_id": scope["ws_a"].id, "correlation_id": "   "},
    )
    assert "correlation_id" not in without["result"]


# ── the two refusals, and they are the argv lane's own ───────────────────────


@pytest.mark.parametrize(
    "method,key",
    [(WORKSPACE_USE_METHOD, "workspace_id"), (REALM_USE_METHOD, "realm_id")],
)
def test_a_missing_id_is_invalid_params(scope, method, key):
    reply = _call(method, {})

    assert reply["error"]["code"] == serve_rpc.ERR_INVALID_PARAMS
    assert reply["error"]["data"]["reason"] == f"{key}_required"


@pytest.mark.parametrize(
    "method,key,entity",
    [
        (WORKSPACE_USE_METHOD, "workspace_id", "workspace"),
        (REALM_USE_METHOD, "realm_id", "realm"),
    ],
)
def test_an_unknown_id_is_not_found_and_writes_nothing(scope, method, key, entity):
    before = (WorkspaceStore().active_id(), RealmStore().active_id())

    reply = _call(method, {key: "does_not_exist"})

    assert reply["error"]["code"] == serve_rpc.ERR_NOT_FOUND
    assert reply["error"]["data"]["reason"] == f"{entity}_not_found"
    assert reply["error"]["data"][key] == "does_not_exist"
    assert (WorkspaceStore().active_id(), RealmStore().active_id()) == before


# ── the shared implementation, called directly ───────────────────────────────


def test_the_shim_and_the_shared_function_agree(scope):
    """``perform_scope_activation`` adds params validation and serialization and
    nothing else. If it ever grew a decision of its own, this would diverge."""

    direct = activate_workspace(scope["ws_a"].id)
    WorkspaceStore().set_active(None)
    through_shim = perform_scope_activation(
        {"workspace_id": scope["ws_a"].id}, verb=WORKSPACE_USE_METHOD
    )

    from agent_runtime.serde import to_jsonable

    assert through_shim.refusal is None
    assert through_shim.result == to_jsonable(direct)


def test_the_method_lane_inherits_WS1s_scope_patch_for_free(scope, monkeypatch):
    """The cross-lane property, asserted rather than assumed.

    WS1 put the ``scope`` patch emission INSIDE ``WorkspaceStore.set_active`` —
    the store chokepoint — and this lane's shared implementation goes through
    that same call. So a switch carried by the method lane produces the same
    patch a switch carried by argv produces, with no WS4 code knowing the patch
    exists. Had either lane instead emitted from its own handler, this would be
    the test that failed.

    FOUR events since the straddle ruling (2026-09-01), not two: ``ws_b``
    belongs to realm B and the realm pointer starts cleared, so the applied
    workspace selection pulls the realm pointer after it — the second half of
    ``scope_activation``'s never-straddle invariant. Both writes go through
    their store's ``set_active``, which is exactly what this test is about: the
    new arm inherited the patch for free too, and the LAST row carries the
    settled pair.
    """

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.events import EventLog
    from agent_runtime.state_patches import (
        SCOPE_ENTITY,
        SCOPE_PATCH_ID,
        STATE_PATCHED_EVENT_TYPE,
    )

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = True
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

    log = EventLog()
    before = len(log.tail(200))
    reply = _call(WORKSPACE_USE_METHOD, {"workspace_id": scope["ws_b"].id})
    emitted = [
        {"type": event.type, "payload": dict(event.payload or {})}
        for event in log.tail(200)
    ][before:]

    assert reply["result"]["applied"] is True
    assert [event["type"] for event in emitted] == [
        STATE_PATCHED_EVENT_TYPE,
        "workspace.activated",
        STATE_PATCHED_EVENT_TYPE,
        "realm.activated",
    ]
    assert emitted[0]["payload"]["entity"] == SCOPE_ENTITY
    assert emitted[0]["payload"]["id"] == SCOPE_PATCH_ID
    assert emitted[0]["payload"]["changed"]["active_workspace_id"] == scope["ws_b"].id
    assert emitted[-2]["payload"]["changed"] == {
        "active_workspace_id": scope["ws_b"].id,
        "active_realm_id": scope["realm_b"].id,
    }


def test_activate_realm_is_the_only_place_the_reconcile_is_spelled(scope):
    """A door that forgot the reconcile would be a bug reachable from one lane
    only. Both doors call this function, so the fallback is structural."""

    WorkspaceStore().set_active(scope["ws_a"].id)

    row = activate_realm(scope["realm_b"].id)

    assert row["applied"] is True
    assert WorkspaceStore().active_id() == scope["ws_b"].id


def test_a_STAMPED_realm_switch_still_reconciles_the_workspace_under_it(scope):
    """The FIELDED shape, and the one the tests above did not run.

    ``mission_scope_use_client`` stamps every switch (``_scopeIntentIssuedAt``),
    so on the live lane ``activate_realm`` threads ONE basis into TWO
    ``set_active`` writes — the realm pointer's, then the reconcile's on the
    workspace pointer. The realm write leaves that basis stored; the reconcile
    then presents the SAME instant to a different pointer file. If the
    compare-and-set arms were keyed on anything shared between the two files,
    the second write would decline as ``duplicate``/``superseded`` and the
    active workspace would be left pointing into the realm the operator just
    left — the launcher would then be told a scope hermes is not parked in.

    (Investigated on 2026-09-01 as a candidate cause of the realm-switch field
    defect. It is NOT the cause — the pointers land correctly, as the live store
    showed — and this test is what makes that a fact rather than a reading.)
    """

    stamp = "2026-09-01T12:45:44.052220+00:00"
    WorkspaceStore().set_active(scope["ws_a"].id, issued_at=stamp)

    reply = _call(
        REALM_USE_METHOD, {"realm_id": scope["realm_b"].id, "issued_at": stamp}
    )

    assert reply["result"]["applied"] is True
    assert RealmStore().active_id() == scope["realm_b"].id
    assert WorkspaceStore().active_id() == scope["ws_b"].id


def test_a_realm_switch_emits_a_scope_patch_whose_LAST_row_carries_both_new_pointers(
    scope, monkeypatch
):
    """The wire shape the launcher's fold consumes, pinned at the producer.

    A realm switch is TWO store writes, so it emits TWO ``scope`` rows in one
    coalesced batch — the field receipt reads ``fold_applied 2 of 2 rows
    (scope x2)``. The first is mid-reconcile: the realm pointer has moved and
    the workspace pointer has not, so it names a workspace in the realm the
    operator just LEFT. Only the second is the settled pair.

    The launcher folds them in order and the last one wins, so the claim that
    matters is about ORDER and about the final row's contents — a producer that
    emitted them the other way round, or that omitted the second, would leave
    every fielded client holding a straddled scope with no receipt anywhere.
    """

    from agent_runtime import state_patches as sp
    from agent_runtime.config import load_agent_runtime_config
    from agent_runtime.events import EventLog
    from agent_runtime.state_patches import SCOPE_ENTITY, STATE_PATCHED_EVENT_TYPE

    def _loader(*args, **kwargs):
        cfg = load_agent_runtime_config(*args, **kwargs)
        cfg.read_model.delta_patches = True
        return cfg

    monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

    WorkspaceStore().set_active(scope["ws_a"].id)
    log = EventLog()
    before = len(log.tail(200))

    reply = _call(REALM_USE_METHOD, {"realm_id": scope["realm_b"].id})

    emitted = [
        {"type": event.type, "payload": dict(event.payload or {})}
        for event in log.tail(200)
    ][before:]
    assert reply["result"]["applied"] is True
    assert [event["type"] for event in emitted] == [
        STATE_PATCHED_EVENT_TYPE,
        "realm.activated",
        STATE_PATCHED_EVENT_TYPE,
        "workspace.activated",
    ]
    scope_rows = [
        event["payload"]
        for event in emitted
        if event["payload"].get("entity") == SCOPE_ENTITY
    ]
    assert len(scope_rows) == 2
    # Non-vacuity: the two rows genuinely differ, so "the last one wins" is a
    # claim with something behind it.
    assert scope_rows[0]["changed"] == {
        "active_workspace_id": scope["ws_a"].id,
        "active_realm_id": scope["realm_b"].id,
    }
    assert scope_rows[-1]["changed"] == {
        "active_workspace_id": scope["ws_b"].id,
        "active_realm_id": scope["realm_b"].id,
    }
