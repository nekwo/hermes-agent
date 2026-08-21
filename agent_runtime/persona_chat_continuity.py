"""Native persona-chat continuity primitives.

This module intentionally contains no CLI presentation logic.  Direct CLI
commands and long-lived ``harness serve`` processes share these exact lease,
mint-receipt, safe-history, and resident-actor primitives.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Iterator
from datetime import datetime, timezone

from . import paths
from .dispatch_session_policy import superseded_session_id
from .persona_assignments import (
    PersonaInstanceStore,
    persona_chat_session_id_for,
    safe_assignment_text,
    safe_assignment_token,
)
from .redaction import TEXT_SECRET_ASSIGNMENT_RE


logger = logging.getLogger(__name__)

PERSONA_CHAT_SESSION_SOURCE = "agent_runtime_persona_chat"
_TOOL_EXECUTION_SCOPE: ContextVar[str | None] = ContextVar(
    "persona_chat_tool_execution_scope", default=None
)
# Single-homed in ``agent_runtime.redaction`` — see the header there for the
# JSON blind spot every local spelling shared. group(1) is still the key, so
# the ``\1: [redacted]`` rebuild below is unchanged; the value shape widens
# from ``[^\s,;]+`` to ``\S+``, which only removes MORE of the offending run.
_SECRET_RE = TEXT_SECRET_ASSIGNMENT_RE

#: Flat ceiling for one opaque free-text row.
#:
#: **THIS IS A WIRE BOUND, NOT A PERSISTENCE BOUND.** It reads like the latter —
#: it lives in the continuity module, beside the session-DB flush, and it was
#: introduced to keep persisted rows small. But the flush hands its result back
#: into the LIVE actor's message list (``msg.clear(); msg.update(native)`` in
#: ``run_agent``) BEFORE the first provider call of the turn, so whatever this
#: number cuts, the model never sees.
#:
#: That is not a hypothetical. On 2026-08-09 this single flat 20,000 applied to
#: a COMPOSED operator row and delivered 37% of a required skill, cut
#: mid-sentence, with the runtime HUD amputated entirely — and because the cut
#: took the ``</skill_preload>`` closing tag with it, the ``unchanged`` dedupe
#: became structurally unreachable and re-shipped ~3.7 k tokens every turn. The
#: composed row now has its own per-part ceilings (below); this one still
#: governs every other role, and it still governs the wire.
#:
#: Anyone changing it is changing the prompt. :func:`native_wire_row` is the
#: named boundary that says so, and it accounts for every character this number
#: removes so the loss can never again be silent.
_MAX_CONTENT = 20_000
_MAX_ARGUMENTS = 4_000

#: Total ceiling for ONE composed operator user row (message · skill_preload ·
#: runtime_context). Deliberately much larger than :data:`_MAX_CONTENT`, and
#: deliberately the ONLY ceiling on that row — the per-part limits below are
#: priority slices of this one number, not independent budgets that could
#: silently disagree with it.
#:
#: 256 KiB is ~4.6x the largest real preload measured on this lane (qa's
#: ``launcher-stagec-mcp-screenshot``, 54 KB) and it is paid at most ONCE per
#: thread: turn 2+ delivers the compact ``unchanged`` stub, so steady-state rows
#: are a few hundred bytes.
_MAX_USER_ROW_CONTENT = 262_144

#: Priority slice for the runtime-context (HUD) envelope. Served FIRST, because
#: it is the smallest load-bearing part: the wall-budget countdown, roster,
#: scope, and steering lines. Under the previous single flat cap it composed
#: LAST and was therefore the first thing amputated — on exactly the personas
#: whose turns run long enough for a budget warning to matter.
_MAX_RUNTIME_CONTEXT_CONTENT = 32_000

#: Names of the three parts, for the typed bound notes below.
BOUND_PART_MESSAGE = "message"
BOUND_PART_SKILL_PRELOAD = "skill_preload"
BOUND_PART_RUNTIME_CONTEXT = "runtime_context"
#: The whole content of a row that is opaque free text (every role but ``user``:
#: assistant replies, tool results, system notes). Not composed, so not split —
#: but bounded, and therefore accounted, by the same boundary.
BOUND_PART_CONTENT = "content"
#: One tool call's serialized arguments, bounded at :data:`_MAX_ARGUMENTS`. The
#: same silent-loss class as the free-text rows and on the same wire: these ride
#: back into the live actor with the rest of the row, so a truncated argument
#: blob is what the model sees its own previous call as having made.
BOUND_PART_TOOL_ARGUMENTS = "tool_arguments"

#: The parts that make up a row's ``content``, and therefore the only ones whose
#: losses belong in the content arithmetic.
CONTENT_BOUND_PARTS = frozenset(
    {
        BOUND_PART_MESSAGE,
        BOUND_PART_SKILL_PRELOAD,
        BOUND_PART_RUNTIME_CONTEXT,
        BOUND_PART_CONTENT,
    }
)

BOUND_ACTION_TRUNCATED = "truncated"
BOUND_ACTION_DROPPED = "dropped"

#: In-band marker for bounded free text. Unchanged spelling — a persisted row
#: carrying it must keep reading the same way it always has.
_TRUNCATION_MARKER = " … [truncated]"


@dataclass(frozen=True, slots=True)
class ContentBoundNote:
    """One part of a composed row that did not survive its bound intact."""

    part: str
    action: str
    original_chars: int
    bounded_chars: int
    limit: int


@dataclass(frozen=True, slots=True)
class BoundedUserContent:
    """A bounded composed user row, plus what the bound did to it.

    ``notes`` is the whole point. A bound that silently amputates structured
    content mid-token is worse than one that cuts a part and says so, and the
    2026-08-09 F1 finding is what that costs: 37% of a required skill delivered
    mid-sentence, the HUD gone entirely, and — because the amputation took the
    ``</skill_preload>`` closing tag with it — the ``unchanged`` dedupe made
    structurally unreachable, re-shipping ~3.7 k tokens every turn forever.
    """

    text: str
    notes: tuple[ContentBoundNote, ...] = ()
    #: Length of the content this bound was applied TO, measured after redaction
    #: and before any part was cut. Carried so the caller can prove the arithmetic
    #: closes — ``source_chars - len(text)`` must equal the loss the notes
    #: account for. A residue means content left through a path with no note on
    #: it, which is the exact class of defect the notes exist to retire.
    source_chars: int = 0

    @property
    def bounded(self) -> bool:
        return bool(self.notes)

    @property
    def accounted_loss(self) -> int:
        """Characters the notes explain."""

        return sum(max(0, note.original_chars - note.bounded_chars) for note in self.notes)


def _redacted(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:
        return _SECRET_RE.sub(r"\1: [redacted]", text)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_MARKER))
    return text[:keep].rstrip() + _TRUNCATION_MARKER


def _safe_text(value: Any, *, limit: int = _MAX_CONTENT) -> str:
    return _truncate(_redacted(value), limit)


def _bound_envelope(
    envelope: str, *, limit: int, part: str, codec: Any
) -> tuple[str, ContentBoundNote | None]:
    """Bound one envelope by shrinking its BODY, never its tags.

    The opening tag and its attributes are what every downstream consumer reads:
    the transcript projection strips on them, and the skill preload's
    ``unchanged`` dedupe matches on ``revision``/``delivery``. Cutting through
    them is what turned a size problem into a correctness one.

    The whole part is dropped only when the budget cannot hold the tags at all —
    at that size there is no well-formed envelope to emit, and an honest absence
    beats a broken one.
    """

    original = len(envelope)
    if original <= limit:
        return envelope, None
    body = codec.body(envelope)
    if body is None:
        # Not this grammar (a legacy row, a hand-edited one): treat it as opaque
        # free text rather than hand-editing tags we did not parse.
        bounded = _truncate(envelope, limit)
        return bounded, ContentBoundNote(
            part=part,
            action=BOUND_ACTION_TRUNCATED,
            original_chars=original,
            bounded_chars=len(bounded),
            limit=limit,
        )
    body_limit = limit - (original - len(body))
    if body_limit <= len(_TRUNCATION_MARKER):
        return "", ContentBoundNote(
            part=part,
            action=BOUND_ACTION_DROPPED,
            original_chars=original,
            bounded_chars=0,
            limit=limit,
        )
    bounded = codec.with_body(envelope, _truncate(body, body_limit)) or ""
    return bounded, ContentBoundNote(
        part=part,
        action=BOUND_ACTION_TRUNCATED,
        original_chars=original,
        bounded_chars=len(bounded),
        limit=limit,
    )


def bound_composed_user_content(value: Any) -> BoundedUserContent:
    """Bound one operator user row PER PART, in priority order.

    Order of service is the contract, not an implementation detail:

    1. the runtime-context (HUD) envelope — smallest, load-bearing, must always
       arrive;
    2. the operator's own text — never cut without the explicit in-band marker;
    3. the skill preload — largest and the only part with its own re-delivery
       machinery, so it absorbs whatever room is left.

    A row carrying neither envelope is bounded exactly as before (:data:`_MAX_CONTENT`
    on the whole thing), so nothing outside the mission-chat composition changes.
    """

    # Function-local by the same precedent ``prompt_observability`` documents:
    # ``runtime_hud`` pulls a sizeable dependency graph and this module is
    # imported very early. It is guaranteed importable on any lane that composes
    # an envelope (``mission_chat_turn_context`` imports it at module scope), so
    # no defensive swallow — a silent degrade here would restore F1.
    from .runtime_hud import (
        RUNTIME_CONTEXT_CODEC,
        SKILL_PRELOAD_CODEC,
        split_composed_user_row,
    )

    text = _redacted(value)
    parts = split_composed_user_row(text)
    if not parts.has_envelope:
        bounded = _truncate(text, _MAX_CONTENT)
        if bounded == text:
            return BoundedUserContent(text=text, source_chars=len(text))
        return BoundedUserContent(
            text=bounded,
            notes=(
                ContentBoundNote(
                    part=BOUND_PART_MESSAGE,
                    action=BOUND_ACTION_TRUNCATED,
                    original_chars=len(text),
                    bounded_chars=len(bounded),
                    limit=_MAX_CONTENT,
                ),
            ),
            source_chars=len(text),
        )

    notes: list[ContentBoundNote] = []
    remaining = _MAX_USER_ROW_CONTENT

    hud, note = _bound_envelope(
        parts.runtime_context,
        limit=min(_MAX_RUNTIME_CONTEXT_CONTENT, remaining),
        part=BOUND_PART_RUNTIME_CONTEXT,
        codec=RUNTIME_CONTEXT_CODEC,
    )
    if note is not None:
        notes.append(note)
    remaining -= len(hud)

    message = _truncate(parts.message, min(_MAX_CONTENT, remaining))
    if len(message) != len(parts.message):
        notes.append(
            ContentBoundNote(
                part=BOUND_PART_MESSAGE,
                action=BOUND_ACTION_TRUNCATED,
                original_chars=len(parts.message),
                bounded_chars=len(message),
                limit=min(_MAX_CONTENT, remaining),
            )
        )
    remaining -= len(message)

    preload, note = _bound_envelope(
        parts.skill_preload,
        limit=max(0, remaining),
        part=BOUND_PART_SKILL_PRELOAD,
        codec=SKILL_PRELOAD_CODEC,
    )
    if note is not None:
        notes.append(note)

    if notes:
        logger.warning(
            "persona chat user row bounded before the wire: %s",
            ", ".join(
                f"{item.part}={item.action}({item.original_chars}->{item.bounded_chars}"
                f"/{item.limit})"
                for item in notes
            ),
        )
    return BoundedUserContent(
        text=f"{message}{preload}{hud}",
        notes=tuple(notes),
        source_chars=len(text),
    )


def _bounded_free_text(
    value: Any, *, limit: int = _MAX_CONTENT, part: str = BOUND_PART_CONTENT
) -> BoundedUserContent:
    """Bound one opaque free-text row, and SAY SO when it cuts.

    ``_safe_text`` does the same truncation and returns a bare string, which is
    how the assistant/tool/system rows have been losing content silently: the
    per-part accounting added for the composed operator row covered ``user``
    only, while the other three roles kept a flat cap with no note, no log and
    no receipt. A 25 KB tool result was cut to 20 K on the way to the model and
    nothing anywhere recorded that it had been.
    """

    text = _redacted(value)
    bounded = _truncate(text, limit)
    if bounded == text:
        return BoundedUserContent(text=text, source_chars=len(text))
    return BoundedUserContent(
        text=bounded,
        notes=(
            ContentBoundNote(
                part=part,
                action=BOUND_ACTION_TRUNCATED,
                original_chars=len(text),
                bounded_chars=len(bounded),
                limit=limit,
            ),
        ),
        source_chars=len(text),
    )


#: Name of the boundary, for logs and typed drift rows. One spelling, so a
#: search for "who decided what the model sees on this lane" lands in one place.
WIRE_BOUNDARY = "persona_chat.native_wire_row"


@dataclass(frozen=True, slots=True)
class WireBoundaryRow:
    """What the persona-chat boundary put ON THE WIRE, and what it cost.

    ``row`` is the value the flush writes back into the LIVE actor's message
    list, so it is what the next provider call submits — not merely what gets
    persisted. The remaining fields exist so that claim is checkable rather than
    asserted in a comment.
    """

    row: dict[str, Any]
    notes: tuple[ContentBoundNote, ...] = ()
    #: Length of the content handed to the boundary, before redaction.
    submitted_chars: int = 0
    #: Length after redaction — the input the BOUND was actually applied to.
    #: Redaction is a separate, intended transform; separating the two keeps a
    #: redacted secret from masquerading as an unaccounted bound loss.
    redacted_chars: int = 0
    #: Length of what goes to the model.
    wire_chars: int = 0

    @property
    def bounded(self) -> bool:
        return bool(self.notes)

    @property
    def accounted_loss(self) -> int:
        """Characters the notes explain, counting only the CONTENT parts.

        Tool-call arguments are bounded and noted too, but they live in a
        different field of the row and are NOT part of the content arithmetic.
        Summing them here would let an argument truncation cancel out a real
        content residue and drive :attr:`unaccounted_loss` to zero — a check
        that hides the thing it exists to find.
        """

        return sum(
            max(0, note.original_chars - note.bounded_chars)
            for note in self.notes
            if note.part in CONTENT_BOUND_PARTS
        )

    @property
    def argument_loss(self) -> int:
        """Characters removed from tool-call argument blobs. Reported, not netted."""

        return sum(
            max(0, note.original_chars - note.bounded_chars)
            for note in self.notes
            if note.part == BOUND_PART_TOOL_ARGUMENTS
        )

    @property
    def unaccounted_loss(self) -> int:
        """Characters that left between submission and the wire with NOTHING naming them.

        THE INVARIANT. Every character the boundary removes must be explained by
        a typed note or by redaction. A non-zero residue means content is
        reaching the model in a shape no receipt describes — the 2026-08-09
        class, where a bound that read as persistence-only silently governed the
        prompt and the receipt could only report it after the fact.
        """

        return max(0, self.redacted_chars - self.wire_chars - self.accounted_loss)

    @property
    def holds(self) -> bool:
        return self.unaccounted_loss == 0

    def drift_row(self) -> dict[str, Any] | None:
        """The fail-loud typed row, or ``None`` when the invariant holds.

        A ROW rather than a raised assertion, deliberately — see
        :func:`record_wire_boundary_drift`.
        """

        if self.holds:
            return None
        return {
            "boundary": WIRE_BOUNDARY,
            "role": str(self.row.get("role") or ""),
            "submitted_chars": self.submitted_chars,
            "redacted_chars": self.redacted_chars,
            "wire_chars": self.wire_chars,
            "accounted_loss": self.accounted_loss,
            "argument_loss": self.argument_loss,
            "unaccounted_loss": self.unaccounted_loss,
            "notes": [
                {
                    "part": note.part,
                    "action": note.action,
                    "original_chars": note.original_chars,
                    "bounded_chars": note.bounded_chars,
                    "limit": note.limit,
                }
                for note in self.notes
            ],
        }


def record_wire_boundary_drift(bound: WireBoundaryRow) -> dict[str, Any] | None:
    """Report an unaccounted wire loss loudly, and keep the turn alive.

    WHY A TYPED ROW AND NOT AN ASSERTION. This runs inside the crash-resilience
    persist, on the live agent turn loop, BEFORE the first provider call of the
    turn. Raising here would convert an accounting bug into a lost turn on a
    conversation that is otherwise fine — strictly worse than the drift being
    reported, and it would do so in the one place a user cannot retry cheaply.
    The hard equality assertion lives where it is free: the unit tests over the
    pure boundary, which is the seam the invariant actually belongs to.

    The row is also the honest shape for the check. The boundary is SUPPOSED to
    shorten content; the invariant is not "wire == submitted" but "every
    character of the difference is named". A bare assert could only express the
    former, which is why the previous receipt could report drift and never
    prevent it.
    """

    row = bound.drift_row()
    if row is None:
        return None
    logger.error(
        "persona chat wire boundary lost %d unaccounted chars on a %s row "
        "(submitted=%d redacted=%d wire=%d accounted=%d) — the model is being "
        "sent content no receipt describes",
        row["unaccounted_loss"],
        row["role"] or "?",
        row["submitted_chars"],
        row["redacted_chars"],
        row["wire_chars"],
        row["accounted_loss"],
    )
    return row


def native_wire_row(message: dict[str, Any]) -> WireBoundaryRow:
    """THE persona-chat wire boundary.

    Named for what it decides rather than where it is called from. The flush in
    ``run_agent`` writes this result back into the live actor's message list, so
    this function — not the provider call, not the composition step — is what
    settles the bytes the model receives on this lane. That coupling is the
    reason the module's bounds are wire bounds (see :data:`_MAX_CONTENT`), and
    it used to be recorded only in a comment at the call site.

    Tool structure and ordering identifiers survive, while raw/unbounded
    payloads and provider-specific residue do not. Applying this more than once
    is stable, which lets warm memory and cold persistence share the same
    boundary without representation drift.
    """

    role = str(message.get("role") or "").strip().lower()
    if role not in {"system", "user", "assistant", "tool"}:
        role = "assistant"
    # Named `submitted`, not `raw`: the loop over `tool_calls` below rebinds
    # `raw` per call, and the two must not be the same name.
    submitted = message.get("content")
    # The operator user row is the ONE composed row on this lane — a join of
    # three parts with three different contracts — so it is bounded per part
    # (see :func:`bound_composed_user_content`). Every other role is opaque free
    # text and keeps the flat bound it always had — but now reports it.
    bounded = (
        bound_composed_user_content(submitted)
        if role == "user"
        else _bounded_free_text(submitted)
    )
    content = bounded.text
    result: dict[str, Any] = {"role": role, "content": content}
    for key in (
        "tool_call_id",
        "tool_name",
        "finish_reason",
        "platform_message_id",
        "client_message_id",
        "turn_id",
        "root_chat_session_id",
    ):
        raw_value = message.get(key)
        if key == "platform_message_id" and raw_value is None:
            raw_value = message.get("message_id")
        value = safe_assignment_text(raw_value, limit=240)
        if value:
            result[key] = value
    notes: list[ContentBoundNote] = list(bounded.notes)
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        safe_calls: list[dict[str, Any]] = []
        for raw in calls[:64]:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
            call_id = safe_assignment_text(raw.get("id"), limit=240)
            name = safe_assignment_text(function.get("name") or raw.get("name"), limit=240)
            if not call_id or not name:
                continue
            # Accounted for the same reason the row content is: this rides back
            # into the live actor and reaches the model.
            arguments = _bounded_free_text(
                function.get("arguments"),
                limit=_MAX_ARGUMENTS,
                part=BOUND_PART_TOOL_ARGUMENTS,
            )
            notes += arguments.notes
            safe_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments.text},
                }
            )
        if safe_calls:
            result["tool_calls"] = safe_calls
    return WireBoundaryRow(
        row=result,
        notes=tuple(notes),
        submitted_chars=len(submitted) if isinstance(submitted, str) else 0,
        redacted_chars=bounded.source_chars,
        wire_chars=len(content),
    )


def safe_native_message(message: dict[str, Any]) -> dict[str, Any]:
    """The boundary's row, for callers that do not need the accounting.

    Kept as the name every existing call site already uses; the accounting lives
    on :func:`native_wire_row`. Callers that write this result back into a LIVE
    message list — which makes it the wire, not merely the record — should use
    the typed form and report drift.
    """

    return native_wire_row(message).row


def safe_native_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    abandoned = {
        str(item.get("client_message_id"))
        for item in messages
        if isinstance(item, dict) and item.get("abandoned")
    }
    normalized = [
        safe_native_message(raw)
        for raw in messages
        if isinstance(raw, dict)
        and str(raw.get("client_message_id") or "") not in abandoned
    ]
    # Compression children contain the protected current turn as well as the
    # preserved parent transcript. Folding a multi-pass lineage can therefore
    # surface byte-identical copies of one logical user/reply row. Collapse
    # only rows that share the stable client/role/payload identity; distinct
    # compaction summaries and tool-call messages remain intact.
    deduped: list[dict[str, Any]] = []
    seen_logical_rows: set[tuple[str, str, str]] = set()
    for item in normalized:
        client_message_id = str(item.get("client_message_id") or "")
        if client_message_id:
            logical_client_id = re.sub(r":assistant:\d+$", "", client_message_id)
            payload = json.dumps(
                {
                    "content": item.get("content"),
                    "tool_calls": item.get("tool_calls"),
                    "tool_call_id": item.get("tool_call_id"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            key = (item["role"], logical_client_id, payload)
            if key in seen_logical_rows:
                continue
            seen_logical_rows.add(key)
        deduped.append(item)
    normalized = deduped
    available_results = {
        str(item.get("tool_call_id"))
        for item in normalized
        if item.get("role") == "tool" and item.get("tool_call_id")
    }
    safe: list[dict[str, Any]] = []
    live_tool_ids: set[str] = set()
    for item in normalized:
        if item["role"] == "assistant":
            paired_calls = [
                call
                for call in item.get("tool_calls", [])
                if str(call.get("id") or "") in available_results
            ]
            if paired_calls:
                item["tool_calls"] = paired_calls
                live_tool_ids.update(str(call["id"]) for call in paired_calls)
            else:
                item.pop("tool_calls", None)
        if item["role"] == "tool" and item.get("tool_call_id") not in live_tool_ids:
            continue
        safe.append(item)
    return safe


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def native_lineage_summary(session_db: Any, root_session_id: str) -> dict[str, Any]:
    """Resolve a root's current native tip and validated compression depth."""

    root = safe_assignment_text(root_session_id, limit=240)
    tip = session_db.resolve_resume_session_id(root)
    current = tip
    depth = 0
    seen: set[str] = set()
    while current and current != root:
        if current in seen:
            raise ValueError(f"cyclic persona chat lineage for {root}")
        seen.add(current)
        row = session_db.get_session(current)
        if not isinstance(row, dict):
            raise ValueError(f"missing persona chat lineage node {current}")
        parent = safe_assignment_text(row.get("parent_session_id"), limit=240)
        if not parent:
            raise ValueError(f"persona chat tip {tip} does not descend from {root}")
        current = parent
        depth += 1
    return {"active_session_id": tip, "continuation_depth": depth}


