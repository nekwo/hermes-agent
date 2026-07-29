from datetime import timedelta

from hermes_time import now

from agent_runtime.models import AgentPersona, AgentRun, Event, Incident, Proof, Task
from agent_runtime.models import ProofType
from agent_runtime.serde import from_jsonable, to_jsonable
from agent_runtime.states import RunState, TaskState


def test_task_model_round_trips_through_strict_jsonable_shape():
    ts = now()
    task = Task(
        id="task_abc",
        title="Ship tiny slice",
        description="Do the first reliable thing",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        requires_visual_proof=True,
        acceptance_criteria=["tests pass"],
    )

    raw = to_jsonable(task)

    assert raw["state"] == "created"
    assert raw["created_at"].endswith("Z") or "+" in raw["created_at"]
    assert "stages" not in raw
    assert from_jsonable(Task, raw) == task


def test_all_stage_one_models_round_trip():
    ts = now()
    models = [
        AgentPersona(
            id="pm",
            display_name="PM",
            role="pm",
            model=None,
            provider=None,
            api_mode=None,
            toolsets=["file"],
            system_prompt_path="personas/pm/system.md",
        ),
        AgentRun(
            id="run_1",
            persona_id="pm",
            task_id="task_abc",
            stage_id=None,
            state=RunState.RUNNING,
            started_at=ts,
            last_heartbeat_at=ts + timedelta(seconds=1),
        ),
        Proof(
            id="proof_1",
            task_id="task_abc",
            stage_id=None,
            type=ProofType.TEST_RUN,
            title="pytest",
            path_or_value="proofs/task_abc/test-runs/proof_1.txt",
            created_by="harness",
            created_at=ts,
        ),
        Event(ts=ts, type="task.created", task_id="task_abc", run_id=None, persona_id=None),
        Incident(
            id="inc_1",
            task_id="task_abc",
            run_id="run_1",
            kind="tool_failure",
            summary="command failed",
            detail_path="incidents/inc_1.txt",
            opened_at=ts,
        ),
    ]

    for model in models:
        assert from_jsonable(type(model), to_jsonable(model)) == model


def test_agent_persona_legacy_json_defaults_new_stage9_fields():
    raw = {
        "id": "qa",
        "display_name": "QA Agent",
        "role": "qa",
        "model": None,
        "provider": None,
        "api_mode": "codex_responses",
        "toolsets": ["file"],
        "system_prompt_path": "personas/qa/system.md",
        "autonomy": "autonomous",
        "schema_version": 1,
    }

    persona = from_jsonable(AgentPersona, raw)

    assert persona.hermes_profile is None
    assert persona.skills == []
    assert persona.soul_overlay_path is None
    assert persona.required_mcp_servers == []
    assert persona.include_core_context_files is False


def test_task_legacy_json_drops_retired_stage_graph_fields():
    ts = now()
    raw = {
        "id": "task_legacy",
        "title": "Legacy task",
        "description": "Legacy task description",
        "state": "created",
        "created_at": ts.isoformat(),
        "updated_at": ts.isoformat(),
        "requested_by": "tony",
        "mission_plan": {"enabled": True, "stages": [{"id": "old"}]},
        "stages": [{"id": "old"}],
        "schema_version": 1,
    }

    task = from_jsonable(Task, raw)

    assert not hasattr(task, "mission_plan")
    assert not hasattr(task, "stages")


def test_task_legacy_prose_risk_flags_migrate_to_operator_notes():
    ts = now()
    raw = {
        "id": "task_legacy_flags",
        "title": "Legacy flags",
        "description": "Legacy task description",
        "state": "created",
        "created_at": ts.isoformat(),
        "updated_at": ts.isoformat(),
        "requested_by": "tony",
        "risk_flags": [
            "cross_stack_contract_handoff",
            "This is a delivery metadata repair only; use existing passed proof proof_existing_passed.",
            "priority:high",
        ],
        "schema_version": 1,
    }

    task = from_jsonable(Task, raw)

    assert task.risk_flags == ["cross_stack_contract_handoff", "priority:high"]
    assert task.operator_notes == [
        "migrated legacy risk_flag: This is a delivery metadata repair only; use existing passed proof proof_existing_passed."
    ]


def test_task_without_stage_graph_round_trips():
    ts = now()
    task = Task(
        id="task_typed",
        title="Fix Mission Control",
        description="Fix all role streams",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )

    assert from_jsonable(Task, to_jsonable(task)) == task
