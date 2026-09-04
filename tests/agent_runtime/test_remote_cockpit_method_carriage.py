"""The seven method lanes a REMOTE-aimed cockpit refuses are answerable to a paired device.

Why this file exists
--------------------
The launcher's ``mission_method_lane_aim.dart`` refuses, in one place, every
JSON-RPC method-lane call whose aim is a remote install: the local ``harness
serve`` child is non-null in both cases, so binding it would answer correctly
about the WRONG machine. That refusal is right and it is not the end state —
the carriage that replaces it routes the same call at the remote install's
connector instead.

That carriage is a launcher change, and it rests on a hermes fact: these seven
verbs must be answerable to a paired ``console``-tier device. At the time this
file was written they already were, and nothing said so. ``LOCAL_CONSOLE_METHODS``
is a hand-maintained frozenset whose whole purpose is to make a strong-enough
credential refuse anyway, and one line added to it would retract the carriage for
one of these verbs with every other hermes test still green — the launcher would
find out in the field, against an install it cannot debug.

So this is a NEGATIVE guarantee stated positively: not "these are the verbs a
device may run" (the tier vocabulary says that, per verb, and says it well) but
"none of the seven the cockpit currently refuses is kind-restricted, and each is
declared, registered and authorized". A cross-stack precondition, pinned in the
repo that can break it.

What this file is NOT
---------------------
It is not an argument that any of the seven SHOULD be remote-answerable — that
was decided when each declared its tier, and the arguments are on
``_METHOD_TIERS`` and on ``LOCAL_CONSOLE_METHODS``. If a future ruling moves one
of these verbs into the local-only set, the honest edit is to move its row out of
the table below IN THE SAME COMMIT, with the reason, and to tell the launcher.
The test's job is to make that a conversation rather than a surprise.

It also touches no socket. Whether a gateway connection mints a device caller at
all is proven over a real listener in ``test_serve_gateway_lane.py``; what is
proven here is the policy, which a wire test cannot isolate.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    LOCAL_CONSOLE_METHODS,
    TIER_CONSOLE,
    TIER_READ,
    TRANSPORT_GATEWAY,
    RpcCaller,
    authorize_call,
    caller_for_connection,
)


class _Connection:
    """The duck the transport hands the dispatcher.

    Deliberately not a ``SocketConnection``, for the reason
    ``test_serve_rpc_device_scopes`` gives its own copy: ``caller_for_connection``
    reads attributes off whatever it is handed without importing the socket
    module, and a test using the real class would not exercise that.
    """

    def __init__(self, **fields):
        self.key = fields.pop("key", "conn-remote-cockpit")
        self.transport = fields.pop("transport", TRANSPORT_GATEWAY)
        self.authenticated = fields.pop("authenticated", True)
        for name, value in fields.items():
            setattr(self, name, value)


def _console_device() -> RpcCaller:
    return caller_for_connection(
        _Connection(device_id="dev_remote_cockpit", device_tier=TIER_CONSOLE)
    )


#: The seven lanes ``mission_method_lane_aim`` refuses when the cockpit is aimed
#: at a remote install, each with the tier hermes declares for it. The tier is
#: REPEATED here rather than read off ``method_tiers()`` on purpose: a table that
#: derived the expected value from the thing under test would go green through a
#: tier change, which is exactly the move this file exists to notice.
REMOTE_COCKPIT_METHODS: dict[str, str] = {
    "runtime.office.upsert": TIER_CONSOLE,
    "runtime.office.remove": TIER_CONSOLE,
    "runtime.office.surface.update": TIER_CONSOLE,
    "runtime.office.resolve_conflict": TIER_CONSOLE,
    "runtime.agent.create": TIER_CONSOLE,
    "runtime.agent.retire": TIER_CONSOLE,
    # A read, and argued as one on ``_METHOD_TIERS``: it writes no store state,
    # emits no event and mints no id. A viewer device that may not place an agent
    # may certainly warm the cache that makes its own reads fast.
    "runtime.persona.prewarm": TIER_READ,
}


@pytest.mark.parametrize("name", sorted(REMOTE_COCKPIT_METHODS))
def test_the_carriage_exists_for_every_lane_the_remote_cockpit_refuses(name: str):
    """Arm 1 — the verb is registered. Without this the launcher carriage has
    nothing to aim at and the refusal is the only honest answer."""

    assert name in serve_rpc.method_names()


@pytest.mark.parametrize("name,tier", sorted(REMOTE_COCKPIT_METHODS.items()))
def test_every_lane_declares_the_tier_the_launcher_will_check(name: str, tier: str):
    """Arm 2 — the tier rides ``manifest()``'s ``tiers`` block, so a connector
    knows before it dials whether the credential it holds can carry the gesture.
    A verb whose tier moved silently would have the launcher offering a control
    that refuses on press."""

    assert serve_rpc.method_tiers().get(name) == tier
    assert name in serve_rpc.manifest()["tiers"]


@pytest.mark.parametrize("name", sorted(REMOTE_COCKPIT_METHODS))
def test_no_lane_is_kind_restricted_to_this_installs_own_console(name: str):
    """Arm 3 — the one arm that can turn a strong-enough credential into a
    refusal. ``LOCAL_CONSOLE_METHODS`` is about KIND, not strength, and the tier
    vocabulary cannot express it; membership here is invisible to arms 1 and 2
    and to every tier assertion in the tree."""

    assert name not in LOCAL_CONSOLE_METHODS


@pytest.mark.parametrize("name,tier", sorted(REMOTE_COCKPIT_METHODS.items()))
def test_a_console_tier_paired_device_is_authorized_for_every_lane(
    name: str, tier: str
):
    """Arm 4 — the composed answer, asked the way ``serve_rpc.handle_request``
    asks it: tier, caller and METHOD together. Arms 1-3 can each be true while
    this is false, because authorization is a fold over all three."""

    decision = authorize_call(tier, _console_device(), method=name)

    assert decision.ok is True, decision.detail
    assert decision.caller_kind == CALLER_DEVICE


def test_the_table_is_the_launchers_refusal_set_and_not_a_sample():
    """The table above is a claim about the OTHER repo, so it says so out loud.

    Seven is the count ``mission_method_lane_aim``'s own docstring records
    ("Seven bindings shared the shape"). A table that drifted to six would keep
    passing while the eighth lane the launcher refuses went unpinned, which is
    the vacuity this house sweeps for.
    """

    assert len(REMOTE_COCKPIT_METHODS) == 7
    assert set(REMOTE_COCKPIT_METHODS) <= set(serve_rpc.method_names())
