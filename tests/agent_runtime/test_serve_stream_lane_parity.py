"""TC-1 / EG-4.1 — the serve hub's stream lane, made provably launcher-grade.

The lane has existed and been dialled by NOBODY: no ``{"op":"subscribe"}`` is
sent from anywhere in the launcher's Dart tree, so every claim about it has been
a claim about code nothing exercises. Plan
``SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md`` §V2 lists what has to be TRUE
before a client may be pointed at it, and this file is that list turned into
tests. Nothing here changes a default: the launcher still runs the argv stream,
and EG-4.2 is the stage that subscribes this lane.

The four gaps, and what each is pinned with
-------------------------------------------
* **C-1, op advertisement.** A client cannot gate on a lane it cannot detect,
  and a probe-subscribe is not a detector: ``unsupported_lane``, ``draining``
  and ``already_subscribed`` are all answers a LIVE lane gives, so "the runtime
  refused me" and "the runtime is too old to have the lane" are the same
  observation. The ops now advertise themselves under ``"ops"`` on the frames
  ``"rpc"`` already rides. Pinned both ways round: every advertised op is
  answered by the dispatcher, and every op the dispatcher answers is advertised
  (the second half is a source-level gate, because an unadvertised op is
  invisible to exactly the test that would otherwise catch it).
* **Byte parity.** The hub's frames must be the argv stream's frames rather than
  a second contract to keep in sync. Proven at the BYTES, over one seeded root
  and one scripted event sequence, with both lanes running CONCURRENTLY against
  the same event log — which is the only arrangement in which the two can be
  compared at all (a hydrate taken before an append and one taken after it carry
  different watermarks by construction, so a sequential comparison would be
  measuring the seam instead of the producers).
* **C-2, interleaving/backpressure.** Stream frames now share one connection
  with RPC replies and office notifications. The assumption on record is "the
  per-connection writer is a single ordered queue and a fat frame delays but
  never corrupts". Both halves are tested, and so is the failure mode that
  matters most to EG-4.2: a stream subscriber dropped for backpressure must not
  take the method lane on the same connection with it.
* **Re-subscribe recovery.** A drop is only survivable if re-subscribing
  re-baselines, so the recovery is asserted as hydrate-FIRST with a watermark
  that did not go backwards — and the office lane's restart-free join (O-H5) is
  shown not to suppress it.

Fixtures: NONE are added. The parity comparison needs volatile stamps removed,
and the normalizer it uses is the one the committed goldens are generated with
(``scripts/generate_agent_runtime_stream_fixtures.py:_normalize``), imported
rather than copied — a second normalizer here could disagree with the one that
wrote ``tests/fixtures/stream_frames/`` and the disagreement would look like
parity. The goldens themselves are read (never written) as the third leg: the
frames this lane delivers must have the committed hydrate's shape.

Threads: every wait is a bounded poll on a CONDITION, never a bare sleep, and
every test that attaches the REAL producer drains it before returning — see
:func:`_drain_stream_producers` for why that is not politeness.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_runtime import paths
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.patch_coverage import parse_fold_entities_option
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import SNAPSHOT_CONTRACT_VERSION
from agent_runtime.stream import (
    STREAM_PATCH_SCHEMA_VERSION,
    STREAM_SCHEMA_VERSION,
    stream_frames,
)
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop
from tests.agent_runtime.test_serve_socket_lane import (
    WAIT,
    _Sink,
    _StallingSink,
    _StdioPipe,
    _read_until,
    client,
    running_serve,
)
from tests.agent_runtime.test_stream_contract_fixture import FIXTURES, _shape_drift

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "generate_agent_runtime_stream_fixtures.py"
SERVE_SOURCE = Path(serve_module.__file__)

#: The declaration BOTH lanes make in this file. It is the launcher's own
#: shipped set (``kMissionFoldDeclaredEntities``'s first two entries) written the
#: two ways the two lanes spell it — the argv flag's comma string and the
#: subscribe op's list — because a parity test whose two sides were handed the
#: same Python object would not be testing the two declaration paths.
DECLARED_FLAG = "persona_instance,incident"
DECLARED_LIST = ["persona_instance", "incident"]


# ── helpers ─────────────────────────────────────────────────────────────────


def _generator_module():
    """The fixture generator, for its ``_normalize`` — the goldens' own."""

    spec = importlib.util.spec_from_file_location(
        "_stream_fixture_generator_for_parity", GENERATOR
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical(frame: dict) -> str:
    """One frame as comparable bytes: volatile stamps out, compact separators.

    The separators are ``harness stream``'s own
    (``runtime_commands._cmd_stream``). They are NOT the serve writer's — see
    :func:`test_the_two_lanes_encode_the_same_frame_with_different_whitespace`,
    which records that difference rather than letting this helper hide it.
    """

    normalized = _generator_module()._normalize(frame, isolated_root=paths.store_root())
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _seed_events(count: int = 2, *, start: int = 0) -> None:
    log = EventLog()
    for index in range(start, start + count):
        log.append(
            Event(
                ts=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
                + timedelta(seconds=index),
                type="state.reconciled",
                task_id=f"parity_task_{index}",
                run_id=None,
                persona_id="dev",
                payload={"fingerprint": f"parity-fp-{index}"},
            )
        )


def _until(predicate, *, what: str, timeout: float = WAIT):
    """Poll *predicate* to a bounded deadline; return its first truthy value."""

    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for: {what}")
        time.sleep(0.01)


def _live_producer_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("serve-stream-producer-") and thread.is_alive()
    ]


