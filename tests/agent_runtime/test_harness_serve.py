"""Tests for ``hermes harness serve --ndjson`` (serve_loop mechanics + one
real-dispatch integration pass). Design contract:
docs/agent-runtime-harness/harness-serve-design.md."""

from __future__ import annotations

import io
import json
import threading

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
