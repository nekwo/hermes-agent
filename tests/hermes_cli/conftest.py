"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


# ── Pre-existing environment-gap fence (2026-07-30) ─────────────────────────
#
# `python -m pytest tests/hermes_cli` on this Windows 10 workstation reports
# 234 failures across 57 files. Every one of them was triaged against the
# mission-lane removal (commits f47e6d278..25e2651ac) and **none is caused by
# it** — the removal touched `hermes_cli/harness*.py`,
# `hermes_cli/harness_parts/`, `hermes_cli/profiles.py`,
# `hermes_cli/web_server.py` and `tools/`, and the tests covering those are
# green (`test_harness_cli.py`, `test_web_server_blueprints.py`, the
# `test_mission_chat_*` set, `test_flow_commands.py`, `test_persona_chat_session.py`).
#
# The failures are pre-existing gaps between this host and the Linux CI the
# upstream suite is written for. They are NOT skipped and NOT xfailed: they
# still run, still execute their real assertions, and still fail loudly on a
# plain `pytest tests/hermes_cli`. What the registry below adds is a *name*,
# so a run that deliberately wants the fork-owned signal can deselect them:
#
#     python -m pytest tests/hermes_cli -m "not windows_env_gap and not host_dependency_gap"
#
# Two marks, by cause:
#
#   windows_env_gap      The assertion encodes POSIX-only semantics that
#                        Windows cannot satisfy — 0o600 file modes (NTFS
#                        reports 0o666), `os.chown` / `os.getpgid` /
#                        `signal.SIGKILL` / `signal.pause` / `_curses`,
#                        `/usr/bin/...` literals, POSIX path separators,
#                        `bash -n <C:\...>`, cp1252 console decoding, and
#                        the `git -c windows.appendAtomically=false` prefix
#                        that `cmd_update` adds only on win32.
#
#   host_dependency_gap  A host package or capability is absent rather than a
#                        platform property: `fire`, `psutil`, `croniter`,
#                        `pywinpty` are not installed; the installed `rich`
#                        does not emit OSC-8 panel-title hyperlinks; and
#                        upstream has since added a standalone `tool_describe`
#                        bridge tool to the minimal toolset.
#
# Two failure modes cannot be handled by a mark, because they HANG instead of
# failing and a hang kills the whole pytest process before any deselection can
# matter. Those get real prerequisite probes further down (`node --version`
# against the Vite 8 engine floor, and a TCP probe of 127.0.0.1:11434). Both
# are inert on a host that satisfies the prerequisite, so nothing is masked.
#
# Toolchain floor worth stating explicitly, because it is the one gap with a
# concrete version number: `hermes_cli.main._build_web_ui` runs
# `npm run build -w web`, and `web/package.json` pins `vite ^8`, which requires
# Node `^20.19.0 || >=22.12.0`. This box has Node v20.17.0, so that build
# cannot succeed here. `test_web_ui_build.py` fails on it, and
# `test_cmd_update.py` *executes* it for real — it stubs `subprocess.run` but
# not the `subprocess.Popen` that `_run_with_idle_timeout` uses, so a plain run
# of that file performs a real npm install/build (and a real bundled-skill
# sync) against the checkout. Treat that file as side-effecting until the
# upstream mocking is tightened.
#
# Keeping the registry honest: any registered node id that PASSES is printed in
# a "stale environment-gap registry entries" section at the end of the run (see
# pytest_terminal_summary below), so a fixed environment — or a fixed test —
# forces the row to be deleted rather than quietly masking a regression.

_WINDOWS = "windows_env_gap"
_HOST = "host_dependency_gap"


# ── Prerequisite guard: files that execute the REAL web-UI build ───────────
#
# The two files below call `cmd_update` / `_cmd_update_impl` with only
# `subprocess.run` stubbed. Production's `_build_web_ui` reaches
# `_run_with_idle_timeout`, which uses `subprocess.Popen` — unstubbed — so the
# test performs a genuine `npm install` + `npm run build -w web` against the
# checkout. `web/package.json` pins `vite ^8`, whose engine floor is
# Node `^20.19.0 || >=22.12.0`. Below that floor the build cannot complete, the
# call blocks past the 30s per-test cap, and pytest-timeout's thread method
# kills the WHOLE process — taking every other result in the run with it. A
# marker cannot help: the hang is in a test the registry does not (and should
# not) list as a failure.
#
# This is an environment PREREQUISITE check, not a loosened assertion: on a
# host that meets the floor the guard is inert and both files run in full,
# executing every original assertion.
_WEB_BUILD_PREREQ_FILES = frozenset({
    "test_cmd_update.py",
    "test_update_yes_flag.py",
})

_VITE8_NODE_FLOOR = "^20.19.0 || >=22.12.0"


