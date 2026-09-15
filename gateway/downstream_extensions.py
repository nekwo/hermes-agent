"""Fork gateway policy and queue visibility, separate from upstream dispatch."""
from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from gateway.platforms.base import MessageEvent
import os
import asyncio
import logging
from pathlib import Path
logger = logging.getLogger("gateway.run")
import time
from hermes_cli.config import cfg_get

def _needs_risk_assessor_warning(config: dict) -> bool:
    """Does this gateway run with manual approvals and no automated assessor?

    Startup heads-up (#30882): a gateway in manual approval mode with no
    automated risk assessor (tirith disabled AND no ``auxiliary.approval``
    model) can only gate dangerous commands / execute_code scripts via live
    in-chat approval, and those actions now fail closed rather than silently
    auto-running.

    Pulled out of ``GatewayRunner.__init__`` as a pure predicate so the
    condition is reachable from a unit test — the decision is unchanged.
    ``security.tirith_enabled`` is resolved through
    :mod:`hermes_cli.tirith_config`, the single authority for its default and
    its ``TIRITH_ENABLED`` override; reading it off the raw config with
    ``cfg_get`` (as this did) could not see the override, so a gateway whose
    operator had disabled scanning that way never got this heads-up.
    """
    from hermes_cli.tirith_config import tirith_enabled

    mode = str(
        cfg_get(config, "approvals", "mode", default="manual") or "manual"
    ).strip().lower()
    if mode != "manual":
        return False
    if tirith_enabled(config):
        return False
    return not cfg_get(config, "auxiliary", "approval", default=None)