def _drain_stream_producers() -> None:
    """Wait out every real hub producer this test started, and PROVE it.

    Not politeness — correctness of the suite. ``StreamHub.stop()`` cannot
    interrupt a generator parked inside ``next()``, and the real
    ``stream_frames`` producer parks in its poll loop for up to a heartbeat
    (5s) between frames. A producer still parked when the test returns outlives
    ``isolate_agent_runtime_root``'s monkeypatched root, and its next read
    resolves against the OPERATOR's live store — the leak class EG-0.1's
    teardown tripwire exists to catch, arriving here by a route that tripwire
    cannot see (it watches the pins, not the threads).

    One appended event is what makes the wait short: it wakes the generator, the
    producer takes its next lap, sees the stop event the empty room already set,
    and exits. Then this asserts the threads are actually gone, so a future
    change that makes them un-stoppable reds HERE instead of leaking silently.
    """

    if not _live_producer_threads():
        return
    _seed_events(1, start=900)
    _until(
        lambda: not _live_producer_threads(),
        what="every serve-stream-producer thread to exit",
    )


@contextmanager
def _stdio_serve(**kwargs):
    """A stdio-only serve on the real ``serve_loop``, torn down at block exit.

    ``running_serve`` in ``test_serve_socket_lane`` is the socket-lane twin;
    stdio gets its own because the lane EG-4.2 subscribes is the serve CHILD's
    inherited pipe, and "one ordered writer" is a claim about ``_FrameWriter``
    on that pipe, not about the socket's per-connection sink.
    """

    pipe = kwargs.pop("pipe", None) or _StdioPipe()
    sink = kwargs.pop("sink", None) or _Sink()
    kwargs.setdefault("dispatch", lambda argv: 0)
    kwargs.setdefault("liveness_pump_interval_seconds", 60.0)
    exits: dict[str, Any] = {}

    def _run() -> None:
        exits["code"] = serve_loop(pipe, sink, **kwargs)

    thread = threading.Thread(target=_run, name="serve-under-test", daemon=True)
    thread.start()
    try:
        sink.wait_for("ready")
        yield pipe, sink
    finally:
        pipe.close()
        thread.join(WAIT)
        _drain_stream_producers()


def _stream_frames_from(sink: _Sink) -> list[dict]:
    """Only the STREAM frames on a sink: the ones carrying a top-level ``type``.

    Serve's own frames are keyed on ``event``, so this is the same split the
    contract doc tells a consumer to make (``mission-control-stream.md`` —
    "count only decoded protocol frames as stream liveness").
    """

    return [frame for frame in sink.frames() if frame.get("type")]


def _fake_stream(gate: threading.Event, *, blob: str = ""):
    """A stoppable stand-in producer, for the tests whose subject is the OPS."""

    def _factory():
        def _generate():
            index = 0
            yield {"type": "hydrate", "index": index, "blob": blob}
            while not gate.is_set():
                index += 1
                time.sleep(0.005)
                yield {"type": "delta", "index": index, "blob": blob}

        return _generate()

    return _factory


