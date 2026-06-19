from __future__ import annotations

from typing import Any

from .decision_contract_registry import payload_contract as _registry_payload_contract
from .decision_contract_registry import validate_payload_keys
from .decision_schema import DecisionType


def payload_contract(decision_type: DecisionType | str) -> dict[str, Any]:
    """Compatibility facade for the canonical decision contract registry."""

    return _registry_payload_contract(decision_type)
