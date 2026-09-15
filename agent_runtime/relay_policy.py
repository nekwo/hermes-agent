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


# ── Relayed-message sender attribution ──────────────────────────────
#
# A relayed incoming chat message (agent A -> agent B via agent_chat_send) is
# persisted as a bare role="user" SessionDB row — at rest indistinguishable
# from a real operator ("Tony") message. Without a sender marker the target's
# chat renders the relay AS the operator, erasing that another AGENT sent it.
#
# The sender identity rides the row's ``finish_reason`` column — the same
# typed-marker-in-``finish_reason`` precedent the pre_trace_ack ack rows use
# (see persona_chat_history._safe_recent_messages) — so no SessionDB schema
# change is needed and operator/CLI sends (``finish_reason`` stays None) keep
# byte-identical persistence. This module is the SINGLE build/parse authority
# for the marker: no second parse site exists anywhere else in the codebase.

RELAY_SENDER_FINISH_REASON_PREFIX = "relay_from:"


@dataclass(frozen=True)
class RelaySender:
    """Sender identity decoded from a relay marker.

    Either field is ``None`` when that segment was unresolvable at persist time
    — the honest unknown, never a fabricated identity."""

    persona_id: str | None = None
    instance_id: str | None = None


def build_relay_sender_marker(persona_id: str | None, instance_id: str | None) -> str:
    """Encode a relayed message's sender into a ``finish_reason`` marker.

    Shape: ``relay_from:<persona_id>:<instance_id>``. Either segment is emitted
    empty when unresolvable; the fully-unknown sender is the bare marker
    ``relay_from::`` (a deliberate honest-unknown, not a guessed operator)."""
    persona = (persona_id or "").strip()
    instance = (instance_id or "").strip()
    return f"{RELAY_SENDER_FINISH_REASON_PREFIX}{persona}:{instance}"


def parse_relay_sender_marker(value) -> "RelaySender | None":
    """Decode a ``finish_reason`` value into a :class:`RelaySender`, or ``None``.

    Non-marker values (``None``, ``""``, ``"stop"``, ``"pre_trace_ack"``, any
    arbitrary text) return ``None`` so only genuinely relay-tagged rows project
    as relayed. Parsing is defensive: split the remainder after the prefix at
    most twice; empty segments become ``None`` fields."""
    if not isinstance(value, str) or not value.startswith(RELAY_SENDER_FINISH_REASON_PREFIX):
        return None
    remainder = value[len(RELAY_SENDER_FINISH_REASON_PREFIX):]
    parts = remainder.split(":", 2)
    persona = parts[0].strip() if parts else ""
    instance = parts[1].strip() if len(parts) > 1 else ""
    return RelaySender(persona_id=persona or None, instance_id=instance or None)


# ── Harness-delivery origin attribution ─────────────────────────────
#
# A dispatch delivery turn (agent_runtime.dispatch_delivery) is forged into the
# SENDER's own chat session as a role="user" row — at rest indistinguishable
# from a message the operator typed, exactly like a relay. Without a marker the
# launcher attributes a result the HARNESS delivered to the operator ("You"),
# which is the same attribution defect the relay marker above retired, one lane
# over.
#
# It gets its OWN marker rather than borrowing ``relay_from:`` because it is not
# a relay: no agent sent it, there is no sending persona to name, and a consumer
# that grouped it with relayed messages would attribute a machine delivery to
# whichever persona happened to be encoded. What a delivery carries instead is
# the identity of the DISPATCH it settles, whether the dispatching agent flagged
# the operator to be told, and HOW THE DISPATCH ENDED — the facts a consumer
# needs to render it and to decide whether, and how, it is worth surfacing.
#
# The outcome is not decoration. ``pending_deliveries`` selects ``state !=
# running``, so an ``error`` and an ``unknown`` dispatch are delivered exactly
# like a completed one; without it on the wire the only trace of "this failed"
# is the prose body, and a consumer that read the prose to decide what to say
# would be doing the sentence-matching this whole marker exists to retire.
#
# Same column, same precedent, same single-authority rule: this module builds
# and parses; nothing else pattern-matches the string.

HARNESS_DELIVERY_FINISH_REASON_PREFIX = "harness_delivery:"

#: The outcome a delivery reports when the segment is absent or unreadable.
#: ``unknown`` rather than ``completed``: a marker written by a build that
#: predates the segment genuinely does not say how the work ended, and claiming
#: success on its behalf is the one wrong answer.
HARNESS_DELIVERY_UNKNOWN_STATE = "unknown"


@dataclass(frozen=True)
class HarnessDeliveryOrigin:
    """Provenance decoded from a harness-delivery marker.

    ``dispatch_id`` is ``None`` when the segment was unreadable — the marker
    still identifies the row as a delivery, because "the harness delivered this"
    is the load-bearing fact and it does not stop being true when the id is
    lost."""

    dispatch_id: str | None = None
    notify_operator: bool = False
    #: How the dispatch ended: ``completed`` | ``error`` | ``unknown`` (the
    #: ``dispatch_store.TERMINAL_STATES`` vocabulary). Never ``running`` — a
    #: delivery only exists once the work settled.
    state: str = HARNESS_DELIVERY_UNKNOWN_STATE


def build_harness_delivery_marker(
    dispatch_id: str | None,
    notify_operator: bool = False,
    state: str | None = None,
) -> str:
    """Encode a forged delivery turn's origin into a ``finish_reason`` marker.

    Shape: ``harness_delivery:<dispatch_id>:<0|1>:<state>``. The id segment is
    emitted empty when unknown; the flag is always one explicit character so a
    consumer never has to infer "not flagged" from a missing field; the state is
    the settled outcome, defaulting to the honest ``unknown``.

    The state segment is emitted LAST so a parser written against the earlier
    three-segment shape keeps reading the first two correctly."""
    dispatch = (dispatch_id or "").strip()
    settled = (state or "").strip() or HARNESS_DELIVERY_UNKNOWN_STATE
    return (
        f"{HARNESS_DELIVERY_FINISH_REASON_PREFIX}{dispatch}:"
        f"{'1' if notify_operator else '0'}:{settled}"
    )


def parse_harness_delivery_marker(value) -> "HarnessDeliveryOrigin | None":
    """Decode a ``finish_reason`` value into a :class:`HarnessDeliveryOrigin`.

    ``None`` for every non-marker value, so an operator row, a CLI send and a
    relayed message all keep their existing projection untouched. Only ``"1"``
    means flagged: an absent, empty or unparseable flag segment is read as NOT
    flagged, because notifying on an unreadable marker would manufacture
    operator interrupts out of corruption. An absent state segment — every row a
    pre-outcome build wrote — decodes to
    :data:`HARNESS_DELIVERY_UNKNOWN_STATE`, never to success."""
    if not isinstance(value, str) or not value.startswith(
        HARNESS_DELIVERY_FINISH_REASON_PREFIX
    ):
        return None
    remainder = value[len(HARNESS_DELIVERY_FINISH_REASON_PREFIX):]
    parts = remainder.split(":", 3)
    dispatch_id = parts[0].strip() if parts else ""
    flag = parts[1].strip() if len(parts) > 1 else ""
    state = parts[2].strip() if len(parts) > 2 else ""
    return HarnessDeliveryOrigin(
        dispatch_id=dispatch_id or None,
        notify_operator=flag == "1",
        state=state or HARNESS_DELIVERY_UNKNOWN_STATE,
    )
