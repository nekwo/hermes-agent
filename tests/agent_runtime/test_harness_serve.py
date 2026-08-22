"""Tests for ``hermes harness serve --ndjson`` (serve_loop mechanics + one
real-dispatch integration pass). Design contract:
docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/harness-serve-design.md."""

from __future__ import annotations

import io
import json
import os
import threading
import time

from hermes_cli.harness_parts.serve import dispatch_argv, serve_loop


def _request(rid: str, argv: list[str]) -> str:
    return json.dumps({"id": rid, "argv": argv}) + "\n"


def _frames(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line]


def _run(requests, *, dispatch, pool_size: int = 4, **kwargs) -> list[dict]:
    out = io.StringIO()
    assert (
        serve_loop(iter(requests), out, pool_size=pool_size, dispatch=dispatch, **kwargs)
        == 0
    )
    return _frames(out)


SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"


def test_ready_line_and_exit_frames():
    def dispatch(argv):
        print(f"hello {argv[-1]}")
        print("tail without newline", end="")
        return 0

    frames = _run([_request("r1", ["harness", "status", "--json"]), SHUTDOWN], dispatch=dispatch)

    # ``booting`` is the FIRST frame, before any heavy boot work — the
    # launcher's supervisor tells a live cold boot from a wedged child by it
    # (2026-07-26 kill-loop incident). ``ready`` still follows once boot
    # completes; consumers that predate ``booting`` ignore unknown events.
    assert frames[0]["event"] == "booting"
    assert frames[0]["schema_version"] == 1
    assert frames[1]["event"] == "ready"
    assert frames[1]["schema_version"] == 1
    lines = [f for f in frames if f.get("event") == "line" and f.get("id") == "r1"]
    assert [f["line"] for f in lines] == ["hello --json", "tail without newline"]
    exits = [f for f in frames if f.get("event") == "exit"]
    assert exits == [{"id": "r1", "event": "exit", "code": 0}]
    assert frames[-1]["event"] == "shutdown"


def test_concurrent_requests_do_not_bleed_output():
    barrier = threading.Barrier(2, timeout=10)

    def dispatch(argv):
        rid = argv[-1]
        barrier.wait()  # force true overlap
        for index in range(40):
            print(f"{rid}:{index}")
        return 0

    frames = _run(
        [
            _request("a", ["harness", "noop", "a"]),
            _request("b", ["harness", "noop", "b"]),
            SHUTDOWN,
        ],
        dispatch=dispatch,
    )

    for frame in frames:
        if frame.get("event") != "line":
            continue
        rid = frame["id"]
        assert rid in {"a", "b"}
        assert frame["line"].startswith(f"{rid}:"), frame
    exit_codes = {f["id"]: f["code"] for f in frames if f.get("event") == "exit"}
    assert exit_codes == {"a": 0, "b": 0}


def test_liveness_pump_emits_busy_frames_while_a_turn_runs_unprompted():
    # Regression (2026-07-23 live incident): with pool workers deep in
    # chat-turn work the launcher saw NO frames for the whole turn and raised
    # the loud "Runtime offline" banner twice inside one healthy 4-minute
    # turn. The serve must prove life on its own cadence — nothing polls it
    # here (no ping op), yet typed `busy` frames must keep appearing while
    # the synthetic chat turn blocks a worker.
    import time

    started = threading.Event()
    release = threading.Event()

    def dispatch(argv):
        started.set()
        assert release.wait(10)
        return 0

    def requests():
        yield _request(
            "chat-1",
            ["harness", "mission-chat", "message", "--persona", "dev", "--message", "hi"],
        )
        assert started.wait(10)
        # Hold the turn across many pump intervals before letting it finish.
        time.sleep(0.5)
        release.set()
        yield SHUTDOWN

    out = io.StringIO()
    assert (
        serve_loop(
            requests(),
            out,
            dispatch=dispatch,
            liveness_pump_interval_seconds=0.05,
        )
        == 0
    )
    frames = _frames(out)

    busy = [f for f in frames if f.get("event") == "busy"]
    assert len(busy) >= 2, f"expected unprompted busy frames, got: {frames}"
    assert busy[0]["chat_turns"] == 1 and busy[0]["pending"] == 1
    # The turn still completes and the loop still shuts down cleanly.
    assert any(f.get("event") == "exit" and f.get("id") == "chat-1" for f in frames)
    assert frames[-1]["event"] == "shutdown"


