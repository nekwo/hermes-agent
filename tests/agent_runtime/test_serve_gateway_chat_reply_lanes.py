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

C1h-bis, in this file
---------------------
C1h's answer came with a caveat it had to work around: **nothing published a
stream frame on a chat turn's own account**, so the second measurement below
could only sample the ``running_work`` row by FORCING publishes with unrelated
real writes. Stage C1h-bis gave the chat-turn core its own two appends
(``persona_chat.turn_started`` / ``persona_chat.turn_ended``,
``agent_runtime.chat_turn_presence``), so the forcing writes are gone and the
same test now asserts what it previously had to arrange: the row arrives on the
lane within a small multiple of one hub publish interval of the ack, and is
retired within the same of the turn's exit. The caveat is kept as history in the
field notes, not re-asserted here.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_runtime.call_authorization import TIER_CONSOLE
from agent_runtime.mission_chat_phases import TURN_TIMING_ORDER
from agent_runtime.serve_rpc import RPC_CONTRACT_VERSION
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


def _event_types_of(frame: dict) -> list[str]:
    """Every event type a frame NAMES — the single-event ``entity`` and, on a
    coalesced batch, each row of ``events``."""

    found: list[str] = []
    entity = frame.get("entity")
    if isinstance(entity, dict):
        event = entity.get("event")
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            found.append(event["type"])
    for event in frame.get("events") or []:
        if isinstance(event, dict):
            inner = event.get("event")
            if isinstance(inner, dict) and isinstance(inner.get("type"), str):
                found.append(inner["type"])
    return found


def _publishes_the_start(turn_request_id: str):
    """A frame that NAMES the turn's own start event AND carries its row.

    Both halves, and the conjunction is the whole point. "A frame carrying the
    row" alone does not prove the turn published anything: with
    ``new_session: true`` the runtime also appends ``persona_instance.created``
    and ``persona_instance.chat_opened`` around the same moment, and a core
    built for one of THOSE can pick the row up if the write-ahead happened to
    land first. That is precisely the race C1h measured — it passed on some runs
    and timed out on others. Requiring the frame to name
    ``persona_chat.turn_started`` asks the question this stage actually owns:
    did the turn's OWN publish put the row in front of a subscriber.
    """

    carries = _carries_running_chat_turn(turn_request_id)

    def _predicate(frame: dict) -> bool:
        return carries(frame) and "persona_chat.turn_started" in _event_types_of(frame)

    return _predicate


