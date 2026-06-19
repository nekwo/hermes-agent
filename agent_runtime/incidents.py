from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .decision_schema import DecisionPayloadInvalid
from .profile_runner import RunBudgetExceeded

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


@dataclass(frozen=True, slots=True)
class IncidentClassification:
    kind: str
    summary: str


def classify_exception(exc: Exception) -> IncidentClassification:
    if isinstance(exc, RunBudgetExceeded):
        return IncidentClassification(kind=RUN_BUDGET_EXCEEDED, summary=_safe_budget_summary(str(exc)))
    if isinstance(exc, DecisionPayloadInvalid):
        return IncidentClassification(kind=MODEL_INVALID_OUTPUT, summary=_safe_exception_summary(str(exc)))
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return IncidentClassification(kind=RUNTIME_DEPENDENCY_MISSING, summary=_safe_exception_summary(str(exc)))
    text = str(exc).lower()
    class_name = type(exc).__name__.lower()
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code == 429 or "rate limit" in text or "quota" in text:
        return IncidentClassification(kind=PROVIDER_RATE_LIMIT, summary=_safe_exception_summary(str(exc)))
    if "auth" in class_name or "authentication" in text or "unauthorized" in text or "credential" in text or "token_expired" in text or "token expired" in text or "http 401" in text or "http 403" in text or status_code in {401, 403}:
        return IncidentClassification(kind=PROVIDER_AUTH_FAILURE, summary=_safe_exception_summary(str(exc)))
    if "tool" in class_name and ("denied" in text or "policy" in text):
        return IncidentClassification(kind=TOOL_POLICY_VIOLATION, summary=_safe_exception_summary(str(exc)))
    if "request_test_run could not resolve" in text or "affected repo workdir" in text:
        return IncidentClassification(kind=HARNESS_ACTION_FAILURE, summary=_safe_exception_summary(str(exc)))
    return IncidentClassification(kind=PROVIDER_FAILURE, summary=_safe_exception_summary(str(exc)))


def _safe_budget_summary(text: str) -> str:
    allowed = []
    import re

    for key in ("wall_seconds", "api_calls", "total_tokens", "iterations"):
        match = re.search(rf"{key}=([0-9]+(?:\.[0-9]+)?(?:/[0-9]+)?)", text)
        if match:
            allowed.append(f"{key}={match.group(1)}")
    if allowed:
        return "live run budget exceeded: " + ", ".join(allowed)
    return "live run budget exceeded"


def _safe_exception_summary(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "runtime error"
    normalized = raw.replace("\\", "/")
    path_match = re.search(r"([A-Za-z]:/[^ \n\r\t]+|/(?:Users|home|mnt|opt|var|tmp|Volumes)/[^ \n\r\t]+)", normalized)
    if path_match:
        name = Path(path_match.group(1)).name
        return f"runtime artifact not found: {name}" if name else "runtime artifact not found"
    redacted = re.sub(r"(?i)(api[_-]?key|authorization|cookie|bearer|password|credential|token)[^ \n\r\t]*", "<redacted>", raw)
    if len(redacted) > 240:
        redacted = redacted[:237] + "..."
    return redacted