def test_ping_reports_in_flight_chat_turns():
    started = threading.Event()
    release = threading.Event()

    def dispatch(argv):
        started.set()
        assert release.wait(10)
        return 0

    def requests():
        yield _request(
            "chat-1",
            ["harness", "mission-chat", "message", "--persona", "dev", "--message", "hi"],
        )
        assert started.wait(10)
        yield json.dumps({"op": "ping"}) + "\n"
        release.set()
        yield SHUTDOWN

    out = io.StringIO()
    assert serve_loop(requests(), out, dispatch=dispatch) == 0
    frames = _frames(out)

    busy = [f for f in frames if f.get("event") == "busy"]
    assert busy and busy[0]["chat_turns"] == 1 and busy[0]["pending"] == 1


def test_malformed_and_invalid_requests_emit_typed_errors():
    def dispatch(argv):  # pragma: no cover - must not be reached
        raise AssertionError("dispatch must not run for invalid requests")

    frames = _run(
        [
            "this is not json\n",
            json.dumps({"id": "", "argv": ["harness"]}) + "\n",
            json.dumps({"id": "x", "argv": []}) + "\n",
            json.dumps(["not", "an", "object"]) + "\n",
            SHUTDOWN,
        ],
        dispatch=dispatch,
    )

    errors = [f for f in frames if f.get("event") == "error"]
    assert len(errors) == 4
    assert {f["error"] for f in errors} == {"invalid_request"}
    assert not [f for f in frames if f.get("event") == "exit"]


def test_duplicate_in_flight_request_id_is_rejected():
    release = threading.Event()
    started = threading.Event()

    def dispatch(argv):
        started.set()
        assert release.wait(10)
        return 0

    def requests():
        yield _request("dup", ["harness", "status"])
        assert started.wait(10)
        yield _request("dup", ["harness", "status"])
        release.set()
        yield SHUTDOWN

    out = io.StringIO()
    assert serve_loop(requests(), out, dispatch=dispatch) == 0
    frames = _frames(out)

    errors = [f for f in frames if f.get("event") == "error"]
    assert [f["error"] for f in errors] == ["duplicate_request_id"]
    exits = [f for f in frames if f.get("event") == "exit"]
    assert len(exits) == 1


def test_shutdown_drains_in_flight_requests():
    release = threading.Event()
    started = threading.Event()

    def dispatch(argv):
        started.set()
        assert release.wait(10)
        print("finished after shutdown request")
        return 0

    def requests():
        yield _request("slow", ["harness", "status"])
        assert started.wait(10)
        release.set()
        yield SHUTDOWN

    out = io.StringIO()
    assert serve_loop(requests(), out, dispatch=dispatch) == 0
    frames = _frames(out)

    exits = [f for f in frames if f.get("event") == "exit"]
    assert exits == [{"id": "slow", "event": "exit", "code": 0}]
    assert frames[-1]["event"] == "shutdown"


def test_dispatch_exception_emits_error_frame_and_nonzero_exit():
    def dispatch(argv):
        raise RuntimeError("boom")

    frames = _run([_request("r1", ["harness", "status"]), SHUTDOWN], dispatch=dispatch)

    errors = [f for f in frames if f.get("event") == "error" and f.get("id") == "r1"]
    assert errors and errors[0]["error"] == "dispatch_failed"
    exits = [f for f in frames if f.get("event") == "exit"]
    assert exits[0]["id"] == "r1" and exits[0]["code"] != 0


def test_read_model_cache_replays_identical_poll():
    calls: list[list[str]] = []

    def dispatch(argv):
        calls.append(list(argv))
        print('{"open_tasks": 3}')
        return 0

    frames = _run(
        [
            _request("p1", ["harness", "status", "--json"]),
            _request("p2", ["harness", "status", "--json"]),
            SHUTDOWN,
        ],
        dispatch=dispatch,
        pool_size=1,  # serialize so p1's build lands before p2 resolves
        fingerprint=lambda: ("stable",),
    )

    assert len(calls) == 1  # second poll never re-dispatched
    p2_lines = [
        f["line"] for f in frames if f.get("event") == "line" and f.get("id") == "p2"
    ]
    assert p2_lines == ['{"open_tasks": 3}']
    exits = {f["id"]: f for f in frames if f.get("event") == "exit"}
    assert exits["p1"]["code"] == 0
    assert "served_from_cache" not in exits["p1"]  # builds are not stamped
    assert exits["p2"]["code"] == 0
    assert exits["p2"]["served_from_cache"] is True
    assert isinstance(exits["p2"]["cache_age_ms"], int)


