"""S2 read-model goldens: history out of the live frame (operator move 6).

Pins the eviction shape (S7-B RULING-0: the evicted pointer-stub shape is the
ONLY shape — no kill-switch, no full-in-frame legacy branch), the byte-budget
drop, and the log-backed paged history queries that replace the evicted
sections. Archive-never-delete is verified: the rows leave the FRAME, never the
disk stores.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from datetime import timedelta

import pytest

from hermes_time import now
from utils import atomic_json_write

import hermes_cli.harness as harness
from agent_runtime.models import Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.snapshot import (
    ARCHIVED_TASKS_REF_RECENT_CAP,
    _archived_task_summaries,
    _open_incidents_frame,
    _persona_chat_history_frame,
    build_snapshot,
    snapshot_section_bytes,
)
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore, _write_model
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime import paths


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _seed_archive_batch(root, *, count: int, description: str = "x") -> None:
    archive = root / "deleted_archive" / "20260601T010203Z_clear_ready"
    (archive / "tasks").mkdir(parents=True)
    atomic_json_write(
        archive / "manifest.json",
        {"reason": "Tony cleared ready missions", "created_at_utc": "2026-06-01T01:02:03Z"},
    )
    for index in range(count):
        atomic_json_write(
            archive / "tasks" / f"task_archived_{index}.json",
            {
                "id": f"task_archived_{index}",
                "title": f"Archived mission {index}",
                "state": "done",
                "description": description,
            },
        )


def _seed_incident(store, incident_id, *, closed_delta_hours=None) -> None:
    n = now()
    store.open(
        Incident(
            id=incident_id,
            task_id="t1",
            run_id=None,
            kind="proof_failure",
            summary=f"summary {incident_id}",
            detail_path=None,
            opened_at=n,
        )
    )
    if closed_delta_hours is not None:
        incident = store.get(incident_id)
        incident.closed_at = n - timedelta(hours=closed_delta_hours)
        _write_model(paths.incident_path(incident_id), incident)


@pytest.fixture
def parser():
    root = argparse.ArgumentParser()
    subs = root.add_subparsers()
    harness.build_parser(subs)
    return root


def _run(parser, argv):
    ns = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ns.func(ns)
    return rc, buf.getvalue()


# --------------------------------------------------------------------------- #
# archived_tasks — evicted to a pointer stub by default
# --------------------------------------------------------------------------- #


def test_archived_tasks_evicted_to_pointer_stub_by_default(isolate_agent_runtime_root):
    _seed_archive_batch(isolate_agent_runtime_root, count=3)
    snap = build_snapshot()
    ref = snap["archived_tasks"]
    assert isinstance(ref, dict), "archived_tasks must be a typed pointer stub, not the row array"
    assert ref["evicted"] is True
    assert ref["count"] == 3
    # recent_ids mirror the archived-task projection order (newest batch first,
    # filename-ordered within a batch).
    assert set(ref["recent_ids"]) == {"task_archived_0", "task_archived_1", "task_archived_2"}
    assert "task history" in ref["fetch"]


def test_archived_tasks_recent_ids_capped(isolate_agent_runtime_root):
    _seed_archive_batch(isolate_agent_runtime_root, count=ARCHIVED_TASKS_REF_RECENT_CAP + 5)
    snap = build_snapshot()
    ref = snap["archived_tasks"]
    # _archived_task_summaries caps the projection itself at 25; the ref never
    # exceeds the recent-id cap either.
    assert len(ref["recent_ids"]) <= ARCHIVED_TASKS_REF_RECENT_CAP


def test_archive_batch_preserved_on_disk_after_eviction(isolate_agent_runtime_root):
    _seed_archive_batch(isolate_agent_runtime_root, count=1)
    build_snapshot()
    # Archive-never-delete: eviction removes rows from the frame, not from disk.
    assert (isolate_agent_runtime_root / "deleted_archive" / "20260601T010203Z_clear_ready" / "tasks" / "task_archived_0.json").exists()


# --------------------------------------------------------------------------- #
# incidents — windowed, closed/ancient tail evicted to a history ref
# --------------------------------------------------------------------------- #


def test_incidents_open_only_and_history_ref(isolate_agent_runtime_root):
    # Open-only retention (operator decision 2026-07-16): EVERY closed incident
    # is history, regardless of how recently it closed.
    store = IncidentStore()
    _seed_incident(store, "inc_open")  # open → always in-frame
    _seed_incident(store, "inc_recent", closed_delta_hours=1)  # closed → history
    _seed_incident(store, "inc_ancient", closed_delta_hours=1000)  # closed → history

    snap = build_snapshot()
    frame_ids = {row["incident_id"] for row in list(snap["incidents"].values())}
    assert frame_ids == {"inc_open"}, "only open incidents ship in the frame"

    ref = snap["incidents_history_ref"]
    assert ref["evicted"] is True
    assert ref["closed_evicted"] is True
    assert ref["count"] == 2  # both closed incidents are history
    assert "window_hours" not in ref  # no TTL window
    assert "incident list" in ref["fetch"]

    # Archive-never-delete: both closed incidents are still on disk.
    assert paths.incident_path("inc_recent").exists()
    assert paths.incident_path("inc_ancient").exists()


def test_open_incidents_summary_unaffected_by_eviction(isolate_agent_runtime_root):
    store = IncidentStore()
    _seed_incident(store, "inc_open_1")
    _seed_incident(store, "inc_open_2")
    _seed_incident(store, "inc_closed", closed_delta_hours=1)
    snap = build_snapshot()
    # The summary counts OPEN incidents off the full list, not the filtered frame.
    assert snap["summary"]["open_incidents"] == 2


def test_open_incidents_frame_helper_open_only():
    from types import SimpleNamespace

    incidents = [
        SimpleNamespace(closed_at=None),
        SimpleNamespace(closed_at=now()),
        SimpleNamespace(closed_at=None),
    ]
    # The helper always evicts closed incidents (RULING-0: open-only is the only
    # shape) — two open kept, one closed evicted.
    kept, evicted = _open_incidents_frame(incidents)
    assert len(kept) == 2 and evicted == 1


# --------------------------------------------------------------------------- #
# persona_chat_history — recency pointers, tail dropped
# --------------------------------------------------------------------------- #


def test_persona_chat_history_frame_strips_tail_keeps_anchors():
    rows = [
        {
            "session_id": "s1",
            "persona_id": "alice",
            "kind": "chat",
            "live_mission": False,
            "title": "Hello",
            "last_message_preview": "hi",
            "message_count": 12,
            "created_at": "2026-07-16T00:00:00Z",
            "updated_at": "2026-07-16T00:01:00Z",
            "messages": [{"id": "m1", "role": "operator", "text": "hi"}],
        }
    ]
    pointers = _persona_chat_history_frame(rows)
    row = pointers[0]
    assert row["messages"] == []
    assert row["messages_evicted"] is True
    # Recency anchors survive (chat-visibility-contract stays; it reads anchors).
    for key in ("session_id", "persona_id", "kind", "message_count", "created_at", "updated_at", "last_message_preview"):
        assert row[key] == rows[0][key]


# --------------------------------------------------------------------------- #
# byte budget — evicting drops real bytes; the stub is tiny
# --------------------------------------------------------------------------- #


def test_history_eviction_drops_bytes(isolate_agent_runtime_root):
    # Seed a heavy archive (big descriptions) + many closed/ancient incidents.
    from agent_runtime.serde import to_jsonable as _jsonable

    big = "D" * 30_000
    _seed_archive_batch(isolate_agent_runtime_root, count=10, description=big)
    store = IncidentStore()
    for index in range(40):
        _seed_incident(store, f"inc_old_{index}", closed_delta_hours=100)

    # The only shape: history evicted (S7-B RULING-0 — no in-frame legacy build to
    # A/B against). The eviction win is measured against the WEIGHT of the rows
    # the pointer replaced, read straight from the projection the pointer
    # references (the same rows `harness task history` serves).
    snap = build_snapshot()

    archived_stub = snapshot_section_bytes(snap, "archived_tasks")
    assert archived_stub < 1_000  # a pointer stub, not the row array
    assert snap["archived_tasks"]["evicted"] is True
    assert snap["archived_tasks"]["count"] == 10

    full_rows = _archived_task_summaries()
    archived_full = len(
        json.dumps(_jsonable(full_rows), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert archived_full > 250_000  # 10 * 30KB descriptions
    # The bytes the stub keeps out of every steady-state frame.
    assert archived_full - archived_stub >= 250_000

    # Every closed incident left the frame; only the typed history ref accounts
    # for them (open-only retention). The in-frame incidents map is empty here.
    assert snap["incidents"] == {}
    assert snap["incidents_history_ref"]["count"] == 40


# --------------------------------------------------------------------------- #
# log-backed paged history queries (the replacements)
# --------------------------------------------------------------------------- #


def _create_goal(title="Goal", description="do it"):
    n = now()
    task = Task(
        id="goal_hist",
        title=title,
        description=description,
        state=TaskState.RUNNING,
        created_at=n,
        updated_at=n,
        requested_by="tony",
    )
    task.goal_id = "goal_hist"
    TaskStore().create(task)
    return task


def test_goal_history_pages_eventlog_by_id(isolate_agent_runtime_root, parser):
    task = _create_goal()
    log = EventLog()
    for index in range(5):
        log.append(Event(ts=now(), type="run.opened", task_id=task.id, run_id=f"run_{index}", persona_id="dev", payload={"stage_id": "impl"}))
    # A different task's events must not leak into this goal's history.
    log.append(Event(ts=now(), type="run.opened", task_id="other", run_id="run_x", persona_id="dev", payload={"stage_id": "impl"}))

    rc, out = _run(parser, ["harness", "goal", "history", task.id, "--limit", "3", "--output", "json"])
    assert rc == 0
    data = json.loads(out)
    rows = data["items"] if isinstance(data, dict) and "items" in data else data.get("rows", data)
    # Envelope shape tolerance: pull the event rows out whatever the wrapper.
    events = _extract_rows(data)
    assert events, out
    assert all(row.get("task_id") == task.id for row in events)
    assert len(events) <= 3


def test_incident_history_pages_closed(isolate_agent_runtime_root, parser):
    store = IncidentStore()
    for index in range(4):
        _seed_incident(store, f"inc_closed_{index}", closed_delta_hours=index + 1)

    rc, out = _run(parser, ["harness", "incident", "list", "--state", "closed", "--limit", "2", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert data["ok"] is True
    assert data["state"] == "closed"
    assert data["count"] == 2
    assert data["truncated"] is True
    assert data["next_before"] is not None
    assert all(row["is_open"] is False for row in data["incidents"])

    # Page 2 via the returned cursor.
    rc2, out2 = _run(parser, ["harness", "incident", "list", "--state", "closed", "--before", str(data["next_before"]), "--json"])
    page2 = json.loads(out2)
    ids1 = {row["incident_id"] for row in data["incidents"]}
    ids2 = {row["incident_id"] for row in page2["incidents"]}
    assert ids1.isdisjoint(ids2)


def test_persona_chat_history_fetch_returns_tail():
    from agent_runtime.persona_chat_history import persona_chat_session_messages

    class FakeSessionDB:
        def get_messages(self, session_id, include_inactive=False):
            return [
                {"id": "m1", "role": "operator", "content": "hi neko"},
                {"id": "m2", "role": "assistant", "content": "Hello — how can I help?"},
            ]

    data = persona_chat_session_messages(session_id="persona_chat_x", limit=40, session_db=FakeSessionDB())
    assert data["ok"] is True
    assert data["session_id"] == "persona_chat_x"
    assert data["count"] == 2
    assert data["total_count"] == 2
    assert data["has_more"] is False
    assert data["next_before"] is None
    assert data["history_revision"]
    assert [m["role"] for m in data["messages"]] == ["operator", "agent"]


def test_persona_chat_history_fetch_pages_complete_transcript_without_overlap():
    from agent_runtime.persona_chat_history import persona_chat_session_messages

    class FakeSessionDB:
        def get_messages(self, session_id, include_inactive=False):
            return [
                {
                    "id": f"m{index:03d}",
                    "role": "operator" if index % 2 == 0 else "assistant",
                    "content": f"message {index:03d}",
                    "timestamp": 1_780_000_000 + index,
                }
                for index in range(95)
            ]

    db = FakeSessionDB()
    pages = []
    before = None
    revision = None
    while True:
        page = persona_chat_session_messages(
            session_id="persona_chat_x",
            limit=40,
            before=before,
            session_db=db,
        )
        assert page["ok"] is True
        assert page["total_count"] == 95
        revision = revision or page["history_revision"]
        assert page["history_revision"] == revision
        pages.insert(0, page["messages"])
        if not page["has_more"]:
            assert page["next_before"] is None
            break
        assert page["next_before"]
        before = page["next_before"]

    messages = [message for page in pages for message in page]
    assert [len(page) for page in pages] == [15, 40, 40]
    assert len(messages) == 95
    assert len({message["id"] for message in messages}) == 95
    assert messages[0]["text"] == "message 000"
    assert messages[-1]["text"] == "message 094"


def test_persona_chat_history_fetch_rejects_foreign_or_malformed_cursor():
    from agent_runtime.persona_chat_history import persona_chat_session_messages

    class FakeSessionDB:
        def get_messages(self, session_id, include_inactive=False):
            return [
                {"id": "m1", "role": "operator", "content": "hello"},
                {"id": "m2", "role": "assistant", "content": "hi"},
            ]

    first = persona_chat_session_messages(
        session_id="persona_chat_a", limit=1, session_db=FakeSessionDB()
    )
    foreign = persona_chat_session_messages(
        session_id="persona_chat_b",
        limit=1,
        before=first["next_before"],
        session_db=FakeSessionDB(),
    )
    malformed = persona_chat_session_messages(
        session_id="persona_chat_a",
        limit=1,
        before="not-a-history-cursor",
        session_db=FakeSessionDB(),
    )

    assert foreign["ok"] is False
    assert foreign["error_kind"] == "invalid_history_cursor"
    assert malformed["ok"] is False
    assert malformed["error_kind"] == "invalid_history_cursor"


def test_persona_chat_history_cli_accepts_opaque_before_cursor(parser):
    args = parser.parse_args(
        [
            "harness",
            "persona",
            "chat",
            "history",
            "--session-id",
            "persona_chat_a",
            "--limit",
            "40",
            "--before",
            "opaque-cursor",
            "--json",
        ]
    )
    assert args.session_id == "persona_chat_a"
    assert args.limit == 40
    assert args.before == "opaque-cursor"


def test_persona_chat_history_hides_runtime_envelope_but_keeps_turn_reference():
    from agent_runtime.persona_chat_history import persona_chat_session_messages
    from agent_runtime.runtime_hud import render_runtime_context_envelope

    envelope = render_runtime_context_envelope(
        context_id="ctx_history",
        revision="hud_0123456789abcdef",
        delivery="snapshot",
        situational_hud_content="## Runtime Situation\n- Scope: default",
    )

    class FakeSessionDB:
        def get_messages(self, session_id, include_inactive=False):
            return [{"id": "m1", "role": "user", "content": f"hi\n\n{envelope}"}]

    data = persona_chat_session_messages(
        session_id="persona_chat_x", limit=40, session_db=FakeSessionDB()
    )
    assert data["messages"][0]["text"] == "hi"
    assert data["messages"][0]["runtime_context"] == {
        "context_id": "ctx_history",
        "revision": "hud_0123456789abcdef",
        "delivery": "snapshot",
    }


def test_persona_chat_history_fetch_empty_without_sessiondb():
    from agent_runtime.persona_chat_history import persona_chat_session_messages

    data = persona_chat_session_messages(session_id="persona_chat_x", limit=40, session_db=None)
    assert data["ok"] is True
    assert data["count"] == 0
    assert data["total_count"] == 0
    assert data["has_more"] is False
    assert data["messages"] == []


def _extract_rows(data):
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("items", "rows", "goal_event", "events", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        # stage42 list envelope: {"kind": "...", "items": [...]} or nested
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []
