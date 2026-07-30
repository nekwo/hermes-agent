from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .models import Event
from .role_checklists import RoleChecklistStore, checklist_summary, normalize_role_id
from .serde import from_jsonable, to_jsonable

OPEN_STATUSES = frozenset({"open", "continuing", "waiting_for_gate", "waiting_for_qa", "needs_fix"})


@dataclass(slots=True)
class RoleEnvelope:
    envelope_id: str
    task_id: str
    role_id: str
    worker_session_id: str | None
    mission_stage_id: str | None
    phase: str
    status: str
    started_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    checklist_id: str | None = None
    proof_batch_id: str | None = None
    last_run_id: str | None = None
    last_decision_type: str | None = None
    last_progress_hash: str | None = None
    last_qa_finding_hash: str | None = None
    continuation_count: int = 0
    no_progress_count: int = 0
    repair_count: int = 0
    close_reason: str | None = None
    continuation_reason: str | None = None
    legacy_projection: bool = False
    schema_version: int = 1


class RoleEnvelopeStore:
    def __init__(self, event_log=None, checklist_store: RoleChecklistStore | None = None):
        from .events import EventLog

        self.event_log = event_log or EventLog()
        self.checklist_store = checklist_store or RoleChecklistStore(event_log=self.event_log)

    def get(self, task_id: str, envelope_id: str) -> RoleEnvelope:
        raw = json.loads(paths.role_envelope_path(task_id, envelope_id).read_text(encoding="utf-8"))
        return from_jsonable(RoleEnvelope, raw)

    def save(
        self,
        envelope: RoleEnvelope,
        *,
        event_type: str | None = None,
        run_id: str | None = None,
        persona_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RoleEnvelope:
        envelope.updated_at = now()
        path = paths.role_envelope_path(envelope.task_id, envelope.envelope_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(envelope), indent=2, sort_keys=True)
        if event_type:
            data = role_envelope_event_payload(envelope)
            data.update(payload or {})
            self.event_log.append(Event(now(), event_type, envelope.task_id, run_id or envelope.last_run_id, persona_id or envelope.role_id, _safe_payload(data)))
        return envelope

    def open_or_resume(
        self,
        *,
        task: Task,
        role_id: str,
        mission_stage_id: str | None,
        worker_session_id: str | None = None,
        run_id: str | None = None,
        phase: str | None = None,
        legacy_projection: bool = False,
    ) -> RoleEnvelope:
        role_id = normalize_role_id(role_id)
        existing = self.latest_for_role_stage(task.id, role_id=role_id, mission_stage_id=mission_stage_id, active_only=True)
        checklist = self.checklist_store.open_or_create(task=task, role_id=role_id, mission_stage_id=mission_stage_id, run_id=run_id, legacy_projection=legacy_projection)
        if existing is not None:
            existing.worker_session_id = worker_session_id or existing.worker_session_id
            existing.last_run_id = run_id or existing.last_run_id
            existing.checklist_id = existing.checklist_id or checklist.checklist_id
            existing.phase = phase or existing.phase
            existing.status = "continuing"
            existing.continuation_count += 1
            existing.continuation_reason = "same role envelope resumed"
            return self.save(existing, event_type="role_envelope.continued", run_id=run_id, persona_id=role_id)
        envelope = RoleEnvelope(
            envelope_id=f"envelope_{uuid.uuid4().hex[:12]}",
            task_id=task.id,
            role_id=role_id,
            worker_session_id=worker_session_id,
            mission_stage_id=mission_stage_id,
            phase=phase or _phase_for_role(role_id),
            status="open",
            started_at=now(),
            updated_at=now(),
            checklist_id=checklist.checklist_id,
            last_run_id=run_id,
            legacy_projection=legacy_projection,
        )
        return self.save(envelope, event_type="role_envelope.opened", run_id=run_id, persona_id=role_id)

    def record_progress(
        self,
        envelope: RoleEnvelope,
        *,
        run_id: str | None,
        decision_type: str | None = None,
        proof_ids: list[str] | None = None,
        checklist_revision: int | None = None,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
        phase: str | None = None,
        continuation_reason: str | None = None,
        proof_batch_id: str | None = None,
    ) -> RoleEnvelope:
        progress_hash = _progress_hash(
            decision_type=decision_type,
            proof_ids=proof_ids or [],
            checklist_revision=checklist_revision,
            payload=payload,
        )
        if progress_hash and envelope.last_progress_hash == progress_hash:
            envelope.no_progress_count += 1
        elif progress_hash:
            envelope.no_progress_count = 0
            envelope.last_progress_hash = progress_hash
        envelope.last_run_id = run_id or envelope.last_run_id
        envelope.last_decision_type = decision_type or envelope.last_decision_type
        envelope.status = status or envelope.status
        envelope.phase = phase or envelope.phase
        envelope.proof_batch_id = proof_batch_id or envelope.proof_batch_id
        envelope.continuation_reason = continuation_reason or envelope.continuation_reason
        return self.save(
            envelope,
            event_type="role_envelope.continued" if envelope.status in OPEN_STATUSES else "role_envelope.paused",
            run_id=run_id,
            persona_id=envelope.role_id,
            payload={"decision_type": decision_type, "proof_count": len(proof_ids or []), "no_progress_count": envelope.no_progress_count},
        )

    def close(self, task_id: str, envelope_id: str, *, reason: str, run_id: str | None = None) -> RoleEnvelope:
        envelope = self.get(task_id, envelope_id)
        if envelope.status not in OPEN_STATUSES:
            return envelope
        envelope.status = "closed"
        envelope.closed_at = now()
        envelope.close_reason = _safe_text(reason)
        return self.save(envelope, event_type="role_envelope.closed", run_id=run_id, persona_id=envelope.role_id, payload={"close_reason": envelope.close_reason})

    def close_for_task(self, task_id: str, *, reason: str, run_id: str | None = None) -> list[str]:
        closed: list[str] = []
        for envelope in self.list_for_task(task_id):
            if envelope.status in OPEN_STATUSES:
                closed.append(self.close(task_id, envelope.envelope_id, reason=reason, run_id=run_id).envelope_id)
        return closed

    def list_for_task(self, task_id: str) -> list[RoleEnvelope]:
        root = paths.role_envelopes_task_dir(task_id)
        if not root.exists():
            return []
        items: list[RoleEnvelope] = []
        for path in sorted(root.glob("*.json")):
            try:
                items.append(from_jsonable(RoleEnvelope, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(items, key=lambda item: item.updated_at or item.started_at)

    def list_all(self) -> list[RoleEnvelope]:
        root = paths.role_envelopes_dir()
        if not root.exists():
            return []
        items: list[RoleEnvelope] = []
        for path in sorted(root.glob("*/*.json")):
            try:
                items.append(from_jsonable(RoleEnvelope, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(items, key=lambda item: item.updated_at or item.started_at)

    def latest_for_role_stage(self, task_id: str, *, role_id: str, mission_stage_id: str | None, active_only: bool = False) -> RoleEnvelope | None:
        role_id = normalize_role_id(role_id)
        candidates = [
            item for item in self.list_for_task(task_id) if item.role_id == role_id and (item.mission_stage_id or None) == (mission_stage_id or None)
        ]
        if active_only:
            candidates = [item for item in candidates if item.status in OPEN_STATUSES]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.updated_at or item.started_at)


def role_envelope_summary(envelope: RoleEnvelope, *, checklist_store: RoleChecklistStore | None = None) -> dict[str, Any]:
    checklist_store = checklist_store or RoleChecklistStore()
    checklist = None
    if envelope.checklist_id:
        try:
            checklist = checklist_store.get(envelope.task_id, envelope.checklist_id)
        except Exception:
            checklist = None
    return {
        "envelope_id": envelope.envelope_id,
        "task_id": envelope.task_id,
        "role_id": envelope.role_id,
        "worker_session_id": envelope.worker_session_id,
        "mission_stage_id": envelope.mission_stage_id,
        "phase": envelope.phase,
        "status": envelope.status,
        "started_at": envelope.started_at,
        "updated_at": envelope.updated_at,
        "closed_at": envelope.closed_at,
        "checklist_id": envelope.checklist_id,
        "proof_batch_id": envelope.proof_batch_id,
        "last_run_id": envelope.last_run_id,
        "last_decision_type": envelope.last_decision_type,
        "continuation_count": envelope.continuation_count,
        "no_progress_count": envelope.no_progress_count,
        "repair_count": envelope.repair_count,
        "close_reason": envelope.close_reason,
        "continuation_reason": envelope.continuation_reason,
        "legacy_projection": envelope.legacy_projection,
        "checklist": checklist_summary(checklist) if checklist is not None else None,
    }


def role_envelope_event_payload(envelope: RoleEnvelope) -> dict[str, Any]:
    return {
        "envelope_id": envelope.envelope_id,
        "role_id": envelope.role_id,
        "worker_session_id": envelope.worker_session_id,
        "mission_stage_id": envelope.mission_stage_id,
        "phase": envelope.phase,
        "status": envelope.status,
        "checklist_id": envelope.checklist_id,
        "proof_batch_id": envelope.proof_batch_id,
        "last_run_id": envelope.last_run_id,
        "continuation_count": envelope.continuation_count,
        "no_progress_count": envelope.no_progress_count,
        "continuation_reason": envelope.continuation_reason,
    }


def _phase_for_role(role_id: str) -> str:
    role_id = normalize_role_id(role_id)
    if role_id == "neko_supervisor":
        return "planning"
    if role_id == "qa":
        return "qa_review"
    return "implementation"


def _progress_hash(*, decision_type: str | None, proof_ids: list[str], checklist_revision: int | None, payload: dict[str, Any] | None = None) -> str:
    # Hash only what the persona decided. Harness-minted artifacts (fresh proof
    # ids per rerun, mechanical checklist revision bumps) change every cycle of a
    # Dev<->QA ping-pong and previously reset the no-progress counter, so the
    # guard never tripped on a persona repeating the exact same decision
    # (observed live: task_burn_a77bf268, 12 identical request_test_run /
    # report_qa_verdict cycles).
    payload_digest = None
    if isinstance(payload, dict):
        compact_payload = _payload_digest_projection(payload)
        payload_digest = hashlib.sha256(json.dumps(compact_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    data = json.dumps({"decision_type": decision_type, "payload_digest": payload_digest}, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _payload_digest_projection(payload: dict[str, Any]) -> Any:
    safe = to_jsonable(payload)
    text = json.dumps(safe, sort_keys=True, default=str)
    if len(text) > 4000:
        text = text[:4000]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _safe_text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip().replace("\r", " ").replace("\n", " ")[:limit]


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            safe[key] = _safe_text(value)
        elif isinstance(value, list):
            safe[key] = [_safe_text(item, 160) if isinstance(item, str) else item for item in value[:20]]
        elif isinstance(value, dict):
            safe[key] = _safe_payload(value)
        else:
            safe[key] = value
    return safe
