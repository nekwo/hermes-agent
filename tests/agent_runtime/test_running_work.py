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
from datetime import datetime, timedelta, timezone

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


@pytest.fixture
def real_home(tmp_path, monkeypatch):
    """A real env-driven home, exercising the ACTUAL resolver rather than a stub.

    The ``home`` fixture stubs ``_head_home`` so lane tests can be about lanes.
    Convergence tests must not: the whole class of bug they cover is the reader
    and the writer disagreeing about which directory they mean, and a stubbed
    reader cannot disagree with anything.
    """

    profile = tmp_path / "profiles" / "neko"
    head = tmp_path / "profiles" / "base"
    profile.mkdir(parents=True, exist_ok=True)
    head.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(head))
    return profile, head


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


# --- writer / reader convergence --------------------------------------------
#
# The projection reads two stores that OTHER processes write. If the two sides
# resolve different directories the failure is silent and total: every source
# reports `ok`, the row list is empty, and an operator watching a 20-minute
# build is told nothing is running. These tests pin the agreement itself.


def test_the_writers_and_the_reader_resolve_the_same_directory(real_home):
    """Under the launcher's own layout: HERMES_HOME=profile, HEAD_HOME=base."""

    from tools import process_registry
    from tools.async_delegation import _db_path

    profile, head = real_home
    paths = running_work_store_paths()

    assert process_registry.checkpoint_path() == paths[0]
    assert _db_path() == paths[1]
    # ...and that shared directory is the operator's head, not the profile the
    # ambient env var happens to point at.
    assert paths[0].parent == head
    assert paths[0].parent != profile


def test_a_checkpoint_written_by_the_writer_is_seen_by_the_reader(real_home):
    """End-to-end: the writer's own path, read back through the projection."""

    from tools import process_registry

    _write_checkpoint(
        process_registry.checkpoint_path().parent,
        [
            {
                "session_id": "sess-converged",
                "command": "npm run build",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_700_000_000.0,
            }
        ],
    )

    rows = _rows_of_kind(build_running_work(), KIND_TERMINAL)

    assert [row["work_id"] for row in rows] == ["terminal:sess-converged"]


def test_with_no_explicit_head_both_sides_fall_back_to_the_ambient_home(
    tmp_path, monkeypatch
):
    """Gateway / TUI / plain CLI set no head — behaviour must be unchanged."""

    from tools import process_registry
    from tools.async_delegation import _db_path

    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)

    assert process_registry.checkpoint_path() == ambient / "processes.json"
    assert _db_path() == ambient / "state.db"
    assert running_work_store_paths() == (
        ambient / "processes.json",
        ambient / "state.db",
    )
    assert _head_home_provenance() == "ambient_home"


def test_an_explicit_head_is_reported_as_such_in_the_source_detail(real_home):
    assert _head_home_provenance() == "head_home"


def _head_home_provenance() -> str:
    return running_work._head_home()[1]


def test_the_checkpoint_path_is_resolved_per_call_not_frozen_at_import(
    tmp_path, monkeypatch
):
    """An import-time bind depended on WHEN the module was first imported.

    ``persona_profile_context`` flips HERMES_HOME process-globally mid-turn, so
    a frozen path silently followed whichever profile happened to be active at
    first import.
    """

    from tools import process_registry

    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(first))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    assert process_registry.checkpoint_path().parent == first

    monkeypatch.setenv("HERMES_HOME", str(second))
    assert process_registry.checkpoint_path().parent == second


def test_a_recorded_head_survives_a_persona_profile_flip(tmp_path, monkeypatch):
    """The case the contextvar exists for: a turn running under a profile home.

    ``persona_profile_context`` records the operator home BEFORE diverting the
    ambient one, so background work spawned inside a persona turn still lands
    where the operator's projection reads it.
    """

    from hermes_constants import (
        record_hermes_head_home_if_unset,
        reset_hermes_head_home,
    )
    from tools import process_registry

    operator = tmp_path / "operator"
    persona = tmp_path / "persona"
    operator.mkdir()
    persona.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(operator))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)

    token = record_hermes_head_home_if_unset(operator)
    try:
        # The flip persona_profile_context performs.
        monkeypatch.setenv("HERMES_HOME", str(persona))
        assert process_registry.checkpoint_path().parent == operator
        assert running_work_store_paths()[0].parent == operator
    finally:
        reset_hermes_head_home(token)


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


