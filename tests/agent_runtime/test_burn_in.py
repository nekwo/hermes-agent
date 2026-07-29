from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pytest

from hermes_time import now

from agent_runtime.burn_in import _product_repos_modified, burn_in_dir, create_burn_in, run_burn_in_case, summarize_burn_in, swarm_certification_allows_production
from agent_runtime.errors import LegacyOrchestratorRemoved
from agent_runtime.models import Incident, Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import IncidentStore, ProofStore, RunStore, TaskStore


class PassingBurnInEngine:
    def __init__(self):
        self.task_store = TaskStore()
        self.run_store = RunStore()
        self.proof_store = ProofStore()
        self.incident_store = IncidentStore()

    def run_until_settled(self, *, task_id: str, max_actions: int):
        run = self.run_store.open_run("qa", task_id, tick_id="tick_burn")
        self.run_store.close_run(
            run.id,
            state=RunState.COMPLETED,
            final_decision={"type": "report_qa_verdict", "summary": "approved"},
        )
        proof = self.proof_store.attach(
            Proof(
                id="proof_burn_pass",
                task_id=task_id,
                stage_id=None,
                type=ProofType.QA_VERDICT,
                title="QA approved",
                path_or_value="approved",
                created_by="qa",
                created_at=now(),
                metadata={"verdict": "approved"},
                redaction_status="safe",
            )
        )
        task = self.task_store.get(task_id)
        task.state = TaskState.DONE
        task.proof_ids.append(proof.id)
        task.updated_at = now()
        self.task_store.update(task, actor="qa", reason="burn-in passed")
        return RunUntilSettledResult(
            settle_id="settle_burn",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            stop_reason="task_terminal",
            final_task_state=TaskState.DONE.value,
            max_actions=max_actions,
        )


class StalledBurnInEngine:
    def __init__(self):
        self.task_store = TaskStore()
        self.run_store = RunStore()
        self.proof_store = ProofStore()
        self.incident_store = IncidentStore()

    def run_until_settled(self, *, task_id: str, max_actions: int):
        run = self.run_store.open_run("dev", task_id, stage_id="stage_1", tick_id="tick_burn")
        run.last_heartbeat_at = now() - timedelta(seconds=600)
        self.run_store.update(run)
        return RunUntilSettledResult(
            settle_id="settle_burn",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            stop_reason="active_run",
            final_task_state=TaskState.RUNNING.value,
            max_actions=max_actions,
        )


class BudgetContinuationBurnInEngine:
    def __init__(self):
        self.task_store = TaskStore()
        self.run_store = RunStore()
        self.proof_store = ProofStore()
        self.incident_store = IncidentStore()
        self.calls = 0

    def run_until_settled(self, *, task_id: str, max_actions: int):
        self.calls += 1
        if self.calls == 1:
            run = self.run_store.open_run("dev", task_id, stage_id="launcher_contract_smoke", session_id="session_budget", tick_id="tick_budget")
            run.state = RunState.WAITING_ON_APPROVAL
            run.error = {"type": "run_budget_exceeded", "summary": "live run budget exceeded"}
            self.run_store.update(run)
            incident = self.incident_store.open(
                Incident(
                    id="inc_budget",
                    task_id=task_id,
                    run_id=run.id,
                    kind="run_budget_exceeded",
                    summary="live run budget exceeded",
                    detail_path=None,
                    opened_at=now(),
                )
            )
            task = self.task_store.get(task_id)
            task.open_incident_ids.append(incident.id)
            task.updated_at = now()
            self.task_store.update(task, actor="harness", reason="budget continuation required")
            return RunUntilSettledResult(
                settle_id="settle_budget_1",
                started_at=now(),
                finished_at=now(),
                task_id=task_id,
                stop_reason="action_failed",
                final_task_state=TaskState.RUNNING.value,
                max_actions=max_actions,
            )
        waiting = self.run_store.list_for_task(task_id)[0]
        self.run_store.approve_continuation(waiting.id)
        self.incident_store.close("inc_budget", reason="Neko approved bounded continuation")
        run = self.run_store.open_run("qa", task_id, tick_id="tick_budget_done")
        self.run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "report_qa_verdict", "summary": "approved"})
        proof = self.proof_store.attach(
            Proof(
                id="proof_after_budget",
                task_id=task_id,
                stage_id=None,
                type=ProofType.QA_VERDICT,
                title="QA approved",
                path_or_value="approved",
                created_by="qa",
                created_at=now(),
                metadata={"verdict": "approved"},
                redaction_status="safe",
            )
        )
        task = self.task_store.get(task_id)
        task.state = TaskState.DONE
        task.open_incident_ids = []
        task.proof_ids.append(proof.id)
        task.updated_at = now()
        self.task_store.update(task, actor="qa", reason="burn-in passed after budget continuation")
        return RunUntilSettledResult(
            settle_id="settle_budget_2",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            stop_reason="task_terminal",
            final_task_state=TaskState.DONE.value,
            max_actions=max_actions,
        )


