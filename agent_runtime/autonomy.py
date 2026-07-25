from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .context_builder import AgentContext
from .decision_contract_registry import CONTRACT_SCHEMA_VERSION, contract_hash
from .events import EventLog
from .mission_plan import current_plan_stage
from .models import AgentPersona, Event
from .personas import role_from_persona
from .proof_recipes import RECIPES
from .redaction import ENV_SECRET_ASSIGNMENT_RE
from .repo_context import safe_affected_repo_labels
from .serde import to_jsonable
from .stage_intent import no_product_edit_recipe_id, stage_requires_product_edit
from .store import RunStore

DEFAULT_READ_SEARCH_LIMIT = 4
DEV_READ_SEARCH_LIMIT = 6
FOCUSED_DEV_READ_SEARCH_LIMIT = 24
DEFAULT_PROOF_RETRY_LIMIT = 1
DEFAULT_PROOF_COMMAND_LIMIT = 1


def record_autonomy_packet(
    persona: AgentPersona,
    ctx: AgentContext,
    *,
    event_log: EventLog | None = None,
    run_store: RunStore | None = None,
) -> dict[str, Any]:
    """Persist the Harness-owned autonomy/context receipt before model work starts.

    The packet is public operational context, not chain-of-thought. It records
    bounded skill/tool/proof intent and a receipt for the log/proof context the
    run is absorbing, while raw logs remain in their original runtime artifacts.
    """

    event_log = event_log or EventLog()
    run_store = run_store or RunStore(event_log=event_log)
    sequence = _next_packet_sequence(ctx.task.id, persona.id, ctx.run.id)
    context_receipt_id = f"ctxr_{_safe_token(ctx.run.id)}_{sequence}"
    packet_id = f"auto_{_safe_token(ctx.run.id)}_{sequence}"
    budget = _inspection_budget(persona, ctx)
    selected_skills, rejected_skills = _skill_selection(persona, ctx, budget["skill_load_limit"])
    generated_at = now()
    stage_self_heal = _stage_self_heal_state(ctx)
    failed_proof_ids = _safe_failed_proof_ids(stage_self_heal)
    packet = {
        "schema_version": 1,
        "decision_contract_version": CONTRACT_SCHEMA_VERSION,
        "decision_contract_hash": contract_hash(),
        "autonomy_packet_id": packet_id,
        "context_receipt_id": context_receipt_id,
        "generated_at": generated_at,
        "agent": persona.id,
        "role": str(role_from_persona(persona)),
        "task_id": ctx.task.id,
        "run_id": ctx.run.id,
        "stage_id": ctx.run.stage_id or ctx.task.current_stage_id,
        "goal_read": _goal_read(ctx),
        "role_scope": _role_scope(persona, ctx),
        "proof_strategy": _proof_strategy(persona, ctx),
        "available_proof_recipes": _available_proof_recipes(persona, ctx),
        "selected_skills": selected_skills,
        "rejected_skills": rejected_skills,
        "inspection_budget": budget,
        "self_heal_plan": _self_heal_plan(persona, ctx),
        "handoff_shape": _handoff_shape(persona, ctx),
        "mission_hud": _compact_mission_hud(ctx.mission_hud),
        "environment_fingerprint_status": _environment_fingerprint_status(ctx),
        "raw_logs_preserved_in": "events.jsonl",
        "raw_proofs_preserved_under": "proofs/<task_id>/",
    }
    if failed_proof_ids:
        packet["failed_proof_ids"] = failed_proof_ids
    receipt = _context_receipt(ctx, packet=packet, generated_at=generated_at)
    packet["context_summary_hash"] = receipt["summary_hash"]
    packet["context_event_count"] = receipt["source_event_count"]
    packet["context_proof_count"] = receipt["proof_count"]
    packet["context_incident_count"] = receipt["incident_count"]

    root = context_artifact_dir(ctx.task.id, persona.id)
    root.mkdir(parents=True, exist_ok=True)
    _append_jsonl(root / "autonomy_packets.jsonl", packet)
    _append_jsonl(root / "absorbed_logs.jsonl", _absorbed_logs_record(ctx, packet=packet, receipt=receipt, generated_at=generated_at))
    _append_jsonl(root / "compression_receipts.jsonl", receipt)
    atomic_json_write(root / "context_summary.json", _context_summary(ctx, packet=packet, receipt=receipt), indent=2, sort_keys=True)
    (root / "context_summary.md").write_text(_context_summary_markdown(ctx, packet=packet, receipt=receipt), encoding="utf-8")

    ctx.autonomy_packet = _prompt_packet(packet)
    _record_run_progress(run_store, ctx, persona=persona, packet=packet, receipt=receipt, event_log=event_log)
    return ctx.autonomy_packet