def test_read_model_cache_invalidates_when_fingerprint_moves():
    calls: list[list[str]] = []
    fingerprints = iter([("gen-1",), ("gen-2",)])

    def dispatch(argv):
        calls.append(list(argv))
        print(f"build {len(calls)}")
        return 0

    frames = _run(
        [
            _request("p1", ["harness", "status", "--json"]),
            _request("p2", ["harness", "status", "--json"]),
            SHUTDOWN,
        ],
        dispatch=dispatch,
        pool_size=1,
        fingerprint=lambda: next(fingerprints),
    )

    assert len(calls) == 2  # store state moved -> sequence check forces rebuild
    exits = {f["id"]: f for f in frames if f.get("event") == "exit"}
    assert "served_from_cache" not in exits["p2"]
    p2_lines = [
        f["line"] for f in frames if f.get("event") == "line" and f.get("id") == "p2"
    ]
    assert p2_lines == ["build 2"]


def test_read_model_cache_ttl_bounds_out_of_band_staleness():
    # Git working trees / provider health live outside the fingerprint; the
    # TTL forces a rebuild even when the fingerprint holds. max_age=-1 makes
    # every entry already-expired without sleeping.
    calls: list[list[str]] = []

    def dispatch(argv):
        calls.append(list(argv))
        print("fresh")
        return 0

    frames = _run(
        [
            _request("p1", ["harness", "status", "--json"]),
            _request("p2", ["harness", "status", "--json"]),
            SHUTDOWN,
        ],
        dispatch=dispatch,
        pool_size=1,
        fingerprint=lambda: ("stable",),
        read_cache_max_age=-1.0,
    )

    assert len(calls) == 2
    exits = {f["id"]: f for f in frames if f.get("event") == "exit"}
    assert "served_from_cache" not in exits["p2"]


def test_read_model_cache_never_replays_failed_builds():
    codes = iter([1, 0])
    calls: list[list[str]] = []

    def dispatch(argv):
        calls.append(list(argv))
        code = next(codes)
        print(f"exit {code}")
        return code

    frames = _run(
        [
            _request("p1", ["harness", "snapshot", "--json"]),
            _request("p2", ["harness", "snapshot", "--json"]),
            SHUTDOWN,
        ],
        dispatch=dispatch,
        pool_size=1,
        fingerprint=lambda: ("stable",),
    )

    assert len(calls) == 2  # the failed build was not fossilized
    exits = {f["id"]: f for f in frames if f.get("event") == "exit"}
    assert exits["p1"]["code"] == 1
    assert exits["p2"]["code"] == 0
    assert "served_from_cache" not in exits["p2"]


def test_read_model_cache_ignores_non_poll_commands():
    calls: list[list[str]] = []

    def dispatch(argv):
        calls.append(list(argv))
        print("ran")
        return 0

    _run(
        [
            _request("t1", ["harness", "task", "show", "task_1", "--json"]),
            _request("t2", ["harness", "task", "show", "task_1", "--json"]),
            SHUTDOWN,
        ],
        dispatch=dispatch,
        pool_size=1,
        fingerprint=lambda: ("stable",),
    )

    assert len(calls) == 2  # only the status/snapshot poll lanes cache


def test_real_dispatch_status_json(isolate_agent_runtime_root):
    frames = _run(
        [_request("s1", ["harness", "status", "--json"]), SHUTDOWN],
        dispatch=dispatch_argv,
    )

    exits = [f for f in frames if f.get("event") == "exit"]
    assert exits == [{"id": "s1", "event": "exit", "code": 0}]
    body = "\n".join(
        f["line"] for f in frames if f.get("event") == "line" and f.get("id") == "s1"
    )
    payload = json.loads(body)
    assert isinstance(payload, dict) and payload


