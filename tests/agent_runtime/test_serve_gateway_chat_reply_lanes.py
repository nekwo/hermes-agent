"""WHICH LANE carries a remote chat turn's start and its reply. A measurement.

Plan ``EterniaLauncher/docs/mission_control/planned/remote-chat-parity.md``
stage C1h §2, and ruling **R-C3** ("the reply is proved in the same stage as the
send, by a receipt") is written against the answer this file records. C1l has to
put its ``chat_turn_reply … first_frame=+N ms`` receipt where the console first
folds a frame for the turn, and it cannot choose that place from the plan's
prose: the plan named two candidates and asked which one is real.

The two candidates, from ``_runtime_chat_message``'s own docstring:

(a) **the per-request frame lane on the CALLING connection**, under the
    ``request_id`` the ack returns. That is the lane the local launcher already
    reads for an argv request, and it is where the docstring says the turn's
    frames ride.
(b) **the ``stream`` lane**, which any subscriber holds — a ``running_work``
    ``chat_turn`` row, the chat projection, or the trace deltas
    (``chat.trace.appended``). This is the lane
    ``agent_chat/mission_external_turn_presence.dart`` folds an unaccounted-for
    turn out of, and the one a SECOND console (the Mac's own launcher, watching
    the same runtime) would have to see the turn on.

What is REAL here, and what is not
----------------------------------
Real: a ``serve_loop`` with both listeners up, a TLS-pinned socket, a device
paired at ``console`` through the real ceremony, a ``stream`` subscription on
that same connection, the real ``dispatch_argv``, the real
``runtime.chat.message`` accept, and the real ``mission-chat message`` handler
running on the serve's worker pool with the real turn journal, chat-root lease,
transcript persistence and event log.

Not real: the model. The agent factory is replaced by
:class:`_ScriptedAgent`, which answers a fixed string without a network call —
the seam ``ProfileAgentRunner`` already has, patched at the module-level default
so the production construction path (``_uses_default_agent_factory``) is the one
under test. What is being measured is which LANE carries the frames, not what an
agent says, and a real provider here would measure the provider.

The assertions are what was OBSERVED. The recorded answer is in
``<repo>/docs/agent-runtime-harness/planned/remote-chat-parity-field-notes-2026-09-05.md``.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_runtime.call_authorization import TIER_CONSOLE
from hermes_cli.harness_parts.serve import dispatch_argv
from tests.agent_runtime.test_serve_gateway_lane import (
    WAIT,
    device_client,
    gateway_on,  # noqa: F401 - fixture
    pair_device,
    running_serve,
)

WORKSPACE = "ws_chat_reply_lanes"
PERSONA = "qa"

#: What the scripted agent answers. Distinctive so a grep over a captured frame
#: cannot confuse it with anything the harness itself writes.
SCRIPTED_REPLY = "scripted-reply-from-the-other-machine"


class _ScriptedAgent:
    """An agent that answers without a provider.

    The shape is ``run_agent.AIAgent``'s as ``ProfileAgentRunner`` uses it: built
    with keyword arguments, asked for one ``run_conversation``, read back through
    ``final_response`` / ``messages``. Modelled on the fake in
    ``test_send_path_runner_reuse``, which pins the same seam.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "session_scripted"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = "https://example.invalid/v1"
        self.tools = []
        self.messages = []
        self._persist_user_message_idx = None
        self.status_callback = kwargs.get("status_callback")
        self.max_iterations = kwargs.get("max_iterations")
        self.cache_scope_id = kwargs.get("cache_scope_id")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tool_start_callback = kwargs.get("tool_start_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.clarify_callback = kwargs.get("clarify_callback")

    #: Seconds the "provider call" takes. Zero for the fast measurement; the
    #: slow one sets it so the turn is still RUNNING across several of the
    #: stream hub's poll cycles — which is the only arrangement in which
    #: "does the stream lane show a turn in flight" is answerable at all.
    dwell_seconds = 0.0

    def run_conversation(self, user_message, system_message=None, task_id=None, **kwargs):
        if self.dwell_seconds:
            time.sleep(self.dwell_seconds)
        self.messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": SCRIPTED_REPLY},
        ]
        self._persist_user_message_idx = 0
        return {
            "final_response": SCRIPTED_REPLY,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "messages": self.messages,
            "api_calls": 1,
            "total_tokens": 7,
        }