# ── C-1: the op lane advertises itself ──────────────────────────────────────


def test_stdio_learns_the_op_set_from_ready_and_can_re_ask_version():
    """The gate EG-4.2 reads, stated exactly.

    Asserted as an EXACT block, not a membership check, for the reason the
    method lane's manifest test gives: a client folds this to a decision, and a
    key silently appearing or vanishing changes the decision. ``ready`` and
    ``version`` must agree, because a durable service outlives the install it
    was started from and a client that reconnects reads only one of them.
    """

    expected = {
        "contract": 1,
        "transport": "stdio",
        "ops": [
            "cancel",
            "connections",
            "drain",
            "ping",
            "shutdown",
            "stacks",
            "subscribe",
            "unsubscribe",
            "version",
        ],
        "subscribe_lanes": ["stream"],
    }
    with _stdio_serve() as (pipe, sink):
        ready = sink.wait_for("ready")
        pipe.send({"op": "version"})
        version = sink.wait_for("version")

    assert ready["ops"] == expected
    assert version["ops"] == expected
    assert serve_module.ops_manifest(transport="stdio") == expected
    # The lane the launcher gates on, named rather than inferred from the op.
    assert "subscribe" in ready["ops"]["ops"]
    assert ready["ops"]["subscribe_lanes"] == ["stream"]


def test_the_socket_greeting_advertises_the_ops_it_will_actually_answer():
    """Per transport, because the answer differs — and the difference is proven.

    ``shutdown`` is the stdio owner's verb: a socket client asking for it is
    refused ``op_not_available_on_socket``. Advertising it there would be the
    false all-clear this whole workstream exists to retire, so the socket's set
    omits it — and the refusal is exercised in the same test, because an
    omission nobody can justify is indistinguishable from an oversight.
    """

    with running_serve() as handle:
        with client(handle, name="ops-peer") as (connection, hello_ok):
            assert hello_ok["ops"] == {
                "contract": 1,
                "transport": "socket",
                "ops": [
                    "cancel",
                    "connections",
                    "drain",
                    "ping",
                    "stacks",
                    "subscribe",
                    "unsubscribe",
                    "version",
                ],
                "subscribe_lanes": ["stream"],
            }
            # The omission is a fact about this transport, not a preference.
            assert "shutdown" not in hello_ok["ops"]["ops"]
            connection.send({"op": "shutdown"})
            refused = _read_until(connection, "error")
            assert refused["error"] == "op_not_available_on_socket"

            # Re-askable, and answered for the transport that asked.
            connection.send({"op": "version"})
            version = _read_until(connection, "version")
            assert version["ops"]["transport"] == "socket"
            assert "shutdown" not in version["ops"]["ops"]


def test_every_advertised_op_is_answered_by_the_dispatcher():
    """An advertisement is a promise; this collects on all of it.

    An op the manifest names and the dispatcher does not handle falls through to
    the ARGV lane and comes back ``invalid_request`` — indistinguishable, from
    the client's side, from a malformed request of its own. So each advertised op
    is sent and matched against the typed frame it owns, and the absence of any
    ``invalid_request`` is asserted separately: without the per-op mapping the
    test would pass on a dispatcher that answered everything with ``busy``.
    """

    answers = {
        "ping": "busy",
        "version": "version",
        "connections": "socket_connections",
        "stacks": "stacks_dumped",
        "subscribe": "subscribed",
        "unsubscribe": "unsubscribed",
        "cancel": "cancel_denied",
    }
    gate = threading.Event()
    try:
        with _stdio_serve(stream_source_factory=_fake_stream(gate)) as (pipe, sink):
            for op in ("ping", "version", "connections", "stacks", "subscribe"):
                pipe.send({"op": op})
                sink.wait_for(answers[op])
            pipe.send({"op": "unsubscribe"})
            sink.wait_for(answers["unsubscribe"])
            pipe.send({"op": "cancel", "id": "no-such-request"})
            sink.wait_for(answers["cancel"])
            pipe.send({"op": "shutdown"})
            sink.wait_for("shutdown")
        errors = [
            frame
            for frame in sink.frames()
            if frame.get("error") == "invalid_request"
        ]
        assert errors == []
    finally:
        gate.set()

    # ``drain`` ends the process, so it gets its own runtime rather than a
    # position in the sequence above.
    with _stdio_serve() as (pipe, sink):
        pipe.send({"op": "drain", "deadline_seconds": 5})
        assert sink.wait_for("draining")["deadline_seconds"] == 5
        sink.wait_for("drain_complete")

    advertised = set(serve_module.ops_manifest(transport="stdio")["ops"])
    assert advertised == set(answers) | {"shutdown", "drain"}


