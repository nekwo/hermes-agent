from __future__ import annotations

import pytest

from hermes_time import now

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.context_builder import build_context
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.models import AgentPersona, MissionIntent, MissionPlan, MissionPlanStage, Proof, Task
from agent_runtime.proof_batches import ProofBatchStore
from agent_runtime.proof_rules import ProofType
from agent_runtime.role_checklists import (
    RoleChecklistStore,
    apply_decision_checklist_updates,
    sanitize_decision_checklist_payload,
    stage_checklist_hud,
    validate_decision_checklist_payload,
)
from agent_runtime.role_envelopes import RoleEnvelopeStore
from agent_runtime.runtime_config import RoleEnvelopeConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import ProofStore, RunStore, TaskStore
from agent_runtime.ticker import TickEngine


def _task(task_id: str = "task_stage52", state: TaskState = TaskState.RUNNING) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Stage 52 role envelope smoke",
        description="Keep one competent role worker moving with durable checklist proof.",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["hermes-agent"],
        current_stage_id="stage_1",
    )


def _config(enabled: bool = True) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(role_envelope=RoleEnvelopeConfig(enabled=enabled))


def _persona(persona_id: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} Agent",
        role="dev",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal"],
        system_prompt_path="agent_runtime/prompts/dev.md",
    )


class ChecklistProofRuntime:
    def __init__(self, *, item_id: str = "inspect"):
        self.item_id = item_id
        self.contexts = []

    def run_tick(self, persona, ctx, *, run):
        self.contexts.append(ctx)
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="Collect proof and self-approve bounded work.",
            rationale="The active checklist item is complete once the deterministic proof passes.",
            payload={
                "stage_id": "implement",
                "commands": ["printf stage52-ok\\n"],
                "active_checklist_item_id": self.item_id,
                "self_approval_status": "ready_for_qa",
                "checklist_updates": [
                    {
                        "item_id": self.item_id,
                        "status": "self_approved",
                        "summary": "Context inspection complete and proof requested.",
                    }
                ],
            },
        )


class PassingProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands, **_kwargs):
        proof = Proof(
            id=f"proof_{task.id}_{stage_id}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="passing proof",
            path_or_value="stage52-ok",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "run_id": run_id, "actor_requested": actor},
            redaction_status="safe",
        )
        return [self.proof_store.attach(proof)]


def test_role_envelope_config_defaults_off():
    cfg = AgentRuntimeConfig()

    assert cfg.role_envelope.enabled is False
    assert cfg.role_envelope.checklist_hud_enabled is True
    assert cfg.role_envelope.max_same_session_continuations > 0


def test_checklist_hud_is_gated_and_lists_valid_choices():
    tasks = TaskStore()
    runs = RunStore()
    task = _task("task_hud")
    tasks.create(task)
    run = runs.open_run("dev", task.id, stage_id="stage_1")

    assert stage_checklist_hud(task, "dev", run, config=_config(False)) is None

    hud = stage_checklist_hud(task, "dev", run, config=_config(True))

    assert hud["role_id"] == "dev"
    assert hud["valid_item_ids"]
    assert "self_approved" in hud["valid_statuses"]
    assert "ready_for_qa" in hud["valid_self_approval_statuses"]
    assert hud["operational_scope"] == "role_local"
    assert hud["can_promote_global_stage"] is False
    assert "local subtasks" in hud["promotion_rule"]


def test_checklist_validation_rejects_unknown_item_and_invalid_status():
    tasks = TaskStore()
    task = _task("task_validation")
    tasks.create(task)
    RoleChecklistStore().open_or_create(task=task, role_id="dev", mission_stage_id="stage_1")

    with pytest.raises(DecisionPayloadInvalid, match="valid_item_ids"):
        validate_decision_checklist_payload(
            task,
            role_id="dev",
            mission_stage_id="stage_1",
            payload={"active_checklist_item_id": "invented_item"},
            config=_config(True),
        )

    with pytest.raises(DecisionPayloadInvalid, match="valid_statuses"):
        validate_decision_checklist_payload(
            task,
            role_id="dev",
            mission_stage_id="stage_1",
            payload={"checklist_updates": [{"item_id": "inspect_context", "status": "doneish"}]},
            config=_config(True),
        )

    with pytest.raises(DecisionPayloadInvalid, match="each checklist update must be an object"):
        validate_decision_checklist_payload(
            task,
            role_id="dev",
            mission_stage_id="stage_1",
            payload={"checklist_updates": ["inspect_context"]},
            config=_config(True),
        )


