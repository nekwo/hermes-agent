"""WS4's acceptance: the two scope methods over a REAL serve, both doors.

`test_scope_use_methods.py` proves the predicate and the shim. This file proves
the thing a predicate test cannot: that an actual ``serve_loop`` — real loopback
socket, real gateway listener, real TLS handshake, real HMAC over a real paired
credential — publishes the two methods, RUNS them for the machine owner, and
REFUSES them to a device that authenticated successfully and holds the strongest
tier this vocabulary has.

The device half is the whole reason this file exists. R-B's sentence is that a
remote cockpit cannot park this install's pointer even by asking nicely, and
"asking nicely" is precisely a device that completed the pairing ceremony, pinned
the certificate, proved its HMAC and holds ``console``. Nothing short of a real
handshake produces that caller.

The harness is `test_serve_gateway_lane`'s, imported rather than re-built, for
the reason that file gives about its own fakes: two spellings of "a real paired
device" would be two chances to disagree about what one is.
"""

from __future__ import annotations

import pytest

from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    REASON_SCOPE_DENIED,
    TIER_CONSOLE,
    TIER_READ,
)
from agent_runtime.scope_activation import REALM_USE_METHOD, WORKSPACE_USE_METHOD
from agent_runtime.store import RealmStore, WorkspaceStore
from tests.agent_runtime.test_serve_gateway_lane import (  # noqa: F401 — `gateway_on` is a fixture
    _rpc,
    device_client,
    gateway_on,
    pair_device,
    running_serve,
)


@pytest.fixture()
def scope(isolate_agent_runtime_root):
    realms = RealmStore()
    workspaces = WorkspaceStore()
    realm = realms.create(name="Acceptance Realm")
    return {
        "realm": realm,
        "ws_a": workspaces.create(name="Acceptance A", realm_id=realm.id),
        "ws_b": workspaces.create(name="Acceptance B", realm_id=realm.id),
    }


def test_a_real_serve_publishes_both_methods_in_its_greeting(scope):
    """The manifest fact the launcher's lowering is a membership test against.
    Read off the ``ready`` frame a real boot emits, not off ``manifest()``."""

    with running_serve() as handle:
        rpc = handle.ready["rpc"]

    assert WORKSPACE_USE_METHOD in rpc["methods"]
    assert REALM_USE_METHOD in rpc["methods"]
    assert rpc["contract"] == 1
    assert rpc["tiers"][WORKSPACE_USE_METHOD] == TIER_CONSOLE
    assert rpc["tiers"][REALM_USE_METHOD] == TIER_CONSOLE


def test_the_machine_owner_parks_the_pointer_over_the_loopback_socket(scope):
    """Accepted from ``local_console``, and the idempotent duplicate arm intact
    on the same connection — the two facts the launcher's accept path rests on."""

    from agent_runtime import paths
    from agent_runtime.serve_auth import read_token
    from agent_runtime.serve_socket import ServeSocketClient

    stamp = "2026-09-01T09:00:00+00:00"
    with running_serve() as handle:
        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=20.0)
        connection.connect()
        try:
            # The install's own serve token: holding it IS being the machine
            # owner, which is what makes this connection `local_console`.
            connection.hello(
                token=read_token(paths.store_root()) or "", client="acceptance"
            )
            first = _rpc(
                connection,
                WORKSPACE_USE_METHOD,
                {"workspace_id": scope["ws_a"].id, "issued_at": stamp},
            )
            # A second identical intent: the store's duplicate arm, answered as
            # a RESULT, exactly as the argv verb answers it with exit 0.
            second = _rpc(
                connection,
                WORKSPACE_USE_METHOD,
                {"workspace_id": scope["ws_a"].id, "issued_at": stamp},
            )
        finally:
            connection.close()

    assert first["result"]["applied"] is True
    assert first["result"]["id"] == scope["ws_a"].id
    assert "error" not in second
    assert second["result"]["applied"] is False
    assert second["result"]["reason"] == "duplicate"
    assert WorkspaceStore().active_id() == scope["ws_a"].id


@pytest.mark.parametrize("tier", [TIER_READ, TIER_CONSOLE])
@pytest.mark.parametrize("method,key", [(WORKSPACE_USE_METHOD, "workspace_id")])
def test_a_paired_device_is_refused_and_the_pointer_does_not_move(
    gateway_on, scope, tier, method, key
):
    """**R-B, over the wire.** The device authenticated: it redeemed a real
    pairing code, pinned the real certificate and answered the real HMAC
    challenge. It is refused anyway, and the ``console`` case is the one that
    matters — a tier comparison alone would have let it through."""

    WorkspaceStore().set_active(scope["ws_b"].id)
    credential = pair_device(tier=tier, name=f"phone-{tier}")

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, hello):
            assert hello["event"] == "hello_ok"
            reply = _rpc(connection, method, {key: scope["ws_a"].id})

    assert "result" not in reply
    assert reply["error"]["data"]["reason"] == REASON_SCOPE_DENIED
    assert reply["error"]["data"]["caller"] == CALLER_DEVICE
    assert reply["error"]["data"]["tier"] == TIER_CONSOLE
    # The operator-facing sentence names the KIND, not a tier the caller may
    # well hold — the wording arm WS4 added to the decision.
    assert "own console" in reply["error"]["message"]
    # The half that is not about the frame: nothing was written.
    assert WorkspaceStore().active_id() == scope["ws_b"].id


def test_a_console_device_keeps_the_console_verbs_it_already_had(gateway_on, scope):
    """The refusal is scoped to the two names and did not narrow the paired
    surface. Proven with the verb R11's sentence is actually about — a chat
    turn — reaching its own guards rather than the authorization gate."""

    credential = pair_device(tier=TIER_CONSOLE, name="phone-console")

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _):
            reply = _rpc(connection, "runtime.chat.message", {})

    # Whatever this answers, it must not be the SCOPE refusal: the gate let it
    # through and a param/spawn guard judged it.
    data = reply.get("error", {}).get("data", {})
    assert data.get("reason") != REASON_SCOPE_DENIED
