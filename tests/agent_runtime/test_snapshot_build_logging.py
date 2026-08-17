"""EG-2.1: ONE receipt per build; every other line admits it is a WAIT.

What this file exists to prevent is a specific, twice-paid diagnosis failure.
On the 2026-08-17 boot the log showed three ``snapshot_build`` lines inside the
authoritative window and two independent investigations spent their longest
detours deciding whether that meant three concurrent builds. It did not: it was
ONE build plus two callers logging how long they waited for it — and the boot's
most expensive build (the serve prewarm) had no line at all, because nothing on
its path had one to emit.

So three claims are pinned here, and each is pinned by a probe the other two
cannot satisfy:

1. **The build count is the count of ``role=led``** — one receipt per actual
   build, emitted by the caller that ran it, regardless of how many callers
   shared it (HY-0's led-count + invocation-counter pair).
2. **Every caller learns its own role** — ``led`` / ``rode`` / ``shared_next``,
   one ``build_info`` dict per caller, produced by three different code paths a
   test-owned gate forces them down (HC-0's role matrix). A hardcoded role
   matches at most one of them.
3. **The attachments say so** — the socket lane, the office RPC lane and the CLI
   each log ONE line when they attach to the shared stream. A 12 MB serve-child
   log with zero office lines is why "is the push lane attached?" was
   unanswerable from the log the operator actually has (plan §8 item 5).

Counts, roles and orderings only. No test here asserts an elapsed millisecond:
the seconds are read off these receipts in production, which is the whole point
of emitting them (plan #60).
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from argparse import Namespace

import pytest

from agent_runtime import snapshot as snapshot_mod
from agent_runtime import stream as stream_mod

_CORE_PREFIX = "snapshot_build_core "
_WAIT_PREFIX = "snapshot_build "
_ATTACH_PREFIX = "stream_attach "
_PAIR = re.compile(r"(?P<key>[a-z_]+)=(?P<value>\S+)")


def _pairs(message: str, prefix: str) -> dict[str, str]:
    return {
        match["key"]: match["value"]
        for match in _PAIR.finditer(message[len(prefix) :])
    }


def _lines(caplog, prefix: str) -> list[dict[str, str]]:
    """Parsed log lines carrying exactly ``prefix``.

    ``snapshot_build_core`` and ``snapshot_build`` share a stem on purpose (one
    grep finds both), so the match is anchored: a wait line counted as a receipt
    would re-create the very conflation this stage removes.
    """

    rows = []
    for record in caplog.records:
        message = record.getMessage()
        if prefix == _WAIT_PREFIX and message.startswith(_CORE_PREFIX):
            continue
        if message.startswith(prefix):
            rows.append(_pairs(message, prefix))
    return rows


def _led_records(caplog) -> list[str]:
    """Every captured line that claims the ``led`` role, whatever emitted it.

    Deliberately a text scan across BOTH loggers rather than a receipt count:
    HY-0's killing mutation is "emit ``role=led`` on every path", and a mutant
    that stamps ``led`` onto the riders' wait lines has to be convicted here.
    """

    return [
        record.getMessage()
        for record in caplog.records
        if "role=led" in record.getMessage()
    ]


@pytest.fixture(autouse=True)
def _reset_coalescer():
    """The coalescer is process-global; a leaked ``running`` wedges the next
    test's leader into a wait that nothing will ever release."""

    with snapshot_mod._BUILD_COALESCE:
        # ``started``/``done`` go back to zero as well, unlike the sibling
        # coalesce file's fixture: the generation on the receipt is a
        # process-lifetime counter (which is what makes it useful in a boot log),
        # so a test asserting ``generation=1`` would otherwise read whatever the
        # tests before it happened to build.
        snapshot_mod._build_coalesce_state.update(
            running=False, result=None, waiters=0, started=0, done=0
        )
    yield
    with snapshot_mod._BUILD_COALESCE:
        # ``started``/``done`` go back to zero as well, unlike the sibling
        # coalesce file's fixture: the generation on the receipt is a
        # process-lifetime counter (which is what makes it useful in a boot log),
        # so a test asserting ``generation=1`` would otherwise read whatever the
        # tests before it happened to build.
        snapshot_mod._build_coalesce_state.update(
            running=False, result=None, waiters=0, started=0, done=0
        )


