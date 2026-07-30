from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .events import EventLog
from .locks import worker_session_lock
from .models import AgentPersona, AgentRun, Event, WorkerSession
from .serde import from_jsonable, to_jsonable
from .states import PossessionState, RunState, WorkerSessionState
from .store import ACTIVE_RUN_STATES, _safe_session_id

TERMINAL_WORKER_STATES = frozenset(
    {
        WorkerSessionState.COMPLETED,
        WorkerSessionState.BLOCKED,
        WorkerSessionState.CLOSED,
    }
)
ACTIVE_WORKER_STATES = frozenset(
    {
        WorkerSessionState.ASSIGNED,
        WorkerSessionState.RUNNING,
        WorkerSessionState.WAITING_ON_TOOL,
        WorkerSessionState.WAITING_ON_PROOF,
        WorkerSessionState.SELF_HEALING,
        WorkerSessionState.WAITING_ON_HUMAN,
        WorkerSessionState.POSSESSED,
    }
)


class WorkerSessionStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def open_or_resume(
        self,
        *,
        task_id: str,
        persona: AgentPersona,
        stage_id: str | None,
        session_id: str | None = None,
        goal_epoch: str | None = None,
        assignment_id: str | None = None,
    ) -> WorkerSession:
        existing = self.latest_for_task_persona(task_id=task_id, persona_id=persona.id, include_terminal=False)
        if existing is None:
            return self.open(
                task_id=task_id,
                persona=persona,
                stage_id=stage_id,
                session_id=session_id,
                goal_epoch=goal_epoch,
                assignment_id=assignment_id,
            )
        with worker_session_lock(existing.id):
            worker = self.get(existing.id)
            if worker.state in TERMINAL_WORKER_STATES:
                return worker
            worker.current_stage_id = stage_id or worker.current_stage_id
            if assignment_id:
                worker.current_assignment_id = _safe_token(assignment_id)
            worker.state = WorkerSessionState.ASSIGNED if worker.state == WorkerSessionState.IDLE else worker.state
            worker.last_heartbeat_at = now()
            if _safe_session_id(session_id) and not _safe_session_id(worker.session_id):
                worker.session_id = _safe_session_id(session_id)
            if goal_epoch and not worker.goal_epoch:
                worker.goal_epoch = _safe_token(goal_epoch)
            self._write(worker)
        self._event("worker_session.resumed", worker, {"stage_id": worker.current_stage_id, "session_id_present": bool(worker.session_id)})
        return self.get(worker.id)

    def open(
        self,
        *,
        task_id: str,
        persona: AgentPersona,
        stage_id: str | None,
        session_id: str | None = None,
        goal_epoch: str | None = None,
        assignment_id: str | None = None,
    ) -> WorkerSession:
        ts = now()
        worker = WorkerSession(
            id=f"worker_{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            persona_id=persona.id,
            role=str(persona.role),
            display_name=persona.display_name,
            state=WorkerSessionState.ASSIGNED,
            opened_at=ts,
            last_heartbeat_at=ts,
            goal_epoch=_safe_token(goal_epoch) if goal_epoch else None,
            current_stage_id=stage_id,
            current_assignment_id=_safe_token(assignment_id) if assignment_id else None,
            session_id=_safe_session_id(session_id),
            model=persona.model,
            provider=persona.provider,
            api_mode=persona.api_mode,
            prompt_contract_hash=_prompt_contract_hash(persona),
            skill_manifest_hash=_skill_manifest(
                persona, root_node_mode=_task_root_node_mode(task_id)
            )[0],
        )
        self._write(worker)
        _write_static_prompt_receipt(worker, persona)
        self._event("worker_session.opened", worker, {"stage_id": stage_id, "session_id_present": bool(worker.session_id)})
        self._event("worker_session.assigned", worker, {"stage_id": stage_id})
        return worker

    def get(self, worker_session_id: str) -> WorkerSession:
        path = paths.worker_session_path(worker_session_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return from_jsonable(WorkerSession, raw)

    def update(self, worker: WorkerSession) -> WorkerSession:
        with worker_session_lock(worker.id):
            if _safe_session_id(worker.session_id):
                worker.session_id = _safe_session_id(worker.session_id)
            else:
                worker.session_id = None
            self._write(worker)
        return self.get(worker.id)

    def list_all(self) -> list[WorkerSession]:
        directory = paths.worker_sessions_dir()
        if not directory.exists():
            return []
        workers = []
        for path in sorted(directory.glob("*.json")):
            try:
                workers.append(from_jsonable(WorkerSession, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(workers, key=lambda item: item.opened_at)

    def list_for_task(self, task_id: str) -> list[WorkerSession]:
        return [worker for worker in self.list_all() if worker.task_id == task_id]

    def find_active(self, *, task_id: str | None = None, persona_id: str | None = None) -> list[WorkerSession]:
        return [
            worker
            for worker in self.list_all()
            if _worker_is_active(worker)
            and (task_id is None or worker.task_id == task_id)
            and (persona_id is None or worker.persona_id == persona_id)
        ]

    def latest_for_task_persona(
        self,
        *,
        task_id: str,
        persona_id: str,
        include_terminal: bool = True,
    ) -> WorkerSession | None:
        workers = [
            worker
            for worker in self.list_all()
            if worker.task_id == task_id
            and worker.persona_id == persona_id
            and (include_terminal or worker.state not in TERMINAL_WORKER_STATES)
        ]
        if not workers:
            return None
        return max(workers, key=lambda item: item.last_heartbeat_at or item.opened_at)

    def reusable_session_id(self, *, task_id: str, persona_id: str) -> str | None:
        worker = self.latest_for_task_persona(task_id=task_id, persona_id=persona_id, include_terminal=True)
        if worker is None:
            return None
        return _safe_session_id(worker.session_id)

    def assign_run(self, worker_session_id: str, run: AgentRun) -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            if worker.state in TERMINAL_WORKER_STATES:
                return worker
            worker.active_run_id = run.id
            worker.current_stage_id = run.stage_id or worker.current_stage_id
            worker.session_id = _safe_session_id(run.session_id) or _safe_session_id(worker.session_id)
            worker.state = WorkerSessionState.RUNNING
            worker.last_heartbeat_at = now()
            self._write(worker)
        self._event("worker_session.assigned", worker, {"run_id": run.id, "stage_id": worker.current_stage_id})
        return self.get(worker.id)

    def record_context(self, worker_session_id: str, *, context_receipt_id: str | None) -> WorkerSession:
        if not context_receipt_id:
            return self.get(worker_session_id)
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            worker.context_receipt_id = _safe_token(context_receipt_id)
            worker.last_context_receipt_at = now()
            worker.last_heartbeat_at = now()
            self._write(worker)
        self._event("worker_session.context_absorbed", worker, {"context_receipt_id": worker.context_receipt_id})
        return self.get(worker.id)

    def update_after_run(
        self,
        worker_session_id: str,
        run: AgentRun,
        *,
        proof_ids_added: list[str] | None = None,
        close_reason: str | None = None,
        count_decision: bool = True,
    ) -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            worker.session_id = _safe_session_id(run.session_id) or _safe_session_id(worker.session_id)
            worker.current_stage_id = run.stage_id or worker.current_stage_id
            if count_decision:
                worker.decision_count += 1
            worker.proof_count += len(proof_ids_added or [])
            llm = run.llm if isinstance(run.llm, dict) else {}
            if count_decision:
                worker.token_budget_used += _safe_int(llm.get("total_tokens"))
                worker.tool_budget_used += _safe_int(llm.get("tool_turns"))
            progress = run.progress if isinstance(run.progress, dict) else {}
            if progress.get("loop_warning") or progress.get("severity") == "critical":
                worker.watchdog_warning_count += 1
            if progress.get("context_receipt_id"):
                worker.context_receipt_id = _safe_token(progress.get("context_receipt_id"))
                worker.last_context_receipt_at = now()
            if progress.get("environment_fingerprint_status"):
                worker.last_environment_fingerprint = _safe_token(progress.get("environment_fingerprint_status"))
            failed_ids = progress.get("last_failed_proof_ids")
            if isinstance(failed_ids, list) and failed_ids:
                worker.last_failed_proof_id = _safe_token(failed_ids[-1])
            if close_reason:
                worker.close_reason = _safe_text(close_reason)
            if run.state in {RunState.WAITING_ON_TOOL}:
                worker.state = WorkerSessionState.WAITING_ON_TOOL
                worker.active_run_id = run.id
            elif run.state in {RunState.WAITING_ON_APPROVAL}:
                worker.state = WorkerSessionState.WAITING_ON_HUMAN
                worker.active_run_id = run.id
            else:
                worker.state = WorkerSessionState.RUNNING if run.state in ACTIVE_RUN_STATES else WorkerSessionState.IDLE
                worker.active_run_id = run.id if run.state in ACTIVE_RUN_STATES else None
            worker.last_heartbeat_at = now()
            self._write(worker)
        return self.get(worker.id)

    def heartbeat(self, worker_session_id: str) -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            if worker.state not in TERMINAL_WORKER_STATES:
                worker.last_heartbeat_at = now()
                self._write(worker)
        self._event("worker_session.heartbeat", worker, {"state": worker.state.value})
        return self.get(worker.id)

    def record_watchdog_warning(self, worker_session_id: str, *, kind: str, summary: str) -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            if worker.state not in TERMINAL_WORKER_STATES:
                worker.watchdog_warning_count += 1
                worker.last_heartbeat_at = now()
                self._write(worker)
        self._event("worker_session.watchdog_warning", worker, {"kind": _safe_token(kind), "summary": _safe_text(summary)})
        return self.get(worker.id)

    def close(self, worker_session_id: str, *, reason: str, state: WorkerSessionState = WorkerSessionState.CLOSED) -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            if worker.state in TERMINAL_WORKER_STATES:
                return worker
            worker.state = state
            worker.closed_at = now()
            worker.active_run_id = None
            worker.close_reason = _safe_text(reason)
            if worker.possession_state != PossessionState.DISABLED:
                worker.possession_state = PossessionState.AVAILABLE
            worker.lease_owner = None
            worker.lease_expires_at = None
            worker.last_heartbeat_at = now()
            self._write(worker)
        self._event("worker_session.closed", worker, {"close_reason": worker.close_reason, "state": worker.state.value})
        return self.get(worker.id)

    def pause(self, worker_session_id: str, *, actor: str = "cli", reason: str = "operator pause") -> WorkerSession:
        return self._steer(worker_session_id, actor=actor, state=WorkerSessionState.WAITING_ON_HUMAN, action="pause", reason=reason)

    def resume(self, worker_session_id: str, *, actor: str = "cli", reason: str = "operator resume") -> WorkerSession:
        return self._steer(worker_session_id, actor=actor, state=WorkerSessionState.ASSIGNED, action="resume", reason=reason)

    def interrupt(self, worker_session_id: str, *, actor: str = "cli", reason: str = "operator interrupt") -> WorkerSession:
        return self._steer(worker_session_id, actor=actor, state=WorkerSessionState.SELF_HEALING, action="interrupt", reason=reason)

    def nudge(self, worker_session_id: str, *, actor: str = "cli", note: str = "") -> WorkerSession:
        return self._steer(worker_session_id, actor=actor, state=None, action="nudge", reason=note)

    def possess(
        self,
        worker_session_id: str,
        *,
        actor: str = "cli",
        lease_seconds: int = 900,
    ) -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            if worker.state in TERMINAL_WORKER_STATES:
                raise ValueError("worker already terminal")
            if worker.possession_state == PossessionState.POSSESSED and worker.lease_owner and worker.lease_owner != actor:
                raise ValueError("worker possession lease is held")
            worker.possession_state = PossessionState.POSSESSED
            worker.state = WorkerSessionState.POSSESSED
            worker.lease_owner = _safe_token(actor)
            worker.lease_expires_at = now() + _seconds_delta(max(1, int(lease_seconds or 1)))
            worker.last_heartbeat_at = now()
            self._write(worker)
        self._event("worker_session.possessed", worker, {"lease_owner": worker.lease_owner, "lease_expires_at": worker.lease_expires_at})
        return self.get(worker.id)

    def release(self, worker_session_id: str, *, actor: str = "cli", handback: str = "") -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            if worker.possession_state != PossessionState.POSSESSED:
                return worker
            worker.possession_state = PossessionState.AVAILABLE
            worker.state = WorkerSessionState.ASSIGNED
            worker.lease_owner = None
            worker.lease_expires_at = None
            worker.last_heartbeat_at = now()
            self._write(worker)
        self._event("worker_session.released", worker, {"actor": _safe_token(actor), "handback_summary": _safe_text(handback)})
        return self.get(worker.id)

    def close_for_new_goal(self, *, reason: str = "new goal hygiene") -> dict[str, Any]:
        closed: list[str] = []
        expired_possessions: list[str] = []
        for worker in self.find_active():
            target_state = WorkerSessionState.CLOSED
            if worker.possession_state == PossessionState.POSSESSED:
                expired_possessions.append(worker.id)
            closed_worker = self.close(worker.id, reason=reason, state=target_state)
            closed.append(closed_worker.id)
        readonly_markers = mark_existing_sandboxes_readonly(reason=reason)
        return {
            "closed_worker_session_ids": closed,
            "expired_possession_worker_session_ids": expired_possessions,
            "proof_sandbox_readonly_markers": readonly_markers,
        }

    def _steer(
        self,
        worker_session_id: str,
        *,
        actor: str,
        action: str,
        reason: str,
        state: WorkerSessionState | None,
    ) -> WorkerSession:
        with worker_session_lock(worker_session_id):
            worker = self.get(worker_session_id)
            if worker.state in TERMINAL_WORKER_STATES:
                return worker
            if state is not None:
                worker.state = state
            worker.last_heartbeat_at = now()
            self._write(worker)
        self._event("worker_session.steered", worker, {"action": _safe_token(action), "actor": _safe_token(actor), "reason": _safe_text(reason)})
        return self.get(worker.id)

    def _write(self, worker: WorkerSession) -> None:
        path = paths.worker_session_path(worker.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(worker), indent=2, sort_keys=True)

    def _event(self, event_type: str, worker: WorkerSession, payload: dict[str, Any] | None = None) -> None:
        data = worker_event_payload(worker)
        data.update(_safe_event_payload(payload or {}))
        self.event_log.append(Event(now(), event_type, worker.task_id, worker.active_run_id, worker.persona_id, data))


def worker_event_payload(worker: WorkerSession) -> dict[str, Any]:
    return {
        "worker_session_id": worker.id,
        "state": worker.state.value,
        "stage_id": worker.current_stage_id,
        "session_id_present": bool(_safe_session_id(worker.session_id)),
        "possession_state": worker.possession_state.value,
        "active_run_id": worker.active_run_id,
        "watchdog_warnings": worker.watchdog_warning_count,
    }


def worker_session_summary(worker: WorkerSession, *, reference_time=None) -> dict[str, Any]:
    ref = reference_time or now()
    return {
        "worker_session_id": worker.id,
        "task_id": worker.task_id,
        "persona_id": worker.persona_id,
        "display_name": worker.display_name,
        "role": worker.role,
        "state": worker.state.value,
        "current_stage_id": worker.current_stage_id,
        "active_run_id": worker.active_run_id,
        "session_id_present": bool(_safe_session_id(worker.session_id)),
        "heartbeat_age_seconds": _age_seconds(ref, worker.last_heartbeat_at),
        "context_receipt_id": worker.context_receipt_id,
        "compression_receipt_id": worker.compression_receipt_id,
        "possession_state": worker.possession_state.value,
        "lease_owner": worker.lease_owner,
        "watchdog_warning_count": worker.watchdog_warning_count,
        "decision_count": worker.decision_count,
        "proof_count": worker.proof_count,
        "repair_count": worker.repair_count,
        "token_budget_used": worker.token_budget_used,
        "close_reason": worker.close_reason,
        "next_expected": _next_expected(worker),
    }


def worker_dirty_state(workers: list[WorkerSession]) -> dict[str, Any]:
    active = [worker for worker in workers if _worker_is_active(worker)]
    possessed = [worker for worker in workers if worker.possession_state == PossessionState.POSSESSED]
    return {
        "active_worker_sessions": len(active),
        "possessed_worker_sessions": len(possessed),
        "active_worker_session_ids": [worker.id for worker in active[:20]],
        "possessed_worker_session_ids": [worker.id for worker in possessed[:20]],
        "dirty": bool(active or possessed),
    }


def mark_existing_sandboxes_readonly(*, reason: str) -> list[str]:
    root = paths.proof_sandbox_root()
    if not root.exists():
        return []
    markers: list[str] = []
    for task_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        marker = task_dir / ".readonly.json"
        if marker.exists():
            markers.append(str(marker.relative_to(root)))
            continue
        atomic_json_write(
            marker,
            {
                "schema_version": 1,
                "marked_at": to_jsonable(now()),
                "reason": _safe_text(reason),
                "preserved_evidence": True,
            },
            indent=2,
            sort_keys=True,
        )
        markers.append(str(marker.relative_to(root)))
    return markers


def worker_context_manifest(task_id: str, persona_id: str) -> dict[str, Any]:
    root = paths.worker_context_dir(task_id, persona_id)
    files = {}
    if root.exists():
        for path in sorted(root.glob("*")):
            if path.is_file():
                files[path.name] = {"bytes": path.stat().st_size}
    return {"task_id": task_id, "persona_id": persona_id, "files": files}


def _write_static_prompt_receipt(worker: WorkerSession, persona: AgentPersona) -> None:
    root = paths.worker_context_dir(worker.task_id, worker.persona_id)
    root.mkdir(parents=True, exist_ok=True)
    skill_manifest_hash, skill_receipts = _skill_manifest(
        persona, root_node_mode=_task_root_node_mode(worker.task_id)
    )
    receipt = {
        "schema_version": 1,
        "worker_session_id": worker.id,
        "task_id": worker.task_id,
        "persona_id": worker.persona_id,
        "role": worker.role,
        "created_at": to_jsonable(worker.opened_at),
        "prompt_contract_hash": worker.prompt_contract_hash,
        "system_prompt_ref": _safe_text(getattr(persona, "system_prompt_path", None)),
        "skill_manifest_hash": skill_manifest_hash,
        "skills": skill_receipts,
        "strategy": "static_prompt_once_hud_every_tick",
        "raw_prompts_not_stored": True,
    }
    atomic_json_write(root / "static_prompt_receipt.json", receipt, indent=2, sort_keys=True)


def _worker_is_active(worker: WorkerSession) -> bool:
    return worker.state in ACTIVE_WORKER_STATES or worker.possession_state == PossessionState.POSSESSED


def _prompt_contract_hash(persona: AgentPersona) -> str | None:
    del persona
    from .decision_contract_registry import contract_hash

    return contract_hash()


def _skill_manifest(
    persona: AgentPersona, *, root_node_mode: bool = False
) -> tuple[str, list[dict[str, Any]]]:
    from agent.skill_utils import (
        resolve_skill,
        skill_package_content_hash,
        skill_runtime_compatibility,
    )

    rows: list[dict[str, Any]] = []
    for raw_name in list(getattr(persona, "skills", []) or []):
        name = str(raw_name or "").strip()
        if not name:
            continue
        resolution = resolve_skill(name)
        selected = resolution.candidate
        compatibility = skill_runtime_compatibility(
            selected,
            surface="mission_worker",
            root_node_mode=root_node_mode,
        )
        content_hash = (
            skill_package_content_hash(selected.skill_dir, selected.skill_md)
            if selected
            else None
        )
        rows.append(
            {
                "name": name,
                "assignment_policy": str(
                    compatibility.get("load_policy") or "recommended"
                ),
                "load_state": (
                    "loaded_this_turn"
                    if compatibility.get("compatible")
                    and compatibility.get("load_policy") == "required_preload"
                    else "assigned_not_loaded"
                ),
                "resolution_status": resolution.status,
                "source_kind": selected.source_kind if selected else None,
                "content_hash": content_hash,
                "hash_tracked": content_hash is not None,
                "compatibility": compatibility,
            }
        )
    manifest_hash = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return manifest_hash, rows


def _task_root_node_mode(task_id: str) -> bool:
    return False


def _safe_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, (int, float, bool)):
            safe[str(key)] = value
        elif isinstance(value, str):
            safe[str(key)] = _safe_text(value)
        else:
            safe[str(key)] = to_jsonable(value)
    return safe


def _safe_text(value: Any) -> str | None:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "cookie", "authorization", "bearer", "api_key", "sk-")):
        return "redacted"
    if ":/" in text or "\\" in text or text.startswith(("/", "~")):
        return "path-redacted"
    return text[:240]


def _safe_token(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in text)
    return cleaned[:128] or None


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _age_seconds(reference_time, timestamp) -> float | None:
    if timestamp is None:
        return None
    ref = reference_time
    ts = timestamp
    if ref.tzinfo is None and ts.tzinfo is not None:
        ref = ref.replace(tzinfo=ts.tzinfo)
    elif ref.tzinfo is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=ref.tzinfo)
    return max(0.0, (ref - ts).total_seconds())


def _next_expected(worker: WorkerSession) -> str:
    if worker.state == WorkerSessionState.POSSESSED:
        return "release_possession"
    if worker.state == WorkerSessionState.WAITING_ON_HUMAN:
        return "operator_resume_or_release"
    if worker.state == WorkerSessionState.WAITING_ON_PROOF:
        return "proof_result"
    if worker.state == WorkerSessionState.SELF_HEALING:
        return "repair_or_block"
    if worker.state in TERMINAL_WORKER_STATES:
        return "archive_or_inspect"
    return "worker_progress"


def _seconds_delta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)
