from __future__ import annotations

import importlib.util

import agent_runtime.models as models


def test_stage_graph_runtime_is_removed() -> None:
    assert importlib.util.find_spec("agent_runtime.default_plan") is None
    assert importlib.util.find_spec("agent_runtime.mission_plan") is None
    assert importlib.util.find_spec("agent_runtime.state_machine") is None
    assert not hasattr(models, "MissionPlan")
    assert not hasattr(models, "MissionPlanStage")
    assert not hasattr(models, "TaskStage")
