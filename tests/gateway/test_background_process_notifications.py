"""Tests for configurable background process notification modes.

The gateway process watcher pushes status updates to users' chats when
background terminal commands run.  ``display.background_process_notifications``
controls verbosity: off | result (default) | error | all.

Contributed by @PeterFile (PR #593), reimplemented on current main.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _parse_session_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Return pre-canned sessions, then None once exhausted."""

    def __init__(self, sessions, consumed=False):
        self._sessions = list(sessions)
        self._consumed = consumed

    def get(self, session_id):
        if self._sessions:
            return self._sessions.pop(0)
        return None

    def is_completion_consumed(self, session_id):
        return self._consumed


def _build_runner(monkeypatch, tmp_path, mode: str) -> GatewayRunner:
    """Create a GatewayRunner with a fake config for the given mode."""
    (tmp_path / "config.yaml").write_text(
        f"display:\n  background_process_notifications: {mode}\n",
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner


def _watcher_dict(session_id="proc_test", thread_id=""):
    d = {
        "session_id": session_id,
        "check_interval": 0,
        "platform": "telegram",
        "chat_id": "123",
    }
    if thread_id:
        d["thread_id"] = thread_id
    return d


# ---------------------------------------------------------------------------
# _load_background_notifications_mode unit tests
# ---------------------------------------------------------------------------

class TestLoadBackgroundNotificationsMode:

    def test_defaults_to_result(self, monkeypatch, tmp_path):
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "result"

    def test_reads_config_yaml(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: error\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "error"

    def test_env_var_overrides_config(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: error\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.setenv("HERMES_BACKGROUND_NOTIFICATIONS", "off")
        assert GatewayRunner._load_background_notifications_mode() == "off"

    def test_false_value_maps_to_off(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: false\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "off"

    def test_invalid_value_defaults_to_result(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: banana\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "result"


# ---------------------------------------------------------------------------
# _run_process_watcher integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumed_completion_skips_raw_notification(monkeypatch, tmp_path):
    """#65379: after process(wait) already returned the completion inline,
    the gateway watcher must NOT also push the raw
    "[Background process ... finished with exit code ...]" message.

    The agent-notify branch already honored _completion_consumed, but its
    skip fell through to the text-notification branch, double-delivering the
    same output to the chat (observed on Slack with
    background_process_notifications: all)."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="sleep 1; echo done",
    )]
    monkeypatch.setattr(
        pr_module, "process_registry", _FakeRegistry(sessions, consumed=True)
    )

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    # notify_on_complete=True mirrors the reported scenario: the watcher's
    # agent-notify skip must not fall through to a raw adapter.send().
    watcher = _watcher_dict()
    watcher["notify_on_complete"] = True
    await runner._run_process_watcher(watcher)

    adapter.send.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumed_completion_skips_raw_notification_without_agent_notify(
    monkeypatch, tmp_path
):
    """#65379 variant: same double-delivery guard for plain watchers
    (notify_on_complete=False) — wait/log consumption suppresses the raw
    completion message in every mode."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="echo done",
    )]
    monkeypatch.setattr(
        pr_module, "process_registry", _FakeRegistry(sessions, consumed=True)
    )

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_watcher_dict())

    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_watch_notification_routes_from_session_store_origin(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="123",
            user_name="Emiliyan",
        )
    )

    evt = {
        "session_id": "proc_watch",
        "session_key": "agent:main:telegram:group:-100:42",
    }

    await runner._inject_watch_notification("[SYSTEM: Background process matched]", evt)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.internal is True
    assert synth_event.source.platform == Platform.TELEGRAM
    assert synth_event.source.chat_id == "-100"
    assert synth_event.source.chat_type == "group"
    assert synth_event.source.thread_id == "42"
    assert synth_event.source.user_id == "123"
    assert synth_event.source.user_name == "Emiliyan"


@pytest.mark.asyncio
async def test_agent_notification_carries_message_id_reply_anchor(monkeypatch, tmp_path):
    """notify_on_complete direct-send carries the triggering message_id so the
    compact notification can be reply-anchored back into a Telegram DM topic.

    Without an anchor, Telegram private-chat topic sends fall back to the main
    chat (see _thread_kwargs_for_send / telegram_dm_topic_reply_fallback)."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="SMOKE_OK\n", exited=True, exit_code=0, command="sleep 1",
    )]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    watcher = {
        "session_id": "proc_anchor",
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123:24296",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "24296",
        "message_id": "555",
        "notify_on_complete": True,
    }
    await runner._run_process_watcher(watcher)

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    args, kwargs = adapter.send.await_args
    assert args[0] == "123"
    assert "Background process proc_anchor finished with exit code 0" in args[1]
    assert "SMOKE_OK" in args[1]
    assert kwargs["reply_to"] == "555"
    assert kwargs["metadata"] == {"thread_id": "24296"}


@pytest.mark.asyncio
async def test_agent_notification_no_message_id_is_tolerated(monkeypatch, tmp_path):
    """A watcher dict without message_id (CLI spawn, pre-upgrade checkpoint)
    still sends directly — reply anchor is simply None."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="sleep 1",
    )]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    watcher = {
        "session_id": "proc_anchorless",
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123:24296",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "24296",
        "notify_on_complete": True,
    }
    await runner._run_process_watcher(watcher)

    adapter.handle_message.assert_not_awaited()
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.kwargs["reply_to"] is None


