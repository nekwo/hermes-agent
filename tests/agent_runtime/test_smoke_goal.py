import os

import pytest

from agent_runtime.smoke import run_smoke


def test_no_model_smoke_runs_in_temp_root_and_finishes_done(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)

    result = run_smoke(temp_root=True, no_model=True)

    assert result["ok"] is True
    assert result["first_action"] == "run_slot"
    assert result["final_action"] == "complete_task"
    assert result["final_state"] == "done"
    assert result["transitions"] == [
        "neko_supervisor:scope_route",
        "backend_dev:hand_off",
        "dev:hand_off",
    ]
    assert result["proof_ids"] == ["proof_smoke_backend", "proof_smoke_launcher"]


def test_no_model_smoke_restores_runtime_root_when_smoke_fails(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", "before_runtime")

    def fail_attach(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("agent_runtime.smoke.ProofStore.attach", fail_attach)

    with pytest.raises(RuntimeError, match="boom"):
        run_smoke(temp_root=True, no_model=True)

    assert os.environ["HERMES_AGENT_RUNTIME_ROOT"] == "before_runtime"


def test_live_smoke_without_no_model_returns_truthful_readiness_result():
    result = run_smoke(temp_root=True, no_model=False)

    assert result["ok"] is False
    assert result["mode"] == "live_model"
    assert result["failure_class"] == "live_model_smoke_not_implemented"
    assert "intervention" in result