def test_checklist_validation_rejects_invented_stage_promotion_fields():
    tasks = TaskStore()
    task = _task("task_local_only")
    tasks.create(task)
    RoleChecklistStore().open_or_create(task=task, role_id="dev", mission_stage_id="stage_1")

    with pytest.raises(DecisionPayloadInvalid, match="allowed_update_keys"):
        validate_decision_checklist_payload(
            task,
            role_id="dev",
            mission_stage_id="stage_1",
            payload={
                "checklist_updates": [
                    {
                        "item_id": "inspect",
                        "status": "in_progress",
                        "create_global_stage": {"id": "invented_cross_role_stage"},
                    }
                ]
            },
            config=_config(True),
        )


def test_checklist_sanitizer_drops_cross_role_verified_updates_without_invalidating_decision():
    task = _task("task_sanitize_foreign_verify")
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="backend_contract_smoke",
        stages=[
            MissionPlanStage(
                id="backend_contract_smoke",
                title="Backend Contract Smoke",
                objective="Collect backend proof.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="proof_only",
                status=StageStatus.READY_FOR_QA,
            )
        ],
    )
    RoleChecklistStore().open_or_create(task=task, role_id="neko_supervisor", mission_stage_id="backend_contract_smoke")
    payload = {
        "stage_id": "backend_contract_smoke",
        "target_stage_id": "qa_release",
        "checklist_updates": [
            {
                "item_id": "inspect_contract",
                "status": "verified",
                "summary": "Neko should not verify backend-owned local checklist items.",
            }
        ],
    }

    sanitized, ignored = sanitize_decision_checklist_payload(
        task,
        role_id="neko_supervisor",
        mission_stage_id="backend_contract_smoke",
        payload=payload,
        config=_config(True),
    )

    assert "checklist_updates" not in sanitized
    assert ignored == [
        {
            "item_id": "inspect_contract",
            "status": "verified",
            "owner_role": "backend_dev",
            "reason": "only QA or the owning role can mark an item verified",
        }
    ]
    validate_decision_checklist_payload(
        task,
        role_id="neko_supervisor",
        mission_stage_id="backend_contract_smoke",
        payload=sanitized,
        config=_config(True),
    )


def test_checklist_sanitizer_drops_invented_active_item_without_invalidating_decision():
    task = _task("task_sanitize_active_item")
    RoleChecklistStore().open_or_create(task=task, role_id="qa", mission_stage_id="qa_release")
    payload = {
        "verdict": "blocked",
        "active_checklist_item_id": "invented_item",
        "findings": [{"severity": "blocking", "summary": "delivery packet missing findings"}],
    }

    sanitized, ignored = sanitize_decision_checklist_payload(
        task,
        role_id="qa",
        mission_stage_id="qa_release",
        payload=payload,
        config=_config(True),
    )

    assert "active_checklist_item_id" not in sanitized
    assert ignored == [
        {
            "item_id": "invented_item",
            "status": "active",
            "owner_role": "",
            "reason": "active_checklist_item_id is not visible for this role/stage",
        }
    ]
    validate_decision_checklist_payload(
        task,
        role_id="qa",
        mission_stage_id="qa_release",
        payload=sanitized,
        config=_config(True),
    )


def test_checklist_sanitizer_drops_invented_update_item_without_invalidating_decision():
    task = _task("task_sanitize_invented_update_item")
    RoleChecklistStore().open_or_create(task=task, role_id="qa", mission_stage_id="qa_release")
    payload = {
        "verdict": "approved",
        "checklist_updates": [
            {
                "item_id": "invented_item",
                "status": "verified",
                "summary": "Model referenced a checklist item outside its visible HUD.",
            }
        ],
    }

    sanitized, ignored = sanitize_decision_checklist_payload(
        task,
        role_id="qa",
        mission_stage_id="qa_release",
        payload=payload,
        config=_config(True),
    )

    assert "checklist_updates" not in sanitized
    assert ignored == [
        {
            "item_id": "invented_item",
            "status": "verified",
            "owner_role": "",
            "reason": "item_id is not visible for this role/stage",
        }
    ]
    validate_decision_checklist_payload(
        task,
        role_id="qa",
        mission_stage_id="qa_release",
        payload=sanitized,
        config=_config(True),
    )


