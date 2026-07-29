from hermes_time import now

from agent_runtime.final_gate import final_gate_commands
from agent_runtime.models import MissionIntent, MissionPlan, Task, TaskStage
from agent_runtime.states import StageStatus, TaskState


def test_final_gate_preserves_multiline_heredoc_commands():
    stamp = now()
    task = Task(
        id="task_gate",
        title="Backend proof",
        description="Run focused backend proof.",
        state=TaskState.RUNNING,
        created_at=stamp,
        updated_at=stamp,
        requested_by="test",
        affected_repos=["EterniaBackend"],
    )
    stage = TaskStage(
        id="backend_implementation",
        title="Backend Implementation",
        objective="Patch backend.",
        status=StageStatus.IMPLEMENTING,
        test_plan=[
            "source .EterniaBackendVirtualEnv/Scripts/activate && python - <<'PY'\n"
            "import django\n"
            "from django.conf import settings\n"
            "print('django_import_ok', django.get_version(), settings.configured)\n"
            "PY\n"
            "scripts/test.sh media.tests.FinalizeUploadTests"
        ],
    )

    commands = final_gate_commands(task, stage)

    assert commands == [stage.test_plan[0]]
    assert "PY\nscripts/test.sh" in commands[0]



def _goal_task(description, *, repos, notes=None):
    stamp = now()
    return Task(
        id="task_goal_cmd",
        title="Investigate liveness",
        description=description,
        state=TaskState.RUNNING,
        created_at=stamp,
        updated_at=stamp,
        requested_by="test",
        affected_repos=list(repos),
        operator_notes=list(notes or []),
    )


def _edit_stage(stage_id="implement", title="Launcher Implementation", objective="Patch the launcher shop widget file lib/features/shop/widget.dart.", test_plan=None):
    return TaskStage(
        id=stage_id,
        title=title,
        objective=objective,
        status=StageStatus.IMPLEMENTING,
        test_plan=list(test_plan or []),
    )


def test_goal_named_focused_command_outranks_generic_repo_default():
    from agent_runtime.final_gate import goal_named_proof_commands

    task = _goal_task(
        "Update the shop widget in lib/features/shop/widget.dart. Proof: `flutter test test/features/shop/widget_test.dart`.",
        repos=["EterniaLauncher"],
    )
    stage = _edit_stage()

    assert goal_named_proof_commands(task) == ["flutter test test/features/shop/widget_test.dart"]
    commands = final_gate_commands(task, stage)
    assert commands == ["flutter test test/features/shop/widget_test.dart"]


def test_stage_test_plan_still_outranks_goal_named_command():
    task = _goal_task(
        "Update the shop widget in lib/features/shop/widget.dart. Proof: `flutter test test/features/shop/widget_test.dart`.",
        repos=["EterniaLauncher"],
    )
    stage = _edit_stage(test_plan=["flutter analyze lib/features/shop"])

    commands = final_gate_commands(task, stage)
    assert commands == ["flutter analyze lib/features/shop"]


def test_goal_named_exact_proof_outranks_stage_test_plan():
    from agent_runtime.final_gate import goal_named_proof_commands

    task = _goal_task(
        "Write docs. Exact proof: `echo e2e-trust-probe`; no Flutter tests.",
        repos=["EterniaLauncher"],
    )
    stage = _edit_stage(test_plan=["flutter analyze lib/features/mission_control", "flutter test test/features/mission_control"])

    assert goal_named_proof_commands(task) == ["echo e2e-trust-probe"]
    commands = final_gate_commands(task, stage)
    assert commands == ["echo e2e-trust-probe"]


def test_goal_named_exact_proof_parses_prose_without_backticks():
    from agent_runtime.final_gate import goal_named_proof_commands

    task = _goal_task(
        "Write docs/scratch/e2e_trust_probe.md. The final Harness-owned command proof must run exactly: echo e2e-trust-probe. Do not run Flutter analyze/tests.",
        repos=["EterniaLauncher"],
    )
    stage = _edit_stage(test_plan=["flutter analyze lib/features/mission_control", "flutter test test/features/mission_control"])

    assert goal_named_proof_commands(task) == ["echo e2e-trust-probe"]
    commands = final_gate_commands(task, stage)
    assert commands == ["echo e2e-trust-probe"]


