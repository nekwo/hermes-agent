from __future__ import annotations

import pytest

from hermes_time import now

from agent_runtime.actions import HarnessActionType
from agent_runtime.blueprints import BlueprintStore, instantiate_blueprint
from agent_runtime.blueprints.routing import apply_stage_outcome
from agent_runtime.blueprints.schema import StageOutcome, validate_blueprint
from agent_runtime.models import Task
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import TaskState


BLUEPRINT_REPLAYS = {
    "one_agent_smoke": {
        "bindings": {"builder": "persona:dev"},
        "first_slot": "builder",
        "steps": [("build", StageOutcome.PASSED)],
    },
    "two_agent_build_verify": {
        "bindings": {"builder": "persona:dev", "verifier": "persona:qa"},
        "first_slot": "builder",
        "steps": [("implement", StageOutcome.PASSED), ("verify", StageOutcome.PASSED)],
    },
    "neko_dev_qa_basic": {
        "bindings": {"lead": "persona:neko_supervisor", "builder": "persona:dev", "verifier": "persona:qa"},
        "first_slot": "lead",
        "steps": [("scope", StageOutcome.READY), ("implement", StageOutcome.PASSED), ("verify", StageOutcome.PASSED)],
    },
    "frontend_backend_join": {
        "bindings": {"backend": "persona:backend_dev", "frontend": "persona:dev", "verifier": "persona:qa"},
        "first_slot": "backend",
        "steps": [
            ("backend_contract", StageOutcome.PASSED),
            ("frontend_implementation", StageOutcome.PASSED),
            ("verify_join", StageOutcome.PASSED),
        ],
    },
    "visual_ui_qa": {
        "bindings": {"builder": "persona:dev", "verifier": "persona:qa"},
        "first_slot": "builder",
        "steps": [("implement_ui", StageOutcome.PASSED), ("visual_verify", StageOutcome.PASSED)],
    },
    "full_production_flow": {
        "bindings": {
            "lead": "persona:neko_supervisor",
            "backend": "persona:backend_dev",
            "frontend": "persona:dev",
            "verifier": "persona:qa",
            "reviewer": "persona:qa",
        },
        "first_slot": "lead",
        "steps": [
            ("scope", StageOutcome.READY),
            ("backend_contract", StageOutcome.PASSED),
            ("frontend_implementation", StageOutcome.PASSED),
            ("local_self_test", StageOutcome.PASSED),
            ("qa_verify", StageOutcome.PASSED),
            ("staging_smoke", StageOutcome.PASSED),
            ("production_rollout_proof", StageOutcome.PASSED),
            ("final_verdict", StageOutcome.PASSED),
        ],
    },
}


def test_blueprint_library_contains_expected_ship_set():
    ids = {bp.id for bp in BlueprintStore().list()}

    assert set(BLUEPRINT_REPLAYS).issubset(ids)




def _task_for(blueprint_id: str, bindings: dict[str, str]) -> Task:
    bp = BlueprintStore().get(blueprint_id)
    plan = instantiate_blueprint(bp, goal=f"{blueprint_id} replay", bindings=bindings)
    return Task(
        id=f"task_{blueprint_id}_replay",
        title=bp.title,
        description=f"{blueprint_id} replay",
        state=TaskState.CREATED,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        mission_plan=plan,
        current_stage_id=plan.current_stage_id,
    )
