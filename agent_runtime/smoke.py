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
from .states import RunState, TaskState
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
            description="Verify Neko Mission Lead -> Backend Dev -> Launcher Dev -> proof -> done in a temp root.",
            state=TaskState.CREATED,
            created_at=ts,
            updated_at=ts,
            requested_by="smoke",
            acceptance_criteria=["Backend smoke proof attached", "Launcher smoke proof attached"],
        )
        ensure_default_mission_plan(task)
        task_store.create(task)
        machine = MissionStateMachine(proof_store=proof_store)
        transitions: list[str] = []

        first_action = machine.next_action(task)
        if first_action.type != HarnessActionType.RUN_SLOT or first_action.slot_id != "neko_supervisor":
            raise RuntimeError(f"expected Neko Mission Lead first action, got {first_action.type.value}")
        neko_decision = AgentDecision(
            type=DecisionType.SCOPE_ROUTE,
            summary="smoke Neko scoped mission",
            rationale="deterministic no-model smoke exercises Neko Mission Lead scoping",
            payload={
                "objective": task.description,
                "acceptance_criteria": list(task.acceptance_criteria),
                "target_owner": "backend_dev",
                "target_repo": "EterniaBackend",
                "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed"},
            },
        )
        validate_decision_for_role(neko_decision, AgentRole.ALICE_SUPERVISOR)
        run = run_store.open_run("neko_supervisor", task.id, None, tick_id="tick_smoke")
        machine.apply_decision(task, neko_decision, actor="neko_supervisor", proof_store=proof_store, run_id=run.id)
        run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": neko_decision.type.value, "summary": neko_decision.summary})
        transitions.append("neko_supervisor:scope_route")

        if task.current_stage_id != "backend_implementation":
            raise RuntimeError(f"expected backend implementation stage after Neko, got {task.current_stage_id!r}")
        proof_store.attach(Proof(id="proof_smoke_backend", task_id=task.id, stage_id=task.current_stage_id, type=ProofType.TEST_RUN, title="Smoke backend no-model test", path_or_value="no-model backend smoke", created_by="smoke", created_at=now(), metadata={"status": "passed", "exit_code": 0}, redaction_status="safe"))
        backend_decision = AgentDecision(
            type=DecisionType.HAND_OFF,
            summary="smoke Backend Dev attached proof",
            rationale="deterministic no-model smoke has a safe backend proof artifact",
            payload={"stage_id": task.current_stage_id},
        )
        validate_decision_for_role(backend_decision, AgentRole.DEV)
        run = run_store.open_run("backend_dev", task.id, task.current_stage_id, tick_id="tick_smoke")
        machine.apply_decision(task, backend_decision, actor="backend_dev", proof_store=proof_store, run_id=run.id, normal_worker_flow=True)
        run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": backend_decision.type.value, "summary": backend_decision.summary})
        transitions.append("backend_dev:hand_off")

        if task.current_stage_id != "implement":
            raise RuntimeError(f"expected launcher implementation stage after Backend Dev, got {task.current_stage_id!r}")
        proof_store.attach(Proof(id="proof_smoke_launcher", task_id=task.id, stage_id=task.current_stage_id, type=ProofType.TEST_RUN, title="Smoke launcher no-model test", path_or_value="no-model launcher smoke", created_by="smoke", created_at=now(), metadata={"status": "passed", "exit_code": 0}, redaction_status="safe"))
        dev_decision = AgentDecision(
            type=DecisionType.HAND_OFF,
            summary="smoke Launcher Dev attached proof",
            rationale="deterministic no-model smoke has a safe launcher proof artifact",
            payload={"stage_id": task.current_stage_id},
        )
        validate_decision_for_role(dev_decision, AgentRole.DEV)
        run = run_store.open_run("dev", task.id, task.current_stage_id, tick_id="tick_smoke")
        machine.apply_decision(task, dev_decision, actor="dev", proof_store=proof_store, run_id=run.id, normal_worker_flow=True)
        run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": dev_decision.type.value, "summary": dev_decision.summary})
        transitions.append("dev:hand_off")

        task.proof_ids = ["proof_smoke_backend", "proof_smoke_launcher"]
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
