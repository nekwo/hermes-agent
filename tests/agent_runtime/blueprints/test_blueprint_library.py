from __future__ import annotations

from pathlib import Path


def test_stage_graph_catalog_is_removed() -> None:
    package = Path(__file__).parents[3] / "agent_runtime" / "blueprints"
    assert not list(package.glob("*.yaml"))