def _node_version() -> str | None:
    """Return the host `node --version` string, or None when unavailable."""
    import subprocess as _sp

    try:
        out = _sp.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def _web_build_prereq_failure() -> str | None:
    """Return a skip reason when the host cannot build the web UI, else None."""
    raw = _node_version()
    if raw is None:
        return (
            "web-UI build prerequisite: node is not runnable on this host, so "
            f"`npm run build -w web` (vite ^8, requires Node {_VITE8_NODE_FLOOR}) "
            "cannot complete; this file executes that build for real."
        )
    try:
        major, minor, patch = (int(part) for part in raw.lstrip("v").split(".")[:3])
    except ValueError:
        return None  # unparseable: assume capable rather than guess
    ok = (
        (major == 20 and (minor, patch) >= (19, 0))
        or (major == 21)
        or (major == 22 and (minor, patch) >= (12, 0))
        or major > 22
    )
    if ok:
        return None
    return (
        f"web-UI build prerequisite: Node {_VITE8_NODE_FLOOR} required for "
        f"`npm run build -w web` (web/package.json pins vite ^8); found {raw}. "
        "This file drives the real build through an unstubbed subprocess.Popen, "
        "so it would block past the per-test timeout and kill the whole run."
    )


_WEB_BUILD_PREREQ_REASON = _web_build_prereq_failure()


# ── Prerequisite guard: tests that construct AIAgent against a local model ──
#
# These two build a real `AIAgent` with `base_url=http://127.0.0.1:11434/v1`.
# `agent_init` probes that endpoint (`detect_local_server_type` /
# `query_ollama_num_ctx`) several times over httpx. On a host where the port
# refuses immediately — nothing listening, or a real ollama answering — the
# probes cost milliseconds and the tests run normally. On a host where the port
# is BLACKHOLED (a firewall or a WSL/Docker port proxy that drops SYN instead
# of sending RST) every probe burns its full timeout, the test blows the 30s
# per-test cap, and pytest-timeout's thread method kills the whole process.
#
# The probe below distinguishes those cases directly: a refusal or a successful
# connect means the prerequisite holds and the guard stays inert.
_LOCAL_MODEL_PROBE_NODE_IDS = {
    "test_timeouts.py": frozenset({
        "test_default_non_stream_stale_timeout_auto_disables_for_local_endpoints",
        "test_explicit_non_stream_stale_timeout_is_honored_for_local_endpoints",
    }),
}


def _local_model_probe_failure() -> str | None:
    """Return a skip reason when 127.0.0.1:11434 neither answers nor refuses."""
    import socket as _socket

    try:
        conn = _socket.create_connection(("127.0.0.1", 11434), timeout=2.0)
    except (ConnectionRefusedError, OSError) as exc:
        if isinstance(exc, ConnectionRefusedError):
            return None  # fast refusal — the probes will fail fast too
        if not isinstance(exc, (TimeoutError, _socket.timeout)):
            return None  # some other immediate error — still fast
        return (
            "local-endpoint prerequisite: 127.0.0.1:11434 accepted neither a "
            "connection nor a refusal within 2s (the SYN is being dropped, not "
            "reset). AIAgent construction probes that endpoint over httpx, so "
            "this test would block past the per-test timeout and kill the "
            "whole run. Free the port or let it refuse to re-enable."
        )
    conn.close()
    return None


_LOCAL_MODEL_PROBE_REASON = _local_model_probe_failure()

