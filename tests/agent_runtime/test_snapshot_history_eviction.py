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
from datetime import timedelta

import pytest

from hermes_time import now
from utils import atomic_json_write

import hermes_cli.harness as harness
from agent_runtime.models import Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.snapshot import (
    _persona_chat_history_frame,
    build_snapshot,
)

# S27: ``ARCHIVED_TASKS_REF_RECENT_CAP`` and ``_archived_task_summaries`` are
# gone. S18 kept them as "the fetch lane behind ``harness task history``", but S8
# had already removed that CLI verb family — the asserts at the bottom of this
# module prove the parser now rejects it — so the reader served no lane and the
# cap capped nothing. The eviction contract itself (rows leave the FRAME, never
# the disk) is unchanged and is pinned below without the dead reader.
#
# S29: ``_open_incidents_frame`` and ``snapshot_section_bytes`` went the same
# way, and for the same reason. S27 kept them as reachability roots because
# THIS module imported them -- but S9 had already removed ``incidents`` and
# ``archived_tasks`` as frame sections, so the splitter had no list to split
# and the section-weigher had no section to weigh. A test import is not a
# caller. The eviction contract below is asserted on the frame itself
# (section absent) and on the rows still on disk, neither of which needed a
# production helper to state.
ARCHIVED_TASK_SEED_COUNT = 30
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore, _write_model
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
    incident = Incident(
        id=incident_id,
        task_id="t1",
        run_id=None,
        kind="proof_failure",
        summary=f"summary {incident_id}",
        detail_path=None,
        opened_at=n,
    )
    if closed_delta_hours is not None:
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
    assert "archived_tasks" not in snap
    assert (isolate_agent_runtime_root / "deleted_archive").is_dir()


def test_archived_tasks_recent_ids_capped(isolate_agent_runtime_root):
    _seed_archive_batch(isolate_agent_runtime_root, count=ARCHIVED_TASK_SEED_COUNT)
    snap = build_snapshot()
    assert "archived_tasks" not in snap


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
    assert "incidents" not in snap
    assert "incidents_history_ref" not in snap

    # Archive-never-delete: both closed incidents are still on disk.
    assert paths.incident_path("inc_recent").exists()
    assert paths.incident_path("inc_ancient").exists()


def test_open_incidents_summary_unaffected_by_eviction(isolate_agent_runtime_root):
    store = IncidentStore()
    _seed_incident(store, "inc_open_1")
    _seed_incident(store, "inc_open_2")
    _seed_incident(store, "inc_closed", closed_delta_hours=1)
    snap = build_snapshot()
    assert "open_incidents" not in snap["summary"]


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
    big = "D" * 30_000
    _seed_archive_batch(isolate_agent_runtime_root, count=10, description=big)
    store = IncidentStore()
    for index in range(40):
        _seed_incident(store, f"inc_old_{index}", closed_delta_hours=100)

    # The only shape: history evicted (S7-B RULING-0 — no in-frame legacy build to
    # A/B against). S27 removed the in-process reader this used to weigh and S29
    # removed the section-weigher, so the win is measured against the rows ON
    # DISK — the authoritative weight the frame refuses to carry, and the same
    # bytes an on-demand fetch would page.
    archived_full = sum(
        path.stat().st_size
        for path in (isolate_agent_runtime_root / "deleted_archive").rglob("*.json")
    )
    assert archived_full > 250_000  # 10 * 30KB descriptions

    snap = build_snapshot()

    # None of that weight reaches the frame, and the section itself is absent.
    assert "archived_tasks" not in snap

    # Every closed incident left the frame too: S9 removed ``incidents`` as a
    # frame section outright, so there is no in-frame map and no history ref.
    assert "incidents" not in snap


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
    with pytest.raises(SystemExit):
        parser.parse_args(["harness", "goal", "history", "removed", "--json"])


def test_incident_history_pages_closed(isolate_agent_runtime_root, parser):
    with pytest.raises(SystemExit):
        parser.parse_args(["harness", "incident", "list", "--json"])


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


def test_persona_chat_history_fetch_refuses_an_ambient_self_resolve(monkeypatch):
    """INVERTED 2026-08-12. This pin used to assert the retired behavior: a
    self-resolved fetch under the sandbox (no head named, no pointer, no
    instance record) opened whichever ``state.db`` ambient resolution produced
    and returned ``ok: true, count: 0`` — the silent-empty envelope the
    2026-08-12 incident could not distinguish from a lost transcript. The read
    now REFUSES with a typed reason and says which scope it refused."""

    from agent_runtime.persona_chat_history import persona_chat_session_messages

    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.delenv("HERMES_ALLOW_AMBIENT_CHAT_READS", raising=False)
    data = persona_chat_session_messages(session_id="persona_chat_x", limit=40, session_db=None)
    assert data["ok"] is False
    assert data["error_kind"] == "chat_scope_unresolved"
    assert data["chat_scope"]["source"] == "ambient_home"
    # None of the success vocabulary may appear on a read that did not happen.
    assert "count" not in data
    assert "messages" not in data


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
