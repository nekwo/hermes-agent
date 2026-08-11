"""The mission-chat background-work attribution seam, both halves AND the join.

A ``terminal(background=True, notify_on_complete=True)`` completion is
LEDGERLESS: delivery depends entirely on the completion event naming a session
that resolves to a persona chat root
(``dispatch_delivery._chat_root_of_completion`` — positive proof only, the
#64484 no-guessing rule). The spawn side therefore has to WRITE that key.
Nothing did on the mission-chat lane, so a live 2026-08-11 test task
(``proc_b0593bc9fb0e``) spawned with ``session_key: ""``: its completion was
re-queued by every 5s drain pass forever, evaporated with its output tail on
the next serve restart, and its ``running_work`` row rendered under "NO OWNING
AGENT" — exactly as the ``1c0e95bc3`` commit message predicted.

The fix is ``chat_root_session_key_scope``: the persona run binds its root chat
session id into ``tools.approval``'s session-key ContextVar (upstream's own
public setter — no upstream file is edited), so the terminal tool's existing
``get_current_session_key(default="") or task_id`` resolution stamps a
resolvable root at spawn time. Positive knowledge at spawn, not drain-time
guessing.

The join matters more than the halves: a producer test and a consumer test can
both pass while the seam stays broken, so the drain-side test here consumes an
event built by the REAL producer (``ProcessRegistry._move_to_finished``) keyed
exactly the way the spawn-side test proves the terminal tool stamps it.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_runtime import dispatch_delivery
from agent_runtime.persona_chat_continuity import chat_root_session_key_scope

SENDER_ROOT = "persona_chat_personainst_neko_aaaaaaaaaaaa"


# ── the scope itself ────────────────────────────────────────────────────────


def test_scope_binds_only_persona_chat_roots():
    """Only ids that name a persona chat root enter upstream's key surface.

    A gateway key, a bare task id, or an empty id must leave the resolution
    exactly as it was — this scope exists to make attribution resolvable, not
    to widen what upstream's session key means.
    """

    from tools.approval import get_current_session_key

    assert get_current_session_key(default="") == ""
    with chat_root_session_key_scope("session:telegram-123"):
        assert get_current_session_key(default="") == ""
    with chat_root_session_key_scope(None):
        assert get_current_session_key(default="") == ""
    with chat_root_session_key_scope(SENDER_ROOT):
        assert get_current_session_key(default="") == SENDER_ROOT
    assert get_current_session_key(default="") == ""


# ── spawn half: the terminal tool stamps the bound root ─────────────────────


def _minimal_terminal_config(cwd="/default"):
    return {
        "env_type": "local",
        "cwd": cwd,
        "timeout": 60,
        "lifetime_seconds": 3600,
    }


def _background_spawn_session_key(monkeypatch, task_id: str) -> str:
    """Run a REAL ``terminal_tool(background=True)`` dispatch against a fake
    registry and return the ``session_key`` it was spawned with."""

    import tools.process_registry as process_registry_mod
    import tools.terminal_tool as terminal_tool

    class FakeEnv:
        env = {}
        cwd = "/workspace"

    class FakeRegistry:
        def __init__(self):
            self.calls = []
            self.pending_watchers = []

        def spawn_local(self, **kwargs):
            self.calls.append(kwargs)
            # The notify wiring after the spawn reads/writes these fields on
            # the returned session; a bare namespace would AttributeError and
            # convert the spawn into an error result.
            return SimpleNamespace(
                id="proc_attribution_probe",
                pid=4321,
                watcher_platform="",
                notify_on_complete=False,
            )

    registry = FakeRegistry()
    monkeypatch.setattr(terminal_tool, "_active_environments", {task_id: FakeEnv()})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool, "_get_env_config", lambda: _minimal_terminal_config()
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(
        terminal_tool, "_resolve_container_task_id", lambda value: value or "default"
    )
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )
    monkeypatch.setattr(process_registry_mod, "process_registry", registry)

    result = json.loads(
        terminal_tool.terminal_tool(
            command="sleep 1",
            task_id=task_id,
            background=True,
            notify_on_complete=True,
        )
    )
    assert result["exit_code"] == 0
    assert len(registry.calls) == 1
    return registry.calls[0]["session_key"]


def test_terminal_background_spawn_carries_the_bound_chat_root(monkeypatch):
    """Inside a persona run the spawn is keyed by the chat root, so the
    completion event it will produce names a session the drain can resolve."""

    with chat_root_session_key_scope(SENDER_ROOT):
        assert _background_spawn_session_key(monkeypatch, "task-under-chat") == SENDER_ROOT


def test_terminal_background_spawn_outside_a_persona_run_is_unchanged(monkeypatch):
    """No scope bound → the historical task-id fallback, byte for byte."""

    assert _background_spawn_session_key(monkeypatch, "task-plain") == "task-plain"


# ── delivery half + join: the REAL producer's event resolves and delivers ───


class _Forge:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.ok, {"ok": self.ok}


@pytest.fixture
def resolvable_sender(monkeypatch):
    monkeypatch.setattr(
        dispatch_delivery,
        "_sender_persona",
        lambda root: ("neko_supervisor", "personainst_neko")
        if root == SENDER_ROOT
        else None,
    )


@pytest.fixture
def idle_sender(monkeypatch):
    monkeypatch.setattr(dispatch_delivery, "_sender_is_idle", lambda root: True)


def _real_completion_event(session_key: str) -> dict:
    """Produce the completion event with the REAL producer.

    ``ProcessRegistry._move_to_finished`` is the one place these events are
    built (ANSI-stripped 2000-char tail, real exit code, ``session_key``
    verbatim from the session). Building the dict by hand here would pin this
    suite to a copy free to drift from the producer — the exact idiom class
    the board-card wire regression came from.
    """

    from tools.process_registry import ProcessRegistry, ProcessSession

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_join_specimen",
        command="echo 'Background notification test'",
        task_id="",
        session_key=session_key,
        started_at=time.time(),
        exited=True,
        exit_code=0,
        output_buffer="Background notification test completed after 20 seconds",
        notify_on_complete=True,
    )
    session.completion_reason = "exited"
    registry._running[session.id] = session
    with patch.object(registry, "_write_checkpoint"):
        registry._move_to_finished(session)
    assert registry.completion_queue.qsize() == 1
    return registry.completion_queue.get_nowait()


def _own_global_queue():
    """Take ownership of the process-global completion queue."""

    from tools.process_registry import process_registry

    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    return process_registry


def test_a_chat_rooted_terminal_completion_is_delivered_into_the_root(
    resolvable_sender, idle_sender
):
    """The join: producer-built event, keyed the way the spawn half stamps it,
    resolved and forged into the sender's chat root by the real drain."""

    registry = _own_global_queue()
    evt = _real_completion_event(SENDER_ROOT)
    registry.completion_queue.put(evt)
    dispatch_delivery._background_attempts.clear()

    forge = _Forge(ok=True)
    tally = dispatch_delivery.drain_background_completions(forge=forge)

    assert tally["delivered"] == 1
    assert len(forge.calls) == 1
    call = forge.calls[0]
    assert call["root_session_id"] == SENDER_ROOT
    assert call["persona_id"] == "neko_supervisor"
    assert call["persona_instance_id"] == "personainst_neko"
    # The output tail must survive into the delivered turn: it exists nowhere
    # durable for a terminal completion, so dropping it here loses it forever.
    assert "Background notification test completed after 20 seconds" in call["message"]
    assert registry.completion_queue.empty()


def test_an_unkeyed_terminal_completion_is_requeued_never_forged(
    resolvable_sender, idle_sender
):
    """A completion with no resolvable session is never forged anywhere.

    An unkeyed event (``session_key: ""``) rides ``drain_notifications``'s
    ownerless-legacy branch, so the drain does pick it up — but
    ``_chat_root_of_completion`` resolves nothing and the event goes straight
    back on the queue, untouched, for a consumer that can own it. This is the
    pre-fix posture (the live 2026-08-11 event lived exactly this loop), and
    the honest one: the drain must never guess a chat root (#64484)."""

    registry = _own_global_queue()
    evt = _real_completion_event("")
    registry.completion_queue.put(evt)
    dispatch_delivery._background_attempts.clear()

    forge = _Forge(ok=True)
    try:
        tally = dispatch_delivery.drain_background_completions(forge=forge)

        assert tally["delivered"] == 0
        assert tally["requeued"] == 1
        assert forge.calls == []
        assert registry.completion_queue.qsize() == 1
    finally:
        _own_global_queue()  # leave the singleton clean for other suites