def test_real_dispatch_argv_parse_failure(isolate_agent_runtime_root):
    frames = _run(
        [_request("bad", ["harness", "definitely-not-a-command"]), SHUTDOWN],
        dispatch=dispatch_argv,
    )

    errors = [f for f in frames if f.get("event") == "error" and f.get("id") == "bad"]
    assert errors and errors[0]["error"] == "argv_parse_failed"
    exits = [f for f in frames if f.get("event") == "exit"]
    assert exits[0]["code"] == 2
    assert any(f.get("event") == "stderr" for f in frames)


def test_cancel_drops_a_queued_request_before_dispatch():
    """A request still queued behind the pool is cancellable: it must answer
    an exit frame with cancelled=true and its argv must NEVER dispatch (the
    Stage 13 wedged-child scenario — an abandoned scope mutation draining
    minutes later)."""
    started = threading.Event()
    release = threading.Event()
    dispatched: list[list[str]] = []

    def dispatch(argv):
        dispatched.append(list(argv))
        started.set()
        assert release.wait(10)
        return 0

    def requests():
        yield _request("r1", ["harness", "status", "--json"])
        assert started.wait(10)
        # pool_size=1: r1 owns the only worker, so r2 sits queued.
        yield _request("r2", ["harness", "realm", "use", "realm_x", "--json"])
        yield json.dumps({"op": "cancel", "id": "r2"}) + "\n"
        release.set()
        yield SHUTDOWN

    out = io.StringIO()
    assert serve_loop(requests(), out, pool_size=1, dispatch=dispatch) == 0
    frames = _frames(out)

    exits = {frame["id"]: frame for frame in frames if frame.get("event") == "exit"}
    assert exits["r2"]["cancelled"] is True
    assert exits["r2"]["code"] == 130
    assert exits["r1"]["code"] == 0
    assert dispatched == [["harness", "status", "--json"]]


def test_cancel_of_running_or_unknown_request_is_denied():
    """A running request is uninterruptible and an unknown id may already
    have finished — both answer cancel_denied with the state, telling the
    client the side effect may still land (the --issued-at replay guard is
    what makes that safe)."""
    started = threading.Event()
    release = threading.Event()

    def dispatch(argv):
        started.set()
        assert release.wait(10)
        return 0

    def requests():
        yield _request("r1", ["harness", "status", "--json"])
        assert started.wait(10)
        yield json.dumps({"op": "cancel", "id": "r1"}) + "\n"
        yield json.dumps({"op": "cancel", "id": "ghost"}) + "\n"
        yield json.dumps({"op": "cancel"}) + "\n"
        release.set()
        yield SHUTDOWN

    out = io.StringIO()
    assert serve_loop(requests(), out, dispatch=dispatch) == 0
    frames = _frames(out)

    denied = {frame["id"]: frame for frame in frames if frame.get("event") == "cancel_denied"}
    assert denied["r1"]["state"] == "running"
    assert denied["ghost"]["state"] == "unknown"
    errors = [frame for frame in frames if frame.get("event") == "error"]
    assert errors and errors[0]["error"] == "invalid_request"
    exits = {frame["id"]: frame for frame in frames if frame.get("event") == "exit"}
    assert exits["r1"]["code"] == 0 and "cancelled" not in exits["r1"]


def test_cancel_of_running_runtime_stream_is_cooperative_and_frees_worker():
    """The infinite read stream is the one running request that must cancel.

    With a one-worker pool, a cancelled stream must release the worker so a
    following status request runs; otherwise four Launcher reconnects exhaust
    the production pool permanently.
    """

    from agent_runtime.request_control import request_cancelled

    started = threading.Event()
    status_ran = threading.Event()

    def dispatch(argv):
        if argv[:2] == ["harness", "stream"]:
            started.set()
            while not request_cancelled():
                threading.Event().wait(0.01)
            return 0
        status_ran.set()
        return 0

    def requests():
        yield _request("stream-1", ["harness", "stream"])
        assert started.wait(10)
        yield json.dumps({"op": "cancel", "id": "stream-1"}) + "\n"
        yield _request("status-1", ["harness", "status", "--json"])
        assert status_ran.wait(10)
        yield SHUTDOWN

    out = io.StringIO()
    assert serve_loop(requests(), out, pool_size=1, dispatch=dispatch) == 0
    frames = _frames(out)

    accepted = [
        frame
        for frame in frames
        if frame.get("id") == "stream-1"
        and frame.get("event") == "cancel_accepted"
    ]
    assert accepted and accepted[0]["state"] == "running"
    exits = {frame["id"]: frame for frame in frames if frame.get("event") == "exit"}
    assert exits["stream-1"]["code"] == 0
    assert exits["status-1"]["code"] == 0


