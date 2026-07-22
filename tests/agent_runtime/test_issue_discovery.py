import pytest

from hermes_time import now
from agent_runtime.context_builder import build_context, render_context
from agent_runtime.decision_contracts import validate_planning_decision
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType, validate_decision_for_role
from agent_runtime.models import AgentRun, Proof, Task
from agent_runtime.observability import build_observability
from agent_runtime.planning import apply_planning_decision
from agent_runtime.proof_rules import ProofType
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import IncidentStore, ProofStore, TaskStore
from agent_runtime.ticker import TickEngine
from hermes_cli import harness as harness_cli


def make_task(state=TaskState.RUNNING):
    ts = now()
    return Task(
        id="task_parent",
        title="Parent mission",
        description="Ship the parent mission",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        acceptance_criteria=["parent tests pass"],
        non_goals=["do not fix unrelated import crash"],
    )


def discovery_decision(**overrides):
    payload = {
        "title": "Unrelated import crash",
        "summary": "Importing a neighboring module crashes outside this mission scope.",
        "evidence": ["pytest tests/other_test.py failed with ImportError"],
        "affected_paths": ["agent_runtime/other.py"],
        "severity": "high",
        "relationship_hint": "fork_child",
        "suggested_child_title": "Fix unrelated import crash",
        "suggested_child_description": "Repair the import crash separately from the parent mission.",
        "suggested_acceptance_criteria": ["Focused import regression test passes"],
    }
    payload.update(overrides)
    return AgentDecision(
        type=DecisionType.REPORT_ISSUE_DISCOVERY,
        summary="found unrelated issue",
        rationale="It is outside the parent acceptance criteria.",
        payload=payload,
    )


def triage_decision(**overrides):
    payload = {
        "discovery_id": "disc_missing",
        "decision": "fork_child",
        "rationale": "Separate scope with separate proof.",
        "child_title": "Fix unrelated import crash",
        "child_description": "Repair the import crash separately from the parent mission.",
        "child_acceptance_criteria": ["Focused import regression test passes"],
        "priority": "high",
    }
    payload.update(overrides)
    return AgentDecision(
        type=DecisionType.TRIAGE_ISSUE_DISCOVERY,
        summary="fork child mission",
        rationale="PM keeps parent focused.",
        payload=payload,
    )


def test_legacy_discovery_is_archive_only_for_live_roles():
    decision = discovery_decision()
    with pytest.raises(DecisionPayloadInvalid):
        validate_decision_for_role(decision, "dev")
    with pytest.raises(DecisionPayloadInvalid):
        validate_decision_for_role(decision, "qa")
    with pytest.raises(DecisionPayloadInvalid):
        validate_decision_for_role(decision, "pm")


def test_discovery_payload_validation_rejects_bad_relationship():
    with pytest.raises(DecisionPayloadInvalid):
        validate_planning_decision(discovery_decision(relationship_hint="maybe"))


def test_record_discovery_preserves_parent_state_and_adds_safe_event():
    task = make_task()
    before = task.state

    apply_planning_decision(task, discovery_decision(), actor="dev")

    assert task.state == before
    assert len(task.issue_discoveries) == 1
    discovery = task.issue_discoveries[0]
    assert discovery["id"].startswith("disc_")
    assert discovery["triage_status"] == "untriaged"
    assert discovery["relationship_hint"] == "fork_child"


def test_pm_triage_fork_child_creates_exactly_one_child_and_keeps_parent_moving():
    parent_store = TaskStore()
    parent_store.create(make_task())
    parent = parent_store.get("task_parent")
    apply_planning_decision(parent, discovery_decision(), actor="dev")
    parent_store.update(parent, actor="dev", reason="discovery")
    discovery_id = parent.issue_discoveries[0]["id"]

    triage = triage_decision(discovery_id=discovery_id)
    apply_planning_decision(parent, triage, actor="pm", task_store=parent_store, incident_store=IncidentStore())
    parent_store.update(parent, actor="pm", reason="triaged")
    apply_planning_decision(parent, triage, actor="pm", task_store=parent_store, incident_store=IncidentStore())

    parent = parent_store.get("task_parent")
    discovery = parent.issue_discoveries[0]
    children = [task for task in parent_store.list_all() if task.parent_task_id == "task_parent"]
    assert parent.state == TaskState.RUNNING
    assert discovery["triage_status"] == "forked"
    assert discovery["child_task_id"] == children[0].id
    assert len(children) == 1
    assert children[0].acceptance_criteria == ["Focused import regression test passes"]
    assert children[0].requested_by == f"harness:issue_discovery:{discovery_id}"
    assert "max_child_depth:1" in children[0].risk_flags
    assert any("Do not fork additional child missions" in item for item in children[0].non_goals)


