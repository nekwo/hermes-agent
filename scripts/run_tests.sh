#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then the shared
#     canonical test venv — $HERMES_TEST_VENV, ~/.venvs/hermes-test —
#     then ~/.hermes/...). A worktree with no .venv of its own finds
#     the shared one with no env vars set.
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).
#
# ── Running tests/hermes_cli (and why through here) ─────────────────────────
#
#   scripts/run_tests.sh tests/hermes_cli -j 6
#
# This script is THE checkpoint runner for that directory, and a red is
# defined as "any FILE red under it". Running `pytest tests/hermes_cli`
# directly asks a different question: 568 files then share one interpreter,
# and ~41 failures appear in test_web_server_* / test_web_ui_build.py that are
# green when each file is run alone (ledger row F3). That is module-level
# state leaking across files — the exact thing per-file spawning exists to
# prevent — so it is a fact about the interpreter, not a defect in those
# tests, and it is not being chased (ML-7 / operator ruling R-e, 2026-08-18).
#
# If this script refuses with "no virtualenv with pytest found", it lists every
# candidate it probed; each one either does not exist or has no pytest
# INSTALLED — an empty `.venv/` directory counts as the former. The fix is to
# build the shared canonical venv (recipe in the probe comment below) or point
# the script at one you already have:
#
#   HERMES_TEST_VENV=/path/to/venv scripts/run_tests.sh tests/hermes_cli
#   HERMES_PYTHON=/c/Python312/python.exe scripts/run_tests.sh tests/hermes_cli
#
# Prefer HERMES_TEST_VENV: it takes a venv (so its pins are whatever that venv
# pins), where HERMES_PYTHON takes any interpreter — including a system one
# whose site-packages shadow this repo's pins. Refusing is deliberate; a venv
# without pytest reports "0 tests passed", which reads green at a glance.

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Locate python ───────────────────────────────────────────────────────────
# Probe local venvs first; fall back to the Nix devShell's editable venv
# (HERMES_PYTHON is exported by the devShell hook and ships [dev] extras:
# pytest, pytest-asyncio, pytest-timeout, ruff, ty).
#
# A candidate must have pytest INSTALLED, not merely exist. The release venv
# at ~/.hermes/hermes-agent/venv has bin/activate but no pytest, so an
# existence-only probe selected it in checkouts/worktrees without a local
# .venv — every file then died with "No module named pytest" and the run
# reported "0 tests passed" (which reads green at a glance even though the
# exit code is 1). Skip such a venv and keep probing instead.
#
# ── The canonical SHARED test venv ────────────────────────────────────────
#
# A per-checkout ``.venv`` is the right answer for one checkout and the wrong
# answer for a machine that carries several. This repo is worked in worktrees
# (``git worktree add``), and a fresh worktree has no ``.venv`` — so every
# wave that ran a suite from one had to hand-carry ``HERMES_PYTHON=`` pointed
# at whatever interpreter happened to have pytest. On the workstation that
# found this, that was the system ``C:\Python312``: a grab-bag with
# ``packaging==26.2`` shadowing the repo's pinned ``26.0``, ``mcp==1.28.1``
# against the pinned ``1.26.0`` and ``starlette==1.0.0`` against ``1.6.0``.
# Different pins per wave is not a test environment, it is three of them.
#
# So the probe now also looks for a venv that lives OUTSIDE every checkout and
# is therefore shared by all of them, in this order:
#
#   1. ``$HERMES_TEST_VENV``        — explicit, wins over the default
#   2. ``$HOME/.venvs/hermes-test`` — the default
#
# Both are PORTABLE. No machine-specific path is spelled in this file, on
# purpose: a site-local literal in a shared script is a fact about one
# workstation that everyone else has to read past, and it rots silently when
# that machine changes. A box whose venv lives elsewhere — on a different
# volume, beside its checkouts, wherever — links it into place instead:
#
#   Windows: New-Item -ItemType Junction \
#              -Path "$env:USERPROFILE\.venvs\hermes-test" -Target <real path>
#   POSIX:   ln -s <real path> ~/.venvs/hermes-test
#
# ``$HOME/.venvs/hermes-test`` then resolves there with nothing set and nothing
# site-local committed. ``$HERMES_TEST_VENV`` is the alternative for anyone who
# would rather not link.
#
# Both are absent-safe: a candidate that does not exist, or that exists
# without pytest, is skipped exactly like the release venv below. Nothing here
# changes a checkout that HAS its own ``.venv`` — the local one still wins, and
# CI (which creates one) is byte-for-byte unchanged. Build the shared one from
# the pins the live install actually runs, not from a system interpreter:
#
#   <live venv>/Scripts/python.exe -m pip freeze  # minus the -e editable line
#   python -m venv <shared>; <shared>/Scripts/python.exe -m pip install \
#       -r <those pins> pytest pytest-asyncio pytest-timeout setuptools
#
# The editable ``-e ...#egg=hermes_agent`` line is dropped ON PURPOSE: it
# resolves to ONE checkout, and a shared venv that imports the primary
# checkout's ``hermes_cli`` while you run a worktree's tests is a silent lie.
# pytest's rootdir insertion (``tests/__init__.py`` makes the repo root the
# import base) already puts the RUNNING tree on ``sys.path``.
VENV=""
VENV_PYTHON=""
SKIPPED_VENVS=""
VENV_CANDIDATES=("$REPO_ROOT/.venv" "$REPO_ROOT/venv")
if [ -n "${HERMES_TEST_VENV:-}" ]; then
  VENV_CANDIDATES+=("$HERMES_TEST_VENV")
