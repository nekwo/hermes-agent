"""What a paired install may LOOK AT here: the roster, and one thread's tail.

S2b, ruling R-IP9 (*two read-only peer methods; nothing else joins the
allowlist*). Two projections and one HUD block, all read-only, all shaped by
THIS install's own rules so the calling install never has to guess at a scope
only this one can resolve.

The gap this closes
-------------------

Since Stage 7 an agent on install A has been able to send to ``@B/neko`` and
have the reply delivered back into its conversation. What it could not do was
anything a person does before and after sending: see who is on B, or read the
thread it was just handed. The dispatch delivery has been printing *"Their
thread: persona_chat_… (agent_chat_open with this session_id to read the whole
exchange)"* — a pointer that resolved to nothing on the machine that received
it, because the transcript is on B's disk. These two doors make that sentence
true.

Why B projects, rather than A filtering
----------------------------------------

A roster is not a list of rows; it is the answer to *who is addressable from
this scope*, and the scope rules — workspace pointers, canonical-row shadowing,
placement-beats-plumbing — are B's. Handing A the raw instance list and letting
it apply those rules would be a second implementation of
``workspace_scope.addressable_roster`` that drifts the first time either side is
edited, and the drift would show up as an agent addressing a teammate B does not
consider addressable. So B runs its own composition and hands back rows that are
already sendable, exactly as ``agent_chat_threads`` does locally.

Why every row is REACHABLE and not merely present
--------------------------------------------------

Both projections filter through ``_resolve_mission_chat_persona_id`` for the
reason ``agent_chat_threads`` does: a row that cannot be sent to is worse than a
missing row, because an agent will address it and lose a turn finding out. A
roster is an offer, and an offer has to be honourable.

What is NOT here
----------------

No enumeration beyond one workspace, no path, no transcript search, no write.
``peer.thread.read`` requires the ``target`` as well as the ``session_id`` and
applies the same lane guard the local ``agent_chat_open`` applies, so a session
id that is not part of that teammate's chat lane is ``foreign_session`` here
exactly as it is there — a paired install can spend a pointer it was given and
can discover nothing else. That is ``peer.media.get``'s asymmetry (the reference
travels out, never in) applied to conversations.

The one behaviour with no precedent
------------------------------------

**A transcript read inside the serve process is new.** Every transcript read
before S2b ran in a CLI or child process. ``persona_chat_session_messages``
resolves its store through ``resolve_chat_session_scope``, whose ambient rung is
refused unless ``HERMES_ALLOW_AMBIENT_CHAT_READS`` is set and whose head
mismatch answers ``chat_scope_mismatch``. This module surfaces both as its own
typed refusal (``thread_unreadable``, carrying the reader's own word in
``data``) and **never sets that env var**: a read that silently widened its own
scope would be a peer door quietly granted more than the local tool has. If the
serve's head resolution turns out not to answer for persona-chat sessions, the
fallback is spelled in the plan's §5 — run the read through the worker lane as
the argv ``harness persona chat history`` — and is not invented here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "HUD_INSTALL_CAP",
    "HUD_ROSTER_CAP",
    "PEER_ROSTER_CONTRACT",
    "PEER_THREAD_CONTRACT",
    "ROSTER_ROW_CAP",
    "installs_hud_block",
    "peer_roster_projection",
    "read_chat_lane_tail",
    "resolve_far_target_scope",
]

#: Each projection carries its own shape number, beside ``PEER_PING_CONTRACT``'s
#: and for its reason: they describe these two results and nothing else, so a
#: later widening of one does not tell every ``runtime.*`` client that something
#: moved.
PEER_ROSTER_CONTRACT = 1
PEER_THREAD_CONTRACT = 1

#: How many roster rows one answer carries. Bounded because the caller is on
#: another machine and the cost of a large answer is paid on a link this install
#: does not own; ``truncated`` says so rather than the list silently ending.
ROSTER_ROW_CAP = 64

#: The HUD's own two caps, smaller than the store's because a prompt block is
#: read by a model with a budget rather than by a client with a scrollbar.
HUD_INSTALL_CAP = 8
HUD_ROSTER_CAP = 8


# ── the roster ───────────────────────────────────────────────────────────────


def resolve_far_target_scope(target: Any = None) -> str | None:
    """The workspace a far ``target`` would resolve in, from THIS install's rules.

    A resident ``personainst_*`` handle answers with that instance's own
    workspace; anything else answers with the active one — which is exactly what
    a bare ``@B/dev`` turn already resolves in today
    (``sender_scope_workspace_id`` falls back to the active workspace when no
    owning instance is found, and a peer turn carries no ``--workspace-id``).

    Answering the SAME question the send path answers is the whole point. A
    roster scoped one way and a send resolved another would offer an agent a
    teammate the very next message could not reach.
    """

    from .persona_assignments import PersonaInstanceStore, safe_assignment_token
    from .store import WorkspaceStore
    from .workspace_scope import effective_workspace_id

    active = WorkspaceStore().active_id()
    token = safe_assignment_token(target) if target else ""
    if not token.startswith("personainst_"):
        return active
    for instance in PersonaInstanceStore().list_all():
        if getattr(instance, "id", None) == token:
            return effective_workspace_id(instance, active_workspace_id=active)
    return active


def peer_roster_projection(*, scope_workspace_id: str | None) -> dict[str, Any]:
    """Who is addressable in one workspace, as rows a far install can send to.

    The composition is ``agent_chat_threads``' own, minus the thread bodies:
    the persisted personas seed the instance store, ``addressable_roster``
    applies the scoping rules (workspace narrowing, canonical plumbing excluded,
    a persona's canonical row shadowed behind an in-scope placement), and every
    surviving row is proven reachable before it is offered.

    ``last_turn_at`` comes from the same ``persona_chat_history_summary``
    projection the local threads list reads, keyed by each instance's default
    session. It is the one field that makes a roster useful rather than merely
    true: an operator scanning two machines wants to know who has been working,
    and "there are six agents over there" does not say it.
    """

    from .config import ensure_persisted_personas, load_agent_runtime_config
    from .persona_assignments import (
        PersonaInstanceStore,
        is_canonical_persona_channel,
        resolve_default_chat_session_id_for_instance,
        safe_assignment_text,
    )
    from .persona_chat_history import persona_chat_history_summary
    from .workspace_scope import addressable_roster
    from hermes_cli import harness as _harness

    store = PersonaInstanceStore()
    store.ensure_for_personas(list(ensure_persisted_personas(load_agent_runtime_config())))
    instances = addressable_roster(
        store.list_all(),
        scope_workspace_id=scope_workspace_id,
        is_canonical=is_canonical_persona_channel,
    )

    history_by_session: dict[str, dict] = {}
    try:
        for row in persona_chat_history_summary(persona_instances=instances):
            session_id = row.get("session_id")
            if session_id:
                history_by_session[session_id] = row
    except Exception:  # pragma: no cover - a projection glitch must not blank it
        history_by_session = {}

    rows: list[dict[str, Any]] = []
    truncated = False
    for instance in instances:
        persona_id = safe_assignment_text(getattr(instance, "persona_id", None), limit=160)
        handle = safe_assignment_text(getattr(instance, "id", None), limit=160)
        if not persona_id or not handle:
            continue
        # Only rows a send would actually reach — ``agent_chat_threads``' rule,
        # and it matters more across a machine boundary: an agent that addresses
        # an unreachable row loses a turn AND a network round trip finding out.
        try:
            reachable = _harness._resolve_mission_chat_persona_id(persona_id, persona_id)
        except ValueError:
            continue
        if len(rows) >= ROSTER_ROW_CAP:
            truncated = True
            break
        session_id = resolve_default_chat_session_id_for_instance(
            store, persona_id=reachable, persona_instance_id=handle
        )
        summary = history_by_session.get(session_id) if session_id else None
        rows.append(
            {
                "handle": handle,
                "persona_id": reachable,
                "label": safe_assignment_text(
                    getattr(instance, "display_name", None), limit=120
                )
                or handle,
                "is_canonical_primary": bool(is_canonical_persona_channel(instance)),
                "last_turn_at": (summary or {}).get("updated_at"),
                "workspace_id": getattr(instance, "workspace_id", None),
            }
        )

    return {
        "contract": PEER_ROSTER_CONTRACT,
        "workspace_id": scope_workspace_id,
        "count": len(rows),
        "truncated": truncated,
        "rows": rows,
        "at": _now_iso(),
    }


# ── the thread ───────────────────────────────────────────────────────────────


def read_chat_lane_tail(
    persona_id: Any,
    *,
    session_id: Any = None,
    limit: Any = 20,
    requested_by_session: Any = None,
) -> dict[str, Any]:
    """The bounded tail of ONE chat lane, as a dict. ONE implementation, two doors.

    Lifted out of ``agent_chat_open`` rather than reimplemented beside it, which
    is the whole reason it is a function: the lane guard
    (``_session_belongs_to_chat_lane``) is the thing standing between "review
    our thread" and "read any transcript on this machine", and a second copy of
    it for the peer door is a second place for that guard to be widened by
    accident. The local tool calls this; so does ``peer.thread.read``.

    Returns the success dict, or ``{"ok": False, "error": …, "error_kind": …}``
    — never raises past a typed refusal, because both callers hand their answer
    to a model or to a JSON-RPC error and neither can use a traceback.

    ``error_kind`` values, and they are the same on both doors:
    ``unsupported_persona`` (no such teammate here), ``foreign_session`` (the
    guard), and whatever the reader answered for a failed read
    (``thread_unreadable`` at the peer door, carrying the reader's own word).
    """

    from .persona_chat_history import (
        MAX_PERSONA_CHAT_MESSAGE_TAIL,
        persona_chat_session_messages,
    )
    from tools.agent_chat_tool import (
        _resolve_chat_lane_target,
        _session_belongs_to_chat_lane,
    )

    target, refusal = _resolve_chat_lane_target(
        persona_id, requested_by_session=requested_by_session, verb="agent_chat_open"
    )
    if refusal is not None:
        import json as _json

        # ``_resolve_chat_lane_target`` answers in the tool's own refusal
        # ENVELOPE (a JSON string) because it predates this function. Decoded
        # here rather than changed there, so the local tool's bytes are
        # unchanged and the peer door gets a dict.
        return _json.loads(refusal)

    resolved_persona = target.persona
    handle = target.handle
    default_session = target.default_session

    try:
        bounded = max(1, min(int(limit or 20), MAX_PERSONA_CHAT_MESSAGE_TAIL))
    except (TypeError, ValueError):
        bounded = 20

    requested_session = (str(session_id).strip() or None) if session_id else None
    if requested_session is not None:
        if not _session_belongs_to_chat_lane(
            requested_session, handle=handle, default_session=default_session
        ):
            return {
                "ok": False,
                "error": (
                    f"session {requested_session!r} is not part of "
                    f"{resolved_persona}'s chat lane; agent_chat_open only "
                    "reviews your shared thread with the target, not arbitrary "
                    "sessions."
                ),
                "error_kind": "foreign_session",
                "target_persona": resolved_persona,
            }
        target_session = requested_session
    else:
        target_session = default_session

    if not target_session:
        # Never chatted — an honest empty result, and no session minted.
        return {
            "ok": True,
            "target_persona": resolved_persona,
            "handle": handle,
            "session_id": None,
            "has_thread": False,
            "count": 0,
            "messages": [],
        }

    data = persona_chat_session_messages(session_id=target_session, limit=bounded)
    if data.get("ok") is False:
        # Propagated, never flattened into the success envelope: ``count: 0``
        # over an UNREAD transcript is the single most misleading answer
        # available here, because an agent checking whether a teammate replied
        # would conclude they did not and act on it.
        return {
            "ok": False,
            "error": (
                f"could not read {resolved_persona}'s thread {target_session}: "
                f"{data.get('error') or data.get('error_kind') or 'unknown error'}. "
                "This is NOT an empty thread — the transcript was not read."
            ),
            "error_kind": data.get("error_kind") or "session_db_unavailable",
            "target_persona": resolved_persona,
            "handle": handle,
            "session_id": target_session,
        }

    return {
        "ok": True,
        "target_persona": resolved_persona,
        "handle": handle,
        "session_id": target_session,
        "has_thread": True,
        "count": len(data.get("messages") or []),
        "redaction_status": data.get("redaction_status"),
        "messages": [
            {
                "role": message.get("role"),
                "text": message.get("text"),
                "timestamp": message.get("timestamp"),
            }
            for message in (data.get("messages") or [])
        ],
    }


# ── the HUD block ────────────────────────────────────────────────────────────


def installs_hud_block(store_root: Any) -> list[dict[str, Any]]:
    """The paired installs, as HUD rows. Reads two files and DIALS NOTHING.

    R-IP11's visible half: residency is something an agent can see rather than
    something it discovers by being wrong. Every fact here is already on disk —
    the trust row, the cache row, the roster somebody fetched earlier — so the
    block costs a prompt two file reads and never a network round trip. A HUD
    that dialled would make every turn's opening depend on a machine that might
    be asleep.

    ``ref`` is the spelling the grammar accepts (``usable_peers``' own), so a
    line an agent reads is always an address a send would resolve — never a
    display name the resolver would refuse as ambiguous.

    Best effort: an unreadable store answers ``[]``, which renders as no block
    at all, which is the same thing an unpaired install shows.
    """

    from .gateway_peers import REACHABILITY_UNREACHABLE, usable_peers

    try:
        peers = usable_peers(Path(store_root))
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    for peer in peers[:HUD_INSTALL_CAP]:
        cache = peer.cache
        row: dict[str, Any] = {
            "ref": peer.ref,
            "install_id": peer.record.peer_install_id,
            "display_name": (
                (cache.announced_display_name if cache is not None else None)
                or peer.record.display_name
            ),
            "reachability": (
                cache.reachability if cache is not None else "unknown"
            ),
        }
        if (
            cache is not None
            and cache.reachability == REACHABILITY_UNREACHABLE
            and cache.unreachable_since
        ):
            row["unreachable_since"] = cache.unreachable_since
        roster = cache.roster if cache is not None else None
        if isinstance(roster, dict):
            row["roster_fetched_at"] = roster.get("fetched_at")
            row["roster"] = [
                {"handle": entry.get("handle"), "persona_id": entry.get("persona_id")}
                for entry in (roster.get("rows") or [])[:HUD_ROSTER_CAP]
                if isinstance(entry, dict) and entry.get("handle")
            ]
        rows.append(row)
    return rows


def _now_iso() -> str:
    from hermes_time import now

    return now().isoformat()