def test_child_mission_discovery_is_reported_instead_of_forking_grandchild():
    task_store = TaskStore()
    parent = make_task()
    child = make_task()
    child.id = "task_child"
    child.parent_task_id = parent.id
    task_store.create(parent)
    task_store.create(child)
    child = task_store.get("task_child")
    apply_planning_decision(child, discovery_decision(), actor="dev")
    discovery_id = child.issue_discoveries[0]["id"]

    apply_planning_decision(child, triage_decision(discovery_id=discovery_id), actor="pm", task_store=task_store, incident_store=IncidentStore())

    children = [task for task in task_store.list_all() if task.parent_task_id == "task_child"]
    discovery = child.issue_discoveries[0]
    assert children == []
    assert discovery["triage_status"] == "reported"
    assert discovery["final_report"] is True
    assert discovery["final_report_reason"] == "child_mission_depth_or_sibling_limit_reached"
    assert f"final_gap_report:{discovery_id}" in child.risk_flags


def test_parent_with_existing_child_reports_second_fork_instead_of_spawning_sibling_tree():
    task_store = TaskStore()
    parent = make_task()
    existing_child = make_task()
    existing_child.id = "task_existing_child"
    existing_child.parent_task_id = parent.id
    task_store.create(parent)
    task_store.create(existing_child)
    parent = task_store.get("task_parent")
    apply_planning_decision(parent, discovery_decision(), actor="qa")
    discovery_id = parent.issue_discoveries[0]["id"]

    apply_planning_decision(parent, triage_decision(discovery_id=discovery_id), actor="pm", task_store=task_store, incident_store=IncidentStore())

    direct_children = [task for task in task_store.list_all() if task.parent_task_id == "task_parent"]
    discovery = parent.issue_discoveries[0]
    assert len(direct_children) == 1
    assert direct_children[0].id == "task_existing_child"
    assert discovery["triage_status"] == "reported"
    assert discovery["final_report"] is True


def test_same_scope_triage_allows_one_bounded_test_fix_pass_then_reports_remaining_gaps():
    task_store = TaskStore()
    task_store.create(make_task())
    task = task_store.get("task_parent")
    apply_planning_decision(task, discovery_decision(relationship_hint="same_scope"), actor="qa")
    first_id = task.issue_discoveries[0]["id"]
    apply_planning_decision(task, triage_decision(discovery_id=first_id, decision="same_scope"), actor="pm", task_store=task_store, incident_store=IncidentStore())

    apply_planning_decision(task, discovery_decision(title="Second same-scope gap", relationship_hint="same_scope"), actor="qa")
    second_id = task.issue_discoveries[1]["id"]
    apply_planning_decision(task, triage_decision(discovery_id=second_id, decision="same_scope"), actor="pm", task_store=task_store, incident_store=IncidentStore())

    first, second = task.issue_discoveries
    assert first["triage_status"] == "same_scope"
    assert first["bounded_test_fix_pass"] == "allowed_once"
    assert "bounded_test_fix_pass_used" in task.risk_flags
    assert second["triage_status"] == "reported"
    assert second["final_report_reason"] == "bounded_test_fix_pass_already_used"


def test_pm_triage_blocks_parent_and_opens_incident_for_blocker():
    task_store = TaskStore(); incident_store = IncidentStore()
    task_store.create(make_task())
    parent = task_store.get("task_parent")
    apply_planning_decision(parent, discovery_decision(relationship_hint="blocks_current"), actor="dev")
    discovery_id = parent.issue_discoveries[0]["id"]

    apply_planning_decision(parent, triage_decision(discovery_id=discovery_id, decision="blocks_current"), actor="pm", task_store=task_store, incident_store=incident_store)

    assert parent.state == TaskState.BLOCKED
    incidents = incident_store.list_open()
    assert len(incidents) == 1
    assert incidents[0].kind == "scope_blocker"


def test_severe_untriaged_discovery_routes_to_neko_mission_lead_before_dev():
    from agent_runtime.state_machine import MissionStateMachine
    from agent_runtime.actions import HarnessActionType
    task = make_task(TaskState.RUNNING)
    apply_planning_decision(task, discovery_decision(severity="critical", relationship_hint="fork_child"), actor="dev")

    action = MissionStateMachine().next_action(task)

    assert action.type == HarnessActionType.RUN_SLOT
    assert "Neko Mission Lead issue discovery triage" in action.reason