def context_artifact_dir(task_id: str, persona_id: str) -> Path:
    return paths.store_root() / "context" / _safe_token(task_id) / _safe_token(persona_id)


def _next_packet_sequence(task_id: str, persona_id: str, run_id: str) -> int:
    path = context_artifact_dir(task_id, persona_id) / "autonomy_packets.jsonl"
    if not path.exists():
        return 1
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("run_id") == run_id:
                count += 1
    except (OSError, json.JSONDecodeError):
        return 1
    return count + 1


def _inspection_budget(persona: AgentPersona, ctx: AgentContext) -> dict[str, int]:
    role = str(role_from_persona(persona))
    visual = bool(ctx.task.requires_visual_proof or (ctx.current_stage and ctx.current_stage.requires_visual_proof))
    cross_stack = _is_cross_stack(ctx)
    if role == "alice_supervisor":
        read_limit = 3
        skill_limit = 1
    elif role == "qa":
        read_limit = DEFAULT_READ_SEARCH_LIMIT
        skill_limit = 2 if visual else 1
    else:
        if _is_no_edit_context_stage(ctx):
            read_limit = 2
            skill_limit = 1
        else:
            read_limit = FOCUSED_DEV_READ_SEARCH_LIMIT if _has_focused_stage_lane(ctx) else DEV_READ_SEARCH_LIMIT
            skill_limit = 3 if visual or cross_stack else 2
    return {
        "read_search_limit": read_limit,
        "proof_retry_limit": DEFAULT_PROOF_RETRY_LIMIT,
        "proof_command_limit": DEFAULT_PROOF_COMMAND_LIMIT,
        "skill_load_limit": skill_limit,
    }


def _has_focused_stage_lane(ctx: AgentContext) -> bool:
    stage = ctx.current_stage
    if stage is None:
        return False
    affected_paths = [str(item).strip() for item in (stage.affected_paths or []) if str(item).strip()]
    test_plan = [str(item).strip() for item in (stage.test_plan or []) if str(item).strip()]
    if not affected_paths or not test_plan:
        return False
    if len(affected_paths) > 6 or len(test_plan) > 4:
        return False
    return True


