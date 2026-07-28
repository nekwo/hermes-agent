"""Shared, in-process creation of a real Mission Control goal.

This is the single source of truth for "create a live harness goal": the CLI
``harness task create`` handler and the operator-chat ``mission_goal_create``
tool both call :func:`create_mission_goal`. Keeping it here (in ``agent_runtime``,
not ``hermes_cli``) lets the chat tool create a real, self-driving goal fully
in-process — it never shells out to the ``hermes`` CLI, which can hit the
``agent.log`` rotation lock when one Hermes process invokes another.

Creating a new foreground goal runs the standard new-goal hygiene (parks other
open goals, preempts background runs) exactly as the CLI does, so a goal created
from chat behaves identically to one created from the terminal or Assign Work.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from hermes_time import now

from .blueprints.instantiate import instantiate_blueprint
from .blueprints.resolve import BindingResolver
from .blueprints.store import BlueprintStore
from .cli_format import task_summary
from .config import load_agent_runtime_config, load_root_runtime_config
from .default_plan import ensure_default_mission_plan, specialize_default_plan_for_task
from .delivery_directive import DeliveryDirectiveInvalid, normalize_delivery_directive
from .events import EventLog
from .goal_hygiene import (
    activate_foreground_runtime,
    prepare_new_goal_runtime,
    repo_clean_baseline_from_hygiene,
)
from .launcher_process_hygiene import launcher_visual_cleanup_needed
from .models import Event, MissionPlan, Task
from .states import TaskState
from .store import TaskStore

DEFAULT_GOAL_REQUESTED_BY = "mission-control-chat"
SINGLE_REPO_BLUEPRINT_ID = "neko_single_dev"
WRITABLE_PLAN_OWNERS = frozenset({"dev", "backend_dev"})
_GOAL_CREATE_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "idempotency_key",
        "source_surface",
        "operator",
        "goal",
        "blueprint",
        "graph",
        "repo_scope",
    }
)
_GOAL_CREATE_GOAL_KEYS = frozenset(
    {
        "title",
        "description",
        "acceptance_criteria",
        "proof_expectations",
        "requires_visual_proof",
        "delivery_directive",
    }
)
_GOAL_CREATE_BLUEPRINT_KEYS = frozenset({"requested_blueprint_id", "selection_mode", "bindings"})
_GOAL_CREATE_GRAPH_KEYS = frozenset({"owner_slot", "owner_persona_id", "owner_label"})
_GOAL_CREATE_OPERATOR_KEYS = frozenset({"operator_id", "display_name", "session_id"})


def create_mission_goal(
    *,
    title: str,
    description: str,
    requested_by: str = DEFAULT_GOAL_REQUESTED_BY,
    start_daemon_mode: bool | None = None,
    config: Any | None = None,
    schema_version: int = 1,
    idempotency_key: str | None = None,
    source_surface: str = "mission_control",
    operator: dict[str, Any] | None = None,
    acceptance_criteria: list[str] | None = None,
    proof_expectations: list[str] | None = None,
    requires_visual_proof: bool = False,
    delivery_directive: dict[str, Any] | None = None,
    requested_blueprint_id: str | None = None,
    blueprint_selection_mode: str = "default",
    blueprint_bindings: dict[str, str] | None = None,
    graph_owner_persona_id: str | None = None,
    graph_owner_label: str | None = None,
    repo_scope: list[str] | None = None,
    dry_run: bool = False,
    envelope_warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a real Mission Control goal and (optionally) start the daemon.

    ``start_daemon_mode`` mirrors the CLI's tri-state ``--start-daemon`` flag:
    ``True`` forces the targeted daemon on, ``False`` leaves the goal for manual
    ticking, and ``None`` defers to ``task_create_auto_start_daemon`` config.

    Returns the task summary augmented with ``new_goal_hygiene``,
    ``foreground_runtime`` and ``daemon_start`` — the same payload the CLI emits.
    """

    config = config or load_agent_runtime_config()
    repo_scope = _safe_repo_scope_list(repo_scope)
    resolved_blueprint_id, resolved_selection_mode = _select_blueprint_for_scope(
        requested_blueprint_id=requested_blueprint_id,
        selection_mode=blueprint_selection_mode,
        repo_scope=repo_scope,
        config=config,
    )
    validation_error = _validate_create_request(
        schema_version=schema_version,
        title=title,
        description=description,
        source_surface=source_surface,
        requested_blueprint_id=requested_blueprint_id,
        blueprint_selection_mode=blueprint_selection_mode,
        repo_scope=repo_scope,
    )
    if validation_error is not None:
        return validation_error
    try:
        resolved_delivery_directive = normalize_delivery_directive(delivery_directive)
    except DeliveryDirectiveInvalid as exc:
        return _create_error(
            "invalid_request",
            f"Unsupported delivery directive: {exc}",
            retryable=False,
        )
    idempotency_key = _safe_idempotency_key(idempotency_key)
    safe_graph_owner = _safe_persona_id(graph_owner_persona_id)
    effective_blueprint_bindings = dict(blueprint_bindings or {})
    if safe_graph_owner:
        effective_blueprint_bindings.setdefault("lead", f"persona:{safe_graph_owner}")
    request_fingerprint = _create_request_fingerprint(
        title=title,
        description=description,
        source_surface=source_surface,
        acceptance_criteria=acceptance_criteria or [],
        proof_expectations=proof_expectations or [],
        requested_blueprint_id=requested_blueprint_id,
        resolved_blueprint_id=resolved_blueprint_id,
        blueprint_selection_mode=blueprint_selection_mode,
        blueprint_bindings=effective_blueprint_bindings,
        graph_owner_persona_id=safe_graph_owner,
        repo_scope=repo_scope or [],
    )
    if idempotency_key:
        duplicate = _find_create_request_by_idempotency_key(idempotency_key)
        if duplicate is not None:
            create_meta = (duplicate.harness_self_heal or {}).get("mission_goal_create")
            if isinstance(create_meta, dict) and create_meta.get("fingerprint") != request_fingerprint:
                return _create_error(
                    "duplicate_conflict",
                    "Same idempotency key was submitted with different mission content.",
                    retryable=False,
                    safe_details={"idempotency_key": idempotency_key},
                )
            return _create_response(
                duplicate,
                state="already_created",
                duplicate_of=idempotency_key,
                extra=task_summary(duplicate),
            )
    ts = now()
    task = Task(
        id=f"task_{uuid.uuid4().hex[:8]}",
        goal_id=f"goal_{uuid.uuid4().hex[:8]}",
        title=title,
        description=description,
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by=requested_by,
        requires_visual_proof=bool(requires_visual_proof),
        delivery_directive=resolved_delivery_directive,
        acceptance_criteria=list(acceptance_criteria or []),
        proof_expectations=list(proof_expectations or []),
        affected_repos=list(repo_scope or []),
    )
    plan_error = _attach_requested_blueprint_plan(
        task,
        requested_blueprint_id=resolved_blueprint_id,
        selection_mode=resolved_selection_mode,
        bindings=effective_blueprint_bindings,
        config=config,
    )
    if plan_error is not None:
        return plan_error
    route_error = _validate_resolved_repo_scope(
        task,
        repo_scope=repo_scope,
        requested_blueprint_id=resolved_blueprint_id,
    )
    if route_error is not None:
        return route_error
    if proof_expectations:
        task.operator_notes.append(
            "Proof expectations: " + "; ".join(_safe_note(item) for item in proof_expectations if _safe_note(item))
        )
    task.harness_self_heal["mission_goal_create"] = {
        "schema_version": schema_version,
        "idempotency_key": idempotency_key,
        "source_surface": source_surface,
        "operator_id": _safe_note((operator or {}).get("operator_id")) if operator else None,
        "session_id": _safe_note((operator or {}).get("session_id")) if operator else None,
        "fingerprint": request_fingerprint,
        "requested_blueprint_id": requested_blueprint_id,
        "resolved_blueprint_id": resolved_blueprint_id,
        "proof_expectations": list(proof_expectations or []),
        "repo_scope_pinned": list(repo_scope or []),
        "graph_owner_persona_id": safe_graph_owner,
        "graph_owner_label": _safe_note(graph_owner_label),
        "created_at": ts,
    }
    if envelope_warnings:
        task.harness_self_heal["mission_goal_create"]["field_drop_warnings"] = list(envelope_warnings)
    if dry_run:
        data = task_summary(task)
        data.update(_create_response(task, state="dry_run", duplicate_of=None))
        if envelope_warnings:
            data["warnings"] = list(envelope_warnings)
        return data
    cleanup_launcher_visual = launcher_visual_cleanup_needed(title, description)
    try:
        hygiene = prepare_new_goal_runtime(
            cleanup_stage47_temp=False,
            cleanup_launcher_visual_processes=cleanup_launcher_visual,
            heartbeat_ttl_seconds=config.heartbeat_ttl_seconds,
            foreground_mode=True,
            park_open_tasks=True,
            preempt_background_runs=True,
        )
    except Exception as exc:
        return _create_error(
            "runtime_unavailable",
            "Harness runtime could not accept the mission right now.",
            retryable=True,
            safe_details={"error_class": type(exc).__name__},
        )
    task.harness_self_heal["repo_clean_baseline"] = repo_clean_baseline_from_hygiene(hygiene)
    TaskStore().create(task)
    _emit_goal_create_field_drop_warnings(task, envelope_warnings or [])
    try:
        foreground_runtime = activate_foreground_runtime(
            task.id, started_by=requested_by or DEFAULT_GOAL_REQUESTED_BY
        )
        daemon_start = start_daemon_for_new_goal(
            config,
            task_id=task.id,
            start_daemon_mode=start_daemon_mode,
            foreground_runtime_instance_id=foreground_runtime.get("instance_id"),
        )
    except Exception as exc:
        return _create_error(
            "runtime_unavailable",
            "Harness runtime could not start the mission runtime.",
            retryable=True,
            safe_details={"error_class": type(exc).__name__, "mission_id": task.id},
        )
    data = task_summary(task)
    data["new_goal_hygiene"] = hygiene
    data["foreground_runtime"] = foreground_runtime
    data["daemon_start"] = daemon_start
    data.update(_create_response(task, state="created", duplicate_of=None))
    return data


