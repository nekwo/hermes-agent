"""A freshly created agent's FIRST message, on its own pointer.

The operator gesture, end to end: drag a QA agent into the Mission Control
office (``runtime.agent.create`` — the RPC lane the launcher actually uses),
then send it a message on the ``default_chat_session_id`` that create handed
back. That is one gesture to a human and two lanes to this program, and on
2026-08-20 the seam between them was where every such agent died:

    CALL HARNESS CAPABILITY FAILED IN AGENT RUNTIME HARNESS. UNKNOWN EXPLICIT
    PERSONA CHAT ROOT: persona_chat_personainst_qa_agent_03ba2049_67d5a1a6921f

The refusal was correct — that guard exists to refuse client-fabricated roots,
and the root genuinely named no SessionDB row. What was wrong is that the
create lane minted the pointer without ever making the row, because the
durability step lived in a CLI command part that ``agent_runtime`` cannot
import. Neither half of the gesture is wrong on its own; only the join is, so
only a test that spans the join can see it.

The turn is never allowed to reach a provider — a stub runtime raises the
moment it is asked — because the claim under test is entirely about
ADMISSION: the guard at the top of ``_cmd_mission_chat_message`` must not
refuse an agent its own freshly minted chat pointer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_runtime.mission_chat_outcome import ChatErrorKind
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_created_agent_first_message"


@pytest.fixture
def isolate_agent_runtime_root(tmp_path, monkeypatch):
    """Local twin of the ``tests/agent_runtime`` conftest fixture.

    This file drives real store writes (persona rows, the office surface, the
    turn journal) so it needs a throwaway runtime root, the same way the
    ``agent_runtime`` suite does.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    yield root


@pytest.fixture
def qa_persona(isolate_agent_runtime_root):
    """A roster persona complete enough to be ASKED to answer.

    Model/provider are filled in, unlike the create suite's fixture, so the
    lane's admission path runs to the point where a provider would be spawned
    rather than refusing for a reason that has nothing to do with this bug.
    """

    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


class _ProviderReached(RuntimeError):
    """Raised by the stub runtime: admission got all the way to the model."""


@pytest.fixture
def harness_with_stub_provider(monkeypatch):
    from agent_runtime.config import AgentRuntimeConfig
    from hermes_cli import harness

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, *args, **kwargs):
            raise _ProviderReached("admission reached the provider")

    monkeypatch.setattr(
        harness, "load_agent_runtime_config", lambda: AgentRuntimeConfig()
    )
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _Provider)
    # Deliberately NOT patching ``_default_persona_session_db``: the guard under
    # test only fires against CANONICAL persistence, so a stub store would make
    # every row here pass vacuously. The real per-test SessionDB is the point.
    return harness


def _drag_in_an_agent(placement_id: str) -> dict:
    """Create through ``runtime.agent.create`` — the launcher's own lane."""

    from agent_runtime import serve_rpc
    from agent_runtime.office_store import OfficeStore

    seed_workspace_record(WORKSPACE)
    OfficeStore().ensure_surface(WORKSPACE, created_by="seed")

    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "drag-1",
            "method": "runtime.agent.create",
            "params": {
                "persona_id": "qa",
                "workspace_id": WORKSPACE,
                "position": [3.5, -1.25],
                "idempotency_key": f"gesture-{placement_id}",
                "placement_id": placement_id,
            },
        }
    )
    assert "result" in reply, reply
    return reply["result"]


def _message_args(*, instance_id: str, session_id: str):
    return SimpleNamespace(
        persona_id="qa",
        persona_instance_id=instance_id,
        session_id=session_id,
        message="hello, are you there?",
        surface_prompt="",
        intent_hint="chat",
        requested_by="operator",
        client_message_id="first-message",
        stream=False,
        max_seconds=5.0,
        json=True,
    )


def test_a_dragged_in_agent_is_not_refused_its_own_chat_pointer(
    qa_persona, harness_with_stub_provider, capsys
):
    harness = harness_with_stub_provider
    created = _drag_in_an_agent("qa_agent_first_message")
    root = created["default_chat_session_id"]

    harness._cmd_mission_chat_message(
        _message_args(
            instance_id=created["persona_instance_id"], session_id=root
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload.get("error_kind") != ChatErrorKind.UNKNOWN_CHAT_SESSION, (
        "the agent this gesture just created was refused its own "
        f"default_chat_session_id as a phantom root: {root}"
    )
    assert payload.get("error_kind") != ChatErrorKind.FOREIGN_CHAT_SESSION, (
        f"the created root {root} is not recognised as owned by "
        f"{created['persona_instance_id']}"
    )
    # Anti-vacuity: two ``!=`` assertions are satisfied by ANY other refusal,
    # including one raised long before the guard under test. The stub runtime's
    # own words are the witness that admission ran to completion and the turn
    # was handed to a model.
    assert "admission reached the provider" in str(payload.get("blocker") or ""), (
        "this row never reached the chat-root guard it claims to be about; "
        f"the lane stopped earlier with {payload.get('error_kind')!r}"
    )
    assert payload["session_id"] == root


def test_the_created_root_is_the_one_the_turn_actually_threads_onto(
    qa_persona, harness_with_stub_provider, capsys
):
    """...and the SessionDB row behind it is the operator-visible one.

    Separated from the guard row above because it fails for a different reason:
    the pointer could resolve while the turn threaded somewhere else, which is
    the split-transcript shape (a pointer in the shared runtime root naming a
    root minted in a profile-local ``state.db``) that ``default_persona_session_db``
    exists to prevent.
    """

    from agent_runtime.persona_chat_durability import default_persona_session_db

    harness = harness_with_stub_provider
    created = _drag_in_an_agent("qa_agent_threaded")
    root = created["default_chat_session_id"]

    harness._cmd_mission_chat_message(
        _message_args(
            instance_id=created["persona_instance_id"], session_id=root
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["root_chat_session_id"] == root

    session_db = default_persona_session_db()
    try:
        row = session_db.get_session(root)
    finally:
        session_db.close()
    assert row is not None
    assert row["title"] == "QA Agent chat"