def test_goal_named_exact_proof_reads_locked_mission_intent():
    from agent_runtime.final_gate import goal_named_proof_commands

    task = _goal_task(
        "Create the concise Launcher trust-probe document artifact.",
        repos=["EterniaLauncher"],
    )
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(
            title="Launcher exact proof QA trust probe",
            objective=(
                "Write docs/scratch/e2e_trust_probe_qa.md. "
                "The final Harness-owned command proof must run exactly: echo e2e-trust-probe-qa. "
                "Do not run Flutter analyze/tests."
            ),
        )
    )
    stage = _edit_stage(test_plan=["flutter analyze lib/features/mission_control", "flutter test test/features/mission_control"])

    assert goal_named_proof_commands(task) == ["echo e2e-trust-probe-qa"]
    commands = final_gate_commands(task, stage)
    assert commands == ["echo e2e-trust-probe-qa"]




def test_handoff_packet_exact_proof_outranks_stage_test_plan():
    task = _goal_task(
        "Write docs/scratch/e2e_trust_probe.md. Exact proof command: echo e2e-trust-probe. Do not run Flutter analyze/tests.",
        repos=["EterniaLauncher"],
    )
    stage = _edit_stage(test_plan=["flutter analyze lib/features/mission_control", "flutter test test/features/mission_control"])
    handoff_packet = {
        "body": {
            "target_repo": "EterniaLauncher",
            "proof_gate": {
                "required": True,
                "commands": ["echo e2e-trust-probe"],
                "forbidden_commands": ["flutter analyze", "flutter test"],
            },
        }
    }

    commands = final_gate_commands(task, stage, handoff_packet=handoff_packet)
    assert commands == ["echo e2e-trust-probe"]


def test_handoff_packet_forbidden_command_suppresses_repo_default():
    task = _goal_task(
        "Patch the Launcher widget; the handoff forbids generic Flutter proof.",
        repos=["EterniaLauncher"],
    )
    stage = _edit_stage(test_plan=[])
    handoff_packet = {
        "body": {
            "target_repo": "EterniaLauncher",
            "proof_gate": {
                "required": True,
                "required_proof_types": ["test_run"],
                "forbidden_commands": ["flutter analyze"],
            },
        }
    }

    commands = final_gate_commands(task, stage, handoff_packet=handoff_packet)
    assert commands == []


def test_goal_named_command_for_other_repo_is_excluded_from_stage_gate():
    task = _goal_task(
        "Polish the shop. Proof: `python -m pytest tests/agent_runtime/test_liveness.py -q` in hermes-agent.",
        repos=["EterniaLauncher"],
    )
    stage = _edit_stage()

    commands = final_gate_commands(task, stage)
    assert commands == ["flutter analyze"]


def test_goal_named_command_extraction_rejects_shell_plumbing_and_secrets():
    from agent_runtime.final_gate import goal_named_proof_commands

    task = _goal_task(
        "Run `python -m pytest tests/x.py -q && rm -rf /` or `python print_password_token.py`.",
        repos=["hermes-agent"],
    )
    assert goal_named_proof_commands(task) == []








def test_explicit_graph_blueprint_stage_repo_is_not_overridden(isolate_agent_runtime_root):
    from agent_runtime.default_plan import ensure_default_mission_plan
    from agent_runtime.final_gate import stage_repo_for_gate

    task = _goal_task("Cross-repo goal", repos=["hermes-agent"])
    plan = ensure_default_mission_plan(task)
    plan.blueprint_id = "custom_graph_v1"
    stage = next(s for s in plan.stages if s.id == "implement")

    assert stage_repo_for_gate(task, stage) == "EterniaLauncher"


def test_default_blueprint_cross_stack_product_stage_repo_survives_stale_task_scope(isolate_agent_runtime_root):
    from agent_runtime.default_plan import ensure_default_mission_plan
    from agent_runtime.final_gate import stage_repo_for_gate

    task = _goal_task(
        "Fork check: parallel Backend plus Launcher health; Launcher consumes the backend contract.",
        repos=["EterniaBackend"],
    )
    plan = ensure_default_mission_plan(task)
    stage = next(s for s in plan.stages if s.id == "implement")
    plan.current_stage_id = "implement"
    task.current_stage_id = "implement"

    assert stage_repo_for_gate(task, stage) == "EterniaLauncher"
