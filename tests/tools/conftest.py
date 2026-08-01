"""Shared fixtures for tests/tools/ web-provider tests.

Per-file subprocess isolation means each test file gets a fresh interpreter,
so module-level state (like the web-search-provider registry) is empty when
a file starts.  The ``web_registry_populated`` fixture registers all bundled
providers before each test and resets the registry afterwards — tests that
depend on the registry being populated should use it explicitly or via
``@pytest.mark.usefixtures("web_registry_populated")``.
"""

from unittest.mock import patch

import pytest

from tests._env_gap_fence import (
    HOST_DEPENDENCY_GAP as _HOST,
    WINDOWS_ENV_GAP as _WINDOWS,
    EnvGapRegistry,
    StaleEntryTracker,
    apply_marks,
    register_marks,
)


def register_all_web_providers():
    """Register all bundled web-search providers into the global registry.

    This is the single source of truth for the provider list used by
    test classes that need the registry populated for dispatch checks.
    """
    from agent.web_search_registry import register_provider, _reset_for_tests
    from plugins.web.brave_free.provider import BraveFreeWebSearchProvider
    from plugins.web.ddgs.provider import DDGSWebSearchProvider
    from plugins.web.exa.provider import ExaWebSearchProvider
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
    from plugins.web.parallel.provider import ParallelWebSearchProvider
    from plugins.web.searxng.provider import SearXNGWebSearchProvider
    from plugins.web.tavily.provider import TavilyWebSearchProvider
    from plugins.web.xai.provider import XAIWebSearchProvider

    _reset_for_tests()
    for cls in (
        BraveFreeWebSearchProvider,
        DDGSWebSearchProvider,
        ExaWebSearchProvider,
        FirecrawlWebSearchProvider,
        ParallelWebSearchProvider,
        SearXNGWebSearchProvider,
        TavilyWebSearchProvider,
        XAIWebSearchProvider,
    ):
        register_provider(cls())


@pytest.fixture
def web_registry_populated():
    """Populate the web-search-provider registry for one test, then reset."""
    register_all_web_providers()
    yield
    from agent.web_search_registry import _reset_for_tests
    _reset_for_tests()


@pytest.fixture
def disable_lazy_stt_install():
    """Disarm the runtime lazy-install probe so static ``_HAS_FASTER_WHISPER``
    patches accurately simulate 'faster-whisper not installed'.

    Without this, ``_try_lazy_install_stt()`` calls
    ``importlib.util.find_spec("faster_whisper")``, which returns truthy
    whenever the package is installed in the dev / CI environment —
    defeating the test's ``_HAS_FASTER_WHISPER=False`` patch.

    Opt in at module scope with
    ``pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")``.
    """
    with patch("tools.transcription_tools._try_lazy_install_stt", return_value=False):
        yield


