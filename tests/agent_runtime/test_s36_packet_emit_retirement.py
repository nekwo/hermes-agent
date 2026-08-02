"""S36 retires packet emission; S58 removes the final orphaned history read."""

from __future__ import annotations

from agent_runtime import packets
from agent_runtime.decision_contract_registry import event_catalog


def test_the_writerless_packet_emit_api_is_gone():
    for name in ("make_packet", "record_packet", "record_decision_packets"):
        assert not hasattr(packets, name), name


def test_packet_recorded_is_no_longer_an_advertised_write_contract():
    assert "packet.recorded" not in event_catalog()


def test_validation_survives_after_the_historical_accessors_retire():
    for name in (
        "validate_decision_packets",
        "iter_packet_payloads",
    ):
        assert callable(getattr(packets, name)), name
    assert not hasattr(packets, "latest_packet")
    assert not hasattr(packets, "latest_packets_for_task")
    assert packets.HANDOFF_PACKET_KEYS
    assert packets.HANDOFF_MODES