@pytest.fixture
def build_log(caplog):
    caplog.set_level(logging.INFO, logger="agent_runtime.snapshot")
    caplog.set_level(logging.INFO, logger="agent_runtime.stream")
    return caplog


def _wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _core(*, build_ms: int, offset: int = 4096, sections: dict | None = None) -> dict:
    """A core shaped where the receipt reads: the parity envelope, nothing else.

    The receipt is REQUIRED to read its numbers off the envelope the build
    already computed rather than measure a second time, so these fakes carry
    numbers no timer in a millisecond-long test could produce.
    """

    return {
        "schema_version": 2,
        "generated_at": "2026-08-17T00:00:00+00:00",
        "parity": {
            "build_ms": build_ms,
            "sections_ms": sections
            if sections is not None
            else {"agents_readiness": 3000, "events": 900, "boards_offices": 400, "persona_chat": 21},
            "watermark": {"event_offset": offset, "captured_at": "2026-08-17T00:00:00+00:00"},
            "completeness": {},
            "drops": [],
            "warnings": [],
        },
    }


# ── 1. one receipt per build (HY-0) ─────────────────────────────────────────


def test_one_led_receipt_per_build_while_two_callers_ride_it(monkeypatch, build_log):
    """Two callers, one build, ONE ``role=led`` line — HY-0's killing pair.

    *Mutation:* emit ``role=led`` on every path. *Probes:* the count of captured
    ``role=led`` records is 1 while the caller count is 2, AND the test's own
    builder counter is 1. A mutant that fakes the role cannot reduce the record
    count without restoring the branch it mutated, and a mutant that really
    double-builds must call the counter twice.
    """

    calls: list[int] = []
    calls_lock = threading.Lock()
    building = threading.Event()
    release = threading.Event()

    def fake_build(**_kwargs):
        with calls_lock:
            calls.append(1)
        building.set()
        assert release.wait(5)
        return _core(build_ms=4321)

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    leader_info: dict = {"caller": "prewarm"}
    rider_info: dict = {"caller": "hub"}
    leader = threading.Thread(
        target=lambda: snapshot_mod.build_snapshot(build_info=leader_info)
    )
    leader.start()
    assert building.wait(5)
    rider = threading.Thread(
        target=lambda: snapshot_mod.build_snapshot(
            accept_inflight=True, build_info=rider_info
        )
    )
    rider.start()
    assert _wait_for(lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1)
    release.set()
    leader.join(5)
    rider.join(5)

    assert len(calls) == 1
    assert leader_info["role"] == "led"
    assert rider_info["role"] == "rode"

    receipts = _lines(build_log, _CORE_PREFIX)
    assert len(receipts) == 1, [r.getMessage() for r in build_log.records]
    assert receipts[0]["role"] == "led"
    assert receipts[0]["caller"] == "prewarm"
    assert receipts[0]["generation"] == "1"
    assert len(_led_records(build_log)) == 1, _led_records(build_log)


