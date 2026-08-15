"""``hermes harness serve --ndjson`` — persistent stdio bridge (schema v1).

One warm process replaces the per-call CLI spawns the Launcher Mission
Control bridge pays ~3s import tax on today. Requests dispatch into the
EXISTING harness argparse tree and ``_cmd_*`` handlers, unchanged — argv
arrives verbatim as the bridge already builds it, so intent→argv mapping,
the capability registry, and the per-call CLI fallback stay byte-identical.

Design doc: ``docs/agent-runtime-harness/harness-serve-design.md``
(settled 2026-07-08). Explicit non-goals: no network listener, not the
mission daemon, no second chat pipeline. "No auth (a local stdio child IS
the security model)" held while the transport was an inherited pipe; the
durable runtime-root service replaces that pipe with one any local process
can reach, so a per-root token is now minted at boot (unwired — see
``agent_runtime/serve_auth.py``) rather than retrofitted after the socket
exists.

Protocol (NDJSON, one frame per line):

- boot:      ``{"event":"ready","pid":…,"schema_version":1,"runtime_root":…}``
             plus the durable-service foundations, all additive:
             ``"build"`` (which commit this runtime is on —
             ``agent_runtime/build_stamp.py``), ``"auth"``
             (``{"token_file":"present"|"minted"|"error:<reason>"}`` — the
             posture, NEVER the token itself), and ``"instance"`` (this
             serve's registry entry under ``<store_root>/serve_instances/``).
- request:   ``{"id":"req-7","argv":["harness","status","--json"]}``
- reply:     ``{"id":"req-7","event":"line","line":…}`` × N then
             ``{"id":"req-7","event":"exit","code":0}``
             (a status/snapshot poll replayed from the read-model cache adds
             ``"served_from_cache": true, "cache_age_ms": N`` to its exit
             frame — additive; see _ReadModelCache below)
- stderr:    ``{"id":<request id or null>,"event":"stderr","line":…}``
- ping:      ``{"op":"ping"}`` → ``{"event":"busy","chat_turns":N,"pending":M}``
             (the Launcher supervisor must NEVER recycle serve while
             ``chat_turns`` > 0 — recording safety)
- shutdown:  ``{"op":"shutdown"}`` → drain in-flight requests, exit 0
- version:   ``{"op":"version"}`` → ``{"event":"version","build":{…},
             "runtime_root":…,"boot_id":…,"transport":"stdio","auth":{…}}``
             — the SAME stamp the ready frame carried, re-askable at any
             time. A durable service outlives the install it was started
             from; this is how a client proves it is not talking to last
             week's code.
- drain:     ``{"op":"drain"[,"deadline_seconds":30][,"force":true]}`` → stop
             accepting new requests (each is answered
             ``{"id":…,"event":"draining",…}`` and a terminal ``exit`` frame
             with code 75), let in-flight requests finish, then
             ``{"event":"drain_complete","requests_refused":N,
             "requests_completed":M,"drain_ms":X}`` and exit 0. If the
             deadline elapses first: ``{"event":"drain_timeout",…,
             "stuck_request_ids":[…],"held_by_chat_turns":N,"terminal":true}``
             and a NONZERO exit — a drain that can hang forever is not a drain.
             Progress is reported as ``{"event":"drain_progress",…}`` while the
             wait runs, so a draining service never looks dead to a watchdog.

             THE DEADLINE IS THE SERVER'S, and so is the kill. Two rules make
             it so, and both exist because `drain` on the socket is reachable
             by any local process holding the root's secret while `shutdown` is
             refused there on purpose:

             * ON THE SOCKET the effective deadline is ``max(client ask,
               server minimum)`` — a socket client could previously ask for
               0.05s, which turned the restart verb into a kill
               (`hard_exit(3)` is `os._exit`) over a live chat turn, straight
               through the never-recycle-during-turns contract this file opens
               with. Over STDIO the ask still stands as given: that asker is
               the parent that spawned this process and owns its stdin, so it
               can end the runtime with a signal regardless, and flooring it
               would change a contract this lane promised to leave untouched;
             * a deadline that expires WHILE A CHAT TURN IS IN FLIGHT does not
               end the process. It emits a non-terminal
               ``{"event":"drain_timeout","terminal":false,
               "held_by_chat_turns":N,…}``, keeps serving, and re-arms. Only an
               expiry with no chat turn in flight is terminal. Recording safety
               outranks restart latency: a killed turn is lost work, a late
               restart is a slow one.

             On the SOCKET lane `drain` additionally requires ``"force":true``.
             Same reasoning as the `shutdown` refusal — an attached client
             asking to replace a service other clients are using should have to
             say so explicitly — and one flag is a trivial cost for the
             operator verb (`harness serve connect --drain` sets it). The
             refusal is typed: ``{"event":"error","error":"drain_requires_force"}``.
- cancel:    ``{"op":"cancel","id":"req-7"}`` → a QUEUED request is dropped
             and answers ``{"id":"req-7","event":"exit","code":130,
             "cancelled":true}``; a request already RUNNING (or unknown)
             answers ``{"id":…,"event":"cancel_denied","state":
             "running"|"unknown"}`` — its side effects may still happen, so
             mutation verbs carry their own replay guard (``--issued-at``).
             A RUNNING read-only ``harness stream`` is cooperatively cancelled
             and releases its pool worker; it is the sole running exception.
- errors:    ``{"id":…,"event":"error","error":"invalid_request"|…,"detail":…}``
- method:    ``{"jsonrpc":"2.0","id":…,"method":"runtime.office.get"|
             "runtime.office.upsert",
             "params":{…}}`` → ``{"jsonrpc":"2.0","id":…,"result":{…}}`` or
             ``{"jsonrpc":"2.0","id":…,"error":{"code":…,"message":…,"data":…}}``.

             The CALL half (``agent_runtime/serve_rpc.py``), mirroring
             ``tui_gateway``'s JSON-RPC 2.0 shape and its error codes rather
             than minting a third convention. It sits BESIDE the argv lane
             above, which is unchanged and remains the fallback: a frame is
             claimed by this lane only when it names ``jsonrpc`` or ``method``,
             neither of which an argv request has ever carried.

             HOW A CLIENT LEARNS THE SURFACE — the ``hello_contract``
             precedent, not a parallel scheme. ``{"contract":N,"methods":[…]}``
             rides the greeting each transport already reads (``ready`` on
             stdio, ``hello_ok`` on the socket) under ``"rpc"``, and is
             restated on the re-askable ``version`` reply because a durable
             service outlives the install it was started from. The manifest is
             a SET plus an integer: the integer moves when an existing
             method's shape changes incompatibly, the set grows when a method
             is added — so methods can be adopted one at a time, exactly as
             ``fold_entities`` does for patch entities. A runtime that
             predates the lane carries no ``rpc`` key, which reads as "argv
             only" rather than as a failure.

Per-request stdout: handlers ``print()`` directly and streaming turns emit
deltas live, so ``sys.stdout``/``sys.stderr`` are swapped once for
contextvar-dispatching proxies; each pool worker binds its request id and a
single write lock keeps frames atomic. Writes from threads a handler spawns
itself carry no request id and are forwarded with ``"id": null``.

The socket lane (slice 3)
-------------------------

``serve_loop`` is now transport-agnostic: ONE dispatcher answers ops arriving
on stdio and on a localhost socket alike. Everything above this line is
unchanged on stdio — every frame, reply, and exit code is byte-identical,
because the socket lane is injected and OFF unless ``_cmd_serve`` turns it on.

- ownership: one serve per root owns the socket, decided by an OS-held
  exclusive lock (``agent_runtime/serve_socket.py``). The loser runs
  stdio-only and says so on ``ready`` under ``"socket"``:
  ``{"outcome":"lock_held_by","pid":…}``. The winner's ``ready`` carries
  ``{"outcome":"listening","host":"127.0.0.1","port":…}`` and its registry
  entry records ``transport:"stdio+socket"`` plus the port.
- hello:     CHALLENGE-RESPONSE, and the SERVER speaks first
             (``hello_contract`` 2). On accept the service writes
             ``{"event":"server_hello","nonce":<64 hex>,"boot_id":…,
             "contract":1,"hello_contract":2,"algorithm":"hmac-sha256"}`` and
             the client answers
             ``{"op":"hello","client":…,"client_build":…,"proof":<hex>}`` where
             the proof is ``HMAC-SHA256(key=<per-root token>, msg=<nonce>)``.
             Success → ``{"event":"hello_ok","build":{…},"boot_id":…,
             "contract":1,"hello_contract":2,"build_mismatch":true|false|null}``;
             failure → ONE ``{"event":"hello_rejected","reason":…}`` and the
             connection is closed, with a rate limit against hammering. Before
             that proof is verified a connection can do NOTHING.

             THE TOKEN NEVER TRAVELS. It is the HMAC key, never a field, so it
             appears in no frame, log, error, or registry entry on either side,
             and a captured transcript is unreplayable (fresh nonce per
             connection). The first cut sent the raw token and paired that with
             a discovery fallback that could hand a client a target already
             classified ``stale_dead_pid`` — an impostor on a dead serve's port
             harvested the real token, live-proven. There is deliberately no
             compatibility shim for the old hello: it has no other clients yet,
             and a shim would keep the cleartext lane open forever.

             Rejection reasons are typed and mean different things:
             ``bad_proof`` / ``hello_required`` / ``hello_malformed`` are
             AUTHENTICATION failures and are the only ones that charge the rate
             limiter; ``too_many_connections`` / ``too_many_pending`` /
             ``draining`` / ``hello_timeout`` / ``rate_limited`` /
             ``handshake_throttled`` describe the SERVER's state and never do —
             charging them made a blocked window extend itself forever, so a
             client with the right credential could not recover.
- subscribe: ``{"op":"subscribe","lane":"stream"}`` pushes the SAME hydrate /
             delta / heartbeat frames ``harness stream`` produces, from ONE
             shared producer fanned out to every subscriber (a per-batch
             snapshot rebuild is why it is not one generator per client). A
             subscriber that outruns its bounded buffer gets
             ``{"event":"subscription_dropped","reason":"backpressure",…}`` and
             is unsubscribed — never silently stalled, and never able to wedge
             the producer or another subscriber. ``{"op":"unsubscribe"}`` ends
             it cleanly, and so does a disconnect.
             An optional ``"fold_entities":["persona_instance",…]`` declares
             which entity classes THIS client can fold in place; the producer
             promotes a coalesced batch to a small ``patch`` frame only for
             declared entities and demotes anything else to the full core it
             would have sent anyway. Omitting it means the historical
             ``{persona_instance, incident}`` — exactly today's wire, so an
             un-updated client is unaffected. The producer is SHARED, so the
             ACCEPTED set is the intersection over every attached subscriber and
             is echoed on the ``subscribed`` ack (and on the hydrate) rather than
             left for the client to assume. A malformed declaration is refused
             with ``{"event":"subscribe_denied","reason":"invalid_fold_entities"}``
             instead of being silently read as absent.
- connections: ``{"op":"connections"}`` → ``{"event":"socket_connections",…}``
             (count, and per client: name, build, subscribed, connected_at,
             frames and bytes pushed). The same block rides the ``version``
             reply, so "who is attached to this runtime" is answerable from the
             handshake a client already performs.

Client disconnect unsubscribes and does NOTHING else: the backend state a
client was watching is the runtime's, not the client's, and surviving a client
is the entire point of the durable service.
"""

from __future__ import annotations

import argparse
import contextvars
import inspect
import io
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TextIO

SERVE_SCHEMA_VERSION = 1
DEFAULT_POOL_SIZE = 4

# ── Drain ────────────────────────────────────────────────────────────────────
#
# A durable service must be replaceable WITHOUT killing work: `drain` refuses
# new requests, lets the in-flight ones land, and exits. Every path emits its
# typed terminal frame BEFORE exiting — the frame is the observability, and a
# drain that exited without one would be indistinguishable from the crash it
# exists to avoid.
DEFAULT_DRAIN_DEADLINE_SECONDS = 30.0
#: The absolute sanity floor, both transports: a deadline of zero is not a
#: deadline. This is the long-standing stdio contract and is unchanged.
_DRAIN_DEADLINE_FLOOR_SECONDS = 0.05
#: The SOCKET lane's floor under a client-supplied deadline, and the
#: correction for a real defect: with only the sanity floor above, any local
#: process holding the root's secret could ask for a deadline that expires
#: instantly, and `drain` became `kill` — the timeout path calls `hard_exit`,
#: which is `os._exit`, over whatever was running.
#:
#: Why the SOCKET lane only. A deadline is a promise about how long in-flight
#: work is allowed to finish, and the question is who is entitled to shorten
#: it. Over stdio the asker is the PARENT that spawned this process and owns
#: its stdin; it can end this runtime with a signal whether or not the drain
#: cooperates, so a floor there buys no safety and would silently rewrite a
#: contract the socket slice promised to leave byte-identical. Over the socket
#: the asker is any local process that could read the token file, refereeing
#: work it cannot see. `max(ask, this)` on that lane, always.
_DRAIN_SOCKET_MINIMUM_DEADLINE_SECONDS = 30.0
#: An unbounded value would restore "can hang forever" through the front door.
_DRAIN_DEADLINE_MAX_SECONDS = 3600.0
_DRAIN_POLL_INTERVAL_SECONDS = 0.05
#: While draining, this replaces the `busy` liveness pump (which stops with the
#: delivery drain the moment draining starts). A watchdog keyed on "no frames
#: for N seconds" must not declare a healthily-draining runtime dead.
_DRAIN_PROGRESS_INTERVAL_SECONDS = 5.0
#: Refused-because-draining. 75 is EX_TEMPFAIL: "try again", which is exactly
#: what a client should do — against the replacement runtime. The refusal also
#: carries this terminal `exit` frame so a client that predates the typed
#: `draining` event still terminates its request instead of waiting forever.
DRAINING_EXIT_CODE = 75
#: In-flight work outlived the deadline. Nonzero on purpose: a supervisor must
#: be able to tell "drained" from "gave up with work still running".
DRAIN_TIMEOUT_EXIT_CODE = 3
#: ONE deadline for everything between "the drain has decided how it ended"
#: and "this process is gone": publishing the terminal frame, broadcasting it
#: to attached clients, tearing the socket lane down, unregistering, and the
#: reader unwinding. It is armed as the FIRST act of ``_finish_drain`` rather
#: than after the teardown, because the teardown is exactly what can hang —
#: hub joins were 2.0s EACH and a wedged reader can park a broadcast write for
#: IO_TIMEOUT, so with 32 subscribers the old arrangement could sum past a
#: minute with the watchdog not yet armed. Summed per-step budgets are not a
#: bound; this is.
_DRAIN_EXIT_DEADLINE_SECONDS = 15.0
#: The mirror image: how long the READER waits for the drain monitor to publish
#: its terminal frame when the transport closed first (a `shutdown` op or EOF
#: arriving mid-drain). The pool has already been joined by then, so the monitor
#: is normally one poll interval away; past this bound the drain is declared
#: abandoned IN A FRAME rather than exiting silently.
_DRAIN_ABANDON_GRACE_SECONDS = 5.0

