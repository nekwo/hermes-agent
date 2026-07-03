from hermes_time import now

from agent_runtime.final_gate import final_gate_commands
from agent_runtime.models import Task, TaskStage
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


def test_authoritative_gate_decision_uses_goal_named_command_for_no_edit_stage():
    from agent_runtime.ticker import _build_authoritative_stage_gate_decision

    task = _goal_task(
        "No-edit investigation: in the hermes-agent repo run `python -m pytest tests/agent_runtime/test_liveness.py -q` and report findings without product edits.",
        repos=["hermes-agent"],
    )
    stage = TaskStage(
        id="implement",
        title="No-Edit Investigation",
        objective="Investigate without product edits and attach focused proof.",
        status=StageStatus.IMPLEMENTING,
        test_plan=[],
    )

    decision = _build_authoritative_stage_gate_decision(task, stage)
    assert decision is not None
    assert decision.payload["commands"] == ["python -m pytest tests/agent_runtime/test_liveness.py -q"]
    assert decision.payload["proof_intent"] == "authoritative_gate_after_hand_off"
