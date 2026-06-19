from __future__ import annotations

import json

from .serde import to_jsonable


def emit_json(data) -> str:
    return json.dumps(to_jsonable(data), indent=2, ensure_ascii=False, sort_keys=True)


def task_summary(task) -> dict:
    return {"task_id": task.id, "title": task.title, "state": str(task.state), "updated_at": task.updated_at}


def human_task_line(task) -> str:
    return f"{task.id} [{task.state}] {task.title}"