def test_the_receipt_reads_its_cost_off_the_envelope_the_build_built(
    monkeypatch, build_log
):
    """``build_ms`` / ``offset`` / ``sections_top`` come off the parity envelope.

    *Mutation:* time the build here instead (a second authority on one span), or
    print a constant. *Probes:* two driven envelopes in one test, whose numbers
    a millisecond-long fake build cannot produce and a constant matches at most
    one of.
    """

    cores = [
        _core(build_ms=4321, offset=4096, sections={"agents_readiness": 3000, "events": 900, "boards_offices": 400, "persona_chat": 21}),
        _core(build_ms=777, offset=8192, sections={"prompt_observability": 500, "running_work": 200, "events": 60}),
    ]

    def fake_build(**_kwargs):
        return cores.pop(0)

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    snapshot_mod.build_snapshot(build_info={"caller": "cli"})
    snapshot_mod.build_snapshot(build_info={"caller": "hub"})

    receipts = _lines(build_log, _CORE_PREFIX)
    assert len(receipts) == 2
    assert receipts[0]["build_ms"] == "4321"
    assert receipts[0]["offset"] == "4096"
    # Top THREE, cost-descending — the split that answers "slow of what?".
    assert receipts[0]["sections_top"] == "agents_readiness:3000,events:900,boards_offices:400"
    assert receipts[1]["build_ms"] == "777"
    assert receipts[1]["offset"] == "8192"
    assert receipts[1]["sections_top"] == "prompt_observability:500,running_work:200,events:60"


def test_an_injected_store_build_is_attributed_but_prints_no_receipt(
    monkeypatch, build_log
):
    """A fixture is not a boot. Injected-store builds fill ``build_info`` (they
    genuinely led) and print nothing: one receipt per unit test would bury the
    boot's own lines in the log this stage exists to make readable."""

    monkeypatch.setattr(
        snapshot_mod,
        "_build_snapshot_uncoalesced",
        lambda **_kwargs: _core(build_ms=11),
    )

    info: dict = {"caller": "doctor"}
    snapshot_mod.build_snapshot(agent_store=object(), build_info=info)

    assert info["role"] == "led"
    assert info["generation"] is None
    assert _lines(build_log, _CORE_PREFIX) == []


# ── 2. the role matrix (HC-0) ───────────────────────────────────────────────


def test_the_role_matrix_names_the_leader_the_rider_and_the_sharer(
    monkeypatch, build_log
):
    """Four callers, three roles, two builds — HC-0's matrix on HEAD's semantics.

    HC-0 specified three callers (led / rode / shared_next). HEAD's coalescer
    makes the first PLAIN waiter LEAD the next build (see
    ``build_snapshot``'s target rule and ``test_concurrent_storm_costs_at_most_
    two_builds``), so ``shared_next`` is only reachable behind a second plain
    waiter — hence four callers, and hence the pair assertion: which of C/D wins
    the lead is the condition's wakeup order, which no test owns. Both roles are
    still asserted to occur exactly once, which is what the mutations turn on.

    *Mutation:* hardcode ``role="led"``. *Red:* B, and the C/D pair. *Mutation:*
    make everyone lead. *Red:* the invocation counter (4, not 2).
    """

    calls: list[int] = []
    calls_lock = threading.Lock()
    building = {1: threading.Event(), 2: threading.Event()}
    release = {1: threading.Event(), 2: threading.Event()}

    def fake_build(**_kwargs):
        with calls_lock:
            calls.append(1)
            generation = len(calls)
        building[generation].set()
        assert release[generation].wait(5)
        return _core(build_ms=1000 + generation)

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    infos = {name: {"caller": name} for name in ("a", "b", "c", "d")}

    def call(name: str, **kwargs):
        return threading.Thread(
            target=lambda: snapshot_mod.build_snapshot(
                build_info=infos[name], **kwargs
            ),
            name=f"role-matrix-{name}",
        )

    leader = call("a")
    leader.start()
    assert building[1].wait(5)

    rider = call("b", accept_inflight=True)
    rider.start()
    assert _wait_for(lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1)

    third, fourth = call("c"), call("d")
    third.start()
    fourth.start()
    assert _wait_for(lambda: snapshot_mod._build_coalesce_state["waiters"] >= 3)

    release[1].set()
    assert building[2].wait(5)
    release[2].set()
    for thread in (leader, rider, third, fourth):
        thread.join(5)

    # The coalesce witness is a CALL COUNT, never a duration: four callers, two
    # builds.
    assert len(calls) == 2

    assert infos["a"]["role"] == snapshot_mod.BUILD_ROLE_LED
    assert infos["a"]["generation"] == 1
    # B opted into the build already running, so it rode generation 1 — the
    # build that started BEFORE it arrived.
    assert infos["b"]["role"] == snapshot_mod.BUILD_ROLE_RODE
    assert infos["b"]["generation"] == 1
    pair = sorted((infos["c"]["role"], infos["d"]["role"]))
    assert pair == [snapshot_mod.BUILD_ROLE_LED, snapshot_mod.BUILD_ROLE_SHARED_NEXT]
    assert infos["c"]["generation"] == 2
    assert infos["d"]["generation"] == 2

    # Each caller owns its OWN dict — four distinct objects, so a single
    # shared/global out-param could not have produced the matrix above.
    assert len({id(info) for info in infos.values()}) == 4

    # Two builds, two receipts, both ``led`` — and the second names whichever
    # waiter actually ran it.
    receipts = _lines(build_log, _CORE_PREFIX)
    assert [row["role"] for row in receipts] == ["led", "led"]
    assert receipts[0]["caller"] == "a"
    assert receipts[1]["caller"] in {"c", "d"}
    assert len(_led_records(build_log)) == 2