def test_checklist_sanitizer_drops_invalid_status_without_invalidating_decision():
    task = _task("task_sanitize_invalid_status")
    RoleChecklistStore().open_or_create(task=task, role_id="neko_supervisor", mission_stage_id="backend_contract_smoke")
    payload = {
        "objective": "Scope the mission.",
        "acceptance_criteria": ["Backend Dev receives the investigation stage."],
        "checklist_updates": [
            {
                "item_id": "typed_plan",
                "status": "done",
                "summary": "Model used a non-HUD checklist status.",
            }
        ],
    }

    sanitized, ignored = sanitize_decision_checklist_payload(
        task,
        role_id="neko_supervisor",
        mission_stage_id="backend_contract_smoke",
        payload=payload,
        config=_config(True),
    )

    assert "checklist_updates" not in sanitized
    assert sanitized["objective"] == "Scope the mission."
    assert ignored == [
        {
            "item_id": "typed_plan",
            "status": "done",
            "owner_role": "neko_supervisor",
            "reason": "checklist status is not one of the HUD valid_statuses",
        }
    ]
    validate_decision_checklist_payload(
        task,
        role_id="neko_supervisor",
        mission_stage_id="backend_contract_smoke",
        payload=sanitized,
        config=_config(True),
    )


def test_checklist_template_uses_requested_typed_stage_not_current_stage():
    task = _task("task_requested_stage")
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="launcher_impl",
        stages=[
            MissionPlanStage(
                id="launcher_impl",
                title="Launcher implementation",
                objective="Patch launcher UI.",
                owner="dev",
                repo="EterniaLauncher",
                kind="implementation",
                status=StageStatus.IMPLEMENTING,
            ),
            MissionPlanStage(
                id="qa_release",
                title="QA release",
                objective="Verify release evidence.",
                owner="qa",
                repo="EterniaLauncher",
                kind="qa_verdict",
                depends_on=["launcher_impl"],
            ),
        ],
    )

    checklist = RoleChecklistStore().open_or_create(task=task, role_id="qa", mission_stage_id="qa_release")

    assert checklist.mission_stage_id == "qa_release"
    assert checklist.role_id == "qa"
    assert checklist.operational_scope == "role_local"
    assert checklist.can_promote_global_stage is False
    assert [item.item_id for item in checklist.items][:2] == ["dependency_readiness", "command_proof"]


def test_neko_checklist_is_the_only_stage_promotion_surface():
    task = _task("task_neko_promotes")
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="neko_scope",
        stages=[
            MissionPlanStage(
                id="neko_scope",
                title="Neko scope",
                objective="Scope operational mission stages.",
                owner="neko_supervisor",
                repo="none",
                kind="planning",
            )
        ],
    )

    checklist = RoleChecklistStore().open_or_create(task=task, role_id="neko_supervisor", mission_stage_id="neko_scope")

    assert checklist.operational_scope == "mission_stage"
    assert checklist.can_promote_global_stage is True
    assert "Neko may create or promote global mission stages" in checklist.promotion_rule


def test_apply_checklist_updates_preserves_self_approval_metadata():
    task = _task("task_apply")
    checklist = apply_decision_checklist_updates(
        task,
        role_id="dev",
        mission_stage_id="stage_1",
        run_id="run_1",
        config=_config(True),
        payload={
            "checklist_updates": [
                {
                    "item_id": "inspect",
                    "status": "self_approved",
                    "summary": "Scoped context inspected.",
                    "evidence_refs": ["proof_1"],
                }
            ]
        },
    )

    item = next(item for item in checklist.items if item.item_id == "inspect")
    assert checklist.revision == 1
    assert item.status == "self_approved"
    assert item.self_approved_by_run_id == "run_1"
    assert item.evidence_refs == ["proof_1"]


