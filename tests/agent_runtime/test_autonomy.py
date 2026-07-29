from __future__ import annotations

import json

from hermes_time import now

from agent_runtime.autonomy import context_artifact_dir, record_autonomy_packet
from agent_runtime.context_builder import build_context, render_context
from agent_runtime.events import EventLog
from agent_runtime.models import MissionIntent, MissionPlan, MissionPlanStage, Task, TaskStage
from agent_runtime.personas import default_personas
from agent_runtime.states import StageStatus, TaskState
from agent_runtime.store import RunStore

# ``default_personas()`` carries only the Harness-owned baseline since
# ``9ad9c8017`` ("provision bundled agent personas") — that list is the bundled
# INSTALL contract, not a deployment's skill roster — and since ``07e662a9c``
# ("make skill context backend authoritative") a config ``skills:`` block
# EXTENDS that baseline rather than replacing it, with ``skills_remove:`` as the
# only subtraction.
#
# ``autonomy._skill_selection`` can only ever choose from what a persona GRANTS,
# so driving the selection policy with the bare baseline tests the baseline
# rather than the policy: every candidate the priority list names is filtered
# out before it can be ranked. These are the grants the deployed personas
# actually carry (`hermes harness skills inventory`), restricted to the ids the
# priority policy can propose for that role.
#
# These are the EXTRA grants only — the additions a deployment's ``skills:``
# block layers on top of the seeded baseline, restricted to the ids the priority
# policy can propose for that role. ``configured_persona`` re-derives the full
# roster as ``baseline + extras`` so this fixture keeps matching the real extend
# semantics even when a bundled baseline gains or loses a skill, instead of
# silently replacing it with a frozen list.
_CONFIGURED_EXTRA_SKILLS = {
    "dev": [
        "eternia-launcher-workflow",
        "frontend-backend-contract-handoff",
        "flutter-ui-development",
        "systematic-debugging",
        "test-driven-development",
    ],
    "backend_dev": [
        "eternia-backend-tests",
        "frontend-backend-contract-handoff",
        "systematic-debugging",
        "test-driven-development",
    ],
}


def configured_persona(persona_id: str):
    """A bundled persona as a deployment actually configures it.

    EXTENDS the seeded baseline (``default_personas()`` grants) with the
    deployment's extra grants — the same ``baseline + skills:`` composition
    ``07e662a9c`` made authoritative — rather than replacing the roster.
    """

    import dataclasses

    persona = next(item for item in default_personas() if item.id == persona_id)
    skills = list(dict.fromkeys([*persona.skills, *_CONFIGURED_EXTRA_SKILLS[persona_id]]))
    return dataclasses.replace(persona, skills=skills)


def make_task() -> Task:
    ts = now()
    return Task(
        id="task_auto",
        title="Ship Launcher Mission Control polish from X:/Private/secret/repo",
        description="Need focused Launcher proof without leaking SECRET_KEY=super-secret.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["X:/Unreal Engine/Engine/Launcher/EterniaLauncher"],
        requires_visual_proof=True,
    )