@pytest.mark.asyncio
async def test_notify_on_complete_legacy_agent_turn_requires_explicit_opt_in(
    monkeypatch, tmp_path
):
    """The old full-agent notification turn remains available only by explicit opt-in."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="sleep 1",
    )]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    monkeypatch.setenv("HERMES_BACKGROUND_AGENT_TURNS", "true")

    runner = _build_runner(monkeypatch, tmp_path, "result")
    adapter = runner.adapters[Platform.TELEGRAM]

    watcher = {
        "session_id": "proc_legacy",
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123:24296",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "24296",
        "notify_on_complete": True,
    }
    await runner._run_process_watcher(watcher)

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_on_complete_legacy_agent_turn_can_be_enabled_by_config(
    monkeypatch, tmp_path
):
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="sleep 1",
    )]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    monkeypatch.delenv("HERMES_BACKGROUND_AGENT_TURNS", raising=False)

    runner = _build_runner(monkeypatch, tmp_path, "result")
    (tmp_path / "config.yaml").write_text(
        "display:\n"
        "  background_process_notifications: result\n"
        "  background_process_agent_turns: true\n",
        encoding="utf-8",
    )
    adapter = runner.adapters[Platform.TELEGRAM]

    watcher = {
        "session_id": "proc_legacy_config",
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123:24296",
        "platform": "telegram",
        "chat_id": "123",
        "thread_id": "24296",
        "notify_on_complete": True,
    }
    await runner._run_process_watcher(watcher)

    adapter.handle_message.assert_awaited_once()
    adapter.send.assert_not_awaited()


def test_long_running_heartbeat_mentions_stop_guidance():
    text = GatewayRunner._format_long_running_heartbeat(
        elapsed_seconds=75,
        status_detail=" — running: pytest",
    )

    assert "Working" in text
    assert "1 min" in text
    assert "pytest" in text
    assert "/stop" in text


@pytest.mark.asyncio
async def test_inject_watch_notification_carries_message_id_reply_anchor(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:dm:123:24296"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            thread_id="24296",
            user_id="1",
            user_name="Fabio",
        )
    )

    evt = {
        "session_id": "proc_watch",
        "session_key": "agent:main:telegram:dm:123:24296",
        "message_id": "777",
    }

    await runner._inject_watch_notification("[SYSTEM: Background process matched]", evt)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.message_id == "777"
    assert synth_event.source.thread_id == "24296"


@pytest.mark.asyncio
async def test_inject_watch_notification_ignores_foreground_event_source(monkeypatch, tmp_path):
    """Negative test: watch notification must NOT route to the foreground thread."""
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    # Session store has the process's original thread (thread 42)
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="proc_owner",
            user_name="alice",
        )
    )

    # The evt dict carries the correct session_key — NOT a foreground event
    evt = {
        "session_id": "proc_cross_thread",
        "session_key": "agent:main:telegram:group:-100:42",
    }

    await runner._inject_watch_notification("[SYSTEM: watch match]", evt)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    # Must route to thread 42 (process origin), NOT some other thread
    assert synth_event.source.thread_id == "42"
    assert synth_event.source.user_id == "proc_owner"


# ---------------------------------------------------------------------------
# _parse_session_key helper
# ---------------------------------------------------------------------------


def test_parse_session_key_with_extra_parts():
    """6th part in a group key may be a user_id, not a thread_id — omit it."""
    result = _parse_session_key("agent:main:discord:group:chan123:thread456")
    assert result == {"platform": "discord", "chat_type": "group", "chat_id": "chan123"}


# ---------------------------------------------------------------------------
# api_server (stateless) wake routing — gateway/wake.py self-post path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inject_watch_notification_raw_session_key_self_posts(monkeypatch, tmp_path):
    """An event whose session_key is a RAW api_server session id (not an
    agent:main:... structured key) must wake the real session via the
    /v1/chat/completions self-post instead of being dropped for missing
    routing metadata."""
    runner = _build_runner(monkeypatch, tmp_path, "all")
    api_adapter = SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AsyncMock(),
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="m",
    )
    runner.adapters[Platform.API_SERVER] = api_adapter

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod
    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    evt = {
        "session_id": "proc_watch",
        "session_key": "raw-hq-session-id",  # no agent:main:... structure
    }
    result = await runner._inject_watch_notification("[SYSTEM: subagent finished]", evt)

    assert result is True
    api_adapter.handle_message.assert_not_awaited()
    assert posts == [
        {"text": "[SYSTEM: subagent finished]", "session_id": "raw-hq-session-id"}
    ]


@pytest.mark.asyncio
async def test_inject_watch_notification_origin_session_id_wins(monkeypatch, tmp_path):
    """origin_session_id (stamped at dispatch time by async_delegation) takes
    precedence as the wake target."""
    runner = _build_runner(monkeypatch, tmp_path, "all")
    api_adapter = SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AsyncMock(),
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="m",
    )
    runner.adapters[Platform.API_SERVER] = api_adapter

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append(session_id)

    import gateway.wake as wake_mod
    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    evt = {
        "session_id": "proc_watch",
        "session_key": "",
        "origin_session_id": "raw-origin-sid",
    }
    result = await runner._inject_watch_notification("[SYSTEM: done]", evt)
    assert result is True
    assert posts == ["raw-origin-sid"]
