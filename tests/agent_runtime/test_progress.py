from agent_runtime.progress import _safe_progress_payload


def test_safe_progress_payload_preserves_dev_work_file_summary_but_not_paths():
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "phase": "dev_work",
            "step": "patch",
            "tool_name": "patch",
            "status": "passed",
            "summary": "Patched 2 files: mission_control_page.dart, private_token.dart",
            "detail": "Changed files: mission_control_page.dart, C:/Users/beast/private_token.dart",
            "patch_summary": "Patched 2 files",
            "changed_files": [
                "mission_control_page.dart",
                "C:/Users/beast/private_token.dart",
                "api_key.txt",
            ],
            "files_touched": 3,
            "diff": "SECRET raw diff must be dropped",
        },
    )

    assert payload == {
        "type": "run.tool.finished",
        "phase": "dev_work",
        "step": "patch",
        "tool_name": "patch",
        "status": "passed",
        "patch_summary": "Patched 2 files",
        "changed_files": ["mission_control_page.dart"],
        "files_touched": 3,
    }
    encoded = repr(payload)
    assert "C:/Users" not in encoded
    assert "api_key" not in encoded
    assert "SECRET" not in encoded


def test_safe_progress_payload_preserves_agent_thinking_summary_only():
    payload = _safe_progress_payload(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "thinking_process",
            "step": "reasoning_summary",
            "status": "running",
            "summary": "Agent thinking process updated",
            "reasoning_summary": "Checking proof coverage before QA handoff.",
            "raw_thoughts": "SECRET hidden chain-of-thought must be dropped",
            "detail": "C:/Users/beast/private_token.txt must be dropped",
        },
    )

    assert payload == {
        "type": "run.progress",
        "phase": "thinking_process",
        "step": "reasoning_summary",
        "status": "running",
        "summary": "Agent thinking process updated",
        "reasoning_summary": "Checking proof coverage before QA handoff.",
    }
    encoded = repr(payload)
    assert "raw_thoughts" not in encoded
    assert "SECRET" not in encoded
    assert "C:/Users" not in encoded
