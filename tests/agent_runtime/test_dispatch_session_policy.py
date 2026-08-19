"""The dispatch thread-target decision — one authority, unit-testable.

An ``agent_chat_send`` that names no thread used to continue the target's
durable default session forever, so a mission lead's ten unrelated dispatches
piled into one thread that was re-fed to the provider every turn. The decision
is now policy-backed and TRI-STATE: ``new_session`` True / False / unset are
three different answers, and only "unset" defers to config.

These tests pin the decision table itself (pure, no config/store/IO), because
the alternative — proving each row through a handler turn — is slow and hides
the table behind fixtures.
"""

from __future__ import annotations

import inspect

import pytest

from agent_runtime.dispatch_session_policy import (
    DEFAULT_DISPATCH_SESSION_POLICY,
    DISPATCH_SESSION_POLICIES,
    NEW_PER_DISPATCH,
    REASON_CLARIFY_TOKEN,
    REASON_EXPLICIT_NEW_SESSION,
    REASON_EXPLICIT_SESSION_ID,
    REASON_POLICY_NEW_PER_DISPATCH,
    REASON_POLICY_STICKY,
    REASON_STICKY_DEFAULT,
    STICKY,
    coerce_optional_flag,
    derive_dispatch_title,
    normalize_dispatch_session_policy,
    resolve_dispatch_session_decision,
    session_established_payload,
    superseded_session_id,
)


# ── the default is the whole point ──────────────────────────────────────────


def test_dispatch_defaults_to_a_fresh_thread_per_task():
    assert DEFAULT_DISPATCH_SESSION_POLICY == NEW_PER_DISPATCH
    decision = resolve_dispatch_session_decision(policy=NEW_PER_DISPATCH)
    assert decision.mint is True
    assert decision.reason == REASON_POLICY_NEW_PER_DISPATCH
    assert decision.explicit is False


def test_sticky_policy_restores_the_durable_pair_thread_deployment_wide():
    decision = resolve_dispatch_session_decision(policy=STICKY)
    assert decision.mint is False
    assert decision.reason == REASON_POLICY_STICKY


# ── precedence: explicit caller intent always wins ──────────────────────────


@pytest.mark.parametrize("policy", DISPATCH_SESSION_POLICIES)
def test_explicit_session_id_wins_under_every_policy(policy):
    decision = resolve_dispatch_session_decision(
        session_id="persona_chat_personainst_qa_abcdef123456", policy=policy
    )
    assert decision.mint is False
    assert decision.reason == REASON_EXPLICIT_SESSION_ID
    assert decision.explicit is True


@pytest.mark.parametrize("policy", DISPATCH_SESSION_POLICIES)
def test_explicit_new_session_true_wins_under_every_policy(policy):
    decision = resolve_dispatch_session_decision(new_session=True, policy=policy)
    assert decision.mint is True
    assert decision.reason == REASON_EXPLICIT_NEW_SESSION


@pytest.mark.parametrize("policy", DISPATCH_SESSION_POLICIES)
def test_explicit_new_session_false_continues_under_every_policy(policy):
    # The CLI/serve operator console arrives here (argparse store_true → False).
    # Its durable thread must survive the new default untouched.
    decision = resolve_dispatch_session_decision(new_session=False, policy=policy)
    assert decision.mint is False
    assert decision.reason == REASON_STICKY_DEFAULT
    assert decision.explicit is True


def test_an_explicit_caller_never_loads_the_configured_policy(monkeypatch):
    # `mission_chat_dispatch_session_policy()` parses the ROOT config.yaml
    # UNCACHED. The explicit lanes outrank it, and the explicit-session lane in
    # particular runs this resolver on EVERY mission-chat turn purely to name a
    # reason — so consulting config there was a per-turn YAML parse for a value
    # nothing reads. Only "the caller stated nothing" may reach it.
    import agent_runtime.config as runtime_config_module

    loads: list[int] = []

    def _counted(cfg=None):
        loads.append(1)
        return STICKY

    monkeypatch.setattr(
        runtime_config_module, "mission_chat_dispatch_session_policy", _counted
    )

    for stated in (
        dict(session_id="persona_chat_personainst_qa_abcdef123456"),
        dict(new_session=True),
        dict(new_session=False),
    ):
        decision = resolve_dispatch_session_decision(**stated)
        assert loads == [], f"config consulted for an explicit caller: {stated}"
        # Nothing was consulted, so nothing is claimed about deployment policy.
        assert decision.policy is None

    assert resolve_dispatch_session_decision().reason == REASON_POLICY_STICKY
    assert loads == [1], "the unset lane is the only one that may load config"


