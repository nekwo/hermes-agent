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
        _recursive_supervision(cfg),
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
        "implemented",
        controls=[
            "simplified_agent_contract.enabled activates the collapsed hand_off/block/escalate/scope_route/qa_verdict agent-facing contract",
            "expose_only_simplified_actions removes legacy delivery and proof-request payload fill surfaces from the HUD",
            "agent terminal self-tests are retained as observational evidence",
            "hand_off captures the grounded isolated-worktree diff and then runs the stage proof recipe/test plan as the authoritative gate",
            "legacy decision aliases are pruned; simplified contracts accept only collapsed signal decisions",
            "keep_internal_state_machine preserves the old deterministic executor behind the simplified action surface",
            "rollback is operator-safe: disable simplified_agent_contract.enabled to restore closed-choice/normal-worker-flow exposure for in-flight goals",
            "HUD exposes decision_contract_migration so old/new contract mode and rollback state are observable",
        ],
        flags={
            "simplified_agent_contract.enabled": bool(getattr(simplified, "enabled", False)),
            "expose_only_simplified_actions": bool(getattr(simplified, "expose_only_simplified_actions", False)),
            "keep_internal_state_machine": bool(getattr(simplified, "keep_internal_state_machine", False)),
        },
    )


def _h6_operator_control(cfg: Any) -> dict[str, Any]:
    permissions = getattr(cfg, "coordinator_permissions", None)
    return _item(
        "H6",
        "operator_control",
        "implemented",
        controls=[
            "worker.pause and worker.resume capabilities are registered",
            "GoalRuntimeInstanceStore supports lane park/resume",
            "daemon stop/kill paths exist for foreground runtime control",
            "worker.takeover is a single audited operator workflow: it parks active lanes, pauses peer workers, possesses the target under a lease, and emits operator.takeover.* events",
            "takeover cancellation of an active run is destructive and requires approve_destructive; without it the run remains alive and an approval_required event is recorded",
            "irreversible/prod actions require explicit approval through command safety and promotion gates",
        ],
        flags={
            "coordinator_permissions.max_spawns": int(getattr(permissions, "max_spawns", 0) or 0),
            "coordinator_permissions.may_kill_own": bool(getattr(permissions, "may_kill_own", False)),
            "coordinator_permissions.may_kill_others": bool(getattr(permissions, "may_kill_others", False)),
        },
    )


def _h7_cost_fanout(cfg: Any) -> dict[str, Any]:
    swarm = getattr(cfg, "swarm", None)
    swarm_on = bool(getattr(swarm, "enabled", False))
    return _item(
        "H7",
        "cost_fanout_governance",
        "implemented",
        controls=[
            "per-run wall/API/token budgets are validated",
            "mission token ceilings are enforced before opening another persona run",
            "budget incidents route to bounded Neko continuation or scope recovery",
            (
                "swarm hard token ceilings are enforced before opening another persona run"
                if swarm_on
                else "swarm hard token ceilings are enforced before opening another persona run once swarm.enabled (implemented + tested; currently gated off — see swarm.enabled)"
            ),
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
        "implemented",
        controls=[
            "events, runs, tasks, proofs, role envelopes, worker sessions, and runtime instances are file-backed",
            "archive preserves task/run/proof/context evidence instead of deleting artifacts",
            "stale runs are marked and recoverable through the ticker",
            "mid-run daemon loss is detected by heartbeat TTL; a restarted ticker marks the persisted run stale and opens a stale_run incident before launching duplicate work",
            "run updates are terminal-idempotent: stale in-memory run objects cannot overwrite cancelled/completed/failed runs",
            "same persona/task/stage active run opens are de-duped by RunStore.open_run before another model call starts",
        ],
        flags={
            "daemon_enabled": bool(getattr(cfg, "daemon_enabled", False)),
            "run_lease_seconds": int(getattr(cfg, "run_lease_seconds", 0) or 0),
        },
    )


def _h9_scheduling(cfg: Any) -> dict[str, Any]:
    swarm = getattr(cfg, "swarm", None)
    swarm_on = bool(getattr(swarm, "enabled", False))
    return _item(
        "H9",
        "multi_goal_scheduling_backpressure",
        "implemented",
        controls=[
            "daemon queue mode is lane-based, so goal activation does not overwrite the target task or force foreground-only starvation",
            "foreground runtime hygiene detects foreign active runs and stale runs before new-goal work proceeds",
            "repo bundle queueing gates dependent launcher/backend handoffs",
            "repo locks and swarm budget summaries are surfaced in status",
            (
                "swarm hard token ceilings block opening another persona run and emit a swarm_budget_exceeded incident instead of crashing"
                if swarm_on
                else "swarm hard token ceilings block opening another persona run and emit a swarm_budget_exceeded incident (implemented + tested; active once swarm.enabled, currently gated off — see swarm.enabled)"
            ),
            "lane summaries include priority, state, current owner/stage, repo locks, and budget counters for resource isolation readback",
        ],
        flags={
            "swarm.enabled": bool(getattr(swarm, "enabled", False)),
            "max_active_lanes": int(getattr(swarm, "max_active_lanes", 0) or 0),
        },
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


def _recursive_supervision(cfg: Any) -> dict[str, Any]:
    supervision = getattr(cfg, "supervision", None)
    swarm = getattr(cfg, "swarm", None)
    enabled = any(
        [
            bool(getattr(supervision, "child_events_enabled", False)),
            bool(getattr(supervision, "recursive_enabled", False)),
            bool(getattr(supervision, "hierarchical_budget_enabled", False)),
            bool(getattr(supervision, "deploy_verification_enabled", False)),
            bool(getattr(swarm, "enabled", False)),
        ]
    )
    certified = False
    cert = {"state": "retired", "consecutive_green": 0, "required_consecutive_green": 0}
    blockers = []
    status = "implemented"
    if enabled and not certified:
        status = "gated"
        blockers.append(
            "recursive_supervision requires 10 green unattended certification runs before production recursive lanes are advertised as ready"
        )
    return _item(
        "recursive_supervision",
        "recursive_agent_supervised_execution",
        status,
        controls=[
            "liveness watchdog cancels hung active runs and opens run_hung incidents without model polling",
            "child.progress/child.blocked/child.returned/child.deploy_failed events are redaction-bounded and wake only parent supervisors",
            "recursive child returns are proof-gated before parent wake proceeds",
            "hierarchical budgets enforce global and per-child token ceilings before opening another child run",
            "ready independent blueprint stages can run as bounded in-process lanes behind swarm.enabled and certification",
            "deploy verification emits child.deploy_failed for assignment-starvation and startup-evidence failures",
            "production enablement is ledger-gated by the existing unattended burn-in certification target",
        ],
        flags={
            "supervision.child_events_enabled": bool(getattr(supervision, "child_events_enabled", False)),
            "supervision.recursive_enabled": bool(getattr(supervision, "recursive_enabled", False)),
            "supervision.hierarchical_budget_enabled": bool(getattr(supervision, "hierarchical_budget_enabled", False)),
            "supervision.deploy_verification_enabled": bool(getattr(supervision, "deploy_verification_enabled", False)),
            "swarm.enabled": bool(getattr(swarm, "enabled", False)),
            "certification_state": cert.get("state"),
            "consecutive_green": int(cert.get("consecutive_green") or 0),
            "required_consecutive_green": int(cert.get("required_consecutive_green") or 10),
        },
        blockers=blockers,
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
