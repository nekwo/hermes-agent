# Stage 01 — Gateway Responsiveness Watchdog and Notification Priority

## Goal

Fix the Hermes gateway UX failure where Telegram can look frozen while a long agent turn or background-process notification is being processed. The operator must be able to communicate while terminal work, background monitors, or Harness goals are running.

## Product stance

Messaging gateways are an operator control surface, not a batch console. Human messages must have priority over monitor/completion events, and long-running turns must show a compact, interruptible status signal instead of leaving Telegram in an ambiguous typing state.

## Root-cause audit evidence

- `gateway/run.py::_run_process_watcher` currently treats `notify_on_complete=True` as an internal synthetic `MessageEvent` and calls `adapter.handle_message(...)`, which creates a full agent turn for a process completion.
- `tui_gateway/server.py::_notification_poller_loop` similarly emits a process status update and then chains `_run_prompt_submit(...)`, creating an agent turn for a notification if the session is idle.
- `gateway/run.py::_handle_message` has running-agent busy handling for human messages, but internal background notifications use the same message path and can consume the session turn slot.
- `gateway/run.py::_notify_long_running` already has a heartbeat loop, but the default interval comes from `agent.gateway_notify_interval` and the heartbeat copy does not explicitly explain `/stop`.
- Alice profile currently has `display.background_process_notifications: all`, which is too noisy for Telegram and increases the odds that monitor/status traffic competes with human steering.

## Fixed architecture decisions

1. Background process completion notifications default to compact direct platform sends, not agent-reasoned turns.
2. Human-originated messages remain the only normal trigger for a full agent turn. Internal monitor/completion events should never starve a human reply.
3. Legacy agent-turn notification behavior may remain behind an explicit opt-in config/env if needed for users who depended on the old behavior.
4. Long-running turn watchdog status must include clear `/stop` guidance and use edit-in-place when the platform supports it.
5. Alice profile should favor `background_process_notifications: result` and `busy_text_mode: interrupt` for Telegram responsiveness.

## Stages

### Stage A — Regression tests

Affected files:

- `tests/gateway/test_background_process_notifications.py`
- `tests/gateway/test_gateway_responsiveness.py` or nearby gateway tests

Tests/proof:

- Default background notification mode is `result`, not `all`.
- `notify_on_complete=True` delivers a compact direct send and does not call `adapter.handle_message` by default.
- Completion direct-send preserves topic/thread metadata and reply anchor where available.
- Long-running heartbeat text includes elapsed/status context and `/stop` guidance.

### Stage B — Gateway notification implementation

Affected files:

- `gateway/run.py`
- `tui_gateway/server.py`
- `hermes_cli/config.py`

Implementation actions:

- Add helper(s) for compact process notification text with redaction/truncation.
- Gate old internal-agent notification injection behind explicit legacy config/env.
- Change default `display.background_process_notifications` to `result`.
- Update long-running heartbeat copy to include `/stop` guidance.

### Stage C — Alice profile safety defaults

Affected file:

- `X:/Eternia/.hermes/profiles/alice/config.yaml`

Implementation actions:

- Change `display.background_process_notifications` from `all` to `result`.
- Ensure `display.busy_text_mode: interrupt` is present so Telegram text can cut in instead of silently queueing when configured that way.

### Stage D — Verification and gap loop

Verification commands:

```bash
cd C:/Users/beast/AppData/Local/hermes/hermes-agent
venv/Scripts/python.exe -m pytest -o addopts='' -p no:timeout tests/gateway/test_background_process_notifications.py tests/gateway/test_gateway_inactivity_timeout.py tests/e2e/test_platform_commands.py tests/test_tui_gateway_server.py::test_notification_poller_delivers_status_only_by_default tests/test_tui_gateway_server.py::test_notification_poller_legacy_agent_turn_env_opt_in tests/test_tui_gateway_server.py::test_tui_background_agent_turns_can_be_enabled_by_config tests/test_tui_gateway_server.py::test_notification_poller_status_only_when_busy_by_default tests/test_tui_gateway_server.py::test_notification_poller_skips_consumed -q
venv/Scripts/python.exe -m compileall -q gateway/run.py tui_gateway/server.py hermes_cli/config.py tools/process_registry.py tools/terminal_tool.py tests/gateway/test_background_process_notifications.py tests/test_tui_gateway_server.py
git diff --check
```

Status: implemented locally. Targeted tests, compile, and diff hygiene passed.

Implementation proof points:

- Gateway watcher `notify_on_complete=True` compact direct-sends by default.
- Legacy completion-as-agent-turn behavior requires `HERMES_BACKGROUND_AGENT_TURNS=true` or `display.background_process_agent_turns: true`.
- TUI notification poller emits process status only by default and supports the same legacy opt-in.
- Background notification defaults and examples now prefer `result` over noisy `all`.
- Process notification command/output text is ANSI-stripped, bounded, and redacted before UI/status delivery.
- Long-running heartbeat text now includes `/stop` guidance.
- Alice profile was updated to `busy_text_mode: interrupt`, `background_process_notifications: result`, and `background_process_agent_turns: false`.

AAA gap checklist:

- No raw secrets, raw paths, hidden chain-of-thought, or raw process logs beyond bounded redacted tails in notifications.
- Human messages cannot be starved by synthetic background events.
- Telegram operators see `/stop` guidance during long turns.
- Alice config is less noisy by default.
- Legacy behavior remains possible only by explicit opt-in.
