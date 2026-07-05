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
from agent_runtime.proof_rules import ProofType
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import ProofStore


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


def test_one_agent_blueprint_instantiates_to_mission_plan_and_run_slot():
    bp = BlueprintStore().get("one_agent_smoke")
    plan = instantiate_blueprint(bp, goal="smoke", bindings={"builder": "profile:gpt-launcher"})
    task = Task(
        id="task_blueprint_smoke",
        title="Smoke",
        description="smoke",
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        mission_plan=plan,
        current_stage_id=plan.current_stage_id,
    )

    assert validate_mission_plan(plan) == []
    assert plan.blueprint_id == "one_agent_smoke"
    assert plan.blueprint_version == 1
    assert plan.bindings == {"builder": "gpt-launcher"}
    assert plan.binding_sources == {"builder": "profile:gpt-launcher"}
    assert plan.stages[0].owner == "gpt-launcher"
    assert plan.stages[0].owner_slot == "builder"

    action = MissionStateMachine().next_action(task)
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "builder"

    summary = mission_plan_summary(task)
    assert summary["blueprint_id"] == "one_agent_smoke"
    assert summary["stages"][0]["owner_slot"] == "builder"


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


def test_blueprint_outcome_edges_route_build_verify_loop():
    task = _blueprint_task("two_agent_build_verify")
    plan = task.mission_plan

    assert apply_stage_outcome(task, "implement", StageOutcome.PASSED, reason="implemented") == "verify"
    assert plan.current_stage_id == "verify"
    assert MissionStateMachine().next_action(task).slot_id == "verifier"

    assert apply_stage_outcome(task, "verify", StageOutcome.FAILED, reason="failed verification") == "implement"
    assert plan.current_stage_id == "implement"
    implement = next(stage for stage in plan.stages if stage.id == "implement")
    assert implement.status == StageStatus.REWORK
    assert plan.stage_attempts == {"implement": 1, "verify": 1}


def test_blueprint_REWORK_routes_back_until_stage_attempt_limit():
    task = _blueprint_task("two_agent_build_verify")
    plan = task.mission_plan

    apply_stage_outcome(task, "implement", StageOutcome.PASSED, reason="attempt 1")
    apply_stage_outcome(task, "verify", StageOutcome.REWORK, reason="needs fixes 1")
    apply_stage_outcome(task, "implement", StageOutcome.PASSED, reason="attempt 2")
    result = apply_stage_outcome(task, "verify", StageOutcome.REWORK, reason="needs fixes 2")

    assert result == "intervention"
    assert task.state == TaskState.RUNNING
    assert plan.current_stage_id == "verify"
    assert plan.stage_attempts == {"implement": 2, "verify": 2}
    assert "blueprint retry limit exceeded" in task.operator_notes[-1]
    action = MissionStateMachine().next_action(task)
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert "adjudication" in action.reason


def test_blueprint_verify_passed_routes_to_done():
    task = _blueprint_task("two_agent_build_verify")

    apply_stage_outcome(task, "implement", StageOutcome.PASSED, reason="implemented")
    result = apply_stage_outcome(task, "verify", StageOutcome.PASSED, reason="APPROVED")

    assert result == "done"
    assert task.mission_plan.current_stage_id is None
    assert MissionStateMachine().next_action(task).type == HarnessActionType.COMPLETE_TASK


def test_blueprint_terminal_close_blocks_latest_failed_stage_proof(isolate_agent_runtime_root):
    task = _blueprint_task("two_agent_build_verify")
    plan = task.mission_plan
    for stage in plan.stages:
        stage.status = StageStatus.PASSED
    plan.current_stage_id = None
    task.current_stage_id = None
    task.state = TaskState.RUNNING

    store = ProofStore()
    passed = store.attach(
        Proof(
            id="proof_implement_passed",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="passed implement proof",
            path_or_value="proof-pass.log",
            created_by="dev",
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0},
            redaction_status="safe",
        )
    )
    failed = store.attach(
        Proof(
            id="proof_implement_failed",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="failed implement proof",
            path_or_value="proof-fail.log",
            created_by="dev",
            created_at=now(),
            metadata={"status": "failed", "exit_code": 1},
            redaction_status="safe",
        )
    )
    task.proof_ids = [passed.id, failed.id]

    action = MissionStateMachine(proof_store=store).next_action(task)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert plan.current_stage_id == "implement"
    implement = next(stage for stage in plan.stages if stage.id == "implement")
    assert implement.status == StageStatus.BLOCKED
    assert "latest proof proof_implement_failed" in action.reason