def test_record_autonomy_packet_writes_context_receipts_and_prompt_contract():
    task = make_task()
    failed_proof_id = "test_task_burn_d2dfc2a8_backend_no_op_route_run_a0dc825e7654_0_54c12760"
    task.harness_self_heal = {"stages": {"stage_1": {"last_failed_proof_ids": [failed_proof_id]}}}
    run_store = RunStore()
    run = run_store.open_run("dev", task.id, stage_id="stage_1")
    persona = configured_persona("dev")
    ctx = build_context(
        task,
        run,
        recent_events=[
            {"ts": now().isoformat(), "type": "run.opened", "run_id": run.id, "persona_id": "dev"},
            {"ts": now().isoformat(), "type": "proof.attached", "run_id": run.id, "persona_id": "harness"},
        ],
        proof_ids=["proof_1"],
    )

    packet = record_autonomy_packet(persona, ctx, event_log=EventLog(), run_store=run_store)

    root = context_artifact_dir(task.id, persona.id)
    assert (root / "autonomy_packets.jsonl").exists()
    assert (root / "absorbed_logs.jsonl").exists()
    assert (root / "compression_receipts.jsonl").exists()
    assert (root / "context_summary.md").exists()
    packet_record = json.loads((root / "autonomy_packets.jsonl").read_text(encoding="utf-8").splitlines()[0])
    receipt_record = json.loads((root / "compression_receipts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert packet_record["autonomy_packet_id"] == packet["autonomy_packet_id"]
    assert receipt_record["context_receipt_id"] == packet["context_receipt_id"]
    assert receipt_record["source_event_count"] == 2
    assert receipt_record["proof_count"] == 1
    assert packet_record["inspection_budget"]["read_search_limit"] == 6
    assert packet_record["failed_proof_ids"] == [failed_proof_id]
    assert "decision_contract_mode" not in packet_record["mission_hud"]
    assert "decision_menu" not in packet_record["mission_hud"]
    assert "next_required_move" not in packet_record["mission_hud"]
    assert packet_record["mission_hud"]["agent_hud"]["recommended_action"]["forbid_unknown_payload_keys"] is True
    assert packet["mission_hud"]["agent_hud"]["context_options"][0]["shape_id"] == "common.request_file_reads"
    assert packet["failed_proof_ids"] == [failed_proof_id]
    assert [item["id"] for item in packet_record["selected_skills"]][:2] == [
        "harness-dev-delivery",
        "eternia-launcher-workflow",
    ]
    rendered = render_context(ctx)
    assert "Autonomy / Tool Economy Contract" in rendered
    assert packet["autonomy_packet_id"] in rendered
    combined = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*"))
    assert "super-secret" not in combined
    assert "X:/Private" not in combined
    progress = run_store.get(run.id).progress
    assert progress["autonomy_packet_id"] == packet["autonomy_packet_id"]
    assert progress["context_receipt_id"] == packet["context_receipt_id"]
    assert progress["last_failed_proof_ids"] == [failed_proof_id]
    assert progress["skill_load_limit"] == 3


def test_launcher_contract_smoke_selects_launcher_analyze_skill_without_backend_fanout():
    ts = now()
    task = Task(
        id="task_launcher_analyze",
        title="Stage 47 launcher_contract_smoke",
        description="Consume backend packet proof and choose the correct flutter analyze gate without lib/main.dart fanout.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["X:/Unreal Engine/Engine/Launcher/EterniaLauncher"],
    )
    run_store = RunStore()
    run = run_store.open_run("dev", task.id, stage_id="launcher_contract_smoke")
    persona = configured_persona("dev")
    ctx = build_context(task, run, recent_events=[], proof_ids=["proof_backend"])

    packet = record_autonomy_packet(persona, ctx, event_log=EventLog(), run_store=run_store)

    selected = [item["id"] for item in packet["selected_skills"]]
    packet_path = context_artifact_dir(task.id, persona.id) / "autonomy_packets.jsonl"
    packet_record = json.loads(packet_path.read_text(encoding="utf-8").splitlines()[0])
    rejected = [item["id"] for item in packet_record["rejected_skills"]]
    assert selected == [
        "harness-dev-delivery",
        "launcher-analyze-proof",
        "frontend-backend-contract-handoff",
    ]
    assert "eternia-backend-tests" not in selected
    assert "eternia-backend-tests" not in rejected
    assert run_store.get(run.id).progress["selected_skill_count"] == 3


def test_product_edit_stage_does_not_advertise_no_edit_smoke_recipes():
    ts = now()
    task = Task(
        id="task_dm_bubbles",
        title="Mission Control DM bubble terminal rows",
        description="Upgrade Launcher Mission Control event rows into compact DM bubbles.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["EterniaLauncher"],
        current_stage_id="mc_terminal_dm_bubble_rows",
        stages=[
            TaskStage(
                id="mc_terminal_dm_bubble_rows",
                title="Implement compact Mission Control terminal DM bubble event rows",
                objective="Replace heavy block cards with compact expandable DM bubble rows.",
                status=StageStatus.IMPLEMENTING,
                affected_paths=["lib/features/mission_control/", "test/features/mission_control/"],
                acceptance_criteria=["Widget tests cover bubble row rendering and expansion behavior."],
                test_plan=["flutter test test/features/mission_control"],
            )
        ],
    )
    run_store = RunStore()
    run = run_store.open_run("dev", task.id, stage_id="mc_terminal_dm_bubble_rows")
    persona = next(item for item in default_personas() if item.id == "dev")
    ctx = build_context(task, run, recent_events=[], proof_ids=[])

    packet = record_autonomy_packet(persona, ctx, event_log=EventLog(), run_store=run_store)

    recipe_ids = {item["recipe_id"] for item in packet["available_proof_recipes"]}
    assert "launcher_contract_smoke" not in recipe_ids
    assert "archive_button_cli_contract" not in recipe_ids
    assert "harness_runtime_status_snapshot" not in recipe_ids
    assert packet["inspection_budget"]["read_search_limit"] == 24


def test_read_search_loop_recovery_packet_forces_patch_test_or_block():
    ts = now()
    task = Task(
        id="task_loop_recovery",
        title="Launcher post media thumbnails and portrait videos 3x",
        description="Make post thumbnails and portrait videos 3x larger.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["EterniaLauncher"],
        current_stage_id="launcher_implementation",
        stages=[
            TaskStage(
                id="launcher_implementation",
                title="Launcher Implementation",
                objective="Patch post media sizing.",
                status=StageStatus.IMPLEMENTING,
                affected_paths=["lib/features/posts/", "test/features/posts/"],
                test_plan=["flutter test test/features/posts"],
            )
        ],
    )
    run_store = RunStore()
    run = run_store.open_run("dev", task.id, stage_id="launcher_implementation")
    run.progress = {
        "loop_warning": "read_search_without_patch_threshold",
        "read_search_count": 6,
        "read_search_limit": 6,
        "patch_count": 0,
    }
    run_store.update(run)
    persona = next(item for item in default_personas() if item.id == "dev")
    ctx = build_context(task, run, recent_events=[], proof_ids=[])

    packet = record_autonomy_packet(persona, ctx, event_log=EventLog(), run_store=run_store)

    assert "Read/search budget was already exhausted without a patch" in packet["self_heal_plan"]
    assert "Make the smallest safe patch" in packet["self_heal_plan"]
    packet_path = context_artifact_dir(task.id, persona.id) / "autonomy_packets.jsonl"
    packet_record = json.loads(packet_path.read_text(encoding="utf-8").splitlines()[0])
    assert packet_record["self_heal_plan"] == packet["self_heal_plan"]


def test_no_edit_context_stage_uses_single_fast_dev_skill_budget():
    ts = now()
    task = Task(
        id="task_backend_context_fast",
        title="Backend Dev live efficiency smoke",
        description="No-edit Backend Dev efficiency smoke. Do not inspect code or edit files.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["EterniaBackend"],
        current_stage_id="diagnostic_backend_dev",
        mission_plan=MissionPlan(
            mission_intent=MissionIntent(
                title="Backend Dev live efficiency smoke",
                objective="No-edit Backend Dev efficiency smoke.",
            ),
            current_stage_id="diagnostic_backend_dev",
            stages=[
                MissionPlanStage(
                    id="diagnostic_backend_dev",
                    title="backend_dev Diagnostic",
                    objective="No-edit Backend Dev efficiency smoke. Do not inspect code or edit files.",
                    owner="backend_dev",
                    repo="EterniaBackend",
                    kind="context",
                    requires_product_edit=False,
                ),
            ],
        ),
    )
    run_store = RunStore()
    run = run_store.open_run("backend_dev", task.id, stage_id="diagnostic_backend_dev")
    persona = configured_persona("backend_dev")
    ctx = build_context(task, run, recent_events=[], proof_ids=[])

    packet = record_autonomy_packet(persona, ctx, event_log=EventLog(), run_store=run_store)

    assert packet["inspection_budget"]["read_search_limit"] == 2
    assert packet["inspection_budget"]["skill_load_limit"] == 1
    assert [item["id"] for item in packet["selected_skills"]] == ["harness-dev-delivery"]
    packet_path = context_artifact_dir(task.id, persona.id) / "autonomy_packets.jsonl"
    packet_record = json.loads(packet_path.read_text(encoding="utf-8").splitlines()[0])
    rejected = {item["id"] for item in packet_record["rejected_skills"]}
    assert "eternia-backend-tests" in rejected
    assert "frontend-backend-contract-handoff" in rejected
    progress = run_store.get(run.id).progress
    assert progress["selected_skill_count"] == 1
    assert progress["skill_load_limit"] == 1