def test_context_snapshot_and_observability_show_safe_discovery_handles():
    task_store = TaskStore(); task = make_task(); apply_planning_decision(task, discovery_decision(), actor="dev"); task_store.create(task)
    run = AgentRun(id="run_1", persona_id="pm", task_id=task.id, stage_id=None, state=RunState.RUNNING, started_at=now(), last_heartbeat_at=now())

    rendered = render_context(build_context(task, run))
    snap = build_snapshot(task_store=task_store)
    obs = build_observability(tasks=[task], runs=[], incidents=[], proofs=[], daemon_status={"state": "idle"})
    encoded = str(snap) + str(obs)

    assert "Issue Discoveries" in rendered
    assert task.issue_discoveries[0]["id"] in rendered
    assert list(snap["goals"].values())[0]["issue_discovery_counts"]["untriaged"] == 1
    assert list(snap["goals"].values())[0]["untriaged_issue_severities"] == ["high"]
    assert obs["signals"]["untriaged_issue_discoveries"] == 1
    assert any(item["kind"] == "issue_discovery_triage_needed" for item in obs["interventions"])
    assert "pytest tests/other_test.py failed" not in encoded


def test_child_proof_does_not_satisfy_parent_gate():
    parent_store = TaskStore(); proof_store = ProofStore(); ts = now()
    parent = make_task(TaskState.RUNNING); child = make_task(TaskState.RUNNING)
    child.id = "task_child"; child.parent_task_id = parent.id
    parent_store.create(parent); parent_store.create(child)
    proof_store.attach(Proof(id="proof_child_test", task_id=child.id, stage_id=None, type=ProofType.TEST_RUN, title="child test", path_or_value="ok", created_by="qa", created_at=ts, redaction_status="safe"))

    snap = build_snapshot(task_store=parent_store, proof_store=proof_store)
    parent_summary = next(item for item in list(snap["goals"].values()) if item["task_id"] == parent.id)

    assert parent_summary["missing_proof"]


class DiscoveryRuntime:
    def run_tick(self, persona, ctx, *, run):
        return discovery_decision()


class TriageRuntime:
    def __init__(self, discovery_id):
        self.discovery_id = discovery_id
    def run_tick(self, persona, ctx, *, run):
        return triage_decision(discovery_id=self.discovery_id)


def test_tick_flow_reports_then_forks_child_with_fake_personas():
    task_store = TaskStore(); task_store.create(make_task(TaskState.RUNNING))
    dev_result = TickEngine(task_store=task_store, persona_runtime=DiscoveryRuntime()).tick_once()
    parent = task_store.get("task_parent")
    discovery_id = parent.issue_discoveries[0]["id"]

    pm_result = TickEngine(task_store=task_store, persona_runtime=TriageRuntime(discovery_id)).tick_once()
    children = [task for task in task_store.list_all() if task.parent_task_id == "task_parent"]

    assert dev_result.actions_taken[0].ok
    assert pm_result.actions_taken[0].ok
    assert len(children) == 1


def test_cli_issue_list_show_and_triage_create_child(capsys):
    task_store = TaskStore(); task_store.create(make_task())
    parent = task_store.get("task_parent")
    apply_planning_decision(parent, discovery_decision(), actor="dev")
    task_store.update(parent, actor="dev", reason="discovery")
    discovery_id = parent.issue_discoveries[0]["id"]

    list_args = type("Args", (), {"task_id": "task_parent", "json": False})()
    show_args = type("Args", (), {"discovery_id": discovery_id, "json": False})()
    triage_args = type("Args", (), {
        "discovery_id": discovery_id,
        "decision": "fork_child",
        "child_title": "CLI child",
        "child_description": "CLI child mission",
        "acceptance": ["CLI acceptance passes"],
        "rationale": "CLI triage",
        "priority": "medium",
        "json": False,
    })()

    assert harness_cli._cmd_issue_list(list_args) == 0
    assert discovery_id in capsys.readouterr().out
    assert harness_cli._cmd_issue_show(show_args) == 0
    assert "Unrelated import crash" in capsys.readouterr().out
    assert harness_cli._cmd_issue_triage(triage_args) == 0
    out = capsys.readouterr().out

    assert "child_task_id" in out
    assert len([task for task in task_store.list_all() if task.parent_task_id == "task_parent"]) == 1
