"""Did this turn produce anything a human can SEE? — one typed answer.

A turn that ran successfully and a turn whose answer reached somebody are not
the same fact, and the runtime has never had a name for the difference. Every
lane that needed it re-derived it, from whatever evidence was in reach:

* the delivery drain compared ``reply`` against the empty string;
* ``cron/scheduler`` treats an empty ``final_response`` as a soft FAILURE and
  writes an English sentence into ``error``;
* ``agent/monitoring/cron_health`` then reads that sentence back with
  ``if "empty response" in text`` to recover the classification cron had and
  discarded;
* ``agent/error_classifier`` keeps a list of five English phrasings for the
  same event;
* ``gateway`` has both a "did it produce nothing" predicate and a
  silence-narration text filter, and treats the answer as normal traffic.

Three of those are string-matching on prose. They disagree about whether an
empty turn is an error, a normal outcome, or something to filter — and none of
them can be relied on by the others, so the next lane that needs the answer
writes a sixth derivation. That is the weakness this module retires: the fact is
computed ONCE, from the strongest evidence available, and travels as a typed
block. (2026-08-11: a background-completion delivery turn returned no content
three retries deep; the completion notice landed in the operator's thread
answered by nothing, while every surface watching that lane reported a clean
delivery.)

**Evidence, in the order it is trusted.** All of it is READ from what the
runtime already produces — this module adds no new plumbing to the agent loop
and edits nothing upstream:

1. the reply text itself, which is what a human would actually see;
2. the final assistant message's ``finish_reason`` — the provider-derived,
   already-persisted typed value (``incomplete``/``length`` when the model was
   cut off, ``content_filter`` when it was blocked). It is what explains an
   empty reply, never what overrules a non-empty one;
3. the run result's ``failed``/``error`` keys, for the turn that ended in an
   error rather than in silence.

**Unknown is a first-class answer.** A consumer holding a payload that predates
this block, or a stubbed turn that reports no reply at all, has NO evidence —
and "no evidence" must not collapse into either "visible" or "silent". A bare
bool would force exactly that collapse, which is why the state is a three-value
enum and every consumer is expected to handle the third one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

#: The wire key this block travels under, on the mission-chat payload and
#: anywhere else that adopts it. One spelling, imported — never a literal at a
#: call site, which is how the two halves of a seam drift apart.
TURN_VISIBILITY_KEY = "turn_visibility"


class VisibilityState(str, Enum):
    """Whether a human would see anything from this turn.

    ``str`` mixin so the value serialises as itself and a consumer comparing
    against the plain string still works — the launcher, the drain mirror and
    ``harness status --json`` all round-trip through JSON.
    """

    VISIBLE = "visible"
    SILENT = "silent"
    #: No evidence either way. NOT a synonym for either of the above: a payload
    #: written before this block existed is unknown, and reporting it as
    #: ``silent`` would manufacture an incident out of missing data.
    UNKNOWN = "unknown"


class VisibilityReason(str, Enum):
    """WHY the state is what it is — the sub-cause, never the decision."""

    #: Visible: the turn produced printable text.
    CONTENT = "content"
    #: Silent: no printable text, and nothing explains why.
    EMPTY = "empty"
    #: Silent: the model was cut off before it produced any content
    #: (``finish_reason`` ``incomplete``/``length``).
    TRUNCATED = "truncated"
    #: Silent: the provider blocked the content (``content_filter``).
    FILTERED = "filtered"
    #: Silent: the turn ended in an error and left no reply behind.
    FAILED = "failed"
    #: Unknown: nothing to classify from.
    NO_EVIDENCE = "no_evidence"


#: ``finish_reason`` values that EXPLAIN an empty reply. Deliberately narrow:
#: these are the provider/runtime spellings the codex + chat-completions
#: adapters emit (``agent/codex_responses_adapter``, ``agent/conversation_loop``),
#: and a value not listed here leaves an empty reply classified as ``empty``
#: rather than being silently upgraded to a cause nobody proved.
TRUNCATION_FINISH_REASONS = frozenset({"incomplete", "length", "max_output_tokens"})
FILTER_FINISH_REASONS = frozenset({"content_filter", "content_policy_blocked"})

#: ``finish_reason`` written on the assistant row of an INTERMEDIATE tool-call
#: round. Such a row is empty because the model called a tool instead of
#: speaking — it is not the end of a turn, and it has no visibility verdict at
#: all. Across the operator's live profiles these outnumber genuinely silent
#: rows 22,179 to 5, which is why the transcript read path has always dropped
#: EVERY empty assistant row: the scaffolding is the overwhelming majority, and
#: until this module existed there was nothing typed on the row to separate the
#: two. Distinguishing them is the whole job of
#: :func:`classify_persisted_turn_row`.
TOOL_CALL_FINISH_REASONS = frozenset({"tool_calls"})

#: The reasons that can accompany :attr:`VisibilityState.SILENT`. Exported so a
#: presentation layer can PROVE its own text table is total over them, instead
#: of re-listing the vocabulary in a second place and drifting from it.
SILENT_REASONS = frozenset(
    {
        VisibilityReason.EMPTY,
        VisibilityReason.TRUNCATED,
        VisibilityReason.FILTERED,
        VisibilityReason.FAILED,
    }
)


@dataclass(frozen=True)
class TurnVisibility:
    """One turn's visibility, as a value object.

    Frozen: this is a FACT about a turn that already happened, so nothing
    downstream may adjust it on the way past — the single hardest failure to
    debug on this lane was a report that had been "corrected" by a consumer.
    """

    state: VisibilityState
    reason: VisibilityReason
    #: The final assistant message's finish reason, verbatim and unclassified.
    #: Carried even when it did not drive the outcome, because "the reply was
    #: empty and the finish reason was `stop`" is a materially different
    #: incident from "empty, and the model was cut off".
    finish_reason: str = ""
    #: Length of the printable reply. 0 and ``UNKNOWN`` are distinguishable:
    #: this is 0 for a measured-empty reply and 0 for no evidence, which is
    #: exactly why the state — not this number — is the answer.
    reply_chars: int = 0

    @property
    def is_silent(self) -> bool:
        """PROVEN silent. Unknown is not silent — that is the whole point."""

        return self.state is VisibilityState.SILENT

    @property
    def is_visible(self) -> bool:
        """PROVEN visible. Unknown is not visible either."""

        return self.state is VisibilityState.VISIBLE

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. Plain JSON values only."""

        return {
            "state": self.state.value,
            "reason": self.reason.value,
            "finish_reason": self.finish_reason,
            "reply_chars": self.reply_chars,
        }

    def describe(self) -> str:
        """A short human line for logs and telemetry details.

        One phrasing, so the outcome rows a person greps and the log lines they
        grep next cannot disagree about wording — which is precisely how
        ``if "empty response" in text`` came to exist elsewhere.
        """

        if self.state is VisibilityState.UNKNOWN:
            return "turn visibility unknown (no reply evidence in the payload)"
        if self.state is VisibilityState.VISIBLE:
            return f"reply of {self.reply_chars} chars"
        detail = {
            VisibilityReason.TRUNCATED: "the model was cut off before producing content",
            VisibilityReason.FILTERED: "the provider blocked the content",
            VisibilityReason.FAILED: "the turn ended in an error",
        }.get(self.reason, "the model produced no content")
        suffix = f", finish_reason={self.finish_reason}" if self.finish_reason else ""
        return f"empty reply — {detail}{suffix}"

    # -- constructors ------------------------------------------------------

    @staticmethod
    def unknown() -> "TurnVisibility":
        return TurnVisibility(
            state=VisibilityState.UNKNOWN, reason=VisibilityReason.NO_EVIDENCE
        )

    @classmethod
    def from_dict(cls, data: Any) -> "TurnVisibility":
        """Rehydrate a wire block. TOTAL: anything unrecognised is unknown.

        A consumer reads this off a payload it did not build — an older serve,
        a hand-rolled test double, a future field it does not know. None of
        those may raise, and none of them may be guessed at.
        """

        if not isinstance(data, dict):
            return cls.unknown()
        try:
            state = VisibilityState(str(data.get("state") or ""))
            reason = VisibilityReason(str(data.get("reason") or ""))
        except ValueError:
            return cls.unknown()
        try:
            chars = int(data.get("reply_chars") or 0)
        except (TypeError, ValueError):
            chars = 0
        return cls(
            state=state,
            reason=reason,
            finish_reason=str(data.get("finish_reason") or ""),
            reply_chars=max(0, chars),
        )

    @classmethod
    def from_payload(cls, payload: Any) -> "TurnVisibility":
        """Read a mission-chat payload: typed block first, reply text second.

        The fallback is not redundancy for its own sake — it is what lets a
        consumer keep working against a serve process that predates the block
        (the runtime is long-lived; a landed fix is not a running one until it
        restarts). A payload carrying neither is honestly unknown.
        """

        if not isinstance(payload, dict):
            return cls.unknown()
        block = payload.get(TURN_VISIBILITY_KEY)
        if block is not None:
            return cls.from_dict(block)
        if "reply" not in payload:
            return cls.unknown()
        return classify_turn_visibility(reply_text=payload.get("reply"))


