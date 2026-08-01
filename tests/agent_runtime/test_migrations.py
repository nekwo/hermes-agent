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


def test_validate_runtime_config_warns_on_shadowing_override(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "agent_runtime:\n"
        "  default_model: gpt-5.5\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_runtime.config.get_config_path", lambda: p)

    result = validate_runtime_config(AgentRuntimeConfig())

    # Warnings never flip ok; they surface the stale-pin divergence.
    assert result["ok"] is True
    assert any(
        w["field"] == "agent_runtime.default_model" and "shadows" in w["reason"]
        for w in result["warnings"]
    )


def test_validate_runtime_config_no_warning_when_authority_is_clean(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("model:\n  default: gpt-5.6-luna\n", encoding="utf-8")
    monkeypatch.setattr("agent_runtime.config.get_config_path", lambda: p)

    result = validate_runtime_config(AgentRuntimeConfig())

    assert result["ok"] is True
    assert result["warnings"] == []


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
    # S11 removed bundled role/persona defaults: configured data is authoritative.
    assert summary["effective_personas"]["dev"]["skills"] == []
    assert summary["production_envelope"]["production_ready"] is True
    assert {item["id"] for item in summary["production_envelope"]["items"]} >= {"H5", "H6", "H7", "H8", "H9", "H10", "recursive_supervision"}
    assert summary["production_envelope"]["blockers"] == []


def test_h5_envelope_advertises_behavioral_migration_and_rollback_controls():
    summary = effective_config_summary(
        AgentRuntimeConfig(simplified_agent_contract=SimplifiedAgentContractConfig(enabled=True))
    )
    h5 = next(item for item in summary["production_envelope"]["items"] if item["id"] == "H5")

    assert h5["status"] == "implemented"
    assert not h5["blockers"]
    assert any("collapsed hand_off/block/escalate/scope_route/qa_verdict" in control for control in h5["controls"])
    assert not any("proof-from-trace" in control for control in h5["controls"])
    assert any("hand_off captures the grounded isolated-worktree diff" in control for control in h5["controls"])
    assert any("legacy decision aliases are pruned" in control for control in h5["controls"])
    assert any("disable simplified_agent_contract.enabled" in control for control in h5["controls"])


def test_h6_h8_h9_envelope_names_real_enforcement_controls():
    summary = effective_config_summary(AgentRuntimeConfig())
    items = {item["id"]: item for item in summary["production_envelope"]["items"]}

    assert summary["production_envelope"]["production_ready"] is True
    assert items["H6"]["status"] == "implemented"
    assert not items["H6"]["blockers"]
    # INVERTED at S49 (2026-08-01). These two lines asserted the H6 item still
    # ADVERTISED the audited worker.takeover workflow and its approve_destructive
    # gate. Both claims described `operator_control.py`, which was deleted whole
    # for want of a production caller -- so an envelope that still named them
    # would be telling an operator about a control they cannot invoke. The pin is
    # inverted, not dropped: H6 must keep its REAL controls and must not regrow
    # the retired ones.
    assert not any("takeover" in control for control in items["H6"]["controls"])
    assert not any("approve_destructive" in control for control in items["H6"]["controls"])
    assert any("worker.pause" in control for control in items["H6"]["controls"])
    assert items["H8"]["status"] == "implemented"
    assert any("heartbeat TTL" in control for control in items["H8"]["controls"])
    assert any("terminal-idempotent" in control for control in items["H8"]["controls"])
    assert items["H9"]["status"] == "implemented"
    assert any("swarm hard token ceilings block" in control for control in items["H9"]["controls"])
    assert any("repo bundle queueing" in control for control in items["H9"]["controls"])


def test_swarm_ceiling_controls_disclose_gating_when_swarm_disabled():
    # Honesty guard: the swarm hard-token-ceiling controls (H7/H9) must not read
    # as active enforcement when swarm.enabled is False. With swarm off they must
    # disclose the gate; with swarm on they read as active (no gated caveat).
    from agent_runtime.runtime_config import SwarmConfig

    off = effective_config_summary(AgentRuntimeConfig())
    items_off = {item["id"]: item for item in off["production_envelope"]["items"]}
    for hid in ("H7", "H9"):
        ceiling = [c for c in items_off[hid]["controls"] if "swarm hard token ceilings" in c]
        assert ceiling, hid
        assert all("swarm.enabled" in c and "gated off" in c for c in ceiling), (hid, ceiling)

    on = effective_config_summary(AgentRuntimeConfig(swarm=SwarmConfig(enabled=True)))
    items_on = {item["id"]: item for item in on["production_envelope"]["items"]}
    for hid in ("H7", "H9"):
        ceiling = [c for c in items_on[hid]["controls"] if "swarm hard token ceilings" in c]
        assert ceiling, hid
        assert all("gated off" not in c for c in ceiling), (hid, ceiling)


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
