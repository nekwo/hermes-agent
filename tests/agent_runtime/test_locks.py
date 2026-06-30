from agent_runtime.locks import task_lock, tick_lock


def test_tick_lock_creates_lock_file_and_allows_reentry_after_release(isolate_agent_runtime_root):
    with tick_lock():
        assert (isolate_agent_runtime_root / "locks" / "tick.lock").exists()

    with tick_lock():
        assert (isolate_agent_runtime_root / "locks" / "tick.lock").exists()


def test_task_lock_uses_task_specific_lock_file(isolate_agent_runtime_root):
    with task_lock("task_abc"):
        assert (isolate_agent_runtime_root / "locks" / "task_task_abc.lock").exists()
