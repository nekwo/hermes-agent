from __future__ import annotations

from typing import Any


STRICT = "strict"
OBSERVE = "observe"
ALLOWED = {STRICT, OBSERVE}


def normalize_redaction_mode(value: Any, *, fallback: str = STRICT) -> str:
    text = str(value or "").strip().lower()
    if text in ALLOWED:
        return text
    return fallback if fallback in ALLOWED else STRICT


def redaction_mode(config: Any | None = None) -> str:
    if config is None:
        try:
            from .config import load_root_runtime_config

            config = load_root_runtime_config()
        except Exception:
            return STRICT
    return normalize_redaction_mode(getattr(config, "redaction_mode", STRICT))


def redaction_observe_enabled(config: Any | None = None) -> bool:
    return redaction_mode(config) == OBSERVE