def test_apply_checklist_updates_ignores_non_object_updates_defensively():
    task = _task("task_apply_non_object")

    checklist = apply_decision_checklist_updates(
        task,
        role_id="dev",
        mission_stage_id="stage_1",
        run_id="run_1",
        config=_config(True),
        payload={"checklist_updates": ["inspect"]},
    )

    assert checklist is not None
    assert checklist.revision == 0
    assert all(item.status == "pending" for item in checklist.items)


def test_role_envelope_resumes_same_role_stage_and_records_no_progress():
    task = _task("task_envelope")
    store = RoleEnvelopeStore()

    first = store.open_or_resume(task=task, role_id="dev", mission_stage_id="stage_1", worker_session_id="worker_1", run_id="run_1")
    second = store.open_or_resume(task=task, role_id="dev", mission_stage_id="stage_1", worker_session_id="worker_1", run_id="run_2")
    updated = store.record_progress(second, run_id="run_2", decision_type="deliver", checklist_revision=0)
    repeated = store.record_progress(updated, run_id="run_3", decision_type="deliver", checklist_revision=0)

    assert first.envelope_id == second.envelope_id
    assert second.continuation_count == 1
    assert repeated.no_progress_count == 1


def test_role_envelope_progress_hash_includes_payload_digest():
    task = _task("task_envelope_payload")
    store = RoleEnvelopeStore()

    envelope = store.open_or_resume(task=task, role_id="dev", mission_stage_id="stage_1", worker_session_id="worker_1", run_id="run_1")
    first = store.record_progress(envelope, run_id="run_1", decision_type="request_test_run", payload={"commands": ["pytest tests/a.py"]})
    repeated = store.record_progress(first, run_id="run_2", decision_type="request_test_run", payload={"commands": ["pytest tests/a.py"]})
    repeated_count = repeated.no_progress_count
    changed = store.record_progress(repeated, run_id="run_3", decision_type="request_test_run", payload={"commands": ["pytest tests/b.py"]})

    assert repeated_count == 1
    assert changed.no_progress_count == 0


def test_proof_batch_supersedes_previous_recipe_batch():
    store = ProofBatchStore()

    first = store.record_batch(
        task_id="task_batch",
        mission_stage_id="stage_1",
        role_envelope_id="envelope_1",
        recipe_id="recipe_a",
        proof_ids=["proof_old"],
        status="passed",
    )
    second = store.record_batch(
        task_id="task_batch",
        mission_stage_id="stage_1",
        role_envelope_id="envelope_1",
        recipe_id="recipe_a",
        proof_ids=["proof_new"],
        status="passed",
    )

    assert store.get("task_batch", first.proof_batch_id).status == "superseded"
    assert second.supersedes == [first.proof_batch_id]


def test_tick_records_envelope_checklist_and_proof_batch_when_enabled():
    tasks = TaskStore()
    proofs = ProofStore()
    task = _task("task_tick")
    tasks.create(task)
    runtime = ChecklistProofRuntime()

    result = TickEngine(
        task_store=tasks,
        proof_store=proofs,
        persona_runtime=runtime,
        proof_runner=PassingProofRunner(proofs),
        config=_config(True),
    ).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    envelopes = RoleEnvelopeStore().list_for_task(task.id)
    checklists = RoleChecklistStore().list_for_task(task.id)
    batches = ProofBatchStore().list_for_task(task.id)
    assert len(envelopes) == 1
    assert len(checklists) == 1
    assert len(batches) == 1
    assert envelopes[0].proof_batch_id == batches[0].proof_batch_id
    assert checklists[0].revision == 1
    assert batches[0].proof_ids == [f"proof_{task.id}_implement"]


def test_tick_does_not_record_stage52_state_when_disabled():
    tasks = TaskStore()
    proofs = ProofStore()
    task = _task("task_disabled")
    tasks.create(task)

    result = TickEngine(
        task_store=tasks,
        proof_store=proofs,
        persona_runtime=ChecklistProofRuntime(),
        proof_runner=PassingProofRunner(proofs),
        config=_config(False),
    ).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    assert RoleEnvelopeStore().list_for_task(task.id) == []
    assert RoleChecklistStore().list_for_task(task.id) == []
    assert ProofBatchStore().list_for_task(task.id) == []


