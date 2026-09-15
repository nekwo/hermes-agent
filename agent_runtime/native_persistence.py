"""Persona-chat live/persisted wire projection at the upstream database flush boundary."""
from agent_runtime.persona_chat_continuity import native_wire_row, record_wire_boundary_drift


def project_native_message(agent, msg, content, msg_idx):
    bound = native_wire_row({
        **msg,
        "content": content,
        "tool_calls": msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else None,
        "root_chat_session_id": agent._persona_chat_root_session_id,
        "client_message_id": agent._persona_chat_client_message_id,
        "turn_id": agent._persona_chat_turn_id,
    })
    record_wire_boundary_drift(bound)
    msg.clear()
    msg.update(bound.row)
    platform_id = agent._persona_chat_client_message_id
    role = msg.get("role", "unknown")
    if role != "user":
        platform_id = f"{platform_id}:{role}:{msg_idx}"
    msg["platform_message_id"] = platform_id
    return role, msg.get("content")
