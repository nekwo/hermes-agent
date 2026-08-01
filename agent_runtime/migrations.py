from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import paths
from .config import AgentRuntimeConfig, persona_records_from_config, load_agent_runtime_config, load_root_runtime_config
from .production_envelope import production_envelope_status

CURRENT_RUNTIME_SCHEMA_VERSION = 1


def effective_config_summary(
    cfg: AgentRuntimeConfig | None = None,
    *,
    migration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_agent_runtime_config()
    data = asdict(cfg)
    data["store_root"] = str(paths.store_root())
    data["schema_version"] = int(getattr(cfg, "schema_version", CURRENT_RUNTIME_SCHEMA_VERSION) or CURRENT_RUNTIME_SCHEMA_VERSION)
    data["validation"] = validate_runtime_config(cfg)
    data["migration"] = migration if migration is not None else migration_status()
    data["production_envelope"] = production_envelope_status(cfg)
    data["effective_personas"] = _effective_persona_summary(cfg)
    return _redaction_safe_config(data)


def validate_runtime_config(cfg: AgentRuntimeConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_root_runtime_config()
    errors: list[dict[str, str]] = []

    _positive(errors, "heartbeat_ttl_seconds", cfg.heartbeat_ttl_seconds)
    _positive(errors, "max_actions_per_tick", cfg.max_actions_per_tick)
    _positive(errors, "daemon_interval_seconds", cfg.daemon_interval_seconds)
    _positive(errors, "daemon_idle_interval_seconds", cfg.daemon_idle_interval_seconds)
    _positive(errors, "daemon_heartbeat_seconds", cfg.daemon_heartbeat_seconds)
    if not isinstance(getattr(cfg, "root_node_mode", False), bool):
        errors.append({"field": "root_node_mode", "reason": "must be boolean"})
    _positive(errors, "live_run_max_wall_seconds", cfg.live_run_max_wall_seconds)
    _positive(errors, "live_run_max_api_calls", cfg.live_run_max_api_calls)
    _positive(errors, "live_run_max_total_tokens", cfg.live_run_max_total_tokens)
    _positive(errors, "live_run_iteration_budget", cfg.live_run_iteration_budget)
    _positive(errors, "scope_wait_deadline_seconds", cfg.scope_wait_deadline_seconds)
    _positive(errors, "run_lease_seconds", cfg.run_lease_seconds)
    _positive(errors, "tool_wait_timeout_seconds", cfg.tool_wait_timeout_seconds)
    _positive(errors, "liveness_poll_seconds", cfg.liveness_poll_seconds)
    _positive(errors, "liveness_quiet_strikes", cfg.liveness_quiet_strikes)
    _positive(errors, "liveness_hung_seconds", cfg.liveness_hung_seconds)
    _positive(errors, "child_progress_min_interval_seconds", cfg.child_progress_min_interval_seconds)
    _positive(errors, "deploy_timeout_seconds", cfg.deploy_timeout_seconds)
    _positive(errors, "lock_acquire_timeout_seconds", cfg.lock_acquire_timeout_seconds)
    _positive(errors, "mission_max_total_tokens", cfg.mission_max_total_tokens)
    _positive(errors, "mission_wall_clock_deadline_seconds", cfg.mission_wall_clock_deadline_seconds)
    _positive(errors, "neko_recovery_attempt_cap", cfg.neko_recovery_attempt_cap)
    _positive(errors, "neko_extension_cap", cfg.neko_extension_cap)
    _positive(errors, "artifact_storage_low_watermark_mb", cfg.artifact_storage_low_watermark_mb)
    _positive(errors, "artifact_storage_high_watermark_mb", cfg.artifact_storage_high_watermark_mb)
    _positive(errors, "artifact_storage_critical_watermark_mb", cfg.artifact_storage_critical_watermark_mb)
    crs = getattr(cfg, "continuous_role_sessions", None)
    if crs is not None:
        _positive(errors, "continuous_role_sessions.max_decisions_per_envelope", crs.max_decisions_per_envelope)
        _positive(errors, "continuous_role_sessions.max_proofs_per_envelope", crs.max_proofs_per_envelope)
        _positive(errors, "continuous_role_sessions.max_continuations_per_stage", crs.max_continuations_per_stage)
    ews = getattr(cfg, "enterprise_worker_sessions", None)
    if ews is not None:
        if ews.mode not in {"observe_only", "enforce"}:
            errors.append({"field": "enterprise_worker_sessions.mode", "reason": "must be observe_only or enforce"})
        if ews.static_prompt_strategy not in {"capability_detect", "always_send", "receipt_only"}:
            errors.append({"field": "enterprise_worker_sessions.static_prompt_strategy", "reason": "invalid static prompt strategy"})
        _positive(errors, "enterprise_worker_sessions.worker_heartbeat_seconds", ews.worker_heartbeat_seconds)
        _positive(errors, "enterprise_worker_sessions.worker_stale_seconds", ews.worker_stale_seconds)
        _positive(errors, "enterprise_worker_sessions.possession_lease_seconds", ews.possession_lease_seconds)
        _positive(errors, "enterprise_worker_sessions.max_same_worker_repairs_per_stage", ews.max_same_worker_repairs_per_stage)
        _positive(errors, "enterprise_worker_sessions.max_worker_context_compressions_per_goal", ews.max_worker_context_compressions_per_goal)
    nwf = getattr(cfg, "normal_worker_flow", None)
    if nwf is not None:
        _positive(errors, "normal_worker_flow.max_self_test_repeats_without_change", nwf.max_self_test_repeats_without_change)
    # S47 removed the five ``role_envelope.*`` range validators with the config
    # block they guarded — S44 had already deleted every reader of those knobs.
    swarm = getattr(cfg, "swarm", None)
    if swarm is not None:
        for field in (
            "max_active_lanes",
            "global_token_soft_limit",
            "global_token_hard_limit",
            "global_api_call_soft_limit",
            "global_api_call_hard_limit",
            "per_lane_token_limit",
            "per_lane_api_call_limit",
        ):
            _positive(errors, f"swarm.{field}", getattr(swarm, field))
        if swarm.global_token_soft_limit > swarm.global_token_hard_limit:
            errors.append({"field": "swarm.global_token_*_limit", "reason": "soft limit must be <= hard limit"})
        if swarm.global_api_call_soft_limit > swarm.global_api_call_hard_limit:
            errors.append({"field": "swarm.global_api_call_*_limit", "reason": "soft limit must be <= hard limit"})

    if cfg.live_run_max_total_tokens > cfg.mission_max_total_tokens:
        errors.append({
            "field": "mission_max_total_tokens",
            "reason": "mission token ceiling must be >= per-run token ceiling",
        })
    if int(getattr(cfg, "liveness_poll_seconds", 0) or 0) < 30 or int(getattr(cfg, "liveness_poll_seconds", 0) or 0) > 120:
        errors.append({"field": "liveness_poll_seconds", "reason": "must be between 30 and 120"})
    if int(getattr(cfg, "liveness_hung_seconds", 0) or 0) >= int(getattr(cfg, "heartbeat_ttl_seconds", 0) or 0):
        errors.append({"field": "liveness_hung_seconds", "reason": "must be less than heartbeat_ttl_seconds"})
    if not (
        cfg.artifact_storage_low_watermark_mb
        <= cfg.artifact_storage_high_watermark_mb
        <= cfg.artifact_storage_critical_watermark_mb
    ):
        errors.append({
            "field": "artifact_storage_*_watermark_mb",
            "reason": "storage watermarks must be ordered low <= high <= critical",
        })
    version = int(getattr(cfg, "schema_version", CURRENT_RUNTIME_SCHEMA_VERSION) or CURRENT_RUNTIME_SCHEMA_VERSION)
    if version != CURRENT_RUNTIME_SCHEMA_VERSION:
        errors.append({"field": "schema_version", "reason": f"unsupported runtime schema version {version}"})

    # Additive, non-fatal: flag an ``agent_runtime.default_model`` that shadows or
    # duplicates the top-level ``model.default`` authority. Warnings never flip
    # ``ok`` — a deliberate harness-wide override is valid, just worth surfacing.
    warnings = _runtime_default_warnings()

    return {"ok": not errors, "errors": errors, "warnings": warnings, "schema_version": CURRENT_RUNTIME_SCHEMA_VERSION}


def _runtime_default_warnings() -> list[dict[str, str]]:
    from .config import describe_runtime_default_authority

    try:
        authority = describe_runtime_default_authority()
    except Exception:
        return []
    warnings: list[dict[str, str]] = []
    override = authority.get("harness_override", {})
    top = authority.get("top_level", {})
    if override.get("model_state") == "shadowing":
        warnings.append({
            "field": "agent_runtime.default_model",
            "reason": (
                f"shadows model.default ({override.get('model')} vs {top.get('model')}) — "
                "agents run the agent_runtime override, not the model you set; "
                "remove it unless the harness is deliberately pinned"
            ),
        })
    elif override.get("model_state") == "redundant":
        warnings.append({
            "field": "agent_runtime.default_model",
            "reason": (
                "duplicates model.default and is unmaintained by any write path — "
                "remove it so the single runtime-default authority stays single"
            ),
        })
    return warnings


def migration_status(root: Path | None = None) -> dict[str, Any]:
    root = root or paths.store_root()
    counts = {
        "tasks": _count_json(root / "tasks"),
        "runs": _count_json(root / "runs"),
        "worker_sessions": _count_json(root / "worker_sessions"),
        "repo_bundles": _count_nested_json(root / "repo_bundles"),
        "agents": _count_json(root / "agents"),
        "incidents": _count_json(root / "incidents"),
        "proofs": _count_nested_json(root / "proofs"),
        "self_tests": _count_nested_json(root / "self_tests"),
        "archive_batches": _count_dirs(root / "deleted_archive"),
    }
    return {
        "current_schema_version": CURRENT_RUNTIME_SCHEMA_VERSION,
        "pending": False,
        "safe_to_run": True,
        "store_root": str(root),
        "counts": counts,
        "actions": [],
    }


def _positive(errors: list[dict[str, str]], field: str, value: Any) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append({"field": field, "reason": "must be numeric"})
        return
    if number <= 0:
        errors.append({"field": field, "reason": "must be positive"})


def _count_json(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.glob("*.json") if item.is_file())


def _count_nested_json(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*.json") if item.is_file())


def _count_dirs(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.iterdir() if item.is_dir())


def _redaction_safe_config(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if any(marker in lowered for marker in ("secret", "token", "password", "credential", "api_key")):
            result[key] = "<redacted>"
        elif key == "personas" and isinstance(value, dict):
            result[key] = {str(pid): _redaction_safe_config(raw) if isinstance(raw, dict) else raw for pid, raw in value.items()}
        else:
            result[key] = value
    return result


def _effective_persona_summary(cfg: AgentRuntimeConfig) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for persona in persona_records_from_config(cfg):
        summary[persona.id] = {
            "role": persona.role,
            "hermes_profile": persona.hermes_profile,
            "skills": list(persona.skills or []),
            "required_mcp_servers": list(persona.required_mcp_servers or []),
        }
    return summary
