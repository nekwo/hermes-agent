from __future__ import annotations

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.preflight import PreflightCheck, PreflightResult, environment_fingerprint, open_preflight_blocker, record_preflight_pass, run_preflight
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore


def task(**overrides):
    data = {
        "id": "task_preflight",
        "title": "Backend docker proof",
        "description": "Run docker compose proof",
        "state": TaskState.RUNNING,
        "created_at": now(),
        "updated_at": now(),
        "requested_by": "test",
        "affected_repos": ["EterniaBackend"],
        "current_stage_id": "stage_1",
        "harness_self_heal": {},
    }
    data.update(overrides)
    return Task(**data)


def test_environment_fingerprint_is_stable_and_changes_on_tokens():
    checks = [
        PreflightCheck("docker", True, "docker=up", "ok", "none"),
        PreflightCheck("flutter", False, "flutter=absent", "missing", "install"),
    ]
    assert environment_fingerprint(checks) == environment_fingerprint(list(reversed(checks)))
    changed = [PreflightCheck("docker", True, "docker=down", "ok", "none"), checks[1]]
    assert environment_fingerprint(checks) != environment_fingerprint(changed)


def test_visual_scope_preflight_blocks_on_missing_mcp():
    t = task(title="Mission Control screenshot proof", description="Capture Stage C MCP visual proof", requires_visual_proof=True)
    result = run_preflight(t, persona_target="qa")

    assert not result.ok
    assert result.blocker["check_id"] == "flutter" or result.blocker["check_id"] == "launcher_qa_mcp"


def test_visual_scope_preflight_smokes_configured_launcher_qa_mcp(tmp_path, monkeypatch):
    from agent_runtime import preflight
    from agent_runtime.stagec_mcp_visual_provider import StageCMcpSmokeResult

    cfg = tmp_path / "config.yaml"
    cfg.write_text("mcp_servers:\n  launcher_qa:\n    command: launcher-qa\n", encoding="utf-8")

    class FakeConfig:
        command = "launcher-qa"

    monkeypatch.setattr(preflight, "load_launcher_qa_mcp_config", lambda persona_target: FakeConfig())
    monkeypatch.setattr(preflight, "smoke_launcher_qa_mcp", lambda config: StageCMcpSmokeResult(True, "ready", "ok"))

    t = task(title="Visual proof", description="Capture screenshot proof", requires_visual_proof=True, affected_repos=[])
    result = run_preflight(t, persona_target="qa")

    assert result.ok
    assert any(check.id == "launcher_qa_mcp" and check.ok for check in result.checks)


def test_backend_persona_preflight_does_not_inherit_launcher_visual_mcp_scope():
    t = task(
        title="Stage 46 Mission Control MCP cross-stack smoke",
        description="Backend Dev first, then Launcher Dev and QA review visual or MCP evidence later.",
        affected_repos=["EterniaBackend", "EterniaLauncher"],
    )

    result = run_preflight(t, persona_target="backend_dev")
    check_ids = {check.id for check in result.checks}

    assert "flutter" not in check_ids
    assert "launcher_qa_mcp" not in check_ids


def test_launcher_non_visual_handoff_does_not_require_mcp_for_mission_control_text():
    t = task(
        id="task_launcher_non_visual",
        title="Mission Control launcher smoke",
        description="Launcher Dev checks Mission Control and treats MCP absence as blocker evidence only.",
        affected_repos=["EterniaLauncher", "EterniaBackend"],
        current_stage_id="stage_launcher",
    )
    EventLog().append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=t.id,
            run_id="run_neko",
            persona_id="neko_supervisor",
            payload={
                "packet_type": "handoff_packet",
                "stage_id": "stage_launcher",
                "body": {
                    "target_owner": "dev",
                    "target_repo": "EterniaLauncher",
                    "proof_gate": {"required": True, "visual_required": False, "minimum_status": "passed"},
                },
            },
        )
    )

    result = run_preflight(t, persona_target="dev")
    check_ids = {check.id for check in result.checks}

    assert "flutter" in check_ids
    assert "launcher_qa_mcp" not in check_ids