def _publishes_the_end(turn_request_id: str):
    """A frame that names the turn's end event and no longer holds its row.

    The ``running_work`` section must be PRESENT: a frame without one (a
    ``patch``, or a hydrate that predates the store) says nothing about running
    work, which is a different fact from "this turn is over" and must not be
    allowed to answer for the end publish.
    """

    work_id = f"chat_turn:{turn_request_id}"

    def _predicate(frame: dict) -> bool:
        if not _is_stream(frame):
            return False
        if "persona_chat.turn_ended" not in _event_types_of(frame):
            return False
        core = frame.get("core")
        running = core.get("running_work") if isinstance(core, dict) else None
        if not isinstance(running, dict):
            return False
        rows = running.get("rows")
        return not any(
            isinstance(row, dict) and row.get("work_id") == work_id
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

    # ── (a2) RO-7: the turn's own timing split rides that same payload ───────
    #
    # The per-request lane's LAST frame is the ``exit`` above, and it carries a
    # code and nothing else — the launcher's fold models it as exactly that
    # (``MissionControlStreamLine.exit(code)``, routed ``terminal: true`` by
    # ``mission_control_serve_session_io.dart``). The last frame that carries a
    # PAYLOAD is the ``--json`` object parsed above, which the launcher decodes
    # into a map and its bridge calls "the conversational terminal". That is
    # where a block a consumer has to READ can ride.
    #
    # This turn's agent is scripted and never reaches a provider, which makes
    # it the honest demonstration of the absence rule on a REAL serve: the
    # runner reported no provider durations, so those keys are not there — not
    # zeroed.
    timing = payload.get("timing")
    assert timing is not None, payload
    assert set(timing) <= set(TURN_TIMING_ORDER), timing
    assert isinstance(timing["resident_actor_reused"], bool)
    runner_timing = payload.get("profile_timing") or {}
    for wire_key, source_key in (
        ("turn_context_ms", "profile_conversation_turn_context_ms"),
        ("responses_create_ms", "profile_provider_responses_create_ms"),
        ("stream_consume_ms", "profile_provider_stream_consume_ms"),
    ):
        assert (wire_key in timing) == (source_key in runner_timing), (
            wire_key,
            timing,
            runner_timing,
        )
    # ADDITIVE: the method surface's contract integer did not move for it, and
    # the greeting a paired device gates on still says what it said.
    assert hello["rpc"]["contract"] == RPC_CONTRACT_VERSION

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
        # ONE extraction, shared with the two publish predicates. The version
        # inlined here read ``type`` off the top of each ``events`` row, where
        # ``_delta_entity`` does not put it, so a coalesced batch's types went
        # unreported — which is exactly the kind of quiet under-reporting a
        # measurement file must not have.
        event_types.extend(_event_types_of(frame))
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

#: ONE publish interval of the serve hub's producer, from ``stream_frames``'
#: own defaults: ``poll_interval_seconds`` 0.25 + ``delta_debounce_seconds``
#: 0.2. A frame also costs the core build the delta carries, which is real work
#: on a real serve and is not a constant, so the deadlines below are multiples
#: of this rather than the number itself.
_PUBLISH_INTERVAL_SECONDS = 0.45

#: How long the turn's OWN start publish gets to put a row on the lane, measured
#: from the ACK. Half the dwell, and the reason it is not one publish interval is
#: a measured fact worth stating: the ack is returned when the turn is ACCEPTED,
#: and the row cannot exist until the write-ahead record lands, which is after
#: the session mint, the chat open and the actor prewarm — 1.4–1.9 s of real
#: work in this fixture. So this bound says "promptly, and by a margin that
#: cannot be the turn's end"; :data:`_START_ROW_ELAPSED_CEILING_SECONDS` is the
#: one that says "within a publish interval of the row becoming real".
_START_ROW_ACK_CEILING_SECONDS = SLOW_TURN_SECONDS / 2

#: The tight bound, read off the ROW rather than off the clock: the turn's own
#: ``elapsed_seconds`` at the moment the frame carrying it was built. Anchored on
#: the write-ahead stamp, so it measures exactly the gap this stage closed — one
#: publish interval, one core build, and an integer floor. Before C1h-bis no
#: frame carried the row at all inside the dwell.
_START_ROW_ELAPSED_CEILING_SECONDS = 3

#: How long the END publish gets to retire the row, measured from the turn's exit
#: frame. A small multiple of one publish interval: by the exit the journal has
#: already made its terminal transition, so this is the hub's latency and
#: nothing else.
_END_ROW_DEADLINE_SECONDS = 8 * _PUBLISH_INTERVAL_SECONDS


@pytest.fixture
def slow_scripted_model(monkeypatch, scripted_model):
    """The same scripted agent, holding its turn open across poll cycles."""

    monkeypatch.setattr(_ScriptedAgent, "dwell_seconds", SLOW_TURN_SECONDS)


@pytest.mark.timeout(300)
def test_a_running_turn_publishes_its_own_start_and_end_on_the_stream_lane(
    gateway_on, placed_agent, slow_scripted_model, capfd
):
    """The other half of the measurement, and the one C1l's shape turns on.

    The fast turn above finished inside a single hub publish, so its empty
    ``running_work`` was not evidence: "the row is never published" and "nobody
    published while it existed" are the same observation at that speed. This
    holds the turn open for :data:`SLOW_TURN_SECONDS` and asks the question of a
    turn that is provably in flight.

    **What this asserted before C1h-bis, and why it had to.** The stream hub is
    EVENT-DRIVEN — it publishes when the event log moves — and a chat turn
    running a model appended nothing of its own between its write-ahead record
    and its projection commit. So simply waiting for the row during a slow turn
    passed on some runs and timed out on others, and that instability was the
    finding rather than a flake to tune away: the row was in the projection and
    no frame carried it. The test therefore FORCED publishes — up to five real
    ``runtime.agent.create`` calls across the dwell, the shape of an operator
    moving the office while a turn runs — and asked what the projection said at a
    moment it chose. The C1h field notes keep that measurement (a row sampled
    +3187 ms into a turn, ``owner.persona_id`` null) as history.

    **What it asserts now.** The turn publishes on its own account
    (``persona_chat.turn_started`` at the write-ahead,
    ``persona_chat.turn_ended`` when it leaves the in-flight set — see
    ``agent_runtime.chat_turn_presence``), so the forcing writes are GONE and
    nothing else moves the event log inside this test's windows. The row must
    arrive within :data:`_START_ROW_ACK_CEILING_SECONDS` of the ack AND carry an
    ``elapsed_seconds`` no greater than
    :data:`_START_ROW_ELAPSED_CEILING_SECONDS` — the second is the tight bound,
    because it is anchored on the write-ahead stamp rather than on an ack that
    precedes the row's existence by the whole session mint. It must then be
    RETIRED within :data:`_END_ROW_DEADLINE_SECONDS` of the turn's exit.

    A SECOND console watching the same runtime (the Mac's own launcher, which is
    exactly C5's arrangement) has only this lane.
    """

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
                ack_at, ack = recorder.wait_for(
                    lambda frame: frame.get("id") == "slow-turn-1",
                    what="the chat turn ack",
                )
                assert "error" not in ack, ack
                request_id = ack["result"]["request_id"]

                # (1) THE START, on the turn's own account. No forcing write:
                # nothing else touches this runtime between the ack and the row,
                # so a frame carrying it can only have come from the turn's own
                # ``persona_chat.turn_started`` append. Before C1h-bis this wait
                # timed out for the whole dwell.
                started_at, _start_frame = recorder.wait_for(
                    _publishes_the_start("slow-gesture-1"),
                    what="the frame that publishes this turn's own start row",
                    timeout=_START_ROW_ACK_CEILING_SECONDS,
                )
                start_row_ms = (started_at - ack_at) * 1000.0
                first_row_ms = (started_at - sent_at) * 1000.0
                in_flight = recorder.matching(_is_stream)
                exited_early = recorder.matching(
                    lambda frame: frame.get("id") == request_id
                    and frame.get("event") == "exit"
                )

                exit_at, _exit_frame = recorder.wait_for(
                    lambda frame: frame.get("id") == request_id
                    and frame.get("event") == "exit",
                    what="the slow turn's exit",
                    timeout=180.0,
                )
                # (2) THE END. The row has to be RETIRED on the lane, not merely
                # stop being republished: a second console paints a pending
                # bubble off this row and needs a frame that no longer carries
                # it. Identified by the turn's own end event for the same reason
                # the start is — an empty projection on an unrelated frame is
                # not this turn's end being published.
                ended_at, _end_frame = recorder.wait_for(
                    _publishes_the_end("slow-gesture-1"),
                    what="the frame that publishes this turn's end",
                    timeout=_END_ROW_DEADLINE_SECONDS,
                )
                end_row_ms = (ended_at - exit_at) * 1000.0
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
    print(f"  start row on the stream lane: +{start_row_ms:.0f} ms after the ack")
    print(f"                               (+{first_row_ms:.0f} ms after the send)")
    print(f"  row retired on the stream lane: +{end_row_ms:.0f} ms after the exit")
    for key, value in mid.items():
        print(f"  {key}: {value}")
    print("=== OVER THE WHOLE TURN ===")
    for key, value in whole.items():
        print(f"  {key}: {value}")

    # ── the finding ─────────────────────────────────────────────────────────
    # A turn that is still running IS in the projection the stream lane
    # publishes, as a ``running_work`` row — and since C1h-bis the turn itself
    # is what puts a frame carrying it on the lane, so a second console CAN
    # paint a pending bubble for a turn it did not start without waiting for an
    # unrelated write.
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
    # C1h measured this field NULL beside the two above it. A console rendering
    # "who is talking" reads it, so it is asserted rather than described.
    assert row["owner"]["persona_id"] == PERSONA

    # Both publishes are PROMPT, and the numbers are the assertion. Nothing else
    # moved this runtime's event log in either window, so these are the turn's
    # own two appends and nobody else's.
    #
    # The start is bounded TWICE because the two bounds answer different
    # questions. Off the clock: it arrived within half the dwell of the ack, so
    # it cannot be the turn's end arriving. Off the ROW: the turn had been in
    # flight for at most :data:`_START_ROW_ELAPSED_CEILING_SECONDS` when the
    # frame carrying it was built — that is one publish interval plus a core
    # build plus the field's integer floor, measured from the write-ahead stamp
    # rather than from the ack that precedes it by the whole session mint.
    assert start_row_ms < _START_ROW_ACK_CEILING_SECONDS * 1000.0, start_row_ms
    assert row["elapsed_seconds"] <= _START_ROW_ELAPSED_CEILING_SECONDS, row
    assert end_row_ms < _END_ROW_DEADLINE_SECONDS * 1000.0, end_row_ms

    # And the two publishes are on the record as the turn's OWN, by type.
    assert "persona_chat.turn_started" in whole["event_types"], whole
    assert "persona_chat.turn_ended" in whole["event_types"], whole

    # What the lane still does NOT carry, at any speed: the reply text, and the
    # ack's ``request_id``.
    assert whole["carried_the_reply_text"] is False, whole
    assert whole["named_the_request_id"] is False, whole
