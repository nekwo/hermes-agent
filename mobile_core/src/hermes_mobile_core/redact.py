"""Central redaction for every error and diagnostic path."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "proxy-authorization",
    "api-key",
    "apikey",
    "api_key",
    "x-api-key",
    "cookie",
    "set-cookie",
    "access-token",
    "access_token",
    "refresh-token",
    "refresh_token",
}
_SIGNED_QUERY_MARKERS = (
    "signature",
    "credential",
    "security-token",
    "access_token",
    "api_key",
    "apikey",
    "token",
    "sig",
    "key",
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key|cookie|set-cookie)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_URL_RE = re.compile(r"https?://[^\s\]\[<>{}\"']+")


def _sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("_", "-")
    return normalized in {item.replace("_", "-") for item in _SENSITIVE_KEYS}


def redact_url(url: str) -> str:
    """Redact credentials and signed query values while preserving URL shape."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.scheme or not parts.netloc:
        return url
    hostname = parts.hostname or ""
    if parts.port:
        hostname = f"{hostname}:{parts.port}"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        sensitive = lower.startswith("x-amz-") or any(
            marker in lower for marker in _SIGNED_QUERY_MARKERS
        )
        query.append((key, REDACTED if sensitive else value))
    return urlunsplit((parts.scheme, hostname, parts.path, urlencode(query), parts.fragment))


def redact_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", str(value))
    text = _HEADER_RE.sub(lambda match: f"{match.group(1)}: {REDACTED}", text)
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def redact(value: Any) -> Any:
    """Return a JSON-safe, recursively redacted copy of *value*."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))
