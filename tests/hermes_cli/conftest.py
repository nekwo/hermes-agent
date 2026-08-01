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
#                        platform property: `croniter`, `pywinpty`, `pathspec`
#                        are not installed (all three are declared project
#                        dependencies, so this is an install gap on THIS box,
#                        not an optional extra); the installed `rich` does not
#                        emit OSC-8 panel-title hyperlinks; upstream has since
#                        added a standalone `tool_describe` bridge tool to the
#                        minimal toolset; and the host's git 2.31.1 does not
#                        honour `--ignore-cr-at-eol` for `--name-only` output.
#
#                        `fire` and `psutil` USED to be in that list. They were
#                        installed on the ambient interpreter on 2026-08-01
#                        (ledger item 7, RULED EXECUTE) and every row naming
#                        them retired — three groups here, and more in the
#                        gateway/tools registries.
#
# ── 2026-07-31 upstream-sync sweep ─────────────────────────────────────────
#
# The `upstream/main` merge (b9721809e) replayed this registry against a suite
# that upstream had pruned hard (prune waves 1-2: 46,820 -> 19,757 test
# functions). Two things were done in one pass:
#
#   * every registry row whose node id no longer exists was DELETED (163 rows
#     across 40 files, plus the whole `test_uv_tool_update.py` file, which
#     upstream removed). An orphaned row marks nothing, so it silently stops
#     being a fence while still reading like one;
#   * four rows the stale detector reported as PASSING were deleted too
#     (test_dashboard_unified_launch, test_gateway_wsl, test_install_cua_driver,
#     test_kanban_db).
#
# The rows ADDED in that same pass are all merge-delta failures that were
# triaged individually to a real host/platform cause — never to hide a
# regression. The one genuine merge-resolution defect found in this area
# (`hermes_cli/dep_ensure.py` lost `import os` when the merge took upstream's
# import block over the fork's `ensure_git_bash` body) was FIXED in the code,
# not fenced.
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