@pytest.fixture
def scripted_model(monkeypatch):
    """No network, and patched where PRODUCTION resolves it.

    ``ProfileAgentRunner`` binds ``_default_agent_factory`` at construction, so
    the patch has to be on the module attribute rather than on a runner
    instance — a runner built later on the serve's worker thread must pick it
    up, and one built with an explicit factory would be exercising the seam
    instead of the send path.
    """

    from agent_runtime import profile_runner

    monkeypatch.setattr(profile_runner, "_default_agent_factory", _ScriptedAgent)
    monkeypatch.setattr(
        profile_runner,
        "resolve_runtime_provider",
        lambda requested, target_model: {
            "provider": requested or "scripted",
            "model": target_model or "scripted-model",
            "api_mode": "codex_responses",
        },
    )


@pytest.fixture
def head_home(monkeypatch):
    """Name this process's chat HEAD home, the way the launcher's spawn does.

    Not a convenience. ``persona_chat_durability.default_persona_session_db``
    fails CLOSED when a HERMES_HOME override is live and no authority resolved a
    head — and inside a serve every request runs under
    ``profile_context.process_home_scope(serve_request_home)``, which IS such an
    override. In the field the launcher always starts serve with
    ``HERMES_HEAD_HOME`` and ``serve_loop``'s boot publishes it; a harness that
    omitted it would measure ``chat_session_db_unavailable`` and learn nothing
    about which lane carries a reply. Setting it here reproduces the field's
    arrangement rather than working around the guard.
    """

    import os

    monkeypatch.setenv("HERMES_HEAD_HOME", os.environ["HERMES_HOME"])


@pytest.fixture
def placed_agent(isolate_agent_runtime_root, head_home):
    """One roster persona and one real placement, minted by the verb that mints."""

    from agent_runtime import serve_rpc
    from agent_runtime.models import AgentPersona
    from agent_runtime.office_store import OfficeStore
    from agent_runtime.store import AgentStore
    from tests.agent_runtime.office_seed import seed_workspace_record

    AgentStore().save(
        AgentPersona(
            id=PERSONA,
            display_name="QA Agent",
            role="qa",
            model=None,
            provider=None,
            api_mode=None,
            toolsets=[],
            system_prompt_path="",
        )
    )
    seed_workspace_record(WORKSPACE)
    OfficeStore().ensure_surface(WORKSPACE, created_by="seed")
    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "seed",
            "method": "runtime.agent.create",
            "params": {
                "persona_id": PERSONA,
                "workspace_id": WORKSPACE,
                "position": [1.0, 2.0],
                "idempotency_key": "reply-lane-seed",
            },
        }
    )
    assert "result" in reply, reply
    return reply["result"]


