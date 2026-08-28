"""A PARKED MCP server must not cost a turn its tools.

Measured 2026-08-27: four consecutive ``hermes harness mission-chat message``
turns to the SAME session (persona ``qa``, instance
``personainst_qa_agent_c1a70d19``) reported ``profile_timing
.mcp_admitted_servers`` of 3, 0, 3, 0. Perfect alternation, not noise — and the
Stage C skill's retry-once rule masks a strict 2-cycle exactly, which is why it
took four back-to-back turns to see it.

THE MECHANISM, and it is a two-turn cycle by construction:

* R2 leaves the TRANSPORT warm across turns and tears the REGISTRY scope down
  after each one. A server can therefore be cached in ``tools.mcp_tool._servers``
  with ``session is None`` — parked, or mid-reconnect — and with no registered
  tools at all.
* ``_live_mcp_sessions`` calls that state COLD and routes it to
  ``register_mcp_servers``, on the written belief that the latter "has dedicated
  wake handling for exactly that case".
* ``register_mcp_servers`` has a wake and no REGISTRATION. It fires
  ``_signal_reconnect`` — fire and forget, deliberately not the
  ``_signal_reconnect_and_wait`` sibling the tool-call path uses — and then hits
  ``if not new_servers: return _existing_tool_names()``. The name is already in
  ``_servers``, so ``new_servers`` is empty, so it returns a STALE list of tools
  it did not register and exits. Nothing lands in the registry.
* ``admit_mcp_servers`` does not trust that return value (rightly): it re-reads
  ``registered_mcp_server_names()``. Which is empty. **Admitted: 0**, three
  ``mcp_not_registered_on_lane`` denials, and the turn runs with no MCP tools.
* The nudge lands seconds later on the background loop, so the NEXT turn finds a
  live session, takes the warm path, and re-registers all three. **Admitted: 3.**
  That turn is also the only one that registers a teardown, and the only one
  that can kill the transport by using it — so the turn after it is parked
  again. The two turns do opposite things, which is what makes it alternate
  rather than settle.

THE FIX: the admission layer stops handing a parked server to a function that
cannot register it. It nudges the parked ones, WAITS a bounded moment for the
sessions it just asked for, and routes whatever came back through the warm path
that re-registers off a live session. Failing to wake still falls through to the
cold path exactly as before — this widens nothing, it only stops throwing a
turn's tools away while the cure is already in flight.

The existing ``test_a_parked_server_is_treated_as_cold`` asserted only that
``_live_mcp_sessions`` excludes a session-less entry. Nothing in the suite ever
ran ``_default_registrar`` on one, which is why a turn admitting zero servers
had no test standing in front of it.
"""

from __future__ import annotations

import threading
import time

import pytest

from agent_runtime import mcp_admission as admission_module

from tests.agent_runtime.test_mcp_admission_r2 import (  # noqa: F401 - fixtures
    _FakeMcpTool,
    _LAUNCHER_QA_FULL_SURFACE,
    _registered_launcher_qa_tools,
    clean_registry,
)


class _ParkedServer:
    """A cached server with NO session, whose reconnect nudge revives it.

    This is the live shape ``_signal_reconnect`` talks to, minus the transport:
    ``_reconnect_event`` is the only thing that path reaches for, and setting it
    is what eventually puts a session back. ``revive_after`` is how long the
    background loop takes to land the reconnect — zero for "already there by the
    time we look", nonzero to prove the fix WAITS rather than peeking once.
    """

    def __init__(self, name: str, tool_names, *, revive_after: float = 0.0,
                 revives: bool = True):
        self.name = name
        self.session = None
        self.tool_timeout = 5.0
        self._tools = [_FakeMcpTool(tool) for tool in tool_names]
        self.nudges = 0
        self._revive_after = revive_after
        self._revives = revives
        self._reconnect_event = self

    # ``_signal_reconnect`` finds this object under ``_reconnect_event`` and,
    # with no running MCP loop, calls ``.set()`` on it directly.
    def set(self) -> None:
        self.nudges += 1
        if not self._revives:
            return
        if self._revive_after <= 0:
            self.session = object()
            return
        timer = threading.Timer(self._revive_after, self._wake)
        timer.daemon = True
        timer.start()

    def _wake(self) -> None:
        self.session = object()


