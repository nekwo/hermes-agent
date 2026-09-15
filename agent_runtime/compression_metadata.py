"""Keep persona ownership across native compression rotations."""
import json


def child_model_config(agent):
    config = agent._session_init_model_config
    root_id = getattr(agent, "_persona_chat_root_session_id", None)
    if not root_id:
        return config
    try:
        row = agent._session_db.get_session(root_id) or {}
        metadata = json.loads(row.get("model_config") or "{}")
    except Exception:
        metadata = {}
    return {
        **(config or {}),
        **{key: metadata[key] for key in ("mission_chat_root_id", "persona_instance_id", "source")
           if metadata.get(key) is not None},
    }
