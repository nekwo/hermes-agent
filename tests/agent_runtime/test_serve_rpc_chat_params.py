"""R-C8 — the method carries the verb's whole operator surface, and the
runtime says which surface it carries.

The C5 field run (2026-09-06 00:01Z) measured the whole gap in one line: the
Windows cockpit's send to the Mac never left the launcher —

    chat_turn lane=argv capability=mission.chat.message
        reason=methodUnavailable detail=args_not_carried:workspace_name

``decorateMissionChatWithAgentContext`` puts ``workspace_name`` on EVERY console
send whose workspace has a name, ``normalize_chat_message`` read eleven keys and
silently ignored the rest, and the manifest advertised ``methods`` and ``tiers``
only — so the launcher could not know what a given runtime honours and correctly
refused to guess. Two halves, tested here:

1. the normaliser lowers the operator's whole surface to the argv a local send
   would have used (one test per key, an argv LITERAL, because the no-divergence
   claim is only worth what the exact list asserts), and
2. ``manifest()`` advertises a ``params`` block derived from the SAME tuple the
   normaliser reads, proven by driving the normaliser through a recording
   mapping — so the advertisement cannot drift from the code that honours it.

Silently ignoring an unknown param stays the contract (a client cannot be
refused for a key this runtime does not know); the ``params`` block is what
makes that safe, because a client can now tell "ignored" from "honoured" BEFORE
it sends.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from agent_runtime.chat_turn import (
    CHAT_MESSAGE_METHOD,
    CHAT_STEER_METHOD,
    CHAT_TURN_METHOD_PARAMS,
    ChatTurnInvalid,
    normalize_chat_message,
    normalize_chat_steer,
    perform_chat_turn,
)


def _message(**extra) -> list[str]:
    """The argv for a minimal send plus ``extra``."""

    params = {"turn_request_id": "k1", "persona_id": "neko", "message": "hi"}
    params.update(extra)
    return normalize_chat_message(params).argv


#: The argv every send lowers to before any optional key is added. Spelled once
#: so the per-key tests below can assert the SUFFIX their key contributes and
#: still prove nothing else moved.
BASE_ARGV = [
    "harness",
    "mission-chat",
    "message",
    "--persona",
    "neko",
    "--message",
    "hi",
    "--client-message-id",
    "k1",
    "--requested-by",
    "gateway_device",
    "--json",
]


# ── one key at a time, as argv ───────────────────────────────────────────────


def test_workspace_name_reaches_the_turn_that_the_console_names_it_on():
    """THE key from the field run. The console's decorator puts it on every
    send whose workspace has a name, so without this line the cockpit's ordinary
    message can never take the method lane to a remote install."""

    assert _message(workspace_name="Eternia") == BASE_ARGV + [
        "--workspace-name",
        "Eternia",
    ]


def test_a_provider_override_rides_the_turn():
    assert _message(provider="anthropic") == BASE_ARGV + ["--provider", "anthropic"]


def test_a_model_override_rides_the_turn():
    assert _message(model="claude-opus-4") == BASE_ARGV + ["--model", "claude-opus-4"]


def test_use_agent_default_is_a_flag_and_carries_no_value():
    assert _message(use_agent_default=True) == BASE_ARGV + ["--use-agent-default"]


def test_use_agent_default_false_is_the_absence_of_the_flag():
    assert _message(use_agent_default=False) == BASE_ARGV


def test_a_clarify_token_reaches_the_thread_the_question_was_asked_in():
    """C1l's own finding, from the other side. Dropping this one silently would
    answer into the WRONG thread — which is why the launcher refused the method
    lane for it, and why the fix is to carry it rather than to drop it."""

    assert _message(clarify_token="clr_9f2a") == BASE_ARGV + [
        "--clarify-token",
        "clr_9f2a",
    ]


def test_a_non_default_surface_prompt_rides_and_a_blank_one_does_not():
    """``--surface-prompt`` defaults to ``''`` in the parser, so a blank value
    is the operator expressing nothing. Sending the empty string would be a
    longer argv that means the same — and an argv that differs from a local
    send's for no reason is the divergence this lane exists to prevent."""

    assert _message(surface_prompt="be brief") == BASE_ARGV + [
        "--surface-prompt",
        "be brief",
    ]
    assert _message(surface_prompt="") == BASE_ARGV
    assert _message(surface_prompt="   ") == BASE_ARGV


