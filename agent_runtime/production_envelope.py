from __future__ import annotations

from typing import Any


def production_envelope_status(cfg: Any) -> dict[str, Any]:
    """Machine-readable H5-H10 readiness for decision/HUD simplification.

    The production envelope intentionally distinguishes implemented controls
    from flag-gated stubs. This prevents an operator from treating a simplified
    HUD run as production-swarm ready when larger migration, durability, or
    scheduler work is only scaffolded.
    """

    items = [
        _h5_migration_rollback(cfg),
        _h6_operator_control(cfg),
        _h7_cost_fanout(cfg),
        _h8_durability(cfg),
        _h9_scheduling(cfg),
        _h10_observation_tests(cfg),
    ]
    blockers = [
        {
            "id": item["id"],
            "title": item["title"],
            "status": item["status"],
            "blockers": item["blockers"],
        }
        for item in items
        if item["status"] != "implemented" or item["blockers"]
    ]
    return {
        "schema_version": 1,
        "production_ready": not blockers,
        "items": items,
        "blockers": blockers,
    }


def _h5_migration_rollback(cfg: Any) -> dict[str, Any]:
    simplified = getattr(cfg, "simplified_agent_contract", None)
    return _item(
        "H5",
        "migration_rollback",
        "not_implemented",
        controls=[
            "simplified_agent_contract.enabled currently only opts into repo-bundle routing compatibility",
            "simplified_agent_contract subflags are parsed for future migration wiring but do not gate contract exposure",
        ],
        flags={
            "simplified_agent_contract.enabled": bool(getattr(simplified, "enabled", False)),
            "expose_only_simplified_actions": bool(getattr(simplified, "expose_only_simplified_actions", False)),
            "allow_legacy_decision_aliases": bool(getattr(simplified, "allow_legacy_decision_aliases", False)),
            "keep_internal_state_machine": bool(getattr(simplified, "keep_internal_state_machine", False)),
        },
        blockers=[
            "simplified_agent_contract subflags have no behavioral consumer for old-vs-new contract exposure",
            "dual-run old/new decision renderer parity is not yet enforced as a release gate",
            "automatic rollback for in-flight goals is not yet wired to a migration version switch",
        ],
    )


def _h6_operator_control(cfg: Any) -> dict[str, Any]:
    permissions = getattr(cfg, "coordinator_permissions", None)
    return _item(
        "H6",
        "operator_control",
        "flag_gated",
        controls=[
            "worker.pause and worker.resume capabilities are registered",
            "GoalRuntimeInstanceStore supports lane park/resume",
            "daemon stop/kill paths exist for foreground runtime control",
            "irreversible/prod actions require explicit approval through command safety and promotion gates",
        ],
        flags={
            "coordinator_permissions.max_spawns": int(getattr(permissions, "max_spawns", 0) or 0),
            "coordinator_permissions.may_kill_own": bool(getattr(permissions, "may_kill_own", False)),
            "coordinator_permissions.may_kill_others": bool(getattr(permissions, "may_kill_others", False)),
        },
        blockers=[
            "human takeover is exposed as control primitives but not yet a single audited takeover workflow",
        ],
    )


def _h7_cost_fanout(cfg: Any) -> dict[str, Any]:
    swarm = getattr(cfg, "swarm", None)
    return _item(
        "H7",
        "cost_fanout_governance",
        "implemented",
        controls=[
            "per-run wall/API/token budgets are validated",
            "mission token ceilings are enforced before opening another persona run",
            "budget incidents route to bounded Neko continuation or scope recovery",
            "swarm hard token ceilings are enforced before opening another persona run",
            "swarm soft/hard API and token budgets are surfaced in status",
        ],
        flags={
            "swarm.enabled": bool(getattr(swarm, "enabled", False)),
            "swarm.requires_certification": bool(getattr(swarm, "requires_certification", True)),
            "max_active_lanes": int(getattr(swarm, "max_active_lanes", 0) or 0),
        },
    )


def _h8_durability(cfg: Any) -> dict[str, Any]:
    return _item(
        "H8",
        "durability_crash_recovery",
        "flag_gated",
        controls=[
            "events, runs, tasks, proofs, role envelopes, worker sessions, and runtime instances are file-backed",
            "archive preserves task/run/proof/context evidence instead of deleting artifacts",
            "stale runs are marked and recoverable through the ticker",
        ],
        flags={
            "daemon_enabled": bool(getattr(cfg, "daemon_enabled", False)),
            "run_lease_seconds": int(getattr(cfg, "run_lease_seconds", 0) or 0),
        },
        blockers=[
            "idempotent hand-off replay after process crash is not yet certified end to end",
            "event-sourced rebuild is partial; JSON stores remain the primary state source",
        ],
    )


def _h9_scheduling(cfg: Any) -> dict[str, Any]:
    swarm = getattr(cfg, "swarm", None)
    return _item(
        "H9",
        "multi_goal_scheduling_backpressure",
        "flag_gated",
        controls=[
            "foreground runtime hygiene parks conflicting open work",
            "repo bundle queueing gates dependent launcher/backend handoffs",
            "repo locks and swarm budget summaries are surfaced in status",
        ],
        flags={
            "swarm.enabled": bool(getattr(swarm, "enabled", False)),
            "max_active_lanes": int(getattr(swarm, "max_active_lanes", 0) or 0),
        },
        blockers=[
            "production multi-goal scheduler is not enabled until swarm certification passes",
        ],
    )


def _h10_observation_tests(cfg: Any) -> dict[str, Any]:
    return _item(
        "H10",
        "observation_path_test_strategy",
        "implemented",
        controls=[
            "delivery work_status normalization is unit-tested",
            "trace-observed proof and authoritative gate lanes are unit-tested",
            "repo baseline diff exclusion and worktree isolation are unit-tested",
            "terminal safety and test-tampering fail-closed paths are unit-tested",
            "live blueprint smoke evidence is archived by task id for operator inspection",
        ],
    )


def _item(
    item_id: str,
    title: str,
    status: str,
    *,
    controls: list[str],
    flags: dict[str, Any] | None = None,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title,
        "status": status,
        "controls": controls,
        "flags": flags or {},
        "blockers": blockers or [],
    }
