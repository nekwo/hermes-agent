"""Discovery: which serves are running against THIS runtime root, on what code.

Why this exists
---------------

Multiple runtime roots legitimately coexist on this machine — the operator's
live root, QA lanes, isolated worktree roots, dev profiles. Once a serve is a
durable service rather than a child the Launcher spawned and owns, "connect to
the service for root X" needs an answer, and so does the operator question
nothing can answer today: *how many serves are running against this root, and
which code is each one on?*

This module is the file half of that answer. Each serve writes
``<store_root>/serve_instances/<pid>.json`` at boot and removes it on any clean
exit (shutdown or drain). A crash leaves the file behind — deliberately: a
registry that could only be correct if every process died politely would be
wrong exactly when it matters.

Liveness is therefore checked at READ time, never trusted from the file
--------------------------------------------------------------------

A stale entry is not an error, it is a state, and it has three of them. The
reader proves each one:

* ``live`` — the pid is alive, its process start time matches the baseline
  recorded at registration, and its command line still looks like a hermes
  serve.
* ``stale_dead_pid`` — the pid is gone. Nothing to connect to.
* ``stale_recycled_pid`` — the pid is alive but is a DIFFERENT process wearing
  a recycled number (start-time mismatch, or a command line that is not a
  hermes serve). This repo has already been bitten by PID recycling: a stored
  number landed on a desktop browser's session leader and the tree-kill
  SIGTERMed it (see ``tools/process_registry._host_pid_is_ours``). Connecting
  to — or worse, signalling — a recycled pid is the same defect one layer up.
* ``unknown`` — a probe could not answer. This is the FAIL-SAFE direction and
  it is never collapsed into ``live``: "I could not read this process's start
  time" is not evidence that it is mine. Guessing "mine" is how a client
  attaches to a stranger; guessing "stale" is how a prune deletes a running
  service's entry.

Consequently :func:`prune_stale_serve_instances` deletes **only**
``stale_dead_pid`` entries — provably dead, nothing else — and reports exactly
what it removed. Listing never prunes: an operator debugging "why do I have
four serves" must see the wreckage, not a registry that tidied the evidence
away before they looked.

Pruning at BOOT, and why that is a different moment
---------------------------------------------------

Serve boot calls the prune once, immediately after registering its own entry,
and puts the returned report on the service log
(``harness_parts.serve``, event ``serve_instances_pruned``). That is not the
rule above being quietly relaxed. The rule protects a READ from destroying the
evidence it is reporting; a boot is a WRITE moment — it has just created a file
in this directory — and the evidence is not destroyed but relocated: the report
names pid, boot_id, path, classification and reason for every record, deleted
and kept alike, onto a channel correlatable by ``boot_id`` against that boot's
``ready`` frame. Records leave a directory nobody reads for a log the operator
already reads.

It is needed because the launcher's boot hygiene sweep ``taskkill /F``s orphan
serves, which is a crash by construction — those serves are never given the
chance to unregister — so the deliberate "a crash leaves the file behind"
design accumulates without a floor. Measured on the operator's runtime: 14
serve boots in ~19 h left 2 records, while a third exited cleanly and removed
its own. This is tidiness plus forensics, NOT a correctness fix: leftover
records are already harmless, since ``resolve_socket_target`` returns only rows
classified ``live``, the launcher never reads this directory, and the
fingerprint exclusion below keeps them out of every freshness key.

Ordering is load-bearing (register first, then prune) and so is the classifier:
the boot caller passes no widened classification set, so ``unknown`` and
``stale_recycled_pid`` survive a boot exactly as they survive everything else.

The end-reason sidecar: why the last runtime ended (RL-16)
-----------------------------------------------------------

A row says *there is a runtime here*. It never said *why the one that was here
is gone*, and on 2026-09-05 that was the whole question: a leftover row named
pid 33680, the pid was gone, and a closed console window, a logoff, an
uncaught fault and a ``TerminateProcess`` from a hygiene sweep all left exactly
the same evidence. So a serve now writes ``serve_instances/<pid>.ended.json``
— ``{reason, at, boot_id, pid}`` — on any end it can observe, through the same
atomic writer as the row.

Three things about it are load-bearing and easy to break:

* It is a SEPARATE file because the row is REMOVED on a clean exit and the
  reason has to outlive that removal.
* **Absence is a reading.** ``TerminateProcess`` runs no code in the target, so
  it writes nothing; a stale row with no sidecar therefore says *something
  killed this without asking* (the launcher words it ``ended=absent``). Nothing
  may write a placeholder for an end it did not observe.
* It shares this directory, so :func:`list_serve_instances` — the one scan every
  other reader in the tree is built on — filters the suffix out. A sidecar read
  as a row is a pid-less record that classifies ``unknown``, which is the
  fail-safe direction and therefore silent.

Retention is a boot-time floor (:func:`prune_serve_ended`, newest
``SERVE_ENDED_RETENTION``): nothing ever consumes a reason, so without one the
directory grows forever.

The stderr log: what the runtime was saying (RL-19)
---------------------------------------------------

A row says a runtime is here and a sidecar says why the last one left; neither
says what it was SAYING. Since RL-17 the launcher starts the service with all
three stdio handles on ``DEVNULL``, so every traceback it writes goes nowhere —
measured on 2026-09-06, when a local hub stream stalled for thirty seconds and
the runtime's half of that half hour was simply unrecoverable. So a
``--service`` runtime opens ``serve_instances/<pid>.stderr.log`` at boot
(:func:`open_serve_stderr_log`) and keeps its stderr there: line-buffered UTF-8
text, one file per runtime, a header line naming pid/boot_id/build/start so a
file read cold still says whose it was. It is a THIRD shape in this directory,
so :func:`list_serve_instances` ignores it (by glob and by name) and
:func:`prune_serve_ended` floors it — newest ``SERVE_ENDED_RETENTION`` of each
family, separately.

``hermes_home``: which home, answered from OUTSIDE the process
--------------------------------------------------------------

``store_root`` answers *which runtime root*, and that is a different axis from
*which profile home*. A serve child spawned with ``HERMES_HOME`` pointed at
``profiles/base`` writes the same ``store_root`` as one that resolved
``profiles/alice``, so until this field existed nothing outside a running serve
could say which home it was on — ``harness status --json`` answers it live, but
only for the process you can already talk to, which is the wrong end of the
question when you are looking at a directory of records.

The field is *the home this process resolved AT REGISTRATION*. It is
deliberately not per-turn truth: the runtime may rebind a home for a single
turn and this key will not have moved. Reading it as anything stronger than a
boot-time observation is a misuse.

Three states, three spellings, and a reader must keep them apart: a path says
*this home*; ``null`` says *this serve could not resolve one*; an ABSENT key
says *this entry predates the field*. That third state is real — the records
written before this landed have no such key — so **nothing classifies on it**.
It is reported, never branched on, and ``schema_version`` stays 1 for exactly
that reason: an additive nullable key that no reader requires is not a new
schema (the ``port`` / ``socket_started_at`` precedent).

Resolution is the CALLER's job. This module imports no ``hermes_constants``;
``hermes_cli.harness_parts.serve`` passes ``str(get_hermes_home())`` and
degrades to ``None`` if that raises, because a registry entry is bookkeeping
and bookkeeping must not be able to fail a boot.

Fingerprint exclusion (load-bearing)
------------------------------------

``serve_instances/`` MUST NOT be added to any freshness fingerprint — neither
serve's ``_FINGERPRINT_ROOT_FILES``/``_FINGERPRINT_STORE_DIRS`` nor
``stream._scope_fingerprint``. Entries appear and vanish at every serve boot
and exit, which inside a fingerprint would hold the read-model cache cold and
make the stream emit ``state.reconciled`` on every runtime restart. Same
rationale, and the same standing precedent, as
``dispatch_delivery.DRAIN_STATE_FILENAME``.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .serde import write_json_atomic

__all__ = [
    "CLASSIFICATION_LIVE",
    "CLASSIFICATION_STALE_DEAD_PID",
    "CLASSIFICATION_STALE_RECYCLED_PID",
    "CLASSIFICATION_UNKNOWN",
    "SERVE_ENDED_RETENTION",
    "SERVE_ENDED_SUFFIX",
    "SERVE_INSTANCES_DIRNAME",
    "SERVE_INSTANCE_SCHEMA_VERSION",
    "SERVE_STDERR_HEADER_PREFIX",
    "SERVE_STDERR_SUFFIX",
    "ProcessProbe",
    "ServeInstanceRegistration",
    "default_process_probe",
    "list_serve_ended",
    "list_serve_instances",
    "list_serve_stderr_logs",
    "open_serve_stderr_log",
    "pid_alive",
    "prune_serve_ended",
    "prune_stale_serve_instances",
    "read_serve_ended",
    "register_serve_instance",
    "serve_ended_path",
    "serve_instance_path",
    "serve_instances_dir",
    "serve_stderr_log_path",
    "unregister_serve_instance",
    "write_serve_ended",
]

SERVE_INSTANCES_DIRNAME = "serve_instances"
SERVE_INSTANCE_SCHEMA_VERSION = 1

#: The end-reason sidecar's filename tail: ``serve_instances/<pid>.ended.json``.
#:
#: It shares the row's directory ON PURPOSE — the row and the reason answer the
#: same question about the same pid, and a second directory would be a second
#: thing to create, sandbox, exclude from every freshness fingerprint, and
#: remember to look in. The price of sharing is that the row scan must now tell
#: two file shapes apart, which is exactly what a naive ``*.json`` glob does
#: not; see :func:`list_serve_instances`.
SERVE_ENDED_SUFFIX = ".ended.json"

#: How many sidecars survive a serve boot. A registry row is removed on a clean
#: exit, but a reason is written to be READ LATER — so nothing ever deletes one
#: for having been used, and on a machine that restarts its runtime a dozen
#: times a day the directory would grow without a ceiling forever. Twenty is a
#: forensic window, not a quota: the launcher reads exactly one (the pid on the
#: stale row it just found), and an operator reconstructing a bad afternoon
#: reads the tail.
SERVE_ENDED_RETENTION = 20

#: The service runtime's own stderr, filed beside its row and its reason:
#: ``serve_instances/<pid>.stderr.log`` (RL-19). NOT ``.json`` on purpose — it
#: is a text log, so the row scan's ``*.json`` glob cannot see it at all, which
#: is the cheapest possible answer to "does a third file shape break the
#: reader". The suffix is still named, exported and pinned by a test, because
#: "the glob happens not to match it" survives only until somebody widens the
#: glob.
SERVE_STDERR_SUFFIX = ".stderr.log"

#: Everything in this directory that is NOT a registry row. One tuple, one
#: place: two non-row shapes joined the row here within a day of each other,
#: and the next reader written must not have to rediscover both.
_NON_ROW_SUFFIXES = (SERVE_ENDED_SUFFIX, SERVE_STDERR_SUFFIX)

#: The first line of a stderr log, and the only line the RUNTIME'S OWN code
#: puts there. It exists so a file read COLD — days later, out of a QA bundle,
#: with no registry row left to join it to — still says which runtime it
#: belonged to. Parsed back by :func:`list_serve_stderr_logs` for the retention
#: ordering, which is why the format is a constant here and not a print in
#: ``serve.py``.
SERVE_STDERR_HEADER_PREFIX = "# harness serve --service"

CLASSIFICATION_LIVE = "live"
CLASSIFICATION_STALE_DEAD_PID = "stale_dead_pid"
CLASSIFICATION_STALE_RECYCLED_PID = "stale_recycled_pid"
CLASSIFICATION_UNKNOWN = "unknown"

#: Tokens that must appear in a live process's command line for it to pass as
#: a hermes serve. Matched case-insensitively against the whole line, so both
#: ``python -m hermes_cli.main harness serve --ndjson`` and a packaged
#: ``hermes.exe harness serve`` satisfy it.
_SERVE_CMDLINE_TOKENS = ("hermes", "serve")

#: Bounded retry budget for removing an entry on a clean exit (~0.5s). Sized
#: for the AV/indexer handle window on Windows, which is tens of milliseconds —
#: long enough to lose the race, far too short to justify waiting on it longer.
UNREGISTER_ATTEMPTS = 20
UNREGISTER_RETRY_DELAY_SECONDS = 0.025

#: argv is a HINT, capped hard. A serve's own argv is benign, but this file is
#: read by operators and pasted into reports, and argv on OTHER harness lanes
#: carries message text — so the shape never grows past a hint.
_ARGV_HINT_TOKEN_CAP = 8
_ARGV_HINT_CHAR_CAP = 200


@dataclass(frozen=True, slots=True)
class ServeInstanceRegistration:
    """What registration did. Never raises; the outcome is the report."""

    #: ``registered`` | ``error:<reason>``
    outcome: str
    pid: int
    boot_id: str
    path: str

    @property
    def registered(self) -> bool:
        return self.outcome == "registered"

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "path": self.path,
        }


@dataclass(frozen=True)
class ProcessProbe:
    """The three questions classification asks the OS about a pid.

    Injected so classification is unit-testable against synthetic processes:
    every probe may answer ``None`` for "could not determine", which the
    classifier must route to ``unknown`` rather than to a guess.
    """

    alive: Callable[[int], bool | None]
    start_time: Callable[[int], int | None]
    cmdline: Callable[[int], str | None]


def serve_instances_dir(store_root: Path | str) -> Path:
    return Path(store_root) / SERVE_INSTANCES_DIRNAME


def serve_instance_path(store_root: Path | str, pid: int) -> Path:
    return serve_instances_dir(store_root) / f"{int(pid)}.json"


def register_serve_instance(
    store_root: Path | str,
    *,
    transport: str = "stdio",
    build: dict[str, Any] | None = None,
    pid: int | None = None,
    boot_id: str | None = None,
    argv: list[str] | None = None,
    probe: ProcessProbe | None = None,
    port: int | None = None,
    socket_started_at: str | None = None,
    hermes_home: str | None = None,
    service: bool = False,
    starter_pid: int | None = None,
) -> ServeInstanceRegistration:
    """Announce this serve under *store_root*. Best effort, always reported.

    ``port`` / ``socket_started_at`` are the socket lane's additive fields
    (slice 3), and they are ALWAYS written — ``null`` on a stdio-only serve. A
    conditional key could not distinguish "this serve has no socket" from "this
    entry predates the socket lane"; a null says the first, plainly.
    ``transport`` becomes ``stdio+socket`` on the serve that won the per-root
    socket ownership lock, so discovery reads the port off the entry whose
    liveness this module has already classified rather than out of a second file
    with its own staleness story.

    ``service`` and ``starter_pid`` are the service-lifetime fields (L-h) and
    follow the same ALWAYS-WRITTEN rule, for a sharper version of the same
    reason: a client discovering this row is deciding whether to ATTACH to the
    process or to start one, and "closing my end of that pipe would kill this
    runtime" versus "it will keep going without me" is the whole decision.
    ``False`` says the runtime dies with its stdin; a MISSING key says the row
    was written by a hermes that predates service mode — which is a different
    fact, and the launcher's fallback condition. ``starter_pid`` is the pid that
    STARTED the process (its parent at boot, computed by the caller like
    ``hermes_home`` above), not its parent now: a detached service is reparented
    the moment its starter exits, so a value read later names somebody else.

    ``hermes_home`` follows the same additive-null rule and is likewise ALWAYS
    written — see the module docstring for what it does and does not mean. The
    value is COMPUTED BY THE CALLER and passed in: this module resolves nothing
    and imports no ``hermes_constants``, so the field stays unit-testable
    against an injected string and the registry keeps its one job. A caller
    whose resolution failed passes ``None``, which is written as ``null`` and
    never as an empty string — an empty string reads like a path.
    """

    resolved_pid = int(pid if pid is not None else os.getpid())
    resolved_boot_id = boot_id or uuid.uuid4().hex
    path = serve_instance_path(store_root, resolved_pid)
    prober = probe or default_process_probe()
    record = {
        "schema_version": SERVE_INSTANCE_SCHEMA_VERSION,
        "pid": resolved_pid,
        "boot_id": resolved_boot_id,
        "transport": transport,
        "port": None if port is None else int(port),
        "socket_started_at": socket_started_at,
        "store_root": str(store_root),
        # The home THIS process resolved AT BOOT — an observability fact, not
        # per-turn authority: the runtime may rebind a home for a turn, and
        # this key will not have moved. Null says "resolution failed"; an
        # ABSENT key says "this entry predates the field".
        "hermes_home": None if hermes_home is None else str(hermes_home),
        # Whether this runtime outlives the stdin it was started on, and who
        # started it. See the docstring: present-and-false and absent are two
        # different answers.
        "service": bool(service),
        "starter_pid": None if starter_pid is None else int(starter_pid),
        "started_at": _now_iso(),
        # The identity baseline the recycled-pid check compares against. None
        # when the OS would not say — recorded as null so the reader knows the
        # check is unavailable rather than passing by default.
        "started_at_ticks": _safe(lambda: prober.start_time(resolved_pid)),
        "argv_hint": _argv_hint(argv if argv is not None else list(sys.argv)),
        "build": dict(build) if isinstance(build, dict) else None,
    }
    try:
        write_json_atomic(path, record)
    except Exception as exc:
        return ServeInstanceRegistration(
            outcome=f"error:{type(exc).__name__}",
            pid=resolved_pid,
            boot_id=resolved_boot_id,
            path=str(path),
        )
    return ServeInstanceRegistration(
        outcome="registered",
        pid=resolved_pid,
        boot_id=resolved_boot_id,
        path=str(path),
    )


def unregister_serve_instance(
    store_root: Path | str,
    *,
    pid: int | None = None,
    attempts: int = UNREGISTER_ATTEMPTS,
    retry_delay_seconds: float = UNREGISTER_RETRY_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Remove this serve's entry on a clean exit. True when a file was removed.

    Retries a transiently LOCKED file, because on Windows that is the ordinary
    case, not an exotic one. A file created seconds ago in a scanned directory
    is routinely held open by the AV/indexer for a few tens of milliseconds, and
    ``unlink`` then fails with WinError 32 — observed live on the drain path,
    where the whole point is a clean handover: the drain published its terminal
    frame, released the socket lock, and left its registry entry behind, so the
    registry advertised a serve that had just gone. The lock was held for ~16ms
    there; one retry cleared it. FileNotFoundError is NOT retried — already gone
    is a state, not a failure.

    Never raises: a runtime on its way down must not fail on bookkeeping, and a
    leftover entry is a state the reader already classifies honestly.
    """

    resolved_pid = int(pid if pid is not None else os.getpid())
    path = serve_instance_path(store_root, resolved_pid)
    total = max(1, int(attempts))
    for attempt in range(total):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            if attempt + 1 >= total:
                return False
            sleep(max(0.0, float(retry_delay_seconds)))
    return False  # pragma: no cover - loop always returns


