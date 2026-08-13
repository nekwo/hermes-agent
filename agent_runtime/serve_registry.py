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
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "CLASSIFICATION_LIVE",
    "CLASSIFICATION_STALE_DEAD_PID",
    "CLASSIFICATION_STALE_RECYCLED_PID",
    "CLASSIFICATION_UNKNOWN",
    "SERVE_INSTANCES_DIRNAME",
    "SERVE_INSTANCE_SCHEMA_VERSION",
    "ProcessProbe",
    "ServeInstanceRegistration",
    "default_process_probe",
    "list_serve_instances",
    "prune_stale_serve_instances",
    "register_serve_instance",
    "serve_instance_path",
    "serve_instances_dir",
    "unregister_serve_instance",
]

SERVE_INSTANCES_DIRNAME = "serve_instances"
SERVE_INSTANCE_SCHEMA_VERSION = 1

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
) -> ServeInstanceRegistration:
    """Announce this serve under *store_root*. Best effort, always reported."""

    resolved_pid = int(pid if pid is not None else os.getpid())
    resolved_boot_id = boot_id or uuid.uuid4().hex
    path = serve_instance_path(store_root, resolved_pid)
    prober = probe or default_process_probe()
    record = {
        "schema_version": SERVE_INSTANCE_SCHEMA_VERSION,
        "pid": resolved_pid,
        "boot_id": resolved_boot_id,
        "transport": transport,
        "store_root": str(store_root),
        "started_at": _now_iso(),
        # The identity baseline the recycled-pid check compares against. None
        # when the OS would not say — recorded as null so the reader knows the
        # check is unavailable rather than passing by default.
        "started_at_ticks": _safe(lambda: prober.start_time(resolved_pid)),
        "argv_hint": _argv_hint(argv if argv is not None else list(sys.argv)),
        "build": dict(build) if isinstance(build, dict) else None,
    }
    try:
        _atomic_write_json(path, record)
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
    frame and left its registry entry behind, so the registry advertised a serve
    that had just gone. The lock was held for ~16ms there; one retry cleared it.
    FileNotFoundError is NOT retried — already gone is a state, not a failure.

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
        entries = sorted(directory.glob("*.json"))
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


# ── OS probes ───────────────────────────────────────────────────────────────


def default_process_probe() -> ProcessProbe:
    return ProcessProbe(
        alive=_pid_alive, start_time=_process_start_time, cmdline=_process_cmdline
    )


def _pid_alive(pid: int) -> bool | None:
    """Liveness WITHOUT signalling. None when it cannot be determined.

    Never ``os.kill(pid, 0)``: on Windows CPython routes signal 0 through
    ``GenerateConsoleCtrlEvent``, so the "is it alive" probe Ctrl-C's the
    target's whole console group (bpo-14484). ``gateway.status._pid_exists``
    is the repo's one correct answer to this question — read, not edited;
    ``dispatch_store.restore_undelivered_dispatches`` already depends on it.
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


def _atomic_write_json(path: Path, record: dict[str, Any]) -> None:
    """tmp + rename, so a reader never sees a half-written record."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, default=str, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(payload)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


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
