from agent_runtime.capabilities import capability_descriptors, capability_ids
from agent_runtime.models import Task
from agent_runtime.observability import build_observability
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore
from hermes_time import now


def test_capability_descriptors_are_unique_and_redaction_safe():
    envelope = capability_descriptors()
    descriptors = envelope["descriptors"]
    ids = [item["id"] for item in descriptors]
    by_id = {item["id"]: item for item in descriptors}

    assert envelope["schema_version"] == 1
    assert envelope["write_boundary"] == "hermes harness --json"
    assert len(ids) == len(set(ids))
    assert "mission.chat.message" in ids
    assert "persona.profile.instantiate" in ids
    assert "persona.instance.create" in ids
    assert "persona.instance.open_chat" in ids
    assert "persona.instance.message" not in ids
    assert "persona.instance.run_once" not in ids
    assert "persona.profile.create" in ids
    assert "persona.profile.promote" in ids
    assert "task.run_until_settled" in ids
    assert "daemon.start" in ids
    assert "shell.anything" not in ids
    assert all("command" not in item for item in descriptors)
    assert all(item["capability_id"] == item["id"] for item in descriptors)
    assert all(item["args_schema"]["additionalProperties"] is False for item in descriptors)
    assert all(item["enabled"] is True for item in descriptors)
    assert all("readback" in item for item in descriptors)
    assert by_id["persona.profile.instantiate"]["args_schema"]["required"] == ["display_name"]
    assert by_id["persona.instance.create"]["args_schema"]["required"] == []
    assert by_id["persona.instance.create"]["label"] == "Open Agent Chat"
    assert "kill_active" in by_id["persona.profile.instantiate"]["allowed_args"]
    assert by_id["persona.profile.instantiate"]["args_schema"]["properties"]["kill_active"]["type"] == "boolean"
    assert {"add_instance", "placement_id", "kill_active"}.issubset(
        set(by_id["persona.instance.create"]["allowed_args"])
    )
    assert by_id["persona.instance.create"]["args_schema"]["properties"]["add_instance"]["type"] == "boolean"
    assert by_id["persona.instance.create"]["args_schema"]["properties"]["kill_active"]["type"] == "boolean"
    assert by_id["persona.instance.create"]["danger"] == "warning"
    assert by_id["persona.instance.create"]["args_schema"]["properties"]["coordinator_spawns_used"]["minimum"] == 0
    assert by_id["persona.instance.create"]["args_schema"]["properties"]["coordinator_may_kill_own"]["type"] == "boolean"
    assert "session_id" in by_id["mission.chat.message"]["allowed_args"]
    assert by_id["mission.chat.message"]["default_args"]["surface_prompt"] == ""
    assert {"add_instance", "placement_id", "kill_active"}.issubset(
        set(by_id["persona.instance.open_chat"]["allowed_args"])
    )
    assert {"requested_by", "coordinator_id", "coordinator_max_spawns", "coordinator_may_kill_own"}.issubset(
        set(by_id["persona.instance.close"]["allowed_args"])
    )
    assert {"requested_by", "coordinator_id", "coordinator_max_spawns", "coordinator_may_kill_own"}.issubset(
        set(by_id["run.cancel"]["allowed_args"])
    )
    for capability_id in (
        "persona.message_task",
        "mission.chat.message",
        "persona.diagnose",
    ):
        assert "attachments" in by_id[capability_id].get("allowed_args", [])
        attachment_schema = by_id[capability_id]["args_schema"]["properties"]["attachments"]
        assert attachment_schema["type"] == "array"
        assert attachment_schema["items"]["required"] == ["kind", "mime", "uri"]
        assert attachment_schema["items"]["properties"]["kind"]["enum"] == ["image", "video", "file"]
    assert by_id["persona.instance.open_chat"]["args_schema"]["required"] == ["persona_id", "session_id"]
    assert "attachments" not in by_id["worker.nudge"].get("allowed_args", [])


def test_snapshot_emits_callable_capability_descriptors(isolate_agent_runtime_root):
    ts = TaskStore()
    stamp = now()
    ts.create(
        Task(
            id="task_caps",
            title="Capability smoke",
            description="d",
            state=TaskState.CREATED,
            created_at=stamp,
            updated_at=stamp,
            requested_by="tony",
        )
    )

    snap = build_snapshot(task_store=ts)
    ids = {item["id"] for item in snap["capabilities"]["descriptors"]}

    assert snap["capabilities"]["schema_version"] == 1
    assert capability_ids().issubset(ids)
    assert "mission.chat.message" in ids
    assert "persona.instance.message" not in ids
    assert "daemon.start" in ids


def test_observability_emits_same_capability_descriptors(isolate_agent_runtime_root):
    envelope = build_observability(
        tasks=[],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "offline"},
    )

    ids = {item["id"] for item in envelope["capabilities"]["descriptors"]}

    assert envelope["capabilities"]["schema_version"] == 1
    assert capability_ids().issubset(ids)
    assert "task.archive" in ids
    assert "run.cancel" in ids