def test_snapshot_surfaces_stage52_state_for_mission_control():
    tasks = TaskStore()
    proofs = ProofStore()
    task = _task("task_snapshot")
    tasks.create(task)
    TickEngine(
        task_store=tasks,
        proof_store=proofs,
        persona_runtime=ChecklistProofRuntime(),
        proof_runner=PassingProofRunner(proofs),
        config=_config(True),
    ).tick_once(task_id=task.id)

    snap = build_snapshot(task_store=tasks, proof_store=proofs)
    item = list(snap["goals"].values())[0]

    assert snap["role_envelopes"]
    assert snap["role_checklists"]
    assert snap["proof_batches"]
    assert item["role_envelopes"][0]["role_id"] == "dev"
    assert item["role_checklists"][0]["items"][0]["item_id"] == "inspect"
    assert item["proof_batches"][0]["proof_ids"] == [f"proof_{task.id}_implement"]


def test_context_hud_injects_task_list_after_checklist_exists():
    tasks = TaskStore()
    runs = RunStore()
    task = _task("task_context")
    tasks.create(task)
    run = runs.open_run("dev", task.id, stage_id="stage_1")
    RoleChecklistStore().open_or_create(task=task, role_id="dev", mission_stage_id="stage_1", run_id=run.id)

    ctx = build_context(task, run, config=_config(True))

    assert ctx.mission_hud["stage_task_list"]["current_item_id"] == "inspect"
    assert ctx.mission_hud["stage_task_list"]["stage_id"] == "stage_1"
    assert "valid_item_ids" in ctx.mission_hud["stage_task_list"]


def test_archive_preserves_role_state_evidence(isolate_agent_runtime_root):
    # S7-B: the frame carries an archived_tasks POINTER stub, not the rows. The
    # preserved role-state evidence is verified two ways: the on-disk archive
    # artifacts (below) + the archived-task projection the pointer references (the
    # same rows `harness task history` serves) — not the in-frame projection.
    tasks = TaskStore()
    task = _task("task_archive", state=TaskState.DONE)
    tasks.create(task)
    envelope = RoleEnvelopeStore().open_or_resume(task=task, role_id="dev", mission_stage_id="stage_1", run_id="run_1")
    ProofBatchStore().record_batch(
        task_id=task.id,
        mission_stage_id="stage_1",
        role_envelope_id=envelope.envelope_id,
        recipe_id="recipe_a",
        proof_ids=["proof_1"],
        status="passed",
    )

    result = tasks.archive(task.id, actor="test", reason="stage52 archive")

    archived = result["archived_tasks"][0]
    assert archived["role_state_archived"] is True
    assert archived["role_envelope_ids"] == [envelope.envelope_id]
    archive_dir = isolate_agent_runtime_root / "deleted_archive" / result["archive_batch"]
    assert (archive_dir / "role_envelopes" / task.id / f"{envelope.envelope_id}.json").exists()
    assert (archive_dir / "role_checklists" / task.id).exists()
    assert (archive_dir / "proof_batches" / task.id).exists()

    from agent_runtime.snapshot import _archived_task_summaries

    assert task.id in build_snapshot(task_store=tasks)["archived_tasks"]["recent_ids"]
    archived_task = next(item for item in _archived_task_summaries() if item["task_id"] == task.id)
    assert archived_task["role_envelopes"][0]["role_id"] == "dev"
    assert archived_task["role_checklists"][0]["items"][0]["item_id"] == "inspect"
    assert archived_task["proof_batches"][0]["proof_ids"] == ["proof_1"]
    dev_stream = next(stream for stream in archived_task["role_streams"] if stream["persona_id"] == "dev")
    assert dev_stream["role_envelopes"][0]["envelope_id"] == envelope.envelope_id
    assert dev_stream["role_checklists"][0]["role_id"] == "dev"
    assert dev_stream["proof_batches"][0]["status"] == "passed"
