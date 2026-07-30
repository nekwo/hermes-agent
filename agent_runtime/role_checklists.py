"""Role-local checklists: the store, its template, and payload validation.

S27 removed the DECISION-side half — ``validate_decision_checklist_payload``,
``sanitize_decision_checklist_payload`` and ``apply_decision_checklist_updates``
— which applied a role's checklist updates while executing a typed decision, a
lane that went with the dispatch loop in S5. ``stage_checklist_hud`` went with
them: its last caller left in ``8fa9ee283`` (the S19 ``context_builder`` HUD
cluster cut), which the plan for this wave had not accounted for.

``checklist_for_task_stage`` is deliberately KEPT — it is reached through
``RoleChecklistStore.open_or_create`` from ``role_envelopes``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .decision_schema import DecisionPayloadInvalid
from .models import Event
from .serde import from_jsonable, to_jsonable

#: The ``Task`` record was deleted with the mission lane (S8). The three
#: surviving signatures below take a duck-typed, task-shaped object and read it
#: only through ``.id``, so ``TaskLike`` names that honestly instead of an
#: annotation that resolves to nothing (``from __future__ import annotations``
#: hid the NameError; ``typing.get_type_hints`` would still raise).
TaskLike = Any

CHECKLIST_ITEM_STATUSES = frozenset(
    {
        "pending",
        "in_progress",
        "self_approved",
        "verified",
        "needs_fix",
        "blocked",
        "skipped_with_reason",
    }
)
SELF_APPROVAL_STATUSES = frozenset({"none", "working", "ready_for_gate", "ready_for_handoff", "ready_for_qa", "blocked"})
CHECKLIST_UPDATE_KEYS = frozenset({"item_id", "status", "evidence_refs", "summary"})


@dataclass(slots=True)
class RoleChecklistItem:
    item_id: str
    title: str
    owner_role: str
    required: bool = True
    status: str = "pending"
    done_criteria: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    self_approved_at: datetime | None = None
    self_approved_by_run_id: str | None = None
    blocked_reason: str | None = None
    summary: str | None = None


@dataclass(slots=True)
class RoleChecklist:
    checklist_id: str
    task_id: str
    mission_stage_id: str | None
    role_id: str
    items: list[RoleChecklistItem] = field(default_factory=list)
    status: str = "active"
    revision: int = 0
    legacy_projection: bool = False
    operational_scope: str = "role_local"
    can_promote_global_stage: bool = False
    promotion_rule: str = (
        "Role checklist items are local subtasks. Only Neko/Harness may create or promote global mission stages when a cross-role dependency is discovered."
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None
    schema_version: int = 1


class RoleChecklistStore:
    def __init__(self, event_log=None):
        from .events import EventLog

        self.event_log = event_log or EventLog()

    def get(self, task_id: str, checklist_id: str) -> RoleChecklist:
        raw = json.loads(paths.role_checklist_path(task_id, checklist_id).read_text(encoding="utf-8"))
        return from_jsonable(RoleChecklist, raw)

    def save(
        self,
        checklist: RoleChecklist,
        *,
        event_type: str | None = None,
        run_id: str | None = None,
        persona_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RoleChecklist:
        checklist.updated_at = now()
        if checklist.created_at is None:
            checklist.created_at = checklist.updated_at
        path = paths.role_checklist_path(checklist.task_id, checklist.checklist_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(checklist), indent=2, sort_keys=True)
        if event_type:
            event_payload = {"checklist_id": checklist.checklist_id, "role_id": checklist.role_id, "mission_stage_id": checklist.mission_stage_id}
            event_payload.update(payload or {})
            self.event_log.append(Event(now(), event_type, checklist.task_id, run_id, persona_id or checklist.role_id, _safe_payload(event_payload)))
            self._write_event(checklist.task_id, event_type, event_payload)
        return checklist

    def open_or_create(
        self,
        *,
        task: TaskLike,
        role_id: str,
        mission_stage_id: str | None,
        run_id: str | None = None,
        legacy_projection: bool | None = None,
    ) -> RoleChecklist:
        existing = self.latest_for_role_stage(task.id, role_id=role_id, mission_stage_id=mission_stage_id, active_only=True)
        if existing is not None:
            return existing
        checklist = checklist_for_task_stage(task, role_id=role_id, mission_stage_id=mission_stage_id, legacy_projection=bool(legacy_projection))
        self.save(checklist, event_type="role_checklist.created", run_id=run_id, persona_id=role_id)
        return checklist

    def list_for_task(self, task_id: str) -> list[RoleChecklist]:
        root = paths.role_checklists_task_dir(task_id)
        if not root.exists():
            return []
        items: list[RoleChecklist] = []
        for path in sorted(root.glob("*.json")):
            try:
                items.append(from_jsonable(RoleChecklist, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(items, key=lambda item: item.updated_at or item.created_at or now())

    def list_all(self) -> list[RoleChecklist]:
        root = paths.role_checklists_dir()
        if not root.exists():
            return []
        items: list[RoleChecklist] = []
        for path in sorted(root.glob("*/*.json")):
            try:
                items.append(from_jsonable(RoleChecklist, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(items, key=lambda item: item.updated_at or item.created_at or now())

    def latest_for_role_stage(self, task_id: str, *, role_id: str, mission_stage_id: str | None, active_only: bool = False) -> RoleChecklist | None:
        candidates = [
            item
            for item in self.list_for_task(task_id)
            if item.role_id == normalize_role_id(role_id) and (item.mission_stage_id or None) == (mission_stage_id or None)
        ]
        if active_only:
            candidates = [item for item in candidates if item.status == "active"]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.updated_at or item.created_at or now())

    def apply_updates(self, checklist: RoleChecklist, *, updates: list[dict[str, Any]], run_id: str | None, persona_id: str | None) -> RoleChecklist:
        by_id = {item.item_id: item for item in checklist.items}
        changed: list[str] = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            item_id = str(update.get("item_id") or "").strip()
            item = by_id.get(item_id)
            if item is None:
                continue
            status = str(update.get("status") or item.status).strip()
            if status in CHECKLIST_ITEM_STATUSES:
                item.status = status
            evidence = update.get("evidence_refs")
            if isinstance(evidence, list):
                item.evidence_refs = _dedupe([*item.evidence_refs, *[str(ref).strip() for ref in evidence if str(ref).strip()]])
            summary = _safe_text(update.get("summary"))
            if summary:
                item.summary = summary
            if item.status == "self_approved":
                item.self_approved_at = now()
                item.self_approved_by_run_id = run_id
            if item.status == "blocked":
                item.blocked_reason = summary or item.blocked_reason
            changed.append(item.item_id)
        if changed:
            checklist.revision += 1
            self.save(
                checklist,
                event_type="role_checklist.item_updated",
                run_id=run_id,
                persona_id=persona_id or checklist.role_id,
                payload={"item_ids": changed, "revision": checklist.revision},
            )
        return checklist

    def _write_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        event_id = f"event_{uuid.uuid4().hex[:12]}"
        path = paths.role_checklist_event_path(task_id, event_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"event_id": event_id, "type": event_type, "created_at": to_jsonable(now()), "payload": _safe_payload(payload)}, indent=2, sort_keys=True)


def checklist_for_task_stage(task: TaskLike, *, role_id: str, mission_stage_id: str | None, legacy_projection: bool = False) -> RoleChecklist:
    normalized_role = normalize_role_id(role_id)
    stage_kind = "implementation"
    stage_owner = normalized_role
    stage = _typed_stage_for_checklist(task, mission_stage_id)
    if stage is not None:
        stage_kind = str(stage.kind or stage_kind)
        stage_owner = normalize_role_id(stage.owner or normalized_role)
        mission_stage_id = stage.id or mission_stage_id
    legacy_projection = False
    template_role = stage_owner if stage_owner in {"neko_supervisor", "dev", "backend_dev", "qa"} else normalized_role
    can_promote = template_role == "neko_supervisor"
    return RoleChecklist(
        checklist_id=f"checklist_{uuid.uuid4().hex[:12]}",
        task_id=task.id,
        mission_stage_id=mission_stage_id,
        role_id=normalized_role,
        items=_template_items(template_role, stage_kind),
        legacy_projection=legacy_projection,
        operational_scope="mission_stage" if can_promote else "role_local",
        can_promote_global_stage=can_promote,
        promotion_rule=_promotion_rule(template_role),
        created_at=now(),
        updated_at=now(),
    )


def checklist_summary(checklist: RoleChecklist, *, max_items: int = 8) -> dict[str, Any]:
    items = checklist.items[: max(1, max_items)]
    done_statuses = {"self_approved", "verified", "skipped_with_reason"}
    current = next((item for item in checklist.items if item.status in {"pending", "in_progress", "needs_fix", "blocked"}), checklist.items[-1] if checklist.items else None)
    return {
        "checklist_id": checklist.checklist_id,
        "role_id": checklist.role_id,
        "mission_stage_id": checklist.mission_stage_id,
        "status": checklist.status,
        "revision": checklist.revision,
        "legacy_projection": checklist.legacy_projection,
        "operational_scope": checklist.operational_scope,
        "can_promote_global_stage": checklist.can_promote_global_stage,
        "promotion_rule": checklist.promotion_rule,
        "current_item_id": current.item_id if current else None,
        "done_count": len([item for item in checklist.items if item.status in done_statuses]),
        "total_count": len(checklist.items),
        "valid_item_ids": [item.item_id for item in checklist.items],
        "valid_statuses": sorted(CHECKLIST_ITEM_STATUSES),
        "valid_self_approval_statuses": sorted(SELF_APPROVAL_STATUSES),
        "items": [item_summary(item) for item in items],
    }


def item_summary(item: RoleChecklistItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "title": item.title,
        "owner_role": item.owner_role,
        "required": item.required,
        "status": item.status,
        "done_criteria": list(item.done_criteria),
        "evidence_refs": list(item.evidence_refs)[:8],
        "summary": item.summary,
        "blocked_reason": item.blocked_reason,
    }


def validate_checklist_payload_structure(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    if "self_approval_status" in payload and str(payload.get("self_approval_status") or "") not in SELF_APPROVAL_STATUSES:
        raise DecisionPayloadInvalid(_repair_message("self_approval_status", valid_self_approval_statuses=sorted(SELF_APPROVAL_STATUSES)))
    if "active_checklist_item_id" in payload and not str(payload.get("active_checklist_item_id") or "").strip():
        raise DecisionPayloadInvalid(_repair_message("active_checklist_item_id"))
    updates = payload.get("checklist_updates")
    if updates is None:
        return
    if not isinstance(updates, list):
        raise DecisionPayloadInvalid(_repair_message("checklist_updates", message="checklist_updates must be a list"))
    for update in updates:
        if not isinstance(update, dict):
            raise DecisionPayloadInvalid(_repair_message("checklist_updates[]", message="each checklist update must be an object"))
        extra_keys = sorted(set(update) - CHECKLIST_UPDATE_KEYS)
        if extra_keys:
            raise DecisionPayloadInvalid(
                _repair_message(
                    "checklist_updates[]",
                    message=f"checklist update has unsupported keys: {extra_keys}",
                    allowed_update_keys=sorted(CHECKLIST_UPDATE_KEYS),
                )
            )
        item_id = str(update.get("item_id") or "").strip()
        if not item_id:
            raise DecisionPayloadInvalid(_repair_message("item_id", message="checklist update item_id is required"))
        status = str(update.get("status") or "").strip()
        if status and status not in CHECKLIST_ITEM_STATUSES:
            raise DecisionPayloadInvalid(_repair_message("status", valid_statuses=sorted(CHECKLIST_ITEM_STATUSES)))
        if status == "skipped_with_reason" and not _safe_text(update.get("summary")):
            raise DecisionPayloadInvalid(_repair_message("summary", message="skipped_with_reason requires summary"))
        if status == "blocked" and not _safe_text(update.get("summary")):
            raise DecisionPayloadInvalid(_repair_message("summary", message="blocked checklist update requires summary"))
        evidence_refs = update.get("evidence_refs")
        if evidence_refs is not None and not isinstance(evidence_refs, list):
            raise DecisionPayloadInvalid(_repair_message("evidence_refs", message="evidence_refs must be a list"))


def normalize_role_id(role_id: str) -> str:
    value = str(role_id or "").strip()
    if value in {"alice_supervisor", "pm", "neko"}:
        return "neko_supervisor"
    if value in {"launcher_dev"}:
        return "dev"
    return value or "dev"


def _typed_stage_for_checklist(task: TaskLike, mission_stage_id: str | None):
    return None


def _promotion_rule(role_id: str) -> str:
    if normalize_role_id(role_id) == "neko_supervisor":
        return "Neko may create or promote global mission stages only for real routing, dependency, recovery, or scope changes."
    return "Role checklist items are local subtasks. Do not create or promote global mission stages from Dev/QA checklist updates; request_missing_input or report_blocker for cross-role dependencies."


def _template_items(role_id: str, stage_kind: str) -> list[RoleChecklistItem]:
    role_id = normalize_role_id(role_id)
    if role_id == "neko_supervisor":
        return [
            _item("preserve_intent", "Preserve parent mission intent", role_id, ["Parent objective and acceptance criteria are not narrowed."]),
            _item("typed_plan", "Create or repair typed stages", role_id, ["Stages have owner, kind, repo, dependencies, and proof requirements."]),
            _item("release_owner", "Release next unblocked owner", role_id, ["Next owner is selected from typed dependency readiness."]),
        ]
    if role_id == "qa":
        return [
            _item("dependency_readiness", "Verify blocking typed stages", role_id, ["All blocking stages are ready or passed."]),
            _item("command_proof", "Verify command proof IDs", role_id, ["Required command proof IDs exist and passed."]),
            _item("visual_proof", "Verify visual proof IDs", role_id, ["Visual proof exists when required."]),
            _item("release_verdict", "Issue evidence-backed verdict", role_id, ["Verdict cites proof coverage and remaining gaps."]),
        ]
    if role_id == "backend_dev" or stage_kind == "proof_only":
        return [
            _item("inspect_contract", "Inspect requested contract/runtime state", role_id, ["Relevant logs/state/proof recipe are understood."]),
            _item("request_gate", "Request exact deterministic proof recipe", role_id, ["Proof recipe or command is scoped to the typed stage."]),
            _item("attach_proof", "Verify proof attaches to stage", role_id, ["Proof IDs are attached to task and stage."]),
            _item("handoff", "Hand off according to dependencies", role_id, ["Next owner follows typed dependency graph."]),
        ]
    return [
        _item("inspect", "Inspect target files/logs", role_id, ["Target files/logs are read narrowly."]),
        _item("patch", "Patch product code", role_id, ["Relevant product files changed and unrelated churn avoided."]),
        _item("self_test", "Run focused self-test", role_id, ["Focused command exits 0 or blocker is recorded."]),
        _item("attach_self_test", "Attach self-test evidence", role_id, ["Self-test evidence refs are recorded when available."]),
        _item("handoff", "Hand off to QA", role_id, ["Delivery summary and proof refs are ready for QA."]),
    ]


def _item(item_id: str, title: str, owner_role: str, done_criteria: list[str]) -> RoleChecklistItem:
    return RoleChecklistItem(item_id=item_id, title=title, owner_role=owner_role, done_criteria=done_criteria)


def _repair_message(
    invalid_field: str,
    *,
    message: str | None = None,
    valid_item_ids: list[str] | None = None,
    valid_statuses: list[str] | None = None,
    valid_self_approval_statuses: list[str] | None = None,
    allowed_update_keys: list[str] | None = None,
) -> str:
    payload = {
        "message": message or f"invalid checklist payload field: {invalid_field}",
        "repair_kind": "checklist_payload",
        "invalid_field": invalid_field,
        "valid_item_ids": valid_item_ids,
        "valid_statuses": valid_statuses or sorted(CHECKLIST_ITEM_STATUSES),
        "valid_self_approval_statuses": valid_self_approval_statuses or sorted(SELF_APPROVAL_STATUSES),
        "allowed_update_keys": allowed_update_keys,
        "next_expected": "Update one visible checklist item or omit checklist_updates.",
    }
    return json.dumps({key: value for key, value in payload.items() if value not in (None, [], {})}, sort_keys=True)


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


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
