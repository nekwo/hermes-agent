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


def _run(requests, *, dispatch, pool_size: int = 4) -> list[dict]:
    out = io.StringIO()
    assert serve_loop(iter(requests), out, pool_size=pool_size, dispatch=dispatch) == 0
    return _frames(out)


SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"


def test_ready_line_and_exit_frames():
    def dispatch(argv):
        print(f"hello {argv[-1]}")
        print("tail without newline", end="")
        return 0

    frames = _run([_request("r1", ["harness", "status", "--json"]), SHUTDOWN], dispatch=dispatch)

    assert frames[0]["event"] == "ready"
    assert frames[0]["schema_version"] == 1
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
