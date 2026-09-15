from __future__ import annotations

# S54: every import this module had went with ``classify_exception`` -- it was
# the only thing here that did any work. ``dataclass`` typed its return value,
# ``re``/``Path`` redacted the summary, and ``DecisionPayloadInvalid`` /
# ``RunBudgetExceeded`` were the exception classes it switched on. What remains
# is a pure vocabulary module: incident-kind constants and the CRITICAL set.
# A dead import is not free (the S41 rule) -- and the ``profile_runner`` one in
# particular kept a heavyweight import edge alive for a function nobody called.

RUNTIME_DEPENDENCY_MISSING = "runtime_dependency_missing"
INTERPRETER_MISMATCH = "interpreter_mismatch"
PROVIDER_FAILURE = "provider_failure"
PROVIDER_AUTH_FAILURE = "provider_auth_failure"
PROVIDER_RATE_LIMIT = "provider_rate_limit"
TOOL_POLICY_VIOLATION = "tool_policy_violation"
PRODUCT_VERIFICATION_FAILURE = "product_verification_failure"
MODEL_INVALID_OUTPUT = "model_invalid_output"
HARNESS_ACTION_FAILURE = "harness_action_failure"
RUN_BUDGET_EXCEEDED = "run_budget_exceeded"
CRITICAL = "critical"
SECURITY = "security"

CRITICAL_INCIDENT_KINDS = {
    RUNTIME_DEPENDENCY_MISSING,
    INTERPRETER_MISMATCH,
    PROVIDER_FAILURE,
    PROVIDER_AUTH_FAILURE,
    PROVIDER_RATE_LIMIT,
    TOOL_POLICY_VIOLATION,
    PRODUCT_VERIFICATION_FAILURE,
    HARNESS_ACTION_FAILURE,
    RUN_BUDGET_EXCEEDED,
    CRITICAL,
    SECURITY,
}


# S54 took ``IncidentClassification`` and the two redaction helpers
# ``_safe_budget_summary`` / ``_safe_exception_summary`` with
# ``classify_exception``: the dataclass was its return type and the helpers built
# the summary it carried. Nothing else referenced any of the three.
#
# KEPT deliberately: ``MODEL_INVALID_OUTPUT``. Nothing reads the NAME, so a
# reference count calls it dead -- but its VALUE is live, as a bare string
# literal in ``observability.py`` and in two tests. Cutting the constant would
# leave the live concept addressed only by a duplicated literal. That is a
# single-name-authority weakness to fix at the observability end, not a dead
# symbol to delete here; it was not on the operator's ruled list and is recorded
# in doc 19 instead.

# S54 removed ``classify_exception``. It classified exceptions for the retired
# run-loop incident router; ``IncidentStore.open`` is the live lane and takes an
# explicit kind.