def test_no_op_the_dispatcher_answers_is_left_off_the_advertisement():
    """The half a behaviour test cannot reach.

    An op that exists and is NOT advertised is invisible: no client sends it, so
    no test that drives the wire can find it, and the advertisement quietly
    becomes a subset of the truth — which is how a client ends up probing again
    for the one op that was forgotten. So the dispatcher's own source is the
    witness: every ``op == "…"`` branch in ``serve.py`` must be advertised on at
    least one transport.

    ``hello`` is the one deliberate exclusion. It is the socket's FIRST line,
    consumed by ``serve_socket`` before this dispatcher exists; a hello that
    REACHES the dispatcher is a second one and is answered ``unexpected_hello``.
    Its contract is advertised as ``hello_contract`` on ``server_hello``.
    """

    source = SERVE_SOURCE.read_text(encoding="utf-8")
    dispatched = set(re.findall(r'\bop == "([a-z_]+)"', source))
    # Anti-vacuity: the scrape found the real branch table, not zero of it.
    assert {"ping", "subscribe", "version", "shutdown"} <= dispatched

    advertised = set(serve_module.ops_manifest(transport="stdio")["ops"]) | set(
        serve_module.ops_manifest(transport="socket")["ops"]
    )
    assert dispatched - advertised == {"hello"}


def test_the_advertised_lanes_are_the_lanes_subscribe_accepts():
    """``subscribe_lanes`` and the dispatcher's lane check, held together.

    Two lists that must agree and are written in two places: the advertisement
    would keep saying ``stream`` for a dispatcher that had stopped accepting it,
    and would keep omitting a lane the dispatcher had quietly grown. Both
    directions are checked against the live refusal, which is the only witness
    that is not a copy of one of the two lists.
    """

    gate = threading.Event()
    try:
        with _stdio_serve(stream_source_factory=_fake_stream(gate)) as (pipe, sink):
            advertised = sink.wait_for("ready")["ops"]["subscribe_lanes"]
            assert advertised == ["stream"]

            for lane in advertised:
                pipe.send({"op": "subscribe", "lane": lane})
                assert sink.wait_for("subscribed")["lane"] == lane
                pipe.send({"op": "unsubscribe"})
                sink.wait_for("unsubscribed")

            pipe.send({"op": "subscribe", "lane": "telemetry"})
            denied = sink.wait_for("subscribe_denied")
            assert denied == {
                "event": "subscribe_denied",
                "lane": "telemetry",
                "reason": "unsupported_lane",
            }
    finally:
        gate.set()


def test_the_advertisement_grew_and_no_contract_integer_moved():
    """The eighth RPC method's discipline, applied to the op lane.

    A set plus an integer: the set grows because a client only ever sends what
    it FOUND, and the integers move only when an existing shape changes
    incompatibly. Nothing about this stage changes a frame, so every version on
    the wire is pinned here — including the two the collapse plan forbids
    touching outright.
    """

    from agent_runtime import serve_rpc

    assert serve_module.OPS_CONTRACT_VERSION == 1
    assert serve_module.SERVE_SCHEMA_VERSION == 1
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    assert STREAM_SCHEMA_VERSION == 1
    assert STREAM_PATCH_SCHEMA_VERSION == 2
    assert SNAPSHOT_CONTRACT_VERSION == 54
    # The method lane's own advertisement is untouched by this stage: the ops
    # ride BESIDE it, they do not join it. A future edit that merged the two
    # would break every client that folds ``rpc.methods`` as the method set.
    assert set(serve_rpc.manifest()) == {"contract", "methods"}


# ── byte parity against argv ``harness stream`` ──────────────────────────────