class _Recorder:
    """Every frame off ONE device connection, stamped when it arrived.

    A reader THREAD rather than a read loop in the test body, because the whole
    question is which of two interleaved lanes delivers first: a body that read
    for one lane and then the other would measure its own polling order.
    """

    def __init__(self, connection):
        self._connection = connection
        self._frames: list[tuple[float, dict]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._pump, name="reply-lane-recorder", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._connection.read_frame()
            except Exception:
                return
            if frame is None:
                return
            with self._lock:
                self._frames.append((time.monotonic(), frame))

    def stop(self) -> None:
        self._stop.set()

    def frames(self) -> list[tuple[float, dict]]:
        with self._lock:
            return list(self._frames)

    def matching(self, predicate) -> list[tuple[float, dict]]:
        return [(at, frame) for at, frame in self.frames() if predicate(frame)]

    def wait_for(self, predicate, *, what: str, timeout: float = WAIT):
        return self._wait(self.matching, predicate, what=what, timeout=timeout)

    def _wait(self, select, predicate, *, what: str, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hits = select(predicate)
            if hits:
                return hits[0]
            time.sleep(0.01)
        raise AssertionError(f"no frame for {what} within {timeout}s")

    def since_start_ms(self, at: float) -> float:
        return (at - self.started_at) * 1000.0


def _is_stream(frame: dict) -> bool:
    """A STREAM frame: the ones carrying a top-level ``type``.

    Serve's own frames are keyed on ``event``, so this is the same split the
    stream contract tells a consumer to make and the same one
    ``test_serve_stream_lane_parity._stream_frames_from`` uses.
    """

    return bool(frame.get("type")) and "event" not in frame


def _request_frame(request_id: str):
    def _predicate(frame: dict) -> bool:
        return frame.get("id") == request_id and "event" in frame

    return _predicate


def _carries_running_chat_turn(turn_request_id: str):
    """A stream frame whose projection holds a RUNNING row for this turn."""

    work_id = f"chat_turn:{turn_request_id}"

    def _predicate(frame: dict) -> bool:
        if not _is_stream(frame):
            return False
        core = frame.get("core")
        running = core.get("running_work") if isinstance(core, dict) else None
        rows = running.get("rows") if isinstance(running, dict) else None
        return any(
            isinstance(row, dict)
            and row.get("work_id") == work_id
            and row.get("status") == "running"
            for row in rows or []
        )

    return _predicate


def _stream_payload(frame: dict) -> dict:
    """A stream frame IS its payload on this lane — there is no envelope."""

    return frame


@pytest.mark.timeout(300)
def test_a_device_tier_turn_reports_which_lane_carries_its_start_and_reply(
    gateway_on, placed_agent, scripted_model, capfd
):
    """THE measurement. Everything asserted here was observed on this harness.

    Read the ``LANE REPORT`` this test prints (``-s``) beside the field notes:
    it lists every frame that arrived on the connection, in order, with the
    milliseconds since the subscription was established.
    """

    instance_id = placed_agent["persona_instance_id"]
    credential = pair_device(tier=TIER_CONSOLE, name="the mac")

    with running_serve(dispatch=dispatch_argv) as handle:
        with device_client(handle, credential) as (connection, hello):
            assert hello["event"] == "hello_ok"
            assert "subscribe" in hello["ops"]["ops"]

            recorder = _Recorder(connection)
            recorder.start()
            try:
                # (a) the stream subscription, on the SAME connection the turn
                #     will be sent on. One connection is the honest arrangement:
                #     it is what the launcher holds, and it is the only one in
                #     which "which lane arrived first" is a fair question.
                connection.send({"op": "subscribe", "lane": "stream"})
                subscribed_at, subscribed = recorder.wait_for(
                    lambda frame: frame.get("event") == "subscribed",
                    what="the stream subscription ack",
                )
                assert subscribed["lane"] == "stream"
                hydrate_at, hydrate = recorder.wait_for(
                    lambda frame: _is_stream(frame)
                    and _stream_payload(frame).get("type") == "hydrate",
                    what="the stream lane's hydrate",
                )

                # (b) the turn, over the method lane, as a remote console sends
                #     it. ``new_session`` so the runtime mints the root — the
                #     shape C5's operator proof uses.
                sent_at = time.monotonic()
                connection.send(
                    {
                        "jsonrpc": "2.0",
                        "id": "turn-req-1",
                        "method": "runtime.chat.message",
                        "params": {
                            "turn_request_id": "gesture-from-windows-1",
                            "persona_id": PERSONA,
                            "persona_instance_id": instance_id,
                            "message": "hello from Windows",
                            "new_session": True,
                        },
                    }
                )
                ack_at, ack = recorder.wait_for(
                    lambda frame: frame.get("id") == "turn-req-1",
                    what="the chat turn ack",
                )

                # (c) the ack's shape, exactly as the plan states it.
                assert "error" not in ack, ack
                result = ack["result"]
                assert result["turn_request_id"] == "gesture-from-windows-1"
                assert result["accepted"] is True
                assert result["state"] == "accepted"
                request_id = result["request_id"]
                assert request_id

                # (d) WHICH LANE. Wait for the turn to finish on the per-request
                #     lane — its ``exit`` frame is the one unambiguous end — and
                #     then give the stream lane a bounded, generous window to
                #     say anything about the turn at all.
                exit_at, exit_frame = recorder.wait_for(
                    lambda frame: frame.get("id") == request_id
                    and frame.get("event") == "exit",
                    what="the turn's exit on the per-request lane",
                    timeout=120.0,
                )
                _settle_stream(recorder, seconds=3.0)
            finally:
                recorder.stop()

    per_request = recorder.matching(_request_frame(request_id))
    stream_frames = recorder.matching(_is_stream)
    payload = _payload_of(per_request)

    said = _stream_says_about(
        stream_frames, session_id=(payload or {}).get("session_id", ""), request_id=request_id
    )
    _print_lane_report(recorder, request_id=request_id, sent_at=sent_at, said=said)

    # ── (a) the per-request frame lane: it carried the whole turn ────────────
    assert exit_frame["code"] == 0, payload
    assert payload is not None, "no --json payload on the per-request lane"
    assert payload["ok"] is True, payload
    assert payload["reply"] == SCRIPTED_REPLY
    assert payload["session_id"]
    # The turn's START is observable on this lane too: the first frame under the
    # request id arrives before its exit.
    assert per_request[0][0] < exit_at

    # ── (b) the stream lane ─────────────────────────────────────────────────
    # Recorded rather than hoped for, and asserted only where it is STABLE at
    # this speed. Whether a ``running_work`` row for the turn was sampled
    # depends on whether a poll landed inside a turn that took ~500 ms; that
    # question is asked properly by the slow measurement below, where the turn
    # is provably still running while the lane is sampling. Asserting emptiness
    # here would be asserting a race.
    assert stream_frames, "the stream lane delivered nothing at all"
    assert hydrate is not None and hydrate_at >= subscribed_at
    # The turn's ROOT is named on this lane, so a second subscriber learns which
    # conversation moved.
    assert said["named_the_session"] is True, said
    # And these two are the findings C1l has to build its receipt around: the
    # reply TEXT never rides this lane, and the ``request_id`` the ack returned
    # is not a key anything on it can be joined by.
    assert said["carried_the_reply_text"] is False, said
    assert said["named_the_request_id"] is False, said


def _settle_stream(recorder: _Recorder, *, seconds: float) -> None:
    """Let the stream lane's poll cadence run out after the turn is over.

    The hub polls the event log; a delta the turn caused can legitimately arrive
    after the turn's own exit frame. Waiting a bounded, generous window is the
    difference between "the stream lane carried nothing" and "the test asked too
    early", and only the first of those is a finding.
    """

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)


def _payload_of(per_request: list[tuple[float, dict]]) -> dict | None:
    """The ``--json`` payload parsed out of the request's ``line`` frames."""

    body = "\n".join(
        frame.get("line", "")
        for _at, frame in per_request
        if frame.get("event") == "line"
    ).strip()
    if not body:
        return None
    for candidate in (body, body.splitlines()[-1]):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _stream_says_about(
    stream_frames: list[tuple[float, dict]], *, session_id: str, request_id: str
) -> dict:
    """What the stream lane published about this turn. Facts, not a verdict.

    Read against the frame shape the committed goldens pin
    (``tests/fixtures/stream_frames/``): a frame carries a top-level ``type``,
    a delta carries ``op`` / ``entity`` / ``coalesced_count``, and the
    projection — ``running_work`` included — hangs under ``core``.
    """

    reply_seen = False
    session_seen = False
    request_id_seen = False
    running_rows: list[dict] = []
    running_sections = 0
    running_row_kinds: list[str] = []
    delta_ops: list[str] = []
    event_types: list[str] = []
    for _at, frame in stream_frames:
        rendered = json.dumps(frame)
        if SCRIPTED_REPLY in rendered:
            reply_seen = True
        if session_id and session_id in rendered:
            session_seen = True
        if request_id in rendered:
            request_id_seen = True
        op = frame.get("op")
        if isinstance(op, str):
            delta_ops.append(op)
        entity = frame.get("entity")
        if isinstance(entity, dict):
            event = entity.get("event")
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                event_types.append(event["type"])
        for event in frame.get("events") or []:
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                event_types.append(event["type"])
        core = frame.get("core")
        running = core.get("running_work") if isinstance(core, dict) else None
        if isinstance(running, dict):
            running_sections += 1
        rows = running.get("rows") if isinstance(running, dict) else None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            running_row_kinds.append(str(row.get("kind")))
            if row.get("kind") == "chat_turn":
                running_rows.append(row)
    return {
        "frame_count": len(stream_frames),
        "frame_types": [frame.get("type") for _at, frame in stream_frames],
        "carried_the_reply_text": reply_seen,
        "named_the_session": session_seen,
        "named_the_request_id": request_id_seen,
        # Both numbers, because "no chat_turn row" and "no running_work section
        # was published at all" are different findings and only one of them is
        # about chat.
        "frames_carrying_a_running_work_section": running_sections,
        "running_work_row_kinds": sorted(set(running_row_kinds)),
        "running_work_chat_turn_rows": running_rows,
        "delta_ops": sorted(set(delta_ops)),
        "event_types": sorted(set(event_types)),
    }


def _print_lane_report(
    recorder: _Recorder, *, request_id: str, sent_at: float, said: dict
) -> None:
    """The measurement, in the form the field notes quote."""

    print("\n=== LANE REPORT (ms from the stream subscription) ===")
    for at, frame in recorder.frames():
        if _is_stream(frame):
            payload = _stream_payload(frame)
            what = f"stream/{payload.get('type')} watermark={payload.get('watermark')}"
        elif frame.get("id") == request_id:
            what = f"per-request/{frame.get('event')} id={request_id}"
        else:
            what = f"{frame.get('event') or 'rpc-reply'} id={frame.get('id')}"
        marker = " <- turn sent" if at >= sent_at > at - 0.0005 else ""
        print(f"  {recorder.since_start_ms(at):9.1f} ms  {what}{marker}")
    print(f"  (turn sent at {recorder.since_start_ms(sent_at):.1f} ms)")
    print("=== WHAT THE STREAM LANE SAID ABOUT THIS TURN ===")
    for key, value in said.items():
        print(f"  {key}: {value}")


#: How long the slow measurement holds its turn open. Long enough to cross
#: several of the hub's poll cycles (the fast turn above finished inside one),
#: bounded so the file stays under its wall.
SLOW_TURN_SECONDS = 12.0

#: How many real writes are issued during the dwell to force the hub to publish,
#: and how long each one is given to produce a frame. Their product stays under
#: :data:`SLOW_TURN_SECONDS` so every publish happens while the turn is running.
_MID_TURN_NUDGES = 5
_MID_TURN_NUDGE_WAIT_SECONDS = 1.5


@pytest.fixture
def slow_scripted_model(monkeypatch, scripted_model):
    """The same scripted agent, holding its turn open across poll cycles."""

    monkeypatch.setattr(_ScriptedAgent, "dwell_seconds", SLOW_TURN_SECONDS)


@pytest.mark.timeout(300)
def test_a_running_turn_is_in_the_projection_but_nothing_publishes_it(
    gateway_on, placed_agent, slow_scripted_model, capfd
):
    """The other half of the measurement, and the one C1l's shape turns on.

    The fast turn above finished inside a single hub publish, so its empty
    ``running_work`` was not evidence: "the row is never published" and "nobody
    published while it existed" are the same observation at that speed. This
    holds the turn open for :data:`SLOW_TURN_SECONDS` and asks the question of a
    turn that is provably in flight.

    **The question has to be asked in two halves, because the first way of
    asking it is not stable and the instability IS the finding.** Simply waiting
    for a row to appear during a slow turn passes on some runs and times out on
    others — the stream hub is EVENT-DRIVEN, so it publishes when the event log
    moves, and a chat turn running a model appends nothing of its own until it
    is over. So:

    1. a delta is FORCED while the turn is in flight, by doing something
       unrelated on the same connection (placing a second agent — the shape of
       an operator moving the office while a turn runs). What the projection on
       that frame says is a fact about the PROJECTION, asked at a moment this
       test chose rather than one the poll phase chose.
    2. the standing caveat is recorded rather than asserted: nothing publishes a
       frame on the turn's own account, so a second console holding only this
       lane may see a running turn LATE or not until something else moves.

    A SECOND console watching the same runtime (the Mac's own launcher, which is
    exactly C5's arrangement) has only this lane.
    """

    from agent_runtime import serve_rpc

    instance_id = placed_agent["persona_instance_id"]
    credential = pair_device(tier=TIER_CONSOLE, name="the mac")

    with running_serve(dispatch=dispatch_argv) as handle:
        with device_client(handle, credential) as (connection, hello):
            recorder = _Recorder(connection)
            recorder.start()
            try:
                connection.send({"op": "subscribe", "lane": "stream"})
                recorder.wait_for(
                    lambda frame: frame.get("event") == "subscribed",
                    what="the stream subscription ack",
                )
                recorder.wait_for(
                    lambda frame: _is_stream(frame) and frame.get("type") == "hydrate",
                    what="the stream lane's hydrate",
                )

                sent_at = time.monotonic()
                connection.send(
                    {
                        "jsonrpc": "2.0",
                        "id": "slow-turn-1",
                        "method": "runtime.chat.message",
                        "params": {
                            "turn_request_id": "slow-gesture-1",
                            "persona_id": PERSONA,
                            "persona_instance_id": instance_id,
                            "message": "hello from Windows, slowly",
                            "new_session": True,
                        },
                    }
                )
                _ack_at, ack = recorder.wait_for(
                    lambda frame: frame.get("id") == "slow-turn-1",
                    what="the chat turn ack",
                )
                assert "error" not in ack, ack
                request_id = ack["result"]["request_id"]

                # (1) the forced publishes, mid-turn. A second placement is a
                # real write on a real verb: it appends events, so the hub
                # publishes, so the projection on that frame is taken while this
                # turn is running. Issued REPEATEDLY across the dwell rather than
                # once, because a single nudge fired the instant the ack came
                # back can be projected before the turn's journal row exists —
                # and "the projection was taken too early" would read exactly
                # like "the projection never carries the row".
                projected_at = None
                for index in range(_MID_TURN_NUDGES):
                    nudge_id = f"nudge-{index}"
                    connection.send(
                        {
                            "jsonrpc": "2.0",
                            "id": nudge_id,
                            "method": "runtime.agent.create",
                            "params": {
                                "persona_id": PERSONA,
                                "workspace_id": WORKSPACE,
                                "position": [7.0 + index, 8.0],
                                "idempotency_key": f"mid-turn-nudge-{index}",
                                "placement_id": f"qa_agent_c0ffee0{index}",
                            },
                        }
                    )
                    _nudge_at, nudge = recorder.wait_for(
                        lambda frame, wanted=nudge_id: frame.get("id") == wanted,
                        what=f"the mid-turn placement ack {nudge_id}",
                    )
                    assert "error" not in nudge, nudge
                    try:
                        projected_at, _frame = recorder.wait_for(
                            _carries_running_chat_turn("slow-gesture-1"),
                            what="a running_work chat_turn row for this turn",
                            timeout=_MID_TURN_NUDGE_WAIT_SECONDS,
                        )
                    except AssertionError:
                        continue
                    break
                assert projected_at is not None, (
                    "no stream frame carried a running_work row for this turn, "
                    f"across {_MID_TURN_NUDGES} forced publishes"
                )
                first_row_ms = (projected_at - sent_at) * 1000.0
                in_flight = recorder.matching(_is_stream)
                exited_early = recorder.matching(
                    lambda frame: frame.get("id") == request_id
                    and frame.get("event") == "exit"
                )

                recorder.wait_for(
                    lambda frame: frame.get("id") == request_id
                    and frame.get("event") == "exit",
                    what="the slow turn's exit",
                    timeout=180.0,
                )
                _settle_stream(recorder, seconds=3.0)
            finally:
                recorder.stop()

    assert not exited_early, "the projection was taken after the turn had ended"

    per_request = recorder.matching(_request_frame(request_id))
    payload = _payload_of(per_request)
    assert payload is not None and payload["ok"] is True, payload
    assert payload["reply"] == SCRIPTED_REPLY

    mid = _stream_says_about(
        in_flight, session_id=payload["session_id"], request_id=request_id
    )
    whole = _stream_says_about(
        recorder.matching(_is_stream),
        session_id=payload["session_id"],
        request_id=request_id,
    )
    print("\n=== WHILE THE TURN WAS STILL RUNNING ===")
    print(f"  projection forced at: +{first_row_ms:.0f} ms after the send")
    for key, value in mid.items():
        print(f"  {key}: {value}")
    print("=== OVER THE WHOLE TURN ===")
    for key, value in whole.items():
        print(f"  {key}: {value}")

    # ── the finding ─────────────────────────────────────────────────────────
    # A turn that is still running IS in the projection the stream lane
    # publishes, as a ``running_work`` row — so a second console CAN paint a
    # pending bubble for a turn it did not start, once a frame arrives.
    rows = mid["running_work_chat_turn_rows"]
    assert rows, mid
    row = rows[-1]
    assert row["status"] == "running"
    # The join key, and it is the LAUNCHER'S OWN: ``work_id`` is
    # ``chat_turn:<turn_request_id>``, the id the console minted and sent — not
    # the server-minted ``request_id``, which never appears on this lane at all.
    # R-C3's reply receipt therefore joins on the value the send already holds.
    assert row["work_id"] == "chat_turn:slow-gesture-1"
    assert row["owner"]["session_id"] == payload["session_id"]
    assert row["owner"]["persona_instance_id"] == instance_id

    # What the lane still does NOT carry, at any speed: the reply text, and the
    # ack's ``request_id``.
    assert whole["carried_the_reply_text"] is False, whole
    assert whole["named_the_request_id"] is False, whole
