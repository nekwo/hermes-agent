"""Fork construction timing and tool-schema cache receipts."""
import logging
import time
from typing import Any, Callable, Dict, Optional
logger = logging.getLogger(__name__)

def _tool_defs_cache_misses() -> Optional[int]:
    """This thread's cumulative tool-schema memo misses, or ``None``.

    ``None`` is the honest answer when ``model_tools`` cannot be consulted at
    all. The caller turns an unknown reading — at EITHER end of the window —
    into no receipt at all, rather than a hit it never observed.
    """

    try:
        from model_tools import tool_defs_cache_misses_this_thread

        return int(tool_defs_cache_misses_this_thread())
    except Exception:
        return None

def _emit_tool_defs_receipt(
    status_callback: Optional[Callable[[Dict[str, Any]], None]],
    *,
    started: float,
    misses_before: Optional[int],
    misses_after: Optional[int],
) -> None:
    """Name whether this construction BUILT the tool schemas or reused the memo.

    Emits exactly one of ``agent_init_tool_defs_build_ms`` (the memo missed —
    the registry walk, the schema filter and the ``check_fn`` sweep were paid)
    or ``agent_init_tool_defs_cached_ms`` (the memo hit). Both ride the existing
    ``*_ms`` profile-timing channel, so they reach the durable turn record
    through ``mission_chat_turns.safe_turn_profile_timing`` with no schema
    change and no new phase key.

    NEITHER key is emitted when the counter could not be read at either end.
    That absence is a third state — "unmeasured" — and collapsing it into
    "cached" would be exactly the fake-zero this plan's honesty contract exists
    to forbid.

    Deliberately NOT routed through ``_emit_init_timing``: that helper walks a
    rolling checkpoint, so emitting through it would silently redefine the
    existing ``agent_init_tool_setup_ms`` to mean "the rest of tool setup". A
    receipt must not move a measurement that already has readers.
    """

    if status_callback is None:
        return
    if misses_before is None or misses_after is None:
        return
    step = (
        "tool_defs_build" if misses_after > misses_before else "tool_defs_cached"
    )
    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    try:
        status_callback(
            {
                "type": "run.progress",
                "phase": "timing",
                "step": f"agent_init_{step}",
                "status": "completed",
                "summary": (
                    f"Agent init {step.replace('_', ' ')} completed in {duration_ms}ms."
                ),
                "duration_ms": duration_ms,
                "timing_key": f"agent_init_{step}_ms",
            }
        )
    except Exception:
        logger.debug("tool-defs receipt callback failed", exc_info=True)

def init_timing_callback(status_callback):
    _init_started = time.perf_counter()
    _init_last_checkpoint = _init_started
    def emit(step: str) -> None:
        nonlocal _init_last_checkpoint
        if status_callback is None:
            _init_last_checkpoint = time.perf_counter()
            return
        current = time.perf_counter()
        duration_ms = max(0, int((current - _init_last_checkpoint) * 1000))
        elapsed_ms = max(0, int((current - _init_started) * 1000))
        _init_last_checkpoint = current
        try:
            status_callback(
                {
                    "type": "run.progress",
                    "phase": "timing",
                    "step": f"agent_init_{step}",
                    "status": "completed",
                    "summary": f"Agent init {step.replace('_', ' ').title()} completed in {duration_ms}ms.",
                    "duration_ms": duration_ms,
                    "elapsed_ms": elapsed_ms,
                    "timing_key": f"agent_init_{step}_ms",
                }
            )
        except Exception:
            pass
    return emit