def test_the_prewarm_names_itself_on_the_line_it_used_to_never_emit(
    monkeypatch, build_log
):
    """The serve prewarm is the most expensive build of a cold boot and had no
    line anywhere; every line in the window belonged to a caller that rode it.

    *Mutation:* drop the ``build_info`` seed from the prewarm. *Probe:*
    ``caller=prewarm`` on the receipt — an unseeded build prints
    ``caller=unknown``, which is exactly the boot this stage is closing.
    """

    from hermes_cli.harness_parts import serve as serve_mod

    monkeypatch.setattr(
        snapshot_mod,
        "_build_snapshot_uncoalesced",
        lambda **_kwargs: _core(build_ms=21400),
    )

    serve_mod._prewarm_read_model_snapshot()

    receipts = _lines(build_log, _CORE_PREFIX)
    assert len(receipts) == 1
    assert receipts[0]["role"] == "led"
    assert receipts[0]["caller"] == "prewarm"
    assert receipts[0]["build_ms"] == "21400"


# ── 3. the wait line stops impersonating the build ──────────────────────────


def test_a_riders_wait_line_names_the_role_and_the_build_it_did_not_run(
    monkeypatch, build_log
):
    """The hydrate rider's line: ``role=rode``, plus the ``build_ms`` of the
    build it rode — which it did not measure and could not have.

    *Mutation:* keep reporting the wait as the build (the pre-EG-2.1 line).
    *Probe:* ``build_ms`` equals the driven envelope value, and ``role`` is
    ``rode`` while ``waited_ms`` is the caller's own number.
    """

    building = threading.Event()
    release = threading.Event()

    def fake_build(**_kwargs):
        building.set()
        assert release.wait(5)
        return _core(build_ms=6543, offset=555)

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    leader = threading.Thread(
        target=lambda: snapshot_mod.build_snapshot(build_info={"caller": "prewarm"})
    )
    leader.start()
    assert building.wait(5)

    frames: dict = {}
    rider = threading.Thread(
        target=lambda: frames.__setitem__(
            "hydrate", stream_mod.hydrate_frame(caller="hub")
        )
    )
    rider.start()
    assert _wait_for(lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1)
    release.set()
    leader.join(5)
    rider.join(5)

    assert frames["hydrate"]["watermark"]["event_offset"] == 555
    waits = _lines(build_log, _WAIT_PREFIX)
    assert len(waits) == 1, [r.getMessage() for r in build_log.records]
    assert waits[0]["reason"] == "hydrate"
    assert waits[0]["role"] == "rode"
    assert waits[0]["caller"] == "hub"
    assert waits[0]["build_ms"] == "6543"
    assert waits[0]["offset"] == "555"
    # The one receipt for the one build belongs to the caller that ran it.
    receipts = _lines(build_log, _CORE_PREFIX)
    assert [row["caller"] for row in receipts] == ["prewarm"]


