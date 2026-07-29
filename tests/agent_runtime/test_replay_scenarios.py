from hermes_time import now

from agent_runtime.replay_scenarios import (
    get_scenario,
    list_scenarios,
    record_scenario_candidate,
    replay_all,
    replay_scenario,
)


def make_candidate(payload=None, error="qa_review has unsupported keys: ['notes']", dtype="report_qa_verdict"):
    return record_scenario_candidate(
        task_id="task_x",
        run_id="run_x",
        persona_id="qa",
        decision_type=dtype,
        payload=payload if payload is not None else {"qa_review": {"notes": "extra"}},
        error_class="DecisionPayloadInvalid",
        error_message=error,
    )


def test_record_and_list_scenarios(isolate_agent_runtime_root):
    record = make_candidate()

    assert record is not None
    assert record["scenario_id"].startswith("scen_")
    assert record["status"] == "candidate"
    listed = list_scenarios()
    assert [item["scenario_id"] for item in listed] == [record["scenario_id"]]
    assert get_scenario(record["scenario_id"])["task_id"] == "task_x"


def test_identical_failure_dedupes(isolate_agent_runtime_root):
    first = make_candidate()
    second = make_candidate()

    assert first is not None
    assert second is None
    assert len(list_scenarios()) == 1


def test_payloadless_failure_is_not_captured(isolate_agent_runtime_root):
    record = record_scenario_candidate(
        task_id="task_x",
        persona_id="neko_supervisor",
        decision_type=None,
        payload=None,
        error_class="DecisionPayloadInvalid",
        error_message="model output was not valid JSON",
    )

    assert record is None
    assert list_scenarios() == []


def test_replay_normalized_payload_passes_current_contract(isolate_agent_runtime_root):
    # qa_review unknown keys are normalized (dropped) by current intake, so a
    # historically failing payload should now pass replay.
    record = record_scenario_candidate(
        task_id="task_x",
        persona_id="qa",
        decision_type="report_qa_verdict",
        payload={
            "review_scope": "implementation",
            "qa_review": {
                "qa_review_version": 1,
                "review_scope": "implementation",
                "mission_phase": "qa_release",
                "proof_reviewed": ["proof_1"],
                "decision_basis": "proof passed",
                "remaining_gaps": [],
                "next_owner": "harness",
                "coverage": {
                    "backend_contract": "reviewed",
                    "launcher_integration": "not_required",
                    "visual_or_mcp": "not_required",
                    "cross_stack_join": "not_required",
                },
                "mcp_status": "not_required",
                "notes": "legacy unsupported key",
            }
        },
        error_class="DecisionPayloadInvalid",
        error_message="qa_review has unsupported keys: ['notes']",
    )

    result = replay_scenario(record["scenario_id"])

    assert result["ok"] is True
    assert result["verdict"] == "passes_current_contract"


def test_replay_unknown_scenario(isolate_agent_runtime_root):
    result = replay_scenario("scen_missing")

    assert result["ok"] is False
    assert result["error"] == "scenario_not_found"


def test_replay_all_summarizes(isolate_agent_runtime_root):
    make_candidate()

    summary = replay_all()

    assert summary["total"] == 1
    assert len(summary["passes_current_contract"]) + len(summary["still_failing"]) + len(summary["not_replayable"]) == 1




def test_replay_runs_mission_plan_validation(isolate_agent_runtime_root):
    record = record_scenario_candidate(
        task_id="task_plan",
        persona_id="neko_supervisor",
        decision_type="propose_acceptance",
        payload={
            "objective": "investigate",
            "mission_plan": {
                "stages": [
                    {"id": "stage", "title": "A", "objective": "a", "owner": "backend_dev", "repo": "EterniaBackend", "kind": "investigation"},
                    {"id": "stage", "title": "B", "objective": "b", "owner": "qa", "repo": "EterniaBackend", "kind": "qa", "depends_on": ["missing_stage"]},
                ]
            },
        },
        error_class="DecisionPayloadInvalid",
        error_message="invalid mission_plan: ['duplicate stage id: stage']",
    )

    result = replay_scenario(record["scenario_id"])

    assert result["ok"] is True
    assert result["verdict"] == "still_failing"
    assert "mission_plan" in result["current_error"]


def test_failure_origin_contract_violation_by_default(isolate_agent_runtime_root):
    from agent_runtime.replay_scenarios import classify_failure_origin

    result = classify_failure_origin(payload={"qa_review": {"notes": "x"}}, error_message="qa_review has unsupported keys: ['notes']")

    assert result["failure_origin"] == "contract_violation"


