"""Stage 12 read-path freshness hardening tests.

Mission Control's read path is watermark-gated on the EventLog offset, so any
store mutation that changes client-visible snapshot state without appending an
event is invisible to every stream consumer (docs/agent-runtime-harness/
12-read-path-freshness-hardening.md). These tests pin the Stage 12 fixes:
event-coupled mutations at the store chokepoint, the blueprint-save event, the
stream fingerprint backstop, and contract-validated appends.
"""

import importlib.util
from types import SimpleNamespace

from agent_runtime.events import EventLog
from tests.agent_runtime.stream_liveness_helpers import drain_boot_liveness


def test_blueprint_save_command_is_removed_with_stage_graph():
    from hermes_cli import harness

    assert not hasattr(harness, "_cmd_blueprint_save")


def test_blueprint_catalog_store_is_removed_with_stage_graph():
    assert importlib.util.find_spec("agent_runtime.blueprints.store") is None


# ── Slice B1: emission lives at the STORE chokepoint, not the CLI verb ──────


def _offset() -> int:
    import agent_runtime.paths as paths

    path = paths.events_path()
    return path.stat().st_size if path.exists() else 0


def test_workspace_set_active_emits_at_store_level_and_advances_offset():
    from agent_runtime.store import WorkspaceStore

    workspace = WorkspaceStore().create(name="Chokepoint WS")
    before = _offset()
    WorkspaceStore().set_active(workspace.id)  # programmatic caller, NO CLI verb
    assert _offset() > before

    event = EventLog().tail(1)[0]
    assert event.type == "workspace.activated"
    assert event.payload["workspace_id"] == workspace.id
    assert event.payload["name"] == "Chokepoint WS"


def test_realm_set_active_clear_emits_cleared_payload():
    from agent_runtime.store import RealmStore

    RealmStore().set_active(None)
    event = EventLog().tail(1)[0]
    assert event.type == "realm.activated"
    assert event.payload["cleared"] is True
    assert "realm_id" not in event.payload


def test_workspace_rename_emits_specific_event_exactly_once():
    from agent_runtime.store import WorkspaceStore

    workspace = WorkspaceStore().create(name="Before")
    WorkspaceStore().rename(workspace.id, "After")
    renames = [e for e in EventLog().tail(10) if e.type == "workspace.updated"]
    assert len(renames) == 1  # named mutator emits once; save(emit_event=False) inside
    assert renames[0].payload["change"] == "renamed"
    assert renames[0].payload["name"] == "After"


def test_agent_store_save_emits_persona_updated():
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    AgentStore().save(
        AgentPersona(
            id="stage12_persona",
            display_name="Stage 12",
            role="dev",
            model="m",
            provider="p",
            api_mode="codex_responses",
            toolsets=[],
            system_prompt_path="dev.md",
        )
    )
    event = EventLog().tail(1)[0]
    assert event.type == "persona.updated"
    assert event.payload["persona_id"] == "stage12_persona"


# ── Slice C: stream backstop — bounded staleness for ANY write ──────────────


def test_stream_scope_switch_yields_delta_with_fresh_active_pointer():
    """A scope switch mid-stream must reach the client as a delta whose full
    core already carries the new active pointer — no forced poll."""

    from agent_runtime.store import RealmStore
    from agent_runtime.stream import stream_frames

    realm_a = RealmStore().create(name="Realm A")
    realm_b = RealmStore().create(name="Realm B")
    RealmStore().set_active(realm_a.id)

    frames = stream_frames(poll_interval_seconds=0.01, heartbeat_interval_seconds=30.0)
    hydrate = next(frames)
    assert hydrate["type"] == "hydrate"
    assert hydrate["core"]["active_realm_id"] == realm_a.id

    RealmStore().set_active(realm_b.id)
    delta = next(frames)
    frames.close()
    assert delta["type"] == "delta"
    assert delta["entity"]["event"]["type"] == "realm.activated"
    assert delta["core"]["active_realm_id"] == realm_b.id


