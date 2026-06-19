import builtins
import importlib
import sys


def _block_environment_helper_import(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if (
            name == "agent.skill_utils"
            and "skill_matches_environment" in tuple(fromlist or ())
        ):
            raise ImportError(
                "cannot import name 'skill_matches_environment' from 'agent.skill_utils'"
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_prompt_builder_import_survives_missing_environment_helper(monkeypatch):
    original = sys.modules.pop("agent.prompt_builder", None)
    try:
        _block_environment_helper_import(monkeypatch)

        module = importlib.import_module("agent.prompt_builder")

        assert module.skill_matches_environment({}) is True
        assert module.skill_matches_environment({"environments": ["kanban"]}) is False
    finally:
        sys.modules.pop("agent.prompt_builder", None)
        if original is not None:
            sys.modules["agent.prompt_builder"] = original


def test_skills_tool_environment_helper_fails_closed_for_tagged_skills(monkeypatch):
    from tools import skills_tool

    _block_environment_helper_import(monkeypatch)

    assert skills_tool.skill_matches_environment({}) is True
    assert skills_tool.skill_matches_environment({"environments": ["kanban"]}) is False
