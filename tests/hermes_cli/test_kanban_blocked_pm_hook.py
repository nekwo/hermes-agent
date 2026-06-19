from hermes_cli import kanban_db as kb
from hermes_cli.kanban_blocked_pm import (
    BlockedPmHookConfig,
    classify_blocked_card,
    handle_blocked_event,
    unseen_blocked_events,
)


def _setup_db(tmp_path, monkeypatch, name="blocked-hook.db"):
    db_path = tmp_path / name
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    return db_path


def test_review_required_blocked_event_creates_one_pm_auto_route_card(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="Implement feature",
            body="Body mentions credential, auth, token, redaction, taskkill as generic safety boilerplate.",
            assignee="worker",
        )
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: implementation complete, tests green, route QA",
        )

        event = unseen_blocked_events(conn, after_event_id=0)[0]
        result = handle_blocked_event(
            conn,
            event,
            BlockedPmHookConfig(pm_assignee="pm", workspace_kind="dir", workspace_path="X:/repo"),
        )
        result2 = handle_blocked_event(
            conn,
            event,
            BlockedPmHookConfig(pm_assignee="pm", workspace_kind="dir", workspace_path="X:/repo"),
        )

        assert result.created_pm_task_id
        assert result.created_pm_task_id == result2.created_pm_task_id
        pm_task = kb.get_task(conn, result.created_pm_task_id)
        assert pm_task is not None
        assert pm_task.assignee == "pm"
        assert pm_task.status == "ready"
        assert pm_task.workspace_kind == "dir"
        assert pm_task.workspace_path == "X:/repo"
        assert "PM: auto-route blocked card" in pm_task.title
        assert tid in (pm_task.body or "")
        assert "non-serious process/routing blocker" in (pm_task.body or "")
    finally:
        conn.close()


def test_serious_credential_blocker_creates_serious_pm_triage_card(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, "serious-hook.db")
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Needs credential", assignee="worker")
        assert kb.block_task(
            conn,
            tid,
            reason="cannot proceed without staging credential and Tony approval",
        )

        event = unseen_blocked_events(conn, after_event_id=0)[0]
        result = handle_blocked_event(
            conn,
            event,
            BlockedPmHookConfig(pm_assignee="pm"),
        )

        assert result.created_pm_task_id
        pm_task = kb.get_task(conn, result.created_pm_task_id)
        assert pm_task is not None
        assert "PM: serious blocker triage" in pm_task.title
        assert "serious blocker candidate" in (pm_task.body or "")
        assert tid in (pm_task.body or "")
    finally:
        conn.close()


def test_classifier_ignores_body_boilerplate_when_review_signal_is_non_serious(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch, "classifier-hook.db")
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="Review gate",
            body="This body includes security, token, password, auth, redaction, production, migration, and taskkill boilerplate.",
            assignee="worker",
        )
        kb.add_comment(conn, tid, "worker", "implementation complete; commit exists; self-QA green")
        assert kb.block_task(conn, tid, reason="review-required: commit ready for QA")

        classification = classify_blocked_card(conn, tid)

        assert classification.actionable is True
        assert classification.serious is False
        assert classification.reason == "non-serious-process"
        assert "review-required" in classification.signal
        assert "password" not in classification.signal
    finally:
        conn.close()