def _final_assistant_finish_reason(messages: Any) -> str:
    """The last assistant message's finish reason, or ``""``.

    Scanned from the END: the list is the turn's message history, and only its
    final assistant entry describes how the turn actually stopped. Tool-call
    rounds in the middle carry their own reasons and would mis-explain it.
    """

    if not isinstance(messages, (list, tuple)):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "assistant":
            continue
        return str(message.get("finish_reason") or "")
    return ""


def _reason_for_empty(finish_reason: str, *, raw: Any = None) -> VisibilityReason:
    """WHY an empty reply is empty — the one ladder, shared by every entry point.

    Extracted so the live classifier and the persisted-row classifier cannot
    answer the same question differently. A second copy of this ladder would be
    the exact defect this module was written to retire, one level down.
    """

    if finish_reason in FILTER_FINISH_REASONS:
        return VisibilityReason.FILTERED
    if finish_reason in TRUNCATION_FINISH_REASONS:
        return VisibilityReason.TRUNCATED
    if isinstance(raw, dict) and (raw.get("failed") or raw.get("error")):
        # Last, and only for an otherwise unexplained silence: `error` is set on
        # soft/partial outcomes too, so letting it outrank a real finish reason
        # would relabel every truncation as a failure.
        return VisibilityReason.FAILED
    return VisibilityReason.EMPTY


