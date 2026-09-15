"""Codex provider receipts retained independently of the upstream stream assembler."""
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict
logger = logging.getLogger(__name__)

def _emit_provider_timing(
    agent: Any,
    step: str,
    duration_ms: int,
    *,
    status: str = "completed",
    timing_values: Dict[str, int] | None = None,
    **extra: Any,
) -> None:
    callback = getattr(agent, "status_callback", None)
    if callback is None:
        return
    try:
        callback(
            {
                "type": "run.progress",
                "phase": "timing",
                "step": f"provider_{step}",
                "status": status,
                "summary": f"Provider {step.replace('_', ' ')} {status} in {duration_ms}ms.",
                "duration_ms": max(0, int(duration_ms)),
                "timing_key": f"provider_{step}_ms",
                "timing_values": timing_values or {},
                "provider": getattr(agent, "provider", None),
                "model": getattr(agent, "model", None),
                "api_mode": getattr(agent, "api_mode", None),
                **extra,
            }
        )
    except Exception:
        logger.debug("provider timing callback failed", exc_info=True)

def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))

def note_stream_event(stats, event, started):
    if stats is None:
        return
    for key in ("event_count", "text_delta_count", "reasoning_delta_count", "tool_call_event_count", "output_item_count"):
        stats.setdefault(key, 0)
    stats["event_count"] += 1
    stats.setdefault("first_event_ms", _elapsed_ms(started))
    kind = event.get("type", "") if isinstance(event, dict) else getattr(event, "type", "")
    kind = kind if isinstance(kind, str) else ""
    if "output_text.delta" in kind:
        stats["text_delta_count"] += 1
    if "function_call" in kind:
        stats["tool_call_event_count"] += 1
    if "reasoning" in kind and "delta" in kind:
        stats["reasoning_delta_count"] += 1
    if kind == "response.output_item.done":
        stats["output_item_count"] += 1
    if kind in {"response.completed", "response.incomplete", "response.failed"}:
        stats["terminal_event_type"] = kind
        stats["terminal_event_ms"] = _elapsed_ms(started)


def stream_timing_values(stats):
    values = {f"provider_stream_{key}": int(stats.get(key, 0) or 0) for key in (
        "event_count", "text_delta_count", "reasoning_delta_count", "tool_call_event_count",
        "output_item_count", "saw_terminal_count",
    )}
    if stats.get("terminal_event_ms") is not None:
        values["provider_stream_terminal_event_ms"] = int(stats["terminal_event_ms"])
    return values


@contextmanager
def measure_provider(agent, step, *, stats=None, **extra):
    started = time.perf_counter()
    try:
        yield
    except BaseException as exc:
        _emit_provider_timing(agent, step, _elapsed_ms(started), status="failed",
            timing_values=stream_timing_values(stats) if stats is not None else {},
            error_class=type(exc).__name__, **extra)
        raise
    else:
        if stats is not None and stats.get("first_event_ms") is not None:
            _emit_provider_timing(agent, "stream_first_event", int(stats["first_event_ms"]), **extra)
        _emit_provider_timing(agent, step, _elapsed_ms(started),
            timing_values=stream_timing_values(stats) if stats is not None else {},
            **({"terminal_event_type": stats.get("terminal_event_type")} if stats is not None else {}), **extra)
