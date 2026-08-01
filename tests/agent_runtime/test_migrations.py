from __future__ import annotations

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.migrations import effective_config_summary, migration_status, validate_runtime_config
from utils import atomic_json_write


def test_the_token_ceiling_cross_check_is_gone_with_both_its_fields():
    """INVERTED at S57 (was ``test_validate_runtime_config_rejects_bad_ceiling_order``).

    The check related ``live_run_max_total_tokens`` to ``mission_max_total_tokens``.
    NEITHER had a production reader — no run opener enforced the first, no
    enforcer consulted the second — so the cross-check made two dead knobs look
    like a governed budget pair. Both fields and the arm went at S57. The pin
    inverts: constructing a config with the old "bad" ordering is not expressible
    any more, and validation reports no such error.
    """
    for retired in ("live_run_max_total_tokens", "mission_max_total_tokens"):
        assert not hasattr(AgentRuntimeConfig(), retired), retired

    result = validate_runtime_config(AgentRuntimeConfig())

    assert result["ok"] is True
    assert not any(item["field"] == "mission_max_total_tokens" for item in result["errors"])


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


def test_the_storage_watermark_ordering_check_is_gone_with_its_three_fields():
    """INVERTED at S57 (was ``test_validate_runtime_config_rejects_bad_storage_watermark_order``).

    Same shape as the token-ceiling case: three watermarks no sweeper reads,
    ordered against each other by a validator, which is how an unimplemented
    artifact-storage policy looked configured for months. All three fields and
    the ordering arm went at S57.
    """
    for retired in (
        "artifact_storage_low_watermark_mb",
        "artifact_storage_high_watermark_mb",
        "artifact_storage_critical_watermark_mb",
    ):
        assert not hasattr(AgentRuntimeConfig(), retired), retired

    result = validate_runtime_config(AgentRuntimeConfig())

    assert not any(
        item["field"] == "artifact_storage_*_watermark_mb" for item in result["errors"]
    )


def test_effective_config_summary_is_redaction_safe(isolate_agent_runtime_root):
    cfg = AgentRuntimeConfig(personas={"dev": {"api_token": "super-secret-value", "model": "gpt"}})

    summary = effective_config_summary(cfg)

    assert summary["validation"]["ok"] is True
    assert "super-secret-value" not in str(summary)
    assert summary["personas"]["dev"]["api_token"] == "<redacted>"
    # S11 removed bundled role/persona defaults: configured data is authoritative.
    assert summary["effective_personas"]["dev"]["skills"] == []
    # S56 deleted `production_envelope.py` whole: the envelope was hand-written
    # prose keyed on config flags, several of its claims false against this tree.
    # The pin inverts rather than being dropped so a stale producer cannot
    # resurrect a reader. (The three H5/H6-H9/swarm-ceiling cases that had the
    # envelope as their ONLY subject were deleted with it.)
    assert "production_envelope" not in summary


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
