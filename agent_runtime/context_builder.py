from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from .context_requests import fulfilled_context_bundles
from .events import EventLog
from .models import AgentRun, Event, Proof
from .objective_templates import render_objective
from .packets import HANDOFF_MODES, HANDOFF_OWNERS, HANDOFF_REPOS, QA_NEXT_OWNERS, latest_packet, latest_packets_for_task
from .profile_context import mcp_owner_profile_name
from .repo_context import repo_execution_context_for_task, safe_affected_repo_labels
from .serde import to_jsonable

#: The ``Task`` record was deleted with the mission lane (S8), so there is no
#: type left to name here. Everything below takes a duck-typed, task-shaped
#: object and reads it exclusively through ``getattr``/``.id``-style access;
#: ``TaskLike`` states that honestly instead of annotating a name that resolves
#: to nothing (``from __future__ import annotations`` hid the NameError, but
#: ``typing.get_type_hints`` on these signatures would still raise).
TaskLike = Any


def _stage_records(task: TaskLike) -> list:
    """The task stage graph was retired; persisted stage keys are ignored."""

    return []


@dataclass(slots=True)
class AgentContext:
    task: TaskLike
    run: AgentRun
    current_stage: object | None = None
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    proof_ids: list[str] = field(default_factory=list)
    requires_repair: bool = False
    repair_error: str | None = None
    context_bundles: list[dict[str, Any]] = field(default_factory=list)
    proof_records: list[dict[str, Any]] = field(default_factory=list)
    incident_records: list[dict[str, Any]] = field(default_factory=list)
    repo_context: dict[str, Any] | None = None
    mission_hud: dict[str, Any] | None = None
    autonomy_packet: dict[str, Any] | None = None
    latest_handoff_packet: dict[str, Any] | None = None
    latest_delivery: dict[str, Any] | None = None
    latest_qa_review: dict[str, Any] | None = None


def build_context(
    task: TaskLike,
    run: AgentRun,
    *,
    recent_events: list[dict[str, Any]] | None = None,
    proof_ids: list[str] | None = None,
    requires_repair: bool = False,
    repair_error: str | None = None,
    proof_store=None,
    incident_store=None,
    event_log=None,
    config=None,
) -> AgentContext:
    current_stage = None
    current_stage_id = getattr(task, "current_stage_id", None)
    if current_stage_id:
        current_stage = next((stage for stage in _stage_records(task) if stage.id == current_stage_id), None)
    if current_stage is None and run.stage_id:
        current_stage = next((stage for stage in _stage_records(task) if stage.id == run.stage_id), None)
    selected_proof_ids = proof_ids if proof_ids is not None else list(getattr(task, "proof_ids", []) or [])
    event_log = event_log or EventLog()
    stage_id = run.stage_id or current_stage_id
    packets = latest_packets_for_task(task.id, event_log=event_log, stage_id=stage_id)
    packets = _add_cross_stage_source_delivery(
        task.id,
        packets,
        event_log=event_log,
        persona_id=run.persona_id,
    )
    packets = _add_cross_stage_qa_review(
        task.id,
        packets,
        event_log=event_log,
        persona_id=run.persona_id,
    )
    selected_events = recent_events
    if selected_events is None:
        selected_events = _recent_relevant_events(
            task.id,
            event_log=event_log,
            persona_id=run.persona_id,
            stage_id=stage_id,
        )
    mission_hud = _mission_hud(task, run, packets, config=config, proof_store=proof_store)
    if requires_repair:
        mission_hud = dict(mission_hud or {})
        mission_hud["validation_repair"] = _validation_repair_hud(repair_error, run=run, mission_hud=mission_hud)
    return AgentContext(
        task=task,
        run=run,
        current_stage=current_stage,
        recent_events=selected_events or [],
        proof_ids=selected_proof_ids,
        requires_repair=requires_repair,
        repair_error=repair_error,
        context_bundles=fulfilled_context_bundles(task),
        proof_records=_proof_records(selected_proof_ids, proof_store=proof_store),
        incident_records=_incident_records(task, incident_store=incident_store),
        repo_context=_safe_repo_context(task),
        mission_hud=mission_hud,
        latest_handoff_packet=_safe_packet_projection(packets.get("handoff_packet")),
        latest_delivery=_safe_packet_projection(packets.get("delivery")),
        latest_qa_review=_safe_packet_projection(packets.get("qa_review")),
    )