fi
VENV_CANDIDATES+=("$HOME/.venvs/hermes-test")
VENV_CANDIDATES+=("$HOME/.hermes/hermes-agent/venv")
for candidate in "${VENV_CANDIDATES[@]}"; do
  if [ -f "$candidate/bin/activate" ]; then
    if "$candidate/bin/python" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/bin/python"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
  # Native Windows venv layout: python.exe and activate live under
  # Scripts/, and there is no bin/. Anyone running this script from
  # Git Bash / MSYS with a `python -m venv`- or uv-created venv hits
  # this branch — without it the canonical runner refuses to start.
  if [ -f "$candidate/Scripts/activate" ]; then
    if "$candidate/Scripts/python.exe" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/Scripts/python.exe"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
done

if [ -n "$SKIPPED_VENVS" ]; then
  for skipped in $SKIPPED_VENVS; do
    echo "▶ skipping venv without pytest: $skipped" >&2
  done
fi

if [ -n "$VENV" ]; then
  PYTHON="$VENV_PYTHON"
  # Say WHICH venv won. With a shared candidate in the list, "the suite was
  # green" is only a fact once you know which pins produced it — and a
  # worktree's run now silently uses an environment that is not inside it.
  echo "▶ venv: $VENV"
elif [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ] \
    && "$HERMES_PYTHON" -c 'import pytest' 2>/dev/null; then
  # Guard with an import check: HERMES_PYTHON may point at the RELEASE
  # venv (no pytest) when inherited from a wrapped `hermes` binary rather
  # than the devShell hook.
  PYTHON="$HERMES_PYTHON"
  echo "▶ no local venv — using Nix dev venv via HERMES_PYTHON: $PYTHON"
else
  echo "error: no virtualenv with pytest found. Probed, in order:" >&2
  for candidate in "${VENV_CANDIDATES[@]}"; do
    echo "         $candidate" >&2
  done
  echo "       and HERMES_PYTHON is not a python with pytest (enter the Nix devShell," >&2
  echo "       create $REPO_ROOT/.venv, or build the shared venv — see the comment" >&2
  echo "       above the probe in this script)" >&2
  if [ -n "$SKIPPED_VENVS" ]; then
    echo "       (skipped for missing pytest:$SKIPPED_VENVS — install dev extras there, or create $REPO_ROOT/.venv)" >&2
  fi
  exit 1
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Windows location variables (computed before we drop env) ───────────────
# `env -i` forwards HOME, which is enough on POSIX. Native Windows CPython
# resolves Path.home() from USERPROFILE (or HOMEDRIVE+HOMEPATH), stdlib
# platform paths come from LOCALAPPDATA/APPDATA, ssl/sockets need SYSTEMROOT,
# and tempfile needs TEMP/TMP. Dropping them breaks collection on native
# Windows (issues #67385, #70813). These are location variables, not
# credentials, so forwarding them keeps the isolation intent intact. Each is
# only forwarded when actually set, so POSIX runs are byte-for-byte unchanged.
WIN_ENV=()
for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do
  if [ -n "${!_win_var:-}" ]; then
    WIN_ENV+=("$_win_var=${!_win_var}")
  fi
done

# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
#
# The Windows platform vars below are opt-in for the same reason PATH/HOME are:
# they are not credentials, they are how the OS answers "where is the user, and
# where is scratch space". Without USERPROFILE/LOCALAPPDATA/APPDATA, CPython's
# ``Path.home()`` has nothing to resolve against and every import that touches
# it dies at COLLECTION time — a baseline run on Windows reported ~56 files
# failing for reasons no source change caused, which makes the failure-set diff
# (the only honest "is this mine?" signal) unreadable. SYSTEMROOT is required by
# the socket/ssl/subprocess machinery on Windows, and TEMP/TMP keep tempfile off
# a fabricated path. All six are absent-safe: `${VAR:+VAR="$VAR"}` expands to
# nothing on POSIX, so Linux/macOS/CI runs are byte-for-byte unchanged.
#
# SYSTEMROOT is read UPPERCASE on purpose. Windows treats env var names as
# case-insensitive; the shell reading them here does not. git-bash exports the
# variable as `SYSTEMROOT`, so the mixed-case `${SystemRoot:+…}` this line used
# to carry expanded to NOTHING and the var was silently dropped from the
# hermetic env — the exact class of dead guard it was written to prevent. The
# child is still handed it under the spelling Windows itself uses.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── The operator's REAL store root, named for the fence ─────────────────────
#
# ``tests/hermes_cli/_gateway_fence.py`` has a "would run hermes against the
# operator's REAL store" arm. It learns that root by calling the production
# resolver, ``hermes_constants.get_default_hermes_root()`` — which reads
# ``HERMES_HOME``. Under BARE pytest, launched from an operator shell where
# HERMES_HOME names the live store, that answers correctly and the arm works.
#
# Under THIS script it did not, and the reason is the `env -i` two lines below:
# HERMES_HOME is deliberately not forwarded, so ``tests/conftest.py`` mints a
# throwaway session home, the fence imports AFTER that, and the resolver hands
# it the TEMPDIR. Measured on this workstation 2026-09-03, before this block:
#
#   _REAL_ROOT under run_tests.sh = C:\...\Temp\hermes-test-home-g_quyxlh
#   _REAL_ROOT under bare pytest  = X:\Eternia\.hermes
#
# and a `hermes config get` argv aimed at X:\Eternia\.hermes\profiles\alice
# classified ALLOWED in the first case, REFUSED in the second.
#
# So the whole arm — and the three tests in test_gateway_spawn_fence.py that
# drive it — was measuring a directory that had existed for a few milliseconds
# and would never appear in any argv. The defence existed only on the path
# nobody is told to use.
#
# Forwarding HERMES_HOME itself is NOT the fix: conftest must keep installing
# its own hermetic home, and handing the child the real one would put the live
# store back in front of every test — the hazard, not the guard. Instead the
# root travels under a dedicated TEST-ONLY name the fence reads first.
# HERMES_REAL_HOME was NOT reused: that is a production variable
# (``hermes_constants.py:1004`` ``_iter_real_home_candidates``, whose first
# candidate it is — the OS-user home an ACP child inherits, not a
# store root) and ``tests/conftest.py`` blanks it per test on purpose.
#
# Computed with the probed venv python and the production resolver rather than
# re-derived in shell, so the "which profile dir belongs to which root"
# unwrapping has exactly one implementation. Fail-soft: if the probe prints
# nothing the variable is not forwarded and the fence falls back to today's
# behavior.
REAL_HERMES_ROOT="$(
  "$PYTHON" -c 'import sys; sys.path.insert(0, "."); from hermes_constants import get_default_hermes_root; print(get_default_hermes_root())' \
    2>/dev/null || true
)"
if [ -n "$REAL_HERMES_ROOT" ]; then
  echo "▶ real store root handed to the gateway fence: $REAL_HERMES_ROOT"
fi

# Fork (Git Bash / MSYS / WSL): a native-Windows "$PYTHON" (…/Scripts/python.exe)
# cannot open a POSIX-style /x/... or /mnt/x/... script path, so translate the
# runner path to the spelling Windows itself uses before exec'ing.
RUNNER_PATH="$SCRIPT_DIR/run_tests_parallel.py"
if command -v cygpath >/dev/null 2>&1 && [[ "$PYTHON" == *.exe ]]; then
  RUNNER_PATH="$(cygpath -w "$RUNNER_PATH")"
elif [[ "$PYTHON" == *.exe && "$RUNNER_PATH" =~ ^/mnt/([A-Za-z])/(.*)$ ]]; then
  drive="${BASH_REMATCH[1]^^}"
  rest="${BASH_REMATCH[2]//\//\\}"
  RUNNER_PATH="${drive}:\\${rest}"
fi

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_E2E_BROWSER:+HERMES_E2E_BROWSER="$HERMES_E2E_BROWSER"} \
  ${HERMES_TEST_TMP_ROOT:+HERMES_TEST_TMP_ROOT="$HERMES_TEST_TMP_ROOT"} \
  ${REAL_HERMES_ROOT:+HERMES_TEST_REAL_ROOT="$REAL_HERMES_ROOT"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$RUNNER_PATH" "$@"
