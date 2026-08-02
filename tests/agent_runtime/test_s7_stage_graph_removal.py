from __future__ import annotations

from pathlib import Path

from agent_runtime.blueprints.resolve import promote_profile_to_persona as legacy_promote
from agent_runtime.personas import promote_profile_to_persona




def test_blueprints_package_is_only_the_permanent_profile_promotion_shim() -> None:
    package = Path(__file__).parents[2] / "agent_runtime" / "blueprints"
    assert {path.name for path in package.iterdir() if path.name != "__pycache__"} == {
        "__init__.py",
        "resolve.py",
    }
    assert legacy_promote is promote_profile_to_persona
