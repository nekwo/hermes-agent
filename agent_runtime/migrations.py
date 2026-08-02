from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import paths
from .config import AgentRuntimeConfig, persona_records_from_config, load_agent_runtime_config, load_root_runtime_config

CURRENT_RUNTIME_SCHEMA_VERSION = 1


def effective_config_summary(
    cfg: AgentRuntimeConfig | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_agent_runtime_config()
    data = asdict(cfg)
    data["store_root"] = str(paths.store_root())
    data["schema_version"] = int(getattr(cfg, "schema_version", CURRENT_RUNTIME_SCHEMA_VERSION) or CURRENT_RUNTIME_SCHEMA_VERSION)
    data["validation"] = validate_runtime_config(cfg)
    data["effective_personas"] = _effective_persona_summary(cfg)
    return _redaction_safe_config(data)


def validate_runtime_config(cfg: AgentRuntimeConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_root_runtime_config()
    errors: list[dict[str, str]] = []

    _positive(errors, "lock_acquire_timeout_seconds", cfg.lock_acquire_timeout_seconds)
    # S47 removed the five ``role_envelope.*`` range validators with the config
    # block they guarded — S44 had already deleted every reader of those knobs.
    # S56 removed the rest of that class in one pass: the three
    # ``continuous_role_sessions.*``, the seven ``enterprise_worker_sessions.*``,
    # the one ``normal_worker_flow.*`` and the nine ``swarm.*`` validators. A
    # range check on a field no code path reads validates nothing — it only
    # makes a dead knob look governed. The blocks themselves are gone; these
    # arms would now range-check attributes that do not exist.
    #
    # S57 finished the class for the SCALARS. Twenty-six ``_positive`` arms, the
    # ``root_node_mode`` bool check, and FOUR cross-field checks went with the 29
    # fields they guarded: the ``live_run_max_total_tokens`` <=
    # ``mission_max_total_tokens`` soft/hard ceiling, the
    # ``liveness_poll_seconds`` 30..120 range, the ``liveness_hung_seconds`` <
    # ``heartbeat_ttl_seconds`` ordering, and the low <= high <= critical
    # ``artifact_storage_*`` ordering. Each related two dead knobs to each other,
    # which is the same "looks governed" illusion one level up. What is left is
    # the ONE scalar with a real reader (``locks.py`` reads
    # ``lock_acquire_timeout_seconds``) plus the schema-version check.

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
        # S57 removed ``"repo_bundles"``. It was a real on-disk count, not a
        # constant literal -- but it counted a store class whose LAST writer went
        # at S52, whose four status projections went at S56, whose checkpoint
        # EntityClass row went with them, and whose module + model + path helper
        # are deleted in this commit. The live root has no ``repo_bundles/``
        # directory at all, so the wire reported ``0`` by construction. Counting
        # a store the runtime can no longer name is the S47 item-5 class wearing
        # a filesystem read as a disguise; the honest move is to retire the row
        # rather than leave an operator a counter that can only say zero. This
        # edits the emitted frame, which is why it rides the same 47 -> 48
        # contract move as the config-scalar cut.
        "agents": _count_json(root / "agents"),
        "incidents": _count_json(root / "incidents"),
        "proofs": _count_nested_json(root / "proofs"),
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
