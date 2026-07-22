"""Cooperative control for long-lived Harness requests.

The stdio serve pool executes ordinary CLI handlers unchanged. Most mutations
remain deliberately non-interruptible once running, but the read-only
``harness stream`` handler is infinite and must release its worker when its
consumer disconnects. Serve binds one cancellation event at dispatch; the
stream polls this tiny runtime-owned seam without importing the CLI layer.
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from collections.abc import Iterator


_request_cancel_event: contextvars.ContextVar[threading.Event | None] = (
    contextvars.ContextVar("harness_request_cancel_event", default=None)
)


@contextmanager
def request_cancel_scope(event: threading.Event) -> Iterator[None]:
    token = _request_cancel_event.set(event)
    try:
        yield
    finally:
        _request_cancel_event.reset(token)


def request_cancelled() -> bool:
    event = _request_cancel_event.get()
    return bool(event is not None and event.is_set())
