"""Ratchet guard: no NEW module-level ``get_hermes_home()`` snapshots.

Assigning ``X = get_hermes_home() / ...`` at module top binds the path to
whatever ``HERMES_HOME`` was set when the module was first imported. Because
``get_hermes_home()`` reads the env *live* on every call but this constant does
not, the two drift apart the moment anything changes ``HERMES_HOME`` after
import — most importantly the autouse ``_hermetic_environment`` test fixture,
which redirects ``HERMES_HOME`` to a per-test tmpdir *after* collection has
already imported the module.

That is not hypothetical: it silently deposited fixture chat sessions into the
live ``state.db`` (surfaced as Mission Control's "projection drops" alert),
because a no-arg ``SessionDB()`` bound to the import-time ``DEFAULT_DB_PATH``
rather than the test's redirected home. The fix was to resolve the home at call
time — see ``hermes_state._resolve_default_db_path`` for the canonical pattern
(resolve live via ``get_hermes_home()``; honor an explicitly reassigned
constant so ``monkeypatch.setattr`` isolation keeps working).

This test freezes the set of pre-existing snapshots so the class cannot GROW.
When you touch one of the allowlisted modules, prefer converting it to
call-time resolution and removing its allowlist entry. Do not add new entries
without a concrete reason the value genuinely cannot be resolved lazily.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that never participate in the import-time hazard (test code may
# legitimately snapshot; the rest are build/vendor/runtime artifacts).
_SKIP_DIRS = {
    ".git", ".venv", "venv", "build", "dist", "__pycache__",
    "node_modules", ".hermes", ".pytest_cache", "tests",
}

# Column-0 assignment whose RHS calls get_hermes_home(). Anchored at start of
# line so indented (function-local) uses — which re-resolve on each call and
# are safe — do not match. ``get_hermes_home_override()`` etc. are excluded by
# the literal ``()``.
_MODULE_SNAPSHOT = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*[^\n]*get_hermes_home\(\)")

# Pre-existing snapshots as of 2026-07-13. Almost all live in upstream
# NousResearch files (gateway/, tools/, plugins/, cli.py) where a fork rewrite
# would create merge conflicts on the next upstream sync, so they are tracked
# debt rather than converted wholesale. ``hermes_state.py:DEFAULT_DB_PATH``
# remains but is now benign — ``SessionDB`` resolves live and only falls back to
# the constant when it is explicitly reassigned.
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    ("agent/auxiliary_client.py", "_AUTH_JSON_PATH"),
    ("cli.py", "_hermes_home"),
    ("cron/jobs.py", "HERMES_DIR"),
    ("cron/suggestions.py", "CRON_DIR"),
    ("gateway/channel_directory.py", "CHANNEL_ALIASES_PATH"),
    ("gateway/channel_directory.py", "DIRECTORY_PATH"),
    ("gateway/hooks.py", "HOOKS_DIR"),
    ("gateway/mirror.py", "_SESSIONS_DIR"),
    ("gateway/platforms/base.py", "_HERMES_HOME"),
    ("gateway/run.py", "_hermes_home"),
    ("gateway/sticker_cache.py", "CACHE_PATH"),
    ("hermes_cli/doctor.py", "HERMES_HOME"),
    ("hermes_cli/web_server.py", "_ACTION_LOG_DIR"),
    ("hermes_state.py", "DEFAULT_DB_PATH"),
    ("plugins/platforms/feishu/feishu_comment_rules.py", "PAIRING_FILE"),
    ("plugins/platforms/feishu/feishu_comment_rules.py", "RULES_FILE"),
    ("run_agent.py", "_hermes_home"),
    ("scripts/profile-tui.py", "DEFAULT_LOG"),
    ("scripts/profile-tui.py", "DEFAULT_STATE_DB"),
    ("skills/productivity/google-workspace/scripts/google_api.py", "HERMES_HOME"),
    ("skills/productivity/google-workspace/scripts/setup.py", "HERMES_HOME"),
    ("tools/checkpoint_manager.py", "CHECKPOINT_BASE"),
    ("tools/environments/modal.py", "_SNAPSHOT_STORE"),
    ("tools/environments/singularity.py", "_SNAPSHOT_STORE"),
    ("tools/process_registry.py", "CHECKPOINT_PATH"),
    ("tools/skill_manager_tool.py", "HERMES_HOME"),
    ("tools/skills_sync.py", "HERMES_HOME"),
    ("tools/skills_tool.py", "HERMES_HOME"),
    ("trajectory_compressor.py", "_hermes_home"),
    ("tui_gateway/server.py", "_hermes_home"),
})


def _scan_module_snapshots() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in REPO_ROOT.rglob("*.py"):
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "get_hermes_home()" not in text:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = _MODULE_SNAPSHOT.match(line)
            if match:
                found.add((rel, match.group(1)))
    return found


def test_no_new_module_level_hermes_home_snapshots():
    found = _scan_module_snapshots()

    new = sorted(found - _ALLOWLIST)
    assert not new, (
        "New module-level get_hermes_home() snapshot(s) detected:\n"
        + "\n".join(f"  {f}: {name}" for f, name in new)
        + "\n\nA module-level constant freezes HERMES_HOME at import and breaks "
        "test isolation / in-process profile switches. Resolve the path at call "
        "time instead — see hermes_state._resolve_default_db_path for the "
        "pattern. If the value genuinely cannot be lazy, add it to _ALLOWLIST in "
        "this test with a justification comment."
    )

    stale = sorted(_ALLOWLIST - found)
    assert not stale, (
        "Allowlisted get_hermes_home() snapshot(s) no longer present:\n"
        + "\n".join(f"  {f}: {name}" for f, name in stale)
        + "\n\nNice — one fewer frozen home. Remove the entr"
        "y/entries above from _ALLOWLIST so the debt ledger stays honest."
    )