class DownstreamGatewayMixin:
    @staticmethod
    def _background_agent_turns_enabled() -> bool:
        """Return True only when legacy agent-turn completion notifications are enabled.

        Background process completions used to synthesize an internal user
        message and run a full agent turn.  That is expensive and can starve a
        real human message behind monitor traffic.  Keep it as an explicit
        compatibility opt-in; compact direct sends are the default.
        """
        raw = os.getenv("HERMES_BACKGROUND_AGENT_TURNS", "").strip().lower()
        if not raw:
            from gateway.run import _load_gateway_config
            cfg = _load_gateway_config()
            raw = str(
                cfg_get(
                    cfg,
                    "display",
                    "background_process_agent_turns",
                    default="",
                ) or ""
            ).strip().lower()
        return raw in {"1", "true", "yes", "on", "agent", "legacy"}

    @staticmethod
    def _format_background_completion_notification(
        *,
        session_id: str,
        exit_code: int | None,
        command: str,
        output: str,
        limit: int = 2000,
    ) -> str:
        """Build a compact, bounded, redacted process-completion message."""
        from agent.redact import redact_sensitive_text
        from tools.ansi_strip import strip_ansi

        clean_output = strip_ansi(output or "")
        if len(clean_output) > limit:
            tail = clean_output[-limit:]
            nl = tail.find("\n")
            tail = tail[nl + 1:] if nl != -1 else tail
            clean_output = f"[… output truncated — showing last {len(tail)} chars]\n{tail}"
        clean_command = redact_sensitive_text(str(command or ""))
        clean_output = redact_sensitive_text(clean_output)
        return (
            f"[Background process {session_id} finished with exit code {exit_code}.\n"
            f"Command: {clean_command}\n"
            f"Output:\n{clean_output}]"
        )

    @staticmethod
    def _format_long_running_heartbeat(
        *,
        elapsed_seconds: float,
        status_detail: str = "",
    ) -> str:
        elapsed_mins = max(0, int(elapsed_seconds // 60))
        return (
            f"⏳ Working — {elapsed_mins} min{status_detail}. "
            f"Send /stop to interrupt if you need me back now."
        )

    async def _handle_queue_status_command(self, event: MessageEvent) -> str:
        """Handle /queue-status command with active-run and queue visibility."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        from tools.process_registry import format_uptime_short

        source = event.source
        # Must go through the awaited facade, not the raw sync store: this is
        # loop-side code and `session_store.get_or_create_session()` does
        # blocking SQLite work that would stall every other gateway turn.
        # Enforced by the AST guard in tests/gateway/test_async_session_store.py,
        # which arrived with the 2026-07-31 upstream merge; this fork-owned
        # handler predates the guard and was the single violation it found.
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        adapter = self.adapters.get(source.platform) if source else None
        pending_slot = getattr(adapter, "_pending_messages", {}) if adapter is not None else {}
        current_pending_slot = 1 if session_key in pending_slot else 0
        overflow_depth = len((getattr(self, "_queued_events", None) or {}).get(session_key, []))
        explicit_queue_depth = self._queue_depth(session_key, adapter=adapter)

        running_agents = getattr(self, "_running_agents", {}) or {}
        running_started = getattr(self, "_running_agents_ts", {}) or {}
        current_agent = running_agents.get(session_key)
        is_running = session_key in running_agents
        if not is_running:
            state = "idle"
        elif current_agent is _AGENT_PENDING_SENTINEL:
            state = "starting"
        else:
            state = "running"
        start_ts = float(running_started.get(session_key, 0) or 0)
        running_age = (
            format_uptime_short(max(0, int(time.time() - start_ts)))
            if start_ts and is_running
            else "0s"
        )

        total_pending_slots = 0
        for platform_adapter in (getattr(self, "adapters", {}) or {}).values():
            total_pending_slots += len(getattr(platform_adapter, "_pending_messages", {}) or {})
        total_overflow_depth = sum(
            len(items) for items in (getattr(self, "_queued_events", None) or {}).values()
        )

        lines = [
            "🧭 Gateway queue/status",
            "",
            f"Active agents: {len(running_agents)}",
            f"Current session: {state}",
            f"Running age: {running_age}",
            f"Pending follow-up slot: {current_pending_slot}",
            f"Explicit /queue depth: {explicit_queue_depth}",
            f"Overflow queue depth: {overflow_depth}",
            f"All pending slots: {total_pending_slots}",
            f"All overflow queued: {total_overflow_depth}",
            "",
            "Platforms:",
        ]

        adapters = getattr(self, "adapters", {}) or {}
        if adapters:
            for platform in sorted(adapters.keys(), key=lambda p: getattr(p, "value", str(p))):
                name = getattr(platform, "value", str(platform))
                lines.append(f"- {name}: connected")
        else:
            lines.append("- none: connected=0")

        failed_platforms = getattr(self, "_failed_platforms", {}) or {}
        for platform, info in sorted(
            failed_platforms.items(),
            key=lambda item: getattr(item[0], "value", str(item[0])),
        ):
            name = getattr(platform, "value", str(platform))
            paused = bool(info.get("paused")) if isinstance(info, dict) else False
            attempts = info.get("attempts", 0) if isinstance(info, dict) else 0
            state_text = "paused" if paused else "retrying"
            lines.append(f"- {name}: {state_text} (attempts={attempts})")

        return "\n".join(lines)


    async def _kanban_blocked_pm_hook_watcher(self, interval: float = 5.0) -> None:
        """React to committed Kanban `blocked` events by routing PM."""
        try:
            from hermes_cli.config import load_config as _load_config
            from hermes_cli import kanban_db as _kb
            from hermes_cli import kanban_db_connect, kanban_db_dispatch
            from hermes_cli.kanban_blocked_pm import (
                BlockedPmHookConfig,
                handle_blocked_event,
                unseen_blocked_events,
            )
        except Exception:
            logger.warning("kanban blocked PM hook: imports unavailable; disabled")
            return

        env_override = os.environ.get("HERMES_KANBAN_PM_BLOCKED_HOOK", "").strip().lower()
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban blocked PM hook: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        hook_cfg = kanban_cfg.get("pm_blocked_hook", {}) if isinstance(kanban_cfg, dict) else {}
        if not isinstance(hook_cfg, dict):
            hook_cfg = {}
        enabled = bool(hook_cfg.get("enabled", False))
        if env_override in {"1", "true", "yes", "on"}:
            enabled = True
        elif env_override in {"0", "false", "no", "off"}:
            enabled = False
        if not enabled:
            logger.info(
                "kanban blocked PM hook: disabled "
                "(set kanban.pm_blocked_hook.enabled=true to enable)"
            )
            return

        pm_config = BlockedPmHookConfig(
            pm_assignee=str(hook_cfg.get("assignee") or "pm"),
            workspace_kind=str(hook_cfg.get("workspace_kind") or "scratch"),
            workspace_path=hook_cfg.get("workspace_path"),
            priority=int(hook_cfg.get("priority", 100) or 100),
        )
        dispatch_after_create = bool(hook_cfg.get("dispatch_after_create", True))
        cursors: dict[str, int] = getattr(self, "_kanban_blocked_pm_hook_cursors", {})
        self._kanban_blocked_pm_hook_cursors = cursors

        await asyncio.sleep(5)

        while self._running:
            try:
                def _tick_once() -> list[str]:
                    outputs: list[str] = []
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = (
                                str(Path(db_path).expanduser().resolve())
                                if db_path
                                else str(_kb.kanban_db_path(slug).resolve())
                            )
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            continue
                        seen_db_paths.add(resolved_db_path)
                        conn = None
                        try:
                            conn = kanban_db_connect.connect(board=slug)
                            cursor = int(cursors.get(resolved_db_path, 0) or 0)
                            events = unseen_blocked_events(conn, after_event_id=cursor)
                            if not events:
                                max_row = conn.execute(
                                    "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
                                ).fetchone()
                                cursors[resolved_db_path] = max(
                                    cursor, int(max_row["m"] or 0)
                                )
                                continue
                            created_any = False
                            for event in events:
                                result = handle_blocked_event(conn, event, pm_config)
                                cursors[resolved_db_path] = max(
                                    cursors.get(resolved_db_path, 0), int(event.id)
                                )
                                if result.created_pm_task_id:
                                    created_any = True
                                    outputs.append(
                                        f"{slug}:{event.task_id}->"
                                        f"{result.created_pm_task_id}:{result.action}"
                                    )
                            if created_any and dispatch_after_create:
                                try:
                                    kanban_db_dispatch.dispatch_once(conn, board=slug, max_spawn=1)
                                except Exception as exc:
                                    logger.warning(
                                        "kanban blocked PM hook: dispatch after create "
                                        "failed on board %s: %s",
                                        slug,
                                        exc,
                                    )
                        except Exception as exc:
                            logger.warning(
                                "kanban blocked PM hook: board %s tick failed: %s",
                                slug,
                                exc,
                            )
                        finally:
                            if conn is not None:
                                conn.close()
                    return outputs

                routed = await asyncio.to_thread(_tick_once)
                for item in routed:
                    logger.info("kanban blocked PM hook routed %s", item)
            except Exception as exc:
                logger.warning("kanban blocked PM hook tick failed: %s", exc)
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)
