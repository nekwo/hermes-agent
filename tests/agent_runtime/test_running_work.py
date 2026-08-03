"""The ``running_work`` projection's honesty contract.

Every test here pins a rule that, if relaxed, turns the Activity HUD into a
confident liar: a source that failed silently reads as "nothing running", a
recycled PID reads as a live process, a wedged delegation reads as healthy, and
an unbounded preview floods the frame. The projection's whole value is that an
operator can trust what it says, so these are contract tests, not smoke tests.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from agent_runtime import running_work
from agent_runtime.parity import ProjectionAccountant
from agent_runtime.running_work import (
    KIND_CHAT_TURN,
    KIND_CRON_JOB,
    KIND_DELEGATION,
    KIND_MCP_SERVER,
    KIND_TERMINAL,
    RUNNING_WORK_SOURCES,
    SOURCE_OK,
    SOURCE_UNAVAILABLE,
    STATUS_RUNNING,
    STATUS_STALLED,
    STATUS_UNKNOWN,
    TAIL_PREVIEW_LIMIT,
    build_running_work,
    cancel_work,
    peek_work,
    running_work_store_paths,
    split_work_id,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HERMES home that the head-home resolver will land on."""

    head = tmp_path / "home"
    head.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(running_work, "_head_home", lambda: (head, "test_home"))
    return head


def _self_start_time() -> int | None:
    from gateway.status import get_process_start_time

    return get_process_start_time(os.getpid())


def _write_checkpoint(home, entries) -> None:
    (home / "processes.json").write_text(json.dumps(entries), encoding="utf-8")


def _seed_delegation_db(home, rows) -> None:
    """A minimal ``async_delegations`` store, written the way the real one is.

    Deliberately hand-built rather than driven through ``dispatch_async_delegation``:
    that helper spawns a worker thread and an executor, which a projection test
    has no business starting. Only the columns the projection reads are seeded.
    """

    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        """CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT
        )"""
    )
    conn.executemany(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, parent_session_id, state,
            dispatched_at, owner_pid, owner_started_at, task_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    conn.close()


def _rows_of_kind(payload, kind):
    return [row for row in payload["rows"] if row["kind"] == kind]


# --- per-source fail-closed -------------------------------------------------


def test_every_declared_source_reports_health_on_every_build(home):
    payload = build_running_work()
    assert set(payload["sources"]) == set(RUNNING_WORK_SOURCES)
    for name, entry in payload["sources"].items():
        assert entry["status"] in {SOURCE_OK, SOURCE_UNAVAILABLE}, name


def test_an_unreadable_checkpoint_is_reported_unavailable_never_silently_empty(home):
    (home / "processes.json").write_text("{not json", encoding="utf-8")

    payload = build_running_work()

    terminal = payload["sources"][KIND_TERMINAL]
    assert terminal["status"] == SOURCE_UNAVAILABLE
    assert terminal["reason"] == "checkpoint_unreadable"
    assert _rows_of_kind(payload, KIND_TERMINAL) == []
    assert payload["counts"]["unavailable_sources"] >= 1


def test_a_checkpoint_that_is_not_a_list_is_reported_rather_than_coerced(home):
    (home / "processes.json").write_text('{"session_id": "s1"}', encoding="utf-8")

    terminal = build_running_work()["sources"][KIND_TERMINAL]

    assert terminal["status"] == SOURCE_UNAVAILABLE
    assert terminal["reason"] == "checkpoint_malformed"


def test_an_unresolvable_home_is_reported_not_treated_as_no_work(monkeypatch):
    monkeypatch.setattr(running_work, "_head_home", lambda: (None, "unresolved"))

    payload = build_running_work()

    for name in (KIND_TERMINAL, KIND_DELEGATION):
        assert payload["sources"][name]["status"] == SOURCE_UNAVAILABLE
        assert payload["sources"][name]["reason"] == "home_unresolved"


def test_an_absent_checkpoint_is_ok_with_a_reason_not_unavailable(home):
    """A home with no checkpoint PROVES zero processes; that is readable, not blind."""

    terminal = build_running_work()["sources"][KIND_TERMINAL]

    assert terminal["status"] == SOURCE_OK
    assert "no checkpoint file" in terminal["detail"]


def test_a_lane_that_explodes_cannot_break_the_frame(home, monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(running_work, "_collect_chat_turns", _boom)
    monkeypatch.setattr(
        running_work,
        "_COLLECTORS",
        tuple(
            (name, _boom if name == KIND_CHAT_TURN else fn, takes_now)
            for name, fn, takes_now in running_work._COLLECTORS
        ),
    )

    payload = build_running_work()

    assert payload["sources"][KIND_CHAT_TURN]["status"] == SOURCE_UNAVAILABLE
    assert payload["sources"][KIND_CHAT_TURN]["reason"] == "collector_failed"


# --- PID identity -----------------------------------------------------------


def test_a_verified_pid_reports_running(home):
    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-live",
                "command": "npm run dev",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_800_000_000.0,
            }
        ],
    )

    rows = _rows_of_kind(build_running_work(), KIND_TERMINAL)

    assert [row["work_id"] for row in rows] == ["terminal:sess-live"]
    assert rows[0]["status"] == STATUS_RUNNING
    assert rows[0]["pid_verified"] is True
    assert rows[0]["cancellable"] is True