# --------------------------------------------------------------------------
# detached-dispatch delivery drain (WP-H2)
# --------------------------------------------------------------------------


def test_the_delivery_drain_runs_for_the_life_of_the_serve_process(monkeypatch):
    """Serve is the ONLY lane that can keep `wait: false`'s promise.

    A detached dispatch's answer is delivered by a drain that has to outlive the
    turn that dispatched it, so it belongs to the one long-lived process that
    hosts persona turns. Started after `ready` (a cold boot is never delayed by
    a delivery) and stopped before `shutdown` (it must not forge a turn into a
    process on its way down).
    """

    from agent_runtime import dispatch_delivery

    started = {}

    def fake_start(*, stop_event, **kwargs):
        started["stop_event"] = stop_event
        started["ready_seen"] = True
        return threading.Thread(target=lambda: None)

    monkeypatch.setattr(dispatch_delivery, "start_delivery_drain", fake_start)

    frames = _run([SHUTDOWN], dispatch=lambda argv: 0)

    assert started["ready_seen"] is True
    # Stopped by the same event the liveness pump uses, before the shutdown frame.
    assert started["stop_event"].is_set()
    assert frames[-1]["event"] == "shutdown"


def test_a_drain_that_cannot_start_does_not_take_the_runtime_down(monkeypatch):
    """Completions stay durable; the next boot picks them up."""

    from agent_runtime import dispatch_delivery

    def boom(**kwargs):
        raise RuntimeError("no drain today")

    monkeypatch.setattr(dispatch_delivery, "start_delivery_drain", boom)

    frames = _run([SHUTDOWN], dispatch=lambda argv: 0)

    assert frames[1]["event"] == "ready"
    assert frames[-1]["event"] == "shutdown"


def test_boot_reports_the_dispatches_it_settled(monkeypatch):
    """A dispatch whose process died is reclassified BEFORE the drain starts.

    Otherwise the sender waits forever on an answer nothing is left to produce.
    """

    from agent_runtime import dispatch_store

    monkeypatch.setattr(
        dispatch_store, "restore_undelivered_dispatches", lambda: {"restored": 2}
    )

    frames = _run([SHUTDOWN], dispatch=lambda argv: 0)

    assert frames[1]["event"] == "ready"
    assert frames[1]["dispatches_restored"] == 2


# ── Boot instrumentation (T5/T9) ────────────────────────────────────────────
#
# A cold boot is the one worth attributing and the one nobody is watching a
# console for, so the boot stamps itself into the frames a supervisor already
# reads.


def test_booting_frame_carries_the_interpreter_stamp():
    """``booting`` is emitted before any heavy work, so its stamp is exactly
    the interpreter + CLI import tax — the cold-boot term a supervisor can see
    no other way."""

    from agent_runtime.boot_timeline import BootTimeline

    timeline = BootTimeline(process_start_monotonic=time.monotonic() - 4.0)
    frames = _run([SHUTDOWN], dispatch=lambda argv: 0, boot_timeline=timeline)

    booting = frames[0]
    assert booting["event"] == "booting"
    assert 3900 <= booting["boot"]["interpreter_ms"] <= 4300


# BW-0: ``interpreter_ms`` alone was one opaque 20-second number on the
# 2026-08-17 cold boot. These two cases pin that the loop puts its NAMED SEGMENTS
# on the same frame, derived from the CLI's own anchors and from the same
# process-creation anchor ``interpreter_ms`` uses.


