"""Gap-1 regression tests: Neko scope cannot silently contradict the goal's named repo.

A goal that literally names a repo (mid-sentence in the description, or via an
operator-pinned repo scope at create time) must not be scoped to a different
repo without a recorded justification. Mismatches raise DecisionPayloadInvalid
so the ticker's repair-feedback lane routes the contradiction back to Neko.
"""

import pytest
from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import Task
from agent_runtime.planning import apply_planning_decision
from agent_runtime.repo_context import canonical_repo_scope_label, explicit_repo_mentions
from agent_runtime.simplified_contract import _internal_execution_decision
from agent_runtime.states import StageStatus, TaskState


def make_task(description: str, *, title: str = "T") -> Task:
    ts = now()
    return Task(id="task_scope", title=title, description=description, state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony")


def acceptance(payload_overrides: dict) -> AgentDecision:
    payload = {
        "objective": "obj",
        "acceptance_criteria": ["done"],
    }
    payload.update(payload_overrides)
    return AgentDecision(type=DecisionType.PROPOSE_ACCEPTANCE, summary="s", rationale="r", payload=payload)


def test_explicit_repo_mentions_mid_sentence():
    text = "Investigate the liveness watchdog. In the hermes-agent repo, list the tests and report."
    assert explicit_repo_mentions(text) == ("hermes-agent",)


def test_explicit_repo_mentions_matches_camelcase_and_alias_forms():
    assert explicit_repo_mentions("Polish the EterniaLauncher shop page") == ("EterniaLauncher",)
    assert explicit_repo_mentions("the agent-runtime-harness repo alias") == ("hermes-agent",)
    assert explicit_repo_mentions("fix eternia-backend serializers") == ("EterniaBackend",)


def test_explicit_repo_mentions_ignores_generic_words():
    assert explicit_repo_mentions("Add a launcher button and a backend cache") == ()


def test_canonical_repo_scope_label_resolves_aliases():
    assert canonical_repo_scope_label("launcher") == "EterniaLauncher"
    assert canonical_repo_scope_label("Hermes-Agent") == "hermes-agent"
    assert canonical_repo_scope_label("not-a-repo") is None


def test_named_repo_cannot_scope_to_other_repo_without_justification(isolate_agent_runtime_root):
    t = make_task("In the hermes-agent repo, audit the liveness tests and report findings.")
    with pytest.raises(DecisionPayloadInvalid) as exc:
        apply_planning_decision(t, acceptance({"affected_repos": ["EterniaLauncher"]}), actor="neko_supervisor")
    message = str(exc.value)
    assert "hermes-agent" in message
    assert "scope_override_reason" in message


def test_named_repo_mismatch_with_recorded_justification_is_applied(isolate_agent_runtime_root):
    t = make_task("In the hermes-agent repo, audit the liveness tests and report findings.")
    log = EventLog()
    apply_planning_decision(
        t,
        acceptance(
            {
                "affected_repos": ["EterniaLauncher"],
                "scope_override_reason": "goal text is stale; the liveness UI regression lives in the Launcher",
            }
        ),
        actor="neko_supervisor",
        event_log=log,
    )
    assert t.affected_repos == ["EterniaLauncher"]
    events = [evt for evt in log.tail(20) if evt.type == "scope.override_recorded"]
    assert len(events) == 1
    assert events[0].payload["named_repo_scope"] == ["hermes-agent"]
    assert "stale" in events[0].payload["scope_override_reason"]


def test_matching_scope_passes_without_override(isolate_agent_runtime_root):
    t = make_task("In the hermes-agent repo, audit the liveness tests.")
    apply_planning_decision(t, acceptance({"affected_repos": ["hermes-agent"]}), actor="neko_supervisor")
    assert t.affected_repos == ["hermes-agent"]
    assert t.state == TaskState.RUNNING


def test_cross_stack_subset_scope_passes(isolate_agent_runtime_root):
    t = make_task("Wire the EterniaLauncher shop to the new eternia-backend entitlement route.")
    apply_planning_decision(t, acceptance({"affected_repos": ["EterniaBackend"]}), actor="neko_supervisor")
    assert t.affected_repos == ["EterniaBackend"]


def test_alias_scope_is_canonicalized_on_apply(isolate_agent_runtime_root):
    t = make_task("no repo named here")
    apply_planning_decision(t, acceptance({"affected_repos": ["launcher"]}), actor="neko_supervisor")
    assert t.affected_repos == ["EterniaLauncher"]


def test_free_form_scope_stays_out_of_contract(isolate_agent_runtime_root):
    t = make_task("In the hermes-agent repo, audit the liveness tests.")
    apply_planning_decision(t, acceptance({"affected_repos": ["X:/somewhere/custom-checkout"]}), actor="neko_supervisor")
    assert t.affected_repos == ["X:/somewhere/custom-checkout"]


def test_declared_scope_conflict_is_rejected(isolate_agent_runtime_root):
    t = make_task("generic description without repo names")
    t.harness_self_heal["repo_scope_pinned"] = ["hermes-agent"]
    with pytest.raises(DecisionPayloadInvalid) as exc:
        apply_planning_decision(t, acceptance({"affected_repos": ["EterniaLauncher"]}), actor="neko_supervisor")
    assert "pinned" in str(exc.value)


def test_declared_scope_expansion_is_rejected(isolate_agent_runtime_root):
    t = make_task("generic description without repo names")
    t.harness_self_heal["repo_scope_pinned"] = ["EterniaLauncher"]
    with pytest.raises(DecisionPayloadInvalid) as exc:
        apply_planning_decision(
            t,
            acceptance({"affected_repos": ["hermes-agent", "EterniaBackend", "EterniaLauncher"]}),
            actor="neko_supervisor",
        )
    assert "pinned" in str(exc.value)


def test_declared_scope_expansion_with_override_is_rejected(isolate_agent_runtime_root):
    t = make_task("generic description without repo names")
    t.harness_self_heal["repo_scope_pinned"] = ["EterniaLauncher"]
    with pytest.raises(DecisionPayloadInvalid) as exc:
        apply_planning_decision(
            t,
            acceptance(
                {
                    "affected_repos": ["hermes-agent", "EterniaBackend", "EterniaLauncher"],
                    "scope_override_reason": "Neko thinks this should be cross-stack",
                }
            ),
            actor="neko_supervisor",
        )
    message = str(exc.value)
    assert "pinned" in message
    assert "cannot be overridden" in message


def test_declared_scope_match_passes(isolate_agent_runtime_root):
    t = make_task("generic description without repo names")
    t.harness_self_heal["repo_scope_pinned"] = ["hermes-agent"]
    apply_planning_decision(t, acceptance({"affected_repos": ["hermes-agent"]}), actor="neko_supervisor")
    assert t.affected_repos == ["hermes-agent"]


def test_declared_scope_wins_over_graph_release_union(isolate_agent_runtime_root):
    from agent_runtime.models import MissionPlan, MissionPlanStage
    from agent_runtime.planning import _release_stage_affected_repos

    t = make_task("Launcher-only trust probe.")
    t.affected_repos = ["hermes-agent", "EterniaBackend", "EterniaLauncher"]
    t.harness_self_heal["repo_scope_pinned"] = ["EterniaLauncher"]
    t.mission_plan = MissionPlan(
        enabled=True,
        current_stage_id="implement",
        stages=[
            MissionPlanStage(id="scope", title="Scope", objective="Scope", owner="neko_supervisor", kind="scope", repo="hermes-agent", status=StageStatus.PASSED),
            MissionPlanStage(id="backend_implementation", title="Backend", objective="Backend", owner="backend_dev", kind="implementation", repo="EterniaBackend", status=StageStatus.PASSED),
            MissionPlanStage(id="implement", title="Launcher", objective="Launcher", owner="dev", kind="implementation", repo="EterniaLauncher", status=StageStatus.READY),
        ],
    )

    assert _release_stage_affected_repos(t, "EterniaLauncher") == ["EterniaLauncher"]


def test_scope_route_projection_carries_override_reason():
    t = make_task("In the hermes-agent repo, audit the liveness tests.")
    decision = AgentDecision(
        type=DecisionType.SCOPE_ROUTE,
        summary="s",
        rationale="r",
        payload={
            "objective": "obj",
            "acceptance_criteria": ["done"],
            "target_owner": "dev",
            "target_repo": "EterniaLauncher",
            "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed"},
            "scope_override_reason": "goal text is stale",
        },
    )
    projected = _internal_execution_decision(t, decision)
    assert projected.type == DecisionType.SCOPE_ROUTE
    assert projected.payload["target_repo"] == "EterniaLauncher"
    assert projected.payload["scope_override_reason"] == "goal text is stale"


