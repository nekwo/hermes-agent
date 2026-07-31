"""Environment fences for ``tests/agent`` (added by the 2026-07-31 upstream sync).

Two independent mechanisms live here. Neither weakens an assertion.

1. ``_SLOW_LOOPBACK_TIMEOUT_NODE_IDS`` — a per-test WATCHDOG CEILING raise, not
   a skip. The listed tests construct a real ``AIAgent`` against a local
   ``base_url``; ``agent_init`` then probes that endpoint several times over
   httpx (``detect_local_server_type`` / ``query_ollama_num_ctx``). On a host
   whose loopback stack answers instantly the whole file costs under a second.
   On this workstation a *refused* ``127.0.0.1`` connect costs ~2.0-2.4s and a
   failing ``getaddrinfo`` ~1.0s (measured), so those probes push three of the
   tests to 36-39s and a fourth to 24s against the 30s ``--timeout`` in
   ``pyproject.toml``. Because ``--timeout-method=thread`` kills the WHOLE
   interpreter, one over-budget test takes the entire file with it and the
   parallel runner reports "no tests ran" for a file where nothing is wrong.

   Raising the ceiling for exactly those node ids masks nothing: every
   assertion still executes and any real failure still fails. The only thing
   given up is catching a genuine hang in those four tests within 30s, and
   ``scripts/run_tests_parallel.py``'s 300s per-file process kill still bounds
   that. Unlike a skip, this needs no host probe to stay honest — on a fast
   host the tests finish in under a second and never reach the raised ceiling.

2. ``_ENV_GAPS`` — the shared environment-gap registry (see
   ``tests/_env_gap_fence`` for the full contract). Registered tests still run
   and still fail loudly; the mark only lets a run deselect them:

       python -m pytest tests/agent -m "not windows_env_gap and not host_dependency_gap"
"""

from __future__ import annotations

import pytest

from tests._env_gap_fence import (
    HOST_DEPENDENCY_GAP as _HOST,
    WINDOWS_ENV_GAP as _WINDOWS,
    EnvGapRegistry,
    StaleEntryTracker,
    apply_marks,
    register_marks,
)

# Per-test ceiling for the AIAgent-constructing tests described above. Sized so
# the slowest measured run (38.7s on this host) has ~3x headroom while the sum
# across a single file stays under the runner's 300s per-file process kill.
_SLOW_LOOPBACK_TIMEOUT_SECONDS = 120

_SLOW_LOOPBACK_TIMEOUT_NODE_IDS: dict[str, frozenset[str]] = {
    # base_url="http://test" — every probe pays a ~1.0s getaddrinfo failure.
    'test_skip_memory_store_65429.py': frozenset({
        'test_skip_memory_with_memory_toolset_creates_store',        # 38.7s
        'test_skip_memory_memory_tool_handler_works_and_provider_skipped',  # 24.5s
    }),
    # base_url="http://127.0.0.1:8000/v1" — every probe pays a ~2.0s refusal.
    'test_verification_stop_caching.py': frozenset({
        'test_db_flush_drops_only_nudge_keeps_candidate',   # 38.3s
        'test_json_log_drops_only_nudge_keeps_candidate',   # 36.2s
    }),
}