# ── Pre-existing environment-gap fence (2026-07-31 upstream sync) ───────────
#
# Every row was reproduced individually on this Windows 10 workstation and
# traced to a concrete host/platform cause. Rows marked "pre-existing" produce
# an identical FAILED line on the pre-merge fork tip `1adf0404f`; rows marked
# "arrived with the merge" are in files (or nodes) that did not exist there, so
# there is no fork regression intent to preserve in them either.
#
# INTERPRETER MATTERS. These rows describe ambient `C:\Python312\python.exe`,
# the same interpreter the already-landed `tests/hermes_cli/conftest.py`
# registry was built against. Do not mix interpreters between the registry and
# the run: sweeping under the runtime venv at
# `X:\Eternia\.hermes\venvs\hermes-agent\Scripts\python.exe` is a known trap.
#
# 2026-08-01 (ledger item 7, RULED EXECUTE): psutil==7.2.2, fire==0.7.1 and
# Markdown==3.10.2 — all three DECLARED in `[project.dependencies]` and all
# three resolved in uv.lock — were finally installed on this ambient
# interpreter, which is what the declaration always said should be true. Every
# row that named a missing psutil/fire/markdown was retired here; `pathspec`
# and `croniter` remain uninstalled, so rows naming those stay. Two former
# psutil rows in `test_process_registry.py` survived the install and were
# RE-DIAGNOSED to the platform cause that the missing import had been masking
# — see the inline notes there.
#
# NEVER FENCED — the three real defects triage left red here were FIXED, not
# marked. Recorded so nobody re-adds a row for them:
#
#   * `test_file_tools_live.py::TestSearch::*` and
#     `test_search_error_guard.py::TestSearchErrorGuard::*[_search_with_rg]` —
#     `search_files` content search was genuinely BROKEN on Windows. Upstream's
#     `_bash_safe_path()` rewrote every `ShellFileOperations._escape_shell_arg`
#     argument from `C:\Users\x` to `/c/Users/x`, while
#     `_apply_windows_msys_bash_env_defaults` sets MSYS_NO_PATHCONV=1 /
#     MSYS2_ARG_CONV_EXCL=* so MSYS never converted it back for the NATIVE
#     ripgrep that receives it. The two mechanisms contradicted each other.
#     Reconciled by splitting the consumer classes: bash's own script
#     constructs keep `_bash_safe_path`'s `/c/...` form, while program
#     ARGUMENTS go through the new `_shell_arg_safe_path`, which emits the
#     drive-qualified `C:/Users/x` form that the MSYS runtime AND native
#     binaries both resolve. Quoting/escaping is untouched, and non-path
#     arguments (search patterns, `python -c` snippets) are now left verbatim
#     instead of having their backslashes rewritten. See `fix(sync): reconcile
#     the MSYS path rewrite with MSYS_NO_PATHCONV`.
#   * `test_t6b_brief_descriptions.py::test_mirror_covers_thirty_five_tools` —
#     the count was stale, not the mirror: the mission-lane removal retired
#     `mission_goal_create` and dropped its row without updating the assertion.
#     Corrected to 34 and renamed.
#   * `test_modal_sandbox_fixes.py::TestToolResolution::
#     test_terminal_and_file_toolsets_resolve_all_tools` — upstream asserted an
#     exact toolset; the fork injects `tool_describe` into every resolved lane.
#     The fork contract won; the expectation was retargeted onto it.
#
# NOT fenced, deliberately: `test_file_read_guards.py` did not fail here — it
# HUNG, unbounded, because os.path.normpath("/dev/zero") is "\dev\zero" on
# Windows and missed tools/file_tools.py's `_BLOCKED_DEVICE_PATHS`, letting the
# read fall through to `wc -c < /dev/zero` under Git Bash. A mark cannot fence a
# hang (a plain run still executes it and kills the whole interpreter), and the
# guard's entire purpose is preventing exactly that hang — so the GUARD was
# fixed instead. See `fix(sync): make the device-path guard match POSIX
# spellings on Windows`.
#
# These tests are NOT skipped and NOT xfailed: they still run, still execute
# their real assertions, and still fail loudly on a plain `pytest tests/tools`.
# See `tests/_env_gap_fence` for the full contract and the rules for adding a
# row.
#
#     python -m pytest tests/tools -m "not windows_env_gap and not host_dependency_gap"
_CP1252 = (
    'the test reads a source file with open()/read_text() and no encoding=, so '
    "Python uses this host's cp1252 locale default and dies on the UTF-8 bytes "
    'in the file it is scanning'
)
_SEP = (
    'asserts a path in its POSIX spelling; the code builds it with os.sep (or '
    'via _bash_safe_path, which emits the /c/... MSYS form, or '
    '_shell_arg_safe_path, which emits the drive-qualified C:/... form), so on '
    'Windows the separators do not match'
)
_GITBASH = (
    'the test depends on the POSIX spawn/coreutils shape — a shebang, an '
    'executable bit, $SHELL resolution, or a coreutil reached the way a real '
    'shell would. Under Git Bash on Windows the child exits 127 or receives a '
    'mangled path, so the behaviour under test never runs'
)
_POSIX_MODE = (
    'asserts POSIX file modes or POSIX-only sensitive roots (/etc, /private, '
    '/boot); NTFS has no mode bits (os.chmod only toggles the read-only flag) '
    'and none of those roots exist on Windows'
)
_POSIX_PROC = (
    'depends on POSIX process primitives absent on Windows — os.getpgid, '
    'process groups, real signal delivery, or /proc/<pid>/'
)
_AF_UNIX = (
    'socket.AF_UNIX does not exist on Windows, so the unix-socket path under '
    'test cannot be exercised at all'
)
_TIRITH = (
    'tirith has no Windows build, so is_platform_supported() is False and '
    'every verdict short-circuits to allow/unsupported_platform before the '
    'behaviour under test is reached'
)
_WSL_TTS = (
    "upstream's new _wsl_powershell_tts_available() fallback finds a REAL "
    'powershell.exe on this Windows host, so the mocked-WSL path it is trying '
    'to exercise takes the PowerShell branch instead'
)