@pytest.fixture
def impatient_wake(monkeypatch):
    """Shrink the wake budget so the give-up case costs a moment, not seconds."""

    monkeypatch.setattr(admission_module, "_PARKED_WAKE_TIMEOUT_SECONDS", 0.3)


def test_a_parked_server_is_woken_and_its_tools_are_registered(
    monkeypatch, clean_registry
):
    """The 0-turn of the measured 3/0/3/0, and the whole defect in one assert.

    Before the fix this registered NOTHING: the nudge went out, the registrar
    returned a stale name list it had not registered, and the turn ran with no
    MCP tools while the reconnect it had just asked for was already landing.
    """

    import tools.mcp_tool as mcp_tool
    from agent_runtime.mcp_admission import _default_registrar

    parked = _ParkedServer("launcher_qa", _LAUNCHER_QA_FULL_SURFACE)
    monkeypatch.setitem(mcp_tool._servers, "launcher_qa", parked)

    _default_registrar({"launcher_qa": {"command": "noop"}})

    assert parked.nudges >= 1, "the parked server was never asked to reconnect"
    assert len(_registered_launcher_qa_tools(clean_registry)) == len(
        _LAUNCHER_QA_FULL_SURFACE
    )


def test_the_wake_waits_for_a_reconnect_that_lands_late(monkeypatch, clean_registry):
    """A peek is not a wait.

    The whole reason the turn lost its tools is that the nudge is asynchronous:
    the session comes back on the background MCP loop a moment later. A fix that
    nudged and then re-read ``session`` immediately would reproduce the defect
    exactly.
    """

    import tools.mcp_tool as mcp_tool
    from agent_runtime.mcp_admission import _default_registrar

    parked = _ParkedServer(
        "launcher_qa", _LAUNCHER_QA_FULL_SURFACE, revive_after=0.3
    )
    monkeypatch.setitem(mcp_tool._servers, "launcher_qa", parked)

    _default_registrar({"launcher_qa": {"command": "noop"}})

    assert len(_registered_launcher_qa_tools(clean_registry)) == len(
        _LAUNCHER_QA_FULL_SURFACE
    )


def test_a_server_that_will_not_wake_is_bounded_and_registers_nothing(
    monkeypatch, clean_registry, impatient_wake
):
    """Fails CLOSED and fails FAST — the same two properties R2 already holds.

    A genuinely dead server must not be re-registered off a session it does not
    have, and must not hold the turn open either: the wake is bounded, the
    server falls through to the cold path exactly as it does today, and an
    honest ``mcp_not_registered_on_lane`` is the outcome.
    """

    import tools.mcp_tool as mcp_tool
    from agent_runtime.mcp_admission import _default_registrar

    parked = _ParkedServer(
        "launcher_qa", _LAUNCHER_QA_FULL_SURFACE, revives=False
    )
    monkeypatch.setitem(mcp_tool._servers, "launcher_qa", parked)

    started = time.monotonic()
    _default_registrar({"launcher_qa": {"command": "noop"}})
    elapsed = time.monotonic() - started

    assert _registered_launcher_qa_tools(clean_registry) == set()
    assert elapsed < 3.0, f"the bounded wake was not bounded ({elapsed:.2f}s)"


def test_a_live_server_is_not_nudged(monkeypatch, clean_registry):
    """The warm path is untouched: no wake, no wait, no extra reconnect.

    The 3-turn of the cycle must keep costing what it costs today.
    """

    import tools.mcp_tool as mcp_tool
    from agent_runtime.mcp_admission import _default_registrar

    warm = _ParkedServer("launcher_qa", _LAUNCHER_QA_FULL_SURFACE)
    warm.session = object()
    monkeypatch.setitem(mcp_tool._servers, "launcher_qa", warm)

    _default_registrar({"launcher_qa": {"command": "noop"}})

    assert warm.nudges == 0
    assert len(_registered_launcher_qa_tools(clean_registry)) == len(
        _LAUNCHER_QA_FULL_SURFACE
    )