def test_the_hub_lane_delivers_the_argv_streams_frames_byte_for_byte():
    """The contract test: ONE producer's frames, reachable two ways.

    Both lanes run CONCURRENTLY over the same event log, which is what makes the
    comparison meaningful: the hydrate's watermark is the log's tail at the
    moment it is built, so a sequential comparison would compare two different
    tails and could only ever be normalized into agreement. Started together and
    fed one scripted append, the two must agree on the hydrate AND on the batch
    the append produces — the full-core ``delta_batch`` lane, since
    ``state.reconciled`` is not patch-coverable and demotes by contract.

    Volatile stamps are removed by the GOLDENS' normalizer, imported from the
    generator that writes ``tests/fixtures/stream_frames/``. A local normalizer
    would be a second authority, and the failure mode of a second normalizer is
    that it normalizes away a real divergence.
    """

    _seed_events(2)
    argv_frames: list[dict] = []
    argv_error: list[BaseException] = []

    def _argv_lane() -> None:
        try:
            for frame in stream_frames(
                poll_interval_seconds=0.25,
                heartbeat_interval_seconds=5.0,
                delta_debounce_seconds=0.2,
                max_frames=2,
                resync=False,
                fold_entities=parse_fold_entities_option(DECLARED_FLAG),
                caller="cli",
            ):
                argv_frames.append(to_jsonable(frame))
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            argv_error.append(exc)

    argv_thread = threading.Thread(target=_argv_lane, name="argv-stream", daemon=True)
    with _stdio_serve() as (pipe, sink):
        argv_thread.start()
        pipe.send(
            {"op": "subscribe", "lane": "stream", "fold_entities": DECLARED_LIST}
        )
        ack = sink.wait_for("subscribed")
        # The two lanes really did negotiate the same set; a parity result
        # between two DIFFERENT declarations would be meaningless.
        assert ack["fold_entities"] == sorted(DECLARED_LIST)

        _until(
            lambda: _stream_frames_from(sink) and argv_frames,
            what="both lanes to deliver their hydrate",
        )
        # The scripted event, appended once, AFTER both baselines exist — so
        # both lanes tail it from the same offset.
        _seed_events(1, start=2)
        _until(
            lambda: len(_stream_frames_from(sink)) >= 2 and len(argv_frames) >= 2,
            what="both lanes to deliver the scripted batch",
        )
        hub_frames = _stream_frames_from(sink)[:2]
        pipe.send({"op": "unsubscribe"})
        sink.wait_for("unsubscribed")

    argv_thread.join(WAIT)
    assert not argv_error, f"argv lane raised: {argv_error!r}"

    assert [frame["type"] for frame in hub_frames] == ["hydrate", "delta"]
    assert [frame["type"] for frame in argv_frames[:2]] == ["hydrate", "delta"]
    # The batch is the coalescing lane's, not a per-event delta — which is what
    # makes the second frame the FAT one the collapse plan's C-2 worries about.
    assert hub_frames[1]["coalesced_count"] == argv_frames[1]["coalesced_count"] == 1

    for index, (hub, argv) in enumerate(zip(hub_frames, argv_frames)):
        hub_bytes, argv_bytes = _canonical(hub), _canonical(argv)
        assert hub_bytes == argv_bytes, (
            f"frame {index} ({hub.get('type')}) diverged between the lanes; "
            f"hub={len(hub_bytes)}B argv={len(argv_bytes)}B"
        )
    # Anti-vacuity: an empty or trivial corpus would satisfy the loop above.
    assert len(_canonical(hub_frames[0])) > 1000


def test_the_lanes_frames_have_the_committed_goldens_shape():
    """The third leg, DERIVED from the goldens rather than from a new fixture.

    Byte parity between two lanes says they agree; it does not say they agree on
    the contract the launcher was built against. The committed
    ``hydrate.json`` — the cross-repo golden the launcher folds through its real
    read model — is the witness for that, compared by nested KEY PATH with the
    contract-fixture gate's own comparator. Read-only: no golden is written, no
    manifest moves, and this stage mints nothing.

    The comparison is a SUPERSET check in one direction and an equality in the
    other, and the asymmetry is the contract rather than a loosened assertion:
    nothing the golden carries may be missing (that would be a field the shipped
    launcher reads and this lane does not send), while the two v2 keys the patch
    lane added to the hydrate — ``delta_patches`` and the echoed
    ``fold_entities`` — are legitimately absent from a golden generated by
    ``hydrate_frame()`` with the lane off. They are NAMED here, so a THIRD extra
    key reds instead of riding in behind them.
    """

    _seed_events(2)
    with _stdio_serve() as (pipe, sink):
        pipe.send(
            {"op": "subscribe", "lane": "stream", "fold_entities": DECLARED_LIST}
        )
        sink.wait_for("subscribed")
        hydrate = _until(
            lambda: next(
                (
                    frame
                    for frame in _stream_frames_from(sink)
                    if frame.get("type") == "hydrate"
                ),
                None,
            ),
            what="the hub lane's hydrate",
        )
        pipe.send({"op": "unsubscribe"})
        sink.wait_for("unsubscribed")

    golden = json.loads((FIXTURES / "hydrate.json").read_text(encoding="utf-8"))
    producer_only, golden_only = _shape_drift(hydrate, golden)
    assert golden_only == []
    assert producer_only == ["delta_patches", "fold_entities"]


