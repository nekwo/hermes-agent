"""Unified ``running_work`` projection — what background work is running RIGHT NOW.

One aggregator, five lanes (terminal background processes, background subagent
delegations, in-flight mission-chat turns, detached agent-to-agent dispatches,
and cron jobs), producing ONE row vocabulary so an operator surface never has
to know which subsystem spawned a piece of work.

Connected MCP transports are deliberately NOT a lane. A warm connection is
capability infrastructure, not work: it can outlive the agent turn that admitted
it, can be reused by several persona instances, and has no single truthful
owner. Active MCP calls already travel with the owning chat turn's tool trace;
counting the connection itself made an idle runtime claim background motion.

Durable-first, and that is the whole architecture
-------------------------------------------------
The CLI snapshot lane, the serve process, and the stream are SEPARATE
PROCESSES. Every in-memory registry in this runtime (``process_registry``,
``async_delegation._records``, the cron scheduler's running-id set) lives only
inside the process that spawned the work. A
projection built from those registries alone would report an empty HUD from any
other lane and call it "nothing running" — the exact silent-lie class this
module exists to retire.

So each lane declares a DURABLE view (readable from anywhere: the
``processes.json`` checkpoint, the ``async_delegations`` table, the mission-chat
turn journal) and an optional LIVE enrichment.

Residency is not ownership — this cost a design iteration and is worth stating
plainly. The obvious liveness test, "is ``tools.process_registry`` in
``sys.modules``?", is WRONG here: ``agent_runtime.mission_chat_turns`` already
drags ``tools.process_registry``, ``tools.async_delegation`` and
``cron.scheduler`` into any process that reads the turn journal, so residency is
true almost everywhere and proves nothing about who owns the work. What each
registry can honestly report is therefore split in two:

* **Durable-backed lanes** (terminal, delegation) merge live rows when the
  registry HAS any. A registry in a non-owning process returns an empty list,
  which enriches nothing and drops nothing, and the lane keeps reporting
  ``durable`` — it only claims ``live`` once live rows were actually observed.
* **Live-only lanes** (cron jobs) have no durable record at all, so they apply
  the rule *presence proves, absence proves nothing*: this process is the owner
  only if it holds cron pool/running-id state. Without that proof the lane
  reports ``unavailable``, because an empty
  ``get_running_job_ids()`` in a non-scheduler process is indistinguishable from
  "no cron jobs are running anywhere" and rendering it as the latter is exactly
  the silent lie this projection exists to retire.

``sys.modules.get(...)`` is still used as the cheap first gate — never
``import_module`` — so that a lane this process genuinely knows nothing about
costs nothing to skip, and so a read-only projection never constructs a tool
singleton as a side effect of being asked a question.

Four honesty rules, all prior operator rulings — do not relax
------------------------------------------------------------
1. **Fail-closed per source.** Every lane contributes a ``sources`` entry
   (``ok`` / ``unavailable`` + reason). A lane that cannot be read is REPORTED
   unavailable; it never degrades into a silently empty row list, because
   "nothing is running" and "I could not look" are different facts and an
   operator must be able to tell them apart.
2. **PID honesty.** A stored PID is not proof of life: the kernel recycles PID
   numbers, and this repo has already been bitten by a recycled number landing
   on an unrelated desktop process. Identity is verified against the
   ``host_start_time`` captured at spawn (the same guard
   ``process_registry._host_pid_is_ours`` applies before it will signal
   anything). A row whose identity cannot be PROVEN carries
   ``pid_verified: false`` and status ``unknown`` — never ``running``. A row
   whose identity is actively DISPROVEN (dead PID, or a recycled number) is not
   running work at all and is dropped through the accountant, never silently.
3. **Liveness honesty.** ``running`` means progress was observed, not that a
   record exists. Delegations carry the stale monitor's progress token, so a
   wedged child surfaces as ``stalled`` even before the 30s monitor sweep
   notices. Where no progress signal is readable (durable lane), ``progress``
   declares itself ``unavailable`` rather than reporting zeros that read as
   "idle".
4. **Bounded previews by design.** ``tail_preview`` is capped at
   :data:`TAIL_PREVIEW_LIMIT` characters and the truncation is declared
   ``by_design`` in the parity accounting, so a deliberate bound never trips the
   amber "lost data" pill.

Path resolution and the HERMES_HOME flip
----------------------------------------
``persona_profile_context`` flips ``HERMES_HOME`` PROCESS-GLOBALLY for the
duration of a persona turn, and persona turns run inside the serve process —
the same process that builds snapshots on another thread. Resolving
``processes.json`` / ``state.db`` through ambient ``get_hermes_home()`` would
therefore read a different profile's files depending on what was mid-turn at
the instant the projection ran. Both durable stores resolve through the
head-home scope (:func:`agent_runtime.chat_session_scope.resolve_chat_session_scope`),
the same durable pointer the chat lane uses, with ambient home as a declared
fallback only when that resolver itself fails.

Contract is wire; ambient is context — and they never share a string
--------------------------------------------------------------------
A source entry's ``status`` / ``lane`` / ``reason`` are CONTRACT: a consumer
branches on them, and this module owes them a stable vocabulary. Everything
else a lane could say about itself is AMBIENT — which directory the projection
resolved, what the filesystem under it happens to contain — machine-local
context, not a fact about work.

The first implementation concatenated the two into one ``detail`` prose string
(``"head_home=base; no state.db"``), and that mixing did not merely read badly:
it made the producer NON-DETERMINISTIC. Concretely, ``_collect_delegations``
runs before ``_collect_chat_turns`` and reported whether ``state.db`` existed;
the chat-turn lane's lazy ``from . import mission_chat_turns`` drags
``model_tools`` → ``tools.registry.discover_builtin_tools()`` →
``tools.process_registry``'s module-scope singleton →
``async_delegation.restore_undelivered_completions()``, which CREATES that very
``state.db``. So the FIRST build in a process said ``"; no state.db"`` and every
later build did not, for identical work and an identical contract — and under
pytest which answer you got was decided by whether some other test module had
already dragged that import chain in. A producer whose output depends on hidden
import state cannot be pinned honestly by anyone, which is why the mixing is the
bug rather than the prose.

The split this module now keeps:

* **Contract, on each source entry.** ``status``, ``lane``, ``reason``, the
  bounded ``detail`` an UNAVAILABLE lane attaches (already a bare machine token
  — an exception class name — never prose), and ``live_enrichment_error``: the
  typed exception class name recorded when a live enrichment raised while the
  durable answer survived. Absence of the last is unambiguous — the enrichment
  did not raise — which is why it is a bare token rather than a nullable block.
* **Ambient, in ONE clearly-named block.** :func:`_ambient_context`, published
  as ``ambient`` beside ``rows`` / ``sources`` / ``counts``. It names the
  resolved background-work home — provenance plus BASENAME only, because the
  error contract forbids absolute paths in operator messages — so "0 processes"
  can still be told apart from "0 processes *in the home I happened to
  resolve*", the diagnostic the writer/reader divergence above cost an entire
  build to learn. **No consumer is expected to depend on it, and nothing in
  this module branches on it.**
* **Off the wire entirely: store PRESENCE.** ``"no checkpoint file"``,
  ``"no state.db"`` and ``"no async_delegations table"`` were observations about
  storage LAYOUT, not about work — a durable lane answering ``ok`` with zero
  rows out of a named home is already the complete honest answer — and two of
  the three were perturbed by the import chain above, so they could never have
  been reported honestly at all.

This module is READ-ONLY with respect to every store it touches: it opens no
database it would have to create, marks no output consumed, and takes no lock a
writer could be waiting on. That invariant holds for every store this file reads
DIRECTLY. It does NOT survive ``_collect_chat_turns``'s import chain, which
reaches a tool singleton whose constructor runs delegation recovery — a
structural weakness in ``model_tools``' module-scope
``discover_builtin_tools()`` call, filed rather than fixed here — the full
execution-verified audit and retirement plan live in
``docs/agent-runtime-harness/eager-tool-discovery-audit-2026-08-09.md``.
Nothing this projection PUBLISHES depends on that side effect any more, which
is what makes the wire deterministic in spite of it.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .parity import ProjectionAccountant
from .redaction import TEXT_SECRET_ASSIGNMENT_RE

__all__ = [
    "KIND_CHAT_TURN",
    "KIND_CRON_JOB",
    "KIND_DELEGATION",
    "KIND_DISPATCH",
    "KIND_TERMINAL",
    "PEEK_TAIL_LIMIT",
    "PROJECTION",
    "RUNNING_WORK_KINDS",
    "RUNNING_WORK_SOURCES",
    "STATUS_VALUES",
    "TAIL_PREVIEW_LIMIT",
    "build_running_work",
    "cancel_work",
    "find_work_row",
    "peek_work",
    "running_work_store_paths",
    "split_work_id",
]

PROJECTION = "running_work"

#: Hard cap on the inline output preview carried on every row. Declared
#: ``by_design`` through the accountant whenever it actually truncates.
TAIL_PREVIEW_LIMIT = 200
#: Hard cap on the ``work peek`` tail. Deliberately small: the peek exists so an
#: operator can see what a process is doing without flooding an agent's context.
PEEK_TAIL_LIMIT = 2048

KIND_TERMINAL = "terminal"
KIND_DELEGATION = "delegation"
KIND_CHAT_TURN = "chat_turn"
KIND_CRON_JOB = "cron_job"
#: Detached agent-to-agent dispatches (``agent_chat_send(wait=false)``), backed
#: by :mod:`agent_runtime.dispatch_store`. Declared in this tuple one wave
#: BEFORE its producer existed, so the wire vocabulary was complete from the
#: first landing and no consumer had to re-derive it when the lane arrived.
KIND_DISPATCH = "dispatch"

#: How far back an UNDELIVERABLE dispatch keeps surfacing on the Activity
#: projection. A day, because "your agent's answer was thrown away" is worth
#: seeing on the next session start, and not forever, because the HUD reports
#: what the machine is doing now rather than serving as an incident archive.
#:
#: BOUND, STATED: past this window the row goes silent here with no "N older"
#: affordance. That is a deliberate limit of THIS surface, not a claim that
#: nothing older happened — the drop is still in the EventLog
#: (``dispatch.dropped``), still on the store row, and still reported to the
#: dispatching agent through ``agent_chat_dispatches``. A counted-older
#: affordance belongs here eventually; until it exists, a reader must not treat
#: an empty dispatch lane as "nothing was ever abandoned".
_UNDELIVERABLE_WINDOW_SECONDS = 24 * 60 * 60

RUNNING_WORK_KINDS = (
    KIND_TERMINAL,
    KIND_DELEGATION,
    KIND_CHAT_TURN,
    KIND_CRON_JOB,
    KIND_DISPATCH,
)

#: The lanes that report a ``sources`` entry on every build. ``dispatch`` joined
#: at WP-H2, when it acquired a producer (:mod:`agent_runtime.dispatch_store`);
#: until then declaring a source for a lane nothing could write would have been
#: the dead-by-emptiness the parity rules forbid.
RUNNING_WORK_SOURCES = (
    KIND_TERMINAL,
    KIND_DELEGATION,
    KIND_CHAT_TURN,
    KIND_CRON_JOB,
    KIND_DISPATCH,
)

STATUS_RUNNING = "running"
STATUS_STALLING = "stalling"
STATUS_STALLED = "stalled"
STATUS_FINALIZING = "finalizing"
STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"

STATUS_VALUES = (
    STATUS_RUNNING,
    STATUS_STALLING,
    STATUS_STALLED,
    STATUS_FINALIZING,
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_UNKNOWN,
)

SOURCE_OK = "ok"
SOURCE_UNAVAILABLE = "unavailable"

LANE_LIVE = "live"
LANE_DURABLE = "durable"

#: Reason a lane reports ``unavailable`` because its truth is a process-global
#: that only the owning process can answer.
REASON_NOT_IN_PROCESS = "not_in_process"

#: Fallback stale thresholds, used ONLY when ``tools.async_delegation`` is not
#: resident (i.e. the durable lane, where no progress token exists anyway and
#: these are therefore unreachable). When the module IS resident its constants
#: are read directly so this file never becomes a second authority for them.
_FALLBACK_STALE_IDLE_SECONDS = 450.0
_FALLBACK_STALE_IN_TOOL_SECONDS = 1200.0

#: Per-lane row cap. A runaway subsystem must not be able to inflate the
#: snapshot; overflow is accounted (``lane_capped``, by design) rather than
#: dropped in silence.
_MAX_ROWS_PER_SOURCE = 200

_CHECKPOINT_FILENAME = "processes.json"
_STATE_DB_FILENAME = "state.db"


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------


def _safe_text(value: Any, *, limit: int) -> str:
    """Whitespace-collapsed, secret-masked, length-bounded operator text.

    Mirrors ``snapshot._safe_text``'s ruling: repo paths are the CONTENT on an
    operator console, not a leak, so only secret-shaped assignments are masked —
    and they are masked in place rather than dropping the whole string.
    """

    if value is None:
        return ""
    text = " ".join(str(value).split())
    if not text:
        return ""
    text = TEXT_SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}: [redacted]", text)
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _iso(epoch: Any) -> str:
    """UTC ISO-8601 for an epoch-seconds value; empty string when unusable."""

    try:
        seconds = float(epoch)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _parse_iso(text: Any) -> float | None:
    """Epoch seconds for an ISO-8601 stamp, or None when unparseable.

    Accepts the ``Z`` suffix the turn journal writes. A naive stamp is read as
    UTC — the journal writes UTC — rather than as local time, which would make
    ``elapsed_seconds`` jump by the machine's offset.
    """

    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso_from_naive_local(text: Any) -> str:
    """UTC ISO-8601 for the naive LOCAL stamp the process registry emits.

    ``process_registry.list_sessions`` formats ``started_at`` with
    ``time.localtime`` and no offset, so the string is unanchored: two rows on
    one wire would carry stamps in different frames of reference depending on
    which lane produced them, and any consumer parsing them (the launcher's
    elapsed clock, this module's own ``--issued-at`` replay guard) would be off
    by the machine's UTC offset without anything looking wrong.

    Normalizing HERE rather than in ``process_registry`` is deliberate: that
    function has other consumers (the ``process`` tool's ``list`` action, the
    goal-loop judge) whose displayed output would change under them. The
    projection is the boundary that publishes a wire contract, so the
    projection is where the stamp gets anchored.

    Already-anchored input is passed through, and anything unparseable is
    returned empty rather than guessed at.
    """

    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        # `astimezone()` on a naive datetime interprets it as local time, which
        # is exactly what `time.localtime` produced.
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat()


def _elapsed(started_epoch: float | None, *, now: float) -> int:
    if started_epoch is None:
        return 0
    return int(max(0.0, now - started_epoch))


def _module(name: str):
    """The already-imported module, or None.

    Deliberately ``sys.modules.get`` and never ``importlib.import_module``: a
    read-only projection must not pay an import cost — or, for
    ``tools.process_registry``, construct a tool singleton — as a side effect of
    being asked a question.

    Residency is a NECESSARY condition for a live answer, never a sufficient
    one: see the module docstring. Callers must prove ownership separately.
    """

    return sys.modules.get(name)


def _head_home() -> tuple[Path | None, str]:
    """The durable home the background stores live under, plus its provenance.

    This MUST resolve to the directory the WRITERS use, and now it does: this
    function, ``process_registry.checkpoint_path`` and
    ``async_delegation._db_path`` all call the same
    ``get_hermes_background_work_home`` authority.

    The first version resolved the CHAT head-home scope instead. That scope
    consults a durable ``chat_head_home.json`` pointer the writers know nothing
    about, so wherever the pointer disagreed with the writers' ambient home the
    projection watched one directory while the work was recorded in another —
    and reported "nothing running", with every source ``ok``, for the entire
    life of a build. A confident empty answer is the worst failure this
    projection can produce, and it was reachable on the launcher's own layout
    (``HERMES_HOME=profiles/<profile>`` beside ``HERMES_HEAD_HOME=profiles/base``).

    Ambient ``get_hermes_home()`` is not an option either: it is flipped
    process-globally for the duration of a persona turn, and persona turns share
    a process with snapshot builds. The head authority is correct in both
    situations precisely because ``persona_profile_context`` records the
    operator home BEFORE it diverts the ambient one.
    """

    try:
        from hermes_constants import (
            get_hermes_background_work_home,
            hermes_head_home_is_authoritative,
        )

        home = Path(get_hermes_background_work_home())
    except Exception:
        return None, "unresolved"
    try:
        explicit = bool(hermes_head_home_is_authoritative())
    except Exception:  # pragma: no cover - defensive
        explicit = False
    return home, "head_home" if explicit else "ambient_home"


def running_work_store_paths() -> tuple[Path, ...]:
    """The durable stores this projection reads — the ONE authority for them.

    Exported because the serve read-model cache must fingerprint exactly these
    files: both mutate with NO EventLog event (a background process starting or
    exiting rewrites the checkpoint; a delegation dispatch/finalize writes
    ``async_delegations``), so without a stat the 20s cache would keep serving a
    HUD claiming three processes are running for twenty seconds after they all
    exited — and, worse, claiming nothing is running for twenty seconds after an
    agent kicked off a long build.

    Returns an empty tuple when the home cannot be resolved; the caller must
    treat that as "cannot fingerprint" rather than "nothing to watch".
    """

    head, _ = _head_home()
    if head is None:
        return ()
    return (head / _CHECKPOINT_FILENAME, head / _STATE_DB_FILENAME)


#: ``_pid_identity`` verdicts. The split between the last two matters more than
#: it looks: "a different process holds this number" and "I could not read this
#: number's start time" lead to OPPOSITE actions. The first is proof the work is
#: gone (drop the row); the second is an absence of proof (keep the row, admit
#: it is unverified). Collapsing them — which the first implementation did —
#: means a psutil hiccup or an access-denied probe silently deletes a running
#: build from the operator's HUD and files it under "recycled PID".
PID_VERIFIED = "verified"
PID_DEAD = "dead"
PID_RECYCLED = "recycled"
PID_NO_BASELINE = "no_baseline"
PID_START_TIME_UNREADABLE = "start_time_unreadable"


def _pid_identity(pid: Any, expected_start: Any) -> tuple[bool, bool, str]:
    """``(alive, verified, verdict)`` for a host PID against its spawn ticks.

    ``alive`` is a bare existence probe; ``verified`` additionally proves the
    number was not recycled onto an unrelated process. ``verdict`` is one of the
    ``PID_*`` constants and is what the caller must branch on — only
    :data:`PID_DEAD` and :data:`PID_RECYCLED` are DISPROOF that the work is
    running. :data:`PID_NO_BASELINE` and :data:`PID_START_TIME_UNREADABLE` are
    both merely unproven, and must surface as an ``unknown`` row.
    """

    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False, False, PID_DEAD
    if pid_int <= 0:
        return False, False, PID_DEAD
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        # Without the probe we cannot even test liveness. Refusing to claim
        # anything is the honest answer, and the row keeps its unknown status
        # rather than being reported dead.
        return True, False, PID_START_TIME_UNREADABLE
    try:
        alive = bool(_pid_exists(pid_int))
    except Exception:
        return True, False, PID_START_TIME_UNREADABLE
    if not alive:
        return False, False, PID_DEAD
    if expected_start is None:
        return True, False, PID_NO_BASELINE
    try:
        observed = get_process_start_time(pid_int)
    except Exception:
        return True, False, PID_START_TIME_UNREADABLE
    if observed is None:
        # The platform/permissions could not answer. NOT a mismatch: comparing
        # None against a real baseline yields False, which is how an unreadable
        # probe used to masquerade as a recycled PID.
        return True, False, PID_START_TIME_UNREADABLE
    try:
        matches = int(observed) == int(expected_start)
    except (TypeError, ValueError):
        return True, False, PID_START_TIME_UNREADABLE
    return True, matches, PID_VERIFIED if matches else PID_RECYCLED


def _stale_thresholds() -> tuple[float, float]:
    """``(idle, in_tool)`` stale seconds, read from the ONE authority that owns them.

    ``tools.async_delegation`` defines these for its own stale monitor. Reading
    them off the resident module keeps this projection's staleness verdict
    identical to the monitor's instead of standing up a second set of numbers
    that could silently drift apart.
    """

    mod = _module("tools.async_delegation")
    if mod is None:
        return _FALLBACK_STALE_IDLE_SECONDS, _FALLBACK_STALE_IN_TOOL_SECONDS
    return (
        float(getattr(mod, "_STALE_IDLE_SECONDS", _FALLBACK_STALE_IDLE_SECONDS)),
        float(getattr(mod, "_STALE_IN_TOOL_SECONDS", _FALLBACK_STALE_IN_TOOL_SECONDS)),
    )


def _progress(
    *,
    api_calls: Any = None,
    in_tool: Any = None,
    seconds_since_progress: Any = None,
    available: bool,
) -> dict[str, Any]:
    """The row's progress block.

    ``available: False`` emits explicit nulls plus ``source: "unavailable"``
    rather than zeros — a zero here reads as "idle right now", which is a
    different (and unproven) claim from "this lane cannot see progress".
    """

    if not available:
        return {
            "api_calls": None,
            "in_tool": None,
            "seconds_since_progress": None,
            "source": SOURCE_UNAVAILABLE,
        }
    return {
        "api_calls": int(api_calls) if isinstance(api_calls, (int, float)) else None,
        "in_tool": in_tool if isinstance(in_tool, str) and in_tool else bool(in_tool) if in_tool is not None else None,
        "seconds_since_progress": (
            round(float(seconds_since_progress), 1)
            if isinstance(seconds_since_progress, (int, float))
            else None
        ),
        "source": SOURCE_OK,
    }


def _row(
    *,
    kind: str,
    stable_id: str,
    label: str,
    status: str,
    source_lane: str,
    command: str = "",
    pid: Any = None,
    pid_verified: bool = False,
    persona_id: str = "",
    persona_instance_id: str = "",
    session_id: str = "",
    started_at: str = "",
    elapsed_seconds: int = 0,
    progress: dict[str, Any] | None = None,
    tail_preview: str = "",
    cancellable: bool = False,
) -> dict[str, Any]:
    return {
        "work_id": f"{kind}:{stable_id}",
        "kind": kind,
        "label": label,
        "command": command,
        "pid": int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else None,
        "pid_verified": bool(pid_verified),
        "owner": {
            "persona_id": persona_id or None,
            "persona_instance_id": persona_instance_id or None,
            "session_id": session_id or None,
        },
        "status": status,
        "started_at": started_at,
        "elapsed_seconds": int(max(0, elapsed_seconds)),
        "progress": progress if progress is not None else _progress(available=False),
        "tail_preview": tail_preview,
        "source_lane": source_lane,
        "cancellable": bool(cancellable),
    }


def _preview(text: Any, accountant: ProjectionAccountant | None) -> str:
    """Bounded, secret-masked inline preview; truncation declared by design."""

    raw = str(text or "")
    if not raw:
        return ""
    stripped = _strip_ansi(raw)
    bounded = _safe_text(stripped, limit=TAIL_PREVIEW_LIMIT)
    if accountant is not None and len(" ".join(stripped.split())) > TAIL_PREVIEW_LIMIT:
        accountant.drop(
            "tail_truncated",
            detail=f"preview bounded to {TAIL_PREVIEW_LIMIT} chars",
            by_design=True,
        )
    return bounded


def _strip_ansi(text: str) -> str:
    """ANSI-strip through the repo's one implementation when it is resident.

    Terminal output previews come from a PTY, so escape sequences are the norm
    rather than the exception. The stripper lives beside the process registry;
    when that tree is not loaded (durable lane) the checkpoint carries no output
    anyway, so the untouched text is returned rather than importing a tool
    module into a projection.
    """

    try:
        from tools.ansi_strip import strip_ansi
    except Exception:
        return text
    try:
        return strip_ansi(text)
    except Exception:
        return text


def _source(
    status: str,
    *,
    lane: str = "",
    reason: str = "",
    detail: str = "",
    live_enrichment_error: str = "",
) -> dict[str, Any]:
    """One lane's CONTRACT health entry. Ambient facts do not belong here.

    ``detail`` is reserved for the bounded machine token an UNAVAILABLE lane
    attaches (an exception class name) — never prose, and never anything the
    filesystem or the environment decides. Machine-local context rides
    :func:`_ambient_context` instead, where no consumer is asked to parse it
    back out of a sentence.

    ``live_enrichment_error`` is the typed counterpart of the old
    ``"; live enrichment failed: TypeError"`` suffix: a lane whose durable
    answer stands but whose live enrichment raised is still ``ok``, and the
    reader must be able to see the difference without sentence-matching.
    """

    entry: dict[str, Any] = {"status": status}
    if lane:
        entry["lane"] = lane
    if reason:
        entry["reason"] = reason
    if detail:
        entry["detail"] = _safe_text(detail, limit=240)
    if live_enrichment_error:
        entry["live_enrichment_error"] = _safe_text(live_enrichment_error, limit=80)
    return entry


def _ambient_context() -> dict[str, str]:
    """Machine-local context for this build. **NOT contract — do not branch on it.**

    Names the background-work home the projection resolved, so an operator can
    tell "nothing is running" apart from "nothing is running *in the directory I
    happened to resolve*" — the exact silent-empty failure
    ``get_hermes_background_work_home`` was introduced to retire.

    Deliberately a BASENAME plus a provenance token, not a path: the error
    contract forbids absolute paths in operator-visible messages, and the
    question this answers ("which home did you mean?") is answered by the name.

    Deliberately ONE block rather than a per-lane field, because it is ONE
    fact: both durable lanes resolve the same home through the same authority,
    and duplicating it into two lanes' prose is how it got concatenated onto a
    contract field in the first place.

    Nothing in this module reads this back. It is published, never consulted.
    """

    head, provenance = _head_home()
    return {
        "home_provenance": provenance,
        "home_name": head.name if head is not None else "",
    }


def _cap(
    rows: list[dict[str, Any]],
    *,
    source: str,
    accountant: ProjectionAccountant | None,
) -> list[dict[str, Any]]:
    if len(rows) <= _MAX_ROWS_PER_SOURCE:
        return rows
    if accountant is not None:
        accountant.drop(
            "lane_capped",
            count=len(rows) - _MAX_ROWS_PER_SOURCE,
            entity_id=source,
            detail=f"{source} lane bounded to {_MAX_ROWS_PER_SOURCE} rows",
            by_design=True,
        )
        accountant.mark_truncated()
    return rows[:_MAX_ROWS_PER_SOURCE]


# --------------------------------------------------------------------------
# lane: terminal background processes
# --------------------------------------------------------------------------


def _collect_terminal(
    *, now: float, accountant: ProjectionAccountant | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Durable ``processes.json`` checkpoint, enriched from the live registry.

    The checkpoint is written atomically on every start/exit and holds
    running-only entries with the ``host_start_time`` identity baseline, so it
    is the one lane that answers honestly from a cold CLI process.
    """

    rows: dict[str, dict[str, Any]] = {}
    head, _provenance = _head_home()
    if head is None:
        return [], _source(
            SOURCE_UNAVAILABLE, lane=LANE_DURABLE, reason="home_unresolved"
        )
    # Which home this resolved to rides `_ambient_context()`, once, for the whole
    # projection — it is machine-local context, not this lane's health.

    path = head / _CHECKPOINT_FILENAME
    try:
        if path.exists():
            entries = json.loads(path.read_text(encoding="utf-8"))
        else:
            # An absent checkpoint and a checkpoint listing nothing are the SAME
            # runtime fact — zero background processes, proven — so the lane says
            # `ok` with zero rows either way. Which of the two it was is storage
            # layout, and reporting it here is what used to put a filesystem
            # observation on a contract field.
            entries = []
    except Exception as exc:
        return [], _source(
            SOURCE_UNAVAILABLE,
            lane=LANE_DURABLE,
            reason="checkpoint_unreadable",
            detail=f"{type(exc).__name__}",
        )

    if not isinstance(entries, list):
        return [], _source(
            SOURCE_UNAVAILABLE, lane=LANE_DURABLE, reason="checkpoint_malformed"
        )

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        session_id = _safe_text(entry.get("session_id"), limit=200)
        if not session_id:
            continue
        if accountant is not None:
            accountant.consider()
        pid = entry.get("pid")
        _alive, verified, verdict = _pid_identity(pid, entry.get("host_start_time"))
        if verdict == PID_DEAD:
            # The checkpoint says running; the kernel says the PID is gone.
            # A process that exited is not running work, and reporting it would
            # be exactly the liveness lie this projection exists to prevent.
            # Checkpoint lag between an exit and the next atomic write is the
            # normal steady state, so this is a bound, not lost data.
            if accountant is not None:
                accountant.drop(
                    "process_exited",
                    entity_id=session_id,
                    detail="checkpoint row's PID no longer exists",
                    by_design=True,
                )
            continue
        if verdict == PID_RECYCLED:
            # Alive, but a DIFFERENT process now holds the number. Our work is
            # gone and the live process is a stranger we must never signal.
            # Reached ONLY on a real start-time mismatch — an unreadable probe
            # falls through below as unverified instead of being deleted here.
            if accountant is not None:
                accountant.drop(
                    "pid_recycled",
                    entity_id=session_id,
                    detail="host_start_time mismatch; PID was recycled",
                    by_design=True,
                )
            continue
        started = entry.get("started_at")
        command = _safe_text(entry.get("command"), limit=400)
        row = _row(
            kind=KIND_TERMINAL,
            stable_id=session_id,
            label=_safe_text(entry.get("command"), limit=120) or session_id,
            command=command,
            status=STATUS_RUNNING if verified else STATUS_UNKNOWN,
            source_lane=LANE_DURABLE,
            pid=pid,
            pid_verified=verified,
            session_id=_safe_text(entry.get("session_key"), limit=200),
            started_at=_iso(started),
            elapsed_seconds=_elapsed(
                float(started) if isinstance(started, (int, float)) else None, now=now
            ),
            progress=_progress(available=False),
            cancellable=True,
        )
        rows[row["work_id"]] = row

    registry = _module("tools.process_registry")
    lane = LANE_DURABLE
    live_error = ""
    if registry is not None:
        try:
            sessions = registry.process_registry.list_sessions()
            # The lane is only "live" once the registry actually produced rows.
            # A resident-but-non-owning registry returns [], and labelling that
            # ``live`` would claim first-hand knowledge this process does not
            # have.
            if sessions:
                lane = LANE_LIVE
            for item in sessions:
                if not isinstance(item, dict):
                    continue
                session_id = _safe_text(item.get("session_id"), limit=200)
                if not session_id:
                    continue
                work_id = f"{KIND_TERMINAL}:{session_id}"
                if str(item.get("status") or "") == "exited":
                    # The live registry is authoritative about its own children:
                    # an exited one is not running work, and it must also not
                    # survive as a stale durable row from a checkpoint written
                    # before the exit.
                    if rows.pop(work_id, None) is not None and accountant is not None:
                        accountant.drop(
                            "process_exited",
                            entity_id=session_id,
                            detail="live registry reports exited",
                            by_design=True,
                        )
                    continue
                existing = rows.get(work_id)
                if existing is None:
                    if accountant is not None:
                        accountant.consider()
                    existing = _row(
                        kind=KIND_TERMINAL,
                        stable_id=session_id,
                        label=_safe_text(item.get("command"), limit=120) or session_id,
                        command=_safe_text(item.get("command"), limit=400),
                        status=STATUS_RUNNING,
                        source_lane=LANE_LIVE,
                        pid=item.get("pid"),
                        # In-process ownership IS the identity proof: this
                        # registry holds the handle it spawned, so no
                        # start-time comparison is needed or possible.
                        pid_verified=True,
                        # The registry formats this with `time.localtime` and no
                        # offset; every `started_at` on this wire is UTC.
                        started_at=_iso_from_naive_local(item.get("started_at")),
                        elapsed_seconds=int(item.get("uptime_seconds") or 0),
                        cancellable=True,
                    )
                    rows[work_id] = existing
                else:
                    existing["source_lane"] = LANE_LIVE
                    existing["pid_verified"] = True
                    existing["status"] = STATUS_RUNNING
                    if item.get("uptime_seconds"):
                        existing["elapsed_seconds"] = int(item["uptime_seconds"])
                existing["tail_preview"] = _preview(item.get("output_preview"), accountant)
        except Exception as exc:
            live_error = type(exc).__name__

    ordered = sorted(rows.values(), key=lambda item: (item.get("started_at") or "", item["work_id"]))
    capped = _cap(ordered, source=KIND_TERMINAL, accountant=accountant)
    if accountant is not None:
        # Count what actually ships. Including pre-cap would report rows the
        # frame does not carry, and `considered - dropped` would stop reconciling.
        accountant.include(len(capped))
    return (
        capped,
        _source(SOURCE_OK, lane=lane, live_enrichment_error=live_error),
    )


# --------------------------------------------------------------------------
# lane: background subagent delegations
# --------------------------------------------------------------------------


def _delegation_status(
    record: dict[str, Any], *, idle_stale: float, in_tool_stale: float
) -> str:
    """Map a delegation record onto the shared status vocabulary.

    The stale monitor sweeps every 30s, so a child can be frozen well past the
    threshold while its record still reads ``running``. Deriving the verdict
    from the progress token here — the same token and the same thresholds the
    monitor uses — means the projection never shows ``running`` for something
    that demonstrably stopped progressing.
    """

    raw = str(record.get("status") or record.get("state") or "").strip().lower()
    if raw in {"stalled"}:
        return STATUS_STALLED
    if raw in {"stalling"}:
        return STATUS_STALLING
    if raw in {"finalizing"}:
        return STATUS_FINALIZING
    if raw in {"completed", "ok", "success"}:
        return STATUS_COMPLETED
    if raw in {"error", "failed", "interrupted"}:
        return STATUS_ERROR
    if raw != "running":
        return STATUS_UNKNOWN

    quiet = record.get("seconds_since_progress")
    if isinstance(quiet, (int, float)):
        threshold = in_tool_stale if record.get("in_tool") else idle_stale
        if float(quiet) >= threshold:
            return STATUS_STALLED
    return STATUS_RUNNING


def _collect_delegations(
    *, now: float, accountant: ProjectionAccountant | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Durable ``async_delegations`` rows, enriched from the live record map.

    The durable read opens the database only when the file already exists and
    never runs DDL: this projection must not be the thing that CREATES a
    ``state.db`` (or upgrades its schema) as a side effect of an operator
    reading a HUD.
    """

    rows: dict[str, dict[str, Any]] = {}
    idle_stale, in_tool_stale = _stale_thresholds()
    head, _provenance = _head_home()
    if head is None:
        return [], _source(SOURCE_UNAVAILABLE, lane=LANE_DURABLE, reason="home_unresolved")

    db_path = head / _STATE_DB_FILENAME
    # Whether `state.db` exists yet is deliberately NOT reported. It is a
    # filesystem observation rather than a fact about work, and — uniquely
    # corrosive — it is one this very projection perturbs: the chat-turn lane
    # that runs after this one imports a chain that CREATES the file, so build 1
    # and build 2 of the same process would disagree about it. See the module
    # docstring.
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        except Exception as exc:
            return [], _source(
                SOURCE_UNAVAILABLE,
                lane=LANE_DURABLE,
                reason="store_unreadable",
                detail=type(exc).__name__,
            )
        try:
            cursor = conn.execute(
                """SELECT delegation_id, origin_session, parent_session_id, state,
                          dispatched_at, owner_pid, owner_started_at, task_json
                   FROM async_delegations
                   WHERE state IN ('running','finalizing')"""
            )
            fetched = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            # A state.db that predates the delegation table is a store with no
            # delegations, not an unreadable store.
            if "no such table" not in str(exc).lower():
                conn.close()
                return [], _source(
                    SOURCE_UNAVAILABLE,
                    lane=LANE_DURABLE,
                    reason="store_unreadable",
                    detail=type(exc).__name__,
                )
            # A store that predates the table holds zero delegations, which is
            # the same answer as a table with no rows. Same ruling as the absent
            # checkpoint above: schema layout is not lane health.
            fetched = []
        except Exception as exc:
            conn.close()
            return [], _source(
                SOURCE_UNAVAILABLE,
                lane=LANE_DURABLE,
                reason="store_unreadable",
                detail=type(exc).__name__,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

        for row in fetched:
            (
                delegation_id,
                origin_session,
                parent_session_id,
                state,
                dispatched_at,
                owner_pid,
                owner_started_at,
                task_json,
            ) = row
            delegation_id = _safe_text(delegation_id, limit=200)
            if not delegation_id:
                continue
            if accountant is not None:
                accountant.consider()
            _alive, verified, verdict = _pid_identity(owner_pid, owner_started_at)
            if verdict == PID_DEAD:
                # The owning process is gone. ``recover_abandoned_delegations``
                # is the authority that reclassifies these; reporting them as
                # running here would contradict it.
                if accountant is not None:
                    accountant.drop(
                        "owner_exited",
                        entity_id=delegation_id,
                        detail="delegation owner process no longer exists",
                        by_design=True,
                    )
                continue
            if verdict == PID_RECYCLED:
                if accountant is not None:
                    accountant.drop(
                        "pid_recycled",
                        entity_id=delegation_id,
                        detail="owner PID was recycled",
                        by_design=True,
                    )
                continue
            try:
                task = json.loads(task_json or "{}")
            except Exception:
                task = {}
            goal = task.get("goal") if isinstance(task, dict) else ""
            status = (
                STATUS_FINALIZING
                if str(state or "") == "finalizing"
                else (STATUS_RUNNING if verified else STATUS_UNKNOWN)
            )
            work_row = _row(
                kind=KIND_DELEGATION,
                stable_id=delegation_id,
                label=_safe_text(goal, limit=160) or delegation_id,
                status=status,
                source_lane=LANE_DURABLE,
                pid=owner_pid,
                pid_verified=verified,
                session_id=_safe_text(parent_session_id or origin_session, limit=200),
                started_at=_iso(dispatched_at),
                elapsed_seconds=_elapsed(
                    float(dispatched_at) if isinstance(dispatched_at, (int, float)) else None,
                    now=now,
                ),
                # No progress token survives a process boundary — say so rather
                # than emitting zeros that would read as "idle".
                progress=_progress(available=False),
                cancellable=True,
            )
            rows[work_row["work_id"]] = work_row

    lane = LANE_DURABLE
    live_error = ""
    mod = _module("tools.async_delegation")
    if mod is not None:
        try:
            live_records = mod.list_async_delegations()
            # Same rule as the terminal lane: rows prove ownership, residency
            # does not.
            if live_records:
                lane = LANE_LIVE
            for record in live_records:
                if not isinstance(record, dict):
                    continue
                delegation_id = _safe_text(record.get("delegation_id"), limit=200)
                if not delegation_id:
                    continue
                status = _delegation_status(
                    record, idle_stale=idle_stale, in_tool_stale=in_tool_stale
                )
                work_id = f"{KIND_DELEGATION}:{delegation_id}"
                if status in {STATUS_COMPLETED, STATUS_ERROR}:
                    if rows.pop(work_id, None) is not None and accountant is not None:
                        accountant.drop(
                            "delegation_settled",
                            entity_id=delegation_id,
                            detail="live record reports a terminal status",
                            by_design=True,
                        )
                    continue
                api_calls = None
                activity = record.get("children_activity")
                if isinstance(activity, list) and activity:
                    first = activity[0]
                    if isinstance(first, dict):
                        api_calls = first.get("api_calls")
                dispatched = record.get("dispatched_at")
                existing = rows.get(work_id)
                if existing is None:
                    if accountant is not None:
                        accountant.consider()
                    existing = _row(
                        kind=KIND_DELEGATION,
                        stable_id=delegation_id,
                        label=_safe_text(record.get("goal"), limit=160) or delegation_id,
                        status=status,
                        source_lane=LANE_LIVE,
                        session_id=_safe_text(
                            record.get("parent_session_id") or record.get("session_key"),
                            limit=200,
                        ),
                        started_at=_iso(dispatched),
                        elapsed_seconds=_elapsed(
                            float(dispatched) if isinstance(dispatched, (int, float)) else None,
                            now=now,
                        ),
                        cancellable=True,
                    )
                    rows[work_id] = existing
                else:
                    existing["source_lane"] = LANE_LIVE
                    existing["status"] = status
                    if not existing.get("label") or existing["label"] == delegation_id:
                        existing["label"] = (
                            _safe_text(record.get("goal"), limit=160) or delegation_id
                        )
                existing["progress"] = _progress(
                    api_calls=api_calls,
                    in_tool=record.get("in_tool"),
                    seconds_since_progress=record.get("seconds_since_progress"),
                    available=True,
                )
        except Exception as exc:
            live_error = type(exc).__name__

    ordered = sorted(rows.values(), key=lambda item: (item.get("started_at") or "", item["work_id"]))
    capped = _cap(ordered, source=KIND_DELEGATION, accountant=accountant)
    if accountant is not None:
        accountant.include(len(capped))
    return (
        capped,
        _source(SOURCE_OK, lane=lane, live_enrichment_error=live_error),
    )


# --------------------------------------------------------------------------
# lane: in-flight mission-chat turns
# --------------------------------------------------------------------------


def _collect_chat_turns(
    *, now: float, accountant: ProjectionAccountant | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """In-flight turn-journal records — the durable "an agent is thinking" fact.

    The journal is written from whichever process executes the turn and read
    from anywhere, so this lane needs no live enrichment to be honest. A record
    stuck in an in-flight state after its executor died is a corpse the
    serve-boot sweep and the next-send repair flip to ``interrupted``; until one
    of them runs it is reported ``unknown`` rather than ``running`` when its
    start stamp is missing, because with no anchor there is nothing to age.
    """

    try:
        from . import mission_chat_turns

        records = mission_chat_turns.inflight_turn_rows()
    except Exception as exc:
        return [], _source(
            SOURCE_UNAVAILABLE,
            lane=LANE_DURABLE,
            reason="journal_unreadable",
            detail=type(exc).__name__,
        )

    rows: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if accountant is not None:
            accountant.consider()
        turn_id = _safe_text(record.get("turn_id") or record.get("client_message_id"), limit=200)
        if not turn_id:
            if accountant is not None:
                accountant.drop("unidentified_turn", detail="record carries no turn id")
            continue
        started_at = _safe_text(record.get("started_at"), limit=80)
        started_epoch = _parse_iso(started_at)
        state = str(record.get("state") or "")
        instance_id = _safe_text(record.get("persona_instance_id"), limit=200)
        rows.append(
            _row(
                kind=KIND_CHAT_TURN,
                stable_id=turn_id,
                label=instance_id or turn_id,
                # The journal state IS the status: ``pending``/``executing``
                # mean a turn is in flight, ``outcome_unknown`` means the
                # provider outcome was never observed — which is exactly the
                # shared ``unknown`` and must not be dressed up as running.
                status=STATUS_RUNNING if state in {"pending", "executing", "running"} else STATUS_UNKNOWN,
                source_lane=LANE_DURABLE,
                persona_instance_id=instance_id,
                session_id=_safe_text(
                    record.get("session_id")
                    or record.get("root_chat_session_id")
                    or record.get("active_session_id"),
                    limit=240,
                ),
                started_at=started_at,
                elapsed_seconds=_elapsed(started_epoch, now=now),
                progress=_progress(available=False),
                # Chat turns are interrupted through the chat lane's own
                # authority (turn-resolve / the steer marker), never by this
                # verb — see ``cancel_work``.
                cancellable=False,
            )
        )

    rows.sort(key=lambda item: (item.get("started_at") or "", item["work_id"]))
    capped = _cap(rows, source=KIND_CHAT_TURN, accountant=accountant)
    if accountant is not None:
        accountant.include(len(capped))
    return capped, _source(SOURCE_OK, lane=LANE_DURABLE)


# --------------------------------------------------------------------------
# lane: detached agent-to-agent dispatches
# --------------------------------------------------------------------------


def _collect_dispatches(
    *, now: float, accountant: ProjectionAccountant | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """In-flight detached dispatches — durable-only, and honestly so.

    The dispatch store is the whole truth here: a dispatch's row is written
    BEFORE its turn starts and settled when it ends, from whichever process ran
    it, so any lane reads the same answer and no live enrichment exists to add.
    That is unusual among these lanes and it is a property of the store, not an
    omission.

    Owner identity is verified like every other lane — a row whose recorded PID
    is gone is work that cannot still be running, and a recycled number is a
    stranger. The difference from the terminal/delegation lanes is what happens
    next: an orphaned dispatch is NOT lost, because
    ``restore_undelivered_dispatches`` reclassifies it into a deliverable
    ``unknown`` outcome at the next serve boot. So dropping it from the HUD is
    honest — it stopped running — rather than a silent loss.
    """

    try:
        from . import dispatch_store

        rows = dispatch_store.running_dispatches(limit=_MAX_ROWS_PER_SOURCE * 2)
    except Exception as exc:
        return [], _source(
            SOURCE_UNAVAILABLE,
            lane=LANE_DURABLE,
            reason="store_unreadable",
            detail=type(exc).__name__,
        )

    collected: list[dict[str, Any]] = []
    for record in rows:
        if not isinstance(record, dict):
            continue
        dispatch_id = _safe_text(record.get("dispatch_id"), limit=200)
        if not dispatch_id:
            continue
        if accountant is not None:
            accountant.consider()
        pid = record.get("owner_pid")
        _alive, verified, verdict = _pid_identity(pid, record.get("owner_started_at"))
        # A dead or recycled owner is REPORTED here, not dropped — the one place
        # this lane deliberately differs from the terminal and delegation lanes,
        # and the difference is a property of the store rather than a relaxation
        # of the rule.
        #
        # There, a dead PID means the work is over and nobody is owed anything;
        # dropping the row is the honest end of it. Here, a dead PID means the
        # CHILD PROCESS running a dispatch died while a SENDER IS STILL WAITING
        # for its answer. Hiding it would make the one dispatch most worth
        # looking at — the one that just died — the only invisible one, and to an
        # operator its disappearance reads as "it finished". So it surfaces as
        # ``unknown`` with ``pid_verified: false``: something was dispatched, its
        # process is gone, and nothing has settled it yet. The periodic orphan
        # sweep converts it into a deliverable ``unknown`` completion within its
        # cadence, and the row then leaves this projection because it is no
        # longer running work.
        orphaned = verdict in {PID_DEAD, PID_RECYCLED}
        started = record.get("dispatched_at")
        target = _safe_text(record.get("target_persona"), limit=120)
        label = _safe_text(record.get("title"), limit=160) or (
            f"{target}: {_safe_text(record.get('ask'), limit=120)}" if target else dispatch_id
        )
        collected.append(
            _row(
                kind=KIND_DISPATCH,
                stable_id=dispatch_id,
                label=label,
                status=STATUS_RUNNING if (verified and not orphaned) else STATUS_UNKNOWN,
                source_lane=LANE_DURABLE,
                pid=pid,
                pid_verified=verified and not orphaned,
                persona_id=target,
                persona_instance_id=_safe_text(record.get("target_instance_id"), limit=200),
                # The SENDER's chat root: the thread the answer is owed to, and
                # the row an operator would click through to.
                session_id=_safe_text(record.get("sender_session_id"), limit=240),
                started_at=_iso(started),
                elapsed_seconds=_elapsed(
                    float(started) if isinstance(started, (int, float)) else None, now=now
                ),
                # The target's turn owns its own progress signal; none of it
                # survives to this store, so say so rather than emit zeros.
                progress=_progress(available=False),
                tail_preview=_preview(record.get("ask"), accountant),
                # No interrupt seam in v1. The turn runs in a child process this
                # projection could signal, but a cancel has to unwind the store
                # row and the sender's pending delivery too, and half a cancel is
                # worse than none. Claiming cancellable here would put a button
                # on the HUD that cannot keep its promise.
                cancellable=False,
            )
        )

    # UNDELIVERABLE completions ride this lane too, and they are the reason it
    # is not purely "running" work.
    #
    # Three paths abandon a finished dispatch without ever forging a delivery
    # turn — no sender session, an unresolvable sender, and the attempt cap —
    # and all three used to leave nothing behind but an EventLog row that no
    # consumer reads. So an agent asked for work, the work RAN, an answer came
    # back, and then the answer was discarded in silence: no delivery, no
    # notification, and a HUD that says everything is fine. Surfacing them here
    # is the cheapest honest fix, because this is already the surface an
    # operator looks at to ask "what is my machine doing about that request".
    #
    # They are `error`, not `unknown`: nothing here is uncertain. We know the
    # dispatch settled, and we know its answer will never be delivered.
    try:
        dropped = dispatch_store.undeliverable_dispatches(
            limit=_MAX_ROWS_PER_SOURCE,
            since=now - _UNDELIVERABLE_WINDOW_SECONDS,
        )
    except Exception:
        # A read that fails here must not take the whole lane down with it — the
        # running rows above are already collected and are still true.
        dropped = []
    for record in dropped:
        if not isinstance(record, dict):
            continue
        dispatch_id = _safe_text(record.get("dispatch_id"), limit=200)
        if not dispatch_id:
            continue
        if accountant is not None:
            accountant.consider()
        target = _safe_text(record.get("target_persona"), limit=120)
        reason = _safe_text(record.get("delivery_error"), limit=160) or "undelivered"
        settled = record.get("completed_at") or record.get("updated_at")
        collected.append(
            _row(
                kind=KIND_DISPATCH,
                stable_id=dispatch_id,
                label=(
                    f"undelivered reply from {target}"
                    if target
                    else f"undelivered reply ({dispatch_id})"
                ),
                status=STATUS_ERROR,
                source_lane=LANE_DURABLE,
                persona_id=target,
                persona_instance_id=_safe_text(
                    record.get("target_instance_id"), limit=200
                ),
                session_id=_safe_text(record.get("sender_session_id"), limit=240),
                started_at=_iso(settled),
                elapsed_seconds=0,
                progress=_progress(available=False),
                # The drop REASON is the whole value of the row: "your answer
                # could not be delivered" is useless without "because".
                tail_preview=_preview(
                    f"delivery abandoned: {reason}", accountant
                ),
                cancellable=False,
            )
        )

    collected.sort(key=lambda item: (item.get("started_at") or "", item["work_id"]))
    capped = _cap(collected, source=KIND_DISPATCH, accountant=accountant)
    if accountant is not None:
        accountant.include(len(capped))
    return capped, _source(SOURCE_OK, lane=LANE_DURABLE)


# --------------------------------------------------------------------------
# lane: cron jobs
# --------------------------------------------------------------------------


def _cron_owned_here(scheduler: Any, running_ids: set) -> bool:
    """True when the cron scheduler's dispatch machinery lives in THIS process.

    Takes the already-read running ids rather than re-reading them, so a failure
    to read the scheduler is reported by the CALLER as ``scheduler_unreadable``
    instead of being swallowed here and rendered as ``not_in_process`` — those
    are different facts ("the scheduler broke" vs "I am not the scheduler") and
    only the first is actionable.

    The pools are created lazily by ``_submit_with_guard`` on the first tick and
    then persist, so their existence is durable proof of ownership for the life
    of the process — stronger than the running-id set alone, which empties
    between jobs and would make the lane flicker to ``unavailable`` in the gaps.
    """

    if running_ids:
        return True
    return any(
        getattr(scheduler, attr, None) is not None
        for attr in ("_parallel_pool", "_sequential_pool")
    )


def _collect_cron(
    *, accountant: ProjectionAccountant | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Currently-executing cron jobs — live-only, for a structural reason.

    ``scheduler.get_running_job_ids()`` reads a PROCESS-GLOBAL set owned by the
    scheduler's own thread pool. From any other process it returns an empty
    frozenset, which would render as "no cron jobs running" — a silent lie
    about a lane we simply cannot see. Module residency does not rescue this
    (``mission_chat_turns`` imports the scheduler transitively), so the lane
    demands positive proof that the scheduler runs HERE: a live running-id, or
    one of the dispatch pools the ticker creates on its first tick.
    """

    scheduler = _module("cron.scheduler")
    if scheduler is None:
        return [], _source(
            SOURCE_UNAVAILABLE, lane=LANE_LIVE, reason=REASON_NOT_IN_PROCESS
        )
    try:
        running_ids = set(scheduler.get_running_job_ids())
    except Exception as exc:
        return [], _source(
            SOURCE_UNAVAILABLE,
            lane=LANE_LIVE,
            reason="scheduler_unreadable",
            detail=type(exc).__name__,
        )
    if not _cron_owned_here(scheduler, running_ids):
        return [], _source(
            SOURCE_UNAVAILABLE, lane=LANE_LIVE, reason=REASON_NOT_IN_PROCESS
        )

    labels: dict[str, str] = {}
    jobs_mod = _module("cron.jobs")
    if jobs_mod is not None and running_ids:
        try:
            for job in jobs_mod.list_jobs(include_disabled=True) or []:
                if isinstance(job, dict) and job.get("id") in running_ids:
                    labels[str(job["id"])] = _safe_text(
                        job.get("name") or job.get("prompt") or job.get("id"), limit=160
                    )
        except Exception:
            # Label enrichment is cosmetic; the running set is the fact.
            labels = {}

    rows: list[dict[str, Any]] = []
    for job_id in sorted(running_ids):
        job_id = _safe_text(job_id, limit=200)
        if not job_id:
            continue
        if accountant is not None:
            accountant.consider()
        rows.append(
            _row(
                kind=KIND_CRON_JOB,
                stable_id=job_id,
                label=labels.get(job_id) or job_id,
                status=STATUS_RUNNING,
                source_lane=LANE_LIVE,
                progress=_progress(available=False),
                cancellable=False,
            )
        )

    capped = _cap(rows, source=KIND_CRON_JOB, accountant=accountant)
    if accountant is not None:
        accountant.include(len(capped))
    return capped, _source(SOURCE_OK, lane=LANE_LIVE)


# --------------------------------------------------------------------------
# public surface
# --------------------------------------------------------------------------

_COLLECTORS = (
    (KIND_TERMINAL, _collect_terminal, True),
    (KIND_DELEGATION, _collect_delegations, True),
    (KIND_CHAT_TURN, _collect_chat_turns, True),
    (KIND_DISPATCH, _collect_dispatches, True),
    (KIND_CRON_JOB, _collect_cron, False),
)


def build_running_work(
    accountant: ProjectionAccountant | None = None,
) -> dict[str, Any]:
    """The ``running_work`` projection: rows + per-source health + counts + ambient.

    Never raises. A lane that blows up in an unforeseen way still yields an
    ``unavailable`` source entry, because a projection that can crash the
    snapshot build is worse than one that admits a blind spot.

    ``rows`` / ``sources`` / ``counts`` are CONTRACT. ``ambient`` is not: it is
    machine-local context (see :func:`_ambient_context`), carried in its own
    named block precisely so it can never again be concatenated into a field a
    consumer branches on.
    """

    now = time.time()
    rows: list[dict[str, Any]] = []
    sources: dict[str, Any] = {}

    for name, collector, takes_now in _COLLECTORS:
        try:
            if takes_now:
                lane_rows, source = collector(now=now, accountant=accountant)
            else:
                lane_rows, source = collector(accountant=accountant)
        except Exception as exc:  # noqa: BLE001 - a lane must never break the frame
            lane_rows, source = [], _source(
                SOURCE_UNAVAILABLE,
                reason="collector_failed",
                detail=type(exc).__name__,
            )
        rows.extend(lane_rows)
        sources[name] = source

    counts = {"total": len(rows)}
    for status in STATUS_VALUES:
        matched = sum(1 for row in rows if row.get("status") == status)
        if matched:
            counts[status] = matched
    counts["unavailable_sources"] = sum(
        1 for entry in sources.values() if entry.get("status") != SOURCE_OK
    )

    return {
        "rows": rows,
        "sources": sources,
        "counts": counts,
        "ambient": _ambient_context(),
    }


def find_work_row(work_id: str) -> dict[str, Any] | None:
    """The single row matching ``work_id``, or None."""

    target = str(work_id or "").strip()
    if not target:
        return None
    for row in build_running_work().get("rows") or []:
        if row.get("work_id") == target:
            return row
    return None


def split_work_id(work_id: str) -> tuple[str, str]:
    """``("terminal", "sess-1")`` for ``"terminal:sess-1"``; ``("", "")`` if malformed.

    Split on the FIRST colon only — session ids and job ids may contain them.
    """

    text = str(work_id or "").strip()
    kind, sep, stable = text.partition(":")
    if not sep or kind not in RUNNING_WORK_KINDS or not stable:
        return "", ""
    return kind, stable


def peek_work(work_id: str) -> dict[str, Any]:
    """Bounded read-only look at what one piece of work is doing.

    Mirrors ``process_registry.poll()``'s READ side and nothing else. It must
    never mark ``_completion_consumed`` (which is what ``read_log``/``wait``
    do), and unlike ``poll`` it does not even record a poll observation: this is
    an operator surface, and letting it register as consumption would suppress
    the autonomous ``notify_on_complete`` delivery turn the agent is waiting on.

    Output buffers are process-local, so a peek from a lane that did not spawn
    the work honestly reports ``tail_available: false`` instead of an empty tail
    that would read as "no output".
    """

    kind, stable = split_work_id(work_id)
    if not kind:
        return {
            "work_id": str(work_id or ""),
            "found": False,
            "error": "malformed_work_id",
        }

    row = find_work_row(work_id)
    payload: dict[str, Any] = {
        "work_id": work_id,
        # ``work_kind``, not ``kind``: this payload is emitted through the
        # stage42 object envelope, whose own ``kind`` names the ENVELOPE shape.
        # A row-level ``kind`` here would silently overwrite it and every
        # consumer branching on envelope kind would mis-route the response.
        "work_kind": kind,
        "found": row is not None,
        "row": row,
        "tail_available": False,
        "tail": "",
        "tail_limit": PEEK_TAIL_LIMIT,
        "consumed": False,
    }
    if row is None:
        return payload

    if kind == KIND_TERMINAL:
        registry = _module("tools.process_registry")
        if registry is None:
            payload["tail_reason"] = REASON_NOT_IN_PROCESS
            return payload
        try:
            session = registry.process_registry.get(stable)
        except Exception:
            session = None
        if session is None:
            payload["tail_reason"] = "session_not_in_registry"
            return payload
        try:
            # Read the rolling buffer directly under the session's own lock.
            # No reconcile, no consumption flag, no state transition.
            with session._lock:  # noqa: SLF001 - the buffer's only guard
                buffered = session.output_buffer or ""
            tail = _strip_ansi(buffered)[-PEEK_TAIL_LIMIT:]
        except Exception as exc:
            payload["tail_reason"] = f"buffer_unreadable:{type(exc).__name__}"
            return payload
        payload["tail_available"] = True
        payload["tail"] = _safe_text(tail, limit=PEEK_TAIL_LIMIT)
        payload["truncated"] = len(buffered) > PEEK_TAIL_LIMIT
        return payload

    # Every other kind reports progress, not output: the row already carries the
    # whole readable truth (status, elapsed, in_tool, seconds_since_progress).
    payload["tail_reason"] = "no_output_stream"
    return payload


def _delegation_owned_here(mod: Any, delegation_id: str) -> bool:
    """True when THIS process holds the live record for ``delegation_id``.

    Ownership is per-record, not per-module: a process can hold some
    delegations and know nothing about others, and only the holder has the
    ``interrupt_fn`` that can actually stop the child.
    """

    try:
        return any(
            str((record or {}).get("delegation_id") or "") == delegation_id
            for record in mod.list_async_delegations()
        )
    except Exception:
        return False


def cancel_work(work_id: str, *, reason: str = "operator_cancel") -> dict[str, Any]:
    """Route a cancel to the owning subsystem's interrupt seam.

    Never a bare PID kill. Terminal work goes through
    ``process_registry.kill_process``, whose tree-kill is identity-guarded by
    the same ``host_start_time`` baseline this projection verifies; delegations
    go through ``interrupt_for_session``, which lets the child unwind and
    deliver partial results through the normal finalize path. Kinds with no
    interrupt seam return a typed ``cancel_unsupported`` rather than pretending.
    """

    kind, stable = split_work_id(work_id)
    if not kind:
        return {"status": "error", "code": "invalid_request", "work_id": str(work_id or "")}

    row = find_work_row(work_id)
    if row is None:
        return {"status": "error", "code": "not_found", "work_id": work_id}

    if kind == KIND_TERMINAL:
        registry = _module("tools.process_registry")
        session = None
        if registry is not None:
            try:
                session = registry.process_registry.get(stable)
            except Exception:
                session = None
        if session is None:
            # The row exists — it came from the durable checkpoint — but the
            # handle lives in another process, so there is nothing here to kill.
            # The same positive-ownership rule the read lanes apply: falling
            # through to `kill_process` would return `not_found` and report
            # "no such work" about work that is demonstrably running.
            return {
                "status": "error",
                "code": "cancel_unavailable",
                "work_id": work_id,
                "detail": REASON_NOT_IN_PROCESS,
                "owning_lane": "serve",
            }
        try:
            # consume_output=False: a cancel is not the agent reading its
            # output, so it must not suppress the completion notification.
            result = registry.process_registry.kill_process(
                stable, source="harness.work_cancel", consume_output=False
            )
        except Exception as exc:
            return {
                "status": "error",
                "code": "cancel_failed",
                "work_id": work_id,
                "detail": type(exc).__name__,
            }
        killed = str(result.get("status")) != "not_found"
        return {
            "status": "cancelled" if killed else "error",
            "code": "" if killed else "not_found",
            "work_id": work_id,
            "kind": kind,
            "result": _safe_text(result.get("status"), limit=80),
        }

    if kind == KIND_DELEGATION:
        mod = _module("tools.async_delegation")
        if mod is None or not _delegation_owned_here(mod, stable):
            # The interrupt callable lives in the record map of the process that
            # dispatched the child. From anywhere else `interrupt_for_session`
            # matches nothing and returns 0, which the caller would otherwise
            # read as "the cancel failed" rather than "ask the owning lane".
            return {
                "status": "error",
                "code": "cancel_unavailable",
                "work_id": work_id,
                "detail": REASON_NOT_IN_PROCESS,
                "owning_lane": "serve",
            }
        session_id = (row.get("owner") or {}).get("session_id") or ""
        if not session_id:
            return {
                "status": "error",
                "code": "cancel_unavailable",
                "work_id": work_id,
                "detail": "delegation row carries no owning session",
            }
        try:
            interrupted = mod.interrupt_for_session(
                parent_session_id=str(session_id), reason=reason
            )
        except Exception as exc:
            return {
                "status": "error",
                "code": "cancel_failed",
                "work_id": work_id,
                "detail": type(exc).__name__,
            }
        if not interrupted:
            return {
                "status": "error",
                "code": "cancel_failed",
                "work_id": work_id,
                "detail": "no running delegation matched the owning session",
            }
        return {
            "status": "cancelled",
            "code": "",
            "work_id": work_id,
            "kind": kind,
            "interrupted": int(interrupted),
        }

    return {
        "status": "error",
        "code": "cancel_unsupported",
        "work_id": work_id,
        "kind": kind,
        "detail": f"{kind} work has no interrupt seam in v1",
    }
