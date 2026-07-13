from agent_runtime.progress import _safe_progress_payload


def test_observe_mode_keeps_suppressed_progress_payload_with_marker(monkeypatch):
    monkeypatch.setenv("HERMES_REDACTION_MODE", "observe")

    payload = _safe_progress_payload(
        "run.progress",
            {
                "raw_model_output": "ordinary operator-visible output",
                "command_label": "curl -H API_KEY=abcdef1234567890",
            },
        )

    assert payload["raw_model_output"] == "ordinary operator-visible output"
    assert "[redacted line" in payload["command_label"]
    assert payload["would_redact"]["raw_model_output"] == "unsupported_progress_key"
    assert payload["would_redact"]["command_label"] == "command_label"


def test_observe_mode_still_masks_secret_lines(monkeypatch):
    monkeypatch.setenv("HERMES_REDACTION_MODE", "observe")

    payload = _safe_progress_payload(
        "run.progress",
        {"raw_model_output": "safe line\nAPI_KEY=abcdef1234567890\nnext line"},
    )

    assert "safe line" in payload["raw_model_output"]
    assert "[redacted line" in payload["raw_model_output"]
    assert "abcdef1234567890" not in payload["raw_model_output"]