def test_booting_frame_carries_the_named_segments_of_the_interpreter_stamp(monkeypatch):
    """The segments are the exact spans between the injected anchors.

    Anti-vacuity. *Mutation:* stamp ``main_import_ms: 0`` (or any constant)
    unconditionally. *Probed fields:* the three segment VALUES, each computed in
    this test from two instants the production code never sees — the injected
    ``process_start_monotonic`` and the three ``_boot_clock`` anchors this test
    writes. *Why the mutation cannot also set them:* a constant cannot equal
    three different derived spans, and the sibling case below re-derives them
    from a second, disjoint set of anchors.
    """

    from agent_runtime.boot_timeline import BootTimeline
    from hermes_cli import _boot_clock

    now = time.monotonic()
    process_start = now - 30.0
    monkeypatch.setattr(_boot_clock, "MAIN_IMPORT_STARTED", process_start + 2.5)
    monkeypatch.setattr(_boot_clock, "MAIN_IMPORT_COMPLETED", process_start + 6.0)
    monkeypatch.setattr(_boot_clock, "MAIN_ENTERED", process_start + 6.125)
    monkeypatch.setattr(_boot_clock, "BYTECODE_SWEEP_MS", 913)
    monkeypatch.setattr(_boot_clock, "HARNESS_PARSER_MS", 2211)

    timeline = BootTimeline(process_start_monotonic=process_start)
    dispatch_reached = timeline.started_monotonic
    frames = _run([SHUTDOWN], dispatch=lambda argv: 0, boot_timeline=timeline)

    boot = frames[0]["boot"]
    assert boot["interpreter_boot_ms"] == 2500
    assert boot["main_import_ms"] == 3500
    assert boot["dispatch_ms"] == int(
        (dispatch_reached - (process_start + 6.125)) * 1000
    )
    assert boot["bytecode_sweep_ms"] == 913
    assert boot["harness_parser_ms"] == 2211
    # The parts decompose the whole they sit beside — that is the point of the
    # split, and it only holds if every segment is measured off the SAME
    # process-creation anchor ``interpreter_ms`` is.
    assert boot["main_import_ms"] <= boot["interpreter_ms"]
    assert (
        boot["interpreter_boot_ms"] + boot["main_import_ms"] + boot["dispatch_ms"]
        <= boot["interpreter_ms"]
    )


def test_an_unmeasured_segment_is_absent_from_the_booting_frame(monkeypatch):
    """Anti-vacuity's other half: absence, not a fabricated zero.

    *Mutation:* have the annotation fill in 0 for anchors it could not read.
    *Probed field:* key membership on the emitted frame. A process that never
    went through ``main()`` — which is every ``serve_loop`` unit test, and any
    embedding of the loop — has no module anchors, and the frame must say so by
    omission. ``interpreter_ms`` itself keeps the same contract for the psutil
    anchor, so this is the existing rule applied to the new keys, not a new one.
    """

    from agent_runtime.boot_timeline import BootTimeline
    from hermes_cli import _boot_clock

    monkeypatch.setattr(_boot_clock, "MAIN_IMPORT_STARTED", None)
    monkeypatch.setattr(_boot_clock, "MAIN_IMPORT_COMPLETED", None)
    monkeypatch.setattr(_boot_clock, "MAIN_ENTERED", None)
    monkeypatch.setattr(_boot_clock, "BYTECODE_SWEEP_MS", None)
    monkeypatch.setattr(_boot_clock, "HARNESS_PARSER_MS", None)

    timeline = BootTimeline(process_start_monotonic=time.monotonic() - 1.0)
    frames = _run([SHUTDOWN], dispatch=lambda argv: 0, boot_timeline=timeline)

    boot = frames[0]["boot"]
    for key in (
        "interpreter_boot_ms",
        "main_import_ms",
        "dispatch_ms",
        "bytecode_sweep_ms",
        "harness_parser_ms",
    ):
        assert key not in boot, key
    # The pre-existing stamps are untouched by the absence.
    assert boot["interpreter_ms"] >= 900


def test_ready_frame_attributes_every_boot_phase():
    from agent_runtime.boot_timeline import BootTimeline

    timeline = BootTimeline(process_start_monotonic=time.monotonic() - 1.0)
    frames = _run([SHUTDOWN], dispatch=lambda argv: 0, boot_timeline=timeline)

    ready = frames[1]
    assert ready["event"] == "ready"
    stamps = ready["boot_timeline"]
    # Every phase between ``booting`` and ``ready`` is named, so a slow boot
    # says WHICH step was slow instead of only that it was slow.
    for phase in (
        "chat_registry_ms",
        "head_publish_ms",
        "store_root_ms",
        "orphaned_turn_sweep_ms",
        "dispatch_restore_ms",
    ):
        assert phase in stamps, phase
        assert stamps[phase] >= 0
    assert stamps["interpreter_ms"] >= 900
    assert stamps["total_ms"] >= stamps["elapsed_ms"]