_ENV_GAPS: EnvGapRegistry = {
    # ── cp1252 default encoding ──────────────────────────────────────────
    'test_browser_content_none_guard.py': [
        (_WINDOWS, _CP1252, {
            'TestBrowserSourceLinesAreGuarded::test_extract_relevant_content_guarded',
            'TestBrowserSourceLinesAreGuarded::test_browser_vision_guarded',
        }),
    ],
    'test_llm_content_none_guard.py': [
        (_WINDOWS, _CP1252, {
            'TestSourceLinesAreGuarded::test_web_tools_guarded',
            'TestSourceLinesAreGuarded::test_vision_tools_guarded',
        }),
    ],
    'test_skills_guard.py': [
        (_WINDOWS, 'arrived with the merge. ' + _CP1252 + " — here a zero-width "
         'space (U+200B) in the injection fixture cannot be encoded to cp1252', {
            'TestScanFile::test_detect_markdown_injection',
        }),
    ],
    'test_process_registry.py': [
        # 'TestPidReuseGuard::test_terminate_refuses_when_start_time_mismatches'
        # was retired 2026-08-01 (ledger item 7): the start-time comparison it
        # guards is a psutil read, so psutil==7.2.2 made it pass.
        (_WINDOWS, _POSIX_PROC, {
            'TestStdinHelpers::test_close_stdin_allows_eof_driven_process_to_finish',
            # Re-diagnosed 2026-08-01 (ledger item 7): was registered as
            # "psutil not installed". With psutil==7.2.2 now installed on the
            # ambient interpreter the real cause is visible, and it is a
            # platform property, not a host gap — patching `os.getpgid` raises
            # AttributeError because Windows `os` has no such attribute.
            'TestPopenLeakOnSetupFailure::test_popen_killed_when_thread_creation_fails',
        }),
        # Re-diagnosed 2026-08-01 (ledger item 7): also previously registered
        # as "psutil not installed". psutil is installed now and the assertion
        # still cannot hold, because the seam it mocks is POSIX-only.
        (_WINDOWS, 'the test mocks `psutil.Process(pid).terminate()` and '
         'asserts it was called, but that is the POSIX kill path only: '
         '`ProcessRegistry._terminate_host_pid` shells out to '
         '`taskkill /PID <pid> /T /F` on Windows (process_registry.py:565,601) '
         'and never constructs a psutil.Process, so the mocked terminate '
         'records nothing. The kill itself succeeds (status == "killed"); only '
         'the POSIX-specific call assertion fails', {
            'TestKillProcess::test_kill_detached_session_uses_host_pid',
        }),
        # 'TestSpawnEnvSanitization::test_spawn_local_strips_blocked_vars_from_
        # _background_env' was retired 2026-08-01 (ledger item 7). Unlike every
        # other retirement in that pass this one is NOT attributable to the
        # psutil/fire/Markdown install: the stale detector reported it PASSING
        # on the pre-install sweep as well, so the row had already stopped
        # being a fence. Its recorded cause was an import-time
        # `get_hermes_home()` resolution in tools/environments/singularity.py
        # that raised once os.environ was scrubbed, and the row itself noted the
        # node "passed at 1adf0404f only because a sibling test ... used to warm
        # that import first". Something warms it again on this tip, in both
        # whole-directory and per-file runs. Deleted per the fence contract — a
        # passing row hides a future regression — and recorded here rather than
        # silently dropped. The underlying fragility is unchanged and still
        # worth retiring at the source (resolve the snapshot store lazily).
    ],
    'test_terminal_output_transform_hook.py': [
        (_HOST, 'arrived with the merge. The test drives a `python3` child, and '
         'Git Bash on Windows has no `python3` on PATH (the interpreter is '
         '`python`), so the child exits 127 before producing output', {
            'test_large_process_output_is_bounded_before_sudo_and_plugin_hooks',
        }),
    ],
    # ── POSIX-only primitives ────────────────────────────────────────────
    'test_code_execution.py': [
        (_WINDOWS, _AF_UNIX, {
            'TestRpcTokenAuthorization::test_missing_token_rejected',
        }),
    ],
    'test_local_interrupt_cleanup.py': [
        (_WINDOWS, _POSIX_PROC, {
            'test_kill_process_uses_cached_pgid_if_wrapper_already_exited',
            'test_wait_for_process_kills_subprocess_on_keyboardinterrupt',
        }),
    ],
    'test_voice_mode.py': [
        (_WINDOWS, _AF_UNIX, {
            'TestPulseSocketReachable::test_stale_socket_file_not_reachable',
            'TestPulseSocketReachable::test_listening_socket_reachable_via_xdg_runtime',
        }),
        (_WINDOWS, _WSL_TTS, {
            'TestDetectAudioEnvironment::test_wsl_without_pulse_blocks_voice',
            'TestWSL2PowerShellFallback::test_powershell_pipeline_preserves_real_exit_status',
            'TestWSL2PowerShellFallback::test_wsl2_unique_temp_filename',
        }),
    ],
    'test_voice_wsl_pipewire.py': [
        (_WINDOWS, 'arrived with the merge. ' + _WSL_TTS, {
            'test_wsl_without_forwarding_still_blocks',
        }),
    ],
    'test_tirith_security.py': [
        (_WINDOWS, _TIRITH, {
            'TestExitCodeMapping::test_exit_1_block_with_findings',
            'TestExitCodeMapping::test_exit_2_warn_with_findings',
            'TestJsonParseFailure::test_exit_1_invalid_json_still_blocks',
            'TestOSErrorFailOpen::test_file_not_found_fail_open',
            'TestOSErrorFailOpen::test_os_error_fail_closed',
            'TestTimeoutFailOpen::test_timeout_fail_closed',
            'TestUnknownExitCode::test_unknown_exit_code_fail_closed',
            'TestProgrammingErrors::test_attribute_error_propagates',
            'TestEnsureInstalled::test_found_on_path_returns_immediately',
            'TestFailedDownloadCaching::test_failed_install_cached_no_retry',
            'TestExplicitPathNoAutoDownload::test_default_path_does_auto_download',
            'TestBackgroundInstall::test_ensure_installed_non_blocking',
            'TestSpawnWarningDedup::test_repeated_spawn_failure_logs_once',
            'TestAppTldSuppression::test_mixed_findings_preserve_warn',
            'TestAppTldSuppression::test_block_verdict_never_suppressed',
            'TestMkdtempOSErrorNoSpace::test_mkdtemp_oserror_returns_no_space',
            'TestCaps::test_findings_and_summary_capped',
        }),
    ],
    # ── POSIX path separators / MSYS path form ───────────────────────────
    'test_credential_files.py': [
        (_WINDOWS, _SEP, {'TestIterSkillsFiles::test_returns_files_skipping_symlinks'}),
    ],
    'test_daytona_environment.py': [
        (_WINDOWS, _SEP, {'TestSyncSafety::test_single_upload_quotes_parent_path'}),
    ],
    'test_file_tools_cwd_resolution.py': [
        (_WINDOWS, _SEP, {
            'test_container_absolute_input_path_does_not_follow_host_symlink',
            'test_container_relative_path_keeps_container_cwd_symlink',
            'test_warning_fires_when_relative_path_escapes_workspace',
            'test_warning_fires_from_terminal_cwd_when_registry_empty',
        }),
    ],
    'test_file_tools_tilde_profile.py': [
        (_WINDOWS, _SEP, {'TestExpandTilde::test_tilde_expands_to_profile_home'}),
    ],
    'test_local_env_cwd_recovery.py': [
        (_WINDOWS, _SEP, {'TestResolveSafeCwd::test_returns_root_when_only_root_exists'}),
    ],
    'test_local_env_blocklist.py': [
        (_WINDOWS, _SEP, {
            'TestSanePathIncludesHomebrew::test_make_run_env_appends_homebrew_on_minimal_path',
            'TestSanePathIncludesHomebrew::test_make_run_env_real_launchd_path_gains_homebrew',
            'TestSanePathIncludesHomebrew::test_make_run_env_preserves_windows_mixed_case_path_key',
            'TestHermesBinDirOnPath::test_make_run_env_injects_hermes_bin_dir',
        }),
    ],
    'test_local_env_relative_cwd.py': [
        (_WINDOWS, 'arrived with the merge. ' + _SEP, {
            'test_local_environment_keeps_existing_relative_child_cwd',
        }),
    ],
    'test_local_env_windows_msys.py': [
        (_WINDOWS, 'arrived with the merge. _git_bash_bin_dirs builds with '
         'os.path.join, producing "/pg\\usr\\bin" on Windows, while the test '
         'asserts the POSIX "/pg/usr/bin". The function is byte-identical to '
         'upstream/main', {
            'TestGitBashCoreutilsOnPath::test_derives_dirs_from_portablegit_layout',
        }),
    ],
    'test_local_tempdir.py': [
        (_WINDOWS, _SEP, {
            'TestLocalTempDir::test_uses_os_tmpdir_for_session_artifacts',
            'TestLocalTempDir::test_falls_back_to_tempfile_when_tmp_missing',
        }),
    ],
    'test_local_shell_init.py': [
        (_WINDOWS, _SEP, {
            'TestResolveShellInitFiles::test_auto_sources_bashrc_when_present',
            'TestResolveShellInitFiles::test_auto_sources_profile_when_present',
            'TestResolveShellInitFiles::test_auto_sources_profile_before_bashrc',
        }),
    ],
    'test_skills_sync.py': [
        (_WINDOWS, _SEP, {'TestComputeRelativeDest::test_preserves_category_structure'}),
    ],
    'test_skill_bundle_provenance.py': [
        (_WINDOWS, 'arrived with the merge. ' + _SEP + ' — the bundle keys read '
         '"references\\all.md" where the test expects "references/all.md"', {
            'test_bundled_optional_source_still_includes_support_files',
        }),
    ],
    'test_checkpoint_manager.py': [
        (_WINDOWS, 'arrived with the merge. ' + _SEP, {
            'TestGitEnvIsolation::test_env_pins_store_worktree_and_ignores_ambient_git_state',
            'TestClearFunctions::test_clear_all_wipes_base_then_is_a_noop',
        }),
    ],
    # ── POSIX modes / POSIX-only roots ───────────────────────────────────
    'test_base_environment.py': [
        (_WINDOWS, _POSIX_MODE, {
            'TestSnapshotFileModes::test_snapshot_and_cwd_files_are_0600',
        }),
        (_WINDOWS, 'the snapshot concurrency tests rely on POSIX rename/replace '
         'semantics over an open file; on Windows os.replace onto an open '
         'handle raises PermissionError', {
            'TestAtomicSnapshotConcurrencyBehavioral::test_concurrent_writes_never_tear_the_snapshot',
            'TestAtomicSnapshotConcurrencyBehavioral::test_failed_export_does_not_destroy_good_snapshot',
        }),
    ],
    'test_file_write_safety.py': [
        (_WINDOWS, _POSIX_MODE, {
            'TestAtomicWrite::test_patch_routes_through_atomic_write',
            'TestCheckSensitivePathMacOSBypass::test_etc_hosts_blocked',
            'TestCheckSensitivePathMacOSBypass::test_private_etc_hosts_blocked',
            'TestCheckSensitivePathMacOSBypass::test_private_etc_ssh_config_blocked',
            'TestCheckSensitivePathMacOSBypass::test_private_var_blocked',
            'TestCheckSensitivePathMacOSBypass::test_boot_still_blocked',
        }),
    ],
    'test_stage2_hook_symlink_chown.py': [
        (_WINDOWS, _POSIX_MODE, {
            'test_chown_helper_refuses_symlinked_directories',
            'test_chown_helper_refuses_target_under_symlinked_home',
        }),
    ],
    'test_skills_sync_client.py': [
        (_WINDOWS, 'arrived with the merge. ' + _POSIX_MODE + ' — the blob mode '
         'comes back "file" where the test expects "exec"', {
            'TestObjectBuilding::test_build_tree_blob_and_exec',
        }),
    ],
    # ── Git Bash spawn / coreutils shape ─────────────────────────────────
    'test_approved_command_clean_slate.py': [
        # 'test_execute_code_non_approved_still_interrupts_on_stale_bit' was
        # retired 2026-08-01 (ledger item 7): installing psutil==7.2.2 on the
        # ambient interpreter made it pass. Its two siblings below still fail.
        (_WINDOWS, _GITBASH, {
            'test_approved_command_genuine_interrupt_after_start_still_kills',
            'test_approved_note_enriched_not_misleading_on_interrupt',
        }),
    ],
    'test_file_ops_cwd_tracking.py': [
        (_WINDOWS, _GITBASH, {
            'TestShellFileOpsCwdTracking::test_patch_returns_success_only_when_file_actually_written',
        }),
    ],
    'test_file_sync.py': [
        (_WINDOWS, _GITBASH, {
            'TestSyncBackSecurity::test_sync_back_does_not_overwrite_uploaded_credential_files',
        }),
    ],
    'test_find_shell.py': [
        (_WINDOWS, _GITBASH, {
            'TestFindShellPrefersUserShell::test_returns_shell_env_when_set_and_exists',
            'TestFindShellPrefersUserShell::test_honours_allowlisted_bash_and_dash',
        }),
    ],
    'test_lazy_deps_durable_target.py': [
        (_WINDOWS, _GITBASH, {'TestAbiStamp::test_readonly_target_reports_error'}),
    ],
    'test_local_background_child_hang.py': [
        (_WINDOWS, _GITBASH, {
            'TestBackgroundChildDoesNotHang::test_utf8_multibyte_across_read_boundary',
            'TestBackgroundChildDoesNotHang::test_invalid_utf8_uses_replacement_not_fallback',
            'TestBackgroundChildDoesNotHang::test_default_capture_is_full_fidelity_for_internal_consumers',
        }),
    ],
    'test_subprocess_stdin_guard.py': [
        (_WINDOWS, _GITBASH, {'test_all_tui_subprocess_calls_have_stdin'}),
    ],
    'test_execution_flag_detection.py': [
        (_WINDOWS, 'arrived with the merge. ' + _GITBASH + '. The real binaries '
         'behave differently too: Windows `sort.exe` returns 1 ("Input file '
         'specified two times") where the test expects 2', {
            'test_real_read_tool_binaries_confirm_option_ownership[argv1--2-]',
            'test_real_binaries_execute_leading_dash_program_payload[rg-args0-None-False]',
            'test_real_binaries_execute_leading_dash_program_payload[rg-args1-None-False]',
            'test_real_binaries_execute_leading_dash_program_payload[sort-args2-{bulk}-False]',
        }),
    ],
    # ── mixed / file-specific ────────────────────────────────────────────
    'test_approval.py': [
        (_WINDOWS, 'arrived with the merge. Depends on POSIX temp-dir and '
         'symlink canonicalization semantics that Windows does not reproduce', {
            'TestDetectDangerousRm::test_nonrecursive_verification_artifact_cleanup_is_not_dangerous',
            'TestDetectDangerousRm::test_symlinked_temp_dir_only_exempts_canonical_target',
        }),
    ],
    'test_computer_use.py': [
        (_WINDOWS, 'the CLI-fallback test interpolates a Windows path into a '
         'JSON literal, where "\\U" and "\\b" are invalid escapes so json.loads '
         'never succeeds; the gnome-shell skip is gated on '
         'sys.platform == "linux" (cua_backend.py:344) and cannot fire here. '
         'Baseline at 1adf0404f is inconclusive — the file HUNG there, and the '
         'merge fixes that hang', {
            'TestCuaDriverSessionReconnect::test_cli_fallback_reads_screenshot_from_file',
            'TestCaptureAppFilterNoMatch::test_linux_default_capture_skips_gnome_shell_helper',
        }),
    ],
    'test_docker_config_migrate.py': [
        (_WINDOWS, _SEP, {'test_docker_config_migrate_does_not_rewrite_invalid_yaml'}),
    ],
    'test_file_operations.py': [
        (_WINDOWS, _SEP, {
            'TestSearchFilesFallbackHiddenPaths::test_hidden_root_with_hidden_ancestor_includes_files',
            'TestSearchFilesFallbackHiddenPaths::test_normal_root_still_excludes_hidden_descendants',
        }),
        (_WINDOWS, 'arrived with the merge. ' + _POSIX_MODE + ' — umask and '
         'chmod "=rw" are no-ops on NTFS, so the asserted mode never appears', {
            'TestAtomicWriteNewFilePermissions::test_new_file_gets_umask_default_permissions[2]',
            'TestAtomicWriteNewFilePermissions::test_new_file_gets_umask_default_permissions[18]',
            'TestAtomicWriteNewFilePermissions::test_new_file_gets_umask_default_permissions[63]',
            'TestAtomicWriteNewFilePermissions::test_overwrite_still_preserves_existing_mode',
        }),
    ],
    'test_file_tools.py': [
        (_WINDOWS, _SEP, {'TestSensitivePathCheck::test_system_path_still_blocked'}),
        (_WINDOWS, _GITBASH, {
            'TestWriteFileHandler::test_writes_content',
            'TestPatchHandler::test_replace_mode_calls_patch_replace',
            'TestPatchSensitivePathExtraction::test_patch_move_to_sensitive_dst_blocked',
            'TestPatchSensitivePathExtraction::test_patch_update_no_space_after_asterisks_blocked',
        }),
    ],
    'test_file_tools_live.py': [
        (_WINDOWS, _SEP, {'TestExpandPath::test_tilde_exact'}),
        (_WINDOWS, _GITBASH, {
            'TestLocalEnvironmentExecute::test_cat_deterministic_content',
            'TestTerminalOutputCleanliness::test_cat',
        }),
    ],
    # 'test_mcp_stability.py' had one row (TestStdioPidTracking::
    # test_kill_orphaned_handles_dead_pids, "the live-system guard blocks
    # os.kill(999999999, 15)"). Retired 2026-08-01 (ledger item 7): with
    # psutil==7.2.2 installed the dead-pid probe no longer reaches that
    # os.kill, so the guard never fires and the test passes.
    'test_mcp_tool.py': [
        (_WINDOWS, "KeyError 'ProgramFiles' — Windows environment variables are "
         'case-insensitive and the test indexes a fixed casing that os.environ '
         'does not preserve', {
            'TestBuildSafeEnv::test_windows_location_vars_passed_without_secrets',
        }),
    ],
    'test_memory_tool.py': [
        (_WINDOWS, _SEP, {'TestMemoryStorePersistence::test_deduplication_on_load'}),
    ],
    'test_modal_sandbox_fixes.py': [
        (_WINDOWS, _SEP, {
            'TestCwdHandling::test_users_path_maps_to_workspace_for_docker_when_enabled',
            'TestCwdHandling::test_docker_default_cwd_maps_current_directory_when_enabled',
        }),
    ],
    'test_pr_6656_regressions.py': [
        (_WINDOWS, 'the bundle hash is taken over bytes read through the '
         'default text mode, so CRLF normalization on Windows makes the '
         'in-bundle and on-disk hashes diverge', {
            'TestBundleHashFilenameSensitivity::test_bundle_and_disk_hash_match',
        }),
    ],
    'test_search_error_guard.py': [
        (_HOST, 'the installed ripgrep (15.1.0) refuses the escaped literal '
         r'"\\n" at regex-parse time — "the literal \"\n\" is not allowed in a '
         'regex ... Consider enabling multiline mode" — for both the single- '
         'and double-backslash spellings, so search() returns an error before '
         'the newline-warning branch under test is ever reached. Verified by '
         'invoking rg directly with the exact argv the tool builds. Not a '
         'Windows gap: this row previously read as a Git Bash spawn-shape '
         'failure only because the MSYS-form path bug masked it — with that '
         'fixed, the real cause is the rg version on this host', {
            'TestSearchContentNewlineWarning::test_literal_backslash_n_pattern_does_not_warn',
        }),
    ],
    'test_skills_hub.py': [
        (_WINDOWS, 'content hashes are computed over text-mode reads, so CRLF '
         'normalization and the os.sep in bundle keys make the installed and '
         'bundled digests diverge on Windows', {
            'TestCheckForSkillUpdates::test_bundle_content_hash_matches_installed_content_hash',
            'TestOptionalSkillSourceBinaryAssets::test_fetch_preserves_binary_assets',
        }),
    ],
    'test_working_diff.py': [
        (
            _WINDOWS,
            'Path.write_text() emits CRLF on Windows, and the fixture runs git '
            'with HOME=<repo> so only the system gitconfig applies '
            '(core.autocrlf=true, the Git-for-Windows installer default) and '
            'the index is normalized to LF; collect_working_diff() then runs '
            'git with the ambient env where the user global sets '
            'core.autocrlf=false, so the CRLF worktree file differs from the '
            'LF index and the repo is not "clean"',
            {
                'test_clean_repo_reports_empty',
            },
        ),
    ],
    'test_zombie_process_cleanup.py': [
        (
            _WINDOWS,
            'the finally-block teardown calls os.kill(pid, signal.SIGKILL); '
            'signal.SIGKILL does not exist on Windows, so the test errors in '
            'cleanup after its assertions have already passed',
            {
                'TestZombieReproduction::test_orphaned_processes_survive_without_cleanup',
            },
        ),
    ],
}

_STALE = StaleEntryTracker(_ENV_GAPS, "tests/tools/conftest.py")


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register the environment-gap marks."""
    register_marks(config)


def pytest_collection_modifyitems(items):  # noqa: D401 — pytest hook
    """Attach the environment-gap mark to every registered node id."""
    apply_marks(items, _ENV_GAPS)


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record registered environment-gap node ids that actually passed."""
    _STALE.record(report)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Surface registry rows that no longer describe a real failure."""
    _STALE.report(terminalreporter)
