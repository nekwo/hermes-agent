"""Public decision-type normalization.

S27 removed the simplified-contract PROJECTION half: ``DecisionProjection``,
``project_decision_for_execution``, the three ``legacy_*_decision_from_*``
shims, the ``collapsed_signal_for`` identity, the three config predicates
(``simplified_contract_enabled`` / ``expose_only_simplified_actions`` /
``keep_internal_state_machine``), and the ``decision_contract.parity`` recorder.
They projected an agent decision into an executable one for the deterministic
executor deleted in S5, and had no caller after it.

What remains is the wire-value normalizer that ``operator_channels`` and
``store`` read when rendering a decision type. The ``simplified_agent_contract``
config block is untouched — ``production_envelope`` still reports it.
"""

from __future__ import annotations

from .decision_schema import DecisionType


def public_decision_type(decision_type: DecisionType | str | None) -> DecisionType | None:
    if decision_type is None:
        return None
    try:
        resolved = decision_type if isinstance(decision_type, DecisionType) else DecisionType(str(decision_type))
    except Exception:
        return None
    return resolved


def public_decision_type_value(decision_type: DecisionType | str | None) -> str | None:
    resolved = public_decision_type(decision_type)
    return resolved.value if resolved is not None else None
