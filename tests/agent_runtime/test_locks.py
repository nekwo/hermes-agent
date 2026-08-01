from agent_runtime.locks import task_lock


def test_task_lock_uses_task_specific_lock_file(isolate_agent_runtime_root):
    with task_lock("task_abc"):
        assert (isolate_agent_runtime_root / "locks" / "task_task_abc.lock").exists()
