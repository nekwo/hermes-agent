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


def test_live_persona_preflight_raises_before_runner_when_provider_dependency_corrupt(monkeypatch):
    import agent_runtime.provider_health as provider_health

    called = {"runner": False}

    class ShouldNotRunAgent:
        def __init__(self, **kwargs):
            called["runner"] = True

    monkeypatch.setattr(provider_health, "runtime_environment_status", lambda packages: types.SimpleNamespace(
        executable="/test/python",
        package_available={"openai": True},
        issues=[{"kind": "runtime_dependency_corrupt", "package": "jiter.from_json", "summary": "broken"}],
    ))
    ts = now()
    task = Task(id="task_health", title="Health", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="tony")
    run = AgentRun(id="run_health", persona_id="dev", task_id=task.id, stage_id=None, state=RunState.RUNNING, started_at=ts, last_heartbeat_at=ts)
    runtime = GPTPersonaRuntime(agent_factory=ShouldNotRunAgent)

    with pytest.raises(ImportError, match="before token spend"):
        runtime.run_tick(_persona(), build_context(task, run), run=run)

    assert called["runner"] is False
