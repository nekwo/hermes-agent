from agent_runtime.observability import _safe_event_payload


def test_observability_preserves_dev_work_payload_for_mission_control():
    payload = _safe_event_payload(
        {
            "phase": "dev_work",
            "step": "patch",
            "tool_name": "patch",
            "status": "passed",
            "summary": "Patched 1 file: mission_control_page.dart",
            "detail": "Changed files: mission_control_page.dart",
            "patch_summary": "Patched 1 file",
            "changed_files": ["mission_control_page.dart", "C:/Users/beast/secret_token.dart"],
            "files_touched": 2,
            "diff": "SECRET raw diff must be dropped",
        }
    )

    assert payload == {
        "phase": "dev_work",
        "step": "patch",
        "tool_name": "patch",
        "status": "passed",
        "summary": "Patched 1 file: mission_control_page.dart",
        "detail": "Changed files: mission_control_page.dart",
        "patch_summary": "Patched 1 file",
        "changed_files": ["mission_control_page.dart"],
        "files_touched": 2,
    }


def test_observability_carries_patch_shape_but_never_the_artifact_path():
    """Lane separation, pinned.

    This is the Telegram-grade lane: it is path-free by design. The SHAPE of a
    patch (how many lines moved, which grammar) is safe to carry there; WHERE
    the diff was written is not, and the artifact path must not appear even
    though the mission-chat lane carries it happily."""

    payload = _safe_event_payload(
        {
            "phase": "dev_work",
            "step": "patch",
            "tool_name": "patch",
            "status": "passed",
            "patch_summary": "Patched 1 file",
            "patch_adds": 12,
            "patch_dels": 3,
            "patch_mode": "replace",
            "patch_artifact": (
                "C:/Users/beast/.hermes/agent-runtime/patch_diffs/x.diff"
            ),
        }
    )

    assert payload == {
        "phase": "dev_work",
        "step": "patch",
        "tool_name": "patch",
        "status": "passed",
        "patch_summary": "Patched 1 file",
        "patch_adds": 12,
        "patch_dels": 3,
        "patch_mode": "replace",
    }
    encoded = repr(payload)
    assert "patch_artifact" not in encoded
    assert "patch_diffs" not in encoded


def test_observability_preserves_safe_agent_thinking_summary_only():
    payload = _safe_event_payload(
        {
            "phase": "thinking_process",
            "step": "reasoning_summary",
            "status": "running",
            "summary": "Agent thinking process updated",
            "reasoning_summary": "Comparing tool proof against acceptance criteria.",
            "raw_thoughts": "SECRET hidden chain-of-thought must be dropped",
            "detail": "C:/Users/beast/private_token.txt must be dropped",
        }
    )

    assert payload == {
        "phase": "thinking_process",
        "step": "reasoning_summary",
        "status": "running",
        "summary": "Agent thinking process updated",
        "reasoning_summary": "Comparing tool proof against acceptance criteria.",
    }
    encoded = repr(payload)
    assert "raw_thoughts" not in encoded
    assert "SECRET" not in encoded
    assert "C:/Users" not in encoded
