from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes_time import now

from .actions import HarnessActionType
from .default_plan import ensure_default_mission_plan
from .decision_schema import AgentDecision, DecisionType, validate_decision_for_role
from .models import Proof, Task
from .personas import AgentRole
from .proof_rules import ProofType
from .state_machine import MissionStateMachine
from .states import RunState, StageStatus, TaskState
from .store import ProofStore, RunStore, TaskStore


@contextmanager
def _runtime_root(root: Path | None) -> Iterator[Path]:
    previous = os.environ.get("HERMES_AGENT_RUNTIME_ROOT")
    tmp: str | None = None
    try:
        if root is None:
            tmp = tempfile.mkdtemp(prefix="hermes-harness-smoke-")
            os.environ["HERMES_AGENT_RUNTIME_ROOT"] = tmp
            yield Path(tmp)
        else:
            os.environ["HERMES_AGENT_RUNTIME_ROOT"] = str(root)
            root.mkdir(parents=True, exist_ok=True)
            yield root
    finally:
        if previous is None:
            os.environ.pop("HERMES_AGENT_RUNTIME_ROOT", None)
        else:
            os.environ["HERMES_AGENT_RUNTIME_ROOT"] = previous


def run_smoke(*, temp_root: bool = True, no_model: bool = True) -> dict:
    if not no_model:
        return {
            "ok": False,
            "mode": "live_model",
            "runtime_root_kind": "temp" if temp_root else "configured",
            "failure_class": "live_model_smoke_not_implemented",
            "intervention": {
                "severity": "medium",
                "required_upgrade": "Run a credentialed live persona smoke after deterministic proof collection is enabled and profile readiness is confirmed.",
                "safe_default": "Use --no-model for deterministic CI/local smoke until live provider credentials are intentionally exercised.",
            },
        }
    root_arg = None if temp_root else Path(os.environ.get("HERMES_AGENT_RUNTIME_ROOT", ".hermes-agent-runtime"))
    with _runtime_root(root_arg) as root:
        task_store = TaskStore(); run_store = RunStore(); proof_store = ProofStore()
        ts = now()
        task = Task(
            id="task_smoke",
            title="Harness smoke goal",
            description="Verify Neko Mission Lead -> Dev -> QA -> proof -> done in a temp root.",
            state=TaskState.CREATED,
            created_at=ts,
            updated_at=ts,
            requested_by="smoke",
            acceptance_criteria=["Smoke proof attached", "QA verdict approved"],
        )
        ensure_default_mission_plan(task)
        task_store.create(task)
        machine = MissionStateMachine(proof_store=proof_store)
        transitions: list[str] = []

        first_action = machine.next_action(task)
        if first_action.type != HarnessActionType.RUN_SLOT or first_action.slot_id != "neko_supervisor":
            raise RuntimeError(f"expected Neko Mission Lead first action, got {first_action.type.value}")
        neko_decision = AgentDecision(
            type=DecisionType.PROPOSE_ACCEPTANCE,
            summary="smoke Neko scoped mission",
            rationale="deterministic no-model smoke exercises Neko Mission Lead scoping",
            payload={"objective": task.description, "acceptance_criteria": list(task.acceptance_criteria)},
        )
        validate_decision_for_role(neko_decision, AgentRole.ALICE_SUPERVISOR)
        run = run_store.open_run("neko_supervisor", task.id, None, tick_id="tick_smoke")
        machine.apply_decision(task, neko_decision, actor="neko_supervisor", proof_store=proof_store, run_id=run.id)
        run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": neko_decision.type.value, "summary": neko_decision.summary})
        transitions.append("neko_supervisor:propose_acceptance")

        task.current_stage_id = task.mission_plan.current_stage_id if task.mission_plan else "implement"
        proof_store.attach(Proof(id="proof_smoke_test", task_id=task.id, stage_id=task.current_stage_id, type=ProofType.TEST_RUN, title="Smoke no-model test", path_or_value="no-model smoke", created_by="smoke", created_at=now(), metadata={"status": "passed", "exit_code": 0}, redaction_status="safe"))
        dev_decision = AgentDecision(
            type=DecisionType.PROPOSE_PATCH,
            summary="smoke Dev attached proof",
            rationale="deterministic no-model smoke has a safe proof artifact",
            payload={"proof_ids": ["proof_smoke_test"]},
        )
        validate_decision_for_role(dev_decision, AgentRole.DEV)
        run = run_store.open_run("dev", task.id, task.current_stage_id, tick_id="tick_smoke")
        machine.apply_decision(task, dev_decision, actor="dev", proof_store=proof_store, run_id=run.id)
        run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": dev_decision.type.value, "summary": dev_decision.summary})
        transitions.append("dev:propose_patch")

        qa_decision = AgentDecision(
            type=DecisionType.REPORT_QA_VERDICT,
            summary="smoke QA approved proof",
            rationale="QA reviewed the deterministic no-model proof",
            payload={"review_scope": "implementation", "verdict": "approved", "proof_ids": ["proof_smoke_test"], "findings": []},
        )
        validate_decision_for_role(qa_decision, AgentRole.QA)
        proof_store.attach(Proof(id="proof_smoke_qa", task_id=task.id, stage_id=task.current_stage_id, type=ProofType.QA_VERDICT, title="Smoke QA verdict", path_or_value="approved", created_by="qa", created_at=now(), metadata={"verdict": "approved"}, redaction_status="safe"))
        run = run_store.open_run("qa", task.id, task.current_stage_id, tick_id="tick_smoke")
        machine.apply_decision(task, qa_decision, actor="qa", proof_store=proof_store, run_id=run.id)
        run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": qa_decision.type.value, "summary": qa_decision.summary})
        transitions.append("qa:report_qa_verdict")

        task.proof_ids = ["proof_smoke_test", "proof_smoke_qa"]
        close_action = machine.next_action(task)
        if close_action.type != HarnessActionType.COMPLETE_TASK:
            raise RuntimeError(f"expected deterministic completion, got {close_action.type.value}")
        task.state = TaskState.DONE
        task.updated_at = now()
        task_store.update(task, actor="smoke", reason=close_action.reason)
        return {
            "ok": True,
            "mode": "no_model",
            "runtime_root_kind": "temp" if temp_root else "configured",
            "task_id": task.id,
            "first_action": first_action.type.value,
            "final_action": close_action.type.value,
            "final_state": task.state.value,
            "transitions": transitions,
            "proof_ids": list(task.proof_ids),
        }
