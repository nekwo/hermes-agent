from __future__ import annotations

from typing import Any

CAPABILITY_SCHEMA_VERSION = 1

_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "id": "task.create",
        "target_kind": "task_collection",
        "label": "Create Goal",
        "group": "queue",
        "execution_semantics": "queues_only",
        "required_args": ["title", "description"],
        "danger": "normal",
    },
    {
        "id": "persona.message_task",
        "target_kind": "task",
        "label": "Queue Message",
        "group": "queue",
        "execution_semantics": "queues_only",
        "required_args": ["persona_id", "message"],
        "allowed_args": ["attachments"],
        "default_args": {"title": "Operator message"},
        "danger": "normal",
    },
    {
        "id": "persona.instance.message",
        "target_kind": "persona_instance",
        "label": "Queue Message",
        "group": "queue",
        "execution_semantics": "bounded_execution",
        "required_args": ["message"],
        "allowed_args": ["attachments", "auto_run", "max_actions", "max_seconds"],
        "default_args": {"title": "Free-floating operator message", "auto_run": True, "max_actions": 1, "max_seconds": 240},
        "danger": "normal",
    },
    {
        "id": "persona.profile.instantiate",
        "target_kind": "persona",
        "label": "Create Agent Profile",
        "group": "lifecycle",
        "execution_semantics": "control_state_change",
        "required_args": ["display_name"],
        "allowed_args": ["attachments", "auto_run", "max_actions", "max_seconds", "message", "session_id"],
        "default_args": {"title": "Create Agent Profile", "message": "Create Mission Control agent profile.", "auto_run": False, "max_actions": 1, "max_seconds": 240},
        "danger": "normal",
    },
    {
        "id": "persona.instance.create",
        "target_kind": "persona",
        "label": "Create Agent Profile",
        "group": "lifecycle",
        "execution_semantics": "control_state_change",
        "required_args": ["display_name"],
        "allowed_args": ["attachments", "auto_run", "max_actions", "max_seconds", "message", "session_id"],
        "default_args": {"title": "Create Agent Profile", "message": "Create Mission Control agent profile.", "auto_run": False, "max_actions": 1, "max_seconds": 240},
        "danger": "normal",
    },
    {
        "id": "persona.instance.open_chat",
        "target_kind": "persona_instance",
        "label": "Open Chat",
        "group": "lifecycle",
        "execution_semantics": "control_state_change",
        "required_args": ["persona_id", "session_id"],
        "danger": "normal",
    },
    {
        "id": "persona.profile.create",
        "target_kind": "persona_collection",
        "label": "Create Hermes Profile",
        "group": "lifecycle",
        "execution_semantics": "profile_create",
        "required_args": ["name"],
        "allowed_args": ["clone_from", "provider", "model", "description"],
        "danger": "normal",
    },
    {
        "id": "persona.profile.promote",
        "target_kind": "persona",
        "label": "Promote Profile To Persona",
        "group": "lifecycle",
        "execution_semantics": "persona_promotion",
        "required_args": ["profile", "slot_role"],
        "allowed_args": ["persona_id"],
        "danger": "normal",
    },
    {
        "id": "persona.profile.delete",
        "target_kind": "persona",
        "label": "Delete Hermes Profile",
        "group": "archive",
        "execution_semantics": "destructive_profile_delete",
        "required_args": ["profile"],
        "allowed_args": ["persona_id"],
        "danger": "destructive",
    },
    {
        "id": "task.tick",
        "target_kind": "task",
        "label": "Run Tick",
        "group": "run",
        "execution_semantics": "bounded_execution",
        "danger": "normal",
    },
    {
        "id": "task.run_until_settled",
        "target_kind": "task",
        "label": "Run Until Settled",
        "group": "run",
        "execution_semantics": "bounded_execution",
        "default_args": {"max_actions": 10, "max_seconds": 240},
        "danger": "normal",
    },
    {
        "id": "persona.instance.run_once",
        "target_kind": "persona_instance",
        "label": "Run One Turn",
        "group": "run",
        "execution_semantics": "bounded_execution",
        "allowed_args": ["attachments"],
        "default_args": {
            "title": "Free-floating operator message",
            "message": "Run one bounded free-floating persona sandbox turn.",
            "max_actions": 1,
            "max_seconds": 240,
        },
        "danger": "normal",
    },
    {
        "id": "persona.diagnose",
        "target_kind": "persona",
        "label": "Test Persona",
        "group": "run",
        "execution_semantics": "bounded_execution",
        "required_args": ["message"],
        "allowed_args": ["attachments"],
        "default_args": {"title": "Persona diagnostic", "max_actions": 1, "max_seconds": 240},
        "danger": "normal",
    },
    {
        "id": "worker.nudge",
        "target_kind": "worker_session",
        "label": "Nudge",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "required_args": ["note"],
        "danger": "normal",
    },
    {
        "id": "worker.pause",
        "target_kind": "worker_session",
        "label": "Pause",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "required_args": ["reason"],
        "danger": "warning",
    },
    {
        "id": "worker.resume",
        "target_kind": "worker_session",
        "label": "Resume",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "required_args": ["reason"],
        "danger": "normal",
    },
    {
        "id": "worker.interrupt",
        "target_kind": "worker_session",
        "label": "Interrupt",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "required_args": ["reason"],
        "danger": "warning",
    },
    {
        "id": "worker.possess",
        "target_kind": "worker_session",
        "label": "Possess",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "default_args": {"lease_seconds": 900},
        "danger": "normal",
    },
    {
        "id": "worker.release",
        "target_kind": "worker_session",
        "label": "Release",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "required_args": ["reason"],
        "danger": "normal",
    },
    {
        "id": "run.cancel",
        "target_kind": "run",
        "label": "Cancel Run",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "required_args": ["reason"],
        "danger": "destructive",
    },
    {
        "id": "run.approve",
        "target_kind": "run",
        "label": "Approve Run",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "danger": "normal",
    },
    {
        "id": "task.unblock",
        "target_kind": "task",
        "label": "Unblock Task",
        "group": "steer",
        "execution_semantics": "control_state_change",
        "required_args": ["reason"],
        "default_args": {"state": "created", "rescope": False, "foreground": False},
        "danger": "warning",
    },
    *(
        {
            "id": f"lane.{verb}",
            "target_kind": "lane",
            "label": f"{verb.title()} Lane",
            "group": "steer",
            "execution_semantics": "control_state_change",
            "required_args": ["reason"],
            "danger": "warning" if verb in {"pause", "park", "drain"} else "normal",
        }
        for verb in ("pause", "park", "resume", "drain")
    ),
    {
        "id": "daemon.start",
        "target_kind": "daemon",
        "label": "Start Daemon",
        "group": "lifecycle",
        "execution_semantics": "daemon_lifecycle",
        "danger": "normal",
    },
    {
        "id": "daemon.stop",
        "target_kind": "daemon",
        "label": "Stop Daemon",
        "group": "lifecycle",
        "execution_semantics": "daemon_lifecycle",
        "danger": "warning",
    },
    {
        "id": "daemon.run_once",
        "target_kind": "daemon",
        "label": "Run Daemon Loop Once",
        "group": "run",
        "execution_semantics": "bounded_execution",
        "danger": "normal",
    },
    {
        "id": "persona.instance.close",
        "target_kind": "persona_instance",
        "label": "Close Assignment",
        "group": "archive",
        "execution_semantics": "archive_or_close",
        "required_args": ["reason"],
        "danger": "warning",
    },
    {
        "id": "persona.instance.archive",
        "target_kind": "persona_instance",
        "label": "Archive Assignment",
        "group": "archive",
        "execution_semantics": "archive_or_close",
        "required_args": ["reason"],
        "danger": "warning",
    },
    {
        "id": "task.archive",
        "target_kind": "task",
        "label": "Archive Task",
        "group": "archive",
        "execution_semantics": "archive_or_close",
        "danger": "warning",
    },
    {
        "id": "task.archive_ready",
        "target_kind": "task_collection",
        "label": "Archive Ready Tasks",
        "group": "archive",
        "execution_semantics": "archive_or_close",
        "danger": "warning",
    },
)


