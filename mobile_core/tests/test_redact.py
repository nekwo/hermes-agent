from __future__ import annotations

import json

from hermes_mobile_core.redact import REDACTED, redact, redact_text


def test_recursive_headers_and_signed_urls_are_redacted() -> None:
    value = {
        "Authorization": "Bearer secret-value",
        "nested": {
            "api_key": "secret-two",
            "url": "https://example.invalid/file?X-Amz-Signature=abc&safe=yes",
        },
        "message": "Authorization: Bearer third-secret",
    }
    cleaned = redact(value)
    rendered = json.dumps(cleaned)
    assert "secret" not in rendered.lower()
    assert cleaned["Authorization"] == REDACTED
    assert "X-Amz-Signature=%5BREDACTED%5D" in cleaned["nested"]["url"]
    assert "safe=yes" in cleaned["nested"]["url"]


def test_bearer_token_is_redacted_from_free_text() -> None:
    assert redact_text("failed with Bearer abc.def-123") == "failed with Bearer [REDACTED]"
