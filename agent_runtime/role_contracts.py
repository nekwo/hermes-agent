from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


NEKO_ACTIONS = ("assign", "report_blocker", "request_missing_input")
DEV_ACTIONS = ("deliver", "report_blocker", "request_missing_input")
QA_ACTIONS = ("approve", "reject", "request_missing_proof")
SIMPLIFIED_NEKO_ACTIONS = ("scope_route", "block", "escalate")
SIMPLIFIED_DEV_ACTIONS = ("hand_off", "block", "escalate")
SIMPLIFIED_QA_ACTIONS = ("qa_verdict", "block", "escalate")


@dataclass(frozen=True, slots=True)
class SimplifiedRoleContract:
    role_id: str
    display_name: str
    allowed_actions: tuple[str, ...]
    delivery_template: dict[str, Any] = field(default_factory=dict)
    hud_rules: tuple[str, ...] = ()

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "role_id": self.role_id,
            "display_name": self.display_name,
            "allowed_actions": list(self.allowed_actions),
            "delivery_template": dict(self.delivery_template),
            "hud_rules": list(self.hud_rules),
        }


def contract_for_persona(persona_id: str | None, role: str | None = None, *, simplified: bool = False) -> dict[str, Any]:
    key = (persona_id or role or "").lower()
    role_key = (role or "").lower()
    if "qa" in key or role_key == "qa":
        return (simplified_qa_contract() if simplified else qa_contract()).manifest()
    if "backend" in key:
        return (simplified_backend_dev_contract() if simplified else backend_dev_contract()).manifest()
    if "dev" in key or role_key == "dev":
        return (simplified_launcher_dev_contract() if simplified else launcher_dev_contract()).manifest()
    return (simplified_neko_contract() if simplified else neko_contract()).manifest()


def neko_contract() -> SimplifiedRoleContract:
    return SimplifiedRoleContract(
        role_id="neko_supervisor",
        display_name="Neko Mission Lead",
        allowed_actions=NEKO_ACTIONS,
        delivery_template={
            "action": "scope_route",
            "owner": "launcher_dev | backend_dev | qa",
            "objective": "specific next outcome",
            "acceptance": ["observable finish criteria"],
            "proof_expectation": "command | visual | no_product_edit",
        },
        hud_rules=(
            "Pick the next owner and the smallest complete assignment.",
            "Ask another persona for missing input before asking the operator.",
            "Report a blocker only when no persona can answer it.",
        ),
    )


def launcher_dev_contract() -> SimplifiedRoleContract:
    return SimplifiedRoleContract(
        role_id="dev",
        display_name="Launcher Dev Agent",
        allowed_actions=DEV_ACTIONS,
        delivery_template={
            "action": "deliver",
            "changed_files": ["path"],
            "proofs": ["proof_id"],
            "summary": "what changed and why it satisfies the assignment",
            "needs_qa": True,
        },
        hud_rules=(
            "Make the product change and run the most relevant proof in the same run.",
            "Use request_missing_input for backend contracts, visual questions, or scope ambiguity.",
            "Do not ask the harness for proof recipes when you can run the proof directly.",
        ),
    )


def backend_dev_contract() -> SimplifiedRoleContract:
    return SimplifiedRoleContract(
        role_id="backend_dev",
        display_name="Backend Dev Agent",
        allowed_actions=DEV_ACTIONS,
        delivery_template={
            "action": "deliver",
            "changed_files": ["path"],
            "proofs": ["proof_id"],
            "summary": "backend contract or runtime behavior delivered",
            "needs_qa": True,
        },
        hud_rules=(
            "Make the backend/runtime change and run the most relevant proof in the same run.",
            "Use request_missing_input when the Launcher, QA, or Neko can answer missing context.",
            "Prefer one bounded proof after each environment fix.",
        ),
    )


def qa_contract() -> SimplifiedRoleContract:
    return SimplifiedRoleContract(
        role_id="qa",
        display_name="QA Agent",
        allowed_actions=QA_ACTIONS,
        delivery_template={
            "action": "approve | reject",
            "proofs_reviewed": ["proof_id"],
            "verdict": "approved | rejected",
            "summary": "final outcome evidence",
            "follow_up": "none | specific required fix",
        },
        hud_rules=(
            "Validate final outcome, not every intermediate patch.",
            "Request missing proof only when the delivered evidence cannot certify the outcome.",
            "Reject with the smallest actionable fix when proof fails.",
        ),
    )


def simplified_neko_contract() -> SimplifiedRoleContract:
    base = neko_contract()
    return SimplifiedRoleContract(
        role_id=base.role_id,
        display_name=base.display_name,
        allowed_actions=SIMPLIFIED_NEKO_ACTIONS,
        delivery_template={**base.delivery_template, "action": "scope_route"},
        hud_rules=base.hud_rules,
    )


def simplified_launcher_dev_contract() -> SimplifiedRoleContract:
    return SimplifiedRoleContract(
        role_id="dev",
        display_name="Launcher Dev Agent",
        allowed_actions=SIMPLIFIED_DEV_ACTIONS,
        delivery_template={
            "action": "hand_off",
            "summary": "what is ready for Harness attribution/proof",
        },
        hud_rules=(
            "Use hand_off when your slice is ready; Harness captures diff and runs the authoritative gate.",
            "Use block for exact missing prerequisites.",
            "Use escalate only for out-of-scope or systemic issues.",
        ),
    )


def simplified_backend_dev_contract() -> SimplifiedRoleContract:
    return SimplifiedRoleContract(
        role_id="backend_dev",
        display_name="Backend Dev Agent",
        allowed_actions=SIMPLIFIED_DEV_ACTIONS,
        delivery_template={
            "action": "hand_off",
            "summary": "backend slice ready for Harness attribution/proof",
        },
        hud_rules=(
            "Use hand_off when your slice is ready; Harness captures diff and runs the authoritative gate.",
            "Use block for exact missing prerequisites.",
            "Use escalate only for out-of-scope or systemic issues.",
        ),
    )


def simplified_qa_contract() -> SimplifiedRoleContract:
    return SimplifiedRoleContract(
        role_id="qa",
        display_name="QA Agent",
        allowed_actions=SIMPLIFIED_QA_ACTIONS,
        delivery_template={
            "action": "qa_verdict",
            "proofs_reviewed": ["proof_id"],
            "verdict": "passed | failed | needs_fixes",
            "summary": "final outcome evidence",
            "follow_up": "none | specific required fix",
        },
        hud_rules=qa_contract().hud_rules,
    )
