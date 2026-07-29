from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.blueprints import BlueprintStore, instantiate_blueprint
from agent_runtime.blueprints.routing import apply_decision_outcome, apply_stage_outcome, derive_stage_outcome, next_target
from agent_runtime.blueprints.runs import BlueprintRunStore
from agent_runtime.blueprints.schema import StageOutcome, blueprint_from_dict, validate_bindings, validate_blueprint
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.mission_plan import mission_plan_summary, validate_mission_plan
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Proof, Task
from agent_runtime.models import ProofType
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import StageStatus, TaskState


def test_schema_rejects_undeclared_owner_slot():
    bp = blueprint_from_dict(
        {
            "id": "bad_owner",
            "version": 1,
            "title": "Bad Owner",
            "slots": [{"id": "builder", "role": "builder"}],
            "stages": [{"id": "build", "title": "Build", "objective": "Build", "owner_slot": "ghost"}],
            "edges": [{"source": "build", "outcome": "passed", "target": "done"}],
            "limits": {"max_attempts_per_stage": 1, "max_total_stages": 1},
        }
    ) if False else None

    raw = {
        "id": "bad_owner",
        "version": 1,
        "title": "Bad Owner",
        "slots": [{"id": "builder", "role": "builder"}],
        "stages": [{"id": "build", "title": "Build", "objective": "Build", "owner_slot": "ghost"}],
        "edges": [{"source": "build", "outcome": "passed", "target": "done"}],
        "limits": {"max_attempts_per_stage": 1, "max_total_stages": 1},
    }
    with pytest.raises(ValueError, match="owner_slot 'ghost' is not declared"):
        blueprint_from_dict(raw)


def test_schema_rejects_unprefixed_bindings():
    bp = BlueprintStore().get("one_agent_smoke")

    assert validate_bindings(bp, {"builder": "gpt-launcher"}) == [
        "binding for slot builder must start with persona: or profile:"
    ]


def test_schema_rejects_unknown_agent_topology_slot():
    raw = {
        "id": "bad_agent_topology",
        "version": 1,
        "title": "Bad Agent Topology",
        "slots": [{"id": "builder", "role": "builder"}],
        "stages": [{"id": "build", "title": "Build", "objective": "Build", "owner_slot": "builder"}],
        "edges": [{"source": "build", "outcome": "passed", "target": "done"}],
        "agent_topology": {"root": "lead", "edges": [{"source": "lead", "target": "builder"}]},
    }

    with pytest.raises(ValueError, match="agent_topology root 'lead' is not a declared slot"):
        blueprint_from_dict(raw)


def test_schema_rejects_agent_topology_cycle():
    raw = {
        "id": "cyclic_agent_topology",
        "version": 1,
        "title": "Cyclic Agent Topology",
        "slots": [
            {"id": "lead", "role": "neko"},
            {"id": "builder", "role": "builder"},
        ],
        "stages": [
            {"id": "scope", "title": "Scope", "objective": "Scope", "owner_slot": "lead"},
            {"id": "build", "title": "Build", "objective": "Build", "owner_slot": "builder"},
        ],
        "edges": [{"source": "scope", "outcome": "ready", "target": "build"}],
        "agent_topology": {
            "root": "lead",
            "edges": [
                {"source": "lead", "target": "builder"},
                {"source": "builder", "target": "lead"},
            ],
        },
    }

    with pytest.raises(ValueError, match="agent_topology cycle"):
        blueprint_from_dict(raw)


def test_blueprint_agent_topology_instantiates_to_mission_plan_summary():
    bp = BlueprintStore().get("neko_two_dev_default")
    plan = instantiate_blueprint(
        bp,
        goal="topology smoke",
        bindings={
            "lead": "persona:neko_supervisor",
            "backend_builder": "persona:backend_dev",
            "builder": "persona:dev",
        },
    )
    task = Task(
        id="task_topology_smoke",
        title="Topology Smoke",
        description="topology smoke",
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        mission_plan=plan,
    )

    summary = mission_plan_summary(task)
    # Fan-out: neko (lead) coordinates BOTH dev branches directly — no chain.
    assert summary["agent_topology"] == {
        "root": "lead",
        "edges": [
            {"source": "lead", "target": "backend_builder", "kind": "steers"},
            {"source": "lead", "target": "builder", "kind": "steers"},
        ],
    }


def test_blueprint_output_type_code_feature_materializes_test_run_gate():
    bp = blueprint_from_dict(
        {
            "id": "code_output",
            "version": 1,
            "title": "Code Output",
            "slots": [{"id": "builder", "role": "builder"}],
            "stages": [
                {
                    "id": "build",
                    "title": "Build",
                    "objective": "Build the feature.",
                    "owner_slot": "builder",
                    "output_type": "code feature",
                }
            ],
            "edges": [{"source": "build", "outcome": "passed", "target": "done"}],
        }
    )
    plan = instantiate_blueprint(bp, goal="ship code", bindings={"builder": "persona:dev"})

    assert plan.stages[0].output_type == "code feature"
    assert plan.stages[0].proof_gate["required_proof_types"] == ["test_run"]
    assert plan.stages[0].proof_gate["required"] is True


