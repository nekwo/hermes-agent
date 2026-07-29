from __future__ import annotations

import pytest

from agent_runtime.missing_input import create_missing_input_request
from agent_runtime.role_contracts import contract_for_persona
from agent_runtime.worklog import append_persona_worklog, persona_worklog_for_task


def test_simplified_role_contracts_expose_only_product_actions():
    assert contract_for_persona("neko_supervisor")["allowed_actions"] == [
        "assign",
        "report_blocker",
        "request_missing_input",
    ]
    assert contract_for_persona("dev")["allowed_actions"] == ["deliver", "report_blocker", "request_missing_input"]
    assert contract_for_persona("backend_dev")["allowed_actions"] == ["deliver", "report_blocker", "request_missing_input"]
    assert contract_for_persona("qa")["allowed_actions"] == ["approve", "reject", "request_missing_proof"]


def test_missing_input_routes_to_persona_before_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    request = create_missing_input_request(
        task_id="task_1",
        requester_persona_id="dev",
        input_type="backend_contract",
        question="Which backend contract should this Launcher view consume?",
    )

    assert request.route_to == "backend_dev"
    assert request.manifest()["input_type"] == "backend_contract"


def test_missing_input_rejects_unknown_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    with pytest.raises(ValueError):
        create_missing_input_request(task_id="task_1", requester_persona_id="dev", input_type="anything", question="please")


def test_persona_worklog_is_task_and_persona_filterable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    append_persona_worklog(task_id="task_1", persona_id="dev", message="I patched the terminal rows and ran the focused proof.")
    append_persona_worklog(task_id="task_1", persona_id="qa", message="I verified the terminal rows.")

    assert [evt.persona_id for evt in persona_worklog_for_task("task_1")] == ["dev", "qa"]
    assert [evt.persona_id for evt in persona_worklog_for_task("task_1", persona_id="dev")] == ["dev"]