def test_declared_repo_scope_is_kept_on_task():
    task = make_task("Audit liveness")
    task.harness_self_heal["repo_scope_pinned"] = ["hermes-agent"]
    assert task.harness_self_heal["repo_scope_pinned"] == ["hermes-agent"]


def test_default_plan_stage_release_does_not_leak_placeholder_repo(isolate_agent_runtime_root):
    """Live regression 2026-07-03 (task_0cf230b7): Neko's validated acceptance
    set affected_repos=['hermes-agent'], then the typed-plan stage release
    overwrote it with the default blueprint's placeholder stage repo
    (EterniaBackend) and the downstream gate ran in the wrong repo."""

    from agent_runtime.default_plan import ensure_default_mission_plan

    t = make_task("Bounded no-edit investigation. In the hermes-agent repo, verify the daemon status contract and report.")
    ensure_default_mission_plan(t)
    apply_planning_decision(
        t,
        acceptance({"affected_repos": ["hermes-agent"]}),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    assert t.affected_repos == ["hermes-agent"]


def test_default_plan_cross_stack_release_preserves_graph_repo_union(isolate_agent_runtime_root):
    from agent_runtime.default_plan import ensure_default_mission_plan

    t = make_task("Fork check: parallel Backend and Launcher health.")
    ensure_default_mission_plan(t)
    apply_planning_decision(
        t,
        acceptance(
            {
                "objective": "Fork check: parallel Backend and Launcher health.",
                "acceptance_criteria": ["Backend and Launcher lanes both finish with proof."],
                "affected_repos": ["EterniaBackend"],
            }
        ),
        actor="neko_supervisor",
        mission_plan_flow=True,
    )

    assert t.affected_repos == ["hermes-agent", "EterniaBackend", "EterniaLauncher"]
