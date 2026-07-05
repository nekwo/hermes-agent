from __future__ import annotations

import json

from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun, Task
from agent_runtime.observability import build_observability
from agent_runtime.persona_runtime import _apply_llm_metadata
from agent_runtime.profile_runner import AgentRunResult
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import RunStore, TaskStore
from agent_runtime.ticker import TickEngine


class RuntimeWithMetadata:
    def run_tick(self, persona, ctx, *, run):
        run.session_id = "session_test_123"
        run.llm = {
            "provider": "openai-codex",
            "model": "gpt-test",
            "base_url_host": "chatgpt.com",
            "session_id": "session_test_123",
            "api_calls": 1,
            "tool_turns": 0,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "timing": {"provider_call_ms": 7},
        }
        return AgentDecision(
            type=DecisionType.PROPOSE_ACCEPTANCE,
            summary="ok",
            rationale="r",
            payload={"objective": "obj", "acceptance_criteria": ["done"]},
        )


def _task():
    ts = now()
    return Task(id="task_meta", title="T", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony")


def test_tick_persists_session_id_and_llm_metadata():
    tasks = TaskStore()
    runs = RunStore()
    tasks.create(_task())

    result = TickEngine(task_store=tasks, run_store=runs, persona_runtime=RuntimeWithMetadata()).tick_once()

    run_id = result.actions_taken[0].payload["run_id"]
    stored = runs.get(run_id)
    assert stored.session_id == "session_test_123"
    assert stored.llm["provider"] == "openai-codex"
    assert stored.llm["decision_type"] == "scope_route"
    assert stored.llm["validation_status"] == "valid"
    timing = stored.llm["timing"]
    assert timing["context_build_ms"] >= 0
    assert timing["autonomy_packet_ms"] >= 0
    assert timing["persona_runtime_ms"] >= 0
    assert timing["decision_apply_ms"] >= 0
    assert timing["provider_call_ms"] == 7


def test_run_events_include_correlation_fields_and_safe_llm_counts():
    tasks = TaskStore()
    runs = RunStore()
    tasks.create(_task())

    result = TickEngine(task_store=tasks, run_store=runs, persona_runtime=RuntimeWithMetadata()).tick_once()
    run_id = result.actions_taken[0].payload["run_id"]
    events = EventLog().tail(30)
    opened = next(event for event in events if event.type == "run.opened" and event.run_id == run_id)
    closed = next(event for event in events if event.type == "run.closed" and event.run_id == run_id)

    assert opened.payload["tick_id"] == result.tick_id
    assert opened.task_id == "task_meta"
    assert opened.persona_id == "neko_supervisor"
    assert closed.payload["session_id"] == "session_test_123"
    assert closed.payload["decision_type"] == "scope_route"
    assert closed.payload["validation_status"] == "valid"
    assert closed.payload["total_tokens"] == 15


def test_observability_recent_runs_exposes_safe_llm_metadata_without_full_urls_or_prompts():
    ts = now()
    run = AgentRun(
        id="run_safe",
        persona_id="pm",
        task_id="task_safe",
        stage_id=None,
        state=RunState.COMPLETED,
        started_at=ts,
        last_heartbeat_at=ts,
        finished_at=ts,
        session_id="session_safe",
        final_decision={"type": "propose_acceptance", "summary": "ok"},
        error={"message": "raw prompt with SECRET_TOKEN must not surface"},
        llm={
            "provider": "openai-codex",
            "model": "gpt-test",
            "base_url_host": "chatgpt.com",
            "base_url": "https://token:SECRET@example.com/private",
            "session_id": "session_safe",
            "total_tokens": 123,
            "timing": {"context_build_ms": 3, "provider_call_ms": 42, "unsafe": "x"},
            "validation_status": "valid",
            "decision_type": "propose_acceptance",
        },
    )
    obs = build_observability(tasks=[], runs=[run], incidents=[], proofs=[], daemon_status={"state": "offline"}, reference_time=ts)
    encoded = json.dumps(obs, default=str)

    assert obs["recent_runs"][0]["session_id"] == "session_safe"
    assert obs["recent_runs"][0]["provider"] == "openai-codex"
    assert obs["recent_runs"][0]["base_url_host"] == "chatgpt.com"
    assert obs["recent_runs"][0]["timing"] == {"context_build_ms": 3, "provider_call_ms": 42}
    assert "SECRET" not in encoded
    assert "/private" not in encoded
    assert "raw prompt" not in encoded


def test_llm_metadata_records_redaction_safe_decision_metrics():
    ts = now()
    run = AgentRun(
        id="run_metrics",
        persona_id="dev",
        task_id="task_metrics",
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        progress={"loop_warning": "read_search_without_patch_threshold", "read_search_count": 6, "patch_count": 0},
    )
    result = AgentRunResult(
        final_response="{}",
        session_id="session_metrics",
        provider="openai-codex",
        model="gpt-test",
        base_url="https://chatgpt.com/backend-api/codex",
        messages=[{"role": "assistant", "tool_calls": [{"id": "tool_1"}]}],
        api_calls=2,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        latency_ms=42,
    )

    _apply_llm_metadata(run, result)

    metrics = run.llm["decision_metrics"]
    assert metrics["classification"] == "possible_loop"
    assert metrics["api_calls"] == 2
    assert metrics["tool_turns"] == 1
    assert metrics["read_search_count"] == 6