def test_an_unprovable_identity_reports_unknown_never_running(home):
    """No baseline captured — alive, but we cannot prove it is OUR process."""

    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-legacy",
                "command": "sleep 999",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": None,
                "started_at": 1_800_000_000.0,
            }
        ],
    )

    rows = _rows_of_kind(build_running_work(), KIND_TERMINAL)

    assert rows[0]["status"] == STATUS_UNKNOWN
    assert rows[0]["pid_verified"] is False


def test_a_recycled_pid_is_refused_and_accounted_never_reported_running(home):
    """Alive, but a DIFFERENT process holds the number: our work is gone."""

    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-recycled",
                "command": "old build",
                "pid": os.getpid(),
                "pid_scope": "host",
                # A start time that cannot be ours.
                "host_start_time": 1,
                "started_at": 1_800_000_000.0,
            }
        ],
    )
    accountant = ProjectionAccountant("running_work")

    rows = _rows_of_kind(build_running_work(accountant), KIND_TERMINAL)

    assert rows == []
    summary = accountant.summary()
    assert summary["reasons"]["pid_recycled"] == 1
    assert "pid_recycled" in summary["by_design"]
    assert summary["considered"] == 1


def test_a_dead_pid_is_dropped_through_the_accountant_not_in_silence(home):
    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-dead",
                "command": "finished build",
                # A PID that cannot exist.
                "pid": 2 ** 31 - 1,
                "pid_scope": "host",
                "host_start_time": 99,
                "started_at": 1_800_000_000.0,
            }
        ],
    )
    accountant = ProjectionAccountant("running_work")

    rows = _rows_of_kind(build_running_work(accountant), KIND_TERMINAL)

    assert rows == []
    assert accountant.summary()["reasons"]["process_exited"] == 1


def test_a_delegation_whose_owner_is_gone_is_not_reported_running(home):
    _seed_delegation_db(
        home,
        [("del-1", "sess", "sess", "running", 1_800_000_000.0, 2 ** 31 - 1, 99, "{}")],
    )
    accountant = ProjectionAccountant("running_work")

    payload = build_running_work(accountant)

    assert _rows_of_kind(payload, KIND_DELEGATION) == []
    assert payload["sources"][KIND_DELEGATION]["status"] == SOURCE_OK
    assert accountant.summary()["reasons"]["owner_exited"] == 1


# --- delegation lane --------------------------------------------------------


def test_durable_delegation_rows_declare_progress_unavailable(home):
    """No progress token survives a process boundary — say so, never emit zeros.

    A ``seconds_since_progress: 0`` here would read as "made progress just now",
    which is the opposite of what the durable lane actually knows.
    """

    _seed_delegation_db(
        home,
        [
            (
                "del-live",
                "origin-sess",
                "parent-sess",
                "running",
                1_800_000_000.0,
                os.getpid(),
                _self_start_time(),
                json.dumps({"goal": "refactor the widget"}),
            )
        ],
    )

    rows = _rows_of_kind(build_running_work(), KIND_DELEGATION)

    assert [row["work_id"] for row in rows] == ["delegation:del-live"]
    assert rows[0]["label"] == "refactor the widget"
    assert rows[0]["owner"]["session_id"] == "parent-sess"
    assert rows[0]["progress"] == {
        "api_calls": None,
        "in_tool": None,
        "seconds_since_progress": None,
        "source": SOURCE_UNAVAILABLE,
    }


def test_a_state_db_without_the_table_is_a_store_with_no_delegations(home):
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()

    source = build_running_work()["sources"][KIND_DELEGATION]

    assert source["status"] == SOURCE_OK
    assert "no async_delegations table" in source["detail"]


