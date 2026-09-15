"""Relay policy — the single authority for agent-relay depth/cycle/budget
(agent_runtime/relay_policy.py). Pure decision-table tests."""

import pytest

from agent_runtime import relay_policy
from agent_runtime.relay_policy import (
    DEFAULT_MAX_RELAY_DEPTH,
    MIN_RELAY_BUDGET_SECONDS,
    RELAY_SENDER_FINISH_REASON_PREFIX,
    RelaySender,
    build_relay_sender_marker,
    evaluate_relay,
    max_relay_depth,
    normalize_chain,
    parse_deadline_epoch,
    parse_relay_sender_marker,
    remaining_budget_seconds,
)


# ── normalize_chain / parse helpers ─────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ()),
        ("", ()),
        ("neko_supervisor", ("neko_supervisor",)),
        ("Neko_Supervisor, dev ,", ("neko_supervisor", "dev")),
        (["profile:alice", "NEKO_SUPERVISOR"], ("profile:alice", "neko_supervisor")),
        (("dev",), ("dev",)),
        (42, ()),
    ],
)
def test_normalize_chain_accepts_envelope_shapes(raw, expected):
    assert normalize_chain(raw) == expected


def test_parse_deadline_epoch():
    assert parse_deadline_epoch(None) is None
    assert parse_deadline_epoch("nope") is None
    assert parse_deadline_epoch(0) is None
    assert parse_deadline_epoch(-5) is None
    assert parse_deadline_epoch("1234.5") == 1234.5


def test_remaining_budget_seconds():
    assert remaining_budget_seconds(None) is None
    assert remaining_budget_seconds(1100.0, now=1000.0) == 100.0


# ── max depth resolution ────────────────────────────────────────────


