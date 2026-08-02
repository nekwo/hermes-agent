from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.config import AgentRuntimeConfig, persona_records_from_config
from agent_runtime.models import AgentPersona
from agent_runtime.personas import AgentRole, blocked_tool_names, effective_toolsets
from tests.agent_runtime.persona_samples import sample_personas
import agent_runtime.repo_context as repo_context
from agent_runtime.repo_context import repo_execution_context_for_task, resolve_affected_repo_workdir, safe_affected_repo_labels
from agent_runtime.snapshot import build_snapshot


def _by_id(personas: list[AgentPersona]) -> dict[str, AgentPersona]:
    return {persona.id: persona for persona in personas}


def test_default_specialist_persona_collection_includes_backend_dev_and_frontend_dev_compat_label():
    personas = _by_id(sample_personas())

    assert {"dev", "qa", "neko_supervisor", "backend_dev"} <= set(personas)

    frontend = personas["dev"]
    assert frontend.display_name in {"Launcher Dev Agent", "Frontend Dev Agent", "Frontend Dev", "Compatibility Frontend Dev"}
    assert frontend.role == AgentRole.DEV.value
    assert "terminal" in effective_toolsets(frontend)

    backend = personas["backend_dev"]
    assert backend.display_name in {"Backend Dev Agent", "Backend Dev"}
    assert backend.role == AgentRole.DEV.value
    assert backend.hermes_profile == "backend-dev"
    assert "terminal" in effective_toolsets(backend)
    assert "write_file" not in blocked_tool_names(backend)


def test_persona_records_from_config_support_collection_specialists_without_dropping_unknown_dev_like_agent():
    cfg = AgentRuntimeConfig(
        personas={
            "backend_dev": {
                "display_name": "Backend Dev",
                "hermes_profile": "backend-dev",
                "toolsets": ["file", "search", "terminal", "code_execution"],
            },
            "ml_dev": {
                "display_name": "ML Dev",
                "role": "dev",
                "hermes_profile": "ml-dev",
                "toolsets": ["file", "search", "terminal"],
            },
        }
    )

    personas = _by_id(persona_records_from_config(cfg))

    assert personas["backend_dev"].hermes_profile == "backend-dev"
    assert personas["ml_dev"].display_name == "ML Dev"
    assert personas["ml_dev"].role == AgentRole.DEV.value
    assert personas["ml_dev"].hermes_profile == "ml-dev"


def test_specialist_agents_snapshot_is_collection_based_redaction_safe_and_repo_scoped(monkeypatch):
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

    # Base-profile foundation: only `base` is seeded into the store now, so persist the
    # typed specialist personas into this test's isolated store to exercise the snapshot
    # specialist projection (repo scoping, display names). They remain resolvable via the
    # dormant catalog in production but are not seeded/shown by default.
    from agent_runtime.store import AgentStore

    for _persona in persona_records_from_config():
        AgentStore().save(_persona)

    snapshot = build_snapshot()
    agents = {agent["persona_id"]: agent for agent in snapshot["agents"]}

    assert "backend_dev" in agents
    assert agents["dev"]["display_name"] in {"Launcher Dev Agent", "Frontend Dev Agent", "Frontend Dev", "Compatibility Frontend Dev"}
    assert agents["backend_dev"]["display_name"] in {"Backend Dev Agent", "Backend Dev"}
    assert agents["backend_dev"]["hermes_profile"] == "backend-dev"

    repo_scopes = snapshot["repo_scopes"]
    assert repo_scopes["harness"]["label"] == "hermes-agent"
    assert repo_scopes["frontend"]["label"] == "EterniaLauncher"
    assert repo_scopes["backend"]["label"] == "EterniaBackend"
    encoded = repr(snapshot)
    assert "access_token" not in encoded.lower()
    assert "api_key" not in encoded.lower()
    assert "\\.hermes\\profiles" not in encoded
    assert "/.hermes/profiles" not in encoded


def test_backend_dev_repo_grounding_uses_backend_alias_without_raw_path_leakage(tmp_path, monkeypatch):
    backend_root = tmp_path / "eternia-backend"
    launcher_root = tmp_path / "EterniaLauncher"
    backend_root.mkdir()
    launcher_root.mkdir()
    monkeypatch.setitem(repo_context._REPO_ALIAS_PATHS, "eterniabackend", (str(backend_root),))
    monkeypatch.setitem(repo_context._REPO_ALIAS_PATHS, "eternia-backend", (str(backend_root),))
    monkeypatch.setitem(repo_context._REPO_ALIAS_PATHS, "backend", (str(backend_root),))
    monkeypatch.setitem(repo_context._REPO_ALIAS_PATHS, "eternialauncher", (str(launcher_root),))
    monkeypatch.setitem(repo_context._REPO_ALIAS_PATHS, "eternia-launcher", (str(launcher_root),))
    monkeypatch.setitem(repo_context._REPO_ALIAS_PATHS, "launcher", (str(launcher_root),))

    backend = resolve_affected_repo_workdir("EterniaBackend")
    frontend = resolve_affected_repo_workdir("EterniaLauncher")
    harness = resolve_affected_repo_workdir("hermes-agent")

    assert backend is not None
    assert backend.name == "eternia-backend"
    assert frontend is not None
    assert frontend.name == "EterniaLauncher"
    assert harness is not None
    assert harness == Path(__file__).resolve().parents[2]

    labels = safe_affected_repo_labels(["EterniaBackend", "EterniaLauncher", "hermes-agent"])
    assert labels == ["EterniaBackend", "EterniaLauncher", "hermes-agent"]
    assert "Users" not in repr(labels)
    assert "Unreal Engine" not in repr(labels)


def test_backend_dev_explicit_repo_scope_loads_backend_repo_context(tmp_path):
    backend_root = tmp_path / "eternia-backend"
    backend_root.mkdir()
    (backend_root / "CLAUDE.md").write_text("Backend repo guidance", encoding="utf-8")
    backend_dev = _by_id(sample_personas())["backend_dev"]
    backend_dev.repo_scope = str(backend_root)

    ctx = repo_execution_context_for_task(type("Task", (), {"affected_repos": ["EterniaBackend"]})(), explicit_workdir=backend_dev.repo_scope)

    assert ctx is not None
    assert ctx.repo_label == "eternia-backend"
    assert "CLAUDE.md" in ctx.context_files