def render_context(ctx: AgentContext) -> str:
    objective_stage = _context_objective_stage(ctx.task, ctx.run)
    lines = [
        "# Agent Runtime Tick Context",
        "",
        "## Task",
        f"- id: {ctx.task.id}",
        f"- title: {ctx.task.title}",
        f"- state: {ctx.task.state}",
        f"- requested_by: {ctx.task.requested_by}",
        f"- requires_visual_proof: {bool(getattr(ctx.task, 'requires_visual_proof', False))}",
        "",
        "## Objective",
        render_objective(
            objective_stage,
            goal=ctx.task,
            input_artifact=_objective_input_artifact(ctx, objective_stage),
            role=_stage_role(ctx.task, ctx.run, objective_stage),
            output_type=_stage_output_type(objective_stage),
        ),
        "",
        "## Acceptance Criteria",
    ]
    lines.extend(f"- {item}" for item in (getattr(ctx.task, "acceptance_criteria", []) or ["(none yet)"]))
    if getattr(ctx.task, "affected_repos", None):
        lines.extend(["", "## Affected Repositories"])
        safe_repos = safe_affected_repo_labels(ctx.task.affected_repos)
        lines.extend(f"- {item}" for item in (safe_repos or ["(unresolved; absolute path withheld)"]))
    if ctx.repo_context:
        lines.extend(
            [
                "",
                "## Repo-Grounded Execution",
                f"- repo_label: {ctx.repo_context.get('repo_label')}",
                "- working_directory: resolved affected repo root (absolute path withheld)",
                f"- context_loaded: {ctx.repo_context.get('context_loaded')}",
                "- scope_rule: Start file/search/terminal work from this repo. Do not search outside it unless the task explicitly requires broader scope.",
                "- brain_search_rule: Use session_search only when task context is insufficient, and summarize prior decisions/conventions without dumping vault pages or secrets.",
            ]
        )
        context_excerpts = ctx.repo_context.get("context_excerpts")
        if isinstance(context_excerpts, list) and context_excerpts:
            lines.extend(["", "## Repo Context Excerpts (Harness-Controlled)"])
            for item in context_excerpts:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "context").strip()
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                truncated = bool(item.get("truncated"))
                lines.extend(
                    [
                        f"### {label}",
                        "```text",
                        content,
                        "```",
                    ]
                )
                if truncated:
                    lines.append("- excerpt_truncated: true")
    if getattr(ctx.task, "non_goals", None):
        lines.extend(["", "## Non-goals / Scope Boundaries"])
        lines.extend(f"- {item}" for item in ctx.task.non_goals)
    lines.extend(["", "## Task Snapshot", "```json", _context_json(_safe_task_snapshot(ctx)), "```"])
    if ctx.mission_hud:
        lines.extend(["", "## Mission HUD", "```json", _context_json(_prompt_visible_mission_hud(ctx.mission_hud)), "```"])
    if ctx.autonomy_packet:
        lines.extend(
            [
                "",
                "## Autonomy / Tool Economy Contract",
                "Harness-generated public operating packet. Follow these budgets before broad inspection, proof, or handoff.",
                "```json",
                _context_json(ctx.autonomy_packet),
                "```",
            ]
        )
    if ctx.latest_handoff_packet:
        lines.extend(["", "## Latest Handoff Packet", "```json", _context_json(ctx.latest_handoff_packet), "```"])
    if ctx.latest_delivery:
        lines.extend(["", "## Latest Delivery Packet", "```json", _context_json(ctx.latest_delivery), "```"])
    if ctx.latest_qa_review:
        lines.extend(["", "## Latest QA Review Packet", "```json", _context_json(ctx.latest_qa_review), "```"])
    if _stage_records(ctx.task):
        lines.extend(["", "## All Stages"])
        for stage in _stage_records(ctx.task):
            status = stage.status.value if hasattr(stage.status, "value") else str(stage.status)
            lines.extend(
                [
                    f"- id: {stage.id}",
                    f"  title: {stage.title}",
                    f"  status: {status}",
                    f"  current: {stage.id == (ctx.current_stage.id if ctx.current_stage else None)}",
                    f"  acceptance_criteria: {len(stage.acceptance_criteria or [])} item(s)",
                    f"  test_plan: {len(stage.test_plan or [])} item(s)",
                ]
            )
    if getattr(ctx.task, "issue_discoveries", None):
        lines.extend(["", "## Issue Discoveries"])
        for item in ctx.task.issue_discoveries:
            lines.append(
                f"- id: {item.get('id')} status: {item.get('triage_status')} severity: {item.get('severity')} relationship_hint: {item.get('relationship_hint')} title: {item.get('title')}"
            )
        lines.append("Dev/QA must report unrelated issues instead of fixing them inline. PM may fork at most one direct child mission; child missions must report new gaps at the end instead of spawning deeper trees. Same-scope test/analyzer fixing is limited to one bounded pass per mission.")
    lines.extend(["", "## Current Run", f"- id: {ctx.run.id}", f"- persona_id: {ctx.run.persona_id}"])
    if ctx.current_stage:
        lines.extend(
            [
                "",
                "## Current Stage",
                f"- id: {ctx.current_stage.id}",
                f"- title: {ctx.current_stage.title}",
                f"- objective: {ctx.current_stage.objective}",
                f"- status: {ctx.current_stage.status}",
            ]
        )
    lines.extend(["", "## Proof IDs", _format_proof_ids(ctx.proof_ids)])
    if ctx.proof_records:
        lines.extend(["", "## Proof Records"])
        for proof in ctx.proof_records:
            lines.extend(
                [
                    f"- id: {proof.get('id')}",
                    f"  type: {proof.get('type')}",
                    f"  stage_id: {proof.get('stage_id')}",
                    f"  title: {proof.get('title')}",
                    f"  path_or_value: {proof.get('path_or_value')}",
                    f"  redaction_status: {proof.get('redaction_status')}",
                ]
            )
            metadata = proof.get("metadata")
            if isinstance(metadata, dict):
                for key in (
                    "status",
                    "exit_code",
                    "timed_out",
                    "shell",
                    "workdir_label",
                    "command",
                    "verdict",
                    "artifact_exists",
                    "artifact_bytes",
                    "artifact_relative_path",
                    "stdout_excerpt",
                    "stderr_excerpt",
                    "proof_intent",
                    "environment_fingerprint",
                    "environment_fingerprint_status",
                ):
                    if key in metadata:
                        lines.append(f"  {key}: {metadata[key]}")
                if isinstance(metadata.get("findings"), list):
                    lines.append(f"  findings: {metadata['findings']}")
    if ctx.incident_records:
        lines.extend(["", "## Open Incidents"])
        for incident in ctx.incident_records:
            lines.extend(
                [
                    f"- id: {incident.get('id')}",
                    f"  kind: {incident.get('kind')}",
                    f"  summary: {incident.get('summary')}",
                    f"  run_id: {incident.get('run_id')}",
                ]
            )
            if incident.get("underlying_run_terminal"):
                lines.append(f"  underlying_run_state: {incident.get('underlying_run_state')}")
                lines.append(f"  resolution_hint: {incident.get('resolution_hint')}")
    if ctx.recent_events:
        lines.extend(["", "## Recent Events"])
        lines.extend(f"- {to_jsonable(event)}" for event in ctx.recent_events)
    if ctx.context_bundles:
        lines.extend(["", "## Fulfilled File Context"])
        for bundle in ctx.context_bundles:
            lines.append(f"- request_id: {bundle.get('request_id')} bundle_id: {bundle.get('bundle_id')}")
            for item in bundle.get("files", []):
                lines.extend([f"### {item.get('path')}", "```", str(item.get("content", "")), "```"])
    if ctx.requires_repair:
        lines.extend(["", "## Previous decision failed validation", ctx.repair_error or "Unknown validation error"])
        lines.append("Return one corrected AgentDecision JSON object only. Do not repeat the invalid field or invalid value.")
    return "\n".join(lines)


