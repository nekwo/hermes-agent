import asyncio

from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


async def _run_one_blocked_pm_hook_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_blocked_pm_hook_watcher(interval=1)


def test_gateway_blocked_pm_hook_routes_blocked_event_without_subscription(tmp_path, monkeypatch):
    db_path = tmp_path / "gateway-blocked-hook.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {
            "kanban": {
                "pm_blocked_hook": {
                    "enabled": True,
                    "assignee": "pm",
                    "workspace_kind": "scratch",
                    "dispatch_after_create": False,
                }
            }
        },
    )

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Implementation complete", assignee="worker")
        assert kb.block_task(conn, tid, reason="review-required: implementation complete, tests green")
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_blocked_pm_hook_cursors = {}

    asyncio.run(_run_one_blocked_pm_hook_tick(monkeypatch, runner))

    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT id, title, assignee, body, idempotency_key FROM tasks "
            "WHERE created_by = 'kanban-blocked-hook'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["assignee"] == "pm"
        assert row["title"] == f"PM: auto-route blocked card {tid}"
        assert "non-serious process/routing blocker" in row["body"]
        assert tid in row["body"]
        assert row["idempotency_key"].startswith(f"pm-blocked-hook-{tid}-")
    finally:
        conn.close()