def list_serve_instances(
    store_root: Path | str, *, probe: ProcessProbe | None = None
) -> list[dict[str, Any]]:
    """Every registered entry for *store_root*, each classified at READ time.

    Reports, never prunes — see the module docstring. Rows are sorted by pid so
    two consecutive reads of an unchanged registry are byte-identical.
    """

    prober = probe or default_process_probe()
    rows: list[dict[str, Any]] = []
    directory = serve_instances_dir(store_root)
    try:
        # ``*.json`` alone is wrong since the end-reason sidecar joined this
        # directory (RL-16): ``<pid>.ended.json`` matches it, and a sidecar read
        # as a registry record is a row with no pid, no port and no identity
        # baseline — which every reader downstream then classifies ``unknown``
        # and reports. Three file shapes now, one directory, one place that
        # knows: the RL-19 stderr log is excluded by name here as well as by the
        # glob, so widening the glob one day cannot quietly turn a text log into
        # a phantom runtime.
        entries = sorted(
            entry
            for entry in directory.glob("*.json")
            if not entry.name.endswith(_NON_ROW_SUFFIXES)
        )
    except OSError:
        return rows
    for entry in entries:
        record = _read_json(entry)
        if record is None:
            rows.append(
                {
                    "path": str(entry),
                    "pid": _pid_from_filename(entry),
                    "classification": CLASSIFICATION_UNKNOWN,
                    "classification_reason": "record_unreadable",
                }
            )
            continue
        classification, reason = classify_serve_instance(record, probe=prober)
        row = dict(record)
        row["path"] = str(entry)
        row["classification"] = classification
        row["classification_reason"] = reason
        rows.append(row)
    rows.sort(key=lambda item: (item.get("pid") is None, item.get("pid") or 0))
    return rows


