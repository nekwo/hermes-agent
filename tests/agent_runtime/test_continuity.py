from __future__ import annotations

from types import SimpleNamespace

from agent_runtime.continuity import REF_LIMIT, REF_TEXT_LIMIT, SUMMARY_LIMIT, return_summary_to_parent_session
from agent_runtime.events import EventLog
from agent_runtime.persona_assignments import PersonaInstanceStore
from tests.agent_runtime.persona_samples import sample_personas


def _persona(persona_id: str):
    return next(persona for persona in sample_personas() if persona.id == persona_id)


def test_return_summary_posts_bounded_parent_message_and_records_lineage(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    store = PersonaInstanceStore()
    parent = store.ensure_for_goal(_persona("neko_supervisor"), goal_id="task_r3", spawned_by=None)
    child = store.ensure_for_goal(_persona("dev"), goal_id="task_r3", spawned_by=parent.id)
    summary = "R3 continuity proof " + ("x" * (SUMMARY_LIMIT + 200))
    proof_ids = [f"proof_{idx}_{'p' * 200}" for idx in range(20)]
    artifact_refs = [f"artifact://r3/{idx}/{'a' * 200}" for idx in range(20)]

    result = return_summary_to_parent_session(
        child.id,
        parent_session_id="parent_session_r3",
        summary=summary,
        proof_ids=proof_ids,
        artifact_refs=artifact_refs,
    )

    from hermes_state import SessionDB

    messages = SessionDB().get_messages("parent_session_r3")
    returned = store.get(child.id)
    events = EventLog().for_session("parent_session_r3")

    assert result["ok"] is True
    assert result["capability_id"] == "persona.instance.return_summary"
    assert result["bounded"] is True
    assert result["summary_chars"] == SUMMARY_LIMIT
    assert len(result["proof_ids"]) == REF_LIMIT
    assert all(len(item) <= REF_TEXT_LIMIT for item in result["proof_ids"])
    assert len(result["artifact_refs"]) == REF_LIMIT
    assert all(len(item) <= REF_TEXT_LIMIT for item in result["artifact_refs"])
    assert returned.returned_to == "parent_session_r3"
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert f"Continuity return from {child.id}:" in messages[0]["content"]
    assert "Proof refs:" in messages[0]["content"]
    assert "Artifact refs:" in messages[0]["content"]
    assert [event.type for event in events] == ["steer.returned"]
    # S27: the ``task_id`` column and the ``stage_id`` payload key were removed
    # with the CLI-unreachable parameters that were their only source; there is
    # no ``Task`` record left for either to name.
    assert events[0].task_id is None
    assert "stage_id" not in events[0].payload
    assert events[0].persona_id == "dev"
    assert events[0].payload["result"] == "summary_returned"
    assert events[0].payload["source_node_id"] == child.id
    assert events[0].payload["target_node_id"] == "parent_session_r3"


def test_return_summary_cli_uses_first_class_primitive(tmp_path, monkeypatch, capsys):
    """The namespace is exactly what the parser now produces.

    S22 removed the verb's ``--task``/``--stage`` flags (they wrote the deleted
    mission-record key and stage-graph payload), so a namespace carrying
    ``task_id``/``stage_id`` would no longer be reachable from the CLI. The ref
    flags stay: they render into the parent chat message.
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    child = PersonaInstanceStore().ensure_for_goal(
        _persona("dev"),
        goal_id="task_r3_cli",
        spawned_by="personainst_neko_supervisor",
    )

    from hermes_cli import harness

    code = harness._cmd_persona_instance_return_summary(
        SimpleNamespace(
            persona_instance_id=child.id,
            parent_session_id="parent_session_cli",
            summary="CLI return summary",
            proof_ids=["proof_cli"],
            artifact_refs=["artifact://cli"],
            json=True,
        )
    )

    output = capsys.readouterr().out
    assert code == 0
    assert '"capability_id": "persona.instance.return_summary"' in output
    assert '"parent_session_id": "parent_session_cli"' in output
    assert PersonaInstanceStore().get(child.id).returned_to == "parent_session_cli"