def test_the_wait_line_ships_both_the_new_key_and_the_deprecated_one(
    monkeypatch, build_log
):
    """``elapsed_ms`` is kept for one release beside ``waited_ms`` — SAME value.

    *Mutation:* drop ``elapsed_ms`` (breaks the fielded launcher's parser), or
    make it a second measurement. *Probe:* both keys present, byte-equal.
    """

    monkeypatch.setattr(
        snapshot_mod,
        "_build_snapshot_uncoalesced",
        lambda **_kwargs: _core(build_ms=120, offset=7),
    )

    stream_mod.hydrate_frame(caller="cli")

    waits = _lines(build_log, _WAIT_PREFIX)
    assert len(waits) == 1
    assert "waited_ms" in waits[0] and "elapsed_ms" in waits[0]
    assert waits[0]["waited_ms"] == waits[0]["elapsed_ms"]
    assert waits[0]["role"] == "led"
    assert waits[0]["caller"] == "cli"


@pytest.mark.parametrize(
    "build_ms, carries_sections",
    [
        (snapshot_mod.BUILD_SECTIONS_WAIT_THRESHOLD_MS - 1, False),
        (snapshot_mod.BUILD_SECTIONS_WAIT_THRESHOLD_MS + 1, True),
    ],
)
def test_sections_ride_a_wait_line_only_when_the_build_was_slow(
    monkeypatch, build_log, build_ms, carries_sections
):
    """A wait line carries the split when the build under it was slow enough for
    "of what?" to be the next question; the build's own receipt always carries
    it. *Mutation:* always print, or never. *Probe:* two driven values across
    the threshold — a constant matches at most one."""

    monkeypatch.setattr(
        snapshot_mod,
        "_build_snapshot_uncoalesced",
        lambda **_kwargs: _core(build_ms=build_ms, sections={"agents_readiness": build_ms}),
    )

    stream_mod.hydrate_frame(caller="cli")

    waits = _lines(build_log, _WAIT_PREFIX)
    assert len(waits) == 1
    assert ("sections_top" in waits[0]) is carries_sections
    # The receipt is unconditional either way — one line per build, fully
    # attributed.
    assert _lines(build_log, _CORE_PREFIX)[0]["sections_top"] == f"agents_readiness:{build_ms}"


# ── 4. the attachments say so ───────────────────────────────────────────────


def test_the_socket_subscribe_logs_its_attachment(build_log):
    """``{"op":"subscribe"}`` is one of three ways to attach to the shared
    producer, and none of them said so in the child's own log — which is how
    the boot's third stream rider stayed unidentified."""

    from hermes_cli.harness_parts.serve import serve_loop

    def _factory():
        return iter([{"type": "hydrate", "index": 0}])

    out = io.StringIO()
    requests = [
        json.dumps({"op": "subscribe", "lane": "stream"}) + "\n",
        json.dumps({"op": "shutdown"}) + "\n",
    ]
    assert (
        serve_loop(
            iter(requests),
            out,
            dispatch=lambda argv: 0,
            stream_source_factory=_factory,
        )
        == 0
    )

    attaches = _lines(build_log, _ATTACH_PREFIX)
    assert len(attaches) == 1, [r.getMessage() for r in build_log.records]
    assert attaches[0]["op"] == "subscribe"
    assert attaches[0]["purpose"] == "stream_lane"
    assert attaches[0]["connection"] == "stdio"