def classify_serve_instance(
    record: dict[str, Any], *, probe: ProcessProbe | None = None
) -> tuple[str, str]:
    """``(classification, reason)`` for one registry record.

    The fail-safe direction is ``unknown``: every probe that cannot answer
    lands there, never on ``live``.
    """

    prober = probe or default_process_probe()
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return CLASSIFICATION_UNKNOWN, "pid_missing"

    alive = _safe(lambda: prober.alive(pid))
    if alive is None:
        return CLASSIFICATION_UNKNOWN, "liveness_unreadable"
    if not alive:
        return CLASSIFICATION_STALE_DEAD_PID, "pid_not_running"

    baseline = record.get("started_at_ticks")
    observed = _safe(lambda: prober.start_time(pid))
    if baseline is None:
        start_time_reason = "no_identity_baseline"
    elif observed is None:
        start_time_reason = "start_time_unreadable"
    elif int(observed) != int(baseline):
        # Alive, but not the process that registered: the number was recycled.
        return CLASSIFICATION_STALE_RECYCLED_PID, "start_time_mismatch"
    else:
        start_time_reason = ""

    cmdline = _safe(lambda: prober.cmdline(pid))
    if cmdline is None:
        cmdline_reason = "cmdline_unreadable"
    elif not _looks_like_serve(cmdline):
        # An identity baseline that MATCHES outranks this: a matching start
        # time is proof of identity, while a command line we cannot parse the
        # way we expect is only weak evidence. But with no baseline, a
        # non-serve command line is the only recycling signal there is.
        if start_time_reason == "":
            cmdline_reason = "cmdline_not_serve_like"
        else:
            return CLASSIFICATION_STALE_RECYCLED_PID, "cmdline_not_serve_like"
    else:
        cmdline_reason = ""

    if start_time_reason:
        return CLASSIFICATION_UNKNOWN, start_time_reason
    if cmdline_reason:
        return CLASSIFICATION_UNKNOWN, cmdline_reason
    return CLASSIFICATION_LIVE, ""


