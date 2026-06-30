from agent_runtime.incidents import classify_exception
from agent_runtime.errors import NotFound


class AuthLikeError(Exception):
    pass


class RateLimitLikeError(Exception):
    status_code = 429


def test_classify_exception_maps_auth_like_errors():
    exc = AuthLikeError("authentication failed")

    classification = classify_exception(exc)

    assert classification.kind == "provider_auth_failure"


def test_classify_exception_maps_rate_limit_like_errors():
    exc = RateLimitLikeError("rate limit exceeded")

    classification = classify_exception(exc)

    assert classification.kind == "provider_rate_limit"


def test_classify_exception_maps_token_expired_text_to_auth_failure():
    exc = RuntimeError("HTTP 401: Provided authentication token is expired. code=token_expired")

    classification = classify_exception(exc)

    assert classification.kind == "provider_auth_failure"


def test_classify_exception_sanitizes_runtime_paths():
    exc = NotFound(r"X:\Eternia\.hermes\agent-runtime\incidents\previous_decision_parse_failed_missing_handoff_packet_proof_gate_required_proof_types.json")

    classification = classify_exception(exc)

    assert classification.kind == "provider_failure"
    assert classification.summary == "runtime artifact not found: previous_decision_parse_failed_missing_handoff_packet_proof_gate_required_proof_types.json"
    assert "X:" not in classification.summary
