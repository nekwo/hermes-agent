"""Regression tests for bounded/lazy CLI MCP startup."""

from __future__ import annotations

from argparse import Namespace
from contextlib import nullcontext
import sys
import threading
import types

import pytest

import cli as cli_mod
from hermes_cli import main as main_mod
from hermes_cli import mcp_startup


@pytest.fixture(autouse=True)
def _reset_mcp_startup_state():
    saved_started = mcp_startup._mcp_discovery_started
    saved_thread = mcp_startup._mcp_discovery_thread
    try:
        mcp_startup._mcp_discovery_started = False
        mcp_startup._mcp_discovery_thread = None
        yield
    finally:
        thread = mcp_startup._mcp_discovery_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        mcp_startup._mcp_discovery_started = saved_started
        mcp_startup._mcp_discovery_thread = saved_thread


def _agent_args(**overrides) -> Namespace:
    base = {
        "accept_hooks": False,
        "command": "chat",
        "cron_command": None,
        "gateway_command": None,
        "mcp_action": None,
        "tui": False,
    }
    base.update(overrides)
    return Namespace(**base)


#: How long the stubbed discovery stays blocked. Bounded rather than forever so
#: a regression that runs discovery INLINE fails with the assertions below
#: instead of hanging the file, and long enough that the wall-clock line can
#: never be the thing that decides the verdict.
_BLOCKED_DISCOVERY_SECONDS = 10.0


def test_prepare_agent_startup_backgrounds_blocking_mcp_for_chat(monkeypatch):
    """The backgrounding contract, held as an EVENT rather than raced on a clock.

    This used to assert ``elapsed < 0.2`` — a stopwatch reading of a claim the
    stopwatch cannot make. It measured 1.109 s on a loaded runner and is one of
    the three tests the flake policy's wall-clock rule was written for.

    What the test is actually about: ``_prepare_agent_startup`` must not run MCP
    discovery inline. That is provable without a clock, because the stub BLOCKS:
    the main call returned while ``_blocking_discover`` was still parked inside
    its wait, and the thread it is parked on is alive. An inline regression
    cannot produce that state at all — it would return only after the stub's
    bounded wait elapsed, with no live discovery thread behind it.
    """

    stop = threading.Event()
    entered = threading.Event()
    left = threading.Event()
    calls = {"mcp": 0}

    def _blocking_discover():
        calls["mcp"] += 1
        entered.set()
        stop.wait(_BLOCKED_DISCOVERY_SECONDS)
        left.set()

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            read_raw_config=lambda: {"mcp_servers": {"demo": {"transport": "stdio"}}},
            load_config=lambda: {},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "agent.shell_hooks",
        types.SimpleNamespace(register_from_config=lambda *_a, **_k: None),
    )
    # Stub mcp_oauth so the background thread doesn't pay the real (cold,
    # ~0.75s) ``tools.mcp_oauth`` import before calling discovery. This test
    # asserts the *backgrounding contract* (discovery runs off-thread), not
    # OAuth suppression, and the unrelated import latency would otherwise sit
    # inside the window this test waits on the discovery thread to enter.
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_oauth",
        types.SimpleNamespace(suppress_interactive_oauth=lambda: nullcontext()),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_tool",
        types.SimpleNamespace(discover_mcp_tools=_blocking_discover),
    )

    try:
        main_mod._prepare_agent_startup(_agent_args())

        # Discovery ran, and it ran somewhere this thread is not: the call above
        # has already returned while the stub is still inside its wait.
        assert entered.wait(_BLOCKED_DISCOVERY_SECONDS), (
            "background MCP discovery never started"
        )
        assert calls["mcp"] == 1
        # The discriminating line, and the one with no clock in it: an INLINE
        # regression could only return here after the stub had left its wait.
        assert not left.is_set()
        assert mcp_startup._mcp_discovery_thread is not None
        assert mcp_startup._mcp_discovery_thread.is_alive()
    finally:
        stop.set()


def test_background_mcp_discovery_suppresses_interactive_oauth(monkeypatch):
    state = {"active": False, "during_discover": None}

    class SuppressInteractiveOAuth:
        def __enter__(self):
            state["active"] = True

        def __exit__(self, *_exc):
            state["active"] = False

    def _discover():
        state["during_discover"] = state["active"]

    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            read_raw_config=lambda: {"mcp_servers": {"demo": {"url": "https://mcp.example.test/mcp"}}},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_oauth",
        types.SimpleNamespace(
            suppress_interactive_oauth=lambda: SuppressInteractiveOAuth(),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_tool",
        types.SimpleNamespace(discover_mcp_tools=_discover),
    )

    mcp_startup.start_background_mcp_discovery(
        logger=types.SimpleNamespace(debug=lambda *_a, **_k: None),
        thread_name="test-mcp-discovery",
    )
    assert mcp_startup._mcp_discovery_thread is not None
    mcp_startup._mcp_discovery_thread.join(timeout=1.0)

    assert state["during_discover"] is True
    assert state["active"] is False








def _retry_logger():
    return types.SimpleNamespace(
        debug=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
    )


def _install_retry_stubs(monkeypatch, *, connected: bool, calls: dict):
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.config",
        types.SimpleNamespace(
            read_raw_config=lambda: {"mcp_servers": {"demo": {"transport": "stdio"}}},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_oauth",
        types.SimpleNamespace(suppress_interactive_oauth=lambda: nullcontext()),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.mcp_tool",
        types.SimpleNamespace(
            discover_mcp_tools=lambda: calls.__setitem__("mcp", calls["mcp"] + 1),
            get_mcp_status=lambda: [{"connected": connected}],
        ),
    )




