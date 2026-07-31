from __future__ import annotations

from typing import Any

from .incidents import RUN_BUDGET_EXCEEDED
from .states import RunState
from .store import RunStore


def budget_incident_can_continue(incident: Any, run_store: RunStore, *, cap: int = 2) -> bool:
    if getattr(incident, "closed_at", None) is not None:
        return False
    if getattr(incident, "kind", None) != RUN_BUDGET_EXCEEDED or not getattr(incident, "run_id", None):
        return False
    try:
        run = run_store.get(incident.run_id)
    except Exception:
        return False
    if run.state != RunState.WAITING_ON_APPROVAL or not run.session_id:
        return False
    if _run_stopped_on_read_search_loop_without_delivery(run):
        return False
    return approved_budget_continuation_count(run, run_store) < _safe_cap(cap)


def budget_incident_needs_scope_recovery(incident: Any, run_store: RunStore) -> bool:
    """Return true when Dev exhausted read/search without delivery and Neko must rescope.

    This is intentionally separate from same-session continuation approval. A
    read/search loop with no patch or proof is not safe to continue as-is, but
    it is still an automatic supervisor recovery case rather than a human stop.
    """
    if getattr(incident, "closed_at", None) is not None:
        return False
    if getattr(incident, "kind", None) != RUN_BUDGET_EXCEEDED or not getattr(incident, "run_id", None):
        return False
    try:
        run = run_store.get(incident.run_id)
    except Exception:
        return False
    if run.state != RunState.WAITING_ON_APPROVAL:
        return False
    return _run_stopped_on_read_search_loop_without_delivery(run)


def approved_budget_continuation_count(run: Any, run_store: RunStore) -> int:
    try:
        runs = run_store.list_for_task(run.task_id)
    except Exception:
        return 0
    count = 0
    for item in runs:
        if item.persona_id != run.persona_id or item.stage_id != run.stage_id:
            continue
        if run.session_id and item.session_id != run.session_id:
            continue
        error = item.error if isinstance(item.error, dict) else {}
        progress = item.progress if isinstance(item.progress, dict) else {}
        if error.get("type") != RUN_BUDGET_EXCEEDED:
            continue
        if error.get("approved_for_continuation") is True or progress.get("approved_for_continuation") is True:
            count += 1
    return count


def _safe_cap(value: int | str | None) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 2


def _run_stopped_on_read_search_loop_without_delivery(run: Any) -> bool:
    progress = run.progress if isinstance(getattr(run, "progress", None), dict) else {}
    if str(progress.get("loop_warning") or "") != "read_search_without_patch_threshold":
        return False
    patch_count = _safe_int(progress.get("patch_count"))
    proof_count = _safe_int(progress.get("proof_count"))
    if patch_count > 0 or proof_count > 0:
        return False
    read_count = _safe_int(progress.get("read_search_count"))
    read_limit = _safe_int(progress.get("read_search_limit"))
    return read_limit <= 0 or read_count >= read_limit


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