def _context_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)


def _prompt_visible_mission_hud(hud: dict[str, Any]) -> dict[str, Any]:
    """Return the worker-facing HUD contract without legacy debug surfaces."""

    if not isinstance(hud, dict):
        return {}
    result: dict[str, Any] = {}
    agent_hud = hud.get("agent_hud")
    if isinstance(agent_hud, dict):
        result["agent_hud"] = agent_hud
    for key in (
        "terminal_feedback",
        "validation_repair",
        "failed_proof_ids",
        "current_stage_command_hints",
        "environment_fingerprint_status",
        "stage_task_list",
        "typed_current_stage",
        "typed_qa_gate",
        "counters",
        "self_heal_attempts_remaining",
    ):
        value = hud.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    return result


def _validation_repair_hud(repair_error: str | None, *, run: AgentRun, mission_hud: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _parse_repair_payload(repair_error)
    message = str(payload.get("message") or repair_error or "Unknown validation error").strip()
    hud: dict[str, Any] = {
        "status": "invalid_previous_decision",
        "message": message[:500],
        "run_id": run.id,
        "required_action": "Return one corrected AgentDecision JSON object only; preserve the same task intent and change only the invalid contract fields.",
    }
    hud.update(_repair_hint_for_message(message))
    corrected = _corrected_shape_for_repair(payload, mission_hud=mission_hud)
    if corrected:
        hud["corrected_shape"] = corrected
        hud["allowed_retry_action"] = corrected.get("decision_type")
        hud["retry_rule"] = "Retry once with the corrected visible action shape and no unknown fields; then block with exact feedback."
    for key in ("decision_type", "invalid_field", "invalid_value", "repair_attempt", "max_repair_attempts"):
        if payload.get(key) is not None:
            hud[key] = payload[key]
    return {key: value for key, value in hud.items() if value not in (None, "", [], {})}


def _corrected_shape_for_repair(payload: dict[str, Any], *, mission_hud: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(mission_hud, dict):
        return None
    agent_hud = mission_hud.get("agent_hud") if isinstance(mission_hud.get("agent_hud"), dict) else {}
    recommended = agent_hud.get("recommended_action") if isinstance(agent_hud, dict) else None
    if not isinstance(recommended, dict):
        return None
    message = str(payload.get("message") or "").lower()
    if "request_test_run is not valid for no-edit investigation context stages" in message:
        context_shape = _corrected_shape_from_menu(
            mission_hud.get("context_expansion_menu"),
            role=str(mission_hud.get("role") or ""),
            shape_id="common.request_file_reads",
            fallback_action_id="request_context",
        )
        if context_shape:
            return context_shape
    requested_type = str(payload.get("decision_type") or "").strip()
    if requested_type and requested_type != str(recommended.get("decision_type") or ""):
        for item in agent_hud.get("options") or []:
            if isinstance(item, dict) and str(item.get("decision_type") or "") == requested_type:
                candidate = dict(recommended)
                candidate.update({key: item.get(key) for key in ("choice_id", "action_id", "decision_type", "shape_id", "label") if item.get(key)})
                return candidate
    return dict(recommended)


def _corrected_shape_from_menu(menu: Any, *, role: str, shape_id: str, fallback_action_id: str) -> dict[str, Any] | None:
    if not isinstance(menu, list):
        return None
    item = next((entry for entry in menu if isinstance(entry, dict) and entry.get("shape_id") == shape_id), None)
    if not isinstance(item, dict):
        return None
    skill = _skill_reference_for_action(role, shape_id=shape_id, action_id=str(item.get("worker_action_id") or fallback_action_id))
    action = {
        "choice_id": item.get("choice_id"),
        "action_id": item.get("worker_action_id") or fallback_action_id,
        "decision_type": item.get("decision_type"),
        "shape_id": shape_id,
        "label": item.get("label"),
        "reason": item.get("when"),
        "required_payload_keys": item.get("required_payload_keys", []),
        "allowed_payload_keys": item.get("allowed_payload_keys", []),
        "nested_required": item.get("nested_required", {}),
        "enum_choices": item.get("enum_choices", {}),
        "payload_skeleton": item.get("payload_template") or {},
        "forbid_unknown_payload_keys": True,
        **skill,
    }
    return {key: value for key, value in action.items() if key == "payload_skeleton" or value not in (None, "", [], {})}


def _parse_repair_payload(repair_error: str | None) -> dict[str, Any]:
    if not repair_error:
        return {}
    try:
        parsed = json.loads(repair_error)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _repair_hint_for_message(message: str) -> dict[str, Any]:
    text = message.lower()
    if "payload has unsupported keys" in text:
        return {
            "invalid_field": "payload",
            "repair_mode": "closed_payload_contract",
            "shape_hint": "Remove unsupported payload keys. Use the Mission HUD recommended visible action and include only keys listed in agent_hud.recommended_action.allowed_payload_keys.",
        }
    if "stages[" in text and "unsupported keys" in text:
        return {
            "invalid_field": "payload.stages[]",
            "repair_mode": "closed_stage_contract",
            "allowed_stage_keys": ["id", "title", "objective", "acceptance_criteria", "affected_paths", "test_plan", "requires_visual_proof", "delivery"],
            "shape_hint": "Remove unsupported stage fields. Stage details must use only the allowed stage keys.",
        }
    if _is_visual_proof_repair_message(text):
        invalid_field = _visual_proof_invalid_field(text)
        owner_profile = mcp_owner_profile_name("launcher_qa")
        hint: dict[str, Any] = {
            "required_payload_keys": [
                "stage_id",
                "target",
                "proof_requirement",
                "mcp_server",
                "required_launch_pins",
            ],
            "required_launch_pins_keys": ["hermes_profile", "runtime_root_id"],
            "recommended_values": {
                "target": "mission_control",
                "mcp_server": "launcher_qa",
                "required_launch_pins.hermes_profile": owner_profile,
                "required_launch_pins.runtime_root_id": "agent-runtime",
            },
            "shape_hint": f'For request_screenshot/request_video, return payload {{"stage_id":"<current stage>","target":"mission_control","proof_requirement":"<exact visual claim>","mcp_server":"launcher_qa","required_launch_pins":{{"hermes_profile":"{owner_profile}","runtime_root_id":"agent-runtime"}}}}. Use the persisted profile of the worker that owns the request; use a redaction-safe runtime_root_id token, never an absolute path.',
        }
        if invalid_field:
            hint["invalid_field"] = invalid_field
        if invalid_field == "payload.mcp_server":
            hint["allowed_values"] = ["launcher_qa"]
            hint["recommended_value"] = "launcher_qa"
        elif invalid_field == "payload.target":
            hint["recommended_value"] = "mission_control"
        return hint
    if "delivery.next_owner" in text:
        return {
            "invalid_field": "delivery.next_owner",
            "allowed_values": sorted(HANDOFF_OWNERS),
            "recommended_value": "<next owner persona id>",
            "shape_hint": "Name the next owner from the active persona data, or omit next_owner if no handoff is needed.",
        }
    if "handoff_packet" in text and ("target_owner" in text or "next_owner" in text or "final_owner" in text):
        return {
            "invalid_field": "handoff_packet.owner",
            "allowed_values": sorted(HANDOFF_OWNERS),
            "shape_hint": "Use target_owner or next_owner from the allowed values only.",
        }
    if "handoff_packet.handoff_mode" in text:
        return {
            "invalid_field": "handoff_packet.handoff_mode",
            "allowed_values": sorted(HANDOFF_MODES),
            "recommended_value": "sequential_specialists",
            "shape_hint": "For contract_join or qa_coordination_release packets in this flow, include handoff_mode=sequential_specialists and keep packet_kind, mission_phase, target_owner, target_repo, proof_gate, and join_gate aligned.",
        }
    if "qa_review.next_owner" in text:
        return {
            "invalid_field": "qa_review.next_owner",
            "allowed_values": sorted(QA_NEXT_OWNERS),
            "shape_hint": "Route work to a persona id present in the active runtime data, or to human for a true external blocker.",
        }
    if "qa_review.coverage" in text:
        return {
            "invalid_field": "payload.qa_review.coverage",
            "required_coverage_keys": ["backend_contract", "launcher_integration", "visual_or_mcp", "cross_stack_join"],
            "allowed_values": ["not_required", "missing", "reviewed", "blocked", "failed"],
            "shape_hint": "For implementation verdicts, payload.qa_review.coverage must include all four keys. Use reviewed for covered proof, not_required only when that lane is out of scope, missing/blocked/failed for gaps. Approved verdicts normally route next_owner=harness.",
            "example": {
                "coverage": {
                    "backend_contract": "not_required",
                    "launcher_integration": "reviewed",
                    "visual_or_mcp": "reviewed",
                    "cross_stack_join": "not_required",
                },
                "mcp_status": "passed",
                "decision_basis": "proof_packet",
                "next_owner": "harness",
            },
        }
    if "target_repo" in text or "next_repo" in text or "final_repo" in text:
        return {
            "invalid_field": "repo",
            "allowed_values": sorted(HANDOFF_REPOS),
            "shape_hint": "Use canonical repo labels only; never use absolute paths in packets.",
        }
    if "work_status" in text:
        return {
            "invalid_field": "delivery.work_status",
            "allowed_values": ["proof_requested", "ready_for_qa"],
            "shape_hint": "Harness derives delivery status from hand_off and the authoritative proof gate; do not declare work_status in public decisions.",
        }
    if "proof_ids" in text:
        return {
            "invalid_field": "payload.proof_ids",
            "shape_hint": "Use hand_off for Dev completion and qa_verdict for QA findings; Harness derives delivery and proof gate state.",
        }
    if "recipe_id" in text:
        return {
            "invalid_field": "payload.recipe_id",
            "shape_hint": "Use one advertised proof recipe exactly, or omit recipe_id and provide focused commands.",
        }
    if "proof command policy" in text or "generic flutter/dart readiness" in text or "unbounded full-suite" in text:
        return {
            "invalid_field": "payload.commands",
            "shape_hint": "Replace generic or unbounded commands with the narrow proof command requested by the current stage and proof intent.",
        }
    return {}


def _is_visual_proof_repair_message(text: str) -> bool:
    if "request_screenshot" in text or "request_video" in text:
        return True
    if "mcp_server" in text or "required_launch_pins" in text:
        return True
    return "missing payload keys" in text and "target" in text and "target_repo" not in text


def _visual_proof_invalid_field(text: str) -> str | None:
    if "mcp_server" in text:
        return "payload.mcp_server"
    if "required_launch_pins.hermes_profile" in text or "hermes_profile" in text:
        return "payload.required_launch_pins.hermes_profile"
    if "required_launch_pins.runtime_root_id" in text or "runtime_root_id" in text:
        return "payload.required_launch_pins.runtime_root_id"
    if "required_launch_pins" in text:
        return "payload.required_launch_pins"
    if "proof_requirement" in text:
        return "payload.proof_requirement"
    if "target" in text and "target_repo" not in text:
        return "payload.target"
    if "stage_id" in text:
        return "payload.stage_id"
    return None


def _proof_records(proof_ids: list[str], *, proof_store=None) -> list[dict[str, Any]]:
    if proof_store is None or not proof_ids:
        return []
    records: list[dict[str, Any]] = []
    for proof_id in proof_ids[:20]:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            records.append({"id": proof_id, "missing": True})
            continue
        records.append(_safe_proof_record(proof))
    return records


def _format_proof_ids(proof_ids: list[str], *, limit: int = 12) -> str:
    clean = [str(proof_id).strip() for proof_id in (proof_ids or []) if str(proof_id).strip()]
    if not clean:
        return "(none)"
    selected = clean[-max(1, limit):]
    omitted = len(clean) - len(selected)
    suffix = f" (+{omitted} earlier proof id(s) omitted from prompt; raw proof list remains in task/proof store)" if omitted > 0 else ""
    return ", ".join(selected) + suffix


def _incident_records(task: TaskLike, *, incident_store=None) -> list[dict[str, Any]]:
    if incident_store is None:
        return []
    incident_ids = list(getattr(task, "open_incident_ids", []) or [])
    if not incident_ids and hasattr(incident_store, "list_open"):
        incident_ids = [incident.id for incident in incident_store.list_open() if incident.task_id == task.id]
    if not incident_ids:
        return []
    records: list[dict[str, Any]] = []
    for incident_id in incident_ids[:10]:
        try:
            incident = incident_store.get(incident_id)
        except Exception:
            records.append({"id": incident_id, "missing": True})
            continue
        record = {
            "id": incident.id,
            "kind": str(incident.kind)[:120],
            "summary": str(incident.summary)[:500],
            "run_id": str(incident.run_id or "")[:120] or None,
        }
        terminal_state = _incident_run_terminal_state(incident.run_id)
        if terminal_state:
            record["underlying_run_state"] = terminal_state
            record["underlying_run_terminal"] = True
            record["resolution_hint"] = (
                "The underlying run is already terminal; this incident is yours to close with "
                "resolve_incident and a redaction-safe reason. Do not block on it."
            )
        records.append(record)
    return records


def _incident_run_terminal_state(run_id: str | None) -> str | None:
    if not run_id:
        return None
    try:
        from .states import RunState
        from .store import RunStore

        run = RunStore().get(str(run_id))
    except Exception:
        return None
    if run.state in {RunState.CANCELLED, RunState.FAILED, RunState.COMPLETED, RunState.STALE}:
        return run.state.value
    return None


def _safe_repo_context(task: TaskLike) -> dict[str, Any] | None:
    try:
        ctx = repo_execution_context_for_task(task)
    except ValueError:
        return None
    if ctx is None:
        return None
    return {
        "repo_label": ctx.repo_label,
        "source": ctx.source,
        "context_loaded": ctx.context_loaded_label,
    }


def _safe_task_snapshot(ctx: AgentContext) -> dict[str, Any]:
    snapshot = to_jsonable(ctx.task)
    if not isinstance(snapshot, dict) and hasattr(ctx.task, "__dict__"):
        snapshot = to_jsonable(vars(ctx.task))
    if isinstance(snapshot, dict):
        snapshot["affected_repos"] = safe_affected_repo_labels(getattr(ctx.task, "affected_repos", []) or [])
        if isinstance(snapshot.get("stages"), list):
            snapshot["stages"] = [
                {
                    "id": stage.get("id"),
                    "title": stage.get("title"),
                    "status": stage.get("status"),
                    "acceptance_criteria_count": len(stage.get("acceptance_criteria") or []),
                    "test_plan_count": len(stage.get("test_plan") or []),
                }
                for stage in snapshot["stages"]
                if isinstance(stage, dict)
            ]
    return snapshot


def _safe_proof_record(proof: Proof) -> dict[str, Any]:
    return {
        "id": proof.id,
        "task_id": proof.task_id,
        "stage_id": proof.stage_id,
        "type": proof.type.value if hasattr(proof.type, "value") else str(proof.type),
        "title": proof.title,
        "path_or_value": proof.path_or_value,
        "redaction_status": proof.redaction_status,
        "metadata": _safe_proof_metadata(proof.metadata),
    }


def _safe_proof_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "exit_code",
        "timed_out",
        "shell",
        "workdir_label",
        "command",
        "commands_requested",
        "timeout_seconds",
        "verdict",
        "artifact_exists",
        "artifact_bytes",
        "artifact_relative_path",
        "stdout_excerpt",
        "stderr_excerpt",
        "proof_intent",
        "environment_fingerprint",
        "environment_fingerprint_status",
    }
    safe = {key: value for key, value in (metadata or {}).items() if key in allowed}
    for key in ("command", "artifact_relative_path", "stdout_excerpt", "stderr_excerpt", "proof_intent", "environment_fingerprint"):
        if key in safe:
            safe[key] = str(safe[key])[:2200]
    findings = metadata.get("findings") if isinstance(metadata, dict) else None
    if isinstance(findings, list):
        safe["findings"] = [_safe_finding(item) for item in findings[:10] if isinstance(item, dict)]
    return safe


def _context_objective_stage(task: TaskLike, run: AgentRun):
    return None
def _objective_input_artifact(ctx: AgentContext, stage) -> str | None:
    depends_on = list(getattr(stage, "depends_on", []) or []) if stage is not None else []
    if not depends_on:
        return None
    for packet in (ctx.latest_delivery, ctx.latest_qa_review, ctx.latest_handoff_packet):
        if isinstance(packet, dict):
            body = packet.get("body") if isinstance(packet.get("body"), dict) else packet
            if body:
                return _context_json(body)[:1200]
    return None


def _stage_role(task: TaskLike, run: AgentRun, stage) -> str:
    return str(getattr(stage, "owner", "") or "").strip() or _hud_owner(run)
def _stage_output_type(stage) -> str:
    explicit = str(getattr(stage, "output_type", "") or "").strip()
    if explicit:
        return explicit
    gate = _stage_proof_gate(stage)
    required = {str(item) for item in gate.get("required_proof_types", []) or []}
    kind = str(getattr(stage, "kind", "") or "").strip().lower().replace("_", " ")
    if "qa_verdict" in required or kind == "qa verdict":
        return "qa verdict"
    if required & {"test_run", "diff", "diff_stat", "commit"} or kind in {"implementation", "proof only"}:
        return "code feature"
    if required & {"artifact", "text", "url"} or kind in {"scope", "context", "investigation"}:
        return "design document"
    return "design document"


def _stage_proof_gate(stage) -> dict[str, Any]:
    gate = getattr(stage, "proof_gate", None)
    if isinstance(gate, dict):
        return dict(gate)
    if bool(getattr(stage, "requires_visual_proof", False)) and getattr(stage, "requires_product_edit", None) is not True:
        return {"required": True, "minimum_status": "passed", "required_proof_types": ["screenshot"], "visual_required": True}
    test_plan = list(getattr(stage, "test_plan", []) or []) if stage is not None else []
    if test_plan:
        return {"required": True, "minimum_status": "passed", "required_proof_types": ["test_run"], "commands": test_plan}
    return {"required": False, "required_proof_types": [], "minimum_status": "passed"}


def _mission_hud(task: TaskLike, run: AgentRun, packets: dict[str, dict[str, Any]], *, config=None, proof_store=None) -> dict[str, Any] | None:
    hud = {
        "task_id": task.id,
        "phase": str(task.state),
        "current_owner": run.persona_id,
        "role": _hud_owner(run),
        "current_stage_id": run.stage_id or getattr(task, "current_stage_id", None),
    }
    feedback = _terminal_feedback(task, run)
    if feedback:
        hud["terminal_feedback"] = feedback
    return hud


def _terminal_feedback(task: TaskLike, run: AgentRun) -> dict[str, Any] | None:
    context_feedback = _latest_context_request_feedback(task, run)
    if context_feedback:
        return context_feedback
    progress = run.progress if isinstance(getattr(run, "progress", None), dict) else {}
    if progress:
        feedback = {
            "source": "run_progress",
            "status": progress.get("status"),
            "phase": progress.get("phase"),
            "step": progress.get("step"),
            "summary": progress.get("summary"),
            "next_expected": progress.get("next_expected"),
            "decision_type": progress.get("decision_type"),
        }
        return {key: value for key, value in feedback.items() if value not in (None, "", [], {})}
    return None


def _latest_context_request_feedback(task: TaskLike, run: AgentRun) -> dict[str, Any] | None:
    requests = [req for req in (getattr(task, "context_requests", []) or []) if isinstance(req, dict)]
    if not requests:
        return None
    persona = str(getattr(run, "persona_id", "") or "")
    stage_id = str(getattr(run, "stage_id", "") or getattr(task, "current_stage_id", "") or "")
    relevant = [req for req in requests if str(req.get("actor") or "") in {"", persona, _hud_owner(run)}]
    req = (relevant or requests)[-1]
    status = str(req.get("status") or "unknown")
    failure = str(req.get("failure_reason") or "").strip()
    paths = [str(item) for item in (req.get("paths") or []) if str(item).strip()][:8]
    if status == "fulfilled":
        action_result = "context_available"
        summary = "Context request fulfilled; use the attached context bundle before asking again."
        next_expected = "use_context_then_deliver_or_request_one_narrower_context"
    elif status == "fulfilled_partial":
        action_result = "context_available_partial"
        summary = "Context request partially fulfilled; use returned files and inspect per-path failures before asking again."
        next_expected = "use_partial_context_then_request_one_missing_path_or_block"
    elif status == "superseded":
        action_result = "context_request_ignored"
        summary = "Duplicate context request was superseded; do not repeat the same request."
        next_expected = "choose_a_different_visible_hud_action_or_block_with_evidence"
    elif status == "unsupported":
        action_result = "context_unavailable"
        summary = f"Context request was unsupported: {failure or 'unknown'}."
        next_expected = "request_one_narrower_repo_relative_path_or_block_with_exact_feedback"
    else:
        action_result = "context_request_pending"
        summary = "Context request is pending."
        next_expected = "wait_for_context_feedback_or_block_if_stale"
    feedback = {
        "source": "context_request",
        "request_id": req.get("id"),
        "action_result": action_result,
        "status": status,
        "failure_reason": failure or None,
        "paths": paths,
        "path_results": (req.get("path_results") or [])[:10] if isinstance(req.get("path_results"), list) else [],
        "stage_id": stage_id or None,
        "summary": summary,
        "next_expected": next_expected,
    }
    return {key: value for key, value in feedback.items() if value not in (None, "", [], {})}


def _skill_reference_for_action(role: str, *, shape_id: str, action_id: str) -> dict[str, str]:
    del role, action_id
    if shape_id.startswith("neko."):
        section = "Scope Route"
        if "recovery" in shape_id:
            section = "Bounded Recovery"
        elif "qa_coordination" in shape_id:
            section = "QA Release"
        elif "resolve_incident" in shape_id:
            section = "Incident Resolution"
        return {
            "skill_ref": "harness-mission-lead",
            "skill_section": section,
            "skill_reason": "Open only when owner/repo/proof-gate routing is not obvious from the HUD.",
        }
    if shape_id.startswith("qa."):
        section = "QA Verdict"
        if "screenshot" in shape_id or "video" in shape_id:
            section = "Request Missing Proof"
        elif "block" in shape_id:
            section = "Report Blocker"
        return {
            "skill_ref": "harness-qa-verdict",
            "skill_section": section,
            "skill_reason": "Open only when proof strength, visual proof, or rejection wording is non-trivial.",
        }
    section = "Hand Off"
    if "request_test_run" in shape_id:
        section = "Request Proof Recipe"
    elif "request_file_reads" in shape_id or "needs_context" in shape_id:
        section = "Request Context"
    elif "stage_plan" in shape_id or "correct_stage" in shape_id:
        section = "Stage Plan"
    elif "block" in shape_id:
        section = "Report Blocker"
    return {
        "skill_ref": "harness-dev-delivery",
        "skill_section": section,
        "skill_reason": "Open only when delivery shape, proof request, or blocker wording is non-trivial.",
    }


def _hud_owner(run: AgentRun) -> str:
    return str(getattr(run, "persona_id", "") or "").strip() or "unknown"


def _safe_packet_projection(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(packet, dict):
        return None
    allowed = {
        "packet_id",
        "packet_type",
        "packet_version",
        "stage_id",
        "actor",
        "source_decision_type",
        "content_hash",
        "normalization_status",
        "dropped_fields",
        "renamed_fields",
        "truncated_fields",
        "redaction_status",
        "summary",
        "body",
    }
    projected = {key: packet.get(key) for key in allowed if key in packet}
    body = projected.get("body")
    if isinstance(body, dict):
        projected["body"] = _safe_packet_body(body)
    return projected


def _safe_packet_body(body: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "packet_kind",
        "mission_phase",
        "handoff_mode",
        "decision_status",
        "target_owner",
        "target_repo",
        "target_dev_persona",
        "next_owner",
        "next_repo",
        "final_owner",
        "final_repo",
        "proof_gate",
        "join_gate",
        "harness_rules",
        "self_heal",
        "joined_proof_ids",
        "joined_contract_packet_ids",
        "assumptions_made",
        "alternatives_considered",
        "operator_note",
        "human_action_required",
        "delivery_version",
        "source_handoff_packet_id",
        "work_status",
        "consumed_contract_packet_ids",
        "consumed_proof_ids",
        "produced_contract_packet_id",
        "contract_packet",
        "changed_files",
        "changed_paths",
        "inspected_paths",
        "dirty_baseline",
        "coverage_claims",
        "known_non_coverage",
        "proof_reuse_basis",
        "failed_proof_classification",
        "handoff_repair",
        "proof_ids",
        "proof_summary",
        "command_summary",
        "summary",
        "findings",
        "recommendations",
        "model_options",
        "wd_tagger_assessment",
        "questions",
        "known_gaps",
        "qa_review_version",
        "review_scope",
        "proof_reviewed",
        "coverage",
        "contract_packets_reviewed",
        "delivery_packets_reviewed",
        "remaining_gaps",
        "decision_basis",
        "mcp_status",
    }
    safe = {key: value for key, value in body.items() if key in allowed}
    return _truncate_packet_values(safe)


def _add_cross_stage_source_delivery(
    task_id: str,
    packets: dict[str, dict[str, Any]],
    *,
    event_log: EventLog,
    persona_id: str | None,
) -> dict[str, dict[str, Any]]:
    """Carry the previous specialist delivery into cross-stage handoff/review.

    Contract-join handoffs intentionally switch stages from Backend to Launcher.
    A strict current-stage packet lookup can therefore hide the backend delivery
    packet from Launcher Dev, even though the handoff says the join is satisfied.

    QA release stages have the same shape: the current stage is ``qa_release``
    while the reviewable Dev delivery belongs to the completed implementation or
    investigation stage.
    """

    if str(persona_id or "").strip() == "qa":
        source_delivery = latest_packet(task_id, "delivery", event_log=event_log, stage_id=None)
        if source_delivery:
            merged = dict(packets)
            merged["delivery"] = source_delivery
            return merged
    if packets.get("delivery"):
        return packets
    handoff = packets.get("handoff_packet")
    body = handoff.get("body") if isinstance(handoff, dict) and isinstance(handoff.get("body"), dict) else {}
    if str(body.get("packet_kind") or "") != "contract_join":
        return packets
    if not _packet_targets_persona(body, persona_id):
        return packets
    source_delivery = latest_packet(task_id, "delivery", event_log=event_log, stage_id=None)
    if not source_delivery:
        return packets
    merged = dict(packets)
    merged["delivery"] = source_delivery
    return merged


def _add_cross_stage_qa_review(
    task_id: str,
    packets: dict[str, dict[str, Any]],
    *,
    event_log: EventLog,
    persona_id: str | None,
) -> dict[str, dict[str, Any]]:
    """Carry QA's latest review back to the implementation stage.

    QA verdicts are recorded on the QA release stage, but Dev recovery resumes
    on the implementation/investigation stage. Without this carryback, Dev sees
    a generic "needs fixes" state but not QA's concrete remaining gaps.
    """

    if packets.get("qa_review"):
        return packets
    if not str(persona_id or "").strip():
        return packets
    source_review = latest_packet(task_id, "qa_review", event_log=event_log, stage_id=None)
    if not source_review:
        return packets
    merged = dict(packets)
    merged["qa_review"] = source_review
    return merged


def _packet_targets_persona(body: dict[str, Any], persona_id: str | None) -> bool:
    target = str(body.get("target_owner") or body.get("next_owner") or "").strip()
    persona = str(persona_id or "").strip()
    return bool(persona and target == persona)


#: Event types this builder will surface in the tick context. S15 de-registered
#: the other seven rows this set used to carry (``proof.attached``,
#: ``task.transition`` / ``.blocked`` / ``.unblocked``,
#: ``qa.coordination_released``, and the two ``cross_stack.*`` rows): their
#: producers went with the mission lane, so no surviving code can emit them and
#: ``EventLog.append`` now refuses them. ``tests/agent_runtime/
#: test_s19_context_builder_cluster_removal.py`` keeps this set a subset of the
#: registry-derived ``ALLOWED_EVENT_TYPES``, so a row can never again advertise
#: a shape no reader will see.
_RECENT_CONTEXT_EVENT_TYPES = frozenset({"packet.recorded"})


def _recent_relevant_events(
    task_id: str,
    *,
    event_log: EventLog,
    persona_id: str | None,
    stage_id: str | None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for event in reversed(event_log.for_task(task_id, limit=0)):
        projected = _safe_event_projection(event, persona_id=persona_id, stage_id=stage_id)
        if not projected:
            continue
        selected.append(projected)
        if len(selected) >= limit:
            break
    return list(reversed(selected))


def _safe_event_projection(event: Event, *, persona_id: str | None, stage_id: str | None) -> dict[str, Any] | None:
    if event.type not in _RECENT_CONTEXT_EVENT_TYPES:
        return None
    # ``packet.recorded`` is the only surviving selected type, so this is the
    # only projection shape: the proof arm and the generic transition arm both
    # keyed on types S15 de-registered.
    payload = event.payload if isinstance(event.payload, dict) else {}
    packet = _safe_packet_projection(payload)
    if not packet:
        return None
    if not _packet_event_relevant(packet, persona_id=persona_id, stage_id=stage_id):
        return None
    return {
        "type": event.type,
        "ts": str(event.ts) if event.ts is not None else None,
        "run_id": str(event.run_id or "")[:120] or None,
        "persona_id": str(event.persona_id or "")[:120] or None,
        "payload": {"packet": packet},
    }


def _packet_event_relevant(packet: dict[str, Any], *, persona_id: str | None, stage_id: str | None) -> bool:
    packet_stage = packet.get("stage_id")
    body = packet.get("body") if isinstance(packet.get("body"), dict) else {}
    if packet_stage in {None, stage_id}:
        return True
    if str(body.get("packet_kind") or "") == "contract_join" and _packet_targets_persona(body, persona_id):
        return True
    if packet.get("packet_type") == "delivery" and str(body.get("next_owner") or "") == str(persona_id or ""):
        return True
    return False


def _truncate_packet_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _truncate_packet_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_packet_values(item) for item in value[:20]]
    if isinstance(value, str):
        return value[:500]
    return value


def _safe_finding(item: dict[str, Any]) -> dict[str, str]:
    allowed = ("severity", "issue", "required_fix")
    return {key: str(item.get(key, ""))[:1000] for key in allowed if item.get(key) is not None}