def prune_stale_serve_instances(
    store_root: Path | str, *, probe: ProcessProbe | None = None
) -> dict[str, Any]:
    """Delete ONLY provably-dead entries, and report exactly what went.

    ``stale_recycled_pid`` and ``unknown`` are deliberately kept: the first
    names a live process this registry no longer understands (an operator
    should see that), and the second means a probe failed — deleting on a
    failed probe is how a prune removes a running service.
    """

    deleted: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for row in list_serve_instances(store_root, probe=probe):
        summary = {
            "pid": row.get("pid"),
            "boot_id": row.get("boot_id"),
            "path": row.get("path"),
            "classification": row.get("classification"),
            "classification_reason": row.get("classification_reason"),
        }
        if row.get("classification") != CLASSIFICATION_STALE_DEAD_PID:
            kept.append(summary)
            continue
        try:
            Path(str(row.get("path"))).unlink()
        except OSError as exc:
            summary["error"] = type(exc).__name__
            kept.append(summary)
            continue
        deleted.append(summary)
    return {"deleted": deleted, "kept": kept, "deleted_count": len(deleted)}


# ── the end-reason sidecar (RL-16) ──────────────────────────────────────────
#
# The registry row answers "is there a runtime here". It cannot answer the
# question that was actually being asked on 2026-09-05: a row was found for pid
# 33680, the pid was gone, and NOTHING on the machine could say why — closed
# console window, logoff, drain that never finished, uncaught fault, or a
# TerminateProcess from a hygiene sweep were all the same leftover file. The
# sidecar makes the runtime name its own cause on the way out, so the next
# unexplained death is read rather than guessed.
#
# Three properties carry the whole design:
#
# * It is a DIFFERENT FILE from the row, because a clean exit removes the row
#   and the reason for the exit must survive the exit.
# * It is written best effort and NEVER raises, because every caller is a
#   process on its way down — an ``atexit`` hook, an OS-owned console control
#   thread, a signal handler.
# * ABSENCE IS A READING. A ``TerminateProcess`` runs no code in the target, so
#   it writes nothing; a stale row with no sidecar therefore says "something
#   killed this without asking", which the launcher words ``ended=absent``.
#   Nothing may write a placeholder for an end it did not observe.