def test_an_unreadable_start_time_is_unknown_not_recycled(home, monkeypatch):
    """Absence of proof is not proof of absence.

    ``get_process_start_time`` returns None on a permissions failure or an
    unsupported platform. Comparing that against a real baseline yields False,
    so an unreadable probe used to be indistinguishable from a recycled PID —
    and silently DELETED a running build from the HUD, filed under "recycled".
    """

    import gateway.status

    monkeypatch.setattr(gateway.status, "get_process_start_time", lambda _pid: None)
    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-unreadable",
                "command": "npm run build",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": 12345,
                "started_at": 1_700_000_000.0,
            }
        ],
    )
    accountant = ProjectionAccountant("running_work")

    rows = _rows_of_kind(build_running_work(accountant), KIND_TERMINAL)

    assert [row["work_id"] for row in rows] == ["terminal:sess-unreadable"]
    assert rows[0]["status"] == STATUS_UNKNOWN
    assert rows[0]["pid_verified"] is False
    assert accountant.summary()["reasons"] == {}


def test_a_raising_start_time_probe_is_also_unknown_not_recycled(home, monkeypatch):
    import gateway.status

    def _boom(_pid):
        raise PermissionError("access denied")

    monkeypatch.setattr(gateway.status, "get_process_start_time", _boom)
    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-denied",
                "command": "npm run build",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": 12345,
                "started_at": 1_700_000_000.0,
            }
        ],
    )

    rows = _rows_of_kind(build_running_work(), KIND_TERMINAL)

    assert [row["status"] for row in rows] == [STATUS_UNKNOWN]