def test_blueprint_terminal_close_requires_declared_qa_verdict(isolate_agent_runtime_root):
    task = _blueprint_task("two_agent_build_verify")
    plan = task.mission_plan
    for stage in plan.stages:
        stage.status = StageStatus.PASSED
    plan.current_stage_id = None
    task.current_stage_id = None
    task.state = TaskState.RUNNING

    store = ProofStore()
    proof = store.attach(
        Proof(
            id="proof_implement_only",
            task_id=task.id,
            stage_id="implement",
            type=ProofType.TEST_RUN,
            title="passed implement proof",
            path_or_value="proof-pass.log",
            created_by="dev",
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0},
            redaction_status="safe",
        )
    )
    task.proof_ids = [proof.id]

    action = MissionStateMachine(proof_store=store).next_action(task)

    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"
    assert plan.current_stage_id == "verify"
    verify = next(stage for stage in plan.stages if stage.id == "verify")
    assert verify.status == StageStatus.BLOCKED
    assert "missing qa_verdict proof" in action.reason


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


def test_blueprint_on_unhandled_route_is_honored():
    bp = blueprint_from_dict(
        {
            "id": "unhandled_to_done",
            "version": 1,
            "title": "Unhandled To Done",
            "slots": [{"id": "builder", "role": "builder"}],
            "stages": [{"id": "build", "title": "Build", "objective": "Build", "owner_slot": "builder"}],
            "edges": [{"source": "build", "outcome": "passed", "target": "done"}],
            "on_unhandled": "done",
        }
    )
    plan = instantiate_blueprint(bp, goal="smoke", bindings={"builder": "persona:dev"})
    task = _task_with_plan(plan)

    assert next_target(plan, "build", StageOutcome.BLOCKED) == "done"
    assert apply_stage_outcome(task, "build", StageOutcome.BLOCKED, reason="unhandled") == "done"
    assert MissionStateMachine().next_action(task).type == HarnessActionType.COMPLETE_TASK


def test_decision_and_proof_derive_stage_outcome_and_route_edge():
    task = _blueprint_task("two_agent_build_verify")
    apply_stage_outcome(task, "implement", StageOutcome.PASSED, reason="implemented")
    decision = AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="QA approved",
        rationale="evidence passed",
        payload={"review_scope": "implementation", "verdict": "approved", "proof_ids": ["proof_verify"], "findings": []},
    )

    result = MissionStateMachine().apply_decision(task, decision, actor="qa")

    assert result.from_state == TaskState.RUNNING
    assert task.mission_plan.current_stage_id is None
    assert MissionStateMachine().next_action(task).type == HarnessActionType.COMPLETE_TASK


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


def test_required_blueprint_proof_gate_missing_proof_surfaces_hud_evidence():
    task = _blueprint_task("one_agent_smoke", bindings={"builder": "persona:dev"})
    stage = task.mission_plan.stages[0]
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="proof",
        rationale="collect proof",
        payload={"stage_id": "build", "commands": ["python -c pass"]},
    )

    assert stage.proof_gate["required"] is True
    assert derive_stage_outcome(decision, stage, proofs=[]) == StageOutcome.MISSING_INPUT

    assert apply_decision_outcome(task, decision, proofs=[]) == "intervention"
    assert task.state == TaskState.RUNNING
    evidence = task.harness_self_heal["evidence_stack"]
    assert evidence[0]["kind"] == "proof_gate"
    assert evidence[0]["missing"] == ["missing test_run proof"]

    action = MissionStateMachine().next_action(task)
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "neko_supervisor"


def test_blueprint_cli_non_dry_run_creates_persisted_task(tmp_path):
    env = os.environ.copy()
    env["HERMES_AGENT_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "harness",
        "blueprint",
        "run",
        "one_agent_smoke",
        "--goal",
        "smoke",
        "--bind",
        "builder=persona:dev",
        "--json",
    ]
    created = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[3], env=env, capture_output=True, text=True, timeout=30)

    assert created.returncode == 0, created.stderr
    data = json.loads(created.stdout)
    assert data["created"] is True
    assert data["next_action"]["type"] == "run_slot"

    shown = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "task", "show", data["task_id"], "--json"],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert shown.returncode == 0, shown.stderr
    shown_data = json.loads(shown.stdout)
    task_data = shown_data.get("task") or shown_data
    assert task_data["mission_plan"]["blueprint_id"] == "one_agent_smoke"
    assert task_data["mission_plan"]["current_stage_id"] == "build"