def test_stream_backstop_reconciles_eventless_write():
    """A write that bypasses the store entirely (rule violation) must still
    converge within the Stage 12 SLO via a synthetic state.reconciled delta."""

    import json

    import agent_runtime.paths as paths
    from agent_runtime.store import RealmStore
    from agent_runtime.stream import stream_frames

    realm = RealmStore().create(name="Ghost Realm")

    frames = stream_frames(poll_interval_seconds=0.01, heartbeat_interval_seconds=0.05)
    # Past the boot build's own liveness (MC-4 / P6): at a sub-second cadence
    # the boot build heartbeats before its hydrate. Identified by the activity
    # block, never by position.
    assert drain_boot_liveness(frames)["type"] == "hydrate"

    # Simulate the violation: flip the active pointer with a RAW file write —
    # no store, no event, offset unchanged.
    pointer = paths.active_realm_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"realm_id": realm.id}), encoding="utf-8")

    reconcile = None
    for _ in range(50):  # bounded: well past 2× the 0.05s heartbeat interval
        frame = next(frames)
        if frame["type"] == "delta":
            reconcile = frame
            break
    frames.close()

    assert reconcile is not None, "event-less write never reconciled — backstop failed"
    assert reconcile["entity"]["event"]["type"] == "state.reconciled"
    assert reconcile["entity"]["event"]["payload"]["source"] == "stream_watchdog"
    assert reconcile["core"]["active_realm_id"] == realm.id


def test_scope_fingerprint_covers_head_home_session_db():
    """Persona-chat truth (Chat History) is derived from the head-home
    SessionDB, whose writers emit no EventLog events. Without the DB in the
    fingerprint, the S6 patch lane keeps a stream's hydrate-time chat list
    for the stream's whole lifetime (live incident 2026-07-25: the Launcher's
    Chat History froze for ~36h until a restart re-hydrated)."""

    import sqlite3
    import time as _time

    from hermes_constants import get_hermes_head_home

    from agent_runtime.stream import _scope_fingerprint

    db_path = get_hermes_head_home() / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS probe (payload TEXT)")

    before = _scope_fingerprint()

    _time.sleep(0.02)  # NTFS mtime granularity guard
    with sqlite3.connect(db_path) as con:
        # A page-sized payload so size moves even if the mtime tick doesn't.
        con.execute("INSERT INTO probe (payload) VALUES (?)", ("x" * 4096,))

    assert _scope_fingerprint() != before


def test_scope_fingerprint_covers_running_work_stores():
    """A background process starting or exiting rewrites ``processes.json``
    with NO EventLog event (``running_work_store_paths``'s own contract). The
    serve read-model cache adopted these stores on 2026-08-03; the stream
    backstop did not, so a stream consumer rendered the last pre-exit
    ``running_work`` row forever (live incident 2026-08-11: a 20-second
    terminal task showed "Terminal · running" in the Launcher minutes after
    the durable side had settled). Resolved through the writers' own path
    authority — never a second path list."""

    import time as _time

    from hermes_constants import get_hermes_background_work_home

    from agent_runtime.stream import _scope_fingerprint

    checkpoint = get_hermes_background_work_home() / "processes.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text("[]", encoding="utf-8")

    before = _scope_fingerprint()

    _time.sleep(0.02)  # NTFS mtime granularity guard
    # A different byte length so size moves even if the mtime tick doesn't.
    checkpoint.write_text('[{"id": "proc_fingerprint_probe"}]', encoding="utf-8")

    assert _scope_fingerprint() != before


def _wal_mode_head_home_db():
    """The head-home chat ``state.db`` in WAL mode, with NO connection left open.

    WAL mode and the closed state both matter: the defect under test is a
    connection-LIFETIME artefact, so a helper that leaves a handle open hides
    it (measured while building this slice — an instrumentation connection held
    open during the probe kept the WAL alive and made the UNFIXED code look
    correct).
    """

    import sqlite3

    from hermes_constants import get_hermes_head_home

    db_path = get_hermes_head_home() / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS probe (payload TEXT)")
        con.commit()
    finally:
        con.close()
    return db_path


