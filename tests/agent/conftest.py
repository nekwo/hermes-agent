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

2. ``_ENV_GAP_SKIPS`` — the shared environment-gap registry, in its
   probe-backed form (see ``tests/_env_gap_fence`` for the full contract). A
   row names a mechanism and carries a live probe; when the probe says the gap
   is present on this host the test is really SKIPPED, with the mechanism as
   the reason. When the probe says otherwise the test runs, and
   ``tests/test_env_gap_registry.py`` fails on the stale row.

   ``_ENV_GAPS`` (the older mark-only form, which left the tests failing) is
   now empty here: the 2026-08-10 audit found twenty of its twenty-four rows
   were stale tests or a real defect. See the block comment above it.
"""

from __future__ import annotations

import os

import pytest

from tests._env_gap_fence import (
    EnvGapRegistry,
    EnvGapSkipRegistry,
    StaleEntryTracker,
    apply_marks,
    apply_skips,
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

# ── Environment-gap registry (audited 2026-08-10) ───────────────────────────
#
# This directory registered 24 node ids across 12 files, all as pre-existing
# Windows/host gaps. The audit reproduced every one of them and found TWENTY
# were not gaps at all:
#
#   * agent/image_routing.py's _LOCAL_IMAGE_PATH_RE anchored only on `~/` and
#     `/`, though its own comment claims parity with gateway's
#     extract_local_files() — whose copy grew a Windows drive-letter anchor in
#     #34632. A REAL DEFECT: a Windows operator's pasted C:\...\shot.png was
#     silently dropped here while the identical path delivered via the gateway.
#     Fixed in the code, not fenced.
#   * ten rows asserted a POSIX path SPELLING (a "/" separator, a
#     "cache/images" substring, a lowercase env-var name that Windows
#     upper-cases, a UTF-8 file read back with the cp1252 default). Windows
#     reproduces every one of those behaviours; only the string differed.
#   * three rows monkeypatched HOME and expected `~` to follow. It does not on
#     Windows — ntpath.expanduser prefers USERPROFILE — so the test quietly
#     expanded ~ to the developer's REAL profile and stopped testing anything.
#     They now use tests._home_env.point_home_at.
#   * one leaked an open SessionDB into shutil.rmtree. Not a Windows quirk: a
#     thread and an atexit registration leaked on EVERY platform, and POSIX
#     merely unlinked the open file without complaining.
#   * one was non-hermetic — credential_pool._seed_from_singletons()
#     auto-discovers ~/.claude/.credentials.json, which HERMES_HOME does not
#     sandbox, so a test whose premise was "the ONLY pool entry" ran with two.
#   * one blamed cmd.exe for an inline `!`pwd`` snippet that run_inline_shell
#     executes under bash on every platform (it prefers Git Bash on Windows).
#     The stated reason was simply wrong; the real mechanism is that Git Bash
#     answers in MSYS form, which `pwd -W` fixes.
#
# Nothing is left in _ENV_GAPS. What remains is one genuine, probe-backed gap.
_ENV_GAPS: EnvGapRegistry = {}


def _shell_hook_scripts_are_not_directly_executable() -> bool:
    """True where a `#!`-line script cannot be handed to CreateProcess/execve.

    agent/shell_hooks.py runs a hook with ``subprocess.run(argv, shell=False)``
    where argv[0] is the script path. POSIX honours the shebang in execve;
    Windows has no shebang handling at all, so a `.sh` hook raises
    FileNotFoundError before any payload is produced. This is a real product
    gap (shell hooks do not function on Windows), not a test defect — see the
    audit note filed with this change.
    """
    return os.name == "nt"


_ENV_GAP_SKIPS: EnvGapSkipRegistry = {
    'test_shell_hooks.py': [
        (
            _shell_hook_scripts_are_not_directly_executable,
            'agent/shell_hooks.py spawns the hook via subprocess.run(argv, '
            'shell=False) with the script path as argv[0]; Windows CreateProcess '
            'has no shebang handling, so a POSIX hook script is not directly '
            'executable and the callback fails before the payload is produced',
            {
                'TestCallbackSubprocess::test_block_translation_end_to_end',
                'TestCallbackSubprocess::test_block_aggregation_through_plugin_manager',
                'TestCallbackSubprocess::test_matcher_regex_filters_callback',
                'TestCallbackSubprocess::test_payload_schema_delivered',
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
    apply_skips(items, _ENV_GAP_SKIPS)


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record registered environment-gap node ids that actually passed."""
    _STALE.record(report)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Surface registry rows that no longer describe a real failure."""
    _STALE.report(terminalreporter)
