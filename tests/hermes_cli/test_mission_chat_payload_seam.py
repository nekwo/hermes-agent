"""The mission-chat payload seam, and the capability answer bound beside it.

``_mission_chat_emit`` is the ONE place a mission-chat turn payload leaves the
handler. Before it existed, an in-process caller (the agent-to-agent relay) got
the payload by wrapping the call in ``contextlib.redirect_stdout`` and parsing
the captured text back into JSON — which rebinds ``sys.stdout``
PROCESS-GLOBALLY, so every other thread in a serve process wrote into that
buffer for the duration. These tests pin both halves of the replacement: the CLI
still prints byte-for-byte what it printed before, and a caller that installs a
sink gets the dict and NO output at all.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

import hermes_cli.harness as harness

PAYLOAD = {"ok": False, "error": "boom", "error_kind": "unsupported_persona"}


def _emit(args, *rest, **kwargs) -> str:
    """Run the seam and return whatever reached stdout."""

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        harness._mission_chat_emit(args, *rest, **kwargs)
    return buffer.getvalue()


def test_the_cli_json_lane_prints_exactly_emit_json():
    args = SimpleNamespace(json=True, stream=False)

    assert _emit(args, PAYLOAD) == harness.emit_json(PAYLOAD) + "\n"


def test_the_cli_text_lane_prints_the_error_by_default():
    args = SimpleNamespace(json=False, stream=False)

    assert _emit(args, PAYLOAD) == "boom\n"


def test_an_explicit_plain_line_overrides_the_error():
    args = SimpleNamespace(json=False, stream=False)

    assert _emit(args, PAYLOAD, "mission chat reply for dev") == "mission chat reply for dev\n"


def test_the_stream_lane_emits_the_chat_final_envelope():
    args = SimpleNamespace(json=True, stream=True)

    frame = json.loads(_emit(args, PAYLOAD))

    assert frame["type"] == "chat.final"
    assert frame["error"] == "boom"


def test_a_stream_override_wins_over_the_args_attribute():
    """One site had already resolved `stream` into a local; it must still route."""

    args = SimpleNamespace(json=True, stream=False)

    assert json.loads(_emit(args, PAYLOAD, stream=True))["type"] == "chat.final"


def test_a_sink_takes_the_dict_and_suppresses_all_output():
    """The whole point: a nested reply must never touch the caller's stdout.

    In serve, stdout is a per-request line-frame proxy, so a nested handler's
    print lands in the OUTER request's capture and corrupts its payload. The
    sink removes the print rather than redirecting it.
    """

    seen = []
    args = SimpleNamespace(json=True, stream=False, payload_sink=seen.append)

    assert _emit(args, PAYLOAD) == ""
    assert seen == [PAYLOAD]
    # Handed over BY REFERENCE — no serialise/parse round trip in between.
    assert seen[0] is PAYLOAD


def test_a_sink_wins_over_the_stream_lane_too():
    seen = []
    args = SimpleNamespace(json=True, stream=True, payload_sink=seen.append)

    assert _emit(args, PAYLOAD) == ""
    assert seen == [PAYLOAD]


def test_a_non_callable_sink_falls_back_to_printing():
    """An argparse Namespace can never carry one, but fail toward the CLI shape."""

    args = SimpleNamespace(json=True, stream=False, payload_sink=None)

    assert _emit(args, PAYLOAD) == harness.emit_json(PAYLOAD) + "\n"


# --------------------------------------------------------------------------
# capability honesty
# --------------------------------------------------------------------------


@pytest.fixture
def unbound_capability():
    """Reset the per-session delivery capability around each check."""

    from gateway.session_context import _SESSION_ASYNC_DELIVERY, _UNSET

    token = _SESSION_ASYNC_DELIVERY.set(_UNSET)
    yield
    _SESSION_ASYNC_DELIVERY.reset(token)


def test_a_serve_hosted_turn_can_take_a_late_completion(monkeypatch, unbound_capability):
    """True because the drain runs HERE — not because nothing bound the var."""

    from gateway.session_context import async_delivery_supported

    monkeypatch.setattr(harness, "persona_chat_runtime_registry", lambda: object())

    assert harness._bind_mission_chat_delivery_capability() is True
    assert async_delivery_supported() is True


def test_a_cold_cli_turn_refuses_the_promise(monkeypatch, unbound_capability):
    """The process exits with the turn, so `terminal` notifications die with it.

    ``delegate_task`` then falls back to its inline path and returns the result
    INSIDE the turn that asked for it, which is strictly better than a promise
    that would be kept, if ever, in some future session.
    """

    from gateway.session_context import async_delivery_supported

    monkeypatch.setattr(harness, "persona_chat_runtime_registry", lambda: None)

    assert harness._bind_mission_chat_delivery_capability() is False
    assert async_delivery_supported() is False