def test_scope_fingerprint_ignores_session_db_connection_lifetime():
    """MERELY OPENING the chat database must not move the fingerprint.

    The storm this pins (measured on the operator's runtime, 22.16 h to
    2026-08-21 09:06): 2433 ``snapshot_build reason=demote`` against 35
    hydrates - 2.29 h of CPU - and 1239 ``state.reconciled``, 96.9% of every
    event appended in the window, at a median 9.0 s spacing. SQLite deletes the
    ``-wal`` sibling on a clean last-close and re-creates it EMPTY on the next
    open, so under a raw stat of the siblings a poll landing while any process
    held the database open read a fresh ``mtime_ns`` and a poll landing at rest
    read ``absent``. A database nobody wrote to flapped the fingerprint twice
    per open, and each flap costs one synthetic ``state.reconciled`` -> one
    UNCOVERED batch -> one full core rebuild.

    This test asserts NOTHING about writes; that direction is
    ``test_scope_fingerprint_moves_on_committed_chat_write``, and the pair is
    the point - a fingerprint pinned constant would pass this one alone.
    """

    import sqlite3

    from agent_runtime.stream import _scope_fingerprint

    db_path = _wal_mode_head_home_db()
    wal_path = db_path.with_name(db_path.name + "-wal")

    at_rest = _scope_fingerprint()

    reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        reader.execute("SELECT count(*) FROM probe").fetchall()
        assert wal_path.exists(), (
            "precondition: the open must have created the -wal sibling, or this "
            "test is not exercising the flip it exists to catch"
        )
        assert _scope_fingerprint() == at_rest, (
            "opening the chat SessionDB moved the scope fingerprint - a READ is "
            "being keyed as a write, which is the 2433-rebuild storm"
        )
    finally:
        reader.close()

    assert (
        _scope_fingerprint() == at_rest
    ), "closing the chat SessionDB moved the scope fingerprint"


def test_scope_fingerprint_ignores_checkpoint_without_data_change():
    """A checkpoint that moves no rows must not move the fingerprint.

    The second half of the same class: a WAL truncated back to zero length, and
    its mtime moved, says only that frames already durable in ``state.db`` were
    copied there. Under the mask a frameless WAL keys as one triple however it
    got that way.
    """

    import os
    import sqlite3

    from agent_runtime.stream import _scope_fingerprint

    db_path = _wal_mode_head_home_db()

    con = sqlite3.connect(db_path)
    try:
        before = _scope_fingerprint()
        con.execute("PRAGMA wal_checkpoint(PASSIVE)")
        os.utime(db_path.with_name(db_path.name + "-wal"), None)
        assert (
            _scope_fingerprint() == before
        ), "a checkpoint/utime with no committed row moved the fingerprint"
    finally:
        con.close()


def test_scope_fingerprint_moves_on_committed_chat_write():
    """A REAL committed write from another connection MUST move the fingerprint.

    The guarantee half, and the gate that convicts the cheapest wrong fix: a
    fingerprint pinned to a constant kills the rebuild storm perfectly and
    re-opens live incident 2026-07-25 (Chat History frozen ~36 h) in the same
    stroke. Deliberately the WAL-mode, separate-connection, NOT-yet-checkpointed
    case - the one a sibling mask has to be careful not to swallow, because
    ``state.db``'s own mtime and size have not moved at all yet.
    """

    import sqlite3

    from agent_runtime.stream import _scope_fingerprint

    db_path = _wal_mode_head_home_db()

    # Held open so the writer's own close cannot checkpoint the frames away and
    # let ``state.db``'s triple carry the signal instead of the WAL's.
    reader_side = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        before = _scope_fingerprint()
        main_before = db_path.stat()

        writer = sqlite3.connect(db_path)
        try:
            writer.execute("INSERT INTO probe (payload) VALUES (?)", ("minted",))
            writer.commit()

            assert _scope_fingerprint() != before, (
                "a committed chat write did not move the scope fingerprint - the "
                "watchdog is blind and Chat History freezes (incident 2026-07-25)"
            )
            main_after = db_path.stat()
            assert (main_after.st_mtime_ns, main_after.st_size) == (
                main_before.st_mtime_ns,
                main_before.st_size,
            ), (
                "precondition drifted: the commit reached state.db directly, so "
                "this run never exercised the uncheckpointed-WAL path"
            )
        finally:
            writer.close()
    finally:
        reader_side.close()