def test_the_projection_never_creates_the_state_db_it_reads(home):
    build_running_work()

    assert not (home / "state.db").exists()


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"status": "running", "seconds_since_progress": 1.0}, STATUS_RUNNING),
        # Frozen past the idle threshold: the 30s monitor sweep may not have
        # tripped yet, but the projection must not report this as healthy.
        ({"status": "running", "seconds_since_progress": 900.0}, STATUS_STALLED),
        # In-tool gets the far longer budget — a slow build is not a stall.
        (
            {"status": "running", "seconds_since_progress": 900.0, "in_tool": True},
            STATUS_RUNNING,
        ),
        (
            {"status": "running", "seconds_since_progress": 5000.0, "in_tool": True},
            STATUS_STALLED,
        ),
        ({"status": "stalling"}, "stalling"),
        ({"status": "stalled"}, STATUS_STALLED),
        ({"status": "finalizing"}, "finalizing"),
        ({"status": "error"}, "error"),
        ({"status": "who-knows"}, STATUS_UNKNOWN),
    ],
)
def test_staleness_maps_a_frozen_progress_token_to_stalled(record, expected):
    idle, in_tool = running_work._stale_thresholds()
    assert (idle, in_tool) == (450.0, 1200.0)

    assert (
        running_work._delegation_status(record, idle_stale=idle, in_tool_stale=in_tool)
        == expected
    )


# --- chat-turn lane ---------------------------------------------------------


def test_inflight_chat_turns_are_listed_with_their_owner(home, isolate_agent_runtime_root):
    from agent_runtime.mission_chat_turns import persist_mission_chat_turn

    persist_mission_chat_turn(
        session_id="chat-sess-1",
        client_message_id="cmid-1",
        turn_id="turn-1",
        elements=None,
        state="executing",
        write_ahead=True,
        metadata={
            "persona_instance_id": "personainst_neko",
            "root_chat_session_id": "chat-sess-1",
        },
    )

    rows = _rows_of_kind(build_running_work(), KIND_CHAT_TURN)

    assert [row["work_id"] for row in rows] == ["chat_turn:turn-1"]
    assert rows[0]["status"] == STATUS_RUNNING
    assert rows[0]["owner"]["persona_instance_id"] == "personainst_neko"
    assert rows[0]["owner"]["session_id"] == "chat-sess-1"
    assert rows[0]["started_at"]
    # Chat turns are settled by the chat lane's own authority; this verb must
    # not advertise a kill it cannot honestly perform.
    assert rows[0]["cancellable"] is False


def test_a_settled_chat_turn_is_not_running_work(home, isolate_agent_runtime_root):
    from agent_runtime.mission_chat_turns import persist_mission_chat_turn

    persist_mission_chat_turn(
        session_id="chat-sess-2",
        client_message_id="cmid-2",
        turn_id="turn-2",
        elements=None,
        state="pending",
        write_ahead=True,
    )
    persist_mission_chat_turn(
        session_id="chat-sess-2",
        client_message_id="cmid-2",
        turn_id="turn-2",
        elements=None,
        state="abandoned",
    )

    assert _rows_of_kind(build_running_work(), KIND_CHAT_TURN) == []


def test_an_outcome_unknown_turn_reports_unknown_not_running(
    home, isolate_agent_runtime_root
):
    from agent_runtime.mission_chat_turns import persist_mission_chat_turn

    persist_mission_chat_turn(
        session_id="chat-sess-3",
        client_message_id="cmid-3",
        turn_id="turn-3",
        elements=None,
        state="pending",
        write_ahead=True,
    )
    persist_mission_chat_turn(
        session_id="chat-sess-3",
        client_message_id="cmid-3",
        turn_id="turn-3",
        elements=None,
        state="executing",
    )
    persist_mission_chat_turn(
        session_id="chat-sess-3",
        client_message_id="cmid-3",
        turn_id="turn-3",
        elements=None,
        state="outcome_unknown",
    )

    rows = _rows_of_kind(build_running_work(), KIND_CHAT_TURN)

    assert [row["status"] for row in rows] == [STATUS_UNKNOWN]


def test_chat_turn_rows_never_carry_message_content(home, isolate_agent_runtime_root):
    """The HUD needs identity + timing; chat text has no reader on this wire."""

    from agent_runtime.mission_chat_turns import persist_mission_chat_turn

    persist_mission_chat_turn(
        session_id="chat-sess-4",
        client_message_id="cmid-4",
        turn_id="turn-4",
        elements=[{"kind": "text", "text": "a secret plan"}],
        state="executing",
        write_ahead=True,
    )

    rows = _rows_of_kind(build_running_work(), KIND_CHAT_TURN)

    assert rows
    assert "a secret plan" not in json.dumps(rows)


