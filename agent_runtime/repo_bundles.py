from __future__ import annotations

import json
from typing import Any

from . import paths
from .delivery_directive import read_bundle_promotion_record
from .events import EventLog
from .models import RepoBundle
from .serde import from_jsonable

# S52 removed the write lane, and with it every binding only a writer read:
# ``hashlib`` / ``dataclasses.replace`` / ``hermes_time.now`` /
# ``utils.atomic_json_write`` / ``models.Event`` / ``serde.to_jsonable``, plus
# the ``REPO_BUNDLE_STATES`` / ``TERMINAL_*`` / ``DELIVERED_*`` /
# ``REPO_LOCK_MODES`` / ``WAKE_DEPENDENCY_DELIVERED`` vocabularies and the
# ``_REPO_OWNER_RULES`` table. A dead import is not free (the S41 rule): it keeps
# a retired symbol reachable by name, so the next reachability pass counts it as
# used. ``EventLog`` stays because the constructor still accepts one.
REPO_BUNDLE_DELIVERY_CONTRACT = "staged_bundle_not_applied"
REPO_BUNDLE_CHECKOUT_STATUS = "not_applied"


class RepoBundleStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    # S52 (2026-08-01) removed this store's WRITE lane whole:
    # ``create_or_update_from_task``, ``update``, ``attach_assignment``,
    # ``mark_running``, ``mark_verified``, ``mark_rejected``,
    # ``wake_ready_dependencies``, the hollow ``cancel_superseded`` seam
    # (``return []``), and the two private writers ``_write`` / ``_event`` that
    # only they reached. Not one had a production caller: the last one went with
    # the mission/dispatch lane, and what remained was a store that could only
    # ever be written by its own tests. The seven ``repo_bundle.*`` contracts
    # they emitted are de-registered with them. The READ side below is LIVE --
    # ``status.py`` builds every operator bundle row off ``list_all`` -- so this
    # is a write-lane cut, not a store removal. See
    # tests/agent_runtime/test_s52_repo_bundle_write_lane_removal.py.

    def get(self, task_id: str, bundle_id: str) -> RepoBundle:
        raw = json.loads(paths.repo_bundle_path(task_id, bundle_id).read_text(encoding="utf-8"))
        return from_jsonable(RepoBundle, raw)

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


# S52 also removed the two module-level repo-lock MUTATORS,
# ``acquire_repo_bundle_locks`` / ``release_repo_bundle_locks``, plus the two
# helpers only they reached (``_repo_lock_conflicts``, ``_write_repo_locks``),
# and ``desired_bundles_for_task`` / ``merge_desired_bundle`` / ``qa_waiting_on``
# — the first two reachable only from the deleted
# ``create_or_update_from_task``, the third with no caller at all.
#
# KNOWN CONSEQUENCE, deliberately left for the follow-up wave rather than taken
# here: with both writers gone, nothing can create a row in
# ``repo_bundle_locks.json``, so ``repo_lock_summary()`` below can now only ever
# report ``{"lock_count": 0, "locks": []}`` — and ``status.py`` publishes it as
# ``repo_locks``. That is the S47 item-5 defect class (a wire whose value no
# code path can move), but retiring it means editing the emitted status frame,
# which belongs with the read-side/contract work this wave was scoped out of.
# Recorded in docs/agent-runtime-harness/19-deferred-debt-ledger.md.


def repo_lock_summary() -> dict[str, Any]:
    locks = _read_repo_locks()
    return {"lock_count": len(locks), "locks": locks}


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


# S52: ``bundle_id_for`` / ``safe_bundle_state`` / ``normalize_repo`` /
# ``safe_token`` / ``safe_text`` / ``_dedupe_preserve_order`` and the
# ``owner_for_repo`` + ``_REPO_OWNER_RULES`` pair went with the write lane. Every
# one was reachable ONLY from a deleted writer (id minting, state coercion, and
# payload sanitising are write-time concerns), and none had an importer outside
# this module. Leaving them would be the residue S25 named when it retired
# ``events._safe_int`` with the formatter arm that was its only caller: a private
# helper outliving its only branch.
