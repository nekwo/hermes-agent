from __future__ import annotations

from agent_runtime.role_contracts import contract_for_persona


def test_simplified_role_contracts_expose_only_product_actions():
    assert contract_for_persona("neko_supervisor")["allowed_actions"] == [
        "assign",
        "report_blocker",
        "request_missing_input",
    ]
    assert contract_for_persona("dev")["allowed_actions"] == ["deliver", "report_blocker", "request_missing_input"]
    assert contract_for_persona("backend_dev")["allowed_actions"] == ["deliver", "report_blocker", "request_missing_input"]
    assert contract_for_persona("qa")["allowed_actions"] == ["approve", "reject", "request_missing_proof"]


# The missing-input routing and persona-worklog tests that lived here were
# retired with ``agent_runtime/missing_input.py`` and ``agent_runtime/worklog.py``
# in S13 — both modules lost their last producer when the task lane went, so the
# behaviour they pinned no longer exists. ``role_contracts`` survives: it still
# describes what a chat persona is allowed to decide.