# --- live-only lanes --------------------------------------------------------


def test_live_only_lanes_report_unavailable_rather_than_no_work(home):
    """Absence proves nothing: an empty registry in a non-owning process is blind.

    Reporting ``ok`` with zero rows here would tell an operator "no MCP servers
    and no cron jobs are running" on the sole evidence that THIS process is not
    the one that would know.
    """

    payload = build_running_work()

    for name in (KIND_MCP_SERVER, KIND_CRON_JOB):
        entry = payload["sources"][name]
        assert entry["status"] == SOURCE_UNAVAILABLE
        assert entry["reason"] == "not_in_process"


def test_cron_ownership_requires_more_than_module_residency():
    """``mission_chat_turns`` imports the scheduler transitively — residency lies."""

    class _Bare:
        def get_running_job_ids(self):
            return frozenset()

    class _Owner:
        _parallel_pool = object()

        def get_running_job_ids(self):
            return frozenset()

    class _Busy:
        def get_running_job_ids(self):
            return frozenset({"job-1"})

    assert running_work._cron_owned_here(_Bare()) is False
    assert running_work._cron_owned_here(_Owner()) is True
    assert running_work._cron_owned_here(_Busy()) is True


def test_mcp_ownership_requires_connection_state():
    class _Bare:
        _servers = {}
        _server_connecting = set()
        _server_connect_errors = {}

    class _Owner(_Bare):
        _servers = {"docs": object()}

    assert running_work._mcp_owned_here(_Bare()) is False
    assert running_work._mcp_owned_here(_Owner()) is True


# --- bounded previews -------------------------------------------------------


def test_the_tail_preview_is_bounded_and_the_bound_is_declared_by_design():
    accountant = ProjectionAccountant("running_work")

    preview = running_work._preview("x" * 5000, accountant)

    assert len(preview) <= TAIL_PREVIEW_LIMIT
    summary = accountant.summary()
    assert summary["reasons"]["tail_truncated"] == 1
    # An undeclared drop trips the amber "lost data" pill; a deliberate bound
    # must not.
    assert "tail_truncated" in summary["by_design"]
    assert accountant.dropped_by_design == accountant.dropped


def test_a_short_preview_records_no_drop():
    accountant = ProjectionAccountant("running_work")

    assert running_work._preview("all done", accountant) == "all done"
    assert accountant.summary()["reasons"] == {}


def test_previews_mask_secret_assignments():
    assert "hunter2" not in running_work._preview("export API_KEY=hunter2", None)


def test_a_lane_that_floods_is_capped_and_the_cap_is_accounted(home):
    _write_checkpoint(
        home,
        [
            {
                "session_id": f"sess-{index}",
                "command": "sleep 1",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_800_000_000.0 + index,
            }
            for index in range(running_work._MAX_ROWS_PER_SOURCE + 25)
        ],
    )
    accountant = ProjectionAccountant("running_work")

    rows = _rows_of_kind(build_running_work(accountant), KIND_TERMINAL)

    assert len(rows) == running_work._MAX_ROWS_PER_SOURCE
    summary = accountant.summary()
    assert summary["reasons"]["lane_capped"] == 25
    assert summary["truncated"] is True


# --- row shape / ids --------------------------------------------------------


def test_every_row_carries_the_full_wire_shape(home):
    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-shape",
                "command": "npm test",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_800_000_000.0,
                "session_key": "gateway-sess",
            }
        ],
    )

    row = _rows_of_kind(build_running_work(), KIND_TERMINAL)[0]

    assert set(row) == {
        "work_id",
        "kind",
        "label",
        "command",
        "pid",
        "pid_verified",
        "owner",
        "status",
        "started_at",
        "elapsed_seconds",
        "progress",
        "tail_preview",
        "source_lane",
        "cancellable",
    }
    assert set(row["owner"]) == {"persona_id", "persona_instance_id", "session_id"}
    assert row["started_at"].startswith("2027-")
    assert row["elapsed_seconds"] >= 0


@pytest.mark.parametrize(
    "work_id, expected",
    [
        ("terminal:sess-1", ("terminal", "sess-1")),
        # Ids may contain colons; only the FIRST one delimits the kind.
        ("chat_turn:root:abc:1", ("chat_turn", "root:abc:1")),
        ("nonsense:x", ("", "")),
        ("terminal:", ("", "")),
        ("terminal", ("", "")),
        ("", ("", "")),
    ],
)
def test_work_ids_split_on_the_first_colon_only(work_id, expected):
    assert split_work_id(work_id) == expected