def test_blueprint_cli_matrix_run_reports_isolated_cases(tmp_path):
    env = os.environ.copy()
    env["HERMES_AGENT_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "harness",
        "blueprint",
        "matrix-run",
        "two_agent_build_verify",
        "--goal",
        "swap smoke",
        "--bind",
        "verifier=persona:qa",
        "--vary",
        "builder=persona:dev,persona:backend_dev",
        "--dry-run",
        "--json",
    ]
    completed = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[3], env=env, capture_output=True, text=True, timeout=30)

    assert completed.returncode == 0, completed.stderr
    data = json.loads(completed.stdout)
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["case_count"] == 2
    assert [item["bindings"]["builder"] for item in data["results"]] == ["persona:dev", "persona:backend_dev"]
    assert all(item["next_action"]["type"] == "run_slot" for item in data["results"])


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


def test_scope_decision_outcome_attributes_to_deciding_stage_not_advanced_stage():
    """Live 2026-07-03 (task_3e2ae539): Neko's scope release advanced the plan's
    current stage to backend_implementation BEFORE the decision outcome was
    applied, so the scope_route outcome (PASSED) landed on backend_implementation
    with zero proof. The terminal proof gate clawed it back, but at the cost of
    an extra Neko adjudication turn and a redundant dev re-dispatch. The outcome
    must attribute to the stage the deciding run actually ran (stage_id)."""
    bp = BlueprintStore().get("neko_two_dev_default")
    plan = instantiate_blueprint(
        bp,
        goal="attribution",
        bindings={
            "lead": "persona:neko_supervisor",
            "backend_builder": "persona:backend_dev",
            "builder": "persona:dev",
        },
    )
    task = _task_with_plan(plan)
    # Simulate apply_planning_decision having already advanced the current stage
    # (Neko's typed-plan release does this before the outcome is applied).
    task.mission_plan.current_stage_id = "backend_implementation"
    task.current_stage_id = "backend_implementation"
    decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="Route the first no-edit proof slice to Backend Dev",
        rationale="scope",
        payload={"objective": "route", "acceptance_criteria": ["backend proof attached"]},
    )

    MissionStateMachine().apply_decision(task, decision, actor="neko_supervisor", stage_id="scope")

    stages = {stage.id: stage for stage in task.mission_plan.stages}
    assert stages["scope"].status == StageStatus.PASSED
    assert stages["backend_implementation"].status != StageStatus.PASSED, (
        "a scope decision must never mark the downstream dev stage passed without proof"
    )
    assert task.mission_plan.current_stage_id == "backend_implementation"
    action = MissionStateMachine().next_action(task)
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id in {"backend_builder", "backend_dev"}, (
        f"next dispatch must be the backend stage owner, got {action.slot_id}"
    )


def test_scope_decision_on_gated_dev_stage_yields_no_outcome():
    """Neko's recovery re-scope runs while the blocked dev stage is current; its
    propose_acceptance must NOT phantom-pass the proof-gated stage (live
    task_826869af looped neko->implement 5x on exactly this)."""
    bp = BlueprintStore().get("neko_two_dev_default")
    plan = instantiate_blueprint(
        bp,
        goal="recovery attribution",
        bindings={
            "lead": "persona:neko_supervisor",
            "backend_builder": "persona:backend_dev",
            "builder": "persona:dev",
        },
    )
    task = _task_with_plan(plan)
    task.mission_plan.current_stage_id = "backend_implementation"
    task.current_stage_id = "backend_implementation"
    decision = AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="Re-route the blocked backend proof slice to Backend Dev",
        rationale="recovery",
        payload={"objective": "recover", "acceptance_criteria": ["backend proof attached"]},
    )

    MissionStateMachine().apply_decision(
        task, decision, actor="neko_supervisor", stage_id="backend_implementation"
    )

    stages = {stage.id: stage for stage in task.mission_plan.stages}
    assert stages["backend_implementation"].status != StageStatus.PASSED, (
        "a routing decision must never mark a proof-gated dev stage passed"
    )
    assert stages["implement"].status != StageStatus.PASSED


