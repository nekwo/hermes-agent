#!/usr/bin/env python3
"""Mission goal tool — create a REAL Mission Control goal from operator chat.

Lets the supervisor persona turn an operator request ("kick off a goal",
"trigger a mission") into a live harness task that self-drives through the
Neko -> Dev -> QA pipeline with real models, proof, and budgets — instead of
running the deterministic no-model smoke (``agent_runtime/smoke.py``), which
only validates the graph in a throwaway temp root and never appears in Mission
Control.

Runs fully in-process via ``agent_runtime.mission_goal.create_mission_goal`` so
it never shells out to the ``hermes`` CLI (which can hit the ``agent.log``
rotation lock when one Hermes process invokes another).
"""

import json
import logging

from tools.registry import registry

logger = logging.getLogger(__name__)


MISSION_GOAL_CREATE_SCHEMA = {
    "name": "mission_goal_create",
    "description": (
        "Create a REAL Mission Control goal: a live self-driving Neko -> Dev -> QA task with proof gates and a tracked task_id. Use when the operator asks to start/kick off a goal. Disambiguator: NOT the no-model smoke test and NOT agent_chat_send -- this starts real tracked work and parks other open goals."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short imperative goal title, e.g. 'Add reply quoting to broadcast composer'.",
            },
            "description": {
                "type": "string",
                "description": (
                    "What the goal must accomplish, with enough detail for the Dev/QA personas: scope, "
                    "affected area, proof expectations, and explicit non-goals."
                ),
            },
            "start_daemon": {
                "type": "boolean",
                "description": "Start the Mission Daemon so the goal self-drives without manual ticks. Default true.",
                "default": True,
            },
        },
        "required": ["title", "description"],
    },
}


def mission_goal_create(*, title, description, start_daemon=True, requested_by="mission-control-chat"):
    title = (title or "").strip()
    description = (description or "").strip()
    if not title:
        return json.dumps({"ok": False, "error": "mission_goal_create requires a non-empty title."})
    if not description:
        return json.dumps({"ok": False, "error": "mission_goal_create requires a non-empty description."})
    try:
        from agent_runtime.mission_goal import create_mission_goal

        data = create_mission_goal(
            title=title,
            description=description,
            requested_by=requested_by or "mission-control-chat",
            start_daemon_mode=bool(start_daemon),
        )
    except Exception as exc:  # pragma: no cover - defensive; surfaced to the model
        logger.exception("mission_goal_create failed")
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return json.dumps({"ok": True, **data}, indent=2, default=str)


registry.register(
    name="mission_goal_create",
    toolset="mission_goal",
    schema=MISSION_GOAL_CREATE_SCHEMA,
    handler=lambda args, **kw: mission_goal_create(
        title=args.get("title"),
        description=args.get("description"),
        start_daemon=args.get("start_daemon", True),
        requested_by="mission-control-chat",
    ),
    description="Create a real Mission Control goal and start the Mission Daemon.",
    emoji="🚀",
)
