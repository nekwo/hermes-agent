# Integration checkpoint 2 — 2026-09-15

IN PROGRESS. No combined regression test has run; this is not a deliverable tree.

This cumulative recovery patch covers reconciled conflict files and their changed/new dependencies relative to pinned upstream 110baa095bc7135a0624557a9cc35df0f98ece0f. The active merge remains in `X:/wt/hermes-upstream-integration-20260915`. Do not reset/abort it. Original main remains 34ad8ba33f2508ab10bb24a26f0377ddb62660cb.

## Additional resolutions

- AIAgent retains blocked tool names, per-call usage ledger, cache routing receipts and journal retry reuse across upstream's new facade/mixins. Native persona wire projection runs at the new batch-persistence boundary.
- Init and Codex timing moved to dedicated downstream modules; upstream stream assembly, single-writer retirement and deferred response finalization remain intact. Managed Local llama retains its verified 4096+ context exception.
- Pool selection state remains a typed sidecar, separate from credentials/operator priority. A downstream mixin implements cursor/count behavior while upstream retains locking, deferred OAuth refresh and borrowed credential protections. Probe selections do not persist rotation.
- Shared HTTP transport retains upstream connection pooling/dual-stack behavior plus cached default SSL context and explicit custom verification.
- Main CLI fork registration/typed error envelopes, single-winner bytecode sweep, and desktop process-table seam now have small downstream owners. Profile preparse keeps upstream new flag/supervisor/SSH handling behind the fork's real-entrypoint-only gate.
- Profile delete uses off-loop execution with typed 409 refusal; atomic writes combine upstream fsync/ownership with Windows retry and explicit newline support.
- Read-only plugin config loading is applied once in upstream's shared helper. Call-time roots and head/auth/shared-character authorities remain distinct.
- Several Windows/test-isolation fixes are now covered by upstream equivalents; retained fork tests follow moved owners. Brief/full tool documentation is updated for batched clarify, batched skill operations and persistent execute_code kernels. No installer was implemented.
- Fork development instructions moved into a linked downstream guide, preserving the call-time home/test-runner contract without restoring the superseded long upstream root guide.

## Remaining

Finish unresolved tool/provider/runtime/config/gateway and test conflicts; audit dependency and compatibility paths; run focused wrapper regressions, contract checks and an isolated real-model probe; update installer design; review full diff/ancestry before ff-only main delivery. Baseline proof remains original-fork-only.

Unmerged paths remaining: 101

- `.github/workflows/tests.yml`
- `agent/conversation_compression.py`
- `agent/pet/generate/atlas.py`
- `agent/prompt_builder.py`
- `agent/shell_hooks.py`
- `agent/skill_commands.py`
- `agent/skill_utils.py`
- `agent/system_prompt.py`
- `agent/transports/codex.py`
- `apps/desktop/e2e/sidebar-states.spec.ts`
- `apps/desktop/src/app/chat/sidebar/session-row-state.test.ts`
- `cli.py`
- `docs/design/profile-builder.md`
- `docs/plans/2026-06-09-003-fix-telegram-stream-overflow-continuations-plan.md`
- `gateway/kanban_watchers.py`
- `gateway/platforms/base.py`
- `gateway/run.py`
- `gateway/slash_commands.py`
- `hermes_cli/commands.py`
- `hermes_cli/dep_ensure.py`
- `hermes_cli/gateway.py`
- `hermes_cli/gateway_windows.py`
- `hermes_cli/kanban.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/mcp_config.py`
- `hermes_cli/plugins.py`
- `hermes_cli/profiles.py`
- `hermes_cli/runtime_provider.py`
- `hermes_cli/skills_hub.py`
- `hermes_cli/status.py`
- `hermes_cli/tools_config.py`
- `hermes_cli/uninstall.py`
- `hermes_cli/update_cmd.py`
- `model_tools.py`
- `plugins/dashboard_auth/basic/__init__.py`
- `plugins/memory/__init__.py`
- `pyproject.toml`
- `scripts/check_subprocess_stdin.py`
- `scripts/run_tests_parallel.py`
- `tests/agent/conftest.py`
- `tests/agent/test_api_call_ttfb.py`
- `tests/agent/test_prompt_builder.py`
- `tests/agent/test_request_assembled_marker.py`
- `tests/agent/test_shell_hooks.py`
- `tests/agent/test_system_prompt.py`
- `tests/agent/test_turn_finalizer_final_response_persistence.py`
- `tests/agent/transports/test_codex_transport.py`
- `tests/cli/test_surrogate_sanitization.py`
- `tests/conftest.py`
- `tests/gateway/test_background_process_notifications.py`
- `tests/gateway/test_feishu.py`
- `tests/gateway/test_update_command.py`
- `tests/hermes_cli/conftest.py`
- `tests/hermes_cli/test_auth_noninteractive.py`
- `tests/hermes_cli/test_commands.py`
- `tests/hermes_cli/test_dashboard_unified_launch.py`
- `tests/hermes_cli/test_doctor.py`
- `tests/hermes_cli/test_harness_providers.py`
- `tests/hermes_cli/test_kanban_db.py`
- `tests/hermes_cli/test_managed_uv.py`
- `tests/hermes_cli/test_nous_inference_url_validation.py`
- `tests/hermes_cli/test_plugins_cmd.py`
- `tests/scripts/test_run_tests_parallel.py`
- `tests/test_hermes_constants.py`
- `tests/test_hermes_state.py`
- `tests/tools/test_approval.py`
- `tests/tools/test_browser_content_none_guard.py`
- `tests/tools/test_execute_code_approval_cluster.py`
- `tests/tools/test_file_operations.py`
- `tests/tools/test_file_tools.py`
- `tests/tools/test_local_env_blocklist.py`
- `tests/tools/test_skills_tool.py`
- `tests/tools/test_terminal_tool.py`
- `tests/tools/test_terminal_tool_requirements.py`
- `tests/tools/test_tool_search.py`
- `tests/tools/test_toolsets.py`
- `tools/approval.py`
- `tools/browser_tool.py`
- `tools/checkpoint_manager.py`
- `tools/credential_files.py`
- `tools/environments/file_sync.py`
- `tools/environments/local.py`
- `tools/file_tools.py`
- `tools/lazy_deps.py`
- `tools/mcp_tool.py`
- `tools/process_registry.py`
- `tools/registry.py`
- `tools/session_search_tool.py`
- `tools/skills_hub.py`
- `tools/skills_sync.py`
- `tools/skills_tool.py`
- `tools/terminal_tool.py`
- `tools/tirith_security.py`
- `tools/tool_search.py`
- `tools/tts_tool.py`
- `tools/vision_tools.py`
- `toolsets.py`
- `tui_gateway/server.py`
- `uv.lock`
- `website/docs/developer-guide/gateway-session-lifecycle.md`
- `website/docs/reference/mcp-config-reference.md`
