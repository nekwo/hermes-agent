"""Checklist payload validation — the one surviving half of the role-checklist lane.

This module is now a small leaf with a single external name:
``validate_checklist_payload_structure``, called from
``decision_contract_registry.validate_payload_keys`` on every typed decision. It
is deliberately left in place rather than folded into the registry — the three
vocabularies below (``CHECKLIST_ITEM_STATUSES``, ``SELF_APPROVAL_STATUSES``,
``CHECKLIST_UPDATE_KEYS``) are the payload contract a persona is repaired
against, and they read better owned by a named module than inlined into a
600-line registry.

How the rest of the module died, in order:

* **S27** removed the DECISION-side half (``validate_decision_checklist_payload``,
  ``sanitize_decision_checklist_payload``, ``apply_decision_checklist_updates``,
  ``stage_checklist_hud``) — the lane that applied a role's checklist updates
  while executing a typed decision, which went with the dispatch loop at S5.
* **S44** removed the STORE half (``RoleChecklistStore``, ``RoleChecklist``,
  ``RoleChecklistItem``, ``checklist_for_task_stage``, ``checklist_summary``,
  ``item_summary``, ``normalize_role_id``, and the template/promotion helpers).

S27 pinned ``checklist_for_task_stage`` as LIVE, and that was true when written:
``RoleChecklistStore.open_or_create`` called it, and ``role_envelopes.py:91``
called that. Its ONLY justification was the ``role_envelopes`` import. S44
deleted ``role_envelopes`` whole, so the chain is dead from the root — the ruling
is transitively falsified, not overridden. See
``tests/agent_runtime/test_s44_role_envelope_family_removal.py``.
"""

from __future__ import annotations

import json
from typing import Any

from .decision_schema import DecisionPayloadInvalid

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
