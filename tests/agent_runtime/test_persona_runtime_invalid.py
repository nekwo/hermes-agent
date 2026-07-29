import json
from pathlib import Path

import pytest

from hermes_time import now

from agent_runtime.context_builder import build_context
from agent_runtime.decision_schema import DecisionPayloadInvalid
from agent_runtime.models import AgentPersona, AgentRun, Task
from agent_runtime.persona_runtime import GPTPersonaRuntime
from agent_runtime.personas import AgentRole, default_personas
from agent_runtime.states import RunState, TaskState


class RepairThenBadAIAgent:
    instances = []

    def __init__(self, **kwargs):
        self.calls = []
        RepairThenBadAIAgent.instances.append(self)

    def run_conversation(self, *, user_message, system_message, task_id):
        self.calls.append({"user_message": user_message, "system_message": system_message, "task_id": task_id})
        if len(RepairThenBadAIAgent.instances) == 1:
            return {"final_response": "not json"}
        return {"final_response": json.dumps({"type": "complete", "summary": "missing fields"})}


class PMPatchAIAgent:
    def __init__(self, **kwargs):
        pass

    def run_conversation(self, *, user_message, system_message, task_id):
        return {
            "final_response": json.dumps(
                {
                    "type": "propose_patch",
                    "summary": "PM tries to patch",
                    "rationale": "bad role behavior",
                    "payload": {"patch": "..."},
                    "requires_approval": False,
                    "schema_version": 1,
                }
            )
        }


class RepairInvalidPayloadAIAgent:
    instances = []

    def __init__(self, **kwargs):
        self.calls = []
        RepairInvalidPayloadAIAgent.instances.append(self)

    def run_conversation(self, *, user_message, system_message, task_id):
        self.calls.append({"user_message": user_message, "system_message": system_message, "task_id": task_id})
        if len(RepairInvalidPayloadAIAgent.instances) == 1:
            return {
                "final_response": json.dumps(
                    {
                        "type": "request_file_reads",
                        "summary": "Need files",
                        "rationale": "Need file context before planning.",
                        "payload": {"reason": "Need source context."},
                        "requires_approval": False,
                        "schema_version": 1,
                    }
                )
            }
        return {
            "final_response": json.dumps(
                {
                    "type": "request_file_reads",
                    "summary": "Need files",
                    "rationale": "Need file context before planning.",
                    "payload": {"reason": "Need source context.", "paths": ["lib/main.dart"]},
                    "requires_approval": False,
                    "schema_version": 1,
                }
            )
        }


def make_task_run(persona_id="pm"):
    ts = now()
    task = Task(
        id="task_abc",
        title="Build harness",
        description="Make agent runtime reliable",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )
    run = AgentRun(
        id="run_abc",
        persona_id=persona_id,
        task_id=task.id,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )
    return task, run


def explicit_pm_persona():
    return AgentPersona(
        id="pm",
        display_name="PM",
        role=AgentRole.PM.value,
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "todo"],
        system_prompt_path="personas/pm/system.md",
    )


def test_bad_json_gets_one_bounded_repair_attempt_then_raises():
    RepairThenBadAIAgent.instances.clear()
    task, run = make_task_run()
    pm = explicit_pm_persona()
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=RepairThenBadAIAgent)

    with pytest.raises(DecisionPayloadInvalid):
        runtime.run_tick(pm, build_context(task, run), run=run)

    assert len(RepairThenBadAIAgent.instances) == 2
    assert "Previous decision failed validation" in RepairThenBadAIAgent.instances[1].calls[0]["user_message"]


def test_decision_type_not_allowed_for_role_raises():
    task, run = make_task_run()
    pm = explicit_pm_persona()
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=PMPatchAIAgent)

    with pytest.raises(DecisionPayloadInvalid):
        runtime.run_tick(pm, build_context(task, run), run=run)


def test_contract_invalid_payload_gets_one_bounded_repair_attempt_then_succeeds(
    bundled_persona_profiles,
):
    # Unlike the two tests above (which build an explicit profile-less PM), this
    # one drives a BUNDLED persona, and since 9ad9c8017 a bundled persona's
    # Hermes profile must exist before `_invoke_agent` will run it at all.
    RepairInvalidPayloadAIAgent.instances.clear()
    task, run = make_task_run(persona_id="dev")
    task.affected_repos = [str(Path.cwd())]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=RepairInvalidPayloadAIAgent)

    decision = runtime.run_tick(dev, build_context(task, run), run=run)

    assert decision.payload["paths"] == ["lib/main.dart"]
    assert len(RepairInvalidPayloadAIAgent.instances) == 2
    assert "Previous decision failed validation" in RepairInvalidPayloadAIAgent.instances[1].calls[0]["user_message"]
