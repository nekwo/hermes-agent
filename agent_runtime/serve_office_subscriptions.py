"""Office push: a RE-ENVELOPE of an existing derivation, never a second one.

Operator ruling 2026-08-15, option (A). The office's delta lane already exists
and works: ``state_patches.emit_office_actor_patch`` writes an ``office_actor``
``upsert`` into the event log, and ``stream.patch_batch_frame`` assembles a
batch of them into ``{"type": "patch", base_offset, watermark, patches}``. The
only thing wrong with it is the ENVELOPE — a bare NDJSON frame with no
correlation rule, which is how a push could arrive and be dropped on the floor
with nothing to say so.

So this module adds no derivation. It registers with the SAME
:class:`~agent_runtime.serve_stream_hub.StreamHub` every stream client uses,
under a sink that re-wraps the frames it cares about as JSON-RPC
notifications. Three things follow from that choice, and each was a reason to
prefer it over tailing the event log a second time:

* one producer, so a patch cannot exist on one lane and not the other;
* an RPC subscriber COUNTS as a subscriber. This is load-bearing rather than
  incidental: the hub stops producing when its room empties, so once the
  launcher stops joining the legacy stream — the whole point of the ruling —
  a fan-out that merely OBSERVED the hub would observe a producer that had
  already exited, and the office would silently stop updating;
* backpressure, drop accounting and the bounded buffer are inherited whole.

What crosses, and what does not
-------------------------------
The sink is ADDRESSED where a frame is broadcast. A frame crosses when at least
one of its rows is scoped to this subscription's workspace by
:func:`~agent_runtime.state_patches.office_patch_scope` — the ONE authority for
that question, living beside the id builders it reads backwards, and called by
``_delta_touches_workspace`` here as well. A batch with nothing for this
workspace sends NOTHING, rather than a frame the client opens and discards.

That authority is single-homed because it was not, and the fork cost a silent
drop (task #57, RD-H1). The rule used to be restated privately here as "entity
is ``office_actor`` AND the id sits under ``"<workspace_id>/"``", which is what
``office_actor_patch_id`` was built for and was true of every row that could
promote — until WV-H3 (2026-08-16) let ``office_surface`` promote, whose id is
the BARE workspace id with no slash. A folder-only batch then failed both
conjuncts and was dropped with neither a patch nor a resync, against this
module's own rule below that a resync is recoverable and a dropped change is
not. Deriving the scope from the module that owns the id scheme is what makes
the next covered office entity a compile-and-test problem rather than a silent
one.

A ``hydrate`` or ``delta`` frame means the batch was NOT patch-coverable
(``batch_is_patch_coverable`` said no — a create moved ``actor_count``, an
archive rewrote the resurrection ledger), so there is no honest office patch to
send. That becomes :data:`OFFICE_RESYNC_METHOD` and the client re-subscribes.
An UNKNOWN frame type takes the same branch deliberately: a type this module
has not been taught is, by definition, a change it cannot express as a patch,
and the conservative direction is a refetch rather than a silent gap.

``heartbeat`` is skipped. It is the legacy lane's liveness signal and carries no
state; the subscribe lane's own bookmark/heartbeat is separate, undesigned work
and must not be faked out of this.

The baseline rule
-----------------
ONE rule absorbs two different seams, which is why it is stated as an offset
comparison rather than as two guards:

* :meth:`StreamHub.subscribe` deliberately restarts the producer so a late
  joiner's first frame is a hydrate. We do not want that hydrate — the
  subscribe REPLY already carried the baseline.
* the dispatcher emits a handler's reply AFTER the handler returns, so a patch
  pushed in between lands BEFORE the baseline it rebases on.

Both are "this frame is at or behind what the client already has", so both are
answered by dropping any frame whose ``watermark.event_offset`` does not exceed
the offset captured with the baseline. That capture happens under the office
lock in :mod:`agent_runtime.serve_rpc`; here it is just a number.

Why a second subscribe REPLACES rather than being refused
---------------------------------------------------------
Registering and answering in one call is what makes the baseline honest, and it
has one consequence that was not thought through when the lane landed: by the
time the client has PARSED the reply the subscription already exists. A client
that reads the baseline and finds it unusable — a truncated projection, an
unreadable watermark, a workspace id it did not ask for — is right to refuse it
rather than fold a knowingly-partial office as authoritative. But refusing left
a live subscription it would never fold against, and the old
``already_subscribed`` answer meant a retry on the same connection could not
reclaim it. Short of dropping the connection, the runtime could not take that
subscriber back.

So a second subscribe for the same ``(connection, workspace)`` now TEARS DOWN
the existing registration and installs a fresh sink at a fresh baseline. The
stuck subscription stops being a state that can exist, because the cure for a
bad baseline is the same call that produced it. The refusal it replaces existed
only to stop a subscriber leaking per retry, and replacement prevents that leak
by the same mechanism — one key, one subscription — while being useful rather
than merely safe.

It is not free, and it must not look free
-----------------------------------------
:meth:`StreamHub.subscribe` restarts the producer on purpose, so every OTHER
subscriber attached to that hub pays a fresh full core when this connection
re-baselines. Under the old refusal a redundant subscribe cost nothing at all —
the hub declined the duplicate key before it ever bumped a generation — so this
is a real new cost on the path a confused client takes repeatedly. (It is one
restart and not two: the teardown below stops a producer without starting one,
and the re-subscribe starts the single new generation any first subscribe would
have started. What changed is that a REDUNDANT subscribe went from free to
costing exactly what a first one costs.)

That is why a replacement is a RECEIPT rather than a silent success: the reply
carries ``replaced`` and a bound service log is told. A client in a retry loop
taxing the whole room is something an operator must be able to see in the log
and the client must be able to see in its own answer. The one shape this
program keeps refusing to ship is the cost nobody is billed for.

Giving a subscription back without dropping the connection
----------------------------------------------------------
:meth:`OfficeSubscriptions.release_one` is the other half. ``release`` sweeps a
DEPARTING connection and is the disconnect path's; a client that merely gave up
on a workspace, or navigated away from it, needs to hand one subscription back
while keeping the socket it is still using for everything else. Releasing
something already released answers False rather than raising, because that is
precisely what a recovering client does and an error there would make recovery
indistinguishable from a fault.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .patch_coverage import HISTORICAL_FOLD_ENTITIES
from .serve_rpc import notification
from .state_patches import (
    OFFICE_ACTOR_ENTITY,
    STATE_PATCHED_EVENT_TYPE,
    office_patch_scope,
)

#: What an RPC office subscriber declares it can fold — the historical set PLUS
#: ``office_actor``, and the union half of that is the whole point.
#:
#: **Why not ``{office_actor}`` alone.** The accepted set is the INTERSECTION
#: over everybody attached to the shared producer
#: (:func:`~agent_runtime.patch_coverage.accepted_fold_entities`), and a client
#: that declares nothing contributes
#: :data:`~agent_runtime.patch_coverage.HISTORICAL_FOLD_ENTITIES`. A bare
#: ``{office_actor}`` therefore intersects to the EMPTY SET against any legacy
#: stream client in the room: nothing would be promotable for anyone, and the
#: persona-instance patch lane that works today would go dark the moment an
#: office subscriber joined. That is a regression wearing a fix's clothes, and
#: it is exactly what the intersection rule exists to make visible.
#:
#: **Why the superset is honest and not merely convenient.** A declaration is a
#: promise to fold, and this subscriber keeps it for every entity named.
#: :func:`office_patch_sink` discards every row that is not an ``office_actor``
#: under its own workspace — and a discard is a CORRECT fold for an entity a
#: subscriber does not track: the office lane is a projection of one
#: workspace's actors, not a read model of the whole core, so a persona-instance
#: patch fanned out to this sink leaves nothing it holds stale.
#:
#: **Why not widen HISTORICAL_FOLD_ENTITIES instead.** That constant is what a
#: client which said NOTHING is taken to fold, so widening it would declare
#: ``office_actor`` on behalf of every un-updated client in the field — see its
#: own comment: it may only name entities every fielded client already folds.
#: This one declares for exactly the subscriber that can keep the promise.
#:
#: **Since 2026-08-16 it is the FAIL-OPEN DEFAULT, not the authority.** It was
#: the authority while it was the only answer available: the constant is
#: SERVER-side, so it says what an office subscriber can fold in general and
#: cannot say what the CONNECTED one can. That gap is exactly the hole the
#: capability token opened (plan §V4) — the runtime could not learn whether this
#: client's fold had been widened, so it could never promote a lifecycle row on
#: this lane at all. ``runtime.office.subscribe`` now takes an optional
#: ``fold_entities``, and this constant is what an absent one resolves to:
#: today's behaviour for every client that has not been taught to declare.
#: Retiring it is deferred (D5) until no fielded launcher omits the param.
OFFICE_FOLD_ENTITIES: frozenset[str] = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}


def normalize_office_fold_entities(declared: Any) -> frozenset[str] | None:
    """The trust-boundary normalizer for a subscribe's ``fold_entities`` param.

    ``None`` — the key was absent — stays ``None`` all the way to
    :meth:`OfficeSubscriptions.subscribe`, which resolves it to
    :data:`OFFICE_FOLD_ENTITIES`. That distinction is the whole fail-open rule:
    "said nothing" (every client in the field) must not collapse into "said
    empty" (a client explicitly asking for full cores), which is the same
    discipline :func:`~agent_runtime.patch_coverage.normalize_fold_entities`
    keeps one layer down.

    Degenerate MEMBERS are normalized away — non-strings, blanks, surrounding
    whitespace — because a declaration is a set of names and a blank is not one.
    UNKNOWN members are passed through untouched: the declaration channel has
    never interpreted its strings, and a server that filtered to a known
    vocabulary here would silently drop the next capability token the way this
    one would have been dropped.

    Returns ``None`` for a non-list value too; the caller refuses that at the
    boundary rather than guessing, so a client sending the wrong shape learns it
    instead of being quietly treated as legacy.
    """

    if not isinstance(declared, (list, tuple)):
        return None
    return frozenset(
        value.strip() for value in declared if isinstance(value, str) and value.strip()
    )


#: What a subscribe receipt prints when the client sent no ``reason``. A
#: SENTINEL rather than an omitted key, for the same reason ``replaced`` is
#: always present on the reply: a field that appears only sometimes is one an
#: operator learns to stop reading, and "this client is too old to say" and
#: "this client said nothing" must both be visible as a fact rather than as a
#: gap in the line.
SUBSCRIBE_REASON_ABSENT = "-"

#: The cause is a LOG TOKEN, not free text. The launcher's own resubscribe
#: chokepoint already mints exactly this shape (``start``, ``fold:fenced``,
#: ``push:full_core``, ``reconnect``, ``deferred:*``, ``fold_threw``), so the
#: charset is the one those causes live in and nothing more.
SUBSCRIBE_REASON_MAX_CHARS = 64
_SUBSCRIBE_REASON_RE = re.compile(r"^[a-z0-9_:.-]{1,%d}$" % SUBSCRIBE_REASON_MAX_CHARS)


def normalize_office_subscribe_reason(declared: Any) -> str | None:
    """The trust-boundary normalizer for a subscribe's ``reason`` param.

    Same two-value shape as :func:`normalize_office_fold_entities`, and read the
    same way by the caller: ``None`` means REFUSE. The caller has already
    established the key was present, so there is no "absent" case to encode here
    — absence never reaches this function.

    The value is taken VERBATIM or not at all. No stripping, no lowercasing, no
    truncation: the whole point of the param is that the token on the server
    receipt is the token the launcher printed in its own ladder, and a server
    that silently repaired a value would make the two logs agree about a string
    neither side actually used. A client sending the wrong shape learns it.

    Bounded and charset-restricted because this string is written to the service
    log on a path a confused client takes repeatedly (Plan D covert-channel
    discipline V1): an unbounded echo is a free write primitive into an
    operator's tail, and a newline in it would forge a log line. ``[a-z0-9_:.-]``
    excludes whitespace, quotes and control bytes by construction, so there is
    nothing left to escape.

    A blank or all-whitespace value is REFUSED rather than treated as absent.
    Absence is already expressible — omit the key — so a client that sends an
    empty cause has a bug in its cause plumbing, and quietly filing it as
    "said nothing" would hide exactly the thing this param exists to reveal.
    """

    if not isinstance(declared, str):
        return None
    return declared if _SUBSCRIBE_REASON_RE.match(declared) else None


#: The push. Params mirror the patch frame's own body minus its envelope, so a
#: client's fold is byte-identical work on either lane — which is what makes
#: the legacy frames deletable rather than merely deprecated.
OFFICE_PATCH_METHOD = "runtime.office.patch"

#: "I cannot express what just happened as a patch — call subscribe again."
#: Carries the workspace and a reason and nothing else: a resync that shipped
#: partial state would be a third lane.
OFFICE_RESYNC_METHOD = "runtime.office.resync"

#: The join. Named here as well as on `serve_rpc`'s `@method` decorator for
#: exactly one reason: the attach line this module logs prints the op as the
#: CLIENT called it, and a hand-typed string in a log line is how a rename ships
#: a log that names a method nobody can call.
OFFICE_SUBSCRIBE_METHOD = "runtime.office.subscribe"

#: Frame types that are a FULL CORE for this lane's purposes: the batch was not
#: patch-coverable, so no office patch exists to forward.
_FULL_CORE_FRAME_TYPES = frozenset({"hydrate", "delta"})

#: The one frame type that ENUMERATES what it carried, and therefore the only
#: one whose relevance to a given workspace is decidable (see
#: :func:`_delta_touches_workspace`). A ``hydrate`` says only "here is
#: everything", which is precisely the case that cannot be scoped.
_ENUMERATED_FRAME_TYPE = "delta"

#: Realm-sync watermarks. A sync is scoped to a REALM and rewrites store state
#: from OUTSIDE this machine's own write lane; :func:`_delta_touches_workspace`
#: is asked about a WORKSPACE. The payload cannot answer that question, so the
#: answer is the conservative one — see the third arm's block comment.
_REALM_SYNC_EVENT_TYPES = frozenset({"realm.sync.pulled", "realm.sync.published"})


def _delta_touches_workspace(frame: dict[str, Any], workspace_id: str) -> bool | None:
    """Did this uncovered batch carry anything for ``workspace_id``?

    ``True``/``False`` when the frame's ``events`` list can be read; ``None``
    when it cannot — a THIRD answer, not a False, because a frame this function
    cannot enumerate is exactly the case where the conservative resync is the
    honest reply. Coercing it to "no" would silently drop a change.

    Why this is answerable at all: a coalesced ``delta`` carries ``events`` —
    one redaction-safe block per batched event, built by ``stream._delta_entity``
    — so the frame already says what it was about. Two things count as touching
    this workspace, and they are different facts rather than one restated:

    * an ``office.*`` domain event whose payload names this ``workspace_id``
      (the surface/actor writes that are uncovered ON PURPOSE and genuinely need
      a refetch here);
    * a ``state.patched`` whose office scope IS this workspace — a patch that rode
      in an UNCOVERABLE batch, i.e. one demoted by something else in the same
      drain. The office row moved and this lane is not getting a patch frame for
      it, so it must refetch. Scoped by
      :func:`~agent_runtime.state_patches.office_patch_scope`, the SAME authority
      the patch sink below uses: this arm read ``office_actor`` only, and while it
      happened to be saved for folder writes by the ``office.*`` arm above, "saved
      by its neighbour" is not an invariant — the twin that was NOT saved dropped
      the change outright (task #57).
    * a REALM-SYNC watermark (:data:`_REALM_SYNC_EVENT_TYPES`) — see below.

    Everything else — an agent's turn, a board write, another workspace's
    office — moved nothing this subscriber holds.
    """

    events = frame.get("events")
    if not isinstance(events, list):
        return None
    for entry in events:
        if not isinstance(entry, dict):
            return None
        event = entry.get("event")
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return None
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if event_type in _REALM_SYNC_EVENT_TYPES:
            # THE THIRD ARM, and the only one that answers True without reading
            # the payload — because the payload cannot answer the question.
            # ``realm.sync.pulled`` carries ``{realm_id, changed, artifacts}``
            # (``realm_sync._append_realm_sync_event``): no workspace, no office
            # scope. A sync is scoped to a REALM and this function is asked about
            # a WORKSPACE, so the honest answer is the same one the ``None``
            # branches above already get — the module's stated rule for a
            # question it cannot answer ("A resync is recoverable; a dropped
            # change is not", see ``office_patch_sink``) — spelled as an explicit
            # True so the reason is legible at the arm.
            #
            # WHY NOT WIDEN THE PAYLOAD instead. Adding ``workspace_ids`` to the
            # sync events was considered and rejected: the EventLog has a 4 KB
            # payload cap, a realm with many workspaces would blow it, and a
            # TRUNCATED list is a silent drop — the exact failure class this arm
            # exists to refuse. The conservative arm costs one refetch per realm
            # sync, which is bounded by operator gestures.
            #
            # Both members earn their place. ``.pulled`` is the inbound half
            # (a pull rewrites office rows through
            # ``office_sync.apply_office_pull``); ``.published`` is the outbound
            # half and fires TODAY — publish rewrites ``office_baseline.json``,
            # which flips every actor's DERIVED ``unpublished`` marker with no
            # office event of its own, so a published canvas keeps rendering
            # "unpublished" desks until something else moves.
            return True
        if event_type.startswith("office."):
            # An office event that does not name its workspace is unplaceable,
            # and unplaceable takes the conservative arm like every other
            # ambiguity on this lane.
            named = payload.get("workspace_id")
            if not isinstance(named, str) or not named:
                return None
            if named == workspace_id:
                return True
            continue
        if event_type == STATE_PATCHED_EVENT_TYPE:
            if office_patch_scope(payload) == workspace_id:
                return True
    return False

#: Carries no state, so it is neither a patch nor a resync.
_LIVENESS_FRAME_TYPES = frozenset({"heartbeat"})


def office_subscription_key(connection_key: str | None, workspace_id: str) -> str:
    """The hub key for one connection's subscription to one workspace.

    Namespaced, and that is not cosmetic. ``serve.py``'s stream lane already
    subscribes under the BARE connection key (``_owner_of``), and
    :meth:`StreamHub.subscribe` refuses a duplicate key — so an office
    subscription that reused it would either be refused or, worse, silently
    take the stream client's place. The prefix also lets the release path find
    every office subscription a departing connection owns without keeping a
    second index in sync with the hub's.
    """

    return f"rpc:office:{connection_key or 'stdio'}:{workspace_id}"


def event_offset_of(watermark: Any) -> int | None:
    """The offset a watermark block carries, or None when it carries none.

    None is a THIRD answer, not a zero. A frame we cannot place must not be
    silently dropped by the baseline gate — an unplaceable frame is exactly the
    case where a resync is the honest reply, and coercing it to 0 would make it
    look like ancient history and drop it.

    PUBLIC, and asked of ``parity.events_watermark()``'s block as well as of a
    frame's (``serve_rpc._runtime_office_subscribe``). "What position does this
    watermark state?" is ONE question, and it now has one answer: the subscribe
    handler used to ask it a second way (``int(… or 0)``) and got the opposite
    one, folding an unreadable log into the head of the log. The three absences
    — no block, an explicit ``None`` from a failed stat, an unparseable value —
    are all "no position", and none of them is 0.
    """

    if not isinstance(watermark, dict):
        return None
    raw = watermark.get("event_offset")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def office_patch_sink(
    *,
    workspace_id: str,
    baseline_offset: int,
    emit: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any]], None]:
    """Adapt stream frames to office notifications for ONE subscriber.

    Returned as a closure rather than a method so it can be unit-tested against
    hand-built frames without a hub, a socket or a serve loop — the filtering
    rules are where this lane can go quietly wrong, and they deserve a test that
    is not an integration test.
    """

    def _deliver(frame: dict[str, Any]) -> None:
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        if frame_type in _LIVENESS_FRAME_TYPES:
            return
        watermark = frame.get("watermark") or {}
        event_offset = event_offset_of(watermark)
        # THE BASELINE GATE RUNS BEFORE THE TYPE BRANCH, and that ordering is
        # the whole rule rather than a detail.
        #
        # It was written the other way round first, and the lane did not work:
        # `StreamHub.subscribe` deliberately restarts the producer so a late
        # joiner's first frame is a HYDRATE — a full core — which the type
        # branch answered with an unconditional resync. That is a LOOP, not a
        # slow start: the client resyncs, re-subscribes, the hub restarts the
        # producer, the new hydrate resyncs it again, and every other
        # subscriber on that shared producer pays a fresh core each lap. The 23
        # fake-hub tests could not see it, because a fake never delivers a
        # frame nobody asked for.
        #
        # Stated positively: a frame at or behind what the subscribe reply
        # already carried has nothing to tell this client, WHATEVER its type.
        # Only frames that are genuinely new get classified.
        if event_offset is not None and event_offset <= int(baseline_offset or 0):
            return
        if frame_type != "patch":
            # SCOPED (office fold-promotion plan O-H4, 2026-08-16). An uncovered
            # batch used to resync EVERY office subscription runtime-wide, which
            # meant an agent's turn, a board write or another workspace's office
            # edit each cost this lane a full re-subscribe — and a re-subscribe
            # restarts the shared producer, billing every OTHER subscriber a
            # fresh ~822 KB core for a frame that carried nothing for anyone
            # here. Background activity alone could walk the client up the
            # 250ms→500ms→1s backoff and park it at 5 in 60s, which is the park
            # threshold being consumed by noise instead of by defects.
            #
            # Only a ``delta`` is scopable, and the asymmetry is the rule rather
            # than an optimisation: a delta ENUMERATES its batch (``events``), so
            # its relevance is decidable; a hydrate says "here is everything" and
            # a type this module has not been taught says nothing at all. Both
            # keep the unconditional resync — upstream's ``full`` bit, by
            # another name.
            #
            # CAVEAT for a future stream-less client: skipping is sound TODAY
            # because the stream lane owns read-model currency and this
            # subscriber's canvas is a projection of one workspace. A client
            # whose only lane is this one would need the skip path to advance a
            # bookmark before this rule could carry it alone.
            if frame_type == _ENUMERATED_FRAME_TYPE and (
                _delta_touches_workspace(frame, workspace_id) is False
            ):
                return
            # Includes the unknown-type branch on purpose — see the module
            # docstring. A resync is recoverable; a dropped change is not.
            #
            # An UNPLACEABLE frame (no readable watermark) reaches here too, and
            # must: the gate above deliberately does not fire without an offset,
            # because silently dropping a frame we cannot place would turn the
            # one recoverable outcome into the unrecoverable one.
            emit(
                notification(
                    OFFICE_RESYNC_METHOD,
                    {
                        "workspace_id": workspace_id,
                        "reason": "full_core"
                        if frame_type in _FULL_CORE_FRAME_TYPES
                        else "unknown_frame_type",
                    },
                )
            )
            return
        rows = [patch for patch in frame.get("patches") or [] if isinstance(patch, dict)]
        # ONE SCOPE AUTHORITY (RD-H1 / task #57). This was a private restatement
        # of the id scheme that knew only ``office_actor`` and its slash-prefixed
        # id — so when WV-H3 widened what may PROMOTE to include ``office_surface``
        # (bare workspace id, no slash), a folder-only frame failed BOTH conjuncts
        # and took the bare ``return`` below: no patch, no resync, the change gone.
        # ``office_patch_scope`` lives beside the id builders it reads backwards,
        # and ``_delta_touches_workspace`` above calls the same function, so the
        # promotion vocabulary and this lane's scope can no longer fork.
        in_scope = any(office_patch_scope(patch) == workspace_id for patch in rows)
        if not in_scope:
            # Addressed, not broadcast: a batch that moved another workspace's
            # actors is not this subscriber's business and costs it nothing.
            # UNCHANGED by the completeness fix below — nothing in scope still
            # means nothing sent.
            return
        # THE WHOLE BATCH, not this workspace's rows (office fold-promotion plan
        # §V6, 2026-08-16). This used to forward a workspace-FILTERED subset
        # stamped with the FULL batch's watermark, and that combination is a
        # silent data-loss race the moment mixed batches start promoting:
        #
        # The launcher folds BOTH transports into ONE `MissionReadModel` with
        # ONE sequence and a `base == held` gate. A delete gesture's batch is
        # `[persona_instance remove, office_actor remove]`. Under the old filter
        # this lane forwarded only the office row while CLAIMING the full span —
        # so if it folded first, the stream lane's frame at the same watermark
        # was dropped as stale and the persona remove NEVER APPLIED. Silently
        # stale roster, unrecoverable by any gate, because the watermark says
        # the span was already applied.
        #
        # Forwarding whole is the cheap half of the fix (O-L2 is the per-row
        # `seq` half). It is affordable by construction: every row is bounded by
        # the 4 KB EventLog payload cap, and the client's fold already handles
        # every entity — one body for both lanes — so a row it does not track is
        # a correct no-op rather than an error. It also stops being a special
        # case the day the launcher leaves the second producer behind (D7).
        #
        # The rows are NOT re-filtered by entity either: a `persona_instance`
        # row in this batch is real state at this watermark, and dropping it
        # while claiming the watermark is the same bug one entity over.
        emit(
            notification(
                OFFICE_PATCH_METHOD,
                {
                    "workspace_id": workspace_id,
                    "base_offset": frame.get("base_offset"),
                    "watermark": watermark,
                    "patches": rows,
                },
            )
        )

    return _deliver


@dataclass(frozen=True)
class SubscribeOutcome:
    """What a subscribe DID — not merely whether it worked.

    A bare ``bool`` was enough while the only two answers were "registered" and
    "refused". Re-baselining adds a third fact that the caller has to be able to
    put on the wire: whether this registration DISPLACED a live one, and
    therefore whether every other subscriber on the shared producer just paid a
    fresh full core for it. That is the receipt, and a receipt that the handler
    cannot see is not a receipt.

    ``reason`` is the second thing a bool could not carry, and it is not
    cosmetic either. ``StreamHub.subscribe`` answers False for two unrelated
    situations, and until now the handler guessed between them by asking
    ``bound()`` — which cannot tell "no hub at all" from "a bound factory whose
    hub is draining", and so told a client racing ``_close_socket_lane`` that it
    was ALREADY SUBSCRIBED. That was a lie in the one direction that matters: it
    points the client at a cure (stop retrying, you are already registered) for
    a state where the only cure is to reconnect. Removing the
    ``already_subscribed`` arm would otherwise have silently rehomed that
    mislabel onto ``push_lane_unavailable``, which is nearly as wrong — a
    permanent runtime fact where the truth is a transient one.

    ``__bool__`` is defined deliberately rather than left to the dataclass
    default. Every dataclass instance is truthy, so a caller who wrote the
    natural ``if not registered:`` against this type would get a branch that can
    never be taken — a refusal reported as a success, which is the exact failure
    shape this lane exists to delete.
    """

    registered: bool
    replaced: bool = False
    #: ``None`` when registered. Otherwise the typed reason the handler puts on
    #: the wire verbatim, because the registry is the only layer that can tell
    #: these two apart.
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.registered


#: No hub is bound at all: this runtime has no socket lane, or the serve loop
#: has not reached its bind yet. A fact about the runtime, not about the call.
NO_PUSH_LANE = "push_lane_unavailable"

#: A hub IS bound but refused the registration, which after re-baselining has
#: exactly one remaining cause: the hub is stopping (``StreamHub.subscribe``
#: returns False once its stop event is set). Transient by nature — the cure is
#: to reconnect and subscribe again, never to assume this runtime cannot push.
PUSH_LANE_DRAINING = "push_lane_draining"

#: The event log's tail could not be read, so there is no offset to baseline a
#: subscription at. The THIRD member of this vocabulary, and transient for the
#: same reason ``PUSH_LANE_DRAINING`` is: the cure is to ask again once the log
#: is readable, never to conclude this runtime cannot push.
#:
#: It exists because the handler used to answer this state with ``0`` — the one
#: value ``parity.events_watermark`` documents as maximally damaging, because
#: every reader takes it for a real position. A baseline of 0 disables the
#: sink's own ``<=`` gate, so the hub's mandatory post-subscribe hydrate is
#: forwarded as a resync, the client re-subscribes, the producer restarts, and
#: the next hydrate resyncs it again — the loop the baseline-before-type
#: ordering in :func:`office_patch_sink` exists to end, at a full core per lap
#: for every subscriber in the room. Refusing costs one reply; guessing costs
#: the room.
BASELINE_UNAVAILABLE = "baseline_unavailable"


class OfficeSubscriptions:
    """Which connections are subscribed to which workspaces, and the teardown.

    Process-global by the same reasoning as ``serve_rpc._METHODS``: a method
    handler is reached through a module-level registry and has no server object
    to ask, and threading a hub through :class:`~agent_runtime.serve_rpc.
    RpcContext` would put a server-wide service on a per-request value that is
    documented as answering WHO called.

    The hub arrives as a FACTORY, not an instance. ``serve.py`` builds its hub
    lazily on first use and can close and replace it across a drain, so a
    captured instance would go stale exactly when a client reconnects.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hub_factory: Callable[[], Any] | None = None
        #: connection key -> the hub keys it owns. The hub is the authority on
        #: whether a subscription is live; this is only the index the release
        #: path needs, and it is pruned in the same breath as the hub call.
        self._owned: dict[str, set[str]] = {}
        #: hub key -> what THAT subscription declared it can fold. Keyed by hub
        #: key rather than by connection because two workspaces on one socket are
        #: two subscriptions and each is entitled to its own answer; the shared
        #: producer intersects over all of them either way.
        #:
        #: A key absent from here is a subscription that declared nothing, and
        #: :meth:`declarations` resolves it to :data:`OFFICE_FOLD_ENTITIES` —
        #: fail-open, so a client that has never heard of the param keeps
        #: today's wire exactly.
        self._declared: dict[str, frozenset[str]] = {}
        #: Where a re-baseline is BILLED. Optional because this registry is
        #: process-global and reachable from a runtime that has no service log
        #: (a test, a stdio probe); ``None`` means the receipt still reaches the
        #: client on its reply, just not an operator's tail.
        self._log: Callable[[dict[str, Any]], None] | None = None
        #: ``serve.py``'s ``_accepted_fold_entities``, when one was bound. The
        #: room spans both lanes and only serve.py can see both tables, so this
        #: is borrowed rather than re-derived — see :meth:`bind`.
        self._accepted: Callable[[], Any] | None = None

    # ── wiring ──────────────────────────────────────────────────────────────

    def bind(
        self,
        hub_factory: Callable[[], Any] | None,
        *,
        log: Callable[[dict[str, Any]], None] | None = None,
        accepted_fold_entities: Callable[[], Any] | None = None,
    ) -> None:
        """Point the lane at a hub factory, and optionally at a service log.

        ``log`` rides ``bind`` rather than being a constructor argument for the
        same reason the hub does: this object outlives any one serve loop, and
        the log it should write to belongs to whichever loop is currently
        running. Unbinding clears it, so a stopped loop's sink cannot be handed
        a line by the next one.

        ``accepted_fold_entities`` is ``serve.py``'s own
        ``_accepted_fold_entities``, and it is what makes a restart-free rejoin
        decidable HERE. The registry knows what each office subscription
        declared, but the room is BOTH LANES — the socket stream lane's
        declaration table lives in a serve-loop closure and is not reachable
        from a process-global registry. Passing the derivation in (rather than
        re-implementing half of it) keeps one authority for what the room
        accepts, which is the property the office lane already lost once by
        reading only one of the two tables.

        Absent — a test, a stdio probe — the lane simply keeps restarting on
        every join, i.e. today's behaviour. A missing probe must degrade to
        correct-and-expensive, never to cheap-and-wrong.
        """

        with self._lock:
            self._hub_factory = hub_factory
            self._log = log if hub_factory is not None else None
            self._accepted = accepted_fold_entities if hub_factory is not None else None
            if hub_factory is None:
                self._owned.clear()
                self._declared.clear()

    def bound(self) -> bool:
        with self._lock:
            return self._hub_factory is not None

    def service_log(self) -> Callable[[dict[str, Any]], None] | None:
        """The running serve loop's log sink, or None when nothing is bound.

        Exposed for the ONE refusal the handler decides before it ever reaches
        :meth:`subscribe` — an unreadable event log (:data:`BASELINE_UNAVAILABLE`).
        The registry writes its own receipts from inside the lock, but a refusal
        that never gets that far still has to be attributable: an operator seeing
        clients fail to subscribe needs the platform error class that caused it,
        and that class is discarded by the time the reply is on the wire.

        Read through the lock, and cleared by ``bind(None)`` like ``_log``
        itself, so a stopped loop's sink can never be handed a line.
        """

        with self._lock:
            return self._log

    # ── the lane ────────────────────────────────────────────────────────────

    def subscribe(
        self,
        *,
        connection_key: str | None,
        workspace_id: str,
        baseline_offset: int,
        emit: Callable[[dict[str, Any]], None],
        fold_entities: frozenset[str] | None = None,
        reason: str | None = None,
    ) -> SubscribeOutcome:
        """Register one subscription, REPLACING this connection's existing one.

        ``fold_entities`` is what THIS client says it can fold. ``None`` means it
        said nothing and resolves to :data:`OFFICE_FOLD_ENTITIES` — see that
        constant for why the server-side default had to stop being the
        authority, and :func:`normalize_office_fold_entities` for why "said
        nothing" and "said empty" must stay distinguishable this far down.

        ``reason`` is WHY this client is subscribing — its own resubscribe cause,
        already boundary-validated by the handler. It is carried for exactly one
        purpose: to be stamped on the re-baseline receipt, so an operator reading
        the service log can tell a ``fold:fenced`` ladder from a
        ``push:full_core`` one WITHOUT joining two logs on timestamps. It
        influences no decision here and must not: a cause the client chose is
        evidence, never authority.

        A falsy outcome — rather than a raise — is reserved for the two states a
        caller must answer differently from a crash, and ``reason`` is what
        tells them apart: no hub is bound at all (:data:`NO_PUSH_LANE`), or a
        bound hub refused because it is stopping (:data:`PUSH_LANE_DRAINING`).
        Neither is a fault, and their cures are opposites — give up on pushes,
        versus reconnect and ask again — which is why the registry names them
        here rather than leaving the handler to guess from ``bound()``.

        A duplicate key is no longer among those states. It used to be, and the
        module docstring above says why it stopped being: refusing left the
        client holding a subscription it had already decided not to fold
        against, with no way back short of dropping the connection.
        """

        with self._lock:
            factory = self._hub_factory
            log = self._log
            accepted_probe = self._accepted
        if factory is None:
            return SubscribeOutcome(False, reason=NO_PUSH_LANE)
        hub = factory()
        if hub is None:
            # A BOUND factory that answers None is still "no lane", not a
            # duplicate — the case the old ``bound()`` guess reported as
            # ``already_subscribed`` to a client that held no subscription.
            return SubscribeOutcome(False, reason=NO_PUSH_LANE)
        key = office_subscription_key(connection_key, workspace_id)
        sink = office_patch_sink(
            workspace_id=workspace_id,
            baseline_offset=baseline_offset,
            emit=emit,
        )

        def _on_drop(_key: str, payload: dict[str, Any]) -> None:
            # A dropped subscriber missed frames by definition, so the honest
            # thing is not silence: tell it to refetch. Best-effort — the drop
            # may itself be a dead socket, and the hub has already accounted it.
            try:
                emit(
                    notification(
                        OFFICE_RESYNC_METHOD,
                        {
                            "workspace_id": workspace_id,
                            "reason": str(payload.get("reason") or "subscription_dropped"),
                        },
                    )
                )
            except Exception:
                pass

        # RE-BASELINE: the old registration goes FIRST, and it has to. The hub
        # refuses a duplicate key, so there is no atomic swap available to us
        # and no ordering in which the new sink is installed before the old one
        # is gone. What makes that safe for this lane rather than a gap is the
        # caller: ``_runtime_office_subscribe`` holds ``office_lock`` across the
        # projection, the watermark and this call, so no office write can land
        # inside the window the teardown opens. A caller that dropped the lock
        # here would be re-introducing exactly the unreportable window that
        # made subscribe one call in the first place.
        # WHETHER THIS JOIN NEEDS A PRODUCER RESTART, decided BEFORE the
        # teardown and before this subscription's own declaration is recorded
        # (office fold-promotion plan O-H5, 2026-08-16).
        #
        # ``StreamHub.subscribe`` restarts by contract so a late joiner's first
        # frame is a hydrate. This lane does not want that hydrate — the
        # subscribe REPLY already carried the baseline, and the sink provably
        # discards everything at or behind it — so every re-baseline was
        # manufacturing an ~822 KB core to throw away, and billing every other
        # subscriber in the room for it.
        #
        # It is only safe to skip when the join cannot NARROW what the room may
        # promote. A running producer was built against the accepted set in
        # force; if this joiner's declaration is a superset, the new accepted set
        # is unchanged and every row that producer promotes is still foldable by
        # everyone including the joiner. Narrowing without a restart would leave
        # a producer promoting rows the room can no longer fold — the exact
        # regression the negotiation exists to prevent.
        #
        # Measured against the SET IN FORCE via serve.py's own derivation, taken
        # before this declaration is recorded so the joiner cannot answer its own
        # question. A departed subscriber makes that reading WIDER than the
        # running producer's true set, which makes the superset test stricter,
        # never looser — the safe direction. Any failure to read it falls back to
        # restarting.
        restart_producer = True
        if accepted_probe is not None:
            declared = fold_entities if fold_entities is not None else OFFICE_FOLD_ENTITIES
            try:
                in_force = frozenset(accepted_probe())
                producer_live = bool(hub.stats().get("producer_running"))
            except Exception:
                in_force, producer_live = None, False
            if producer_live and in_force is not None and frozenset(declared) >= in_force:
                restart_producer = False
        try:
            replaced = bool(hub.unsubscribe(key))
        except Exception:
            # The hub's own accounting has the truth; a teardown that raised
            # must not turn a re-baseline into a handler error, because the
            # client asking for one is already trying to recover.
            replaced = False
        # The declaration is recorded BEFORE ``hub.subscribe``, and the order is
        # load-bearing. That call bumps the hub's generation and starts a
        # producer thread, and the producer asks :meth:`declarations` what it
        # may promote. An index written afterwards would race that thread: this
        # subscription would be missing from the very producer its own subscribe
        # created, and the first office write after a join would demote to a
        # full core for no reason a reader could find. The stream lane records
        # its own declaration before ``hub.subscribe`` for the same reason.
        #
        # The re-baseline above is what lets this be unconditional. An earlier
        # version needed a ``declared_here`` guard so a REFUSED duplicate could
        # not withdraw the LIVE subscription's declaration on its way out; a
        # duplicate is impossible here now, because the key was torn down a few
        # lines up and the only registration in play is this one.
        with self._lock:
            self._owned.setdefault(str(connection_key or "stdio"), set()).add(key)
            # Recorded in the same breath and under the same lock, for the same
            # reason: the producer this subscribe is about to start reads
            # ``declarations()``, and a declaration written after would race the
            # thread its own call created.
            if fold_entities is None:
                self._declared.pop(key, None)
            else:
                self._declared[key] = frozenset(fold_entities)
        # The generation immediately before the join, so the receipt below can
        # report what the hub ACTUALLY did rather than what was asked for. The
        # two can differ legitimately: the teardown above empties the room when
        # this connection was its only member, which stops the producer, and the
        # hub's own floor then starts a fresh one however this call was flagged.
        # A receipt that reported the REQUEST would say ``producer_restarted:
        # false`` for a join that restarted — the kind of cost-nobody-is-billed
        # -for this lane's receipts exist to prevent.
        try:
            generation_before = int(hub.stats().get("generation") or 0)
        except Exception:
            generation_before = -1
        if not hub.subscribe(
            key,
            sink=sink,
            on_drop=_on_drop,
            restart_producer=restart_producer,
            # What this pump resolves a per-subscriber ``fold_variants`` envelope
            # against. It must be the SAME value :meth:`declarations` contributes
            # to the room — an office subscriber that declared nothing folds
            # :data:`OFFICE_FOLD_ENTITIES`, not the historical stream set — or a
            # split frame would hand this sink a core for the very rows the push
            # lane exists to patch, and the negotiation would be worse than it
            # was before the split existed.
            declared=(
                fold_entities if fold_entities is not None else OFFICE_FOLD_ENTITIES
            ),
        ):
            # ONE cause remains now that a duplicate key is impossible here: the
            # hub is stopping (``StreamHub.subscribe`` refuses once its stop
            # event is set), i.e. this call raced ``_close_socket_lane``. It is
            # reported as its own transient reason rather than folded into
            # ``push_lane_unavailable``, because the cures differ — reconnect,
            # versus give up on pushes entirely.
            #
            # ``replaced`` is carried out with it. The teardown above ran, so a
            # client that DID hold a subscription no longer does, and telling it
            # only "refused" would leave it believing its old lane survived.
            # Tearing that lane down early costs nothing: ``StreamHub.stop``
            # releases every subscriber moments later anyway.
            #
            # The index must lose the key in the same breath — a key kept here
            # that the hub does not hold would make ``release`` report a
            # teardown it never performed.
            self._forget(connection_key, key)
            return SubscribeOutcome(False, replaced=replaced, reason=PUSH_LANE_DRAINING)
        try:
            generation_after = int(hub.stats().get("generation") or 0)
        except Exception:
            generation_after = generation_before
        # ONE line per office ATTACHMENT, in the serve child's OWN log. The
        # receipt below is a different lane and a different question: it rides
        # `log` (serve's stderr, read by the supervising launcher) and only
        # fires on a RE-baseline. Nothing wrote an attach line into the child's
        # log at all, and the measured consequence is plan §8 item 5: 12 MB of
        # serve-child log with ZERO office lines, which leaves "is the push lane
        # even attached?" unanswerable from the log the operator actually has —
        # and left the boot's third stream rider unidentifiable (EG-2.1).
        from .stream import log_stream_attach

        log_stream_attach(
            op=OFFICE_SUBSCRIBE_METHOD,
            purpose="office_patch",
            connection=str(connection_key or "stdio"),
            workspace=workspace_id,
            baseline_offset=int(baseline_offset or 0),
            reason=reason or SUBSCRIBE_REASON_ABSENT,
            replaced=bool(replaced),
            producer_restarted=generation_after != generation_before,
        )
        if replaced and log is not None:
            try:
                log(
                    {
                        "event": "serve_office_subscription_rebaselined",
                        "connection": str(connection_key or "stdio"),
                        "workspace_id": workspace_id,
                        "key": key,
                        "baseline_offset": int(baseline_offset or 0),
                        # WHY the client came back, in the client's own words —
                        # the one fact this line could never derive. Without it
                        # a re-baseline storm is a count with no class, and
                        # separating "the fold fenced" from "the batch demoted
                        # to a full core" meant joining this log to the
                        # launcher's on timestamps, which is the attribution
                        # failure shape that made the ladder unreadable in the
                        # first place. Verbatim: the handler validated the
                        # charset, and repairing it here would print a token
                        # neither side used.
                        "reason": reason or SUBSCRIBE_REASON_ABSENT,
                        # Named on the line rather than left to be inferred:
                        # this is the whole reason the line exists. A retry loop
                        # shows up here as a repeating cost, not as a mystery in
                        # the hub's generation counter.
                        #
                        # MEASURED since O-H5, not asserted. It was the constant
                        # ``True`` while a restart was unconditional; now a
                        # non-narrowing rejoin attaches to the running producer
                        # instead, and a receipt that kept saying True would be
                        # billing the room for a core nobody built. The
                        # generation counter is the hub's own answer, so this
                        # cannot drift from what happened.
                        "producer_restarted": generation_after != generation_before,
                    }
                )
            except Exception:
                # A logging sink is never allowed to fail a subscribe that has
                # already succeeded against the hub.
                pass
        return SubscribeOutcome(True, replaced=replaced)

    def _forget(self, connection_key: str | None, key: str) -> None:
        """Drop one key from the index only. Never touches the hub."""

        owner = str(connection_key or "stdio")
        with self._lock:
            # The declaration goes unconditionally, even when the index has no
            # such owner: a declaration outliving its subscription would narrow
            # (or widen) the room's intersection on behalf of a client that is
            # no longer in it.
            self._declared.pop(key, None)
            owned = self._owned.get(owner)
            if owned is None:
                return
            owned.discard(key)
            if not owned:
                self._owned.pop(owner, None)

    def release_one(self, connection_key: str | None, workspace_id: str) -> bool:
        """Give ONE workspace's subscription back, keeping the connection.

        ``release`` is the disconnect sweep and takes everything a departing
        connection owns. This is the opposite situation: the client is still
        here and still talking, it has simply stopped caring about one
        workspace — it navigated away, or it refused a baseline it could not
        use. Without this the only way to stop a push lane was to drop the
        socket carrying every other call too.

        The answer is the HUB's, not the index's. A key sitting in ``_owned``
        that the hub no longer holds is a bookkeeping error, and reporting it as
        a successful release would hide the one leak this whole registry exists
        to make visible. False therefore means "nothing was live here" — an
        answer, not a fault, because a client recovering from a half-finished
        subscribe releases what it is not sure it holds.
        """

        key = office_subscription_key(connection_key, workspace_id)
        # Pruned FIRST and unconditionally: whatever the hub says, this
        # connection is no longer claiming the key, and an index that outlived
        # the claim would have the disconnect sweep chase a phantom.
        self._forget(connection_key, key)
        with self._lock:
            factory = self._hub_factory
        hub = factory() if factory is not None else None
        if hub is None:
            return False
        try:
            return bool(hub.unsubscribe(key))
        except Exception:
            return False

    def declarations(self) -> list[frozenset[str]]:
        """What each LIVE office subscription declared it can fold.

        A list rather than a set, because the consumer (``serve.py``'s
        ``_accepted_fold_entities``) intersects a SEQUENCE of per-subscriber
        declarations, and an empty sequence is meaningfully different from one
        entry: no office subscriber at all must leave the room exactly as it
        was, contributing nothing to narrow OR to widen.

        PER-CLIENT since 2026-08-16. This returned the server-side constant for
        every subscription, which was the only answer available while nothing
        asked the client — and it is precisely why a widened launcher could never
        have its lifecycle rows promoted on this lane: the room's intersection
        could not contain a token the server never heard. A subscription that
        declared nothing still contributes :data:`OFFICE_FOLD_ENTITIES`, so a
        room of un-updated clients intersects to exactly today's set.
        """

        with self._lock:
            return [
                self._declared.get(key, OFFICE_FOLD_ENTITIES)
                for keys in self._owned.values()
                for key in keys
            ]

    def release(self, connection_key: str | None) -> int:
        """Drop every office subscription this connection owns. Returns a count.

        Called from ``serve.py``'s ``_release_subscription``, which already
        unsubscribes the STREAM lane's bare key. The office keys are namespaced
        away from that one, so neither release can take the other's
        subscription — and a connection that never subscribed here costs one
        dictionary lookup.
        """

        owner = str(connection_key or "stdio")
        with self._lock:
            keys = self._owned.pop(owner, set())
            for key in keys:
                self._declared.pop(key, None)
            factory = self._hub_factory
        if not keys:
            return 0
        hub = factory() if factory is not None else None
        released = 0
        for key in keys:
            if hub is None:
                continue
            try:
                if hub.unsubscribe(key):
                    released += 1
            except Exception:
                # Teardown never raises into a disconnect path: the client is
                # already gone and the hub's own accounting has the truth.
                pass
        return released

    def owned_keys(self, connection_key: str | None) -> set[str]:
        with self._lock:
            return set(self._owned.get(str(connection_key or "stdio"), set()))


#: The process's registry. One per runtime, like the method table beside it.
OFFICE_SUBSCRIPTIONS = OfficeSubscriptions()
