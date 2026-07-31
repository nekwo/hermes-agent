"""S23 removes the two packets.py symbols with no reference of any kind.

``UNSUPPORTED_HANDOFF_MODES`` and ``_reject_unknown_packet_keys`` are the only
names in ``agent_runtime/packets.py`` that nothing reads -- not production, not
the module itself, not a test. Both are superseded in place:

* ``HANDOFF_MODES`` already contains ``parallel_specialists`` and
  ``split_child_missions``; ``_validate_handoff_packet`` validates against that
  set alone, so the "unsupported" subset never gated anything.
* ``_reject_unknown_packet_keys`` is the strict-raise predecessor of
  ``_normalize_unknown_packet_metadata``, which every validator calls instead
  (accounting unknown keys rather than rejecting the packet).

The validation and historical-read half stays. S36 later retires the writerless
emit half under a separate explicit ruling; the last test reflects that split.
"""

from __future__ import annotations

from agent_runtime import packets


def test_the_unreferenced_packet_symbols_are_gone():
    assert not hasattr(packets, "UNSUPPORTED_HANDOFF_MODES")
    assert not hasattr(packets, "_reject_unknown_packet_keys")


def test_the_handoff_mode_vocabulary_is_unchanged():
    # The removed subset was a strict subset of HANDOFF_MODES; dropping it must
    # not shrink what a handoff packet may declare.
    assert {"parallel_specialists", "split_child_missions"}.issubset(packets.HANDOFF_MODES)


def test_unknown_packet_keys_are_still_accounted_not_silently_dropped():
    from agent_runtime.decision_schema import AgentDecision, DecisionType

    decision = AgentDecision(
        type=DecisionType.HAND_OFF,
        summary="handoff",
        rationale="Unknown keys must be accounted, not rejected.",
        payload={
            "handoff_packet": {
                "packet_version": 1,
                "packet_kind": "implementation_handoff",
                "mission_phase": "implementation",
                "handoff_mode": "single_specialist",
                "target_owner": "dev",
                "target_repo": "EterniaLauncher",
                "proof_gate": {
                    "required": False,
                    "required_proof_types": ["test_run"],
                    "minimum_status": "passed",
                    "visual_required": False,
                },
                "totally_unknown_key": "value",
            }
        },
    )

    packets.validate_decision_packets(decision)
    packet = decision.payload["handoff_packet"]

    # The normalizer keeps the packet valid and RECORDS the drop -- the strict
    # rejecter that would have raised here is what S23 removed.
    assert "totally_unknown_key" not in packet
    assert "dropped_fields" in packet["_normalization"]


def test_the_live_packet_validation_and_history_surface_survives():
    """S36 cuts emission while preserving decision validation/history reads."""

    for name in (
        "validate_decision_packets",
        "latest_packet",
        "latest_packets_for_task",
        "iter_packet_payloads",
        "content_hash",
        "adapt_eternia_backend_manage_py_command",
    ):
        assert callable(getattr(packets, name)), name
