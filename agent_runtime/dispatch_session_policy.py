#!/usr/bin/env python3
"""Which chat session a dispatched agent→agent task lands in — ONE authority.

Before this module, an ``agent_chat_send`` that omitted ``session_id`` always
continued the target's durable default thread. That is right for a
*conversation* and wrong for a *dispatch*: a mission lead briefing the same
teammate on ten unrelated tasks accumulated all ten in one mega-thread, and
every turn re-fed the whole transcript to the provider (observed live: 293K
input tokens to produce a 1.7K-output answer). Threads that never end also make
recall worse, not better — "what did we decide about X" is buried in a wall of
unrelated task chatter.

So a dispatch is now **task-scoped by default**: a fresh chat session per
dispatched task, with the predecessor recorded as typed lineage
(``_dispatched_from`` in the session meta) and the established session reported
back in the turn envelope (``session_established``). Continuation stays
possible and becomes *explicit* — the caller passes the ``session_id`` the
dispatch returned (in-task follow-ups, clarify round-trips), or
``new_session: false`` to deliberately continue the durable pair thread. Recall
across retired threads rides ``session_search`` plus the lineage chain.

The decision lives here rather than in the handler because three callers must
agree on it (the ``agent_chat_send`` tool, the ``hermes harness mission-chat
message`` CLI/serve lane, and the tests that pin the contract), and because a
policy expressed as scattered ``if not args.new_session`` booleans is exactly
the fragile-boolean shape this codebase keeps having to retire. Everything here
is pure: no config read at import, no store access, no I/O.

Precedence (highest first) — mirrors ``resolve_mission_chat_max_seconds``:
explicit caller intent always beats the configured default.

1. ``session_id``            → continue THAT thread (``explicit_session_id``)
2. ``new_session=True``      → fresh thread (``explicit_new_session``)
3. ``new_session=False``     → durable pair thread (``sticky_default``)
4. unset (``None``)          → the configured policy decides
   (``policy_new_per_dispatch`` / ``policy_sticky``)

Note the tri-state: ``False`` and "unset" are DIFFERENT answers now, so the
CLI's ``--new-session`` (argparse ``store_true`` → ``False`` when absent) keeps
the operator console on its durable thread byte-for-byte, while a tool caller
that says nothing gets the dispatch default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: ``agent_runtime.mission_chat.dispatch_session_policy`` vocabulary.
NEW_PER_DISPATCH = "new_per_dispatch"
STICKY = "sticky"
DISPATCH_SESSION_POLICIES = (NEW_PER_DISPATCH, STICKY)
DEFAULT_DISPATCH_SESSION_POLICY = NEW_PER_DISPATCH

#: Typed reasons carried in the ``session_established`` envelope block. A caller
#: (human reading Mission Control, or an agent reading its own tool result) can
#: always tell WHY it landed in the session it landed in.
REASON_EXPLICIT_SESSION_ID = "explicit_session_id"
REASON_EXPLICIT_NEW_SESSION = "explicit_new_session"
REASON_STICKY_DEFAULT = "sticky_default"
REASON_POLICY_NEW_PER_DISPATCH = "policy_new_per_dispatch"
REASON_POLICY_STICKY = "policy_sticky"

DISPATCH_SESSION_REASONS = (
    REASON_EXPLICIT_SESSION_ID,
    REASON_EXPLICIT_NEW_SESSION,
    REASON_STICKY_DEFAULT,
    REASON_POLICY_NEW_PER_DISPATCH,
    REASON_POLICY_STICKY,
)

#: How much of the dispatch message becomes the fresh thread's title.
DISPATCH_TITLE_MAX_CHARS = 48

_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "off"})
_WHITESPACE = re.compile(r"\s+")


def coerce_optional_flag(value) -> bool | None:
    """Tri-state coercion for ``new_session``: ``True`` / ``False`` / unset.

    ``bool(value)`` is wrong here in a way that only bites now that "unset" is
    a distinct answer: it folds ``None`` into ``False`` (silently pinning every
    tool caller to the sticky lane) and folds the string ``"false"`` into
    ``True`` (a provider that serializes booleans as text would invert the
    caller's intent). Unrecognized junk degrades to "unset" — the caller stated
    no usable opinion, so the policy decides."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    token = str(value).strip().lower()
    if not token:
        return None
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def normalize_dispatch_session_policy(
    value, default: str = DEFAULT_DISPATCH_SESSION_POLICY
) -> str:
    """Coerce a configured policy token, degrading to *default* on junk.

    Degrading (rather than raising) matches ``_mission_chat_config``'s clamp
    stance: a fat-fingered stanza must not fail every dispatch on the lane."""

    token = str(value or "").strip().lower().replace("-", "_")
    if token in DISPATCH_SESSION_POLICIES:
        return token
    return default if default in DISPATCH_SESSION_POLICIES else DEFAULT_DISPATCH_SESSION_POLICY


@dataclass(frozen=True, slots=True)
class DispatchSessionDecision:
    """Whether this send starts a fresh thread, and the typed reason why.

    ``mint`` is the *intent* (start a fresh thread). Whether a session was
    actually minted is a separate fact — a sticky send against a target that
    has never chatted also mints one — so the envelope reports ``fresh``
    separately from ``reason`` (see :func:`session_established_payload`)."""

    mint: bool
    reason: str
    policy: str

    @property
    def explicit(self) -> bool:
        """True when the caller stated the thread target rather than inheriting it."""

        return self.reason in (
            REASON_EXPLICIT_SESSION_ID,
            REASON_EXPLICIT_NEW_SESSION,
            REASON_STICKY_DEFAULT,
        )


def resolve_dispatch_session_decision(
    *,
    session_id: str | None = None,
    new_session=None,
    policy: str | None = None,
) -> DispatchSessionDecision:
    """The one place the fresh-vs-continue question is answered.

    *policy* defaults to the configured
    ``agent_runtime.mission_chat.dispatch_session_policy`` (loaded lazily so
    this module stays importable without touching config, and so a config fault
    degrades to the built-in default instead of failing a turn)."""

    if policy is None:
        try:  # lazy: keeps this module pure/importable and config faults non-fatal
            from agent_runtime.config import mission_chat_dispatch_session_policy

            resolved_policy = mission_chat_dispatch_session_policy()
        except Exception:  # pragma: no cover - defensive; a config fault must not kill a turn
            resolved_policy = DEFAULT_DISPATCH_SESSION_POLICY
    else:
        resolved_policy = normalize_dispatch_session_policy(policy)

    if str(session_id or "").strip():
        return DispatchSessionDecision(
            mint=False, reason=REASON_EXPLICIT_SESSION_ID, policy=resolved_policy
        )
    flag = coerce_optional_flag(new_session)
    if flag is True:
        return DispatchSessionDecision(
            mint=True, reason=REASON_EXPLICIT_NEW_SESSION, policy=resolved_policy
        )
    if flag is False:
        return DispatchSessionDecision(
            mint=False, reason=REASON_STICKY_DEFAULT, policy=resolved_policy
        )
    if resolved_policy == STICKY:
        return DispatchSessionDecision(
            mint=False, reason=REASON_POLICY_STICKY, policy=resolved_policy
        )
    return DispatchSessionDecision(
        mint=True, reason=REASON_POLICY_NEW_PER_DISPATCH, policy=resolved_policy
    )


def session_established_payload(
    decision: DispatchSessionDecision,
    *,
    fresh: bool,
    predecessor_session_id: str | None = None,
) -> dict:
    """The typed lineage block both envelopes carry.

    ``fresh`` is the OUTCOME (a session was minted this turn), ``reason`` is the
    DECISION — they can disagree honestly: a sticky send to a teammate who has
    never chatted mints a session with reason ``sticky_default``.
    ``predecessor_session_id`` is only meaningful for a fresh mint (the thread
    this dispatch superseded); a continuation has no predecessor because it IS
    the predecessor."""

    return {
        "fresh": bool(fresh),
        "reason": decision.reason,
        "predecessor_session_id": (
            str(predecessor_session_id) if (fresh and predecessor_session_id) else None
        ),
    }


def derive_dispatch_title(message, *, limit: int = DISPATCH_TITLE_MAX_CHARS) -> str | None:
    """Name a fresh dispatch thread after the task it carries.

    Task-scoped threads are only navigable if they are named — a Mission Control
    sidebar of nine identical "QA Agent chat" rows is worse than the mega-thread
    it replaced. Cut on a word boundary so the title reads as words, not a
    guillotined token; returns ``None`` for an empty message so the caller falls
    back to the durable per-persona title."""

    text = _WHITESPACE.sub(" ", str(message or "")).strip()
    if not text:
        return None
    bound = max(8, int(limit))
    if len(text) <= bound:
        return text
    head = text[:bound]
    cut = head.rfind(" ")
    if cut >= bound // 2:
        head = head[:cut]
    return head.rstrip(" ,;:.-–—") or text[:bound]
