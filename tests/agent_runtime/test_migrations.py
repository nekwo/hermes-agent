from __future__ import annotations

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.migrations import effective_config_summary, migration_status, validate_runtime_config
from agent_runtime.runtime_config import SimplifiedAgentContractConfig
from utils import atomic_json_write


def test_validate_runtime_config_rejects_bad_ceiling_order():
    cfg = AgentRuntimeConfig(live_run_max_total_tokens=500, mission_max_total_tokens=100)

    result = validate_runtime_config(cfg)

    assert result["ok"] is False
    assert any(item["field"] == "mission_max_total_tokens" for item in result["errors"])


def test_validate_runtime_config_rejects_bad_storage_watermark_order():
    cfg = AgentRuntimeConfig(
        artifact_storage_low_watermark_mb=100,
        artifact_storage_high_watermark_mb=50,
        artifact_storage_critical_watermark_mb=200,
    )

    result = validate_runtime_config(cfg)

    assert result["ok"] is False
    assert any(item["field"] == "artifact_storage_*_watermark_mb" for item in result["errors"])


def test_effective_config_summary_is_redaction_safe(isolate_agent_runtime_root):
    cfg = AgentRuntimeConfig(personas={"dev": {"api_token": "super-secret-value", "model": "gpt"}})

    summary = effective_config_summary(cfg)

    assert summary["validation"]["ok"] is True
    assert "super-secret-value" not in str(summary)
    assert summary["personas"]["dev"]["api_token"] == "<redacted>"
    assert "harness-dev-delivery" in summary["effective_personas"]["dev"]["skills"]
    assert summary["production_envelope"]["production_ready"] is False
    assert {item["id"] for item in summary["production_envelope"]["items"]} == {"H5", "H6", "H7", "H8", "H9", "H10"}
    assert not any(item["id"] == "H5" for item in summary["production_envelope"]["blockers"])


def test_h5_envelope_advertises_behavioral_migration_and_rollback_controls():
    summary = effective_config_summary(
        AgentRuntimeConfig(simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True))
    )
    h5 = next(item for item in summary["production_envelope"]["items"] if item["id"] == "H5")

    assert h5["status"] == "implemented"
    assert not h5["blockers"]
    assert any("controls simplified HUD/worker-action contract exposure" in control for control in h5["controls"])
    assert any("compatibility shim" in control for control in h5["controls"])
    assert any("disable simplified_agent_contract.enabled" in control for control in h5["controls"])


def test_migration_status_counts_existing_runtime_records(isolate_agent_runtime_root):
    root = isolate_agent_runtime_root
    atomic_json_write(root / "tasks" / "task_1.json", {"id": "task_1", "schema_version": 1})
    atomic_json_write(root / "runs" / "run_1.json", {"id": "run_1", "schema_version": 1})
    atomic_json_write(root / "proofs" / "task_1" / "proof_p1.json", {"id": "p1", "schema_version": 1})
    (root / "deleted_archive" / "batch_1").mkdir(parents=True)

    status = migration_status(root)

    assert status["pending"] is False
    assert status["counts"]["tasks"] == 1
    assert status["counts"]["runs"] == 1
    assert status["counts"]["proofs"] == 1
    assert status["counts"]["archive_batches"] == 1