def _arg_schema_for(item: dict[str, Any]) -> dict[str, Any]:
    required = [str(name) for name in item.get("required_args", [])]
    allowed = {
        *required,
        *(str(name) for name in item.get("allowed_args", [])),
        *(str(name) for name in item.get("default_args", {}).keys()),
    }
    properties: dict[str, Any] = {}
    for name in sorted(allowed):
        if name == "attachments":
            properties[name] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["kind", "mime", "uri"],
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": ["image", "video", "file"]},
                        "mime": {"type": "string", "minLength": 1},
                        "uri": {"type": "string", "minLength": 1},
                        "safe_label": {"type": "string"},
                    },
                },
            }
        elif name in {"max_actions", "max_seconds", "lease_seconds"}:
            properties[name] = {"type": "integer", "minimum": 1}
        elif name in {"rescope", "foreground", "auto_run"}:
            properties[name] = {"type": "boolean"}
        else:
            properties[name] = {"type": "string", "minLength": 1}
    return {
        "type": "object",
        "required": required,
        "additionalProperties": False,
        "properties": properties,
    }


def _readback_for(item: dict[str, Any]) -> list[str]:
    capability_id = str(item["id"])
    if capability_id.startswith("persona."):
        return ["snapshot", "persona.assignments"]
    if capability_id.startswith("worker."):
        return ["snapshot", "worker.show"]
    if capability_id.startswith("run."):
        return ["snapshot", "run.show"]
    if capability_id.startswith("daemon."):
        return ["snapshot", "status"]
    if capability_id.startswith("task.archive"):
        return ["snapshot", "task.history"]
    return ["snapshot"]


def _descriptor_for(item: dict[str, Any]) -> dict[str, Any]:
    descriptor = dict(item)
    descriptor.setdefault("schema_version", CAPABILITY_SCHEMA_VERSION)
    descriptor.setdefault("capability_id", item["id"])
    descriptor.setdefault("danger_level", item.get("danger", "normal"))
    descriptor.setdefault("enabled", True)
    descriptor.setdefault("disabled_reason", None)
    descriptor.setdefault("readback", _readback_for(item))
    descriptor.setdefault("args_schema", _arg_schema_for(item))
    return descriptor


def capability_descriptors() -> dict[str, Any]:
    """Return redaction-safe callable Harness capability descriptors.

    The descriptors are declarative. Callers still submit actions through audited
    `hermes harness ... --json` commands; this catalog only tells UIs what those
    supported calls are and what arguments are accepted.
    """

    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "write_boundary": "hermes harness --json",
        "descriptors": [_descriptor_for(item) for item in _CAPABILITIES],
    }


def capability_ids() -> set[str]:
    return {str(item["id"]) for item in _CAPABILITIES}
