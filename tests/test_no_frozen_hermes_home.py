"""Ratchet guard: no NEW module-level values frozen against ``HERMES_HOME``.

Resolving ``get_hermes_home()`` at module top binds the path to whatever
``HERMES_HOME`` was set when the module was first imported. ``get_hermes_home()``
reads the env *live* on every call, but a module-level constant does not, so the
two drift apart the moment anything changes ``HERMES_HOME`` after import — most
importantly the autouse ``_hermetic_environment`` test fixture, which redirects
``HERMES_HOME`` to a per-test tmpdir *after* collection has already imported the
module.

That is not hypothetical: it silently deposited fixture chat sessions into the
live ``state.db`` (surfaced as Mission Control's "projection drops" alert),
because a no-arg ``SessionDB()`` bound to the import-time ``DEFAULT_DB_PATH``
rather than the test's redirected home. The fix was to resolve the home at call
time — see ``hermes_state._resolve_default_db_path`` for the canonical pattern
(resolve live via ``get_hermes_home()``; honor an explicitly reassigned constant
so ``monkeypatch.setattr`` isolation keeps working).

How this is checked
-------------------
By running the hazard, not by reading for it. A subprocess points ``HERMES_HOME``
at a fresh tmpdir, imports every module that mentions ``get_hermes_home()``, and
reports each module-level attribute whose value still contains that tmpdir. A
plain module attribute cannot re-resolve, so "resolved from the home at import"
and "frozen" are the same statement.

This replaced a column-0 regex over the source (``^NAME = ...get_hermes_home()``),
which could only see the *first* hop. The regex reported 29 frozen names; the
probe finds 51, because the ones it missed are derived — ``CRON_DIR =
get_hermes_home() / "cron"`` was caught, but ``JOBS_FILE = CRON_DIR /
"jobs.json"`` right below it was not, and JOBS_FILE is every bit as frozen.
Six of ``cron/jobs.py``'s constants are frozen where the regex saw one; seven of
``gateway/platforms/base.py``'s where it saw one.

The ledger below is therefore the honest debt, not the syntactically visible
slice of it. Almost all of it lives in upstream NousResearch files (``gateway/``,
``tools/``, ``cron/``, ``cli.py``) where a fork rewrite would collide on the next
upstream sync, so it is tracked rather than converted wholesale. When you touch
an allowlisted module, prefer converting it to call-time resolution and dropping
its entry — the test fails on a stale entry, so shrinking the ledger is enforced,
not merely encouraged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that never participate in the import-time hazard (test code may
# legitimately snapshot; the rest are build/vendor/runtime artifacts).
SKIP_DIRS = (
    ".git", ".venv", "venv", "build", "dist", "__pycache__",
    "node_modules", ".hermes", ".pytest_cache", "tests", "web",
)

# Module-level names whose value is resolved from HERMES_HOME at import time,
# keyed by repo-relative path. Every file carries the reason its freeze is
# tolerated. Measured by the probe below, not read off the source.
FROZEN_LEDGER: dict[str, tuple[frozenset[str], str]] = {
    "agent/auxiliary_client.py": (
        frozenset({"_AUTH_JSON_PATH"}),
        "upstream: auxiliary provider auth path",
    ),
    "cli.py": (
        frozenset({"_hermes_home"}),
        "upstream: CLI entry resolves the home once at startup",
    ),
    "cron/executions.py": (
        frozenset({"EXECUTIONS_FILE"}),
        "upstream: cron execution ledger path",
    ),
    "cron/jobs.py": (
        frozenset({
            "CRON_DIR", "HERMES_DIR", "JOBS_FILE", "OUTPUT_DIR",
            "TICKER_HEARTBEAT_FILE", "TICKER_SUCCESS_FILE",
        }),
        "upstream: cron store layout, all derived from one frozen HERMES_DIR",
    ),
    "cron/suggestions.py": (
        frozenset({"CRON_DIR", "SUGGESTIONS_FILE"}),
        "upstream: cron suggestion store",
    ),
    "gateway/channel_directory.py": (
        frozenset({"CHANNEL_ALIASES_PATH", "DIRECTORY_PATH"}),
        "upstream: gateway channel directory",
    ),
    "gateway/hooks.py": (
        frozenset({"HOOKS_DIR"}),
        "upstream: gateway hook directory",
    ),
    "gateway/mirror.py": (
        frozenset({"_SESSIONS_DIR", "_SESSIONS_INDEX"}),
        "upstream: gateway session mirror",
    ),
    "gateway/pairing.py": (
        frozenset({"PAIRING_DIR"}),
        "upstream: gateway pairing store",
    ),
    "gateway/platforms/base.py": (
        frozenset({
            "AUDIO_CACHE_DIR", "DOCUMENT_CACHE_DIR", "IMAGE_CACHE_DIR",
            "SCREENSHOT_CACHE_DIR", "VIDEO_CACHE_DIR", "_HERMES_HOME",
            "_HERMES_ROOT",
        }),
        "upstream: platform adapter media caches, derived from one frozen root",
    ),
    "gateway/run.py": (
        frozenset({"_config_path", "_env_path", "_hermes_home"}),
        "upstream: gateway process entry resolves the home once at startup",
    ),
    "gateway/sticker_cache.py": (
        frozenset({"CACHE_PATH"}),
        "upstream: gateway sticker cache",
    ),
    "hermes_cli/claw.py": (
        frozenset({"_OPENCLAW_SCRIPT_INSTALLED"}),
        "upstream: openclaw install marker path",
    ),
    "hermes_cli/doctor.py": (
        frozenset({"_DHH"}),
        "display LABEL only, never a filesystem read; run_doctor resolves the "
        "home and its .env at call time",
    ),
    "hermes_cli/web_server.py": (
        frozenset({"_ACTION_LOG_DIR"}),
        "upstream: dashboard action log directory",
    ),
    "hermes_state.py": (
        frozenset({"DEFAULT_DB_PATH", "_IMPORT_DEFAULT_DB_PATH"}),
        "upstream, and benign: SessionDB resolves live via "
        "_resolve_default_db_path and only falls back to DEFAULT_DB_PATH when "
        "it has been deliberately reassigned. _IMPORT_DEFAULT_DB_PATH exists "
        "precisely to detect that reassignment.",
    ),
    "plugins/platforms/feishu/feishu_comment_rules.py": (
        frozenset({"PAIRING_FILE", "RULES_FILE"}),
        "upstream plugin: feishu comment rule store",
    ),
    "plugins/platforms/matrix/adapter.py": (
        frozenset({"_CRYPTO_DB_PATH", "_STORE_DIR"}),
        "upstream plugin: matrix crypto store",
    ),
    "run_agent.py": (
        frozenset({"_hermes_home"}),
        "upstream: process entry resolves the home once at startup",
    ),
    "skills/productivity/google-workspace/scripts/google_api.py": (
        frozenset({"CLIENT_SECRET_PATH", "HERMES_HOME", "TOKEN_PATH"}),
        "upstream skill script: google credential paths. Not importable by "
        "dotted name (dashed directory) — loaded by path, and it freezes the "
        "same way, which is why the probe loads it at all",
    ),
    "skills/productivity/google-workspace/scripts/setup.py": (
        frozenset({
            "CLIENT_SECRET_PATH", "HERMES_HOME", "PENDING_AUTH_PATH", "TOKEN_PATH",
        }),
        "upstream skill script: google oauth setup paths, same dashed-directory "
        "case as google_api.py",
    ),
    "tools/checkpoint_manager.py": (
        frozenset({"CHECKPOINT_BASE"}),
        "upstream tool: checkpoint store root",
    ),
    # ``tools/environments/modal.py`` used to sit here for ``_SNAPSHOT_STORE``,
    # for exactly the reason recorded for ``singularity.py`` below and on the
    # same import chain — ``tools/terminal_tool.py`` imports both, so
    # ``spawn_local`` resolved BOTH homes at first-call import time. Retiring
    # only one just moved the traceback down a frame. Now the lazy
    # ``_snapshot_store_path()``.
    # ``tools/environments/singularity.py`` used to sit here for
    # ``_SNAPSHOT_STORE``. The freeze was not merely untidy: this module is
    # imported at the top of ``tools/terminal_tool.py``, which
    # ``ProcessRegistry.spawn_local`` imports on its first call, so the
    # import-time ``get_hermes_home()`` ran inside whatever environment the
    # first spawn happened to have. A caller that legitimately scrubs the
    # environment took a ``RuntimeError: Could not determine home directory``
    # out of a *snapshot-store path* it never touches. Now the lazy
    # ``_snapshot_store_path()``, matching ``vercel_sandbox.py``.
    # ``tools/process_registry.py`` used to sit here for ``CHECKPOINT_PATH``.
    # The Activity projection made the freeze load-bearing rather than merely
    # untidy — a second process reading the checkpoint has to agree with the
    # writer about WHERE it is, and an import-time bind made that depend on when
    # the module first got imported relative to a persona-profile home flip. The
    # constant is now the lazy ``checkpoint_path()``, so the ledger row is gone
    # rather than re-worded.
    "tools/skill_manager_tool.py": (
        frozenset({"HERMES_HOME", "SKILLS_DIR", "_SKILLS_DIR_AT_IMPORT"}),
        "upstream tool: skills root; _SKILLS_DIR_AT_IMPORT is a deliberate "
        "import-time snapshot used to detect reassignment",
    ),
    "tools/skills_sync.py": (
        frozenset({
            "HERMES_HOME", "MANIFEST_FILE", "SKILLS_DIR",
            "_HERMES_HOME_AT_IMPORT", "_MANIFEST_FILE_AT_IMPORT",
            "_SKILLS_DIR_AT_IMPORT",
        }),
        "upstream tool: skills sync layout; the *_AT_IMPORT trio are "
        "deliberate snapshots used to detect reassignment",
    ),
    "tools/skills_tool.py": (
        frozenset({"HERMES_HOME", "SKILLS_DIR", "_SKILLS_DIR_AT_IMPORT"}),
        "upstream tool: skills root; _SKILLS_DIR_AT_IMPORT is a deliberate "
        "import-time snapshot used to detect reassignment",
    ),
    "tools/tts_tool.py": (
        frozenset({"DEFAULT_OUTPUT_DIR"}),
        "upstream tool: TTS output directory",
    ),
    "tui_gateway/server.py": (
        frozenset({"_CRASH_LOG", "_hermes_home"}),
        "upstream: TUI gateway process entry",
    ),
    # ── Carried, not measured ──────────────────────────────────────────────
    # Entries here come from the pre-probe regex ledger rather than from a run,
    # because the module does not import on this platform (see UNPROBED). They
    # are listed anyway so the guard does not report them as NEW on a host where
    # they DO import. If that host finds additional derived names — likely,
    # since the regex only ever saw the first hop — the failure message names
    # them and they belong here.
    #
    # trajectory_compressor.py has since GRADUATED out of this bucket: `fire`
    # was installed on the ambient interpreter on 2026-08-01 (ledger item 7,
    # RULED EXECUTE), the probe imported the module for the first time, and the
    # measurement CONFIRMED the carried regex ledger exactly — `_hermes_home`
    # and nothing else. The regex's "first hop only" worry did not materialize.
    # It keeps its UNPROBED row as well, deliberately: that row is what a host
    # WITHOUT `fire` needs, and per test_ledger_reasons_are_present both entries
    # are legitimate at once so long as the module is ledgered here.
    "trajectory_compressor.py": (
        frozenset({"_hermes_home"}),
        "upstream: process entry. Measured 2026-08-01 once `fire` was installed; "
        "confirms the carried regex ledger with no additional derived names",
    ),
    "scripts/profile-tui.py": (
        frozenset({"DEFAULT_LOG", "DEFAULT_STATE_DB"}),
        "upstream script: TUI profiler log/db paths. Carried from the regex "
        "ledger; needs a POSIX host (termios) to be measured",
    ),
}

# Modules the probe could not import in this environment, with the reason. An
# import failure is not a pass: it means the module was never checked, so it is
# recorded here rather than silently dropped.
UNPROBED: dict[str, str] = {
    "trajectory_compressor.py": (
        "imports `fire` at module level, a declared dependency absent from some "
        "test environments. Probed wherever fire is installed — including this "
        "ambient interpreter since 2026-08-01, where it now imports and its "
        "FROZEN_LEDGER entry is measured rather than carried. The row stays for "
        "hosts that still lack `fire`."
    ),
    "scripts/profile-tui.py": (
        "imports `termios`, a POSIX-only stdlib module, so it cannot load on "
        "Windows. Probed on any POSIX host."
    ),
}

_PROBE = r'''
import importlib, importlib.util, json, os, sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
skip = set(json.loads(sys.argv[2]))
out_path = Path(sys.argv[3])
# The redirected home's leaf directory carries a nonce, and we look for THAT
# rather than the full path: display_hermes_home() rewrites the prefix to "~/",
# and different modules store forward- or back-slashed forms. The nonce
# survives every one of those rewrites, so detection does not depend on where
# the temp directory happens to live.
nonce = sys.argv[4].lower()
sys.path.insert(0, str(repo))

frozen, failed = {}, {}

for path in sorted(repo.rglob("*.py")):
    rel = path.relative_to(repo)
    if any(part in skip for part in rel.parts) or path.name == "__main__.py":
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        continue
    if "get_hermes_home()" not in text:
        continue
    key = rel.as_posix()
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    dotted = ".".join(parts)
    importable = bool(parts) and all(p.isidentifier() for p in parts)
    try:
        if importable:
            module = importlib.import_module(dotted)
        else:
            # Scripts under dashed directories (scripts/profile-tui.py,
            # skills/.../google_api.py) are not importable by dotted name but
            # freeze exactly the same way — load them by path.
            spec = importlib.util.spec_from_file_location(
                "frozen_probe_" + str(abs(hash(key))), path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    except BaseException as exc:
        failed[key] = type(exc).__name__ + ": " + str(exc)[:200]
        continue
    hits = []
    for name, value in list(vars(module).items()):
        if name.startswith("__"):
            continue
        if isinstance(value, Path):
            text_value = str(value)
        elif isinstance(value, str):
            text_value = value
        else:
            continue
        if nonce in text_value.lower():
            hits.append(name)
    if hits:
        frozen[key] = sorted(hits)

# Written to a file, not stdout: importing the gateway/TUI modules swaps
# sys.stdout, so a print here lands wherever they redirected it.
out_path.write_text(json.dumps({"frozen": frozen, "failed": failed}), encoding="utf-8")
'''


@pytest.fixture(scope="module")
def probe_result(tmp_path_factory) -> dict:
    """Import every get_hermes_home() module under a redirected home.

    Run out-of-process: importing ~220 modules mutates sys.modules and builds a
    whole home skeleton on disk, neither of which belongs in the test process.
    """

    workdir = tmp_path_factory.mktemp("frozen_home_probe")
    nonce = "hermesfrozenprobe" + uuid.uuid4().hex[:10]
    home = workdir / nonce
    home.mkdir()
    result_path = workdir / "probe.json"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            str(REPO_ROOT),
            json.dumps(list(SKIP_DIRS)),
            str(result_path),
            nonce,
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert result_path.is_file(), (
        "frozen-home probe produced no result\n"
        f"exit={completed.returncode}\n"
        f"stdout tail:\n{completed.stdout[-2000:]}\n"
        f"stderr tail:\n{completed.stderr[-2000:]}"
    )
    return json.loads(result_path.read_text(encoding="utf-8"))


@pytest.mark.timeout(300)
def test_no_new_frozen_hermes_home_values(probe_result: dict) -> None:
    found = {
        (rel, name)
        for rel, names in probe_result["frozen"].items()
        for name in names
    }
    ledgered = {
        (rel, name) for rel, (names, _reason) in FROZEN_LEDGER.items() for name in names
    }

    new = sorted(found - ledgered)
    assert not new, (
        "New module-level value(s) frozen against HERMES_HOME at import:\n"
        + "\n".join(f"  {rel}: {name}" for rel, name in new)
        + "\n\nA module-level constant freezes HERMES_HOME at import and breaks "
        "test isolation / in-process profile switches. Resolve the path at call "
        "time instead — see hermes_state._resolve_default_db_path for the "
        "pattern. If the value genuinely cannot be lazy, add it to FROZEN_LEDGER "
        "in this test with a justification."
    )


@pytest.mark.timeout(300)
def test_frozen_ledger_has_no_stale_entries(probe_result: dict) -> None:
    found = {
        (rel, name)
        for rel, names in probe_result["frozen"].items()
        for name in names
    }
    failed = set(probe_result["failed"])
    stale = sorted(
        (rel, name)
        for rel, (names, _reason) in FROZEN_LEDGER.items()
        for name in names
        # A module that would not import was never checked; absence there is
        # not evidence the freeze is gone.
        if rel not in failed and (rel, name) not in found
    )
    assert not stale, (
        "Ledgered frozen value(s) no longer present:\n"
        + "\n".join(f"  {rel}: {name}" for rel, name in stale)
        + "\n\nNice — fewer frozen homes. Remove the entry/entries above from "
        "FROZEN_LEDGER so the debt ledger stays honest."
    )


@pytest.mark.timeout(300)
def test_probe_import_failures_are_declared(probe_result: dict) -> None:
    """An unimportable module is unchecked, not clean — say so out loud.

    Optional third-party dependencies legitimately go missing in some
    environments; anything else (a NameError, a SyntaxError, a broken
    module-level side effect) is a real defect wearing an import failure.
    """

    failed = probe_result["failed"]
    undeclared = sorted(set(failed) - set(UNPROBED))
    assert not undeclared, (
        "Module(s) the frozen-home probe could not import, and which are not "
        "declared in UNPROBED:\n"
        + "\n".join(f"  {rel}: {failed[rel]}" for rel in undeclared)
        + "\n\nIf this is a missing optional dependency, add it to UNPROBED with "
        "that reason. If it is anything else, it is a real import-time defect: "
        "fix the module."
    )
    for rel, detail in failed.items():
        assert detail.split(":", 1)[0] in {"ImportError", "ModuleNotFoundError"}, (
            f"{rel} failed the frozen-home probe with a non-import error, which "
            f"UNPROBED does not excuse: {detail}"
        )


@pytest.mark.timeout(300)
def test_ledger_reasons_are_present(probe_result: dict) -> None:
    """Every ledger entry carries a reason; no silent debt."""

    missing = sorted(rel for rel, (_names, reason) in FROZEN_LEDGER.items() if not reason.strip())
    assert not missing, f"FROZEN_LEDGER entries without a reason: {missing}"
    unreasoned = sorted(rel for rel, reason in UNPROBED.items() if not reason.strip())
    assert not unreasoned, f"UNPROBED entries without a reason: {unreasoned}"

    # UNPROBED is per-environment: a module that needs `fire` or `termios`
    # imports fine on a host that has them, and then it must ALSO be ledgered
    # so the guard does not report its freezes as new. Both entries are
    # therefore legitimate at once — what is not legitimate is an UNPROBED
    # entry that neither fails to import nor freezes anything anywhere.
    inert = sorted(
        rel
        for rel in UNPROBED
        if rel not in probe_result["failed"]
        and rel not in probe_result["frozen"]
        and rel not in FROZEN_LEDGER
    )
    assert not inert, (
        "These files are declared UNPROBED but imported fine here and froze "
        f"nothing: {inert} — drop the UNPROBED entry."
    )