# Chat turns must survive supervisor recycles (recording safety): these argv
# shapes mark a request as an in-flight chat turn for the busy/ping frame.
_CHAT_TURN_COMMANDS = (("mission-chat", "message"), ("mission-chat", "steer"))

# ── Read-model cache (follow-up slice 1 of the serve design doc) ─────────────
#
# The Launcher polls `harness status --json` / `harness snapshot --json` on a
# fixed cadence; each build recomputes the full projection (~1.7s status /
# ~7s snapshot warm) even when NOTHING changed. Serve is a warm process, so it
# caches the exact stdout payload of these read-only requests keyed by a
# runtime-state fingerprint (the sequence check) and replays it while the
# fingerprint holds.
#
# The fingerprint stats the cheap change signals: events.jsonl (every store
# mutation appends an event — the architecture's change feed), the turn store,
# scope pointers, the live store directories (record add/rename
# flips a directory's mtime), and the SessionDB files (chat writes; -wal /
# -journal included because a SQLite WAL commit does not touch the main db's
# mtime). Signals that live OUTSIDE the runtime root (git working trees for
# dirty state, provider health) cannot flip the fingerprint, so a TTL bounds
# their staleness: a cached payload older than _READ_CACHE_MAX_AGE_SECONDS is
# rebuilt even on a fingerprint match.
#
# Visibility: a replayed response stamps `served_from_cache` + `cache_age_ms`
# on its exit frame (additive), and the payload's own parity envelope keeps
# the honest original `generated_at`.

_CACHEABLE_ARGV: dict[tuple[str, ...], str] = {
    ("harness", "status", "--json"): "status",
    ("harness", "snapshot", "--json"): "snapshot",
}
_READ_CACHE_MAX_AGE_SECONDS = 20.0

_FINGERPRINT_ROOT_FILES = (
    "events.jsonl",
    "mission_chat_turns.json",
    "active_realm.json",
    "active_workspace.json",
)
_FINGERPRINT_STORE_DIRS = (
    "runs",
    "incidents",
    "agents",
    # S57 dropped "repo_bundles" here with the store: this list exists to
    # invalidate the read cache when a store directory changes, and no code path
    # can write that tree any more (S52 took the last writer, S57 the module).
    # Stat'ing it every poll was cost against a directory that cannot move. Same
    # rule S56 applied to "worker_sessions".
    "runtime_instances",
    "persona_instances",
    "persona_assignments",
    "workspaces",
    "realms",
    # DELIBERATELY ABSENT: "serve_instances". Its entries appear and vanish at
    # every serve boot/exit, and the ``serve_auth_token`` file appears at first
    # boot — inside a fingerprint either one would cold the read-model cache
    # exactly when a fresh runtime is warming up, and make the stream emit
    # ``state.reconciled`` on every restart. Same standing precedent as
    # ``dispatch_delivery.DRAIN_STATE_FILENAME``; the rule is restated at both
    # ``agent_runtime/serve_registry.py`` and ``agent_runtime/serve_auth.py``.
)

# The ``running_work`` durable stores (``processes.json``, ``state.db``) are
# fingerprinted too, but they live under the HERMES **home** rather than the
# agent-runtime store root — on a profiled install those are genuinely
# different directories — so they cannot ride the two tuples above. Their one
# path authority is ``agent_runtime.running_work.running_work_store_paths``,
# called from ``_runtime_state_fingerprint`` below; duplicating the names here
# would stand up a second list free to drift from the projection's.


_FINGERPRINT_BOARD_CARD_CAP = 600  # bounded per-board card stat; remainder is rare + also evented


def _stat_board_tree(root: Any, _stat) -> None:
    """Bounded stat of the boards/ subtree: the root, each board's def + card
    files + conflict dir. Card files are stat'd individually so in-place edits
    (move/edit rewrite a file without touching the dir mtime) still flip the
    fingerprint. Capped per board to stay cheap on the hot poll path."""

    boards_root = root / "boards"
    _stat(boards_root)
    try:
        board_dirs = sorted(p for p in boards_root.iterdir() if p.is_dir())
    except OSError:
        return
    for board_dir in board_dirs:
        _stat(board_dir / "board.json")
        cards_dir = board_dir / "cards"
        _stat(cards_dir)
        _stat(board_dir / "conflicts")
        try:
            card_files = sorted(cards_dir.glob("*.json"))
        except OSError:
            continue
        for card_path in card_files[:_FINGERPRINT_BOARD_CARD_CAP]:
            _stat(card_path)


_FINGERPRINT_TURN_FILE_CAP = 200  # session cap is 50; defensive bound only


def _stat_turn_store_tree(root: Any, _stat) -> None:
    """Bounded stat of the per-session turn store (mission_chat_turns/<key>.json).

    The turn-store split (one file per chat session) made the legacy
    `mission_chat_turns.json` root-file stat a dead signal: after migration the
    monolith is renamed aside and every streamed-turn flush rewrites ONE
    session file in place — which does not reliably move the directory mtime.
    Stat each session file individually (the board-tree pattern) so a cached
    snapshot can never serve stale turn elements. The legacy root file stays in
    _FINGERPRINT_ROOT_FILES so the one-time migration rename also flips the
    fingerprint."""

    turns_root = root / "mission_chat_turns"
    _stat(turns_root)
    try:
        session_files = sorted(turns_root.glob("*.json"))
    except OSError:
        return
    for session_path in session_files[:_FINGERPRINT_TURN_FILE_CAP]:
        _stat(session_path)


def _runtime_state_fingerprint() -> tuple | None:
    """Cheap stat-based sequence check over the harness read-model inputs.

    Returns None when the runtime root cannot be resolved — callers must
    treat None as "never cache"."""
    try:
        from agent_runtime import paths as _paths

        root = _paths.store_root()
    except Exception:
        return None
    parts: list[tuple[str, int, int]] = []

    def _stat(path: Any) -> None:
        try:
            st = os.stat(path)
        except OSError:
            parts.append((str(path), -1, -1))
            return
        parts.append((str(path), st.st_mtime_ns, st.st_size))

    for name in _FINGERPRINT_ROOT_FILES:
        _stat(root / name)
    for name in _FINGERPRINT_STORE_DIRS:
        _stat(root / name)
    # Background-work stores hang off the HERMES home, not the store root, and
    # resolve through the same head authority their WRITERS use
    # (``get_hermes_background_work_home``): ``persona_profile_context`` flips
    # ambient HERMES_HOME process-globally while a persona turn runs in THIS
    # process, so an ambient read here would fingerprint whichever profile
    # happened to be mid-turn.
    #
    # An EMPTY tuple means the authority could not resolve a home — "I cannot
    # fingerprint these", not "there is nothing to watch". Both that case and a
    # raised exception get the same sentinel, because caching against a silently
    # missing signal is exactly how a stale HUD gets served.
    try:
        from agent_runtime.running_work import running_work_store_paths

        store_paths = running_work_store_paths()
        if not store_paths:
            parts.append(("running_work_stores", -1, -1))
        for path in store_paths:
            _stat(path)
    except Exception:
        parts.append(("running_work_stores", -1, -1))
    # Event-log rotation (C6a) moves appends off the static "events.jsonl" onto a
    # rotating live slice, so the _FINGERPRINT_ROOT_FILES entry above freezes once
    # the log rotates. Stat the manifest (flips on each rotation) AND the resolved
    # live slice (flips on every append) so a cached snapshot never serves stale
    # frames after rotation. Pre-rotation the live slice IS events.jsonl (a
    # harmless duplicate stat); the manifest is absent (a stable -1/-1 signal).
    try:
        from agent_runtime import event_rotation as _event_rotation

        _stat(_event_rotation.manifest_path())
        _stat(_event_rotation.live_path())
    except Exception:
        parts.append(("event_log_rotation", -1, -1))
    # Mission Board tree is nested two levels deep (boards/<id>/cards/<card>.json),
    # so a top-level dir stat alone misses card adds/moves/in-place edits and
    # pull-materialized cards. Every board mutation also advances events.jsonl
    # (already fingerprinted), but a bounded subtree walk here keeps cached
    # snapshots honest even for event-less file materialization (realm pull).
    _stat_board_tree(root, _stat)
    # Per-session turn store: streamed-turn flushes rewrite one session file in
    # place and emit NO EventLog event, so without these stats a cached snapshot
    # would serve stale turn elements.
    _stat_turn_store_tree(root, _stat)
    try:
        # Fingerprint the database the CHAT LANE actually writes, not the one
        # ambient HERMES_HOME resolution happens to hand this process. A bare
        # ``SessionDB()`` keyed the cache on ``HERMES_HOME/state.db`` while every
        # chat write goes to the resolved chat scope; whenever the two diverge a
        # cached snapshot could serve a frozen Chat History for the life of the
        # serve process (defect D1 in
        # ``docs/agent-runtime-harness/chat-session-presence-authority.md``,
        # the serve twin of the stream-lane fix 639242901). Resolving the PATH
        # also stops the poll loop from opening — and potentially creating — a
        # database just to read its own filename.
        from agent_runtime.chat_session_scope import chat_session_db_path

        db_path = str(chat_session_db_path())
        for suffix in ("", "-wal", "-journal"):
            _stat(db_path + suffix)
    except Exception:
        # Chat persistence unavailable → its absence is itself stable.
        parts.append(("session_db", -1, -1))
    return tuple(parts)


class _ReadModelCacheEntry:
    __slots__ = ("fingerprint", "lines", "code", "built_monotonic")

    def __init__(
        self, fingerprint: tuple, lines: list[str], code: int, built_monotonic: float
    ):
        self.fingerprint = fingerprint
        self.lines = lines
        self.code = code
        self.built_monotonic = built_monotonic


class _ReadModelCache:
    """Per-serve-loop response cache for the read-only poll commands."""

    def __init__(self, max_age_seconds: float = _READ_CACHE_MAX_AGE_SECONDS):
        self._entries: dict[str, _ReadModelCacheEntry] = {}
        self._lock = threading.Lock()
        self._max_age = max_age_seconds

    def get(
        self, key: str, fingerprint: tuple | None, now_monotonic: float
    ) -> _ReadModelCacheEntry | None:
        if fingerprint is None:
            return None
        with self._lock:
            entry = self._entries.get(key)
        if entry is None or entry.fingerprint != fingerprint:
            return None
        if now_monotonic - entry.built_monotonic > self._max_age:
            return None
        return entry

    def put(
        self,
        key: str,
        fingerprint: tuple | None,
        lines: list[str],
        code: int,
        now_monotonic: float,
    ) -> None:
        # Only successful builds are worth replaying; a failed build must
        # re-run so the error stays live, not fossilized.
        if fingerprint is None or code != 0:
            return
        with self._lock:
            self._entries[key] = _ReadModelCacheEntry(
                fingerprint, lines, code, now_monotonic
            )

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_serve_request_id", default=None
)

#: WHERE this request's frames go. Bound by ``_run`` alongside the request id,
#: for the same span and in the same pool-worker context.
#:
#: A durable service answers more than one transport, and a handler's ``print``
#: belongs to the client that asked — not to whoever owns stdout. Unset (the
#: default) means stdout, which is every stdio request and every thread a
#: handler spawns for itself, so the stdio lane is byte-identical to the
#: pre-socket loop: same proxy, same frames, same order.
_request_sink: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "harness_serve_request_sink", default=None
)


def current_serve_request_id() -> str | None:
    """The serve frame-protocol request id bound to THIS context, or None.

    ``_run`` binds it for exactly the span of one serve-dispatched request, in
    the pool-worker context that dispatches to the command handler — so a
    non-None answer is DIRECT provenance that the current work arrived as a
    serve frame request. Every other lane reads None: a one-shot CLI turn, the
    delivery drain's forged turns, background threads.

    This is the honest "did this turn arrive via serve" fact, and the only one.
    Two proxies have already been retired for impersonating it (both live
    2026-08-09 findings): ``persona_chat_runtime_registry() is not None`` is
    really "the hot-sessions CACHE is enabled" (default off, so every live
    serve read False), and ``delivery_drain_is_live()`` is really "a delivery
    consumer exists" — a serve whose drain died is still a serve. Provenance
    questions read THIS; capability questions read the drain.
    """

    return _request_id.get()