def test_the_hub_producer_names_itself_the_caller(monkeypatch, build_log):
    """Every build the shared producer pays for is billed to ``hub``, not to
    whichever subscriber triggered the restart: one producer, N subscribers.

    *Mutation:* drop the ``caller="hub"`` argument. *Probe:* the recorded kwarg
    — the default is ``cli``, i.e. a terminal, which is a false census entry.
    """

    from hermes_cli.harness_parts.serve import serve_loop

    seen: list[dict] = []

    def fake_stream_frames(**kwargs):
        seen.append(kwargs)
        return iter([{"type": "hydrate"}])

    monkeypatch.setattr(stream_mod, "stream_frames", fake_stream_frames)

    out = io.StringIO()
    requests = [
        json.dumps({"op": "subscribe", "lane": "stream"}) + "\n",
        json.dumps({"op": "shutdown"}) + "\n",
    ]
    assert serve_loop(iter(requests), out, dispatch=lambda argv: 0) == 0

    assert _wait_for(lambda: bool(seen))
    assert seen[0]["caller"] == "hub"


def test_the_office_subscribe_logs_its_attachment(build_log):
    """The office lane's attach line — plan §8 item 5's missing line: 12 MB of
    serve-child log with zero ``office`` entries made "is the push lane
    attached?" unanswerable from the log the operator has."""

    from agent_runtime.serve_office_subscriptions import (
        OFFICE_SUBSCRIBE_METHOD,
        OFFICE_SUBSCRIPTIONS,
    )

    class _Hub:
        def __init__(self) -> None:
            self.generation = 0
            self.producer_running = False

        def subscribe(self, key, *, sink, on_drop=None, restart_producer=True):
            self.generation += 1
            self.producer_running = True
            return True

        def unsubscribe(self, key):
            return False

        def stats(self):
            return {
                "generation": self.generation,
                "producer_running": self.producer_running,
            }

    hub = _Hub()
    OFFICE_SUBSCRIPTIONS.bind(lambda: hub)
    try:
        outcome = OFFICE_SUBSCRIPTIONS.subscribe(
            connection_key="conn-7",
            workspace_id="ws_attach_log",
            baseline_offset=4096,
            emit=lambda frame: None,
            reason="push:full_core",
        )
    finally:
        OFFICE_SUBSCRIPTIONS.bind(None)

    assert bool(outcome) is True
    attaches = _lines(build_log, _ATTACH_PREFIX)
    assert len(attaches) == 1, [r.getMessage() for r in build_log.records]
    assert attaches[0]["op"] == OFFICE_SUBSCRIBE_METHOD
    assert attaches[0]["purpose"] == "office_patch"
    assert attaches[0]["workspace"] == "ws_attach_log"
    assert attaches[0]["connection"] == "conn-7"
    # The client's own words for WHY, carried verbatim — the fact no line can
    # derive, and the one that separates a fold-fence ladder from a full_core one.
    assert attaches[0]["reason"] == "push:full_core"
    assert attaches[0]["producer_restarted"] == "True"


def test_the_office_attach_line_names_the_method_clients_actually_call():
    """The op on the line is the registered method name. A hand-typed string in
    a log line is how a rename ships a log naming a method nobody can call."""

    from agent_runtime import serve_rpc
    from agent_runtime.serve_office_subscriptions import OFFICE_SUBSCRIBE_METHOD

    assert OFFICE_SUBSCRIBE_METHOD in set(serve_rpc.manifest()["methods"])


def test_the_cli_stream_command_logs_its_attachment(monkeypatch, build_log):
    """A terminal tailing the stream is a full subscriber of the same producer
    and paid for builds nobody could attribute to it."""

    from hermes_cli.harness_parts import runtime_commands

    monkeypatch.setattr(stream_mod, "stream_frames", lambda **kwargs: iter(()))

    assert (
        runtime_commands._cmd_stream(
            Namespace(
                poll_interval=0.01,
                heartbeat_interval=60,
                delta_debounce_ms=0,
                max_frames=1,
                resync=False,
                fold_entities=None,
            )
        )
        == 0
    )

    attaches = _lines(build_log, _ATTACH_PREFIX)
    assert len(attaches) == 1
    assert attaches[0]["op"] == "harness_stream"
    assert attaches[0]["purpose"] == "cli_stream"
