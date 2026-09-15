from __future__ import annotations

import json
from pathlib import Path

from hermes_mobile_core.turn_runner import normalize_parsed_response, normalized_payload


def test_captured_completed_responses_normalize_identically() -> None:
    from agent.transports.chat_completions import ChatCompletionsTransport as LiveTransport
    from hermes_mobile_core._vendor.agent.transports.chat_completions import (
        ChatCompletionsTransport as VendoredTransport,
    )

    fixture_path = Path(__file__).parent / "golden" / "completions.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixtures
    for fixture in fixtures:
        live = normalized_payload(
            normalize_parsed_response(LiveTransport(), fixture["response"])
        )
        vendored = normalized_payload(
            normalize_parsed_response(VendoredTransport(), fixture["response"])
        )
        assert vendored == live, fixture["name"]


def test_all_golden_fixtures_are_redaction_safe() -> None:
    golden = Path(__file__).parent / "golden"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in golden.glob("*.json"))
    forbidden = ("sk-", "Bearer ", "api_key", "authorization", "cookie")
    assert not any(marker.lower() in combined.lower() for marker in forbidden)
