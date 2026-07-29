from __future__ import annotations

import types

import pytest

from hermes_cli.runtime_environment import missing_runtime_packages_for, runtime_environment_status

from agent_runtime.models import AgentPersona, AgentRun, Task
from agent_runtime.context_builder import build_context
from agent_runtime.persona_runtime import GPTPersonaRuntime
from agent_runtime.provider_health import provider_health_for_personas
from agent_runtime.status import build_status
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import TaskStore
from hermes_time import now


def _persona() -> AgentPersona:
    return AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        provider="openai-codex",
        model="gpt-5.5",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="personas/dev/system.md",
    )


def test_provider_health_reports_runtime_profile_and_interpreter(isolate_agent_runtime_root):
    health = provider_health_for_personas([_persona()])

    assert "interpreter" in health
    assert health["runtime_root"] == str(isolate_agent_runtime_root)
    assert health["hermes_profile"]
    assert "openai" in health["required_packages"]
    assert "issues" in health


def test_status_includes_provider_runtime_health(isolate_agent_runtime_root):
    status = build_status(task_store=TaskStore())

    assert "runtime_health" in status
    assert "interpreter" in status["runtime_health"]
    assert status["runtime_health"]["runtime_root"] == str(isolate_agent_runtime_root)


def test_corrupt_jiter_from_json_is_reported_before_token_spend(monkeypatch):
    import hermes_cli.runtime_environment as runtime_environment

    monkeypatch.setattr(runtime_environment.importlib.util, "find_spec", lambda package: object() if package in {"openai"} else None)
    monkeypatch.setitem(__import__("sys").modules, "jiter", types.ModuleType("jiter"))

    status = runtime_environment_status(["openai"])

    assert status.package_available["openai"] is True
    assert any(issue["package"] == "jiter.from_json" for issue in status.issues)
    assert missing_runtime_packages_for(provider="openai-codex", api_mode="codex_responses", model="gpt-5.5") == ["jiter.from_json"]
