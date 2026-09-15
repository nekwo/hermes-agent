from hermes_cli.commands import COMMANDS, alias_deprecation_warning


def test_tasks_alias_is_deprecated():
    assert alias_deprecation_warning("/tasks") == (
        "/tasks is deprecated and will be removed after Stage 44; use /agents."
    )
    assert "deprecated alias for /agents" in COMMANDS["/tasks"]
