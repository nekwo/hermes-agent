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
        state=TaskState.DEV_IMPLEMENTING,
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