def test_session_id_beats_a_contradictory_new_session_flag():
    # The tool refuses this combination up front; the CLI documents it as
    # "ignored when --session-id is given". Either way ONE resolver answers,
    # and it answers with the named thread rather than minting a stray one.
    decision = resolve_dispatch_session_decision(
        session_id="persona_chat_personainst_qa_abcdef123456",
        new_session=True,
        policy=NEW_PER_DISPATCH,
    )
    assert decision.reason == REASON_EXPLICIT_SESSION_ID
    assert decision.mint is False


# ── rung 0: a resolved clarify ticket outranks everything ───────────────────


_CLARIFY_SESSION = "persona_chat_personainst_dev_cccccccccccc"


@pytest.mark.parametrize("policy", DISPATCH_SESSION_POLICIES)
@pytest.mark.parametrize(
    "stated",
    [
        {},
        {"session_id": "persona_chat_personainst_dev_aaaaaaaaaaaa"},
        {"new_session": True},
        {"new_session": False},
        {
            "session_id": "persona_chat_personainst_dev_aaaaaaaaaaaa",
            "new_session": False,
        },
    ],
)
def test_clarify_token_session_beats_every_other_input(policy, stated):
    # The whole point: an agent answering a clarifying question is the caller
    # LEAST likely to get its thread arguments right, so the resolved ticket
    # outranks all of them — including a session_id naming a different thread.
    # Refusing that conflict would defeat the design; the handler reports the
    # override instead (clarify_binding.overrode_session_id).
    decision = resolve_dispatch_session_decision(
        clarify_session_id=_CLARIFY_SESSION, policy=policy, **stated
    )
    assert decision.mint is False
    assert decision.reason == REASON_CLARIFY_TOKEN
    assert decision.explicit is True


def test_clarify_reason_is_the_one_the_handler_reads():
    # This used to assert `REASON_CLARIFY_TOKEN in DISPATCH_SESSION_REASONS` —
    # a set literally built from that constant, so it could not fail. The
    # question worth asking is whether the value a decision carries is the one
    # a reader downstream branches on, which is a fact about the handler.
    from agent_runtime import dispatch_session_policy

    source = inspect.getsource(dispatch_session_policy)
    assert f'"{REASON_CLARIFY_TOKEN}"' in source


def test_an_unresolved_clarify_token_leaves_precedence_untouched():
    # The handler hands in None when a token was pruned or never presented.
    # Degrade, do not refuse: normal precedence answers exactly as before.
    assert (
        resolve_dispatch_session_decision(
            clarify_session_id=None, policy=NEW_PER_DISPATCH
        ).reason
        == REASON_POLICY_NEW_PER_DISPATCH
    )
    assert (
        resolve_dispatch_session_decision(
            clarify_session_id="   ",
            session_id="persona_chat_personainst_dev_aaaaaaaaaaaa",
        ).reason
        == REASON_EXPLICIT_SESSION_ID
    )


def test_clarify_binding_never_loads_the_configured_policy(monkeypatch):
    # Same hot-path rule as every other explicit lane: `clarify_session_id` is
    # stated intent, and `mission_chat_dispatch_session_policy()` parses the root
    # config.yaml UNCACHED.
    import agent_runtime.config as runtime_config_module

    loads: list[int] = []

    def _counted(cfg=None):
        loads.append(1)
        return STICKY

    monkeypatch.setattr(
        runtime_config_module, "mission_chat_dispatch_session_policy", _counted
    )

    decision = resolve_dispatch_session_decision(clarify_session_id=_CLARIFY_SESSION)
    assert loads == []
    assert decision.policy is None


# ── tri-state coercion ──────────────────────────────────────────────────────


def test_unset_and_false_are_different_answers():
    assert coerce_optional_flag(None) is None
    assert coerce_optional_flag("") is None
    assert coerce_optional_flag(False) is False
    assert coerce_optional_flag(True) is True


def test_string_booleans_are_not_inverted():
    # bool("false") is True — a provider that serializes booleans as text would
    # have flipped the caller's intent straight into a fresh thread.
    assert coerce_optional_flag("false") is False
    assert coerce_optional_flag("False") is False
    assert coerce_optional_flag("true") is True
    assert coerce_optional_flag("no") is False