# file basename -> [(mark, reason, {node ids within the file}), ...].
#
# A file can carry MORE THAN ONE group when its failures have more than one
# cause (e.g. test_web_ui_build.py fails partly on POSIX npm/fcntl assumptions
# and partly because `pathspec` is not installed). Every group is applied
# independently, so each node id keeps the mark and the reason that actually
# explains it — a single-mark-per-file registry would have forced one of the
# two causes to be recorded as a lie.
_ENV_GAPS: dict[str, list[tuple[str, str, set[str]]]] = {
    'test_apply_profile_override.py': [
        (
            _WINDOWS,
            'profile lookup resolves get_default_hermes_root(), which is '
            '%LOCALAPPDATA%\\hermes on Windows while the fixtures seed ~/.hermes',
            {
                'TestApplyProfileOverrideHermesHomeGuard::test_sudo_explicit_profile_resolves_invoking_users_profile',
                'TestSupervisedChildIgnoresStickyProfile::test_non_supervised_run_still_follows_active_profile',
                'TestSupervisedChildIgnoresStickyProfile::test_supervised_named_profile_flag_still_wins',
            },
        ),
    ],
    'test_atomic_json_write.py': [
        (
            _WINDOWS,
            'concurrent os.replace() onto an open file raises PermissionError on '
            'Windows; POSIX rename is atomic',
            {
                'TestAtomicJsonWrite::test_concurrent_writes_dont_corrupt',
            },
        ),
    ],
    'test_auth_nous_provider.py': [
        (
            _WINDOWS,
            'asserts a 0o600 file mode; NTFS reports 0o666 because os.chmod only '
            'toggles the read-only bit',
            {
                'test_shared_store_write_and_read_roundtrip',
            },
        ),
    ],
    'test_backup.py': [
        (
            _WINDOWS,
            'asserts 0o600 modes and POSIX profile-wrapper symlinks; NTFS reports '
            '0o666',
            {
                'TestMemoryProviderExternalPaths::test_import_restores_external_to_home_relative_location',
                'TestProfileRestoration::test_import_skips_profile_dirs_without_config',
                'TestQuickSnapshotProjectsKanban::test_board_db_copied_wal_safely',
            },
        ),
    ],
    'test_cmd_update.py': [
        (
            _WINDOWS,
            'cmd_update prepends `git -c windows.appendAtomically=false` on win32 and '
            'resolves npm as npm.CMD; the mocks assert the POSIX argv. WARNING: this '
            'file only stubs subprocess.run, so `_build_web_ui` still runs a REAL npm '
            'install + vite build against the checkout',
            {
                'TestCmdUpdateBranchFallback::test_update_on_fork_checks_upstream_when_origin_up_to_date',
            },
        ),
    ],
    'test_codex_runtime_plugin_migration.py': [
        (
            _WINDOWS,
            'the em dash in MIGRATION_MARKER is written as UTF-8 and read back with '
            'the Windows default cp1252 codec, so the marker comes back as mojibake '
            "('â€”') and is never found",
            {
                'TestStripUnmanagedPluginTables::test_migrate_dedups_codex_owned_plugin_tables',
            },
        ),
    ],
    'test_commands.py': [
        (
            _HOST,
            "Slack native-slash parity against the installed plugin set ('version' "
            'present on Telegram only)',
            {
                'TestSlackNativeSlashes::test_telegram_parity',
            },
        ),
    ],
    'test_completion.py': [
        (
            _WINDOWS,
            'shells out to `bash -n <windows path>`; Git-bash cannot resolve a C:\\... '
            'argument',
            {
                'TestGenerateBash::test_valid_bash_syntax',
            },
        ),
    ],
    'test_config.py': [
        (
            _WINDOWS,
            'asserts the POSIX default root ~/.hermes; get_hermes_home() returns '
            '%LOCALAPPDATA%\\hermes on Windows',
            {
                'TestGetHermesHome::test_default_path',
            },
        ),
    ],
    'test_container_boot.py': [
        (
            _WINDOWS,
            's6/container supervision layout is Linux-only; service_manager calls '
            'os.chown, which does not exist on Windows',
            {
                'test_profiles_default_subdir_is_skipped_with_warning',
                'test_register_service_overwrites_existing_slot',
                'test_registered_profile_has_finish_script',
                'test_running_profile_is_registered_and_autostarted',
            },
        ),
    ],
    'test_cron.py': [
        (
            _HOST,
            "optional dependency 'croniter' is not installed, so every cron "
            'expression is rejected',
            {
                'TestGatewayNotRunningWarning::test_list_warns_when_gateway_absent',
            },
        ),
    ],
    'test_debug.py': [
        (
            _WINDOWS,
            'log-snapshot truncation budget differs because the captured log is '
            'CRLF-terminated on Windows',
            {
                'TestCaptureLogSnapshot::test_keeps_first_line_when_truncation_on_boundary',
            },
        ),
    ],
    'test_diff_command.py': [
        (
            _WINDOWS,
            'the fixture writes main.py with Path.write_text (Windows text mode emits '
            'CRLF) and commits it with HOME pointed at the tmp repo, where '
            "Git-for-Windows' SYSTEM config core.autocrlf=true still applies and "
            'normalises it to LF in the index; the in-process /diff then runs under '
            'the real HOME (core.autocrlf=false), so the clean repo reads as one '
            'modified file',
            {
                'test_diff_clean_repo_reports_no_changes',
            },
        ),
    ],
    'test_early_recovery.py': [
        (
            _HOST,
            "the test's builtins.__import__ guard keys on name.split('.')[0], so it "
            'rejects every RELATIVE import executed under it (`from . import '
            "_compiler` arrives as name ''). That only stays invisible on a host "
            'whose startup/site path already imported re, locale, subprocess and '
            'importlib. This interpreter (CPython 3.12.5, no preloading .pth files) '
            'has none of them in sys.modules at startup, so their module bodies '
            'execute under the guard and trip it',
            {
                'test_early_recovery_module_is_stdlib_only',
            },
        ),
    ],
    'test_ensure_acp_launcher.py': [
        (
            _WINDOWS,
            'os.geteuid does not exist on Windows',
            {
                'test_unwritable_bin_dir_is_skipped',
            },
        ),
    ],
    'test_ensure_hermes_home_uid_34107.py': [
        (
            _WINDOWS,
            'os.chown does not exist on Windows',
            {
                'TestChownToHermesUid::test_attributeerror_swallowed_for_windows_compat',
                'TestChownToHermesUid::test_calls_os_chown_when_both_set',
                'TestChownToHermesUid::test_eperm_is_silently_swallowed',
                'TestResolveHermesUidGid::test_returns_parsed_values_when_both_set',
            },
        ),
    ],
    'test_gateway_restart_loop.py': [
        (
            _HOST,
            "optional dependency 'croniter' is not installed, so the lifecycle block "
            'message never reaches the assertion',
            {
                'TestCreateJobBlocksLifecycleCommands::test_cronjob_tool_surfaces_block_as_error',
            },
        ),
    ],
    'test_hooks_cli.py': [
        (
            _WINDOWS,
            'shell-hook doctor executes POSIX scripts and compares POSIX '
            'mtime/approval state',
            {
                'TestHooksDoctor::test_flags_mtime_drift',
                'TestHooksTest::test_fires_real_subprocess_and_parses_block',
                'TestHooksTest::test_synthetic_payload_matches_production_shape',
            },
        ),
    ],
    'test_kanban_boards.py': [
        (
            _WINDOWS,
            'board-DB teardown asserts a cache invalidation that Windows file locking '
            'defers',
            {
                'TestBoardCRUD::test_remove_clears_init_cache_for_recreated_db[False]',
                'TestBoardCRUD::test_remove_clears_init_cache_for_recreated_db[True]',
            },
        ),
    ],
    'test_kanban_core_functionality.py': [
        (
            _WINDOWS,
            'os.WIFEXITED / os.WEXITSTATUS / os.WIFSIGNALED do not exist on Windows, '
            'so _classify_worker_exit() falls into its except branch and returns '
            '("unknown", None) for every reaped worker. A clean-exit protocol '
            'violation is then counted as a plain crash against the unified failure '
            'budget instead of the violation-only streak, and the task blocks one '
            "attempt early. Also: tests/conftest.py's live-system guard refuses "
            'os.kill() of a synthetic PID',
            {
                'test_protocol_violation_budget_not_consumed_by_other_failures',
            },
        ),
    ],
    'test_kanban_db.py': [
        (
            _WINDOWS,
            'worker-PID reaping asserts POSIX signal delivery to synthetic PIDs',
            {
                'test_rate_limit_exit_requeues_without_counting_failure',
                'test_worktree_workspace_explicit_target_materializes_linked_worktree',
            },
        ),
    ],
    'test_kanban_worker_image_extraction.py': [
        (
            _WINDOWS,
            'image paths in a task body are matched with a POSIX path regex; Windows '
            'paths carry a drive letter',
            {
                'TestBuildPartsFromTaskBody::test_code_block_example_is_not_attached',
                'TestBuildPartsFromTaskBody::test_local_path_becomes_native_image_part',
                'TestExtractFromTaskBody::test_local_path_in_body_round_trips',
            },
        ),
    ],
    'test_managed_uv.py': [
        (
            _WINDOWS,
            'POSIX uv/venv layout: the fixture "binary" is a `#!/bin/sh` text file at '
            'bin/uv (no .exe, so Windows resolution misses it and executing it raises '
            'WinError 216) and _default_live_venv probes Scripts/python.exe rather '
            'than bin/python; the install-branch tests additionally assert '
            '_install_uv_posix, while Windows takes _install_uv_windows',
            {
                'TestDefaultLiveVenv::test_dot_venv_only_is_targeted',
                'TestEnsureUv::test_install_reports_runtime_repair_to_observer',
                'TestEnsureUv::test_installs_if_missing',
                'TestInstallUvInternals::test_posix_sets_uv_unmanaged_install',
                'TestResolveUv::test_existing_executable',
                'TestUpdateManagedUv::test_fresh_stamp_skips_network_self_update_but_not_repair',
                'TestUpdateManagedUv::test_stale_stamp_runs_self_update_and_refreshes_stamp',
            },
        ),
    ],
    'test_plugins_cmd.py': [
        (
            _WINDOWS,
            'reads a plugin file without an explicit encoding; the Windows default '
            'cp1252 codec raises UnicodeDecodeError',
            {
                'TestNoAutoActivation::test_compressor_default_ignores_plugin',
            },
        ),
    ],
    'test_profiles.py': [
        (
            _WINDOWS,
            'asserts a 0o600 .env mode; NTFS reports 0o666 because os.chmod only '
            'toggles the read-only bit',
            {
                'TestBackfillProfileEnvs::test_copies_default_env_into_envless_profiles',
                'TestCreateProfile::test_seeds_placeholder_env_file',
            },
        ),
    ],
    'test_projects_db.py': [
        (
            _WINDOWS,
            "asserts POSIX absolute-path literals ('/a/c'); Path normalisation yields "
            "'X:\\a\\c'",
            {
                'test_create_get_list',
                'test_per_profile_isolation',
            },
        ),
    ],
    'test_prompt_compose_command.py': [
        (
            _WINDOWS,
            'the fake $EDITOR is a POSIX shell script and does not execute on Windows',
            {
                'test_compose_reads_and_strips_header',
            },
        ),
    ],
    'test_relaunch.py': [
        (
            _WINDOWS,
            "asserts os.execvp against '/usr/bin/hermes'",
            {
                'TestRelaunch::test_calls_execvp',
            },
        ),
    ],
    'test_service_manager.py': [
        (
            _WINDOWS,
            'os.chown does not exist on Windows and systemd units cannot be written',
            {
                'test_s6_log_run_creates_leaf_as_hermes_without_chown',
                'test_seed_supervise_skeleton_creates_expected_layout',
            },
        ),
    ],
    'test_session_browse.py': [
        (
            _WINDOWS,
            "the '_curses' module is not available on Windows",
            {
                'TestCursesBrowse::test_escape_cancels',
                'TestCursesBrowse::test_type_to_filter_then_enter',
            },
        ),
    ],
    'test_setup_blank_slate.py': [
        (
            _HOST,
            "minimal toolset now also carries upstream's standalone 'tool_describe' "
            'bridge tool',
            {
                'TestBlankSlateMinimalToolsets::test_tool_schema_survives_disabled_toolsets_from_config',
            },
        ),
    ],
    'test_setup_hermes_script.py': [
        (
            _WINDOWS,
            'shells out to `bash -n <windows path>`; Git-bash cannot resolve an '
            'X:\\... argument',
            {
                'test_setup_hermes_script_is_valid_shell',
            },
        ),
    ],
    'test_setup_matrix_e2ee.py': [
        (
            _WINDOWS,
            'reads a source file without an explicit encoding; the Windows default '
            'cp1252 codec raises UnicodeDecodeError',
            {
                'TestSetupShutilImport::test_shutil_imported_at_module_level',
            },
        ),
    ],
    'test_skin_cmd.py': [
        (
            _WINDOWS,
            'reads the generated skin YAML with Path.read_text() and no encoding; the '
            'Windows default cp1252 codec cannot decode its UTF-8 bytes',
            {
                'test_set_forks_a_builtin_without_inventing_a_background',
            },
        ),
    ],
    'test_subprocess_timeouts.py': [
        (
            _WINDOWS,
            'reads source files without an explicit encoding; the Windows default '
            'cp1252 codec raises UnicodeDecodeError',
            {
                'test_all_subprocess_run_calls_have_timeout[hermes_cli/banner.py]',
                'test_all_subprocess_run_calls_have_timeout[hermes_cli/doctor.py]',
                'test_all_subprocess_run_calls_have_timeout[hermes_cli/status.py]',
            },
        ),
    ],
    'test_tui_resume_flow.py': [
        (
            _WINDOWS,
            'asserts a child process wrote exactly b"ok\\n"; Windows text-mode newline '
            'translation makes it b"ok\\r\\n"',
            {
                'test_oneshot_subprocess_exits_without_teardown_abort',
            },
        ),
    ],
    'test_uninstall_node_symlinks.py': [
        (
            _WINDOWS,
            'asserts POSIX symlinks for node/npm/npx',
            {
                'test_removes_fhs_symlinks_in_usr_local_bin',
            },
        ),
    ],
    'test_update_eol_churn.py': [
        (
            _HOST,
            'git toolchain floor: this host runs git 2.31.1.windows.1, where `git '
            'diff --name-only --ignore-cr-at-eol` still lists a file whose full '
            '--ignore-cr-at-eol diff is empty (the name-only output is decided before '
            'the content-level ignore rules run). _normalize_managed_eol() derives '
            'its EOL-only set as `dirty - dirty(--ignore-cr-at-eol)`, which is '
            'therefore always empty here, so it pins core.autocrlf=false without '
            'restoring anything. test_churn_across_more_files_than_fit_in_one_argv '
            "additionally depends on git's 1-second racy-index window: its 1200-file "
            'checkout straddles a second boundary on this filesystem, so only the '
            "last few hundred index entries are re-read and the fixture's own "
            '`len(_dirty(repo)) == 1200` precondition cannot hold',
            {
                'test_churn_across_more_files_than_fit_in_one_argv',
                'test_churn_invisible_under_autocrlf_true_is_still_found',
                'test_churn_is_cleared_and_the_pin_is_persisted',
                'test_real_edits_survive_even_when_line_endings_also_flipped',
            },
        ),
    ],
    'test_update_stale_dashboard.py': [
        (
            _WINDOWS,
            'POSIX process plumbing: synthetic-PID signal delivery, '
            '/proc/<pid>/cmdline, the `ps` fallback, and systemd/cgroup unit restart. '
            'On Windows the stale-dashboard kill path shells out to taskkill and '
            '_dashboard_cmdline_for_pid returns None by design, so the '
            'systemd-restart and argv-capture branches are never reached',
            {
                'TestCmdlineCapture::test_falls_back_to_ps_without_proc',
                'TestCmdlineCapture::test_reads_proc_cmdline_when_available',
                'TestFindStaleDashboardPids::test_self_pid_excluded',
                'TestManualBackendRespawn::test_argv_capture_failure_falls_back_to_hint',
                'TestSupervisedBackendRestart::test_supervised_pid_restarts_owning_unit',
            },
        ),
    ],
    'test_web_server_oauth_write.py': [
        (
            _WINDOWS,
            'asserts a 0o600 file mode; NTFS reports 0o666',
            {
                'test_dashboard_oauth_write_uses_owner_only_permissions',
            },
        ),
    ],
    'test_web_ui_build.py': [
        (
            _WINDOWS,
            "asserts npm at '/usr/bin/npm' (Windows resolves 'C:\\Program "
            "Files\\nodejs\\npm.CMD'), and the flock test imports fcntl, which does not "
            'exist on Windows',
            {
                'TestBuildWebUIFlock::test_contended_lock_without_dist_waits_then_skips_fresh_build',
                'TestBuildWebUISkipsWhenFresh::test_web_build_uses_idle_timeout_helper',
                'TestBuildWebUISkipsWhenFresh::test_web_install_omits_workspace_when_web_has_own_lockfile',
            },
        ),
        (
            _HOST,
            "declared dependency 'pathspec' (pathspec==1.1.1 in pyproject) is not "
            'installed on this host, so _compute_web_ui_content_hash() raises '
            'ModuleNotFoundError; _write_web_ui_build_stamp() swallows it and writes '
            'no stamp, and _web_ui_build_needed() then reports stale unconditionally',
            {
                'TestWebUIBuildNeeded::test_content_hash_is_deterministic',
                'TestWebUIBuildNeeded::test_mtime_only_change_is_not_stale',
                'TestWebUIBuildNeeded::test_write_stamp_creates_file_with_hash',
            },
        ),
    ],
    'test_win_pty_bridge.py': [
        (
            _HOST,
            "optional dependency 'pywinpty' is not installed",
            {
                'TestWinPtyBridgeClose::test_close_terminates_long_running_child',
                'TestWinPtyBridgeEnv::test_cwd_is_respected',
                'TestWinPtyBridgeEnv::test_env_is_forwarded',
                'TestWinPtyBridgeIO::test_read_returns_none_after_child_exits',
                'TestWinPtyBridgeIO::test_write_sends_to_child_stdin',
                'TestWinPtyBridgeResize::test_resize_after_close_is_silent',
                'TestWinPtyBridgeResize::test_resize_does_not_raise_on_live_child',
                'TestWinPtyBridgeSpawn::test_spawn_returns_bridge_with_pid',
            },
        ),
    ],
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
        "toolchain (croniter / pywinpty / pathspec, Node "
        ">=20.19 for the Vite 8 web build, git >=2.31 semantics for "
        "`--name-only --ignore-cr-at-eol`, outbound HTTP). Not a fork "
        "regression; deselect with -m 'not host_dependency_gap'.",
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
        groups = _ENV_GAPS.get(item.path.name)
        if groups is None:
            continue
        _, _, within_file = item.nodeid.partition("::")
        for mark, reason, node_ids in groups:
            if within_file in node_ids:
                item.add_marker(getattr(pytest.mark, mark)(reason=reason))


_STALE_ENV_GAP_ENTRIES: list[str] = []


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record registered environment-gap node ids that actually passed."""
    if report.when != "call" or report.outcome != "passed":
        return
    file_name = report.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
    groups = _ENV_GAPS.get(file_name)
    if groups is None:
        return
    _, _, within_file = report.nodeid.partition("::")
    if any(within_file in node_ids for _, _, node_ids in groups):
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