def serve_ended_path(store_root: Path | str, pid: int) -> Path:
    return serve_instances_dir(store_root) / f"{int(pid)}{SERVE_ENDED_SUFFIX}"


def write_serve_ended(
    store_root: Path | str,
    *,
    reason: str,
    boot_id: str,
    pid: int | None = None,
    at: str | None = None,
) -> bool:
    """Record why this runtime ended. True when a file landed.

    Four keys and no more: a launcher reading a dead runtime's last word needs
    the word, when, which boot, and whose pid — and every additional key would
    be a fact about a process that no longer exists to be asked about it.

    Written through ``write_json_atomic``, the same helper the registry row
    uses: tmp-and-rename in the destination directory, LF-canonical, and (this
    is why it must not be a second writer) ACL-safe on Windows, where a file
    created directly under a store root the launcher also reads has been the
    source of enough grief already. One writer for this directory.

    Never raises. The caller is dying.
    """

    resolved_pid = int(pid if pid is not None else os.getpid())
    record = {
        "reason": str(reason),
        "at": at or _now_iso(),
        "boot_id": str(boot_id),
        "pid": resolved_pid,
    }
    try:
        write_json_atomic(serve_ended_path(store_root, resolved_pid), record)
    except Exception:
        return False
    return True


def read_serve_ended(store_root: Path | str, pid: int) -> dict[str, Any] | None:
    """This pid's last word, or ``None`` — which is itself the reading."""

    return _read_json(serve_ended_path(store_root, pid))