def native_history_revision(session_db: Any, root_session_id: str) -> str:
    tip = session_db.resolve_resume_session_id(root_session_id)
    history = session_db.get_messages_as_conversation(tip, include_ancestors=True)
    payload = json.dumps(safe_native_history(history or []), sort_keys=True, separators=(",", ":"))
    return f"{tip}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


@contextmanager
def tool_execution_scope(scope_id: str | None) -> Iterator[None]:
    token = _TOOL_EXECUTION_SCOPE.set(safe_assignment_text(scope_id, limit=240))
    try:
        yield
    finally:
        _TOOL_EXECUTION_SCOPE.reset(token)


def current_tool_execution_scope() -> str | None:
    return _TOOL_EXECUTION_SCOPE.get()


@contextmanager
def chat_root_session_key_scope(scope_id: str | None) -> Iterator[None]:
    """Bind ``tools.approval``'s session-key ContextVar to this run's chat root.

    Session-scoped upstream surfaces resolve their identity through
    ``tools.approval.get_current_session_key``: the process registry stamps it
    onto every background spawn as ``session_key``, ``delegate_task`` records it
    on delegations, and the terminal tool keys cwd records by it. On the
    mission-chat lane nothing ever bound it, so a terminal spawned from a chat
    turn wrote ``session_key: ""`` — which meant (live incident 2026-08-11,
    ``proc_b0593bc9fb0e``):

    * its ``notify_on_complete`` completion event named no session that
      resolves to a persona chat root, so serve's completion drain
      (``dispatch_delivery._chat_root_of_completion`` — positive proof only,
      the #64484 no-guessing rule) re-queued it every pass forever, and the
      ledgerless event evaporated on the next serve restart with the output
      tail it carried;
    * the ``running_work`` projection could not attribute the row to an agent
      (the ``1c0e95bc3`` commit message predicted exactly this while wiring
      the resolver).

    Binding the ROOT chat session id here is positive knowledge at spawn time,
    not drain-time guessing: the run knows which chat root it executes under.
    Bound only for ids that name a persona chat root — this scope exists to
    make attribution resolvable, and pushing any other string into upstream's
    session-key surface would widen its meaning for no gain. ContextVar
    propagation into tool worker threads is owned by
    ``tools.thread_context.propagate_context_to_thread`` (copy_context), the
    same mechanism the terminal envelope scope already rides on this lane.

    Permission posture is unchanged by design: gated terminal commands on the
    governed mission-chat lane are decided by ``agent_runtime.terminal_envelope``,
    which does not consult the approval session key.
    """

    key = safe_assignment_text(scope_id, limit=240) if scope_id else ""
    if not key or not key.startswith("persona_chat_"):
        yield
        return
    try:
        from tools.approval import (
            reset_current_session_key,
            set_current_session_key,
        )
    except Exception:  # pragma: no cover - upstream surface unavailable
        yield
        return
    token = set_current_session_key(key)
    try:
        yield
    finally:
        reset_current_session_key(token)


