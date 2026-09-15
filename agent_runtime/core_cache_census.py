"""Read the demote receipts nobody was reading (BO-4).

=============================================================================
WHY THIS EXISTS
=============================================================================

``core_cache``'s own channel table says it plainly: "``CoreDecision.reason`` is
read by no caller today… a field census of WHY a cache demoted has exactly one
source: this line." The ``fingerprint_mismatch`` tail (``inputs= changed= diff=``)
is genuinely good instrumentation that nothing consumed — which is precisely how
a **self-invalidating cache ran unnoticed for months**: every serve boot rewrote
``serve_socket.owner.json`` and the delivery drain rewrote its mirror within
seconds of boot, so no boot's key could describe the store the next boot stat'd,
and the lane demoted on every same-commit boot from the day it shipped. It was
found by hand-diffing mtimes against a build window. The gap was never emission;
it was CONSUMPTION.

So this module executes the classification the table already states in prose. It
**reads and returns**: no file is opened for writing, no store is touched, no
snapshot is built. It is safe to point at a live serve's log.

=============================================================================
THE TABLE'S CENSUS RULES, AND HOW EACH IS HONOURED HERE
=============================================================================

These are not this module's opinions. Each is quoted from the row that owns it
in ``core_cache``'s channel table, and each is a way this census could lie:

* **"``absent`` is deliberately NOT logged … a census must not read 'no demote
  line' as 'no demote'."** The ordinary cold start would print a line on every
  build in every process, so it prints none. A window with zero demote lines is
  therefore UNMEASURED, not clean — and this census answers it with
  :data:`VERDICT_NO_LINES`, which the script turns into a non-zero exit. The
  ``core_source=cache`` count rides along as CONTEXT for exactly this reason: it
  is the only way to tell "the lane was consulted and hit" from "nothing asked".
* **"``fingerprint_mismatch`` ALONE grows a tail."** No other reason does, and a
  census that expected one would report every ``build_stamp_mismatch`` as a
  missing measurement. Only mismatch lines are looked at for a diff, and only
  mismatch lines can be bucketed as tail-absent.
* **"An arm that could not compute the diff says so in its own words rather
  than emitting an empty list, which would read as 'we looked and nothing
  moved'."** A ``diff_reason=`` line is counted as DIFF-UNAVAILABLE and can
  never reach a clean verdict; :data:`VERDICT_DIFF_UNAVAILABLE` exists so the
  distinction survives into the summary line an operator actually reads.
* **"The scope is ``last_pair`` BY CONSTRUCTION and that caveat is the row's
  most important sentence."** A demote diff is the delta since the last
  write-back, so on a busy store it TRUTHFULLY names files that are simply
  moving. The receipt is true without naming a defect. That is why this census
  buckets rather than counts: it is self-perturbation evidence ONLY when the
  named paths are ones the runtime itself writes.
* **"``home_mismatch`` is not an ordinary miss."** It says the two runs asked
  different QUESTIONS. On a multi-home install it is ordinary; on a
  single-profile operator boot it is a defect to go fix. The log cannot tell
  those apart, so this census FLAGS it and says which reading applies to which
  install rather than picking one.

=============================================================================
THE RUNTIME-AUTHORED LIST IS IMPORTED, NEVER SPELLED
=============================================================================

Same rule the fingerprint's own exclusion set is built on, and for the same
reason it was written there: ``"drain_state.json"`` once sat in that set
annotated "per ``dispatch_delivery.DRAIN_STATE_FILENAME``" while that constant
read ``dispatch_delivery_drain.json`` — so the exclusion named a file that has
never existed. A comment naming a constant is not a reference to it. The four
names the table lists are imported from the modules that own them.

``state.db-wal`` is the one entry with no owning constant for its full name (the
database name is configurable), so it is matched by the ``-wal`` SUFFIX through
``core_cache._WAL_SIBLING``. That is a deliberate widening of the table's
literal: a WAL sibling exists at all only because a process committed to SQLite,
which is the runtime writing, whichever database it is.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .core_cache import (
    CORE_SOURCE_CACHE,
    CORE_SOURCE_REBUILT,
    DEMOTE_FINGERPRINT_MISMATCH,
    DEMOTE_HOME_MISMATCH,
    DIFF_UNAVAILABLE,
    _WAL_SIBLING,
)
from .dispatch_delivery import DRAIN_STATE_FILENAME
from .serve_socket import SOCKET_LOCK_FILENAME, SOCKET_OWNER_FILENAME

#: The family token is this module's ONE literal, and it is the reason
#: ``test_core_cache_demote_census`` drives a real ``_log_demote`` through this
#: parser instead of a hand-typed line: a producer rename would otherwise leave
#: the census measuring a clean zero forever, which is the exact failure mode
#: the zero-parse guard exists for one level down.
FAMILY = "snapshot_core_cache"

_DEMOTE_ANCHOR = f"{FAMILY} core_source={CORE_SOURCE_REBUILT} "
_HIT_ANCHOR = f"{FAMILY} core_source={CORE_SOURCE_CACHE} "
#: The stale-first paint's own spelling — a cache read that was NOT served as
#: authority. It shares the hit anchor and is told apart by this marker, exactly
#: as ``take_stale_first_core`` emits it.
_STALE_MARKER = "stale=true"

#: Verbatim from the channel table's self-perturbation rule. Basenames, because
#: the receipt carries absolute paths from the fingerprint's own entries.
RUNTIME_AUTHORED_BASENAMES = frozenset(
    {DRAIN_STATE_FILENAME, SOCKET_OWNER_FILENAME, SOCKET_LOCK_FILENAME}
)

#: How a diff path is bucketed.
PATH_RUNTIME_AUTHORED = "runtime_authored"
PATH_STORE = "store"

#: The arm for a ``fingerprint_mismatch`` line that carries no tail at all —
#: code that predates the tail entirely. Its own token rather than borrowing
#: ``no_entries``: that reason says the diagnostic has not been WRITTEN yet and
#: resolves on the next write-back, while this says the line was emitted by a
#: producer that never had one. Both are "not measured"; neither is clean.
DIFF_TAIL_ABSENT = "tail_absent"

VERDICT_NO_LINES = "no_demote_lines_parsed"
VERDICT_SELF_PERTURBATION = "self_perturbation"
VERDICT_DIFF_UNAVAILABLE = "diff_unavailable"
VERDICT_STORE_CHURN = "store_churn"
VERDICT_NO_MISMATCH = "no_fingerprint_mismatch"

#: Bound on the distinct store paths carried back in the report. The
#: runtime-authored bucket is NOT capped — it is the finding, and truncating a
#: finding to keep a report tidy is how the report becomes the thing nobody
#: reads.
_STORE_PATH_SAMPLE = 20

DEFAULT_WINDOW = 500


def default_log_path() -> Path:
    """``<HERMES_HOME>/logs/agent.log`` — through the authority that owns it."""

    from hermes_constants import get_hermes_home

    from hermes_cli.logs import LOG_FILES

    return Path(get_hermes_home()) / "logs" / LOG_FILES["agent"]


def classify_path(path: str) -> str:
    """Runtime-authored, or a store path the operator's own writes touched?

    Basename matching: the receipt carries absolute paths out of the
    fingerprint's stat entries, and the table names files, not locations.
    """

    base = str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    if base in RUNTIME_AUTHORED_BASENAMES:
        return PATH_RUNTIME_AUTHORED
    if base.endswith(_WAL_SIBLING):
        return PATH_RUNTIME_AUTHORED
    return PATH_STORE


def parse_demote_line(message: str) -> dict[str, Any] | None:
    """One ``core_source=rebuilt`` receipt, or ``None`` if this is not one.

    ``diff=`` is read as EVERYTHING to the end of the line and split on ``,``,
    which is the producer's own contract: ``_diff_detail`` puts the field last
    precisely because "a path may contain spaces, so nothing can be
    field-parsed after it". Reading it as a whitespace field would silently
    truncate every diff at the first path with a space in it — and a truncated
    diff still looks like a measurement.
    """

    index = message.find(_DEMOTE_ANCHOR)
    if index < 0:
        return None
    tail = message[index + len(_DEMOTE_ANCHOR) :]
    diff_at = tail.find(" diff=")
    head = tail if diff_at < 0 else tail[:diff_at]
    fields: dict[str, str] = {}
    for token in head.split():
        if "=" in token:
            key, _, value = token.partition("=")
            fields[key] = value
    row: dict[str, Any] = {
        "caller": fields.get("caller", "unknown"),
        "reason": fields.get("reason", "unknown"),
        "inputs": fields.get("inputs", "unknown"),
        "diff_scope": fields.get("diff_scope"),
        "diff_reason": fields.get("diff_reason"),
        "changed": fields.get("changed"),
        "diff_paths": None,
    }
    if diff_at >= 0:
        raw = tail[diff_at + len(" diff=") :].strip()
        row["diff_paths"] = (
            None
            if raw == DIFF_UNAVAILABLE
            else [part.strip() for part in raw.split(",") if part.strip()]
        )
    return row


def census_demotes(
    lines: Iterable[str], *, window: int = DEFAULT_WINDOW
) -> dict[str, Any]:
    """The report. Reads; writes nothing; decides nothing about the store.

    ``window`` is the last N demote RECEIPTS, not the last N log lines: a serve
    log is mostly other traffic, so bounding by lines would make the window's
    size depend on how chatty the rest of the runtime happened to be.
    """

    scanned = 0
    demotes: list[dict[str, Any]] = []
    hits = 0
    stale_first = 0
    for raw in lines:
        scanned += 1
        row = parse_demote_line(raw)
        if row is not None:
            demotes.append(row)
            continue
        if _HIT_ANCHOR in raw:
            if _STALE_MARKER in raw:
                stale_first += 1
            else:
                hits += 1
    if window > 0:
        demotes = demotes[-window:]

    reasons = Counter(row["reason"] for row in demotes)
    runtime_authored: Counter[str] = Counter()
    store_paths: Counter[str] = Counter()
    unavailable_reasons: Counter[str] = Counter()
    mismatch = self_perturbing = store_only = unavailable = 0

    for row in demotes:
        # ONLY fingerprint_mismatch grows a tail. Asking any other reason for
        # one would report a measurement that was never owed — the table's own
        # words: a diff on a build_stamp_mismatch would name every file the
        # operator's upgrade touched and read as store churn.
        if row["reason"] != DEMOTE_FINGERPRINT_MISMATCH:
            continue
        mismatch += 1
        paths = row["diff_paths"]
        if paths is None:
            unavailable += 1
            unavailable_reasons[row["diff_reason"] or DIFF_TAIL_ABSENT] += 1
            continue
        buckets = Counter(classify_path(path) for path in paths)
        for path in paths:
            if classify_path(path) == PATH_RUNTIME_AUTHORED:
                runtime_authored[
                    str(path).replace("\\", "/").rsplit("/", 1)[-1]
                ] += 1
            else:
                store_paths[path] += 1
        if buckets[PATH_RUNTIME_AUTHORED]:
            self_perturbing += 1
        elif buckets[PATH_STORE]:
            store_only += 1
        else:
            # ``diff_scope=last_pair changed=N`` with an empty list is a shape
            # the producer refuses to emit (that refusal is C16's lesson, and
            # ``_demote_diff_detail`` has an arm for it). If one ever reaches
            # here it is a producer defect, and it is NOT clean.
            unavailable += 1
            unavailable_reasons[DIFF_TAIL_ABSENT] += 1

    if not demotes:
        verdict = VERDICT_NO_LINES
    elif self_perturbing:
        verdict = VERDICT_SELF_PERTURBATION
    elif unavailable:
        verdict = VERDICT_DIFF_UNAVAILABLE
    elif store_only:
        verdict = VERDICT_STORE_CHURN
    else:
        verdict = VERDICT_NO_MISMATCH

    return {
        "window": window,
        "lines_scanned": scanned,
        "demotes_parsed": len(demotes),
        # CONTEXT, deliberately outside the histogram: ``absent`` is never
        # logged, so these are the only evidence that the lane was asked at all.
        "context": {"cache_hits": hits, "stale_first_paints": stale_first},
        "reasons": dict(sorted(reasons.items())),
        "fingerprint_mismatch": {
            "lines": mismatch,
            "self_perturbation_lines": self_perturbing,
            "store_only_lines": store_only,
            "diff_unavailable_lines": unavailable,
            "diff_unavailable_reasons": dict(sorted(unavailable_reasons.items())),
            "runtime_authored_paths": dict(runtime_authored.most_common()),
            "store_paths": dict(store_paths.most_common(_STORE_PATH_SAMPLE)),
        },
        "home_mismatch_lines": reasons.get(DEMOTE_HOME_MISMATCH, 0),
        "verdict": verdict,
    }


def format_census(report: dict[str, Any], *, source: str = "-") -> str:
    """The operator-facing text. Never uses the word "clean" it has not earned."""

    mismatch = report["fingerprint_mismatch"]
    out: list[str] = [
        f"core-cache demote census — {source}",
        f"  scanned {report['lines_scanned']} log lines; "
        f"{report['demotes_parsed']} demote receipts in the last "
        f"{report['window']}",
        f"  context (NOT demotes): cache hits {report['context']['cache_hits']}, "
        f"stale-first paints {report['context']['stale_first_paints']}",
        f"  verdict: {report['verdict']}",
    ]

    if report["verdict"] == VERDICT_NO_LINES:
        out.append(
            "  NOT A CLEAN BILL. `absent` — the ordinary cold start — is "
            "deliberately never logged, so a window with no demote line is "
            "UNMEASURED, not demote-free. Check the log path, the window, and "
            "whether the log rotated."
        )
        return "\n".join(out)

    out.append("  reasons:")
    for reason, count in report["reasons"].items():
        out.append(f"    {reason}: {count}")

    out.append(
        f"  fingerprint_mismatch: {mismatch['lines']} "
        f"(self-perturbation {mismatch['self_perturbation_lines']}, "
        f"store-only {mismatch['store_only_lines']}, "
        f"diff unavailable {mismatch['diff_unavailable_lines']})"
    )

    if mismatch["runtime_authored_paths"]:
        out.append(
            "  ** SELF-PERTURBATION ** the cache demoted because the RUNTIME "
            "moved its own inputs:"
        )
        for path, count in mismatch["runtime_authored_paths"].items():
            out.append(f"    {path}: {count}")
        out.append(
            "    Fix at the WRITER or add the name to core_cache's exclusion "
            "set — never trust the cache harder."
        )

    if mismatch["diff_unavailable_lines"]:
        out.append(
            "  diff UNAVAILABLE on "
            f"{mismatch['diff_unavailable_lines']} mismatch line(s) — the "
            "producer said so in its own words; this is 'not measured', never "
            "'nothing moved':"
        )
        for reason, count in mismatch["diff_unavailable_reasons"].items():
            out.append(f"    {reason}: {count}")

    if mismatch["store_paths"]:
        out.append(
            "  store paths (last_pair scope — on a busy store these move for "
            "ordinary reasons and the cache is working as designed):"
        )
        for path, count in mismatch["store_paths"].items():
            out.append(f"    {path}: {count}")

    if report["home_mismatch_lines"]:
        out.append(
            f"  home_mismatch: {report['home_mismatch_lines']} — NOT an "
            "ordinary miss. The persisted pair was keyed under a different "
            "Hermes home than the reading process resolved, i.e. the two runs "
            "asked different questions. Ordinary on a multi-home install; on a "
            "SINGLE-PROFILE operator boot it is evidence a persona scope was "
            "live while a build stat'd — a defect to go fix."
        )

    return "\n".join(out)
