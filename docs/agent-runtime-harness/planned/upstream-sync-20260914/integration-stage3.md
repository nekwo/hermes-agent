# Integration checkpoint 3 — 2026-09-15

IN PROGRESS. Candidate has not run combined regression tests and must not be used as a runtime.

Cumulative patch relative to pinned upstream. Keep the active merge worktree; do not abort/reset it.

## Resolutions

- Persona Codex header cache scope is a separate parameter from upstream logical body-cache scope. Original content-addressed body behavior and bounded matching persona headers remain; normal upstream sessions retain upstream routing. Final routing receipts use redacted fingerprints from a downstream owner. Transport tests still need migration for the now explicit persona parameter.
- Native compression keeps upstream transactional publish/lease fencing while inheriting persona root, instance and source metadata through a downstream helper. Managed Local llama context exception also checks the same endpoint.
- Shared skill resolution, provenance, collision detection, per-turn policy and caches moved into agent_runtime/skill_resolution.py. The upstream parser, walkers and trusted-project policy remain authoritative for their existing responsibilities. Shared canonical roots are retained, and normalized wire paths use POSIX separators.
- Windows hooks use upstream's equivalent shared tokenizer, retaining extensionless path detection. Required skill preloads survive upstream's shared loader refactor.
- Provider resolution retains upstream's ladder and uses a context-local rotation-write scope across built-in and custom pool selection. Readiness functions moved downstream. No provider calls were made as verification.
- Upstream live profile accessors replace the equivalent fork skill-sync implementation. Existing Windows checkpoint cleanup, skill bundle byte/path fidelity, external skill linking, and explicit uninstall warnings are preserved at upstream's new owners. Dashboard basic config uses the already-reconciled readonly/deepcopy helper.
- Scratch persona transcripts remain excluded from session recall; full/brief references retain current upstream shape. Tirith keeps the shared fork config authority and unsupported-platform receipt.

## Proof and remaining work

AST parsing only for the files in the manifest; no behavioral PASS claimed. Previous 394 tests and real llama roundtrip were ORIGINAL FORK baseline only. Finish remaining conflicts, migrate preserved tests to new owners, run focused wrapper regressions and isolated real probe, finalize installer contract review, then review and deliver the history-preserving merge through ff-only main advancement.

Reconciled manifest paths: 138. Patch SHA-256: `ba26daebf7586a0b8df29a638d4c7f83e99d32a9673908088b07e4d2513f6e88`. Unmerged paths remaining: 86.

- `.github/workflows/tests.yml`
- `agent/pet/generate/atlas.py`
- `agent/prompt_builder.py`
- `agent/system_prompt.py`
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
- `hermes_cli/status.py`
- `hermes_cli/tools_config.py`
- `hermes_cli/update_cmd.py`
- `model_tools.py`
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
- `tools/credential_files.py`
- `tools/environments/file_sync.py`
- `tools/environments/local.py`
- `tools/file_tools.py`
- `tools/lazy_deps.py`
- `tools/mcp_tool.py`
- `tools/process_registry.py`
- `tools/registry.py`
- `tools/skills_tool.py`
- `tools/terminal_tool.py`
- `tools/tool_search.py`
- `tools/tts_tool.py`
- `tools/vision_tools.py`
- `toolsets.py`
- `tui_gateway/server.py`
- `uv.lock`
- `website/docs/developer-guide/gateway-session-lifecycle.md`
- `website/docs/reference/mcp-config-reference.md`
