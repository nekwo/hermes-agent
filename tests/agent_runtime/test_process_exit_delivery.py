"""Stage 3b, the JOIN — a ``process notify`` exit becomes a real delivery turn.

The producer half is pinned in ``tests/tools/test_process_exit_notify.py``. This
file proves the half that matters to the agent: the completion the process
reaper publishes is consumed by the EXISTING serve delivery drain
(``drain_background_completions``) and forged into a turn in the requesting
agent's own thread — the same road a detached ``agent_chat_send`` reply travels
back (the live specimen the plan names,
``dispatch-delivery-dispatch-23aa318aa3d4``).

A producer test and a consumer test can both pass while the seam between them
is broken — that is the lesson ``test_background_completion_attribution`` was
written from — so the event here is built by the REAL producer
(``ProcessRegistry._move_to_finished`` after a real ``notify_on_exit``), never
hand-rolled.
"""

from __future__ import annotations

import time

import pytest

from agent_runtime import dispatch_delivery
from agent_runtime.persona_chat_continuity import chat_root_session_key_scope
from tools.process_registry import ProcessRegistry, ProcessSession

ROOT = "persona_chat_personainst_chara_a2_7b31d0e4_a238c5f9c4c2"
INSTANCE = "personainst_chara_a2_7b31d0e4"
PERSONA = "chara_a2"


@pytest.fixture()
def notify_home(tmp_path, monkeypatch):
    import hermes_constants
    import tools.process_notify_store as store

    home = tmp_path / "background-home"
    home.mkdir()
    monkeypatch.setattr(
        hermes_constants, "get_hermes_background_work_home", lambda: home
    )
    store.reset_cache()
    return home


@pytest.fixture()
def owned_root(monkeypatch):
    import agent_runtime.persona_assignments as assignments

    monkeypatch.setattr(
        assignments,
        "chat_session_owner_persona",
        lambda session_id: (PERSONA, INSTANCE) if session_id == ROOT else None,
    )
    monkeypatch.setattr(
        dispatch_delivery,
        "_sender_persona",
        lambda session_id: (PERSONA, INSTANCE) if session_id == ROOT else None,
    )


def _exited_notify_event(registry: ProcessRegistry) -> dict:
    session = ProcessSession(
        id="proc_rows",
        command="hermes harness characters rows --draft d1 --json",
        session_key=ROOT,
        started_at=time.time(),
        output_buffer="row 10/10 ok\n",
    )
    registry._running[session.id] = session
    with chat_root_session_key_scope(ROOT):
        armed = registry.notify_on_exit(session.id)
    assert armed["status"] == "armed"
    session.exited = True
    session.exit_code = 0
    registry._move_to_finished(session)
    return registry.completion_queue.get_nowait()


def test_the_exit_is_forged_into_a_turn_in_the_requesting_thread(
    notify_home, owned_root, monkeypatch
):
    registry = ProcessRegistry()
    event = _exited_notify_event(registry)

    import tools.process_registry as registry_mod

    monkeypatch.setattr(registry_mod, "process_registry", registry)
    monkeypatch.setattr(dispatch_delivery, "_sender_is_idle", lambda root: True)
    registry.completion_queue.put(event)

    forged: list[dict] = []

    def _forge(**kwargs):
        forged.append(kwargs)
        return True, {"ok": True, "reply": "thanks"}

    tally = dispatch_delivery.drain_background_completions(forge=_forge)

    assert tally["delivered"] == 1
    assert len(forged) == 1
    call = forged[0]
    assert call["root_session_id"] == ROOT
    assert call["persona_instance_id"] == INSTANCE
    assert call["persona_id"] == PERSONA
    # The delivered message is the receipt, and it says why it arrived.
    assert "BACKGROUND PROCESS COMPLETE" in call["message"]
    assert "process notify" in call["message"]
    assert "rows --draft d1" in call["message"]


def test_a_busy_thread_requeues_rather_than_splicing_the_receipt_mid_turn(
    notify_home, owned_root, monkeypatch
):
    """Inherited invariant: a delivery never lands in a thread mid-turn."""

    registry = ProcessRegistry()
    event = _exited_notify_event(registry)

    import tools.process_registry as registry_mod

    monkeypatch.setattr(registry_mod, "process_registry", registry)
    monkeypatch.setattr(dispatch_delivery, "_sender_is_idle", lambda root: False)
    registry.completion_queue.put(event)

    tally = dispatch_delivery.drain_background_completions(
        forge=lambda **kwargs: pytest.fail("a busy thread must not be forged into")
    )

    assert tally["delivered"] == 0
    assert tally["requeued"] == 1
    assert registry.completion_queue.qsize() == 1
