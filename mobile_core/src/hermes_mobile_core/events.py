"""Ordered, JSON-safe event emission for a single provider turn."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from .redact import redact

SCHEMA_VERSION = 1
TERMINAL_KINDS = frozenset({"turn.completed", "turn.cancelled", "turn.failed"})


class EventEmitter:
    """Attach the common envelope and enforce one terminal event."""

    def __init__(
        self,
        *,
        request_id: str,
        provider: str,
        model: str,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self.request_id = request_id
        self.provider = provider
        self.model = model
        self._emit = emit
        self._seq = 0
        self._terminal = False
        self._lock = Lock()

    @property
    def terminal(self) -> bool:
        with self._lock:
            return self._terminal

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._seq

    def send(self, kind: str, payload: Mapping[str, Any] | None = None) -> bool:
        with self._lock:
            if self._terminal:
                return False
            terminal = kind in TERMINAL_KINDS
            event = {
                "schema_version": SCHEMA_VERSION,
                "request_id": self.request_id,
                "seq": self._seq,
                "provider": self.provider,
                "model": self.model,
                "kind": kind,
                "payload": redact(dict(payload or {})),
            }
            self._seq += 1
            if terminal:
                self._terminal = True
        self._emit(event)
        return True
