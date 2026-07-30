from __future__ import annotations

from typing import Any, Callable

from hermes_time import now

from .delivery_directive import reap_orphan_worktrees
from .events import EventLog, event_log_health
from .snapshot import build_snapshot


DEFAULT_WORKTREE_MIN_AGE_SECONDS = 3600


def run_harness_doctor(
    *,
    fix: bool = False,
    dry_run: bool = False,
    worktree_min_age_seconds: int = DEFAULT_WORKTREE_MIN_AGE_SECONDS,
    include_worktrees: bool = True,
    event_log: EventLog | None = None,
    snapshot_builder: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Report surviving chat-runtime health without reviving mission records.

    Checks: orphan worktrees, snapshot null-id rows, event-log health, model
    authority, and persona/profile binding. The mission-era threshold/store
    parameters and the event-compaction switch were removed with the mission
    lane (doc 16); the CLI stopped passing them in 126976088.
    """

    ref = now()
    event_log = event_log or EventLog()
    snapshot_builder = snapshot_builder or build_snapshot
    if include_worktrees:
        worktrees = reap_orphan_worktrees(
            min_age_seconds=max(0, int(worktree_min_age_seconds or 0)),
            event_log=event_log,
            dry_run=not fix or dry_run,
        )
    else:
        worktrees = {
            "reaped": [],
            "kept": [],
            "dry_run": True,
            "skipped": "worktree_scan_disabled",
        }

    snapshot_defects = _snapshot_null_id_defects(snapshot_builder)
    event_health = event_log_health()
    finding_counts = {
        "orphan_worktrees": len(worktrees.get("reaped") or []),
        "snapshot_null_id_rows": len(snapshot_defects),
    }
    repairs = {
        "worktrees_reaped": (
            [item.get("worktree") for item in worktrees.get("reaped", []) if item.get("worktree")]
            if fix and not dry_run
            else []
        ),
        "dry_run": bool(dry_run),
    }
    return {
        "schema_version": 2,
        "generated_at": ref,
        "ok": True,
        "mode": {"fix": bool(fix), "dry_run": bool(dry_run)},
        "thresholds": {
            "worktree_min_age_seconds": int(worktree_min_age_seconds),
            "include_worktrees": bool(include_worktrees),
        },
        "summary": {
            "finding_counts": finding_counts,
            "needs_fix": any(finding_counts.values()),
            "repairs_applied": bool(fix and not dry_run),
            "preserved_evidence": True,
            "product_repos_modified": False,
        },
        "findings": {
            "orphan_worktrees": worktrees,
            "snapshot_null_id_rows": snapshot_defects,
            "event_log": event_health,
        },
        "model_authority": _model_authority_report(),
        "persona_binding": _persona_binding_report(),
        "repairs": repairs,
    }


def _persona_binding_report() -> dict[str, Any]:
    try:
        from .persona_profile_binding import binding_index

        index = binding_index()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:320], "diverged": []}
    diverged = [binding.as_row() for binding in index.values() if binding.diverged]
    return {
        "ok": True,
        "resolved_by": "store_wins",
        "agent_count": len(index),
        "diverged_count": len(diverged),
        "diverged": sorted(diverged, key=lambda row: row["persona_id"]),
        "remediation": "harness agent set-profile <persona_id> --profile <name> (moves the store) or edit config.yaml (moves the declaration)",
    }


def _model_authority_report() -> dict[str, Any]:
    from .config import describe_runtime_default_authority

    try:
        authority = describe_runtime_default_authority()
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    override = authority.get("harness_override", {})
    pins = authority.get("persona_pins", []) or []
    redundant_pins = [p for p in pins if p.get("matches_runtime_default") is True]
    provider_only_pins = [p for p in pins if p.get("provider_pinned_without_model")]
    notices: list[str] = []
    if override.get("model_state") == "shadowing":
        notices.append(
            f"agent_runtime.default_model ({override.get('model')}) shadows the runtime default from model.default"
        )
    elif override.get("model_state") == "redundant":
        notices.append("agent_runtime.default_model duplicates model.default and is unmaintained")
    if redundant_pins:
        notices.append(
            "persona pins duplicate the runtime default: "
            + ", ".join(sorted(p.get("persona_id", "?") for p in redundant_pins))
        )
    if provider_only_pins:
        notices.append(
            "persona provider pinned without a model: "
            + ", ".join(sorted(p.get("persona_id", "?") for p in provider_only_pins))
        )
    return {
        "available": True,
        "resolved": authority.get("resolved", {}),
        "top_level": authority.get("top_level", {}),
        "harness_override": override,
        "persona_pins": pins,
        "divergent": override.get("model_state") == "shadowing",
        "notices": notices,
    }


def _snapshot_null_id_defects(snapshot_builder: Callable[[], dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        snapshot = snapshot_builder()
    except Exception as exc:
        return [{"collection": "snapshot", "index": None, "id_key": None, "reason": type(exc).__name__}]
    expected = {
        "agents": "persona_id",
        "persona_instances": "persona_instance_id",
    }
    defects: list[dict[str, Any]] = []
    for collection, id_key in expected.items():
        rows = snapshot.get(collection)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if isinstance(row, dict) and not row.get(id_key):
                defects.append({"collection": collection, "index": index, "id_key": id_key})
    return defects