class _FrameWriter:
    """Sole owner of the real stdout; one lock keeps frames atomic."""

    def __init__(self, stream: TextIO):
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, frame: dict[str, Any]) -> None:
        payload = json.dumps(frame, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(payload + "\n")
            self._stream.flush()


class _LineFrameProxy(io.TextIOBase):
    """Stand-in for sys.stdout/sys.stderr that re-emits handler output as
    tagged line frames, buffered per request id until a newline."""

    def __init__(self, frames: _FrameWriter, event: str):
        super().__init__()
        self._frames = frames
        self._event = event
        self._buffers: dict[tuple[int | None, str | None], str] = {}
        self._captures: dict[tuple[int | None, str | None], list[str]] = {}
        self._lock = threading.Lock()

    def writable(self) -> bool:  # pragma: no cover - io protocol
        return True

    def isatty(self) -> bool:
        # Handlers key default output on isatty(); serve is a pipe.
        return False

    @staticmethod
    def _slot(rid: str | None) -> tuple[int | None, str | None]:
        """The partial-line buffer this write belongs to.

        Keyed by (destination, request id), not by request id alone. Request
        ids are chosen by CLIENTS, so once more than one transport is attached
        two connections may legitimately both be running ``req-1`` — and a
        buffer keyed on the id alone would splice one client's half-written
        line into the other's. Stdio's destination is None (stdout), which is
        what every pre-socket request already was.
        """

        sink = _request_sink.get()
        return (id(sink) if sink is not None else None, rid)

    def write(self, text: str) -> int:
        if not text:
            return 0
        rid = _request_id.get()
        slot = self._slot(rid)
        with self._lock:
            buffered = self._buffers.get(slot, "") + str(text)
            *lines, remainder = buffered.split("\n")
            self._buffers[slot] = remainder
            capture = self._captures.get(slot)
            if capture is not None:
                capture.extend(lines)
        sink = _request_sink.get() or self._frames
        for line in lines:
            sink.emit({"id": rid, "event": self._event, "line": line})
        return len(text)

    def flush(self) -> None:  # pragma: no cover - io protocol
        return None

    def begin_capture(self, rid: str | None) -> None:
        """Start mirroring [rid]'s emitted lines for the read-model cache."""
        with self._lock:
            self._captures[self._slot(rid)] = []

    def end_capture(self, rid: str | None) -> list[str]:
        """Stop mirroring and return everything captured for [rid]."""
        with self._lock:
            return self._captures.pop(self._slot(rid), [])

    def flush_request(self, rid: str | None) -> None:
        """Emit a request's unterminated tail (handler printed without a
        trailing newline) and drop its buffer."""
        slot = self._slot(rid)
        with self._lock:
            remainder = self._buffers.pop(slot, "")
            if remainder:
                capture = self._captures.get(slot)
                if capture is not None:
                    capture.append(remainder)
        if remainder:
            sink = _request_sink.get() or self._frames
            sink.emit({"id": rid, "event": self._event, "line": remainder})


class _SafeSink:
    """A frame sink that never raises — the socket lane's request path.

    A pool worker's ``finally`` MUST emit its terminal ``exit`` frame and clean
    up its inflight entry; a client that hung up mid-request would otherwise
    take that bookkeeping down with it, leaking the request id forever and
    stalling any drain waiting on it. Stdout is deliberately NOT wrapped: the
    stdio pipe failing is the process losing its transport, and that has always
    propagated.
    """

    __slots__ = ("_target", "write_failures")

    def __init__(self, target: Any):
        self._target = target
        self.write_failures = 0

    def emit(self, frame: dict[str, Any]) -> None:
        try:
            self._target.emit(frame)
        except Exception:
            self.write_failures += 1


class _ArgvRequest:
    __slots__ = (
        "rid",
        "argv",
        "is_chat_turn",
        "is_runtime_stream",
        "cancel_event",
        "key",
        "owner",
        "sink",
    )

    def __init__(
        self,
        rid: str,
        argv: list[str],
        *,
        owner: str = "stdio",
        sink: Any = None,
    ):
        self.rid = rid
        self.argv = argv
        tail = argv[1:] if argv and argv[0] == "harness" else argv
        self.is_chat_turn = any(
            tuple(tail[: len(shape)]) == shape for shape in _CHAT_TURN_COMMANDS
        )
        self.is_runtime_stream = bool(tail and tail[0] == "stream")
        self.cancel_event = threading.Event()
        #: Which connection asked. ``stdio`` for the inherited pipe.
        self.owner = owner
        #: The inflight-table key. Request ids are chosen by CLIENTS, so two
        #: connections may legitimately both use ``req-1``; the table is keyed
        #: per owner so neither can collide with — or cancel — the other's work.
        #: Stdio keeps the bare id, so its frames and its drain reports are
        #: byte-identical to the single-transport loop.
        self.key = rid if owner == "stdio" else f"{owner}:{rid}"
        #: Where this request's frames go. None means stdout.
        self.sink = sink


class _DrainState:
    """One drain in progress, and everything its terminal frame must account for.

    The counters are the point. A `drain_complete` that only said "done" would
    be a frame with the right NAME and no evidence — it could not distinguish a
    drain that let three turns land from one that refused them all, which is
    the difference between a safe restart and lost work.
    """

    __slots__ = (
        "started_monotonic",
        "deadline_seconds",
        "refused",
        "completed",
        "deadline_holds",
        "lock",
    )

    def __init__(self, deadline_seconds: float):
        self.started_monotonic = time.monotonic()
        self.deadline_seconds = deadline_seconds
        self.refused = 0
        self.completed = 0
        #: How many times the deadline expired and was NOT allowed to end the
        #: process because a chat turn was still in flight. Counted because
        #: "this restart is taking a while" and "this restart has been held
        #: open by recording safety four times" are different operator facts.
        self.deadline_holds = 0
        self.lock = threading.Lock()

    def note_refused(self) -> int:
        with self.lock:
            self.refused += 1
            return self.refused

    def note_completed(self) -> None:
        with self.lock:
            self.completed += 1

    def note_deadline_held(self) -> int:
        with self.lock:
            self.deadline_holds += 1
            return self.deadline_holds

    def counters(self) -> dict[str, Any]:
        with self.lock:
            return {
                "requests_refused": self.refused,
                "requests_completed": self.completed,
                "deadline_holds": self.deadline_holds,
            }

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)


def _drain_deadline_seconds(
    raw: Any, default: float, *, minimum: float = _DRAIN_DEADLINE_FLOOR_SECONDS
) -> float:
    """The EFFECTIVE deadline: the client's ask, floored by the server's.

    A client may lengthen a drain (up to the hard ceiling) and may not shorten
    it below the floor the caller passes for its TRANSPORT: the sanity floor on
    stdio (unchanged — that asker owns the process), the socket minimum on the
    socket lane. The floor is a parameter rather than a constant read in here
    precisely so the two lanes can differ and so the loop's own tests can run a
    drain in milliseconds; it is a SERVER-side parameter either way, and no
    field a client sends can lower it.
    """

    floor = max(0.0, float(minimum))
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return max(floor, float(default))
    return max(floor, min(float(raw), _DRAIN_DEADLINE_MAX_SECONDS))


def _build_harness_parser() -> argparse.ArgumentParser:
    """A fresh top-level parser holding only the harness tree. Built per
    request: cheap next to any handler, and avoids sharing one parser
    across pool threads."""
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_parser(subparsers)
    return parser


def dispatch_argv(argv: list[str]) -> int:
    """Parse and run one request exactly as ``hermes <argv…>`` would,
    including the harness error-envelope contract."""
    from hermes_cli.harness import emit_harness_error

    parser = _build_harness_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.parse_args([*argv, "--help"])  # exits 0 after printing help
        return 0
    try:
        code = func(args)
    except SystemExit:
        raise
    except BaseException as exc:  # mirror hermes_cli.main harness dispatch
        return emit_harness_error(exc, args=args)
    return code if isinstance(code, int) else 0


def _prewarm_read_model_snapshot() -> None:
    """Build ONE read-model core in the background right after ``ready``.

    A fresh serve child's first snapshot build costs ~7.5s against ~2.2s warm
    (measured 2026-08-09, B4/B11): ~5s of that is per-process cache fill —
    YAML parse cache, tool-visibility memos, skill resolution, the event tail.
    Serve is long-lived, so paying it on a daemon thread the moment the child
    is ready takes it off whichever request would otherwise have been first.

    Read-only by construction: ``build_snapshot`` projects, it does not write
    (``write_snapshot`` is the writer, and is not called here). Concurrency is
    handled by the builder's own coalescing — a real request arriving mid-build
    joins it (hydrate) or waits and shares the next one; it never double-builds.
    Best effort by contract: a failure here surfaces on the first real request
    exactly as it would have without the prewarm.
    """

    try:
        from agent_runtime.snapshot import build_snapshot

        build_snapshot()
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "serve snapshot prewarm did not complete", exc_info=True
        )


def _prewarm_provider_runtime() -> None:
    """Best-effort warmup of the per-process one-time costs a chat turn pays.

    Runs on a daemon thread right after the ready frame. Each step is
    independent and failure-isolated: a broken CA bundle or missing provider
    dependency surfaces on the first real turn with its normal typed error,
    exactly as it would without prewarm.
    """
    try:
        from agent.process_bootstrap import _load_openai_cls, shared_ssl_context

        _load_openai_cls()
        shared_ssl_context()
    except Exception:
        pass
    try:
        from agent.ssl_guard import verify_ca_bundle

        verify_ca_bundle()
    except Exception:
        pass
    try:
        from model_tools import get_tool_definitions

        # The exact cache key varies per persona toolset; this call warms the
        # shared parts (tool module imports, registry build, config parse).
        get_tool_definitions(quiet_mode=True)
    except Exception:
        pass


