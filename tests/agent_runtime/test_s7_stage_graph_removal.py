from __future__ import annotations

import importlib.util
from pathlib import Path

import agent_runtime.models as models
from agent_runtime.blueprints.resolve import promote_profile_to_persona as legacy_promote
from agent_runtime.personas import promote_profile_to_persona


def test_retired_stage_graph_modules_are_absent() -> None:
    assert importlib.util.find_spec("agent_runtime.default_plan") is None
    assert importlib.util.find_spec("agent_runtime.mission_plan") is None
    assert importlib.util.find_spec("agent_runtime.state_machine") is None
    assert not hasattr(models, "MissionPlan")
    assert not hasattr(models, "MissionPlanStage")
    assert not hasattr(models, "TaskStage")


def test_blueprints_package_is_only_the_permanent_profile_promotion_shim() -> None:
    package = Path(__file__).parents[2] / "agent_runtime" / "blueprints"
    assert {path.name for path in package.iterdir() if path.name != "__pycache__"} == {
        "__init__.py",
        "resolve.py",
    }
    assert legacy_promote is promote_profile_to_persona