def test_the_two_lanes_encode_the_same_frame_with_different_whitespace():
    """TC-1's spec says "byte parity"; this records where that stops being true.

    ``harness stream`` writes ``separators=(",", ":")``;
    ``_FrameWriter.emit`` — the serve child's stdout, and therefore the hub
    lane's — writes json's DEFAULT separators. The same frame is the same bytes
    only after canonical re-encoding, which is what :func:`_canonical` does and
    what this test stops it from hiding: the parity above is a claim about the
    FRAME, and a consumer that compared raw lines across the two lanes would be
    comparing whitespace.

    Harmless by the contract's own terms — ``mission-control-stream.md`` tells a
    consumer to parse each line independently and branch on ``type`` — and
    recorded rather than fixed, because changing either encoder would move bytes
    on a lane in the field for no consumer's benefit.
    """

    frame = {"type": "heartbeat", "schema_version": 1, "watermark": {"event_offset": 7}}
    cli_line = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    serve_line = json.dumps(frame, ensure_ascii=False, default=str)

    assert cli_line != serve_line
    assert json.loads(cli_line) == json.loads(serve_line) == frame
    assert '"type":"heartbeat"' in cli_line
    assert '"type": "heartbeat"' in serve_line


def test_the_parity_comparator_can_actually_fail():
    """Anti-vacuity for :func:`_canonical`, driven on planted data.

    The normalizer it borrows is deliberately blunt — it flattens timestamps,
    build timings and section maps — so "the comparator agreed" is only evidence
    if the comparator still discriminates AFTER that flattening. A value the
    normalizer does not own must survive, and a value it does own must not.
    """

    base = {
        "type": "hydrate",
        "generated_at": "2026-08-17T00:00:00.000000Z",
        "watermark": {"event_offset": 11, "captured_at": "2026-08-17T00:00:00.000000Z"},
        "core": {"parity": {"build_ms": 5, "sections_ms": {"events": 3}}},
    }
    volatile = json.loads(json.dumps(base))
    volatile["generated_at"] = "2999-01-01T00:00:00.000000Z"
    volatile["watermark"]["captured_at"] = "2999-01-01T00:00:00.000000Z"
    volatile["core"]["parity"]["build_ms"] = 9999
    volatile["core"]["parity"]["sections_ms"]["events"] = 9999
    assert _canonical(volatile) == _canonical(base)

    real = json.loads(json.dumps(base))
    real["watermark"]["event_offset"] = 12
    assert _canonical(real) != _canonical(base)


# ── C-2: interleaving and backpressure on one connection ────────────────────