def test_unrecognized_flag_junk_degrades_to_unset():
    assert coerce_optional_flag("maybe") is None
    assert resolve_dispatch_session_decision(
        new_session="maybe", policy=NEW_PER_DISPATCH
    ).reason == REASON_POLICY_NEW_PER_DISPATCH


# ── config token normalization ──────────────────────────────────────────────


def test_policy_token_normalization_accepts_the_vocabulary():
    assert normalize_dispatch_session_policy("sticky") == STICKY
    assert normalize_dispatch_session_policy(" NEW-PER-DISPATCH ") == NEW_PER_DISPATCH


def test_malformed_policy_degrades_instead_of_failing_the_turn():
    assert normalize_dispatch_session_policy("per_task") == DEFAULT_DISPATCH_SESSION_POLICY
    assert normalize_dispatch_session_policy(None) == DEFAULT_DISPATCH_SESSION_POLICY
    assert (
        resolve_dispatch_session_decision(policy="per_task").reason
        == REASON_POLICY_NEW_PER_DISPATCH
    )


# ── the envelope block ──────────────────────────────────────────────────────


def test_envelope_reports_the_outcome_and_the_reason_separately():
    # A sticky send to a teammate who has never chatted DOES mint — the reason
    # stays honest about the decision that was made.
    decision = resolve_dispatch_session_decision(new_session=False, policy=STICKY)
    payload = session_established_payload(decision, fresh=True, predecessor_session_id=None)
    assert payload == {
        "fresh": True,
        "reason": REASON_STICKY_DEFAULT,
        "predecessor_session_id": None,
    }


def test_predecessor_is_carried_only_by_a_fresh_thread():
    decision = resolve_dispatch_session_decision(policy=NEW_PER_DISPATCH)
    fresh = session_established_payload(
        decision, fresh=True, predecessor_session_id="persona_chat_personainst_qa_aaaaaaaaaaaa"
    )
    assert fresh["predecessor_session_id"] == "persona_chat_personainst_qa_aaaaaaaaaaaa"
    # A continuation has no predecessor — it IS the predecessor.
    continued = session_established_payload(
        resolve_dispatch_session_decision(new_session=False),
        fresh=False,
        predecessor_session_id="persona_chat_personainst_qa_aaaaaaaaaaaa",
    )
    assert continued["predecessor_session_id"] is None


def test_a_session_is_never_its_own_predecessor():
    # The replay lane: a retried dispatch resolves the same idempotency-keyed
    # mint receipt, so by the time the envelope is built the default-thread
    # pointer read a moment earlier IS this thread. Recording that would write a
    # lineage loop ("A superseded A") into both the envelope and the session
    # meta's `_dispatched_from`, which a reader following the chain never leaves.
    same = "persona_chat_personainst_dev_aaaaaaaaaaaa"
    older = "persona_chat_personainst_dev_bbbbbbbbbbbb"
    assert superseded_session_id(same, established=same) is None
    assert superseded_session_id(older, established=same) == older
    # Nothing to supersede reads as nothing, in every empty shape.
    assert superseded_session_id(None, established=same) is None
    assert superseded_session_id("  ", established=same) is None
    # No established session yet (a first-ever mint) still reports the truth.
    assert superseded_session_id(older, established=None) == older


# ── titles make task-scoped threads navigable ───────────────────────────────


def test_short_message_becomes_the_whole_title():
    assert derive_dispatch_title("Triage the flaky login test") == "Triage the flaky login test"


def test_long_message_is_cut_on_a_word_boundary():
    message = (
        "Investigate why the launcher's Mission Control drawer takes 4 seconds "
        "to open on a cold start and report the dominant cost"
    )
    title = derive_dispatch_title(message)
    assert len(title) <= 48
    assert message.startswith(title)
    # Words, not a guillotined token.
    assert not title.endswith(" ")
    assert message[len(title)] in " " or title == message[:48]


def test_empty_message_has_no_derived_title():
    # The caller falls back to the durable "<persona> chat" name.
    assert derive_dispatch_title("") is None
    assert derive_dispatch_title("   \n  ") is None
    assert derive_dispatch_title(None) is None


def test_whitespace_is_collapsed_so_a_pasted_brief_still_titles():
    assert derive_dispatch_title("Fix   the\n\ncrash") == "Fix the crash"


def test_unbroken_run_still_yields_a_bounded_title():
    title = derive_dispatch_title("x" * 200)
    assert title and len(title) <= 48