def test_scope_fingerprint_absent_session_db_is_stable_and_its_arrival_moves_it():
    """Missing-database behaviour, pinned in both directions.

    A database that does not exist yet must key STABLY (the behaviour the old
    ``absent`` sentinel had), and a database that APPEARS must move the
    fingerprint - the distinction the WAL mask suspends for the WAL sibling
    alone and must not lose for the database itself.
    """

    import sqlite3

    from hermes_constants import get_hermes_head_home

    from agent_runtime.stream import _scope_fingerprint

    db_path = get_hermes_head_home() / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists():
            candidate.unlink()

    absent = _scope_fingerprint()
    assert _scope_fingerprint() == absent, "an absent database is not keyed stably"

    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE probe (payload TEXT)")
        con.commit()
    finally:
        con.close()

    assert (
        _scope_fingerprint() != absent
    ), "the chat database APPEARING did not move the fingerprint"


def test_stream_delivers_eventless_chat_write_within_the_staleness_slo():
    """End-to-end: a committed chat write with NO event reaches the consumer.

    The in-process pin on the staleness SLO that incident 2026-07-25 bought. A
    row is committed straight into the head-home SessionDB - no store call, no
    EventLog append - and the stream must produce a ``state.reconciled`` delta
    without the consumer asking for anything.
    """

    import sqlite3

    from agent_runtime.stream import stream_frames

    db_path = _wal_mode_head_home_db()

    frames = stream_frames(poll_interval_seconds=0.01, heartbeat_interval_seconds=0.05)
    assert drain_boot_liveness(frames)["type"] == "hydrate"

    writer = sqlite3.connect(db_path)
    try:
        writer.execute("INSERT INTO probe (payload) VALUES (?)", ("eventless mint",))
        writer.commit()
    finally:
        writer.close()

    reconciled = None
    for _ in range(80):
        frame = next(frames)
        if frame.get("type") != "delta":
            continue
        event = (frame.get("entity") or {}).get("event") or {}
        if event.get("type") == "state.reconciled":
            reconciled = event
            break
    frames.close()

    assert reconciled is not None, (
        "an event-less committed chat write never reached the stream consumer - "
        "the staleness SLO incident 2026-07-25 bought is broken"
    )
    assert reconciled["payload"]["source"] == "stream_watchdog"


def test_stream_stays_quiet_while_the_session_db_is_merely_opened():
    """The storm, end to end: reading the chat database appends NO reconcile.

    ``test_scope_fingerprint_ignores_session_db_connection_lifetime`` pins the
    fingerprint; this pins the CONSEQUENCE, which is what actually cost 2.29 h
    of CPU - every flap became a synthetic ``state.reconciled``, which
    ``patch_coverage`` classifies UNCOVERED, which demotes the batch to a full
    core rebuild (``snapshot_build reason=demote``).
    """

    import sqlite3

    from agent_runtime.stream import stream_frames

    db_path = _wal_mode_head_home_db()

    frames = stream_frames(poll_interval_seconds=0.01, heartbeat_interval_seconds=0.03)
    assert drain_boot_liveness(frames)["type"] == "hydrate"

    kinds = []
    for index in range(6):
        # Open and close the database between polls, writing nothing - exactly
        # what every projection read, drain tick and presence probe does. The
        # alternating close order makes half the polls land with the WAL present
        # and half with it gone, which is the flip itself.
        probe = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        probe.execute("SELECT count(*) FROM probe").fetchall()
        if index % 2:
            probe.close()
        kinds.append(next(frames)["type"])
        probe.close()
    frames.close()

    assert set(kinds) == {"heartbeat"}, (
        f"reading the chat SessionDB made the watchdog reconcile ({kinds}) - each "
        "one costs a full core rebuild; this is the 2433-demote storm"
    )