def list_serve_ended(store_root: Path | str) -> list[dict[str, Any]]:
    """Every sidecar under *store_root*, NEWEST FIRST, each carrying ``path``.

    Ordered by the record's own ``at`` and then by filename, never by mtime: a
    copied, restored or archived directory has mtimes that say when the files
    were moved, and the whole value of these records is a timeline.
    """

    rows: list[dict[str, Any]] = []
    directory = serve_instances_dir(store_root)
    try:
        entries = sorted(directory.glob(f"*{SERVE_ENDED_SUFFIX}"))
    except OSError:
        return rows
    for entry in entries:
        record = _read_json(entry) or {}
        row = dict(record)
        row["path"] = str(entry)
        if not isinstance(row.get("pid"), int):
            row["pid"] = _pid_from_filename(Path(entry.name[: -len(SERVE_ENDED_SUFFIX)]))
        rows.append(row)
    rows.sort(key=_ended_sort_key, reverse=True)
    return rows


def prune_serve_ended(
    store_root: Path | str, *, keep: int = SERVE_ENDED_RETENTION
) -> dict[str, Any]:
    """Floor BOTH forensic families at the newest *keep* records each.

    Called once at serve boot, beside the row prune and for the mirror-image
    reason: that one exists because a crash deliberately leaves its row behind,
    this one because nothing ever consumes a reason. Deleting the OLDEST is the
    only safe direction — the record a reader wants is the one belonging to the
    runtime that just died.

    Two families, one call, one retention (RL-19): the end reason and the stderr
    log are written by the same runtime about the same ending, and a directory
    where one is floored at twenty while the other grows forever would hand an
    operator a reason with no log or a log with no reason. They are floored
    SEPARATELY rather than as one pool of forty — a machine that runs twenty
    service serves would otherwise evict every reason a non-service serve wrote.

    Blind to registry rows by construction (it globs the two non-row suffixes),
    which matters more here than it reads: this prune runs at boot, milliseconds
    after this very serve wrote its own row into the same directory — and, since
    RL-19, milliseconds after it opened its own stderr log there, which is the
    NEWEST file of its family and therefore never a candidate.
    """

    limit = max(0, int(keep))
    deleted: list[dict[str, Any]] = []
    families: dict[str, dict[str, int]] = {}
    for kind, rows in (
        ("ended", list_serve_ended(store_root)),
        ("stderr", list_serve_stderr_logs(store_root)),
    ):
        gone = 0
        for row in rows[limit:]:
            try:
                Path(str(row.get("path"))).unlink()
            except OSError:
                # Including "still open by this very process" on Windows, where
                # an open file cannot be unlinked. Keeping it is the right
                # answer to that error, not a consolation prize.
                continue
            gone += 1
            deleted.append(
                {"pid": row.get("pid"), "path": row.get("path"), "kind": kind}
            )
        families[kind] = {
            "deleted_count": gone,
            "kept_count": max(0, len(rows) - gone),
        }
    return {
        "deleted": deleted,
        "deleted_count": len(deleted),
        "kept_count": sum(family["kept_count"] for family in families.values()),
        "families": families,
    }


