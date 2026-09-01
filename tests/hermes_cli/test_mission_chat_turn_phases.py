"""Turn-record phase spans (schema v3) — the honesty contract, as executable rows.

The operator's report was "my messaging feels slow", and answering it required
hand-correlating ``agent.log`` prose, the turn record's wall stamps, the
emitter's ``ttft_ms`` (whose clock starts ~1,100 lines into the handler) and the
launcher's dateless local-time diag lines. The record now carries the breakdown.

**What these rows defend is not the presence of numbers — it is the ABSENCE of
the ones that were never measured.** A phase the turn did not reach has no key.
Not ``0``, not ``null``, not present-and-empty. A ``0`` is a measurement; a
phase that never happened was not measured, and a consumer that cannot tell
those apart will report a turn that died before the provider as one that reached
the provider instantaneously. That is the exact class of defect this whole plan
exists to prevent, so the named sabotage for this stage is "make the writer emit
``0`` for an unreached ``provider_first_byte``" and these rows must red on it.

Driven end-to-end through the REAL handler wherever a fact is about the handler
(which phases a live turn marks, what lands on the durable record), and at the
``TurnPhaseMarks`` unit wherever a fact is about the clock (exact values,
first-mark-wins), because only the unit can be given a scripted clock without
putting a test seam on a live chat path.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.conversation_loop import _emit_request_assembled_marker
from agent_runtime import mission_chat_phases
from agent_runtime.mission_chat_phases import (
    PHASE_ORDER,
    TURN_PHASES_KEY,
    TURN_RECORD_SCHEMA_VERSION,
    TurnPhaseMarks,
    mark_from_trace_payload,
    safe_turn_phases,
)
from agent_runtime.mission_chat_turns import TURN_PROFILE_TIMING_KEY
from agent_runtime.persona_runtime import _chat_trace_callback
from agent_runtime.profile_runner import AgentRunRequest, _profile_status_callback
from hermes_constants import CONVERSATION_REQUEST_ASSEMBLED_STEP

from tests.hermes_cli.test_mission_chat_budget_payload import (  # type: ignore
    _SESSION_ID,
    _seed,
    isolate_agent_runtime_root,  # noqa: F401  (re-exported fixture)
)


# --------------------------------------------------------------------------- #
# Scripted clock                                                               #
# --------------------------------------------------------------------------- #
class _TickClock:
    """A monotonic clock that advances exactly one second per READ.

    Deterministic ORDER without pinning a call count: asserting "context_built
    == 3000" would break the moment an unrelated edit takes one more clock
    reading, which is a test that fails for a reason that is not a defect. What
    a scripted clock is actually for here is proving that the marks come out in
    the order the turn passes through them, and that ``request_received`` is the
    anchor rather than a measurement.
    """

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> float:
        value = float(self.reads)
        self.reads += 1
        return value


@pytest.fixture
def scripted_marks(monkeypatch):
    """Give the LIVE handler a scripted anchor.

    ``persona_commands`` is exec'd into ``harness.py`` globals, so its import of
    ``TurnPhaseMarks`` is function-local and re-executed on every turn — which
    means patching the class on its owning module reaches the real handler
    without any seam existing in the handler itself.
    """

    created: list[TurnPhaseMarks] = []

    def _factory():
        marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "2026-08-21T21:17:43.400000Z")
        created.append(marks)
        return marks

    monkeypatch.setattr(mission_chat_phases, "TurnPhaseMarks", _factory)
    return created


# --------------------------------------------------------------------------- #
# Providers                                                                    #
# --------------------------------------------------------------------------- #
def _result(**timing):
    return SimpleNamespace(
        final_response="hello world",
        input_tokens=1,
        output_tokens=2,
        total_tokens=3,
        latency_ms=4,
        profile_timing=dict(timing),
        raw={},
    )


def _streaming_provider(*, profile_timing=None, deltas=("hello ", "world")):
    """A provider that walks the real callback protocol: ready, then tokens."""

    timing = dict(profile_timing or {})

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, *args, **kwargs):
            ready = kwargs.get("agent_ready_callback")
            if ready is not None:
                # The real runner swallows a raising ready-callback
                # (`profile_runner._notify_agent_ready`); mirroring that here
                # keeps the fake from being kinder OR harsher than production.
                try:
                    ready(SimpleNamespace(id="fake-agent"))
                except Exception:
                    pass
            # The conversation loop announces the dispatch instant as a trace
            # payload right before the provider call — after the ready
            # callback, before the first delta. The fake walks the same
            # protocol so the wiring (payload → `request_assembled` mark) is
            # what the tests prove, not the fake.
            #
            # And it walks it through the REAL chain, not by calling the
            # handler's `trace_callback` directly: on the live lane the payload
            # crosses `profile_runner._profile_status_callback` and a REAL
            # `ChatProgressSink`, and the sink dropped it — every live turn
            # record through 2026-08-23 lacked `request_assembled` while a fake
            # that skipped the sink reported the wiring healthy. Faking only the
            # transport keeps that hole closed.
            trace = kwargs.get("trace_callback")
            if trace is not None:
                progress_callback = _chat_trace_callback(
                    session_id=kwargs.get("permission_session_id") or _SESSION_ID,
                    persona=args[0] if args else SimpleNamespace(id="dev"),
                    turn_id=kwargs.get("turn_id"),
                    on_trace=trace,
                )
                request = AgentRunRequest(
                    profile=None,
                    provider="openai-codex",
                    model="gpt-5.6-luna",
                    progress_callback=progress_callback,
                )
                _emit_request_assembled_marker(
                    SimpleNamespace(
                        status_callback=_profile_status_callback(request, {})
                    ),
                    api_call_count=1,
                )
            stream = kwargs.get("stream_callback")
            if stream is not None:
                for chunk in deltas:
                    stream(chunk)
            return _result(**timing)

    return _Provider


def _never_reaches_the_provider():
    """The turn the absence rule exists for: it dies with no token ever seen."""

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, *args, **kwargs):
            raise RuntimeError("provider transport died before any byte")

    return _Provider


def _args(client_message_id: str, *, stream: bool):
    return SimpleNamespace(
        persona_id="dev",
        persona_instance_id="personainst_dev",
        session_id=_SESSION_ID,
        message="please answer",
        surface_prompt="",
        intent_hint="chat",
        requested_by="test",
        client_message_id=client_message_id,
        stream=stream,
        max_seconds=5.0,
        json=True,
    )


# --------------------------------------------------------------------------- #
# Reading the DURABLE record (never the envelope — the record is the contract)  #
# --------------------------------------------------------------------------- #
def _record_on_disk(root: Path, client_message_id: str) -> dict:
    store = Path(root) / "mission_chat_turns"
    for path in sorted(store.glob("*.json")):
        session = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(session, dict) and client_message_id in session:
            return session[client_message_id]
    raise AssertionError(
        f"no turn record for {client_message_id!r} under {store} "
        f"(files: {sorted(p.name for p in store.glob('*.json'))})"
    )


def _drive(monkeypatch, capsys, provider, *, turn_id, stream=True):
    harness = _seed(monkeypatch, provider)
    code = harness._cmd_mission_chat_message(_args(turn_id, stream=stream))
    capsys.readouterr()  # stream frames + terminal envelope; the record is the subject
    return code


# --------------------------------------------------------------------------- #
# 1. A live turn writes the block, on schema v3                                #
# --------------------------------------------------------------------------- #
@pytest.fixture
def streamed_turn(monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks):  # noqa: F811
    _drive(
        monkeypatch,
        capsys,
        _streaming_provider(profile_timing={"resident_actor_reused": 0}),
        turn_id="phases_ok",
    )
    return _record_on_disk(isolate_agent_runtime_root, "phases_ok")


def test_a_completed_turn_carries_a_phases_block_on_schema_v3(streamed_turn):
    assert streamed_turn["schema_version"] == TURN_RECORD_SCHEMA_VERSION == 3
    assert TURN_PHASES_KEY in streamed_turn, (
        "the terminal persist must carry the phase block; without it the turn "
        "record is back to wall stamps and prose"
    )


def test_the_block_carries_the_wall_anchor_as_a_STRING(streamed_turn):
    """``anchored_at`` exists to cross-reference agent.log BY EYE.

    It is a string precisely so that nothing can subtract it from anything. The
    launcher stamps local time with no dates while the record stamps UTC, and
    that hand-correlation hazard has already produced two misreads in a day.
    """

    phases = streamed_turn[TURN_PHASES_KEY]
    assert isinstance(phases["anchored_at"], str)
    for key, value in phases.items():
        if key == "anchored_at":
            continue
        assert isinstance(value, (int, bool)), (
            f"{key} must be an elapsed-ms int (or a bool flag), got {value!r}"
        )


def test_request_received_is_the_anchor_and_is_always_zero(streamed_turn):
    assert streamed_turn[TURN_PHASES_KEY]["request_received"] == 0


def test_a_streamed_turn_marks_every_phase_it_actually_passed_through(streamed_turn):
    """The full walk, including the two marks that bracket profile bootstrap.

    ``agent_ready`` / ``provider_request_started`` come from the runner's
    ready-callback and ``provider_first_byte`` from the first delta, so a fake
    that walks the real callback protocol proves the wiring, not the fake.
    """

    phases = streamed_turn[TURN_PHASES_KEY]
    for phase in PHASE_ORDER:
        assert phase in phases, f"a completed streamed turn must mark {phase}"


def test_the_marks_are_non_decreasing_in_lifecycle_order(streamed_turn):
    phases = streamed_turn[TURN_PHASES_KEY]
    seen = [(phase, phases[phase]) for phase in PHASE_ORDER if phase in phases]
    values = [value for _, value in seen]
    assert values == sorted(values), (
        f"phase marks are out of lifecycle order: {seen}"
    )


def test_the_scripted_clock_makes_every_phase_a_distinct_instant(streamed_turn):
    """One tick per clock read, so no two phases can collapse onto one value.

    A real turn may legitimately mark two phases in the same millisecond; the
    scripted clock removes that ambiguity so "the ordering holds" is asserted
    against a timeline where ordering is actually observable.
    """

    phases = streamed_turn[TURN_PHASES_KEY]
    values = [phases[phase] for phase in PHASE_ORDER if phase in phases]
    assert len(set(values)) == len(values), f"phases collapsed onto one instant: {values}"


# --------------------------------------------------------------------------- #
# 2. THE ABSENCE RULE — the row the named sabotage must red                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def dead_before_provider(monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks):  # noqa: F811
    code = _drive(
        monkeypatch, capsys, _never_reaches_the_provider(), turn_id="phases_dead"
    )
    assert code == 2
    return _record_on_disk(isolate_agent_runtime_root, "phases_dead")


@pytest.mark.parametrize(
    "phase", ["request_assembled", "provider_first_byte", "stream_done", "projected"]
)
def test_a_phase_the_turn_never_reached_has_NO_KEY(dead_before_provider, phase):
    """Absent means the key is not there. Zero is a measurement; this was not.

    A turn whose provider transport died saw no byte. If ``provider_first_byte``
    materialized as ``0`` the record would read as an instantaneous first token
    — the single most misleading value the field could hold, and the reason the
    honesty contract's first rule exists.
    """

    phases = dead_before_provider[TURN_PHASES_KEY]
    assert phase not in phases, (
        f"absent phase materialized as {phases.get(phase)!r}: {phase} was never "
        f"reached by this turn and must have NO key on the record"
    )


def test_the_dead_turn_still_records_what_it_DID_reach(dead_before_provider):
    """Absence is only honest if presence is real: the admission half is there."""

    phases = dead_before_provider[TURN_PHASES_KEY]
    for phase in ("request_received", "context_built", "observability_built",
                  "emitter_created", "write_ahead"):
        assert phase in phases, f"{phase} happened on this turn and must be recorded"


def test_the_dead_turn_carries_no_stage_four_counters(dead_before_provider):
    """``builds_overlapped`` is defined over anchor → stream_done.

    No ``stream_done`` means no window, and no window means no count — not a
    zero. A zero here would claim "no builds overlapped this turn", which is a
    finding nobody made.
    """

    assert "builds_overlapped" not in dead_before_provider[TURN_PHASES_KEY]


# --------------------------------------------------------------------------- #
# 3. First mark wins — the rule that survives a per-token call site            #
# --------------------------------------------------------------------------- #
def test_a_second_mark_never_moves_the_first():
    clock = _TickClock()
    marks = TurnPhaseMarks(monotonic=clock, wall_now=lambda: "stamp")
    first = marks.mark("provider_first_byte")
    again = marks.mark("provider_first_byte")
    assert first == again == marks.snapshot()["provider_first_byte"]


def test_the_emitter_marks_the_first_byte_once_across_many_deltas(monkeypatch):
    """``delta()`` runs per token; the phase must cost one mark for the turn."""

    from hermes_cli import harness

    marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    emitter = harness._ChatProtocolV2Emitter(
        turn_id="turn_x",
        client_message_id="client_x",
        emit_frames=False,
        turn_phases=marks,
    )
    for chunk in ("a", "b", "c", "d"):
        emitter.delta(chunk)
    first_byte = marks.snapshot()["provider_first_byte"]
    emitter.delta("e")
    assert marks.snapshot()["provider_first_byte"] == first_byte


def test_an_empty_delta_is_not_a_first_byte():
    """The emitter returns early on a falsy delta; no byte means no mark."""

    from hermes_cli import harness

    marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    emitter = harness._ChatProtocolV2Emitter(
        turn_id="turn_y",
        client_message_id="client_y",
        emit_frames=False,
        turn_phases=marks,
    )
    emitter.delta("")
    emitter.delta(None)
    assert "provider_first_byte" not in marks.snapshot()


# --------------------------------------------------------------------------- #
# 3b. `request_assembled` — the honest split of the "provider" span            #
# --------------------------------------------------------------------------- #
# Measured live (turn c59ab99e, 2026-08-22): 1,762 ms of the 13,532 ms span
# the audit rendered as "provider first_byte" elapsed before the per-request
# client even existed — turn-context prologue, tool-schema serialization,
# middleware, hooks. `request_assembled` is the mark that separates that
# hermes work from the genuine client-init + network + provider wait. The
# named sabotage for this stage is "emit the marker under a misspelled step"
# — the mark must then vanish from the record, and these rows must red.
def test_request_assembled_lands_between_request_started_and_first_byte(streamed_turn):
    phases = streamed_turn[TURN_PHASES_KEY]
    assert "request_assembled" in phases, (
        "the dispatch-start trace payload must become the request_assembled "
        "mark; without it the whole run_conversation prologue is billed to "
        "the provider"
    )
    assert (
        phases["provider_request_started"]
        <= phases["request_assembled"]
        <= phases["provider_first_byte"]
    )


def test_the_trace_mapper_takes_only_the_step_it_names():
    """Unknown payloads take nothing — the mapper is an instrument, and the
    trace stream carries many other payload shapes (tool events, spans)."""

    marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    mark_from_trace_payload(marks, None)
    mark_from_trace_payload(marks, "not a dict")
    mark_from_trace_payload(marks, {"phase": "timing", "step": "conversation_request_build"})
    mark_from_trace_payload(
        marks, {"phase": "tool", "step": CONVERSATION_REQUEST_ASSEMBLED_STEP}
    )
    assert "request_assembled" not in marks.snapshot()
    mark_from_trace_payload(
        marks, {"phase": "timing", "step": CONVERSATION_REQUEST_ASSEMBLED_STEP}
    )
    assert "request_assembled" in marks.snapshot()


def test_the_loop_marker_and_the_mapper_agree_on_the_step():
    """The producer/consumer contract, held at the seam.

    ``_emit_request_assembled_marker`` (agent/conversation_loop.py) is what a
    live turn fires; feeding its ACTUAL payload through the mapper proves the
    two sides spell the step identically — the drift this row exists to catch,
    because a misspelling fails silently as an absent (never-wrong) mark.
    """

    from agent.conversation_loop import _emit_request_assembled_marker

    captured: list[dict] = []
    agent = SimpleNamespace(status_callback=captured.append)
    _emit_request_assembled_marker(agent, api_call_count=1)
    assert len(captured) == 1
    marks = TurnPhaseMarks(monotonic=_TickClock(), wall_now=lambda: "stamp")
    mark_from_trace_payload(marks, captured[0])
    assert "request_assembled" in marks.snapshot()
    # An instant, not a span: it must not carry a timing_key, or the profile
    # timing collector would record a meaningless 0ms duration for it.
    assert "timing_key" not in captured[0]
    assert "duration_ms" not in captured[0]


def test_the_retry_ladder_keeps_the_first_dispatch_instant():
    """Two dispatch attempts, one mark: first-mark-wins at the mapper too."""

    clock = _TickClock()
    marks = TurnPhaseMarks(monotonic=clock, wall_now=lambda: "stamp")
    payload = {"phase": "timing", "step": CONVERSATION_REQUEST_ASSEMBLED_STEP}
    mark_from_trace_payload(marks, payload)
    first = marks.snapshot()["request_assembled"]
    mark_from_trace_payload(marks, payload)
    assert marks.snapshot()["request_assembled"] == first


# --------------------------------------------------------------------------- #
# 3c. The runner's timing dict, on the record                                  #
# --------------------------------------------------------------------------- #
# The phase block spans profile bootstrap in ONE number (`write_ahead →
# agent_ready`, 3.0–3.6 s cold). The runner measures the pieces — runtime
# resolve, MCP admission, agent construct — and until now dropped every one of
# them when the stream ended, so "which part of the 3 s?" was a log-grep against
# a serve nobody could re-run. Carried on the record, each prep-cost remedy gets
# a before/after receipt.
_RUNNER_TIMING = {
    "runtime_resolve_ms": 310,
    "agent_construct_ms": 1_480,
    "mcp_admission_ms": 22,
    "conversation_call_ms": 4_012,
    "resident_actor_reused": 0,
    "resident_rebuild_runtime_signature_changed": 1,
    # Everything below is in the runner's dict too, and none of it may land:
    # a nested accounting block (carried under its own key), transport labels,
    # and a counter that is not a duration.
    "run_budget": {"bounded_by": "wall", "budgets": []},
    "mcp_admission_transport": ["stdio:C:/Users/beast/secret_server.exe"],
    "mcp_admitted_servers": 3,
}


@pytest.fixture
def timed_turn(monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks):  # noqa: F811
    _drive(
        monkeypatch,
        capsys,
        _streaming_provider(profile_timing=_RUNNER_TIMING),
        turn_id="phases_timing",
    )
    return _record_on_disk(isolate_agent_runtime_root, "phases_timing")


def test_the_record_carries_the_runners_own_timing_breakdown(timed_turn):
    block = timed_turn[TURN_PROFILE_TIMING_KEY]
    assert block["runtime_resolve_ms"] == 310
    assert block["agent_construct_ms"] == 1_480
    assert block["mcp_admission_ms"] == 22
    assert block["conversation_call_ms"] == 4_012
    assert block["resident_actor_reused"] == 0
    assert block["resident_rebuild_runtime_signature_changed"] == 1


def test_the_persisted_timing_block_admits_nothing_but_its_three_shapes(timed_turn):
    """Bounded by construction, not by scrubbing.

    The runner's dict is an open namespace shared by admission receipts and
    transport labels — real paths among them. Only ``*_ms`` durations,
    ``resident_actor_reused`` and ``resident_rebuild_*`` are admitted, so no
    free text can reach a durable record through this key.
    """

    block = timed_turn[TURN_PROFILE_TIMING_KEY]
    assert "run_budget" not in block, "the accounting block has its own key"
    assert "mcp_admission_transport" not in block
    assert "mcp_admitted_servers" not in block
    assert all(isinstance(value, int) for value in block.values())
    assert "secret_server" not in repr(block)


def test_the_accounting_block_still_rides_its_own_key(timed_turn):
    """The new key is additive: it must not displace what the record carried."""

    assert timed_turn["run_budget"]["bounded_by"] == "wall"
    assert TURN_PHASES_KEY in timed_turn


@pytest.fixture
def warm_timed_turn(monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks):  # noqa: F811
    _drive(
        monkeypatch,
        capsys,
        _streaming_provider(
            profile_timing={"resident_actor_reused": 1, "conversation_call_ms": 900}
        ),
        turn_id="phases_timing_warm",
    )
    return _record_on_disk(isolate_agent_runtime_root, "phases_timing_warm")


def test_a_reused_actor_constructed_NOTHING_and_the_record_says_so_by_absence(
    warm_timed_turn,
):
    """No construction happened, so there is no construction cost to report.

    A ``0`` here would read as "constructing the agent was free", which is the
    opposite of what a reused resident actor proves.
    """

    block = warm_timed_turn[TURN_PROFILE_TIMING_KEY]
    assert block["resident_actor_reused"] == 1
    assert "agent_construct_ms" not in block


def test_a_runner_that_reported_no_timing_leaves_only_what_the_HANDLER_measured(
    monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks  # noqa: F811
):
    """Absent stays absent — but "nobody" now has to mean nobody.

    This row used to assert the key was absent entirely when the runner reported
    nothing, and that was the right assertion while the runner was the block's
    only contributor. chat-turn-prep Stage 4 made the block the TURN's rather
    than the runner's: ``_cmd_mission_chat_message`` measures its own SessionDB
    open — a cost paid before any runner exists, inside the
    ``request_received → context_built`` span — and folds it in across the turn
    plan.

    So the invariant is unchanged and its application is sharpened: a block may
    only carry what was actually measured. With a blind runner that is EXACTLY
    ONE key, and specifically not a fabricated ``resident_actor_reused`` or a
    zeroed ``agent_construct_ms``. The "nothing measured at all" arm is still
    pinned, one layer down, by
    ``test_safe_turn_profile_timing_rejects_everything_it_cannot_read``'s empty
    dict returning ``None``.
    """

    _drive(
        monkeypatch,
        capsys,
        _streaming_provider(profile_timing={}),
        turn_id="phases_timing_blind",
    )
    record = _record_on_disk(isolate_agent_runtime_root, "phases_timing_blind")
    block = record[TURN_PROFILE_TIMING_KEY]
    assert set(block) == {"session_db_open_ms"}, (
        "a blind runner must contribute nothing; only the handler's own "
        "measurement may appear"
    )
    assert isinstance(block["session_db_open_ms"], int)
    assert block["session_db_open_ms"] >= 0


def test_the_handlers_session_db_open_reaches_the_durable_record(
    monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks  # noqa: F811
):
    """Stage 4's instrument, end to end on a real turn.

    H3 (§3 of the plan) confirmed in CODE that every turn constructs a fresh
    ``SessionDB`` — schema init, FTS probe, WAL checks — and confirmed just as
    plainly that nobody had ever measured it. The pooling remedy is gated on
    this number existing, so the number reaching a persisted record IS the
    deliverable; a key the store's sanitizer silently refused would gate the
    remedy on nothing.
    """

    _drive(
        monkeypatch,
        capsys,
        _streaming_provider(profile_timing={"resident_actor_reused": 1}),
        turn_id="phases_session_db_open",
    )
    record = _record_on_disk(isolate_agent_runtime_root, "phases_session_db_open")
    block = record[TURN_PROFILE_TIMING_KEY]
    assert "session_db_open_ms" in block
    assert isinstance(block["session_db_open_ms"], int)
    assert block["session_db_open_ms"] >= 0
    # It is ADDITIVE: the runner's own accounting is untouched beside it.
    assert block["resident_actor_reused"] == 1


def test_a_plan_that_measured_nothing_leaves_session_db_open_ms_ABSENT(
    monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks  # noqa: F811
):
    """Unmeasured is a third state, and a zero would erase it.

    ``session_db_open_ms = 0`` reads as "opening the chat database was free",
    which is the exact claim Stage 4 exists to test. A plan that carries no
    measurement must produce no key.
    """

    from agent_runtime import mission_chat_outcome

    real_plan = mission_chat_outcome.MissionChatTurnPlan

    def _unmeasured_plan(*args, **kwargs):
        kwargs["session_db_open_ms"] = None
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(
        mission_chat_outcome, "MissionChatTurnPlan", _unmeasured_plan
    )
    _drive(
        monkeypatch,
        capsys,
        _streaming_provider(profile_timing={"resident_actor_reused": 1}),
        turn_id="phases_session_db_unmeasured",
    )
    record = _record_on_disk(
        isolate_agent_runtime_root, "phases_session_db_unmeasured"
    )
    assert "session_db_open_ms" not in record[TURN_PROFILE_TIMING_KEY]


@pytest.mark.parametrize(
    "value",
    [
        {"agent_construct_ms": "1480"},
        {"agent_construct_ms": None},
        {"agent_construct_ms": -1},
        {"agent_construct_ms": 24 * 60 * 60 * 1000 + 1},
        {"profile_label": "openai-codex"},
        "not a dict",
        None,
    ],
)
def test_the_timing_sanitizer_drops_what_it_cannot_read(value):
    from agent_runtime.mission_chat_turns import safe_turn_profile_timing

    assert safe_turn_profile_timing(value) is None


# --------------------------------------------------------------------------- #
# 4. The store boundary                                                        #
# --------------------------------------------------------------------------- #
def _seed_session_file(tmp_path, monkeypatch, session_id: str, session: dict) -> None:
    """Write one session file through the store's OWN path scheme.

    Filenames are a sanitized prefix plus a sha256 suffix, so hand-naming the
    file would produce a fixture the reader never opens — a test that passes by
    finding nothing.
    """

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime import mission_chat_turns as store_module

    path = store_module._session_file_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session), encoding="utf-8")


def test_a_v2_record_with_no_phases_key_reads_back_unchanged(tmp_path, monkeypatch):
    """Records already on disk are v2. They are not migrated and not rewritten.

    "This turn predates phase spans" is a fact about it, and the reader must not
    paper over it with an empty or defaulted block.
    """

    from agent_runtime.mission_chat_turns import mission_chat_turn_records

    _seed_session_file(
        tmp_path,
        monkeypatch,
        "legacy_session",
        {
            "client_legacy": {
                "schema_version": 2,
                "turn_id": "turn_legacy",
                "state": "projected",
                "elements": [],
            }
        },
    )
    records = mission_chat_turn_records(session_id="legacy_session")
    assert [row["turn_id"] for row in records] == ["turn_legacy"], (
        "the fixture must actually be readable, or this row proves nothing"
    )
    assert TURN_PHASES_KEY not in records[0]


def test_a_v3_records_phase_block_survives_the_READ_boundary(tmp_path, monkeypatch):
    """The consumer reads through ``mission_chat_turn_records``, not the file."""

    from agent_runtime.mission_chat_turns import mission_chat_turn_records

    _seed_session_file(
        tmp_path,
        monkeypatch,
        "v3_session",
        {
            "client_v3": {
                "schema_version": 3,
                "turn_id": "turn_v3",
                "state": "projected",
                "elements": [],
                TURN_PHASES_KEY: {
                    "anchored_at": "2026-08-21T21:17:43.400000Z",
                    "request_received": 0,
                    "provider_first_byte": 5400,
                    "builds_overlapped": 0,
                },
            }
        },
    )
    phases = mission_chat_turn_records(session_id="v3_session")[0][TURN_PHASES_KEY]
    assert phases["provider_first_byte"] == 5400
    # A real zero survives; it is a measurement, and the sanitizer's job is to
    # drop the unreadable, never to prune the honest.
    assert phases["builds_overlapped"] == 0
    assert "stream_done" not in phases


@pytest.mark.parametrize(
    "block",
    [
        {"anchored_at": "s", "provider_first_byte": None},
        {"anchored_at": "s", "provider_first_byte": "12"},
        {"anchored_at": "s", "provider_first_byte": -1},
        {"anchored_at": "s", "provider_first_byte": True},
    ],
)
def test_the_sanitizer_DROPS_what_it_cannot_read_rather_than_defaulting(block):
    """Defensive in one direction only: drop, never supply.

    ``True`` is called out because ``bool`` is an ``int`` subclass — a truthy
    value that landed in an elapsed-ms slot is corruption, not a one-millisecond
    phase, and a naive ``isinstance(x, int)`` would admit it as ``1``.
    """

    cleaned = safe_turn_phases(block) or {}
    assert "provider_first_byte" not in cleaned


def test_the_sanitizer_never_invents_the_anchor():
    assert safe_turn_phases({}) is None
    assert "request_received" not in (safe_turn_phases({"anchored_at": "s"}) or {})


# --------------------------------------------------------------------------- #
# 9. The v2 tool frame carries the patch's diff artifact                       #
# --------------------------------------------------------------------------- #
# Lives here because this is the file that already constructs a real
# `_ChatProtocolV2Emitter`; the claim is about the LIVE-turn carrier, which is
# the same emitter these phase rows drive. A streaming patch tile and a
# reloaded one must agree, so the four patch fields have to ride the frame as
# well as the snapshot, and they must ride it ABSENT-when-absent — a null
# sentinel would make the launcher's "no affordance" branch unreachable.
def test_the_tool_finished_frame_carries_the_patch_artifact_and_counts(capsys):
    import json as _json

    from hermes_cli import harness

    emitter = harness._ChatProtocolV2Emitter(
        turn_id="turn_patch",
        client_message_id="client_patch",
    )
    emitter.progress(
        {
            "type": "run.tool.finished",
            "tool_name": "patch",
            "status": "passed",
            "duration_ms": 42,
            "changed_files": ["main.dart"],
            "patch_artifact": r"X:\Unreal Engine\store\patch_diffs\a.diff",
            "patch_adds": 12,
            "patch_dels": 3,
            "patch_mode": "replace",
        }
    )

    frames = [
        _json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    finished = next(f for f in frames if f["type"] == "tool.finished")
    # A path with a space in it survives whole.
    assert finished["patch_artifact"] == r"X:\Unreal Engine\store\patch_diffs\a.diff"
    assert finished["patch_adds"] == 12
    assert finished["patch_dels"] == 3
    assert finished["patch_mode"] == "replace"
    # ...and the element the turn store persists agrees with the frame.
    element = next(e for e in emitter.elements if e.get("kind") == "tool")
    assert element["patch_artifact"] == finished["patch_artifact"]
    assert element["patch_adds"] == 12


def test_a_non_patch_tool_frame_grows_no_patch_keys(capsys):
    from hermes_cli import harness

    emitter = harness._ChatProtocolV2Emitter(
        turn_id="turn_terminal",
        client_message_id="client_terminal",
    )
    emitter.progress(
        {
            "type": "run.tool.finished",
            "tool_name": "terminal",
            "status": "passed",
            "command_full": "pytest -q",
        }
    )

    import json as _json

    frames = [
        _json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    finished = next(f for f in frames if f["type"] == "tool.finished")
    for key in ("patch_artifact", "patch_adds", "patch_dels", "patch_mode"):
        assert key not in finished