def test_blueprint_output_type_design_document_materializes_artifact_gate():
    bp = blueprint_from_dict(
        {
            "id": "design_output",
            "version": 1,
            "title": "Design Output",
            "slots": [{"id": "builder", "role": "builder"}],
            "stages": [
                {
                    "id": "design",
                    "title": "Design",
                    "objective": "Write the design.",
                    "owner_slot": "builder",
                    "output_type": "design document",
                }
            ],
            "edges": [{"source": "design", "outcome": "passed", "target": "done"}],
        }
    )
    plan = instantiate_blueprint(bp, goal="ship docs", bindings={"builder": "persona:dev"})

    assert plan.stages[0].output_type == "design document"
    assert plan.stages[0].proof_gate["required_proof_types"] == ["artifact"]
    assert "test_run" not in plan.stages[0].proof_gate["required_proof_types"]


def test_blueprint_output_type_infers_from_existing_proof_gate():
    bp = blueprint_from_dict(
        {
            "id": "inferred_output",
            "version": 1,
            "title": "Inferred Output",
            "slots": [{"id": "builder", "role": "builder"}],
            "stages": [
                {
                    "id": "build",
                    "title": "Build",
                    "objective": "Build the feature.",
                    "owner_slot": "builder",
                    "proof_gate": {"required": True, "minimum_status": "passed", "required_proof_types": ["test_run"]},
                }
            ],
            "edges": [{"source": "build", "outcome": "passed", "target": "done"}],
        }
    )

    assert bp.stages[0].output_type == "code feature"


def test_legacy_plan_without_owner_slot_normalizes_to_owner():
    plan = MissionPlan(
        mission_intent=MissionIntent(title="Legacy", objective="Legacy"),
        stages=[
            MissionPlanStage(
                id="legacy_dev",
                title="Legacy Dev",
                objective="Do it",
                owner="dev",
                repo="hermes-agent",
                kind="implementation",
            )
        ],
        current_stage_id="legacy_dev",
    )
    task = Task(
        id="task_legacy",
        title="Legacy",
        description="Legacy",
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        mission_plan=plan,
    )

    assert validate_mission_plan(plan) == []
    assert mission_plan_summary(task)["stages"][0]["owner_slot"] == "dev"


def test_two_agent_blueprint_validates_and_instantiates_with_swapped_bindings():
    bp = BlueprintStore().get("two_agent_build_verify")

    assert validate_blueprint(bp) == []
    plan = instantiate_blueprint(
        bp,
        goal="swap smoke",
        bindings={"builder": "profile:gpt-launcher", "verifier": "persona:qa"},
    )

    assert validate_mission_plan(plan) == []
    assert [stage.owner_slot for stage in plan.stages] == ["builder", "verifier"]
    assert plan.bindings == {"builder": "gpt-launcher", "verifier": "qa"}
    assert any(edge["source"] == "verify" and edge["target"] == "implement" for edge in plan.edges)


def test_blueprint_terminal_run_writes_versioned_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    first = _blueprint_task("two_agent_build_verify")
    first.id = "task_blueprint_record_first"

    apply_stage_outcome(first, "implement", StageOutcome.PASSED, reason="implemented")
    assert apply_stage_outcome(first, "verify", StageOutcome.PASSED, reason="APPROVED") == "done"

    second = _blueprint_task("two_agent_build_verify", bindings={"builder": "persona:backend_dev", "verifier": "persona:qa"})
    second.id = "task_blueprint_record_second"
    second.mission_plan.blueprint_version = 2
    apply_stage_outcome(second, "implement", StageOutcome.PASSED, reason="implemented")
    assert apply_stage_outcome(second, "verify", StageOutcome.PASSED, reason="APPROVED") == "done"

    records = BlueprintRunStore().list_all()
    assert [record.task_id for record in records] == ["task_blueprint_record_first", "task_blueprint_record_second"]
    assert [record.blueprint_version for record in records] == [1, 2]
    assert records[0].bindings == {"builder": "dev", "verifier": "qa"}
    assert records[1].bindings == {"builder": "backend_dev", "verifier": "qa"}
    assert records[0].per_stage_outcomes == {"implement": "passed", "verify": "passed"}