class PersonaChatBusyError(RuntimeError):
    error_kind = "chat_busy"

    def __init__(self, root_session_id: str, owner: dict[str, Any] | None = None):
        super().__init__(f"persona chat root is busy: {root_session_id}")
        self.root_session_id = root_session_id
        self.owner = dict(owner or {})


def _root_stem(root: str) -> str:
    prefix = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in root)[:80]
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
    return f"{prefix or 'chat'}_{digest}"


def _lease_paths(root: str) -> tuple[Path, Path]:
    base = paths.store_root() / "persona_chat_leases"
    stem = _root_stem(root)
    return base / f"{stem}.lock", base / f"{stem}.owner.json"


def _try_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def persona_chat_root_lease(
    root_session_id: str,
    *,
    owner_id: str | None = None,
    observer_kind: str = "cli",
    timeout_seconds: float = 0.0,
) -> Iterator[dict[str, Any]]:
    """Hold the OS-backed root lease for an entire native turn."""

    root = safe_assignment_text(root_session_id, limit=240)
    if not root:
        raise ValueError("root_session_id is required")
    lock_path, owner_path = _lease_paths(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    acquired = False
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    try:
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    owner = None
                    try:
                        owner = json.loads(owner_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                    raise PersonaChatBusyError(root, owner)
                time.sleep(0.01)
        owner = {
            "root_chat_session_id": root,
            "owner_id": safe_assignment_token(owner_id) or f"pid-{os.getpid()}",
            "observer_kind": safe_assignment_token(observer_kind) or "cli",
            "pid": os.getpid(),
            "acquired_at": time.time(),
        }
        tmp = owner_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(owner, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(owner_path))
        yield owner
    finally:
        # Control flow is UNCHANGED: both arms still swallow, the release order
        # is still unlink-owner → unlock → close, and ``os.close(fd)`` is still
        # always reached. What is new is that neither failure is silent any
        # more. This is the producer-side half of the stale-lock discriminator:
        # a WARNING here, correlated by root and time with the delivery drain's
        # ``lease_busy_ownerless``, turns a hypothesis into a diagnosis.
        if acquired:
            try:
                owner_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "persona chat root lease owner file for %s could not be removed"
                    " (%s) — release continues",
                    root,
                    exc,
                    exc_info=True,
                )
            try:
                _unlock(fd)
            except OSError as exc:
                logger.warning(
                    "persona chat root lease byte-unlock FAILED for %s (%s) — the lock"
                    " will be released by handle close, whose timing Windows does not"
                    " guarantee; if the delivery drain then reports"
                    " lease_busy_ownerless, this line is the cause",
                    root,
                    exc,
                )
        os.close(fd)


def repair_orphaned_chat_turns() -> list[str]:
    """Settle in-flight turn records whose executor process died (boot sweep).

    A native turn holds the OS-backed root lease for its ENTIRE execution and
    the kernel releases the lock when the holding process dies, so "in-flight
    record AND acquirable lease" is proof the turn can no longer settle itself
    (live incident 2026-07-25: the Stage C MCP flow reaped the Launcher, which
    took the serve child executing a QA relay turn with it; the record froze
    at ``executing`` and the console showed a running turn forever). A session
    whose lease is HELD is a live turn in another process and is skipped.

    Runs at ``harness serve`` boot — the moment a launcher restart replaces a
    dead runtime — before the first hydrate is served, so the repaired records
    project as typed ``turn_interrupted`` markers instead of frozen output.
    When anything flips, a ``state.reconciled`` event is appended so any
    already-connected watermark-gated consumer converges too (turn files are
    not patch-covered). Best-effort per session; the next boot retries.
    """

    from .mission_chat_turns import (
        inflight_chat_session_roots,
        mark_stale_inflight_turns_interrupted,
    )

    repaired: list[str] = []
    for root in inflight_chat_session_roots():
        try:
            with persona_chat_root_lease(root, observer_kind="orphan_sweep"):
                flipped = mark_stale_inflight_turns_interrupted(
                    session_id=root,
                    active_client_message_id=None,
                )
        except PersonaChatBusyError:
            continue
        except Exception:  # noqa: BLE001 — sweep must never block serve boot
            continue
        if flipped:
            repaired.append(root)
    if repaired:
        try:
            from hermes_time import now

            from .events import EventLog
            from .models import Event

            digest = hashlib.sha1("|".join(sorted(repaired)).encode("utf-8")).hexdigest()[:16]
            EventLog().append(
                Event(
                    now(),
                    "state.reconciled",
                    None,
                    None,
                    None,
                    {"fingerprint": digest, "source": "chat_orphan_sweep"},
                )
            )
        except Exception:  # noqa: BLE001 — the repair itself already landed
            pass
    return repaired


class PersonaChatMintReceiptStore:
    def _path(self, instance_id: str, key: str) -> Path:
        digest = hashlib.sha256(f"{instance_id}\0{key}".encode("utf-8")).hexdigest()
        return paths.store_root() / "persona_chat_mint_receipts" / f"{digest}.json"

    def mint(
        self,
        *,
        instance_store: PersonaInstanceStore,
        session_db: Any,
        persona_id: str,
        persona_instance_id: str,
        idempotency_key: str,
        title: str | None = None,
        dispatched_from: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reserve-then-create the instance's canonical chat root.

        ``dispatched_from`` records task-scoped dispatch lineage — the thread
        this fresh session superseded, and the sender session that asked for it
        — into the session meta as ``_dispatched_from``. It is honoured by the
        mint that CREATES the thread; a replay of the same idempotency key
        ignores it and carries the stored block forward unchanged. It rides the
        SAME meta write as ``mission_chat_root_id`` (one write, one authority)
        rather than a second update, and deliberately does NOT touch
        ``parent_session_id``:
        on the persona-chat lane that column is claimed by native-compression
        lineage (``native_lineage_summary`` raises on a foreign parent, and
        usage aggregation blanks), so borrowing it for relay provenance would
        corrupt both. Marker-key precedent: ``_delegate_from`` / ``_branched_from``.

        Order of operations is load-bearing, not incidental: assert bindable →
        reserve the receipt → BIND → create the session → meta → title. The bind
        used to be last, which is why a ``retire`` landing mid-lane still left a
        titled thread behind for a placement that no longer existed. See the
        early-bind comment below.

        That ordering's own cost — a bound pointer standing for the instant
        before its transcript row exists — is paid by RETRACTING the bind when
        the row cannot be written (``rollback_chat_root_bind``), and the failure
        then surfaces as :class:`PersonaChatPersistenceError` rather than as a
        raw storage exception. The receipt stays ``reserved`` through that
        failure on purpose: a same-key retry resolves the SAME root and
        completes, so a crash in the same window (which no in-process rollback
        can cover) is repaired rather than duplicated.
        """

        instance_id = safe_assignment_token(persona_instance_id)
        key = safe_assignment_text(idempotency_key, limit=240)
        if not instance_id or not key:
            raise ValueError("persona_instance_id and idempotency_key are required")
        # PRECONDITION, asserted before this lane's FIRST durable write, through
        # the SAME seam the bind itself uses (``assert_bindable`` → one
        # derivation of the target id, one retirement rule, one refusal). It
        # reports the id the seam resolved, not the caller's raw token: a refusal
        # reachable from three sites must not identify its target three ways.
        #
        # It closes the refusal that is TRUE AT ENTRY — the target was already
        # retired when the mint arrived, which is every dispatch at a deleted
        # placement. The narrower race (a ``retire`` landing WHILE this lane
        # runs) is closed by the early bind below, not here.
        #
        # The local ``instance_id`` deliberately stays the CALLER's token: it
        # keys the idempotency receipt path and derives the root session id, and
        # canonicalizing it here would re-key every in-flight receipt — a replay
        # would miss its own reservation and mint a SECOND thread for a task
        # that already has one.
        #
        # The id it RESOLVES is kept: the retraction below has to name the row
        # ``open_chat`` will write, and re-deriving it there would be the second
        # derivation this seam exists to prevent.
        bind_target_id = instance_store.assert_bindable(
            persona_id=persona_id, persona_instance_id=instance_id
        )
        path = self._path(instance_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:
                    time.sleep(0.01)
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                receipt = {}
            root = safe_assignment_text(receipt.get("root_chat_session_id"), limit=240)
            # THE replay signal: a receipt that already names a root is one this
            # key established on an earlier pass. Anything a retry must NOT
            # re-derive branches on this fact, never on the shape of the values
            # the retry happened to compute.
            replayed = bool(root)
            if not replayed:
                root = persona_chat_session_id_for(instance_id)
                receipt = {
                    "schema_version": 1,
                    "persona_instance_id": instance_id,
                    "idempotency_key": key,
                    "root_chat_session_id": root,
                    "state": "reserved",
                    "created_at": time.time(),
                }
                _atomic_json(path, receipt)
            # EARLY BIND. This used to be the LAST step of the lane, and that
            # position was the whole remaining defect: ``retire`` refuses only on
            # a live run/worker binding or an active assignment, and a chat mint
            # held neither until it bound — so a ``retire`` landing after the
            # precondition above still let the lane create the session, write its
            # meta and TITLE it before the bind refused, leaving a titled thread
            # in Mission Control for a placement that no longer exists.
            #
            # Binding first inverts that. The bind is the step that proves the
            # target is still live, so it runs before the first SESSION-visible
            # write; from here on every write belongs to a target this lane has
            # already proven bindable. A ``retire`` that lands after this point
            # archives a row that legitimately owned the thread — its tombstone
            # carries the pointer, and preserved chat history is exactly what
            # ``retire`` promises. No orphan either way.
            #
            # Ordered AFTER the receipt reservation on purpose: the receipt is
            # what makes the whole lane idempotent, it is internal (never a
            # Mission Control row), and reserving first means a crash between the
            # two is repaired by a retry that resolves the same root instead of
            # minting a second one.
            #
            # …and the price of binding first is a WINDOW: from here until the
            # row below is written, the instance points at a root no transcript
            # store holds. That is the phantom
            # ``agent_runtime.persona_chat_durability`` was written to make
            # impossible on the create lane, and it is permanent —
            # ``resolve_default_chat_session_id_for_instance`` re-offers a
            # chat-shaped own-instance pointer forever without ever asking
            # whether it resolves, so every later ``mission-chat message`` is
            # refused ``unknown_chat_session``.
            #
            # The create lane closed it by persisting AT the bind argument. This
            # lane cannot: the ordering above is load-bearing, and its
            # ``session_db`` is the CALLER's handle (dispatch, relay and argv
            # each pass their own), so routing through
            # ``ensure_durable_persona_chat_root`` — which acquires the process
            # DEFAULT store — would write the row into a different database from
            # the one this mint's own reader dereferences. The window is closed
            # from the other end instead: the bind is RETRACTED when the write it
            # was ordered ahead of cannot be made.
            #
            # ``previous_row`` is the row as it stood BEFORE the bind, ``None``
            # when the bind is what creates it; the retraction restores exactly
            # that, and only while the live pointer still names OUR root.
            try:
                previous_row = instance_store.get(bind_target_id)
            except Exception:
                previous_row = None
            instance = instance_store.open_chat(
                persona_id=persona_id,
                persona_instance_id=instance_id,
                session_id=root,
            )
            try:
                session_db.create_session(
                    session_id=root,
                    source=PERSONA_CHAT_SESSION_SOURCE,
                    model=None,
                    system_prompt=f"Mission Control persona chat for {persona_id}",
                )
            except Exception as exc:
                instance_store.rollback_chat_root_bind(
                    persona_instance_id=bind_target_id,
                    root_session_id=root,
                    previous=previous_row,
                )
                # Typed, and chained: this lane is reached over RPC and from
                # argv, and a bare storage exception escaping it reaches the
                # operator as a traceback instead of the persistence frame the
                # chat lanes already speak. Lazy import —
                # ``persona_chat_durability`` imports THIS module, so a
                # module-level import would close the cycle.
                from .persona_chat_durability import persona_chat_persistence_failed

                persona_chat_persistence_failed("session_create", exc, required=True)
                raise  # pragma: no cover — the helper always raises when required
            meta = {
                "mission_chat_root_id": root,
                "persona_instance_id": instance_id,
                "source": PERSONA_CHAT_SESSION_SOURCE,
            }
            if replayed:
                # REPLAY: lineage is established ONCE, by the mint that created
                # the thread, and is never recomputed. A retry cannot re-derive
                # it, because both inputs have gone stale in ways that lie:
                #   * the caller's `predecessor` was read from the instance's
                #     default-thread pointer, which this very session has since
                #     BECOME — so on a same-key retry it is a self-reference
                #     (dropped), and after an INTERLEAVED later dispatch it is
                #     that later thread, which this session never superseded.
                #     Recording it would invert the arrow (A claiming it retired
                #     B when A came first).
                #   * `requested_by_session` belongs to whoever asked for the
                #     ORIGINAL mint; the retry's sender is not that fact.
                # And this meta write replaces the stored value wholesale, so
                # anything not carried forward is ERASED. Carry it forward
                # unconditionally: the stored block is the whole truth, and an
                # empty one means the thread was born without lineage. (A mint
                # that died between reserving the receipt and writing this meta
                # therefore keeps no lineage — silence, never a fabricated arrow.)
                lineage = _stored_dispatch_lineage(session_db, root)
            else:
                lineage = _dispatch_lineage_meta(dispatched_from, root=root)
            if lineage:
                meta["_dispatched_from"] = lineage
            session_db.update_session_meta(root, json.dumps(meta, sort_keys=True))
            if title and not session_db.get_session_title(root):
                try:
                    session_db.set_session_title(root, title)
                except Exception as exc:
                    if "already in use" not in str(exc).lower():
                        raise
                    session_db.set_session_title(root, f"{title} · {root[-8:]}")
            receipt.update({"state": "completed", "completed_at": time.time()})
            _atomic_json(path, receipt)
            return {
                **receipt,
                "default_chat_session_id": instance.default_chat_session_id,
                # The lineage this mint actually RECORDED (not the lineage it was
                # asked for), so the reply envelope reports the same predecessor
                # the session meta holds — on a first mint and on a replay alike.
                # Not persisted into the receipt file: the meta is its home.
                "dispatched_from": dict(lineage),
            }
        finally:
            try:
                _unlock(fd)
            except OSError:
                pass
            os.close(fd)


#: How long a clarify ticket file is kept before the sweep may prune it. TTL
#: governs GARBAGE COLLECTION ONLY — an expired-but-present ticket still binds
#: (see :class:`PersonaChatClarifyTicketStore`). One rule, no cliff.
CLARIFY_TICKET_TTL_SECONDS = 604_800  # 7 days

#: ``clarify-<12 hex>`` — mirrors the relay id precedent
#: (``agent-relay-<12 hex>``, ``tools/agent_chat_tool.py``). 48 bits is ample:
#: the token is a LOOKUP KEY validated against a stored record, never a
#: capability secret. The session it resolves to is independently re-validated
#: by the handler's existing ``unknown_chat_session`` / ``foreign_chat_session``
#: guards, so guessing a token buys nothing a caller could not already name.
CLARIFY_TOKEN_PREFIX = "clarify-"

#: Ticket lifecycle states. ``open`` → the question is unanswered; ``answered``
#: → some turn landed in its session (with or without an echoed token);
#: ``rebound`` → a later, DIFFERENT turn bound through the same token.
CLARIFY_TICKET_OPEN = "open"
CLARIFY_TICKET_ANSWERED = "answered"
CLARIFY_TICKET_REBOUND = "rebound"


class PersonaChatClarifyTicketStore:
    """Binds a clarify ANSWER to the thread its QUESTION was asked in.

    A child that calls ``clarify`` on the mission-chat lane has its question
    threaded back to the asker as ``clarify_request``. Until this store existed,
    the ONLY thing linking that question to its answer was the enclosing
    ``session_id``, and passing it back was enforced by prompt text alone — so a
    parent that omitted it fell through to ``policy_new_per_dispatch`` and the
    child read a bare choice with no question attached. Continuity that depends
    on a model reproducing an opaque identifier is not continuity; the runtime
    owns the binding now.

    A sidecar keyed store, deliberately NOT session meta:
    :meth:`PersonaChatMintReceiptStore.mint` writes meta WHOLESALE
    (``update_session_meta(root, json.dumps(meta))``), which is exactly why
    ``640131e8c`` had to add carry-forward logic for ``_dispatched_from``. A
    ticket parked in meta would be erased by the next mint against that root.

    **The filename is the digest of the token, never the token itself.** The
    token arrives as a caller-supplied string from a model; interpolating it
    into a path is traversal. Same precedent as the mint receipt store above.
    """

    def _root_dir(self) -> Path:
        return paths.store_root() / "persona_chat_clarify_tickets"

    def _path(self, token: str) -> Path:
        digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        return self._root_dir() / f"{digest}.json"

    # ── Open-ticket index (by session) ──────────────────────────────────────
    #
    # WHY THIS EXISTS. ``open_ticket_for_session`` is the tokenless-settlement
    # lookup and it runs on nearly EVERY mission-chat turn. Reading it as a glob
    # over the ticket directory made each turn pay for every question ever asked
    # inside the live TTL window — a per-turn cost that grows with a store that
    # is supposed to be write-rare. The index answers the same question in a
    # bounded number of file reads: one index file, then the newest candidate
    # ticket it names.
    #
    # AUTHORITY AND SELF-HEALING. The index is a CACHE OF POINTERS, never a
    # second source of truth. Every token it hands back is re-read from its own
    # ticket file and re-verified (``state == open`` AND the record's own
    # ``chat_session_id`` matches) before it can bind anything, so a stale,
    # hand-edited, or half-written entry can only ever produce a MISS — never a
    # wrong binding. Entries that fail that verification are dropped on the way
    # past, so the index converges without a repair pass.
    #
    # THE MARKER FILE is what makes "no index file for this session" mean "no
    # open tickets" rather than "index lost". It is written LAST, after a full
    # rebuild from the ticket files themselves, so a crashed rebuild simply
    # leaves no marker and the next call redoes it (idempotent). Without it, a
    # session with no tickets — the overwhelmingly common case — could never be
    # told from a missing index and would fall back to the glob on every turn,
    # which is the cost this exists to retire.
    #
    # THAT CLAIM IS RETRACTABLE, and must be. Being a cache saves this index
    # from every failure that leaves it with an entry too MANY — the read path
    # verifies those away. Nothing saves it from an entry too FEW, because the
    # marker is precisely what stops anyone from going and looking. So the two
    # writes that can lose a pointer take the claim back rather than keep it:
    # a failed add (:meth:`_index_add`) and a rebuild racing a concurrent mint
    # (:meth:`_rebuild_index`, which unions instead of replacing). One extra
    # rebuild is the price; a permanently invisible open ticket is not.

    def _index_dir(self) -> Path:
        return self._root_dir() / "by_session"

    def _index_path(self, chat_session_id: str) -> Path:
        digest = hashlib.sha256(str(chat_session_id or "").encode("utf-8")).hexdigest()
        return self._index_dir() / f"{digest}.json"

    def _index_state_path(self) -> Path:
        # Leading underscore, never a sha256 hex digest, so it cannot collide
        # with a per-session file.
        return self._index_dir() / "_index_state.json"

    def _index_entries(self, chat_session_id: str) -> list[dict[str, Any]]:
        """The open-token pointers recorded for *chat_session_id*, newest first.

        "Could not read" collapses to "none recorded" here, which is the right
        answer for every caller that only READS the result — the lookup misses
        once and re-reads next turn, a cost it pays rather than a pointer it
        drops. It is the wrong answer for a caller about to write the list back,
        and that caller must use :meth:`_read_index` instead."""

        return self._read_index(chat_session_id) or []

    def _read_index(self, chat_session_id: str) -> list[dict[str, Any]] | None:
        """The recorded pointers, or ``None`` when the file could not be read.

        UNREADABLE IS NOT EMPTY, exactly as unreadable is not dead in
        :meth:`_sweep_index`. A file that is simply absent legitimately means
        "no pointers recorded" and answers ``[]``; a file that IS there but
        would not read has told us nothing, and answering ``[]`` for it hands a
        read-modify-write caller a blank slate to overwrite live pointers with.
        The failure is the ordinary Windows one the add path already documents
        for its WRITE — an AV or indexer holding the file for the instant we
        touch it — not corruption, and not crash-only."""

        try:
            text = self._index_path(chat_session_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            # The one failure that IS an answer: nothing was ever recorded.
            return []
        except Exception:
            return None
        try:
            payload = json.loads(text)
        except Exception:
            return None
        raw = payload.get("open_tokens") if isinstance(payload, dict) else None
        entries = [
            {
                "clarify_token": str(item.get("clarify_token") or ""),
                "created_at": float(item.get("created_at") or 0.0),
            }
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict) and item.get("clarify_token")
        ]
        entries.sort(key=lambda item: item["created_at"], reverse=True)
        return entries

    @staticmethod
    def _index_payload(chat_session_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """The on-disk shape of one per-session index file. ONE writer of it.

        ``newest_created_at`` is the file's own record of the newest ticket it
        names, and it is what :meth:`_sweep_index` reclaims by. Recording it
        here rather than inferring it from the filesystem is the point: an
        mtime is a property of the FILE, which any backup, restore, sync, or
        copy tool is free to rewrite without touching a byte of content, while
        this is a property of the DATA and travels with it.

        Written by :meth:`_write_index` and :meth:`_rebuild_index` alike, so
        the two cannot disagree about the shape — the drift that made the
        rebuild's hand-built dict a second spelling of this one.
        """

        ordered = sorted(entries, key=lambda item: item["created_at"], reverse=True)
        return {
            # v2 adds ``newest_created_at``. Purely additive: a v1 file still
            # reads correctly (the field's absence is DERIVED from the entries,
            # see :meth:`_index_newest_created_at`), so there is no migration
            # pass and no rebuild owed.
            "schema_version": 2,
            "chat_session_id": str(chat_session_id),
            "newest_created_at": float(ordered[0]["created_at"]) if ordered else 0.0,
            "open_tokens": ordered,
        }

    def _write_index(self, chat_session_id: str, entries: list[dict[str, Any]]) -> bool:
        """Record *entries* as the open pointers for *chat_session_id*.

        Returns ``False`` when they could not be recorded at all. Whether that
        is survivable depends entirely on the direction: a failed DROP leaves a
        pointer the read path verifies away anyway, a failed ADD loses one
        outright — see :meth:`_index_add`."""

        path = self._index_path(chat_session_id)
        if not entries:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return False
            return True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(path, self._index_payload(chat_session_id, entries))
        except OSError:
            return False
        return True

    def _index_newest_created_at(self, path: Path) -> float | None:
        """The newest ticket ``created_at`` the index file at *path* names.

        ``None`` means the file could not be read — which is NOT the same
        answer as ``0.0`` and must never be treated as one: a transient read
        failure (the ordinary Windows AV/indexer case) would otherwise present
        a live index file as a dead one and have the sweep delete it, losing
        pointers the marker still swears are there.

        A ``schema_version`` 1 file predates the recorded field; the same fact
        is derivable from the entries it names, so those files answer honestly
        too and converge to v2 on their next write. Either way the answer comes
        from the file's CONTENT, never from its mtime.
        """

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            recorded = float(payload.get("newest_created_at") or 0.0)
        except (TypeError, ValueError):
            recorded = 0.0
        if recorded > 0.0:
            return recorded
        tokens = payload.get("open_tokens")
        newest = 0.0
        for item in tokens if isinstance(tokens, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                newest = max(newest, float(item.get("created_at") or 0.0))
            except (TypeError, ValueError):
                continue
        return newest

    def _rebuild_index(self) -> bool:
        """Rebuild every per-session index from the ticket files. Idempotent.

        Runs once per store (on the first mint or lookup after this code
        arrives, and again after any crash that left no marker). The marker is
        written LAST so a partial rebuild is simply redone rather than trusted.

        REFUSES TO CLAIM COMPLETENESS when any ticket file will not decode. The
        marker this method writes is not a cache entry, it is the sentence "the
        index may be trusted as complete" (:meth:`_ensure_index`), and it is
        PERMANENT — once written, every later lookup answers from the index and
        the full-scan fallback is never taken again. So a ticket skipped during
        the rebuild is not skipped once, it is skipped forever: the tokenless
        settlement stops finding it, the turn that would have closed it opens a
        second ticket for the same question, and no later pass can recover it
        because nothing will ever rebuild again. Since the skip is usually a
        transient hold on a file that reads fine a second later, that is a
        permanent hole punched by a momentary failure.

        Returning ``False`` is not a new failure mode — it is the one this
        method already has, and its contract is exactly right here: the caller
        falls back to the full scan, which reads the store directly and is
        correct by construction. The store keeps working, unindexed, until the
        unreadable file is repaired or removed; correctness never depended on
        the index existing.
        """

        records, unreadable = self.scan_records()
        if unreadable:
            return False
        by_session: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record.get("state") != CLARIFY_TICKET_OPEN:
                continue
            root = str(record.get("chat_session_id") or "")
            token = str(record.get("clarify_token") or "")
            if not root or not token:
                continue
            by_session.setdefault(root, []).append(
                {"clarify_token": token, "created_at": float(record.get("created_at") or 0.0)}
            )
        try:
            self._index_dir().mkdir(parents=True, exist_ok=True)
            for root, entries in by_session.items():
                # UNION with what is already recorded, never a replacement. The
                # scan above is a SNAPSHOT, and a mint that lands after it but
                # before this write would otherwise have its pointer overwritten
                # by the pre-mint view — and lost for good, because the marker
                # written below then stops any later rebuild from recovering it.
                # That is not merely a miss: the lookup goes on answering with an
                # OLDER open ticket that the pre-index glob would never have
                # returned, so the tokenless settlement closes the wrong
                # question. A union can only ever ADD a pointer, and one the
                # ticket files do not back is dropped on the read path like every
                # other stale entry — the same verification that makes this
                # index a cache and not an authority.
                merged = {entry["clarify_token"]: entry for entry in entries}
                for entry in self._index_entries(root):
                    merged.setdefault(entry["clarify_token"], entry)
                _atomic_json(
                    self._index_path(root),
                    self._index_payload(root, list(merged.values())),
                )
            _atomic_json(
                self._index_state_path(), {"schema_version": 1, "rebuilt_at": time.time()}
            )
        except OSError:
            return False
        return True

    def _ensure_index(self) -> bool:
        """``True`` when the by-session index may be trusted as complete.

        ``False`` means the caller must fall back to the scan — correctness
        never depends on the index being present."""

        try:
            if self._index_state_path().exists():
                return True
        except OSError:
            return False
        return self._rebuild_index()

    def _invalidate_index(self) -> None:
        """Retract the completeness claim, forcing ONE rebuild on the next read.

        The marker is the whole reason "no index file for this session" may be
        read as "no open tickets". Anything that leaves that claim false has to
        take the claim back, or the index goes on answering a question it can no
        longer answer."""

        try:
            self._index_state_path().unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive; the ticket is what matters
            pass

    def _index_add(self, chat_session_id: str, token: str, created_at: float) -> None:
        entries = self._read_index(chat_session_id)
        if entries is None:
            # A READ WE COULD NOT MAKE IS NOT AN EMPTY INDEX, and this is the
            # one caller where the difference costs a ticket: the list is about
            # to be written back WHOLESALE, so treating an unreadable file as
            # "nothing recorded" replaces every pointer it held with just this
            # one — while the marker goes on swearing the index is complete, so
            # the tokenless settlement never looks for the others again and
            # nothing rebuilds. Same silent permanent miss as a swallowed add,
            # same answer: retract the claim and let ONE rebuild re-derive every
            # pointer from the ticket files, which are the authority the index
            # only ever cached. Deliberately no write — a list we know is
            # missing entries is not a better record than the one on disk.
            self._invalidate_index()
            return
        if any(entry["clarify_token"] == token for entry in entries):
            return
        entries.append({"clarify_token": token, "created_at": float(created_at)})
        entries.sort(key=lambda item: item["created_at"], reverse=True)
        if not self._write_index(chat_session_id, entries):
            # A SWALLOWED ADD IS A SILENT PERMANENT MISS. The ticket is on disk
            # and open, the marker still swears the index is complete, so the
            # tokenless settlement never looks for it again and nothing ever
            # rebuilds — and the loss lands squarely on the one number the
            # adoption readout exists to report. A transient replace failure is
            # not exotic on Windows (an AV or indexer holding the target is the
            # ordinary case), so this cannot be reasoned away as crash-only.
            # Retracting the marker turns it into ONE extra rebuild on the next
            # lookup, which re-derives the pointer from the ticket file that IS
            # there. The marker exists so the scan is not paid ROUTINELY, not so
            # it is never paid at all.
            self._invalidate_index()

    def _index_drop(self, chat_session_id: str, token: str) -> None:
        entries = self._index_entries(chat_session_id)
        remaining = [entry for entry in entries if entry["clarify_token"] != token]
        if len(remaining) != len(entries):
            # A failed drop is survivable and deliberately NOT invalidating: the
            # entry it left behind names a settled ticket, which the read path
            # re-verifies and discards. Only a lost ADD costs a ticket.
            self._write_index(chat_session_id, remaining)

    @staticmethod
    def new_token() -> str:
        import uuid

        return f"{CLARIFY_TOKEN_PREFIX}{uuid.uuid4().hex[:12]}"

    def mint(
        self,
        *,
        chat_session_id: str,
        persona_instance_id: str | None = None,
        persona_id: str | None = None,
        asked_by_client_message_id: str | None = None,
        asked_turn_id: str | None = None,
        requested_by_session: str | None = None,
    ) -> str | None:
        """Record a clarify ticket for the question this turn is asking.

        Returns the token to ship down inside ``clarify_request``, or ``None``
        when there is no session to bind to or the write failed. Best-effort by
        construction: a ticket that cannot be written must not fail the turn
        that produced a real reply — the caller degrades to today's precedence,
        which is exactly the pre-token behavior."""

        root = safe_assignment_text(chat_session_id, limit=240)
        if not root:
            return None
        token = self.new_token()
        record = {
            "schema_version": 1,
            "clarify_token": token,
            "chat_session_id": root,
            "persona_instance_id": safe_assignment_token(persona_instance_id) or None,
            "persona_id": safe_assignment_token(persona_id) or None,
            "asked_by_client_message_id": safe_assignment_text(
                asked_by_client_message_id, limit=240
            )
            or None,
            "asked_turn_id": safe_assignment_text(asked_turn_id, limit=240) or None,
            "requested_by_session": safe_assignment_text(requested_by_session, limit=240)
            or None,
            "state": CLARIFY_TICKET_OPEN,
            "created_at": time.time(),
            "answered_at": None,
            "answered_by_client_message_id": None,
            "bound_via": None,
        }
        path = self._path(token)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(path, record)
        except OSError:
            return None
        # GC RUNS HERE, on the mint, because this is the only cold seam in the
        # lane. The TTL was defined with no caller, so nothing ever pruned: the
        # directory grew for the life of the runtime, and since
        # ``open_ticket_for_session`` reads EVERY file in it, the tokenless
        # settlement on every mission-chat turn paid for every ticket ever
        # minted (measured: ~4s per turn at 400 tickets). A question is asked
        # far more rarely than a turn is taken, and this one already writes, so
        # it is the seam that can afford the scan the hot path cannot.
        #
        # After the write, never before: a sweep that ran first would still
        # leave this ticket unpruned, and one that ran on the read path would
        # put the cost back where it must not be. The just-written ticket is
        # never its own victim — ``created_at`` is now, and the cutoff is a full
        # TTL behind it. Best-effort: reclaiming disk must not fail the question
        # that was actually asked.
        try:
            self.sweep()
        except OSError:  # pragma: no cover - defensive; GC must never fail a turn
            pass
        # Index AFTER the ticket exists, so a crash between the two can only
        # lose the pointer (the tokenless settlement misses; the token itself
        # still binds), never publish a pointer to a ticket that is not there.
        # A rebuild triggered here already contains this ticket — the add is
        # idempotent, so the two cannot double-count.
        try:
            if self._ensure_index():
                self._index_add(root, token, float(record["created_at"]))
        except OSError:  # pragma: no cover - defensive; the ticket is what matters
            pass
        return token

    def resolve(self, token: str | None) -> dict[str, Any] | None:
        """The stored ticket for *token*, or ``None`` for unknown/unreadable.

        NEVER RAISES. An unknown or GC'd token must DEGRADE (the caller falls
        through to normal precedence and reports ``unknown_token``), never
        refuse: turning a pruned ticket into a hard failure would punish a
        parent that did exactly the right thing."""

        candidate = safe_assignment_text(token, limit=240)
        if not candidate:
            return None
        try:
            record = json.loads(self._path(candidate).read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(record, dict):
            return None
        # A record whose stored token does not match is not this token's ticket:
        # a digest collision or a hand-edited file must never bind a turn onto
        # somebody else's thread.
        if str(record.get("clarify_token") or "") != candidate:
            return None
        return record

    def settle(
        self,
        token: str | None,
        *,
        client_message_id: str | None = None,
        bound_via: str = "clarify_token",
    ) -> dict[str, Any] | None:
        """Mark the ticket answered, idempotently, and report its new state.

        Re-presentation with the SAME ``client_message_id`` (a lease re-entry, a
        relay retry) settles identically — the same replay-signal discipline the
        mint receipt uses. A settle from a DIFFERENT message is a genuine second
        answer and is recorded as ``rebound``: visible, never an error, because
        binding a token to its live thread is always the right outcome. The
        returned dict is the record as it now stands (or ``None`` when the token
        does not resolve)."""

        record = self.resolve(token)
        if record is None:
            return None
        message_id = safe_assignment_text(client_message_id, limit=240) or None
        already = record.get("answered_by_client_message_id")
        if record.get("state") == CLARIFY_TICKET_OPEN:
            record["state"] = CLARIFY_TICKET_ANSWERED
        elif already and message_id and already != message_id:
            record["state"] = CLARIFY_TICKET_REBOUND
        if not already or (message_id and already == message_id):
            record["answered_by_client_message_id"] = message_id or already
            record["answered_at"] = record.get("answered_at") or time.time()
            record["bound_via"] = record.get("bound_via") or str(bound_via)
        try:
            _atomic_json(self._path(str(record.get("clarify_token"))), record)
        except OSError:
            pass
        # A settled ticket is no longer a candidate for the tokenless lookup, so
        # it leaves the index. Best-effort: a failure here costs one wasted
        # verification on the next lookup, which then drops the entry itself.
        if record.get("state") != CLARIFY_TICKET_OPEN:
            try:
                self._index_drop(
                    str(record.get("chat_session_id") or ""),
                    str(record.get("clarify_token") or ""),
                )
            except OSError:  # pragma: no cover - defensive
                pass
        return record

    def open_ticket_for_session(self, chat_session_id: str | None) -> dict[str, Any] | None:
        """The newest OPEN ticket bound to *chat_session_id*, if any.

        Settlement without a token: any turn landing in a session with an open
        ticket settles it. Without this, every prompt-compliant-via-``session_id``
        parent would leave a permanently-open ticket and the adoption metric
        would lie in the pessimistic direction.

        THIS RUNS ON NEARLY EVERY MISSION-CHAT TURN, so it answers from the
        by-session index — one index read plus the newest ticket it names — and
        falls back to the full scan only when the index cannot be established.
        The index is never trusted on its own: every token it offers is re-read
        and re-verified against its own record, so the worst a stale entry can
        do is cost one wasted read and get itself dropped."""

        root = safe_assignment_text(chat_session_id, limit=240)
        if not root:
            return None
        if not self._ensure_index():
            return self._scan_open_ticket_for_session(root)
        entries = self._index_entries(root)
        stale = 0
        for entry in entries:
            record = self.resolve(entry["clarify_token"])
            if (
                record is None
                or record.get("state") != CLARIFY_TICKET_OPEN
                or str(record.get("chat_session_id") or "") != root
            ):
                stale += 1
                continue
            # Stop at the first live ticket: the entries are newest-first, so
            # this IS the answer, and reading past it would trade the O(1) the
            # index exists for. Anything stale BEHIND it is dropped when it in
            # turn becomes the head, or by the sweep.
            if stale:
                self._write_index(root, entries[stale:])
            return record
        if stale:
            self._write_index(root, [])
        return None

    def _scan_open_ticket_for_session(self, root: str) -> dict[str, Any] | None:
        """Index-free lookup: the pre-index behavior, kept as the fallback.

        Reached only when the index could not be written (read-only or full
        store root). Correctness must never depend on the index existing."""

        newest: dict[str, Any] | None = None
        for record in self._iter_records():
            if record.get("state") != CLARIFY_TICKET_OPEN:
                continue
            if str(record.get("chat_session_id") or "") != root:
                continue
            if newest is None or float(record.get("created_at") or 0.0) > float(
                newest.get("created_at") or 0.0
            ):
                newest = record
        return newest

    def scan_tickets(self) -> tuple[list[dict[str, Any]], int]:
        """:meth:`list_tickets`, plus how many ticket files would not decode.

        The count travels because this readout's whole point is a RATIO. The
        adoption metric is computed over the entire store — the site that builds
        it says so, and says why: "an adoption ratio that moved because the
        operator asked to see fewer rows would be a lying metric". A file that
        silently drops out of the scan moves the denominator in exactly the way
        that comment forbids, and does it without the operator having asked for
        anything. So the readout states it instead of absorbing it.
        """

        records, unreadable = self.scan_records()
        records.sort(key=lambda record: float(record.get("created_at") or 0.0), reverse=True)
        return records, unreadable

    def sweep(self, *, ttl_seconds: float = CLARIFY_TICKET_TTL_SECONDS) -> int:
        """Prune ticket files older than *ttl_seconds*. Returns the count.

        TTL governs GC only — nothing consults it when deciding whether a token
        binds, so a ticket that survives past its TTL keeps working right up
        until a sweep removes the file. One rule, no cliff."""

        cutoff = time.time() - max(float(ttl_seconds), 0.0)
        pruned = 0
        for record in self._iter_records():
            if not self._expired(float(record.get("created_at") or 0.0), cutoff):
                continue
            try:
                self._path(str(record.get("clarify_token"))).unlink(missing_ok=True)
            except OSError:
                continue
            pruned += 1
        self._sweep_index(cutoff)
        return pruned

    @staticmethod
    def _expired(created_at: float, cutoff: float) -> bool:
        """THE expiry predicate, and there is deliberately only one.

        The ticket loop and the index sweep must agree EXACTLY about what
        "past its TTL" means, because the index sweep's entire safety argument
        is "every ticket this file names was already unlinked by the ticket
        loop". Two spellings of the comparison — or a second cutoff constant —
        make that argument true only by coincidence, and the day it stops being
        true the index file naming a LIVE ticket is the one deleted."""

        return created_at <= cutoff

    def _sweep_index(self, cutoff: float) -> None:
        """Drop index files that cannot name a live ticket any more.

        BY THE FILE'S OWN RECORD of the newest ticket it names, against the
        SAME cutoff the ticket loop above just used. That is the whole safety
        argument: if every token in the file is expired, the loop already
        unlinked all of them, so the file can only name the dead.

        It used to be by MTIME, which is the same argument resting on an
        INFERRED invariant — "the filesystem's clock tracks this file's
        content" — that nothing in the code enforces and that any backup,
        restore, sync, or copy tool breaks silently. Both directions of that
        break are real, and one of them is not survivable: a restored file
        carrying a fresh mtime over stale content merely leaks (the file is
        inert; entries verify away on the read path), but a copy that lands
        stale mtimes on LIVE content gets the file deleted while the marker
        goes on swearing the index is complete — the silent permanent miss
        this store already fixed once, arriving through the back door.
        ``created_at`` is a property of the DATA and travels with it.

        Deliberately NOT a read-modify-write per pruned ticket: that would race
        a concurrent mint on the same session and could clobber a live pointer.
        Entries for individually-pruned tickets self-heal on the read path
        instead."""

        try:
            entries = list(self._index_dir().glob("*.json"))
        except OSError:
            return
        state_path = self._index_state_path()
        for entry in entries:
            if entry == state_path:
                continue
            newest = self._index_newest_created_at(entry)
            # UNREADABLE IS NOT DEAD. A file we could not read has not been
            # proven to name only expired tickets, and the sweep only ever
            # deletes on proof: keeping an inert file costs one small file,
            # deleting a live one costs a ticket nothing will look for again.
            if newest is None or not self._expired(newest, cutoff):
                continue
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                continue

    def scan_records(self) -> tuple[list[dict[str, Any]], int]:
        """Every ticket record on disk, beside how many files did not decode.

        THE chokepoint. :meth:`_iter_records` is the thin view over it, so the
        callers that only want records keep their shape while the two that must
        not describe a short answer as complete can ask the fuller question:
        :meth:`_rebuild_index`, which mints a COMPLETENESS CLAIM from this scan,
        and the operator readout, whose adoption ratio is computed over the whole
        store.

        This is the same law :meth:`_read_index` already states for the index
        file — UNREADABLE IS NOT EMPTY — applied to the ticket files themselves.
        The failure it counts is usually the ordinary Windows one that method
        documents: an AV or indexer holding a file for the instant we touch it,
        which is transient, so the file it hid is very likely readable a second
        later. That is precisely why it must not be silently absorbed into a
        permanent claim.
        """

        root = self._root_dir()
        try:
            entries = sorted(root.glob("*.json"))
        except OSError:
            return [], 0
        records: list[dict[str, Any]] = []
        unreadable = 0
        for entry in entries:
            try:
                record = json.loads(entry.read_text(encoding="utf-8"))
            except Exception:
                unreadable += 1
                continue
            if isinstance(record, dict) and record.get("clarify_token"):
                records.append(record)
        return records, unreadable

    def _iter_records(self) -> Iterator[dict[str, Any]]:
        yield from self.scan_records()[0]


#: Keys accepted inside ``_dispatched_from``. An allow-list rather than a
#: pass-through: session meta is read back by projections and shipped to the
#: launcher, so an unbounded caller-supplied dict is a payload-growth and
#: leak surface, not provenance.
_DISPATCH_LINEAGE_KEYS = ("predecessor_chat_session_id", "requested_by_session")


def _stored_dispatch_lineage(session_db: Any, root: str) -> dict[str, str]:
    """The ``_dispatched_from`` already recorded for *root*, if any."""

    try:
        row = session_db.get_session(root)
    except Exception:
        return {}
    raw = (row if isinstance(row, dict) else {}).get("model_config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except Exception:
            return {}
    stored = raw.get("_dispatched_from") if isinstance(raw, dict) else None
    if not isinstance(stored, dict):
        return {}
    return {
        str(key): text
        for key in _DISPATCH_LINEAGE_KEYS
        if (text := safe_assignment_text(stored.get(key), limit=240))
    }


def _dispatch_lineage_meta(
    dispatched_from: dict[str, Any] | None, *, root: str
) -> dict[str, str]:
    """Bounded, string-only projection of the dispatch lineage.

    FRESH MINTS ONLY — a replay carries the stored block forward instead of
    re-projecting caller arguments that have gone stale (see :meth:`mint`).
    *root* is the session this meta belongs to; a predecessor equal to it is
    dropped through the shared :func:`superseded_session_id` rule, which a
    fresh mint's brand-new id cannot trip but which keeps the one lineage
    projection honest for any caller that hands in the thread itself."""

    if not isinstance(dispatched_from, dict):
        return {}
    lineage: dict[str, str] = {}
    for key in _DISPATCH_LINEAGE_KEYS:
        value = safe_assignment_text(dispatched_from.get(key), limit=240)
        if key == "predecessor_chat_session_id":
            value = superseded_session_id(value, established=root) or ""
        if value:
            lineage[key] = value
    return lineage


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(str(tmp), str(path))


@dataclass
class ResidentPersonaChatRuntime:
    root_session_id: str
    active_session_id: str
    signature: str
    revision: str
    agent: Any
    last_used_at: float
    created_at: float
    last_resumed_at: str
    turn_count: int = 0


class PersonaChatRuntimeRegistry:
    """Bounded process-scoped one-resident-agent-per-root registry."""

    def __init__(self, *, max_entries: int = 8, ttl_seconds: float = 900.0):
        self.max_entries = max(1, int(max_entries))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._entries: OrderedDict[str, ResidentPersonaChatRuntime] = OrderedDict()
        self._transitions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def acquire(
        self,
        *,
        root_session_id: str,
        active_session_id: str,
        signature: str,
        revision: str,
        factory: Callable[[], Any],
    ) -> tuple[ResidentPersonaChatRuntime, bool, str | None]:
        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            entry = self._entries.pop(root_session_id, None)
            rebuild_reason = None
            if entry is not None and entry.signature != signature:
                rebuild_reason = "runtime_signature_changed"
                self._close_entry(entry)
                entry = None
            elif entry is not None and (entry.revision != revision or entry.active_session_id != active_session_id):
                rebuild_reason = "disk_revision_changed"
                self._close_entry(entry)
                entry = None
            reused = entry is not None
            if entry is None:
                entry = ResidentPersonaChatRuntime(
                    root_session_id=root_session_id,
                    active_session_id=active_session_id,
                    signature=signature,
                    revision=revision,
                    agent=factory(),
                    created_at=now,
                    last_used_at=now,
                    last_resumed_at=_utc_now_iso(),
                )
                self._record_transition(
                    root_session_id,
                    "cold",
                    "rebuilt" if rebuild_reason else "rehydrated",
                )
            entry.last_used_at = now
            self._entries[root_session_id] = entry
            while len(self._entries) > self.max_entries:
                evicted_root, evicted = self._entries.popitem(last=False)
                self._close_entry(evicted)
                self._record_transition(evicted_root, "cold", "evicted")
            return entry, reused, rebuild_reason

    def finish(self, root_session_id: str, *, active_session_id: str, revision: str) -> None:
        with self._lock:
            entry = self._entries.get(root_session_id)
            if entry is None:
                return
            tip_advanced = entry.active_session_id != active_session_id
            entry.active_session_id = active_session_id
            entry.revision = revision
            entry.last_used_at = time.monotonic()
            entry.turn_count += 1
            self._entries.move_to_end(root_session_id)
            self._record_transition(
                root_session_id,
                "hot",
                "tip_advanced" if tip_advanced else None,
            )

    def transition(self, root_session_id: str, state: str) -> None:
        """Record process-local lifecycle truth for an owning serve observer."""

        if state not in {"cold", "busy", "hot", "failed"}:
            raise ValueError(f"invalid persona chat runtime state: {state}")
        with self._lock:
            self._record_transition(
                root_session_id,
                state,
                "failed" if state == "failed" else None,
            )

    def evict(self, root_session_id: str) -> bool:
        with self._lock:
            entry = self._entries.pop(root_session_id, None)
            removed = entry is not None
            if entry is not None:
                self._close_entry(entry)
            self._record_transition(root_session_id, "cold", "evicted")
            return removed

    def observation(self, root_session_id: str, *, owning_process: bool) -> dict[str, Any]:
        if not owning_process:
            return {
                "runtime_state": "unknown",
                "runtime_observer_id": "external_cli",
                "runtime_observed_at": _utc_now_iso(),
            }
        with self._lock:
            entry = self._entries.get(root_session_id)
            transition = self._transitions.get(root_session_id) or {}
            state = transition.get("state") or ("hot" if entry else "cold")
            return {
                "runtime_state": state,
                "last_runtime_transition": transition.get("transition"),
                "runtime_observer_id": f"serve:{os.getpid()}",
                "runtime_observed_at": _utc_now_iso(),
                "active_session_id": entry.active_session_id if entry else None,
                "last_resumed_at": entry.last_resumed_at if entry else None,
            }

    def _record_transition(
        self, root_session_id: str, state: str, transition: str | None = None
    ) -> None:
        previous = self._transitions.get(root_session_id) or {}
        self._transitions[root_session_id] = {
            "state": state,
            "transition": transition or previous.get("transition"),
        }

    @staticmethod
    def _close_entry(entry: ResidentPersonaChatRuntime) -> None:
        close = getattr(entry.agent, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _evict_expired(self, now: float) -> None:
        expired = [key for key, value in self._entries.items() if now - value.last_used_at > self.ttl_seconds]
        for key in expired:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._close_entry(entry)
            self._record_transition(key, "cold", "evicted")


_REGISTRY: PersonaChatRuntimeRegistry | None = None


def initialize_persona_chat_runtime_registry(
    *, enabled: bool = True, max_entries: int = 8, ttl_seconds: float = 1800.0
) -> PersonaChatRuntimeRegistry | None:
    global _REGISTRY
    _REGISTRY = (
        PersonaChatRuntimeRegistry(max_entries=max_entries, ttl_seconds=ttl_seconds)
        if enabled
        else None
    )
    return _REGISTRY


def persona_chat_runtime_registry() -> PersonaChatRuntimeRegistry | None:
    return _REGISTRY