def classify_persisted_turn_row(
    *,
    content: Any,
    finish_reason: Any = None,
    tool_calls: Any = None,
) -> TurnVisibility | None:
    """Classify one PERSISTED assistant row. TOTAL — never raises.

    The live classifier above reads a completed run's reply; this one reads a
    row that was written to SessionDB minutes or weeks ago, where the only
    evidence left is the three columns the flush persisted. Both share the
    reason ladder, so a turn cannot be described one way live and another way
    on reload.

    ``None`` is returned for a row that is NOT a turn boundary — an
    intermediate tool-call round, whose emptiness means "the model called a
    tool" and carries no verdict about whether the operator saw anything. That
    is a third answer, distinct from ``SILENT`` and from ``UNKNOWN``: the
    question does not apply to this row. It is deliberately not a
    :class:`VisibilityState` member, because such a row has no visibility, and
    inventing a state for it would put a non-answer into a vocabulary every
    consumer switches on exhaustively.

    An unexpected input yields ``None`` rather than ``unknown()``: ``None`` is
    "drop this row", which is precisely what the read path did for every empty
    assistant row before this function existed. The failure mode of a bug here
    is therefore today's behaviour, never a marker manufactured over a turn
    nobody proved was silent.
    """

    try:
        text = "" if content is None else str(content).strip()
        reason_token = str(finish_reason or "").strip()
        if text:
            # Content wins over every finish reason, including ``tool_calls``:
            # a model that spoke AND called a tool was seen. Same precedence as
            # the live classifier, for the same reason.
            return TurnVisibility(
                state=VisibilityState.VISIBLE,
                reason=VisibilityReason.CONTENT,
                finish_reason=reason_token,
                reply_chars=len(text),
            )
        if _has_tool_calls(tool_calls) or reason_token in TOOL_CALL_FINISH_REASONS:
            return None
        return TurnVisibility(
            state=VisibilityState.SILENT,
            reason=_reason_for_empty(reason_token),
            finish_reason=reason_token,
            reply_chars=0,
        )
    except Exception:  # pragma: no cover — the totality guarantee itself
        return None


def _has_tool_calls(tool_calls: Any) -> bool:
    """Does this row carry tool calls? Tolerant of both persisted shapes.

    ``SessionDB.get_messages`` deserialises the column into a list, but the
    lineage loaders and older rows can still hand back the raw JSON string, and
    a row that reaches here as ``"[]"`` has no tool calls despite being truthy
    as a string.
    """

    if tool_calls is None:
        return False
    if isinstance(tool_calls, str):
        stripped = tool_calls.strip()
        return bool(stripped) and stripped not in {"[]", "{}", "null"}
    return bool(tool_calls)


def classify_turn_visibility(
    *,
    reply_text: Any,
    messages: Any = None,
    raw: Any = None,
) -> TurnVisibility:
    """Classify one completed turn. TOTAL — never raises, for any input.

    Called from a payload builder and from an exception handler, so a raise
    here would either corrupt the one-JSON-object stdout contract or replace a
    real failure with this function's own. Every branch below is written to
    that standard.

    ``reply_text`` of ``None`` means "not reported" and yields UNKNOWN; an
    empty or whitespace-only string means "reported, and it was empty", which
    is a measured silence. Those two must not collapse.
    """

    try:
        if reply_text is None:
            return TurnVisibility.unknown()
        text = str(reply_text).strip()
        finish_reason = _final_assistant_finish_reason(messages)
        if text:
            # A non-empty reply is VISIBLE regardless of finish reason: a
            # truncated turn that still produced words is a short answer the
            # operator can read, not a silence. Truncation is a modifier on
            # emptiness here, never an override of content.
            return TurnVisibility(
                state=VisibilityState.VISIBLE,
                reason=VisibilityReason.CONTENT,
                finish_reason=finish_reason,
                reply_chars=len(text),
            )
        return TurnVisibility(
            state=VisibilityState.SILENT,
            reason=_reason_for_empty(finish_reason, raw=raw),
            finish_reason=finish_reason,
            reply_chars=0,
        )
    except Exception:  # pragma: no cover — the totality guarantee itself
        return TurnVisibility.unknown()


__all__ = [
    "FILTER_FINISH_REASONS",
    "SILENT_REASONS",
    "TOOL_CALL_FINISH_REASONS",
    "TRUNCATION_FINISH_REASONS",
    "TURN_VISIBILITY_KEY",
    "TurnVisibility",
    "VisibilityReason",
    "VisibilityState",
    "classify_persisted_turn_row",
    "classify_turn_visibility",
]