def test_dependency_blocked_branch_waits_for_scope_not_current_stage_id():
    """Graph-driven guard: the dev branches depend_on [scope]. A stale/over-advanced
    current_stage_id pointing at a branch must NOT dispatch it before scope passes —
    dispatch enforces depends_on itself, not current_stage_id blindly. (Descendant of
    the 2026-07-03 task_3e2ae539 fix, on the fork graph.)"""
    bp = BlueprintStore().get("neko_two_dev_default")
    plan = instantiate_blueprint(
        bp,
        goal="dependency dispatch",
        bindings={
            "lead": "persona:neko_supervisor",
            "backend_builder": "persona:backend_dev",
            "builder": "persona:dev",
        },
    )
    task = _task_with_plan(plan)
    assert task.mission_plan.limits["strict_depends_on_dispatch"] == 1
    stages = {stage.id: stage for stage in task.mission_plan.stages}
    stages["scope"].status = StageStatus.READY  # scope NOT passed yet
    stages["backend_implementation"].status = StageStatus.IMPLEMENTING  # stale over-advance
    task.mission_plan.current_stage_id = "backend_implementation"
    task.current_stage_id = "backend_implementation"

    action = MissionStateMachine().next_action(task)

    # Scope (lead) must run first; the branch is blocked on its unmet depends_on.
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "lead"
    assert stages["backend_implementation"].status != StageStatus.PASSED


def test_neko_two_dev_default_fork_dispatches_both_devs_then_completes():
    # Graph-driven fork: neko scopes, both dev branches fan out in parallel (both
    # depend only on scope); the harness completes once BOTH pass — no join stage.
    bp = BlueprintStore().get("neko_two_dev_default")
    plan = instantiate_blueprint(
        bp,
        goal="fork-join dispatch",
        bindings={
            "lead": "persona:neko_supervisor",
            "backend_builder": "persona:backend_dev",
            "builder": "persona:dev",
        },
    )
    from agent_runtime.runtime_config import RuntimeConfig, SwarmConfig

    task = _task_with_plan(plan)
    # Graph-driven concurrent dispatch (the swarm-on path neko uses to deploy both).
    machine = MissionStateMachine(config=RuntimeConfig(swarm=SwarmConfig(max_active_lanes=2)))
    stages = {stage.id: stage for stage in task.mission_plan.stages}

    # Neko scopes first (only the root is ready).
    scope_actions = machine.next_actions(task)
    scope_slots = {a.slot_id for a in scope_actions if a.type == HarnessActionType.RUN_SLOT}
    assert scope_slots == {"lead"}
    stages["scope"].status = StageStatus.PASSED

    # Both dev branches fan out together (neither depends on the other).
    dev_actions = machine.next_actions(task)
    dev_slots = {a.slot_id for a in dev_actions if a.type == HarnessActionType.RUN_SLOT}
    assert dev_slots == {"backend_builder", "builder"}
    # No Neko join stage is offered — the fork joins implicitly at completion.
    assert "lead" not in dev_slots

    # Both branches pass -> the harness completes the task (it waited for both).
    stages["backend_implementation"].status = StageStatus.PASSED
    stages["implement"].status = StageStatus.PASSED
    final = machine.next_action(task)
    assert final.type == HarnessActionType.COMPLETE_TASK


def test_neko_two_dev_default_fires_both_dev_lanes_concurrently_when_lanes_allow():
    # With >1 active lane, scope passing releases BOTH dev branches in the same tick —
    # this is what "neko deploys both sub-agents" looks like on the graph.
    from agent_runtime.runtime_config import RuntimeConfig, SwarmConfig

    bp = BlueprintStore().get("neko_two_dev_default")
    plan = instantiate_blueprint(
        bp,
        goal="concurrent fork",
        bindings={
            "lead": "persona:neko_supervisor",
            "backend_builder": "persona:backend_dev",
            "builder": "persona:dev",
        },
    )
    task = _task_with_plan(plan)
    machine = MissionStateMachine(config=RuntimeConfig(swarm=SwarmConfig(max_active_lanes=2)))
    stages = {stage.id: stage for stage in task.mission_plan.stages}
    stages["scope"].status = StageStatus.PASSED

    ready = machine.next_actions(task)
    ready_slots = {action.slot_id for action in ready if action.type == HarnessActionType.RUN_SLOT}
    assert ready_slots == {"backend_builder", "builder"}