def test_a_slow_boot_phase_shows_up_in_its_own_stamp(monkeypatch):
    """The stamps measure the phase they name — pinned by making one phase
    genuinely slow and reading it back off the frame."""

    from agent_runtime import dispatch_store

    def slow_restore():
        time.sleep(0.25)
        return {"restored": 0}

    monkeypatch.setattr(
        dispatch_store, "restore_undelivered_dispatches", slow_restore
    )

    frames = _run([SHUTDOWN], dispatch=lambda argv: 0)

    stamps = frames[1]["boot_timeline"]
    assert stamps["dispatch_restore_ms"] >= 200
    assert stamps["orphaned_turn_sweep_ms"] < 200


# ── Read-model prewarm (T2) ─────────────────────────────────────────────────


def test_the_snapshot_prewarm_starts_before_ready_is_announced():
    """Only the build that STARTED FIRST can be shared. The launcher's first
    request lands within milliseconds of ``ready``, so a prewarm started after
    that frame loses the race and queues a redundant second build behind the
    request it was supposed to spare."""

    started = threading.Event()
    ready_seen = threading.Event()
    order: list[str] = []

    def prewarm():
        order.append("prewarm")
        started.set()

    def dispatch(argv):
        return 0

    out = io.StringIO()

    class _WatchingWriter:
        def write(self, payload):
            if '"ready"' in payload:
                # The prewarm must already have been handed its thread.
                assert started.wait(5)
                order.append("ready")
                ready_seen.set()
            return out.write(payload)

        def flush(self):
            return out.flush()

    assert (
        serve_loop(
            iter([SHUTDOWN]),
            _WatchingWriter(),
            dispatch=dispatch,
            snapshot_prewarm=prewarm,
        )
        == 0
    )
    assert ready_seen.wait(5)
    assert order == ["prewarm", "ready"]


def test_no_prewarm_is_started_when_none_is_injected():
    """The loop is mechanism; the warmup is policy the entry point supplies.
    A unit-test loop must never fire a multi-second projection build — nor, since
    EG-3.2, import the OpenAI SDK to observe a ready frame."""

    frames = _run([SHUTDOWN], dispatch=lambda argv: 0)

    assert [f["event"] for f in frames[:2]] == ["booting", "ready"]
    thread_names = {thread.name for thread in threading.enumerate()}
    # Both historical names: the two prewarm threads became one, and a test that
    # only knew the old snapshot name would pass against a loop that still
    # started the provider warmup unconditionally.
    assert "harness-serve-snapshot-prewarm" not in thread_names
    assert "harness-serve-prewarm" not in thread_names