# Every row below was reproduced individually on this Windows 10 workstation,
# traced to a concrete host/platform cause, and proven PRE-EXISTING by running
# the same node on the pre-merge fork tip `1adf0404f` — where each of these
# files failed with a superset of these node ids (upstream's prune waves
# removed the rest). The one exception is `test_credential_pool_routing.py`,
# whose node arrived WITH the merge (upstream-only test, upstream-identical
# production code) and is a property of this host, not of the merge.
#
# The one genuine merge-resolution defect found in this directory
# (`agent/prompt_builder.py` lost the fork's guarded
# `skill_matches_environment` import when the merge took upstream's
# consolidated import block, breaking the fork-owned regression test
# `test_skill_environment_fallback.py`) was FIXED in the code, not fenced.
_ENV_GAPS: EnvGapRegistry = {
    'test_codex_app_server_persist.py': [
        (
            _WINDOWS,
            'the temp-dir teardown unlinks state.db while the SQLite handle is '
            'still open; Windows refuses with WinError 32 where POSIX allows '
            'unlinking an open file',
            {
                'test_codex_turn_persists_each_message_exactly_once',
            },
        ),
    ],
    'test_credential_pool_routing.py': [
        (
            _HOST,
            'this host has a Claude Code OAuth credential on disk and anthropic '
            'explicitly configured, so credential_pool._seed_from_singletons() '
            'auto-discovers it into every anthropic pool — HERMES_HOME does not '
            'sandbox that source. The test needs the seeded entry to be the '
            'ONLY pool entry; with a second one present the recovery correctly '
            'rotates and returns True',
            {
                'TestFailureAttribution::test_unmatched_key_does_not_retry_only_pool_entry',
            },
        ),
    ],
    'test_curator_classification.py': [
        (
            _WINDOWS,
            'report.md is written UTF-8 and read back with the Windows default '
            'cp1252, so the em dash in "Pruned — archived for staleness" '
            'arrives mojibaked and the section assertion cannot match',
            {
                'test_report_md_splits_consolidated_and_pruned_sections',
            },
        ),
    ],
    'test_file_safety_sandbox_mirror.py': [
        (
            _WINDOWS,
            'asserts sandbox-mirror paths in their POSIX spelling '
            '("profiles/default/cron/jobs.json", '
            '"sandboxes/docker/default/home/.hermes"); the classifier builds '
            'them with os.sep, so on Windows they carry backslashes',
            {
                'TestClassifySandboxMirrorTarget::test_docker_mirror_soul_md_classified',
                'TestClassifySandboxMirrorTarget::test_other_backends_and_inner_files_match[docker-profiles/coder/memories/MEMORY.md]',
                'TestClassifySandboxMirrorTarget::test_other_backends_and_inner_files_match[daytona-profiles/default/cron/jobs.json]',
                'TestGetSandboxMirrorWarning::test_mirror_warning_names_mirror_root_and_inner_path',
            },
        ),
    ],
    'test_image_routing.py': [
        (
            _WINDOWS,
            "extract_image_refs()'s path pattern is POSIX-shaped, so a Windows "
            'absolute path (drive letter + backslashes) is never recognised as '
            'an image reference and the expected list comes back empty',
            {
                'TestExtractImageRefs::test_finds_absolute_path',
                'TestExtractImageRefs::test_finds_home_relative_path',
            },
        ),
    ],
    'test_proxy_and_url_validation.py': [
        (
            _WINDOWS,
            'asserts the lowercase env-var name in the error message; Windows '
            'environment variables are case-insensitive and os.environ '
            'upper-cases them, so the message names HTTP_PROXY / HTTPS_PROXY / '
            'ALL_PROXY and the regex cannot match',
            {
                'test_proxy_env_rejects_malformed_port[http_proxy]',
                'test_proxy_env_rejects_malformed_port[https_proxy]',
                'test_proxy_env_rejects_malformed_port[all_proxy]',
            },
        ),
    ],
    'test_save_url_image.py': [
        (
            _WINDOWS,
            'asserts the POSIX substring "cache/images" in a path the code '
            'builds with os.sep, so on Windows it reads "cache\\images"',
            {
                'TestSaveUrlImage::test_writes_real_bytes_to_hermes_home_cache',
            },
        ),
    ],
    'test_shell_hooks.py': [
        (
            _WINDOWS,
            'the fixtures write POSIX shell scripts and rely on the shebang / '
            'executable bit to run them; Windows has neither, so every callback '
            'subprocess fails with "command not found" before the hook payload '
            'is produced',
            {
                'TestCallbackSubprocess::test_block_translation_end_to_end',
                'TestCallbackSubprocess::test_block_aggregation_through_plugin_manager',
                'TestCallbackSubprocess::test_matcher_regex_filters_callback',
                'TestCallbackSubprocess::test_payload_schema_delivered',
            },
        ),
    ],
    'test_shell_hooks_consent.py': [
        (
            _WINDOWS,
            'monkeypatches HOME and expects "~" to expand to it; '
            'os.path.expanduser prefers USERPROFILE on Windows, so the '
            'approved tilde path resolves elsewhere and no mtime is recorded',
            {
                'TestAllowlistOps::test_tilde_path_approval_records_resolvable_mtime',
            },
        ),
    ],
    # tests/agent/lsp/ — this conftest governs the whole tests/agent subtree,
    # and the registry keys on the file BASENAME, so nested files register here.
    'test_workspace.py': [
        (
            _WINDOWS,
            'monkeypatches HOME=/home/user and expects normalize_path("~/x.py") '
            'to expand to it; os.path.expanduser prefers USERPROFILE on '
            'Windows, so it expands to the real user profile instead',
            {
                'test_normalize_path_expands_tilde',
            },
        ),
    ],
    'test_skill_commands.py': [
        (
            _WINDOWS,
            'the supporting-files block emits str(Path.relative_to(...)), which '
            'is "scripts\\run.js" on Windows; the test asserts the POSIX '
            'spelling "scripts/run.js"',
            {
                'TestSkillDirectoryHeader::test_supporting_files_shown_with_absolute_paths',
            },
        ),
        (
            _WINDOWS,
            "the inline-shell snippet is !`pwd`, which has no cmd.exe builtin, "
            'so the expansion never yields the Windows skill directory the test '
            'asserts',
            {
                'TestInlineShellExpansion::test_inline_shell_runs_in_skill_directory',
            },
        ),
    ],
}

_STALE = StaleEntryTracker(_ENV_GAPS, "tests/agent/conftest.py")


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register the environment-gap marks."""
    register_marks(config)


def pytest_collection_modifyitems(items):  # noqa: D401 — pytest hook
    """Raise the watchdog ceiling for the slow-probe tests, then apply the marks."""
    for item in items:
        node_ids = _SLOW_LOOPBACK_TIMEOUT_NODE_IDS.get(item.path.name)
        if node_ids is None:
            continue
        _, _, within_file = item.nodeid.partition("::")
        if within_file in node_ids:
            item.add_marker(pytest.mark.timeout(_SLOW_LOOPBACK_TIMEOUT_SECONDS))
    apply_marks(items, _ENV_GAPS)


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record registered environment-gap node ids that actually passed."""
    _STALE.record(report)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Surface registry rows that no longer describe a real failure."""
    _STALE.report(terminalreporter)
