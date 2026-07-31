"""S37 retires the two packet contracts left writerless by S36."""

from __future__ import annotations

from agent_runtime.decision_contract_registry import event_catalog


def test_writerless_packet_duplicate_and_normalized_contracts_are_gone():
    catalog = event_catalog()
    assert "packet.duplicate" not in catalog
    assert "packet.normalized" not in catalog