def test_store_paths_are_the_single_authority_the_serve_cache_fingerprints(home):
    assert running_work_store_paths() == (
        home / "processes.json",
        home / "state.db",
    )


def test_a_process_starting_invalidates_the_serve_read_model_cache(
    home, isolate_agent_runtime_root
):
    """Neither store emits an EventLog event, so only a stat can catch them.

    Without this the 20s read-model cache would keep replaying a HUD that says
    "nothing running" for twenty seconds after an agent kicked off a long build
    — and "3 running" for twenty seconds after they all exited.
    """

    from hermes_cli.harness_parts import serve

    before = serve._runtime_state_fingerprint()
    _write_checkpoint(home, [{"session_id": "sess-new", "pid": os.getpid()}])
    after = serve._runtime_state_fingerprint()

    assert before is not None
    assert after != before


# --- peek / cancel ----------------------------------------------------------


def test_peek_refuses_a_malformed_id_rather_than_guessing():
    payload = peek_work("not-a-work-id")

    assert payload["found"] is False
    assert payload["error"] == "malformed_work_id"


def test_peek_reports_a_missing_row_as_not_found(home):
    assert peek_work("terminal:nope")["found"] is False


def test_peek_never_marks_output_consumed(home):
    """``read_log`` semantics here would suppress notify_on_complete turns."""

    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-peek",
                "command": "long build",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_800_000_000.0,
            }
        ],
    )

    payload = peek_work("terminal:sess-peek")

    assert payload["found"] is True
    assert payload["consumed"] is False
    assert payload["tail_limit"] == running_work.PEEK_TAIL_LIMIT
    # This process does not own the registry, so there is no buffer to read —
    # and the honest answer is "unavailable", not an empty tail that would read
    # as "produced no output".
    assert payload["tail_available"] is False
    assert payload["tail_reason"] in {"not_in_process", "session_not_in_registry"}


def test_cancel_refuses_a_kind_with_no_interrupt_seam(home, isolate_agent_runtime_root):
    from agent_runtime.mission_chat_turns import persist_mission_chat_turn

    persist_mission_chat_turn(
        session_id="chat-sess-cancel",
        client_message_id="cmid-c",
        turn_id="turn-c",
        elements=None,
        state="executing",
        write_ahead=True,
    )

    result = cancel_work("chat_turn:turn-c")

    assert result["status"] == "error"
    assert result["code"] == "cancel_unsupported"


def test_cancel_reports_not_found_for_unknown_work(home):
    assert cancel_work("terminal:ghost")["code"] == "not_found"


def test_cancel_refuses_a_malformed_id(home):
    assert cancel_work("garbage")["code"] == "invalid_request"


def test_cancel_never_reaches_for_a_bare_pid(home, monkeypatch):
    """The kill must route through the identity-guarded registry seam.

    A projection that shelled out to ``kill <pid>`` would bypass the
    ``host_start_time`` guard that exists precisely because a recycled number
    once got a desktop browser SIGTERMed.
    """

    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-kill",
                "command": "long build",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_800_000_000.0,
            }
        ],
    )
    monkeypatch.setattr(running_work, "_module", lambda name: None)

    result = cancel_work("terminal:sess-kill")

    assert result["status"] == "error"
    assert result["code"] == "cancel_unavailable"
    assert result["detail"] == "not_in_process"


def test_cancel_routes_terminal_work_through_the_registry_kill_seam(home, monkeypatch):
    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-kill",
                "command": "long build",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_800_000_000.0,
            }
        ],
    )
    seen = {}

    class _Registry:
        def kill_process(self, session_id, *, source, consume_output):
            seen.update(
                session_id=session_id, source=source, consume_output=consume_output
            )
            return {"status": "killed"}

    class _Module:
        process_registry = _Registry()

    real_module = running_work._module
    monkeypatch.setattr(
        running_work,
        "_module",
        lambda name: _Module() if name == "tools.process_registry" else real_module(name),
    )

    result = cancel_work("terminal:sess-kill")

    assert result["status"] == "cancelled"
    assert seen["session_id"] == "sess-kill"
    # A cancel is not the agent reading its output: consuming here would
    # suppress the completion notification the agent is waiting on.
    assert seen["consume_output"] is False
