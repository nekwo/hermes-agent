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

import pytest

from agent_runtime.dispatch_session_policy import (
    DEFAULT_DISPATCH_SESSION_POLICY,
    DISPATCH_SESSION_POLICIES,
    NEW_PER_DISPATCH,
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