@pytest.mark.parametrize(
    "observed, baseline, expected",
    [
        (999, 999, running_work.PID_VERIFIED),
        (1000, 999, running_work.PID_RECYCLED),
        (None, 999, running_work.PID_START_TIME_UNREADABLE),
        (999, None, running_work.PID_NO_BASELINE),
    ],
)
def test_pid_identity_reports_a_distinct_verdict_per_case(
    monkeypatch, observed, baseline, expected
):
    import gateway.status

    monkeypatch.setattr(gateway.status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(gateway.status, "get_process_start_time", lambda _pid: observed)

    alive, verified, verdict = running_work._pid_identity(4242, baseline)

    assert alive is True
    assert verdict == expected
    assert verified is (expected == running_work.PID_VERIFIED)


def test_a_dead_pid_reports_the_dead_verdict(monkeypatch):
    import gateway.status

    monkeypatch.setattr(gateway.status, "_pid_exists", lambda _pid: False)

    assert running_work._pid_identity(4242, 999) == (False, False, running_work.PID_DEAD)


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
        pass

    class _Owner:
        _parallel_pool = object()

    assert running_work._cron_owned_here(_Bare(), set()) is False
    assert running_work._cron_owned_here(_Owner(), set()) is True
    # A live running id proves ownership even before any pool is inspected.
    assert running_work._cron_owned_here(_Bare(), {"job-1"}) is True


def test_a_broken_scheduler_is_unreadable_not_someone_elses_process(home, monkeypatch):
    """"The scheduler broke" and "I am not the scheduler" are different facts.

    Only the first is actionable, and swallowing the exception into the
    ownership predicate reported it as the second.
    """

    class _Broken:
        def get_running_job_ids(self):
            raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(
        running_work,
        "_module",
        lambda name: _Broken() if name == "cron.scheduler" else None,
    )

    source = build_running_work()["sources"][KIND_CRON_JOB]

    assert source["status"] == SOURCE_UNAVAILABLE
    assert source["reason"] == "scheduler_unreadable"
    assert source["detail"] == "RuntimeError"


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


def test_every_started_at_on_the_wire_carries_a_utc_offset(home, monkeypatch):
    """The live registry emits naive LOCAL stamps; the wire must not.

    Two rows built on different lanes would otherwise carry stamps in different
    frames of reference with nothing marking which is which.
    """

    class _Registry:
        def list_sessions(self):
            return [
                {
                    "session_id": "sess-live",
                    "command": "npm run dev",
                    "pid": 4242,
                    # Exactly what `time.strftime(..., time.localtime(...))`
                    # produces: no offset, local clock.
                    "started_at": "2026-08-03T10:00:00",
                    "uptime_seconds": 30,
                    "status": "running",
                    "output_preview": "",
                }
            ]

    class _Module:
        process_registry = _Registry()

    real_module = running_work._module
    monkeypatch.setattr(
        running_work,
        "_module",
        lambda name: _Module() if name == "tools.process_registry" else real_module(name),
    )

    row = _rows_of_kind(build_running_work(), KIND_TERMINAL)[0]

    parsed = datetime.fromisoformat(row["started_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # ...and it is the SAME instant the registry meant, not a relabelled one.
    assert parsed == datetime(2026, 8, 3, 10, 0, 0).astimezone(timezone.utc)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("not a stamp", ""),
        # Already anchored: passed through, not re-interpreted.
        ("2026-08-03T10:00:00+00:00", "2026-08-03T10:00:00+00:00"),
        ("2026-08-03T10:00:00Z", "2026-08-03T10:00:00+00:00"),
    ],
)
def test_anchored_and_unusable_stamps_are_left_alone(raw, expected):
    assert running_work._iso_from_naive_local(raw) == expected


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
        def get(self, session_id):
            return object() if session_id == "sess-kill" else None

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


def test_cancelling_work_owned_by_another_process_says_so(home, monkeypatch):
    """A durable-visible row this process cannot kill is `cancel_unavailable`.

    Falling through to `kill_process` on a registry that never spawned it
    returns `not_found`, which would tell an operator "no such work" about work
    their own HUD is listing as running.
    """

    _write_checkpoint(
        home,
        [
            {
                "session_id": "sess-elsewhere",
                "command": "long build",
                "pid": os.getpid(),
                "pid_scope": "host",
                "host_start_time": _self_start_time(),
                "started_at": 1_700_000_000.0,
            }
        ],
    )

    class _EmptyRegistry:
        def get(self, session_id):
            return None

        def kill_process(self, *_args, **_kwargs):
            raise AssertionError("must not reach the kill seam for foreign work")

    class _Module:
        process_registry = _EmptyRegistry()

    real_module = running_work._module
    monkeypatch.setattr(
        running_work,
        "_module",
        lambda name: _Module() if name == "tools.process_registry" else real_module(name),
    )

    result = cancel_work("terminal:sess-elsewhere")

    assert result["status"] == "error"
    assert result["code"] == "cancel_unavailable"
    assert result["detail"] == "not_in_process"
    assert result["owning_lane"] == "serve"


def test_cancelling_a_delegation_this_process_does_not_hold_says_so(home, monkeypatch):
    _seed_delegation_db(
        home,
        [
            (
                "del-elsewhere",
                "origin",
                "parent",
                "running",
                1_700_000_000.0,
                os.getpid(),
                _self_start_time(),
                "{}",
            )
        ],
    )

    class _Module:
        @staticmethod
        def list_async_delegations():
            return []

        @staticmethod
        def interrupt_for_session(**_kwargs):
            raise AssertionError("must not reach the interrupt seam")

    real_module = running_work._module
    monkeypatch.setattr(
        running_work,
        "_module",
        lambda name: _Module if name == "tools.async_delegation" else real_module(name),
    )

    result = cancel_work("delegation:del-elsewhere")

    assert result["code"] == "cancel_unavailable"
    assert result["detail"] == "not_in_process"


# --------------------------------------------------------------------------
# lane: detached dispatches (WP-H2)
# --------------------------------------------------------------------------


@pytest.fixture
def dispatch_home(tmp_path, monkeypatch):
    """A real background-work home the dispatch store and the lane BOTH resolve.

    Not a stub: the class of bug this lane can have is the projection reading a
    different directory than the store wrote, and a stubbed resolver cannot
    disagree with anything.
    """

    home = tmp_path / "dispatch-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(home))
    return home


def _record_dispatch(**overrides):
    from agent_runtime.dispatch_store import mint_dispatch_id, record_dispatch

    payload = {
        "dispatch_id": mint_dispatch_id(),
        "sender_session_id": "persona_chat_personainst_neko_aaaaaaaaaaaa",
        "target_persona": "dev",
        "title": "Run the suite",
        "ask": "Run the full launcher suite and report failures.",
    }
    payload.update(overrides)
    record_dispatch(**payload)
    return payload["dispatch_id"]


def test_the_dispatch_lane_is_declared_on_every_frame(dispatch_home):
    """A lane with a producer MUST report health, even with nothing running.

    "Nothing is running" and "I could not look" are different facts; the sources
    block is the only place a consumer can tell them apart.
    """

    frame = build_running_work()

    assert "dispatch" in RUNNING_WORK_SOURCES
    assert frame["sources"]["dispatch"]["status"] == SOURCE_OK
    assert [row for row in frame["rows"] if row["kind"] == "dispatch"] == []


def test_an_in_flight_dispatch_surfaces_as_a_row(dispatch_home):
    dispatch_id = _record_dispatch()

    rows = [row for row in build_running_work()["rows"] if row["kind"] == "dispatch"]

    assert len(rows) == 1
    row = rows[0]
    assert row["work_id"] == f"dispatch:{dispatch_id}"
    assert row["status"] == STATUS_RUNNING
    assert row["pid_verified"] is True
    assert row["owner"]["persona_id"] == "dev"
    # The SENDER's root: the thread the answer is owed to.
    assert row["owner"]["session_id"] == "persona_chat_personainst_neko_aaaaaaaaaaaa"
    # No interrupt seam in v1 — a cancel button that cannot cancel is worse
    # than no button.
    assert row["cancellable"] is False


def test_a_settled_dispatch_stops_being_running_work(dispatch_home):
    from agent_runtime.dispatch_store import STATE_COMPLETED, record_completion

    dispatch_id = _record_dispatch()
    record_completion(dispatch_id, state=STATE_COMPLETED, reply="done")

    assert [row for row in build_running_work()["rows"] if row["kind"] == "dispatch"] == []


def test_a_dispatch_whose_owner_died_is_SHOWN_as_unknown_not_hidden(
    dispatch_home, monkeypatch
):
    """The one lane that reports a dead owner instead of dropping it.

    A dead PID on a terminal or delegation row means the work is over and nobody
    is owed anything. A dead PID on a DISPATCH row means the child process died
    while a sender is still waiting for its answer — so hiding it would make the
    single dispatch most worth looking at the only invisible one, and to an
    operator the disappearance would read as "it finished". It surfaces as
    unknown/unverified until the periodic sweep settles it into a deliverable
    ``unknown`` completion, at which point it leaves the projection because it
    genuinely is not running work any more.
    """

    _record_dispatch()
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    accountant = ProjectionAccountant("running_work")

    rows = [row for row in build_running_work(accountant)["rows"] if row["kind"] == "dispatch"]

    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_UNKNOWN
    assert rows[0]["pid_verified"] is False
    assert accountant.summary()["reasons"] == {}


def test_the_dispatch_lane_reports_unavailable_when_the_store_cannot_be_read(
    dispatch_home, monkeypatch
):
    monkeypatch.setattr(
        "agent_runtime.dispatch_store.running_dispatches",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("locked")),
    )

    entry = build_running_work()["sources"]["dispatch"]

    assert entry["status"] == SOURCE_UNAVAILABLE
    assert entry["reason"] == "store_unreadable"


def test_cancelling_a_dispatch_is_a_typed_refusal_not_a_pretend_success(dispatch_home):
    dispatch_id = _record_dispatch()

    result = cancel_work(f"dispatch:{dispatch_id}")

    assert result["code"] == "cancel_unsupported"