def test_request_test_run_proofs_drive_blueprint_outcome():
    task = _blueprint_task("one_agent_smoke", bindings={"builder": "persona:dev"})
    stage = task.mission_plan.stages[0]
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="proof",
        rationale="collect proof",
        payload={"stage_id": "build", "commands": ["python -c pass"]},
    )
    proof = Proof(
        id="proof_build",
        task_id=task.id,
        stage_id="build",
        type=ProofType.TEST_RUN,
        title="passed",
        path_or_value="proof.log",
        created_by="dev",
        created_at=now(),
        metadata={"status": "passed", "exit_code": 0},
        redaction_status="safe",
    )

    assert derive_stage_outcome(decision, stage, proofs=[proof]) == StageOutcome.PASSED
    assert apply_decision_outcome(task, decision, proofs=[proof]) == "done"


def test_resolver_finds_existing_persona_for_profile_binding():
    from agent_runtime.blueprints.resolve import BindingResolver
    from agent_runtime.models import AgentPersona

    wrapper = AgentPersona(
        id="launcher_dev",
        display_name="Launcher Dev",
        role="dev",
        model="m",
        provider="p",
        api_mode="a",
        toolsets=[],
        system_prompt_path="personas/dev/system.md",
        hermes_profile="gpt-launcher",
    )
    resolver = BindingResolver(
        configured=[wrapper],
        profile_exists=lambda name: name == "gpt-launcher",
    )
    # profile binding resolves to the persona that already wraps the profile
    assert resolver.resolve("profile:gpt-launcher", slot_role="builder") == "launcher_dev"
    # direct persona binding resolves to itself
    assert resolver.resolve("persona:launcher_dev", slot_role="builder") == "launcher_dev"


def test_resolver_promotes_unwrapped_profile_into_persisted_persona(tmp_path, monkeypatch):
    from agent_runtime.blueprints.resolve import BindingResolver
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = AgentStore()
    dev_template = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model="m",
        provider="p",
        api_mode="a",
        toolsets=[],
        system_prompt_path="personas/dev/system.md",
    )
    resolver = BindingResolver(
        agent_store=store,
        configured=[dev_template],
        profile_exists=lambda name: name == "fresh-profile",
    )

    persona_id = resolver.resolve("profile:fresh-profile", slot_role="builder")
    promoted = store.get(persona_id)
    assert promoted.hermes_profile == "fresh-profile"
    assert promoted.role == "dev"
    # find-only mode refuses to promote
    find_only = BindingResolver(
        agent_store=AgentStore(),
        configured=[dev_template],
        profile_exists=lambda name: name == "other-profile",
        allow_promote=False,
    )
    with pytest.raises(ValueError, match="has no persona"):
        find_only.resolve("profile:other-profile", slot_role="builder")


def test_save_blueprint_round_trips_and_rejects_invalid(tmp_path):
    from agent_runtime.blueprints.schema import blueprint_from_dict
    from agent_runtime.blueprints.store import BlueprintStore, blueprint_to_dict, save_blueprint

    spec = {
        "id": "edit_smoke",
        "version": 2,
        "title": "Edit Smoke",
        "slots": [{"id": "builder", "role": "builder"}],
        "stages": [{"id": "build", "title": "Build", "objective": "Build", "owner_slot": "builder"}],
        "edges": [
            {"source": "build", "outcome": "passed", "target": "done"},
            {"source": "build", "outcome": "blocked", "target": "intervention"},
        ],
        "limits": {"max_attempts_per_stage": 1, "max_total_stages": 2},
    }
    bp = blueprint_from_dict(spec)
    path = save_blueprint(bp, root=tmp_path)
    assert path.exists()

    # reloaded from disk by the store and round-trips to the same canonical dict
    reloaded = BlueprintStore(roots=[tmp_path]).get("edit_smoke")
    assert reloaded.version == 2
    assert blueprint_to_dict(reloaded) == blueprint_to_dict(bp)

    # an invalid spec (edge to unknown stage) is rejected before any write
    bad = dict(spec, edges=[{"source": "build", "outcome": "passed", "target": "ghost"}])
    with pytest.raises(ValueError, match="not a known stage or terminal target"):
        blueprint_from_dict(bad)


def test_resolver_rejects_unknown_persona_binding():
    from agent_runtime.blueprints.resolve import BindingResolver

    resolver = BindingResolver(configured=[], profile_exists=lambda name: False)
    with pytest.raises(ValueError, match="does not exist"):
        resolver.resolve("persona:ghost", slot_role="builder")


def _blueprint_task(blueprint_id: str, *, bindings: dict[str, str] | None = None) -> Task:
    bp = BlueprintStore().get(blueprint_id)
    bindings = bindings or {"builder": "persona:dev", "verifier": "persona:qa"}
    plan = instantiate_blueprint(bp, goal="swap smoke", bindings=bindings)
    return _task_with_plan(plan)


def _task_with_plan(plan: MissionPlan) -> Task:
    return Task(
        id=f"task_{plan.blueprint_id or 'blueprint'}",
        title="Blueprint",
        description="swap smoke",
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        mission_plan=plan,
        current_stage_id=plan.current_stage_id,
    )