def serve_loop(
    reader: TextIO,
    writer: TextIO,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    dispatch: Callable[[list[str]], int] = dispatch_argv,
    fingerprint: Callable[[], tuple | None] = _runtime_state_fingerprint,
    read_cache_max_age: float = _READ_CACHE_MAX_AGE_SECONDS,
    liveness_pump_interval_seconds: float = 5.0,
    boot_timeline: Any = None,
    snapshot_prewarm: Callable[[], None] | None = None,
    root_anchor: Callable[[], Any] | None = None,
    drain_deadline_seconds: float = DEFAULT_DRAIN_DEADLINE_SECONDS,
    drain_socket_minimum_deadline_seconds: float = (
        _DRAIN_SOCKET_MINIMUM_DEADLINE_SECONDS
    ),
    drain_poll_interval_seconds: float = _DRAIN_POLL_INTERVAL_SECONDS,
    drain_wakeup: Callable[[], None] | None = None,
    hard_exit: Callable[[int], None] | None = None,
    socket_lane: bool = False,
    stream_source_factory: Callable[[], Any] | None = None,
    stream_buffer_limit: int | None = None,
    stream_byte_limit: int | None = None,
) -> int:
    """Core dispatch loop over explicit streams. stdio is transport #1; the
    localhost socket is transport #2, and both feed THIS dispatcher.

    ``socket_lane`` is injected and OFF by default — the same contract as
    ``root_anchor`` and ``hard_exit`` — so every pre-socket test observes the
    byte-identical stdio loop, and a caller that wants the durable service says
    so explicitly. When it is on, this loop races for the per-root socket lock,
    binds an ephemeral loopback port before ``ready`` (so the ready frame and
    the registry entry can both carry it), and starts accepting only after the
    request pool exists.

    ``stream_source_factory`` is the shared subscription producer, likewise
    injectable: the default builds the real ``agent_runtime.stream``
    generator, and a test hands over a finite fake so a subscription test costs
    milliseconds instead of a projection build.

    ``drain_wakeup`` and ``hard_exit`` are the two process-level levers the
    drain needs and a unit test must not be given: the first unblocks a reader
    parked on an idle pipe once the drain has finished (the real entry point
    closes the protocol descriptor), the second takes the process down when
    in-flight work outlived the deadline. The timeout case CANNOT be a plain
    return: ``concurrent.futures`` registers an atexit hook that JOINS every
    worker thread, so an interpreter carrying a stuck worker hangs on the way
    out — which is the same "forever" the deadline exists to bound. Both are
    injected and OFF by default, the same contract as ``snapshot_prewarm`` and
    ``root_anchor``, so ``serve_loop``'s own tests observe the frames and the
    return code without ever exiting the test process.

    ``boot_timeline`` is the caller's already-running :class:`BootTimeline`
    (``_cmd_serve`` starts one at the process's first hermes instruction, so
    ``interpreter_ms`` covers the import tax); the loop starts its own when a
    caller supplies none. ``snapshot_prewarm`` is the post-``ready`` warmup
    policy — injected, and OFF unless the real entry point turns it on, so the
    loop's own unit tests never fire a multi-second projection build.
    """

    from agent_runtime.boot_timeline import BootTimeline

    timeline = boot_timeline if boot_timeline is not None else BootTimeline()
    frames = _FrameWriter(writer)
    # Emitted before ANY heavy boot work (the agent_runtime import, root
    # config load, registry init, and the pre-ready orphan sweep below): a
    # supervising launcher can tell a live cold boot from a wedged child by
    # this frame alone. A cold-cache boot can run past any short watchdog
    # before ``ready``; killing it mid-boot respawns into another cold boot
    # forever (2026-07-26 launcher kill-loop incident). Consumers that
    # predate this frame ignore unknown events, so it is purely additive.
    #
    # ``boot``: the self-attributing cold-boot stamp (T9). A >25s cold boot has
    # been recorded and is not reproducible on demand (the OS file cache is
    # warm on any machine that just ran the launcher), so the boot measures
    # itself instead: ``interpreter_ms`` here is the interpreter + hermes CLI
    # import tax the supervisor can see NO other way, and the ``ready`` frame
    # below carries the per-phase breakdown of everything after it.
    frames.emit(
        {
            "event": "booting",
            "pid": os.getpid(),
            "schema_version": SERVE_SCHEMA_VERSION,
            "boot": timeline.stamps(),
        }
    )
    # The METHOD lane's registry + its manifest. Imported here rather than at
    # module scope for the same reason as everything else in this function —
    # nothing agent_runtime-shaped is paid for before ``booting`` is out — and
    # it is cheap: ``serve_rpc`` imports only stdlib, and each method reaches
    # for its stores function-locally when it is actually called.
    from agent_runtime import serve_rpc

    from agent_runtime.persona_chat_continuity import (
        initialize_persona_chat_runtime_registry,
    )
    from agent_runtime.config import load_root_runtime_config

    persona_chat_cfg = load_root_runtime_config().persona_chat
    initialize_persona_chat_runtime_registry(
        enabled=persona_chat_cfg.hot_sessions_enabled,
        max_entries=persona_chat_cfg.max_hot_sessions,
        ttl_seconds=persona_chat_cfg.idle_ttl_seconds,
    )
    timeline.mark("chat_registry_ms")
    # Publish this process's EXPLICIT chat head home into the shared runtime
    # store root — the ONE writer of that pointer. The Launcher always starts
    # serve with HERMES_HEAD_HOME; a plain CLI turn started later names no head
    # and, without the pointer, degrades to its own profile database, minting
    # the transcript where the cockpit never looks while writing the binding
    # into the shared store (the 2026-07-27 read-lane gap). No-op when this
    # process named no head of its own, and best effort by contract.
    from agent_runtime.chat_session_scope import publish_chat_head_home

    publish_chat_head_home()
    timeline.mark("head_publish_ms")
    # Publish the machine root anchor: `agent_runtime.store_root` into the
    # PLATFORM DEFAULT home's config.yaml, so a later ambient process (no
    # HERMES_HOME, no HERMES_AGENT_RUNTIME_ROOT) resolves this serve's real
    # runtime root — and therefore finds the chat-head pointer above — instead
    # of the %LOCALAPPDATA% shadow runtime (the 2026-08-12 ambient
    # chat-history incident: `ok: true, count: 0` from the wrong root).
    # Injected and OFF unless the real entry point turns it on — the same
    # contract as ``snapshot_prewarm`` — so the loop's unit tests can never
    # write the machine-global config. Best effort by contract, but ACCOUNTED:
    # the typed outcome is emitted as its own frame either way, because a
    # silent skip here is exactly the false-all-clear class the anchor
    # retires. Consumers that predate this frame ignore unknown events.
    #
    # Since 2026-08-13 the same call also DECLARES `agent_runtime.head_home`
    # when this serve was started with an explicit head, and the frame carries
    # that outcome additively under `head`. That is the runtime declaring its
    # own identity: the Launcher's `HERMES_HEAD_HOME` pin demotes from sole
    # authority to an override plus a consistency check, and the launcher
    # compares its pin against this frame (a disagreement is a durable
    # `root_declaration_mismatch` transport receipt, never a silent divergence).
    if root_anchor is not None:
        try:
            anchor_report = root_anchor()
            anchor_frame = {"event": "root_anchor", **anchor_report.payload()}
        except Exception as exc:  # must never take the boot down
            anchor_frame = {
                "event": "root_anchor",
                "outcome": "unwritable",
                "detail": type(exc).__name__,
            }
        frames.emit(anchor_frame)
    timeline.mark("root_anchor_ms")
    stdout_proxy = _LineFrameProxy(frames, "line")
    stderr_proxy = _LineFrameProxy(frames, "stderr")
    read_cache = _ReadModelCache(read_cache_max_age)
    from agent_runtime.snapshot import SnapshotBuildContext

    read_build_context = SnapshotBuildContext()

    inflight: dict[str, _ArgvRequest] = {}
    # Futures by request id so ``{"op":"cancel"}`` can drop work that is
    # still queued behind the pool. A running request is uninterruptible —
    # cancel() then returns False and the client is told the side effect may
    # still land.
    inflight_futures: dict[str, Future] = {}
    inflight_lock = threading.Lock()

    # Drain state. ``None`` until a `drain` op arrives; from then on it is the
    # single answer to "are we still accepting work", read by the request path
    # and written once by the op.
    drain_state: _DrainState | None = None
    drain_exit_code = 0
    drain_finished = threading.Event()
    #: Latched the instant a drain DECIDES how it ended, before it publishes
    #: anything. A drain has exactly one terminal frame: without this latch a
    #: mid-drain EOF could publish ``drain_abandoned`` after a completed drain
    #: had already published ``drain_complete``, telling a supervisor that a
    #: successful restart gave up — and exiting 3 on it.
    drain_terminal_published = threading.Event()
    drain_terminal_lock = threading.Lock()
    reader_unwound = threading.Event()
    pool_shutdown_wait = True
    boot_id = uuid.uuid4().hex

    # ── socket lane state (all None unless ``socket_lane`` is on AND this
    # serve wins the per-root ownership lock) ────────────────────────────────
    socket_server: Any = None
    socket_lock: Any = None
    #: The ONE stream producer, built on the first ``subscribe`` and stopped
    #: when the last subscriber leaves. Never per client: a delta batch rebuilds
    #: a full snapshot core, so N generators would cost N of them.
    stream_hub: Any = None
    #: ONE lock over all three lane handles above. They used to be swapped by
    #: bare ``nonlocal`` assignment from the drain path, the shutdown path, and
    #: the EOF path — three threads racing an unsynchronised read-modify-write
    #: on the objects whose whole job is to be released exactly once. Held for
    #: the SWAP only, never across a join: the point is that two closers cannot
    #: both take the same handle, not that teardown is serialised.
    lane_lock = threading.Lock()
    #: Per-subscriber patch-fold declarations: connection key → the entity
    #: classes that client said it can fold, or None when it said nothing (which
    #: is NOT the empty set — see ``patch_coverage.HISTORICAL_FOLD_ENTITIES``).
    #: Guarded by ``lane_lock`` because the producer thread reads it while a
    #: request thread is writing it. The producer is SHARED, so what it may
    #: promote is the INTERSECTION over this table, not any one client's answer.
    stream_fold_entities: dict[str, Any] = {}

    def _busy_frame() -> dict[str, Any]:
        with inflight_lock:
            pending = len(inflight)
            chat_turns = sum(1 for item in inflight.values() if item.is_chat_turn)
        return {"event": "busy", "chat_turns": chat_turns, "pending": pending}

    def _service_log(payload: dict[str, Any]) -> None:
        """One structured line per transport event, on the serve's own stderr.

        Which means it arrives at the supervisor as an ordinary
        ``{"id":null,"event":"stderr","line":…}`` frame — the lane serve already
        uses for everything a handler writes to stderr. No new frame type, no
        new sink, and correlatable by ``boot_id`` against the ready frame.
        """

        try:
            sys.stderr.write(
                json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            )
        except Exception:
            pass

    def _run(request: _ArgvRequest) -> None:
        from agent_runtime.request_control import request_cancel_scope

        token = _request_id.set(request.rid)
        # Answers go back to whoever asked. ``request.sink`` is None on stdio,
        # which leaves the contextvar unset and the proxy on stdout — the
        # pre-socket path, unchanged.
        sink_token = _request_sink.set(request.sink)
        sink: Any = request.sink if request.sink is not None else frames
        code = 1
        cache_key = _CACHEABLE_ARGV.get(tuple(request.argv))
        request_fingerprint: tuple | None = None
        served_from_cache = False
        cache_age_ms = 0
        capturing = False
        try:
            cached = None
            if cache_key is not None:
                request_fingerprint = fingerprint()
                cached = read_cache.get(
                    cache_key, request_fingerprint, time.monotonic()
                )
            if cached is not None:
                served_from_cache = True
                cache_age_ms = int(
                    (time.monotonic() - cached.built_monotonic) * 1000
                )
                code = cached.code
                for line in cached.lines:
                    sink.emit({"id": request.rid, "event": "line", "line": line})
                return
            if cache_key is not None and request_fingerprint is not None:
                stdout_proxy.begin_capture(request.rid)
                capturing = True
            try:
                with request_cancel_scope(request.cancel_event):
                    if cache_key is not None:
                        from agent_runtime.snapshot import snapshot_build_context_scope

                        with snapshot_build_context_scope(read_build_context):
                            code = dispatch(list(request.argv))
                    else:
                        code = dispatch(list(request.argv))
            except SystemExit as exc:  # argparse usage errors land here
                raw = exc.code
                code = raw if isinstance(raw, int) else (0 if raw is None else 2)
                if code != 0:
                    sink.emit(
                        {
                            "id": request.rid,
                            "event": "error",
                            "error": "argv_parse_failed",
                            "detail": "argparse rejected the request argv; usage was forwarded as stderr frames",
                        }
                    )
            except BaseException as exc:  # dispatch() already enveloped harness errors
                sink.emit(
                    {
                        "id": request.rid,
                        "event": "error",
                        "error": "dispatch_failed",
                        "detail": f"{type(exc).__name__}",
                    }
                )
        finally:
            stdout_proxy.flush_request(request.rid)
            stderr_proxy.flush_request(request.rid)
            if capturing:
                read_cache.put(
                    cache_key,
                    request_fingerprint,
                    stdout_proxy.end_capture(request.rid),
                    code,
                    time.monotonic(),
                )
            _request_id.reset(token)
            _request_sink.reset(sink_token)
            with inflight_lock:
                inflight.pop(request.key, None)
                inflight_futures.pop(request.key, None)
                # Accounted here rather than by the monitor's before/after
                # arithmetic: the monitor only ever sees the pending SET, so a
                # request that both started and finished during the drain would
                # be invisible to it.
                #
                # And accounted INSIDE the same critical section as the pop,
                # which it did not used to be. With the increment outside, a
                # request sat in a window where it was gone from ``inflight``
                # and not yet in ``completed`` — the monitor could observe an
                # empty pending set and publish ``drain_complete`` with a
                # completion count LOWER than the number of exits it had
                # actually let land (reproduced: 5 reported for 8 exits). The
                # counters are the drain's only evidence, so an under-count
                # reads to an operator as work the restart dropped.
                #
                # Lock order is inflight_lock → _DrainState.lock, and it is the
                # only nesting of the two: every other site takes them one after
                # the other, never one inside the other.
                if drain_state is not None:
                    drain_state.note_completed()
            exit_frame: dict[str, Any] = {
                "id": request.rid,
                "event": "exit",
                "code": code,
            }
            if served_from_cache:
                exit_frame["served_from_cache"] = True
                exit_frame["cache_age_ms"] = cache_age_ms
            sink.emit(exit_frame)

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_proxy, stderr_proxy
    try:
        store_root_path: Any = None
        try:
            from agent_runtime import paths as _paths

            store_root_path = _paths.store_root()
            runtime_root = str(store_root_path)
        except Exception:
            store_root_path = None
            runtime_root = None
        timeline.mark("store_root_ms")
        # ── Durable-service foundations (slice 2) ───────────────────────────
        #
        # These three run BEFORE ``ready`` because ``ready`` is the frame that
        # carries them: a client that has to ask a second question to learn
        # what code it just connected to has a window in which it does not
        # know, and windows like that are how a stale service serves a whole
        # session before anyone notices.
        #
        # 1. WHICH CODE. Today serve is a per-client child, so a launcher
        #    restart picks up landed fixes for free and nobody ever had to
        #    ask. A durable service silently pins last week's code instead —
        #    the shape of the dispatch dead-flag-proxy incident, which ran
        #    green for a week. Resolved once per process and cached.
        try:
            from agent_runtime.build_stamp import build_stamp

            build_block = build_stamp().frame_payload()
        except Exception as exc:  # an instrument must never take the boot down
            build_block = {
                "commit": None,
                "dirty": None,
                "source": "unknown",
                "resolved_at": None,
                "reason": f"stamp_failed:{type(exc).__name__}",
            }
        # 2. THE SECRET. Unwired to any transport (stdio needs none), minted
        #    now so the socket slice starts with a lock already on the door
        #    rather than shipping open. The frame carries the POSTURE only —
        #    the token value must never appear in a frame, a log, or an event.
        auth_block: dict[str, Any] = {"token_file": "error:root_unresolved"}
        if store_root_path is not None:
            try:
                from agent_runtime.serve_auth import ensure_token

                auth_block = ensure_token(store_root_path).payload()
            except Exception as exc:
                auth_block = {"token_file": f"error:{type(exc).__name__}"}
        # 3. THE TRANSPORT (slice 3). One serve per root owns the socket lane,
        #    decided by an OS-held exclusive lock rather than by who booted
        #    first: two serves against one root is a real, ordinary concurrency
        #    (a launcher restart overlaps its replacement), and "connect to the
        #    service for root X" must have exactly one answer. The loser keeps
        #    serving stdio and SAYS so on the ready frame — a socket that
        #    silently never came up is indistinguishable from one that is
        #    broken.
        #
        #    Bound here, BEFORE the registry entry and the ready frame, so both
        #    can carry the real port; accepting starts later, once the request
        #    pool exists (see ``start_accepting`` below). A client that connects
        #    in between waits in the listen backlog, which is what a backlog is
        #    for.
        socket_block: dict[str, Any] = {"outcome": "disabled"}
        socket_transport = "stdio"
        if socket_lane and store_root_path is not None:
            try:
                from agent_runtime.serve_socket import (
                    SOCKET_HOST,
                    ServeSocketServer,
                    SocketOwnerLock,
                )
                from agent_runtime.serve_auth import read_token as _read_serve_token

                socket_lock = SocketOwnerLock(store_root_path)
                lock_result = socket_lock.acquire()
                if lock_result.acquired:
                    socket_server = ServeSocketServer(
                        store_root_path,
                        boot_id=boot_id,
                        # Late-bound on purpose: these two closures are defined
                        # further down (they need the pool and the drain state),
                        # and a Python closure resolves its enclosing names at
                        # CALL time — which cannot happen before the accept loop
                        # starts, which is after both exist.
                        dispatch_line=lambda line, connection: _handle_socket_line(
                            line, connection
                        ),
                        hello_payload=lambda message, connection: _hello_ok_frame(
                            message, connection
                        ),
                        # THIS root's secret, read per handshake and used as an
                        # HMAC key over a per-connection nonce. It is the key
                        # and never the message, so nothing derived from it and
                        # put on the wire discloses it — which is the whole
                        # reason the hello stopped carrying the token at all.
                        token_provider=lambda: _read_serve_token(store_root_path),
                        frame_contract=SERVE_SCHEMA_VERSION,
                        on_disconnect=lambda connection: _release_subscription(
                            connection
                        ),
                        log=_service_log,
                    )
                    port = socket_server.bind()
                    socket_lock.publish_owner(
                        {
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            "host": SOCKET_HOST,
                            "port": port,
                            "started_at": socket_server.started_at,
                            "store_root": runtime_root,
                        }
                    )
                    socket_transport = "stdio+socket"
                    socket_block = {
                        "outcome": "listening",
                        "host": SOCKET_HOST,
                        "port": port,
                        "started_at": socket_server.started_at,
                    }
                else:
                    socket_block = lock_result.payload()
            except Exception as exc:
                # A transport that failed to come up must not take the runtime
                # with it: stdio still works, and the typed outcome is how an
                # operator learns the socket did not.
                try:
                    if socket_lock is not None:
                        socket_lock.release()
                except Exception:
                    pass
                socket_server = None
                socket_lock = None
                socket_block = {"outcome": f"error:{type(exc).__name__}"}
        # 4. DISCOVERY. Multiple runtime roots legitimately coexist on this
        #    machine (QA lanes, isolated worktree roots), and until now
        #    "how many serves are running against this root, on what code"
        #    had no answer at all. The entry is removed on every clean exit
        #    (shutdown AND drain); a crash leaves it, which is why liveness is
        #    proven at READ time and never trusted from the file.
        #
        #    The socket fields ride the SAME entry (additive): a client
        #    discovering "the service for root X" reads the port from the
        #    instance whose liveness the registry has just classified, rather
        #    than from a second file with its own staleness story.
        instance_block: dict[str, Any] = {"outcome": "error:root_unresolved"}
        if store_root_path is not None:
            try:
                from agent_runtime.serve_registry import register_serve_instance

                instance_block = register_serve_instance(
                    store_root_path,
                    transport=socket_transport,
                    build=build_block,
                    boot_id=boot_id,
                    port=socket_server.port if socket_server is not None else None,
                    socket_started_at=(
                        socket_server.started_at if socket_server is not None else None
                    ),
                ).payload()
            except Exception as exc:
                instance_block = {"outcome": f"error:{type(exc).__name__}"}
        timeline.mark("service_foundations_ms")
        # Orphaned-turn sweep BEFORE the ready frame: serve boot is the moment
        # a launcher restart replaces a dead runtime, and the first hydrate is
        # only requested after ready — so records a dead executor left frozen
        # in-flight (lease provably free) already project as typed
        # ``turn_interrupted`` markers in that hydrate instead of a console
        # stuck "running" forever. Bounded (≤50 session files) and fail-open.
        orphaned_repaired: list[str] = []
        try:
            from agent_runtime.persona_chat_continuity import repair_orphaned_chat_turns

            orphaned_repaired = repair_orphaned_chat_turns()
        except Exception:
            orphaned_repaired = []
        timeline.mark("orphaned_turn_sweep_ms")
        # Same moment, same reason, for detached dispatches: a row still marked
        # ``running`` whose owning process is provably gone can never finish, and
        # the sender is owed that answer too. Reclassifying it here — BEFORE the
        # drain starts — turns "the agent I dispatched went silent forever" into
        # a delivered "the outcome is unknown, re-send if you still need it".
        # Identity-verified (a recycled PID is not the old owner) and fail-open.
        dispatches_restored = 0
        try:
            from agent_runtime.dispatch_store import restore_undelivered_dispatches

            dispatches_restored = int(
                (restore_undelivered_dispatches() or {}).get("restored") or 0
            )
        except Exception:
            dispatches_restored = 0
        timeline.mark("dispatch_restore_ms")
        ready_frame: dict[str, Any] = {
            "event": "ready",
            "pid": os.getpid(),
            "schema_version": SERVE_SCHEMA_VERSION,
            "runtime_root": runtime_root,
            # Additive, always present (never conditional on success): a
            # missing block would read as "old runtime", while a block whose
            # own fields say `unknown`/`error:…` reads as what it is — the
            # measurement was attempted and this is what it found.
            "boot_id": boot_id,
            "build": build_block,
            "auth": auth_block,
            "instance": instance_block,
            # ``disabled`` (no socket lane asked for), ``listening`` with the
            # port, ``lock_held_by`` with the winner's pid, or ``error:<reason>``
            # — the outcome is stated either way, never inferred from absence.
            "socket": socket_block,
            # The METHOD lane's capability manifest — ``{"contract":N,
            # "methods":[…]}``. This is stdio's greeting, so this is where a
            # stdio client learns the method set; the socket's equivalent is
            # ``hello_ok``, and both are restated on the re-askable ``version``
            # reply. Same shape of promise as ``hello_contract``: the server
            # advertises, the client asserts, and a runtime that predates the
            # lane carries no ``rpc`` key at all — which reads as "argv only"
            # rather than as a failure.
            "rpc": serve_rpc.manifest(),
        }
        if orphaned_repaired:
            ready_frame["orphaned_turns_repaired"] = len(orphaned_repaired)
        if dispatches_restored:
            ready_frame["dispatches_restored"] = dispatches_restored
        # Every phase this boot actually paid, on the frame the supervisor
        # already waits for — and the same line in agent.log, because the boot
        # worth attributing (the cold one) is the boot nobody is watching a
        # console for. Emission is defensive: a broken instrument must never be
        # the reason a runtime fails to come up.
        try:
            ready_frame["boot_timeline"] = timeline.stamps()
        except Exception:
            pass
        # Read-model warmup starts BEFORE ``ready`` is announced, unlike the
        # provider warmup below. The launcher's first request lands within
        # milliseconds of this frame, and only the build that STARTED FIRST can
        # be shared: if the request wins the race it leads its own build and
        # the warmup then queues a second, redundant one behind it. Starting a
        # daemon thread costs microseconds, so ``ready`` is not delayed.
        if snapshot_prewarm is not None:
            threading.Thread(
                target=snapshot_prewarm,
                name="harness-serve-snapshot-prewarm",
                daemon=True,
            ).start()
        frames.emit(ready_frame)
        try:
            import logging as _logging

            _logging.getLogger(__name__).info(
                timeline.log_line("harness serve boot timeline:")
            )
        except Exception:
            pass
        # Prewarm the first chat turn's one-time costs in the background:
        # lazy OpenAI SDK import (~1.7s), shared SSL context / CA-guard
        # verification (~0.7s), and the tool-definition module imports +
        # registry build (~1.2s). Serve boots eagerly at Mission Control
        # open, so this runs while the operator is still looking at the
        # canvas — without it the FIRST message of every launcher session
        # pays the whole warmup inline. Best-effort: failures surface on
        # the first real turn exactly as they do today.
        threading.Thread(
            target=_prewarm_provider_runtime,
            name="harness-serve-prewarm",
            daemon=True,
        ).start()
        # (The read-model warmup runs on its own thread, started just before
        # the ready frame above — it must not queue behind this one's ~3s SDK
        # import, and it must start its build before the first request does.)
        # A busy serve must never look dead. The launcher's stream watchdog
        # keys on "no frames for N seconds", and when pool workers are deep in
        # chat-turn work the infinite `stream` request's generator can starve
        # past that budget — Mission Control then raised the loud "Runtime
        # offline" banner DURING healthy turns (live incident 2026-07-23,
        # two flaps inside one 4-minute Neko turn). This dedicated thread
        # emits the same typed `busy` frame the `ping` op returns whenever
        # requests are in flight: pure liveness telemetry on the shared
        # stdout, independent of every pool worker, so the launcher can
        # distinguish "busy running your turn" from "gone".
        liveness_stop = threading.Event()

        def _liveness_pump() -> None:
            while not liveness_stop.wait(liveness_pump_interval_seconds):
                with inflight_lock:
                    busy = bool(inflight)
                if not busy:
                    continue
                try:
                    frames.emit(_busy_frame())
                except Exception:
                    # Writer gone — the main loop is on its way down too.
                    return

        threading.Thread(
            target=_liveness_pump,
            name="harness-serve-liveness",
            daemon=True,
        ).start()
        # Detached-dispatch delivery. This is the half that makes
        # `agent_chat_send(wait=false)` honest: the target's turn ran in the
        # background, its answer is durable, and this thread forges it back into
        # the SENDER's thread once that thread is idle. It lives HERE, and only
        # here, because serve is the one long-lived process that hosts persona
        # turns — a one-shot CLI exits long before a 30-minute dispatch lands.
        #
        # Started after `ready` (so a cold boot is never delayed by a delivery)
        # and stopped with the liveness pump before `shutdown` (so it cannot
        # forge a turn into a process that is on its way down). Best effort by
        # contract: a runtime that cannot start the drain still serves, and the
        # completions stay pending for the next boot rather than being lost.
        try:
            from agent_runtime.dispatch_delivery import start_delivery_drain

            # Rehydrate durable delegation completions BEFORE the drain that
            # will deliver them starts — explicit at serve boot, never as an
            # import side effect (same #16856 class as module-scope MCP
            # discovery; see
            # docs/agent-runtime-harness/eager-tool-discovery-audit-2026-08-09.md).
            from tools.process_registry import process_registry

            process_registry.restore_durable_completions()
            start_delivery_drain(stop_event=liveness_stop)
        except Exception:
            # Function-local: parts files are exec'd into harness.py's globals,
            # which carry no module logger.
            #
            # WARNING, not debug: a drain that fails to start disables the
            # entire `agent_chat_send(wait=false)` lane for the life of this
            # serve — every dispatch is refused with
            # `async_delivery_unavailable` — and at debug level that
            # feature-killing fact was invisible in every live log
            # (2026-08-09 dispatch-lane investigation).
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "dispatch delivery drain did not start; "
                "agent_chat_send(wait=false) will be refused for this serve",
                exc_info=True,
            )

        def _unregister_instance() -> None:
            """Drop this serve's registry entry. Idempotent, never raises."""

            if store_root_path is None:
                return
            try:
                from agent_runtime.serve_registry import (
                    serve_instance_path,
                    unregister_serve_instance,
                )

                if not unregister_serve_instance(store_root_path):
                    # Reported, not swallowed. A clean exit that leaves its
                    # entry behind makes the registry claim a serve that is on
                    # its way out, and the next client's discovery would try to
                    # connect to it. The read-time classification eventually
                    # calls it dead — "eventually" is the part an operator has
                    # to be able to see coming.
                    if serve_instance_path(store_root_path, os.getpid()).exists():
                        _service_log(
                            {
                                "event": "serve_instance_unregister_failed",
                                "boot_id": boot_id,
                                "pid": os.getpid(),
                                "path": str(
                                    serve_instance_path(store_root_path, os.getpid())
                                ),
                            }
                        )
            except Exception:
                pass

        # ── socket lane plumbing ────────────────────────────────────────────
        #
        # Everything below is inert on a stdio-only serve: ``socket_server`` is
        # None, no connection ever exists, and the stdio path never reaches a
        # branch that touches it.

        connection_sinks: dict[str, _SafeSink] = {}
        connection_sinks_lock = threading.Lock()

        def _emit_safely(sink: Any, frame: dict[str, Any]) -> None:
            try:
                sink.emit(frame)
            except Exception:
                pass

        def _sink_for(connection: Any) -> Any:
            """The STABLE per-connection request sink.

            Stable matters twice: the partial-line buffers in
            ``_LineFrameProxy`` are keyed on the sink's identity, and a sink
            rebuilt per line would split one handler's output across two
            buffers mid-line.
            """

            if connection is None:
                return frames
            with connection_sinks_lock:
                sink = connection_sinks.get(connection.key)
                if sink is None:
                    sink = _SafeSink(connection)
                    connection_sinks[connection.key] = sink
                return sink

        def _owner_of(connection: Any) -> str:
            return "stdio" if connection is None else str(connection.key)

        def _accepted_fold_entities() -> Any:
            """What the SHARED producer may promote: the intersection of every
            attached subscriber's declaration.

            One producer feeds N subscribers (``serve_stream_hub``), so a patch
            frame promoted for a client that declared ``office_actor`` would ALSO
            be fanned out to the launcher next to it, which cannot fold that
            entity and would answer with a full re-hydrate. Intersection is the
            only rule under which a promotion is safe for everyone in the room;
            a client that declared nothing contributes the historical set, so a
            room of only today's clients accepts exactly today's set.

            Read at PRODUCER-BUILD time (``subscribe`` restarts the producer, so
            every join re-derives it). A LEAVE deliberately does not re-widen the
            running producer: it would have to restart it — costing every
            remaining subscriber a fresh full core — to buy back a promotion they
            were already living without. The next join re-derives it anyway.
            """

            from agent_runtime.patch_coverage import accepted_fold_entities

            with lane_lock:
                declarations = list(stream_fold_entities.values())
            return accepted_fold_entities(declarations)

        #: Does an INJECTED source factory want the negotiated fold set? Answered
        #: once, by signature — never by calling it and catching ``TypeError``,
        #: which would swallow a TypeError raised INSIDE a zero-arg factory and
        #: retry it at a different arity (the reasoning ``serve_stream_hub``
        #: records for its own stop-event probe, one seam up).
        stream_factory_takes_fold_entities = False
        if stream_source_factory is not None:
            try:
                inspect.signature(stream_source_factory).bind(frozenset())
                stream_factory_takes_fold_entities = True
            except (TypeError, ValueError):
                stream_factory_takes_fold_entities = False

        def _stream_source() -> Any:
            """The shared subscription producer. One per serve, never per client."""

            fold_entities = _accepted_fold_entities()
            if stream_source_factory is not None:
                return (
                    stream_source_factory(fold_entities)
                    if stream_factory_takes_fold_entities
                    else stream_source_factory()
                )
            from agent_runtime.serde import to_jsonable
            from agent_runtime.stream import stream_frames

            def _generate():
                for frame in stream_frames(fold_entities=fold_entities):
                    # Byte-for-byte the frames ``harness stream`` writes: a
                    # subscriber folds the same hydrate/delta/patch/heartbeat
                    # shapes it already folds, so the socket lane introduces no
                    # second stream contract to keep in sync.
                    yield to_jsonable(frame)

            return _generate()

        def _ensure_stream_hub() -> Any:
            nonlocal stream_hub
            with lane_lock:
                if stream_hub is None:
                    from agent_runtime.serve_stream_hub import (
                        DEFAULT_BUFFER_LIMIT,
                        DEFAULT_BYTE_LIMIT,
                        StreamHub,
                    )

                    stream_hub = StreamHub(
                        _stream_source,
                        buffer_limit=int(stream_buffer_limit or DEFAULT_BUFFER_LIMIT),
                        byte_limit=int(stream_byte_limit or DEFAULT_BYTE_LIMIT),
                        log=_service_log,
                    )
                return stream_hub

        def _release_subscription(connection: Any) -> None:
            """A client left. Unsubscribe it, and do NOTHING else.

            Not a cancellation, not a shutdown, not a state change: the runtime
            outliving its clients is the entire point of the durable service,
            and a disconnect that touched backend state would reintroduce the
            per-client lifecycle ownership this workstream exists to retire.
            """

            key = _owner_of(connection)
            with lane_lock:
                hub = stream_hub
                # A departed client's fold declaration must not keep narrowing
                # the lane for the clients that remain — the next subscribe
                # re-derives the accepted set from whoever is actually here.
                stream_fold_entities.pop(key, None)
            if hub is not None:
                try:
                    hub.unsubscribe(key)
                except Exception:
                    pass
            if connection is not None:
                connection.subscribed = False
                with connection_sinks_lock:
                    connection_sinks.pop(connection.key, None)

        def _close_socket_lane(reason: str) -> None:
            """Stop the hub, close every connection, release the ownership lock.

            Idempotent and never raises: it runs on the drain path, the
            shutdown path, and the EOF path, and any of them may be second.
            """

            nonlocal socket_server, socket_lock, stream_hub
            # The three swaps happen together, under the lock, and NOTHING
            # slow happens while it is held: whoever takes a handle owns
            # closing it, and a second caller gets None and does nothing.
            with lane_lock:
                hub, stream_hub = stream_hub, None
                server, socket_server = socket_server, None
                lock, socket_lock = socket_lock, None
                stream_fold_entities.clear()
            if hub is not None:
                try:
                    # One TOTAL budget for the hub, not one per subscriber
                    # join: the drain's exit watchdog is already armed, and a
                    # teardown that can outlast it is how a drained runtime
                    # kept running.
                    hub.stop()
                except Exception:
                    pass
            if server is not None:
                try:
                    server.close(reason=reason)
                except Exception:
                    pass
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

        def _build_mismatch(client_build: Any) -> bool | None:
            """Does the client's build disagree with the code answering it?

            None means NOT COMPARABLE — the client named no build, or this
            runtime could not measure its own. A fabricated ``false`` there
            would answer "you are current" for a runtime that does not know,
            which is exactly the false-all-clear the build stamp exists to
            retire. Prefix comparison so a short hash and a full one agree.
            """

            serve_commit = build_block.get("commit")
            if not isinstance(serve_commit, str) or not serve_commit:
                return None
            if not isinstance(client_build, str) or len(client_build.strip()) < 7:
                return None
            claimed = client_build.strip().lower()
            actual = serve_commit.lower()
            return not (
                actual.startswith(claimed) or claimed.startswith(actual)
            )

        def _hello_ok_frame(message: dict[str, Any], connection: Any) -> dict[str, Any]:
            """The version handshake, enforced end to end at the door."""

            from agent_runtime.serve_socket import HELLO_CONTRACT_VERSION

            return {
                "event": "hello_ok",
                "pid": os.getpid(),
                "boot_id": boot_id,
                # The frame-protocol contract this service speaks. A client that
                # does not recognise it must not proceed on hope.
                "contract": SERVE_SCHEMA_VERSION,
                # Restated from ``server_hello`` so a client that reconnects and
                # reads only the reply still learns which handshake it just
                # completed.
                "hello_contract": HELLO_CONTRACT_VERSION,
                "schema_version": SERVE_SCHEMA_VERSION,
                "transport": "socket",
                "connection": connection.key,
                "runtime_root": runtime_root,
                "build": build_block,
                # Visible, never fatal: a client on other code still gets to
                # work, and now KNOWS it is talking to a different build.
                "build_mismatch": _build_mismatch(connection.client_build),
                "draining": drain_state is not None,
                # The socket's half of the method-lane advertisement. A socket
                # client never reads ``ready`` (that frame goes to the stdio
                # owner), so without this it could only learn the method set by
                # asking ``version`` — one extra round trip on every connect,
                # for something the handshake it already performs can carry.
                "rpc": serve_rpc.manifest(),
            }

        def _connections_frame() -> dict[str, Any]:
            payload: dict[str, Any] = {"event": "socket_connections", "boot_id": boot_id}
            with lane_lock:
                server = socket_server
            if server is None:
                payload["enabled"] = False
                payload["socket"] = socket_block
                payload["count"] = 0
                payload["connections"] = []
            else:
                payload["enabled"] = True
                payload.update(server.connections_payload())
            with lane_lock:
                hub = stream_hub
            payload["subscriptions"] = (
                hub.stats() if hub is not None else {"subscribers": 0}
            )
            return payload

        def _handle_socket_line(line: str, connection: Any) -> None:
            """Every authenticated socket line enters the SHARED dispatcher."""

            _handle_line(line, _sink_for(connection), connection=connection)

        def _finish_drain(code: int, frame: dict[str, Any]) -> None:
            """Emit the drain's terminal frame, then get the process out.

            Order is the contract: the frame is written and flushed BEFORE any
            exit path, because a drain that took the process down without
            accounting for what it refused and what it completed is
            indistinguishable from the crash the drain exists to replace.
            """

            nonlocal drain_exit_code, pool_shutdown_wait

            # A drain has ONE terminal frame. The latch is taken before
            # anything is published, so a mid-drain EOF racing a completing
            # drain cannot follow ``drain_complete`` with ``drain_abandoned``.
            with drain_terminal_lock:
                if drain_terminal_published.is_set():
                    return
                drain_terminal_published.set()
            drain_exit_code = code
            if code != 0:
                # Stuck workers: do NOT let the pool's context manager join
                # them (it would hang exactly as long as "forever"), and do not
                # trust a plain return either — concurrent.futures' atexit hook
                # joins worker threads on the way out of the interpreter.
                pool_shutdown_wait = False
            # THE WATCHDOG IS THE FIRST ACT, before the frame, the broadcast,
            # and the teardown — because every one of those can block. It used
            # to be armed after them, so the very steps most likely to hang ran
            # unwatched: broadcasting to a wedged reader parks a ``sendall``
            # for IO_TIMEOUT, and the hub's joins were a per-subscriber budget
            # that SUMMED. And the wakeup itself can block: observed live on
            # Windows (2026-08-13), closing the protocol descriptor a reader is
            # parked on does not return until that read does, and the child
            # outlived its own completed drain. From here to process exit
            # everything is inside one deadline.
            if hard_exit is not None:
                threading.Thread(
                    target=_force_exit_after_drain,
                    args=(code,),
                    name="harness-serve-drain-exit",
                    daemon=True,
                ).start()
            frames.emit(frame)
            # Socket clients are owed the SAME terminal frame: a client that
            # asked for the drain over the socket, and every client that was
            # merely attached, learns how it ended on the transport it is on.
            # Broadcast before teardown — after ``_close_socket_lane`` there is
            # nobody left to tell.
            if socket_server is not None:
                try:
                    socket_server.broadcast(frame)
                except Exception:
                    pass
            _close_socket_lane(reason="drain")
            _unregister_instance()
            drain_finished.set()
            if drain_wakeup is not None:
                try:
                    drain_wakeup()
                except Exception:
                    pass
            if hard_exit is None:
                # Unit-test path: the loop returns ``drain_exit_code`` and the
                # caller observes the frames. No process-level lever is pulled.
                return
            if code != 0:
                hard_exit(code)
                return
            # Clean drain: the reader gets its chance to unwind normally
            # (closed sockets, flushed writer, restored stdio) and the watchdog
            # above forces the exit if it does not. Nothing is waited on here.

        def _force_exit_after_drain(code: int) -> None:
            """Force the process down if the drain does not finish getting out.

            Armed at the START of ``_finish_drain``, so its deadline covers the
            WHOLE tail: publishing the terminal frame, broadcasting it, closing
            the socket lane (hub joins, connection closes, lock release),
            unregistering, waking the reader, and the reader unwinding. The
            normal case returns in milliseconds; anything else is a drained
            runtime that is still running, which is the state this exists to
            make impossible.

            Read from the module at call time on purpose — a test lowers it.
            """

            deadline = time.monotonic() + _DRAIN_EXIT_DEADLINE_SECONDS
            while time.monotonic() < deadline:
                if drain_finished.is_set() and reader_unwound.is_set():
                    return
                time.sleep(0.02)
            if hard_exit is not None:
                hard_exit(code)

        def _drain_monitor(state: _DrainState) -> None:
            deadline = state.started_monotonic + state.deadline_seconds
            last_progress = state.started_monotonic
            while True:
                with inflight_lock:
                    remaining = sorted(inflight)
                    # Read in the SAME critical section as the pending set: a
                    # timeout that decided "no chat turns" from a second,
                    # later read could kill the turn that started in between.
                    chat_turn_ids = sorted(
                        key for key, item in inflight.items() if item.is_chat_turn
                    )
                if not remaining:
                    _finish_drain(
                        0,
                        {
                            "event": "drain_complete",
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            **state.counters(),
                            "drain_ms": state.elapsed_ms(),
                        },
                    )
                    return
                now = time.monotonic()
                if now >= deadline:
                    expiry = {
                        "event": "drain_timeout",
                        "pid": os.getpid(),
                        "boot_id": boot_id,
                        **state.counters(),
                        "drain_ms": state.elapsed_ms(),
                        "deadline_seconds": state.deadline_seconds,
                        # WHICH requests are stuck, by id — a timeout that
                        # only reported a count would leave the operator
                        # with nothing to correlate against the stack dump.
                        "stuck_request_ids": remaining,
                        # And WHY it is allowed to be stuck. A chat turn in
                        # flight is recording-safety work: this file's own
                        # contract says a supervisor must never recycle serve
                        # while ``chat_turns`` > 0, and a drain deadline firing
                        # `hard_exit` (which is `os._exit`) over one is that
                        # recycle by another name.
                        "held_by_chat_turns": len(chat_turn_ids),
                        "chat_turn_request_ids": chat_turn_ids,
                        "terminal": not chat_turn_ids,
                    }
                    if chat_turn_ids:
                        # NOT terminal: say so, keep serving, re-arm. The frame
                        # is emitted every time the deadline lapses, so a
                        # supervisor watching a drain that is being held open
                        # sees each hold rather than silence.
                        state.note_deadline_held()
                        expiry.update(state.counters())
                        frames.emit(expiry)
                        if socket_server is not None:
                            try:
                                socket_server.broadcast(expiry)
                            except Exception:
                                pass
                        deadline = now + state.deadline_seconds
                        last_progress = now
                        time.sleep(max(0.0, drain_poll_interval_seconds))
                        continue
                    _finish_drain(DRAIN_TIMEOUT_EXIT_CODE, expiry)
                    return
                if now - last_progress >= _DRAIN_PROGRESS_INTERVAL_SECONDS:
                    progress = {
                        "event": "drain_progress",
                        "pending": len(remaining),
                        "request_ids": remaining,
                        "drain_ms": state.elapsed_ms(),
                    }
                    frames.emit(progress)
                    # The ONE drain frame that reached stdio and nothing else.
                    # Its entire purpose is that "a draining service never looks
                    # dead to a watchdog" — and the socket client IS such a
                    # watchdog: it reads with a finite timeout and reports
                    # `transport_failed` on silence. With the socket lane's
                    # minimum deadline, a drain holding a chat turn open puts
                    # the first socket-visible frame 30s out, so a healthy,
                    # completing drain reported a transport failure and exit 6.
                    if socket_server is not None:
                        try:
                            socket_server.broadcast(progress)
                        except Exception:
                            pass
                    last_progress = now
                time.sleep(max(0.0, min(drain_poll_interval_seconds, deadline - now)))

        # ── the shared dispatcher ───────────────────────────────────────────
        #
        # ONE op table, N transports. ``sink`` is where this message's answers
        # go (stdout for stdio, the originating connection for a socket client)
        # and ``connection`` is None on stdio. Every branch below was previously
        # inline in the stdio reader loop and is unchanged in behaviour: on
        # stdio, ``sink is frames`` and ``connection is None``, so the frames,
        # their order, and the exit codes are byte-identical.

        def _handle_line(line: str, sink: Any, *, connection: Any = None) -> str | None:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                sink.emit(
                    {
                        "id": None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": "request line is not valid JSON",
                    }
                )
                return None
            if not isinstance(message, dict):
                sink.emit(
                    {
                        "id": None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": "request must be a JSON object",
                    }
                )
                return None
            return _handle_message(message, sink, connection=connection)

        def _handle_message(
            message: dict[str, Any], sink: Any, *, connection: Any = None
        ) -> str | None:
            """Answer one op. Returns ``"shutdown"`` to stop the stdio reader."""

            nonlocal drain_state

            op = message.get("op")
            if op == "ping":
                sink.emit(_busy_frame())
                return None
            if op == "hello":
                # The socket lane authenticates BEFORE this dispatcher ever
                # sees a line, so a hello arriving here is a second one (or a
                # stdio client speaking the socket handshake at a pipe that
                # needs no handshake). Typed, and never a second auth path.
                sink.emit(
                    {
                        "event": "error",
                        "error": "unexpected_hello",
                        "detail": (
                            "this connection is already established; hello is the "
                            "first line of a SOCKET connection only"
                        ),
                    }
                )
                return None
            if op == "version":
                # Re-askable at any time, and deliberately NOT re-measured:
                # the answer is what code THIS interpreter loaded, which
                # cannot change while it lives. A client comparing against
                # its own install is how "the service is stale" becomes a
                # measurement instead of a theory.
                try:
                    from agent_runtime.build_stamp import build_stamp

                    version_build = build_stamp().payload()
                except Exception as exc:
                    version_build = {
                        "commit": None,
                        "dirty": None,
                        "source": "unknown",
                        "reason": f"stamp_failed:{type(exc).__name__}",
                    }
                sink.emit(
                    {
                        "event": "version",
                        "schema_version": SERVE_SCHEMA_VERSION,
                        "pid": os.getpid(),
                        "boot_id": boot_id,
                        # The transport THIS reply came over — honest per
                        # connection, and unchanged for every stdio consumer.
                        "transport": "stdio" if connection is None else "socket",
                        "runtime_root": runtime_root,
                        "build": version_build,
                        "auth": auth_block,
                        "draining": drain_state is not None,
                        # Additive: what else is attached to this runtime, on
                        # the reply a client already asks for.
                        "socket": socket_block,
                        "connections": _connections_frame(),
                        # Re-askable, like the build stamp beside it and for the
                        # same reason: a durable service outlives the install it
                        # was started from, so "which methods does the thing I
                        # am attached to actually have" must be answerable at
                        # any time, not only at the greeting a client may have
                        # read hours ago.
                        "rpc": serve_rpc.manifest(),
                    }
                )
                return None
            if op == "connections":
                sink.emit(_connections_frame())
                return None
            if op == "subscribe":
                lane = message.get("lane", "stream")
                if lane != "stream":
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": lane,
                            "reason": "unsupported_lane",
                        }
                    )
                    return None
                if drain_state is not None:
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "draining",
                        }
                    )
                    return None
                # Optional patch-fold capability declaration. ABSENT means the
                # client said nothing — the historical {persona_instance,
                # incident} — which is what every client in the field sends and
                # is exactly today's wire. Present-but-malformed is REFUSED
                # rather than quietly read as absent: a client that meant to
                # narrow the set and was silently widened back to the historical
                # one would get patches it cannot fold, which is the precise
                # failure this negotiation exists to prevent.
                declared_raw = message.get("fold_entities")
                if declared_raw is None:
                    declared_fold_entities: Any = None
                elif isinstance(declared_raw, list) and all(
                    isinstance(name, str) and name.strip() for name in declared_raw
                ):
                    declared_fold_entities = frozenset(
                        name.strip() for name in declared_raw
                    )
                else:
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "invalid_fold_entities",
                        }
                    )
                    return None
                key = _owner_of(connection)
                hub = _ensure_stream_hub()
                if hub.has(key):
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "already_subscribed",
                        }
                    )
                    return None
                # Recorded BEFORE ``hub.subscribe``: that call starts the new
                # producer generation, which reads this table to decide what it
                # may promote. Recorded after, this subscriber's declaration
                # would not reach the very producer its own subscribe created.
                with lane_lock:
                    stream_fold_entities[key] = declared_fold_entities
                accepted_entities = sorted(_accepted_fold_entities())
                raw_sink = connection.emit if connection is not None else frames.emit

                def _on_drop(reason: str, stats: dict[str, Any]) -> None:
                    # Typed, never silent: an unsubscribed client that was told
                    # nothing would keep folding a stream that stopped arriving
                    # and believe itself current.
                    #
                    # The buffer is bounded TWICE — by frame count and by bytes
                    # — so the drop has to say WHICH bound tripped and carry
                    # both sets of numbers. The hub measures all of this and
                    # this frame used to throw it away, reporting a count
                    # against a `buffer_limit` read from the CONFIG rather than
                    # from the hub (None whenever it was left at the default).
                    # A client told only `backpressure` cannot tell one that
                    # fell 256 heartbeats behind from one that pinned 32 MiB,
                    # which is the difference between resubscribing and fixing
                    # its reader.
                    _emit_safely(
                        sink,
                        {
                            "event": "subscription_dropped",
                            "lane": "stream",
                            "reason": reason,
                            "bound": stats.get("drop_bound"),
                            "frames_delivered": stats.get("frames_delivered"),
                            "frames_discarded": stats.get("frames_discarded"),
                            "bytes_discarded": stats.get("bytes_discarded"),
                            "buffer_limit": stats.get("frame_limit"),
                            "byte_limit": stats.get("byte_limit"),
                        },
                    )
                    _release_subscription(connection)
                    _service_log(
                        {
                            "event": "serve_stream_subscription_dropped",
                            "boot_id": boot_id,
                            "connection": key,
                            "client": getattr(connection, "client", None),
                            "reason": reason,
                            "bound": stats.get("drop_bound"),
                            "frames_discarded": stats.get("frames_discarded"),
                            "bytes_discarded": stats.get("bytes_discarded"),
                        }
                    )

                # The ACK precedes the subscription, deliberately. The producer
                # starts pushing the moment ``subscribe`` returns, so acking
                # afterwards would let the hydrate overtake the ack — and a
                # client reading "everything up to my ack is a reply to
                # something else" would discard its own baseline.
                if connection is not None:
                    connection.subscribed = True
                sink.emit(
                    {
                        "event": "subscribed",
                        "lane": "stream",
                        "connection": key,
                        "buffer_limit": hub.stats().get("buffer_limit"),
                        # What the shared producer will actually promote — which
                        # can be LESS than this client asked for, because another
                        # subscriber folds less. Echoed on the ack (and again on
                        # the hydrate) so a client can see the answer instead of
                        # assuming its request was the answer.
                        "fold_entities": accepted_entities,
                    }
                )
                if not hub.subscribe(key, sink=raw_sink, on_drop=_on_drop):
                    # Lost a race with another subscribe for the same key. Say
                    # so rather than leave a client believing it is attached.
                    if connection is not None:
                        connection.subscribed = False
                    # The declaration is deliberately LEFT in place. This branch
                    # means another subscribe for the same key won the race, so
                    # that key IS attached — dropping its declaration here could
                    # only WIDEN the lane under a subscriber that never asked
                    # for the wider set, which is the failure direction. A stale
                    # entry can only ever narrow, and ``_release_subscription``
                    # (or the lane close) clears it.
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "already_subscribed",
                        }
                    )
                return None
            if op == "unsubscribe":
                key = _owner_of(connection)
                with lane_lock:
                    hub = stream_hub
                was_subscribed = hub is not None and hub.has(key)
                _release_subscription(connection)
                sink.emit(
                    {
                        "event": "unsubscribed",
                        "lane": "stream",
                        "connection": key,
                        "was_subscribed": was_subscribed,
                    }
                )
                return None
            if op == "drain":
                if connection is not None and message.get("force") is not True:
                    # The socket lane's second key. `shutdown` is refused there
                    # outright because a client does not get to kill a service
                    # other clients are using; `drain` is the safe replacement
                    # verb, but it still ENDS this process, and any local
                    # process holding the root's secret can ask. One explicit
                    # field is a trivial cost for an operator and a real
                    # barrier against an automated or accidental restart.
                    sink.emit(
                        {
                            "event": "error",
                            "error": "drain_requires_force",
                            "transport": "socket",
                            "detail": (
                                "drain over the socket ends the service for every "
                                'attached client; resend as {"op":"drain","force":true}'
                            ),
                        }
                    )
                    return None
                # The EFFECTIVE deadline is decided here, server-side, from the
                # client's ask floored by the minimum for the TRANSPORT it came
                # in on. Over stdio the asker owns this process outright and the
                # ask stands as given (the pre-socket contract, untouched); over
                # the socket it is floored, because that asker is any local
                # process holding the root's secret and it is shortening a
                # promise made to work it cannot see.
                effective_minimum = (
                    drain_socket_minimum_deadline_seconds
                    if connection is not None
                    else _DRAIN_DEADLINE_FLOOR_SECONDS
                )
                effective_deadline = _drain_deadline_seconds(
                    message.get("deadline_seconds"),
                    drain_deadline_seconds,
                    minimum=effective_minimum,
                )
                # ONE critical section for the whole transition. The guard and
                # the install used to be a bare read-modify-write on a closure
                # variable, which was harmless while the only caller was the
                # single stdio reader and became a genuine race the moment N
                # connection threads could ask: two of them could both observe
                # ``None``, both install a ``_DrainState``, and the process
                # would then run two monitors, publish two terminal frames, and
                # split its counters across two objects. The "already draining"
                # answer is decided INSIDE the section that would have
                # installed it, so it cannot be decided against a state a
                # sibling thread is mid-way through replacing.
                with inflight_lock:
                    existing = drain_state
                    if existing is None:
                        drain_state = _DrainState(effective_deadline)
                        started = drain_state
                        pending_at_start = sorted(inflight)
                if existing is not None:
                    sink.emit(
                        {
                            "event": "drain_in_progress",
                            "drain_ms": existing.elapsed_ms(),
                            **existing.counters(),
                        }
                    )
                    return None
                # Stop the delivery drain (and with it the busy pump) the
                # moment we stop accepting work: it forges completed
                # dispatches back into a sender's thread, and doing that to
                # a process on its way down is exactly what the shutdown
                # path already refuses to allow. `drain_progress` frames
                # take over the liveness duty for the rest of the wait.
                liveness_stop.set()
                draining_frame = {
                    "event": "draining",
                    "id": None,
                    "pid": os.getpid(),
                    "boot_id": boot_id,
                    "pending": len(pending_at_start),
                    "request_ids": pending_at_start,
                    "deadline_seconds": started.deadline_seconds,
                    # What was ASKED for, beside what was granted: a client
                    # that requested 0.05s and got 30 must be able to see that
                    # its ask was floored rather than honoured.
                    "requested_deadline_seconds": message.get("deadline_seconds"),
                    "minimum_deadline_seconds": effective_minimum,
                }
                frames.emit(draining_frame)
                if socket_server is not None:
                    # New sockets are refused from here (existing ones stay up
                    # to be told how it ends), and every attached client hears
                    # it at the same moment the stdio supervisor does.
                    try:
                        socket_server.begin_drain()
                        socket_server.broadcast(draining_frame)
                    except Exception:
                        pass
                threading.Thread(
                    target=_drain_monitor,
                    args=(started,),
                    name="harness-serve-drain",
                    daemon=True,
                ).start()
                return None
            if op == "stacks":
                # Operator diagnostic: dump every thread's stack as
                # stderr frames (hung-request forensics without py-spy).
                import traceback

                for thread_id, frame in sys._current_frames().items():
                    sink.emit(
                        {
                            "id": None,
                            "event": "stderr",
                            "line": f"--- thread {thread_id} ---",
                        }
                    )
                    for entry in traceback.format_stack(frame):
                        for line in entry.rstrip().splitlines():
                            sink.emit(
                                {"id": None, "event": "stderr", "line": line}
                            )
                sink.emit({"event": "stacks_dumped"})
                return None
            if op == "shutdown":
                if connection is not None:
                    # A socket client does NOT get to kill a service other
                    # clients are using. `drain` is the multi-client lifecycle
                    # verb — it refuses new work, lets in-flight work land, and
                    # accounts for both — and `shutdown` stays what it has
                    # always been: the verb of the process that owns the pipe.
                    sink.emit(
                        {
                            "event": "error",
                            "error": "op_not_available_on_socket",
                            "detail": (
                                "shutdown is the stdio owner's verb; use "
                                '{"op":"drain"} to replace the service safely'
                            ),
                        }
                    )
                    return None
                return "shutdown"
            if op == "cancel":
                cancel_id = message.get("id")
                cancel_id = cancel_id.strip() if isinstance(cancel_id, str) else ""
                if not cancel_id:
                    sink.emit(
                        {
                            "id": None,
                            "event": "error",
                            "error": "invalid_request",
                            "detail": 'cancel needs {"op": "cancel", "id": "<request id>"}',
                        }
                    )
                    return None
                # Scoped to the asker's OWN work: the inflight table is keyed
                # per owner, so one client can neither cancel nor even observe
                # another's request id.
                owner = _owner_of(connection)
                cancel_key = (
                    cancel_id if owner == "stdio" else f"{owner}:{cancel_id}"
                )
                with inflight_lock:
                    future = inflight_futures.get(cancel_key)
                    running_request = inflight.get(cancel_key)
                    known = running_request is not None
                if future is not None and future.cancel():
                    with inflight_lock:
                        inflight.pop(cancel_key, None)
                        inflight_futures.pop(cancel_key, None)
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "exit",
                            "code": 130,
                            "cancelled": True,
                        }
                    )
                elif running_request is not None and running_request.is_runtime_stream:
                    # The state stream is read-only and infinite. Unlike a
                    # mutation, it has a cooperative cancellation seam and
                    # MUST release its worker when the Launcher reconnects;
                    # otherwise four watchdog cycles exhaust the entire
                    # serve pool with abandoned streams.
                    running_request.cancel_event.set()
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "cancel_accepted",
                            "state": "running",
                        }
                    )
                else:
                    # Already running (uninterruptible) or unknown — the
                    # side effect may still land; mutation verbs' own
                    # --issued-at replay guard is what makes that safe.
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "cancel_denied",
                            "state": "running" if known else "unknown",
                        }
                    )
                return None
            # ── the METHOD lane ─────────────────────────────────────────────
            #
            # Named JSON-RPC 2.0 methods, BESIDE the argv lane rather than
            # instead of it (decision doc §3 / launcher `fa2226750`). The argv
            # lane below is unchanged and stays the fallback: it has never sent
            # `jsonrpc` or `method`, so nothing that used to reach it can be
            # captured here, and nothing about its frames or exit codes moves.
            #
            # Answered INLINE, like `ping` / `version` / `connections` and
            # unlike an argv request. The pool exists for handlers that block —
            # chat turns, streams — and these methods touch a handful of small
            # JSON files under the office lock and are done in microseconds.
            #
            # It is also why the lane is not refused while draining, and the
            # test that matters here is NOT "is it a read": `runtime.office.
            # upsert` mutates and is still answered. A drain refuses new WORK so
            # in-flight work can land, and the work it is protecting is the kind
            # that can be CUT OFF HALF-DONE — a chat turn whose frames stop
            # mid-stream when the process exits. An inline handler cannot be:
            # `OfficeStore` has written the actor file atomically and released
            # the lock before the ack is emitted, and the replacement runtime
            # reads that same file. Refusing it would fail an operator's drag
            # during a restart to protect against a loss that cannot occur.
            # `version` and `ping` are answered throughout for the same reason.
            # (Pinned by `test_a_write_during_a_drain_lands_because_it_cannot_be
            # _cut_off_half_done` in tests/agent_runtime/test_serve_rpc_office_
            # upsert.py — this is a decision, not an oversight.)
            #
            # The handler is told WHO asked, not just what. Both facts come
            # from this frame's own dispatch — ``sink`` is the stable
            # per-connection writer ``_sink_for`` hands out, and ``connection``
            # is None exactly on stdio. Nothing here is office-specific: it is
            # the argument a method needs before it can push to its caller
            # LATER, which request/response methods simply ignore.
            if serve_rpc.is_rpc_frame(message):
                sink.emit(
                    serve_rpc.handle_request(
                        message,
                        serve_rpc.RpcContext(
                            connection_key=getattr(connection, "key", None),
                            transport=getattr(connection, "transport", "stdio"),
                            emit=sink.emit,
                        ),
                    )
                )
                return None
            rid = message.get("id")
            argv = message.get("argv")
            if (
                not isinstance(rid, str)
                or not rid.strip()
                or not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) for item in argv)
            ):
                sink.emit(
                    {
                        "id": rid if isinstance(rid, str) else None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": 'request needs {"id": "<non-empty>", "argv": ["harness", …]}',
                    }
                )
                return None
            if drain_state is not None:
                # Refused, and ACCOUNTED: the count lands on the terminal
                # drain frame, so "the restart dropped work" is a number an
                # operator can read rather than an inference.
                drain_state.note_refused()
                sink.emit(
                    {
                        "id": rid.strip(),
                        "event": "draining",
                        "detail": (
                            "serve is draining and is not accepting new requests; "
                            "reconnect to the replacement runtime"
                        ),
                        "drain_ms": drain_state.elapsed_ms(),
                    }
                )
                # Terminal frame too: a client that predates the `draining`
                # event is waiting for an `exit` and would otherwise hang
                # for the life of its request.
                sink.emit(
                    {
                        "id": rid.strip(),
                        "event": "exit",
                        "code": DRAINING_EXIT_CODE,
                        "draining": True,
                    }
                )
                return None
            request = _ArgvRequest(
                rid.strip(),
                [str(item) for item in argv],
                owner=_owner_of(connection),
                sink=None if connection is None else sink,
            )
            with inflight_lock:
                if request.key in inflight:
                    sink.emit(
                        {
                            "id": request.rid,
                            "event": "error",
                            "error": "duplicate_request_id",
                            "detail": "a request with this id is still in flight",
                        }
                    )
                    return None
                inflight[request.key] = request
            future = pool.submit(_run, request)
            with inflight_lock:
                # _run may already have finished and popped the request;
                # only track the future while the request is in flight so
                # the registry cannot leak completed entries.
                if request.key in inflight:
                    inflight_futures[request.key] = future
            return None

        pool = ThreadPoolExecutor(
            max_workers=max(1, pool_size), thread_name_prefix="harness-serve"
        )
        # The socket starts ACCEPTING only now: the listener has been bound
        # since before the ready frame (so the port could be published), but a
        # connection whose first request landed before this pool existed would
        # have nowhere to dispatch. The backlog holds them for the microseconds
        # in between.
        if socket_server is not None:
            try:
                socket_server.start_accepting()
            except Exception as exc:
                _service_log(
                    {
                        "event": "serve_socket_accept_start_failed",
                        "boot_id": boot_id,
                        "reason": type(exc).__name__,
                    }
                )
                _close_socket_lane(reason="accept_start_failed")
        # Explicit construction + shutdown rather than ``with``: the drain's
        # timeout path must be able to stop waiting on work that has proven it
        # will not finish, and a context manager always joins.
        try:
            for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                if _handle_line(line, frames) == "shutdown":
                    break
        finally:
            # The reader is done; from here the process is unwinding normally,
            # which is what the drain monitor's grace window is waiting to see.
            reader_unwound.set()
            # ``wait`` is True everywhere except after a drain TIMEOUT, where
            # the whole point is that the remaining work has already outlived
            # its deadline and joining it would restore the hang.
            pool.shutdown(wait=pool_shutdown_wait)
        liveness_stop.set()
        if drain_state is not None:
            # The drain owns the terminal frame (``drain_complete`` /
            # ``drain_timeout``); emitting ``shutdown`` as well would tell a
            # consumer that a TIMED-OUT drain ended cleanly — the one thing it
            # must not conclude.
            #
            # But the reader can get here BEFORE the monitor has published
            # anything: a `shutdown` op, or the pipe reaching EOF, while a drain
            # is still in progress. That path used to fall straight to
            # ``return drain_exit_code`` — no terminal frame at all, the registry
            # entry left on disk, and code 0 even when the drain had TIMED OUT.
            # A drain that exits silently is exactly the crash it exists to
            # replace, so wait a bounded moment for the monitor (the pool is
            # already joined above, so it is normally one poll away) and, if it
            # never publishes, say so in a typed frame of its own.
            if not drain_finished.wait(_DRAIN_ABANDON_GRACE_SECONDS):
                if drain_terminal_published.is_set():
                    # A drain that already DECIDED how it ended owns the
                    # terminal frame; this path is only for a drain that never
                    # got one. Publishing ``drain_abandoned`` on top of a
                    # completed drain told a supervisor that a successful
                    # restart gave up, and exited 3 on it — the frame and the
                    # code both wrong, about work that had actually landed. The
                    # exit watchdog covers the case where the publisher is the
                    # thing that hung.
                    return drain_exit_code
                abandoned = {
                    "event": "drain_abandoned",
                    "pid": os.getpid(),
                    "boot_id": boot_id,
                    **drain_state.counters(),
                    "drain_ms": drain_state.elapsed_ms(),
                    "detail": (
                        "the transport closed while a drain was still in "
                        "progress; the drain published no terminal frame"
                    ),
                }
                frames.emit(abandoned)
                if socket_server is not None:
                    try:
                        socket_server.broadcast(abandoned)
                    except Exception:
                        pass
                _close_socket_lane(reason="drain_abandoned")
                _unregister_instance()
                # Nonzero on purpose, and the SAME code a timeout uses: a
                # supervisor must be able to tell "drained" from "gave up".
                return DRAIN_TIMEOUT_EXIT_CODE
            # ``_finish_drain`` published the frame, closed the socket lane, and
            # unregistered; ``drain_exit_code`` is its verdict, not this path's.
            return drain_exit_code
        shutdown_frame = {"event": "shutdown", "pid": os.getpid()}
        # Socket clients hear it BEFORE the transport closes under them: an
        # attached client whose socket simply died could not tell a clean
        # service shutdown from a crash, which is the distinction the durable
        # service exists to make legible.
        if socket_server is not None:
            try:
                socket_server.broadcast(shutdown_frame)
            except Exception:
                pass
        _close_socket_lane(reason="shutdown")
        _unregister_instance()
        frames.emit(shutdown_frame)
        return 0
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr


