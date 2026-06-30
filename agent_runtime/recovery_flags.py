from __future__ import annotations

from typing import Any

from .events import EventLog


NEKO_BLOCK_RECOVERY_ATTEMPTED_FLAG = "neko_block_recovery_attempted"
MISSION_SELF_HEAL_STAGE = "_mission"
BLOCK_RECOVERY_SIGNAL_KEY = "last_block_recovery_signal"
INCIDENT_CLOSE_COUNTER_KEY = "incident_close_counter"


def block_recovery_attempted_for_current_signal(task) -> bool:
    if NEKO_BLOCK_RECOVERY_ATTEMPTED_FLAG not in (getattr(task, "risk_flags", None) or []):
        return False
    state = mission_self_heal_state(task)
    if not state:
        return True
    return str(state.get(BLOCK_RECOVERY_SIGNAL_KEY) or "") == current_block_recovery_signal(task)


def mark_block_recovery_attempt(task) -> None:
    state = mission_self_heal_state(task, create=True)
    state[BLOCK_RECOVERY_SIGNAL_KEY] = current_block_recovery_signal(task)
    task.risk_flags = _dedupe(list(getattr(task, "risk_flags", []) or []), [NEKO_BLOCK_RECOVERY_ATTEMPTED_FLAG])


def mark_incident_closed_for_recovery(task, *, incident_id: str | None = None) -> None:
    state = mission_self_heal_state(task, create=True)
    state[INCIDENT_CLOSE_COUNTER_KEY] = _safe_int(state.get(INCIDENT_CLOSE_COUNTER_KEY)) + 1
    if incident_id:
        state["last_closed_incident_id"] = str(incident_id)[:128]


def current_block_recovery_signal(task) -> str:
    state = mission_self_heal_state(task)
    events = EventLog().for_task(getattr(task, "id", ""), limit=1000) if getattr(task, "id", None) else []
    packet_count = sum(1 for event in events if event.type == "packet.recorded")
    fulfilled_context_count = sum(
        1
        for req in (getattr(task, "context_requests", []) or [])
        if isinstance(req, dict) and req.get("status") in {"fulfilled", "fulfilled_partial"}
    )
    closed_incident_count = sum(1 for event in events if event.type == "incident.closed")
    parts = [
        str(state.get(INCIDENT_CLOSE_COUNTER_KEY) or 0),
        str(state.get("environment_fingerprint_status") or "unknown"),
        str(state.get("last_environment_fingerprint") or "none"),
        str(len(getattr(task, "proof_ids", []) or [])),
        str(packet_count),
        str(fulfilled_context_count),
        str(closed_incident_count),
    ]
    return ":".join(parts)


def mission_self_heal_state(task, *, create: bool = False) -> dict[str, Any]:
    root = getattr(task, "harness_self_heal", None)
    if not isinstance(root, dict):
        if not create:
            return {}
        root = {}
        task.harness_self_heal = root
    stages = root.get("stages")
    if not isinstance(stages, dict):
        if not create:
            return {}
        stages = {}
        root["stages"] = stages
    state = stages.get(MISSION_SELF_HEAL_STAGE)
    if not isinstance(state, dict):
        if not create:
            return {}
        state = {}
        stages[MISSION_SELF_HEAL_STAGE] = state
    return state


def _dedupe(existing: list[str], additions: list[str]) -> list[str]:
    seen = set(existing)
    result = list(existing)
    for item in additions:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _safe_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
