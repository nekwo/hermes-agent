from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .delivery_directive import read_bundle_promotion_record
from .events import EventLog
from .models import Event, RepoBundle
from .serde import from_jsonable, to_jsonable

REPO_BUNDLE_STATES = frozenset(
    {
        "planned",
        "queued_waiting_dependency",
        "assigned",
        "running",
        "delivered_waiting_for_qa",
        "delivered",
        "blocked",
        "verified",
        "rejected",
        "cancelled",
        "archived",
    }
)
TERMINAL_REPO_BUNDLE_STATES = frozenset({"verified", "blocked", "cancelled", "archived"})
DELIVERED_REPO_BUNDLE_STATES = frozenset({"delivered_waiting_for_qa", "delivered", "verified"})
REPO_LOCK_MODES = frozenset({"read", "write", "exclusive_maintenance"})
REPO_BUNDLE_DELIVERY_CONTRACT = "staged_bundle_not_applied"
REPO_BUNDLE_CHECKOUT_STATUS = "not_applied"

WAKE_DEPENDENCY_DELIVERED = "dependency_bundle_delivered"

_REPO_OWNER_RULES: tuple[tuple[str, str], ...] = (
    ("eternialauncher", "dev"),
    ("launcher", "dev"),
    ("eterniabackend", "backend_dev"),
    ("backend", "backend_dev"),
    ("hermes-agent", "backend_dev"),
    ("hermes", "backend_dev"),
)


class RepoBundleStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create_or_update_from_task(self, task: Any) -> list[RepoBundle]:
        desired = desired_bundles_for_task(task)
        result: list[RepoBundle] = []
        for bundle in desired:
            try:
                existing = self.get(task.id, bundle.id)
            except Exception:
                self._write(bundle)
                self._event("repo_bundle.created", bundle, {"state": bundle.state})
                result.append(self.get(task.id, bundle.id))
                continue
            merged = merge_desired_bundle(existing, bundle)
            if to_jsonable(merged) != to_jsonable(existing):
                self._write(merged)
                self._event("repo_bundle.updated", merged, {"state": merged.state})
                result.append(self.get(task.id, merged.id))
            else:
                result.append(existing)
        self.cancel_superseded(task, desired_ids={bundle.id for bundle in desired})
        return sorted(result, key=lambda item: (item.repo.lower(), item.id))

    def get(self, task_id: str, bundle_id: str) -> RepoBundle:
        raw = json.loads(paths.repo_bundle_path(task_id, bundle_id).read_text(encoding="utf-8"))
        return from_jsonable(RepoBundle, raw)

    def update(self, bundle: RepoBundle, *, event_type: str = "repo_bundle.updated", payload: dict[str, Any] | None = None) -> RepoBundle:
        bundle.updated_at = now()
        bundle.state = safe_bundle_state(bundle.state)
        self._write(bundle)
        updated = self.get(bundle.task_id, bundle.id)
        self._event(event_type, updated, {"state": updated.state, **(payload or {})})
        return updated

    def list_for_task(self, task_id: str) -> list[RepoBundle]:
        directory = paths.repo_bundles_task_dir(task_id)
        if not directory.exists():
            return []
        bundles: list[RepoBundle] = []
        for path in sorted(directory.glob("*.json")):
            try:
                bundles.append(from_jsonable(RepoBundle, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(bundles, key=lambda item: (item.repo.lower(), item.id))

    def list_all(self) -> list[RepoBundle]:
        root = paths.repo_bundles_dir()
        if not root.exists():
            return []
        bundles: list[RepoBundle] = []
        for path in sorted(root.glob("*/*.json")):
            try:
                bundles.append(from_jsonable(RepoBundle, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(bundles, key=lambda item: (item.task_id, item.repo.lower(), item.id))

    def find_for_assignment(self, assignment) -> RepoBundle | None:
        bundle_id = getattr(assignment, "repo_bundle_id", None)
        task_id = getattr(assignment, "task_id", None)
        if not bundle_id or not task_id:
            return None
        try:
            return self.get(str(task_id), str(bundle_id))
        except Exception:
            return None

    def attach_assignment(self, bundle: RepoBundle, assignment_id: str | None, *, run_id: str | None = None) -> RepoBundle:
        changed = False
        if assignment_id and bundle.assignment_id != assignment_id:
            bundle.assignment_id = assignment_id
            changed = True
        if run_id and bundle.active_run_id != run_id:
            bundle.active_run_id = run_id
            changed = True
        if bundle.state == "planned":
            bundle.state = "assigned"
            changed = True
        if not changed:
            return bundle
        return self.update(bundle, event_type="repo_bundle.assigned", payload={"assignment_id": assignment_id, "run_id": run_id})

    def mark_running(self, bundle: RepoBundle, *, run_id: str | None) -> RepoBundle:
        bundle.active_run_id = run_id or bundle.active_run_id
        if bundle.state not in TERMINAL_REPO_BUNDLE_STATES:
            bundle.state = "running"
        return self.update(bundle, event_type="repo_bundle.running", payload={"run_id": run_id})

    def mark_verified(self, bundle: RepoBundle, *, proof_ids: list[str] | None = None) -> RepoBundle:
        for proof_id in proof_ids or []:
            safe = safe_token(proof_id)
            if safe and safe not in bundle.proof_ids:
                bundle.proof_ids.append(safe)
        bundle.state = "verified"
        bundle.verified_at = now()
        bundle.active_run_id = None
        return self.update(bundle, event_type="repo_bundle.verified", payload={"proof_count": len(bundle.proof_ids)})

    def mark_rejected(self, bundle: RepoBundle, *, reason: str = "") -> RepoBundle:
        bundle.state = "rejected"
        bundle.rejected_at = now()
        bundle.active_run_id = None
        bundle.last_terminal_feedback = {"reason": safe_text(reason, limit=500)}
        return self.update(bundle, event_type="repo_bundle.rejected", payload={"reason": safe_text(reason, limit=200)})

    def wake_ready_dependencies(self, task_id: str) -> list[RepoBundle]:
        bundles = self.list_for_task(task_id)
        by_id = {bundle.id: bundle for bundle in bundles}
        woke: list[RepoBundle] = []
        for bundle in bundles:
            if bundle.state != "queued_waiting_dependency":
                continue
            if all(by_id.get(dep_id) and by_id[dep_id].state in DELIVERED_REPO_BUNDLE_STATES for dep_id in bundle.dependency_bundle_ids):
                bundle.state = "planned"
                bundle.queue_reason = None
                bundle.wake_condition = WAKE_DEPENDENCY_DELIVERED
                woke.append(self.update(bundle, event_type="repo_bundle.woke", payload={"wake_condition": WAKE_DEPENDENCY_DELIVERED}))
        return woke

    def cancel_superseded(self, task: Any, *, desired_ids: set[str]) -> list[RepoBundle]:
        return []

    def _write(self, bundle: RepoBundle) -> None:
        bundle.state = safe_bundle_state(bundle.state)
        bundle.updated_at = bundle.updated_at or now()
        bundle.created_at = bundle.created_at or bundle.updated_at
        path = paths.repo_bundle_path(bundle.task_id, bundle.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(bundle), indent=2, sort_keys=True)

    def _event(self, event_type: str, bundle: RepoBundle, payload: dict[str, Any]) -> None:
        self.event_log.append(
            Event(
                ts=now(),
                type=event_type,
                task_id=bundle.task_id,
                run_id=bundle.active_run_id,
                persona_id=bundle.owner_persona_id,
                payload={
                    "repo_bundle_id": bundle.id,
                    "repo": safe_text(bundle.repo, limit=160),
                    **payload,
                },
            )
        )


def desired_bundles_for_task(task: Any) -> list[RepoBundle]:
    repos = list(getattr(task, "affected_repos", None) or [])
    if not repos:
        return []
    created_at = now()
    bundles: list[RepoBundle] = []
    for repo in _dedupe_preserve_order(repos):
        repo_text = safe_text(repo, limit=160)
        bundles.append(
            RepoBundle(
                id=bundle_id_for(task.id, repo_text, stage_ids=[]),
                task_id=task.id,
                repo=repo_text,
                owner_persona_id=owner_for_repo(repo_text),
                state="planned",
                title=f"{repo_text} bundle",
                objective=task.description,
                acceptance=list(getattr(task, "acceptance_criteria", None) or []),
                non_goals=list(getattr(task, "non_goals", None) or []),
                proof_targets=list(getattr(task, "proof_expectations", None) or []),
                proof_requirements=[],
                visual_requirements=["visual_proof"] if getattr(task, "requires_visual_proof", False) else [],
                created_at=created_at,
                updated_at=created_at,
            )
        )
    return bundles


def acquire_repo_bundle_locks(*, lane_id: str, task_id: str, bundle_ids: list[str], mode: str = "write", lane_kind: str = "production") -> dict[str, Any]:
    mode = mode if mode in REPO_LOCK_MODES else "write"
    if lane_kind == "playground" and mode != "read":
        return {"ok": False, "park_state": "parked_by_repo_lock", "reason": "playground lanes are read-locked by default", "conflicts": []}
    locks = _read_repo_locks()
    desired = [str(item).strip() for item in bundle_ids if str(item).strip()]
    conflicts = []
    for bundle_id in desired:
        for lock in locks:
            if lock.get("bundle_id") != bundle_id or lock.get("lane_id") == lane_id:
                continue
            if _repo_lock_conflicts(mode, str(lock.get("mode") or "write")):
                conflicts.append({"bundle_id": bundle_id, "owner_lane_id": lock.get("lane_id"), "owner_task_id": lock.get("task_id"), "mode": lock.get("mode")})
    if conflicts:
        return {"ok": False, "park_state": "parked_by_repo_lock", "reason": "repo bundle lock held by another lane", "conflicts": conflicts}
    now_text = now()
    kept = [lock for lock in locks if lock.get("lane_id") != lane_id or lock.get("bundle_id") not in desired]
    kept.extend({"lane_id": lane_id, "task_id": task_id, "bundle_id": bundle_id, "mode": mode, "acquired_at": now_text} for bundle_id in desired)
    _write_repo_locks(kept)
    return {"ok": True, "locks": [lock for lock in kept if lock.get("lane_id") == lane_id and lock.get("bundle_id") in desired]}


def release_repo_bundle_locks(*, lane_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
    locks = _read_repo_locks()
    kept = []
    released = []
    for lock in locks:
        if (lane_id and lock.get("lane_id") == lane_id) or (task_id and lock.get("task_id") == task_id):
            released.append(lock)
        else:
            kept.append(lock)
    if released:
        _write_repo_locks(kept)
    return {"released_count": len(released), "released": released}


def repo_lock_summary() -> dict[str, Any]:
    locks = _read_repo_locks()
    return {"lock_count": len(locks), "locks": locks}


def _repo_lock_conflicts(requested: str, held: str) -> bool:
    if requested == "read" and held == "read":
        return False
    return requested in {"write", "exclusive_maintenance"} or held in {"write", "exclusive_maintenance"}


def _repo_locks_path():
    return paths.store_root() / "repo_bundle_locks.json"


def _read_repo_locks() -> list[dict[str, Any]]:
    path = _repo_locks_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    locks = data.get("locks") if isinstance(data, dict) else data
    return [item for item in locks if isinstance(item, dict)] if isinstance(locks, list) else []


def _write_repo_locks(locks: list[dict[str, Any]]) -> None:
    atomic_json_write(_repo_locks_path(), to_jsonable({"schema_version": 1, "locks": locks, "updated_at": now()}), indent=2, sort_keys=True)


def merge_desired_bundle(existing: RepoBundle, desired: RepoBundle) -> RepoBundle:
    mutable_state = existing.state if existing.state in REPO_BUNDLE_STATES else desired.state
    if mutable_state in {"running", "delivered_waiting_for_qa", "delivered", "verified", "blocked", "rejected", "cancelled", "archived"}:
        desired_state = mutable_state
    else:
        desired_state = desired.state
    return replace(
        existing,
        repo=desired.repo,
        owner_persona_id=desired.owner_persona_id,
        state=desired_state,
        title=desired.title,
        objective=desired.objective,
        stage_ids=desired.stage_ids,
        affected_paths=desired.affected_paths,
        acceptance=desired.acceptance,
        non_goals=desired.non_goals,
        proof_targets=desired.proof_targets,
        proof_requirements=desired.proof_requirements,
        visual_requirements=desired.visual_requirements,
        dependency_bundle_ids=desired.dependency_bundle_ids,
        queue_reason=desired.queue_reason if desired_state == "queued_waiting_dependency" else existing.queue_reason,
        wake_condition=desired.wake_condition if desired_state == "queued_waiting_dependency" else existing.wake_condition,
        updated_at=now(),
    )


def owner_for_repo(repo: str | None) -> str | None:
    normalized = normalize_repo(repo)
    for marker, owner in _REPO_OWNER_RULES:
        if marker in normalized:
            return owner
    return None


def qa_waiting_on(bundles: list[RepoBundle]) -> list[str]:
    return [bundle.id for bundle in bundles if bundle.state not in DELIVERED_REPO_BUNDLE_STATES and bundle.state not in TERMINAL_REPO_BUNDLE_STATES]


def bundle_queue_summary(bundles: list[RepoBundle]) -> list[dict[str, Any]]:
    queued = [bundle for bundle in bundles if bundle.state == "queued_waiting_dependency"]
    return [
        {
            "repo_bundle_id": bundle.id,
            "repo": bundle.repo,
            "owner_persona_id": bundle.owner_persona_id,
            "state": bundle.state,
            "queue_reason": bundle.queue_reason,
            "wake_condition": bundle.wake_condition,
            "dependency_bundle_ids": list(bundle.dependency_bundle_ids or []),
        }
        for bundle in queued
    ]


def repo_bundle_summary(bundle: RepoBundle) -> dict[str, Any]:
    promotion = read_bundle_promotion_record(bundle.task_id, bundle.id)
    return {
        "repo_bundle_id": bundle.id,
        "task_id": bundle.task_id,
        "repo": bundle.repo,
        "owner_persona_id": bundle.owner_persona_id,
        "state": bundle.state,
        "title": bundle.title,
        "objective": bundle.objective,
        "stage_ids": list(bundle.stage_ids or []),
        "affected_paths": list(bundle.affected_paths or []),
        "acceptance": list(bundle.acceptance or []),
        "non_goals": list(bundle.non_goals or []),
        "proof_targets": list(bundle.proof_targets or []),
        "proof_requirements": list(bundle.proof_requirements or []),
        "visual_requirements": list(bundle.visual_requirements or []),
        "dependency_bundle_ids": list(bundle.dependency_bundle_ids or []),
        "contract_input_ids": list(bundle.contract_input_ids or []),
        "contract_output_ids": list(bundle.contract_output_ids or []),
        "assignment_id": bundle.assignment_id,
        "active_run_id": bundle.active_run_id,
        "proof_ids": list(bundle.proof_ids or []),
        "delivery_contract": _bundle_delivery_contract(promotion),
        "checkout_applied": _promotion_applied(promotion),
        "checkout_status": _bundle_checkout_status(promotion),
        "closeout_label": _repo_bundle_closeout_label(bundle, promotion),
        "delivery_capture": dict(getattr(bundle, "delivery_capture", None) or {}),
        "promotion": _promotion_summary(promotion),
        "queue_reason": bundle.queue_reason,
        "wake_condition": bundle.wake_condition,
        "delivered_at": bundle.delivered_at,
        "verified_at": bundle.verified_at,
        "rejected_at": bundle.rejected_at,
        "last_terminal_feedback": dict(bundle.last_terminal_feedback or {}),
        "created_at": bundle.created_at,
        "updated_at": bundle.updated_at,
    }


def repo_bundle_delivery_summary(bundles: list[RepoBundle]) -> dict[str, Any]:
    bundle_list = list(bundles or [])
    states = {bundle.state for bundle in bundle_list}
    delivered_ids = [
        bundle.id
        for bundle in bundle_list
        if bundle.state in {"delivered_waiting_for_qa", "delivered", "verified"}
    ]
    promotions = {
        bundle.id: read_bundle_promotion_record(bundle.task_id, bundle.id)
        for bundle in bundle_list
    }
    promoted_ids = [bundle_id for bundle_id, record in promotions.items() if _promotion_applied(record)]
    any_promoted = bool(promoted_ids)
    return {
        "delivery_contract": "delivery_directive" if any_promoted else REPO_BUNDLE_DELIVERY_CONTRACT,
        "checkout_applied": any_promoted,
        "checkout_status": "promoted" if any_promoted else REPO_BUNDLE_CHECKOUT_STATUS,
        "repo_bundle_ids": [bundle.id for bundle in bundle_list],
        "delivered_repo_bundle_ids": delivered_ids,
        "promoted_repo_bundle_ids": promoted_ids,
        "state_counts": {state: len([bundle for bundle in bundle_list if bundle.state == state]) for state in sorted(states)},
        "closeout_label": _repo_bundle_task_closeout_label(bundle_list, promotions),
    }


def _promotion_applied(promotion: dict[str, Any] | None) -> bool:
    status = str(((promotion or {}).get("promote") or {}).get("status") or "")
    return status in {"promoted", "already_applied"}


def _promotion_summary(promotion: dict[str, Any] | None) -> dict[str, Any] | None:
    if not promotion:
        return None
    promote = promotion.get("promote") or {}
    worktree = promotion.get("worktree") or {}
    return {
        "status": promote.get("status"),
        "reason": promote.get("reason"),
        "commit": promote.get("commit"),
        "worktree_status": worktree.get("status"),
        "recorded_at": promotion.get("recorded_at"),
    }


def _bundle_delivery_contract(promotion: dict[str, Any] | None) -> str:
    return "delivery_directive" if promotion else REPO_BUNDLE_DELIVERY_CONTRACT


def _bundle_checkout_status(promotion: dict[str, Any] | None) -> str:
    if _promotion_applied(promotion):
        return "promoted"
    if promotion and str((promotion.get("promote") or {}).get("status")) == "failed":
        return "promotion_failed"
    return REPO_BUNDLE_CHECKOUT_STATUS


def _repo_bundle_closeout_label(bundle: RepoBundle, promotion: dict[str, Any] | None = None) -> str:
    if _promotion_applied(promotion):
        commit = ((promotion or {}).get("promote") or {}).get("commit")
        suffix = f" as commit {commit}" if commit else ""
        return f"Repo bundle promoted to the checkout by the delivery directive{suffix}."
    if promotion and str((promotion.get("promote") or {}).get("status")) == "failed":
        return "Delivery directive could not promote this bundle; see the bundle_promotion_failed incident."
    if bundle.state in {"delivered_waiting_for_qa", "delivered", "verified"}:
        return "Staged repo bundle delivered; checkout not modified by bundle delivery."
    if bundle.state in {"blocked", "rejected", "cancelled"}:
        return "Repo bundle remained staged; checkout not modified by bundle delivery."
    return "Repo bundle is staged; checkout not modified by bundle delivery."


def _repo_bundle_task_closeout_label(
    bundles: list[RepoBundle], promotions: dict[str, dict[str, Any] | None] | None = None
) -> str:
    if not bundles:
        return "No repo bundles are attached to this task."
    promotions = promotions or {}
    promoted = [bundle for bundle in bundles if _promotion_applied(promotions.get(bundle.id))]
    if promoted and len(promoted) == len(bundles):
        return "All repo bundles promoted to the checkout by the delivery directive."
    if promoted:
        return "Some repo bundles promoted by the delivery directive; others remain staged."
    if all(bundle.state in {"delivered_waiting_for_qa", "delivered", "verified"} for bundle in bundles):
        return "Task repo bundles are staged/delivered only; checkout not modified by bundle delivery."
    if any(bundle.state in {"blocked", "rejected"} for bundle in bundles):
        return "One or more repo bundles need repair; checkout not modified by bundle delivery."
    return "Task repo bundles are staged only; checkout not modified by bundle delivery."


def bundle_id_for(task_id: str, repo: str, *, stage_ids: list[str]) -> str:
    payload = json.dumps(
        {
            "task_id": safe_token(task_id),
            "repo": normalize_repo(repo),
            "stage_ids": sorted(safe_token(item) for item in stage_ids if safe_token(item)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"bundle_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def safe_bundle_state(value: str | None) -> str:
    state = safe_token(value)
    return state if state in REPO_BUNDLE_STATES else "planned"


def normalize_repo(value: str | None) -> str:
    return "".join(ch.lower() for ch in str(value or "").strip() if ch.isalnum() or ch in {"_", "-", ".", "/", "\\"})


def safe_token(value: Any) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(value or "").strip())
    return text.strip("._-")[:120]


def safe_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _dedupe_preserve_order(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for item in items:
        key = str(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
