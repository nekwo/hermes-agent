from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.config import persona_records_from_config
from agent_runtime.personas import blocked_tool_names, effective_toolsets
from agent_runtime.snapshot import _agent_summary


def _personas_by_id():
    from agent_runtime.store import AgentStore

    return {persona.id: persona for persona in AgentStore().list_all()}


def test_config_personas_include_frontend_compatible_dev_and_backend_dev_bindings():
    personas = _personas_by_id()

    assert {"dev", "backend_dev", "qa", "neko_supervisor"}.issubset(personas)

    frontend = personas["dev"]
    assert frontend.id == "dev", "persisted persona_id=dev must remain stable for active/archived runs"
    assert frontend.role == "dev"
    assert frontend.display_name in {"Launcher Dev Agent", "Frontend Dev"}
    assert frontend.repo_scope_label == "EterniaLauncher"
    assert frontend.repo_scope is None or "EterniaLauncher" in frontend.repo_scope.replace("\\", "/")
    assert frontend.include_core_context_files is False

    backend = personas["backend_dev"]
    assert backend.role == "dev"
    assert backend.display_name in {"Backend Dev Agent", "Backend Dev"}
    assert backend.hermes_profile == "backend-dev"
    assert backend.repo_scope_label == "EterniaBackend"
    assert backend.repo_scope is not None
    assert "EterniaBackend" in backend.repo_scope.replace("\\", "/")
    assert backend.include_core_context_files is False


def test_dev_specialists_share_implementation_toolsets_but_remain_non_qa_roles():
    personas = _personas_by_id()

    for persona_id in ("dev", "backend_dev"):
        persona = personas[persona_id]
        assert persona.role == "dev"
        assert "terminal" in effective_toolsets(persona)
        assert "code_execution" in effective_toolsets(persona)
        assert "write_file" not in blocked_tool_names(persona)
        assert "patch" not in blocked_tool_names(persona)
        assert "send_message" in blocked_tool_names(persona)

    qa = personas["qa"]
    assert qa.role == "qa"
    assert "write_file" not in blocked_tool_names(qa)
    assert "patch" not in blocked_tool_names(qa)


def test_snapshot_agent_summaries_are_collection_based_and_redaction_safe(monkeypatch):
    # _agent_summary sources readiness through the shared TTL-memoized
    # _profile_readiness_for_visibility seam (one compute per agent per build),
    # so stub that rather than the raw profile_readiness_for_persona.
    monkeypatch.setattr(
        "agent_runtime.snapshot._profile_readiness_for_visibility",
        lambda persona: {
            "readiness": "ready",
            "summary": "ready",
            "missing_skills": [],
            "missing_mcp_servers": [],
        },
    )
    summaries = [_agent_summary(persona) for persona in _personas_by_id().values()]
    by_id = {summary["persona_id"]: summary for summary in summaries}

    assert by_id["dev"]["display_name"] in {"Launcher Dev Agent", "Frontend Dev"}
    assert by_id["dev"]["repo_scope_label"] == "EterniaLauncher"
    assert by_id["dev"]["core_context_files"] == "isolated"
    assert by_id["backend_dev"]["display_name"] in {"Backend Dev Agent", "Backend Dev"}
    assert by_id["backend_dev"]["hermes_profile"] == "backend-dev"
    assert by_id["backend_dev"]["repo_scope_label"] == "EterniaBackend"
    assert by_id["backend_dev"]["core_context_files"] == "isolated"

    # Snapshot/model surfaces may expose approved labels and profile ids, but not raw local paths.
    rendered = repr(summaries)
    assert "C:/Users" not in rendered
    assert "C:\\Users" not in rendered
    assert "X:/Unreal Engine" not in rendered
    assert "X:\\Unreal Engine" not in rendered