def _raw_fd_lines(fd: int):
    """Yield lines from [fd] as they arrive on an OPEN interactive pipe.

    Reads the descriptor directly (``os.read`` returns per pipe write, lines
    are assembled manually) instead of iterating a text wrapper, so no stdio
    layer can buffer a request until EOF."""
    buffer = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            if buffer:
                yield buffer.decode("utf-8", errors="replace")
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")


def _claim_protocol_pipes() -> tuple[int, int]:
    """Move the NDJSON protocol onto private descriptors and detach fd 0/1.

    Handlers spawn subprocesses (git for dirty state, proof runners, …) that
    inherit the standard descriptors. With serve's stdin pipe left on fd 0,
    any child that reads stdin blocks forever against the Launcher's open
    pipe — ``git status`` deadlocked the whole status handler (observed live
    2026-07-08; the piped smoke passed only because a closed pipe is
    instant EOF). A child writing raw output to an inherited fd 1 would
    likewise corrupt the frame stream. Serve therefore dups the protocol
    pipes to private fds and points fd 0 at the null device (children read
    EOF) and fd 1 at the null device (stray child writes vanish; every
    handler print already flows through the contextvar proxy)."""
    protocol_in = os.dup(0)
    protocol_out = os.dup(1)
    devnull_read = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_read, 0)
    os.close(devnull_read)
    devnull_write = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_write, 1)
    os.close(devnull_write)
    return protocol_in, protocol_out


