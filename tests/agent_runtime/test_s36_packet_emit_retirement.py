"""S36 retires packet emission; S58 removes the final orphaned history read."""

from __future__ import annotations

from agent_runtime import packets






def test_validation_survives_after_the_historical_accessors_retire():
    for name in (
        "validate_decision_packets",
        "iter_packet_payloads",
    ):
        assert callable(getattr(packets, name)), name
    assert packets.HANDOFF_PACKET_KEYS
    assert packets.HANDOFF_MODES