def test_a_non_default_intent_hint_rides_and_the_default_word_does_not():
    """``--intent-hint`` defaults to ``chat``. The launcher sends the word on
    every ordinary message, so lowering it unconditionally would put a flag on
    every single turn that a local send does not carry."""

    assert _message(intent_hint="plan") == BASE_ARGV + ["--intent-hint", "plan"]
    assert _message(intent_hint="chat") == BASE_ARGV
    assert _message(intent_hint="") == BASE_ARGV


def test_the_new_keys_lower_in_one_stable_order_beside_the_old_ones():
    """The whole surface at once, as a literal — the same discipline
    ``test_the_rpc_door_lowers_to_the_argv_a_local_send_would_have_used`` holds
    for the original eleven, extended to the seven R-C8 added."""

    argv = normalize_chat_message(
        {
            "turn_request_id": "outbox-7",
            "persona_id": "neko",
            "message": "status?",
            "session_id": "root-1",
            "persona_instance_id": "personainst_neko_1",
            "workspace_id": "ws-1",
            "title": "Field run",
            "new_session": True,
            "stream": True,
            "max_seconds": 90,
            "workspace_name": "Eternia",
            "provider": "anthropic",
            "model": "claude-opus-4",
            "clarify_token": "clr_9f2a",
            "surface_prompt": "be brief",
            "intent_hint": "plan",
        }
    ).argv
    assert argv == [
        "harness",
        "mission-chat",
        "message",
        "--persona",
        "neko",
        "--message",
        "status?",
        "--client-message-id",
        "outbox-7",
        "--requested-by",
        "gateway_device",
        "--json",
        "--session-id",
        "root-1",
        "--persona-instance-id",
        "personainst_neko_1",
        "--workspace-id",
        "ws-1",
        "--title",
        "Field run",
        "--new-session",
        "--stream",
        "--max-seconds",
        "90.0",
        "--workspace-name",
        "Eternia",
        "--provider",
        "anthropic",
        "--model",
        "claude-opus-4",
        "--clarify-token",
        "clr_9f2a",
        "--surface-prompt",
        "be brief",
        "--intent-hint",
        "plan",
    ]
    from hermes_cli.harness_parts.serve import _ArgvRequest

    assert _ArgvRequest("r1", argv).is_chat_turn is True


# ── the refusals the new keys bring ──────────────────────────────────────────


@pytest.mark.parametrize("override", [{"provider": "anthropic"}, {"model": "opus"}])
def test_use_agent_default_with_an_override_is_refused_at_the_door(override):
    """The argv handler raises ``ValueError`` for this deep inside the turn
    (``persona_commands._requested_chat_model_override``), which over the method
    lane would be an accepted turn that dies as a handler failure. R-C8 moves
    the same rule to the boundary, where a client gets ``ERR_INVALID_PARAMS``
    and a reason it can branch on."""

    params = {
        "turn_request_id": "k1",
        "persona_id": "neko",
        "message": "hi",
        "use_agent_default": True,
    }
    params.update(override)
    with pytest.raises(ChatTurnInvalid) as caught:
        normalize_chat_message(params)
    assert caught.value.reason == "model_override_conflict"

    outcome = perform_chat_turn(
        params, verb=CHAT_MESSAGE_METHOD, spawn=lambda *_: None
    )
    assert outcome.result is None
    assert outcome.refusal.code == serve_rpc.ERR_INVALID_PARAMS
    assert outcome.refusal.data["reason"] == "model_override_conflict"


@pytest.mark.parametrize(
    "params,reason",
    [
        ({"workspace_name": 7}, "workspace_name_invalid"),
        ({"workspace_name": "w" * 121}, "workspace_name_invalid"),
        ({"provider": ["anthropic"]}, "provider_invalid"),
        ({"model": 3}, "model_invalid"),
        ({"use_agent_default": "yes"}, "use_agent_default_invalid"),
        ({"clarify_token": "c" * 241}, "clarify_token_invalid"),
        ({"intent_hint": 1}, "intent_hint_invalid"),
        ({"surface_prompt": {"a": 1}}, "surface_prompt_invalid"),
    ],
)
def test_a_malformed_new_key_is_refused_with_a_machine_readable_reason(params, reason):
    body = {"turn_request_id": "k1", "persona_id": "neko", "message": "hi"}
    body.update(params)
    outcome = perform_chat_turn(body, verb=CHAT_MESSAGE_METHOD, spawn=lambda *_: None)
    assert outcome.result is None
    assert outcome.refusal.code == serve_rpc.ERR_INVALID_PARAMS
    assert outcome.refusal.data["reason"] == reason


