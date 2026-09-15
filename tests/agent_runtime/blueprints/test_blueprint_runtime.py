from __future__ import annotations

import importlib.util


def test_stage_graph_implementation_modules_are_removed() -> None:
    for module in (
        "agent_runtime.blueprints.instantiate",
        "agent_runtime.blueprints.routing",
        "agent_runtime.blueprints.runs",
        "agent_runtime.blueprints.schema",
        "agent_runtime.blueprints.store",
    ):
        assert importlib.util.find_spec(module) is None