def test_eventless_write_coincident_with_evented_batch_still_converges():
    """Live-proof regression (2026-07-09): the watchdog memo must be taken
    BEFORE the delta batch. A memo taken after the batch absorbed any
    event-less write racing the batch — swallowed forever. With the pre-batch
    candidate, a write visible before the batch is delivered through the
    batch's own full cores (each delta rebuilds the snapshot at emission
    time), and a write after the candidate reconciles at the next heartbeat —
    either way the client converges."""

    import json

    import agent_runtime.paths as paths
    from agent_runtime.store import RealmStore, WorkspaceStore
    from agent_runtime.stream import stream_frames

    realm_a = RealmStore().create(name="Realm A")
    realm_b = RealmStore().create(name="Realm B")
    workspace = WorkspaceStore().create(name="WS")
    RealmStore().set_active(realm_a.id)

    frames = stream_frames(poll_interval_seconds=0.01, heartbeat_interval_seconds=0.05)
    # Past the boot build's own liveness (MC-4 / P6): at a sub-second cadence
    # the boot build heartbeats before its hydrate. Identified by the activity
    # block, never by position.
    assert drain_boot_liveness(frames)["type"] == "hydrate"

    # Evented mutation AND a raw rule-violating write land in the same window.
    RealmStore().set_active(realm_b.id)
    pointer = paths.active_workspace_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"workspace_id": workspace.id}), encoding="utf-8")

    converged = None
    for _ in range(60):
        frame = next(frames)
        if frame["type"] != "delta":
            continue
        core = frame["core"]
        if core.get("active_realm_id") == realm_b.id and core.get("active_workspace_id") == workspace.id:
            converged = frame
            break
    frames.close()
    assert converged is not None, (
        "client never converged on BOTH the evented switch and the raw write — "
        "the batch/memo race is back"
    )


def test_stream_backstop_stays_quiet_without_mutations():
    """No false positives: an idle harness heartbeats, never reconciles."""

    from agent_runtime.stream import stream_frames

    frames = stream_frames(poll_interval_seconds=0.01, heartbeat_interval_seconds=0.03)
    # Past the boot build's own liveness (MC-4 / P6): at a sub-second cadence
    # the boot build heartbeats before its hydrate. Identified by the activity
    # block, never by position.
    assert drain_boot_liveness(frames)["type"] == "hydrate"
    kinds = {next(frames)["type"] for _ in range(3)}
    frames.close()
    assert kinds == {"heartbeat"}


# ── Slice D: contract-validated appends — strict in CI, observe live ────────


def test_strict_mode_rejects_missing_summary_fields(monkeypatch):
    from hermes_time import now as _now

    from agent_runtime.models import Event

    monkeypatch.setenv("HERMES_EVENT_CONTRACT_STRICT", "1")
    import pytest

    with pytest.raises(ValueError, match="missing contract summary fields"):
        EventLog().append(Event(_now(), "realm.created", None, None, None, {}))
    assert EventLog().tail(1) == []


def test_observe_mode_warns_but_appends(monkeypatch, caplog):
    import logging

    from hermes_time import now as _now

    from agent_runtime.models import Event

    monkeypatch.delenv("HERMES_EVENT_CONTRACT_STRICT", raising=False)
    import agent_runtime.events as events_module

    events_module._WARNED_CONTRACT_SHAPES.clear()  # per-process warn dedupe
    with caplog.at_level(logging.WARNING, logger="agent_runtime.events"):
        EventLog().append(Event(_now(), "realm.created", None, None, None, {}))
    assert EventLog().tail(1)[0].type == "realm.created"
    assert any("missing contract summary fields" in record.message for record in caplog.records)


def test_strict_mode_accepts_contract_complete_payload(monkeypatch):
    from hermes_time import now as _now

    from agent_runtime.models import Event

    monkeypatch.setenv("HERMES_EVENT_CONTRACT_STRICT", "1")
    EventLog().append(
        Event(_now(), "realm.created", None, None, None, {"realm_id": "realm_x", "name": "X"})
    )
    assert EventLog().tail(1)[0].payload["realm_id"] == "realm_x"


# ── Slice F: heartbeat frames carry NO core (the launcher drops same-offset
# snapshots, so a heartbeat core would be a silently-dead channel) ──────────


def test_heartbeat_frame_carries_no_core():
    from agent_runtime.stream import heartbeat_frame

    frame = heartbeat_frame(offset=123)
    assert frame["type"] == "heartbeat"
    assert "core" not in frame
    assert frame["watermark"]["event_offset"] == 123


def test_workspace_use_verb_emits_exactly_one_activation(capsys):
    """No double emission: the verb no longer appends on top of the store."""
    from hermes_cli.harness import _cmd_workspace_use
    from agent_runtime.store import WorkspaceStore

    workspace = WorkspaceStore().create(name="Verb WS")
    assert _cmd_workspace_use(SimpleNamespace(workspace_id=workspace.id, json=True)) == 0
    capsys.readouterr()
    activations = [
        e
        for e in EventLog().tail(10)
        if e.type == "workspace.activated" and e.payload.get("workspace_id") == workspace.id
    ]
    assert len(activations) == 1