def test_the_workspace_name_cap_matches_the_argv_handler_that_reads_it():
    """120, because ``persona_commands`` reads it with
    ``safe_assignment_text(..., limit=120)``. A boundary that accepted more
    would hand the handler a string it silently truncates, and the operator
    would never learn which half arrived."""

    assert _message(workspace_name="w" * 120) == BASE_ARGV + [
        "--workspace-name",
        "w" * 120,
    ]


def test_an_unknown_param_is_still_ignored_rather_than_refused():
    """The contract R-C8 leaves alone, and the reason the ``params`` block had
    to exist: a runtime cannot refuse a client for a key it does not know, so
    the only honest way for a client to learn what is honoured is to be told."""

    assert _message(agents_file="X:/somewhere/AGENTS.md", nonsense=object()) == BASE_ARGV


# ── the advertisement ────────────────────────────────────────────────────────


class _RecordingParams(dict):
    """A params mapping that remembers every key the normaliser asked for.

    The one way to assert "the manifest advertises exactly what the code reads"
    without a second hand-maintained list to keep in step. Every read in
    ``chat_turn`` goes through ``params.get(...)``.
    """

    def __init__(self, values: dict):
        super().__init__(values)
        self.asked: list[str] = []

    def get(self, key, default=None):  # type: ignore[override]
        self.asked.append(key)
        return super().get(key, default)


#: Values that satisfy every validator, so the normaliser runs to completion and
#: every ``get`` is reached. ``use_agent_default`` is False on purpose: the
#: conflict rule would otherwise stop the walk.
_EVERY_MESSAGE_PARAM = {
    "turn_request_id": "k1",
    "persona_id": "neko",
    "message": "hi",
    "session_id": "root-1",
    "persona_instance_id": "personainst_neko_1",
    "workspace_id": "ws-1",
    "workspace_name": "Eternia",
    "title": "Field run",
    "new_session": True,
    "stream": True,
    "max_seconds": 90,
    "correlation_id": "corr-1",
    "provider": "anthropic",
    "model": "claude-opus-4",
    "use_agent_default": False,
    "clarify_token": "clr_9f2a",
    "surface_prompt": "be brief",
    "intent_hint": "plan",
}

_EVERY_STEER_PARAM = {
    "turn_request_id": "steer-1",
    "session_id": "root-1",
    "message": "stop",
    "persona_id": "neko",
    "persona_instance_id": "personainst_neko_1",
    "correlation_id": "corr-1",
}


@pytest.mark.parametrize(
    "method,normalizer,values",
    [
        (CHAT_MESSAGE_METHOD, normalize_chat_message, _EVERY_MESSAGE_PARAM),
        (CHAT_STEER_METHOD, normalize_chat_steer, _EVERY_STEER_PARAM),
    ],
)
def test_the_advertised_params_are_exactly_the_keys_the_normalizer_reads(
    method, normalizer, values
):
    recorder = _RecordingParams(values)
    normalizer(recorder)
    assert sorted(set(recorder.asked)) == list(manifest_params()[method])
    # …and the tuple the manifest is derived FROM is the same one, so a key
    # added to the normaliser without a tuple entry reds here rather than
    # shipping an advertisement that under-reports.
    assert list(CHAT_TURN_METHOD_PARAMS[method]) == list(manifest_params()[method])


def manifest_params() -> dict[str, list[str]]:
    return serve_rpc.manifest()["params"]


def test_the_manifest_carries_a_params_block_beside_methods_and_tiers():
    block = serve_rpc.manifest()["params"]
    assert set(block) == {CHAT_MESSAGE_METHOD, CHAT_STEER_METHOD}
    assert block[CHAT_MESSAGE_METHOD] == sorted(block[CHAT_MESSAGE_METHOD])
    assert block[CHAT_STEER_METHOD] == sorted(block[CHAT_STEER_METHOD])
    assert "workspace_name" in block[CHAT_MESSAGE_METHOD]
    assert "clarify_token" in block[CHAT_MESSAGE_METHOD]
    # JSON, not tuples: the block rides a greeting frame.
    assert all(isinstance(v, list) for v in block.values())


def test_the_params_block_is_additive_and_the_contract_integer_does_not_move():
    """The manifest's own rule, applied to itself: a key beside ``methods`` and
    ``tiers`` changes no existing method's request or result shape, so a client
    that ignores it keeps working — and a manifest WITHOUT the block reads as
    "the eleven keys" on the launcher side rather than as an error."""

    assert serve_rpc.manifest()["contract"] == 1
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    assert set(serve_rpc.manifest()) == {"contract", "methods", "tiers", "params"}