def _ended_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    at = row.get("at")
    return (at if isinstance(at, str) else "", str(row.get("path") or ""))


# ── the service runtime's stderr (RL-19) ────────────────────────────────────
#
# A row says a runtime is here; a sidecar says why the last one left. Neither
# could say what the runtime was SAYING while it was here — and since RL-17 the
# launcher starts the service with all three stdio handles on ``DEVNULL``, so
# every traceback, warning and library gripe it writes to stderr goes nowhere at
# all. That is a real cost, paid on 2026-09-06 at 09:09:18Z: a local hub stream
# went silent for thirty seconds, the watchdog tore it down, and the runtime
# side of that half hour is unrecoverable because nothing was listening to the
# only channel it had.
#
# So a ``--service`` runtime keeps its own stderr, in this directory, next to
# the two records that name it. Three properties are load-bearing:
#
# * It is a TEXT log, not a record. Nothing parses it, nothing branches on it,
#   and it is never a contract — it is the channel the process already writes
#   to, kept.
# * It is per-pid and TRUNCATED at boot, exactly like the sidecar it sits
#   beside: one file per runtime, and a recycled pid overwrites, which is the
#   same trade RL-16 already made and the reason the header line exists.
# * The DIRECTORY is shared, so every reader of it must ignore this shape —
#   ``list_serve_instances`` does (the ``*.json`` glob cannot see a ``.log``,
#   and the name check says so out loud), and ``prune_serve_ended`` floors it
#   rather than ignoring it, because an unfloored log directory on a machine
#   that restarts its runtime a dozen times a day is the growth RL-16 already
#   refused to allow for reasons.


def serve_stderr_log_path(store_root: Path | str, pid: int) -> Path:
    return serve_instances_dir(store_root) / f"{int(pid)}{SERVE_STDERR_SUFFIX}"