def test_max_depth_default_and_env_override(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_CHAT_MAX_DEPTH", raising=False)
    assert max_relay_depth() == DEFAULT_MAX_RELAY_DEPTH
    monkeypatch.setenv("HERMES_AGENT_CHAT_MAX_DEPTH", "2")
    assert max_relay_depth() == 2
    monkeypatch.setenv("HERMES_AGENT_CHAT_MAX_DEPTH", "99")
    assert max_relay_depth() == 8  # clamped
    monkeypatch.setenv("HERMES_AGENT_CHAT_MAX_DEPTH", "0")
    assert max_relay_depth() == 1  # clamped
    monkeypatch.setenv("HERMES_AGENT_CHAT_MAX_DEPTH", "garbage")
    assert max_relay_depth() == DEFAULT_MAX_RELAY_DEPTH


# ── depth ───────────────────────────────────────────────────────────


def test_root_turn_with_empty_chain_is_never_depth_refused():
    decision = evaluate_relay(chain=(), target_persona_id="neko_supervisor", max_depth=1)
    assert decision.allowed
    assert decision.chain == ("neko_supervisor",)


def test_hops_below_the_cap_are_allowed():
    # chain includes the current speaker: ("neko",) = zero hops so far.
    decision = evaluate_relay(chain=("neko_supervisor",), target_persona_id="dev", max_depth=3)
    assert decision.allowed
    assert decision.chain == ("neko_supervisor", "dev")


def test_hop_at_the_cap_is_refused_with_typed_error():
    chain = ("profile:alice", "neko_supervisor", "dev", "backend_dev")  # 3 hops done
    decision = evaluate_relay(chain=chain, target_persona_id="qa", max_depth=3)
    assert not decision.allowed
    assert decision.error_kind == "relay_depth_limit"
    assert "profile:alice -> neko_supervisor -> dev -> backend_dev" in decision.reason
    assert decision.chain == chain


# ── cycles ──────────────────────────────────────────────────────────


def test_relay_back_to_a_chain_member_is_a_cycle():
    decision = evaluate_relay(
        chain=("profile:alice", "neko_supervisor"), target_persona_id="profile:alice"
    )
    assert not decision.allowed
    assert decision.error_kind == "relay_cycle"


def test_self_relay_is_a_cycle():
    decision = evaluate_relay(chain=("dev",), target_persona_id="dev")
    assert not decision.allowed
    assert decision.error_kind == "relay_cycle"


def test_cycle_check_is_case_insensitive():
    decision = evaluate_relay(chain=("Neko_Supervisor",), target_persona_id="NEKO_SUPERVISOR")
    assert not decision.allowed
    assert decision.error_kind == "relay_cycle"


# ── shared budget ───────────────────────────────────────────────────


def test_exhausted_deadline_is_refused():
    decision = evaluate_relay(
        chain=("neko_supervisor",),
        target_persona_id="dev",
        deadline_epoch=1000.0 + MIN_RELAY_BUDGET_SECONDS - 1,
        now=1000.0,
    )
    assert not decision.allowed
    assert decision.error_kind == "relay_budget_exhausted"


def test_sufficient_deadline_is_allowed():
    decision = evaluate_relay(
        chain=("neko_supervisor",),
        target_persona_id="dev",
        deadline_epoch=1000.0 + 120.0,
        now=1000.0,
    )
    assert decision.allowed


# ── ContextVar carriers exist with safe defaults ────────────────────


def test_context_carriers_default_empty():
    assert relay_policy.RELAY_CHAIN.get() == ()
    assert relay_policy.RELAY_DEADLINE.get() is None


# ── relayed-message sender marker (single build/parse authority) ─────


def test_build_marker_full_identity_round_trips():
    marker = build_relay_sender_marker("neko_supervisor", "personainst_neko")
    assert marker == f"{RELAY_SENDER_FINISH_REASON_PREFIX}neko_supervisor:personainst_neko"
    assert parse_relay_sender_marker(marker) == RelaySender(
        persona_id="neko_supervisor", instance_id="personainst_neko"
    )


def test_build_marker_persona_only_and_instance_only():
    persona_only = build_relay_sender_marker("dev", None)
    assert persona_only == f"{RELAY_SENDER_FINISH_REASON_PREFIX}dev:"
    assert parse_relay_sender_marker(persona_only) == RelaySender(persona_id="dev", instance_id=None)

    instance_only = build_relay_sender_marker(None, "personainst_qa_agent_2")
    assert instance_only == f"{RELAY_SENDER_FINISH_REASON_PREFIX}:personainst_qa_agent_2"
    assert parse_relay_sender_marker(instance_only) == RelaySender(
        persona_id=None, instance_id="personainst_qa_agent_2"
    )


def test_build_bare_marker_is_the_honest_unknown():
    bare = build_relay_sender_marker(None, None)
    assert bare == f"{RELAY_SENDER_FINISH_REASON_PREFIX}:"
    assert parse_relay_sender_marker(bare) == RelaySender(persona_id=None, instance_id=None)


def test_build_marker_strips_whitespace_segments():
    # Blank/whitespace inputs collapse to the empty segment (parsed back as None),
    # never a whitespace-only fabricated id.
    assert build_relay_sender_marker("  ", "\t") == f"{RELAY_SENDER_FINISH_REASON_PREFIX}:"
    assert parse_relay_sender_marker(build_relay_sender_marker("  neko ", " personainst_x ")) == (
        RelaySender(persona_id="neko", instance_id="personainst_x")
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "stop",
        "pre_trace_ack",
        "length",
        "relay_fromX",  # prefix must match exactly, colon included
        "prefixed relay_from:neko:inst",  # marker must be at the START
        42,
        object(),
    ],
)
def test_non_marker_values_parse_to_none(value):
    assert parse_relay_sender_marker(value) is None


def test_parse_is_defensive_against_extra_colons():
    # A stray third colon in the tail must never raise; the parse takes the first
    # two segments and drops the remainder (the frozen split(":", 2) contract).
    parsed = parse_relay_sender_marker(f"{RELAY_SENDER_FINISH_REASON_PREFIX}neko:personainst_x:extra")
    assert parsed == RelaySender(persona_id="neko", instance_id="personainst_x")