def _skill_selection(persona: AgentPersona, ctx: AgentContext, limit: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    available = _unique_strings(getattr(persona, "skills", []) or [])
    priorities = _skill_priorities(persona, ctx)
    selected: list[dict[str, str]] = []
    selected_ids: set[str] = set()
    for skill_id, reason in priorities:
        if skill_id in available and skill_id not in selected_ids:
            selected.append({"id": skill_id, "reason": reason})
            selected_ids.add(skill_id)
        if len(selected) >= limit:
            break
    if not selected and available:
        selected.append({"id": available[0], "reason": "persona default skill; load only if active stage requires it"})
        selected_ids.add(available[0])
    rejected = [
        {
            "id": skill_id,
            "reason": "not selected for this packet; use skill_search only if the current stage creates a specific need",
        }
        for skill_id in available
        if skill_id not in selected_ids
    ][:12]
    return selected, rejected


def _skill_priorities(persona: AgentPersona, ctx: AgentContext) -> list[tuple[str, str]]:
    persona_id = str(persona.id)
    role = str(role_from_persona(persona))
    visual = bool(ctx.task.requires_visual_proof or (ctx.current_stage and ctx.current_stage.requires_visual_proof))
    cross_stack = _is_cross_stack(ctx)
    text = _task_haystack(ctx)
    if role == "alice_supervisor":
        self_heal = getattr(ctx.task, "harness_self_heal", None)
        root_node_mode = bool(
            isinstance(self_heal, dict) and self_heal.get("root_node_mode")
        )
        if root_node_mode:
            return [("harness-mission-lead", "scope, route, join proofs, and deliver final alternatives after completion")]
        return [("harness-runtime-model", "inspect and operate the standard Mission Control surface")]
    if role == "qa":
        priorities = [("harness-qa-verdict", "evidence-backed approval or blocker verdict")]
        if visual:
            priorities.append(("launcher-stagec-mcp-screenshot", "visual claim requires fullscreen screenshot proof"))
        return priorities
    if persona_id == "backend_dev":
        if _is_no_edit_context_stage(ctx):
            return [("harness-dev-delivery", "bounded no-edit Dev decision shape")]
        priorities = [
            ("harness-dev-delivery", "bounded implementation/proof delivery shape"),
            ("eternia-backend-tests", "backend-focused proof command selection"),
        ]
        if cross_stack:
            priorities.append(("frontend-backend-contract-handoff", "backend-to-frontend contract handoff"))
        priorities.extend(
            [
                ("systematic-debugging", "single bounded retry after proof-backed failure"),
                ("test-driven-development", "red/green stage when explicitly requested"),
            ]
        )
        return priorities
    needs_launcher_analyze = _needs_launcher_analyze_proof_skill(text)
    if _is_no_edit_context_stage(ctx):
        return [("harness-dev-delivery", "bounded no-edit Dev decision shape")]
    priorities = [("harness-dev-delivery", "bounded implementation/proof delivery shape")]
    if needs_launcher_analyze:
        priorities.append(("launcher-analyze-proof", "focused Launcher analyze and contract proof command selection"))
    else:
        priorities.append(("eternia-launcher-workflow", "Launcher repo conventions and focused Flutter proof"))
    if visual:
        priorities.append(("launcher-stagec-mcp-screenshot", "user-visible UI change requires fullscreen visual proof"))
    if cross_stack:
        priorities.append(("frontend-backend-contract-handoff", "consume backend contract proof without rediscovery"))
    if needs_launcher_analyze:
        priorities.append(("eternia-launcher-workflow", "Launcher repo conventions and focused Flutter proof"))
    priorities.extend(
        [
            ("flutter-ui-development", "Flutter widget/state work when UI code is in scope"),
            ("systematic-debugging", "single bounded retry after proof-backed failure"),
        ]
    )
    return priorities


def _context_receipt(ctx: AgentContext, *, packet: dict[str, Any], generated_at) -> dict[str, Any]:
    events = [event for event in ctx.recent_events if isinstance(event, dict)]
    event_ts = [str(event.get("ts") or event.get("timestamp") or "") for event in events if event.get("ts") or event.get("timestamp")]
    proof_ids = _unique_strings(ctx.proof_ids)
    incident_ids = _unique_strings(item.get("id") for item in ctx.incident_records if isinstance(item, dict))
    summary = {
        "task_id": ctx.task.id,
        "run_id": ctx.run.id,
        "stage_id": ctx.run.stage_id or ctx.task.current_stage_id,
        "persona_id": ctx.run.persona_id,
        "state": str(ctx.task.state.value if hasattr(ctx.task.state, "value") else ctx.task.state),
        "proof_count": len(proof_ids),
        "incident_count": len(incident_ids),
        "event_count": len(events),
        "packet_flags": _packet_flags(ctx),
    }
    summary_hash = hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    text_estimate = len(json.dumps(summary, sort_keys=True, default=str)) + sum(len(str(event.get("type") or "")) for event in events)
    return {
        "schema_version": 1,
        "decision_contract_version": packet.get("decision_contract_version"),
        "decision_contract_hash": packet.get("decision_contract_hash"),
        "context_receipt_id": packet["context_receipt_id"],
        "autonomy_packet_id": packet["autonomy_packet_id"],
        "generated_at": generated_at,
        "run_id": ctx.run.id,
        "task_id": ctx.task.id,
        "persona_id": ctx.run.persona_id,
        "stage_id": ctx.run.stage_id or ctx.task.current_stage_id,
        "source_event_count": len(events),
        "source_event_first_ts": event_ts[0] if event_ts else None,
        "source_event_last_ts": event_ts[-1] if event_ts else None,
        "proof_count": len(proof_ids),
        "incident_count": len(incident_ids),
        "packet_flags": _packet_flags(ctx),
        "estimated_tokens": max(1, text_estimate // 4),
        "summary_hash": summary_hash,
        "dropped_fields": [
            "raw_model_output",
            "absolute_paths",
            "full_proof_artifacts",
            "full_event_payloads",
            "secret-like values",
        ],
    }


def _absorbed_logs_record(ctx: AgentContext, *, packet: dict[str, Any], receipt: dict[str, Any], generated_at) -> dict[str, Any]:
    event_handles = []
    for event in ctx.recent_events[:20]:
        if not isinstance(event, dict):
            continue
        event_handles.append(
            {
                "type": _safe_text(event.get("type")),
                "run_id": _safe_token_or_none(event.get("run_id")),
                "persona_id": _safe_token_or_none(event.get("persona_id")),
                "ts": _safe_text(event.get("ts") or event.get("timestamp")),
            }
        )
    return {
        "schema_version": 1,
        "absorbed_at": generated_at,
        "context_receipt_id": packet["context_receipt_id"],
        "autonomy_packet_id": packet["autonomy_packet_id"],
        "run_id": ctx.run.id,
        "task_id": ctx.task.id,
        "persona_id": ctx.run.persona_id,
        "raw_log_ref": "events.jsonl",
        "source_event_count": receipt["source_event_count"],
        "source_event_first_ts": receipt["source_event_first_ts"],
        "source_event_last_ts": receipt["source_event_last_ts"],
        "event_handles": [item for item in event_handles if item],
    }


def _context_summary(ctx: AgentContext, *, packet: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "context_receipt_id": receipt["context_receipt_id"],
        "autonomy_packet_id": packet["autonomy_packet_id"],
        "task_id": ctx.task.id,
        "run_id": ctx.run.id,
        "persona_id": ctx.run.persona_id,
        "stage_id": ctx.run.stage_id or ctx.task.current_stage_id,
        "goal_read": packet["goal_read"],
        "proof_strategy": packet["proof_strategy"],
        "selected_skill_ids": [item["id"] for item in packet["selected_skills"]],
        "inspection_budget": packet["inspection_budget"],
        "recommended_action": ((packet.get("mission_hud") or {}).get("agent_hud") or {}).get("recommended_action"),
        "decision_choice_ids": [
            item.get("shape_id")
            for item in (((packet.get("mission_hud") or {}).get("agent_hud") or {}).get("options") or [])
            if isinstance(item, dict) and item.get("shape_id")
        ][:12],
        "proof_ids": _unique_strings(ctx.proof_ids)[:20],
        "incident_ids": _unique_strings(item.get("id") for item in ctx.incident_records if isinstance(item, dict))[:10],
        "summary_hash": receipt["summary_hash"],
    }


def _context_summary_markdown(ctx: AgentContext, *, packet: dict[str, Any], receipt: dict[str, Any]) -> str:
    selected = ", ".join(item["id"] for item in packet["selected_skills"]) or "none"
    budget = packet["inspection_budget"]
    lines = [
        f"# Agent Context Summary: {ctx.run.persona_id}",
        "",
        f"- task_id: {ctx.task.id}",
        f"- run_id: {ctx.run.id}",
        f"- stage_id: {ctx.run.stage_id or ctx.task.current_stage_id or ''}",
        f"- autonomy_packet_id: {packet['autonomy_packet_id']}",
        f"- context_receipt_id: {receipt['context_receipt_id']}",
        f"- generated_at: {to_jsonable(receipt['generated_at'])}",
        f"- goal_read: {packet['goal_read']}",
        f"- proof_strategy: {packet['proof_strategy']}",
        f"- recommended_action: {_next_move_markdown(packet)}",
        f"- selected_skills: {selected}",
        f"- read_search_limit: {budget['read_search_limit']}",
        f"- proof_retry_limit: {budget['proof_retry_limit']}",
        f"- proof_command_limit: {budget['proof_command_limit']}",
        f"- skill_load_limit: {budget['skill_load_limit']}",
        f"- source_event_count: {receipt['source_event_count']}",
        f"- proof_count: {receipt['proof_count']}",
        f"- incident_count: {receipt['incident_count']}",
        f"- summary_hash: {receipt['summary_hash']}",
        "",
        "Raw logs and proof artifacts are preserved in the runtime store; this file is a redaction-safe receipt.",
    ]
    return "\n".join(lines) + "\n"


def _prompt_packet(packet: dict[str, Any]) -> dict[str, Any]:
    budget = packet["inspection_budget"]
    prompt = {
        "autonomy_packet_id": packet["autonomy_packet_id"],
        "context_receipt_id": packet["context_receipt_id"],
        "goal_read": packet["goal_read"],
        "role_scope": packet["role_scope"],
        "proof_strategy": packet["proof_strategy"],
        "available_proof_recipes": packet.get("available_proof_recipes", []),
        "selected_skills": packet["selected_skills"],
        "rejected_skill_count": len(packet["rejected_skills"]),
        "inspection_budget": budget,
        "self_heal_plan": packet["self_heal_plan"],
        "handoff_shape": packet["handoff_shape"],
        "mission_hud": packet.get("mission_hud") or {},
        "environment_fingerprint_status": packet.get("environment_fingerprint_status"),
    }
    if packet.get("failed_proof_ids"):
        prompt["failed_proof_ids"] = packet["failed_proof_ids"]
    return prompt


def _compact_mission_hud(hud: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(hud, dict) or not hud:
        return {}
    compact = {
        "agent_hud": _compact_agent_hud(hud.get("agent_hud")),
        "terminal_feedback": hud.get("terminal_feedback"),
        "validation_repair": hud.get("validation_repair"),
        "failed_proof_ids": hud.get("failed_proof_ids"),
        "current_stage_command_hints": hud.get("current_stage_command_hints"),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _compact_agent_hud(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "schema_version",
        "mode",
        "stage",
        "simplified_phase",
        "current_assignment",
        "repo_bundles",
        "bundle_queue",
        "qa_waiting_on",
        "contract",
        "response_rule",
        "options",
        "context_options",
        "recommended_action",
    }
    return {key: _safe_prompt_value(value.get(key)) for key in allowed if value.get(key) not in (None, "", [], {})}


def _safe_prompt_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_prompt_value(item) for key, item in value.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [_safe_prompt_value(item) for item in value if item not in (None, "", [], {})]
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _compact_decision_menu(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    menu: list[dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        menu.append(
            {
                key: item.get(key)
                for key in (
                    "choice_id",
                    "primary",
                    "shape_id",
                    "label",
                    "decision_type",
                    "when",
                    "required_payload_keys",
                    "allowed_payload_keys",
                    "nested_required",
                    "enum_choices",
                    "forbid_unknown_payload_keys",
                    "recommended_payload",
                )
                if item.get(key) not in (None, "", [], {})
            }
        )
    return menu


def _next_move_markdown(packet: dict[str, Any]) -> str:
    hud = packet.get("mission_hud") if isinstance(packet.get("mission_hud"), dict) else {}
    agent_hud = hud.get("agent_hud") if isinstance(hud, dict) and isinstance(hud.get("agent_hud"), dict) else {}
    next_move = agent_hud.get("recommended_action") if isinstance(agent_hud, dict) else None
    if not isinstance(next_move, dict):
        return "none"
    decision = str(next_move.get("decision_type") or "unknown")
    shape_id = str(next_move.get("shape_id") or "unknown")
    reason = str(next_move.get("reason") or "").strip()
    return f"{decision} via {shape_id}" + (f" ({reason[:120]})" if reason else "")


def _record_run_progress(
    run_store: RunStore,
    ctx: AgentContext,
    *,
    persona: AgentPersona,
    packet: dict[str, Any],
    receipt: dict[str, Any],
    event_log: EventLog,
) -> None:
    budget = packet["inspection_budget"]
    payload = {
        "type": "run.progress",
        "phase": "autonomy",
        "severity": "info",
        "step": "autonomy_packet",
        "status": "ready",
        "summary": "Autonomy packet ready; tool budgets recorded.",
        "autonomy_packet_id": packet["autonomy_packet_id"],
        "context_receipt_id": packet["context_receipt_id"],
        "selected_skill_count": len(packet["selected_skills"]),
        "rejected_skill_count": len(packet["rejected_skills"]),
        "read_search_limit": budget["read_search_limit"],
        "proof_retry_limit": budget["proof_retry_limit"],
        "proof_command_limit": budget["proof_command_limit"],
        "skill_load_limit": budget["skill_load_limit"],
        "context_event_count": receipt["source_event_count"],
        "context_proof_count": receipt["proof_count"],
        "context_incident_count": receipt["incident_count"],
        "context_size_estimate": receipt["estimated_tokens"],
        "environment_fingerprint_status": packet.get("environment_fingerprint_status"),
        "next_expected": "bounded_agent_work",
    }
    if packet.get("failed_proof_ids"):
        payload["last_failed_proof_ids"] = packet["failed_proof_ids"]
    try:
        run = run_store.get(ctx.run.id)
        run.progress = {**(run.progress or {}), **payload}
        run.last_heartbeat_at = now()
        run_store.update(run)
    except Exception:
        pass
    event_log.append(Event(now(), "run.progress", ctx.task.id, ctx.run.id, persona.id, payload))


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _goal_read(ctx: AgentContext) -> str:
    parts = [ctx.task.title, ctx.task.description]
    if ctx.current_stage:
        parts.extend([ctx.current_stage.title, ctx.current_stage.objective])
    safe = _safe_text(" ".join(part for part in parts if part))
    return safe[:240] or "Carry the current Harness mission to a proof-backed terminal outcome."


def _role_scope(persona: AgentPersona, ctx: AgentContext) -> str:
    role = str(role_from_persona(persona))
    if role == "alice_supervisor":
        return "Own scoping, routing, proof joins, bounded recovery, and final alternatives after completion; do not patch code."
    if role == "qa":
        return "Own independent evidence review and approval/blocker verdicts; do not patch code."
    labels = safe_affected_repo_labels(list(getattr(ctx.task, "affected_repos", []) or []))
    repo = str(getattr(persona, "repo_scope_label", "") or (labels[0] if labels else "assigned repo"))
    return f"Own implementation and focused proof for {repo}; avoid unrelated repos unless Neko handoff requires cross-stack work."


def _proof_strategy(persona: AgentPersona, ctx: AgentContext) -> str:
    role = str(role_from_persona(persona))
    visual = bool(ctx.task.requires_visual_proof or (ctx.current_stage and ctx.current_stage.requires_visual_proof))
    if role == "alice_supervisor":
        return "Choose the cheapest routing mode, join existing proof IDs, and route only when the next owner has enough context."
    if role == "qa":
        if visual:
            return "Review command proof plus fullscreen visual proof before approval; block if required visual evidence is missing."
        return "Review attached proof IDs and approve only evidence-backed implementation claims."
    if str(persona.id) == "backend_dev":
        return "Inspect narrowly, patch if needed, then request one focused backend command proof."
    if visual:
        return "Inspect narrowly, patch if needed, then request focused Flutter proof plus fullscreen Stage C screenshot when claiming UI behavior."
    return "Inspect narrowly, patch if needed, then request one focused command proof."


def _available_proof_recipes(persona: AgentPersona, ctx: AgentContext) -> list[dict[str, str]]:
    text = _task_haystack(ctx)
    persona_id = str(getattr(persona, "id", "") or "")
    selected: list[str] = []
    stage_id = str(getattr(ctx.run, "stage_id", None) or getattr(ctx.task, "current_stage_id", None) or "").strip()
    current_stage = next((stage for stage in getattr(ctx.task, "stages", []) or [] if stage.id == stage_id), None) if stage_id else None
    product_edit_scope = stage_requires_product_edit(ctx.task, current_stage)
    if "archive" in text or "mission control" in text or "harness" in text:
        selected.extend(["archive_button_cli_contract", "harness_runtime_status_snapshot"])
    if "backend" in text or persona_id == "backend_dev":
        selected.append("backend_contract_smoke")
    if any(marker in text for marker in ("launcher", "frontend", "flutter", "contract", "mission control")) or persona_id == "dev":
        selected.append("launcher_contract_smoke")
    if persona_id == "qa" or "qa" in text:
        selected.append("qa_release_verdict_smoke")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for recipe_id in selected:
        if recipe_id in seen or recipe_id not in RECIPES:
            continue
        if product_edit_scope and no_product_edit_recipe_id(recipe_id):
            continue
        seen.add(recipe_id)
        recipe = RECIPES[recipe_id]
        result.append({"recipe_id": recipe.id, "repo_scope": recipe.repo_scope or "", "mode": recipe.mode})
    return result[:4]


def _self_heal_plan(persona: AgentPersona, ctx: AgentContext) -> str:
    progress = ctx.run.progress if isinstance(getattr(ctx.run, "progress", None), dict) else {}
    if str(progress.get("loop_warning") or "") == "read_search_without_patch_threshold":
        read_count = progress.get("read_search_count")
        read_limit = progress.get("read_search_limit")
        return (
            f"Read/search budget was already exhausted without a patch ({read_count}/{read_limit}); "
            "do not perform more discovery unless an exact missing file path is named. "
            "Make the smallest safe patch, request the focused test plan, or block with the exact missing prerequisite."
        )
    fingerprint_status = _environment_fingerprint_status(ctx)
    if fingerprint_status == "unchanged":
        return "Do not repeat the same proof command; block or ask Neko for cannot_self_heal unless code/config/task signal changed."
    return "Apply one bounded local correction after proof-backed failure, reuse failed proof IDs, and escalate if the next signal is unchanged."


def _handoff_shape(persona: AgentPersona, ctx: AgentContext) -> str:
    if _simplified_contract_active():
        role = str(role_from_persona(persona))
        if role == "alice_supervisor":
            return "scope_route with objective, acceptance criteria, target owner/repo, and proof gate."
        if role == "qa":
            return "qa_verdict with verdict, cited proof IDs, findings, and remaining risk."
        return "hand_off with concise summary; Harness captures isolated-worktree diff and runs the authoritative gate or proof recipe."
    role = str(role_from_persona(persona))
    if role == "alice_supervisor":
        return "handoff_packet with target owner/repo, proof gate, join gate, and next expected owner."
    if role == "qa":
        return "qa_verdict with verdict, proof IDs, findings, remaining risk, and autonomy packet IDs reviewed."
    return "hand_off with concise summary and known gaps; Harness derives changed files, proof IDs, delivery, and next graph owner."


def _simplified_contract_active() -> bool:
    try:
        from .config import load_root_runtime_config

        cfg = load_root_runtime_config()
    except Exception:
        return False
    simplified = getattr(cfg, "simplified_agent_contract", None)
    return bool(
        getattr(simplified, "enabled", False)
        and getattr(simplified, "expose_only_simplified_actions", True)
    )


def _environment_fingerprint_status(ctx: AgentContext) -> str:
    state = _stage_self_heal_state(ctx)
    status = state.get("environment_fingerprint_status") if isinstance(state, dict) else None
    if isinstance(status, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", status):
        return status
    if state.get("last_environment_fingerprint") or state.get("environment_fingerprint"):
        return "recorded"
    return "unknown"


def _stage_self_heal_state(ctx: AgentContext) -> dict[str, Any]:
    root = getattr(ctx.task, "harness_self_heal", None)
    if not isinstance(root, dict):
        return {}
    stages = root.get("stages") if isinstance(root.get("stages"), dict) else root
    stage_key = ctx.run.stage_id or ctx.task.current_stage_id or "_mission"
    state = stages.get(stage_key) if isinstance(stages, dict) else {}
    return state if isinstance(state, dict) else {}


def _safe_failed_proof_ids(state: dict[str, Any]) -> list[str]:
    values = state.get("last_failed_proof_ids") if isinstance(state, dict) else None
    if not isinstance(values, list):
        return []
    safe: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,220}", text):
            safe.append(text)
    return safe[:8]


def _packet_flags(ctx: AgentContext) -> dict[str, bool]:
    return {
        "latest_handoff_packet": bool(ctx.latest_handoff_packet),
        "latest_delivery": bool(ctx.latest_delivery),
        "latest_qa_review": bool(ctx.latest_qa_review),
        "mission_hud": bool(ctx.mission_hud),
        "requires_repair": bool(ctx.requires_repair),
    }


def _is_cross_stack(ctx: AgentContext) -> bool:
    text = _task_haystack(ctx)
    markers = ("backend", "launcher", "frontend", "front-end", "cross-stack", "contract", "ui")
    return ("backend" in text and any(marker in text for marker in ("launcher", "frontend", "front-end", "ui"))) or "cross-stack" in text or "contract" in text


def _is_no_edit_context_stage(ctx: AgentContext) -> bool:
    stage = current_plan_stage(ctx.task) or ctx.current_stage
    if stage is None:
        return False
    return str(getattr(stage, "kind", "") or "") == "context" and not stage_requires_product_edit(ctx.task, stage)


def _needs_launcher_analyze_proof_skill(text: str) -> bool:
    if not any(marker in text for marker in ("launcher", "mission control", "mission_control", "flutter", "marionette", "stage c")):
        return False
    markers = (
        "flutter analyze",
        "launcher_contract_smoke",
        "contract smoke",
        "contract proof",
        "contract-consumption",
        "contract consumption",
        "static analysis",
        "no-edit",
        "no product edit",
        "no-product-edit",
        "burn-in",
        "stage 47",
        "main_marionette",
        "mission_control_actions_test",
        "mission_control_bridge",
    )
    return any(marker in text for marker in markers)


def _task_haystack(ctx: AgentContext) -> str:
    values: list[str] = [
        ctx.task.title,
        ctx.task.description,
        " ".join(ctx.task.acceptance_criteria or []),
        " ".join(ctx.task.risk_flags or []),
        " ".join(safe_affected_repo_labels(list(ctx.task.affected_repos or []))),
    ]
    if ctx.current_stage:
        values.extend(
            [
                ctx.current_stage.id,
                ctx.current_stage.title,
                ctx.current_stage.objective,
                " ".join(ctx.current_stage.test_plan or []),
                " ".join(ctx.current_stage.acceptance_criteria or []),
            ]
        )
    return " ".join(_safe_text(value).lower() for value in values if value)


def _unique_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_token(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_")[:80] or "item"


def _safe_token_or_none(value: Any) -> str | None:
    if not value:
        return None
    text = _safe_token(value)
    return text if text != "item" else None


def _safe_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    # Single-homed in ``agent_runtime.redaction`` — see the header there for the
    # JSON blind spot every local spelling shared. group(1) is still the full
    # key, so the ``\1=[REDACTED]`` rebuild is unchanged.
    text = ENV_SECRET_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{12,}", "bearer [REDACTED]", text)
    text = re.sub(r"(?i)\b[A-Z]:(?:[\\/]+[^\"'<>|\r\n]+)+", lambda match: _path_label(match.group(0)), text)
    text = re.sub(r"(?i)\b[A-Z]:(?:[\\/]+[^\\/\s\"'<>|:]+)+", lambda match: _path_label(match.group(0)), text)
    text = re.sub(r"(?<![\w.-])/(?:Users|home|mnt|opt|var|tmp|Volumes)/(?:[^\s\"'<>|:]+/?)+", lambda match: _path_label(match.group(0)), text)
    return text[:1000]


def _path_label(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1] or "path"
    return f"<path:{_safe_token(name)}>"