def test_the_serve_entry_point_wires_the_real_prewarm_and_timeline(monkeypatch):
    """The default is OFF in the loop, so the production wiring is what makes
    the prewarm real — pin it, or the whole item ships dead."""

    from hermes_cli.harness_parts import serve as serve_mod

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(serve_mod, "_claim_protocol_pipes", lambda: (read_fd, write_fd))

    captured: dict = {}

    def fake_serve_loop(reader, writer, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(serve_mod, "serve_loop", fake_serve_loop)

    class _Args:
        ndjson = True
        pool_size = 4

    assert serve_mod._cmd_serve(_Args()) == 0
    assert captured["snapshot_prewarm"] is serve_mod._prewarm_read_model_snapshot
    # EG-3.2's injectable-parameter contract (HC-H3): the provider warmup is
    # policy the entry point supplies, exactly like the read-model one. Pinned
    # here because the loop's default is OFF — a wiring that forgot it would ship
    # a serve whose first chat turn pays the whole SDK import inline, with every
    # loop test still green.
    assert captured["provider_prewarm"] is serve_mod._prewarm_provider_runtime
    assert captured["boot_timeline"] is not None
    # Started at the command's first instruction: everything before it is
    # interpreter + import tax, which is what the term is supposed to mean.
    assert captured["boot_timeline"].interpreter_ms is None or (
        captured["boot_timeline"].interpreter_ms >= 0
    )


# ── Provider prewarm follows the first build (EG-3.2 = HY-H2 = HC-H3) ───────


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _gated_prewarm_run(*, snapshot_raises: bool = False):
    """Drive ``serve_loop`` to ``ready`` with the read-model prewarm GATED.

    The gate is the test's, and nothing in the loop can release it — which is
    what makes "the provider prewarm has not started" an observation rather than
    a race (the BW-L5 never-completing-fake pattern). Returns the levers.
    """

    gate = threading.Event()
    entered = threading.Event()
    provider_calls: list[str] = []
    out = io.StringIO()

    def snapshot_prewarm():
        entered.set()
        if snapshot_raises:
            raise RuntimeError("the build blew up")
        assert gate.wait(10)

    def provider_prewarm():
        provider_calls.append("provider")

    result: dict = {}

    def run():
        result["code"] = serve_loop(
            iter([SHUTDOWN]),
            out,
            dispatch=lambda argv: 0,
            snapshot_prewarm=snapshot_prewarm,
            provider_prewarm=provider_prewarm,
        )

    loop = threading.Thread(target=run, name="eg32-serve-loop")
    loop.start()
    assert entered.wait(10)
    return gate, provider_calls, out, loop, result


def test_the_provider_prewarm_does_not_begin_until_the_snapshot_prewarm_returns():
    """The first build gets the process to itself.

    Two daemon threads used to race from ``ready``: the read-model build the
    launcher's canvas waits on, and ~5-8s of provider warmup CPU (SDK import, SSL
    context, tool registry) which under the GIL was subtracted from it. Nothing
    the provider warmup produces is consumable before the canvas is
    authoritative.

    *Killing mutation:* restore the two parallel threads. *Probed field:* the
    recorder's LENGTH at the gated instant — the fake the test owns cannot be
    un-appended, and the mutant cannot release the gate. No elapsed-ms assertion
    exists here or anywhere in this stage.
    """

    gate, provider_calls, out, loop, result = _gated_prewarm_run()
    try:
        # The build is provably pending: the gate is unreleased and held by us.
        assert provider_calls == []
        # And it stays empty — a parallel mutant would append within microseconds
        # of ready, so this is where it dies.
        time.sleep(0.2)
        assert provider_calls == []
    finally:
        gate.set()
    assert _wait_for(lambda: provider_calls == ["provider"])
    loop.join(10)
    assert not loop.is_alive()
    assert result["code"] == 0
    # Exactly one, on the one thread: the sequential worker must not also leave
    # the old independent start behind.
    assert provider_calls == ["provider"]


def test_ready_is_emitted_before_either_prewarm_completes():
    """The boot must not slow down to buy the ordering.

    *Mutation:* join the prewarm thread before emitting ready. *Probed field:*
    the frame order on the wire while the gate is still held — a joining mutant
    cannot reach the emit at all, so the loop never returns and this reads no
    ready frame.
    """

    gate, provider_calls, out, loop, result = _gated_prewarm_run()
    try:
        # The loop ran to completion — booting, ready, shutdown — while the
        # read-model prewarm is still parked inside the test's gate.
        loop.join(10)
        assert not loop.is_alive()
        frames = _frames(out)
        assert [f["event"] for f in frames[:2]] == ["booting", "ready"]
        assert frames[-1]["event"] == "shutdown"
        assert provider_calls == []
    finally:
        gate.set()


def test_a_failed_build_still_warms_the_providers():
    """Sequential must not mean conditional. The steps are isolated, so a build
    that raised still leaves the first chat turn warm — the property the two
    independent threads had for free and a naive `then` would have silently
    dropped."""

    gate, provider_calls, out, loop, result = _gated_prewarm_run(snapshot_raises=True)
    gate.set()
    assert _wait_for(lambda: provider_calls == ["provider"])
    loop.join(10)
    assert result["code"] == 0


def test_a_failing_prewarm_never_takes_the_runtime_down(monkeypatch):
    from hermes_cli.harness_parts import serve as serve_mod
    from agent_runtime import snapshot as snapshot_mod

    def boom(**_kwargs):
        raise RuntimeError("store torn mid-read")

    monkeypatch.setattr(snapshot_mod, "build_snapshot", boom)

    # Best effort by contract: it swallows, logs, and the serve keeps serving.
    serve_mod._prewarm_read_model_snapshot()
