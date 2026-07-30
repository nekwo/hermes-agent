from dataclasses import dataclass, field

from hermes_time import now

from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.stage_intent import no_product_edit_recipe_conflicts_with_stage, no_product_edit_recipe_for_stage, stage_requires_product_edit
from agent_runtime.states import TaskState


@dataclass
class StageFixture:
    id: str
    title: str
    objective: str
    # Persisted stage rows carry the status as a plain string; the enum that
    # used to type this field was the removed stage graph's (S23).
    status: str
    owner: str = ""
    repo: str = ""
    kind: str = ""
    proof_recipe_id: str | None = None
    requires_product_edit: bool | None = None
    affected_paths: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)
    audit_notes: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)


def _task() -> Task:
    ts = now()
    return Task(
        id="task_stage_intent",
        title="Stage 50 live cross-stack no-edit routing certification",
        description="No product edits. Certify backend-first then Launcher no-edit proof routing.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        affected_repos=["EterniaBackend"],
        risk_flags=["no_product_edits"],
    )


def test_no_edit_recipe_stage_is_not_classified_as_product_edit():
    task = _task()
    stage = StageFixture(
        id="backend_contract_smoke",
        title="Backend Contract Smoke",
        objective="Request the existing backend_contract_smoke no_product_edit proof recipe without modifying product repositories.",
        status="implementing",
        acceptance_criteria=["No product repository edits are made."],
        test_plan=[
            "request_test_run with stage_id=backend_contract_smoke and recipe_id=backend_contract_smoke",
        ],
    )

    assert stage_requires_product_edit(task, stage) is False
    assert no_product_edit_recipe_conflicts_with_stage(task, stage, "backend_contract_smoke") is False


def test_without_editing_product_code_is_not_classified_as_product_edit():
    task = _task()
    task.description = "Produce a staged hardening plan. Do not edit product code."
    stage = StageFixture(
        id="backend_investigation",
        title="Backend Investigation",
        objective="Inspect backend moderation paths and produce a staged AAA hardening plan without editing product code.",
        status="implementing",
    )

    assert stage_requires_product_edit(task, stage) is False


def test_no_edit_recipe_still_conflicts_with_real_product_edit_stage():
    task = _task()
    stage = StageFixture(
        id="backend_api_change",
        title="Backend API Change",
        objective="Modify src/api.py to add a new backend endpoint.",
        status="implementing",
        affected_paths=["src/api.py"],
        acceptance_criteria=["New endpoint is implemented."],
        test_plan=["pytest tests/test_api.py"],
    )

    assert stage_requires_product_edit(task, stage) is True
    assert no_product_edit_recipe_conflicts_with_stage(task, stage, "backend_contract_smoke") is True


def test_generic_no_edit_recipe_stage_ignores_global_ui_fix_language():
    task = _task()
    task.title = "Fix Mission Control role terminal UI after backend stream smoke"
    task.description = "Make the Launcher UI show Neko, Backend Dev, Launcher Dev, and QA live terminal events."
    stage = StageFixture(
        id="eterniabackend_fresh_scope",
        title="Backend stream seed proof",
        objective="Seed the live mission with Backend Dev redaction-safe events/proof by requesting only the existing no-product-edit backend_contract_smoke proof path.",
        status="implementing",
        acceptance_criteria=["No backend product files are inspected or changed."],
        test_plan=[
            "Run/request the existing backend_contract_smoke no-product-edit proof recipe.",
        ],
    )

    assert no_product_edit_recipe_for_stage(stage) == "backend_contract_smoke"
    assert stage_requires_product_edit(task, stage) is False
    assert no_product_edit_recipe_conflicts_with_stage(task, stage, "backend_contract_smoke") is False


def test_recipe_inference_refuses_ambiguous_evidence_fields_without_identity_match():
    stage = StageFixture(
        id="contract_join_stage",
        title="Contract join stage",
        objective="Consume backend_contract_smoke and certify launcher_contract_smoke.",
        status="implementing",
        acceptance_criteria=["Both proof recipes are referenced for the join."],
    )

    assert no_product_edit_recipe_for_stage(stage) is None


def test_generic_recipe_stage_stays_no_edit_after_backend_correction_notes():
    task = _task()
    task.title = "Fix Mission Control all-role live terminals"
    task.description = "Seed and prove Backend Dev live terminal/event rows only by running the existing no-product-edit backend_contract_smoke proof recipe, without inspecting, editing, or patching backend product files."
    stage = StageFixture(
        id="eterniabackend_fresh_scope",
        title="Backend Dev Fresh Scope",
        objective="Seed and prove Backend Dev live terminal/event rows only by running the existing no-product-edit backend_contract_smoke proof recipe, without inspecting, editing, or patching backend product files.",
        status="implementing",
        affected_paths=["EterniaBackend"],
        acceptance_criteria=[
            "Backend Dev requests/runs only the existing backend_contract_smoke proof recipe.",
            "Backend Dev does not inspect, edit, or patch EterniaBackend product files during this stage.",
            "Backend Dev returns the passed proof ID before Launcher Dev starts UI/bridge repair.",
        ],
        test_plan=[
            "Harness-owned request_test_run for stage eterniabackend_fresh_scope using recipe_id backend_contract_smoke once exposed.",
        ],
        audit_notes=[
            "backend_dev: Current HUD required_next_decision=propose_patch conflicts with the task description and acceptance criteria requiring only backend_contract_smoke proof seeding.",
            "backend_dev: No backend product files were inspected, edited, or patched during this tick.",
        ],
        corrections=[
            "backend_dev: Reclassify this stage as a no-product-edit proof-seeding stage instead of a product-edit implementation stage.",
            "backend_dev: Do not require or accept changed_files, patch content, or product-edit delivery for this stage.",
        ],
    )

    assert no_product_edit_recipe_for_stage(stage) == "backend_contract_smoke"
    assert stage_requires_product_edit(task, stage) is False
    assert no_product_edit_recipe_conflicts_with_stage(task, stage, "backend_contract_smoke") is False


def test_typed_proof_only_stage_overrides_legacy_implementation_id_marker():
    task = _task()
    task.affected_repos = ["EterniaBackend", "EterniaLauncher"]
    stage = StageFixture(
        id="implement",
        title="Launcher No-Edit Contract Smoke",
        objective="Collect the Harness-owned launcher_contract_smoke proof without editing Launcher product files.",
        owner="dev",
        repo="EterniaLauncher",
        kind="proof_only",
        status="implementing",
        proof_recipe_id="launcher_contract_smoke",
        requires_product_edit=False,
        test_plan=["proof_recipe:launcher_contract_smoke"],
    )

    assert stage_requires_product_edit(task, stage) is False
    assert no_product_edit_recipe_conflicts_with_stage(task, stage, "launcher_contract_smoke") is False