def open_serve_stderr_log(
    store_root: Path | str,
    *,
    boot_id: str,
    pid: int | None = None,
    build: Any = None,
    at: str | None = None,
) -> Any:
    """Open this runtime's stderr log, header written. ``None`` if it cannot.

    Line-buffered and UTF-8 with ``errors="replace"``, both deliberately: a
    crash's last partial line is worth more than a tidy buffer, and a byte
    sequence some library wrote must never be able to raise inside a write to
    the log that exists to record crashes.

    Opened here rather than in ``serve.py`` for the same reason
    :func:`write_serve_ended` is written here: one writer for this directory,
    which is what keeps the header format and the retention ordering that reads
    it back in the same module. WIRING the handle to ``sys.stderr`` and to the
    logging root stays the caller's job — that is process policy, and this
    module owns files.

    Never raises. A runtime that cannot open its log still boots.
    """

    resolved_pid = int(pid if pid is not None else os.getpid())
    if isinstance(build, dict):
        commit = build.get("commit")
    else:
        commit = build
    header = " ".join(
        (
            SERVE_STDERR_HEADER_PREFIX,
            f"pid={resolved_pid}",
            f"boot_id={boot_id}",
            f"build={commit or 'unknown'}",
            f"started={at or _now_iso()}",
        )
    )
    path = serve_stderr_log_path(store_root, resolved_pid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(
            path,
            "w",
            buffering=1,
            encoding="utf-8",
            errors="replace",
            newline="\n",
        )
        handle.write(header + "\n")
    except Exception:
        return None
    return handle


def list_serve_stderr_logs(store_root: Path | str) -> list[dict[str, Any]]:
    """Every stderr log under *store_root*, NEWEST FIRST, each carrying ``path``.

    Ordered by the header line's own ``started=`` stamp and then by filename,
    never by mtime — same argument as :func:`list_serve_ended`, and sharper
    here: a log is APPENDED to for the life of its runtime, so its mtime says
    when that runtime last spoke, while the question the ordering answers is
    which runtime is oldest.

    A file with no readable header sorts oldest and is pruned first. That is the
    fail-safe direction for a log: a header-less file is either half-written or
    from a shape this code does not know, and neither is the record an operator
    is about to want.
    """

    rows: list[dict[str, Any]] = []
    directory = serve_instances_dir(store_root)
    try:
        entries = sorted(directory.glob(f"*{SERVE_STDERR_SUFFIX}"))
    except OSError:
        return rows
    for entry in entries:
        header = _read_first_line(entry)
        rows.append(
            {
                "path": str(entry),
                "pid": _pid_from_filename(Path(entry.name[: -len(SERVE_STDERR_SUFFIX)])),
                "started": _header_field(header, "started"),
                "boot_id": _header_field(header, "boot_id"),
                "header": header,
            }
        )
    rows.sort(key=_stderr_log_sort_key, reverse=True)
    return rows


def _stderr_log_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    started = row.get("started")
    return (started if isinstance(started, str) else "", str(row.get("path") or ""))


#: How much of a log to read looking for its header. The header is the first
#: line and is under 200 bytes; the cap is what stops a reader of a directory
#: from pulling a runtime's whole afternoon into memory to sort it.
_STDERR_HEADER_READ_BYTES = 4096


def _read_first_line(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(_STDERR_HEADER_READ_BYTES)
    except OSError:
        return ""
    return head.split("\n", 1)[0].strip()


def _header_field(header: str, key: str) -> str | None:
    """``key=value`` out of the header line, or ``None``. Never raises."""

    if not header.startswith(SERVE_STDERR_HEADER_PREFIX):
        return None
    for token in header.split():
        name, sep, value = token.partition("=")
        if sep and name == key:
            return value or None
    return None


# ── OS probes ───────────────────────────────────────────────────────────────


def default_process_probe() -> ProcessProbe:
    return ProcessProbe(
        alive=pid_alive, start_time=_process_start_time, cmdline=_process_cmdline
    )


def pid_alive(pid: int) -> bool | None:
    """Liveness WITHOUT signalling. None when it cannot be determined.

    Never ``os.kill(pid, 0)``: on Windows CPython routes signal 0 through
    ``GenerateConsoleCtrlEvent``, so the "is it alive" probe Ctrl-C's the
    target's whole console group (bpo-14484). ``gateway.status._pid_exists``
    is the repo's one correct answer to this question — read, not edited;
    ``dispatch_store.restore_undelivered_dispatches`` already depends on it.

    PUBLIC since the listener-start-path stage (R-L2), and that is the whole
    point of the rename: ``SocketOwnerLock`` has to decide "is the pid in the
    owner sidecar still running" and this classification's ``stale_dead_pid``
    answers the identical question one directory over. Two spellings of
    "is that process alive" would be two things to keep true, and the socket
    lock is precisely the caller that must not be the one that drifts — it
    hands a launcher the word ``took_over_from``.
    """

    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(int(pid)))
    except Exception:
        return None


def _process_start_time(pid: int) -> int | None:
    try:
        from gateway.status import get_process_start_time

        value = get_process_start_time(int(pid))
    except Exception:
        return None
    return None if value is None else int(value)


def _process_cmdline(pid: int) -> str | None:
    try:
        import psutil

        parts = psutil.Process(int(pid)).cmdline()
    except Exception:
        return None
    if not parts:
        return None
    return " ".join(str(part) for part in parts)


def _looks_like_serve(cmdline: str) -> bool:
    lowered = str(cmdline).lower()
    return all(token in lowered for token in _SERVE_CMDLINE_TOKENS)


# ── file helpers ────────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _pid_from_filename(path: Path) -> int | None:
    try:
        return int(path.stem)
    except (TypeError, ValueError):
        return None


def _argv_hint(argv: list[str]) -> str:
    tokens = [str(item) for item in argv[:_ARGV_HINT_TOKEN_CAP]]
    if tokens:
        tokens[0] = Path(tokens[0]).name
    return " ".join(tokens)[:_ARGV_HINT_CHAR_CAP]


def _now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _safe(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception:
        return None
