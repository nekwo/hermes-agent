"""RL-19: a service runtime keeps its own stderr, and no reader trips over it.

Since RL-17 the launcher starts the runtime with all three stdio handles on
``DEVNULL``, which bought the thing it was for — a child that cannot block on a
pipe nobody reads — and cost the thing this file restores: everything the
runtime writes to stderr, its tracebacks included, went nowhere. On 2026-09-06
at 09:09:18Z a local hub stream went silent for thirty seconds and the watchdog
tore it down, and the runtime's side of that half hour did not exist.

So a ``--service`` runtime opens ``serve_instances/<pid>.stderr.log`` and keeps
it. Three claims live here, and they are separable:

1. **The log is a file with a shape** — per-pid, in the registry's own
   directory, opened through the registry's own helper, line-buffered UTF-8,
   and carrying a header line that names the runtime so a file read COLD (out
   of a QA bundle, with no row left to join it to) still says whose it was.
2. **Every reader of ``serve_instances/`` ignores it.** The directory now holds
   three file shapes. Same one-test-per-reader shape as RL-16's file, for the
   same reason: a phantom row is silent, because a pid-less record classifies
   ``unknown`` and ``unknown`` is the fail-safe direction.
3. **It is floored on boot, beside the reasons.** ``prune_serve_ended`` learned
   the second family; nothing consumes a log either, and a machine that
   restarts its runtime a dozen times a day would otherwise keep every one.

That the redirect actually CATCHES a real runtime's traceback is not provable
without a real process, and is proven in
``test_serve_stderr_log_child_e2e.py``. This file holds the seam.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_runtime.serve_registry import (
    SERVE_ENDED_RETENTION,
    SERVE_ENDED_SUFFIX,
    SERVE_STDERR_HEADER_PREFIX,
    SERVE_STDERR_SUFFIX,
    list_serve_instances,
    list_serve_stderr_logs,
    open_serve_stderr_log,
    prune_serve_ended,
    prune_stale_serve_instances,
    register_serve_instance,
    serve_instance_path,
    serve_instances_dir,
    serve_stderr_log_path,
    write_serve_ended,
)


def _log(root: Path, pid: int, *, started: str, body: str = "") -> Path:
    """A log as the runtime would have left it, at a chosen start time."""

    handle = open_serve_stderr_log(
        root, boot_id=f"boot{pid}", pid=pid, build="deadbee", at=started
    )
    assert handle is not None
    with handle:
        if body:
            handle.write(body)
    return serve_stderr_log_path(root, pid)


def _sidecar(root: Path, pid: int, *, at: str) -> None:
    assert write_serve_ended(
        root, reason="drained", boot_id=f"boot{pid}", pid=pid, at=at
    )


# ── 1. the file ─────────────────────────────────────────────────────────────


def test_the_log_is_named_by_pid_in_the_registrys_own_directory(tmp_path: Path) -> None:
    """RL-19 ruled the location, not just the fact: beside the row and the
    reason, through the registry's directory helper.

    *Killing mutation:* point the helper at a second directory and this fails —
    which is the point, since a second directory is a second thing to create,
    sandbox, exclude from every freshness fingerprint and remember to look in.
    """

    path = serve_stderr_log_path(tmp_path, 4242)
    assert path.name == f"4242{SERVE_STDERR_SUFFIX}"
    assert path.parent == serve_instances_dir(tmp_path)


def test_the_header_line_names_pid_boot_id_build_and_start(tmp_path: Path) -> None:
    """The whole reason the first line exists: a file read cold, days later,
    says which runtime it was.

    *Killing mutation:* drop any of the four fields and this fails.
    """

    handle = open_serve_stderr_log(
        tmp_path,
        boot_id="b" * 32,
        pid=4242,
        build={"commit": "f7b89826", "dirty": False},
        at="2026-09-06T09:09:18.000Z",
    )
    assert handle is not None
    handle.close()
    header = serve_stderr_log_path(tmp_path, 4242).read_text(encoding="utf-8").strip()
    assert header.startswith(SERVE_STDERR_HEADER_PREFIX)
    assert "pid=4242" in header
    assert f"boot_id={'b' * 32}" in header
    assert "build=f7b89826" in header
    assert "started=2026-09-06T09:09:18.000Z" in header


def test_an_unresolvable_build_still_leaves_a_readable_header(tmp_path: Path) -> None:
    """A build stamp that failed is the boot most worth having a log for. The
    header degrades to ``build=unknown``; it never becomes an exception."""

    handle = open_serve_stderr_log(tmp_path, boot_id="b1", pid=7, build=None)
    assert handle is not None
    handle.close()
    header = serve_stderr_log_path(tmp_path, 7).read_text(encoding="utf-8")
    assert "build=unknown" in header


def test_a_line_is_on_disk_before_the_handle_is_closed(tmp_path: Path) -> None:
    """Line buffering is the entire forensic value: the runtime this file
    exists for does not get to close its handle.

    *Killing mutation:* open with default buffering and the last line before a
    kill is still in a 8 KiB buffer that nobody flushes.
    """

    handle = open_serve_stderr_log(tmp_path, boot_id="b1", pid=8, at="2026-01-01T00:00:00.000Z")
    assert handle is not None
    try:
        handle.write("a traceback is arriving\n")
        text = serve_stderr_log_path(tmp_path, 8).read_text(encoding="utf-8")
    finally:
        handle.close()
    assert "a traceback is arriving" in text


def test_the_log_is_utf8_and_a_bad_byte_cannot_raise_into_the_runtime(
    tmp_path: Path,
) -> None:
    """It is opened UTF-8 with ``errors="replace"`` on purpose: a library that
    writes an unencodable character to stderr must not be able to take down the
    runtime through the log that exists to record its death."""

    handle = open_serve_stderr_log(tmp_path, boot_id="b1", pid=9, at="2026-01-01T00:00:00.000Z")
    assert handle is not None
    with handle:
        handle.write("naïve — 日本語\n")
        handle.write("\udcff surrogate\n")
    text = serve_stderr_log_path(tmp_path, 9).read_text(encoding="utf-8")
    assert "naïve — 日本語" in text
    assert "surrogate" in text


def test_an_unopenable_log_is_none_and_never_an_exception(tmp_path: Path) -> None:
    """Bookkeeping must never be the thing that fails a boot. A store root that
    is a FILE is the cheapest way to make the open fail for real."""

    blocked = tmp_path / "root"
    blocked.write_text("not a directory", encoding="utf-8")
    assert open_serve_stderr_log(blocked, boot_id="b1", pid=10) is None


# ── 2. one test per reader of serve_instances/ ──────────────────────────────


def test_the_registry_lister_ignores_the_stderr_log(tmp_path: Path) -> None:
    """Reader 1: ``list_serve_instances``, the one scan every other reader is
    built on.

    *Killing mutation:* widen the glob to ``*`` AND drop the suffix from
    ``_NON_ROW_SUFFIXES`` — either alone still excludes a ``.log`` — and this
    returns two rows, the second a pid-less ``unknown`` ghost.
    """

    register_serve_instance(tmp_path, pid=os.getpid(), boot_id="b1")
    _log(tmp_path, 4242, started="2026-09-06T09:00:00.000Z")
    rows = list_serve_instances(tmp_path)
    assert [row["pid"] for row in rows] == [os.getpid()]


def test_the_registry_lister_ignores_a_directory_of_logs_and_sidecars(
    tmp_path: Path,
) -> None:
    """The state of a machine whose last three runtimes died: no rows, three
    reasons, three logs. The honest reading is no runtimes."""

    for pid in (11, 22, 33):
        _sidecar(tmp_path, pid, at="2026-09-06T09:00:00.000Z")
        _log(tmp_path, pid, started="2026-09-06T09:00:00.000Z")
    assert list_serve_instances(tmp_path) == []


def test_a_json_glob_of_the_directory_cannot_see_a_stderr_log(tmp_path: Path) -> None:
    """The property the OTHER scanners lean on, pinned where it is decided.

    ``test_serve_socket_child_e2e``'s row-gone helper and every hand-rolled scan
    of this directory glob ``*.json`` and subtract the sidecar suffix. That is
    correct for the log only because a ``.log`` is not a ``.json`` — so if this
    file's suffix ever changes, this test is the one that says which other
    readers just started seeing ghosts.
    """

    _log(tmp_path, 4242, started="2026-09-06T09:00:00.000Z")
    assert SERVE_STDERR_SUFFIX.endswith(".log")
    matched = [
        entry
        for entry in serve_instances_dir(tmp_path).glob("*.json")
        if not entry.name.endswith(SERVE_ENDED_SUFFIX)
    ]
    assert matched == []


def test_socket_target_resolution_ignores_the_stderr_log(tmp_path: Path) -> None:
    """Reader 2: ``resolve_socket_target`` — the lane ``harness serve connect``
    and the launcher's attach both go through. A log read as a row is a row with
    no port, surfacing exactly where ``allow_stale=True`` names its refusal."""

    from agent_runtime.serve_socket import resolve_socket_target

    for pid in (11, 22):
        _log(tmp_path, pid, started="2026-09-06T09:00:00.000Z")
    assert resolve_socket_target(tmp_path) is None
    assert resolve_socket_target(tmp_path, allow_stale=True) is None


def test_the_boot_row_prune_ignores_the_stderr_log(tmp_path: Path) -> None:
    """Reader 3: ``prune_stale_serve_instances``, which runs on every boot —
    including the boot of the runtime whose log is open right now."""

    path = _log(tmp_path, 4242, started="2026-09-06T09:00:00.000Z")
    report = prune_stale_serve_instances(tmp_path)
    assert report == {"deleted": [], "kept": [], "deleted_count": 0}
    assert path.exists()


# ── 3. retention: newest twenty of EACH family ──────────────────────────────


def test_retention_keeps_the_newest_twenty_logs(tmp_path: Path) -> None:
    """*Killing mutation:* drop the stderr family from the prune and this
    directory keeps all 25 — the growth RL-16 already refused for reasons."""

    for index in range(25):
        _log(tmp_path, 1000 + index, started=f"2026-09-06T00:00:{index:02d}.000Z")
    report = prune_serve_ended(tmp_path)
    assert report["families"]["stderr"]["deleted_count"] == 5
    survivors = {row["pid"] for row in list_serve_stderr_logs(tmp_path)}
    assert survivors == set(range(1005, 1025))


def test_retention_floors_the_two_families_separately(tmp_path: Path) -> None:
    """Twenty of EACH, not twenty of forty.

    *Killing mutation:* pool them and twenty service boots evict every reason a
    non-service serve ever wrote — which is the record the launcher reads.
    """

    assert SERVE_ENDED_RETENTION == 20
    for index in range(25):
        _sidecar(tmp_path, 2000 + index, at=f"2026-09-06T00:00:{index:02d}.000Z")
        _log(tmp_path, 2000 + index, started=f"2026-09-06T00:00:{index:02d}.000Z")
    report = prune_serve_ended(tmp_path)
    assert report["families"] == {
        "ended": {"deleted_count": 5, "kept_count": 20},
        "stderr": {"deleted_count": 5, "kept_count": 20},
    }
    assert report["deleted_count"] == 10
    assert {row["kind"] for row in report["deleted"]} == {"ended", "stderr"}


def test_retention_orders_logs_by_the_header_stamp_not_by_mtime(
    tmp_path: Path,
) -> None:
    """A log is APPENDED to for the life of its runtime, so its mtime says when
    that runtime last spoke — while the question is which runtime is OLDEST.
    Written newest-first on disk, then the oldest is written to again, so an
    mtime-ordered prune deletes the wrong two."""

    for index in reversed(range(4)):
        _log(tmp_path, 3000 + index, started=f"2026-09-06T00:00:{index:02d}.000Z")
    with open(serve_stderr_log_path(tmp_path, 3000), "a", encoding="utf-8") as handle:
        handle.write("the oldest runtime is still talking\n")
    report = prune_serve_ended(tmp_path, keep=2)
    assert report["families"]["stderr"]["deleted_count"] == 2
    assert {row["pid"] for row in list_serve_stderr_logs(tmp_path)} == {3002, 3003}


def test_retention_never_touches_a_registry_row_or_the_live_runtimes_own_log(
    tmp_path: Path,
) -> None:
    """Three file shapes, one directory: the prune that bounds two must be blind
    to the third, and must not evict the log of the boot that is running it.

    The live log is the NEWEST of its family by construction (its header stamp
    is now), which is what keeps it — and on Windows it is also open, which the
    prune's ``OSError`` arm keeps rather than reports.
    """

    register_serve_instance(tmp_path, pid=777, boot_id="live")
    for index in range(25):
        _log(tmp_path, 4000 + index, started=f"2026-09-06T00:00:{index:02d}.000Z")
    live = open_serve_stderr_log(tmp_path, boot_id="live", pid=777, at="2026-09-06T23:59:59.000Z")
    assert live is not None
    try:
        prune_serve_ended(tmp_path, keep=1)
    finally:
        live.close()
    assert serve_instance_path(tmp_path, 777).exists()
    assert serve_stderr_log_path(tmp_path, 777).exists()


def test_a_log_with_no_readable_header_sorts_oldest_and_is_not_a_crash(
    tmp_path: Path,
) -> None:
    """Half-written, hand-edited, or from a shape this code does not know:
    still listable, still prunable, and pruned FIRST — a header-less file is
    not the record an operator is about to want."""

    serve_instances_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (serve_instances_dir(tmp_path) / f"10{SERVE_STDERR_SUFFIX}").write_bytes(
        b"\xff\xfe not a header at all"
    )
    _log(tmp_path, 11, started="2026-09-06T00:00:01.000Z")
    _log(tmp_path, 12, started="2026-09-06T00:00:02.000Z")
    rows = list_serve_stderr_logs(tmp_path)
    assert [row["pid"] for row in rows] == [12, 11, 10]
    assert rows[-1]["started"] is None
    report = prune_serve_ended(tmp_path, keep=2)
    assert report["families"]["stderr"]["deleted_count"] == 1
    assert {row["pid"] for row in list_serve_stderr_logs(tmp_path)} == {11, 12}


# ── 4. the loop's stderr proxy tees into the file ───────────────────────────


class _Sink:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def emit(self, frame: dict) -> None:
        self.frames.append(frame)


def _proxy(mirror=None):
    from hermes_cli.harness_parts.serve import _LineFrameProxy

    sink = _Sink()
    proxy = _LineFrameProxy(sink, "stderr")
    if mirror is not None:
        proxy.set_mirror(mirror)
    return proxy, sink


def test_the_stderr_proxy_tees_completed_lines_into_the_log(tmp_path: Path) -> None:
    """What the runtime SAYS while it is alive, not only how it dies.

    ``_service_log`` writes the transport's events — socket takeovers, the boot
    prune report — to ``sys.stderr``, which under ``--service`` is this proxy
    and a frame lane nobody is reading. The mirror is how those survive.

    *Killing mutation:* drop the mirror call from ``write`` and the file holds
    the header and nothing else, while the frames still look fine.
    """

    handle = open_serve_stderr_log(tmp_path, boot_id="b1", pid=21, at="2026-01-01T00:00:00.000Z")
    assert handle is not None
    proxy, sink = _proxy(handle)
    try:
        proxy.write('{"event":"serve_instances_pruned"}\n')
        proxy.write("half a line")
        proxy.flush_request(None)
        text = serve_stderr_log_path(tmp_path, 21).read_text(encoding="utf-8")
    finally:
        handle.close()
    assert '{"event":"serve_instances_pruned"}' in text
    assert "half a line" in text
    # The frame lane is untouched: this is a tee, not a redirect.
    assert [frame["line"] for frame in sink.frames] == [
        '{"event":"serve_instances_pruned"}',
        "half a line",
    ]


def test_a_proxy_with_no_mirror_behaves_exactly_as_before(tmp_path: Path) -> None:
    """Every non-service serve and every unit test is this arm. The mirror is
    ``None`` there and the proxy must not have grown a new way to fail."""

    proxy, sink = _proxy()
    proxy.write("a line\n")
    assert [frame["line"] for frame in sink.frames] == ["a line"]


def test_a_mirror_that_raises_cannot_break_a_print() -> None:
    """A full disk, a closed handle, a file the AV took a lock on: the log is
    forensics, and forensics must never be able to take down the runtime it is
    describing."""

    class _Broken:
        def write(self, text: str) -> int:
            raise OSError("no space left on device")

    proxy, sink = _proxy(_Broken())
    proxy.write("the runtime is still talking\n")
    assert [frame["line"] for frame in sink.frames] == ["the runtime is still talking"]


def test_the_listing_carries_the_pid_and_boot_id_of_each_log(tmp_path: Path) -> None:
    """What a QA bundle joins on: the filename says the pid, the header says
    which boot — two different facts, and a recycled pid needs both."""

    _log(tmp_path, 5001, started="2026-09-06T00:00:01.000Z")
    (row,) = list_serve_stderr_logs(tmp_path)
    assert row["pid"] == 5001
    assert row["boot_id"] == "boot5001"
    assert row["path"].endswith(f"5001{SERVE_STDERR_SUFFIX}")