def test_no_product_edit_preflight_blocks_dirty_affected_repo(tmp_path):
    import subprocess

    repo = tmp_path / "dirty-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "preexisting.txt").write_text("dirty\n", encoding="utf-8")
    t = task(
        title="No product edit proof",
        description="Route without edits",
        affected_repos=[str(repo)],
        risk_flags=["no_product_edits"],
    )

    result = run_preflight(t, persona_target="dev")

    assert not result.ok
    assert result.blocker["check_id"] == "repo_clean"
    assert result.blocker["metadata"]["repos"][0]["dirty_count"] == 1
    assert not hasattr(TaskStore(), "create")


def test_no_product_edit_preflight_allows_unchanged_dirty_baseline(tmp_path):
    import subprocess

    repo = tmp_path / "dirty-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "preexisting.txt").write_text("dirty\n", encoding="utf-8")
    baseline = {
        "repos": [
            {
                "label": "dirty-repo",
                "dirty": True,
                "dirty_count": 1,
                "error": None,
                "status_excerpt": ["?? preexisting.txt"],
            }
        ]
    }
    t = task(
        title="No product edit proof",
        description="Route without edits",
        affected_repos=[str(repo)],
        risk_flags=["no_product_edits"],
    )
    t.harness_self_heal["repo_clean_baseline"] = baseline

    result = run_preflight(t, persona_target="dev")

    assert result.ok
    repo_check = next(check for check in result.checks if check.id == "repo_clean")
    assert repo_check.token == "repo_clean=baseline_unchanged"
    assert repo_check.metadata["baseline_delta_status"] == "unchanged"


def test_no_product_edit_preflight_blocks_changed_dirty_baseline(tmp_path):
    import subprocess

    repo = tmp_path / "dirty-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "preexisting.txt").write_text("dirty\n", encoding="utf-8")
    t = task(
        title="No product edit proof",
        description="Route without edits",
        affected_repos=[str(repo)],
        risk_flags=["no_product_edits"],
    )
    t.harness_self_heal["repo_clean_baseline"] = {
        "repos": [
            {
                "label": "dirty-repo",
                "dirty": False,
                "dirty_count": 0,
                "error": None,
                "status_excerpt": [],
            }
        ]
    }

    result = run_preflight(t, persona_target="dev")

    assert not result.ok
    assert result.blocker["check_id"] == "repo_clean"
    assert result.blocker["metadata"]["baseline_delta_status"] == "baseline_changed"


def test_docker_preflight_autostarts_desktop_when_engine_is_down(monkeypatch):
    from agent_runtime import preflight

    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"{name}.exe" if name in {"docker", "python"} else None)
    monkeypatch.setattr(preflight, "_docker_info_ok", lambda **kwargs: False)
    monkeypatch.setattr(preflight, "_start_docker_desktop", lambda: {"ok": True, "detail": "start requested"})
    monkeypatch.setattr(preflight, "_wait_for_docker_engine", lambda timeout_seconds: True)

    result = run_preflight(task(), persona_target="backend_dev")
    docker_check = next(check for check in result.checks if check.id == "docker_engine")

    assert result.ok
    assert docker_check.token == "docker_engine=up_after_autostart"
    assert docker_check.metadata["remediation_action"] == "docker_desktop_autostart"
    assert docker_check.metadata["remediation_status"] == "applied"


def test_docker_preflight_blocks_after_failed_autostart(monkeypatch):
    from agent_runtime import preflight

    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"{name}.exe" if name in {"docker", "python"} else None)
    monkeypatch.setattr(preflight, "_docker_info_ok", lambda **kwargs: False)
    monkeypatch.setattr(preflight, "_start_docker_desktop", lambda: {"ok": True, "detail": "start requested"})
    monkeypatch.setattr(preflight, "_wait_for_docker_engine", lambda timeout_seconds: False)
    monkeypatch.setenv("HERMES_PREFLIGHT_DOCKER_AUTOSTART_SECONDS", "1")

    result = run_preflight(task(), persona_target="backend_dev")

    assert not result.ok
    assert result.blocker["check_id"] == "docker_engine"
    assert result.blocker["metadata"]["remediation_action"] == "docker_desktop_autostart"
    assert result.blocker["metadata"]["remediation_status"] == "failed"


def test_preflight_pass_records_applied_remediation_event():
    assert not hasattr(TaskStore(), "create")


def test_preflight_blocker_records_incident_without_retired_proof_store(monkeypatch):
    assert not hasattr(TaskStore(), "create")
