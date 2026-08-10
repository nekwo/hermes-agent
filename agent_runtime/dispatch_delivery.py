"""Serve-hosted delivery drain — the half that makes ``wait: false`` honest.

``agent_chat_send(wait=false)`` promises the sender "their answer will come back
to you". This module is where that promise is kept: a daemon thread inside the
serve process walks the durable dispatch store, and for every completion whose
sender is IDLE it forges a new mission-chat turn into that sender's thread
carrying the result.

Why a forged TURN and not an injected message
---------------------------------------------
The completion has to become a real turn, in the sender's real thread, when the
sender is idle — never spliced into a conversation mid-flight. That invariant
is inherited from the delegation lane (``tools/async_delegation``) and it is
load-bearing twice over: message-role alternation must stay legal, and past
context must never be mutated (which would invalidate the prompt cache and can
corrupt a provider's view of the conversation). So delivery goes through the
SAME ``_cmd_mission_chat_message`` handler an operator message goes through —
one chat lane, no second write path, and the transcript, live log, turn journal
and Mission Control projection all get it for free.

Idle is checked twice, deliberately
-----------------------------------
First a zero-timeout probe of the sender's chat-root lease plus a scan of the
turn journal for in-flight records; then the handler's own lease acquisition
inside the forge. The probe is an optimisation that avoids paying for a turn
setup we know will be refused; the handler's lease is the actual guard, because
anything else would be a time-of-check/time-of-use race against an operator who
starts typing. A busy sender is never an error here — the claim is released and
the completion waits for a quieter moment.

The lease is probed and RELEASED before forging, never held across it: the
handler takes the same lease itself, and a same-process re-entrant acquisition
is exactly the deadlock this would otherwise be.

Where the chat database comes from
----------------------------------
Head-home scope ONLY. Persona turns flip ``HERMES_HOME`` process-globally for
their duration and they share this process, so a drain that read the ambient
home would resolve a different profile's SessionDB depending on what happened to
be mid-turn at that instant — and would forge a delivery into a database the
operator's console never opens.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "DELIVERY_REQUESTED_BY",
    "MAX_BACKGROUND_DELIVERY_ATTEMPTS",
    "ORPHAN_SWEEP_INTERVAL_SECONDS",
    "delivery_client_message_id",
    "delivery_drain_is_live",
    "delivery_requested_by",
    "drain_background_completions",
    "drain_once",
    "format_dispatch_delivery",
    "parse_delivery_requested_by",
    "start_delivery_drain",
    "sweep_orphaned_dispatches",
]

#: Provenance stamped on every forged delivery turn. The launcher renders turns
#: by origin, so this is what lets a delivered result read as a delivered result
#: instead of as something the operator typed.
#:
#: This is the PREFIX, not the whole value — see :func:`delivery_requested_by`.
DELIVERY_REQUESTED_BY = "harness-delivery"

#: Bound on the reply carried into the delivery message. Mirrors the relay
#: tool's own reply bound: past this, the answer belongs in the thread it was
#: written in, which the block points at.
REPLY_LIMIT = 8000

#: How often the drain looks for work. Deliberately unhurried — a completion is
#: not latency-critical, and every pass costs a lease probe plus a journal read
#: on a process that is also running turns.
DEFAULT_DRAIN_INTERVAL_SECONDS = 5.0

#: Per-pass delivery cap. Ten completions landing at once must not turn into ten
#: back-to-back forged turns that monopolise the sender's thread.
MAX_DELIVERIES_PER_PASS = 3

#: How often the orphan sweep runs. Its own cadence, deliberately: it walks every
#: running row and probes PIDs, which is too expensive for the 5s delivery loop —
#: and boot-only, which is what it was, meant a dispatch whose process died could
#: sit ``running`` in the HUD for as long as serve stayed up.
ORPHAN_SWEEP_INTERVAL_SECONDS = 60.0

#: Attempt cap for queue-backed background completions that have NO durable row
#: to count on — ``terminal`` notifications, and any completion whose producer
#: never persisted it. Without a cap a persistently-failing event would requeue
#: every drain pass forever.
#:
#: ``delegate_task`` completions are deliberately NOT counted here: they DO have
#: a durable row, ``async_delegations``, whose ``delivery_attempts`` column is
#: the authority the gateway, the interactive CLI and the TUI gateway have all
#: counted against since the lane shipped. Two counters for one completion is a
#: second ledger, which is the whole defect this cap must not reintroduce.
MAX_BACKGROUND_DELIVERY_ATTEMPTS = 8

#: Per-event failure counts for the LEDGERLESS half of the queue lane, keyed by
#: the event's own identity. Process-local by nature — the queue it guards is
#: process-local too, and so is the class of event it covers.
_background_attempts: dict[str, int] = {}

#: The live drain thread, or None. Read by the capability binding: "serve is
#: running" was only ever a PROXY for "a consumer exists", and the drain starts
#: best-effort, so a serve whose drain failed used to go on promising deliveries
#: nothing would perform.
_drain_thread: threading.Thread | None = None
#: Guards the registration/teardown pair above, so a restart and an old loop's
#: teardown cannot interleave into "registered, then immediately cleared".
_drain_lock = threading.Lock()


def delivery_drain_is_live() -> bool:
    """Whether a delivery drain is actually running in THIS process.

    The honest answer to "can a background completion reach this session later",
    and the reason the mission-chat capability binding is not just a serve check.
    """

    thread = _drain_thread
    return bool(thread is not None and thread.is_alive())


def delivery_client_message_id(dispatch_id: str) -> str:
    """The idempotency key for a dispatch's delivery turn.

    Derived from the dispatch id rather than minted per attempt, so the chat
    lane's EXISTING dedup (client-message-id replay + resend detection) makes a
    retried delivery converge on one turn instead of doubling the message. The
    store's claim protocol makes a double attempt unlikely; this makes a double
    TURN impossible, which is the part the sender would actually see.
    """

    return f"dispatch-delivery-{dispatch_id}"


def delivery_requested_by(
    dispatch_id: str, *, notify_operator: bool = False, state: str | None = None
) -> str:
    """The ``requested_by`` provenance a forged delivery turn is sent under.

    Shape: ``harness-delivery:<dispatch_id>:<0|1>:<state>``. Structured
    provenance in this field is the ESTABLISHED convention on this lane, not a
    new one — an ``agent_chat_send`` relay already travels as
    ``agent:<caller session id>`` and the mission-chat handler already decodes
    that into the marker its user row is typed with. A delivery follows the same
    road.

    Three facts ride along because the handler is the last place that has them
    and the persisted row is the last place they can be recovered from: WHICH
    dispatch this settles, whether the dispatching agent flagged the operator to
    be told, and HOW IT ENDED. Without the first two, a consumer would have to
    join against a ``running_work`` row that no longer exists by then (a
    dispatch leaves that projection the moment it stops running). Without the
    third, an ``error`` delivery is indistinguishable from a successful one
    except in prose — and ``pending_deliveries`` selects ``state != running``,
    so failures genuinely travel this lane.
    """

    from .relay_policy import HARNESS_DELIVERY_UNKNOWN_STATE

    settled = (state or "").strip() or HARNESS_DELIVERY_UNKNOWN_STATE
    return (
        f"{DELIVERY_REQUESTED_BY}:{str(dispatch_id or '').strip()}:"
        f"{'1' if notify_operator else '0'}:{settled}"
    )


def parse_delivery_requested_by(value):
    """Decode a ``requested_by`` value into a ``HarnessDeliveryOrigin`` or None.

    Tolerates the bare ``harness-delivery`` (no suffix) that a pre-attribution
    build stamped: those rows are still deliveries and must still render as
    deliveries; they simply carry no dispatch id, no flag, and an ``unknown``
    outcome. Returns ``None`` for every other value, so operator, CLI,
    coordinator and relay sends are untouched.
    """

    from .relay_policy import HARNESS_DELIVERY_UNKNOWN_STATE, HarnessDeliveryOrigin

    if not isinstance(value, str):
        return None
    token = value.strip()
    if token == DELIVERY_REQUESTED_BY:
        return HarnessDeliveryOrigin()
    prefix = f"{DELIVERY_REQUESTED_BY}:"
    if not token.startswith(prefix):
        return None
    parts = token[len(prefix):].split(":", 3)
    dispatch_id = parts[0].strip() if parts else ""
    flag = parts[1].strip() if len(parts) > 1 else ""
    state = parts[2].strip() if len(parts) > 2 else ""
    return HarnessDeliveryOrigin(
        dispatch_id=dispatch_id or None,
        notify_operator=flag == "1",
        state=state or HARNESS_DELIVERY_UNKNOWN_STATE,
    )


def _elapsed(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if rest == 0 else f"{minutes}m{rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h{minutes}m"


def format_dispatch_delivery(row: dict[str, Any]) -> str:
    """The self-contained block a delivery turn carries.

    Modelled on ``process_registry._format_async_delegation`` and for the same
    reason: by the time this re-enters the conversation the sender may be deep
    in unrelated context and will not remember why the dispatch existed. So the
    block stands entirely on its own — who was asked, what they were asked, what
    came back, where the thread is, and how long it took — enough to either use
    the result or re-dispatch because the world moved on.
    """

    result = row.get("result") or {}
    status = str(row.get("state") or "unknown")
    reply = str(result.get("reply") or "")[:REPLY_LIMIT]
    error = str(result.get("error") or "")
    target = str(row.get("target_persona") or "the teammate")
    instance = str(row.get("target_instance_id") or "")
    ask = str(row.get("ask") or "")
    dispatched_at = float(row.get("dispatched_at") or 0.0)
    completed_at = float(row.get("completed_at") or time.time())

    lines = [
        f"[BACKGROUND DISPATCH COMPLETE — {row.get('dispatch_id')}]",
        (
            f"You dispatched {target} in the background earlier and kept working. "
            "They have finished; their answer is below. You may have moved on since "
            "then — use this, or re-dispatch if things have changed."
        ),
        "",
        f"Dispatched to: {target}" + (f" ({instance})" if instance else ""),
    ]
    if dispatched_at:
        lines.append(
            "Dispatched: "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(dispatched_at))
            + f" ({_elapsed(completed_at - dispatched_at)} ago)"
        )
    if ask:
        lines.append("")
        lines.append("What you asked them:")
        lines.append(ask[:2000])
    lines.append("")
    lines.append(f"Status: {status}")
    if row.get("target_session_id"):
        # Thread pointer, not a transcript: the full exchange lives there and
        # agent_chat_open / agent_chat_log_path can read it on demand.
        lines.append(
            f"Their thread: {row['target_session_id']} "
            "(agent_chat_open with this session_id to read the whole exchange)"
        )
    lines.append("")
    if reply:
        lines.append("--- THEIR REPLY ---")
        lines.append(reply)
    if error:
        lines.append("--- ERROR ---")
        lines.append(error[:600])
    if not reply and not error:
        lines.append("They returned no reply text. Read the thread above, or re-dispatch.")
    if row.get("notify_operator"):
        lines.append("")
        lines.append(
            "OPERATOR IS WAITING ON THIS: you flagged this dispatch to notify the operator. "
            "Tell them the result now, in this conversation, in your own words — what you "
            "asked for, what came back, and what you are doing about it."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# idle gating
# --------------------------------------------------------------------------


def _sender_is_idle(root_session_id: str) -> bool:
    """True when the sender's chat root has no turn in flight.

    Two independent signals, and BOTH are needed. The lease answers "is a turn
    holding this root right now" — but only while its holder is alive, so a
    turn whose executor died leaves the lease free while the journal still shows
    it in flight. The journal answers "does the record say a turn is running" —
    but it is written by whichever process ran the turn and lags a live
    acquisition. Trusting either one alone delivers into a thread that is mid-turn.
    """

    from .mission_chat_turns import INFLIGHT_TURN_STATES, mission_chat_turn_records
    from .persona_chat_continuity import PersonaChatBusyError, persona_chat_root_lease

    try:
        records = mission_chat_turn_records(session_id=root_session_id)
    except Exception:
        # Cannot read the journal ⇒ cannot prove idle. Fail closed: a delayed
        # delivery costs nothing, a spliced one corrupts a conversation.
        return False
    for record in records or []:
        if str((record or {}).get("state") or "") in INFLIGHT_TURN_STATES:
            return False

    try:
        # Probe ONLY: acquire with no wait and release immediately. Holding it
        # across the forge would deadlock against the handler's own acquisition
        # of the same root.
        with persona_chat_root_lease(
            root_session_id, owner_id="dispatch-delivery-probe", observer_kind="serve"
        ):
            pass
    except PersonaChatBusyError:
        return False
    except Exception:
        return False
    return True


def _sender_persona(root_session_id: str) -> tuple[str, str] | None:
    """``(persona_id, persona_instance_id)`` that owns a chat root, or None.

    This is the POSITIVE OWNERSHIP proof a restored completion needs (#64484):
    a row recovered from a previous process names a chat root, and delivery only
    proceeds if that root still resolves to a real persona instance in THIS
    runtime. Absence of the instance is not "deliver anyway", it is "do not".

    The derivation itself lives in
    :func:`agent_runtime.persona_assignments.chat_session_owner_persona`, the
    runtime's ONE answer to "whose work is this" — shared with the running-work
    projection, which used to ship a null owner on every delegation row rather
    than reach for it. This name stays as the delivery lane's local vocabulary
    and must never grow a second derivation.
    """

    from .persona_assignments import chat_session_owner_persona

    return chat_session_owner_persona(root_session_id)


# --------------------------------------------------------------------------
# the forge
# --------------------------------------------------------------------------


def forge_delivery_turn(
    *,
    root_session_id: str,
    persona_id: str,
    persona_instance_id: str,
    message: str,
    client_message_id: str,
    dispatch_id: str = "",
    notify_operator: bool = False,
    state: str | None = None,
    max_seconds: float | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Run one delivery as a real mission-chat turn. Returns ``(ok, payload)``.

    Goes through the canonical handler, with the payload taken off the
    ``payload_sink`` seam so nothing is printed into the serve frame protocol.
    """

    from .config import mission_chat_default_max_seconds

    payloads: list[dict] = []
    args = SimpleNamespace(
        persona_id=persona_id,
        persona_instance_id=persona_instance_id,
        session_id=root_session_id,
        clarify_token=None,
        # NEVER a fresh thread: the delivery belongs in the conversation the
        # dispatch was sent from, which is the whole point of delivering it.
        new_session=False,
        task_id=None,
        goal_id=None,
        title=None,
        message=message,
        provider=None,
        model=None,
        use_agent_default=False,
        surface_prompt="",
        intent_hint="chat",
        # Structured provenance: the handler decodes this into the typed marker
        # the persisted user row carries, which is the ONLY reason a consumer
        # can tell a delivered result from something the operator typed.
        requested_by=delivery_requested_by(
            dispatch_id, notify_operator=notify_operator, state=state
        ),
        client_message_id=client_message_id,
        stream=False,
        max_seconds=float(max_seconds or mission_chat_default_max_seconds()),
        json=True,
        requested_by_session=root_session_id,
        # A delivery is a NEW CHAIN ROOT: it is not a hop of the original relay
        # (that chain ended when the target answered), and giving it the old
        # chain would make the sender's own follow-up sends look like depth-3
        # hops of a conversation that is already over.
        relay_chain=[],
        relay_deadline_epoch=None,
        payload_sink=payloads.append,
    )
    from hermes_cli import harness as _harness

    exit_code = _harness._cmd_mission_chat_message(args)
    payload = payloads[-1] if payloads else None
    ok = exit_code == 0 and bool((payload or {}).get("ok"))
    return ok, payload


# --------------------------------------------------------------------------
# the drain
# --------------------------------------------------------------------------


def drain_once(*, forge: Callable | None = None, limit: int | None = None) -> dict[str, int]:
    """One delivery pass. Never raises; returns a per-pass tally.

    A pass that cannot read the store, cannot resolve a sender, or finds every
    sender busy is a normal outcome, not an error — the completions are durable
    and the next pass tries again.
    """

    from . import dispatch_store

    tally = {"considered": 0, "delivered": 0, "busy": 0, "dropped": 0, "failed": 0}
    forge = forge or forge_delivery_turn
    try:
        rows = dispatch_store.pending_deliveries(
            limit=int(limit or MAX_DELIVERIES_PER_PASS) * 4
        )
    except Exception:
        logger.debug("dispatch delivery drain could not read the store", exc_info=True)
        return tally

    budget = int(limit or MAX_DELIVERIES_PER_PASS)
    for row in rows:
        if tally["delivered"] >= budget:
            break
        tally["considered"] += 1
        dispatch_id = str(row.get("dispatch_id") or "")
        root = str(row.get("sender_session_id") or "")
        if not dispatch_id:
            continue
        if not root:
            # Nowhere to deliver, ever. Terminal rather than retried, so it
            # stops occupying the queue — and recorded, so it is not silent.
            dispatch_store.drop_delivery(dispatch_id, reason="no_sender_session")
            tally["dropped"] += 1
            continue

        owner = _sender_persona(root)
        if owner is None:
            dispatch_store.drop_delivery(dispatch_id, reason="sender_session_unresolvable")
            tally["dropped"] += 1
            continue

        if not _sender_is_idle(root):
            tally["busy"] += 1
            continue

        claim_id = f"serve-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            if not dispatch_store.claim_delivery(dispatch_id, claim_id):
                continue
        except Exception:
            logger.debug("dispatch %s claim failed", dispatch_id, exc_info=True)
            continue

        persona_id, instance_id = owner
        payload: dict[str, Any] | None = None
        try:
            ok, payload = forge(
                root_session_id=root,
                persona_id=persona_id,
                persona_instance_id=instance_id,
                message=format_dispatch_delivery(row),
                client_message_id=delivery_client_message_id(dispatch_id),
                dispatch_id=dispatch_id,
                notify_operator=bool(row.get("notify_operator")),
                state=str(row.get("state") or ""),
            )
        except Exception:
            logger.warning("dispatch %s delivery turn failed", dispatch_id, exc_info=True)
            ok = False
        if ok:
            dispatch_store.mark_delivered(dispatch_id)
            tally["delivered"] += 1
            continue
        # The sender took its lease between the idle probe and the forge. That
        # is a RACE WITH A LIVE OPERATOR, not a failure — the completion is
        # perfectly deliverable and will be, moments later. Refund the attempt
        # the claim burned: without this, eight unlucky races walk a good
        # completion to a terminal `dropped` having never once failed.
        busy = str((payload or {}).get("error_kind") or "") == "chat_busy"
        dispatch_store.release_delivery_claim(dispatch_id, refund_attempt=busy)
        if busy:
            tally["busy"] += 1
        else:
            tally["failed"] += 1
    return tally


def sweep_orphaned_dispatches() -> int:
    """Settle dispatches whose executing process is provably gone. Returns the count.

    Runs on the drain's own cadence, not only at boot. Boot-only was a real gap:
    a serve process can stay up for days, so a dispatch whose child died at 09:00
    stayed ``running`` — in the operator's HUD and in the sender's mental model —
    until the next restart. The sender was owed "the outcome is unknown" within a
    minute of it becoming true, not within a week.

    Identity-verified through the store's own sweep, which treats an unreadable
    start-time probe as absence of proof rather than as death.
    """

    from . import dispatch_store

    try:
        return int((dispatch_store.restore_undelivered_dispatches() or {}).get("restored") or 0)
    except Exception:  # pragma: no cover - a sweep must never kill the drain
        logger.debug("dispatch orphan sweep failed", exc_info=True)
        return 0


# --------------------------------------------------------------------------
# background-completion lane (delegate_task / terminal notifications)
# --------------------------------------------------------------------------


def _chat_root_of_completion(evt: dict[str, Any]) -> str | None:
    """The persona chat root a queued completion belongs to, or None.

    POSITIVE proof, in the sense ``drain_notifications`` means it: the event has
    to NAME a session that resolves to a live persona instance's chat lane in
    this runtime. Gateway and CLI sessions resolve to nothing here and are left
    queued for their own consumers rather than adopted — the #64484 rule, which
    is why this returns None instead of guessing.
    """

    for key in ("origin_ui_session_id", "parent_session_id", "session_key", "session_id"):
        candidate = str(evt.get(key) or "").strip()
        if not candidate.startswith("persona_chat_"):
            continue
        if _sender_persona(candidate) is not None:
            return candidate
    return None


def _claim_durable_completion(evt: dict[str, Any]) -> str | None:
    """Claim a queued completion's durable row. ``""`` when it has none.

    Three outcomes, and the middle one is why this is not a bool:

    * ``""`` — LEDGERLESS. The event is not a ``delegate_task`` completion (a
      ``terminal`` notification, say), or its producer never persisted a row, or
      the store could not be reached. Delivery proceeds and the process-local
      attempt counter governs it.
    * a claim id — the row was ``async_delegations``-backed and is now ours for
      this pass. The claim itself counted the attempt.
    * ``None`` — the row exists and is NOT claimable: another consumer holds it,
      or it already settled. The caller must not deliver.

    A store this process cannot reach fails OPEN, deliberately: the ledger is
    accounting, the forge is the operator's answer, and refusing to deliver a
    finished result because a bookkeeping table was unreadable would trade the
    defect above for a strictly worse one. The cost is a possible duplicate
    delivery, which the chat lane's ``client_message_id`` replay already
    converges, and it is logged rather than silent.
    """

    if str(evt.get("type") or "") != "async_delegation":
        return ""
    try:
        from tools.async_delegation import claim_event_delivery
    except Exception:
        logger.debug("durable completion claim unavailable", exc_info=True)
        return ""
    try:
        return claim_event_delivery(evt, "harness-serve")
    except Exception:
        logger.warning(
            "could not claim durable completion %s; delivering unclaimed",
            evt.get("delegation_id"),
            exc_info=True,
        )
        return ""


def _settle_durable_completion(
    evt: dict[str, Any], claim: str, *, delivered: bool
) -> None:
    """Write a delivery outcome back to the row that owns it. Never raises.

    ``delivered`` acknowledges; anything else releases the claim so another
    consumer — or this drain on a later pass — may retry, with the row's own
    attempt budget deciding when retrying stops. A ledgerless event (empty
    *claim*) has nothing to settle.
    """

    if not claim:
        return
    try:
        from tools.async_delegation import (
            complete_event_delivery,
            release_event_delivery,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("durable completion settle unavailable", exc_info=True)
        return
    try:
        if delivered:
            complete_event_delivery(evt, claim)
        else:
            release_event_delivery(evt, claim)
    except Exception:
        logger.warning(
            "could not settle durable completion %s (delivered=%s)",
            evt.get("delegation_id"),
            delivered,
            exc_info=True,
        )


def drain_background_completions(*, forge: Callable | None = None) -> dict[str, int]:
    """Deliver ``delegate_task(background)`` / ``terminal`` completions.

    These already publish to ``process_registry.completion_queue`` — the CLI and
    the gateway have drained it for a long time; serve never did, which is the
    only reason those tools had to be refused on the persona-chat lane. With
    this drain running, the same completions become delivery turns through the
    same forged-turn path a dispatch uses, so the two lanes cannot diverge in
    how a background result reaches an agent.

    Ownership is proven per event, not assumed per process: an event that does
    not name a resolvable persona chat root is re-queued untouched.

    The durable row is settled through ITS OWN authority, not a second ledger
    ------------------------------------------------------------------------
    ``delegate_task`` completions are persisted to ``async_delegations`` before
    they are ever queued, and that row carries the whole delivery protocol:
    ``delivery_state``, ``delivery_attempts``, ``delivery_claim``,
    ``delivered_at``. The gateway, the interactive CLI and the TUI gateway have
    always driven it through ``claim_event_delivery`` /
    ``complete_event_delivery`` / ``release_event_delivery``. Serve was the one
    consumer that did not: it drained the in-memory queue, forged a real turn,
    and left the row ``pending`` with ``delivery_attempts: 0`` forever.

    That was not cosmetic. Three things followed from it, and a live 2026-08-10
    delegation (``deleg_1f53b4be``) exhibited all three at once — a completion
    that HAD been delivered into the operator's thread while every durable field
    said no attempt was ever made:

    * **The forensics lied in the worst direction.** ``delivery_attempts: 0`` on
      a delivered row reads as "the drain never picked it up", which is the one
      conclusion the evidence cannot support and the first one an investigator
      draws.
    * **Every serve delivery was re-armed for replay.** ``restore_undelivered_
      completions`` selects exactly ``state != 'running' AND delivery_state =
      'pending'``, so serve's own successful deliveries were re-queued at the
      next boot. The chat lane's ``client_message_id`` replay dedup is what has
      been absorbing them — an idempotency guard silently doing a ledger's job,
      and only for as long as the turn journal retains the record.
    * **Retention could not do its job.** ``_prune_durable_records`` deletes
      ``delivery_state='delivered'`` rows first and falls back to evicting the
      OLDEST pending ones; with nothing ever marked delivered, the fallback is
      the only path, so undelivered work is what gets dropped.

    So the claim is taken here, after the idle probe (never before — a burnt
    attempt for losing a race with a live operator is the failure class the
    dispatch lane already had to fix) and before the forge, and the outcome is
    written back to the row that owns it. A claim this process cannot take means
    another consumer holds it or the row already settled: the event leaves the
    queue rather than spinning, because the durable row — not this queue — is
    the authority on whether the completion is still owed.

    A LEDGERLESS event (a ``terminal`` notification; a completion whose producer
    never persisted a row) claims nothing, and only those fall back to the
    process-local :data:`MAX_BACKGROUND_DELIVERY_ATTEMPTS` counter. Without it
    such an event re-queues every five seconds forever — an invisible hot loop
    that also blocks the queue behind it — so past the cap it is dropped with a
    WARNING naming it, because an event that can never be delivered should end
    loudly rather than spin quietly.
    """

    tally = {
        "considered": 0,
        "delivered": 0,
        "requeued": 0,
        "failed": 0,
        "abandoned": 0,
        "unclaimed": 0,
    }
    forge = forge or forge_delivery_turn
    try:
        from tools.process_registry import process_registry
    except Exception:
        return tally

    try:
        pairs = process_registry.drain_notifications(
            owns_event=lambda evt: _chat_root_of_completion(evt) is not None,
            skip_poll_observed=False,
        )
    except Exception:
        logger.debug("background completion drain failed", exc_info=True)
        return tally

    for evt, text in pairs:
        tally["considered"] += 1
        root = _chat_root_of_completion(evt)
        owner = _sender_persona(root) if root else None
        if root is None or owner is None or not text:
            # Ownership was proven at drain time; if it cannot be re-proven now
            # the event goes BACK on the queue rather than being dropped.
            process_registry.completion_queue.put(evt)
            tally["requeued"] += 1
            continue
        if not _sender_is_idle(root):
            process_registry.completion_queue.put(evt)
            tally["requeued"] += 1
            continue
        persona_id, instance_id = owner
        # The queue has no durable per-event id, so the dedup key is derived
        # from the event's own stable identity (the process/delegation id plus
        # its type) rather than minted per attempt.
        marker = str(
            evt.get("delegation_id") or evt.get("session_id") or uuid.uuid4().hex
        )[:40]
        key = f"{evt.get('type', 'completion')}:{marker}"
        # Taken AFTER the idle probe: a claim burnt on a busy sender would spend
        # a durable attempt on a race with a live operator, which is the exact
        # over-counting the dispatch lane's `refund_attempt` had to undo.
        claim = _claim_durable_completion(evt)
        if claim is None:
            # The row is not ours to deliver: another consumer holds the claim,
            # or it already settled. Not re-queued — spinning on a completion
            # somebody else owns is how a queue silently starves the events
            # behind it.
            tally["unclaimed"] += 1
            continue
        durable = bool(claim)
        if (
            not durable
            and _background_attempts.get(key, 0) >= MAX_BACKGROUND_DELIVERY_ATTEMPTS
        ):
            # Terminal, and LOUD. A silently-abandoned completion is the failure
            # class this whole lane exists to retire, so it leaves a named log
            # line rather than disappearing off the queue.
            #
            # Only ledgerless events reach here. A durable one converges through
            # its own row instead: the release below drops it to a terminal
            # state once its attempts are spent, and the claim above then
            # refuses it for good.
            logger.warning(
                "abandoning background completion %s after %d failed deliveries into %s",
                key,
                MAX_BACKGROUND_DELIVERY_ATTEMPTS,
                root,
            )
            _background_attempts.pop(key, None)
            tally["abandoned"] += 1
            continue
        try:
            ok, payload = forge(
                root_session_id=root,
                persona_id=persona_id,
                persona_instance_id=instance_id,
                message=text,
                client_message_id=f"bg-completion-{key}",
            )
        except Exception:
            logger.warning("background completion delivery failed", exc_info=True)
            ok, payload = False, None
        if ok:
            # Acknowledge on the row that owns the question. Without this the
            # next boot's restore sweep re-queues a completion this process
            # already delivered.
            _settle_durable_completion(evt, claim, delivered=True)
            _background_attempts.pop(key, None)
            tally["delivered"] += 1
            continue
        _settle_durable_completion(evt, claim, delivered=False)
        process_registry.completion_queue.put(evt)
        # Same rule the dispatch lane follows: losing a race with a live
        # operator is not a failure, so a `chat_busy` refusal does not count
        # against the cap.
        if str((payload or {}).get("error_kind") or "") == "chat_busy":
            tally["requeued"] += 1
        elif durable:
            # The durable row counted this attempt at claim time; counting it
            # here as well is the second ledger, so the tally records the
            # failure and nothing else does.
            tally["failed"] += 1
        else:
            _background_attempts[key] = _background_attempts.get(key, 0) + 1
            tally["failed"] += 1
    return tally


def start_delivery_drain(
    *,
    stop_event: threading.Event,
    interval_seconds: float = DEFAULT_DRAIN_INTERVAL_SECONDS,
) -> threading.Thread:
    """Run the drain on a daemon thread until *stop_event* is set.

    Daemon by contract: this loop must never be the reason a process cannot
    exit. Everything it would have done is durable, so the next boot picks it up
    — which is the same guarantee the restore-on-boot sweep provides.
    """

    def _loop() -> None:
        global _drain_thread
        next_sweep = 0.0
        try:
            while not stop_event.wait(interval_seconds):
                try:
                    drain_once()
                except Exception:  # pragma: no cover - a drain must never die
                    logger.debug("dispatch delivery pass failed", exc_info=True)
                try:
                    drain_background_completions()
                except Exception:  # pragma: no cover
                    logger.debug("background completion pass failed", exc_info=True)
                # The orphan sweep runs on its own, slower cadence: it walks
                # every running row and probes PIDs, which is far too expensive
                # for the 5s delivery loop and far too rare for boot-only.
                now = time.monotonic()
                if now >= next_sweep:
                    next_sweep = now + ORPHAN_SWEEP_INTERVAL_SECONDS
                    sweep_orphaned_dispatches()
        finally:
            # The capability binding reads this. A drain that died must stop the
            # runtime promising deliveries it can no longer make — the same
            # honesty rule that made the binding conditional in the first place.
            #
            # Cleared ONLY when this loop is still the registered drain. An
            # unconditional clear let an old loop's teardown null a drain that
            # had already been restarted, after which `delivery_drain_is_live()`
            # answered False forever and every dispatch was refused by a runtime
            # perfectly able to deliver.
            with _drain_lock:
                if _drain_thread is threading.current_thread():
                    _drain_thread = None

    thread = threading.Thread(
        target=_loop, name="harness-serve-dispatch-delivery", daemon=True
    )
    global _drain_thread
    with _drain_lock:
        _drain_thread = thread
    thread.start()
    return thread
