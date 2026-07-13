"""Relay policy for agent-to-agent chat orchestration.

Single authority for relay depth / cycle / budget decisions on the
``agent_chat_send`` lane. The mission-chat handler evaluates this policy at
the canonical persona chokepoint (AFTER persona-id canonicalization), so
every entry point — in-process tool relay, CLI, serve transport — gets the
same answer. ``agent_chat_send`` only *carries* the envelope; it does not
re-decide depth or cycles.

Provenance model: the relay chain is EXPLICIT request-envelope data
(``relay_chain`` / ``relay_deadline_epoch`` on the mission-chat request), so
it survives process boundaries (serve handler subprocesses, future remote
transports). The ContextVars below are only the in-process ferry from a
running turn into its tool worker threads (the concurrent tool executor runs
workers under ``contextvars.copy_context``); the handler seeds them from the
envelope at turn start and resets them when the turn settles.

Chain semantics: entries are canonical persona ids, outermost speaker first,
INCLUDING the persona whose turn is currently running. Hops-so-far is
therefore ``len(chain) - 1``; ``max_depth`` caps chained relay hops
(default 3: operator -> A -> B -> C -> D refuses the D->E hop).

Pure and stdlib-only: no harness imports, no I/O beyond ``os.environ`` /
``time.time`` defaults — the decision table is unit-testable in isolation.
"""

from __future__ import annotations

import os
import time
from contextvars import ContextVar
from dataclasses import dataclass

DEFAULT_MAX_RELAY_DEPTH = 3
_MAX_DEPTH_FLOOR = 1
_MAX_DEPTH_CEILING = 8

# A relay hop below this wall budget cannot complete a model turn; refuse it
# honestly instead of letting the target turn start and die mid-flight.
MIN_RELAY_BUDGET_SECONDS = 10.0

# In-process carriers (see module docstring — the envelope is the truth).
RELAY_CHAIN: ContextVar[tuple[str, ...]] = ContextVar("hermes_relay_chain", default=())
RELAY_DEADLINE: ContextVar[float | None] = ContextVar("hermes_relay_deadline", default=None)


def max_relay_depth() -> int:
    raw = (os.environ.get("HERMES_AGENT_CHAT_MAX_DEPTH") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_RELAY_DEPTH
    except ValueError:
        return DEFAULT_MAX_RELAY_DEPTH
    return max(_MAX_DEPTH_FLOOR, min(value, _MAX_DEPTH_CEILING))


def normalize_chain(raw) -> tuple[str, ...]:
    """Accept envelope shapes (None, comma-separated string, list/tuple) and
    return a tuple of non-empty lowercased canonical persona ids."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        parts = raw
    else:
        return ()
    chain = []
    for part in parts:
        token = str(part or "").strip().lower()
        if token:
            chain.append(token)
    return tuple(chain)


def parse_deadline_epoch(raw) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def remaining_budget_seconds(deadline_epoch: float | None, *, now: float | None = None) -> float | None:
    if deadline_epoch is None:
        return None
    return deadline_epoch - (time.time() if now is None else now)


@dataclass(frozen=True)
class RelayDecision:
    allowed: bool
    error_kind: str | None = None  # relay_depth_limit | relay_cycle | relay_budget_exhausted
    reason: str | None = None
    chain: tuple[str, ...] = ()


def evaluate_relay(
    *,
    chain,
    target_persona_id: str,
    max_depth: int | None = None,
    deadline_epoch: float | None = None,
    now: float | None = None,
) -> RelayDecision:
    """Decide whether a relay hop onto ``target_persona_id`` may run.

    ``chain`` is the envelope chain (personas already speaking on this relay,
    outermost first, including the immediate caller). ``target_persona_id``
    must already be CANONICAL — evaluate at the persona chokepoint, not on
    raw model-supplied ids.
    """
    normalized = normalize_chain(chain)
    target = str(target_persona_id or "").strip().lower()
    limit = max_relay_depth() if max_depth is None else max_depth

    hops_so_far = max(0, len(normalized) - 1)
    if normalized and hops_so_far >= limit:
        return RelayDecision(
            allowed=False,
            error_kind="relay_depth_limit",
            reason=(
                f"agent-to-agent relay depth limit reached (max {limit} chained hops; "
                f"chain so far: {' -> '.join(normalized)}). Answer your caller directly "
                "instead of relaying onward."
            ),
            chain=normalized,
        )
    if target and target in normalized:
        return RelayDecision(
            allowed=False,
            error_kind="relay_cycle",
            reason=(
                f"relay cycle detected: '{target}' is already on this relay chain "
                f"({' -> '.join(normalized)}). Answer your caller directly instead of "
                "relaying back around the loop."
            ),
            chain=normalized,
        )
    remaining = remaining_budget_seconds(deadline_epoch, now=now)
    if remaining is not None and remaining < MIN_RELAY_BUDGET_SECONDS:
        return RelayDecision(
            allowed=False,
            error_kind="relay_budget_exhausted",
            reason=(
                f"relay budget exhausted: {remaining:.1f}s left on the shared chain "
                f"deadline (minimum {MIN_RELAY_BUDGET_SECONDS:.0f}s per hop; chain: "
                f"{' -> '.join(normalized) or '(root)'}). Answer your caller with what "
                "you have."
            ),
            chain=normalized,
        )
    return RelayDecision(allowed=True, chain=normalized + ((target,) if target else ()))