def test_a_fat_stream_frame_delays_the_method_lane_and_never_corrupts_it():
    """ASSUMPTION C-2, answered: one ordered writer, delay without corruption.

    The hub's sink and the method lane's sink on a stdio connection are the SAME
    ``_FrameWriter``, whose single lock is what makes a frame atomic — so a
    822 KB hydrate cannot be spliced with a small reply behind it. Asserted at
    the wire rather than by reading the code: every line the serve wrote parses
    as a complete JSON frame while the real producer is pushing full cores, the
    method lane's replies all arrive, and they arrive IN THE ORDER THEY WERE
    ASKED — the property head-of-line blocking would degrade and interleaving
    would destroy.

    What this deliberately does NOT assert is a LATENCY. TC-1's mandate is
    frames, bytes and ordering; an elapsed-ms bound here would be a claim about
    the machine running the suite.
    """

    _seed_events(2)
    request_ids = [f"m{index}" for index in range(6)]
    with _stdio_serve() as (pipe, sink):
        pipe.send(
            {"op": "subscribe", "lane": "stream", "fold_entities": DECLARED_LIST}
        )
        sink.wait_for("subscribed")
        _until(
            lambda: _stream_frames_from(sink),
            what="the fat hydrate to be on the wire",
        )
        # Fired while the producer is building/pushing full cores: each scripted
        # append demotes to a fat ``delta_batch``, so the small replies really do
        # queue behind big frames on one writer.
        for index, rid in enumerate(request_ids):
            pipe.send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "runtime.office.get",
                    "params": {"workspace_id": f"ws_no_such_{index}"},
                }
            )
            _seed_events(1, start=10 + index)

        def _replies() -> list[dict]:
            return [
                frame
                for frame in sink.frames()
                if "jsonrpc" in frame and frame.get("id") in request_ids
            ]

        _until(
            lambda: len(_replies()) == len(request_ids),
            what="every method-lane reply to land behind the fat frames",
        )
        # The fat lane has to have actually carried its batches, or the ordering
        # assertions below would be describing an idle producer.
        _until(
            lambda: len(_stream_frames_from(sink)) >= 3,
            what="the fat lane to deliver its scripted batches",
        )
        replies = _replies()
        stream_seen = _stream_frames_from(sink)
        pipe.send({"op": "unsubscribe"})
        sink.wait_for("unsubscribed")

    # No frame was lost, and none arrived out of order relative to its ask.
    assert [reply["id"] for reply in replies] == request_ids
    # Every line on the pipe is a WHOLE frame. ``_Sink.frames()`` drops a line it
    # cannot parse, so the count is compared against the raw line count — a
    # spliced frame would show up as two unparseable halves.
    lines = [line for line in sink.text().splitlines() if line.strip()]
    assert len(sink.frames()) == len(lines)
    # The hub's own frames kept their total order (one producer, one generation).
    offsets = [
        frame["watermark"]["event_offset"]
        for frame in stream_seen
        if isinstance(frame.get("watermark"), dict)
    ]
    assert offsets == sorted(offsets)
    # Anti-vacuity: the fat lane really was carrying traffic during the window,
    # and the frames really were the big ones (a heartbeat would prove nothing).
    assert len(stream_seen) >= 3
    assert all("core" in frame for frame in stream_seen)


def test_a_dropped_stream_subscriber_does_not_take_the_method_lane_with_it():
    """The drop is scoped to the SUBSCRIPTION, with exact numbers.

    EG-4.2 puts the read model on this lane, so the blast radius of a stream
    drop is the question that decides whether the office lane and the write
    verbs can share the connection. They can: the subscription is closed, the
    client is told which bound tripped and by how much, and the same connection
    answers a method call immediately afterwards.

    The bounds are set so only ONE can trip — 100k frames is unreachable, 64 KiB
    of 8 KiB frames is eight — so "which bound" has a right answer that a
    hard-coded one would get wrong.
    """

    release = threading.Event()
    sink = _StallingSink('"type": "delta"', release)
    factory = _fake_stream(release, blob="x" * 8192)
    try:
        with _stdio_serve(
            sink=sink,
            stream_source_factory=factory,
            stream_buffer_limit=100_000,
            stream_byte_limit=64 * 1024,
        ) as (pipe, _sink):
            pipe.send({"op": "subscribe", "lane": "stream"})
            assert sink.entered.wait(WAIT)
            # Long enough for the producer to blow past 64 KiB of 8 KiB frames
            # and nowhere near 100k frames, so the bound that trips is not in
            # doubt. Bounded by the BYTES it must produce, not by a latency.
            time.sleep(0.5)
            release.set()

            dropped = sink.wait_for("subscription_dropped")
            assert dropped["reason"] == "backpressure"
            assert dropped["bound"] == "bytes"
            assert dropped["byte_limit"] == 64 * 1024
            assert dropped["buffer_limit"] == 100_000
            assert dropped["frames_discarded"] > 0
            assert dropped["bytes_discarded"] > 0

            # The connection is intact: the method lane answers, and the op lane
            # agrees the subscription is gone rather than merely quiet.
            pipe.send(
                {
                    "jsonrpc": "2.0",
                    "id": "after-drop",
                    "method": "runtime.office.get",
                    "params": {"workspace_id": "ws_no_such"},
                }
            )
            reply = _until(
                lambda: next(
                    (
                        frame
                        for frame in sink.frames()
                        if frame.get("id") == "after-drop" and "jsonrpc" in frame
                    ),
                    None,
                ),
                what="a method reply after the stream drop",
            )
            assert "jsonrpc" in reply
            pipe.send({"op": "unsubscribe"})
            assert sink.wait_for("unsubscribed")["was_subscribed"] is False
    finally:
        release.set()