def create_mission_goal_from_request(
    request: dict[str, Any],
    *,
    config: Any | None = None,
    start_daemon_mode: bool | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create a goal from the Stage 38 canonical request envelope.

    ``start_daemon_mode`` mirrors the CLI tri-state ``--start-daemon`` flag; it
    is transport-level (who drives the daemon), so it rides beside the envelope
    rather than inside it.
    """

    goal = request.get("goal") if isinstance(request.get("goal"), dict) else {}
    blueprint = request.get("blueprint") if isinstance(request.get("blueprint"), dict) else {}
    graph = request.get("graph") if isinstance(request.get("graph"), dict) else {}
    operator = request.get("operator") if isinstance(request.get("operator"), dict) else {}
    envelope_warnings = _goal_create_field_drop_warnings(request)
    source_surface = str(request.get("source_surface") or "")
    permission_error = _authorize_create_request(source_surface=source_surface, operator=operator)
    if permission_error is not None:
        return permission_error
    bindings = _string_dict(blueprint.get("bindings"))
    graph_owner = _safe_persona_id(graph.get("owner_persona_id"))
    if graph_owner:
        owner_slot = str(graph.get("owner_slot") or "lead").strip() or "lead"
        bindings.setdefault(owner_slot, f"persona:{graph_owner}")
    return create_mission_goal(
        title=str(goal.get("title") or ""),
        description=str(goal.get("description") or ""),
        requested_by=str(operator.get("operator_id") or "launcher"),
        start_daemon_mode=start_daemon_mode,
        config=config,
        schema_version=int(request.get("schema_version") or 1),
        idempotency_key=str(request.get("idempotency_key") or ""),
        source_surface=str(request.get("source_surface") or ""),
        operator=operator,
        acceptance_criteria=_string_list(goal.get("acceptance_criteria")),
        proof_expectations=_string_list(goal.get("proof_expectations")),
        requires_visual_proof=bool(goal.get("requires_visual_proof") or False),
        delivery_directive=goal.get("delivery_directive") if isinstance(goal.get("delivery_directive"), dict) else None,
        requested_blueprint_id=str(blueprint.get("requested_blueprint_id") or ""),
        blueprint_selection_mode=str(blueprint.get("selection_mode") or "default"),
        blueprint_bindings=bindings,
        graph_owner_persona_id=graph_owner,
        graph_owner_label=str(graph.get("owner_label") or ""),
        repo_scope=_string_list(request.get("repo_scope")),
        dry_run=dry_run,
        envelope_warnings=envelope_warnings,
    )


def _attach_requested_blueprint_plan(
    task: Task,
    *,
    requested_blueprint_id: str | None,
    selection_mode: str,
    bindings: dict[str, str],
    config: Any | None = None,
) -> dict[str, Any] | None:
    blueprint_id = str(requested_blueprint_id or "").strip()
    if not blueprint_id or selection_mode != "explicit":
        if bool(getattr(config or load_root_runtime_config(), "root_node_mode", False)):
            try:
                bp = BlueprintStore().get("neko_default_script")
                plan = instantiate_blueprint(
                    bp,
                    goal=task.description or task.title,
                    bindings={"root": "persona:neko_supervisor", "dev": "persona:dev", "qa": "persona:qa"},
                    resolver=BindingResolver(allow_promote=False),
                )
            except Exception as exc:
                return _create_error(
                    "blueprint_invalid",
                    "Root-node script blueprint could not instantiate.",
                    retryable=False,
                    safe_details={"requested_blueprint_id": "neko_default_script", "error_class": type(exc).__name__},
                )
            if plan.mission_intent is not None:
                plan.mission_intent.title = task.title
                plan.mission_intent.acceptance_criteria = list(task.acceptance_criteria or [])
                plan.mission_intent.source_task_id = task.id
            task.mission_plan = plan
            task.current_stage_id = plan.current_stage_id
            task.harness_self_heal["root_node_mode"] = True
            return None
        ensure_default_mission_plan(task)
        return None
    try:
        bp = BlueprintStore().get(blueprint_id)
    except FileNotFoundError:
        return _create_error(
            "blueprint_not_found",
            "Requested Neko Mission Lead blueprint is unavailable.",
            retryable=False,
            safe_details={"requested_blueprint_id": blueprint_id},
        )
    try:
        requested_bindings = {**_default_bindings_for_blueprint(bp), **bindings}
        if bp.id == SINGLE_REPO_BLUEPRINT_ID:
            repo = _single_repo_scope(task.affected_repos)
            if repo and "builder" not in bindings:
                requested_bindings["builder"] = f"persona:{_single_repo_builder_persona(repo)}"
        plan = instantiate_blueprint(
            bp,
            goal=task.description or task.title,
            bindings=requested_bindings,
            resolver=BindingResolver(allow_promote=False),
        )
    except Exception as exc:
        return _create_error(
            "blueprint_invalid",
            "Requested blueprint could not instantiate.",
            retryable=False,
            safe_details={"requested_blueprint_id": blueprint_id, "error_class": type(exc).__name__},
        )
    if plan.mission_intent is not None:
        plan.mission_intent.title = task.title
        plan.mission_intent.acceptance_criteria = list(task.acceptance_criteria or [])
        plan.mission_intent.source_task_id = task.id
    _specialize_single_repo_plan_for_task(task, plan)
    specialize_default_plan_for_task(task, plan)
    task.mission_plan = plan
    task.current_stage_id = plan.current_stage_id
    return None


def _default_bindings_for_blueprint(bp: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for slot in getattr(bp, "slots", []) or []:
        slot_id = str(getattr(slot, "id", "") or "").strip()
        role = str(getattr(slot, "role", "") or "").strip()
        persona = {
            "lead": "neko_supervisor",
            "neko": "neko_supervisor",
            "pm": "neko_supervisor",
            "builder": "dev",
            "dev": "dev",
            "backend_dev": "backend_dev",
            "verifier": "qa",
            "reviewer": "qa",
            "qa": "qa",
        }.get(role, "dev")
        if slot_id:
            result[slot_id] = f"persona:{persona}"
    return result


def _select_blueprint_for_scope(
    *,
    requested_blueprint_id: str | None,
    selection_mode: str,
    repo_scope: list[str],
    config: Any,
) -> tuple[str | None, str]:
    blueprint_id = str(requested_blueprint_id or "").strip() or None
    mode = str(selection_mode or "default").strip() or "default"
    if mode == "explicit":
        return blueprint_id, mode
    if bool(getattr(config, "root_node_mode", False)):
        return blueprint_id, mode
    if len(repo_scope) == 1:
        return SINGLE_REPO_BLUEPRINT_ID, "explicit"
    return blueprint_id, mode


def _single_repo_scope(repo_scope: list[str] | None) -> str | None:
    repos = _safe_repo_scope_list(repo_scope)
    return repos[0] if len(repos) == 1 else None


def _single_repo_builder_persona(repo: str) -> str:
    return "backend_dev" if repo == "EterniaBackend" else "dev"


def _specialize_single_repo_plan_for_task(task: Task, plan: MissionPlan) -> None:
    if getattr(plan, "blueprint_id", None) != SINGLE_REPO_BLUEPRINT_ID:
        return
    repo = _single_repo_scope(task.affected_repos)
    if repo is None:
        return
    for stage in plan.stages:
        if stage.id == "implement":
            stage.repo = repo
            stage.owner = _single_repo_builder_persona(repo)
    plan.bindings["builder"] = _single_repo_builder_persona(repo)
    plan.binding_sources.setdefault("builder", f"persona:{_single_repo_builder_persona(repo)}")


def _validate_resolved_repo_scope(
    task: Task,
    *,
    repo_scope: list[str],
    requested_blueprint_id: str | None,
) -> dict[str, Any] | None:
    if not repo_scope:
        return None
    covered = _plan_writable_repos(task.mission_plan)
    missing = [repo for repo in repo_scope if repo not in covered]
    if not missing:
        return None
    candidates = _candidate_blueprints_for_repos(repo_scope)
    return _create_error(
        "repo_scope_unroutable",
        "Requested repo scope cannot be written by the selected blueprint.",
        retryable=False,
        safe_details={
            "repo_scope": repo_scope,
            "unroutable_repos": missing,
            "blueprint_id": getattr(task.mission_plan, "blueprint_id", None) or requested_blueprint_id,
            "covered_repos": sorted(covered),
            "candidate_blueprints": candidates,
        },
    )


def _plan_writable_repos(plan: MissionPlan | None) -> set[str]:
    if plan is None:
        return set()
    repos: set[str] = set()
    for stage in getattr(plan, "stages", []) or []:
        owner = str(getattr(stage, "owner", "") or "").strip()
        kind = str(getattr(stage, "kind", "") or "").strip().lower()
        repo = str(getattr(stage, "repo", "") or "").strip()
        if owner in WRITABLE_PLAN_OWNERS and kind in {"implementation", "proof_only"} and repo:
            repos.add(repo)
    return repos


def _candidate_blueprints_for_repos(repo_scope: list[str]) -> list[dict[str, Any]]:
    wanted = set(repo_scope)
    result: list[dict[str, Any]] = []
    for bp in BlueprintStore().list():
        covered = set()
        for stage in getattr(bp, "stages", []) or []:
            if str(getattr(stage, "kind", "") or "").strip().lower() not in {"implementation", "proof_only"}:
                continue
            repo = str(getattr(stage, "repo", "") or "").strip()
            if repo:
                covered.add(repo)
        if bp.id == SINGLE_REPO_BLUEPRINT_ID:
            covered.update(wanted)
        if wanted.issubset(covered):
            result.append({"blueprint_id": bp.id, "covered_repos": sorted(covered)})
    return sorted(result, key=lambda item: (len(item["covered_repos"]), item["blueprint_id"]))[:6]


def _validate_create_request(
    *,
    schema_version: int,
    title: str,
    description: str,
    source_surface: str,
    requested_blueprint_id: str | None,
    blueprint_selection_mode: str,
    repo_scope: list[str] | None,
) -> dict[str, Any] | None:
    if int(schema_version or 0) != 1:
        return _create_error("invalid_request", "Unsupported goal-create schema version.", retryable=False)
    if not str(title or "").strip() or not str(description or "").strip():
        return _create_error("invalid_request", "Mission title and description are required.", retryable=False)
    if source_surface and source_surface != "mission_control":
        return _create_error("invalid_request", "Unsupported goal-create source surface.", retryable=False)
    if blueprint_selection_mode == "explicit" and not str(requested_blueprint_id or "").strip():
        return _create_error("invalid_request", "Explicit blueprint selection requires requested_blueprint_id.", retryable=False)
    allowed_repos = {"EterniaLauncher", "EterniaBackend", "hermes-agent"}
    invalid = [repo for repo in (repo_scope or []) if repo not in allowed_repos]
    if invalid:
        return _create_error("repo_scope_invalid", "Requested repo scope is unknown or unsafe.", retryable=False, safe_details={"repo_scope": invalid})
    return None


def _authorize_create_request(*, source_surface: str, operator: dict[str, Any] | None) -> dict[str, Any] | None:
    """Mission Control goals must be attributed to an operator identity.

    Read visibility never implies write permission; an unattributed
    ``mission_control`` create is rejected with ``permission_denied`` rather
    than silently creating an orphaned, unauthorizable mission.
    """

    if source_surface != "mission_control":
        return None
    operator_id = str((operator or {}).get("operator_id") or "").strip()
    if not operator_id:
        return _create_error(
            "permission_denied",
            "Operator identity is required to create a Mission Control goal.",
            retryable=False,
        )
    return None


def _find_create_request_by_idempotency_key(idempotency_key: str) -> Task | None:
    for task in TaskStore().list_all():
        meta = (task.harness_self_heal or {}).get("mission_goal_create")
        if isinstance(meta, dict) and meta.get("idempotency_key") == idempotency_key:
            return task
    return None


def _create_response(task: Task, *, state: str, duplicate_of: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = getattr(task, "mission_plan", None)
    data = dict(extra or {})
    data.update(
        {
            "schema_version": 1,
            "mission_id": task.id,
            "task_id": task.id,
            "goal_id": getattr(task, "goal_id", None) or task.id,
            "blueprint_id": getattr(plan, "blueprint_id", None),
            "blueprint_version": getattr(plan, "blueprint_version", None),
            "delivery_directive": task.delivery_directive,
            "proof_expectations": list(getattr(task, "proof_expectations", []) or []),
            "state": state,
            "first_snapshot_ref": f"snapshot:{task.id}:1",
            "duplicate_of": duplicate_of,
        }
    )
    return data


def _create_error(code: str, message: str, *, retryable: bool, safe_details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "safe_details": safe_details or {},
        },
    }


def _create_request_fingerprint(**payload: Any) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_idempotency_key(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", text) else None


def _safe_persona_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", text) else None


def _safe_note(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if re.search(r"[:/\\]", text) or any(marker in text.lower() for marker in ("secret", "token", "password", "credential")):
        return ""
    return text[:240]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _safe_repo_scope_list(value: list[str] | None) -> list[str]:
    result: list[str] = []
    for item in value or []:
        repo = str(item or "").strip()
        if repo and repo not in result:
            result.append(repo)
    return result


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key).strip() and str(item).strip()}


def _goal_create_field_drop_warnings(request: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    _append_unknown_fields(warnings, prefix="", data=request, allowed=_GOAL_CREATE_ENVELOPE_KEYS)
    goal = request.get("goal") if isinstance(request.get("goal"), dict) else {}
    blueprint = request.get("blueprint") if isinstance(request.get("blueprint"), dict) else {}
    graph = request.get("graph") if isinstance(request.get("graph"), dict) else {}
    operator = request.get("operator") if isinstance(request.get("operator"), dict) else {}
    _append_unknown_fields(warnings, prefix="goal", data=goal, allowed=_GOAL_CREATE_GOAL_KEYS)
    _append_unknown_fields(warnings, prefix="blueprint", data=blueprint, allowed=_GOAL_CREATE_BLUEPRINT_KEYS)
    _append_unknown_fields(warnings, prefix="graph", data=graph, allowed=_GOAL_CREATE_GRAPH_KEYS)
    _append_unknown_fields(warnings, prefix="operator", data=operator, allowed=_GOAL_CREATE_OPERATOR_KEYS)
    return warnings


def _append_unknown_fields(
    warnings: list[dict[str, Any]],
    *,
    prefix: str,
    data: dict[str, Any],
    allowed: frozenset[str],
) -> None:
    for key in sorted(str(item) for item in data.keys()):
        if key in allowed:
            continue
        field = f"{prefix}.{key}" if prefix else key
        warnings.append(
            {
                "field": field[:160],
                "reason": "unknown_or_unpersisted_goal_create_field",
            }
        )


def _emit_goal_create_field_drop_warnings(task: Task, warnings: list[dict[str, Any]]) -> None:
    if not warnings:
        return
    event_log = EventLog()
    for warning in warnings:
        event_log.append(
            Event(
                ts=now(),
                type="goal_create.field_dropped",
                task_id=task.id,
                run_id=None,
                persona_id=None,
                payload={
                    "field": str(warning.get("field") or "")[:160],
                    "reason": str(warning.get("reason") or "unknown_or_unpersisted_goal_create_field")[:160],
                    "summary": f"Goal create ignored unsupported field {str(warning.get('field') or '')[:120]}",
                },
            )
        )


def start_daemon_for_new_goal(
    config: Any,
    *,
    task_id: str,
    start_daemon_mode: bool | None,
    foreground_runtime_instance_id: str | None = None,
) -> dict[str, Any]:
    """Retired: the background Mission Daemon was removed.

    Goal creation no longer spawns a runtime; missions advance via
    ``harness goal run`` / the goal-runner. The ``start_daemon_mode`` argument is
    accepted for call-site compatibility but is a no-op.
    """

    return {
        "attempted": False,
        "started": False,
        "summary": "daemon retired; use `harness goal run` to advance this goal",
    }