def _cmd_serve(args) -> int:
    # Started before anything else this command does: everything from process
    # creation up to here is the interpreter + hermes import tax, and it is the
    # single largest term in a cold boot.
    from agent_runtime.boot_timeline import BootTimeline

    timeline = BootTimeline()
    if not getattr(args, "ndjson", False):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "unsupported_transport",
                    "detail": "hermes harness serve currently requires --ndjson (schema v1)",
                }
            )
        )
        return 2
    protocol_in, protocol_out = _claim_protocol_pipes()
    writer = os.fdopen(protocol_out, "w", encoding="utf-8", newline="\n")
    # Function-local on purpose: this file is exec'd into harness.py's globals.
    from agent_runtime.root_anchor import publish_store_root_anchor

    def _wake_reader() -> None:
        """Unblock a reader parked on an idle protocol pipe after a drain.

        Closing the descriptor makes the in-progress (or next) ``os.read``
        fail, which ``_raw_fd_lines`` already treats as end-of-stream. The
        second close in the ``finally`` below is a harmless no-op.
        """

        try:
            os.close(protocol_in)
        except OSError:
            pass

    try:
        return serve_loop(
            _raw_fd_lines(protocol_in),
            writer,
            pool_size=getattr(args, "pool_size", DEFAULT_POOL_SIZE)
            or DEFAULT_POOL_SIZE,
            boot_timeline=timeline,
            snapshot_prewarm=_prewarm_read_model_snapshot,
            root_anchor=publish_store_root_anchor,
            drain_wakeup=_wake_reader,
            # ``os._exit``, not ``sys.exit``: after a drain TIMEOUT the
            # interpreter cannot be trusted to come down at all — the
            # concurrent.futures atexit hook joins worker threads, and the
            # stuck ones are precisely why the deadline fired. Every frame is
            # flushed at emit, so nothing observable is lost.
            hard_exit=os._exit,
            # The durable service's transport. ON here and nowhere else: every
            # ``serve_loop`` unit test observes the byte-identical stdio loop
            # unless it asks for the socket by name.
            socket_lane=not getattr(args, "no_socket", False),
        )
    finally:
        try:
            writer.flush()
        except Exception:
            pass
        try:
            os.close(protocol_in)
        except OSError:
            pass


