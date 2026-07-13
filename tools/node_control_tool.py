#!/usr/bin/env python3
"""Root-node Harness service tools."""

from tools.registry import registry


RUN_NODE_SCHEMA = {
    "name": "run_node",
    "description": "Root-only Harness service tool: run one child node stage to completion and return summary plus harness-captured evidence.",
    "parameters": {
        "type": "object",
        "properties": {
            "stage": {
                "type": "object",
                "description": "Stage object with id, title, objective, owner/persona_id, repo, acceptance_criteria, and test_plan.",
            }
        },
        "required": ["stage"],
        "additionalProperties": False,
    },
}


STEER_NODE_SCHEMA = {
    "name": "steer_node",
    "description": "Root-only Harness service tool: run one more turn in a child node's same session.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Child node session_id returned by run_node or steer_node."},
            "message": {"type": "string", "description": "Redaction-safe steering message for the child node's next turn."},
            "stage_id": {"type": "string", "description": "Optional stage id for persistence."},
            "persona_id": {"type": "string", "description": "Optional child persona id if the session cannot be resolved."},
        },
        "required": ["session_id", "message"],
        "additionalProperties": False,
    },
}


registry.register(
    name="run_node",
    toolset="node_control",
    schema=RUN_NODE_SCHEMA,
    handler=lambda args, **kwargs: __import__("agent_runtime.node_tools", fromlist=["run_node_tool"]).run_node_tool(args, **kwargs),
    check_fn=lambda: __import__("agent_runtime.node_tools", fromlist=["node_tools_available"]).node_tools_available(),
    description="Run a child node stage from the root Harness node.",
    emoji="",
)


registry.register(
    name="steer_node",
    toolset="node_control",
    schema=STEER_NODE_SCHEMA,
    handler=lambda args, **kwargs: __import__("agent_runtime.node_tools", fromlist=["steer_node_tool"]).steer_node_tool(args, **kwargs),
    check_fn=lambda: __import__("agent_runtime.node_tools", fromlist=["node_tools_available"]).node_tools_available(),
    description="Steer a child node session from the root Harness node.",
    emoji="",
)