# ── re-subscribe recovery ───────────────────────────────────────────────────


def test_a_re_subscribe_after_a_drop_re_baselines_with_a_hydrate_first():
    """The recovery the plan puts in place of ``harness stream --resync``.

    A dropped subscriber has to be able to come back, and coming back is only
    safe if the first frame is a complete baseline — a client handed a delta
    would fold onto state it no longer holds, which is the silent-wrong-answer
    class the hub's own docstring opens with. So: drop, re-subscribe, and the
    FIRST stream frame of the new subscription is a hydrate whose watermark did
    not go backwards.
    """

    _seed_events(2)
    with _stdio_serve() as (pipe, sink):
        pipe.send(
            {"op": "subscribe", "lane": "stream", "fold_entities": DECLARED_LIST}
        )
        sink.wait_for("subscribed")
        first = _until(
            lambda: next(
                (f for f in _stream_frames_from(sink) if f.get("type") == "hydrate"),
                None,
            ),
            what="the first baseline",
        )
        before = len(_stream_frames_from(sink))

        # A clean leave stands in for the drop's client-side effect: the
        # subscription is gone and the client must re-baseline. (The DROP's own
        # accounting is pinned above; what is under test here is the rejoin.)
        pipe.send({"op": "unsubscribe"})
        sink.wait_for("unsubscribed")
        _seed_events(1, start=20)

        pipe.send(
            {"op": "subscribe", "lane": "stream", "fold_entities": DECLARED_LIST}
        )
        sink.wait_for("subscribed")
        rejoined = _until(
            lambda: _stream_frames_from(sink)[before:] or None,
            what="the re-subscribed lane's first frame",
        )
        pipe.send({"op": "unsubscribe"})
        sink.wait_for("unsubscribed")

    assert rejoined[0]["type"] == "hydrate"
    # Continuity, not merely a hydrate: the new baseline is at or ahead of the
    # old one, so nothing between the two is replayed as fresh activity.
    assert (
        rejoined[0]["watermark"]["event_offset"]
        >= first["watermark"]["event_offset"]
    )


def test_an_office_lanes_restart_free_join_does_not_suppress_a_stream_hydrate():
    """O-H5 and the stream lane's join semantics, held apart.

    ``StreamHub.subscribe(restart_producer=False)`` exists for the office push
    lane, whose baseline rides its own RPC reply — it attaches to the RUNNING
    generation and pays no full core. That option is one edit away from the
    stream lane's join, and if it ever leaked there a subscriber would attach
    mid-generation and fold deltas onto nothing. The floor rule says it cannot;
    this drives the real hub through the real sequence and says so.
    """

    from agent_runtime.serve_stream_hub import StreamHub

    delivered_office: list[dict] = []
    delivered_stream: list[dict] = []
    gate = threading.Event()
    hub = StreamHub(_fake_stream(gate))
    try:
        # The office lane joins first and arms the producer.
        assert hub.subscribe("office:ws", sink=delivered_office.append)
        _until(lambda: delivered_office, what="the office join to be fed")
        # A second office-shaped join, restart-free, against a LIVE producer:
        # this is the O-H5 path, and it must not re-baseline the room.
        assert hub.subscribe(
            "office:ws2", sink=lambda frame: None, restart_producer=False
        )

        # Now the stream lane joins the same hub the way ``serve.py`` does it.
        assert hub.subscribe("stdio", sink=delivered_stream.append)
        _until(lambda: delivered_stream, what="the stream join to be fed")
        assert delivered_stream[0]["type"] == "hydrate"
        # And the restart the stream join forced re-baselined the room, which is
        # the cost O-H5 avoids for its own joins and never for this one.
        assert any(frame["type"] == "hydrate" for frame in delivered_office[1:])
    finally:
        gate.set()
        hub.stop(join_timeout=2.0)
