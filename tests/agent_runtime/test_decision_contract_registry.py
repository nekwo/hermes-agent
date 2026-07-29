from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agent_runtime.context_builder import build_context
from agent_runtime.decision_contract_registry import (
    agent_decision_json_schema,
    allowed_decisions_for_role,
    canonical_role_value,
    contract_hash,
    contract_manifest,
    event_catalog,
    hud_shape_index_for_stage,
    payload_contract,
    verify_registry,
)
from agent_runtime.decision_contract_examples import verify_harness_skill_examples
from agent_runtime.decision_payload_contracts import payload_contract as facade_payload_contract
from agent_runtime.decision_schema import ALLOWED_DECISIONS_BY_ROLE, DECISION_SCHEMA, DecisionType
from agent_runtime.events import ALLOWED_EVENT_TYPES
from agent_runtime.models import AgentRun, Task
from agent_runtime.persona_runtime import build_system_prompt
from agent_runtime.personas import AgentRole
from agent_runtime.states import RunState, TaskState
from hermes_time import now


REPO_ROOT = Path(__file__).resolve().parents[2]


def _task() -> Task:
    ts = now()
    return Task(
        id="task_contract_registry",
        title="Launcher contract smoke",
        description="Validate registry HUD shapes.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        current_stage_id="stage_1",
        affected_repos=["EterniaLauncher"],
    )


def _run(persona_id: str = "dev") -> AgentRun:
    return AgentRun(
        id="run_contract_registry",
        task_id="task_contract_registry",
        persona_id=persona_id,
        state=RunState.RUNNING,
        started_at=now(),
        last_heartbeat_at=now(),
        stage_id="stage_1",
    )


def test_registry_covers_every_decision_type_and_projects_schema():
    result = verify_registry()

    assert result["ok"] is True
    assert {item.value for item in DecisionType} == set(contract_manifest()["decisions"])
    assert result["missing_object_contracts"] == []
    assert result["hud_template_errors"] == []
    assert DECISION_SCHEMA == agent_decision_json_schema()


def test_role_permissions_are_registry_generated():
    for role in AgentRole:
        assert ALLOWED_DECISIONS_BY_ROLE[role] == allowed_decisions_for_role(role)


def test_hud_shapes_do_not_expose_decisions_for_the_wrong_role():
    for role in AgentRole:
        allowed = {item.value for item in allowed_decisions_for_role(role)}
        for shape_id, shape in hud_shape_index_for_stage(role).items():
            assert shape["decision_type"] in allowed, f"{role.value} leaked {shape_id}"

    assert "common.request_human" not in hud_shape_index_for_stage("dev")
    assert "common.needs_context" not in hud_shape_index_for_stage("qa")
    assert "common.report_issue_discovery" not in hud_shape_index_for_stage("neko_supervisor")


def test_payload_contract_facade_matches_registry():
    for decision_type in DecisionType:
        assert facade_payload_contract(decision_type) == payload_contract(decision_type)


def test_role_aliases_support_runtime_persona_ids():
    assert canonical_role_value("neko_supervisor") == AgentRole.ALICE_SUPERVISOR.value
    assert canonical_role_value("backend_dev") == AgentRole.DEV.value
    assert "scope_route" in contract_manifest()["roles"][canonical_role_value("neko_supervisor")]


def test_registry_shapes_remain_available_without_stage_graph():
    registry_shapes = hud_shape_index_for_stage("dev")

    assert contract_hash()
    assert registry_shapes["dev.request_test_run"]["allowed_payload_keys"]
    assert registry_shapes["dev.request_test_run"].get("nested_required", {}) == {}


def test_qa_registry_exposes_nested_and_enum_choices():
    qa_shapes = hud_shape_index_for_stage("qa")
    screenshot = qa_shapes["qa.request_screenshot"]
    verdict = qa_shapes["qa.verdict"]

    assert screenshot["nested_required"]["required_launch_pins"] == ["hermes_profile", "runtime_root_id"]
    assert screenshot["enum_choices"]["mcp_server"] == ["launcher_qa"]
    assert verdict["enum_choices"]["verdict"] == ["approved", "needs_fixes", "blocked"]
    assert "coverage" in verdict["allowed_payload_keys"]


def test_prompt_contract_includes_registry_hash():
    from agent_runtime.models import AgentPersona

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model="test",
        provider="test",
        api_mode="codex_responses",
        toolsets=[],
        system_prompt_path="agent_runtime/prompts/dev.md",
    )

    prompt = build_system_prompt(persona)

    assert contract_hash() in prompt
    assert "request_test_run: required [stage_id]" in prompt


def test_event_allowlist_is_catalog_projected():
    catalog = event_catalog()

    assert ALLOWED_EVENT_TYPES == set(catalog)
    assert catalog["worker_session.context_absorbed"]["display_label"] == "Context absorbed"
    assert catalog["run.progress"]["summary_fields"]


def test_contract_cli_dump_and_verify_examples():
    dump = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "contracts", "dump", "--role", "dev", "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(dump.stdout)
    assert payload["contract_hash"] == contract_hash()
    assert "request_test_run" in payload["allowed_decisions"]
    assert "dev.request_test_run" in payload["decision_menu_shape_ids"]
    assert payload["hud_shapes"]["dev.request_test_run"]["allowed_payload_keys"]
    assert "recipe_id" in payload["hud_shapes"]["dev.request_test_run"]["allowed_payload_keys"]

    neko_dump = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "contracts", "dump", "--role", "neko_supervisor", "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    neko_payload = json.loads(neko_dump.stdout)
    assert neko_payload["role"] == "alice_supervisor"
    assert "neko.scope_route" in neko_payload["decision_menu_shape_ids"]

    verify = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "contracts", "verify-examples", "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    verify_payload = json.loads(verify.stdout)
    assert verify_payload["ok"] is True
    assert verify_payload["skill_examples"]["checked_count"] >= 6


def test_harness_skill_examples_validate_against_live_contracts():
    result = verify_harness_skill_examples()

    assert result["ok"] is True
    assert result["failure_count"] == 0
    # Rationale (doc-08 v4 / N1): the root-node rewrite of harness-mission-lead no
    # longer emits AgentDecision JSON examples (the skill's explicit contract is
    # "Do not emit AgentDecision JSON"), so it is intentionally no longer part of
    # the decision-contract example-validation set. Dropped here, not weakened
    # elsewhere. See tests/agent_runtime/test_root_node_mode.py for the new coverage.
    assert {item["skill"] for item in result["checked"]} >= {
        "harness-dev-delivery",
        "harness-qa-verdict",
        "launcher-analyze-proof",
    }