def test_failure_origin_starvation_when_referenced_id_resolves(isolate_agent_runtime_root):
    from agent_runtime.models import Proof, Task
    from agent_runtime.proof_rules import ProofType
    from agent_runtime.replay_scenarios import classify_failure_origin
    from agent_runtime.states import TaskState
    from agent_runtime.store import ProofStore, TaskStore

    ts = now()
    task = Task(id="task_starve", title="t", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony")
    TaskStore().create(task)
    ProofStore().attach(
        Proof(
            id="proof_real123",
            task_id=task.id,
            stage_id="s1",
            type=ProofType.TEST_RUN,
            title="t",
            path_or_value="p",
            created_by="harness",
            created_at=ts,
            metadata={"status": "passed"},
        )
    )

    result = classify_failure_origin(
        task=task,
        payload={"proof_ids": ["proof_real123"]},
        error_message="unknown proof_ids: ['proof_real123']",
    )

    assert result["failure_origin"] == "context_starvation"
    assert any("proof_real123" in item for item in result["origin_evidence"])


def test_failure_origin_overload_only_when_fully_grounded(isolate_agent_runtime_root):
    from types import SimpleNamespace

    from agent_runtime.replay_scenarios import classify_failure_origin

    run = SimpleNamespace(llm={"input_tokens": 120000}, progress={})

    # No load-bearing references + large context = dilution-style overload.
    result = classify_failure_origin(run=run, payload={"x": 1}, error_message="payload has unsupported keys: ['x']")

    assert result["failure_origin"] == "context_overload"


def test_large_context_missing_needed_entity_is_starvation_not_overload(isolate_agent_runtime_root):
    from types import SimpleNamespace

    from agent_runtime.models import Proof, Task
    from agent_runtime.proof_rules import ProofType
    from agent_runtime.replay_scenarios import classify_failure_origin
    from agent_runtime.states import TaskState
    from agent_runtime.store import ProofStore, TaskStore

    ts = now()
    # Task projects NO proofs, but the referenced proof exists in the store.
    task = Task(id="task_bigstarve", title="t", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony")
    TaskStore().create(task)
    ProofStore().attach(
        Proof(id="proof_needed99", task_id=task.id, stage_id="s1", type=ProofType.TEST_RUN, title="t", path_or_value="p", created_by="harness", created_at=ts, metadata={"status": "passed"})
    )
    run = SimpleNamespace(llm={"input_tokens": 120000}, progress={})

    result = classify_failure_origin(task=task, run=run, payload={"proof_ids": ["proof_needed99"]}, error_message="unknown proof_ids: ['proof_needed99']")

    # Large context, but the needed entity was not projected -> starvation wins.
    assert result["failure_origin"] == "context_starvation"
    assert result["context_relevance"]["grounding_coverage"] < 1.0


def test_context_relevance_grounding_coverage(isolate_agent_runtime_root):
    from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Task
    from agent_runtime.replay_scenarios import estimate_context_relevance
    from agent_runtime.states import TaskState
    from agent_runtime.store import TaskStore

    ts = now()
    task = Task(id="task_rel", title="t", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="tony", proof_ids=["proof_projected1"])
    task.mission_plan = MissionPlan(enabled=True, mission_intent=MissionIntent(title="t", objective="o", source_task_id=task.id), current_stage_id="stage_alpha", stages=[MissionPlanStage(id="stage_alpha", title="A", objective="o", owner="dev", repo="EterniaBackend", kind="implementation")])
    TaskStore().create(task)

    rel = estimate_context_relevance(task=task, payload={"proof_ids": ["proof_projected1"], "stage_id": "stage_alpha"})

    assert rel["referenced_count"] == 2
    assert rel["grounding_coverage"] == 1.0
    assert rel["missing_but_in_store"] == []


def test_scenario_record_carries_failure_origin(isolate_agent_runtime_root):
    record = record_scenario_candidate(
        task_id="task_o",
        persona_id="qa",
        decision_type="report_qa_verdict",
        payload={"qa_review": {"notes": "x"}},
        error_class="DecisionPayloadInvalid",
        error_message="qa_review has unsupported keys: ['notes']",
        failure_origin={"failure_origin": "contract_violation", "origin_evidence": []},
    )

    assert record["failure_origin"] == "contract_violation"
