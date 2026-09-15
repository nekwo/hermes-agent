"""Fork turn timing and dispatch receipts, shared by upstream turn phases."""
import logging
import time
from typing import Any, List, Optional
from hermes_constants import CONVERSATION_REQUEST_ASSEMBLED_STEP
logger = logging.getLogger(__name__)

def _emit_conversation_timing(
    agent: Any,
    step: str,
    started: float,
    *,
    status: str = "completed",
    **extra: Any,
) -> int:
    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    callback = getattr(agent, "status_callback", None)
    if callback is not None:
        try:
            callback(
                {
                    "type": "run.progress",
                    "phase": "timing",
                    "step": f"conversation_{step}",
                    "status": status,
                    "summary": f"Conversation {step.replace('_', ' ')} {status} in {duration_ms}ms.",
                    "duration_ms": duration_ms,
                    "timing_key": f"conversation_{step}_ms",
                    **extra,
                }
            )
        except Exception:
            logger.debug("conversation timing callback failed", exc_info=True)
    return duration_ms

def _emit_request_assembled_marker(agent: Any, **extra: Any) -> None:
    """Announce the dispatch instant: every byte hermes will send now exists.

    Not an :func:`_emit_conversation_timing` span — it names an INSTANT, not a
    duration, so it carries no ``duration_ms``/``timing_key`` (which also keeps
    it out of the profile-timing dict, whose collector only reads ``*_ms``
    keys). The mission-chat handler converts it into the ``request_assembled``
    phase mark (``agent_runtime/mission_chat_phases.py:mark_from_trace_payload``)
    that splits the turn record's "provider" span into hermes assembly vs
    genuine client-init + network + provider wait.

    Fired once per PHYSICAL dispatch attempt, right after the transport
    preflight (so a codex token refresh lands on the hermes side of the split)
    and right before the provider call. On a retry ladder the consumer's
    first-mark-wins keeps the first attempt's instant.
    """

    callback = getattr(agent, "status_callback", None)
    if callback is None:
        return
    try:
        callback(
            {
                "type": "run.progress",
                "phase": "timing",
                "step": CONVERSATION_REQUEST_ASSEMBLED_STEP,
                "status": "reached",
                "summary": "Provider request assembled; dispatching.",
                **extra,
            }
        )
    except Exception:
        logger.debug("request-assembled marker callback failed", exc_info=True)

def _format_ttfb_token(first_byte_s: Optional[float]) -> str:
    """Render the ``ttfb=`` token for the ``API call #N`` line, or nothing.

    Absent is not zero. ``None`` means no first-byte instant was observed for
    this response — a non-streaming call, or a stream whose first-delta
    callback never fired — and an unobserved measurement must vanish from the
    line rather than print ``ttfb=0.0s``, which reads as an instantaneous
    provider and is a lie no downstream reader can detect.
    """

    if first_byte_s is None:
        return ""
    return f" ttfb={first_byte_s:.1f}s"

def _first_delta_recorder(
    cell: List[Optional[float]],
    dispatch_started: float,
    on_first_delta: Any,
) -> Any:
    """Wrap ``on_first_delta`` so the first firing also times provider TTFB.

    ``dispatch_started`` and the instant taken here are both
    :func:`time.perf_counter` readings, so the difference is monotonic and
    cannot go negative or jump on a wall-clock correction.

    First firing wins: providers are free to invoke the delta callback once per
    token, and TTFB is the FIRST byte, not the latest one. The measurement is
    taken before the wrapped callback runs so spinner teardown and any
    thinking-callback work is never charged to the provider — and so the
    instant survives a callback that raises.
    """

    def _record() -> None:
        if cell[0] is None:
            cell[0] = time.perf_counter() - dispatch_started
        if on_first_delta is not None:
            on_first_delta()

    return _record
