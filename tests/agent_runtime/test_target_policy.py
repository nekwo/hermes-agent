"""Pure decision-table tests for the ambiguous-target policy.

``agent_runtime.target_policy`` is the single authority for the
``ambiguous_target`` refusal (a bare persona id that names a persona running
more than one live instance). It is pure/stdlib — no store, no I/O — so the
whole decision table is exercised here without any runtime plumbing. The
handler wiring (candidate enumeration, caller-pinned detection, operator-console
scope) is proven end-to-end in
``tests/hermes_cli/test_mission_chat_ambiguous_target.py``.
"""

from __future__ import annotations

from agent_runtime import target_policy
from agent_runtime.target_policy import TargetCandidate, evaluate_target


def _two_qa():
    return [
        TargetCandidate(instance_id="personainst_qa", display_name="QA Agent"),
        TargetCandidate(instance_id="personainst_qa_agent_2", display_name="QA Agent (2)"),
    ]


def test_bare_persona_two_live_instances_is_refused_with_both_candidates():
    decision = evaluate_target(
        persona_id="qa",
        candidates=_two_qa(),
        caller_pinned_instance=False,
    )
    assert decision.allowed is False
    assert decision.error_kind == target_policy.AMBIGUOUS_TARGET
    assert decision.persona_id == "qa"
    # Both instances are listed with handle + display_name so the caller can
    # retry against an exact @handle.
    assert [c.as_dict() for c in decision.candidates] == [
        {"persona_instance_id": "personainst_qa", "display_name": "QA Agent"},
        {"persona_instance_id": "personainst_qa_agent_2", "display_name": "QA Agent (2)"},
    ]
    # The reason text surfaces the addressable handles for the model to read.
    assert "@personainst_qa" in decision.reason
    assert "@personainst_qa_agent_2" in decision.reason


def test_single_live_instance_resolves_as_before():
    decision = evaluate_target(
        persona_id="qa",
        candidates=[TargetCandidate(instance_id="personainst_qa", display_name="QA Agent")],
        caller_pinned_instance=False,
    )
    assert decision.allowed is True
    assert decision.error_kind is None
    assert decision.candidates == ()


def test_no_live_instances_resolves_as_before():
    decision = evaluate_target(persona_id="qa", candidates=[], caller_pinned_instance=False)
    assert decision.allowed is True


def test_caller_pinned_instance_is_never_refused_even_with_many_instances():
    # An explicit @personainst_* target / persona_instance_id / caller-chosen
    # session collapses to caller_pinned_instance=True at the handler; the policy
    # must never refuse it, however many siblings exist.
    decision = evaluate_target(
        persona_id="qa",
        candidates=_two_qa(),
        caller_pinned_instance=True,
    )
    assert decision.allowed is True
    assert decision.candidates == ()


def test_profile_target_is_out_of_scope_and_not_refused():
    decision = evaluate_target(
        persona_id="profile:alice",
        candidates=[
            TargetCandidate(instance_id="personainst_profile_alice", display_name="Alice"),
            TargetCandidate(instance_id="personainst_alice_agent_2", display_name="Alice (2)"),
        ],
        caller_pinned_instance=False,
        is_profile_target=True,
    )
    assert decision.allowed is True


def test_relay_chain_is_echoed_into_the_refusal_for_provenance_parity():
    decision = evaluate_target(
        persona_id="qa",
        candidates=_two_qa(),
        caller_pinned_instance=False,
        relay_chain=("neko_supervisor", "qa"),
    )
    assert decision.allowed is False
    # Mirrors relay_depth_limit / relay_cycle / relay_budget_exhausted, which all
    # carry the relay chain on the typed refusal.
    assert decision.chain == ("neko_supervisor", "qa")


def test_relay_chain_is_echoed_on_the_allowed_decision_too():
    decision = evaluate_target(
        persona_id="qa",
        candidates=[TargetCandidate(instance_id="personainst_qa", display_name="QA Agent")],
        caller_pinned_instance=False,
        relay_chain=("neko_supervisor", "qa"),
    )
    assert decision.allowed is True
    assert decision.chain == ("neko_supervisor", "qa")