# ── the first real client ────────────────────────────────────────────────────
#
# ``harness serve connect`` is the operator/agent lane onto the socket: it
# resolves the root's live service from the registry, performs the mandatory
# hello, and prints what it got as JSON. It exists for three reasons, in order
# of importance:
#
# 1. A transport with no client is a transport nobody has proven. This performs
#    the REAL handshake against the REAL auth token over the REAL socket.
# 2. ``--drain`` gives the durable service its restart verb from the outside —
#    the thing slice 2 could describe but could not exercise, because a drain
#    over stdio ends the only connection that could observe it.
# 3. It is the shape the Launcher's client will mirror when it migrates.

#: Nothing LIVE to connect to for this root — either no registered socket
#: service at all, or one the registry classified as anything other than
#: ``live``. Both are "do not connect", and the second is the more important
#: one: a serve's port outlives the serve, so a dead entry names an address
#: some other local process may now be answering on.
SERVE_CONNECT_NO_SERVICE_EXIT_CODE = 4
#: The service is there; this client could not authenticate to it.
SERVE_CONNECT_REJECTED_EXIT_CODE = 5
#: The connection itself failed (refused, timed out, died mid-handshake).
SERVE_CONNECT_TRANSPORT_EXIT_CODE = 6


def _cmd_serve_connect(args) -> int:
    from agent_runtime import paths
    from agent_runtime.build_stamp import build_stamp
    from agent_runtime.serve_auth import read_token
    from agent_runtime.serve_socket import (
        HELLO_CONTRACT_VERSION,
        ServeHelloProtocolError,
        ServeSocketClient,
        resolve_socket_target,
    )

    def _emit(payload: dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))

    store_root = paths.store_root()
    # ``allow_stale`` is asked for so the REFUSAL can name what it refused —
    # not so a non-live target can be used. Discovery itself returns live rows
    # only; this call is the diagnostic form, and the check below is the gate.
    target = resolve_socket_target(store_root, allow_stale=True)
    if target is None:
        _emit(
            {
                "ok": False,
                "error": "no_socket_service",
                "detail": (
                    "no live serve with a socket transport is registered for this "
                    "runtime root"
                ),
                "runtime_root": str(store_root),
            }
        )
        return SERVE_CONNECT_NO_SERVICE_EXIT_CODE
    if not target.live:
        # The half of the credential-disclosure defect that lives on the client.
        # A registry row classified ``stale_dead_pid`` names a port whose owner
        # is gone, and a local port is reusable the moment its owner dies — so
        # connecting here means handshaking with whatever took it over. That is
        # not a theory: with the old raw-token hello, an impostor listening on a
        # dead serve's port harvested the real token. The token no longer
        # travels, and this connect still refuses, by name.
        _emit(
            {
                "ok": False,
                "error": "socket_service_not_live",
                "classification": target.classification,
                "detail": (
                    "the only socket service registered for this runtime root is "
                    f"classified {target.classification!r}, not 'live'; its port may "
                    "now belong to another process, so this client will not "
                    "handshake with it"
                ),
                "runtime_root": str(store_root),
                "target": target.payload(),
            }
        )
        return SERVE_CONNECT_NO_SERVICE_EXIT_CODE
    token = read_token(store_root)
    if not token:
        # Fails CLOSED, and says which side is missing: a client with no token
        # cannot authenticate, and pretending otherwise would send a hello that
        # can only ever be rejected.
        _emit(
            {
                "ok": False,
                "error": "no_auth_token",
                "detail": "this runtime root has no serve auth token to present",
                "runtime_root": str(store_root),
                "target": target.payload(),
            }
        )
        return SERVE_CONNECT_REJECTED_EXIT_CODE
    client_build = build_stamp().commit
    report: dict[str, Any] = {
        "ok": False,
        "runtime_root": str(store_root),
        "target": target.payload(),
        "client": getattr(args, "client", None) or "harness-serve-connect",
        "client_build": client_build,
        # Which handshake this client speaks. Stated in the report because the
        # next client of this lane is the Launcher, and "which contract did the
        # thing that worked use" must not be archaeology.
        "hello_contract": HELLO_CONTRACT_VERSION,
    }
    timeout = float(getattr(args, "timeout", 10.0) or 10.0)
    connection = ServeSocketClient(target.host, target.port, timeout_seconds=timeout)
    try:
        connection.connect()
    except OSError as exc:
        report["error"] = "connect_failed"
        report["detail"] = type(exc).__name__
        _emit(report)
        return SERVE_CONNECT_TRANSPORT_EXIT_CODE
    try:
        try:
            hello = connection.hello(
                token=token, client=report["client"], client_build=client_build
            )
        except ServeHelloProtocolError as exc:
            # Either the peer refused us before the challenge (its typed reason
            # is the answer) or what is on this port does not speak this
            # contract. Neither is a case for sending a credential anyway.
            report["error"] = (
                "hello_rejected" if exc.reason else "hello_contract_mismatch"
            )
            report["detail"] = exc.detail
            report["reason"] = exc.reason
            report["hello"] = exc.frame
            _emit(report)
            return SERVE_CONNECT_REJECTED_EXIT_CODE
        report["server_hello"] = connection.server_hello
        report["hello"] = hello
        if not isinstance(hello, dict) or hello.get("event") != "hello_ok":
            report["error"] = (
                "hello_rejected" if isinstance(hello, dict) else "no_hello_reply"
            )
            _emit(report)
            return SERVE_CONNECT_REJECTED_EXIT_CODE
        if getattr(args, "probe", False):
            connection.send({"op": "version"})
            report["version"] = connection.read_frame()
        if getattr(args, "drain", False):
            deadline = getattr(args, "deadline_seconds", None)
            # ``force`` is mandatory on the socket lane — this verb IS the
            # deliberate operator restart, so it says so rather than being
            # refused by the service it is trying to replace.
            request: dict[str, Any] = {"op": "drain", "force": True}
            if deadline is not None:
                request["deadline_seconds"] = float(deadline)
            connection.send(request)
            # Read to the TERMINAL frame, not to the first one: the drain's
            # evidence (what it refused, what it completed) is on the terminal
            # frame, and a client that stopped at ``draining`` would report a
            # restart it never watched finish.
            observed: list[dict[str, Any]] = []
            terminal = {
                "drain_complete",
                "drain_timeout",
                "drain_abandoned",
                "drain_in_progress",
            }
            while True:
                frame = connection.read_frame()
                if frame is None:
                    break
                observed.append(frame)
                if frame.get("event") not in terminal:
                    continue
                if (
                    frame.get("event") == "drain_timeout"
                    and frame.get("terminal") is False
                ):
                    # A deadline lapse HELD OPEN by a chat turn in flight: the
                    # service is still serving and will re-arm, so this is
                    # progress, not an ending. Reading it as terminal would
                    # report a restart that has not happened.
                    continue
                break
            report["drain"] = observed
            report["drain_outcome"] = (
                observed[-1].get("event") if observed else "no_frames"
            )
            report["drain_deadline_holds"] = len(
                [
                    frame
                    for frame in observed
                    if frame.get("event") == "drain_timeout"
                    and frame.get("terminal") is False
                ]
            )
        report["ok"] = True
        _emit(report)
        return 0
    except OSError as exc:
        report["error"] = "transport_failed"
        report["detail"] = type(exc).__name__
        _emit(report)
        return SERVE_CONNECT_TRANSPORT_EXIT_CODE
    except ServeHelloProtocolError as exc:  # pragma: no cover - defensive
        report["error"] = "hello_contract_mismatch"
        report["detail"] = exc.detail
        _emit(report)
        return SERVE_CONNECT_REJECTED_EXIT_CODE
    finally:
        connection.close()