class BudgetCapReachedBurnInEngine:
    def __init__(self):
        self.task_store = TaskStore()
        self.run_store = RunStore()
        self.proof_store = ProofStore()
        self.incident_store = IncidentStore()
        self.calls = 0

    def run_until_settled(self, *, task_id: str, max_actions: int):
        self.calls += 1
        for idx in range(2):
            approved = self.run_store.open_run("dev", task_id, stage_id="stage_1", session_id="session_budget")
            approved.state = RunState.WAITING_ON_APPROVAL
            approved.error = {"type": "run_budget_exceeded", "summary": f"budget {idx}"}
            self.run_store.update(approved)
            self.run_store.approve_continuation(approved.id)
        waiting = self.run_store.open_run("dev", task_id, stage_id="stage_1", session_id="session_budget")
        waiting.state = RunState.WAITING_ON_APPROVAL
        waiting.error = {"type": "run_budget_exceeded", "summary": "budget cap reached"}
        self.run_store.update(waiting)
        incident = self.incident_store.open(
            Incident(
                id="inc_budget_cap",
                task_id=task_id,
                run_id=waiting.id,
                kind="run_budget_exceeded",
                summary="budget cap reached",
                detail_path=None,
                opened_at=now(),
            )
        )
        task = self.task_store.get(task_id)
        task.open_incident_ids.append(incident.id)
        task.updated_at = now()
        self.task_store.update(task, actor="harness", reason="budget cap reached")
        return RunUntilSettledResult(
            settle_id="settle_budget_cap",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            stop_reason="action_failed",
            final_task_state=TaskState.RUNNING.value,
            max_actions=max_actions,
        )


def test_swarm_certification_gate_blocks_production_but_exempts_playground(isolate_agent_runtime_root):
    allowed, cert = swarm_certification_allows_production()
    assert allowed is False
    assert cert["state"] == "red"

    allowed, cert = swarm_certification_allows_production(lane_kind="playground")
    assert allowed is True
    assert cert["exempt"] is True

    allowed, cert = swarm_certification_allows_production(allow_uncertified_dev_swarm=True)
    assert allowed is True
    assert cert["override"] is True


def test_burn_in_product_repo_modified_flag_ignores_harness_only_dirty_state():
    assert _product_repos_modified({"repos": [{"label": "EterniaLauncher", "dirty": True, "error": None}]}) is True
    assert _product_repos_modified({"repos": [{"label": "EterniaBackend", "dirty": False, "error": "git_status_failed"}]}) is True
    assert _product_repos_modified({"repos": [{"label": "hermes-agent", "dirty": True, "error": None}]}) is False
    assert _product_repos_modified(None) is False


def test_burn_in_execution_refuses_after_dispatch_loop_retirement():
    with pytest.raises(LegacyOrchestratorRemoved, match="dispatch loop is retired"):
        run_burn_in_case("noop-orchestration")


def test_legacy_stage46_blueprint_ids_still_resolve():
    """Old records name these blueprints after the retired Stage 46; keep reading them."""
    from agent_runtime.burn_in import _custom_blueprint_raw

    legacy_to_current = {
        "stage46_custom_backend_proof": "custom_backend_proof",
        "stage46_custom_launcher_proof": "custom_launcher_proof",
        "stage46_custom_cross_stack_proof": "custom_cross_stack_proof",
    }
    for legacy_id, current_id in legacy_to_current.items():
        assert _custom_blueprint_raw(legacy_id) == _custom_blueprint_raw(current_id)
        assert _custom_blueprint_raw(legacy_id)["id"] == current_id


def test_burn_in_summary_fails_closed_when_evidence_is_missing():
    manifest = create_burn_in(case_id="noop-orchestration")

    summary = summarize_burn_in(manifest["burn_id"])

    assert summary["ok"] is False
    assert summary["failure_class"] == "incomplete_evidence"
    assert "task_create.json" in summary["missing_files"]


def test_burn_in_create_cleans_previous_stage47_temp_state():
    ts = TaskStore()
    runs = RunStore()
    stamp = now()
    old = ts.create(Task(id="task_burn_previous", title="Previous burn-in", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="stage47_burn_in"))
    run = runs.open_run("dev", old.id)

    manifest = create_burn_in(case_id="noop-orchestration")

    assert old.id in manifest["new_goal_hygiene"]["cancelled_task_ids"]
    assert run.id in manifest["new_goal_hygiene"]["cancelled_run_ids"]
    assert ts.get(old.id).state == TaskState.CANCELLED
    assert runs.get(run.id).state == RunState.CANCELLED