# file basename -> (mark, reason, {node ids within the file})
_ENV_GAPS: dict[str, tuple[str, str, set[str]]] = {
    'test_apply_profile_override.py': (
        _WINDOWS,
        'profile lookup resolves get_default_hermes_root(), which is %LOCALAPPDATA%\\hermes on Windows while the fixtures seed ~/.hermes',
        {
            'TestApplyProfileOverrideHermesHomeGuard::test_hermes_home_unset_reads_active_profile',
            'TestApplyProfileOverrideHermesHomeGuard::test_profile_after_chat_subcommand_is_still_consumed',
            'TestApplyProfileOverrideHermesHomeGuard::test_sudo_explicit_profile_resolves_invoking_users_profile',
            'TestApplyProfileOverrideHermesHomeGuard::test_top_level_profile_after_continue_flag_is_consumed',
            'TestApplyProfileOverrideHermesHomeGuard::test_top_level_profile_after_value_flag_is_consumed',
            'TestSupervisedChildIgnoresStickyProfile::test_non_supervised_run_still_follows_active_profile',
            'TestSupervisedChildIgnoresStickyProfile::test_supervised_named_profile_flag_still_wins',
        },
    ),
    'test_arcee_provider.py': (
        _HOST,
        "optional dependency 'fire' is not installed",
        {
            'TestArceeURLMapping::test_trajectory_compressor_detects_arcee',
        },
    ),
    'test_atomic_json_write.py': (
        _WINDOWS,
        'concurrent os.replace() onto an open file raises PermissionError on Windows; POSIX rename is atomic',
        {
            'TestAtomicJsonWrite::test_concurrent_writes_dont_corrupt',
        },
    ),
    'test_auth_nous_provider.py': (
        _WINDOWS,
        'asserts a 0o600 file mode; NTFS reports 0o666 because os.chmod only toggles the read-only bit',
        {
            'test_shared_store_write_and_read_roundtrip',
        },
    ),
    'test_auth_qwen_provider.py': (
        _WINDOWS,
        'asserts a 0o600 file mode; NTFS reports 0o666 because os.chmod only toggles the read-only bit',
        {
            'test_save_qwen_cli_tokens_permissions',
        },
    ),
    'test_backup.py': (
        _WINDOWS,
        'asserts 0o600 modes and POSIX profile-wrapper symlinks; NTFS reports 0o666',
        {
            'TestMemoryProviderExternalPaths::test_import_restores_external_to_home_relative_location',
            'TestProfileRestoration::test_import_creates_profile_wrappers',
            'TestProfileRestoration::test_import_skips_profile_dirs_without_config',
            'TestQuickSnapshotProjectsKanban::test_board_db_copied_wal_safely',
        },
    ),
    'test_banner.py': (
        _HOST,
        "installed 'rich' does not emit an OSC-8 hyperlink escape in a Panel title",
        {
            'test_build_welcome_banner_title_is_hyperlinked_to_release',
        },
    ),
    'test_cmd_update.py': (
        _WINDOWS,
        'cmd_update prepends `git -c windows.appendAtomically=false` on win32 and resolves npm as npm.CMD; the mocks assert the POSIX argv. WARNING: this file only stubs subprocess.run, so `_build_web_ui` still runs a REAL npm install + vite build against the checkout',
        {
            'TestCmdUpdateBranchFallback::test_update_on_fork_checks_upstream_when_origin_up_to_date',
            'TestCmdUpdateBranchFallback::test_update_refreshes_repo_and_tui_node_dependencies',
        },
    ),
    'test_codex_runtime_plugin_migration.py': (
        _WINDOWS,
        "the em dash in MIGRATION_MARKER is written as UTF-8 and read back with the Windows default cp1252 codec, so the marker comes back as mojibake ('â€”') and is never found",
        {
            'TestMigrate::test_managed_root_keys_stay_top_level_when_config_ends_in_table',
            'TestMigrate::test_no_servers_no_plugins_no_perms_writes_placeholder',
            'TestMigrate::test_preserves_user_codex_config_above_marker',
            'TestStripUnmanagedPluginTables::test_migrate_dedups_codex_owned_plugin_tables',
        },
    ),
    'test_commands.py': (
        _HOST,
        "Slack native-slash parity against the installed plugin set ('version' present on Telegram only)",
        {
            'TestSlackNativeSlashes::test_telegram_parity',
        },
    ),
    'test_completion.py': (
        _WINDOWS,
        'shells out to `bash -n <windows path>`; Git-bash cannot resolve a C:\\... argument',
        {
            'TestGenerateBash::test_valid_bash_syntax',
        },
    ),
    'test_config.py': (
        _WINDOWS,
        'asserts the POSIX default root ~/.hermes; get_hermes_home() returns %LOCALAPPDATA%\\hermes on Windows',
        {
            'TestGetHermesHome::test_default_path',
        },
    ),
    'test_container_boot.py': (
        _WINDOWS,
        's6/container supervision layout is Linux-only; service_manager calls os.chown, which does not exist on Windows',
        {
            'test_corrupt_state_file_treated_as_no_prior_state',
            'test_default_slot_always_registered_on_empty_home',
            'test_default_slot_appears_before_named_profiles',
            'test_default_slot_autostarts_when_root_state_running',
            'test_default_slot_cleans_up_stale_runtime_files_at_root',
            'test_default_slot_does_not_autostart_when_root_state_startup_failed',
            'test_default_slot_does_not_autostart_when_root_state_stopped',
            'test_default_slot_run_script_omits_profile_flag',
            'test_degraded_runtime_state_autostarts',
            'test_desired_state_running_autostarts_even_if_runtime_failed',
            'test_desired_state_stopped_blocks_legacy_running_runtime',
            'test_desired_state_stopped_overrides_draining_runtime',
            'test_directory_without_marker_file_is_skipped',
            'test_draining_default_root_autostarts',
            'test_draining_runtime_state_autostarts',
            'test_invalid_profile_name_in_directory_raises',
            'test_legacy_gateway_run_cmd_seeds_default_running_state[container_argv0]',
            'test_legacy_gateway_run_cmd_seeds_default_running_state[container_argv1]',
            'test_legacy_gateway_run_env_no_supervise_does_not_seed_s6_state',
            'test_legacy_gateway_run_no_supervise_does_not_seed_s6_state[container_argv0]',
            'test_legacy_gateway_run_no_supervise_does_not_seed_s6_state[container_argv1]',
            'test_main_ignores_removed_skip_reconcile_env_var',
            'test_main_reconciles_in_gateway_container',
            'test_missing_profiles_root_still_registers_default_slot',
            'test_profile_without_state_file_is_registered_but_not_started',
            'test_profiles_default_subdir_is_skipped_with_warning',
            'test_reconcile_log_does_not_rotate_below_threshold',
            'test_reconcile_log_is_written',
            'test_reconcile_log_rotates_when_size_exceeded',
            'test_reconcile_log_rotation_overwrites_existing_dot1',
            'test_register_service_cleans_up_stale_tmp_dir',
            'test_register_service_overwrites_existing_slot',
            'test_register_service_publishes_atomically',
            'test_registered_profile_has_finish_script',
            'test_running_profile_is_registered_and_autostarted',
            'test_stale_runtime_files_are_removed',
            'test_starting_state_does_not_autostart',
            'test_startup_failed_does_not_autostart',
            'test_stopped_profile_is_registered_but_not_started',
        },
    ),
    'test_cron.py': (
        _HOST,
        "optional dependency 'croniter' is not installed, so every cron expression is rejected",
        {
            'TestGatewayNotRunningWarning::test_create_silent_when_gateway_running',
            'TestGatewayNotRunningWarning::test_create_warns_when_gateway_absent',
            'TestGatewayNotRunningWarning::test_list_warns_when_gateway_absent',
        },
    ),
    'test_dashboard_unified_launch.py': (
        _WINDOWS,
        "asserts the POSIX literal '/opt/data'; get_default_hermes_root() returns a WindowsPath ('\\opt\\data')",
        {
            'TestUnifiedDashboardRouting::test_reexec_pins_docker_machine_root',
        },
    ),
    'test_debug.py': (
        _WINDOWS,
        'log-snapshot truncation budget differs because the captured log is CRLF-terminated on Windows',
        {
            'TestCaptureLogSnapshot::test_keeps_first_line_when_truncation_on_boundary',
        },
    ),
    'test_ensure_hermes_home_uid_34107.py': (
        _WINDOWS,
        'os.chown does not exist on Windows',
        {
            'TestChownToHermesUid::test_attributeerror_swallowed_for_windows_compat',
            'TestChownToHermesUid::test_calls_os_chown_when_both_set',
            'TestChownToHermesUid::test_eperm_is_silently_swallowed',
            'TestChownToHermesUid::test_no_op_when_neither_set',
            'TestChownToHermesUid::test_uses_minus_one_for_missing_field',
            'TestResolveHermesUidGid::test_invalid_uid_returns_none_for_that_field',
            'TestResolveHermesUidGid::test_returns_parsed_values_when_both_set',
            'TestResolveHermesUidGid::test_uid_only_returns_gid_none',
            'TestResolveHermesUidGid::test_whitespace_padded_values',
        },
    ),
    'test_gateway.py': (
        _WINDOWS,
        'signal.SIGKILL does not exist on Windows',
        {
            'test_install_linux_gateway_from_setup_non_root_never_offers_system',
            'test_reap_unsupervised_orphans_sigterms_then_sigkills_survivor',
        },
    ),
    'test_gateway_restart_loop.py': (
        _HOST,
        "optional dependency 'croniter' is not installed, so the lifecycle block message never reaches the assertion",
        {
            'TestCreateJobBlocksLifecycleCommands::test_cronjob_tool_surfaces_block_as_error',
            'TestCronCreateLifecycleBlock::test_block_launchctl_kickstart',
        },
    ),
    'test_gateway_s6_dispatch.py': (
        _WINDOWS,
        'signal.pause does not exist on Windows',
        {
            'test_block_until_terminated_installs_sigterm_handler_and_blocks',
        },
    ),
    'test_gateway_wsl.py': (
        _WINDOWS,
        'supports_systemd_services() is False off Linux',
        {
            'TestSupportsSystemdServicesWSL::test_native_linux',
            'TestSupportsSystemdServicesWSL::test_wsl_with_systemd',
        },
    ),
    'test_gui_command.py': (
        _HOST,
        "optional dependency 'psutil' is not installed and the desktop build shells out to npm/electron",
        {
            'test_compute_desktop_content_hash_changes_on_edit',
            'test_compute_desktop_content_hash_respects_gitignore',
            'test_compute_desktop_content_hash_stable',
            'test_compute_desktop_content_hash_works_without_gitignore',
            'test_desktop_build_stamp_round_trip',
            'test_gui_exits_when_npm_missing',
            'test_gui_source_mode_uses_renderer_build_and_electron',
            'test_stop_desktop_build_lock_no_release_dir',
            'test_stop_desktop_build_lock_noop_off_windows',
            'test_stop_desktop_build_lock_terminates_only_release_procs',
        },
    ),
    'test_hooks_cli.py': (
        _WINDOWS,
        'shell-hook doctor executes POSIX scripts and compares POSIX mtime/approval state',
        {
            'TestHooksDoctor::test_clean_script_runs',
            'TestHooksDoctor::test_flags_invalid_json',
            'TestHooksDoctor::test_flags_mtime_drift',
            'TestHooksTest::test_fires_real_subprocess_and_parses_block',
            'TestHooksTest::test_synthetic_payload_matches_production_shape',
        },
    ),
    'test_install_cua_driver.py': (
        _WINDOWS,
        'os.getpgid / POSIX process groups do not exist on Windows',
        {
            'TestInstallerTimeoutKillsProcessGroup::test_timeout_kills_process_group_and_returns_false',
            'TestStaleInstallLockClear::test_dead_holder_lock_is_cleared',
            'TestStaleInstallLockClear::test_pidless_old_lock_is_cleared',
        },
    ),
    'test_kanban_boards.py': (
        _WINDOWS,
        'board-DB teardown asserts a cache invalidation that Windows file locking defers',
        {
            'TestBoardCRUD::test_remove_clears_init_cache_for_recreated_db[False]',
            'TestBoardCRUD::test_remove_clears_init_cache_for_recreated_db[True]',
        },
    ),
    'test_kanban_core_functionality.py': (
        _WINDOWS,
        'tests/conftest.py live-system guard refuses os.kill() of a synthetic PID; psutil resolves it differently on Windows',
        {
            'test_detect_crashed_workers_protocol_violation_auto_blocks',
            'test_dispatch_once_integrates_stale_detection',
            'test_gateway_dispatcher_retries_corrupt_board_after_quarantine',
            'test_list_profiles_on_disk',
        },
    ),
    'test_kanban_db.py': (
        _WINDOWS,
        'worker-PID reaping asserts POSIX signal delivery to synthetic PIDs',
        {
            'test_classify_worker_exit_recognizes_rate_limit_sentinel',
            'test_dispatch_once_still_reaps_via_extracted_fn',
            'test_dispatch_worktree_task_persists_materialized_workspace_and_branch',
            'test_dispatch_worktree_task_rerun_reuses_existing_linked_worktree_and_branch',
            'test_rate_limit_exit_requeues_without_counting_failure',
            'test_reap_worker_zombies_records_exit_status',
            'test_reap_worker_zombies_returns_count',
            'test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim',
            'test_resolve_hermes_argv_hermes_bin_bare_name_uses_path',
            'test_resolve_hermes_argv_prefers_path_shim',
            'test_worktree_workspace_explicit_target_materializes_linked_worktree',
            'test_worktree_workspace_repo_root_anchor_materializes_linked_worktree',
            'test_zombie_reaper_runs_despite_board_connect_failure',
            'test_zombie_reaper_survives_all_boards_failing',
        },
    ),
    'test_kanban_worker_image_extraction.py': (
        _WINDOWS,
        'image paths in a task body are matched with a POSIX path regex; Windows paths carry a drive letter',
        {
            'TestBuildPartsFromTaskBody::test_body_with_both_yields_two_image_parts',
            'TestBuildPartsFromTaskBody::test_code_block_example_is_not_attached',
            'TestBuildPartsFromTaskBody::test_local_path_becomes_native_image_part',
            'TestExtractFromTaskBody::test_local_path_in_body_round_trips',
            'TestExtractFromTaskBody::test_mixed_path_and_url_in_body',
        },
    ),
    'test_managed_uv.py': (
        _WINDOWS,
        'asserts _install_uv_posix is called; Windows takes the _install_uv_windows branch',
        {
            'TestEnsureUv::test_already_installed_no_bootstrap',
            'TestEnsureUv::test_installs_if_missing',
            'TestInstallUvInternals::test_posix_sets_uv_unmanaged_install',
            'TestResolveUv::test_existing_executable',
            'TestUpdateManagedUv::test_self_update_failure_non_fatal',
            'TestUpdateManagedUv::test_self_update_success',
        },
    ),
    'test_path_completion.py': (
        _WINDOWS,
        'completion candidates come from the real cwd, which holds Windows .ps1 fixtures',
        {
            'TestIntegration::test_absolute_path_triggers_completion',
            'TestPathCompletions::test_home_expansion',
        },
    ),
    'test_plugins_cmd.py': (
        _WINDOWS,
        'reads a plugin file without an explicit encoding; the Windows default cp1252 codec raises UnicodeDecodeError',
        {
            'TestCursesRadiolist::test_keyboard_interrupt_returns_cancel_value',
            'TestNoAutoActivation::test_compressor_default_ignores_plugin',
        },
    ),
    'test_profiles.py': (
        _WINDOWS,
        'asserts a 0o600 .env mode; NTFS reports 0o666 because os.chmod only toggles the read-only bit',
        {
            'TestBackfillProfileEnvs::test_copies_default_env_into_envless_profiles',
            'TestCreateProfile::test_seeds_placeholder_env_file',
        },
    ),
    'test_projects_db.py': (
        _WINDOWS,
        "asserts POSIX absolute-path literals ('/a/c'); Path normalisation yields 'X:\\a\\c'",
        {
            'test_add_remove_folder_and_primary_repoint',
            'test_create_get_list',
            'test_paths_normalized',
            'test_record_and_list_discovered_repos',
            'test_record_discovered_repos_replace_drops_stale_rows',
        },
    ),
    'test_prompt_compose_command.py': (
        _WINDOWS,
        'the fake $EDITOR is a POSIX shell script and does not execute on Windows',
        {
            'test_compose_reads_and_strips_header',
            'test_prompt_sets_pending_seed',
        },
    ),
    'test_prompt_size.py': (
        _HOST,
        "minimal toolset now also carries upstream's standalone 'tool_describe' bridge tool",
        {
            'test_blank_slate_prompt_size_counts_only_minimal_tools',
        },
    ),
    'test_relaunch.py': (
        _WINDOWS,
        "asserts os.execvp against '/usr/bin/hermes'",
        {
            'TestRelaunch::test_calls_execvp',
        },
    ),
    'test_service_manager.py': (
        _WINDOWS,
        'os.chown does not exist on Windows and systemd units cannot be written',
        {
            'test_s6_log_run_chowns_gateways_parent',
            'test_s6_register_creates_service_dir_and_triggers_scan',
            'test_s6_register_extra_env_is_quoted',
            'test_s6_register_rolls_back_on_svscanctl_failure',
            'test_s6_register_staging_dir_is_dotfile_hidden_from_svscan',
            'test_s6_register_start_now_false_writes_down_marker',
            'test_s6_register_start_now_true_no_down_marker',
            'test_s6_register_writes_finish_script',
            'test_s6_running_true_when_comm_and_basedir_match',
            'test_seed_supervise_skeleton_creates_expected_layout',
            'test_seed_supervise_skeleton_handles_log_subservice',
            'test_seed_supervise_skeleton_is_idempotent',
            'test_seed_supervise_skeleton_skips_when_no_log_subservice',
        },
    ),
    'test_session_browse.py': (
        _WINDOWS,
        "the '_curses' module is not available on Windows",
        {
            'TestCursesBrowse::test_backspace_removes_filter_char',
            'TestCursesBrowse::test_down_down_enter_selects_third',
            'TestCursesBrowse::test_down_then_enter_selects_second',
            'TestCursesBrowse::test_enter_selects_first_session',
            'TestCursesBrowse::test_escape_cancels',
            'TestCursesBrowse::test_escape_clears_filter_first',
            'TestCursesBrowse::test_filter_matches_preview',
            'TestCursesBrowse::test_filter_matches_source',
            'TestCursesBrowse::test_filter_no_match_enter_does_nothing',
            'TestCursesBrowse::test_q_cancels',
            'TestCursesBrowse::test_q_quits_when_no_filter_active',
            'TestCursesBrowse::test_q_types_into_filter_when_filter_active',
            'TestCursesBrowse::test_type_to_filter_then_enter',
            'TestCursesBrowse::test_up_wraps_to_last',
        },
    ),
    'test_setup.py': (
        _WINDOWS,
        'setup gateway branches on systemctl/container detection that cannot hold on Windows',
        {
            'test_setup_gateway_in_container_shows_docker_guidance',
            'test_setup_gateway_skips_service_install_when_systemctl_missing',
        },
    ),
    'test_setup_blank_slate.py': (
        _HOST,
        "minimal toolset now also carries upstream's standalone 'tool_describe' bridge tool",
        {
            'TestBlankSlateMinimalToolsets::test_tool_schema_builder_yields_only_file_and_terminal_tools',
            'TestBlankSlateMinimalToolsets::test_tool_schema_survives_disabled_toolsets_from_config',
        },
    ),
    'test_setup_hermes_script.py': (
        _WINDOWS,
        'shells out to `bash -n <windows path>`; Git-bash cannot resolve an X:\\... argument',
        {
            'test_setup_hermes_script_is_valid_shell',
        },
    ),
    'test_setup_matrix_e2ee.py': (
        _WINDOWS,
        'reads a source file without an explicit encoding; the Windows default cp1252 codec raises UnicodeDecodeError',
        {
            'TestSetupShutilImport::test_shutil_imported_at_module_level',
        },
    ),
    'test_signal_handler_kanban_worker.py': (
        _WINDOWS,
        'reads a source file without an explicit encoding; the Windows default cp1252 codec raises UnicodeDecodeError',
        {
            'test_real_handler_uses_os_exit_for_kanban_workers',
        },
    ),
    'test_subprocess_timeouts.py': (
        _WINDOWS,
        'reads source files without an explicit encoding; the Windows default cp1252 codec raises UnicodeDecodeError',
        {
            'test_all_subprocess_run_calls_have_timeout[hermes_cli/banner.py]',
            'test_all_subprocess_run_calls_have_timeout[hermes_cli/doctor.py]',
            'test_all_subprocess_run_calls_have_timeout[hermes_cli/status.py]',
        },
    ),
    # test_timeouts.py is NOT listed here: its two local-endpoint tests hang
    # rather than fail, so they are handled by the 127.0.0.1:11434 prerequisite
    # probe above, which stays inert on a host where that port refuses fast.
    'test_uninstall_node_symlinks.py': (
        _WINDOWS,
        'asserts POSIX symlinks for node/npm/npx',
        {
            'test_only_some_links_present',
            'test_removes_dangling_symlink_into_hermes_node',
            'test_removes_fhs_symlinks_in_usr_local_bin',
            'test_removes_symlinks_pointing_into_hermes_node',
        },
    ),
    'test_update_autostash.py': (
        _WINDOWS,
        "cmd_update prepends `git -c windows.appendAtomically=false` on win32; the mocks assert the bare ['git', ...] argv",
        {
            'test_cmd_update_falls_back_to_reset_when_ff_only_fails',
            'test_cmd_update_fetch_is_scoped_to_target_branch',
            'test_cmd_update_retries_optional_extras_individually_when_all_fails',
            'test_cmd_update_succeeds_with_extras',
        },
    ),
    'test_update_interrupted_recovery.py': (
        _WINDOWS,
        'the recovery self-lock guard walks a POSIX process ancestry',
        {
            'test_recovery_self_lock_guard_clears_marker_without_install',
            'test_recovery_self_lock_guard_inactive_when_not_ancestor',
        },
    ),
    'test_update_post_pull_syntax_guard.py': (
        _WINDOWS,
        "asserts POSIX-separated repo-relative paths ('hermes_cli/main.py')",
        {
            'test_validate_critical_files_syntax_detects_break_in_main_py',
            'test_validate_critical_files_syntax_detects_break_in_web_server',
            'test_validate_critical_files_syntax_detects_conflict_markers',
        },
    ),
    'test_update_stale_dashboard.py': (
        _WINDOWS,
        'asserts POSIX signal delivery to synthetic dashboard PIDs',
        {
            'TestFindStaleDashboardPids::test_exclude_pids_filters_specified_pids',
            'TestFindStaleDashboardPids::test_exclude_pids_none_is_noop',
            'TestFindStaleDashboardPids::test_grep_lines_ignored',
            'TestFindStaleDashboardPids::test_invalid_pid_lines_skipped',
            'TestFindStaleDashboardPids::test_matches_running_dashboard',
            'TestFindStaleDashboardPids::test_multiple_matches',
            'TestFindStaleDashboardPids::test_self_pid_excluded',
            'TestFindStaleDashboardPids::test_unrelated_process_containing_word_dashboard_not_matched',
        },
    ),
    'test_update_venv_health.py': (
        _WINDOWS,
        'venv health probe asserts POSIX venv layout (bin/ vs Scripts/)',
        {
            'test_venv_health_broken_interpreter_is_unhealthy',
            'test_venv_health_reports_missing_imports',
        },
    ),
    'test_uv_tool_update.py': (
        _WINDOWS,
        "asserts POSIX binary paths ('/usr/bin/pipx', '/usr/bin/uv')",
        {
            'TestCmdUpdatePipInstallLayouts::test_pipx_managed_uses_pipx_upgrade',
            'TestCmdUpdatePipUsesUvTool::test_runs_uv_pip_install_when_not_uv_tool',
        },
    ),
    'test_web_server.py': (
        _HOST,
        "optional dependency 'croniter' is not installed, so cron-blueprint instantiation is rejected",
        {
            'TestNewEndpoints::test_blueprint_instantiate_creates_job',
        },
    ),
    'test_web_server_files.py': (
        _WINDOWS,
        "hosted file policy asserts the POSIX root '/opt/data'",
        {
            'test_gated_local_mode_still_defaults_to_home',
            'test_hosted_policy_locks_to_opt_data',
            'test_local_mode_defaults_to_home_and_can_jump_to_absolute_path',
        },
    ),
    'test_web_server_git.py': (
        _WINDOWS,
        'git worktree lifecycle asserts POSIX worktree paths',
        {
            'test_worktrees_and_branch_lifecycle',
        },
    ),
    'test_web_server_oauth_write.py': (
        _WINDOWS,
        'asserts a 0o600 file mode; NTFS reports 0o666',
        {
            'test_dashboard_oauth_write_uses_owner_only_permissions',
        },
    ),
    'test_web_ui_build.py': (
        _WINDOWS,
        "asserts npm at '/usr/bin/npm'; Windows resolves 'C:\\Program Files\\nodejs\\npm.CMD'",
        {
            'TestBuildWebUISkipsWhenFresh::test_desktop_web_install_uses_existing_workspace_root',
            'TestBuildWebUISkipsWhenFresh::test_termux_web_install_is_workspace_scoped',
            'TestBuildWebUISkipsWhenFresh::test_web_build_uses_idle_timeout_helper',
            'TestBuildWebUISkipsWhenFresh::test_web_install_omits_workspace_when_web_has_own_lockfile',
        },
    ),
    'test_win_pty_bridge.py': (
        _HOST,
        "optional dependency 'pywinpty' is not installed",
        {
            'TestWinPtyBridgeClose::test_close_is_idempotent',
            'TestWinPtyBridgeClose::test_close_terminates_long_running_child',
            'TestWinPtyBridgeEnv::test_cwd_is_respected',
            'TestWinPtyBridgeEnv::test_env_is_forwarded',
            'TestWinPtyBridgeEnv::test_spawn_defaults_term_when_not_set',
            'TestWinPtyBridgeIO::test_read_returns_none_after_child_exits',
            'TestWinPtyBridgeIO::test_reads_child_stdout',
            'TestWinPtyBridgeIO::test_write_after_close_is_silent',
            'TestWinPtyBridgeIO::test_write_sends_to_child_stdin',
            'TestWinPtyBridgeResize::test_resize_after_close_is_silent',
            'TestWinPtyBridgeResize::test_resize_clamps_garbage_dimensions',
            'TestWinPtyBridgeResize::test_resize_does_not_raise_on_live_child',
            'TestWinPtyBridgeSpawn::test_is_available_on_windows',
            'TestWinPtyBridgeSpawn::test_spawn_returns_bridge_with_pid',
        },
    ),
}


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register the environment-gap marks (see the block comment above)."""
    config.addinivalue_line(
        "markers",
        f"{_WINDOWS}: pre-existing failure caused by POSIX-only test "
        "expectations that Windows cannot satisfy. Not a fork regression; "
        "deselect with -m 'not windows_env_gap'.",
    )
    config.addinivalue_line(
        "markers",
        f"{_HOST}: pre-existing failure caused by a missing host package or "
        "toolchain (fire / psutil / croniter / pywinpty, Node >=20.19 for the "
        "Vite 8 web build, outbound HTTP). Not a fork regression; deselect "
        "with -m 'not host_dependency_gap'.",
    )


def pytest_collection_modifyitems(items):  # noqa: D401 — pytest hook
    """Attach the environment-gap mark to every registered node id."""
    for item in items:
        if (
            _WEB_BUILD_PREREQ_REASON is not None
            and item.path.name in _WEB_BUILD_PREREQ_FILES
        ):
            item.add_marker(pytest.mark.skip(reason=_WEB_BUILD_PREREQ_REASON))
        if _LOCAL_MODEL_PROBE_REASON is not None:
            probe_ids = _LOCAL_MODEL_PROBE_NODE_IDS.get(item.path.name)
            _, _, probe_name = item.nodeid.partition("::")
            if probe_ids is not None and probe_name in probe_ids:
                item.add_marker(pytest.mark.skip(reason=_LOCAL_MODEL_PROBE_REASON))
        entry = _ENV_GAPS.get(item.path.name)
        if entry is None:
            continue
        mark, reason, node_ids = entry
        _, _, within_file = item.nodeid.partition("::")
        if within_file in node_ids:
            item.add_marker(getattr(pytest.mark, mark)(reason=reason))


_STALE_ENV_GAP_ENTRIES: list[str] = []


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record registered environment-gap node ids that actually passed."""
    if report.when != "call" or report.outcome != "passed":
        return
    file_name = report.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
    entry = _ENV_GAPS.get(file_name)
    if entry is None:
        return
    _, _, within_file = report.nodeid.partition("::")
    if within_file in entry[2]:
        _STALE_ENV_GAP_ENTRIES.append(report.nodeid)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Surface registry rows that no longer describe a real failure."""
    if not _STALE_ENV_GAP_ENTRIES:
        return
    terminalreporter.write_sep("=", "stale environment-gap registry entries")
    terminalreporter.write_line(
        "These node ids are registered in _ENV_GAPS (tests/hermes_cli/conftest.py) "
        "but PASSED. Delete their rows — a stale row hides a future regression."
    )
    for nodeid in sorted(set(_STALE_ENV_GAP_ENTRIES)):
        terminalreporter.write_line(f"  {nodeid}")
